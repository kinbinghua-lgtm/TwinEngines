#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键部署自动领取功能到VPS"""

import os
import sys
from pathlib import Path

def print_manual_steps():
    """打印手动操作步骤"""
    print("""
================================================================
          TwinEngines VPS 自动领取功能修复指南
================================================================

由于 SSH 连接问题，请按以下步骤手动操作：

================================================================
步骤 1: 使用 WinSCP 上传文件
================================================================

1. 打开 WinSCP (如未安装，从 https://winscp.net 下载)
2. 连接信息:
   - 主机: 47.243.169.235
   - 用户名: root
   - 密码: Jinbh1977
   - 端口: 22

3. 上传以下文件:

   本地文件                                          -> VPS路径
   -----------------------------------------------------------------
   src/twinengines/live/naked_pm_runner.py          -> /root/TwinEngines/src/twinengines/live/
   src/twinengines/io/polymarket_client.py          -> /root/TwinEngines/src/twinengines/io/
   test_auto_redeem.py                              -> /root/TwinEngines/

================================================================
步骤 2: SSH 连接到 VPS
================================================================

使用 PuTTY 或其他 SSH 客户端连接:
   ssh root@47.243.169.235
   密码: Jinbh1977

================================================================
步骤 3: 在 VPS 上执行以下命令
================================================================

# 进入项目目录
cd /root/TwinEngines

# 安装/更新依赖
pip3 install web3
pip3 install 'git+https://github.com/Polymarket/py-builder-relayer-client.git'

# 测试自动领取功能
python3 test_auto_redeem.py

# 如果测试通过，重启服务
systemctl stop twinengines-strategy
systemctl start twinengines-strategy

# 查看日志
tail -f logs/twinengines.log | grep -E "auto_redeem|redeem"

================================================================
预期结果
================================================================

test_auto_redeem.py 应该输出:
  [OK] 客户端初始化: 成功
  [OK] 领取能力检查: 通过

日志中应该出现:
  {"phase": "auto_redeem", "action": "redeem_attempted", ...}

================================================================
改进内容
================================================================

1. [OK] 增加了领取能力检查
2. [OK] 使用配置的检查间隔（30秒，可在 .env 中配置）
3. [OK] 增加了详细的错误日志
4. [OK] 检查范围从10个扩大到20个合约
5. [OK] 添加了诊断工具 test_auto_redeem.py

================================================================
常见问题
================================================================

Q: 提示 "relayer_dependencies_missing"
A: 运行: pip3 install 'git+https://github.com/Polymarket/py-builder-relayer-client.git'

Q: 提示 "web3_unavailable"
A: 运行: pip3 install web3

Q: 提示 "missing_builder_credentials"
A: 检查 .env 中的 POLY_BUILDER_API_KEY, POLY_BUILDER_SECRET, POLY_BUILDER_PASSPHRASE

Q: 链上执行失败
A: 查看详细日志: grep "redeem" logs/twinengines.log | tail -50

================================================================

详细文档请查看: VPS_AUTO_REDEEM_FIX.md

""")

def main():
    print_manual_steps()
    
    # 检查文件是否存在
    files_to_check = [
        "src/twinengines/live/naked_pm_runner.py",
        "src/twinengines/io/polymarket_client.py",
        "test_auto_redeem.py",
    ]
    
    print("\n检查本地文件...")
    all_exist = True
    for f in files_to_check:
        path = Path(f)
        if path.exists():
            print(f"  [OK] {f}")
        else:
            print(f"  [X] {f} (不存在)")
            all_exist = False
    
    if not all_exist:
        print("\n错误: 某些文件不存在，请确保在项目根目录运行此脚本")
        return 1
    
    print("\n所有文件准备就绪，请按照上述步骤操作。")
    return 0

if __name__ == "__main__":
    sys.exit(main())
