"""
配置脱敏快照 (config snapshot, redacted).

设计:
    - 严禁回显任何私钥 / 完整代理地址 / Telegram token
    - 给 AI 维护看的是: thresholds, baseline_prob, training meta, 安全开关状态, 端口
    - artifact/model 摘要从 JSON 读 (不 import 策略代码), 失败给空字典而不是 raise
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

SECRET_KEYS = {
    "POLYMARKET_PRIVATE_KEY",
    "TELEGRAM_BOT_TOKEN",
    "POLY_BUILDER_API_KEY",
    "POLY_BUILDER_SECRET",
    "POLY_BUILDER_PASSPHRASE",
}
PARTIAL_KEYS = {
    "POLYMARKET_PROXY_ADDRESS",
    "TELEGRAM_CHAT_ID",
}
SAFE_PUBLIC_KEYS = {
    "DRY_RUN",
    "ENABLE_REAL_ORDERS",
    "SIGNATURE_TYPE",
    "POLYGON_RPC_URL",
    "HTTP_TIMEOUT_SEC",
    "WS_PING_INTERVAL_SEC",
    "WS_PING_TIMEOUT_SEC",
    "WS_MAX_RETRIES",
    "WS_RETRY_DELAY_SEC",
    "ORDER_POLL_INTERVAL_SEC",
    "ORDER_POLL_MAX_WAIT_SEC",
    "FOK_FALLBACK_TO_GTC",
    "BOOK_MAX_STALENESS_SEC",
    "DAILY_MAX_LOSS_USDC",
    "NAKED_DAILY_DRAWDOWN_STOP",
    "ABORT_ON_UNKNOWN_API_ERROR",
    "LOG_DIR",
    "LOG_LEVEL",
    "HEALTH_CHECK_PORT",
    "HTTP_USER_AGENT",
    "MARKET_UNDERLYING",
    "MARKET_HORIZON_MINUTES",
    "MARKET_REFRESH_INTERVAL_SEC",
    "MIN_USDC_ALLOWANCE_MULTIPLE",
    "EQUITY_REFRESH_INTERVAL_SEC",
    "RECONCILE_INTERVAL_SEC",
    "STATE_DB_PATH",
    "INSTANCE_LOCK_PATH",
}


def _mask_partial(value: str) -> str:
    s = value.strip()
    if not s or len(s) <= 10:
        return "***"
    return f"{s[:6]}...{s[-4:]}"


@dataclass
class ConfigSnapshot:
    env_file: str = ".env"
    artifact_path: Optional[str] = None

    def build(self) -> dict[str, Any]:
        return {
            "env_file": self.env_file,
            "env_redacted": self._read_env_redacted(),
            "artifact": self._read_artifact_summary(),
        }

    def _read_env_redacted(self) -> dict[str, str]:
        out: dict[str, str] = {}
        p = Path(self.env_file)
        if not p.is_file():
            return {"_warning": f".env not found: {self.env_file}"}
        try:
            for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip()
                if not k:
                    continue
                if k in SECRET_KEYS:
                    out[k] = "[REDACTED]" if v else "[EMPTY]"
                elif k in PARTIAL_KEYS:
                    out[k] = _mask_partial(v) if v else "[EMPTY]"
                elif k in SAFE_PUBLIC_KEYS:
                    out[k] = v if v else "[EMPTY]"
                else:
                    out[k] = _mask_partial(v) if v else "[EMPTY]"
        except Exception as e:
            return {"_error": f"read .env failed: {e}"}
        return out

    def _read_artifact_summary(self) -> dict[str, Any]:
        path = self.artifact_path
        if not path:
            return {"path": None, "exists": False, "_warning": "no artifact configured"}
        p = Path(path)
        out: dict[str, Any] = {"path": str(p), "exists": p.is_file()}
        if not p.is_file():
            return out
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            out["_error"] = f"parse failed: {e}"
            return out

        schema = str(payload.get("schema_version") or "")
        out["schema_version"] = payload.get("schema_version")
        out["trained_at"] = payload.get("trained_at")
        if schema == "third_digit_prefix_dynamic_v1":
            buckets = payload.get("prefix_buckets") if isinstance(payload.get("prefix_buckets"), dict) else {}
            out["model_kind"] = "prefix_bucket_survival"
            out["params"] = {
                "gamma": payload.get("gamma"),
                "beta": payload.get("beta"),
                "dynamic_a": payload.get("dynamic_a"),
                "dynamic_c": payload.get("dynamic_c"),
                "symbol": payload.get("symbol"),
            }
            out["prefix_model"] = {
                "bucket_count": len(buckets),
                "first3_count": sum(1 for k in buckets if isinstance(k, str) and len(k) == 3),
                "first4_count": sum(1 for k in buckets if isinstance(k, str) and len(k) == 4),
                "focus_buckets": {
                    k: {
                        "n_windows": v.get("n_windows"),
                        "event_rate": v.get("event_rate"),
                        "a": v.get("a"),
                        "c": v.get("c"),
                    }
                    for k, v in buckets.items()
                    if k in {"000", "111", "010", "101", "0000", "0001", "1110", "1111"}
                    and isinstance(v, dict)
                },
            }
            out["training_meta"] = payload.get("training_meta", {})
        else:
            out["model_kind"] = "legacy_global_survival"
            out["params"] = payload.get("params", {})
            out["thresholds"] = payload.get("thresholds", {})
            out["baseline_prob"] = (payload.get("calibration") or {}).get("baseline_prob")
            out["data_meta"] = payload.get("data_meta", {})
            ci = payload.get("ci")
            if isinstance(ci, dict):
                out["ci"] = ci
        return out
