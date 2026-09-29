# -*- coding: utf-8 -*-
"""与相机（默认 192.168.5.100）的通讯延时压测（仅标准库，直接运行）。

运行：
    python bench_camera.py                             # 默认 192.168.5.100:8000
    python bench_camera.py --host 192.168.5.100 --port 8000 --token camera-token
    python bench_camera.py --n 200 --concurrency 10

测量项：
  1) ICMP ping RTT                —— 网络层往返（无 HTTP 开销），需系统 ping 命令
  2) TCP connect 建连耗时         —— 三次握手 RTT（socket 层）
  3) HTTP GET /                   —— 一次完整 HTTP 请求（404 也算，测 HTTP 栈）
  4) HTTP PATCH /gpio/do/1        —— 反控联动实际调用（Token 不对时返回 401，仍测往返）
  5) 10 并发 PATCH /gpio/do/1     —— 并发吞吐与延时分布

每项输出 min / avg / p50 / p95 / max（毫秒）。若响应含平台信封 metrics.api_ms，
额外输出服务端处理耗时均值，用以拆分「网络+HTTP 栈」与「相机业务逻辑」两部分。
"""
from __future__ import annotations

import argparse
import json
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_HOST = "192.168.5.100"
DEFAULT_PORT = 8000
DEFAULT_TOKEN = "camera-token"


# ---------------- 统计工具 ----------------
def percentile(sorted_list, q):
    if not sorted_list:
        return float("nan")
    idx = min(len(sorted_list) - 1, int(len(sorted_list) * q))
    return sorted_list[idx]


def report(name, samples_ms, api_ms_list=None, extra=""):
    if not samples_ms:
        print(f"{name:<38} n=0  无有效样本 {extra}")
        return
    s = sorted(samples_ms)
    n = len(s)
    line = (f"{name:<38} n={n:<4} "
            f"min={s[0]:7.2f}  avg={statistics.mean(s):7.2f}  "
            f"p50={percentile(s, 0.50):7.2f}  p95={percentile(s, 0.95):7.2f}  "
            f"max={s[-1]:7.2f}  (ms)")
    if api_ms_list:
        line += f"  服务端api_ms均值={statistics.mean(api_ms_list):.2f}"
    if extra:
        line += f"  {extra}"
    print(line)


