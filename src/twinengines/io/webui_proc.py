"""
WebUI 子进程管理 (process orchestrator).

设计:
    1. 策略 / connectivity / git pull / shadow-report 都通过 subprocess 启动,
       面板进程本身不 import 任何策略代码;
    2. 策略进程 PID 落到 data_runtime/strategy.pid (附带 mode + cmd 元信息);
    3. 状态查询纯文件 + os.kill(pid, 0); 无后台线程;
    4. 启动新策略前会先 stop_strategy(), 防双开;
    5. 跨平台: Windows 用 CREATE_NEW_PROCESS_GROUP + Ctrl+Break; Linux 用 SIGTERM.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

IS_WINDOWS = os.name == "nt"


# ============================================================
# 常量
# ============================================================

PID_FILE = "data_runtime/strategy.pid"
STRATEGY_STDOUT = "logs/strategy_stdout.log"
STRATEGY_STDERR = "logs/strategy_stderr.log"

CONNECTIVITY_REPORT = "reports/connectivity_latest.json"
SHADOW_REPORT_JSON = "reports/shadow_latest.json"
SHADOW_REPORT_MD = "reports/shadow_latest.md"


# ============================================================
# 策略进程状态
# ============================================================

@dataclass
class StrategyStatus:
    running: bool
    pid: Optional[int]
    mode: Optional[str]              # "dry_run" | "live" | "dry_run_signals"
    started_at_ms: Optional[int]
    cmd: Optional[list[str]]
    stale_pidfile: bool = False      # PID 文件存在但进程已死


# ============================================================
# 进程管理器
# ============================================================

@dataclass
class ProcessManager:
    project_root: str = "."
    python_bin: str = sys.executable
    env_file: str = ".env"
    pid_file: str = PID_FILE
    stdout_path: str = STRATEGY_STDOUT
    stderr_path: str = STRATEGY_STDERR
    default_artifact: str = "artifact_btc_v6.json"
    default_naked_model_json: str = "reports/prefix_survival_model_90d_step10.json"
    default_naked_state_json: str = "data_runtime/naked_real_state.json"

    # ---------------- 状态查询 ----------------

    def status(self) -> StrategyStatus:
        meta = self._read_pidfile()
        sd_active = self._systemd_strategy_active()
        if meta is None:
            if sd_active:
                return StrategyStatus(
                    running=True,
                    pid=None,
                    mode="live",
                    started_at_ms=None,
                    cmd=["systemd", "twinengines-strategy.service"],
                )
            return StrategyStatus(running=False, pid=None, mode=None,
                                  started_at_ms=None, cmd=None)
        pid = meta.get("pid")
        if isinstance(pid, int) and self._pid_alive(pid):
            return StrategyStatus(
                running=True,
                pid=int(pid),
                mode=meta.get("mode"),
                started_at_ms=meta.get("started_at_ms"),
                cmd=meta.get("cmd"),
            )
        if sd_active:
            return StrategyStatus(
                running=True,
                pid=None,
                mode="live",
                started_at_ms=None,
                cmd=["systemd", "twinengines-strategy.service"],
            )
        return StrategyStatus(running=False, pid=pid, mode=meta.get("mode"),
                              started_at_ms=meta.get("started_at_ms"),
                              cmd=meta.get("cmd"), stale_pidfile=True)

    # ---------------- 启动 / 停止 ----------------

    def start_strategy(
        self,
        *,
        mode: str,
        artifact_path: Optional[str] = None,
        shadow_signal_log_path: str = "logs/shadow_signals.jsonl",
        disable_trend: bool = False,
        extra_args: Optional[list[str]] = None,
    ) -> tuple[bool, str, Optional[StrategyStatus]]:
        """启动策略子进程. mode ∈ {dry_run, live, dry_run_signals}."""
        if self.status().running:
            return False, "strategy already running, stop it first", self.status()

        cmd = [
            self.python_bin,
            "-m",
            "twinengines.cli",
            "naked-third-digit-live",
            "--env-file",
            self.env_file,
            "--model-json",
            self.default_naked_model_json,
            "--poll-sec",
            "8",
            "--confidence-min",
            "0.51",
            "--target-quote-usdc",
            "1",
            "--state-json",
            self.default_naked_state_json,
        ]
        if mode == "live":
            # live 模式默认真实下单（与 deploy/systemd/twinengines-strategy.service 对齐）
            cmd.append("--yes-real-money")
            cmd.append("--kelly-sizing")
        elif mode == "dry_run_signals":
            cmd += ["--bare-formula-eval"]
        elif mode == "dry_run":
            pass
        else:
            return False, f"unknown mode: {mode}", None

        if extra_args:
            cmd += list(extra_args)

        Path(self.stdout_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.pid_file).parent.mkdir(parents=True, exist_ok=True)

        try:
            popen_kwargs: dict[str, Any] = {
                "cwd": self.project_root,
                "stdout": open(self.stdout_path, "ab"),
                "stderr": open(self.stderr_path, "ab"),
                "stdin": subprocess.DEVNULL,
            }
            if IS_WINDOWS:
                popen_kwargs["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                )
            else:
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except Exception as e:
            return False, f"spawn failed: {e}", None

        meta = {
            "pid": proc.pid,
            "mode": mode,
            "started_at_ms": int(time.time() * 1000),
            "cmd": cmd,
        }
        self._write_pidfile(meta)
        return True, f"started pid={proc.pid} mode={mode}", self.status()

    def stop_strategy(self, *, timeout_sec: float = 10.0) -> tuple[bool, str]:
        if self._systemd_strategy_active():
            try:
                subprocess.run(
                    ["systemctl", "stop", "twinengines-strategy.service"],
                    capture_output=True,
                    text=True,
                    timeout=max(float(timeout_sec), 35.0),
                    check=False,
                )
                self._remove_pidfile()
                return True, "已停止 systemd 单元 twinengines-strategy.service"
            except Exception as e:
                return False, f"systemctl stop failed: {e}"

        meta = self._read_pidfile()
        if meta is None:
            return False, "no pidfile; nothing to stop"
        pid = meta.get("pid")
        if not isinstance(pid, int) or not self._pid_alive(pid):
            self._remove_pidfile()
            return True, "process not alive; pidfile cleaned"
        try:
            if IS_WINDOWS:
                os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            else:
                os.kill(pid, signal.SIGTERM)
        except Exception as e:
            return False, f"signal failed: {e}"

        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if not self._pid_alive(pid):
                self._remove_pidfile()
                return True, f"stopped pid={pid}"
            time.sleep(0.3)

        try:
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                               capture_output=True, timeout=5)
            else:
                os.kill(pid, signal.SIGKILL)
        except Exception as e:
            return False, f"force-kill failed: {e}"
        self._remove_pidfile()
        return True, f"force-killed pid={pid}"

    # ---------------- 一次性子命令 (同步, 短任务) ----------------

    def run_connectivity(self, *, check_allowance: bool = True,
                         timeout_sec: float = 60.0) -> tuple[bool, dict[str, Any]]:
        Path(CONNECTIVITY_REPORT).parent.mkdir(parents=True, exist_ok=True)
        cmd = [self.python_bin, "-m", "twinengines.cli", "connectivity",
               "--env-file", self.env_file, "--out", CONNECTIVITY_REPORT]
        if check_allowance:
            cmd.append("--check-allowance")
        try:
            r = subprocess.run(cmd, cwd=self.project_root, capture_output=True,
                               text=True, timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            return False, {"error": "timeout", "cmd": cmd}
        report: dict[str, Any] = {}
        try:
            if Path(CONNECTIVITY_REPORT).is_file():
                report = json.loads(Path(CONNECTIVITY_REPORT).read_text(encoding="utf-8"))
        except Exception:
            report = {}
        report["_exit_code"] = r.returncode
        report["_stderr_tail"] = (r.stderr or "")[-1000:]
        return r.returncode == 0, report

    def run_git_pull(self, *, timeout_sec: float = 60.0) -> tuple[bool, dict[str, Any]]:
        try:
            r = subprocess.run(["git", "pull", "--ff-only"], cwd=self.project_root,
                               capture_output=True, text=True, timeout=timeout_sec)
        except FileNotFoundError:
            return False, {"error": "git not installed"}
        except subprocess.TimeoutExpired:
            return False, {"error": "timeout"}
        return r.returncode == 0, {
            "exit_code": r.returncode,
            "stdout": r.stdout,
            "stderr": r.stderr,
        }

    def run_shadow_report(
        self,
        *,
        alerts_path: str = "logs/alerts.jsonl",
        state_db_path: str = "data_runtime/state.sqlite",
        window_hours: float = 168.0,
        timeout_sec: float = 60.0,
    ) -> tuple[bool, dict[str, Any]]:
        Path(SHADOW_REPORT_JSON).parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.python_bin, "-m", "twinengines.cli", "shadow-report",
            "--alerts", alerts_path,
            "--state-db", state_db_path,
            "--window-hours", str(window_hours),
            "--out", SHADOW_REPORT_JSON,
            "--markdown", SHADOW_REPORT_MD,
        ]
        try:
            r = subprocess.run(cmd, cwd=self.project_root, capture_output=True,
                               text=True, timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            return False, {"error": "timeout", "cmd": cmd}
        report: dict[str, Any] = {"_exit_code": r.returncode}
        try:
            if Path(SHADOW_REPORT_JSON).is_file():
                report["report"] = json.loads(
                    Path(SHADOW_REPORT_JSON).read_text(encoding="utf-8"))
                report["report_path"] = SHADOW_REPORT_JSON
                report["markdown_path"] = SHADOW_REPORT_MD
        except Exception as e:
            report["error"] = str(e)
        return r.returncode in (0, 2), report
        # exit_code 2 表示健康分低 -> 报告本身仍然合法, 可下载

    # ---------------- 内部 helpers ----------------

    def _systemd_strategy_active(self) -> bool:
        if IS_WINDOWS:
            return False
        try:
            r = subprocess.run(
                ["systemctl", "is-active", "twinengines-strategy.service"],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
            return r.returncode == 0 and (r.stdout or "").strip() == "active"
        except Exception:
            return False

    def _pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            if IS_WINDOWS:
                r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                   capture_output=True, text=True, timeout=3)
                return str(pid) in (r.stdout or "")
            else:
                os.kill(pid, 0)
                return True
        except (OSError, subprocess.SubprocessError):
            return False

    def _read_pidfile(self) -> Optional[dict]:
        try:
            return json.loads(Path(self.pid_file).read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_pidfile(self, meta: dict) -> None:
        Path(self.pid_file).write_text(
            json.dumps(meta, ensure_ascii=False, default=str), encoding="utf-8")

    def _remove_pidfile(self) -> None:
        try:
            os.remove(self.pid_file)
        except FileNotFoundError:
            pass
        except Exception:
            pass
