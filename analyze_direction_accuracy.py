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

rev_trades = []
trend_trades = []

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
    elif ev_trend > min_ev:
        action = 'trend'
    else:
        continue
    
    if action == 'reversal':
        fee = fee_rev
        if s['actual_reversal']:
            pnl = (1.0 - real_ask_rev) - fee
            direction_correct = True
        else:
            pnl = -real_ask_rev - fee
            direction_correct = False
        rev_trades.append({
            'pnl': pnl, 'ask': real_ask_rev, 'p_raw': s['p_raw'], 'p_adj': p_adj,
            'direction_correct': direction_correct, 'actual_rev': s['actual_reversal']
        })
    else:
        fee = fee_trend
        if not s['actual_reversal']:
            pnl = (1.0 - real_ask_trend) - fee
            direction_correct = True
        else:
            pnl = -real_ask_trend - fee
            direction_correct = False
        trend_trades.append({
            'pnl': pnl, 'ask': real_ask_trend, 'p_raw': s['p_raw'], 'p_adj': p_adj,
            'direction_correct': direction_correct, 'actual_rev': s['actual_reversal']
        })

print(f"k={k}, m={m}, min_ev={min_ev}")
print()

# 反转单分析
print("="*60)
print("REVERSAL TRADES")
print("="*60)
n = len(rev_trades)
n_correct = sum(1 for t in rev_trades if t['direction_correct'])
n_pnl_pos = sum(1 for t in rev_trades if t['pnl'] > 0)
total_pnl = sum(t['pnl'] for t in rev_trades)
print(f"Total: {n}")
print(f"Direction correct: {n_correct}/{n} = {n_correct/n*100:.1f}%" if n > 0 else "")
print(f"PnL positive: {n_pnl_pos}/{n} = {n_pnl_pos/n*100:.1f}%" if n > 0 else "")
print(f"Total PnL: {total_pnl:+.2f}")
if n > 0:
    asks = [t['ask'] for t in rev_trades]
    print(f"Ask range: {min(asks):.2f} ~ {max(asks):.2f}, avg: {sum(asks)/len(asks):.2f}")
print()

# 顺势单分析
print("="*60)
print("TREND TRADES")
print("="*60)
n = len(trend_trades)
n_correct = sum(1 for t in trend_trades if t['direction_correct'])
n_pnl_pos = sum(1 for t in trend_trades if t['pnl'] > 0)
total_pnl = sum(t['pnl'] for t in trend_trades)
print(f"Total: {n}")
print(f"Direction correct: {n_correct}/{n} = {n_correct/n*100:.1f}%" if n > 0 else "")
print(f"PnL positive: {n_pnl_pos}/{n} = {n_pnl_pos/n*100:.1f}%" if n > 0 else "")
print(f"Total PnL: {total_pnl:+.2f}")
if n > 0:
    asks = [t['ask'] for t in trend_trades]
    print(f"Ask range: {min(asks):.2f} ~ {max(asks):.2f}, avg: {sum(asks)/len(asks):.2f}")

# 按ask区间分析顺势单
print()
print("="*60)
print("TREND TRADES by ask range:")
print("="*60)
bins = [(0, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 1.0)]
for lo, hi in bins:
    subset = [t for t in trend_trades if lo <= t['ask'] < hi]
    if not subset:
        continue
    n_correct = sum(1 for t in subset if t['direction_correct'])
    n_pos = sum(1 for t in subset if t['pnl'] > 0)
    total = sum(t['pnl'] for t in subset)
    print(f"  ask {lo:.1f}-{hi:.1f}: {len(subset):4d} trades, "
          f"direction correct: {n_correct}/{len(subset)}={n_correct/len(subset)*100:5.1f}%, "
          f"PnL positive: {n_pos}/{len(subset)}={n_pos/len(subset)*100:5.1f}%, "
          f"total PnL: {total:+.2f}")

# 同样分析反转单
print()
print("="*60)
print("REVERSAL TRADES by ask range:")
print("="*60)
for lo, hi in bins:
    subset = [t for t in rev_trades if lo <= t['ask'] < hi]
    if not subset:
        continue
    n_correct = sum(1 for t in subset if t['direction_correct'])
    n_pos = sum(1 for t in subset if t['pnl'] > 0)
    total = sum(t['pnl'] for t in subset)
    print(f"  ask {lo:.1f}-{hi:.1f}: {len(subset):4d} trades, "
          f"direction correct: {n_correct}/{len(subset)}={n_correct/len(subset)*100:5.1f}%, "
          f"PnL positive: {n_pos}/{len(subset)}={n_pos/len(subset)*100:5.1f}%, "
          f"total PnL: {total:+.2f}")
