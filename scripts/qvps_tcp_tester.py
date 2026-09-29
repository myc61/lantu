# -*- coding: utf-8 -*-
"""
防错视觉传感器 TCP 检测接口 —— 接口测试工具

接口文档：《防错视觉传感器 TCP 检测接口 V1.0》（产品版本 1.6.3）

协议要点：
  1. TCP 连接 <相机IP>:<端口，默认5000>，UTF-8 编码。
  2. 客户端发送程序号：PN-001，或携带 trace_id：PN-001__|__<trace_id>
     分隔符固定为 __|__；trace_id 仅含字母/数字/_/-，长度 1-128。
  3. 设备返回 JSON 文本，并以两个换行符 \\n\\n 作为结束标记。
  4. 响应关键字段：trace_id / program_number / result(OK|NG|ERROR)
     / error_type / error_message / solution_id / solution_name
     / check_time / duration_ms / image_path / compressed_image_path
     / evidences[]。

图片获取说明：
  接口仅返回设备端图片路径（image_path / compressed_image_path），文档未定义
  图片下载通道。本工具按以下顺序自动尝试获取原图：
    1) TCP 尾随数据：部分固件会在 JSON(\\n\\n) 后继续发送图片二进制；
    2) HTTP 下载：http://<相机IP>/<image_path>（端口 80，可用 --http-port 修改）。
  若两种方式均失败，工具会把设备端路径打印出来，可人工通过 U 盘/Web 界面取图。

用法示例：
  py qvps_tcp_tester.py full -H 192.168.31.100 -p PN-001
  py qvps_tcp_tester.py check -H 192.168.31.100 -p PN-001
  py qvps_tcp_tester.py loop  -H 192.168.31.100 -p PN-001 -n 20
"""

import argparse
import json
import os
import re
import socket
import sys
import time
import uuid
from datetime import datetime
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

# ---------- Windows 控制台 UTF-8 输出 ----------
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

DEFAULT_PORT = 10008
DELIMITER = b"\n\n"
TRACE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# 结果着色（Windows 10+ 终端支持 ANSI）
class C:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    GRAY = "\033[90m"
    BOLD = "\033[1m"
    END = "\033[0m"


def enable_color():
    if os.name == "nt":
        os.system("")  # 激活 VT100


# ================= 协议层 =================

def build_request(program_number, trace_id=None):
    """构造请求报文：PN-001 或 PN-001__|__trace_id"""
    if trace_id is not None and not TRACE_RE.match(trace_id):
        raise ValueError(
            "trace_id 非法：只能包含字母、数字、下划线_、短横线-，长度 1-128")
    if trace_id is None:
        text = program_number
    else:
        text = f"{program_number}__|__{trace_id}"
    return text.encode("utf-8")


def _drain_trailing(sock, idle_timeout=0.6, max_bytes=50 * 1024 * 1024):
    """JSON 结束标记后，继续读取设备可能尾随推送的二进制（如图片）。

    连接在 idle_timeout 时间内没有新数据即认为发送完毕。
    """
    buf = b""
    sock.settimeout(idle_timeout)
    while len(buf) < max_bytes:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            break
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    return buf


def request_check(host, port, program_number, trace_id=None,
                  timeout=15.0, drain_image=True):
    """触发一次检测，返回 (result_dict, trailing_bytes)。

    完整实现文档第 5 节调用流程：发送程序号 -> 持续接收直到 \\n\\n -> 解析 JSON。
    """
    payload = build_request(program_number, trace_id)
    started = time.time()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload)

        resp = b""
        while DELIMITER not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("连接在响应完成前被设备关闭")
            resp += chunk
            if b"\n\n" not in resp and time.time() - started > timeout:
                raise socket.timeout("等待响应超时")

        packet, after = resp.split(DELIMITER, 1)
        result = json.loads(packet.decode("utf-8"))

        trailing = b""
        if drain_image:
            trailing = after + _drain_trailing(sock)
    return result, trailing


# ================= 图片获取 =================

