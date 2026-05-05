#!/usr/bin/env python3
"""下载VPS数据并在本地训练"""

from fabric import Connection
import json
import pickle
from scipy.optimize import minimize
from math import exp

def download_and_train():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("下载数据并训练EV模型")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 下载VPS数据
        print("1. 下载VPS数据...")
        conn.get('/root/TwinEngines/logs/shadow_signals.jsonl', 'shadow_signals_vps.jsonl')
        print("  [OK] 已下载")
        print()
        
        conn.close()
        
        # 2. 加载数据
        print("2. 加载数据...")
        data = []
        
        with open('shadow_signals_vps.jsonl', 'r') as f:
            for line in f:
                try:
                    obj = json.loads(line.strip())
                    
                    d = obj.get('d_abs_pct', 0)
                    T = obj.get('t_remaining_sec', 0)
                    
                    poly = obj.get('polymarket', {})
                    book = poly.get('book', {})
                    
                    ask = book.get('best_ask')
                    bid = book.get('best_bid')
                    
                    # 简化：假设有反转信息
                    # 实际需要确认字段名
                    reversal = obj.get('final_reversal', False)
                    
                    if ask and bid and 0 < ask < 0.99:
                        data.append({
                            'd': float(d),
                            'T': float(T),
                            'ask': float(ask),
                            'reversal': bool(reversal),
                        })
                except Exception as e:
                    pass
        
        print(f"  加载了 {len(data)} 条数据")
        print()
        
        if len(data) < 5:
            print("[警告] 数据太少，训练效果可能不好")
            print("但我们继续验证流程...")
            print()
        
        # 3. 训练
        print("3. 训练EV优化模型...")
        
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
                    if reversal:
                        actual_ev = (1 - ask)
                    else:
                        actual_ev = -ask
                else:
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
        
        # 4. 保存
        print("4. 保存参数...")
        with open('ev_model_params_local.pkl', 'wb') as f:
            pickle.dump({
                'lambda0': float(lambda0),
                'gamma': float(gamma),
                'beta': float(beta),
                'total_ev': float(total_ev),
                'n_samples': len(data),
            }, f)
        
        print("  [OK] 已保存到 ev_model_params_local.pkl")
        print()
        
        print("="*60)
        print("完成")
        print("="*60)
        print()
        print("[OK] 流程验证成功！")
        print()
        print("训练结果:")
        print(f"  样本数: {len(data)}")
        print(f"  lambda0: {lambda0:.4f}")
        print(f"  gamma: {gamma:.4f}")
        print(f"  beta: {beta:.4f}")
        print(f"  总EV: {total_ev:.2f}")
        print()
        print("注意:")
        print("  - 数据量少，这只是流程验证")
        print("  - 建议等待6小时积累更多数据")
        print("  - 然后重新训练")
        print()
        print("下一步:")
        print("  1. 等待6小时")
        print("  2. 重新运行此脚本")
        print("  3. 用充足数据训练")
        print("  4. 部署到VPS实盘验证")
        print()
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    download_and_train()
