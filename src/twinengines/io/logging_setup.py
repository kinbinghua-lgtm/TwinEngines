"""
统一 logging 配置: 文件滚动 + stdout 双输出, 与 IO 层 / 风控 / 决策共享。

设计原则:
    - 实盘禁止 print, 全部走 logger
    - 文件滚动 (按大小, 默认 10MB x 7), 防 VPS 磁盘炸
    - 关键决策点 (信号触发/开仓/平仓/拒单/优雅退出) 必须走 INFO+
    - 私钥/API secret 严禁出现在日志中, 由调用方负责

使用:
    from twinengines.io import setup_logging, get_logger
    setup_logging(level="INFO", log_dir="logs")
    log = get_logger("strategy")
    log.info("signal=%s stake=%.2f", sig, stake)
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional


_INITIALIZED = False


def setup_logging(
    *,
    level: str = "INFO",
    log_dir: str = "logs",
    file_name: str = "twinengines.log",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 7,
    fmt: Optional[str] = None,
) -> None:
    """
    幂等初始化全局 root logger。
    多次调用只生效一次, 避免重复 handler 引起日志放大。
    """
    global _INITIALIZED
    if _INITIALIZED:
        return

    if fmt is None:
        fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    formatter = logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    for h in list(root.handlers):
        root.removeHandler(h)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    root.addHandler(sh)

    try:
        os.makedirs(log_dir, exist_ok=True)
        fh = RotatingFileHandler(
            os.path.join(log_dir, file_name),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        fh.setFormatter(formatter)
        root.addHandler(fh)
    except OSError as e:
        root.warning("log file init failed (%s); fallback to stdout only", e)

    # py_clob_client_v2 在某些 403 场景会把 Cloudflare HTML 全量写到日志，降级该 logger 降噪
    logging.getLogger("py_clob_client_v2").setLevel(logging.CRITICAL)
    logging.getLogger("py_clob_client_v2.http_helpers.helpers").setLevel(logging.CRITICAL)

    _INITIALIZED = True


def get_logger(name: str) -> logging.Logger:
    """命名 logger 助手, 自动用全局 root handler。"""
    return logging.getLogger(name)
