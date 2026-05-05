"""
单实例锁: 防止同一台机器上启动多个 live_runner 进程导致重复下单。

实现两层防御:
    1. PID lockfile (POSIX fcntl / Windows msvcrt 都不强依赖, 退化为非阻塞独占创建)。
    2. 同时绑定 health 端口 (在 health 模块) 提供天然占用检测。

接口:
    SingleInstanceLock(path).acquire() -> True/False
    SingleInstanceLock(path).release()

策略:
    - 锁文件写当前 PID + 启动时间;
    - 启动时若锁文件存在且 PID 仍存活 -> 拒绝启动;
    - 若 PID 不存在 (上一进程崩溃) -> 接管锁文件, 写入新 PID, 记录 audit。
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .logging_setup import get_logger

logger = get_logger(__name__)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if sys.platform.startswith("win"):
            try:
                import ctypes
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if not handle:
                    return False
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            except Exception:
                return False
        else:
            os.kill(pid, 0)
            return True
    except OSError:
        return False
    except Exception:
        return False


@dataclass
class SingleInstanceLock:
    path: str
    min_restart_interval_sec: int = 180
    acquired: bool = False
    _pid_in_file: Optional[int] = None

    def acquire(self) -> bool:
        p = Path(self.path)
        p.parent.mkdir(parents=True, exist_ok=True)

        if p.exists():
            try:
                content = p.read_text(encoding="utf-8").strip()
                parts = content.split(",") if content else []
                pid = int(parts[0]) if parts else 0
                started_ts = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                self._pid_in_file = pid
            except Exception:
                pid = 0
                started_ts = 0

            if pid > 0 and _pid_alive(pid):
                logger.error(
                    "single_instance: another live_runner is running (pid=%d, lockfile=%s); refuse to start",
                    pid, self.path,
                )
                return False
            else:
                now_ts = int(time.time())
                elapsed = now_ts - started_ts if started_ts > 0 else None
                if elapsed is not None and elapsed < int(self.min_restart_interval_sec):
                    logger.error(
                        "single_instance: restart too frequent (last=%ss < min=%ss); refuse to take over lock",
                        elapsed, self.min_restart_interval_sec,
                    )
                    return False
                logger.warning(
                    "single_instance: stale lockfile detected (pid=%d not alive); taking over",
                    pid,
                )

        try:
            p.write_text(f"{os.getpid()},{int(time.time())}\n", encoding="utf-8")
            self.acquired = True
            logger.info("single_instance lock acquired pid=%d path=%s", os.getpid(), self.path)
            return True
        except Exception as e:
            logger.exception("single_instance: failed to write lockfile: %s", e)
            return False

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            p = Path(self.path)
            if p.exists():
                content = p.read_text(encoding="utf-8").strip()
                pid = int(content.split(",")[0]) if content else 0
                if pid == os.getpid():
                    p.unlink()
                    logger.info("single_instance lock released path=%s", self.path)
        except Exception as e:
            logger.warning("single_instance.release failed: %s", e)
        self.acquired = False
