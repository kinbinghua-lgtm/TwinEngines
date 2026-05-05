# 盘口数据模拟方案设计

## 📊 现有数据统计

### 本地数据
- **总记录数**: 1,139条
- **有完整盘口**: 11条 (1%)
- **盘口特征**:
  - Best Ask均值: 0.3818
  - Spread均值: 0.0109 (1.09%)
  - Bid/Ask比率: 0.9360

### VPS数据
- **shadow_signals.jsonl**: 14条 (100%有盘口)
- **naked_live_ticks.jsonl**: 7,341条

### 合计
- **有盘口数据**: ~25条
- **无盘口数据**: ~8,000条

## 🎯 模拟目标

基于25条真实盘口数据，模拟出8,000+条的盘口信息，要求：
1. ✅ 统计特征与真实数据一致
2. ✅ 考虑市场微观结构
3. ✅ 反映置信度和价格的关系
4. ✅ 可重现（用于验证）

---

## 💡 可靠的模拟方案

### 方案1: 基于经验分布的Bootstrap采样 ⭐⭐⭐⭐⭐

**原理**: 从真实数据中学习分布，然后重采样

**优势**:
- ✅ 统计学上最可靠
- ✅ 保留真实数据的所有特征
- ✅ 不引入假设偏差
- ✅ 学术界广泛认可

**实现**:

```python
import numpy as np
from scipy import stats

class OrderBookSimulator:
    def __init__(self, real_data):
        """
        real_data: 真实盘口数据
        [
            {'best_ask': 0.38, 'best_bid': 0.37, 'confidence': 0.55, ...},
            ...
        ]
        """
        self.real_data = real_data
        self._fit_distributions()
    
    def _fit_distributions(self):
        """拟合统计分布"""
        
        # 1. Spread分布（最重要）
        spreads = [d['best_ask'] - d['best_bid'] for d in self.real_data]
        self.spread_mean = np.mean(spreads)
        self.spread_std = np.std(spreads)
        
        # 2. Bid/Ask比率分布
        ratios = [d['best_bid'] / d['best_ask'] for d in self.real_data]
        self.ratio_mean = np.mean(ratios)
        self.ratio_std = np.std(ratios)
        
        # 3. 按置信度分组的Ask价格分布
        self.ask_by_conf = {}
        for d in self.real_data:
            conf_bin = round(d['confidence'], 1)  # 0.5, 0.6, 0.7...
            if conf_bin not in self.ask_by_conf:
                self.ask_by_conf[conf_bin] = []
            self.ask_by_conf[conf_bin].append(d['best_ask'])
    
    def simulate(self, confidence, fair_price=None, seed=None):
        """
        模拟一个盘口
        
        参数:
            confidence: 模型置信度 (0.5-1.0)
            fair_price: 公平价格（可选）
            seed: 随机种子（可重现）
        
        返回:
            {'best_ask': ..., 'best_bid': ..., 'spread': ...}
        """
        if seed is not None:
            np.random.seed(seed)
        
        # 方法1: 基于置信度的条件分布
        conf_bin = round(confidence, 1)
        
        if conf_bin in self.ask_by_conf and len(self.ask_by_conf[conf_bin]) > 0:
            # 从同置信度区间的真实数据中采样
            best_ask = np.random.choice(self.ask_by_conf[conf_bin])
        else:
            # 从最近的置信度区间采样
            nearest_bin = min(self.ask_by_conf.keys(), 
                            key=lambda x: abs(x - conf_bin))
            best_ask = np.random.choice(self.ask_by_conf[nearest_bin])
        
        # 方法2: 如果有公平价格，添加噪声
        if fair_price is not None:
            # Ask应该略高于公平价格
            noise = np.random.normal(0, 0.02)  # 2%噪声
            best_ask = fair_price * (1 + abs(noise))
            best_ask = np.clip(best_ask, 0.01, 0.99)
        
        # 生成Spread（从真实分布采样）
        spread = np.random.normal(self.spread_mean, self.spread_std)
        spread = max(0.001, spread)  # 至少0.1%
        
        # 计算Bid
        best_bid = best_ask - spread
        best_bid = max(0.01, min(best_bid, best_ask - 0.001))
        
        return {
            'best_ask': round(best_ask, 4),
            'best_bid': round(best_bid, 4),
            'spread': round(spread, 4),
            'midpoint': round((best_ask + best_bid) / 2, 4),
        }
```

**使用示例**:

