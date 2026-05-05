"""
Polymarket 限价买单合规尺寸 (模块化).

规则 (与产品文档一致):
    - 股数 >= min_shares (默认 5)
    - 名义金额 >= min_quote_usdc (默认 $1)
    - 股数 >= 凯利公式换算股数 (向上取整到 share_tick)
    - 名义不得超过可用余额; 若按规则计算后买不起 min 股数则放弃 (返回 None)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CompliantBuySize:
    """合规后的限价买单参数 (买入侧)."""

    size_shares: float
    size_quote_usdc: float
    best_ask: float


def _ceil_shares_2dp(x: float) -> float:
    return math.ceil(max(0.0, x) * 100.0) / 100.0


def _floor_shares_2dp(x: float) -> float:
    return math.floor(max(0.0, x) * 100.0) / 100.0


def compute_compliant_limit_buy(
    *,
    kelly_quote_usdc: float,
    best_ask: float,
    available_balance_usdc: float,
    min_shares: float = 5.0,
    min_quote_usdc: float = 1.0,
    share_tick: float = 0.01,
) -> Optional[CompliantBuySize]:
    """
    根据凯利金额与最优卖价计算满足最低手数与最低名义的下单股数 / USDC 金额.

    若可用余额不足以支付至少 min_shares 且满足 min_quote_usdc, 返回 None.
    """
    if not math.isfinite(best_ask) or best_ask <= 0:
        return None
    if not math.isfinite(kelly_quote_usdc) or kelly_quote_usdc <= 0:
        return None
    if not math.isfinite(available_balance_usdc) or available_balance_usdc <= 0:
        return None

    kelly_shares = kelly_quote_usdc / best_ask
    step = max(share_tick, 1e-6)
    kelly_shares_ceil = _ceil_shares_2dp(kelly_shares)
    if kelly_shares_ceil < step:
        kelly_shares_ceil = step

    shares = max(kelly_shares_ceil, float(min_shares))
    shares = math.ceil(shares / step) * step
    shares = round(shares, 8)

    notional = shares * best_ask
    if notional + 1e-9 < float(min_quote_usdc):
        need_sh = _ceil_shares_2dp(float(min_quote_usdc) / best_ask)
        need_sh = math.ceil(need_sh / step) * step
        shares = max(shares, need_sh)
        shares = round(shares, 8)
        notional = shares * best_ask

    if notional > available_balance_usdc + 1e-6:
        max_sh = _floor_shares_2dp(available_balance_usdc / best_ask)
        max_sh = math.floor(max_sh / step) * step
        if max_sh + 1e-9 < float(min_shares):
            return None
        shares = min(shares, max_sh)
        notional = shares * best_ask
        if notional + 1e-9 < float(min_quote_usdc):
            return None

    notional = round(notional, 2)
    return CompliantBuySize(size_shares=float(shares), size_quote_usdc=float(notional), best_ask=float(best_ask))
