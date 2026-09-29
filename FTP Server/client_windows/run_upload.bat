@echo off
REM ============================================================
REM  chassis_following FTP 压测 - 上行 (upload)
REM  双击运行，或传参覆盖:
REM    run_upload.bat --size 5M --files 30 --concurrency 8
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
chcp 65001 >nul

set "PYCMD=python"
if exist "python_cmd.txt" set /p PYCMD=<python_cmd.txt

echo [INFO] 使用 Python: %PYCMD%
echo [INFO] 开始上行压测...
echo.

"%PYCMD%" stress_test.py upload --config client_config.json %*

echo.
echo ============================================================
echo  压测结束。报告: %TEMP%\ftp_stress_report.json
echo ============================================================
pause
endlocal
