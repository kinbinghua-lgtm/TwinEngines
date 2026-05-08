from __future__ import annotations

import math
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
    max_adverse_prob_seen: float = 0.0
    max_held_prob_seen: float = 0.0
    last_p_up: Optional[float] = None
    last_p_down: Optional[float] = None
    strong_reverse_triggered: bool = False
    exit_intent_reason: Optional[str] = None
    last_exit_attempt_ts_ms: int = 0
    exit_failure_count: int = 0


@dataclass
class PositionExitGuardCfg:
    enabled: bool = True
    check_interval_sec: float = 1.0
    min_exit_quote_usdc: float = 1.0
    adverse_prob_epsilon: float = 1e-6
    held_prob_drawdown_exit: float = 0.18
    min_held_prob_exit: float = 0.52
    last_seconds_exit_sec: float = 20.0
    strong_adverse_prob_exit: float = 0.62
    strong_prob_gap_exit: float = 0.12
    catastrophic_adverse_prob_exit: float = 0.78
    held_prob_floor_exit: float = 0.35
    held_prob_decay_exit_floor: float = 0.52
    exit_retry_cooldown_sec: float = 2.0
    max_exit_attempts_per_position: int = 8
    early_entry_reversal_exit_sec: float = 45.0
    early_entry_adverse_prob_exit: float = 0.58
    early_entry_prob_gap_exit: float = 0.16
    late_reversal_relax_seconds_left: float = 15.0
    late_reversal_adverse_prob_exit: float = 0.93


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

    def observe_probability(self, *, window_id: str, p_up: float, p_down: float) -> None:
        if not self.cfg.enabled:
            return
        for pos in list(self._positions.values()):
            if pos.closed or str(pos.window_id) != str(window_id):
                continue
            pos.last_p_up = float(p_up)
            pos.last_p_down = float(p_down)
            adverse_dir = "down" if pos.direction == "up" else "up"
            adverse_prob = float(p_down if adverse_dir == "down" else p_up)
            held_prob = float(p_up if pos.direction == "up" else p_down)
            if adverse_prob > float(pos.max_adverse_prob_seen or 0.0):
                pos.max_adverse_prob_seen = adverse_prob
            if held_prob > float(pos.max_held_prob_seen or 0.0):
                pos.max_held_prob_seen = held_prob
            self._maybe_exit_on_probability_danger(
                pos,
                trigger="probability_observed",
                signal_dir="down" if p_down > p_up else "up",
                p_up=float(p_up),
                p_down=float(p_down),
                held_prob=held_prob,
                adverse_prob=adverse_prob,
                ts_ms=int(time.time() * 1000),
            )
            self._persist_positions()

    def observe_signal(self, *, window_id: str, best_dir: str, p_up: float, p_down: float, ts_ms: Optional[int] = None) -> None:
        if not self.cfg.enabled:
            return
        signal_dir = str(best_dir or "").lower()
        if signal_dir not in ("up", "down"):
            return
        for pos in list(self._positions.values()):
            if pos.closed or str(pos.window_id) != str(window_id):
                continue
            pos.last_p_up = float(p_up)
            pos.last_p_down = float(p_down)
            adverse_dir = "down" if pos.direction == "up" else "up"
            adverse_prob = float(p_down if adverse_dir == "down" else p_up)
            previous_max = float(pos.max_adverse_prob_seen or 0.0)
            held_prob = float(p_up if pos.direction == "up" else p_down)
            previous_held_max = float(pos.max_held_prob_seen or 0.0)
            if adverse_prob > previous_max:
                pos.max_adverse_prob_seen = adverse_prob
            if held_prob > previous_held_max:
                pos.max_held_prob_seen = held_prob
            self._maybe_exit_on_probability_danger(
                pos,
                trigger="signal_observed",
                signal_dir=signal_dir,
                p_up=float(p_up),
                p_down=float(p_down),
                held_prob=held_prob,
                adverse_prob=adverse_prob,
                previous_held_max=previous_held_max,
                ts_ms=int(ts_ms or time.time() * 1000),
            )
            self._persist_positions()

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
        if pos.exit_intent_reason:
            self._exit_5share_loop(pos, reason=pos.exit_intent_reason)
            if pos.closed:
                return
        book = self.client.fetch_book_depth(pos.token_id, max_levels=20)
        if book.get("stale"):
            pos.last_reason = "book_stale"
            self._audit("exit_guard_book_unavailable", pos, {"reason": "book_stale"})
            return
        best_bid = float(book.get("best_bid") or 0.0)
        seconds_left = (int(pos.market_end_ts_ms) - int(now_ms)) / 1000.0
        held_prob = self._held_probability(pos)
        if seconds_left <= float(self.cfg.last_seconds_exit_sec) and held_prob is not None and held_prob < float(self.cfg.min_held_prob_exit):
            pos.last_reason = "last_seconds_weak_probability"
            self._audit("exit_guard_last_seconds_weak_probability", pos, {
                "seconds_left": round(seconds_left, 2),
                "held_prob": round(float(held_prob), 4),
                "best_bid": round(best_bid, 4),
            })
            self._exit_5share_loop(pos, reason="last_seconds_weak_probability")
            return
        pos.last_reason = "hold_direction_probability"

    def _maybe_exit_on_probability_danger(
        self,
        pos: ExitPosition,
        *,
        trigger: str,
        signal_dir: str,
        p_up: float,
        p_down: float,
        held_prob: float,
        adverse_prob: float,
        previous_held_max: Optional[float] = None,
        ts_ms: Optional[int] = None,
    ) -> None:
        if pos.closed:
            return
        prob_gap = float(adverse_prob) - float(held_prob)
        now_ms = int(ts_ms or time.time() * 1000)
        age_sec = max(0.0, (now_ms - int(pos.opened_ts_ms or now_ms)) / 1000.0)
        seconds_left = max(0.0, (int(pos.market_end_ts_ms) - now_ms) / 1000.0)
        late_relaxed = seconds_left <= float(self.cfg.late_reversal_relax_seconds_left)
        catastrophic_adverse_prob_exit = float(self.cfg.catastrophic_adverse_prob_exit)
        strong_adverse_prob_exit = float(self.cfg.strong_adverse_prob_exit)
        if late_relaxed:
            catastrophic_adverse_prob_exit = max(catastrophic_adverse_prob_exit, float(self.cfg.late_reversal_adverse_prob_exit))
            strong_adverse_prob_exit = max(strong_adverse_prob_exit, float(self.cfg.late_reversal_adverse_prob_exit))
        reason: Optional[str] = None
        if (
            age_sec <= float(self.cfg.early_entry_reversal_exit_sec)
            and adverse_prob >= float(self.cfg.early_entry_adverse_prob_exit)
            and prob_gap >= float(self.cfg.early_entry_prob_gap_exit)
        ):
            reason = "early_entry_reversal"
        elif adverse_prob >= catastrophic_adverse_prob_exit and held_prob <= max(float(self.cfg.held_prob_floor_exit), 1.0 - catastrophic_adverse_prob_exit):
            reason = "catastrophic_probability_reversal"
        elif adverse_prob >= strong_adverse_prob_exit and prob_gap >= float(self.cfg.strong_prob_gap_exit):
            reason = "direction_probability_reversed"
        elif previous_held_max and previous_held_max > 0 and held_prob <= previous_held_max - float(self.cfg.held_prob_drawdown_exit) and held_prob < float(self.cfg.held_prob_decay_exit_floor):
            reason = "held_probability_decay"
        if reason is None:
            return
        if pos.strong_reverse_triggered and reason in ("direction_probability_reversed", "catastrophic_probability_reversal", "early_entry_reversal"):
            return
        if reason in ("direction_probability_reversed", "catastrophic_probability_reversal", "early_entry_reversal"):
            pos.strong_reverse_triggered = True
        pos.exit_intent_reason = reason
        pos.last_reason = reason
        self._audit(f"exit_guard_{reason}", pos, {
            "trigger": trigger,
            "signal_dir": signal_dir,
            "p_up": round(float(p_up), 4),
            "p_down": round(float(p_down), 4),
            "held_prob": round(float(held_prob), 4),
            "adverse_prob": round(float(adverse_prob), 4),
            "prob_gap": round(float(prob_gap), 4),
            "position_age_sec": round(float(age_sec), 2),
            "seconds_left": round(float(seconds_left), 2),
            "late_reversal_relaxed": bool(late_relaxed),
            "effective_strong_adverse_prob_exit": round(float(strong_adverse_prob_exit), 4),
            "effective_catastrophic_adverse_prob_exit": round(float(catastrophic_adverse_prob_exit), 4),
            "max_held_prob_seen": round(float(pos.max_held_prob_seen or 0.0), 4),
            "ts_ms": now_ms,
        })
        self._exit_5share_loop(pos, reason=reason)

    def _exit_5share_loop(self, pos: ExitPosition, *, reason: str) -> None:
        min_shares = float(POLYMARKET_PLATFORM.min_limit_order_shares)
        now_ms = int(time.time() * 1000)
        cooldown_ms = int(max(0.2, float(self.cfg.exit_retry_cooldown_sec)) * 1000)
        if now_ms - int(pos.last_exit_attempt_ts_ms or 0) < cooldown_ms:
            return
        if int(pos.exit_failure_count or 0) >= int(self.cfg.max_exit_attempts_per_position):
            pos.last_reason = "exit_retry_limit_reached"
            self._audit("exit_guard_exit_retry_limit", pos, {"reason": reason, "failure_count": int(pos.exit_failure_count or 0)})
            return
        pos.last_exit_attempt_ts_ms = now_ms
        sold_any = False
        while pos.remaining_shares + 1e-9 >= min_shares:
            book = self.client.fetch_book_depth(pos.token_id, max_levels=20)
            if book.get("stale"):
                pos.last_reason = "exit_book_stale"
                pos.exit_failure_count += 1
                self._audit("exit_guard_exit_unavailable", pos, {"reason": reason, "failure": "book_stale"})
                break
            bid = float(book.get("best_bid") or 0.0)
            if bid <= 0:
                pos.last_reason = "exit_no_best_bid"
                pos.exit_failure_count += 1
                self._audit("exit_guard_exit_unavailable", pos, {
                    "reason": reason,
                    "failure": "no_best_bid",
                    "remaining_shares": round(float(pos.remaining_shares), 4),
                })
                break
            min_exit_quote = float(self.cfg.min_exit_quote_usdc)
            min_quote_shares = math.ceil((min_exit_quote / bid) * 100.0) / 100.0
            min_sell_shares = max(min_shares, min_quote_shares)
            if pos.remaining_shares + 1e-9 < min_sell_shares:
                if pos.remaining_shares * bid >= min_exit_quote:
                    chunk = pos.remaining_shares
                else:
                    pos.last_reason = "exit_quote_too_small"
                    pos.exit_failure_count += 1
                    self._audit("exit_guard_exit_quote_too_small", pos, {
                        "reason": reason,
                        "bid": round(bid, 4),
                        "remaining_shares": round(float(pos.remaining_shares), 4),
                        "remaining_quote": round(float(pos.remaining_shares) * bid, 4),
                        "min_exit_quote": round(min_exit_quote, 4),
                        "min_sell_shares": round(min_sell_shares, 4),
                    })
                    break
            else:
                chunk = min_sell_shares if pos.remaining_shares > min_sell_shares * 2 else pos.remaining_shares
            if chunk * bid < min_exit_quote:
                pos.last_reason = "exit_quote_too_small"
                pos.exit_failure_count += 1
                self._audit("exit_guard_exit_quote_too_small", pos, {
                    "reason": reason,
                    "bid": round(bid, 4),
                    "target_shares": round(chunk, 4),
                    "target_quote": round(chunk * bid, 4),
                    "min_exit_quote": round(min_exit_quote, 4),
                })
                break
            pos.exit_seq += 1
            coid = f"{pos.window_id}:{pos.direction}:exit:{reason}:{pos.exit_seq}"
            ticket = self.client.submit_order(side="SELL", token_id=pos.token_id, price=bid, size_quote_usdc=chunk * bid, size_shares=chunk, client_order_id=coid)
            self._audit("exit_guard_order", pos, {"client_order_id": coid, "reason": reason, "price": round(bid, 4), "target_shares": round(chunk, 4), "state": ticket.state.value, "error": ticket.last_error, "exchange_order_id": ticket.exchange_order_id, "filled_shares": round(float(ticket.filled_size_shares or 0.0), 4)})
            if ticket.state not in (OrderState.FILLED, OrderState.PARTIAL) or float(ticket.filled_size_shares or 0.0) <= 0:
                pos.last_reason = f"exit_failed:{ticket.state.value}:{ticket.last_error}"
                pos.exit_failure_count += 1
                self._audit("exit_guard_exit_failed", pos, {
                    "reason": reason,
                    "state": ticket.state.value,
                    "error": ticket.last_error,
                    "price": round(bid, 4),
                    "target_shares": round(chunk, 4),
                    "exchange_order_id": ticket.exchange_order_id,
                })
                break
            sold = min(float(pos.remaining_shares), float(ticket.filled_size_shares or 0.0))
            pos.remaining_shares = max(0.0, float(pos.remaining_shares) - sold)
            pos.last_exit_ts_ms = int(time.time() * 1000)
            pos.exit_failure_count = 0
            pos.last_reason = reason
            sold_any = True
            logger.info("exit_guard sold window_id=%s reason=%s shares=%.4f price=%.4f remaining=%.4f", pos.window_id, reason, sold, bid, pos.remaining_shares)
        if sold_any:
            self._persist_positions()
        if pos.remaining_shares + 1e-9 < min_shares:
            pos.closed = True
            pos.exit_intent_reason = None
            self._audit("exit_guard_closed", pos, {"reason": "fully_or_dust_exited"})

    @staticmethod
    def _held_probability(pos: ExitPosition) -> Optional[float]:
        if pos.direction == "up":
            return pos.last_p_up
        if pos.direction == "down":
            return pos.last_p_down
        return None

    @staticmethod
    def _key(window_id: str, token_id: str) -> str:
        return f"{window_id}:{token_id}"

    def _audit(self, kind: str, pos: ExitPosition, extra: dict[str, Any]) -> None:
        if self.store is None:
            return
        payload = {"window_id": pos.window_id, "direction": pos.direction, "token_id_suffix": pos.token_id[-12:], "entry_price": round(pos.entry_price, 4), "entry_cost_usdc": round(pos.entry_cost_usdc, 2), "entry_shares": round(pos.entry_shares, 4), "remaining_shares": round(pos.remaining_shares, 4), "max_adverse_prob_seen": round(float(pos.max_adverse_prob_seen or 0.0), 4), "max_held_prob_seen": round(float(pos.max_held_prob_seen or 0.0), 4), "last_p_up": pos.last_p_up, "last_p_down": pos.last_p_down, "last_reason": pos.last_reason, "exit_intent_reason": pos.exit_intent_reason, "exit_failure_count": int(pos.exit_failure_count or 0), "last_exit_attempt_ts_ms": int(pos.last_exit_attempt_ts_ms or 0)}
        payload.update(extra)
        self.store.append_audit(kind, payload)
