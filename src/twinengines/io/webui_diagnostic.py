"""
AI 诊断包打包 (diagnostic bundle for AI maintenance).

设计目标:
    用户在面板点一次按钮, 我作为后续维护 AI 收到的就是
    "可以独立分析、不需要再问、不漏敏感信息" 的完整上下文.

打包内容 (按板块):
    1. 摘要 (system + git + python + uptime)
    2. 当前策略状态 (running / mode / regime / equity)
    3. 24h 信号频率 vs 回测基线
    4. 24h 小时分桶 (trend / reversal / triggered)
    5. 7d 订单结局统计 (filled / failed / shadow)
    6. artifact 摘要 (params + thresholds + ci + data_meta)
    7. 脱敏 .env (DRY_RUN / endpoints / 阈值参数 等)
    8. 最近 20 笔影子信号 (完整 dict)
    9. 最近 20 条告警
   10. 最近 100 行 ERROR/WARNING 日志
   11. 给 AI 的固定提示词 (复制即可用)

输出:
    - build_markdown() -> 单个 Markdown 字符串 (推荐, 复制即可用)
    - build_zip(path) -> 把 .md + 原始 artifact + 原始 alerts 截断片段打包成 zip

模块化:
    - 不 import 任何策略代码
    - 全部数据来自 webui_data / webui_config / webui_system / webui_proc
    - 任意子项失败都不影响其它项 (单项 try/except)
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .webui_config import ConfigSnapshot
from .webui_data import (
    AlertsTail,
    HourlyMetrics,
    LogsTail,
    OrdersTail,
    RegimeInfo,
    ShadowSignalsTail,
    SignalCounts,
    TriggersTail,
)
from .webui_proc import ProcessManager
from .webui_system import SystemInfo


# ============================================================
# 给 AI 的固定提示词 (中文, 可直接发给 Claude)
# ============================================================

AI_PROMPT_TEMPLATE = """\
你好，请基于下方诊断包对 TwinEngines 项目进行**只读分析**：

1. 看 `2_strategy_status` 与 `3_signal_frequency`：策略当前是否在跑、信号是否在产生、与回测基线 (顺势 ~19/天、反转 ~1/天) 偏差是否在 ±30% 内
2. 看 `4_hourly_buckets`：是否有时段集中或时段空洞 (例如某 6 小时无任何 triggered)
3. 看 `5_order_outcomes`：filled / failed / shadow 各类订单的比例是否健康 (failed 占比 < 5%)
4. 看 `6_artifact_summary`：thresholds 与训练 CI 是否仍在合理区间
5. 看 `9_recent_alerts` 与 `10_recent_log_lines`：是否有 fatal 类告警 / WS 长时间断线 / 时钟漂移过大
6. 输出结构: (a) 健康判定 (绿/黄/红) (b) 必须立即处理的风险项 (c) 建议优化项 (不直接改策略代码)

