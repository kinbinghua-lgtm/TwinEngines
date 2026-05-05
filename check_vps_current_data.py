#!/usr/bin/env python3
"""检查VPS上最新的采集数据"""

from fabric import Connection
import json
from datetime import datetime

def check_vps_data():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("检查VPS采集数据")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 统计总数
        print("1. 统计数据量...")
        result = conn.run(
            "cd /root/TwinEngines && wc -l logs/shadow_signals.jsonl",
            hide=True,
            warn=True
        )
        
        lines = int(result.stdout.strip().split()[0])
        print(f"  总记录数: {lines}")
        print()
        
        # 2. 检查有结算结果的数量
        print("2. 检查有结算结果的数量...")
        result = conn.run(
            """cd /root/TwinEngines && python3 -c "
import json

total = 0
with_result = 0
reversal_true = 0
reversal_false = 0

with open('logs/shadow_signals.jsonl', 'r') as f:
    for line in f:
        try:
            obj = json.loads(line.strip())
            total += 1
            
            final_reversal = obj.get('final_reversal')
            if final_reversal is not None:
                with_result += 1
                if final_reversal:
                    reversal_true += 1
                else:
                    reversal_false += 1
        except:
            pass

print(f'Total: {total}')
print(f'With result: {with_result}')
print(f'Reversal True: {reversal_true}')
print(f'Reversal False: {reversal_false}')
if with_result > 0:
    print(f'Win rate: {reversal_true / with_result * 100:.1f}%')
" 2>/dev/null
""",
            hide=True,
            warn=True
        )
        
        for line in result.stdout.strip().split('\n'):
            print(f"  {line}")
        print()
        
        # 3. 查看最新3条
        print("3. 最新3条记录...")
        result = conn.run(
            """cd /root/TwinEngines && tail -3 logs/shadow_signals.jsonl | python3 -c "
import sys
import json
from datetime import datetime

for i, line in enumerate(sys.stdin, 1):
    try:
        obj = json.loads(line.strip())
        ts_ms = obj.get('ts_ms', 0)
        dt = datetime.fromtimestamp(ts_ms / 1000)
        
        window_id = obj.get('window_id', 'N/A')
        final_reversal = obj.get('final_reversal')
        
        poly = obj.get('polymarket', {})
        book = poly.get('book', {})
        ask = book.get('best_ask', 'N/A')
        
        print(f'{i}. {dt} {window_id} ask={ask} result={final_reversal}')
    except:
        pass
" 2>/dev/null
""",
            hide=True,
            warn=True
        )
        
        for line in result.stdout.strip().split('\n'):
            if line.strip():
                print(f"  {line}")
        print()
        
        # 4. 检查时间跨度
        print("4. 时间跨度...")
        result = conn.run(
            """cd /root/TwinEngines && python3 -c "
import json
from datetime import datetime

with open('logs/shadow_signals.jsonl', 'r') as f:
    lines = f.readlines()
    
    if lines:
        first = json.loads(lines[0])
        last = json.loads(lines[-1])
        
        first_ts = first.get('ts_ms', 0)
        last_ts = last.get('ts_ms', 0)
        
        first_dt = datetime.fromtimestamp(first_ts / 1000)
        last_dt = datetime.fromtimestamp(last_ts / 1000)
        
        span_hours = (last_ts - first_ts) / 3600000
        
        print(f'First: {first_dt}')
        print(f'Last: {last_dt}')
        print(f'Span: {span_hours:.1f} hours')
        print(f'Windows: ~{int(span_hours * 12)} (5min each)')
" 2>/dev/null
""",
            hide=True,
            warn=True
        )
        
        for line in result.stdout.strip().split('\n'):
            print(f"  {line}")
        print()
        
        print("="*60)
        print("总结")
        print("="*60)
        print()
        
        if lines >= 50:
            print(f"[OK] 有{lines}条记录，可以训练了！")
        elif lines >= 30:
            print(f"[进行中] 有{lines}条记录，建议再等待一会")
        else:
            print(f"[等待中] 有{lines}条记录，建议等待更多数据")
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    check_vps_data()
