#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTP 通讯压测客户端 (上行 / 下行)
=================================

针对部署在 chassis_following 上的 FTP 服务，进行链路吞吐与稳定性压测：

* **下行 (download)**: 从远端 shots 目录批量拉取图片，衡量下行带宽与响应延迟；
* **上行 (upload)**  : 向远端指定目录回传测试文件，衡量上行带宽；
* **并发 (concurrent)**: 多线程并行会话，验证服务端在压力下的表现；
* **报告**           : 输出吞吐 (Mbps)、每文件耗时、错误率、P50/P95/P99 延迟。

客户端仅依赖 Python 3 标准库 (ftplib)，无需在压测机安装 pyftpdlib。

用法示例::

    # 下行压测：并发 4 线程，每线程拉取 50 个文件，跑 3 轮
    python3 stress_test.py download --host 192.168.1.100 --port 2121 \
        --user stress --password stress123 \
        --concurrency 4 --files 50 --rounds 3

    # 上行压测：上传 20 个 1MB 随机文件，并发 2 线程
    python3 stress_test.py upload --host 192.168.1.100 --port 2121 \
        --user stress --password stress123 \
        --concurrency 2 --files 20 --size 1M --remote-dir /stress

    # 混合上下行
    python3 stress_test.py both --host 192.168.1.100 --port 2121 --anonymous
