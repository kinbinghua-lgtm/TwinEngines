"""
Fixed-baseline binary sequence utilities.

每个5分钟窗口取第1分钟开盘价为固定基准，
此后每分钟（含第1分钟自身）收盘价 vs 基准 -> 1/0。
窗口完整长度为5位；触发判定取前3位。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal

TriggerPattern = Literal["111", "000"]
TARGET_TRIGGERS: frozenset[str] = frozenset({"111", "000"})


def build_fixed_baseline_sequence(
    first_minute_open: float,
    minute_closes: Sequence[float],
) -> str:
    """
    根据基准价与每分钟收盘价构建二进制序列（长度可为 1~5）。

    规则:
        close > baseline  -> "1"
        close <= baseline -> "0"
    """
    if not minute_closes:
        raise ValueError("minute_closes must not be empty")

    return "".join("1" if c > first_minute_open else "0" for c in minute_closes)


def extract_trigger(sequence: str) -> str:
    """取前3位作为触发判定形态。"""
    if len(sequence) < 3:
        raise ValueError("sequence length must be >= 3 to extract trigger")
    return sequence[:3]


def is_target_trigger(prefix: str) -> bool:
    """是否命中需求文档中"高频稳定形态"——111 或 000。"""
    return prefix in TARGET_TRIGGERS


def iter_window_triggers(
    first_minute_open: float,
    minute_closes: Iterable[float],
) -> tuple[str, bool]:
    """
    便捷封装：返回 (二进制序列, 是否命中目标触发形态)。
    """
    closes = list(minute_closes)
    seq = build_fixed_baseline_sequence(first_minute_open, closes)
    if len(seq) < 3:
        return seq, False
    return seq, is_target_trigger(extract_trigger(seq))
