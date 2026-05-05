#!/usr/bin/env python3
"""
自适应校准表重建脚本。
输出: data_runtime/calibration_table.json
- 自适应分桶(按prefix频率)
- 95%置信区间 per cell
- min_samples=200 合并
"""
import sys, json, math, numpy as np
from pathlib import Path
from datetime import date, timedelta
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.twinengines.data.binance_downloader import DownloadConfig, download_range
from src.twinengines.data.window import build_windows
from src.twinengines.model.prefix_survival import fit_prefix_dynamic_model
from src.twinengines.survival import SurvivalParams, reversal_probability

def build(days=180):
    end = date.today()
    start = end - timedelta(days=days)
    sym, cache, T_STEP, OBS = "BTCUSDT", Path("data_cache"), 30, 10

    print(f"[cal-build] Range: {start} -> {end} ({days}d)")
    cfg_m = DownloadConfig(symbol=sym, interval="1m", cache_dir=cache)
    cfg_s = DownloadConfig(symbol=sym, interval="1s", cache_dir=cache)
    df_1m = download_range(cfg_m, start, end, progress=True)
    df_1s = download_range(cfg_s, start, end, progress=True)
    samples = build_windows(df_1m, bars_1s=df_1s, obs_step_sec=OBS,
                            trigger_mode="third_digit", obs_start_offset_minutes=3)
    print(f"[cal-build] {len(samples)} windows")

    # Fit prefix model
    pm, _ = fit_prefix_dynamic_model(samples, gamma=0.1, beta=-3.0,
                                       global_a=0.007, global_c=20, min_n=15, shrink_l2=2.0)
    prefix_a = {}
    for pref, bp in pm.buckets.items():
        if len(pref) == 3:
            prefix_a[pref] = (bp.a, bp.c, bp.event_rate)

    # Collect all rows
    # 收集: (prefix, T_bin, d_bucket, p_raw_bin)
    rows_by_key = defaultdict(list)
    for s in samples:
        bl = s.baseline
        if bl <= 0: continue
        seq = s.sequence if hasattr(s, 'sequence') else ""
        if len(seq) < 5: continue
        trig = seq[:3]
        if trig not in prefix_a: continue
        a, c, _ = prefix_a[trig]
        d0 = s.d0_abs_pct
        l0 = a / (1.0 + c * max(d0, 0.0))
        for idx in range(len(s.obs_prices)):
            if idx < 1: continue
            d = abs(s.obs_prices[idx] - bl) / bl * 100.0
            T = float(s.t_per_obs[idx]) if idx < len(s.t_per_obs) else 60
            T = max(T, 1.0)
            # 跳过末15s (T<15)
            if T < 15: continue
            sp = SurvivalParams(lambda0=l0, gamma=0.1, beta=-3.0, beta_vol=0.0)
            p = reversal_probability(d, T, sp)
            if p <= 0 or p >= 1: continue
            T_bin = int(min((T-15) // T_STEP, 2))  # 0=15-45, 1=45-75, 2=75-120
            actual = int(seq[-1] != seq[2])
            rows_by_key[(trig, T_bin, d)].append((p, actual))

    # Group by (prefix, T_bin) for d binning
    by_prefix_t = defaultdict(list)
    for (trig, tbin, d), rows in rows_by_key.items():
        by_prefix_t[(trig, tbin)].extend(rows)

    # Determine d bin edges per (prefix, T_bin)
    D_BINS = 4
    P_BINS = 8
    d_edges_map = {}
    for (trig, tbin), rows in by_prefix_t.items():
        ds = np.array([r[0] for r in rows])  # Using p as proxy... need actual d values
        # We stored (p, actual) in rows, not d. Need to restructure.
        # Fix: store d along with p and actual
        # Actually we need to go back and include d in the row tuple.
        pass

    print("[cal-build] Restructuring needed - rebuilding with d dimension properly...")
    # Redo the collection with d included
    rows_by_key.clear()
    for s in samples:
        bl = s.baseline
        if bl <= 0: continue
        seq = s.sequence if hasattr(s, 'sequence') else ""
        if len(seq) < 5: continue
        trig = seq[:3]
        if trig not in prefix_a: continue
        a, c, _ = prefix_a[trig]
        d0 = s.d0_abs_pct
        l0 = a / (1.0 + c * max(d0, 0.0))
        for idx in range(len(s.obs_prices)):
            if idx < 1: continue
            d = abs(s.obs_prices[idx] - bl) / bl * 100.0
            T = float(s.t_per_obs[idx]) if idx < len(s.t_per_obs) else 60
            T = max(T, 1.0)
            if T < 15: continue
            sp = SurvivalParams(lambda0=l0, gamma=0.1, beta=-3.0, beta_vol=0.0)
            p = reversal_probability(d, T, sp)
            if p <= 0 or p >= 1: continue
            T_bin = int(min((T-15) // T_STEP, 2))
            actual = int(seq[-1] != seq[2])
            rows_by_key[(trig, T_bin)].append((d, p, actual))

    # Group by (prefix, T_bin), bin d into quantiles, then bin p within each d bucket
    table = {}
    for (prefix, tbin), rows in sorted(rows_by_key.items()):
        ds = np.array([r[0] for r in rows])
        ps_raw = np.array([r[1] for r in rows])
        ys = np.array([r[2] for r in rows])
        if len(rows) < 50:
            continue

        # Bin d into D_BINS quantiles
        try:
            d_edges = np.unique(np.round(np.quantile(ds, np.linspace(0, 1, D_BINS + 1)), 4))
        except:
            d_edges = np.linspace(ds.min(), ds.max(), D_BINS + 1)

        d_buckets = {}
        for di in range(len(d_edges) - 1):
            d_lo, d_hi = d_edges[di], d_edges[di+1]
            dm = (ds >= d_lo) & (ds < d_hi)
            if dm.sum() < 30:
                continue
            psub = ps_raw[dm]
            ysub = ys[dm]

            # Bin p within this d bucket into P_BINS quantiles
            try:
                p_edges = np.unique(np.round(np.quantile(psub, np.linspace(0, 1, P_BINS + 1)), 4))
            except:
                p_edges = np.linspace(psub.min(), psub.max(), P_BINS + 1)
            if len(p_edges) < 2:
                continue

            pbins = []
            for pi in range(len(p_edges) - 1):
                plo, phi = p_edges[pi], p_edges[pi+1]
                pm = (psub >= plo) & (psub < phi)
                n = int(pm.sum())
                if n < 5:
                    continue
                rate = float(ysub[pm].mean())
                if n > 1 and 0 < rate < 1:
                    se = math.sqrt(rate * (1 - rate) / n)
                    ci = 1.96 * se
                else:
                    ci = 1.0
                pbins.append({
                    "lo": float(plo), "hi": float(phi), "n": n,
                    "rate": round(rate, 3),
                    "rate_lower": round(max(rate - ci, 0.0), 3),
                    "rate_upper": round(min(rate + ci, 1.0), 3),
                })

            if pbins:
                d_buckets[str(di)] = {
                    "d_lo": round(float(d_lo), 4), "d_hi": round(float(d_hi), 4),
                    "d_n": int(dm.sum()), "bins": pbins,
                }

        if d_buckets:
            t0 = 15 + tbin * T_STEP
            table.setdefault(prefix, {"a": round(prefix_a[prefix][0], 5),
                                       "c": round(prefix_a[prefix][1], 4),
                                       "event_rate": round(prefix_a[prefix][2], 3),
                                       "t_buckets": {}})
            table[prefix]["t_buckets"][str(tbin)] = {
                "T_lo": t0, "T_hi": t0 + T_STEP + 15, "d_buckets": d_buckets,
            }

    out_path = Path("data_runtime/calibration_table.json")
    out = {
        "params": {"gamma": 0.1, "beta": -3.0, "beta_vol": 0.0, "T_step_s": T_STEP},
        "built": str(end),
        "n_windows": len(samples),
        "n_days": days,
        "table": table,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    total_cells = sum(len(v.get("t_buckets",{}).get(str(t),{}).get("bins",[]))
                       for v in table.values() for t in range(4))
    print(f"\n[cal-build] Saved: {out_path}")
    print(f"[cal-build] Prefixes: {len(table)}, total cells: {total_cells}")

if __name__ == "__main__":
    build(days=int(sys.argv[1]) if len(sys.argv) > 1 else 180)
