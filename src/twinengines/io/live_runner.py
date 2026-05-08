"""
实盘主循环 (live_runner): 把所有 io 子模块拼成一个完整运行体。

模块化职责:
    1. ClockSync         同步本地时钟 → 给所有时间相关判断用
    2. BinanceFeed       BTC 实时 1s K 线 → 给信号层用
    3. MarketResolver    Polymarket 5min BTC condition_id → 给下单用
    4. PolymarketClient  下单 / 行情 / 余额查询
    5. PositionLock      短期门控 (防并发撞单)
    6. Reconciler        持仓对账 + 异常熔断
    7. Alerting          关键事件外推
    8. StateStore        状态持久化 (重启恢复)
    9. SingleInstanceLock 防双开
    10. Health server    /health, /metrics

运行时职责 (本文件实现):
    - 启动时: 鉴权 → allowance 检查 → 拉一次 equity → 启动后台线程 → 初始化锁
    - 主循环 (策略决策, 此处不重复策略代码): 由信号层下游回调, 每次决策走完整流程
    - 优雅退出: 接收 SIGTERM/SIGINT, 释放锁, 落盘状态, 关闭线程
    - dry_run: 强制走 PolymarketClient 影子单, 不发链上交易

使用示例 (CLI 入口):
    runner = LiveRunner.from_env(env_file=".env")
    runner.start()  # 阻塞运行直到 Ctrl+C
"""

from __future__ import annotations

import json
import math
import os
import hashlib
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from .alerting import AlertingDispatcher
from .binance_feed import BinanceFeed, BinanceFeedCfg, KlineBar
from .clock_sync import ClockSync, ClockSyncCfg
from .config import (
    POLYMARKET_PLATFORM,
    PolymarketRuntimeCfg,
    is_real_order_allowed,
    load_polymarket_runtime_cfg,
)
from .order_compliance import compute_compliant_limit_buy
from .health import set_halted, set_metrics_provider, start_health_server
from .logging_setup import get_logger, setup_logging
from .market_resolver import ActiveMarket, MarketResolver, MarketResolverCfg
from .polymarket_client import OrderState, OrderTicket, PolymarketClient
from .polymarket_feed import PolymarketFeed, PolymarketFeedCfg
from .position_lock import PositionLock
from .position_exit_guard import PositionExitGuard
from .reconciliation import LocalPosition, Reconciler, ReconcilerCfg
from ..risk.sizing import SizingCfg, stake_for_trade
from .shadow_signal_enrich import enrich_shadow_event_with_polymarket
from .shadow_signal_engine import ShadowSignalEngine
from .single_instance import SingleInstanceLock
from .state_store import KEY_LAST_EQUITY, StateStore, load_regime

logger = get_logger(__name__)

# 模块级状态 (跨回调保持)
_SIM_FILLED: set[str] = set()        # 影子盘方向级锁仓: window_id:direction
_REAL_WINDOW_ORDERS: dict[str, dict[str, Any]] = {}  # 实盘窗口+方向级 FOK 状态机
_SIM_CURRENT: dict = {}
_SIM_WIN_BUDGET: dict[str, float] = {}  # 影子盘每窗口+方向剩余 Kelly 预算
_SIM_WIN_TARGET: dict[str, float] = {}  # 影子盘每窗口+方向当前目标仓位
_SIM_WIN_DIR: dict[str, str] = {}       # 兼容旧审计字段
_SIM_WIN_ENTRY_PROB: dict[str, float] = {}
_SIM_WIN_LAST_PROB: dict[str, float] = {}
_SIM_WIN_ENTRY_EV: dict[str, float] = {}
_REAL_RETRYABLE_STATES = {OrderState.REJECTED, OrderState.CANCELLED, OrderState.TIMEOUT}
_REAL_UNKNOWN_ERRORS = ("unknown", "timeout", "query_failed", "post_order_exception")
      # 真实盘每窗口首次成交方向

@dataclass
class LiveRunnerCfg:
    runtime: PolymarketRuntimeCfg
    env_file: Optional[str] = None
    # 空跑信号模式 (--dry-run-signals): 仅记录, 不下单
    dry_run_signals: bool = False
    # 实盘并存 (--record-shadow-signals): 仍启动 ShadowSignalEngine 写 JSONL/SQLite（下单仍由 .env 双保险决定）
    record_shadow_signals: bool = False
    artifact_path: Optional[str] = None
    shadow_signal_log_path: str = "logs/shadow_signals.jsonl"

