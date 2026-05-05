# VPS自动领取状态检查脚本
# 使用方法: 需要先安装PuTTY (包含plink工具)

$VPS_HOST = "47.243.169.235"
$VPS_USER = "root"
$VPS_PASSWORD = "Jinbh1977"

function Run-SSHCommand {
    param([string]$Command)
    
    # 使用plink (PuTTY命令行工具)
    $plinkPath = "plink.exe"
    
    # 检查plink是否可用
    try {
        $null = Get-Command $plinkPath -ErrorAction Stop
    } catch {
        Write-Host "错误: plink未找到，请安装PuTTY" -ForegroundColor Red
        return $null
    }
    
    # 执行SSH命令
    $output = echo y | & $plinkPath -ssh -l $VPS_USER -pw $VPS_PASSWORD $VPS_HOST $Command 2>&1
    return $output
}

Write-Host "=== 检查VPS自动领取状态 ===" -ForegroundColor Cyan
Write-Host ""

Write-Host "1. 检查运行进程:" -ForegroundColor Yellow
$result = Run-SSHCommand "ps aux | grep -E 'python|twinengines' | grep -v grep"
if ($result) { Write-Host $result }

Write-Host "`n2. 检查最近的日志 (最后50行):" -ForegroundColor Yellow
$result = Run-SSHCommand "tail -50 /root/TwinEngines/logs/twinengines.log"
if ($result) { Write-Host $result }

Write-Host "`n3. 检查状态文件:" -ForegroundColor Yellow
$result = Run-SSHCommand "cat /root/TwinEngines/data_runtime/naked_real_state.json 2>/dev/null || echo 'State file not found'"
if ($result) { Write-Host $result }

Write-Host "`n4. 检查自动领取配置:" -ForegroundColor Yellow
$result = Run-SSHCommand "cd /root/TwinEngines && grep -E 'AUTO_REDEEM|SIGNATURE_TYPE|BUILDER' .env | head -20"
if ($result) { Write-Host $result }

Write-Host "`n5. 检查Python依赖:" -ForegroundColor Yellow
$result = Run-SSHCommand "cd /root/TwinEngines && python3 -c 'import py_builder_relayer_client; print(\"relayer_client OK\")' 2>&1"
if ($result) { Write-Host $result }

$result = Run-SSHCommand "cd /root/TwinEngines && python3 -c 'import web3; print(\"web3 OK\")' 2>&1"
if ($result) { Write-Host $result }

Write-Host "`n=== 检查完成 ===" -ForegroundColor Cyan
