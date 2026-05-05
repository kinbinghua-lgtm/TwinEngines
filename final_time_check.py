from fabric import Connection
import json

conn = Connection('47.243.169.223', user='root', connect_kwargs={'password': 'Jinbh1977'})

# 找包含quote_snapshot的日志
result = conn.run('grep quote_snapshot /root/TwinEngines/logs/strategy_stdout.log | grep prediction | head -1', hide=True)
data = json.loads(result.stdout.strip())

print("="*60)
print("时间对齐检查")
print("="*60)
print()

window_start_ms = data['window_start_ms']
quote = data['quote_snapshot']
now_ms = quote['now_ms']
book_ts_ms = quote['book_ts_ms']
residual_sec = data['prediction']['residual_sec']

print(f"窗口开始时间: {window_start_ms}")
print(f"预测时刻 (now_ms): {now_ms}")
print(f"已过时间: {(now_ms - window_start_ms)/1000:.1f} 秒")
print(f"剩余时间: {residual_sec:.1f} 秒")
print(f"总时间: {(now_ms - window_start_ms)/1000 + residual_sec:.1f} 秒 (应该=300)")
print()

print(f"盘口时间戳: {book_ts_ms}")
print(f"盘口延迟: {book_ts_ms - now_ms} ms")
print()

print(f"预测方向: {quote['side']}")
print(f"Best bid: {quote['best_bid']}")
print(f"Best ask: {quote['best_ask']}")
print(f"p_rev: {data['prediction']['p_rev']:.4f}")
print()

# 检查是否在第3分钟
elapsed = (now_ms - window_start_ms) / 1000
print("时间对齐验证:")
if 170 < elapsed < 190:
    print(f"  ✅ 在第3分钟结束时（{elapsed:.1f}秒，预期180秒）")
else:
    print(f"  ⚠️ 时间偏差：预期180秒，实际{elapsed:.1f}秒")

if abs(book_ts_ms - now_ms) < 1000:
    print(f"  ✅ 盘口时间同步良好（延迟{book_ts_ms - now_ms}ms）")
else:
    print(f"  ⚠️ 盘口延迟较大：{(book_ts_ms - now_ms)/1000:.1f}秒")

print()
print("结论:")
print("  - p_raw基于币安价格（第3分钟结束时）")
print("  - 盘口来自Polymarket（同一时刻）")
if 170 < elapsed < 190 and abs(book_ts_ms - now_ms) < 1000:
    print("  ✅ 数据时间对齐正确！")
else:
    print("  ⚠️ 存在时间对齐问题")

conn.close()
