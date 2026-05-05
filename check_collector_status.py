#!/usr/bin/env python3
"""检查采集器服务状态"""

from fabric import Connection

def check_collector_status():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("检查采集器服务状态")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 检查服务状态
        print("1. 服务状态...")
        result = conn.run(
            "systemctl status twinengines-strategy",
            hide=True,
            warn=True
        )
        
        # 只显示关键行
        for line in result.stdout.split('\n')[:15]:
            if 'Active:' in line or 'Main PID:' in line or 'Memory:' in line:
                print(f"  {line.strip()}")
        print()
        
        # 2. 检查最新日志
        print("2. 最新日志（最后20行）...")
        result = conn.run(
            "journalctl -u twinengines-strategy -n 20 --no-pager",
            hide=True,
            warn=True
        )
        
        for line in result.stdout.split('\n')[-20:]:
            if line.strip():
                # 只显示关键信息
                if any(x in line for x in ['shadow', 'window', 'signal', 'ERROR', 'WARNING']):
                    print(f"  {line}")
        print()
        
        # 3. 检查进程
        print("3. 检查进程...")
        result = conn.run(
            "ps aux | grep twinengines | grep -v grep",
            hide=True,
            warn=True
        )
        
        if result.stdout.strip():
            print("  [OK] 进程正在运行")
        else:
            print("  [错误] 进程未运行")
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")

if __name__ == "__main__":
    check_collector_status()
