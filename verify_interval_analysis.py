#!/usr/bin/env python3
"""
验证 Claude 的区间分析结论：
1. 反转 0.02-0.05 是否真的亏钱
2. 3个核心区间是否真的赚钱
3. 使用与训练代码一致的模拟逻辑

注意：TrainingSample 的字段是 ask_trend 和 ask_rev，
但 run_correction_training.py 中实际赋值的是 bid_rev 和 bid_trend（买入价）。
这里我们保持与 run_correction_training.py 一致。
"""

import json
import sys
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))

from train_correction_model import (
    TrainingSample,
    CorrectionModel,
    polymarket_fee,
)


def load_samples():
    """加载样本，与 run_correction_training.py 一致"""
    json_path = "data_runtime/training_samples.json"
    data = json.loads(Path(json_path).read_text())
    
    samples = []
    for item in data:
        if item.get('actual_reversal') is None:
            continue
        
        if item['prediction_side'] == 'UP':
            # 预测UP，反转=DOWN
            # 注意：这里用的是 bid_rev/bid_trend 赋值给 ask_rev/ask_trend
            # 这是 run_correction_training.py 中的做法
            ask_rev = item['bid_down']      # 反转方向买入价
            ask_trend = item['bid_up']      # 顺势方向买入价
            bid_rev_size = item.get('bid_down_size', 0.0) or 0.0
            bid_trend_size = item.get('bid_up_size', 0.0) or 0.0
            ask_down = item.get('ask_down')
            ask_up = item.get('ask_up')
            spread_rev = abs(ask_down - ask_rev) if ask_down and ask_rev else 0.0
            spread_trend = abs(ask_up - ask_trend) if ask_up and ask_trend else 0.0
        else:
            # 预测DOWN，反转=UP
            ask_rev = item['bid_up']        # 反转方向买入价
            ask_trend = item['bid_down']    # 顺势方向买入价
            bid_rev_size = item.get('bid_up_size', 0.0) or 0.0
            bid_trend_size = item.get('bid_down_size', 0.0) or 0.0
            ask_up = item.get('ask_up')
            ask_down = item.get('ask_down')
            spread_rev = abs(ask_up - ask_rev) if ask_up and ask_rev else 0.0
            spread_trend = abs(ask_down - ask_trend) if ask_down and ask_trend else 0.0
        
        sample = TrainingSample(
            timestamp=item.get('timestamp', ''),
            condition_id=item['condition_id'],
            p_raw=item['p_raw'],
            ask_trend=ask_trend,
            ask_rev=ask_rev,
            bid_rev_size=bid_rev_size,
            bid_trend_size=bid_trend_size,
            spread_rev=spread_rev,
            spread_trend=spread_trend,
            actual_reversal=item['actual_reversal']
        )
        samples.append(sample)
    
    return samples


def analyze_by_p_rev_bucket_v2(samples, model, label=""):
    """
    直接从模型决策结果中按 p_rev 分桶
    """
    print(f"\n\n{'='*80}")
    print(f"{label}")
    print(f"{'='*80}")
    
    # 过滤掉 ask_rev 或 ask_trend 为 None 的样本
    valid_samples = [s for s in samples if s.ask_rev is not None and s.ask_trend is not None]
    print(f"\n有效样本: {len(valid_samples)}/{len(samples)} (过滤了 {len(samples)-len(valid_samples)} 个 None 值)")
    
    # 对每个样本做决策，记录结果
    trade_records = []
    for s in valid_samples:
        action, ev = model.make_decision(s.p_raw, s.ask_rev, s.ask_trend)
        if action == 'pass':
            continue
        
        pnl = model.simulate_trade(s)
        
        trade_records.append({
            "p_raw": s.p_raw,
            "action": action,
            "pnl": pnl,
            "actual_reversal": s.actual_reversal,
            "ask_rev": s.ask_rev,
            "ask_trend": s.ask_trend,
        })
    
    print(f"\n总交易数: {len(trade_records)}")
    
    # 按 p_rev 分桶
    rev_buckets = [(0.0, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.15), 
                   (0.15, 0.20), (0.20, 0.30), (0.30, 0.50), (0.50, 0.60),
                   (0.60, 0.70), (0.70, 0.80), (0.80, 1.0)]
    
    print(f"\n{'='*80}")
    print(f"{'p_rev区间':<15} {'方向':>6} {'笔数':>6} {'胜率':>8} {'PnL':>8} {'平均PnL':>10}")
    print(f"{'-'*80}")
    
    total_pnl = 0
    total_trades = 0
    total_wins = 0
    
    for lo, hi in rev_buckets:
        for action_type in ['reversal', 'trend']:
            bucket = [t for t in trade_records 
                      if lo <= t["p_raw"] < hi and t["action"] == action_type]
            if not bucket:
                continue
            
            n = len(bucket)
            pnl = sum(t["pnl"] for t in bucket)
            wins = sum(1 for t in bucket if t["pnl"] > 0)
            win_rate = wins / n * 100
            avg_pnl = pnl / n
            
            label_str = f"{action_type[:4]} {lo:.2f}-{hi:.2f}"
            print(f"{label_str:<15} {action_type[:4]:>6} {n:>6} {win_rate:>7.1f}% {pnl:>+8.2f} {avg_pnl:>+10.3f}")
            
            total_pnl += pnl
            total_trades += n
            total_wins += wins
    
    print(f"{'-'*80}")
    print(f"{'合计':<15} {'':>6} {total_trades:>6} {total_wins/total_trades*100:>7.1f}% {total_pnl:>+8.2f}")
    
    return trade_records


