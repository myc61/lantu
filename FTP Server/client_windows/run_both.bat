@echo off
REM ============================================================
REM  chassis_following FTP 压测 - 上下行混合 (both)
REM  先上传测试文件，再从服务端回传下载，双向校验链路
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
chcp 65001 >nul

set "PYCMD=python"
if exist "python_cmd.txt" set /p PYCMD=<python_cmd.txt

echo [INFO] 使用 Python: %PYCMD%
echo [INFO] 开始上下行混合压测...
echo.

"%PYCMD%" stress_test.py both --config client_config.json %*

echo.
echo ============================================================
echo  压测结束。报告: %TEMP%\ftp_stress_report.json
echo ============================================================
pause
endlocal
