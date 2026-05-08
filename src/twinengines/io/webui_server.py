"""Single WebUI server for TwinEngines."""
from __future__ import annotations
import argparse, json, os, signal, sqlite3, subprocess, sys, time
from pathlib import Path
from typing import Any, Optional
sys.path.insert(0, os.environ.get("TWINENGINES_ROOT", os.path.abspath(".")))
from flask import Flask, jsonify, redirect, request, send_file

HERE = Path(__file__).resolve().parent
ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 and not str(sys.argv[1]).startswith("-") else Path.cwd().resolve()
STATIC = HERE / "webui_static"

def tail(path: Path, n: int) -> list[str]:
    if not path.is_file(): return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]

def read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        x = json.loads(path.read_text(encoding="utf-8"))
        return x if isinstance(x, dict) else None
    except Exception:
        return None

def read_float(path: Path) -> Optional[float]:
    try: return float(path.read_text(encoding="utf-8").strip())
    except Exception: return None

def read_jsonl(path: Path, n: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in tail(path, n):
        try:
            x = json.loads(line)
            if isinstance(x, dict): out.append(x)
        except Exception: pass
    out.reverse()
    return out

def safe_float(v: Any) -> Optional[float]:
    try: return None if v is None else float(v)
    except Exception: return None

def zh_action(action: Any) -> str:
    a = str(action or "").strip()
    return {
        "submitted": "已提交/已尝试提交",
        "already_attempted_this_condition": "本合约已尝试过，等待下一窗口",
        "waiting_minute3_close": "等待第 3 分钟收盘确认",
        "no_best_ask": "盘口没有可买价格",
        "skip_low_confidence": "方向置信度不足",
        "skip_no_edge": "EV/edge 不足",
        "order_submitted": "订单已提交",
        "order_filled": "订单已成交",
        "real_fok_evaluated": "已进入真实 FOK 评估",
    }.get(a, a or "实时策略运行中")

def zh_strategy_name(name: Any) -> str:
    s = str(name or "")
    return {
        "naked-third-digit-live": "实盘三位序列策略",
        "live-run": "实盘动态阈值策略",
    }.get(s, s or "未确定")

def git_version(root: Path) -> dict[str, Any]:
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(root), capture_output=True, text=True, timeout=3).stdout.strip()
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=str(root), capture_output=True, text=True, timeout=3).stdout.strip()
        subject = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=str(root), capture_output=True, text=True, timeout=3).stdout.strip()
        return {"deployed": head or "unknown", "commit": head, "branch": branch, "subject": subject}
    except Exception:
        return {"deployed": "unknown"}

def record_real_balance(root: Path, balance: float, *, source: str = "chain_usdc_pusd") -> None:
    try:
        path = root / "data_runtime" / "real_balance_history.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        now = int(time.time() * 1000)
        last_ts = 0
        if path.is_file():
            for line in tail(path, 1):
                try:
                    last_ts = int(json.loads(line).get("ts_ms") or 0)
                except Exception:
                    pass
        if now - last_ts < 15_000:
            return
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts_ms": now, "balance_usdc": float(balance), "source": source}, ensure_ascii=False) + "\n")
    except Exception:
        pass

def fetch_real_balance(root: Path) -> Optional[float]:
    cached = read_json(root / "data_runtime" / "real_balance.json") or {}
    ts_ms = safe_float(cached.get("ts_ms"))
    if cached.get("ok") and ts_ms and int(time.time() * 1000) - int(ts_ms) < 30_000:
        bal_cached = safe_float(cached.get("balance_usdc"))
        if bal_cached is not None:
            record_real_balance(root, bal_cached, source=str(cached.get("source") or "cache"))
        return bal_cached
    try:
        from .config import load_polymarket_runtime_cfg
        from .polymarket_client import PolymarketClient
        cfg = load_polymarket_runtime_cfg(str(root / ".env"))
        bal = PolymarketClient(cfg).fetch_account_equity_usdc()
        if bal is not None:
            out = {"ok": True, "balance_usdc": float(bal), "ts_ms": int(time.time() * 1000), "source": "chain_usdc_pusd"}
            try:
                (root / "data_runtime").mkdir(parents=True, exist_ok=True)
                (root / "data_runtime" / "real_balance.json").write_text(json.dumps(out), encoding="utf-8")
            except Exception:
                pass
            record_real_balance(root, float(bal))
            return float(bal)
    except Exception:
        pass
    bal = safe_float(cached.get("balance_usdc"))
    if bal is not None:
        record_real_balance(root, bal, source=str(cached.get("source") or "cache"))
    return bal

