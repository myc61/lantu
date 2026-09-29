# 相机接口与 SSE 事件说明

> 本文档记录对 **PM Camera（SM9V1）** 相机 Web 服务的接口逆向结果，供 `live_monitor.py` / `alarm_receiver.py` 接入参考。
> 探测日期：2026-09-10　相机 IP：192.168.5.100

---

## 1. 网络与端口

| 端口 | 服务 | 说明 |
|---|---|---|
| **80** | nginx 1.23.4 | Web UI（SPA「PM Camera」）+ **API 反向代理** |
| **8000** | 后端 API | 平台信封 `{code,data,error_message,metrics.api_ms}` |
| 8080 | 无关服务 | 纯文本 404 |
| 9000 | 无关服务 | Python http.server，501 |

### 1.1 nginx 代理规则（关键）

前端 JS 调用 `/api/xxx`，nginx（80 端口）**剥离 `/api` 前缀**后转发到后端（8000 端口）：

```
浏览器/客户端 → http://192.168.5.100/api/gpio/do
                        │ nginx 剥离 /api
                        ▼
              http://192.168.5.100:8000/gpio/do
```

**验证**：
- `http://192.168.5.100/api/gpio/do` → 401 `Not authenticated`（端点存在）
- `http://192.168.5.100:8000/api/gpio/do` → 404（后端无 `/api` 前缀）
- `http://192.168.5.100:8000/gpio/do/1` → 405/401（后端真实路径）

> **结论**：外部访问统一走 **80 端口 + `/api` 前缀**；直连 8000 端口需去掉 `/api`。

---

