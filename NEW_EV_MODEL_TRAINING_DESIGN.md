# 重新训练生存模型 - 基于实时盘口和EV的新方案

## 🎯 你的核心想法

### 当前生存模型的问题

**现有训练目标**：
```python
# 当前模型预测：P(reversal | d, T)
# 目标：预测价格是否会反转
# 训练数据：历史K线数据

y = 1 if reversal else 0  # 二分类

# 问题：
# 1. 只考虑价格反转，不考虑盘口
# 2. 不考虑能否成交
# 3. 不直接优化EV
```

### 你提出的新训练目标

**两个关键指标**：

1. **正向EV**
   ```python
   # 目标1：预测实际EV
   if matched and win:
       ev = (1 - best_ask)  # 赢了的收益
   elif matched and lose:
       ev = -best_ask  # 输了的损失
   else:
       ev = 0  # 没成交
   
   y1 = ev  # 回归目标
   ```

2. **基于实时盘口的成交率**
   ```python
   # 目标2：预测能否立即成交
   y2 = 1 if matched else 0  # 分类目标
   
   # matched的定义：
   # - 下单后立即成交
   # - 基于实时盘口（best_ask, best_bid）
   # - 不是理论价格
   ```

---

## 💡 新的训练方案设计

### 方案：多目标联合训练

**完全独立于现有主程序**，从根基上改变训练目标。

#### 模型架构

```python
class RealTimeEVModel:
    """
    基于实时盘口的EV预测模型
    
    输入特征：
        - d_abs_pct: 价格偏离
        - t_remaining: 剩余时间
        - confidence: 当前置信度（如果有）
        - best_ask: 实时盘口ask价
        - best_bid: 实时盘口bid价
        - spread: 价差
        - volatility: 波动率（从K线计算）
        - trend: 趋势强度
    
    输出：
        - predicted_ev: 预期EV（主要目标）
        - match_prob: 成交概率（辅助目标）
    """
    
    def __init__(self):
        # 主模型：预测EV
        self.ev_model = XGBoostRegressor()
        
        # 辅助模型：预测成交概率
        self.match_model = XGBoostClassifier()
    
    def train(self, X, y_ev, y_matched):
        """
        联合训练两个目标
        
        X: 特征矩阵 [n_samples, n_features]
        y_ev: EV目标 [n_samples]
        y_matched: 成交目标 [n_samples]
        """
        # 训练EV模型
        self.ev_model.fit(X, y_ev)
        
        # 训练成交模型
        self.match_model.fit(X, y_matched)
    
    def predict(self, X):
        """
        预测EV和成交概率
        
        返回:
            predicted_ev: 预期EV
            match_prob: 成交概率
        """
        ev = self.ev_model.predict(X)
        match_prob = self.match_model.predict_proba(X)[:, 1]
        
        # 综合决策：EV * 成交概率
        adjusted_ev = ev * match_prob
        
        return adjusted_ev, match_prob
```

---

## 📊 数据准备

### 从shadow_signals.jsonl提取训练数据

```python
def prepare_training_data(jsonl_file):
    """
    从实时采集的数据中提取训练样本
    
    每条记录包含：
        - 特征：d, T, best_ask, best_bid, spread等
        - 结果：matched (True/False)
        - 盈亏：actual_ev
    """
    
    X = []  # 特征
    y_ev = []  # EV目标
    y_matched = []  # 成交目标
    
    with open(jsonl_file, 'r') as f:
        for line in f:
            obj = json.loads(line)
            
            # 只用有结算结果的数据
            if obj.get('matched') is None:
                continue
            
            # 提取特征
            d_abs_pct = obj.get('d_abs_pct', 0)
            t_remaining = obj.get('t_remaining_sec', 0)
            
            poly = obj.get('polymarket', {})
            book = poly.get('book', {})
            best_ask = book.get('best_ask', 0)
            best_bid = book.get('best_bid', 0)
            spread = best_ask - best_bid
            
            # 从K线计算波动率（如果有）
            volatility = calculate_volatility(obj)
            
            features = [
                d_abs_pct,
                t_remaining,
                best_ask,
                best_bid,
                spread,
                volatility,
            ]
            
            # 提取目标
            matched = obj.get('matched')
            
            # 计算实际EV
            if matched:
                # 成交了，计算实际盈亏
                win = obj.get('win', False)  # 需要确认字段名
                if win:
                    ev = (1 - best_ask)
                else:
                    ev = -best_ask
            else:
                # 没成交
                ev = 0
            
            X.append(features)
            y_ev.append(ev)
            y_matched.append(1 if matched else 0)
    
    return np.array(X), np.array(y_ev), np.array(y_matched)
```

