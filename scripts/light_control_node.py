#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""光源控制服务：/chassis_following/light_control/set。

  rosservice call /chassis_following/light_control/set "channel: 1  brightness: 255"
  rosservice call /chassis_following/light_control/set "channel: 1  brightness: 0"
"""
import os
import sys

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
            "[light] 光源服务 %s（channel 1/2, brightness 0-255）", name)

    def _cb_set(self, req):
        ok, msg = self._ctrl.set_light(int(req.channel), int(req.brightness))
        return SetLightResponse(success=ok, message=msg)


def main():
    rospy.init_node("light_control_node", anonymous=False)
    LightControlNode()
    rospy.spin()


if __name__ == "__main__":
    main()