def balance_history_payload(root: Path, range_name: str) -> dict[str, Any]:
    fetch_real_balance(root)
    now = int(time.time() * 1000)
    ranges = {"15m": 15 * 60_000, "1h": 60 * 60_000, "6h": 6 * 60 * 60_000, "24h": 24 * 60 * 60_000, "all": None}
    span = ranges.get(str(range_name or "1h"), 60 * 60_000)
    cutoff = None if span is None else now - span
    raw_items = []
    for x in read_jsonl(root / "data_runtime" / "real_balance_history.jsonl", 8000):
        ts0 = item_ts(x)
        bal = safe_float(x.get("balance_usdc"))
        if ts0 is None or bal is None:
            continue
        if cutoff is not None and ts0 < cutoff:
            continue
        raw_items.append({"ts_ms": ts0, "balance_usdc": bal, "source": x.get("source")})
    raw_items.sort(key=lambda x: int(x["ts_ms"]))
    by_window: dict[int, dict[str, Any]] = {}
    for x in raw_items:
        ts0 = int(x["ts_ms"])
        current_win = (ts0 // 300_000) * 300_000
        sample_after = current_win + 180_000
        if ts0 < sample_after:
            continue
        settled_win = current_win - 300_000
        old = by_window.get(settled_win)
        if old is None or ts0 < int(old["ts_ms"]):
            y = dict(x)
            y["window_id"] = f"w{settled_win}"
            y["sample_window_id"] = f"w{current_win}"
            y["sample_rule"] = "next_window_start_plus_3min_first_balance"
            by_window[settled_win] = y
    items = [by_window[k] for k in sorted(by_window)]
    if len(items) > 300:
        step = max(1, len(items) // 300)
        items = items[::step] + ([] if items[-1] in items[::step] else [items[-1]])
    vals = [float(x["balance_usdc"]) for x in items]
    return {"ok": True, "range": range_name, "items": items, "raw_count": len(raw_items), "count": len(items), "count_note": "每个5分钟窗口取下一窗口开始后第3分钟之后的第一个余额点，避免未结算仓位影响曲线", "latest": vals[-1] if vals else None, "min": min(vals) if vals else None, "max": max(vals) if vals else None, "delta": (vals[-1] - vals[0]) if len(vals) >= 2 else 0.0, "source": "data_runtime/real_balance_history.jsonl sampled after settlement"}

def window_label(wid: str) -> Optional[str]:
    try:
        return time.strftime("%H:%M UTC", time.gmtime(int(str(wid)[1:]) / 1000.0)) if str(wid).startswith("w") else None
    except Exception:
        return None

def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False

def read_pid(path: Path) -> Optional[int]:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None

def live_pid_file(root: Path) -> Path:
    return root / "data_runtime" / "live.pid"

def naked_ticks_file(root: Path) -> Path:
    return root / "logs" / "naked_live_ticks.jsonl"

def latest_naked_tick(root: Path) -> Optional[dict[str, Any]]:
    ticks = [x for x in read_jsonl(naked_ticks_file(root), 200) if isinstance(x, dict)]
    if not ticks:
        return None
    for x in ticks:
        rep = x.get("rep") if isinstance(x.get("rep"), dict) else {}
        if isinstance(x.get("prediction"), dict) or isinstance(rep.get("prediction"), dict) or isinstance(x.get("quote_snapshot"), dict) or isinstance(rep.get("quote_snapshot"), dict):
            return x
    return ticks[0]

def systemd_strategy_status() -> dict[str, Any]:
    try:
        p = subprocess.run(["systemctl", "is-active", "twinengines-strategy"], capture_output=True, text=True, timeout=3)
        active = p.stdout.strip()
        q = subprocess.run(["systemctl", "show", "-p", "MainPID", "-p", "ExecStart", "--value", "twinengines-strategy"], capture_output=True, text=True, timeout=3)
        vals = q.stdout.splitlines()
        pid = 0
        exec_start = ""
        for line in vals:
            s = line.strip()
            if s.isdigit():
                pid = int(s)
            elif s:
                exec_start = s
        return {"available": True, "active": active, "running": active == "active" and pid > 0 and pid_alive(pid), "pid": pid if pid > 0 else None, "exec_start": exec_start}
    except Exception as e:
        return {"available": False, "active": "unknown", "running": False, "pid": None, "error": str(e)}

def live_command(root: Path) -> list[str]:
    python = root / ".venv" / "bin" / "python"
    exe = str(python) if python.is_file() else sys.executable
    return [exe, "-m", "twinengines.cli", "naked-third-digit-live", "--env-file", ".env", "--model-json", "reports/prefix_survival_model_90d_step10.json", "--yes-real-money", "--kelly-sizing", "--poll-sec", "8", "--confidence-min", "0.51", "--target-quote-usdc", "1", "--state-json", "data_runtime/naked_real_state.json", "--live-log-jsonl", "logs/naked_live_ticks.jsonl"]

def strategy_status(root: Path) -> dict[str, Any]:
    sd = systemd_strategy_status()
    if sd.get("available"):
        return {
            "ok": True,
            "running": bool(sd.get("running")),
            "pid": sd.get("pid") if sd.get("running") else None,
            "pid_file": "systemd:twinengines-strategy",
            "service": "twinengines-strategy",
            "service_state": sd.get("active"),
            "command": sd.get("exec_start") or "systemctl start twinengines-strategy",
        }
    pid = read_pid(live_pid_file(root))
    running = bool(pid and pid_alive(pid))
    return {
        "ok": True,
        "running": running,
        "pid": pid if running else None,
        "pid_file": str(live_pid_file(root)),
        "command": " ".join(live_command(root)),
    }

def stop_strategy(root: Path) -> dict[str, Any]:
    sd = systemd_strategy_status()
    if sd.get("available"):
        p = subprocess.run(["systemctl", "stop", "twinengines-strategy"], capture_output=True, text=True, timeout=20)
        return strategy_status(root) | {"stopped": True, "rc": p.returncode, "stderr": p.stderr[-500:]}
    pid = read_pid(live_pid_file(root))
    stopped: list[int] = []
    if pid and pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            stopped.append(pid)
        except Exception:
            pass
        deadline = time.time() + 5.0
        while time.time() < deadline and pid_alive(pid):
            time.sleep(0.2)
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
            except Exception:
                pass
    try:
        live_pid_file(root).unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True, "running": False, "stopped": stopped}

def start_strategy(root: Path) -> dict[str, Any]:
    sd = systemd_strategy_status()
    if sd.get("available"):
        if sd.get("running"):
            return strategy_status(root) | {"started": False, "reason": "already_running"}
        p = subprocess.run(["systemctl", "start", "twinengines-strategy"], capture_output=True, text=True, timeout=20)
        time.sleep(1.0)
        return strategy_status(root) | {"started": p.returncode == 0, "rc": p.returncode, "stderr": p.stderr[-500:]}
    st = strategy_status(root)
    if st.get("running"):
        return st | {"started": False, "reason": "already_running"}
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "data_runtime").mkdir(parents=True, exist_ok=True)
    log = open(root / "logs" / "strategy_stdout.log", "ab", buffering=0)
    env = os.environ.copy()
    env["TWINENGINES_ROOT"] = str(root / "src")
    env["PYTHONPATH"] = str(root / "src")
    proc = subprocess.Popen(live_command(root), cwd=str(root), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=(os.name != "nt"), env=env)
    live_pid_file(root).write_text(str(proc.pid), encoding="utf-8")
    time.sleep(0.5)
    return strategy_status(root) | {"started": True}

