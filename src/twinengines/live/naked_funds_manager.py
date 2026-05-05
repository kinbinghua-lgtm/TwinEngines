"""
naked-third-digit-live 专用资金管理（不依赖 live-run / 影子 / Regime）。

- RiskGuard：单日相对回撤、单笔敞口占比、并发「持仓」数（与 low_conf_tp_watch 对齐）
- 绝对亏损上限：沿用 PolymarketRuntimeCfg.daily_max_loss_usdc（.env DAILY_MAX_LOSS_USDC），按 UTC 日初权益锚定

止盈卖出（减仓）不走本模块检查，由 naked_pm_runner 单独调用 save_after_equity_refresh 仅刷新快照与链上 KV。
"""

from __future__ import annotations

import time
from typing import Any, Optional

from ..io.logging_setup import get_logger
from ..io.polymarket_client import PolymarketClient
from ..io.state_store import KEY_LAST_EQUITY, StateStore
from ..risk.limits import RiskCfg, RiskGuard

logger = get_logger(__name__)

NAKED_TP_STATE_KEY = "low_conf_tp_watch"
KEY_NAKED_FUNDS_SNAPSHOT = "naked_funds.snapshot"


def _utc_ms_calendar_day_key(now_ms: int) -> int:
    return int(now_ms // 86_400_000)


class NakedFundsManager:
    """SQLite 持久化 + RiskGuard；仅拦截新开多入场。"""

    def __init__(
        self,
        store: StateStore,
        daily_max_loss_usdc: float,
        *,
        risk_cfg: Optional[RiskCfg] = None,
    ) -> None:
        self.store = store
        self.daily_max_loss_usdc = max(0.0, float(daily_max_loss_usdc))
        rc = risk_cfg or RiskCfg(max_concurrent_open=1)
        self.guard = RiskGuard(cfg=rc)
        self._abs: dict[str, Any] = {}

    @classmethod
    def from_runtime(
        cls,
        store: StateStore,
        *,
        daily_max_loss_usdc: float,
        daily_drawdown_stop: float = 0.05,
    ) -> NakedFundsManager:
        dd = max(0.0, float(daily_drawdown_stop))
        return cls(
            store,
            daily_max_loss_usdc,
            risk_cfg=RiskCfg(
                daily_drawdown_stop=dd if dd > 0.0 else 10_000.0,
                max_concurrent_open=1,
            ),
        )

    def load_restore(self, now_ms: int) -> None:
        raw = self.store.get(KEY_NAKED_FUNDS_SNAPSHOT, default=None)
        if not isinstance(raw, dict):
            raw = {}
        risk = raw.get("risk_daily")
        if isinstance(risk, dict) and risk:
            self.guard.merge_daily_snapshot(risk, now_ts_ms=now_ms)
        absn = raw.get("abs_usd")
        self._abs = absn if isinstance(absn, dict) else {}

    def _persist(self) -> None:
        self.store.put(
            KEY_NAKED_FUNDS_SNAPSHOT,
            {
                "v": 1,
                "risk_daily": self.guard.snapshot_daily(),
                "abs_usd": dict(self._abs),
            },
        )

    def _audit_block(self, reason: str, extra: dict[str, Any]) -> None:
        try:
            self.store.append_audit(
                "naked_funds_blocked",
                {"reason": reason, "ts_ms": int(time.time() * 1000), **extra},
            )
        except Exception as e:
            logger.debug("naked_funds audit append failed: %s", e)

    def check_new_entry(self, poly: PolymarketClient, st: dict[str, Any], proposed_stake: float) -> tuple[bool, Optional[str]]:
        """返回 (是否允许开新多, 拒绝原因码)。"""
        now_ms = int(time.time() * 1000)
        d = _utc_ms_calendar_day_key(now_ms)
        eq = poly.fetch_account_equity_usdc()
        if eq is None:
            return False, "equity_unavailable"
        eqf = float(eq)

        ad = int(self._abs.get("day_key", -1))
        if ad != d:
            self._abs = {"day_key": d, "day_start_equity": eqf, "halted": False}
        else:
            if bool(self._abs.get("halted")):
                self._persist()
                return False, "daily_max_loss_usd_halted"
            day_start = float(self._abs.get("day_start_equity", eqf))
            loss = day_start - eqf
            if loss > self.daily_max_loss_usdc + 1e-9:
                self._abs["halted"] = True
                self._persist()
                self._audit_block(
                    "daily_max_loss_usd",
                    {"day_start_equity": day_start, "equity": eqf, "loss": loss, "limit": self.daily_max_loss_usdc},
                )
                return False, "daily_max_loss_usd"

        watch = st.get(NAKED_TP_STATE_KEY)
        n_open = 1 if isinstance(watch, dict) else 0
        open_exposure = 0.0
        if isinstance(watch, dict):
            try:
                px = float(watch.get("entry_avg_price") or 0.0)
                sh = float(watch.get("last_remaining_shares") or 0.0)
                open_exposure = max(0.0, px * sh)
            except (TypeError, ValueError):
                open_exposure = 0.0

        ok, reason = self.guard.can_open(
            equity=eqf,
            now_ts_ms=now_ms,
            proposed_stake=float(proposed_stake),
            open_exposure=open_exposure,
            n_open=n_open,
        )
        self._persist()
        if not ok and reason:
            self._audit_block(str(reason), {"equity": eqf, "proposed_stake": float(proposed_stake), "n_open": n_open})
        return ok, reason

    def save_after_equity_refresh(self, poly: PolymarketClient) -> None:
        """成交或平仓后刷新 Risk 格子、绝对亏损锚定日与链上 KV 资金展示。"""
        now_ms = int(time.time() * 1000)
        eq = poly.fetch_account_equity_usdc()
        if eq is None:
            return
        eqf = float(eq)
        d = _utc_ms_calendar_day_key(now_ms)
        ad = int(self._abs.get("day_key", -1))
        if ad != d:
            self._abs = {"day_key": d, "day_start_equity": eqf, "halted": False}
        self.guard.on_equity_update(equity=eqf, now_ts_ms=now_ms)
        self._persist()
        try:
            self.store.put(KEY_LAST_EQUITY, eqf)
        except Exception as e:
            logger.warning("save_after_equity_refresh kv last_equity failed: %s", e)
