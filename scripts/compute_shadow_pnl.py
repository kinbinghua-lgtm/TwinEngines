#!/usr/bin/env python3
"""回算影子订单 PnL: 从 shadow_orders.jsonl 读取模拟单, 查 Binance 窗口结果."""
import sys, json, urllib.request
from pathlib import Path
from collections import defaultdict

def fetch_window_result(window_start_ms: int) -> tuple[str, float, float] | None:
    """返回 (5-bit序列, baseline, close_m5) 或 None."""
    url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&startTime={window_start_ms}&limit=5"
    req = urllib.request.Request(url, headers={"User-Agent": "TwinEngines/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        if len(data) < 5:
            return None
        baseline = float(data[0][1])
        closes = [float(k[4]) for k in data]
        seq = "".join("1" if c > baseline else "0" for c in closes)
        return seq, baseline, closes[4]
    except Exception:
        return None

def compute_pnl(order: dict, window_seq: str | None) -> dict | None:
    """计算模拟单的理论 PnL."""
    if window_seq is None or len(window_seq) < 5:
        return None
    trig_dir = order.get("trigger_direction", "")
    best_dir = order.get("best_dir", "")
    ask_up = float(order.get("ask_up") or 0.5)
    ask_dn = float(order.get("ask_down") or 0.5)
    if best_dir is None:
        return None

    b3 = window_seq[2]
    last = window_seq[-1]
    actual_up = (last == "1")
    actual_dir = "up" if actual_up else "down"

    # Determine if we won
    if best_dir == actual_dir:
        won = True
        stake = 1.0
        if best_dir == "up":
            raw_pnl = 1.0 - ask_up
        else:
            raw_pnl = 1.0 - ask_dn
    else:
        won = False
        if best_dir == "up":
            raw_pnl = -ask_up
        else:
            raw_pnl = -ask_dn

    fee = 0.005 * (ask_up if best_dir == "up" else ask_dn) * (1 - (ask_up if best_dir == "up" else ask_dn))
    gas = 0.02
    net_pnl = raw_pnl - fee - gas

    return {
        "seq": window_seq,
        "best_dir": best_dir,
        "actual_dir": actual_dir,
        "won": won,
        "raw_pnl": round(net_pnl, 4),
        "fee": round(fee, 4),
    }

def main():
    path = Path("logs/shadow_orders.jsonl")
    if not path.exists():
        print(f"{path} not found")
        return

    orders = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                orders.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    print(f"Loaded {len(orders)} simulated orders")

    # Group by window
    by_window = defaultdict(list)
    for o in orders:
        wid = o.get("window_id", "")
        by_window[wid].append(o)

    # Fetch results for each window
    cache = {}
    for wid in sorted(by_window.keys()):
        if not wid.startswith("w") or not wid[1:].isdigit():
            continue
        ws = int(wid[1:])
        seq = cache.get(ws)
        if seq is None:
            result = fetch_window_result(ws)
            if result:
                seq = result[0]
                cache[ws] = seq
            else:
                continue

        # Tag each order with PnL
        for o in by_window[wid]:
            pnl_info = compute_pnl(o, seq)
            if pnl_info:
                o["pnl"] = pnl_info

    # Save enriched orders
    out_path = Path("logs/shadow_pnl_report.jsonl")
    with open(out_path, "w", encoding="utf-8") as f:
        for o in orders:
            if "pnl" in o:
                f.write(json.dumps(o, ensure_ascii=False) + "\n")
    print(f"Saved {sum(1 for o in orders if 'pnl' in o)} enriched orders to {out_path}")

    # Summary
    won_orders = [o for o in orders if o.get("pnl", {}).get("won")]
    lost_orders = [o for o in orders if o.get("pnl", {}) and not o["pnl"]["won"]]
    total_pnl = sum(o["pnl"]["raw_pnl"] for o in orders if o.get("pnl"))

    print(f"\nSummary:")
    print(f"  Total simulated orders: {len(orders)}")
    print(f"  With PnL data: {len(won_orders) + len(lost_orders)}")
    print(f"  Won: {len(won_orders)} ({len(won_orders)/max(len(won_orders)+len(lost_orders),1)*100:.1f}%)")
    print(f"  Lost: {len(lost_orders)}")
    print(f"  Net PnL: ${total_pnl:.4f} (per $1 stake)")

if __name__ == "__main__":
    main()
