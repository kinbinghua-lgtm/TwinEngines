# 自动领取功能部署指南

## 更新内容

本次更新增强了自动领取功能，主要改进：

1. **增加了领取能力检查** (`redeem_capability_check`)
   - 检查必要的依赖库（web3, relayer client）
   - 检查配置完整性（私钥、代理地址、Builder API凭据）
   - 支持多种签名类型（SIGNATURE_TYPE=0/1/2）

2. **使用配置的检查间隔**
   - 默认30秒检查一次（可在 .env 中通过 `AUTO_REDEEM_INTERVAL_SEC` 配置）
   - 避免频繁查询

3. **增加了详细的错误日志**
   - 记录每次领取尝试的详细信息
   - 区分不同类型的错误（relayer错误、web3错误、签名错误等）

4. **扩大检查范围**
   - 从检查最近10个合约扩大到20个
   - 提高发现可领取持仓的概率

5. **支持 Relayer 模式领取**
   - 支持 SIGNATURE_TYPE=1 (Magic Proxy) 和 2 (Safe) 的领取
   - 自动选择合适的领取方式

## 需要上传的文件

```
本地文件                                          VPS路径
─────────────────────────────────────────────────────────────────
src/twinengines/live/naked_pm_runner.py          /root/TwinEngines/src/twinengines/live/
src/twinengines/io/polymarket_client.py          /root/TwinEngines/src/twinengines/io/
test_auto_redeem.py                              /root/TwinEngines/
```

## 手动部署步骤

### 方法1: 使用 WinSCP GUI（推荐）

1. **打开 WinSCP**
   - 如未安装，从 https://winscp.net 下载

2. **连接到VPS**
   - 主机: `47.243.169.223`
   - 用户名: `root`
   - 密码: `Jinbh1977`
   - 端口: `22`

3. **上传文件**
   - 导航到本地项目目录 `E:\TwinEngines`
   - 在右侧（VPS）导航到 `/root/TwinEngines`
   - 拖拽以下文件到对应目录：
     - `src/twinengines/live/naked_pm_runner.py` → `/root/TwinEngines/src/twinengines/live/`
     - `src/twinengines/io/polymarket_client.py` → `/root/TwinEngines/src/twinengines/io/`
     - `test_auto_redeem.py` → `/root/TwinEngines/`

### 方法2: 使用 SSH + 手动编辑

如果无法使用 WinSCP，可以通过 SSH 连接后手动编辑文件。

## VPS上的操作步骤

### 1. SSH 连接到 VPS

```bash
ssh root@47.243.169.223
# 密码: Jinbh1977
```

### 2. 进入项目目录

```bash
cd /root/TwinEngines
```

### 3. 检查依赖

```bash
# 安装/更新必要的依赖
pip3 install web3
pip3 install 'git+https://github.com/Polymarket/py-builder-relayer-client.git'
```

### 4. 测试自动领取功能

```bash
python3 test_auto_redeem.py
```

**预期输出：**
```
=== 测试领取能力检查 ===

客户端初始化: 成功 - ok
领取能力检查: 通过 - ok

=== 测试获取持仓 ===

最近的 condition_ids 数量: X
检查最近 Y 个合约的持仓...

1. Condition ID: 0xabc...
   持仓数量: 1
   - Outcome: Yes, Size: 10.0, CurrPrice: 1, PnL: 5.0
   ✓ 可领取！
```

### 5. 重启服务

```bash
# 停止服务
systemctl stop twinengines-strategy

# 启动服务
systemctl start twinengines-strategy

# 查看服务状态
systemctl status twinengines-strategy
```

### 6. 查看日志

```bash
# 实时查看自动领取相关日志
tail -f logs/twinengines.log | grep -E "auto_redeem|redeem"

# 或查看最近的日志
tail -100 logs/twinengines.log | grep -E "auto_redeem|redeem"
```

**预期日志内容：**
```json
{
  "phase": "auto_redeem",
  "action": "redeem_attempted",
  "redeemable_count": 1,
  "redeemed_count": 1,
  "failed_count": 0,
  "redeemed": [
    {
      "condition_id": "0xabc...",
      "tx_hash": "0x123...",
      "pnl": 5.0
    }
  ]
}
```

## 配置说明

在 VPS 的 `.env` 文件中，确保以下配置正确：

```bash
# 自动领取开关
AUTO_REDEEM_ENABLED=true

# 检查间隔（秒）
AUTO_REDEEM_INTERVAL_SEC=30

# 等待交易回执的超时时间（秒）
AUTO_REDEEM_RECEIPT_WAIT_SEC=30

# 签名类型（0=EOA直接签名, 1=Magic Proxy, 2=Safe）
SIGNATURE_TYPE=1

# 如果使用 SIGNATURE_TYPE=1 或 2，需要配置 Builder API
POLY_BUILDER_API_KEY=your_api_key
POLY_BUILDER_SECRET=your_secret
POLY_BUILDER_PASSPHRASE=your_passphrase
```

## 常见问题

### Q: 提示 "relayer_dependencies_missing"
**A:** 运行以下命令安装依赖：
```bash
pip3 install 'git+https://github.com/Polymarket/py-builder-relayer-client.git'
```

### Q: 提示 "web3_unavailable"
**A:** 运行以下命令安装 web3：
```bash
pip3 install web3
```

### Q: 提示 "missing_builder_credentials"
**A:** 检查 `.env` 文件中的以下配置：
- `POLY_BUILDER_API_KEY`
- `POLY_BUILDER_SECRET`
- `POLY_BUILDER_PASSPHRASE`

### Q: 提示 "proxy_not_equal_to_eoa"
**A:** 如果使用 `SIGNATURE_TYPE=0`，`PROXY_ADDRESS` 必须等于私钥对应的 EOA 地址。建议改用 `SIGNATURE_TYPE=1` (Magic Proxy)。

### Q: 链上执行失败
**A:** 查看详细日志：
```bash
grep "redeem" logs/twinengines.log | tail -50
```

## 验证部署成功

1. **服务正常运行**
   ```bash
   systemctl status twinengines-strategy
   # 应显示 "active (running)"
   ```

2. **日志中出现自动领取检查**
   ```bash
   tail -f logs/twinengines.log | grep auto_redeem
   # 应定期（每30秒）出现检查记录
   ```

3. **测试脚本通过**
   ```bash
   python3 test_auto_redeem.py
   # 应显示 "领取能力检查: 通过"
   ```

## 本地测试结果

本地测试显示：
- ✓ 客户端初始化成功
- ✓ 领取能力检查通过
- ✓ 可以正常获取持仓信息
- 当前有1个持仓，但 CurrPrice=0（未结算或输了），暂无可领取的

## 联系信息

如有问题，请检查：
1. VPS 日志: `/root/TwinEngines/logs/twinengines.log`
2. 服务状态: `systemctl status twinengines-strategy`
3. 配置文件: `/root/TwinEngines/.env`
