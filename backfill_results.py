# -*- coding: utf-8 -*-
import json
import requests
from datetime import datetime

def get_binance_klines(symbol, interval, start_time, end_time):
    '''从Binance获取K线数据'''
    url = 'https://api.binance.com/api/v3/klines'
    params = {
        'symbol': symbol,
        'interval': interval,
        'startTime': start_time,
        'endTime': end_time,
        'limit': 10
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            return response.json()
        else:
            print(f'Binance API error: {response.status_code}')
            return None
    except Exception as e:
        print(f'Request error: {e}')
        return None

def get_window_result(window_id, trigger_direction, baseline_price):
    '''
    查询窗口的最终结果
    
    window_id: 例如 "w1777623900000"
    trigger_direction: "up" 或 "down"
    baseline_price: 基准价格
    
    返回: True (反转了) 或 False (没反转)
    '''
    try:
        # 解析window_id
        window_start_ms = int(window_id[1:])
        window_end_ms = window_start_ms + 5 * 60 * 1000  # 5分钟窗口
        
        # 获取K线数据
        klines = get_binance_klines(
            symbol='BTCUSDT',
            interval='1m',
            start_time=window_start_ms,
            end_time=window_end_ms + 60000
        )
        
        if not klines or len(klines) == 0:
            return None
        
        # 获取窗口结束时的价格
        final_kline = klines[-1]
        final_price = float(final_kline[4])  # close price
        
        # 判断是否反转
        if trigger_direction == 'down':
            # 预测会反转向上
            reversal = final_price > baseline_price
        else:  # 'up'
            # 预测会反转向下
            reversal = final_price < baseline_price
        
        return reversal
        
    except Exception as e:
        print(f'Error getting result for {window_id}: {e}')
        return None

# 读取原始数据
records = []
with open('logs/shadow_signals.jsonl', 'r') as f:
    for line in f:
        records.append(json.loads(line.strip()))

print(f'Total records: {len(records)}')

# 补充结果
updated = 0
for i, record in enumerate(records, 1):
    window_id = record.get('window_id')
    trigger_direction = record.get('trigger_direction')
    baseline_price = record.get('baseline_price')
    
    if not window_id or not trigger_direction or not baseline_price:
        continue
    
    print(f'[{i}/{len(records)}] Checking {window_id}...')
    
    # 查询结果
    result = get_window_result(window_id, trigger_direction, baseline_price)
    
    if result is not None:
        record['final_reversal'] = result
        updated += 1
        print(f'  -> {trigger_direction} baseline={baseline_price:.2f} reversal={result}')
    else:
        print(f'  -> Failed to get result')

print(f'\nUpdated {updated}/{len(records)} records')

# 保存
with open('logs/shadow_signals_with_results.jsonl', 'w') as f:
    for record in records:
        f.write(json.dumps(record) + '\n')

print('Saved to logs/shadow_signals_with_results.jsonl')
