@echo off
REM ============================================================
REM  chassis_following FTP - 按文件夹获取图片到本地
REM
REM  用法 (在 cmd 中):
REM    run_fetch.bat                          获取全部 session
REM    run_fetch.bat 2026-09-04_11-18-02      只获取指定 session 文件夹
REM    run_fetch.bat --list                   列出有哪些 session 文件夹
REM    run_fetch.bat 2026-09-04_11-18-02 --skip-existing
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
chcp 65001 >nul

REM 读取 install.ps1 记录的 python 命令, 回退到 python
set "PYCMD=python"
if exist "python_cmd.txt" set /p PYCMD=<python_cmd.txt

REM --list: 列出远端 session 文件夹
if /I "%~1"=="--list" (
    "%PYCMD%" stress_test.py list --config client_config.json
    goto :end
)

REM 第一个参数不以 - 开头则视为 session 名
set "SESSIONARG="
set "FIRST=%~1"
if defined FIRST (
    echo !FIRST! | findstr /B /C:"-" >nul
    if errorlevel 1 (
        set "SESSIONARG=--session !FIRST!"
        shift
    )
)

echo [INFO] Python : %PYCMD%
echo [INFO] 开始获取图片...
echo.

"%PYCMD%" stress_test.py fetch --config client_config.json !SESSIONARG! %*

:end
echo.
echo ============================================================
echo  获取结束。图片保存在 client_config.json 的 fetch.local_dir
echo  (默认: %%USERPROFILE%%\Downloads\shots)
echo ============================================================
pause
endlocal
