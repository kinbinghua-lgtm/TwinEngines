"""
风控限额: 单日最大回撤 / 同时最大并发持仓 / 累计敞口。

设计:
    - RiskGuard 只做"是否准许下单"的查询
    - 下单与平仓回报由调用方 push 进来, 内部维护轻量状态
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class RiskCfg:
    daily_drawdown_stop: float = 0.05      # 当日权益跌 X% 立即停手
    max_concurrent_open: int = 5
    max_total_exposure_ratio: float = 0.30 # 累计未结算本金 / 净值上限
    equity_floor: float = 0.0              # 资金低于此值进入"优雅退出"状态 (永久)


@dataclass
class _DailyState:
    day_key: int = -1
    equity_high: float = 0.0
    equity_low: float = 0.0
    halted: bool = False


@dataclass
class RiskGuard:
    """
    输入:
        portfolio_equity: 当前净值
        now_ts_ms: 当前 ms 时间戳
        open_exposure: 未结算本金累计
        n_open: 未结算交易数

    永久退出态:
        equity_below_floor 一旦置 True 不再恢复, 调用方应停止运行并落库报告。

    在线热替换 cfg (实盘 RegimeManager 场景):
        - 调用 swap_cfg(new_cfg) 仅替换风控参数, 保留 _state (当日 high/low/halted)
          以及 equity_below_floor, 避免阶段切换瞬间绕过当日回撤限制。
        - 不要直接 new RiskGuard(cfg=...) 然后丢弃旧实例, 那会重置当日状态。
    """

    cfg: RiskCfg = field(default_factory=RiskCfg)
    _state: _DailyState = field(default_factory=_DailyState)
    equity_below_floor: bool = False

    def swap_cfg(self, new_cfg: RiskCfg) -> None:
        """热替换风控配置, 保留当日运行状态与 equity_below_floor 标志位。"""
        self.cfg = new_cfg

    @staticmethod
    def _day_key(now_ts_ms: int) -> int:
        return int(now_ts_ms // (24 * 3600 * 1000))

    def on_equity_update(self, *, equity: float, now_ts_ms: int) -> None:
        d = self._day_key(now_ts_ms)
        st = self._state
        if d != st.day_key:
            st.day_key = d
            st.equity_high = equity
            st.equity_low = equity
            st.halted = False
            return
        st.equity_high = max(st.equity_high, equity)
        st.equity_low = min(st.equity_low, equity)
        if st.equity_high > 0:
            dd = (st.equity_high - equity) / st.equity_high
            if dd >= self.cfg.daily_drawdown_stop:
                st.halted = True

    def snapshot_daily(self) -> dict[str, Any]:
        st = self._state
        return {
            "day_key": int(st.day_key),
            "equity_high": float(st.equity_high),
            "equity_low": float(st.equity_low),
            "halted": bool(st.halted),
        }

    def merge_daily_snapshot(self, snap: dict[str, Any], *, now_ts_ms: int) -> None:
        """若 snap 与当前 UTC 日历日一致则恢复格子（进程重启后续上 RiskGuard）。"""
        d = self._day_key(now_ts_ms)
        try:
            sd = int(snap.get("day_key", -1))
        except (TypeError, ValueError):
            return
        if sd != d:
            return
        st = self._state
        st.day_key = sd
        try:
            st.equity_high = float(snap.get("equity_high", 0.0) or 0.0)
            st.equity_low = float(snap.get("equity_low", 0.0) or 0.0)
            st.halted = bool(snap.get("halted", False))
        except (TypeError, ValueError):
            pass

    def can_open(
        self,
        *,
        equity: float,
        now_ts_ms: int,
        proposed_stake: float,
        open_exposure: float,
        n_open: int,
    ) -> tuple[bool, Optional[str]]:
        if self.equity_below_floor:
            return False, "equity_below_floor"
        if equity <= self.cfg.equity_floor:
            self.equity_below_floor = True
            return False, "equity_below_floor"
        self.on_equity_update(equity=equity, now_ts_ms=now_ts_ms)
        st = self._state
        if st.halted:
            return False, "halted_by_daily_drawdown"
        if n_open >= self.cfg.max_concurrent_open:
            return False, "max_concurrent_reached"
        if equity <= 0:
            return False, "equity_non_positive"
        new_total = open_exposure + proposed_stake
        if new_total / equity > self.cfg.max_total_exposure_ratio:
            return False, "total_exposure_exceeded"
        return True, None
