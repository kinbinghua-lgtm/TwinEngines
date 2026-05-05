#!/usr/bin/env python3
"""检查naked_live_ticks的实际数据格式"""

from fabric import Connection
import json

def check_format():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("检查naked_live_ticks数据格式")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 获取一条完整的记录
        result = conn.run(
            "cd /root/TwinEngines && tail -1 logs/naked_live_ticks.jsonl",
            hide=True,
            warn=True
        )
        
        if result.stdout.strip():
            print("最新一条记录的完整内容:")
            print()
            
            try:
                obj = json.loads(result.stdout.strip())
                print(json.dumps(obj, indent=2, ensure_ascii=False)[:2000])
                
                print("\n" + "="*60)
                print("字段分析")
                print("="*60)
                print()
                print(f"顶层字段: {list(obj.keys())}")
                
                # 检查各种可能的盘口位置
                print("\n检查盘口数据位置:")
                
                # 1. 顶层
                if 'best_ask' in obj:
                    print(f"  [OK] 顶层有best_ask: {obj['best_ask']}")
                else:
                    print("  [X] 顶层无best_ask")
                
                # 2. polymarket.book
                if 'polymarket' in obj:
                    poly = obj['polymarket']
                    print(f"  polymarket字段: {list(poly.keys()) if isinstance(poly, dict) else type(poly)}")
                    if isinstance(poly, dict) and 'book' in poly:
                        book = poly['book']
                        print(f"    book字段: {list(book.keys()) if isinstance(book, dict) else type(book)}")
                
                # 3. order_plan
                if 'order_plan' in obj:
                    plan = obj['order_plan']
                    print(f"  order_plan字段: {list(plan.keys()) if isinstance(plan, dict) else type(plan)}")
                
                # 4. prediction
                if 'prediction' in obj:
                    pred = obj['prediction']
                    print(f"  prediction字段: {list(pred.keys()) if isinstance(pred, dict) else type(pred)}")
                
            except Exception as e:
                print(f"解析失败: {e}")
                print(f"原始内容: {result.stdout.strip()[:500]}")
        
        print("\n" + "="*60)
        print("结论")
        print("="*60)
        print()
        print("naked_live_ticks.jsonl 是实盘运行日志，记录每个tick的:")
        print("  - 预测结果（confidence, p_rev等）")
        print("  - 决策信息（是否下单等）")
        print("  - 但可能不包含完整的盘口快照")
        print()
        print("真正的盘口数据应该在:")
        print("  - shadow_signals.jsonl (影子信号，包含盘口)")
        print("  - 或者需要实时采集")
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")

if __name__ == "__main__":
    check_format()
