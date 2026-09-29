# Safety Sensor 相机报警系统

针对 **PM Camera（SM9V1）** 安全传感器的报警接收、实时监控、通讯延时压测工具集。
纯 Python 标准库实现，无第三方依赖。

---

## 一、项目结构

```
Safety_Alam/
├── alarm_receiver.py        # HTTP 报警接收服务（持久化 + 反控联动）
├── live_monitor.py          # 实时报警监控（SSE 客户端，订阅相机事件流）
├── bench_camera.py          # 与相机的通讯延时压测
├── bench_alarm.py           # 本地接收服务耗时压测
├── test_alarm_receiver.py   # 接收服务功能自测（20 项）
└── DOC/
    ├── 相机通讯延时测试报告.md      # 通讯延时压测结果
    ├── 相机接口与SSE事件说明.md      # 相机 API / SSE 事件逆向文档
    └── 报警接收测试报告.md          # 实时报警接收测试报告
```

---

## 二、脚本用途

| 脚本 | 作用 | 运行方式 |
|---|---|---|
| [`live_monitor.py`](live_monitor.py) | **实时监控**：订阅相机 SSE 流，报警一触发就打印，可选下载证据图片（默认限流每分钟 1 张） | `python3 live_monitor.py --save-images` |
| [`alarm_receiver.py`](alarm_receiver.py) | **接收服务**：HTTP 接口接收报警/证据/图片，JSON 持久化，高级别报警反控相机 DO 输出 | `python3 alarm_receiver.py 8000` |
| [`bench_camera.py`](bench_camera.py) | **延时压测**：ping / TCP / HTTP 分层测量与相机的通讯延时 | `python3 bench_camera.py` |
| [`bench_alarm.py`](bench_alarm.py) | **本地压测**：接收服务串行 + 并发耗时（RTT / api_ms） | `python3 bench_alarm.py` |
| [`test_alarm_receiver.py`](test_alarm_receiver.py) | **功能自测**：鉴权、幂等、证据链、图片去重、查询等 20 项 | `python3 test_alarm_receiver.py` |

---

## 三、快速开始

### 1. 实时监控相机报警（最常用）

```bash
# 实时打印报警 + 下载证据图片（默认每分钟最多 1 张，防止刷屏/占盘）
python3 live_monitor.py --save-images

# 显示 GPIO / 系统状态等详细事件
python3 live_monitor.py --save-images --verbose

# 自定义相机 IP、关闭图片下载限流
python3 live_monitor.py --host 192.168.5.100 --save-images --min-image-interval 0
```

输出示例：

```
[14:19:03.071] 🔔 ALARM #202609100338  area_entry  [person_detection]
           record_id  : 338
           时间       : 2026-09-10T14:21:32.805000+08:00
           报警原因   : area_entry
           算法方案   : 方案1 (person_detection)
           证据图片   : 2 张
             [0] area_entry  → 已保存: alarms_live/20260910/141903.015_...338.jpeg
             [1] area_exit   → 跳过下载（限流 60s/张，约 58s 后可下载）
```

### 2. 启动 HTTP 接收服务（供相机 POST 推送 / 反控联动）

```bash
python3 alarm_receiver.py 8000
```

### 3. 测试与压测

```bash
python3 test_alarm_receiver.py   # 功能自测（约 2 秒）
python3 bench_alarm.py           # 本地压测（约 5 秒）
python3 bench_camera.py          # 相机通讯延时压测
```

---

## 四、相机接入要点（重要）

> 详细逆向文档见 [DOC/相机接口与SSE事件说明.md](DOC/相机接口与SSE事件说明.md)

| 项目 | 值 |
|---|---|
| 相机型号 | PM Camera SM9V1 |
| Web UI | `http://192.168.5.100`（nginx SPA） |
| API 端口 | `8000`（后端），`80`（nginx 代理，`/api/*` 剥离前缀转发到后端） |
| **实时报警获取** | **订阅 SSE：`http://192.168.5.100/api/sse`（无需鉴权）** |
| 证据图片下载 | `http://192.168.5.100/storage/records/<路径>` |
| GPIO 反控 | `PATCH /api/gpio/do/<通道>`（需登录 Token） |

**关键结论**：该相机的「推送」功能仅支持飞书/钉钉/微信 webhook，**不会主动 HTTP POST 报警到自定义服务器**。
要实时获取报警，正确方式是 **订阅相机的 SSE 事件流**（`live_monitor.py` 已实现）。

### SSE 主要事件类型

| 事件 | 含义 |
|---|---|
| `new_record` | 报警记录（含证据图片路径、视频路径、报警原因） |
| `event_log` | 事件日志（人员进入/离开、DO 输出/复位） |
| `solution_entry_counts` | 方案状态（`areas_alarming` 报警中/正常） |
| `gpo_values` | GPIO 输出通道实时值 |
| `system_status` | 系统资源（CPU/内存/温度/运行时间） |
| `system_liveness` | 组件在线状态 |

---

## 五、环境

- Python 3.10+（仅标准库）
- Linux / Windows 均可
- 与相机同一网段（相机默认 `192.168.5.100`）
