# 重新训练生存模型参数 - 基于EV目标

## 🎯 核心思想

### 保持生存模型的公式结构

```python
# 公式不变：
P(reversal | d, T) = 1 - exp(-H(d, T))

其中：
H(d, T) = lambda0 * exp(beta * d) * T^(gamma+1) / (120^gamma * (gamma+1))

参数：lambda0, gamma, beta
```

### 改变训练目标

**现有训练目标**：
```python
# 最大化预测准确率
# 目标：P(reversal) 接近实际是否反转

log_likelihood = sum(
    event * log(P) + (1-event) * log(1-P)
)

# event = 1 if reversal else 0
```

**新的训练目标**：
```python
# 最大化EV
# 目标：优化实际收益

# 假设每个窗口都可以成交
# 如果下单：
if reversal:
    ev = (1 - best_ask)  # 赢
else:
    ev = -best_ask  # 输

# 训练目标：
# 找到 lambda0, gamma, beta
# 使得预测的 P(reversal) 能最大化总EV
```

---

## 📊 新的训练方法

### 方法1：直接优化EV（推荐）

```python
def ev_loss(params, data):
    """
    EV损失函数
    
    参数:
        params: [lambda0, gamma, beta]
        data: 训练数据
    
    返回:
        -total_ev (负的总EV，因为要最小化)
    """
    lambda0, gamma, beta = params
    
    total_ev = 0
    
    for window in data:
        d = window['d_abs_pct']
        T = window['t_remaining']
        best_ask = window['best_ask']
        reversal = window['reversal']  # 实际是否反转
        
        # 计算预测概率
        H = lambda0 * exp(beta * d) * T**(gamma+1) / (120**gamma * (gamma+1))
        p_rev = 1 - exp(-H)
        
        # 计算期望EV
        # 假设我们按照 p_rev 的概率下注
        # 如果 p_rev > threshold，我们下单
        
        if p_rev > 0.5:  # 简化：概率>0.5就下单
            if reversal:
                ev = (1 - best_ask)  # 实际赢了
            else:
                ev = -best_ask  # 实际输了
        else:
            ev = 0  # 不下单
        
        total_ev += ev
    
    return -total_ev  # 返回负值，因为优化器是最小化


# 训练
from scipy.optimize import minimize

result = minimize(
    ev_loss,
    x0=[0.1, 1.0, 0.0],  # 初始参数
    args=(training_data,),
    method='L-BFGS-B',
    bounds=[
        (1e-6, 20.0),   # lambda0
        (0.1, 4.0),     # gamma
        (-8.0, 8.0),    # beta
    ]
)

lambda0, gamma, beta = result.x
```

---

### 方法2：加权似然（更稳定）

```python
def weighted_likelihood(params, data):
    """
    加权似然函数
    
    权重 = EV的大小
    EV大的样本权重高
    """
    lambda0, gamma, beta = params
    
    log_likelihood = 0
    
    for window in data:
        d = window['d_abs_pct']
        T = window['t_remaining']
        best_ask = window['best_ask']
        reversal = window['reversal']
        
        # 计算预测概率
        H = lambda0 * exp(beta * d) * T**(gamma+1) / (120**gamma * (gamma+1))
        p_rev = 1 - exp(-H)
        p_rev = clip(p_rev, 1e-12, 1-1e-12)
        
        # 计算权重（基于潜在EV）
        if reversal:
            weight = (1 - best_ask)  # 赢的收益
        else:
            weight = best_ask  # 输的损失
        
        # 加权似然
        if reversal:
            ll = weight * log(p_rev)
        else:
            ll = weight * log(1 - p_rev)
        
        log_likelihood += ll
    
    return -log_likelihood


# 训练
result = minimize(
    weighted_likelihood,
    x0=[0.1, 1.0, 0.0],
    args=(training_data,),
    method='L-BFGS-B',
    bounds=[
        (1e-6, 20.0),
        (0.1, 4.0),
        (-8.0, 8.0),
    ]
)
```

---

### 方法3：两阶段训练（最稳定）

