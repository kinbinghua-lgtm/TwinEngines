#!/usr/bin/env python3
"""
批次修改: 给生存模型添加 β_vol × vol_ratio 项。
修改3个文件: data/window.py, model/fit.py, model/survival_ablation.py
"""

import os, sys
from pathlib import Path

ROOT = Path("E:/TwinEngines/src/twinengines")
sys.path.insert(0, str(ROOT.parent))

# ════════════════════════════════════════════════════
# 1. data/window.py — add vol_ratio to WindowSample + builder
# ════════════════════════════════════════════════════

wp = ROOT / "data" / "window.py"
old_wp = wp.read_text(encoding="utf-8")

# 1a. Add vol_ratio_per_obs to WindowSample dataclass
old_window_sample = """    close_m5: float = 0.0


    def _touched_reversal_in_window"""
new_window_sample = """    close_m5: float = 0.0
    vol_ratio_per_obs: np.ndarray | None = None  # σ_rev/σ_fwd per obs (600s rolling)

    def _touched_reversal_in_window"""

old_wp = old_wp.replace(old_window_sample, new_window_sample)

# 1b. Add vol_ratio helper function before build_windows
old_build_windows = """def build_windows("""
vol_helper = """VOL_RATIO_WINDOW_SEC: int = 600


def _compute_vol_ratio_per_obs(
    obs_times_ms: np.ndarray,
    raw_1s_times_ms: np.ndarray,
    raw_1s_closes: np.ndarray,
    trend_up: bool,
    window_sec: int = VOL_RATIO_WINDOW_SEC,
) -> np.ndarray:
    '''对每个观测点, 计算前 window_sec 秒的反向/正向波动率比值。'''
    ratios = np.full(len(obs_times_ms), 1.0, dtype=float)
    if len(raw_1s_closes) < 10:
        return ratios
    ret = np.diff(raw_1s_closes)
    ret_t = raw_1s_times_ms[1:]
    ret = ret.astype(np.float64)
    ret[ret == 0] = 1e-12
    # 按正负分正向/反向
    for i, t_obs in enumerate(obs_times_ms):
        t_start = int(t_obs) - window_sec * 1000
        mask = (ret_t >= t_start) & (ret_t < t_obs)
        r = ret[mask]
        if len(r) < 10:
            continue
        if trend_up:
            fwd = r[r > 0]
            bwd = r[r < 0]
        else:
            fwd = r[r < 0]
            bwd = r[r > 0]
        af = np.abs(fwd)
        ab = np.abs(bwd)
        sf = float(np.std(af)) if len(af) > 1 else 0.0
        sb = float(np.std(ab)) if len(ab) > 1 else 0.0
        ratios[i] = float(np.clip(sb / max(sf, 1e-10), 0.1, 10.0))
    return ratios


def build_windows("""

old_wp = old_wp.replace(old_build_windows, vol_helper)

# 1c. In build_windows, after resample but before WindowSample creation, compute vol_ratio
# The section: after resample + d/t computation, before samples.append
old_after_resample = """        d = np.abs(prices - baseline) / baseline * 100.0
        elapsed_sec = (times_ms - obs_start_ms) / MS_PER_SEC
        t_remaining = obs_duration_sec - elapsed_sec
        t_remaining = np.clip(t_remaining, 0.0, obs_duration_sec)

        if trig == "111":
            reversed_ = closes[4] <= baseline
        else:
            reversed_ = closes[4] > baseline

        samples.append("""
new_after_resample = """        d = np.abs(prices - baseline) / baseline * 100.0
        elapsed_sec = (times_ms - obs_start_ms) / MS_PER_SEC
        t_remaining = obs_duration_sec - elapsed_sec
        t_remaining = np.clip(t_remaining, 0.0, obs_duration_sec)

        # 计算 vol_ratio（仅当 1s 原始数据可用时）
        vol_ratio_per_obs = None
        if df_1s is not None and ts_1s_sorted is not None and len(times_ms) > 0:
            vr_start_ms = max(int(times_ms[0]) - VOL_RATIO_WINDOW_SEC * 1000, int(df_1s["open_time"].iloc[0]))
            vr_block = _select_window_seconds_slice(df_1s, ts_1s_sorted, vr_start_ms, int(times_ms[-1]))
            if len(vr_block) > 10:
                vp = vr_block["close"].to_numpy(dtype=np.float64)
                vt = vr_block["open_time"].to_numpy(dtype=np.int64)
                vol_ratio_per_obs = _compute_vol_ratio_per_obs(
                    times_ms, vt, vp,
                    trend_up=(trig == "111"),
                )

        if trig == "111":
            reversed_ = closes[4] <= baseline
        else:
            reversed_ = closes[4] > baseline

        samples.append("""

old_wp = old_wp.replace(old_after_resample, new_after_resample)

