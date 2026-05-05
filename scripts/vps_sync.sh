#!/usr/bin/env bash
# 在已部署的 VPS 上执行: 若有 git 则 pull + 更新依赖
# 用法: cd /root/TwinEngines && TE_BRANCH=main bash scripts/vps_sync.sh

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
BRANCH="${TE_BRANCH:-main}"

if [ -d .git ]; then
  echo "[vps_sync] git pull $ROOT branch=$BRANCH"
  git fetch --all --quiet
  git checkout "$BRANCH" --quiet
  git pull --ff-only --quiet || git pull --quiet
else
  echo "[vps_sync] skip git (no .git)"
fi

echo "[vps_sync] pip install"
# shellcheck disable=SC1091
[[ -f .venv/bin/activate ]] || python3 -m venv .venv
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt
pip install -q 'flask>=3.0.0' 'psutil>=5.9.0'

if command -v systemctl >/dev/null 2>&1 && [[ -f deploy/systemd/twinengines-strategy.service ]]; then
  mkdir -p deploy/systemd/.generated
  sed "s#/root/TwinEngines#${ROOT}#g" deploy/systemd/twinengines-strategy.service > deploy/systemd/.generated/twinengines-strategy.service
  if [[ "${EUID:-0}" -eq 0 ]]; then
    cp deploy/systemd/.generated/twinengines-strategy.service /etc/systemd/system/twinengines-strategy.service
    systemctl daemon-reload
    if systemctl restart twinengines-strategy.service 2>/dev/null; then
      echo "[vps_sync] twinengines-strategy.service restarted (unit synced from repo)"
    else
      echo "[vps_sync] warn: twinengines-strategy.service restart failed or unit absent"
    fi
  else
    echo "[vps_sync] skip systemd unit sync (need root to write /etc/systemd/system)"
  fi
fi

echo "[vps_sync] done."