def audit_rows(root: Path, kinds: tuple[str, ...], n: int) -> list[dict[str, Any]]:
    db = root / "data_runtime" / "state.sqlite"
    if not db.is_file(): return []
    rows: list[dict[str, Any]] = []
    q = ",".join("?" for _ in kinds)
    try:
        con = sqlite3.connect(str(db), timeout=2.0)
        cur = con.execute(f"SELECT ts_ms,kind,payload FROM audit_events WHERE kind IN ({q}) ORDER BY id DESC LIMIT ?", (*kinds, n))
        for ts_ms, kind, payload in cur.fetchall():
            try: p = json.loads(payload)
            except Exception: p = {"_raw": payload}
            if not isinstance(p, dict): p = {"value": p}
            p = dict(p); p["ts_ms"] = int(ts_ms); p["kind"] = str(kind); rows.append(p)
        con.close()
    except Exception:
        pass
    return rows

REAL_RESULTS_CUTOFF_TS_MS = int(os.environ.get("REAL_RESULTS_CUTOFF_TS_MS", "1778078925000"))

def item_ts(x: dict[str, Any]) -> Optional[int]:
    for k in ("ts_ms", "settled_ts_ms", "created_ts_ms", "last_attempt_ts_ms"):
        v = x.get(k)
        try:
            if v is not None: return int(float(v))
        except Exception: pass
    wid = str(x.get("window_id") or "")
    try: return int(wid[1:]) if wid.startswith("w") else None
    except Exception: return None

def item_seq(x: dict[str, Any]) -> str:
    return str(x.get("seq") or x.get("prefix") or x.get("trigger_pattern") or x.get("pattern") or "")

def item_dir(x: dict[str, Any]) -> str:
    return str(x.get("direction") or x.get("dir") or x.get("best_dir") or x.get("side") or "").lower()

def item_amt(x: dict[str, Any]) -> Optional[float]:
    for k in ("fill_amount", "fill_amt", "size_usdc", "amount", "target_quote"):
        v = safe_float(x.get(k))
        if v is not None: return v
    return None

def item_pnl(x: dict[str, Any]) -> Optional[float]:
    for k in ("pnl", "pnl_usdc", "realized_pnl"):
        v = safe_float(x.get(k))
        if v is not None: return v
    return None

def item_won(x: dict[str, Any]) -> Optional[bool]:
    if isinstance(x.get("won"), bool): return bool(x.get("won"))
    r = str(x.get("result") or x.get("outcome") or "").lower()
    if r in ("win", "won", "true", "1"): return True
    if r in ("loss", "lost", "lose", "false", "0"): return False
    p = item_pnl(x)
    return None if p is None else p > 0

def range_cutoff(name: str) -> Optional[int]:
    now = int(time.time() * 1000)
    if name == "1h": return now - 3600_000
    if name == "6h": return now - 6 * 3600_000
    if name == "today":
        lt = time.localtime()
        return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst)) * 1000)
    return None

def filter_items(items: list[dict[str, Any]], args: Any, *, real_cutoff: bool = False) -> list[dict[str, Any]]:
    seq = str(args.get("seq") or "").strip().lower(); wid = str(args.get("window_id") or "").strip().lower()
    direction = str(args.get("direction") or "all").strip().lower(); result = str(args.get("result") or "all").strip().lower()
    q = str(args.get("q") or "").strip().lower(); cutoff = range_cutoff(str(args.get("range") or "all"))
    mn = safe_float(args.get("min_amount")); mx = safe_float(args.get("max_amount"))
    if real_cutoff and str(args.get("include_old") or "0") not in ("1", "true", "yes"):
        cutoff = max(cutoff or 0, REAL_RESULTS_CUTOFF_TS_MS)
    out: list[dict[str, Any]] = []
    for x in items:
        t = item_ts(x); amt = item_amt(x); won = item_won(x); hay = json.dumps(x, ensure_ascii=False).lower()
        if cutoff and (t is None or t < cutoff): continue
        if seq and seq not in item_seq(x).lower(): continue
        if wid and wid not in str(x.get("window_id") or "").lower(): continue
        if direction != "all" and direction not in item_dir(x): continue
        if result == "win" and won is not True: continue
        if result in ("loss", "lose") and won is not False: continue
        if mn is not None and (amt is None or amt < mn): continue
        if mx is not None and (amt is None or amt > mx): continue
        if q and q not in hay: continue
        y = dict(x); y.setdefault("ts_ms", t); y.setdefault("seq", item_seq(y)); y.setdefault("direction", item_dir(y)); y.setdefault("amount", amt); y.setdefault("pnl", item_pnl(y)); y.setdefault("won", won); out.append(y)
    return out

def calc_stats(items: list[dict[str, Any]], *, real_balance: Optional[float] = None, shadow_equity: Optional[float] = None) -> dict[str, Any]:
    settled = [x for x in items if item_won(x) is not None]
    wins = sum(1 for x in settled if item_won(x) is True)
    pnls = [item_pnl(x) for x in items if item_pnl(x) is not None]
    amts = [item_amt(x) for x in items if item_amt(x) is not None]
    return {"count": len(items), "settled_count": len(settled), "wins": wins, "losses": len(settled)-wins, "win_rate": wins/len(settled) if settled else None, "total_pnl": sum(pnls) if pnls else 0.0, "avg_pnl": sum(pnls)/len(pnls) if pnls else None, "avg_amount": sum(amts)/len(amts) if amts else None, "real_balance_usdc": real_balance, "shadow_equity_usdc": shadow_equity}

