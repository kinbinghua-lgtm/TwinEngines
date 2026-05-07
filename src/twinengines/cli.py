"""
TwinEngines 命令行入口（v2 子命令版）。

子命令:
    download    - 从 data.binance.vision 增量下载 1m / 1s klines 到本地缓存
    fit         - 在 train 段拟合生存模型 (区间生存似然)
    calibrate   - 在独立的 valid 段做十分位标定, 输出反转概率阈值
    backtest    - 用 fit/calibrate 产出的 artifact 在 test 段回测双引擎
    signal      - 加载 artifact, 给定一段 1s klines + 触发, 输出当前应执行信号
    pipeline    - 一次性完成 fit -> calibrate -> backtest, 持久化 artifact
    connectivity - 实盘连通性检查 (Binance / Polymarket / Polygon)
    smoke-real-order - 单笔最小合规实盘买单验证（真实扣款，需 --yes-real-money）
    live-run     - 实盘主循环 (默认 dry_run)
    shadow-report - 影子单阶段自检 (alerts.jsonl + state.sqlite → JSON/Markdown)
    shadow-window-stats - 仅从 state.sqlite 审计表统计触发窗 / 发射信号 / 触发但未发射
    third-digit-tune-validate - third_digit 策略下网格搜索 obs_step + 样本外验证 + 滚动切片
    sec-causal-eval - 币安 vision 1s/1m 下载并做秒级因果准确率曲线（对照分钟插值）
    sec-short-multi-valid - 短日历训练 + 多段验证 + 观测段内按分钟的因果方向准确率（秒级路径）
    cross-window-next-open - 上一窗特征预测下一窗涨跌（下一窗收盘相对下一窗开盘价）
    naked-third-digit-live - third_digit 裸因果推断 + Polymarket 极简下单（对照实验）

设计原则:
    - 各阶段产物 = 一份独立 JSON, 互不耦合
    - 全局通用参数 (--symbol/--cache) 提到 download 子命令
    - 其他子命令通过 --bars1m / --bars1s parquet 路径交换数据
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .backtest.engine import BacktestConfig, run_backtest
from .backtest.metrics import summarize
from .backtest.polymarket import PolymarketCfg
from .calibration.decile import calibrate_threshold
from .data.binance import synthesize_minute_bars
from .data.binance_downloader import (
    DownloadConfig,
    download_range,
    parse_iso_date,
    yesterday_utc,
)
from .data.window import build_windows, windows_to_event_table, DEFAULT_OBS_STEP_SEC
from .model.fit import fit_survival_mle, time_split_three
from .model.persist import StrategyArtifact, load_artifact, save_artifact
from .risk.limits import RiskCfg
from .signals import SignalThresholds

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="twinengines")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("download", help="download binance klines")
    d.add_argument("--symbol", default="BTCUSDT")
    d.add_argument("--interval", default="1m", choices=["1m", "1s"])
    d.add_argument("--start", required=True, help="YYYY-MM-DD")
    d.add_argument("--end", default="", help="YYYY-MM-DD; default = yesterday UTC")
    d.add_argument("--cache", default="data_cache")
    d.add_argument("--out", default="", help="optional combined parquet path")

    pl = sub.add_parser("pipeline", help="end-to-end fit + calibrate + backtest")
    pl.add_argument("--bars1m", default="", help="1m klines parquet path")
    pl.add_argument("--bars1s", default="", help="1s klines parquet path (optional)")
    pl.add_argument("--synthetic-minutes", type=int, default=0,
                    help="if >0 and bars1m empty, synthesize this many minutes")
    pl.add_argument("--seed", type=int, default=7)
    pl.add_argument("--obs-step-sec", type=int, default=DEFAULT_OBS_STEP_SEC)
    pl.add_argument("--label-mode", choices=["terminal", "touch"], default="terminal",
                    help="生存模型训练标签: terminal=窗末状态(默认), touch=盘中触碰")
    pl.add_argument("--train-ratio", type=float, default=0.6)
    pl.add_argument("--valid-ratio", type=float, default=0.2)
    pl.add_argument("--significance-margin", type=float, default=0.03)
    pl.add_argument("--reversal-edge-min", type=float, default=0.05)
    pl.add_argument("--trend-prob-max", type=float, default=0.15)
    pl.add_argument("--trend-stability-sec", type=int, default=6)
    pl.add_argument("--trend-min-ev", type=float, default=0.0)
    pl.add_argument("--ev-pullback-ratio", type=float, default=0.05)
    pl.add_argument("--reversal-stability-sec", type=int, default=4)
    pl.add_argument("--reversal-baseline-offset", type=float, default=0.0)
    pl.add_argument("--trend-rising-tol", type=float, default=0.003)
    pl.add_argument("--reversal-falling-tol", type=float, default=0.003)
    pl.add_argument("--reversal-drawdown-tol", type=float, default=0.05)
    pl.add_argument("--n-cv-folds", type=int, default=0)
    pl.add_argument("--n-bootstrap", type=int, default=0)
    pl.add_argument("--bootstrap-block-size", type=int, default=50)
    pl.add_argument("--out-artifact", default="artifact.json")
    pl.add_argument("--out-report", default="backtest_report.json")

    sg = sub.add_parser("signal", help="produce live signal from a single window context")
    sg.add_argument("--artifact", required=True)
    sg.add_argument("--trigger", required=True, choices=["111", "000"])
    sg.add_argument("--baseline", type=float, required=True)
    sg.add_argument("--current-price", type=float, required=True)
    sg.add_argument("--seconds-left", type=int, required=True)

    gp = sub.add_parser("growth-path", help="compare conservative/aggressive/evolutive vs initial equity")
    gp.add_argument("--bars1m", required=True)
    gp.add_argument("--bars1s", default="")
    gp.add_argument("--artifact", required=True)
    gp.add_argument("--obs-step-sec", type=int, default=DEFAULT_OBS_STEP_SEC)
    gp.add_argument("--equities", type=str, default="20,50,100,500,1000,10000",
                    help="csv floats, initial equity values to test")
    gp.add_argument("--strategies", type=str, default="CONSERVATIVE,AGGRESSIVE,EVOLUTIVE")
    gp.add_argument("--use-test-only", action="store_true",
                    help="restrict to last 20%% of windows like other backtests")
    gp.add_argument("--out", required=True)

    vv = sub.add_parser("v4-vs-v5", help="attribution diagnosis: old (v4) vs new (v5) trend rules")
    vv.add_argument("--bars1m", required=True)
    vv.add_argument("--bars1s", default="")
    vv.add_argument("--artifact", required=True)
    vv.add_argument("--obs-step-sec", type=int, default=DEFAULT_OBS_STEP_SEC)
    vv.add_argument("--v4-stab", type=int, default=6)
    vv.add_argument("--v5-stab", type=int, default=10)
    vv.add_argument("--v4-pullback", type=float, default=0.05)
    vv.add_argument("--v5-rising-tol", type=float, default=0.003)
    vv.add_argument("--out", required=True)

    sw = sub.add_parser("sweep", help="grid search trend/reversal stability")
    sw.add_argument("--bars1m", required=True)
    sw.add_argument("--bars1s", default="")
    sw.add_argument("--artifact", required=True)
    sw.add_argument("--obs-step-sec", type=int, default=DEFAULT_OBS_STEP_SEC)
    sw.add_argument("--trend-stab", type=str, default="6,8,10,12", help="csv ints")
    sw.add_argument("--rev-stab", type=str, default="3,4,5,6", help="csv ints")
    sw.add_argument("--rev-offset", type=str, default="0.0", help="csv floats")
    sw.add_argument("--trend-prob-max", type=float, default=0.15)
    sw.add_argument("--use-shadow-book", action="store_true",
                    help="用 shadow_signals.jsonl 的真实盘口快照替换模型成交价（无快照则回退模型价）")
    sw.add_argument("--shadow-book-path", default="logs/shadow_signals_3h_book_v2.jsonl",
                    help="影子盘口快照 jsonl 路径（默认 logs/shadow_signals_3h_book_v2.jsonl）")
    sw.add_argument("--shadow-book-mode", choices=["exact", "simulate"], default="exact",
                    help="盘口替换模式: exact=仅时间精确匹配; simulate=用快照经验分布外推")
    sw.add_argument("--no-risk-guard", action="store_true",
                    help="诊断模式")
    sw.add_argument("--friction-mode", choices=["off", "maker", "taker"], default="off")
    sw.add_argument("--taker-slippage-bps", type=float, default=300.0)
    sw.add_argument("--independent-engine-dd", action="store_true",
                    help="顺势/反转独立计算日回撤熔断 (共享全局敞口/并发/equity_floor)")
    sw.add_argument("--out", required=True)

    bc = sub.add_parser("book-compare", help="同一参数下: 模型价 vs 影子盘口替换价 对比")
    bc.add_argument("--bars1m", required=True)
    bc.add_argument("--bars1s", default="")
    bc.add_argument("--artifact", required=True)
    bc.add_argument("--obs-step-sec", type=int, default=DEFAULT_OBS_STEP_SEC)
    bc.add_argument("--trend-stability-sec", type=int, required=True)
    bc.add_argument("--reversal-stability-sec", type=int, required=True)
    bc.add_argument("--trend-prob-max", type=float, default=0.15)
    bc.add_argument("--reversal-baseline-offset", type=float, default=0.0)
    bc.add_argument("--shadow-book-path", default="logs/shadow_signals_3h_book_v2.jsonl")
    bc.add_argument("--shadow-book-mode", choices=["exact", "simulate"], default="exact")
    bc.add_argument("--no-risk-guard", action="store_true")
    bc.add_argument("--friction-mode", choices=["off", "maker", "taker"], default="off")
    bc.add_argument("--taker-slippage-bps", type=float, default=300.0)
    bc.add_argument("--independent-engine-dd", action="store_true")
    bc.add_argument("--out", required=True)

    td = sub.add_parser("trend-diagnose", help="diagnose why trend engine fires too rarely")
    td.add_argument("--bars1m", required=True)
    td.add_argument("--bars1s", default="")
    td.add_argument("--artifact", required=True)
    td.add_argument("--obs-step-sec", type=int, default=DEFAULT_OBS_STEP_SEC)
    td.add_argument("--trend-prob-max", type=float, default=0.15)
    td.add_argument("--trend-stability-sec", type=int, default=12)
    td.add_argument("--trend-min-ev", type=float, default=0.005)
    td.add_argument("--pullback-ratio", type=float, default=0.08)
    td.add_argument("--out", required=True)

    au = sub.add_parser("audit", help="comprehensive audit answering 5 strategy questions")
    au.add_argument("--bars1m", required=True)
    au.add_argument("--bars1s", default="")
    au.add_argument("--artifact", required=True, help="trained artifact json")
    au.add_argument("--obs-step-sec", type=int, default=DEFAULT_OBS_STEP_SEC)
    au.add_argument("--out", required=True, help="output JSON path")
    au.add_argument("--monthly-min-samples", type=int, default=50)

    co = sub.add_parser("connectivity", help="实盘环境连通性检查 (Binance/Polymarket/Polygon)")
    co.add_argument("--env-file", default=".env", help=".env 文件路径")
    co.add_argument("--check-allowance", action="store_true",
                    help="附加: 链上 USDC allowance 校验")
    co.add_argument("--out", default="", help="可选: 输出 JSON 报告路径")

    sk = sub.add_parser(
        "smoke-real-order",
        help="最小额实盘买单 1 笔：当前 5min BTC 市场 + 平台最小股数，验证 CLOB 成交并写入 order_filled 审计",
    )
    sk.add_argument("--env-file", default=".env", help=".env 路径")
    sk.add_argument(
        "--yes-real-money",
        action="store_true",
        help="必须指定：确认自愿承担真实资金风险",
    )
    sk.add_argument(
        "--side",
        choices=("up", "down"),
        default="up",
        help="up=买 YES token，down=买 NO token",
    )
    sk.add_argument(
        "--state-db",
        default="data_runtime/state.sqlite",
        help="写入 order_filled 审计的 SQLite（与 live-run 共用）",
    )
    sk.add_argument(
        "--max-wait-sec",
        type=float,
        default=120.0,
        help="等待成交的最长时间（秒），超时则撤单",
    )

    lr = sub.add_parser("live-run", help="实盘主循环 (默认 dry_run, 必须显式 --enable-real)")
    lr.add_argument("--env-file", default=".env", help=".env 文件路径")
    lr.add_argument("--enable-real", action="store_true",
                    help="允许真实下单 (会被 .env 双保险二次校验)")
    lr.add_argument("--seconds", type=int, default=0,
                    help=">0 时仅运行指定秒数后退出 (调试用), 0=阻塞至 Ctrl+C")
    lr.add_argument("--dry-run-signals", action="store_true",
                    help="空跑信号: 用 --artifact 加载 artifact, 仅记录信号到 SQLite + JSONL, 绝不下单")
    lr.add_argument("--record-shadow-signals", action="store_true",
                    help="实盘并存: 加载 --artifact 并记录影子信号；真实下单仍仅当 .env 关闭 DRY_RUN 且 ENABLE_REAL_ORDERS=true")
    lr.add_argument("--artifact", default="", help="direction probability model artifact PKL path")
    lr.add_argument("--shadow-signal-log", default="logs/shadow_signals.jsonl",
                    help="--dry-run-signals 输出的 jsonl 路径 (默认 logs/shadow_signals.jsonl)")

    wu = sub.add_parser(
        "webui",
        help="启动远程管理面板 (Flask, 默认 0.0.0.0:8080, 必填 --password)",
    )
    wu.add_argument("--host", default="0.0.0.0", help="绑定地址 (默认 0.0.0.0)")
    wu.add_argument("--port", type=int, default=8080, help="监听端口 (默认 8080)")
    wu.add_argument("--password", required=True, help="登录密码 (必填, >=6 位)")
    wu.add_argument("--project-root", default=".", help="TwinEngines 项目根 (默认当前目录)")
    wu.add_argument("--env-file", default=".env", help=".env 文件路径")
    wu.add_argument("--state-db", default="data_runtime/state.sqlite", help="StateStore SQLite 路径")
    wu.add_argument("--alerts", default="logs/alerts.jsonl", help="alerts.jsonl 路径")
    wu.add_argument("--log-dir", default="logs", help="日志目录 (主日志 = LOG_DIR/twinengines.log)")
    wu.add_argument("--shadow-signals", default="logs/shadow_signals.jsonl",
                    help="影子信号 jsonl 路径")
    wu.add_argument("--default-artifact", default="artifacts/direction_probability_vol_v1.pkl",
                    help="--dry-run-signals 默认 direction probability artifact 路径")

    sr = sub.add_parser(
        "shadow-report",
        help="影子单阶段自检: 解析 alerts.jsonl + state.sqlite 生成报告 (JSON/Markdown)",
    )
    sr.add_argument("--alerts", default="logs/alerts.jsonl", help="alerts.jsonl 路径")
    sr.add_argument("--state-db", default="data_runtime/state.sqlite", help="StateStore SQLite 路径")
    sr.add_argument("--window-hours", type=float, default=24.0, help="分析窗长 (小时), 用于与回测按天折算对比")
    sr.add_argument("--baseline-trend-per-day", type=float, default=901.0 / 48.0,
                    help="回测顺势笔数/天 基线 (默认 901/48)")
    sr.add_argument("--baseline-reversal-per-day", type=float, default=51.0 / 48.0,
                    help="回测反转笔数/天 基线 (默认 51/48)")
    sr.add_argument("--deviation-warn", type=float, default=0.30, help="与基线相对偏差告警阈值 (默认 0.30=±30%%)")
    sr.add_argument("--out", default="", help="JSON 报告输出路径 (空则只打印 stdout)")
    sr.add_argument("--markdown", default="", help="可选: Markdown 报告输出路径")

    sw = sub.add_parser(
        "shadow-window-stats",
        help="空跑窗口核对: audit_events 中触发 / shadow_signal / 触发但窗末无信号 等计数",
    )
    sw.add_argument("--state-db", default="data_runtime/state.sqlite", help="StateStore SQLite 路径")

    pc = sub.add_parser(
        "p-rev-calibrate",
        help="按剩余秒数分桶校准 p_rev (最小可用版)",
    )
    pc.add_argument("--shadow-signals", default="logs/shadow_signals.jsonl", help="shadow_signals.jsonl 路径")
    pc.add_argument("--out", default="data_runtime/p_rev_time_calibration.json", help="校准文件输出路径")
    pc.add_argument("--window-hours", type=float, default=168.0, help="使用最近 N 小时样本")
    pc.add_argument("--bucket-sec", type=int, default=10, help="时间分桶秒数 (建议 5~15)")
    pc.add_argument("--min-samples", type=int, default=8, help="桶最小样本数")
    pc.add_argument("--max-rows", type=int, default=8000, help="最多读取多少条 shadow_signal")

    sv = sub.add_parser(
        "survival-validate",
        help="生存视角验证: 动态 λ0(d0)=a/(1+c*d0), 报告 P(生存)=exp(-H) 对 z=1-反转",
    )
    sv.add_argument("--bars1m", default="", help="1m klines parquet 路径")
    sv.add_argument("--bars1s", default="", help="可选 1s parquet")
    sv.add_argument("--synthetic-minutes", type=int, default=0,
                    help="若未提供 bars1m: 合成分钟数 (>0 时生效)")
    sv.add_argument("--seed", type=int, default=7)
    sv.add_argument("--obs-step-sec", type=int, default=DEFAULT_OBS_STEP_SEC)
    sv.add_argument("--label-mode", choices=["terminal", "touch"], default="terminal")
    sv.add_argument("--train-ratio", type=float, default=0.6)
    sv.add_argument("--valid-ratio", type=float, default=0.2)
    sv.add_argument(
        "--calendar-split",
        action="store_true",
        help="按首窗起 48 天内时间序 80%%/20%% train+valid，再接 30 天 holdout；并输出 111/000 分层指标",
    )
    sv.add_argument(
        "--reversal-metrics",
        action="store_true",
        help="与 --calendar-split 联用: 输出反转 event 的 P(反转)=1-exp(-H) 指标；默认(不指定)为生存 z 指标",
    )
    sv.add_argument("--span-days", type=int, default=48, help="与 --calendar-split 联用: 首段日历天数")
    sv.add_argument("--holdout-days", type=int, default=30, help="与 --calendar-split 联用: holdout 天数")
    sv.add_argument(
        "--train-frac-in-span",
        type=float,
        default=0.8,
        help="与 --calendar-split 联用: span-days 内前多少比例算 train (余为 valid)",
    )
    sv.add_argument("--out-json", default="", help="可选: 写入 JSON 报告路径")

    td = sub.add_parser(
        "third-digit-tune-validate",
        help="third_digit: 调 obs_step；默认因果 H（第3分末起可调滞后秒）+ 留出与滚动；可加 --full-path-eval 对照",
    )
    td.add_argument("--tune-minutes", type=int, default=120 * 1440, help="调参用合成分钟数")
    td.add_argument("--tune-seed", type=int, default=3)
    td.add_argument("--validate-minutes", type=int, default=60 * 1440, help="验证用合成分钟数(样本外 seed)")
    td.add_argument("--validate-seed", type=int, default=42)
    td.add_argument("--tune-span-days", type=int, default=48, help="调参日历 span 内 train/valid")
    td.add_argument("--tune-train-frac", type=float, default=0.8)
    td.add_argument(
        "--obs-step-grid",
        type=str,
        default="5,10,15,20",
        help="逗号分隔候选 obs_step_sec",
    )
    td.add_argument("--holdout-train-days", type=float, default=45.0)
    td.add_argument("--holdout-test-days", type=float, default=15.0)
    td.add_argument("--rolling-train-days", type=float, default=35.0)
    td.add_argument("--rolling-test-days", type=float, default=10.0)
    td.add_argument("--rolling-step-days", type=float, default=5.0)
    td.add_argument("--minute-vol", type=float, default=0.0008)
    td.add_argument("--minute-drift", type=float, default=0.0)
    td.add_argument("--label-mode", choices=["terminal", "touch"], default="terminal")
    td.add_argument(
        "--causal-decision-sec",
        type=float,
        default=0.0,
        help="因果评分：相对第3分钟末观测起点滞后若干秒再决策（0=恰在第3分末）",
    )
    td.add_argument(
        "--full-path-eval",
        action="store_true",
        help="评分改用全路径 H（会用决策点后路径；不等价实盘，仅供对照）",
    )
    td.add_argument("--out-json", default="", help="写入完整 JSON 报告")

    sk = sub.add_parser(
        "sec-causal-eval",
        help="BTCUSDT 币安 vision 1s/1m 下载 → third_digit 秒级(obs_step=1)训练 + 因果准确率曲线（对照分钟插值）",
    )
    sk.add_argument("--cache-dir", default="data_cache", help="vision 分片缓存目录")
    sk.add_argument("--days", type=int, default=7, help="日历天数（含起止）；与 --start/--end 互斥优先 explicit")
    sk.add_argument("--start", default="", help="起始日 YYYY-MM-DD（指定则忽略 --days）")
    sk.add_argument("--end", default="", help="结束日 YYYY-MM-DD；默认昨天 UTC")
    sk.add_argument("--span-days", type=int, default=5, help="日历划分：首段 span 天数（train+valid）")
    sk.add_argument("--holdout-days", type=int, default=2, help="紧随 span 的 holdout 天数")
    sk.add_argument("--train-frac", type=float, default=0.8, help="span 内时间序 train 比例")
    sk.add_argument("--curve-step-sec", type=int, default=10, help="准确率曲线采样步长（秒）")
    sk.add_argument(
        "--fallback-days",
        type=int,
        default=1,
        help="当 1s 行数过少时自动改下载最近这么多天重试（0=禁用）",
    )
    sk.add_argument("--out-json", default="reports/sec_causal_eval.json", help="JSON 报告路径")

    ssh = sub.add_parser(
        "sec-short-multi-valid",
        help=(
            "秒级路径(obs_step=1)拟合动态 λ₀ + 时间序多段验证；每分钟 causal elapsed 的 direction_accuracy "
            "(与实盘 third_digit 一致)。可选 holdout。方案B例: --train-days 28 --valid-days-each 3,3 --holdout-days 5"
        ),
    )
    ssh.add_argument("--cache-dir", default="data_cache", help="vision 分片缓存目录")
    ssh.add_argument("--symbol", default="BTCUSDT", help="合约代码，如 BTCUSDT")
    ssh.add_argument(
        "--train-days",
        type=int,
        default=7,
        help="训练段连续日历天数（方案B建议总跨度的约 70%%–80%%，如 28/37）",
    )
    ssh.add_argument(
        "--valid-days-each",
        type=str,
        default="1,1,2",
        help="逗号分隔：紧接训练段后的多段验证日历天（互不重叠；双段如 3,3 便于看稳定性）",
    )
    ssh.add_argument(
        "--holdout-days",
        type=int,
        default=0,
        help="可选：各验证段之后的连续 holdout 日历天（不参与拟合，仅评估曲线）；0=关闭；建议 4–7",
    )
    ssh.add_argument(
        "--obs-start-offset",
        type=int,
        default=3,
        choices=[1, 3],
        help="观测起点：3=第3分钟末起120s(现行)；1=第1分钟末起240s(更长观测)",
    )
    ssh.add_argument("--label-mode", choices=["terminal", "touch"], default="terminal")
    ssh.add_argument(
        "--trigger-mode",
        default="third_digit",
        choices=["third_digit", "111_000", "first_digit"],
    )
    ssh.add_argument("--end", default="", help="区间结束日 YYYY-MM-DD；默认昨天 UTC")
    ssh.add_argument(
        "--start",
        default="",
        help="区间起始日；默认按 train-days + 各验证天数 + holdout-days 从 end 倒推",
    )
    ssh.add_argument(
        "--fallback-days",
        type=int,
        default=1,
        help="当 1s 行数过少时自动改下载最近这么多天重试（0=禁用）",
    )
    ssh.add_argument("--out-json", default="reports/sec_short_multi_valid.json", help="JSON 报告路径")

    cw = sub.add_parser(
        "cross-window-next-open",
        help=(
            "相邻 5m 窗配对：标签=下一窗 close_m5 相对下一窗开盘价；"
            "报告持续性 / 多数类 / 上一窗收益率逻辑回归 / 探索性 P_rev 相关"
        ),
    )
    cw.add_argument("--cache-dir", default="data_cache")
    cw.add_argument("--symbol", default="BTCUSDT")
    cw.add_argument("--train-days", type=int, default=7)
    cw.add_argument(
        "--valid-days-each",
        type=str,
        default="7",
        help="逗号分隔多段验证日历天数（默认单段 7 天）",
    )
    cw.add_argument("--obs-start-offset", type=int, default=3, choices=[1, 3])
    cw.add_argument(
        "--trigger-mode",
        default="third_digit",
        choices=["third_digit", "111_000", "first_digit"],
    )
    cw.add_argument("--end", default="", help="结束日 YYYY-MM-DD；默认昨天 UTC")
    cw.add_argument("--start", default="", help="起始日；默认从 end 倒推 train+valid 总天数")
    cw.add_argument("--fallback-days", type=int, default=1)
    cw.add_argument("--out-json", default="reports/cross_window_next_open.json")

    st = sub.add_parser(
        "sequence-transition",
        help="统计 5m 序列号的一阶转移：prev_sequence -> next_sequence / next final direction",
    )
    st.add_argument("--cache-dir", default="data_cache")
    st.add_argument("--symbol", default="BTCUSDT")
    st.add_argument("--days", type=int, default=365, help="默认下载/读取最近 365 天 1m 数据")
    st.add_argument("--start", default="", help="起始日 YYYY-MM-DD；指定后忽略 --days 倒推")
    st.add_argument("--end", default="", help="结束日 YYYY-MM-DD；默认昨天 UTC")
    st.add_argument("--alpha", type=float, default=20.0, help="Beta 平滑 alpha")
    st.add_argument("--min-prev-n", type=int, default=500, help="全年/训练段单个 prev_sequence 最小样本")
    st.add_argument("--min-prev-n-month", type=int, default=30, help="月度稳定性单月最小样本")
    st.add_argument("--holdout-days", type=int, default=90, help="末尾 holdout 天数")
    st.add_argument("--holdout-edge-min", type=float, default=0.03, help="训练概率偏离 50%% 至少多少才生成规则")
    st.add_argument("--top-k", type=int, default=20)
    st.add_argument("--out-json", default="reports/sequence_transition_1y.json")

    pm = sub.add_parser("fit-prefix-survival", help="训练 first3/first4 前缀分桶动态生存模型 JSON")
    pm.add_argument("--cache-dir", default="data_cache")
    pm.add_argument("--symbol", default="BTCUSDT")
    pm.add_argument("--start", required=True, help="起始日 YYYY-MM-DD")
    pm.add_argument("--end", required=True, help="结束日 YYYY-MM-DD")
    pm.add_argument("--obs-step-sec", type=int, default=1)
    pm.add_argument("--min-bucket-n", type=int, default=100)
    pm.add_argument("--shrink-l2", type=float, default=2.0)
    pm.add_argument("--base-model-json", default="", help="可选：旧 third_digit_dynamic_v1 JSON，作为全局参数先验")
    pm.add_argument("--gamma", type=float, default=0.1)
    pm.add_argument("--beta", type=float, default=-8.0)
    pm.add_argument("--dynamic-a", type=float, default=0.007561598853351611)
    pm.add_argument("--dynamic-c", type=float, default=24.545266853729263)
    pm.add_argument("--out-json", default="reports/prefix_survival_model.json")

    nk = sub.add_parser(
        "naked-third-digit-live",
        help=(
            "常驻轮询（默认）：等当前 5m 合约 → 第 3 分钟收盘形成 third_digit → "
            "因果 survival 算涨跌概率 → 超阈值则吃盘；加 --once 只做一圈方便调试"
        ),
    )
    nk.add_argument("--model-json", default="", help="dynamic λ0 参数 JSON（schema third_digit_dynamic_v1）")
    nk.add_argument(
        "--write-example-model",
        default="",
        help="写入示例 model-json 到路径并退出（填入离线拟合值后再跑）",
    )
    nk.add_argument(
        "--confidence-min",
        type=float,
        default=0.501,
        help=(
            "押边概率 confidence_on_pick 下限；默认 0.501。"
            ">0.56 时 naked 入场取消「FOK 限价须 <0.5」约束，仅按 0.51 分界拆 FOK/GTC。"
            "设为 0 则不挡；可加严例如 --confidence-min 0.7"
        ),
    )
    nk.add_argument("--target-quote-usdc", type=float, default=1.0, help="目标名义；Kelly 开启时为下限 floor；实际可能受最小股数抬高")
    nk.add_argument("--poll-sec", type=float, default=8.0)
    nk.add_argument(
        "--max-runtime-min",
        type=float,
        default=0.0,
        help="最多运行多少分钟后自动退出（0=不限制；例如 60 表示跑一小时）",
    )
    nk.add_argument(
        "--once",
        action="store_true",
        help="仅执行一次 tick 后退出（默认不设此项则按 --poll-sec 无限轮询）",
    )
    nk.add_argument(
        "--yes-real-money",
        action="store_true",
        help=(
            "真实下单：挂单链路通过后向 Polymarket CLOB 提交限价买单（将动用资金）；"
            "须在运行时配置/.env 中允许实盘（is_real_order_allowed）。"
            "不加此项仅为 dry-run，日志 action=would_submit_dry_run_need_yes_real_money。"
            "不要与 --bare-formula-eval 同时使用。"
        ),
    )
    nk.add_argument(
        "--no-funds-manager",
        action="store_true",
        help="关闭 naked 资金管理（RiskGuard + DAILY_MAX_LOSS_USDC 日损锚定）；实盘默认启用",
    )
    nk.add_argument(
        "--kelly-sizing",
        action="store_true",
        help="启用保守分数 Kelly 动态名义（随净值放大，受 max_stake_ratio 封顶）；未开时仅用 --target-quote-usdc",
    )
    nk.add_argument(
        "--kelly-fraction",
        type=float,
        default=0.125,
        help="分数 Kelly 乘子（默认 0.125≈1/8 Kelly，偏保守）",
    )
    nk.add_argument(
        "--kelly-max-stake-ratio",
        type=float,
        default=0.04,
        help="单笔名义占净值比例硬顶（默认 0.04=4%%，与 risk.sizing 内 max_stake_ratio 一致）",
    )
    nk.add_argument("--env-file", default="", help=".env 路径（空则用默认加载顺序）")
    nk.add_argument(
        "--state-json",
        default="data_runtime/naked_third_digit_state.json",
        help="防止同一 condition_id 重复下单的状态文件",
    )
    nk.add_argument(
        "--live-log-jsonl",
        default="logs/naked_live_ticks.jsonl",
        help="实盘/仿真实时明细输出 JSONL（每 tick + TP 事件一行）",
    )
    nk.add_argument(
        "--eval-delay-after-min3-sec",
        type=float,
        default=0.0,
        help="第 3 根收盘后再推迟几秒才开始评估/挂单（仅时间门槛；默认动态推断仍会随后续已收盘K与剩余时间更新）",
    )
    nk.add_argument(
        "--frozen-minute3-only",
        action="store_true",
        help="恢复旧行为：仅用第3末 d0 + 固定120s 快照算 H，不随第4/5根与剩余时间更新",
    )
    nk.add_argument(
        "--no-1s-dynamic",
        action="store_true",
        help="关闭 Binance 1s 动态核，仅用分钟收盘结点路径（仍非冻结，除非再加 --frozen-minute3-only）",
    )
    nk.add_argument(
        "--sec-path-stride",
        type=int,
        default=1,
        help="1s 路径结点降采样：每 N 秒保留一个结点（默认 1=全保留；减轻 REST 路径长度）",
    )
    nk.add_argument(
        "--require-positive-edge",
        action="store_true",
        help="除置信度外要求 best_ask 低于模型对该侧的公平概率（fair_prob_side），否则不下单",
    )
    nk.add_argument(
        "--skip-before-expiry-sec",
        type=float,
        default=0.0,
        help="距到期过近不下单；默认 0=不启用到期刹车。>0 时在最后 N 秒禁止挂单（bare-formula-eval 仍忽略）",
    )
    nk.add_argument(
        "--bare-formula-eval",
        action="store_true",
        help="纯公式验证：不做过期刹车、不做同一合约去重、不拉盘口不下单；仅输出 prediction（action=bare_formula_tick）",
    )
    nk.add_argument(
        "--max-market-rollovers",
        type=int,
        default=0,
        help="Gamma 活动合约 condition_id 切换满此次数后退出（0=常驻；2≈经历两轮换约）",
    )
    nk.add_argument(
        "--log-p-rev-range",
        action="store_true",
        help="换约时在 window_verdict 里附带本窗内见过的 p_rev 最小/最大与最后一次（便于核对秒级漂移）",
    )

    sc = sub.add_parser(
        "snapshot-config",
        help="生成摩擦参数快照建议 (不自动应用)",
    )
    sc.add_argument("--state-db", default="data_runtime/state.sqlite")
    sc.add_argument("--shadow-signals", default="logs/shadow_signals.jsonl")
    sc.add_argument("--snapshot", default="data_runtime/friction_snapshot.json")
    sc.add_argument("--proposal-out", default="reports/param_update_proposal.json")
    sc.add_argument("--window-days", type=int, default=30)
    sc.add_argument("--max-change-ratio", type=float, default=0.30)
    sc.add_argument("--min-update-interval-days", type=int, default=30)

    ap = sub.add_parser(
        "apply-snapshot",
        help="人工审核后应用摩擦参数建议到快照",
    )
    ap.add_argument("--proposal", default="reports/param_update_proposal.json")
    ap.add_argument("--snapshot", default="data_runtime/friction_snapshot.json")
    ap.add_argument("--state-db", default="data_runtime/state.sqlite")
    ap.add_argument("--operator", default="manual")
    ap.add_argument("--force-manual", action="store_true",
                    help="强制应用被标记 requires_manual_intervention 的项")

    return p

def _load_or_synthesize_1m(args: argparse.Namespace) -> pd.DataFrame:
    if args.bars1m:
        return pd.read_parquet(args.bars1m)
    if args.synthetic_minutes > 0:
        bars = synthesize_minute_bars(n_minutes=args.synthetic_minutes, seed=args.seed)
        return bars.df
    raise SystemExit("must provide --bars1m or --synthetic-minutes > 0")

def _load_1s(args: argparse.Namespace) -> pd.DataFrame | None:
    if not args.bars1s:
        return None
    return pd.read_parquet(args.bars1s)

def _cmd_download(args: argparse.Namespace) -> int:
    end = parse_iso_date(args.end) if args.end else yesterday_utc()
    start = parse_iso_date(args.start)
    cfg = DownloadConfig(
        symbol=args.symbol,
        interval=args.interval,
        cache_dir=Path(args.cache),
    )
    print(f"downloading {args.symbol} {args.interval} {start}..{end}", flush=True)
    df = download_range(cfg, start, end)
    print(f"got {len(df)} rows", flush=True)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(args.out, index=False)
        print(f"wrote {args.out}", flush=True)
    return 0

def _cmd_naked_third_digit_live(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .live.naked_pm_runner import run_naked_third_digit_loop
    from .live.third_digit_naked import save_example_params_json

    out_path = str(args.write_example_model).strip()
    if out_path:
        save_example_params_json(Path(out_path))
        print(f"[naked-third-digit-live] wrote example model JSON -> {out_path}", flush=True)
        return 0

    mj = str(args.model_json).strip()
    if not mj:
        print("[naked-third-digit-live] 需要 --model-json 或使用 --write-example-model PATH", file=sys.stderr)
        return 2

    if bool(args.bare_formula_eval) and bool(args.yes_real_money):
        print(
            "[naked-third-digit-live] --bare-formula-eval 与 --yes-real-money 互斥（裸公式不拉盘不下单）",
            file=sys.stderr,
        )
        return 2

    env_file = str(args.env_file).strip() or None
    run_naked_third_digit_loop(
        model_json=Path(mj),
        env_file=env_file,
        confidence_min=float(args.confidence_min),
        target_quote_usdc=float(args.target_quote_usdc),
        poll_sec=float(args.poll_sec),
        run_once=bool(args.once),
        yes_real_money=bool(args.yes_real_money),
        causal_decision_lag_sec=float(args.eval_delay_after_min3_sec),
        skip_seconds_before_expiry=float(args.skip_before_expiry_sec),
        state_json=Path(str(args.state_json)),
        setup_logs=True,
        max_market_rollovers=int(args.max_market_rollovers) if int(args.max_market_rollovers) > 0 else None,
        frozen_minute3_only=bool(args.frozen_minute3_only),
        require_positive_edge=bool(args.require_positive_edge),
        sec_dynamic_1s=not bool(args.no_1s_dynamic),
        sec_path_stride=max(1, int(args.sec_path_stride)),
        bare_formula_eval=bool(args.bare_formula_eval),
        log_p_rev_range=bool(args.log_p_rev_range),
use_kelly_sizing=bool(args.kelly_sizing),
        kelly_fraction=float(args.kelly_fraction),
        kelly_max_stake_ratio=float(args.kelly_max_stake_ratio),
        live_log_jsonl=Path(str(args.live_log_jsonl)),
        max_runtime_min=float(args.max_runtime_min),
    )
    return 0

def _cmd_sec_causal_eval(args: argparse.Namespace) -> int:
    from .model.sec_kline_causal_eval import download_btc_1m_1s_range, run_sec_kline_causal_eval

    end_d = parse_iso_date(str(args.end).strip()) if str(args.end).strip() else yesterday_utc()
    start_s = str(args.start).strip()
    if start_s:
        start_d = parse_iso_date(start_s)
    else:
        start_d = end_d - timedelta(days=max(0, int(args.days) - 1))

    def _run_for_range(sd: date, ed: date, tag: str) -> tuple[dict[str, Any], int]:
        df_1m, df_1s, dl_meta = download_btc_1m_1s_range(
            cache_dir=str(args.cache_dir),
            start=sd,
            end=ed,
        )
        rep_doc: dict[str, Any] = {
            "download": dl_meta,
            "range_tag": tag,
            "start_date": sd.isoformat(),
            "end_date": ed.isoformat(),
        }
        if df_1m.empty:
            rep_doc["error"] = "empty_1m_after_download"
            return rep_doc, 2
        min_1s = 50_000
        if df_1s.empty or len(df_1s) < min_1s:
            rep_doc["error"] = "insufficient_1s_rows"
            rep_doc["min_1s_rows_expected"] = min_1s
            rep_doc["rows_1s"] = int(len(df_1s))
            return rep_doc, 2
        rep = run_sec_kline_causal_eval(
            df_1m=df_1m,
            df_1s=df_1s,
            span_days=int(args.span_days),
            holdout_days=int(args.holdout_days),
            train_frac_in_span=float(args.train_frac),
            elapsed_curve_step_sec=int(args.curve_step_sec),
        )
        rep_doc.update(rep.as_dict())
        return rep_doc, 0

    tag = f"{start_d.isoformat()}_{end_d.isoformat()}"
    doc, code = _run_for_range(start_d, end_d, tag)
    fb = int(args.fallback_days)
    if code != 0 and fb > 0:
        end2 = yesterday_utc()
        start2 = end2 - timedelta(days=max(0, fb - 1))
        doc["fallback_attempt"] = {"days": fb, "start": start2.isoformat(), "end": end2.isoformat()}
        doc2, code2 = _run_for_range(start2, end2, f"fallback_{fb}d")
        if code2 == 0:
            doc = doc2
            code = 0
        else:
            doc["fallback_error"] = doc2.get("error", doc2)

    out_path = Path(str(args.out_json))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: doc[k] for k in doc if k in ("download", "range_tag", "wall_clock_total_sec", "sec_path_eval", "minute_baseline_eval", "error", "fallback_attempt")}, ensure_ascii=False, default=str), flush=True)
    print(f"[sec-causal-eval] wrote {out_path}", flush=True)
    return code

def _cmd_sec_short_multi_valid(args: argparse.Namespace) -> int:
    from .model.sec_kline_causal_eval import (
        download_btc_1m_1s_range,
        run_short_train_sec_multi_valid_minute_curve,
    )

    raw_v = str(args.valid_days_each).split(",")
    valid_lens = [int(x.strip()) for x in raw_v if x.strip()]
    if not valid_lens or any(v < 1 for v in valid_lens):
        print("[sec-short-multi-valid] need --valid-days-each with one or more positive integers", file=sys.stderr)
        return 1
    train_d = max(1, int(args.train_days))
    hold_d = max(0, int(args.holdout_days))
    total_inner = train_d + sum(valid_lens) + hold_d

    end_d = parse_iso_date(str(args.end).strip()) if str(args.end).strip() else yesterday_utc()
    start_s = str(args.start).strip()
    if start_s:
        start_d = parse_iso_date(start_s)
    else:
        start_d = end_d - timedelta(days=max(0, total_inner - 1))

    sym = str(args.symbol).strip().upper() or "BTCUSDT"

    def _run(sd: date, ed: date, tag: str) -> tuple[dict[str, Any], int]:
        df_1m, df_1s, dl_meta = download_btc_1m_1s_range(
            cache_dir=str(args.cache_dir),
            start=sd,
            end=ed,
            symbol=sym,
        )
        doc: dict[str, Any] = {
            "download": dl_meta,
            "range_tag": tag,
            "start_date": sd.isoformat(),
            "end_date": ed.isoformat(),
            "train_calendar_days": train_d,
            "validation_calendar_days_each": valid_lens,
            "holdout_calendar_days": hold_d,
        }
        if df_1m.empty:
            doc["error"] = "empty_1m_after_download"
            return doc, 2
        min_1s = 50_000
        if df_1s.empty or len(df_1s) < min_1s:
            doc["error"] = "insufficient_1s_rows"
            doc["min_1s_rows_expected"] = min_1s
            doc["rows_1s"] = int(len(df_1s))
            return doc, 2
        rep = run_short_train_sec_multi_valid_minute_curve(
            df_1m=df_1m,
            df_1s=df_1s,
            train_calendar_days=train_d,
            valid_calendar_days_each=valid_lens,
            holdout_calendar_days=hold_d,
            label_mode=str(args.label_mode),
            trigger_mode=str(args.trigger_mode),
            obs_start_offset_minutes=int(args.obs_start_offset),
            obs_step_sec=1,
        )
        doc.update(rep.as_dict())
        return doc, 0

    span_days = (end_d - start_d).days + 1
    if span_days < total_inner:
        print(
            f"[sec-short-multi-valid] warning: date span {span_days}d < train+valid+holdout={total_inner}d; "
            "边界样本可能不足",
            file=sys.stderr,
        )

    tag = f"{start_d.isoformat()}_{end_d.isoformat()}"
    doc, code = _run(start_d, end_d, tag)
    fb = int(args.fallback_days)
    if code != 0 and fb > 0:
        end2 = yesterday_utc()
        start2 = end2 - timedelta(days=max(0, total_inner - 1))
        doc["fallback_attempt"] = {"days": fb, "start": start2.isoformat(), "end": end2.isoformat()}
        doc2, code2 = _run(start2, end2, f"fallback_{fb}d")
        if code2 == 0:
            doc = doc2
            code = 0
        else:
            doc["fallback_error"] = doc2.get("error", doc2)

    out_path = Path(str(args.out_json))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    summary_keys = (
        "download",
        "range_tag",
        "train_calendar_days",
        "validation_calendar_days_each",
        "holdout_calendar_days",
        "wall_clock_total_sec",
        "fit",
        "validation_segments",
        "holdout",
        "error",
        "fallback_attempt",
    )
    print(json.dumps({k: doc[k] for k in summary_keys if k in doc}, ensure_ascii=False, default=str), flush=True)
    print(f"[sec-short-multi-valid] wrote {out_path}", flush=True)
    return code

def _cmd_cross_window_next_open(args: argparse.Namespace) -> int:
    from .data.cross_window import pair_cross_window_samples
    from .data.window import build_windows
    from .model.cross_window_next_open import run_cross_window_next_open_eval
    from .model.sec_kline_causal_eval import download_btc_1m_1s_range

    raw_v = str(args.valid_days_each).split(",")
    valid_lens = [int(x.strip()) for x in raw_v if x.strip()]
    if not valid_lens or any(v < 1 for v in valid_lens):
        print("[cross-window-next-open] need --valid-days-each with positive integers", file=sys.stderr)
        return 1
    train_d = max(1, int(args.train_days))
    total_inner = train_d + sum(valid_lens)

    end_d = parse_iso_date(str(args.end).strip()) if str(args.end).strip() else yesterday_utc()
    start_s = str(args.start).strip()
    if start_s:
        start_d = parse_iso_date(start_s)
    else:
        start_d = end_d - timedelta(days=max(0, total_inner - 1))

    sym = str(args.symbol).strip().upper() or "BTCUSDT"

    def _run(sd: date, ed: date, tag: str) -> tuple[dict[str, Any], int]:
        df_1m, df_1s, dl_meta = download_btc_1m_1s_range(
            cache_dir=str(args.cache_dir),
            start=sd,
            end=ed,
            symbol=sym,
        )
        doc: dict[str, Any] = {
            "download": dl_meta,
            "range_tag": tag,
            "start_date": sd.isoformat(),
            "end_date": ed.isoformat(),
        }
        if df_1m.empty:
            doc["error"] = "empty_1m_after_download"
            return doc, 2
        min_1s = 50_000
        if df_1s.empty or len(df_1s) < min_1s:
            doc["error"] = "insufficient_1s_rows"
            doc["min_1s_rows_expected"] = min_1s
            doc["rows_1s"] = int(len(df_1s))
            return doc, 2
        samples = build_windows(
            df_1m,
            bars_1s=df_1s,
            obs_step_sec=1,
            trigger_mode=str(args.trigger_mode),  # type: ignore[arg-type]
            obs_start_offset_minutes=int(args.obs_start_offset),
        )
        pairs = pair_cross_window_samples(samples)
        doc["n_windows"] = len(samples)
        doc["n_pairs_aligned"] = len(pairs)
        rep = run_cross_window_next_open_eval(
            pairs,
            train_calendar_days=train_d,
            valid_calendar_days_each=valid_lens,
        )
        doc.update(rep)
        xcode = 1 if rep.get("error") == "insufficient_train_pairs_need_30" else 0
        return doc, xcode

    span_days = (end_d - start_d).days + 1
    if span_days < total_inner:
        print(
            f"[cross-window-next-open] warning: span {span_days}d < train+valid={total_inner}d",
            file=sys.stderr,
        )

    tag = f"{start_d.isoformat()}_{end_d.isoformat()}"
    doc, code = _run(start_d, end_d, tag)
    fb = int(args.fallback_days)
    if code != 0 and fb > 0 and doc.get("error") in ("insufficient_1s_rows", "empty_1m_after_download"):
        end2 = yesterday_utc()
        start2 = end2 - timedelta(days=max(0, total_inner - 1))
        doc["fallback_attempt"] = {"days": fb, "start": start2.isoformat(), "end": end2.isoformat()}
        doc2, code2 = _run(start2, end2, f"fallback_{fb}d")
        if code2 == 0:
            doc = doc2
            code = 0
        else:
            doc["fallback_error"] = doc2.get("error", doc2)

    out_path = Path(str(args.out_json))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: doc[k] for k in doc if k in (
        "download", "range_tag", "n_pairs_aligned", "train_metrics", "validation_segments", "error",
    )}, ensure_ascii=False, default=str), flush=True)
    print(f"[cross-window-next-open] wrote {out_path}", flush=True)
    return code

def _cmd_fit_prefix_survival(args: argparse.Namespace) -> int:
    from .data.binance_downloader import DownloadConfig, download_range
    from .live.third_digit_naked import load_third_digit_dynamic_params
    from .model.prefix_survival import PrefixDynamicModel, fit_prefix_dynamic_model

    start_d = parse_iso_date(str(args.start).strip())
    end_d = parse_iso_date(str(args.end).strip())
    sym = str(args.symbol).strip().upper() or "BTCUSDT"
    cfg_1m = DownloadConfig(symbol=sym, interval="1m", cache_dir=Path(str(args.cache_dir)))
    cfg_1s = DownloadConfig(symbol=sym, interval="1s", cache_dir=Path(str(args.cache_dir)))
    df_1m = download_range(cfg_1m, start_d, end_d, progress=True)
    df_1s = download_range(cfg_1s, start_d, end_d, progress=True)
    if df_1m.empty or df_1s.empty:
        print("[fit-prefix-survival] empty 1m or 1s data", file=sys.stderr)
        return 2

    gamma = float(args.gamma)
    beta = float(args.beta)
    dynamic_a = float(args.dynamic_a)
    dynamic_c = float(args.dynamic_c)
    base_path = str(args.base_model_json).strip()
    if base_path:
        base = load_third_digit_dynamic_params(base_path)
        if isinstance(base, PrefixDynamicModel):
            gamma = float(base.gamma)
            beta = float(base.beta)
            dynamic_a = float(base.dynamic_a)
            dynamic_c = float(base.dynamic_c)
        else:
            gamma = float(base.gamma)
            beta = float(base.beta)
            dynamic_a = float(base.dynamic_a)
            dynamic_c = float(base.dynamic_c)

    samples = build_windows(
        df_1m,
        bars_1s=df_1s,
        obs_step_sec=int(args.obs_step_sec),
        trigger_mode="third_digit",
        obs_start_offset_minutes=3,
    )
    if not samples:
        print("[fit-prefix-survival] no windows extracted", file=sys.stderr)
        return 2
    model, agg = fit_prefix_dynamic_model(
        samples,
        gamma=gamma,
        beta=beta,
        global_a=dynamic_a,
        global_c=dynamic_c,
        symbol=sym,
        min_n=int(args.min_bucket_n),
        shrink_l2=float(args.shrink_l2),
    )
    doc = model.to_json()
    doc["training_meta"] = {
        "symbol": sym,
        "start": start_d.isoformat(),
        "end": end_d.isoformat(),
        "rows_1m": int(len(df_1m)),
        "rows_1s": int(len(df_1s)),
        "n_windows": int(len(samples)),
        "n_prefix_rows": int(len(agg)),
        "obs_step_sec": int(args.obs_step_sec),
        "min_bucket_n": int(args.min_bucket_n),
        "shrink_l2": float(args.shrink_l2),
    }
    doc["bucket_summary"] = [v.to_dict() for v in sorted((model.buckets or {}).values(), key=lambda x: (len(x.prefix), x.prefix))]
    out_path = Path(str(args.out_json))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps({
        "n_windows": len(samples),
        "n_prefix_rows": len(agg),
        "n_buckets": len(model.buckets or {}),
        "out_json": str(out_path),
    }, ensure_ascii=False), flush=True)
    return 0

def _cmd_sequence_transition(args: argparse.Namespace) -> int:
    from .analysis.sequence_transition import run_sequence_transition_report
    from .data.binance_downloader import DownloadConfig, download_range

    end_d = parse_iso_date(str(args.end).strip()) if str(args.end).strip() else yesterday_utc()
    start_s = str(args.start).strip()
    if start_s:
        start_d = parse_iso_date(start_s)
    else:
        start_d = end_d - timedelta(days=max(0, int(args.days) - 1))
    sym = str(args.symbol).strip().upper() or "BTCUSDT"
    cfg = DownloadConfig(symbol=sym, interval="1m", cache_dir=Path(str(args.cache_dir)))
    df_1m = download_range(cfg, start_d, end_d, progress=True)
    doc: dict[str, Any] = {
        "download": {
            "symbol": sym,
            "interval": "1m",
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "rows_1m": int(len(df_1m)),
        }
    }
    if df_1m.empty:
        doc["error"] = "empty_1m_after_download"
    else:
        doc.update(run_sequence_transition_report(
            df_1m,
            alpha=float(args.alpha),
            min_prev_n=int(args.min_prev_n),
            min_prev_n_month=int(args.min_prev_n_month),
            holdout_days=int(args.holdout_days),
            holdout_edge_min=float(args.holdout_edge_min),
            top_k=int(args.top_k),
        ))
    out_path = Path(str(args.out_json))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps({k: doc[k] for k in doc if k in ("download", "n_windows", "n_pairs", "base_rates", "holdout_rule_check", "error")}, ensure_ascii=False, default=str), flush=True)
    print(f"[sequence-transition] wrote {out_path}", flush=True)
    return 2 if doc.get("error") else 0

    from .model.third_digit_tune_validate import run_third_digit_tune_and_validate_synthetic

    grid = [int(x.strip()) for x in str(args.obs_step_grid).split(",") if x.strip()]
    if not grid:
        print("[third-digit-tune-validate] empty --obs-step-grid", file=sys.stderr)
        return 1
    rep = run_third_digit_tune_and_validate_synthetic(
        tune_n_minutes=int(args.tune_minutes),
        tune_seed=int(args.tune_seed),
        validate_n_minutes=int(args.validate_minutes),
        validate_seed=int(args.validate_seed),
        tune_span_days=int(args.tune_span_days),
        tune_train_frac=float(args.tune_train_frac),
        obs_step_candidates=grid,
        holdout_train_days=float(args.holdout_train_days),
        holdout_test_days=float(args.holdout_test_days),
        rolling_train_days=float(args.rolling_train_days),
        rolling_test_days=float(args.rolling_test_days),
        rolling_step_days=float(args.rolling_step_days),
        minute_vol=float(args.minute_vol),
        minute_drift=float(args.minute_drift),
        label_mode=str(args.label_mode),
        eval_use_full_path=bool(args.full_path_eval),
        causal_decision_elapsed_sec=float(args.causal_decision_sec),
    )
    d = rep.as_dict()
    d["meta"] = {
        "strategy": "third_digit",
        "objective": "maximize direction_accuracy_valid on tune calendar split",
        "prediction_rule": "111: pred_up if P_rev<0.5 else down; 000: pred_up if P_rev>0.5",
        "eval_hazard": (
            "full_path_integrated_H"
            if args.full_path_eval
            else f"causal_truncated_H_decision_elapsed_sec={float(args.causal_decision_sec)}"
        ),
        "train_fit_note": "MLE/fit still uses full observation-path likelihood per window (not causal-refit)",
        "validate_seed_note": "validate corpus uses different synthetic seed from tune corpus",
    }
    txt = json.dumps(d, indent=2, ensure_ascii=False)
    print(txt)
    if args.out_json:
        Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_json).write_text(txt, encoding="utf-8")
        print(f"[third-digit-tune-validate] wrote {args.out_json}", flush=True)
    return 0

def _cmd_survival_validate(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path

    bars_1m = _load_or_synthesize_1m(args)
    bars_1s = _load_1s(args)
    samples = build_windows(bars_1m, bars_1s=bars_1s, obs_step_sec=args.obs_step_sec)
    if not samples:
        print("[survival-validate] no 111/000 windows", file=sys.stderr)
        return 2
    if args.calendar_split:
        from .model.survival_survival_validation import run_survival_calendar_48_30_trigger_report

        need_ms = int(args.span_days + args.holdout_days) * 86_400_000
        span_real = int(samples[-1].window_start_ms) - int(samples[0].window_start_ms)
        if span_real < need_ms - 86_400_000:
            print(
                f"[survival-validate] 数据时间跨度约 {span_real/86_400_000:.1f} 天, "
                f"需要约 >= {args.span_days + args.holdout_days} 天才能填满 calendar 切分",
                file=sys.stderr,
            )
        if getattr(args, "reversal_metrics", False):
            from .model.survival_survival_validation import run_reversal_calendar_48_30_trigger_report

            payload = run_reversal_calendar_48_30_trigger_report(
                samples,
                span_days=int(args.span_days),
                holdout_days=int(args.holdout_days),
                train_frac_in_span=float(args.train_frac_in_span),
                label_mode=args.label_mode,
            )
        else:
            payload = run_survival_calendar_48_30_trigger_report(
                samples,
                span_days=int(args.span_days),
                holdout_days=int(args.holdout_days),
                train_frac_in_span=float(args.train_frac_in_span),
                label_mode=args.label_mode,
            )
    else:
        event_table = windows_to_event_table(samples, label_mode=args.label_mode)
        train_tbl, valid_tbl, _test_tbl = time_split_three(
            event_table, train_ratio=args.train_ratio, valid_ratio=args.valid_ratio
        )
        if train_tbl.empty or valid_tbl.empty:
            print("[survival-validate] train or valid empty after split", file=sys.stderr)
            return 2
        from .model.survival_survival_validation import run_survival_validation_report

        report = run_survival_validation_report(train_tbl, valid_tbl)
        payload = report.as_dict()
    if args.out_json:
        Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out_json, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
        print(f"[survival-validate] wrote {args.out_json}", flush=True)
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    return 0

def _cmd_pipeline(args: argparse.Namespace) -> int:
    bars_1m = _load_or_synthesize_1m(args)
    bars_1s = _load_1s(args)

    samples = build_windows(bars_1m, bars_1s=bars_1s, obs_step_sec=args.obs_step_sec)
    if not samples:
        raise SystemExit("no 111/000 windows in dataset")

    event_table = windows_to_event_table(samples, label_mode=args.label_mode)
    train_tbl, valid_tbl, test_tbl = time_split_three(
        event_table, train_ratio=args.train_ratio, valid_ratio=args.valid_ratio
    )
    train_ids = sorted(train_tbl["window_id"].unique().tolist())
    valid_ids = sorted(valid_tbl["window_id"].unique().tolist())
    test_ids = sorted(test_tbl["window_id"].unique().tolist())
    test_samples = [samples[i] for i in test_ids]

    fit = fit_survival_mle(train_tbl)

    cv_results: list[dict] = []
    if args.n_cv_folds >= 2 and len(train_ids) >= args.n_cv_folds * 4:
        for fr in walk_forward_cv(train_tbl, n_folds=args.n_cv_folds):
            cv_results.append(
                {
                    "fold": fr.fold_index,
                    "train_n": fr.train_n,
                    "valid_n": fr.valid_n,
                    "params": fr.params.to_dict(),
                    "ll_train": fr.log_likelihood_train,
                    "ll_valid": fr.log_likelihood_valid,
                }
            )

    ci = None
    if args.n_bootstrap > 0:
        try:
            br = block_bootstrap(
                train_tbl,
                n_iter=args.n_bootstrap,
                block_size=args.bootstrap_block_size,
                seed=args.seed,
            )
            ci = {
                "lambda0": list(br.lambda0_ci),
                "gamma": list(br.gamma_ci),
                "beta": list(br.beta_ci),
            }
        except Exception as e:
            print(f"bootstrap skipped: {e}", file=sys.stderr)

    calib = calibrate_threshold(
        valid_tbl, fit.params, n_bins=10, significance_margin=args.significance_margin
    )

    thresholds = SignalThresholds(
        trend_reversal_prob_max=args.trend_prob_max,
        trend_signal_stability_sec_min=args.trend_stability_sec,
        reversal_baseline_offset_min=args.reversal_baseline_offset,
        reversal_signal_stability_sec_min=args.reversal_stability_sec,
        reversal_prob_min=calib.threshold,
        reversal_edge_vs_baseline_min=args.reversal_edge_min,
        trend_min_expected_value=args.trend_min_ev,
    )

    artifact = StrategyArtifact(
        params=fit.params,
        thresholds=thresholds,
        baseline_prob=calib.baseline_prob,
        data_meta={
            "n_total_windows": len(samples),
            "n_train_windows": len(train_ids),
            "n_valid_windows": len(valid_ids),
            "n_test_windows": len(test_ids),
            "obs_step_sec": args.obs_step_sec,
            "label_mode": args.label_mode,
            "observed_from": samples[0].observed_from if samples else "unknown",
        },
        bin_table=calib.bin_table.to_dict(orient="records"),
        ci=ci,
    )
    save_artifact(args.out_artifact, artifact)

    bt_cfg = BacktestConfig(
        params=fit.params,
        thresholds=thresholds,
        baseline_reversal_prob=calib.baseline_prob,
        poly_cfg=PolymarketCfg(),
        risk_cfg=RiskCfg(),
        trend_rising_tolerance=args.trend_rising_tol,
        reversal_falling_tolerance=args.reversal_falling_tol,
        reversal_falling_drawdown_tol=args.reversal_drawdown_tol,
    )
    report = run_backtest(test_samples, bt_cfg)
    stats = summarize(report)

    out: dict[str, Any] = {
        "artifact": args.out_artifact,
        "data_meta": artifact.data_meta,
        "fit": {
            "params": fit.params.to_dict(),
            "log_likelihood": fit.log_likelihood,
            "success": fit.success,
            "message": fit.message,
        },
        "ci": ci,
        "cv": cv_results,
        "calibration": {
            "baseline_prob": calib.baseline_prob,
            "reversal_prob_threshold": calib.threshold,
            "bin_table": calib.bin_table.to_dict(orient="records"),
        },
        "backtest": {
            "n_trades": report.n_trades,
            "win_rate": report.win_rate,
            "total_pnl": report.total_pnl,
            "final_equity": report.final_equity,
            "by_engine": {k: v.__dict__ for k, v in stats.items()},
        },
    }
    Path(args.out_report).write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0

def _cmd_signal(args: argparse.Namespace) -> int:
    art = load_artifact(args.artifact)
    from .signals import (
        TrendStabilityTracker,
        TrendEVPeakTracker,
        decide_signal,
        expected_value_reversal,
        expected_value_trend,
    )
    from .survival import reversal_probability
    from .backtest.polymarket import quote_for_reversal, quote_for_trend, PolymarketCfg

    d_signed = (args.current_price - args.baseline) / args.baseline * 100.0
    d_abs = abs(d_signed)
    p_rev = reversal_probability(d_abs, float(args.seconds_left), art.params)

    poly = PolymarketCfg()
    rq = quote_for_reversal(trigger=args.trigger, d_signed_pct=d_signed, seconds_left=args.seconds_left, cfg=poly)
    tq = quote_for_trend(trigger=args.trigger, d_signed_pct=d_signed, seconds_left=args.seconds_left, cfg=poly)

    ev_rev = expected_value_reversal(p_rev, rq.payoff_gross)
    ev_trend = expected_value_trend(p_rev, tq.payoff_gross)

    sig = decide_signal(
        reversal_probability=p_rev,
        reversal_baseline_probability=art.baseline_prob,
        signal_stability_sec=art.thresholds.trend_signal_stability_sec_min,
        ev_trend_pullback=True,
        ev_trend=ev_trend,
        thresholds=art.thresholds,
    )
    out = {
        "signal": sig.value,
        "p_reversal": p_rev,
        "baseline_prob": art.baseline_prob,
        "reversal_prob_threshold": art.thresholds.reversal_prob_min,
        "ev_reversal": ev_rev,
        "ev_trend": ev_trend,
        "reversal_quote": {
            "side": rq.side,
            "effective_price": rq.effective_price,
            "payoff_gross": rq.payoff_gross,
            "slippage_bps": rq.slippage_bps,
            "rebound": rq.rebound,
            "oracle_drag": rq.oracle_drag,
        },
        "trend_quote": {
            "side": tq.side,
            "effective_price": tq.effective_price,
            "payoff_gross": tq.payoff_gross,
        },
    }
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0

def _cmd_growth_path(args: argparse.Namespace) -> int:
    from .analysis.growth_path import run_growth_path
    from .data.window import build_windows
    from .model.persist import load_artifact

    bars_1m = pd.read_parquet(args.bars1m)
    bars_1s = pd.read_parquet(args.bars1s) if args.bars1s else None
    artifact = load_artifact(args.artifact)
    samples = build_windows(bars_1m, bars_1s=bars_1s, obs_step_sec=args.obs_step_sec)
    if not samples:
        raise SystemExit("no windows extracted")

    if args.use_test_only:
        n = len(samples)
        samples = samples[int(0.8 * n):]

    eqs = [float(x) for x in args.equities.split(",") if x.strip()]
    strats = [s.strip() for s in args.strategies.split(",") if s.strip()]

    results = run_growth_path(
        samples, artifact,
        initial_equities=eqs,
        strategies=strats,
    )

    out_records = []
    for r in results:
        out_records.append({
            "strategy": r.strategy,
            "initial_equity": r.initial_equity,
            "final_equity": r.final_equity,
            "growth_multiple": r.growth_multiple,
            "n_trades": r.n_trades,
            "win_rate": r.win_rate,
            "bust": r.bust,
            "bust_at_window": r.bust_at_window,
            "windows_to_500": r.windows_to_500,
            "windows_to_10k": r.windows_to_10k,
            "regime_history": r.regime_history[-5:] if r.regime_history else [],
        })

    Path(args.out).write_text(
        json.dumps({
            "n_samples": len(samples),
            "results": out_records,
        }, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8"
    )

    df = pd.DataFrame(out_records)
    print(df[[
        "strategy", "initial_equity", "final_equity", "growth_multiple",
        "n_trades", "win_rate", "bust", "windows_to_500", "windows_to_10k"
    ]].to_string(index=False))
    return 0

def _cmd_v4_vs_v5(args: argparse.Namespace) -> int:
    from .analysis.v4_vs_v5_diagnostics import diagnose_v4_vs_v5
    from .data.window import build_windows
    from .model.persist import load_artifact

    bars_1m = pd.read_parquet(args.bars1m)
    bars_1s = pd.read_parquet(args.bars1s) if args.bars1s else None
    artifact = load_artifact(args.artifact)
    samples = build_windows(bars_1m, bars_1s=bars_1s, obs_step_sec=args.obs_step_sec)
    if not samples:
        raise SystemExit("no windows extracted")

    n = len(samples)
    test_samples = samples[int(0.8 * n):]
    if not test_samples:
        test_samples = samples

    v4_th = SignalThresholds(
        trend_reversal_prob_max=0.15,
        trend_signal_stability_sec_min=args.v4_stab,
        reversal_signal_stability_sec_min=4,
        reversal_baseline_offset_min=0.0,
        trend_min_expected_value=0.0,
    )
    v5_th = SignalThresholds(
        trend_reversal_prob_max=0.15,
        trend_signal_stability_sec_min=args.v5_stab,
        reversal_signal_stability_sec_min=3,
        reversal_baseline_offset_min=0.0,
        trend_min_expected_value=0.0,
    )

    rep = diagnose_v4_vs_v5(
        test_samples,
        artifact.params,
        baseline_reversal=artifact.baseline_prob,
        v4_thresholds=v4_th,
        v5_thresholds=v5_th,
        v4_pullback_ratio=args.v4_pullback,
        v5_rising_tolerance=args.v5_rising_tol,
    )

    out = {
        "n_samples": rep.n_samples,
        "n_obs_total": rep.n_obs_total,
        "v4_pass_rates": rep.v4_pass_rates,
        "v4_marginal_kill": rep.v4_marginal_kill,
        "v5_pass_rates": rep.v5_pass_rates,
        "set_analysis": rep.set_analysis,
        "rejected_by_C_features": rep.rejected_by_C_features,
        "win_rate_attribution": rep.win_rate_attribution,
        "overall_pass_rate_compare": rep.overall_pass_rate_compare,
        "p_rev_at_entry_distribution": rep.p_rev_at_entry_distribution,
    }
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0

def _cmd_sweep(args: argparse.Namespace) -> int:
    from .data.window import build_windows
    from .model.persist import load_artifact
    from .sweep import cells_to_dataframe, run_sweep

    bars_1m = pd.read_parquet(args.bars1m)
    bars_1s = pd.read_parquet(args.bars1s) if args.bars1s else None
    artifact = load_artifact(args.artifact)
    samples = build_windows(bars_1m, bars_1s=bars_1s, obs_step_sec=args.obs_step_sec)
    if not samples:
        raise SystemExit("no windows extracted")

    n = len(samples)
    test_samples = samples[int(0.8 * n):]
    if not test_samples:
        test_samples = samples

    trend_stab = [int(x) for x in args.trend_stab.split(",") if x]
    rev_stab = [int(x) for x in args.rev_stab.split(",") if x]
    rev_off = [float(x) for x in args.rev_offset.split(",") if x]

    cells = run_sweep(
        test_samples,
        artifact,
        trend_stab_grid=trend_stab,
        rev_stab_grid=rev_stab,
        rev_offset_grid=rev_off,
        trend_prob_max=args.trend_prob_max,
        use_shadow_book=bool(args.use_shadow_book),
        shadow_book_path=args.shadow_book_path,
        shadow_book_mode=args.shadow_book_mode,
        enable_risk_guard=not bool(args.no_risk_guard),
        obs_step_sec=float(args.obs_step_sec),
        friction_mode=str(args.friction_mode),
        taker_slippage_bps=float(args.taker_slippage_bps),
        independent_engine_drawdown=bool(args.independent_engine_dd),
    )
    df = cells_to_dataframe(cells)
    df_sorted = df.sort_values("total_pnl", ascending=False).reset_index(drop=True)

    out = {
        "n_test_samples": len(test_samples),
        "trend_stab_grid": trend_stab,
        "rev_stab_grid": rev_stab,
        "rev_offset_grid": rev_off,
        "trend_prob_max": args.trend_prob_max,
        "cells_sorted_by_pnl": df_sorted.to_dict(orient="records"),
        "diagnostic": {
            "risk_guard_enabled": not bool(args.no_risk_guard),
            "friction_mode": str(args.friction_mode),
            "taker_slippage_bps": float(args.taker_slippage_bps),
            "independent_engine_dd": bool(args.independent_engine_dd),
        },
    }
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(df_sorted.to_string(index=False))
    return 0

def _cmd_book_compare(args: argparse.Namespace) -> int:
    from .data.window import build_windows
    from .model.persist import load_artifact
    from .signals import SignalThresholds
    from .backtest.engine import BacktestConfig, run_backtest
    from .backtest.metrics import summarize
    from .backtest.polymarket import PolymarketCfg
    from .risk.limits import RiskCfg
    from .backtest.shadow_book_overlay import ShadowBookOverlay

    bars_1m = pd.read_parquet(args.bars1m)
    bars_1s = pd.read_parquet(args.bars1s) if args.bars1s else None
    artifact = load_artifact(args.artifact)
    samples = build_windows(bars_1m, bars_1s=bars_1s, obs_step_sec=args.obs_step_sec)
    if not samples:
        raise SystemExit("no windows extracted")
    n = len(samples)
    test_samples = samples[int(0.8 * n):] or samples

    th = SignalThresholds(
        trend_reversal_prob_max=float(args.trend_prob_max),
        trend_signal_stability_sec_min=int(args.trend_stability_sec),
        reversal_baseline_offset_min=float(args.reversal_baseline_offset),
        reversal_signal_stability_sec_min=int(args.reversal_stability_sec),
    )
    base_cfg = BacktestConfig(
        params=artifact.params,
        thresholds=th,
        baseline_reversal_prob=artifact.baseline_prob,
        poly_cfg=PolymarketCfg(
            friction_mode=str(args.friction_mode),
            taker_slippage_bps=float(args.taker_slippage_bps),
        ),
        risk_cfg=RiskCfg(),
        obs_step_sec=float(args.obs_step_sec),
        enable_risk_guard=not bool(args.no_risk_guard),
        independent_engine_drawdown=bool(args.independent_engine_dd),
    )
    rep_model = run_backtest(test_samples, base_cfg)
    stat_model = summarize(rep_model)

    overlay = ShadowBookOverlay.from_jsonl(
        args.shadow_book_path,
        mode=args.shadow_book_mode,
    )
    book_cfg = BacktestConfig(
        params=artifact.params,
        thresholds=th,
        baseline_reversal_prob=artifact.baseline_prob,
        poly_cfg=PolymarketCfg(
            friction_mode=str(args.friction_mode),
            taker_slippage_bps=float(args.taker_slippage_bps),
        ),
        risk_cfg=RiskCfg(),
        shadow_book_overlay=overlay,
        obs_step_sec=float(args.obs_step_sec),
        enable_risk_guard=not bool(args.no_risk_guard),
        independent_engine_drawdown=bool(args.independent_engine_dd),
    )
    rep_book = run_backtest(test_samples, book_cfg)
    stat_book = summarize(rep_book)

    out = {
        "config": {
            "obs_step_sec": int(args.obs_step_sec),
            "trend_stability_sec": int(args.trend_stability_sec),
            "reversal_stability_sec": int(args.reversal_stability_sec),
            "trend_prob_max": float(args.trend_prob_max),
            "reversal_baseline_offset": float(args.reversal_baseline_offset),
            "shadow_book_path": args.shadow_book_path,
            "shadow_book_mode": args.shadow_book_mode,
            "risk_guard_enabled": not bool(args.no_risk_guard),
            "friction_mode": str(args.friction_mode),
            "taker_slippage_bps": float(args.taker_slippage_bps),
            "independent_engine_dd": bool(args.independent_engine_dd),
        },
        "model_only": {
            "n_trades": rep_model.n_trades,
            "total_pnl": rep_model.total_pnl,
            "by_engine": {k: v.__dict__ for k, v in stat_model.items()},
        },
        "with_shadow_book": {
            "n_trades": rep_book.n_trades,
            "total_pnl": rep_book.total_pnl,
            "by_engine": {k: v.__dict__ for k, v in stat_book.items()},
            "overlay": {
                "total_signals": rep_book.overlay_total_signals,
                "matched_signals": rep_book.overlay_matched_signals,
                "executable_signals": rep_book.overlay_executable_signals,
                "executable_ratio_on_matched": (
                    rep_book.overlay_executable_signals / rep_book.overlay_matched_signals
                    if rep_book.overlay_matched_signals > 0 else None
                ),
                "reject_reason_counts": rep_book.overlay_reject_reasons,
            },
        },
        "delta": {
            "n_trades": rep_book.n_trades - rep_model.n_trades,
            "total_pnl": rep_book.total_pnl - rep_model.total_pnl,
        },
    }
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0

def _cmd_trend_diagnose(args: argparse.Namespace) -> int:
    from .analysis.trend_diagnostics import diagnose_trend_filters
    from .data.window import build_windows
    from .model.persist import load_artifact
    from .signals import SignalThresholds

    bars_1m = pd.read_parquet(args.bars1m)
    bars_1s = pd.read_parquet(args.bars1s) if args.bars1s else None
    artifact = load_artifact(args.artifact)

    samples = build_windows(bars_1m, bars_1s=bars_1s, obs_step_sec=args.obs_step_sec)
    if not samples:
        raise SystemExit("no windows extracted")

    th = SignalThresholds(
        reversal_prob_min=artifact.thresholds.reversal_prob_min,
        reversal_edge_vs_baseline_min=artifact.thresholds.reversal_edge_vs_baseline_min,
        trend_reversal_prob_max=args.trend_prob_max,
        trend_signal_stability_sec_min=args.trend_stability_sec,
        trend_min_expected_value=args.trend_min_ev,
    )

    rep = diagnose_trend_filters(
        samples,
        artifact.params,
        th,
        baseline_reversal=artifact.baseline_prob,
        pullback_ratio=args.pullback_ratio,
    )

    out = {
        "config": {
            "trend_prob_max": args.trend_prob_max,
            "trend_stability_sec": args.trend_stability_sec,
            "trend_min_ev": args.trend_min_ev,
            "pullback_ratio": args.pullback_ratio,
            "baseline_reversal": artifact.baseline_prob,
        },
        "n_samples": rep.n_samples,
        "n_obs_total": rep.n_obs_total,
        "pass_rate": {
            "A_only": rep.pass_A_only,
            "AB": rep.pass_AB_only,
            "ABC": rep.pass_ABC_only,
            "ABCD_final": rep.pass_ABCD,
        },
        "marginal_pass": {
            "A__P_rev_below_max": rep.pass_A,
            "B_given_A__stability_reached": rep.pass_B_given_A,
            "C_given_AB__pullback": rep.pass_C_given_AB,
            "D_given_ABC__ev_above_min": rep.pass_D_given_ABC,
        },
        "p_rev_distribution": rep.p_rev_distribution,
        "ev_trend_distribution": rep.ev_trend_distribution,
        "stability_max_per_window_distribution": rep.stability_max_distribution,
        "suggestions": rep.suggestions,
    }
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0

def _cmd_audit(args: argparse.Namespace) -> int:
    from .analysis.sequence_distribution import analyze_sequence_distribution
    from .analysis.golden_window import evaluate_golden_window
    from .analysis.sampling_bias import evaluate_sampling_bias
    from .analysis.regime_stability import evaluate_monthly
    from .analysis.capacity import estimate_capacity
    from .data.window import build_windows, windows_to_event_table
    from .model.persist import load_artifact

    bars_1m = pd.read_parquet(args.bars1m)
    bars_1s = pd.read_parquet(args.bars1s) if args.bars1s else None
    artifact = load_artifact(args.artifact)

    seq_rep = analyze_sequence_distribution(bars_1m)
    samples = build_windows(bars_1m, bars_1s=bars_1s, obs_step_sec=args.obs_step_sec)
    if not samples:
        raise SystemExit("no windows extracted")
    et = windows_to_event_table(samples)

    gw_rep = evaluate_golden_window(et, artifact.params, n_bins=10)
    sb_rep = evaluate_sampling_bias(samples, artifact.params)
    monthly = evaluate_monthly(samples, min_samples_per_month=args.monthly_min_samples)
    cap = estimate_capacity(samples)

    out = {
        "Q1_sequence_distribution": {
            "n_windows": seq_rep.n_windows,
            "binomial_baseline_freq": seq_rep.binomial_baseline_freq,
            "target_111_000_share": seq_rep.target_111_000_share,
            "trigger_freq": seq_rep.trigger_freq,
            "full_seq_freq": seq_rep.full_seq_freq,
            "chi2_full_pvalue": seq_rep.chi2_full_pvalue,
            "arcsine_table": seq_rep.arcsine_table,
        },
        "Q2_golden_window": {
            "n_windows": gw_rep.n_windows,
            "baseline_prob": gw_rep.baseline_prob,
            "brier_score": gw_rep.brier_score,
            "bin_table": gw_rep.bin_table,
            "reliability_table": gw_rep.reliability_table,
            "golden_lift_top1": gw_rep.golden_lift_top1,
        },
        "Q3_3_sampling_bias": {
            "n_windows": sb_rep.n_windows,
            "actual_reversal_rate": sb_rep.actual_reversal_rate,
            "fixed_t3": {
                "mean_d": sb_rep.fixed_t3_mean_d,
                "pred_prob_mean": sb_rep.fixed_t3_pred_prob_mean,
            },
            "biased_remaining": {
                "mean_d": sb_rep.biased_mean_d,
                "pred_prob_mean": sb_rep.biased_pred_prob_mean,
            },
            "abs_bias_in_estimated_prob": sb_rep.abs_bias_in_estimated_prob,
        },
        "Q4_regime_stability": {
            "monthly": monthly.rows,
        },
        "Q5_capacity": {
            "daily_avg_windows": cap.daily_avg_windows,
            "daily_top_decile_windows": cap.daily_top_decile_windows,
            "assumptions": {
                "poly_depth_alpha": cap.poly_depth_alpha,
                "assumed_payoff": cap.assumed_payoff,
                "assumed_win_rate": cap.assumed_win_rate,
            },
            "by_equity": cap.rows,
        },
    }
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0

def _cmd_connectivity(args: argparse.Namespace) -> int:
    from .io import (
        BinanceFeed, BinanceFeedCfg,
        ClockSync, ClockSyncCfg,
        MarketResolver, MarketResolverCfg,
        PolymarketClient, POLYMARKET_PLATFORM,
        load_polymarket_runtime_cfg, setup_logging,
    )
    import time as _t

    setup_logging(log_dir="logs", level="INFO")
    runtime = load_polymarket_runtime_cfg(env_file=args.env_file)

    report: dict[str, Any] = {"env_file": args.env_file, "checks": {}}

    def _step(name: str) -> None:
        print(f"[connectivity] -> {name} ...", file=sys.stderr, flush=True)

    _step("Binance time sync")
    clock = ClockSync(cfg=ClockSyncCfg())
    ok_clock = clock.sync_once()
    report["checks"]["binance_time_sync"] = {"ok": ok_clock, **clock.snapshot()}

    _step("Binance REST kline (BTCUSDT 1s)")
    feed = BinanceFeed(cfg=BinanceFeedCfg(
        symbol="BTCUSDT", interval="1s",
        user_agent=runtime.http_user_agent,
        request_timeout_sec=runtime.http_timeout_sec,
        enable_ws=False,
        rest_poll_interval_sec=0.5,
    ))
    feed.start()
    deadline = _t.time() + 6.0
    while _t.time() < deadline:
        snap_now = feed.snapshot()
        if snap_now.get("rest_poll_count", 0) > 0:
            break
        _t.sleep(0.3)
    rest_snapshot = feed.snapshot()
    feed.stop()
    report["checks"]["binance_rest_kline"] = {
        "ok": rest_snapshot.get("rest_poll_count", 0) > 0,
        **rest_snapshot,
    }

    _step("Polymarket Gamma resolver (5min BTC market)")
    resolver = MarketResolver(cfg=MarketResolverCfg(
        url=runtime.market_resolver_url_template,
        keywords=tuple(runtime.market_search_keywords),
        horizon_minutes=runtime.market_horizon_minutes,
        request_timeout_sec=runtime.http_timeout_sec,
        user_agent=runtime.http_user_agent,
    ))
    ok_resolver = resolver.refresh_once()
    report["checks"]["polymarket_gamma_resolver"] = {
        "ok": ok_resolver,
        **resolver.snapshot(),
    }

    _step("Polymarket CLOB book (REST)")
    poly = PolymarketClient(runtime_cfg=runtime, platform=POLYMARKET_PLATFORM)
    redeem_ok, redeem_reason = poly.redeem_capability_check()
    report["checks"]["polymarket_auto_redeem_capability"] = {
        "ok": redeem_ok,
        "reason": redeem_reason,
        "signature_type": runtime.signature_type,
    }
    active = resolver.get_active()
    if active is not None:
        book = poly.fetch_book(active.token_id_yes)
        report["checks"]["polymarket_clob_book"] = {
            "ok": book.get("error") is None,
            "best_bid": book.get("best_bid"),
            "best_ask": book.get("best_ask"),
            "stale": book.get("stale"),
            "source": book.get("source"),
            "error": book.get("error"),
        }

        _step("Polymarket WebSocket market feed")
        from .io import PolymarketFeed, PolymarketFeedCfg
        pfeed = PolymarketFeed(cfg=PolymarketFeedCfg(user_agent=runtime.http_user_agent))
        pfeed.start([active.token_id_yes, active.token_id_no])
        deadline = _t.time() + 6.0
        while _t.time() < deadline:
            snap_p = pfeed.snapshot()
            if snap_p.get("ws_alive") and snap_p.get("cached_book_count", 0) > 0:
                break
            _t.sleep(0.3)
        ws_snap = pfeed.snapshot()
        pfeed.stop()
        report["checks"]["polymarket_ws_feed"] = {
            "ok": bool(ws_snap.get("ws_alive")),
            "ws_alive": ws_snap.get("ws_alive"),
            "subscribed_count": ws_snap.get("subscribed_count"),
            "cached_book_count": ws_snap.get("cached_book_count"),
            "ws_error_count": ws_snap.get("ws_error_count"),
            "ws_last_error": ws_snap.get("ws_last_error"),
        }
    else:
        report["checks"]["polymarket_clob_book"] = {
            "ok": True,
            "skipped": True,
            "reason": "no_active_market right now (Gamma OK; will rotate at next 5-min cycle)",
        }
        report["checks"]["polymarket_ws_feed"] = {
            "ok": True,
            "skipped": True,
            "reason": "no_active_market; cannot subscribe yet",
        }

    proxy = (runtime.proxy_address or "").strip()
    placeholder = (not proxy) or proxy.upper().endswith("YOUR_PROXY_ADDRESS") or len(proxy) != 42
    if not placeholder:
        _step("Polygon RPC USDC balance")
        equity = poly.fetch_account_equity_usdc()
        report["checks"]["polygon_usdc_balance"] = {
            "ok": equity is not None,
            "equity_usdc": equity,
            "rpc": runtime.polygon_rpc_url,
        }
        if args.check_allowance:
            _step("Polygon RPC USDC allowance")
            ok_alw, msg, alw = poly.precheck_allowance()
            report["checks"]["polygon_usdc_allowance"] = {
                "ok": ok_alw,
                "allowance_usdc": alw,
                "message": msg,
            }
    else:
        report["checks"]["polygon_usdc_balance"] = {
            "ok": True,
            "skipped": True,
            "reason": "no_proxy_address (placeholder or empty); skipping on-chain query",
        }

    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")

    bad = [k for k, v in report["checks"].items() if isinstance(v, dict) and not v.get("ok")]
    if bad:
        print(f"\n[connectivity] FAIL on: {bad}", file=sys.stderr)
        return 2
    print("\n[connectivity] all checks passed.", file=sys.stderr)
    return 0

def _cmd_shadow_window_stats(args: argparse.Namespace) -> int:
    from .analysis.shadow_window_stats import build_shadow_window_report

    rep = build_shadow_window_report(args.state_db)
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    return 0

def _cmd_p_rev_calibrate(args: argparse.Namespace) -> int:
    from .analysis.p_rev_time_calibration import build_p_rev_time_calibration

    rep = build_p_rev_time_calibration(
        shadow_signals_path=args.shadow_signals,
        out_path=args.out,
        window_hours=float(args.window_hours),
        bucket_sec=max(5, int(args.bucket_sec)),
        min_samples=max(3, int(args.min_samples)),
        max_rows=max(100, int(args.max_rows)),
    )
    print(json.dumps(rep, indent=2, ensure_ascii=False, default=str))
    return 0

def _cmd_snapshot_config(args: argparse.Namespace) -> int:
    from .io.friction_snapshot import build_snapshot_proposal

    rep = build_snapshot_proposal(
        state_db_path=args.state_db,
        shadow_signals_path=args.shadow_signals,
        snapshot_path=args.snapshot,
        proposal_path=args.proposal_out,
        window_days=int(args.window_days),
        max_change_ratio=float(args.max_change_ratio),
        min_update_interval_days=int(args.min_update_interval_days),
    )
    print(json.dumps(rep, indent=2, ensure_ascii=False, default=str))
    return 0

def _cmd_apply_snapshot(args: argparse.Namespace) -> int:
    from .io.friction_snapshot import apply_snapshot_proposal

    out = apply_snapshot_proposal(
        proposal_path=args.proposal,
        snapshot_path=args.snapshot,
        state_db_path=args.state_db,
        operator=args.operator,
        force_manual=bool(args.force_manual),
    )
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0

def _cmd_shadow_report(args: argparse.Namespace) -> int:
    from .analysis.shadow_run_report import build_report, report_to_markdown

    rep = build_report(
        alerts_path=args.alerts,
        state_db_path=args.state_db,
        window_hours=float(args.window_hours),
        baseline_trend_per_day=float(args.baseline_trend_per_day),
        baseline_reversal_per_day=float(args.baseline_reversal_per_day),
        deviation_warn=float(args.deviation_warn),
    )
    text = json.dumps(rep, indent=2, ensure_ascii=False, default=str)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    if args.markdown:
        Path(args.markdown).parent.mkdir(parents=True, exist_ok=True)
        Path(args.markdown).write_text(report_to_markdown(rep), encoding="utf-8")
    h = rep.get("4_health_score", {})
    if h.get("suspend_live_plan"):
        return 2
    return 0

def _cmd_smoke_real_order(args: argparse.Namespace) -> int:
    """单笔最小合规限价买单：用于 VPS 实盘链路验证（真实扣款）。"""
    import time as _t

    from .io import (
        MarketResolver,
        MarketResolverCfg,
        PolymarketClient,
        POLYMARKET_PLATFORM,
    )
    from .io.config import is_real_order_allowed, load_polymarket_runtime_cfg
    from .io.polymarket_client import OrderState
    from .io.logging_setup import setup_logging
    from .io.state_store import StateStore, KEY_LAST_EQUITY

    if not bool(args.yes_real_money):
        print(
            "[smoke-real-order] 拒绝执行：请追加 --yes-real-money 以确认真实下单",
            file=sys.stderr,
        )
        return 2

    setup_logging(log_dir="logs", level="INFO")
    runtime_base = load_polymarket_runtime_cfg(env_file=args.env_file)
    ok_allow, reason_allow = is_real_order_allowed(runtime_base)
    if not ok_allow:
        print(f"[smoke-real-order] .env 不允许实盘: {reason_allow}", file=sys.stderr)
        return 1

    max_wait = max(15.0, float(args.max_wait_sec))
    runtime = replace(
        runtime_base,
        order_poll_max_wait_sec=max(
            float(runtime_base.order_poll_max_wait_sec),
            max_wait + 30.0,
        ),
    )

    poly = PolymarketClient(runtime_cfg=runtime, platform=POLYMARKET_PLATFORM)
    ok_init, msg_init = poly.init_real_client()
    if not ok_init or poly._real_client is None:
        print(f"[smoke-real-order] CLOB 初始化失败: {msg_init}", file=sys.stderr)
        return 1

    resolver = MarketResolver(
        cfg=MarketResolverCfg(
            url=runtime.market_resolver_url_template,
            keywords=tuple(runtime.market_search_keywords),
            horizon_minutes=int(runtime.market_horizon_minutes),
            refresh_interval_sec=float(runtime.market_refresh_interval_sec),
            request_timeout_sec=float(runtime.http_timeout_sec),
            user_agent=runtime.http_user_agent,
        )
    )
    if not resolver.refresh_once():
        print("[smoke-real-order] Gamma 市场解析失败", file=sys.stderr)
        return 1
    active = resolver.get_active()
    if active is None:
        print("[smoke-real-order] 当前无活动中的 5min BTC 合约，请稍后重试", file=sys.stderr)
        return 1

    side_l = str(args.side).lower()
    token_id = active.token_id_yes if side_l == "up" else active.token_id_no
    book = poly.fetch_book(token_id)
    best_ask = book.get("best_ask")
    if best_ask is None:
        print(f"[smoke-real-order] 无卖一价，跳过下单 book={book}", file=sys.stderr)
        return 1

    plat = POLYMARKET_PLATFORM
    min_shares = float(plat.min_limit_order_shares)
    price = float(best_ask)
    size_quote = round(min_shares * price + 0.01, 2)
    if size_quote < float(plat.min_order_quote_usdc):
        size_quote = float(plat.min_order_quote_usdc)

    eq_before = poly.fetch_account_equity_usdc()
    window_id = f"w{max(0, int(active.end_ts_ms) - 300_000)}"

    report: dict[str, Any] = {
        "phase": "submit",
        "condition_id": active.condition_id,
        "question": active.question,
        "side": side_l,
        "token_id_tail": token_id[-12:] if len(token_id) > 12 else token_id,
        "price_limit": price,
        "size_quote_usdc": size_quote,
        "min_shares": min_shares,
        "equity_before": eq_before,
        "window_id": window_id,
    }
    print(json.dumps(report, ensure_ascii=False), flush=True)

    coid = f"smoke_manual:{int(_t.time())}"
    ticket = poly.submit_order(
        side="BUY",
        token_id=token_id,
        price=price,
        size_quote_usdc=size_quote,
        client_order_id=coid,
        size_shares=min_shares,
    )

    if ticket.state == OrderState.DRY_RUN_SHADOW:
        print(
            json.dumps({"ok": False, "error": "dry_run_shadow", "ticket": ticket.last_error}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1
    if ticket.state == OrderState.REJECTED:
        print(
            json.dumps({"ok": False, "error": "rejected", "detail": ticket.last_error}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1

    deadline = _t.time() + max_wait
    while _t.time() < deadline and not ticket.is_terminal():
        _t.sleep(2.0)
        ticket = poly.poll_order(ticket)

    report["phase"] = "after_poll"
    report["final_state"] = ticket.state.value
    report["exchange_order_id"] = ticket.exchange_order_id
    report["last_error"] = ticket.last_error

    if ticket.state == OrderState.FILLED:
        ticket = poly.refresh_order_truth(ticket)
        report["final_state"] = ticket.state.value

    elif ticket.state in (OrderState.SUBMITTED, OrderState.PARTIAL):
        ticket = poly.cancel_order(ticket, reason="smoke_max_wait")
        report["cancelled"] = True
        report["final_state"] = ticket.state.value

    eq_after = poly.fetch_account_equity_usdc()
    report["equity_after"] = eq_after

    ok_done = ticket.state == OrderState.FILLED
    report["ok"] = ok_done

    if ok_done:
        try:
            store = StateStore(db_path=str(args.state_db))
            store.append_audit(
                "order_filled",
                {
                    "window_id": window_id,
                    "side": "SMOKE_VERIFY",
                    "direction": side_l,
                    "size_usdc": float(ticket.size_quote_usdc),
                    "price": float(ticket.price),
                    "exchange_order_id": ticket.exchange_order_id,
                    "client_order_id": coid,
                    "ref_limit_price": float(price),
                    "condition_id": active.condition_id,
                    "question": active.question[:120],
                },
            )
            if eq_after is not None:
                store.put(KEY_LAST_EQUITY, eq_after)
            report["audit"] = "order_filled appended"
        except Exception as e:
            report["audit_error"] = f"{type(e).__name__}:{e}"

    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if ok_done else 1

def _cmd_live_run(args: argparse.Namespace) -> int:
    from .io import LiveRunner

    if args.enable_real:
        os.environ.setdefault("ENABLE_REAL_ORDERS", "true")

    if args.enable_real and not args.artifact:
        print("[live-run] --enable-real 需要同时提供 --artifact <path>，否则新模型不会启动", file=sys.stderr)
        return 2
    if args.dry_run_signals and not args.artifact:
        print("[live-run] --dry-run-signals 需要同时提供 --artifact <path>", file=sys.stderr)
        return 2
    if args.record_shadow_signals and not args.artifact:
        print("[live-run] --record-shadow-signals 需要同时提供 --artifact <path>", file=sys.stderr)
        return 2
    if args.dry_run_signals and args.record_shadow_signals:
        print("[live-run] --dry-run-signals 已含信号记录, 勿再指定 --record-shadow-signals", file=sys.stderr)
        return 2
    if args.dry_run_signals and args.enable_real:
        print("[live-run] --dry-run-signals 与 --enable-real 互斥, 请二选一", file=sys.stderr)
        return 2

    runner = LiveRunner.from_env(
        env_file=args.env_file,
        dry_run_signals=bool(args.dry_run_signals),
        record_shadow_signals=bool(args.record_shadow_signals),
        artifact_path=(args.artifact or None),
        shadow_signal_log_path=args.shadow_signal_log,
    )
    if args.seconds and args.seconds > 0:
        import threading as _th
        ok = runner.start(run_forever=False)
        if not ok:
            return 2
        _th.Timer(float(args.seconds), runner.stop).start()
        try:
            while not runner._stopping.is_set():
                import time as _t
                _t.sleep(0.5)
        except KeyboardInterrupt:
            runner.stop()
        return 0

    ok = runner.start(run_forever=True)
    return 0 if ok else 2

def _cmd_webui(args: argparse.Namespace) -> int:
    from .io.webui_server import run_webui
    if not args.password or len(args.password) < 6:
        print("[webui] --password is required and must be >= 6 chars", file=sys.stderr)
        return 2
    return run_webui(
        host=args.host,
        port=int(args.port),
        password=args.password,
        project_root=args.project_root,
        env_file=args.env_file,
        state_db_path=args.state_db,
        alerts_path=args.alerts,
        log_dir=args.log_dir,
        shadow_signals_path=args.shadow_signals,
        default_artifact=args.default_artifact,
    )

def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "download":
        return _cmd_download(args)
    if args.cmd == "pipeline":
        return _cmd_pipeline(args)
    if args.cmd == "signal":
        return _cmd_signal(args)
    if args.cmd == "audit":
        return _cmd_audit(args)
    if args.cmd == "trend-diagnose":
        return _cmd_trend_diagnose(args)
    if args.cmd == "sweep":
        return _cmd_sweep(args)
    if args.cmd == "book-compare":
        return _cmd_book_compare(args)
    if args.cmd == "v4-vs-v5":
        return _cmd_v4_vs_v5(args)
    if args.cmd == "growth-path":
        return _cmd_growth_path(args)
    if args.cmd == "connectivity":
        return _cmd_connectivity(args)
    if args.cmd == "smoke-real-order":
        return _cmd_smoke_real_order(args)
    if args.cmd == "live-run":
        return _cmd_live_run(args)
    if args.cmd == "shadow-report":
        return _cmd_shadow_report(args)
    if args.cmd == "shadow-window-stats":
        return _cmd_shadow_window_stats(args)
    if args.cmd == "p-rev-calibrate":
        return _cmd_p_rev_calibrate(args)
    if args.cmd == "survival-validate":
        return _cmd_survival_validate(args)
    if args.cmd == "third-digit-tune-validate":
        return _cmd_third_digit_tune_validate(args)
    if args.cmd == "naked-third-digit-live":
        return _cmd_naked_third_digit_live(args)
    if args.cmd == "sec-causal-eval":
        return _cmd_sec_causal_eval(args)
    if args.cmd == "sec-short-multi-valid":
        return _cmd_sec_short_multi_valid(args)
    if args.cmd == "cross-window-next-open":
        return _cmd_cross_window_next_open(args)
    if args.cmd == "sequence-transition":
        return _cmd_sequence_transition(args)
    if args.cmd == "fit-prefix-survival":
        return _cmd_fit_prefix_survival(args)
    if args.cmd == "webui":
        return _cmd_webui(args)
    if args.cmd == "snapshot-config":
        return _cmd_snapshot_config(args)
    if args.cmd == "apply-snapshot":
        return _cmd_apply_snapshot(args)
    return 1

if __name__ == "__main__":
    raise SystemExit(main())
