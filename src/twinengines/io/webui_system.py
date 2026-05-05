"""
系统资源 / 环境信息 (无依赖, 纯 stdlib).

设计:
    - psutil 可选: 装了就给 CPU/RAM/磁盘细粒度; 没装也能跑
    - git 信息: 用 subprocess 跑一次 `git rev-parse`; 仓库不存在 = 空字典
    - python / OS / 时区 / 时钟漂移 (取自 health metrics 或 fallback)
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SystemInfo:
    project_root: str = "."

    def snapshot(self) -> dict[str, Any]:
        return {
            "host": self._host(),
            "python": self._python(),
            "process": self._process(),
            "resources": self._resources(),
            "git": self._git(),
            "disk": self._disk(),
        }

    # ---------------- host ----------------

    def _host(self) -> dict[str, Any]:
        return {
            "node": platform.node(),
            "os": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "tz_name": time.tzname[0] if time.tzname else "",
            "utc_now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "local_now": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _python(self) -> dict[str, Any]:
        return {
            "version": sys.version.split()[0],
            "executable": sys.executable,
            "implementation": platform.python_implementation(),
        }

    # ---------------- 进程 ----------------

    def _process(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "webui_pid": os.getpid(),
            "started_ms": int(time.time() * 1000),
        }
        try:
            import psutil  # type: ignore[import-not-found]
            p = psutil.Process(os.getpid())
            out["webui_rss_mb"] = round(p.memory_info().rss / 1024 / 1024, 1)
            out["webui_cpu_percent"] = p.cpu_percent(interval=0.05)
            out["webui_threads"] = p.num_threads()
            out["webui_uptime_sec"] = int(time.time() - p.create_time())
        except Exception:
            out["webui_rss_mb"] = None
            out["webui_cpu_percent"] = None
        return out

    def _resources(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            import psutil  # type: ignore[import-not-found]
            vm = psutil.virtual_memory()
            out["mem_total_gb"] = round(vm.total / 1024**3, 2)
            out["mem_used_gb"] = round(vm.used / 1024**3, 2)
            out["mem_percent"] = vm.percent
            out["cpu_percent"] = psutil.cpu_percent(interval=0.1)
            out["load_1m"] = (os.getloadavg()[0]
                              if hasattr(os, "getloadavg") else None)
        except Exception:
            out["psutil_available"] = False
        else:
            out["psutil_available"] = True
        return out

    # ---------------- git ----------------

    def _git(self) -> dict[str, Any]:
        out: dict[str, Any] = {"available": False}
        if not Path(self.project_root, ".git").exists():
            return out
        try:
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=self.project_root,
                capture_output=True, text=True, timeout=3,
            )
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.project_root,
                capture_output=True, text=True, timeout=3,
            )
            short = subprocess.run(
                ["git", "log", "-1", "--pretty=%h %s (%ci)"], cwd=self.project_root,
                capture_output=True, text=True, timeout=3,
            )
            out["available"] = True
            out["commit"] = commit.stdout.strip()
            out["branch"] = branch.stdout.strip()
            out["last_log"] = short.stdout.strip()
            try:
                dirty = subprocess.run(
                    ["git", "status", "--porcelain"], cwd=self.project_root,
                    capture_output=True, text=True, timeout=3,
                )
                out["dirty"] = bool(dirty.stdout.strip())
                out["dirty_files"] = len(dirty.stdout.strip().splitlines())
            except Exception:
                pass
        except Exception as e:
            out["error"] = str(e)
        return out

    # ---------------- 磁盘 ----------------

    def _disk(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            usage = shutil.disk_usage(self.project_root)
            out["root_total_gb"] = round(usage.total / 1024**3, 2)
            out["root_used_gb"] = round(usage.used / 1024**3, 2)
            out["root_free_gb"] = round(usage.free / 1024**3, 2)
            out["root_percent"] = round(usage.used / usage.total * 100, 1) \
                if usage.total else None
        except Exception as e:
            out["error"] = str(e)
        # 关键目录 size
        for sub in ("logs", "data_runtime", "reports"):
            p = Path(self.project_root, sub)
            if p.is_dir():
                size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                out[f"{sub}_mb"] = round(size / 1024 / 1024, 2)
        return out
