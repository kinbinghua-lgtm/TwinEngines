"""
回测可选: 用影子日志里的真实盘口快照替换模型成交价。

支持两种模式:
- exact: 仅当 (ts_ms, side) 精确命中快照时替换
- simulate: 用快照经验分布外推 48 天 (按 side 采样可成交率/价格比例)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .polymarket import TradeQuote


@dataclass(frozen=True)
class ShadowBookSnapshot:
    ts_ms: int
    side: str
    best_ask: Optional[float]
    eff_cap: Optional[float]
    likely_executable: Optional[bool]
    reason: str


@dataclass
class ShadowBookOverlay:
    by_key: dict[tuple[int, str], ShadowBookSnapshot] = field(default_factory=dict)
    simulate_enabled: bool = False
    exec_ratio_pool: dict[str, list[float]] = field(default_factory=dict)
    reject_reason_pool: dict[str, list[str]] = field(default_factory=dict)
    exec_prob_by_side: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_jsonl(
        cls,
        path: str | Path,
        *,
        mode: str = "exact",
    ) -> "ShadowBookOverlay":
        p = Path(path)
        out = cls()
        if not p.is_file():
            return out
        rows: list[ShadowBookSnapshot] = []
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(ev.get("kind")) != "shadow_signal":
                continue
            ts_ms = int(ev.get("ts_ms") or 0)
            side = str(ev.get("side") or "")
            poly = ev.get("polymarket") or {}
            book = poly.get("book") or {}
            opp = poly.get("opportunity") or {}
            tquote = poly.get("trade_quote") or {}
            best_ask = _as_float(book.get("best_ask"))
            eff_cap = _as_float(tquote.get("effective_price"))
            snap = ShadowBookSnapshot(
                ts_ms=ts_ms,
                side=side,
                best_ask=best_ask,
                eff_cap=eff_cap,
                likely_executable=(None if opp.get("likely_executable") is None else bool(opp.get("likely_executable"))),
                reason=str(opp.get("reason") or ""),
            )
            out.by_key[(ts_ms, side)] = snap
            rows.append(snap)
        if str(mode).lower() == "simulate":
            out._build_simulation_pools(rows)
            out.simulate_enabled = True
        return out

    def evaluate(
        self,
        *,
        ts_ms: int,
        side: str,
        model_quote: TradeQuote,
    ) -> dict:
        if self.simulate_enabled:
            return self._evaluate_simulated(ts_ms=ts_ms, side=side, model_quote=model_quote)
        snap = self.by_key.get((int(ts_ms), str(side)))
        if snap is None:
            return {
                "matched": False,
                "executable": None,
                "reason": "no_snapshot",
                "quote": model_quote,
            }
        ask = snap.best_ask
        cap = snap.eff_cap
        if ask is None:
            return {
                "matched": True,
                "executable": False,
                "reason": "no_best_ask",
                "quote": None,
            }
        if cap is not None and ask > cap:
            return {
                "matched": True,
                "executable": False,
                "reason": f"ask_too_high ask={ask:.4f} cap={cap:.4f}",
                "quote": None,
            }
        replaced = TradeQuote(
            side=model_quote.side,
            raw_price=model_quote.raw_price,
            effective_price=float(ask),
            payoff_gross=(1.0 / float(ask) - 1.0) if ask > 0 else 0.0,
            fill_ratio=model_quote.fill_ratio,
            slippage_bps=model_quote.slippage_bps,
            rebound=model_quote.rebound,
            oracle_drag=model_quote.oracle_drag,
        )
        return {
            "matched": True,
            "executable": True,
            "reason": "book_replace_ok",
            "quote": replaced,
        }

    def _build_simulation_pools(self, rows: list[ShadowBookSnapshot]) -> None:
        by_side_exec_ratio: dict[str, list[float]] = {}
        by_side_rejects: dict[str, list[str]] = {}
        by_side_count: dict[str, int] = {}
        by_side_exec: dict[str, int] = {}
        for r in rows:
            side = str(r.side or "").upper()
            if not side:
                continue
            by_side_count[side] = by_side_count.get(side, 0) + 1
            if (
                r.likely_executable is True
                and r.best_ask is not None
                and r.eff_cap is not None
                and r.eff_cap > 0
            ):
                ratio = float(r.best_ask) / float(r.eff_cap)
                by_side_exec_ratio.setdefault(side, []).append(ratio)
                by_side_exec[side] = by_side_exec.get(side, 0) + 1
            elif r.likely_executable is False:
                by_side_rejects.setdefault(side, []).append(str(r.reason or "rejected"))
        self.exec_ratio_pool = by_side_exec_ratio
        self.reject_reason_pool = by_side_rejects
        probs: dict[str, float] = {}
        for side, n in by_side_count.items():
            ok = by_side_exec.get(side, 0)
            probs[side] = (ok / n) if n > 0 else 0.0
        self.exec_prob_by_side = probs

    def _evaluate_simulated(
        self,
        *,
        ts_ms: int,
        side: str,
        model_quote: TradeQuote,
    ) -> dict:
        side_u = str(side or "").upper()
        p_exec = self.exec_prob_by_side.get(side_u)
        if p_exec is None:
            return {"matched": False, "executable": None, "reason": "no_side_pool", "quote": model_quote}
        u = _u01(int(ts_ms), side_u)
        matched = True
        if u > p_exec:
            reasons = self.reject_reason_pool.get(side_u) or ["sim_reject"]
            idx = int(u * len(reasons)) % len(reasons)
            return {
                "matched": matched,
                "executable": False,
                "reason": f"simulated:{reasons[idx]}",
                "quote": None,
            }
        ratios = self.exec_ratio_pool.get(side_u) or [1.0]
        idx = int(u * len(ratios)) % len(ratios)
        ratio = max(0.01, min(2.0, float(ratios[idx])))
        ask = float(model_quote.effective_price) * ratio
        replaced = TradeQuote(
            side=model_quote.side,
            raw_price=model_quote.raw_price,
            effective_price=ask,
            payoff_gross=(1.0 / ask - 1.0) if ask > 0 else 0.0,
            fill_ratio=model_quote.fill_ratio,
            slippage_bps=model_quote.slippage_bps,
            rebound=model_quote.rebound,
            oracle_drag=model_quote.oracle_drag,
        )
        return {
            "matched": matched,
            "executable": True,
            "reason": f"simulated:ratio={ratio:.4f}",
            "quote": replaced,
        }


def _as_float(v) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _u01(ts_ms: int, side: str) -> float:
    side_sig = 0
    for ch in side:
        side_sig = ((side_sig * 131) + ord(ch)) & 0xFFFFFFFF
    seed = int(ts_ms) ^ side_sig
    x = (1103515245 * (seed & 0x7FFFFFFF) + 12345) & 0x7FFFFFFF
    return float(x) / float(0x7FFFFFFF)

