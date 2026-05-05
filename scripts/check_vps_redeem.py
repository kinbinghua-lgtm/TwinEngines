#!/usr/bin/env python3
"""检查VPS上的自动领取状态和日志"""

import subprocess
import sys

VPS_HOST = "47.243.169.235"
VPS_USER = "root"
VPS_PASSWORD = "Jinbh1977"

def run_ssh_command(cmd):
    """通过SSH执行命令"""
    ssh_cmd = [
        "sshpass", "-p", VPS_PASSWORD,
        "ssh", "-o", "StrictHostKeyChecking=no",
        f"{VPS_USER}@{VPS_HOST}",
        cmd
    ]
    try:
        result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=30)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "SSH timeout"
    except Exception as e:
        return -1, "", str(e)

def main():
    print("=== 检查VPS自动领取状态 ===\n")
    
    # 1. 检查进程状态
    print("1. 检查运行进程:")
    code, out, err = run_ssh_command("ps aux | grep -E 'python|twinengines' | grep -v grep")
    if code == 0:
        print(out)
    else:
        print(f"错误: {err}")
    
    # 2. 检查最近的日志
    print("\n2. 检查最近的日志 (最后50行):")
    code, out, err = run_ssh_command("tail -50 /root/TwinEngines/logs/twinengines.log")
    if code == 0:
        print(out)
    else:
        print(f"错误: {err}")
    
    # 3. 检查状态文件
    print("\n3. 检查状态文件:")
    code, out, err = run_ssh_command("cat /root/TwinEngines/data_runtime/naked_real_state.json 2>/dev/null || echo 'State file not found'")
    if code == 0:
        print(out)
    else:
        print(f"错误: {err}")
    
    # 4. 检查环境变量
    print("\n4. 检查自动领取配置:")
    code, out, err = run_ssh_command("cd /root/TwinEngines && grep -E 'AUTO_REDEEM|SIGNATURE_TYPE|BUILDER' .env | head -20")
    if code == 0:
        print(out)
    else:
        print(f"错误: {err}")
    
    # 5. 检查Python依赖
    print("\n5. 检查关键依赖:")
    code, out, err = run_ssh_command("cd /root/TwinEngines && python3 -c 'import py_builder_relayer_client; print(\"relayer_client OK\")' 2>&1")
    print(out if code == 0 else f"relayer_client: {err}")
    
    code, out, err = run_ssh_command("cd /root/TwinEngines && python3 -c 'import web3; print(\"web3 OK\")' 2>&1")
    print(out if code == 0 else f"web3: {err}")

if __name__ == "__main__":
    # 检查sshpass是否可用
    try:
        subprocess.run(["sshpass", "-V"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("错误: sshpass未安装")
        print("Windows上可以使用plink或其他SSH工具")
        sys.exit(1)
    
    main()