def coverage_stats(root: Path, args: Any, result_items: list[dict[str, Any]]) -> dict[str, Any]:
    now = int(time.time() * 1000); rg = str(args.get("range") or "6h")
    start = range_cutoff(rg)
    if start is None:
        ts_list = [item_ts(x) for x in result_items if item_ts(x)]
        start = min(ts_list) if ts_list else REAL_RESULTS_CUTOFF_TS_MS
    if str(args.get("include_old") or "0") not in ("1", "true", "yes"):
        start = max(start, REAL_RESULTS_CUTOFF_TS_MS)
    if str(args.get("window_id") or "").strip():
        total_windows = 1
    else:
        total_windows = max(1, int((now - start) / 300_000) + 1)
    orders = audit_rows(root, ("order_filled", "order_failed", "order_compliance_skip"), 5000) + naked_submitted_items(root, 5000)
    orders = filter_items(orders, args)
    if str(args.get("include_old") or "0") not in ("1", "true", "yes"):
        orders = [x for x in orders if (item_ts(x) or 0) >= REAL_RESULTS_CUTOFF_TS_MS]
    windows = {str(x.get("window_id") or "") for x in orders if x.get("window_id")}
    covered = len(windows)
    return {"coverage_windows": covered, "coverage_total_windows": total_windows, "coverage_rate": covered / total_windows if total_windows else None, "coverage_start_ts_ms": start, "coverage_end_ts_ms": now}

def naked_submitted_items(root: Path, n: int = 5000) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for x in read_jsonl(naked_ticks_file(root), n):
        if str(x.get("action") or "") != "submitted":
            continue
        rep = x.get("rep") if isinstance(x.get("rep"), dict) else {}
        pred = x.get("prediction") if isinstance(x.get("prediction"), dict) else rep.get("prediction") if isinstance(rep.get("prediction"), dict) else {}
        quote = x.get("quote_snapshot") if isinstance(x.get("quote_snapshot"), dict) else rep.get("quote_snapshot") if isinstance(rep.get("quote_snapshot"), dict) else {}
        plan = x.get("order_plan") if isinstance(x.get("order_plan"), dict) else {}
        ts_iso = str(x.get("ts_iso") or "")
        ts_ms = None
        try:
            from datetime import datetime
            ts_ms = int(datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).timestamp() * 1000) if ts_iso else None
        except Exception:
            ts_ms = item_ts(rep) or item_ts(quote)
        wms = rep.get("window_start_ms") or quote.get("window_start_ms")
        wid = f"w{int(wms)}" if wms is not None else str(rep.get("window_id") or "")
        side = str(quote.get("side") or plan.get("side_polymarket") or "").lower()
        item = {
            "ts_ms": ts_ms,
            "kind": "order_submitted",
            "window_id": wid,
            "seq": pred.get("current_prefix") or pred.get("prefix_bucket_used") or pred.get("trigger") or "",
            "direction": side,
            "amount": safe_float(plan.get("size_quote_usdc") or x.get("target_quote_usdc")),
            "size_usdc": safe_float(plan.get("size_quote_usdc") or x.get("target_quote_usdc")),
            "price": safe_float(plan.get("limit_price") or quote.get("best_ask")),
            "p_up": safe_float(pred.get("p_up")),
            "p_down": safe_float(pred.get("p_down")),
            "client_order_id": x.get("client_order_id"),
            "exchange_order_id": None,
            "status": "submitted",
            "state": "submitted",
            "error": None,
            "condition_id": rep.get("condition_id") or quote.get("condition_id"),
        }
        out.append(item)
    return out

def audit_real_result_items(root: Path, n: int = 3000) -> list[dict[str, Any]]:
    items = audit_rows(root, ("order_settled", "order_filled", "order_failed", "order_compliance_skip"), n)
    out: list[dict[str, Any]] = []
    for x in items:
        y = dict(x)
        kind = str(y.get("kind") or "")
        y.setdefault("status", "settled" if kind == "order_settled" else "filled" if kind == "order_filled" else "failed" if kind == "order_failed" else "skipped")
        y.setdefault("amount", item_amt(y))
        if y.get("pnl") is None:
            y["pnl"] = item_pnl(y)
        if y.get("won") is None:
            y["won"] = item_won(y)
        out.append(y)
    return out

def classify_order_error(x: dict[str, Any]) -> str:
    raw = str(x.get("error") or x.get("last_error") or x.get("state") or x.get("status") or "")
    msg = raw.lower()
    if str(x.get("kind") or "") == "order_settled":
        return "settled_win" if item_won(x) is True else "settled_loss" if item_won(x) is False else "settled"
    if str(x.get("kind") or "") == "order_filled":
        return "filled"
    if "fok_no_fill" in msg or "fully filled" in msg or "not filled" in msg or "couldn't be fully filled" in msg:
        return "fok_no_fill/liquidity_or_price"
    if "invalid amounts" in msg or "max accuracy" in msg or "precision" in msg:
        return "amount_precision"
    if "buy_below_floor" in msg:
        return "buy_below_floor"
    if "size_below_min" in msg or "min shares" in msg or "minimum" in msg:
        return "below_min_size"
    if "balance" in msg or "allowance" in msg or "not enough" in msg:
        return "balance_or_allowance"
    if "timeout" in msg or "unknown_after_order_query" in msg or "unknown_fok_outcome" in msg or "unknown_no_order_id" in msg:
        return "unknown_or_timeout"
    if "circuit_open" in msg:
        return "circuit_open"
    if raw:
        return "other_error"
    return "unknown"

def summary_payload(root: Path) -> dict[str, Any]:
    real_balance = fetch_real_balance(root)
    shadow_equity = read_float(root / "data_runtime" / "sim_equity.txt")
    return {"ok": True, "real_balance_usdc": real_balance, "real_pending_redeem_usdc": None, "real_redeem_ok": None, "shadow_equity_usdc": shadow_equity, "shadow_equity_note": None if shadow_equity is not None else "无影子账户数据"}

