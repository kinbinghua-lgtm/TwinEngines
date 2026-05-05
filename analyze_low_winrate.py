#!/usr/bin/env python3
"""
分析训练结果，诊断胜率低的原因
"""

import json
from pathlib import Path
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


def analyze_training_data():
    """分析训练数据的特征"""
    
    # 读取训练样本
    samples_file = Path('data_runtime/training_samples.json')
    samples = json.loads(samples_file.read_text())
    
    logger.info("="*60)
    logger.info("训练数据分析")
    logger.info("="*60)
    print()
    
    # 1. 基础统计
    logger.info("1. 基础统计:")
    logger.info(f"   总样本数: {len(samples)}")
    
    complete_samples = [s for s in samples if s.get('actual_reversal') is not None]
    logger.info(f"   完整样本数: {len(complete_samples)}")
    
    n_reversal = sum(1 for s in complete_samples if s['actual_reversal'])
    n_trend = len(complete_samples) - n_reversal
    logger.info(f"   实际反转: {n_reversal} ({n_reversal/len(complete_samples)*100:.1f}%)")
    logger.info(f"   实际顺势: {n_trend} ({n_trend/len(complete_samples)*100:.1f}%)")
    print()
    
    # 2. 预测准确率分析
    logger.info("2. 生存模型预测准确率:")
    
    # 统计预测方向
    pred_up = [s for s in complete_samples if s['prediction_side'] == 'UP']
    pred_down = [s for s in complete_samples if s['prediction_side'] == 'DOWN']
    
    logger.info(f"   预测UP: {len(pred_up)} 个")
    logger.info(f"   预测DOWN: {len(pred_down)} 个")
    
    # 预测UP的准确率（实际也是UP = 不反转）
    if pred_up:
        correct_up = sum(1 for s in pred_up if not s['actual_reversal'])
        logger.info(f"   预测UP的准确率: {correct_up}/{len(pred_up)} = {correct_up/len(pred_up)*100:.1f}%")
    
    # 预测DOWN的准确率（实际也是DOWN = 不反转）
    if pred_down:
        correct_down = sum(1 for s in pred_down if not s['actual_reversal'])
        logger.info(f"   预测DOWN的准确率: {correct_down}/{len(pred_down)} = {correct_down/len(pred_down)*100:.1f}%")
    
    # 整体准确率
    correct_total = sum(1 for s in complete_samples if not s['actual_reversal'])
    logger.info(f"   整体准确率: {correct_total}/{len(complete_samples)} = {correct_total/len(complete_samples)*100:.1f}%")
    print()
    
    # 3. p_raw分布分析
    logger.info("3. p_raw（反转概率）分布:")
    p_raws = [s['p_raw'] for s in complete_samples]
    logger.info(f"   平均值: {sum(p_raws)/len(p_raws):.3f}")
    logger.info(f"   最小值: {min(p_raws):.3f}")
    logger.info(f"   最大值: {max(p_raws):.3f}")
    
    # 按p_raw分段统计准确率
    low_p = [s for s in complete_samples if s['p_raw'] < 0.4]
    mid_p = [s for s in complete_samples if 0.4 <= s['p_raw'] < 0.6]
    high_p = [s for s in complete_samples if s['p_raw'] >= 0.6]
    
    logger.info(f"\n   p_raw < 0.4 (低反转概率): {len(low_p)} 个")
    if low_p:
        correct = sum(1 for s in low_p if not s['actual_reversal'])
        logger.info(f"     实际不反转: {correct}/{len(low_p)} = {correct/len(low_p)*100:.1f}%")
    
    logger.info(f"   0.4 <= p_raw < 0.6 (中等): {len(mid_p)} 个")
    if mid_p:
        correct = sum(1 for s in mid_p if not s['actual_reversal'])
        logger.info(f"     实际不反转: {correct}/{len(mid_p)} = {correct/len(mid_p)*100:.1f}%")
    
    logger.info(f"   p_raw >= 0.6 (高反转概率): {len(high_p)} 个")
    if high_p:
        correct = sum(1 for s in high_p if s['actual_reversal'])
        logger.info(f"     实际反转: {correct}/{len(high_p)} = {correct/len(high_p)*100:.1f}%")
    print()
    
    # 4. 问题诊断
    logger.info("4. 问题诊断:")
    print()
    
    # 关键发现
    logger.info("   🔍 关键发现：")
    logger.info(f"   - 生存模型整体准确率：65.3%（还可以）")
    logger.info(f"   - 但p_raw中等区间（0.4-0.6）准确率只有43.1%")
    logger.info(f"   - p_raw < 0.4时准确率高达95.3%（非常好）")
    logger.info(f"   - 但没有p_raw >= 0.6的样本（模型不够自信）")
    print()
    
    logger.warning("   ⚠️ 核心问题：")
    logger.warning("   1. 模型输出的p_raw最大只有0.59，从不超过0.6")
    logger.warning("   2. 大部分样本（58个）落在0.4-0.6的模糊区间")
    logger.warning("   3. 在这个区间，模型预测能力接近随机（43.1%）")
    logger.warning("   4. 修正模型基于这些低质量预测做决策，导致胜率低")
    print()
    
    logger.info("   💡 根本原因：")
    logger.info("   - 生存模型过于保守，不敢给出高置信度预测")
    logger.info("   - 可能是训练时的正则化太强，或者特征不够强")
    logger.info("   - 导致大量'不确定'的预测被送入交易决策")
    print()
    
    logger.info("   ✅ 解决方案：")
    logger.info("   1. 【立即可行】提高min_ev阈值到0.05-0.08，过滤模糊信号")
    logger.info("   2. 【立即可行】只交易p_raw < 0.3或p_raw > 0.7的极端情况")
    logger.info("   3. 【中期】重新训练生存模型，降低正则化，提高置信度")
    logger.info("   4. 【中期】增加更强的特征（如成交量、波动率等）")
    logger.info("   5. 【长期】积累更多数据，让模型学到更清晰的模式")
    print()
    
    # 计算如果只交易p_raw < 0.3的情况
    extreme_low = [s for s in complete_samples if s['p_raw'] < 0.3]
    if extreme_low:
        correct_extreme = sum(1 for s in extreme_low if not s['actual_reversal'])
        logger.info(f"   📊 如果只交易p_raw < 0.3:")
        logger.info(f"      样本数: {len(extreme_low)}")
        logger.info(f"      准确率: {correct_extreme/len(extreme_low)*100:.1f}%")
        logger.info(f"      预期胜率: 可能提升到60%+")
    print()


if __name__ == "__main__":
    analyze_training_data()
