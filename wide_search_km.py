import json
from pathlib import Path
from train_correction_model import CorrectionModel

predictions = json.loads(Path('data_runtime/all_predictions.json').read_text())

valid = [p for p in predictions if p['actual_reversal'] is not None
         and (p.get('bid_rev_size') or 0) >= 10
         and (p.get('bid_trend_size') or 0) >= 10]

windows = {}
for p in valid:
    cid = p['condition_id']
    if cid not in windows:
        windows[cid] = []
    windows[cid].append(p)

window_ids = list(windows.keys())
n_train = int(len(window_ids) * 0.7)
val_window_ids = set(window_ids[n_train:])
val_samples = [p for p in valid if p['condition_id'] in val_window_ids]
val_windows = {}
for p in val_samples:
    cid = p['condition_id']
    if cid not in val_windows:
        val_windows[cid] = []
    val_windows[cid].append(p)

print(f"Validation: {len(val_windows)} windows, {len(val_samples)} samples")
print()

k_range = [2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0, 12.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0, 75.0, 100.0]
m_range = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0, 20.0, 30.0, 50.0, 75.0, 100.0]

results = []

for k in k_range:
    for m in m_range:
        model = CorrectionModel(k=k, m=m, min_ev=0.03)
        total_pnl = 0.0
        n_trades = 0
        n_wins = 0
        windows_with_trades = 0
        
        for cid, preds in val_windows.items():
            window_traded = False
            for p in preds:
                odds_rev = 1 - p['bid_rev']
                odds_trend = 1 - p['bid_trend']
                action, ev = model.make_decision(p['p_raw'], odds_rev, odds_trend, 0.01)
                if action != 'pass':
                    n_trades += 1
                    window_traded = True
                    if action == 'reversal':
                        pnl = odds_rev - 0.01 if p['actual_reversal'] else -(1 - odds_rev) - 0.01
                    else:
                        pnl = odds_trend - 0.01 if not p['actual_reversal'] else -(1 - odds_trend) - 0.01
                    total_pnl += pnl
                    if pnl > 0:
                        n_wins += 1
            if window_traded:
                windows_with_trades += 1
        
        if n_trades > 0:
            win_rate = n_wins / n_trades * 100
            avg_pnl = total_pnl / n_trades
            trigger_pct = windows_with_trades / len(val_windows) * 100
            results.append({
                'k': k, 'm': m,
                'pnl': total_pnl, 'trades': n_trades, 'wins': n_wins,
                'win_rate': win_rate, 'avg_pnl': avg_pnl,
                'windows_with_trades': windows_with_trades,
                'trigger_pct': trigger_pct
            })

# 按胜率降序，只看盈利为正的
profitable = [r for r in results if r['pnl'] > 0]
profitable.sort(key=lambda x: -x['win_rate'])

print("="*90)
print("All profitable combinations sorted by WinRate:")
print("="*90)
print(f"{'k':>6} {'m':>6} | {'PnL':>7} {'Trades':>6} {'Wins':>5} {'WinRate':>7} {'AvgPnL':>7} | {'w/Trade':>7} {'Trig%':>6}")
print("-" * 90)

for r in profitable:
    marker = " ***" if r['win_rate'] >= 75 else " **" if r['win_rate'] >= 70 else " *" if r['win_rate'] >= 65 else ""
    print(f"{r['k']:6.1f} {r['m']:6.1f} | {r['pnl']:+7.2f} {r['trades']:6d} {r['wins']:5d} {r['win_rate']:6.1f}% {r['avg_pnl']:+7.4f} | {r['windows_with_trades']:7d} {r['trigger_pct']:5.1f}%{marker}")

print()
print("="*90)
print("All combinations with WinRate >= 75% (including negative PnL):")
print("="*90)
print(f"{'k':>6} {'m':>6} | {'PnL':>7} {'Trades':>6} {'Wins':>5} {'WinRate':>7} {'AvgPnL':>7} | {'w/Trade':>7} {'Trig%':>6}")
print("-" * 90)

high_wr = [r for r in results if r['win_rate'] >= 75]
high_wr.sort(key=lambda x: -x['pnl'])
for r in high_wr[:30]:
    marker = " $$$" if r['pnl'] > 0 else ""
    print(f"{r['k']:6.1f} {r['m']:6.1f} | {r['pnl']:+7.2f} {r['trades']:6d} {r['wins']:5d} {r['win_rate']:6.1f}% {r['avg_pnl']:+7.4f} | {r['windows_with_trades']:7d} {r['trigger_pct']:5.1f}%{marker}")

print()
print("="*90)
print("All combinations with WinRate >= 80%:")
print("="*90)
high80 = [r for r in results if r['win_rate'] >= 80]
if high80:
    print(f"{'k':>6} {'m':>6} | {'PnL':>7} {'Trades':>6} {'Wins':>5} {'WinRate':>7} {'AvgPnL':>7} | {'w/Trade':>7} {'Trig%':>6}")
    print("-" * 90)
    for r in sorted(high80, key=lambda x: -x['pnl']):
        marker = " $$$" if r['pnl'] > 0 else ""
        print(f"{r['k']:6.1f} {r['m']:6.1f} | {r['pnl']:+7.2f} {r['trades']:6d} {r['wins']:5d} {r['win_rate']:6.1f}% {r['avg_pnl']:+7.4f} | {r['windows_with_trades']:7d} {r['trigger_pct']:5.1f}%{marker}")
else:
    print("  No combinations found with WinRate >= 80%")
