#!/usr/bin/env python3
"""
生成模拟盘口数据
"""

import sys
import json
import pickle
from pathlib import Path
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent))

from src.twinengines.simulation.kline_orderbook_simulator import (
    KlineBasedOrderBookSimulator,
    KlineFeatureExtractor,
)


def generate_simulated_orderbook(
    simulator_file,
    input_jsonl,
    output_jsonl,
    cache_dir='data_cache'
):
    """为所有记录生成模拟盘口"""
    
    print("="*60)
    print("生成模拟盘口数据")
    print("="*60)
    print()
    
    # 1. 加载模拟器
    print(f"加载模拟器: {simulator_file}")
    simulator = KlineBasedOrderBookSimulator.load(simulator_file)
    
    # 2. 读取输入数据
    print(f"读取输入数据: {input_jsonl}")
    records = []
    with open(input_jsonl, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except:
                    pass
    
    print(f"总记录数: {len(records)}")
    
    # 3. 为每条记录生成盘口
    print("\n生成模拟盘口...")
    
    augmented_records = []
    simulated_count = 0
    
    for record in tqdm(records):
        # 检查是否已有盘口
        poly = record.get('polymarket', {})
        book = poly.get('book', {})
        
        has_orderbook = bool(book.get('best_ask') and book.get('best_bid'))
        
        if has_orderbook:
            # 已有盘口，保持不变
            augmented_records.append(record)
        else:
            # 需要模拟盘口
            # 这里需要K线数据，简化版本使用统计模拟
            confidence = record.get('confidence') or record.get('confidence_on_pick', 0.5)
            
            # 使用窗口ID作为seed（可重现）
            window_id = record.get('window_id', '')
            seed = hash(window_id) % (2**31) if window_id else None
            
            # 简化：使用默认K线特征（需要改进）
            from src.twinengines.simulation.kline_orderbook_simulator import KlineFeatures
            
            # 估算特征（基于置信度）
            kline_features = KlineFeatures(
                volatility_1m=0.01,
                volatility_3m=0.01,
                volatility_5m=0.01,
                price_change_pct=0.0,
                price_range_pct=0.01,
                trend_direction=1,
                trend_strength=0.5,
                seconds_elapsed=180,
                seconds_remaining=120,
                price_percentile=0.5,
            )
            
            # 模拟盘口
            simulated_book = simulator.simulate(
                kline_features=kline_features,
                confidence=float(confidence),
                add_noise=True,
                seed=seed
            )
            
            # 添加到记录
            if 'polymarket' not in record:
                record['polymarket'] = {}
            if 'book' not in record['polymarket']:
                record['polymarket']['book'] = {}
            
            record['polymarket']['book']['best_ask'] = simulated_book.best_ask
            record['polymarket']['book']['best_bid'] = simulated_book.best_bid
            record['polymarket']['book']['spread'] = simulated_book.spread
            record['polymarket']['book']['simulated'] = True
            
            augmented_records.append(record)
            simulated_count += 1
    
    print(f"\n模拟了 {simulated_count} 条盘口数据")
    
    # 4. 保存
    print(f"保存到: {output_jsonl}")
    with open(output_jsonl, 'w', encoding='utf-8') as f:
        for record in augmented_records:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    
    print("\n✓ 完成")
    
    return augmented_records


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='生成模拟盘口数据')
    parser.add_argument('--simulator', default='orderbook_simulator.pkl',
                       help='模拟器文件')
    parser.add_argument('--input', required=True,
                       help='输入JSONL文件')
    parser.add_argument('--output', required=True,
                       help='输出JSONL文件')
    parser.add_argument('--cache-dir', default='data_cache',
                       help='Binance数据缓存目录')
    
    args = parser.parse_args()
    
    generate_simulated_orderbook(
        simulator_file=args.simulator,
        input_jsonl=args.input,
        output_jsonl=args.output,
        cache_dir=args.cache_dir
    )
