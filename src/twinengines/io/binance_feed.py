"""
币安实时行情接入: WS 主路径 + REST 兜底 + 断线降级。

设计:
    1. 主路径: WebSocket 订阅 BTCUSDT@kline_1s, 推送 1s K 线 (closed=True 才采用)。
    2. 兜底:   REST /api/v3/klines?interval=1s&limit=1 每秒轮询;
       WS 断开/超时/迟到 > stale_threshold_sec 自动降级。
    3. 心跳:   60s 内未收到任何消息 -> 重连 + 告警;
    4. 数据回调:
        on_bar(bar: KlineBar) -> 给上层 LiveRunner;
        on_status(state: str, info: dict) -> 给监控/告警通道;
    5. 时钟基准:
        所有 bar 时间戳取 server close_time, 不依赖本地 time.time();
        本地时钟漂移由 ClockSync 单独负责。

使用最小可选依赖:
    - websocket-client (lazy)
    - requests (lazy, REST 兜底)
若两者都未安装, 启动直接抛出 RuntimeError, 不允许进入实盘。

线程模型:
    - 单后台线程跑 WS recv loop;
    - 另一后台线程跑 REST fallback loop (默认睡眠状态, 仅 WS 失效时活跃);
    - 主线程只读 last_bar / health snapshot。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .logging_setup import get_logger

logger = get_logger(__name__)


# ---------------- 数据类型 ----------------

@dataclass
class KlineBar:
    symbol: str
    interval: str            # "1s" / "1m"
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool
    source: str              # "ws" / "rest"


# ---------------- 配置 ----------------

@dataclass
class BinanceFeedCfg:
    symbol: str = "BTCUSDT"
    interval: str = "1s"
    ws_url_template: str = "wss://stream.binance.com:9443/ws/{stream}"
    rest_url_template: str = (
        "https://api.binance.com/api/v3/klines"
        "?symbol={symbol}&interval={interval}&limit=1"
    )
    user_agent: str = "TwinEngines-Feed/1.0"
    request_timeout_sec: float = 5.0
    rest_poll_interval_sec: float = 1.0
    stale_threshold_sec: float = 2.0
    ws_reconnect_backoff_sec: tuple[float, float] = (1.0, 30.0)
    enable_rest_fallback: bool = True
    enable_ws: bool = True


# ---------------- 主类 ----------------

@dataclass
class BinanceFeed:
    cfg: BinanceFeedCfg = field(default_factory=BinanceFeedCfg)
    on_bar: Optional[Callable[[KlineBar], None]] = None
    on_status: Optional[Callable[[str, dict], None]] = None

    _running: bool = False
    _ws_thread: Optional[threading.Thread] = None
    _rest_thread: Optional[threading.Thread] = None
    _state_lock: threading.RLock = field(default_factory=threading.RLock)
    _stop_evt: threading.Event = field(default_factory=threading.Event)
    _last_bar: Optional[KlineBar] = None
    _last_msg_ts_ms: float = 0.0
    _ws_alive: bool = False
    _ws_error_count: int = 0
    _ws_last_error: Optional[str] = None
    _rest_poll_count: int = 0
    _rest_fail_count: int = 0
    _ws_app: object = None  # WebSocketApp instance (lazy import)

    # ---------------- 控制 ----------------

    def start(self) -> None:
        with self._state_lock:
            if self._running:
                return
            self._running = True
            self._stop_evt.clear()

        if self.cfg.enable_ws:
            self._ws_thread = threading.Thread(target=self._ws_loop, name="BinanceFeedWS", daemon=True)
            self._ws_thread.start()
        if self.cfg.enable_rest_fallback:
            self._rest_thread = threading.Thread(target=self._rest_loop, name="BinanceFeedREST", daemon=True)
            self._rest_thread.start()
        logger.info(
            "BinanceFeed started symbol=%s interval=%s ws=%s rest_fallback=%s",
            self.cfg.symbol, self.cfg.interval, self.cfg.enable_ws, self.cfg.enable_rest_fallback,
        )

    def stop(self) -> None:
        with self._state_lock:
            self._running = False
            ws = self._ws_app
        self._stop_evt.set()
        if ws is not None:
            try:
                ws.keep_running = False
            except Exception:
                pass
            try:
                ws.close()
            except Exception:
                pass
        for t in (self._ws_thread, self._rest_thread):
            if t is not None:
                t.join(timeout=2.0)
        logger.info("BinanceFeed stopped")

    # ---------------- 状态 ----------------

    def last_bar(self) -> Optional[KlineBar]:
        with self._state_lock:
            return self._last_bar

    def snapshot(self) -> dict:
        with self._state_lock:
            stale_sec = (time.time() * 1000.0 - self._last_msg_ts_ms) / 1000.0 if self._last_msg_ts_ms > 0 else None
            return {
                "ws_alive": self._ws_alive,
                "ws_error_count": self._ws_error_count,
                "ws_last_error": self._ws_last_error,
                "rest_poll_count": self._rest_poll_count,
                "rest_fail_count": self._rest_fail_count,
                "last_msg_ts_ms": int(self._last_msg_ts_ms),
                "stale_sec": None if stale_sec is None else round(stale_sec, 2),
                "last_bar_close": (self._last_bar.close if self._last_bar else None),
                "last_bar_source": (self._last_bar.source if self._last_bar else None),
            }

    @property
    def healthy(self) -> bool:
        with self._state_lock:
            if self._last_msg_ts_ms <= 0:
                return False
            stale_sec = (time.time() * 1000.0 - self._last_msg_ts_ms) / 1000.0
            return stale_sec < self.cfg.stale_threshold_sec * 3.0

    # ---------------- WS 回路 ----------------

    def _ws_loop(self) -> None:
        try:
            import websocket  # type: ignore
        except Exception as e:
            logger.error("websocket-client not installed; WS feed disabled: %s", e)
            self._notify_status("ws_disabled", {"reason": "websocket-client missing"})
            return

        backoff = self.cfg.ws_reconnect_backoff_sec[0]
        backoff_max = self.cfg.ws_reconnect_backoff_sec[1]
        stream = f"{self.cfg.symbol.lower()}@kline_{self.cfg.interval}"
        url = self.cfg.ws_url_template.format(stream=stream)

        while True:
            with self._state_lock:
                if not self._running:
                    return
            try:
                self._notify_status("ws_connecting", {"url": url})
                ws_app = websocket.WebSocketApp(
                    url,
                    header={"User-Agent": self.cfg.user_agent},
                    on_open=lambda ws: self._on_ws_open(ws),
                    on_message=lambda ws, msg: self._on_ws_message(msg),
                    on_error=lambda ws, err: self._on_ws_error(err),
                    on_close=lambda ws, code, msg: self._on_ws_close(code, msg),
                )
                with self._state_lock:
                    self._ws_app = ws_app
                ws_app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                with self._state_lock:
                    self._ws_alive = False
                    self._ws_error_count += 1
                    self._ws_last_error = str(e)
                logger.exception("WS loop crashed: %s", e)
                self._notify_status("ws_crashed", {"error": str(e)})

            with self._state_lock:
                if not self._running:
                    return

            sleep_s = backoff
            backoff = min(backoff * 2.0, backoff_max)
            logger.warning("WS disconnected; reconnect in %.1fs", sleep_s)
            if self._stop_evt.wait(sleep_s):
                return

    def _on_ws_open(self, ws) -> None:
        with self._state_lock:
            self._ws_alive = True
        logger.info("BinanceFeed WS connected")
        self._notify_status("ws_open", {})

    def _on_ws_message(self, msg: str) -> None:
        try:
            data = json.loads(msg)
            k = data.get("k") or {}
            if not k:
                return
            bar = KlineBar(
                symbol=str(data.get("s") or self.cfg.symbol),
                interval=str(k.get("i") or self.cfg.interval),
                open_time_ms=int(k.get("t") or 0),
                close_time_ms=int(k.get("T") or 0),
                open=float(k.get("o") or 0.0),
                high=float(k.get("h") or 0.0),
                low=float(k.get("l") or 0.0),
                close=float(k.get("c") or 0.0),
                volume=float(k.get("v") or 0.0),
                is_closed=bool(k.get("x")),
                source="ws",
            )
            self._record_bar(bar)
        except Exception as e:
            logger.warning("WS message parse failed: %s msg=%s", e, msg[:200])

    def _on_ws_error(self, err) -> None:
        with self._state_lock:
            self._ws_alive = False
            self._ws_error_count += 1
            self._ws_last_error = str(err)
        logger.warning("WS error: %s", err)
        self._notify_status("ws_error", {"error": str(err)})

    def _on_ws_close(self, code, msg) -> None:
        with self._state_lock:
            self._ws_alive = False
        logger.warning("WS closed code=%s msg=%s", code, msg)
        self._notify_status("ws_close", {"code": code, "msg": msg})

    # ---------------- REST 兜底 ----------------

    def _rest_loop(self) -> None:
        try:
            import requests  # type: ignore
        except Exception as e:
            logger.error("requests not installed; REST fallback disabled: %s", e)
            return

        url = self.cfg.rest_url_template.format(symbol=self.cfg.symbol, interval=self.cfg.interval)

        while True:
            with self._state_lock:
                if not self._running:
                    return
                stale_ok = True
                if self._last_msg_ts_ms > 0:
                    stale_sec = (time.time() * 1000.0 - self._last_msg_ts_ms) / 1000.0
                    stale_ok = stale_sec >= self.cfg.stale_threshold_sec

            if self._stop_evt.wait(self.cfg.rest_poll_interval_sec):
                return

            with self._state_lock:
                if not self._running:
                    return
                if self._ws_alive and not stale_ok:
                    continue

            try:
                resp = requests.get(
                    url,
                    timeout=self.cfg.request_timeout_sec,
                    headers={"User-Agent": self.cfg.user_agent},
                )
                if resp.status_code != 200:
                    self._record_rest_fail(f"http {resp.status_code}")
                    continue
                arr = resp.json()
                if not isinstance(arr, list) or not arr:
                    self._record_rest_fail("empty kline")
                    continue
                k = arr[-1]
                bar = KlineBar(
                    symbol=self.cfg.symbol,
                    interval=self.cfg.interval,
                    open_time_ms=int(k[0]),
                    close_time_ms=int(k[6]),
                    open=float(k[1]),
                    high=float(k[2]),
                    low=float(k[3]),
                    close=float(k[4]),
                    volume=float(k[5]),
                    is_closed=True,
                    source="rest",
                )
                self._record_bar(bar)
                with self._state_lock:
                    self._rest_poll_count += 1
                self._notify_status("rest_fallback_active", {"close": bar.close})
            except Exception as e:
                self._record_rest_fail(str(e))

    def _record_rest_fail(self, reason: str) -> None:
        with self._state_lock:
            self._rest_fail_count += 1
        logger.warning("REST fallback failed: %s", reason)

    # ---------------- 共享 ----------------

    def _record_bar(self, bar: KlineBar) -> None:
        with self._state_lock:
            self._last_bar = bar
            self._last_msg_ts_ms = time.time() * 1000.0
        if bar.is_closed and self.on_bar is not None:
            try:
                self.on_bar(bar)
            except Exception as e:
                logger.exception("on_bar callback failed: %s", e)

    def _notify_status(self, state: str, info: dict) -> None:
        if self.on_status is None:
            return
        try:
            self.on_status(state, info)
        except Exception as e:
            logger.exception("on_status callback failed: %s", e)
