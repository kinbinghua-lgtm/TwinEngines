#!/usr/bin/env bash
# =============================================================================
# TwinEngines 一键部署脚本 (Ubuntu 22.04 / Debian 12)
#
# 必传环境变量 (通过 `KEY=value KEY=value bash` 传入):
#   TE_REPO_URL          - GitHub 仓库地址 (https://...) , 必填
#   TE_WEBUI_PASSWORD    - WebUI 登录密码, 必填, >=6 位
#   TE_PROXY_ADDRESS     - Polymarket 代理钱包地址 (0x..., 42 字符), 必填
#   TE_PRIVATE_KEY       - Polygon EOA 私钥 (0x..., 66 字符), 必填
#
# 可选环境变量:
#   TE_INSTALL_DIR       - 安装目录, 默认 /root/TwinEngines
#   TE_BRANCH            - 分支, 默认 main
#   TE_WEBUI_PORT        - WebUI 端口, 默认 8080
#   TE_POLYGON_RPC_URL   - 自定义 Polygon RPC, 默认 https://polygon-bor.publicnode.com
#   TE_SIGNATURE_TYPE    - 签名类型 (0/1/2), 默认 1
#   TE_TELEGRAM_BOT      - 可选 Telegram bot token
#   TE_TELEGRAM_CHAT     - 可选 Telegram chat id
#   TE_SKIP_CONNECTIVITY - 设为 1 跳过环境检查 (调试用)
#   TE_LIVE_DEFAULT=1    - 首次写入 .env 时默认实盘双开 + 资金不足走影子单 + 自动赎回 (充值即用)
#
# 用法:
#   curl -fsSL https://YOUR_DOMAIN/deploy.sh \
#     | TE_REPO_URL=... TE_WEBUI_PASSWORD=... \
#       TE_PROXY_ADDRESS=... TE_PRIVATE_KEY=... bash
# =============================================================================

set -euo pipefail

TE_INSTALL_DIR="${TE_INSTALL_DIR:-/root/TwinEngines}"
TE_BRANCH="${TE_BRANCH:-main}"
TE_WEBUI_PORT="${TE_WEBUI_PORT:-8080}"
TE_POLYGON_RPC_URL="${TE_POLYGON_RPC_URL:-https://polygon-bor.publicnode.com}"
TE_SIGNATURE_TYPE="${TE_SIGNATURE_TYPE:-1}"
TE_SKIP_CONNECTIVITY="${TE_SKIP_CONNECTIVITY:-0}"

