#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查采集器日志格式"""

import sys
import io
from fabric import Connection

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

def main():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("检查采集器日志格式")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 查看最近的日志格式
        print("1. 最近50行日志:")
        result = conn.run(
            "tail -50 /root/TwinEngines/logs/collector_stdout.log",
            hide=True,
            warn=True
        )
        
        lines = result.stdout.strip().split('\n')
        for i, line in enumerate(lines[-20:], 1):
            print(f"   {i:2d}: {line[:150]}")
        print()
        
        # 查找包含prediction的行
        print("2. 查找包含'prediction'的行:")
        result = conn.run(
            "grep -i 'prediction' /root/TwinEngines/logs/collector_stdout.log | head -5",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                print(f"   {line[:200]}")
        else:
            print("   未找到包含'prediction'的行")
        print()
        
        # 查找包含p_rev的行
        print("3. 查找包含'p_rev'的行:")
        result = conn.run(
            "grep 'p_rev' /root/TwinEngines/logs/collector_stdout.log | head -5",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                print(f"   {line[:200]}")
        else:
            print("   未找到包含'p_rev'的行")
        print()
        
        # 查找JSON格式的行
        print("4. 查找JSON格式的行:")
        result = conn.run(
            "grep '^{' /root/TwinEngines/logs/collector_stdout.log | head -5",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                print(f"   {line[:200]}")
        else:
            print("   未找到JSON格式的行")
        print()
        
        # 统计不同类型的日志
        print("5. 日志类型统计:")
        result = conn.run(
            "grep -o '\\[INFO\\]\\|\\[WARNING\\]\\|\\[ERROR\\]' /root/TwinEngines/logs/collector_stdout.log | sort | uniq -c",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            print(result.stdout)
        print()
        
        # 查看策略输出日志（可能包含预测信息）
        print("6. 检查策略输出日志:")
        result = conn.run(
            "tail -20 /root/TwinEngines/logs/strategy_stdout.log | grep -E 'prediction|p_rev|odds' | head -5",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                print(f"   {line[:200]}")
        else:
            print("   未找到相关信息")
        print()
        
        conn.close()
        
        print("="*60)
        print("检查完成")
        print("="*60)
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