def detect_image_ext(data):
    """根据文件头魔数识别图片类型，返回扩展名（不含点）。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:2] == b"BM":
        return "bmp"
    if len(data) >= 12 and data[8:12] == b"WEBP":
        return "webp"
    return None


def fetch_image_http(host, image_path, http_port=80, timeout=10.0):
    """通过 HTTP 下载图片：http://<host>[:port]/<image_path>"""
    url = f"http://{host}:{http_port}{image_path}"
    with urlopen(url, timeout=timeout) as r:
        return r.read(), url


def save_image(data, out_dir, trace_id, kind="image"):
    """把图片字节保存到 out_dir，返回保存路径；无法识别类型返回 None。"""
    os.makedirs(out_dir, exist_ok=True)
    ext = detect_image_ext(data)
    if not ext:
        return None
    fname = f"{trace_id}_{kind}.{ext}"
    fpath = os.path.join(out_dir, fname)
    with open(fpath, "wb") as f:
        f.write(data)
    return fpath


def acquire_images(result, trailing, out_dir, host, http_port=80,
                   no_http=False, verbose=True):
    """获取检测原图。返回 [(类型, 来源, 本地路径或None, 说明)]。"""
    trace_id = result.get("trace_id") or "unknown"
    targets = []
    if result.get("image_path"):
        targets.append(("原图", result["image_path"], "original"))
    if result.get("compressed_image_path"):
        targets.append(("压缩图", result["compressed_image_path"], "compressed"))

    saved = []

    # 1) TCP 尾随二进制（整段按图片保存；若尾随两张图则只取第一张）
    if trailing:
        p = save_image(trailing, out_dir, trace_id, "tcp_trailing")
        if p:
            saved.append(("原图(推测)", "TCP 尾随数据", p,
                          f"{len(trailing)} 字节"))
            if verbose:
                print(f"  {C.GREEN}[图片]{C.END} 从 TCP 尾随数据保存图片 -> {p}")

    # 2) HTTP 下载
    if not no_http:
        for label, dev_path, kind in targets:
            try:
                data, url = fetch_image_http(host, dev_path, http_port)
                p = save_image(data, out_dir, trace_id, kind)
                if p:
                    saved.append((label, url, p, f"{len(data)} 字节"))
                    if verbose:
                        print(f"  {C.GREEN}[图片]{C.END} {label} HTTP 下载成功 -> {p}")
            except (URLError, HTTPError, OSError) as e:
                if verbose:
                    print(f"  {C.GRAY}[图片]{C.END} {label} HTTP 下载失败：{e}")

    # 3) 始终回显设备端路径
    for label, dev_path, _ in targets:
        if verbose:
            print(f"  {C.GRAY}[路径]{C.END} 设备端 {label} 路径：{dev_path}")
    if not saved and not targets and verbose:
        print(f"  {C.YELLOW}[图片]{C.END} 响应中未包含 image_path（取决于设备存图配置）")
    return saved


# ================= 输出展示 =================

def print_response(result, elapsed):
    print(f"\n{C.BOLD}────── 检测响应 ──────{C.END}")
    res = result.get("result")
    color = {"OK": C.GREEN, "NG": C.RED, "ERROR": C.YELLOW}.get(res, C.GRAY)
    print(f"  整体结果   : {color}{C.BOLD}{res}{C.END}")
    print(f"  trace_id   : {result.get('trace_id')}")
    print(f"  程序号     : {result.get('program_number')}")
    print(f"  方案       : {result.get('solution_name')} (id={result.get('solution_id')})")
    print(f"  检测时间   : {result.get('check_time')}")
    print(f"  耗时       : {result.get('duration_ms')} ms （实测 RTT {elapsed*1000:.0f} ms）")
    if result.get("error_type") or result.get("error_message"):
        print(f"  {C.YELLOW}错误码     : {result.get('error_type')}{C.END}")
        print(f"  {C.YELLOW}错误描述   : {result.get('error_message')}{C.END}")

    evs = result.get("evidences") or []
    if evs:
        print(f"  ROI 明细   : 共 {len(evs)} 个")
        for e in evs:
            er = e.get("result")
            ec = {"OK": C.GREEN, "NG": C.RED, "ERROR": C.YELLOW}.get(er, C.GRAY)
            rect = e.get("rectangle")
            rect_s = ""
            if rect and len(rect) == 2:
                rect_s = f" 区域[[{rect[0][0]:.3f},{rect[0][1]:.3f}],[{rect[1][0]:.3f},{rect[1][1]:.3f}]]"
            print(f"    - ROI {e.get('roi_id')}: {ec}{er}{C.END} "
                  f"期望={e.get('expected_label')}({e.get('expected_score')}) "
                  f"实际={e.get('result_label')}({_fmt_score(e.get('result_score'))}){rect_s}")


