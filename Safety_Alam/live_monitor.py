# -*- coding: utf-8 -*-
"""相机实时报警监控（SSE 客户端模式）。

直接订阅相机 Server-Sent Events 流，实时打印报警、下载证据图片。

运行：
    python live_monitor.py                              # 默认 192.168.5.100
    python live_monitor.py --host 192.168.5.100         # 指定相机 IP
    python live_monitor.py --save-images                # 下载证据图片到本地
    python live_monitor.py --no-color                   # 关闭彩色

相机 SSE 事件类型：
    new_record           报警记录（含证据图片路径、视频路径、报警原因）
    event_log            事件日志（人员进入/离开、DO 输出/复位）
    solution_entry_counts 方案状态（areas_alarming 报警中/正常）
    gpo_values           GPIO 输出通道状态
    system_status        系统资源（CPU/内存/磁盘/温度）
    system_liveness      组件在线状态
    system_timestamp     相机时间戳
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# ---------------- 配置 ----------------
DEFAULT_HOST = "192.168.5.100"
DEFAULT_PORT = 80  # nginx 代理端口，/api/sse 走这里
SSE_PATH = "/api/sse"
BASE_DIR = Path(__file__).resolve().parent
IMAGE_DIR = BASE_DIR / "alarms_live"
RECONNECT_DELAY = 3  # 断线重连间隔（秒）
MIN_IMAGE_INTERVAL = 60.0  # 图片下载最小间隔（秒），限流：默认每分钟最多 1 张

# ---------------- ANSI 彩色 ----------------
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"

USE_COLOR = True


def c(text, color):
    return f"{color}{text}{C.RESET}" if USE_COLOR else str(text)


def ts():
    now = datetime.now()
    return now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"


def log(msg, color=None):
    line = f"[{ts()}] {msg}"
    print(c(line, color) if color else line, flush=True)


# ---------------- SSE 解析 ----------------
def sse_events(url, timeout=300):
    """生成器：逐条产出 (event_type, data_str)。断线抛异常。"""
    req = urllib.request.Request(url, headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        event_type = None
        data_lines = []
        for raw_line in resp:
            line = raw_line.decode("utf-8", "replace").rstrip("\n").rstrip("\r")
            if line.startswith("event:"):
                event_type = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
            elif line == "":
                # 空行 = 事件结束
                if event_type and data_lines:
                    yield event_type, "\n".join(data_lines)
                event_type = None
                data_lines = []
            # 忽略 id: / retry: / 注释行


# ---------------- 图片下载 ----------------
def download_image(camera_base, image_path, save_dir, alarm_serial):
    """从相机下载证据图片，返回本地路径或 None。"""
    url = camera_base.rstrip("/") + "/" + image_path.lstrip("/")
    try:
        req = urllib.request.Request(url, headers={"Accept": "image/*"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        log(f"  {c('⚠ 图片下载失败', C.YELLOW)}: {image_path} ({e})", C.YELLOW)
        return None

    now = datetime.now()
    day_dir = save_dir / now.strftime("%Y%m%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%H%M%S.") + f"{now.microsecond // 1000:03d}"
    suffix = Path(image_path).suffix or ".jpg"
    local_name = f"{stamp}_{alarm_serial}{suffix}"
    local_path = day_dir / local_name
    local_path.write_bytes(data)
    return local_path


# ---------------- 事件处理 ----------------
class Monitor:
    def __init__(self, camera_base, save_dir, save_images=False, verbose=False,
                 min_image_interval=MIN_IMAGE_INTERVAL):
        self.camera_base = camera_base
        self.save_dir = save_dir
        self.save_images = save_images
        self.verbose = verbose
        self.min_image_interval = min_image_interval
        self.last_image_time = 0.0  # 上次成功下载图片的时间戳（用于限流）
        self.stats = {"new_record": 0, "event_log": 0, "alarms": 0,
                      "images_saved": 0, "images_skipped": 0}
        self.last_alarming = None  # 上次 areas_alarming 状态，用于检测变化
        if save_images:
            self.save_dir.mkdir(parents=True, exist_ok=True)

    def _can_download_image(self):
        """图片下载限流：距上次下载需 >= min_image_interval 秒才允许。
        interval <= 0 时关闭限流。返回 (是否允许, 还需等待秒数)。"""
        if self.min_image_interval <= 0:
            return True, 0.0
        now = time.time()
        elapsed = now - self.last_image_time
        if elapsed >= self.min_image_interval:
            self.last_image_time = now
            return True, 0.0
        return False, self.min_image_interval - elapsed

    def handle(self, event_type, data_str):
        try:
            data = json.loads(data_str)
        except ValueError:
            if self.verbose:
                log(f"  {c('? 非JSON', C.GRAY)} event={event_type} data={data_str[:100]}", C.GRAY)
            return

        handler = getattr(self, f"_on_{event_type}", None)
        if handler:
            handler(data)
        elif self.verbose:
            log(f"  {c('·', C.GRAY)} {event_type}: {json.dumps(data, ensure_ascii=False)[:120]}", C.GRAY)

    # ---- new_record: 报警记录 ----
    def _on_new_record(self, rec):
        self.stats["new_record"] += 1
        rid = rec.get("id", "?")
        serial = rec.get("serial_number", f"REC-{rid}")
        source = rec.get("source", "?")
        alarm_reason = rec.get("alarm_reason") or "无(记录更新)"
        timestamp = rec.get("timestamp", "")
        video_path = rec.get("capture_video_path", "")
        pipeline = rec.get("pipeline") or {}
        solution_name = pipeline.get("solution_name", "?")
        algo_type = pipeline.get("algorithm_type", "?")
        evidence = rec.get("evidence_events") or []

        # 报警级别颜色
        reason_colors = {
            "area_entry": C.RED, "line_triggered": C.RED, "blind_line_triggered": C.RED,
            "area_counter": C.YELLOW, "shelter": C.MAGENTA,
        }
        color = reason_colors.get(alarm_reason, C.CYAN)

        header = f"{c('🔔 ALARM', color)} #{serial}  {c(alarm_reason, color)}  [{source}]"
        lines = [
            f"record_id  : {rid}",
            f"时间       : {timestamp}",
            f"报警原因   : {c(alarm_reason, color)}",
            f"算法方案   : {solution_name} ({algo_type})",
            f"检测源     : {source}",
        ]
        if video_path:
            lines.append(f"视频       : {video_path}")
        if evidence:
            lines.append(f"证据图片   : {len(evidence)} 张")
            for ev in evidence:
                img_path = ev.get("image_path", "")
                ev_type = ev.get("event_type", "?")
                ev_ts = ev.get("timestamp", "")
                lines.append(f"  [{ev.get('index', '?')}] {ev_type}  {ev_ts}")
                lines.append(f"      {img_path}")
                if self.save_images and img_path:
                    allowed, wait = self._can_download_image()
                    if allowed:
                        local = download_image(self.camera_base, img_path, self.save_dir, serial)
                        if local:
                            self.stats["images_saved"] += 1
                            lines.append(f"      {c('→ 已保存: ' + str(local), C.GREEN)}")
                    else:
                        self.stats["images_skipped"] += 1
                        skip_msg = ("→ 跳过下载（限流 "
                                    + f"{self.min_image_interval:.0f}s/张，约 "
                                    + f"{wait:.0f}s 后可下载）")
                        lines.append(f"      {c(skip_msg, C.YELLOW)}")

        self._print_block(header, lines, color)
        self.stats["alarms"] += 1

    # ---- event_log: 事件日志 ----
    def _on_event_log(self, ev):
        self.stats["event_log"] += 1
        eid = ev.get("id", "?")
        etype = ev.get("type", "?")
        level = ev.get("level", "info")
        desc = ev.get("description") or ""
        timestamp = ev.get("timestamp", "")

        color_map = {"info": C.BLUE, "warning": C.YELLOW, "error": C.RED, "critical": C.RED}
        color = color_map.get(level, C.GRAY)
        tag = c(f"[{etype}]", color)
        msg = f"{c('📋 LOG', C.BLUE)} #{eid}  {tag}  {desc}" if desc else f"{c('📋 LOG', C.BLUE)} #{eid}  {tag}"
        if self.verbose:
            msg += c(f"  ({timestamp})", C.GRAY)
        log(msg, color)

    # ---- solution_entry_counts: 方案报警状态 ----
    def _on_solution_entry_counts(self, solutions):
        if not isinstance(solutions, list):
            return
        for sol in solutions:
            alarming = sol.get("areas_alarming", False)
            name = sol.get("solution_name", "?")
            sid = sol.get("solution_id", "?")
            # 只在状态变化时打印
            key = (sid, alarming)
            if self.last_alarming != key:
                self.last_alarming = key
                if alarming:
                    log(f"{c('⚠️  报警中', C.RED)} 方案 [{name}] (id={sid}) 区域报警触发！", C.RED)
                else:
                    log(f"{c('✅ 恢复正常', C.GREEN)} 方案 [{name}] (id={sid}) 区域报警解除", C.GREEN)

    # ---- gpo_values: GPIO 输出 ----
    def _on_gpo_values(self, channels):
        if not self.verbose or not isinstance(channels, list):
            return
        parts = [f"DO{ch.get('channel_number', '?')}={ch.get('value', '?')}({ch.get('mode', '?')})" for ch in channels]
        log(f"{c('⚡ GPIO', C.YELLOW)} {' | '.join(parts)}", C.YELLOW)

    # ---- system_status ----
    def _on_system_status(self, st):
        if not self.verbose:
            return
        usage = st.get("usage_stats", {})
        hw = st.get("hardware_stats", {})
        cpu = usage.get("cpu_used_cores", "?")
        cpu_total = usage.get("cpu_total_cores", "?")
        mem = usage.get("memory_used_gb", "?")
        mem_total = usage.get("memory_total_gb", "?")
        temp = hw.get("core_temperature", "?")
        uptime = hw.get("uptime_minutes", "?")
        log(f"{c('💻 SYS', C.GRAY)} CPU {cpu}/{cpu_total}  MEM {mem}/{mem_total}GB  TEMP {temp}°C  UP {uptime}min", C.GRAY)

    # ---- system_liveness ----
    def _on_system_liveness(self, components):
        if not self.verbose or not isinstance(components, list):
            return
        parts = [f"{comp.get('component', '?')}={comp.get('status', '?')}" for comp in components]
        log(f"{c('🔌 LIVE', C.GRAY)} {' | '.join(parts)}", C.GRAY)

    # ---- system_timestamp ----
    def _on_system_timestamp(self, data):
        pass  # 太频繁，忽略

    # ---- 打印工具 ----
    def _print_block(self, header, lines, color=None):
        stamp = c(f"[{ts()}]", C.GRAY)
        head = c(header, color) if color else header
        print(f"{stamp} {head}", flush=True)
        for ln in lines:
            print(f"           {ln}", flush=True)


# ---------------- 主循环 ----------------
def run_monitor(host, port, save_images, verbose, save_dir, min_image_interval=MIN_IMAGE_INTERVAL):
    camera_base = f"http://{host}:{port}"
    sse_url = f"{camera_base}{SSE_PATH}"
    monitor = Monitor(camera_base, save_dir, save_images, verbose, min_image_interval)

    banner = [
        c("═" * 68, C.CYAN),
        c("  Safety Sensor 实时报警监控（SSE 客户端模式）", C.BOLD + C.CYAN),
        c("═" * 68, C.CYAN),
        f"  相机地址 : {camera_base}",
        f"  SSE 流   : {sse_url}",
        f"  图片保存 : {'ON → ' + str(save_dir) if save_images else 'OFF（仅打印路径）'}",
        f"  下载限流 : {(f'每 {min_image_interval:.0f} 秒最多 1 张' if min_image_interval > 0 else '关闭') if save_images else '-'}",
        f"  详细模式 : {'ON' if verbose else 'OFF（--verbose 开启 GPIO/系统状态）'}",
        f"  彩色输出 : {'ON' if USE_COLOR else 'OFF'}",
        "",
        c("  订阅事件: new_record / event_log / solution_entry_counts / gpo_values", C.DIM),
        c("  等待相机报警中… (Ctrl+C 退出)", C.DIM),
        c("═" * 68, C.CYAN),
    ]
    for line in banner:
        print(line, flush=True)

    consecutive_failures = 0
    while True:
        try:
            log(f"{c('🔗 连接', C.CYAN)} 正在订阅 {sse_url} …", C.CYAN)
            t0 = time.time()
            event_count = 0
            for event_type, data_str in sse_events(sse_url):
                event_count += 1
                if event_count == 1:
                    latency = (time.time() - t0) * 1000
                    log(f"{c('✅ 已连接', C.GREEN)} SSE 流建立成功（首事件延时 {latency:.0f} ms）", C.GREEN)
                consecutive_failures = 0
                monitor.handle(event_type, data_str)
            # 流正常结束（相机关闭连接）
            log(f"{c('⚠ 流结束', C.YELLOW)} 相机关闭了 SSE 连接，{RECONNECT_DELAY}s 后重连…", C.YELLOW)
        except KeyboardInterrupt:
            raise
        except (urllib.error.URLError, OSError, TimeoutError, ConnectionError) as e:
            consecutive_failures += 1
            delay = min(RECONNECT_DELAY * consecutive_failures, 30)
            log(f"{c('❌ 连接失败', C.RED)} {e}  ({delay}s 后重试，连续失败 {consecutive_failures} 次)", C.RED)
            time.sleep(delay)
            continue
        except Exception as e:
            consecutive_failures += 1
            log(f"{c('❌ 异常', C.RED)} {type(e).__name__}: {e}", C.RED)
            time.sleep(RECONNECT_DELAY)
            continue

        time.sleep(RECONNECT_DELAY)


def main():
    global USE_COLOR
    ap = argparse.ArgumentParser(description="相机实时报警监控（SSE 客户端）")
    ap.add_argument("--host", default=DEFAULT_HOST, help=f"相机 IP（默认 {DEFAULT_HOST}）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"相机端口（默认 {DEFAULT_PORT}，nginx 代理）")
    ap.add_argument("--save-images", action="store_true", help="下载证据图片到本地")
    ap.add_argument("--image-dir", default=str(IMAGE_DIR), help=f"图片保存目录（默认 {IMAGE_DIR}）")
    ap.add_argument("--min-image-interval", type=float, default=MIN_IMAGE_INTERVAL,
                    help=f"图片下载最小间隔秒数（限流），默认 {MIN_IMAGE_INTERVAL:.0f}s 即每分钟最多 1 张；设 0 关闭限流")
    ap.add_argument("--verbose", "-v", action="store_true", help="显示 GPIO/系统状态等详细事件")
    ap.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    args = ap.parse_args()

    if args.no_color or not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        USE_COLOR = False
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    save_dir = Path(args.image_dir).resolve()

    # Ctrl+C 优雅退出
    def _sigint(sig, frame):
        print()
        log(f"{c('STOP', C.YELLOW)} 收到 Ctrl+C，退出监控", C.YELLOW)
        sys.exit(0)
    signal.signal(signal.SIGINT, _sigint)

    run_monitor(args.host, args.port, args.save_images, args.verbose, save_dir,
                args.min_image_interval)


if __name__ == "__main__":
    main()
