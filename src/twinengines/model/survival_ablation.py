"""
生存模型对比实验：常数 λ₀（现行 MLE） vs 有理动态 λ₀(d₀)=a/(1+c·d₀)。

用于离线验证与报告；不改变实盘默认路径。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from ..data.window import WindowSample
from ..survival import T_REF_SECONDS, SurvivalParams
from .fit import EPS, _cumulative_hazard_per_window


def _hazard_rows(
    ds: np.ndarray,
    ts: np.ndarray,
    *,
    lambda0: float,
    gamma: float,
    beta: float,
    beta_vol: float = 0.0,
    vs: np.ndarray | None = None,
) -> np.ndarray:
    arg = float(beta) * ds
    if vs is not None:
        arg += float(beta_vol) * vs
    return (
        float(lambda0)
        * np.power(np.clip(ts / T_REF_SECONDS, EPS, None), float(gamma))
        * np.exp(arg)
    )


def _window_H_scalar_l0(
    ds: np.ndarray,
    ts: np.ndarray,
    *,
    lambda0: float,
    gamma: float,
    beta: float,
    beta_vol: float = 0.0,
    vs: np.ndarray | None = None,
) -> float:
    if len(ds) == 0:
        return 0.0
    h = _hazard_rows(ds, ts, lambda0=lambda0, gamma=gamma, beta=beta, beta_vol=beta_vol, vs=vs)
    if len(ts) > 1:
        dt = -np.diff(ts)
        dt = np.append(dt, max(float(ts[-1]), 1.0))
        dt = np.clip(dt, 0.0, None)
    else:
        dt = np.array([max(float(ts[0]), 1.0)])
    return float(np.sum(h * dt))


def _window_H_dynamic_l0(
    ds: np.ndarray,
    ts: np.ndarray,
    *,
    d0: float,
    a: float,
    c: float,
    gamma: float,
    beta: float,
    beta_vol: float = 0.0,
    vs: np.ndarray | None = None,
) -> float:
    l0 = float(a) / (1.0 + float(c) * max(float(d0), 0.0))
    return _window_H_scalar_l0(ds, ts, lambda0=l0, gamma=gamma, beta=beta, beta_vol=beta_vol, vs=vs)


def aggregate_event_table_with_d0(event_table: pd.DataFrame) -> pd.DataFrame:
    """按 window_id 聚合路径 + 第 3 分钟末 d0 + terminal 反转标签（event=1 窗末反转）+ trigger。"""
    return _aggregate_with_d0(event_table)


def _aggregate_with_d0(event_table: pd.DataFrame) -> pd.DataFrame:
    if event_table.empty:
        return pd.DataFrame(columns=["window_id", "ds", "ts", "d0", "event", "trigger"])
    if "d0_abs_pct" not in event_table.columns:
        raise ValueError("event_table must contain d0_abs_pct (from windows_to_event_table)")
    grouped = event_table.sort_values(["window_id", "obs_idx" if "obs_idx" in event_table.columns else "second"])
    rows: list[dict] = []
    for wid, g in grouped.groupby("window_id"):
        trig = ""
        if "trigger" in g.columns:
            trig = str(g["trigger"].iloc[0])
        rows.append(
            {
                "window_id": int(wid),
                "ds": g["d"].to_numpy(dtype=float),
                "ts": g["t_remaining"].to_numpy(dtype=float),
                "d0": float(g["d0_abs_pct"].iloc[0]),
                "vs": g["vol_ratio"].to_numpy(dtype=float) if "vol_ratio" in g.columns and g["vol_ratio"].notna().any() else None,
                "event": int(g["event"].max()),
                "trigger": trig,
            }
        )
    return pd.DataFrame(rows)


def _neg_ll_dynamic(
    x: np.ndarray,
    ds_arr: list,
    ts_arr: list,
    d0_arr: list,
    e: np.ndarray,
    gamma: float,
    beta: float,
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
    p = 1.0 - np.exp(-np.clip(H, 0.0, 50.0))
    p = np.clip(p, EPS, 1.0 - EPS)
    ll = e * np.log(p) + (1.0 - e) * np.log(1.0 - p)
    return float(-np.sum(ll))


@dataclass(frozen=True)
class DynamicL0FitReport:
    a: float
    c: float
    gamma: float
    beta: float
    log_likelihood: float
    n_windows: int
    success: bool
    message: str


def fit_survival_mle_dynamic_lambda0(
    event_table: pd.DataFrame,
    *,
    gamma: float,
    beta: float,
    initial_a: float | None = None,
    initial_c: float | None = None,
) -> DynamicL0FitReport:
    """
    在训练集上仅拟合 λ₀(d₀)=a/(1+c·d₀) 的 (a, c)；γ、β固定（通常取常数 λ₀ MLE 的估计），
    以贴近「只替换 λ₀、其余时间/偏离形状不变」的对比。
    """
    agg = _aggregate_with_d0(event_table)
    n = len(agg)
    if n == 0:
        raise ValueError("empty aggregate")
    ds_arr = agg["ds"].tolist()
    ts_arr = agg["ts"].tolist()
    d0_arr = agg["d0"].tolist()
    vs_arr = agg["vs"].tolist() if "vs" in agg.columns else None
    e = agg["event"].to_numpy(dtype=float)

    a0 = float(initial_a) if initial_a is not None else 0.1
    c0 = float(initial_c) if initial_c is not None else 0.05
    x0 = np.array([np.log(max(a0, EPS)), np.log(max(c0, EPS))], dtype=float)
    bounds = [
        (np.log(1e-6), np.log(50.0)),
        (np.log(1e-6), np.log(200.0)),
    ]
    # β_vol 在拟合动态 λ₀ 时固定为 0（β_vol 已由标量 MLE 学出）
    res = minimize(
        _neg_ll_dynamic,
        x0,
        args=(ds_arr, ts_arr, d0_arr, e, float(gamma), float(beta), 0.0, vs_arr),
        method="L-BFGS-B",
        bounds=bounds,
    )
    log_a, log_c = res.x
    return DynamicL0FitReport(
        a=float(np.exp(log_a)),
        c=float(np.exp(log_c)),
        gamma=float(gamma),
        beta=float(beta),
        log_likelihood=float(-res.fun),
        n_windows=n,
        success=bool(res.success),
        message=str(res.message),
    )


def loglik_scalar_l0_on_agg(agg: pd.DataFrame, params: SurvivalParams) -> float:
    """常数 λ₀ 模型在已聚合窗表上的对数似然。"""
    if agg.empty:
        return 0.0
    ds_arr = agg["ds"].tolist()
    ts_arr = agg["ts"].tolist()
    e = agg["event"].to_numpy(dtype=float)
    H = _cumulative_hazard_per_window(ds_arr, ts_arr, params.lambda0, params.gamma, params.beta)
    p = 1.0 - np.exp(-np.clip(H, 0.0, 50.0))
    p = np.clip(p, EPS, 1.0 - EPS)
    return float(np.sum(e * np.log(p) + (1.0 - e) * np.log(1.0 - p)))


def loglik_dynamic_on_agg(agg: pd.DataFrame, rep: DynamicL0FitReport) -> float:
    if agg.empty:
        return 0.0
    ds_arr = agg["ds"].tolist()
    ts_arr = agg["ts"].tolist()
    d0_arr = agg["d0"].tolist()
    e = agg["event"].to_numpy(dtype=float)
    H = np.array(
        [
            _window_H_dynamic_l0(ds, ts, d0=d0, a=rep.a, c=rep.c, gamma=rep.gamma, beta=rep.beta)
            for ds, ts, d0 in zip(ds_arr, ts_arr, d0_arr)
        ],
        dtype=float,
    )
    p = 1.0 - np.exp(-np.clip(H, 0.0, 50.0))
    p = np.clip(p, EPS, 1.0 - EPS)
    return float(np.sum(e * np.log(p) + (1.0 - e) * np.log(1.0 - p)))


def brier_scalar(agg: pd.DataFrame, params: SurvivalParams) -> float:
    if agg.empty:
        return 0.0
    ds_arr = agg["ds"].tolist()
    ts_arr = agg["ts"].tolist()
    e = agg["event"].to_numpy(dtype=float)
    H = _cumulative_hazard_per_window(ds_arr, ts_arr, params.lambda0, params.gamma, params.beta)
    p = 1.0 - np.exp(-np.clip(H, 0.0, 50.0))
    return float(np.mean((e - p) ** 2))


def brier_dynamic(agg: pd.DataFrame, rep: DynamicL0FitReport) -> float:
    if agg.empty:
        return 0.0
    ds_arr = agg["ds"].tolist()
    ts_arr = agg["ts"].tolist()
    d0_arr = agg["d0"].tolist()
    e = agg["event"].to_numpy(dtype=float)
    H = np.array(
        [
            _window_H_dynamic_l0(ds, ts, d0=d0, a=rep.a, c=rep.c, gamma=rep.gamma, beta=rep.beta)
            for ds, ts, d0 in zip(ds_arr, ts_arr, d0_arr)
        ],
        dtype=float,
    )
    p = 1.0 - np.exp(-np.clip(H, 0.0, 50.0))
    return float(np.mean((e - p) ** 2))


def event_table_from_samples(samples: list[WindowSample], *, label_mode: str = "terminal") -> pd.DataFrame:
    from ..data.window import windows_to_event_table

    return windows_to_event_table(samples, label_mode=label_mode)


def calendar_train_valid_holdout(
    samples: list[WindowSample],
    *,
    train_span_days: int,
    holdout_days: int,
    train_frac: float,
) -> tuple[list[WindowSample], list[WindowSample], list[WindowSample]]:
    """
    按 window_start_ms 排序后：
    - 前 train_span_days 天内的样本按时间顺序切 train_frac / (1-train_frac) 为 train / valid；
    - 接下来 holdout_days 天为 holdout（全新验证段）。
    """
    if not samples:
        return [], [], []
    if not 0.0 < train_frac < 1.0:
        raise ValueError("train_frac must be in (0,1)")
    ordered = sorted(samples, key=lambda s: int(s.window_start_ms))
    t0 = int(ordered[0].window_start_ms)
    train_end = t0 + int(train_span_days) * 86_400_000
    holdout_end = train_end + int(holdout_days) * 86_400_000
    pool = [s for s in ordered if int(s.window_start_ms) < train_end]
    hold = [s for s in ordered if train_end <= int(s.window_start_ms) < holdout_end]
    n = len(pool)
    if n == 0:
        return [], [], hold
    cut = max(1, min(n - 1, int(n * train_frac)))
    train = pool[:cut]
    valid = pool[cut:]
    return train, valid, hold


def summarize_counts(train: list, valid: list, hold: list) -> dict[str, int]:
    return {"n_train_windows": len(train), "n_valid_windows": len(valid), "n_holdout_windows": len(hold)}
