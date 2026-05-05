#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""停止VPS上的主策略程序"""

import sys
import io
from fabric import Connection

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

def main():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("停止VPS主策略程序")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 检查当前运行的服务
        print("1. 当前运行的服务:")
        result = conn.run(
            "systemctl list-units --type=service --state=running | grep twinengines",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                print(f"   {line}")
        print()
        
        # 2. 停止主策略服务
        print("2. 停止主策略服务 (twinengines-strategy)...")
        result = conn.run(
            "systemctl stop twinengines-strategy",
            hide=True,
            warn=True
        )
        print("   [OK] 已发送停止命令")
        print()
        
        # 3. 等待2秒
        import time
        print("3. 等待服务停止...")
        time.sleep(2)
        print()
        
        # 4. 检查服务状态
        print("4. 检查服务状态:")
        result = conn.run(
            "systemctl is-active twinengines-strategy",
            hide=True,
            warn=True
        )
        status = result.stdout.strip()
        if status == "inactive":
            print(f"   ✓ 主策略服务已停止")
        else:
            print(f"   状态: {status}")
        print()
        
        # 5. 检查进程
        print("5. 检查相关进程:")
        result = conn.run(
            "ps aux | grep 'naked-third-digit-live' | grep -v grep",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            print("   警告: 仍有进程在运行:")
            for line in result.stdout.strip().split('\n'):
                parts = line.split()
                if len(parts) >= 2:
                    print(f"     PID: {parts[1]}")
        else:
            print("   ✓ 没有相关进程在运行")
        print()
        
        # 6. 查看所有TwinEngines服务状态
        print("6. 所有TwinEngines服务状态:")
        services = [
            "twinengines-strategy",
            "twinengines-collector", 
            "twinengines-webui"
        ]
        for service in services:
            result = conn.run(
                f"systemctl is-active {service} 2>/dev/null || echo 'not-found'",
                hide=True,
                warn=True
            )
            status = result.stdout.strip()
            if status == "active":
                print(f"   {service}: ✓ 运行中")
            elif status == "inactive":
                print(f"   {service}: ✗ 已停止")
            else:
                print(f"   {service}: {status}")
        print()
        
        conn.close()
        
        print("="*60)
        print("主策略程序已停止")
        print("="*60)
        print()
        print("说明:")
        print("  - 主策略服务 (twinengines-strategy) 已停止")
        print("  - 采集器 (twinengines-collector) 仍在运行")
        print("  - WebUI (twinengines-webui) 仍在运行")
        print()
        print("如需重新启动主策略:")
        print("  systemctl start twinengines-strategy")
        print()
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
