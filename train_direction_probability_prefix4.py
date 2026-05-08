#!/usr/bin/env python3
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import brier_score_loss, log_loss

sys.path.insert(0, str(Path(__file__).parent / "src"))

from twinengines.data.window import normalize_to_ms
from twinengines.model.direction_probability import build_direction_artifact_payload

CACHE = Path("data_cache/BTCUSDT")
ARTIFACT_DIR = Path("artifacts")
REPORT_DIR = Path("reports")
OBS_STEP_SEC = 5
PREFIX_LEN = 4
PREFIXES = [format(i, "04b") for i in range(16)]
EPS = 1e-6
PRED_BINS = [0, 0.35, 0.45, 0.50, 0.55, 0.65, 0.75, 1.0]


def add_second_features(df1s: pd.DataFrame) -> pd.DataFrame:
    df = normalize_to_ms(df1s[["open_time", "close", "high", "low"]].copy()).sort_values("open_time")
    df["close"] = df["close"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    ret = df["close"].pct_change() * 100.0
    df["ret_1s_abs"] = ret.abs().fillna(0.0)
    df["vol_30s"] = ret.rolling(30, min_periods=5).std().fillna(0.0)
    df["vol_60s"] = ret.rolling(60, min_periods=10).std().fillna(0.0)
    df["absret_30s"] = df["ret_1s_abs"].rolling(30, min_periods=5).sum().fillna(0.0)
    df["absret_60s"] = df["ret_1s_abs"].rolling(60, min_periods=10).sum().fillna(0.0)
    high30 = df["high"].rolling(30, min_periods=5).max()
    low30 = df["low"].rolling(30, min_periods=5).min()
    high60 = df["high"].rolling(60, min_periods=10).max()
    low60 = df["low"].rolling(60, min_periods=10).min()
    df["range_30s"] = ((high30 - low30) / df["close"] * 100.0).fillna(0.0)
    df["range_60s"] = ((high60 - low60) / df["close"] * 100.0).fillna(0.0)
    return df[["open_time", "vol_30s", "vol_60s", "absret_30s", "absret_60s", "range_30s", "range_60s"]]


def sequence_from_baseline(base: float, closes: list[float]) -> str:
    return "".join("1" if float(c) > base else "0" for c in closes)


def day_samples(day: str) -> pd.DataFrame:
    p1m = CACHE / "1m" / f"{day}.parquet"
    p1s = CACHE / "1s" / f"{day}.parquet"
    if not p1m.exists() or not p1s.exists():
        return pd.DataFrame()
    df1m = normalize_to_ms(pd.read_parquet(p1m)).sort_values("open_time").reset_index(drop=True)
    df1s = normalize_to_ms(pd.read_parquet(p1s)).sort_values("open_time").reset_index(drop=True)
    sec_feat = add_second_features(df1s).set_index("open_time")
    sec_prices = df1s[["open_time", "close"]].copy()
    ts = sec_prices["open_time"].to_numpy(dtype=np.int64)
    n_full = (len(df1m) // 5) * 5
    rows: list[tuple] = []
    for i in range(0, n_full, 5):
        block = df1m.iloc[i : i + 5]
        if len(block) < 5:
            continue
        base = float(block["open"].iloc[0])
        if base <= 0 or not np.isfinite(base):
            continue
        closes = block["close"].astype(float).tolist()
        seq = sequence_from_baseline(base, closes)
        prefix = seq[:PREFIX_LEN]
        final_up = int(float(closes[4]) > base)
        window_start_ms = int(block["open_time"].iloc[0])
        obs_start_ms = window_start_ms + PREFIX_LEN * 60_000
        obs_end_ms = window_start_ms + 5 * 60_000 - 1_000
        j0 = int(np.searchsorted(ts, obs_start_ms, side="left"))
        j1 = int(np.searchsorted(ts, obs_end_ms, side="right"))
        sec_block = sec_prices.iloc[j0:j1]
        if len(sec_block) < 25:
            continue
        sec_block = sec_block.iloc[::OBS_STEP_SEC]
        for _, r in sec_block.iterrows():
            t_ms = int(r.open_time)
            px = float(r.close)
            d = (px - base) / base * 100.0
            t_remaining = max(0.0, (window_start_ms + 5 * 60_000 - t_ms) / 1000.0)
            if t_ms in sec_feat.index:
                f = sec_feat.loc[t_ms]
                vol30 = float(f.vol_30s)
                vol60 = float(f.vol_60s)
                abs30 = float(f.absret_30s)
                abs60 = float(f.absret_60s)
                rng30 = float(f.range_30s)
                rng60 = float(f.range_60s)
            else:
                vol30 = vol60 = abs30 = abs60 = rng30 = rng60 = 0.0
            rows.append((
                int(window_start_ms), prefix, d, abs(d), t_remaining, d * t_remaining,
                vol30, vol60, abs30, abs60, rng30, rng60,
                np.clip(d / (vol30 + EPS), -50, 50),
                np.clip(d / (vol60 + EPS), -50, 50),
                final_up, seq,
            ))
    return pd.DataFrame(rows, columns=[
        "window_id", "prefix", "d_signed", "d_abs", "t_remaining", "d_x_t",
        "vol_30s", "vol_60s", "absret_30s", "absret_60s", "range_30s", "range_60s",
        "d_over_vol30", "d_over_vol60", "final_up", "sequence",
    ])


def load_period(days: list[str]) -> pd.DataFrame:
    parts = []
    for i, day in enumerate(days, 1):
        df = day_samples(day)
        if not df.empty:
            parts.append(df)
        print(f"[{i}/{len(days)}] {day} samples={len(df)} windows={df.window_id.nunique() if not df.empty else 0}", flush=True)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def make_features(df: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "d_signed", "d_abs", "t_remaining", "d_x_t", "vol_30s", "vol_60s",
        "absret_30s", "absret_60s", "range_30s", "range_60s", "d_over_vol30", "d_over_vol60",
    ]
    x = df[cols].astype(float).copy()
    x["t_norm"] = x["t_remaining"] / 60.0
    x["is_above_open"] = (x["d_signed"] > 0).astype(int)
    x["d_over_vol30"] = x["d_over_vol30"].clip(-50, 50)
    x["d_over_vol60"] = x["d_over_vol60"].clip(-50, 50)
    for p in PREFIXES:
        x[f"prefix_{p}"] = (df["prefix"] == p).astype(int)
        x[f"{p}_d"] = x[f"prefix_{p}"] * x["d_signed"]
        x[f"{p}_t"] = x[f"prefix_{p}"] * x["t_norm"]
        x[f"{p}_vol60"] = x[f"prefix_{p}"] * x["vol_60s"]
    return x


def prefix4_table(df: pd.DataFrame) -> dict[str, dict[str, float | int]]:
    windows = df.drop_duplicates("window_id")[["window_id", "prefix", "final_up"]]
    global_p = float(windows.final_up.mean()) if len(windows) else 0.5
    table: dict[str, dict[str, float | int]] = {}
    for p in PREFIXES:
        sub = windows[windows.prefix == p]
        n = int(len(sub))
        up = int(sub.final_up.sum()) if n else 0
        p_raw = float(up / n) if n else global_p
        p_smooth = float((up + 2.0 * global_p) / (n + 2.0)) if n else global_p
        table[p] = {"n": n, "up_count": up, "p_up_raw": p_raw, "p_up": p_smooth, "p_down": 1.0 - p_smooth}
    return table


def calibration(pred: pd.DataFrame) -> pd.DataFrame:
    p = pred.copy()
    p["pred_bucket"] = pd.cut(p.p_up, PRED_BINS)
    c = p.groupby("pred_bucket", observed=True).agg(
        actual_up_rate=("final_up", "mean"),
        sample_count=("final_up", "count"),
        pred_p_up_mean=("p_up", "mean"),
    ).reset_index()
    c["calibration_error"] = (c.actual_up_rate - c.pred_p_up_mean).abs()
    c["pred_bucket"] = c.pred_bucket.astype(str)
    return c


def main() -> None:
    ARTIFACT_DIR.mkdir(exist_ok=True)
    REPORT_DIR.mkdir(exist_ok=True)
    dates = sorted([p.stem for p in (CACHE / "1s").glob("*.parquet")])[-180:]
    if len(dates) < 40:
        raise RuntimeError(f"not enough cached days for prefix4 training: {len(dates)}")
    train_days = dates[: max(1, len(dates) - 30)]
    valid_days = dates[max(1, len(dates) - 30):]
    print(f"Train {len(train_days)} days {train_days[0]}..{train_days[-1]}")
    train = load_period(train_days)
    print(f"Valid {len(valid_days)} days {valid_days[0]}..{valid_days[-1]}")
    valid = load_period(valid_days)
    if train.empty or valid.empty:
        raise RuntimeError("empty train/valid samples")
    x_train = make_features(train)
    y_train = train.final_up.astype(int)
    x_valid = make_features(valid)
    y_valid = valid.final_up.astype(int)
    print(f"Training prefix4 model: train={len(train)} valid={len(valid)} features={x_train.shape[1]}")
    base = HistGradientBoostingClassifier(
        max_iter=140,
        learning_rate=0.04,
        max_leaf_nodes=23,
        min_samples_leaf=60,
        l2_regularization=0.12,
        random_state=44,
    )
    model = CalibratedClassifierCV(base, method="isotonic", cv=3)
    model.fit(x_train, y_train)
    p_up = np.clip(model.predict_proba(x_valid)[:, 1], 1e-6, 1 - 1e-6)
    pred = valid.copy()
    pred["p_up"] = p_up
    pred["p_down"] = 1.0 - pred.p_up
    pred["pred_up"] = (pred.p_up >= 0.5).astype(int)
    cal = calibration(pred)
    table = prefix4_table(pd.concat([train, valid], ignore_index=True))
    prefix_rows = []
    valid_windows = valid.drop_duplicates("window_id")
    for p in PREFIXES:
        sub = valid_windows[valid_windows.prefix == p]
        prefix_rows.append({
            "prefix": p,
            "valid_windows": int(len(sub)),
            "valid_actual_up_rate": float(sub.final_up.mean()) if len(sub) else None,
            "artifact_p_up": float(table[p]["p_up"]),
            "artifact_n": int(table[p]["n"]),
        })
    prefix_df = pd.DataFrame(prefix_rows)
    summary = {
        "semantic": "prefix4 final-minute direction probability model and conditional prior table",
        "model": "HistGradientBoostingClassifier + isotonic calibration cv=3",
        "obs_step_sec": OBS_STEP_SEC,
        "prefix_len": PREFIX_LEN,
        "train_start": train_days[0],
        "train_end": train_days[-1],
        "valid_start": valid_days[0],
        "valid_end": valid_days[-1],
        "train_samples": int(len(train)),
        "train_windows": int(train.window_id.nunique()),
        "valid_samples": int(len(valid)),
        "valid_windows": int(valid.window_id.nunique()),
        "feature_count": int(x_train.shape[1]),
        "train_up_rate": float(train.final_up.mean()),
        "valid_up_rate": float(valid.final_up.mean()),
        "valid_brier": float(brier_score_loss(y_valid, pred.p_up)),
        "valid_logloss": float(log_loss(y_valid, pred.p_up)),
        "valid_direction_accuracy": float((pred.pred_up == pred.final_up).mean()),
        "valid_mean_abs_calibration_error": float(cal.calibration_error.mean()) if len(cal) else None,
        "model_version": "direction_probability_prefix4_v1",
    }
    artifact_payload = build_direction_artifact_payload(
        model=model,
        feature_columns=list(x_train.columns),
        metadata={**summary, "prefix_len": PREFIX_LEN, "model_version": "direction_probability_prefix4_v1"},
    )
    artifact_payload["prefix4_table"] = table
    artifact_payload["blend_policy"] = {"phase4_weight_model": 1.0, "table_available": True}
    with (ARTIFACT_DIR / "direction_probability_prefix4_v1.pkl").open("wb") as f:
        pickle.dump(artifact_payload, f)
    (ARTIFACT_DIR / "direction_probability_prefix4_v1_meta.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (ARTIFACT_DIR / "direction_probability_prefix4_table_v1.json").write_text(json.dumps({"summary": summary, "prefix4_table": table}, indent=2), encoding="utf-8")
    cal.to_csv(REPORT_DIR / "direction_probability_prefix4_calibration_valid30.csv", index=False)
    prefix_df.to_csv(REPORT_DIR / "direction_probability_prefix4_table_valid30.csv", index=False)
    print("wrote artifacts/direction_probability_prefix4_v1.pkl")
    print("wrote artifacts/direction_probability_prefix4_table_v1.json")
    print(json.dumps(summary, indent=2))
    print(prefix_df.to_string(index=False))
    print(cal.to_string(index=False))


if __name__ == "__main__":
    main()
