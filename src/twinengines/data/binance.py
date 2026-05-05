"""
Binance 分钟线数据加载与合成数据生成。

- 真实数据：从 CSV 加载，列 = ["open_time","open","high","low","close",...]
- 合成数据：构造受控的趋势/反转模式，便于离线测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class MinuteBars:
    """分钟K线数据封装。"""

    df: pd.DataFrame

    def __post_init__(self) -> None:
        required = {"open_time", "open", "high", "low", "close"}
        missing = required - set(self.df.columns)
        if missing:
            raise ValueError(f"missing columns: {sorted(missing)}")
        if not self.df["open_time"].is_monotonic_increasing:
            raise ValueError("open_time must be monotonically increasing")

    def __len__(self) -> int:
        return len(self.df)


def load_binance_csv(path: str | Path) -> MinuteBars:
    """
    读取 Binance 分钟线 CSV。
    兼容官方 historical klines 的 12 列格式（无表头）和带表头的简化格式。
    """
    p = Path(path)
    head = pd.read_csv(p, nrows=1, header=None)
    if head.shape[1] >= 12:
        cols = [
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_volume",
            "trades",
            "taker_buy_base",
            "taker_buy_quote",
            "ignore",
        ]
        df = pd.read_csv(p, header=None, names=cols)
    else:
        df = pd.read_csv(p)
    df = df[["open_time", "open", "high", "low", "close"]].copy()
    df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce").astype("Int64")
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna().reset_index(drop=True)
    return MinuteBars(df=df)


def synthesize_minute_bars(
    *,
    n_minutes: int = 60 * 24 * 30,
    start_price: float = 60_000.0,
    minute_drift: float = 0.0,
    minute_vol: float = 0.0008,
    seed: int = 7,
) -> MinuteBars:
    """
    生成几何布朗分钟线（用于单测、回测demo）。
    返回的 DataFrame 列 = open_time(ms), open, high, low, close。
    """
    rng = np.random.default_rng(seed)
    rets = rng.normal(loc=minute_drift, scale=minute_vol, size=n_minutes)
    log_prices = np.log(start_price) + np.cumsum(rets)
    closes = np.exp(log_prices)
    opens = np.concatenate([[start_price], closes[:-1]])
    highs = np.maximum(opens, closes) * (1.0 + np.abs(rng.normal(0, minute_vol / 2, n_minutes)))
    lows = np.minimum(opens, closes) * (1.0 - np.abs(rng.normal(0, minute_vol / 2, n_minutes)))
    open_time = (np.arange(n_minutes) * 60_000).astype(np.int64)

    df = pd.DataFrame(
        {
            "open_time": open_time,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
        }
    )
    return MinuteBars(df=df)
