# -*- coding: utf-8 -*-
"""收发报警指令耗时测试（仅标准库，直接运行）。

运行：python bench_alarm.py
本地临时目录启动接收服务（端口 8124），测量：
  1) POST /api/v1/alarms            推送报警（串行 x100）
  2) GET  /api/v1/alarms/<no>       查询报警详情（串行 x100）
  3) POST /api/v1/alarms/<no>/evidence  推送证据链（串行 x50）
  4) POST /api/v1/alarms/<no>/images    推送图片（串行 x50）
  5) POST /api/v1/alarms            并发推送（100 次 / 10 线程）
输出客户端往返耗时 min/avg/p50/p95/max 与服务端 api_ms 均值。
"""
from __future__ import annotations

import base64
import json
import statistics
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import alarm_receiver as ar

BASE = "http://127.0.0.1:8124"
TOKEN = "bench-token"

TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
    "AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)


def call(method, path, body=None, content_type="application/json"):
    """返回 (客户端往返毫秒, 服务端api_ms, 响应bytes)。"""
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    rtt = (time.perf_counter() - t0) * 1000
    api_ms = None
    if raw[:1] == b"{":
        try:
            api_ms = json.loads(raw).get("metrics", {}).get("api_ms")
        except ValueError:
            pass
    return rtt, api_ms, raw


def build_multipart(filename, data):
    b = "----BenchBoundary"
    out = f"--{b}\r\n".encode()
    out += f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'.encode()
    out += b"Content-Type: image/jpeg\r\n\r\n" + data + b"\r\n"
    out += f"--{b}--\r\n".encode()
    return out, f"multipart/form-data; boundary={b}"


def report(name, rtts, api_list):
    rtts = sorted(rtts)
    n = len(rtts)
    p50, p95 = rtts[n // 2], rtts[min(n - 1, int(n * 0.95))]
    api = f"{statistics.mean(api_list):.2f}" if api_list else "-"
    print(f"{name:<38} n={n:<4} min={rtts[0]:6.2f}  avg={statistics.mean(rtts):6.2f} "
          f"p50={p50:6.2f} p95={p95:6.2f} max={rtts[-1]:6.2f}  服务端api_ms均值={api}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    tmp = Path(tempfile.mkdtemp(prefix="alarm_bench_"))
    server = ar.make_server(host="127.0.0.1", port=8124, data_dir=tmp, token=TOKEN)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.5)
    print("单位: 毫秒（客户端往返 RTT）\n")
    try:
        # 1) 串行推送报警 x100
        rtts, apis = [], []
        for i in range(100):
            rtt, api, _ = call("POST", "/api/v1/alarms", {
                "alarm_no": f"BENCH-{i:04d}", "camera_id": "cam-1",
                "channel_number": 1, "level": 3, "title": "压测报警",
            })
            rtts.append(rtt)
            if api is not None:
                apis.append(api)
        report("POST /alarms 推送报警(串行)", rtts, apis)

        # 2) 串行查询详情 x100
        rtts, apis = [], []
        for i in range(100):
            rtt, api, _ = call("GET", f"/api/v1/alarms/BENCH-{i:04d}")
            rtts.append(rtt)
            if api is not None:
                apis.append(api)
        report("GET /alarms/<no> 查询详情(串行)", rtts, apis)

        # 3) 串行推送证据链 x50
        rtts, apis = [], []
        for i in range(50):
            rtt, api, _ = call("POST", f"/api/v1/alarms/BENCH-{i:04d}/evidence",
                               {"items": [{"seq": 1, "type": "log", "description": "证据日志"}]})
            rtts.append(rtt)
            if api is not None:
                apis.append(api)
        report("POST /alarms/<no>/evidence(串行)", rtts, apis)

        # 4) 串行推送图片 x50
        mp, ctype = build_multipart("bench.jpg", TINY_JPEG)
        rtts, apis = [], []
        for i in range(50):
            rtt, api, _ = call("POST", f"/api/v1/alarms/BENCH-{i:04d}/images", mp, content_type=ctype)
            rtts.append(rtt)
            if api is not None:
                apis.append(api)
        report("POST /alarms/<no>/images(串行)", rtts, apis)

        # 5) 并发推送 100 次 / 10 线程
        def _push(i):
            return call("POST", "/api/v1/alarms", {
                "alarm_no": f"BENCH-C{i:04d}", "camera_id": "cam-1", "level": 1,
            })

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=10) as ex:
            results = list(ex.map(_push, range(100)))
        wall = time.perf_counter() - t0
        rtts = [r[0] for r in results]
        apis = [r[1] for r in results if r[1] is not None]
        report("POST /alarms 推送报警(10并发)", rtts, apis)
        print(f"\n并发吞吐: {100 / wall:.1f} req/s （100 次推送总耗时 {wall * 1000:.0f} ms）")
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
