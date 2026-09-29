# chassis_following FTP 服务

将 `chassis_following/shots/` 目录以 FTP 服务方式对外发布，供远端压测机拉取图片（下行）与回传文件（上行），用于评估链路吞吐与稳定性。

## 目录结构

```
FTP Server/
├── ftp_server.py           # 服务端主程序 (pyftpdlib)
├── stress_test.py          # 客户端主程序 (仅依赖标准库)
├── server_config.json      # 服务端配置模板
├── requirements.txt        # 服务端依赖 (pyftpdlib)
├── run_server.sh           # 服务端快捷启动脚本
├── ftp-server.service      # 服务端 systemd unit
├── pack_client.sh          # 打包 Linux 客户端为独立 tar.gz
├── pack_client_windows.sh  # 打包 Windows 客户端为独立 zip
├── deploy.sh               # 一键部署 Linux 客户端到远端 (打包+scp+ssh+install)
├── README.md               # 本文件
├── client/                 # Linux 客户端部署包源目录
│   ├── client_config.json
│   ├── run_download.sh / run_upload.sh / run_both.sh
│   ├── install.sh / uninstall.sh
│   ├── stress-client.service / stress-client.timer
│   └── README.md
├── client_windows/         # Windows 客户端部署包源目录
│   ├── client_config.json
│   ├── run_download.bat / run_upload.bat / run_both.bat
│   ├── install.ps1 / uninstall.ps1
│   └── README_Windows.md
├── dist/                   # pack_client*.sh 生成的 tar.gz / zip
├── docker/                 # Docker 测试容器 (见「Docker 容器部署」)
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── server_config.docker.json  # 容器专用配置 (容器内绝对路径)
│   ├── healthcheck.py             # 健康检查: 登录+LIST+校验虚拟挂载点
│   ├── ftp_docker.sh              # 一键控制脚本 build/up/down/logs/test/shell
│   └── vendor/                    # 离线 pyftpdlib wheel
└── logs/                   # run_server.sh 生成的日志
```

## 快速开始

### 1. 启动服务端（本机 = 图片所在机器）

```bash
cd "/home/naviai/chassis_following/FTP Server"

# 首次运行会自动 pip install pyftpdlib
./run_server.sh

# 或手动:
python3 ftp_server.py --config server_config.json --log-file logs/ftp_server.log
```

启动横幅示例：

```
====================================================================
  chassis_following FTP Server 已启动
  监听地址 : ftp://0.0.0.0:2121
  局域网   : ftp://192.168.1.100:2121
  根目录   : /home/naviai/chassis_following/shots (80 个文件, 78.00 MB)
  被动端口 : 60000-60100
  上传权限 : 允许
  最大连接 : 64
====================================================================
```

### 2. 部署客户端到远端压测机

**一键部署**（推荐，本机执行）：

```bash
cd "/home/naviai/chassis_following/FTP Server"
./deploy.sh --remote stress@192.168.1.50 --sudo --with-systemd
```

**手动部署**：

```bash
./pack_client.sh                             # 生成 dist/ftp-stress-client-*.tar.gz
scp dist/ftp-stress-client-*.tar.gz user@REMOTE:/tmp/
ssh user@REMOTE 'cd /tmp && tar xzf ftp-stress-client-*.tar.gz && cd ftp-stress-client && sudo ./install.sh'
```

### 2b. 部署客户端到 Windows 压测机

Windows 端不能直接 ssh 部署，采用 **本机打包 → 远端 SSH 拉取 zip → 本地 install.ps1** 的三步流程。

**Step 1 · 本机打包**（本仓库所在 Linux 上执行）：

```bash
cd "/home/naviai/chassis_following/FTP Server"
./pack_client_windows.sh
# 产物: dist/ftp-stress-client-windows-YYYYmmdd-HHMM.zip
```

`pack_client_windows.sh` 会：
- 校验 `stress_test.py` + `client_windows/` 下 8 个必需文件；
- 统一转换为 **CRLF + UTF-8**（避免记事本 / PowerShell 乱码）；
- 使用 Python `zipfile` 打包（无需系统 `zip` 命令），并打印 SHA256 前 16 位便于校验。

**Step 2 · Windows 端通过 SSH 拉取**（在 Windows PowerShell 中执行）：

Windows 10/11 自带 OpenSSH 客户端，直接在 PowerShell 里 `scp` 即可：

```powershell
# 拉最新一个 zip 到 D:\Temp
mkdir D:\Temp -Force | Out-Null
scp "naviai@172.16.8.200:'/home/naviai/chassis_following/FTP Server/dist/ftp-stress-client-windows-*.zip'" D:\Temp\
```

> 通配符在远端 shell 展开，因此整个远端路径要用**单引号**包起来，本地路径用**双引号**（含空格时）。