@dataclass
class LiveRunner:
    cfg: LiveRunnerCfg

    # 子模块
    clock: Optional[ClockSync] = None
    feed: Optional[BinanceFeed] = None
    poly_feed: Optional[PolymarketFeed] = None
    market_resolver: Optional[MarketResolver] = None
    poly_client: Optional[PolymarketClient] = None
    position_lock: Optional[PositionLock] = None
    exit_guard: Optional[PositionExitGuard] = None
    reconciler: Optional[Reconciler] = None
    alerting: Optional[AlertingDispatcher] = None
    store: Optional[StateStore] = None
    instance_lock: Optional[SingleInstanceLock] = None
    shadow_engine: Optional[ShadowSignalEngine] = None
    _sim_equity: float = 20.0  # 影子模拟资金 (随盈亏动态变化)
    _win_fills: dict[str, dict] = field(default_factory=dict)  # 每窗口仓位跟踪: {wid: {dir, filled, target, equity}}

    # 运行时
    _started: bool = False
    _stopping: threading.Event = field(default_factory=threading.Event)
    _last_bar: Optional[KlineBar] = None
    _bar_lock: threading.RLock = field(default_factory=threading.RLock)
    _auto_redeem_thread: Optional[threading.Thread] = None
    _redeem_lock: threading.RLock = field(default_factory=threading.RLock)
    _redeem_queue: dict[str, dict] = field(default_factory=dict)
    _last_active_market: Optional[ActiveMarket] = None
    _redeem_status: dict = field(default_factory=lambda: {
        "pending_n": 0,
        "recent_tx_hashes": [],
        "failure_reasons": {},
        "last_attempt_ts_ms": None,
        "last_success_ts_ms": None,
    })
    _cleanup_done: bool = False
    _started_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    _startup_observe_only_windows: set[str] = field(default_factory=set)
    _real_equity_cache_usdc: Optional[float] = None
    _real_equity_cache_ts_ms: int = 0
    _platform_min_boost_used: set[str] = field(default_factory=set)
    _prob_history: dict[str, list[tuple[int, float, float]]] = field(default_factory=dict)  # window_id -> [(ts_ms, p_up, p_down)]

    # ---------------- 工厂 ----------------

    @classmethod
    def from_env(
        cls,
        *,
        env_file: Optional[str] = None,
        dry_run_signals: bool = False,
        record_shadow_signals: bool = False,
        artifact_path: Optional[str] = None,
        shadow_signal_log_path: str = "logs/shadow_signals.jsonl",
    ) -> "LiveRunner":
        runtime = load_polymarket_runtime_cfg(env_file=env_file)
        cfg = LiveRunnerCfg(
            runtime=runtime,
            env_file=env_file,
            dry_run_signals=dry_run_signals,
            record_shadow_signals=bool(record_shadow_signals),
            artifact_path=artifact_path,
            shadow_signal_log_path=shadow_signal_log_path,
        )
        return cls(cfg=cfg)

    # ---------------- 启动 ----------------

    def start(self, *, run_forever: bool = True) -> bool:
        if self._started:
            logger.warning("LiveRunner already started")
            return True

        self._started_at_ms = int(time.time() * 1000)
        self._startup_observe_only_windows.clear()
        runtime = self.cfg.runtime

        # logging
        setup_logging(log_dir=runtime.log_dir, level=runtime.log_level)
        logger.info("===== LiveRunner starting =====")
        logger.info(
            "Mode: dry_run=%s enable_real=%s symbol=BTCUSDT horizon=%dmin",
            runtime.dry_run, runtime.enable_real_orders, runtime.market_horizon_minutes,
        )

        # single instance
        self.instance_lock = SingleInstanceLock(
            path=runtime.instance_lock_path,
            min_restart_interval_sec=int(runtime.single_instance_min_restart_sec),
        )
        if not self.instance_lock.acquire():
            logger.error("LiveRunner: another instance is running; abort start")
            return False

        def _abort_start_cleanup() -> bool:
            """单实例锁之后启动失败须释放锁并停时钟，否则 min_restart_interval 会阻断自动拉起。"""
            try:
                if self.clock is not None:
                    self.clock.stop()
            except Exception as e:
                logger.warning("abort_start_cleanup clock.stop failed: %s", e)
            try:
                self.instance_lock.release()
            except Exception as e:
                logger.warning("abort_start_cleanup lock.release failed: %s", e)
            return False

        # state store
        self.store = StateStore(db_path=runtime.state_db_path)

        # alerting
        self.alerting = AlertingDispatcher.from_env()

        # health
        if runtime.health_check_port:
            try:
                start_health_server(runtime.health_check_port)
                set_metrics_provider(self._metrics_provider)
            except Exception as e:
                logger.warning("health server start failed: %s", e)

        # clock sync
        self.clock = ClockSync(
            cfg=ClockSyncCfg(),
            on_drift_alert=lambda offset: self.alerting.alert(
                "warn", "clock_drift", {"offset_ms": offset},
            ),
        )
        self.clock.start()

        # poly client
        self.poly_client = PolymarketClient(runtime_cfg=runtime, platform=POLYMARKET_PLATFORM)
        ok, reason = is_real_order_allowed(runtime)
        if ok:
            if not bool(runtime.auto_redeem_enabled):
                logger.info("AUTO_REDEEM_ENABLED=false; skipping automatic redeem because Polymarket now provides official redemption flow.")
                if self.alerting is not None:
                    self.alerting.alert("info", "auto_redeem_disabled", {
                        "reason": "AUTO_REDEEM_ENABLED=false",
                    })
            inited, init_reason = self.poly_client.init_real_client()
            if not inited:
                logger.error("Real client init failed: %s", init_reason)
                self.alerting.alert("error", "real_client_init_failed", {"reason": init_reason})
                self._abort("real_client_init_failed")
                return _abort_start_cleanup()

            allow_ok, allow_reason, allowance = self.poly_client.precheck_allowance()
            if not allow_ok:
                logger.error("USDC allowance precheck failed: %s", allow_reason)
                self.alerting.alert("error", "allowance_precheck_failed",
                                    {"reason": allow_reason, "allowance": allowance})
                self._abort("allowance_precheck_failed")
                return _abort_start_cleanup()
            else:
                logger.info("Allowance precheck passed: %s", allow_reason)

            redeem_ok, redeem_reason = self.poly_client.redeem_capability_check()
            if not redeem_ok:
                logger.error("Auto redeem capability check failed: %s", redeem_reason)
                self.alerting.alert("error", "auto_redeem_capability_failed", {
                    "reason": redeem_reason,
                    "signature_type": runtime.signature_type,
                })
                self._abort("auto_redeem_capability_failed")
                return _abort_start_cleanup()

            equity = self.poly_client.fetch_account_equity_usdc()
            logger.info("Account equity USDC at start: %s", equity)
            if equity is not None and self.store is not None:
                self.store.put("account.start_equity_usdc", equity)
                # Web「当前资金」读 kv account.last_equity_usdc；启动即写入避免等首轮 reconcile
                self.store.put(KEY_LAST_EQUITY, equity)
            self._reconcile_window_order_flags_from_exchange()
        else:
            logger.warning("Real orders disabled (%s); running in shadow/dry-run mode", reason)

        # position lock
        self.position_lock = PositionLock(store=self.store)
        self.exit_guard = PositionExitGuard(client=self.poly_client, store=self.store) if self.poly_client is not None else None

        # market resolver
        self.market_resolver = MarketResolver(
            cfg=MarketResolverCfg(
                url=runtime.market_resolver_url_template,
                keywords=tuple(runtime.market_search_keywords),
                horizon_minutes=runtime.market_horizon_minutes,
                refresh_interval_sec=runtime.market_refresh_interval_sec,
                request_timeout_sec=runtime.http_timeout_sec,
                user_agent=runtime.http_user_agent,
            ),
            on_change=self._on_market_change,
            on_status=lambda state, info: self.alerting.alert(
                "info" if state in ("changed",) else "warn", f"market_{state}", info,
            ) if self.alerting else None,
        )
        self.market_resolver.start()

        # binance feed
        self.feed = BinanceFeed(
            cfg=BinanceFeedCfg(
                symbol="BTCUSDT",
                interval="1s",
                user_agent=runtime.http_user_agent,
                request_timeout_sec=runtime.http_timeout_sec,
            ),
            on_bar=self._on_bar,
            on_status=lambda state, info: self.alerting.alert(
                "warn" if "error" in state or "close" in state else "info",
                f"feed_{state}",
                info,
            ) if self.alerting else None,
        )
        self.feed.start()

        # polymarket feed (WS 主路, REST 由 PolymarketClient 负责)
        self.poly_feed = PolymarketFeed(
            cfg=PolymarketFeedCfg(user_agent=runtime.http_user_agent),
            now_ms_provider=(self.clock.now_ms if self.clock is not None else None),
            on_status=lambda state, info: self.alerting.alert(
                "warn" if "error" in state or "close" in state or "crashed" in state else "info",
                f"poly_feed_{state}",
                info,
            ) if self.alerting else None,
        )
        active_now = self.market_resolver.get_active() if self.market_resolver else None
        if active_now is not None:
            self.poly_feed.start([active_now.token_id_yes, active_now.token_id_no])
        else:
            self.poly_feed.start([])
        self.poly_client.attach_feed(self.poly_feed)

        # reconciler (--dry-run-signals: 观测模式, 不因余额/持仓查询失败触发熔断; 拉长间隔降噪)
        _rec_interval = float(runtime.reconcile_interval_sec)
        if self.cfg.dry_run_signals:
            _rec_interval = max(_rec_interval, 120.0)
        self.reconciler = Reconciler(
            client=self.poly_client,
            market_resolver=self.market_resolver,
            position_lock=self.position_lock,
            cfg=ReconcilerCfg(
                interval_sec=_rec_interval,
                observe_only=True,  # 实盘不因对账差异熔断 (避免误杀)
                position_size_drift_usdc=float(runtime.reconcile_untracked_drift_usdc),
            ),
            store=self.store,
            on_alert=lambda kind, payload: self.alerting.alert("warn", f"reconcile_{kind}", payload)
                if self.alerting else None,
            on_circuit_trip=self._on_reconcile_circuit_trip,
        )
        self.reconciler.start()

        # 影子信号记录: 纯空跑 (--dry-run-signals) 或 实盘并存 (--record-shadow-signals)
        self._init_shadow_signal_engine()  # 影子引擎始终启动 (真实盘也需要信号)
        if ok and not self.cfg.dry_run_signals:
            self._start_auto_redeem_worker()
            # 启动时扫描历史已结算市场, 入队赎回
            self._scan_historical_positions_for_redeem()

        # signal handlers
        self._install_signal_handlers()
        self._start_order_watch_worker()

        self._started = True
        LiveRunner._last_instance = self  # WebUI 余额查询
        logger.info("===== LiveRunner started =====")

        if run_forever:
            self._wait_loop()
        return True

    def _start_order_watch_worker(self) -> None:
        """Compatibility hook for legacy background order watcher.

        The current FOK path refreshes order truth synchronously per ticket and
        reconciler remains responsible for account/position observation.
        """
        return

    # ---------------- 主等待循环 ----------------

    def _wait_loop(self) -> None:
        logger.info("LiveRunner main loop entered (sleep-based, decisions are event-driven via on_bar)")
        try:
            while not self._stopping.is_set():
                time.sleep(1.0)
                if self.exit_guard is not None and self.cfg.runtime.enable_real_orders:
                    self.exit_guard.check_once()
                if self.poly_client and self.cfg.runtime.enable_real_orders:
                    self._write_real_balance()
        finally:
            self.stop()

    _balance_last_write: float = 0

    def _write_real_balance(self) -> None:
        """每秒写一次 Polymarket 余额到文件 (供 WebUI 读取)."""
        now = time.time()
        if now - self._balance_last_write < 5.0:
            return
        self._balance_last_write = now
        try:
            import json, os
            bal = self.poly_client.fetch_account_equity_usdc() or 0
            data = {"ok": True, "balance_usdc": round(bal, 2),
                    "pending_redeem": 0, "redeem_ok": self.poly_client.is_healthy()}
            os.makedirs(self._runtime_path("data_runtime"), exist_ok=True)
            open(self._runtime_path("data_runtime", "real_balance.json"), "w", encoding="utf-8").write(
                json.dumps(data))
        except Exception:
            pass

    def stop(self) -> None:
        self._stopping.set()
        if self._cleanup_done:
            return
        self._cleanup_done = True
        logger.info("===== LiveRunner stopping =====")

        for name, comp in [
            ("reconciler", self.reconciler),
            ("feed", self.feed),
            ("poly_feed", self.poly_feed),
            ("market_resolver", self.market_resolver),
            ("clock", self.clock),
        ]:
            if comp is not None:
                try:
                    comp.stop()
                except Exception as e:
                    logger.warning("stop %s failed: %s", name, e)
        if self._auto_redeem_thread is not None:
            self._auto_redeem_thread.join(timeout=2.0)

        if self.instance_lock is not None:
            try:
                self.instance_lock.release()
            except Exception as e:
                logger.warning("instance_lock release failed: %s", e)

        logger.info("===== LiveRunner stopped =====")

    def _abort(self, reason: str) -> None:
        logger.error("LiveRunner abort: %s", reason)
        set_halted(reason)
        self._stopping.set()

    def _install_signal_handlers(self) -> None:
        def _handler(signum, frame):
            logger.info("Signal %s received, requesting stop", signum)
            self._stopping.set()
        try:
            signal.signal(signal.SIGINT, _handler)
            signal.signal(signal.SIGTERM, _handler)
        except Exception as e:
            logger.warning("signal handler install failed: %s", e)

    # ---------------- 空跑信号引擎 ----------------

    def _init_shadow_signal_engine(self) -> None:
        # 启动时从文件恢复权益（避免重启重置为 $20）
        try:
            eq_path = self._runtime_path("data_runtime", "sim_equity.txt")
            if os.path.exists(eq_path):
                saved = float(open(eq_path, encoding="utf-8").read().strip())
                if saved > 0:
                    self._sim_equity = saved
                    logger.info("_sim_equity restored from file: %.2f", self._sim_equity)
        except Exception:
            pass

        artifact = self.cfg.artifact_path
        if not artifact:
            logger.error(
                "shadow signal recording requested but artifact_path is empty; shadow signal engine disabled"
            )
            if self.alerting is not None:
                self.alerting.alert("error", "shadow_engine_no_artifact",
                                    {"reason": "artifact_path missing"})
            return
        try:
            engine = ShadowSignalEngine.from_artifact(
                artifact_path=artifact,
                jsonl_path=self.cfg.shadow_signal_log_path,
            )
        except Exception as e:
            logger.exception("ShadowSignalEngine init failed: %s", e)
            if self.alerting is not None:
                self.alerting.alert("error", "shadow_engine_init_failed", {"error": str(e)})
            return

        if self.store is not None:
            engine.audit_writer = self.store.append_audit
        engine.enrich_shadow_signal_event = self._enrich_shadow_signal_book
        engine.on_signal_event = self._on_shadow_signal_event
        engine.on_window_close = self._on_shadow_window_close
        self.shadow_engine = engine
        self._record_runtime_effective_snapshot(engine=engine, artifact_path=artifact)
        if self.cfg.dry_run_signals:
            logger.info(
                "===== Shadow signal engine ON (artifact=%s, jsonl=%s) — dry-run-signals, NO orders =====",
                artifact, self.cfg.shadow_signal_log_path,
            )
        else:
            logger.info(
                "===== Shadow signal engine ON (artifact=%s, jsonl=%s) — recording alongside live (.env 决定真实下单) =====",
                artifact, self.cfg.shadow_signal_log_path,
            )
        if self.alerting is not None:
            self.alerting.alert("info", "shadow_engine_started",
                                {"artifact": artifact, "jsonl": self.cfg.shadow_signal_log_path})

    def _record_runtime_effective_snapshot(self, *, engine: ShadowSignalEngine, artifact_path: str) -> None:
        snapshot = {
            "ts_ms": int(time.time() * 1000),
            "artifact_path": artifact_path,
            "artifact_sha256": self._safe_sha256(artifact_path),
            "model_version": getattr(engine.direction_model, "model_version", "direction_probability"),
            "semantic": "final_direction_probability",
        }
        logger.info("Runtime effective snapshot: %s", snapshot)
        if self.store is not None:
            self.store.put("runtime.effective_snapshot", snapshot)
            self.store.append_audit("runtime_effective_snapshot", snapshot)

    @staticmethod
    def _safe_sha256(path: str) -> Optional[str]:
        try:
            with open(path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except Exception:
            return None

    def _on_shadow_window_close(self, window_id: str) -> None:
        """窗口关闭时结算影子盘和真实盘。"""
        global _SIM_FILLED, _SIM_CURRENT, _SIM_WIN_BUDGET, _SIM_WIN_DIR, _SIM_WIN_TARGET, _SIM_WIN_ENTRY_PROB, _SIM_WIN_LAST_PROB, _SIM_WIN_ENTRY_EV
        for key in list(_SIM_FILLED):
            if key == window_id or key.startswith(f"{window_id}:"):
                _SIM_FILLED.discard(key)
        for bucket in (_REAL_WINDOW_ORDERS, _SIM_WIN_BUDGET, _SIM_WIN_TARGET, _SIM_WIN_ENTRY_PROB, _SIM_WIN_LAST_PROB, _SIM_WIN_ENTRY_EV):
            for key in list(bucket.keys()):
                if key == window_id or str(key).startswith(f"{window_id}:"):
                    bucket.pop(key, None)
        _SIM_WIN_DIR.pop(window_id, None)
        _SIM_CURRENT.clear()
        
        try:
            import json as _j, urllib.request as _u, os as _o, sqlite3 as _sql
            ws = int(window_id.replace("w", ""))
            url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&startTime=" + str(ws) + "&limit=5"
            data = _j.loads(_u.urlopen(_u.Request(url, headers={"User-Agent": "TE/1.0"}), timeout=8).read())
            if len(data) < 5:
                return
            bl = float(data[0][1])
            seq = "".join("1" if float(k[4]) > bl else "0" for k in data)
            actual_dir = "up" if seq[4] == "1" else "down"

            self._settle_shadow(window_id, seq, actual_dir, ws, _j, _o)
            self._settle_real(window_id, seq, actual_dir, ws, _j, _o, _sql)
        except Exception:
            pass

    def _settle_shadow(self, window_id, seq, actual_dir, ws, _j, _o):
        fills = []
        p = self._runtime_path("logs", "shadow_orders.jsonl")
        if not _o.path.exists(p):
            return
        for line in open(p).readlines()[-500:]:
            if window_id not in line:
                continue
            try:
                o = _j.loads(line.strip())
                if str(o.get("status") or "").lower() == "filled" and o.get("fill_amount", 0) > 0:
                    fa = float(o.get("fill_amount") or 0)
                    bd = o.get("best_dir", "")
                    fs = int(300 - float(o.get("T_remaining") or 0))
                    fask = float(o.get("ask_up") if bd == "up" else o.get("ask_down") or 0)
                    fills.append((fa, fask, bd, fs))
            except Exception:
                pass
        if not fills:
            return
        total_fill = sum(f[0] for f in fills)
        best_dir = fills[0][2]
        first_sec = fills[0][3]
        total_pnl = 0.0
        won = False
        for fa, fask, bd, _ in fills:
            won_leg = actual_dir == bd
            won = won or won_leg
            if won_leg and fask > 0:
                total_pnl += fa * (1.0 / fask - 1.0)
            elif won_leg:
                total_pnl += fa * 0.50
            else:
                total_pnl += -fa
        avg_ask = sum(f[0] * f[1] for f in fills) / total_fill if total_fill > 0 else 0
        last_eq = self._sim_equity
        try:
            p2 = self._runtime_path("logs", "window_results.jsonl")
            if _o.path.exists(p2):
                for tail_line in reversed(open(p2, "r").readlines()[-20:]):
                    tl = _j.loads(tail_line.strip())
                    if tl.get("mode") == "shadow":
                        last_eq = float(tl.get("equity") or last_eq)
                        break
        except Exception:
            pass
        self._sim_equity = last_eq + total_pnl
        res = _j.dumps({
            "window_id": window_id, "seq": seq, "dir": best_dir,
            "won": won, "pnl": round(total_pnl, 2),
            "equity": round(self._sim_equity, 2),
            "ask": round(avg_ask, 4), "fill_amt": round(total_fill, 2),
            "fill_sec": first_sec, "partials": len(fills), "mode": "shadow",
        })
        self._append_jsonl(self._runtime_path("logs", "window_results.jsonl"), res)
        try:
            _o.makedirs(self._runtime_path("data_runtime"), exist_ok=True)
            open(self._runtime_path("data_runtime", "sim_equity.txt"), "w").write(str(round(self._sim_equity, 2)) + chr(10))
        except Exception:
            pass

    def _settle_real(self, window_id, seq, actual_dir, ws, _j, _o, _sql):
        if not self.cfg.runtime.enable_real_orders:
            return
        db_path = self._runtime_path("data_runtime", "state.sqlite")
        if not _o.path.exists(db_path):
            return
        fills = []
        try:
            con = _sql.connect(db_path, timeout=2.0)
            cur = con.execute(
                "SELECT ts_ms, payload FROM audit_events WHERE kind='order_filled' AND payload LIKE ? ORDER BY id",
                (f'%{window_id}%',)
            )
            for ts_ms, payload in cur.fetchall():
                try:
                    o = _j.loads(payload)
                    if o.get("window_id") == window_id:
                        fa = float(o.get("size_usdc") or 0)
                        fask = float(o.get("price") or 0)
                        bd = o.get("direction", "")
                        fs = int((ts_ms - ws) / 1000)
                        fills.append((fa, fask, bd, fs))
                except Exception:
                    pass
            con.close()
        except Exception:
            return
        if not fills:
            return
        total_fill = sum(f[0] for f in fills)
        best_dir = fills[0][2]
        first_sec = fills[0][3]
        total_pnl = 0.0
        won = False
        for fa, fask, bd, _ in fills:
            won_leg = actual_dir == bd
            won = won or won_leg
            if won_leg and fask > 0:
                total_pnl += fa * (1.0 / fask - 1.0)
            elif won_leg:
                total_pnl += fa * 0.50
            else:
                total_pnl += -fa
        avg_ask = sum(f[0] * f[1] for f in fills) / total_fill if total_fill > 0 else 0
        real_equity = 0.0
        try:
            bal_file = self._runtime_path("data_runtime", "real_balance.json")
            if _o.path.exists(bal_file):
                bal = _j.loads(open(bal_file).read())
                real_equity = float(bal.get("balance_usdc") or 0)
        except Exception:
            pass
        res = _j.dumps({
            "window_id": window_id, "seq": seq, "dir": best_dir,
            "won": won, "pnl": round(total_pnl, 2),
            "equity": round(real_equity, 2),
            "ask": round(avg_ask, 4), "fill_amt": round(total_fill, 2),
            "fill_sec": first_sec, "partials": len(fills), "mode": "real",
        })
        self._append_jsonl(self._runtime_path("logs", "real_results.jsonl"), res)

    @staticmethod
    def _append_jsonl(path: str, line: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "ab+") as f:
            f.seek(0, 2)
            if f.tell() > 0:
                f.seek(f.tell() - 1)
                if f.read(1) != b"\n":
                    f.write(b"\n")
            f.write((line + "\n").encode("utf-8"))

    def _on_shadow_signal_event(self, event: dict) -> None:
        global _SIM_FILLED, _SIM_CURRENT
        try:
            self._append_shadow_signal_jsonl(event)
        except Exception as e:
            logger.warning("shadow signal jsonl append failed: %s", e)
        # 影子盘始终运行; 真实盘由 _simulate_order_from_signal 内部触发
        try:
            self._simulate_order_from_signal(event)
        except Exception as e:
            logger.warning("simulate order failed: %s", e)

    def _write_current_window_snapshot(self) -> None:
        try:
            import json as _j, os as _o
            _o.makedirs(self._runtime_path("data_runtime"), exist_ok=True)
            with open(self._runtime_path("data_runtime", "current_window.json"), "w") as _cw:
                _cw.write(_j.dumps(_SIM_CURRENT, default=str))
        except Exception:
            pass

    def _append_shadow_signal_jsonl(self, event: dict) -> None:
        path = self.cfg.shadow_signal_log_path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, default=str)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _simulate_order_from_signal(self, event: dict) -> None:
        global _SIM_FILLED, _SIM_CURRENT
        global _SIM_WIN_BUDGET, _SIM_WIN_DIR, _SIM_WIN_TARGET, _SIM_WIN_ENTRY_PROB, _SIM_WIN_LAST_PROB, _SIM_WIN_ENTRY_EV
        window_id = str(event.get("window_id") or "")
        # 真实盘是否活跃
        is_real_mode = (not self.cfg.dry_run_signals
                        and self.poly_client and self.market_resolver
                        and self.cfg.runtime.enable_real_orders)
        filled_set = _SIM_FILLED   # 影子盘锁仓
        win_budget = _SIM_WIN_BUDGET  # 影子盘预算
        win_target = _SIM_WIN_TARGET  # 影子盘目标仓位
        win_dir = _SIM_WIN_DIR     # 影子盘方向
        p_up = float(event.get("p_up") or 0.5)
        p_down = float(event.get("p_down") or (1.0 - p_up))
        p_up = max(0.0, min(1.0, p_up))
        p_down = max(0.0, min(1.0, p_down))
        norm = p_up + p_down
        if norm > 0:
            p_up, p_down = p_up / norm, p_down / norm
        t_rem = float(event.get("t_remaining_sec") or 0)
        trig = event.get("trigger_pattern", "")
        phase_raw = event.get("phase", 3)
        phase = 3 if phase_raw is None else int(phase_raw)
        elapsed_sec = max(0.0, 300.0 - t_rem)
        p_adj = max(p_up, p_down)
        d_abs = float(event.get("d_abs_pct") or 0)
        d_signed = float(event.get("d_signed_pct") or 0)
        best_prob_dir = "up" if p_up >= p_down else "down"
        self._record_probability_history(window_id, p_up=p_up, p_down=p_down)
        if self.exit_guard is not None and is_real_mode and window_id:
            try:
                self.exit_guard.observe_probability(window_id=window_id, p_up=p_up, p_down=p_down)
            except Exception as e:
                logger.debug("exit_guard observe_probability failed window_id=%s err=%s", window_id, e)

        min_side_prob, phase_confirm_sec_for_display, min_ev = self._phase_gate_rule(phase)
        min_edge = -1.0
        min_kelly_raw = 0.0
        min_entry_t_rem = 15.0
        key = trig[:3] if len(trig) >= 3 else ""
        startup_observe_only = False
        startup_missed_sec = 0.0
        if is_real_mode and window_id.startswith("w"):
            try:
                window_start_ms = int(window_id[1:])
                startup_missed_sec = max(0.0, (int(self._started_at_ms or 0) - window_start_ms) / 1000.0)
                if startup_missed_sec > float(getattr(self.cfg.runtime, "startup_observe_only_max_missed_sec", 8.0)):
                    startup_observe_only = True
                    self._startup_observe_only_windows.add(window_id)
            except Exception:
                startup_observe_only = False

        stale_decision_keys = (
            "best_dir", "best_side_prob", "best_edge", "best_ev", "best_ev_simple", "best_kelly_raw",
            "trade_intent", "intent_allowed", "intent_reason", "lifecycle_phase_policy",
            "req_prob", "req_edge", "req_ev", "req_kelly_raw",
            "is_hedge", "is_add", "is_value_entry", "is_trend_entry",
            "value_max_ask", "value_max_ask_exclusive", "trend_max_ask", "trend_max_ask_exclusive",
            "add_max_ask_exclusive", "high_price_min_ask", "p_rising_required_sec", "p_rising_5s", "p_rising_8s",
            "p_confirm_required_sec", "p_confirm_ok", "p_confirm_threshold",
            "friction_adjusted_ev", "friction_multiplier",
            "real_status", "real_decision_stage", "real_block_reason", "real_skip_reason",
            "real_target_quote", "real_attempt_quote", "real_kelly_raw_quote", "real_platform_min_quote",
            "real_min_5_shares_quote", "real_min_order_quote", "real_limit_px", "real_window_cap",
            "real_min_abs_boost_available", "real_min_abs_boost_cap", "real_min_abs_boost_used", "real_min_funding_boost_key",
            "real_min_funding_boost_used_this_decision", "real_platform_min_boost_available",
            "real_platform_min_boost_used", "real_pre_window_cap", "real_ratio_limited_cap",
            "real_min_share_exception", "real_min_share_exception_reason",
            "status", "fill_amt", "fill_ask", "fill_ev", "fill_edge", "fill_kelly_raw",
        )
        for _stale_key in stale_decision_keys:
            _SIM_CURRENT.pop(_stale_key, None)

        # 基础状态更新
        _SIM_CURRENT.update({"window_id": window_id, "prefix": trig, "T": round(t_rem, 0),
                             "p_up": round(p_up, 3), "p_down": round(p_down, 3),
                             "best_prob_dir": best_prob_dir,
                             "d_signed": round(d_signed, 4), "d_abs": round(d_abs, 4),
                             "phase": phase, "elapsed_sec": round(elapsed_sec, 0),
                             "startup_observe_only": bool(startup_observe_only),
                             "startup_missed_sec": round(startup_missed_sec, 1),
                             "decision_pending": False,
                             "min_side_prob": min_side_prob, "min_edge": min_edge,
                             "min_ev": min_ev, "min_kelly_raw": min_kelly_raw})

        if not window_id or t_rem < min_entry_t_rem:
            _SIM_CURRENT["status"] = f"T<{min_entry_t_rem:.0f}s"; self._write_current_window_snapshot(); return

        # 预填两个方向的 EV
        try:
            _, _, ask_up, ask_down = self._resolve_best_direction_by_ev(event, window_id, phase=phase)
        except:
            ask_up, ask_down = None, None
        _SIM_CURRENT["ask_up"] = ask_up; _SIM_CURRENT["ask_down"] = ask_down
        if ask_up is not None and ask_down is not None:
            ev_up_simple = self._calc_ev(p_up, ask_up)
            ev_down_simple = self._calc_ev(p_down, ask_down)
            edge_up = p_up - ask_up
            edge_down = p_down - ask_down
            kelly_up_raw = self._raw_kelly_ratio(p_up, ask_up)
            kelly_down_raw = self._raw_kelly_ratio(p_down, ask_down)
            _SIM_CURRENT["ev_up"] = round(ev_up_simple, 4)
            _SIM_CURRENT["ev_down"] = round(ev_down_simple, 4)
            _SIM_CURRENT["edge_up"] = round(edge_up, 4)
            _SIM_CURRENT["edge_down"] = round(edge_down, 4)
            _SIM_CURRENT["kelly_up_raw"] = round(kelly_up_raw, 4)
            _SIM_CURRENT["kelly_down_raw"] = round(kelly_down_raw, 4)
            _SIM_CURRENT["up_prob_ok"] = (p_up > min_side_prob)
            _SIM_CURRENT["down_prob_ok"] = (p_down > min_side_prob)

        try:
            best_dir, best_ev, ask_up, ask_down = self._resolve_best_direction_by_ev(event, window_id, phase=phase)
        except:
            _SIM_CURRENT["status"] = "EV err"; self._write_current_window_snapshot(); return

        if best_dir is None:
            _SIM_CURRENT["status"] = "no_eligible_direction"
            _SIM_CURRENT["decision_pending"] = False
            _SIM_CURRENT["trade_intent"] = "NO_TRADE"
            _SIM_CURRENT["intent_allowed"] = False
            _SIM_CURRENT["intent_reason"] = "no_direction_passed_candidate_filter"
            _SIM_CURRENT["lifecycle_phase_policy"] = "candidate_filter"
            req_prob_none, req_confirm_none, req_ev_none = self._phase_gate_rule(phase)
            _SIM_CURRENT["req_prob"] = req_prob_none
            _SIM_CURRENT["req_ev"] = req_ev_none
            _SIM_CURRENT["trend_max_ask_exclusive"] = 0.80
            _SIM_CURRENT["friction_multiplier"] = 1.005
            _SIM_CURRENT["p_confirm_required_sec"] = req_confirm_none
            _SIM_CURRENT["p_confirm_ok"] = False
            _SIM_CURRENT["p_confirm_threshold"] = req_prob_none
            _SIM_CURRENT["candidate_filter_reason"] = f"phase{phase} needs p>{req_prob_none} to select a direction"
            self._write_current_window_snapshot()
            return

        signal_meta = {
            "phase": phase,
            "sec_in_window": int(elapsed_sec),
            "elapsed_sec": round(elapsed_sec, 2),
            "p_up": round(p_up, 6),
            "p_down": round(p_down, 6),
            "best_prob_dir": best_prob_dir,
            "startup_observe_only": bool(startup_observe_only),
            "startup_missed_sec": round(startup_missed_sec, 2),
        }
        def _record_decision(fill_amt, status, reason, extra=None):
            meta = dict(signal_meta)
            if extra:
                meta.update(extra)
            self._write_sim_record(window_id, trig, p_adj, best_side_prob, t_rem, ask_up, ask_down, best_dir, best_ev_simple, fill_amt, status, reason, d_abs, meta=meta)

        best_side_prob = p_up if best_dir == "up" else p_down
        ask = ask_up if best_dir == "up" else ask_down
        best_edge = best_side_prob - ask
        best_ev_simple = self._calc_ev(best_side_prob, ask)
        best_kelly_raw = self._raw_kelly_ratio(best_side_prob, ask)
        dir_key = f"{window_id}:{best_dir}"
        opposite_dir = "down" if best_dir == "up" else "up"
        opposite_key = f"{window_id}:{opposite_dir}"
        opposite_real = _REAL_WINDOW_ORDERS.get(opposite_key) or {}
        has_opposite_position = float(opposite_real.get("filled_shares", 0.0) or 0.0) > 1e-9
        same_real = _REAL_WINDOW_ORDERS.get(dir_key) or {}
        has_same_position = float(same_real.get("filled_shares", 0.0) or 0.0) > 1e-9
        shadow_has_opposite_position = opposite_key in win_target
        shadow_has_same_position = dir_key in win_target
        _SIM_CURRENT["has_same_position"] = bool(has_same_position)
        _SIM_CURRENT["has_opposite_position"] = bool(has_opposite_position)
        _SIM_CURRENT["shadow_has_same_target"] = bool(shadow_has_same_position)
        _SIM_CURRENT["shadow_has_opposite_target"] = bool(shadow_has_opposite_position)
        is_hedged_locked = bool(has_same_position and has_opposite_position)
        if is_hedged_locked:
            _SIM_CURRENT["best_dir"] = best_dir
            _SIM_CURRENT["best_side_prob"] = round(best_side_prob, 4)
            _SIM_CURRENT["best_edge"] = round(best_edge, 4)
            _SIM_CURRENT["best_ev"] = round(best_ev_simple, 4)
            _SIM_CURRENT["trade_intent"] = "NO_TRADE"
            _SIM_CURRENT["intent_allowed"] = False
            _SIM_CURRENT["intent_reason"] = "hedged_locked_no_more_add"
            _SIM_CURRENT["lifecycle_phase_policy"] = "hold_hedged_to_settlement"
            _SIM_CURRENT["status"] = "hedged_locked_no_more_add"
            _record_decision(0, "rejected", "hedged_locked_no_more_add", {
                "trade_intent": "NO_TRADE",
                "intent_allowed": False,
                "intent_reason": "hedged_locked_no_more_add",
                "lifecycle_phase_policy": "hold_hedged_to_settlement",
                "has_same_position": True,
                "has_opposite_position": True,
            })
            self._write_current_window_snapshot()
            return

        # 5-minute lifecycle strategy: classify intent first, then apply phase-aware policy.
        phase_req_prob, phase_confirm_sec, phase_req_net_ev = self._phase_gate_rule(phase)
        p_confirm_ok = self._is_probability_above(window_id, best_dir, seconds=phase_confirm_sec, threshold=phase_req_prob)
        p_stability_sec = 0
        p_stability_max_drop = 0.0
        p_stability_ok = True
        p_stability_drop = None
        if phase == 3:
            p_stability_sec = 5
            p_stability_max_drop = 0.04
        elif phase >= 4:
            p_stability_sec = 8
            p_stability_max_drop = 0.03
        if p_stability_sec > 0:
            p_stability_ok, p_stability_drop = self._probability_drawdown_ok(
                window_id,
                best_dir,
                seconds=p_stability_sec,
                max_drop=p_stability_max_drop,
            )
        low_price_high_ev_monotonic_required = bool(ask < 0.50 and best_ev_simple > 0.30)
        low_price_high_ev_monotonic_sec = 8 if phase >= 4 else 5
        low_price_high_ev_monotonic_ok = True
        if low_price_high_ev_monotonic_required:
            low_price_high_ev_monotonic_ok = self._is_probability_rising(
                window_id,
                best_dir,
                seconds=low_price_high_ev_monotonic_sec,
            )
        lifecycle = self._evaluate_lifecycle_intent_gate(
            phase=phase,
            p_side=best_side_prob,
            ask=ask,
            edge=best_edge,
            ev=best_ev_simple,
            kelly_raw=best_kelly_raw,
            has_same_position=has_same_position,
            has_opposite_position=has_opposite_position,
            p_confirm_ok=p_confirm_ok,
            p_confirm_sec=phase_confirm_sec,
            p_stability_ok=p_stability_ok,
            p_stability_sec=p_stability_sec,
            p_stability_max_drop=p_stability_max_drop,
            p_stability_drop=p_stability_drop,
            low_price_high_ev_monotonic_required=low_price_high_ev_monotonic_required,
            low_price_high_ev_monotonic_ok=low_price_high_ev_monotonic_ok,
            low_price_high_ev_monotonic_sec=low_price_high_ev_monotonic_sec if low_price_high_ev_monotonic_required else 0,
        )
        trade_intent = str(lifecycle["trade_intent"])
        req_edge = float(lifecycle["req_edge"])
        req_ev = float(lifecycle["req_ev"])
        req_kelly_raw = float(lifecycle["req_kelly_raw"])
        req_prob = float(lifecycle["req_prob"])

        _SIM_CURRENT["best_dir"] = best_dir
        _SIM_CURRENT["best_side_prob"] = round(best_side_prob, 4)
        _SIM_CURRENT["best_edge"] = round(best_edge, 4)
        _SIM_CURRENT["best_ev"] = round(best_ev_simple, 4)
        _SIM_CURRENT["best_ev_simple"] = round(best_ev_simple, 4)
        _SIM_CURRENT["best_kelly_raw"] = round(best_kelly_raw, 4)
        _SIM_CURRENT["req_prob"] = round(req_prob, 4)
        _SIM_CURRENT["req_edge"] = round(req_edge, 4)
        _SIM_CURRENT["req_ev"] = round(req_ev, 4)
        _SIM_CURRENT["req_kelly_raw"] = round(req_kelly_raw, 4)
        _SIM_CURRENT["trade_intent"] = trade_intent
        _SIM_CURRENT["intent_allowed"] = bool(lifecycle["allowed"])
        _SIM_CURRENT["intent_reason"] = lifecycle["reason"]
        _SIM_CURRENT["lifecycle_phase_policy"] = lifecycle["phase_policy"]
        _SIM_CURRENT["decision_pending"] = False
        for _stale_lifecycle_key in (
            "value_max_ask",
            "value_max_ask_exclusive",
            "trend_max_ask",
            "trend_max_ask_exclusive",
            "add_max_ask_exclusive",
            "high_price_min_ask",
            "p_rising_required_sec",
            "p_rising_5s",
            "p_rising_8s",
            "p_confirm_required_sec",
            "p_confirm_ok",
            "p_confirm_threshold",
            "p_stability_ok",
            "p_stability_required_sec",
            "p_stability_max_drop",
            "p_stability_drop",
            "low_price_high_ev_monotonic_required",
            "low_price_high_ev_monotonic_ok",
            "low_price_high_ev_monotonic_sec",
            "friction_adjusted_ev",
            "friction_multiplier",
        ):
            _SIM_CURRENT.pop(_stale_lifecycle_key, None)
        _SIM_CURRENT.update(dict(lifecycle.get("meta") or {}))
        _SIM_CURRENT["is_hedge"] = trade_intent == "HEDGE"
        _SIM_CURRENT["is_add"] = trade_intent == "ADD"
        _SIM_CURRENT["is_value_entry"] = trade_intent == "ENTRY_VALUE"
        _SIM_CURRENT["is_trend_entry"] = trade_intent == "ENTRY_TREND"
        signal_meta.update({
            "req_prob": round(req_prob, 4),
            "req_edge": round(req_edge, 4),
            "req_ev": round(req_ev, 4),
            "req_kelly_raw": round(req_kelly_raw, 4),
            "best_edge": round(best_edge, 4),
            "best_kelly_raw": round(best_kelly_raw, 4),
            "trade_intent": trade_intent,
            "intent_allowed": bool(lifecycle["allowed"]),
            "intent_reason": lifecycle["reason"],
            "lifecycle_phase_policy": lifecycle["phase_policy"],
            "is_hedge": trade_intent == "HEDGE",
            "is_add": trade_intent == "ADD",
            "is_value_entry": trade_intent == "ENTRY_VALUE",
            "is_trend_entry": trade_intent == "ENTRY_TREND",
            **dict(lifecycle.get("meta") or {}),
        })

        if not bool(lifecycle["allowed"]):
            reason = str(lifecycle["reason"])
            _SIM_CURRENT["status"] = reason
            _record_decision(0, "rejected", reason, dict(lifecycle.get("meta") or {}))
            self._write_current_window_snapshot()
            return

        sizing_fraction, max_stake_ratio, sizing_tier = self._direction_sizing_profile(
            p_side=best_side_prob,
            edge=best_edge,
            ev=best_ev_simple,
            kelly_raw=best_kelly_raw,
            phase=phase,
            has_position=dir_key in win_target,
        )
        sizing_fraction, max_stake_ratio, sizing_tier = self._apply_lifecycle_sizing_profile(
            sizing_fraction=sizing_fraction,
            max_stake_ratio=max_stake_ratio,
            sizing_tier=sizing_tier,
            trade_intent=trade_intent,
            phase=phase,
        )
        if trade_intent == "ENTRY_VALUE":
            _SIM_CURRENT["floor_price_entry"] = bool(ask < 0.10)
        if trade_intent == "ENTRY_VALUE" and ask < 0.10:
            sizing_tier = f"floor_lottery_{sizing_tier}"
        _SIM_CURRENT["sizing_fraction"] = round(sizing_fraction, 4)
        runtime_window_cap_abs = float(getattr(self.cfg.runtime, "real_max_window_risk_usdc", 0.0) or 0.0)
        runtime_window_cap_ratio = float(getattr(self.cfg.runtime, "real_max_window_risk_ratio", 0.30) or 0.0)
        effective_max_stake_ratio = min(max_stake_ratio, runtime_window_cap_ratio) if runtime_window_cap_ratio > 0 else max_stake_ratio
        _SIM_CURRENT["max_stake_ratio"] = round(max_stake_ratio, 4)
        _SIM_CURRENT["effective_max_stake_ratio"] = round(effective_max_stake_ratio, 4)
        _SIM_CURRENT["real_max_window_risk_usdc"] = round(runtime_window_cap_abs, 2) if runtime_window_cap_abs > 0 else None
        _SIM_CURRENT["sizing_tier"] = sizing_tier
        _SIM_CURRENT["floor_price_entry"] = bool(trade_intent == "ENTRY_VALUE" and ask < 0.10)
        signal_meta.update({
            "sizing_fraction": round(sizing_fraction, 4),
            "max_stake_ratio": round(max_stake_ratio, 4),
            "effective_max_stake_ratio": round(effective_max_stake_ratio, 4),
            "real_max_window_risk_usdc": round(runtime_window_cap_abs, 2) if runtime_window_cap_abs > 0 else None,
            "sizing_tier": sizing_tier,
            "floor_price_entry": bool(trade_intent == "ENTRY_VALUE" and ask < 0.10),
        })

        def _audit_real_decision(outcome: str, reason: str, extra: Optional[dict[str, Any]] = None) -> None:
            if self.store is None:
                return
            payload = {
                **signal_meta,
                "window_id": window_id,
                "direction": best_dir,
                "outcome": outcome,
                "reason": reason,
                "phase": phase,
                "elapsed_sec": round(elapsed_sec, 2),
                "trade_intent": trade_intent,
                "intent_reason": lifecycle["reason"],
                "lifecycle_phase_policy": lifecycle["phase_policy"],
                "best_side_prob": round(best_side_prob, 6),
                "ask": round(float(ask), 6) if ask is not None else None,
                "best_ev": round(best_ev_simple, 6),
                "best_kelly_raw": round(best_kelly_raw, 6),
                "real_status": _SIM_CURRENT.get("real_status"),
                "real_decision_stage": _SIM_CURRENT.get("real_decision_stage"),
                "real_block_reason": _SIM_CURRENT.get("real_block_reason"),
                "real_target_quote": _SIM_CURRENT.get("real_target_quote"),
                "real_attempt_quote": _SIM_CURRENT.get("real_attempt_quote"),
                "real_kelly_raw_quote": _SIM_CURRENT.get("real_kelly_raw_quote"),
                "real_platform_min_quote": _SIM_CURRENT.get("real_platform_min_quote"),
                "real_window_cap": _SIM_CURRENT.get("real_window_cap"),
            }
            if extra:
                payload.update(extra)
            self.store.append_audit("real_decision", payload)

        if self.exit_guard is not None and is_real_mode and window_id:
            try:
                self.exit_guard.observe_signal(
                    window_id=window_id,
                    best_dir=best_dir,
                    p_up=p_up,
                    p_down=p_down,
                    ts_ms=int(time.time() * 1000),
                )
            except Exception as e:
                logger.warning("exit_guard observe_signal failed window_id=%s err=%s", window_id, e)

        poly = event.get("polymarket") or {}
        ask_sz = float(poly.get("best_ask_size_up", 0)) if best_dir == "up" else float(poly.get("best_ask_size_dn", 0))
        slippage_budget = 0.005
        max_price = ask * (1.0 + slippage_budget) if ask > 0 else ask
        depth_cap = min(ask_sz * max_price * 0.8, 200.0) if ask_sz > 0 and ask > 0 else 50.0

        if not is_real_mode:
            _SIM_CURRENT["real_status"] = "real_mode_disabled"
            _SIM_CURRENT["real_decision_stage"] = "real_mode_disabled"
            _audit_real_decision("not_submitted", "real_mode_disabled")

        if is_real_mode:
            try:
                real_equity = self._fetch_real_equity_cached() if self.poly_client else None
                _SIM_CURRENT["real_decision_stage"] = "equity_checked"
                _SIM_CURRENT["real_equity_cached"] = self._real_equity_cache_usdc is not None
                _SIM_CURRENT["real_equity_age_ms"] = max(0, int(time.time() * 1000) - int(self._real_equity_cache_ts_ms or 0)) if self._real_equity_cache_usdc is not None else None
                if real_equity is None:
                    _SIM_CURRENT["real_status"] = "real_equity_unavailable"
                    _audit_real_decision("not_submitted", "real_equity_unavailable")
                    self._write_current_window_snapshot()
                    return
                else:
                    if startup_observe_only:
                        _SIM_CURRENT["real_status"] = "startup_observe_only"
                        _SIM_CURRENT["real_skip_reason"] = "startup_window_not_seen_from_start"
                        _SIM_CURRENT["real_decision_stage"] = "startup_observe_only_hard_skip"
                        if self.store is not None:
                            self.store.append_audit("order_compliance_skip", {
                                "window_id": window_id,
                                "direction": best_dir,
                                "reason": "startup_observe_only",
                                "startup_missed_sec": round(startup_missed_sec, 2),
                                **signal_meta,
                            })
                        _audit_real_decision("not_submitted", "startup_observe_only", {"startup_missed_sec": round(startup_missed_sec, 2)})
                        self._write_current_window_snapshot()
                        return
                    else:
                        real_sizing = SizingCfg(kelly_fraction=sizing_fraction, max_stake_ratio=effective_max_stake_ratio, min_absolute_stake=2.50)
                        real_wp = best_side_prob
                        real_b = (1 - ask) / ask if ask > 0 else 1
                        real_kelly_total = stake_for_trade(
                            portfolio_equity=float(real_equity),
                            win_prob=real_wp,
                            net_payoff=real_b,
                            cfg=real_sizing,
                        )
                    if real_kelly_total < 2.50:
                        _SIM_CURRENT["real_decision_stage"] = "min_absolute_stake_check"
                        _SIM_CURRENT["real_kelly_raw_quote"] = round(float(real_kelly_total), 4)
                        _SIM_CURRENT["real_min_absolute_stake"] = 2.50
                        window_abs_cap = runtime_window_cap_abs if runtime_window_cap_abs > 0 else float("inf")
                        min_abs_boost_cap = window_abs_cap
                        _SIM_CURRENT["real_min_abs_boost_available"] = True
                        _SIM_CURRENT["real_min_abs_boost_cap"] = round(float(min_abs_boost_cap), 4) if math.isfinite(float(min_abs_boost_cap)) else None
                        if min_abs_boost_cap >= 2.50:
                            real_kelly_total = 2.50
                            _SIM_CURRENT["real_min_abs_boost_used"] = True
                            _SIM_CURRENT["real_min_funding_boost_used_this_decision"] = True
                        else:
                            _SIM_CURRENT["real_status"] = "real_Kelly<2.5"
                            _SIM_CURRENT["real_block_reason"] = "kelly_below_min_absolute_and_boost_unavailable_or_cap_cannot_boost"
                            _audit_real_decision("not_submitted", "kelly_below_min_absolute_and_boost_unavailable_or_cap_cannot_boost", {
                                "real_kelly_total": round(float(real_kelly_total), 4),
                                "min_abs_boost_available": True,
                                "min_abs_boost_cap": _SIM_CURRENT.get("real_min_abs_boost_cap"),
                            })
                    if real_kelly_total >= 2.50:
                        active = self.market_resolver.get_active()
                        if not active:
                            _SIM_CURRENT["real_status"] = "real_active_market_unavailable"
                            _SIM_CURRENT["real_decision_stage"] = "active_market_check"
                            _SIM_CURRENT["real_block_reason"] = "active_market_unavailable"
                            _audit_real_decision("not_submitted", "active_market_unavailable", {"real_kelly_total": round(float(real_kelly_total), 4)})
                        if active:
                            token_id = active.token_id_yes if best_dir == "up" else active.token_id_no
                            side_label = "DIRECTION"
                            limit_px = round(ask * 1.005 if ask > 0 else ask, 4)
                            platform_min_quote = max(
                                float(POLYMARKET_PLATFORM.min_order_quote_usdc),
                                float(POLYMARKET_PLATFORM.min_limit_order_shares) * float(limit_px),
                            )
                            min_5_shares_quote = float(POLYMARKET_PLATFORM.min_limit_order_shares) * float(limit_px)
                            _SIM_CURRENT["real_decision_stage"] = "platform_min_check"
                            _SIM_CURRENT["real_platform_min_quote"] = round(platform_min_quote, 4)
                            _SIM_CURRENT["real_min_5_shares_quote"] = round(min_5_shares_quote, 4)
                            _SIM_CURRENT["real_min_order_quote"] = round(float(POLYMARKET_PLATFORM.min_order_quote_usdc), 4)
                            _SIM_CURRENT["real_limit_px"] = round(float(limit_px), 4)
                            if real_kelly_total < platform_min_quote:
                                window_abs_cap = runtime_window_cap_abs if runtime_window_cap_abs > 0 else float("inf")
                                window_ratio_cap = float(real_equity or 0.0) * effective_max_stake_ratio if effective_max_stake_ratio > 0 else float("inf")
                                ratio_limited_cap = min(window_abs_cap, window_ratio_cap)
                                boost_cap = window_abs_cap
                                _SIM_CURRENT["real_platform_min_boost_available"] = True
                                _SIM_CURRENT["real_pre_window_cap"] = round(float(boost_cap), 4) if math.isfinite(float(boost_cap)) else None
                                _SIM_CURRENT["real_ratio_limited_cap"] = round(float(ratio_limited_cap), 4) if math.isfinite(float(ratio_limited_cap)) else None
                                _SIM_CURRENT["real_min_share_exception"] = True
                                _SIM_CURRENT["real_min_share_exception_reason"] = "min_funding_top_up"
                                if boost_cap >= platform_min_quote:
                                    real_kelly_total = platform_min_quote
                                    _SIM_CURRENT["real_platform_min_boost_used"] = True
                                    _SIM_CURRENT["real_min_funding_boost_used_this_decision"] = True
                                else:
                                    _SIM_CURRENT["real_status"] = "real_platform_min_not_met"
                                    _SIM_CURRENT["real_block_reason"] = "platform_min_above_allowed_cap"
                                    _audit_real_decision("not_submitted", "platform_min_above_allowed_cap", {
                                        "boost_cap": round(float(boost_cap), 4) if math.isfinite(float(boost_cap)) else None,
                                        "platform_min_quote": round(float(platform_min_quote), 4),
                                        "one_time_boost_available": True,
                                    })
                                    real_kelly_total = 0.0
                            if real_kelly_total >= platform_min_quote:
                                window_abs_cap = runtime_window_cap_abs if runtime_window_cap_abs > 0 else float("inf")
                                window_ratio_cap = float(real_equity) * effective_max_stake_ratio if effective_max_stake_ratio > 0 else float("inf")
                                high_prob_min_share_exception = (
                                    trade_intent == "ENTRY_TREND"
                                    and float(best_side_prob) >= 0.80
                                    and float(limit_px) < 0.90
                                )
                                min_share_exception = bool(trade_intent == "HEDGE" or high_prob_min_share_exception)
                                platform_min_one_time_exception = bool(
                                    bool(_SIM_CURRENT.get("real_platform_min_boost_used"))
                                    and float(real_kelly_total) <= platform_min_quote + 1e-9
                                )
                                min_share_exception = bool(min_share_exception or platform_min_one_time_exception)
                                hard_window_cap = min(window_abs_cap, window_ratio_cap)
                                if min_share_exception and float(real_kelly_total) <= platform_min_quote + 1e-9:
                                    hard_window_cap = window_abs_cap
                                    _SIM_CURRENT["real_min_share_exception"] = True
                                    _SIM_CURRENT["real_min_share_exception_reason"] = "hedge" if has_opposite_position else "high_prob_price" if high_prob_min_share_exception else "min_funding_top_up" if platform_min_one_time_exception else None
                                if hard_window_cap < platform_min_quote:
                                    _SIM_CURRENT["real_status"] = "real_window_cap_below_platform_min"
                                    _SIM_CURRENT["real_decision_stage"] = "window_cap_check"
                                    _SIM_CURRENT["real_block_reason"] = "window_cap_below_platform_min"
                                    _SIM_CURRENT["real_window_cap"] = round(float(hard_window_cap), 2)
                                    if self.store is not None:
                                        self.store.append_audit("order_compliance_skip", {
                                            "window_id": window_id,
                                            "direction": best_dir,
                                            "reason": "window_cap_below_platform_min",
                                            "window_cap": round(float(hard_window_cap), 4),
                                            "platform_min_quote": round(platform_min_quote, 4),
                                            **signal_meta,
                                        })
                                    _audit_real_decision("not_submitted", "window_cap_below_platform_min", {
                                        "window_cap": round(float(hard_window_cap), 4),
                                        "platform_min_quote": round(float(platform_min_quote), 4),
                                    })
                                    real_kelly_total = 0.0
                                else:
                                    real_kelly_total = min(float(real_kelly_total), float(hard_window_cap))
                                    _SIM_CURRENT["real_window_cap"] = round(float(hard_window_cap), 2)
                            if real_kelly_total >= platform_min_quote:
                                _SIM_CURRENT["real_decision_stage"] = "submit_ready"
                                real_single = min(float(real_kelly_total), max(depth_cap, platform_min_quote))
                                if real_single < platform_min_quote:
                                    real_single = platform_min_quote
                                self._submit_real_window_fok(
                                    window_id=window_id,
                                    best_dir=best_dir,
                                    side_label=side_label,
                                    token_id=token_id,
                                    target_quote=float(real_kelly_total),
                                    limit_price=float(limit_px),
                                    note=f"p={best_side_prob:.3f} edge={best_edge:.3f} ev={best_ev_simple:.3f} kraw={best_kelly_raw:.3f} kelly={real_kelly_total:.2f} phase={phase} sec={int(elapsed_sec)} tier={sizing_tier}",
                                    sizing_tier=sizing_tier,
                                    max_attempt_quote=float(real_single),
                                    decision_meta={
                                        **signal_meta,
                                        "best_side_prob": round(best_side_prob, 6),
                                        "best_edge": round(best_edge, 6),
                                        "best_ev": round(best_ev_simple, 6),
                                        "best_kelly_raw": round(best_kelly_raw, 6),
                                        "target_quote": round(float(real_kelly_total), 4),
                                        "attempt_quote": round(float(real_single), 4),
                                        "effective_max_stake_ratio": round(effective_max_stake_ratio, 6),
                                        "real_window_cap": round(float(_SIM_CURRENT.get("real_window_cap") or 0.0), 4),
                                        "real_min_share_exception": bool(_SIM_CURRENT.get("real_min_share_exception", False)),
                                        "real_min_share_exception_reason": _SIM_CURRENT.get("real_min_share_exception_reason"),
                                        "trade_intent": trade_intent,
                                        "intent_reason": lifecycle["reason"],
                                    },
                                )
                                _SIM_CURRENT["real_status"] = "real_fok_evaluated"
                                _SIM_CURRENT["real_decision_stage"] = "fok_submitted"
                                _SIM_CURRENT["real_target_quote"] = round(float(real_kelly_total), 2)
                                _SIM_CURRENT["real_attempt_quote"] = round(float(real_single), 2)
                                _audit_real_decision("submitted", "fok_submitted", {
                                    "target_quote": round(float(real_kelly_total), 4),
                                    "attempt_quote": round(float(real_single), 4),
                                    "limit_price": round(float(limit_px), 4),
                                    "platform_min_quote": round(float(platform_min_quote), 4),
                                })
            except Exception as e:
                logger.warning("real window fok submit failed: %s", e)
                _SIM_CURRENT["real_status"] = "real_submit_error"
                _SIM_CURRENT["real_decision_stage"] = "exception"
                _SIM_CURRENT["real_block_reason"] = str(e)[:120]
                _audit_real_decision("error", "real_submit_error", {"error": str(e)[:240]})

        if has_opposite_position:
            _SIM_CURRENT["reverse_direction_allowed"] = True

        try:
            equity = self._sim_equity
            sizing = SizingCfg(kelly_fraction=sizing_fraction, max_stake_ratio=max_stake_ratio, min_absolute_stake=2.50)
            wp = best_side_prob
            b = (1 - ask) / ask if ask > 0 else 1
            proposed_target = stake_for_trade(portfolio_equity=equity, win_prob=wp, net_payoff=b, cfg=sizing)
        except Exception:
            equity = self._sim_equity
            proposed_target = 5.0
        platform_min_quote = max(
            float(POLYMARKET_PLATFORM.min_order_quote_usdc),
            float(POLYMARKET_PLATFORM.min_limit_order_shares) * float(ask),
        )
        if proposed_target < platform_min_quote:
            if equity * max_stake_ratio < platform_min_quote:
                _SIM_CURRENT["status"] = f"platform_min<{platform_min_quote:.2f}"
                _SIM_CURRENT["platform_min_quote"] = round(platform_min_quote, 2)
                _SIM_CURRENT["best_dir"] = best_dir
                _record_decision(0, "rejected", f"platform_min<{platform_min_quote:.2f}")
                self._write_current_window_snapshot()
                return
            proposed_target = platform_min_quote

        if dir_key not in win_budget:
            win_target[dir_key] = proposed_target
            win_budget[dir_key] = proposed_target
            win_dir[window_id] = best_dir
            _SIM_WIN_ENTRY_PROB[dir_key] = float(best_side_prob)
            _SIM_WIN_LAST_PROB[dir_key] = float(best_side_prob)
            _SIM_WIN_ENTRY_EV[dir_key] = float(best_ev_simple)
            kelly_total = proposed_target
        else:
            current_target = float(win_target.get(dir_key, win_budget.get(dir_key, 0.0)))
            if proposed_target > current_target + 1e-9:
                entry_prob = float(_SIM_WIN_ENTRY_PROB.get(dir_key, best_side_prob))
                last_prob = float(_SIM_WIN_LAST_PROB.get(dir_key, entry_prob))
                probability_deteriorated = best_side_prob < max(entry_prob - 0.05, last_prob - 0.03)
                if probability_deteriorated:
                    _SIM_CURRENT["status"] = "add_blocked_probability_deteriorated"
                    _SIM_CURRENT["add_blocked"] = True
                    _record_decision(0, "rejected", "add_blocked_probability_deteriorated", {
                        "entry_prob": round(entry_prob, 4),
                        "last_prob": round(last_prob, 4),
                        "add_blocked": True,
                    })
                    self._write_current_window_snapshot()
                    return
                delta = proposed_target - current_target
                win_target[dir_key] = proposed_target
                win_budget[dir_key] = float(win_budget.get(dir_key, 0.0)) + delta
                filled_set.discard(dir_key)
                _SIM_CURRENT["target_upgraded"] = True
                _SIM_CURRENT["target_upgrade_delta"] = round(delta, 2)
            _SIM_WIN_LAST_PROB[dir_key] = float(best_side_prob)
            _SIM_WIN_ENTRY_EV[dir_key] = max(float(_SIM_WIN_ENTRY_EV.get(dir_key, 0.0) or 0.0), float(best_ev_simple))
            kelly_total = float(win_target.get(dir_key, proposed_target))

        remaining = win_budget.get(dir_key, 0)
        if remaining <= 0:
            filled_set.add(dir_key)
            _SIM_CURRENT["status"] = "filled"; self._write_current_window_snapshot(); return

        single = min(remaining, max(depth_cap, 2.50))
        if remaining < 2.50:
            single = remaining
        elif single < 2.50:
            single = 2.50

        win_budget[dir_key] = remaining - single
        _record_decision(single, "filled", "FILLED", {
            "budget_remain": round(win_budget.get(dir_key, 0), 2),
            "budget_total": round(kelly_total, 2),
            "target_upgraded": bool(_SIM_CURRENT.get("target_upgraded", False)),
            "target_upgrade_count": 1 if bool(_SIM_CURRENT.get("target_upgraded", False)) else 0,
        })

        if win_budget[dir_key] <= 0:
            filled_set.add(dir_key)
            _SIM_CURRENT["status"] = "FILLED"
        else:
            _SIM_CURRENT["status"] = f"part_fill"
        _SIM_CURRENT["best_dir"] = best_dir
        _SIM_CURRENT["fill_amt"] = round(single, 2)
        _SIM_CURRENT["fill_ask"] = round(ask, 4)
        _SIM_CURRENT["fill_ev"] = round(best_ev_simple, 4)
        _SIM_CURRENT["fill_edge"] = round(best_edge, 4)
        _SIM_CURRENT["fill_kelly_raw"] = round(best_kelly_raw, 4)
        _SIM_CURRENT["budget_remain"] = round(win_budget.get(dir_key, 0), 2)
        _SIM_CURRENT["budget_total"] = round(kelly_total, 2)
        self._write_current_window_snapshot()

    def _fetch_real_equity_cached(self, *, ttl_sec: float = 3.0) -> Optional[float]:
        now_ms = int(time.time() * 1000)
        if self._real_equity_cache_usdc is not None and now_ms - int(self._real_equity_cache_ts_ms or 0) <= int(ttl_sec * 1000):
            return float(self._real_equity_cache_usdc)
        if self.poly_client is None:
            return None
        equity = self.poly_client.fetch_account_equity_usdc()
        if equity is not None:
            self._real_equity_cache_usdc = float(equity)
            self._real_equity_cache_ts_ms = now_ms
        return equity

    def _apply_real_fok_ticket(self, state: dict[str, Any], ticket: Optional[OrderTicket]) -> None:
        now_ms = int(time.time() * 1000)
        state["attempt_in_flight"] = False
        state["last_attempt_ts_ms"] = now_ms
        if ticket is None:
            state["last_ticket_state"] = "not_submitted"
            state["last_error"] = "precheck_not_submitted"
            return

        state["last_ticket_state"] = ticket.state.value
        state["last_error"] = ticket.last_error
        state["last_order_id"] = ticket.exchange_order_id
        _SIM_CURRENT["real_last_ticket_state"] = ticket.state.value
        _SIM_CURRENT["real_last_error"] = ticket.last_error
        _SIM_CURRENT["real_last_order_id"] = ticket.exchange_order_id
        matched = max(0.0, float(ticket.filled_size_shares or 0.0))
        if ticket.state in (OrderState.FILLED, OrderState.PARTIAL) and matched <= 1e-9:
            state["locked"] = True
            state["lock_reason"] = "unknown_filled_size"
            state["unknown_outcome"] = True
            return

        if matched > 1e-9:
            fill_quote = matched * float(state["limit_price"])
            state["spent_quote"] = min(
                float(state["target_quote"]),
                float(state.get("spent_quote", 0.0)) + fill_quote,
            )
            state["remaining_quote"] = max(
                0.0,
                float(state["target_quote"]) - float(state["spent_quote"]),
            )
            state["filled_shares"] = min(
                float(state["target_shares"]),
                float(state.get("filled_shares", 0.0)) + matched,
            )
            state["remaining_shares"] = max(
                0.0,
                float(state["remaining_quote"]) / max(float(state["limit_price"]), 0.01),
            )

        min_shares = float(POLYMARKET_PLATFORM.min_limit_order_shares)
        min_quote = float(POLYMARKET_PLATFORM.min_order_quote_usdc)
        remaining_quote = float(state.get("remaining_shares", 0.0)) * float(state["limit_price"])
        if float(state.get("remaining_shares", 0.0)) <= 1e-9:
            state["locked"] = True
            state["lock_reason"] = "target_filled"
            return
        if matched > 1e-9 and remaining_quote < min_quote:
            state["locked"] = True
            state["lock_reason"] = "dust_remaining"
            return

        err = str(ticket.last_error or "").lower()
        if ticket.state in _REAL_RETRYABLE_STATES and ("fok_no_fill" in err or "not_fill" in err or "not filled" in err or "fully filled" in err):
            state["no_fill_count"] = int(state.get("no_fill_count", 0)) + 1
            return
        if ticket.state == OrderState.DRY_RUN_SHADOW:
            state["locked"] = True
            state["lock_reason"] = "dry_run_shadow"
            return
        if ticket.state == OrderState.TIMEOUT or any(x in err for x in _REAL_UNKNOWN_ERRORS):
            state["locked"] = True
            state["lock_reason"] = "unknown_fok_outcome"
            state["unknown_outcome"] = True
            return
        if ticket.state not in _REAL_RETRYABLE_STATES:
            state["locked"] = True
            state["lock_reason"] = f"unhandled_state:{ticket.state.value}"

    def _submit_real_window_fok(
        self,
        *,
        window_id: str,
        best_dir: str,
        side_label: str,
        token_id: str,
        target_quote: float,
        limit_price: float,
        note: str,
        sizing_tier: str = "base",
        max_attempt_quote: Optional[float] = None,
        decision_meta: Optional[dict[str, Any]] = None,
    ) -> Optional[OrderTicket]:
        order_key = f"{window_id}:{best_dir}"
        state = _REAL_WINDOW_ORDERS.get(order_key)
        min_shares = float(POLYMARKET_PLATFORM.min_limit_order_shares)
        min_quote = float(POLYMARKET_PLATFORM.min_order_quote_usdc)
        latest_target_shares = float(target_quote) / max(float(limit_price), 0.01)
        if state is None and latest_target_shares + 1e-9 < min_shares:
            logger.info("real window skip below min shares window_id=%s shares=%.4f", window_id, latest_target_shares)
            _SIM_CURRENT["real_status"] = "real_below_min_shares"
            _SIM_CURRENT["real_decision_stage"] = "submit_precheck_min_shares"
            _SIM_CURRENT["real_target_shares"] = round(float(latest_target_shares), 4)
            _SIM_CURRENT["real_min_shares"] = round(float(min_shares), 4)
            return None

        if state is None:
            state = {
                "window_id": window_id,
                "direction": best_dir,
                "side_label": side_label,
                "token_id": token_id,
                "target_quote": float(target_quote),
                "spent_quote": 0.0,
                "remaining_quote": float(target_quote),
                "target_shares": float(latest_target_shares),
                "filled_shares": 0.0,
                "remaining_shares": float(latest_target_shares),
                "limit_price": float(limit_price),
                "sizing_tier": str(sizing_tier),
                "max_target_quote_seen": float(target_quote),
                "target_upgrade_count": 0,
                "locked": False,
                "lock_reason": None,
                "unknown_outcome": False,
                "attempt_in_flight": False,
                "attempt_seq": 0,
                "no_fill_count": 0,
                "created_ts_ms": int(time.time() * 1000),
                "entry_prob": float((decision_meta or {}).get("best_side_prob") or 0.0),
                "entry_phase": (decision_meta or {}).get("phase"),
                "add_blocked": False,
                "decision_meta": dict(decision_meta or {}),
            }
            _REAL_WINDOW_ORDERS[order_key] = state
        else:
            filled_shares = float(state.get("filled_shares", 0.0))
            if decision_meta:
                state["decision_meta"] = dict(decision_meta)
            no_position = filled_shares <= 1e-9 and not bool(state.get("unknown_outcome"))
            if best_dir != state.get("direction"):
                if no_position:
                    logger.info(
                        "real window direction refreshed after no-fill/no-position window_id=%s new=%s old=%s",
                        window_id, best_dir, state.get("direction"),
                    )
                    target_quote_locked = max(float(state.get("target_quote", target_quote)), float(target_quote))
                    latest_target_shares = target_quote_locked / max(float(limit_price), 0.01)
                    state.update({
                        "direction": best_dir,
                        "side_label": side_label,
                        "token_id": token_id,
                        "target_quote": target_quote_locked,
                        "sizing_tier": str(sizing_tier),
                        "max_target_quote_seen": target_quote_locked,
                        "remaining_quote": target_quote_locked,
                        "target_shares": float(latest_target_shares),
                        "remaining_shares": float(latest_target_shares),
                        "limit_price": float(limit_price),
                        "locked": False,
                        "lock_reason": None,
                    })
                else:
                    state["locked"] = True
                    state["lock_reason"] = "reverse_signal"
                    logger.warning(
                        "real window reverse lock window_id=%s new=%s old=%s",
                        window_id, best_dir, state.get("direction"),
                    )
                    return None
            elif no_position:
                target_quote_locked = max(float(state.get("target_quote", target_quote)), float(target_quote))
                latest_target_shares = target_quote_locked / max(float(limit_price), 0.01)
                state.update({
                    "side_label": side_label,
                    "token_id": token_id,
                    "target_quote": target_quote_locked,
                    "sizing_tier": str(sizing_tier),
                    "max_target_quote_seen": target_quote_locked,
                    "remaining_quote": target_quote_locked,
                    "target_shares": float(latest_target_shares),
                    "remaining_shares": float(latest_target_shares),
                    "limit_price": float(limit_price),
                    "locked": False,
                    "lock_reason": None,
                })
            else:
                spent_quote = max(0.0, float(state.get("spent_quote", 0.0)))
                prior_target = float(state.get("target_quote", target_quote))
                new_target = float(target_quote)
                current_prob = float((decision_meta or {}).get("best_side_prob") or 0.0)
                entry_prob = float(state.get("entry_prob") or current_prob or 0.0)
                last_prob = float(state.get("last_best_side_prob") or entry_prob or current_prob or 0.0)
                target_upgrade_requested = new_target > prior_target + 1e-9
                probability_deteriorated = bool(current_prob > 0 and current_prob < max(entry_prob - 0.05, last_prob - 0.03))
                if target_upgrade_requested and probability_deteriorated:
                    state["locked"] = True
                    state["lock_reason"] = "add_blocked_probability_deteriorated"
                    state["add_blocked"] = True
                    state["blocked_current_prob"] = current_prob
                    state["blocked_entry_prob"] = entry_prob
                    state["blocked_last_prob"] = last_prob
                    logger.warning(
                        "real window add blocked: probability deteriorated window_id=%s dir=%s current=%.4f entry=%.4f last=%.4f",
                        window_id, state.get("direction"), current_prob, entry_prob, last_prob,
                    )
                    if self.store is not None:
                        self.store.append_audit("order_add_blocked", {
                            "window_id": window_id,
                            "direction": state.get("direction"),
                            "reason": "probability_deteriorated",
                            "current_prob": round(current_prob, 4),
                            "entry_prob": round(entry_prob, 4),
                            "last_prob": round(last_prob, 4),
                            "prior_target_quote": round(prior_target, 4),
                            "requested_target_quote": round(new_target, 4),
                            **dict(decision_meta or {}),
                        })
                    return None
                total_quote = max(prior_target, new_target)
                if total_quote > float(state.get("target_quote", 0.0)) + 1e-9:
                    state["target_upgrade_count"] = int(state.get("target_upgrade_count", 0)) + 1
                    state["last_upgrade_phase"] = (decision_meta or {}).get("phase")
                state["last_best_side_prob"] = current_prob or last_prob
                remaining_quote = max(0.0, total_quote - spent_quote)
                state.update({
                    "side_label": side_label,
                    "token_id": token_id,
                    "target_quote": total_quote,
                    "sizing_tier": str(sizing_tier),
                    "max_target_quote_seen": max(float(state.get("max_target_quote_seen", 0.0)), total_quote),
                    "remaining_quote": remaining_quote,
                    "target_shares": float(state.get("filled_shares", 0.0)) + remaining_quote / max(float(limit_price), 0.01),
                    "remaining_shares": remaining_quote / max(float(limit_price), 0.01),
                    "limit_price": float(limit_price),
                    "locked": False,
                    "lock_reason": None,
                })

        if state.get("locked") or state.get("unknown_outcome") or state.get("attempt_in_flight"):
            return None

        remaining_shares = float(state.get("remaining_shares", 0.0))
        no_fill_count = int(state.get("no_fill_count", 0))
        px = max(float(state["limit_price"]), 0.01)
        min_quote_shares = math.ceil((min_quote / px) * 100.0) / 100.0
        min_attempt_shares = max(min_shares, min_quote_shares)
        max_quote_shares = None
        if max_attempt_quote is not None and float(max_attempt_quote) > 0:
            max_quote_shares = max(0.0, float(max_attempt_quote) / px)
        if no_fill_count > 0:
            attempt_shares = remaining_shares if remaining_shares <= min_attempt_shares * 2 else min_attempt_shares
        else:
            attempt_shares = remaining_shares
        if max_quote_shares is not None:
            if max_quote_shares + 1e-9 >= min_attempt_shares:
                attempt_shares = min(attempt_shares, max_quote_shares)
            elif remaining_shares >= min_attempt_shares:
                attempt_shares = min(min_attempt_shares, remaining_shares)
        if attempt_shares + 1e-9 < min_attempt_shares and remaining_shares >= min_attempt_shares:
            attempt_shares = min(min_attempt_shares, remaining_shares)
        attempt_quote = attempt_shares * px
        if attempt_quote + 1e-9 < min_quote:
            state["locked"] = True
            state["lock_reason"] = "dust_remaining"
            _SIM_CURRENT["real_status"] = "real_attempt_quote_below_min"
            _SIM_CURRENT["real_decision_stage"] = "submit_precheck_min_quote"
            _SIM_CURRENT["real_attempt_quote"] = round(float(attempt_quote), 4)
            _SIM_CURRENT["real_min_order_quote"] = round(float(min_quote), 4)
            return None
        if attempt_shares + 1e-9 < min_shares:
            state["last_error"] = "waiting_min_shares_at_current_price"
            state["last_wait_price"] = float(state["limit_price"])
            _SIM_CURRENT["real_status"] = "real_waiting_min_shares"
            _SIM_CURRENT["real_decision_stage"] = "submit_precheck_min_shares"
            _SIM_CURRENT["real_attempt_shares"] = round(float(attempt_shares), 4)
            _SIM_CURRENT["real_min_shares"] = round(float(min_shares), 4)
            return None

        book = self.poly_client.fetch_book(token_id) if self.poly_client is not None else {}
        if book.get("stale") or book.get("best_ask") is None:
            state["last_error"] = "depth_precheck_stale_or_no_ask"
            state["last_precheck_ts_ms"] = int(time.time() * 1000)
            _SIM_CURRENT["real_status"] = "real_depth_unavailable"
            _SIM_CURRENT["real_decision_stage"] = "depth_precheck"
            _SIM_CURRENT["real_block_reason"] = "stale_or_no_ask"
            return None
        live_ask = float(book.get("best_ask") or 0.0)
        live_ask_size = float(book.get("best_ask_size") or 0.0)
        if live_ask <= 0 or live_ask > float(state["limit_price"]) + 1e-9:
            state["last_error"] = "ask_moved_above_limit"
            state["last_live_ask"] = live_ask
            state["last_limit_price"] = float(state["limit_price"])
            state["last_precheck_ts_ms"] = int(time.time() * 1000)
            _SIM_CURRENT["real_status"] = "real_ask_moved_above_limit"
            _SIM_CURRENT["real_decision_stage"] = "depth_precheck"
            _SIM_CURRENT["real_live_ask"] = round(float(live_ask), 4)
            _SIM_CURRENT["real_limit_px"] = round(float(state["limit_price"]), 4)
            return None
        if live_ask_size + 1e-9 < attempt_shares:
            state["last_error"] = "depth_below_min_chunk"
            state["last_live_ask"] = live_ask
            state["last_live_ask_size"] = live_ask_size
            state["last_attempt_shares_needed"] = float(attempt_shares)
            state["last_precheck_ts_ms"] = int(time.time() * 1000)
            _SIM_CURRENT["real_status"] = "real_depth_below_chunk"
            _SIM_CURRENT["real_decision_stage"] = "depth_precheck"
            _SIM_CURRENT["real_live_ask"] = round(float(live_ask), 4)
            _SIM_CURRENT["real_live_ask_size"] = round(float(live_ask_size), 4)
            _SIM_CURRENT["real_attempt_shares"] = round(float(attempt_shares), 4)
            return None

        state["attempt_seq"] = int(state.get("attempt_seq", 0)) + 1
        state["attempt_in_flight"] = True
        client_order_id = f"{window_id}:{state['direction']}:fok:{state['attempt_seq']}"
        state["last_client_order_id"] = client_order_id
        state["last_attempt_shares"] = float(attempt_shares)
        _SIM_CURRENT["real_decision_stage"] = "fok_submit_call"
        _SIM_CURRENT["real_client_order_id"] = client_order_id
        _SIM_CURRENT["real_attempt_shares"] = round(float(attempt_shares), 4)
        _SIM_CURRENT["real_attempt_quote"] = round(float(attempt_quote), 4)
        order_note = f"{note} nofill={no_fill_count} chunk_shares={attempt_shares:.4f}"
        if state.get("decision_meta"):
            order_note = f"{order_note} meta={json.dumps(state.get('decision_meta'), sort_keys=True, default=str)[:500]}"
        ticket = self.submit_signal_order(
            window_id=window_id,
            side=str(state["side_label"]),
            direction=str(state["direction"]),
            size_quote_usdc=attempt_quote,
            limit_price=float(state["limit_price"]),
            note=order_note,
            fixed_size_shares=attempt_shares,
            fixed_client_order_id=client_order_id,
            audit_context=dict(state.get("decision_meta") or {}),
        )
        self._apply_real_fok_ticket(state, ticket)
        return ticket

    def _write_sim_record(self, wid, trig, p_best, p_side, t_rem, au, ad, best_dir, best_ev, fill_amt, status, reason, d_abs=0, meta=None):
        try:
            import json as _j, os as _o
            meta = dict(meta or {})
            _o.makedirs("logs", exist_ok=True)
            ask = au if best_dir == "up" else ad if best_dir == "down" else None
            edge = (float(p_side) - float(ask)) if ask is not None else None
            kelly_raw = self._raw_kelly_ratio(float(p_side), float(ask)) if ask is not None else None
            rec = {
                "window_id": wid,
                "status": status,
                "reason": reason,
                "trigger_pattern": trig,
                "phase": meta.get("phase"),
                "sec_in_window": meta.get("sec_in_window"),
                "elapsed_sec": meta.get("elapsed_sec"),
                "p_up": meta.get("p_up"),
                "p_down": meta.get("p_down"),
                "p_best": round(p_best, 3),
                "p_side": round(p_side, 3),
                "best_side_prob": round(p_side, 4),
                "T_remaining": round(t_rem, 0),
                "ask_up": au,
                "ask_down": ad,
                "best_dir": best_dir,
                "best_ev": round(best_ev, 4),
                "edge": None if edge is None else round(edge, 4),
                "kelly_raw": None if kelly_raw is None else round(kelly_raw, 4),
                "req_edge": meta.get("req_edge"),
                "req_ev": meta.get("req_ev"),
                "req_kelly_raw": meta.get("req_kelly_raw"),
                "sizing_fraction": meta.get("sizing_fraction"),
                "max_stake_ratio": meta.get("max_stake_ratio"),
                "effective_max_stake_ratio": meta.get("effective_max_stake_ratio"),
                "real_max_window_risk_usdc": meta.get("real_max_window_risk_usdc"),
                "startup_observe_only": meta.get("startup_observe_only"),
                "startup_missed_sec": meta.get("startup_missed_sec"),
                "target_upgrade_count": meta.get("target_upgrade_count"),
                "add_blocked": meta.get("add_blocked"),
                "sizing_tier": meta.get("sizing_tier"),
                "fill_amount": round(fill_amt, 2),
                "d_abs_pct": round(d_abs, 4),
                "ts_ms": int(__import__("time").time() * 1000),
            }
            rec.update({k: v for k, v in meta.items() if k not in rec and (k.startswith("real_") or k in {
                "trade_intent",
                "intent_allowed",
                "intent_reason",
                "lifecycle_phase_policy",
                "is_hedge",
                "is_add",
                "is_value_entry",
                "is_trend_entry",
                "req_prob",
            })})
            with open(self._runtime_path("logs", "shadow_orders.jsonl"), "ab+") as _f:
                _f.seek(0, 2); pos = _f.tell()
                if pos > 0:
                    _f.seek(pos - 1)
                    if _f.read(1) != b"\n": _f.write(b"\n")
                _f.write((_j.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception as e:
            logger.debug("write_sim_record failed: %s", e)

    @staticmethod
    def _phase_gate_rule(phase: int) -> tuple[float, int, float]:
        rules = {
            0: (0.60, 6, 0.00),
            1: (0.65, 5, 0.02),
            2: (0.70, 4, 0.04),
            3: (0.65, 3, 0.06),
            4: (0.60, 2, 0.08),
        }
        return rules.get(max(0, min(4, int(phase))), rules[4])

    def _record_probability_history(self, window_id: str, *, p_up: float, p_down: float) -> None:
        if not window_id:
            return
        now_ms = int(time.time() * 1000)
        hist = self._prob_history.setdefault(str(window_id), [])
        hist.append((now_ms, float(p_up), float(p_down)))
        cutoff = now_ms - 20_000
        self._prob_history[str(window_id)] = [x for x in hist if int(x[0]) >= cutoff]
        for key in list(self._prob_history.keys()):
            if key != str(window_id):
                self._prob_history.pop(key, None)

    def _is_probability_above(self, window_id: str, direction: str, *, seconds: int, threshold: float) -> bool:
        hist = self._prob_history.get(str(window_id)) or []
        required = max(1, int(seconds))
        if len(hist) < required:
            return False
        now_ms = int(time.time() * 1000)
        recent = [x for x in hist if int(x[0]) >= now_ms - required * 1000]
        if len(recent) < required:
            return False
        vals = [float(x[1] if direction == "up" else x[2]) for x in recent[-required:]]
        return all(v > float(threshold) for v in vals)

    def _is_probability_rising(self, window_id: str, direction: str, *, seconds: int) -> bool:
        hist = self._prob_history.get(str(window_id)) or []
        if len(hist) < max(3, int(seconds) - 1):
            return False
        now_ms = int(time.time() * 1000)
        recent = [x for x in hist if int(x[0]) >= now_ms - int(seconds) * 1000]
        if len(recent) < max(3, int(seconds) - 1):
            return False
        vals = [float(x[1] if direction == "up" else x[2]) for x in recent[-int(seconds):]]
        return all(vals[i] >= vals[i - 1] - 1e-9 for i in range(1, len(vals)))

    def _probability_drawdown_ok(self, window_id: str, direction: str, *, seconds: int, max_drop: float) -> tuple[bool, Optional[float]]:
        hist = self._prob_history.get(str(window_id)) or []
        required = max(2, int(seconds))
        if len(hist) < required:
            return False, None
        now_ms = int(time.time() * 1000)
        recent = [x for x in hist if int(x[0]) >= now_ms - required * 1000]
        if len(recent) < required:
            return False, None
        vals = [float(x[1] if direction == "up" else x[2]) for x in recent[-required:]]
        drop = max(0.0, vals[0] - vals[-1])
        return drop <= float(max_drop) + 1e-9, drop

    @staticmethod
    def _classify_lifecycle_trade_intent(
        *,
        phase: int,
        p_side: float,
        ask: float,
        edge: float,
        ev: float,
        kelly_raw: float,
        has_same_position: bool,
        has_opposite_position: bool,
    ) -> str:
        if has_opposite_position:
            return "HEDGE"
        if has_same_position:
            return "ADD"
        if phase <= 1:
            return "ENTRY_VALUE"
        return "ENTRY_TREND"

    @classmethod
    def _evaluate_lifecycle_intent_gate(
        cls,
        *,
        phase: int,
        p_side: float,
        ask: float,
        edge: float,
        ev: float,
        kelly_raw: float,
        has_same_position: bool,
        has_opposite_position: bool,
        p_confirm_ok: bool = False,
        p_confirm_sec: int = 0,
        p_stability_ok: bool = True,
        p_stability_sec: int = 0,
        p_stability_max_drop: float = 0.0,
        p_stability_drop: Optional[float] = None,
        low_price_high_ev_monotonic_required: bool = False,
        low_price_high_ev_monotonic_ok: bool = True,
        low_price_high_ev_monotonic_sec: int = 0,
    ) -> dict[str, Any]:
        intent = cls._classify_lifecycle_trade_intent(
            phase=phase,
            p_side=p_side,
            ask=ask,
            edge=edge,
            ev=ev,
            kelly_raw=kelly_raw,
            has_same_position=has_same_position,
            has_opposite_position=has_opposite_position,
        )
        meta: dict[str, Any] = {
            "lifecycle_phase": phase,
            "trade_intent": intent,
            "has_same_position": bool(has_same_position),
            "has_opposite_position": bool(has_opposite_position),
            "p_confirm_ok": bool(p_confirm_ok),
            "p_confirm_required_sec": int(p_confirm_sec or 0),
            "p_stability_ok": bool(p_stability_ok),
            "p_stability_required_sec": int(p_stability_sec or 0),
            "p_stability_max_drop": float(p_stability_max_drop or 0.0),
            "p_stability_drop": round(float(p_stability_drop), 6) if p_stability_drop is not None else None,
            "low_price_high_ev_monotonic_required": bool(low_price_high_ev_monotonic_required),
            "low_price_high_ev_monotonic_ok": bool(low_price_high_ev_monotonic_ok),
            "low_price_high_ev_monotonic_sec": int(low_price_high_ev_monotonic_sec or 0),
        }

        def result(allowed: bool, reason: str, policy: str, req_prob: float, req_edge: float, req_ev: float, req_kelly: float, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
            merged = dict(meta)
            if extra:
                merged.update(extra)
            return {
                "allowed": allowed,
                "trade_intent": intent,
                "reason": reason,
                "phase_policy": policy,
                "req_prob": req_prob,
                "req_edge": req_edge,
                "req_ev": req_ev,
                "req_kelly_raw": req_kelly,
                "meta": merged,
            }

        req_prob, confirm_sec, req_net_ev = cls._phase_gate_rule(phase)
        friction_adjusted_ev = cls._calc_ev(p_side, ask * 1.005)
        p_ok = bool(p_confirm_ok) and p_side > req_prob
        ask_ok = ask < 0.80
        ev_ok = friction_adjusted_ev > req_net_ev
        stability_ok = bool(p_stability_ok)
        monotonic_ok = (not low_price_high_ev_monotonic_required) or bool(low_price_high_ev_monotonic_ok)
        ok = p_ok and ask_ok and ev_ok and stability_ok and monotonic_ok
        return result(
            ok,
            f"allowed_phase{max(0, min(4, int(phase)))}_gate" if ok else f"phase{max(0, min(4, int(phase)))}_gate_not_met",
            f"phase{max(0, min(4, int(phase)))}_p_confirm_net_ev",
            req_prob,
            -1.0,
            req_net_ev,
            0.0,
            {
                "trend_max_ask_exclusive": 0.80,
                "p_confirm_required_sec": int(confirm_sec),
                "p_confirm_ok": bool(p_confirm_ok),
                "p_confirm_threshold": float(req_prob),
                "p_stability_ok": bool(p_stability_ok),
                "p_stability_required_sec": int(p_stability_sec or 0),
                "p_stability_max_drop": round(float(p_stability_max_drop or 0.0), 6),
                "p_stability_drop": round(float(p_stability_drop), 6) if p_stability_drop is not None else None,
                "low_price_high_ev_monotonic_required": bool(low_price_high_ev_monotonic_required),
                "low_price_high_ev_monotonic_ok": bool(low_price_high_ev_monotonic_ok),
                "low_price_high_ev_monotonic_sec": int(low_price_high_ev_monotonic_sec or 0),
                "friction_adjusted_ev": round(friction_adjusted_ev, 6),
                "friction_multiplier": 1.005,
            },
        )

        if intent == "HEDGE":
            if phase <= 1:
                req_prob, req_edge, req_ev, req_kelly = 0.35, -1.0, 0.08, 0.0
                ok = p_side >= req_prob and ev >= req_ev
                return result(ok, "allowed_hedge" if ok else "hedge_quality_not_met", "hedge_ev_only", req_prob, req_edge, req_ev, req_kelly)
            if phase == 2:
                req_prob, req_edge, req_ev, req_kelly = 0.60, -1.0, 0.08, 0.0
            else:
                req_prob, req_edge, req_ev, req_kelly = 0.60, -1.0, 0.08, 0.0
            ok = p_side >= req_prob and ev >= req_ev
            return result(ok, "allowed_hedge" if ok else "hedge_quality_not_met", "hedge_priority", req_prob, req_edge, req_ev, req_kelly)

        if intent == "ADD":
            if phase == 2:
                req_prob, req_edge, req_ev, req_kelly = 0.0, -1.0, -1.0, 0.0
                ok = ask < 0.80
                return result(ok, "allowed_phase2_add_price_only" if ok else "phase2_add_price_not_met", "phase2_add_price_lt_0_8", req_prob, req_edge, req_ev, req_kelly, {"add_max_ask_exclusive": 0.80})
            if phase >= 3:
                return result(False, "phase3_add_shadow_only", "no_late_add", 0.0, -1.0, -1.0, 0.0)
            return result(False, "add_not_allowed_before_phase2", "no_early_add", 0.0, -1.0, -1.0, 0.0)

        if intent == "ENTRY_VALUE":
            if phase <= 0:
                req_prob, req_edge, req_ev, req_kelly = 0.35, -1.0, 0.30, 0.0
                ok = p_side >= req_prob and ev > req_ev
                return result(ok, "allowed_phase0_ev" if ok else "phase0_ev_quality_not_met", "phase0_ev_only", req_prob, req_edge, req_ev, req_kelly)
            if phase == 1:
                req_prob, req_edge, req_ev, req_kelly = 0.35, -1.0, 0.15, 0.0
                ok = p_side >= req_prob and ev > req_ev
                return result(ok, "allowed_phase1_ev" if ok else "phase1_ev_quality_not_met", "phase1_ev_only", req_prob, req_edge, req_ev, req_kelly)
            if phase >= 3:
                req_prob, req_edge, req_ev, req_kelly = 0.45, -1.0, 0.80, 0.0
                ok = p_side >= req_prob and ask < 0.80 and ev >= req_ev
                return result(ok, "allowed_phase3_tail_value" if ok else "phase3_tail_value_quality_not_met", "phase3_tail_value_tiny", req_prob, req_edge, req_ev, req_kelly, {"value_max_ask_exclusive": 0.80})
            req_prob, req_edge, req_ev, req_kelly = 0.35, -1.0, 0.40, 0.0
            ok = p_side >= req_prob and ask < 0.80 and ev >= req_ev
            return result(ok, "allowed_phase2_value" if ok else "phase2_value_quality_not_met", "phase2_value_ev_first", req_prob, req_edge, req_ev, req_kelly, {"value_max_ask_exclusive": 0.80})

        if phase <= 0:
            return result(False, "phase0_unreachable_trend", "phase0_ev_only", 0.35, -1.0, 0.30, 0.0)
        if phase == 1:
            return result(False, "phase1_unreachable_trend", "phase1_ev_only", 0.35, -1.0, 0.15, 0.0)
        if phase == 2:
            req_prob, req_edge, req_ev, req_kelly = 0.60, -1.0, 0.0, 0.0
            friction_adjusted_ev = cls._calc_ev(p_side, ask * 1.005)
            ok = p_side >= req_prob and ask < 0.80 and friction_adjusted_ev > req_ev
            return result(ok, "allowed_phase2_trend" if ok else "phase2_trend_quality_not_met", "phase2_trend_price_lt_0_8", req_prob, req_edge, req_ev, req_kelly, {"trend_max_ask_exclusive": 0.80, "friction_adjusted_ev": round(friction_adjusted_ev, 6), "friction_multiplier": 1.005})

        req_prob, req_edge, req_ev, req_kelly = 0.60, -1.0, 0.0, 0.0
        friction_adjusted_ev = cls._calc_ev(p_side, ask * 1.005)
        if phase >= 4:
            ok = p_side >= req_prob and ask < 0.80 and friction_adjusted_ev > req_ev and bool(p_rising_8s)
            return result(ok, "allowed_phase4_rising_trend" if ok else "phase4_trend_shadow_only", "phase4_rising_confirm", req_prob, req_edge, req_ev, req_kelly, {"trend_max_ask_exclusive": 0.80, "p_rising_required_sec": 8, "friction_adjusted_ev": round(friction_adjusted_ev, 6), "friction_multiplier": 1.005})
        ok = p_side >= req_prob and ask < 0.80 and friction_adjusted_ev > req_ev and bool(p_rising_5s)
        return result(ok, "allowed_phase3_rising_trend" if ok else "phase3_trend_shadow_only", "phase3_rising_confirm", req_prob, req_edge, req_ev, req_kelly, {"trend_max_ask_exclusive": 0.80, "p_rising_required_sec": 5, "friction_adjusted_ev": round(friction_adjusted_ev, 6), "friction_multiplier": 1.005})

    @staticmethod
    def _apply_lifecycle_sizing_profile(
        *,
        sizing_fraction: float,
        max_stake_ratio: float,
        sizing_tier: str,
        trade_intent: str,
        phase: int,
    ) -> tuple[float, float, str]:
        if trade_intent == "ENTRY_VALUE":
            if phase >= 3:
                return min(sizing_fraction, 0.08), min(max_stake_ratio, 0.04), f"tail_value_{sizing_tier}"
            return min(sizing_fraction, 0.12), min(max_stake_ratio, 0.06), f"value_{sizing_tier}"
        if trade_intent == "HEDGE":
            return sizing_fraction, max_stake_ratio, f"hedge_{sizing_tier}"
        if trade_intent == "ADD":
            return min(sizing_fraction, 0.14), min(max_stake_ratio, 0.06), f"add_{sizing_tier}"
        return sizing_fraction, max_stake_ratio, f"trend_{sizing_tier}"

    @staticmethod
    def _calc_ev(win_prob: float, ask: float) -> float:
        return win_prob / ask - 1.0

    @staticmethod
    def _raw_kelly_ratio(win_prob: float, ask: float) -> float:
        if ask <= 0 or ask >= 1:
            return 0.0
        return max(0.0, (float(win_prob) - float(ask)) / max(1.0 - float(ask), 1e-9))

    @staticmethod
    def _direction_min_thresholds(
        p_side: float,
        *,
        phase: int = 3,
        elapsed_sec: float = 300.0,
        has_position: bool = False,
    ) -> tuple[float, float, float]:
        if phase <= 0:
            if elapsed_sec < 20.0 and not has_position:
                return 0.15, 0.25, 0.18
            if p_side >= 0.85:
                return 0.09, 0.12, 0.10
            if p_side >= 0.72:
                return 0.12, 0.18, 0.14
            return 0.15, 0.25, 0.18
        if phase == 1:
            if p_side >= 0.85:
                return 0.07, 0.08, 0.08
            if p_side >= 0.80:
                return 0.075, 0.10, 0.09
            return 0.10, 0.16, 0.12
        if phase == 2:
            if p_side >= 0.88:
                return 0.08, 0.05, 0.07
            if p_side >= 0.80:
                return 0.07, 0.08, 0.10
            if p_side >= 0.70:
                return 0.055, 0.08, 0.08
            return 0.08, 0.14, 0.11
        if p_side >= 0.85:
            return 0.08, 0.04, 0.06
        if p_side >= 0.75:
            return 0.055, 0.06, 0.07
        if p_side >= 0.65:
            return 0.055, 0.08, 0.08
        if p_side >= 0.55:
            return 0.06, 0.12, 0.10
        return 0.08, 0.15, 0.12

    @staticmethod
    def _direction_sizing_profile(
        *,
        p_side: float,
        edge: float,
        ev: float,
        kelly_raw: float,
        phase: int = 3,
        has_position: bool = False,
    ) -> tuple[float, float, str]:
        if phase <= 0:
            if p_side >= 0.85 and ev >= 0.12 and kelly_raw >= 0.10:
                return 0.22, 0.14, "phase0_aggressive_probe_plus"
            return 0.18, 0.10, "phase0_aggressive_probe"
        if phase == 1:
            if p_side >= 0.85 and ev >= 0.10 and kelly_raw >= 0.10:
                return 0.26, 0.16, "phase1_aggressive_confirm"
            return 0.22, 0.12, "phase1_aggressive_probe"
        if phase == 2:
            if p_side >= 0.88 and ev >= 0.05 and kelly_raw >= 0.10:
                return 0.32, 0.16, "phase2_exceptional"
            if p_side >= 0.80 and ev >= 0.08 and kelly_raw >= 0.10:
                return 0.28, 0.14, "phase2_strong"
            return 0.22, 0.10, "phase2_base"
        if p_side >= 0.90 and ev >= 0.04 and kelly_raw >= 0.08:
            return 0.35, 0.18, "phase3_exceptional"
        if p_side >= 0.75 and ev >= 0.06 and kelly_raw >= 0.07:
            return 0.28, 0.14, "phase3_strong"
        return 0.20, 0.10, "phase3_base"

    def _resolve_best_direction_by_ev(self, event: dict, window_id: str, *, phase: int = 3):
        if self.poly_client is None or self.market_resolver is None:
            return None, -1.0, None, None
        active = self.market_resolver.get_active()
        if active is None:
            return None, -1.0, None, None
        p_up = float(event.get("p_up") or 0.5)
        p_down = float(event.get("p_down") or (1.0 - p_up))
        p_up = max(0.0, min(1.0, p_up))
        p_down = max(0.0, min(1.0, p_down))
        norm = p_up + p_down
        if norm > 0:
            p_up, p_down = p_up / norm, p_down / norm
        book_up = self.poly_client.fetch_book(active.token_id_yes)
        book_dn = self.poly_client.fetch_book(active.token_id_no)
        ask_up = float(book_up.get("best_ask") or 0.99)
        ask_dn = float(book_dn.get("best_ask") or 0.99)
        if ask_up <= 0 or ask_up >= 1 or ask_dn <= 0 or ask_dn >= 1:
            return None, -1.0, ask_up, ask_dn
        if phase <= 1:
            min_dir_prob = self._phase_gate_rule(phase)[0]
        else:
            min_dir_prob = self._phase_gate_rule(phase)[0]
        up_allowed = p_up > min_dir_prob
        down_allowed = p_down > min_dir_prob
        if not up_allowed and not down_allowed:
            return None, -1.0, ask_up, ask_dn
        ev_up = self._calc_ev(p_up, ask_up) if up_allowed else -1.0
        ev_down = self._calc_ev(p_down, ask_dn) if down_allowed else -1.0
        if ev_up >= ev_down:
            return "up", ev_up, ask_up, ask_dn
        return "down", ev_down, ask_up, ask_dn

    def _on_bar(self, bar: KlineBar) -> None:
        with self._bar_lock:
            self._last_bar = bar
        if self.shadow_engine is not None:
            try:
                self.shadow_engine.on_bar(bar)
            except Exception as e:
                logger.warning("shadow_engine.on_bar failed: %s", e)

    def _on_market_change(self, market: Optional[ActiveMarket]) -> None:
        prev = self._last_active_market
        self._last_active_market = market
        if prev is not None and (market is None or prev.condition_id != market.condition_id):
            self._enqueue_redeem(prev)
        if market is None:
            logger.warning("market changed -> NONE active")
            if self.poly_feed is not None:
                self.poly_feed.subscribe([])
            return
        logger.info(
            "active market: cid=%s end=%s end_ts_ms=%s",
            market.condition_id, market.end_iso, market.end_ts_ms,
        )
        if self.poly_feed is not None:
            self.poly_feed.subscribe([market.token_id_yes, market.token_id_no])

    def _enqueue_redeem(self, market: ActiveMarket) -> None:
        with self._redeem_lock:
            if market.condition_id in self._redeem_queue:
                return
            self._redeem_queue[market.condition_id] = {
                "condition_id": market.condition_id,
                "eligible_ts_ms": int(market.end_ts_ms) + 2_000,
                "attempts": 0,
                "end_iso": market.end_iso,
            }

    def _scan_historical_positions_for_redeem(self) -> None:
        """启动时尝试赎回所有已关闭的 BTC 市场 (不预检, CTF 自行判断)."""
        try:
            if not self.poly_client:
                return
            import urllib.request, json as _j2
            url = "https://clob.polymarket.com/markets?closed=true&limit=30"
            req = urllib.request.Request(url, headers={"User-Agent": "TE/1.0"})
            data = _j2.loads(urllib.request.urlopen(req, timeout=10).read())
            markets = data if isinstance(data, list) else data.get("data", [])
            count = 0
            for m in markets:
                q = str(m.get("question") or "")
                if "Bitcoin" not in q and "BTC" not in q:
                    continue
                cid = m.get("condition_id")
                if not cid: continue
                self._redeem_queue[cid] = {
                    "condition_id": cid,
                    "eligible_ts_ms": 0,
                    "enqueued_at_ms": int(__import__("time").time() * 1000),
                }
                count += 1
            logger.info("auto_redeem: startup enqueued %d BTC markets for redeem", count)
        except Exception as e:
            logger.warning("scan_historical_redeem failed: %s", e)

    def _start_auto_redeem_worker(self) -> None:
        if self._auto_redeem_thread is not None:
            return
        self._auto_redeem_thread = threading.Thread(
            target=self._auto_redeem_loop,
            name="AutoRedeemWorker",
            daemon=True,
        )
        self._auto_redeem_thread.start()

    def _auto_redeem_loop(self) -> None:
        logger.info("auto_redeem worker started")
        interval = max(5.0, float(self.cfg.runtime.auto_redeem_interval_sec))
        while not self._stopping.is_set():
            self._auto_redeem_once()
            self._stopping.wait(interval)

    def _auto_redeem_once(self) -> None:
        if self.poly_client is None:
            return
        now_ms = int(time.time() * 1000)
        targets: list[dict] = []
        with self._redeem_lock:
            for _cid, item in self._redeem_queue.items():
                if int(item.get("eligible_ts_ms") or 0) <= now_ms:
                    targets.append(dict(item))
            self._redeem_status["pending_n"] = len(self._redeem_queue)
            self._persist_auto_redeem_status()
        for it in targets:
            cid = str(it.get("condition_id") or "")
            if not cid:
                continue
            # 直接尝试赎回, CTF 合约自行判断 (不预检 position API)
            ok, reason, tx_hash = self.poly_client.redeem_positions(condition_id=cid)
            if self.store is not None:
                self.store.append_audit("auto_redeem_attempt", {
                    "condition_id": cid,
                    "ok": bool(ok),
                    "reason": reason,
                    "tx_hash": tx_hash,
                    "attempt": int(it.get("attempts") or 0) + 1,
                })
            with self._redeem_lock:
                self._redeem_status["last_attempt_ts_ms"] = now_ms
                if ok and tx_hash:
                    hashes = list(self._redeem_status.get("recent_tx_hashes") or [])
                    hashes.insert(0, str(tx_hash))
                    self._redeem_status["recent_tx_hashes"] = hashes[:20]
                    self._redeem_status["last_success_ts_ms"] = now_ms
                if not ok:
                    fr = dict(self._redeem_status.get("failure_reasons") or {})
                    fr[reason] = int(fr.get(reason, 0)) + 1
                    self._redeem_status["failure_reasons"] = fr
                self._persist_auto_redeem_status()
            if self.alerting is not None:
                self.alerting.alert(
                    "info" if ok else "warn",
                    "auto_redeem_ok" if ok else "auto_redeem_failed",
                    {"condition_id": cid, "reason": reason, "tx_hash": tx_hash},
                )
            with self._redeem_lock:
                q = self._redeem_queue.get(cid)
                if q is None:
                    continue
                if ok:
                    self._redeem_queue.pop(cid, None)
                else:
                    q["attempts"] = int(q.get("attempts") or 0) + 1
                    if q["attempts"] >= 8:
                        self._redeem_queue.pop(cid, None)
                self._redeem_status["pending_n"] = len(self._redeem_queue)
                self._persist_auto_redeem_status()

    def _has_redeemable_position(self, *, condition_id: str) -> bool:
        """赎回前先查该 condition 是否仍有仓位，避免空仓反复触发链上失败。"""
        if self.poly_client is None:
            return False
        try:
            rows = self.poly_client.fetch_market_positions(condition_id=condition_id)
        except Exception as e:
            logger.warning("fetch_market_positions before redeem failed cid=%s err=%s", condition_id, e)
            return True
        if not rows:
            return False
        for r in rows:
            try:
                sz = float(r.get("size") or r.get("size_shares") or 0.0)
            except Exception:
                sz = 0.0
            if sz > 1e-9:
                return True
        return False

    def _persist_auto_redeem_status(self) -> None:
        if self.store is None:
            return
        self.store.put("auto_redeem.status", dict(self._redeem_status))

    def _on_reconcile_circuit_trip(self, kind: str) -> None:
        if self.poly_client is not None:
            self.poly_client._trip_circuit(f"reconcile:{kind}")
        set_halted(f"reconcile_circuit:{kind}")
        if self.alerting is not None:
            self.alerting.alert("error", "reconcile_circuit_trip", {"kind": kind})

    def _metrics_provider(self) -> dict:
        out: dict = {}
        try:
            if self.clock is not None:
                out["clock"] = self.clock.snapshot()
            if self.feed is not None:
                out["feed"] = self.feed.snapshot()
            if self.poly_feed is not None:
                out["poly_feed"] = self.poly_feed.snapshot()
            if self.market_resolver is not None:
                out["market"] = self.market_resolver.snapshot()
            if self.position_lock is not None:
                out["position_lock"] = self.position_lock.snapshot()
            if self.reconciler is not None:
                out["reconciler"] = self.reconciler.snapshot()
            if self.poly_client is not None:
                out["polymarket"] = self.poly_client.snapshot()
            if self.shadow_engine is not None:
                out["shadow_engine"] = self.shadow_engine.snapshot()
        except Exception as e:
            out["metrics_error"] = str(e)
        return out

    @staticmethod
    def _aggressive_buy_limit_price(
        *,
        best_ask: float,
        shares: float,
        equity: float,
        cross_ticks: int,
        price_tick: float,
        price_max: float,
    ) -> float:
        """在 best_ask 上略抬价（跨若干 tick），并用余额缓冲封顶，提高吃单概率。"""
        tick = max(float(price_tick), 0.0001)
        ba = float(best_ask)
        mx = float(price_max)
        ct = max(0, int(cross_ticks))
        ideal = min(ba + ct * tick, mx)
        ideal = round(ideal, 4)
        buf = float(equity) * 0.995
        sh = max(float(shares), 1e-9)
        if ideal * sh > buf + 1e-9:
            cap = buf / sh
            floored = math.floor(cap / tick) * tick
            ideal = min(ideal, floored)
        if ideal + 1e-12 < ba:
            ideal = ba
        ideal = min(max(ideal, ba), mx)
        return float(round(ideal, 4))

    @staticmethod
    def _runtime_path(*parts: str) -> str:
        root = os.environ.get("TWINENGINES_ROOT") or os.getcwd()
        return os.path.join(root, *parts)

    # ---------------- 业务接口 ----------------

    def _reconcile_window_order_flags_from_exchange(self) -> None:
        """Compatibility hook for legacy persisted window-order flags."""
        return

    def _clear_window_order_flag(self, *, window_id: str, clear_window_done: bool = False) -> None:
        """Compatibility hook for legacy window-order flags; current FOK state is in memory."""
        return

    def submit_signal_order(
        self,
        *,
        window_id: str,
        side: str,                  # 策略侧标签，当前主链路使用 DIRECTION
        direction: str,             # "up" / "down" (5min 合约方向)
        size_quote_usdc: float,
        limit_price: float,
        note: str = "",
        fixed_size_shares: Optional[float] = None,
        fixed_client_order_id: Optional[str] = None,
        audit_context: Optional[dict[str, Any]] = None,
    ) -> Optional[OrderTicket]:
        """实盘下单主入口: FOK 限价 + 最低手数合规。窗口状态机可传入固定价格/固定股数。

        流程要点:
            - 凯利金额经 order_compliance 与平台最低 5 股 / $1 对齐;
            - FOK 订单立即成交或取消。
        """
        if self.position_lock is None or self.poly_client is None or self.market_resolver is None:
            logger.error("submit_signal_order: runner not initialized")
            return None

        rt = self.cfg.runtime
        plat = POLYMARKET_PLATFORM
        active = self.market_resolver.get_active()
        if active is None:
            logger.warning("submit_signal_order: no active market; window_id=%s", window_id)
            return None

        token_id = active.token_id_yes if direction.lower() == "up" else active.token_id_no
        book = self.poly_client.fetch_book(token_id)
        if book.get("stale") or book.get("best_ask") is None:
            logger.warning("submit_signal_order: book stale or no best_ask window_id=%s", window_id)
            return None
        best_ask = float(book["best_ask"])
        now_ms = int(time.time() * 1000)
        age_sec = (now_ms - int(book.get("ts_ms") or now_ms)) / 1000.0
        if age_sec > float(rt.book_max_staleness_sec):
            logger.warning(
                "submit_signal_order: book too stale age=%.2fs window_id=%s", age_sec, window_id,
            )
            return None

        eq = self.poly_client.fetch_account_equity_usdc()
        if eq is None:
            logger.warning("submit_signal_order: equity unavailable window_id=%s", window_id)
            return None

        if fixed_size_shares is not None and float(fixed_size_shares) > 0:
            target_shares = float(fixed_size_shares)
            target_quote = float(target_shares) * float(limit_price)
            if target_shares + 1e-9 < float(plat.min_limit_order_shares):
                logger.info(
                    "submit_signal_order: remaining shares below min window_id=%s shares=%.4f",
                    window_id, target_shares,
                )
                return None
            if target_quote + 1e-9 < float(plat.min_order_quote_usdc):
                logger.info(
                    "submit_signal_order: remaining quote below min window_id=%s quote=%.4f",
                    window_id, target_quote,
                )
                return None
            if target_quote > float(eq) * 0.995 + 1e-9:
                logger.warning("submit_signal_order: insufficient equity for fixed remainder window_id=%s", window_id)
                return None
            comp_size_quote = target_quote
            comp_size_shares = target_shares
            entry_px = float(limit_price)
        else:
            comp = compute_compliant_limit_buy(
                kelly_quote_usdc=float(size_quote_usdc),
                best_ask=best_ask,
                available_balance_usdc=float(eq),
                min_shares=float(plat.min_limit_order_shares),
                min_quote_usdc=float(plat.min_order_quote_usdc),
            )
            if comp is None:
                logger.info(
                    "submit_signal_order: compliance skip (cannot meet min shares/quote vs balance) window_id=%s",
                    window_id,
                )
                if self.store is not None:
                    self.store.append_audit("order_compliance_skip", {"window_id": window_id, "best_ask": best_ask})
                return None

            entry_px = self._aggressive_buy_limit_price(
                best_ask=best_ask,
                shares=float(comp.size_shares),
                equity=float(eq),
                cross_ticks=int(rt.entry_buy_cross_ticks),
                price_tick=float(plat.price_tick),
                price_max=float(plat.price_extreme_max),
            )
            comp_size_quote = float(comp.size_quote_usdc)
            comp_size_shares = float(comp.size_shares)

        client_order_id = fixed_client_order_id or f"{window_id}:{side}:{uuid.uuid4().hex[:8]}"
        audit_context = dict(audit_context or {})
        if not self.position_lock.try_acquire(window_id, side=side, client_order_id=client_order_id, note=note):
            logger.warning("submit_signal_order: window lock busy window_id=%s", window_id)
            return None

        try:
            ticket = self.poly_client.submit_order(
                side="BUY",
                token_id=token_id,
                price=float(entry_px),
                size_quote_usdc=float(comp_size_quote),
                client_order_id=client_order_id,
                size_shares=float(comp_size_shares),
            )
            self.position_lock.attach_order(window_id, client_order_id)

            if ticket.state in (OrderState.FILLED, OrderState.PARTIAL) and float(ticket.filled_size_shares or ticket.size_shares or 0.0) > 0:
                self.poly_client.refresh_order_truth(
                    ticket, delay_ms=int(rt.order_ghost_confirm_delay_ms),
                )
                if self.store is not None:
                    self.store.append_audit("order_filled", {
                        "window_id": window_id,
                        "side": side,
                        "direction": direction,
                        "size_usdc": ticket.size_quote_usdc,
                        "price": ticket.price,
                        "exchange_order_id": ticket.exchange_order_id,
                        "client_order_id": client_order_id,
                        "ref_limit_price": float(limit_price),
                        "state": ticket.state.value,
                        **audit_context,
                    })
                if self.exit_guard is not None:
                    try:
                        active_market = self.market_resolver.get_active() if self.market_resolver is not None else None
                        market_end_ts_ms = int(active_market.end_ts_ms) if active_market is not None else int(time.time() * 1000)
                        self.exit_guard.register_entry(
                            window_id=window_id,
                            direction=direction,
                            token_id=token_id,
                            entry_price=float(ticket.price),
                            entry_cost_usdc=float(ticket.size_quote_usdc),
                            entry_shares=float(ticket.filled_size_shares or ticket.size_shares or 0.0),
                            market_end_ts_ms=market_end_ts_ms,
                            client_order_id=client_order_id,
                        )
                    except Exception as e:
                        logger.warning("exit_guard register_entry failed window_id=%s err=%s", window_id, e)
                if self.alerting is not None:
                    self.alerting.alert("info", "order_filled", {
                        "window_id": window_id,
                        "side": side,
                        "direction": direction,
                        "size": ticket.size_quote_usdc,
                        "price": ticket.price,
                    })
                if self.position_lock is not None:
                    self.position_lock.release(window_id, reason="sync_filled")
            elif ticket.state == OrderState.DRY_RUN_SHADOW:
                if self.store is not None:
                    extra: dict = {}
                    try:
                        eqv = self.store.get(KEY_LAST_EQUITY, default=None)
                        if isinstance(eqv, (int, float)):
                            extra["equity_usdc"] = float(eqv)
                        rg = load_regime(self.store)
                        if rg and isinstance(rg.get("name"), str):
                            extra["regime_name"] = str(rg["name"])
                    except Exception:
                        pass
                    self.store.append_audit("shadow_order", {
                        "window_id": window_id,
                        "side": side,
                        "size_usdc": ticket.size_quote_usdc,
                        "price": ticket.price,
                        "best_ask": best_ask,
                        "ref_limit_price": float(limit_price),
                        **extra,
                    })
                if self.position_lock is not None:
                    self.position_lock.release(window_id, reason="dry_run_shadow")
            else:
                self.position_lock.release(window_id, reason=f"order_{ticket.state.value}")
                self._clear_window_order_flag(window_id=window_id, clear_window_done=False)
                if self.store is not None:
                    self.store.append_audit("order_failed", {
                        "window_id": window_id,
                        "side": side,
                        "direction": direction,
                        "size_usdc": ticket.size_quote_usdc,
                        "price": ticket.price,
                        "exchange_order_id": ticket.exchange_order_id,
                        "client_order_id": client_order_id,
                        "state": ticket.state.value,
                        "error": ticket.last_error,
                        **audit_context,
                    })
                if self.alerting is not None and ticket.state in (
                    OrderState.REJECTED, OrderState.CANCELLED, OrderState.TIMEOUT,
                ):
                    self.alerting.alert("warn", f"order_{ticket.state.value.lower()}", {
                        "window_id": window_id, "error": ticket.last_error,
                    })
            return ticket
        except Exception as e:
            logger.exception("submit_signal_order crashed: %s", e)
            if self.position_lock is not None:
                self.position_lock.release(window_id, reason="submit_crash")
            fail_ticket = OrderTicket(
                client_order_id=fixed_client_order_id or f"{window_id}:{side}:submit_crash",
                side="BUY",
                token_id=token_id,
                price=float(limit_price),
                size_quote_usdc=float(size_quote_usdc),
                state=OrderState.TIMEOUT,
                last_error=f"submit_signal_exception:{str(e)[:160]}",
            )
            return fail_ticket

    def settle_window(self, window_id: str, *, reason: str = "expired") -> None:
        if self.position_lock is None:
            return
        self.position_lock.release(window_id, reason=reason)
        if self.reconciler is not None:
            self.reconciler.clear_local_position(window_id)

    def _enrich_shadow_signal_book(self, event: dict) -> None:
        if self.poly_client is None:
            event["polymarket"] = {"error": "poly_client_unavailable"}
            return
        active = self.market_resolver.get_active() if self.market_resolver else None
        if active is None:
            event["polymarket"] = {"error": "no_active_market"}
            return
        from .shadow_signal_enrich import enrich_shadow_event_with_polymarket
        rt = self.cfg.runtime
        enrich_shadow_event_with_polymarket(
            event, poly_client=self.poly_client, active_market=active,
            stake_quote_usdc=float(getattr(rt, "shadow_book_check_stake_usdc", 5.0)),
            book_max_staleness_sec=float(getattr(rt, "book_max_staleness_sec", 5.0)),
        )

