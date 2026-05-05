"""
参数扫描器: 在固定 artifact + 固定测试窗口集上扫描信号参数, 输出对比表。

支持网格扫描:
    - trend_stability_sec
    - reversal_stability_sec
    - reversal_baseline_offset

每个组合输出:
    - 总笔数, 总 PnL, 各引擎笔数 / 胜率 / avg_pnl / sharpe
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .backtest.engine import BacktestConfig, run_backtest
from .backtest.metrics import summarize
from .backtest.polymarket import PolymarketCfg
from .backtest.shadow_book_overlay import ShadowBookOverlay
from .data.window import WindowSample
from .model.persist import StrategyArtifact
from .risk.limits import RiskCfg
from .signals import Signal, SignalThresholds


@dataclass
class SweepCell:
    trend_stab: int
    rev_stab: int
    rev_offset: float
    n_trades: int
    total_pnl: float
    final_equity: float
    n_trend: int
    win_rate_trend: float
    avg_pnl_trend: float
    n_reversal: int
    win_rate_reversal: float
    avg_pnl_reversal: float
    blocked_total: int
    blocked_halted_dd: int


def run_sweep(
    samples: list[WindowSample],
    artifact: StrategyArtifact,
    *,
    trend_stab_grid: list[int],
    rev_stab_grid: list[int],
    rev_offset_grid: list[float] = [0.0],
    trend_prob_max: float = 0.15,
    initial_equity: float = 10_000.0,
    use_shadow_book: bool = False,
    shadow_book_path: str = "logs/shadow_signals_3h_book_v2.jsonl",
    shadow_book_mode: str = "exact",
    enable_risk_guard: bool = True,
    obs_step_sec: float = 1.0,
    friction_mode: str = "off",
    taker_slippage_bps: float = 300.0,
    independent_engine_drawdown: bool = False,
) -> list[SweepCell]:
    cells: list[SweepCell] = []
    overlay = (
        ShadowBookOverlay.from_jsonl(shadow_book_path, mode=shadow_book_mode)
        if use_shadow_book else None
    )
    for ts in trend_stab_grid:
        for rs in rev_stab_grid:
            for off in rev_offset_grid:
                th = SignalThresholds(
                    trend_reversal_prob_max=trend_prob_max,
                    trend_signal_stability_sec_min=ts,
                    reversal_baseline_offset_min=off,
                    reversal_signal_stability_sec_min=rs,
                )
                cfg = BacktestConfig(
                    params=artifact.params,
                    thresholds=th,
                    baseline_reversal_prob=artifact.baseline_prob,
                    poly_cfg=PolymarketCfg(
                        friction_mode=str(friction_mode),
                        taker_slippage_bps=float(taker_slippage_bps),
                    ),
                    risk_cfg=RiskCfg(),
                    initial_equity=initial_equity,
                    shadow_book_overlay=overlay,
                    enable_risk_guard=bool(enable_risk_guard),
                    obs_step_sec=float(obs_step_sec),
                    independent_engine_drawdown=bool(independent_engine_drawdown),
                )
                report = run_backtest(samples, cfg)
                summary = summarize(report)
                tg = summary.get("TREND")
                rg = summary.get("REVERSAL")
                blocked_total = len(report.blocked)
                blocked_halted_dd = sum(
                    1 for b in report.blocked
                    if str(b.get("reason") or "") in {
                        "halted_by_daily_drawdown",
                        "halted_by_daily_drawdown_trend",
                        "halted_by_daily_drawdown_reversal",
                    }
                )
                cells.append(
                    SweepCell(
                        trend_stab=ts,
                        rev_stab=rs,
                        rev_offset=off,
                        n_trades=int(report.n_trades),
                        total_pnl=float(report.total_pnl),
                        final_equity=float(report.final_equity),
                        n_trend=int(tg.n_trades) if tg else 0,
                        win_rate_trend=float(tg.win_rate) if tg else 0.0,
                        avg_pnl_trend=float(tg.avg_pnl) if tg else 0.0,
                        n_reversal=int(rg.n_trades) if rg else 0,
                        win_rate_reversal=float(rg.win_rate) if rg else 0.0,
                        avg_pnl_reversal=float(rg.avg_pnl) if rg else 0.0,
                        blocked_total=int(blocked_total),
                        blocked_halted_dd=int(blocked_halted_dd),
                    )
                )
    return cells


def cells_to_dataframe(cells: list[SweepCell]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "trend_stab": c.trend_stab,
                "rev_stab": c.rev_stab,
                "rev_offset": c.rev_offset,
                "n_trades": c.n_trades,
                "total_pnl": round(c.total_pnl, 2),
                "final_equity": round(c.final_equity, 2),
                "n_trend": c.n_trend,
                "wr_trend": round(c.win_rate_trend, 3),
                "avg_pnl_trend": round(c.avg_pnl_trend, 2),
                "n_rev": c.n_reversal,
                "wr_rev": round(c.win_rate_reversal, 3),
                "avg_pnl_rev": round(c.avg_pnl_reversal, 2),
                "blocked_total": c.blocked_total,
                "blocked_halted_dd": c.blocked_halted_dd,
            }
            for c in cells
        ]
    )