```python
def two_stage_training(data):
    """
    两阶段训练
    
    阶段1：用传统方法训练，得到初始参数
    阶段2：用EV目标微调
    """
    
    # 阶段1：传统MLE训练
    params_stage1 = fit_survival_mle(data)
    
    print(f"阶段1参数: lambda0={params_stage1.lambda0:.4f}, "
          f"gamma={params_stage1.gamma:.4f}, beta={params_stage1.beta:.4f}")
    
    # 阶段2：基于EV微调
    result = minimize(
        ev_loss,
        x0=[params_stage1.lambda0, params_stage1.gamma, params_stage1.beta],
        args=(data,),
        method='L-BFGS-B',
        bounds=[
            (params_stage1.lambda0 * 0.5, params_stage1.lambda0 * 2.0),
            (params_stage1.gamma * 0.8, params_stage1.gamma * 1.2),
            (params_stage1.beta - 1.0, params_stage1.beta + 1.0),
        ]
    )
    
    lambda0, gamma, beta = result.x
    
    print(f"阶段2参数: lambda0={lambda0:.4f}, "
          f"gamma={gamma:.4f}, beta={beta:.4f}")
    
    return SurvivalParams(lambda0=lambda0, gamma=gamma, beta=beta)
```

---

## 🔄 滚动训练实现

