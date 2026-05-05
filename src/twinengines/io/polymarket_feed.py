"""
Polymarket CLOB WebSocket 订单簿主路 (与 BinanceFeed 对齐的"WS主路 + REST 备路"设计).

设计原则:
    1. WS 主路: 订阅 wss://ws-subscriptions-clob.polymarket.com/ws/market
       接收 ``book`` / ``price_change`` / ``tick_size_change`` 事件;
    2. REST 备路: 由 ``PolymarketClient.fetch_book`` 直接负责, 此模块不做 REST 轮询;
       上层调用 ``get_book(token_id)`` 时, 若 WS 缓存陈旧/缺失, **由调用方** fallback REST;
    3. 时间同步: 不使用本地 ``time.time()`` 判 stale, 用 ``ClockSync.now_ms`` (可注入)
       默认退化到本地时钟, 但与 ``BinanceFeed`` 一致地暴露 ``last_msg_ts_ms`` 给监控;
    4. 市场轮换:
       - ``subscribe([token_ids...])`` 在 ``MarketResolver`` 触发市场切换时调用,
       - 内部会断开旧 WS, 用新订阅集合重连;
    5. 任何异常都不抛出, 由 ``snapshot()`` / ``healthy`` 暴露状态给监控/告警通道。

线程模型:
    - 单后台 daemon 线程跑 WebSocketApp.run_forever;
    - 主线程仅读 ``_books`` 缓存, 加锁;
    - ``stop()`` 通过 ``Event`` + ``ws.close()`` 立即唤醒, ≤ 2s 完成 join。

依赖:
    - websocket-client (lazy import, 缺失时静默退化为 "WS 不可用", 上层走 REST)。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .logging_setup import get_logger

logger = get_logger(__name__)


# ---------------- 配置 ----------------

@dataclass
class PolymarketFeedCfg:
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    user_agent: str = "TwinEngines-PolyFeed/1.0"
    ping_interval_sec: float = 20.0
    ping_timeout_sec: float = 10.0
    reconnect_backoff_sec: tuple[float, float] = (1.0, 30.0)
    fresh_threshold_sec: float = 5.0   # 缓存超过此值认为陈旧, 上层应回退 REST


# ---------------- 数据类型 ----------------

@dataclass
class BookSnapshot:
    asset_id: str
    best_bid: Optional[float]
    best_ask: Optional[float]
    bid_size_top: Optional[float]
    ask_size_top: Optional[float]
    bid_levels: int
    ask_levels: int
    market_hash: Optional[str]
    server_ts_ms: Optional[int]
    received_at_ms: int
    source: str = "ws"   # "ws" | "ws_pricechange"

    def to_dict(self) -> dict:
        return {
            "asset_id": self.asset_id[:14] + "..." if len(self.asset_id) > 14 else self.asset_id,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "bid_size_top": self.bid_size_top,
            "ask_size_top": self.ask_size_top,
            "bid_levels": self.bid_levels,
            "ask_levels": self.ask_levels,
            "server_ts_ms": self.server_ts_ms,
            "received_at_ms": self.received_at_ms,
            "source": self.source,
        }


# ---------------- 主类 ----------------

@dataclass
class PolymarketFeed:
    cfg: PolymarketFeedCfg = field(default_factory=PolymarketFeedCfg)
    on_status: Optional[Callable[[str, dict], None]] = None
    on_book: Optional[Callable[[BookSnapshot], None]] = None
    now_ms_provider: Optional[Callable[[], float]] = None  # 可注入 ClockSync.now_ms

    _running: bool = False
    _thread: Optional[threading.Thread] = None
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _stop_evt: threading.Event = field(default_factory=threading.Event)

    _ws_app: object = None
    _ws_alive: bool = False
    _ws_error_count: int = 0
    _ws_last_error: Optional[str] = None
    _last_msg_ts_ms: float = 0.0

    _subscribed: set[str] = field(default_factory=set)
    _books: dict[str, BookSnapshot] = field(default_factory=dict)

    # ---------------- 控制 ----------------

    def start(self, asset_ids: list[str] | None = None) -> None:
        with self._lock:
            if self._running:
                if asset_ids:
                    self._subscribed = {a for a in asset_ids if a}
                return
            self._running = True
            self._stop_evt.clear()
            if asset_ids:
                self._subscribed = {a for a in asset_ids if a}

        self._thread = threading.Thread(
            target=self._ws_loop, name="PolymarketFeedWS", daemon=True,
        )
        self._thread.start()
        logger.info(
            "PolymarketFeed started subs=%d", len(self._subscribed),
        )

    def stop(self) -> None:
        with self._lock:
            self._running = False
            ws = self._ws_app
        self._stop_evt.set()
        if ws is not None:
            try:
                ws.keep_running = False  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                ws.close()  # type: ignore[attr-defined]
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        logger.info("PolymarketFeed stopped")

    def subscribe(self, asset_ids: list[str]) -> None:
        """更新订阅集合, 触发 WS 重连 (新 token_ids 立即生效)."""
        new_set = {a for a in asset_ids if a}
        with self._lock:
            if new_set == self._subscribed:
                return
            self._subscribed = new_set
            ws = self._ws_app
        logger.info("PolymarketFeed subscriptions changed -> %d assets", len(new_set))
        if ws is not None:
            try:
                ws.close()  # type: ignore[attr-defined]
            except Exception:
                pass

    # ---------------- 状态 ----------------

    def get_book(self, asset_id: str) -> Optional[BookSnapshot]:
        with self._lock:
            book = self._books.get(asset_id)
            if book is None:
                return None
            now_ms = self._now_ms()
            stale_sec = (now_ms - book.received_at_ms) / 1000.0
            if stale_sec > self.cfg.fresh_threshold_sec:
                return None
            return book

    def is_fresh(self, asset_id: str) -> bool:
        return self.get_book(asset_id) is not None

    def snapshot(self) -> dict:
        with self._lock:
            now_ms = self._now_ms()
            stale_sec = (now_ms - self._last_msg_ts_ms) / 1000.0 if self._last_msg_ts_ms > 0 else None
            return {
                "ws_alive": self._ws_alive,
                "ws_error_count": self._ws_error_count,
                "ws_last_error": self._ws_last_error,
                "subscribed_count": len(self._subscribed),
                "cached_book_count": len(self._books),
                "last_msg_ts_ms": int(self._last_msg_ts_ms),
                "stale_sec": None if stale_sec is None else round(stale_sec, 2),
                "books": [b.to_dict() for b in self._books.values()],
            }

    @property
    def healthy(self) -> bool:
        with self._lock:
            if not self._ws_alive:
                return False
            if self._last_msg_ts_ms <= 0:
                return False
            stale_sec = (self._now_ms() - self._last_msg_ts_ms) / 1000.0
            return stale_sec < self.cfg.fresh_threshold_sec * 3.0

    # ---------------- 内部 ----------------

    def _now_ms(self) -> float:
        if self.now_ms_provider is not None:
            try:
                return float(self.now_ms_provider())
            except Exception:
                pass
        return time.time() * 1000.0

    def _notify_status(self, state: str, info: dict) -> None:
        if self.on_status is None:
            return
        try:
            self.on_status(state, info)
        except Exception as e:
            logger.exception("PolymarketFeed status callback failed: %s", e)

    def _ws_loop(self) -> None:
        try:
            import websocket  # type: ignore
        except Exception as e:
            logger.error("websocket-client not installed; PolymarketFeed disabled: %s", e)
            self._notify_status("ws_disabled", {"reason": "websocket-client missing"})
            return

        backoff_min, backoff_max = self.cfg.reconnect_backoff_sec
        backoff = backoff_min

        while True:
            with self._lock:
                if not self._running:
                    return
                subs = list(self._subscribed)

            if not subs:
                if self._stop_evt.wait(1.0):
                    return
                continue

            try:
                self._notify_status("ws_connecting", {"url": self.cfg.ws_url, "subs": len(subs)})
                ws_app = websocket.WebSocketApp(
                    self.cfg.ws_url,
                    header={"User-Agent": self.cfg.user_agent},
                    on_open=lambda ws: self._on_ws_open(ws, subs),
                    on_message=lambda ws, msg: self._on_ws_message(msg),
                    on_error=lambda ws, err: self._on_ws_error(err),
                    on_close=lambda ws, code, msg: self._on_ws_close(code, msg),
                )
                with self._lock:
                    self._ws_app = ws_app
                ws_app.run_forever(
                    ping_interval=self.cfg.ping_interval_sec,
                    ping_timeout=self.cfg.ping_timeout_sec,
                )
                backoff = backoff_min
            except Exception as e:
                with self._lock:
                    self._ws_alive = False
                    self._ws_error_count += 1
                    self._ws_last_error = str(e)
                logger.exception("PolymarketFeed WS crashed: %s", e)
                self._notify_status("ws_crashed", {"error": str(e)})

            with self._lock:
                if not self._running:
                    return

            sleep_s = backoff
            backoff = min(backoff * 2.0, backoff_max)
            logger.warning("PolymarketFeed WS disconnected; reconnect in %.1fs", sleep_s)
            if self._stop_evt.wait(sleep_s):
                return

    def _on_ws_open(self, ws, subs: list[str]) -> None:
        with self._lock:
            self._ws_alive = True
        try:
            sub_msg = json.dumps({"assets_ids": subs, "type": "market"})
            ws.send(sub_msg)
        except Exception as e:
            logger.warning("PolymarketFeed subscribe send failed: %s", e)
            self._notify_status("ws_sub_failed", {"error": str(e)})
            return
        logger.info("PolymarketFeed WS connected, subscribed=%d", len(subs))
        self._notify_status("ws_open", {"subs": len(subs)})

    def _on_ws_error(self, err) -> None:
        with self._lock:
            self._ws_alive = False
            self._ws_error_count += 1
            self._ws_last_error = str(err)
        logger.warning("PolymarketFeed WS error: %s", err)
        self._notify_status("ws_error", {"error": str(err)})

    def _on_ws_close(self, code, msg) -> None:
        with self._lock:
            self._ws_alive = False
        logger.warning("PolymarketFeed WS closed code=%s msg=%s", code, msg)
        self._notify_status("ws_close", {"code": code, "msg": msg})

    def _on_ws_message(self, msg: str) -> None:
        try:
            payload = json.loads(msg)
        except Exception:
            logger.debug("PolymarketFeed non-json message: %s", msg[:120])
            return

        events = payload if isinstance(payload, list) else [payload]
        with self._lock:
            self._last_msg_ts_ms = self._now_ms()

        for ev in events:
            if not isinstance(ev, dict):
                continue
            ev_type = ev.get("event_type") or ev.get("type")
            try:
                if ev_type == "book":
                    self._handle_book_event(ev)
                elif ev_type == "price_change":
                    self._handle_price_change(ev)
                elif ev_type == "tick_size_change":
                    self._handle_tick_size(ev)
            except Exception as e:
                logger.warning("PolymarketFeed event handle failed type=%s err=%s", ev_type, e)

    def _handle_book_event(self, ev: dict) -> None:
        asset_id = str(ev.get("asset_id") or "")
        if not asset_id:
            return
        bids = ev.get("bids") or []
        asks = ev.get("asks") or []
        best_bid = _safe_top_price(bids, max_side=True)
        best_ask = _safe_top_price(asks, max_side=False)
        bid_size_top = _safe_top_size(bids, max_side=True)
        ask_size_top = _safe_top_size(asks, max_side=False)
        snap = BookSnapshot(
            asset_id=asset_id,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_size_top=bid_size_top,
            ask_size_top=ask_size_top,
            bid_levels=len(bids),
            ask_levels=len(asks),
            market_hash=str(ev.get("hash") or "") or None,
            server_ts_ms=_safe_int(ev.get("timestamp")),
            received_at_ms=int(self._now_ms()),
            source="ws",
        )
        with self._lock:
            self._books[asset_id] = snap
        if self.on_book is not None:
            try:
                self.on_book(snap)
            except Exception as e:
                logger.exception("on_book callback failed: %s", e)

    def _handle_price_change(self, ev: dict) -> None:
        asset_id = str(ev.get("asset_id") or "")
        if not asset_id:
            return
        with self._lock:
            cur = self._books.get(asset_id)
        if cur is None:
            return
        side = (ev.get("side") or "").lower()
        try:
            new_price = float(ev.get("price"))
            new_size = float(ev.get("size", 0))
        except Exception:
            return

        best_bid = cur.best_bid
        best_ask = cur.best_ask
        bid_top = cur.bid_size_top
        ask_top = cur.ask_size_top
        if side == "buy" and (best_bid is None or new_price >= best_bid):
            best_bid = new_price if new_size > 0 else best_bid
            bid_top = new_size if new_size > 0 else bid_top
        elif side == "sell" and (best_ask is None or new_price <= best_ask):
            best_ask = new_price if new_size > 0 else best_ask
            ask_top = new_size if new_size > 0 else ask_top
        snap = BookSnapshot(
            asset_id=asset_id,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_size_top=bid_top,
            ask_size_top=ask_top,
            bid_levels=cur.bid_levels,
            ask_levels=cur.ask_levels,
            market_hash=cur.market_hash,
            server_ts_ms=_safe_int(ev.get("timestamp")) or cur.server_ts_ms,
            received_at_ms=int(self._now_ms()),
            source="ws_pricechange",
        )
        with self._lock:
            self._books[asset_id] = snap

    def _handle_tick_size(self, ev: dict) -> None:
        asset_id = str(ev.get("asset_id") or "")
        logger.info("PolymarketFeed tick_size_change asset=%s payload=%s", asset_id[:16], ev)


# ---------------- 工具 ----------------

def _safe_top_price(levels: list, *, max_side: bool) -> Optional[float]:
    """Polymarket 盘口 levels = [{"price":"0.42","size":"123"},...] (字符串)."""
    best: Optional[float] = None
    for lv in levels:
        try:
            p = float(lv.get("price"))
        except Exception:
            continue
        if best is None:
            best = p
            continue
        if max_side and p > best:
            best = p
        elif (not max_side) and p < best:
            best = p
    return best


def _safe_top_size(levels: list, *, max_side: bool) -> Optional[float]:
    best_p = _safe_top_price(levels, max_side=max_side)
    if best_p is None:
        return None
    total = 0.0
    for lv in levels:
        try:
            p = float(lv.get("price"))
            s = float(lv.get("size"))
        except Exception:
            continue
        if abs(p - best_p) < 1e-9:
            total += s
    return total if total > 0 else None


def _safe_int(x) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        return None