# ---------------- 1) ICMP ping ----------------
def bench_ping(host, count=20):
    """调用系统 ping，解析 rtt 行；失败返回空列表。"""
    try:
        # -c 次数, -W 单次超时(秒), -i 间隔(秒) —— Linux 语法
        out = subprocess.run(
            ["ping", "-c", str(count), "-W", "2", "-i", "0.2", host],
            capture_output=True, text=True, timeout=count * 2 + 5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[warn] ping 调用失败: {exc}")
        return []
    rtts = []
    for line in out.stdout.splitlines():
        # 形如：64 bytes from 192.168.5.100: icmp_seq=1 ttl=64 time=2.38 ms
        if "time=" in line:
            try:
                rtts.append(float(line.split("time=")[1].split()[0]))
            except (ValueError, IndexError):
                pass
    return rtts


# ---------------- 2) TCP connect ----------------
def bench_tcp_connect(host, port, count=50, timeout=3.0):
    samples = []
    for _ in range(count):
        t0 = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                pass
            samples.append((time.perf_counter() - t0) * 1000)
        except OSError:
            pass
    return samples


# ---------------- 3/4) HTTP 请求 ----------------
def http_call(method, url, body=None, token=None, timeout=5.0):
    """返回 (rtt_ms, http_status, api_ms_or_None, err_or_None)。"""
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        return (time.perf_counter() - t0) * 1000, None, None, str(e)
    rtt = (time.perf_counter() - t0) * 1000
    api_ms = None
    if raw[:1] == b"{":
        try:
            api_ms = json.loads(raw).get("metrics", {}).get("api_ms")
        except ValueError:
            pass
    return rtt, status, api_ms, None


def bench_http(method, path, host, port, token=None, body=None, count=100, label=None):
    url = f"http://{host}:{port}{path}"
    rtts, apis, statuses, errs = [], [], {}, 0
    for _ in range(count):
        rtt, status, api_ms, err = http_call(method, url, body=body, token=token)
        if err is not None:
            errs += 1
            continue
        rtts.append(rtt)
        statuses[status] = statuses.get(status, 0) + 1
        if api_ms is not None:
            apis.append(api_ms)
    label = label or f"{method} {path}"
    stat_str = "status=" + ",".join(f"{k}x{v}" for k, v in sorted(statuses.items()))
    if errs:
        stat_str += f",err={errs}"
    report(label, rtts, apis, extra=stat_str)
    return rtts


# ---------------- 5) 并发 ----------------
def bench_http_concurrent(method, path, host, port, token=None, body=None,
                          count=100, workers=10, label=None):
    url = f"http://{host}:{port}{path}"

    def _one(_):
        return http_call(method, url, body=body, token=token)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(_one, range(count)))
    wall = time.perf_counter() - t0
    rtts = [r[0] for r in results if r[3] is None]
    apis = [r[2] for r in results if r[3] is None and r[2] is not None]
    statuses = {}
    errs = 0
    for r in results:
        if r[3] is not None:
            errs += 1
        else:
            statuses[r[1]] = statuses.get(r[1], 0) + 1
    label = label or f"{method} {path} ({workers}并发)"
    stat_str = "status=" + ",".join(f"{k}x{v}" for k, v in sorted(statuses.items()))
    if errs:
        stat_str += f",err={errs}"
    report(label, rtts, apis, extra=stat_str)
    if wall > 0:
        print(f"  ↳ 并发吞吐: {count / wall:.1f} req/s （{count} 次总耗时 {wall * 1000:.0f} ms）")


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--token", default=DEFAULT_TOKEN)
    ap.add_argument("--n", type=int, default=100, help="HTTP 串行样本数")
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--do-channel", type=int, default=1)
    ap.add_argument("--skip-ping", action="store_true")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"目标相机: http://{args.host}:{args.port}  token={args.token!r}")
    print(f"样本量: 串行 n={args.n}  并发 workers={args.concurrency}")
    print("单位: 毫秒\n")

    # 1) ping
    if not args.skip_ping:
        ping_rtts = bench_ping(args.host, count=20)
        report("ICMP ping (网络层)", ping_rtts)

    # 2) TCP connect
    tcp_rtts = bench_tcp_connect(args.host, args.port, count=50)
    report(f"TCP connect {args.host}:{args.port}", tcp_rtts)

    # 3) HTTP GET / —— 相机会返回 404 信封，用来测 HTTP 栈
    bench_http("GET", "/", args.host, args.port, count=args.n,
               label="HTTP GET /  (HTTP 栈基线)")

    # 4) HTTP PATCH /gpio/do/<ch> —— 反控联动实际调用
    do_body = {
        "name": "bench-linkage",
        "mode": "manual",
        "value": 0,           # 只做延时探测，不真正置高
        "related_solutions": [],
    }
    do_path = f"/gpio/do/{args.do_channel}"
    bench_http("PATCH", do_path, args.host, args.port,
               token=args.token, body=do_body, count=args.n,
               label=f"HTTP PATCH {do_path}  (串行)")

    # 5) 并发
    bench_http_concurrent("PATCH", do_path, args.host, args.port,
                          token=args.token, body=do_body,
                          count=args.n, workers=args.concurrency,
                          label=f"HTTP PATCH {do_path}  ({args.concurrency}并发)")

    print("\n说明：")
    print("  * ping 反映纯网络 RTT；TCP connect 反映握手 RTT（≈ 1×RTT + 内核调度）；")
    print("    HTTP GET/PATCH 反映完整应用层往返（≈ 2×RTT + 服务端处理）。")
    print("  * 服务端 api_ms 均值 = 相机内部处理耗时；RTT - api_ms ≈ 网络+HTTP 栈耗时。")
    print("  * PATCH 若返回 401「无效的令牌」，说明 Token 不对，但 RTT 仍可用作通讯延时参考；")
    print("    如需测「业务成功路径」延时，请用 --token 传入相机实际 Token。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
