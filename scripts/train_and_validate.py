#!/usr/bin/env python3
"""
训练 v6 (ln(vr)) + 验证集校准对比 vs v5.
"""
import sys, json, math, numpy as np
from pathlib import Path
from datetime import date, datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.twinengines.data.binance_downloader import DownloadConfig, download_range
from src.twinengines.data.window import build_windows, windows_to_event_table
from src.twinengines.model.fit import fit_survival_mle, time_split_three
from src.twinengines.model.prefix_survival import fit_prefix_dynamic_model
from src.twinengines.survival import SurvivalParams, reversal_probability, cumulative_hazard

TRAIN_START = date(2026, 4, 1)
TRAIN_END   = date(2026, 4, 20)
VALID_START = date(2026, 4, 21)
VALID_END   = date(2026, 4, 30)

SYM = "BTCUSDT"
CACHE = Path("data_cache")
OBS_STEP = 10

print("=" * 60)
print("Training v6 with ln(vr)")
print("=" * 60)

# 1. Load train data
cfg_1m = DownloadConfig(symbol=SYM, interval="1m", cache_dir=CACHE)
cfg_1s = DownloadConfig(symbol=SYM, interval="1s", cache_dir=CACHE)

df_1m_train = download_range(cfg_1m, TRAIN_START, TRAIN_END, progress=True)
df_1s_train = download_range(cfg_1s, TRAIN_START, TRAIN_END, progress=True)
df_1m_valid = download_range(cfg_1m, VALID_START, VALID_END, progress=True)
df_1s_valid = download_range(cfg_1s, VALID_START, VALID_END, progress=True)

# 2. Build windows with ln(vr)
samples_train = build_windows(df_1m_train, bars_1s=df_1s_train, obs_step_sec=OBS_STEP,
                               trigger_mode="third_digit", obs_start_offset_minutes=3)
samples_valid = build_windows(df_1m_valid, bars_1s=df_1s_valid, obs_step_sec=OBS_STEP,
                               trigger_mode="third_digit", obs_start_offset_minutes=3)

et_train = windows_to_event_table(samples_train, label_mode="terminal")
et_valid = windows_to_event_table(samples_valid, label_mode="terminal")
print(f"Train: {len(samples_train)} windows, Valid: {len(samples_valid)} windows")

train, _, _ = time_split_three(et_train, 0.9, 0.05)  # mostly train
valid, _, _ = time_split_three(et_valid, 0.99, 0.005)  # keep train_frac high

# 3. Scalar MLE
print("\n--- Scalar MLE (v6 ln(vr)) ---")
res = fit_survival_mle(train, initial=SurvivalParams(0.003, 0.5, -3.0))
p6 = res.params
print(f"  lambda0={p6.lambda0:.6f} gamma={p6.gamma:.4f} beta={p6.beta:.4f} beta_vol={p6.beta_vol:.4f}")
print(f"  LL={res.log_likelihood:.1f} n={res.n_windows} success={res.success}")

# 4. Prefix dynamic model
print("\n--- Prefix model ---")
st = build_windows(df_1m_train, bars_1s=df_1s_train, obs_step_sec=OBS_STEP,
                    trigger_mode="third_digit", obs_start_offset_minutes=3)
pm, _ = fit_prefix_dynamic_model(st, gamma=p6.gamma, beta=p6.beta,
                                   global_a=0.007, global_c=20, min_n=15, shrink_l2=2.0)

# 5. Build v5 reference: same training data, no vol_ratio
print("\n--- Reference MLE (v5 baseline, no vol_ratio) ---")
# Need to rebuild without vol_ratio — easiest: set beta_vol=0 in inference
p5_ref = SurvivalParams(lambda0=0.00339, gamma=0.1, beta=-8.0)  # old params

# 6. Validate: bin calibration on validation set
print(f"\n{'='*60}")
print("Validation: bin calibration (10 bins)")
print(f"{'='*60}")

