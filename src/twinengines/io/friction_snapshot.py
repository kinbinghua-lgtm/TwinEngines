from __future__ import annotations

import json
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any

from ..backtest.polymarket import PolymarketCfg
from .state_store import StateStore

FRICTION_KEYS = [
    "top_of_book_slippage_bps",
    "depth_slippage_bps_per_unit",
    "depth_fill_ratio",
    "rebound_base_per_sec_squared",
    "oracle_basis_drag_normal",
    "oracle_basis_drag_extreme",
]


def _now_ms() -> int:
    return int(time.time() * 1000)


def default_friction_params() -> dict[str, float]:
    cfg = PolymarketCfg()
    return {k: float(getattr(cfg, k)) for k in FRICTION_KEYS}


def ensure_snapshot(path: str) -> dict[str, Any]:
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    p.parent.mkdir(parents=True, exist_ok=True)
    now = _now_ms()
    snap = {
        "version": 1,
        "updated_ts_ms": now,
        "params": default_friction_params(),
        "param_last_updated_ts_ms": {k: now for k in FRICTION_KEYS},
        "source": "defaults_from_polymarket_cfg",
    }
    p.write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")
    return snap


def _quantile(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    idx = int((len(s) - 1) * q)
    return float(s[idx])


def _read_shadow_rows(shadow_signals_path: str, since_ms: int) -> list[dict[str, Any]]:
    p = Path(shadow_signals_path)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if int(d.get("ts_ms") or 0) < since_ms:
            continue
        rows.append(d)
    return rows


def _read_order_rows(state_db_path: str, since_ms: int) -> list[dict[str, Any]]:
    p = Path(state_db_path)
    if not p.exists():
        return []
    conn = sqlite3.connect(str(p))
    try:
        cur = conn.execute(
            "SELECT ts_ms, kind, payload FROM audit_events "
            "WHERE ts_ms>=? AND kind IN (?,?)",
            (since_ms, "order_filled", "shadow_order"),
        )
        out: list[dict[str, Any]] = []
        for ts_ms, kind, payload in cur.fetchall():
            try:
                d = json.loads(payload)
            except Exception:
                d = {}
            out.append({"ts_ms": int(ts_ms), "kind": str(kind), "payload": d})
        return out
    finally:
        conn.close()


def build_snapshot_proposal(
    *,
    state_db_path: str,
    shadow_signals_path: str,
    snapshot_path: str,
    proposal_path: str,
    window_days: int = 30,
    max_change_ratio: float = 0.30,
    min_update_interval_days: int = 30,
) -> dict[str, Any]:
    now = _now_ms()
    since_ms = now - int(window_days * 86400 * 1000)
    snap = ensure_snapshot(snapshot_path)
    current = snap.get("params") or default_friction_params()
    last_updates = snap.get("param_last_updated_ts_ms") or {}

    shadow_rows = _read_shadow_rows(shadow_signals_path, since_ms)
    order_rows = _read_order_rows(state_db_path, since_ms)

    slippage_bps: list[float] = []
    ask_sizes: list[float] = []
    implicit_fee_rates: list[float] = []
    likely_exec_flags: list[int] = []
    for e in shadow_rows:
        poly = e.get("polymarket")
        if not isinstance(poly, dict):
            continue
        quote = poly.get("trade_quote") if isinstance(poly.get("trade_quote"), dict) else {}
        book = poly.get("book") if isinstance(poly.get("book"), dict) else {}
        opp = poly.get("opportunity") if isinstance(poly.get("opportunity"), dict) else {}
        eff = quote.get("effective_price")
        raw = quote.get("raw_price")
        ask = book.get("best_ask")
        ask_size = book.get("best_ask_size")
        if isinstance(ask_size, (int, float)) and ask_size > 0:
            ask_sizes.append(float(ask_size))
        if isinstance(opp.get("likely_executable"), bool):
            likely_exec_flags.append(1 if opp.get("likely_executable") else 0)
        if isinstance(raw, (int, float)) and raw > 0 and isinstance(eff, (int, float)):
            implicit_fee_rates.append(max(0.0, (float(eff) - float(raw)) / float(raw)))
        if isinstance(ask, (int, float)) and ask > 0 and isinstance(eff, (int, float)) and eff > 0:
            slippage_bps.append((float(ask) / float(eff) - 1.0) * 10000.0)

    n_orders = len(order_rows)
    latency_ms: list[float] = []
    for r in order_rows:
        pld = r["payload"]
        if isinstance(pld.get("submitted_at_ms"), (int, float)) and isinstance(pld.get("filled_at_ms"), (int, float)):
            latency_ms.append(float(pld["filled_at_ms"]) - float(pld["submitted_at_ms"]))

    suggested = dict(current)
    p50_slip = _quantile([x for x in slippage_bps if x > 0], 0.5)
    p90_slip = _quantile([x for x in slippage_bps if x > 0], 0.9)
    p50_depth = _quantile(ask_sizes, 0.5)
    p10_depth = _quantile(ask_sizes, 0.1)
    p50_fee = _quantile(implicit_fee_rates, 0.5)
    exec_rate = (sum(likely_exec_flags) / len(likely_exec_flags)) if likely_exec_flags else None

    if p50_slip is not None:
        suggested["top_of_book_slippage_bps"] = max(1.0, p50_slip)
    if p90_slip is not None and suggested["top_of_book_slippage_bps"] > 0:
        extra = max(0.0, p90_slip - suggested["top_of_book_slippage_bps"])
        suggested["depth_slippage_bps_per_unit"] = max(1.0, extra / 5.0)
    if p10_depth is not None and p50_depth is not None and p50_depth > 0:
        ratio = max(0.3, min(0.99, p10_depth / p50_depth))
        suggested["depth_fill_ratio"] = ratio
    if p50_fee is not None:
        # 用保守映射更新 oracle drag，避免过拟合
        suggested["oracle_basis_drag_normal"] = max(current["oracle_basis_drag_normal"], min(0.01, p50_fee))
        suggested["oracle_basis_drag_extreme"] = max(current["oracle_basis_drag_extreme"], min(0.03, p50_fee * 4))

    proposals: list[dict[str, Any]] = []
    for k in FRICTION_KEYS:
        cur = float(current[k])
        raw_new = float(suggested[k])
        lo = cur * (1.0 - max_change_ratio)
        hi = cur * (1.0 + max_change_ratio)
        capped = min(max(raw_new, lo), hi)
        capped_flag = abs(raw_new - capped) > 1e-12
        last_ts = int(last_updates.get(k) or 0)
        days_since = ((now - last_ts) / 1000 / 86400) if last_ts > 0 else 9999.0
        interval_ok = days_since >= float(min_update_interval_days)
        requires_manual = bool(capped_flag or (not interval_ok))
        proposals.append({
            "param": k,
            "current": cur,
            "suggested_raw": raw_new,
            "suggested_capped": capped,
            "max_change_ratio": max_change_ratio,
            "change_ratio": ((raw_new - cur) / cur) if cur != 0 else None,
            "capped_by_safety": capped_flag,
            "days_since_last_update": round(days_since, 2),
            "min_interval_days": min_update_interval_days,
            "interval_ok": interval_ok,
            "requires_manual_intervention": requires_manual,
            "reason": (
                "exceeds_single_step_limit"
                if capped_flag else ("update_interval_too_short" if not interval_ok else "ok")
            ),
        })

    sample_count = len(shadow_rows)
    consistency_ok = sample_count >= 30 and (statistics.pstdev(slippage_bps) < 100.0 if slippage_bps else False)
    checks = {
        "sample_count": sample_count,
        "order_count": n_orders,
        "sample_count_ok": sample_count >= 30,
        "consistency_ok": consistency_ok,
    }
    out = {
        "generated_ts_ms": now,
        "window_days": window_days,
        "inputs": {
            "state_db_path": state_db_path,
            "shadow_signals_path": shadow_signals_path,
            "snapshot_path": snapshot_path,
        },
        "current_params": current,
        "stats": {
            "slippage_bps_p50": p50_slip,
            "slippage_bps_p90": p90_slip,
            "depth_best_ask_size_p50": p50_depth,
            "depth_best_ask_size_p10": p10_depth,
            "implicit_fee_rate_p50": p50_fee,
            "likely_executable_rate": exec_rate,
            "latency_ms_p50": _quantile(latency_ms, 0.5),
        },
        "checks": checks,
        "proposals": proposals,
    }
    p = Path(proposal_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def apply_snapshot_proposal(
    *,
    proposal_path: str,
    snapshot_path: str,
    state_db_path: str,
    operator: str = "manual",
    force_manual: bool = False,
) -> dict[str, Any]:
    proposal = json.loads(Path(proposal_path).read_text(encoding="utf-8"))
    checks = proposal.get("checks") or {}
    if not checks.get("sample_count_ok"):
        raise ValueError("sample_count check failed")
    if not checks.get("consistency_ok"):
        raise ValueError("consistency check failed")

    snap = ensure_snapshot(snapshot_path)
    params = dict(snap.get("params") or default_friction_params())
    last = dict(snap.get("param_last_updated_ts_ms") or {})
    now = _now_ms()
    applied: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for pr in proposal.get("proposals") or []:
        if pr.get("requires_manual_intervention") and not force_manual:
            blocked.append(pr)
            continue
        k = str(pr["param"])
        new_v = float(pr["suggested_capped"])
        old_v = float(params.get(k, 0.0))
        params[k] = new_v
        last[k] = now
        applied.append({"param": k, "old": old_v, "new": new_v, "reason": pr.get("reason")})

    snap["params"] = params
    snap["param_last_updated_ts_ms"] = last
    snap["updated_ts_ms"] = now
    snap["last_proposal_applied_ts_ms"] = now
    Path(snapshot_path).write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")

    store = StateStore(db_path=state_db_path)
    for a in applied:
        store.append_friction_param_audit(
            operator=operator,
            param=str(a["param"]),
            old_value=float(a["old"]),
            new_value=float(a["new"]),
            reason=str(a.get("reason") or "manual_apply"),
            proposal_path=proposal_path,
        )
    store.append_audit(
        "friction_snapshot_apply",
        {
            "ts_ms": now,
            "operator": operator,
            "proposal_path": proposal_path,
            "snapshot_path": snapshot_path,
            "applied": applied,
            "blocked": [{"param": b.get("param"), "reason": b.get("reason")} for b in blocked],
            "force_manual": bool(force_manual),
        },
    )
    return {
        "ok": True,
        "applied_n": len(applied),
        "blocked_n": len(blocked),
        "applied": applied,
        "blocked": blocked,
        "snapshot_path": snapshot_path,
    }
