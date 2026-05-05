# 基于K线特征的盘口模拟方案（改进版）

## 🎯 核心洞察

**你的观察完全正确！** 盘口信息不是孤立的，而是与市场状态密切相关：

1. **价格波动率** ↑ → Spread ↑（风险增加，做市商要求更高补偿）
2. **成交量** ↑ → Spread ↓（流动性好，竞争激烈）
3. **价格趋势** → 影响Bid/Ask的偏斜
4. **时间剩余** ↓ → Spread可能 ↑（不确定性增加）

**之前的方案问题**：只考虑置信度，忽略了市场状态！

---

## 📊 K线特征与盘口的关系

### 理论基础

#### 1. **波动率 → Spread**

```
经典做市商模型（Avellaneda-Stoikov）:
spread = γ * σ² * T

其中:
- γ: 风险厌恶系数
- σ: 波动率
- T: 持有期
```

**实证关系**:
- 高波动率 → 做市商风险大 → Spread宽
- 低波动率 → 做市商风险小 → Spread窄

#### 2. **成交量 → 流动性 → Spread**

```
流动性越好 → 竞争越激烈 → Spread越窄
```

#### 3. **价格趋势 → Bid/Ask偏斜**

```
上涨趋势 → Ask相对更高（卖方惜售）
下跌趋势 → Bid相对更低（买方观望）
```

#### 4. **时间衰减 → 不确定性**

```
接近到期 → 不确定性增加 → Spread可能扩大
```

---

## 💡 改进的模拟方案

### 方案：基于K线特征的条件模拟

#### 第1步：提取K线特征

```python
def extract_kline_features(window_data):
    """
    从窗口K线数据中提取特征
    
    参数:
        window_data: 窗口内的1s或1m K线数据
    
    返回:
        features: dict
    """
    
    # 1. 价格特征
    prices = window_data['close']
    returns = prices.pct_change()
    
    features = {
        # 波动率（最重要！）
        'volatility_1m': returns[-60:].std() * np.sqrt(60),  # 最近1分钟
        'volatility_3m': returns[-180:].std() * np.sqrt(180),  # 最近3分钟
        'volatility_5m': returns.std() * np.sqrt(300),  # 整个窗口
        
        # 价格变化
        'price_change_pct': (prices.iloc[-1] - prices.iloc[0]) / prices.iloc[0],
        'price_range_pct': (prices.max() - prices.min()) / prices.iloc[0],
        
        # 趋势
        'trend_direction': 1 if prices.iloc[-1] > prices.iloc[0] else -1,
        'trend_strength': abs(prices.iloc[-1] - prices.iloc[0]) / prices.std(),
        
        # 成交量（如果有）
        'volume_mean': window_data['volume'].mean() if 'volume' in window_data else None,
        'volume_std': window_data['volume'].std() if 'volume' in window_data else None,
        
        # 时间特征
        'seconds_elapsed': len(window_data),
        'seconds_remaining': 300 - len(window_data),
        
        # 价格位置
        'price_percentile': (prices.iloc[-1] - prices.min()) / (prices.max() - prices.min()),
    }
    
    return features
```

#### 第2步：建立K线特征 → 盘口的映射模型

