# 置信度阈值调整方案

## 📊 当前代码结构分析

### 1. 两套独立的配置系统

#### **系统A: `NAKED_DYNAMIC_ENTRY_TIERS`** (naked_pm_runner.py 第89行)
用于**动态入场盘口闸**，控制不同置信度下的最高买价和最小edge要求。

```python
NAKED_DYNAMIC_ENTRY_TIERS: tuple[tuple[float, float, float], ...] = (
    # (confidence下限, best_ask最高允许值, fair_prob_side - best_ask 最小值)
    (0.85, 0.78, 0.08),  # 超高置信度: ask<0.78, edge≥0.08
    (0.75, 0.72, 0.07),  # 高置信度: ask<0.72, edge≥0.07
    (0.65, 0.65, 0.06),  # 中高置信度: ask<0.65, edge≥0.06
    (0.56, 0.55, 0.05),  # 中等置信度: ask<0.55, edge≥0.05
    (0.501, 0.49, 0.06), # 低置信度: ask<0.49, edge≥0.06  ← 当前最低档
)
```

**作用**: 
- 在 `_dynamic_entry_rule_for_conf()` 函数中使用
- 从上到下匹配第一个满足 `conf >= min_conf` 的档位
- 返回该档的 `max_ask` 和 `min_edge` 要求

#### **系统B: `DEFAULT_CONF_ODDS_TIERS`** (naked_odds_tiers.py 第12行)
用于**赔率分档**，控制不同置信度下的最高买价上限（用于止盈等）。

```python
DEFAULT_CONF_ODDS_TIERS: ConfOddsTiers = (
    # (confidence下限, best_ask严格上界)
    (0.60, 0.50),
    (0.56, 0.40),
    (0.53, 0.30),
    (0.52, 0.20),
    (0.51, 0.10),  ← 当前最低档
)
```

**作用**:
- 在 `odds_cap_strict_below_for_confidence()` 函数中使用
- 用于止盈逻辑和价格上限控制

#### **系统C: CLI参数 `--confidence-min`** (cli.py 第1044行)
```python
nk.add_argument(
    "--confidence-min",
    type=float,
    default=0.51,  ← 默认值
    help="押边概率 confidence_on_pick 下限；默认 0.51。"
)
```

**作用**:
- 全局最低置信度阈值
- 低于此值的预测直接拒绝，不进入后续逻辑

### 2. 三者的关系

```
CLI --confidence-min (0.51)
    ↓ 全局过滤
NAKED_DYNAMIC_ENTRY_TIERS (0.501档)
    ↓ 入场条件检查
DEFAULT_CONF_ODDS_TIERS (0.51档)
    ↓ 止盈和价格控制
```

## 🎯 修改方案

根据你的建议：**低置信区从0.501开始**

### 方案A: 仅修改入场档位（推荐）

**优点**: 
- 改动最小
- 只影响入场逻辑
- 其他系统保持稳定

**修改内容**:

1. **修改 `NAKED_DYNAMIC_ENTRY_TIERS`** (naked_pm_runner.py)
   ```python
   NAKED_DYNAMIC_ENTRY_TIERS: tuple[tuple[float, float, float], ...] = (
       (0.85, 0.78, 0.08),
       (0.75, 0.72, 0.07),
       (0.65, 0.65, 0.06),
       (0.56, 0.55, 0.05),
       (0.501, 0.49, 0.08),  # 修改: edge从0.06提高到0.08
   )
   ```

2. **保持 CLI 默认值不变** (0.51)
   - 用户可以通过 `--confidence-min 0.501` 手动调整

3. **保持 `DEFAULT_CONF_ODDS_TIERS` 不变**
   - 止盈逻辑不受影响

### 方案B: 全面调整（更激进）

**优点**:
- 三个系统保持一致
- 更彻底的策略调整

**修改内容**:

1. **修改 `NAKED_DYNAMIC_ENTRY_TIERS`**
   ```python
   NAKED_DYNAMIC_ENTRY_TIERS: tuple[tuple[float, float, float], ...] = (
       (0.85, 0.78, 0.08),
       (0.75, 0.72, 0.07),
       (0.65, 0.65, 0.06),
       (0.56, 0.55, 0.05),
       (0.501, 0.49, 0.08),  # 新增: 低置信度高edge档
   )
   ```

