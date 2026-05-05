#!/usr/bin/env python3
"""重启VPS采集器并验证"""

from fabric import Connection
import time

def restart_vps_collector():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("重启VPS采集器")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 检查当前状态
        print("1. 检查当前服务状态...")
        result = conn.run("systemctl status twinengines-strategy | head -20", hide=True, warn=True)
        # 只显示关键信息
        for line in result.stdout.split('\n')[:5]:
            try:
                print(line)
            except:
                pass
        print()
        
        # 2. 检查日志中的错误
        print("2. 检查最近的日志...")
        result = conn.run(
            "cd /root/TwinEngines && tail -50 logs/twinengines.log | grep -E 'shadow|error|exception' | tail -10",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            print("最近的相关日志:")
            print(result.stdout)
        else:
            print("没有发现明显错误")
        print()
        
        # 3. 重启服务
        print("3. 重启服务...")
        result = conn.run("systemctl restart twinengines-strategy", warn=True)
        print("[OK] 服务已重启")
        print()
        
        # 4. 等待启动
        print("4. 等待服务启动...")
        time.sleep(5)
        
        result = conn.run("systemctl is-active twinengines-strategy", hide=True, warn=True)
        status = result.stdout.strip()
        
        if status == "active":
            print("[OK] 服务运行中")
        else:
            print(f"[警告] 服务状态: {status}")
        print()
        
        # 5. 验证采集
        print("5. 验证采集器是否工作...")
        print("等待30秒，检查是否有新数据...")
        
        # 记录当前行数
        result = conn.run("wc -l /root/TwinEngines/logs/shadow_signals.jsonl 2>/dev/null || echo '0'", hide=True, warn=True)
        lines_before = int(result.stdout.strip().split()[0]) if result.stdout.strip() else 0
        print(f"当前行数: {lines_before}")
        
        time.sleep(30)
        
        result = conn.run("wc -l /root/TwinEngines/logs/shadow_signals.jsonl 2>/dev/null || echo '0'", hide=True, warn=True)
        lines_after = int(result.stdout.strip().split()[0]) if result.stdout.strip() else 0
        print(f"30秒后行数: {lines_after}")
        
        if lines_after > lines_before:
            print(f"[OK] 采集器正在工作！新增 {lines_after - lines_before} 条记录")
        else:
            print("[警告] 30秒内没有新数据，可能需要等待下一个窗口")
        print()
        
        # 6. 显示最新记录
        print("6. 最新的记录:")
        result = conn.run(
            "cd /root/TwinEngines && tail -1 logs/shadow_signals.jsonl",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            import json
            try:
                obj = json.loads(result.stdout.strip())
                print(f"  时间: {obj.get('ts_iso', 'N/A')}")
                print(f"  窗口: {obj.get('window_id', 'N/A')}")
                print(f"  置信度: {obj.get('confidence', 0):.3f}")
                
                poly = obj.get('polymarket', {})
                book = poly.get('book', {})
                if book.get('best_ask'):
                    print(f"  盘口: ask={book['best_ask']}, bid={book['best_bid']}")
                else:
                    print("  盘口: 无")
            except:
                print(result.stdout.strip()[:200])
        print()
        
        print("="*60)
        print("完成")
        print("="*60)
        print()
        print("[OK] 采集器已重启")
        print()
        print("下一步:")
        print("  - 采集器将在每个窗口记录盘口数据")
        print("  - 每5分钟一个窗口")
        print("  - 6小时后约有72个窗口的数据")
        print("  - 然后可以训练正式的模拟器")
        print()
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    restart_vps_collector()