```python
class KlineBasedOrderBookSimulator:
    def __init__(self, real_data_with_klines):
        """
        real_data_with_klines: 包含K线特征和盘口的真实数据
        [
            {
                'kline_features': {...},
                'orderbook': {'best_ask': ..., 'best_bid': ...},
                'confidence': ...,
            },
            ...
        ]
        """
        self.real_data = real_data_with_klines
        self._train_models()
    
    def _train_models(self):
        """训练K线特征 → 盘口的映射模型"""
        
        # 准备训练数据
        X = []  # 特征
        y_spread = []  # 目标：spread
        y_midpoint = []  # 目标：midpoint
        
        for d in self.real_data:
            features = d['kline_features']
            book = d['orderbook']
            
            # 特征向量
            x = [
                features['volatility_1m'],
                features['volatility_3m'],
                features['price_change_pct'],
                features['trend_strength'],
                features['seconds_remaining'],
                d['confidence'],  # 也包含模型置信度
            ]
            X.append(x)
            
            # 目标变量
            spread = book['best_ask'] - book['best_bid']
            midpoint = (book['best_ask'] + book['best_bid']) / 2
            
            y_spread.append(spread)
            y_midpoint.append(midpoint)
        
        X = np.array(X)
        y_spread = np.array(y_spread)
        y_midpoint = np.array(y_midpoint)
        
        # 训练模型
        from sklearn.ensemble import RandomForestRegressor
        
        # 模型1: 预测Spread
        self.spread_model = RandomForestRegressor(n_estimators=100, random_state=42)
        self.spread_model.fit(X, y_spread)
        
        # 模型2: 预测Midpoint
        self.midpoint_model = RandomForestRegressor(n_estimators=100, random_state=42)
        self.midpoint_model.fit(X, y_midpoint)
        
        # 特征重要性
        self.feature_importance = self.spread_model.feature_importances_
        print("特征重要性:")
        feature_names = ['vol_1m', 'vol_3m', 'price_chg', 'trend', 'time_left', 'confidence']
        for name, imp in zip(feature_names, self.feature_importance):
            print(f"  {name}: {imp:.3f}")
    
    def simulate(self, kline_features, confidence, add_noise=True):
        """
        基于K线特征模拟盘口
        
        参数:
            kline_features: K线特征字典
            confidence: 模型置信度
            add_noise: 是否添加随机噪声
        
        返回:
            {'best_ask': ..., 'best_bid': ..., 'spread': ...}
        """
        
        # 构造特征向量
        x = np.array([[
            kline_features['volatility_1m'],
            kline_features['volatility_3m'],
            kline_features['price_change_pct'],
            kline_features['trend_strength'],
            kline_features['seconds_remaining'],
            confidence,
        ]])
        
        # 预测
        spread_pred = self.spread_model.predict(x)[0]
        midpoint_pred = self.midpoint_model.predict(x)[0]
        
        # 添加噪声（模拟市场随机性）
        if add_noise:
            spread_noise = np.random.normal(0, spread_pred * 0.1)  # 10%噪声
            midpoint_noise = np.random.normal(0, midpoint_pred * 0.02)  # 2%噪声
            
            spread_pred += spread_noise
            midpoint_pred += midpoint_noise
        
        # 确保合理范围
        spread_pred = max(0.001, min(spread_pred, 0.1))  # 0.1%-10%
        midpoint_pred = max(0.01, min(midpoint_pred, 0.99))
        
        # 计算Bid和Ask
        best_ask = midpoint_pred + spread_pred / 2
        best_bid = midpoint_pred - spread_pred / 2
        
        # 边界检查
        best_ask = max(0.01, min(best_ask, 0.99))
        best_bid = max(0.01, min(best_bid, best_ask - 0.001))
        
        return {
            'best_ask': round(best_ask, 4),
            'best_bid': round(best_bid, 4),
            'spread': round(spread_pred, 4),
            'midpoint': round(midpoint_pred, 4),
            'method': 'kline_based_ml',
        }
```

#### 第3步：使用示例

```python
# 1. 准备训练数据（需要同时有K线和盘口）
training_data = []

for record in real_data_with_orderbook:
    # 提取K线特征
    kline_features = extract_kline_features(record['window_klines'])
    
    training_data.append({
        'kline_features': kline_features,
        'orderbook': record['orderbook'],
        'confidence': record['confidence'],
    })

# 2. 训练模拟器
simulator = KlineBasedOrderBookSimulator(training_data)

# 3. 为新数据生成盘口
for record in records_without_orderbook:
    # 提取K线特征
    kline_features = extract_kline_features(record['window_klines'])
    
    # 模拟盘口
    simulated_book = simulator.simulate(
        kline_features=kline_features,
        confidence=record['confidence']
    )
    
    record['orderbook'] = simulated_book
```

---

## 🔬 验证方法

### 1. 特征相关性验证

```python
def validate_feature_correlation(real_data):
    """验证K线特征与盘口的相关性"""
    
    import pandas as pd
    
    # 提取数据
    data = []
    for d in real_data:
        row = {
            'volatility': d['kline_features']['volatility_1m'],
            'spread': d['orderbook']['best_ask'] - d['orderbook']['best_bid'],
            'price_change': d['kline_features']['price_change_pct'],
            'confidence': d['confidence'],
        }
        data.append(row)
    
    df = pd.DataFrame(data)
    
    # 相关性矩阵
    corr = df.corr()
    print("相关性矩阵:")
    print(corr)
    
    # 关键验证
    vol_spread_corr = corr.loc['volatility', 'spread']
    print(f"\n波动率 vs Spread 相关性: {vol_spread_corr:.3f}")
    
    if vol_spread_corr > 0.3:
        print("✓ 正相关（符合预期）")
    else:
        print("⚠ 相关性较弱")
```

### 2. 预测精度验证

```python
def validate_prediction_accuracy(simulator, test_data):
    """验证模拟器的预测精度"""
    
    predictions = []
    actuals = []
    
    for d in test_data:
        # 预测
        pred = simulator.simulate(
            kline_features=d['kline_features'],
            confidence=d['confidence'],
            add_noise=False  # 不加噪声，测试纯预测能力
        )
        
        # 实际
        actual = d['orderbook']
        
        predictions.append(pred['spread'])
        actuals.append(actual['best_ask'] - actual['best_bid'])
    
    # 计算误差
    mae = np.mean(np.abs(np.array(predictions) - np.array(actuals)))
    rmse = np.sqrt(np.mean((np.array(predictions) - np.array(actuals))**2))
    
    print(f"预测误差:")
    print(f"  MAE: {mae:.4f}")
    print(f"  RMSE: {rmse:.4f}")
    
    # 相对误差
    mape = np.mean(np.abs((np.array(predictions) - np.array(actuals)) / np.array(actuals))) * 100
    print(f"  MAPE: {mape:.2f}%")
    
    if mape < 20:
        print("✓ 预测精度良好（<20%）")
    else:
        print("⚠ 预测精度需要改进")
```

