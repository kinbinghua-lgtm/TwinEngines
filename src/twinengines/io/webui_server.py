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

def live_command(root: Path) -> list[str]:
    python = root / ".venv" / "bin" / "python3"
    exe = str(python) if python.is_file() else sys.executable
    return [exe, "-m", "src.twinengines.cli", "live-run", "--enable-real", "--record-shadow-signals", "--artifact", "artifact_btc_v6.json"]

def strategy_status(root: Path) -> dict[str, Any]:
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
    st = strategy_status(root)
    if st.get("running"):
        return st | {"started": False, "reason": "already_running"}
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "data_runtime").mkdir(parents=True, exist_ok=True)
    log = open(root / "logs" / "live.log", "ab", buffering=0)
    env = os.environ.copy()
    env["TWINENGINES_ROOT"] = str(root)
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

def summary_payload(root: Path) -> dict[str, Any]:
    real = read_json(root / "data_runtime" / "real_balance.json") or {}; real_ok = bool(real.get("ok"))
    return {"ok": True, "real_balance_usdc": safe_float(real.get("balance_usdc")) if real_ok else None, "real_pending_redeem_usdc": safe_float(real.get("pending_redeem")) if real_ok else None, "real_redeem_ok": real.get("redeem_ok") if real else None, "shadow_equity_usdc": read_float(root / "data_runtime" / "sim_equity.txt")}

