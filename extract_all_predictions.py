#!/usr/bin/env python3
"""
重新提取训练数据 - 提取所有预测时刻
"""

from fabric import Connection
import json
from pathlib import Path
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


def extract_all_predictions():
    """从VPS提取所有预测时刻的数据"""
    
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    logger.info("连接VPS并提取所有预测数据...")
    
    conn = Connection(
        host=vps_host,
        user=vps_user,
        connect_kwargs={"password": vps_password}
    )
    
    # 提取所有包含prediction和quote_snapshot的日志
    logger.info("下载策略日志...")
    result = conn.run(
        'grep quote_snapshot /root/TwinEngines/logs/strategy_stdout.log | grep prediction',
        hide=True,
        warn=True
    )
    
    lines = result.stdout.strip().split('\n')
    logger.info(f"找到 {len(lines)} 条预测记录")
    
    # 解析每条记录
    predictions = []
    
    for i, line in enumerate(lines, 1):
        try:
            data = json.loads(line)
            
            # 提取关键信息
            condition_id = data['condition_id']
            window_start_ms = data['window_start_ms']
            
            quote = data['quote_snapshot']
            now_ms = quote['now_ms']
            
            prediction = data['prediction']
            p_rev = prediction['p_rev']
            residual_sec = prediction['residual_sec']
            
            # 计算已过时间
            elapsed_sec = (now_ms - window_start_ms) / 1000
            
            # 提取盘口
            side = quote['side']
            best_bid = quote['best_bid']
            best_ask = quote['best_ask']
            best_bid_size = quote.get('best_bid_size', 0)
            best_ask_size = quote.get('best_ask_size', 0)
            
            # 计算两个方向的盘口
            if side == 'UP':
                # 预测UP，反转=DOWN
                bid_rev = 1 - best_ask  # DOWN的买价
                bid_trend = best_bid    # UP的买价
                bid_rev_size = best_ask_size or 0.0
                bid_trend_size = best_bid_size or 0.0
            else:
                # 预测DOWN，反转=UP
                bid_rev = 1 - best_ask  # UP的买价
                bid_trend = best_bid    # DOWN的买价
                bid_rev_size = best_ask_size or 0.0
                bid_trend_size = best_bid_size or 0.0
            
            predictions.append({
                'condition_id': condition_id,
                'window_start_ms': window_start_ms,
                'now_ms': now_ms,
                'elapsed_sec': elapsed_sec,
                'residual_sec': residual_sec,
                'p_raw': p_rev,
                'prediction_side': side,
                'bid_rev': bid_rev,
                'bid_trend': bid_trend,
                'bid_rev_size': bid_rev_size,
                'bid_trend_size': bid_trend_size,
                'spread_rev': 0.01,  # 简化
                'spread_trend': 0.01,
                'actual_reversal': None  # 稍后填充
            })
            
            if i % 100 == 0:
                logger.info(f"  已处理 {i}/{len(lines)} 条记录...")
                
        except Exception as e:
            logger.warning(f"  跳过第{i}行: {e}")
            continue
    
    logger.info(f"成功提取 {len(predictions)} 个预测样本")
    
    # 按窗口分组
    windows = {}
    for pred in predictions:
        cid = pred['condition_id']
        if cid not in windows:
            windows[cid] = []
        windows[cid].append(pred)
    
    logger.info(f"涉及 {len(windows)} 个不同窗口")
    
    # 统计每个窗口的预测次数
    pred_counts = [len(preds) for preds in windows.values()]
    logger.info(f"每个窗口平均预测次数: {sum(pred_counts)/len(pred_counts):.1f}")
    logger.info(f"最多预测次数: {max(pred_counts)}")
    logger.info(f"最少预测次数: {min(pred_counts)}")
    
    conn.close()
    
    return predictions, windows


def fetch_actual_results(predictions, windows):
    """获取实际结果"""
    import requests
    import time
    
    logger.info("获取实际结果...")
    
    # 为每个窗口获取结果
    results = {}
    
    for i, cid in enumerate(windows.keys(), 1):
        logger.info(f"[{i}/{len(windows)}] 获取 {cid[:20]}... 的结果")
        
        try:
            url = f"https://clob.polymarket.com/markets/{cid}"
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                
                if data.get('closed'):
                    tokens = data.get('tokens', [])
                    for token in tokens:
                        if token.get('winner'):
                            outcome = token.get('outcome')
                            results[cid] = outcome
                            logger.info(f"  结果: {outcome}")
                            break
            
            time.sleep(0.5)
            
        except Exception as e:
            logger.warning(f"  获取失败: {e}")
    
    logger.info(f"成功获取 {len(results)}/{len(windows)} 个窗口的结果")
    
    # 填充actual_reversal
    for pred in predictions:
        cid = pred['condition_id']
        if cid in results:
            outcome = results[cid]
            side = pred['prediction_side']
            
            # 判断是否反转
            if side == 'UP':
                pred['actual_reversal'] = (outcome == 'Down')
            else:
                pred['actual_reversal'] = (outcome == 'Up')
    
    # 过滤掉没有结果的样本
    complete_predictions = [p for p in predictions if p['actual_reversal'] is not None]
    logger.info(f"完整样本数: {len(complete_predictions)}")
    
    return complete_predictions


def save_predictions(predictions, output_path='data_runtime/all_predictions.json'):
    """保存预测数据"""
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    
    output_file.write_text(json.dumps(predictions, indent=2, ensure_ascii=False))
    logger.info(f"数据已保存到 {output_path}")


if __name__ == "__main__":
    logger.info("="*60)
    logger.info("重新提取所有预测数据")
    logger.info("="*60)
    print()
    
    # 1. 提取所有预测
    predictions, windows = extract_all_predictions()
    
    print()
    
    # 2. 获取实际结果
    complete_predictions = fetch_actual_results(predictions, windows)
    
    print()
    
    # 3. 保存数据
    save_predictions(complete_predictions)
    
    print()
    logger.info("="*60)
    logger.info("数据提取完成！")
    logger.info("="*60)
    logger.info(f"总样本数: {len(complete_predictions)}")
    logger.info(f"涉及窗口: {len(set(p['condition_id'] for p in complete_predictions))}")
