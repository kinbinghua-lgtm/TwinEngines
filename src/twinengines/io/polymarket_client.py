"""
Polymarket CLOB 客户端 (实盘 IO).

填实内容:
    1. init_real_client    - 初始化 py_clob_client_v2 + 派生 L2 凭证
    2. fetch_book          - 拉取盘口 (urllib + 重试 + 时间戳防缓存 + UA)
    3. fetch_account_equity_usdc - web3 链上 USDC.e + pUSD 余额之和 (V2 抵押多为 pUSD)
    4. fetch_usdc_allowance / fetch_pusd_allowance - 检查 collateral allowance
    5. submit_order        - 入场: 纯 FOK 模式（Fill-or-Kill）
    6. poll_order          - 长 pending 自动 cancel
    7. cancel_order        - 主动撤单
    8. _sync_balance_allowance - 下单前同步链上余额到 CLOB

防御性约束:
    - 所有外部调用统一超时 (cfg.http_timeout_sec / web3 5s)
    - 余额查询失败 → 返回 None, 调用方据此暂停下单
    - 任何下单前都拉一次盘口, 盘口空/过期不下单
    - 价格强制裁剪 [extreme_min, extreme_max], 同时不低于 buy_price_floor
    - dry_run / 双保险未通过时, 任何 submit_order 均走影子单
    - 已分配但未成交的保证金, OrderTicket 状态机维护, 终态归零
    - py_clob_client_v2 / web3 未安装时, 实盘自动 fail-safe 拒单 (不静默)
"""

from __future__ import annotations

import json
import math
import threading
import time
import urllib.request
import urllib.parse
import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .config import (
    POLYMARKET_PLATFORM,
    PolymarketPlatform,
    PolymarketRuntimeCfg,
    is_real_order_allowed,
)
from .logging_setup import get_logger

logger = get_logger(__name__)

# ============================================================
# 订单状态机
# ============================================================

class OrderState(str, Enum):
    NEW = "NEW"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    TIMEOUT = "TIMEOUT"
    DRY_RUN_SHADOW = "DRY_RUN_SHADOW"

@dataclass
class OrderTicket:
    client_order_id: str
    side: str                       # "BUY" | "SELL"
    token_id: str
    price: float
    size_quote_usdc: float
    state: OrderState = OrderState.NEW
    exchange_order_id: Optional[str] = None
    margin_locked: float = 0.0
    submitted_at_ms: Optional[int] = None
    last_polled_at_ms: Optional[int] = None
    fills: list[dict] = field(default_factory=list)
    last_error: Optional[str] = None
    order_type: str = "FOK"
    size_shares: Optional[float] = None
    filled_size_shares: float = 0.0  # 当前 exchange_order 上已成交股数；若 prior_leg>0 则仅本腿
    reconcile_checkpoint_shares: float = 0.0  # 已向 reconciler 合并的累计股数 (用于增量 delta)

    def is_terminal(self) -> bool:
        return self.state in (
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.TIMEOUT,
            OrderState.DRY_RUN_SHADOW,
        )

    def release_margin(self) -> float:
        released = float(self.margin_locked)
        self.margin_locked = 0.0
        return released

# ============================================================
# 工具函数
# ============================================================

def _tick_round(price: float, tick: float = 0.01) -> float:
    return math.floor(price / tick) * tick

def _round_order_size_shares_up(raw_size: float, min_sz: float) -> float:
    """CLOB 对手数量精度（常见限制 taker 最多 4 位小数）；向上取整避免名义不足。"""
    x = max(float(raw_size), float(min_sz))
    return round(math.ceil(x * 10000 - 1e-9) / 10000, 4)

def _round_buy_size_for_quote_cents(*, price: float, raw_size: float, min_sz: float) -> float:
    """BUY FOK 的 price*size 会形成美元金额；Polymarket 要求金额最多 2 位小数。"""
    px = max(float(price), 0.01)
    target = max(float(raw_size), float(min_sz))
    cents = max(1, math.ceil(px * target * 100 - 1e-9))
    for _ in range(10000):
        size = round((cents / 100.0) / px, 4)
        if size + 1e-12 >= target and abs((size * px * 100) - round(size * px * 100)) < 1e-7:
            return size
        cents += 1
    return _round_order_size_shares_up(target, min_sz)

def _is_fok_no_fill_error(err: str) -> bool:
    msg = (err or "").lower()
    return (
        "fok_order_not_filled_error" in msg
        or "not filled" in msg
        or "couldn't be fully filled" in msg
        or "could not be fully filled" in msg
        or "fully filled or killed" in msg
    )

