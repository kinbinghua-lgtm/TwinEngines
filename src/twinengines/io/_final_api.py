"""Minimal WebUI: static index.html + JSON API."""
from pathlib import Path
import json, os, secrets, sys
from flask import Flask, jsonify, request

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
STATIC = Path(__file__).resolve().parent / "webui_static"

@app.route("/")
def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return html

@app.route("/api/shadow_orders")
def api_shadow_orders():
    n = int(request.args.get("n", "50"))
    p = ROOT / "logs" / "shadow_orders.jsonl"
    items = []
    if p.exists():
        with open(p, encoding="utf-8") as f:
            for line in f.readlines()[-n:]:
                try: items.append(json.loads(line.strip()))
                except: pass
    return jsonify({"ok": True, "items": list(reversed(items))})

@app.route("/static/<path:path>")
def static_files(path):
    return (STATIC / path).read_bytes(), 200, {"Content-Type": "text/css" if path.endswith(".css") else "text/javascript" if path.endswith(".js") else "text/html"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
