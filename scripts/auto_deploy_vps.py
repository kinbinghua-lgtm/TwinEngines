#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动上传文件到VPS并部署"""

import subprocess
import sys
from pathlib import Path

VPS_HOST = "47.243.169.235"
VPS_USER = "root"
VPS_PASSWORD = "Jinbh1977"

def run_command(cmd, description):
    """运行命令并显示结果"""
    print(f"\n>>> {description}")
    print(f"命令: {cmd}")
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            print(f"[成功] {result.stdout}")
            return True
        else:
            print(f"[失败] {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        print("[超时] 命令执行超时")
        return False
    except Exception as e:
        print(f"[错误] {e}")
        return False

def upload_files():
    """上传文件到VPS"""
    files = [
        ("src/twinengines/live/naked_pm_runner.py", "/root/TwinEngines/src/twinengines/live/naked_pm_runner.py"),
        ("src/twinengines/io/polymarket_client.py", "/root/TwinEngines/src/twinengines/io/polymarket_client.py"),
        ("test_auto_redeem.py", "/root/TwinEngines/test_auto_redeem.py"),
    ]
    
    print("=" * 60)
    print("开始上传文件到VPS")
    print("=" * 60)
    
    for local_file, remote_file in files:
        if not Path(local_file).exists():
            print(f"[错误] 本地文件不存在: {local_file}")
            continue
        
        # 使用 scp 命令（需要 sshpass 或手动输入密码）
        cmd = f'scp "{local_file}" {VPS_USER}@{VPS_HOST}:{remote_file}'
        print(f"\n上传: {local_file} -> {remote_file}")
        print(f"请手动执行: {cmd}")
        print(f"或使用 WinSCP 图形界面上传")

def install_dependencies():
    """在VPS上安装依赖"""
    commands = [
        ("cd /root/TwinEngines && pip3 install web3", "安装 web3"),
        ("cd /root/TwinEngines && pip3 install 'git+https://github.com/Polymarket/py-builder-relayer-client.git'", "安装 relayer client"),
    ]
    
    print("\n" + "=" * 60)
    print("在VPS上安装依赖")
    print("=" * 60)
    
    for cmd, desc in commands:
        ssh_cmd = f'ssh {VPS_USER}@{VPS_HOST} "{cmd}"'
        print(f"\n{desc}")
        print(f"请手动执行: {ssh_cmd}")

def test_and_restart():
    """测试并重启服务"""
    commands = [
        ("cd /root/TwinEngines && python3 test_auto_redeem.py", "测试自动领取功能"),
        ("systemctl restart twinengines-strategy", "重启服务"),
        ("systemctl status twinengines-strategy", "查看服务状态"),
    ]
    
    print("\n" + "=" * 60)
    print("测试并重启服务")
    print("=" * 60)
    
    for cmd, desc in commands:
        ssh_cmd = f'ssh {VPS_USER}@{VPS_HOST} "{cmd}"'
        print(f"\n{desc}")
        print(f"请手动执行: {ssh_cmd}")

def main():
    print("""
╔════════════════════════════════════════════════════════════════╗
║              VPS 自动部署脚本                                  ║
╚════════════════════════════════════════════════════════════════╝

由于 Windows 上 SSH/SCP 命令行工具限制，建议使用以下方法之一：

方法1: 使用 WinSCP (推荐)
  1. 下载 WinSCP: https://winscp.net
  2. 连接到 47.243.169.235 (用户: root, 密码: Jinbh1977)
  3. 上传以下文件:
     - src/twinengines/live/naked_pm_runner.py -> /root/TwinEngines/src/twinengines/live/
     - src/twinengines/io/polymarket_client.py -> /root/TwinEngines/src/twinengines/io/
     - test_auto_redeem.py -> /root/TwinEngines/

方法2: 使用 Git (如果VPS上有仓库)
  1. 本地提交: git add . && git commit -m "Fix auto redeem" && git push
  2. VPS拉取: ssh root@47.243.169.235 "cd /root/TwinEngines && git pull"

方法3: 使用本脚本生成的命令
""")
    
    upload_files()
    install_dependencies()
    test_and_restart()
    
    print("\n" + "=" * 60)
    print("部署完成后，查看日志:")
    print("=" * 60)
    print(f'ssh {VPS_USER}@{VPS_HOST} "tail -f /root/TwinEngines/logs/twinengines.log | grep auto_redeem"')

if __name__ == "__main__":
    main()
