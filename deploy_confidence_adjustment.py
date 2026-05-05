#!/usr/bin/env python3
"""部署置信度阈值调整到VPS"""

from fabric import Connection
import time

def main():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    files_to_upload = [
        "src/twinengines/live/naked_pm_runner.py",
        "src/twinengines/cli.py",
    ]
    
    print("="*60)
    print("部署置信度阈值调整到VPS")
    print("="*60)
    print()
    
    print("修改内容:")
    print("  1. NAKED_DYNAMIC_ENTRY_TIERS 最低档: edge 0.06 → 0.08")
    print("  2. CLI --confidence-min 默认值: 0.51 → 0.501")
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        print("1. 上传修改后的文件...")
        for local_file in files_to_upload:
            remote_file = f"/root/TwinEngines/{local_file}"
            print(f"   上传: {local_file}")
            conn.put(local_file, remote_file)
            print(f"      [OK] 完成")
        print()
        
        print("2. 验证VPS上的修改...")
        result = conn.run(
            "cd /root/TwinEngines && python3 -c \"from src.twinengines.live.naked_pm_runner import NAKED_DYNAMIC_ENTRY_TIERS; print(NAKED_DYNAMIC_ENTRY_TIERS[-1])\"",
            hide=True,
            warn=True
        )
        print(f"   VPS上的最低档配置: {result.stdout.strip()}")
        print()
        
        print("3. 停止当前服务...")
        conn.run("systemctl stop twinengines-strategy", hide=True)
        print("   [OK] 服务已停止")
        print()
        
        print("4. 启动服务（应用新配置）...")
        conn.run("systemctl start twinengines-strategy", hide=True)
        print("   [OK] 服务已启动")
        print()
        
        print("5. 等待3秒...")
        time.sleep(3)
        
        print("6. 检查服务状态...")
        result = conn.run("systemctl is-active twinengines-strategy", hide=True, warn=True)
        status = result.stdout.strip()
        print(f"   服务状态: {status}")
        
        if status == "active":
            print("   [OK] 服务运行正常")
        else:
            print("   [警告] 服务状态异常，请检查日志")
        print()
        
        print("7. 查看最近的日志（最后20行）...")
        result = conn.run("tail -20 /root/TwinEngines/logs/twinengines.log", hide=True, warn=True)
        print("   最近日志:")
        for line in result.stdout.strip().split('\n')[-10:]:
            print(f"     {line[:120]}")
        print()
        
        print("="*60)
        print("部署完成！")
        print("="*60)
        print()
        print("新配置已生效:")
        print("  - 置信度阈值: 0.501")
        print("  - 最低档edge要求: 0.08")
        print()
        print("监控命令:")
        print(f"  ssh {vps_user}@{vps_host}")
        print("  tail -f /root/TwinEngines/logs/twinengines.log")
        print()
        print("查看实时信号:")
        print("  tail -f /root/TwinEngines/logs/naked_live_ticks.jsonl | jq .")
        print()
        
        conn.close()
        return 0
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    import sys
    sys.exit(main())
