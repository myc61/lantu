# -*- coding: utf-8 -*-
"""删除超过保留天数的运行日志和照片。当天正在写的日志不删。"""
import os
import stat
from datetime import datetime


def purge_expired(log_dir, capture_dir, days=7, keep_dirs=()):
    """返回删除数量：logs 个日志文件，images 张图片，dirs 个空目录。"""
    days = int(days)
    if days < 1:
        return {"logs": 0, "images": 0, "dirs": 0}
    cutoff = time_cutoff(days)
    keep = set()
    for path in keep_dirs or ():
        if path:
            keep.add(os.path.abspath(path))
    logs = _purge_logs(log_dir, cutoff, keep)
    images, dirs = _purge_shots(capture_dir, cutoff, keep)
    return {"logs": logs, "images": images, "dirs": dirs}


def time_cutoff(days, now=None):
    now = time_now() if now is None else now
    return now - float(days) * 86400.0


def time_now():
    return datetime.now().timestamp()


def _today_log_name(now=None):
    now = datetime.now() if now is None else datetime.fromtimestamp(now)
    return "chassis_following_%s.log" % now.strftime("%Y-%m-%d")


def _purge_logs(log_dir, cutoff, keep):
    if not log_dir or not os.path.isdir(log_dir):
        return 0
    today = _today_log_name()
    removed = 0
    for name in os.listdir(log_dir):
        path = os.path.join(log_dir, name)
        if name == today or os.path.abspath(path) in keep:
            continue
        if not _is_file(path):
            continue
        try:
            if os.path.getmtime(path) >= cutoff:
                continue
            os.remove(path)
            removed += 1
        except OSError:
            continue
    return removed


def _purge_shots(root, cutoff, keep):
    if not root or not os.path.isdir(root):
        return 0, 0
    root = os.path.abspath(root)
    images = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if _kept(dirpath, keep):
            dirnames[:] = []
            continue
        for name in filenames:
            path = os.path.join(dirpath, name)
            if not _is_image(name) or not _is_file(path):
                continue
            try:
                if os.path.getmtime(path) >= cutoff:
                    continue
                os.remove(path)
                images += 1
            except OSError:
                continue
    _drop_spent_runs(root, cutoff, keep)
    dirs = _remove_empty_dirs(root, keep)
    return images, dirs


def _drop_spent_runs(root, cutoff, keep):
    """照片已清空的旧目录，连带删掉里面的 csv。还有新照片的目录不动。"""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if os.path.abspath(dirpath) == root or _kept(dirpath, keep):
            continue
        if any(_is_image(name) for name in filenames):
            continue
        for name in filenames:
            path = os.path.join(dirpath, name)
            if not _is_file(path):
                continue
            try:
                if os.path.getmtime(path) >= cutoff:
                    continue
                os.remove(path)
            except OSError:
                continue


def _remove_empty_dirs(root, keep):
    removed = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        if os.path.abspath(dirpath) == root or _kept(dirpath, keep):
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
                removed += 1
        except OSError:
            continue
    return removed


def _kept(path, keep):
    path = os.path.abspath(path)
    for item in keep:
        if path == item or path.startswith(item + os.sep):
            return True
    return False


def _is_file(path):
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode)


def _is_image(name):
    return name.lower().endswith((".jpg", ".jpeg", ".png"))