---

## 🚀 实施方案

### 第1步：数据积累（3-7天）

**目标**：积累300+条有结算结果的记录

**检查点**：
```bash
# 每天检查数据量
python3 check_training_data.py

# 输出：
# 总记录数: 500
# 有结算结果: 320
# 成交率: 45%
# 平均EV: 0.12
```

---

### 第2步：训练脚本（完全独立）

```python
#!/usr/bin/env python3
"""
独立的EV模型训练脚本
完全不依赖现有主程序
"""

import json
import numpy as np
import xgboost as xgb
from sklearn.model_selection import train_test_split
import pickle

def train_realtime_ev_model():
    """
    每10小时训练一次
    使用最近120个窗口的数据
    """
    
    print(f"[{datetime.now()}] 开始训练...")
    
    # 1. 加载数据
    X, y_ev, y_matched = prepare_training_data('logs/shadow_signals.jsonl')
    
    # 只用最近120个窗口（10小时）
    if len(X) > 120:
        X = X[-120:]
        y_ev = y_ev[-120:]
        y_matched = y_matched[-120:]
    
    print(f"训练样本数: {len(X)}")
    print(f"成交率: {y_matched.mean():.2%}")
    print(f"平均EV: {y_ev.mean():.4f}")
    
    if len(X) < 50:
        print("数据不足，跳过训练")
        return
    
    # 2. 划分训练集和验证集
    X_train, X_val, y_ev_train, y_ev_val, y_match_train, y_match_val = train_test_split(
        X, y_ev, y_matched, test_size=0.2, random_state=42
    )
    
    # 3. 训练EV模型
    print("\n训练EV预测模型...")
    ev_model = xgb.XGBRegressor(
        n_estimators=100,
        max_depth=5,
        learning_rate=0.1,
        objective='reg:squarederror'
    )
    ev_model.fit(X_train, y_ev_train)
    
    # 验证
    ev_pred = ev_model.predict(X_val)
    mae = np.mean(np.abs(ev_pred - y_ev_val))
    print(f"验证集MAE: {mae:.4f}")
    
    # 4. 训练成交模型
    print("\n训练成交概率模型...")
    match_model = xgb.XGBClassifier(
        n_estimators=100,
        max_depth=5,
        learning_rate=0.1
    )
    match_model.fit(X_train, y_match_train)
    
    # 验证
    match_pred = match_model.predict(X_val)
    acc = np.mean(match_pred == y_match_val)
    print(f"验证集准确率: {acc:.2%}")
    
    # 5. 保存模型
    with open('models/realtime_ev_model.pkl', 'wb') as f:
        pickle.dump({
            'ev_model': ev_model,
            'match_model': match_model,
            'feature_names': ['d_abs_pct', 't_remaining', 'best_ask', 'best_bid', 'spread', 'volatility'],
            'trained_at': datetime.now().isoformat(),
            'n_samples': len(X),
        }, f)
    
    print(f"\n[{datetime.now()}] 模型已保存")
    
    # 6. 特征重要性
    print("\nEV模型特征重要性:")
    for name, imp in zip(feature_names, ev_model.feature_importances_):
        print(f"  {name}: {imp:.3f}")

if __name__ == "__main__":
    train_realtime_ev_model()
```

