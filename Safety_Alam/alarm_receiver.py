# -*- coding: utf-8 -*-
"""Safety Sensor 相机报警接收服务（单文件、仅标准库）。

运行：python alarm_receiver.py [端口]        （默认 8000）
自测：python test_alarm_receiver.py

功能（约定与相机平台文档截图一致：Bearer 鉴权 + code/error_message/data/metrics 信封）：
  POST /api/v1/alarms                    接收报警结果（JSON，alarm_no 幂等）
  POST /api/v1/alarms/<no>/evidence      接收证据链（JSON items 列表）
  POST /api/v1/alarms/<no>/images        接收图片（multipart 多文件，sha256 去重落盘）
  GET  /api/v1/alarms                    报警列表（?camera_id=&level=）
  GET  /api/v1/alarms/<no>               报警详情（含证据链）
  GET  /api/v1/evidence/<id>/file        证据文件取回
  GET  /health                           健康检查（免鉴权）

可选反控联动：配置 CAMERA_BASE_URL 后，level >= LINKAGE_LEVEL 的报警入库时
自动向相机下发 DO 置高（PATCH /gpio/do/<通道>），DO_HOLD_SECONDS 后复位为 0。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------- 配置 ----------------
HOST = "0.0.0.0"
PORT = 8000
TOKEN = "safety-sensor-token"          # 平台侧 Bearer Token
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
IMAGE_DIR = DATA_DIR / "images"

# 可选：相机反控联动（留空则关闭）
CAMERA_BASE_URL = ""                   # 例如 "http://127.0.0.1:4523"
CAMERA_TOKEN = "camera-token"
LINKAGE_LEVEL = 3                      # 报警级别 >= 该值触发 DO 输出
DO_CHANNEL = 1
DO_HOLD_SECONDS = 5

LOCK = threading.Lock()


# ---------------- 存储（JSON 文件） ----------------
class Store:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "alarms.json"
        self.image_dir = data_dir / "images"
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.db = {"alarms": [], "next_alarm_id": 1, "next_evidence_id": 1}
        if self.path.exists():
            try:
                self.db = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.db, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    # 调用方需持有 LOCK
    def find_alarm(self, alarm_no):
        for a in self.db["alarms"]:
            if a["alarm_no"] == alarm_no:
                return a
        return None

    def find_evidence(self, evidence_id):
        for a in self.db["alarms"]:
            for ev in a["evidence"]:
                if ev["id"] == evidence_id:
                    return ev
        return None


# ---------------- 可选反控联动 ----------------
def _camera_patch(path, body):
    req = urllib.request.Request(
        CAMERA_BASE_URL + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {CAMERA_TOKEN}", "Content-Type": "application/json"},
        method="PATCH",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status


def trigger_linkage(alarm):
    """level 达标时 DO 置高，hold 到期复位；失败仅打印不影响接收主流程。"""
    if not CAMERA_BASE_URL or alarm.get("level", 1) < LINKAGE_LEVEL:
        return

    def _run():
        body = {
            "name": f"alarm-{alarm['alarm_no']}",
            "mode": "manual",
            "value": 1,
            "related_solutions": [],
        }
        try:
            _camera_patch(f"/gpio/do/{DO_CHANNEL}", body)
            print(f"[linkage] DO{DO_CHANNEL} -> 1 (alarm {alarm['alarm_no']})")
            time.sleep(DO_HOLD_SECONDS)
            body["value"] = 0
            _camera_patch(f"/gpio/do/{DO_CHANNEL}", body)
            print(f"[linkage] DO{DO_CHANNEL} -> 0 (reset)")
        except Exception as exc:
            print(f"[linkage] failed: {exc}")

    threading.Thread(target=_run, daemon=True).start()


# ---------------- multipart 解析（标准库） ----------------
def parse_multipart(body: bytes, content_type: str):
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type or "")
    if not m:
        return []
    boundary = (m.group(1) or m.group(2)).strip().encode()
    files = []
    for seg in body.split(b"--" + boundary)[1:]:
        if seg.startswith(b"--"):          # 结束符
            continue
        if seg.startswith(b"\r\n"):
            seg = seg[2:]
        if seg.endswith(b"\r\n"):
            seg = seg[:-2]
        if b"\r\n\r\n" not in seg:
            continue
        head, data = seg.split(b"\r\n\r\n", 1)
        fm = re.search(r'filename="([^"]*)"', head.decode("utf-8", "replace"))
        if fm and data:
            files.append((fm.group(1), data))
    return files


# ---------------- HTTP 处理 ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = "SafetySensorAlarmReceiver/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 精简日志
        print(f"[{datetime.now():%H:%M:%S}] {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    # ---- 工具 ----
    def _send(self, status, payload, body_bytes=None, content_type="application/json"):
        started = self._t0
        if body_bytes is None:
            if isinstance(payload, dict) and "metrics" not in payload:
                payload["metrics"] = {"api_ms": round((time.perf_counter() - started) * 1000, 1)}
            body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _ok(self, data):
        self._send(200, {"code": "ok", "data": data, "error_message": None})

    def _err(self, status, code, msg):
        self._send(status, {"code": code, "data": None, "error_message": msg})

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _authed(self):
        return self.headers.get("Authorization", "") == f"Bearer {self.store.token}"

    @property
    def store(self) -> Store:
        return self.server.store

    # ---- 路由 ----
    def do_GET(self):
        self._t0 = time.perf_counter()
        path = self.path.split("?")[0]
        if path == "/health":
            return self._ok({"status": "up"})
        if not self._authed():
            return self._err(401, "ERR_AUTH", "missing or invalid bearer token")
        if path == "/api/v1/alarms":
            return self._list_alarms()
        m = re.fullmatch(r"/api/v1/alarms/([^/]+)", path)
        if m:
            return self._alarm_detail(urllib_unquote(m.group(1)))
        m = re.fullmatch(r"/api/v1/evidence/(\d+)/file", path)
        if m:
            return self._evidence_file(int(m.group(1)))
        self._err(404, "ERR_NOT_FOUND", f"no route: {path}")

    def do_POST(self):
        self._t0 = time.perf_counter()
        path = self.path.split("?")[0]
        if not self._authed():
            return self._err(401, "ERR_AUTH", "missing or invalid bearer token")
        if path == "/api/v1/alarms":
            return self._push_alarm()
        m = re.fullmatch(r"/api/v1/alarms/([^/]+)/evidence", path)
        if m:
            return self._push_evidence(urllib_unquote(m.group(1)))
        m = re.fullmatch(r"/api/v1/alarms/([^/]+)/images", path)
        if m:
            return self._push_images(urllib_unquote(m.group(1)))
        self._err(404, "ERR_NOT_FOUND", f"no route: {path}")

    # ---- 业务 ----
    def _push_alarm(self):
        try:
            payload = json.loads(self._body() or b"{}")
        except ValueError:
            return self._err(400, "ERR_PARAM", "body is not json")
        alarm_no, camera_id = payload.get("alarm_no"), payload.get("camera_id")
        if not alarm_no or not camera_id:
            return self._err(400, "ERR_PARAM", "alarm_no and camera_id are required")
        with LOCK:
            existing = self.store.find_alarm(alarm_no)
            if existing:
                return self._ok({"alarm_id": existing["id"], "duplicated": True})
            alarm = {
                "id": self.store.db["next_alarm_id"],
                "alarm_no": alarm_no,
                "camera_id": camera_id,
                "channel_number": payload.get("channel_number", 0),
                "solution": payload.get("solution"),
                "level": payload.get("level", 1),
                "title": payload.get("title"),
                "description": payload.get("description"),
                "occurred_at": payload.get("occurred_at"),
                "source": "push",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "evidence": [],
            }
            self.store.db["next_alarm_id"] += 1
            self.store.db["alarms"].append(alarm)
            self.store.save()
        trigger_linkage(alarm)
        self._ok({"alarm_id": alarm["id"], "duplicated": False})

    def _push_evidence(self, alarm_no):
        try:
            payload = json.loads(self._body() or b"{}")
        except ValueError:
            return self._err(400, "ERR_PARAM", "body is not json")
        with LOCK:
            alarm = self.store.find_alarm(alarm_no)
            if not alarm:
                return self._err(404, "ERR_NOT_FOUND", f"alarm {alarm_no} not found")
            added = skipped = 0
            for item in payload.get("items", []):
                url = item.get("url")
                if url and any(ev.get("url") == url for ev in alarm["evidence"]):
                    skipped += 1
                    continue
                ev = {
                    "id": self.store.db["next_evidence_id"],
                    "seq": item.get("seq", len(alarm["evidence"]) + 1),
                    "type": item.get("type", "other"),
                    "name": item.get("name"),
                    "description": item.get("description"),
                    "url": url,
                    "file_path": None,
                    "sha256": None,
                }
                self.store.db["next_evidence_id"] += 1
                alarm["evidence"].append(ev)
                added += 1
            self.store.save()
        self._ok({"added": added, "skipped": skipped})

    def _push_images(self, alarm_no):
        body = self._body()
        with LOCK:
            alarm = self.store.find_alarm(alarm_no)
            if not alarm:
                return self._err(404, "ERR_NOT_FOUND", f"alarm {alarm_no} not found")
            saved = duplicated = 0
            for filename, data in parse_multipart(body, self.headers.get("Content-Type")):
                sha = hashlib.sha256(data).hexdigest()
                if any(ev.get("sha256") == sha for ev in alarm["evidence"]):
                    duplicated += 1
                    continue
                day = datetime.now().strftime("%Y%m%d")
                rel = Path(day) / f"{alarm_no}_{sha[:8]}{Path(filename).suffix or '.jpg'}"
                (self.store.image_dir / day).mkdir(parents=True, exist_ok=True)
                (self.store.image_dir / rel).write_bytes(data)
                ev = {
                    "id": self.store.db["next_evidence_id"],
                    "seq": len(alarm["evidence"]) + 1,
                    "type": "image",
                    "name": filename,
                    "description": None,
                    "url": None,
                    "file_path": rel.as_posix(),
                    "sha256": sha,
                }
                self.store.db["next_evidence_id"] += 1
                alarm["evidence"].append(ev)
                saved += 1
            self.store.save()
        self._ok({"saved": saved, "duplicated": duplicated})

    def _list_alarms(self):
        query = dict(p.split("=", 1) for p in self.path.split("?")[1].split("&") if "=" in p) if "?" in self.path else {}
        with LOCK:
            rows = list(self.store.db["alarms"])
        if query.get("camera_id"):
            rows = [a for a in rows if a["camera_id"] == query["camera_id"]]
        if query.get("level"):
            rows = [a for a in rows if str(a["level"]) == query["level"]]
        self._ok({"total": len(rows), "items": rows})

    def _alarm_detail(self, alarm_no):
        with LOCK:
            alarm = self.store.find_alarm(alarm_no)
        if not alarm:
            return self._err(404, "ERR_NOT_FOUND", f"alarm {alarm_no} not found")
        self._ok(alarm)

    def _evidence_file(self, evidence_id):
        with LOCK:
            ev = self.store.find_evidence(evidence_id)
            file_path = ev["file_path"] if ev else None
        if not file_path:
            return self._err(404, "ERR_NOT_FOUND", f"evidence {evidence_id} has no file")
        path = self.store.image_dir / file_path
        if not path.exists():
            return self._err(404, "ERR_NOT_FOUND", f"file missing on disk: {file_path}")
        data = path.read_bytes()
        ctype = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "application/octet-stream"
        self._send(200, None, body_bytes=data, content_type=ctype)


def urllib_unquote(s):
    from urllib.parse import unquote
    return unquote(s)


def make_server(host=HOST, port=PORT, data_dir: Path = DATA_DIR, token=TOKEN):
    server = ThreadingHTTPServer((host, port), Handler)
    server.store = Store(data_dir)
    server.store.token = token
    return server


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    server = make_server(port=port)
    print(f"报警接收服务已启动: http://{HOST}:{port}  (token={TOKEN})")
    print("接口: POST /api/v1/alarms | POST /api/v1/alarms/<no>/evidence | POST /api/v1/alarms/<no>/images")
    print("      GET  /api/v1/alarms | GET /api/v1/alarms/<no> | GET /api/v1/evidence/<id>/file")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
