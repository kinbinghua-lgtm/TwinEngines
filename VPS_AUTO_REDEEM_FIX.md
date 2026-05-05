# TwinEngines VPS 自动领取功能修复指南

## 问题诊断

根据分析，自动领取功能可能失败的原因：

1. **依赖缺失**: 缺少 `py-builder-relayer-client` 或 `web3` 库
2. **配置问题**: Builder API 凭据配置不正确
3. **签名类型**: SIGNATURE_TYPE=1 需要 relayer 支持
4. **网络问题**: RPC 连接超时或失败

## 修复步骤

### 1. 上传更新的代码到 VPS

使用 WinSCP 或 pscp 上传以下文件：

```
本地路径 -> VPS路径
src/twinengines/live/naked_pm_runner.py -> /root/TwinEngines/src/twinengines/live/naked_pm_runner.py
src/twinengines/io/polymarket_client.py -> /root/TwinEngines/src/twinengines/io/polymarket_client.py
test_auto_redeem.py -> /root/TwinEngines/test_auto_redeem.py
```

### 2. SSH 连接到 VPS

```bash
ssh root@47.243.169.235
# 密码: Jinbh1977
```

### 3. 安装/更新依赖

```bash
cd /root/TwinEngines

# 安装 web3
pip3 install web3

# 安装 py-builder-relayer-client
pip3 install 'git+https://github.com/Polymarket/py-builder-relayer-client.git'

# 或者使用 requirements.txt
pip3 install -r requirements.txt
```

### 4. 测试自动领取功能

```bash
cd /root/TwinEngines
python3 test_auto_redeem.py
```

预期输出：
- ✓ 客户端初始化成功
- ✓ 领取能力检查通过
- 显示可领取的持仓（如果有）

### 5. 检查配置

```bash
cd /root/TwinEngines
cat .env | grep -E "AUTO_REDEEM|SIGNATURE_TYPE|BUILDER"
```

确保以下配置正确：
```
AUTO_REDEEM_ENABLED=true
AUTO_REDEEM_INTERVAL_SEC=30
SIGNATURE_TYPE=1
POLY_BUILDER_API_KEY=019ca0ba-1881-7b79-a773-aec734fc44a6
POLY_BUILDER_SECRET=BfX2rtdxC1VvDrFNiNTYmkyS5q3SXP1b30BZMaQueJc=
POLY_BUILDER_PASSPHRASE=5cc7b41006f5ec3de3c444c5679f178fc001517d50c8c0e7fab2278f00820999
```

### 6. 重启服务

```bash
# 停止服务
systemctl stop twinengines-strategy

# 启动服务
systemctl start twinengines-strategy

# 查看状态
systemctl status twinengines-strategy

# 实时查看日志
tail -f /root/TwinEngines/logs/twinengines.log
```

### 7. 监控自动领取日志

```bash
# 过滤自动领取相关日志
tail -f /root/TwinEngines/logs/twinengines.log | grep -E "auto_redeem|redeem"

# 或者查看最近的领取记录
grep -A 5 "auto_redeem" /root/TwinEngines/logs/twinengines.log | tail -50
```

## 改进内容

### 1. 增强的自动领取功能

- ✓ 添加了领取能力检查
- ✓ 使用配置的检查间隔（30秒）
- ✓ 增加了详细的错误日志
- ✓ 检查范围从10个扩大到20个合约
- ✓ 添加了更详细的日志输出

### 2. 错误处理

- ✓ 捕获并记录所有异常
- ✓ 区分不同类型的失败原因
- ✓ 提供诊断信息

### 3. 日志输出

现在会输出以下信息：
```json
{
  "phase": "auto_redeem",
  "action": "redeem_attempted",
  "redeemable_count": 1,
  "redeemed_count": 1,
  "failed_count": 0,
  "redeemed": [
    {
      "condition_id": "0x...",
      "tx_hash": "0x...",
      "pnl": 0.5
    }
  ],
  "failed": []
}
```

## 常见问题排查

### 问题1: relayer_dependencies_missing

**原因**: 缺少 py-builder-relayer-client 库

**解决**:
```bash
pip3 install 'git+https://github.com/Polymarket/py-builder-relayer-client.git'
```

### 问题2: web3_unavailable

**原因**: 缺少 web3 库或 RPC 连接失败

**解决**:
```bash
pip3 install web3
# 检查 RPC 连接
curl https://polygon-bor.publicnode.com
```

### 问题3: missing_builder_credentials

**原因**: Builder API 凭据未配置或配置错误

**解决**: 检查 .env 文件中的以下配置
```
POLY_BUILDER_API_KEY=...
POLY_BUILDER_SECRET=...
POLY_BUILDER_PASSPHRASE=...
```

### 问题4: 链上执行失败

**可能原因**:
1. Gas 不足
2. 合约尚未结算
3. 已经领取过
4. 网络拥堵

**排查**:
```bash
# 查看详细错误
grep "redeem_failed" /root/TwinEngines/logs/twinengines.log | tail -20
```

## 验证自动领取是否工作

1. 等待一个交易窗口结束并结算
2. 查看日志中是否出现 `auto_redeem` 相关输出
3. 检查是否有 `redeemed_count > 0` 的记录
4. 查看钱包余额是否增加

## 手动触发领取（测试用）

```bash
cd /root/TwinEngines
python3 test_auto_redeem.py
```

按提示选择 'y' 来手动领取可领取的持仓。

## 联系支持

如果问题仍然存在，请提供：
1. `test_auto_redeem.py` 的完整输出
2. 最近的日志文件（包含错误信息）
3. .env 配置（隐藏敏感信息）
