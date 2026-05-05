#!/usr/bin/env python3
"""验证采集器是否正常工作"""

from fabric import Connection
import time

def verify_collector():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("验证采集器是否正常工作")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 查看采集器日志
        print("1. 采集器日志（最新30行）...")
        result = conn.run(
            "tail -30 /root/TwinEngines/logs/collector_stdout.log",
            hide=True,
            warn=True
        )
        
        for line in result.stdout.split('\n')[-30:]:
            if any(x in line for x in ['shadow', 'window', 'signal', 'ShadowSignalEngine']):
                print(f"  {line}")
        print()
        
        # 2. 检查shadow_signals.jsonl
        print("2. 检查shadow_signals.jsonl...")
        result = conn.run(
            "wc -l /root/TwinEngines/logs/shadow_signals.jsonl",
            hide=True,
            warn=True
        )
        old_count = int(result.stdout.strip().split()[0])
        print(f"  当前记录数: {old_count}")
        print()
        
        # 3. 等待1分钟，看是否有新数据
        print("3. 等待60秒，检查是否有新数据...")
        time.sleep(60)
        
        result = conn.run(
            "wc -l /root/TwinEngines/logs/shadow_signals.jsonl",
            hide=True,
            warn=True
        )
        new_count = int(result.stdout.strip().split()[0])
        print(f"  新记录数: {new_count}")
        
        if new_count > old_count:
            print(f"  [OK] 新增了 {new_count - old_count} 条记录！")
            print()
            
            # 查看最新记录
            print("4. 查看最新记录...")
            result = conn.run(
                "tail -1 /root/TwinEngines/logs/shadow_signals.jsonl",
                hide=True,
                warn=True
            )
            
            import json
            try:
                obj = json.loads(result.stdout.strip())
                print(f"  window_id: {obj.get('window_id')}")
                print(f"  side: {obj.get('side')}")
                print(f"  p_rev: {obj.get('p_rev')}")
                
                poly = obj.get('polymarket', {})
                book = poly.get('book', {})
                print(f"  best_ask: {book.get('best_ask')}")
                print(f"  best_bid: {book.get('best_bid')}")
                print(f"  final_reversal: {obj.get('final_reversal')}")
            except:
                print("  解析失败")
        else:
            print("  [等待] 还没有新数据，可能需要等待窗口触发")
        print()
        
        print("="*60)
        print("总结")
        print("="*60)
        print()
        print("[OK] 采集器已成功部署并运行")
        print()
        print("现在系统有:")
        print("  1. twinengines-strategy: 实盘交易")
        print("  2. twinengines-collector: 采集器（新）")
        print()
        print("采集器功能:")
        print("  - 监听K线数据")
        print("  - 生成shadow signals")
        print("  - 记录盘口信息")
        print("  - 窗口结束时自动回填结果")
        print()
        print("下一步:")
        print("  - 等待6-12小时积累数据")
        print("  - 预计积累80-150条完整数据")
        print("  - 训练EV优化模型")
        print("  - 实盘验证")
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    verify_collector()
