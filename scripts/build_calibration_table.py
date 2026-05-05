#!/usr/bin/env python3
"""
Build per-prefix calibration table: p_rev_raw range → actual reversal rate.
Uses β=−3, β_vol=0.35, per-prefix (a,c) from prefix model, ln(vr).
"""
import sys, json, math, numpy as np
from pathlib import Path
from datetime import date
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.twinengines.data.binance_downloader import DownloadConfig, download_range
from src.twinengines.data.window import build_windows
from src.twinengines.model.prefix_survival import fit_prefix_dynamic_model
from src.twinengines.survival import SurvivalParams, reversal_probability

START = date(2026, 4, 1)
END   = date(2026, 5, 1)
SYM = "BTCUSDT"
CACHE = Path("data_cache")
OBS = 10

# ── fixed params for calibration ──
GAMMA = 0.1
BETA  = -3.0
BETA_VOL = 0.35
T_REF = 120.0
N_BINS = 10

print(f"Load: {START} -> {END}")
cfg_1m = DownloadConfig(symbol=SYM, interval="1m", cache_dir=CACHE)
cfg_1s = DownloadConfig(symbol=SYM, interval="1s", cache_dir=CACHE)
df_1m = download_range(cfg_1m, START, END, progress=True)
df_1s = download_range(cfg_1s, START, END, progress=True)

print("Build windows ...")
samples = build_windows(df_1m, bars_1s=df_1s, obs_step_sec=OBS,
                        trigger_mode="third_digit", obs_start_offset_minutes=3)
print(f"  {len(samples)} windows")

# Fit prefix model to get (a,c) per prefix
print("Fit prefix model ...")
pm, _ = fit_prefix_dynamic_model(samples, gamma=GAMMA, beta=BETA,
                                   global_a=0.007, global_c=20, min_n=15, shrink_l2=2.0)

prefix_a = {}
prefix_count = defaultdict(int)
for pref, bp in pm.buckets.items():
    if len(pref) == 3:
        prefix_a[pref] = (bp.a, bp.c, bp.event_rate)
        prefix_count[pref] = bp.n_windows

print(f"  Prefix buckets: {len(prefix_a)}")

# Collect all (prefix, p_rev_raw, actual_reversal) tuples
rows = []
no_lambda = 0
for s in samples:
    bl = s.baseline
    if bl <= 0:
        continue
    seq = s.sequence if hasattr(s, 'sequence') else ""
    if len(seq) < 5:
        continue

    trig = seq[:3]  # 3-bit prefix
    if trig not in prefix_a:
        continue

    a, c, _ = prefix_a[trig]
    d0 = s.d0_abs_pct
    l0 = a / (1.0 + c * max(d0, 0.0))

    last_idx = len(s.obs_prices) - 1
    if last_idx < 1:
        continue

    # Take samples at 3 different time points (early, mid, late) for more data
    for sample_idx in [last_idx // 3, 2 * last_idx // 3, last_idx]:
        if sample_idx >= len(s.obs_prices) or sample_idx < 1:
            continue
        d = abs(s.obs_prices[sample_idx] - bl) / bl * 100.0
        T = float(s.t_per_obs[sample_idx]) if hasattr(s, 't_per_obs') and sample_idx < len(s.t_per_obs) else 60.0
        T = max(T, 1.0)

        vr = None
        if hasattr(s, 'vol_ratio_per_obs') and s.vol_ratio_per_obs is not None and sample_idx < len(s.vol_ratio_per_obs):
            vr = float(s.vol_ratio_per_obs[sample_idx])

        sp = SurvivalParams(lambda0=l0, gamma=GAMMA, beta=BETA, beta_vol=BETA_VOL)
        p = reversal_probability(d, T, sp, vol_ratio=vr)
        if p <= 0 or p >= 1:
            continue

        last_bit = seq[-1]
        b3 = seq[2]
        actual = int(last_bit != b3)

        rows.append((trig, p, actual))

print(f"  Total rows: {len(rows)}")

# Build per-prefix calibration table
def build_table(rows_by_prefix, n_bins):
    table = {}
    for prefix in sorted(rows_by_prefix.keys()):
        rows = rows_by_prefix[prefix]
        if len(rows) < 50:
            continue
        ps = np.array([r[1] for r in rows])
        ys = np.array([r[2] for r in rows])

        # Equal-count bins (quantiles)
        try:
            edges = np.quantile(ps, np.linspace(0, 1, n_bins + 1))
        except Exception:
            edges = np.linspace(ps.min(), ps.max(), n_bins + 1)
        edges = np.unique(edges)
        if len(edges) < 3:
            edges = np.linspace(ps.min(), ps.max(), n_bins + 1)

        bins = []
        for i in range(len(edges) - 1):
            lo, hi = edges[i], edges[i+1]
            mask = (ps >= lo) & (ps < hi)
            n_mask = int(mask.sum())
            if n_mask < 5:
                continue
            actual_rate = float(ys[mask].mean())
            bins.append({
                "lo": round(float(lo), 4),
                "hi": round(float(hi), 4),
                "n": n_mask,
                "rate": round(actual_rate, 3),
            })

        if bins:
            table[prefix] = {
                "n_windows": prefix_count.get(prefix, 0),
                "a": round(prefix_a[prefix][0], 5),
                "c": round(prefix_a[prefix][1], 4),
                "event_rate": round(prefix_a[prefix][2], 3),
                "bins": bins,
            }

    return table

rows_by_prefix = defaultdict(list)
for trig, p, y in rows:
    rows_by_prefix[trig].append((trig, p, y))

table = build_table(rows_by_prefix, N_BINS)

# Print summary
print("\n" + "=" * 70)
print("Per-prefix calibration table:")
print("=" * 70)
for prefix in sorted(table.keys()):
    t = table[prefix]
    print(f"\n  {prefix} (event_rate={t['event_rate']:.3f}, a={t['a']:.5f}):")
    for b in t["bins"]:
        mid = round((b["lo"] + b["hi"]) / 2, 4)
        mark = ""
        if b["rate"] >= 0.5:
            mark = " <<< REVERSAL"
        print(f"    [{b['lo']:.4f}, {b['hi']:.4f}] n={b['n']:4d} rate={b['rate']:.3f}{mark}")

# Save
out_path = Path("data_runtime/calibration_table.json")
out_path.parent.mkdir(parents=True, exist_ok=True)
out = {
    "params": {"gamma": GAMMA, "beta": BETA, "beta_vol": BETA_VOL},
    "data": {f"trained_{START}_{END}": len(samples)},
    "table": table,
}
out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\nSaved: {out_path}")
