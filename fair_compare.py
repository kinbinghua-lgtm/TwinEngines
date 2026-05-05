import json
from pathlib import Path
from train_simple_model import polymarket_fee, TrainingSample

predictions = json.loads(Path('data_runtime/all_predictions.json').read_text())

# 转换数据
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

# 按窗口分组，按窗口70/30切分
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

# 用全部94个窗口测试（不分训练/验证）
all_samples = [p for preds in windows.values() for p in preds]

print(f"Windows: {len(windows)}, Samples: {len(all_samples)}")
print()


def simulate(samples, p_func, min_ev, label):
    """
    p_func: 接受p_raw和ask_rev，返回p_adj
    """
    total_pnl = 0
    n_trades = 0
    n_wins = 0
    n_rev = 0
    n_trend = 0
    
    for s in samples:
        p_adj = p_func(s['p_raw'], s['ask_rev'])
        ask_rev = s['ask_rev']
        ask_trend = s['ask_trend']
        
        fee_rev = polymarket_fee(ask_rev)
        fee_trend = polymarket_fee(ask_trend)
        
        ev_rev = p_adj * (1.0 - ask_rev) - (1.0 - p_adj) * ask_rev - fee_rev
        ev_trend = (1.0 - p_adj) * (1.0 - ask_trend) - p_adj * ask_trend - fee_trend
        
        if ev_rev > min_ev and ev_rev >= ev_trend:
            action = 'reversal'
            ev = ev_rev
        elif ev_trend > min_ev:
            action = 'trend'
            ev = ev_trend
        else:
            continue
        
        n_trades += 1
        
        if action == 'reversal':
            n_rev += 1
            fee = fee_rev
            if s['actual_reversal']:
                pnl = (1.0 - ask_rev) - fee
            else:
                pnl = -ask_rev - fee
        else:
            n_trend += 1
            fee = fee_trend
            if not s['actual_reversal']:
                pnl = (1.0 - ask_trend) - fee
            else:
                pnl = -ask_trend - fee
        
        total_pnl += pnl
        if pnl > 0:
            n_wins += 1
    
    win_rate = n_wins / n_trades * 100 if n_trades > 0 else 0
    avg_pnl = total_pnl / n_trades if n_trades > 0 else 0
    
    print(f"{label:>30s} | PnL={total_pnl:+7.2f} Trades={n_trades:4d} WinRate={win_rate:5.1f}% AvgPnL={avg_pnl:+.4f} | Rev={n_rev} Trend={n_trend}")


import numpy as np

# 修正函数
def correct_p(k, m):
    def f(p_raw, ask_rev):
        penalty = 1.0 + m * (ask_rev - 0.5) ** 2
        denominator = 1.0 + k * ask_rev * penalty
        return np.clip(p_raw / denominator, 0.01, 0.99)
    return f

def no_correction(p_raw, ask_rev):
    return p_raw

print("="*100)
print("正确PnL公式（用ask买入, 输了全亏, 含手续费）全量94窗口测试")
print("="*100)
print()

for min_ev in [0.0, 0.02, 0.03, 0.05, 0.08, 0.10, 0.15]:
    print(f"--- min_ev = {min_ev:.2f} ---")
    simulate(all_samples, no_correction, min_ev, f"无修正 (p_raw)")
    simulate(all_samples, correct_p(3.0, 4.0), min_ev, f"修正 k=3.0,m=4.0")
    simulate(all_samples, correct_p(3.5, 5.0), min_ev, f"修正 k=3.5,m=5.0")
    simulate(all_samples, correct_p(2.5, 10.0), min_ev, f"修正 k=2.5,m=10.0")
    print()