**禁止动作**:
- 不要修改信号层参数 (stability sec / 概率阈值 / baseline offset)
- 不要修改仓位计算核心逻辑 (kelly / max stake)
- 不要假设诊断包外的数据 (如果信息缺失就直说"信息不足")
"""


# ============================================================
# 主打包器
# ============================================================

@dataclass
class DiagnosticBuilder:
    project_root: str = "."
    env_file: str = ".env"
    state_db_path: str = "data_runtime/state.sqlite"
    alerts_path: str = "logs/alerts.jsonl"
    shadow_signals_path: str = "logs/shadow_signals.jsonl"
    log_dir: str = "logs"
    default_artifact: str = "artifact_btc_v6.json"

    # ---------------- 主入口 ----------------

    def build_dict(
        self,
        *,
        hourly_hours: int = 24,
        recent_signal_n: int = 20,
        recent_alert_n: int = 30,
        recent_log_n: int = 100,
        order_window_hours: float = 168.0,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "_built_at_ms": int(time.time() * 1000),
            "_built_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "_schema_version": "1.0",
        }
        out["1_system"] = self._safe(SystemInfo(project_root=self.project_root).snapshot)
        out["2_strategy_status"] = self._safe(self._strategy_status)
        out["3_signal_frequency"] = self._safe(
            lambda: SignalCounts(state_db_path=self.state_db_path).today(hours=24.0))
        out["4_hourly_buckets"] = self._safe(
            lambda: HourlyMetrics(state_db_path=self.state_db_path)
                .signal_frequency(hours=hourly_hours))
        out["5_order_outcomes"] = self._safe(
            lambda: HourlyMetrics(state_db_path=self.state_db_path)
                .order_outcomes(hours=order_window_hours))
        out["6_artifact_summary"] = self._safe(
            lambda: ConfigSnapshot(env_file=self.env_file,
                                   artifact_path=self.default_artifact).build()
                .get("artifact", {}))
        out["7_env_redacted"] = self._safe(
            lambda: ConfigSnapshot(env_file=self.env_file,
                                   artifact_path=self.default_artifact).build()
                .get("env_redacted", {}))
        out["8_recent_shadow_signals"] = self._safe(
            lambda: ShadowSignalsTail(path=self.shadow_signals_path)
                .recent(n=recent_signal_n))
        out["9_recent_alerts"] = self._safe(
            lambda: AlertsTail(path=self.alerts_path).recent(n=recent_alert_n))
        out["10_recent_log_lines"] = self._safe(
            lambda: LogsTail(log_dir=self.log_dir).tail(n=recent_log_n))
        out["11_recent_window_triggers"] = self._safe(
            lambda: TriggersTail(state_db_path=self.state_db_path).recent(n=20))
        out["12_recent_orders"] = self._safe(
            lambda: OrdersTail(state_db_path=self.state_db_path).recent(n=30))
        out["13_regime"] = self._safe(
            lambda: RegimeInfo(state_db_path=self.state_db_path).current())
        return out

    def build_markdown(self, **kwargs) -> str:
        d = self.build_dict(**kwargs)
        lines: list[str] = []
        lines.append("# TwinEngines AI 诊断包")
        lines.append("")
        lines.append(f"_生成时间: {d.get('_built_at_iso', '?')}_  "
                     f"_schema: {d.get('_schema_version', '?')}_")
        lines.append("")
        lines.append("> **本文件可直接复制粘贴给 Claude (或其他 AI) 做分析。**")
        lines.append("> 私钥/Token 已脱敏；包含约 2~5 KB 的运行时上下文。")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## 0. 给 AI 的提示词")
        lines.append("")
        lines.append("```")
        lines.append(AI_PROMPT_TEMPLATE.strip())
        lines.append("```")
        lines.append("")

        sections = [
            ("1_system", "## 1. 系统 / Git / Python"),
            ("2_strategy_status", "## 2. 策略当前状态"),
            ("13_regime", "## 13. 当前 Regime"),
            ("3_signal_frequency", "## 3. 24h 信号频率 vs 回测基线"),
            ("4_hourly_buckets", "## 4. 24h 小时分桶 (信号 + 触发)"),
            ("5_order_outcomes", "## 5. 7d 订单结局统计"),
            ("6_artifact_summary", "## 6. Artifact 摘要 (params/thresholds/CI/data_meta)"),
            ("7_env_redacted", "## 7. 脱敏 .env (私钥已 [REDACTED])"),
            ("11_recent_window_triggers", "## 11. 最近 20 个 5min 窗口触发"),
            ("12_recent_orders", "## 12. 最近 30 笔订单 (filled / failed / shadow)"),
            ("8_recent_shadow_signals", "## 8. 最近 20 条空跑信号"),
            ("9_recent_alerts", "## 9. 最近 30 条告警"),
            ("10_recent_log_lines", "## 10. 最近 100 行日志"),
        ]
        for key, title in sections:
            lines.append(title)
            lines.append("")
            payload = d.get(key)
            if payload is None or (isinstance(payload, (dict, list)) and not payload):
                lines.append("_（无数据）_")
            elif key == "10_recent_log_lines" and isinstance(payload, list):
                lines.append("```")
                for it in payload:
                    if isinstance(it, dict):
                        lines.append(f"[{it.get('level', '?')}] {it.get('text', '')}")
                    else:
                        lines.append(str(it))
                lines.append("```")
            else:
                lines.append("```json")
                lines.append(json.dumps(payload, ensure_ascii=False,
                                       indent=2, default=str))
                lines.append("```")
            lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("_END OF DIAGNOSTIC BUNDLE_")
        return "\n".join(lines)

    def build_zip(self, output_path: str, **kwargs) -> str:
        """打包 .md + 原始 artifact + 部分原始日志为 zip, 返回 zip 路径."""
        out_p = Path(output_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        md_text = self.build_markdown(**kwargs)
        with zipfile.ZipFile(out_p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("DIAGNOSTIC.md", md_text)
            zf.writestr("DIAGNOSTIC.json",
                        json.dumps(self.build_dict(**kwargs),
                                   ensure_ascii=False, indent=2, default=str))
            self._add_file(zf, self.default_artifact, "artifact.json", max_kb=200)
            self._add_file(zf, self.alerts_path, "alerts_tail.jsonl", max_kb=200)
            self._add_file(zf, self.shadow_signals_path,
                           "shadow_signals_tail.jsonl", max_kb=200)
            self._add_file(zf, str(Path(self.log_dir) / "twinengines.log"),
                           "twinengines_tail.log", max_kb=300)
        return str(out_p)

    # ---------------- helpers ----------------

    def _strategy_status(self) -> dict[str, Any]:
        pm = ProcessManager(project_root=self.project_root, env_file=self.env_file)
        s = pm.status()
        return {
            "running": s.running, "pid": s.pid, "mode": s.mode,
            "started_at_ms": s.started_at_ms, "stale_pidfile": s.stale_pidfile,
            "cmd": s.cmd,
        }

    def _safe(self, fn) -> Any:
        try:
            return fn()
        except Exception as e:
            return {"_error": str(e), "_type": type(e).__name__}

    @staticmethod
    def _add_file(zf: zipfile.ZipFile, path: str, name_in_zip: str,
                  *, max_kb: int = 200) -> None:
        p = Path(path)
        if not p.is_file():
            return
        try:
            data = p.read_bytes()
            if len(data) > max_kb * 1024:
                # 取末尾, 给个标记
                data = (b"... [TRUNCATED HEAD, last %d KB] ...\n" % max_kb
                        + data[-max_kb * 1024:])
            zf.writestr(name_in_zip, data)
        except Exception:
            pass
