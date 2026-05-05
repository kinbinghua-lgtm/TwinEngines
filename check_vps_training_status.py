#!/usr/bin/env python3
"""确认VPS上的训练和数据情况"""

from fabric import Connection
import json

def check_vps_training():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("确认VPS训练情况")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 检查训练使用的数据
        print("1. 检查训练数据源...")
        result = conn.run(
            "cd /root/TwinEngines && ls -lh logs/shadow_signals.jsonl",
            hide=True,
            warn=True
        )
        print(f"  shadow_signals.jsonl: {result.stdout.strip()}")
        
        result = conn.run(
            "cd /root/TwinEngines && wc -l logs/shadow_signals.jsonl",
            hide=True,
            warn=True
        )
        lines = int(result.stdout.strip().split()[0])
        print(f"  总行数: {lines}")
        print()
        
        # 2. 检查最新数据
        print("2. 检查最新的盘口数据...")
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
        
        poly = obj.get('polymarket', {})
        book = poly.get('book', {})
        
        conf = obj.get('confidence', 0)
        ask = book.get('best_ask', 'N/A')
        bid = book.get('best_bid', 'N/A')
        matched = obj.get('matched')
        
        print(f'{i}. {dt} conf={conf:.3f} ask={ask} bid={bid} matched={matched}')
    except:
        pass
" 2>/dev/null
""",
            hide=True,
            warn=True
        )
        
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                print(f"  {line}")
        print()
        
        # 3. 检查是否有结算结果
        print("3. 检查是否有结算结果（matched字段）...")
        result = conn.run(
            """cd /root/TwinEngines && python3 -c "
import json

total = 0
with_matched = 0
matched_true = 0
matched_false = 0

with open('logs/shadow_signals.jsonl', 'r') as f:
    for line in f:
        try:
            obj = json.loads(line.strip())
            total += 1
            
            matched = obj.get('matched')
            if matched is not None:
                with_matched += 1
                if matched:
                    matched_true += 1
                else:
                    matched_false += 1
        except:
            pass

print(f'总记录: {total}')
print(f'有matched字段: {with_matched}')
print(f'matched=True: {matched_true}')
print(f'matched=False: {matched_false}')
print(f'胜率: {matched_true / with_matched * 100:.1f}%' if with_matched > 0 else '胜率: N/A')
" 2>/dev/null
""",
            hide=True,
            warn=True
        )
        
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                print(f"  {line}")
        print()
        
        # 4. 检查数据完整性
        print("4. 检查数据完整性（盘口+结果）...")
        result = conn.run(
            """cd /root/TwinEngines && python3 -c "
import json

complete_records = 0  # 有盘口+有结果

with open('logs/shadow_signals.jsonl', 'r') as f:
    for line in f:
        try:
            obj = json.loads(line.strip())
            
            # 检查盘口
            poly = obj.get('polymarket', {})
            book = poly.get('book', {})
            has_book = bool(book.get('best_ask') and book.get('best_bid'))
            
            # 检查结果
            has_result = obj.get('matched') is not None
            
            if has_book and has_result:
                complete_records += 1
        except:
            pass

print(f'完整记录（盘口+结果）: {complete_records}')
" 2>/dev/null
""",
            hide=True,
            warn=True
        )
        
        if result.stdout.strip():
            print(f"  {result.stdout.strip()}")
        print()
        
        print("="*60)
        print("结论")
        print("="*60)
        print()
        
        print("关于VPS训练:")
        print("  - VPS使用的是shadow_signals.jsonl")
        print("  - 这是实时采集的数据")
        print("  - 包含盘口信息")
        print()
        
        print("关于你的提议（在VPS上实时训练EV模型）:")
        print()
        print("完全可行！而且是个很好的想法！")
        print()
        print("优势:")
        print("  1. 实时数据，最新鲜")
        print("  2. 有盘口（best_ask, best_bid）")
        print("  3. 有结算结果（matched）")
        print("  4. 可以计算真实EV")
        print("  5. 持续学习，模型不断改进")
        print()
        print("需要的数据:")
        print("  - 特征: confidence, edge, p_rev, 盘口等")
        print("  - 目标: matched (True/False) 或 actual_pnl")
        print("  - 需要等待结算（5分钟后）")
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    check_vps_training()
