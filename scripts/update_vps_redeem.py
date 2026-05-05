#!/usr/bin/env python3
"""更新VPS上的自动领取功能"""

import os
import sys
from pathlib import Path

def main():
    print("=== 更新VPS自动领取功能 ===\n")
    
    vps_host = "47.243.169.235"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    # 需要上传的文件
    files_to_upload = [
        "src/twinengines/live/naked_pm_runner.py",
        "src/twinengines/io/polymarket_client.py",
        "test_auto_redeem.py",
    ]
    
    print("准备上传以下文件到VPS:")
    for f in files_to_upload:
        print(f"  - {f}")
    
    print("\n使用 SCP 上传文件...")
    print("注意: Windows 上需要安装 pscp (PuTTY 套件) 或使用 WinSCP\n")
    
    # 生成上传命令
    print("手动执行以下命令:\n")
    
    for local_file in files_to_upload:
        remote_file = f"/root/TwinEngines/{local_file}"
        print(f"# 上传 {local_file}")
        print(f'pscp -pw {vps_password} "{local_file}" {vps_user}@{vps_host}:{remote_file}')
        print()
    
    print("\n上传完成后，在VPS上执行:")
    print(f"ssh {vps_user}@{vps_host}")
    print("cd /root/TwinEngines")
    print()
    print("# 1. 测试自动领取功能")
    print("python3 test_auto_redeem.py")
    print()
    print("# 2. 检查依赖")
    print("pip3 install web3")
    print("pip3 install 'git+https://github.com/Polymarket/py-builder-relayer-client.git'")
    print()
    print("# 3. 重启服务")
    print("systemctl restart twinengines-strategy")
    print()
    print("# 4. 查看日志")
    print("tail -f logs/twinengines.log | grep -E 'redeem|领取'")
    
    print("\n" + "="*60)
    print("提示: 如果使用 WinSCP 图形界面，直接拖拽文件即可")


if __name__ == "__main__":
    main()
