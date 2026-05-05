#!/usr/bin/env python3
"""对比本地和VPS上的文件差异"""

import paramiko
import difflib
from pathlib import Path

def compare_files():
    vps_host = "47.243.169.235"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    # 需要对比的文件
    files_to_compare = [
        "src/twinengines/live/naked_pm_runner.py",
        "src/twinengines/io/polymarket_client.py",
        "test_auto_redeem.py",
    ]
    
    print("=== 对比本地和VPS文件差异 ===\n")
    
    # 连接VPS
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        print(f"连接到 {vps_host}...")
        ssh.connect(vps_host, username=vps_user, password=vps_password, timeout=10)
        sftp = ssh.open_sftp()
        
        for local_file in files_to_compare:
            print(f"\n{'='*60}")
            print(f"文件: {local_file}")
            print('='*60)
            
            local_path = Path(local_file)
            vps_path = f"/root/TwinEngines/{local_file}"
            
            # 读取本地文件
            if not local_path.exists():
                print(f"  [本地] 文件不存在")
                continue
            
            with open(local_path, 'r', encoding='utf-8') as f:
                local_content = f.readlines()
            
            # 读取VPS文件
            try:
                with sftp.open(vps_path, 'r') as f:
                    vps_content = f.readlines()
            except FileNotFoundError:
                print(f"  [VPS] 文件不存在")
                print(f"  需要上传新文件")
                continue
            
            # 对比差异
            diff = list(difflib.unified_diff(
                vps_content,
                local_content,
                fromfile=f'VPS: {local_file}',
                tofile=f'Local: {local_file}',
                lineterm=''
            ))
            
            if not diff:
                print("  ✓ 文件内容相同")
            else:
                print(f"  ✗ 发现差异 ({len(diff)} 行)")
                print("\n差异内容（前50行）:")
                for line in diff[:50]:
                    print(line)
                if len(diff) > 50:
                    print(f"\n... 还有 {len(diff) - 50} 行差异")
        
        sftp.close()
        ssh.close()
        print("\n" + "="*60)
        print("对比完成")
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    compare_files()
