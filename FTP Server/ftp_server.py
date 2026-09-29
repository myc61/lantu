#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chassis_following FTP 服务端
================================

用途：将 ``chassis_following/shots/`` 下的采集图片以 FTP 服务方式对外发布，
供远端压测机下载（下行）与回传（上行），用于评估链路吞吐能力。

依赖::

    pip install pyftpdlib>=2.0

启动示例::

    # 匿名只读，端口 2121，根目录默认 ../shots
    python3 ftp_server.py

    # 带账号 + 允许上传 + 指定端口
    python3 ftp_server.py --port 2121 --user stress --password stress123 \
        --root ../shots --allow-upload

    # 使用配置文件
    python3 ftp_server.py --config server_config.json

作者: chassis_following 项目组
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import stat as stat_module
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from pyftpdlib.authorizers import AuthenticationFailed, DummyAuthorizer
    from pyftpdlib.filesystems import AbstractedFS
    from pyftpdlib.handlers import FTPHandler, DTPHandler
    from pyftpdlib.servers import ThreadedFTPServer
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        "[FATAL] 未检测到 pyftpdlib, 请先执行: pip install pyftpdlib\n"
        f"        原始错误: {exc}\n"
    )
    sys.exit(2)


# ---------------------------------------------------------------------------
# 全局统计
# ---------------------------------------------------------------------------

@dataclass
class TrafficStats:
    """服务端累计流量统计（跨会话）。"""

    uploaded_bytes: int = 0
    downloaded_bytes: int = 0
    upload_files: int = 0
    download_files: int = 0
    sessions: int = 0
    errors: int = 0
    start_time: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_download(self, nbytes: int) -> None:
        with self._lock:
            self.downloaded_bytes += max(0, nbytes)
            self.download_files += 1

    def add_upload(self, nbytes: int) -> None:
        with self._lock:
            self.uploaded_bytes += max(0, nbytes)
            self.upload_files += 1

    def add_error(self) -> None:
        with self._lock:
            self.errors += 1

    def snapshot(self) -> Dict[str, float]:
        with self._lock:
            elapsed = max(1e-6, time.time() - self.start_time)
            return {
                "elapsed_sec": elapsed,
                "uploaded_mb": self.uploaded_bytes / 1024.0 / 1024.0,
                "downloaded_mb": self.downloaded_bytes / 1024.0 / 1024.0,
                "upload_files": self.upload_files,
                "download_files": self.download_files,
                "sessions": self.sessions,
                "errors": self.errors,
                "up_mbps": (self.uploaded_bytes * 8 / 1e6) / elapsed,
                "down_mbps": (self.downloaded_bytes * 8 / 1e6) / elapsed,
            }


STATS = TrafficStats()


# ---------------------------------------------------------------------------
# 日志格式
# ---------------------------------------------------------------------------

class _ColorFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[35m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:  # noqa: D102
        msg = super().format(record)
        color = self.COLORS.get(record.levelname)
        return f"{color}{msg}{self.RESET}" if color else msg


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> None:
    """初始化控制台 + 可选文件日志。"""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    fmt_str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_ColorFormatter(fmt_str, datefmt="%H:%M:%S"))
    root.addHandler(console)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter(fmt_str))
        root.addHandler(fh)

    # 降低 pyftpdlib 内部日志噪音
    logging.getLogger("pyftpdlib").setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# 自定义 Handler：统计上下行流量
# ---------------------------------------------------------------------------

