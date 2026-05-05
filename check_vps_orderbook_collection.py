#!/usr/bin/env python3
"""检查VPS盘口采集状态"""

from fabric import Connection
import json
from datetime import datetime

def check_vps_orderbook_collection():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("检查VPS盘口采集状态")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 检查shadow_signals.jsonl
        print("1. 检查shadow_signals.jsonl...")
        result = conn.run(
            """cd /root/TwinEngines && python3 -c "
import json
import time

try:
    with open('logs/shadow_signals.jsonl', 'r') as f:
        lines = f.readlines()
    
    total = len(lines)
    
    # 检查最后一条
    if lines:
        last = json.loads(lines[-1].strip())
        last_ts = last.get('ts_ms', 0)
        now_ms = int(time.time() * 1000)
        age_minutes = (now_ms - last_ts) / 60000
        
        # 检查最近10条
        recent_with_book = 0
        for line in lines[-10:]:
            try:
                obj = json.loads(line.strip())
                poly = obj.get('polymarket', {})
                book = poly.get('book', {})
                if book.get('best_ask') and book.get('best_bid'):
                    recent_with_book += 1
            except:
                pass
        
        print(json.dumps({
            'total': total,
            'last_ts_ms': last_ts,
            'age_minutes': round(age_minutes, 2),
            'recent_with_book': recent_with_book,
            'recent_total': min(10, total)
        }))
    else:
        print(json.dumps({'total': 0}))
except Exception as e:
    print(json.dumps({'error': str(e)}))
" 2>/dev/null || echo '{}'
""",
            hide=True,
            warn=True
        )
        
        if result.stdout.strip():
            try:
                stats = json.loads(result.stdout.strip())
                print(f"  总记录数: {stats.get('total', 0)}")
                print(f"  最后更新: {stats.get('age_minutes', 0):.1f} 分钟前")
                print(f"  最近10条中有盘口: {stats.get('recent_with_book', 0)}/{stats.get('recent_total', 0)}")
                
                if stats.get('age_minutes', 999) < 10:
                    print("  [OK] 采集器正在工作")
                else:
                    print("  [警告] 采集器可能已停止")
            except:
                print("  解析失败")
        print()
        
        # 2. 检查naked_live_ticks.jsonl
        print("2. 检查naked_live_ticks.jsonl...")
        result = conn.run(
            "cd /root/TwinEngines && wc -l logs/naked_live_ticks.jsonl 2>/dev/null || echo '0'",
            hide=True,
            warn=True
        )
        lines = int(result.stdout.strip().split()[0]) if result.stdout.strip() else 0
        print(f"  总记录数: {lines:,}")
        print()
        
        # 3. 检查服务状态
        print("3. 检查策略服务状态...")
        result = conn.run("systemctl is-active twinengines-strategy", hide=True, warn=True)
        status = result.stdout.strip()
        print(f"  服务状态: {status}")
        
        if status == "active":
            print("  [OK] 服务运行中")
        else:
            print("  [警告] 服务未运行")
        print()
        
        # 4. 采样最新数据
        print("4. 采样最新的盘口数据...")
        result = conn.run(
            """cd /root/TwinEngines && tail -5 logs/shadow_signals.jsonl | python3 -c "
import sys
import json

for line in sys.stdin:
    try:
        obj = json.loads(line.strip())
        poly = obj.get('polymarket', {})
        book = poly.get('book', {})
        
        ts_ms = obj.get('ts_ms', 0)
        conf = obj.get('confidence', 0)
        ask = book.get('best_ask')
        bid = book.get('best_bid')
        
        if ask and bid:
            print(f'ts={ts_ms}, conf={conf:.3f}, ask={ask:.4f}, bid={bid:.4f}, spread={float(ask)-float(bid):.4f}')
    except:
        pass
" 2>/dev/null
""",
            hide=True,
            warn=True
        )
        
        if result.stdout.strip():
            print("  最近5条记录:")
            for line in result.stdout.strip().split('\n'):
                print(f"    {line}")
        else:
            print("  无数据")
        print()
        
        # 5. 估算1小时能采集多少
        print("5. 估算采集速度...")
        result = conn.run(
            """cd /root/TwinEngines && python3 -c "
import json
import time

try:
    with open('logs/shadow_signals.jsonl', 'r') as f:
        lines = f.readlines()
    
    if len(lines) < 2:
        print('样本不足')
    else:
        # 取最后100条计算速度
        recent = lines[-100:] if len(lines) >= 100 else lines
        
        timestamps = []
        for line in recent:
            try:
                obj = json.loads(line.strip())
                ts = obj.get('ts_ms')
                if ts:
                    timestamps.append(ts)
            except:
                pass
        
        if len(timestamps) >= 2:
            span_minutes = (timestamps[-1] - timestamps[0]) / 60000
            rate_per_hour = len(timestamps) / span_minutes * 60 if span_minutes > 0 else 0
            
            # 每个窗口5分钟，1小时12个窗口
            windows_per_hour = 12
            records_per_window = rate_per_hour / windows_per_hour if windows_per_hour > 0 else 0
            
            print(f'采集速度: {rate_per_hour:.1f} 条/小时')
            print(f'每窗口约: {records_per_window:.1f} 条')
            print(f'1小时约: {windows_per_hour} 个窗口')
except Exception as e:
    print(f'错误: {e}')
" 2>/dev/null
""",
            hide=True,
            warn=True
        )
        
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                print(f"  {line}")
        print()
        
        print("="*60)
        print("结论")
        print("="*60)
        print()
        print("建议:")
        print("  1. 如果采集器正在工作，等待1-2小时积累数据")
        print("  2. 然后用VPS上的真实数据训练模拟器")
        print("  3. VPS有K线缓存，可以直接在VPS上训练")
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    check_vps_orderbook_collection()
