#!/usr/bin/env python3
"""自动上传并部署自动领取功能到VPS"""

import os
import sys
from pathlib import Path

def main():
    print("="*60)
    print("TwinEngines - 自动领取功能部署到VPS")
    print("="*60)
    print()
    
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    files_to_upload = [
        "src/twinengines/live/naked_pm_runner.py",
        "src/twinengines/io/polymarket_client.py",
        "test_auto_redeem.py",
    ]
    
    # 检查本地文件
    print("1. 检查本地文件...")
    all_exist = True
    for f in files_to_upload:
        if Path(f).exists():
            size = Path(f).stat().st_size
            print(f"   [OK] {f} ({size} bytes)")
        else:
            print(f"   [X] {f} (不存在)")
            all_exist = False
    
    if not all_exist:
        print("\n错误: 某些文件不存在")
        return 1
    
    print("\n2. 尝试使用 fabric 库上传...")
    try:
        from fabric import Connection
        
        # 连接到VPS
        print(f"   连接到 {vps_host}...")
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 上传文件
        for local_file in files_to_upload:
            remote_file = f"/root/TwinEngines/{local_file}"
            print(f"   上传: {local_file}")
            conn.put(local_file, remote_file)
            print(f"      [OK] 完成")
        
        print("\n3. 在VPS上测试自动领取功能...")
        result = conn.run("cd /root/TwinEngines && python3 test_auto_redeem.py", hide=False)
        
        print("\n4. 重启服务...")
        conn.run("systemctl restart twinengines-strategy", hide=False)
        
        print("\n5. 查看服务状态...")
        conn.run("systemctl status twinengines-strategy --no-pager -l", hide=False)
        
        print("\n" + "="*60)
        print("部署完成！")
        print("="*60)
        print("\n查看日志:")
        print("  ssh root@47.243.169.235")
        print("  tail -f /root/TwinEngines/logs/twinengines.log | grep -E 'redeem|领取'")
        
        conn.close()
        return 0
        
    except ImportError:
        print("   fabric 库未安装")
        print("   安装: pip install fabric")
        print()
        print("="*60)
        print("手动部署步骤:")
        print("="*60)
        print()
        print("方法1: 使用 WinSCP GUI")
        print(f"  1. 打开 WinSCP，连接到 {vps_host}")
        print(f"     用户名: {vps_user}")
        print(f"     密码: {vps_password}")
        print("  2. 上传以下文件到 /root/TwinEngines/:")
        for f in files_to_upload:
            print(f"     - {f}")
        print()
        print("方法2: 使用 SSH 命令")
        print(f"  ssh {vps_user}@{vps_host}")
        print("  密码: " + vps_password)
        print("  cd /root/TwinEngines")
        print("  # 然后手动上传文件")
        print()
        print("上传后执行:")
        print("  cd /root/TwinEngines")
        print("  python3 test_auto_redeem.py")
        print("  systemctl restart twinengines-strategy")
        print("  tail -f logs/twinengines.log | grep -E 'redeem|领取'")
        
        return 1
    
    except Exception as e:
        print(f"\n错误: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())
