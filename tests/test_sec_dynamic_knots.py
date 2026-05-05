"""秒级 ds/ts 构造（无网络）。"""

from __future__ import annotations

import numpy as np

from twinengines.live.third_digit_sec_dynamic import build_ds_ts_from_sec_knots


def test_build_ds_ts_monotone_decreasing_ts():
    end_ts = 1_000_000_000_000 + 5 * 60_000
    obs_start = 1_000_000_000_000 + 3 * 60_000
    now_ms = obs_start + 45_000
    knots = [
        (obs_start, 0.05),
        (obs_start + 15_000, 0.06),
        (obs_start + 30_000, 0.055),
    ]
    ds, ts = build_ds_ts_from_sec_knots(knots_ms_d=knots, end_ts_ms=end_ts, now_ms=now_ms)
    assert ds.shape == ts.shape
    assert np.all(np.diff(ts) <= 1e-9)
