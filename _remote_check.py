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
