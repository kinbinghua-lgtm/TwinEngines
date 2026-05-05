"""
从 StateStore SQLite 的 audit_events 统计空跑信号窗口径:

- ``shadow_window_triggered``: 出现 111/000 触发
- ``shadow_signal``: 该窗最终发射顺势/反转信号
- ``shadow_window_no_signal``: 已触发但在窗末仍无 v2 信号 (与引擎内 ``_rotate_window`` 一致)
- ``shadow_window_skipped_late_start``: 首根 1s bar 过晚, 整窗跳过

用于核对「有触发但没发射」是否偏多; ``_trend_total`` / ``_rev_total`` 仅运行时内存字段,
离线请以本模块对 ``shadow_signal`` payload 里 ``side`` 的聚合为准 (或与 jsonl 对照).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


_SHADOW_KINDS = (
    "shadow_window_triggered",
    "shadow_window_no_signal",
    "shadow_signal",
    "shadow_window_skipped_late_start",
)


def counts_by_kind(db_path: str | Path) -> dict[str, int]:
    """按 kind 计数 (仅 shadow 相关 kinds)."""
    p = Path(db_path)
    if not p.is_file():
        return {k: 0 for k in _SHADOW_KINDS}
    placeholders = ",".join("?" * len(_SHADOW_KINDS))
    conn = sqlite3.connect(str(p))
    try:
        cur = conn.execute(
            f"SELECT kind, COUNT(*) FROM audit_events WHERE kind IN ({placeholders}) GROUP BY kind",
            _SHADOW_KINDS,
        )
        rows = dict(cur.fetchall())
    finally:
        conn.close()
    out = {k: int(rows.get(k, 0)) for k in _SHADOW_KINDS}
    return out


def signal_side_counts_from_audit(db_path: str | Path) -> dict[str, int]:
    """从 audit 里 shadow_signal 行的 payload JSON 解析 ``side`` → TREND / REVERSAL 计数."""
    p = Path(db_path)
    trend = rev = other = 0
    if not p.is_file():
        return {"TREND": 0, "REVERSAL": 0, "other": 0}
    conn = sqlite3.connect(str(p))
    try:
        cur = conn.execute(
            "SELECT payload FROM audit_events WHERE kind=? ORDER BY id ASC",
            ("shadow_signal",),
        )
        for (text,) in cur.fetchall():
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                other += 1
                continue
            side = str(payload.get("side") or "").upper()
            if side == "TREND":
                trend += 1
            elif side == "REVERSAL":
                rev += 1
            else:
                other += 1
    finally:
        conn.close()
    return {"TREND": trend, "REVERSAL": rev, "other": other}


def build_shadow_window_report(db_path: str | Path) -> dict[str, Any]:
    """
    汇总「触发 vs 发射 vs 触发但未发射」及一致性提示.

    恒等式 (进程完整跑过若干窗且每次轮转都落库时):
        triggered ≈ signals_emitted + triggered_no_signal
    若有触发尚未轮转 (例如进程被 kill), 可能缺少对应的 ``shadow_window_no_signal``.
    """
    p = Path(db_path)
    counts = counts_by_kind(p)
    triggered = counts["shadow_window_triggered"]
    no_signal = counts["shadow_window_no_signal"]
    emitted = counts["shadow_signal"]
    skipped = counts["shadow_window_skipped_late_start"]
    sides = signal_side_counts_from_audit(p)

    closed = emitted + no_signal
    gap = triggered - closed
    balance_ok = gap == 0

    notes: list[str] = []
    if triggered > 0 and not balance_ok:
        if gap > 0:
            notes.append(
                f"触发比 (发射+窗末无信号) 多 {gap} 条: 常见于进程在窗口中途退出, "
                "未执行到下一窗的 ``shadow_window_no_signal`` 轮转."
            )
        else:
            notes.append(
                f"(发射+窗末无信号) 比触发多 {-gap} 条: 异常, 请检查是否混入了多段运行或重复审计."
            )

    ratio_no_signal = (no_signal / triggered) if triggered else 0.0

    return {
        "db_path": str(p.resolve()) if p.is_file() else str(p),
        "db_exists": p.is_file(),
        "counts_by_kind": counts,
        "derived": {
            "windows_triggered_111_000": triggered,
            "windows_signal_emitted": emitted,
            "windows_triggered_but_no_signal": no_signal,
            "windows_skipped_late_start": skipped,
            "closure_sum_emitted_plus_no_signal": closed,
            "closure_gap_triggered_minus_closed": gap,
            "closure_balance_exact": balance_ok,
            "share_triggered_with_no_signal": round(ratio_no_signal, 4),
        },
        "signal_side_from_shadow_signal_audit": sides,
        "notes": notes,
        "in_memory_counters_hint": (
            "运行时 _trend_total / _rev_total 仅在 LiveRunner 内存与 /metrics 中; "
            "离线核对请以 shadow_signal 审计或 logs/shadow_signals.jsonl 为准."
        ),
    }