def calibrate(samples, params, use_vr):
    """Compute p_rev for each window's last obs + actual reversal, return bins."""
    bins = {}
    for s in samples:
        bl = s.baseline
        if bl <= 0:
            continue
        d0 = s.d0_abs_pct
        if not hasattr(s, 'trigger_pattern') or s.trigger_pattern is None:
            continue
        trig = s.trigger_pattern[:3] if s.trigger_pattern else "000"
        # Take last observation
        last_idx = len(s.obs_prices) - 1
        if last_idx < 1:
            continue
        d = abs(s.obs_prices[last_idx] - bl) / bl * 100.0
        T = s.t_per_obs[last_idx] if hasattr(s, 't_per_obs') and last_idx < len(s.t_per_obs) else 30
        T = max(T, 1.0)
        # Compute p_rev
        try:
            a, bl_rate = 0.007, 0.3
            # Simplified: use params with no prefix-specific lambda
            sp = params
            if use_vr and hasattr(s, 'vol_ratio_per_obs') and s.vol_ratio_per_obs is not None and last_idx < len(s.vol_ratio_per_obs):
                vr = float(s.vol_ratio_per_obs[last_idx])  # already ln(vr)
            else:
                vr = None
            p = reversal_probability(d, T, sp, vol_ratio=vr)
        except Exception:
            continue
        if p <= 0 or p >= 1:
            continue

        # Actual reversal
        if len(seq_prefix) >= 5:
            last_bit = seq_prefix[-1]
            b3 = seq_prefix[2]
            actual = int(last_bit != b3)  # 1 if reversed
        else:
            continue

        bin_id = int(np.clip(p * 10, 0, 9))
        b = bins.setdefault(bin_id, {"n": 0, "pos": 0})
        b["n"] += 1
        if actual:
            b["pos"] += 1

    result = {}
    for bid in sorted(bins.keys()):
        b = bins[bid]
        rate = b["pos"] / b["n"] if b["n"] > 0 else 0
        result[bid] = {"n": b["n"], "actual_rate": round(rate, 3), "expected": round((bid + 0.5) / 10, 3)}
    return result

# Use proper prefix model for lambda computation
pm_a = {}
for pref, bp in pm.buckets.items():
    if len(pref) == 3:
        pm_a[pref] = (bp.a, bp.c, bp.event_rate)

def cal_with_prefix(samples, params6):
    """Calibrate using full prefix-specific lambda."""
    bins = {}
    for s in samples:
        bl = s.baseline
        if bl <= 0:
            continue
        d0 = s.d0_abs_pct
        seq_prefix = s.sequence if hasattr(s, 'sequence') else ""
        trig = seq_prefix[:3] if len(seq_prefix) >= 3 else "000"
        last_idx = len(s.obs_prices) - 1
        if last_idx < 1:
            continue
        d = abs(s.obs_prices[last_idx] - bl) / bl * 100.0
        T = s.t_per_obs[last_idx] if hasattr(s, 't_per_obs') and last_idx < len(s.t_per_obs) else 30
        T = max(T, 1.0)

        # Effective lambda0
        pc = pm_a.get(trig)
        a, c = (pc[0], pc[1]) if pc else (0.007, 20)
        l0 = a / (1.0 + c * max(d0, 0.0))
        vr = None
        if hasattr(s, 'vol_ratio_per_obs') and s.vol_ratio_per_obs is not None and last_idx < len(s.vol_ratio_per_obs):
            vr = float(s.vol_ratio_per_obs[last_idx])

        sp = SurvivalParams(lambda0=l0, gamma=params6.gamma, beta=params6.beta, beta_vol=params6.beta_vol)
        p = reversal_probability(d, T, sp, vol_ratio=vr)
        if p <= 0 or p >= 1:
            continue

        seq = s.sequence if hasattr(s, 'sequence') else ""
        if len(seq) >= 5:
            last_bit = seq[-1]; b3 = seq[2]
            actual = int(last_bit != b3)
        else:
            continue

        bin_id = int(np.clip(p * 10, 0, 9))
        b = bins.setdefault(bin_id, {"n": 0, "pos": 0})
        b["n"] += 1
        if actual:
            b["pos"] += 1
    result = {}
    for bid in sorted(bins.keys()):
        b = bins[bid]
        rate = b["pos"] / b["n"] if b["n"] > 0 else 0
        result[bid] = {"n": b["n"], "actual_rate": round(rate, 3), "expected": round((bid + 0.5) / 10, 3)}
    return result

# v6 calibration
print("\nV6 (ln(vr) + prefix lambda + beta_vol):")
v6 = cal_with_prefix(samples_valid, p6)
for bid in sorted(v6.keys()):
    b = v6[bid]
    print(f"  bin {bid} ({b['expected']:.1f}): n={b['n']:4d} actual_rate={b['actual_rate']:.3f}")

# v5 calibration (no vol_ratio)
print("\nV5 (no vol_ratio, scalar lambda):")
def cal_v5(samples):
    bins = {}
    sp5 = SurvivalParams(lambda0=0.00339, gamma=0.1, beta=-8.0, beta_vol=0.0)
    for s in samples:
        bl = s.baseline
        if bl <= 0:
            continue
        last_idx = len(s.obs_prices) - 1
        if last_idx < 1:
            continue
        d = abs(s.obs_prices[last_idx] - bl) / bl * 100.0
        T = s.t_per_obs[last_idx] if hasattr(s, 't_per_obs') and last_idx < len(s.t_per_obs) else 30
        T = max(T, 1.0)
        p = reversal_probability(d, T, sp5, vol_ratio=None)
        if p <= 0 or p >= 1:
            continue
        seq = s.sequence if hasattr(s, 'sequence') else ""
        if len(seq) >= 5:
            last_bit = seq[-1]; b3 = seq[2]
            actual = int(last_bit != b3)
        else:
            continue
        bin_id = int(np.clip(p * 10, 0, 9))
        b = bins.setdefault(bin_id, {"n": 0, "pos": 0})
        b["n"] += 1
        if actual:
            b["pos"] += 1
    result = {}
    for bid in sorted(bins.keys()):
        b = bins[bid]
        rate = b["pos"] / b["n"] if b["n"] > 0 else 0
        result[bid] = {"n": b["n"], "actual_rate": round(rate, 3), "expected": round((bid + 0.5) / 10, 3)}
    return result

