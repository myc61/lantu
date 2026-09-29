<#
.SYNOPSIS
    chassis_following FTP 压测客户端 —— Windows 一键安装脚本

.DESCRIPTION
    将客户端文件安装到目标目录，检测 Python 环境，创建桌面快捷方式，
    并可选注册"计划任务"实现定时压测。

.PARAMETER TargetDir
    安装目录，默认:
    D:\GTM\6. 项目内容\1. 岚图汽车具身智能化改造技术\FTP_Client

.PARAMETER WithTask
    注册 Windows 计划任务（默认每 30 分钟执行一次混合压测）。

.PARAMETER NoShortcut
    不创建桌面快捷方式。

.EXAMPLE
    # 在管理员 PowerShell 中（右键"以管理员身份运行"）:
    Set-ExecutionPolicy -Scope Process Bypass -Force
    .\install.ps1
    .\install.ps1 -WithTask
    .\install.ps1 -TargetDir "D:\FTP_Client"
#>

[CmdletBinding()]
param(
    [string]$TargetDir = "D:\GTM\6. 项目内容\1. 岚图汽车具身智能化改造技术\FTP_Client",
    [switch]$WithTask,
    [switch]$NoShortcut
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " chassis_following FTP 压测客户端 - Windows 安装" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  源目录 : $ScriptDir"
Write-Host "  目标   : $TargetDir"
Write-Host ""

# ---------------------------------------------------------------------------
# 1) 检测 Python
# ---------------------------------------------------------------------------
function Find-Python {
    foreach ($cmd in @("py", "python", "python3")) {
        try {
            $exe = Get-Command $cmd -ErrorAction Stop
            # 验证能跑且版本 >= 3.8
            $ver = & $cmd -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver) {
                $parts = $ver.Split('.')
                if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 8) {
                    return [PSCustomObject]@{ Cmd = $cmd; Version = $ver }
                }
            }
        } catch { }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host "[ERROR] 未检测到 Python 3.8+。请先安装:" -ForegroundColor Red
    Write-Host "        winget install -e --id Python.Python.3.11" -ForegroundColor Yellow
    Write-Host "        或从 https://www.python.org/downloads/ 下载，安装时勾选 'Add to PATH'" -ForegroundColor Yellow
    exit 2
}
Write-Host "[INFO] Python 已就绪: $($py.Cmd) ($($py.Version))" -ForegroundColor Green

# ---------------------------------------------------------------------------
# 2) 创建目标目录并复制文件
# ---------------------------------------------------------------------------
if (-not (Test-Path $TargetDir)) {
    New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
    Write-Host "[INFO] 已创建目录: $TargetDir"
}

$files = @(
    "stress_test.py",
    "client_config.json",
    "run_download.bat",
    "run_upload.bat",
    "run_both.bat",
    "run_fetch.bat",
    "README_Windows.md",
    "install.ps1",
    "uninstall.ps1"
)
foreach ($f in $files) {
    $src = Join-Path $ScriptDir $f
    if (Test-Path $src) {
        Copy-Item -Path $src -Destination (Join-Path $TargetDir $f) -Force
        Write-Host "  ✓ $f"
    }
}

# 把探测到的 python 命令写入一个 env 文件，供 bat 使用
$pyCmdFile = Join-Path $TargetDir "python_cmd.txt"
$py.Cmd | Out-File -FilePath $pyCmdFile -Encoding ascii -NoNewline
Write-Host "[INFO] 已记录 Python 命令 -> python_cmd.txt ($($py.Cmd))"

# ---------------------------------------------------------------------------
# 3) 桌面快捷方式
# ---------------------------------------------------------------------------
if (-not $NoShortcut) {
    try {
        $desktop = [Environment]::GetFolderPath("Desktop")
        $ws = New-Object -ComObject WScript.Shell
        foreach ($item in @(
            @{ Name = "FTP压测-下行"; Bat = "run_download.bat" },
            @{ Name = "FTP压测-上行"; Bat = "run_upload.bat" },
            @{ Name = "FTP压测-混合"; Bat = "run_both.bat" },
            @{ Name = "FTP获取图片"; Bat = "run_fetch.bat" }
        )) {
            $lnk = $ws.CreateShortcut((Join-Path $desktop "$($item.Name).lnk"))
            $lnk.TargetPath = Join-Path $TargetDir $item.Bat
            $lnk.WorkingDirectory = $TargetDir
            $lnk.Save()
        }
        Write-Host "[INFO] 已在桌面创建 4 个快捷方式" -ForegroundColor Green
    } catch {
        Write-Host "[WARN] 创建桌面快捷方式失败: $_" -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------------------
# 4) 可选: 计划任务（定时压测）
# ---------------------------------------------------------------------------
if ($WithTask) {
    $taskName = "chassis_following_ftp_stress"
    $batPath = Join-Path $TargetDir "run_both.bat"
    try {
        # 删除旧任务
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

        $action = New-ScheduledTaskAction -Execute "cmd.exe" `
            -Argument "/c `"$batPath`"" -WorkingDirectory $TargetDir
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
            -RepetitionInterval (New-TimeSpan -Minutes 30)
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -Settings $settings -Description "chassis_following FTP 上下行定时压测" | Out-Null
        Write-Host "[INFO] 已注册计划任务 '$taskName'（每 30 分钟一次）" -ForegroundColor Green
        Write-Host "       查看: Get-ScheduledTask -TaskName $taskName" 
    } catch {
        Write-Host "[WARN] 注册计划任务失败（可能需要管理员权限）: $_" -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------------------
# 5) 冒烟测试
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "[INFO] 冒烟测试: stress_test.py --help"
Push-Location $TargetDir
try {
    & $py.Cmd "stress_test.py" "--help" | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  ✓ stress_test.py 可正常执行" -ForegroundColor Green
    } else {
        Write-Host "  ✗ 退出码 $LASTEXITCODE" -ForegroundColor Red
    }
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " 安装完成 ✅" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host " 后续操作:"
Write-Host "  1. 编辑配置: $TargetDir\client_config.json"
Write-Host "     确认 host 指向 FTP 服务端 (默认已填 172.16.8.200)"
Write-Host "  2. 双击桌面快捷方式，或运行:"
Write-Host "     $TargetDir\run_download.bat   (下行)"
Write-Host "     $TargetDir\run_upload.bat     (上行)"
Write-Host "     $TargetDir\run_both.bat       (混合)"
Write-Host "  3. 查看报告: $env:TEMP\ftp_stress_report.json"
Write-Host ""
