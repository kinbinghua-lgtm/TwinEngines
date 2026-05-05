from fabric import Connection
import json

conn = Connection('47.243.169.223', user='root', connect_kwargs={'password': 'Jinbh1977'})
result = conn.run('grep quote_snapshot /root/TwinEngines/logs/strategy_stdout.log | grep prediction | head -1', hide=True)
data = json.loads(result.stdout.strip())

window_start_ms = data['window_start_ms']
quote = data['quote_snapshot']
now_ms = quote['now_ms']
book_ts_ms = quote['book_ts_ms']
residual_sec = data['prediction']['residual_sec']
elapsed = (now_ms - window_start_ms) / 1000

print("="*60)
print("Time Alignment Check")
print("="*60)
print()
print(f"Window start: {window_start_ms}")
print(f"Prediction time (now_ms): {now_ms}")
print(f"Elapsed: {elapsed:.1f} seconds")
print(f"Residual: {residual_sec:.1f} seconds")
print(f"Total: {elapsed + residual_sec:.1f} seconds (should be 300)")
print()
print(f"Book timestamp: {book_ts_ms}")
print(f"Book delay: {book_ts_ms - now_ms} ms")
print()
print(f"Side: {quote['side']}")
print(f"Best bid: {quote['best_bid']}")
print(f"Best ask: {quote['best_ask']}")
print(f"p_rev: {data['prediction']['p_rev']:.4f}")
print()
print("Verification:")
print(f"  Expected prediction at 180s (3rd minute end)")
print(f"  Actual: {elapsed:.1f}s")
if 170 < elapsed < 190:
    print("  OK: Within 3rd minute window")
else:
    print(f"  WARNING: Time deviation = {elapsed - 180:.1f}s")

if abs(book_ts_ms - now_ms) < 1000:
    print(f"  OK: Book sync good ({book_ts_ms - now_ms}ms)")
else:
    print(f"  WARNING: Book delay = {(book_ts_ms - now_ms)/1000:.1f}s")

print()
print("Conclusion:")
if 170 < elapsed < 190 and abs(book_ts_ms - now_ms) < 1000:
    print("  Data is time-aligned correctly!")
else:
    print("  Time alignment issue detected!")

conn.close()
