from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


def _window_start_ms(window_id: str) -> Optional[int]:
    if not window_id.startswith("w"):
        return None
    raw = window_id[1:]
    if not raw.isdigit():
        return None
    return int(raw)


def _fetch_binance_window_last_close(*, start_ms: int, end_ms: int) -> tuple[Optional[float], Optional[str]]:
    url = "https://api.binance.com/api/v3/klines"
    params = urllib.parse.urlencode({
        "symbol": "BTCUSDT",
        "interval": "1s",
        "startTime": str(start_ms),
        "endTime": str(end_ms),
        "limit": "1000",
    })
    req = urllib.request.Request(
        url=f"{url}?{params}",
        headers={"User-Agent": "TwinEngines-Calibrator/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return None, f"http_{e.code}"
    except Exception as e:
        return None, f"net_{type(e).__name__}"
    try:
        arr = json.loads(body)
    except Exception:
        return None, "bad_json"
    if not isinstance(arr, list) or not arr:
        return None, "empty_klines"
    last = arr[-1]
    if not isinstance(last, list) or len(last) < 5:
        return None, "bad_kline_row"
    try:
        return float(last[4]), None
    except Exception:
        return None, "bad_close"


def build_p_rev_time_calibration(
    *,
    shadow_signals_path: str,
    out_path: str,
    window_hours: float = 168.0,
    bucket_sec: int = 10,
    min_samples: int = 8,
    max_rows: int = 8000,
) -> dict[str, Any]:
    p = Path(shadow_signals_path)
    now_ms = int(time.time() * 1000)
    out: dict[str, Any] = {
        "generated_ts_ms": now_ms,
        "source_path": shadow_signals_path,
        "window_hours": float(window_hours),
        "bucket_sec": int(bucket_sec),
        "min_samples": int(min_samples),
        "sample_n": 0,
        "buckets": [],
        "notes": [],
    }
    if not p.is_file():
        out["notes"].append("shadow_signals_file_missing")
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        return out

    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    rows = lines[-max_rows:]
    since_ms = now_ms - int(window_hours * 3600 * 1000)
    agg: dict[int, dict[str, float]] = {}
    used = 0
    for ln in rows:
        try:
            e = json.loads(ln)
        except Exception:
            continue
        if not isinstance(e, dict):
            continue
        ts_ms = int(e.get("ts_ms") or 0)
        if ts_ms < since_ms:
            continue
        p_rev = e.get("p_rev")
        trigger = str(e.get("trigger_pattern") or "")
        baseline = e.get("baseline_price")
        window_id = str(e.get("window_id") or "")
        t_rem = e.get("t_remaining_sec")
        if not isinstance(p_rev, (int, float)):
            continue
        if not isinstance(baseline, (int, float)) or float(baseline) <= 0:
            continue
        if not isinstance(t_rem, (int, float)):
            continue
        ws = _window_start_ms(window_id)
        if ws is None:
            continue
        we = ws + 300_000 - 1
        if now_ms <= we:
            continue
        close_px, err = _fetch_binance_window_last_close(start_ms=ws, end_ms=we)
        if close_px is None or err is not None:
            continue
        actual = "UP" if close_px > float(baseline) else "DOWN"
        last_bit = trigger[-1] if len(trigger) >= 1 else "0"
        reversal_side = "DOWN" if last_bit == "1" else "UP"
        y = 1.0 if actual == reversal_side else 0.0
        bstart = int(max(0.0, float(t_rem)) // bucket_sec) * bucket_sec
        a = agg.setdefault(bstart, {"n": 0.0, "sum_p": 0.0, "sum_y": 0.0})
        a["n"] += 1.0
        a["sum_p"] += float(p_rev)
        a["sum_y"] += y
        used += 1

    buckets: list[dict[str, Any]] = []
    for bstart in sorted(agg.keys()):
        a = agg[bstart]
        n = int(a["n"])
        if n <= 0:
            continue
        mean_p = a["sum_p"] / a["n"]
        mean_y = a["sum_y"] / a["n"]
        buckets.append({
            "start_sec": int(bstart),
            "end_sec": int(bstart + bucket_sec),
            "n": n,
            "mean_pred_p_rev": mean_p,
            "mean_obs_reversal": mean_y,
            "delta": mean_y - mean_p,
            "active": bool(n >= min_samples),
        })
    out["sample_n"] = used
    out["buckets"] = buckets
    if used == 0:
        out["notes"].append("no_usable_samples")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


@dataclass
class PRevTimeBucketCalibrator:
    bucket_sec: int
    min_samples: int
    buckets: list[dict[str, Any]]

    @classmethod
    def from_file(cls, path: str) -> Optional["PRevTimeBucketCalibrator"]:
        p = Path(path)
        if not p.is_file():
            return None
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(obj, dict):
            return None
        bks = obj.get("buckets")
        if not isinstance(bks, list):
            return None
        return cls(
            bucket_sec=int(obj.get("bucket_sec") or 10),
            min_samples=int(obj.get("min_samples") or 8),
            buckets=[x for x in bks if isinstance(x, dict)],
        )

    def apply(self, *, p_raw: float, t_remaining_sec: float) -> tuple[float, Optional[dict[str, Any]]]:
        p = max(0.0, min(1.0, float(p_raw)))
        t = max(0.0, float(t_remaining_sec))
        hit = None
        for b in self.buckets:
            s = float(b.get("start_sec") or 0.0)
            e = float(b.get("end_sec") or (s + self.bucket_sec))
            if s <= t < e:
                hit = b
                break
        if hit is None:
            return p, None
        n = int(hit.get("n") or 0)
        if n < self.min_samples or not bool(hit.get("active")):
            return p, hit
        delta = float(hit.get("delta") or 0.0)
        # 小样本收缩，避免细分桶过度修正
        shrink = min(1.0, n / max(self.min_samples * 2.0, 1.0))
        p_adj = max(0.0, min(1.0, p + shrink * delta))
        return p_adj, hit