# ---------- 颜色 ----------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
say()  { echo -e "${BLUE}[deploy]${NC} $*"; }
ok()   { echo -e "${GREEN}[ ok ]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
err()  { echo -e "${RED}[fail]${NC} $*" >&2; }

trap 'err "deploy failed at line $LINENO. last cmd exit=$?"; exit 1' ERR

# ---------- 0. 必填校验 ----------
say "Step 0/8: 校验必填环境变量"
missing=()
[[ -z "${TE_REPO_URL:-}" ]]        && missing+=("TE_REPO_URL")
[[ -z "${TE_WEBUI_PASSWORD:-}" ]]  && missing+=("TE_WEBUI_PASSWORD")
[[ -z "${TE_PROXY_ADDRESS:-}" ]]   && missing+=("TE_PROXY_ADDRESS")
[[ -z "${TE_PRIVATE_KEY:-}" ]]     && missing+=("TE_PRIVATE_KEY")
if (( ${#missing[@]} > 0 )); then
  err "缺少必填环境变量: ${missing[*]}"
  err "示例: TE_REPO_URL=https://github.com/you/te.git TE_WEBUI_PASSWORD=xx \\"
  err "       TE_PROXY_ADDRESS=0x... TE_PRIVATE_KEY=0x... bash deploy.sh"
  exit 2
fi
if (( ${#TE_WEBUI_PASSWORD} < 6 )); then
  err "TE_WEBUI_PASSWORD 至少 6 位"; exit 2
fi
if [[ ! "$TE_PROXY_ADDRESS" =~ ^0x[a-fA-F0-9]{40}$ ]]; then
  err "TE_PROXY_ADDRESS 格式错: 必须 0x + 40 hex (实际长度=${#TE_PROXY_ADDRESS})"; exit 2
fi
if [[ ! "$TE_PRIVATE_KEY" =~ ^0x[a-fA-F0-9]{64}$ ]]; then
  err "TE_PRIVATE_KEY 格式错: 必须 0x + 64 hex (实际长度=${#TE_PRIVATE_KEY})"; exit 2
fi
ok "必填变量齐全"

if [[ "$EUID" -ne 0 ]]; then
  warn "建议用 root 执行 (当前 UID=$EUID, 安装目录=$TE_INSTALL_DIR)"
fi

# ---------- 1. 系统软件 ----------
say "Step 1/8: 安装系统依赖 (apt)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -yqq \
  python3 python3-pip python3-venv python3-dev \
  git curl ca-certificates build-essential \
  chrony screen ufw jq sqlite3 >/dev/null
ok "apt 安装完成"

# ---------- 2. 时间同步 ----------
say "Step 2/8: 启动 chrony 并强制对时"
systemctl enable --now chrony >/dev/null 2>&1 || true
chronyc -a makestep >/dev/null 2>&1 || warn "chrony makestep 失败 (可能仍在初始化, 不阻塞)"
ok "chrony 已就绪"

# ---------- 3. 拉代码 ----------
say "Step 3/8: 克隆/更新代码到 $TE_INSTALL_DIR (branch=$TE_BRANCH)"
if [[ -d "$TE_INSTALL_DIR/.git" ]]; then
  warn "目录已是 git 仓库, 执行 git pull"
  git -C "$TE_INSTALL_DIR" fetch --all --quiet
  git -C "$TE_INSTALL_DIR" checkout "$TE_BRANCH" --quiet
  git -C "$TE_INSTALL_DIR" pull --ff-only --quiet
else
  rm -rf "$TE_INSTALL_DIR"
  git clone --branch "$TE_BRANCH" --depth 1 "$TE_REPO_URL" "$TE_INSTALL_DIR" >/dev/null
fi
ok "代码就绪"

# ---------- 4. 虚拟环境 + 依赖 ----------
say "Step 4/8: 创建虚拟环境并安装 Python 依赖 (这一步耗时较长)"
cd "$TE_INSTALL_DIR"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
# 确保 flask + psutil 装上 (面板和系统资源卡需要)
pip install --quiet 'flask>=3.0.0' 'psutil>=5.9.0'
ok "依赖安装完成"

# ---------- 5. 写 .env (私钥不外泄) ----------
say "Step 5/8: 生成 .env (敏感字段写入磁盘, 严禁回显)"
cp -n .env.example .env || true
# umask 077 让 .env 仅 root 可读
chmod 600 .env
write_env() {
  local key="$1" val="$2"
  if grep -q "^${key}=" .env; then
    # 用 # 作分隔符避免与 / 0x 冲突
    sed -i "s#^${key}=.*#${key}=${val}#" .env
  else
    echo "${key}=${val}" >> .env
  fi
}
write_env POLYMARKET_PROXY_ADDRESS "$TE_PROXY_ADDRESS"
write_env POLYMARKET_PRIVATE_KEY   "$TE_PRIVATE_KEY"
write_env SIGNATURE_TYPE           "$TE_SIGNATURE_TYPE"
write_env POLYGON_RPC_URL          "$TE_POLYGON_RPC_URL"
TE_LIVE_DEFAULT="${TE_LIVE_DEFAULT:-0}"
if [[ "$TE_LIVE_DEFAULT" == "1" ]]; then
  write_env DRY_RUN                        "false"
  write_env ENABLE_REAL_ORDERS             "true"
  write_env AUTO_SHADOW_ON_INSUFFICIENT_FUNDS "true"
  write_env AUTO_REDEEM_ENABLED            "true"
  ok ".env 已生成 (chmod 600), TE_LIVE_DEFAULT=1 → 实盘双开 + 不足额影子单 + 自动赎回"
else
  write_env DRY_RUN                  "true"
  write_env ENABLE_REAL_ORDERS       "false"
  ok ".env 已生成 (chmod 600), DRY_RUN=true 双保险默认 (未设 TE_LIVE_DEFAULT=1)"
fi
[[ -n "${TE_TELEGRAM_BOT:-}"  ]] && write_env TELEGRAM_BOT_TOKEN "$TE_TELEGRAM_BOT"
[[ -n "${TE_TELEGRAM_CHAT:-}" ]] && write_env TELEGRAM_CHAT_ID   "$TE_TELEGRAM_CHAT"

# ---------- 6. 防火墙 (只放 SSH + WebUI) ----------
say "Step 6/8: 配置防火墙 ufw 放行 22 + ${TE_WEBUI_PORT}"
ufw allow 22/tcp >/dev/null 2>&1 || true
ufw allow "${TE_WEBUI_PORT}/tcp" >/dev/null 2>&1 || true
yes | ufw enable >/dev/null 2>&1 || true
ok "ufw 已开启 (22, ${TE_WEBUI_PORT})"

# ---------- 7. 环境检查 ----------
if [[ "$TE_SKIP_CONNECTIVITY" != "1" ]]; then
  say "Step 7/8: 环境连通性检查 (Binance/Polymarket/Polygon)"
  set +e
  python -m twinengines.cli connectivity --env-file .env --check-allowance \
    --out reports/connectivity_initial.json
  CONN_RC=$?
  set -e
  if (( CONN_RC != 0 )); then
    warn "环境检查未全绿 (exit=$CONN_RC); 已写 reports/connectivity_initial.json"
    warn "面板会照常启动, 但请先到 ⑨ 卡片排障再启动策略"
  else
    ok "环境检查全部通过"
  fi
else
  warn "Step 7/8: 已跳过环境检查 (TE_SKIP_CONNECTIVITY=1)"
fi

# ---------- 8. 启动 WebUI + 注册自动实盘策略服务 ----------
say "Step 8/8: 启动 WebUI + 配置系统启动自动实盘"
screen -S twinengines_webui -X quit >/dev/null 2>&1 || true
mkdir -p logs reports data_runtime
screen -dmS twinengines_webui bash -c \
  "cd '$TE_INSTALL_DIR' && source .venv/bin/activate && \
   python -m twinengines.cli webui \
     --host 0.0.0.0 --port ${TE_WEBUI_PORT} \
     --password '${TE_WEBUI_PASSWORD}' \
     --project-root '$TE_INSTALL_DIR' \
     --env-file .env \
     >> logs/webui.log 2>&1"
sleep 2
if screen -ls | grep -q twinengines_webui; then
  ok "WebUI 已在 screen session twinengines_webui 中后台启动"
else
  err "WebUI 启动失败, 请查看 logs/webui.log"
  exit 1
fi

# 安装并启用 strategy systemd 服务（开机自动实盘）
if command -v systemctl >/dev/null 2>&1; then
  mkdir -p deploy/systemd/.generated
  sed "s#/root/TwinEngines#${TE_INSTALL_DIR}#g" \
    deploy/systemd/twinengines-strategy.service > deploy/systemd/.generated/twinengines-strategy.service
  cp deploy/systemd/.generated/twinengines-strategy.service /etc/systemd/system/twinengines-strategy.service
  systemctl daemon-reload
  systemctl enable --now twinengines-strategy.service >/dev/null 2>&1 || warn "strategy service 启动失败，请手动检查 systemctl status twinengines-strategy.service"
  if systemctl is-active --quiet twinengines-strategy.service; then
    ok "strategy systemd 服务已启用（开机自动拉起实盘）"
  else
    warn "strategy service 未处于 active，请执行 systemctl status twinengines-strategy.service"
  fi
else
  warn "系统无 systemctl，未配置开机自启实盘策略"
fi

# ---------- 收尾 ----------
PUBLIC_IP=$(curl -s --max-time 4 https://api.ipify.org || echo "<your-vps-ip>")

cat <<EOF

${GREEN}============================================================${NC}
${GREEN}  ✓ TwinEngines 部署完成${NC}
${GREEN}============================================================${NC}

  访问地址 :  http://${PUBLIC_IP}:${TE_WEBUI_PORT}
  登录密码 :  ${TE_WEBUI_PASSWORD}
  安装路径 :  ${TE_INSTALL_DIR}
  .env 路径 :  ${TE_INSTALL_DIR}/.env  (chmod 600)
  WebUI 日志:  ${TE_INSTALL_DIR}/logs/webui.log

  策略服务 :  systemd 已启用 twinengines-strategy.service（开机自动实盘）
  说明     :  策略命令默认 --yes-real-money，且仍受 .env 实盘闸门控制

  常用命令 (在 ${TE_INSTALL_DIR} 目录下):
    source .venv/bin/activate
    python -m twinengines.cli connectivity --env-file .env --check-allowance
    systemctl status twinengines-strategy.service
    systemctl restart twinengines-strategy.service
    python -m twinengines.cli naked-third-digit-live --env-file .env --model-json configs/example_third_digit_dynamic.json --yes-real-money --kelly-sizing

  WebUI 进程管理:
    screen -r twinengines_webui    # 进入 (退出按 Ctrl+A 然后 D)
    screen -S twinengines_webui -X quit  # 杀掉

${GREEN}============================================================${NC}
EOF
