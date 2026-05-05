"""
空跑信号事件富化: 在信号落库前附加 Polymarket 盘口与「是否可能成交」估计.

模块化:
    - 与 ShadowSignalEngine 解耦 (通过回调写入 dict)
    - 使用与回测一致的报价函数 ``quote_for_trend`` / ``quote_for_reversal``
      (同一 PolymarketCfg 摩擦模型), **不修改策略阈值或信号逻辑**
    - 仅只读 ``PolymarketClient.fetch_book``, 不下单

输出写入 event["polymarket"]:
    - trade_quote: 模型给出的有效价 / payoff 等
    - trade_direction: up → YES token, down → NO token
    - book: 顶簿快照
    - opportunity: likely_executable + 原因说明
"""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any, Optional

from ..backtest.polymarket import PolymarketCfg, quote_for_reversal, quote_for_trend
from .logging_setup import get_logger

logger = get_logger(__name__)


def enrich_shadow_event_with_polymarket(
    event: dict[str, Any],
    *,
    poly_client: Any,
    active_market: Optional[Any],
    stake_quote_usdc: float,
    book_max_staleness_sec: float,
) -> None:
    """就地修改 ``event``, 追加 key ``polymarket``。任何失败写入 error, 不 raise。"""
    try:
        _do_enrich(
            event,
            poly_client=poly_client,
            active_market=active_market,
            stake_quote_usdc=stake_quote_usdc,
            book_max_staleness_sec=book_max_staleness_sec,
        )
    except Exception as e:
        logger.warning("polymarket enrich failed: %s", e)
        event["polymarket"] = {"error": str(e), "type": type(e).__name__}


def _do_enrich(
    event: dict[str, Any],
    *,
    poly_client: Any,
    active_market: Optional[Any],
    stake_quote_usdc: float,
    book_max_staleness_sec: float,
) -> None:
    if active_market is None:
        event["polymarket"] = {
            "error": "no_active_market",
            "hint": "Gamma 未匹配到当前 5min BTC 合约; 仅币安信号, 无盘口对照",
        }
        return

    trigger = event.get("trigger_pattern") or ""
    if not trigger or len(trigger) < 1:
        event["polymarket"] = {"error": "invalid_trigger", "trigger": trigger}
        return
    # third_digit 惯例: 用末位确定虚拟 trigger
    trig_virtual = "111" if trigger[-1] == "1" else "000"

    side_sig = str(event.get("side") or "").upper()
    if side_sig not in ("TREND", "REVERSAL"):
        event["polymarket"] = {"error": "invalid_signal_side", "side": side_sig}
        return

    baseline = float(event.get("baseline_price") or 0.0)
    current = float(event.get("current_price") or 0.0)
    if baseline <= 0:
        event["polymarket"] = {"error": "invalid_baseline"}
        return

    d_signed_pct = (current - baseline) / baseline * 100.0
    t_left = max(0, int(float(event.get("t_remaining_sec") or 0)))
    baseline_rev = float(event.get("baseline_reversal_prob") or 0.166)

    cfg = PolymarketCfg()
    stake = max(0.2, float(stake_quote_usdc))

    if side_sig == "REVERSAL":
        q = quote_for_reversal(
            trigger=trig_virtual,
            d_signed_pct=d_signed_pct,
            seconds_left=t_left,
            prior_reversal=baseline_rev,
            cfg=cfg,
            stake_quote=stake,
        )
    else:
        q = quote_for_trend(
            trigger=trig_virtual,
            d_signed_pct=d_signed_pct,
            seconds_left=t_left,
            prior_reversal=baseline_rev,
            cfg=cfg,
            stake_quote=stake,
        )

    poly_side = str(q.side).upper()
    direction = "up" if poly_side == "UP" else "down"
    token_id = (
        active_market.token_id_yes if direction == "up" else active_market.token_id_no
    )

    book = poly_client.fetch_book(token_id)
    # Also fetch the opposite token's book for EV calculation
    other_token_id = active_market.token_id_no if direction == "up" else active_market.token_id_yes
    other_book = poly_client.fetch_book(other_token_id)
    eff = float(q.effective_price)

    now_ms = int(time.time() * 1000)
    book_ts = int(book.get("ts_ms") or now_ms)
    book_age_sec = max(0.0, (now_ms - book_ts) / 1000.0)

    best_ask = book.get("best_ask")
    best_bid = book.get("best_bid")
    ask_sz = book.get("best_ask_size")
    bid_sz = book.get("best_bid_size")

    tick = 0.01
    tol = tick * 3
    stale = bool(book.get("stale")) or book_age_sec > float(book_max_staleness_sec)
    err = book.get("error")

    ref_px = float(best_ask) if best_ask is not None else eff
    need_shares = stake / max(ref_px, tick)

    price_ok = bool(best_ask is not None and float(best_ask) <= eff + tol)
    depth_ok = bool(
        ask_sz is not None and float(ask_sz) >= max(1.0, need_shares * 0.25)
    )

    reasons: list[str] = []
    if err:
        reasons.append(f"book_fetch:{err}")
    if best_ask is None:
        reasons.append("no_best_ask")
    elif not price_ok:
        reasons.append(f"ask_too_high ask={best_ask:.4f} eff_cap={eff + tol:.4f}")
    if ask_sz is not None and not depth_ok:
        reasons.append(f"thin_depth need~{need_shares:.1f}_shares have={ask_sz}")
    elif ask_sz is None:
        reasons.append("no_ask_size")
    if stale:
        reasons.append(f"stale_or_old book_age_sec={book_age_sec:.2f}")

    likely = price_ok and depth_ok and not stale and err is None

    event["polymarket"] = {
        "condition_id": getattr(active_market, "condition_id", None),
        "market_end_ts_ms": getattr(active_market, "end_ts_ms", None),
        "quote_model": "backtest.polymarket",
        "trade_quote": {k: _json_safe(getattr(q, k)) for k in (
            "side", "raw_price", "effective_price", "payoff_gross",
            "fill_ratio", "slippage_bps", "rebound", "oracle_drag",
        )},
        "mapping": {
            "signal_engine_side": side_sig,
            "trigger_pattern": trigger,
            "polymarket_side": poly_side,
            "trade_direction": direction,
            "token_role": "YES" if direction == "up" else "NO",
        },
        "token_id_suffix": token_id[-12:] if len(token_id) > 12 else token_id,
        "best_ask_up": other_book.get("best_ask") if direction == "down" else best_ask,
        "best_ask_down": other_book.get("best_ask") if direction == "up" else best_ask,
        "best_ask_size_up": other_book.get("best_ask_size") if direction == "down" else ask_sz,
        "best_ask_size_dn": other_book.get("best_ask_size") if direction == "up" else ask_sz,
        "book": {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "best_bid_size": bid_sz,
            "best_ask_size": ask_sz,
            "midpoint": book.get("midpoint"),
            "source": book.get("source"),
            "stale": book.get("stale"),
            "ts_ms": book_ts,
            "book_age_sec": round(book_age_sec, 3),
            "error": err,
        },
        "opportunity": {
            "check_stake_usdc": stake,
            "likely_executable": likely,
            "reason": "; ".join(reasons) if reasons else "ok",
            "spread": (
                round(float(best_ask) - float(best_bid), 4)
                if best_bid is not None and best_ask is not None
                else None
            ),
        },
    }


def _json_safe(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 8)
    return v
