#!/usr/bin/env python3
"""查看完整日志找出问题"""

from fabric import Connection

def check_full_logs():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("查看完整日志找出问题")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 查看最近100行日志
        print("1. 查看最近100行日志...")
        result = conn.run(
            "journalctl -u twinengines-strategy -n 100 --no-pager",
            hide=True,
            warn=True
        )
        
        print(result.stdout)
        print()
        
        # 2. 查看错误日志
        print("2. 查看错误日志...")
        result = conn.run(
            "journalctl -u twinengines-strategy -p err -n 50 --no-pager",
            hide=True,
            warn=True
        )
        
        if result.stdout.strip():
            print(result.stdout)
        else:
            print("  没有错误日志")
        print()
        
        # 3. 检查程序启动命令
        print("3. 检查程序启动命令...")
        result = conn.run(
            "cat /etc/systemd/system/twinengines-strategy.service",
            hide=True,
            warn=True
        )
        
        print(result.stdout)
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    check_full_logs()
