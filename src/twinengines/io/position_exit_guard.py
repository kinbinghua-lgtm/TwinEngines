from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Optional

from .config import POLYMARKET_PLATFORM
from .logging_setup import get_logger
from .polymarket_client import OrderState, PolymarketClient
from .state_store import StateStore

logger = get_logger(__name__)


@dataclass
class ExitPosition:
    window_id: str
    direction: str
    token_id: str
    entry_price: float
    entry_cost_usdc: float
    entry_shares: float
    remaining_shares: float
    market_end_ts_ms: int
    client_order_id: str
    opened_ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    peak_value_usdc: float = 0.0
    principal_recovered: bool = False
    closed: bool = False
    exit_seq: int = 0
    last_check_ts_ms: int = 0
    last_exit_ts_ms: int = 0
    last_reason: Optional[str] = None


@dataclass
class PositionExitGuardCfg:
    enabled: bool = True
    low_entry_price_max: float = 0.25
    principal_take_profit_multiple: float = 2.0
    principal_recovery_buffer: float = 1.03
    drawdown_activate_profit_multiple: float = 2.0
    peak_drawdown_ratio: float = 0.40
    final_protect_sec: float = 15.0
    final_profit_multiple: float = 1.5
    depth_haircut: float = 0.70
    chunk_count: int = 3
    check_interval_sec: float = 1.0
    min_exit_quote_usdc: float = 1.0


