# -*- coding: utf-8 -*-
"""腕部拍照：厂家 qvps_tcp_tester 协议（不改该文件）。

WristCameraCapture：扫拍节点直接 TCP；相机服务节点也用它。
WristCameraClient：前端/外部 call /chassis_following/capture，扫拍不用。
左腕 host 留空则只拍右腕。
"""
import os
import sys
import threading
from datetime import datetime

import rospy
from chassis_following.runlog import log

def _import_qvps():
    try:
        from qvps_tcp_tester import (
            build_request, detect_image_ext, fetch_image_http, request_check)
        return build_request, detect_image_ext, fetch_image_http, request_check
    except ImportError:
        pass
    here = os.path.abspath(os.path.dirname(__file__))
    d = here
    for _ in range(8):
        for cand in (d, os.path.join(d, "scripts")):
            if os.path.isfile(os.path.join(cand, "qvps_tcp_tester.py")):
                if cand not in sys.path:
                    sys.path.insert(0, cand)
                from qvps_tcp_tester import (
                    build_request, detect_image_ext, fetch_image_http, request_check)
                return build_request, detect_image_ext, fetch_image_http, request_check
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    raise ImportError("找不到 qvps_tcp_tester.py（不要改那个文件）")


build_request, detect_image_ext, fetch_image_http, request_check = _import_qvps()


class WristCameraCapture(object):
    def __init__(self, right_host="192.168.217.51", left_host="",
                 port=10008, left_port=5000, program="PN-001", left_program="1",
                 timeout=15.0, http_port=80, left_http_port=8000,
                 save_images=True):
        self.right_host = (right_host or "").strip()
        self.left_host = (left_host or "").strip()
        self.port = int(port)
        self.left_port = int(left_port)
        self.program = program or "PN-001"
        self.left_program = left_program or "1"
        self.timeout = float(timeout)
        self.http_port = int(http_port)
        self.left_http_port = int(left_http_port)
        self.save_images = bool(save_images)
        self.last_saved = []
        self._save_lock = threading.Lock()
        self._log_gen = 0
        log.info(
            "[capture] QVPS 右腕=%s:%d 程序=%s 取图=%d 左腕=%s:%d 程序=%s 取图=%d"
            "（触发立刻拍照，传图在后台，回家途中继续）",
            self.right_host or "(未装)", self.port, self.program, self.http_port,
            self.left_host or "(未装)", self.left_port, self.left_program,
            self.left_http_port)

    def _host(self, side):
        if side == "left":
            return self.left_host
        return self.right_host

    def _port(self, side):
        if side == "left":
            return self.left_port
        return self.port

    def _http_port(self, side):
        if side == "left":
            return self.left_http_port
        return self.http_port

    def _program(self, side):
        if side == "left":
            return self.left_program
        return self.program

    def hush(self):
        """扫拍结束：后台线程继续传图，不再打相机日志。"""
        self._log_gen += 1

    def _logging(self, gen):
        return gen == self._log_gen

    def _write(self, path, data):
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, "wb") as f:
            f.write(data)

    def _image_bytes(self, result, trailing, host, http_port, gen=None):
        if trailing and detect_image_ext(trailing):
            return trailing
        for key in ("image_path", "compressed_image_path"):
            dev_path = result.get(key)
            if not dev_path:
                continue
            if gen is None or self._logging(gen):
                log.info("[capture] HTTP取图 http://%s:%d%s",
                         host, http_port, dev_path)
            try:
                data, _url = fetch_image_http(
                    host, dev_path, http_port)
            except Exception as e:
                if gen is None or self._logging(gen):
                    log.warn("[capture] HTTP 取图失败 %s: %s", key, e)
                continue
            if data and detect_image_ext(data):
                return data
        return None

    def save(self, side, path):
        host = self._host(side)
        if not host:
            return True
        gen = self._log_gen
        sent = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        side_cn = "左" if side == "left" else "右"
        program = self._program(side)
        port = self._port(side)
        body = build_request(program)
        log.info(
            "[capture] %s腕拍照 %s TCP发送 host=%s:%d 请求体=%r hex=%s len=%d path=%s",
            side_cn, sent, host, port,
            body.decode("utf-8", "replace"), body.hex(), len(body),
            os.path.basename(path))
        try:
            result, trailing = request_check(
                host, port, program, timeout=self.timeout)
        except Exception as e:
            if self._logging(gen):
                log.warn("[capture] %s腕 TCP 失败: %s",
                              "左" if side == "left" else "右", e)
            return False
        res = result.get("result")
        if self._logging(gen):
            log.info(
                "[capture] %s腕 result=%s %s",
                "左" if side == "left" else "右",
                res, result.get("error_message") or "")
        if not self.save_images:
            if self._logging(gen):
                log.info("[capture] %s腕不保存本地图片", side_cn)
            return True
        data = self._image_bytes(result, trailing, host, self._http_port(side), gen)
        if not data:
            return False
        self._write(path, data)
        with self._save_lock:
            self.last_saved.append(path)
        return True

    def missing_sides(self):
        missing = []
        if self.left_host:
            missing.append("左")
        if self.right_host:
            missing.append("右")
        return missing

    def save_both(self, left_path, right_path):
        """已配置的相机同时触发；未装的一侧跳过。会阻塞到 TCP 返回。"""
        self.last_saved = []
        jobs = []
        if self.left_host:
            jobs.append(("left", left_path))
        if self.right_host:
            jobs.append(("right", right_path))
        if not jobs:
            return False
        if len(jobs) == 1:
            return self.save(jobs[0][0], jobs[0][1])
        ok = {side: False for side, _path in jobs}

        def work(side, path):
            ok[side] = self.save(side, path)

        threads = []
        for side, path in jobs:
            t = threading.Thread(target=work, args=(side, path))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        return all(ok.values())

    def capture_paths(self, left_path, right_path):
        """左右同时拍（有路径的一侧）。阻塞到 TCP/存盘结束。

        返回 (success, message, left_ok, right_ok, left_saved, right_saved)。
        未装的一侧视为成功、saved 为空。路径都空则失败。
        """
        left_path = (left_path or "").strip()
        right_path = (right_path or "").strip()
        jobs = []
        if left_path:
            jobs.append(("left", left_path))
        if right_path:
            jobs.append(("right", right_path))
        if not jobs:
            return False, "left_path 和 right_path 都为空", False, False, "", ""

        ok = {}

        def work(side, path):
            ok[side] = bool(self.save(side, path))

        threads = []
        for side, path in jobs:
            t = threading.Thread(target=work, args=(side, path))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

        left_ok = bool(ok.get("left", False))
        right_ok = bool(ok.get("right", False))
        left_saved = left_path if left_ok and self.left_host and self.save_images else ""
        right_saved = right_path if right_ok and self.right_host and self.save_images else ""
        if not all(ok[side] for side, _path in jobs):
            failed = []
            if left_path and not left_ok:
                failed.append("左")
            if right_path and not right_ok:
                failed.append("右")
            return False, "%s腕拍照失败" % "/".join(failed), left_ok, right_ok, left_saved, right_saved
        if not self.save_images:
            return True, "已触发，未保存图片", left_ok, right_ok, left_saved, right_saved
        return True, "拍照完成", left_ok, right_ok, left_saved, right_saved

    def trigger_both(self, left_path, right_path, sides=None):
        """立刻起后台线程发 request_check（这一刻开始曝光），不等上一张传完。

        sides 缺省左右都发。传图可以叠、可以拖到回家；0.2s 节奏由扫拍时钟决定。
        未装相机视为成功。
        """
        if sides is None:
            sides = ("left", "right")
        want = set(sides)
        jobs = []
        if self.left_host and "left" in want and left_path:
            jobs.append(("left", left_path))
        if self.right_host and "right" in want and right_path:
            jobs.append(("right", right_path))
        if not jobs:
            return True

        gen = self._log_gen

        def work(side, path):
            ok = self.save(side, path)
            if not self._logging(gen):
                return
            if ok and self.save_images:
                log.info("[capture] %s腕已存 %s",
                              "左" if side == "left" else "右", path)
            elif ok:
                log.info("[capture] %s腕已触发，未存本地",
                              "左" if side == "left" else "右")
            else:
                log.warn("[capture] %s腕后台存图失败 %s",
                              "左" if side == "left" else "右", path)

        for side, path in jobs:
            t = threading.Thread(target=work, args=(side, path))
            t.daemon = True
            t.start()
        return True


