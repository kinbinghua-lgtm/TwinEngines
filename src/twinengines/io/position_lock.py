"""
实盘"一窗一笔"门控 (WindowPositionLock)。

回测中 _replay_window 第一次成功 trade 后即 return, 天然只会下一笔。
但实盘是流式数据 + 同一窗口多次 evaluate signal, 必须显式锁定:
    1. 每个窗口由 (window_id 或 trigger_minute_open_ts) 唯一标识。
    2. 锁定后, 同窗口任何后续 signal 一律拒绝;
       即便信号变化 (TREND -> REVERSAL) 也不能再次入场。
    3. 锁定状态持久化到 SQLite, 重启后保持 (避免重复入场)。

接口:
    PositionLock.try_acquire(window_id) -> True/False
    PositionLock.release(window_id, reason)   # 窗口结算后释放
    PositionLock.is_locked(window_id) -> bool
    PositionLock.snapshot() -> dict           # 写入 state_store

线程安全: 内部 RLock, 单实例供 live_runner 共享。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .logging_setup import get_logger
from .state_store import StateStore, KEY_POSITION_LOCK

logger = get_logger(__name__)


@dataclass
class _LockEntry:
    window_id: str
    acquired_ts_ms: int
    side: str                # 信号侧 (TREND/REVERSAL)
    client_order_id: Optional[str] = None
    note: str = ""


@dataclass
class PositionLock:
    store: Optional[StateStore] = None
    auto_expire_sec: float = 600.0  # 安全网: 10 分钟还未释放强制清掉, 防止死锁
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _entries: dict[str, _LockEntry] = field(default_factory=dict)
    _released_history: list[dict] = field(default_factory=list)
    _max_history: int = 500

    def __post_init__(self) -> None:
        if self.store is not None:
            snap = self.store.get(KEY_POSITION_LOCK, default=None)
            if isinstance(snap, dict):
                try:
                    for wid, e in (snap.get("entries") or {}).items():
                        self._entries[wid] = _LockEntry(
                            window_id=wid,
                            acquired_ts_ms=int(e.get("acquired_ts_ms", time.time() * 1000)),
                            side=str(e.get("side", "?")),
                            client_order_id=e.get("client_order_id"),
                            note=str(e.get("note", "")),
                        )
                except Exception as ex:
                    logger.exception("position_lock restore failed: %s", ex)

    # ---------------- 主接口 ----------------

    def try_acquire(self, window_id: str, *, side: str, client_order_id: Optional[str] = None, note: str = "") -> bool:
        if not window_id:
            logger.error("position_lock.try_acquire: empty window_id rejected")
            return False
        with self._lock:
            self._gc_locked()
            if window_id in self._entries:
                e = self._entries[window_id]
                logger.warning(
                    "position_lock.try_acquire DENIED window_id=%s side_new=%s side_existing=%s coid=%s",
                    window_id, side, e.side, e.client_order_id,
                )
                return False
            self._entries[window_id] = _LockEntry(
                window_id=window_id,
                acquired_ts_ms=int(time.time() * 1000),
                side=side,
                client_order_id=client_order_id,
                note=note,
            )
            self._persist_locked()
        logger.info("position_lock acquired window_id=%s side=%s coid=%s", window_id, side, client_order_id)
        return True

    def release(self, window_id: str, *, reason: str = "settle") -> bool:
        with self._lock:
            e = self._entries.pop(window_id, None)
            if e is None:
                return False
            self._released_history.append({
                "window_id": window_id,
                "side": e.side,
                "acquired_ts_ms": e.acquired_ts_ms,
                "released_ts_ms": int(time.time() * 1000),
                "client_order_id": e.client_order_id,
                "reason": reason,
            })
            if len(self._released_history) > self._max_history:
                self._released_history = self._released_history[-self._max_history :]
            self._persist_locked()
        logger.info("position_lock released window_id=%s reason=%s", window_id, reason)
        return True

    def is_locked(self, window_id: str) -> bool:
        with self._lock:
            self._gc_locked()
            return window_id in self._entries

    def attach_order(self, window_id: str, client_order_id: str) -> None:
        with self._lock:
            e = self._entries.get(window_id)
            if e is not None:
                e.client_order_id = client_order_id
                self._persist_locked()

    # ---------------- 维护 ----------------

    def _gc_locked(self) -> None:
        if self.auto_expire_sec <= 0:
            return
        now_ms = time.time() * 1000.0
        expired: list[str] = []
        for wid, e in self._entries.items():
            if now_ms - e.acquired_ts_ms > self.auto_expire_sec * 1000.0:
                expired.append(wid)
        for wid in expired:
            logger.warning("position_lock auto-expired window_id=%s (>%.1fs)", wid, self.auto_expire_sec)
            self._released_history.append({
                "window_id": wid,
                "side": self._entries[wid].side,
                "acquired_ts_ms": self._entries[wid].acquired_ts_ms,
                "released_ts_ms": int(now_ms),
                "client_order_id": self._entries[wid].client_order_id,
                "reason": "auto_expire",
            })
            self._entries.pop(wid, None)
        if expired:
            self._persist_locked()

    def _persist_locked(self) -> None:
        if self.store is None:
            return
        snap = {
            "entries": {
                wid: {
                    "acquired_ts_ms": e.acquired_ts_ms,
                    "side": e.side,
                    "client_order_id": e.client_order_id,
                    "note": e.note,
                }
                for wid, e in self._entries.items()
            },
            "ts_ms": int(time.time() * 1000),
        }
        self.store.put(KEY_POSITION_LOCK, snap)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "active_count": len(self._entries),
                "active": [
                    {
                        "window_id": wid,
                        "side": e.side,
                        "client_order_id": e.client_order_id,
                        "acquired_ts_ms": e.acquired_ts_ms,
                    }
                    for wid, e in self._entries.items()
                ],
                "recent_released": self._released_history[-20:],
            }
