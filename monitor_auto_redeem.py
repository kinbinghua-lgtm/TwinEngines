#!/usr/bin/env python3
"""等待并监控自动领取功能"""

from fabric import Connection
import time

def main():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("监控VPS自动领取功能（等待60秒）")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        print("等待60秒，让自动领取功能运行...")
        for i in range(60, 0, -10):
            print(f"  剩余 {i} 秒...")
            time.sleep(10)
        
        print("\n检查自动领取日志...\n")
        
        result = conn.run(
            "cd /root/TwinEngines && tail -300 logs/twinengines.log | grep -E 'auto_redeem|redeem_capability|redeem_attempted' | tail -20 || echo '暂无自动领取日志'", 
            hide=True, 
            warn=True
        )
        
        logs = result.stdout.strip().split('\n')
        
        if logs and logs[0] != '暂无自动领取日志':
            print("找到自动领取相关日志:")
            print("-" * 60)
            for line in logs:
                if line.strip():
                    print(line)
            print("-" * 60)
        else:
            print("⚠️ 暂无自动领取日志")
            print()
            print("可能的原因:")
            print("  1. 服务刚启动，还未到第一次检查时间（30秒间隔）")
            print("  2. 没有可领取的持仓")
            print("  3. 功能未正确启动")
            print()
            print("检查最近的所有日志（最后20行）:")
            result2 = conn.run("cd /root/TwinEngines && tail -20 logs/twinengines.log", hide=True, warn=True)
            for line in result2.stdout.strip().split('\n')[-20:]:
                if line.strip():
                    print(f"  {line[:120]}")
        
        print()
        print("="*60)
        print("监控完成")
        print("="*60)
        print()
        print("继续监控:")
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