```python
#!/usr/bin/env python3
"""
滚动训练生存模型参数
每10小时训练一次
"""

import json
import numpy as np
from scipy.optimize import minimize
from datetime import datetime, timedelta
import pickle

class RollingSurvivalTrainer:
    def __init__(self):
        self.data_file = 'logs/shadow_signals.jsonl'
        self.results_file = 'logs/survival_rolling_results.jsonl'
        self.models_dir = 'models/survival_rolling'
    
    def load_training_data(self, start_time, end_time):
        """
        加载训练数据
        
        返回：
        [
            {
                'd_abs_pct': float,
                't_remaining': float,
                'best_ask': float,
                'reversal': bool,  # 实际是否反转
            },
            ...
        ]
        """
        data = []
        
        start_ms = int(start_time.timestamp() * 1000)
        end_ms = int(end_time.timestamp() * 1000)
        
        with open(self.data_file, 'r') as f:
            for line in f:
                obj = json.loads(line.strip())
                
                ts_ms = obj.get('ts_ms', 0)
                if not (start_ms <= ts_ms < end_ms):
                    continue
                
                # 提取特征
                d_abs_pct = obj.get('d_abs_pct', 0)
                t_remaining = obj.get('t_remaining_sec', 0)
                
                poly = obj.get('polymarket', {})
                book = poly.get('book', {})
                best_ask = book.get('best_ask', 0)
                
                # 实际是否反转
                reversal = obj.get('final_reversal', False)
                
                if best_ask and best_ask < 0.99:
                    data.append({
                        'd_abs_pct': float(d_abs_pct),
                        't_remaining': float(t_remaining),
                        'best_ask': float(best_ask),
                        'reversal': bool(reversal),
                    })
        
        return data
    
    def ev_loss(self, params, data):
        """EV损失函数"""
        lambda0, gamma, beta = params
        
        total_ev = 0
        
        for window in data:
            d = window['d_abs_pct']
            T = window['t_remaining']
            best_ask = window['best_ask']
            reversal = window['reversal']
            
            # 计算预测概率
            try:
                g = gamma + 1.0
                H = lambda0 * np.exp(beta * d) * (T ** g) / ((120 ** gamma) * g)
                H = np.clip(H, 0, 50)
                p_rev = 1 - np.exp(-H)
            except:
                p_rev = 0.5
            
            # 决策：如果 p_rev > 0.5，下单
            if p_rev > 0.5:
                if reversal:
                    ev = (1 - best_ask)
                else:
                    ev = -best_ask
            else:
                ev = 0
            
            total_ev += ev
        
        return -total_ev  # 最小化负EV = 最大化EV
    
    def train_params(self, data):
        """训练参数"""
        
        print(f"训练样本数: {len(data)}")
        
        if len(data) < 50:
            print("数据不足")
            return None
        
        # 初始参数（可以用传统方法先训练一次）
        x0 = [0.1, 1.0, 0.0]
        
        # 优化
        result = minimize(
            self.ev_loss,
            x0,
            args=(data,),
            method='L-BFGS-B',
            bounds=[
                (1e-6, 20.0),
                (0.1, 4.0),
                (-8.0, 8.0),
            ]
        )
        
        lambda0, gamma, beta = result.x
        
        print(f"训练结果:")
        print(f"  lambda0: {lambda0:.4f}")
        print(f"  gamma: {gamma:.4f}")
        print(f"  beta: {beta:.4f}")
        print(f"  总EV: {-result.fun:.2f}")
        
        return {
            'lambda0': float(lambda0),
            'gamma': float(gamma),
            'beta': float(beta),
            'total_ev': float(-result.fun),
            'success': bool(result.success),
        }
    
    def run_iteration(self):
        """运行一次迭代"""
        
        now = datetime.now()
        
        print(f"\n{'='*60}")
        print(f"滚动训练 - {now}")
        print(f"{'='*60}\n")
        
        # 1. 加载所有历史数据训练
        first_time = self.get_first_data_time()
        train_data = self.load_training_data(first_time, now)
        
        print(f"训练时间范围: {first_time} -> {now}")
        print(f"训练时长: {(now - first_time).total_seconds() / 3600:.1f} 小时")
        
        # 2. 训练
        params = self.train_params(train_data)
        
        if params is None:
            return
        
        # 3. 保存
        model_path = f"{self.models_dir}/params_{now.strftime('%Y%m%d_%H%M%S')}.pkl"
        with open(model_path, 'wb') as f:
            pickle.dump({
                'params': params,
                'trained_at': now.isoformat(),
                'n_samples': len(train_data),
            }, f)
        
        print(f"\n参数已保存: {model_path}")
        
        # 4. 验证上一次的参数（10小时前）
        self.validate_previous_params(now)
    
    def validate_previous_params(self, now):
        """验证上一次训练的参数"""
        
        # 加载10小时前的参数
        target_time = now - timedelta(hours=10)
        
        # 找到最接近的模型
        import glob
        models = sorted(glob.glob(f"{self.models_dir}/params_*.pkl"))
        
        for model_path in reversed(models):
            with open(model_path, 'rb') as f:
                model_data = pickle.load(f)
            
            trained_at = datetime.fromisoformat(model_data['trained_at'])
            
            if abs((trained_at - target_time).total_seconds()) < 3600:
                print(f"\n验证参数: {model_path}")
                
                # 加载验证数据（过去10小时）
                val_start = now - timedelta(hours=10)
                val_data = self.load_training_data(val_start, now)
                
                print(f"验证样本数: {len(val_data)}")
                
                # 计算验证集上的EV
                params = model_data['params']
                val_ev = -self.ev_loss(
                    [params['lambda0'], params['gamma'], params['beta']],
                    val_data
                )
                
                print(f"验证集总EV: {val_ev:.2f}")
                
                # 记录结果
                result = {
                    'timestamp': now.isoformat(),
                    'model_path': model_path,
                    'params': params,
                    'val_ev': float(val_ev),
                    'n_val_samples': len(val_data),
                }
                
                with open(self.results_file, 'a') as f:
                    f.write(json.dumps(result) + '\n')
                
                print(f"结果已记录")
                
                break
    
    def get_first_data_time(self):
        """获取第一条数据的时间"""
        with open(self.data_file, 'r') as f:
            first_line = f.readline()
            obj = json.loads(first_line)
            ts_ms = obj.get('ts_ms', 0)
            return datetime.fromtimestamp(ts_ms / 1000)

if __name__ == "__main__":
    trainer = RollingSurvivalTrainer()
    trainer.run_iteration()
```

---

## 🎯 总结

### 核心思想 ✅

1. **保持公式结构**
   ```python
   P(reversal) = 1 - exp(-H(d, T))
   H = lambda0 * exp(beta*d) * T^(gamma+1) / ...
   ```

2. **改变训练目标**
   - 不是：最大化预测准确率
   - 而是：**最大化总EV**

3. **假设都能成交**
   ```python
   if p_rev > 0.5:
       if reversal:
           ev = (1 - best_ask)
       else:
           ev = -best_ask
   ```

4. **滚动训练**
   - 每10小时训练一次
   - 用所有历史数据
   - 验证下一个10小时

### 这样对吗？ ✅

**要开始实现完整代码吗？** 🚀
