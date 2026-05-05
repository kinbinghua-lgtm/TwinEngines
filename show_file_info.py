#!/usr/bin/env python3
"""生成文件信息摘要，便于手动上传"""

from pathlib import Path
import hashlib

def file_hash(filepath):
    """计算文件的MD5哈希"""
    md5 = hashlib.md5()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b''):
            md5.update(chunk)
    return md5.hexdigest()

def main():
    files = [
        "src/twinengines/live/naked_pm_runner.py",
        "src/twinengines/io/polymarket_client.py",
        "test_auto_redeem.py",
    ]
    
    print("="*70)
    print("TwinEngines - 自动领取功能文件信息")
    print("="*70)
    print()
    
    for f in files:
        path = Path(f)
        if not path.exists():
            print(f"[X] {f} - 文件不存在")
            continue
        
        size = path.stat().st_size
        hash_val = file_hash(path)
        
        print(f"文件: {f}")
        print(f"  大小: {size:,} bytes ({size/1024:.2f} KB)")
        print(f"  MD5:  {hash_val}")
        print(f"  VPS路径: /root/TwinEngines/{f}")
        print()
    
    print("="*70)
    print("上传说明:")
    print("="*70)
    print()
    print("1. 使用 WinSCP 连接到 VPS:")
    print("   主机: 47.243.169.223")
    print("   用户: root")
    print("   密码: Jinbh1977")
    print()
    print("2. 将上述文件拖拽到对应的 VPS 路径")
    print()
    print("3. 上传后在 VPS 上执行:")
    print("   cd /root/TwinEngines")
    print("   python3 test_auto_redeem.py")
    print("   systemctl restart twinengines-strategy")
    print()
    print("详细部署指南请查看: AUTO_REDEEM_DEPLOYMENT.md")
    print()

if __name__ == "__main__":
    main()
