"""
币安历史 K 线下载器（公开镜像 data.binance.vision，无需 API key）。

支持:
    - 1m klines （全币对长期可用）
    - 1s klines （主流币 BTCUSDT 自 2023-01 起按日提供）
    - aggTrades （毫秒级成交，按需）

下载策略:
    - 增量：缺失的日分片才会下载
    - 解压后 -> parquet，二次访问直接读 parquet
    - 失败重试 (max_retries) + 超时

公开镜像目录格式:
    /data/spot/daily/klines/{SYMBOL}/{INTERVAL}/{SYMBOL}-{INTERVAL}-{YYYY-MM-DD}.zip
"""

from __future__ import annotations

import io
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

Interval = Literal["1s", "1m"]

BINANCE_BASE: str = "https://data.binance.vision"
KLINE_COLUMNS: list[str] = [
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


@dataclass(frozen=True)
class DownloadConfig:
    symbol: str = "BTCUSDT"
    interval: Interval = "1m"
    cache_dir: Path = Path("data_cache")
    timeout_sec: float = 30.0
    max_retries: int = 3
    retry_backoff_sec: float = 2.0


def _kline_url(symbol: str, interval: Interval, day: date) -> str:
    return (
        f"{BINANCE_BASE}/data/spot/daily/klines/{symbol}/{interval}/"
        f"{symbol}-{interval}-{day.isoformat()}.zip"
    )


def _cache_path(cfg: DownloadConfig, day: date) -> Path:
    return cfg.cache_dir / cfg.symbol / cfg.interval / f"{day.isoformat()}.parquet"


def _http_get(url: str, timeout: float, retries: int, backoff: float) -> bytes:
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = Request(url, headers={"User-Agent": "twinengines-downloader/1.0"})
            with urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (HTTPError, URLError, TimeoutError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(backoff * (2**attempt))
            else:
                break
    raise RuntimeError(f"failed to GET {url}: {last_err}")


def _parse_zip_to_df(payload: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as f:
            df = pd.read_csv(f, header=None, names=KLINE_COLUMNS)
    for c in ("open", "high", "low", "close", "volume", "quote_volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce").astype("int64")
    df["close_time"] = pd.to_numeric(df["close_time"], errors="coerce").astype("int64")
    return df.dropna(subset=["open", "close"]).reset_index(drop=True)


def download_day(cfg: DownloadConfig, day: date, *, force: bool = False) -> pd.DataFrame:
    """
    下载并缓存单日 K 线。返回该日 DataFrame；当日数据可能尚未发布时返回空 DataFrame。
    """
    cache_file = _cache_path(cfg, day)
    if cache_file.exists() and not force:
        return pd.read_parquet(cache_file)

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    url = _kline_url(cfg.symbol, cfg.interval, day)
    try:
        payload = _http_get(url, cfg.timeout_sec, cfg.max_retries, cfg.retry_backoff_sec)
    except RuntimeError as e:
        if "404" in str(e):
            return pd.DataFrame(columns=KLINE_COLUMNS)
        raise
    df = _parse_zip_to_df(payload)
    df.to_parquet(cache_file, index=False)
    return df


def _date_range(start: date, end: date) -> Iterable[date]:
    cur = start
    while cur <= end:
        yield cur
        cur = cur + timedelta(days=1)


def download_range(
    cfg: DownloadConfig,
    start: date,
    end: date,
    *,
    progress: bool = True,
) -> pd.DataFrame:
    """
    下载 [start, end] 区间所有日分片并合并为单个 DataFrame。
    缺失日（未发布或下架）会被静默跳过。
    """
    parts: list[pd.DataFrame] = []
    days = list(_date_range(start, end))
    for i, d in enumerate(days):
        df = download_day(cfg, d)
        if not df.empty:
            parts.append(df)
        if progress:
            print(f"  [{i+1}/{len(days)}] {d.isoformat()}  rows={len(df)}", flush=True)
    if not parts:
        return pd.DataFrame(columns=KLINE_COLUMNS)
    out = pd.concat(parts, ignore_index=True)
    out = out.sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)
    return out


def parse_iso_date(s: str) -> date:
    return datetime.fromisoformat(s).date()


def yesterday_utc() -> date:
    return (datetime.now(timezone.utc) - timedelta(days=1)).date()
