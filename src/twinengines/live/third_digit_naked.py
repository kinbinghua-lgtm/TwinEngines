"""
third_digit + 动态 λ₀(d₀) 推断。

默认实盘预测核见 ``third_digit_sec_dynamic``：**Binance 1s 已收盘路径** + ``residual_sec``（秒级动态）。

本模块保留 **分钟收盘结点** 动态（``naked_predict_live_dynamic``）作为 REST 失败时的回退，
以及 ``--frozen-minute3-only`` 固定 120s 快照。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

import numpy as np

from ..data.window import OBS_DURATION_SEC
from ..model.survival_ablation import _window_H_dynamic_l0
from ..model.survival_survival_validation import reversal_prob_from_H
from ..model.prefix_survival import PrefixDynamicModel

SCHEMA_THIRD_DIGIT_DYNAMIC_V1 = "third_digit_dynamic_v1"
MS_PER_MIN = 60_000
EPS_TS_SEC = 1e-6



@dataclass(frozen=True)
class ThirdDigitDynamicParams:
    gamma: float
    beta: float
    dynamic_a: float
    dynamic_c: float
    symbol: str = "BTCUSDT"

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> ThirdDigitDynamicParams:
        if payload.get("schema_version") != SCHEMA_THIRD_DIGIT_DYNAMIC_V1:
            raise ValueError(
                f"expect schema_version={SCHEMA_THIRD_DIGIT_DYNAMIC_V1!r}, got {payload.get('schema_version')!r}"
            )
        return cls(
            gamma=float(payload["gamma"]),
            beta=float(payload["beta"]),
            dynamic_a=float(payload["dynamic_a"]),
            dynamic_c=float(payload["dynamic_c"]),
            symbol=str(payload.get("symbol", "BTCUSDT")),
        )


def load_third_digit_dynamic_params(path: str | Path) -> ThirdDigitDynamicParams | PrefixDynamicModel:
    p = Path(path)
    payload = json.loads(p.read_text(encoding="utf-8"))
    schema = payload.get("schema_version")
    if schema == SCHEMA_THIRD_DIGIT_DYNAMIC_V1:
        return ThirdDigitDynamicParams.from_json(payload)
    if schema == "third_digit_prefix_dynamic_v1":
        return PrefixDynamicModel.from_json(payload)
    raise ValueError(f"unsupported model schema_version={schema!r}")


def _current_prefix_from_closes(*, baseline: float, closes: list[float], n_closed_minutes: int) -> str:
    n = int(max(0, min(int(n_closed_minutes), len(closes), 5)))
    if n <= 0:
        return ""
    return "".join("1" if float(c) > float(baseline) else "0" for c in closes[:n])


def _dynamic_params_for_prefix(
    params: ThirdDigitDynamicParams | PrefixDynamicModel,
    *,
    prefix: str,
) -> tuple[float, float, float, float, str | None, dict[str, Any] | None]:
    if isinstance(params, PrefixDynamicModel):
        bucket = params.bucket_for_prefix(prefix)
        if bucket is not None:
            return (
                float(bucket.gamma),
                float(bucket.beta),
                float(bucket.a),
                float(bucket.c),
                str(bucket.prefix),
                bucket.to_dict(),
            )
        return (
            float(params.gamma),
            float(params.beta),
            float(params.dynamic_a),
            float(params.dynamic_c),
            None,
            None,
        )
    return (
        float(params.gamma),
        float(params.beta),
        float(params.dynamic_a),
        float(params.dynamic_c),
        None,
        None,
    )


def third_digit_trigger_and_d0(*, baseline: float, closes_first_three: list[float]) -> tuple[str, float]:
    """虚拟触发（序列第三位）与第 3 分钟末 |偏离|% ，与 ``trigger_mode=third_digit`` 一致。"""
    if baseline <= 0 or not np.isfinite(baseline):
        raise ValueError("baseline must be positive finite")
    if len(closes_first_three) != 3:
        raise ValueError("need exactly 3 minute closes")
    seq = "".join("1" if float(c) > baseline else "0" for c in closes_first_three)
    trig = "111" if seq[2] == "1" else "000"
    c3 = float(closes_first_three[2])
    d0 = abs(c3 - baseline) / baseline * 100.0
    return trig, float(d0)


def pr_rev_causal_obs_start_only(d0: float, p: ThirdDigitDynamicParams) -> float:
    """
    观测段起于第 3 分钟末：仅用该点偏离，剩余 ``OBS_DURATION_SEC`` 秒冻结外推（因果 t=0）。
    """
    ds = np.array([float(d0)], dtype=float)
    ts = np.array([float(OBS_DURATION_SEC)], dtype=float)
    H = _window_H_dynamic_l0(
        ds,
        ts,
        d0=float(d0),
        a=float(p.dynamic_a),
        c=float(p.dynamic_c),
        gamma=float(p.gamma),
        beta=float(p.beta),
    )
    return float(reversal_prob_from_H(np.array([H], dtype=float))[0])


def directional_from_pr_rev(pr_rev: float, trig: str) -> tuple[float, bool, float]:
    """
    返回 (p_up, pred_up, confidence_on_predicted_side)。
    confidence = max(p_up, 1-p_up) 当预测方向为 argmax；此处等价于所选边的概率。
    """
    if trig == "111":
        p_up = 1.0 - float(pr_rev)
    else:
        p_up = float(pr_rev)
    pred_up = bool(p_up > 0.5)
    conf = float(p_up if pred_up else (1.0 - p_up))
    return float(p_up), pred_up, conf


def residual_seconds_to_end(*, end_ts_ms: int, at_ms: int) -> float:
    """当前时刻相对合约结算时间的剩余秒数（可为 0；不含 eps）。"""
    return max(0.0, (float(end_ts_ms) - float(at_ms)) / 1000.0)


def closed_minute_bars_since_window_start(*, window_start_ms: int, now_ms: int) -> int:
    """
    相对 ``window_start_ms`` 已完整收盘的 1m 根数 ``∈ [0,5]``（墙钟近似，与 Binance 对齐）。
    例如 ``now >= window_start + 3min`` ⇒ 至少收盘 3 根。
    """
    if int(now_ms) <= int(window_start_ms):
        return 0
    elapsed = int(now_ms) - int(window_start_ms)
    return int(min(5, elapsed // MS_PER_MIN))


def live_causal_ds_ts_minute_path(
    *,
    baseline: float,
    closes: list[float],
    window_start_ms: int,
    end_ts_ms: int,
    now_ms: int,
    n_closed_minutes: int,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]] | None:
    """
    动态因果分钟路径：仅用「已收盘」分钟的收盘价算偏离；``ts`` 为单调递减的剩余秒数结点，
    与 ``_window_H_dynamic_l0`` / 离线 ``aggregate_event_table_with_d0_causal`` 分段语义一致。

    ``n_closed_minutes`` 应由 ``closed_minute_bars_since_window_start`` 给出，避免 REST 返回未收盘最后一根时被误用。
    """
    if baseline <= 0 or len(closes) < 3:
        return None
    nc = int(min(int(n_closed_minutes), len(closes), 5))
    if nc < 3:
        return None
    obs_start_ms = int(window_start_ms) + 3 * MS_PER_MIN
    if int(now_ms) < obs_start_ms:
        return None

    closes_use = [float(x) for x in closes[:nc]]
    d3 = abs(closes_use[2] - baseline) / baseline * 100.0
    d4 = abs(closes_use[3] - baseline) / baseline * 100.0 if nc >= 4 else d3
    d5 = abs(closes_use[4] - baseline) / baseline * 100.0 if nc >= 5 else d4

    T4 = int(window_start_ms) + 4 * MS_PER_MIN

    def R_ts(at_ms: int) -> float:
        """用于 hazard 结点，避免 ts=0 导致数值问题。"""
        return max(EPS_TS_SEC, residual_seconds_to_end(end_ts_ms=end_ts_ms, at_ms=int(at_ms)))

    R_now = R_ts(int(now_ms))
    res_plain = residual_seconds_to_end(end_ts_ms=end_ts_ms, at_ms=int(now_ms))

    meta: dict[str, Any] = {
        "residual_sec": float(res_plain),
        "obs_start_ms": int(obs_start_ms),
        "n_closed_minutes": int(nc),
        "d_path_pct_closed": [abs(closes_use[i] - baseline) / baseline * 100.0 for i in range(nc)],
    }

    # 仅第三根收盘可用：单段，剩余时间连续缩短
    if nc < 4:
        ds = np.array([d3], dtype=float)
        ts = np.array([R_now], dtype=float)
        meta["path_stage"] = "min3_only_dynamic_time"
        return ds, ts, meta

    R_obs = R_ts(obs_start_ms)
    R_T4 = R_ts(T4)

    # 第 4 根已收盘、第 5 根未收盘：d3→d4 两段 + 当前剩余时间
    if nc < 5:
        ds = np.array([d3, d4, d4], dtype=float)
        ts = np.array([R_obs, R_T4, R_now], dtype=float)
        meta["path_stage"] = "min4_closed"
        return ds, ts, meta

    # 五根皆收盘（接近窗末）：d3→d4→d5 三段
    ds = np.array([d3, d4, d5], dtype=float)
    ts = np.array([R_obs, R_T4, R_now], dtype=float)
    meta["path_stage"] = "min5_closed"
    return ds, ts, meta


def naked_predict_live_dynamic(
    *,
    baseline: float,
    closes: list[float],
    window_start_ms: int,
    end_ts_ms: int,
    now_ms: int,
    n_closed_minutes: int,
    params: ThirdDigitDynamicParams | PrefixDynamicModel,
) -> dict[str, Any] | None:
    """第 3 分钟序列确认后，按已收盘分钟 + 当前剩余时间动态重算 ``H`` 与方向概率。"""
    trig, d0 = third_digit_trigger_and_d0(baseline=baseline, closes_first_three=closes[:3])
    built = live_causal_ds_ts_minute_path(
        baseline=baseline,
        closes=closes,
        window_start_ms=int(window_start_ms),
        end_ts_ms=int(end_ts_ms),
        now_ms=int(now_ms),
        n_closed_minutes=int(n_closed_minutes),
    )
    if built is None:
        return None
    ds, ts, meta = built
    prefix = _current_prefix_from_closes(
        baseline=baseline,
        closes=closes,
        n_closed_minutes=min(int(n_closed_minutes), len(closes)),
    )
    gamma, beta, dyn_a, dyn_c, bucket_prefix, bucket_payload = _dynamic_params_for_prefix(params, prefix=prefix)
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
    out: dict[str, Any] = {
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
        "predict_engine": "minute_closed_bins",
        "current_prefix": prefix,
        "prefix_bucket_used": bucket_prefix,
        "prefix_bucket": bucket_payload,
    }
    out.update(meta)
    return out


def naked_predict_minute3(*, baseline: float, closes_first_three: list[float], params: ThirdDigitDynamicParams | PrefixDynamicModel) -> dict[str, Any]:
    trig, d0 = third_digit_trigger_and_d0(baseline=baseline, closes_first_three=closes_first_three)
    gamma, beta, dyn_a, dyn_c, bucket_prefix, bucket_payload = _dynamic_params_for_prefix(
        params,
        prefix="".join("1" if float(c) > float(baseline) else "0" for c in closes_first_three[:3]),
    )
    ds = np.array([float(d0)], dtype=float)
    ts = np.array([float(OBS_DURATION_SEC)], dtype=float)
    H = _window_H_dynamic_l0(
        ds,
        ts,
        d0=float(d0),
        a=dyn_a,
        c=dyn_c,
        gamma=gamma,
        beta=beta,
    )
    pr = float(reversal_prob_from_H(np.array([H], dtype=float))[0])
    p_up, pred_up, conf = directional_from_pr_rev(pr, trig)
    return {
        "trigger": trig,
        "d0_abs_pct": d0,
        "p_rev": pr,
        "p_up": p_up,
        "p_down": float(1.0 - p_up),
        "pred_up": pred_up,
        "confidence_on_pick": conf,
        "dynamic_causal": False,
        "predict_engine": "frozen_minute3_snapshot",
        "path_stage": "frozen_minute3_snapshot",
        "residual_sec": float(OBS_DURATION_SEC),
        "current_prefix": "".join("1" if float(c) > float(baseline) else "0" for c in closes_first_three[:3]),
        "prefix_bucket_used": bucket_prefix,
        "prefix_bucket": bucket_payload,
    }


def fetch_binance_true_up_window_end(
    *,
    symbol: str,
    window_start_ms: int,
    n_bars: int = 5,
    timeout_sec: float = 10.0,
) -> tuple[bool, float, float] | None:
    """
    与离线 ``terminal`` 标签对齐：基准 = 窗口第一分钟开盘价，``true_up`` = 第 5 分钟收盘 > 基准。
    返回 ``(true_up, baseline, close_m5)``；失败返回 ``None``。
    """
    bx = fetch_binance_closes_and_baseline(
        symbol=symbol,
        window_start_ms=window_start_ms,
        n_bars=n_bars,
        timeout_sec=timeout_sec,
    )
    if bx is None:
        return None
    baseline, closes = bx
    if len(closes) < 5:
        return None
    b = float(baseline)
    c5 = float(closes[4])
    if b <= 0 or not np.isfinite(b) or not np.isfinite(c5):
        return None
    return bool(c5 > b), b, c5


def fetch_binance_closes_and_baseline(
    *,
    symbol: str,
    window_start_ms: int,
    n_bars: int = 5,
    timeout_sec: float = 8.0,
) -> tuple[float, list[float]] | None:
    """
    拉取 ``window_start_ms`` 起的连续 ``n_bars`` 根 1m K（Binance open_time 对齐）。
    返回 (baseline=第一根 open, closes 列表)；失败返回 None。
    """
    sym = str(symbol).upper()
    url = (
        "https://api.binance.com/api/v3/klines"
        f"?symbol={sym}&interval=1m&startTime={int(window_start_ms)}&limit={int(n_bars)}"
    )
    req = Request(url, headers={"User-Agent": "TwinEngines-naked-third-digit/1.0"})
    try:
        with urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read()
    except (URLError, HTTPError, TimeoutError, OSError):
        return None
    try:
        rows = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return None
    if not rows or len(rows) < 3:
        return None
    baseline = float(rows[0][1])
    closes = [float(r[4]) for r in rows[: max(3, min(len(rows), n_bars))]]
    return baseline, closes


def trading_ready_after_minute3_close(*, window_start_ms: int, now_ms: int, causal_decision_lag_sec: float = 0.0) -> bool:
    """是否已过第 3 分钟收盘时刻 + 可选滞后（秒）。"""
    t3_close_ms = int(window_start_ms) + 3 * 60_000
    need_ms = t3_close_ms + int(float(causal_decision_lag_sec) * 1000.0)
    return int(now_ms) >= need_ms


def save_example_params_json(path: str | Path) -> None:
    """写入示例参数文件（需替换为你离线拟合得到的数值）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    example = {
        "schema_version": SCHEMA_THIRD_DIGIT_DYNAMIC_V1,
        "gamma": 0.1,
        "beta": -8.0,
        "dynamic_a": 0.007561598853351611,
        "dynamic_c": 24.545266853729263,
        "symbol": "BTCUSDT",
        "_comment": "Sync with configs/example_third_digit_dynamic.json (offline MLE).",
    }
    p.write_text(json.dumps(example, indent=2, ensure_ascii=False), encoding="utf-8")
