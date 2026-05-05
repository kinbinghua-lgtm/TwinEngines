"""
拟合结果与阈值持久化。

落盘格式（JSON）便于版本管理与跨语言加载:
{
  "schema_version": "1.0",
  "trained_at": "2026-04-27T10:00:00Z",
  "data_meta": {
      "symbol": "BTCUSDT",
      "interval": "1s",
      "n_train_windows": 1234,
      "n_valid_windows": 456,
      "obs_step_sec": 10
  },
  "params": {"lambda0": ..., "gamma": ..., "beta": ...},
  "thresholds": {
      "reversal_prob_min": ...,
      "reversal_edge_vs_baseline_min": ...,
      "trend_reversal_prob_max": ...,
      "trend_signal_stability_sec_min": ...,
      "trend_min_expected_value": ...
  },
  "calibration": {
      "baseline_prob": ...,
      "bin_table": [...]
  },
  "ci": {
      "lambda0": [lo, hi],
      "gamma": [lo, hi],
      "beta": [lo, hi]
  }
}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..signals import SignalThresholds
from ..survival import SurvivalParams

SCHEMA_VERSION = "1.1"


@dataclass(frozen=True)
class StrategyArtifact:
    params: SurvivalParams
    thresholds: SignalThresholds
    baseline_prob: float
    data_meta: dict[str, Any]
    bin_table: list[dict[str, Any]]
    ci: dict[str, list[float]] | None = None
    trained_at: str | None = None
    shadow_engine: dict[str, Any] | None = None

    def to_json(self) -> dict:
        out: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "trained_at": self.trained_at or datetime.now(timezone.utc).isoformat(),
            "data_meta": self.data_meta,
            "params": self.params.to_dict(),
            "thresholds": {
                "reversal_prob_min": self.thresholds.reversal_prob_min,
                "reversal_edge_vs_baseline_min": self.thresholds.reversal_edge_vs_baseline_min,
                "trend_reversal_prob_max": self.thresholds.trend_reversal_prob_max,
                "trend_signal_stability_sec_min": self.thresholds.trend_signal_stability_sec_min,
                "reversal_baseline_offset_min": self.thresholds.reversal_baseline_offset_min,
                "reversal_signal_stability_sec_min": self.thresholds.reversal_signal_stability_sec_min,
                "trend_min_expected_value": self.thresholds.trend_min_expected_value,
            },
            "calibration": {
                "baseline_prob": self.baseline_prob,
                "bin_table": self.bin_table,
            },
            "ci": self.ci,
        }
        if self.shadow_engine is not None:
            out["shadow_engine"] = self.shadow_engine
        return out


def save_artifact(path: str | Path, artifact: StrategyArtifact) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(artifact.to_json(), indent=2, ensure_ascii=False), encoding="utf-8")


def load_artifact(path: str | Path) -> StrategyArtifact:
    p = Path(path)
    payload = json.loads(p.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported artifact schema {payload.get('schema_version')}")
    params = SurvivalParams.from_dict(payload["params"])
    th = payload["thresholds"]
    required_threshold_keys = (
        "reversal_prob_min",
        "reversal_edge_vs_baseline_min",
        "trend_reversal_prob_max",
        "trend_signal_stability_sec_min",
        "reversal_baseline_offset_min",
        "reversal_signal_stability_sec_min",
        "trend_min_expected_value",
    )
    missing = [k for k in required_threshold_keys if k not in th]
    if missing:
        raise ValueError(
            f"artifact thresholds missing required fields: {missing}; "
            "refuse to load to avoid silent fallback"
        )
    thresholds = SignalThresholds(
        reversal_prob_min=th["reversal_prob_min"],
        reversal_edge_vs_baseline_min=th["reversal_edge_vs_baseline_min"],
        trend_reversal_prob_max=th["trend_reversal_prob_max"],
        trend_signal_stability_sec_min=th["trend_signal_stability_sec_min"],
        reversal_baseline_offset_min=th["reversal_baseline_offset_min"],
        reversal_signal_stability_sec_min=th["reversal_signal_stability_sec_min"],
        trend_min_expected_value=th["trend_min_expected_value"],
    )
    return StrategyArtifact(
        params=params,
        thresholds=thresholds,
        baseline_prob=payload["calibration"]["baseline_prob"],
        data_meta=payload.get("data_meta", {}),
        bin_table=payload["calibration"].get("bin_table", []),
        ci=payload.get("ci"),
        trained_at=payload.get("trained_at"),
        shadow_engine=payload.get("shadow_engine"),
    )
