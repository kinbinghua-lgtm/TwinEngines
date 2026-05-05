"""
秒级因果动态预测（Binance 1s 已收盘 K 线），与离线 ``obs_step_sec=1`` 路径语义对齐。

积木：
  - ``fetch_binance_1s_obs_segment``：拉取观测段内已收盘 1s K；
  - ``build_ds_ts_from_sec_knots``：构造 ``_window_H_dynamic_l0`` 所需的递减 ``ts``；
  - ``naked_predict_live_sec_dynamic``：输出与 ``naked_predict_live_dynamic`` 相同核心字段，供 runner 复用。
"""

from __future__ import annotations

import json
from typing import Any

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np

from ..model.prefix_survival import PrefixDynamicModel
from ..model.survival_ablation import _window_H_dynamic_l0
from ..model.survival_survival_validation import reversal_prob_from_H
from .third_digit_naked import (
    EPS_TS_SEC,
    ThirdDigitDynamicParams,
    _dynamic_params_for_prefix,
    directional_from_pr_rev,
    residual_seconds_to_end,
    third_digit_trigger_and_d0,
)

MS_PER_MIN = 60_000


def fetch_binance_1s_obs_segment(
    *,
    symbol: str,
    obs_start_ms: int,
    now_ms: int,
    timeout_sec: float = 12.0,
    limit: int = 1000,
) -> list[list[Any]] | None:
    """
    观测段 ``[obs_start_ms, now_ms]`` 内的 1s K 线（Binance REST）。
    调用方应用 ``close_time <= now_ms`` 过滤未收盘根。
    """
    sym = str(symbol).upper()
    lim = int(min(1000, max(10, limit)))
    url = (
        "https://api.binance.com/api/v3/klines"
        f"?symbol={sym}&interval=1s&startTime={int(obs_start_ms)}"
        f"&endTime={int(now_ms)}&limit={lim}"
    )
    req = Request(url, headers={"User-Agent": "TwinEngines-third-digit-sec-dynamic/1.0"})
    try:
        with urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read()
    except (URLError, HTTPError, TimeoutError, OSError):
        return None
    try:
        rows = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return None
    if not rows:
        return None
    return rows


def _merge_knots_and_stride(
    knots: list[tuple[int, float]],
    *,
    stride_sec: int,
) -> list[tuple[int, float]]:
    if stride_sec <= 1:
        return knots
    st = int(stride_sec)
    if len(knots) <= 2:
        return knots
    idx = list(range(0, len(knots), st))
    if idx[-1] != len(knots) - 1:
        idx.append(len(knots) - 1)
    out = [knots[i] for i in idx]
    merged: list[tuple[int, float]] = []
    for t, d in out:
        if merged and merged[-1][0] == t:
            merged[-1] = (t, d)
        else:
            merged.append((t, d))
    return merged


def build_ds_ts_from_sec_knots(
    *,
    knots_ms_d: list[tuple[int, float]],
    end_ts_ms: int,
    now_ms: int,
) -> tuple[np.ndarray, np.ndarray] | None:
    """
    由 (事件时间 ms, d%) 序列构造 ``ds`` / ``ts``（``ts`` = 该时刻相对结算剩余秒，递减）。
    在末尾冻结当前 ``d`` 并接到 ``now_ms`` 的剩余 horizon。
    """
    if not knots_ms_d:
        return None
    R_now = max(EPS_TS_SEC, residual_seconds_to_end(end_ts_ms=end_ts_ms, at_ms=int(now_ms)))
    ds_list: list[float] = []
    ts_list: list[float] = []
    for t_ms, d in knots_ms_d:
        tr = max(EPS_TS_SEC, residual_seconds_to_end(end_ts_ms=end_ts_ms, at_ms=int(t_ms)))
        ds_list.append(float(d))
        ts_list.append(float(tr))
    if abs(ts_list[-1] - R_now) > 1e-5:
        ds_list.append(ds_list[-1])
        ts_list.append(float(R_now))
    ds = np.asarray(ds_list, dtype=float)
    ts = np.asarray(ts_list, dtype=float)
    if np.any(np.diff(ts) > 1e-9):
        return None
    return ds, ts