也可以用 WinSCP / FileZilla / VS Code Remote-SSH 图形化下载 `dist/` 下的最新 zip。

**Step 3 · Windows 端安装**（管理员 PowerShell）：

```powershell
cd D:\Temp
Expand-Archive .\ftp-stress-client-windows-*.zip -DestinationPath . -Force
cd .\ftp-stress-client-windows

Set-ExecutionPolicy -Scope Process Bypass -Force
.\install.ps1                    # 默认安装到 D:\GTM\6. 项目内容\1. 岚图汽车具身智能化改造技术\FTP_Client
.\install.ps1 -WithTask          # 同时注册每 30 分钟一次的计划任务
.\install.ps1 -TargetDir D:\FTP_Client   # 自定义安装目录
```

`install.ps1` 会自动完成：Python 3.8+ 探测 → 复制文件 → 记录 `python_cmd.txt` → 桌面创建 3 个快捷方式（下行 / 上行 / 混合）→ 可选注册计划任务 → 冒烟测试 `stress_test.py --help`。

详细使用 / 配置 / 卸载 / FAQ 见 [`client_windows/README_Windows.md`](client_windows/README_Windows.md)。

### 3. 在远端执行压测

```bash
ssh user@REMOTE
sudo vim /opt/chassis_following/ftp-client/client_config.json   # 改 host 指向本机 IP

# 下行
/opt/chassis_following/ftp-client/run_download.sh
# 或 CONCURRENCY=8 FILES=100 ftp-stress-download

# 上行
SIZE=5M FILES=30 ftp-stress-upload

# 混合
ftp-stress-both
```

### 4. 防火墙

```bash
sudo ufw allow 2121/tcp
sudo ufw allow 60000:60100/tcp     # 被动模式数据端口
```

## 服务端配置

`server_config.json`：

| 字段 | 说明 | 默认 |
|---|---|---|
| `host` | 监听地址 | `0.0.0.0` |
| `port` | 监听端口 | `2121` |
| `root` | FTP 根目录（相对路径基于配置文件所在目录） | `../shots` |
| `anonymous` | 是否允许匿名访问 | `false` |
| `allow_upload` | 是否允许上传（上行压测必需） | `true` |
| `upload_root` | 独立可写上传目录（相对路径基于配置文件所在目录） | `./uploads` |
| `upload_mount` | 上传目录在 FTP 命名空间下的挂载路径 | `/stress_uploads` |
| `extra_mounts` | 额外挂载 `{"FTP路径": "本地目录"}`，让 root 之外的目录对所有账号可见 | `{"/Image_TEST": "./Image_TEST"}` |
| `max_conns` | 最大并发连接 | `64` |
| `stats_interval` | 流量汇总打印间隔（秒），0 关闭 | `15` |
| `users[]` | 用户列表 `{username, password, homedir}` | — |

命令行参数优先覆盖配置文件，例：

```bash
python3 ftp_server.py --config server_config.json --port 2122 --allow-upload
python3 ftp_server.py --anonymous --root /data/shots --port 21
```

完整优先级：**命令行显式传入 > 配置文件 > `ftp_server.py` 里的 `DEFAULTS` 内置默认**。

几个要点：

- 只有**显式传入**的参数才会覆盖配置文件。实现上这些参数的 argparse `default` 全是 `None`，
  `None` 即代表「没传」，不参与覆盖；启动时日志会打一行 `命令行覆盖配置项: port, ...` 方便确认。
- `--anonymous` / `--allow-upload` 是 `store_true`，命令行只能把它们从 `false` 开到 `true`，
  **无法反向关闭**配置文件里的 `true`。要关就直接改配置文件。
- `--user user:pass` 是**追加**语义，不会覆盖配置文件里的 `users` 列表。
- `--log-file` / `--log-level` / `--config` 不参与合并，始终由命令行决定。

### Image_TEST 目录访问

`Image_TEST/` 不在 FTP root（`shots/`）内，通过 `extra_mounts` 挂载到 FTP 命名空间的 `/Image_TEST`，两种访问方式：

| 账号 | 密码 | 访问路径 | 说明 |
|---|---|---|---|
| `stress` / `viewer` | 同原配置 | `/Image_TEST` | 根目录为 shots，`LIST /` 可见 `Image_TEST` 虚拟目录 |
| `imagetest` | `imagetest123` | `/`（homedir 即 Image_TEST） | 登录后直接位于 Image_TEST，图片平铺在根目录 |

```bash
# stress 账号压测 Image_TEST 大图 (25 张 × ≈23MB PNG)
python3 stress_test.py download --host 172.16.8.200 --port 2121 \
    --user stress --password stress123 \
    --remote-dir /Image_TEST --pattern '*.png' --concurrency 4 --files 20

# imagetest 账号以根目录访问
python3 stress_test.py download --host 172.16.8.200 --port 2121 \
    --user imagetest --password imagetest123 \
    --remote-dir / --pattern '*.png' --concurrency 4 --files 20
```

