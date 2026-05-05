#!/usr/bin/env python3
"""测试自动领取功能的诊断脚本"""

import json
import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent / "src"))

from twinengines.io.config import load_polymarket_runtime_cfg
from twinengines.io.polymarket_client import PolymarketClient, POLYMARKET_PLATFORM
from twinengines.io.logging_setup import setup_logging, get_logger

setup_logging(log_dir="logs", level="INFO")
logger = get_logger(__name__)


def test_redeem_capability():
    """测试领取能力检查"""
    print("=== 测试领取能力检查 ===\n")
    
    runtime = load_polymarket_runtime_cfg(env_file=".env")
    poly = PolymarketClient(runtime_cfg=runtime, platform=POLYMARKET_PLATFORM)
    
    # 初始化客户端
    ok_init, msg_init = poly.init_real_client()
    print(f"客户端初始化: {'成功' if ok_init else '失败'} - {msg_init}")
    
    # 检查领取能力
    ok_cap, reason_cap = poly.redeem_capability_check()
    print(f"领取能力检查: {'通过' if ok_cap else '失败'} - {reason_cap}")
    
    if not ok_cap:
        print("\n可能的问题：")
        if "missing_builder_credentials" in reason_cap:
            print("  - 缺少 Builder API 凭据（SIGNATURE_TYPE=1或2时需要）")
            print("  - 检查 .env 中的 POLY_BUILDER_API_KEY, POLY_BUILDER_SECRET, POLY_BUILDER_PASSPHRASE")
        elif "relayer_dependencies_missing" in reason_cap:
            print("  - 缺少 relayer 依赖库")
            print("  - 运行: pip install 'git+https://github.com/Polymarket/py-builder-relayer-client.git'")
        elif "web3_unavailable" in reason_cap:
            print("  - web3 库不可用")
            print("  - 运行: pip install web3")
        elif "proxy_not_equal_to_eoa" in reason_cap:
            print("  - SIGNATURE_TYPE=0 时，PROXY_ADDRESS 必须等于私钥对应的 EOA 地址")
            print("  - 或者改用 SIGNATURE_TYPE=1 (Magic Proxy)")
    
    return ok_cap, runtime, poly


def test_fetch_positions(poly, runtime):
    """测试获取持仓"""
    print("\n=== 测试获取持仓 ===\n")
    
    # 读取状态文件获取最近的 condition_ids
    state_path = Path("data_runtime/naked_real_state.json")
    if not state_path.exists():
        print(f"状态文件不存在: {state_path}")
        return []
    
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        print(f"读取状态文件失败: {e}")
        return []
    
    recent_cids = state.get("recent_condition_ids", [])
    
    # 如果没有 recent_condition_ids，尝试从 last_attempt_condition_id 获取
    if not recent_cids:
        last_cid = state.get("last_attempt_condition_id")
        if last_cid:
            print(f"从 last_attempt_condition_id 获取: {last_cid[:16]}...")
            recent_cids = [last_cid]
        else:
            print("没有找到任何 condition_ids，无法检查持仓")
            return []
    
    print(f"最近的 condition_ids 数量: {len(recent_cids)}")
    
    # 检查最近 5 个合约的持仓
    check_cids = recent_cids[-5:] if len(recent_cids) > 5 else recent_cids
    print(f"检查最近 {len(check_cids)} 个合约的持仓...\n")
    
    redeemable = []
    for i, cid in enumerate(check_cids, 1):
        print(f"{i}. Condition ID: {cid[:16]}...")
        try:
            positions = poly.fetch_market_positions(condition_id=str(cid))
            print(f"   持仓数量: {len(positions)}")
            
            for p in positions:
                if not isinstance(p, dict):
                    continue
                
                curr_price = p.get("currPrice")
                size = p.get("size")
                outcome = p.get("outcome")
                pnl = p.get("totalPnl")
                
                print(f"   - Outcome: {outcome}, Size: {size}, CurrPrice: {curr_price}, PnL: {pnl}")
                
                if curr_price == 1 and size and float(size) > 0:
                    redeemable.append({
                        "condition_id": str(cid),
                        "token_id": p.get("token_id") or p.get("asset"),
                        "size": float(size),
                        "outcome": outcome,
                        "totalPnl": pnl,
                    })
                    print(f"   ✓ 可领取！")
        except Exception as e:
            print(f"   错误: {e}")
    
    return redeemable


def test_redeem_positions(poly, redeemable):
    """测试领取持仓"""
    print("\n=== 测试领取持仓 ===\n")
    
    if not redeemable:
        print("没有可领取的持仓")
        return
    
    print(f"发现 {len(redeemable)} 个可领取的持仓\n")
    
    for i, item in enumerate(redeemable, 1):
        cid = item["condition_id"]
        print(f"{i}. 尝试领取 Condition ID: {cid[:16]}...")
        print(f"   Size: {item['size']}, PnL: {item.get('totalPnl')}")
        
        try:
            ok, reason, tx_hash = poly.redeem_positions(condition_id=cid)
            
            if ok:
                print(f"   ✓ 领取成功！")
                print(f"   交易哈希: {tx_hash}")
            else:
                print(f"   ✗ 领取失败: {reason}")
                
                # 提供详细的错误诊断
                if "relayer" in reason.lower():
                    print("   提示: Relayer 相关错误，检查 Builder API 凭据")
                elif "web3" in reason.lower():
                    print("   提示: Web3 相关错误，检查 RPC 连接")
                elif "signature" in reason.lower():
                    print("   提示: 签名相关错误，检查 SIGNATURE_TYPE 配置")
        except Exception as e:
            print(f"   ✗ 异常: {e}")
        
        print()


def main():
    print("TwinEngines 自动领取功能诊断工具\n")
    print("=" * 60)
    
    # 1. 测试领取能力
    ok_cap, runtime, poly = test_redeem_capability()
    
    if not ok_cap:
        print("\n领取能力检查未通过，请先解决上述问题")
        return
    
    # 2. 测试获取持仓
    redeemable = test_fetch_positions(poly, runtime)
    
    # 3. 测试领取持仓
    if redeemable:
        response = input("\n是否尝试领取这些持仓？(y/n): ")
        if response.lower() == 'y':
            test_redeem_positions(poly, redeemable)
    
    print("\n" + "=" * 60)
    print("诊断完成")


if __name__ == "__main__":
    main()
