"""
持仓 reconciliation: 把本地账本 (我们认为持有什么) 与链上/CLOB 账本 (实际持有什么) 对齐。

实盘上的根本问题:
    1. 网络/API 抖动可能造成 "下单成功但本地未确认" 或 "本地认为成功实际被拒";
    2. 退款/合约结算 (5min 到期, USDC 自动结算) 我们不一定能瞬时感知;
    3. 多策略 / 多账户共用同一钱包时, 本地 ledger 视角不全。

设计:
    - 每 reconcile_interval_sec 拉一次:
        a) 链上 USDC 余额        (PolymarketClient.fetch_account_equity_usdc)
        b) 当前 condition_id 的 OPEN 持仓 (PolymarketClient.fetch_market_positions)
    - 与本地 ledger (持仓快照) 比对:
        * 本地认为有持仓 -> 链上必须有, 否则 trip 熔断;
        * 本地认为无持仓 -> 链上有 (未追踪名义 > drift 阈值 USDC) -> trip 熔断;
        * 余额异常波动 (超过 daily_max_loss_usdc) -> trip 熔断;
    - trip 后调用 alerting + position_lock (拒绝新单);
    - 任何 reconcile 失败 (查询不通) 不立刻 trip, 但累计 N 次后 trip。

接口:
    Reconciler(client, market_resolver, position_lock, alerting).start() / stop()
    Reconciler.snapshot() -> 监控指标
    Reconciler.report_local_position(window_id, side, token_id, expected_size_shares, expected_avg_price)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .logging_setup import get_logger
from .polymarket_client import PolymarketClient
from .market_resolver import MarketResolver
from .position_lock import PositionLock
from .state_store import StateStore, KEY_LAST_EQUITY, KEY_LAST_RECONCILE_TS

logger = get_logger(__name__)


def _position_row_notional_usdc(raw: dict) -> float:
    """链上 / Data API 持仓行折 USDC 名义 (用于与 drift 阈值比较)."""
    try:
        sz = float(raw.get("size") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if sz <= 0:
        return 0.0
    px: Optional[float] = None
    for k in ("avg_price", "avgPrice", "avgBuyPrice", "avgSellPrice", "curPrice", "price"):
        v = raw.get(k)
        if isinstance(v, (int, float)) and float(v) > 0:
            px = float(v)
            break
        if isinstance(v, str):
            try:
                t = float(v)
                if t > 0:
                    px = t
                    break
            except ValueError:
                pass
    if px is None or px <= 0:
        px = 0.5
    return abs(sz * px)


@dataclass
class LocalPosition:
    window_id: str
    side: str
    token_id: str
    expected_size_shares: float
    expected_avg_price: float
    opened_ts_ms: int
    note: str = ""


@dataclass
class ReconcilerCfg:
    interval_sec: float = 30.0
    fail_trip_threshold: int = 5             # 连续失败 N 次 trip 熔断
    position_size_drift_usdc: float = 2.0    # 链上有 / 本地无, 名义超此 USDC 才熔断 (见 _position_row_notional_usdc)
    equity_jump_alert_usdc: float = 50.0     # 单次 reconcile 余额跳变超此值 -> 告警
    # --dry-run-signals: 不下单、本地无持仓账本, equity/positions API 失败不应触发熔断或刷屏告警
    observe_only: bool = False


@dataclass
class Reconciler:
    client: PolymarketClient
    market_resolver: MarketResolver
    position_lock: PositionLock
    cfg: ReconcilerCfg = field(default_factory=ReconcilerCfg)
    store: Optional[StateStore] = None
    on_alert: Optional[Callable[[str, dict], None]] = None
    on_circuit_trip: Optional[Callable[[str], None]] = None

    _running: bool = False
    _thread: Optional[threading.Thread] = None
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _stop_evt: threading.Event = field(default_factory=threading.Event)
    _local_positions: dict[str, LocalPosition] = field(default_factory=dict)

    _consecutive_failures: int = 0
    _last_reconcile_ts_ms: float = 0.0
    _last_equity_usdc: Optional[float] = None
    _reconcile_count: int = 0
    _last_error: Optional[str] = None
    _circuit_triggered: bool = False

    # ---------------- 控制 ----------------

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop, name="Reconciler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # ---------------- 本地账本接口 ----------------

    def report_local_position(self, pos: LocalPosition) -> None:
        with self._lock:
            self._local_positions[pos.window_id] = pos
        logger.info(
            "reconciler local_pos added window_id=%s side=%s shares=%.4f avg_price=%.4f",
            pos.window_id, pos.side, pos.expected_size_shares, pos.expected_avg_price,
        )

    def merge_or_report_local_position(self, pos: LocalPosition) -> None:
        """同一 window 再次成交时累加股数并更新加权均价 (窗口内多笔成交)."""
        with self._lock:
            ex = self._local_positions.get(pos.window_id)
            if ex is None or ex.token_id != pos.token_id:
                self._local_positions[pos.window_id] = pos
                merged = pos
            else:
                s1, p1 = float(ex.expected_size_shares), float(ex.expected_avg_price)
                s2, p2 = float(pos.expected_size_shares), float(pos.expected_avg_price)
                st = s1 + s2
                avg = (s1 * p1 + s2 * p2) / st if st > 1e-12 else p2
                merged = LocalPosition(
                    window_id=pos.window_id,
                    side=pos.side,
                    token_id=pos.token_id,
                    expected_size_shares=st,
                    expected_avg_price=avg,
                    opened_ts_ms=int(ex.opened_ts_ms),
                    note=(ex.note + ";" + pos.note).strip(";"),
                )
                self._local_positions[pos.window_id] = merged
        logger.info(
            "reconciler local_pos merge window_id=%s side=%s shares=%.4f avg_price=%.4f",
            merged.window_id, merged.side, merged.expected_size_shares, merged.expected_avg_price,
        )

    def clear_local_position(self, window_id: str) -> None:
        with self._lock:
            self._local_positions.pop(window_id, None)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "circuit_triggered": self._circuit_triggered,
                "reconcile_count": self._reconcile_count,
                "consecutive_failures": self._consecutive_failures,
                "last_reconcile_ts_ms": int(self._last_reconcile_ts_ms),
                "last_equity_usdc": self._last_equity_usdc,
                "last_error": self._last_error,
                "observe_only": self.cfg.observe_only,
                "local_position_count": len(self._local_positions),
                "local_positions": [
                    {
                        "window_id": p.window_id,
                        "side": p.side,
                        "expected_shares": p.expected_size_shares,
                        "avg_price": p.expected_avg_price,
                    }
                    for p in self._local_positions.values()
                ],
            }

    # ---------------- 主循环 ----------------

    def _loop(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    return
                interval = self.cfg.interval_sec
            if self._stop_evt.wait(interval):
                return
            with self._lock:
                if not self._running:
                    return
            try:
                self.reconcile_once()
            except Exception as e:
                logger.exception("reconcile_once unhandled: %s", e)

    def reconcile_once(self) -> bool:
        equity = self.client.fetch_account_equity_usdc()
        active = self.market_resolver.get_active()
        condition_id = active.condition_id if active is not None else ""

        chain_positions: list[dict] = []
        if condition_id:
            chain_positions = self.client.fetch_market_positions(condition_id=condition_id)

        if equity is None and not chain_positions:
            self._record_failure("equity+positions both unavailable")
            return False

        with self._lock:
            self._consecutive_failures = 0
            self._reconcile_count += 1
            self._last_reconcile_ts_ms = time.time() * 1000.0
            self._last_error = None

            prev_equity = self._last_equity_usdc
            self._last_equity_usdc = equity

        if self.store is not None:
            try:
                self.store.put(KEY_LAST_EQUITY, equity)
                self.store.put(KEY_LAST_RECONCILE_TS, int(time.time() * 1000))
                self.store.append_audit("reconcile", {
                    "equity_usdc": equity,
                    "condition_id": condition_id,
                    "chain_positions_count": len(chain_positions),
                })
            except Exception as e:
                logger.warning("reconcile audit failed: %s", e)

        if (
            equity is not None
            and prev_equity is not None
            and not self.cfg.observe_only
        ):
            delta = equity - prev_equity
            if abs(delta) > self.cfg.equity_jump_alert_usdc:
                self._notify_alert(
                    "equity_jump",
                    {"prev": prev_equity, "now": equity, "delta": delta},
                )

        self._compare_with_local(chain_positions)
        return True

    def _compare_with_local(self, chain_positions: list[dict]) -> None:
        with self._lock:
            local = dict(self._local_positions)

        chain_by_token: dict[str, dict] = {}
        for p in chain_positions:
            tid = str(p.get("token_id") or p.get("asset") or "")
            if not tid:
                continue
            try:
                size = float(p.get("size") or 0)
            except (TypeError, ValueError):
                size = 0.0
            if size <= 0:
                continue
            chain_by_token[tid] = {"size": size, "raw": p}

        local_by_token: dict[str, LocalPosition] = {}
        for pos in local.values():
            local_by_token[pos.token_id] = pos

        only_local = set(local_by_token.keys()) - set(chain_by_token.keys())
        if only_local:
            for tid in only_local:
                pos = local_by_token[tid]
                self._fire_circuit(
                    "local_position_missing_on_chain",
                    {
                        "window_id": pos.window_id,
                        "side": pos.side,
                        "token_id_prefix": tid[:14],
                        "expected_shares": pos.expected_size_shares,
                    },
                )

        only_chain_notional = 0.0
        only_chain_shares = 0.0
        for tid, info in chain_by_token.items():
            if tid in local_by_token:
                continue
            raw = info.get("raw")
            if not isinstance(raw, dict):
                raw = {"size": info.get("size"), "avg_price": 0.5}
            only_chain_notional += _position_row_notional_usdc(raw)
            only_chain_shares += float(info.get("size") or 0.0)
        threshold = self.cfg.position_size_drift_usdc
        if only_chain_notional > threshold:
            self._fire_circuit(
                "untracked_chain_position",
                {
                    "notional_usdc": only_chain_notional,
                    "size_shares": only_chain_shares,
                    "threshold_usdc": threshold,
                },
            )

    # ---------------- 失败/熔断 ----------------

    def _record_failure(self, reason: str) -> None:
        with self._lock:
            self._last_error = reason
            if self.cfg.observe_only:
                # 降噪: 仅记录快照字段, 不累计失败次数, 避免无交易模式下的 API 抖动误触熔断
                logger.debug("reconcile failure (observe_only, ignored): %s", reason)
                return
            self._consecutive_failures += 1
            failures = self._consecutive_failures
        logger.warning("reconcile failure #%d: %s", failures, reason)
        if failures >= self.cfg.fail_trip_threshold:
            self._fire_circuit("reconcile_failures_exceeded", {"failures": failures, "reason": reason})

    def _fire_circuit(self, kind: str, payload: dict) -> None:
        if self.cfg.observe_only:
            logger.debug(
                "reconcile circuit suppressed (observe_only) kind=%s payload=%s",
                kind,
                payload,
            )
            return
        with self._lock:
            already = self._circuit_triggered
            self._circuit_triggered = True
        logger.error("RECONCILE CIRCUIT TRIPPED kind=%s payload=%s", kind, payload)
        self._notify_alert(kind, payload)
        if not already and self.on_circuit_trip is not None:
            try:
                self.on_circuit_trip(kind)
            except Exception as e:
                logger.exception("on_circuit_trip callback failed: %s", e)

    def _notify_alert(self, kind: str, payload: dict) -> None:
        if self.on_alert is None:
            return
        try:
            self.on_alert(kind, payload)
        except Exception as e:
            logger.exception("alert callback failed: %s", e)
