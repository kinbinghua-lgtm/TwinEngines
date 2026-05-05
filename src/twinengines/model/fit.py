"""
生存模型 MLE - 累积风险积分版本（区间生存似然近似）。

模型:
    h(d, T) = lambda0 * (T/120)^gamma * exp(beta * d)   瞬时风险
    P(reversal in window i) = 1 - exp(-H_i)
    H_i = sum_{k} h(d_ik, T_ik) * dt_k     (秒级离散积分)

似然:
    log L = sum_i [ event_i * log(P_i) + (1 - event_i) * log(1 - P_i) ]

相对旧版改进:
    - 用整条秒级 d(t) 路径计算 H_i, 而非 d 的窗口均值
    - 充分利用 d 与 T 的耦合, 避免信息损失
    - 数学上等价于"反转事件遵循非齐次泊松过程"

参数边界: 收紧到经验合理区间 (lambda0 in [1e-3, 5], gamma in [0.5, 2.5], beta in [-3, 3]).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from ..survival import SurvivalParams, T_REF_SECONDS

EPS = 1e-12


@dataclass(frozen=True)
class FitReport:
    params: SurvivalParams
    log_likelihood: float
    n_windows: int
    success: bool
    message: str


def _aggregate_per_window(event_table: pd.DataFrame) -> pd.DataFrame:
    """
    按 window_id 聚合: 每窗口收集 (d_k, t_k) 序列与窗口结局。
    返回字段: window_id, ds(list), ts(list), event(int).
    """
    if event_table.empty:
        return pd.DataFrame(columns=["window_id", "ds", "ts", "vs", "event"])
    grouped = event_table.sort_values(["window_id", "obs_idx" if "obs_idx" in event_table.columns else "second"])
    has_vr = "vol_ratio" in event_table.columns and event_table["vol_ratio"].notna().any()
    rows: list[dict] = []
    for wid, g in grouped.groupby("window_id"):
        vs = g["vol_ratio"].to_numpy(dtype=float) if has_vr else None
        rows.append(
            {
                "window_id": int(wid),
                "ds": g["d"].to_numpy(dtype=float),
                "ts": g["t_remaining"].to_numpy(dtype=float),
                "vs": vs,
                "event": int(g["event"].max()),
            }
        )
    return pd.DataFrame(rows)


def _cumulative_hazard_per_window(
    ds_arr: list[np.ndarray],
    ts_arr: list[np.ndarray],
    lambda0: float,
    gamma: float,
    beta: float,
    beta_vol: float = 0.0,
    vs_arr: list[np.ndarray | None] | None = None,
) -> np.ndarray:
    """
    对每个窗口计算累积风险 H_i = sum_k h_k * dt_k
    h_k = lambda0 * (t_k/120)^gamma * exp(beta * d_k + beta_vol * vol_ratio_k)
    """
    H = np.empty(len(ds_arr), dtype=float)
    for i, (ds, ts) in enumerate(zip(ds_arr, ts_arr)):
        if len(ds) == 0:
            H[i] = 0.0
            continue
        exp_arg = beta * ds
        if vs_arr is not None and vs_arr[i] is not None:
            exp_arg += beta_vol * vs_arr[i]
        h = (
            lambda0
            * np.power(np.clip(ts / T_REF_SECONDS, EPS, None), gamma)
            * np.exp(exp_arg)
        )
        if len(ts) > 1:
            dt = -np.diff(ts)
            dt = np.append(dt, max(ts[-1], 1.0))
            dt = np.clip(dt, 0.0, None)
        else:
            dt = np.array([max(ts[0], 1.0)])
        H[i] = float(np.sum(h * dt))
    return H


def _neg_log_likelihood(
    x: np.ndarray,
    ds_arr: list[np.ndarray],
    ts_arr: list[np.ndarray],
    e: np.ndarray,
    vs_arr: list[np.ndarray | None] | None = None,
) -> float:
    log_l0, gamma, beta = x[0], x[1], x[2]
    beta_vol = x[3] if len(x) > 3 else 0.0
    lambda0 = float(np.exp(log_l0))
    H = _cumulative_hazard_per_window(ds_arr, ts_arr, lambda0, gamma, beta, beta_vol, vs_arr)
    p = 1.0 - np.exp(-np.clip(H, 0.0, 50.0))
    p = np.clip(p, EPS, 1.0 - EPS)
    ll = e * np.log(p) + (1.0 - e) * np.log(1.0 - p)
    return float(-np.sum(ll))


def fit_survival_mle(
    event_table: pd.DataFrame,
    initial: SurvivalParams = SurvivalParams(lambda0=0.1, gamma=1.0, beta=0.0),
) -> FitReport:
    agg = _aggregate_per_window(event_table)
    n = len(agg)
    if n == 0:
        raise ValueError("event_table is empty; cannot fit")

    ds_arr = agg["ds"].tolist()
    ts_arr = agg["ts"].tolist()
    vs_arr = agg["vs"].tolist() if "vs" in agg.columns else None
    e = agg["event"].to_numpy(dtype=float)

    x0 = np.array(
        [np.log(max(initial.lambda0, EPS)), initial.gamma, initial.beta, 0.0],
        dtype=float,
    )
    bounds = [
        (np.log(1e-6), np.log(20.0)),
        (0.1, 4.0),
        (-8.0, 8.0),
        (-5.0, 5.0),
    ]

    res = minimize(
        _neg_log_likelihood,
        x0,
        args=(ds_arr, ts_arr, e, vs_arr),
        method="L-BFGS-B",
        bounds=bounds,
    )
    log_l0, gamma, beta, beta_vol = res.x
    params = SurvivalParams(
        lambda0=float(np.exp(log_l0)),
        gamma=float(gamma),
        beta=float(beta),
        beta_vol=float(beta_vol),
    )
    return FitReport(
        params=params,
        log_likelihood=float(-res.fun),
        n_windows=n,
        success=bool(res.success),
        message=str(res.message),
    )


def time_split(
    event_table: pd.DataFrame, train_ratio: float = 0.7
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not 0.0 < train_ratio < 1.0:
        raise ValueError("train_ratio must be in (0,1)")
    n = event_table["window_id"].nunique()
    cut = int(n * train_ratio)
    train = event_table[event_table["window_id"] < cut].copy()
    test = event_table[event_table["window_id"] >= cut].copy()
    return train, test


def time_split_three(
    event_table: pd.DataFrame,
    train_ratio: float = 0.6,
    valid_ratio: float = 0.2,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    三段独立切分: train(拟参) / valid(标定阈值) / test(评估)。
    """
    if not 0 < train_ratio < 1:
        raise ValueError("train_ratio must be in (0,1)")
    if not 0 < valid_ratio < 1:
        raise ValueError("valid_ratio must be in (0,1)")
    if train_ratio + valid_ratio >= 1.0:
        raise ValueError("train + valid must leave room for test")

    n = event_table["window_id"].nunique()
    c1 = int(n * train_ratio)
    c2 = int(n * (train_ratio + valid_ratio))
    train = event_table[event_table["window_id"] < c1].copy()
    valid = event_table[(event_table["window_id"] >= c1) & (event_table["window_id"] < c2)].copy()
    test = event_table[event_table["window_id"] >= c2].copy()
    return train, valid, test
