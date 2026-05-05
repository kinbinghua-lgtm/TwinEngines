"""
5 分钟窗口构建（重构版）。

核心设计:
    - 直接消费币安真实数据：1m klines (基准+触发) + 1s klines (秒级路径)
    - 完全废弃合成插值 (旧版 _interp_seconds)
    - 观测步长可配置 (默认 10 秒): 在 1s klines 之上做下采样
    - 仍严格遵守需求文档第七节: 第 3 分钟末为固定起点, 不倒推

输入约定:
    bars_1m: DataFrame[open_time(ms), open, high, low, close, ...]
    bars_1s: DataFrame[open_time(ms), open, high, low, close, ...] (可选)
            如缺失, 退化为分钟级近似 (warning)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from ..sequence import build_fixed_baseline_sequence, extract_trigger, is_target_trigger

TriggerMode = Literal["111_000", "third_digit", "first_digit"]

WINDOW_MINUTES: int = 5
OBS_DURATION_SEC: int = 120  # 第3分钟末 -> 第5分钟末
DEFAULT_OBS_STEP_SEC: int = 10
MS_PER_SEC: int = 1000
MS_PER_MIN: int = 60 * MS_PER_SEC
MS_PER_WINDOW: int = WINDOW_MINUTES * MS_PER_MIN


def _detect_time_unit(open_time_series: pd.Series) -> str:
    """
    检测 open_time 单位: ms / us / ns。

    判断依据: 当 1m K 线相邻 open_time 差约为 60_000 时单位为 ms,
    60_000_000 为 us, 60_000_000_000 为 ns。
    """
    if len(open_time_series) < 2:
        return "ms"
    diff = int(open_time_series.iloc[1]) - int(open_time_series.iloc[0])
    if diff <= 0:
        return "ms"
    if 30_000 <= diff <= 120_000:
        return "ms"
    if 30_000_000 <= diff <= 120_000_000:
        return "us"
    if 30_000_000_000 <= diff <= 120_000_000_000:
        return "ns"
    sample_val = int(open_time_series.iloc[0])
    if sample_val < 10**13:
        return "ms"
    if sample_val < 10**16:
        return "us"
    return "ns"


def normalize_to_ms(df: pd.DataFrame) -> pd.DataFrame:
    """
    将 open_time / close_time (若存在) 统一归一化为毫秒。
    Binance 自 2025 年中起对部分接口返回微秒单位, 此函数兼容历史数据。
    """
    if "open_time" not in df.columns:
        return df
    unit = _detect_time_unit(df["open_time"])
    if unit == "ms":
        return df
    factor = 1_000 if unit == "us" else 1_000_000
    out = df.copy()
    out["open_time"] = (out["open_time"] // factor).astype("int64")
    if "close_time" in out.columns:
        out["close_time"] = (out["close_time"] // factor).astype("int64")
    return out


@dataclass(frozen=True)
class WindowSample:
    """
    一个命中 111/000 触发的 5 分钟窗口样本。

    fields:
        window_start_ms: 第1分钟 open_time
        baseline: 第1分钟 open
        trigger: "111" or "000"
        sequence: 完整 5 位序列
        obs_times_ms: 观测时间戳 (绝对 ms), 默认起于第3分钟末；obs_start_offset_minutes=1 时起于第1分钟末
        obs_prices: 对应观测价格 (来自 1s klines close, 或回退到分钟线)
        d_per_obs: 对应每个观测点的 |相对偏离%|
        t_per_obs: 对应每个观测点的剩余秒数 (>= 0)
        reversed: 第5分钟收盘是否相对基准反转
        observed_from: "1s" or "1m" 标记数据来源
        close_m5: 第 5 分钟收盘价（用于跨窗标签：下一窗涨跌相对下一窗开盘价）
    """

    window_start_ms: int
    baseline: float
    trigger: str
    sequence: str
    obs_times_ms: np.ndarray
    obs_prices: np.ndarray
    d_per_obs: np.ndarray
    t_per_obs: np.ndarray
    reversed: bool
    observed_from: str
    d0_abs_pct: float = 0.0  # 动态 λ0 锚定偏离：默认第 3 分钟末；obs_start_offset_minutes=1 时为第 1 分钟末
    obs_duration_sec: float = float(OBS_DURATION_SEC)  # 观测段长度（秒），用于 t_remaining 上界
    obs_start_offset_minutes: int = 3  # 观测起点：1=第 1 分钟末起，3=第 3 分钟末起（现行）
    close_m5: float = 0.0
    vol_ratio_per_obs: np.ndarray | None = None  # σ_rev/σ_fwd per obs (600s rolling)


def _select_window_minutes(df_1m: pd.DataFrame, start_ms: int) -> pd.DataFrame:
    end_ms = start_ms + MS_PER_WINDOW
    return df_1m[(df_1m["open_time"] >= start_ms) & (df_1m["open_time"] < end_ms)]


def _select_window_seconds(df_1s: pd.DataFrame, obs_start_ms: int, obs_end_ms: int) -> pd.DataFrame:
    """obs 起点 = 第3分钟末, 终点 = 第5分钟末 (含)。"""
    return df_1s[(df_1s["open_time"] >= obs_start_ms) & (df_1s["open_time"] <= obs_end_ms)]


def _select_window_seconds_slice(
    df_1s: pd.DataFrame,
    ts_sorted: np.ndarray,
    obs_start_ms: int,
    obs_end_ms: int,
) -> pd.DataFrame:
    """在已按 open_time 升序的 1s 表上用 searchsorted 切片，避免每窗全表扫描。"""
    i0 = int(np.searchsorted(ts_sorted, obs_start_ms, side="left"))
    i1 = int(np.searchsorted(ts_sorted, obs_end_ms, side="right"))
    return df_1s.iloc[i0:i1]


def _resample_to_step(prices: np.ndarray, times_ms: np.ndarray, step_sec: int) -> tuple[np.ndarray, np.ndarray]:
    """
    在已有 1 秒 close 序列上做下采样: 取每 step_sec 的最新一个观测点。
    """
    if step_sec <= 1:
        return times_ms, prices
    keep = np.arange(0, len(times_ms), step_sec)
    return times_ms[keep], prices[keep]


def _interp_prices_1m_from_obs_start(
    times_ms: np.ndarray,
    *,
    window_start_ms: int,
    obs_start_ms: int,
    closes: list[float],
) -> np.ndarray:
    """在 [obs_start_ms, 第5分钟末] 上按分钟收盘分段线性插值（closes[0]..[4] 为各分钟收盘）。"""
    w = int(window_start_ms)
    o = int(obs_start_ms)
    c = [float(x) for x in closes]
    prices = np.empty(len(times_ms), dtype=float)
    b2, b3, b4, b5 = w + 2 * MS_PER_MIN, w + 3 * MS_PER_MIN, w + 4 * MS_PER_MIN, w + 5 * MS_PER_MIN
    for i, t in enumerate(times_ms):
        t = int(t)
        if t < b2:
            den = max(float(b2 - o), 1.0)
            u = (float(t - o)) / den
            prices[i] = c[0] + u * (c[1] - c[0])
        elif t < b3:
            u = (float(t - b2)) / float(MS_PER_MIN)
            prices[i] = c[1] + u * (c[2] - c[1])
        elif t < b4:
            u = (float(t - b3)) / float(MS_PER_MIN)
            prices[i] = c[2] + u * (c[3] - c[2])
        else:
            u = (float(t - b4)) / float(MS_PER_MIN)
            prices[i] = c[3] + u * (c[4] - c[3])
    return prices



VOL_RATIO_WINDOW_SEC: int = 600


def _compute_vol_ratio_per_obs(
    obs_times_ms: np.ndarray,
    raw_1s_times_ms: np.ndarray,
    raw_1s_closes: np.ndarray,
    trend_up: bool,
    window_sec: int = VOL_RATIO_WINDOW_SEC,
) -> np.ndarray:
    """对每个观测点, 计算前 window_sec 秒的反向/正向波动率比值。"""
    ratios = np.full(len(obs_times_ms), 1.0, dtype=float)
    if len(raw_1s_closes) < 10:
        return ratios
    ret = np.diff(raw_1s_closes)
    ret_t = raw_1s_times_ms[1:]
    for i, t_obs in enumerate(obs_times_ms):
        t_start = int(t_obs) - window_sec * 1000
        mask = (ret_t >= t_start) & (ret_t < int(t_obs))
        r = ret[mask]
        if len(r) < 10:
            continue
        fwd = r[r > 0] if trend_up else r[r < 0]
        bwd = r[r < 0] if trend_up else r[r > 0]
        af = np.abs(fwd)
        ab = np.abs(bwd)
        sf = float(np.std(af)) if len(af) > 1 else 1e-10
        sb = float(np.std(ab)) if len(ab) > 1 else 1e-10
        ratios[i] = float(np.log(np.clip(sb / max(sf, 1e-10), 0.1, 10.0)))
    return ratios


def build_windows(
    bars_1m,
    *,
    bars_1s: pd.DataFrame | None = None,
    obs_step_sec: int = DEFAULT_OBS_STEP_SEC,
    trigger_mode: TriggerMode = "111_000",
    obs_start_offset_minutes: int = 3,
) -> list[WindowSample]:
    """
    扫描分钟线, 切成不重叠的 5 分钟窗口。

    trigger_mode:
        - ``111_000``（默认）: 仅保留前三分钟序列为 111 或 000 的窗口（现行策略）。
        - ``third_digit``: 保留所有前三分钟已走完的窗口；用**第 3 分钟末相对基准的 1/0**
          （序列第 3 位）虚拟成 ``111``/``000`` 以定义反转标签与 Polymarket 侧，
          不再要求前两分钟与第三分钟同向。
        - ``first_digit``: 保留所有完整 5 分钟窗口；用**第 1 分钟收盘相对基准的 1/0**
          （序列第 1 位）虚拟成 ``111``/``000``，反转定义与同分支下的 ``third_digit`` 一致，
          便于与 ``obs_start_offset_minutes=1`` 组合做「一位版」因果评估。

    若提供 bars_1s 则从中切取秒级真实路径; 否则回退为"分钟级线性插值"
    （保守标记 observed_from='1m'; 实盘禁用此回退）。

    obs_start_offset_minutes:
        - ``3``（默认）: 观测与现行一致，起于第 3 分钟末，观测长约 120s。
        - ``1``: 起于第 1 分钟末，观测至第 5 分钟末（约 240s），用于「首序列位形成后」等实验；
          ``d0_abs_pct`` 改为第 1 分钟末偏离，以匹配动态 λ0 锚定时刻。

    bars_1m 接受 DataFrame 或 MinuteBars (兼容旧调用)。
    """
    if obs_start_offset_minutes not in (1, 3):
        raise ValueError("obs_start_offset_minutes must be 1 or 3")
    if hasattr(bars_1m, "df"):
        bars_1m = bars_1m.df
    if "open_time" not in bars_1m.columns:
        raise ValueError("bars_1m must contain open_time column")
    df_1m = normalize_to_ms(bars_1m).sort_values("open_time").reset_index(drop=True)

    ts_1s_sorted: np.ndarray | None = None
    if bars_1s is not None and not bars_1s.empty:
        df_1s = normalize_to_ms(bars_1s).sort_values("open_time").reset_index(drop=True)
        ts_1s_sorted = df_1s["open_time"].to_numpy(dtype=np.int64)
        observed_from = "1s"
    else:
        df_1s = None
        observed_from = "1m"

    n_full = (len(df_1m) // WINDOW_MINUTES) * WINDOW_MINUTES
    if n_full == 0:
        return []

    samples: list[WindowSample] = []
    for i in range(0, n_full, WINDOW_MINUTES):
        block = df_1m.iloc[i : i + WINDOW_MINUTES]
        baseline = float(block["open"].iloc[0])
        if baseline <= 0 or not np.isfinite(baseline):
            continue
        closes = block["close"].astype(float).tolist()
        seq = build_fixed_baseline_sequence(baseline, closes)
        trig3 = extract_trigger(seq)
        if trigger_mode == "111_000":
            if not is_target_trigger(trig3):
                continue
            trig = trig3
        elif trigger_mode == "third_digit":
            if len(seq) < 3:
                continue
            trig = "111" if seq[2] == "1" else "000"
        elif trigger_mode == "first_digit":
            if len(seq) < 1:
                continue
            trig = "111" if seq[0] == "1" else "000"
        else:
            raise ValueError(f"unknown trigger_mode={trigger_mode!r}")

        window_start_ms = int(block["open_time"].iloc[0])
        obs_start_ms = window_start_ms + int(obs_start_offset_minutes) * MS_PER_MIN
        obs_end_ms = window_start_ms + 5 * MS_PER_MIN - MS_PER_SEC  # 第5分钟最后一秒
        obs_duration_sec = float(obs_end_ms - obs_start_ms + MS_PER_SEC) / float(MS_PER_SEC)
        if obs_start_offset_minutes == 3:
            d0_abs_pct = abs(float(closes[2]) - baseline) / baseline * 100.0
        else:
            d0_abs_pct = abs(float(closes[0]) - baseline) / baseline * 100.0

        if df_1s is not None and ts_1s_sorted is not None:
            sec_block = _select_window_seconds_slice(
                df_1s, ts_1s_sorted, obs_start_ms, obs_end_ms,
            )
            if len(sec_block) < int(obs_duration_sec) // 2:
                # 秒级数据缺口过半 -> 跳过本窗口（保守）
                continue
            times_ms = sec_block["open_time"].to_numpy(dtype=np.int64)
            prices = sec_block["close"].to_numpy(dtype=float)
        else:
            times_ms = np.arange(
                obs_start_ms, obs_end_ms + 1, MS_PER_SEC, dtype=np.int64
            )
            if obs_start_offset_minutes == 3:
                min3_close = float(closes[2])
                min4_close = float(closes[3])
                min5_close = float(closes[4])
                seg1 = times_ms < (window_start_ms + 4 * MS_PER_MIN)
                seg2 = (times_ms >= window_start_ms + 4 * MS_PER_MIN) & (
                    times_ms < window_start_ms + 5 * MS_PER_MIN
                )
                prices = np.empty_like(times_ms, dtype=float)
                t1 = (times_ms[seg1] - obs_start_ms) / (MS_PER_MIN)
                prices[seg1] = min3_close + (min4_close - min3_close) * t1
                t2 = (times_ms[seg2] - (window_start_ms + 4 * MS_PER_MIN)) / (MS_PER_MIN)
                prices[seg2] = min4_close + (min5_close - min4_close) * t2
            else:
                prices = _interp_prices_1m_from_obs_start(
                    times_ms,
                    window_start_ms=window_start_ms,
                    obs_start_ms=obs_start_ms,
                    closes=closes,
                )

        times_ms, prices = _resample_to_step(prices, times_ms, obs_step_sec)
        if len(prices) < 2:
            continue

        d = np.abs(prices - baseline) / baseline * 100.0
        elapsed_sec = (times_ms - obs_start_ms) / MS_PER_SEC
        t_remaining = obs_duration_sec - elapsed_sec
        t_remaining = np.clip(t_remaining, 0.0, obs_duration_sec)

        # 计算 vol_ratio（仅 1s 原始数据可用时）
        vr_per_obs = None
        if df_1s is not None and ts_1s_sorted is not None and len(times_ms) > 0:
            vr_start_ms = max(int(times_ms[0]) - VOL_RATIO_WINDOW_SEC * 1000, int(df_1s["open_time"].iloc[0]))
            vr_block = _select_window_seconds_slice(df_1s, ts_1s_sorted, vr_start_ms, int(times_ms[-1]))
            if len(vr_block) > 10:
                vp = vr_block["close"].to_numpy(dtype=np.float64)
                vt = vr_block["open_time"].to_numpy(dtype=np.int64)
                vr_per_obs = _compute_vol_ratio_per_obs(
                    times_ms, vt, vp, trend_up=(trig == "111"),
                )

        if trig == "111":
            reversed_ = closes[4] <= baseline
        else:
            reversed_ = closes[4] > baseline

        samples.append(
            WindowSample(
                window_start_ms=window_start_ms,
                baseline=baseline,
                trigger=trig,
                sequence=seq,
                obs_times_ms=times_ms,
                obs_prices=prices,
                d_per_obs=d,
                t_per_obs=t_remaining,
                reversed=bool(reversed_),
                observed_from=observed_from,
                d0_abs_pct=float(d0_abs_pct),
                obs_duration_sec=float(obs_duration_sec),
                obs_start_offset_minutes=int(obs_start_offset_minutes),
                close_m5=float(closes[4]),
                vol_ratio_per_obs=vr_per_obs,
            )
        )
    return samples


def _touched_reversal_in_window(s: WindowSample) -> bool:
    b = float(s.baseline)
    if s.trigger == "111":
        return bool(np.any(s.obs_prices <= b))
    return bool(np.any(s.obs_prices >= b))


def windows_to_event_table(
    samples: list[WindowSample],
    *,
    label_mode: str = "terminal",
) -> pd.DataFrame:
    """
    展平为每窗口多观测点的事件表 (用于累积风险积分似然)。
    每条记录代表一个观测点 (d_t, T_t)。
    事件标签:
      - terminal: 第5分钟末是否反转（默认，推荐）
      - touch:    窗口内是否曾触碰/越过基准价（兼容旧口径）
    """
    if not samples:
        return pd.DataFrame(
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
        )

    mode = str(label_mode).lower().strip()
    if mode not in {"terminal", "touch"}:
        raise ValueError(f"unsupported label_mode={label_mode!r}, expect terminal|touch")

    rows: list[dict] = []
    for wid, s in enumerate(samples):
        n = len(s.d_per_obs)
        obs_start_ms = int(s.window_start_ms) + int(s.obs_start_offset_minutes) * MS_PER_MIN
        odur = float(getattr(s, "obs_duration_sec", float(OBS_DURATION_SEC)))
        ev = bool(s.reversed) if mode == "terminal" else _touched_reversal_in_window(s)
        for k in range(n):
            elapsed_sec = float(int(s.obs_times_ms[k]) - obs_start_ms) / float(MS_PER_SEC)
            vr_val = None
            if hasattr(s, "vol_ratio_per_obs") and s.vol_ratio_per_obs is not None and k < len(s.vol_ratio_per_obs):
                vr_val = float(s.vol_ratio_per_obs[k])
            rows.append(
                {
                    "window_id": wid,
                    "obs_idx": k,
                    "d": float(s.d_per_obs[k]),
                    "t_remaining": float(s.t_per_obs[k]),
                    "elapsed_from_obs_start_sec": float(elapsed_sec),
                    "obs_duration_sec": odur,
                    "event": int(ev and k == n - 1),
                    "trigger": s.trigger,
                    "d0_abs_pct": float(s.d0_abs_pct),
                    "vol_ratio": vr_val,
                }
            )
    return pd.DataFrame(rows)
