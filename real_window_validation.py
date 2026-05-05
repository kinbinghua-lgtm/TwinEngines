import json
from pathlib import Path
from train_correction_model import CorrectionModel

predictions = json.loads(Path('data_runtime/all_predictions.json').read_text())

valid = [p for p in predictions if p['actual_reversal'] is not None
         and (p.get('bid_rev_size') or 0) >= 10
         and (p.get('bid_trend_size') or 0) >= 10]

# 按窗口分组
windows = {}
for p in valid:
    cid = p['condition_id']
    if cid not in windows:
        windows[cid] = []
    windows[cid].append(p)

# 按elapsed_sec排序每个窗口的预测
for cid in windows:
    windows[cid].sort(key=lambda x: x['elapsed_sec'])

print(f"Total windows: {len(windows)}")
print(f"Total valid predictions: {len(valid)}")
print()

# 过滤：只保留第3分钟末（180s）到第5分钟（300s）的预测
for cid in windows:
    windows[cid] = [p for p in windows[cid] if 180 <= p['elapsed_sec'] <= 300]

# 去掉没有预测的窗口
windows = {cid: preds for cid, preds in windows.items() if len(preds) > 0}

total_preds_in_range = sum(len(preds) for preds in windows.values())
print(f"Windows with predictions in 180-300s: {len(windows)}")
print(f"Predictions in range: {total_preds_in_range}")
print(f"Avg predictions per window in range: {total_preds_in_range/len(windows):.1f}")
print()

# 测试三组参数
params_list = [
    (3.0, 4.0),
    (3.5, 5.0),
    (2.5, 10.0),
]

for k, m in params_list:
    model = CorrectionModel(k=k, m=m, min_ev=0.03)
    
    print("="*90)
    print(f"  k={k}, m={m}")
    print("="*90)
    print()
    
    total_pnl = 0.0
    n_windows_traded = 0
    n_windows_skipped = 0
    total_trades_in_traded_windows = 0
    window_results = []
    
    for cid, preds in windows.items():
        # 模拟真实决策：从第3分钟末开始，逐秒扫描
        # 第一次EV满足条件时下单，之后该窗口不再下单
        traded = False
        window_pnl = 0.0
        window_trades = 0
        first_action = None
        first_ev = None
        first_elapsed = None
        
        for p in preds:
            odds_rev = 1 - p['bid_rev']
            odds_trend = 1 - p['bid_trend']
            action, ev = model.make_decision(p['p_raw'], odds_rev, odds_trend, 0.01)
            
            if action != 'pass':
                window_trades += 1
                
                if action == 'reversal':
                    pnl = odds_rev - 0.01 if p['actual_reversal'] else -(1 - odds_rev) - 0.01
                else:
                    pnl = odds_trend - 0.01 if not p['actual_reversal'] else -(1 - odds_trend) - 0.01
                
                window_pnl += pnl
                
                if not traded:
                    traded = True
                    first_action = action
                    first_ev = ev
                    first_elapsed = p['elapsed_sec']
        
        if traded:
            n_windows_traded += 1
            total_trades_in_traded_windows += window_trades
            total_pnl += window_pnl
            
            result_str = "WIN" if window_pnl > 0 else "LOSS"
            window_results.append({
                'cid': cid[:12],
                'pnl': window_pnl,
                'trades': window_trades,
                'first_action': first_action,
                'first_ev': first_ev,
                'first_elapsed': first_elapsed,
                'actual_rev': preds[0]['actual_reversal'],
                'result': result_str
            })
        else:
            n_windows_skipped += 1
    
    # 统计
    n_wins = sum(1 for r in window_results if r['pnl'] > 0)
    n_losses = len(window_results) - n_wins
    win_rate = n_wins / len(window_results) * 100 if window_results else 0
    avg_pnl_per_window = total_pnl / len(window_results) if window_results else 0
    avg_trades_per_window = total_trades_in_traded_windows / n_windows_traded if n_windows_traded > 0 else 0
    
    print(f"  Summary:")
    print(f"    Total windows:          {len(windows)}")
    print(f"    Windows traded:         {n_windows_traded} ({n_windows_traded/len(windows)*100:.1f}%)")
    print(f"    Windows skipped:        {n_windows_skipped}")
    print(f"    Windows won:            {n_wins}")
    print(f"    Windows lost:           {n_losses}")
    print(f"    Window win rate:        {win_rate:.1f}%")
    print(f"    Total PnL:              {total_pnl:+.2f}")
    print(f"    Avg PnL per window:     {avg_pnl_per_window:+.4f}")
    print(f"    Total trades:           {total_trades_in_traded_windows}")
    print(f"    Avg trades per window:  {avg_trades_per_window:.1f}")
    print()
    
    print(f"  Per-window detail:")
    print(f"  {'CID':>14} {'Result':>6} {'PnL':>7} {'Trades':>6} {'1stAction':>10} {'1stEV':>7} {'1stTime':>7} {'Actual':>7}")
    print(f"  {'-'*76}")
    
    for r in window_results:
        actual = "REV" if r['actual_rev'] else "TREND"
        print(f"  {r['cid']:>14} {r['result']:>6} {r['pnl']:+7.2f} {r['trades']:6d} {r['first_action']:>10} {r['first_ev']:+7.3f} {r['first_elapsed']:6.1f}s {actual:>7}")
    
    print()
