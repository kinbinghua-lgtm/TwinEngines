import paramiko, json

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('47.243.169.223', username='root', password='Jinbh1977', timeout=15)

script = """
import json
with open("logs/shadow_signals.jsonl") as f:
    lines = f.readlines()
print(f"Total signals: {len(lines)}")
for line in lines:
    obj = json.loads(line)
    sec = obj.get("sec_in_window", 0)
    trig = obj.get("trigger_pattern", "")
    side = obj.get("side", "")
    t_rem = obj.get("t_remaining_sec", 0)
    p_rev = obj.get("p_rev", 0)
    rev = obj.get("final_reversal", None)
    poly = obj.get("polymarket", {})
    book = poly.get("book", {})
    opp = poly.get("opportunity", {})
    ask = book.get("best_ask")
    bid = book.get("best_bid")
    spread = round(float(ask)-float(bid), 3) if ask and bid else None
    executable = opp.get("likely_executable")
    print(f"sec={sec:3d} trig={trig} side={side} t_rem={t_rem:4.0f}s ask={ask} bid={bid} spread={spread} exec={executable} rev={rev}")
"""

stdin, stdout, stderr = client.exec_command(f'python3 -c "{script}"')
out = stdout.read().decode()
err = stderr.read().decode()
if err: print("STDERR:", err)
print(out)

# Also check the naked_live_ticks for a broader view
print("\n=== Naked live ticks (last 20) ===")
stdin2, stdout2, stderr2 = client.exec_command(
    'tail -20 /root/TwinEngines/logs/naked_live_ticks.jsonl | python3 -c "'
    'import sys,json;'
    '[print(json.dumps({k:json.loads(line).get(k) for k in [\"ts_iso\",\"sec_in_window\",\"confidence_on_pick\",\"best_ask\",\"best_bid\",\"matched\",\"action\"] if json.loads(line).get(k) is not None}, default=str)) for line in sys.stdin if line.strip()]"'
)
print(stdout2.read().decode())
if stderr2.read().decode(): print("ERR:", stderr2.read().decode())

client.close()
