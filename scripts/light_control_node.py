#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""光源控制服务：/chassis_following/light_control/set。

  rosservice call /chassis_following/light_control/set "channel: 0  brightness: 255"
  rosservice call /chassis_following/light_control/set "channel: 1  brightness: 0"
通道 0 是左手灯（192.168.217.2:2000）。
通道 1 是右手灯（192.168.217.3:2001）。
两台的报文通道都是 1，亮度超过 230 按 230 发。
"""
import os
import sys
from datetime import datetime

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [
    p for p in sys.path
    if os.path.abspath(p or ".") != os.path.abspath(_scripts_dir)]

import rospy
from chassis_following.runlog import log

from chassis_following.light import LightController
from chassis_following.srv import SetLight, SetLightResponse


class LightControlNode(object):
    def __init__(self):
        self._ctrl = LightController()
        name = rospy.get_param("~light_service", "/chassis_following/light_control/set")
        self._srv = rospy.Service(name, SetLight, self._cb_set)
        log.warn(
            "[light] 光源服务 %s（0 左手 / 1 右手，报文通道都是 1，亮度上限 230）",
            name)

    def _cb_set(self, req):
        received = datetime.now()
        log.info(
            "[light] 收到服务 %s channel=%d brightness=%d",
            received.strftime("%H:%M:%S.%f")[:-3],
            int(req.channel), int(req.brightness))
        ok, msg = self._ctrl.set_light(
            int(req.channel), int(req.brightness), received_at=received)
        return SetLightResponse(success=ok, message=msg)


def main():
    rospy.init_node("light_control_node", anonymous=False)
    LightControlNode()
    rospy.spin()


if __name__ == "__main__":
    main()
