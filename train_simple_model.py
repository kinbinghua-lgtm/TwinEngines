#!/usr/bin/env python3
"""
修正模型训练框架 V2.0 - 简化版

核心改动：
1. 用best_ask作为买入价（二元市场）
2. 正确的PnL: 赢=1-ask-fee, 输=-ask-fee
3. 手续费: 0.05 × ask × (1-ask)
4. 去掉修正函数，直接用p_raw计算EV
"""

from __future__ import annotations

import json
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Tuple
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


def polymarket_fee(price: float) -> float:
    """Polymarket taker手续费: 0.05 × p × (1-p)"""
    p = max(0.0, min(1.0, price))
    return 0.05 * p * (1.0 - p)


@dataclass
class TrainingSample:
    """单个训练样本"""
    timestamp: str
    condition_id: str
    p_raw: float
    ask_trend: float      # 顺势方向best_ask（买入价）
    ask_rev: float        # 反转方向best_ask（买入价，近似值）
    bid_rev_size: float
    bid_trend_size: float
    spread_rev: float
    spread_trend: float
    actual_reversal: bool

    def is_high_quality(self, min_depth: float = 10.0, max_spread: float = 0.15) -> bool:
        return (
            self.bid_rev_size >= min_depth and
            self.bid_trend_size >= min_depth and
            self.spread_rev <= max_spread and
            self.spread_trend <= max_spread
        )


class SimpleModel:
    """简化模型：直接用p_raw计算EV，不做修正"""
    
    def __init__(self, min_ev: float = 0.03):
        self.min_ev = min_ev
    
    def calculate_ev(
        self,
        p_rev: float,
        ask_rev: float,
        ask_trend: float,
    ) -> Tuple[float, float]:
        """
        二元市场EV计算
        买入价=best_ask, 赢得1.00, 输得0.00
        
        ev = P(win) × (1 - ask) - P(lose) × ask - fee
        """
        fee_rev = polymarket_fee(ask_rev)
        fee_trend = polymarket_fee(ask_trend)
        
        ev_rev = p_rev * (1.0 - ask_rev) - (1.0 - p_rev) * ask_rev - fee_rev
        ev_trend = (1.0 - p_rev) * (1.0 - ask_trend) - p_rev * ask_trend - fee_trend
        
        return ev_rev, ev_trend
    
    def make_decision(
        self,
        p_raw: float,
        ask_rev: float,
        ask_trend: float,
    ) -> Tuple[str, float]:
        """做出交易决策"""
        ev_rev, ev_trend = self.calculate_ev(p_raw, ask_rev, ask_trend)
        
        if ev_rev > self.min_ev and ev_rev >= ev_trend:
            return 'reversal', ev_rev
        elif ev_trend > self.min_ev:
            return 'trend', ev_trend
        else:
            return 'pass', 0.0
    
    def simulate_trade(self, sample: TrainingSample) -> float:
        """
        模拟单笔交易，返回盈亏（二元市场）
        赢: 1.0 - ask - fee
        输: -ask - fee
        """
        action, ev = self.make_decision(
            sample.p_raw,
            sample.ask_rev,
            sample.ask_trend,
        )
        
        if action == 'pass':
            return 0.0
        
        if action == 'reversal':
            fee = polymarket_fee(sample.ask_rev)
            if sample.actual_reversal:
                return (1.0 - sample.ask_rev) - fee
            else:
                return -sample.ask_rev - fee
        else:  # trend
            fee = polymarket_fee(sample.ask_trend)
            if not sample.actual_reversal:
                return (1.0 - sample.ask_trend) - fee
            else:
                return -sample.ask_trend - fee


