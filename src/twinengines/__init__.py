"""TwinEngines - 五分钟固定基准序列双引擎择时策略。"""

from .sequence import (
    TARGET_TRIGGERS,
    build_fixed_baseline_sequence,
    extract_trigger,
    is_target_trigger,
    iter_window_triggers,
)
from .signals import (
    Signal,
    SignalThresholds,
    TrendEVPeakTracker,
    TrendStabilityTracker,
    decide_signal,
    expected_value_reversal,
    expected_value_trend,
)
from .survival import (
    SurvivalParams,
    cumulative_hazard,
    reversal_probability,
    reversal_probability_vec,
)

__all__ = [
    "TARGET_TRIGGERS",
    "Signal",
    "SignalThresholds",
    "SurvivalParams",
    "TrendEVPeakTracker",
    "TrendStabilityTracker",
    "build_fixed_baseline_sequence",
    "cumulative_hazard",
    "decide_signal",
    "expected_value_reversal",
    "expected_value_trend",
    "extract_trigger",
    "is_target_trigger",
    "iter_window_triggers",
    "reversal_probability",
    "reversal_probability_vec",
]
