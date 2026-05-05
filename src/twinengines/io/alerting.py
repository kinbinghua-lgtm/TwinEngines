"""
告警通道: 把关键事件推到外部 (Telegram / 日志 / 文件), 便于运维秒级响应。

设计:
    - 多 Sink 组合: LogSink / FileSink / TelegramSink, 任意启用一个或多个;
    - dispatch(level, kind, payload) 统一入口, 内部带去重 + 节流 (每 kind 60s 内不重复);
    - Telegram 用 requests POST, 失败不阻塞 (静默重试 + 日志);
    - 默认就有 LogSink 兜底, 任何告警至少会留在日志里。

使用:
    alerting = AlertingDispatcher()
    alerting.add_sink(LogSink())
    alerting.add_sink(TelegramSink(bot_token=..., chat_id=..., user_agent=...))
    alerting.alert("warn", "ws_disconnected", {"reason": "..."})
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

from .logging_setup import get_logger

logger = get_logger(__name__)


# ---------------- 抽象 Sink ----------------

class Sink:
    def emit(self, level: str, kind: str, payload: dict) -> None:
        raise NotImplementedError


# ---------------- LogSink ----------------

@dataclass
class LogSink(Sink):
    def emit(self, level: str, kind: str, payload: dict) -> None:
        msg = f"ALERT [{level}] kind={kind} payload={json.dumps(payload, ensure_ascii=False, default=str)}"
        lvl = (level or "info").lower()
        if lvl in ("error", "fatal", "critical"):
            logger.error(msg)
        elif lvl in ("warn", "warning"):
            logger.warning(msg)
        else:
            logger.info(msg)


# ---------------- FileSink ----------------

@dataclass
class FileSink(Sink):
    path: str

    def __post_init__(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        except Exception as e:
            logger.warning("FileSink mkdir failed: %s", e)

    def emit(self, level: str, kind: str, payload: dict) -> None:
        line = json.dumps(
            {
                "ts_ms": int(time.time() * 1000),
                "level": level,
                "kind": kind,
                "payload": payload,
            },
            ensure_ascii=False,
            default=str,
        )
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            logger.warning("FileSink write failed: %s", e)


# ---------------- TelegramSink ----------------

@dataclass
class TelegramSink(Sink):
    bot_token: str
    chat_id: str
    user_agent: str = "TwinEngines/1.0"
    timeout_sec: float = 5.0

    def emit(self, level: str, kind: str, payload: dict) -> None:
        if not self.bot_token or not self.chat_id:
            return
        text = f"[{level.upper()}] {kind}\n{json.dumps(payload, ensure_ascii=False, default=str)[:3500]}"
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            data = urllib.parse.urlencode({
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": "true",
            }).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                method="POST",
                headers={"User-Agent": self.user_agent},
            )
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as _:
                pass
        except Exception as e:
            logger.warning("TelegramSink emit failed: %s", e)


# ---------------- 调度器 ----------------

@dataclass
class AlertingDispatcher:
    dedup_seconds: float = 60.0
    sinks: list[Sink] = field(default_factory=list)
    _last_emit_ts: dict[str, float] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def add_sink(self, sink: Sink) -> None:
        with self._lock:
            self.sinks.append(sink)

    def alert(self, level: str, kind: str, payload: dict) -> None:
        with self._lock:
            now = time.time()
            last = self._last_emit_ts.get(kind, 0.0)
            if now - last < self.dedup_seconds and (level or "").lower() not in ("error", "fatal", "critical"):
                return
            self._last_emit_ts[kind] = now
            sinks = list(self.sinks)
        for s in sinks:
            try:
                s.emit(level, kind, payload)
            except Exception as e:
                logger.exception("alerting sink %s failed: %s", type(s).__name__, e)

    @classmethod
    def from_env(cls) -> "AlertingDispatcher":
        d = cls()
        d.add_sink(LogSink())
        log_dir = os.environ.get("LOG_DIR", "logs")
        d.add_sink(FileSink(path=os.path.join(log_dir, "alerts.jsonl")))
        bot = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if bot and chat:
            d.add_sink(TelegramSink(bot_token=bot, chat_id=chat))
            logger.info("AlertingDispatcher: Telegram sink enabled")
        return d