class WristCameraClient(object):
    """外部/前端调用 /chassis_following/capture。直接 WristCameraCapture。"""

    def __init__(self, service="/chassis_following/capture",
                 left_host="", right_host="", wait_s=10.0):
        self.left_host = (left_host or "").strip()
        self.right_host = (right_host or "").strip()
        self._service = service
        self._wait_s = float(wait_s)
        self._ready = False
        self._log_gen = 0
        log.info(
            "[capture] 外部拍照客户端 %s 右腕=%s 左腕=%s",
            self._service,
            self.right_host or "(未装)",
            self.left_host or "(未装)")

    def hush(self):
        """扫拍结束"""
        self._log_gen += 1

    def _logging(self, gen):
        return gen == self._log_gen

    def _call(self, left_path, right_path):
        from chassis_following.srv import Capture
        rospy.wait_for_service(self._service, timeout=self._wait_s)
        proxy = rospy.ServiceProxy(self._service, Capture, persistent=False)
        return proxy(left_path=left_path or "", right_path=right_path or "")

    def trigger_both(self, left_path, right_path):
        """线程 call 拍照服务，不等存盘。每组独立连接，可重叠。"""
        gen = self._log_gen

        def work():
            try:
                resp = self._call(left_path, right_path)
                if not self._logging(gen):
                    return
                if not self._ready:
                    self._ready = True
                    log.info("[capture] 已连上 %s", self._service)
                if resp.success:
                    log.info(
                        "[capture] 服务完成 left=%s right=%s",
                        resp.left_saved or "-", resp.right_saved or "-")
                else:
                    log.warn("[capture] 服务失败: %s", resp.message)
            except rospy.ROSException as e:
                if self._logging(gen):
                    log.error(
                        "[capture] 等不到 %s（先起 qvps_camera_node）: %s",
                        self._service, e)
            except Exception as e:
                if self._logging(gen):
                    log.warn("[capture] 调用 %s 失败: %s", self._service, e)

        t = threading.Thread(target=work)
        t.daemon = True
        t.start()
        return True