class PositionExitGuard:
    def __init__(
        self,
        *,
        client: PolymarketClient,
        store: Optional[StateStore] = None,
        cfg: Optional[PositionExitGuardCfg] = None,
        alert: Optional[Callable[[str, str, dict[str, Any]], None]] = None,
    ) -> None:
        self.client = client
        self.store = store
        self.cfg = cfg or PositionExitGuardCfg()
        self.alert = alert
        self._positions: dict[str, ExitPosition] = {}
        self._restore_positions()

    def register_entry(
        self,
        *,
        window_id: str,
        direction: str,
        token_id: str,
        entry_price: float,
        entry_cost_usdc: float,
        entry_shares: float,
        market_end_ts_ms: int,
        client_order_id: str,
    ) -> None:
        if not self.cfg.enabled:
            return
        if float(entry_price) > float(self.cfg.low_entry_price_max):
            return
        key = self._key(window_id, token_id)
        existing = self._positions.get(key)
        if existing is not None and not existing.closed:
            existing.entry_cost_usdc += float(entry_cost_usdc)
            existing.entry_shares += float(entry_shares)
            existing.remaining_shares += float(entry_shares)
            existing.entry_price = existing.entry_cost_usdc / max(existing.entry_shares, 1e-9)
            existing.last_reason = "entry_merged"
            self._audit("exit_guard_entry_merged", existing, {})
            self._persist_positions()
            return
        pos = ExitPosition(
            window_id=str(window_id),
            direction=str(direction).lower(),
            token_id=str(token_id),
            entry_price=float(entry_price),
            entry_cost_usdc=float(entry_cost_usdc),
            entry_shares=float(entry_shares),
            remaining_shares=float(entry_shares),
            market_end_ts_ms=int(market_end_ts_ms),
            client_order_id=str(client_order_id),
            peak_value_usdc=float(entry_cost_usdc),
        )
        self._positions[key] = pos
        self._audit("exit_guard_entry_registered", pos, {})
        self._persist_positions()
        logger.info(
            "exit_guard registered window_id=%s dir=%s entry=%.4f cost=%.2f shares=%.4f",
            pos.window_id, pos.direction, pos.entry_price, pos.entry_cost_usdc, pos.entry_shares,
        )

    def check_once(self) -> None:
        if not self.cfg.enabled:
            return
        now_ms = int(time.time() * 1000)
        for key, pos in list(self._positions.items()):
            if pos.closed:
                continue
            if now_ms - int(pos.last_check_ts_ms or 0) < int(self.cfg.check_interval_sec * 1000):
                continue
            pos.last_check_ts_ms = now_ms
            try:
                self._check_position(pos, now_ms=now_ms)
            except Exception as e:
                logger.warning("exit_guard check failed window_id=%s err=%s", pos.window_id, e)
                self._audit("exit_guard_check_failed", pos, {"error": str(e)[:160]})
            if pos.closed:
                self._positions.pop(key, None)
                self._persist_positions()

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.cfg.enabled),
            "positions": [
                {
                    "window_id": p.window_id,
                    "direction": p.direction,
                    "entry_price": round(p.entry_price, 4),
                    "entry_cost_usdc": round(p.entry_cost_usdc, 2),
                    "remaining_shares": round(p.remaining_shares, 4),
                    "peak_value_usdc": round(p.peak_value_usdc, 2),
                    "principal_recovered": p.principal_recovered,
                    "last_reason": p.last_reason,
                }
                for p in self._positions.values() if not p.closed
            ],
        }

    def _restore_positions(self) -> None:
        if self.store is None:
            return
        rows = self.store.get("exit_guard.positions", default=[])
        if not isinstance(rows, list):
            return
        now_ms = int(time.time() * 1000)
        restored = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                pos = ExitPosition(**row)
            except Exception:
                continue
            if pos.closed or int(pos.market_end_ts_ms) <= now_ms:
                continue
            if float(pos.remaining_shares) + 1e-9 < float(POLYMARKET_PLATFORM.min_limit_order_shares):
                continue
            self._positions[self._key(pos.window_id, pos.token_id)] = pos
            restored += 1
        if restored:
            logger.info("exit_guard restored %d active position(s)", restored)

    def _persist_positions(self) -> None:
        if self.store is None:
            return
        try:
            self.store.put(
                "exit_guard.positions",
                [asdict(p) for p in self._positions.values() if not p.closed],
            )
        except Exception as e:
            logger.debug("exit_guard persist failed err=%s", e)

    def _check_position(self, pos: ExitPosition, *, now_ms: int) -> None:
        min_shares = float(POLYMARKET_PLATFORM.min_limit_order_shares)
        if pos.remaining_shares + 1e-9 < min_shares:
            pos.closed = True
            pos.last_reason = "dust_remaining"
            self._audit("exit_guard_closed", pos, {"reason": pos.last_reason})
            return
        t_remaining = (int(pos.market_end_ts_ms) - now_ms) / 1000.0
        if t_remaining <= 0:
            pos.closed = True
            pos.last_reason = "market_ended"
            self._audit("exit_guard_closed", pos, {"reason": pos.last_reason})
            return

        book = self.client.fetch_book_depth(pos.token_id, max_levels=20)
        if book.get("stale"):
            pos.last_reason = "book_stale"
            return
        bids = book.get("bids") or []
        if not bids:
            pos.last_reason = "no_bids"
            return
        best_bid = float(book.get("best_bid") or bids[0].get("price") or 0.0)
        if best_bid <= 0:
            pos.last_reason = "no_best_bid"
            return

        mark_value = best_bid * float(pos.remaining_shares)
        pos.peak_value_usdc = max(float(pos.peak_value_usdc), mark_value)
        self._persist_positions()
        multiple = best_bid / max(float(pos.entry_price), 1e-9)

        reason: Optional[str] = None
        target_shares = 0.0
        if not pos.principal_recovered and multiple >= float(self.cfg.principal_take_profit_multiple):
            target_value = float(pos.entry_cost_usdc) * float(self.cfg.principal_recovery_buffer)
            target_shares = min(float(pos.remaining_shares), target_value / max(best_bid, 1e-9))
            reason = "recover_principal"
        elif (
            pos.peak_value_usdc >= float(pos.entry_cost_usdc) * float(self.cfg.drawdown_activate_profit_multiple)
            and mark_value <= pos.peak_value_usdc * (1.0 - float(self.cfg.peak_drawdown_ratio))
        ):
            target_shares = float(pos.remaining_shares) * 0.70
            reason = "peak_drawdown"
        elif t_remaining <= float(self.cfg.final_protect_sec) and multiple >= float(self.cfg.final_profit_multiple):
            target_shares = float(pos.remaining_shares) * 0.50
            reason = "final_protect"

        if reason is None or target_shares <= 0:
            pos.last_reason = "hold"
            return

        target_shares = min(float(pos.remaining_shares), max(target_shares, min_shares))
        if target_shares * best_bid < float(self.cfg.min_exit_quote_usdc):
            pos.last_reason = "target_quote_too_small"
            return
        self._exit_in_chunks(pos, bids=bids, best_bid=best_bid, target_shares=target_shares, reason=reason)

    def _exit_in_chunks(
        self,
        pos: ExitPosition,
        *,
        bids: list[dict[str, Any]],
        best_bid: float,
        target_shares: float,
        reason: str,
    ) -> None:
        min_shares = float(POLYMARKET_PLATFORM.min_limit_order_shares)
        chunks = max(1, int(self.cfg.chunk_count))
        remaining_target = min(float(target_shares), float(pos.remaining_shares))
        sold_any = False
        for _ in range(chunks):
            if remaining_target + 1e-9 < min_shares or pos.remaining_shares + 1e-9 < min_shares:
                break
            top_safe = self._safe_bid_shares_at_or_above(bids, best_bid) * float(self.cfg.depth_haircut)
            chunk_shares = min(remaining_target, pos.remaining_shares, max(min_shares, remaining_target / max(1, chunks)))
            chunk_shares = min(chunk_shares, top_safe)
            chunk_shares = self._floor_shares(chunk_shares)
            if chunk_shares + 1e-9 < min_shares:
                pos.last_reason = f"{reason}:depth_insufficient"
                break
            pos.exit_seq += 1
            coid = f"{pos.window_id}:{pos.direction}:exit:{reason}:{pos.exit_seq}"
            ticket = self.client.submit_order(
                side="SELL",
                token_id=pos.token_id,
                price=float(best_bid),
                size_quote_usdc=float(chunk_shares) * float(best_bid),
                size_shares=float(chunk_shares),
                client_order_id=coid,
            )
            self._audit("exit_guard_order", pos, {
                "reason": reason,
                "client_order_id": coid,
                "target_shares": round(chunk_shares, 4),
                "price": round(float(best_bid), 4),
                "state": ticket.state.value,
                "error": ticket.last_error,
                "exchange_order_id": ticket.exchange_order_id,
                "filled_shares": round(float(ticket.filled_size_shares or 0.0), 4),
            })
            if ticket.state in (OrderState.FILLED, OrderState.PARTIAL) and float(ticket.filled_size_shares or 0.0) > 0:
                sold = min(float(pos.remaining_shares), float(ticket.filled_size_shares or 0.0))
                pos.remaining_shares = max(0.0, float(pos.remaining_shares) - sold)
                remaining_target = max(0.0, remaining_target - sold)
                sold_any = True
                pos.last_exit_ts_ms = int(time.time() * 1000)
                if reason == "recover_principal":
                    pos.principal_recovered = True
                logger.info(
                    "exit_guard sold window_id=%s reason=%s shares=%.4f price=%.4f remaining=%.4f",
                    pos.window_id, reason, sold, best_bid, pos.remaining_shares,
                )
            else:
                pos.last_reason = f"{reason}:{ticket.state.value}:{ticket.last_error}"
                break
        if sold_any:
            pos.last_reason = reason
        if pos.remaining_shares + 1e-9 < min_shares:
            pos.closed = True
            self._audit("exit_guard_closed", pos, {"reason": "fully_or_dust_exited"})

    @staticmethod
    def _safe_bid_shares_at_or_above(bids: list[dict[str, Any]], limit_price: float) -> float:
        total = 0.0
        for level in bids:
            price = float(level.get("price") or 0.0)
            size = float(level.get("size") or 0.0)
            if price + 1e-12 < float(limit_price):
                break
            total += max(0.0, size)
        return total

    @staticmethod
    def _floor_shares(shares: float) -> float:
        return max(0.0, int(float(shares) * 10000) / 10000.0)

    @staticmethod
    def _key(window_id: str, token_id: str) -> str:
        return f"{window_id}:{token_id}"

    def _audit(self, kind: str, pos: ExitPosition, extra: dict[str, Any]) -> None:
        if self.store is None:
            return
        payload = {
            "window_id": pos.window_id,
            "direction": pos.direction,
            "token_id_suffix": pos.token_id[-12:],
            "entry_price": round(pos.entry_price, 4),
            "entry_cost_usdc": round(pos.entry_cost_usdc, 2),
            "entry_shares": round(pos.entry_shares, 4),
            "remaining_shares": round(pos.remaining_shares, 4),
            "peak_value_usdc": round(pos.peak_value_usdc, 2),
            "principal_recovered": bool(pos.principal_recovered),
        }
        payload.update(extra)
        try:
            self.store.append_audit(kind, payload)
        except Exception as e:
            logger.debug("exit_guard audit failed kind=%s err=%s", kind, e)
        self._persist_positions()