## 2. 认证

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/auth/login` | POST | 登录，body 需 `username` + `password`，返回 Token |
| `/api/auth/me` | GET | 当前用户信息 |
| `/api/auth/renew-token` | POST | 刷新 Token |
| `/api/auth/change-password` | POST | 修改密码 |

- 鉴权方式：`Authorization: Bearer <token>`
- 未鉴权访问受保护端点 → 401 `{"detail":"Not authenticated"}`
- Token 错误 → 401 `{"code":"auth.002","error_message":"无效的令牌"}`
- **`/api/sse` 与 `/storage/*` 无需鉴权**（可直接订阅/下载）

> 注：`alarm_receiver.py` 中默认的 `CAMERA_TOKEN = "camera-token"` 对本相机无效，需通过 `/api/auth/login` 获取真实 Token。

---

## 3. SSE 实时事件流（获取报警的正确方式）

**端点**：`GET http://192.168.5.100/api/sse`
**鉴权**：无需
**协议**：Server-Sent Events（`text/event-stream`）
**首事件延时**：实测 47~74 ms

### 3.1 事件类型

| event | 频率 | 含义 |
|---|---|---|
| `new_record` | 报警时 | **报警记录**（核心，含证据图片/视频路径、报警原因） |
| `event_log` | 事件时 | 事件日志（人员进入/离开、DO 输出/复位） |
| `solution_entry_counts` | ~1/s | 方案状态（`areas_alarming` 是否报警中） |
| `gpo_values` | 变化时 | GPIO 输出通道实时值 |
| `system_status` | ~5/s | 系统资源（CPU/内存/磁盘/温度） |
| `system_liveness` | 启动时 | 组件在线状态 |
| `system_timestamp` | ~1/s | 相机 UTC 时间戳 |

### 3.2 `new_record` 数据结构（报警记录）

```json
{
  "id": 338,
  "timestamp": "2026-09-10T14:21:32.805000+08:00",
  "serial_number": "202609100338",
  "source": "person_detection",
  "alarm_reason": "area_entry",
  "capture_video_path": "/storage/records/2026-09/10/xxxx.mp4",
  "evidence_events": [
    {
      "id": 123,
      "timestamp": "2026-09-10T14:21:32.805000+08:00",
      "index": 0,
      "image_path": "/storage/records/2026-09/10/xxxx_0.jpeg",
      "event_type": "area_entry",
      "allow_add_to_whitelist": true
    },
    {
      "index": 1,
      "image_path": "/storage/records/2026-09/10/xxxx_1.jpeg",
      "event_type": "area_exit"
    }
  ],
  "pipeline": {
    "id": 2,
    "algorithm_type": "person_detection",
    "solution_id": 10000,
    "solution_name": "方案1",
    "algorithm_model_name": "safety-v5m",
    "parameters": { "detect_conf_thresh": 0.1, "alarm_areas": [...], "...": "..." }
  }
}
```

**字段要点**：
- `alarm_reason`：报警原因（`area_entry` 区域入侵 / `area_exit` 离开 / `line_triggered` 越线 等）
- `evidence_events[].image_path`：证据图片相对路径，拼接 `http://192.168.5.100` 即可下载
- `capture_video_path`：报警录像路径
- **同一 `id` 会多次推送**：证据链逐步累积时（先 entry 图、后 exit 图），相机会重发同一 record，客户端需按 `id` 去重

### 3.3 `event_log` 数据结构

```json
{"id": 413, "timestamp": "...", "level": "info", "type": "DO1复位", "description": "DO1: on"}
{"id": 414, "timestamp": "...", "level": "info", "type": "人员进入", "description": null}
```

常见 `type`：`人员进入`、`人员离开`、`DO1输出`、`DO1复位`。

### 3.4 `solution_entry_counts` 数据结构

```json
[{
  "solution_id": 10000, "solution_name": "方案1", "solution_status": "running",
  "areas_alarming": true, "pipeline_id": 2, "entry_count": 0
}]
```

`areas_alarming` 由 `false→true` 表示报警触发，`true→false` 表示解除。

### 3.5 `gpo_values` 数据结构

```json
[{"channel_number": 1, "value": 0, "mode": "normal"},
 {"channel_number": 2, "value": 0, "mode": "normal"}]
```

---

## 4. 证据文件下载

| 类型 | URL 模板 | 鉴权 |
|---|---|---|
| 图片 | `http://192.168.5.100/storage/records/<YYYY-MM>/<DD>/<hash>_<index>.jpeg` | 无 |
| 视频 | `http://192.168.5.100/storage/records/<YYYY-MM>/<DD>/<hash>.mp4` | 无 |

路径直接取自 `new_record` 的 `image_path` / `capture_video_path`，前缀拼相机 IP（80 端口）即可。

---

## 5. GPIO 反控（DO 输出）

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/gpio/do` | GET | 列出所有 DO 通道 |
| `/api/gpio/do/<channel>` | PATCH | 设置 DO 输出 |

**PATCH body**：
```json
{"name": "alarm-xxx", "mode": "manual", "value": 1, "related_solutions": []}
```
- `value`: 1=置高，0=复位
- 需 Bearer Token（登录获取）

> `alarm_receiver.py` 的 `_camera_patch()` 当前用 `/gpio/do/<ch>`（直连 8000 端口语法）。
> 若走 80 端口需改为 `/api/gpio/do/<ch>`，并配置 `CAMERA_BASE_URL = "http://192.168.5.100"`。

---

## 6. 其他已发现端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/solutions` | GET | 算法方案列表 |
| `/api/solutions/available-algorithms` | GET | 可用算法 |
| `/api/records/search` | POST | 搜索报警记录 |
| `/api/records/export` | POST | 导出记录 |
| `/api/models` | GET | 模型列表 |
| `/api/whitelist` | GET | 白名单 |
| `/api/system/logs` | GET | 系统日志 |
| `/api/system-logs/event` | GET | 事件日志 |
| `/api/system/networking` | GET | 网络配置 |
| `/api/system/time-settings` | GET | 时间设置 |
| `/api/live/rtc-offer/visible-light` | POST | 可见光 WebRTC 直播 |
| `/api/live/rtc-offer/infrared` | POST | 红外 WebRTC 直播 |
| `/api/webrtc/tracking/offer` | POST | 跟踪 WebRTC |

---

## 7. 相机「推送」功能的真相

前端 i18n 字符串显示，相机自带的「推送」仅支持：

- **飞书（Feishu）** 机器人 webhook
- **钉钉（DingTalk）** 机器人 webhook
- **微信（WeChat）** 机器人 webhook

配置项：`pushPlatform`（平台）、`pushAddress`（webhook 地址）、`pushTest`（测试推送）。

> **重要**：相机**不支持**推送报警到任意自定义 HTTP 服务器。
> 因此 `alarm_receiver.py`（等待相机 POST）无法直接收到本机相机的报警。
> **正确的实时获取方式是订阅 `/api/sse`**，即 `live_monitor.py` 的做法。

---

## 8. 硬件信息（实测）

| 项目 | 值 |
|---|---|
| 产品型号 | SM9V1 |
| 设备序列号 | PMA2632SSP0002940 |
| CPU | 6 核 |
| 内存 | 5.51 GB |
| 存储 | SD 59.5GB + MMC 29.1GB |
| 组件 | gpio / infrared-sensor / rgb-sensor（均 online） |
| 算法模型 | safety-v5m |
