"""
WebUI 主应用 (Flask, 完全独立进程).

模块化分工:
    webui_data.py   - 只读数据层 (SQLite / JSONL / log tail)
    webui_proc.py   - 子进程管理 (start/stop/connectivity/git pull/report)
    webui.py        - 本文件: HTTP 路由 + session 鉴权 + 静态托管
    webui_static/   - HTML/CSS/JS 单页面板

关键安全约束:
    1. 严禁 import 策略代码 (signals/survival/risk/sizing/backtest)
    2. .env 内容绝不返回前端 (任何端点不读 .env)
    3. 必须 --password 启动, 否则拒绝启动
    4. 默认 0.0.0.0:8080, 公网访问需自行加 nginx/firewall
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .webui_config import ConfigSnapshot
from .webui_data import (
    AlertsTail,
    AutoRedeemStatus,
    EdgeValidationReport,
    EquityHistory,
    FrictionSnapshotStatus,
    HourlyMetrics,
    KV_NAKED_FUNDS_SNAPSHOT,
    LatestBacktestSnapshot,
    NakedTradeJournal,
    LogsTail,
    LiveFilledOutcomeBoard,
    OrdersTail,
    PRevCalibrationStatus,
    RegimeInfo,
    RuntimeEffectiveSnapshot,
    ShadowSignalsTail,
    ShadowSignalOutcomeBoard,
    SignalCounts,
    TriggersTail,
    read_kv_state_json,
    read_strategy_exec_start_from_repo,
)
from .webui_diagnostic import AI_PROMPT_TEMPLATE, DiagnosticBuilder
from .webui_proc import ProcessManager
from .webui_system import SystemInfo

# Flask 是延迟导入: 普通用户跑回测时不需要装
try:
    from flask import Flask, jsonify, render_template_string, request, send_file, session
except ImportError as _e:  # pragma: no cover
    Flask = None  # type: ignore[assignment]
    _flask_import_error = _e


SESSION_COOKIE_NAME = "te_webui_session"
LOGIN_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>TwinEngines 控制台</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
     background:#0f1116;color:#eee;display:flex;align-items:center;justify-content:center;
     min-height:100vh;margin:0;}
.box{background:#171b24;padding:40px;border-radius:12px;
     box-shadow:0 4px 30px rgba(0,0,0,.4);min-width:320px;}
h1{margin:0 0 16px;font-size:20px;font-weight:600;}
input{width:100%;padding:10px;font-size:15px;border-radius:6px;
      border:1px solid #2c3142;background:#0f1116;color:#eee;box-sizing:border-box;}
button{margin-top:12px;width:100%;padding:10px;background:#3d7eff;color:white;
       border:0;border-radius:6px;font-size:15px;cursor:pointer;}
.err{color:#ff5b5b;margin-top:8px;font-size:13px;min-height:16px;}
</style></head><body>
<div class="box">
<h1>TwinEngines</h1>
<form method="post" action="/login">
<input type="password" name="password" placeholder="请输入访问密码" autofocus required>
<button type="submit">登录</button>
<div class="err">{{ error or "" }}</div>
</form></div></body></html>"""


# ============================================================
# 工厂
# ============================================================

