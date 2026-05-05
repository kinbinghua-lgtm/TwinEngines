@echo off
echo ============================================================
echo TwinEngines - 上传自动领取功能到VPS
echo ============================================================
echo.

set VPS_HOST=47.243.169.223
set VPS_USER=root
set VPS_PASS=Jinbh1977

echo 准备上传以下文件:
echo   - src/twinengines/live/naked_pm_runner.py
echo   - src/twinengines/io/polymarket_client.py
echo   - test_auto_redeem.py
echo.

echo 使用 WinSCP 上传文件...
echo.

REM 创建 WinSCP 脚本
echo open sftp://%VPS_USER%:%VPS_PASS%@%VPS_HOST% > winscp_upload.txt
echo cd /root/TwinEngines >> winscp_upload.txt
echo put src/twinengines/live/naked_pm_runner.py src/twinengines/live/ >> winscp_upload.txt
echo put src/twinengines/io/polymarket_client.py src/twinengines/io/ >> winscp_upload.txt
echo put test_auto_redeem.py . >> winscp_upload.txt
echo exit >> winscp_upload.txt

echo WinSCP 脚本已创建: winscp_upload.txt
echo.
echo 请执行以下步骤:
echo.
echo 方法1: 使用 WinSCP GUI
echo   1. 打开 WinSCP
echo   2. 连接信息:
echo      主机: %VPS_HOST%
echo      用户名: %VPS_USER%
echo      密码: %VPS_PASS%
echo   3. 拖拽以下文件到对应目录:
echo      - src/twinengines/live/naked_pm_runner.py
echo      - src/twinengines/io/polymarket_client.py
echo      - test_auto_redeem.py
echo.
echo 方法2: 使用 WinSCP 命令行
echo   winscp.com /script=winscp_upload.txt
echo.
echo 上传完成后，在VPS上执行:
echo   ssh root@%VPS_HOST%
echo   cd /root/TwinEngines
echo   python3 test_auto_redeem.py
echo   systemctl restart twinengines-strategy
echo.
pause
