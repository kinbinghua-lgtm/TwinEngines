from __future__ import annotations

import pickle
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

EPS = 1e-6
WINDOW_MINUTES = 5


def prefix_values(prefix_len: int) -> list[str]:
    if prefix_len <= 0:
        return []
    prefix_len = max(0, min(8, int(prefix_len)))
    return [format(i, f"0{prefix_len}b") for i in range(2 ** prefix_len)]


@dataclass(frozen=True)
class DirectionProbabilityInput:
    prefix: str
    d_signed: float
    t_remaining: float
    vol_30s: float
    vol_60s: float
    absret_30s: float
    absret_60s: float
    range_30s: float
    range_60s: float


@dataclass(frozen=True)
class DirectionProbabilityOutput:
    p_up: float
    p_down: float
    model_version: str
    features: dict[str, float | int | str]
    prefix_len: int = 3


@dataclass
class RollingVolatilityFeatureBuffer:
    maxlen: int = 90
    _closes: deque[float] = field(default_factory=lambda: deque(maxlen=90))
    _highs: deque[float] = field(default_factory=lambda: deque(maxlen=90))
    _lows: deque[float] = field(default_factory=lambda: deque(maxlen=90))

    def update(self, bar: Any) -> None:
        self._closes.append(float(bar.close))
        self._highs.append(float(bar.high))
        self._lows.append(float(bar.low))

    def snapshot(self) -> dict[str, float]:
        closes = list(self._closes)
        highs = list(self._highs)
        lows = list(self._lows)
        if len(closes) < 2:
            return {
                "vol_30s": 0.0,
                "vol_60s": 0.0,
                "absret_30s": 0.0,
                "absret_60s": 0.0,
                "range_30s": 0.0,
                "range_60s": 0.0,
            }
        rets = np.diff(np.asarray(closes, dtype=float)) / np.asarray(closes[:-1], dtype=float) * 100.0

        def std_last(n: int, min_n: int) -> float:
            arr = rets[-n:]
            return float(np.std(arr, ddof=1)) if len(arr) >= min_n else 0.0

        def abs_sum_last(n: int, min_n: int) -> float:
            arr = np.abs(rets[-n:])
            return float(np.sum(arr)) if len(arr) >= min_n else 0.0

        def range_last(n: int, min_n: int) -> float:
            if len(closes) < min_n:
                return 0.0
            hh = max(highs[-n:])
            ll = min(lows[-n:])
            px = max(closes[-1], EPS)
            return float((hh - ll) / px * 100.0)

        return {
            "vol_30s": std_last(30, 5),
            "vol_60s": std_last(60, 10),
            "absret_30s": abs_sum_last(30, 5),
            "absret_60s": abs_sum_last(60, 10),
            "range_30s": range_last(30, 5),
            "range_60s": range_last(60, 10),
        }


@dataclass
class DirectionProbabilityModel:
    model: Any
    feature_columns: list[str]
    model_version: str = "direction_probability_v1"
    metadata: dict[str, Any] = field(default_factory=dict)
    prefix_len: int = 3

    @classmethod
    def load(cls, path: str | Path) -> "DirectionProbabilityModel":
        p = Path(path)
        with p.open("rb") as f:
            payload = pickle.load(f)
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, dict):
            raise ValueError(f"unsupported direction model artifact type: {type(payload)!r}")
        model = payload.get("model")
        feature_columns = list(payload.get("feature_columns") or [])
        if model is None or not feature_columns:
            raise ValueError("direction model artifact missing model or feature_columns")
        metadata = dict(payload.get("metadata") or {})
        prefix_len = int(payload.get("prefix_len", metadata.get("prefix_len", 3)))
        return cls(
            model=model,
            feature_columns=feature_columns,
            model_version=str(payload.get("model_version") or payload.get("schema_version") or "direction_probability_v1"),
            metadata=metadata,
            prefix_len=prefix_len,
        )

    def predict(self, x: DirectionProbabilityInput) -> DirectionProbabilityOutput:
        row = self._make_feature_row(x)
        frame = pd.DataFrame([row], columns=self.feature_columns)
        p_up = float(self.model.predict_proba(frame)[0, 1])
        p_up = max(1e-6, min(1.0 - 1e-6, p_up))
        return DirectionProbabilityOutput(
            p_up=p_up,
            p_down=1.0 - p_up,
            model_version=self.model_version,
            features=row,
            prefix_len=self.prefix_len,
        )

    def _make_feature_row(self, x: DirectionProbabilityInput) -> dict[str, float | int | str]:
        prefix_len = max(0, min(8, int(self.prefix_len)))
        prefix = str(x.prefix or "")[:prefix_len]
        d_signed = float(x.d_signed)
        d_abs = abs(d_signed)
        t_remaining = float(x.t_remaining)
        vol30 = float(x.vol_30s)
        vol60 = float(x.vol_60s)
        obs_duration_sec = max(float((WINDOW_MINUTES - prefix_len) * 60), 1.0)
        row: dict[str, float | int | str] = {
            "d_signed": d_signed,
            "d_abs": d_abs,
            "t_remaining": t_remaining,
            "d_x_t": d_signed * t_remaining,
            "vol_30s": vol30,
            "vol_60s": vol60,
            "absret_30s": float(x.absret_30s),
            "absret_60s": float(x.absret_60s),
            "range_30s": float(x.range_30s),
            "range_60s": float(x.range_60s),
            "d_over_vol30": max(-50.0, min(50.0, d_signed / (vol30 + EPS))),
            "d_over_vol60": max(-50.0, min(50.0, d_signed / (vol60 + EPS))),
            "t_norm": t_remaining / obs_duration_sec,
            "is_above_open": int(d_signed > 0),
        }
        for p in prefix_values(prefix_len):
            is_p = int(prefix == p)
            row[f"prefix_{p}"] = is_p
            row[f"{p}_d"] = is_p * d_signed
            row[f"{p}_t"] = is_p * float(row["t_norm"])
            row[f"{p}_vol60"] = is_p * vol60
        return row


def build_direction_artifact_payload(*, model: Any, feature_columns: list[str], metadata: dict[str, Any]) -> dict[str, Any]:
    prefix_len = int(metadata.get("prefix_len", 3))
    return {
        "schema_version": "direction_probability_phase_v1",
        "model_version": str(metadata.get("model_version") or f"direction_probability_phase{prefix_len}_v1"),
        "semantic": "P(close_m5 > open_m5 | phase_prefix, d_signed, t_remaining, realized_volatility_features)",
        "prefix_len": prefix_len,
        "model": model,
        "feature_columns": list(feature_columns),
        "metadata": dict(metadata),
    }
