"""
仓位 sizing。

提供 Kelly 与分数 Kelly:
    Kelly fraction f* = p - q/b
    其中 p 为胜率, q=1-p, b 为净赔率 (payoff / stake)

实战中绝不使用全 Kelly (方差爆炸), 默认使用 1/4 Kelly (kelly_fraction=0.25)。
还会施加上下限裁剪。

小资金兼容性 (size-aware):
    - min_absolute_stake: 实盘最小可下单金额, 低于此值返回 0 (跳过信号)
    - share_quantization: 量化到最小 share 单位 (Polymarket 1 share = $0.02-$0.98)

防御性约束 (实盘安全审查后加入, 不改变策略行为):
    - kelly_optimal_ratio 输出硬裁剪到 [0, 1.0], 避免极端胜率/payoff 下的数值放大
    - kelly_max_cap: 全 Kelly 比例硬上限 (默认 1.0, 极端值兜底)
    - 当 kelly_optimal_ratio = 0 时, **绝不使用 min_stake_ratio 强行入场**
      (旧代码会在 Kelly=0 但 min_stake_ratio>0 时下单, 等于负 EV 赌博)
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SizingCfg:
    kelly_fraction: float = 0.25
    min_stake_ratio: float = 0.0
    max_stake_ratio: float = 0.10              # 单笔本金占组合净值最大比例
    min_absolute_stake: float = 0.20           # 实盘最小下单 USDC (Polymarket: 1 share ≈ $0.02 起, 留余地)
    share_quantization: float = 0.01           # 1 cent 取整 (Polymarket 内部精度)
    kelly_max_cap: float = 1.0                 # 全 Kelly f* 硬上限 (兜底, 防极端 payoff/胜率)


def kelly_optimal_ratio(win_prob: float, net_payoff: float) -> float:
    """单笔最优 Kelly 比例 (硬裁剪到 [0, 1])。net_payoff = b。"""
    if net_payoff is None or not math.isfinite(net_payoff) or net_payoff <= 0:
        return 0.0
    if win_prob is None or not math.isfinite(win_prob):
        return 0.0
    p = max(0.0, min(1.0, float(win_prob)))
    q = 1.0 - p
    f = p - q / net_payoff
    if not math.isfinite(f):
        return 0.0
    return max(0.0, min(1.0, f))


def stake_for_trade(
    *,
    portfolio_equity: float,
    win_prob: float,
    net_payoff: float,
    cfg: SizingCfg = SizingCfg(),
) -> float:
    """
    返回本次交易应使用的本金（USDC 绝对额）。

    返回 0 表示当前不入场:
        - 资金 <= 0 或非法
        - kelly 比例 = 0 (无正期望) — **不会因 min_stake_ratio>0 强行入场**
        - 量化后金额 < min_absolute_stake (实盘无法下单)
    """
    if portfolio_equity is None or not math.isfinite(portfolio_equity) or portfolio_equity <= 0:
        return 0.0

    f_full = kelly_optimal_ratio(win_prob, net_payoff)
    if f_full <= 0.005:  # Kelly 低于 0.5% → 正期望不够，不下注
        return 0.0

    cap = max(0.0, min(1.0, cfg.kelly_max_cap))
    f_full = min(f_full, cap)

    f = f_full * max(0.0, cfg.kelly_fraction)
    upper = max(0.0, cfg.max_stake_ratio)
    lower = max(0.0, min(cfg.min_stake_ratio, upper))
    f = max(lower, min(upper, f))

    raw = portfolio_equity * f
    if not math.isfinite(raw) or raw <= 0:
        return 0.0

    if cfg.share_quantization > 0:
        eps = cfg.share_quantization * 1e-6
        n = int((raw + eps) / cfg.share_quantization)
        raw = n * cfg.share_quantization

    if raw < cfg.min_absolute_stake:
        if f_full >= 0.02 and cfg.min_absolute_stake <= portfolio_equity * cfg.max_stake_ratio:
            raw = float(cfg.min_absolute_stake)
        else:
            return 0.0
    return float(raw)
