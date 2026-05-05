"""
影子单阶段离线报告: 从 alerts.jsonl + state.sqlite 提取指标, 生成结构化 JSON / Markdown.

不依赖网络; 全部基于日志与审计表 (数据说话).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..io.config import POLYMARKET_PLATFORM
from ..risk.regime import GROWTH_PRESET, MATURE_PRESET, SEED_PRESET, ULTRA_PRESET


# 回测基线 (48 天): 顺势约 901 笔、反转约 51 笔
DEFAULT_BASELINE_TREND_PER_DAY = 901.0 / 48.0
DEFAULT_BASELINE_REVERSAL_PER_DAY = 51.0 / 48.0
DEFAULT_DEVIATION_WARN = 0.30  # ±30%


_PRESETS = {
    "ULTRA": ULTRA_PRESET,
    "SEED": SEED_PRESET,
    "GROWTH": GROWTH_PRESET,
    "MATURE": MATURE_PRESET,
}


def _max_stake_ratio(regime_name: Optional[str], side: str) -> float:
    name = (regime_name or "SEED").upper()
    p = _PRESETS.get(name, SEED_PRESET)
    if (side or "").upper() == "TREND":
        return float(p.trend_sizing.max_stake_ratio)
    return float(p.reversal_sizing.max_stake_ratio)


def _equity_floor(regime_name: Optional[str]) -> float:
    name = (regime_name or "SEED").upper()
    p = _PRESETS.get(name, SEED_PRESET)
    return float(p.risk_cfg.equity_floor)


def load_alerts_jsonl(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        return []
    out: list[dict[str, Any]] = []
    with p.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def load_shadow_orders_from_sqlite(db_path: str | Path) -> list[dict[str, Any]]:
    p = Path(db_path)
    if not p.is_file():
        return []
    conn = sqlite3.connect(str(p))
    try:
        cur = conn.execute(
            "SELECT ts_ms, kind, payload FROM audit_events "
            "WHERE kind IN (?, ?, ?) ORDER BY id ASC",
            ("shadow_order", "order_filled", "shadow_signal"),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for ts_ms, kind, text in rows:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {"_raw": text}
        out.append({"ts_ms": int(ts_ms), "kind": str(kind), "payload": payload})
    return out


def _classify_alert_kind(kind: str) -> str:
    k = kind or ""
    if k == "feed_ws_close":
        return "binance_ws_disconnect"
    if k in ("feed_ws_open", "feed_ws_connecting"):
        return "binance_ws_connect"
    if k == "feed_rest_fallback_active":
        return "binance_rest_fallback"
    if k == "poly_feed_ws_close":
        return "polymarket_ws_disconnect"
    if k in ("poly_feed_ws_open", "poly_feed_ws_connecting"):
        return "polymarket_ws_connect"
    if k == "clock_drift":
        return "clock_drift"
    if k.startswith("reconcile_") and "circuit" not in k:
        return "reconcile_other"
    if k == "reconcile_circuit_trip":
        return "circuit_breaker"
    if "circuit" in k.lower() or k == "reconcile_circuit_trip":
        return "circuit_breaker"
    return "other"


def analyze_ws_gaps(events: list[dict[str, Any]], close_kind: str, open_kind: str) -> dict[str, Any]:
    """计算 WS close -> 下一次 open 的间隔 (毫秒), 找出超过阈值的致命段."""
    ts_sorted = sorted(events, key=lambda e: int(e.get("ts_ms", 0)))
    gaps_ms: list[int] = []
    fatal_over_ms = 300_000  # 5 分钟
    fatals: list[dict[str, Any]] = []
    pending_close: Optional[int] = None

    for ev in ts_sorted:
        kind = str(ev.get("kind", ""))
        ts = int(ev.get("ts_ms", 0))
        if kind == close_kind:
            pending_close = ts
        elif kind == open_kind and pending_close is not None:
            gap = ts - pending_close
            if gap >= 0:
                gaps_ms.append(gap)
                if gap >= fatal_over_ms:
                    fatals.append({"gap_sec": round(gap / 1000.0, 1), "from_ts_ms": pending_close, "to_ts_ms": ts})
            pending_close = None

    max_gap = max(gaps_ms) if gaps_ms else 0
    return {
        "close_open_pairs": len(gaps_ms),
        "max_gap_sec": round(max_gap / 1000.0, 2),
        "fatal_gaps_over_5min": fatals,
        "fatal_gap_count": len(fatals),
    }


def analyze_clock_drift(events: list[dict[str, Any]]) -> dict[str, Any]:
    offsets: list[float] = []
    for ev in events:
        if str(ev.get("kind")) != "clock_drift":
            continue
        pl = ev.get("payload") or {}
        try:
            offsets.append(abs(float(pl.get("offset_ms", 0))))
        except (TypeError, ValueError):
            pass
    return {
        "count": len(offsets),
        "max_abs_offset_ms": round(max(offsets), 2) if offsets else 0.0,
    }


def count_alerts_by_bucket(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[int]] = {}
    for ev in events:
        raw_kind = str(ev.get("kind", ""))
        b = _classify_alert_kind(raw_kind)
        ts = int(ev.get("ts_ms", 0))
        buckets.setdefault(b, []).append(ts)

    out: dict[str, dict[str, Any]] = {}
    for b, tss in buckets.items():
        tss.sort()
        out[b] = {
            "count": len(tss),
            "first_ts_ms": tss[0] if tss else None,
            "last_ts_ms": tss[-1] if tss else None,
        }
    return out


def validate_shadow_orders(
    orders: list[dict[str, Any]],
    *,
    buy_floor: float = POLYMARKET_PLATFORM.buy_price_floor,
) -> dict[str, Any]:
    """逐笔校验影子单 / 成交审计."""
    violations: list[dict[str, Any]] = []
    by_window: dict[str, list[dict[str, Any]]] = {}

    for row in orders:
        if row.get("kind") != "shadow_order":
            continue
        pl = row.get("payload") or {}
        wid = str(pl.get("window_id", ""))
        if not wid:
            violations.append({"rule": "missing_window_id", "payload": pl})
            continue
        by_window.setdefault(wid, []).append(row)

    for wid, rows in by_window.items():
        if len(rows) > 1:
            violations.append({
                "rule": "multiple_orders_per_window",
                "severity": "fatal",
                "window_id": wid,
                "count": len(rows),
            })

    for row in orders:
        if row.get("kind") != "shadow_order":
            continue
        pl = row.get("payload") or {}
        price = pl.get("price")
        size = pl.get("size_usdc")
        side = str(pl.get("side", ""))
        try:
            pf = float(price)
        except (TypeError, ValueError):
            violations.append({"rule": "invalid_price", "severity": "fatal", "payload": pl})
            continue
        if pf < buy_floor:
            violations.append({
                "rule": "buy_below_floor",
                "severity": "fatal",
                "price": pf,
                "floor": buy_floor,
                "window_id": pl.get("window_id"),
            })

        eq = pl.get("equity_usdc")
        regime = pl.get("regime_name")
        if eq is not None and size is not None:
            try:
                eqf = float(eq)
                szf = float(size)
            except (TypeError, ValueError):
                continue
            floor_e = _equity_floor(regime if isinstance(regime, str) else None)
            if eqf <= floor_e:
                violations.append({
                    "rule": "order_with_equity_at_or_below_floor",
                    "severity": "fatal",
                    "equity_usdc": eqf,
                    "equity_floor": floor_e,
                    "window_id": pl.get("window_id"),
                })
            cap = _max_stake_ratio(regime if isinstance(regime, str) else None, side)
            if eqf > 0 and szf > eqf * cap * (1.0 + 1e-6):
                violations.append({
                    "rule": "stake_exceeds_max_stake_ratio",
                    "severity": "fatal",
                    "size_usdc": szf,
                    "equity_usdc": eqf,
                    "max_ratio": cap,
                    "implied_ratio": round(szf / eqf, 6),
                    "side": side,
                    "window_id": pl.get("window_id"),
                })

    fatal = [v for v in violations if v.get("severity") == "fatal"]
    return {
        "shadow_order_count": sum(1 for r in orders if r.get("kind") == "shadow_order"),
        "violations": violations,
        "fatal_violation_count": len(fatal),
        "pass": len(fatal) == 0,
    }


def compute_health_score(
    *,
    binance_gap_fatal: int,
    poly_gap_fatal: int,
    clock_max_ms: float,
    error_like_count: int,
    warn_bucket_total: int,
    rest_fallback_count: int,
    shadow_pass: bool,
    hours: float,
) -> tuple[float, dict[str, float]]:
    """0-100 综合分, 返回 (score, factors)."""
    factors: dict[str, float] = {}
    score = 100.0

    if binance_gap_fatal + poly_gap_fatal > 0:
        d = min(50.0, 25.0 * (binance_gap_fatal + poly_gap_fatal))
        factors["ws_fatal_gaps_penalty"] = d
        score -= d

    if clock_max_ms > 1500:
        d = min(25.0, (clock_max_ms - 1500.0) / 200.0)
        factors["clock_drift_penalty"] = d
        score -= d

    if hours > 0:
        rf_per_h = rest_fallback_count / hours
        d = min(15.0, max(0.0, (rf_per_h - 30.0) * 0.3))
        factors["rest_fallback_rate_penalty"] = d
        score -= d

    d = min(30.0, error_like_count * 8.0)
    factors["error_level_penalty"] = d
    score -= d

    d = min(15.0, warn_bucket_total * 0.15)
    factors["warn_volume_penalty"] = d
    score -= d

    if not shadow_pass:
        factors["shadow_rule_penalty"] = 30.0
        score -= 30.0

    score = max(0.0, min(100.0, score))
    factors["final"] = score
    return score, factors


def build_report(
    *,
    alerts_path: str | Path,
    state_db_path: str | Path,
    window_hours: float = 24.0,
    baseline_trend_per_day: float = DEFAULT_BASELINE_TREND_PER_DAY,
    baseline_reversal_per_day: float = DEFAULT_BASELINE_REVERSAL_PER_DAY,
    deviation_warn: float = DEFAULT_DEVIATION_WARN,
) -> dict[str, Any]:
    events = load_alerts_jsonl(alerts_path)
    orders = load_shadow_orders_from_sqlite(state_db_path)

    hours = max(window_hours, 1e-6)
    windows_total = int(round(hours * 12.0))  # 每 5 分钟一窗

    def _is_signal_or_order(r: dict[str, Any]) -> bool:
        return r.get("kind") in ("shadow_order", "shadow_signal")

    trend_n = sum(
        1 for r in orders
        if _is_signal_or_order(r)
        and str((r.get("payload") or {}).get("side", "")).upper() == "TREND"
    )
    rev_n = sum(
        1 for r in orders
        if _is_signal_or_order(r)
        and str((r.get("payload") or {}).get("side", "")).upper() == "REVERSAL"
    )

    exp_trend = baseline_trend_per_day * (hours / 24.0)
    exp_rev = baseline_reversal_per_day * (hours / 24.0)

    def _dev(actual: float, expected: float) -> Optional[str]:
        if expected <= 0:
            return None
        ratio = abs(actual - expected) / expected
        if ratio > deviation_warn:
            return f"偏差 {ratio:.1%} 超过阈值 ±{deviation_warn:.0%}"
        return None

    freq_report = {
        "window_hours": hours,
        "windows_total_estimate_5min": windows_total,
        "shadow_trend_count": trend_n,
        "shadow_reversal_count": rev_n,
        "baseline_trend_per_day": round(baseline_trend_per_day, 3),
        "baseline_reversal_per_day": round(baseline_reversal_per_day, 3),
        "expected_trend_in_window": round(exp_trend, 2),
        "expected_reversal_in_window": round(exp_rev, 2),
        "trend_deviation_flag": _dev(float(trend_n), exp_trend),
        "reversal_deviation_flag": _dev(float(rev_n), exp_rev),
    }

    buckets = count_alerts_by_bucket(events)
    binance_ws = analyze_ws_gaps(events, "feed_ws_close", "feed_ws_open")
    poly_ws = analyze_ws_gaps(events, "poly_feed_ws_close", "poly_feed_ws_open")
    clock_info = analyze_clock_drift(events)

    error_like = sum(
        1 for e in events
        if str(e.get("level", "")).lower() in ("error", "fatal", "critical")
    )
    warn_total = sum(v["count"] for k, v in buckets.items() if k != "other")

    val = validate_shadow_orders(orders)
    score, factors = compute_health_score(
        binance_gap_fatal=binance_ws["fatal_gap_count"],
        poly_gap_fatal=poly_ws["fatal_gap_count"],
        clock_max_ms=float(clock_info["max_abs_offset_ms"]),
        error_like_count=error_like,
        warn_bucket_total=warn_total,
        rest_fallback_count=int(buckets.get("binance_rest_fallback", {}).get("count", 0)),
        shadow_pass=bool(val["pass"]),
        hours=hours,
    )

    suspend_live = score < 80.0 or val["fatal_violation_count"] > 0
    suspend_reasons: list[str] = []
    if score < 80.0:
        suspend_reasons.append(f"健康分 {score:.1f} < 80")
    if val["fatal_violation_count"] > 0:
        suspend_reasons.append("影子单规则存在致命违反")

    return {
        "meta": {
            "alerts_path": str(alerts_path),
            "state_db_path": str(state_db_path),
        },
        "1_signal_frequency_vs_backtest": freq_report,
        "2_alert_buckets": buckets,
        "2_ws_gap_analysis": {
            "binance": binance_ws,
            "polymarket": poly_ws,
        },
        "2_clock_drift": clock_info,
        "3_shadow_order_validation": val,
        "4_health_score": {
            "score": round(score, 2),
            "factors": {k: round(v, 3) for k, v in factors.items()},
            "suspend_live_plan": suspend_live,
            "suspend_reasons": suspend_reasons,
        },
    }


def report_to_markdown(rep: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# 影子单阶段分析报告\n")
    f1 = rep["1_signal_frequency_vs_backtest"]
    lines.append("## 1. 信号触发频率 vs 回测基线\n")
    lines.append(f"- 分析窗长: **{f1['window_hours']}** 小时 (估计 5min 窗数 **{f1['windows_total_estimate_5min']}**)\n")
    lines.append(f"- 回测基线: 顺势约 **{f1['baseline_trend_per_day']:.2f}** 笔/天, 反转约 **{f1['baseline_reversal_per_day']:.2f}** 笔/天\n")
    lines.append(f"- 影子单顺势: **{f1['shadow_trend_count']}** 次 (本窗期望约 {f1['expected_trend_in_window']:.1f})\n")
    lines.append(f"- 影子单反转: **{f1['shadow_reversal_count']}** 次 (本窗期望约 {f1['expected_reversal_in_window']:.1f})\n")
    if f1["trend_deviation_flag"]:
        lines.append(f"- ⚠️ 顺势: **异常** — {f1['trend_deviation_flag']}\n")
    else:
        lines.append("- 顺势: 偏差在 ±30% 内\n")
    if f1["reversal_deviation_flag"]:
        lines.append(f"- ⚠️ 反转: **异常** — {f1['reversal_deviation_flag']}\n")
    else:
        lines.append("- 反转: 偏差在 ±30% 内\n")

    lines.append("\n## 2. 告警分类统计\n")
    for name, info in sorted(rep["2_alert_buckets"].items()):
        lines.append(f"- **{name}**: count={info['count']}, first_ts_ms={info['first_ts_ms']}, last_ts_ms={info['last_ts_ms']}\n")
    ws = rep["2_ws_gap_analysis"]
    lines.append(f"\n### WS 断连最长间隔 (close→open)\n")
    lines.append(f"- Binance: max **{ws['binance']['max_gap_sec']}** s, 超 5min 段数 **{ws['binance']['fatal_gap_count']}**\n")
    lines.append(f"- Polymarket: max **{ws['polymarket']['max_gap_sec']}** s, 超 5min 段数 **{ws['polymarket']['fatal_gap_count']}**\n")
    cd = rep["2_clock_drift"]
    lines.append(f"\n### 时钟漂移告警\n- 次数 **{cd['count']}**, 最大 |offset| **{cd['max_abs_offset_ms']}** ms\n")

    v = rep["3_shadow_order_validation"]
    lines.append("\n## 3. 影子单规则校验\n")
    lines.append(f"- 影子单笔数: **{v['shadow_order_count']}**\n")
    lines.append(f"- 致命违反数: **{v['fatal_violation_count']}**\n")
    if v["violations"]:
        lines.append("\n明细:\n")
        for item in v["violations"][:50]:
            lines.append(f"```json\n{json.dumps(item, ensure_ascii=False)}\n```\n")
    else:
        lines.append("- 无违反项\n")

    h = rep["4_health_score"]
    lines.append("\n## 4. 系统健康度综合评分\n")
    lines.append(f"- **得分: {h['score']}/100**\n")
    lines.append(f"- 是否建议暂停实盘: **{'是' if h['suspend_live_plan'] else '否'}**\n")
    if h["suspend_reasons"]:
        lines.append(f"- 原因: {', '.join(h['suspend_reasons'])}\n")
    lines.append("\n### 因子分解\n")
    for k, vv in h["factors"].items():
        lines.append(f"- {k}: {vv}\n")

    return "".join(lines)


_LOG_ALERT_RE = re.compile(
    r"ALERT\s+\[(?P<level>\w+)\]\s+kind=(?P<kind>\S+)\s+payload=(?P<payload>\{.*\})\s*$",
)

def ingest_alerts_from_log_text(log_text: str) -> list[dict[str, Any]]:
    """从 twinengines.log 文本中解析 ALERT 行 (jsonl 缺失时兜底)."""
    out: list[dict[str, Any]] = []
    for line in log_text.splitlines():
        m = _LOG_ALERT_RE.search(line)
        if not m:
            continue
        try:
            payload = json.loads(m.group("payload"))
        except json.JSONDecodeError:
            payload = {"_raw": m.group("payload")}
        out.append({
            "ts_ms": 0,
            "level": m.group("level").lower(),
            "kind": m.group("kind"),
            "payload": payload,
        })
    return out
