"""
裸跑 third-digit：置信度 → best_ask 上限分档（与 ``naked_pm_runner`` 入场闸一致）。

离线寻优：准备若干行 ``(confidence_on_pick, best_ask, outcome)``，对候选 ``tiers`` 调用
``score_tiers_on_labeled_rows``，用 **时间序留出** 的验证集选分（避免同一批数据过拟合）。
``outcome`` 可取 0/1（是否盈利）或已实现 PnL；目标函数需与业务一致（例如最大化通过样本的
sum(PnL) 而非仅 mean win rate）。
"""

from __future__ import annotations

from typing import Sequence

ConfOddsTiers = tuple[tuple[float, float], ...]

DEFAULT_CONF_ODDS_TIERS: ConfOddsTiers = (
    (0.60, 0.50),
    (0.56, 0.40),
    (0.53, 0.30),
    (0.52, 0.20),
    (0.51, 0.10),
)


def odds_cap_strict_below_for_confidence(conf: float, tiers: ConfOddsTiers | None = None) -> float:
    """与实盘一致：按置信度从高到低命中首档，返回该档 ``best_ask`` 严格上界；未命中任何档时返回 0.10。"""
    t = DEFAULT_CONF_ODDS_TIERS if tiers is None else tiers
    c = float(conf)
    for min_conf, cap in t:
        if c + 1e-15 >= float(min_conf):
            return float(cap)
    return 0.10


def odds_gate_passes(conf: float, best_ask: float, tiers: ConfOddsTiers | None = None) -> bool:
    """是否满足「所选边 best_ask 严格低于分档 cap」（与 runner 中 ``>= cap`` 拒单对偶）。"""
    if best_ask is None or not isinstance(best_ask, (int, float)):
        return False
    if not float(best_ask) == float(best_ask):  # NaN
        return False
    cap = odds_cap_strict_below_for_confidence(conf, tiers=tiers)
    return float(best_ask) < float(cap)


def score_tiers_on_labeled_rows(
    rows: Sequence[tuple[float, float, float]],
    tiers: ConfOddsTiers,
    *,
    min_trades: int = 30,
) -> dict[str, float]:
    """
    rows: (confidence_on_pick, best_ask, outcome_scalar)，仅 ``best_ask`` 有限时参与。

    返回 ``mean_outcome_if_pass``（通过闸样本上 outcome 均值）、``pass_rate`` 等；
    ``objective_mean_y_if_min_trades`` 在通过数 < min_trades 时为 nan，便于网格里过滤不稳定解。
    """
    ys: list[float] = []
    n_in = 0
    for row in rows:
        if len(row) != 3:
            continue
        conf, ask, y = row
        n_in += 1
        try:
            fa = float(ask)
        except (TypeError, ValueError):
            continue
        if not fa == fa or fa <= 0.0:
            continue
        if not odds_gate_passes(float(conf), fa, tiers=tiers):
            continue
        try:
            ys.append(float(y))
        except (TypeError, ValueError):
            continue
    n = len(ys)
    tot = float(n_in) if n_in > 0 else 0.0
    return {
        "n_input_rows": float(n_in),
        "n_pass_gate": float(n),
        "pass_rate": float(n / tot) if tot > 0 else 0.0,
        "mean_outcome_if_pass": float(sum(ys) / n) if n else float("nan"),
        "objective_mean_y_if_min_trades": float(sum(ys) / n) if n >= int(min_trades) else float("nan"),
        "sum_outcome_if_pass": float(sum(ys)),
    }
