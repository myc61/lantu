# -*- coding: utf-8 -*-
"""运行日志按日期写到包目录 logs/chassis_following_YYYY-MM-DD.log，并打到当前终端。"""
import os
import threading
import time
from datetime import datetime

try:
    import rospkg
except ImportError:
    rospkg = None

_lock = threading.Lock()
_last = {}
_folder = None


def _log_dir():
    global _folder
    if _folder is None:
        if rospkg is not None:
            root = rospkg.RosPack().get_path("chassis_following")
        else:
            root = os.path.normpath(os.path.join(
                os.path.dirname(__file__), "..", ".."))
        _folder = os.path.join(root, "logs")
        if not os.path.isdir(_folder):
            os.makedirs(_folder)
    return _folder


def log_path(now=None):
    """当天的日志文件。跨过零点后下一条写入新文件。"""
    now = now or datetime.now()
    name = "chassis_following_%s.log" % now.strftime("%Y-%m-%d")
    return os.path.join(_log_dir(), name)


def _emit(level, msg, args):
    now = datetime.now()
    text = msg % args if args else msg
    line = "%s %s %s" % (now.strftime("%Y-%m-%d %H:%M:%S"), level, text)
    print(line, flush=True)
    with _lock:
        with open(log_path(now), "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _throttle(period, key):
    now = time.monotonic()
    last = _last.get(key)
    if last is not None and now - last < float(period):
        return False
    _last[key] = now
    return True


class _Log(object):
    def info(self, msg, *args):
        _emit("INFO", msg, args)

    def warn(self, msg, *args):
        _emit("WARN", msg, args)

    def error(self, msg, *args):
        _emit("ERROR", msg, args)

    def info_throttle(self, period, msg, *args):
        if _throttle(period, ("INFO", msg)):
            _emit("INFO", msg, args)

    def warn_throttle(self, period, msg, *args):
        if _throttle(period, ("WARN", msg)):
            _emit("WARN", msg, args)

    def error_throttle(self, period, msg, *args):
        if _throttle(period, ("ERROR", msg)):
            _emit("ERROR", msg, args)


log = _Log()