def current_decision_payload(root: Path) -> dict[str, Any]:
    cw = read_json(root / "data_runtime" / "current_window.json") or {}; sm = summary_payload(root); wid = str(cw.get("window_id") or "")
    T = safe_float(cw.get("T") or cw.get("T_remaining") or cw.get("t_remaining_sec")); best_dir = str(cw.get("best_dir") or "").lower()
    ask_up = safe_float(cw.get("ask_up")); ask_down = safe_float(cw.get("ask_down")); ask = ask_up if best_dir == "up" else ask_down if best_dir == "down" else None
    ev = safe_float(cw.get("fill_ev") or cw.get("best_ev") or cw.get("ev_rev") or cw.get("ev_trend")); ev_min = 0.05 if (T or 0) > 80 else (0.03 if (T or 0) > 30 else 0.02)
    real_target = safe_float(cw.get("real_target_quote")); shares = real_target / ask if real_target is not None and ask else None
    bal = sm.get("real_balance_usdc"); cap = float(bal) * 0.15 if bal is not None else None; real_status = str(cw.get("real_status") or "未提交")
    def c(k,l,s,t): return {"key": k, "label": l, "status": s, "text": t}
    real = [
        c("time","时间条件","pass" if T is not None and T >= 5 else "fail", f"T={T:.0f}s >= 5s" if T is not None else "无数据"),
        c("book","盘口条件","pass" if ask_up and ask_down else "fail", f"ask_up={ask_up}, ask_down={ask_down}"),
        c("ev","EV 条件","pass" if ev is not None and ev >= ev_min else "fail", f"EV={ev if ev is not None else '--'}，阈值={ev_min:.2f}"),
        c("reversal_d","reversal d_abs 条件","pass" if cw.get("r_d_ok") is True else "fail" if cw.get("r_d_ok") is False else "unknown", f"d_abs={cw.get('d_abs','--')}，d_cliff={cw.get('d_cliff','--')}"),
        c("reversal_p","reversal p 条件","pass" if cw.get("r_p_ok") is True else "fail" if cw.get("r_p_ok") is False else "unknown", f"p_rev={cw.get('p_lower') or cw.get('p_rev') or '--'}，p_min_r={cw.get('p_min_r','--')}"),
        c("kelly","Kelly 条件","pass" if real_target is not None and real_target >= 2.5 else "fail" if "Kelly<2.5" in real_status else "unknown", f"目标金额={real_target if real_target is not None else '--'}，最低=$2.50"),
        c("cap","资金上限","pass" if cap is not None and cap >= 2.5 else "fail" if cap is not None else "unknown", f"单窗口上限={cap:.2f}" if cap is not None else "真实余额不可用"),
        c("min_shares","最低 5 shares","pass" if shares is not None and shares >= 5 else "fail" if shares is not None else "unknown", f"目标 shares={shares:.2f}" if shares is not None else "尚无目标金额/价格"),
        c("submit","实盘状态","pass" if real_status == "real_fok_evaluated" else "warn", real_status),
    ]
    fail = next((x for x in real if x["status"] == "fail"), None)
    reason = "已提交/评估 FOK，等待订单审计确认" if real_status == "real_fok_evaluated" else ("未提交：Kelly<2.5，账户余额或 cap 不足" if "Kelly<2.5" in real_status else (f"未提交：{fail['label']}未满足" if fail else f"未提交：{real_status}"))
    fill = safe_float(cw.get("fill_amt") or cw.get("fill_amount")); shadow_status = str(cw.get("status") or "等待")
    shadow = [c("time","时间条件","pass" if T is not None and T >= 5 else "fail", f"T={T:.0f}s >= 5s" if T is not None else "无数据"), c("ev","EV 条件","pass" if ev is not None and ev >= ev_min else "fail", f"EV={ev if ev is not None else '--'}，阈值={ev_min:.2f}"), c("kelly","模拟 Kelly 条件","pass" if fill and fill >= 2.5 else "warn", f"影子 fill={fill if fill is not None else '--'}")]
    return {"ok": True, "window_id": wid, "window_label": window_label(wid), "seq": item_seq(cw), "prefix": item_seq(cw), "T_remaining": T, "td": cw.get("td"), "best_dir": best_dir, "ask_up": ask_up, "ask_down": ask_down, "best_ev": ev, "real": {"status": real_status, "reason": reason, "target_quote": real_target, "target_shares": shares, "conditions": real}, "shadow": {"status": shadow_status, "reason": str(cw.get("reason") or shadow_status), "fill_amount": fill, "ev": ev, "equity": sm.get("shadow_equity_usdc"), "conditions": shadow}, "source": "current_window.json + derived"}


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
        d = read_json(root / "data_runtime" / "current_window.json") or {}; wid = str(d.get("window_id") or "")
        d.update(summary_payload(root)); d.update({"ok": True, "window_label": window_label(wid), "source": "data_runtime/current_window.json"})
        return jsonify(d)
    @app.route("/api/current_decision")
    def current_decision(): return jsonify(current_decision_payload(root))
    @app.route("/api/summary")
    def summary(): return jsonify(summary_payload(root))

    @app.route("/api/real/orders")
    def real_orders():
        n = int(request.args.get("n", request.args.get("limit", "20")))
        items = filter_items(audit_rows(root, ("order_filled", "order_failed", "order_compliance_skip"), max(n * 4, n)), request.args)
        return jsonify({"ok": True, "items": items[:n], "source": "state.sqlite:audit_events"})
    @app.route("/api/real/results")
    def real_results():
        n = int(request.args.get("n", request.args.get("limit", "20"))); off = int(request.args.get("offset", "0"))
        raw = read_jsonl(root / "logs" / "real_results.jsonl", max(3000, n + off + 100)); items = filter_items(raw, request.args, real_cutoff=True); sm = summary_payload(root)
        return jsonify({"ok": True, "items": items[off:off+n], "stats": calc_stats(items, real_balance=sm.get("real_balance_usdc")), "source": "logs/real_results.jsonl", "cutoff_ts_ms": REAL_RESULTS_CUTOFF_TS_MS, "old_data_filtered": str(request.args.get("include_old") or "0") not in ("1", "true", "yes")})

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
        items = [{"source": "twinengines.log", "text": x} for x in tail(root / "logs" / "twinengines.log", n * 2)] + [{"source": "webui.log", "text": x} for x in tail(root / "logs" / "webui.log", n)]
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
        d = read_json(root / "data_runtime" / "deploy_version.json") or {}; d.setdefault("deployed", "unknown"); d["ok"] = True; return jsonify(d)
    return app

def run_webui(*, host: str = "0.0.0.0", port: int = 8080, password: Optional[str] = None, project_root: str = ".", **_: Any) -> int:
    create_app(root=Path(project_root).resolve(), password=password).run(host=host, port=int(port), debug=False); return 0

if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("project_root", nargs="?", default=str(ROOT)); p.add_argument("--host", default="0.0.0.0"); p.add_argument("--port", type=int, default=8080); p.add_argument("--password", default=None); a = p.parse_args(); run_webui(host=a.host, port=a.port, password=a.password, project_root=a.project_root)