def create_app(
    *,
    password: str,
    project_root: str = ".",
    env_file: str = ".env",
    state_db_path: str = "data_runtime/state.sqlite",
    alerts_path: str = "logs/alerts.jsonl",
    log_dir: str = "logs",
    shadow_signals_path: str = "logs/shadow_signals.jsonl",
    default_artifact: str = "artifact_btc_v6.json",
) -> "Flask":
    if Flask is None:
        raise RuntimeError(
            "Flask not installed. Run: pip install 'flask>=3.0' "
            f"(import error: {_flask_import_error})"
        )
    if not password or len(password) < 6:
        raise ValueError("password must be >= 6 chars")

    static_dir = Path(__file__).parent / "webui_static"
    app = Flask(__name__, static_folder=str(static_dir), static_url_path="/static")
    app.secret_key = secrets.token_hex(32)
    app.config["SESSION_COOKIE_NAME"] = SESSION_COOKIE_NAME
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    equity = EquityHistory(state_db_path=state_db_path)
    signals = SignalCounts(state_db_path=state_db_path)
    alerts = AlertsTail(path=alerts_path)
    logs_tail = LogsTail(log_dir=log_dir)
    trade_journal = NakedTradeJournal(stdout_path=os.path.join(log_dir, "strategy_stdout.log"))
    edge_validation = EdgeValidationReport(stdout_path=os.path.join(log_dir, "strategy_stdout.log"))
    regime = RegimeInfo(state_db_path=state_db_path)
    shadow_signals_tail = ShadowSignalsTail(path=shadow_signals_path)
    shadow_outcomes = ShadowSignalOutcomeBoard(path=shadow_signals_path)
    live_outcomes = LiveFilledOutcomeBoard(
        state_db_path=state_db_path,
        shadow_signals_path=shadow_signals_path,
    )
    orders_tail = OrdersTail(state_db_path=state_db_path)
    triggers_tail = TriggersTail(state_db_path=state_db_path)
    hourly = HourlyMetrics(state_db_path=state_db_path)
    sysinfo = SystemInfo(project_root=project_root)
    friction_snap = FrictionSnapshotStatus(state_db_path=state_db_path)
    auto_redeem_status = AutoRedeemStatus(state_db_path=state_db_path)
    p_rev_cal_status = PRevCalibrationStatus(
        calibration_path=str(Path(project_root) / "data_runtime" / "p_rev_time_calibration.json")
    )
    runtime_snap = RuntimeEffectiveSnapshot(state_db_path=state_db_path)
    latest_backtest = LatestBacktestSnapshot(project_root=project_root)
    proc = ProcessManager(
        project_root=project_root,
        env_file=env_file,
        default_artifact=default_artifact,
    )
    cfg_snap = ConfigSnapshot(env_file=env_file, artifact_path=str(Path(project_root) / proc.default_naked_model_json))
    diag = DiagnosticBuilder(
        project_root=project_root,
        env_file=env_file,
        state_db_path=state_db_path,
        alerts_path=alerts_path,
        shadow_signals_path=shadow_signals_path,
        log_dir=log_dir,
        default_artifact=default_artifact,
    )

    # ============================================================
    # 鉴权
    # ============================================================

    def _logged_in() -> bool:
        return bool(session.get("authed"))

    def _require_login():
        if not _logged_in():
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "unauthorized"}), 401
            return render_template_string(LOGIN_HTML, error=None), 401
        return None

    @app.before_request
    def _gate():
        # 放行: 登录页 / 健康检查 / 静态资源 (无敏感数据)
        if request.path in ("/login", "/logout", "/healthz"):
            return None
        if request.path.startswith("/static/"):
            return None
        if request.path.startswith("/api/"):
            if not _logged_in():
                return jsonify({"ok": False, "error": "unauthorized"}), 401
            return None
        # 其它页面 (如 /) 需要登录
        if not _logged_in():
            return render_template_string(LOGIN_HTML, error=None), 401
        return None

    # ============================================================
    # 登录
    # ============================================================

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            if _logged_in():
                from flask import redirect, url_for
                return redirect(url_for("index"))
            return render_template_string(LOGIN_HTML, error=None)
        provided = (request.form.get("password") or "").strip()
        if secrets.compare_digest(provided, password):
            session["authed"] = True
            session["login_ts"] = int(time.time())
            from flask import redirect, url_for
            return redirect(url_for("index"))
        return render_template_string(LOGIN_HTML, error="密码错误"), 401

    @app.route("/logout")
    def logout():
        session.clear()
        from flask import redirect, url_for
        return redirect(url_for("login"))

    # ============================================================
    # 页面
    # ============================================================

    @app.route("/")
    def index():
        index_html = static_dir / "index.html"
        if not index_html.is_file():
            return jsonify({"error": "static index.html not found"}), 500
        return index_html.read_text(encoding="utf-8")

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "ts": int(time.time())})

    # ============================================================
    # API: 状态总览
    # ============================================================

    @app.route("/api/status")
    def api_status():
        st = proc.status()
        rg = regime.current()
        sc = signals.today(hours=24.0)
        cfg = cfg_snap.build()
        env_redacted = cfg.get("env_redacted") if isinstance(cfg, dict) else {}
        proxy_masked = None
        if isinstance(env_redacted, dict):
            proxy_masked = env_redacted.get("POLYMARKET_PROXY_ADDRESS") or env_redacted.get("PROXY_ADDRESS")
        naked_funds_snap = read_kv_state_json(state_db_path, KV_NAKED_FUNDS_SNAPSHOT)
        exec_start = read_strategy_exec_start_from_repo(project_root)
        return jsonify({
            "ok": True,
            "strategy": {
                "running": st.running,
                "pid": st.pid,
                "mode": st.mode,
                "started_at_ms": st.started_at_ms,
                "stale_pidfile": st.stale_pidfile,
                "cmd": st.cmd,
            },
            "regime": rg,
            "funds": {
                "onchain_usdc_pusd": rg.get("last_equity_usdc"),
                "equity_updated_ts_ms": rg.get("last_equity_ts_ms"),
                "source": "onchain_proxy_wallet_usdc_e_plus_pusd",
                "proxy_address_masked": proxy_masked,
            },
            "strategy_program": {
                "cli": "naked-third-digit-live",
                "systemd_unit": "twinengines-strategy.service",
                "exec_start": exec_start,
                "default_model_json": proc.default_naked_model_json,
                "model_schema": (cfg.get("artifact") or {}).get("schema_version") if isinstance(cfg, dict) else None,
                "model_kind": (cfg.get("artifact") or {}).get("model_kind") if isinstance(cfg, dict) else None,
                "prefix_model": (cfg.get("artifact") or {}).get("prefix_model") if isinstance(cfg, dict) else None,
                "training_meta": (cfg.get("artifact") or {}).get("training_meta") if isinstance(cfg, dict) else None,
            },
            "naked_funds_snapshot": naked_funds_snap,
            "signals_today": sc,
            "server_ts_ms": int(time.time() * 1000),
        })

    @app.route("/api/latest_backtest")
    def api_latest_backtest():
        return jsonify({"ok": True, "data": latest_backtest.build()})

    @app.route("/api/equity")
    def api_equity():
        hours = float(request.args.get("hours", "24"))
        pts = equity.recent(hours=hours)
        return jsonify({
            "ok": True,
            "hours": hours,
            "points": [{"ts_ms": p.ts_ms, "equity": p.equity_usdc, "src": p.source}
                       for p in pts],
        })

    @app.route("/api/alerts")
    def api_alerts():
        n = int(request.args.get("n", "50"))
        return jsonify({"ok": True, "items": alerts.recent(n=n)})

    @app.route("/api/shadow_signals")
    def api_shadow_signals():
        n = int(request.args.get("n", "50"))
        return jsonify({"ok": True, "items": shadow_signals_tail.recent(n=n)})

    @app.route("/api/shadow_orders")
    def api_shadow_orders():
        n = int(request.args.get("n", "50"))
        orders_path = Path(project_root) / "logs" / "shadow_orders.jsonl"
        items = []
        if orders_path.exists():
            with open(orders_path, encoding="utf-8") as f:
                lines = f.readlines()[-n:]
            for line in lines:
                try: items.append(json.loads(line.strip()))
                except: pass
        return jsonify({"ok":True,"items":list(reversed(items))})

