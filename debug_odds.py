import json
from pathlib import Path

predictions = json.loads(Path('data_runtime/all_predictions.json').read_text())

# 找那几笔异常的
for p in predictions:
    if p.get('bid_trend') is not None and p['bid_trend'] < 0.05 and p['actual_reversal'] is not None:
        print(f"CID: {p['condition_id'][:16]}")
        print(f"  prediction_side: {p['prediction_side']}")
        print(f"  p_raw: {p['p_raw']:.3f}")
        print(f"  bid_rev (raw): {p['bid_rev']:.3f}")
        print(f"  bid_trend (raw): {p['bid_trend']:.3f}")
        print(f"  elapsed: {p['elapsed_sec']:.1f}s")
        print(f"  actual_reversal: {p['actual_reversal']}")
        print()
        
# 再看看原始数据中best_bid和best_ask
from fabric import Connection
conn = Connection('47.243.169.223', user='root', connect_kwargs={'password': 'Jinbh1977'})
result = conn.run('grep quote_snapshot /root/TwinEngines/logs/strategy_stdout.log | grep prediction | tail -5', hide=True)

lines = result.stdout.strip().split('\n')
for line in lines[:3]:
    data = json.loads(line)
    quote = data['quote_snapshot']
    print(f"CID: {data['condition_id'][:16]}")
    print(f"  side: {quote['side']}")
    print(f"  best_bid: {quote['best_bid']}")
    print(f"  best_ask: {quote['best_ask']}")
    print(f"  p_rev: {data['prediction']['p_rev']:.3f}")
    
    # 重新算bid_rev和bid_trend
    if quote['side'] == 'UP':
        bid_rev = 1 - quote['best_ask']
        bid_trend = quote['best_bid']
        print(f"  -> bid_rev (DOWN) = 1 - ask = 1 - {quote['best_ask']} = {bid_rev}")
        print(f"  -> bid_trend (UP) = bid = {bid_trend}")
    else:
        bid_rev = 1 - quote['best_ask']
        bid_trend = quote['best_bid']
        print(f"  -> bid_rev (UP) = 1 - ask = 1 - {quote['best_ask']} = {bid_rev}")
        print(f"  -> bid_trend (DOWN) = bid = {bid_trend}")
    
    print(f"  -> odds_rev = 1 - bid_rev = {1 - bid_rev}")
    print(f"  -> odds_trend = 1 - bid_trend = {1 - bid_trend}")
    print()

conn.close()