### 3. 回测验证（最重要）

```python
def validate_backtest(real_book_data, simulated_book_data):
    """对比真实盘口和模拟盘口的回测结果"""
    
    # 用真实盘口回测
    results_real = backtest_with_orderbook(real_book_data)
    
    # 用模拟盘口回测
    results_sim = backtest_with_orderbook(simulated_book_data)
    
    print("回测对比:")
    print(f"  真实盘口: 胜率={results_real.win_rate:.2%}, PnL={results_real.total_pnl:.2f}")
    print(f"  模拟盘口: 胜率={results_sim.win_rate:.2%}, PnL={results_sim.total_pnl:.2f}")
    
    diff_wr = abs(results_real.win_rate - results_sim.win_rate)
    diff_pnl = abs(results_real.total_pnl - results_sim.total_pnl) / abs(results_real.total_pnl)
    
    print(f"\n差异:")
    print(f"  胜率差异: {diff_wr:.2%}")
    print(f"  PnL差异: {diff_pnl:.2%}")
    
    if diff_wr < 0.05 and diff_pnl < 0.15:
        print("✓ 模拟盘口可靠（差异<5%胜率，<15%PnL）")
    else:
        print("⚠ 模拟盘口偏差较大，需要改进")
```

---

## 📊 数据需求

### 必需数据

对于每条记录，需要：

1. **K线数据** ✅
   - 你们已经有Binance 1s数据
   - 可以计算所有波动率、趋势等特征

2. **盘口数据** ⚠️
   - 目前只有~25条
   - 需要这25条同时有K线和盘口

3. **模型预测** ✅
   - confidence, fair_prob等
   - 你们已经有

### 数据准备

```python
def prepare_training_data():
    """准备训练数据"""
    
    # 1. 加载有盘口的记录
    records_with_book = load_records_with_orderbook()  # ~25条
    
    # 2. 为每条记录加载对应的K线
    training_data = []
    
    for record in records_with_book:
        # 获取窗口的K线数据
        window_start = record['window_start_ms']
        window_end = record['ts_ms']
        
        klines = load_binance_klines(
            symbol='BTCUSDT',
            interval='1s',
            start_time=window_start,
            end_time=window_end
        )
        
        # 提取特征
        kline_features = extract_kline_features(klines)
        
        training_data.append({
            'kline_features': kline_features,
            'orderbook': record['orderbook'],
            'confidence': record['confidence'],
        })
    
    return training_data
```

---

## 🎯 完整流程

### 第1步：准备训练数据

```bash
python prepare_kline_orderbook_data.py \
    --real-orderbook logs/shadow_signals_3h_book_v2.jsonl \
    --binance-cache data_cache \
    --output training_data.pkl
```

### 第2步：训练模拟器

```bash
python train_orderbook_simulator.py \
    --training-data training_data.pkl \
    --output orderbook_simulator.pkl
```

### 第3步：验证

```bash
python validate_simulator.py \
    --simulator orderbook_simulator.pkl \
    --test-data test_data.pkl
```

### 第4步：生成模拟数据

```bash
python generate_simulated_orderbook.py \
    --simulator orderbook_simulator.pkl \
    --input-data all_records.jsonl \
    --output augmented_data.jsonl
```

### 第5步：训练模型

```bash
python train_ev_model.py \
    --data augmented_data.jsonl \
    --output ev_prediction_model.pkl
```

---

## ⚠️ 关键改进点

### vs 之前的方案

| 方面 | 之前方案 | 改进方案 |
|------|---------|---------|
| **输入特征** | 仅置信度 | K线特征 + 置信度 |
| **理论基础** | 统计采样 | 市场微观结构 + ML |
| **可验证性** | 统计检验 | 统计 + 特征相关性 + 回测 |
| **准确性** | 中等 | 更高（考虑市场状态） |
| **可解释性** | 低 | 高（特征重要性） |

### 关键优势

1. ✅ **考虑市场状态** - 波动率、趋势、流动性
2. ✅ **理论支撑** - 做市商模型、市场微观结构
3. ✅ **可验证** - 特征相关性、预测精度、回测对比
4. ✅ **可解释** - 知道哪些因素影响盘口
5. ✅ **可改进** - 随着数据增加，模型自动改进

---

## 📝 总结

**你的洞察非常正确！** 盘口必须与K线相关。

**改进方案**:
- ✅ 提取K线特征（波动率、趋势、成交量等）
- ✅ 训练K线特征 → 盘口的映射模型
- ✅ 基于市场状态模拟盘口
- ✅ 多层验证（特征相关性 + 预测精度 + 回测）

**下一步**: 我可以立即实现这个改进的模拟器！

需要我开始编码吗？ 🚀