class SimpleTrainer:
    """简化训练器：只搜索min_ev阈值"""
    
    def __init__(
        self,
        min_ev_range: List[float] = None,
        min_depth: float = 10.0,
        max_spread: float = 0.15,
        validation_split: float = 0.3
    ):
        self.min_ev_range = min_ev_range or [0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10]
        self.min_depth = min_depth
        self.max_spread = max_spread
        self.validation_split = validation_split
    
    def filter_samples(self, samples: List[TrainingSample]) -> List[TrainingSample]:
        filtered = [
            s for s in samples
            if s.is_high_quality(self.min_depth, self.max_spread)
        ]
        logger.info(
            f"Data filtering: {len(samples)} → {len(filtered)} samples "
            f"(removed {len(samples) - len(filtered)} low-quality)"
        )
        return filtered
    
    def split_data(
        self,
        samples: List[TrainingSample]
    ) -> Tuple[List[TrainingSample], List[TrainingSample]]:
        n_train = int(len(samples) * (1 - self.validation_split))
        train_samples = samples[:n_train]
        val_samples = samples[n_train:]
        logger.info(
            f"Data split: {len(train_samples)} train, {len(val_samples)} validation"
        )
        return train_samples, val_samples
    
    def evaluate(self, samples: List[TrainingSample], min_ev: float) -> dict:
        model = SimpleModel(min_ev)
        
        total_pnl = 0.0
        n_trades = 0
        n_wins = 0
        n_reversal = 0
        n_trend = 0
        
        for sample in samples:
            pnl = model.simulate_trade(sample)
            
            if pnl != 0:
                n_trades += 1
                total_pnl += pnl
                if pnl > 0:
                    n_wins += 1
                
                action, _ = model.make_decision(sample.p_raw, sample.ask_rev, sample.ask_trend)
                if action == 'reversal':
                    n_reversal += 1
                elif action == 'trend':
                    n_trend += 1
        
        win_rate = n_wins / n_trades if n_trades > 0 else 0.0
        avg_pnl = total_pnl / n_trades if n_trades > 0 else 0.0
        
        return {
            'total_pnl': total_pnl,
            'n_trades': n_trades,
            'n_wins': n_wins,
            'win_rate': win_rate,
            'avg_pnl': avg_pnl,
            'n_reversal': n_reversal,
            'n_trend': n_trend
        }
    
    def train(self, samples: List[TrainingSample]) -> SimpleModel:
        logger.info(f"Starting training with {len(samples)} samples")
        
        clean_samples = self.filter_samples(samples)
        train_samples, val_samples = self.split_data(clean_samples)
        
        logger.info(f"Searching min_ev threshold: {len(self.min_ev_range)} values")
        
        best_min_ev = None
        best_val_pnl = -float('inf')
        results = []
        
        for min_ev in self.min_ev_range:
            train_metrics = self.evaluate(train_samples, min_ev)
            val_metrics = self.evaluate(val_samples, min_ev)
            
            result = {
                'min_ev': min_ev,
                'train': train_metrics,
                'val': val_metrics
            }
            results.append(result)
            
            if val_metrics['total_pnl'] > best_val_pnl:
                best_val_pnl = val_metrics['total_pnl']
                best_min_ev = min_ev
            
            logger.info(
                f"min_ev={min_ev:.3f} | "
                f"Train: PnL={train_metrics['total_pnl']:+.2f}, "
                f"trades={train_metrics['n_trades']}, "
                f"win_rate={train_metrics['win_rate']:.2%} | "
                f"Val: PnL={val_metrics['total_pnl']:+.2f}, "
                f"trades={val_metrics['n_trades']}, "
                f"win_rate={val_metrics['win_rate']:.2%}"
            )
        
        logger.info(f"\nBest min_ev: {best_min_ev:.3f} (validation PnL={best_val_pnl:.2f})")
        
        # 保存结果
        output = {
            'best_params': {'min_ev': best_min_ev},
            'results': results
        }
        output_path = Path('reports/simple_model_training_results.json')
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(output, indent=2))
        logger.info(f"Results saved to {output_path}")
        
        return SimpleModel(best_min_ev)
