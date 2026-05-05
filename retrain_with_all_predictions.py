#!/usr/bin/env python3
"""
使用所有预测数据重新训练修正模型
"""

import json
from pathlib import Path
import logging
from train_correction_model import TrainingSample, CorrectionModelTrainer

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


def load_all_predictions(json_path='data_runtime/all_predictions.json'):
    """加载所有预测数据"""
    
    predictions_file = Path(json_path)
    predictions = json.loads(predictions_file.read_text())
    
    logger.info(f"加载了 {len(predictions)} 个预测样本")
    
    # 转换为TrainingSample
    training_samples = []
    
    for pred in predictions:
        bid_rev_size = pred.get('bid_rev_size') or 0.0
        bid_trend_size = pred.get('bid_trend_size') or 0.0
        
        sample = TrainingSample(
            timestamp=f"{pred['elapsed_sec']:.1f}s",
            condition_id=pred['condition_id'],
            p_raw=pred['p_raw'],
            bid_rev=pred['bid_rev'],
            bid_trend=pred['bid_trend'],
            bid_rev_size=bid_rev_size,
            bid_trend_size=bid_trend_size,
            spread_rev=pred.get('spread_rev') or 0.01,
            spread_trend=pred.get('spread_trend') or 0.01,
            actual_reversal=pred['actual_reversal']
        )
        training_samples.append(sample)
    
    # 统计
    n_reversal = sum(1 for s in training_samples if s.actual_reversal)
    n_trend = len(training_samples) - n_reversal
    
    logger.info(f"  实际反转: {n_reversal} ({n_reversal/len(training_samples)*100:.1f}%)")
    logger.info(f"  实际顺势: {n_trend} ({n_trend/len(training_samples)*100:.1f}%)")
    
    return training_samples


if __name__ == "__main__":
    logger.info("="*60)
    logger.info("使用所有预测数据重新训练修正模型")
    logger.info("="*60)
    print()
    
    # 1. 加载数据
    samples = load_all_predictions()
    
    print()
    
    # 2. 创建训练器
    trainer = CorrectionModelTrainer(
        k_range=[0.3, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0],
        m_range=[0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0],
        min_ev=0.03,
        transaction_cost=0.01,
        min_depth=10.0,
        max_spread=0.15,
        validation_split=0.3
    )
    
    # 3. 训练
    model = trainer.train(samples)
    
    print()
    logger.info("="*60)
    logger.info("训练完成！")
    logger.info("="*60)
    logger.info(f"\n最优参数:")
    logger.info(f"  k = {model.k:.2f}")
    logger.info(f"  m = {model.m:.2f}")
    logger.info(f"  min_ev = {model.min_ev:.3f}")
    
    # 4. 保存模型参数
    model_params = {
        'k': model.k,
        'm': model.m,
        'min_ev': model.min_ev
    }
    
    params_file = Path('data_runtime/correction_model_params_v2.json')
    params_file.write_text(json.dumps(model_params, indent=2))
    logger.info(f"\n模型参数已保存到: {params_file}")
