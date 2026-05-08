"""
Polymarket 5 分钟 BTC 合约轮换发现器。

Polymarket 上的 5 分钟价格合约是周期性的, 每 5 分钟一个新 condition_id;
策略要在合适的窗口下单, 必须能动态查询当前 active 的 condition_id + 双侧 token_ids。

接口:
    Gamma API: GET https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=N
    返回字段中包含:
        - conditionId: 反转/顺势使用的 CTF condition
        - clobTokenIds: 两个 token_id, 对应 YES/NO 两侧
        - endDate / endDateIso: 合约结束时间 (UTC)
        - question: 标题, 用于关键字筛选 (BTC + 5min)
        - outcomes: ["Yes","No"] / ["Up","Down"]

设计:
    1. 后台线程每 market_refresh_interval_sec 拉一次 markets list;
    2. 用关键字 (Bitcoin / BTC) + 时长 (=5min) 筛出当前活动合约;
    3. 缓存当前合约信息, 提供 get_active() 给上层使用;
    4. 失败有重试 + 降级 (返回上一次缓存); 持续失败触发告警。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Callable, Optional

from .logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class ActiveMarket:
    condition_id: str
    question: str
    end_iso: str                      # 合约结束 ISO 时间
    end_ts_ms: int                    # 合约结束 ms
    token_id_yes: str
    token_id_no: str
    raw: dict = field(default_factory=dict)


@dataclass
class MarketResolverCfg:
    url: str = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=200"
    keywords: tuple = ("Bitcoin", "BTC")
    horizon_minutes: int = 5
    refresh_interval_sec: float = 5.0
    request_timeout_sec: float = 8.0
    user_agent: str = "TwinEngines/1.0"
    enable_slug_fallback: bool = True
    gamma_base: str = "https://gamma-api.polymarket.com"


@dataclass
class MarketResolver:
    cfg: MarketResolverCfg = field(default_factory=MarketResolverCfg)
    on_change: Optional[Callable[[Optional[ActiveMarket]], None]] = None
    on_status: Optional[Callable[[str, dict], None]] = None

    _running: bool = False
    _thread: Optional[threading.Thread] = None
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _stop_evt: threading.Event = field(default_factory=threading.Event)
    _active: Optional[ActiveMarket] = None
    _last_refresh_ts_ms: float = 0.0
    _refresh_count: int = 0
    _fail_count: int = 0
    _last_error: Optional[str] = None

    # ---------------- 控制 ----------------

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_evt.clear()
        self.refresh_once()
        self._thread = threading.Thread(target=self._loop, name="MarketResolver", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # ---------------- 状态 ----------------

    def get_active(self) -> Optional[ActiveMarket]:
        with self._lock:
            active = self._active
            expired = bool(active is not None and active.end_ts_ms > 0 and active.end_ts_ms < time.time() * 1000)
        if active is None or expired:
            self.refresh_once()
        with self._lock:
            if self._active is None:
                return None
            if self._active.end_ts_ms > 0 and self._active.end_ts_ms < time.time() * 1000:
                return None
            return self._active

    def snapshot(self) -> dict:
        with self._lock:
            a = self._active
            return {
                "active": None if a is None else {
                    "condition_id": a.condition_id,
                    "question": a.question,
                    "end_iso": a.end_iso,
                    "end_ts_ms": a.end_ts_ms,
                    "token_id_yes": a.token_id_yes[:14] + "..." if len(a.token_id_yes) > 14 else a.token_id_yes,
                    "token_id_no": a.token_id_no[:14] + "..." if len(a.token_id_no) > 14 else a.token_id_no,
                },
                "last_refresh_ts_ms": int(self._last_refresh_ts_ms),
                "refresh_count": self._refresh_count,
                "fail_count": self._fail_count,
                "last_error": self._last_error,
                "healthy": self._healthy_locked(),
            }

    @property
    def healthy(self) -> bool:
        with self._lock:
            return self._healthy_locked()

    def _healthy_locked(self) -> bool:
        if self._refresh_count == 0:
            return False
        stale_sec = (time.time() * 1000.0 - self._last_refresh_ts_ms) / 1000.0
        if stale_sec > self.cfg.refresh_interval_sec * 3.0:
            return False
        return self._active is not None

    # ---------------- 核心刷新 ----------------

    def refresh_once(self) -> bool:
        try:
            import requests  # type: ignore
        except Exception as e:
            logger.error("requests not installed; market_resolver disabled: %s", e)
            return False
        try:
            resp = requests.get(
                self.cfg.url,
                timeout=self.cfg.request_timeout_sec,
                headers={"User-Agent": self.cfg.user_agent, "Accept": "application/json"},
            )
            if resp.status_code != 200:
                self._record_fail(f"http {resp.status_code}: {resp.text[:200]}")
                return False
            data = resp.json()
            if not isinstance(data, list):
                self._record_fail(f"unexpected payload type: {type(data).__name__}")
                return False

            picked = self._pick_active(data)
            if picked is None and self.cfg.enable_slug_fallback:
                picked = self._pick_active_via_slug(requests)
            with self._lock:
                old = self._active
                self._active = picked
                self._last_refresh_ts_ms = time.time() * 1000.0
                self._refresh_count += 1
                self._last_error = None

            if picked is None:
                logger.warning("market_resolver: no active 5min BTC market matched (kw=%s)", self.cfg.keywords)
                self._notify_status("no_active", {})
            else:
                changed = (old is None) or (old.condition_id != picked.condition_id)
                if changed:
                    logger.info(
                        "market_resolver active: cid=%s end=%s q=%s",
                        picked.condition_id, picked.end_iso, picked.question[:80],
                    )
                    self._notify_status("changed", {
                        "condition_id": picked.condition_id,
                        "end_iso": picked.end_iso,
                    })
                    if self.on_change is not None:
                        try:
                            self.on_change(picked)
                        except Exception as e:
                            logger.exception("on_change callback failed: %s", e)
            return True
        except Exception as e:
            self._record_fail(str(e))
            return False

    def _pick_active(self, items: list) -> Optional[ActiveMarket]:
        kws = tuple(k.lower() for k in self.cfg.keywords)
        now_ms = time.time() * 1000.0
        candidates: list[ActiveMarket] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                if it.get("closed") or not it.get("active", True):
                    continue
                question = str(it.get("question") or it.get("title") or "")
                if not any(kw in question.lower() for kw in kws):
                    continue

                # 关键: endDate(完整时分秒)优先, endDateIso(仅日期)仅兜底
                end_iso = str(it.get("endDate") or it.get("endDateIso") or "")
                end_ts_ms = _parse_iso_to_ms(end_iso)
                if end_ts_ms <= 0 or end_ts_ms < now_ms:
                    continue
                horizon_ms = (end_ts_ms - now_ms)
                expected_ms = self.cfg.horizon_minutes * 60_000
                if horizon_ms > expected_ms * 1.5:
                    continue

                token_ids = _parse_token_ids(it.get("clobTokenIds"))
                if not isinstance(token_ids, list) or len(token_ids) < 2:
                    continue

                cid = str(it.get("conditionId") or it.get("condition_id") or "")
                if not cid:
                    continue

                yes_id, no_id = str(token_ids[0]), str(token_ids[1])
                candidates.append(ActiveMarket(
                    condition_id=cid,
                    question=question,
                    end_iso=end_iso,
                    end_ts_ms=end_ts_ms,
                    token_id_yes=yes_id,
                    token_id_no=no_id,
                    raw=it,
                ))
            except Exception as e:
                logger.debug("market_resolver pick item err: %s", e)
                continue
        if not candidates:
            return None
        candidates.sort(key=lambda m: m.end_ts_ms)
        return candidates[0]

    def _pick_active_via_slug(self, requests_mod) -> Optional[ActiveMarket]:
        """参考实现兜底: 直接按当前 5min slug 查询 event，避免 markets 列表字段异常导致漏检。"""
        now_ms = self._binance_server_time_ms(requests_mod)
        if now_ms <= 0:
            now_ms = int(time.time() * 1000.0)
        win_ts = int((now_ms // 1000) // 300 * 300)
        slug = f"btc-updown-5m-{win_ts}"
        url = f"{self.cfg.gamma_base.rstrip('/')}/events/slug/{slug}"
        try:
            resp = requests_mod.get(
                url,
                timeout=self.cfg.request_timeout_sec,
                headers={"User-Agent": self.cfg.user_agent, "Accept": "application/json"},
            )
            if resp.status_code != 200:
                return None
            event = resp.json()
            markets = event.get("markets") if isinstance(event, dict) else None
            if not isinstance(markets, list) or not markets:
                return None
            m = markets[0]
            question = str(m.get("question") or m.get("title") or "")
            end_iso = str(m.get("endDate") or m.get("endDateIso") or "")
            end_ts_ms = _parse_iso_to_ms(end_iso)
            token_ids = _parse_token_ids(m.get("clobTokenIds"))
            cid = str(m.get("conditionId") or m.get("condition_id") or "")
            if not cid or len(token_ids) < 2 or end_ts_ms <= int(time.time() * 1000):
                return None
            return ActiveMarket(
                condition_id=cid,
                question=question,
                end_iso=end_iso,
                end_ts_ms=end_ts_ms,
                token_id_yes=str(token_ids[0]),
                token_id_no=str(token_ids[1]),
                raw=m,
            )
        except Exception:
            return None

    def _binance_server_time_ms(self, requests_mod) -> int:
        try:
            resp = requests_mod.get(
                "https://api.binance.com/api/v3/time",
                timeout=min(5.0, self.cfg.request_timeout_sec),
                headers={"User-Agent": self.cfg.user_agent, "Accept": "application/json"},
            )
            if resp.status_code != 200:
                return 0
            data = resp.json()
            return int(data.get("serverTime") or 0)
        except Exception:
            return 0

    def _record_fail(self, reason: str) -> None:
        with self._lock:
            self._fail_count += 1
            self._last_error = reason
        logger.warning("market_resolver refresh failed: %s", reason)
        self._notify_status("refresh_failed", {"reason": reason})

    def _notify_status(self, state: str, info: dict) -> None:
        if self.on_status is None:
            return
        try:
            self.on_status(state, info)
        except Exception as e:
            logger.exception("market_resolver status callback failed: %s", e)

    def _loop(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    return
                interval = self.cfg.refresh_interval_sec
            if self._stop_evt.wait(interval):
                return
            with self._lock:
                if not self._running:
                    return
            self.refresh_once()


# ---------------- 工具 ----------------

def _parse_iso_to_ms(iso: str) -> int:
    if not iso:
        return 0
    try:
        s = iso.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000.0)
    except Exception:
        return 0


def _parse_token_ids(raw) -> list[str]:
    token_ids = raw or []
    if isinstance(token_ids, str):
        try:
            token_ids = json.loads(token_ids)
        except Exception:
            token_ids = []
    if not isinstance(token_ids, list):
        return []
    return [str(t) for t in token_ids if str(t)]
