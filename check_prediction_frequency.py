from fabric import Connection
import json

conn = Connection('47.243.169.223', user='root', connect_kwargs={'password': 'Jinbh1977'})
result = conn.run('grep quote_snapshot /root/TwinEngines/logs/strategy_stdout.log | grep prediction | head -10', hide=True)
lines = result.stdout.strip().split('\n')

print(f"Found {len(lines)} samples in same window")
print()
print("Checking prediction frequency:")
print()

prev_window = None
same_window_count = 0

for i, line in enumerate(lines[:10], 1):
    data = json.loads(line)
    window_start = data['window_start_ms']
    elapsed = (data['quote_snapshot']['now_ms'] - window_start) / 1000
    residual = data['prediction']['residual_sec']
    p_rev = data['prediction']['p_rev']
    
    if prev_window == window_start:
        same_window_count += 1
    else:
        if prev_window is not None:
            print(f"  -> Window had {same_window_count} predictions")
            print()
        print(f"Window {window_start}:")
        same_window_count = 1
        prev_window = window_start
    
    print(f"  {i}. Elapsed: {elapsed:.1f}s, Residual: {residual:.1f}s, p_rev: {p_rev:.4f}")

print(f"  -> Window had {same_window_count} predictions")
print()
print("Conclusion:")
if same_window_count > 1:
    print("  YES: Multiple predictions per window (continuous/per-second)")
else:
    print("  NO: Only one prediction per window")

conn.close()
