# -*- coding: utf-8 -*-
"""alarm_receiver.py 自测脚本（仅标准库，直接运行）。

运行：python test_alarm_receiver.py
在临时目录启动接收服务（端口 8123），依次验证：
鉴权、报警推送与幂等、证据链、图片上传去重、查询、文件取回。
全部通过退出码 0。
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import alarm_receiver as ar

BASE = "http://127.0.0.1:8123"
TOKEN = "test-token"
FAILURES: list[str] = []

TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
    "AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)
TINY_JPEG_2 = TINY_JPEG + b"\x00"


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def call(method, path, body=None, token=TOKEN, content_type="application/json"):
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            return resp.status, (json.loads(raw) if "json" in ctype else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def build_multipart(files):
    boundary = "----PyTestBoundary"
    out = b""
    for name, filename, data in files:
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        out += b"Content-Type: image/jpeg\r\n\r\n"
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


def main():
    tmp = Path(tempfile.mkdtemp(prefix="alarm_test_"))
    server = ar.make_server(host="127.0.0.1", port=8123, data_dir=tmp, token=TOKEN)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.5)
    try:
        # 鉴权
        status, body = call("GET", "/api/v1/alarms", token=None)
        check("无 token 401 ERR_AUTH", status == 401 and body["code"] == "ERR_AUTH", str(body))
        status, body = call("GET", "/api/v1/alarms", token="wrong")
        check("错误 token 401", status == 401, str(status))
        status, body = call("GET", "/health", token=None)
        check("health 免鉴权", status == 200 and body["code"] == "ok", str(body))

        # 报警推送 + 信封 + 幂等
        status, body = call("POST", "/api/v1/alarms", {
            "alarm_no": "ALM-T1", "camera_id": "cam-1", "channel_number": 1,
            "solution": {"id": 7, "name": "安全帽检测"}, "level": 3, "title": "未戴安全帽",
        })
        check("推送报警 200 信封 ok", status == 200 and body["code"] == "ok"
              and body["error_message"] is None and "api_ms" in body["metrics"], str(body))
        check("首次推送 duplicated=false", body["data"]["duplicated"] is False, str(body))
        status, body2 = call("POST", "/api/v1/alarms", {"alarm_no": "ALM-T1", "camera_id": "cam-1", "level": 3})
        check("重复推送幂等 duplicated=true", body2["data"]["duplicated"] is True
              and body2["data"]["alarm_id"] == body["data"]["alarm_id"], str(body2))
        status, body = call("POST", "/api/v1/alarms", {"camera_id": "cam-1"})
        check("缺 alarm_no 400 ERR_PARAM", status == 400 and body["code"] == "ERR_PARAM", str(body))

        # 证据链
        status, body = call("POST", "/api/v1/alarms/ALM-T1/evidence", {"items": [
            {"seq": 1, "type": "log", "description": "算法判定日志"},
            {"seq": 2, "type": "image", "url": "http://camera.local/snap/1.jpg"},
        ]})
        check("证据链 added=2", status == 200 and body["data"]["added"] == 2, str(body))
        status, body = call("POST", "/api/v1/alarms/ALM-T1/evidence", {"items": [
            {"seq": 2, "type": "image", "url": "http://camera.local/snap/1.jpg"}]})
        check("相同 url 证据跳过", body["data"] == {"added": 0, "skipped": 1}, str(body))
        status, body = call("POST", "/api/v1/alarms/ALM-NOPE/evidence", {"items": []})
        check("证据链报警不存在 404", status == 404 and body["code"] == "ERR_NOT_FOUND", str(body))

        # 图片上传 + sha256 去重
        mp, ctype = build_multipart([
            ("files", "front.jpg", TINY_JPEG),
            ("files", "side.jpg", TINY_JPEG_2),
        ])
        status, body = call("POST", "/api/v1/alarms/ALM-T1/images", mp, content_type=ctype)
        check("图片x2 saved=2", status == 200 and body["data"] == {"saved": 2, "duplicated": 0}, str(body))
        status, body = call("POST", "/api/v1/alarms/ALM-T1/images", mp, content_type=ctype)
        check("重复图片去重 duplicated=2", body["data"] == {"saved": 0, "duplicated": 2}, str(body))
        check("图片落盘 2 个文件", len(list((tmp / "images").rglob("*.jpg"))) == 2,
              str(list((tmp / "images").rglob("*"))))

        # 查询
        call("POST", "/api/v1/alarms", {"alarm_no": "ALM-T2", "camera_id": "cam-2", "level": 1})
        status, body = call("GET", "/api/v1/alarms")
        check("列表 total=2", body["data"]["total"] == 2, str(body["data"]["total"]))
        status, body = call("GET", "/api/v1/alarms?camera_id=cam-2")
        check("按 camera_id 过滤", body["data"]["total"] == 1, str(body["data"]))
        status, body = call("GET", "/api/v1/alarms/ALM-T1")
        check("详情证据链=4", len(body["data"]["evidence"]) == 4, str(len(body["data"]["evidence"])))
        status, body = call("GET", "/api/v1/alarms/ALM-NOPE")
        check("详情不存在 404", status == 404, str(status))

        # 文件取回
        status, detail = call("GET", "/api/v1/alarms/ALM-T1")
        img = [ev for ev in detail["data"]["evidence"] if ev["type"] == "image" and ev["file_path"]][0]
        status, raw = call("GET", f"/api/v1/evidence/{img['id']}/file")
        check("证据文件字节一致", status == 200 and raw in (TINY_JPEG, TINY_JPEG_2), str(status))
        log_ev = [ev for ev in detail["data"]["evidence"] if ev["type"] == "log"][0]
        status, _ = call("GET", f"/api/v1/evidence/{log_ev['id']}/file")
        check("无文件证据 404", status == 404, str(status))
    finally:
        server.shutdown()

    print("\n自测结果:", "全部通过" if not FAILURES else f"失败 {len(FAILURES)} 项: {FAILURES}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
