"""
信号判定层 (v2): 双引擎都用"稳定性 + 趋势方向"判定。

设计原则:
    顺势引擎 = 反转概率持续低位 + 不上升趋势
    反转引擎 = 反转概率持续高于基线 + 不快速回落趋势

不再使用:
    - reversal_prob_min (顶分位绝对阈值, 易漏掉稳健型反转)
    - ev_pullback_ratio (峰值回落, 不区分良性/恶性回落)

两个 tracker 共享一个判别原则:
    "持续 N 秒满足方向条件" + "在 N 秒滑窗里趋势不反向"
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque

import numpy as np


class Signal(str, Enum):
    NONE = "NONE"
    TREND = "TREND"
    REVERSAL = "REVERSAL"


@dataclass(frozen=True)
class SignalThresholds:
    """
    信号判定阈值 (v2)。

    顺势:
        trend_reversal_prob_max: P_rev 上限 (低于此值视为"低位")
        trend_signal_stability_sec_min: 持续低位且不上升的最小秒数

    反转:
        reversal_baseline_offset_min: P_rev 至少要超过 baseline 多少 (例如 0.0 = 严格 > baseline)
        reversal_signal_stability_sec_min: 持续高于基线且不回落的最小秒数

    旧字段保留向后兼容, 但不再被 v2 决策使用:
        reversal_prob_min, reversal_edge_vs_baseline_min, trend_min_expected_value
    """

    trend_reversal_prob_max: float
    trend_signal_stability_sec_min: int
    reversal_baseline_offset_min: float = 0.02
    reversal_signal_stability_sec_min: int = 5

    reversal_prob_min: float = 0.0
    reversal_edge_vs_baseline_min: float = 0.0
    trend_min_expected_value: float = 0.0


def _avg_slope(values: list[float]) -> float:
    """对一段 1Hz 序列估计单步平均斜率 (越大越上升)。"""
    n = len(values)
    if n < 2:
        return 0.0
    arr = np.asarray(values, dtype=float)
    diffs = np.diff(arr)
    return float(np.mean(diffs))


@dataclass
class TrendStabilityTracker:
    """
    顺势"低位且不上升"追踪器:
        - 在过去 window_sec 个观测里, P_rev <= prob_max
        - 滑窗内 P_rev 平均斜率 <= rising_tolerance (即不呈上升趋势)

    update(prob) 返回当前持续满足的连续秒数 (consec)。
    is_non_rising() 单独提供"窗口非上升"判定。
    """

    window_sec: int
    prob_max: float
    rising_tolerance: float = 0.003
    sample_interval_sec: float = 1.0

    _hits: Deque[bool] = field(default_factory=deque)
    _values: Deque[float] = field(default_factory=deque)

    def update(self, prob: float) -> int:
        ok = prob <= self.prob_max
        self._hits.append(ok)
        self._values.append(float(prob))
        while len(self._hits) > self.window_sec:
            self._hits.popleft()
        while len(self._values) > self.window_sec:
            self._values.popleft()

        consecutive = 0
        for hit in reversed(self._hits):
            if hit:
                consecutive += 1
            else:
                break
        return consecutive

    def is_non_rising(self) -> bool:
        if len(self._values) < 2:
            return True
        slope = _avg_slope(list(self._values))
        dt = self.sample_interval_sec if self.sample_interval_sec > 0 else 1.0
        slope_per_sec = slope / dt
        return slope_per_sec <= self.rising_tolerance


@dataclass
class ReversalStabilityTracker:
    """
    反转"高于基线且不快速回落"追踪器:
        - 在过去 window_sec 个观测里, P_rev >= baseline + offset_min
        - 滑窗内 P_rev 平均斜率 >= -falling_tolerance (即不呈快速下降趋势)
        - 同时窗口内最大回撤 (max - current) <= falling_drawdown_tol
            -> 防止"瞬时跳高随即回落"型假反转
    """

    window_sec: int
    baseline_prob: float
    offset_min: float = 0.0
    falling_tolerance: float = 0.003
    falling_drawdown_tol: float = 0.05
    sample_interval_sec: float = 1.0

    _hits: Deque[bool] = field(default_factory=deque)
    _values: Deque[float] = field(default_factory=deque)

    def update(self, prob: float) -> int:
        threshold = self.baseline_prob + self.offset_min
        ok = prob >= threshold
        self._hits.append(ok)
        self._values.append(float(prob))
        while len(self._hits) > self.window_sec:
            self._hits.popleft()
        while len(self._values) > self.window_sec:
            self._values.popleft()

        consecutive = 0
        for hit in reversed(self._hits):
            if hit:
                consecutive += 1
            else:
                break
        return consecutive

    def is_non_falling(self) -> bool:
        if len(self._values) < 2:
            return True
        slope = _avg_slope(list(self._values))
        dt = self.sample_interval_sec if self.sample_interval_sec > 0 else 1.0
        slope_per_sec = slope / dt
        if slope_per_sec < -self.falling_tolerance:
            return False
        max_in_window = max(self._values)
        drawdown = max_in_window - self._values[-1]
        return drawdown <= self.falling_drawdown_tol


def expected_value_trend(prob_reversal: float, payoff: float, stake: float = 1.0) -> float:
    win = 1.0 - prob_reversal
    return win * payoff - prob_reversal * stake


def expected_value_reversal(prob_reversal: float, payoff: float, stake: float = 1.0) -> float:
    return prob_reversal * payoff - (1.0 - prob_reversal) * stake


def decide_signal_v2(
    *,
    reversal_probability: float,
    reversal_baseline_probability: float,
    trend_consec: int,
    trend_non_rising: bool,
    reversal_consec: int,
    reversal_non_falling: bool,
    thresholds: SignalThresholds,
) -> Signal:
    """
    新决策 (反转优先):
        反转: P_rev >= baseline + offset 且持续 >= reversal_stability_sec 且不回落
        顺势: P_rev <= trend_prob_max 且持续 >= trend_stability_sec 且不上升
    """
    rev_threshold = reversal_baseline_probability + thresholds.reversal_baseline_offset_min
    if (
        reversal_probability >= rev_threshold
        and reversal_consec >= thresholds.reversal_signal_stability_sec_min
        and reversal_non_falling
    ):
        return Signal.REVERSAL

    if (
        reversal_probability <= thresholds.trend_reversal_prob_max
        and trend_consec >= thresholds.trend_signal_stability_sec_min
        and trend_non_rising
    ):
        return Signal.TREND

    return Signal.NONE


def decide_signal(
    *,
    reversal_probability: float,
    reversal_baseline_probability: float,
    signal_stability_sec: int,
    ev_trend_pullback: bool,
    ev_trend: float,
    thresholds: SignalThresholds,
) -> Signal:
    """旧 v1 决策, 仅用于向后兼容现有测试。新代码请用 decide_signal_v2。"""
    edge = reversal_probability - reversal_baseline_probability
    if (
        reversal_probability >= thresholds.reversal_prob_min
        and edge >= thresholds.reversal_edge_vs_baseline_min
    ):
        return Signal.REVERSAL

    if (
        reversal_probability <= thresholds.trend_reversal_prob_max
        and signal_stability_sec >= thresholds.trend_signal_stability_sec_min
        and ev_trend_pullback
        and ev_trend >= thresholds.trend_min_expected_value
    ):
        return Signal.TREND

    return Signal.NONE


@dataclass
class TrendEVPeakTracker:
    """旧 v1 EV 峰值回落追踪 (保留向后兼容, v2 决策不再使用)。"""
    pullback_ratio: float = 0.05
    peak_ev: float | None = None
    last_ev: float | None = None

    def update(self, ev: float) -> bool:
        if self.peak_ev is None or ev > self.peak_ev:
            self.peak_ev = ev
        pullback = False
        if self.peak_ev is not None and self.peak_ev > 0:
            pullback = ev <= self.peak_ev * (1.0 - self.pullback_ratio)
        self.last_ev = ev
        return pullback