def current_decision_payload(root: Path) -> dict[str, Any]:
    tick = latest_naked_tick(root)
    if tick:
        rep = tick.get("rep") if isinstance(tick.get("rep"), dict) else tick
        pred = tick.get("prediction") if isinstance(tick.get("prediction"), dict) else rep.get("prediction") if isinstance(rep.get("prediction"), dict) else {}
        quote = tick.get("quote_snapshot") if isinstance(tick.get("quote_snapshot"), dict) else rep.get("quote_snapshot") if isinstance(rep.get("quote_snapshot"), dict) else {}
        book = tick.get("book") if isinstance(tick.get("book"), dict) else rep.get("book") if isinstance(rep.get("book"), dict) else {}
        sm = summary_payload(root)
        wid = str(rep.get("window_id") or rep.get("window_start_ms") or quote.get("window_start_ms") or "")
        window_id = f"w{wid}" if wid and not wid.startswith("w") and wid.isdigit() else wid
        action = str(tick.get("action") or rep.get("action") or "")
        side = str(quote.get("side") or "").lower()
        pred_up = pred.get("pred_up")
        best_dir = "up" if side == "up" or pred_up is True else "down" if side == "down" or pred_up is False else ""
        p_up = safe_float(pred.get("p_up")); p_down = safe_float(pred.get("p_down")); confidence = safe_float(pred.get("confidence_on_pick"))
        best_ask = safe_float(quote.get("best_ask") if quote else book.get("best_ask"))
        best_bid = safe_float(quote.get("best_bid") if quote else book.get("best_bid"))
        fair = safe_float(quote.get("fair_prob_side"))
        edge = safe_float(quote.get("edge_vs_best_ask"))
        T = safe_float(quote.get("seconds_to_expiry") or pred.get("residual_sec"))
        try:
            if window_id.startswith("w"):
                T = max(0.0, (int(window_id[1:]) + 300_000 - int(time.time() * 1000)) / 1000.0)
        except Exception:
            pass
        seq = str(pred.get("current_prefix") or pred.get("prefix_bucket_used") or pred.get("trigger") or "")
        seq_total = int(os.environ.get("WEBUI_SEQ_TOTAL", "3")); seq_display = (seq + "·" * max(0, seq_total - len(seq)))[:seq_total] if seq else "·" * seq_total
        status_map = {
            "waiting_minute3_close": "等待第 3 分钟收盘确认",
            "no_best_ask": "盘口没有可买价格",
            "skip_low_confidence": "方向置信度不足",
            "skip_no_edge": "EV/edge 不足",
            "submitted": "已提交/已尝试提交",
            "already_attempted_this_condition": "本合约已尝试过，等待下一窗口",
            "order_submitted": "订单已提交",
            "order_filled": "订单已成交",
        }
        reason = status_map.get(action, zh_action(action))
        if action == "waiting_minute3_close":
            reason = "等待第 3 分钟确认后开始评估"
        elif action == "no_best_ask":
            reason = "未提交：当前方向没有 best ask，不能发 FOK"
        elif best_ask is not None and edge is not None and edge < 0:
            reason = "未提交：报价相对模型公平价没有正 edge"
        real_status = zh_action(action or "running")
        def c(k,l,s,t): return {"key": k, "label": l, "status": s, "text": t}
        real = [
            c("service", "策略服务是否运行", "pass", "systemd twinengines-strategy active"),
            c("stage", "当前阶段", "warn" if action == "waiting_minute3_close" else "pass", reason),
            c("quote", "盘口报价", "pass" if best_ask is not None else "fail", f"side={side.upper() if side else '--'} best_bid={best_bid if best_bid is not None else '--'} best_ask={best_ask if best_ask is not None else '--'}"),
            c("confidence", "方向置信度", "pass" if confidence is not None and confidence >= 0.51 else "fail" if confidence is not None else "unknown", f"p_up={p_up if p_up is not None else '--'} p_down={p_down if p_down is not None else '--'} confidence={confidence if confidence is not None else '--'} 阈值=0.51"),
            c("edge", "EV/edge", "pass" if edge is not None and edge >= 0.08 else "fail" if edge is not None else "unknown", f"fair={fair if fair is not None else '--'} edge={edge if edge is not None else '--'} 需要>=0.08"),
        ]
        return {"ok": True, "window_id": window_id, "window_label": window_label(window_id), "seq": seq, "seq_display": seq_display, "seq_total": seq_total, "prefix": seq, "T_remaining": T, "server_ts_ms": int(time.time() * 1000), "p_up": p_up, "p_down": p_down, "ev_up": edge if best_dir == "up" else None, "ev_down": edge if best_dir == "down" else None, "best_dir": best_dir, "best_dir_label": best_dir.upper() if best_dir else "未确定", "decision_mode": zh_strategy_name("naked-third-digit-live"), "evaluated_direction": best_dir, "evaluated_direction_label": best_dir.upper() if best_dir else "未确定", "evaluated_ask": best_ask, "evaluated_bid": best_bid, "ask_up": best_ask if best_dir == "up" else None, "ask_down": best_ask if best_dir == "down" else None, "bid_up": best_bid if best_dir == "up" else None, "bid_down": best_bid if best_dir == "down" else None, "fair_prob_side": fair, "best_ev": edge, "real": {"status": real_status, "reason": reason, "target_quote": safe_float(tick.get("target_quote_usdc")), "target_shares": None, "filled_order": None, "conditions": real}, "shadow": {"status": "等待" if action == "waiting_minute3_close" else "同步实盘 tick", "reason": reason, "fill_amount": None, "ev": edge, "equity": sm.get("shadow_equity_usdc"), "conditions": []}, "source": "logs/naked_live_ticks.jsonl"}
    cw = read_json(root / "data_runtime" / "current_window.json") or {}; sm = summary_payload(root); wid = str(cw.get("window_id") or "")
    seq = item_seq(cw); seq_total = int(os.environ.get("WEBUI_SEQ_TOTAL", "3")); seq_display = (seq + "·" * max(0, seq_total - len(seq)))[:seq_total] if seq else "·" * seq_total
    filled = next((x for x in audit_rows(root, ("order_filled",), 80) if str(x.get("window_id") or "") == wid), None)
    T = safe_float(cw.get("T") or cw.get("T_remaining") or cw.get("t_remaining_sec")); best_dir = str(cw.get("best_dir") or "").lower()
    dir_label = best_dir.upper() if best_dir else "未确定"
    ask_up = safe_float(cw.get("ask_up")); ask_down = safe_float(cw.get("ask_down")); ask = ask_up if best_dir == "up" else ask_down if best_dir == "down" else None
    ev_up = safe_float(cw.get("ev_up")); ev_down = safe_float(cw.get("ev_down"))
    ev = safe_float(cw.get("best_ev"))
    if ev is None:
        ev = safe_float(cw.get("best_ev_simple"))
    if ev is None:
        ev = ev_up if best_dir == "up" else ev_down if best_dir == "down" else None
    ev_min = safe_float(cw.get("min_ev")) or 0.08
    p_up = safe_float(cw.get("p_up")); p_down = safe_float(cw.get("p_down")); best_prob = p_up if best_dir == "up" else p_down if best_dir == "down" else None
    real_target = safe_float(cw.get("real_target_quote")); shares = real_target / ask if real_target is not None and ask else None
    sizing_fraction = safe_float(cw.get("sizing_fraction")) or 0.20; max_stake_ratio = safe_float(cw.get("max_stake_ratio")) or 0.10
    bal = sm.get("real_balance_usdc"); cap = float(bal) * max_stake_ratio if bal is not None else None; real_status = str(cw.get("real_status") or "未提交")
    req_prob = safe_float(cw.get("req_prob"))
    req_edge = safe_float(cw.get("req_edge"))
    req_ev = safe_float(cw.get("req_ev"))
    req_kelly = safe_float(cw.get("req_kelly_raw"))
    best_edge = safe_float(cw.get("best_edge") or (best_prob - ask if best_prob is not None and ask is not None else None))
    best_kelly = safe_float(cw.get("best_kelly_raw"))
    trade_intent = str(cw.get("trade_intent") or "--")
    intent_reason = str(cw.get("intent_reason") or cw.get("reason") or "--")
    phase = cw.get("phase")
    phase_policy = str(cw.get("lifecycle_phase_policy") or "--")
    intent_allowed = cw.get("intent_allowed")
    def c(k,l,s,t): return {"key": k, "label": l, "status": s, "text": t}
    real = [
        c("phase","当前生命周期阶段","pass", f"Phase={phase}；意图={trade_intent}；策略={phase_policy}"),
        c("book","盘口是否能报价","pass" if ask_up and ask_down else "fail", f"UP 买价={ask_up}，DOWN 买价={ask_down}；当前选中 {dir_label}，价格={ask}"),
        c("intent","阶段-意图门控是否通过","pass" if intent_allowed is True else "fail" if intent_allowed is False else "unknown", f"{intent_reason}"),
        c("prob","动态概率条件","pass" if req_prob is not None and best_prob is not None and best_prob >= req_prob else "fail" if req_prob is not None and best_prob is not None else "unknown", f"当前={best_prob if best_prob is not None else '--'}；本阶段/意图要求 >= {req_prob if req_prob is not None else '--'}"),
        c("edge","edge 条件","pass", f"已取消准入限制；仅展示当前 edge={best_edge if best_edge is not None else '--'}"),
        c("ev","动态 EV 条件","pass" if req_ev is not None and ev is not None and ev >= req_ev else "fail" if req_ev is not None and ev is not None else "unknown", f"UP EV={ev_up if ev_up is not None else '--'}；DOWN EV={ev_down if ev_down is not None else '--'}；当前={ev if ev is not None else '--'}；要求 >= {req_ev if req_ev is not None else '--'}"),
        c("kelly_raw","动态 KellyRaw 条件","pass" if req_kelly is not None and best_kelly is not None and best_kelly >= req_kelly else "fail" if req_kelly is not None and best_kelly is not None else "unknown", f"当前={best_kelly if best_kelly is not None else '--'}；要求 >= {req_kelly if req_kelly is not None else '--'}"),
        c("kelly","真实账户建议下注额是否够最小单","pass" if real_target is not None and real_target >= 2.5 else "fail" if "Kelly<2.5" in real_status else "unknown", f"按真实余额和胜率算，{dir_label} 建议下注={real_target if real_target is not None else '--'}；低于 $2.50 不下"),
        c("cap","账户资金上限是否允许下单","pass" if cap is not None and cap >= 2.5 else "fail" if cap is not None else "unknown", f"当前档位 {cw.get('sizing_tier') or 'base'}：Kelly fraction={sizing_fraction:.2f}，单窗口最多用真实余额的 {max_stake_ratio:.0%}，当前上限={cap:.2f}，至少要覆盖 $2.50" if cap is not None else "真实余额不可用"),
        c("min_shares","是否满足 Polymarket 最少 5 shares","pass" if shares is not None and shares >= 5 else "fail" if shares is not None else "unknown", f"按 {dir_label} 当前价格估算可买 shares={shares:.2f}；少于 5 shares 不提交" if shares is not None else "还没有目标金额或本方向价格"),
        c("submit","是否已经进入真实 FOK 下单流程","pass" if real_status == "real_fok_evaluated" else "warn", f"当前真实盘状态：{real_status}；只有前面条件都过才会提交 FOK"),
    ]
    fail = next((x for x in real if x["status"] == "fail"), None)
    if real_status == "real_fok_evaluated": reason = "已进入真实 FOK 下单评估，等待订单审计确认"
    elif filled: reason = "真实 FOK 已成交"
    elif T is not None and T < 15: reason = "未提交：当前窗口剩余时间少于 15 秒"
    elif "Kelly<2.5" in real_status: reason = "未提交：建议下注金额低于 $2.50，或账户余额/cap 不足"
    else: reason = f"未提交：{fail['label']}未通过" if fail else f"未提交：{real_status}"
    fill = safe_float(cw.get("fill_amt") or cw.get("fill_amount")); shadow_status = str(cw.get("status") or "等待")
    shadow = [c("time","时间条件","pass" if T is not None and T >= 15 else "fail", f"T={T:.0f}s >= 15s" if T is not None else "无数据"), c("intent","阶段-意图门控", "pass" if intent_allowed is True else "fail" if intent_allowed is False else "unknown", f"{intent_reason}"), c("ev","动态 EV 条件","pass" if req_ev is not None and ev is not None and ev >= req_ev else "fail" if req_ev is not None and ev is not None else "unknown", f"EV={ev if ev is not None else '--'}，当前要求={req_ev if req_ev is not None else '--'}"), c("kelly","模拟 Kelly 条件","pass" if fill and fill >= 2.5 else "warn", f"影子 fill={fill if fill is not None else '--'}")]
    return {"ok": True, "window_id": wid, "window_label": window_label(wid), "seq": seq, "seq_display": seq_display, "seq_total": seq_total, "prefix": seq, "T_remaining": T, "server_ts_ms": int(time.time() * 1000), "p_up": p_up, "p_down": p_down, "ev_up": ev_up, "ev_down": ev_down, "best_dir": best_dir, "best_dir_label": dir_label, "decision_mode": "方向概率", "evaluated_direction": best_dir, "evaluated_direction_label": dir_label, "evaluated_ask": ask, "ask_up": ask_up, "ask_down": ask_down, "best_ev": ev, "real": {"status": real_status, "reason": reason, "target_quote": real_target, "target_shares": shares, "filled_order": filled, "conditions": real}, "shadow": {"status": shadow_status, "reason": str(cw.get("reason") or shadow_status), "fill_amount": fill, "ev": ev, "equity": sm.get("shadow_equity_usdc"), "conditions": shadow}, "source": "current_window.json + derived"}


