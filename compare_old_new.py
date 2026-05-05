import json
from pathlib import Path

predictions = json.loads(Path('data_runtime/all_predictions.json').read_text())

# 统计bid和ask的差异
diffs = []
for p in predictions:
    bid_trend = p.get('bid_trend')
    if bid_trend is not None and bid_trend > 0:
        ask_trend = min(0.99, bid_trend + 0.01)
        
        # 旧方法的赔率
        odds_old = 1 - bid_trend
        
        # 新方法的利润
        profit_new = 1 - ask_trend
        
        # 旧方法的亏损
        loss_old = bid_trend
        
        # 新方法的亏损
        loss_new = ask_trend
        
        diffs.append({
            'bid': bid_trend,
            'ask': ask_trend,
            'odds_old': odds_old,
            'profit_new': profit_new,
            'loss_old': loss_old,
            'loss_new': loss_new,
            'profit_diff': profit_new - odds_old,
            'loss_diff': loss_new - loss_old
        })

print("旧方法 vs 新方法对比（前20个样本）：")
print()
print(f"{'bid':>6} {'ask':>6} | {'赢(旧)':>8} {'赢(新)':>8} {'差':>6} | {'输(旧)':>8} {'输(新)':>8} {'差':>6}")
print("-" * 70)

for d in diffs[:20]:
    print(f"{d['bid']:6.2f} {d['ask']:6.2f} | "
          f"{d['odds_old']:8.2f} {d['profit_new']:8.2f} {d['profit_diff']:+6.2f} | "
          f"{d['loss_old']:8.2f} {d['loss_new']:8.2f} {d['loss_diff']:+6.2f}")

print()
print("统计：")
avg_profit_diff = sum(d['profit_diff'] for d in diffs) / len(diffs)
avg_loss_diff = sum(d['loss_diff'] for d in diffs) / len(diffs)
print(f"平均赢的差异: {avg_profit_diff:+.4f}")
print(f"平均输的差异: {avg_loss_diff:+.4f}")
print()
print("结论：")
print(f"  赢的时候少赚 {-avg_profit_diff:.4f}")
print(f"  输的时候多亏 {avg_loss_diff:.4f}")
print(f"  总影响: {avg_profit_diff + avg_loss_diff:.4f} (假设50%胜率)")
