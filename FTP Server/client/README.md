# chassis_following FTP 压测客户端

部署在**远端压测机**上的独立客户端。仅依赖 Python 3.8+ 标准库 (`ftplib` / `threading` / `statistics`)，无需 pip 安装任何第三方包。

## 目录结构

```
ftp-stress-client/
├── stress_test.py           # 压测主程序（上行/下行/混合）
├── client_config.json       # 默认配置（服务端地址、账号、并发数等）
├── run_download.sh          # 下行压测快捷入口
├── run_upload.sh            # 上行压测快捷入口
├── run_both.sh              # 上下行混合压测
├── install.sh               # 一键安装到 /opt/chassis_following/ftp-client
├── uninstall.sh             # 卸载
├── stress-client.service    # systemd 服务单元（占位符 __PREFIX__/__USER__）
├── stress-client.timer      # systemd 定时器（默认 30 分钟一次）
└── README.md
```

## 部署流程

### 方式 A：一键部署（推荐，本地执行）

在**本地**（服务端所在机器）执行：

```bash
cd "/home/naviai/chassis_following/FTP Server"
./deploy.sh --remote user@192.168.1.50           # 打包 + scp + ssh 安装
./deploy.sh --remote user@192.168.1.50 --with-systemd
```

### 方式 B：手动部署

1. 打包：
   ```bash
   cd "/home/naviai/chassis_following/FTP Server"
   ./pack_client.sh                # 生成 dist/ftp-stress-client-<日期>.tar.gz
   ```
2. 传输：
   ```bash
   scp dist/ftp-stress-client-*.tar.gz user@192.168.1.50:/tmp/
   ```
3. 远端安装：
   ```bash
   ssh user@192.168.1.50
   cd /tmp && tar -xzf ftp-stress-client-*.tar.gz && cd ftp-stress-client
   sudo ./install.sh                # 或 sudo ./install.sh --with-systemd
   ```

## 配置

编辑 `/opt/chassis_following/ftp-client/client_config.json`：

```json
{
  "host": "192.168.1.100",     // 服务端 IP（改成实际值）
  "port": 2121,
  "user": "stress",
  "password": "stress123",
  "concurrency": 4,             // 并发 worker 数
  "rounds": 3,                  // 压测轮次
  "files": 50,                  // 每轮文件数
  "download": { "remote_dir": "/", "pattern": "*.jpg" },
  "upload":   { "remote_dir": "/stress_test", "size": "1M", "payload": "random" },
  "out_json": "/tmp/ftp_stress_report.json"
}
```

> **命令行参数优先于配置文件**。

## 使用

### 按文件夹获取图片（fetch，核心用途）

`shots/` 下每个时间戳文件夹（如 `2026-09-04_11-18-02`）是一组图片。`fetch` 按文件夹拉取到本地，**保留目录结构、不删除**：

```bash
# 列出远端有哪些 session 文件夹（含图片数/大小）
ftp-fetch --list
# 或 run_fetch.sh --list

# 获取指定文件夹下所有图片
ftp-fetch 2026-09-04_11-18-02
# 或 run_fetch.sh 2026-09-04_11-18-02 --skip-existing

# 获取全部 session（不带参数），断点续传
ftp-fetch

# 自定义本地目录 / 并发
LOCAL_DIR=/data/shots CONCURRENCY=8 ftp-fetch 2026-09-04_11-18-02
```

本地结果保留 session 层级：`~/fetched_shots/2026-09-04_11-18-02/shot_01_*.jpg`（默认目录见配置 `fetch.local_dir`）。

### 通讯压测（download / upload / both）

```bash
# 下行压测（下载到临时目录后删除，反复测吞吐）
sudo -u $USER /opt/chassis_following/ftp-client/run_download.sh
CONCURRENCY=8 FILES=200 ROUNDS=5 ftp-stress-download

# 上行压测
SIZE=5M FILES=30 CONCURRENCY=8 ftp-stress-upload

# 混合上下行
ftp-stress-both

# 直接调用主程序（更灵活的参数）
python3 /opt/chassis_following/ftp-client/stress_test.py download \
    --host 10.0.0.5 --port 2121 --user stress --password stress123 \
    --concurrency 8 --files 100 --rounds 3 --out-json /tmp/report.json
```

## 报告输出

终端表格：

```
================================================================================
📋 压测汇总报告
================================================================================
轮次 类型      成功/总数     错误率     总流量   耗时(s)     Mbps     MB/s   P95(ms)
--------------------------------------------------------------------------------
   1 download     50/50      0.00%   375.00MB     3.412   880.32   110.04     82.5
   2 download     50/50      0.00%   375.00MB     3.398   883.90   110.49     81.9
--------------------------------------------------------------------------------
总计: 成功 100 / 失败 0 | 总流量 750.00MB | 总耗时 6.81s | 平均吞吐 882.11 Mbps
延迟(ms): min=45.2 p50=68.3 p95=82.5 p99=95.1 max=112.4
================================================================================
💾 JSON 报告已写入 /tmp/ftp_stress_report.json
```

JSON 报告可用于长期归档、绘制吞吐曲线。

## 定时压测（systemd timer）

安装时加 `--with-systemd`：

```bash
sudo ./install.sh --with-systemd
systemctl list-timers | grep stress-client
journalctl -u stress-client.service -f
```

默认每 30 分钟触发一次；修改 `/etc/systemd/system/stress-client.timer` 中的 `OnUnitActiveSec` 即可。

## 常见问题

**Q: 连接被拒绝 / 超时**  
A: 检查服务端是否启动、防火墙是否放行 2121/tcp 与 60000-60100/tcp（被动端口）。

**Q: 上传失败 550 Permission denied**  
A: 服务端需启用 `allow_upload`；`server_config.json` 中 `"allow_upload": true`，或启动时加 `--allow-upload`。

**Q: 主动/被动模式**  
A: 默认被动模式（PASV），跨 NAT 场景友好。如需主动模式，加 `--active`。

**Q: 并发上不去 / 吞吐低**  
A: 提高 `--concurrency`；同时确认服务端 `max_conns` 足够大、磁盘 IO 不是瓶颈。