```python
# 1. 加载真实数据
real_data = load_real_orderbook_data()  # 25条真实数据

# 2. 创建模拟器
simulator = OrderBookSimulator(real_data)

# 3. 为每条无盘口的记录生成模拟盘口
for record in records_without_book:
    simulated_book = simulator.simulate(
        confidence=record['confidence'],
        fair_price=record.get('fair_prob_side'),
        seed=hash(record['window_id'])  # 可重现
    )
    
    record['best_ask'] = simulated_book['best_ask']
    record['best_bid'] = simulated_book['best_bid']
```

---

### 方案2: 基于市场微观结构的参数化模型 ⭐⭐⭐⭐

**原理**: 使用金融学的市场微观结构理论

**模型**: Kyle模型 + Roll模型

```python
class MicrostructureSimulator:
    def __init__(self, real_data):
        self.real_data = real_data
        self._estimate_parameters()
    
    def _estimate_parameters(self):
        """估计市场微观结构参数"""
        
        # 1. 信息不对称参数 (λ)
        # 高置信度 → 更多信息 → 更小的spread
        
        # 2. 库存成本参数 (γ)
        # 做市商的库存风险
        
        # 3. 订单处理成本 (c)
        # 固定成本
        
        spreads = [d['best_ask'] - d['best_bid'] for d in self.real_data]
        confidences = [d['confidence'] for d in self.real_data]
        
        # 简化的线性关系: spread = c + λ * (1 - confidence)
        from scipy.optimize import curve_fit
        
        def spread_model(conf, c, lam):
            return c + lam * (1 - conf)
        
        params, _ = curve_fit(spread_model, confidences, spreads)
        self.c = params[0]  # 固定成本
        self.lam = params[1]  # 信息参数
    
    def simulate(self, confidence, fair_price):
        """基于微观结构模型模拟"""
        
        # 计算理论spread
        spread = self.c + self.lam * (1 - confidence)
        spread = max(0.001, spread)
        
        # Ask = fair_price + spread/2
        # Bid = fair_price - spread/2
        best_ask = fair_price + spread / 2
        best_bid = fair_price - spread / 2
        
        # 添加随机噪声（市场冲击）
        noise = np.random.normal(0, 0.005)
        best_ask += noise
        best_bid += noise
        
        return {
            'best_ask': np.clip(best_ask, 0.01, 0.99),
            'best_bid': np.clip(best_bid, 0.01, 0.99),
            'spread': spread,
        }
```

---

### 方案3: 混合方法（最推荐）⭐⭐⭐⭐⭐

**结合方案1和方案2的优势**

```python
class HybridOrderBookSimulator:
    def __init__(self, real_data):
        self.bootstrap_sim = OrderBookSimulator(real_data)
        self.micro_sim = MicrostructureSimulator(real_data)
        self.real_data = real_data
    
    def simulate(self, confidence, fair_price=None, method='auto'):
        """
        混合模拟方法
        
        method:
            'auto' - 自动选择最佳方法
            'bootstrap' - 纯Bootstrap
            'microstructure' - 纯微观结构
            'blend' - 混合
        """
        
        if method == 'auto':
            # 如果有同置信度区间的真实数据，用Bootstrap
            conf_bin = round(confidence, 1)
            if conf_bin in self.bootstrap_sim.ask_by_conf:
                method = 'bootstrap'
            else:
                method = 'microstructure'
        
        if method == 'bootstrap':
            return self.bootstrap_sim.simulate(confidence, fair_price)
        
        elif method == 'microstructure':
            if fair_price is None:
                # 估计fair_price
                fair_price = self._estimate_fair_price(confidence)
            return self.micro_sim.simulate(confidence, fair_price)
        
        elif method == 'blend':
            # 混合两种方法
            book1 = self.bootstrap_sim.simulate(confidence, fair_price)
            book2 = self.micro_sim.simulate(confidence, fair_price)
            
            # 加权平均
            alpha = 0.7  # Bootstrap权重
            return {
                'best_ask': alpha * book1['best_ask'] + (1-alpha) * book2['best_ask'],
                'best_bid': alpha * book1['best_bid'] + (1-alpha) * book2['best_bid'],
                'spread': alpha * book1['spread'] + (1-alpha) * book2['spread'],
            }
    
    def _estimate_fair_price(self, confidence):
        """根据置信度估计公平价格"""
        # 简化假设: confidence越高，fair_price越接近极端值
        if confidence > 0.5:
            return 0.3 + (confidence - 0.5) * 0.4  # 0.3-0.5
        else:
            return 0.5 - (0.5 - confidence) * 0.4  # 0.3-0.5
```

