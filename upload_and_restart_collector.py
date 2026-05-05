#!/usr/bin/env python3
"""上传修改后的代码并重启采集器"""

from fabric import Connection

def upload_and_restart():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("上传修改后的代码并重启采集器")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 上传修改后的文件
        print("1. 上传shadow_signal_engine.py...")
        conn.put(
            'src/twinengines/io/shadow_signal_engine.py',
            '/root/TwinEngines/src/twinengines/io/shadow_signal_engine.py'
        )
        print("  [OK] 已上传")
        print()
        
        # 2. 重启服务
        print("2. 重启采集器服务...")
        result = conn.run(
            "systemctl restart twinengines-strategy",
            hide=True,
            warn=True
        )
        
        if result.ok:
            print("  [OK] 服务已重启")
        else:
            print("  [警告] 重启可能失败")
        print()
        
        # 3. 检查状态
        print("3. 检查服务状态...")
        result = conn.run(
            "systemctl status twinengines-strategy | head -20",
            hide=True,
            warn=True
        )
        
        for line in result.stdout.split('\n')[:10]:
            if line.strip():
                print(f"  {line}")
        print()
        
        print("="*60)
        print("完成")
        print("="*60)
        print()
        print("[OK] 采集器已更新并重启")
        print()
        print("新功能:")
        print("  - 窗口结束时自动查询Binance K线")
        print("  - 判断是否反转")
        print("  - 回填final_reversal字段到jsonl")
        print()
        print("下一步:")
        print("  - 等待6小时积累数据（约100条）")
        print("  - 所有数据都会有final_reversal字段")
        print("  - 重新训练EV优化模型")
        print("  - 实盘验证1小时")
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    upload_and_restart()
