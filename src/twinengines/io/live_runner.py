"""
实盘主循环 (live_runner): 把所有 io 子模块拼成一个完整运行体。

模块化职责:
    1. ClockSync         同步本地时钟 → 给所有时间相关判断用
    2. BinanceFeed       BTC 实时 1s K 线 → 给信号层用
    3. MarketResolver    Polymarket 5min BTC condition_id → 给下单用
    4. PolymarketClient  下单 / 行情 / 余额查询
    5. PositionLock      短期门控 (防并发撞单; GTC 挂单注册后即释放以支持同窗多笔)
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
from typing import Optional

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
from .reconciliation import LocalPosition, Reconciler, ReconcilerCfg
from .shadow_signal_enrich import enrich_shadow_event_with_polymarket
from .shadow_signal_engine import ShadowSignalEngine
from .single_instance import SingleInstanceLock
from .state_store import KEY_LAST_EQUITY, StateStore, load_regime
from ..risk.limits import RiskCfg, RiskGuard
from ..risk.regime import RegimeManager, RegimeBands, SEED_PRESET, GROWTH_PRESET
from ..risk.sizing import SizingCfg, stake_for_trade

logger = get_logger(__name__)

# 模块级状态 (跨回调保持)
_SIM_FILLED: set[str] = set()        # 影子盘窗口锁仓
_REAL_FILLED: set[str] = set()       # 真实盘窗口锁仓 (独立于影子)
_SIM_CURRENT: dict = {}
_SIM_WIN_BUDGET: dict[str, float] = {}  # 影子盘每窗口剩余 Kelly 预算
_SIM_WIN_DIR: dict[str, str] = {}       # 影子盘每窗口首次成交方向
_REAL_WIN_BUDGET: dict[str, float] = {} # 真实盘每窗口剩余 Kelly 预算 (独立于影子)
_REAL_WIN_DIR: dict[str, str] = {}      # 真实盘每窗口首次成交方向

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
    disable_trend: bool = False
    disable_reversal: Optional[bool] = None


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
    reconciler: Optional[Reconciler] = None
    alerting: Optional[AlertingDispatcher] = None
    store: Optional[StateStore] = None
    instance_lock: Optional[SingleInstanceLock] = None
    shadow_engine: Optional[ShadowSignalEngine] = None
    regime_manager: Optional[RegimeManager] = None
    risk_guard: Optional[RiskGuard] = None
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
    _open_gtc_orders: dict[str, dict] = field(default_factory=dict)
    _order_watch_thread: Optional[threading.Thread] = None
    _window_order_flags: dict[str, dict] = field(default_factory=dict)
    _window_order_flags_lock: threading.RLock = field(default_factory=threading.RLock)
    _window_gtc_timeout_streak: dict[str, int] = field(default_factory=dict)
    _cleanup_done: bool = False

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
        disable_trend: bool = False,
        disable_reversal: Optional[bool] = None,
    ) -> "LiveRunner":
        runtime = load_polymarket_runtime_cfg(env_file=env_file)
        cfg = LiveRunnerCfg(
            runtime=runtime,
            env_file=env_file,
            dry_run_signals=dry_run_signals,
            record_shadow_signals=bool(record_shadow_signals),
            artifact_path=artifact_path,
            shadow_signal_log_path=shadow_signal_log_path,
            disable_trend=bool(disable_trend),
            disable_reversal=disable_reversal,
        )
        return cls(cfg=cfg)

    # ---------------- 启动 ----------------

    def start(self, *, run_forever: bool = True) -> bool:
        if self._started:
            logger.warning("LiveRunner already started")
            return True

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
        self._restore_window_order_flags()

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
                logger.error("Real mode requires AUTO_REDEEM_ENABLED=true to prevent capital lock.")
                self.alerting.alert("error", "auto_redeem_required", {
                    "reason": "AUTO_REDEEM_ENABLED=false",
                })
                self._abort("auto_redeem_required")
                return _abort_start_cleanup()
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
                observe_only=bool(self.cfg.dry_run_signals or self.cfg.record_shadow_signals),
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

        # 风控: Regime + RiskGuard (启动时先用 ULTRA 参数, 后续随权益动态切换)
        self.regime_manager = RegimeManager()
        self.risk_guard = RiskGuard(cfg=self.regime_manager.ultra.risk_cfg)

        # signal handlers
        self._install_signal_handlers()
        self._start_order_watch_worker()

        self._started = True
        LiveRunner._last_instance = self  # WebUI 余额查询
        logger.info("===== LiveRunner started =====")

        if run_forever:
            self._wait_loop()
        return True

    # ---------------- 主等待循环 ----------------

    _monitor_positions: dict[str, dict] = field(default_factory=dict)  # 0.99 平仓追踪

    def _wait_loop(self) -> None:
        logger.info("LiveRunner main loop entered (sleep-based, decisions are event-driven via on_bar)")
        try:
            while not self._stopping.is_set():
                time.sleep(1.0)
                if self.poly_client and self.cfg.runtime.enable_real_orders:
                    self._auto_settle_99_check()
                    self._write_real_balance()
        finally:
            self.stop()

    def _auto_settle_99_check(self) -> None:
        """0.99 平仓: 持仓 token best_bid >= 0.99 则卖出, 跳过赎回."""
        if not self.market_resolver:
            return
        active = self.market_resolver.get_active()
        if not active or not self._monitor_positions:
            return
        for window_id, pos in list(self._monitor_positions.items()):
            token_id = pos.get("token_id")
            if not token_id:
                continue
            try:
                book = self.poly_client.fetch_book(token_id)
                best_bid = float(book.get("best_bid") or 0)
                if best_bid >= 0.99:
                    size = float(book.get("best_bid_size") or 0)
                    amount = pos.get("amount", 0)  # number of shares
                    sell_sz = min(amount, size)
                    if sell_sz > 0:
                        self.submit_signal_order(
                            window_id=window_id, side="REVERSAL", direction=pos.get("dir", ""),
                            size_quote_usdc=sell_sz * best_bid, limit_price=best_bid,
                            note="0.99_auto_settle")
                        logger.info("0.99 auto-settle: sold window=%s token=%s sz=%.2f bid=%.2f",
                                    window_id, token_id, sell_sz, best_bid)
                    self._monitor_positions.pop(window_id, None)
            except Exception:
                pass

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
            os.makedirs("/root/TwinEngines/data_runtime", exist_ok=True)
            open("/root/TwinEngines/data_runtime/real_balance.json", "w").write(
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
        if self._order_watch_thread is not None:
            self._order_watch_thread.join(timeout=2.0)

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
        global _sim_equity
        # 启动时从文件恢复权益（避免重启重置为 $20）
        try:
            eq_path = "/root/TwinEngines/data_runtime/sim_equity.txt"
            if os.path.exists(eq_path):
                saved = float(open(eq_path).read().strip())
                if saved > 0:
                    _sim_equity = saved
                    logger.info("_sim_equity restored from file: %.2f", _sim_equity)
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
                disable_trend=bool(self.cfg.disable_trend),
                disable_reversal=self.cfg.disable_reversal,
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
            "disable_trend": bool(engine.cfg.disable_trend),
            "disable_reversal": bool(engine.cfg.disable_reversal),
            "baseline_reversal_prob": float(engine.baseline_reversal_prob),
            "thresholds": {
                "trend_reversal_prob_max": float(engine.thresholds.trend_reversal_prob_max),
                "trend_signal_stability_sec_min": int(engine.thresholds.trend_signal_stability_sec_min),
                "reversal_baseline_offset_min": float(engine.thresholds.reversal_baseline_offset_min),
                "reversal_signal_stability_sec_min": int(engine.thresholds.reversal_signal_stability_sec_min),
                "reversal_prob_min": float(engine.thresholds.reversal_prob_min),
                "reversal_edge_vs_baseline_min": float(engine.thresholds.reversal_edge_vs_baseline_min),
                "trend_min_expected_value": float(engine.thresholds.trend_min_expected_value),
            },
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
        global _SIM_FILLED, _REAL_FILLED, _SIM_CURRENT, _SIM_WIN_BUDGET, _SIM_WIN_DIR, _REAL_WIN_BUDGET, _REAL_WIN_DIR
        _SIM_FILLED.discard(window_id)
        _REAL_FILLED.discard(window_id)
        _SIM_WIN_BUDGET.pop(window_id, None)
        _SIM_WIN_DIR.pop(window_id, None)
        _REAL_WIN_BUDGET.pop(window_id, None)
        _REAL_WIN_DIR.pop(window_id, None)
        _SIM_CURRENT.clear()
        try:
            import json as _j, urllib.request as _u, os as _o
            ws = int(window_id.replace("w", ""))
            url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&startTime=" + str(ws) + "&limit=5"
            data = _j.loads(_u.urlopen(_u.Request(url, headers={"User-Agent": "TE/1.0"}), timeout=8).read())
            if len(data) >= 5:
                bl = float(data[0][1]); seq = "".join("1" if float(k[4]) > bl else "0" for k in data)
                actual_dir = "up" if seq[4] == "1" else "down"
                # 汇总本窗口所有 partial fills
                best_dir = ""; total_fill = 0.0; total_pnl = 0.0; first_sec = 0
                fills = []  # [(fill_amt, fill_ask)]
                p = "/root/TwinEngines/logs/shadow_orders.jsonl"
                if _o.path.exists(p):
                    for line in open(p).readlines()[-500:]:
                        if window_id not in line:
                            continue
                        try:
                            o = _j.loads(line.strip())
                            s = str(o.get("status") or "").lower()
                            if s == "filled" and o.get("fill_amount", 0) > 0:
                                fa = float(o.get("fill_amount") or 0)
                                if not best_dir:
                                    best_dir = o.get("best_dir", "")
                                    first_sec = int(300 - float(o.get("T_remaining") or 0))
                                # 从盘口价格反推买入价
                                if o.get("best_dir") == "up":
                                    fask = float(o.get("ask_up") or 0)
                                else:
                                    fask = float(o.get("ask_down") or 0)
                                fills.append((fa, fask))
                        except Exception:
                            pass
                if not fills:
                    return
                # 汇总 PnL: 每笔 fill 独立计算盈亏（不同价位可能不同）
                total_fill = sum(f[0] for f in fills)
                for fa, fask in fills:
                    won = actual_dir == best_dir if best_dir else False
                    if won and fask > 0:
                        total_pnl += fa * (1.0 / fask - 1.0)
                    elif won:
                        total_pnl += fa * 0.50
                    else:
                        total_pnl += -fa
                # 加权平均买入价
                avg_ask = sum(f[0] * f[1] for f in fills) / total_fill if total_fill > 0 else 0
                # 从最近一条结算记录推算权益
                last_eq = self._sim_equity
                try:
                    p2 = "/root/TwinEngines/logs/window_results.jsonl"
                    if _o.path.exists(p2):
                        tail = open(p2, "r").readlines()[-1:]
                        if tail:
                            last = _j.loads(tail[0].strip())
                            last_eq = float(last.get("equity") or last_eq)
                except Exception:
                    pass
                self._sim_equity = last_eq + total_pnl
                is_real = not (self.cfg.dry_run_signals or self.cfg.record_shadow_signals)
                res = _j.dumps({
                    "window_id": window_id, "seq": seq, "dir": best_dir,
                    "won": won, "pnl": round(total_pnl, 2),
                    "equity": round(self._sim_equity, 2),
                    "ask": round(avg_ask, 4), "fill_amt": round(total_fill, 2),
                    "fill_sec": first_sec, "partials": len(fills),
                    "mode": "real" if is_real else "shadow",
                })
                # 确保文件尾有换行符, 防止 JSON 粘连
                with open("/root/TwinEngines/logs/window_results.jsonl", "ab+") as _f:
                    _f.seek(0, 2)  # SEEK_END
                    pos = _f.tell()
                    if pos > 0:
                        _f.seek(pos - 1)
                        if _f.read(1) != b"\n":
                            _f.write(b"\n")
                    _f.write((res + "\n").encode("utf-8"))
                # 真实盘单独写一份 (供 WebUI 显示)
                if is_real:
                    with open("/root/TwinEngines/logs/real_results.jsonl", "ab+") as _rf:
                        _rf.seek(0, 2)
                        if _rf.tell() > 0:
                            _rf.seek(_rf.tell() - 1)
                            if _rf.read(1) != b"\n":
                                _rf.write(b"\n")
                        _rf.write((res + "\n").encode("utf-8"))
                open("/root/TwinEngines/data_runtime/sim_equity.txt", "w").write(str(round(self._sim_equity, 2)) + chr(10))
        except Exception:
            pass

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

    def _append_shadow_signal_jsonl(self, event: dict) -> None:
        path = self.cfg.shadow_signal_log_path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, default=str)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")




    def _simulate_order_from_signal(self, event: dict) -> None:
        global _SIM_FILLED, _REAL_FILLED, _SIM_CURRENT
        global _SIM_WIN_BUDGET, _SIM_WIN_DIR, _REAL_WIN_BUDGET, _REAL_WIN_DIR
        window_id = str(event.get("window_id") or "")
        # 真实盘是否活跃
        is_real_mode = (not self.cfg.dry_run_signals and not self.cfg.record_shadow_signals
                        and self.poly_client and self.market_resolver
                        and self.cfg.runtime.enable_real_orders)
        # 影子盘始终用自己的状态 (不受实盘影响)
        filled_set = _SIM_FILLED   # 影子盘锁仓
        win_budget = _SIM_WIN_BUDGET  # 影子盘预算
        win_dir = _SIM_WIN_DIR     # 影子盘方向
        p_rev = float(event.get("p_rev_lower") or event.get("p_rev") or 0.3)
        t_rem = float(event.get("t_remaining_sec") or 0)
        trig = event.get("trigger_pattern", "")
        p_adj = float(event.get("p_rev") or 0.3)
        d_abs = float(event.get("d_abs_pct") or 0)
        td = event.get("trigger_direction", "")

        PREFIX_D_CLIFF = {"000":0.0284,"001":0.0169,"010":0.0187,"011":0.0228,
                          "100":0.0232,"101":0.0191,"110":0.0172,"111":0.0317}
        PREFIX_P_MIN  = {"000":0.344,"001":0.427,"010":0.386,"011":0.377,
                          "100":0.383,"101":0.420,"110":0.442,"111":0.345}
        key = trig[:3] if len(trig) >= 3 else ""
        d_cliff = PREFIX_D_CLIFF.get(key, 0.03)
        p_min_r = PREFIX_P_MIN.get(key, 0.25)

        # 基础状态更新
        _SIM_CURRENT.update({"window_id": window_id, "prefix": trig, "T": round(t_rem, 0),
                             "p_adj": round(p_adj, 3), "p_lower": round(p_rev, 3),
                             "d_abs": round(d_abs, 4), "td": td,
                             "d_cliff": d_cliff, "p_min_r": p_min_r})
        try:
            import json as _j, os as _o
            _o.makedirs("/root/TwinEngines/data_runtime", exist_ok=True)
            with open("/root/TwinEngines/data_runtime/current_window.json", "w") as _cw:
                _cw.write(_j.dumps(_SIM_CURRENT, default=str))
        except: pass

        if not window_id or t_rem < 5:
            _SIM_CURRENT["status"] = "T<5s"; return

        # 预填两个方向的 EV
        try:
            _, _, ask_up, ask_down = self._resolve_best_direction_by_ev(event, window_id)
        except:
            ask_up, ask_down = None, None
        _SIM_CURRENT["ask_up"] = ask_up; _SIM_CURRENT["ask_down"] = ask_down
        if ask_up is not None and ask_down is not None:
            if td == "up":
                _SIM_CURRENT["ev_rev"] = round(self._calc_ev(p_rev, ask_down), 4)
                _SIM_CURRENT["ev_trend"] = round(self._calc_ev(1 - p_rev, ask_up), 4)
                _SIM_CURRENT["r_d_ok"] = (d_abs <= d_cliff)
                _SIM_CURRENT["r_p_ok"] = (p_rev >= p_min_r)
            else:
                _SIM_CURRENT["ev_rev"] = round(self._calc_ev(p_rev, ask_up), 4)
                _SIM_CURRENT["ev_trend"] = round(self._calc_ev(1 - p_rev, ask_down), 4)
                _SIM_CURRENT["r_d_ok"] = (d_abs <= d_cliff)
                _SIM_CURRENT["r_p_ok"] = (p_rev >= p_min_r)

        ev_min = 0.05 if t_rem > 80 else (0.03 if t_rem > 30 else 0.02)
        try:
            best_dir, best_ev, ask_up, ask_down = self._resolve_best_direction_by_ev(event, window_id)
        except:
            _SIM_CURRENT["status"] = "EV err"; return

        if best_dir is None:
            _SIM_CURRENT["status"] = "EV neg"; return

        # 检查本窗是否还有剩余预算 (完全锁仓则跳过)
        if window_id in filled_set:
            _SIM_CURRENT["status"] = "filled"; return

        # 反向信号锁仓: 已有仓位但新信号方向相反 → 立即锁仓
        if window_id in win_dir and best_dir != win_dir[window_id]:
            filled_set.add(window_id)
            _SIM_CURRENT["status"] = "locked_reverse"
            _SIM_CURRENT["best_dir"] = best_dir
            self._write_sim_record(window_id, trig, p_adj, p_rev, t_rem, ask_up, ask_down, best_dir, best_ev, 0,
                                   "rejected", f"reverse_lock:{best_dir}vs{win_dir[window_id]}", d_abs)
            return

        # 反转方向: d + p 双重过滤，被拒则记录
        is_reversal = (best_dir != event.get("trigger_direction", ""))
        reject_reason = ""
        if is_reversal:
            if d_abs > d_cliff:
                reject_reason = f"R:d>{d_cliff:.3f}"
            elif p_rev < p_min_r:
                reject_reason = f"R:p<{p_min_r:.3f}"
        if reject_reason:
            _SIM_CURRENT["status"] = reject_reason
            _SIM_CURRENT["best_dir"] = best_dir
            self._write_sim_record(window_id, trig, p_adj, p_rev, t_rem, ask_up, ask_down, best_dir, best_ev, 0, "rejected", reject_reason, d_abs)
            return

        if best_ev < ev_min:
            _SIM_CURRENT["status"] = f"EV<{ev_min}"
            _SIM_CURRENT["best_dir"] = best_dir
            self._write_sim_record(window_id, trig, p_adj, p_rev, t_rem, ask_up, ask_down, best_dir, best_ev, 0, "rejected", f"EV<{ev_min}", d_abs)
            return

        # Kelly sizing — 首次信号计算总预算, 后续追单用剩余额度
        ask = ask_up if best_dir == "up" else ask_down
        if window_id not in win_budget:
            try:
                # 影子盘用模拟权益, 真实盘用 Polymarket 实际余额
                if is_real_mode and self.poly_client:
                    equity = self.poly_client.fetch_account_equity_usdc() or self._sim_equity
                else:
                    equity = self._sim_equity
                sizing = SizingCfg(kelly_fraction=0.30, max_stake_ratio=0.15, min_absolute_stake=2.50)
                wp = p_rev if is_reversal else (1 - p_rev)
                b = (1 - ask) / ask if ask > 0 else 1
                kelly_total = stake_for_trade(portfolio_equity=equity, win_prob=wp, net_payoff=b, cfg=sizing)
            except:
                kelly_total = 5.0
            if kelly_total < 2.50:
                if equity * 0.15 < 2.50:
                    _SIM_CURRENT["status"] = "Kelly<2.5"
                    _SIM_CURRENT["best_dir"] = best_dir
                    self._write_sim_record(window_id, trig, p_adj, p_rev, t_rem, ask_up, ask_down, best_dir, best_ev, 0, "rejected", "Kelly<2.5", d_abs)
                    return
                kelly_total = 2.50
            win_budget[window_id] = kelly_total
            win_dir[window_id] = best_dir
        else:
            kelly_total = win_budget[window_id]  # 保持不变

        remaining = win_budget.get(window_id, 0)
        if remaining <= 0:
            filled_set.add(window_id)
            _SIM_CURRENT["status"] = "filled"; return

        # FOK + 滑点: 用内置手续费中的滑点预算 (0.005) 作为可接受价差
        poly = event.get("polymarket") or {}
        ask_sz = float(poly.get("best_ask_size_up", 0)) if best_dir == "up" else float(poly.get("best_ask_size_dn", 0))
        slippage_budget = 0.005  # 来自 fee 公式: 0.005 * a * (1-a) * s
        max_price = ask * (1.0 + slippage_budget) if ask > 0 else ask
        depth_cap = min(ask_sz * max_price * 0.8, 200.0) if ask_sz > 0 and ask > 0 else 50.0
        single = min(remaining, max(depth_cap, 2.50))
        if remaining < 2.50:
            single = remaining  # 剩余不足 $2.50 就全下, 不强行拉高
        elif single < 2.50:
            single = 2.50       # 预算够但深度薄, 保底 $2.50

        # 真实盘: FOK 下单 (带防护)
        if is_real_mode:
            # 防反向: 已有持仓但方向相反 → 锁仓
            if window_id in _REAL_WIN_DIR and best_dir != _REAL_WIN_DIR[window_id]:
                _REAL_FILLED.add(window_id)
                self._write_sim_record(window_id, trig, p_adj, p_rev, t_rem, ask_up, ask_down, best_dir, best_ev, 0,
                                       "rejected", f"real_reverse:{best_dir}vs{_REAL_WIN_DIR[window_id]}", d_abs)
                return
            # 防重复: 本窗已成交 → 跳过
            if window_id in _REAL_FILLED:
                return
            # 记录首次方向
            if window_id not in _REAL_WIN_DIR:
                _REAL_WIN_DIR[window_id] = best_dir

            try:
                active = self.market_resolver.get_active()
                if active:
                    token_id = active.token_id_yes if best_dir == "up" else active.token_id_no
                    side_label = "TREND" if not is_reversal else "REVERSAL"
                    limit_px = ask * 1.005 if ask > 0 else ask
                    # 下单前查余额
                    bal_before = self.poly_client.fetch_account_equity_usdc()
                    ticket = self.submit_signal_order(
                        window_id=window_id, side=side_label, direction=best_dir,
                        size_quote_usdc=single, limit_price=round(limit_px, 4),
                        note=f"ev={best_ev:.3f} kelly={kelly_total:.2f}")
                    # 下单后查余额, 对比判断是否成交
                    import time as _tm
                    _tm.sleep(0.5)
                    bal_after = self.poly_client.fetch_account_equity_usdc()
                    if bal_before is not None and bal_after is not None and (bal_before - bal_after) > single * 0.3:
                        _REAL_FILLED.add(window_id)  # 余额减少了 → 成交
                    else:
                        self._write_sim_record(window_id, trig, p_adj, p_rev, t_rem, ask_up, ask_down, best_dir, best_ev, 0,
                                               "rejected", "FOK_rejected", d_abs)
                        return
            except Exception as e:
                logger.warning("real order submit failed: %s", e)
                return

        # 扣减预算 (影子盘直接扣; 真实盘通过 FOK 检查才到这里)
        win_budget[window_id] = remaining - single
        self._write_sim_record(window_id, trig, p_adj, p_rev, t_rem, ask_up, ask_down, best_dir, best_ev, single, "filled", "FILLED", d_abs)

        if win_budget[window_id] <= 0:
            filled_set.add(window_id)
            _SIM_CURRENT["status"] = "FILLED"
        else:
            _SIM_CURRENT["status"] = f"part_fill"
        _SIM_CURRENT["best_dir"] = best_dir
        _SIM_CURRENT["fill_amt"] = round(single, 2)
        _SIM_CURRENT["fill_ask"] = round(ask, 4)
        _SIM_CURRENT["fill_ev"] = round(best_ev, 4)
        _SIM_CURRENT["budget_remain"] = round(win_budget.get(window_id, 0), 2)
        _SIM_CURRENT["budget_total"] = round(kelly_total, 2)

    def _write_sim_record(self, wid, trig, p_adj, p_lower, t_rem, au, ad, best_dir, best_ev, fill_amt, status, reason, d_abs=0):
        try:
            import json as _j, os as _o
            _o.makedirs("logs", exist_ok=True)
            rec = {"window_id": wid, "status": status, "reason": reason,
                   "trigger_pattern": trig, "p_adj": round(p_adj,3), "p_rev_lower": round(p_lower,3),
                   "T_remaining": round(t_rem,0), "ask_up": au, "ask_down": ad,
                   "best_dir": best_dir, "best_ev": round(best_ev,4),
                   "fill_amount": round(fill_amt,2),
                   "d_abs_pct": round(d_abs,4),
                   "ts_ms": int(__import__("time").time() * 1000)}
            with open("logs/shadow_orders.jsonl", "ab+") as _f:
                _f.seek(0, 2); pos = _f.tell()
                if pos > 0:
                    _f.seek(pos - 1)
                    if _f.read(1) != b"\n": _f.write(b"\n")
                _f.write((_j.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
        except: pass


    @staticmethod
    def _calc_ev(win_prob: float, ask: float) -> float:
        return win_prob / ask - 1.0

    def _resolve_best_direction_by_ev(self, event: dict, window_id: str):
        if self.poly_client is None or self.market_resolver is None:
            return None, -1.0, None, None
        active = self.market_resolver.get_active()
        if active is None:
            return None, -1.0, None, None
        p_rev = float(event.get("p_rev_lower") or event.get("p_rev") or 0.3)
        trig_dir = str(event.get("trigger_direction") or "").lower()
        if trig_dir not in ("up", "down"):
            return None, -1.0, None, None
        book_up = self.poly_client.fetch_book(active.token_id_yes)
        book_dn = self.poly_client.fetch_book(active.token_id_no)
        ask_up = float(book_up.get("best_ask") or 0.99)
        ask_dn = float(book_dn.get("best_ask") or 0.99)
        if ask_up <= 0 or ask_up >= 1 or ask_dn <= 0 or ask_dn >= 1:
            return None, -1.0, ask_up, ask_dn
        def fee(a, s=5.0): return 0.018 * a * s + 0.10 + 0.002 * s
        def ev(wp, a, s=5.0): return s * (wp / a - 1.0) - fee(a, s)
        if trig_dir == "up":
            ev_rev = ev(p_rev, ask_dn)
            ev_trend = ev(1-p_rev, ask_up)
        else:
            ev_rev = ev(p_rev, ask_up)
            ev_trend = ev(1-p_rev, ask_dn)
        thr = 0.02
        if ev_rev > thr and ev_rev >= ev_trend:
            return ("down" if trig_dir == "up" else "up"), ev_rev, ask_up, ask_dn
        elif ev_trend > thr:
            return trig_dir, ev_trend, ask_up, ask_dn
        return None, max(ev_rev, ev_trend), ask_up, ask_dn


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
        self._cancel_expired_gtc_orders(now_ts_ms=int(time.time() * 1000))

    def _start_order_watch_worker(self) -> None:
        if self._order_watch_thread is not None:
            return
        self._order_watch_thread = threading.Thread(
            target=self._order_watch_loop,
            name="OrderWatchWorker",
            daemon=True,
        )
        self._order_watch_thread.start()

    def _order_watch_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self._poll_open_gtc_orders()
                self._cancel_expired_gtc_orders(now_ts_ms=int(time.time() * 1000))
            except Exception as e:
                logger.warning("order watch loop failed: %s", e)
            self._stopping.wait(1.0)

    def _cancel_open_gtc_exchange_prefix(self, window_id: str) -> None:
        """撤掉链上仍挂着、client_order_id 属于本窗口的 GTC (runner 本地未跟踪到的挂单)."""
        if self.poly_client is None:
            return
        prefix = f"{window_id}:"
        try:
            open_rows = self.poly_client.fetch_open_orders()
        except Exception as e:
            logger.warning("fetch_open_orders for cancel_prefix failed: %s", e)
            return
        for o in open_rows:
            coid = str(o.get("client_order_id") or o.get("clientOrderId") or "")
            if not coid.startswith(prefix):
                continue
            oid = str(o.get("id") or o.get("orderID") or o.get("orderId") or "")
            if not oid:
                continue
            stub = OrderTicket(
                client_order_id=coid,
                side="BUY",
                token_id=str(o.get("asset_id") or o.get("token_id") or ""),
                price=float(o.get("price") or 0),
                size_quote_usdc=0.0,
                state=OrderState.SUBMITTED,
                exchange_order_id=oid,
            )
            self.poly_client.cancel_order(stub, reason="window_cancel_orphan")

    def _cancel_all_pending_gtc_for_window(self, window_id: str) -> None:
        """新信号前: 取消本窗口在册 GTC + 交易所前缀孤儿单; 若有已成交部分则合并入账并释放门控."""
        if self.poly_client is None:
            return
        meta = self._open_gtc_orders.pop(window_id, None)
        if meta is not None:
            ticket: OrderTicket = meta["ticket"]
            if not ticket.is_terminal():
                self.poly_client.cancel_order(ticket, reason="new_signal_cancel_prior")  # type: ignore[union-attr]
            self.poly_client.refresh_order_truth(ticket, delay_ms=int(self.cfg.runtime.order_ghost_confirm_delay_ms))  # type: ignore[union-attr]
            filled = float(ticket.filled_size_shares or 0.0)
            if filled > 1e-9:
                self._report_gtc_fill_to_reconciler(
                    window_id=window_id,
                    side=str(meta.get("side") or ""),
                    direction=str(meta.get("direction") or ""),
                    token_id=str(meta.get("token_id") or ""),
                    ticket=ticket,
                    note=str(meta.get("note") or ""),
                )
            if self.position_lock is not None:
                self.position_lock.release(window_id, reason="new_signal_cancel_prior")
            self._clear_window_order_flag(window_id=window_id, clear_window_done=False)
        self._cancel_open_gtc_exchange_prefix(window_id)

    def _report_gtc_fill_to_reconciler(
        self,
        *,
        window_id: str,
        side: str,
        direction: str,
        token_id: str,
        ticket: OrderTicket,
        note: str,
    ) -> None:
        if self.reconciler is None:
            return
        px = float(ticket.price)
        prior = float(getattr(ticket, "prior_leg_filled_shares", 0.0) or 0.0)
        cur = float(ticket.filled_size_shares or 0.0)
        total = prior + cur
        if total < 1e-9 and ticket.state == OrderState.FILLED:
            total = float(ticket.size_shares or (ticket.size_quote_usdc / max(px, 0.01)))
        if total < 1e-9:
            return
        chk = float(getattr(ticket, "reconcile_checkpoint_shares", 0.0) or 0.0)
        delta = max(0.0, total - chk)
        if delta < 1e-9:
            return
        self.reconciler.merge_or_report_local_position(LocalPosition(
            window_id=window_id,
            side=side,
            token_id=token_id,
            expected_size_shares=delta,
            expected_avg_price=px,
            opened_ts_ms=int(ticket.submitted_at_ms or time.time() * 1000),
            note=note,
        ))
        ticket.reconcile_checkpoint_shares = total

    def _infer_gtc_expire_ts_ms(self, *, active_market: ActiveMarket) -> int:
        now_ms = int(time.time() * 1000)
        wait_ms = int(max(1.0, float(self.cfg.runtime.gtc_max_wait_sec)) * 1000)
        return min(int(active_market.end_ts_ms), now_ms + wait_ms)

    def _poll_open_gtc_orders(self) -> None:
        if self.poly_client is None:
            return
        rows = list(self._open_gtc_orders.items())
        for window_id, meta in rows:
            ticket: OrderTicket = meta["ticket"]
            self.poly_client.poll_order(ticket)
            if ticket.state == OrderState.FILLED:
                self.poly_client.refresh_order_truth(
                    ticket, delay_ms=int(self.cfg.runtime.order_ghost_confirm_delay_ms),
                )
                self._report_gtc_fill_to_reconciler(
                    window_id=window_id,
                    side=str(meta.get("side") or ""),
                    direction=str(meta.get("direction") or ""),
                    token_id=str(meta.get("token_id") or ""),
                    ticket=ticket,
                    note=str(meta.get("note") or ""),
                )
                if self.position_lock is not None:
                    self.position_lock.release(window_id, reason="gtc_filled")
                self._finalize_gtc_window(window_id, reason="gtc_filled", ticket=ticket)
                continue
            if ticket.state == OrderState.PARTIAL:
                self.poly_client.cancel_order(ticket, reason="gtc_partial_cancel_remaining")
                self.poly_client.refresh_order_truth(
                    ticket, delay_ms=int(self.cfg.runtime.order_ghost_confirm_delay_ms),
                )
                if self.store is not None:
                    self.store.append_audit("gtc_partial_cancelled", {
                        "window_id": window_id,
                        "client_order_id": ticket.client_order_id,
                        "exchange_order_id": ticket.exchange_order_id,
                        "filled_shares": ticket.filled_size_shares,
                    })
                self._report_gtc_fill_to_reconciler(
                    window_id=window_id,
                    side=str(meta.get("side") or ""),
                    direction=str(meta.get("direction") or ""),
                    token_id=str(meta.get("token_id") or ""),
                    ticket=ticket,
                    note=str(meta.get("note") or ""),
                )
                if self.position_lock is not None:
                    self.position_lock.release(window_id, reason="gtc_partial_done")
                self._finalize_gtc_window(window_id, reason="gtc_partial_cancelled", ticket=ticket)
                continue
            if ticket.state in (OrderState.CANCELLED, OrderState.REJECTED, OrderState.TIMEOUT):
                self.poly_client.refresh_order_truth(
                    ticket, delay_ms=int(self.cfg.runtime.order_ghost_confirm_delay_ms),
                )
                if float(ticket.filled_size_shares or 0) > 1e-9:
                    if self.alerting is not None:
                        self.alerting.alert("warn", "gtc_ghost_or_late_fill", {
                            "window_id": window_id,
                            "filled_shares": ticket.filled_size_shares,
                            "state": ticket.state.value,
                        })
                    self._report_gtc_fill_to_reconciler(
                        window_id=window_id,
                        side=str(meta.get("side") or ""),
                        direction=str(meta.get("direction") or ""),
                        token_id=str(meta.get("token_id") or ""),
                        ticket=ticket,
                        note=str(meta.get("note") or ""),
                    )
                if self.position_lock is not None:
                    self.position_lock.release(window_id, reason=f"gtc_{ticket.state.value.lower()}")
                self._finalize_gtc_window(window_id, reason=f"gtc_{ticket.state.value.lower()}", ticket=ticket)

    def _cancel_expired_gtc_orders(self, *, now_ts_ms: int) -> None:
        if self.poly_client is None:
            return
        rows = list(self._open_gtc_orders.items())
        for window_id, meta in rows:
            expire_ts_ms = int(meta.get("expire_ts_ms") or 0)
            if expire_ts_ms > 0 and now_ts_ms >= expire_ts_ms:
                ticket: OrderTicket = meta["ticket"]
                if not ticket.is_terminal():
                    self.poly_client.cancel_order(ticket, reason="gtc_max_wait_or_window_end")
                self.poly_client.refresh_order_truth(
                    ticket, delay_ms=int(self.cfg.runtime.order_ghost_confirm_delay_ms),
                )
                filled = float(ticket.filled_size_shares or 0.0)
                if filled > 1e-9:
                    self._report_gtc_fill_to_reconciler(
                        window_id=window_id,
                        side=str(meta.get("side") or ""),
                        direction=str(meta.get("direction") or ""),
                        token_id=str(meta.get("token_id") or ""),
                        ticket=ticket,
                        note=str(meta.get("note") or ""),
                    )
                if self.position_lock is not None:
                    self.position_lock.release(window_id, reason="gtc_expired_cancel")
                if self.store is not None:
                    self.store.append_audit("gtc_window_timeout_cancelled", {
                        "window_id": window_id,
                        "client_order_id": ticket.client_order_id,
                        "exchange_order_id": ticket.exchange_order_id,
                        "filled_shares": ticket.filled_size_shares,
                    })
                self._finalize_gtc_window(window_id, reason="gtc_window_timeout_cancelled", ticket=ticket)

    def _finalize_gtc_window(self, window_id: str, *, reason: str, ticket: Optional[OrderTicket] = None) -> None:
        self._open_gtc_orders.pop(window_id, None)
        self._clear_window_order_flag(window_id=window_id, clear_window_done=False)
        filled = 0.0
        if ticket is not None:
            filled = float(ticket.filled_size_shares or 0.0)
            if filled < 1e-9 and ticket.state == OrderState.FILLED:
                filled = float(ticket.size_shares or 0.0)
        rlow = reason.lower()
        if filled < 1e-9 and rlow == "gtc_window_timeout_cancelled":
            self._window_gtc_timeout_streak[window_id] = int(self._window_gtc_timeout_streak.get(window_id, 0)) + 1
        elif filled > 1e-9 or "filled" in rlow or "partial" in rlow:
            self._window_gtc_timeout_streak[window_id] = 0
        if self.store is not None:
            self.store.append_audit("gtc_window_finalized", {
                "window_id": window_id,
                "reason": reason,
                "filled_shares": filled,
                "timeout_streak": int(self._window_gtc_timeout_streak.get(window_id, 0)),
            })
        # 注册 0.99 平仓监控 (仅实盘)
        meta = self._open_gtc_orders.get(window_id)
        if filled > 0 and meta and self.cfg.runtime.enable_real_orders:
            self._monitor_positions[window_id] = {
                "token_id": meta.get("token_id"),
                "amount": filled,
                "dir": meta.get("dir", ""),
            }

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
        """启动时扫描 Polymarket 上已有的可赎回持仓并入队.

        最佳实践: 启动时直接调用 CTF redeemPositions 赎回所有已结算持仓,
        不需要逐市场检查. Polymarket 的 redeem_positions 方法传入 condition_id
        和 index_sets 即可赎回该市场所有 winning tokens.
        """
        try:
            if not self.poly_client or not self._redeem_lock:
                return
            # 直接尝试赎回最近活跃过的市场 (从 active market 往回推)
            active = self.market_resolver.get_active() if self.market_resolver else None
            if active:
                self._redeem_queue[active.condition_id] = {
                    "condition_id": active.condition_id,
                    "eligible_ts_ms": int(active.end_ts_ms) + 60_000,  # 结算后 1 分钟
                    "enqueued_at_ms": int(__import__("time").time() * 1000),
                }
                logger.info("auto_redeem: enqueued current market cid=%s", active.condition_id[:16])
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
            has_pos = self._has_redeemable_position(condition_id=cid)
            if not has_pos:
                with self._redeem_lock:
                    self._redeem_queue.pop(cid, None)
                    self._redeem_status["pending_n"] = len(self._redeem_queue)
                    self._persist_auto_redeem_status()
                if self.store is not None:
                    self.store.append_audit("auto_redeem_skipped_no_position", {
                        "condition_id": cid,
                        "attempt": int(it.get("attempts") or 0) + 1,
                    })
                continue
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
        for _wid, meta in list(self._open_gtc_orders.items()):
            t: OrderTicket = meta["ticket"]
            if self.poly_client is not None and not t.is_terminal():
                try:
                    self.poly_client.cancel_order(t, reason=f"reconcile_circuit:{kind}")
                except Exception as e:
                    logger.warning("cancel gtc on reconcile circuit failed: %s", e)
            self._open_gtc_orders.pop(_wid, None)
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

    # ---------------- 业务接口 ----------------

    def submit_signal_order(
        self,
        *,
        window_id: str,
        side: str,                  # "TREND" / "REVERSAL" (策略层信号方向)
        direction: str,             # "up" / "down" (5min 合约方向)
        size_quote_usdc: float,
        limit_price: float,
        note: str = "",
    ) -> Optional[OrderTicket]:
        """实盘下单主入口: GTC 限价 + 盘口 best_ask + 最低手数合规 (策略传入的 limit_price 仅作参考, 实盘以盘口为准).

        流程要点:
            - 同一窗口内新信号会先撤掉未完结 GTC;
            - 凯利金额经 order_compliance 与平台最低 5 股 / $1 对齐;
            - GTC 最长等待 min(GTC_MAX_WAIT_SEC, 至窗口结束);
            - 挂单注册后释放 PositionLock, 允许窗口内多次独立下单。
        """
        if self.position_lock is None or self.poly_client is None or self.market_resolver is None:
            logger.error("submit_signal_order: runner not initialized")
            return None

        rt = self.cfg.runtime
        plat = POLYMARKET_PLATFORM
        max_streak = max(1, int(rt.gtc_window_max_consecutive_timeouts))
        if int(self._window_gtc_timeout_streak.get(window_id, 0)) >= max_streak:
            logger.info(
                "submit_signal_order skipped: gtc_timeout_streak window_id=%s streak=%s",
                window_id, self._window_gtc_timeout_streak.get(window_id, 0),
            )
            if self.store is not None:
                self.store.append_audit("gtc_skip_timeout_streak", {"window_id": window_id})
            return None

        self._cancel_all_pending_gtc_for_window(window_id)

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

        client_order_id = f"{window_id}:{side}:{uuid.uuid4().hex[:8]}"
        if not self.position_lock.try_acquire(window_id, side=side, client_order_id=client_order_id, note=note):
            logger.warning("submit_signal_order: window lock busy window_id=%s", window_id)
            return None

        try:
            ticket = self.poly_client.submit_order(
                side="BUY",
                token_id=token_id,
                price=float(entry_px),
                size_quote_usdc=float(comp.size_quote_usdc),
                client_order_id=client_order_id,
                size_shares=float(comp.size_shares),
            )
            self.position_lock.attach_order(window_id, client_order_id)

            if ticket.state == OrderState.FILLED:
                self.poly_client.refresh_order_truth(
                    ticket, delay_ms=int(rt.order_ghost_confirm_delay_ms),
                )
                self._report_gtc_fill_to_reconciler(
                    window_id=window_id,
                    side=side,
                    direction=direction,
                    token_id=token_id,
                    ticket=ticket,
                    note=note,
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
                    })
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
            elif ticket.order_type == "GTC" and ticket.state in (OrderState.SUBMITTED, OrderState.PARTIAL):
                expire_ts_ms = self._infer_gtc_expire_ts_ms(active_market=active)
                self._mark_window_order_active(
                    window_id=window_id,
                    client_order_id=client_order_id,
                    exchange_order_id=str(ticket.exchange_order_id or ""),
                    expire_ts_ms=expire_ts_ms,
                )
                self._open_gtc_orders[window_id] = {
                    "ticket": ticket,
                    "expire_ts_ms": expire_ts_ms,
                    "side": side,
                    "direction": direction,
                    "dir": direction,
                    "note": note,
                    "token_id": token_id,
                }
                if float(getattr(ticket, "prior_leg_filled_shares", 0.0) or 0.0) > 1e-9:
                    self._report_gtc_fill_to_reconciler(
                        window_id=window_id,
                        side=side,
                        direction=direction,
                        token_id=token_id,
                        ticket=ticket,
                        note=note,
                    )
                if self.store is not None:
                    self.store.append_audit("gtc_submitted", {
                        "window_id": window_id,
                        "client_order_id": client_order_id,
                        "exchange_order_id": ticket.exchange_order_id,
                        "expire_ts_ms": expire_ts_ms,
                        "price": ticket.price,
                        "size_usdc": ticket.size_quote_usdc,
                        "size_shares": comp.size_shares,
                        "best_ask": best_ask,
                        "ref_limit_price": float(limit_price),
                    })
                if self.position_lock is not None:
                    self.position_lock.release(window_id, reason="gtc_registered")
            else:
                self.position_lock.release(window_id, reason=f"order_{ticket.state.value}")
                self._clear_window_order_flag(window_id=window_id, clear_window_done=False)
                if self.store is not None:
                    self.store.append_audit("order_failed", {
                        "window_id": window_id,
                        "state": ticket.state.value,
                        "error": ticket.last_error,
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
            return None

    def settle_window(self, window_id: str, *, reason: str = "expired") -> None:
        ticket: Optional[OrderTicket] = None
        if self.poly_client is not None:
            self._cancel_open_gtc_exchange_prefix(window_id)
            meta = self._open_gtc_orders.get(window_id)
            if meta is not None:
                ticket = meta["ticket"]
                if not ticket.is_terminal():
                    self.poly_client.cancel_order(ticket, reason=f"settle_window:{reason}")
                self.poly_client.refresh_order_truth(
                    ticket, delay_ms=int(self.cfg.runtime.order_ghost_confirm_delay_ms),
                )
                filled = float(ticket.filled_size_shares or 0.0)
                if filled > 1e-9:
                    self._report_gtc_fill_to_reconciler(
                        window_id=window_id,
                        side=str(meta.get("side") or ""),
                        direction=str(meta.get("direction") or ""),
                        token_id=str(meta.get("token_id") or ""),
                        ticket=ticket,
                        note=str(meta.get("note") or ""),
                    )
            self._finalize_gtc_window(window_id, reason=f"settle_window:{reason}", ticket=ticket)
        self._clear_window_order_flag(window_id=window_id, clear_window_done=True)
        self._window_gtc_timeout_streak.pop(window_id, None)
        if self.position_lock is None:
            return
        self.position_lock.release(window_id, reason=reason)
        if self.reconciler is not None:
            self.reconciler.clear_local_position(window_id)

    def _restore_window_order_flags(self) -> None:
        if self.store is None:
            return
        raw = self.store.get("window_order_flags.active", default={})
        if not isinstance(raw, dict):
            return
        with self._window_order_flags_lock:
            self._window_order_flags = {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}

    def _persist_window_order_flags(self) -> None:
        if self.store is None:
            return
        with self._window_order_flags_lock:
            self.store.put("window_order_flags.active", dict(self._window_order_flags))

    def _is_window_order_active(self, window_id: str) -> bool:
        with self._window_order_flags_lock:
            e = self._window_order_flags.get(window_id)
            return bool(e and e.get("active"))

    def _mark_window_order_active(
        self,
        *,
        window_id: str,
        client_order_id: str,
        exchange_order_id: str,
        expire_ts_ms: int,
    ) -> None:
        with self._window_order_flags_lock:
            self._window_order_flags[window_id] = {
                "active": True,
                "window_done": False,
                "client_order_id": client_order_id,
                "exchange_order_id": exchange_order_id,
                "expire_ts_ms": int(expire_ts_ms),
                "updated_ts_ms": int(time.time() * 1000),
            }
        self._persist_window_order_flags()

    def _clear_window_order_flag(self, *, window_id: str, clear_window_done: bool) -> None:
        with self._window_order_flags_lock:
            e = self._window_order_flags.get(window_id)
            if e is None:
                return
            e["active"] = False
            e["updated_ts_ms"] = int(time.time() * 1000)
            if clear_window_done:
                self._window_order_flags.pop(window_id, None)
        self._persist_window_order_flags()

    def _reconcile_window_order_flags_from_exchange(self) -> None:
        if self.poly_client is None:
            return
        open_orders = self.poly_client.fetch_open_orders()
        ids = set()
        for o in open_orders:
            coid = str(o.get("client_order_id") or o.get("clientOrderId") or "")
            if not coid:
                continue
            window_id = coid.split(":", 1)[0]
            if not window_id:
                continue
            ids.add(window_id)
            self._mark_window_order_active(
                window_id=window_id,
                client_order_id=coid,
                exchange_order_id=str(o.get("id") or o.get("orderID") or o.get("orderId") or ""),
                expire_ts_ms=int(time.time() * 1000) + 300_000,
            )
        # 清掉本地残留但链上已无活跃挂单的窗口
        with self._window_order_flags_lock:
            stale = [wid for wid, e in self._window_order_flags.items() if e.get("active") and wid not in ids]
        for wid in stale:
            self._clear_window_order_flag(window_id=wid, clear_window_done=False)
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


