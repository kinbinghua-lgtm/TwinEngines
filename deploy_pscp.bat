@echo off
REM VPS自动部署脚本 - 使用 pscp (PuTTY)

echo ================================================================
echo           VPS 自动部署脚本 (使用 pscp)
echo ================================================================
echo.

REM 检查pscp是否安装
where pscp.exe >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 pscp.exe
    echo 请安装 PuTTY 或将 pscp.exe 添加到 PATH
    echo 下载地址: https://www.chiark.greenend.org.uk/~sgtatham/putty/latest.html
    pause
    exit /b 1
)

echo [OK] 找到 pscp
echo.

set VPS_HOST=47.243.169.235
set VPS_USER=root
set VPS_PASS=Jinbh1977

echo 开始上传文件...
echo.

echo [1/3] 上传 naked_pm_runner.py...
echo %VPS_PASS%| pscp -pw %VPS_PASS% "src\twinengines\live\naked_pm_runner.py" %VPS_USER%@%VPS_HOST%:/root/TwinEngines/src/twinengines/live/
if %errorlevel% neq 0 (
    echo [失败] 上传失败
    pause
    exit /b 1
)
echo [OK] 上传成功
echo.

echo [2/3] 上传 polymarket_client.py...
echo %VPS_PASS%| pscp -pw %VPS_PASS% "src\twinengines\io\polymarket_client.py" %VPS_USER%@%VPS_HOST%:/root/TwinEngines/src/twinengines/io/
if %errorlevel% neq 0 (
    echo [失败] 上传失败
    pause
    exit /b 1
)
echo [OK] 上传成功
echo.

echo [3/3] 上传 test_auto_redeem.py...
echo %VPS_PASS%| pscp -pw %VPS_PASS% "test_auto_redeem.py" %VPS_USER%@%VPS_HOST%:/root/TwinEngines/
if %errorlevel% neq 0 (
    echo [失败] 上传失败
    pause
    exit /b 1
)
echo [OK] 上传成功
echo.

echo ================================================================
echo 文件上传完成！
echo ================================================================
echo.

echo 现在连接到VPS执行后续步骤...
echo.

REM 检查plink是否可用
where plink.exe >nul 2>&1
if %errorlevel% neq 0 (
    echo [警告] 未找到 plink.exe，无法自动执行远程命令
    goto manual_steps
)

echo [OK] 找到 plink，开始执行远程命令...
echo.

echo [步骤1] 安装 web3...
echo %VPS_PASS%| plink -pw %VPS_PASS% %VPS_USER%@%VPS_HOST% "cd /root/TwinEngines && pip3 install web3"
echo.

echo [步骤2] 安装 py-builder-relayer-client...
echo %VPS_PASS%| plink -pw %VPS_PASS% %VPS_USER%@%VPS_HOST% "cd /root/TwinEngines && pip3 install 'git+https://github.com/Polymarket/py-builder-relayer-client.git'"
echo.

echo [步骤3] 测试自动领取功能...
echo %VPS_PASS%| plink -pw %VPS_PASS% %VPS_USER%@%VPS_HOST% "cd /root/TwinEngines && python3 test_auto_redeem.py"
echo.

echo ================================================================
echo 测试完成！
echo ================================================================
echo.

set /p RESTART="是否重启服务？(y/n): "
if /i "%RESTART%"=="y" (
    echo.
    echo 重启服务...
    echo %VPS_PASS%| plink -pw %VPS_PASS% %VPS_USER%@%VPS_HOST% "systemctl restart twinengines-strategy"
    echo [OK] 服务已重启
    echo.
    
    timeout /t 3 /nobreak >nul
    
    echo 查看服务状态...
    echo %VPS_PASS%| plink -pw %VPS_PASS% %VPS_USER%@%VPS_HOST% "systemctl status twinengines-strategy"
    echo.
    
    echo 查看最近的日志...
    echo %VPS_PASS%| plink -pw %VPS_PASS% %VPS_USER%@%VPS_HOST% "tail -50 /root/TwinEngines/logs/twinengines.log | grep -E 'auto_redeem|redeem' || tail -50 /root/TwinEngines/logs/twinengines.log"
)

goto end

:manual_steps
echo.
echo 请手动执行以下命令:
echo.
echo 1. SSH连接到VPS:
echo    plink -ssh root@47.243.169.235
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

:end
echo.
echo ================================================================
echo 部署完成！
echo ================================================================
echo.
pause
