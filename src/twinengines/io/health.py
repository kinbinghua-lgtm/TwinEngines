"""
轻量健康检查 HTTP 端点 (实盘静默崩溃监控).

- 不引入 fastapi/flask, 用 stdlib http.server, 零依赖
- 单线程后台运行, 占用极低
- 暴露 /health, /metrics 两个端点

外部监控接入示例:
    GET /health  → 200 {"status": "ok", "uptime_sec": ...}  / 503 {"status": "halted", ...}
    GET /metrics → 200 {"trades_today": N, "pnl_usdc": X, "regime": "GROWTH", ...}

任何状态变更 (熔断/优雅退出) 都通过 set_halted(reason) 切到 503,
便于外部 Prometheus / cronjob / Telegram bot 监控告警。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class _State:
    started_at: float = time.time()
    halted: bool = False
    halt_reason: Optional[str] = None
    metrics_provider: Optional[Callable[[], dict[str, Any]]] = None


def set_halted(reason: str) -> None:
    _State.halted = True
    _State.halt_reason = reason
    logger.error("health status: HALTED (%s)", reason)


def set_metrics_provider(fn: Callable[[], dict[str, Any]]) -> None:
    _State.metrics_provider = fn


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def _write_json(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path.startswith("/health"):
            if _State.halted:
                self._write_json(503, {
                    "status": "halted",
                    "reason": _State.halt_reason,
                    "uptime_sec": int(time.time() - _State.started_at),
                })
            else:
                self._write_json(200, {
                    "status": "ok",
                    "uptime_sec": int(time.time() - _State.started_at),
                })
            return
        if self.path.startswith("/metrics"):
            metrics: dict[str, Any] = {"uptime_sec": int(time.time() - _State.started_at)}
            try:
                if _State.metrics_provider is not None:
                    extra = _State.metrics_provider() or {}
                    if isinstance(extra, dict):
                        metrics.update(extra)
            except Exception as e:
                metrics["metrics_error"] = str(e)
            self._write_json(200, metrics)
            return
        self._write_json(404, {"error": "not_found", "path": self.path})


def start_health_server(port: int) -> threading.Thread:
    """启动后台 HTTP 服务. 端口失败时记录日志但不抛出, 不影响主程序。"""
    def _serve() -> None:
        try:
            server = HTTPServer(("0.0.0.0", int(port)), _Handler)
            logger.info("health server listening on :%d", port)
            server.serve_forever()
        except OSError as e:
            logger.warning("health server start failed on :%d - %s", port, e)
        except Exception as e:
            logger.error("health server crashed: %s", e, exc_info=True)

    t = threading.Thread(target=_serve, name="health-server", daemon=True)
    t.start()
    return t
