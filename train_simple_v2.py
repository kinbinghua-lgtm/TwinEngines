#!/usr/bin/env python3
"""
用已有数据训练简化模型
"""

import json
from pathlib import Path
from train_simple_model import TrainingSample, SimpleTrainer

# 加载已有数据
predictions = json.loads(Path('data_runtime/all_predictions.json').read_text())

print(f"加载了 {len(predictions)} 个预测样本")

# 转换为新格式（使用best_ask）
training_samples = []

for pred in predictions:
    if pred.get('actual_reversal') is None:
        continue
    
    # 原数据中：
    # bid_rev = 反转方向的bid
    # bid_trend = 顺势方向的bid
    
    # 转换为ask（买入价）：
    # ask_trend = bid_trend + spread ≈ bid_trend + 0.01
    # ask_rev = bid_rev + spread ≈ bid_rev + 0.01
    
    bid_rev = pred.get('bid_rev')
    bid_trend = pred.get('bid_trend')
    
    if bid_rev is None or bid_trend is None:
        continue
    
    # 简单近似：ask = bid + 0.01
    ask_trend = min(0.99, bid_trend + 0.01)
    ask_rev = min(0.99, bid_rev + 0.01)
    
    sample = TrainingSample(
        timestamp=f"{pred['elapsed_sec']:.1f}s",
        condition_id=pred['condition_id'],
        p_raw=pred['p_raw'],
        ask_trend=ask_trend,
        ask_rev=ask_rev,
        bid_rev_size=pred.get('bid_rev_size') or 0.0,
        bid_trend_size=pred.get('bid_trend_size') or 0.0,
        spread_rev=pred.get('spread_rev') or 0.01,
        spread_trend=pred.get('spread_trend') or 0.01,
        actual_reversal=pred['actual_reversal']
    )
    training_samples.append(sample)

print(f"转换为 {len(training_samples)} 个训练样本")
print()

# 训练
trainer = SimpleTrainer(
    min_ev_range=[0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.12, 0.15],
    min_depth=10.0,
    max_spread=0.15,
    validation_split=0.3
)

model = trainer.train(training_samples)

print()
print("="*60)
print("训练完成！")
print("="*60)
print(f"最优参数: min_ev = {model.min_ev:.3f}")
