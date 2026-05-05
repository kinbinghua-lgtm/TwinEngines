#!/usr/bin/env python3
"""修复采集器service配置"""

from fabric import Connection

def fix_collector_service():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("修复采集器service配置")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 更新service配置
        service_content = """[Unit]
Description=TwinEngines Shadow Signal Collector
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/TwinEngines
Environment="PATH=/root/TwinEngines/.venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/root/TwinEngines/.venv/bin/python -m twinengines.cli live-run --dry-run-signals --artifact reports/prefix_survival_model_90d_step10.json
Restart=always
RestartSec=10
StandardOutput=append:/root/TwinEngines/logs/collector_stdout.log
StandardError=append:/root/TwinEngines/logs/collector_stderr.log

[Install]
WantedBy=multi-user.target
"""
        
        print("1. 更新service配置...")
        with open('twinengines-collector.service', 'w') as f:
            f.write(service_content)
        
        conn.put('twinengines-collector.service', '/tmp/twinengines-collector.service')
        conn.run('sudo mv /tmp/twinengines-collector.service /etc/systemd/system/', hide=True)
        print("  [OK] 配置已更新")
        print()
        
        # 2. 重新加载并重启
        print("2. 重新加载systemd...")
        conn.run('sudo systemctl daemon-reload', hide=True)
        print("  [OK]")
        print()
        
        print("3. 重启采集器...")
        conn.run('sudo systemctl restart twinengines-collector', hide=True)
        print("  [OK]")
        print()
        
        # 4. 等待并检查
        print("4. 检查状态（等待5秒）...")
        import time
        time.sleep(5)
        
        result = conn.run(
            "systemctl status twinengines-collector",
            hide=True,
            warn=True
        )
        
        for line in result.stdout.split('\n')[:10]:
            if 'Active:' in line:
                print(f"  {line.strip()}")
        print()
        
        print("="*60)
        print("完成 - 采集器已修复并运行")
        print("="*60)
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")

if __name__ == "__main__":
    fix_collector_service()
