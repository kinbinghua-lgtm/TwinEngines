#!/usr/bin/env python3
from fabric import Connection

vps_host = "47.243.169.223"
vps_user = "root"
vps_password = "Jinbh1977"

print("修复采集器service...")

conn = Connection(host=vps_host, user=vps_user, connect_kwargs={"password": vps_password})

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

with open('twinengines-collector.service', 'w') as f:
    f.write(service_content)

conn.put('twinengines-collector.service', '/tmp/twinengines-collector.service')
conn.run('sudo mv /tmp/twinengines-collector.service /etc/systemd/system/', hide=True)
conn.run('sudo systemctl daemon-reload', hide=True)
conn.run('sudo systemctl restart twinengines-collector', hide=True)

import time
time.sleep(5)

result = conn.run("systemctl status twinengines-collector", hide=True, warn=True)
print(result.stdout.split('\n')[2])

print("\n[OK] 采集器已修复并重启")
print("等待6-12小时积累数据...")

conn.close()
