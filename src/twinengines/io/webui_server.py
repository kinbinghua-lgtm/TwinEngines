"""Single WebUI server for TwinEngines."""
from __future__ import annotations
import argparse, json, os, signal, sqlite3, subprocess, sys, time
from pathlib import Path
from typing import Any, Optional
sys.path.insert(0, os.environ.get("TWINENGINES_ROOT", os.path.abspath(".")))
from flask import Flask, jsonify, request, send_file

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

def create_app(*, root: Path, password: Optional[str] = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.secret_key = os.environ.get("WEBUI_SECRET", "te-webui-") + str(os.getpid())

    @app.route("/")
    def index(): return send_file(str(STATIC / "index.html"))
    @app.route("/static/<path:filename>")
    def static_files(filename): return send_file(str(STATIC / filename))
    @app.route("/real")
    def real(): return redirect("/")
    @app.route("/healthz")
    def healthz(): return jsonify({"ok": True, "root": str(root), "ts_ms": int(time.time() * 1000)})

    @app.route("/api/strategy/status")
    def api_strategy_status():
        return jsonify(strategy_status(root))

    @app.route("/api/strategy/start", methods=["POST"])
    def api_strategy_start():
        return jsonify(start_strategy(root))

    @app.route("/api/strategy/stop", methods=["POST"])
    def api_strategy_stop():
        return jsonify(stop_strategy(root))

    @app.route("/api/current_window")
    def current_window():
        d = read_json(root / "data_runtime" / "current_window.json") or {}
        wid = str(d.get("window_id") or "")
        d.update({"ok": True, "window_label": window_label(wid), "source": "data_runtime/current_window.json"})
        return jsonify(d)

    @app.route("/api/summary")
    def summary():
        real = read_json(root / "data_runtime" / "real_balance.json") or {}
        real_ok = bool(real.get("ok"))
        return jsonify({
            "ok": True,
            "real_balance_usdc": safe_float(real.get("balance_usdc")) if real_ok else None,
            "real_pending_redeem_usdc": safe_float(real.get("pending_redeem")) if real_ok else None,
            "real_redeem_ok": real.get("redeem_ok") if real else None,
            "shadow_equity_usdc": read_float(root / "data_runtime" / "sim_equity.txt"),
        })

    @app.route("/api/real/orders")
    def real_orders(): return jsonify({"ok": True, "items": audit_rows(root, ("order_filled", "order_failed"), int(request.args.get("n", "50"))), "source": "state.sqlite:audit_events"})
    @app.route("/api/real/results")
    def real_results(): return jsonify({"ok": True, "items": read_jsonl(root / "logs" / "real_results.jsonl", int(request.args.get("n", "80"))), "source": "logs/real_results.jsonl"})

    @app.route("/api/shadow/orders")
    def shadow_orders():
        items = read_jsonl(root / "logs" / "shadow_orders.jsonl", int(request.args.get("n", "80")))
        for x in items: x.setdefault("fill_amount", x.get("fill_amt") or x.get("kelly_stake"))
        return jsonify({"ok": True, "items": items, "source": "logs/shadow_orders.jsonl"})

    @app.route("/api/shadow/results")
    def shadow_results():
        n = int(request.args.get("n", "80")); raw = read_jsonl(root / "logs" / "window_results.jsonl", max(n * 3, n))
        return jsonify({"ok": True, "items": [x for x in raw if str(x.get("mode") or "shadow").lower() != "real"][:n], "source": "logs/window_results.jsonl:mode!=real"})

    @app.route("/api/logs")
    def logs():
        n = int(request.args.get("n", "160"))
        items = [{"source": "twinengines.log", "text": x} for x in tail(root / "logs" / "twinengines.log", n // 2 + 1)]
        items += [{"source": "webui.log", "text": x} for x in tail(root / "logs" / "webui.log", n // 2 + 1)]
        return jsonify({"ok": True, "items": items[-n:]})

    @app.route("/api/version")
    def version():
        d = read_json(root / "data_runtime" / "deploy_version.json") or {}; d.setdefault("deployed", "unknown"); d["ok"] = True; return jsonify(d)
    return app

def run_webui(*, host: str = "0.0.0.0", port: int = 8080, password: Optional[str] = None, project_root: str = ".", **_: Any) -> int:
    create_app(root=Path(project_root).resolve(), password=password).run(host=host, port=int(port), debug=False); return 0

if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("project_root", nargs="?", default=str(ROOT)); p.add_argument("--host", default="0.0.0.0"); p.add_argument("--port", type=int, default=8080); p.add_argument("--password", default=None); a = p.parse_args(); run_webui(host=a.host, port=a.port, password=a.password, project_root=a.project_root)
