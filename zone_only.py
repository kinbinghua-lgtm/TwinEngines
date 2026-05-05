import json
import numpy as np
from pathlib import Path
from train_simple_model import polymarket_fee

predictions = json.loads(Path('data_runtime/all_predictions.json').read_text())

valid = []
for pred in predictions:
    if pred.get('actual_reversal') is None:
        continue
    bid_rev = pred.get('bid_rev')
    bid_trend = pred.get('bid_trend')
    if bid_rev is None or bid_trend is None:
        continue
    ask_trend = min(0.99, bid_trend + 0.01)
    ask_rev = min(0.99, bid_rev + 0.01)
    bid_rev_size = pred.get('bid_rev_size') or 0.0
    bid_trend_size = pred.get('bid_trend_size') or 0.0
    if bid_rev_size < 10 or bid_trend_size < 10:
        continue
    valid.append({
        'condition_id': pred['condition_id'],
        'elapsed_sec': pred['elapsed_sec'],
        'p_raw': pred['p_raw'],
        'ask_trend': ask_trend,
        'ask_rev': ask_rev,
        'actual_reversal': pred['actual_reversal']
    })

samples = [p for p in valid if 180 <= p['elapsed_sec'] <= 300]

SLIPPAGE = 0.01

def correct_p(k, m, p_raw, ask_rev):
    penalty = 1.0 + m * (ask_rev - 0.5) ** 2
    denominator = 1.0 + k * ask_rev * penalty
    return np.clip(p_raw / denominator, 0.01, 0.99)

k, m, min_ev = 3.5, 5.0, 0.15

# 定义4个盈利区间
profitable_zones = [
    ('reversal', 0.10, 0.15),  # 反转 ask=0.10~0.15
    ('trend', 0.50, 0.60),     # 顺势 ask=0.50~0.60
    ('both', 0.02, 0.05),      # 低价彩票（反转+顺势）
    ('reversal', 0.05, 0.10),  # 反转 ask=0.05~0.10
]

all_trades = []

for s in samples:
    p_adj = correct_p(k, m, s['p_raw'], s['ask_rev'])
    real_ask_rev = min(0.99, s['ask_rev'] + SLIPPAGE)
    real_ask_trend = min(0.99, s['ask_trend'] + SLIPPAGE)
    fee_rev = polymarket_fee(real_ask_rev)
    fee_trend = polymarket_fee(real_ask_trend)
    
    ev_rev = p_adj * (1.0 - real_ask_rev) - (1.0 - p_adj) * real_ask_rev - fee_rev
    ev_trend = (1.0 - p_adj) * (1.0 - real_ask_trend) - p_adj * real_ask_trend - fee_trend
    
    if ev_rev > min_ev and ev_rev >= ev_trend:
        action = 'reversal'
        ask = real_ask_rev
        fee = fee_rev
    elif ev_trend > min_ev:
        action = 'trend'
        ask = real_ask_trend
        fee = fee_trend
    else:
        continue
    
    direction_correct = s['actual_reversal'] if action == 'reversal' else not s['actual_reversal']
    pnl = (1.0 - ask) - fee if direction_correct else -ask - fee
    
    # 判断是否在4个盈利区间内
    in_zone = False
    zone_name = ""
    for zone_action, lo, hi in profitable_zones:
        if zone_action == 'both' or zone_action == action:
            if lo <= ask < hi:
                in_zone = True
                zone_name = f"{action} {lo:.2f}-{hi:.2f}"
                break
    
    all_trades.append({
        'action': action,
        'ask': ask,
        'pnl': pnl,
        'direction_correct': direction_correct,
        'in_zone': in_zone,
        'zone_name': zone_name,
    })

# 只做4个盈利区间的交易
zone_trades = [t for t in all_trades if t['in_zone']]
other_trades = [t for t in all_trades if not t['in_zone']]

print("="*80)
print("只做4个盈利区间 vs 全做 vs 排除4个盈利区间")
print("="*80)
print()

for label, trades in [("4个盈利区间", zone_trades), ("全部交易", all_trades), ("其他区间", other_trades)]:
    n = len(trades)
    if n == 0:
        print(f"{label}: 0 trades")
        continue
    n_correct = sum(1 for t in trades if t['direction_correct'])
    n_pnl_pos = sum(1 for t in trades if t['pnl'] > 0)
    total_pnl = sum(t['pnl'] for t in trades)
    avg_pnl = total_pnl / n
    
    print(f"{label:>15}: {n:4d} trades, "
          f"direction correct: {n_correct}/{n}={n_correct/n*100:5.1f}%, "
          f"PnL positive: {n_pnl_pos}/{n}={n_pnl_pos/n*100:5.1f}%, "
          f"Total PnL: {total_pnl:+.2f}, Avg PnL: {avg_pnl:+.4f}")

print()
print()

# 按zone分组统计
print("="*80)
print("4个盈利区间明细")
print("="*80)
print()

from collections import defaultdict
zone_groups = defaultdict(list)
for t in zone_trades:
    zone_groups[t['zone_name']].append(t)

total_n = 0
total_correct = 0
total_pnl_pos = 0
grand_pnl = 0

for zone_name in sorted(zone_groups.keys()):
    trades = zone_groups[zone_name]
    n = len(trades)
    n_correct = sum(1 for t in trades if t['direction_correct'])
    n_pnl_pos = sum(1 for t in trades if t['pnl'] > 0)
    total_pnl = sum(t['pnl'] for t in trades)
    avg_pnl = total_pnl / n
    
    total_n += n
    total_correct += n_correct
    total_pnl_pos += n_pnl_pos
    grand_pnl += total_pnl
    
    print(f"  {zone_name:>25}: {n:4d} trades, "
          f"correct: {n_correct}/{n}={n_correct/n*100:5.1f}%, "
          f"PnL+: {n_pnl_pos}/{n}={n_pnl_pos/n*100:5.1f}%, "
          f"PnL: {total_pnl:+.2f}, Avg: {avg_pnl:+.4f}")

print(f"  {'-'*70}")
print(f"  {'TOTAL':>25}: {total_n:4d} trades, "
      f"correct: {total_correct}/{total_n}={total_correct/total_n*100:5.1f}%, "
      f"PnL+: {total_pnl_pos}/{total_n}={total_pnl_pos/total_n*100:5.1f}%, "
      f"PnL: {grand_pnl:+.2f}, Avg: {grand_pnl/total_n:+.4f}")

# 按窗口统计
print()
print()
print("="*80)
print("4个盈利区间 - 按窗口统计")
print("="*80)
print()

windows_traded = set()
windows_won = set()
window_pnl = defaultdict(float)

for t in zone_trades:
    # 需要找到对应的condition_id... 用样本里的
    pass

# 用全量样本计算窗口触发率
all_cids = set(p['condition_id'] for p in samples)
print(f"Total windows with predictions: {len(all_cids)}")

# 看zone_trades覆盖了多少样本占比
print(f"Zone trades: {len(zone_trades)} / {len(all_trades)} = {len(zone_trades)/len(all_trades)*100:.1f}% of all trades")
