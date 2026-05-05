#!/usr/bin/env python3
"""检查自动领取函数是否被调用"""

from fabric import Connection

def main():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("检查自动领取函数调用情况")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        print("1. 查找 _run_auto_redeem_tick 函数定义...")
        result = conn.run("cd /root/TwinEngines && grep -n 'def _run_auto_redeem_tick' src/twinengines/live/naked_pm_runner.py", hide=True, warn=True)
        print(f"   {result.stdout.strip()}")
        print()
        
        print("2. 查找函数调用位置...")
        result = conn.run("cd /root/TwinEngines && grep -n '_run_auto_redeem_tick(' src/twinengines/live/naked_pm_runner.py | grep -v 'def _run'", hide=True, warn=True)
        if result.stdout.strip():
            print("   找到调用:")
            for line in result.stdout.strip().split('\n'):
                print(f"     {line}")
        else:
            print("   [警告] 未找到函数调用！")
            print("   这意味着函数虽然存在，但没有被执行")
        print()
        
        print("3. 检查主循环中是否有 auto_redeem 相关代码...")
        result = conn.run("cd /root/TwinEngines && grep -n -A2 -B2 'auto_redeem' src/twinengines/live/naked_pm_runner.py | head -30", hide=True, warn=True)
        if result.stdout.strip():
            print("   找到相关代码:")
            for line in result.stdout.strip().split('\n')[:20]:
                print(f"     {line}")
        print()
        
        print("4. 查看 naked-third-digit-live 命令的主函数...")
        result = conn.run("cd /root/TwinEngines && grep -n 'def.*naked.*third.*digit.*live' src/twinengines/cli.py", hide=True, warn=True)
        if result.stdout.strip():
            print("   找到主函数:")
            print(f"     {result.stdout.strip()}")
        print()
        
        print("5. 检查是否需要在主循环中添加调用...")
        result = conn.run("cd /root/TwinEngines && grep -n 'while.*True\\|for.*in.*range' src/twinengines/live/naked_pm_runner.py | head -5", hide=True, warn=True)
        if result.stdout.strip():
            print("   找到循环:")
            for line in result.stdout.strip().split('\n'):
                print(f"     {line}")
        print()
        
        print("="*60)
        print("分析结果")
        print("="*60)
        print()
        print("如果未找到函数调用，需要在主循环中添加:")
        print("  result = _run_auto_redeem_tick(")
        print("      poly=poly,")
        print("      state_path=state_path,")
        print("      runtime_cfg=runtime_cfg,")
        print("  )")
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
