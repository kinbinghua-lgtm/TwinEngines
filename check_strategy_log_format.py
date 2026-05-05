#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查策略输出日志（主程序）"""

import sys
import io
from fabric import Connection

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

def main():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("检查策略输出日志（主程序）")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 查看策略输出日志的最后几行
        print("1. 策略输出日志最后30行:")
        result = conn.run(
            "tail -30 /root/TwinEngines/logs/strategy_stdout.log",
            hide=True,
            warn=True
        )
        
        lines = result.stdout.strip().split('\n')
        for i, line in enumerate(lines[-10:], 1):
            if line.strip():
                print(f"   {i:2d}: {line[:200]}")
        print()
        
        # 查找JSON格式的行
        print("2. 查找JSON格式的行（包含prediction）:")
        result = conn.run(
            "grep '\"prediction\"' /root/TwinEngines/logs/strategy_stdout.log | tail -3",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                print(f"   {line}")
                print()
        else:
            print("   未找到")
        print()
        
        # 查找包含odds的行
        print("3. 查找包含'odds'的行:")
        result = conn.run(
            "grep 'odds' /root/TwinEngines/logs/strategy_stdout.log | tail -3",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                print(f"   {line[:300]}")
        else:
            print("   未找到")
        print()
        
        # 统计JSON行数
        print("4. 统计JSON行数:")
        result = conn.run(
            "grep -c '^{' /root/TwinEngines/logs/strategy_stdout.log || echo 0",
            hide=True,
            warn=True
        )
        json_count = result.stdout.strip()
        print(f"   JSON行数: {json_count}")
        print()
        
        # 查看一个完整的JSON样本
        print("5. 查看一个完整的JSON样本:")
        result = conn.run(
            "grep '^{' /root/TwinEngines/logs/strategy_stdout.log | tail -1",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            import json
            try:
                data = json.loads(result.stdout.strip())
                print(f"   Keys: {list(data.keys())}")
                if 'prediction' in data:
                    print(f"   Prediction keys: {list(data['prediction'].keys())}")
                if 'quote_snapshot' in data:
                    print(f"   Quote keys: {list(data['quote_snapshot'].keys())}")
                print(f"\n   完整JSON:")
                print(json.dumps(data, indent=2, ensure_ascii=False)[:1000])
            except:
                print(f"   {result.stdout.strip()[:500]}")
        print()
        
        # 检查日志文件大小
        print("6. 日志文件信息:")
        result = conn.run(
            "ls -lh /root/TwinEngines/logs/strategy_stdout.log",
            hide=True,
            warn=True
        )
        print(f"   {result.stdout.strip()}")
        
        result = conn.run(
            "wc -l /root/TwinEngines/logs/strategy_stdout.log",
            hide=True,
            warn=True
        )
        print(f"   {result.stdout.strip()}")
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