def _fmt_score(s):
    return f"{s:.3f}" if isinstance(s, (int, float)) else s


# ================= 业务流程 =================

def do_check(host, port, program, trace_id, timeout, args):
    """触发一次检测并打印结果，返回 result（异常返回 None）。"""
    print(f"{C.CYAN}[触发]{C.END} {host}:{port} 程序号={program} "
          f"trace_id={trace_id or '(自动)'}")
    t0 = time.time()
    try:
        result, trailing = request_check(
            host, port, program, trace_id, timeout=timeout,
            drain_image=not args.no_image)
    except (socket.timeout, OSError, ConnectionError, json.JSONDecodeError) as e:
        print(f"  {C.RED}[失败]{C.END} 检测请求异常：{type(e).__name__}: {e}")
        return None
    elapsed = time.time() - t0
    print_response(result, elapsed)

    if not args.no_image:
        print(f"\n{C.BOLD}────── 获取原图 ──────{C.END}")
        acquire_images(result, trailing, args.outdir, host,
                       http_port=args.http_port, no_http=args.no_http)
    return result


def run_full(args):
    """主流程测试：触发检测 -> 获取 OK/NG 结果 -> 获取检测原图。"""
    trace_id = args.trace_id or uuid.uuid4().hex
    print(f"{C.BOLD}========== 主流程测试 =========={C.END}")
    result = do_check(args.host, args.port, args.program, trace_id,
                      args.timeout, args)

    print(f"\n{C.BOLD}────── 测试结论 ──────{C.END}")
    if result is None:
        print(f"  {C.RED}结论：通信失败{C.END}（无法连接设备或响应不合法）")
        return 2
    res = result.get("result")
    # 校验 trace_id 回显
    if trace_id and result.get("trace_id") != trace_id:
        print(f"  {C.YELLOW}警告：trace_id 回显不一致"
              f"（发送 {trace_id} / 收到 {result.get('trace_id')}）{C.END}")
    if res == "OK":
        print(f"  {C.GREEN}接口链路正常，本次检测结果 OK。{C.END}")
        return 0
    if res == "NG":
        print(f"  {C.GREEN}接口链路正常，设备正确返回 NG 结果"
              f"（产品判定不良，非接口故障）。{C.END}")
        return 0
    print(f"  {C.RED}设备返回 ERROR：{result.get('error_type')} - "
          f"{result.get('error_message')}{C.END}")
    return 1


def run_loop(args):
    """连续触发多次，统计 OK/NG/ERROR/失败。"""
    stats = {"OK": 0, "NG": 0, "ERROR": 0, "FAIL": 0}
    print(f"{C.BOLD}========== 连续触发测试 x{args.count} "
          f"（间隔 {args.interval}s）=========={C.END}")
    for i in range(1, args.count + 1):
        tid = uuid.uuid4().hex
        print(f"\n{C.BOLD}[{i}/{args.count}]{C.END}")
        result = do_check(args.host, args.port, args.program, tid,
                          args.timeout, args)
        if result is None:
            stats["FAIL"] += 1
        else:
            stats[result.get("result", "FAIL")] = stats.get(
                result.get("result", "FAIL"), 0) + 1
        if i < args.count:
            time.sleep(args.interval)

    print(f"\n{C.BOLD}────── 统计 ──────{C.END}")
    print(f"  总数 {args.count}：{C.GREEN}OK={stats['OK']}{C.END}  "
          f"{C.RED}NG={stats['NG']}{C.END}  {C.YELLOW}ERROR={stats['ERROR']}{C.END}  "
          f"{C.RED}通信失败={stats['FAIL']}{C.END}")
    return 0 if stats["FAIL"] == 0 else 2


