#!/usr/bin/env python3
"""上传自动领取功能到VPS"""

import subprocess
import sys
from pathlib import Path

def upload_via_scp():
    """使用 scp 命令上传文件"""
    vps_host = "47.243.169.235"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    files_to_upload = [
        ("src/twinengines/live/naked_pm_runner.py", "/root/TwinEngines/src/twinengines/live/"),
        ("src/twinengines/io/polymarket_client.py", "/root/TwinEngines/src/twinengines/io/"),
        ("test_auto_redeem.py", "/root/TwinEngines/"),
    ]
    
    print("=== 上传自动领取功能到VPS ===\n")
    
    # 检查本地文件
    print("检查本地文件...")
    for local_file, _ in files_to_upload:
        if Path(local_file).exists():
            print(f"  ✓ {local_file}")
        else:
            print(f"  ✗ {local_file} (不存在)")
            return False
    
    print("\n开始上传...\n")
    
    # 使用 scp 上传
    for local_file, remote_dir in files_to_upload:
        remote_path = f"{remote_dir}{Path(local_file).name}"
        print(f"上传: {local_file} -> {remote_path}")
        
        # 使用 scp 命令（需要 sshpass 或手动输入密码）
        cmd = f'scp "{local_file}" {vps_user}@{vps_host}:{remote_path}'
        print(f"  命令: {cmd}")
        print(f"  密码: {vps_password}")
        print()
    
    print("\n" + "="*60)
    print("手动上传步骤:")
    print("="*60)
    print("\n1. 使用 WinSCP 或其他 SFTP 客户端")
    print(f"   主机: {vps_host}")
    print(f"   用户: {vps_user}")
    print(f"   密码: {vps_password}")
    print("\n2. 上传以下文件:")
    for local_file, remote_dir in files_to_upload:
        print(f"   {local_file} -> {remote_dir}")
    
    print("\n3. 在VPS上执行:")
    print("   ssh root@47.243.169.235")
    print("   cd /root/TwinEngines")
    print("   python3 test_auto_redeem.py")
    print("   systemctl restart twinengines-strategy")
    print("   tail -f logs/twinengines.log | grep -E 'redeem|领取'")
    
    return True

if __name__ == "__main__":
    upload_via_scp()
