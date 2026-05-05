#!/usr/bin/env python3
"""分析VPS上的naked_live_ticks数据"""

from fabric import Connection
import json

def analyze_vps_naked_live_ticks():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("分析VPS naked_live_ticks数据")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 分析数据
        result = conn.run(
            """cd /root/TwinEngines && python3 -c "
import json
import sys

total = 0
with_orderbook = 0
unique_windows = set()
confidences = []
spreads = []
timestamps = []

try:
    with open('logs/naked_live_ticks.jsonl', 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            
            try:
                obj = json.loads(line)
                total += 1
                
                # 提取窗口ID
                window_id = obj.get('window_id', '')
                if window_id:
                    unique_windows.add(window_id)
                
                # 提取时间戳
                ts_ms = obj.get('ts_ms')
                if ts_ms:
                    timestamps.append(ts_ms)
                
                # 检查盘口数据
                # 方式1: 直接字段
                best_ask = obj.get('best_ask')
                best_bid = obj.get('best_bid')
                
                # 方式2: polymarket.book
                if not (best_ask and best_bid):
                    poly = obj.get('polymarket', {})
                    book = poly.get('book', {})
                    best_ask = book.get('best_ask')
                    best_bid = book.get('best_bid')
                
                # 方式3: order_plan
                if not (best_ask and best_bid):
                    order_plan = obj.get('order_plan', {})
                    best_ask = order_plan.get('best_ask')
                    best_bid = order_plan.get('best_bid')
                
                if best_ask and best_bid:
                    try:
                        ask = float(best_ask)
                        bid = float(best_bid)
                        if 0 < ask <= 1 and 0 < bid <= 1 and bid < ask:
                            with_orderbook += 1
                            spreads.append(ask - bid)
                            
                            # 提取置信度
                            conf = obj.get('confidence') or obj.get('confidence_on_pick')
                            if conf:
                                confidences.append(float(conf))
                    except:
                        pass
            
            except Exception as e:
                pass
    
    # 计算时间跨度
    time_span_hours = 0
    if len(timestamps) >= 2:
        time_span_hours = (max(timestamps) - min(timestamps)) / 3600000
    
    # 输出结果
    print(json.dumps({
        'total_records': total,
        'with_orderbook': with_orderbook,
        'orderbook_rate': round(with_orderbook / total * 100, 2) if total > 0 else 0,
        'unique_windows': len(unique_windows),
        'time_span_hours': round(time_span_hours, 2),
        'avg_spread': round(sum(spreads) / len(spreads), 4) if spreads else 0,
        'avg_confidence': round(sum(confidences) / len(confidences), 4) if confidences else 0,
        'records_per_window': round(total / len(unique_windows), 1) if unique_windows else 0,
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
                stats = json.loads(result.stdout.strip())
                
                print("数据统计:")
                print(f"  总记录数: {stats.get('total_records', 0):,}")
                print(f"  有盘口数据: {stats.get('with_orderbook', 0):,} ({stats.get('orderbook_rate', 0)}%)")
                print(f"  唯一窗口数: {stats.get('unique_windows', 0):,}")
                print(f"  时间跨度: {stats.get('time_span_hours', 0):.1f} 小时 ({stats.get('time_span_hours', 0)/24:.1f} 天)")
                print(f"  每窗口记录数: {stats.get('records_per_window', 0):.1f}")
                print()
                
                if stats.get('with_orderbook', 0) > 0:
                    print("盘口特征:")
                    print(f"  平均Spread: {stats.get('avg_spread', 0):.4f}")
                    print(f"  平均Confidence: {stats.get('avg_confidence', 0):.4f}")
                    print()
                
                # 评估
                print("="*60)
                print("评估")
                print("="*60)
                print()
                
                with_book = stats.get('with_orderbook', 0)
                windows = stats.get('unique_windows', 0)
                
                if with_book >= 100:
                    print(f"[OK] 有{with_book}条盘口数据，足够训练初步模型！")
                elif with_book >= 50:
                    print(f"[OK] 有{with_book}条盘口数据，可以训练基础模型")
                elif with_book >= 20:
                    print(f"[警告] 只有{with_book}条盘口数据，样本较少")
                else:
                    print(f"[警告] 只有{with_book}条盘口数据，样本太少")
                
                print()
                print(f"窗口数: {windows}个")
                print(f"  理论上每个窗口5分钟")
                print(f"  {windows}个窗口 ≈ {windows * 5 / 60:.1f} 小时")
                print()
                
                if with_book > 0:
                    print("建议:")
                    print("  1. 立即在VPS上训练模拟器")
                    print("  2. 使用这些真实盘口数据")
                    print("  3. 匹配对应的K线特征")
                    print("  4. 训练基于K线的盘口模拟器")
                
            except Exception as e:
                print(f"解析失败: {e}")
                print(f"原始输出: {result.stdout.strip()}")
        
        # 采样几条数据看看格式
        print()
        print("="*60)
        print("数据采样（最近5条）")
        print("="*60)
        print()
        
        result = conn.run(
            """cd /root/TwinEngines && tail -5 logs/naked_live_ticks.jsonl | python3 -c "
import sys
import json

for i, line in enumerate(sys.stdin, 1):
    try:
        obj = json.loads(line.strip())
        
        # 尝试提取盘口
        best_ask = obj.get('best_ask')
        best_bid = obj.get('best_bid')
        
        if not (best_ask and best_bid):
            poly = obj.get('polymarket', {})
            book = poly.get('book', {})
            best_ask = book.get('best_ask')
            best_bid = book.get('best_bid')
        
        if not (best_ask and best_bid):
            order_plan = obj.get('order_plan', {})
            best_ask = order_plan.get('best_ask')
            best_bid = order_plan.get('best_bid')
        
        window_id = obj.get('window_id', 'N/A')
        conf = obj.get('confidence') or obj.get('confidence_on_pick', 0)
        
        if best_ask and best_bid:
            print(f'{i}. window={window_id[-12:]}, conf={conf:.3f}, ask={best_ask}, bid={best_bid}, has_book=YES')
        else:
            print(f'{i}. window={window_id[-12:]}, conf={conf:.3f}, has_book=NO')
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
        
        conn.close()
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    analyze_vps_naked_live_ticks()
