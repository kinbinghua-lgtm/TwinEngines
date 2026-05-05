# 关于在VPS上实时训练EV模型的方案

## 🎯 你的想法完全可行！而且非常好！

### 发现的关键信息

从VPS数据看到：
- ✅ 有实时盘口数据（best_ask, best_bid）
- ⚠️ **matched字段都是None**（还没有结算结果）
- 原因：需要等待窗口结束后才能知道结果

---

## 💡 你的提议：在VPS上实时训练EV预测模型

### 完全可行！原因：

1. **VPS有完整的训练数据**
   - ✅ 特征：confidence, edge, p_rev, best_ask, best_bid等
   - ✅ 目标：matched (True/False) 或 actual_pnl
   - ✅ K线数据（Binance缓存）

2. **实时训练的优势**
   - ✅ 数据最新鲜
   - ✅ 模型持续改进
   - ✅ 自动适应市场变化
   - ✅ 不需要手动下载数据

3. **技术上完全可行**
   - ✅ VPS有Python环境
   - ✅ 有sklearn/xgboost等库
   - ✅ 可以定时训练（每天/每周）

---

## 📊 实时训练方案设计

### 方案A：定时重训练（推荐）⭐⭐⭐⭐⭐

**原理**：
- 每天凌晨自动训练一次
- 使用过去N天的数据
- 更新模型文件
- 策略自动加载新模型

**实现**：

```python
# /root/TwinEngines/train_ev_model_daily.py

import json
import pickle
from datetime import datetime, timedelta
from sklearn.ensemble import RandomForestClassifier
import numpy as np

def load_training_data(days=7):
    """加载过去N天的数据"""
    
    cutoff_time = datetime.now() - timedelta(days=days)
    cutoff_ms = int(cutoff_time.timestamp() * 1000)
    
    X = []  # 特征
    y = []  # 目标（matched）
    
    with open('logs/shadow_signals.jsonl', 'r') as f:
        for line in f:
            try:
                obj = json.loads(line.strip())
                
                # 只用最近N天的数据
                ts_ms = obj.get('ts_ms', 0)
                if ts_ms < cutoff_ms:
                    continue
                
                # 只用有结算结果的数据
                matched = obj.get('matched')
                if matched is None:
                    continue
                
                # 提取特征
                confidence = obj.get('confidence', 0)
                edge = obj.get('edge_vs_best_ask', 0)
                p_rev = obj.get('p_rev', 0)
                
                poly = obj.get('polymarket', {})
                book = poly.get('book', {})
                best_ask = book.get('best_ask', 0)
                spread = book.get('best_ask', 0) - book.get('best_bid', 0)
                
                # 特征向量
                features = [
                    confidence,
                    edge,
                    p_rev,
                    best_ask,
                    spread,
                ]
                
                X.append(features)
                y.append(1 if matched else 0)
            
            except:
                pass
    
    return np.array(X), np.array(y)

def train_ev_model():
    """训练EV预测模型"""
    
    print(f"[{datetime.now()}] 开始训练EV模型...")
    
    # 加载数据
    X, y = load_training_data(days=7)
    
    if len(X) < 50:
        print(f"数据不足（{len(X)}条），跳过训练")
        return
    
    print(f"训练样本数: {len(X)}")
    print(f"胜率: {y.mean():.2%}")
    
    # 训练模型
    model = RandomForestClassifier(
        n_estimators=100,
        max_depth=10,
        random_state=42
    )
    model.fit(X, y)
    
    # 保存模型
    with open('models/ev_predictor.pkl', 'wb') as f:
        pickle.dump(model, f)
    
    print(f"[{datetime.now()}] 模型已保存")
    
    # 特征重要性
    feature_names = ['confidence', 'edge', 'p_rev', 'best_ask', 'spread']
    for name, imp in zip(feature_names, model.feature_importances_):
        print(f"  {name}: {imp:.3f}")

if __name__ == "__main__":
    train_ev_model()
```

**定时任务**：

```bash
# 添加到crontab
crontab -e

# 每天凌晨3点训练
0 3 * * * cd /root/TwinEngines && python3 train_ev_model_daily.py >> logs/training.log 2>&1
```

---

### 方案B：在线学习（更高级）⭐⭐⭐⭐

**原理**：
- 每次有新的结算结果，立即更新模型
- 使用增量学习算法
- 模型实时改进

**实现**：

