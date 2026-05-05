#!/usr/bin/env python3
"""分析现有盘口数据并设计模拟方案"""

import json
import sys
from pathlib import Path
from collections import defaultdict
import statistics

def analyze_orderbook_data(jsonl_files):
    """分析现有盘口数据的统计特征"""
    
    print("="*60)
    print("盘口数据分析")
    print("="*60)
    print()
    
    all_data = []
    
    for file_path in jsonl_files:
        if not Path(file_path).exists():
            continue
        
        print(f"读取: {file_path}")
        
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    all_data.append(obj)
                except:
                    pass
        
        print(f"  记录数: {len(all_data)}")
    
    print(f"\n总记录数: {len(all_data)}")
    print()
    
    # 提取盘口特征
    spreads = []
    bid_ask_ratios = []
    best_asks = []
    best_bids = []
    confidences = []
    edges = []
    outcomes = []
    
    for obj in all_data:
        # 尝试多种可能的字段名
        poly = obj.get('polymarket', {})
        book = poly.get('book', {})
        
        best_ask = (
            book.get('best_ask') or 
            obj.get('best_ask') or
            poly.get('best_ask')
        )
        
        best_bid = (
            book.get('best_bid') or 
            obj.get('best_bid') or
            poly.get('best_bid')
        )
        
        if best_ask and best_bid:
            try:
                ask = float(best_ask)
                bid = float(best_bid)
                
                if 0 < ask <= 1 and 0 < bid <= 1:
                    best_asks.append(ask)
                    best_bids.append(bid)
                    spreads.append(ask - bid)
                    bid_ask_ratios.append(bid / ask if ask > 0 else 0)
            except:
                pass
        
        # 提取其他特征
        conf = obj.get('confidence') or obj.get('confidence_on_pick')
        if conf:
            try:
                confidences.append(float(conf))
            except:
                pass
        
        edge = obj.get('edge_vs_best_ask') or obj.get('edge')
        if edge:
            try:
                edges.append(float(edge))
            except:
                pass
        
        matched = obj.get('matched')
        if matched is not None:
            outcomes.append(bool(matched))
    
    # 统计分析
    print("="*60)
    print("统计特征")
    print("="*60)
    print()
    
    if best_asks:
        print(f"Best Ask:")
        print(f"  样本数: {len(best_asks)}")
        print(f"  均值: {statistics.mean(best_asks):.4f}")
        print(f"  中位数: {statistics.median(best_asks):.4f}")
        print(f"  标准差: {statistics.stdev(best_asks):.4f}")
        print(f"  最小值: {min(best_asks):.4f}")
        print(f"  最大值: {max(best_asks):.4f}")
        print()
    
    if spreads:
        print(f"Spread (ask - bid):")
        print(f"  样本数: {len(spreads)}")
        print(f"  均值: {statistics.mean(spreads):.4f}")
        print(f"  中位数: {statistics.median(spreads):.4f}")
        print(f"  标准差: {statistics.stdev(spreads):.4f}")
        print(f"  最小值: {min(spreads):.4f}")
        print(f"  最大值: {max(spreads):.4f}")
        print()
    
    if bid_ask_ratios:
        print(f"Bid/Ask Ratio:")
        print(f"  样本数: {len(bid_ask_ratios)}")
        print(f"  均值: {statistics.mean(bid_ask_ratios):.4f}")
        print(f"  中位数: {statistics.median(bid_ask_ratios):.4f}")
        print()
    
    if confidences:
        print(f"Confidence:")
        print(f"  样本数: {len(confidences)}")
        print(f"  均值: {statistics.mean(confidences):.4f}")
        print(f"  中位数: {statistics.median(confidences):.4f}")
        print(f"  标准差: {statistics.stdev(confidences):.4f}")
        print()
    
    if edges:
        print(f"Edge:")
        print(f"  样本数: {len(edges)}")
        print(f"  均值: {statistics.mean(edges):.4f}")
        print(f"  中位数: {statistics.median(edges):.4f}")
        print(f"  标准差: {statistics.stdev(edges):.4f}")
        print()
    
    if outcomes:
        win_rate = sum(outcomes) / len(outcomes)
        print(f"Outcomes:")
        print(f"  样本数: {len(outcomes)}")
        print(f"  胜率: {win_rate:.2%}")
        print()
    
    # 返回统计参数
    return {
        'best_ask': {
            'mean': statistics.mean(best_asks) if best_asks else None,
            'std': statistics.stdev(best_asks) if len(best_asks) > 1 else None,
            'median': statistics.median(best_asks) if best_asks else None,
            'samples': best_asks,
        },
        'spread': {
            'mean': statistics.mean(spreads) if spreads else None,
            'std': statistics.stdev(spreads) if len(spreads) > 1 else None,
            'median': statistics.median(spreads) if spreads else None,
            'samples': spreads,
        },
        'bid_ask_ratio': {
            'mean': statistics.mean(bid_ask_ratios) if bid_ask_ratios else None,
            'std': statistics.stdev(bid_ask_ratios) if len(bid_ask_ratios) > 1 else None,
            'samples': bid_ask_ratios,
        },
        'confidence': {
            'samples': confidences,
        },
        'edge': {
            'samples': edges,
        },
        'outcomes': outcomes,
    }

if __name__ == "__main__":
    files = [
        "logs/shadow_signals_3h_book_v2.jsonl",
        "logs/naked_live_ticks.jsonl",
        "logs/naked_live_ticks_1h.jsonl",
        "logs/naked_live_ticks_cycle8.jsonl",
    ]
    
    stats = analyze_orderbook_data(files)
    
    # 保存统计结果
    with open("orderbook_stats.json", "w") as f:
        # 移除samples（太大）
        output = {}
        for key, val in stats.items():
            if isinstance(val, dict):
                output[key] = {k: v for k, v in val.items() if k != 'samples'}
        json.dump(output, f, indent=2)
    
    print("统计结果已保存到: orderbook_stats.json")
