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
    
    if action == 'reversal':
        direction_correct = s['actual_reversal']
    else:
        direction_correct = not s['actual_reversal']
    
    if direction_correct:
        pnl = (1.0 - ask) - fee
    else:
        pnl = -ask - fee
    
    all_trades.append({
        'action': action,
        'ask': ask,
        'pnl': pnl,
        'direction_correct': direction_correct,
        'p_raw': s['p_raw'],
        'p_adj': p_adj,
        'actual_rev': s['actual_reversal'],
        'win_pnl': (1.0 - ask) - fee,
        'lose_pnl': -ask - fee,
    })

bins = [(0.02, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.30), (0.30, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80)]

print(f"k={k}, m={m}, min_ev={min_ev}")
print()

for action_type in ['reversal', 'trend']:
    trades = [t for t in all_trades if t['action'] == action_type]
    label = "REVERSAL" if action_type == 'reversal' else "TREND"
    
    print("="*100)
    print(f"{label} TRADES - detailed by ask range")
    print("="*100)
    print(f"{'Ask':>10} | {'Count':>5} | {'Correct':>8} {'Rate':>6} | {'AvgWin':>7} {'AvgLose':>8} | {'TotalPnL':>8} {'AvgPnL':>7}")
    print("-" * 85)
    
    for lo, hi in bins:
        subset = [t for t in trades if lo <= t['ask'] < hi]
        if not subset:
            continue
        n = len(subset)
        n_correct = sum(1 for t in subset if t['direction_correct'])
        rate = n_correct / n * 100
        total_pnl = sum(t['pnl'] for t in subset)
        avg_pnl = total_pnl / n
        avg_win = sum(t['win_pnl'] for t in subset) / n
        avg_lose = sum(t['lose_pnl'] for t in subset) / n
        
        print(f" {lo:.2f}-{hi:.2f} | {n:5d} | {n_correct:4d}/{n:<3d} {rate:5.1f}% | {avg_win:+7.2f} {avg_lose:+8.2f} | {total_pnl:+8.2f} {avg_pnl:+7.4f}")
    
    # total
    n = len(trades)
    if n > 0:
        n_correct = sum(1 for t in trades if t['direction_correct'])
        total_pnl = sum(t['pnl'] for t in trades)
        print("-" * 85)
        print(f"     TOTAL | {n:5d} | {n_correct:4d}/{n:<3d} {n_correct/n*100:5.1f}% | {'':>7} {'':>8} | {total_pnl:+8.2f} {total_pnl/n:+7.4f}")
    print()
