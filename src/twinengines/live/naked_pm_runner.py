"""
third_digit 裸模型 + Polymarket 单笔入场。

预测核默认：**Binance 1s 已收盘路径 + 剩余时间**（秒级动态 hazard）；可退回分钟结点或冻结快照。

下单执行、到期刹车、状态去重等仅在常规模式下启用；``bare_formula_eval=True`` 时只产出 prediction，不拉盘口。
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

from ..io import MarketResolver, MarketResolverCfg, PolymarketClient, POLYMARKET_PLATFORM
from ..io.config import is_real_order_allowed, load_polymarket_runtime_cfg
from ..io.logging_setup import get_logger, setup_logging
from ..io.polymarket_client import OrderState, OrderTicket
from ..io.state_store import KEY_LAST_EQUITY, StateStore
from ..risk.sizing import SizingCfg
from .third_digit_naked import (
    ThirdDigitDynamicParams,
    closed_minute_bars_since_window_start,
    fetch_binance_closes_and_baseline,
    fetch_binance_true_up_window_end,
    load_third_digit_dynamic_params,
    naked_predict_live_dynamic,
    naked_predict_minute3,
    trading_ready_after_minute3_close,
)
from .third_digit_sec_dynamic import naked_predict_live_sec_dynamic
from .naked_funds_manager import NakedFundsManager
from .naked_kelly_sizing import naked_target_quote_with_kelly
from .naked_odds_tiers import DEFAULT_CONF_ODDS_TIERS as NAKED_CONF_ODDS_TIERS
from .naked_odds_tiers import odds_cap_strict_below_for_confidence as _odds_cap_strict_below_for_confidence

logger = get_logger(__name__)


def _kv_sync_last_equity_after_trade(poly: PolymarketClient) -> None:
    """WebUI「当前资金」读 kv account.last_equity_usdc；naked 路径无 Reconciler 时需成交后主动刷新。"""
    try:
        db_path = str(getattr(poly.runtime_cfg, "state_db_path", "") or "").strip()
        if not db_path:
            return
        eq = poly.fetch_account_equity_usdc()
        if eq is None:
            return
        StateStore(db_path=db_path).put(KEY_LAST_EQUITY, float(eq))
    except Exception as e:
        logger.warning("kv_sync_last_equity_after_trade failed: %s", e)


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = dict(payload)
    if "ts_iso" not in rec:
        rec["ts_iso"] = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str))
        f.write("\n")


# 高价分界：限价 >= 此值视为「高价侧」（宽松模式下走 FOK 扫单；严格模式下仍可能因 FOK 价规改 GTC）。
NAKED_HIGH_PRICE_FOK_ONLY_THRESHOLD = 0.51
# 严格模式（confidence_min <= NAKED_CONFIDENCE_RELAX_PRICE_CAP）：仅当限价 **严格低于** 此值才允许 FOK；否则走 GTC。
NAKED_STRICT_FOK_MAX_PRICE_EXCLUSIVE = 0.5
# 置信度门槛 **高于** 此值时，不再套用「FOK 必须价 < 0.5」，仅按 0.51 高价分界决定 FOK vs GTC。
NAKED_CONFIDENCE_RELAX_PRICE_CAP = 0.56
NAKED_FOK_RETRY_MAX_ATTEMPTS = 8
NAKED_FOK_RETRY_SLEEP_SEC = 1.0
# GTC 仅当所选边 best_ask **严格低于** 此值；否则一律走 FOK 重试（与价位路由无关）。
NAKED_GTC_MAX_BEST_ASK_EXCLUSIVE = 0.2
# 动态入场盘口闸：按模型选边置信度分档限制最高可买价，并要求最小正 edge。
# 每档含义：(confidence 下限, best_ask 最高允许值, fair_prob_side - best_ask 最小值)。
NAKED_DYNAMIC_ENTRY_TIERS: tuple[tuple[float, float, float], ...] = (
    (0.85, 0.78, 0.08),
    (0.75, 0.72, 0.07),
    (0.65, 0.65, 0.06),
    (0.56, 0.55, 0.05),
    (0.501, 0.49, 0.08),  # 2026-05-03: 提高edge要求从0.06到0.08，基于Edge验证报告低置信度+高edge表现更好
)

STATE_KEY_LOW_CONF_TP = "low_conf_tp_watch"
# 按入场时的赔率档 odds_cap（与 NAKED_CONF_ODDS_TIERS 一致）：要求的买一/入场均价倍数 bid/entry；None=窗口结束前不平，到期后再平仓。
# 0.1→5倍浮盈→bid/entry>=6；0.2→4倍→>=5；0.3→3倍→>=4；0.4→2倍→>=3；0.5→None（留到 end_ts_ms 后）。
NAKED_TP_CLOSE_POLL_ATTEMPTS = 40
NAKED_TP_CLOSE_SLEEP_SEC = 0.75


def _tp_min_bid_over_entry_for_odds_cap(odds_cap: float) -> Optional[float]:
    oc = float(odds_cap)
    if oc >= 0.5 - 1e-9:
        return None
    if oc >= 0.4 - 1e-9:
        return 3.0
    if oc >= 0.3 - 1e-9:
        return 4.0
    if oc >= 0.2 - 1e-9:
        return 5.0
    return 6.0


def _tp_trigger_decision(
    *,
    odds_cap: float,
    entry_px: float,
    bid: float,
    now_ms: int,
    end_ts_ms: int,
) -> Tuple[bool, str]:
    thr = _tp_min_bid_over_entry_for_odds_cap(odds_cap)
    if thr is None:
        if end_ts_ms > 0 and now_ms < end_ts_ms:
            return False, "defer_until_window_end"
        return True, "window_end_unwind"
    ratio = float(bid) / float(entry_px)
    if ratio + 1e-15 < float(thr):
        return False, f"below_float_multiple_need_bid_over_entry_{thr}"
    return True, f"float_multiple_met_ratio_{ratio:.6f}"


def _naked_round_shares_up(raw: float, min_sz: float) -> float:
    x = max(float(raw), float(min_sz))
    return round(math.ceil(x * 10000 - 1e-9) / 10000, 4)


def _naked_round_shares_for_sell(remaining: float, min_sz: float) -> float:
    """卖出数量：向下取 4 位小数且不超过持仓，且不低于最小手数。"""
    r = float(remaining)
    x = math.floor(r * 10000 + 1e-9) / 10000
    x = round(float(x), 4)
    x = max(float(min_sz), x)
    return float(min(x, r))


def _dynamic_entry_rule_for_conf(conf: float) -> dict[str, Any] | None:
    c = float(conf)
    for min_conf, max_ask, min_edge in NAKED_DYNAMIC_ENTRY_TIERS:
        if c + 1e-15 >= float(min_conf):
            return {
                "min_conf": float(min_conf),
                "max_ask": float(max_ask),
                "min_edge": float(min_edge),
            }
    return None


def _run_auto_redeem_tick(
    *,
    poly: PolymarketClient,
    state_path: Path,
    runtime_cfg: Any,
) -> dict[str, Any] | None:
    """自动领取赢单：扫描最近成交的合约，找出 currPrice=1 的持仓并领取。
    
    改进版本：
    1. 使用配置的检查间隔（默认30秒）
    2. 增加详细的错误日志
    3. 检查 redeem capability
    """
    # 检查是否启用自动领取
    if not getattr(runtime_cfg, "auto_redeem_enabled", False):
        return None
    
    st = _load_state(state_path)
    last_redeem_ts = st.get("last_auto_redeem_ts_ms", 0)
    now_ms = int(time.time() * 1000)
    
    # 使用配置的检查间隔
    interval_ms = int(getattr(runtime_cfg, "auto_redeem_interval_sec", 30.0) * 1000)
    if now_ms - last_redeem_ts < interval_ms:
        return None
    
    st["last_auto_redeem_ts_ms"] = now_ms
    _save_state(state_path, st)
    
    # 检查 redeem 能力
    ok_cap, reason_cap = poly.redeem_capability_check()
    if not ok_cap:
        logger.warning("auto_redeem capability check failed: %s", reason_cap)
        return {
            "phase": "auto_redeem",
            "action": "capability_check_failed",
            "reason": reason_cap,
        }
    
    # 获取最近尝试过的 condition_ids
    recent_cids = st.get("recent_condition_ids", [])
    if not recent_cids:
        return {"phase": "auto_redeem", "action": "no_recent_conditions"}
    
    # 检查最近 20 个合约（增加范围）
    check_cids = recent_cids[-20:] if len(recent_cids) > 20 else recent_cids
    redeemable = []
    
    for cid in check_cids:
        try:
            positions = poly.fetch_market_positions(condition_id=str(cid))
            for p in positions:
                if not isinstance(p, dict):
                    continue
                curr_price = p.get("currPrice")
                size = p.get("size")
                # currPrice=1 表示已结算且赢了
                if curr_price == 1 and size and float(size) > 0:
                    redeemable.append({
                        "condition_id": str(cid),
                        "token_id": p.get("token_id") or p.get("asset"),
                        "size": float(size),
                        "outcome": p.get("outcome"),
                        "totalPnl": p.get("totalPnl"),
                    })
        except Exception as e:
            logger.debug("fetch_market_positions failed for cid=%s: %s", str(cid)[:12], e)
            continue
    
    if not redeemable:
        return {"phase": "auto_redeem", "action": "no_redeemable_positions", "checked": len(check_cids)}
    
    # 尝试领取
    redeemed = []
    failed = []
    for item in redeemable:
        cid = item["condition_id"]
        try:
            logger.info("attempting redeem for condition_id=%s size=%.4f", str(cid)[:12], item["size"])
            ok, reason, tx_hash = poly.redeem_positions(condition_id=cid)
            if ok:
                logger.info("redeem success: cid=%s tx=%s", str(cid)[:12], tx_hash)
                redeemed.append({"condition_id": cid, "tx_hash": tx_hash, "pnl": item.get("totalPnl")})
            else:
                logger.warning("redeem failed: cid=%s reason=%s", str(cid)[:12], reason)
                failed.append({"condition_id": cid, "reason": reason})
        except Exception as e:
            logger.exception("redeem exception for cid=%s: %s", str(cid)[:12], e)
            failed.append({"condition_id": cid, "error": str(e)})
    
    return {
        "phase": "auto_redeem",
        "action": "redeem_attempted",
        "redeemable_count": len(redeemable),
        "redeemed_count": len(redeemed),
        "failed_count": len(failed),
        "redeemed": redeemed,
        "failed": failed,
    }


def _position_size_for_token(positions: list[dict], token_id: str) -> float:
    tid = str(token_id).strip()
    for p in positions:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("token_id") or p.get("asset") or "").strip()
        if pid != tid:
            continue
        for k in ("size", "balance", "amount", "position", "numShares"):
            v = p.get(k)
            if v is None:
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def run_low_conf_take_profit_tick(
    *,
    poly: PolymarketClient,
    state_path: Path,
    yes_real_money: bool,
) -> dict[str, Any] | None:
    """成交头寸止盈：按入场赔率档 odds_cap（NAKED_CONF_ODDS_TIERS）设买一/入场倍数；0.5 档仅在 end_ts_ms 之后 FOK 清仓。
    
    窗口结束后直接清除 watch，不再尝试卖出（避免已结算合约卡住并发限制）。
    """
    if not yes_real_money:
        return None
    st = _load_state(state_path)
    watch = st.get(STATE_KEY_LOW_CONF_TP)
    if not isinstance(watch, dict):
        return None

    cid = watch.get("condition_id")
    tid = watch.get("token_id")
    entry_px = float(watch.get("entry_avg_price") or 0)
    entry_conf = float(watch.get("confidence_at_entry") or 0)
    end_ts_watch = int(watch.get("end_ts_ms") or 0)
    now_ms_tp = int(time.time() * 1000)
    
    if not cid or not tid or entry_px <= 1e-12:
        del st[STATE_KEY_LOW_CONF_TP]
        _save_state(state_path, st)
        return {"phase": "low_conf_tp", "action": "watch_cleared_invalid"}

    # 窗口结束后直接清除 watch，不再尝试卖出
    if end_ts_watch > 0 and now_ms_tp >= end_ts_watch + 60_000:
        del st[STATE_KEY_LOW_CONF_TP]
        _save_state(state_path, st)
        return {
            "phase": "low_conf_tp",
            "action": "watch_cleared_window_expired",
            "end_ts_ms": end_ts_watch,
            "now_ms": now_ms_tp,
            "note": "window_ended_stop_trying_to_sell",
        }

    ok_init, msg_init = poly.init_real_client()
    if not ok_init:
        return {"phase": "low_conf_tp", "action": "clob_init_failed", "error": msg_init}

    plat = POLYMARKET_PLATFORM
    positions = poly.fetch_market_positions(condition_id=str(cid))
    remaining = _position_size_for_token(positions, str(tid))

    min_sh = float(plat.min_limit_order_shares)
    if remaining <= 1e-6:
        del st[STATE_KEY_LOW_CONF_TP]
        _save_state(state_path, st)
        return {"phase": "low_conf_tp", "action": "flat_cleared_watch"}

    if remaining + 1e-9 < min_sh:
        del st[STATE_KEY_LOW_CONF_TP]
        _save_state(state_path, st)
        return {
            "phase": "low_conf_tp",
            "action": "dust_cleared_watch",
            "remaining": remaining,
            "note": "below_min_limit_order_shares",
        }

    book = poly.fetch_book(str(tid))
    bid = book.get("best_bid")
    if bid is None:
        return {"phase": "low_conf_tp", "action": "no_best_bid_skip", "remaining": remaining}

    odds_cap = float(watch.get("odds_cap_at_entry") or _odds_cap_strict_below_for_confidence(entry_conf))
    ratio_at_trigger = float(bid) / entry_px
    ok_trig, trig_reason = _tp_trigger_decision(
        odds_cap=odds_cap,
        entry_px=entry_px,
        bid=float(bid),
        now_ms=now_ms_tp,
        end_ts_ms=end_ts_watch,
    )
    if not ok_trig:
        return None

    runtime = poly.runtime_cfg
    cross_ticks = max(0, int(getattr(runtime, "entry_buy_cross_ticks", 0)))
    tick = float(plat.price_tick)

    attempts_out: list[dict[str, Any]] = []
    for i in range(int(NAKED_TP_CLOSE_POLL_ATTEMPTS)):
        book_i = poly.fetch_book(str(tid))
        bid_i = book_i.get("best_bid")
        if bid_i is None:
            attempts_out.append({"i": i, "skip": "no_best_bid"})
            time.sleep(float(NAKED_TP_CLOSE_SLEEP_SEC))
            continue
        sell_px = float(bid_i) - cross_ticks * tick
        sell_px = max(float(plat.price_extreme_min), min(float(plat.price_extreme_max), sell_px))
        sell_px = round(sell_px, 2)

        positions = poly.fetch_market_positions(condition_id=str(cid))
        remaining = _position_size_for_token(positions, str(tid))
        if remaining <= 1e-6:
            del st[STATE_KEY_LOW_CONF_TP]
            _save_state(state_path, st)
            return {
                "phase": "low_conf_tp",
                "action": "tp_flat_done",
                "attempts": attempts_out,
                "bid_over_entry_at_trigger": ratio_at_trigger,
                "tp_trigger_reason": trig_reason,
                "odds_cap_at_entry": odds_cap,
            }

        if remaining + 1e-9 < min_sh:
            del st[STATE_KEY_LOW_CONF_TP]
            _save_state(state_path, st)
            return {
                "phase": "low_conf_tp",
                "action": "tp_flat_dust_remainder",
                "remaining": remaining,
                "attempts": attempts_out,
            }

        sell_sz = _naked_round_shares_for_sell(remaining, min_sh)
        quote = round(sell_sz * sell_px, 2)
        if quote < float(plat.min_order_quote_usdc):
            quote = float(plat.min_order_quote_usdc)

        coid = f"naked_tp_close:{str(cid)[-12:]}:{int(time.time())}:{i}"
        ticket = poly.submit_order(
            side="SELL",
            token_id=str(tid),
            price=float(sell_px),
            size_quote_usdc=float(quote),
            client_order_id=coid,
            size_shares=float(sell_sz),
            entry_fok_first=True,
            entry_fok_fallback_gtc=False,
        )
        attempts_out.append(
            {
                "i": i,
                "state": ticket.state.value,
                "sell_sz": sell_sz,
                "sell_px": sell_px,
                "last_error": ticket.last_error,
            }
        )
        time.sleep(float(NAKED_TP_CLOSE_SLEEP_SEC))

    watch["remaining_shares_last"] = remaining
    st[STATE_KEY_LOW_CONF_TP] = watch
    _save_state(state_path, st)
    return {
        "phase": "low_conf_tp",
        "action": "tp_round_incomplete_retry_next_poll",
        "attempts": attempts_out,
        "remaining": remaining,
    }


def _naked_entry_use_fok_retries(*, limit_price: float, confidence_min: float) -> tuple[bool, dict[str, Any]]:
    hi = float(NAKED_HIGH_PRICE_FOK_ONLY_THRESHOLD)
    cap = float(NAKED_STRICT_FOK_MAX_PRICE_EXCLUSIVE)
    relax_cap = float(NAKED_CONFIDENCE_RELAX_PRICE_CAP)
    cm = float(confidence_min)
    relaxed = cm > relax_cap
    meta: dict[str, Any] = {
        "high_price_fok_threshold": hi,
        "strict_fok_requires_limit_below": cap,
        "confidence_relax_when_above": relax_cap,
        "confidence_min": cm,
        "price_rule_relaxed": relaxed,
    }
    if relaxed:
        meta["branch"] = "relaxed_split"
        use_fok = limit_price >= hi
        meta["use_fok_retries"] = use_fok
        meta["reason"] = "gte_high_fok" if use_fok else "lt_high_gtc"
        return use_fok, meta
    meta["branch"] = "strict_fok_price_cap"
    if limit_price >= hi:
        meta["use_fok_retries"] = False
        meta["reason"] = "gte_high_gtc_only"
        return False, meta
    if limit_price < cap:
        meta["use_fok_retries"] = True
        meta["reason"] = "lt_cap_fok_retries"
        return True, meta
    meta["use_fok_retries"] = False
    meta["reason"] = "mid_band_cap_to_high_gtc_only"
    return False, meta


def _naked_gate_gtc_by_best_ask(
    *,
    use_fok_retries: bool,
    entry_route: dict[str, Any],
    best_ask: float,
) -> tuple[bool, dict[str, Any]]:
    """价位路由若选 GTC，仍须 best_ask < NAKED_GTC_MAX_BEST_ASK_EXCLUSIVE，否则强制 FOK。"""
    if use_fok_retries:
        return use_fok_retries, entry_route
    cap = float(NAKED_GTC_MAX_BEST_ASK_EXCLUSIVE)
    if float(best_ask) < cap - 1e-15:
        return use_fok_retries, entry_route
    meta = dict(entry_route)
    meta["gtc_blocked_by_best_ask_ge_cap"] = True
    meta["gtc_allowed_only_best_ask_strict_below"] = cap
    meta["best_ask_at_gate"] = float(best_ask)
    meta["reason_gate"] = "best_ask_ge_cap_force_fok_retries"
    meta["use_fok_retries"] = True
    return True, meta


def run_naked_third_digit_tick(
    *,
    params: ThirdDigitDynamicParams,
    poly: PolymarketClient,
    resolver: MarketResolver,
    confidence_min: float,
    target_quote_usdc: float,
    causal_decision_lag_sec: float,
    state_path: Path,
    yes_real_money: bool,
    skip_seconds_before_expiry: float = 0.0,
    frozen_minute3_only: bool = False,
    require_positive_edge: bool = False,
    sec_dynamic_1s: bool = True,
    sec_path_stride: int = 1,
    bare_formula_eval: bool = False,
    funds_manager: Optional[NakedFundsManager] = None,
    use_kelly_sizing: bool = False,
    kelly_fraction: float = 0.125,
    kelly_max_stake_ratio: float = 0.04,
) -> dict[str, Any]:
    """
    单次扫描：若当前活动合约已越过第 3 分钟末且置信度达标，则对预测方向按 best_ask(+tick) 限价买入。

    执行拆分（参见 ``_naked_entry_use_fok_retries`` + ``_naked_gate_gtc_by_best_ask``）：
    - ``confidence_min > NAKED_CONFIDENCE_RELAX_PRICE_CAP``：限价 ``>= 0.51`` → FOK 重试；否则 GTC。
    - 否则（严格）：限价 ``< 0.5`` → FOK；``>= 0.51`` 或落在 ``[0.5, 0.51)`` → GTC。
    - **GTC 总闸**：仅当所选边 ``best_ask < NAKED_GTC_MAX_BEST_ASK_EXCLUSIVE``（默认 0.2）才允许 GTC；否则强制 FOK。

    盘口闸：按模型选边置信度使用动态入场分档 ``NAKED_DYNAMIC_ENTRY_TIERS``，同时要求：
    - ``best_ask <= tier.max_ask``，避免高价追单；
    - ``fair_prob_side - best_ask >= tier.min_edge``，保证安全边际。
    旧 ``confidence_on_pick`` 分档（``NAKED_CONF_ODDS_TIERS``）仅写入诊断字段
    ``legacy_odds_cap_strict_below`` / ``legacy_odds_tier_would_block``，不再硬拦截。

    止盈监视（FILLED 后写入 ``low_conf_tp_watch``）：按入场档 ``odds_cap_at_entry`` —— 0.1→bid/entry≥6，
    0.2→≥5，0.3→≥4，0.4→≥3；0.5 档盘中不按倍数触发，待 ``end_ts_ms`` 后再 FOK 清仓（``run_low_conf_take_profit_tick``）。

    默认 ``sec_dynamic_1s=True``：优先用 **1s 已收盘价路径** + 剩余时间算 ``H``；失败则退回分钟结点动态；
    ``frozen_minute3_only=True`` 时仍为固定 120s 快照。
    """
    plat = POLYMARKET_PLATFORM
    out: dict[str, Any] = {"phase": "tick", "action": "noop"}
    st: dict[str, Any] = {} if bare_formula_eval else _load_state(state_path)

    if not resolver.refresh_once():
        out["error"] = "resolver_refresh_failed"
        return out

    active = resolver.get_active()
    if active is None:
        out["action"] = "no_active_market"
        return out

    cid = str(active.condition_id)
    if not bare_formula_eval and st.get("last_attempt_condition_id") == cid:
        out["action"] = "already_attempted_this_condition"
        out["condition_id"] = cid
        return out

    window_start_ms = int(active.end_ts_ms) - 5 * 60_000
    end_ts_ms = int(active.end_ts_ms)
    now_ms = int(time.time() * 1000)

    if (
        not bare_formula_eval
        and float(skip_seconds_before_expiry) > 0.0
        and now_ms >= end_ts_ms - int(float(skip_seconds_before_expiry) * 1000)
    ):
        out["action"] = "too_close_to_expiry"
        out["condition_id"] = cid
        return out

    if not trading_ready_after_minute3_close(
        window_start_ms=window_start_ms,
        now_ms=now_ms,
        causal_decision_lag_sec=causal_decision_lag_sec,
    ):
        out["action"] = "waiting_minute3_close"
        out["condition_id"] = cid
        out["window_start_ms"] = window_start_ms
        out["market_question"] = getattr(active, "question", "")[:120]
        return out

    bx = fetch_binance_closes_and_baseline(symbol=params.symbol, window_start_ms=window_start_ms, n_bars=5)
    if bx is None:
        out["action"] = "binance_klines_failed"
        out["condition_id"] = cid
        return out

    baseline, closes = bx
    if len(closes) < 3:
        out["action"] = "binance_insufficient_bars"
        out["condition_id"] = cid
        return out

    n_closed = closed_minute_bars_since_window_start(window_start_ms=window_start_ms, now_ms=now_ms)
    pred: dict[str, Any] | None = None
    if frozen_minute3_only:
        pred = naked_predict_minute3(baseline=baseline, closes_first_three=closes[:3], params=params)
    elif sec_dynamic_1s:
        pred = naked_predict_live_sec_dynamic(
            baseline=baseline,
            closes_first_three=closes[:3],
            window_start_ms=window_start_ms,
            end_ts_ms=end_ts_ms,
            now_ms=now_ms,
            params=params,
            sec_path_stride=int(sec_path_stride),
        )
        if pred is None:
            pred = naked_predict_live_dynamic(
                baseline=baseline,
                closes=closes,
                window_start_ms=window_start_ms,
                end_ts_ms=end_ts_ms,
                now_ms=now_ms,
                n_closed_minutes=min(int(n_closed), len(closes)),
                params=params,
            )
            if pred is not None:
                pred["prediction_fallback_from"] = "sec_1s_unavailable"
    else:
        pred = naked_predict_live_dynamic(
            baseline=baseline,
            closes=closes,
            window_start_ms=window_start_ms,
            end_ts_ms=end_ts_ms,
            now_ms=now_ms,
            n_closed_minutes=min(int(n_closed), len(closes)),
            params=params,
        )

    if pred is None:
        out["action"] = "dynamic_prediction_unavailable"
        out["condition_id"] = cid
        out["n_closed_minutes_wallclock"] = int(n_closed)
        out["sec_dynamic_1s_attempted"] = bool(sec_dynamic_1s and not frozen_minute3_only)
        return out
    out["prediction"] = pred
    out["condition_id"] = cid
    out["window_start_ms"] = window_start_ms
    out["market_question"] = getattr(active, "question", "")[:120]

    if bare_formula_eval:
        out["action"] = "bare_formula_tick"
        return out

    pred_up = bool(pred["pred_up"])
    token_id = active.token_id_yes if pred_up else active.token_id_no
    book = poly.fetch_book(token_id)
    best_ask = book.get("best_ask")
    if best_ask is not None:
        fair_side = float(pred["p_up"]) if pred_up else float(1.0 - float(pred["p_up"]))
        edge_vs_best_ask = fair_side - float(best_ask)
        out["fair_prob_side"] = fair_side
        out["edge_vs_best_ask"] = edge_vs_best_ask
        out["ask_below_fair"] = bool(edge_vs_best_ask > 1e-9)
    else:
        out["fair_prob_side"] = None
        out["edge_vs_best_ask"] = None
        out["ask_below_fair"] = None

    conf = float(pred["confidence_on_pick"])
    out["quote_snapshot"] = {
        "condition_id": cid,
        "token_id_tail": token_id[-16:] if len(token_id) > 16 else token_id,
        "side": "UP" if pred_up else "DOWN",
        "window_start_ms": int(window_start_ms),
        "end_ts_ms": int(end_ts_ms),
        "now_ms": int(now_ms),
        "seconds_to_expiry": max(0.0, (int(end_ts_ms) - int(now_ms)) / 1000.0),
        "best_bid": book.get("best_bid"),
        "best_ask": book.get("best_ask"),
        "best_bid_size": book.get("best_bid_size"),
        "best_ask_size": book.get("best_ask_size"),
        "midpoint": book.get("midpoint"),
        "book_stale": book.get("stale"),
        "book_ts_ms": book.get("ts_ms"),
        "book_source": book.get("source"),
        "book_error": book.get("error"),
        "fair_prob_side": out.get("fair_prob_side"),
        "edge_vs_best_ask": out.get("edge_vs_best_ask"),
        "confidence_on_pick": conf,
        "entry_gate": None,
    }

    # confidence_min=0：不设概率阈值，预测后仅由盘口/到期/边缘等闸门决定是否挂单
    if float(confidence_min) > 0.0 and conf < float(confidence_min):
        out["action"] = "below_confidence_threshold"
        out["confidence_min"] = float(confidence_min)
        out["book"] = book
        return out

    if best_ask is None:
        out["action"] = "no_best_ask"
        out["book"] = book
        return out

    entry_rule = _dynamic_entry_rule_for_conf(conf)
    out["entry_gate"] = {
        "version": "confidence_tier_dynamic_cap_v1",
        "tiers": [
            {"min_conf": a, "max_ask": b, "min_edge": c}
            for a, b, c in NAKED_DYNAMIC_ENTRY_TIERS
        ],
        "selected": entry_rule,
    }
    if isinstance(out.get("quote_snapshot"), dict):
        out["quote_snapshot"]["entry_gate"] = out["entry_gate"]
    if entry_rule is None:
        out["action"] = "below_confidence_tier_min"
        out["condition_id"] = cid
        out["confidence_on_pick"] = conf
        out["confidence_tier_min_required"] = float(NAKED_DYNAMIC_ENTRY_TIERS[-1][0])
        out["book"] = book
        return out

    if float(best_ask) > float(entry_rule["max_ask"]) + 1e-15:
        out["action"] = "odds_above_dynamic_tier_cap"
        out["condition_id"] = cid
        out["confidence_on_pick"] = conf
        out["best_ask"] = float(best_ask)
        out["dynamic_tier_max_ask"] = float(entry_rule["max_ask"])
        out["book"] = book
        return out

    edge_now = float(out.get("edge_vs_best_ask") or 0.0)
    if edge_now < float(entry_rule["min_edge"]):
        out["action"] = "edge_below_dynamic_tier_min"
        out["condition_id"] = cid
        out["confidence_on_pick"] = conf
        out["best_ask"] = float(best_ask)
        out["edge_vs_best_ask"] = float(edge_now)
        out["min_edge_required"] = float(entry_rule["min_edge"])
        out["book"] = book
        return out

    odds_cap = _odds_cap_strict_below_for_confidence(conf)
    out["legacy_odds_cap_strict_below"] = float(odds_cap)
    out["legacy_odds_tier_would_block"] = bool(float(best_ask) >= float(odds_cap))

    if require_positive_edge and not bool(out["ask_below_fair"]):
        out["action"] = "no_positive_edge_vs_fair"
        out["condition_id"] = cid
        return out

    runtime = poly.runtime_cfg
    cross_ticks = max(0, int(getattr(runtime, "entry_buy_cross_ticks", 0)))
    tick = float(plat.price_tick)
    limit_price = float(best_ask) + cross_ticks * tick
    limit_price = min(max(limit_price, float(plat.buy_price_floor)), float(plat.price_extreme_max))
    limit_price = round(limit_price, 2)

    min_shares = float(plat.min_limit_order_shares)
    min_quote = float(plat.min_order_quote_usdc)
    equity_for_kelly: Optional[float] = None
    if use_kelly_sizing and yes_real_money:
        ok_ke, _msg_ke = poly.init_real_client()
        if ok_ke:
            equity_for_kelly = poly.fetch_account_equity_usdc()
    kelly_sizing_cfg = SizingCfg(
        kelly_fraction=max(1e-6, float(kelly_fraction)),
        max_stake_ratio=max(1e-6, min(1.0, float(kelly_max_stake_ratio))),
        min_stake_ratio=0.0,
        min_absolute_stake=max(1.0, float(min_quote)),
    )
    target_q, sizing_meta = naked_target_quote_with_kelly(
        use_kelly=bool(use_kelly_sizing),
        equity_usdc=equity_for_kelly,
        limit_price=float(limit_price),
        win_prob=float(conf),
        floor_quote_usdc=float(target_quote_usdc),
        plat_min_order_quote_usdc=float(min_quote),
        sizing_cfg=kelly_sizing_cfg,
    )
    target_q = max(float(target_q), float(min_quote))
    raw_shares = target_q / limit_price if limit_price > 0 else min_shares
    need_shares = _naked_round_shares_up(raw_shares, min_shares)
    size_quote = round(need_shares * limit_price, 2)
    step = 0.0001
    while size_quote + 1e-9 < min_quote and need_shares < 1e8:
        need_shares = round(need_shares + step, 4)
        need_shares = max(need_shares, min_shares)
        size_quote = round(need_shares * limit_price, 2)
    if size_quote < min_quote:
        size_quote = float(min_quote)

    use_fok_retries, entry_route = _naked_entry_use_fok_retries(
        limit_price=limit_price,
        confidence_min=float(confidence_min),
    )
    use_fok_retries, entry_route = _naked_gate_gtc_by_best_ask(
        use_fok_retries=use_fok_retries,
        entry_route=entry_route,
        best_ask=float(best_ask),
    )
    out["order_plan"] = {
        "side_polymarket": "UP" if pred_up else "DOWN",
        "token_id_tail": token_id[-16:] if len(token_id) > 16 else token_id,
        "limit_price": limit_price,
        "size_shares": need_shares,
        "size_quote_usdc": size_quote,
        "best_ask": float(best_ask),
        "entry_execution": "fok_only_retries" if use_fok_retries else "gtc_only",
        "entry_routing": entry_route,
    }
    if isinstance(out.get("quote_snapshot"), dict):
        out["quote_snapshot"]["limit_price"] = float(limit_price)
        out["quote_snapshot"]["size_shares"] = float(need_shares)
        out["quote_snapshot"]["size_quote_usdc"] = float(size_quote)
        out["quote_snapshot"]["entry_execution"] = "fok_only_retries" if use_fok_retries else "gtc_only"
    out["sizing"] = sizing_meta

    if not yes_real_money:
        out["action"] = "would_submit_dry_run_need_yes_real_money"
        return out

    if funds_manager is not None:
        ok_funds, reason_funds = funds_manager.check_new_entry(poly, st, float(size_quote))
        if not ok_funds:
            out["action"] = "funds_blocked"
            out["funds_block_reason"] = reason_funds
            return out

    ok_init, msg_init = poly.init_real_client()
    if not ok_init:
        out["action"] = "clob_init_failed"
        out["error"] = msg_init
        return out

    ticket: Optional[OrderTicket] = None
    coid_final = ""

    if use_fok_retries:
        attempts_log: list[dict[str, Any]] = []
        for attempt in range(int(NAKED_FOK_RETRY_MAX_ATTEMPTS)):
            coid = f"naked_third_digit:{cid[-12:]}:{int(time.time())}:{attempt}"
            ticket = poly.submit_order(
                side="BUY",
                token_id=token_id,
                price=limit_price,
                size_quote_usdc=float(size_quote),
                client_order_id=coid,
                size_shares=float(need_shares),
                entry_fok_first=True,
                entry_fok_fallback_gtc=False,
            )
            attempts_log.append(
                {
                    "attempt": attempt,
                    "client_order_id": coid,
                    "state": ticket.state.value,
                    "last_error": ticket.last_error,
                }
            )
            coid_final = coid
            if ticket.state == OrderState.FILLED:
                break
            if ticket.state == OrderState.DRY_RUN_SHADOW:
                break
            time.sleep(float(NAKED_FOK_RETRY_SLEEP_SEC))

        out["fok_retry_attempts"] = attempts_log

        lock_state = ticket is not None and ticket.state in (
            OrderState.FILLED,
            OrderState.DRY_RUN_SHADOW,
        )
        if not lock_state:
            out["action"] = "fok_no_fill_will_retry"
            out["ticket_state"] = ticket.state.value if ticket else None
            out["client_order_id"] = coid_final
            if ticket and ticket.state == OrderState.REJECTED:
                out["error"] = ticket.last_error
            return out
    else:
        coid_final = f"naked_third_digit:{cid[-12:]}:{int(time.time())}"
        ticket = poly.submit_order(
            side="BUY",
            token_id=token_id,
            price=limit_price,
            size_quote_usdc=float(size_quote),
            client_order_id=coid_final,
            size_shares=float(need_shares),
            entry_fok_first=False,
            entry_fok_fallback_gtc=True,
        )
        lock_state = ticket.state in (
            OrderState.FILLED,
            OrderState.SUBMITTED,
            OrderState.PARTIAL,
            OrderState.DRY_RUN_SHADOW,
        )
        if not lock_state:
            out["action"] = "gtc_post_failed_will_retry"
            out["ticket_state"] = ticket.state.value
            out["client_order_id"] = coid_final
            if ticket.state == OrderState.REJECTED:
                out["error"] = ticket.last_error
            return out

    assert ticket is not None
    st["last_attempt_condition_id"] = cid
    st["last_attempt_ts_ms"] = now_ms
    st["last_ticket_state"] = ticket.state.value
    
    # Track recent condition_ids for auto redeem
    recent_cids = st.get("recent_condition_ids", [])
    if cid not in recent_cids:
        recent_cids.append(cid)
    # Keep last 20
    if len(recent_cids) > 20:
        recent_cids = recent_cids[-20:]
    st["recent_condition_ids"] = recent_cids
    
    if ticket.state == OrderState.FILLED:
        filled_sz = float(ticket.filled_size_shares or ticket.size_shares or need_shares)
        cap_at_entry = float(_odds_cap_strict_below_for_confidence(conf))
        st[STATE_KEY_LOW_CONF_TP] = {
            "condition_id": cid,
            "token_id": token_id,
            "entry_avg_price": float(limit_price),
            "confidence_at_entry": float(conf),
            "odds_cap_at_entry": cap_at_entry,
            "end_ts_ms": int(end_ts_ms),
            "opened_ts_ms": now_ms,
            "last_remaining_shares": round(filled_sz, 4),
        }
    _save_state(state_path, st)
    if ticket.state in (OrderState.FILLED, OrderState.PARTIAL):
        if funds_manager is not None:
            funds_manager.save_after_equity_refresh(poly)
        else:
            _kv_sync_last_equity_after_trade(poly)

    out["action"] = "submitted"
    out["ticket_state"] = ticket.state.value
    out["client_order_id"] = coid_final
    if ticket.state == OrderState.REJECTED:
        out["error"] = ticket.last_error
    return out


def run_naked_third_digit_loop(
    *,
    model_json: Path,
    env_file: str | None,
    confidence_min: float,
    target_quote_usdc: float,
    poll_sec: float,
    run_once: bool,
    yes_real_money: bool,
    causal_decision_lag_sec: float,
    skip_seconds_before_expiry: float,
    state_json: Path,
    setup_logs: bool,
    max_market_rollovers: int | None = None,
    frozen_minute3_only: bool = False,
    require_positive_edge: bool = False,
    sec_dynamic_1s: bool = True,
    sec_path_stride: int = 1,
    bare_formula_eval: bool = False,
    log_p_rev_range: bool = False,
    use_funds_manager: bool = True,
    use_kelly_sizing: bool = False,
    kelly_fraction: float = 0.125,
    kelly_max_stake_ratio: float = 0.04,
    live_log_jsonl: Path | None = None,
    max_runtime_min: float = 0.0,
) -> None:
    if setup_logs:
        setup_logging(log_dir="logs", level="INFO")

    params = load_third_digit_dynamic_params(model_json)
    runtime_base = load_polymarket_runtime_cfg(env_file=env_file)
    if yes_real_money:
        ok_allow, reason_allow = is_real_order_allowed(runtime_base)
        if not ok_allow:
            raise SystemExit(f"[naked-third-digit-live] 实盘未允许: {reason_allow}")

    runtime = replace(
        runtime_base,
        order_poll_max_wait_sec=max(45.0, float(runtime_base.order_poll_max_wait_sec)),
    )
    poly = PolymarketClient(runtime_cfg=runtime, platform=POLYMARKET_PLATFORM)
    funds: Optional[NakedFundsManager] = None
    if yes_real_money:
        ok_init0, msg_init0 = poly.init_real_client()
        if not ok_init0:
            raise SystemExit(f"[naked-third-digit-live] CLOB 初始化失败: {msg_init0}")
        if use_funds_manager:
            funds = NakedFundsManager.from_runtime(
                StateStore(db_path=str(runtime.state_db_path)),
                daily_max_loss_usdc=float(runtime.daily_max_loss_usdc),
                daily_drawdown_stop=float(getattr(runtime, "naked_daily_drawdown_stop", 0.05)),
            )
            funds.load_restore(int(time.time() * 1000))
            funds.save_after_equity_refresh(poly)
        else:
            _kv_sync_last_equity_after_trade(poly)

    resolver = MarketResolver(
        cfg=MarketResolverCfg(
            url=runtime.market_resolver_url_template,
            keywords=tuple(runtime.market_search_keywords),
            horizon_minutes=int(runtime.market_horizon_minutes),
            refresh_interval_sec=float(runtime.market_refresh_interval_sec),
            request_timeout_sec=float(runtime.http_timeout_sec),
            user_agent=runtime.http_user_agent,
        )
    )

    print(
        json.dumps(
            {
                "phase": "runner_started",
                "mode": "once" if run_once else "continuous_poll",
                "poll_sec": float(poll_sec),
                "model_json": str(model_json),
                "model_schema": getattr(params, "schema_version", None) or type(params).__name__,
                "prefix_bucket_model": hasattr(params, "bucket_for_prefix"),
                "confidence_min": float(confidence_min),
                "yes_real_money": bool(yes_real_money),
                "note": (
                    "每窗在第3分钟序列确认后 survival；默认 Binance 1s 已收盘路径动态 H（失败退回分钟结点）"
                    if (not frozen_minute3_only and sec_dynamic_1s)
                    else (
                        "minute_dynamic_only：仅用分钟收盘结点"
                        if not frozen_minute3_only
                        else "frozen_minute3_only：固定120s快照"
                    )
                ),
                "max_market_rollovers": max_market_rollovers,
                "frozen_minute3_only": bool(frozen_minute3_only),
                "require_positive_edge": bool(require_positive_edge),
                "sec_dynamic_1s": bool(sec_dynamic_1s),
                "sec_path_stride": int(sec_path_stride),
                "bare_formula_eval": bool(bare_formula_eval),
                "log_p_rev_range": bool(log_p_rev_range),
                "funds_manager": bool(yes_real_money and use_funds_manager),
                "kelly_sizing": bool(use_kelly_sizing),
                "kelly_fraction": float(kelly_fraction),
                "kelly_max_stake_ratio": float(kelly_max_stake_ratio),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    prev_cid: str | None = None
    rollovers = 0
    last_pred_by_cid: dict[str, dict[str, Any]] = {}
    window_start_by_cid: dict[str, int] = {}
    scored_matches: list[bool] = []
    p_rev_min_max: dict[str, tuple[float, float]] = {}
    t0_ms = int(time.time() * 1000)

    while True:
        if float(max_runtime_min) > 0.0:
            elapsed_min = (int(time.time() * 1000) - t0_ms) / 60_000.0
            if elapsed_min >= float(max_runtime_min):
                print(
                    json.dumps(
                        {
                            "phase": "runner_stopped",
                            "reason": "max_runtime_reached",
                            "elapsed_min": float(elapsed_min),
                            "max_runtime_min": float(max_runtime_min),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                return
        if yes_real_money and not bare_formula_eval:
            tp_evt = run_low_conf_take_profit_tick(poly=poly, state_path=state_json, yes_real_money=yes_real_money)
            if tp_evt is not None:
                print(json.dumps(tp_evt, ensure_ascii=False, default=str), flush=True)
                if live_log_jsonl is not None:
                    _append_jsonl(
                        live_log_jsonl,
                        {
                            "kind": "naked_live_tp",
                            "yes_real_money": bool(yes_real_money),
                            "confidence_min": float(confidence_min),
                            "event": tp_evt,
                        },
                    )
                if tp_evt.get("action") in (
                    "tp_flat_done",
                    "flat_cleared_watch",
                    "dust_cleared_watch",
                    "tp_flat_dust_remainder",
                    "watch_cleared_invalid",
                ):
                    if funds is not None:
                        funds.save_after_equity_refresh(poly)
                    else:
                        _kv_sync_last_equity_after_trade(poly)
            
            # Auto redeem winning positions
            redeem_evt = _run_auto_redeem_tick(poly=poly, state_path=state_json, runtime_cfg=runtime)
            if redeem_evt is not None:
                print(json.dumps(redeem_evt, ensure_ascii=False, default=str), flush=True)
                if live_log_jsonl is not None:
                    _append_jsonl(
                        live_log_jsonl,
                        {
                            "kind": "naked_live_redeem",
                            "yes_real_money": bool(yes_real_money),
                            "event": redeem_evt,
                        },
                    )
                if redeem_evt.get("redeemed_count", 0) > 0:
                    if funds is not None:
                        funds.save_after_equity_refresh(poly)
                    else:
                        _kv_sync_last_equity_after_trade(poly)

        rep = run_naked_third_digit_tick(
            params=params,
            poly=poly,
            resolver=resolver,
            confidence_min=confidence_min,
            target_quote_usdc=target_quote_usdc,
            causal_decision_lag_sec=causal_decision_lag_sec,
            state_path=state_json,
            yes_real_money=yes_real_money,
            skip_seconds_before_expiry=float(skip_seconds_before_expiry),
            frozen_minute3_only=bool(frozen_minute3_only),
            require_positive_edge=bool(require_positive_edge),
            sec_dynamic_1s=bool(sec_dynamic_1s),
            sec_path_stride=int(sec_path_stride),
            bare_formula_eval=bool(bare_formula_eval),
            funds_manager=funds,
            use_kelly_sizing=bool(use_kelly_sizing),
            kelly_fraction=float(kelly_fraction),
            kelly_max_stake_ratio=float(kelly_max_stake_ratio),
        )
        print(json.dumps(rep, ensure_ascii=False, default=str), flush=True)
        if live_log_jsonl is not None:
            _append_jsonl(
                live_log_jsonl,
                {
                    "kind": "naked_live_tick",
                    "schema_version": "naked_live_tick_v2",
                    "yes_real_money": bool(yes_real_money),
                    "confidence_min": float(confidence_min),
                    "target_quote_usdc": float(target_quote_usdc),
                    "poll_sec": float(poll_sec),
                    "quote_snapshot": rep.get("quote_snapshot"),
                    "entry_gate": rep.get("entry_gate"),
                    "book": rep.get("book"),
                    "order_plan": rep.get("order_plan"),
                    "prediction": rep.get("prediction"),
                    "action": rep.get("action"),
                    "ticket_state": rep.get("ticket_state"),
                    "client_order_id": rep.get("client_order_id"),
                    "rep": rep,
                },
            )

        cid_rep = rep.get("condition_id")
        if isinstance(cid_rep, str) and cid_rep:
            pred_obj = rep.get("prediction")
            if (
                log_p_rev_range
                and isinstance(pred_obj, dict)
                and pred_obj.get("p_rev") is not None
            ):
                try:
                    pr = float(pred_obj["p_rev"])
                    if cid_rep in p_rev_min_max:
                        lo, hi = p_rev_min_max[cid_rep]
                        p_rev_min_max[cid_rep] = (min(lo, pr), max(hi, pr))
                    else:
                        p_rev_min_max[cid_rep] = (pr, pr)
                except (TypeError, ValueError):
                    pass
            if isinstance(pred_obj, dict):
                last_pred_by_cid[cid_rep] = pred_obj
            ws_ms = rep.get("window_start_ms")
            if ws_ms is not None:
                window_start_by_cid[cid_rep] = int(ws_ms)

            if prev_cid is not None and cid_rep != prev_cid:
                verdict: dict[str, Any] = {
                    "phase": "window_verdict",
                    "completed_condition_tail": prev_cid[-12:],
                }
                ws_prev = window_start_by_cid.get(prev_cid)
                pred_last = last_pred_by_cid.get(prev_cid)
                if ws_prev is not None and isinstance(pred_last, dict):
                    truth = fetch_binance_true_up_window_end(
                        symbol=params.symbol,
                        window_start_ms=int(ws_prev),
                    )
                    if truth is not None:
                        true_up, bl, c5 = truth
                        pred_up = bool(pred_last.get("pred_up"))
                        ok_dir = pred_up == true_up
                        scored_matches.append(ok_dir)
                        verdict.update(
                            {
                                "window_start_ms": int(ws_prev),
                                "pred_up": pred_up,
                                "true_up_binance": true_up,
                                "direction_match": ok_dir,
                                "p_up_last_snapshot": pred_last.get("p_up"),
                                "confidence_last_snapshot": pred_last.get("confidence_on_pick"),
                                "predict_engine": pred_last.get("predict_engine"),
                                "baseline_open_m1": bl,
                                "close_min5": c5,
                            }
                        )
                    else:
                        verdict["error"] = "binance_truth_fetch_failed"
                else:
                    verdict["note"] = "no_stored_prediction_or_window_start"

                if log_p_rev_range and prev_cid in p_rev_min_max:
                    lo, hi = p_rev_min_max[prev_cid]
                    last_pr = None
                    if isinstance(pred_last, dict) and pred_last.get("p_rev") is not None:
                        try:
                            last_pr = float(pred_last["p_rev"])
                        except (TypeError, ValueError):
                            last_pr = None
                    verdict["p_rev_range"] = {"min": float(lo), "max": float(hi), "last": last_pr}
                    del p_rev_min_max[prev_cid]

                print(json.dumps(verdict, ensure_ascii=False, default=str), flush=True)

                rollovers += 1
                print(
                    json.dumps(
                        {
                            "phase": "market_rollover",
                            "rollovers_so_far": rollovers,
                            "from_condition_tail": prev_cid[-12:],
                            "to_condition_tail": cid_rep[-12:],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            prev_cid = cid_rep

        if max_market_rollovers is not None and rollovers >= int(max_market_rollovers):
            acc_summary: dict[str, Any] = {}
            if scored_matches:
                acc_summary["n_windows_scored"] = len(scored_matches)
                acc_summary["direction_accuracy_on_rollovers"] = float(sum(1 for x in scored_matches if x) / len(scored_matches))
            print(
                json.dumps(
                    {
                        "phase": "runner_stopped",
                        "reason": "max_market_rollovers_reached",
                        "market_rollovers": rollovers,
                        **acc_summary,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return

        if run_once:
            return
        time.sleep(max(1.0, float(poll_sec)))
