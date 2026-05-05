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

windows = {}
for p in valid:
    cid = p['condition_id']
    if cid not in windows:
        windows[cid] = []
    windows[cid].append(p)

for cid in windows:
    windows[cid].sort(key=lambda x: x['elapsed_sec'])
    windows[cid] = [p for p in windows[cid] if 180 <= p['elapsed_sec'] <= 300]

windows = {cid: preds for cid, preds in windows.items() if len(preds) > 0}

window_ids = list(windows.keys())
n_train = int(len(window_ids) * 0.7)
train_window_ids = set(window_ids[:n_train])
val_window_ids = set(window_ids[n_train:])

train_samples = [p for p in valid if p['condition_id'] in train_window_ids and 180 <= p['elapsed_sec'] <= 300]
val_samples = [p for p in valid if p['condition_id'] in val_window_ids and 180 <= p['elapsed_sec'] <= 300]

print(f"Train: {len(train_window_ids)} windows, {len(train_samples)} samples")
print(f"Val: {len(val_window_ids)} windows, {len(val_samples)} samples")
print()

SLIPPAGE = 0.01

def simulate(samples, p_func, min_ev):
    total_pnl = 0
    n_trades = 0
    n_wins = 0
    n_rev = 0
    n_trend = 0
    
    for s in samples:
        p_adj = p_func(s['p_raw'], s['ask_rev'])
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
        
        n_trades += 1
        if action == 'reversal':
            n_rev += 1
            fee = fee_rev
            pnl = (1.0 - real_ask_rev) - fee if s['actual_reversal'] else -real_ask_rev - fee
        else:
            n_trend += 1
            fee = fee_trend
            pnl = (1.0 - real_ask_trend) - fee if not s['actual_reversal'] else -real_ask_trend - fee
        
        total_pnl += pnl
        if pnl > 0:
            n_wins += 1
    
    win_rate = n_wins / n_trades * 100 if n_trades > 0 else 0
    avg_pnl = total_pnl / n_trades if n_trades > 0 else 0
    return {
        'pnl': total_pnl, 'trades': n_trades, 'wins': n_wins,
        'win_rate': win_rate, 'avg_pnl': avg_pnl,
        'n_rev': n_rev, 'n_trend': n_trend
    }

def correct_p(k, m):
    def f(p_raw, ask_rev):
        penalty = 1.0 + m * (ask_rev - 0.5) ** 2
        denominator = 1.0 + k * ask_rev * penalty
        return np.clip(p_raw / denominator, 0.01, 0.99)
    return f

def no_correction(p_raw, ask_rev):
    return p_raw

k_range = [0.0, 0.3, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0, 8.0, 10.0]
m_range = [0.0, 0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0]
min_ev_range = [0.0, 0.03, 0.05, 0.08, 0.10, 0.12, 0.15, 0.20]

results = []
for min_ev in min_ev_range:
    for k in k_range:
        for m in m_range:
            if k == 0 and m > 0:
                continue
            p_func = correct_p(k, m) if k > 0 else no_correction
            train_r = simulate(train_samples, p_func, min_ev)
            val_r = simulate(val_samples, p_func, min_ev)
            results.append({
                'k': k, 'm': m, 'min_ev': min_ev,
                'train': train_r, 'val': val_r
            })

# 只看训练集和验证集都赚钱的
both_positive = [r for r in results if r['train']['pnl'] > 0 and r['val']['pnl'] > 0]

print(f"Total combinations: {len(results)}")
print(f"Both train & val positive: {len(both_positive)}")
print()

if both_positive:
    print("="*110)
    print("BOTH TRAIN & VAL POSITIVE - sorted by Val PnL:")
    print("="*110)
    print(f"{'k':>5} {'m':>5} {'minEV':>5} | {'TrPnL':>7} {'TrTrd':>5} {'TrWR%':>5} {'TrRev':>5} {'TrTrd':>5} | {'VaPnL':>7} {'VaTrd':>5} {'VaWR%':>5} {'VaRev':>5} {'VaTrd':>5}")
    print("-" * 100)
    
    for r in sorted(both_positive, key=lambda x: -x['val']['pnl'])[:30]:
        t = r['train']
        v = r['val']
        print(f"{r['k']:5.1f} {r['m']:5.1f} {r['min_ev']:5.2f} | "
              f"{t['pnl']:+7.2f} {t['trades']:5d} {t['win_rate']:5.1f} {t['n_rev']:5d} {t['n_trend']:5d} | "
              f"{v['pnl']:+7.2f} {v['trades']:5d} {v['win_rate']:5.1f} {v['n_rev']:5d} {v['n_trend']:5d}")
    
    print()
    print("="*110)
    print("BOTH POSITIVE - sorted by COMBINED (Train + Val) PnL:")
    print("="*110)
    print(f"{'k':>5} {'m':>5} {'minEV':>5} | {'TrPnL':>7} {'TrTrd':>5} {'TrWR%':>5} {'TrRev':>5} {'TrTrd':>5} | {'VaPnL':>7} {'VaTrd':>5} {'VaWR%':>5} {'VaRev':>5} {'VaTrd':>5} | {'Total':>7}")
    print("-" * 110)
    
    for r in sorted(both_positive, key=lambda x: -(x['train']['pnl'] + x['val']['pnl']))[:20]:
        t = r['train']
        v = r['val']
        total = t['pnl'] + v['pnl']
        print(f"{r['k']:5.1f} {r['m']:5.1f} {r['min_ev']:5.2f} | "
              f"{t['pnl']:+7.2f} {t['trades']:5d} {t['win_rate']:5.1f} {t['n_rev']:5d} {t['n_trend']:5d} | "
              f"{v['pnl']:+7.2f} {v['trades']:5d} {v['win_rate']:5.1f} {v['n_rev']:5d} {v['n_trend']:5d} | "
              f"{total:+7.2f}")
else:
    print("NO combinations with both train & val positive!")
    print()
    print("Best train PnL combinations:")
    for r in sorted(results, key=lambda x: -x['train']['pnl'])[:10]:
        t = r['train']
        v = r['val']
        print(f"k={r['k']:.1f} m={r['m']:.1f} minEV={r['min_ev']:.2f} | Train={t['pnl']:+.2f} Val={v['pnl']:+.2f}")
