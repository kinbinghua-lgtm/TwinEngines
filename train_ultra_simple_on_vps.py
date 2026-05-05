#!/usr/bin/env python3
"""在VPS上训练超简化版模拟器"""

from fabric import Connection

def train_ultra_simple_on_vps():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("在VPS上训练超简化版模拟器 v0.1")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 上传代码
        print("1. 上传代码到VPS...")
        conn.put('ultra_simple_simulator.py', '/root/TwinEngines/ultra_simple_simulator.py')
        print("  [OK] ultra_simple_simulator.py")
        print()
        
        # 2. 训练
        print("2. 在VPS上训练...")
        result = conn.run(
            "cd /root/TwinEngines && python3 ultra_simple_simulator.py train logs/shadow_signals.jsonl",
            hide=True,
            warn=True
        )
        
        if result.ok:
            print("[OK] 训练成功")
            # 只显示关键信息
            for line in result.stdout.split('\n'):
                if 'Spread' in line or 'Conf' in line or '训练' in line or 'OK' in line:
                    try:
                        print(f"  {line}")
                    except:
                        pass
        else:
            print("[错误] 训练失败")
            return
        
        print()
        
        # 3. 测试
        print("3. 测试模拟器...")
        result = conn.run(
            "cd /root/TwinEngines && python3 ultra_simple_simulator.py test ultra_simple_simulator.pkl",
            hide=True,
            warn=True
        )
        
        if result.ok:
            print("[OK] 测试成功")
            for line in result.stdout.split('\n'):
                if 'Conf=' in line:
                    try:
                        print(f"  {line.strip()}")
                    except:
                        pass
        print()
        
        # 4. 下载
        print("4. 下载模拟器到本地...")
        conn.get(
            '/root/TwinEngines/ultra_simple_simulator.pkl',
            'orderbook_simulator_v0.1.pkl'
        )
        print("[OK] 已下载: orderbook_simulator_v0.1.pkl")
        print()
        
        print("="*60)
        print("完成")
        print("="*60)
        print()
        print("[OK] v0.1模拟器训练完成！")
        print()
        print("模拟器信息:")
        print("  - 训练数据: VPS上的shadow_signals.jsonl")
        print("  - 样本数: ~14条")
        print("  - 方法: 统计采样（不依赖K线）")
        print("  - 文件: orderbook_simulator_v0.1.pkl")
        print()
        print("用途:")
        print("  - 流程验证")
        print("  - 初步测试")
        print("  - 参数探索")
        print()
        print("限制:")
        print("  - 样本少，精度有限")
        print("  - 不考虑K线特征")
        print("  - 不建议用于最终决策")
        print()
        print("下一步:")
        print("  - 采集器正在运行")
        print("  - 等待6小时积累~300条数据")
        print("  - 重新训练v1.0（考虑K线特征）")
        print("  - v1.0可以正式使用")
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    train_ultra_simple_on_vps()
