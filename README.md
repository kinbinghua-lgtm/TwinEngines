# TwinEngines（精简运行树）

默认实盘策略：`python -m twinengines.cli naked-third-digit-live --model-json configs/example_third_digit_dynamic.json --yes-real-money`（参数见 `--help`）。

自动开机实盘：`deploy/systemd/twinengines-strategy.service`（systemd）。

- 策略配置：`artifact_btc_v5.json`（含 `shadow_engine` 经验顺势与 EV 阈值）。
- 部署：`scripts/vps_deploy_remote.py`（需环境变量 `VPS_PASS`）。
- 仅更新 artifact 并重启 WebUI：`scripts/quick_upload_artifact_restart_webui.py`。