v5 = cal_v5(samples_valid)
for bid in sorted(v5.keys()):
    b = v5[bid]
    print(f"  bin {bid} ({b['expected']:.1f}): n={b['n']:4d} actual_rate={b['actual_rate']:.3f}")

# Brier score comparison
def brier(samples, params, use_vr, use_prefix=False):
    errs = []
    for s in samples:
        bl = s.baseline
        if bl <= 0:
            continue
        last_idx = len(s.obs_prices) - 1
        if last_idx < 1:
            continue
        d = abs(s.obs_prices[last_idx] - bl) / bl * 100.0
        T = s.t_per_obs[last_idx] if hasattr(s, 't_per_obs') and last_idx < len(s.t_per_obs) else 30
        T = max(T, 1.0)

        seq_prefix = s.sequence if hasattr(s, 'sequence') else ""
        trig = seq_prefix[:3] if len(seq_prefix) >= 3 else "000"
        d0 = s.d0_abs_pct
        if use_prefix:
            pc = pm_a.get(trig)
            a, c = (pc[0], pc[1]) if pc else (0.007, 20)
            lambda0 = a / (1.0 + c * max(d0, 0.0))
        else:
            lambda0 = params.lambda0

        vr = None
        if use_vr and hasattr(s, 'vol_ratio_per_obs') and s.vol_ratio_per_obs is not None and last_idx < len(s.vol_ratio_per_obs):
            vr = float(s.vol_ratio_per_obs[last_idx])

        sp = SurvivalParams(lambda0=lambda0, gamma=params.gamma, beta=params.beta, beta_vol=params.beta_vol if use_vr else 0.0)
        try:
            p = reversal_probability(d, T, sp, vol_ratio=vr)
        except Exception:
            continue

        seq = s.sequence if hasattr(s, 'sequence') else ""
        if len(seq) >= 5:
            last_bit = seq[-1]; b3 = seq[2]
            actual = int(last_bit != b3)
        else:
            continue

        errs.append((actual - p) ** 2)

    return float(np.mean(errs)) if errs else -1

bs_v5 = brier(samples_valid, SurvivalParams(0.00339, 0.1, -8.0), False)
bs_v6 = brier(samples_valid, p6, True, use_prefix=True)
bs_v6_no_prefix = brier(samples_valid, p6, True, use_prefix=False)

print(f"\n{'='*60}")
print(f"Brier score (lower = better):")
print(f"  V5 (scalar, no vol_ratio):     {bs_v5:.4f}")
print(f"  V6 (ln(vr), prefix lambda):    {bs_v6:.4f}")
print(f"  V6 (ln(vr), scalar lambda):    {bs_v6_no_prefix:.4f}")
print(f"{'='*60}")

# Save v6 artifact
prefix_buckets = {}
for pref, bp in sorted(pm.buckets.items()):
    if len(pref) != 3:
        continue
    p_max = 1.0 - math.exp(-bp.a * 120.0 / 1.1)
    prefix_buckets[pref] = {"a": round(bp.a, 5), "c": round(bp.c, 4),
                            "event_rate": round(bp.event_rate, 3), "p_max": round(p_max, 4),
                            "n": bp.n_windows}

out = {
    "schema_version": "1.1",
    "trained_at": datetime.now(timezone.utc).isoformat(),
    "data_meta": {
        "n_total_windows": len(samples_train) + len(samples_valid),
        "n_train_windows": len(samples_train),
        "n_valid_windows": len(samples_valid),
        "obs_step_sec": OBS_STEP,
        "observed_from": "1s",
        "note": "ln(vr) vol_ratio. beta_vol={}".format(p6.beta_vol),
    },
    "params": {
        "lambda0": round(p6.lambda0, 6),
        "gamma": round(p6.gamma, 4),
        "beta": round(p6.beta, 4),
        "beta_vol": round(p6.beta_vol, 4),
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
    "calibration": {"baseline_prob": 0.3, "bin_table": []},
    "survival_model": {
        "global_dynamic": {"a": 0.007, "c": 20},
        "prefix_buckets": prefix_buckets,
        "source": "ln(vr) beta_vol={}".format(p6.beta_vol),
    },
}
Path("artifact_btc_v6.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\nSaved artifact_btc_v6.json with beta_vol={p6.beta_vol:.4f}")
