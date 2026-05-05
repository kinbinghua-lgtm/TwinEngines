# VPS自动领取功能修复 - 最终部署方案

## 当前状态

### ✅ 已完成
1. **代码修复完成** - 本地已修复自动领取功能
2. **测试通过** - 本地测试显示领取能力检查通过
3. **文件准备就绪** - 需要上传的3个文件已准备好

### ❌ 遇到的问题
- **SSH密码认证失败** - paramiko连接VPS时认证失败
- 可能原因：
  1. VPS的SSH配置不允许密码认证（只允许密钥）
  2. 密码可能已更改
  3. 需要特殊的SSH配置

## 需要上传的文件

```
本地路径 -> VPS路径
─────────────────────────────────────────────────────────────
E:\TwinEngines\src\twinengines\live\naked_pm_runner.py
  -> /root/TwinEngines/src/twinengines/live/naked_pm_runner.py

E:\TwinEngines\src\twinengines\io\polymarket_client.py
  -> /root/TwinEngines/src/twinengines/io/polymarket_client.py

E:\TwinEngines\test_auto_redeem.py
  -> /root/TwinEngines/test_auto_redeem.py
```

## 推荐部署方法：使用WinSCP

### 步骤1: 下载并安装WinSCP
- 下载地址: https://winscp.net/eng/download.php
- 安装后打开WinSCP

### 步骤2: 连接到VPS
```
文件协议: SFTP
主机名: 47.243.169.235
端口: 22
用户名: root
密码: Jinbh1977
```
点击"登录"

**如果密码认证失败**，可能需要：
- 使用SSH密钥认证
- 联系VPS管理员确认密码
- 检查SSH配置是否允许密码登录

### 步骤3: 上传文件
在WinSCP中，左侧是本地文件，右侧是VPS文件。

1. 在左侧导航到 `E:\TwinEngines\src\twinengines\live\`
2. 在右侧导航到 `/root/TwinEngines/src/twinengines/live/`
3. 将 `naked_pm_runner.py` 从左侧拖到右侧

重复以上步骤上传其他两个文件。

### 步骤4: 打开终端并执行命令
在WinSCP中按 `Ctrl+T` 打开终端，或使用PuTTY连接。

```bash
# 进入项目目录
cd /root/TwinEngines

# 安装依赖
pip3 install web3
pip3 install 'git+https://github.com/Polymarket/py-builder-relayer-client.git'

# 测试自动领取功能
python3 test_auto_redeem.py
```

**预期输出**:
```
=== 测试领取能力检查 ===
客户端初始化: 成功 - ok
领取能力检查: 通过 - ok
```

### 步骤5: 重启服务
如果测试通过：
```bash
systemctl restart twinengines-strategy
systemctl status twinengines-strategy
```

### 步骤6: 监控日志
```bash
tail -f logs/twinengines.log | grep auto_redeem
```

应该看到类似输出：
```json
{"phase": "auto_redeem", "action": "redeem_attempted", ...}
```

## 备选方案：手动编辑文件

如果无法上传文件，可以手动编辑：

### 1. SSH连接到VPS
```bash
ssh root@47.243.169.235
# 或使用PuTTY
```

### 2. 备份原文件
```bash
cd /root/TwinEngines
cp src/twinengines/live/naked_pm_runner.py src/twinengines/live/naked_pm_runner.py.bak
```

### 3. 编辑文件
```bash
nano src/twinengines/live/naked_pm_runner.py
```

然后：
1. 在本地打开 `E:\TwinEngines\src\twinengines\live\naked_pm_runner.py`
2. 复制全部内容
3. 在nano中删除所有内容（Ctrl+K多次）
4. 粘贴新内容
5. 保存（Ctrl+O）并退出（Ctrl+X）

对其他文件重复此操作。

## 改进内容总结

### 主要改进
1. **添加领取能力检查** - 在尝试领取前检查配置和依赖
2. **使用配置的检查间隔** - 从固定60秒改为可配置（默认30秒）
3. **增加详细日志** - 记录每次领取尝试的详细信息
4. **扩大检查范围** - 从10个合约扩大到20个
5. **更好的错误处理** - 捕获并记录所有异常

### 代码变更位置

**naked_pm_runner.py** (第180-240行):
```python
def _run_auto_redeem_tick(
    *,
    poly: PolymarketClient,
    state_path: Path,
    runtime_cfg: Any,  # 新增参数
) -> dict[str, Any] | None:
    # 检查是否启用
    if not getattr(runtime_cfg, "auto_redeem_enabled", False):
        return None
    
    # 使用配置的间隔
    interval_ms = int(getattr(runtime_cfg, "auto_redeem_interval_sec", 30.0) * 1000)
    
    # 检查领取能力
    ok_cap, reason_cap = poly.redeem_capability_check()
    if not ok_cap:
        logger.warning("auto_redeem capability check failed: %s", reason_cap)
        return {"phase": "auto_redeem", "action": "capability_check_failed", "reason": reason_cap}
    
    # ... 其余代码
```

## 验证部署成功

### 1. 测试输出正常
```
客户端初始化: 成功 - ok
领取能力检查: 通过 - ok
```

### 2. 服务运行正常
```bash
systemctl status twinengines-strategy
# 应显示 active (running)
```

### 3. 日志中出现自动领取记录
```bash
grep "auto_redeem" logs/twinengines.log | tail -10
```

### 4. 等待实际领取
- 等待一个交易窗口结束（约5分钟）
- 如果有赢单，应该会自动领取
- 日志中会显示 `"redeemed_count": 1` 或更多

## 常见问题

### Q: SSH连接失败
**A:** 
- 检查密码是否正确
- 尝试使用PuTTY或其他SSH客户端
- 可能需要使用SSH密钥而不是密码

### Q: 测试显示 "relayer_dependencies_missing"
**A:** 
```bash
pip3 install 'git+https://github.com/Polymarket/py-builder-relayer-client.git'
```

### Q: 测试显示 "web3_unavailable"
**A:** 
```bash
pip3 install web3
```

### Q: 没有可领取的持仓
**A:** 
- 这是正常的，只有 `currPrice=1` 的持仓才能领取
- 等待交易窗口结束并结算
- 只有赢的单子才能领取

## 下一步

1. ✅ 使用WinSCP连接VPS（推荐）
2. ✅ 上传3个文件
3. ✅ 安装依赖
4. ✅ 运行测试
5. ✅ 重启服务
6. ✅ 监控日志

---

**所有修复的文件位于**: `E:\TwinEngines\`
**VPS信息**: root@47.243.169.235
**密码**: Jinbh1977 (如果认证失败，请确认密码或使用密钥)
