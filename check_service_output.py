#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查看VPS服务输出"""

import sys
import io
from fabric import Connection

# 设置UTF-8输出
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

def main():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("检查VPS采集器服务")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 检查服务状态
        print("1. 服务状态:")
        result = conn.run("systemctl is-active twinengines-strategy", hide=True, warn=True)
        status = result.stdout.strip()
        print(f"   状态: {status}")
        
        if status == "active":
            result = conn.run("systemctl status twinengines-strategy | grep 'Active:'", hide=True, warn=True)
            print(f"   {result.stdout.strip()}")
        print()
        
        # 2. 检查进程
        print("2. 进程信息:")
        result = conn.run("ps aux | grep 'twinengines' | grep -v grep | head -3", hide=True, warn=True)
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                parts = line.split()
                if len(parts) >= 11:
                    print(f"   PID: {parts[1]}, CPU: {parts[2]}%, MEM: {parts[3]}%, 运行时间: {parts[9]}")
        else:
            print("   未找到进程")
        print()
        
        # 3. 查看最新日志
        print("3. 最新日志 (journalctl 最后30行):")
        result = conn.run(
            "journalctl -u twinengines-strategy -n 30 --no-pager --output=short-iso",
            hide=True,
            warn=True
        )
        lines = result.stdout.strip().split('\n')
        for line in lines[-30:]:
            if line.strip():
                print(f"   {line}")
        print()
        
        # 4. 查看日志文件
        print("4. 日志文件:")
        result = conn.run("ls -lh /root/TwinEngines/logs/*.log 2>/dev/null", hide=True, warn=True)
        if result.stdout.strip():
            print(result.stdout)
            
            # 查看最新的日志内容
            print("\n5. 最新日志内容 (tail -30):")
            result = conn.run(
                "tail -30 /root/TwinEngines/logs/twinengines.log 2>/dev/null",
                hide=True,
                warn=True
            )
            if result.stdout.strip():
                for line in result.stdout.strip().split('\n'):
                    print(f"   {line}")
            else:
                print("   日志文件为空或不存在")
        else:
            print("   未找到日志文件")
        print()
        
        # 6. 检查数据文件
        print("6. 数据文件:")
        result = conn.run(
            "ls -lh /root/TwinEngines/data_runtime/*.json 2>/dev/null | tail -5",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            print(result.stdout)
        else:
            print("   未找到数据文件")
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
