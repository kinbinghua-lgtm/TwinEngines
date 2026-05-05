#!/usr/bin/env python3
"""
从本机通过 SSH 更新 VPS 上的 TwinEngines (无 .git 时用 SFTP 同步源码).

用法 (PowerShell):
  $env:VPS_PASS='root密码'; python scripts/vps_deploy_remote.py

可选环境变量: VPS_HOST VPS_USER VPS_TE_ROOT VPS_WEBUI_PORT VPS_WEBUI_PASSWORD
  VPS_WRITE_LIVE_ENV=1 时才会写入 DRY_RUN/ENABLE_REAL_ORDERS 等实盘相关 .env 键 (默认不写).
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import paramiko


def _sftp_mkdir_p(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    # Windows 上 Path(...).parent 可能含反斜杠; SFTP 目标必须按 POSIX 逐级建目录
    remote_dir = remote_dir.replace("\\", "/").rstrip("/")
    parts = [p for p in remote_dir.split("/") if p]
    cur = ""
    for p in parts:
        cur += "/" + p
        try:
            sftp.stat(cur)
        except OSError:
            sftp.mkdir(cur)


def _sftp_put_tree(
    sftp: paramiko.SFTPClient,
    *,
    local_root: Path,
    remote_prefix: str,
    subpath: str,
    allowed_suffixes: set[str],
) -> int:
    base = (local_root / subpath).resolve()
    if not base.is_dir():
        return 0
    n = 0
    for f in base.rglob("*"):
        if f.is_dir():
            continue
        if "__pycache__" in f.parts:
            continue
        suf = f.suffix.lower()
        if f.name != ".typed" and suf not in allowed_suffixes:
            continue
        rel = f.relative_to(local_root)
        remote_path = f"{remote_prefix.rstrip('/')}/{rel.as_posix()}"
        parent = remote_path.rsplit("/", 1)[0]
        _sftp_mkdir_p(sftp, parent)
        sftp.put(str(f), remote_path)
        n += 1
    return n


def main() -> int:
    pw = (os.environ.get("VPS_PASS") or "").strip()
    if not pw:
        print("缺少环境变量 VPS_PASS", file=sys.stderr)
        return 2
    host = (os.environ.get("VPS_HOST") or "47.243.169.223").strip()
    user = (os.environ.get("VPS_USER") or "root").strip()
    root_dir = (os.environ.get("VPS_TE_ROOT") or "/root/TwinEngines").strip()
    web_port = (os.environ.get("VPS_WEBUI_PORT") or "8080").strip()
    webui_pw = (os.environ.get("VPS_WEBUI_PASSWORD") or pw).strip()
    b64_ui = base64.b64encode(webui_pw.encode("utf-8")).decode("ascii")
    write_live = "1" if os.environ.get("VPS_WRITE_LIVE_ENV", "").strip() == "1" else "0"

    project_root = Path(__file__).resolve().parent.parent

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            host,
            username=user,
            password=pw,
            timeout=90,
            allow_agent=False,
            look_for_keys=False,
        )
    except Exception as e:
        print(f"SSH 连接失败: {e}", file=sys.stderr)
        return 3

    stdin, stdout, stderr = client.exec_command(
        f"if [ -d '{root_dir}/.git' ]; then echo HASGIT; else echo NOGIT; fi"
    )
    has_git = stdout.read().decode().strip() == "HASGIT"

    if has_git:
        print("[deploy] 使用 git pull")
        pull = f"""set -euo pipefail
