"""
时钟同步 (Binance /api/v3/time) + 本地漂移检测。

实盘场景:
    - 5 分钟窗口的入场/出场判定都依赖"本地秒"与"币安服务器秒"的对齐;
    - VPS / NTP 漂移 > 1s 时, 第 4 分钟末的"剩余 60s"判定可能错位, 进而导致信号窗口提前关闭。

设计:
    1. 启动时拉一次 /api/v3/time, 算 offset_ms = server_ms - local_ms。
    2. 后台线程每 60s 重测一次, 滑动平均 (剔除 RTT/2)。
    3. 若 |offset_ms| > drift_alert_ms, 触发告警回调 (默认日志 ERROR)。
    4. 提供 now_ms() / now_s_aligned() 给上层使用, 替代 time.time()。

依赖:
    标准库 + requests (lazy import, 装不上时优雅退化为本地时钟 + 警告)。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class ClockSyncCfg:
    binance_time_url: str = "https://api.binance.com/api/v3/time"
    sync_interval_sec: float = 60.0
    drift_alert_ms: float = 1500.0
    request_timeout_sec: float = 5.0
    smoothing: float = 0.4  # 0=完全用新值, 1=完全用旧值


@dataclass
class ClockSync:
    cfg: ClockSyncCfg = field(default_factory=ClockSyncCfg)
    on_drift_alert: Optional[Callable[[float], None]] = None

    _offset_ms: float = 0.0
    _last_sync_ts_ms: float = 0.0
    _last_rtt_ms: float = 0.0
    _running: bool = False
    _thread: Optional[threading.Thread] = None
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _stop_evt: threading.Event = field(default_factory=threading.Event)
    _sync_count: int = 0
    _fail_count: int = 0
    _last_error: Optional[str] = None

    def now_ms(self) -> float:
        with self._lock:
            return time.time() * 1000.0 + self._offset_ms

    def now_s(self) -> float:
        return self.now_ms() / 1000.0

    @property
    def offset_ms(self) -> float:
        with self._lock:
            return self._offset_ms

    @property
    def healthy(self) -> bool:
        with self._lock:
            if self._sync_count == 0:
                return False
            if abs(self._offset_ms) > self.cfg.drift_alert_ms:
                return False
            stale_sec = (time.time() * 1000.0 - self._last_sync_ts_ms) / 1000.0
            if stale_sec > self.cfg.sync_interval_sec * 3:
                return False
            return True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "offset_ms": round(self._offset_ms, 2),
                "last_rtt_ms": round(self._last_rtt_ms, 2),
                "last_sync_ts_ms": int(self._last_sync_ts_ms),
                "sync_count": self._sync_count,
                "fail_count": self._fail_count,
                "last_error": self._last_error,
                "healthy": self.healthy,
            }

    def sync_once(self) -> bool:
        try:
            import requests  # lazy import
        except Exception as e:  # pragma: no cover
            logger.warning("requests not installed; clock_sync degraded to local clock (%s)", e)
            return False

        try:
            t0 = time.time()
            resp = requests.get(
                self.cfg.binance_time_url,
                timeout=self.cfg.request_timeout_sec,
                headers={"User-Agent": "TwinEngines-ClockSync/1.0"},
            )
            t1 = time.time()
            if resp.status_code != 200:
                self._record_fail(f"http {resp.status_code}")
                return False
            data = resp.json()
            server_ms = float(data.get("serverTime", 0))
            if server_ms <= 0:
                self._record_fail("empty serverTime")
                return False
            rtt_ms = (t1 - t0) * 1000.0
            local_mid_ms = (t0 + t1) / 2.0 * 1000.0
            new_offset = server_ms - local_mid_ms

            with self._lock:
                if self._sync_count == 0:
                    self._offset_ms = new_offset
                else:
                    s = max(0.0, min(1.0, self.cfg.smoothing))
                    self._offset_ms = s * self._offset_ms + (1.0 - s) * new_offset
                self._last_rtt_ms = rtt_ms
                self._last_sync_ts_ms = time.time() * 1000.0
                self._sync_count += 1
                self._last_error = None
                offset_now = self._offset_ms

            if abs(offset_now) > self.cfg.drift_alert_ms:
                logger.error(
                    "clock drift alert: offset_ms=%.1f exceeds %.1f",
                    offset_now, self.cfg.drift_alert_ms,
                )
                if self.on_drift_alert is not None:
                    try:
                        self.on_drift_alert(offset_now)
                    except Exception as cb_err:
                        logger.exception("drift alert callback failed: %s", cb_err)
            else:
                logger.info("clock sync ok: offset_ms=%.1f rtt_ms=%.1f", offset_now, rtt_ms)
            return True
        except Exception as e:
            self._record_fail(str(e))
            return False

    def _record_fail(self, reason: str) -> None:
        with self._lock:
            self._fail_count += 1
            self._last_error = reason
        logger.warning("clock sync failed: %s", reason)

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_evt.clear()
        self.sync_once()
        self._thread = threading.Thread(target=self._loop, name="ClockSyncLoop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
        self._stop_evt.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)

    def _loop(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    return
                interval = self.cfg.sync_interval_sec
            if self._stop_evt.wait(interval):
                return
            with self._lock:
                if not self._running:
                    return
            self.sync_once()
