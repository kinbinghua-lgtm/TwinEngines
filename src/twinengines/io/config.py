"""
平台静态参数 (与代码版本耦合) + 运行时可配置项 (.env 注入)。

参考来源:
    平台参数设置参考/SuperMusk - 副本 (5)/ 中的实盘项目, 已在 Polygon 主网验证可用。

只读常量 (POLYMARKET_PLATFORM):
    - 链 / RPC / CLOB host / WebSocket / USDC 合约地址 / CTF spender
    - 价格 tick / 极端价边界 / 买单价格下限
    - 实盘 1 share = 1 USDC 合约面值

运行时配置 (PolymarketRuntimeCfg, 由 .env 加载):
    - 私钥 / proxy 地址 / API key / 签名类型
    - dry_run 开关 (默认 True, 必须显式 export DRY_RUN=false 才发真实订单)
    - 各种超时与重试上限
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


# ============================================================
# 平台静态常量 (代码版本耦合, 不需要从 .env 读)
# ============================================================

@dataclass(frozen=True)
class PolymarketPlatform:
    """Polygon 主网 + Polymarket CLOB 平台静态参数。"""

    chain_id: int = 137
    chain_name: str = "Polygon"

    clob_host: str = "https://clob.polymarket.com"
    data_api_host: str = "https://data-api.polymarket.com"
    gamma_api_host: str = "https://gamma-api.polymarket.com"
    ws_market_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    ws_user_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

    # USDC.e ( bridged ) — 充值入账常见形态；CLOB V2 交易抵押见 pusd_address
    usdc_address: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    # Polymarket USD (pUSD) — CTF Exchange V2 抵押品（见 docs.polymarket.com/resources/contracts）
    pusd_address: str = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
    ctf_spender: str = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

    price_tick: float = 0.01            # Polymarket CLOB 报价精度
    price_extreme_min: float = 0.01     # 低于此价禁止下单 (流动性极差/无意义)
    price_extreme_max: float = 0.99
    buy_price_floor: float = 0.05       # 买入端硬下限, 避免 1¢ "送钱单"

    contract_face_value_usdc: float = 1.0   # 1 share = 1 USDC 面值
    min_order_quote_usdc: float = 1.0       # 实盘最小订单金额 (CLOB 风控建议)
    min_limit_order_shares: float = 5.0     # 限价单最低手数 (Polymarket CLOB 规则)


POLYMARKET_PLATFORM = PolymarketPlatform()


# ============================================================
# 运行时配置 (.env 注入)
# ============================================================

@dataclass
class PolymarketRuntimeCfg:
    """实盘运行时配置, 通常由 .env 注入。

    安全约束:
        - dry_run 默认 True, 真实下单必须显式关闭
        - private_key 缺失或非 0x... 时直接拒绝初始化下单客户端
        - 所有网络调用统一超时
    """

    # 钱包/认证
    private_key: Optional[str] = None
    proxy_address: Optional[str] = None              # Polymarket 代理钱包地址
    signature_type: int = 1                          # 0=EOA, 1=Proxy(Magic), 2=Proxy(Gnosis)
    polygon_rpc_url: str = "https://polygon-bor.publicnode.com"

    # API key (可选, 派生)
    builder_api_key: Optional[str] = None
    builder_secret: Optional[str] = None
    builder_passphrase: Optional[str] = None

    # 安全开关
    dry_run: bool = True                             # 默认空跑, 实盘需显式关
    enable_real_orders: bool = False                 # 双保险: 必须 dry_run=False 且此项=True

    # 超时/重试
    http_timeout_sec: float = 8.0
    ws_ping_interval_sec: float = 30.0
    ws_ping_timeout_sec: float = 10.0
    ws_max_retries: int = 5
    ws_retry_delay_sec: float = 5.0

    # 订单轮询
    order_poll_interval_sec: float = 1.0
    order_poll_max_wait_sec: float = 30.0            # 长时间 pending 主动取消并重新评估
    # 信号入场: 略跨 best_ask + FOK 吃单（Fill-or-Kill，全成或取消）
    entry_buy_cross_ticks: int = 2                 # 在 best_ask 上抬几个最小报价 tick（默认 0.01）
    # 成交回报后二次确认订单状态 (毫秒), 缓解幽灵成交
    order_ghost_confirm_delay_ms: int = 400

    # 行情/数据
    book_max_staleness_sec: float = 5.0              # 盘口超过此值视为过期, 不下单

    # 资金安全 (与策略层无关, 实盘 io 兜底)
    daily_max_loss_usdc: float = 50.0                # 实盘当日最大允许亏损 (绝对值兜底)
    naked_daily_drawdown_stop: float = 0.05          # naked 专用日内相对回撤停手；<=0 表示关闭
    abort_on_unknown_api_error: bool = True          # 遇到未知错误码立刻停手, 等人工
    auto_shadow_on_insufficient_funds: bool = True   # 资金不足/低于最小下单额时自动降级影子单

    # 日志
    log_dir: str = "logs"
    log_level: str = "INFO"

    # 健康检查
    health_check_port: Optional[int] = None          # None=不开, 1xxxx=HTTP /health 端点

    # 市场解析 (5min BTC 合约轮换)
    market_underlying: str = "BTC"                   # 关键字过滤: 标题中包含 BTC
    market_horizon_minutes: int = 5                  # 5 分钟周期合约
    market_search_keywords: tuple = ("Bitcoin", "BTC")
    market_refresh_interval_sec: float = 60.0
    market_resolver_url_template: str = (
        "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=200"
    )

    # 链上资金安全
    min_usdc_allowance_multiple: float = 100.0       # allowance 至少为 min_order_quote*N, 否则拒启
    equity_refresh_interval_sec: float = 30.0
    reconcile_interval_sec: float = 30.0
    # 链上持仓与本地账本不一致时: 仅当「未追踪」名义超过此值 (USDC) 才熔断 (旧版曾误用股数与阈值比较)
    reconcile_untracked_drift_usdc: float = 2.0

    # HTTP UA (Polymarket CLOB 部分端点要求 UA, 否则 403)
    http_user_agent: str = "TwinEngines/1.0 (+contact:ops)"

    # 单实例锁与状态文件
    state_db_path: str = "data_runtime/state.sqlite"
    instance_lock_path: str = "data_runtime/twinengines.pid"
    single_instance_min_restart_sec: int = 180

    # 空跑信号: 附带盘口机会估计时使用的名义金额 (USDC), 仅供诊断, 不影响下单
    shadow_book_check_stake_usdc: float = 5.0

    # 到期头寸自动赎回 (实盘资金回流闭环)
    auto_redeem_enabled: bool = True
    auto_redeem_interval_sec: float = 30.0
    auto_redeem_receipt_wait_sec: float = 20.0
    # p_rev 时间分桶校准 (最小可用版)
    enable_p_rev_time_calibration: bool = True
    p_rev_calibration_bucket_sec: int = 10
    p_rev_calibration_min_samples: int = 8
    p_rev_calibration_window_hours: float = 168.0
    p_rev_calibration_path: str = "data_runtime/p_rev_time_calibration.json"


def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(key)
    if v is None or v == "":
        return default
    return v


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(key: str, default: float) -> float:
    raw = _env(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def load_polymarket_runtime_cfg(env_file: Optional[str] = None) -> PolymarketRuntimeCfg:
    """
    从环境变量 (或可选 .env 文件) 加载运行时配置。

    安全保护:
        - 若未安装 python-dotenv, 直接读 os.environ, 不报错
        - dry_run 默认 True; enable_real_orders 也默认 False (双保险)
        - 私钥不写日志, 不打印
    """
    if env_file:
        try:
            from dotenv import load_dotenv  # type: ignore
            if os.path.isfile(env_file):
                load_dotenv(env_file, override=False)
        except ImportError:
            pass
    else:
        try:
            from dotenv import load_dotenv  # type: ignore
            load_dotenv(override=False)
        except ImportError:
            pass

    cfg = PolymarketRuntimeCfg(
        private_key=_env("POLYMARKET_PRIVATE_KEY") or _env("PRIVATE_KEY"),
        proxy_address=_env("POLYMARKET_PROXY_ADDRESS") or _env("PROXY_ADDRESS") or _env("POLYMARKET_ADDRESS"),
        signature_type=_env_int("SIGNATURE_TYPE", 1),
        polygon_rpc_url=_env("POLYGON_RPC_URL", "https://polygon-bor.publicnode.com") or "https://polygon-bor.publicnode.com",
        builder_api_key=_env("POLY_BUILDER_API_KEY"),
        builder_secret=_env("POLY_BUILDER_SECRET"),
        builder_passphrase=_env("POLY_BUILDER_PASSPHRASE"),
        dry_run=_env_bool("DRY_RUN", True),
        enable_real_orders=_env_bool("ENABLE_REAL_ORDERS", False),
        http_timeout_sec=_env_float("HTTP_TIMEOUT_SEC", 8.0),
        ws_ping_interval_sec=_env_float("WS_PING_INTERVAL_SEC", 30.0),
        ws_ping_timeout_sec=_env_float("WS_PING_TIMEOUT_SEC", 10.0),
        ws_max_retries=_env_int("WS_MAX_RETRIES", 5),
        ws_retry_delay_sec=_env_float("WS_RETRY_DELAY_SEC", 5.0),
        order_poll_interval_sec=_env_float("ORDER_POLL_INTERVAL_SEC", 1.0),
        order_poll_max_wait_sec=_env_float("ORDER_POLL_MAX_WAIT_SEC", 30.0),
        entry_buy_cross_ticks=max(0, _env_int("ENTRY_BUY_CROSS_TICKS", 2)),
        order_ghost_confirm_delay_ms=_env_int("ORDER_GHOST_CONFIRM_DELAY_MS", 400),
        book_max_staleness_sec=_env_float("BOOK_MAX_STALENESS_SEC", 5.0),
        daily_max_loss_usdc=_env_float("DAILY_MAX_LOSS_USDC", 50.0),
        naked_daily_drawdown_stop=_env_float("NAKED_DAILY_DRAWDOWN_STOP", 0.05),
        abort_on_unknown_api_error=_env_bool("ABORT_ON_UNKNOWN_API_ERROR", True),
        auto_shadow_on_insufficient_funds=_env_bool("AUTO_SHADOW_ON_INSUFFICIENT_FUNDS", True),
        log_dir=_env("LOG_DIR", "logs") or "logs",
        log_level=_env("LOG_LEVEL", "INFO") or "INFO",
        health_check_port=(_env_int("HEALTH_CHECK_PORT", 0) or None),
        market_underlying=_env("MARKET_UNDERLYING", "BTC") or "BTC",
        market_horizon_minutes=_env_int("MARKET_HORIZON_MINUTES", 5),
        market_refresh_interval_sec=_env_float("MARKET_REFRESH_INTERVAL_SEC", 60.0),
        min_usdc_allowance_multiple=_env_float("MIN_USDC_ALLOWANCE_MULTIPLE", 100.0),
        equity_refresh_interval_sec=_env_float("EQUITY_REFRESH_INTERVAL_SEC", 30.0),
        reconcile_interval_sec=_env_float("RECONCILE_INTERVAL_SEC", 30.0),
        reconcile_untracked_drift_usdc=max(0.0, _env_float("RECONCILE_UNTRACKED_DRIFT_USDC", 2.0)),
        http_user_agent=_env("HTTP_USER_AGENT", "TwinEngines/1.0 (+contact:ops)") or "TwinEngines/1.0 (+contact:ops)",
        state_db_path=_env("STATE_DB_PATH", "data_runtime/state.sqlite") or "data_runtime/state.sqlite",
        instance_lock_path=_env("INSTANCE_LOCK_PATH", "data_runtime/twinengines.pid") or "data_runtime/twinengines.pid",
        single_instance_min_restart_sec=_env_int("SINGLE_INSTANCE_MIN_RESTART_SEC", 180),
        shadow_book_check_stake_usdc=_env_float("SHADOW_BOOK_CHECK_STAKE_USDC", 5.0),
        auto_redeem_enabled=_env_bool("AUTO_REDEEM_ENABLED", False),
        auto_redeem_interval_sec=_env_float("AUTO_REDEEM_INTERVAL_SEC", 30.0),
        auto_redeem_receipt_wait_sec=_env_float("AUTO_REDEEM_RECEIPT_WAIT_SEC", 20.0),
        enable_p_rev_time_calibration=_env_bool("ENABLE_P_REV_TIME_CALIBRATION", True),
        p_rev_calibration_bucket_sec=_env_int("P_REV_CALIBRATION_BUCKET_SEC", 10),
        p_rev_calibration_min_samples=_env_int("P_REV_CALIBRATION_MIN_SAMPLES", 8),
        p_rev_calibration_window_hours=_env_float("P_REV_CALIBRATION_WINDOW_HOURS", 168.0),
        p_rev_calibration_path=_env("P_REV_CALIBRATION_PATH", "data_runtime/p_rev_time_calibration.json")
        or "data_runtime/p_rev_time_calibration.json",
    )
    return cfg


def is_real_order_allowed(cfg: PolymarketRuntimeCfg) -> tuple[bool, str]:
    """
    实盘下单前的双保险检查。
    返回 (allowed, reason)。reason 为拒绝原因或 "ok"。
    """
    if cfg.dry_run:
        return False, "dry_run=True (set DRY_RUN=false to enable real orders)"
    if not cfg.enable_real_orders:
        return False, "enable_real_orders=False (set ENABLE_REAL_ORDERS=true)"
    if not cfg.private_key or not cfg.private_key.strip():
        return False, "missing PRIVATE_KEY"
    pk = cfg.private_key.strip()
    if not (pk.startswith("0x") and len(pk) == 66):
        return False, "invalid PRIVATE_KEY format (expect 0x + 64 hex)"
    if not cfg.proxy_address or not cfg.proxy_address.strip():
        return False, "missing PROXY_ADDRESS"
    return True, "ok"
