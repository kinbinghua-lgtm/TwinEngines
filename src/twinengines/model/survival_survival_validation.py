"""
「生存」视角下的验证（与现行「反转/死亡」标签对偶）。

语义
----
- 训练表里 ``event=1`` 仍表示 **窗末发生反转**（terminal 口径，与 ``windows_to_event_table`` 一致）。
- **生存事件** 定义为 ``z = 1 - event``：窗末 **未反转** = 趋势相对基准「活到」观测窗末。
- 风险结构仍用同一套 **反转累计 hazard** ``H``（非齐次泊松近似）:
      P(窗内发生反转) ≈ 1 - exp(-H),   P(生存) = exp(-H).
  因此 ``H`` 的物理含义仍是「反转强度积分」；换生存视角后，**报告与校准的对象**改为 ``exp(-H)`` 对 ``z``。

动态基线
--------
使用 ``survival_ablation`` 中有理动态 ``λ0(d0)=a/(1+c·d0)``，与 ``fit_survival_mle_dynamic_lambda0`` 一致。
在 ``event ∈ {0,1}`` 的窗级 Bernoulli 下，对 ``(a,c)`` 的 MLE 与「反转标签」写法数值等价；
本模块额外输出 **生存概率** 的 Brier、粗分桶校准，便于你做「生存」叙事下的验证。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Sequence

import numpy as np
import pandas as pd

from ..survival import SurvivalParams
from .fit import _cumulative_hazard_per_window, fit_survival_mle
from .survival_ablation import (
    DynamicL0FitReport,
    aggregate_event_table_with_d0,
    fit_survival_mle_dynamic_lambda0,
    loglik_dynamic_on_agg,
    loglik_scalar_l0_on_agg,
    _window_H_dynamic_l0,
)


def _h_dynamic_array(agg: pd.DataFrame, rep: DynamicL0FitReport) -> np.ndarray:
    ds_arr = agg["ds"].tolist()
    ts_arr = agg["ts"].tolist()
    d0_arr = agg["d0"].tolist()
    return np.array(
        [
            _window_H_dynamic_l0(ds, ts, d0=d0, a=rep.a, c=rep.c, gamma=rep.gamma, beta=rep.beta)
            for ds, ts, d0 in zip(ds_arr, ts_arr, d0_arr)
        ],
        dtype=float,
    )


def survival_prob_from_H(H: np.ndarray) -> np.ndarray:
    return np.exp(-np.clip(H, 0.0, 50.0))


def brier_survival(z: np.ndarray, p_surv: np.ndarray) -> float:
    """Brier score 对生存概率: mean((z - p_surv)^2)。"""
    z = np.asarray(z, dtype=float)
    p = np.asarray(p_surv, dtype=float)
    return float(np.mean((z - p) ** 2))


def reversal_prob_from_H(H: np.ndarray) -> np.ndarray:
    """P(窗末反转) ≈ 1 - exp(-H)。"""
    return 1.0 - survival_prob_from_H(H)


def brier_reversal(e: np.ndarray, p_rev: np.ndarray) -> float:
    """Brier score 对反转概率: mean((e - p_rev)^2)。"""
    e = np.asarray(e, dtype=float)
    p = np.asarray(p_rev, dtype=float)
    return float(np.mean((e - p) ** 2))


def _coarse_bins(d0: np.ndarray, n_bins: int) -> np.ndarray:
    qs = np.quantile(d0, np.linspace(0, 1, n_bins + 1))
    qs[0] = qs[0] - 1e-9
    return np.digitize(d0, qs[1:-1], right=True)


def calibration_survival_by_d0(
    agg: pd.DataFrame,
    rep: DynamicL0FitReport,
    *,
    n_bins: int = 8,
) -> list[dict[str, Any]]:
    """按 d0 分位分桶: 桶内平均 z vs 平均预测 P(生存)。"""
    if agg.empty or n_bins < 2:
        return []
    H = _h_dynamic_array(agg, rep)
    p_s = survival_prob_from_H(H)
    z = 1.0 - agg["event"].to_numpy(dtype=float)
    d0 = agg["d0"].to_numpy(dtype=float)
    bins = _coarse_bins(d0, n_bins)
    rows: list[dict[str, Any]] = []
    for b in range(n_bins):
        m = bins == b
        if not np.any(m):
            continue
        rows.append(
            {
                "d0_bin": int(b),
                "n": int(np.sum(m)),
                "z_rate": float(np.mean(z[m])),
                "pred_surv_mean": float(np.mean(p_s[m])),
                "d0_lo": float(np.min(d0[m])),
                "d0_hi": float(np.max(d0[m])),
            }
        )
    return rows


@dataclass(frozen=True)
class SurvivalValidationReport:
    """可 JSON 序列化的摘要（由 ``as_dict`` 输出）。"""

    framing: str
    n_train_windows: int
    n_valid_windows: int
    const_lambda0: float
    gamma: float
    beta: float
    dynamic_a: float
    dynamic_c: float
    dynamic_fit_success: bool
    dynamic_fit_message: str
    ll_train_dynamic: float
    ll_valid_dynamic: float
    ll_valid_const_l0: float
    brier_surv_valid_dynamic: float
    brier_surv_valid_const: float
    mean_z_valid: float
    mean_pred_surv_valid_dynamic: float
    calibration_valid: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "framing": self.framing,
            "n_train_windows": self.n_train_windows,
            "n_valid_windows": self.n_valid_windows,
            "const_lambda0": self.const_lambda0,
            "gamma": self.gamma,
            "beta": self.beta,
            "dynamic_lambda0_a": self.dynamic_a,
            "dynamic_lambda0_c": self.dynamic_c,
            "dynamic_fit_success": self.dynamic_fit_success,
            "dynamic_fit_message": self.dynamic_fit_message,
            "loglik_train_dynamic": self.ll_train_dynamic,
            "loglik_valid_dynamic": self.ll_valid_dynamic,
            "loglik_valid_const_lambda0": self.ll_valid_const_l0,
            "brier_survival_valid_dynamic": self.brier_surv_valid_dynamic,
            "brier_survival_valid_const_lambda0": self.brier_surv_valid_const,
            "mean_observed_survival_z_valid": self.mean_z_valid,
            "mean_pred_survival_valid_dynamic": self.mean_pred_surv_valid_dynamic,
            "calibration_survival_by_d0_valid": self.calibration_valid,
        }


MS_PER_DAY = 86_400_000


def split_samples_calendar_train_valid_holdout(
    samples: Sequence[Any],
    *,
    span_days: int = 48,
    holdout_days: int = 30,
    train_frac_in_span: float = 0.8,
) -> tuple[list[Any], list[Any], list[Any]]:
    """
    以首窗 ``window_start_ms`` 为 t0:
    - [t0, t0+span_days): 内按时间排序后 **前 train_frac** 为 train，余下为 valid
    - [t0+span_days, t0+span_days+holdout_days): holdout
    """
    if not samples:
        return [], [], []
    if not 0.0 < train_frac_in_span < 1.0:
        raise ValueError("train_frac_in_span must be in (0,1)")
    ordered = sorted(samples, key=lambda s: int(s.window_start_ms))
    t0 = int(ordered[0].window_start_ms)
    span_ms = int(span_days) * MS_PER_DAY
    hold_ms = int(holdout_days) * MS_PER_DAY
    pool = [s for s in ordered if t0 <= int(s.window_start_ms) < t0 + span_ms]
    hold = [s for s in ordered if t0 + span_ms <= int(s.window_start_ms) < t0 + span_ms + hold_ms]
    n = len(pool)
    if n == 0:
        return [], [], hold
    cut = max(1, min(n - 1, int(n * float(train_frac_in_span))))
    return pool[:cut], pool[cut:], hold


def _metrics_reversal_on_agg(
    agg: pd.DataFrame,
    *,
    dyn: DynamicL0FitReport,
    const_l0: float,
    gamma: float,
    beta: float,
) -> dict[str, Any]:
    """反转标签 event=e: 评估 P(反转)=1-exp(-H) 与 Bernoulli 对数似然。"""
    if agg.empty:
        return {
            "n_windows": 0,
            "loglik_dynamic": float("nan"),
            "loglik_const_lambda0": float("nan"),
            "brier_reversal_dynamic": float("nan"),
            "brier_reversal_const": float("nan"),
            "mean_event_reversal": float("nan"),
            "mean_pred_reversal_dynamic": float("nan"),
            "coverage_pred_reversal_gt_0p5_dynamic": float("nan"),
            "coverage_pred_reversal_gt_0p5_const": float("nan"),
            "mae_reversal_prob_dynamic": float("nan"),
            "mae_reversal_prob_const": float("nan"),
        }
    ll_d = loglik_dynamic_on_agg(agg, dyn)
    sp = SurvivalParams(lambda0=float(const_l0), gamma=float(gamma), beta=float(beta))
    ll_c = loglik_scalar_l0_on_agg(agg, sp)
    H_d = _h_dynamic_array(agg, dyn)
    p_d = reversal_prob_from_H(H_d)
    e = agg["event"].to_numpy(dtype=float)
    ds_arr = agg["ds"].tolist()
    ts_arr = agg["ts"].tolist()
    H_c = _cumulative_hazard_per_window(ds_arr, ts_arr, float(const_l0), float(gamma), float(beta))
    p_c = reversal_prob_from_H(H_c)
    return {
        "n_windows": int(len(agg)),
        "loglik_dynamic": float(ll_d),
        "loglik_const_lambda0": float(ll_c),
        "brier_reversal_dynamic": brier_reversal(e, p_d),
        "brier_reversal_const": brier_reversal(e, p_c),
        "mean_event_reversal": float(np.mean(e)),
        "mean_pred_reversal_dynamic": float(np.mean(p_d)),
        "coverage_pred_reversal_gt_0p5_dynamic": float(np.mean(p_d > 0.5)),
        "coverage_pred_reversal_gt_0p5_const": float(np.mean(p_c > 0.5)),
        "mae_reversal_prob_dynamic": float(np.mean(np.abs(e - p_d))),
        "mae_reversal_prob_const": float(np.mean(np.abs(e - p_c))),
    }


def _metrics_on_agg(
    agg: pd.DataFrame,
    *,
    dyn: DynamicL0FitReport,
    const_l0: float,
    gamma: float,
    beta: float,
) -> dict[str, Any]:
    if agg.empty:
        return {
            "n_windows": 0,
            "loglik_dynamic": float("nan"),
            "loglik_const_lambda0": float("nan"),
            "brier_survival_dynamic": float("nan"),
            "brier_survival_const": float("nan"),
            "mean_z": float("nan"),
            "mean_pred_surv_dynamic": float("nan"),
            "mean_event_reversal": float("nan"),
            "coverage_pred_surv_gt_0p5_dynamic": float("nan"),
            "coverage_pred_surv_gt_0p5_const": float("nan"),
            "mae_survival_prob_dynamic": float("nan"),
            "mae_survival_prob_const": float("nan"),
        }
    ll_d = loglik_dynamic_on_agg(agg, dyn)
    sp = SurvivalParams(lambda0=float(const_l0), gamma=float(gamma), beta=float(beta))
    ll_c = loglik_scalar_l0_on_agg(agg, sp)
    H_d = _h_dynamic_array(agg, dyn)
    p_d = survival_prob_from_H(H_d)
    z = 1.0 - agg["event"].to_numpy(dtype=float)
    ds_arr = agg["ds"].tolist()
    ts_arr = agg["ts"].tolist()
    H_c = _cumulative_hazard_per_window(ds_arr, ts_arr, float(const_l0), float(gamma), float(beta))
    p_c = survival_prob_from_H(H_c)
    cov_d = float(np.mean(p_d > 0.5))
    cov_c = float(np.mean(p_c > 0.5))
    mae_d = float(np.mean(np.abs(z - p_d)))
    mae_c = float(np.mean(np.abs(z - p_c)))
    return {
        "n_windows": int(len(agg)),
        "loglik_dynamic": float(ll_d),
        "loglik_const_lambda0": float(ll_c),
        "brier_survival_dynamic": brier_survival(z, p_d),
        "brier_survival_const": brier_survival(z, p_c),
        "mean_z": float(np.mean(z)),
        "mean_pred_surv_dynamic": float(np.mean(p_d)),
        "mean_event_reversal": float(np.mean(agg["event"].to_numpy(dtype=float))),
        "coverage_pred_surv_gt_0p5_dynamic": cov_d,
        "coverage_pred_surv_gt_0p5_const": cov_c,
        "mae_survival_prob_dynamic": mae_d,
        "mae_survival_prob_const": mae_c,
    }


def _trigger_subset(agg: pd.DataFrame, trigger: Literal["111", "000"]) -> pd.DataFrame:
    if agg.empty or "trigger" not in agg.columns:
        return pd.DataFrame(columns=agg.columns)
    return agg[agg["trigger"] == trigger].reset_index(drop=True)


def run_survival_calendar_48_30_trigger_report(
    samples: Sequence[Any],
    *,
    span_days: int = 48,
    holdout_days: int = 30,
    train_frac_in_span: float = 0.8,
    label_mode: str = "terminal",
) -> dict[str, Any]:
    """
    日历: 首窗起 48 天内 80% train / 20% valid；随后 30 天 holdout。
    在 **合并 train(111+000)** 上拟合常数 λ0、γβ 与动态 (a,c)；在 valid/holdout 上
    分别报 **全体 / 仅111 / 仅000** 的生存指标（同一套参数）。
    """
    from ..data.window import windows_to_event_table

    train_s, valid_s, hold_s = split_samples_calendar_train_valid_holdout(
        list(samples),
        span_days=span_days,
        holdout_days=holdout_days,
        train_frac_in_span=train_frac_in_span,
    )
    if not train_s or not valid_s:
        raise ValueError(
            "calendar split produced empty train or valid; "
            "check data span vs span_days/train_frac_in_span"
        )

    et_tr = windows_to_event_table(train_s, label_mode=label_mode)
    et_va = windows_to_event_table(valid_s, label_mode=label_mode)
    et_ho = windows_to_event_table(hold_s, label_mode=label_mode) if hold_s else pd.DataFrame()

    base = fit_survival_mle(et_tr)
    g, b = float(base.params.gamma), float(base.params.beta)
    dyn = fit_survival_mle_dynamic_lambda0(et_tr, gamma=g, beta=b)

    agg_tr = aggregate_event_table_with_d0(et_tr)
    agg_va = aggregate_event_table_with_d0(et_va)
    agg_ho = aggregate_event_table_with_d0(et_ho) if not et_ho.empty else pd.DataFrame()

    l0 = float(base.params.lambda0)

    def block(agg: pd.DataFrame, name: str) -> dict[str, Any]:
        out: dict[str, Any] = {"segment": name, "all": _metrics_on_agg(agg, dyn=dyn, const_l0=l0, gamma=g, beta=b)}
        for tg in ("111", "000"):
            out[f"trigger_{tg}"] = _metrics_on_agg(
                _trigger_subset(agg, tg), dyn=dyn, const_l0=l0, gamma=g, beta=b
            )
        return out

    out: dict[str, Any] = {
        "framing": (
            "terminal: z=1 未反转; P(生存)=exp(-H); "
            "train=首窗起48天内前80%%时间窗; valid=同48天后20%%; holdout=随后30天; "
            "111/000 为同模型分层评估"
        ),
        "split": {
            "span_days": int(span_days),
            "holdout_days": int(holdout_days),
            "train_frac_in_span": float(train_frac_in_span),
            "n_train_windows": len(train_s),
            "n_valid_windows": len(valid_s),
            "n_holdout_windows": len(hold_s),
        },
        "fit": {
            "const_lambda0": l0,
            "gamma": g,
            "beta": b,
            "dynamic_a": float(dyn.a),
            "dynamic_c": float(dyn.c),
            "dynamic_fit_success": bool(dyn.success),
            "dynamic_fit_message": str(dyn.message),
            "loglik_train_dynamic": float(loglik_dynamic_on_agg(agg_tr, dyn)),
        },
        "valid": block(agg_va, "valid"),
        "holdout": block(agg_ho, "holdout") if len(hold_s) and not et_ho.empty else {"segment": "holdout", "note": "empty"},
    }
    if len(hold_s) and not et_ho.empty:
        out["holdout"]["calibration_survival_by_d0_dynamic"] = calibration_survival_by_d0(
            agg_ho, dyn, n_bins=8
        )
    if not agg_va.empty:
        out["valid"]["calibration_survival_by_d0_dynamic"] = calibration_survival_by_d0(agg_va, dyn, n_bins=8)
    return out


def run_reversal_calendar_48_30_trigger_report(
    samples: Sequence[Any],
    *,
    span_days: int = 48,
    holdout_days: int = 30,
    train_frac_in_span: float = 0.8,
    label_mode: str = "terminal",
) -> dict[str, Any]:
    """
    与 ``run_survival_calendar_48_30_trigger_report`` 相同日历切分，
    指标改为 **反转** Bernoulli：``event=1`` 为窗末反转，``P(反转)≈1-exp(-H)``。
    """
    from ..data.window import windows_to_event_table

    train_s, valid_s, hold_s = split_samples_calendar_train_valid_holdout(
        list(samples),
        span_days=span_days,
        holdout_days=holdout_days,
        train_frac_in_span=train_frac_in_span,
    )
    if not train_s or not valid_s:
        raise ValueError(
            "calendar split produced empty train or valid; "
            "check data span vs span_days/train_frac_in_span"
        )

    et_tr = windows_to_event_table(train_s, label_mode=label_mode)
    et_va = windows_to_event_table(valid_s, label_mode=label_mode)
    et_ho = windows_to_event_table(hold_s, label_mode=label_mode) if hold_s else pd.DataFrame()

    base = fit_survival_mle(et_tr)
    g, b = float(base.params.gamma), float(base.params.beta)
    dyn = fit_survival_mle_dynamic_lambda0(et_tr, gamma=g, beta=b)

    agg_tr = aggregate_event_table_with_d0(et_tr)
    agg_va = aggregate_event_table_with_d0(et_va)
    agg_ho = aggregate_event_table_with_d0(et_ho) if not et_ho.empty else pd.DataFrame()

    l0 = float(base.params.lambda0)

    def block(agg: pd.DataFrame, name: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "segment": name,
            "all": _metrics_reversal_on_agg(agg, dyn=dyn, const_l0=l0, gamma=g, beta=b),
        }
        for tg in ("111", "000"):
            out[f"trigger_{tg}"] = _metrics_reversal_on_agg(
                _trigger_subset(agg, tg), dyn=dyn, const_l0=l0, gamma=g, beta=b
            )
        return out

    out: dict[str, Any] = {
        "framing": (
            "terminal reversal: event=1 窗末反转; P(反转)=1-exp(-H); "
            "λ0(d0)=a/(1+c*d0); 切分同 survival calendar 48+30"
        ),
        "split": {
            "span_days": int(span_days),
            "holdout_days": int(holdout_days),
            "train_frac_in_span": float(train_frac_in_span),
            "n_train_windows": len(train_s),
            "n_valid_windows": len(valid_s),
            "n_holdout_windows": len(hold_s),
        },
        "fit": {
            "const_lambda0": l0,
            "gamma": g,
            "beta": b,
            "dynamic_a": float(dyn.a),
            "dynamic_c": float(dyn.c),
            "dynamic_fit_success": bool(dyn.success),
            "dynamic_fit_message": str(dyn.message),
            "loglik_train_dynamic": float(loglik_dynamic_on_agg(agg_tr, dyn)),
        },
        "valid": block(agg_va, "valid"),
        "holdout": block(agg_ho, "holdout") if len(hold_s) and not et_ho.empty else {"segment": "holdout", "note": "empty"},
    }
    return out


def run_survival_validation_report(
    train_event_table: pd.DataFrame,
    valid_event_table: pd.DataFrame,
) -> SurvivalValidationReport:
    """
    在 train 上: 常数 λ0 MLE → 取 γ,β；再拟合动态 (a,c)。
    在 valid 上: 对比动态 vs 常数 λ0 的 **生存概率** Brier 与对数似然（反转似然，与现行代码一致）。
    """
    if train_event_table.empty or valid_event_table.empty:
        raise ValueError("train and valid event tables must be non-empty")

    base = fit_survival_mle(train_event_table)
    g, b = float(base.params.gamma), float(base.params.beta)
    dyn = fit_survival_mle_dynamic_lambda0(train_event_table, gamma=g, beta=b)

    agg_tr = aggregate_event_table_with_d0(train_event_table)
    agg_va = aggregate_event_table_with_d0(valid_event_table)

    ll_tr = loglik_dynamic_on_agg(agg_tr, dyn)
    ll_va = loglik_dynamic_on_agg(agg_va, dyn)
    ll_va_const = loglik_scalar_l0_on_agg(agg_va, base.params)

    H_va = _h_dynamic_array(agg_va, dyn)
    p_s_va = survival_prob_from_H(H_va)
    z_va = 1.0 - agg_va["event"].to_numpy(dtype=float)

    ds_arr = agg_va["ds"].tolist()
    ts_arr = agg_va["ts"].tolist()
    Hc = _cumulative_hazard_per_window(ds_arr, ts_arr, base.params.lambda0, g, b)
    p_s_const = survival_prob_from_H(Hc)

    cal = calibration_survival_by_d0(agg_va, dyn, n_bins=8)

    return SurvivalValidationReport(
        framing="terminal: z=1 窗末未反转(生存); H 为反转累计 hazard; P(生存)=exp(-H); λ0(d0)=a/(1+c*d0)",
        n_train_windows=int(len(agg_tr)),
        n_valid_windows=int(len(agg_va)),
        const_lambda0=float(base.params.lambda0),
        gamma=g,
        beta=b,
        dynamic_a=float(dyn.a),
        dynamic_c=float(dyn.c),
        dynamic_fit_success=bool(dyn.success),
        dynamic_fit_message=str(dyn.message),
        ll_train_dynamic=float(ll_tr),
        ll_valid_dynamic=float(ll_va),
        ll_valid_const_l0=float(ll_va_const),
        brier_surv_valid_dynamic=brier_survival(z_va, p_s_va),
        brier_surv_valid_const=brier_survival(z_va, p_s_const),
        mean_z_valid=float(np.mean(z_va)),
        mean_pred_surv_valid_dynamic=float(np.mean(p_s_va)),
        calibration_valid=cal,
    )
