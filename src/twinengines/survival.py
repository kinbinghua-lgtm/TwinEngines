"""
生存分析模型 (语义统一版):

瞬时风险率 lambda(d, t) = lambda0 * (t/120)^gamma * exp(beta * d)

剩余时间累计反转概率 (假设当前 d 在剩余时间内不变, 用于 *inference*):
    H_remaining(d, T) = ∫_0^T lambda(d, s) ds
                      = lambda0 * exp(beta * d) * T^(gamma+1) / (120^gamma * (gamma+1))

    P_remaining(d, T) = 1 - exp(-H_remaining)

训练时对真实观测路径求累计 (fit.py 中实现), inference 时用该剩余累计公式。
calibration 与 backtest 都使用同一 inference 公式, 保证语义一致。

变量定义:
    d   : 当前价格相对第1分钟开盘价的绝对偏离百分比 (取正)
    T   : 剩余秒数 (>=0)
    参数: lambda0, gamma, beta 由历史数据 MLE 拟合
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from math import exp
from typing import Any

import numpy as np

T_REF_SECONDS: float = 120.0


@dataclass(frozen=True)
class SurvivalParams:
    lambda0: float
    gamma: float
    beta: float
    beta_vol: float = 0.0

    def to_dict(self) -> dict[str, float]:
        d = asdict(self)
        if self.beta_vol == 0.0 and "beta_vol" in d:
            d.pop("beta_vol", None)
        return d

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SurvivalParams":
        return cls(
            lambda0=float(payload["lambda0"]),
            gamma=float(payload["gamma"]),
            beta=float(payload["beta"]),
            beta_vol=float(payload.get("beta_vol", 0.0)),
        )


def _validate(d_abs_pct: float, t_remaining_sec: float, params: SurvivalParams) -> None:
    if d_abs_pct < 0:
        raise ValueError("d_abs_pct must be >= 0")
    if t_remaining_sec < 0:
        raise ValueError("t_remaining_sec must be >= 0")
    if params.lambda0 <= 0:
        raise ValueError("lambda0 must be > 0")


def instantaneous_hazard(
    d_abs_pct: float,
    t_remaining_sec: float,
    params: SurvivalParams,
) -> float:
    """瞬时风险率 lambda(d, t) (训练用, 在路径上积分)。"""
    _validate(d_abs_pct, t_remaining_sec, params)
    return (
        params.lambda0
        * ((t_remaining_sec / T_REF_SECONDS) ** params.gamma)
        * exp(params.beta * d_abs_pct)
    )


def cumulative_hazard(
    d_abs_pct: float,
    t_remaining_sec: float,
    params: SurvivalParams,
    vol_ratio: float | None = None,
) -> float:
    """
    剩余时间累计 hazard (假设 d 不变):
        H = lambda0 * exp(beta*d + beta_vol*vol_ratio) * T^(gamma+1) / (120^gamma * (gamma+1))
    """
    _validate(d_abs_pct, t_remaining_sec, params)
    if t_remaining_sec <= 0:
        return 0.0
    g = params.gamma + 1.0
    exp_arg = params.beta * d_abs_pct
    if vol_ratio is not None and params.beta_vol != 0.0:
        exp_arg += params.beta_vol * vol_ratio
    return (
        params.lambda0
        * exp(exp_arg)
        * (t_remaining_sec**g)
        / ((T_REF_SECONDS**params.gamma) * g)
    )


def reversal_probability(
    d_abs_pct: float,
    t_remaining_sec: float,
    params: SurvivalParams,
    vol_ratio: float | None = None,
) -> float:
    """
    剩余时间内累计反转概率 P(at least one reversal in [0, T] | d_now, params)
    = 1 - exp(-H_remaining(d, T))
    """
    return 1.0 - exp(-cumulative_hazard(d_abs_pct, t_remaining_sec, params, vol_ratio))


def reversal_probability_vec(
    d_abs_pct: np.ndarray,
    t_remaining_sec: np.ndarray,
    params: SurvivalParams,
) -> np.ndarray:
    """向量化版本 (剩余时间累计概率)。"""
    d = np.asarray(d_abs_pct, dtype=float)
    t = np.asarray(t_remaining_sec, dtype=float)
    if (d < 0).any():
        raise ValueError("d_abs_pct must be >= 0")
    if (t < 0).any():
        raise ValueError("t_remaining_sec must be >= 0")
    if params.lambda0 <= 0:
        raise ValueError("lambda0 must be > 0")
    g = params.gamma + 1.0
    h = (
        params.lambda0
        * np.exp(params.beta * d)
        * np.power(np.clip(t, a_min=0.0, a_max=None), g)
        / ((T_REF_SECONDS**params.gamma) * g)
    )
    return 1.0 - np.exp(-h)