# 1d. In WindowSample constructor call, add vol_ratio_per_obs
old_ws_call = """                d0_abs_pct=float(d0_abs_pct),
                obs_duration_sec=float(obs_duration_sec),
                obs_start_offset_minutes=int(obs_start_offset_minutes),
                close_m5=float(closes[4]),
            )"""
new_ws_call = """                d0_abs_pct=float(d0_abs_pct),
                obs_duration_sec=float(obs_duration_sec),
                obs_start_offset_minutes=int(obs_start_offset_minutes),
                close_m5=float(closes[4]),
                vol_ratio_per_obs=vol_ratio_per_obs,
            )"""
old_wp = old_wp.replace(old_ws_call, new_ws_call)

# 1e. In windows_to_event_table, add vol_ratio to output rows
old_et_row = """                {
                    "window_id": wid,"""
new_et_row = """                vr = None
                if hasattr(s, "vol_ratio_per_obs") and s.vol_ratio_per_obs is not None and k < len(s.vol_ratio_per_obs):
                    vr = float(s.vol_ratio_per_obs[k])
                rows.append(
                    {
                        "window_id": wid,"""
old_wp = old_wp.replace(old_et_row, new_et_row)

# Add vol_ratio field to each row
old_et_fields = """                    "d0_abs_pct": float(s.d0_abs_pct),
                }
            )
        """
new_et_fields = """                    "d0_abs_pct": float(s.d0_abs_pct),
                    "vol_ratio": vr,
                }
            )
        """
old_wp = old_wp.replace(old_et_fields, new_et_fields)

# Update empty DataFrame columns
old_empty = """        return pd.DataFrame(
            columns=[
                "window_id",
                "obs_idx",
                "d",
                "t_remaining",
                "elapsed_from_obs_start_sec",
                "obs_duration_sec",
                "event",
                "trigger",
                "d0_abs_pct",
            ]
        )"""
new_empty = """        return pd.DataFrame(
            columns=[
                "window_id",
                "obs_idx",
                "d",
                "t_remaining",
                "elapsed_from_obs_start_sec",
                "obs_duration_sec",
                "event",
                "trigger",
                "d0_abs_pct",
                "vol_ratio",
            ]
        )"""
old_wp = old_wp.replace(old_empty, new_empty)

wp.write_text(old_wp, encoding="utf-8")
print("1. data/window.py updated")

# ════════════════════════════════════════════════════
# 2. model/fit.py — add beta_vol to MLE
# ════════════════════════════════════════════════════

fp = ROOT / "model" / "fit.py"
old_fp = fp.read_text(encoding="utf-8")

# 2a. Update docstring
old_fp = old_fp.replace(
    "lambda0 in [1e-3, 5], gamma in [0.5, 2.5], beta in [-3, 3]",
    "lambda0 in [1e-6, 20], gamma in [0.1, 4], beta in [-8, 8], beta_vol in [-5, 5]"
)

# 2b. _aggregate_per_window: add vol_ratio arrays
old_agg1 = """    if event_table.empty:
        return pd.DataFrame(columns=["window_id", "ds", "ts", "event"])
    grouped = event_table.sort_values(["window_id", "obs_idx" if "obs_idx" in event_table.columns else "second"])
    rows: list[dict] = []
    for wid, g in grouped.groupby("window_id"):
        rows.append(
            {
                "window_id": int(wid),
                "ds": g["d"].to_numpy(dtype=float),
                "ts": g["t_remaining"].to_numpy(dtype=float),
                "event": int(g["event"].max()),
            }
        )
    return pd.DataFrame(rows)"""
new_agg1 = """    if event_table.empty:
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
    return pd.DataFrame(rows)"""
old_fp = old_fp.replace(old_agg1, new_agg1)

# 2c. _cumulative_hazard_per_window: add vs_arr + beta_vol
old_hazard = """def _cumulative_hazard_per_window(
    ds_arr: list[np.ndarray],
    ts_arr: list[np.ndarray],
    lambda0: float,
    gamma: float,
    beta: float,
) -> np.ndarray:
    """
    对每个窗口计算累积风险 H_i = sum_k h_k * dt_k
    dt_k 由相邻 ts 的差近似（最后一段补齐到结尾）。
    """
    H = np.empty(len(ds_arr), dtype=float)
    for i, (ds, ts) in enumerate(zip(ds_arr, ts_arr)):
        if len(ds) == 0:
            H[i] = 0.0
            continue
        h = (
            lambda0
            * np.power(np.clip(ts / T_REF_SECONDS, EPS, None), gamma)
            * np.exp(beta * ds)
        )
        if len(ts) > 1:
            dt = -np.diff(ts)
            dt = np.append(dt, max(ts[-1], 1.0))
            dt = np.clip(dt, 0.0, None)
        else:
            dt = np.array([max(ts[0], 1.0)])
        H[i] = float(np.sum(h * dt))
    return H"""
new_hazard = """def _cumulative_hazard_per_window(
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
    return H"""
