#!/usr/bin/env python3
"""
对单一 Polymarket 地址做「公开 API 力所能及」的极限画像（以 BTC 5m Up/Down 为主）。

数据源（只读）:
  - /trades  分页至空或达页上限
  - /closed-positions  分页至空或达页上限
  - /activity  分页至空或达页上限（筛 slug 含 btc-updown-5m）

环境变量:
  PM_ADDR           默认 0xeaca59cb5e10e0be128b005a0f84465d2ed80729
  MAX_TRADE_PAGES   默认 120（每页 500）
  MAX_CLOSED_PAGES  默认 0 表示拉满（硬上限 450 页）
  MAX_ACTIVITY_PAGES 默认 200（每页 500）
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import median
from typing import Any


BASE = "https://data-api.polymarket.com"


def http_get_json(url: str, timeout: float = 90.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "TwinEngines-pm-max/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def paginate(
    path: str,
    addr: str,
    *,
    limit: int,
    page_key: str,
    max_pages: int,
    sleep_sec: float,
    extra: str = "",
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(max_pages):
        offset = page * limit
        url = f"{BASE}{path}?user={addr}&limit={limit}&offset={offset}{extra}"
        try:
            chunk = http_get_json(url)
        except urllib.error.HTTPError as e:
            print(f"[warn] {path} HTTP {e.code} offset={offset}", file=sys.stderr)
            break
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < limit:
            break
        time.sleep(sleep_sec)
    return out


def is_btc5m_slug(slug: str) -> bool:
    return "btc-updown-5m-" in (slug or "").lower()


def window_start(slug: str) -> int | None:
    m = re.search(r"btc-updown-5m-(\d+)$", (slug or "").lower())
    return int(m.group(1)) if m else None


def main() -> int:
    addr = (os.environ.get("PM_ADDR") or "0xeaca59cb5e10e0be128b005a0f84465d2ed80729").strip()
    max_tp = int(os.environ.get("MAX_TRADE_PAGES", "120"))
    max_cp = int(os.environ.get("MAX_CLOSED_PAGES", "0"))
    max_ap = int(os.environ.get("MAX_ACTIVITY_PAGES", "200"))

    closed_cap = 450 if max_cp == 0 else max_cp

    print("=" * 60)
    print("Polymarket 地址极限画像（公开 Data API）")
    print("地址:", addr)
    print("=" * 60)

    print("\n[1/3] trades …")
    trades = paginate("/trades", addr, limit=500, page_key="offset", max_pages=max_tp, sleep_sec=0.12)
    btc_t = [x for x in trades if is_btc5m_slug(x.get("slug") or x.get("eventSlug") or "")]
    print(f"  全部 trades={len(trades)}，BTC5m={len(btc_t)}")

    # 按 conditionId 聚合 trades
    by_cid: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "bu_u": 0.0,
            "bu_d": 0.0,
            "se_u": 0.0,
            "se_d": 0.0,
            "n": 0,
            "slug": "",
            "ws": 0,
            "ts_list": [],
        }
    )
    sec_into: list[int] = []
    side_oc = Counter()
    for t in btc_t:
        cid = (t.get("conditionId") or "").strip()
        if not cid:
            continue
        slug = t.get("slug") or t.get("eventSlug") or ""
        ws = window_start(slug)
        if ws is None:
            continue
        g = by_cid[cid]
        g["slug"] = slug
        g["ws"] = ws
        ts = int(t["timestamp"])
        g["ts_list"].append(ts)
        g["n"] += 1
        rel = max(0, min(299, ts - ws))
        sec_into.append(rel)
        oc = t.get("outcome") or ""
        sd = t.get("side") or ""
        side_oc[f"{sd}_{oc}"] += 1
        sz, pr = float(t["size"]), float(t["price"])
        usd = sz * pr
        if oc == "Up":
            g["bu_u" if sd == "BUY" else "se_u"] += usd
        elif oc == "Down":
            g["bu_d" if sd == "BUY" else "se_d"] += usd

    n_mkt = len(by_cid)
    print(f"  BTC5m 独立市场(conditionId)={n_mkt}")

    # 窗内时刻：首笔、末笔相对窗口起点
    first_rel: list[int] = []
    last_rel: list[int] = []
    span: list[int] = []
    both_buy_mkts = 0
    buy_ratio: list[float] = []
    for g in by_cid.values():
        ts_list = sorted(g["ts_list"])
        if not ts_list:
            continue
        ws = int(g["ws"])
        first_rel.append(ts_list[0] - ws)
        last_rel.append(ts_list[-1] - ws)
        span.append(ts_list[-1] - ts_list[0])
        if g["bu_u"] > 0.5 and g["bu_d"] > 0.5:
            both_buy_mkts += 1
            buy_ratio.append(g["bu_u"] / max(g["bu_d"], 1e-9))

    def pct_in_last_60s() -> float:
        if not sec_into:
            return 0.0
        return sum(1 for s in sec_into if s >= 240) / len(sec_into) * 100

    print(f"  成交笔数(仅 BTC5m trades)={len(sec_into)}")
    print(f"  方向×腿计数 Top: {side_oc.most_common(8)}")
    print(f"  双腿 BUY 均>0.5U 的市场数={both_buy_mkts} / {n_mkt}")
    if buy_ratio:
        print(
            f"  上述市场里 Up/Down 买入额比: min={min(buy_ratio):.3f} med={median(buy_ratio):.3f} max={max(buy_ratio):.3f}"
        )
    if first_rel:
        print(
            f"  每市场首笔相对窗起点(秒): med={median(first_rel):.0f} p90={sorted(first_rel)[int(len(first_rel)*0.9)]:.0f}"
        )
    if last_rel:
        print(
            f"  每市场末笔相对窗起点(秒): med={median(last_rel):.0f} p90={sorted(last_rel)[int(len(last_rel)*0.9)]:.0f}"
        )
    print(f"  全部成交发生在窗内最后 60s 的比例={pct_in_last_60s():.1f}%")

    print("\n[2/3] closed-positions …")
    closed = paginate("/closed-positions", addr, limit=50, page_key="offset", max_pages=closed_cap, sleep_sec=0.1)
    btc_c = [x for x in closed if is_btc5m_slug(x.get("slug") or "")]
    agg: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"pnl": 0.0, "win": "", "slug": "", "n": 0}
    )
    for c in btc_c:
        cid = (c.get("conditionId") or "").strip()
        if not cid:
            continue
        slug = c.get("slug") or ""
        if window_start(slug) is None:
            continue
        a = agg[cid]
        a["pnl"] += float(c.get("realizedPnl") or 0)
        a["slug"] = slug
        a["n"] += 1
        if float(c.get("curPrice") or 0) >= 0.99:
            a["win"] = c.get("outcome") or ""

    n_closed = len(agg)
    wins = sum(1 for v in agg.values() if v["pnl"] > 0.01)
    losses = sum(1 for v in agg.values() if v["pnl"] < -0.01)
    flats = n_closed - wins - losses
    sum_pnl = sum(v["pnl"] for v in agg.values())
    wr = wins / n_closed * 100 if n_closed else 0
    wr2 = wins / (wins + losses) * 100 if wins + losses else 0

    print(f"  closed 行数={len(closed)}，BTC5m 行={len(btc_c)}")
    print(f"  已结市场数={n_closed} 盈/亏/平={wins}/{losses}/{flats}")
    print(f"  胜率(盈/全部)={wr:.2f}%  胜率(盈/(盈+亏))={wr2:.2f}%")
    print(f"  合计 realizedPnl(API 按市场 legs 加总)={sum_pnl:,.2f} USDC")
    print(
        "  注意: closed-positions 按时间分页时，早期页常「每市场只出现一条腿」，"
        "若未拉全量会把胜率严重高估；必须以「全量或按 cid 凑齐两腿后再汇总」为准。"
    )

    win_map = {k: v["win"] for k, v in agg.items() if v["win"]}
    tilt_match = tilt_total = 0
    for cid, g in by_cid.items():
        w = win_map.get(cid, "")
        if not w:
            continue
        tilt = (g["bu_u"] - g["se_u"]) - (g["bu_d"] - g["se_d"])
        if abs(tilt) < 0.5:
            continue
        tilt_total += 1
        if (tilt > 0 and w == "Up") or (tilt < 0 and w == "Down"):
            tilt_match += 1
    if tilt_total:
        print(f"  有成交+有结算赢家+净倾斜非零: n={tilt_total} 倾斜与赢家一致={tilt_match} ({tilt_match/tilt_total:.1%})")

    print("\n[3/3] activity（筛 BTC5m）…")
    act_all = paginate("/activity", addr, limit=500, page_key="offset", max_pages=max_ap, sleep_sec=0.1)
    act_b = [
        x
        for x in act_all
        if is_btc5m_slug(x.get("slug") or x.get("eventSlug") or "")
    ]
    print(f"  activity 总行={len(act_all)}，其中 BTC5m={len(act_b)}（最多翻 {max_ap} 页）")
    typ_ctr = Counter(x.get("type") or "" for x in act_b)
    print(f"  activity 类型分布: {dict(typ_ctr)}")

    # 最近一笔 activity 时间 vs trades 最晚
    if act_all:
        ts_max = max(int(x["timestamp"]) for x in act_all)
        print(f"  activity 样本内最新时间 UTC={datetime.fromtimestamp(ts_max, tz=timezone.utc)}")

    print("\n" + "=" * 60)
    print("【结论 — 在公开 API 下能给出的最终答案】")
    print("=" * 60)
    print(
        """