def naked_predict_live_sec_dynamic(
    *,
    baseline: float,
    closes_first_three: list[float],
    window_start_ms: int,
    end_ts_ms: int,
    now_ms: int,
    params: ThirdDigitDynamicParams | PrefixDynamicModel,
    sec_path_stride: int = 1,
) -> dict[str, Any] | None:
    """
    用观测起点（第 3 分钟末）的 minute close 锚定首结点 + 之后 **已收盘** 1s 收盘价更新 ``d``，
    剩余时间随墙钟缩短；与训练侧秒级离散 hazard 一致（同一 ``_window_H_dynamic_l0``）。
    """
    if len(closes_first_three) < 3 or baseline <= 0:
        return None
    obs_start_ms = int(window_start_ms) + 3 * MS_PER_MIN
    if int(now_ms) < obs_start_ms:
        return None

    trig, d0 = third_digit_trigger_and_d0(baseline=baseline, closes_first_three=closes_first_three)
    current_prefix = "".join("1" if float(c) > float(baseline) else "0" for c in closes_first_three[:3])
    gamma, beta, dyn_a, dyn_c, bucket_prefix, bucket_payload = _dynamic_params_for_prefix(
        params,
        prefix=current_prefix,
    )
    symbol = str(getattr(params, "symbol", "BTCUSDT"))
    d3 = abs(float(closes_first_three[2]) - baseline) / baseline * 100.0

    knots: list[tuple[int, float]] = [(int(obs_start_ms), float(d3))]

    rows = fetch_binance_1s_obs_segment(
        symbol=symbol,
        obs_start_ms=obs_start_ms,
        now_ms=int(now_ms),
    )
    if rows is None:
        return None

    for row in rows:
        try:
            ot = int(row[0])
            ct = int(row[6])
            close_px = float(row[4])
        except (IndexError, TypeError, ValueError):
            continue
        if ot < obs_start_ms:
            continue
        if ct > int(now_ms):
            continue
        if not np.isfinite(close_px):
            continue
        d_i = abs(close_px - baseline) / baseline * 100.0
        knots.append((int(ct), float(d_i)))

    knots.sort(key=lambda x: x[0])
    slim: list[tuple[int, float]] = []
    for t, d in knots:
        if slim and slim[-1][0] == t:
            slim[-1] = (t, d)
        else:
            slim.append((t, d))

    slim = _merge_knots_and_stride(slim, stride_sec=max(1, int(sec_path_stride)))

    built = build_ds_ts_from_sec_knots(
        knots_ms_d=slim,
        end_ts_ms=int(end_ts_ms),
        now_ms=int(now_ms),
    )
    if built is None:
        return None
    ds, ts = built

    H = float(
        _window_H_dynamic_l0(
            ds,
            ts,
            d0=float(d0),
            a=dyn_a,
            c=dyn_c,
            gamma=gamma,
            beta=beta,
        )
    )
    pr = float(reversal_prob_from_H(np.array([H], dtype=float))[0])
    p_up, pred_up, conf = directional_from_pr_rev(pr, trig)

    res_plain = residual_seconds_to_end(end_ts_ms=int(end_ts_ms), at_ms=int(now_ms))
    return {
        "trigger": trig,
        "d0_abs_pct": float(d0),
        "p_rev": pr,
        "p_up": float(p_up),
        "p_down": float(1.0 - p_up),
        "pred_up": pred_up,
        "confidence_on_pick": float(conf),
        "H": H,
        "ds": ds.tolist(),
        "ts_sec": ts.tolist(),
        "dynamic_causal": True,
        "predict_engine": "sec_1s_binance",
        "residual_sec": float(res_plain),
        "obs_start_ms": int(obs_start_ms),
        "n_sec_knots": int(len(slim)),
        "sec_path_stride": int(max(1, sec_path_stride)),
        "path_stage": "sec_closed_1s_and_now",
        "current_prefix": current_prefix,
        "prefix_bucket_used": bucket_prefix,
        "prefix_bucket": bucket_payload,
    }