def analyze_raw_accuracy(samples):
    """
    分析原始 p_raw 的准确率（不经过修正模型）
    按 p_rev 区间看实际反转率
    """
    print(f"\n\n{'='*80}")
    print("原始 p_raw 准确率分析（不经过修正模型）")
    print(f"{'='*80}")
    
    rev_buckets = [(0.0, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.15), 
                   (0.15, 0.20), (0.20, 0.30), (0.30, 0.50), (0.50, 0.60),
                   (0.60, 0.70), (0.70, 0.80), (0.80, 1.0)]
    
    print(f"\n{'='*80}")
    print(f"{'p_rev区间':<15} {'样本数':>6} {'反转数':>6} {'反转率':>8} {'顺势数':>6} {'顺势率':>8}")
    print(f"{'-'*80}")
    
    for lo, hi in rev_buckets:
        bucket = [s for s in samples if lo <= s.p_raw < hi]
        if not bucket:
            continue
        
        n = len(bucket)
        n_rev = sum(1 for s in bucket if s.actual_reversal)
        n_trend = n - n_rev
        rev_rate = n_rev / n * 100
        trend_rate = n_trend / n * 100
        
        print(f"{f'{lo:.2f}-{hi:.2f}':<15} {n:>6} {n_rev:>6} {rev_rate:>7.1f}% {n_trend:>6} {trend_rate:>7.1f}%")


def analyze_ev_threshold_impact(samples):
    """
    分析不同 EV 阈值对交易分布的影响
    """
    print(f"\n\n{'='*80}")
    print("EV 阈值影响分析")
    print(f"{'='*80}")
    
    valid_samples = [s for s in samples if s.ask_rev is not None and s.ask_trend is not None]
    
    for min_ev in [0.01, 0.02, 0.03, 0.05, 0.08]:
        model = CorrectionModel(k=2.5, m=0.5, min_ev=min_ev)
        
        n_trades = 0
        n_rev = 0
        n_trend = 0
        total_pnl = 0
        
        for s in valid_samples:
            pnl = model.simulate_trade(s)
            if pnl != 0:
                n_trades += 1
                total_pnl += pnl
                action, _ = model.make_decision(s.p_raw, s.ask_rev, s.ask_trend)
                if action == 'reversal':
                    n_rev += 1
                else:
                    n_trend += 1
        
        print(f"  min_ev={min_ev:.2f}: {n_trades} 笔交易, PnL={total_pnl:+.2f}, "
              f"反转={n_rev}, 顺势={n_trend}")


def main():
    print("="*80)
    print("验证 Claude 的区间分析结论")
    print("="*80)
    
    # 加载样本
    samples = load_samples()
    print(f"\n加载样本: {len(samples)} 个")
    
    # 统计基本信息
    n_rev = sum(1 for s in samples if s.actual_reversal)
    n_trend = len(samples) - n_rev
    print(f"  反转: {n_rev} ({n_rev/len(samples)*100:.1f}%)")
    print(f"  顺势: {n_trend} ({n_trend/len(samples)*100:.1f}%)")
    
    # 1. 原始准确率分析
    analyze_raw_accuracy(samples)
    
    # 2. 使用 v2 参数 (k=2.5, m=0.5)
    model_v2 = CorrectionModel(k=2.5, m=0.5, min_ev=0.03)
    analyze_by_p_rev_bucket_v2(samples, model_v2, "使用 v2 参数 (k=2.5, m=0.5)")
    
    # 3. 使用 v1 参数 (k=0.3, m=0.0)
    model_v1 = CorrectionModel(k=0.3, m=0.0, min_ev=0.03)
    analyze_by_p_rev_bucket_v2(samples, model_v1, "使用 v1 参数 (k=0.3, m=0.0)")
    
    # 4. 无修正模型 (k=0, m=0)
    model_raw = CorrectionModel(k=0.0, m=0.0, min_ev=0.03)
    analyze_by_p_rev_bucket_v2(samples, model_raw, "无修正 (k=0, m=0)")
    
    # 5. EV 阈值影响
    analyze_ev_threshold_impact(samples)
    
    # 6. 数据质量检查
    print(f"\n\n{'='*80}")
    print("数据质量检查")
    print(f"{'='*80}")
    
    none_ask_rev = sum(1 for s in samples if s.ask_rev is None)
    none_ask_trend = sum(1 for s in samples if s.ask_trend is None)
    print(f"ask_rev 为 None: {none_ask_rev}/{len(samples)}")
    print(f"ask_trend 为 None: {none_ask_trend}/{len(samples)}")
    
    # 检查 p_raw 分布
    p_raws = [s.p_raw for s in samples]
    print(f"\np_raw 分布:")
    print(f"  范围: {min(p_raws):.4f} - {max(p_raws):.4f}")
    print(f"  均值: {sum(p_raws)/len(p_raws):.4f}")
    print(f"  中位数: {sorted(p_raws)[len(p_raws)//2]:.4f}")
    
    # 检查 ask_rev 分布
    ask_revs = [s.ask_rev for s in samples if s.ask_rev is not None]
    print(f"\nask_rev (反转方向价格) 分布:")
    print(f"  范围: {min(ask_revs):.4f} - {max(ask_revs):.4f}")
    print(f"  均值: {sum(ask_revs)/len(ask_revs):.4f}")
    
    # 检查 ask_trend 分布
    ask_trends = [s.ask_trend for s in samples if s.ask_trend is not None]
    print(f"\nask_trend (顺势方向价格) 分布:")
    print(f"  范围: {min(ask_trends):.4f} - {max(ask_trends):.4f}")
    print(f"  均值: {sum(ask_trends)/len(ask_trends):.4f}")


if __name__ == "__main__":
    main()