```python
from sklearn.linear_model import SGDClassifier

class OnlineEVPredictor:
    def __init__(self):
        self.model = SGDClassifier(
            loss='log_loss',  # 逻辑回归
            warm_start=True   # 支持增量学习
        )
        self.is_fitted = False
    
    def update(self, features, outcome):
        """增量更新模型"""
        X = np.array([features])
        y = np.array([1 if outcome else 0])
        
        if not self.is_fitted:
            self.model.fit(X, y)
            self.is_fitted = True
        else:
            self.model.partial_fit(X, y)
    
    def predict_proba(self, features):
        """预测胜率"""
        if not self.is_fitted:
            return 0.5
        
        X = np.array([features])
        return self.model.predict_proba(X)[0][1]
```

---

### 方案C：混合方案（最推荐）⭐⭐⭐⭐⭐

**结合定时重训练 + 在线微调**：

1. **每天凌晨**：用过去7天数据重新训练基础模型
2. **实时**：用新结算的数据微调模型
3. **每周**：用过去30天数据训练长期模型

---

## 🔬 目标变量的选择

### 选项1：预测matched（分类）

```python
# 目标：预测是否会成交并获胜
y = 1 if matched else 0

# 模型：RandomForest / XGBoost
# 输出：P(win)
```

**优势**：
- 简单直接
- 容易解释

### 选项2：预测actual_pnl（回归）

```python
# 目标：预测实际盈亏
y = actual_pnl  # 例如：+0.5 或 -0.3

# 模型：RandomForest Regressor / XGBoost Regressor
# 输出：预期盈亏
```

**优势**：
- 更精确
- 直接优化收益

### 选项3：预测EV（回归，最好）⭐

```python
# 目标：预测期望值
if matched:
    ev = (1 - best_ask)  # 赢了
else:
    ev = -best_ask  # 输了

y = ev

# 模型：XGBoost Regressor
# 输出：预期EV
```

**优势**：
- 直接优化EV
- 考虑了盘口价格
- 最符合决策目标

---

## 📝 完整实施方案

### 第1步：数据准备（现在）

```bash
# 确保shadow_signals.jsonl包含：
# - 特征：confidence, edge, p_rev, best_ask, best_bid
# - 结果：matched (True/False)
# - 盈亏：actual_pnl（如果有）
```

### 第2步：等待数据积累（1-3天）

- 需要至少100条有结算结果的记录
- 建议300+条

### 第3步：部署训练脚本（3天后）

```bash
# 上传训练脚本
scp train_ev_model_daily.py root@47.243.169.223:/root/TwinEngines/

# 设置定时任务
ssh root@47.243.169.223
crontab -e
# 添加：0 3 * * * cd /root/TwinEngines && python3 train_ev_model_daily.py
```

### 第4步：策略集成

```python
# 在策略中加载模型
class NakedPMRunner:
    def __init__(self):
        self.ev_model = self.load_ev_model()
    
    def load_ev_model(self):
        try:
            with open('models/ev_predictor.pkl', 'rb') as f:
                return pickle.load(f)
        except:
            return None
    
    def should_take_signal(self, signal):
        if self.ev_model is None:
            # 使用原有逻辑
            return signal.confidence >= self.confidence_min
        
        # 使用模型预测
        features = [
            signal.confidence,
            signal.edge,
            signal.p_rev,
            signal.best_ask,
            signal.spread,
        ]
        
        predicted_ev = self.ev_model.predict([features])[0]
        
        # 只有预期EV > 阈值才下单
        return predicted_ev > 0.05  # 例如：预期5%收益
```

---

## ⚠️ 注意事项

### 1. 数据延迟

- 结算结果需要等待窗口结束（5分钟）
- 训练数据会有延迟
- 解决：使用过去的数据训练

### 2. 样本不平衡

- 可能胜率很高或很低
- 需要处理样本不平衡
- 解决：使用class_weight或SMOTE

### 3. 过拟合风险

- 数据量少时容易过拟合
- 需要交叉验证
- 解决：使用简单模型，正则化

### 4. 模型更新策略

- 不要每次都完全替换模型
- 可以用ensemble（多个模型投票）
- 解决：保留历史模型，加权平均

---

## 🎯 总结

### 你的想法完全可行！✅

**优势**：
1. ✅ 实时数据，最新鲜
2. ✅ 持续学习，自动改进
3. ✅ 直接优化EV
4. ✅ 不需要手动干预

**建议**：
1. 等待3-7天积累数据（300+条）
2. 先用定时重训练（每天一次）
3. 验证效果后，考虑在线学习
4. 目标变量用EV（最直接）

**下一步**：
1. 确认shadow_signals.jsonl包含matched字段
2. 等待数据积累
3. 我帮你实现完整的训练脚本
4. 部署到VPS

**要开始实现吗？** 🚀
