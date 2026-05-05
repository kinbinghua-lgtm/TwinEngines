#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""详细检查采集器采集的数据量和数据源"""

import sys
import io
import json
from fabric import Connection

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

def main():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("检查采集器数据采集详情")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 统计Polymarket盘口数据采集量
        print("1. Polymarket盘口数据采集统计:")
        result = conn.run(
            "grep -c 'PolymarketFeed.*book' /root/TwinEngines/logs/collector_stdout.log 2>/dev/null || echo 0",
            hide=True,
            warn=True
        )
        poly_book_count = result.stdout.strip()
        print(f"   盘口更新记录数: {poly_book_count}")
        
        # 查看最近的盘口数据
        result = conn.run(
            "grep 'book' /root/TwinEngines/logs/collector_stdout.log 2>/dev/null | tail -5",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            print(f"   最近5条盘口记录:")
            for line in result.stdout.strip().split('\n'):
                print(f"     {line[:150]}")
        print()
        
        # 2. 检查币安数据采集
        print("2. 币安(Binance)数据采集统计:")
        result = conn.run(
            "grep -i 'binance' /root/TwinEngines/logs/collector_stdout.log 2>/dev/null | head -10",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            print(f"   找到币安相关日志:")
            for line in result.stdout.strip().split('\n')[:10]:
                print(f"     {line[:150]}")
        else:
            print(f"   未找到币安相关日志")
        
        # 统计币安数据量
        result = conn.run(
            "grep -c -i 'binance' /root/TwinEngines/logs/collector_stdout.log 2>/dev/null || echo 0",
            hide=True,
            warn=True
        )
        binance_count = result.stdout.strip()
        print(f"   币安数据记录数: {binance_count}")
        print()
        
        # 3. 检查WebSocket连接
        print("3. WebSocket连接详情:")
        result = conn.run(
            "grep 'ws.*connect' /root/TwinEngines/logs/collector_stdout.log 2>/dev/null | tail -10",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                if 'binance' in line.lower():
                    print(f"   [币安] {line[:120]}")
                elif 'polymarket' in line.lower():
                    print(f"   [Poly] {line[:120]}")
                else:
                    print(f"   {line[:120]}")
        print()
        
        # 4. 统计市场切换次数
        print("4. 市场切换统计:")
        result = conn.run(
            "grep -c 'market_changed' /root/TwinEngines/logs/collector_stdout.log 2>/dev/null || echo 0",
            hide=True,
            warn=True
        )
        market_changes = result.stdout.strip()
        print(f"   市场切换次数: {market_changes}")
        
        # 查看最近的市场
        result = conn.run(
            "grep 'market_changed' /root/TwinEngines/logs/collector_stdout.log 2>/dev/null | tail -5",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            print(f"   最近5次市场切换:")
            for line in result.stdout.strip().split('\n'):
                print(f"     {line[:150]}")
        print()
        
        # 5. 检查采集器日志文件大小和行数
        print("5. 采集器日志文件统计:")
        result = conn.run(
            "wc -l /root/TwinEngines/logs/collector_stdout.log 2>/dev/null",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            print(f"   总行数: {result.stdout.strip()}")
        
        result = conn.run(
            "ls -lh /root/TwinEngines/logs/collector_stdout.log 2>/dev/null",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            print(f"   文件大小: {result.stdout.strip().split()[4]}")
        
        # 采集器运行时长
        result = conn.run(
            "ps -p 308909 -o etime= 2>/dev/null || echo '未运行'",
            hide=True,
            warn=True
        )
        print(f"   运行时长: {result.stdout.strip()}")
        print()
        
        # 6. 查看最近30秒的实时数据流
        print("6. 最近30秒数据流（查看数据源）:")
        result = conn.run(
            "tail -50 /root/TwinEngines/logs/collector_stdout.log 2>/dev/null",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            lines = result.stdout.strip().split('\n')
            binance_lines = [l for l in lines if 'binance' in l.lower() or 'btcusdt' in l.lower()]
            poly_lines = [l for l in lines if 'polymarket' in l.lower() or 'poly_feed' in l.lower()]
            clock_lines = [l for l in lines if 'clock_sync' in l.lower()]
            
            print(f"   最近50行中:")
            print(f"     - 币安相关: {len(binance_lines)} 条")
            print(f"     - Polymarket相关: {len(poly_lines)} 条")
            print(f"     - 时钟同步: {len(clock_lines)} 条")
            
            if binance_lines:
                print(f"\n   币安数据示例:")
                for line in binance_lines[-3:]:
                    print(f"     {line[:150]}")
            
            if poly_lines:
                print(f"\n   Polymarket数据示例:")
                for line in poly_lines[-3:]:
                    print(f"     {line[:150]}")
        print()
        
        # 7. 检查feed_ws相关的所有连接
        print("7. 数据源WebSocket连接汇总:")
        result = conn.run(
            "grep -E 'feed.*ws.*connect|ws.*open' /root/TwinEngines/logs/collector_stdout.log 2>/dev/null | tail -10",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                if 'binance' in line.lower():
                    print(f"   [币安WS] {line[:130]}")
                elif 'polymarket' in line.lower() or 'clob' in line.lower():
                    print(f"   [Poly WS] {line[:130]}")
                else:
                    print(f"   [其他] {line[:130]}")
        print()
        
        # 8. 实时监控10秒，看数据流
        print("8. 实时监控10秒（观察数据流）:")
        print("   开始监控...")
        result = conn.run(
            "timeout 10 tail -f /root/TwinEngines/logs/collector_stdout.log 2>/dev/null || true",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            lines = result.stdout.strip().split('\n')
            print(f"   10秒内捕获 {len(lines)} 行新数据:")
            for line in lines[-15:]:
                print(f"     {line[:150]}")
        else:
            print("   10秒内无新数据")
        print()
        
        conn.close()
        
        print("="*60)
        print("数据采集详情检查完成")
        print("="*60)
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
