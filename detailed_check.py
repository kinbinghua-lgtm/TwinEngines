#!/usr/bin/env python3
"""详细检查服务运行状态"""

from fabric import Connection

def main():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("详细检查服务运行状态")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        print("1. 检查最近50行日志...")
        result = conn.run("cd /root/TwinEngines && tail -50 logs/twinengines.log", hide=True, warn=True)
        lines = result.stdout.strip().split('\n')
        
        print("   最近的日志（最后20行）:")
        for line in lines[-20:]:
            if line.strip():
                # 只显示前150个字符
                print(f"     {line[:150]}")
        print()
        
        print("2. 搜索包含 'phase' 的日志（查看运行阶段）...")
        result = conn.run("cd /root/TwinEngines && tail -100 logs/twinengines.log | grep 'phase' | tail -5", hide=True, warn=True)
        if result.stdout.strip():
            print("   最近的阶段日志:")
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    print(f"     {line[:150]}")
        else:
            print("     未找到 phase 日志")
        print()
        
        print("3. 检查是否有 JSON 格式的日志...")
        result = conn.run("cd /root/TwinEngines && tail -100 logs/twinengines.log | grep '{' | tail -3", hide=True, warn=True)
        if result.stdout.strip():
            print("   最近的JSON日志:")
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    print(f"     {line[:200]}")
        else:
            print("     未找到 JSON 日志")
        print()
        
        print("4. 检查 naked_pm_runner.py 文件是否存在...")
        result = conn.run("ls -lh /root/TwinEngines/src/twinengines/live/naked_pm_runner.py", hide=True, warn=True)
        print(f"     {result.stdout.strip()}")
        print()
        
        print("5. 检查文件更新时间...")
        result = conn.run("stat /root/TwinEngines/src/twinengines/live/naked_pm_runner.py | grep Modify", hide=True, warn=True)
        print(f"     {result.stdout.strip()}")
        print()
        
        print("6. 验证自动领取函数是否存在...")
        result = conn.run("cd /root/TwinEngines && grep -n '_run_auto_redeem_tick' src/twinengines/live/naked_pm_runner.py | head -1", hide=True, warn=True)
        if result.stdout.strip():
            print(f"     [OK] 找到函数: {result.stdout.strip()}")
        else:
            print("     [X] 未找到 _run_auto_redeem_tick 函数")
        print()
        
        print("7. 检查服务配置...")
        result = conn.run("cat /etc/systemd/system/twinengines-strategy.service | grep -E 'ExecStart|WorkingDirectory'", hide=True, warn=True)
        print("   服务配置:")
        for line in result.stdout.strip().split('\n'):
            if line.strip():
                print(f"     {line.strip()}")
        print()
        
        print("="*60)
        print("检查完成")
        print("="*60)
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
