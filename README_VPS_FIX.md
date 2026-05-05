# VPS 自动领取功能修复总结

## 问题分析

你的VPS上自动领取功能提示"链上执行失败"，经过代码分析，发现以下问题：

1. **自动领取逻辑不够健壮**
   - 缺少领取能力检查
   - 检查间隔固定为60秒（应该使用配置）
   - 错误处理不够详细
   - 检查范围只有10个合约

2. **可能缺少依赖**
   - `py-builder-relayer-client` (SIGNATURE_TYPE=1时需要)
   - `web3` (链上交互需要)

## 已完成的修复

### 1. 改进自动领取功能 ✅

**文件**: `src/twinengines/live/naked_pm_runner.py`

改进内容：
- ✅ 添加 `redeem_capability_check()` 检查
- ✅ 使用配置的检查间隔（`AUTO_REDEEM_INTERVAL_SEC`，默认30秒）
- ✅ 增加详细的日志输出（logger.info/warning/exception）
- ✅ 检查范围从10个扩大到20个合约
- ✅ 更好的异常处理和错误诊断

### 2. 创建诊断工具 ✅

**文件**: `test_auto_redeem.py`

功能：
- 测试领取能力检查
- 查看可领取的持仓
- 手动触发领取操作
- 提供详细的错误诊断

### 3. 创建部署文档 ✅

- `VPS_FIX_QUICK_GUIDE.md` - 快速指南
- `VPS_AUTO_REDEEM_FIX.md` - 详细文档
- `deploy_auto_redeem.py` - 部署脚本

## 部署方法（推荐使用WinSCP）

### 方法1: 使用 WinSCP（最简单）

1. **下载并安装 WinSCP**: https://winscp.net/eng/download.php

2. **连接到VPS**:
   - 文件协议: SFTP
   - 主机名: 47.243.169.235
   - 端口: 22
   - 用户名: root
   - 密码: Jinbh1977

3. **上传文件**（直接拖拽）:
   ```
   本地文件 -> VPS路径
   src/twinengines/live/naked_pm_runner.py -> /root/TwinEngines/src/twinengines/live/
   src/twinengines/io/polymarket_client.py -> /root/TwinEngines/src/twinengines/io/
   test_auto_redeem.py -> /root/TwinEngines/
   ```

4. **在VPS上执行**（使用WinSCP内置终端或PuTTY）:
   ```bash
   cd /root/TwinEngines
   pip3 install web3
   pip3 install 'git+https://github.com/Polymarket/py-builder-relayer-client.git'
   python3 test_auto_redeem.py
   systemctl restart twinengines-strategy
   ```

### 方法2: 使用 Git（如果VPS上有代码仓库）

```bash
# 在本地提交更改
git add src/twinengines/live/naked_pm_runner.py
git add src/twinengines/io/polymarket_client.py
git add test_auto_redeem.py
git commit -m "Fix auto redeem functionality"
git push

# 在VPS上拉取
ssh root@47.243.169.235
cd /root/TwinEngines
git pull
pip3 install web3
pip3 install 'git+https://github.com/Polymarket/py-builder-relayer-client.git'
python3 test_auto_redeem.py
systemctl restart twinengines-strategy
```

## 验证步骤

### 1. 测试领取能力
```bash
cd /root/TwinEngines
python3 test_auto_redeem.py
```

**预期输出**:
```
=== 测试领取能力检查 ===
客户端初始化: 成功 - ok
领取能力检查: 通过 - ok
```

### 2. 查看日志
```bash
tail -f /root/TwinEngines/logs/twinengines.log | grep -E "auto_redeem|redeem"
```

**预期看到**:
```json
{"phase": "auto_redeem", "action": "redeem_attempted", "redeemable_count": 1, ...}
```

### 3. 监控服务状态
```bash
systemctl status twinengines-strategy
```

## 关键配置检查

确保 `/root/TwinEngines/.env` 中有：
```bash
AUTO_REDEEM_ENABLED=true
AUTO_REDEEM_INTERVAL_SEC=30
SIGNATURE_TYPE=1
POLY_BUILDER_API_KEY=019ca0ba-1881-7b79-a773-aec734fc44a6
POLY_BUILDER_SECRET=BfX2rtdxC1VvDrFNiNTYmkyS5q3SXP1b30BZMaQueJc=
POLY_BUILDER_PASSPHRASE=5cc7b41006f5ec3de3c444c5679f178fc001517d50c8c0e7fab2278f00820999
```

## 常见错误及解决方案

| 错误信息 | 原因 | 解决方案 |
|---------|------|---------|
| `relayer_dependencies_missing` | 缺少 relayer 库 | `pip3 install 'git+https://github.com/Polymarket/py-builder-relayer-client.git'` |
| `web3_unavailable` | 缺少 web3 库 | `pip3 install web3` |
| `missing_builder_credentials` | Builder API 凭据未配置 | 检查 .env 中的 POLY_BUILDER_* 配置 |
| `capability_check_failed` | 领取能力检查失败 | 运行 `python3 test_auto_redeem.py` 查看详细原因 |

## 工作原理

1. **每30秒检查一次**（可配置）
2. **扫描最近20个合约**的持仓
3. **找出 currPrice=1 的持仓**（表示已结算且赢了）
4. **调用 redeem_positions()** 领取
5. **记录详细日志**（成功/失败/原因）

## 下一步

1. ✅ 使用 WinSCP 上传文件到VPS
2. ✅ SSH 连接VPS并安装依赖
3. ✅ 运行 `python3 test_auto_redeem.py` 测试
4. ✅ 重启服务 `systemctl restart twinengines-strategy`
5. ✅ 监控日志确认自动领取工作正常

## 需要帮助？

如果遇到问题，请提供：
1. `python3 test_auto_redeem.py` 的完整输出
2. 最近的日志: `tail -100 /root/TwinEngines/logs/twinengines.log`
3. 配置检查: `cat /root/TwinEngines/.env | grep -E "AUTO_REDEEM|SIGNATURE_TYPE"`

---

**文件清单**:
- ✅ `src/twinengines/live/naked_pm_runner.py` - 改进的自动领取逻辑
- ✅ `src/twinengines/io/polymarket_client.py` - 已有的领取功能
- ✅ `test_auto_redeem.py` - 诊断工具
- ✅ `VPS_FIX_QUICK_GUIDE.md` - 快速指南
- ✅ `VPS_AUTO_REDEEM_FIX.md` - 详细文档
- ✅ `deploy_auto_redeem.py` - 部署脚本

所有文件已准备就绪，可以开始部署！
