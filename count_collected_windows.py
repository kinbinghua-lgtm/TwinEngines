#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统计采集器采集的5分钟窗口数量"""

import sys
import io
import json
from fabric import Connection

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

def main():
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    print("="*60)
    print("统计采集的5分钟窗口数量")
    print("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 1. 统计市场切换次数（每次切换=一个新的5分钟窗口）
        print("1. 市场切换统计（每次切换=新的5分钟窗口）:")
        result = conn.run(
            "grep 'market_changed' /root/TwinEngines/logs/collector_stdout.log 2>/dev/null | wc -l",
            hide=True,
            warn=True
        )
        market_changes = result.stdout.strip()
        print(f"   市场切换次数: {market_changes} 次")
        print(f"   = 已采集 {market_changes} 个5分钟窗口")
        print()
        
        # 2. 查看所有市场切换记录
        print("2. 所有市场切换记录:")
        result = conn.run(
            "grep 'market_changed' /root/TwinEngines/logs/collector_stdout.log 2>/dev/null",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            lines = result.stdout.strip().split('\n')
            print(f"   共 {len(lines)} 个窗口:")
            for i, line in enumerate(lines, 1):
                # 提取时间和condition_id
                parts = line.split()
                if len(parts) >= 2:
                    timestamp = f"{parts[0]} {parts[1]}"
                    # 提取condition_id
                    if 'condition_id' in line:
                        try:
                            payload_start = line.index('payload=')
                            payload_str = line[payload_start+8:]
                            payload = json.loads(payload_str)
                            cid_short = payload['condition_id'][:10] + "..." + payload['condition_id'][-6:]
                            end_time = payload.get('end_iso', 'N/A')
                            print(f"   窗口{i:2d}: {timestamp} | 结束时间: {end_time} | CID: {cid_short}")
                        except:
                            print(f"   窗口{i:2d}: {line[:120]}")
        print()
        
        # 3. 统计采集器运行时长
        print("3. 采集器运行时长:")
        result = conn.run(
            "ps -p 308909 -o etime= 2>/dev/null || echo '进程不存在'",
            hide=True,
            warn=True
        )
        runtime = result.stdout.strip()
        print(f"   运行时长: {runtime}")
        
        # 计算理论窗口数
        if runtime != '进程不存在' and ':' in runtime:
            parts = runtime.split(':')
            if len(parts) == 2:  # MM:SS
                total_minutes = int(parts[0])
            elif len(parts) == 3:  # HH:MM:SS
                total_minutes = int(parts[0]) * 60 + int(parts[1])
            else:
                total_minutes = 0
            
            theoretical_windows = total_minutes // 5
            print(f"   总运行分钟数: ~{total_minutes} 分钟")
            print(f"   理论窗口数: ~{theoretical_windows} 个 (每5分钟一个)")
            print(f"   实际采集: {market_changes} 个")
            if theoretical_windows > 0:
                coverage = int(market_changes) / theoretical_windows * 100
                print(f"   覆盖率: {coverage:.1f}%")
        print()
        
        # 4. 查看第一个和最后一个窗口
        print("4. 采集时间范围:")
        result = conn.run(
            "grep 'market_changed' /root/TwinEngines/logs/collector_stdout.log 2>/dev/null | head -1",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            first_line = result.stdout.strip()
            first_time = ' '.join(first_line.split()[:2])
            print(f"   第一个窗口: {first_time}")
        
        result = conn.run(
            "grep 'market_changed' /root/TwinEngines/logs/collector_stdout.log 2>/dev/null | tail -1",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            last_line = result.stdout.strip()
            last_time = ' '.join(last_line.split()[:2])
            print(f"   最后一个窗口: {last_time}")
        print()
        
        # 5. 检查是否有窗口数据被保存
        print("5. 检查数据文件:")
        result = conn.run(
            "ls -lh /root/TwinEngines/data_runtime/*.json 2>/dev/null | grep -v naked",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            print("   数据文件:")
            for line in result.stdout.strip().split('\n'):
                print(f"     {line}")
        
        # 查看状态文件
        result = conn.run(
            "cat /root/TwinEngines/data_runtime/naked_real_state.json 2>/dev/null",
            hide=True,
            warn=True
        )
        if result.stdout.strip():
            try:
                data = json.loads(result.stdout)
                if 'recent_condition_ids' in data:
                    recent_ids = data['recent_condition_ids']
                    print(f"\n   最近处理的condition_ids数量: {len(recent_ids)}")
                    print(f"   最近5个:")
                    for cid in recent_ids[:5]:
                        cid_short = cid[:10] + "..." + cid[-6:]
                        print(f"     {cid_short}")
            except:
                pass
        print()
        
        # 6. 统计日志中的窗口相关活动
        print("6. 窗口相关活动统计:")
        
        # 统计window_start相关日志
        result = conn.run(
            "grep -c 'window_start_ms' /root/TwinEngines/logs/collector_stdout.log 2>/dev/null || echo 0",
            hide=True,
            warn=True
        )
        window_mentions = result.stdout.strip()
        print(f"   提到window_start的日志行数: {window_mentions}")
        
        # 统计active market日志
        result = conn.run(
            "grep -c 'active market' /root/TwinEngines/logs/collector_stdout.log 2>/dev/null || echo 0",
            hide=True,
            warn=True
        )
        active_market = result.stdout.strip()
        print(f"   active market日志: {active_market}")
        print()
        
        conn.close()
        
        print("="*60)
        print("统计完成")
        print("="*60)
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
