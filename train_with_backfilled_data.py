#!/usr/bin/env python3
"""用回填后的数据训练EV模型"""

import json
import pickle
from scipy.optimize import minimize
from math import exp

def train_with_results():
    print("="*60)
    print("用回填后的数据训练EV模型")
    print("="*60)
    print()
    
    # 1. 加载数据
    print("1. 加载数据...")
    data = []
    
    with open('shadow_signals_with_results.jsonl', 'r') as f:
        for line in f:
            try:
                obj = json.loads(line.strip())
                
                d = obj.get('d_abs_pct', 0)
                T = obj.get('t_remaining_sec', 0)
                
                poly = obj.get('polymarket', {})
                book = poly.get('book', {})
                
                ask = book.get('best_ask')
                bid = book.get('best_bid')
                
                # 现在有结果了！
                reversal = obj.get('final_reversal')
                
                if ask and bid and 0 < ask < 0.99 and reversal is not None:
                    data.append({
                        'd': float(d),
                        'T': float(T),
                        'ask': float(ask),
                        'reversal': bool(reversal),
                    })
            except Exception as e:
                print(f"  跳过一条: {e}")
    
    print(f"  加载了 {len(data)} 条完整数据")
    
    # 统计
    reversals = sum(1 for d in data if d['reversal'])
    print(f"  反转: {reversals}, 不反转: {len(data)-reversals}")
    print(f"  胜率: {reversals/len(data)*100:.1f}%")
    print()
    
    # 2. 训练
    print("2. 训练EV优化模型...")
    
    def survival_formula(d, T, lambda0, gamma, beta):
        try:
            g = gamma + 1.0
            H = lambda0 * exp(beta * d) * (T ** g) / ((120 ** gamma) * g)
            H = min(H, 50)
            p = 1 - exp(-H)
            return max(0.001, min(0.999, p))
        except:
            return 0.5
    
    def ev_loss(params, data):
        lambda0, gamma, beta = params
        
        total_ev = 0
        
        for window in data:
            d = window['d']
            T = window['T']
            ask = window['ask']
            reversal = window['reversal']
            
            # 预测概率
            p_rev = survival_formula(d, T, lambda0, gamma, beta)
            
            # 计算EV
            ev = p_rev * (1 - ask) - (1 - p_rev) * ask
            
            # 决策
            if ev > 0:
                # 下单
                if reversal:
                    actual_ev = (1 - ask)
                else:
                    actual_ev = -ask
            else:
                # 不下单
                actual_ev = 0
            
            total_ev += actual_ev
        
        return -total_ev
    
    result = minimize(
        ev_loss,
        x0=[0.1, 1.0, 0.0],
        args=(data,),
        method='L-BFGS-B',
        bounds=[
            (1e-6, 20.0),
            (0.1, 4.0),
            (-8.0, 8.0),
        ]
    )
    
    lambda0, gamma, beta = result.x
    total_ev = -result.fun
    
    print(f"  [OK] 训练完成")
    print(f"  lambda0: {lambda0:.4f}")
    print(f"  gamma: {gamma:.4f}")
    print(f"  beta: {beta:.4f}")
    print(f"  总EV: {total_ev:.2f}")
    print()
    
    # 3. 分析决策
    print("3. 分析决策...")
    trades = 0
    wins = 0
    
    for window in data:
        d = window['d']
        T = window['T']
        ask = window['ask']
        reversal = window['reversal']
        
        p_rev = survival_formula(d, T, lambda0, gamma, beta)
        ev = p_rev * (1 - ask) - (1 - p_rev) * ask
        
        if ev > 0:
            trades += 1
            if reversal:
                wins += 1
    
    print(f"  会下单: {trades}/{len(data)}")
    if trades > 0:
        print(f"  下单胜率: {wins/trades*100:.1f}%")
    print()
    
    # 4. 保存
    print("4. 保存参数...")
    with open('ev_model_params_trained.pkl', 'wb') as f:
        pickle.dump({
            'lambda0': float(lambda0),
            'gamma': float(gamma),
            'beta': float(beta),
            'total_ev': float(total_ev),
            'n_samples': len(data),
            'n_trades': trades,
            'n_wins': wins,
        }, f)
    
    print("  [OK] 已保存到 ev_model_params_trained.pkl")
    print()
    
    print("="*60)
    print("完成")
    print("="*60)
    print()
    print("[OK] EV模型训练完成！")
    print()
    print("训练结果:")
    print(f"  样本数: {len(data)}")
    print(f"  lambda0: {lambda0:.4f}")
    print(f"  gamma: {gamma:.4f}")
    print(f"  beta: {beta:.4f}")
    print(f"  总EV: {total_ev:.2f}")
    print(f"  会下单: {trades}/{len(data)}")
    if trades > 0:
        print(f"  下单胜率: {wins/trades*100:.1f}%")
    print()
    print("下一步: 更新采集器，自动添加结算结果")
    print()

if __name__ == "__main__":
    train_with_results()
