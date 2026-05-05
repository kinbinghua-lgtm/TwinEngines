#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证采集器是否正确采集盘口数据"""

import sys
import io
import json
from fabric import Connection
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

def main():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("验证采集器数据采集情况")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 检查最新的盘口数据日志
        print("1. 检查实时盘口数据采集（最近30行包含price/book的日志）:")
        result = conn.run(
            "grep -E 'book|price|bid|ask|orderbook' /root/TwinEngines/logs/twinengines.log | tail -30",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n')[-15:]:
                print(f"   {line}")
        else:
            print("   未找到盘口相关日志")
        print()
        
        # 2. 查看WebSocket连接状态
        print("2. WebSocket连接状态（最近10条）:")
        result = conn.run(
            "grep -E 'ws_|WebSocket|websocket' /root/TwinEngines/logs/twinengines.log | tail -10",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                print(f"   {line}")
        print()
        
        # 3. 查看市场数据更新
        print("3. 市场数据更新（最近10条）:")
        result = conn.run(
            "grep -E 'market_resolver|market_changed|active market' /root/TwinEngines/logs/twinengines.log | tail -10",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                print(f"   {line}")
        print()
        
        # 4. 查看数据文件内容
        print("4. 查看实时状态数据文件:")
        result = conn.run(
            "cat /root/TwinEngines/data_runtime/naked_real_state.json 2>/dev/null",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            try:
                data = json.loads(result.stdout)
                print(f"   文件大小: {len(result.stdout)} bytes")
                print(f"   数据键: {list(data.keys())}")
                if 'last_update' in data:
                    print(f"   最后更新: {data['last_update']}")
                if 'market' in data:
                    print(f"   市场信息: {data['market']}")
                print(f"   完整数据预览:")
                print(f"   {json.dumps(data, indent=2, ensure_ascii=False)[:500]}")
            except:
                print(f"   原始内容: {result.stdout[:500]}")
        else:
            print("   文件不存在或为空")
        print()
        
        # 5. 查看collector输出日志
        print("5. 查看collector标准输出（最近20行）:")
        result = conn.run(
            "tail -20 /root/TwinEngines/logs/collector_stdout.log 2>/dev/null",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                print(f"   {line}")
        else:
            print("   文件为空")
        print()
        
        # 6. 查看最新的策略输出
        print("6. 策略输出（最近20行，查看是否有盘口数据）:")
        result = conn.run(
            "tail -20 /root/TwinEngines/logs/strategy_stdout.log 2>/dev/null",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                print(f"   {line}")
        else:
            print("   文件为空")
        print()
        
        # 7. 实时监控5秒
        print("7. 实时监控日志5秒（查看是否有新数据）:")
        print("   开始监控...")
        result = conn.run(
            "timeout 5 tail -f /root/TwinEngines/logs/twinengines.log 2>/dev/null || true",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            lines = result.stdout.strip().split('\n')
            print(f"   捕获到 {len(lines)} 行新日志:")
            for line in lines[-10:]:
                print(f"   {line}")
        else:
            print("   5秒内无新日志")
        print()
        
        conn.close()
        
        print("="*60)
        print("验证完成")
        print("="*60)
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
