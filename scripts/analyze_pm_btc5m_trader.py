#!/usr/bin/env python3
"""
拉取某 Polymarket 地址在 BTC 5m Up/Down 市场的成交 + 已平仓记录，
并与 Binance BTCUSDT 同期 1m K 线对齐，输出可复现的统计规律（只读公开 API）。

胜率口径: 对同一 conditionId 下所有 closed-positions 行的 realizedPnl 求和，
sum>0.01 记为「该市场盈利」，sum<-0.01 为亏损，否则近似打平。
合计盈亏为上述 sum 再对所有 BTC5m 市场相加（与 PM 前端总账是否一致取决于 API 字段定义，可作交叉验证）。

用法:
  set PM_ADDR=0xeaca59cb5e10e0be128b005a0f84465d2ed80729
  python scripts/analyze_pm_btc5m_trader.py

环境变量:
  PM_ADDR   钱包地址（默认示例地址）
  MAX_TRADE_PAGES  每页 500，默认 30 页
  MAX_CLOSED_PAGES closed-positions 每页 50；默认 120 页（约 6000 条），设 0=一直拉到空页（最多 400 页防失控）
  SKIP_BINANCE=1  跳过币安请求（只算胜率/CSV 时更快）
  PM_ANALYZE_CSV=路径  写出每市场一行 CSV（utf-8）
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


PM_DATA = "https://data-api.polymarket.com"
BINANCE = "https://api.binance.com/api/v3/klines"


def _get(url: str, timeout: float = 60.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "TwinEngines-analyze/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_all_trades(addr: str, max_pages: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    limit = 500
    for page in range(max_pages):
        url = f"{PM_DATA}/trades?user={addr}&limit={limit}&offset={page * limit}"
        chunk = _get(url)
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < limit:
            break
        time.sleep(0.15)
    return out


def fetch_closed(addr: str, max_pages: int) -> list[dict[str, Any]]:
    """max_pages=0 表示拉到返回不足一页为止，硬上限 400 页。"""
    out: list[dict[str, Any]] = []
    limit = 50
    cap = 400 if max_pages == 0 else max_pages
    for page in range(cap):
        url = f"{PM_DATA}/closed-positions?user={addr}&limit={limit}&offset={page * limit}"
        chunk = _get(url)
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < limit:
            break
        time.sleep(0.12)
        if max_pages != 0 and page + 1 >= max_pages:
            break
    return out


def aggregate_closed_btc5m(
    closed: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """按 conditionId 汇总 BTC5m 已平仓：总 realizedPnl、结算胜出腿、slug。"""
    by: dict[str, dict[str, Any]] = {}
    for c in closed:
        if not is_btc5m(c):
            continue
        cid = (c.get("conditionId") or "").strip()
        if not cid:
            continue
        slug = c.get("slug") or ""
        if window_start_from_slug(slug) is None:
            continue
        if cid not in by:
            by[cid] = {
                "sum_pnl": 0.0,
                "winner": "",
                "slug": slug,
                "n_closed_rows": 0,
            }
        by[cid]["sum_pnl"] += float(c.get("realizedPnl") or 0)
        by[cid]["n_closed_rows"] += 1
        if slug:
            by[cid]["slug"] = slug
        if float(c.get("curPrice") or 0) >= 0.99:
            by[cid]["winner"] = c.get("outcome") or ""
    return by


def is_btc5m(t: dict[str, Any]) -> bool:
    slug = (t.get("slug") or t.get("eventSlug") or "").lower()
    return "btc-updown-5m-" in slug


def window_start_from_slug(slug: str) -> int | None:
    m = re.search(r"btc-updown-5m-(\d+)$", slug.lower())
    if not m:
        return None
    return int(m.group(1))


def binance_1m_window(start_sec: int) -> tuple[float, float, float]:
    """返回 (窗口首根 1m open, 窗口末根 close, 窗内 high-low 极差/首价)."""
    start_ms = start_sec * 1000
    end_ms = (start_sec + 300) * 1000
    url = (
        f"{BINANCE}?symbol=BTCUSDT&interval=1m"
        f"&startTime={start_ms}&endTime={end_ms - 1}&limit=10"
    )
    rows = _get(url)
    if not rows:
        return float("nan"), float("nan"), float("nan")
    o0 = float(rows[0][1])
    c_last = float(rows[-1][4])
    hi = max(float(r[2]) for r in rows)
    lo = min(float(r[3]) for r in rows)
    rng = (hi - lo) / o0 if o0 else float("nan")
    return o0, c_last, rng


@dataclass
class MarketAgg:
    condition_id: str
    slug: str
    window_start: int
    buys_up_usdc: float = 0.0
    buys_dn_usdc: float = 0.0
    sells_up_usdc: float = 0.0
    sells_dn_usdc: float = 0.0
    first_ts: int | None = None
    last_ts: int | None = None
    n_trades: int = 0

    def add(self, t: dict[str, Any]) -> None:
        ts = int(t["timestamp"])
        self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
        self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)
        self.n_trades += 1
        oc = t.get("outcome") or ""
        side = t.get("side") or ""
        sz = float(t["size"])
        pr = float(t["price"])
        usd = sz * pr
        if oc == "Up":
            if side == "BUY":
                self.buys_up_usdc += usd
            else:
                self.sells_up_usdc += usd
        elif oc == "Down":
            if side == "BUY":
                self.buys_dn_usdc += usd
            else:
                self.sells_dn_usdc += usd


def _row_from_agg(
    agg: MarketAgg,
    *,
    win_by_cid: dict[str, str],
    skip_binance: bool,
    cache: dict[int, tuple[float, float, float]],
) -> dict[str, Any]:
    ws = agg.window_start
    if not skip_binance:
        if ws not in cache:
            try:
                cache[ws] = binance_1m_window(ws)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                print(f"  [warn] binance fail ws={ws}: {e}", file=sys.stderr)
                cache[ws] = (float("nan"), float("nan"), float("nan"))
            time.sleep(0.08)
        o0, c1, _rng = cache[ws]
        btc_ret = (c1 - o0) / o0 if o0 and o0 == o0 else float("nan")
        btc_up = 1 if btc_ret > 1e-8 else (-1 if btc_ret < -1e-8 else 0)
    else:
        btc_ret = float("nan")
        btc_up = 0
    net_up = agg.buys_up_usdc - agg.sells_up_usdc
    net_dn = agg.buys_dn_usdc - agg.sells_dn_usdc
    tilt = net_up - net_dn
    winner = win_by_cid.get(agg.condition_id, "")
    return {
        "condition_id": agg.condition_id,
        "window_utc": datetime.fromtimestamp(ws, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "slug": agg.slug,
        "slug_tail": agg.slug.split("-")[-1] if agg.slug else "",
        "n_trades": agg.n_trades,
        "buy_up": round(agg.buys_up_usdc, 2),
        "buy_dn": round(agg.buys_dn_usdc, 2),
        "tilt_usdc": round(tilt, 2),
        "btc_ret_pct": round(btc_ret * 100, 4) if btc_ret == btc_ret else None,
        "btc_proxy_up": btc_up,
        "pm_winner_outcome": winner or None,
        "tilt_sign": 1 if tilt > 0.5 else (-1 if tilt < -0.5 else 0),
    }


def main() -> int:
    addr = (os.environ.get("PM_ADDR") or "0xeaca59cb5e10e0be128b005a0f84465d2ed80729").strip()
    max_trade_pages = int(os.environ.get("MAX_TRADE_PAGES", "30"))
    max_closed_pages = int(os.environ.get("MAX_CLOSED_PAGES", "120"))
    skip_binance = os.environ.get("SKIP_BINANCE", "").strip() == "1"
    csv_path = (os.environ.get("PM_ANALYZE_CSV") or "").strip()

    print(f"地址 {addr}")
    print("[1] 拉取 trades …")
    trades = fetch_all_trades(addr, max_trade_pages)
    btc_trades = [t for t in trades if is_btc5m(t)]
    print(f"  trades 总计 {len(trades)}，其中 btc-updown-5m: {len(btc_trades)}")

    by_cid: dict[str, MarketAgg] = {}
    for t in btc_trades:
        cid = t.get("conditionId") or ""
        if not cid:
            continue
        slug = t.get("slug") or t.get("eventSlug") or ""
        ws = window_start_from_slug(slug)
        if ws is None:
            continue
        if cid not in by_cid:
            by_cid[cid] = MarketAgg(condition_id=cid, slug=slug, window_start=ws)
        by_cid[cid].add(t)

    print(f"  独立 5m 市场数(conditionId): {len(by_cid)}")

    print("[2] 拉取 closed-positions …")
    closed = fetch_closed(addr, max_closed_pages)
    closed_btc5m = [c for c in closed if is_btc5m(c)]
    closed_agg = aggregate_closed_btc5m(closed)
    win_by_cid: dict[str, str] = {k: v["winner"] for k, v in closed_agg.items() if v.get("winner")}

    n_m_closed = len(closed_agg)
    wins = sum(1 for v in closed_agg.values() if v["sum_pnl"] > 0.01)
    losses = sum(1 for v in closed_agg.values() if v["sum_pnl"] < -0.01)
    flats = n_m_closed - wins - losses
    sum_pnl = sum(v["sum_pnl"] for v in closed_agg.values())
    denom = wins + losses
    wr_excl_flat = (wins / denom * 100) if denom else float("nan")
    wr_all = (wins / n_m_closed * 100) if n_m_closed else float("nan")

    print(f"  closed 原始条数 {len(closed)}，BTC5m 条数 {len(closed_btc5m)}")
    print(f"  按 conditionId 汇总后的 BTC5m 已结市场数: {n_m_closed}")
    print(
        f"  按市场总 realizedPnl: 盈利>{0.01} {wins} / 亏损<-0.01 {losses} / 近似平 {flats}；"
        f"合计盈亏 {sum_pnl:.2f} USDC"
    )
    print(f"  胜率(盈利市场/全部已结市场): {wr_all:.1f}%")
    print(f"  胜率(盈利/(盈+亏), 不含打平): {wr_excl_flat:.1f}%")

    print(
        f"  其中能标出 PM 胜出 outcome 的: {len(win_by_cid)} "
        f"(与 trades 有交集的见下文 tilt 分析)"
    )

    print(
        "[3] 对齐 Binance 1m …"
        + (" 已跳过 (SKIP_BINANCE=1)" if skip_binance else "（每窗口一次请求）")
    )
    cache: dict[int, tuple[float, float, float]] = {}
    rows_out: list[dict[str, Any]] = []
    for agg in sorted(by_cid.values(), key=lambda x: x.window_start):
        rows_out.append(_row_from_agg(agg, win_by_cid=win_by_cid, skip_binance=skip_binance, cache=cache))

    # —— 统计 ——
    both_buy = [r for r in rows_out if (r["buy_up"] or 0) > 0.5 and (r["buy_dn"] or 0) > 0.5]
    with_winner = [r for r in rows_out if r["pm_winner_outcome"]]

    def match_tilt_winner(r: dict[str, Any]) -> bool | None:
        w = r.get("pm_winner_outcome")
        if not w:
            return None
        ts = r.get("tilt_sign")
        if ts == 0:
            return None
        return (ts > 0 and w == "Up") or (ts < 0 and w == "Down")

    aligned = [r for r in with_winner if match_tilt_winner(r) is not None]
    correct = sum(1 for r in aligned if match_tilt_winner(r))

    print("\n========== 摘要 ==========")
    print(f"有成交的 BTC5m 窗口数: {len(rows_out)}")
    print(f"双腿 BUY 均 >0.5 USDC 的窗口数: {len(both_buy)}")
    print(f"closed-positions 能标出胜出 outcome 的窗口数: {len(with_winner)}")
    if aligned:
        print(f"有明确 tilt_sign 且能对照 PM 胜出腿的样本: {len(aligned)}，tilt 与胜出腿一致: {correct} ({correct/len(aligned):.1%})")

    btc_up_windows = [r for r in rows_out if r.get("btc_proxy_up") == 1]
    btc_dn_windows = [r for r in rows_out if r.get("btc_proxy_up") == -1]
    print(f"Binance 窗内涨跌 proxy: 涨 {len(btc_up_windows)} / 跌 {len(btc_dn_windows)} / 平 {len(rows_out)-len(btc_up_windows)-len(btc_dn_windows)}")

    if both_buy:
        avg_ratio = sum((b["buy_up"] / max(b["buy_dn"], 0.01)) for b in both_buy) / len(both_buy)
        print(f"双腿买入窗口: Up/Down 买入金额比均值 ≈ {avg_ratio:.3f}")

    print("\n========== 最近 12 个窗口（UTC 开始时间）==========")
    for r in rows_out[-12:]:
        print({k: v for k, v in r.items() if k != "condition_id"})

    if csv_path:
        def ws_for(cid: str) -> int:
            s = closed_agg.get(cid, {}).get("slug") or (by_cid[cid].slug if cid in by_cid else "")
            w0 = window_start_from_slug(s)
            return w0 if w0 is not None else 0

        all_cids = sorted(set(closed_agg.keys()) | set(by_cid.keys()), key=ws_for)
        fieldnames = [
            "condition_id",
            "window_utc",
            "slug",
            "sum_realized_pnl",
            "pm_winner",
            "n_closed_rows",
            "n_trades",
            "buy_up",
            "buy_dn",
            "tilt_usdc",
            "btc_ret_pct",
            "btc_proxy_up",
        ]
        n_csv = 0
        with open(csv_path, "w", newline="", encoding="utf-8") as fp:
            cw = csv.DictWriter(fp, fieldnames=fieldnames)
            cw.writeheader()
            for cid in all_cids:
                ca = closed_agg.get(cid, {})
                slug = ca.get("slug") or (by_cid[cid].slug if cid in by_cid else "")
                ws = window_start_from_slug(slug)
                if ws is None:
                    continue
                agg = by_cid.get(cid)
                if agg:
                    net_up = agg.buys_up_usdc - agg.sells_up_usdc
                    net_dn = agg.buys_dn_usdc - agg.sells_dn_usdc
                    tilt = net_up - net_dn
                    nu, nd = agg.buys_up_usdc, agg.buys_dn_usdc
                    nt = agg.n_trades
                else:
                    tilt = nu = nd = 0.0
                    nt = 0
                br: float | None = None
                bpu: int | str = ""
                if not skip_binance:
                    try:
                        if ws not in cache:
                            cache[ws] = binance_1m_window(ws)
                            time.sleep(0.08)
                        o0, c1, _ = cache[ws]
                        if o0 and o0 == o0:
                            br = round((c1 - o0) / o0 * 100, 4)
                            bpu = (
                                1
                                if (c1 - o0) / o0 > 1e-8
                                else (-1 if (c1 - o0) / o0 < -1e-8 else 0)
                            )
                    except Exception:
                        br, bpu = None, ""
                cw.writerow(
                    {
                        "condition_id": cid,
                        "window_utc": datetime.fromtimestamp(ws, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                        "slug": slug,
                        "sum_realized_pnl": round(ca.get("sum_pnl", 0.0), 4),
                        "pm_winner": ca.get("winner") or "",
                        "n_closed_rows": ca.get("n_closed_rows", 0),
                        "n_trades": nt,
                        "buy_up": round(nu, 2),
                        "buy_dn": round(nd, 2),
                        "tilt_usdc": round(tilt, 2),
                        "btc_ret_pct": br,
                        "btc_proxy_up": bpu,
                    }
                )
                n_csv += 1
        print(f"\n[CSV] 已写入 {csv_path} ，行数={n_csv}")

    print("\n========== 策略解读（基于以上公开数据，非投资建议）==========")
    print(
        "1) Polymarket 结算价源与 Binance 现货不完全一致，本脚本用 Binance 1m 仅作「同期波动」参考，"
        "不能等同 PM 判定 Up/Down。"
    )
    print(
        "2) 若双腿 BUY 常见且 Up/Down 金额比接近常数（~1.02），更像「固定比例对冲/做市库存」，"
        "而非完全对称套利；需结合 MERGE/REDEEM 与 SELL 时点才能还原完整 PnL。"
    )
    print(
        "3) 若要严谨复现其策略，应：全量 trades + activity（SPLIT/MERGE/REDEEM）+ 子图/链上；"
        "Binance 数据建议用 1s 与 PM 窗口精确对齐，并记录盘口 mid 而非仅 OHLC。"
    )
    print(
        "4) 「算不算策略」: 若行为在统计上呈现固定仓位结构、择时或对冲规则，即属于交易策略范畴；"
        "本脚本只量化结果分布，不推断是否违规或是否可复刻。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
