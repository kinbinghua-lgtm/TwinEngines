"""
实盘 IO 层 (live trading I/O layer).

设计原则:
    1. 模块化: 每个外部 IO 关注点 (平台配置 / 行情 / 下单 / 锁 / 持久化 / 告警)
       拆成独立子模块, 便于按需替换或 mock。
    2. 防御性优先: 所有外部调用都带超时、重试、通用兜底。
    3. 不耦合策略: 决策层只看 PriceFeed/AccountSnapshot, 完全不感知 IO 实现。
    4. dry_run: 默认 dry_run=True, 启动时只下"影子单"到日志, 不发链上交易。

子模块:
    config            平台静态参数 + 环境变量加载 + 动态运行时配置
    logging_setup     统一 logging (文件 + stdout, 滚动归档)
    clock_sync        本地时钟与币安服务器时钟同步
    state_store       SQLite 持久化 (RiskGuard / Regime / PositionLock)
    position_lock     实盘"一窗一笔"门控
    single_instance   PID 锁防双开
    binance_feed      实时 WS + REST 兜底 K 线
    market_resolver   Polymarket 5min 合约动态发现
    polymarket_client Polymarket CLOB 下单 / 查询 (GTC 限价为主路径)
    reconciliation    持仓 reconciliation 与熔断
    alerting          告警通道 (Log + File + Telegram)
    health            外部监控/心跳
    live_runner       主循环 (整合上述模块)
"""

from .alerting import AlertingDispatcher, FileSink, LogSink, TelegramSink
from .binance_feed import BinanceFeed, BinanceFeedCfg, KlineBar
from .clock_sync import ClockSync, ClockSyncCfg
from .config import (
    POLYMARKET_PLATFORM,
    PolymarketPlatform,
    PolymarketRuntimeCfg,
    is_real_order_allowed,
    load_polymarket_runtime_cfg,
)
from .health import set_halted, set_metrics_provider, start_health_server
from .live_runner import LiveRunner, LiveRunnerCfg
from .logging_setup import get_logger, setup_logging
from .market_resolver import ActiveMarket, MarketResolver, MarketResolverCfg
from .polymarket_client import OrderState, OrderTicket, PolymarketClient
from .polymarket_feed import BookSnapshot, PolymarketFeed, PolymarketFeedCfg
from .position_lock import PositionLock
from .reconciliation import LocalPosition, Reconciler, ReconcilerCfg
from .shadow_signal_engine import ShadowSignalEngine, ShadowSignalEngineCfg
from .single_instance import SingleInstanceLock
from .state_store import (
    KEY_LAST_EQUITY,
    KEY_LAST_RECONCILE_TS,
    KEY_POSITION_LOCK,
    KEY_REGIME,
    KEY_RISK_GUARD,
    StateStore,
    load_regime,
    load_risk_state,
    save_regime,
    save_risk_state,
)

__all__ = [
    # config
    "POLYMARKET_PLATFORM",
    "PolymarketPlatform",
    "PolymarketRuntimeCfg",
    "load_polymarket_runtime_cfg",
    "is_real_order_allowed",
    # logging
    "setup_logging",
    "get_logger",
    # clock
    "ClockSync",
    "ClockSyncCfg",
    # state
    "StateStore",
    "KEY_RISK_GUARD",
    "KEY_REGIME",
    "KEY_POSITION_LOCK",
    "KEY_LAST_EQUITY",
    "KEY_LAST_RECONCILE_TS",
    "save_risk_state",
    "load_risk_state",
    "save_regime",
    "load_regime",
    # locks
    "PositionLock",
    "SingleInstanceLock",
    # feed
    "BinanceFeed",
    "BinanceFeedCfg",
    "KlineBar",
    # market
    "MarketResolver",
    "MarketResolverCfg",
    "ActiveMarket",
    # polymarket
    "PolymarketClient",
    "OrderState",
    "OrderTicket",
    "PolymarketFeed",
    "PolymarketFeedCfg",
    "BookSnapshot",
    # reconcile
    "Reconciler",
    "ReconcilerCfg",
    "LocalPosition",
    # shadow signal engine
    "ShadowSignalEngine",
    "ShadowSignalEngineCfg",
    # alerting
    "AlertingDispatcher",
    "LogSink",
    "FileSink",
    "TelegramSink",
    # health
    "start_health_server",
    "set_halted",
    "set_metrics_provider",
    # runner
    "LiveRunner",
    "LiveRunnerCfg",
]
