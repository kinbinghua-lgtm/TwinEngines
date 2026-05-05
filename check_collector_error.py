#!/usr/bin/env python3
"""检查采集器错误"""

from fabric import Connection

def check_collector_error():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("检查采集器错误")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 查看错误日志
        print("1. 错误日志...")
        result = conn.run(
            "tail -50 /root/TwinEngines/logs/collector_stderr.log",
            hide=True,
            warn=True
        )
        print(result.stdout)
        print()
        
        # 2. 查看systemd日志
        print("2. Systemd日志...")
        result = conn.run(
            "journalctl -u twinengines-collector -n 30 --no-pager",
            hide=True,
            warn=True
        )
        print(result.stdout)
        print()
        
        # 3. 手动测试命令
        print("3. 手动测试命令...")
        result = conn.run(
            "cd /root/TwinEngines && /root/TwinEngines/.venv/bin/python -m twinengines.cli live-run --dry-run-signals --artifact data_runtime/artifact_v1.pkl 2>&1 | head -20",
            hide=True,
            warn=True,
            timeout=15
        )
        print(result.stdout)
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")

if __name__ == "__main__":
    check_collector_error()
