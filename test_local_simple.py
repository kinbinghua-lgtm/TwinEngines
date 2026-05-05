"""本地测试：跑一个窗口看信号和盘口匹配"""
import sys
import time
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from twinengines.io.config import load_polymarket_runtime_cfg
from twinengines.io.polymarket_client import PolymarketClient
from twinengines.io.market_resolver import MarketResolver, MarketResolverCfg
from twinengines.live.third_digit_naked import load_third_digit_dynamic_params
from twinengines.live.naked_pm_runner import run_naked_third_digit_tick

def main():
    runtime = load_polymarket_runtime_cfg(".env")
    poly = PolymarketClient(runtime_cfg=runtime)
    
    ok, msg = poly.init_real_client()
    if not ok:
        print(f"CLOB init failed: {msg}")
        return
    
    params = load_third_digit_dynamic_params("reports/prefix_survival_model_90d_step10.json")
    resolver = MarketResolver(
        cfg=MarketResolverCfg(
            keywords=("Bitcoin", "BTC"),
            horizon_minutes=5,
        ),
    )
    
    results = []
    for i in range(20):
        result = run_naked_third_digit_tick(
            params=params,
            poly=poly,
            resolver=resolver,
            confidence_min=0.51,
            target_quote_usdc=1.0,
            causal_decision_lag_sec=3.0,
            state_path=Path("data_runtime/test_local_state.json"),
            yes_real_money=False,
            bare_formula_eval=False,
            sec_dynamic_1s=True,
        )
        
        action = result.get("action")
        pred = result.get("prediction", {})
        op = result.get("order_plan", {})
        gate = result.get("entry_gate", {})
        
        record = {
            "tick": i+1,
            "action": action,
            "conf": pred.get("confidence_on_pick"),
            "prefix": pred.get("current_prefix"),
            "bucket": pred.get("prefix_bucket_used"),
            "best_ask": op.get("best_ask") or result.get("best_ask"),
            "edge": result.get("edge_vs_best_ask"),
            "gate_selected": gate.get("selected"),
        }
        results.append(record)
        
        if action == "would_submit_dry_run_need_yes_real_money":
            print(f"Tick {i+1}: MATCH! conf={record['conf']:.3f} ask={record['best_ask']} edge={record['edge']:.3f}")
        elif action == "waiting_minute3_close":
            print(f"Tick {i+1}: waiting...")
        else:
            print(f"Tick {i+1}: {action}")
        
        time.sleep(8)
    
    with open("test_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    
    print(f"\nResults saved to test_results.json")
    
    actions = {}
    for r in results:
        a = r["action"]
        actions[a] = actions.get(a, 0) + 1
    
    print("\nSummary:")
    for a, count in sorted(actions.items(), key=lambda x: -x[1]):
        print(f"  {a}: {count}")

if __name__ == "__main__":
    main()
