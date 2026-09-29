# -*- coding: utf-8 -*-
"""地图定位。扫拍/回家都看 /zj_humanoid/navigation/odom_info。"""
import math

import rospy
from nav_msgs.msg import Odometry


def yaw_from_quat(q):
    siny = 2.0 * (q.w * q.z + q.x * q.y)
    cosy = 1.0 - 2.0 * (q.y * q.y + q.z * q.z)
    return math.atan2(siny, cosy)


def ang_diff(a, b):
    d = (float(a) - float(b) + math.pi) % (2.0 * math.pi) - math.pi
    return abs(d)


class MapLocalization(object):
    """订地图系里程计 /zj_humanoid/navigation/odom_info。"""

    def __init__(self, topic=""):
        self._ready = False
        self._x = 0.0
        self._y = 0.0
        self._yaw = 0.0
        self._rx_stamp = None
        self._topic = (topic or "").strip()
        if self._topic:
            rospy.Subscriber(self._topic, Odometry, self._cb)

    def _cb(self, msg):
        p = msg.pose.pose
        self._x = float(p.position.x)
        self._y = float(p.position.y)
        self._yaw = yaw_from_quat(p.orientation)
        self._rx_stamp = rospy.Time.now()
        self._ready = True

    def ready(self):
        return self._ready

    def age_sec(self):
        """距最近一帧地图定位的秒数。还没收到过则返回 None。"""
        if self._rx_stamp is None:
            return None
        return (rospy.Time.now() - self._rx_stamp).to_sec()

    def pose(self):
        if not self._ready:
            return None
        return self._x, self._y, self._yaw

    def at_xy(self, x, y, tol):
        if not self._ready:
            return None
        dx = self._x - float(x)
        dy = self._y - float(y)
        return math.hypot(dx, dy) <= float(tol)

    def at_pose(self, x, y, yaw, tol_xy, tol_yaw):
        if not self._ready:
            return False
        if math.hypot(self._x - float(x), self._y - float(y)) > float(tol_xy):
            return False
        return ang_diff(self._yaw, yaw) <= float(tol_yaw)
