#!/usr/bin/env python3
"""
超简化版盘口模拟器 - 不依赖任何第三方库
只使用Python标准库
"""

import json
import random
import pickle
from pathlib import Path
from typing import List, Dict, Optional


class UltraSimpleOrderBookSimulator:
    """超简化版盘口模拟器（只用标准库）"""
    
    def __init__(self):
        self.spread_by_confidence = {}  # 按置信度分组的spread列表
        self.midpoint_by_confidence = {}  # 按置信度分组的midpoint列表
        self.global_spread_mean = 0.01
        self.global_spread_std = 0.005
        self.is_trained = False
    
    def train(self, real_orderbook_data: List[Dict]):
        """训练模拟器"""
        print(f"训练样本数: {len(real_orderbook_data)}")
        
        if len(real_orderbook_data) == 0:
            print("错误: 没有训练数据")
            return
        
        # 按置信度分组
        for d in real_orderbook_data:
            conf = d.get('confidence', 0.5)
            conf_bin = round(conf, 1)  # 0.5, 0.6, 0.7...
            
            best_ask = float(d['best_ask'])
            best_bid = float(d['best_bid'])
            spread = best_ask - best_bid
            midpoint = (best_ask + best_bid) / 2
            
            if conf_bin not in self.spread_by_confidence:
                self.spread_by_confidence[conf_bin] = []
                self.midpoint_by_confidence[conf_bin] = []
            
            self.spread_by_confidence[conf_bin].append(spread)
            self.midpoint_by_confidence[conf_bin].append(midpoint)
        
        # 计算全局统计
        all_spreads = []
        for spreads in self.spread_by_confidence.values():
            all_spreads.extend(spreads)
        
        self.global_spread_mean = sum(all_spreads) / len(all_spreads)
        
        # 计算标准差
        variance = sum((x - self.global_spread_mean)**2 for x in all_spreads) / len(all_spreads)
        self.global_spread_std = variance ** 0.5
        
        print(f"全局Spread: mean={self.global_spread_mean:.4f}, std={self.global_spread_std:.4f}")
        print(f"置信度分组数: {len(self.spread_by_confidence)}")
        
        for conf_bin in sorted(self.spread_by_confidence.keys()):
            spreads = self.spread_by_confidence[conf_bin]
            mean_spread = sum(spreads) / len(spreads)
            print(f"  Conf {conf_bin}: {len(spreads)}条, spread_mean={mean_spread:.4f}")
        
        self.is_trained = True
        print("\n[OK] 训练完成")
    
    def simulate(self, confidence: float, seed: Optional[int] = None) -> Dict:
        """模拟盘口"""
        if not self.is_trained:
            raise RuntimeError("模拟器未训练")
        
        if seed is not None:
            random.seed(seed)
        
        conf_bin = round(confidence, 1)
        
        # 从对应置信度区间采样
        if conf_bin in self.spread_by_confidence and len(self.spread_by_confidence[conf_bin]) > 0:
            spread = random.choice(self.spread_by_confidence[conf_bin])
            midpoint = random.choice(self.midpoint_by_confidence[conf_bin])
        else:
            # 从最近的区间采样
            nearest_bin = min(self.spread_by_confidence.keys(), 
                            key=lambda x: abs(x - conf_bin))
            spread = random.choice(self.spread_by_confidence[nearest_bin])
            midpoint = random.choice(self.midpoint_by_confidence[nearest_bin])
        
        # 添加小噪声
        spread += random.gauss(0, self.global_spread_std * 0.2)
        midpoint += random.gauss(0, 0.01)
        
        # 确保合理范围
        spread = max(0.001, min(spread, 0.1))
        midpoint = max(0.01, min(midpoint, 0.99))
        
        # 计算Bid和Ask
        best_ask = midpoint + spread / 2
        best_bid = midpoint - spread / 2
        
        best_ask = max(0.01, min(best_ask, 0.99))
        best_bid = max(0.01, min(best_bid, best_ask - 0.001))
        
        return {
            'best_ask': round(best_ask, 4),
            'best_bid': round(best_bid, 4),
            'spread': round(spread, 4),
            'midpoint': round(midpoint, 4),
        }
    
    def save(self, path: str):
        """保存模拟器"""
        with open(path, 'wb') as f:
            pickle.dump(self, f)
        print(f"模拟器已保存: {path}")
    
    @classmethod
    def load(cls, path: str):
        """加载模拟器"""
        with open(path, 'rb') as f:
            return pickle.load(f)


def extract_orderbook_data(jsonl_files):
    """从JSONL文件提取盘口数据"""
    
    print("="*60)
    print("提取盘口数据")
    print("="*60)
    print()
    
    data = []
    
    for file_path in jsonl_files:
        if not Path(file_path).exists():
            continue
        
        print(f"读取: {file_path}")
        count = 0
        
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                
                try:
                    obj = json.loads(line)
                    
                    # 提取盘口
                    poly = obj.get('polymarket', {})
                    book = poly.get('book', {})
                    
                    best_ask = book.get('best_ask')
                    best_bid = book.get('best_bid')
                    
                    if best_ask and best_bid:
                        ask = float(best_ask)
                        bid = float(best_bid)
                        
                        if 0 < ask <= 1 and 0 < bid <= 1 and bid < ask:
                            conf = obj.get('confidence') or obj.get('confidence_on_pick', 0.5)
                            
                            data.append({
                                'best_ask': ask,
                                'best_bid': bid,
                                'confidence': float(conf),
                            })
                            count += 1
                except:
                    pass
        
        print(f"  提取 {count} 条")
    
    print(f"\n总计: {len(data)} 条")
    return data


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("用法:")
        print("  训练: python ultra_simple_simulator.py train <file1> [file2] ...")
        print("  测试: python ultra_simple_simulator.py test <simulator.pkl>")
        sys.exit(1)
    
    mode = sys.argv[1]
    
    if mode == 'train':
        files = sys.argv[2:]
        if not files:
            print("错误: 请指定输入文件")
            sys.exit(1)
        
        data = extract_orderbook_data(files)
        
        if not data:
            print("错误: 没有数据")
            sys.exit(1)
        
        simulator = UltraSimpleOrderBookSimulator()
        simulator.train(data)
        simulator.save('ultra_simple_simulator.pkl')
        
        print(f"\n[OK] 训练完成，已保存到: ultra_simple_simulator.pkl")
    
    elif mode == 'test':
        if len(sys.argv) < 3:
            print("错误: 请指定模拟器文件")
            sys.exit(1)
        
        simulator_file = sys.argv[2]
        simulator = UltraSimpleOrderBookSimulator.load(simulator_file)
        
        print("\n测试模拟:")
        for conf in [0.50, 0.55, 0.60, 0.65, 0.70]:
            book = simulator.simulate(confidence=conf, seed=42)
            print(f"  Conf={conf:.2f}: ask={book['best_ask']:.4f}, "
                  f"bid={book['best_bid']:.4f}, spread={book['spread']:.4f}")
    
    else:
        print(f"错误: 未知模式 '{mode}'")
        sys.exit(1)
