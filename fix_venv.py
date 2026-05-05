#!/usr/bin/env python3
"""检查并修复虚拟环境"""

from fabric import Connection

def fix_venv():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("检查并修复虚拟环境")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 检查虚拟环境
        print("1. 检查虚拟环境...")
        result = conn.run(
            "/root/TwinEngines/.venv/bin/python -c 'import sys; print(sys.path)'",
            hide=True,
            warn=True
        )
        print(result.stdout)
        print()
        
        # 2. 检查是否安装了twinengines
        print("2. 检查twinengines模块...")
        result = conn.run(
            "/root/TwinEngines/.venv/bin/python -c 'import twinengines; print(twinengines.__file__)'",
            hide=True,
            warn=True
        )
        
        if result.ok:
            print(f"  [OK] 已安装: {result.stdout.strip()}")
        else:
            print("  [错误] 未安装")
            print()
            print("3. 安装twinengines...")
            result = conn.run(
                "cd /root/TwinEngines && /root/TwinEngines/.venv/bin/pip install -e .",
                hide=True,
                warn=True
            )
            if result.ok:
                print("  [OK] 安装成功")
            else:
                print("  [错误] 安装失败")
                print(result.stderr)
        print()
        
        # 4. 重新测试
        print("4. 重新测试命令...")
        result = conn.run(
            "cd /root/TwinEngines && /root/TwinEngines/.venv/bin/python -m twinengines.cli --help 2>&1 | head -10",
            hide=True,
            warn=True,
            timeout=10
        )
        
        if 'live-run' in result.stdout:
            print("  [OK] 命令可以运行")
        else:
            print("  [错误] 命令仍然失败")
            print(result.stdout)
        print()
        
        # 5. 重启采集器
        print("5. 重启采集器service...")
        conn.run("systemctl restart twinengines-collector", hide=True)
        print("  [OK] 已重启")
        print()
        
        # 6. 等待并检查状态
        print("6. 检查状态（等待5秒）...")
        import time
        time.sleep(5)
        
        result = conn.run(
            "systemctl status twinengines-collector",
            hide=True,
            warn=True
        )
        
        for line in result.stdout.split('\n')[:10]:
            if 'Active:' in line:
                print(f"  {line.strip()}")
                if 'running' in line:
                    print("  [OK] 采集器正在运行！")
                else:
                    print("  [错误] 采集器未运行")
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    fix_venv()
