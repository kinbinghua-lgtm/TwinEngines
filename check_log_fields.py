from fabric import Connection
import json

conn = Connection('47.243.169.223', user='root', connect_kwargs={'password': 'Jinbh1977'})

# 看一条完整的日志，检查所有字段
result = conn.run('grep quote_snapshot /root/TwinEngines/logs/strategy_stdout.log | grep prediction | head -1', hide=True)
data = json.loads(result.stdout.strip())

print("=== Full log entry keys ===")
print("Top-level keys:", list(data.keys()))
print()

quote = data['quote_snapshot']
print("=== quote_snapshot keys ===")
print(list(quote.keys()))
print()

print("=== quote_snapshot values ===")
for k, v in quote.items():
    print(f"  {k}: {v}")

print()
print("=== prediction ===")
pred = data['prediction']
for k, v in pred.items():
    print(f"  {k}: {v}")

print()

# Check if there's any mention of "down" or "opposite" book
result2 = conn.run('grep quote_snapshot /root/TwinEngines/logs/strategy_stdout.log | head -1', hide=True)
data2 = json.loads(result2.stdout.strip())
print("=== All keys in full log (including nested) ===")
def print_keys(d, prefix=""):
    for k, v in d.items():
        if isinstance(v, dict):
            print(f"{prefix}{k}: (dict)")
            print_keys(v, prefix + "  ")
        else:
            print(f"{prefix}{k}: {v}")
print_keys(data2)

conn.close()
