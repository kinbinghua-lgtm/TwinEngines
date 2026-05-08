"""
实时方向概率信号引擎。

语义：每秒直接输出当前状态下第 5 分钟最终 UP/DOWN 概率。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..model.direction_probability import (
    DirectionProbabilityInput,
    DirectionProbabilityModel,
    RollingVolatilityFeatureBuffer,
)
from ..sequence import build_fixed_baseline_sequence
from .binance_feed import KlineBar
from .logging_setup import get_logger

logger = get_logger(__name__)

WINDOW_SEC = 300
TRIGGER_AT_MIN = 3


@dataclass
class ShadowSignalEngineCfg:
    artifact_path: Optional[str] = None
    jsonl_path: str = "logs/shadow_signals.jsonl"
    bar_must_be_closed: bool = True
    log_every_eval: bool = False
    max_baseline_offset_sec: float = 120.0


@dataclass
class _WindowState:
    window_id: str
    start_ts_ms: int
    end_ts_ms: int
    baseline_price: Optional[float] = None
    skipped: bool = False
    minute_closes: list[float] = field(default_factory=list)
    triggered: bool = False
    trigger_pattern: Optional[str] = None
    signal_count: int = 0
    last_eval_sec: int = -1
    last_no_signal_reason: Optional[dict] = None

    def remaining_sec(self, now_ts_ms: int) -> float:
        return max(0.0, (self.end_ts_ms - now_ts_ms) / 1000.0)


@dataclass
class ShadowSignalEngine:
    cfg: ShadowSignalEngineCfg
    direction_model: DirectionProbabilityModel
    direction_models: dict[int, DirectionProbabilityModel] = field(default_factory=dict)

    on_signal_event: Optional[Callable[[dict], None]] = None
    audit_writer: Optional[Callable[[str, dict], None]] = None
    enrich_shadow_signal_event: Optional[Callable[[dict], None]] = None
    on_window_close: Optional[Callable[[str], None]] = None

    _state: Optional[_WindowState] = None
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _vol_buffer: RollingVolatilityFeatureBuffer = field(default_factory=RollingVolatilityFeatureBuffer)
    _evals_total: int = 0
    _signals_total: int = 0
    _windows_seen: int = 0
    _windows_triggered: int = 0
    _windows_skipped_late_start: int = 0
    _last_signal_event: Optional[dict] = None

    @classmethod
    def from_artifact(
        cls,
        artifact_path: str,
        *,
        jsonl_path: str = "logs/shadow_signals.jsonl",
        disable_trend: bool = False,
        disable_reversal: bool | None = None,
    ) -> "ShadowSignalEngine":
        del disable_trend, disable_reversal
        paths = [p.strip() for p in str(artifact_path).split(",") if p.strip()]
        models: dict[int, DirectionProbabilityModel] = {}
        for path in paths:
            m = DirectionProbabilityModel.load(path)
            models[int(getattr(m, "prefix_len", 3))] = m
        if not models:
            raise ValueError("no direction model artifacts provided")
        fallback_phase = 3 if 3 in models else sorted(models)[-1]
        model = models[fallback_phase]
        return cls(
            cfg=ShadowSignalEngineCfg(
                artifact_path=artifact_path,
                jsonl_path=jsonl_path,
            ),
            direction_model=model,
            direction_models=models,
        )

    def on_bar(self, bar: KlineBar) -> None:
        if self.cfg.bar_must_be_closed and not bar.is_closed:
            return
        try:
            self._handle_bar(bar)
        except Exception as e:
            logger.exception("ShadowSignalEngine handle_bar failed: %s", e)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "model_version": self.direction_model.model_version,
                "phase_models": {str(k): v.model_version for k, v in sorted(self.direction_models.items())},
                "evals_total": self._evals_total,
                "signals_total": self._signals_total,
                "windows_seen": self._windows_seen,
                "windows_triggered": self._windows_triggered,
                "windows_skipped_late_start": self._windows_skipped_late_start,
                "current_window_id": self._state.window_id if self._state else None,
                "current_window_triggered": self._state.triggered if self._state else False,
                "current_window_emitted": (self._state.signal_count > 0) if self._state else False,
                "current_window_skipped": self._state.skipped if self._state else False,
                "last_signal_event": self._last_signal_event,
            }

    def _handle_bar(self, bar: KlineBar) -> None:
        with self._lock:
            self._vol_buffer.update(bar)
            wid_start = (bar.close_time_ms // (WINDOW_SEC * 1000)) * (WINDOW_SEC * 1000)
            if self._state is None or self._state.start_ts_ms != wid_start:
                self._rotate_window(wid_start)

            st = self._state
            assert st is not None
            if st.skipped:
                return

            if st.baseline_price is None:
                offset_sec = (bar.close_time_ms - st.start_ts_ms) / 1000.0
                if offset_sec > 3.0:
                    if not self._backfill_window_from_rest(st):
                        st.skipped = True
                        self._windows_skipped_late_start += 1
                        self._emit_audit("shadow_window_skipped_late_start", {
                            "window_id": st.window_id,
                            "first_bar_offset_sec": round(offset_sec, 2),
                            "reason": "baseline_backfill_failed",
                        })
                        return
                if st.baseline_price is None:
                    st.baseline_price = float(bar.open)

            self._maybe_record_minute_close(st, bar)

            if not st.triggered and len(st.minute_closes) >= TRIGGER_AT_MIN:
                self._evaluate_trigger(st)
                if st.triggered:
                    self._windows_triggered += 1
                    self._emit_audit("shadow_window_triggered", {
                        "window_id": st.window_id,
                        "pattern": st.trigger_pattern,
                        "baseline_price": st.baseline_price,
                        "minute_closes": list(st.minute_closes),
                    })

            if st.baseline_price:
                self._evaluate_signal(st, bar)

    def _rotate_window(self, wid_start_ms: int) -> None:
        prev = self._state
        if prev is not None:
            if prev.triggered and prev.signal_count == 0:
                self._emit_audit("shadow_window_no_signal", {
                    "window_id": prev.window_id,
                    "reason": "window_expired_without_signal",
                    "detail": prev.last_no_signal_reason,
                })
            if self.on_window_close is not None:
                try:
                    self.on_window_close(prev.window_id)
                except Exception as e:
                    logger.warning("on_window_close failed wid=%s err=%s", prev.window_id, e)
        self._state = _WindowState(
            window_id=f"w{wid_start_ms}",
            start_ts_ms=int(wid_start_ms),
            end_ts_ms=int(wid_start_ms + WINDOW_SEC * 1000),
        )
        self._windows_seen += 1

    def _maybe_record_minute_close(self, st: _WindowState, bar: KlineBar) -> None:
        offset_ms = bar.close_time_ms - st.start_ts_ms
        if offset_ms < 0 or offset_ms > WINDOW_SEC * 1000:
            return
        minute_idx = offset_ms // 60_000
        if (offset_ms % 60_000) < 59_000:
            return
        if minute_idx >= len(st.minute_closes) and minute_idx < 4:
            st.minute_closes.append(float(bar.close))

    def _backfill_window_from_rest(self, st: _WindowState) -> bool:
        try:
            import json as _json
            import urllib.request as _ureq
            url = (
                "https://api.binance.com/api/v3/klines?symbol=BTCUSDT"
                f"&interval=1m&startTime={st.start_ts_ms}&limit=4"
            )
            data = _json.loads(_ureq.urlopen(_ureq.Request(url, headers={"User-Agent": "TE/1.0"}), timeout=5).read())
            if not isinstance(data, list) or not data:
                return False
            st.baseline_price = float(data[0][1])
            for k in data[:3]:
                st.minute_closes.append(float(k[4]))
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
        st.trigger_pattern = build_fixed_baseline_sequence(float(st.baseline_price), st.minute_closes[:TRIGGER_AT_MIN])
        st.triggered = True

    def _phase_for_sec(self, sec_in_window: int) -> int:
        if sec_in_window < 60:
            return 0
        if sec_in_window < 120:
            return 1
        if sec_in_window < 180:
            return 2
        if sec_in_window < 240:
            return 3
        return 4

    def _model_for_phase(self, phase: int) -> DirectionProbabilityModel | None:
        if self.direction_models:
            if phase in self.direction_models:
                return self.direction_models[phase]
            available = [k for k in self.direction_models if k <= phase]
            if available:
                return self.direction_models[max(available)]
            return self.direction_models[min(self.direction_models)]
        return self.direction_model

    def _evaluate_signal(self, st: _WindowState, bar: KlineBar) -> None:
        sec_in_window = int((bar.close_time_ms - st.start_ts_ms) // 1000)
        if sec_in_window <= st.last_eval_sec:
            return
        st.last_eval_sec = sec_in_window
        phase = self._phase_for_sec(sec_in_window)
        model = self._model_for_phase(phase)
        if model is None:
            return
        required_prefix_len = int(getattr(model, "prefix_len", phase))
        if len(st.minute_closes) < required_prefix_len:
            return
        baseline = float(st.baseline_price or 0.0)
        if baseline <= 0:
            return
        t_remaining = st.remaining_sec(bar.close_time_ms)
        if t_remaining <= 0:
            return

        current_price = float(bar.close)
        d_signed = (current_price - baseline) / baseline * 100.0
        vol = self._vol_buffer.snapshot()
        prefix = build_fixed_baseline_sequence(float(baseline), st.minute_closes[:required_prefix_len]) if required_prefix_len > 0 else ""
        out = model.predict(DirectionProbabilityInput(
            prefix=prefix,
            d_signed=d_signed,
            t_remaining=t_remaining,
            vol_30s=vol["vol_30s"],
            vol_60s=vol["vol_60s"],
            absret_30s=vol["absret_30s"],
            absret_60s=vol["absret_60s"],
            range_30s=vol["range_30s"],
            range_60s=vol["range_60s"],
        ))
        self._evals_total += 1
        st.signal_count += 1
        self._signals_total += 1

        event = {
            "kind": "shadow_signal",
            "ts_ms": int(bar.close_time_ms),
            "window_id": st.window_id,
            "side": "DIRECTION",
            "trigger_pattern": prefix,
            "sec_in_window": sec_in_window,
            "t_remaining_sec": round(t_remaining, 2),
            "baseline_price": baseline,
            "current_price": current_price,
            "d_signed_pct": round(d_signed, 6),
            "d_abs_pct": round(abs(d_signed), 6),
            "p_up": round(out.p_up, 6),
            "p_down": round(out.p_down, 6),
            "best_prob_dir": "up" if out.p_up >= out.p_down else "down",
            "best_prob": round(max(out.p_up, out.p_down), 6),
            "model_version": out.model_version,
            "phase": phase,
            "prefix_len": required_prefix_len,
            "vol_features": {k: round(float(v), 8) for k, v in vol.items()},
            "source": "direction_probability_engine",
            "signal_index_in_window": int(st.signal_count),
        }
        if self.enrich_shadow_signal_event is not None:
            try:
                self.enrich_shadow_signal_event(event)
            except Exception as e:
                logger.warning("enrich_shadow_signal_event failed: %s", e)
                event["polymarket_enrich_error"] = str(e)
        self._last_signal_event = event
        self._emit_signal_event(event)
        self._emit_audit("shadow_signal", event)

    def _emit_signal_event(self, event: dict) -> None:
        if self.on_signal_event is not None:
            try:
                self.on_signal_event(event)
                return
            except Exception as e:
                logger.exception("on_signal_event callback failed: %s", e)
        try:
            path = self.cfg.jsonl_path
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            logger.warning("ShadowSignalEngine jsonl write failed: %s", e)

    def _emit_audit(self, kind: str, payload: dict) -> None:
        if self.audit_writer is None:
            return
        try:
            self.audit_writer(kind, payload)
        except Exception as e:
            logger.exception("ShadowSignalEngine audit callback failed: %s", e)