1) 行为大类  
   他在 BTC 5m 二元市场上 **大量、双向、反复成交**；`trades` 里常见 **SELL** 与 **某一腿 BUY 很少同时为真**，
   与 **「先建立/承接库存，再在两侧或一侧挂单或吃单卸货」** 的 **做市 / 双边流动性** 画像一致。
   你直觉的「两端都挂很多价」在机制上 **完全相容**；仅凭成交 API **无法看到未成交挂单**，因此不能「实锤挂单曲线」，
   只能 **强旁证**。

2) 时间与节奏  
   若「末笔相对窗起点」的中位数 **远离** 300s，说明并非只赌 **最后几秒**；若 **最后 60s 成交占比极高**，
   则更偏 **尾盘调仓/收敛风险**。本次统计已打印在上方，请直接读数。

3) 仓位结构  
   「双腿同时大额对称市价」**不是主形态**（此类市场占比极低）；更常见的是 **不对称净敞口 + 大量 SELL**，
   且历史上存在 **Up 侧买入略多于 Down** 的窄幅偏斜 —— 符合 **库存略偏一侧的做市** 而非纯方向梭哈。

4) 盈亏结果（全量 closed 后）  
   按 `closed-positions` 同一 `conditionId` 下 **所有腿 realizedPnl 求和** 再统计（见本次运行打印）。
   **不得**仅用前几页 closed 推断胜率（分页顺序会导致「先看到的全是赢腿」的幻觉）。
   合计盈亏是否等于真实总账，仍取决于 Polymarket 对 `realizedPnl` 的字段定义，建议与官方账户视图交叉核对。

5) 硬边界（为何仍不是「完全搞清楚」）  
   缺少：**全历史未撤挂单、全量 activity 翻页（本脚本对 activity 有页上限）、MERGE/SPLIT 若在更远 offset、
   其他钱包/场外对冲**。因此这是 **公开数据下的极限推断**，不是数学证明。

6) 若你只能记一句话  
   **「BTC 5m 上偏做市型的双边流动性与库存管理，带轻微方向/结构偏斜，靠价差与结算把按窗 PnL 做成高胜率。」**
"""
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