@app.route("/api/calibration")
    def api_calibration():
        cal_path = Path(project_root) / "data_runtime" / "calibration_table.json"
        if not cal_path.exists():
            return jsonify({"ok": False, "error": "no calibration table"})
        data = json.loads(cal_path.read_text(encoding="utf-8"))
        return jsonify({
            "ok": True,
            "built": data.get("built", ""),
            "params": data.get("params", {}),
            "prefixes": {k: {"event_rate": v.get("event_rate", 0), "t_buckets": len(v.get("t_buckets", {}))}
                         for k, v in data.get("table", {}).items() if isinstance(v, dict)},
        })

    @app.route("/api/shadow_outcomes")
    def api_shadow_outcomes():
        n = int(request.args.get("n", "50"))
        return jsonify({
            "ok": True,
            "summary": shadow_outcomes.summary(hours=24.0, scan_n=max(200, n * 10)),
            "items": shadow_outcomes.recent(n=n),
        })

    @app.route("/api/live_outcomes")
    def api_live_outcomes():
        n = int(request.args.get("n", "30"))
        return jsonify({
            "ok": True,
            "summary": live_outcomes.summary(hours=24.0, scan_n=max(120, n * 10)),
            "items": live_outcomes.recent(n=n),
        })

    @app.route("/api/logs")
    def api_logs():
        n = int(request.args.get("n", "200"))
        level = request.args.get("level", "")
        return jsonify({"ok": True, "items": logs_tail.tail(n=n, level_filter=level)})

    @app.route("/api/trade_journal")
    def api_trade_journal():
        n = int(request.args.get("n", "80"))
        return jsonify({"ok": True, "items": trade_journal.recent(n=n)})

    @app.route("/api/edge_validation")
    def api_edge_validation():
        max_lines = int(request.args.get("max_lines", "24000"))
        return jsonify(edge_validation.build(max_lines=max(1000, min(120000, max_lines))))

    # ============================================================
    # API: 控制 (POST)
    # ============================================================

    @app.route("/api/connectivity", methods=["POST"])
    def api_connectivity():
        ok, payload = proc.run_connectivity(check_allowance=True)
        return jsonify({"ok": ok, "report": payload})

    @app.route("/api/git_pull", methods=["POST"])
    def api_git_pull():
        ok, payload = proc.run_git_pull()
        return jsonify({"ok": ok, "result": payload})

    @app.route("/api/strategy/start", methods=["POST"])
    def api_strategy_start():
        body = request.get_json(silent=True) or {}
        mode = str(body.get("mode", "dry_run")).strip()
        disable_trend = bool(body.get("disable_trend", False))
        if mode not in ("dry_run", "live", "dry_run_signals"):
            return jsonify({"ok": False, "error": f"bad mode: {mode}"}), 400
        artifact = body.get("artifact_path") or default_artifact
        ok, msg, status = proc.start_strategy(
            mode=mode,
            artifact_path=artifact if mode == "dry_run_signals" else None,
            disable_trend=disable_trend,
        )
        out: dict[str, Any] = {"ok": ok, "message": msg}
        if status:
            out["status"] = {
                "running": status.running, "pid": status.pid,
                "mode": status.mode, "cmd": status.cmd,
            }
        return jsonify(out), (200 if ok else 409)

    @app.route("/api/strategy/stop", methods=["POST"])
    def api_strategy_stop():
        ok, msg = proc.stop_strategy()
        return jsonify({"ok": ok, "message": msg})

    @app.route("/api/report/shadow", methods=["POST"])
    def api_report_shadow():
        body = request.get_json(silent=True) or {}
        hours = float(body.get("window_hours", 168.0))
        ok, payload = proc.run_shadow_report(window_hours=hours)
        return jsonify({"ok": ok, "report": payload})

    @app.route("/api/report/shadow/download")
    def api_report_shadow_download():
        from .webui_proc import SHADOW_REPORT_JSON
        path = (Path(project_root) / SHADOW_REPORT_JSON).resolve()
        if not path.is_file():
            return jsonify({"ok": False, "error": "report not generated yet"}), 404
        return send_file(str(path), as_attachment=True,
                         download_name="shadow_report.json")

    @app.route("/api/report/connectivity/download")
    def api_report_connectivity_download():
        from .webui_proc import CONNECTIVITY_REPORT
        path = (Path(project_root) / CONNECTIVITY_REPORT).resolve()
        if not path.is_file():
            return jsonify({"ok": False, "error": "report not generated yet"}), 404
        return send_file(str(path), as_attachment=True,
                         download_name="connectivity_report.json")

    # ============================================================
    # API: 性能指标 (小时分桶 + 订单结局)
    # ============================================================

    @app.route("/api/metrics/hourly")
    def api_metrics_hourly():
        hours = int(request.args.get("hours", "24"))
        return jsonify({"ok": True, "data": hourly.signal_frequency(hours=hours)})

    @app.route("/api/metrics/orders")
    def api_metrics_orders():
        hours = float(request.args.get("hours", "168"))
        return jsonify({"ok": True, "data": hourly.order_outcomes(hours=hours)})

    @app.route("/api/orders/recent")
    def api_orders_recent():
        n = int(request.args.get("n", "30"))
        hours = float(request.args.get("hours", "168"))
        return jsonify({"ok": True, "items": orders_tail.recent(n=n, hours=hours)})

    @app.route("/api/triggers/recent")
    def api_triggers_recent():
        n = int(request.args.get("n", "20"))
        hours = float(request.args.get("hours", "24"))
        return jsonify({"ok": True, "items": triggers_tail.recent(n=n, hours=hours)})

    # ============================================================
    # API: 系统资源 / 配置脱敏快照
    # ============================================================

    @app.route("/api/system")
    def api_system():
        return jsonify({"ok": True, "data": sysinfo.snapshot()})

    @app.route("/api/config")
    def api_config():
        return jsonify({"ok": True, "data": cfg_snap.build()})

    @app.route("/api/friction_snapshot_status")
    def api_friction_snapshot_status():
        return jsonify({"ok": True, "data": friction_snap.build()})

    @app.route("/api/auto_redeem_status")
    def api_auto_redeem_status():
        return jsonify({"ok": True, "data": auto_redeem_status.build()})

    @app.route("/api/p_rev_calibration_status")
    def api_p_rev_calibration_status():
        return jsonify({"ok": True, "data": p_rev_cal_status.build()})

    @app.route("/api/runtime_effective_snapshot")
    def api_runtime_effective_snapshot():
        return jsonify({"ok": True, "data": runtime_snap.build()})

    # ============================================================
    # API: AI 诊断包
    # ============================================================

    @app.route("/api/diagnostic/markdown")
    def api_diagnostic_md():
        try:
            text = diag.build_markdown()
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True, "markdown": text, "length": len(text)})

    @app.route("/api/diagnostic/json")
    def api_diagnostic_json():
        try:
            return jsonify({"ok": True, "data": diag.build_dict()})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/diagnostic/download.md")
    def api_diagnostic_download_md():
        try:
            text = diag.build_markdown()
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        from flask import Response
        ts = time.strftime("%Y%m%d-%H%M%S")
        return Response(
            text, mimetype="text/markdown; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="diagnostic_{ts}.md"'},
        )

    @app.route("/api/diagnostic/download.zip")
    def api_diagnostic_download_zip():
        ts = time.strftime("%Y%m%d-%H%M%S")
        out_path = Path("reports") / f"diagnostic_{ts}.zip"
        try:
            diag.build_zip(str(out_path))
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return send_file(str(out_path), as_attachment=True,
                         download_name=out_path.name)

    @app.route("/api/diagnostic/prompt")
    def api_diagnostic_prompt():
        return jsonify({"ok": True, "prompt": AI_PROMPT_TEMPLATE})

    return app


# ============================================================
# 入口 (供 cli.py 调用)
# ============================================================

def run_webui(
    *,
    host: str,
    port: int,
    password: str,
    project_root: str,
    env_file: str,
    state_db_path: str,
    alerts_path: str,
    log_dir: str,
    shadow_signals_path: str,
    default_artifact: str,
) -> int:
    app = create_app(
        password=password,
        project_root=project_root,
        env_file=env_file,
        state_db_path=state_db_path,
        alerts_path=alerts_path,
        log_dir=log_dir,
        shadow_signals_path=shadow_signals_path,
        default_artifact=default_artifact,
    )
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.WARNING)
    print(
        f"[webui] listening on http://{host}:{port}  "
        f"(login with --password)\n"
        f"[webui] static project_root={project_root}  env_file={env_file}\n",
        flush=True,
    )
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
    return 0
