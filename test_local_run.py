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
    print("=== 加载配置 ===")
    runtime = load_polymarket_runtime_cfg(".env")
    poly = PolymarketClient(runtime_cfg=runtime)
    
    print("=== 初始化 CLOB ===")
    ok, msg = poly.init_real_client()
    if not ok:
        print(f"CLOB init failed: {msg}")
        return
    print("CLOB init ok")
    
    print("=== 加载模型 ===")
    params = load_third_digit_dynamic_params("reports/prefix_survival_model_90d_step10.json")
    print(f"模型加载完成")
    
    print("=== 初始化 Market Resolver ===")
    resolver = MarketResolver(
        cfg=MarketResolverCfg(
            keywords=("Bitcoin", "BTC"),
            horizon_minutes=5,
        ),
    )
    
    print("=== 开始轮询（最多 20 次，约 2.5 分钟）===")
    for i in range(20):
        print(f"\n--- Tick {i+1} ---")
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
        print(f"Action: {action}")
        
        if action == "waiting_minute3_close":
            print(f"  等待第3分钟...")
        elif action == "no_active_market":
            print(f"  没有活动市场")
        elif action == "bare_formula_tick":
            pred = result.get("prediction", {})
            print(f"  Confidence: {pred.get('confidence_on_pick'):.4f}")
            print(f"  Prefix: {pred.get('current_prefix')}")
            print(f"  Bucket: {pred.get('prefix_bucket_used')}")
        elif action in ("odds_above_dynamic_tier_cap", "edge_below_dynamic_tier_min", "below_confidence_tier_min"):
            pred = result.get("prediction", {})
            gate = result.get("entry_gate", {})
            print(f"  Confidence: {pred.get('confidence_on_pick'):.4f}")
            print(f"  Best ask: {result.get('best_ask')}")
            print(f"  Edge: {result.get('edge_vs_best_ask'):.4f}")
            print(f"  Gate: {gate.get('selected')}")
        elif action == "would_submit_dry_run_need_yes_real_money":
            pred = result.get("prediction", {})
            op = result.get("order_plan", {})
            print(f"  [OK] 满足所有条件！")
            print(f"  Confidence: {pred.get('confidence_on_pick'):.4f}")
            print(f"  Prefix: {pred.get('current_prefix')}")
            print(f"  Best ask: {op.get('best_ask')}")
            print(f"  Limit price: {op.get('limit_price')}")
            print(f"  Edge: {result.get('edge_vs_best_ask'):.4f}")
            print(f"  Size: {op.get('size_quote_usdc')} USDC")
        else:
            print(f"  其他: {action}")
        
        time.sleep(8)
    
    print("\n=== 测试完成 ===")

if __name__ == "__main__":
    main()
