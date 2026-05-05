"""
Polymarket 二元期权摩擦模型 + 市场报价模型（生产参数）。

报价模型 v2 修正:
    旧 sigmoid(k*d) 仅基于即时偏离 d 推导市场胜率, 在 d≈0 时会给出 ≈0.5,
    与 Polymarket 实际现象不符（看到 111 三连后, 胜方价格已被推至 0.7-0.9）。

    新模型: 市场隐含反转概率 = prior_reversal * exp(-k * d_abs_pct)
    - prior_reversal = 历史 baseline 反转率 (例如 0.18)
    - d 越大, 离反转触发越远, 市场反转报价越低
    - 在 d=0 时 raw_price_reversal = prior_reversal, raw_price_trend = 1 - prior_reversal

成本拆分（每笔交易, 单位均为本金占比）:
    1. 入场滑点 (slippage_bps): top-of-book + 深度击穿
    2. 撤单回弹 (rebound): 越靠近窗口末越显著, 非线性
    3. UMA / 协议手续费 (uma_fee_rate): 仅对净利润抽取
    4. Maker rebate (maker_rebate_rate): 部分市场对挂单方有反向 rebate
    5. Polygon gas (gas_cost_quote): 入场+出场固定成本
    6. Chainlink 锚定 drag (oracle_basis_drag): 极端波动时与 Binance 价差
    7. 深度折扣 (depth_fill_ratio): 大额单实际成交比例

经验数值来源 (公开数据观察, 2024-2025):
    - top-of-book 滑点: 5–15 bp 常态
    - 深度击穿: 50–250 bp / $5k 单
    - 临窗末撤单回弹: 平方时变, 30s 内可达 1.5%–4%
    - UMA 协议费: 部分市场 2% on profit, 部分 0%
    - Polygon gas: $0.01–0.5/tx
    - Chainlink basis: 常态 5–20 bp, 极端 50–200 bp
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from math import exp


def _safe_payoff_from_price(eff_price: float) -> float:
    """从有效成交价反推 net payoff (= 1/p - 1), 含极端值兜底, 防 ZeroDivision/Inf。"""
    if eff_price is None or not math.isfinite(eff_price) or eff_price <= 0:
        return 0.0
    payoff = 1.0 / eff_price - 1.0
    if not math.isfinite(payoff):
        return 0.0
    return max(0.0, payoff)


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = exp(-x)
        return 1.0 / (1.0 + z)
    z = exp(x)
    return z / (1.0 + z)


@dataclass(frozen=True)
class PolymarketCfg:
    """
    生产级保守默认参数。所有数值偏向悲观, 实盘前应用自有盘口数据校准。

    资金规模缩放 (size-aware):
        depth_*_per_unit 系列只有当订单规模 (USDC) 超过 typical_order_quote 时才生效。
        小单 (例如 $1) 几乎不击穿盘口, 因此 depth slippage = 0, fill_ratio ≈ 1。
    """

    sensitivity_k: float = 30.0

    # 顶簿固定滑点 (即使 $0.5 单也要承担, 来源于 best bid/ask spread)
    top_of_book_slippage_bps: float = 25.0

    # 深度击穿: 仅当订单规模超过 typical_order_quote 时按比例叠加
    depth_slippage_bps_per_unit: float = 120.0
    typical_order_units: float = 0.5            # legacy, 用于 $10k 级别默认计算
    typical_order_quote: float = 50.0           # 单位 USDC: 低于此值视为"小单不冲击市场"

    # 撤单回弹: 经验上 30s 末段累积 ≈ 2-4%
    rebound_base_per_sec_squared: float = 2.5e-4
    rebound_max: float = 0.06

    # UMA / 协议费 (Polymarket 部分市场 2% on profit)
    uma_fee_rate: float = 0.02
    maker_rebate_rate: float = 0.0

    # Polygon gas (USD): 入场+出场各一次
    gas_cost_quote: float = 0.20

    # Chainlink 与币安瞬时价差 drag
    oracle_basis_drag_normal: float = 0.0015
    oracle_basis_drag_extreme: float = 0.018
    extreme_d_threshold: float = 0.7

    # 深度折扣 (不能全量成交) — 同样 size-aware
    depth_fill_ratio: float = 0.75              # 大单 (>= typical_order_quote) 的成交比例
    small_order_fill_ratio: float = 0.98        # 小单近乎全成交

    min_price: float = 0.02
    max_price: float = 0.98

    # 实盘最小可交易额 (Polymarket 单 share 价值 $0.02-$0.98, 1 share 即可下单)
    min_order_quote: float = 0.20
    # 真实摩擦成本模块
    friction_mode: str = "off"   # off | maker | taker
    taker_slippage_bps: float = 300.0  # 2-5% 建议区间, 默认 3%


@dataclass(frozen=True)
class TradeQuote:
    side: str
    raw_price: float
    effective_price: float
    payoff_gross: float
    fill_ratio: float
    slippage_bps: float
    rebound: float
    oracle_drag: float


def market_up_probability(d_signed_pct: float, cfg: PolymarketCfg) -> float:
    """旧式 sigmoid 报价 (向后兼容, 不推荐用于回测主路径)。"""
    return _sigmoid(cfg.sensitivity_k * d_signed_pct / 100.0)


def market_implied_reversal_prob(
    *,
    trigger: str,
    d_signed_pct: float,
    prior_reversal: float,
    cfg: PolymarketCfg,
) -> float:
    """
    市场对反转的隐含概率 = prior * exp(-k * d_abs).

    d 越大 (顺势方向延伸), 反转报价越低; d 接近 0 时回归 prior。
    若 d 与 trigger 方向相反 (异常局面), 反转报价反而增大。
    """
    sign = 1.0 if trigger == "111" else -1.0
    aligned = d_signed_pct * sign
    if aligned >= 0:
        decay = exp(-cfg.sensitivity_k * (aligned / 100.0))
        p = prior_reversal * decay
    else:
        p = prior_reversal * (1.0 + cfg.sensitivity_k * abs(aligned) / 100.0)
    return max(min(p, cfg.max_price), cfg.min_price)


def _slippage_bps(cfg: PolymarketCfg, stake_quote: float | None = None) -> float:
    """
    顶簿+深度滑点。

    若提供 stake_quote (单位 USDC):
        - stake_quote <= typical_order_quote: 仅顶簿滑点 (小单不冲击市场)
        - stake_quote >  typical_order_quote: 按超出比例叠加深度滑点
    若 stake_quote 为 None: 退回 legacy ($10k 级别) 计算
    """
    if stake_quote is None:
        return cfg.top_of_book_slippage_bps + cfg.depth_slippage_bps_per_unit * cfg.typical_order_units
    if stake_quote <= cfg.typical_order_quote:
        return cfg.top_of_book_slippage_bps
    excess_units = (stake_quote - cfg.typical_order_quote) / max(cfg.typical_order_quote, 1.0)
    return cfg.top_of_book_slippage_bps + cfg.depth_slippage_bps_per_unit * excess_units


def _fill_ratio(cfg: PolymarketCfg, stake_quote: float | None = None) -> float:
    """小单近乎全成交, 大单按 depth_fill_ratio 折扣。"""
    if stake_quote is None:
        return cfg.depth_fill_ratio
    if stake_quote <= cfg.typical_order_quote:
        return cfg.small_order_fill_ratio
    return cfg.depth_fill_ratio


def _rebound(cfg: PolymarketCfg, seconds_left: int) -> float:
    elapsed = max(0, 120 - seconds_left)
    raw = cfg.rebound_base_per_sec_squared * (elapsed**2)
    return min(raw, cfg.rebound_max)


def _oracle_drag(cfg: PolymarketCfg, d_abs_pct: float) -> float:
    if d_abs_pct >= cfg.extreme_d_threshold:
        return cfg.oracle_basis_drag_extreme
    return cfg.oracle_basis_drag_normal


def _adjust_price(raw_price: float, slippage_bps: float, rebound: float, cfg: PolymarketCfg) -> float:
    """
    实盘安全约束:
        min_price 严禁配置为 <= 0 (会导致 payoff = 1/p - 1 溢出/除零)。
        本函数同时强制最低价为 max(cfg.min_price, 0.01) 兜底, 与 Polymarket
        tick 0.01 一致。
    """
    p = raw_price * (1.0 + slippage_bps / 10_000.0) + rebound
    floor = cfg.min_price if cfg.min_price > 0 else 0.01
    return max(min(p, cfg.max_price), floor)


def quote_for_reversal(
    *,
    trigger: str,
    d_signed_pct: float,
    seconds_left: int,
    prior_reversal: float = 0.18,
    cfg: PolymarketCfg = PolymarketCfg(),
    stake_quote: float | None = None,
) -> TradeQuote:
    """
    反转方向报价 v2 (基于 prior_reversal)。
    传入 stake_quote 时, 摩擦按订单规模动态缩放 (小单→近无冲击)。
    """
    raw = market_implied_reversal_prob(
        trigger=trigger,
        d_signed_pct=d_signed_pct,
        prior_reversal=prior_reversal,
        cfg=cfg,
    )
    side = "DOWN" if trigger == "111" else "UP"

    slip = _slippage_bps(cfg, stake_quote=stake_quote)
    rb = _rebound(cfg, seconds_left)
    drag = _oracle_drag(cfg, abs(d_signed_pct))

    eff = _adjust_price(raw, slip, rb, cfg)
    payoff_gross = _safe_payoff_from_price(eff)
    return TradeQuote(
        side=side,
        raw_price=raw,
        effective_price=eff,
        payoff_gross=payoff_gross,
        fill_ratio=_fill_ratio(cfg, stake_quote=stake_quote),
        slippage_bps=slip,
        rebound=rb,
        oracle_drag=drag,
    )


def quote_for_trend(
    *,
    trigger: str,
    d_signed_pct: float,
    seconds_left: int,
    prior_reversal: float = 0.18,
    cfg: PolymarketCfg = PolymarketCfg(),
    stake_quote: float | None = None,
) -> TradeQuote:
    """
    顺势方向报价（市场胜方价格 = 1 - 隐含反转概率）。
    传入 stake_quote 时, 摩擦按订单规模动态缩放。
    """
    raw_rev = market_implied_reversal_prob(
        trigger=trigger,
        d_signed_pct=d_signed_pct,
        prior_reversal=prior_reversal,
        cfg=cfg,
    )
    raw = 1.0 - raw_rev
    side = "UP" if trigger == "111" else "DOWN"

    slip = _slippage_bps(cfg, stake_quote=stake_quote)
    rb = _rebound(cfg, seconds_left)
    drag = _oracle_drag(cfg, abs(d_signed_pct))
    eff = _adjust_price(raw, slip, rb, cfg)
    payoff_gross = _safe_payoff_from_price(eff)
    return TradeQuote(
        side=side,
        raw_price=raw,
        effective_price=eff,
        payoff_gross=payoff_gross,
        fill_ratio=_fill_ratio(cfg, stake_quote=stake_quote),
        slippage_bps=slip,
        rebound=rb,
        oracle_drag=drag,
    )


def settle_pnl(
    *,
    won: bool,
    quote: TradeQuote,
    stake: float,
    cfg: PolymarketCfg = PolymarketCfg(),
) -> dict:
    """
    根据报价、本金、胜负，扣除全部摩擦项后的净 PnL。

    扣减项:
        - depth_fill_ratio: 实际成交本金缩水
        - oracle_drag: 把胜率转化为期望损耗 (近似形式: 直接从赢面减去)
        - uma_fee_rate: 对正利润再抽
        - maker_rebate: 入场固定收益
        - gas_cost_quote: 双向固定成本

    返回 dict 便于审计。
    """
    eff_stake = stake * quote.fill_ratio
    mode = str(cfg.friction_mode or "off").lower()
    taker_fee_rate = 0.0
    extra_slippage = 0.0
    if mode == "taker":
        p = float(max(0.0, min(1.0, quote.effective_price)))
        # Taker Fee = 0.05 × 0.25 × (p × (1-p))²
        taker_fee_rate = 0.0125 * ((p * (1.0 - p)) ** 2)
        extra_slippage = eff_stake * (max(0.0, cfg.taker_slippage_bps) / 10_000.0)
    if won:
        gross = quote.payoff_gross * eff_stake
        gross *= 1.0 - quote.oracle_drag
        if taker_fee_rate > 0:
            gross *= 1.0 - taker_fee_rate
        if cfg.uma_fee_rate > 0 and gross > 0:
            gross *= 1.0 - cfg.uma_fee_rate
        pnl = gross
    else:
        pnl = -eff_stake
    pnl += cfg.maker_rebate_rate * eff_stake
    pnl -= 2.0 * cfg.gas_cost_quote
    pnl -= extra_slippage
    return {
        "pnl": float(pnl),
        "effective_stake": float(eff_stake),
        "won": bool(won),
        "side": quote.side,
        "effective_price": quote.effective_price,
        "payoff_gross": quote.payoff_gross,
        "slippage_bps": quote.slippage_bps,
        "rebound": quote.rebound,
        "oracle_drag": quote.oracle_drag,
        "friction_mode": mode,
        "taker_fee_rate": taker_fee_rate,
        "extra_slippage_cost": extra_slippage,
    }
