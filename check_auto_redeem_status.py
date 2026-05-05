#!/usr/bin/env python3
"""检查VPS上的自动领取功能状态"""

from fabric import Connection
import time

def main():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("检查VPS自动领取功能状态")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        print("1. 检查 .env 配置...")
        result = conn.run("cd /root/TwinEngines && grep -E 'AUTO_REDEEM|SIGNATURE_TYPE|BUILDER' .env | grep -v '^#' || echo '未找到相关配置'", hide=True, warn=True)
        print("   配置内容:")
        for line in result.stdout.strip().split('\n'):
            if line.strip():
                print(f"     {line}")
        print()
        
        print("2. 检查最近的自动领取日志...")
        result = conn.run("cd /root/TwinEngines && tail -200 logs/twinengines.log | grep -i 'auto_redeem' | tail -10 || echo '暂无自动领取日志'", hide=True, warn=True)
        print("   最近10条自动领取日志:")
        logs = result.stdout.strip().split('\n')
        if logs and logs[0] != '暂无自动领取日志':
            for line in logs:
                if line.strip():
                    print(f"     {line[:150]}")
        else:
            print("     暂无自动领取日志")
        print()
        
        print("3. 检查服务运行时间...")
        result = conn.run("systemctl show twinengines-strategy -p ActiveEnterTimestamp --no-pager", hide=True, warn=True)
        print(f"   {result.stdout.strip()}")
        print()
        
        print("4. 检查最近的错误日志...")
        result = conn.run("cd /root/TwinEngines && tail -100 logs/twinengines.log | grep -E 'ERROR|WARNING' | tail -5 || echo '无错误'", hide=True, warn=True)
        print("   最近5条错误/警告:")
        for line in result.stdout.strip().split('\n')[-5:]:
            if line.strip():
                print(f"     {line[:150]}")
        print()
        
        print("5. 检查进程状态...")
        result = conn.run("ps aux | grep -E 'twinengines|python3.*naked' | grep -v grep || echo '未找到进程'", hide=True, warn=True)
        print("   运行中的进程:")
        for line in result.stdout.strip().split('\n'):
            if line.strip() and '未找到进程' not in line:
                # 只显示关键信息
                parts = line.split()
                if len(parts) > 10:
                    print(f"     PID: {parts[1]}, CPU: {parts[2]}%, MEM: {parts[3]}%, CMD: {' '.join(parts[10:13])}")
        print()
        
        print("="*60)
        print("状态检查完成")
        print("="*60)
        print()
        print("建议操作:")
        print("  1. 如果看到 'auto_redeem' 日志，说明功能正在运行")
        print("  2. 如果没有看到，可能需要检查 .env 中的 AUTO_REDEEM_ENABLED=true")
        print("  3. 查看实时日志: ssh root@47.243.169.223")
        print("     tail -f /root/TwinEngines/logs/twinengines.log")
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