def _http_get_json(url: str, *, timeout: float, user_agent: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Cache-Control": "no-cache",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def _is_balance_or_allowance_error(err_msg: str) -> bool:
    msg = (err_msg or "").lower()
    return "not enough balance" in msg or "allowance" in msg

def _parse_filled_shares_from_clob_order(order: dict) -> float:
    """从 CLOB get_order 返回体解析已成交股数 (best-effort)."""
    if not isinstance(order, dict):
        return 0.0
    for k in ("size_matched", "sizeMatched", "filledSize", "filled_size", "matched_size", "executed_size"):
        v = order.get(k)
        if isinstance(v, (int, float)) and float(v) >= 0:
            return float(v)
        if isinstance(v, str):
            try:
                return max(0.0, float(v))
            except (TypeError, ValueError):
                pass
    try:
        orig = float(order.get("original_size") or order.get("originalSize") or order.get("size") or 0)
        rem = float(order.get("remaining_size") or order.get("remainingSize") or order.get("size_remaining") or orig)
        if orig > 0 and rem <= orig:
            return max(0.0, orig - rem)
    except (TypeError, ValueError):
        pass
    return 0.0

# ============================================================
# 主客户端
# ============================================================

@dataclass
class PolymarketClient:
    runtime_cfg: PolymarketRuntimeCfg
    platform: PolymarketPlatform = field(default_factory=lambda: POLYMARKET_PLATFORM)

    _real_client: Any = None
    _real_inited: bool = False
    _w3: Any = None
    _maker_address: Optional[str] = None
    _circuit_open: bool = False
    _circuit_open_reason: Optional[str] = None
    _consecutive_unknown_errors: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _feed: Any = None  # PolymarketFeed (可选, 由 LiveRunner 注入)
    _ws_book_hits: int = 0
    _rest_book_hits: int = 0
    _relayer_client: Any = None

    _CTF_REDEEM_ABI = [
        {
            "name": "redeemPositions",
            "type": "function",
            "inputs": [
                {"name": "collateralToken", "type": "address"},
                {"name": "parentCollectionId", "type": "bytes32"},
                {"name": "conditionId", "type": "bytes32"},
                {"name": "indexSets", "type": "uint256[]"},
            ],
            "outputs": [],
        }
    ]

    # ---------------- 初始化 ----------------

    def init_real_client(self) -> tuple[bool, str]:
        with self._lock:
            if self._real_inited:
                return self._real_client is not None, "already_inited"

            ok, reason = is_real_order_allowed(self.runtime_cfg)
            if not ok:
                logger.info("polymarket_client init: real orders disabled (%s)", reason)
                self._real_inited = True
                return False, reason

            try:
                from py_clob_client_v2.client import ClobClient  # type: ignore
            except ImportError as e:
                logger.error("py_clob_client_v2 not installed; cannot init real CLOB client: %s", e)
                self._real_inited = True
                self._trip_circuit(f"py_clob_client_v2 missing: {e}")
                return False, "py_clob_client_v2_missing"

            try:
                funder_raw = (self.runtime_cfg.proxy_address or "").strip() or None
                funder: Optional[str] = funder_raw
                try:
                    from web3 import Web3  # type: ignore
                    if funder_raw:
                        funder = Web3.to_checksum_address(funder_raw)
                except Exception:
                    pass

                client = ClobClient(
                    host=self.platform.clob_host,
                    chain_id=self.platform.chain_id,
                    key=str(self.runtime_cfg.private_key).strip(),
                    signature_type=self.runtime_cfg.signature_type,
                    funder=funder,
                )
                try:
                    # V2 上 create_api_key 常见先触发 /auth/api-key 403（随后 derive 可成功），
                    # 这里直接走 derive 降低无效 ERROR 噪音。
                    creds = client.derive_api_key()
                    if creds:
                        client.set_api_creds(creds)
                    else:
                        logger.warning("polymarket L2 creds derive returned empty; orders may fail")
                except Exception as e:
                    logger.warning("polymarket L2 creds init failed: %s", e)

                self._real_client = client
                self._maker_address = funder
                self._real_inited = True
                logger.info(
                    "polymarket_client real ready: sig_type=%s funder=%s host=%s",
                    self.runtime_cfg.signature_type,
                    "set" if funder else "unset",
                    self.platform.clob_host,
                )

                # 同时初始化 web3 (用于链上 USDC 查询)
                self._init_web3()
                return True, "ok"
            except Exception as e:
                logger.exception("polymarket_client real init crashed: %s", e)
                self._real_inited = True
                self._trip_circuit(f"real_init_crash:{e}")
                return False, f"init_error:{e}"

    def _init_web3(self) -> None:
        try:
            from web3 import Web3  # type: ignore
            self._w3 = Web3(
                Web3.HTTPProvider(
                    self.runtime_cfg.polygon_rpc_url,
                    request_kwargs={"timeout": 5},
                )
            )
            if not self._w3.is_connected():
                logger.warning("polygon RPC not connected: %s", self.runtime_cfg.polygon_rpc_url)
                self._w3 = None
            else:
                logger.info("web3 polygon RPC connected: %s", self.runtime_cfg.polygon_rpc_url)
        except ImportError:
            logger.warning("web3 not installed; on-chain USDC queries disabled")
            self._w3 = None
        except Exception as e:
            logger.exception("web3 init failed: %s", e)
            self._w3 = None

    # ---------------- 状态/熔断 ----------------

    def _trip_circuit(self, reason: str) -> None:
        with self._lock:
            if not self._circuit_open:
                self._circuit_open = True
                self._circuit_open_reason = reason
                logger.error("CIRCUIT BREAKER tripped: %s", reason)

    def reset_circuit(self) -> None:
        with self._lock:
            self._circuit_open = False
            self._circuit_open_reason = None
            self._consecutive_unknown_errors = 0
        logger.warning("polymarket_client circuit RESET (manual)")

    def is_healthy(self) -> bool:
        with self._lock:
            return not self._circuit_open

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "real_inited": self._real_inited,
                "real_available": self._real_client is not None,
                "w3_available": self._w3 is not None,
                "circuit_open": self._circuit_open,
                "circuit_reason": self._circuit_open_reason,
                "consecutive_unknown_errors": self._consecutive_unknown_errors,
                "maker": self._maker_address,
                "dry_run": self.runtime_cfg.dry_run,
                "enable_real": self.runtime_cfg.enable_real_orders,
                "feed_attached": self._feed is not None,
                "ws_book_hits": self._ws_book_hits,
                "rest_book_hits": self._rest_book_hits,
            }

    # ---------------- 行情 ----------------

    def attach_feed(self, feed: Any) -> None:
        """注入 PolymarketFeed (WS 主路). 由 LiveRunner 在 start() 时调用."""
        with self._lock:
            self._feed = feed

    def fetch_book(self, token_id: str) -> dict[str, Any]:
        out = {
            "best_bid": None,
            "best_ask": None,
            "best_bid_size": None,
            "best_ask_size": None,
            "midpoint": None,
            "stale": False,
            "ts_ms": int(time.time() * 1000),
            "source": "rest",
            "error": None,
        }
        feed = self._feed
        if feed is not None:
            try:
                snap = feed.get_book(token_id)
            except Exception:
                snap = None
            if snap is not None:
                out["best_bid"] = snap.best_bid
                out["best_ask"] = snap.best_ask
                out["best_bid_size"] = snap.bid_size_top
                out["best_ask_size"] = snap.ask_size_top
                if snap.best_bid is not None and snap.best_ask is not None:
                    out["midpoint"] = (snap.best_bid + snap.best_ask) / 2.0
                out["ts_ms"] = int(snap.received_at_ms)
                out["source"] = snap.source
                with self._lock:
                    self._ws_book_hits += 1
                return out
        url = (
            f"{self.platform.clob_host}/book"
            f"?token_id={urllib.parse.quote(token_id)}&_={int(time.time() * 1000)}"
        )
        last_err: Optional[Exception] = None
        for attempt in range(2):
            try:
                data = _http_get_json(
                    url,
                    timeout=self.runtime_cfg.http_timeout_sec,
                    user_agent=self.runtime_cfg.http_user_agent,
                )
                bids = data.get("bids", []) if isinstance(data, dict) else []
                asks = data.get("asks", []) if isinstance(data, dict) else []
                if bids:
                    best_bid = max(bids, key=lambda x: float(x.get("price") or 0))
                    out["best_bid"] = float(best_bid.get("price") or 0)
                    out["best_bid_size"] = float(best_bid.get("size") or 0)
                if asks:
                    best_ask = min(asks, key=lambda x: float(x.get("price") or 1))
                    out["best_ask"] = float(best_ask.get("price") or 0)
                    out["best_ask_size"] = float(best_ask.get("size") or 0)
                if out["best_bid"] is not None and out["best_ask"] is not None:
                    out["midpoint"] = (out["best_bid"] + out["best_ask"]) / 2.0
                with self._lock:
                    self._rest_book_hits += 1
                return out
            except Exception as e:
                last_err = e
                if attempt == 0:
                    logger.debug("fetch_book retry (1st failed): %s", e)
                    time.sleep(0.5)
        logger.warning("fetch_book failed (after retry): %s", last_err)
        out["stale"] = True
        out["error"] = str(last_err) if last_err else "unknown"
        return out

    def fetch_book_depth(self, token_id: str, *, max_levels: int = 20) -> dict[str, Any]:
        """获取多档盘口深度 (REST fallback only, WS 暂不支持多档)。
        
        返回:
            {
                "bids": [{"price": float, "size": float}, ...],  # 降序
                "asks": [{"price": float, "size": float}, ...],  # 升序
                "best_bid": float,
                "best_ask": float,
                "stale": bool,
                "ts_ms": int,
                "error": str | None,
            }
        """
        out = {
            "bids": [],
            "asks": [],
            "best_bid": None,
            "best_ask": None,
            "stale": False,
            "ts_ms": int(time.time() * 1000),
            "error": None,
        }
        url = (
            f"{self.platform.clob_host}/book"
            f"?token_id={urllib.parse.quote(token_id)}&_={int(time.time() * 1000)}"
        )
        last_err: Optional[Exception] = None
        for attempt in range(2):
            try:
                data = _http_get_json(
                    url,
                    timeout=self.runtime_cfg.http_timeout_sec,
                    user_agent=self.runtime_cfg.http_user_agent,
                )
                raw_bids = data.get("bids", []) if isinstance(data, dict) else []
                raw_asks = data.get("asks", []) if isinstance(data, dict) else []
                bids = [
                    {"price": float(b.get("price") or 0), "size": float(b.get("size") or 0)}
                    for b in raw_bids if float(b.get("price") or 0) > 0
                ]
                asks = [
                    {"price": float(a.get("price") or 0), "size": float(a.get("size") or 0)}
                    for a in raw_asks if float(a.get("price") or 0) > 0
                ]
                bids.sort(key=lambda x: x["price"], reverse=True)
                asks.sort(key=lambda x: x["price"])
                out["bids"] = bids[:max_levels]
                out["asks"] = asks[:max_levels]
                if bids:
                    out["best_bid"] = bids[0]["price"]
                if asks:
                    out["best_ask"] = asks[0]["price"]
                return out
            except Exception as e:
                last_err = e
                if attempt == 0:
                    logger.debug("fetch_book_depth retry (1st failed): %s", e)
                    time.sleep(0.5)
        logger.warning("fetch_book_depth failed (after retry): %s", last_err)
        out["stale"] = True
        out["error"] = str(last_err) if last_err else "unknown"
        return out

    # ---------------- 账户 (链上) ----------------

    _USDC_ABI = [
        {
            "constant": True,
            "inputs": [{"name": "account", "type": "address"}],
            "name": "balanceOf",
            "outputs": [{"name": "", "type": "uint256"}],
            "type": "function",
        },
        {
            "constant": True,
            "inputs": [
                {"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"},
            ],
            "name": "allowance",
            "outputs": [{"name": "", "type": "uint256"}],
            "type": "function",
        },
    ]

    _ERC20_DECIMALS_ABI = [
        {
            "constant": True,
            "inputs": [],
            "name": "decimals",
            "outputs": [{"name": "", "type": "uint8"}],
            "type": "function",
        },
        {
            "constant": True,
            "inputs": [{"name": "account", "type": "address"}],
            "name": "balanceOf",
            "outputs": [{"name": "", "type": "uint256"}],
            "type": "function",
        },
        {
            "constant": True,
            "inputs": [
                {"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"},
            ],
            "name": "allowance",
            "outputs": [{"name": "", "type": "uint256"}],
            "type": "function",
        },
    ]

    def _ensure_w3(self) -> bool:
        with self._lock:
            if self._w3 is not None:
                return True
        self._init_web3()
        return self._w3 is not None

    def _erc20_balance_human(self, *, token_address: str, holder_address: str) -> Optional[float]:
        """ERC20 balance → 人类可读金额 (按链上 decimals, 失败则 None)."""
        if not self._ensure_w3():
            return None
        try:
            w3 = self._w3
            token_cs = w3.to_checksum_address(token_address)
            holder_cs = w3.to_checksum_address(holder_address)
            c = w3.eth.contract(address=token_cs, abi=self._ERC20_DECIMALS_ABI)
            raw = int(c.functions.balanceOf(holder_cs).call())
            try:
                dec = int(c.functions.decimals().call())
            except Exception:
                dec = 6
            return float(raw) / float(10 ** dec)
        except Exception as e:
            logger.debug("erc20 balance failed token=%s err=%s", token_address[:12], e)
            return None

    def _erc20_allowance_human(
        self, *, token_address: str, holder_address: str, spender_address: str,
    ) -> Optional[float]:
        if not self._ensure_w3():
            return None
        try:
            w3 = self._w3
            token_cs = w3.to_checksum_address(token_address)
            holder_cs = w3.to_checksum_address(holder_address)
            spender_cs = w3.to_checksum_address(spender_address)
            c = w3.eth.contract(address=token_cs, abi=self._ERC20_DECIMALS_ABI)
            raw = int(
                c.functions.allowance(holder_cs, spender_cs).call(),
            )
            try:
                dec = int(c.functions.decimals().call())
            except Exception:
                dec = 6
            return float(raw) / float(10 ** dec)
        except Exception as e:
            logger.debug("erc20 allowance failed token=%s err=%s", token_address[:12], e)
            return None

    def fetch_account_equity_usdc(self) -> Optional[float]:
        if not self.runtime_cfg.proxy_address:
            logger.debug("fetch_account_equity: proxy_address empty")
            return None
        if not self._ensure_w3():
            return None
        holder = str(self.runtime_cfg.proxy_address).strip()
        bal_usdc = self._erc20_balance_human(
            token_address=self.platform.usdc_address,
            holder_address=holder,
        )
        bal_pusd = self._erc20_balance_human(
            token_address=self.platform.pusd_address,
            holder_address=holder,
        )
        if bal_usdc is None and bal_pusd is None:
            logger.warning("fetch_account_equity failed: both USDC.e and pUSD balance queries failed")
            return None
        total = float(bal_usdc or 0.0) + float(bal_pusd or 0.0)
        logger.debug(
            "fetch_account_equity: usdc_e=%s pusd=%s total=%s",
            bal_usdc, bal_pusd, total,
        )
        return total

    def fetch_usdc_allowance(self, *, spender: Optional[str] = None) -> Optional[float]:
        if not self.runtime_cfg.proxy_address:
            return None
        spender_addr = spender or self.platform.ctf_spender
        return self._erc20_allowance_human(
            token_address=self.platform.usdc_address,
            holder_address=str(self.runtime_cfg.proxy_address).strip(),
            spender_address=spender_addr,
        )

    def fetch_pusd_allowance(self, *, spender: Optional[str] = None) -> Optional[float]:
        if not self.runtime_cfg.proxy_address:
            return None
        spender_addr = spender or self.platform.ctf_spender
        return self._erc20_allowance_human(
            token_address=self.platform.pusd_address,
            holder_address=str(self.runtime_cfg.proxy_address).strip(),
            spender_address=spender_addr,
        )

    def precheck_allowance(self) -> tuple[bool, str, Optional[float]]:
        """启动时调用: allowance < min_order_quote * N 时拒绝启动。"""
        if self.runtime_cfg.dry_run:
            return True, "dry_run skips allowance check", None
        a_usdc = self.fetch_usdc_allowance()
        a_pusd = self.fetch_pusd_allowance()
        if a_usdc is None and a_pusd is None:
            return False, "allowance query failed (RPC down or no proxy)", None
        allowance = max(float(a_usdc or 0.0), float(a_pusd or 0.0))
        threshold = (
            self.platform.min_order_quote_usdc
            * self.runtime_cfg.min_usdc_allowance_multiple
        )
        if allowance < threshold:
            return (
                False,
                f"collateral allowance max(USDC.e,pUSD) {allowance:.2f} < required {threshold:.2f} "
                f"(deposit/approve via Polymarket UI)",
                allowance,
            )
        detail = f"USDC.e={a_usdc}, pUSD={a_pusd}" if (a_usdc is not None or a_pusd is not None) else ""
        return True, f"allowance ok ({allowance:.2f} >= {threshold:.2f}) {detail}".strip(), allowance

    # ---------------- 价格校验 ----------------

    def _validate_price(self, price: float, side: str) -> tuple[bool, str, float]:
        p = self.platform
        try:
            f_price = float(price)
        except Exception:
            return False, "price_invalid", 0.0
        if not math.isfinite(f_price):
            return False, "price_nan", 0.0
        clamped = _tick_round(max(p.price_extreme_min, min(p.price_extreme_max, f_price)))
        if clamped <= 0:
            clamped = p.price_extreme_min
        if side.upper() == "BUY" and clamped < p.buy_price_floor:
            return False, f"buy_below_floor({p.buy_price_floor:.2f})", clamped
        return True, "ok", clamped

    # ---------------- 余额同步 ----------------

    def _sync_balance_allowance(self) -> None:
        if self._real_client is None:
            return
        try:
            from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams  # type: ignore
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=-1)
            self._real_client.update_balance_allowance(params)
            logger.debug("clob balance/allowance synced")
        except Exception as e:
            logger.debug("clob balance/allowance sync failed (continuing): %s", e)

    def _sync_conditional_balance(self, token_id: str) -> None:
        if self._real_client is None:
            return
        try:
            from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams  # type: ignore
            params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=str(token_id))
            self._real_client.update_balance_allowance(params)
            logger.debug("clob conditional balance synced token=%s...", str(token_id)[:16])
        except Exception as e:
            logger.debug("conditional balance sync failed (continuing): %s", e)

    @staticmethod
    def _extract_order_id(result) -> Optional[str]:
        if isinstance(result, dict):
            return result.get("orderID") or result.get("orderId")
        return getattr(result, "orderID", None) or getattr(result, "orderId", None)

    # ---------------- 下单 ----------------

    def submit_order(
        self,
        *,
        side: str,
        token_id: str,
        price: float,
        size_quote_usdc: float,
        client_order_id: str,
        size_shares: Optional[float] = None,
    ) -> OrderTicket:
        ticket = OrderTicket(
            client_order_id=client_order_id,
            side=side.upper(),
            token_id=token_id,
            price=float(price),
            size_quote_usdc=float(size_quote_usdc),
        )
        if size_shares is not None and float(size_shares) > 0:
            ticket.size_shares = float(size_shares)

        if self._circuit_open:
            if (
                bool(self.runtime_cfg.auto_shadow_on_insufficient_funds)
                and str(self._circuit_open_reason or "").startswith("balance_or_allowance:")
            ):
                ticket.state = OrderState.DRY_RUN_SHADOW
                ticket.last_error = f"auto_shadow_circuit:{self._circuit_open_reason}"
                ticket.submitted_at_ms = int(time.time() * 1000)
                logger.warning(
                    "submit_order auto-shadow (circuit balance/allowance) coid=%s reason=%s",
                    client_order_id,
                    self._circuit_open_reason,
                )
                return ticket
            ticket.state = OrderState.REJECTED
            ticket.last_error = f"circuit_open:{self._circuit_open_reason}"
            logger.warning("submit_order rejected by circuit: %s", ticket.last_error)
            return ticket

        ok, reason, clamped = self._validate_price(price, side)
        if not ok:
            ticket.state = OrderState.REJECTED
            ticket.last_error = reason
            logger.warning("submit_order rejected price: %s coid=%s", reason, client_order_id)
            return ticket
        ticket.price = clamped

        size_quote_usdc = round(float(size_quote_usdc), 2)
        if size_quote_usdc < self.platform.min_order_quote_usdc:
            if bool(self.runtime_cfg.auto_shadow_on_insufficient_funds):
                ticket.state = OrderState.DRY_RUN_SHADOW
                ticket.last_error = f"size_below_min_auto_shadow({self.platform.min_order_quote_usdc:.2f})"
                ticket.submitted_at_ms = int(time.time() * 1000)
                logger.warning(
                    "submit_order auto-shadow size: %.4f < min %.2f coid=%s",
                    size_quote_usdc, self.platform.min_order_quote_usdc, client_order_id,
                )
                return ticket
            ticket.state = OrderState.REJECTED
            ticket.last_error = f"size_below_min({self.platform.min_order_quote_usdc:.2f})"
            logger.warning(
                "submit_order rejected size: %.4f < min %.2f",
                size_quote_usdc, self.platform.min_order_quote_usdc,
            )
            return ticket
        ticket.size_quote_usdc = size_quote_usdc

        # dry_run 或真实客户端未就绪 -> 影子单
        if self.runtime_cfg.dry_run or not self._real_inited:
            self.init_real_client()
        if self.runtime_cfg.dry_run or self._real_client is None:
            ticket.state = OrderState.DRY_RUN_SHADOW
            ticket.margin_locked = 0.0
            ticket.submitted_at_ms = int(time.time() * 1000)
            logger.info(
                "[DRY-RUN] shadow_order side=%s token=%s... price=%.4f size=%.2f cid=%s",
                ticket.side, token_id[:10], ticket.price, ticket.size_quote_usdc, client_order_id,
            )
            return ticket

        # ---------------- 真实下单 (纯 FOK 模式) ----------------
        ticket.submitted_at_ms = int(time.time() * 1000)
        if ticket.side == "SELL":
            ticket.margin_locked = 0.0
        else:
            ticket.margin_locked = ticket.size_quote_usdc
        ticket.state = OrderState.SUBMITTED

        self._sync_balance_allowance()
        if ticket.side == "SELL":
            self._submit_exit_sell(
                ticket,
            )
        else:
            self._submit_entry_buy(
                ticket,
            )
        return ticket

    def _submit_entry_buy(
        self,
        ticket: OrderTicket,
    ) -> None:
        self._submit_clob_limit_order(
            ticket,
            sync_conditional_token_id=None,
        )

    def _submit_exit_sell(
        self,
        ticket: OrderTicket,
    ) -> None:
        self._submit_clob_limit_order(
            ticket,
            sync_conditional_token_id=str(ticket.token_id),
        )

    def _submit_clob_limit_order(
        self,
        ticket: OrderTicket,
        *,
        sync_conditional_token_id: Optional[str] = None,
    ) -> None:
        """限价单（买/卖）：纯 FOK 模式；股数按交易所精度向上取整。"""
        if sync_conditional_token_id:
            self._sync_conditional_balance(sync_conditional_token_id)
        min_sz = float(self.platform.min_limit_order_shares)
        try:
            from py_clob_client_v2.clob_types import OrderArgsV2, OrderType  # type: ignore

            limit_price = max(0.01, float(ticket.price))
            if ticket.size_shares is not None and float(ticket.size_shares) > 0:
                raw_size = float(ticket.size_shares)
            else:
                raw_size = ticket.size_quote_usdc / limit_price if limit_price > 0 else 0.0
            if ticket.side.upper() == "BUY":
                size = _round_buy_size_for_quote_cents(price=limit_price, raw_size=raw_size, min_sz=min_sz)
                ticket.size_quote_usdc = round(limit_price * float(size), 2)
                ticket.margin_locked = ticket.size_quote_usdc
            else:
                size = _round_order_size_shares_up(raw_size, min_sz)
            if size < min_sz:
                logger.warning(
                    "entry buy rejected: size %.2f < platform min %.1f coid=%s",
                    size, min_sz, ticket.client_order_id,
                )
                ticket.state = OrderState.REJECTED
                ticket.last_error = f"size_below_min_shares({min_sz})"
                ticket.release_margin()
                return

            ticket.order_type = "FOK"
            ticket.size_shares = float(size)
            args_fok = OrderArgsV2(
                token_id=ticket.token_id,
                price=limit_price,
                size=float(size),
                side=ticket.side,
            )
            signed_fok = self._real_client.create_order(args_fok)
            try:
                result_fok = self._real_client.post_order(signed_fok, OrderType.FOK)
            except Exception as e:
                err = str(e)
                logger.warning("FOK post_order exception: %s coid=%s", err, ticket.client_order_id)
                if _is_fok_no_fill_error(err):
                    ticket.state = OrderState.REJECTED
                    ticket.last_error = "fok_no_fill"
                else:
                    ticket.state = OrderState.TIMEOUT
                    ticket.last_error = f"post_order_exception:{err[:160]}"
                ticket.release_margin()
                return

            if isinstance(result_fok, dict):
                success = result_fok.get("success")
                err_msg = str(result_fok.get("errorMsg") or result_fok.get("error") or "")
                if success is False:
                    ticket.state = OrderState.REJECTED
                    if _is_fok_no_fill_error(err_msg):
                        ticket.last_error = "fok_no_fill"
                    else:
                        ticket.last_error = err_msg or "fok_rejected"
                    ticket.release_margin()
                    return

            oid_fok = self._extract_order_id(result_fok)
            if not oid_fok:
                logger.warning("FOK no orderID result=%s coid=%s", repr(result_fok)[:200], ticket.client_order_id)
                ticket.state = OrderState.TIMEOUT
                ticket.last_error = "unknown_no_order_id"
                ticket.release_margin()
                return

            ticket.exchange_order_id = oid_fok
            self.refresh_order_truth(
                ticket, delay_ms=int(self.runtime_cfg.order_ghost_confirm_delay_ms),
            )
            matched = float(ticket.filled_size_shares or 0.0)
            if ticket.state == OrderState.FILLED and matched > 1e-9:
                logger.info(
                    "FOK filled size=%.4f price=%.4f oid=%s coid=%s",
                    matched, limit_price, oid_fok, ticket.client_order_id,
                )
                return
            if ticket.state == OrderState.FILLED and matched <= 1e-9:
                ticket.state = OrderState.TIMEOUT
                ticket.last_error = "unknown_filled_size"
                ticket.release_margin()
                return
            if ticket.state == OrderState.PARTIAL and matched > 1e-9:
                logger.warning(
                    "FOK partial matched=%.4f target=%.4f price=%.4f oid=%s coid=%s",
                    matched, float(size), limit_price, oid_fok, ticket.client_order_id,
                )
                ticket.margin_locked = 0.0
                return
            if ticket.state in (OrderState.CANCELLED, OrderState.REJECTED):
                ticket.last_error = ticket.last_error or "fok_no_fill"
                ticket.release_margin()
                return

            ticket.state = OrderState.TIMEOUT
            ticket.last_error = "unknown_after_order_query"
            ticket.release_margin()
        except Exception as e:
            err_msg = str(e)
            logger.exception("clob limit order exception: %s coid=%s", err_msg, ticket.client_order_id)
            ticket.last_error = f"unknown:{err_msg[:160]}"
            ticket.state = OrderState.TIMEOUT
            ticket.release_margin()
            if _is_balance_or_allowance_error(err_msg):
                self._trip_circuit(f"balance_or_allowance:{err_msg[:80]}")

    def refresh_order_truth(self, ticket: OrderTicket, *, delay_ms: Optional[int] = None) -> OrderTicket:
        """延迟后再次拉取订单状态 (缓解回报与簿记不一致)."""
        if ticket.exchange_order_id is None or self._real_client is None:
            return ticket
        ms = int(delay_ms if delay_ms is not None else self.runtime_cfg.order_ghost_confirm_delay_ms)
        ms = max(0, min(ms, 5000))
        if ms > 0:
            time.sleep(ms / 1000.0)
        try:
            order = self._real_client.get_order(ticket.exchange_order_id)
            if not isinstance(order, dict):
                return ticket
            status = (order.get("status") or "").upper()
            matched = _parse_filled_shares_from_clob_order(order)
            if matched > 0:
                ticket.filled_size_shares = matched
            if status in ("MATCHED", "FILLED"):
                ticket.state = OrderState.FILLED
                ticket.release_margin()
            elif status in ("PARTIAL", "PARTIALLY_FILLED", "LIVE"):
                if matched > 0 and ticket.size_shares is not None and matched + 1e-9 < float(ticket.size_shares):
                    ticket.state = OrderState.PARTIAL
                    ticket.margin_locked = max(0.0, (float(ticket.size_shares) - matched) * max(0.01, float(ticket.price)))
                elif matched > 0 and ticket.size_shares is not None and abs(matched - float(ticket.size_shares)) < 1e-6:
                    ticket.state = OrderState.FILLED
                    ticket.release_margin()
                elif status in ("PARTIAL", "PARTIALLY_FILLED"):
                    ticket.state = OrderState.PARTIAL
            elif status == "CANCELLED":
                sz_tot = float(ticket.size_shares or 0)
                if matched > 1e-9 and sz_tot > 1e-9 and matched + 1e-9 < sz_tot:
                    ticket.filled_size_shares = matched
                    ticket.state = OrderState.PARTIAL
                    ticket.margin_locked = 0.0
                else:
                    ticket.state = OrderState.CANCELLED
                    ticket.release_margin()
            elif status == "REJECTED":
                ticket.state = OrderState.REJECTED
                ticket.release_margin()
        except Exception as e:
            logger.warning("refresh_order_truth failed coid=%s err=%s", ticket.client_order_id, e)
        return ticket

    # ---------------- 轮询/取消 ----------------

    def poll_order(self, ticket: OrderTicket) -> OrderTicket:
        if ticket.is_terminal():
            return ticket

        now_ms = int(time.time() * 1000)
        ticket.last_polled_at_ms = now_ms
        if ticket.submitted_at_ms is not None:
            elapsed = (now_ms - ticket.submitted_at_ms) / 1000.0
            if elapsed > self.runtime_cfg.order_poll_max_wait_sec:
                self.cancel_order(ticket, reason="poll_timeout")
                return ticket

        if self._real_client is None or not ticket.exchange_order_id:
            return ticket

        try:
            order = self._real_client.get_order(ticket.exchange_order_id)
            if isinstance(order, dict):
                fs = _parse_filled_shares_from_clob_order(order)
                prior = float(getattr(ticket, "prior_leg_filled_shares", 0.0) or 0.0)
                if fs > 0:
                    if prior > 1e-9:
                        ticket.filled_size_shares = fs
                    else:
                        ticket.filled_size_shares = max(float(ticket.filled_size_shares), fs)
                status = (order.get("status") or "").upper()
                if status in ("MATCHED", "FILLED"):
                    ticket.state = OrderState.FILLED
                    ticket.release_margin()
                elif status in ("PARTIAL", "PARTIALLY_FILLED"):
                    ticket.state = OrderState.PARTIAL
                    remaining = self._estimate_remaining_margin(order=order, ticket=ticket)
                    ticket.margin_locked = max(0.0, remaining)
                elif status == "CANCELLED":
                    sz_tot = float(ticket.size_shares or 0)
                    if fs > 1e-9 and sz_tot > 1e-9 and fs + 1e-9 < sz_tot:
                        ticket.state = OrderState.PARTIAL
                        ticket.margin_locked = 0.0
                    else:
                        ticket.state = OrderState.CANCELLED
                        ticket.release_margin()
                elif status == "REJECTED":
                    ticket.state = OrderState.REJECTED
                    ticket.release_margin()
        except Exception as e:
            logger.debug("poll_order get_order failed: %s", e)
        return ticket

    def _estimate_remaining_margin(self, *, order: dict, ticket: OrderTicket) -> float:
        size_total = float(ticket.size_shares or 0.0)
        if size_total <= 0:
            return float(ticket.margin_locked)
        filled_size = None
        for k in ("filled_size", "filledSize", "size_matched", "matched_size", "executed_size"):
            v = order.get(k)
            if isinstance(v, (int, float)):
                filled_size = float(v)
                break
            if isinstance(v, str):
                try:
                    filled_size = float(v)
                    break
                except Exception:
                    pass
        if filled_size is None:
            return float(ticket.margin_locked)
        rem_size = max(0.0, size_total - max(0.0, filled_size))
        return rem_size * max(0.01, float(ticket.price))

    def cancel_order(self, ticket: OrderTicket, *, reason: str = "manual") -> OrderTicket:
        if ticket.is_terminal():
            return ticket
        try:
            if self._real_client is not None and ticket.exchange_order_id:
                try:
                    pre = self._real_client.get_order(ticket.exchange_order_id)
                    if isinstance(pre, dict):
                        fs = _parse_filled_shares_from_clob_order(pre)
                        if fs > 0:
                            prior = float(getattr(ticket, "prior_leg_filled_shares", 0.0) or 0.0)
                            if prior > 1e-9:
                                ticket.filled_size_shares = fs
                            else:
                                ticket.filled_size_shares = max(float(ticket.filled_size_shares), fs)
                except Exception:
                    pass
                self._real_client.cancel_order(order_id=ticket.exchange_order_id)
        except Exception as e:
            logger.warning("cancel real order failed (will mark CANCELLED anyway): %s", e)
        ticket.state = OrderState.CANCELLED
        released = ticket.release_margin()
        logger.info(
            "order cancelled coid=%s oid=%s reason=%s released=%.2f",
            ticket.client_order_id, ticket.exchange_order_id, reason, released,
        )
        return ticket

    # ---------------- Position 查询 ----------------

    def fetch_market_positions(self, *, condition_id: str) -> list[dict]:
        if not self.runtime_cfg.proxy_address:
            return []
        cid = condition_id.strip()
        if cid and not cid.startswith("0x"):
            cid = "0x" + cid
        url = (
            f"{self.platform.data_api_host}/v1/market-positions"
            f"?market={urllib.parse.quote(cid)}"
            f"&user={urllib.parse.quote(self.runtime_cfg.proxy_address)}"
            f"&status=OPEN&limit=50"
        )
        try:
            data = _http_get_json(
                url,
                timeout=self.runtime_cfg.http_timeout_sec,
                user_agent=self.runtime_cfg.http_user_agent,
            )
        except Exception as e:
            logger.warning("fetch_market_positions failed: %s", e)
            return []
        flat: list[dict] = []
        for item in (data if isinstance(data, list) else []):
            if isinstance(item, dict) and "positions" in item:
                token = item.get("token") or item.get("asset")
                for pos in item.get("positions", []):
                    p = dict(pos)
                    if token and not p.get("asset"):
                        p["asset"] = token
                    p["token_id"] = p.get("asset") or token
                    flat.append(p)
            elif isinstance(item, dict) and (item.get("size") is not None or item.get("asset")):
                p = dict(item)
                p["token_id"] = p.get("asset") or p.get("token_id")
                flat.append(p)
        return flat

    def fetch_open_orders(self) -> list[dict]:
        """查询当前账户活跃挂单 (best-effort, 兼容不同 SDK 版本)."""
        if self.runtime_cfg.dry_run:
            return []
        if not self._real_inited:
            self.init_real_client()
        if self._real_client is None:
            return []
        candidates = ("get_open_orders", "get_orders", "get_active_orders")
        for name in candidates:
            fn = getattr(self._real_client, name, None)
            if fn is None:
                continue
            try:
                result = fn()
            except TypeError:
                try:
                    result = fn({"status": "LIVE"})
                except Exception:
                    continue
            except Exception:
                continue
            if isinstance(result, list):
                return [x for x in result if isinstance(x, dict)]
            if isinstance(result, dict):
                for k in ("data", "orders", "items", "results"):
                    v = result.get(k)
                    if isinstance(v, list):
                        return [x for x in v if isinstance(x, dict)]
        return []

    def redeem_capability_check(self) -> tuple[bool, str]:
        if self.runtime_cfg.dry_run:
            return False, "dry_run_mode"
        if not self.runtime_cfg.private_key:
            return False, "missing_private_key"
        if not self.runtime_cfg.proxy_address:
            return False, "missing_proxy_address"
        sig_type = int(self.runtime_cfg.signature_type)
        if sig_type in (1, 2):
            if not self.runtime_cfg.builder_api_key or not self.runtime_cfg.builder_secret or not self.runtime_cfg.builder_passphrase:
                return False, "missing_builder_credentials_for_relayer"
            ok, reason = self._init_relayer_client()
            return (ok, reason)
        if sig_type != 0:
            return False, f"unsupported_signature_type:{sig_type}"
        if not self._ensure_w3():
            return False, "web3_unavailable"
        try:
            from web3 import Web3  # type: ignore
            eoa = Web3().eth.account.from_key(str(self.runtime_cfg.private_key).strip()).address
            if eoa.lower() != str(self.runtime_cfg.proxy_address).strip().lower():
                return False, "proxy_not_equal_to_eoa"
        except Exception as e:
            return False, f"derive_eoa_failed:{type(e).__name__}"
        return True, "ok"

    def _init_relayer_client(self) -> tuple[bool, str]:
        if self._relayer_client is not None:
            return True, "ok"
        try:
            from py_builder_relayer_client.client import RelayClient  # type: ignore
            try:
                from py_builder_relayer_client.client import RelayerTxType  # type: ignore
            except ImportError:
                # PyPI 0.0.1 等旧版仅在 models 中暴露 RelayerTxType
                from py_builder_relayer_client.models import RelayerTxType  # type: ignore
            from py_builder_signing_sdk.config import BuilderConfig  # type: ignore
            from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds  # type: ignore
        except Exception as e:
            return False, (
                f"relayer_dependencies_missing:{type(e).__name__}:{e}"
                "; upgrade: pip install -U "
                "'git+https://github.com/Polymarket/py-builder-relayer-client.git'"
            )
        try:
            priv = str(self.runtime_cfg.private_key or "").strip()
            creds = BuilderApiKeyCreds(
                key=str(self.runtime_cfg.builder_api_key or "").strip(),
                secret=str(self.runtime_cfg.builder_secret or "").strip(),
                passphrase=str(self.runtime_cfg.builder_passphrase or "").strip(),
            )
            bcfg = BuilderConfig(local_builder_creds=creds)
            relay_type = RelayerTxType.PROXY if int(self.runtime_cfg.signature_type) == 1 else RelayerTxType.SAFE
            self._relayer_client = RelayClient(
                "https://relayer-v2.polymarket.com/",
                int(self.platform.chain_id),
                private_key=priv,
                builder_config=bcfg,
                relay_tx_type=relay_type,
                rpc_url=self.runtime_cfg.polygon_rpc_url,
            )
            return True, "ok"
        except Exception as e:
            return False, f"relayer_init_failed:{type(e).__name__}:{e}"

    def redeem_positions(self, *, condition_id: str, index_sets: Optional[list[int]] = None) -> tuple[bool, str, Optional[str]]:
        """调用 CTF redeemPositions 将已结算头寸兑回 USDC.

        注意: 当前仅支持 signature_type=0 且 proxy_address==EOA 的直接签名模式。
        """
        ok, reason = self.redeem_capability_check()
        if not ok:
            return False, reason, None
        if int(self.runtime_cfg.signature_type) in (1, 2):
            return self._redeem_positions_via_relayer(condition_id=condition_id, index_sets=index_sets)
        try:
            from web3 import Web3  # type: ignore
        except Exception as e:
            return False, f"web3_import_failed:{e}", None
        try:
            w3 = self._w3
            assert w3 is not None
            priv = str(self.runtime_cfg.private_key).strip()
            sender = w3.to_checksum_address(str(self.runtime_cfg.proxy_address).strip())
            ctf_addr = w3.to_checksum_address(self.platform.ctf_spender)
            collateral = w3.to_checksum_address(self.platform.pusd_address)
            cid = str(condition_id).strip()
            if cid and not cid.startswith("0x"):
                cid = "0x" + cid
            if len(cid) != 66:
                return False, f"bad_condition_id:{condition_id}", None
            parent_zero = "0x" + ("00" * 32)
            idxs = index_sets or [1, 2]
            ctf = w3.eth.contract(address=ctf_addr, abi=self._CTF_REDEEM_ABI)
            tx = ctf.functions.redeemPositions(
                collateral,
                parent_zero,
                cid,
                [int(x) for x in idxs],
            ).build_transaction({
                "from": sender,
                "nonce": w3.eth.get_transaction_count(sender, "pending"),
                "chainId": int(self.platform.chain_id),
                "gas": 300000,
                "gasPrice": w3.eth.gas_price,
                "value": 0,
            })
            signed = w3.eth.account.sign_transaction(tx, private_key=priv)
            tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
            receipt = w3.eth.wait_for_transaction_receipt(
                tx_hash,
                timeout=max(5.0, float(self.runtime_cfg.auto_redeem_receipt_wait_sec)),
            )
            th = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
            if int(getattr(receipt, "status", 0)) == 1:
                return True, "ok", th
            return False, "receipt_status_0", th
        except Exception as e:
            return False, f"redeem_failed:{type(e).__name__}:{e}", None

    def _redeem_positions_via_relayer(
        self,
        *,
        condition_id: str,
        index_sets: Optional[list[int]] = None,
    ) -> tuple[bool, str, Optional[str]]:
        ok, reason = self._init_relayer_client()
        if not ok:
            return False, reason, None
        try:
            from py_builder_relayer_client.models import Transaction  # type: ignore
            w3 = self._w3
            if w3 is None:
                return False, "web3_unavailable", None
            ctf_addr = w3.to_checksum_address(self.platform.ctf_spender)
            collateral = w3.to_checksum_address(self.platform.pusd_address)
            cid = str(condition_id).strip()
            if cid and not cid.startswith("0x"):
                cid = "0x" + cid
            if len(cid) != 66:
                return False, f"bad_condition_id:{condition_id}", None
            parent_zero = "0x" + ("00" * 32)
            idxs = [int(x) for x in (index_sets or [1, 2])]
            ctf_contract = w3.eth.contract(address=ctf_addr, abi=self._CTF_REDEEM_ABI)
            data = ctf_contract.encode_abi(
                abi_element_identifier="redeemPositions",
                args=[collateral, parent_zero, cid, idxs],
            )
            if isinstance(data, str):
                data_hex = data if data.startswith("0x") else ("0x" + data)
            elif isinstance(data, (bytes, bytearray)):
                data_hex = "0x" + bytes(data).hex()
            elif hasattr(data, "hex"):
                h = data.hex()
                data_hex = h if str(h).startswith("0x") else ("0x" + str(h))
            else:
                return False, f"redeem_encode_data_type:{type(data).__name__}", None
            tx_obj = Transaction(to=ctf_addr, data=data_hex, value="0")
            resp = self._relayer_client.execute([tx_obj], f"redeem:{cid[:12]}")
            waited = resp.wait()
            tx_hash = None
            if isinstance(waited, dict):
                tx_hash = waited.get("txHash") or waited.get("transactionHash")
            # py_builder_relayer_client: wait() 在 onchain failed 时返回 None（并由其 logger 打 error）
            if waited is None:
                if not tx_hash and hasattr(resp, "transaction_hash"):
                    tx_hash = getattr(resp, "transaction_hash", None)
                return False, "relayer_wait_failed_onchain", (str(tx_hash) if tx_hash else None)
            if not tx_hash and hasattr(resp, "transaction_hash"):
                tx_hash = getattr(resp, "transaction_hash", None)
            return True, "ok", (str(tx_hash) if tx_hash else None)
        except Exception as e:
            return False, f"relayer_redeem_failed:{type(e).__name__}:{e}", None

    def handle_unknown_api_error(self, err: Exception) -> None:
        with self._lock:
            self._consecutive_unknown_errors += 1
            count = self._consecutive_unknown_errors
        logger.error("unknown api error #%d: %s", count, err, exc_info=True)
        if self.runtime_cfg.abort_on_unknown_api_error:
            self._trip_circuit(f"unknown_api_error:{type(err).__name__}")
        elif count >= 5:
            self._trip_circuit("5_consecutive_unknown_errors")
