# VPS 更新说明

## 当前线上运行方式

- VPS 项目目录：`/root/TwinEngines`
- Git 分支：`direction-probability-dynamic-thresholds`
- 策略服务：`twinengines-strategy.service`
- WebUI 服务：`twinengines-webui.service`
- 两个服务都使用同一个虚拟环境：`/root/TwinEngines/venv/bin/python`
- 不要使用或重建旧的 `.venv`。旧 `.venv` 曾是 Python 3.6，无法运行当前代码。

## 安全更新流程

本地先提交并推送干净 commit：

```bash
git add <changed-files>
git commit -m "..."
git push origin HEAD
```

VPS 上只通过 Git 更新，不要把本地整个工作区 `scp`、`rsync` 或覆盖到 VPS：

```bash
cd /root/TwinEngines
git fetch origin
git reset --hard origin/direction-probability-dynamic-thresholds
/root/TwinEngines/venv/bin/python -m py_compile src/twinengines/io/live_runner.py
systemctl restart twinengines-strategy.service
systemctl restart twinengines-webui.service
systemctl status twinengines-strategy.service --no-pager -l
systemctl status twinengines-webui.service --no-pager -l
curl -sS http://127.0.0.1:8080/healthz
```

如需确认当前服务实际使用的环境：

```bash
systemctl show twinengines-strategy.service -p ExecStart -p WorkingDirectory --no-pager
systemctl show twinengines-webui.service -p ExecStart -p WorkingDirectory --no-pager
/root/TwinEngines/venv/bin/python --version
```

## 当前阶段入场规则

`src/twinengines/io/live_runner.py` 中 `_phase_gate_rule()` 当前为：

- 阶段 0：`p > 0.60` 连续 `6s`，净 EV `> 0.00`
- 阶段 1：`p > 0.65` 连续 `5s`，净 EV `> 0.02`
- 阶段 2：`p > 0.70` 连续 `4s`，净 EV `> 0.04`
- 阶段 3：`p > 0.65` 连续 `3s`，净 EV `> 0.06`
- 阶段 4：`p > 0.60` 连续 `2s`，净 EV `> 0.08`

WebUI 的条件显示读取运行态 `req_prob`、`p_confirm_required_sec`、`req_ev` 等字段；策略服务重启后会随新规则同步显示。

## 本次踩坑记录

在 Windows PowerShell 本地通过 `python -c` 包远程 SSH 命令时，远程命令里的 `$()`、管道 `|`、`&&`、嵌套引号容易被本地 PowerShell 提前解析，导致看似 VPS 命令失败，实际是本地 shell 解析失败。

建议：

- 复杂远程命令优先写成临时 Python/Paramiko 脚本，或通过 `bash -s` stdin 发送脚本。
- 不要在一层 `python -c` 中再塞多层 shell/Python 嵌套引号。
- 验证 VPS 运行状态时优先用短命令分步确认。

## 环境清理记录

- 保留：`/root/TwinEngines/venv`，这是 systemd 当前使用的 Python 3.11 环境。
- 已删除：`/root/TwinEngines/.venv`，这是未被服务引用的旧 Python 3.6 环境。
- 不要删除 `.env`，不要打印或修改私钥/API key/proxy/真实下单开关。
