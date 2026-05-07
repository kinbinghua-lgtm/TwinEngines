from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

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
    closed: bool = False
    exit_seq: int = 0
    last_check_ts_ms: int = 0
    last_exit_ts_ms: int = 0
    last_reason: Optional[str] = None


@dataclass
class PositionExitGuardCfg:
    enabled: bool = True
    take_profit_multiple: float = 2.0
    check_interval_sec: float = 1.0
    min_exit_quote_usdc: float = 1.0


class PositionExitGuard:
    def __init__(self, *, client: PolymarketClient, store: Optional[StateStore] = None, cfg: Optional[PositionExitGuardCfg] = None) -> None:
        self.client = client
        self.store = store
        self.cfg = cfg or PositionExitGuardCfg()
        self._positions: dict[str, ExitPosition] = {}
        self._restore_positions()

    def register_entry(self, *, window_id: str, direction: str, token_id: str, entry_price: float, entry_cost_usdc: float, entry_shares: float, market_end_ts_ms: int, client_order_id: str) -> None:
        if not self.cfg.enabled:
            return
        if float(entry_shares) <= 0:
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
        )
        self._positions[key] = pos
        self._audit("exit_guard_entry_registered", pos, {})
        self._persist_positions()
        logger.info("exit_guard registered entry window_id=%s dir=%s entry=%.4f shares=%.4f", pos.window_id, pos.direction, pos.entry_price, pos.entry_shares)

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
                pos.last_reason = f"check_failed:{str(e)[:80]}"
                logger.warning("exit_guard check failed window_id=%s err=%s", pos.window_id, e)
                self._audit("exit_guard_check_failed", pos, {"error": str(e)[:160]})
            if pos.closed:
                self._positions.pop(key, None)
            self._persist_positions()

    def snapshot(self) -> dict[str, Any]:
        return {"enabled": bool(self.cfg.enabled), "positions": [asdict(p) for p in self._positions.values() if not p.closed]}

    def _restore_positions(self) -> None:
        if self.store is None:
            return
        rows = self.store.get("exit_guard.positions", default=[])
        if not isinstance(rows, list):
            return
        now_ms = int(time.time() * 1000)
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

    def _persist_positions(self) -> None:
        if self.store is None:
            return
        self.store.put("exit_guard.positions", [asdict(p) for p in self._positions.values() if not p.closed])

    def _check_position(self, pos: ExitPosition, *, now_ms: int) -> None:
        min_shares = float(POLYMARKET_PLATFORM.min_limit_order_shares)
        if pos.remaining_shares + 1e-9 < min_shares:
            pos.closed = True
            pos.last_reason = "dust_remaining"
            self._audit("exit_guard_closed", pos, {"reason": pos.last_reason})
            return
        if int(pos.market_end_ts_ms) <= now_ms:
            pos.closed = True
            pos.last_reason = "market_ended"
            self._audit("exit_guard_closed", pos, {"reason": pos.last_reason})
            return
        book = self.client.fetch_book_depth(pos.token_id, max_levels=20)
        if book.get("stale"):
            pos.last_reason = "book_stale"
            return
        best_bid = float(book.get("best_bid") or 0.0)
        if best_bid + 1e-12 < float(pos.entry_price) * float(self.cfg.take_profit_multiple):
            pos.last_reason = "hold_below_2x"
            return
        self._exit_5share_loop(pos, best_bid=best_bid)

    def _exit_5share_loop(self, pos: ExitPosition, *, best_bid: float) -> None:
        min_shares = float(POLYMARKET_PLATFORM.min_limit_order_shares)
        sold_any = False
        while pos.remaining_shares + 1e-9 >= min_shares:
            book = self.client.fetch_book_depth(pos.token_id, max_levels=20)
            if book.get("stale"):
                pos.last_reason = "exit_book_stale"
                break
            bid = float(book.get("best_bid") or 0.0)
            if bid + 1e-12 < float(pos.entry_price) * float(self.cfg.take_profit_multiple):
                pos.last_reason = "exit_bid_below_2x"
                break
            chunk = min_shares if pos.remaining_shares > min_shares * 2 else pos.remaining_shares
            if chunk * bid < float(self.cfg.min_exit_quote_usdc):
                pos.last_reason = "exit_quote_too_small"
                break
            pos.exit_seq += 1
            coid = f"{pos.window_id}:{pos.direction}:exit2x:{pos.exit_seq}"
            ticket = self.client.submit_order(side="SELL", token_id=pos.token_id, price=bid, size_quote_usdc=chunk * bid, size_shares=chunk, client_order_id=coid)
            self._audit("exit_guard_order", pos, {"client_order_id": coid, "reason": "2x_5share", "price": round(bid, 4), "target_shares": round(chunk, 4), "state": ticket.state.value, "error": ticket.last_error, "exchange_order_id": ticket.exchange_order_id, "filled_shares": round(float(ticket.filled_size_shares or 0.0), 4)})
            if ticket.state not in (OrderState.FILLED, OrderState.PARTIAL) or float(ticket.filled_size_shares or 0.0) <= 0:
                pos.last_reason = f"exit_failed:{ticket.state.value}:{ticket.last_error}"
                break
            sold = min(float(pos.remaining_shares), float(ticket.filled_size_shares or 0.0))
            pos.remaining_shares = max(0.0, float(pos.remaining_shares) - sold)
            pos.last_exit_ts_ms = int(time.time() * 1000)
            pos.last_reason = "2x_5share"
            sold_any = True
            logger.info("exit_guard 2x sold window_id=%s shares=%.4f price=%.4f remaining=%.4f", pos.window_id, sold, bid, pos.remaining_shares)
        if sold_any:
            self._persist_positions()
        if pos.remaining_shares + 1e-9 < min_shares:
            pos.closed = True
            self._audit("exit_guard_closed", pos, {"reason": "fully_or_dust_exited"})

    @staticmethod
    def _key(window_id: str, token_id: str) -> str:
        return f"{window_id}:{token_id}"

    def _audit(self, kind: str, pos: ExitPosition, extra: dict[str, Any]) -> None:
        if self.store is None:
            return
        payload = {"window_id": pos.window_id, "direction": pos.direction, "token_id_suffix": pos.token_id[-12:], "entry_price": round(pos.entry_price, 4), "entry_cost_usdc": round(pos.entry_cost_usdc, 2), "entry_shares": round(pos.entry_shares, 4), "remaining_shares": round(pos.remaining_shares, 4), "last_reason": pos.last_reason}
        payload.update(extra)
        self.store.append_audit(kind, payload)
