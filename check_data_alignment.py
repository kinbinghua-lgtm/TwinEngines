#!/usr/bin/env python3
"""
检查训练数据的时间对齐问题
"""

import json
from pathlib import Path
from datetime import datetime
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


def check_data_alignment():
    """检查数据时间对齐"""
    
    samples_file = Path('data_runtime/training_samples.json')
    samples = json.loads(samples_file.read_text())
    
    logger.info("="*60)
    logger.info("数据时间对齐检查")
    logger.info("="*60)
    print()
    
    logger.info("1. 数据来源分析:")
    logger.info("   - p_raw: 来自生存模型（基于币安BTC价格）")
    logger.info("   - 盘口数据: 来自Polymarket")
    logger.info("   - 实际结果: 来自Polymarket结算")
    print()
    
    logger.info("2. 关键问题:")
    logger.info("   ⚠️ p_raw是在窗口第3分钟结束时计算的")
    logger.info("   ⚠️ 但我们提取的盘口数据是什么时刻的？")
    print()
    
    # 检查样本数据结构
    if samples:
        sample = samples[0]
        logger.info("3. 样本数据结构:")
        logger.info(f"   Keys: {list(sample.keys())}")
        logger.info(f"   timestamp: {sample.get('timestamp', 'N/A')}")
        logger.info(f"   window_start_ms: {sample.get('window_start_ms', 'N/A')}")
        logger.info(f"   window_end_ms: {sample.get('window_end_ms', 'N/A')}")
        
        if sample.get('window_start_ms'):
            start_time = datetime.fromtimestamp(sample['window_start_ms'] / 1000)
            end_time = datetime.fromtimestamp(sample['window_end_ms'] / 1000)
            logger.info(f"   窗口开始: {start_time}")
            logger.info(f"   窗口结束: {end_time}")
        print()
    
    logger.info("4. 时间对齐问题诊断:")
    print()
    
    logger.warning("   ⚠️ 潜在问题1: 预测时刻不明确")
    logger.warning("   - 策略日志中的prediction是在什么时刻生成的？")
    logger.warning("   - 是第3分钟结束时？还是其他时刻？")
    logger.warning("   - 如果不是第3分钟，p_raw可能不准确")
    print()
    
    logger.warning("   ⚠️ 潜在问题2: 盘口时刻不明确")
    logger.warning("   - quote_snapshot中的盘口是预测时刻的盘口吗？")
    logger.warning("   - 还是决策下单时刻的盘口？")
    logger.warning("   - 如果时刻不一致，训练会有偏差")
    print()
    
    logger.warning("   ⚠️ 潜在问题3: 币安和Poly时间同步")
    logger.warning("   - 币安价格和Poly盘口是同一时刻的吗？")
    logger.warning("   - 如果有延迟，p_raw和盘口可能不匹配")
    print()
    
    # 检查实际数据
    logger.info("5. 检查实际数据的时间信息:")
    
    # 从策略日志中查找包含prediction的行
    logger.info("   需要检查策略日志中prediction的时间戳...")
    print()
    
    logger.info("6. 建议:")
    logger.info("   ✅ 理想情况：")
    logger.info("      - p_raw: 第3分钟结束时（基于币安价格）")
    logger.info("      - 盘口: 同一时刻的Poly盘口")
    logger.info("      - 时间差: < 1秒")
    print()
    
    logger.info("   📋 需要验证：")
    logger.info("      1. 查看策略日志中prediction的时间戳")
    logger.info("      2. 查看quote_snapshot的时间戳")
    logger.info("      3. 计算两者的时间差")
    logger.info("      4. 确认是否在第3分钟结束时")
    print()


def check_strategy_log_timestamps():
    """检查策略日志中的时间戳"""
    from fabric import Connection
    
    vps_host = "47.243.169.223"
    vps_user = "root"
    vps_password = "Jinbh1977"
    
    logger.info("="*60)
    logger.info("检查策略日志时间戳")
    logger.info("="*60)
    print()
    
    try:
        conn = Connection(
            host=vps_host,
            user=vps_user,
            connect_kwargs={"password": vps_password}
        )
        
        # 查找一个包含prediction的完整JSON
        logger.info("1. 提取一个完整的prediction样本:")
        result = conn.run(
            "grep '\"prediction\"' /root/TwinEngines/logs/strategy_stdout.log | head -1",
            hide=True,
            warn=True
        )
        
        if result.stdout.strip():
            import json
            try:
                data = json.loads(result.stdout.strip())
                
                logger.info("   找到样本！")
                print()
                
                # 提取关键时间信息
                logger.info("2. 时间信息:")
                
                window_start_ms = data.get('window_start_ms')
                if window_start_ms:
                    window_start = datetime.fromtimestamp(window_start_ms / 1000)
                    logger.info(f"   窗口开始时间: {window_start}")
                    logger.info(f"   window_start_ms: {window_start_ms}")
                
                quote = data.get('quote_snapshot', {})
                now_ms = quote.get('now_ms')
                if now_ms:
                    now_time = datetime.fromtimestamp(now_ms / 1000)
                    logger.info(f"   预测时刻: {now_time}")
                    logger.info(f"   now_ms: {now_ms}")
                
                residual_sec = data.get('prediction', {}).get('residual_sec')
                if residual_sec:
                    logger.info(f"   剩余时间: {residual_sec:.1f} 秒")
                
                # 计算预测时刻相对于窗口开始的时间
                if window_start_ms and now_ms:
                    elapsed_sec = (now_ms - window_start_ms) / 1000
                    logger.info(f"   已过时间: {elapsed_sec:.1f} 秒")
                    logger.info(f"   预期: 180秒（第3分钟结束）")
                    
                    if abs(elapsed_sec - 180) < 10:
                        logger.info("   ✅ 时间对齐正确！在第3分钟结束时")
                    else:
                        logger.warning(f"   ⚠️ 时间偏差: {elapsed_sec - 180:.1f} 秒")
                print()
                
                # 检查盘口时间戳
                logger.info("3. 盘口时间戳:")
                book_ts_ms = quote.get('book_ts_ms')
                if book_ts_ms:
                    book_time = datetime.fromtimestamp(book_ts_ms / 1000)
                    logger.info(f"   盘口时间: {book_time}")
                    logger.info(f"   book_ts_ms: {book_ts_ms}")
                    
                    if now_ms:
                        delay_ms = book_ts_ms - now_ms
                        logger.info(f"   盘口延迟: {delay_ms} ms")
                        
                        if abs(delay_ms) < 1000:
                            logger.info("   ✅ 盘口时间同步良好（<1秒）")
                        else:
                            logger.warning(f"   ⚠️ 盘口延迟较大: {delay_ms/1000:.1f} 秒")
                print()
                
                # 显示盘口数据
                logger.info("4. 盘口数据:")
                logger.info(f"   side: {quote.get('side')}")
                logger.info(f"   best_bid: {quote.get('best_bid')}")
                logger.info(f"   best_ask: {quote.get('best_ask')}")
                logger.info(f"   best_bid_size: {quote.get('best_bid_size')}")
                logger.info(f"   best_ask_size: {quote.get('best_ask_size')}")
                print()
                
            except json.JSONDecodeError as e:
                logger.error(f"   JSON解析失败: {e}")
        else:
            logger.warning("   未找到包含prediction的日志")
        
        conn.close()
        
    except Exception as e:
        logger.error(f"错误: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    check_data_alignment()
    print()
    check_strategy_log_timestamps()
