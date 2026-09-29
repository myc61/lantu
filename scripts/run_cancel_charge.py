#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""取消充电：agv_charge enable=false，再导航回 map_home。

不经过扫拍状态机。

  source /shared/catkin_ws/devel/setup.bash
  rosrun chassis_following run_cancel_charge.py
"""
import math
import os
import sys

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [
    p for p in sys.path
    if os.path.abspath(p or ".") != os.path.abspath(_scripts_dir)]

import rospy
from chassis_following.charge import ChargeClient


def main():
    rospy.init_node("run_cancel_charge", anonymous=True)
    x = float(rospy.get_param("~map_home_x", -1.77))
    y = float(rospy.get_param("~map_home_y", 0.27))
    yaw = math.radians(float(rospy.get_param("~map_home_yaw_deg", -11.0)))
    dist_tol = float(rospy.get_param("~map_arrive_tol_xy", 0.15))
    yaw_tol = math.radians(float(rospy.get_param("~map_arrive_tol_yaw_deg", 1.0)))
    client = ChargeClient()
    ok, msg = client.cancel(x, y, yaw, dist_tol, yaw_tol)
    if ok:
        rospy.loginfo("[run_cancel_charge] %s", msg)
        sys.exit(0)
    rospy.logerr("[run_cancel_charge] %s", msg)
    sys.exit(1)


if __name__ == "__main__":
    main()
