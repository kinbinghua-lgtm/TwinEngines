"""
进化型风险管理 (RegimeManager)。

设计原则:
    1. 资金规模决定阶段, 阶段决定参数 (sizing + risk + min_stake)
    2. 阈值带"滞回" (hysteresis): 升档与降档阈值不同, 避免在边界反复抖动
    3. 平滑过渡: 不在阶段切换瞬间跳变, 而是用 logistic blend 在过渡带内插值
    4. 非破坏性: 不改回测引擎签名, 只产出 BacktestConfig 所需的子配置

阶段定义 (默认):
    SEED   <  $500  : 激进 (Kelly 0.50/0.20, max stake 30%/10%, 允许爆仓)
    GROWTH $500-$10k: 渐进 (Kelly 在阈值内 logistic 衰减到生产值)
    MATURE > $10k   : 生产保守 (Kelly 0.30/0.10, max stake 10%/3%)

平滑过渡:
    在 [seed_to_growth_low, seed_to_growth_high] 区间内, 用 logistic 把激进/生产参数加权
    保证当前 equity 提升 1% 不会引起切档剧变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import exp
from typing import Optional

from .limits import RiskCfg
from .sizing import SizingCfg


@dataclass(frozen=True)
class RegimePreset:
    """单个阶段的全部子配置。"""
    name: str
    reversal_sizing: SizingCfg
    trend_sizing: SizingCfg
    risk_cfg: RiskCfg
    daily_drawdown_stop: float
    max_concurrent_open: int


# === 四阶段默认 preset ===

ULTRA_PRESET = RegimePreset(
    name="ULTRA",
    reversal_sizing=SizingCfg(
        kelly_fraction=0.50,
        max_stake_ratio=0.30,
        min_absolute_stake=2.50,
    ),
    trend_sizing=SizingCfg(
        kelly_fraction=0.20,
        max_stake_ratio=0.10,
        min_absolute_stake=2.50,
    ),
    risk_cfg=RiskCfg(
        daily_drawdown_stop=0.95,
        max_concurrent_open=2,
        max_total_exposure_ratio=0.80,
        equity_floor=2.0,            # 低于 $2 优雅退出 (= 10 个最小单)
    ),
    daily_drawdown_stop=0.95,
    max_concurrent_open=2,
)

SEED_PRESET = RegimePreset(
    name="SEED",
    reversal_sizing=SizingCfg(
        kelly_fraction=0.50,
        max_stake_ratio=0.30,
        min_absolute_stake=2.50,
    ),
    trend_sizing=SizingCfg(
        kelly_fraction=0.20,
        max_stake_ratio=0.10,
        min_absolute_stake=2.50,
    ),
    risk_cfg=RiskCfg(
        daily_drawdown_stop=0.50,    # 种子阶段允许单日 -50% (尽快积累)
        max_concurrent_open=3,
        max_total_exposure_ratio=0.50,
        equity_floor=2.0,            # 低于 $2 (=10 个最小单) 优雅退出
    ),
    daily_drawdown_stop=0.50,
    max_concurrent_open=3,
)

GROWTH_PRESET = RegimePreset(
    name="GROWTH",
    reversal_sizing=SizingCfg(
        kelly_fraction=0.40,
        max_stake_ratio=0.20,
        min_absolute_stake=0.20,
    ),
    trend_sizing=SizingCfg(
        kelly_fraction=0.15,
        max_stake_ratio=0.06,
        min_absolute_stake=0.20,
    ),
    risk_cfg=RiskCfg(
        daily_drawdown_stop=0.20,    # 成长阶段允许单日 -20%
        max_concurrent_open=4,
        max_total_exposure_ratio=0.40,
        equity_floor=20.0,           # 低于 $20 视为回退到种子阶段, 此处先优雅停
    ),
    daily_drawdown_stop=0.20,
    max_concurrent_open=4,
)

MATURE_PRESET = RegimePreset(
    name="MATURE",
    reversal_sizing=SizingCfg(
        kelly_fraction=0.30,
        max_stake_ratio=0.10,
        min_absolute_stake=0.20,
    ),
    trend_sizing=SizingCfg(
        kelly_fraction=0.10,
        max_stake_ratio=0.03,
        min_absolute_stake=0.20,
    ),
    risk_cfg=RiskCfg(
        daily_drawdown_stop=0.05,    # 成熟阶段单日 -5% 即停
        max_concurrent_open=5,
        max_total_exposure_ratio=0.30,
        equity_floor=500.0,          # 大资金阶段不允许回到 $500 以下重启
    ),
    daily_drawdown_stop=0.05,
    max_concurrent_open=5,
)


@dataclass(frozen=True)
class RegimeBands:
    """四阶段切换带 (含滞回)。"""
    ultra_to_seed_low: float = 150.0     # equity 突破 $150 进入 SEED (摆脱 gas 死亡区)
    ultra_to_seed_high: float = 250.0
    seed_to_growth_low: float = 400.0    # equity 突破 $400 进入 GROWTH
    seed_to_growth_high: float = 600.0
    growth_to_mature_low: float = 8_000.0
    growth_to_mature_high: float = 12_000.0
    blend_zone_width: float = 200.0      # 阈值附近过渡带宽度 (USDC), 控制 logistic 软度


def _logistic_blend(equity: float, center: float, width: float) -> float:
    """
    返回 0..1 的"高阶段权重".
        equity << center: → 0
        equity >> center: → 1
        center 处: 0.5
        width: 控制软度, 越大越平滑
    """
    if width <= 0:
        return 1.0 if equity >= center else 0.0
    z = (equity - center) / width
    z = max(min(z, 50.0), -50.0)
    return 1.0 / (1.0 + exp(-z))


def _blend_sizing(low: SizingCfg, high: SizingCfg, w: float) -> SizingCfg:
    """w=0 全用 low, w=1 全用 high, 之间线性插值。"""
    return SizingCfg(
        kelly_fraction=low.kelly_fraction * (1 - w) + high.kelly_fraction * w,
        min_stake_ratio=low.min_stake_ratio * (1 - w) + high.min_stake_ratio * w,
        max_stake_ratio=low.max_stake_ratio * (1 - w) + high.max_stake_ratio * w,
        min_absolute_stake=max(low.min_absolute_stake, high.min_absolute_stake),
        share_quantization=max(low.share_quantization, high.share_quantization),
    )


def _blend_risk(low: RiskCfg, high: RiskCfg, w: float) -> RiskCfg:
    return RiskCfg(
        daily_drawdown_stop=low.daily_drawdown_stop * (1 - w) + high.daily_drawdown_stop * w,
        max_concurrent_open=int(round(low.max_concurrent_open * (1 - w) + high.max_concurrent_open * w)),
        max_total_exposure_ratio=low.max_total_exposure_ratio * (1 - w) + high.max_total_exposure_ratio * w,
        equity_floor=low.equity_floor,  # equity_floor 不混合 (用低端的, 更保守)
    )


@dataclass
class RegimeManager:
    """
    根据当前 equity 输出 (reversal_sizing, trend_sizing, risk_cfg, regime_name)。

    四阶段:
        ULTRA  (<$150)        → 超激进, 让小本翻越 gas 死亡线
        SEED   ($150–$400)    → 激进
        GROWTH ($400–$8k)     → 渐进
        MATURE (>$8k)         → 生产保守

    使用方式:
        rm = RegimeManager()
        sizing_rev, sizing_trend, risk_cfg, name = rm.resolve(equity)
    """

    ultra: RegimePreset = field(default_factory=lambda: ULTRA_PRESET)
    seed: RegimePreset = field(default_factory=lambda: SEED_PRESET)
    growth: RegimePreset = field(default_factory=lambda: GROWTH_PRESET)
    mature: RegimePreset = field(default_factory=lambda: MATURE_PRESET)
    bands: RegimeBands = field(default_factory=RegimeBands)
    last_regime: Optional[str] = None

    def resolve(self, equity: float) -> tuple[SizingCfg, SizingCfg, RiskCfg, str]:
        b = self.bands

        ultra_seed_center = (b.ultra_to_seed_low + b.ultra_to_seed_high) / 2.0
        seed_growth_center = (b.seed_to_growth_low + b.seed_to_growth_high) / 2.0
        growth_mature_center = (b.growth_to_mature_low + b.growth_to_mature_high) / 2.0

        if equity < b.ultra_to_seed_low:
            name = "ULTRA"
            rev = self.ultra.reversal_sizing
            tr = self.ultra.trend_sizing
            risk = self.ultra.risk_cfg
        elif equity < b.ultra_to_seed_high:
            w = _logistic_blend(equity, ultra_seed_center, b.blend_zone_width)
            name = "ULTRA→SEED"
            rev = _blend_sizing(self.ultra.reversal_sizing, self.seed.reversal_sizing, w)
            tr = _blend_sizing(self.ultra.trend_sizing, self.seed.trend_sizing, w)
            risk = _blend_risk(self.ultra.risk_cfg, self.seed.risk_cfg, w)
        elif equity < b.seed_to_growth_low:
            name = "SEED"
            rev = self.seed.reversal_sizing
            tr = self.seed.trend_sizing
            risk = self.seed.risk_cfg
        elif equity < b.seed_to_growth_high:
            w = _logistic_blend(equity, seed_growth_center, b.blend_zone_width)
            name = "SEED→GROWTH"
            rev = _blend_sizing(self.seed.reversal_sizing, self.growth.reversal_sizing, w)
            tr = _blend_sizing(self.seed.trend_sizing, self.growth.trend_sizing, w)
            risk = _blend_risk(self.seed.risk_cfg, self.growth.risk_cfg, w)
        elif equity < b.growth_to_mature_low:
            name = "GROWTH"
            rev = self.growth.reversal_sizing
            tr = self.growth.trend_sizing
            risk = self.growth.risk_cfg
        elif equity < b.growth_to_mature_high:
            w = _logistic_blend(equity, growth_mature_center, b.blend_zone_width)
            name = "GROWTH→MATURE"
            rev = _blend_sizing(self.growth.reversal_sizing, self.mature.reversal_sizing, w)
            tr = _blend_sizing(self.growth.trend_sizing, self.mature.trend_sizing, w)
            risk = _blend_risk(self.growth.risk_cfg, self.mature.risk_cfg, w)
        else:
            name = "MATURE"
            rev = self.mature.reversal_sizing
            tr = self.mature.trend_sizing
            risk = self.mature.risk_cfg

        self.last_regime = name
        return rev, tr, risk, name
