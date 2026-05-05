#!/usr/bin/env python3
"""检查本地和VPS的盘口数据，并设计模拟方案"""

from fabric import Connection
import json

def check_vps_data():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("检查VPS盘口数据")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 统计VPS上的数据文件
        result = conn.run(
            "cd /root/TwinEngines && find logs -name '*.jsonl' -type f -exec wc -l {} \\; 2>/dev/null",
            hide=True,
            warn=True
        )
        
        print("VPS上的JSONL文件:")
        for line in result.stdout.strip().split('\n'):
            print(f"  {line}")
        print()
        
        # 分析shadow_signals.jsonl
        result = conn.run(
            """cd /root/TwinEngines && python3 -c "
import json
import sys

total = 0
with_book = 0
with_outcome = 0

try:
    with open('logs/shadow_signals.jsonl', 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                total += 1
                
                # 检查盘口数据
                poly = obj.get('polymarket', {})
                book = poly.get('book', {})
                if book.get('best_ask') and book.get('best_bid'):
                    with_book += 1
                
                # 检查结果
                if obj.get('matched') is not None:
                    with_outcome += 1
            except:
                pass
    
    print(json.dumps({
        'total': total,
        'with_book': with_book,
        'with_outcome': with_outcome,
        'book_rate': round(with_book / total * 100, 2) if total > 0 else 0,
        'outcome_rate': round(with_outcome / total * 100, 2) if total > 0 else 0
    }))
except Exception as e:
    print(json.dumps({'error': str(e)}))
" 2>/dev/null || echo '{}'
""",
            hide=True,
            warn=True
        )
        
        if result.stdout.strip():
            try:
                vps_stats = json.loads(result.stdout.strip())
                print("VPS shadow_signals.jsonl 统计:")
                print(f"  总记录数: {vps_stats.get('total', 0)}")
                print(f"  有盘口数据: {vps_stats.get('with_book', 0)} ({vps_stats.get('book_rate', 0)}%)")
                print(f"  有结算结果: {vps_stats.get('with_outcome', 0)} ({vps_stats.get('outcome_rate', 0)}%)")
            except:
                print("  解析失败")
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")

if __name__ == "__main__":
    check_vps_data()