def create_app(*, root: Path, password: Optional[str] = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.secret_key = os.environ.get("WEBUI_SECRET", "te-webui-") + str(os.getpid())

    @app.route("/")
    @app.route("/real")
    @app.route("/shadow")
    @app.route("/logs")
    def index(): return send_file(str(STATIC / "index.html"))
    @app.route("/static/<path:filename>")
    def static_files(filename): return send_file(str(STATIC / filename))
    @app.route("/healthz")
    def healthz(): return jsonify({"ok": True, "root": str(root), "ts_ms": int(time.time() * 1000)})

    @app.route("/api/strategy/status")
    def api_strategy_status(): return jsonify(strategy_status(root))
    @app.route("/api/strategy/start", methods=["POST"])
    def api_strategy_start(): return jsonify(start_strategy(root))
    @app.route("/api/strategy/stop", methods=["POST"])
    def api_strategy_stop(): return jsonify(stop_strategy(root))

    @app.route("/api/current_window")
    def current_window():
        tick = latest_naked_tick(root)
        if tick:
            rep = tick.get("rep") if isinstance(tick.get("rep"), dict) else tick
            pred = tick.get("prediction") if isinstance(tick.get("prediction"), dict) else rep.get("prediction") if isinstance(rep.get("prediction"), dict) else {}
            quote = tick.get("quote_snapshot") if isinstance(tick.get("quote_snapshot"), dict) else rep.get("quote_snapshot") if isinstance(rep.get("quote_snapshot"), dict) else {}
            wid_raw = str(rep.get("window_id") or rep.get("window_start_ms") or quote.get("window_start_ms") or "")
            wid = f"w{wid_raw}" if wid_raw and not wid_raw.startswith("w") and wid_raw.isdigit() else wid_raw
            best_dir = str(quote.get("side") or "").lower()
            d = {
                "ok": True,
                "window_id": wid,
                "window_label": window_label(wid),
                "source": "logs/naked_live_ticks.jsonl",
                "status": tick.get("action") or rep.get("action"),
                "reason": tick.get("action") or rep.get("action"),
                "seq": pred.get("current_prefix") or pred.get("prefix_bucket_used") or pred.get("trigger"),
                "best_dir": best_dir,
                "p_up": pred.get("p_up"),
                "p_down": pred.get("p_down"),
                "T": max(0.0, (int(wid[1:]) + 300_000 - int(time.time() * 1000)) / 1000.0) if wid.startswith("w") else (quote.get("seconds_to_expiry") or pred.get("residual_sec")),
                "ask_up": quote.get("best_ask") if best_dir == "up" else None,
                "ask_down": quote.get("best_ask") if best_dir == "down" else None,
                "best_ask": quote.get("best_ask"),
                "best_bid": quote.get("best_bid"),
                "best_ev": quote.get("edge_vs_best_ask"),
                "fair_prob_side": quote.get("fair_prob_side"),
                "market_question": rep.get("market_question"),
                "condition_id": rep.get("condition_id") or quote.get("condition_id"),
            }
            d.update(summary_payload(root))
            return jsonify(d)
        d = read_json(root / "data_runtime" / "current_window.json") or {}; wid = str(d.get("window_id") or "")
        d.update(summary_payload(root)); d.update({"ok": True, "window_label": window_label(wid), "source": "data_runtime/current_window.json"})
        return jsonify(d)
    @app.route("/api/current_decision")
    def current_decision(): return jsonify(current_decision_payload(root))
    @app.route("/api/summary")
    def summary(): return jsonify(summary_payload(root))
    @app.route("/api/real/balance_history")
    def real_balance_history(): return jsonify(balance_history_payload(root, str(request.args.get("range") or "1h")))

    @app.route("/api/real/orders")
    def real_orders():
        n = int(request.args.get("n", request.args.get("limit", "20")))
        items = filter_items(audit_rows(root, ("order_settled", "order_filled", "order_failed", "order_compliance_skip"), max(n * 4, n)), request.args)
        for x in items:
            x["error_class"] = classify_order_error(x)
        counts: dict[str, int] = {}
        for x in items:
            k = str(x.get("error_class") or "unknown")
            counts[k] = counts.get(k, 0) + 1
        return jsonify({"ok": True, "items": items[:n], "error_classes": sorted(counts.items(), key=lambda kv: kv[1], reverse=True), "source": "state.sqlite:audit_events"})
    @app.route("/api/real/results")
    def real_results():
        n = int(request.args.get("n", request.args.get("limit", "20"))); off = int(request.args.get("offset", "0"))
        raw_file = read_jsonl(root / "logs" / "real_results.jsonl", max(3000, n + off + 100))
        raw = raw_file if raw_file else (audit_real_result_items(root, 5000) + naked_submitted_items(root, 5000))
        rank = {"order_settled": 5, "order_filled": 4, "order_failed": 3, "order_compliance_skip": 2, "order_submitted": 1}
        picked: dict[str, dict[str, Any]] = {}
        for x in raw:
            key = str(x.get("client_order_id") or x.get("exchange_order_id") or f"{x.get('kind')}:{x.get('window_id')}:{x.get('direction')}:{item_ts(x)}")
            old = picked.get(key)
            if old is None or rank.get(str(x.get("kind") or ""), 0) > rank.get(str(old.get("kind") or ""), 0) or (rank.get(str(x.get("kind") or ""), 0) == rank.get(str(old.get("kind") or ""), 0) and (item_ts(x) or 0) > (item_ts(old) or 0)):
                picked[key] = x
        raw = sorted(picked.values(), key=lambda z: item_ts(z) or 0, reverse=True)
        items = filter_items(raw, request.args, real_cutoff=True)
        sm = summary_payload(root); st = calc_stats(items, real_balance=sm.get("real_balance_usdc")); st.update(coverage_stats(root, request.args, items))
        return jsonify({"ok": True, "items": items[off:off+n], "stats": st, "source": "logs/real_results.jsonl" if raw_file else "state.sqlite:audit_events", "cutoff_ts_ms": REAL_RESULTS_CUTOFF_TS_MS, "old_data_filtered": str(request.args.get("include_old") or "0") not in ("1", "true", "yes")})

    @app.route("/api/shadow/orders")
    def shadow_orders():
        n = int(request.args.get("n", request.args.get("limit", "20"))); off = int(request.args.get("offset", "0"))
        raw = read_jsonl(root / "logs" / "shadow_orders.jsonl", max(3000, n + off + 100))
        for x in raw: x.setdefault("fill_amount", x.get("fill_amt") or x.get("kelly_stake"))
        items = filter_items(raw, request.args); sm = summary_payload(root)
        return jsonify({"ok": True, "items": items[off:off+n], "stats": calc_stats(items, shadow_equity=sm.get("shadow_equity_usdc")), "source": "logs/shadow_orders.jsonl"})
    @app.route("/api/shadow/results")
    def shadow_results():
        n = int(request.args.get("n", request.args.get("limit", "20"))); off = int(request.args.get("offset", "0"))
        raw = [x for x in read_jsonl(root / "logs" / "window_results.jsonl", max(4000, n + off + 100)) if str(x.get("mode") or "shadow").lower() != "real"]
        items = filter_items(raw, request.args); sm = summary_payload(root)
        return jsonify({"ok": True, "items": items[off:off+n], "stats": calc_stats(items, shadow_equity=sm.get("shadow_equity_usdc")), "source": "logs/window_results.jsonl:mode!=real"})

    @app.route("/api/logs")
    def logs():
        n = int(request.args.get("n", "200")); q = str(request.args.get("q") or "").lower().strip(); level = str(request.args.get("level") or "all").lower()
        items = [{"source": "twinengines.log", "text": x} for x in tail(root / "logs" / "twinengines.log", n * 2)] + [{"source": "strategy_stdout.log", "text": x} for x in tail(root / "logs" / "strategy_stdout.log", n)] + [{"source": "naked_live_ticks.jsonl", "text": x} for x in tail(root / "logs" / "naked_live_ticks.jsonl", n)] + [{"source": "webui.log", "text": x} for x in tail(root / "logs" / "webui.log", n)]
        if level in ("error", "warning", "warn"):
            ks = ["error"] if level == "error" else ["warning", "warn"]; items = [x for x in items if any(k in str(x.get("text") or "").lower() for k in ks)]
        if q: items = [x for x in items if q in (str(x.get("text") or "") + " " + str(x.get("source") or "")).lower()]
        return jsonify({"ok": True, "items": items[-n:]})
    @app.route("/api/audit")
    def audit():
        n = int(request.args.get("n", "100")); kinds = tuple(x.strip() for x in str(request.args.get("kinds") or "order_filled,order_failed,order_compliance_skip").split(",") if x.strip())
        return jsonify({"ok": True, "items": filter_items(audit_rows(root, kinds, max(n * 3, n)), request.args)[:n], "source": "state.sqlite:audit_events"})
    @app.route("/api/version")
    def version():
        d = read_json(root / "data_runtime" / "deploy_version.json") or git_version(root); d.setdefault("deployed", "unknown"); d["ok"] = True; return jsonify(d)
    return app

def run_webui(*, host: str = "0.0.0.0", port: int = 8080, password: Optional[str] = None, project_root: str = ".", **_: Any) -> int:
    create_app(root=Path(project_root).resolve(), password=password).run(host=host, port=int(port), debug=False); return 0

if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("project_root", nargs="?", default=str(ROOT)); p.add_argument("--host", default="0.0.0.0"); p.add_argument("--port", type=int, default=8080); p.add_argument("--password", default=None); a = p.parse_args(); run_webui(host=a.host, port=a.port, password=a.password, project_root=a.project_root)
