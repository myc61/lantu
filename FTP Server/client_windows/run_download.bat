@echo off
REM ============================================================
REM  chassis_following FTP 压测 - 下行 (download)
REM  双击运行，或在命令行传参覆盖:
REM    run_download.bat --concurrency 8 --files 100
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
chcp 65001 >nul

REM 读取 install.ps1 记录的 python 命令，回退到 py / python
set "PYCMD=python"
if exist "python_cmd.txt" set /p PYCMD=<python_cmd.txt

echo [INFO] 使用 Python: %PYCMD%
echo [INFO] 开始下行压测...
echo.

"%PYCMD%" stress_test.py download --config client_config.json %*

echo.
echo ============================================================
echo  压测结束。报告: %TEMP%\ftp_stress_report.json
echo ============================================================
pause
endlocal