"""

from __future__ import annotations

import argparse
import ftplib
import io
import json
import logging
import os
import queue
import random
import statistics
import string
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class OpResult:
    """单次上传/下载操作的结果。"""

    ok: bool
    size: int
    duration: float
    filename: str
    error: Optional[str] = None

    @property
    def mbps(self) -> float:
        if self.duration <= 0:
            return 0.0
        return (self.size * 8 / 1e6) / self.duration


@dataclass
class RoundReport:
    """单轮压测汇总。"""

    kind: str  # download / upload
    round_idx: int
    results: List[OpResult] = field(default_factory=list)
    wall_time: float = 0.0

    def summarize(self) -> Dict[str, float]:
        ok_results = [r for r in self.results if r.ok]
        err_results = [r for r in self.results if not r.ok]
        total_bytes = sum(r.size for r in ok_results)
        durations = sorted(r.duration for r in ok_results) if ok_results else [0.0]
        latencies = [r.duration * 1000 for r in ok_results]

        def pct(p: float) -> float:
            if not latencies:
                return 0.0
            k = max(0, min(len(latencies) - 1, int(round(p / 100 * (len(latencies) - 1)))))
            return latencies[k]

        wall = self.wall_time if self.wall_time > 0 else sum(durations)
        return {
            "kind": self.kind,
            "round": self.round_idx,
            "total_ops": len(self.results),
            "ok_ops": len(ok_results),
            "err_ops": len(err_results),
            "err_rate": (len(err_results) / len(self.results)) if self.results else 0.0,
            "total_mb": total_bytes / 1024 / 1024,
            "wall_sec": wall,
            "throughput_mbps": (total_bytes * 8 / 1e6) / wall if wall > 0 else 0.0,
            "throughput_MBps": (total_bytes / 1024 / 1024) / wall if wall > 0 else 0.0,
            "avg_file_sec": statistics.mean(durations) if durations else 0.0,
            "p50_ms": pct(50),
            "p95_ms": pct(95),
            "p99_ms": pct(99),
        }


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def parse_size(text: str) -> int:
    """将 '1M' / '512K' / '2048' 解析为字节数。"""
    text = text.strip().upper()
    mult = 1
    if text.endswith("K"):
        mult, text = 1024, text[:-1]
    elif text.endswith("M"):
        mult, text = 1024 * 1024, text[:-1]
    elif text.endswith("G"):
        mult, text = 1024 * 1024 * 1024, text[:-1]
    return int(float(text) * mult)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def random_suffix(n: int = 6) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


# ---------------------------------------------------------------------------
# FTP 客户端封装
# ---------------------------------------------------------------------------

class FTPClient:
    """轻量 FTP 客户端封装，支持匿名/账号登录、目录列举、上传、下载。"""

    def __init__(self, host: str, port: int, user: Optional[str],
                 password: Optional[str], timeout: float = 30.0,
                 passive: bool = True):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.timeout = timeout
        self.passive = passive
        self._ftp: Optional[ftplib.FTP] = None

    def __enter__(self) -> "FTPClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> None:
        ftp = ftplib.FTP()
        ftp.connect(self.host, self.port, timeout=self.timeout)
        if self.user:
            ftp.login(self.user, self.password or "")
        else:
            ftp.login()  # anonymous
        ftp.set_pasv(self.passive)
        self._ftp = ftp

    def close(self) -> None:
        if self._ftp:
            try:
                self._ftp.quit()
            except Exception:
                try:
                    self._ftp.close()
                except Exception:
                    pass
            self._ftp = None

    @property
    def ftp(self) -> ftplib.FTP:
        if not self._ftp:
            raise RuntimeError("FTP 未连接")
        return self._ftp

    # ---- 高层操作 ----

    def list_files(self, remote_dir: str = "/", recursive: bool = True,
                   pattern: Optional[str] = None) -> List[str]:
        """列出远端文件，返回以 remote_dir 为前缀的完整远端路径。"""
        results: List[str] = []
        base = remote_dir.rstrip("/") or ""

        def _walk(path: str):
            entries: List[str] = []
            try:
                self.ftp.retrlines(f"LIST {path}", entries.append)
            except ftplib.error_perm as e:
                logging.warning("LIST %s 失败: %s", path, e)
                return
            for line in entries:
                parts = line.split(None, 8)
                if len(parts) < 9:
                    continue
                perm, name = parts[0], parts[8]
                full_remote = f"{path.rstrip('/')}/{name}"
                if perm.startswith("d"):
                    if recursive:
                        _walk(full_remote)
                else:
                    if pattern is None or _match(name, pattern):
                        results.append(full_remote)

        _walk(remote_dir if remote_dir.startswith("/") else "/" + remote_dir)
        return results

    def download(self, remote_path: str, local_path: Path) -> int:
        """下载单个文件，返回字节数。"""
        local_path.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with local_path.open("wb") as f:
            def _cb(chunk: bytes):
                nonlocal size
                size += len(chunk)
                f.write(chunk)
            self.ftp.retrbinary(f"RETR {remote_path}", _cb)
        return size

    def upload(self, local_data: bytes, remote_path: str) -> int:
        """上传内存字节流到远端，返回字节数。"""
        # 保证目录存在
        parent = remote_path.rsplit("/", 1)[0] or "/"
        self._mkdirtree(parent)
        self.ftp.storbinary(f"STOR {remote_path}", io.BytesIO(local_data))
        return len(local_data)

    def _mkdirtree(self, path: str) -> None:
        """递归创建远端目录（已存在则跳过）。"""
        if not path or path == "/":
            return
        parts = [p for p in path.split("/") if p]
        cur = ""
        for p in parts:
            cur += "/" + p
            # 先尝试 cwd, 成功说明已存在
            try:
                self.ftp.cwd(cur)
                self.ftp.cwd("/")  # 复位
                continue
            except ftplib.error_perm:
                pass
            # 尝试创建
            try:
                self.ftp.mkd(cur)
                logging.debug("MKD 创建成功: %s", cur)
            except ftplib.error_perm as e:
                msg = str(e).lower()
                # 常见“已存在”响应: 550 xxx: file exists / 521
                if "exist" in msg or "521" in msg:
                    logging.debug("MKD 目录已存在: %s", cur)
                    continue
                raise

    def size(self, remote_path: str) -> int:
        """获取远端文件字节数。SIZE 命令需先切换二进制模式 (TYPE I)。"""
        try:
            self.ftp.voidcmd("TYPE I")
            return self.ftp.size(remote_path) or 0
        except ftplib.all_errors:
            return 0


def _match(name: str, pattern: str) -> bool:
    import fnmatch
    return fnmatch.fnmatch(name, pattern)


# ---------------------------------------------------------------------------
# 下载压测
# ---------------------------------------------------------------------------

def download_worker(host: str, port: int, user: Optional[str], pwd: Optional[str],
                    remote_files: List[str], work_dir: Path,
                    file_queue: "queue.Queue[str]", results: List[OpResult],
                    lock: threading.Lock, worker_id: int, passive: bool) -> None:
    """单个下载 worker：持续从队列取文件，直到队列耗尽。"""
    log = logging.getLogger(f"DL-{worker_id}")
    try:
        with FTPClient(host, port, user, pwd, passive=passive) as client:
            log.debug("已连接, 开始下载")
            while True:
                try:
                    remote = file_queue.get_nowait()
                except queue.Empty:
                    break
                local = work_dir / f"w{worker_id}_{Path(remote).name}"
                t0 = time.perf_counter()
                err: Optional[str] = None
                size = 0
                try:
                    # remote 已是完整路径 (以 / 开头)
                    rp = remote if remote.startswith("/") else "/" + remote
                    size = client.download(rp, local)
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    log.warning("下载失败 %s: %s", remote, err)
                dur = time.perf_counter() - t0
                with lock:
                    results.append(OpResult(ok=err is None, size=size,
                                            duration=dur, filename=remote, error=err))
                try:
                    if local.exists():
                        local.unlink()
                except OSError:
                    pass
    except Exception as e:
        log.error("worker 连接异常: %s", e)


def run_download_round(cfg: argparse.Namespace, files: List[str],
                       work_dir: Path, round_idx: int) -> RoundReport:
    logging.info("🚀 [下行 round %d] 待下载文件=%d 并发=%d",
                 round_idx, len(files), cfg.concurrency)
    q: "queue.Queue[str]" = queue.Queue()
    for f in files:
        q.put(f)

    results: List[OpResult] = []
    lock = threading.Lock()
    t_start = time.perf_counter()

    with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
        futures = []
        for wid in range(cfg.concurrency):
            futures.append(pool.submit(
                download_worker, cfg.host, cfg.port, cfg.user, cfg.password,
                files, work_dir, q, results, lock, wid, cfg.active is False,
            ))
        for fu in as_completed(futures):
            try:
                fu.result()
            except Exception as e:
                logging.error("worker 抛出: %s", e)

    wall = time.perf_counter() - t_start
    rep = RoundReport(kind="download", round_idx=round_idx,
                      results=results, wall_time=wall)
    return rep


# ---------------------------------------------------------------------------
# 获取模式 (fetch): 按文件夹拉取图片到本地, 保留目录结构, 不删除
# ---------------------------------------------------------------------------

def fetch_worker(host: str, port: int, user: Optional[str], pwd: Optional[str],
                 file_queue: "queue.Queue[str]", local_root: Path,
                 results: List[OpResult], lock: threading.Lock,
                 worker_id: int, passive: bool, skip_existing: bool) -> None:
    """获取 worker: 从队列取远端文件, 下载到 local_root 并保留相对路径。"""
    log = logging.getLogger(f"FETCH-{worker_id}")
    try:
        with FTPClient(host, port, user, pwd, passive=passive) as client:
            while True:
                try:
                    remote = file_queue.get_nowait()
                except queue.Empty:
                    break
                # 保留从 FTP 根开始的完整相对路径 (包含 session 文件夹名)
                rel = remote.lstrip("/")
                local = local_root / rel

                # 断点跳过: 已存在且非空则不重传
                if skip_existing and local.exists() and local.stat().st_size > 0:
                    with lock:
                        results.append(OpResult(ok=True, size=local.stat().st_size,
                                                duration=0.0, filename=rel, error=None))
                    log.debug("跳过已存在: %s", rel)
                    continue

                local.parent.mkdir(parents=True, exist_ok=True)
                t0 = time.perf_counter()
                err: Optional[str] = None
                size = 0
                try:
                    rp = remote if remote.startswith("/") else "/" + remote
                    size = client.download(rp, local)
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    log.warning("获取失败 %s: %s", remote, err)
                    # 清理半成品
                    try:
                        if local.exists():
                            local.unlink()
                    except OSError:
                        pass
                dur = time.perf_counter() - t0
                with lock:
                    results.append(OpResult(ok=err is None, size=size,
                                            duration=dur, filename=rel, error=err))
                if err is None:
                    log.info("✓ %s (%.1f KB)", rel, size / 1024.0)
    except Exception as e:
        log.error("worker 连接异常: %s", e)


def run_fetch(cfg: argparse.Namespace, files: List[str],
              local_root: Path) -> RoundReport:
    logging.info("📥 [获取] 文件数=%d 目标目录=%s 并发=%d 断点跳过=%s",
                 len(files), local_root, cfg.concurrency,
                 "是" if cfg.skip_existing else "否")
    q: "queue.Queue[str]" = queue.Queue()
    for f in files:
        q.put(f)

    results: List[OpResult] = []
    lock = threading.Lock()
    t_start = time.perf_counter()

    with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
        futures = [
            pool.submit(fetch_worker, cfg.host, cfg.port, cfg.user, cfg.password,
                        q, local_root, results, lock, wid, cfg.passive,
                        cfg.skip_existing)
            for wid in range(cfg.concurrency)
        ]
        for fu in as_completed(futures):
            try:
                fu.result()
            except Exception as e:
                logging.error("worker 抛出: %s", e)

    wall = time.perf_counter() - t_start
    return RoundReport(kind="fetch", round_idx=1, results=results, wall_time=wall)


def list_sessions(cfg: argparse.Namespace, remote_dir: str,
                  pattern: str) -> List[Tuple[str, int, int]]:
    """列出远端 session 文件夹, 返回 [(名称, 图片数, 总字节), ...]。"""
    with FTPClient(cfg.host, cfg.port, cfg.user, cfg.password,
                   timeout=cfg.timeout, passive=cfg.passive) as c:
        entries: List[str] = []
        base = remote_dir if remote_dir.startswith("/") else "/" + remote_dir
        c.ftp.retrlines(f"LIST {base}", entries.append)
        dirs: List[str] = []
        for line in entries:
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            if parts[0].startswith("d"):
                dirs.append(parts[8])
        result: List[Tuple[str, int, int]] = []
        for d in sorted(dirs):
            full = f"{base.rstrip('/')}/{d}"
            imgs = c.list_files(full, recursive=False, pattern=pattern)
            total = 0
            for img in imgs:
                total += c.size(img)
            result.append((d, len(imgs), total))
        return result


# ---------------------------------------------------------------------------
# 上传压测
# ---------------------------------------------------------------------------

def make_payload(size: int, kind: str = "random") -> bytes:
    """生成上传用的 payload。"""
    if kind == "zero":
        return b"\x00" * size
    if kind == "pattern":
        unit = b"chassis_following_stress_"
        return (unit * (size // len(unit) + 1))[:size]
    return os.urandom(size)


def upload_worker(host: str, port: int, user: Optional[str], pwd: Optional[str],
                  payload: bytes, remote_dir: str, count: int,
                  results: List[OpResult], lock: threading.Lock,
                  worker_id: int, passive: bool) -> None:
    log = logging.getLogger(f"UL-{worker_id}")
    try:
        with FTPClient(host, port, user, pwd, passive=passive) as client:
            log.debug("已连接, 开始上传 %d 个文件", count)
            for i in range(count):
                name = f"stress_w{worker_id}_{int(time.time())}_{random_suffix()}.bin"
                remote = f"{remote_dir.rstrip('/')}/{name}"
                t0 = time.perf_counter()
                err: Optional[str] = None
                size = 0
                try:
                    size = client.upload(payload, remote)
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    log.warning("上传失败 %s: %s", remote, err)
                dur = time.perf_counter() - t0
                with lock:
                    results.append(OpResult(ok=err is None, size=size,
                                            duration=dur, filename=name, error=err))
    except Exception as e:
        log.error("worker 连接异常: %s", e)


def run_upload_round(cfg: argparse.Namespace, round_idx: int) -> RoundReport:
    payload = make_payload(cfg.size_bytes, cfg.payload)
    per_worker = max(1, cfg.files // cfg.concurrency)
    total = per_worker * cfg.concurrency
    logging.info("🚀 [上行 round %d] 单文件=%s 总数=%d 并发=%d 远端目录=%s",
                 round_idx, human(cfg.size_bytes), total, cfg.concurrency, cfg.remote_dir)

    results: List[OpResult] = []
    lock = threading.Lock()
    t_start = time.perf_counter()

    with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
        futures = [
            pool.submit(upload_worker, cfg.host, cfg.port, cfg.user, cfg.password,
                        payload, cfg.remote_dir, per_worker, results, lock, wid,
                        cfg.active is False)
            for wid in range(cfg.concurrency)
        ]
        for fu in as_completed(futures):
            try:
                fu.result()
            except Exception as e:
                logging.error("worker 抛出: %s", e)

    wall = time.perf_counter() - t_start
    return RoundReport(kind="upload", round_idx=round_idx,
                       results=results, wall_time=wall)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.2f}{unit}"
        n /= 1024
    return f"{n:.2f}TB"


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def print_report(reports: List[RoundReport], out_json: Optional[Path]) -> None:
    print()
    print("=" * 78)
    print("📋 压测汇总报告")
    print("=" * 78)
    header = f"{'轮次':>4} {'类型':<8} {'成功/总数':>10} {'错误率':>8} " \
             f"{'总流量':>10} {'耗时(s)':>9} {'Mbps':>9} {'MB/s':>9} {'P95(ms)':>9}"
    print(header)
    print("-" * 78)
    for r in reports:
        s = r.summarize()
        print(f"{s['round']:>4} {s['kind']:<8} "
              f"{s['ok_ops']:>4}/{s['total_ops']:<5} "
              f"{s['err_rate'] * 100:>7.2f}% "
              f"{s['total_mb']:>9.2f}MB "
              f"{s['wall_sec']:>9.3f} "
              f"{s['throughput_mbps']:>9.2f} "
              f"{s['throughput_MBps']:>9.2f} "
              f"{s['p95_ms']:>9.1f}")
    print("-" * 78)

    # 全局
    all_ok = [op for r in reports for op in r.results if op.ok]
    all_err = [op for r in reports for op in r.results if not op.ok]
    total_wall = sum(r.wall_time for r in reports)
    total_bytes = sum(op.size for op in all_ok)
    print(f"总计: 成功 {len(all_ok)} / 失败 {len(all_err)} | "
          f"总流量 {human(total_bytes)} | 总耗时 {total_wall:.2f}s | "
          f"平均吞吐 {(total_bytes * 8 / 1e6) / total_wall if total_wall else 0:.2f} Mbps")
    if all_ok:
        lat = sorted(op.duration * 1000 for op in all_ok)
        print(f"延迟(ms): min={lat[0]:.1f} "
              f"p50={lat[len(lat)//2]:.1f} "
              f"p95={lat[int(len(lat)*0.95)]:.1f} "
              f"p99={lat[int(len(lat)*0.99)]:.1f} "
              f"max={lat[-1]:.1f}")
    print("=" * 78)

    if out_json:
        data = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "rounds": [r.summarize() for r in reports],
            "totals": {
                "ok": len(all_ok),
                "err": len(all_err),
                "bytes": total_bytes,
                "wall_sec": total_wall,
                "mbps": (total_bytes * 8 / 1e6) / total_wall if total_wall else 0,
            },
        }
        out_json.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"💾 JSON 报告已写入 {out_json}")


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="FTP 上下行通讯压测客户端",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", default=None,
                        help="FTP 服务器地址 (必需, 可在配置文件 host 字段提供)")
    common.add_argument("--port", type=int, default=2121, help="FTP 端口")
    common.add_argument("--user", default=None, help="用户名 (省略则匿名)")
    common.add_argument("--password", default=None, help="密码")
    common.add_argument("--concurrency", type=int, default=4, help="并发 worker 数")
    common.add_argument("--rounds", type=int, default=1, help="压测轮次")
    common.add_argument("--files", type=int, default=20, help="每轮文件数")
    common.add_argument("--active", action="store_true",
                        help="使用主动模式 (默认被动)")
    common.add_argument("--timeout", type=float, default=30.0, help="连接/传输超时(s)")
    common.add_argument("--log-level", default="INFO")
    common.add_argument("--out-json", type=Path, default=None,
                        help="JSON 报告输出路径")
    common.add_argument("--config", type=Path, default=None,
                        help="JSON 配置文件, 命令行参数优先")

    # download
    pd = sub.add_parser("download", parents=[common], help="下行压测")
    pd.add_argument("--remote-dir", default="/", help="远端起始目录")
    pd.add_argument("--pattern", default="*.jpg", help="文件匹配模式")
    pd.add_argument("--work-dir", type=Path, default=None,
                    help="本地临时目录 (默认系统 tmp)")

    # upload
    pu = sub.add_parser("upload", parents=[common], help="上行压测")
    pu.add_argument("--remote-dir", default="/stress_uploads", help="远端上传目录")
    pu.add_argument("--size", default="1M", help="单文件大小 (支持 K/M/G)")
    pu.add_argument("--payload", choices=["random", "zero", "pattern"],
                    default="random", help="上传数据模式")

    # both
    pb = sub.add_parser("both", parents=[common], help="先上行后下行")
    pb.add_argument("--remote-dir", default="/stress_uploads", help="上行远端目录")
    pb.add_argument("--size", default="1M", help="上行单文件大小")
    pb.add_argument("--payload", choices=["random", "zero", "pattern"],
                    default="random")
    pb.add_argument("--download-dir", default="/", help="下行起始目录")
    pb.add_argument("--pattern", default="*.jpg", help="下行文件匹配模式")

    # fetch (按文件夹获取图片到本地, 保留结构)
    pf = sub.add_parser("fetch", parents=[common],
                        help="获取远端图片到本地(保留目录结构, 不删除)")
    pf.add_argument("--remote-dir", default="/",
                    help="远端文件夹(如 /2026-09-04_11-18-02), 默认整个根")
    pf.add_argument("--session", default=None,
                    help="session 文件夹名简写, 等价于 --remote-dir /<name>")
    pf.add_argument("--pattern", default="*.jpg", help="文件匹配模式")
    pf.add_argument("--local-dir", type=Path, default=None,
                    help="本地保存目录 (默认 ./fetched_shots)")
    pf.add_argument("--skip-existing", action="store_true",
                    help="跳过已存在且非空的文件(断点续传)")

    # list (列出远端 session 文件夹)
    pl = sub.add_parser("list", parents=[common], help="列出远端 session 文件夹")
    pl.add_argument("--remote-dir", default="/", help="起始目录")
    pl.add_argument("--pattern", default="*.jpg", help="图片匹配模式(用于计数)")

    return p


def probe_files(cfg: argparse.Namespace, remote_dir: str, pattern: str,
                want: int) -> List[str]:
    """从远端枚举可用文件，随机取样 want 个。"""
    logging.info("🔍 枚举远端文件: dir=%s pattern=%s ...", remote_dir, pattern)
    t0 = time.perf_counter()
    with FTPClient(cfg.host, cfg.port, cfg.user, cfg.password,
                   timeout=cfg.timeout, passive=not cfg.active) as c:
        files = c.list_files(remote_dir, recursive=True, pattern=pattern)
    logging.info("   发现 %d 个文件, 用时 %.2fs", len(files), time.perf_counter() - t0)
    if not files:
        raise RuntimeError(f"远端 {remote_dir} 下没有匹配 {pattern} 的文件")
    if want and want < len(files):
        files = random.sample(files, want)
    elif want and want > len(files):
        logging.warning("请求 %d 个文件, 远端仅有 %d 个, 将循环使用", want, len(files))
        files = [files[i % len(files)] for i in range(want)]
    return files


def apply_config_file(cfg: argparse.Namespace) -> None:
    """从 JSON 配置文件补齐未在命令行显式指定的字段。

    支持两种结构：
    1. 扫平键（共享）::

        {
            "host": "192.168.1.100",
            "port": 2121,
            "user": "stress",
            "password": "stress123",
            "concurrency": 4,
            "rounds": 3,
            "files": 50
        }

    2. 子命令分区（仅对相应子命令生效）::

        {
            "download": { "remote_dir": "/", "pattern": "*.jpg" },
            "upload":   { "remote_dir": "/stress_test", "size": "1M" },
            "both":     { ... }
        }

    命令行显式传入的值优先于配置文件。
    """
    path = getattr(cfg, "config", None)
    if not path:
        default = Path(__file__).resolve().parent / "client_config.json"
        if default.exists():
            path = default
    if not path or not Path(path).exists():
        return
    with Path(path).open("r", encoding="utf-8") as f:
        data = json.load(f)

    parser_defaults = _parser_defaults()
    cmd = getattr(cfg, "cmd", None)

    def _assign(source: Dict[str, object]) -> None:
        for k, v in source.items():
            if k.startswith("_") or isinstance(v, dict):
                continue
            attr = k.replace("-", "_")
            if not hasattr(cfg, attr):
                continue
            current = getattr(cfg, attr)
            if current is None or current == parser_defaults.get(attr, current):
                setattr(cfg, attr, v)

    # 全局字段
    _assign(data)
    # 子命令专属字段
    if cmd and isinstance(data.get(cmd), dict):
        _assign(data[cmd])
    # both 子命令也尊重 download/upload 子区
    if cmd == "both":
        for sub_key in ("download", "upload"):
            if isinstance(data.get(sub_key), dict):
                for k, v in data[sub_key].items():
                    if k.startswith("_"):
                        continue
                    attr = k.replace("-", "_")
                    # both 下 remote_dir/pattern/size 已在 both 子命令里，不覆盖
                    if attr in ("remote_dir", "pattern", "size", "payload"):
                        continue
                    if hasattr(cfg, attr) and getattr(cfg, attr) is None:
                        setattr(cfg, attr, v)

    logging.info("已加载客户端配置: %s", path)


_PARSER: Optional[argparse.ArgumentParser] = None


def _parser_defaults() -> Dict[str, object]:
    """缓存 parser 默认值，用于判断命令行是否显式指定。"""
    global _PARSER
    if _PARSER is None:
        _PARSER = build_parser()
    defaults: Dict[str, object] = {}
    for action in _PARSER._actions:
        if action.dest != "help":
            defaults[action.dest] = action.default
    # 子命令 parser 的默认值
    for sub in _PARSER._subparsers._group_actions[0].choices.values():  # type: ignore[attr-defined]
        for action in sub._actions:
            if action.dest != "help":
                defaults.setdefault(action.dest, action.default)
    return defaults


def main() -> int:
    global _PARSER
    _PARSER = build_parser()
    args = _PARSER.parse_args()
    setup_logging(args.log_level)

    # 归一化 cfg
    cfg = argparse.Namespace(**vars(args))
    apply_config_file(cfg)

    # host 为必填项 (命令行或配置文件提供)
    if not getattr(cfg, "host", None):
        logging.error("未指定 FTP 服务器地址: 请用 --host 或在配置文件中设置 host 字段")
        return 2

    if cfg.cmd in ("upload", "both"):
        cfg.size_bytes = parse_size(cfg.size)
    cfg.passive = not cfg.active

    # 展开路径中的环境变量 (Windows %TEMP% / Linux $HOME) 与 ~
    if getattr(cfg, "out_json", None):
        cfg.out_json = Path(os.path.expanduser(os.path.expandvars(str(cfg.out_json))))
    if getattr(cfg, "work_dir", None):
        cfg.work_dir = Path(os.path.expanduser(os.path.expandvars(str(cfg.work_dir))))
    if getattr(cfg, "local_dir", None):
        cfg.local_dir = Path(os.path.expanduser(os.path.expandvars(str(cfg.local_dir))))

    # list 子命令: 列出远端 session 文件夹后直接返回
    if cfg.cmd == "list":
        sessions = list_sessions(cfg, cfg.remote_dir, cfg.pattern)
        print()
        print(f"远端 {cfg.host}:{cfg.port} 目录 {cfg.remote_dir} 下的 session 文件夹:")
        print("-" * 56)
        print(f"{'文件夹':<26}{'图片数':>8}{'大小':>12}")
        print("-" * 56)
        grand_n, grand_b = 0, 0
        for name, n, b in sessions:
            print(f"{name:<26}{n:>8}{human(b):>12}")
            grand_n += n
            grand_b += b
        print("-" * 56)
        print(f"{'合计':<24}{grand_n:>8}{human(grand_b):>12}  ({len(sessions)} 个文件夹)")
        print()
        print("获取某个文件夹:  stress_test.py fetch --session <文件夹名>")
        return 0

    reports: List[RoundReport] = []

    if cfg.cmd == "download":
        work_dir = cfg.work_dir or Path(tempfile.mkdtemp(prefix="ftp_dl_"))
        work_dir.mkdir(parents=True, exist_ok=True)
        files = probe_files(cfg, cfg.remote_dir, cfg.pattern, cfg.files)
        for r in range(1, cfg.rounds + 1):
            reports.append(run_download_round(cfg, files, work_dir, r))

    elif cfg.cmd == "upload":
        for r in range(1, cfg.rounds + 1):
            reports.append(run_upload_round(cfg, r))

    elif cfg.cmd == "both":
        # 上行
        for r in range(1, cfg.rounds + 1):
            reports.append(run_upload_round(cfg, r))
        # 下行 (从上行的目录里拿回来)
        work_dir = Path(tempfile.mkdtemp(prefix="ftp_dl_"))
        try:
            files = probe_files(cfg, cfg.remote_dir, "*", cfg.files)
        except Exception as e:
            logging.warning("⚠️  无法枚举上行目录 %s: %s，改为从根目录拉取 jpg",
                            cfg.remote_dir, e)
            files = probe_files(cfg, "/", "*.jpg", cfg.files)
        for r in range(1, cfg.rounds + 1):
            rep = run_download_round(cfg, files, work_dir, r)
            rep.kind = "download-back"
            reports.append(rep)

    elif cfg.cmd == "fetch":
        # 确定远端目录: --session 优先于 --remote-dir
        remote_dir = cfg.remote_dir
        if getattr(cfg, "session", None):
            remote_dir = "/" + cfg.session.strip("/")
        # 本地保存目录
        local_root = cfg.local_dir or Path("fetched_shots")
        local_root.mkdir(parents=True, exist_ok=True)
        # 获取全部匹配文件 (want=0 表示不限数量)
        files = probe_files(cfg, remote_dir, cfg.pattern, 0)
        rep = run_fetch(cfg, files, local_root)
        reports.append(rep)
        s = rep.summarize()
        print()
        print("=" * 60)
        print("📦 获取完成")
        print("=" * 60)
        print(f"  远端目录 : {remote_dir}")
        print(f"  本地目录 : {local_root.resolve()}")
        print(f"  成功/总数: {s['ok_ops']}/{s['total_ops']}  错误率 {s['err_rate'] * 100:.2f}%")
        print(f"  总大小   : {human(sum(op.size for op in rep.results if op.ok))}")
        print(f"  耗时     : {s['wall_sec']:.2f}s  吞吐 {s['throughput_mbps']:.2f} Mbps")
        print("=" * 60)
        # fetch 模式下 probe_files 失败会抛异常, 这里不重复报告
        if cfg.out_json:
            print_report(reports, cfg.out_json)
        return 0 if s["err_ops"] == 0 else 1

    print_report(reports, cfg.out_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
