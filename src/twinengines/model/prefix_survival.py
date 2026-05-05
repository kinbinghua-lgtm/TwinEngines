"""
Prefix-conditioned dynamic survival model.

The live strategy can only condition on prefixes that are already observable. This module
therefore trains bucket parameters for first3 and first4 prefixes, rather than using the
final 5-bit sequence as a feature.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from ..data.window import MS_PER_MIN, MS_PER_SEC, WindowSample
from .fit import EPS
from .survival_ablation import _window_H_dynamic_l0
from .survival_survival_validation import reversal_prob_from_H

SCHEMA_PREFIX_DYNAMIC_V1 = "third_digit_prefix_dynamic_v1"


@dataclass(frozen=True)
class PrefixBucketParams:
    prefix: str
    n_windows: int
    n_events: int
    event_rate: float
    a: float
    c: float
    gamma: float
    beta: float
    log_likelihood: float
    success: bool
    message: str
    shrink_l2: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PrefixBucketParams":
        return cls(
            prefix=str(payload["prefix"]),
            n_windows=int(payload.get("n_windows", 0)),
            n_events=int(payload.get("n_events", 0)),
            event_rate=float(payload.get("event_rate", 0.0)),
            a=float(payload["a"]),
            c=float(payload["c"]),
            gamma=float(payload["gamma"]),
            beta=float(payload["beta"]),
            log_likelihood=float(payload.get("log_likelihood", 0.0)),
            success=bool(payload.get("success", True)),
            message=str(payload.get("message", "")),
            shrink_l2=float(payload.get("shrink_l2", 0.0)),
        )


@dataclass(frozen=True)
class PrefixDynamicModel:
    gamma: float
    beta: float
    dynamic_a: float
    dynamic_c: float
    symbol: str = "BTCUSDT"
    buckets: dict[str, PrefixBucketParams] | None = None

    def to_json(self) -> dict[str, Any]:
        buckets = self.buckets or {}
        return {
            "schema_version": SCHEMA_PREFIX_DYNAMIC_V1,
            "gamma": float(self.gamma),
            "beta": float(self.beta),
            "dynamic_a": float(self.dynamic_a),
            "dynamic_c": float(self.dynamic_c),
            "symbol": str(self.symbol),
            "prefix_buckets": {k: v.to_dict() for k, v in sorted(buckets.items())},
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "PrefixDynamicModel":
        if payload.get("schema_version") != SCHEMA_PREFIX_DYNAMIC_V1:
            raise ValueError(
                f"expect schema_version={SCHEMA_PREFIX_DYNAMIC_V1!r}, got {payload.get('schema_version')!r}"
            )
        raw = payload.get("prefix_buckets") or {}
        buckets = {str(k): PrefixBucketParams.from_dict(v) for k, v in raw.items()}
        return cls(
            gamma=float(payload["gamma"]),
            beta=float(payload["beta"]),
            dynamic_a=float(payload["dynamic_a"]),
            dynamic_c=float(payload["dynamic_c"]),
            symbol=str(payload.get("symbol", "BTCUSDT")),
            buckets=buckets,
        )

    def bucket_for_prefix(self, prefix: str | None) -> PrefixBucketParams | None:
        if not prefix:
            return None
        buckets = self.buckets or {}
        p = str(prefix)
        if p in buckets:
            return buckets[p]
        if len(p) > 3 and p[:3] in buckets:
            return buckets[p[:3]]
        return None


def _switches(bits: str) -> int:
    return sum(1 for a, b in zip(bits, bits[1:]) if a != b)


def prefix_target_switch_count(prefix: str, pred_bit: str) -> int:
    p = str(prefix)
    if not p:
        return 0
    return _switches(p) + (0 if str(pred_bit) == p[-1] else 1)


def _sample_prefix_row(s: WindowSample, prefix_len: int) -> dict[str, Any] | None:
    seq = str(s.sequence)
    if len(seq) < 5 or prefix_len not in (3, 4):
        return None
    prefix = seq[:prefix_len]
    last_bit = prefix[-1]
    final_bit = seq[-1]
    event = int(final_bit != last_bit)
    boundary_ms = int(s.window_start_ms) + int(prefix_len) * MS_PER_MIN
    mask = np.asarray(s.obs_times_ms, dtype=np.int64) >= int(boundary_ms)
    if not bool(np.any(mask)):
        return None
    prices = np.asarray(s.obs_prices, dtype=float)[mask]
    times = np.asarray(s.obs_times_ms, dtype=np.int64)[mask]
    if len(prices) == 0:
        return None
    baseline = float(s.baseline)
    if baseline <= 0:
        return None
    ds = np.abs(prices - baseline) / baseline * 100.0
    vs = None
    if hasattr(s, "vol_ratio_per_obs") and s.vol_ratio_per_obs is not None:
        vs = np.asarray(s.vol_ratio_per_obs, dtype=float)[mask]
        if len(vs) == 0:
            vs = None
    end_obs_ms = int(s.window_start_ms) + 5 * MS_PER_MIN - MS_PER_SEC
    obs_duration = float(end_obs_ms - boundary_ms + MS_PER_SEC) / float(MS_PER_SEC)
    ts = obs_duration - ((times - boundary_ms) / float(MS_PER_SEC))
    ts = np.clip(ts, 0.0, obs_duration)
    d0 = float(abs(float(prices[0]) - baseline) / baseline * 100.0)
    return {
        "window_start_ms": int(s.window_start_ms),
        "prefix": prefix,
        "prefix_len": int(prefix_len),
        "sequence": seq,
        "ds": np.asarray(ds, dtype=float),
        "ts": np.asarray(ts, dtype=float),
        "vs": np.asarray(vs, dtype=float) if vs is not None else None,
        "d0": float(d0),
        "event": int(event),
        "final_bit": final_bit,
        "prefix_switches": _switches(prefix),
        "target_switch_if_event": _switches(prefix) + 1,
    }


def build_prefix_survival_agg(samples: Iterable[WindowSample], *, prefix_lens: tuple[int, ...] = (3, 4)) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for s in samples:
        for plen in prefix_lens:
            row = _sample_prefix_row(s, int(plen))
            if row is not None:
                rows.append(row)
    return pd.DataFrame(rows)


def _neg_ll_bucket(
    x: np.ndarray,
    ds_arr: list[np.ndarray],
    ts_arr: list[np.ndarray],
    d0_arr: list[float],
    e: np.ndarray,
    gamma: float,
    beta: float,
    prior_log_a: float,
    prior_log_c: float,
    shrink_l2: float,
    beta_vol: float = 0.0,
    vs_arr: list[np.ndarray | None] | None = None,
) -> float:
    log_a, log_c = x
    a = float(np.exp(log_a))
    c = float(np.exp(log_c))
    H = np.empty(len(ds_arr), dtype=float)
    for i, (ds, ts, d0) in enumerate(zip(ds_arr, ts_arr, d0_arr)):
        vs = vs_arr[i] if vs_arr is not None and i < len(vs_arr) else None
        H[i] = _window_H_dynamic_l0(ds, ts, d0=d0, a=a, c=c, gamma=gamma, beta=beta, beta_vol=beta_vol, vs=vs)
    p = reversal_prob_from_H(np.clip(H, 0.0, 50.0))
    p = np.clip(p, EPS, 1.0 - EPS)
    ll = e * np.log(p) + (1.0 - e) * np.log(1.0 - p)
    penalty = float(shrink_l2) * ((float(log_a) - float(prior_log_a)) ** 2 + (float(log_c) - float(prior_log_c)) ** 2)
    return float(-np.sum(ll) + penalty)


def fit_prefix_bucket_dynamic(
    agg: pd.DataFrame,
    *,
    prefix: str,
    gamma: float,
    beta: float,
    global_a: float,
    global_c: float,
    min_n: int = 100,
    shrink_l2: float = 2.0,
) -> PrefixBucketParams | None:
    g = agg[agg["prefix"] == str(prefix)]
    n = int(len(g))
    if n < int(min_n):
        return None
    ds_arr = g["ds"].tolist()
    ts_arr = g["ts"].tolist()
    d0_arr = [float(x) for x in g["d0"].tolist()]
    vs_arr = g["vs"].tolist() if "vs" in g.columns else None
    e = g["event"].to_numpy(dtype=float)
    prior_log_a = float(np.log(max(float(global_a), EPS)))
    prior_log_c = float(np.log(max(float(global_c), EPS)))
    x0 = np.array([prior_log_a, prior_log_c], dtype=float)
    bounds = [(np.log(1e-6), np.log(50.0)), (np.log(1e-6), np.log(300.0))]
    # β_vol 已由标量 MLE 全局拟合，前缀拟合时固定
    res = minimize(
        _neg_ll_bucket,
        x0,
        args=(ds_arr, ts_arr, d0_arr, e, float(gamma), float(beta), prior_log_a, prior_log_c, float(shrink_l2), 0.0, vs_arr),
        method="L-BFGS-B",
        bounds=bounds,
    )
    log_a, log_c = res.x
    ll_pen = -float(res.fun)
    return PrefixBucketParams(
        prefix=str(prefix),
        n_windows=n,
        n_events=int(np.sum(e)),
        event_rate=float(np.mean(e)),
        a=float(np.exp(log_a)),
        c=float(np.exp(log_c)),
        gamma=float(gamma),
        beta=float(beta),
        log_likelihood=ll_pen,
        success=bool(res.success),
        message=str(res.message),
        shrink_l2=float(shrink_l2),
    )


def fit_prefix_dynamic_model(
    samples: list[WindowSample],
    *,
    gamma: float,
    beta: float,
    global_a: float,
    global_c: float,
    symbol: str = "BTCUSDT",
    min_n: int = 100,
    shrink_l2: float = 2.0,
) -> tuple[PrefixDynamicModel, pd.DataFrame]:
    agg = build_prefix_survival_agg(samples, prefix_lens=(3, 4))
    buckets: dict[str, PrefixBucketParams] = {}
    if not agg.empty:
        for prefix in sorted(str(x) for x in agg["prefix"].unique()):
            bp = fit_prefix_bucket_dynamic(
                agg,
                prefix=prefix,
                gamma=float(gamma),
                beta=float(beta),
                global_a=float(global_a),
                global_c=float(global_c),
                min_n=int(min_n),
                shrink_l2=float(shrink_l2),
            )
            if bp is not None:
                buckets[prefix] = bp
    model = PrefixDynamicModel(
        gamma=float(gamma),
        beta=float(beta),
        dynamic_a=float(global_a),
        dynamic_c=float(global_c),
        symbol=str(symbol),
        buckets=buckets,
    )
    return model, agg
