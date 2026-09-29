#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立充电：TaskType.Charge 导航成功后 call /zj_humanoid/chassis/agv_charge。

不经过扫拍状态机。参数默认读 yaml，也可 rosparam 覆盖。

  source /shared/catkin_ws/devel/setup.bash
  rosrun chassis_following run_charge.py
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
    rospy.init_node("run_charge", anonymous=True)
    x = float(rospy.get_param("~charge_x", 0.5))
    y = float(rospy.get_param("~charge_y", 1.7))
    yaw = math.radians(float(rospy.get_param("~charge_yaw_deg", 90.0)))
    dist_tol = float(rospy.get_param("~charge_arrive_tol_xy", 0.1))
    yaw_tol = math.radians(float(rospy.get_param("~charge_arrive_tol_yaw_deg", 5.73)))
    client = ChargeClient()
    ok, msg = client.run(x, y, yaw, dist_tol, yaw_tol)
    if ok:
        rospy.loginfo("[run_charge] %s", msg)
        sys.exit(0)
    rospy.logerr("[run_charge] %s", msg)
    sys.exit(1)


if __name__ == "__main__":
    main()
