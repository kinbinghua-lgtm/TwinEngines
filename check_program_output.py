#!/usr/bin/env python3
"""查看程序实际输出和配置"""

from fabric import Connection

def check_program_output():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("查看程序实际输出")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 查看service配置
        print("1. Service配置...")
        result = conn.run(
            "cat /etc/systemd/system/twinengines-strategy.service",
            hide=True,
            warn=True
        )
        print(result.stdout)
        print()
        
        # 2. 查看程序日志文件
        print("2. 查看程序日志文件...")
        result = conn.run(
            "ls -lh /root/TwinEngines/logs/",
            hide=True,
            warn=True
        )
        print(result.stdout)
        print()
        
        # 3. 查看最新的程序日志
        print("3. 查看最新的程序日志...")
        result = conn.run(
            "tail -50 /root/TwinEngines/logs/*.log 2>/dev/null | head -100",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            print(result.stdout[:2000])  # 只显示前2000字符
        else:
            print("  没有日志文件")
        print()
        
        # 4. 检查程序是否在运行
        print("4. 检查程序进程...")
        result = conn.run(
            "ps aux | grep python | grep -v grep",
            hide=True,
            warn=True
        )
        print(result.stdout)
        print()
        
        # 5. 手动运行程序看输出
        print("5. 手动测试程序（5秒）...")
        result = conn.run(
            "cd /root/TwinEngines && timeout 5 python -m twinengines.cli live-run --dry-run-signals --artifact data_runtime/artifact_v1.pkl 2>&1 | head -50",
            hide=True,
            warn=True
        )
        print(result.stdout)
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")

if __name__ == "__main__":
    check_program_output()