2. **修改 CLI 默认值**
   ```python
   nk.add_argument(
       "--confidence-min",
       type=float,
       default=0.501,  # 从0.51改为0.501
       help="押边概率 confidence_on_pick 下限；默认 0.501。"
   )
   ```

3. **修改 `DEFAULT_CONF_ODDS_TIERS`**
   ```python
   DEFAULT_CONF_ODDS_TIERS: ConfOddsTiers = (
       (0.60, 0.50),
       (0.56, 0.40),
       (0.53, 0.30),
       (0.52, 0.20),
       (0.501, 0.10),  # 从0.51改为0.501
   )
   ```

## 💡 我的建议

### 推荐：**方案A + 渐进式验证**

**第一步**: 修改入场档位
- 只改 `NAKED_DYNAMIC_ENTRY_TIERS` 的最低档
- 将 edge 要求从 0.06 提高到 0.08
- CLI 保持 0.51，通过命令行参数 `--confidence-min 0.501` 测试

**第二步**: 观察效果
- 先在 dry-run 模式运行 24-48 小时
- 观察信号数量和质量
- 对比 Edge 验证报告的数据

**第三步**: 根据结果决定是否全面调整
- 如果效果好，再考虑修改 CLI 默认值和止盈档位
- 如果效果不理想，可以快速回滚

### 具体修改代码

```python
# 文件: src/twinengines/live/naked_pm_runner.py
# 第89行附近

# 修改前:
NAKED_DYNAMIC_ENTRY_TIERS: tuple[tuple[float, float, float], ...] = (
    (0.85, 0.78, 0.08),
    (0.75, 0.72, 0.07),
    (0.65, 0.65, 0.06),
    (0.56, 0.55, 0.05),
    (0.501, 0.49, 0.06),  # ← 旧值
)

# 修改后:
NAKED_DYNAMIC_ENTRY_TIERS: tuple[tuple[float, float, float], ...] = (
    (0.85, 0.78, 0.08),
    (0.75, 0.72, 0.07),
    (0.65, 0.65, 0.06),
    (0.56, 0.55, 0.05),
    (0.501, 0.49, 0.08),  # ← 新值: edge从0.06提高到0.08
)
```

### 测试命令

```bash
# 本地测试
cd /root/TwinEngines
python -m twinengines.cli naked-third-digit-live \
  --model-json reports/prefix_survival_model_90d_step10.json \
  --confidence-min 0.501 \
  --target-quote-usdc 1 \
  --poll-sec 8 \
  --once

# VPS dry-run 测试
systemctl stop twinengines-strategy
python -m twinengines.cli naked-third-digit-live \
  --model-json reports/prefix_survival_model_90d_step10.json \
  --confidence-min 0.501 \
  --env-file .env \
  --state-json data_runtime/naked_real_state.json \
  --live-log-jsonl logs/naked_live_ticks.jsonl \
  --max-runtime-min 60
```

## ⚠️ 注意事项

1. **Edge验证报告的对应关系**
   - 报告中的 "low_conf_shadow" 区间是 0.52~0.56
   - 我们的修改是 0.501~0.52 区间
   - 需要重新生成 Edge 报告来验证 0.501~0.52 的效果

2. **与现有实盘的兼容性**
   - 如果当前实盘正在运行，需要先停止
   - 修改后重启服务
   - 观察日志中的 `confidence_on_pick` 分布

3. **回滚方案**
   - 保留原文件备份
   - 如果效果不好，直接恢复原值
   - 重启服务即可

## 📝 总结

**我打算这样修改**:

1. ✅ **修改 `NAKED_DYNAMIC_ENTRY_TIERS` 最低档**
   - 保持 confidence 下限 0.501
   - 将 edge 要求从 0.06 提高到 0.08
   - 这样既降低了置信度门槛，又提高了edge要求

2. ✅ **保持 CLI 默认值 0.51**
   - 通过命令行参数灵活调整
   - 便于A/B测试

3. ✅ **暂不修改止盈档位**
   - 先验证入场效果
   - 避免一次改动过多

4. ✅ **建议测试流程**
   - 本地测试 → dry-run 24小时 → 小额实盘 → 全量实盘

你觉得这个方案如何？需要我立即执行修改吗？
