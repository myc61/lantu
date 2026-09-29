#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""腕部 QVPS 拍照服务，给前端/外部用。扫拍节点自己走 TCP，不经过本服务。

  rosservice call /chassis_following/capture "{left_path: '', right_path: '/tmp/right.jpg'}"
"""
import os
import sys

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [
    p for p in sys.path
    if os.path.abspath(p or ".") != os.path.abspath(_scripts_dir)]

import rospy
from chassis_following.runlog import log

from chassis_following.capture import WristCameraCapture
from chassis_following.srv import Capture, CaptureResponse


class QvpsCameraNode(object):
    def __init__(self):
        self.wrist = WristCameraCapture(
            right_host=rospy.get_param("~qvps_right_host", "192.168.217.51"),
            left_host=rospy.get_param("~qvps_left_host", ""),
            port=rospy.get_param("~qvps_port", 10008),
            left_port=rospy.get_param("~qvps_left_port", 5000),
            program=rospy.get_param("~qvps_program", "PN-001"),
            left_program=rospy.get_param("~qvps_left_program", "1"),
            timeout=rospy.get_param("~qvps_timeout", 15.0),
            http_port=rospy.get_param("~qvps_http_port", 80),
            left_http_port=rospy.get_param("~qvps_left_http_port", 8000),
            save_images=rospy.get_param("~save_images", True))
        name = rospy.get_param("~capture_service", "/chassis_following/capture")
        self._srv = rospy.Service(name, Capture, self._cb_capture)
        log.warn("[qvps] 前端拍照服务 %s（扫拍不走这里）", name)

    def _cb_capture(self, req):
        self.wrist.save_images = bool(rospy.get_param("~save_images", True))
        try:
            success, message, left_ok, right_ok, left_saved, right_saved = (
                self.wrist.capture_paths(req.left_path, req.right_path))
        except Exception as e:
            log.error("[qvps] capture 异常: %s", e)
            return CaptureResponse(
                success=False, message=str(e),
                left_ok=False, right_ok=False,
                left_saved="", right_saved="")
        log.info(
            "[qvps] success=%s left_ok=%s right_ok=%s %s",
            success, left_ok, right_ok, message)
        return CaptureResponse(
            success=success, message=message,
            left_ok=left_ok, right_ok=right_ok,
            left_saved=left_saved, right_saved=right_saved)


def main():
    rospy.init_node("qvps_camera_node", anonymous=False)
    QvpsCameraNode()
    rospy.spin()


if __name__ == "__main__":
    main()
