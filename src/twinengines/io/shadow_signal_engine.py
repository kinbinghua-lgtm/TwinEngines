"""
空跑信号引擎 (--dry-run-signals 专用):

只做一件事——接收 1s K 线, 在策略层视角下生成 TREND / REVERSAL / NONE 信号,
**不调用任何下单路径**, 仅:
    - 写入 SQLite ``audit_events`` 表 (kind='shadow_signal')
    - 追加到 ``logs/shadow_signals.jsonl``

设计原则 (与回测严格对齐, 不引入新策略):
    1. 5min 窗口对齐 UTC 整 5 分钟 (与 build_windows 同口径)
    2. baseline = 窗口起始时刻 K 线 close (= 第 1 分钟 open ≈ 5min K 线 open)
    4. 触发: 任意 3-bit 前缀均触发 (third_digit 语义)
    5. 触发后秒级评估:
       - d_abs_pct = |close - baseline| / baseline * 100
       - d0_abs_pct = 第 3 分钟末 |偏离|% (用于动态 λ₀)
       - p_rev 使用三层生存模型:
           layer 3: prefix 专用 (a_p, c_p) → λ₀ = a_p / (1 + c_p·d₀)
           layer 2: 全局动态 (a, c) → λ₀ = a / (1 + c·d₀)
           layer 1: 标量 λ₀ (回退)
       - 双 tracker 同时维护 (TrendStability + ReversalStability)
       - 每秒调用 decide_signal_v2
    6. 同窗多信号: 对 REVERSAL（及未禁用时的 TREND）采用「上升沿」触发——同一波连续满足只 emit 一次；
       条件中断后再次满足则再 emit，与实盘 GTC 多次挂单语义对齐。

模块化:
    - 不依赖 LiveRunner / PolymarketClient / 任何外部 IO; 单 KlineBar 即可驱动
    - 所有外部副作用 (DB / JSONL) 走两个回调钩子, 便于离线测试
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..sequence import build_fixed_baseline_sequence
from ..signals import (
    Signal,
    SignalThresholds,
)
from ..survival import SurvivalParams, reversal_probability
from .binance_feed import KlineBar
from .logging_setup import get_logger

logger = get_logger(__name__)


WINDOW_SEC = 300            # 5min
TRIGGER_AT_MIN = 3          # 第 3 分钟末判定触发
EVAL_OBS_HZ_SEC = 1         # 信号每秒评估一次


# ============================================================
# 配置
# ============================================================

@dataclass
class ShadowSignalEngineCfg:
    artifact_path: Optional[str] = None
    jsonl_path: str = "logs/shadow_signals.jsonl"
    bar_must_be_closed: bool = True
    log_every_eval: bool = False
    max_baseline_offset_sec: float = 120.0  # 允许窗中启动
    disable_trend: bool = False
    disable_reversal: bool = False
    global_dynamic_a: float = 0.0       # <=0 表示使用标量 lambda0
    global_dynamic_c: float = 0.0
    prefix_bucket_table: dict[str, dict] = field(default_factory=dict)


# ============================================================
# 窗口状态
# ============================================================

@dataclass
class _WindowState:
    window_id: str
    start_ts_ms: int
    end_ts_ms: int
    baseline_price: Optional[float] = None
    skipped: bool = False                     # 窗口起点错过 → 整窗作废

    minute_closes: list[float] = field(default_factory=list)  # 第 1/2/3 分钟 close
    triggered: bool = False
    trigger_pattern: Optional[str] = None      # 3-bit prefix (e.g. "010", "111")
    trigger_direction: Optional[str] = None    # "up" | "down"
    d0_abs_pct: float = 0.0                   # 第 3 分钟末 |偏离|%

    last_eval_sec: int = -1
    signal_count: int = 0
    last_no_signal_reason: Optional[dict] = None

    def remaining_sec(self, now_ts_ms: int) -> float:
        return max(0.0, (self.end_ts_ms - now_ts_ms) / 1000.0)


# ============================================================
# 主引擎
# ============================================================

@dataclass
class ShadowSignalEngine:
    cfg: ShadowSignalEngineCfg
    survival_params: SurvivalParams
    thresholds: SignalThresholds
    baseline_reversal_prob: float

    # 副作用钩子 (默认 = JSONL + audit_events)
    on_signal_event: Optional[Callable[[dict], None]] = None
    audit_writer: Optional[Callable[[str, dict], None]] = None
    # 信号事件富化 (例如 LiveRunner 附加 Polymarket 盘口; 就地修改 dict)
    enrich_shadow_signal_event: Optional[Callable[[dict], None]] = None
    # 5 分钟窗口轮换前回调 (例如 LiveRunner settle_window 取消未完结 GTC)
    on_window_close: Optional[Callable[[str], None]] = None

    # 内部状态
    _state: Optional[_WindowState] = None
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _evals_total: int = 0
    _signals_total: int = 0
    _trend_total: int = 0
    _rev_total: int = 0
    _windows_seen: int = 0
    _windows_triggered: int = 0
    _windows_skipped_late_start: int = 0
    _last_signal_event: Optional[dict] = None
    _cal_table: dict | None = None

    # ---------------- 工厂 ----------------

    @classmethod
    def from_artifact(
        cls,
        artifact_path: str,
        *,
        jsonl_path: str = "logs/shadow_signals.jsonl",
        disable_trend: bool = False,
        disable_reversal: bool | None = None,
    ) -> "ShadowSignalEngine":
        import json as _json
        with open(str(artifact_path), "r", encoding="utf-8") as _f:
            _raw = _json.load(_f)
        se = _raw.get("shadow_engine") or {}
        dr = bool(se.get("disable_reversal", False)) if disable_reversal is None else bool(disable_reversal)

        sm = _raw.get("survival_model") or {}
        gd = sm.get("global_dynamic") or {}
        pbt = dict(sm.get("prefix_buckets") or {})
        from ..model.persist import load_artifact
        art = load_artifact(artifact_path)

        cfg = ShadowSignalEngineCfg(
            artifact_path=artifact_path,
            jsonl_path=jsonl_path,
            disable_trend=bool(disable_trend),
            disable_reversal=dr,
            global_dynamic_a=float(gd.get("a") or 0.0),
            global_dynamic_c=float(gd.get("c") or 0.0),
            prefix_bucket_table=pbt,
        )
        out = cls(
            cfg=cfg,
            survival_params=art.params,
            thresholds=SignalThresholds(
                trend_reversal_prob_max=float(art.thresholds.trend_reversal_prob_max),
                trend_signal_stability_sec_min=int(art.thresholds.trend_signal_stability_sec_min),
                reversal_baseline_offset_min=float(art.thresholds.reversal_baseline_offset_min),
                reversal_signal_stability_sec_min=int(art.thresholds.reversal_signal_stability_sec_min),
                reversal_prob_min=float(art.thresholds.reversal_prob_min),
                reversal_edge_vs_baseline_min=float(art.thresholds.reversal_edge_vs_baseline_min),
                trend_min_expected_value=float(art.thresholds.trend_min_expected_value),
            ),
            baseline_reversal_prob=float(art.baseline_prob),
        )
        return out

    # ---------------- 主入口 ----------------

    def on_bar(self, bar: KlineBar) -> None:
        """LiveRunner 把 1s 闭合 K 线转发到这里."""
        if self.cfg.bar_must_be_closed and not bar.is_closed:
            return
        try:
            self._handle_bar(bar)
        except Exception as e:
            logger.exception("ShadowSignalEngine handle_bar failed: %s", e)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "evals_total": self._evals_total,
                "signals_total": self._signals_total,
                "trend_total": self._trend_total,
                "reversal_total": self._rev_total,
                "windows_seen": self._windows_seen,
                "windows_triggered": self._windows_triggered,
                "windows_skipped_late_start": self._windows_skipped_late_start,
                "current_window_id": self._state.window_id if self._state else None,
                "current_window_triggered": self._state.triggered if self._state else False,
                "current_window_emitted": (
                    (self._state.signal_count > 0) if self._state else False
                ),
                "current_window_skipped": self._state.skipped if self._state else False,
                "last_signal_event": self._last_signal_event,
                "disable_reversal": bool(self.cfg.disable_reversal),
            }

    # ---------------- 内部 ----------------

    def _handle_bar(self, bar: KlineBar) -> None:
        with self._lock:
            wid_start = (bar.close_time_ms // (WINDOW_SEC * 1000)) * (WINDOW_SEC * 1000)
            need_new = self._state is None or self._state.start_ts_ms != wid_start
            if need_new:
                self._rotate_window(wid_start, bar)

            st = self._state
            assert st is not None
            if st.skipped:
                return

            # 第一根 1s bar 用作 baseline (窗口起点附近); open 比 close 更接近窗口边界
            if st.baseline_price is None:
                offset_sec = (bar.close_time_ms - st.start_ts_ms) / 1000.0
                if offset_sec > self.cfg.max_baseline_offset_sec:
                    # 晚启动: 尝试从 REST 回补分钟数据, 避免跳窗
                    if self._backfill_window_from_rest(st):
                        # 回补成功: 已设置 baseline + minute_closes, 可能已触发
                        logger.info("ShadowSignalEngine backfilled window %s (offset=%.1fs)",
                                    st.window_id, offset_sec)
                    else:
                        st.skipped = True
                        self._windows_skipped_late_start += 1
                        self._emit_audit("shadow_window_skipped_late_start", {
                            "window_id": st.window_id,
                            "first_bar_offset_sec": round(offset_sec, 2),
                            "max_allowed_sec": self.cfg.max_baseline_offset_sec,
                        })
                        return
                if st.baseline_price is None:
                    st.baseline_price = float(bar.open)

            # 维护 1m close 桶 (按 close_time_ms 落入第几个 1min)
            self._maybe_record_minute_close(st, bar)

            # 第 3 分钟末完成后判定触发 (一次性)
            if not st.triggered and len(st.minute_closes) >= TRIGGER_AT_MIN:
                self._evaluate_trigger(st)
                if st.triggered:
                    self._windows_triggered += 1
                    self._emit_audit("shadow_window_triggered", {
                        "window_id": st.window_id,
                        "pattern": st.trigger_pattern,
                        "direction": st.trigger_direction,
                        "baseline_price": st.baseline_price,
                        "minute_closes": list(st.minute_closes),
                    })

            # 触发后做秒级评估 (同窗允许多次 emit，见 _evaluate_signal 上升沿逻辑)
            if st.triggered and st.baseline_price:
                self._evaluate_signal(st, bar)

    def _rotate_window(self, wid_start_ms: int, bar: KlineBar) -> None:
        prev = self._state
        if prev is not None:
            if prev.triggered and prev.signal_count == 0:
                self._emit_audit("shadow_window_no_signal", {
                    "window_id": prev.window_id,
                    "reason": "window_expired_without_signal",
                    "detail": prev.last_no_signal_reason,
                })
            cb = self.on_window_close
            if cb is not None:
                try:
                    cb(prev.window_id)
                except Exception as e:
                    logger.warning("on_window_close failed wid=%s err=%s", prev.window_id, e)

        self._state = _WindowState(
            window_id=f"w{wid_start_ms}",
            start_ts_ms=int(wid_start_ms),
            end_ts_ms=int(wid_start_ms + WINDOW_SEC * 1000),
        )
        self._windows_seen += 1
        logger.debug("ShadowSignalEngine new window %s", self._state.window_id)

    def _maybe_record_minute_close(self, st: _WindowState, bar: KlineBar) -> None:
        offset_ms = bar.close_time_ms - st.start_ts_ms
        if offset_ms < 0 or offset_ms > WINDOW_SEC * 1000:
            return
        minute_idx = offset_ms // 60_000   # 0..4
        # 仅在某分钟最后一秒 (offset 落在该分钟末) 才记 close
        in_last_sec_of_minute = (offset_ms % 60_000) >= 59_000
        if not in_last_sec_of_minute:
            return
        if minute_idx >= len(st.minute_closes) and minute_idx < 4:
            st.minute_closes.append(float(bar.close))

    def _backfill_window_from_rest(self, st: _WindowState) -> bool:
        """晚启动时从 Binance REST 回补当前窗口的分钟数据, 返回是否成功."""
        try:
            import json as _json, urllib.request as _ureq
            url = (f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT"
                   f"&interval=1m&startTime={st.start_ts_ms}&limit=4")
            data = _json.loads(_ureq.urlopen(
                _ureq.Request(url, headers={"User-Agent": "TE/1.0"}), timeout=5).read())
            if not isinstance(data, list) or len(data) < 1:
                return False
            # baseline = 第一个可用的 1 分钟 K 线 open
            st.baseline_price = float(data[0][1])
            # 记录已完成的分钟 close (最多前 3 分钟)
            for k in data[:3]:
                st.minute_closes.append(float(k[4]))
            # 如果前 3 分钟已齐, 立即判定触发
            if len(st.minute_closes) >= TRIGGER_AT_MIN:
                self._evaluate_trigger(st)
                if st.triggered:
                    self._windows_triggered += 1
            return True
        except Exception as e:
            logger.warning("ShadowSignalEngine backfill REST failed: %s", e)
            return False

    def _evaluate_trigger(self, st: _WindowState) -> None:
        if len(st.minute_closes) < TRIGGER_AT_MIN or st.baseline_price is None:
            return
        baseline = float(st.baseline_price)
        seq3 = build_fixed_baseline_sequence(baseline, st.minute_closes[:TRIGGER_AT_MIN])
        c3_close = float(st.minute_closes[2])
        d0 = abs(c3_close - baseline) / baseline * 100.0
        st.triggered = True
        st.trigger_pattern = seq3
        st.d0_abs_pct = d0
        last_bit = seq3[-1]
        st.trigger_direction = "up" if last_bit == "1" else "down"

    def _effective_lambda0(self, prefix: str | None, d0: float) -> float:
        """三层 λ₀: prefix 专用 → 全局动态 → 标量回退。"""
        if prefix and len(prefix) >= 3:
            key = prefix[:3]
            bucket = self.cfg.prefix_bucket_table.get(key)
            if bucket:
                a, c = float(bucket["a"]), float(bucket["c"])
                return a / (1.0 + c * max(d0, 0.0))
        if self.cfg.global_dynamic_a > 0:
            return self.cfg.global_dynamic_a / (1.0 + self.cfg.global_dynamic_c * max(d0, 0.0))
        return self.survival_params.lambda0


    def _lookup_calibration(self, p_rev_raw: float, prefix: str | None, t_remaining: float, d_abs_pct: float) -> tuple[float, float]:
        """返回 (rate, rate_lower) — 4D校准: prefix × T × d × p_raw。"""
        if not hasattr(self, '_cal_table') or self._cal_table is None:
            self._load_calibration_table()
        if self._cal_table is None:
            self._cal_table = {}
        key = (prefix[:3] if prefix and len(prefix) >= 3 else "") if prefix else ""
        entry = self._cal_table.get(key)
        if not entry:
            return float(p_rev_raw), float(p_rev_raw)

        overall_event_rate = float(entry.get("event_rate", p_rev_raw))

        t_buckets = entry.get("t_buckets") or {}
        if not t_buckets:
            return float(p_rev_raw), float(p_rev_raw)

        # Find T bucket (15-45, 45-75, 75-120)
        T = max(float(t_remaining), 1.0)
        t_bin = "2"
        if T < 75: t_bin = "1"
        if T < 45: t_bin = "0"
        tb = t_buckets.get(t_bin) or t_buckets.get("0") or {}
        if not tb:
            return float(p_rev_raw), float(p_rev_raw)

        # Find d bucket
        d_buckets = tb.get("d_buckets") or {}
        if not d_buckets:
            return float(p_rev_raw), float(p_rev_raw)
        d_bin = "0"
        best_d = 999
        for di, db in d_buckets.items():
            d_mid = (db.get("d_lo", 0) + db.get("d_hi", 0)) / 2
            if abs(float(d_abs_pct) - d_mid) < best_d:
                best_d = abs(float(d_abs_pct) - d_mid)
                d_bin = di
        dbucket = d_buckets.get(d_bin)
        if not dbucket:
            dbucket = list(d_buckets.values())[0]
        bins = dbucket.get("bins") or []
        if not bins:
            return float(p_rev_raw), float(p_rev_raw)

        for b in bins:
            if b["lo"] <= p_rev_raw < b["hi"]:
                # 在训练数据范围内: 正常返回校准值
                cal_rate = float(b["rate"])
                cal_lower = float(b.get("rate_lower", cal_rate))
                # 安全兜底: 校准值不应该远超该前缀的总体反转率
                cap = max(overall_event_rate * 1.8, overall_event_rate + 0.10)
                if cal_rate > cap:
                    return cap, min(cal_lower, cap)
                return cal_rate, cal_lower

        # p_rev_raw 超出训练数据所有 bin 的范围 → 用该前缀总体事件率兜底
        if p_rev_raw >= bins[-1]["hi"]:
            # 模型高估了反转概率, 回退到经验平均值
            return overall_event_rate, overall_event_rate * 0.85
        else:
            # p_rev_raw 低于第一个 bin: 用第一个 bin 的 rate
            return float(bins[0]["rate"]), float(bins[0].get("rate_lower", bins[0]["rate"]))

    def _load_calibration_table(self) -> None:
        try:
            import json as _json
            from pathlib import Path as _Path
            p = "data_runtime/calibration_table.json"
            if _Path(p).exists():
                data = _json.loads(_Path(p).read_text(encoding="utf-8"))
                self._cal_table = data.get("table") or {}
                self._cal_table["_params"] = data.get("params") or {}
            else:
                self._cal_table = {}
        except Exception:
            self._cal_table = {}


    def _evaluate_signal(self, st: _WindowState, bar: KlineBar) -> None:
        sec_in_window = int((bar.close_time_ms - st.start_ts_ms) // 1000)
        if sec_in_window <= st.last_eval_sec:
            return
        st.last_eval_sec = sec_in_window

        if sec_in_window < TRIGGER_AT_MIN * 60:
            return

        baseline = float(st.baseline_price or 0.0)
        if baseline <= 0:
            return

        d_abs_pct = abs(float(bar.close) - baseline) / baseline * 100.0
        t_remaining = st.remaining_sec(bar.close_time_ms)
        if t_remaining <= 0:
            return

        try:
            l0_eff = self._effective_lambda0(st.trigger_pattern, st.d0_abs_pct)
            sp_eff = SurvivalParams(
                lambda0=l0_eff, gamma=self.survival_params.gamma,
                beta=self.survival_params.beta,
            )
            p_rev_raw = reversal_probability(d_abs_pct, t_remaining, sp_eff)
        except Exception as e:
            logger.warning("reversal_probability failed: %s", e)
            return
        p_rev, p_rev_lower = self._lookup_calibration(float(p_rev_raw), st.trigger_pattern, t_remaining, d_abs_pct)
        self._evals_total += 1

        if self.cfg.log_every_eval:
            self._emit_signal_event({
                "kind": "eval",
                "ts_ms": int(bar.close_time_ms),
                "window_id": st.window_id,
                "sec_in_window": sec_in_window,
                "t_remaining_sec": round(t_remaining, 2),
                "d_abs_pct": round(d_abs_pct, 6),
                "p_rev": round(p_rev, 6),
                "p_rev_raw": round(float(p_rev_raw), 6),
                "baseline_reversal_prob": self.baseline_reversal_prob,
                "decision": "RAW",
            })

        st.signal_count += 1
        self._signals_total += 1
        self._trend_total += 1

        event = {
            "kind": "shadow_signal",
            "ts_ms": int(bar.close_time_ms),
            "window_id": st.window_id,
            "side": "TREND",
            "trigger_pattern": st.trigger_pattern,
            "trigger_direction": st.trigger_direction,
            "sec_in_window": sec_in_window,
            "t_remaining_sec": round(t_remaining, 2),
            "baseline_price": st.baseline_price,
            "current_price": float(bar.close),
            "d_abs_pct": round(d_abs_pct, 6),
            "p_rev": round(p_rev, 6),
            "p_rev_lower": round(float(p_rev_lower), 6),
            "p_rev_raw": round(float(p_rev_raw), 6),
            "baseline_reversal_prob": float(self.baseline_reversal_prob),
            "thresholds": {
                "disable_trend": bool(self.cfg.disable_trend),
                "disable_reversal": bool(self.cfg.disable_reversal),
            },
            "source": "shadow_signal_engine",
            "signal_index_in_window": int(st.signal_count),
        }
        enrich = self.enrich_shadow_signal_event
        if enrich is not None:
            try:
                enrich(event)
            except Exception as e:
                logger.warning("enrich_shadow_signal_event failed: %s", e)
                event["polymarket_enrich_error"] = str(e)
        self._last_signal_event = event
        self._emit_signal_event(event)
        self._emit_audit("shadow_signal", event)

    # ---------------- 副作用 ----------------

    def _emit_signal_event(self, event: dict) -> None:
        cb = self.on_signal_event
        if cb is not None:
            try:
                cb(event)
                return
            except Exception as e:
                logger.exception("on_signal_event callback failed: %s", e)
        # 默认: 追加到 jsonl
        try:
            path = self.cfg.jsonl_path
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            line = json.dumps(event, ensure_ascii=False, default=str)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            logger.warning("ShadowSignalEngine jsonl write failed: %s", e)

    def _emit_audit(self, kind: str, payload: dict) -> None:
        cb = self.audit_writer
        if cb is None:
            return
        try:
            cb(kind, payload)
        except Exception as e:
            logger.exception("ShadowSignalEngine audit callback failed: %s", e)