---

## 🔬 验证方法

### 1. 统计检验

```python
def validate_simulation(real_data, simulated_data):
    """验证模拟数据的质量"""
    
    from scipy import stats
    
    # 1. Kolmogorov-Smirnov检验（分布一致性）
    real_spreads = [d['spread'] for d in real_data]
    sim_spreads = [d['spread'] for d in simulated_data]
    
    ks_stat, p_value = stats.ks_2samp(real_spreads, sim_spreads)
    print(f"KS检验: statistic={ks_stat:.4f}, p-value={p_value:.4f}")
    
    if p_value > 0.05:
        print("✓ 分布一致（p>0.05）")
    else:
        print("✗ 分布不一致（p<0.05）")
    
    # 2. 均值和方差检验
    print(f"\nSpread统计:")
    print(f"  真实数据: mean={np.mean(real_spreads):.4f}, std={np.std(real_spreads):.4f}")
    print(f"  模拟数据: mean={np.mean(sim_spreads):.4f}, std={np.std(sim_spreads):.4f}")
    
    # 3. 分位数对比
    for q in [0.25, 0.5, 0.75]:
        real_q = np.quantile(real_spreads, q)
        sim_q = np.quantile(sim_spreads, q)
        print(f"  {int(q*100)}%分位数: 真实={real_q:.4f}, 模拟={sim_q:.4f}")
```

### 2. 回测对比

```python
# 用真实盘口训练模型A
model_A = train_model(data_with_real_book)

# 用模拟盘口训练模型B
model_B = train_model(data_with_simulated_book)

# 在测试集上对比
test_results_A = backtest(model_A, test_data)
test_results_B = backtest(model_B, test_data)

print(f"真实盘口模型: 胜率={test_results_A.win_rate:.2%}")
print(f"模拟盘口模型: 胜率={test_results_B.win_rate:.2%}")
print(f"差异: {abs(test_results_A.win_rate - test_results_B.win_rate):.2%}")
```

---

## 📝 实施步骤

### 第1步: 收集所有真实盘口数据

```bash
# 本地
python collect_real_orderbook.py --local

# VPS
python collect_real_orderbook.py --vps
```

### 第2步: 训练模拟器

```python
# 加载真实数据
real_data = load_all_real_orderbook()

# 创建模拟器
simulator = HybridOrderBookSimulator(real_data)

# 保存模拟器参数
simulator.save('orderbook_simulator.pkl')
```

### 第3步: 生成模拟数据

```python
# 为所有无盘口的记录生成模拟盘口
for record in all_records:
    if not has_orderbook(record):
        simulated = simulator.simulate(
            confidence=record['confidence'],
            fair_price=record.get('fair_prob_side'),
            seed=hash(record['window_id'])
        )
        record.update(simulated)

# 保存
save_augmented_data(all_records, 'data_with_simulated_book.jsonl')
```

### 第4步: 验证

```python
# 统计验证
validate_simulation(real_data, simulated_data)

# 回测验证
compare_models(real_book_model, simulated_book_model)
```

### 第5步: 训练模型

```python
# 用模拟数据训练
model = train_ev_prediction_model(data_with_simulated_book)

# 评估
evaluate_model(model, test_data)
```

---

## ⚠️ 注意事项

### 1. 模拟的局限性

- ⚠️ 无法完全捕捉极端市场条件
- ⚠️ 可能低估尾部风险
- ⚠️ 假设市场结构稳定

### 2. 使用建议

- ✅ 用于初步参数探索
- ✅ 用于快速迭代测试
- ⚠️ 最终模型必须用真实数据验证
- ❌ 不要直接用于实盘决策

### 3. 持续改进

- 随着真实数据积累，定期重新训练模拟器
- 对比模拟vs真实的偏差
- 调整模拟参数

---

## 🎯 总结

**推荐方案**: 混合方法（Bootstrap + 微观结构）

**优势**:
- ✅ 统计学上可靠
- ✅ 考虑市场微观结构
- ✅ 可验证、可重现
- ✅ 学术界认可

**使用场景**:
- ✅ 初步参数调优
- ✅ 快速原型验证
- ✅ 策略回测

**最终验证**:
- ⚠️ 必须用真实数据验证
- ⚠️ 持续监控偏差
- ⚠️ 逐步替换为真实数据

---

**下一步**: 我可以立即实现这个模拟器，需要我开始编码吗？
