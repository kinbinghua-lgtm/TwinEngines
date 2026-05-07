#!/usr/bin/env python3
from __future__ import annotations

import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import brier_score_loss, log_loss

from src.twinengines.model.direction_probability import build_direction_artifact_payload

CACHE = Path("data_cache/BTCUSDT")
OUT = Path("reports")
OBS_STEP_SEC = 5
EPS = 1e-6
PRED_BINS = [0, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 1.0]


def add_second_features(df1s: pd.DataFrame) -> pd.DataFrame:
    df = df1s[["open_time", "close", "high", "low"]].copy()
    df["close"] = df["close"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    ot = df["open_time"].astype("int64")
    df["open_time_ms"] = (ot // 1000).astype("int64") if ot.iloc[0] > 10_000_000_000_000 else ot.astype("int64")
    ret = df["close"].pct_change() * 100.0
    df["ret_1s_abs"] = ret.abs().fillna(0.0)
    df["vol_30s"] = ret.rolling(30, min_periods=5).std().fillna(0.0)
    df["vol_60s"] = ret.rolling(60, min_periods=10).std().fillna(0.0)
    df["absret_30s"] = df["ret_1s_abs"].rolling(30, min_periods=5).sum().fillna(0.0)
    df["absret_60s"] = df["ret_1s_abs"].rolling(60, min_periods=10).sum().fillna(0.0)
    h30 = df["high"].rolling(30, min_periods=5).max()
    l30 = df["low"].rolling(30, min_periods=5).min()
    h60 = df["high"].rolling(60, min_periods=10).max()
    l60 = df["low"].rolling(60, min_periods=10).min()
    df["range_30s"] = ((h30 - l30) / df["close"] * 100.0).fillna(0.0)
    df["range_60s"] = ((h60 - l60) / df["close"] * 100.0).fillna(0.0)
    return df[["open_time_ms", "vol_30s", "vol_60s", "absret_30s", "absret_60s", "range_30s", "range_60s"]]


def normalize_open_time_ms(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    ot = out["open_time"].astype("int64")
    if len(ot) and ot.iloc[0] > 10_000_000_000_000:
        out["open_time"] = (ot // 1000).astype("int64")
    else:
        out["open_time"] = ot.astype("int64")
    return out.sort_values("open_time").reset_index(drop=True)


def sequence_from_closes(baseline: float, closes: list[float]) -> str:
    return "".join("1" if float(c) > baseline else "0" for c in closes)


def prefix_values(prefix_len: int) -> list[str]:
    if prefix_len <= 0:
        return []
    return [format(i, f"0{prefix_len}b") for i in range(2 ** prefix_len)]


def day_samples(day: str, prefix_len: int) -> pd.DataFrame:
    p1m = CACHE / "1m" / f"{day}.parquet"
    p1s = CACHE / "1s" / f"{day}.parquet"
    if not p1m.exists() or not p1s.exists():
        return pd.DataFrame()
    df1m = normalize_open_time_ms(pd.read_parquet(p1m))
    df1s = normalize_open_time_ms(pd.read_parquet(p1s))
    sec_feat = add_second_features(df1s).set_index("open_time_ms")
    sec_ts = df1s["open_time"].to_numpy(dtype=np.int64)
    sec_close = df1s["close"].astype(float).to_numpy()
    rows: list[tuple] = []
    n_full = (len(df1m) // 5) * 5
    for i in range(0, n_full, 5):
        block = df1m.iloc[i:i + 5]
        if len(block) < 5:
            continue
        baseline = float(block["open"].iloc[0])
        if baseline <= 0 or not np.isfinite(baseline):
            continue
        closes = block["close"].astype(float).tolist()
        seq = sequence_from_closes(baseline, closes)
        if len(seq) < prefix_len:
            continue
        prefix = seq[:prefix_len] if prefix_len > 0 else ""
        final_up = int(float(closes[4]) > baseline)
        window_start_ms = int(block["open_time"].iloc[0])
        obs_start_ms = window_start_ms + prefix_len * 60_000
        obs_end_ms = window_start_ms + 5 * 60_000 - 1_000
        i0 = int(np.searchsorted(sec_ts, obs_start_ms, side="left"))
        i1 = int(np.searchsorted(sec_ts, obs_end_ms, side="right"))
        if i1 <= i0:
            continue
        times = sec_ts[i0:i1:OBS_STEP_SEC]
        prices = sec_close[i0:i1:OBS_STEP_SEC]
        if len(times) < 2:
            continue
        obs_duration = float(obs_end_ms - obs_start_ms + 1_000) / 1000.0
        for t_ms, px in zip(times, prices):
            d = (float(px) - baseline) / baseline * 100.0
            elapsed = (int(t_ms) - obs_start_ms) / 1000.0
            t_remaining = max(0.0, obs_duration - elapsed)
            if int(t_ms) in sec_feat.index:
                f = sec_feat.loc[int(t_ms)]
                vol30 = float(f.vol_30s)
                vol60 = float(f.vol_60s)
                abs30 = float(f.absret_30s)
                abs60 = float(f.absret_60s)
                rng30 = float(f.range_30s)
                rng60 = float(f.range_60s)
            else:
                vol30 = vol60 = abs30 = abs60 = rng30 = rng60 = 0.0
            rows.append((
                int(window_start_ms), prefix_len, prefix, seq, d, abs(d), t_remaining, d * t_remaining,
                vol30, vol60, abs30, abs60, rng30, rng60, d / (vol30 + EPS), d / (vol60 + EPS),
                final_up, elapsed,
            ))
    return pd.DataFrame(rows, columns=[
        "window_id", "prefix_len", "prefix", "sequence", "d_signed", "d_abs", "t_remaining", "d_x_t",
        "vol_30s", "vol_60s", "absret_30s", "absret_60s", "range_30s", "range_60s", "d_over_vol30", "d_over_vol60",
        "final_up", "elapsed_from_prefix_sec",
    ])


def load_period(days: list[str], prefix_len: int) -> pd.DataFrame:
    parts = []
    for i, day in enumerate(days, 1):
        df = day_samples(day, prefix_len)
        if not df.empty:
            parts.append(df)
        print(f"prefix_len={prefix_len} [{i}/{len(days)}] {day} samples={len(df)} windows={df.window_id.nunique() if not df.empty else 0}", flush=True)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def make_features(df: pd.DataFrame, prefix_len: int) -> pd.DataFrame:
    cols = ["d_signed", "d_abs", "t_remaining", "d_x_t", "vol_30s", "vol_60s", "absret_30s", "absret_60s", "range_30s", "range_60s", "d_over_vol30", "d_over_vol60"]
    x = df[cols].astype(float).copy()
    obs_duration = float((5 - prefix_len) * 60)
    x["t_norm"] = x["t_remaining"] / max(obs_duration, 1.0)
    x["is_above_open"] = (x["d_signed"] > 0).astype(int)
    x["d_over_vol30"] = x["d_over_vol30"].clip(-50, 50)
    x["d_over_vol60"] = x["d_over_vol60"].clip(-50, 50)
    for p in prefix_values(prefix_len):
        x[f"prefix_{p}"] = (df["prefix"] == p).astype(int)
        x[f"{p}_d"] = x[f"prefix_{p}"] * x["d_signed"]
        x[f"{p}_t"] = x[f"prefix_{p}"] * x["t_norm"]
        x[f"{p}_vol60"] = x[f"prefix_{p}"] * x["vol_60s"]
    return x


def calibration_table(pred: pd.DataFrame) -> pd.DataFrame:
    p = pred.copy()
    p["pred_bucket"] = pd.cut(p.p_up, PRED_BINS)
    out = p.groupby("pred_bucket", observed=True).agg(
        actual_up_rate=("final_up", "mean"),
        sample_count=("final_up", "count"),
        window_count=("window_id", "nunique"),
        pred_p_up_mean=("p_up", "mean"),
    ).reset_index()
    out["calibration_error"] = (out.actual_up_rate - out.pred_p_up_mean).abs()
    out["pred_bucket"] = out.pred_bucket.astype(str)
    return out


def confidence_table(pred: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for threshold in [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]:
        sub = pred[pred.p_side >= threshold]
        if sub.empty:
            rows.append({"threshold": threshold, "samples": 0, "windows": 0})
            continue
        rows.append({
            "threshold": threshold,
            "samples": int(len(sub)),
            "windows": int(sub.window_id.nunique()),
            "direction_accuracy": float(sub.side_correct.mean()),
            "mean_p_side": float(sub.p_side.mean()),
            "actual_side_win_rate": float(sub.side_correct.mean()),
            "mean_t_remaining": float(sub.t_remaining.mean()),
            "mean_elapsed_from_prefix": float(sub.elapsed_from_prefix_sec.mean()),
        })
    return pd.DataFrame(rows)


def evaluate_prefix(prefix_len: int, train_days: list[str], valid_days: list[str]) -> dict:
    train = load_period(train_days, prefix_len)
    valid = load_period(valid_days, prefix_len)
    if train.empty or valid.empty:
        raise RuntimeError(f"empty train/valid for prefix_len={prefix_len}")
    x_train = make_features(train, prefix_len)
    y_train = train.final_up.astype(int)
    x_valid = make_features(valid, prefix_len)
    y_valid = valid.final_up.astype(int)
    model = CalibratedClassifierCV(
        HistGradientBoostingClassifier(
            max_iter=180,
            learning_rate=0.045,
            max_leaf_nodes=31,
            min_samples_leaf=80,
            l2_regularization=0.08,
            random_state=43 + prefix_len,
        ),
        method="isotonic",
        cv=3,
    )
    print(f"training prefix_len={prefix_len} train={len(train)} valid={len(valid)} features={x_train.shape[1]}", flush=True)
    model.fit(x_train, y_train)
    p_up = np.clip(model.predict_proba(x_valid)[:, 1], 1e-6, 1 - 1e-6)
    pred = valid.copy()
    pred["p_up"] = p_up
    pred["p_down"] = 1.0 - pred.p_up
    pred["pred_up"] = (pred.p_up >= 0.5).astype(int)
    pred["p_side"] = np.maximum(pred.p_up, pred.p_down)
    pred["side_correct"] = ((pred.p_up >= 0.5).astype(int) == pred.final_up.astype(int)).astype(int)
    cal = calibration_table(pred)
    conf = confidence_table(pred)
    cal_path = OUT / f"direction_prefix_len{prefix_len}_calibration.csv"
    conf_path = OUT / f"direction_prefix_len{prefix_len}_confidence.csv"
    pred_path = OUT / f"direction_prefix_len{prefix_len}_predictions.parquet"
    cal.to_csv(cal_path, index=False)
    conf.to_csv(conf_path, index=False)
    pred.to_parquet(pred_path, index=False)
    summary = {
        "prefix_len": prefix_len,
        "model_version": f"direction_probability_phase{prefix_len}_v1",
        "obs_start_minute": prefix_len,
        "mean_initial_t_remaining_sec": float((5 - prefix_len) * 60),
        "train_samples": int(len(train)),
        "train_windows": int(train.window_id.nunique()),
        "valid_samples": int(len(valid)),
        "valid_windows": int(valid.window_id.nunique()),
        "feature_count": int(x_train.shape[1]),
        "valid_up_rate": float(valid.final_up.mean()),
        "brier": float(brier_score_loss(y_valid, pred.p_up)),
        "logloss": float(log_loss(y_valid, pred.p_up)),
        "direction_accuracy": float(pred.side_correct.mean()),
        "mean_abs_calibration_error": float(cal.calibration_error.mean()),
        "p_side_mean": float(pred.p_side.mean()),
        "p_side_p75": float(pred.p_side.quantile(0.75)),
        "p_side_p90": float(pred.p_side.quantile(0.90)),
        "samples_p_ge_055": int((pred.p_side >= 0.55).sum()),
        "windows_p_ge_055": int(pred.loc[pred.p_side >= 0.55, "window_id"].nunique()),
        "samples_p_ge_065": int((pred.p_side >= 0.65).sum()),
        "windows_p_ge_065": int(pred.loc[pred.p_side >= 0.65, "window_id"].nunique()),
        "samples_p_ge_075": int((pred.p_side >= 0.75).sum()),
        "windows_p_ge_075": int(pred.loc[pred.p_side >= 0.75, "window_id"].nunique()),
        "samples_p_ge_085": int((pred.p_side >= 0.85).sum()),
        "windows_p_ge_085": int(pred.loc[pred.p_side >= 0.85, "window_id"].nunique()),
        "calibration_csv": str(cal_path),
        "confidence_csv": str(conf_path),
        "predictions_parquet": str(pred_path),
    }
    artifact_dir = Path("artifacts")
    artifact_dir.mkdir(exist_ok=True)
    artifact_path = artifact_dir / f"direction_probability_phase{prefix_len}.pkl"
    payload = build_direction_artifact_payload(
        model=model,
        feature_columns=list(x_train.columns),
        metadata=summary,
    )
    with artifact_path.open("wb") as f:
        pickle.dump(payload, f)
    meta_path = artifact_dir / f"direction_probability_phase{prefix_len}_meta.json"
    meta_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    summary["artifact_path"] = str(artifact_path)
    summary["artifact_meta_path"] = str(meta_path)
    print(f"wrote {artifact_path}", flush=True)
    return summary


def main() -> None:
    OUT.mkdir(exist_ok=True)
    dates = sorted([p.stem for p in (CACHE / "1s").glob("*.parquet")])[-180:]
    if len(dates) < 30:
        raise SystemExit("not enough cached days")
    train_days = dates[:150] if len(dates) >= 180 else dates[: max(1, int(len(dates) * 0.8))]
    valid_days = dates[len(train_days):]
    print(f"days={len(dates)} train={len(train_days)} valid={len(valid_days)}")
    parser = __import__("argparse").ArgumentParser()
    parser.add_argument("--only-prefix-len", type=int, default=None)
    args = parser.parse_args()
    prefix_lens = [int(args.only_prefix_len)] if args.only_prefix_len is not None else [0, 1, 2, 3]
    summaries = []
    for prefix_len in prefix_lens:
        summaries.append(evaluate_prefix(prefix_len, train_days, valid_days))
    summary_df = pd.DataFrame(summaries)
    summary_path = OUT / "direction_prefix_len_comparison.csv"
    json_path = OUT / "direction_prefix_len_comparison.json"
    summary_df.to_csv(summary_path, index=False)
    json_path.write_text(json.dumps(summaries, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\n=== PREFIX LENGTH COMPARISON ===")
    print(summary_df.to_string(index=False))
    print(f"\nwrote {summary_path}")
    print(f"wrote {json_path}")


if __name__ == "__main__":
    main()
