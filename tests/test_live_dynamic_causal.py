"""动态因果分钟路径单元测试（无网络）。"""

from __future__ import annotations

import numpy as np

from twinengines.live.third_digit_naked import (
    ThirdDigitDynamicParams,
    closed_minute_bars_since_window_start,
    live_causal_ds_ts_minute_path,
    naked_predict_live_dynamic,
)


def test_closed_minute_bars_monotonic():
    w = 1_700_000_000_000  # arbitrary ms
    assert closed_minute_bars_since_window_start(window_start_ms=w, now_ms=w) == 0
    assert closed_minute_bars_since_window_start(window_start_ms=w, now_ms=w + 179_999) == 2
    assert closed_minute_bars_since_window_start(window_start_ms=w, now_ms=w + 180_000) == 3
    assert closed_minute_bars_since_window_start(window_start_ms=w, now_ms=w + 300_000) == 5


def test_live_ds_ts_shapes_and_monotone_ts():
    baseline = 100_000.0
    # 5 closes with mild moves
    closes = [100_000.0, 100_050.0, 100_080.0, 100_070.0, 100_090.0]
    w0 = 1_700_000_000_000
    end_ts = w0 + 5 * 60_000
    obs_start = w0 + 3 * 60_000
    T4 = w0 + 4 * 60_000

    # minute 3 closed, minute 4 not: single segment
    now1 = obs_start + 15_000
    out1 = live_causal_ds_ts_minute_path(
        baseline=baseline,
        closes=closes,
        window_start_ms=w0,
        end_ts_ms=end_ts,
        now_ms=now1,
        n_closed_minutes=3,
    )
    assert out1 is not None
    ds1, ts1, m1 = out1
    assert ds1.shape == (1,) and ts1.shape == (1,)
    assert m1["path_stage"] == "min3_only_dynamic_time"

    # minute 4 closed
    now2 = T4 + 10_000
    out2 = live_causal_ds_ts_minute_path(
        baseline=baseline,
        closes=closes,
        window_start_ms=w0,
        end_ts_ms=end_ts,
        now_ms=now2,
        n_closed_minutes=4,
    )
    assert out2 is not None
    ds2, ts2, m2 = out2
    assert ds2.shape == (3,) and ts2.shape == (3,)
    assert m2["path_stage"] == "min4_closed"
    assert bool(np.all(np.diff(ts2) <= 0)), "ts must be non-increasing"

    # minute 5 closed
    now3 = w0 + 5 * 60_000 - 1_000
    out3 = live_causal_ds_ts_minute_path(
        baseline=baseline,
        closes=closes,
        window_start_ms=w0,
        end_ts_ms=end_ts,
        now_ms=now3,
        n_closed_minutes=5,
    )
    assert out3 is not None
    ds3, ts3, m3 = out3
    assert ds3.shape == (3,) and ts3.shape == (3,)
    assert m3["path_stage"] == "min5_closed"
    assert bool(np.all(np.diff(ts3) <= 0))


def test_naked_predict_live_dynamic_smoke():
    p = ThirdDigitDynamicParams(gamma=0.1, beta=-8.0, dynamic_a=0.01, dynamic_c=14.0)
    baseline = 100_000.0
    closes = [100_000.0, 100_050.0, 100_080.0, 100_070.0, 100_090.0]
    w0 = 1_700_000_000_000
    end_ts = w0 + 5 * 60_000
    now_ms = w0 + 3 * 60_000 + 25_000
    r = naked_predict_live_dynamic(
        baseline=baseline,
        closes=closes,
        window_start_ms=w0,
        end_ts_ms=end_ts,
        now_ms=now_ms,
        n_closed_minutes=3,
        params=p,
    )
    assert r is not None
    assert "p_up" in r and "H" in r and r["dynamic_causal"] is True
    assert 0.0 <= float(r["p_up"]) <= 1.0
