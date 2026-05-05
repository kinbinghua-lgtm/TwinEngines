#!/usr/bin/env python3
"""检查实盘程序配置"""

from fabric import Connection

def check_live_program():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("检查实盘程序配置")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 查看实盘程序日志
        print("1. 实盘程序最新日志...")
        result = conn.run(
            "tail -30 /root/TwinEngines/logs/twinengines.log",
            hide=True,
            warn=True
        )
        
        # 只显示关键行
        for line in result.stdout.split('\n')[-30:]:
            if any(x in line for x in ['shadow', 'signal', 'window', 'ERROR', 'WARNING']):
                print(f"  {line}")
        print()
        
        # 2. 检查实盘程序是否有shadow signal功能
        print("2. 检查是否启用了shadow signal...")
        result = conn.run(
            "grep -i 'shadow\\|dry.*run' /etc/systemd/system/twinengines-strategy.service",
            hide=True,
            warn=True
        )
        
        if result.stdout.strip():
            print("  找到shadow相关配置:")
            print(result.stdout)
        else:
            print("  [问题] 没有启用shadow signal采集！")
        print()
        
        # 3. 查看完整的启动命令
        print("3. 完整启动命令...")
        result = conn.run(
            "grep ExecStart /etc/systemd/system/twinengines-strategy.service | head -1",
            hide=True,
            warn=True
        )
        cmd = result.stdout.strip()
        print(f"  {cmd}")
        print()
        
        if '--dry-run-signals' in cmd:
            print("  [OK] 已启用采集器模式")
        else:
            print("  [问题] 这是实盘交易模式，不是采集器！")
            print()
            print("  需要添加 --dry-run-signals 参数")
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")

if __name__ == "__main__":
    check_live_program()
