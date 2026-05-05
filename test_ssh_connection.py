#!/usr/bin/env python3
"""测试SSH连接"""
import paramiko
import sys

VPS_HOST = "47.243.169.235"
VPS_USER = "root"
VPS_PASS = "Jinbh1977"

print(f"尝试连接到 {VPS_USER}@{VPS_HOST}...")
print(f"密码长度: {len(VPS_PASS)}")

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

try:
    client.connect(
        VPS_HOST,
        username=VPS_USER,
        password=VPS_PASS,
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
        banner_timeout=30
    )
    print("✓ SSH连接成功！")
    
    # 测试执行命令
    stdin, stdout, stderr = client.exec_command("whoami")
    result = stdout.read().decode().strip()
    print(f"✓ 当前用户: {result}")
    
    stdin, stdout, stderr = client.exec_command("pwd")
    result = stdout.read().decode().strip()
    print(f"✓ 当前目录: {result}")
    
    stdin, stdout, stderr = client.exec_command("ls -la /root/TwinEngines")
    result = stdout.read().decode().strip()
    print(f"✓ TwinEngines目录内容:\n{result[:500]}")
    
    client.close()
    print("\n连接测试成功！")
    sys.exit(0)
    
except paramiko.AuthenticationException as e:
    print(f"✗ 认证失败: {e}")
    print("\n可能的原因:")
    print("1. 密码不正确")
    print("2. SSH服务器不允许密码认证")
    print("3. 需要使用密钥认证")
    sys.exit(1)
    
except paramiko.SSHException as e:
    print(f"✗ SSH错误: {e}")
    sys.exit(1)
    
except Exception as e:
    print(f"✗ 连接失败: {e}")
    sys.exit(1)
