"""
状态持久化层 (SQLite)。

实盘场景:
    - 程序意外重启 (OOM / 部署 / 崩溃) 后, 必须从磁盘恢复:
      * RiskGuard._state (当日 drawdown / equity / halt 状态)
      * RegimeManager 当前阶段 (避免重启回到 ULTRA 阶段)
      * position_lock (本窗口是否已开过仓)
      * 上一笔已知 equity (与链上对账)

设计:
    - 用单文件 SQLite, 模块化按"键值表"组织; 字段固定, 用 JSON 存复杂结构。
    - 事务原子写入, 写一次刷一次 (commit 立即落盘)。
    - 无外部依赖, 仅标准库 sqlite3。
    - 任何读失败都不阻塞主流程 (返回 None / 默认值 + 日志告警)。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .logging_setup import get_logger

logger = get_logger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_ts_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS friction_param_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    operator TEXT NOT NULL,
    param TEXT NOT NULL,
    old_value REAL NOT NULL,
    new_value REAL NOT NULL,
    reason TEXT NOT NULL,
    proposal_path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts_ms);
CREATE INDEX IF NOT EXISTS idx_audit_kind ON audit_events(kind);
CREATE INDEX IF NOT EXISTS idx_friction_ts ON friction_param_audit(ts_ms);
CREATE INDEX IF NOT EXISTS idx_friction_param ON friction_param_audit(param);
"""


@dataclass
class StateStore:
    db_path: str
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=5.0,
            isolation_level=None,  # autocommit; we manage tx manually
        )
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    # ---------------- KV API ----------------

    def put(self, key: str, value: Any) -> None:
        try:
            payload = json.dumps(value, ensure_ascii=False, default=str)
        except Exception as e:
            logger.exception("state_store.put serialize failed key=%s err=%s", key, e)
            return
        ts_ms = int(time.time() * 1000.0)
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute(
                        "INSERT INTO kv_state(key,value,updated_ts_ms) VALUES(?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts_ms=excluded.updated_ts_ms",
                        (key, payload, ts_ms),
                    )
                    conn.commit()
            except Exception as e:
                logger.exception("state_store.put failed key=%s err=%s", key, e)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            try:
                with self._connect() as conn:
                    cur = conn.execute("SELECT value FROM kv_state WHERE key=?", (key,))
                    row = cur.fetchone()
            except Exception as e:
                logger.exception("state_store.get failed key=%s err=%s", key, e)
                return default
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except Exception as e:
            logger.exception("state_store.get json decode failed key=%s err=%s", key, e)
            return default

    def delete(self, key: str) -> None:
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute("DELETE FROM kv_state WHERE key=?", (key,))
                    conn.commit()
            except Exception as e:
                logger.exception("state_store.delete failed key=%s err=%s", key, e)

    # ---------------- Audit API ----------------

    def append_audit(self, kind: str, payload: dict) -> None:
        try:
            text = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception as e:
            logger.exception("audit serialize failed: %s", e)
            return
        ts_ms = int(time.time() * 1000.0)
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute(
                        "INSERT INTO audit_events(ts_ms,kind,payload) VALUES(?,?,?)",
                        (ts_ms, kind, text),
                    )
                    conn.commit()
            except Exception as e:
                logger.exception("audit insert failed: %s", e)

    def recent_audit(self, kind: Optional[str] = None, limit: int = 100) -> list[dict]:
        with self._lock:
            try:
                with self._connect() as conn:
                    if kind is None:
                        cur = conn.execute(
                            "SELECT ts_ms,kind,payload FROM audit_events ORDER BY id DESC LIMIT ?",
                            (limit,),
                        )
                    else:
                        cur = conn.execute(
                            "SELECT ts_ms,kind,payload FROM audit_events WHERE kind=? "
                            "ORDER BY id DESC LIMIT ?",
                            (kind, limit),
                        )
                    rows = cur.fetchall()
            except Exception as e:
                logger.exception("recent_audit failed: %s", e)
                return []

        out: list[dict] = []
        for ts_ms, k, text in rows:
            try:
                payload = json.loads(text)
            except Exception:
                payload = {"_raw": text}
            out.append({"ts_ms": ts_ms, "kind": k, "payload": payload})
        return out

    def append_friction_param_audit(
        self,
        *,
        operator: str,
        param: str,
        old_value: float,
        new_value: float,
        reason: str,
        proposal_path: str,
    ) -> None:
        ts_ms = int(time.time() * 1000.0)
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute(
                        "INSERT INTO friction_param_audit(ts_ms,operator,param,old_value,new_value,reason,proposal_path) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (
                            ts_ms,
                            str(operator),
                            str(param),
                            float(old_value),
                            float(new_value),
                            str(reason),
                            str(proposal_path),
                        ),
                    )
                    conn.commit()
            except Exception as e:
                logger.exception("append_friction_param_audit failed: %s", e)


# ---------------- 高层封装 ----------------
# 上层模块统一通过这些 key 存取, 避免硬编码字符串散落

KEY_RISK_GUARD = "risk_guard.state"
KEY_REGIME = "regime.current"
KEY_POSITION_LOCK = "position_lock.snapshot"
KEY_LAST_EQUITY = "account.last_equity_usdc"
KEY_LAST_RECONCILE_TS = "reconciliation.last_ts_ms"


def save_risk_state(store: StateStore, *, day_key: str, day_start_equity: float, peak_equity: float, halted: bool) -> None:
    store.put(KEY_RISK_GUARD, {
        "day_key": day_key,
        "day_start_equity": day_start_equity,
        "peak_equity": peak_equity,
        "halted": halted,
    })


def load_risk_state(store: StateStore) -> Optional[dict]:
    return store.get(KEY_RISK_GUARD, default=None)


def save_regime(store: StateStore, name: str, equity: float) -> None:
    store.put(KEY_REGIME, {"name": name, "equity": equity, "ts_ms": int(time.time() * 1000.0)})


def load_regime(store: StateStore) -> Optional[dict]:
    return store.get(KEY_REGIME, default=None)
