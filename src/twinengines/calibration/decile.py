"""
反转概率阈值标定（来自需求文档第六节）：

1. 用 MLE 拟合好的生存模型，对历史 (111/000) 窗口逐窗计算反转概率
2. 按概率从低到高排序，等分十组
3. 找到真实反转率显著高于基线的层级
4. 取这些层级最低概率下限作为反转策略阈值

显著性判定: 该层真实反转率 > 基线 + significance_margin
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..model.fit import _aggregate_per_window
from ..survival import SurvivalParams, reversal_probability_vec


@dataclass(frozen=True)
class CalibrationReport:
    baseline_prob: float
    threshold: float
    bin_table: pd.DataFrame
    significance_margin: float


def _model_probs_per_window(event_table: pd.DataFrame, params: SurvivalParams) -> pd.DataFrame:
    """
    用与 backtest 一致的 *剩余时间累计概率* P_remaining(d_k, T_k) 评估每个观测点,
    并取窗口内最大值作为该窗口的模型预测概率, 与"实盘最早何时跨阈"语义对齐。

    这与 fit.py 的训练目标 (路径累计 hazard 拟合 reversed flag) 不同,
    但保证 calibration -> 阈值 -> backtest 决策三者使用同一推理公式。
    """
    agg = _aggregate_per_window(event_table)
    if agg.empty:
        return pd.DataFrame(columns=["window_id", "p_model", "event"])

    p_max = []
    for ds, ts in zip(agg["ds"].tolist(), agg["ts"].tolist()):
        if len(ds) == 0:
            p_max.append(0.0)
            continue
        ps = reversal_probability_vec(np.asarray(ds, dtype=float), np.asarray(ts, dtype=float), params)
        p_max.append(float(np.max(ps)))

    return pd.DataFrame(
        {
            "window_id": agg["window_id"].to_numpy(),
            "p_model": np.asarray(p_max, dtype=float),
            "event": agg["event"].to_numpy(dtype=int),
        }
    )


def calibrate_threshold(
    event_table: pd.DataFrame,
    params: SurvivalParams,
    *,
    n_bins: int = 10,
    significance_margin: float = 0.05,
) -> CalibrationReport:
    """
    对训练/验证样本做十分位分层标定，返回反转概率阈值。
    """
    df = _model_probs_per_window(event_table, params)
    if df.empty:
        raise ValueError("event_table is empty; cannot calibrate")

    baseline = float(df["event"].mean())

    df = df.sort_values("p_model").reset_index(drop=True)
    df["bin"] = pd.qcut(
        df["p_model"].rank(method="first"),
        q=n_bins,
        labels=False,
        duplicates="drop",
    )

    bin_stats = (
        df.groupby("bin")
        .agg(
            n=("event", "size"),
            real_reversal_rate=("event", "mean"),
            p_min=("p_model", "min"),
            p_max=("p_model", "max"),
        )
        .reset_index()
    )

    high_quality = bin_stats[
        bin_stats["real_reversal_rate"] >= baseline + significance_margin
    ]
    if high_quality.empty:
        threshold = float(bin_stats["p_max"].max())
    else:
        threshold = float(high_quality["p_min"].min())

    return CalibrationReport(
        baseline_prob=baseline,
        threshold=threshold,
        bin_table=bin_stats,
        significance_margin=significance_margin,
    )
