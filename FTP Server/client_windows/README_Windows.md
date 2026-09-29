# chassis_following FTP 压测客户端 (Windows)

部署在 **Windows 远端压测机** 上的客户端。仅依赖 Python 3.8+ 标准库，无需 pip 安装任何第三方包。

## 目标机器信息

| 项 | 值 |
|---|---|
| 远端 IP | `172.16.9.173` |
| 安装目录 | `D:\GTM\6. 项目内容\1. 岚图汽车具身智能化改造技术\FTP_Client` |
| FTP 服务端 | `172.16.8.200:2121`（已预填进 `client_config.json`） |

## 前置条件

- Windows 已安装 **Python 3.8+**，且 `py` 或 `python` 在 PATH 中。
  - 检测：打开 PowerShell 执行 `py --version` 或 `python --version`
  - 未安装：`winget install -e --id Python.Python.3.11`（安装时勾选 **Add python.exe to PATH**）

## 安装步骤

### 1. 解压部署包

将 `ftp-stress-client-windows-*.zip` 解压到任意临时目录，例如 `D:\Temp\ftp-stress-client-windows`。

### 2. 运行安装脚本

以**管理员身份**打开 PowerShell，进入解压目录后执行：

```powershell
cd "D:\Temp\ftp-stress-client-windows"

# 允许本次会话运行脚本
Set-ExecutionPolicy -Scope Process Bypass -Force

# 安装到默认目标目录
.\install.ps1

# 或指定其它目录 / 同时注册定时任务
.\install.ps1 -TargetDir "D:\FTP_Client"
.\install.ps1 -WithTask
```

安装脚本会自动：
1. 检测 Python 环境；
2. 复制文件到目标目录；
3. 在桌面创建 3 个快捷方式（下行 / 上行 / 混合）；
4. 可选注册计划任务（`-WithTask`，每 30 分钟一次）；
5. 冒烟测试 `stress_test.py --help`。

## 使用

### 按文件夹获取图片（fetch，核心用途）

`shots/` 下每个时间戳文件夹（如 `2026-09-04_11-18-02`）是一组图片。双击桌面「**FTP获取图片**」快捷方式，或在 cmd 中：

```bat
cd "D:\GTM\6. 项目内容\1. 岚图汽车具身智能化改造技术\FTP_Client"

REM 列出远端有哪些 session 文件夹（含图片数/大小）
run_fetch.bat --list

REM 获取指定文件夹下所有图片
run_fetch.bat 2026-09-04_11-18-02

REM 获取全部 session（不带参数），断点续传跳过已下载
run_fetch.bat

REM 获取指定文件夹 + 跳过已存在
run_fetch.bat 2026-09-04_11-18-02 --skip-existing
```

图片保存到 `%USERPROFILE%\Downloads\shots\<session名>\`（可在 `client_config.json` 的 `fetch.local_dir` 修改），**保留 session 文件夹层级、不删除**。

### 通讯压测（download / upload / both）

安装后，直接**双击桌面快捷方式**，或运行：

```
D:\GTM\6. 项目内容\1. 岚图汽车具身智能化改造技术\FTP_Client\run_download.bat   下行
D:\GTM\6. 项目内容\1. 岚图汽车具身智能化改造技术\FTP_Client\run_upload.bat     上行
D:\GTM\6. 项目内容\1. 岚图汽车具身智能化改造技术\FTP_Client\run_both.bat       混合
```

带参数覆盖（在 cmd 中）：

```bat
run_download.bat --concurrency 8 --files 100 --rounds 5
run_upload.bat   --size 5M --files 30 --concurrency 8
```

## 配置

编辑目标目录下的 `client_config.json`：

```json
{
  "host": "172.16.8.200",        // FTP 服务端 IP（本机）
  "port": 2121,
  "user": "stress",
  "password": "stress123",
  "concurrency": 4,               // 并发线程数
  "rounds": 3,                    // 压测轮次
  "files": 50,                    // 每轮文件数
  "download": { "remote_dir": "/", "pattern": "*.jpg" },
  "upload":   { "remote_dir": "/stress_uploads", "size": "1M" },
  "both":     { "remote_dir": "/stress_uploads", "size": "1M" },
  "fetch":    { "remote_dir": "/", "pattern": "*.jpg",
                "local_dir": "%USERPROFILE%\\Downloads\\shots",
                "skip_existing": true },
  "out_json": "%TEMP%\\ftp_stress_report.json"
}
```

> 命令行参数优先于配置文件；`%TEMP%`、`%USERPROFILE%` 等环境变量会自动展开。

## 报告

- 终端表格：轮次 / 成功数 / 错误率 / 总流量 / 耗时 / Mbps / MB/s / P95 延迟
- JSON 报告：`%TEMP%\ftp_stress_report.json`

## 卸载

```powershell
cd "D:\GTM\6. 项目内容\1. 岚图汽车具身智能化改造技术\FTP_Client"
Set-ExecutionPolicy -Scope Process Bypass -Force
.\uninstall.ps1
```

会移除计划任务、桌面快捷方式与安装目录。

## 常见问题

**Q: 双击 bat 一闪而过**  
A: bat 末尾有 `pause`，正常会停留。若失败，请在 cmd 中手动运行以查看报错。

**Q: 提示 'python' 不是内部或外部命令**  
A: Python 未加入 PATH。重装 Python 勾选 "Add to PATH"，或用 `py` 启动器。install.ps1 会自动探测并把可用命令写入 `python_cmd.txt`。

**Q: 连接服务端超时**  
A: 确认本机 FTP 服务已启动、Windows 防火墙放行、且能 ping 通 `172.16.8.200`。被动模式还需放行服务端 `60000-60100/tcp`。

**Q: 上传报 550**  
A: 服务端需启用 `allow_upload`，上传目录固定为挂载点 `/stress_uploads`。

**Q: 中文乱码**  
A: bat 已执行 `chcp 65001` 切换 UTF-8；若 PowerShell 仍乱码，执行 `chcp 65001` 后重试。
