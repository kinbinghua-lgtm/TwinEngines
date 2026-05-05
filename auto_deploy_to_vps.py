#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""使用paramiko自动上传文件到VPS并部署"""

import paramiko
import sys
from pathlib import Path
import time

VPS_HOST = "47.243.169.235"
VPS_USER = "root"
VPS_PASSWORD = "Jinbh1977"
VPS_PORT = 22

def create_ssh_client():
    """创建SSH客户端"""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        print(f"正在连接到 {VPS_HOST}...")
        client.connect(
            VPS_HOST, 
            port=VPS_PORT, 
            username=VPS_USER, 
            password=VPS_PASSWORD, 
            timeout=30,
            look_for_keys=False,
            allow_agent=False
        )
        print("[成功] SSH连接成功")
        return client
    except paramiko.AuthenticationException as e:
        print(f"[失败] SSH认证失败: {e}")
        print("请检查用户名和密码是否正确")
        return None
    except paramiko.SSHException as e:
        print(f"[失败] SSH连接错误: {e}")
        return None
    except Exception as e:
        print(f"[失败] SSH连接失败: {e}")
        return None

def upload_file(sftp, local_path, remote_path):
    """上传单个文件"""
    try:
        print(f"  上传: {local_path} -> {remote_path}")
        
        # 确保远程目录存在
        remote_dir = str(Path(remote_path).parent)
        try:
            sftp.stat(remote_dir)
        except FileNotFoundError:
            print(f"  创建远程目录: {remote_dir}")
            sftp.mkdir(remote_dir)
        
        sftp.put(local_path, remote_path)
        print(f"  [成功] 上传完成")
        return True
    except Exception as e:
        print(f"  [失败] 上传失败: {e}")
        return False

def run_remote_command(client, command, description):
    """在远程服务器上执行命令"""
    try:
        print(f"\n>>> {description}")
        print(f"命令: {command}")
        stdin, stdout, stderr = client.exec_command(command, timeout=60)
        
        # 读取输出
        output = stdout.read().decode('utf-8')
        error = stderr.read().decode('utf-8')
        exit_code = stdout.channel.recv_exit_status()
        
        if output:
            print(f"输出:\n{output}")
        if error:
            print(f"错误:\n{error}")
        
        if exit_code == 0:
            print(f"[成功] 命令执行成功")
            return True
        else:
            print(f"[失败] 命令执行失败 (退出码: {exit_code})")
            return False
    except Exception as e:
        print(f"[错误] 执行命令时出错: {e}")
        return False

def main():
    print("""
================================================================
          VPS 自动部署脚本 (使用 paramiko)
================================================================
""")
    
    # 检查本地文件
    files_to_upload = [
        ("src/twinengines/live/naked_pm_runner.py", "/root/TwinEngines/src/twinengines/live/naked_pm_runner.py"),
        ("src/twinengines/io/polymarket_client.py", "/root/TwinEngines/src/twinengines/io/polymarket_client.py"),
        ("test_auto_redeem.py", "/root/TwinEngines/test_auto_redeem.py"),
    ]
    
    print("检查本地文件...")
    for local_file, _ in files_to_upload:
        if Path(local_file).exists():
            print(f"  [OK] {local_file}")
        else:
            print(f"  [X] {local_file} 不存在")
            return 1
    
    # 创建SSH连接
    client = create_ssh_client()
    if not client:
        return 1
    
    try:
        # 创建SFTP客户端
        sftp = client.open_sftp()
        
        # 上传文件
        print("\n" + "=" * 60)
        print("步骤1: 上传文件到VPS")
        print("=" * 60)
        
        upload_success = True
        for local_file, remote_file in files_to_upload:
            if not upload_file(sftp, local_file, remote_file):
                upload_success = False
        
        sftp.close()
        
        if not upload_success:
            print("\n[警告] 部分文件上传失败")
        
        # 安装依赖
        print("\n" + "=" * 60)
        print("步骤2: 安装依赖")
        print("=" * 60)
        
        run_remote_command(
            client,
            "cd /root/TwinEngines && pip3 install web3",
            "安装 web3"
        )
        
        run_remote_command(
            client,
            "cd /root/TwinEngines && pip3 install 'git+https://github.com/Polymarket/py-builder-relayer-client.git'",
            "安装 py-builder-relayer-client"
        )
        
        # 测试自动领取功能
        print("\n" + "=" * 60)
        print("步骤3: 测试自动领取功能")
        print("=" * 60)
        
        run_remote_command(
            client,
            "cd /root/TwinEngines && python3 test_auto_redeem.py",
            "运行测试脚本"
        )
        
        # 重启服务
        print("\n" + "=" * 60)
        print("步骤4: 重启服务")
        print("=" * 60)
        
        response = input("\n测试通过了吗？是否重启服务？(y/n): ")
        if response.lower() == 'y':
            run_remote_command(
                client,
                "systemctl stop twinengines-strategy",
                "停止服务"
            )
            
            time.sleep(2)
            
            run_remote_command(
                client,
                "systemctl start twinengines-strategy",
                "启动服务"
            )
            
            time.sleep(2)
            
            run_remote_command(
                client,
                "systemctl status twinengines-strategy",
                "查看服务状态"
            )
            
            # 查看日志
            print("\n" + "=" * 60)
            print("步骤5: 查看最近的日志")
            print("=" * 60)
            
            run_remote_command(
                client,
                "tail -50 /root/TwinEngines/logs/twinengines.log | grep -E 'auto_redeem|redeem' || tail -50 /root/TwinEngines/logs/twinengines.log",
                "查看自动领取相关日志"
            )
        else:
            print("\n跳过重启服务")
        
        print("\n" + "=" * 60)
        print("部署完成！")
        print("=" * 60)
        print("\n监控日志命令:")
        print(f"ssh {VPS_USER}@{VPS_HOST}")
        print("tail -f /root/TwinEngines/logs/twinengines.log | grep auto_redeem")
        
    except Exception as e:
        print(f"\n[错误] 部署过程中出错: {e}")
        return 1
    finally:
        client.close()
        print("\nSSH连接已关闭")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
