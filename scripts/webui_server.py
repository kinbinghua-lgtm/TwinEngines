"""Standalone WebUI server for TwinEngines."""
from pathlib import Path
import json, os, sys

sys.path.insert(0, os.environ.get("TWINENGINES_ROOT", os.path.abspath(".")))

from flask import Flask, jsonify, request, send_file

HERE = Path(__file__).resolve().parent
STATIC = HERE / "webui_static"
ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE

app = Flask(__name__, static_folder=None)

@app.route("/")
def index():
    return send_file(str(STATIC / "index.html"))

@app.route("/real")
def real_index():
    return send_file(str(STATIC / "real.html"))

@app.route("/static/<path:path>")
def static_files(path):
    f = STATIC / path
    if not f.exists():
        return "Not found", 404
    ct = "text/css" if path.endswith(".css") else "text/javascript" if path.endswith(".js") else "text/html"
    return send_file(str(f), mimetype=ct)

@app.route("/api/shadow_orders")
def api_orders():
    n = int(request.args.get("n", "50"))
    p = ROOT / "logs" / "shadow_orders.jsonl"
    items = []
    if p.exists():
        with open(p, encoding="utf-8") as f:
            for line in f.readlines()[-n:]:
                try: items.append(json.loads(line.strip()))
                except: pass
    result = []
    for i in items:
        result.append({
            "window_id": i.get("window_id",""),
            "trigger_pattern": i.get("trigger_pattern",""),
            "best_dir": i.get("best_dir",""),
            "p_adj": round(i.get("p_adj",0), 3),
            "best_ev": round(i.get("best_ev",0), 4),
            "fill_amount": round(i.get("fill_amount") or i.get("kelly_stake") or 0, 1),
            "T_remaining": round(i.get("T_remaining",0), 0),
            "status": i.get("status", "filled"),
            "reason": i.get("reason", ""),
            "ts_ms": i.get("ts_ms", 0),
            "d_abs_pct": round(i.get("d_abs_pct", 0), 4) if "d_abs_pct" in i else None,
            "p_rev_lower": round(i.get("p_rev_lower", 0), 3) if "p_rev_lower" in i else None,
        })
    return jsonify({"ok": True, "items": list(reversed(result))})

@app.route("/api/summary")
def api_summary():
    eq_path = ROOT / "data_runtime" / "sim_equity.txt"
    e = None
    if eq_path.exists():
        try: e = round(float(eq_path.read_text().strip()), 2)
        except: pass
    return jsonify({"ok": True, "equity": e, "start": 20.0})

@app.route("/api/current_window")
def api_current_window():
    try:
        cw_path = ROOT / "data_runtime" / "current_window.json"
        if cw_path.exists():
            data = json.loads(cw_path.read_text())
            return jsonify({"ok": True, **data})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/results")
def api_results():
    p = ROOT / "logs" / "window_results.jsonl"
    items = []
    if p.exists():
        with open(p, encoding="utf-8") as f:
            for line in f.readlines()[-200:]:
                try: items.append(json.loads(line.strip()))
                except: pass
    return jsonify({"ok": True, "items": list(reversed(items))})

@app.route("/api/real/balance")
def api_real_balance():
    try:
        import sys, os
        sys.path.insert(0, os.environ.get("TWINENGINES_ROOT", str(ROOT)))
        from twinengines.io.live_runner import LiveRunner
        runner = LiveRunner._last_instance if hasattr(LiveRunner, '_last_instance') else None
        if runner and runner.poly_client:
            bal = runner.poly_client.fetch_account_equity_usdc()
            return jsonify({"ok": True, "balance_usdc": round(bal or 0, 2), "pending_redeem": 0, "redeem_ok": runner.poly_client.is_healthy()})
    except: pass
    return jsonify({"ok": False, "balance_usdc": 0, "pending_redeem": 0, "redeem_ok": False, "msg": "Shadow mode - no Poly client"})

@app.route("/api/version")
def api_version():
    v_path = ROOT / "data_runtime" / "deploy_version.json"
    if v_path.exists():
        try: return jsonify({"ok": True, **json.loads(v_path.read_text())})
        except: pass
    return jsonify({"ok": True, "deployed": "unknown", "files": {}})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
