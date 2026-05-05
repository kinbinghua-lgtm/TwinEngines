"""
Q1 - 二进制序列分布是否符合反正弦定律？

反正弦定律 (Levy arcsine law) 描述: 简单随机游走在 [0, T] 内位于正侧的时间分布
近似为 1/(pi * sqrt(x*(1-x)))，**两端密度极高**。

对应到我们的 5 位序列 (5个收盘价相对开盘的符号位):
    - 全 1 (11111) 或全 0 (00000) 的概率应显著高于均匀分布的 1/32
    - 我们使用的"前3位 111/000"是更宽松的触发条件 (16种序列中包含)
    - 还要单独看 5 位序列分布 (32种)

本模块对真实分钟线统计:
    - 完整 5 位序列频率 (32 种)
    - 前 3 位触发频率 (8 种)
    - 与"独立伯努利 p=0.5"基线的对比 (Chi-squared)
    - 与反正弦定律理论值的对比 (聚合到"正侧时间")
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..data.window import WINDOW_MINUTES, normalize_to_ms
from ..sequence import build_fixed_baseline_sequence


@dataclass(frozen=True)
class SequenceDistributionReport:
    n_windows: int
    full_seq_counts: dict[str, int]
    full_seq_freq: dict[str, float]
    trigger_counts: dict[str, int]
    trigger_freq: dict[str, float]
    binomial_baseline_freq: float
    chi2_full: float
    chi2_full_pvalue: float
    arcsine_table: list[dict]
    target_111_000_share: float


def _generate_5bit_seqs() -> list[str]:
    return [format(i, "05b") for i in range(32)]


def _arcsine_density(k: int, n: int) -> float:
    """
    反正弦定律理论密度 (k = 正侧位数, n = 总位数).
    使用 P(K=k) = C(2k,k)*C(2(n-k),n-k) / 4^n 经典公式 (k in [0, n]).
    """
    from math import comb

    if not 0 <= k <= n:
        return 0.0
    return comb(2 * k, k) * comb(2 * (n - k), n - k) / (4**n)


def analyze_sequence_distribution(bars_1m: pd.DataFrame) -> SequenceDistributionReport:
    df = normalize_to_ms(bars_1m).sort_values("open_time").reset_index(drop=True)
    n_full = (len(df) // WINDOW_MINUTES) * WINDOW_MINUTES
    df = df.iloc[:n_full].reset_index(drop=True)

    seq_counter: Counter[str] = Counter()
    trig_counter: Counter[str] = Counter()
    for i in range(0, n_full, WINDOW_MINUTES):
        block = df.iloc[i : i + WINDOW_MINUTES]
        baseline = float(block["open"].iloc[0])
        if baseline <= 0:
            continue
        closes = block["close"].astype(float).tolist()
        seq = build_fixed_baseline_sequence(baseline, closes)
        seq_counter[seq] += 1
        trig_counter[seq[:3]] += 1

    n_windows = sum(seq_counter.values())
    if n_windows == 0:
        raise ValueError("no full 5-minute windows")

    full_freq = {s: seq_counter.get(s, 0) / n_windows for s in _generate_5bit_seqs()}
    trig_freq = {format(i, "03b"): trig_counter.get(format(i, "03b"), 0) / n_windows for i in range(8)}

    expected = n_windows / 32.0
    chi2 = sum(
        (seq_counter.get(s, 0) - expected) ** 2 / expected for s in _generate_5bit_seqs()
    )
    from scipy.stats import chi2 as chi2_dist  # type: ignore

    p = float(1.0 - chi2_dist.cdf(chi2, df=31))

    arcsine_rows: list[dict] = []
    n = 5
    for k in range(n + 1):
        empirical = sum(seq_counter.get(s, 0) for s in _generate_5bit_seqs() if s.count("1") == k)
        empirical /= n_windows
        theoretical = _arcsine_density(k, n)
        arcsine_rows.append(
            {
                "k_positives": k,
                "empirical_freq": empirical,
                "arcsine_theoretical": theoretical,
                "uniform_baseline": (
                    1
                    if n == 0
                    else (1 / 32.0)
                    * sum(1 for s in _generate_5bit_seqs() if s.count("1") == k)
                ),
            }
        )

    target_share = trig_freq.get("111", 0.0) + trig_freq.get("000", 0.0)
    return SequenceDistributionReport(
        n_windows=n_windows,
        full_seq_counts=dict(seq_counter),
        full_seq_freq=full_freq,
        trigger_counts=dict(trig_counter),
        trigger_freq=trig_freq,
        binomial_baseline_freq=1 / 32.0,
        chi2_full=float(chi2),
        chi2_full_pvalue=p,
        arcsine_table=arcsine_rows,
        target_111_000_share=float(target_share),
    )
