"""回测指标。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..signals import Signal
from .engine import BacktestReport


@dataclass(frozen=True)
class EngineStats:
    name: str
    n_trades: int
    win_rate: float
    avg_pnl: float
    total_pnl: float
    sharpe_like: float


def _sharpe_like(pnls: list[float]) -> float:
    if len(pnls) < 2:
        return 0.0
    arr = np.asarray(pnls, dtype=float)
    sd = float(arr.std(ddof=1))
    if sd <= 0:
        return 0.0
    return float(arr.mean() / sd) * np.sqrt(len(arr))


def summarize(report: BacktestReport) -> dict[str, EngineStats]:
    groups = report.split_by_signal()
    out: dict[str, EngineStats] = {}
    for sig in (Signal.REVERSAL, Signal.TREND):
        recs = groups.get(sig, [])
        pnls = [r.pnl for r in recs]
        out[sig.value] = EngineStats(
            name=sig.value,
            n_trades=len(recs),
            win_rate=float(np.mean([r.won for r in recs])) if recs else 0.0,
            avg_pnl=float(np.mean(pnls)) if recs else 0.0,
            total_pnl=float(np.sum(pnls)) if recs else 0.0,
            sharpe_like=_sharpe_like(pnls),
        )
    all_pnls = [t.pnl for t in report.trades]
    out["TOTAL"] = EngineStats(
        name="TOTAL",
        n_trades=report.n_trades,
        win_rate=report.win_rate,
        avg_pnl=float(np.mean(all_pnls)) if all_pnls else 0.0,
        total_pnl=report.total_pnl,
        sharpe_like=_sharpe_like(all_pnls),
    )
    return out
