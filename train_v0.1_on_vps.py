#!/usr/bin/env python3
"""
在VPS上训练盘口模拟器 v0.1
使用现有的25条数据
"""

from fabric import Connection
import time

def train_on_vps():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("在VPS上训练盘口模拟器 v0.1")
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
        
        files_to_upload = [
            'src/twinengines/simulation/kline_orderbook_simulator.py',
            'src/twinengines/simulation/__init__.py',
            'simple_orderbook_simulator.py',
        ]
        
        for file in files_to_upload:
            try:
                conn.put(file, f'/root/TwinEngines/{file}')
                print(f"  [OK] {file}")
            except Exception as e:
                print(f"  [警告] {file} - {e}")
        
        print()
        
        # 2. 使用简化版模拟器（不需要K线）
        print("2. 训练简化版模拟器（不依赖K线）...")
        print()
        
        result = conn.run(
            """cd /root/TwinEngines && python3 simple_orderbook_simulator.py \
                --train \
                --files logs/shadow_signals.jsonl \
                --output simple_orderbook_simulator_v0.1.pkl
            """,
            warn=True
        )
        
        print(result.stdout)
        
        if result.ok:
            print("[OK] 训练完成")
        else:
            print("[错误] 训练失败")
            print(result.stderr)
            return
        
        print()
        
        # 3. 测试模拟器
        print("3. 测试模拟器...")
        result = conn.run(
            """cd /root/TwinEngines && python3 simple_orderbook_simulator.py \
                --test \
                --simulator simple_orderbook_simulator_v0.1.pkl
            """,
            warn=True
        )
        
        print(result.stdout)
        print()
        
        # 4. 下载到本地
        print("4. 下载模拟器到本地...")
        conn.get(
            '/root/TwinEngines/simple_orderbook_simulator_v0.1.pkl',
            'simple_orderbook_simulator_v0.1.pkl'
        )
        print("[OK] 已下载到本地: simple_orderbook_simulator_v0.1.pkl")
        print()
        
        print("="*60)
        print("完成")
        print("="*60)
        print()
        print("[OK] v0.1模拟器训练完成")
        print()
        print("说明:")
        print("  - 使用了现有的~25条盘口数据")
        print("  - 基于统计方法（不依赖K线）")
        print("  - 可以用于流程验证和初步测试")
        print("  - 不建议用于最终决策")
        print()
        print("下一步:")
        print("  - 等待6小时，积累~300条数据")
        print("  - 重新训练v1.0（考虑K线特征）")
        print("  - v1.0可以正式使用")
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    train_on_vps()
