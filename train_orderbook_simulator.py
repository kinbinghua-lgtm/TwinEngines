#!/usr/bin/env python3
"""
训练盘口模拟器
"""

import sys
import pickle
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.twinengines.simulation.kline_orderbook_simulator import (
    KlineBasedOrderBookSimulator,
    validate_simulator,
)


def train_simulator(training_data_file, output_file='orderbook_simulator.pkl'):
    """训练模拟器"""
    
    print("="*60)
    print("训练盘口模拟器")
    print("="*60)
    print()
    
    # 1. 加载训练数据
    print(f"加载训练数据: {training_data_file}")
    with open(training_data_file, 'rb') as f:
        training_data = pickle.load(f)
    
    print(f"训练样本数: {len(training_data)}")
    
    if len(training_data) < 3:
        print("警告: 训练样本太少，建议至少10条以上")
    
    # 2. 划分训练集和测试集
    split_idx = int(len(training_data) * 0.8)
    train_data = training_data[:split_idx]
    test_data = training_data[split_idx:]
    
    print(f"训练集: {len(train_data)} 条")
    print(f"测试集: {len(test_data)} 条")
    print()
    
    # 3. 训练模拟器
    simulator = KlineBasedOrderBookSimulator()
    simulator.train(train_data)
    
    # 4. 验证
    if test_data:
        print()
        validate_simulator(simulator, test_data)
    
    # 5. 保存
    print()
    simulator.save(output_file)
    
    return simulator


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='训练盘口模拟器')
    parser.add_argument('--training-data', default='training_data.pkl',
                       help='训练数据文件')
    parser.add_argument('--output', default='orderbook_simulator.pkl',
                       help='输出模拟器文件')
    
    args = parser.parse_args()
    
    simulator = train_simulator(
        training_data_file=args.training_data,
        output_file=args.output
    )
    
    print("\n✓ 训练完成")