## 服务端 systemd 部署

```bash
sudo mkdir -p /opt/chassis_following/ftp-server /var/log/chassis_following
sudo cp ftp_server.py server_config.json requirements.txt \
        /opt/chassis_following/ftp-server/
sudo pip3 install -r /opt/chassis_following/ftp-server/requirements.txt
sudo cp ftp-server.service /etc/systemd/system/
# 按需修改 ftp-server.service 里的 User / WorkingDirectory
sudo systemctl daemon-reload
sudo systemctl enable --now ftp-server.service
sudo systemctl status ftp-server.service
journalctl -u ftp-server.service -f
```

## Docker 容器部署

用于把 FTP 服务跑在隔离容器里做测试，不影响宿主机环境。

```bash
cd docker/
./ftp_docker.sh build     # 构建镜像 naviai-ftp-test:local
./ftp_docker.sh up        # 启动并等待健康检查通过
./ftp_docker.sh test      # 端到端自测: LIST / 下载 / 上传
./ftp_docker.sh status    # 状态 + 端口 + 健康检查详情 + 挂载点
./ftp_docker.sh logs -f   # 跟随日志
./ftp_docker.sh shell     # 进容器 bash
./ftp_docker.sh restart   # 改了宿主机代码/配置后重启生效
./ftp_docker.sh down      # 停止并移除
```

容器名 `naviai_ftp_test`，连接方式与直接跑 `run_server.sh` 完全一致
（`ftp://<宿主机IP>:2121`，账号 `stress/viewer/imagetest`）。

### 几个关键设计

**基础镜像** — 本机 Docker Hub 不可达，无法 `pull python:3.11-slim`，因此复用内部仓库
已缓存的 `navi_project/demos:v1.0.2`（Ubuntu 20.04 + Python 3.8.10，本地镜像里体积最小
且自带可用 pip 的一个）。可用 `BASE_IMAGE=xxx ./ftp_docker.sh build` 换。

**pyftpdlib 离线安装** — `docker/vendor/` 下放了宿主机预生成的
`pyftpdlib-2.1.0-py3-none-any.whl`，构建时 `pip install --no-index` 离线装，
不依赖容器内网络，也保证版本与宿主机运行环境**严格一致**。
这点很重要：`MountableFS` 对 `format_list` / `on_file_sent` 的重写依赖 2.1.0 的具体行为，
换版本可能让虚拟挂载点在 `LIST` 中重新消失。
重新生成 wheel：`python3 -m pip wheel --no-deps -w docker/vendor "pyftpdlib==2.1.0"`。

**network_mode: host** — FTP 被动模式的数据通道要在 60000-60100 里动态开端口。
bridge 模式下要么逐个映射 101 个端口，要么 PASV 响应回的是容器内网 IP，
客户端连不上数据通道。host 模式一步到位，也不需要配 `masquerade_address`。
**代价**：2121 端口与宿主机直接跑 `run_server.sh` 互斥，二者只能起一个
（`up` 会先检查端口占用并拒绝启动）。

**降权运行** — 容器以 `FTP_UID:FTP_GID`（默认取当前用户，即 1000:1000）运行而非 root。
否则上传到 `/stress_uploads` 的文件落到宿主机 `uploads/` 会变成 `root:root`，
自己的账号都删不掉。可行性：`shots/` 虽属 root 但全局可读，`uploads/`、`logs/`、
`Image_TEST/` 本来就属当前用户，端口 2121 > 1024 不需特权。

**挂载布局** — 代码只读挂载、数据分层挂载：

| 宿主机 | 容器内 | 权限 |
| --- | --- | --- |
| `FTP Server/` | `/app/ftp` | ro（改代码后 `restart` 即生效，无需重新构建） |
| `chassis_following/shots/` | `/app/shots` | ro |
| `FTP Server/uploads/` | `/app/ftp/uploads` | rw（嵌套挂载覆盖上面的 ro） |
| `FTP Server/logs/` | `/app/ftp/logs` | rw（写 `ftp_server_docker.log`，与宿主机日志分开） |

### 健康检查

不是简单探端口，而是完整跑一遍 `连接 → 登录 → LIST / → 校验虚拟挂载点可见`。
最后一步盯的是历史故障点：虚拟挂载点物理上不在 homedir 下，
pyftpdlib 的 `format_list` 会对每个条目 `lstat` 并**静默丢弃**失败项，
所以「能登录能下载」并不代表 `LIST` 正常。
端口、账号、期望挂载点全部从配置文件反推，改了配置无需同步改脚本。

