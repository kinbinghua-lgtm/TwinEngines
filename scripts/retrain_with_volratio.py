#!/usr/bin/env python3
"""
Retrain survival model with β_vol:
  1. Scalar MLE → (λ₀, γ, β, β_vol)
  2. Prefix dynamic model with fixed β_vol
  3. Save artifact with per-prefix mapping endpoints
"""
import sys, json, math
from pathlib import Path
import numpy as np
from datetime import date, datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))
import src.twinengines  # noqa
from src.twinengines.data.binance_downloader import DownloadConfig, download_range
from src.twinengines.data.window import build_windows, windows_to_event_table
from src.twinengines.model.fit import fit_survival_mle, time_split_three
from src.twinengines.model.prefix_survival import fit_prefix_dynamic_model
from src.twinengines.survival import SurvivalParams

START = date(2026, 4, 11)
END = date(2026, 5, 1)
SYM = "BTCUSDT"
CACHE = Path("data_cache")
OBS_STEP = 10

print("=" * 60)
print(f"Training range: {START} -> {END}")
print("=" * 60)

# 1. Download
cfg_1m = DownloadConfig(symbol=SYM, interval="1m", cache_dir=CACHE)
cfg_1s = DownloadConfig(symbol=SYM, interval="1s", cache_dir=CACHE)
df_1m = download_range(cfg_1m, START, END, progress=True)
df_1s = download_range(cfg_1s, START, END, progress=True)
print(f"\n1m: {len(df_1m)} bars, 1s: {len(df_1s)} bars")

# 2. Build windows for scalar MLE
samples = build_windows(df_1m, bars_1s=df_1s, obs_step_sec=OBS_STEP,
                         trigger_mode="third_digit", obs_start_offset_minutes=3)
et = windows_to_event_table(samples, label_mode="terminal")
train, valid, test = time_split_three(et, 0.6, 0.2)
ntr = train["window_id"].nunique()
nva = valid["window_id"].nunique()
nte = test["window_id"].nunique()
print(f"Windows: total={len(samples)} train={ntr} valid={nva} test={nte}")

# 3. Scalar MLE → includes β_vol
print("\n--- Scalar MLE ---")
res = fit_survival_mle(train, initial=SurvivalParams(0.00339, 0.1, -8.0))
p = res.params
print(f"  lambda0={p.lambda0:.6f} gamma={p.gamma:.4f} beta={p.beta:.4f} beta_vol={p.beta_vol:.4f}")
print(f"  LL={res.log_likelihood:.1f} n={res.n_windows} ok={res.success}")

# 4. Prefix dynamic model
print("\n--- Prefix model ---")
s2 = build_windows(df_1m, bars_1s=df_1s, obs_step_sec=OBS_STEP,
                    trigger_mode="third_digit", obs_start_offset_minutes=3)
pm, agg = fit_prefix_dynamic_model(s2, gamma=p.gamma, beta=p.beta,
                                     global_a=0.00756, global_c=24.545,
                                     min_n=15, shrink_l2=2.0)
print(f"  buckets: {len(pm.buckets)}")

# 5. Compute per-prefix mapping endpoints
print("\n--- Mapping endpoints ---")
p_min = 0.01
p_max_global = 1.0 - math.exp(-0.00339 * 12 / 1.1 * 10)
prefix_buckets = {}
for pref, bp in sorted(pm.buckets.items()):
    if len(pref) != 3:
        continue
    bl = bp.event_rate
    p_max = 1.0 - math.exp(-bp.a * 120.0 / 1.1)
    prefix_buckets[pref] = {"a": round(bp.a, 5), "c": round(bp.c, 4),
                            "event_rate": round(bl, 3), "p_max": round(p_max, 4),
                            "n": bp.n_windows}
    print(f"  {pref}: bl={bl:.3f} a={bp.a:.5f} p_max={p_max:.4f}")

# 6. Build artifact JSON
out = {
    "schema_version": "1.1",
    "trained_at": datetime.now(timezone.utc).isoformat(),
    "data_meta": {
        "n_total_windows": len(samples),
        "n_train_windows": ntr,
        "n_valid_windows": nva,
        "n_test_windows": nte,
        "obs_step_sec": OBS_STEP,
        "observed_from": "1s",
        "note": "trained with β_vol + 10min rolling vol_ratio",
    },
    "params": {
        "lambda0": round(p.lambda0, 6),
        "gamma": round(p.gamma, 4),
        "beta": round(p.beta, 4),
        "beta_vol": round(p.beta_vol, 4),
    },
    "thresholds": {
        "trend_reversal_prob_max": 0.15,
        "trend_signal_stability_sec_min": 15,
        "reversal_baseline_offset_min": 0.02,
        "reversal_signal_stability_sec_min": 3,
        "reversal_prob_min": 0.208,
        "reversal_edge_vs_baseline_min": 0.05,
        "trend_min_expected_value": 0.5,
    },
    "calibration": {
        "baseline_prob": round(np.mean([bp.event_rate for bp in pm.buckets.values() if len(bp.prefix) == 3]), 3),
        "bin_table": [],
    },
    "survival_model": {
        "global_dynamic": {"a": 0.00756, "c": 24.545},
        "prefix_buckets": prefix_buckets,
        "source": f"fitted {START}..{END} step={OBS_STEP}s with β_vol={p.beta_vol:.4f}",
    },
    "shadow_engine": {
        "note": "empirical trend archived. Using three-layer survival model with β_vol + vol_ratio mapping.",
    },
}

path = Path("artifact_btc_v6_volratio.json")
path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\nSaved: {path}")
print(f"  β_vol = {p.beta_vol:.4f}")
print("Done!")
