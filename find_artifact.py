#!/usr/bin/env python3
"""查找正确的artifact文件"""

from fabric import Connection

def find_artifact():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("查找artifact文件")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 查找artifact文件
        print("1. 查找artifact文件...")
        result = conn.run(
            "find /root/TwinEngines -name '*.pkl' -o -name '*artifact*.json' | grep -E '(artifact|model)' | head -20",
            hide=True,
            warn=True
        )
        print(result.stdout)
        print()
        
        # 2. 查看data_runtime目录
        print("2. 查看data_runtime目录...")
        result = conn.run(
            "ls -lh /root/TwinEngines/data_runtime/",
            hide=True,
            warn=True
        )
        print(result.stdout)
        print()
        
        # 3. 查看reports目录
        print("3. 查看reports目录...")
        result = conn.run(
            "ls -lh /root/TwinEngines/reports/*.json | head -10",
            hide=True,
            warn=True
        )
        print(result.stdout)
        print()
        
        # 4. 查看实盘程序用的什么artifact
        print("4. 实盘程序使用的artifact...")
        result = conn.run(
            "grep 'model-json' /etc/systemd/system/twinengines-strategy.service",
            hide=True,
            warn=True
        )
        print(result.stdout)
        print()
        
        print("="*60)
        print("建议")
        print("="*60)
        print()
        print("需要修改采集器service，使用正确的artifact文件")
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")

if __name__ == "__main__":
    find_artifact()
