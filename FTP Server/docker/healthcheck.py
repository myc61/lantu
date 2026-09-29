#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""容器健康检查。

不只是探测端口通不通，而是完整跑一遍 FTP 会话:
    连接 -> 登录 -> LIST / -> 校验虚拟挂载点是否出现在列表里

最后一步是关键: MountableFS 的虚拟挂载点 (Image_TEST / stress_uploads)
物理上不在用户 homedir 下，pyftpdlib 的 AbstractedFS.format_list 会对每个
条目做 lstat 并静默丢弃失败项，所以"能登录能下载"并不代表 LIST 正常。
健康检查直接盯住这个历史故障点。

端口、账号、期望挂载点全部从配置文件反推，不硬编码，
改了 server_config.docker.json 无需同步改这里。
例外: 如果用 FTP_EXTRA_ARGS="--port NNNN" 临时改了端口（命令行优先级高于配置文件），
配置文件里仍是旧值，这时需要同时设 FTP_HC_PORT=NNNN 让本脚本跟上。
"""

import ftplib
import json
import os
import sys

CONFIG = os.environ.get("FTP_CONFIG", "/app/ftp/docker/server_config.docker.json")
HOST = os.environ.get("FTP_HC_HOST", "127.0.0.1")
PORT_OVERRIDE = os.environ.get("FTP_HC_PORT", "").strip()
TIMEOUT = float(os.environ.get("FTP_HC_TIMEOUT", "10"))


def load_expectations():
    """从配置文件推导 (端口, 用户名, 密码, 期望在根目录出现的挂载点名)。"""
    with open(CONFIG, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    port = int(PORT_OVERRIDE) if PORT_OVERRIDE else int(cfg.get("port", 2121))
    users = cfg.get("users") or []
    if not users:
        raise RuntimeError("配置中没有具名用户，无法健康检查（匿名模式请自行调整）")

    expect = set()
    for ftp_path in (cfg.get("extra_mounts") or {}):
        expect.add(ftp_path.strip("/").split("/")[-1])
    if cfg.get("allow_upload") and cfg.get("upload_mount"):
        expect.add(cfg["upload_mount"].strip("/").split("/")[-1])

    return port, users[0]["username"], users[0]["password"], expect


def list_root(ftp):
    """用 LIST (而非 NLST) 取根目录，确保走的是 format_list 代码路径。"""
    lines = []
    ftp.retrlines("LIST", lines.append)
    names = []
    for line in lines:
        if not line.strip():
            continue
        # ls -lA 格式: 权限 链接数 属主 属组 大小 月 日 时间/年 名称
        parts = line.split(None, 8)
        names.append(parts[-1] if len(parts) == 9 else line)
    return names


def main():
    try:
        port, user, password, expect = load_expectations()
    except Exception as e:  # 配置读不到 = 挂载没就绪
        print("unhealthy: 无法读取配置 %s: %s" % (CONFIG, e))
        return 1

    try:
        ftp = ftplib.FTP()
        ftp.connect(HOST, port, timeout=TIMEOUT)
        ftp.login(user, password)
        names = list_root(ftp)
        ftp.quit()
    except Exception as e:
        print("unhealthy: FTP 会话失败 %s:%d -> %s" % (HOST, port, e))
        return 1

    missing = sorted(expect - set(names))
    if missing:
        print("unhealthy: LIST / 缺少虚拟挂载点 %s (实际 %d 项: %s)"
              % (missing, len(names), names))
        return 1

    print("healthy: LIST / 返回 %d 项, 挂载点齐全 %s" % (len(names), sorted(expect)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
