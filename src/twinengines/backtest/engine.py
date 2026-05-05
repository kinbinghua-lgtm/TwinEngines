"""
双引擎回测执行器（v2）。

变更:
    - 直接消费 WindowSample.obs_times_ms / obs_prices, 不再做秒级插值
    - 接入新版 PolymarketCfg + settle_pnl 完整摩擦模型
    - 接入 risk.RiskGuard 与 risk.sizing.stake_for_trade
    - 维护组合净值, 动态 sizing
    - 日级 mark-to-market 用于 RiskGuard 触发回撤限制
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np

from ..signals import (
    ReversalStabilityTracker,
    Signal,
    SignalThresholds,
    TrendStabilityTracker,
    decide_signal_v2,
    expected_value_reversal,
    expected_value_trend,
)
from ..survival import SurvivalParams, reversal_probability
from ..data.window import WindowSample
from ..risk.limits import RiskCfg, RiskGuard
from ..risk.regime import RegimeManager
from ..risk.sizing import SizingCfg, stake_for_trade
from .polymarket import PolymarketCfg, quote_for_reversal, quote_for_trend, settle_pnl
from .shadow_book_overlay import ShadowBookOverlay


@dataclass(frozen=True)
class BacktestConfig:
    """
    双引擎使用不同 sizing:
        - reversal_sizing_cfg: 反转引擎 (低频高质, 允许较大 stake)
        - trend_sizing_cfg: 顺势引擎 (薄利稳频, 强制小 stake 以容忍摩擦放大)
    """

    params: SurvivalParams
    thresholds: SignalThresholds
    baseline_reversal_prob: float
    poly_cfg: PolymarketCfg = PolymarketCfg()
    reversal_sizing_cfg: SizingCfg = SizingCfg(kelly_fraction=0.30, max_stake_ratio=0.10)
    trend_sizing_cfg: SizingCfg = SizingCfg(kelly_fraction=0.10, max_stake_ratio=0.03)
    risk_cfg: RiskCfg = RiskCfg()
    initial_equity: float = 10_000.0

    trend_rising_tolerance: float = 0.003
    reversal_falling_tolerance: float = 0.003
    reversal_falling_drawdown_tol: float = 0.05
    obs_step_sec: float = 1.0
    enable_risk_guard: bool = True
    independent_engine_drawdown: bool = False
    enable_reversal_filter: bool = True
    reversal_filter_max_p_rev_min: float = 0.215
    reversal_filter_consec_ge_baseline_min: int = 2
    enable_rejected_reversal_trend_capture: bool = False
    rejected_trend_extreme_ev_min: float = 0.01
    rejected_trend_capture_ev_min: float = 0.01

    regime_manager: RegimeManager | None = None
    shadow_book_overlay: ShadowBookOverlay | None = None


@dataclass
class TradeRecord:
    window_id: int
    window_start_ms: int
    trigger: str
    signal: Signal
    seconds_left_at_entry: int
    entry_price: float
    payoff_gross: float
    fill_ratio: float
    stake: float
    won: bool
    pnl: float
    equity_after: float
    audit: dict


@dataclass
class BacktestReport:
    trades: list[TradeRecord] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    final_equity: float = 0.0
    overlay_total_signals: int = 0
    overlay_matched_signals: int = 0
    overlay_executable_signals: int = 0
    overlay_reject_reasons: dict[str, int] = field(default_factory=dict)
    halted_trend_count: int = 0
    halted_reversal_count: int = 0
    reversal_filter_blocked_true: int = 0
    reversal_filter_blocked_false: int = 0

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def total_pnl(self) -> float:
        return float(sum(t.pnl for t in self.trades))

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return float(np.mean([t.won for t in self.trades]))

    def split_by_signal(self) -> dict[Signal, list[TradeRecord]]:
        groups: dict[Signal, list[TradeRecord]] = {Signal.REVERSAL: [], Signal.TREND: []}
        for t in self.trades:
            groups.setdefault(t.signal, []).append(t)
        return groups


def _signed_deviation(baseline: float, price: float) -> float:
    """相对偏离 (百分比)。baseline 非正时返回 0 (防御实盘异常数据)。"""
    if baseline is None or not (baseline > 0):
        return 0.0
    return (price - baseline) / baseline * 100.0


def _replay_window(
    sample: WindowSample,
    cfg: BacktestConfig,
    *,
    window_id: int,
    equity: float,
    guard: RiskGuard,
    guard_trend: RiskGuard | None = None,
    guard_reversal: RiskGuard | None = None,
    reversal_sizing_override: SizingCfg | None = None,
    trend_sizing_override: SizingCfg | None = None,
    blocked_log: list[dict] | None = None,
) -> TradeRecord | None:
    """
    单窗口回放 (v2):
        - 同步运行两个 tracker (顺势 + 反转), 在每个观测点判定信号
        - 反转优先: 同帧若反转条件满足, 直接入场反转
        - 否则若顺势条件满足, 入场顺势
        - 一窗一笔
    """
    n = len(sample.d_per_obs)
    if n == 0:
        return None

    trend_tracker = TrendStabilityTracker(
        window_sec=cfg.thresholds.trend_signal_stability_sec_min,
        prob_max=cfg.thresholds.trend_reversal_prob_max,
        rising_tolerance=cfg.trend_rising_tolerance,
        sample_interval_sec=float(cfg.obs_step_sec),
    )
    rev_tracker = ReversalStabilityTracker(
        window_sec=cfg.thresholds.reversal_signal_stability_sec_min,
        baseline_prob=cfg.baseline_reversal_prob,
        offset_min=cfg.thresholds.reversal_baseline_offset_min,
        falling_tolerance=cfg.reversal_falling_tolerance,
        falling_drawdown_tol=cfg.reversal_falling_drawdown_tol,
        sample_interval_sec=float(cfg.obs_step_sec),
    )
    max_ev_trend_seen = float("-inf")

    for k in range(n):
        d_abs = float(sample.d_per_obs[k])
        t_left = float(sample.t_per_obs[k])
        price = float(sample.obs_prices[k])
        d_signed = _signed_deviation(sample.baseline, price)
        p_rev = reversal_probability(d_abs, t_left, cfg.params)
        q_trend_now = quote_for_trend(
            trigger=sample.trigger,
            d_signed_pct=d_signed,
            seconds_left=int(t_left),
            prior_reversal=cfg.baseline_reversal_prob,
            cfg=cfg.poly_cfg,
            stake_quote=None,
        )
        ev_trend_now = expected_value_trend(p_rev, q_trend_now.payoff_gross, 1.0)
        if ev_trend_now > max_ev_trend_seen:
            max_ev_trend_seen = float(ev_trend_now)

        trend_consec = trend_tracker.update(p_rev)
        rev_consec = rev_tracker.update(p_rev)
        trend_non_rising = trend_tracker.is_non_rising()
        rev_non_falling = rev_tracker.is_non_falling()

        sig = decide_signal_v2(
            reversal_probability=p_rev,
            reversal_baseline_probability=cfg.baseline_reversal_prob,
            trend_consec=trend_consec,
            trend_non_rising=trend_non_rising,
            reversal_consec=rev_consec,
            reversal_non_falling=rev_non_falling,
            thresholds=cfg.thresholds,
        )

        if sig == Signal.NONE:
            continue

        rev_sizing = reversal_sizing_override or cfg.reversal_sizing_cfg
        tr_sizing = trend_sizing_override or cfg.trend_sizing_cfg

        trend_capture_on_reject = False
        if sig == Signal.REVERSAL:
            if cfg.enable_reversal_filter:
                pass_filter = (
                    float(p_rev) >= float(cfg.reversal_filter_max_p_rev_min)
                    and int(rev_consec) >= int(cfg.reversal_filter_consec_ge_baseline_min)
                )
                if not pass_filter:
                    if (
                        cfg.enable_rejected_reversal_trend_capture
                        and max_ev_trend_seen >= float(cfg.rejected_trend_extreme_ev_min)
                        and ev_trend_now >= float(cfg.rejected_trend_capture_ev_min)
                    ):
                        # 实验分支: 仅在反转被过滤器拒绝时, 尝试同窗口顺势捕捉
                        trend_capture_on_reject = True
                        sig = Signal.TREND
                    else:
                        if blocked_log is not None:
                            blocked_log.append({
                                "window_id": window_id,
                                "trigger": sample.trigger,
                                "reason": "reversal_filtered_out",
                                "signal": sig.value,
                                "p_reversal": round(float(p_rev), 6),
                                "rev_consec": int(rev_consec),
                                "is_true_reversal": bool(sample.reversed),
                            })
                        # 仅拦截该次反转触发，继续扫描本窗口后续时刻
                        continue
                if trend_capture_on_reject and blocked_log is not None:
                    blocked_log.append({
                        "window_id": window_id,
                        "trigger": sample.trigger,
                        "reason": "reversal_filtered_out_but_trend_capture_on",
                        "signal": "TREND",
                        "p_reversal": round(float(p_rev), 6),
                        "rev_consec": int(rev_consec),
                        "ev_trend": round(float(ev_trend_now), 6),
                        "max_ev_trend_seen": round(float(max_ev_trend_seen), 6),
                        "is_true_reversal": bool(sample.reversed),
                    })
            if sig == Signal.REVERSAL:
                quote_raw = quote_for_reversal(
                    trigger=sample.trigger,
                    d_signed_pct=d_signed,
                    seconds_left=int(t_left),
                    prior_reversal=cfg.baseline_reversal_prob,
                    cfg=cfg.poly_cfg,
                    stake_quote=None,
                )
                ev = expected_value_reversal(p_rev, quote_raw.payoff_gross, 1.0)
                if ev <= 0:
                    continue
                won = sample.reversed
                sizing_cfg = rev_sizing
                win_prob = p_rev
                reason = "reversal_v2"
                quote_fn = quote_for_reversal
            else:
                quote_raw = q_trend_now
                ev = ev_trend_now
                if ev <= 0:
                    continue
                # v2 路径补齐: 顺势 EV 门槛由 thresholds.trend_min_expected_value 控制
                if ev < float(cfg.thresholds.trend_min_expected_value):
                    continue
                won = not sample.reversed
                sizing_cfg = tr_sizing
                win_prob = 1.0 - p_rev
                reason = "trend_capture_on_reversal_filter_reject" if trend_capture_on_reject else "trend_v2"
                quote_fn = quote_for_trend
        else:
            quote_raw = quote_for_trend(
                trigger=sample.trigger,
                d_signed_pct=d_signed,
                seconds_left=int(t_left),
                prior_reversal=cfg.baseline_reversal_prob,
                cfg=cfg.poly_cfg,
                stake_quote=None,
            )
            ev = expected_value_trend(p_rev, quote_raw.payoff_gross, 1.0)
            if ev <= 0:
                continue
            # v2 路径补齐: 顺势 EV 门槛由 thresholds.trend_min_expected_value 控制
            if ev < float(cfg.thresholds.trend_min_expected_value):
                continue
            won = not sample.reversed
            sizing_cfg = tr_sizing
            win_prob = 1.0 - p_rev
            reason = "trend_v2"
            quote_fn = quote_for_trend

        proposed = stake_for_trade(
            portfolio_equity=equity,
            win_prob=win_prob,
            net_payoff=quote_raw.payoff_gross,
            cfg=sizing_cfg,
        )
        if proposed <= 0:
            continue

        quote = quote_fn(
            trigger=sample.trigger,
            d_signed_pct=d_signed,
            seconds_left=int(t_left),
            prior_reversal=cfg.baseline_reversal_prob,
            cfg=cfg.poly_cfg,
            stake_quote=proposed,
        )
        overlay_info = None
        if cfg.shadow_book_overlay is not None:
            _inc_overlay_counter(blocked_log, key="overlay_total_signals")
            overlay_info = cfg.shadow_book_overlay.evaluate(
                ts_ms=int(sample.obs_times_ms[k]),
                side=sig.value,
                model_quote=quote,
            )
            if bool(overlay_info.get("matched")):
                _inc_overlay_counter(
                    blocked_log,
                    key="overlay_matched_signals",
                )
            if bool(overlay_info.get("executable")):
                quote = overlay_info.get("quote") or quote
                _inc_overlay_counter(
                    blocked_log,
                    key="overlay_executable_signals",
                )
            elif overlay_info.get("matched"):
                reason = str(overlay_info.get("reason") or "overlay_rejected")
                _inc_overlay_counter(blocked_log, key=f"overlay_reason::{reason}")
                if blocked_log is not None:
                    blocked_log.append({
                        "window_id": window_id,
                        "trigger": sample.trigger,
                        "reason": f"shadow_book_not_executable:{reason}",
                        "signal": sig.value if hasattr(sig, "value") else str(sig),
                        "equity": round(float(equity), 4),
                        "p_reversal": round(float(p_rev), 6),
                        "ts_ms": int(sample.obs_times_ms[k]),
                    })
                return None
            else:
                _inc_overlay_counter(blocked_log, key="overlay_reason::no_snapshot")

        if cfg.enable_risk_guard:
            ok, reason_blocked = guard.can_open(
                equity=equity,
                now_ts_ms=int(sample.obs_times_ms[k]),
                proposed_stake=proposed,
                open_exposure=0.0,
                n_open=0,
            )
            if not ok:
                if blocked_log is not None:
                    blocked_log.append({
                        "window_id": window_id,
                        "trigger": sample.trigger,
                        "reason": reason_blocked or "risk_blocked",
                        "signal": sig.value if hasattr(sig, "value") else str(sig),
                        "proposed_stake": round(float(proposed), 4),
                        "equity": round(float(equity), 4),
                        "p_reversal": round(float(p_rev), 6),
                    })
                return None
            if cfg.independent_engine_drawdown:
                eg = guard_trend if sig == Signal.TREND else guard_reversal
                if eg is not None:
                    ok_e, reason_e = eg.can_open(
                        equity=equity,
                        now_ts_ms=int(sample.obs_times_ms[k]),
                        proposed_stake=0.0,
                        open_exposure=0.0,
                        n_open=0,
                    )
                    if not ok_e and reason_e == "halted_by_daily_drawdown":
                        rs = "halted_by_daily_drawdown_trend" if sig == Signal.TREND else "halted_by_daily_drawdown_reversal"
                        if blocked_log is not None:
                            blocked_log.append({
                                "window_id": window_id,
                                "trigger": sample.trigger,
                                "reason": rs,
                                "signal": sig.value if hasattr(sig, "value") else str(sig),
                                "proposed_stake": round(float(proposed), 4),
                                "equity": round(float(equity), 4),
                                "p_reversal": round(float(p_rev), 6),
                            })
                        return None

        result = settle_pnl(won=won, quote=quote, stake=proposed, cfg=cfg.poly_cfg)
        return TradeRecord(
            window_id=window_id,
            window_start_ms=sample.window_start_ms,
            trigger=sample.trigger,
            signal=sig,
            seconds_left_at_entry=int(t_left),
            entry_price=quote.effective_price,
            payoff_gross=quote.payoff_gross,
            fill_ratio=quote.fill_ratio,
            stake=proposed,
            won=won,
            pnl=float(result["pnl"]),
            equity_after=equity + float(result["pnl"]),
            audit=result | {
                "reason": reason,
                "p_reversal": p_rev,
                "ev": ev,
                "entry_ts_ms": int(sample.obs_times_ms[k]),
                "trend_consec": trend_consec,
                "rev_consec": rev_consec,
                "trend_non_rising": trend_non_rising,
                "rev_non_falling": rev_non_falling,
                "overlay": overlay_info or {"matched": False, "reason": "disabled"},
            },
        )

    return None


def run_backtest(
    samples: Iterable[WindowSample],
    cfg: BacktestConfig,
) -> BacktestReport:
    """
    若 cfg.regime_manager 提供, 每窗根据当前 equity 动态选 sizing+risk_cfg;
    否则使用 cfg 内置的固定 sizing/risk。
    """
    report = BacktestReport()
    rm = cfg.regime_manager
    guard = RiskGuard(cfg=cfg.risk_cfg)
    guard_trend = None
    guard_reversal = None
    if cfg.independent_engine_drawdown:
        from ..risk.limits import RiskCfg as _RiskCfg
        base = cfg.risk_cfg
        guard = RiskGuard(cfg=_RiskCfg(
            daily_drawdown_stop=9.9,
            max_concurrent_open=base.max_concurrent_open,
            max_total_exposure_ratio=base.max_total_exposure_ratio,
            equity_floor=base.equity_floor,
        ))
        guard_trend = RiskGuard(cfg=_RiskCfg(
            daily_drawdown_stop=base.daily_drawdown_stop,
            max_concurrent_open=10**9,
            max_total_exposure_ratio=10.0,
            equity_floor=-1e18,
        ))
        guard_reversal = RiskGuard(cfg=_RiskCfg(
            daily_drawdown_stop=base.daily_drawdown_stop,
            max_concurrent_open=10**9,
            max_total_exposure_ratio=10.0,
            equity_floor=-1e18,
        ))
    equity = cfg.initial_equity
    regime_history: list[dict] = []

    for wid, sample in enumerate(samples):
        if rm is not None:
            rev_cfg, tr_cfg, risk_cfg, regime_name = rm.resolve(equity)
            if not regime_history or regime_history[-1]["name"] != regime_name:
                regime_history.append({"window_id": wid, "equity": round(equity, 2), "name": regime_name})
            guard.swap_cfg(risk_cfg)
        else:
            rev_cfg = None
            tr_cfg = None

        if cfg.enable_risk_guard and guard.equity_below_floor:
            report.blocked.append({"window_id": wid, "equity": round(equity, 4), "reason": "equity_below_floor"})
            break

        rec = _replay_window(
            sample,
            cfg,
            window_id=wid,
            equity=equity,
            guard=guard,
            guard_trend=guard_trend,
            guard_reversal=guard_reversal,
            reversal_sizing_override=rev_cfg,
            trend_sizing_override=tr_cfg,
            blocked_log=report.blocked,
        )
        if rec is None:
            continue
        equity = rec.equity_after
        report.trades.append(rec)

    report.final_equity = equity
    if regime_history:
        report.blocked.append({"regime_history": regime_history})
    _collect_overlay_stats(report)
    return report


def _inc_overlay_counter(blocked_log: list[dict] | None, *, key: str) -> None:
    if blocked_log is None:
        return
    blocked_log.append({"_overlay_counter": key})


def _collect_overlay_stats(report: BacktestReport) -> None:
    reason_counts: dict[str, int] = {}
    total = 0
    matched = 0
    executable = 0
    kept: list[dict] = []
    for item in report.blocked:
        if "_overlay_counter" in item:
            tag = str(item["_overlay_counter"])
            if tag == "overlay_matched_signals":
                matched += 1
            elif tag == "overlay_total_signals":
                total += 1
            elif tag == "overlay_executable_signals":
                executable += 1
            elif tag.startswith("overlay_reason::"):
                k = tag.split("::", 1)[1]
                reason_counts[k] = reason_counts.get(k, 0) + 1
            continue
        kept.append(item)
    report.blocked = kept
    report.overlay_total_signals = total
    report.overlay_matched_signals = matched
    report.overlay_executable_signals = executable
    report.overlay_reject_reasons = reason_counts
    report.halted_trend_count = sum(
        1 for b in kept if str(b.get("reason") or "") == "halted_by_daily_drawdown_trend"
    )
    report.halted_reversal_count = sum(
        1 for b in kept if str(b.get("reason") or "") == "halted_by_daily_drawdown_reversal"
    )