old_fp = old_fp.replace(old_hazard, new_hazard)

# 2d. _neg_log_likelihood: add vs_arr + beta_vol
old_nll = """def _neg_log_likelihood(
    x: np.ndarray,
    ds_arr: list[np.ndarray],
    ts_arr: list[np.ndarray],
    e: np.ndarray,
) -> float:
    log_l0, gamma, beta = x
    lambda0 = float(np.exp(log_l0))
    H = _cumulative_hazard_per_window(ds_arr, ts_arr, lambda0, gamma, beta)"""
new_nll = """def _neg_log_likelihood(
    x: np.ndarray,
    ds_arr: list[np.ndarray],
    ts_arr: list[np.ndarray],
    e: np.ndarray,
    vs_arr: list[np.ndarray | None] | None = None,
) -> float:
    log_l0, gamma, beta, beta_vol = x[0], x[1], x[2], x[3] if len(x) > 3 else 0.0
    lambda0 = float(np.exp(log_l0))
    H = _cumulative_hazard_per_window(ds_arr, ts_arr, lambda0, gamma, beta, beta_vol, vs_arr)"""
old_fp = old_fp.replace(old_nll, new_nll)

# 2e. fit_survival_mle: extract vs_arr, add beta_vol to x0, bounds
old_fit = """    ds_arr = agg["ds"].tolist()
    ts_arr = agg["ts"].tolist()
    e = agg["event"].to_numpy(dtype=float)

    x0 = np.array([np.log(max(initial.lambda0, EPS)), initial.gamma, initial.beta], dtype=float)
    bounds = [
        (np.log(1e-6), np.log(20.0)),
        (0.1, 4.0),
        (-8.0, 8.0),
    ]

    res = minimize(
        _neg_log_likelihood,
        x0,
        args=(ds_arr, ts_arr, e),
        method="L-BFGS-B",
        bounds=bounds,
    )
    log_l0, gamma, beta = res.x
    params = SurvivalParams(
        lambda0=float(np.exp(log_l0)),
        gamma=float(gamma),
        beta=float(beta),
    )"""
new_fit = """    ds_arr = agg["ds"].tolist()
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
        _has_beta_vol=True,
    )"""
old_fp = old_fp.replace(old_fit, new_fit)

# 2f. SurvivalParams needs beta_vol. Let's modify survival.py too
# For backwards compat, add beta_vol with default 0

sp.write_text(...)  # handled below

fp.write_text(old_fp, encoding="utf-8")
print("2. model/fit.py updated")

# ════════════════════════════════════════════════════
# 3. model/survival_ablation.py — add beta_vol to all hazard functions
# ════════════════════════════════════════════════════

ap = ROOT / "model" / "survival_ablation.py"
old_ap = ap.read_text(encoding="utf-8")

# 3a. _hazard_rows
old_ap = old_ap.replace(
    """def _hazard_rows(
    ds: np.ndarray,
    ts: np.ndarray,
    *,
    lambda0: float,
    gamma: float,
    beta: float,
) -> np.ndarray:
    return (
        float(lambda0)
        * np.power(np.clip(ts / T_REF_SECONDS, EPS, None), float(gamma))
        * np.exp(float(beta) * ds)
    )""",
    """def _hazard_rows(
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
    )"""
)

# 3b. _window_H_scalar_l0
old_ap = old_ap.replace(
    """def _window_H_scalar_l0(
    ds: np.ndarray,
    ts: np.ndarray,
    *,
    lambda0: float,
    gamma: float,
    beta: float,
) -> float:
    if len(ds) == 0:
        return 0.0
    h = _hazard_rows(ds, ts, lambda0=lambda0, gamma=gamma, beta=beta)""",
    """def _window_H_scalar_l0(
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
    h = _hazard_rows(ds, ts, lambda0=lambda0, gamma=gamma, beta=beta, beta_vol=beta_vol, vs=vs)"""
)

# 3c. _window_H_dynamic_l0 — pass through beta_vol + vs
old_ap = old_ap.replace(
    """def _window_H_dynamic_l0(
    ds: np.ndarray,
    ts: np.ndarray,
    *,
    d0: float,
    a: float,
    c: float,
    gamma: float,
    beta: float,
) -> float:
    l0 = float(a) / (1.0 + float(c) * max(float(d0), 0.0))
    return _window_H_scalar_l0(ds, ts, lambda0=l0, gamma=gamma, beta=beta)""",
    """def _window_H_dynamic_l0(
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
    return _window_H_scalar_l0(ds, ts, lambda0=l0, gamma=gamma, beta=beta, beta_vol=beta_vol, vs=vs)"""
)

ap.write_text(old_ap, encoding="utf-8")
print("3. model/survival_ablation.py updated")

print()
print("All done. Remember to:")
print("  - Update survival.py to add beta_vol to SurvivalParams")
print("  - Update prefix_survival.py for vol_ratio")
print("  - Update cli.py training commands")
