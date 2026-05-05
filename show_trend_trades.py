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

for cid in windows:
    windows[cid].sort(key=lambda x: x['elapsed_sec'])
    windows[cid] = [p for p in windows[cid] if 180 <= p['elapsed_sec'] <= 300]

windows = {cid: preds for cid, preds in windows.items() if len(preds) > 0}

model = CorrectionModel(k=3.5, m=5.0, min_ev=0.03)

trend_trades = []

for cid, preds in windows.items():
    for p in preds:
        odds_rev = 1 - p['bid_rev']
        odds_trend = 1 - p['bid_trend']
        
        p_adj = model.correct_probability(p['p_raw'], odds_rev)
        action, ev = model.make_decision(p['p_raw'], odds_rev, odds_trend, 0.01)
        
        if action == 'trend':
            pnl = odds_trend - 0.01 if not p['actual_reversal'] else -(1 - odds_trend) - 0.01
            result = "WIN" if pnl > 0 else "LOSS"
            actual = "REV" if p['actual_reversal'] else "TREND"
            trend_trades.append({
                'pnl': pnl,
                'actual': actual,
                'result': result,
                'p_raw': p['p_raw'],
                'p_adj': p_adj,
                'odds_trend': odds_trend,
                'odds_rev': odds_rev,
                'ev': ev,
                'elapsed': p['elapsed_sec'],
                'cid': p['condition_id'][:12]
            })

# 排序：按PnL
trend_trades.sort(key=lambda x: x['pnl'])

print(f"k=3.5, m=5.0 Trend trades: {len(trend_trades)}")
print()

wins = sum(1 for t in trend_trades if t['pnl'] > 0)
losses = len(trend_trades) - wins
total_pnl = sum(t['pnl'] for t in trend_trades)

print(f"Wins: {wins}, Losses: {losses}, WinRate: {wins/len(trend_trades)*100:.1f}%")
print(f"Total PnL: {total_pnl:+.2f}, Avg PnL: {total_pnl/len(trend_trades):+.4f}")
print()

print(f"{'#':>4} {'PnL':>7} {'Result':>6} {'Actual':>6} {'p_raw':>7} {'p_adj':>7} {'odds_t':>7} {'odds_r':>7} {'EV':>7} {'Time':>6}")
print("-" * 82)

for i, t in enumerate(trend_trades, 1):
    print(f"{i:4d} {t['pnl']:+7.2f} {t['result']:>6} {t['actual']:>6} {t['p_raw']:7.3f} {t['p_adj']:7.3f} {t['odds_trend']:7.3f} {t['odds_rev']:7.3f} {t['ev']:+7.3f} {t['elapsed']:5.1f}s")
