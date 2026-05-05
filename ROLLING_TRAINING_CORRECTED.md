# 滚动训练方案 - 正确版本

## 🎯 关键纠正

### 我之前理解错了！

**正确理解**：
- ✅ **所有窗口都用于训练**（不管是否实际下单）
- ✅ 每10小时有**120个完整窗口**
- ✅ 每个窗口都计算：如果下单，EV是多少？能否成交？

---

## 💡 数据构建

```python
# 所有窗口都是训练样本
for window in all_windows:
    # 特征
    features = [d, T, confidence, best_ask, best_bid, spread]
    
    # 目标1：如果下单，EV是多少？
    if final_reversal and best_ask < 0.9:
        ev = (1 - best_ask)  # 理论上会赢
    else:
        ev = -best_ask  # 理论上会输
    
    # 目标2：能否成交？
    matched = 1 if best_ask < 0.9 else 0
    
    X.append(features)
    y_ev.append(ev)
    y_matched.append(matched)
```

**关键**：
- ✅ 10小时 = 120个样本（足够！）
- ✅ 不需要实际下单

---

## 🔄 滚动训练验证

### 你的方案（完美！）

```
[0-10h]  → 训练模型A → 验证[10-20h] → 记录结果
[0-20h]  → 训练模型B → 验证[20-30h] → 记录结果
[0-30h]  → 训练模型C → 验证[30-40h] → 记录结果
...
```

**优势**：
1. ✅ 持续学习（数据越来越多）
2. ✅ 前向验证（避免未来信息泄露）
3. ✅ 记录每次结果（追踪改进）
4. ✅ 自动化

---

## 📊 实施方案

### 滚动训练脚本

```python
class RollingTrainer:
    def run_iteration(self):
        # 1. 用所有历史数据训练
        X_train, y_ev, y_match = load_all_history()
        
        ev_model.fit(X_train, y_ev)
        match_model.fit(X_train, y_match)
        
        # 2. 保存模型
        save_model(f'model_{now}.pkl')
        
        # 3. 验证上一次的模型（10小时前训练的）
        if has_previous_model():
            X_val, y_val = load_last_10h()
            
            ev_pred = model.predict(X_val)
            
            # 记录结果
            log_result({
                'ev_mae': mae(ev_pred, y_val),
                'total_ev': sum(ev_pred),
            })

# 每10小时运行一次
# crontab: 0 */10 * * * python3 rolling_trainer.py
```

---

## 🎯 总结

### 关键点 ✅

1. **所有窗口都训练**
   - ✅ 10小时 = 120个样本
   - ✅ 不需要实际下单

2. **滚动验证**
   - ✅ 用历史训练
   - ✅ 验证未来
   - ✅ 记录结果

3. **训练目标**
   - 最大化EV
   - 保证成交率

**要开始实现吗？** 🚀
