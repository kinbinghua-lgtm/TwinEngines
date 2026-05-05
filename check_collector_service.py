#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查采集器服务状态"""

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
    print("检查采集器服务 (twinengines-collector)")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 检查采集器服务是否存在
        print("1. 检查采集器服务是否存在:")
        result = conn.run(
            "systemctl list-unit-files | grep collector",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            print(f"   {result.stdout.strip()}")
        else:
            print("   未找到collector服务")
        print()
        
        # 2. 检查服务状态
        print("2. 采集器服务状态:")
        result = conn.run(
            "systemctl is-active twinengines-collector 2>/dev/null || echo 'not-found'",
            hide=True,
            warn=True
        )
        status = result.stdout.strip()
        print(f"   状态: {status}")
        
        if status != "not-found":
            result = conn.run(
                "systemctl status twinengines-collector --no-pager | head -20",
                hide=True,
                warn=True
            )
            for line in result.stdout.split('\n')[:15]:
                if line.strip():
                    print(f"   {line}")
        print()
        
        # 3. 检查采集器进程
        print("3. 采集器进程:")
        result = conn.run(
            "ps aux | grep 'dry-run-signals' | grep -v grep",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                parts = line.split()
                if len(parts) >= 11:
                    print(f"   PID: {parts[1]}, CPU: {parts[2]}%, MEM: {parts[3]}%, 运行时间: {parts[9]}")
                    print(f"   命令: {' '.join(parts[10:])[:100]}")
        else:
            print("   未找到采集器进程")
        print()
        
        # 4. 查看采集器日志
        print("4. 采集器输出日志（最近30行）:")
        result = conn.run(
            "tail -30 /root/TwinEngines/logs/collector_stdout.log 2>/dev/null",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            lines = result.stdout.strip().split('\n')
            for line in lines[-30:]:
                print(f"   {line}")
        else:
            print("   日志文件为空或不存在")
        print()
        
        # 5. 查看采集器错误日志
        print("5. 采集器错误日志:")
        result = conn.run(
            "tail -20 /root/TwinEngines/logs/collector_stderr.log 2>/dev/null",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                print(f"   {line}")
        else:
            print("   无错误日志")
        print()
        
        # 6. 检查所有Python进程
        print("6. 所有TwinEngines相关进程:")
        result = conn.run(
            "ps aux | grep -E 'twinengines|TwinEngines' | grep python | grep -v grep",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            for i, line in enumerate(result.stdout.strip().split('\n'), 1):
                parts = line.split()
                if len(parts) >= 11:
                    print(f"   进程{i}: PID={parts[1]}, CPU={parts[2]}%, MEM={parts[3]}%")
                    cmd = ' '.join(parts[10:])
                    if 'dry-run-signals' in cmd:
                        print(f"          类型: 采集器 (shadow signals)")
                    elif 'live-run' in cmd:
                        print(f"          类型: 实盘策略")
                    print(f"          命令: {cmd[:120]}")
        print()
        
        # 7. 实时监控采集器输出5秒
        print("7. 实时监控采集器输出5秒:")
        result = conn.run(
            "timeout 5 tail -f /root/TwinEngines/logs/collector_stdout.log 2>/dev/null || true",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            lines = result.stdout.strip().split('\n')
            print(f"   捕获到 {len(lines)} 行新输出:")
            for line in lines[-10:]:
                print(f"   {line}")
        else:
            print("   5秒内无新输出")
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
