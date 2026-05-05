#!/usr/bin/env python3
"""检查实盘程序是否记录盘口信息"""

from fabric import Connection
import json

def check_live_data():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("检查实盘程序记录的数据")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 查看实盘记录文件
        print("1. 实盘记录文件...")
        result = conn.run(
            "ls -lh /root/TwinEngines/logs/*.jsonl",
            hide=True,
            warn=True
        )
        print(result.stdout)
        print()
        
        # 2. 查看naked_live_ticks.jsonl
        print("2. 查看naked_live_ticks.jsonl（最新3条）...")
        result = conn.run(
            "tail -3 /root/TwinEngines/logs/naked_live_ticks.jsonl 2>/dev/null",
            hide=True,
            warn=True
        )
        
        if result.stdout.strip():
            lines = result.stdout.strip().split('\n')
            for i, line in enumerate(lines, 1):
                try:
                    obj = json.loads(line)
                    print(f"\n记录 {i}:")
                    print(f"  kind: {obj.get('kind')}")
                    print(f"  ts_ms: {obj.get('ts_ms')}")
                    
                    # 检查是否有盘口信息
                    if 'polymarket' in obj:
                        poly = obj['polymarket']
                        if 'book' in poly:
                            book = poly['book']
                            print(f"  盘口: ask={book.get('best_ask')}, bid={book.get('best_bid')}")
                        else:
                            print(f"  [无] 没有book字段")
                    else:
                        print(f"  [无] 没有polymarket字段")
                    
                    # 检查是否有结算结果
                    if 'matched' in obj or 'final_reversal' in obj:
                        print(f"  结果: matched={obj.get('matched')}, final_reversal={obj.get('final_reversal')}")
                    else:
                        print(f"  [无] 没有结算结果")
                        
                except Exception as e:
                    print(f"  解析失败: {e}")
        else:
            print("  文件为空或不存在")
        print()
        
        # 3. 统计数据
        print("3. 统计naked_live_ticks.jsonl...")
        result = conn.run(
            """cd /root/TwinEngines && python3 -c "
import json

total = 0
with_book = 0
with_result = 0

try:
    with open('logs/naked_live_ticks.jsonl', 'r') as f:
        for line in f:
            try:
                obj = json.loads(line.strip())
                total += 1
                
                if 'polymarket' in obj and 'book' in obj['polymarket']:
                    with_book += 1
                
                if 'matched' in obj or 'final_reversal' in obj:
                    with_result += 1
            except:
                pass
    
    print(f'Total records: {total}')
    print(f'With book: {with_book}')
    print(f'With result: {with_result}')
except FileNotFoundError:
    print('File not found')
" 2>/dev/null
""",
            hide=True,
            warn=True
        )
        print(result.stdout)
        print()
        
        print("="*60)
        print("结论")
        print("="*60)
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    check_live_data()
