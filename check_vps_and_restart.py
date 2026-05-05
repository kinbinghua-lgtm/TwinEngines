#!/usr/bin/env python3
"""检查VPS状态并重启服务"""

from fabric import Connection

def main():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("连接到VPS并检查状态")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        print("1. 检查Python版本...")
        result = conn.run("python3 --version", hide=True)
        print(f"   Python版本: {result.stdout.strip()}")
        print()
        
        print("2. 检查当前服务状态...")
        result = conn.run("systemctl is-active twinengines-strategy", hide=True, warn=True)
        print(f"   服务状态: {result.stdout.strip()}")
        print()
        
        print("3. 重启服务...")
        conn.run("systemctl restart twinengines-strategy", hide=True)
        print("   [OK] 服务已重启")
        print()
        
        print("4. 等待3秒...")
        import time
        time.sleep(3)
        
        print("5. 检查服务状态...")
        result = conn.run("systemctl is-active twinengines-strategy", hide=True, warn=True)
        print(f"   服务状态: {result.stdout.strip()}")
        print()
        
        print("6. 查看最近的日志（检查自动领取）...")
        result = conn.run("tail -50 /root/TwinEngines/logs/twinengines.log | grep -E 'auto_redeem|redeem|ERROR' || echo '暂无相关日志'", hide=True, warn=True)
        print("   最近日志:")
        for line in result.stdout.strip().split('\n')[-10:]:
            print(f"     {line}")
        print()
        
        print("="*60)
        print("部署完成！")
        print("="*60)
        print()
        print("查看自动领取日志:")
        print(f"  ssh {vps_user}@{vps_host}")
        print("  tail -f /root/TwinEngines/logs/twinengines.log | grep -E 'redeem|领取'")
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