class MountableFS(AbstractedFS):
    """支持将 FTP 命名空间下的某个子目录挂载到外部可写路径。

    用途：当 FTP root (例如 shots/) 为只读时，仍允许客户端上传到
    一个独立的可写目录 (例如 FTP Server/uploads/)，该目录在 FTP 中
    呈现为 ``/stress_uploads``。

    类变量 ``mounts`` 由服务端启动时填充：
        {"/stress_uploads": "/abs/path/to/uploads"}
    """

    mounts: Dict[str, str] = {}

    # -------- FTP 路径 -> 文件系统路径 --------
    def ftp2fs(self, ftppath: str) -> str:
        norm = self.ftpnorm(ftppath)
        for mount_ftp, real_dir in self.mounts.items():
            mount_norm = self.ftpnorm(mount_ftp)
            if norm == mount_norm:
                return real_dir
            prefix = mount_norm.rstrip("/") + "/"
            if norm.startswith(prefix):
                rel = norm[len(prefix):]
                return os.path.normpath(os.path.join(real_dir, rel))
        return super().ftp2fs(ftppath)

    # -------- 文件系统路径 -> FTP 路径 --------
    def fs2ftp(self, fspath: str) -> str:
        real_fspath = os.path.realpath(fspath)
        for mount_ftp, real_dir in self.mounts.items():
            real_dir_abs = os.path.realpath(real_dir)
            if real_fspath == real_dir_abs:
                return mount_ftp
            prefix = real_dir_abs.rstrip("/") + "/"
            if real_fspath.startswith(prefix):
                rel = real_fspath[len(prefix):]
                return mount_ftp.rstrip("/") + "/" + rel
        return super().fs2ftp(fspath)

    def validpath(self, path: str) -> bool:
        """挂载目录下的路径不走 root 校验。"""
        real = os.path.realpath(path)
        for real_dir in self.mounts.values():
            real_dir_abs = os.path.realpath(real_dir)
            if real == real_dir_abs or real.startswith(real_dir_abs + os.sep):
                return True
        return super().validpath(path)

    def listdir(self, path: str):
        """列目录时注入虚拟挂载点，使 /Image_TEST 等在 LIST / 中可见。

        挂载点是 FTP 命名空间里的虚拟目录，物理上不存在于 root/homedir，
        若不注入，客户端遍历根目录时无法发现它们 (尽管直接 CWD/RETR 可用)。
        """
        listing = super().listdir(path)
        try:
            norm = self.ftpnorm(self.fs2ftp(path))
        except (OSError, ValueError):
            return listing
        if norm != "/":
            return listing
        existing = {e for e in listing if not e.startswith(".")}
        for mount_ftp in self.mounts:
            name = mount_ftp.strip("/")
            # 仅注入单层虚拟目录名，且避免与真实条目重复
            if name and "/" not in name and name not in existing:
                try:
                    st = self.lstat(self.ftp2fs(mount_ftp))
                except OSError:
                    continue
                if stat_module.S_ISDIR(st.st_mode):
                    listing.append(name)
        return listing

    def format_list(self, basedir: str, listing, ignore_err: bool = True):
        """LIST 格式化时解析虚拟挂载点。

        父类实现会对每个条目做 lstat(os.path.join(basedir, name))，
        而 listdir 注入的虚拟挂载点 (如 Image_TEST) 在物理 homedir 下
        并不存在，会被静默丢弃。这里在根目录列表中将虚拟名映射到
        真实挂载路径后再 stat，并按 "ls -lA" 同样格式输出。
        """
        try:
            is_root = self.ftpnorm(self.fs2ftp(basedir)) == "/"
        except (OSError, ValueError):
            is_root = False
        if not is_root or not self.mounts:
            return super().format_list(basedir, listing, ignore_err)

        virtual = {m.strip("/"): m for m in self.mounts
                   if m.strip("/") and "/" not in m.strip("/")}
        timefunc = time.gmtime if self.cmd_channel.use_gmt_times else time.localtime
        six_months = 180 * 24 * 60 * 60
        now = time.time()
        months_map = {n: m for n, m in enumerate(
            ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

        def _fmt_line(st: "os.stat_result", basename: str) -> bytes:
            perms = stat_module.filemode(st.st_mode)
            nlinks = st.st_nlink or 1
            try:
                uname = self.get_user_by_uid(st.st_uid)
                gname = self.get_group_by_gid(st.st_gid)
            except (AttributeError, KeyError):
                uname, gname = "owner", "group"
            mtime = timefunc(st.st_mtime)
            fmtstr = "%d  %Y" if now - st.st_mtime > six_months else "%d %H:%M"
            mtimestr = "%s %s" % (months_map[mtime.tm_mon],
                                  time.strftime(fmtstr, mtime))
            line = "%s %3s %-8s %-8s %8s %s %s\r\n" % (
                perms, nlinks, uname, gname, st.st_size, mtimestr, basename)
            return line.encode(self.cmd_channel.encoding,
                               self.cmd_channel.unicode_errors)

        def _generate():
            for basename in listing:
                display = basename
                if basename in virtual:
                    # 虚拟挂载点: 用真实目录的 stat 信息展示
                    try:
                        st = self.lstat(self.ftp2fs(virtual[basename]))
                    except OSError:
                        if ignore_err:
                            continue
                        raise
                    yield _fmt_line(st, display)
                    continue
                file = os.path.join(basedir, basename)
                try:
                    st = self.lstat(file)
                except OSError:
                    if ignore_err:
                        continue
                    raise
                islink = stat_module.S_ISLNK(st.st_mode)
                if islink:
                    try:
                        display = basename + " -> " + os.readlink(file)
                    except OSError:
                        pass
                yield _fmt_line(st, display)

        return _generate()


class StatsDTPHandler(DTPHandler):
    """DTP（数据通道）处理器。

    注意：pyftpdlib 对 RETR 使用零拷贝 sendfile()，会绕过 send()，
    因此下行流量统计改在 FTPHandler.on_file_sent 回调中按文件大小累加；
    上行（STOR）同理在 on_file_received 中统计，保证两个方向口径一致。
    """
    pass


class StatsFTPHandler(FTPHandler):
    """扩展 FTPHandler，注入统计与更详细的连接日志。"""

    # 使用带统计的 DTP handler
    dtp_handler = StatsDTPHandler

    # 使用支持挂载点的文件系统抽象
    abstracted_fs = MountableFS

    # 增大被动端口范围以支持并发压测
    passive_ports = range(60000, 60100)

    def on_connect(self) -> None:
        STATS.sessions += 1
        logging.info("📡 客户端已连接: %s:%s (会话累计=%d)",
                     self.remote_ip, self.remote_port, STATS.sessions)

    def on_disconnect(self) -> None:
        logging.info("🔌 客户端已断开: %s:%s", self.remote_ip, self.remote_port)

    def on_login(self, username: str) -> None:
        logging.info("🔓 登录成功 user=%s ip=%s", username, self.remote_ip)

    def on_login_failed(self, username: str, password: str) -> None:
        STATS.add_error()
        logging.warning("⛔ 登录失败 user=%s ip=%s", username, self.remote_ip)

    def on_file_sent(self, file: str) -> None:
        # pyftpdlib 回调签名为 on_file_sent(self, file)；RETR 走 sendfile 绕过 send()，
        # 故在此按文件大小累加下行流量
        try:
            size = os.path.getsize(file)
        except OSError:
            size = 0
        STATS.add_download(size)
        logging.info("⬇️  下行完成 %s (%.2f KB) -> %s",
                     file, size / 1024.0, self.remote_ip)

    def on_file_received(self, file: str) -> None:
        try:
            size = os.path.getsize(file)
        except OSError:
            size = 0
        STATS.add_upload(size)
        logging.info("⬆️  上行完成 %s (%.2f KB) <- %s",
                     file, size / 1024.0, self.remote_ip)

    def on_error(self) -> None:
        STATS.add_error()
        logging.exception("❌ FTP 会话异常 ip=%s", self.remote_ip)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def get_local_ip() -> str:
    """探测本机对外 IP（不发起真实连接）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def describe_root(root: Path) -> str:
    """统计根目录下 jpg/png 数量，用于启动横幅。"""
    total_files, total_bytes = 0, 0
    for p in root.rglob("*"):
        if p.is_file():
            total_files += 1
            try:
                total_bytes += p.stat().st_size
            except OSError:
                pass
    return f"{total_files} 个文件, {total_bytes / 1024 / 1024:.2f} MB"


def log_sessions_overview(root: Path, limit: int = 12) -> None:
    """列出根目录下的 session 文件夹及各自图片数，便于运维确认可获取内容。"""
    img_exts = {".jpg", ".jpeg", ".png", ".bmp"}
    sessions = []
    try:
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            n = sum(1 for f in d.iterdir()
                    if f.is_file() and f.suffix.lower() in img_exts)
            sessions.append((d.name, n))
    except OSError as e:
        logging.warning("无法枚举 session 目录: %s", e)
        return
    if not sessions:
        return
    total_imgs = sum(n for _, n in sessions)
    logging.info("📁 可获取的 session 文件夹 (%d 个, 共 %d 张图片):",
                 len(sessions), total_imgs)
    for name, n in sessions[:limit]:
        logging.info("     %-26s %d 张", name, n)
    if len(sessions) > limit:
        logging.info("     ... 及其余 %d 个", len(sessions) - limit)


def build_authorizer(cfg: Dict) -> DummyAuthorizer:
    """按配置构造认证器。"""
    authorizer = DummyAuthorizer()
    root = cfg["root"]
    perm = "elradfmwMT" if cfg["allow_upload"] else "elr"

    # 匿名用户
    if cfg["anonymous"]:
        authorizer.add_anonymous(root, perm=("elr" if not cfg["allow_upload"] else "elradfmwMT"))
        logging.info("已启用匿名访问, 权限=%s", "读写" if cfg["allow_upload"] else "只读")

    # 具名用户
    for user in cfg["users"]:
        authorizer.add_user(
            user["username"],
            user["password"],
            user.get("homedir", root),
            perm=perm,
        )
        logging.info("已添加用户 %s (权限=%s)", user["username"], perm)

    if not cfg["anonymous"] and not cfg["users"]:
        raise ValueError("未配置任何用户，请至少启用 --anonymous 或通过 --user 指定账号")

    return authorizer


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "shots"

# 内置默认值。
# parse_args 里所有可被配置文件覆盖的参数，argparse default 一律设为 None，
# 这样才能区分「用户没传」和「用户显式传了」——否则配置文件会连显式
# 命令行参数一起盖掉（argparse 本身无法区分默认值与显式传值）。
# 真实默认值集中在这里，也是 --help 里展示的值。
DEFAULTS: Dict[str, Any] = {
    "host": "0.0.0.0",
    "port": 2121,
    "root": str(DEFAULT_ROOT),
    "anonymous": False,
    "allow_upload": False,
    "upload_root": None,
    "upload_mount": "/stress_uploads",
    "max_conns": 64,
    "stats_interval": 15,
    "users": [],
    "extra_mounts": {},
}

# argparse 属性名 -> 配置键名。只有列在这里的键支持用命令行覆盖配置文件。
# 注: anonymous / allow_upload 是 store_true，命令行只能把它们从 False 开到 True，
#     无法反向关闭配置文件里的 true（需要关就直接改配置文件）。
CLI_OVERRIDES: Dict[str, str] = {
    "host": "host",
    "port": "port",
    "root": "root",
    "anonymous": "anonymous",
    "allow_upload": "allow_upload",
    "upload_root": "upload_root",
    "upload_mount": "upload_mount",
    "max_conns": "max_conns",
    "stats_interval": "stats_interval",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="chassis_following FTP 服务端 (基于 pyftpdlib)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host", default=None,
                   help=f"监听地址 (默认 {DEFAULTS['host']})")
    p.add_argument("--port", type=int, default=None,
                   help=f"监听端口 (默认 {DEFAULTS['port']})")
    p.add_argument("--root", type=Path, default=None,
                   help=f"FTP 根目录 (默认 {DEFAULT_ROOT})")
    p.add_argument("--anonymous", action="store_true", default=None,
                   help="启用匿名访问 (只读, 除非同时指定 --allow-upload)")
    p.add_argument("--user", action="append", default=[],
                   help="添加用户 user:pass, 可重复 (追加, 不覆盖配置文件里的 users)")
    p.add_argument("--allow-upload", action="store_true", default=None,
                   help="允许上传 (用于上行压测)")
    p.add_argument("--upload-root", type=Path, default=None,
                   help="独立的可写上传目录 (默认: <脚本目录>/uploads)。"
                        "当 FTP root 不可写时仍允许上行压测")
    p.add_argument("--upload-mount", default=None,
                   help=f"上传目录在 FTP 命名空间下的挂载路径"
                        f" (默认 {DEFAULTS['upload_mount']})")
    p.add_argument("--max-conns", type=int, default=None,
                   help=f"最大并发连接数 (默认 {DEFAULTS['max_conns']})")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--log-file", default=None,
                   help="日志输出文件, 默认仅控制台")
    p.add_argument("--config", type=Path, default=None,
                   help="JSON 配置文件路径，命令行显式传入的参数会覆盖同名项")
    p.add_argument("--stats-interval", type=int, default=None,
                   help=f"周期性打印流量统计的间隔秒数, 0 表示关闭"
                        f" (默认 {DEFAULTS['stats_interval']})")
    return p.parse_args()


def load_config(args: argparse.Namespace) -> Dict:
    """合并内置默认、配置文件与命令行参数。

    优先级: 命令行显式参数 > 配置文件 > DEFAULTS 内置默认。
    """
    script_dir = Path(__file__).resolve().parent
    # 从内置默认起步; list/dict 必须拷贝，否则后续修改会污染模块级 DEFAULTS
    cfg: Dict[str, Any] = {
        k: (v.copy() if isinstance(v, (list, dict)) else v)
        for k, v in DEFAULTS.items()
    }

    config_dir: Optional[Path] = None
    if args.config and args.config.exists():
        config_dir = args.config.resolve().parent
        with args.config.open("r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if k.startswith("_"):
                continue
            if k in cfg and v is not None:
                cfg[k] = v
        logging.info("已加载配置文件 %s", args.config)

    # 命令行显式传入的参数最后覆盖，压过配置文件。
    # default=None 时值为 None 即代表用户根本没传，不参与覆盖。
    overridden = []
    for arg_name, key in CLI_OVERRIDES.items():
        v = getattr(args, arg_name, None)
        if v is None:
            continue
        cfg[key] = str(v) if isinstance(v, Path) else v
        overridden.append(key)
    if overridden:
        logging.info("命令行覆盖配置项: %s", ", ".join(overridden))

    # 相对路径以配置文件所在目录为基准，否则以脚本目录为基准
    base = config_dir or script_dir
    root_path = Path(cfg["root"])
    if not root_path.is_absolute():
        root_path = base / root_path
    cfg["root"] = str(root_path.resolve())

    # upload_root 默认值: <脚本目录>/uploads
    if not cfg.get("upload_root"):
        cfg["upload_root"] = str(script_dir / "uploads")
    else:
        ur = Path(cfg["upload_root"])
        if not ur.is_absolute():
            ur = base / ur
        cfg["upload_root"] = str(ur.resolve())

    # 命令行 --user 追加
    for item in args.user:
        if ":" not in item:
            raise ValueError(f"--user 参数格式应为 user:pass, 收到: {item}")
        u, pwd = item.split(":", 1)
        cfg["users"].append({"username": u, "password": pwd, "homedir": cfg["root"]})

    # 配置文件中用户的 homedir 同样解析
    for u in cfg["users"]:
        hd = u.get("homedir")
        if hd and not Path(hd).is_absolute():
            u["homedir"] = str((base / hd).resolve())
        elif not hd:
            u["homedir"] = cfg["root"]

    # extra_mounts: 额外的 "FTP 路径 -> 本地目录" 挂载 (如 /Image_TEST)，
    # 使 root 之外的目录对所有账号可见
    mounts: Dict[str, str] = {}
    for ftp_path, local_dir in (cfg.get("extra_mounts") or {}).items():
        p = Path(local_dir)
        if not p.is_absolute():
            p = base / p
        mounts[ftp_path] = str(p.resolve())
    cfg["extra_mounts"] = mounts
    return cfg


def stats_reporter(interval: int, stop_event: threading.Event) -> None:
    """后台线程：定期打印流量汇总。"""
    while not stop_event.wait(interval):
        s = STATS.snapshot()
        logging.info(
            "📊 累计: 会话=%d 下行=%.2fMB(%.2fMbps) 上行=%.2fMB(%.2fMbps) 错误=%d",
            s["sessions"], s["downloaded_mb"], s["down_mbps"],
            s["uploaded_mb"], s["up_mbps"], s["errors"],
        )


def main() -> int:
    args = parse_args()
    setup_logging(args.log_level, args.log_file)
    cfg = load_config(args)

    root = Path(cfg["root"])
    if not root.exists():
        logging.error("FTP 根目录不存在: %s", root)
        return 1
    if not root.is_dir():
        logging.error("FTP 根目录不是目录: %s", root)
        return 1

    authorizer = build_authorizer(cfg)

    # 额外挂载点: 让 root 之外的目录 (如 Image_TEST) 出现在 FTP 命名空间，
    # 所有账号 (含 stress) 均可通过 /Image_TEST 访问
    MountableFS.mounts = {}
    for ftp_path, local_dir in cfg["extra_mounts"].items():
        if not Path(local_dir).is_dir():
            logging.error("挂载目录不存在: %s -> %s", ftp_path, local_dir)
            return 1
        MountableFS.mounts[ftp_path] = local_dir
        logging.info("已挂载目录: %s -> %s", ftp_path, local_dir)

    # 配置挂载点: 当 root 不可写时，用独立的 upload_root 承接上行
    root_writable = os.access(cfg["root"], os.W_OK)
    if cfg["allow_upload"]:
        upload_root = Path(cfg["upload_root"])
        try:
            upload_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logging.error("无法创建 upload_root %s: %s", upload_root, e)
            return 1
        MountableFS.mounts[cfg["upload_mount"]] = str(upload_root)
        if not root_writable:
            logging.warning(
                "⚠️  FTP root %s 对当前用户不可写，已启用挂载点: %s -> %s",
                cfg["root"], cfg["upload_mount"], upload_root,
            )
        else:
            # root 可写时，仍将挂载点指向 upload_root，避免污染 shots/
            logging.info("上传挂载点: %s -> %s", cfg["upload_mount"], upload_root)

    handler = StatsFTPHandler
    handler.authorizer = authorizer
    handler.banner = (
        f"chassis_following FTP ready. root={root.name} "
        f"upload={'on' if cfg['allow_upload'] else 'off'}"
    )

    server = ThreadedFTPServer(
        (cfg["host"], cfg["port"]),
        handler,
    )
    server.max_cons = cfg["max_conns"]
    server.max_cons_per_ip = cfg["max_conns"]

    local_ip = get_local_ip()
    logging.info("=" * 68)
    logging.info("  chassis_following FTP Server 已启动")
    logging.info("  监听地址 : ftp://%s:%d", cfg["host"], cfg["port"])
    logging.info("  局域网   : ftp://%s:%d", local_ip, cfg["port"])
    logging.info("  根目录   : %s (%s)", root, describe_root(root))
    logging.info("  根可写   : %s", "是" if root_writable else "否 (已启用挂载点)")
    for ftp_path, local_dir in cfg["extra_mounts"].items():
        logging.info("  额外挂载 : %s -> %s", ftp_path, local_dir)
    if cfg["allow_upload"]:
        logging.info("  上传挂载 : %s -> %s", cfg["upload_mount"], cfg["upload_root"])
    logging.info("  被动端口 : %d-%d", 60000, 60100)
    logging.info("  上传权限 : %s", "允许" if cfg["allow_upload"] else "禁止")
    logging.info("  最大连接 : %d", cfg["max_conns"])
    logging.info("=" * 68)
    log_sessions_overview(root)

    stop_event = threading.Event()
    stats_thread: Optional[threading.Thread] = None
    if cfg["stats_interval"] > 0:
        stats_thread = threading.Thread(
            target=stats_reporter,
            args=(cfg["stats_interval"], stop_event),
            daemon=True,
            name="stats-reporter",
        )
        stats_thread.start()

    def _graceful_exit(signum, _frame):
        logging.info("收到信号 %s, 正在关闭服务...", signum)
        stop_event.set()
        threading.Thread(target=server.close_all, daemon=True).start()

    signal.signal(signal.SIGINT, _graceful_exit)
    signal.signal(signal.SIGTERM, _graceful_exit)

    try:
        server.serve_forever(timeout=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.close_all()
        s = STATS.snapshot()
        logging.info(
            "🏁 服务结束. 会话=%d 下行=%.2fMB 上行=%.2fMB 错误=%d 运行时长=%.1fs",
            s["sessions"], s["downloaded_mb"], s["uploaded_mb"],
            s["errors"], s["elapsed_sec"],
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
