"""
WebUI 只读数据访问层 (read-only data adapter).

设计原则 (面板独立性硬约束):
    1. 严禁 import 任何策略代码 (signals/survival/risk/sizing/backtest)
    2. 所有数据来自 LiveRunner 产物的"文件"接口:
        - SQLite        : data_runtime/state.sqlite (audit_events / kv_state)
        - 告警 JSONL     : logs/alerts.jsonl
        - 主日志        : logs/twinengines.log
        - 影子信号 JSONL : logs/shadow_signals.jsonl
    3. 任何文件缺失都返回空, 不抛异常 (面板不能因为文件不存在崩)
    4. 全部按需读取 (无后台线程), 简化进程模型

模块化:
    EquityHistory   - 资金曲线 (audit_events 中 order_filled / kv_state.last_equity)
    SignalCounts    - 当日 / 24h 信号笔数 (顺势 / 反转), 与回测基线比对
    AlertsTail      - 告警最近 N 条
    LogsTail        - 主日志最近 N 行, 支持 ERROR/WARNING 筛选
    RegimeInfo      - 当前阶段 / 当前资金 (kv_state)
    ShadowSignals   - shadow_signals.jsonl 最近 N 条
    LiveFilledOutcomeBoard - SQLite order_filled + shadow jsonl 上下文 → 与影子面板同口径结算对账
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

# 最新纯反转回测基线（30d fresh, p_rev_min=0.215）
BASELINE_REVERSAL_PER_DAY = 231.0 / 30.0   # ≈ 7.70


# ============================================================
# 资金曲线
# ============================================================

@dataclass
class EquityPoint:
    ts_ms: int
    equity_usdc: float
    source: str   # "order_filled" | "kv_state.last_equity" | "audit"


@dataclass
class EquityHistory:
    state_db_path: str

    def recent(self, hours: float = 24.0) -> list[EquityPoint]:
        if not Path(self.state_db_path).is_file():
            return []
        since_ms = int((time.time() - hours * 3600.0) * 1000.0)
        out: list[EquityPoint] = []
        try:
            conn = sqlite3.connect(self.state_db_path, timeout=3.0)
            try:
                cur = conn.execute(
                    "SELECT ts_ms, kind, payload FROM audit_events "
                    "WHERE ts_ms>=? AND kind IN (?,?,?,?,?) ORDER BY id ASC",
                    (since_ms,
                     "order_filled", "shadow_order", "shadow_signal",
                     "reconcile_equity_snapshot", "equity_snapshot"),
                )
                for ts_ms, kind, text in cur.fetchall():
                    try:
                        payload = json.loads(text)
                    except Exception:
                        continue
                    eq = self._extract_equity(payload)
                    if eq is None:
                        continue
                    out.append(EquityPoint(
                        ts_ms=int(ts_ms),
                        equity_usdc=float(eq),
                        source=str(kind),
                    ))
            finally:
                conn.close()
        except Exception:
            return out
        return out

    @staticmethod
    def _extract_equity(payload: dict) -> Optional[float]:
        for key in ("equity_usdc", "equity", "account_equity_usdc"):
            v = payload.get(key)
            if isinstance(v, (int, float)) and v >= 0:
                return float(v)
        return None


# ============================================================
# 当日信号统计
# ============================================================

@dataclass
class SignalCounts:
    state_db_path: str

    def today(self, hours: float = 24.0) -> dict[str, Any]:
        zeros = {
            "reversal_n": 0,
            "reversal_baseline_per_day": BASELINE_REVERSAL_PER_DAY,
            "reversal_baseline_in_window": BASELINE_REVERSAL_PER_DAY * (hours / 24.0),
            "reversal_deviation": None,
            "window_hours": hours,
            "source_kinds": ["shadow_signal", "shadow_order", "order_filled"],
        }
        if not Path(self.state_db_path).is_file():
            return zeros
        since_ms = int((time.time() - hours * 3600.0) * 1000.0)
        rev_n = 0
        try:
            conn = sqlite3.connect(self.state_db_path, timeout=3.0)
            try:
                cur = conn.execute(
                    "SELECT kind, payload FROM audit_events "
                    "WHERE ts_ms>=? AND kind IN (?,?,?)",
                    (since_ms, "shadow_signal", "shadow_order", "order_filled"),
                )
                for _kind, text in cur.fetchall():
                    try:
                        p = json.loads(text)
                    except Exception:
                        continue
                    side = str(p.get("side", "")).upper()
                    if side == "REVERSAL":
                        rev_n += 1
            finally:
                conn.close()
        except Exception:
            pass
        out = dict(zeros)
        out["reversal_n"] = rev_n
        if out["reversal_baseline_in_window"] > 0:
            out["reversal_deviation"] = (rev_n - out["reversal_baseline_in_window"]) / out["reversal_baseline_in_window"]
        return out


@dataclass
class LatestBacktestSnapshot:
    project_root: str

    def build(self) -> dict[str, Any]:
        root = Path(self.project_root)
        p1 = root / "reports" / "p_rev_micro_tuning_around_215_fresh30d.json"
        p2 = root / "reports" / "orig_rule_eval_30d_fresh_20260430.json"
        out: dict[str, Any] = {
            "best_params": {"p_rev_min": 0.215, "consec_min": 2},
            "metrics": {},
            "source_files": [],
        }
        if p1.is_file():
            try:
                d = json.loads(p1.read_text(encoding="utf-8"))
                best = d.get("backup") or d.get("best") or {}
                if isinstance(best, dict):
                    out["metrics"].update({
                        "win_rate": best.get("win_rate"),
                        "coverage": best.get("coverage"),
                        "signals": best.get("signals"),
                        "capture_rate": best.get("capture_rate"),
                    })
                    out["best_params"] = {
                        "p_rev_min": best.get("p_rev_min", 0.215),
                        "consec_min": 2,
                    }
                out["source_files"].append(str(p1))
            except Exception:
                pass
        if p2.is_file():
            try:
                d = json.loads(p2.read_text(encoding="utf-8"))
                m = d.get("metrics") if isinstance(d.get("metrics"), dict) else {}
                if m:
                    out["metrics"].update({
                        "win_rate": out["metrics"].get("win_rate", m.get("win_rate")),
                        "coverage": out["metrics"].get("coverage", m.get("coverage")),
                        "signals": out["metrics"].get("signals", m.get("signals")),
                        "capture_rate": out["metrics"].get("capture_rate", m.get("capture_rate")),
                    })
                out["source_files"].append(str(p2))
            except Exception:
                pass
        return out


# ============================================================
# 告警 (alerts.jsonl)
# ============================================================

@dataclass
class AlertsTail:
    path: str

    def recent(self, n: int = 50) -> list[dict]:
        if not Path(self.path).is_file():
            return []
        try:
            lines = _tail_lines(self.path, n)
        except Exception:
            return []
        out: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                out.append({"_raw": line})
        out.reverse()  # 最新在前
        return out


# ============================================================
# 影子信号 (shadow_signals.jsonl)
# ============================================================

@dataclass
class ShadowSignalsTail:
    path: str

    def recent(self, n: int = 50) -> list[dict]:
        if not Path(self.path).is_file():
            return []
        try:
            lines = _tail_lines(self.path, n)
        except Exception:
            return []
        out: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                out.append({"_raw": line})
        out.reverse()
        return out


# ============================================================
# 与 naked-third-digit-live / systemd 对齐的只读辅助（不 import 策略逻辑）
# ============================================================

KV_NAKED_FUNDS_SNAPSHOT = "naked_funds.snapshot"


def read_kv_state_json(state_db_path: str, key: str) -> Any:
    """读 kv_state 单行 JSON；文件不存在或失败返回 None。"""
    p = Path(state_db_path)
    if not p.is_file():
        return None
    try:
        conn = sqlite3.connect(str(p), timeout=2.0)
        try:
            cur = conn.execute("SELECT value FROM kv_state WHERE key=?", (key,))
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def read_strategy_exec_start_from_repo(project_root: str) -> Optional[str]:
    """从仓库内 systemd unit 解析 ExecStart=（与 VPS 部署的单元应一致）。"""
    path = Path(project_root) / "deploy" / "systemd" / "twinengines-strategy.service"
    if not path.is_file():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("ExecStart="):
                return s.split("=", 1)[1].strip()
    except Exception:
        return None
    return None


# ============================================================
# 裸策略交易日志（strategy_stdout.jsonl）
# ============================================================

@dataclass
class NakedTradeJournal:
    stdout_path: str = "logs/strategy_stdout.log"

    def recent(self, n: int = 80) -> list[dict[str, Any]]:
        if not Path(self.stdout_path).is_file():
            return []
        try:
            lines = _tail_lines(self.stdout_path, max(400, n * 6))
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            row = self._parse_line(line)
            if row is not None:
                out.append(row)
            if len(out) >= max(1, n):
                break
        return out

    def _parse_line(self, line: str) -> Optional[dict[str, Any]]:
        s = line.strip()
        if not s or not s.startswith("{"):
            return None
        try:
            e = json.loads(s)
        except Exception:
            return None
        if not isinstance(e, dict):
            return None

        phase = str(e.get("phase") or "")
        action = str(e.get("action") or "")
        if phase == "runner_started":
            return {
                "kind": "runner_started",
                "ts_ms": int(time.time() * 1000),
                "mode": "live" if bool(e.get("yes_real_money")) else "dry_run",
                "poll_sec": e.get("poll_sec"),
                "confidence_min": e.get("confidence_min"),
                "max_market_rollovers": e.get("max_market_rollovers"),
                "funds_manager": e.get("funds_manager"),
                "kelly_sizing": e.get("kelly_sizing"),
                "kelly_fraction": e.get("kelly_fraction"),
                "kelly_max_stake_ratio": e.get("kelly_max_stake_ratio"),
                "raw": e,
            }
        if phase in ("window_verdict", "market_rollover", "runner_stopped", "low_conf_tp"):
            return {
                "kind": phase,
                "ts_ms": int(time.time() * 1000),
                "condition_id": e.get("completed_condition_tail"),
                "action": action or phase,
                "result": e,
            }

        if phase != "tick":
            return None

        pred = e.get("prediction") if isinstance(e.get("prediction"), dict) else {}
        order_plan = e.get("order_plan") if isinstance(e.get("order_plan"), dict) else {}
        conf = _safe_float(pred.get("confidence_on_pick"))
        best_ask = _safe_float(order_plan.get("best_ask"))
        odds_cap = _safe_float(e.get("odds_cap_strict_below"))
        checks = [
            {
                "name": "预测已生成",
                "passed": bool(pred),
                "detail": pred.get("predict_engine") if pred else "prediction_missing",
            },
            {
                "name": "置信度阈值",
                "passed": (conf is not None and conf >= float(e.get("confidence_min", 0.0)))
                if action != "below_confidence_threshold" else False,
                "detail": {
                    "confidence": conf,
                    "required": e.get("confidence_min"),
                },
            },
            {
                "name": "赔率分档上限",
                "passed": action != "odds_above_confidence_tier_cap",
                "detail": {
                    "best_ask": best_ask,
                    "cap": odds_cap,
                },
            },
            {
                "name": "正向边际",
                "passed": action != "no_positive_edge_vs_fair",
                "detail": {
                    "ask_below_fair": e.get("ask_below_fair"),
                    "edge_vs_best_ask": e.get("edge_vs_best_ask"),
                },
            },
            {
                "name": "资金管理闸门",
                "passed": action != "funds_blocked",
                "detail": e.get("funds_block_reason"),
            },
            {
                "name": "名义 sizing",
                "passed": True,
                "detail": e.get("sizing"),
            },
            {
                "name": "触发下单",
                "passed": action in ("submitted", "would_submit_dry_run_need_yes_real_money"),
                "detail": {
                    "action": action,
                    "ticket_state": e.get("ticket_state"),
                },
            },
        ]
        return {
            "kind": "tick",
            "ts_ms": int(e.get("ts_ms") or time.time() * 1000),
            "condition_id": e.get("condition_id"),
            "action": action,
            "market_question": e.get("market_question"),
            "sizing": e.get("sizing"),
            "funds_block_reason": e.get("funds_block_reason"),
            "prediction": {
                "pred_up": pred.get("pred_up"),
                "p_up": pred.get("p_up"),
                "p_rev": pred.get("p_rev"),
                "confidence_on_pick": pred.get("confidence_on_pick"),
                "predict_engine": pred.get("predict_engine"),
                "current_prefix": pred.get("current_prefix"),
                "prefix_bucket_used": pred.get("prefix_bucket_used"),
                "prefix_bucket": pred.get("prefix_bucket"),
            } if pred else None,
            "order_plan": order_plan or None,
            "ticket_state": e.get("ticket_state"),
            "client_order_id": e.get("client_order_id"),
            "checks": checks,
            "result": e,
        }


# ============================================================
# Edge 验证报告（naked-third-digit-live stdout JSONL）
# ============================================================

@dataclass
class EdgeValidationReport:
    stdout_path: str = "logs/strategy_stdout.log"

    def build(self, *, max_lines: int = 24000) -> dict[str, Any]:
        p = Path(self.stdout_path)
        if not p.is_file():
            return {"ok": True, "source": str(p), "items": [], "strategies": {}, "notes": ["stdout_missing"]}
        try:
            lines = _tail_lines(str(p), max_lines)
        except Exception as e:
            return {"ok": False, "source": str(p), "items": [], "strategies": {}, "notes": [f"read_failed:{e}"]}

        verdicts: dict[str, dict[str, Any]] = {}
        first_by_strategy: dict[str, dict[str, dict[str, Any]]] = {
            "focus_band": {},
            "current_rules": {},
            "low_conf_shadow": {},
        }
        action_counts: dict[str, int] = {}
        pred_ticks = 0
        book_ticks = 0

        for line in lines:
            e = self._load_json(line)
            if not e:
                continue
            if e.get("kind") == "naked_live_tick" and isinstance(e.get("rep"), dict):
                e = e["rep"]
            elif e.get("kind") == "naked_live_tp" and isinstance(e.get("event"), dict):
                e = e["event"]
            phase = str(e.get("phase") or "")
            if phase == "window_verdict":
                tail = str(e.get("completed_condition_tail") or "").strip()
                if tail:
                    verdicts[tail] = e
                continue
            if phase != "tick":
                continue
            action = str(e.get("action") or "unknown")
            action_counts[action] = action_counts.get(action, 0) + 1
            pred = e.get("prediction") if isinstance(e.get("prediction"), dict) else None
            if pred is None:
                continue
            pred_ticks += 1
            cid = str(e.get("condition_id") or "").strip()
            if not cid:
                continue
            conf = _safe_float(e.get("confidence_on_pick"))
            if conf is None:
                conf = _safe_float(pred.get("confidence_on_pick"))
            ask = _safe_float(e.get("best_ask"))
            if ask is None:
                op = e.get("order_plan") if isinstance(e.get("order_plan"), dict) else {}
                ask = _safe_float(op.get("best_ask"))
            edge = _safe_float(e.get("edge_vs_best_ask"))
            fair = _safe_float(e.get("fair_prob_side"))
            pred_up_raw = pred.get("pred_up")
            if not isinstance(pred_up_raw, bool) or conf is None or ask is None or edge is None:
                continue
            book_ticks += 1
            row = {
                "ts_ms": int(e.get("ts_ms") or 0),
                "condition_id": cid,
                "condition_tail": cid[-12:],
                "market_question": e.get("market_question"),
                "action_at_tick": action,
                "pred_up": bool(pred_up_raw),
                "pred_side": "UP" if bool(pred_up_raw) else "DOWN",
                "confidence": float(conf),
                "best_ask": float(ask),
                "fair_prob_side": fair,
                "edge": float(edge),
                "window_start_ms": e.get("window_start_ms"),
                "legacy_odds_tier_would_block": bool(e.get("legacy_odds_tier_would_block")),
            }
            if self._is_focus_band(row):
                first_by_strategy["focus_band"].setdefault(cid, row)
            if self._is_current_rules(row):
                first_by_strategy["current_rules"].setdefault(cid, row)
            if self._is_low_conf_shadow(row):
                first_by_strategy["low_conf_shadow"].setdefault(cid, row)

        strategies: dict[str, Any] = {}
        items: list[dict[str, Any]] = []
        for name, by_cid in first_by_strategy.items():
            rows = []
            for cid, row in by_cid.items():
                judged = self._attach_verdict(row, verdicts.get(cid[-12:]))
                judged["strategy"] = name
                rows.append(judged)
                items.append(judged)
            rows.sort(key=lambda r: int(r.get("ts_ms") or 0), reverse=True)
            strategies[name] = self._summarize(name, rows)
            strategies[name]["recent"] = rows[:12]

        items.sort(key=lambda r: int(r.get("ts_ms") or 0), reverse=True)
        return {
            "ok": True,
            "source": str(p),
            "scanned_lines": len(lines),
            "pred_ticks": pred_ticks,
            "book_ticks": book_ticks,
            "action_counts": sorted(action_counts.items(), key=lambda x: x[1], reverse=True)[:12],
            "strategies": strategies,
            "items": items[:40],
            "notes": [
                "focus_band = conf 0.56~0.62, best_ask 0.35~0.49, edge >= 0.06; 每窗口只取第一次触发",
                "current_rules = 当前实盘规则的影子验证；用 best_ask 作为假设入场价",
                "low_conf_shadow = conf 0.52~0.56 且 ask<0.5 且 edge>=0.08，仅观察以前机会多但胜率不稳的区域",
            ],
        }

    @staticmethod
    def _load_json(line: str) -> Optional[dict[str, Any]]:
        s = line.strip()
        if not s or not s.startswith("{"):
            return None
        try:
            obj = json.loads(s)
        except Exception:
            return None
        return obj if isinstance(obj, dict) else None

    @staticmethod
    def _min_edge_for_conf(conf: float) -> Optional[float]:
        c = float(conf)
        if c >= 0.68:
            return 0.04
        if c >= 0.62:
            return 0.05
        if c >= 0.56:
            return 0.06
        return None

    def _is_current_rules(self, row: dict[str, Any]) -> bool:
        conf = float(row["confidence"])
        ask = float(row["best_ask"])
        edge = float(row["edge"])
        req = self._min_edge_for_conf(conf)
        return ask < 0.5 and req is not None and edge >= req

    @staticmethod
    def _is_focus_band(row: dict[str, Any]) -> bool:
        return (
            0.56 <= float(row["confidence"]) < 0.62
            and 0.35 <= float(row["best_ask"]) < 0.49
            and float(row["edge"]) >= 0.06
        )

    @staticmethod
    def _is_low_conf_shadow(row: dict[str, Any]) -> bool:
        return (
            0.52 <= float(row["confidence"]) < 0.56
            and float(row["best_ask"]) < 0.5
            and float(row["edge"]) >= 0.08
        )

    @staticmethod
    def _attach_verdict(row: dict[str, Any], verdict: Optional[dict[str, Any]]) -> dict[str, Any]:
        out = dict(row)
        if not verdict:
            out.update({"settled": False, "matched": None, "pnl_per_share": None, "roi_on_cost": None})
            return out
        matched_raw = verdict.get("direction_match")
        matched = bool(matched_raw) if isinstance(matched_raw, bool) else None
        entry = float(out["best_ask"])
        pnl = None if matched is None else ((1.0 - entry) if matched else -entry)
        out.update({
            "settled": matched is not None,
            "matched": matched,
            "true_up_binance": verdict.get("true_up_binance"),
            "close_min5": verdict.get("close_min5"),
            "baseline_open_m1": verdict.get("baseline_open_m1"),
            "pnl_per_share": pnl,
            "roi_on_cost": (pnl / entry) if (pnl is not None and entry > 0) else None,
        })
        return out

    @staticmethod
    def _summarize(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        settled = [r for r in rows if isinstance(r.get("matched"), bool)]
        wins = [r for r in settled if r.get("matched") is True]
        pnl_vals = [float(r["pnl_per_share"]) for r in settled if isinstance(r.get("pnl_per_share"), (int, float))]
        ask_vals = [float(r["best_ask"]) for r in rows]
        edge_vals = [float(r["edge"]) for r in rows]
        conf_vals = [float(r["confidence"]) for r in rows]
        return {
            "name": name,
            "n_candidates": len(rows),
            "n_settled": len(settled),
            "n_pending": len(rows) - len(settled),
            "wins": len(wins),
            "win_rate": (len(wins) / len(settled)) if settled else None,
            "avg_pnl_per_share": (sum(pnl_vals) / len(pnl_vals)) if pnl_vals else None,
            "sum_pnl_per_share": sum(pnl_vals) if pnl_vals else 0.0,
            "avg_entry_ask": (sum(ask_vals) / len(ask_vals)) if ask_vals else None,
            "avg_edge": (sum(edge_vals) / len(edge_vals)) if edge_vals else None,
            "avg_confidence": (sum(conf_vals) / len(conf_vals)) if conf_vals else None,
        }

@dataclass
class ShadowSignalOutcomeBoard:
    path: str
    window_sec: int = 300
    _cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    def recent(self, n: int = 50) -> list[dict[str, Any]]:
        if not Path(self.path).is_file():
            return []
        try:
            lines = _tail_lines(self.path, n)
        except Exception:
            return []
        now_ms = int(time.time() * 1000)
        out: list[dict[str, Any]] = []
        for line in reversed(lines):  # 最新在前
            row = self._parse_row(line, now_ms=now_ms)
            if row is not None:
                out.append(row)
        return out

    def summary(self, *, hours: float = 24.0, scan_n: int = 1000) -> dict[str, Any]:
        items = self.recent(n=scan_n)
        now_ms = int(time.time() * 1000)
        since_ms = now_ms - int(hours * 3600 * 1000)
        recent_items = [x for x in items if int(x.get("ts_ms") or 0) >= since_ms]

        judged = [x for x in recent_items if isinstance(x.get("matched"), bool)]
        matched = [x for x in judged if x.get("matched") is True]
        executable_judged = [x for x in recent_items if isinstance(x.get("likely_executable"), bool)]
        executable_yes = [x for x in executable_judged if x.get("likely_executable") is True]
        ev_vals = [
            float(x["theoretical_ev"]) for x in recent_items
            if isinstance(x.get("theoretical_ev"), (int, float))
        ]

        hit_rate = (len(matched) / len(judged)) if judged else None
        executable_rate = (len(executable_yes) / len(executable_judged)) if executable_judged else None
        avg_ev = (sum(ev_vals) / len(ev_vals)) if ev_vals else None
        return {
            "window_hours": hours,
            "sample_n_24h": len(recent_items),
            "hit_rate": hit_rate,
            "hit_n": len(matched),
            "hit_denominator": len(judged),
            "executable_rate": executable_rate,
            "executable_n": len(executable_yes),
            "executable_denominator": len(executable_judged),
            "avg_theoretical_ev": avg_ev,
            "avg_ev_n": len(ev_vals),
        }

    def _parse_row(self, line: str, *, now_ms: int) -> Optional[dict[str, Any]]:
        line = line.strip()
        if not line:
            return None
        try:
            e = json.loads(line)
        except Exception:
            return {"parse_error": True, "_raw": line[:240]}

        ts_ms = int(e.get("ts_ms") or 0)
        window_id = str(e.get("window_id") or "")
        baseline = _safe_float(e.get("baseline_price"))
        side = str(e.get("side") or "").upper()
        poly = e.get("polymarket") if isinstance(e.get("polymarket"), dict) else {}
        mapping = poly.get("mapping") if isinstance(poly.get("mapping"), dict) else {}
        opp = poly.get("opportunity") if isinstance(poly.get("opportunity"), dict) else {}
        quote = poly.get("trade_quote") if isinstance(poly.get("trade_quote"), dict) else {}

        predicted = str(mapping.get("polymarket_side") or quote.get("side") or "").upper()
        if predicted not in ("UP", "DOWN"):
            predicted = _infer_predicted_side(side=side, trigger=str(e.get("trigger_pattern") or ""))
        likely_exec = opp.get("likely_executable")
        executable = bool(likely_exec) if isinstance(likely_exec, bool) else None

        payoff_gross = _safe_float(quote.get("payoff_gross"))
        p_rev = _safe_float(e.get("p_rev"))
        expected_ev = _calc_expected_ev(
            side=side,
            p_reversal=p_rev,
            payoff_gross=payoff_gross,
        )
        actual_side = None
        matched = None
        actual_close = None
        baseline_cmp = baseline
        settled = False
        actual_error = None
        settlement_source = None

        wid_start = _window_start_ms(window_id)
        wid_end = None
        if wid_start is not None:
            wid_end = wid_start + self.window_sec * 1000 - 1
            settled = now_ms > wid_end
        if settled and baseline_cmp is not None and wid_start is not None and wid_end is not None:
            condition_id = str(poly.get("condition_id") or "")
            actual_side, actual_close, actual_error, settlement_source = self._resolve_actual_direction(
                window_id=window_id,
                start_ms=wid_start,
                end_ms=wid_end,
                baseline_price=baseline_cmp,
                condition_id=condition_id if condition_id else None,
            )
            if actual_side in ("UP", "DOWN") and predicted in ("UP", "DOWN"):
                matched = (actual_side == predicted)

        return {
            "ts_ms": ts_ms,
            "window_id": window_id,
            "signal_engine_side": side,
            "trigger_pattern": e.get("trigger_pattern"),
            "predicted_direction": predicted if predicted in ("UP", "DOWN") else None,
            "actual_direction": actual_side,
            "matched": matched,
            "theoretical_ev": expected_ev,
            "payoff_gross": payoff_gross,
            "p_reversal": p_rev,
            "likely_executable": executable,
            "executable_reason": opp.get("reason"),
            "baseline_price": baseline_cmp,
            "actual_close_price": actual_close,
            "settlement_source": settlement_source,
            "settled": settled,
            "actual_error": actual_error,
        }

    def _resolve_actual_direction(
        self,
        *,
        window_id: str,
        start_ms: int,
        end_ms: int,
        baseline_price: float,
        condition_id: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[float], Optional[str], Optional[str]]:
        if window_id in self._cache:
            c = self._cache[window_id]
            return c.get("actual_direction"), c.get("close_price"), c.get("error"), c.get("source")

        # 优先以 Polymarket 结算口径判定（用户口径）
        if condition_id:
            poly_side, poly_err = _fetch_polymarket_resolved_direction(condition_id=condition_id)
            if poly_side in ("UP", "DOWN"):
                self._cache[window_id] = {
                    "actual_direction": poly_side,
                    "close_price": None,
                    "error": None,
                    "source": "polymarket",
                }
                return poly_side, None, None, "polymarket"

        close_price, err = _fetch_binance_window_last_close(start_ms=start_ms, end_ms=end_ms)
        direction = None
        if close_price is not None and baseline_price > 0:
            direction = "UP" if close_price > baseline_price else "DOWN"
        self._cache[window_id] = {
            "actual_direction": direction,
            "close_price": close_price,
            "error": err if direction is not None else (err or (f"polymarket_unavailable:{poly_err}" if condition_id else None)),
            "source": "binance",
        }
        return direction, close_price, self._cache[window_id]["error"], "binance"


def _shadow_events_by_window_recent(path: str, *, max_lines: int = 16000) -> dict[str, dict[str, Any]]:
    """扫描 jsonl 尾部，每个 window_id 保留时间上最后一条（文件靠后的行覆盖靠前的）。"""
    out: dict[str, dict[str, Any]] = {}
    if not Path(path).is_file():
        return out
    try:
        lines = _tail_lines(path, max_lines)
    except Exception:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if not isinstance(e, dict):
            continue
        wid = str(e.get("window_id") or "")
        if wid:
            out[wid] = e
    return out


def _audit_direction_to_ud(direction_raw: Any) -> Optional[str]:
    if not isinstance(direction_raw, str):
        return None
    d = direction_raw.strip().lower()
    if d == "up":
        return "UP"
    if d == "down":
        return "DOWN"
    return None


def _fetch_recent_order_filled_payloads(state_db_path: str, *, limit: int) -> list[tuple[int, dict[str, Any]]]:
    """audit_events 中 kind=order_filled，按 id 倒序（最新在前）。"""
    if not Path(state_db_path).is_file() or limit <= 0:
        return []
    rows: list[tuple[int, dict[str, Any]]] = []
    try:
        conn = sqlite3.connect(state_db_path, timeout=3.0)
        try:
            cur = conn.execute(
                "SELECT ts_ms, payload FROM audit_events WHERE kind=? ORDER BY id DESC LIMIT ?",
                ("order_filled", limit),
            )
            for ts_ms, text in cur.fetchall():
                try:
                    payload = json.loads(text)
                except Exception:
                    payload = {"_raw": text}
                if isinstance(payload, dict):
                    rows.append((int(ts_ms), payload))
        finally:
            conn.close()
    except Exception:
        return []
    return rows


def _condition_id_from_shadow_event(e: dict[str, Any]) -> str:
    poly = e.get("polymarket") if isinstance(e.get("polymarket"), dict) else {}
    return str(poly.get("condition_id") or "").strip()


def _shadow_ev_and_payoff_from_event(e: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    side = str(e.get("side") or "").upper()
    p_rev = _safe_float(e.get("p_rev"))
    poly = e.get("polymarket") if isinstance(e.get("polymarket"), dict) else {}
    quote = poly.get("trade_quote") if isinstance(poly.get("trade_quote"), dict) else {}
    payoff_gross = _safe_float(quote.get("payoff_gross"))
    ev = _calc_expected_ev(side=side, p_reversal=p_rev, payoff_gross=payoff_gross)
    return ev, payoff_gross


def _window_start_ms(window_id: str) -> Optional[int]:
    if not window_id or not window_id.startswith("w"):
        return None
    raw = window_id[1:]
    if not raw.isdigit():
        return None
    try:
        return int(raw)
    except Exception:
        return None


def _infer_predicted_side(*, side: str, trigger: str) -> Optional[str]:
    if side not in ("TREND", "REVERSAL") or trigger not in ("111", "000"):
        return None
    if side == "TREND":
        return "UP" if trigger == "111" else "DOWN"
    return "DOWN" if trigger == "111" else "UP"


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _calc_expected_ev(
    *,
    side: str,
    p_reversal: Optional[float],
    payoff_gross: Optional[float],
) -> Optional[float]:
    """按回测口径计算单位本金 EV。"""
    if p_reversal is None or payoff_gross is None:
        return None
    p = max(0.0, min(1.0, float(p_reversal)))
    payoff = max(0.0, float(payoff_gross))
    side_u = str(side or "").upper()
    if side_u == "REVERSAL":
        # 反转单：赢面是 p_reversal，亏损为 1 单位本金
        return p * payoff - (1.0 - p)
    if side_u == "TREND":
        # 顺势单：赢面是 (1-p_reversal)
        return (1.0 - p) * payoff - p
    return None


@dataclass
class LiveFilledOutcomeBoard:
    """SQLite ``order_filled``（实盘成交）按同窗 ``shadow_signals.jsonl`` 补齐 baseline/condition_id 后的结算对账。"""

    state_db_path: str
    shadow_signals_path: str = "logs/shadow_signals.jsonl"
    window_sec: int = 300
    _settler: ShadowSignalOutcomeBoard = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_settler",
            ShadowSignalOutcomeBoard(path=self.shadow_signals_path, window_sec=self.window_sec),
        )

    def _build_rows(self, *, sql_limit: int) -> list[dict[str, Any]]:
        fills = _fetch_recent_order_filled_payloads(self.state_db_path, limit=max(sql_limit, 1))
        shadow_map = _shadow_events_by_window_recent(self.shadow_signals_path)
        now_ms = int(time.time() * 1000)
        settler = self._settler
        out: list[dict[str, Any]] = []
        for ts_ms, payload in fills:
            wid = str(payload.get("window_id") or "")
            predicted = _audit_direction_to_ud(payload.get("direction"))
            side = str(payload.get("side") or "").upper()
            se = shadow_map.get(wid)
            baseline = _safe_float(se.get("baseline_price")) if se else None
            condition_id = _condition_id_from_shadow_event(se) if se else ""
            theoretical_ev: Optional[float]
            payoff_gross: Optional[float]
            if se:
                theoretical_ev, payoff_gross = _shadow_ev_and_payoff_from_event(se)
            else:
                theoretical_ev, payoff_gross = None, None

            wid_start = _window_start_ms(wid)
            wid_end = (
                wid_start + self.window_sec * 1000 - 1 if wid_start is not None else None
            )
            settled = bool(wid_end is not None and now_ms > wid_end)

            actual_side: Optional[str] = None
            matched: Optional[bool] = None
            actual_close: Optional[float] = None
            actual_error: Optional[str] = None
            settlement_source: Optional[str] = None
            context_note: Optional[str] = None
            if not se:
                context_note = "no_shadow_jsonl_for_window"
            elif baseline is None:
                context_note = "shadow_missing_baseline"

            if settled and baseline is not None and wid_start is not None and wid_end is not None:
                actual_side, actual_close, actual_error, settlement_source = settler._resolve_actual_direction(
                    window_id=wid,
                    start_ms=wid_start,
                    end_ms=wid_end,
                    baseline_price=float(baseline),
                    condition_id=condition_id if condition_id else None,
                )
                if actual_side in ("UP", "DOWN") and predicted in ("UP", "DOWN"):
                    matched = actual_side == predicted
            elif settled and baseline is None:
                actual_error = context_note

            out.append({
                "ts_ms": ts_ms,
                "window_id": wid,
                "signal_engine_side": side or None,
                "predicted_direction": predicted,
                "actual_direction": actual_side,
                "matched": matched,
                "theoretical_ev": theoretical_ev,
                "payoff_gross": payoff_gross,
                "size_usdc": _safe_float(payload.get("size_usdc")),
                "fill_price": _safe_float(payload.get("price")),
                "client_order_id": payload.get("client_order_id"),
                "exchange_order_id": payload.get("exchange_order_id"),
                "baseline_price": baseline,
                "actual_close_price": actual_close,
                "settlement_source": settlement_source,
                "settled": settled,
                "actual_error": actual_error,
                "shadow_context": None if se else "missing",
            })
        return out

    def recent(self, n: int = 30) -> list[dict[str, Any]]:
        rows = self._build_rows(sql_limit=max(n * 4, 80))
        return rows[: max(n, 0)]

    def summary(self, *, hours: float = 24.0, scan_n: int = 200) -> dict[str, Any]:
        rows = self._build_rows(sql_limit=max(scan_n * 4, 120))
        now_ms = int(time.time() * 1000)
        since_ms = now_ms - int(hours * 3600 * 1000)
        recent_items = [x for x in rows if int(x.get("ts_ms") or 0) >= since_ms]
        judged = [x for x in recent_items if isinstance(x.get("matched"), bool)]
        matched_ok = [x for x in judged if x.get("matched") is True]
        hit_rate = (len(matched_ok) / len(judged)) if judged else None
        ev_vals = [
            float(x["theoretical_ev"])
            for x in recent_items
            if isinstance(x.get("theoretical_ev"), (int, float))
        ]
        avg_ev = (sum(ev_vals) / len(ev_vals)) if ev_vals else None
        return {
            "window_hours": hours,
            "sample_n_24h": len(recent_items),
            "filled_n_scanned": len(rows),
            "hit_rate": hit_rate,
            "hit_n": len(matched_ok),
            "hit_denominator": len(judged),
            "avg_theoretical_ev": avg_ev,
            "avg_ev_n": len(ev_vals),
        }


def _fetch_binance_window_last_close(*, start_ms: int, end_ms: int) -> tuple[Optional[float], Optional[str]]:
    url = "https://api.binance.com/api/v3/klines"
    params = urllib.parse.urlencode({
        "symbol": "BTCUSDT",
        "interval": "1s",
        "startTime": str(start_ms),
        "endTime": str(end_ms),
        "limit": "1000",
    })
    req = urllib.request.Request(
        url=f"{url}?{params}",
        headers={"User-Agent": "TwinEngines-WebUI/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return None, f"http_{e.code}"
    except Exception as e:
        return None, f"net_{type(e).__name__}"
    try:
        arr = json.loads(body)
    except Exception:
        return None, "bad_json"
    if not isinstance(arr, list) or not arr:
        return None, "empty_klines"
    last = arr[-1]
    if not isinstance(last, list) or len(last) < 5:
        return None, "bad_kline_row"
    try:
        return float(last[4]), None
    except Exception:
        return None, "bad_close"


def _fetch_polymarket_resolved_direction(*, condition_id: str) -> tuple[Optional[str], Optional[str]]:
    """按 condition_id 查询 Polymarket 结算结果，返回 UP/DOWN。"""
    cid = condition_id.strip()
    if not cid:
        return None, "empty_condition_id"
    if not cid.startswith("0x"):
        cid = "0x" + cid
    url = "https://gamma-api.polymarket.com/markets"
    params = urllib.parse.urlencode({"condition_ids": cid, "limit": "5"})
    req = urllib.request.Request(
        url=f"{url}?{params}",
        headers={"User-Agent": "TwinEngines-WebUI/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return None, f"http_{e.code}"
    except Exception as e:
        return None, f"net_{type(e).__name__}"
    try:
        arr = json.loads(body)
    except Exception:
        return None, "bad_json"
    if not isinstance(arr, list) or not arr:
        return None, "empty_market"
    m = arr[0] if isinstance(arr[0], dict) else {}
    outcomes = m.get("outcomes")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = None
    prices = m.get("outcomePrices") or m.get("outcome_prices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except Exception:
            prices = None

    # 先看显式 winner 字段
    for k in ("resolvedOutcome", "winningOutcome", "winner", "result"):
        v = m.get(k)
        if isinstance(v, str):
            u = v.strip().upper()
            if u in ("UP", "YES"):
                return "UP", None
            if u in ("DOWN", "NO"):
                return "DOWN", None
    # 再看 winner index
    idx_raw = m.get("winningOutcomeIndex")
    if isinstance(idx_raw, int) and isinstance(outcomes, list) and 0 <= idx_raw < len(outcomes):
        name = str(outcomes[idx_raw]).strip().upper()
        if name in ("UP", "YES"):
            return "UP", None
        if name in ("DOWN", "NO"):
            return "DOWN", None
    # 最后看 outcomePrices（结算后会接近 1/0）
    if isinstance(prices, list) and len(prices) >= 2:
        vals: list[float] = []
        for x in prices[:2]:
            try:
                vals.append(float(x))
            except Exception:
                vals.append(-1.0)
        if vals[0] >= 0 and vals[1] >= 0 and abs(vals[0] - vals[1]) > 1e-9:
            i = 0 if vals[0] > vals[1] else 1
            if isinstance(outcomes, list) and len(outcomes) >= 2:
                name = str(outcomes[i]).strip().upper()
                if name in ("UP", "YES"):
                    return "UP", None
                if name in ("DOWN", "NO"):
                    return "DOWN", None
            # 无 outcomes 时按常见顺序兜底：index0=YES/UP
            return ("UP" if i == 0 else "DOWN"), None
    return None, "unresolved_or_unknown_schema"


# ============================================================
# 主日志 (twinengines.log) tail + 级别过滤
# ============================================================

@dataclass
class LogsTail:
    log_dir: str = "logs"
    log_name: str = "twinengines.log"

    def path(self) -> str:
        return os.path.join(self.log_dir, self.log_name)

    def tail(self, n: int = 200, level_filter: Optional[str] = None) -> list[dict]:
        p = self.path()
        if not Path(p).is_file():
            return []
        try:
            lines = _tail_lines(p, max(n * 4, n))
        except Exception:
            return []
        rows: list[dict] = []
        wanted = (level_filter or "").upper().strip()
        wanted_norm = _normalize_alias(wanted) if wanted else ""
        skip_noise_block = False
        for line in lines:
            # 过滤第三方返回的整段 Cloudflare HTML（会污染实时日志面板）
            if _starts_noise_block(line):
                skip_noise_block = True
                continue
            if skip_noise_block:
                if _ends_noise_block(line):
                    skip_noise_block = False
                continue
            if _is_noise_line(line):
                continue
            level = _detect_level(line)
            if wanted and level != wanted_norm:
                continue
            rows.append({
                "level": level,
                "text": line.rstrip("\n"),
            })
            if len(rows) >= n:
                break
        rows.reverse()  # 最新在前
        return rows


def _normalize_alias(level: str) -> str:
    aliases = {"WARN": "WARNING", "ERR": "ERROR", "FATAL": "ERROR", "CRITICAL": "ERROR"}
    return aliases.get(level, level)


def _detect_level(line: str) -> str:
    upper = line.upper()
    if " [ERROR]" in upper or " [FATAL]" in upper or " [CRITICAL]" in upper:
        return "ERROR"
    if " [WARNING]" in upper or " [WARN]" in upper:
        return "WARNING"
    if " [INFO]" in upper:
        return "INFO"
    if " [DEBUG]" in upper:
        return "DEBUG"
    return "INFO"


def _starts_noise_block(line: str) -> bool:
    s = line.strip().lower()
    if not s:
        return False
    return (
        s.startswith("<!doctype html")
        or "sorry, you have been blocked" in s
        or "cloudflare ray id" in s
    )


def _ends_noise_block(line: str) -> bool:
    s = line.strip().lower()
    return "</html>" in s or "</body>" in s


def _is_noise_line(line: str) -> bool:
    s_raw = line.strip()
    s = s_raw.lower()
    if not s:
        return False
    # 兜底: 如果 tail 恰好从 HTML 片段中间开始（缺少 <!doctype> 开头），直接丢弃标签行
    if s_raw.startswith("<") and s_raw.endswith(">"):
        return True
    # 非块状时再兜底过滤常见 Cloudflare HTML 片段
    return (
        s.startswith("<span class=\"cf-")
        or s.startswith("<div class=\"cf-")
        or s.startswith("<link ")
        or s.startswith("<meta ")
        or s.startswith("<script")
        or s.startswith("</script")
        or s.startswith("<style")
        or s.startswith("</style")
        or "cf-footer-item" in s
        or "cf-error-footer" in s
    )


# ============================================================
# 当前阶段 / 当前资金 (kv_state)
# ============================================================

@dataclass
class RegimeInfo:
    state_db_path: str

    def current(self) -> dict[str, Any]:
        out = {
            "regime_name": None,
            "regime_equity": None,
            "regime_ts_ms": None,
            "last_equity_usdc": None,
            "last_equity_ts_ms": None,
        }
        if not Path(self.state_db_path).is_file():
            return out
        try:
            conn = sqlite3.connect(self.state_db_path, timeout=3.0)
            try:
                cur = conn.execute(
                    "SELECT key, value FROM kv_state WHERE key IN (?, ?)",
                    ("regime.current", "account.last_equity_usdc"),
                )
                for k, v in cur.fetchall():
                    try:
                        payload = json.loads(v)
                    except Exception:
                        continue
                    if k == "regime.current" and isinstance(payload, dict):
                        out["regime_name"] = payload.get("name")
                        out["regime_equity"] = payload.get("equity")
                        out["regime_ts_ms"] = payload.get("ts_ms")
                    elif k == "account.last_equity_usdc":
                        if isinstance(payload, (int, float)):
                            out["last_equity_usdc"] = float(payload)
                cur = conn.execute(
                    "SELECT ts_ms, payload FROM audit_events "
                    "WHERE kind IN (?, ?) ORDER BY id DESC LIMIT 20",
                    ("reconcile_equity_snapshot", "equity_snapshot"),
                )
                for ts_ms, payload in cur.fetchall():
                    try:
                        p = json.loads(payload)
                    except Exception:
                        continue
                    eq = EquityHistory._extract_equity(p)
                    if eq is None:
                        continue
                    if out["last_equity_usdc"] is None:
                        out["last_equity_usdc"] = float(eq)
                    out["last_equity_ts_ms"] = int(ts_ms)
                    break
            finally:
                conn.close()
        except Exception:
            return out
        return out


# ============================================================
# 通用 helpers
# ============================================================

# ============================================================
# 最近订单 / 窗口触发 (只读 SQLite)
# ============================================================

@dataclass
class OrdersTail:
    state_db_path: str

    def recent(self, n: int = 100, hours: float = 168.0) -> list[dict]:
        if not Path(self.state_db_path).is_file():
            return []
        since_ms = int((time.time() - hours * 3600.0) * 1000.0)
        out: list[dict] = []
        try:
            conn = sqlite3.connect(self.state_db_path, timeout=3.0)
            try:
                cur = conn.execute(
                    "SELECT ts_ms, kind, payload FROM audit_events "
                    "WHERE ts_ms>=? AND kind IN (?,?,?,?,?) "
                    "ORDER BY id DESC LIMIT ?",
                    (since_ms,
                     "order_filled", "order_failed",
                     "shadow_order", "shadow_signal", "shadow_window_triggered",
                     n),
                )
                for ts_ms, kind, text in cur.fetchall():
                    try:
                        payload = json.loads(text)
                    except Exception:
                        payload = {"_raw": text}
                    out.append({
                        "ts_ms": int(ts_ms), "kind": str(kind), "payload": payload,
                    })
            finally:
                conn.close()
        except Exception:
            return out
        return out


@dataclass
class TriggersTail:
    state_db_path: str

    def recent(self, n: int = 100, hours: float = 24.0) -> list[dict]:
        if not Path(self.state_db_path).is_file():
            return []
        since_ms = int((time.time() - hours * 3600.0) * 1000.0)
        out: list[dict] = []
        try:
            conn = sqlite3.connect(self.state_db_path, timeout=3.0)
            try:
                cur = conn.execute(
                    "SELECT ts_ms, kind, payload FROM audit_events "
                    "WHERE ts_ms>=? AND kind IN (?,?) ORDER BY id DESC LIMIT ?",
                    (since_ms, "shadow_window_triggered",
                     "shadow_window_skipped_late_start", n),
                )
                for ts_ms, kind, text in cur.fetchall():
                    try:
                        payload = json.loads(text)
                    except Exception:
                        payload = {"_raw": text}
                    out.append({
                        "ts_ms": int(ts_ms), "kind": str(kind), "payload": payload,
                    })
            finally:
                conn.close()
        except Exception:
            return out
        return out


# ============================================================
# 小时分桶指标 (signal frequency + trigger rate)
# ============================================================

@dataclass
class HourlyMetrics:
    state_db_path: str

    def signal_frequency(self, hours: int = 24) -> dict[str, Any]:
        """返回最近 N 小时按小时分桶的 trend / reversal 笔数."""
        zeros = {
            "hours": hours,
            "buckets": [],
            "totals": {"trend": 0, "reversal": 0, "triggered": 0},
        }
        if not Path(self.state_db_path).is_file():
            return zeros
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - hours * 3600 * 1000
        bucket_ms = 3600 * 1000

        buckets: list[dict[str, int | str]] = []
        for i in range(hours):
            t0 = start_ms + i * bucket_ms
            buckets.append({
                "ts_ms": t0,
                "label": time.strftime("%m-%d %H:00", time.localtime(t0 / 1000)),
                "trend": 0,
                "reversal": 0,
                "triggered": 0,
            })

        try:
            conn = sqlite3.connect(self.state_db_path, timeout=3.0)
            try:
                cur = conn.execute(
                    "SELECT ts_ms, kind, payload FROM audit_events "
                    "WHERE ts_ms>=? AND kind IN (?,?,?,?)",
                    (start_ms, "shadow_signal", "shadow_order", "order_filled",
                     "shadow_window_triggered"),
                )
                for ts_ms, kind, text in cur.fetchall():
                    idx = int((ts_ms - start_ms) // bucket_ms)
                    if idx < 0 or idx >= hours:
                        continue
                    if kind == "shadow_window_triggered":
                        buckets[idx]["triggered"] = int(buckets[idx]["triggered"]) + 1
                        continue
                    try:
                        side = str(json.loads(text).get("side", "")).upper()
                    except Exception:
                        side = ""
                    if side == "TREND":
                        buckets[idx]["trend"] = int(buckets[idx]["trend"]) + 1
                    elif side == "REVERSAL":
                        buckets[idx]["reversal"] = int(buckets[idx]["reversal"]) + 1
            finally:
                conn.close()
        except Exception:
            return zeros

        return {
            "hours": hours,
            "buckets": buckets,
            "totals": {
                "trend": sum(int(b["trend"]) for b in buckets),
                "reversal": sum(int(b["reversal"]) for b in buckets),
                "triggered": sum(int(b["triggered"]) for b in buckets),
            },
        }

    def order_outcomes(self, hours: float = 168.0) -> dict[str, int]:
        """统计最近 N 小时各类订单结局 (filled / failed / shadow / timeout)."""
        out = {"filled": 0, "failed": 0, "shadow_order": 0, "shadow_signal": 0}
        if not Path(self.state_db_path).is_file():
            return out
        since_ms = int((time.time() - hours * 3600.0) * 1000.0)
        try:
            conn = sqlite3.connect(self.state_db_path, timeout=3.0)
            try:
                cur = conn.execute(
                    "SELECT kind, COUNT(*) FROM audit_events "
                    "WHERE ts_ms>=? AND kind IN (?,?,?,?) GROUP BY kind",
                    (since_ms, "order_filled", "order_failed",
                     "shadow_order", "shadow_signal"),
                )
                for kind, n in cur.fetchall():
                    if kind == "order_filled":
                        out["filled"] = int(n)
                    elif kind == "order_failed":
                        out["failed"] = int(n)
                    elif kind == "shadow_order":
                        out["shadow_order"] = int(n)
                    elif kind == "shadow_signal":
                        out["shadow_signal"] = int(n)
            finally:
                conn.close()
        except Exception:
            return out
        return out


@dataclass
class FrictionSnapshotStatus:
    state_db_path: str
    snapshot_path: str = "data_runtime/friction_snapshot.json"
    proposal_path: str = "reports/param_update_proposal.json"

    def build(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "snapshot_exists": False,
            "proposal_exists": False,
            "snapshot_updated_ts_ms": None,
            "proposal_generated_ts_ms": None,
            "params": {},
            "proposal_checks": {},
            "proposal_counts": {
                "total": 0,
                "requires_manual_intervention": 0,
                "interval_blocked": 0,
                "capped_by_safety": 0,
            },
            "last_apply_event": None,
            "last_param_audits": [],
        }
        snap_file = Path(self.snapshot_path)
        if snap_file.is_file():
            out["snapshot_exists"] = True
            try:
                snap = json.loads(snap_file.read_text(encoding="utf-8"))
                out["snapshot_updated_ts_ms"] = snap.get("updated_ts_ms")
                out["params"] = snap.get("params") or {}
            except Exception:
                pass

        prop_file = Path(self.proposal_path)
        if prop_file.is_file():
            out["proposal_exists"] = True
            try:
                proposal = json.loads(prop_file.read_text(encoding="utf-8"))
                out["proposal_generated_ts_ms"] = proposal.get("generated_ts_ms")
                out["proposal_checks"] = proposal.get("checks") or {}
                proposals = proposal.get("proposals") or []
                out["proposal_counts"] = {
                    "total": len(proposals),
                    "requires_manual_intervention": sum(1 for x in proposals if x.get("requires_manual_intervention")),
                    "interval_blocked": sum(1 for x in proposals if not x.get("interval_ok", True)),
                    "capped_by_safety": sum(1 for x in proposals if x.get("capped_by_safety")),
                }
            except Exception:
                pass

        if not Path(self.state_db_path).is_file():
            return out
        try:
            conn = sqlite3.connect(self.state_db_path, timeout=3.0)
            try:
                cur = conn.execute(
                    "SELECT ts_ms, kind, payload FROM audit_events "
                    "WHERE kind=? ORDER BY id DESC LIMIT 1",
                    ("friction_snapshot_apply",),
                )
                row = cur.fetchone()
                if row:
                    ts_ms, kind, payload = row
                    try:
                        pd = json.loads(payload)
                    except Exception:
                        pd = {"_raw": payload}
                    out["last_apply_event"] = {"ts_ms": int(ts_ms), "kind": str(kind), "payload": pd}

                cur = conn.execute(
                    "SELECT ts_ms, operator, param, old_value, new_value, reason, proposal_path "
                    "FROM friction_param_audit ORDER BY id DESC LIMIT 10"
                )
                audits = []
                for r in cur.fetchall():
                    audits.append({
                        "ts_ms": int(r[0]),
                        "operator": str(r[1]),
                        "param": str(r[2]),
                        "old_value": float(r[3]),
                        "new_value": float(r[4]),
                        "reason": str(r[5]),
                        "proposal_path": str(r[6]),
                    })
                out["last_param_audits"] = audits
            finally:
                conn.close()
        except Exception:
            return out
        return out


@dataclass
class AutoRedeemStatus:
    state_db_path: str

    def build(self) -> dict[str, Any]:
        out = {
            "pending_n": 0,
            "recent_tx_hashes": [],
            "failure_reasons": {},
            "last_attempt_ts_ms": None,
            "last_success_ts_ms": None,
            "recent_attempts": [],
        }
        if not Path(self.state_db_path).is_file():
            return out
        try:
            conn = sqlite3.connect(self.state_db_path, timeout=3.0)
            try:
                cur = conn.execute("SELECT value FROM kv_state WHERE key=?", ("auto_redeem.status",))
                row = cur.fetchone()
                if row:
                    try:
                        kv = json.loads(row[0])
                        if isinstance(kv, dict):
                            out.update({
                                "pending_n": int(kv.get("pending_n") or 0),
                                "recent_tx_hashes": list(kv.get("recent_tx_hashes") or []),
                                "failure_reasons": dict(kv.get("failure_reasons") or {}),
                                "last_attempt_ts_ms": kv.get("last_attempt_ts_ms"),
                                "last_success_ts_ms": kv.get("last_success_ts_ms"),
                            })
                    except Exception:
                        pass
                cur = conn.execute(
                    "SELECT ts_ms, payload FROM audit_events WHERE kind=? ORDER BY id DESC LIMIT 30",
                    ("auto_redeem_attempt",),
                )
                attempts = []
                for ts_ms, payload in cur.fetchall():
                    try:
                        p = json.loads(payload)
                    except Exception:
                        p = {"_raw": payload}
                    attempts.append({
                        "ts_ms": int(ts_ms),
                        "condition_id": p.get("condition_id"),
                        "ok": bool(p.get("ok")),
                        "reason": p.get("reason"),
                        "tx_hash": p.get("tx_hash"),
                        "attempt": p.get("attempt"),
                    })
                out["recent_attempts"] = attempts
            finally:
                conn.close()
        except Exception:
            return out
        return out


@dataclass
class PRevCalibrationStatus:
    calibration_path: str = "data_runtime/p_rev_time_calibration.json"

    def build(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "exists": False,
            "generated_ts_ms": None,
            "window_hours": None,
            "bucket_sec": None,
            "min_samples": None,
            "sample_n": 0,
            "bucket_total": 0,
            "bucket_active": 0,
            "buckets": [],
            "notes": [],
        }
        p = Path(self.calibration_path)
        if not p.is_file():
            out["notes"] = ["calibration_file_missing"]
            return out
        out["exists"] = True
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            out["notes"] = ["bad_json"]
            return out
        if not isinstance(obj, dict):
            out["notes"] = ["bad_schema"]
            return out
        out["generated_ts_ms"] = obj.get("generated_ts_ms")
        out["window_hours"] = obj.get("window_hours")
        out["bucket_sec"] = obj.get("bucket_sec")
        out["min_samples"] = obj.get("min_samples")
        out["sample_n"] = int(obj.get("sample_n") or 0)
        buckets = obj.get("buckets") if isinstance(obj.get("buckets"), list) else []
        norm_buckets: list[dict[str, Any]] = []
        for b in buckets:
            if not isinstance(b, dict):
                continue
            norm_buckets.append({
                "start_sec": b.get("start_sec"),
                "end_sec": b.get("end_sec"),
                "n": int(b.get("n") or 0),
                "delta": b.get("delta"),
                "active": bool(b.get("active")),
                "mean_pred_p_rev": b.get("mean_pred_p_rev"),
                "mean_obs_reversal": b.get("mean_obs_reversal"),
            })
        out["buckets"] = norm_buckets
        out["bucket_total"] = len(norm_buckets)
        out["bucket_active"] = sum(1 for x in norm_buckets if x.get("active"))
        out["notes"] = list(obj.get("notes") or [])
        return out


@dataclass
class RuntimeEffectiveSnapshot:
    state_db_path: str

    def build(self) -> dict[str, Any]:
        out: dict[str, Any] = {"exists": False, "snapshot": None}
        if not Path(self.state_db_path).is_file():
            return out
        try:
            conn = sqlite3.connect(self.state_db_path, timeout=3.0)
            try:
                cur = conn.execute("SELECT value FROM kv_state WHERE key=?", ("runtime.effective_snapshot",))
                row = cur.fetchone()
            finally:
                conn.close()
        except Exception:
            return out
        if row is None:
            return out
        try:
            payload = json.loads(row[0])
        except Exception:
            return out
        if not isinstance(payload, dict):
            return out
        out["exists"] = True
        out["snapshot"] = payload
        return out


def _tail_lines(path: str, n: int, chunk_size: int = 8192) -> list[str]:
    """从文件末尾向前读, 返回最多 n 行 (按文件中的顺序, 即旧→新)."""
    p = Path(path)
    size = p.stat().st_size
    if size == 0:
        return []
    with p.open("rb") as f:
        data = b""
        pos = size
        while pos > 0 and data.count(b"\n") <= n:
            read = min(chunk_size, pos)
            pos -= read
            f.seek(pos)
            data = f.read(read) + data
        text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[-n:]
