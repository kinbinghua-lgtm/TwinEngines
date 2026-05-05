@echo off
REM VPS自动部署脚本 - 使用WinSCP命令行

echo ================================================================
echo           VPS 自动部署脚本 (使用 WinSCP)
echo ================================================================
echo.

REM 检查WinSCP是否安装
where winscp.com >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 winscp.com
    echo 请安装 WinSCP 或将 winscp.com 添加到 PATH
    echo 下载地址: https://winscp.net/eng/download.php
    pause
    exit /b 1
)

echo [OK] 找到 WinSCP
echo.

REM 创建WinSCP脚本
echo 创建上传脚本...
(
echo option batch abort
echo option confirm off
echo open sftp://root:Jinbh1977@47.243.169.235/
echo cd /root/TwinEngines
echo put "src\twinengines\live\naked_pm_runner.py" "src/twinengines/live/"
echo put "src\twinengines\io\polymarket_client.py" "src/twinengines/io/"
echo put "test_auto_redeem.py" "."
echo exit
) > winscp_upload.txt

echo [OK] 脚本创建完成
echo.

echo 开始上传文件...
winscp.com /script=winscp_upload.txt

if %errorlevel% equ 0 (
    echo [成功] 文件上传完成
) else (
    echo [失败] 文件上传失败
    del winscp_upload.txt
    pause
    exit /b 1
)

del winscp_upload.txt

echo.
echo ================================================================
echo 文件上传完成！
echo ================================================================
echo.
echo 接下来请手动执行以下命令:
echo.
echo 1. SSH连接到VPS:
echo    ssh root@47.243.169.235
echo    密码: Jinbh1977
echo.
echo 2. 安装依赖:
echo    cd /root/TwinEngines
echo    pip3 install web3
echo    pip3 install 'git+https://github.com/Polymarket/py-builder-relayer-client.git'
echo.
echo 3. 测试:
echo    python3 test_auto_redeem.py
echo.
echo 4. 重启服务:
echo    systemctl restart twinengines-strategy
echo.
echo 5. 查看日志:
echo    tail -f logs/twinengines.log ^| grep auto_redeem
echo.

pause
