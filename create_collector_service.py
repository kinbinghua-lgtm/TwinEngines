#!/usr/bin/env python3
"""创建独立的采集器service"""

from fabric import Connection

def create_collector_service():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("创建独立的采集器service")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 创建采集器service配置
        service_content = """[Unit]
Description=TwinEngines Shadow Signal Collector
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/TwinEngines
Environment="PATH=/root/TwinEngines/.venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/root/TwinEngines/.venv/bin/python -m twinengines.cli live-run --dry-run-signals --artifact data_runtime/artifact_v1.pkl
Restart=always
RestartSec=10
StandardOutput=append:/root/TwinEngines/logs/collector_stdout.log
StandardError=append:/root/TwinEngines/logs/collector_stderr.log

[Install]
WantedBy=multi-user.target
"""
        
        print("1. 创建service配置文件...")
        with open('twinengines-collector.service', 'w') as f:
            f.write(service_content)
        
        conn.put('twinengines-collector.service', '/tmp/twinengines-collector.service')
        conn.run('sudo mv /tmp/twinengines-collector.service /etc/systemd/system/', hide=True)
        print("  [OK] 配置文件已创建")
        print()
        
        # 2. 重新加载systemd
        print("2. 重新加载systemd...")
        conn.run('sudo systemctl daemon-reload', hide=True)
        print("  [OK] 已重新加载")
        print()
        
        # 3. 启动采集器
        print("3. 启动采集器service...")
        result = conn.run('sudo systemctl start twinengines-collector', hide=True, warn=True)
        if result.ok:
            print("  [OK] 采集器已启动")
        else:
            print("  [错误] 启动失败")
            print(result.stderr)
        print()
        
        # 4. 设置开机自启
        print("4. 设置开机自启...")
        conn.run('sudo systemctl enable twinengines-collector', hide=True)
        print("  [OK] 已设置")
        print()
        
        # 5. 检查状态
        print("5. 检查采集器状态...")
        result = conn.run('systemctl status twinengines-collector', hide=True, warn=True)
        
        for line in result.stdout.split('\n')[:10]:
            if 'Active:' in line or 'Main PID:' in line:
                print(f"  {line.strip()}")
        print()
        
        # 6. 等待几秒，查看日志
        print("6. 查看采集器日志（等待10秒）...")
        import time
        time.sleep(10)
        
        result = conn.run('tail -20 /root/TwinEngines/logs/collector_stdout.log', hide=True, warn=True)
        if result.stdout.strip():
            print("  最新日志:")
            for line in result.stdout.split('\n')[-10:]:
                if line.strip():
                    print(f"    {line}")
        print()
        
        print("="*60)
        print("完成")
        print("="*60)
        print()
        print("[OK] 采集器service已创建并启动")
        print()
        print("现在有两个service:")
        print("  1. twinengines-strategy: 实盘交易")
        print("  2. twinengines-collector: 采集器（新）")
        print()
        print("采集器会:")
        print("  - 监听K线数据")
        print("  - 生成shadow signals")
        print("  - 记录到logs/shadow_signals.jsonl")
        print("  - 窗口结束时自动回填结果")
        print()
        print("下一步:")
        print("  - 等待6小时积累数据")
        print("  - 检查数据是否正常")
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    create_collector_service()