---

### 第3步：定时训练（每10小时）

```bash
# crontab配置
# 每10小时训练一次
0 */10 * * * cd /root/TwinEngines && python3 train_realtime_ev_model.py >> logs/ev_training.log 2>&1
```

**为什么10小时？**
- 10小时 = 120个窗口
- 足够捕捉市场变化
- 不会过拟合短期波动
- 计算成本可控

---

### 第4步：策略集成（可选）

```python
class NakedPMRunner:
    def __init__(self):
        # 加载新模型
        self.realtime_ev_model = self.load_realtime_ev_model()
    
    def load_realtime_ev_model(self):
        try:
            with open('models/realtime_ev_model.pkl', 'rb') as f:
                return pickle.load(f)
        except:
            return None
    
    def should_take_signal(self, signal):
        if self.realtime_ev_model is None:
            # 使用原有逻辑
            return self.original_logic(signal)
        
        # 使用新模型
        features = [
            signal.d_abs_pct,
            signal.t_remaining,
            signal.best_ask,
            signal.best_bid,
            signal.spread,
            signal.volatility,
        ]
        
        ev_model = self.realtime_ev_model['ev_model']
        match_model = self.realtime_ev_model['match_model']
        
        # 预测EV和成交概率
        predicted_ev = ev_model.predict([features])[0]
        match_prob = match_model.predict_proba([features])[0][1]
        
        # 综合决策
        adjusted_ev = predicted_ev * match_prob
        
        # 只有调整后的EV > 阈值才下单
        return adjusted_ev > 0.05  # 例如：5%
```

---

## 📊 与现有模型的对比

| 方面 | 现有生存模型 | 新EV模型 |
|------|-------------|---------|
| **训练目标** | P(reversal) | EV + 成交率 |
| **数据源** | 历史K线 | 实时盘口 |
| **考虑盘口** | ❌ 否 | ✅ 是 |
| **考虑成交** | ❌ 否 | ✅ 是 |
| **优化目标** | 预测准确率 | 实际收益 |
| **更新频率** | 手动 | 每10小时自动 |
| **独立性** | 嵌入主程序 | 完全独立 |

---

## ⚠️ 关键问题

### 1. 数据需求

**最少需要**：
- 50条有结算结果的记录
- 建议100+条
- 理想300+条

**10小时 = 120个窗口**：
- 如果每个窗口都有结算结果：120条
- 实际可能只有50-80条（不是每个窗口都下单）

**结论**：10小时的数据量**可能刚好够**，但建议：
- 初期用24小时数据（288个窗口）
- 稳定后改为10小时

---

### 2. 目标变量的定义

**需要确认shadow_signals.jsonl中的字段**：

```python
# 需要的字段：
{
    "matched": True/False,  # 是否成交
    "win": True/False,      # 是否获胜（如果成交）
    "actual_pnl": 0.15,     # 实际盈亏（如果有）
    "best_ask": 0.35,       # 实时盘口
    "best_bid": 0.34,
}
```

**如果没有这些字段，需要添加**。

---

### 3. 与现有模型的关系

**完全独立**：
- 新模型不修改现有生存模型
- 可以并行运行
- 可以对比效果
- 逐步替换

---

## 🎯 总结

### 你的想法完全正确！✅

**核心改进**：
1. ✅ 训练目标：从P(reversal) → EV + 成交率
2. ✅ 数据源：从历史K线 → 实时盘口
3. ✅ 更新频率：从手动 → 每10小时自动
4. ✅ 独立性：完全独立于主程序

**10小时训练一次**：
- ✅ 120个窗口，数据量刚好够
- ✅ 建议初期用24小时，稳定后改10小时

**下一步**：
1. 确认shadow_signals.jsonl的字段
2. 等待3-7天积累数据
3. 实现训练脚本
4. 部署定时任务

**要开始实现吗？** 🚀
