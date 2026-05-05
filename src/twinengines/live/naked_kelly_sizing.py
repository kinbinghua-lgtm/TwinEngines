"""
naked-third-digit-live 可选：保守分数 Kelly 名义（随净值缓慢放大，带硬上限）。

- 胜率侧用 prediction 的 confidence_on_pick（押边概率）
- 净赔率 b = (1 - limit_price) / limit_price（按限价买入、到期 0/1 兑付）
- 核心公式见 risk.sizing.stake_for_trade（默认 1/4 Kelly 可改为更保守的 kelly_fraction）
"""

from __future__ import annotations

from typing import Any, Optional

from ..risk.sizing import SizingCfg, stake_for_trade


def naked_target_quote_with_kelly(
    *,
    use_kelly: bool,
    equity_usdc: Optional[float],
    limit_price: float,
    win_prob: float,
    floor_quote_usdc: float,
    plat_min_order_quote_usdc: float,
    sizing_cfg: SizingCfg,
) -> tuple[float, dict[str, Any]]:
    """
    返回 (目标名义 USDC, 诊断 dict)。

    - 未启用 Kelly / 无有效净值：用 max(平台最小, floor)
    - 启用 Kelly 且 stake_for_trade 为正：max(平台最小, floor, Kelly 额)
    - Kelly 为 0（模型侧无正比例）：仍用 floor，避免「有信号却永远 0 单」
    """
    min_q = max(float(plat_min_order_quote_usdc), float(floor_quote_usdc))
    meta: dict[str, Any] = {"mode": "fixed_floor", "floor": floor_quote_usdc, "min_quote": plat_min_order_quote_usdc}

    if not use_kelly or equity_usdc is None or not float(equity_usdc) > 0:
        return float(min_q), meta

    lp = float(limit_price)
    if lp <= 1e-12 or lp >= 1.0 - 1e-12:
        net_b = 0.0
    else:
        net_b = (1.0 - lp) / lp

    k_stake = stake_for_trade(
        portfolio_equity=float(equity_usdc),
        win_prob=float(win_prob),
        net_payoff=float(net_b),
        cfg=sizing_cfg,
    )
    meta.update(
        {
            "mode": "kelly",
            "equity": float(equity_usdc),
            "limit_price": lp,
            "net_payoff_b": net_b,
            "kelly_stake_usdc": float(k_stake),
            "kelly_fraction": sizing_cfg.kelly_fraction,
            "max_stake_ratio": sizing_cfg.max_stake_ratio,
        }
    )

    if k_stake > 0:
        out = max(min_q, float(k_stake))
        meta["target_quote_usdc"] = out
        return float(out), meta

    out = float(min_q)
    meta["mode"] = "kelly_zero_use_floor"
    meta["target_quote_usdc"] = out
    return out, meta
