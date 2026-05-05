#!/usr/bin/env python3
"""
获取窗口的实际结果

从Polymarket API或历史数据中获取每个窗口的实际结果
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List, Optional
import logging
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


def fetch_market_result(condition_id: str) -> Optional[dict]:
    """
    从Polymarket API获取市场结果
    
    Returns:
        {
            'outcome': 'Up' or 'Down',
            'resolved': True/False
        }
    """
    try:
        # Polymarket CLOB API
        url = f"https://clob.polymarket.com/markets/{condition_id}"
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            
            # 检查是否已关闭
            closed = data.get('closed', False)
            
            if not closed:
                return {'outcome': None, 'resolved': False}
            
            # 从tokens中找到winner
            tokens = data.get('tokens', [])
            winner_outcome = None
            
            for token in tokens:
                if token.get('winner'):
                    winner_outcome = token.get('outcome')
                    break
            
            return {
                'outcome': winner_outcome,  # 'Up' or 'Down'
                'resolved': closed and winner_outcome is not None
            }
        else:
            logger.warning(f"Failed to fetch market {condition_id[:20]}...: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"Error fetching market {condition_id[:20]}...: {e}")
        return None


def update_samples_with_results(samples_path: str = "data_runtime/training_samples.json"):
    """更新样本的实际结果"""
    
    samples_file = Path(samples_path)
    if not samples_file.exists():
        logger.error(f"Samples file not found: {samples_path}")
        return
    
    # 读取样本
    samples = json.loads(samples_file.read_text())
    logger.info(f"Loaded {len(samples)} samples")
    
    # 统计
    updated_count = 0
    resolved_count = 0
    
    for i, sample in enumerate(samples, 1):
        cid = sample['condition_id']
        
        # 跳过已有结果的样本
        if sample.get('actual_reversal') is not None:
            resolved_count += 1
            continue
        
        logger.info(f"[{i}/{len(samples)}] Fetching result for {cid[:20]}...")
        
        # 获取市场结果
        result = fetch_market_result(cid)
        
        if result and result['resolved']:
            # 判断是否反转
            # 如果预测UP，实际结果是Down → 反转
            # 如果预测DOWN，实际结果是Up → 反转
            pred_side = sample['prediction_side']
            outcome = result['outcome']
            
            if pred_side == 'UP':
                # 预测UP，实际Down → 反转
                actual_reversal = (outcome == 'Down')
            else:
                # 预测DOWN，实际Up → 反转
                actual_reversal = (outcome == 'Up')
            
            sample['actual_reversal'] = actual_reversal
            updated_count += 1
            resolved_count += 1
            
            logger.info(f"  Result: {outcome}, Reversal: {actual_reversal}")
        else:
            logger.info(f"  Market not resolved yet")
        
        # 避免请求过快
        time.sleep(0.5)
    
    # 保存更新后的样本
    samples_file.write_text(json.dumps(samples, indent=2, ensure_ascii=False))
    
    logger.info(f"\nUpdate complete:")
    logger.info(f"  Total samples: {len(samples)}")
    logger.info(f"  Resolved: {resolved_count}")
    logger.info(f"  Newly updated: {updated_count}")
    logger.info(f"  Pending: {len(samples) - resolved_count}")


if __name__ == "__main__":
    update_samples_with_results()
