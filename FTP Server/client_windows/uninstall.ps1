<#
.SYNOPSIS
    chassis_following FTP 压测客户端 —— Windows 卸载脚本
.EXAMPLE
    .\uninstall.ps1
    .\uninstall.ps1 -TargetDir "D:\FTP_Client"
#>
[CmdletBinding()]
param(
    [string]$TargetDir = "D:\GTM\6. 项目内容\1. 岚图汽车具身智能化改造技术\FTP_Client"
)

$ErrorActionPreference = "SilentlyContinue"

Write-Host "[INFO] 卸载目录: $TargetDir"

# 1) 删除计划任务
$taskName = "chassis_following_ftp_stress"
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "[INFO] 已移除计划任务 '$taskName'"
}

# 2) 删除桌面快捷方式
$desktop = [Environment]::GetFolderPath("Desktop")
foreach ($name in @("FTP压测-下行", "FTP压测-上行", "FTP压测-混合", "FTP获取图片")) {
    $lnk = Join-Path $desktop "$name.lnk"
    if (Test-Path $lnk) {
        Remove-Item $lnk -Force
        Write-Host "[INFO] 已删除快捷方式: $name"
    }
}

# 3) 删除安装目录
if (Test-Path $TargetDir) {
    Remove-Item $TargetDir -Recurse -Force
    Write-Host "[INFO] 已删除目录: $TargetDir"
}

Write-Host "[OK] 卸载完成" -ForegroundColor Green
