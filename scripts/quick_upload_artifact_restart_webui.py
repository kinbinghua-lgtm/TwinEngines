#!/usr/bin/env python3
"""仅上传 artifact_btc_v5.json 并重启 screen webui。"""
from __future__ import annotations

import base64
import os
import sys
import time
from pathlib import Path

import paramiko


def main() -> int:
    pw = (os.environ.get("VPS_PASS") or "").strip()
    if not pw:
        print("需要 VPS_PASS", file=sys.stderr)
        return 2
    host = (os.environ.get("VPS_HOST") or "47.243.169.223").strip()
    root = (os.environ.get("VPS_TE_ROOT") or "/root/TwinEngines").strip().rstrip("/")
    port = (os.environ.get("VPS_WEBUI_PORT") or "8080").strip()
    webui_pw = (os.environ.get("VPS_WEBUI_PASSWORD") or pw).strip()
    b64_ui = base64.b64encode(webui_pw.encode("utf-8")).decode("ascii")

    project_root = Path(__file__).resolve().parent.parent
    art = project_root / "artifact_btc_v5.json"
    if not art.is_file():
        print("missing artifact", art, file=sys.stderr)
        return 2

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=90, allow_agent=False, look_for_keys=False)
    sftp = c.open_sftp()
    try:
        sftp.put(str(art), f"{root}/artifact_btc_v5.json")
    finally:
        sftp.close()

    i, o, e = c.exec_command(f"grep trend_min_expected_value {root}/artifact_btc_v5.json | head -1")
    print(o.read().decode(errors="replace").strip())

    script = f"""set -e
screen -S twinengines_webui -X quit 2>/dev/null || true
sleep 1
umask 077
printf '%s' '{b64_ui}' | base64 -d > /tmp/.tew.$$
chmod 600 /tmp/.tew.$$
screen -dmS twinengines_webui bash -lc 'cd "{root}" && source .venv/bin/activate && PW=$(cat /tmp/.tew.$$) && rm -f /tmp/.tew.$$ && exec python -m twinengines.cli webui --host 0.0.0.0 --port {port} --password "$PW" --project-root "{root}" --env-file .env >> logs/webui.log 2>&1'
sleep 1
screen -ls || true
"""
    stdin, stdout, stderr = c.exec_command("bash -s", get_pty=True)
    stdin.write(script)
    stdin.flush()
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()
    sys.stdout.write(out)
    sys.stderr.write(err)
    c.close()
    return 0 if rc == 0 else rc


if __name__ == "__main__":
    raise SystemExit(main())
