#!/usr/bin/env python3
"""直接查看关键信息"""

from fabric import Connection

def check_key_info():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("查看关键信息")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 查看service配置（只看ExecStart）
        print("1. Service启动命令...")
        result = conn.run(
            "grep ExecStart /etc/systemd/system/twinengines-strategy.service",
            hide=True,
            warn=True
        )
        print(result.stdout)
        print()
        
        # 2. 查看日志文件列表
        print("2. 日志文件...")
        result = conn.run(
            "ls -lh /root/TwinEngines/logs/*.log 2>/dev/null || echo 'No log files'",
            hide=True,
            warn=True
        )
        print(result.stdout)
        print()
        
        # 3. 查看shadow_signals.jsonl
        print("3. shadow_signals.jsonl...")
        result = conn.run(
            "ls -lh /root/TwinEngines/logs/shadow_signals.jsonl",
            hide=True,
            warn=True
        )
        print(result.stdout)
        print()
        
        # 4. 检查程序是否真的在运行
        print("4. 程序进程...")
        result = conn.run(
            "ps aux | grep 'twinengines' | grep -v grep | wc -l",
            hide=True,
            warn=True
        )
        count = int(result.stdout.strip())
        print(f"  运行中的进程数: {count}")
        print()
        
        # 5. 手动运行看是否报错
        print("5. 手动运行测试（10秒）...")
        result = conn.run(
            "cd /root/TwinEngines && timeout 10 python -m twinengines.cli live-run --dry-run-signals --artifact data_runtime/artifact_v1.pkl 2>&1",
            hide=True,
            warn=True
        )
        
        # 只显示前1000字符
        output = result.stdout[:1000] if result.stdout else "No output"
        print(output)
        print()
        
        if "error" in output.lower() or "exception" in output.lower():
            print("[错误] 程序运行有问题！")
        else:
            print("[OK] 程序可以运行")
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")

if __name__ == "__main__":
    check_key_info()
