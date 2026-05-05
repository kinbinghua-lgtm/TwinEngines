# VPS 自动领取功能修复 - 快速指南

## 问题总结

VPS上的自动领取功能提示"链上执行失败"，主要原因可能是：
1. 缺少必要的依赖库（py-builder-relayer-client 或 web3）
2. 自动领取逻辑需要改进（检查间隔、错误处理等）

## 已完成的改进

✅ **增强自动领取功能** (`naked_pm_runner.py`)
- 添加了领取能力检查（redeem_capability_check）
- 使用配置的检查间隔（默认30秒，可在.env中配置）
- 增加了详细的错误日志
- 检查范围从10个扩大到20个合约
- 添加了更多的异常处理和诊断信息

✅ **创建诊断工具** (`test_auto_redeem.py`)
- 可以测试领取能力
- 可以查看可领取的持仓
- 可以手动触发领取操作

## 部署步骤（3步完成）

### 步骤1: 上传文件到VPS

使用 WinSCP 连接到 VPS：
- 主机: 47.243.169.235
- 用户: root
- 密码: Jinbh1977

上传以下3个文件：
```
本地 -> VPS
src/twinengines/live/naked_pm_runner.py -> /root/TwinEngines/src/twinengines/live/
src/twinengines/io/polymarket_client.py -> /root/TwinEngines/src/twinengines/io/
test_auto_redeem.py -> /root/TwinEngines/
```

### 步骤2: SSH连接VPS并安装依赖

```bash
ssh root@47.243.169.235
# 密码: Jinbh1977

cd /root/TwinEngines

# 安装依赖
pip3 install web3
pip3 install 'git+https://github.com/Polymarket/py-builder-relayer-client.git'
```

### 步骤3: 测试并重启服务

```bash
# 测试自动领取功能
python3 test_auto_redeem.py

# 如果测试通过（显示"领取能力检查: 通过"），重启服务
systemctl stop twinengines-strategy
systemctl start twinengines-strategy

# 查看日志
tail -f logs/twinengines.log | grep "auto_redeem"
```

## 预期结果

### test_auto_redeem.py 输出
```
=== 测试领取能力检查 ===
客户端初始化: 成功 - ok
领取能力检查: 通过 - ok

=== 测试获取持仓 ===
最近的 condition_ids 数量: X
检查最近 X 个合约的持仓...
```

### 日志输出
```json
{
  "phase": "auto_redeem",
  "action": "redeem_attempted",
  "redeemable_count": 1,
  "redeemed_count": 1,
  "failed_count": 0,
  "redeemed": [{"condition_id": "0x...", "tx_hash": "0x...", "pnl": 0.5}]
}
```

## 常见问题排查

### 问题1: relayer_dependencies_missing
**解决**: `pip3 install 'git+https://github.com/Polymarket/py-builder-relayer-client.git'`

### 问题2: web3_unavailable
**解决**: `pip3 install web3`

### 问题3: missing_builder_credentials
**检查**: .env 中的 POLY_BUILDER_API_KEY, POLY_BUILDER_SECRET, POLY_BUILDER_PASSPHRASE 是否正确

### 问题4: 链上执行失败
**排查**: 
```bash
# 查看详细错误
grep "redeem" /root/TwinEngines/logs/twinengines.log | tail -50

# 检查配置
cat /root/TwinEngines/.env | grep -E "AUTO_REDEEM|SIGNATURE_TYPE|BUILDER"
```

## 配置说明

确保 .env 中有以下配置：
```bash
AUTO_REDEEM_ENABLED=true
AUTO_REDEEM_INTERVAL_SEC=30
AUTO_REDEEM_RECEIPT_WAIT_SEC=20
SIGNATURE_TYPE=1
POLY_BUILDER_API_KEY=019ca0ba-1881-7b79-a773-aec734fc44a6
POLY_BUILDER_SECRET=BfX2rtdxC1VvDrFNiNTYmkyS5q3SXP1b30BZMaQueJc=
POLY_BUILDER_PASSPHRASE=5cc7b41006f5ec3de3c444c5679f178fc001517d50c8c0e7fab2278f00820999
```

## 验证自动领取是否工作

1. 等待一个交易窗口结束并结算（约5分钟）
2. 查看日志: `tail -f logs/twinengines.log | grep auto_redeem`
3. 应该看到类似输出: `{"phase": "auto_redeem", "action": "redeem_attempted", ...}`
4. 如果有可领取的持仓，会显示 `"redeemed_count": 1` 或更多

## 需要帮助？

如果问题仍然存在，请提供：
1. `python3 test_auto_redeem.py` 的完整输出
2. 最近的日志: `tail -100 logs/twinengines.log`
3. 配置信息（隐藏敏感信息）: `cat .env | grep -E "AUTO_REDEEM|SIGNATURE_TYPE"`