### ⚠️ 不要直接敲 docker compose

本机 shell 全局导出了 `COMPOSE_PROJECT_NAME=navi_project`（机器人栈用的）。
实测在 compose v5.1.4 下**这个环境变量的优先级高于 compose 文件里的 top-level `name:`**，
直接跑 `docker compose` 会把本容器并进 `navi_project` 项目 ——
届时一句 `docker compose down --remove-orphans` 就能把整套机器人容器带走。
`ftp_docker.sh` 内部固定传 `-p naviai-ftp-test`（`-p` 优先级最高）来隔离。
若一定要手敲，请自行带上：`docker compose -p naviai-ftp-test ...`。

另外本机 `~/.docker` 属 `root:root` 且 755，当前用户无法在其中创建 `buildx/`，
裸跑 `docker compose build` 会失败在 `permission denied`。
脚本检测到这种情况会自动改用 `DOCKER_CONFIG=~/.cache/docker-config`，
并把原 `config.json` 拷过去（里面存着内部仓库的 registry 认证，丢了拉不到基础镜像）。

## 上传挂载点机制

`shots/` 目录可能由 root 所有（capture.py 以 root 运行），FTP 服务以普通用户运行时无法写入。服务端通过 **MountableFS** 自动把一个独立可写目录挂载到 FTP 命名空间：

```
FTP 视角:                实际文件系统:
/stress_uploads/  ───►   FTP Server/uploads/
/2026-09-08_xxx/  ───►   shots/2026-09-08_xxx/  (只读)
```

客户端上行压测时指定 `--remote-dir /stress_uploads` 即可，服务端会自动创建 `uploads/` 目录。

## 流量统计

服务端每 `stats_interval` 秒输出累计统计：

```
📊 累计: 会话=3 下行=1125.32MB(842.11Mbps) 上行=15.00MB(11.22Mbps) 错误=0
```

进程退出时打印最终汇总，便于事后审计。

## 客户端使用要点

详见 [`client/README.md`](client/README.md)。客户端共 5 个子命令：`list` / `fetch` / `download` / `upload` / `both`。

### 按文件夹获取图片（fetch，核心用途）

`shots/` 下每个时间戳文件夹（如 `2026-09-04_11-18-02`）是一组图片。`fetch` 按文件夹拉取到本地，**保留目录结构、不删除**（区别于压测用的 `download`）：

```bash
# 1) 先列出远端有哪些 session 文件夹（含图片数/大小）
python3 stress_test.py list --host 172.16.8.200 --port 2121 --user stress --password stress123

# 2) 获取指定文件夹下所有图片
python3 stress_test.py fetch --host 172.16.8.200 --port 2121 \
    --user stress --password stress123 \
    --session 2026-09-04_11-18-02 --local-dir ~/fetched_shots

# 3) 获取全部 session（不带 --session），断点续传跳过已下载
python3 stress_test.py fetch --config client_config.json --skip-existing
```

本地结果保留 session 层级：`~/fetched_shots/2026-09-04_11-18-02/shot_01_*.jpg`。

> 快捷脚本：`run_fetch.sh --list` 列目录；`run_fetch.sh <session名>` 获取指定文件夹；`run_fetch.sh` 获取全部。

### 通讯压测（download / upload / both）

```bash
# 下行压测（下载到临时目录后删除，反复测吞吐）
python3 stress_test.py download \
    --host 172.16.8.200 --port 2121 \
    --user stress --password stress123 \
    --concurrency 8 --files 100 --rounds 3 \
    --pattern '*.jpg' --out-json /tmp/report.json
```

输出表格：轮次 / 成功数 / 错误率 / 总流量 / 耗时 / Mbps / MB/s / P95 延迟。同时写入 JSON 报告便于归档。

## 常见问题

**Q: 客户端连接超时**  
A: 服务端是否监听 `0.0.0.0`？防火墙是否放行 2121 与 60000-60100/tcp？

**Q: 上传返回 550**  
A: 服务端 `allow_upload` 必须为 `true`；用户权限字符串包含 `w`（写）与 `M`（建目录）。

**Q: 下行速度远低于带宽**  
A: 提高 `--concurrency`；检查服务端磁盘 IO；确认走的是千兆/万兆网卡而非 WiFi。

**Q: 被动模式失败**  
A: 服务端 NAT 后需配置 `masquerade_address`（pyftpdlib 支持），或改用主动模式 `--active`（客户端需可被服务端反向连接）。

## 安全提示

- `server_config.json` 中的密码为明文，生产环境请通过 `--user user:pass` 临时传入，或改为环境变量注入；
- 压测结束建议关闭服务或仅保留只读匿名；
- `shots/` 目录可能含定位/位姿敏感信息，对外发布前请评估数据合规性。