cd '{root_dir}'
git fetch --all --quiet && git pull --ff-only --quiet || git pull --quiet
"""
        i, o, e = client.exec_command(pull)
        err_pull = e.read().decode(errors="replace")
        if o.channel.recv_exit_status() != 0:
            print(err_pull, file=sys.stderr)
            client.close()
            return 4
    else:
        print("[deploy] 无 .git, SFTP 同步 src/twinengines + scripts")
        rd = root_dir.rstrip("/")
        prep = f"mkdir -p '{rd}/logs' '{rd}/src/twinengines' '{rd}/scripts'"
        _i, _o, _e = client.exec_command(prep)
        if _o.channel.recv_exit_status() != 0:
            err_b = _e.read().decode(errors="replace")
            print(f"[deploy] 远端 mkdir 失败: {err_b}", file=sys.stderr)
            client.close()
            return 5
        sftp = client.open_sftp()
        try:
            _sftp_mkdir_p(sftp, root_dir)
            _sftp_mkdir_p(sftp, f"{rd}/logs")
            src_ext = {".py", ".pyi", ".js", ".css", ".html", ".svg", ".json"}
            n_py = _sftp_put_tree(
                sftp, local_root=project_root, remote_prefix=root_dir,
                subpath="src/twinengines", allowed_suffixes=src_ext,
            )
            # 仅同步部署必需脚本（仓库已瘦身）
            keep_scripts = (
                "vps_deploy_remote.py",
                "quick_upload_artifact_restart_webui.py",
                "vps_sync.sh",
                "deploy.sh",
            )
            n_sc = 0
            _sftp_mkdir_p(sftp, f"{rd}/scripts")
            for name in keep_scripts:
                lp = project_root / "scripts" / name
                if not lp.is_file():
                    continue
                rp = f"{rd}/scripts/{name}"
                sftp.put(str(lp), rp)
                n_sc += 1
            for name in ("requirements.txt", "pyproject.toml", "artifact_btc_v5.json"):
                lp = project_root / name
                if lp.is_file():
                    rp = f"{root_dir.rstrip('/')}/{name}"
                    _sftp_mkdir_p(sftp, root_dir.rstrip("/"))
                    sftp.put(str(lp), rp)
            print(f"[deploy] 已上传 py={n_py} scripts={n_sc}")
        finally:
            sftp.close()

    finish = f"""set -euo pipefail
ROOT='{root_dir}'
cd "$ROOT"
find scripts -maxdepth 1 -name '*.sh' -exec sed -i 's/\\r$//' {{}} \\; 2>/dev/null || true
if [ -f scripts/vps_sync.sh ]; then bash scripts/vps_sync.sh
else
  . .venv/bin/activate && pip install -q -r requirements.txt && pip install -q 'flask>=3.0.0' 'psutil>=5.9.0'
fi
umask 077
printf '%s' '{b64_ui}' | base64 -d > /tmp/.tew.$$
chmod 600 /tmp/.tew.$$
touch .env
chmod 600 .env 2>/dev/null || true
if [ "{write_live}" = "1" ]; then
  set_kv() {{
    k="$1"; v="$2"
    if grep -q "^${{k}}=" .env 2>/dev/null; then sed -i "s#^${{k}}=.*#${{k}}=${{v}}#" .env
    else echo "${{k}}=${{v}}" >> .env; fi
  }}
  set_kv DRY_RUN false
  set_kv ENABLE_REAL_ORDERS true
  set_kv AUTO_SHADOW_ON_INSUFFICIENT_FUNDS true
  set_kv AUTO_REDEEM_ENABLED true
fi
screen -S twinengines_webui -X quit 2>/dev/null || true
sleep 1
screen -dmS twinengines_webui bash -lc 'cd "{root_dir}" && source .venv/bin/activate && PW=$(cat /tmp/.tew.$$) && rm -f /tmp/.tew.$$ && exec python -m twinengines.cli webui --host 0.0.0.0 --port {web_port} --password "$PW" --project-root "{root_dir}" --env-file .env >> logs/webui.log 2>&1'
sleep 2
screen -ls || true
echo DONE
"""
    stdin2, stdout2, stderr2 = client.exec_command("bash -s", get_pty=True)
    stdin2.write(finish)
    stdin2.flush()
    stdin2.channel.shutdown_write()
    out2 = stdout2.read().decode("utf-8", errors="replace")
    err2 = stderr2.read().decode("utf-8", errors="replace")
    rc = stdout2.channel.recv_exit_status()
    sys.stdout.write(out2)
    sys.stderr.write(err2)
    client.close()
    return 0 if rc == 0 else rc


if __name__ == "__main__":
    raise SystemExit(main())