def run_error_cases(args):
    """错误用例测试：非法 trace_id（客户端拦截）+ 不存在的程序号。"""
    print(f"{C.BOLD}========== 错误用例测试 =========={C.END}")

    print(f"\n{C.CYAN}[用例1]{C.END} 非法 trace_id（含非法字符）—— 客户端应拦截")
    try:
        build_request(args.program, "bad id!!!")
        print(f"  {C.RED}未拦截，不符合预期{C.END}")
    except ValueError as e:
        print(f"  {C.GREEN}已按预期拦截：{e}{C.END}")

    print(f"\n{C.CYAN}[用例2]{C.END} 不存在的程序号 PN_NOT_EXIST —— 期望 result=ERROR / program.001")
    bad_args = argparse.Namespace(**vars(args))
    bad_args.no_image = True
    result = do_check(args.host, args.port, "PN_NOT_EXIST", uuid.uuid4().hex,
                      args.timeout, bad_args)
    if result and result.get("result") == "ERROR":
        print(f"  {C.GREEN}设备按预期返回 ERROR：{result.get('error_type')}{C.END}")
        return 0
    print(f"  {C.YELLOW}未返回预期 ERROR（实际："
          f"{result.get('result') if result else '无响应'}）{C.END}")
    return 1


def run_interactive(args):
    """交互模式：循环提示输入程序号。"""
    print(f"{C.BOLD}========== 交互模式 =========={C.END}")
    print(f"设备 {args.host}:{args.port}，输入程序号回车触发（q 退出）")
    while True:
        try:
            pn = input("\n程序号> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not pn:
            continue
        if pn.lower() in ("q", "quit", "exit"):
            break
        args.program = pn
        run_full(args)
    print("已退出。")


# ================= CLI =================

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-H", "--host",
                        default=os.environ.get("QVPS_HOST", "192.168.217.51"),
                        help="相机 IP（也可用环境变量 QVPS_HOST）")
    common.add_argument("-P", "--port", type=int,
                        default=int(os.environ.get("QVPS_PORT", DEFAULT_PORT)),
                        help="TCP 端口（默认 5000）")
    common.add_argument("-p", "--program", default="PN-001", help="程序号/检测方案号")
    common.add_argument("-t", "--timeout", type=float, default=15.0,
                        help="Socket 超时(秒)")
    common.add_argument("--trace-id", default=None,
                        help="自定义 trace_id（默认自动生成 UUID）")
    common.add_argument("-o", "--outdir", default="images", help="图片保存目录")
    common.add_argument("--http-port", type=int, default=80, help="HTTP 取图端口")
    common.add_argument("--no-http", action="store_true", help="不尝试 HTTP 下载图片")
    common.add_argument("--no-image", action="store_true", help="跳过图片获取步骤")

    p = argparse.ArgumentParser(
        parents=[common],
        description="防错视觉传感器 TCP 检测接口测试工具",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("full", parents=[common],
                   help="主流程测试：触发->结果->原图（默认）")
    sp_loop = sub.add_parser("loop", parents=[common], help="连续触发测试")
    sp_loop.add_argument("-n", "--count", type=int, default=10, help="触发次数")
    sp_loop.add_argument("-i", "--interval", type=float, default=1.0,
                         help="间隔(秒)")
    sub.add_parser("error", parents=[common], help="错误用例测试")
    sub.add_parser("interactive", parents=[common], help="交互模式")
    return p


def main(argv=None):
    enable_color()
    argv = list(sys.argv[1:] if argv is None else argv)
    # 未带子命令时默认为 full；这样全局参数放在子命令前后都能用
    known_cmds = {"full", "loop", "error", "interactive"}
    if not any(a in known_cmds for a in argv):
        argv.insert(0, "full")
    args = build_parser().parse_args(argv)
    cmd = args.cmd or "full"
    if cmd == "full":
        return run_full(args)
    if cmd == "loop":
        return run_loop(args)
    if cmd == "error":
        return run_error_cases(args)
    if cmd == "interactive":
        return run_interactive(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
