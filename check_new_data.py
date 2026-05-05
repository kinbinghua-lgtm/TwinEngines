#!/usr/bin/env python3
"""检查VPS上新采集的数据"""

from fabric import Connection
import json
from datetime import datetime

def check_new_data():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("检查新采集的数据")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 统计数据量
        result = conn.run(
            "cd /root/TwinEngines && wc -l logs/shadow_signals.jsonl",
            hide=True,
            warn=True
        )
        
        lines = int(result.stdout.strip().split()[0])
        print(f"shadow_signals.jsonl 总行数: {lines}")
        print()
        
        # 2. 检查时间范围
        result = conn.run(
            """cd /root/TwinEngines && python3 -c "
import json

with open('logs/shadow_signals.jsonl', 'r') as f:
    lines = f.readlines()
    
    if lines:
        first = json.loads(lines[0])
        last = json.loads(lines[-1])
        
        first_ts = first.get('ts_ms', 0)
        last_ts = last.get('ts_ms', 0)
        
        span_hours = (last_ts - first_ts) / 3600000
        
        print(f'第一条: {first_ts}')
        print(f'最后一条: {last_ts}')
        print(f'时间跨度: {span_hours:.1f} 小时')
" 2>/dev/null
""",
            hide=True,
            warn=True
        )
        
        print("时间范围:")
        for line in result.stdout.strip().split('\n'):
            print(f"  {line}")
        print()
        
        # 3. 采样最新数据
        result = conn.run(
            """cd /root/TwinEngines && tail -5 logs/shadow_signals.jsonl | python3 -c "
import sys
import json
from datetime import datetime

for i, line in enumerate(sys.stdin, 1):
    try:
        obj = json.loads(line.strip())
        ts_ms = obj.get('ts_ms', 0)
        dt = datetime.fromtimestamp(ts_ms / 1000)
        
        poly = obj.get('polymarket', {})
        book = poly.get('book', {})
        
        ask = book.get('best_ask', 'N/A')
        bid = book.get('best_bid', 'N/A')
        
        print(f'{i}. {dt} ask={ask} bid={bid}')
    except:
        pass
" 2>/dev/null
""",
            hide=True,
            warn=True
        )
        
        print("最新5条数据:")
        for line in result.stdout.strip().split('\n'):
            print(f"  {line}")
        print()
        
        print("="*60)
        print("结论")
        print("="*60)
        print()
        
        if lines >= 30:
            print(f"[OK] 有{lines}条数据，可以训练初步模型")
            print()
            print("下一步:")
            print("  1. 训练EV优化的生存模型")
            print("  2. 部署到VPS")
            print("  3. 实盘运行1小时")
            print("  4. 对比效果")
        else:
            print(f"[警告] 只有{lines}条数据，建议等待更多数据")
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")

if __name__ == "__main__":
    check_new_data()
