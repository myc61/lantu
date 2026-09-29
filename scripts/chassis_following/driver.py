# -*- coding: utf-8 -*-
"""机器人底盘移动驱动。

按 10Hz 把 Twist 发到 /jzhw/joy_ctrl。
linear.x > 0 前进，linear.x < 0 后退。直线往复，不订急停/碰撞。
"""
import rospy
from geometry_msgs.msg import Twist


class ChassisDriver(object):
    """限幅 + 加速度平滑，发到 /jzhw/joy_ctrl。"""

    def __init__(self,
                 cmd_vel_topic="/jzhw/joy_ctrl",
                 max_vx=0.5, max_vy=0.0, max_omega=1.0,
                 accel_lin=0.3, accel_ang=0.6, rate=10.0,
                 vx_sign=1.0):
        self.max_vx = abs(max_vx)
        self.max_vy = abs(max_vy)
        self.max_omega = abs(max_omega)
        self.accel_lin = abs(accel_lin)
        self.accel_ang = abs(accel_ang)
        self.vx_sign = 1.0 if float(vx_sign) >= 0 else -1.0
        self.dt = 1.0 / max(rate, 1.0)
        self.pub = rospy.Publisher(cmd_vel_topic, Twist, queue_size=1)
        self.target = (0.0, 0.0, 0.0)
        self.current = (0.0, 0.0, 0.0)
        self._holding = False

    def set_target(self, vx, vy, omega):
        self.target = (float(vx), float(vy), float(omega))

    def hold(self):
        """扫拍期间占用 /jzhw/joy_ctrl。"""
        self._holding = True

    def release(self):
        """交出底盘：发一次 0 后不再发，导航才能用。"""
        self.target = (0.0, 0.0, 0.0)
        self.current = (0.0, 0.0, 0.0)
        if self._holding:
            self._publish(0.0, 0.0, 0.0)
        self._holding = False

    def stop(self):
        self.release()

    def step(self):
        if not self._holding:
            return
        tx, ty, to = self.target
        cx, cy, co = self.current
        tx = max(-self.max_vx, min(self.max_vx, tx))
        ty = max(-self.max_vy, min(self.max_vy, ty))
        to = max(-self.max_omega, min(self.max_omega, to))
        nx = self._approach(cx, tx, self.accel_lin)
        ny = self._approach(cy, ty, self.accel_lin)
        no = self._approach(co, to, self.accel_ang)
        self.current = (nx, ny, no)
        self._publish(nx, ny, no)

    def _approach(self, cur, target, accel):
        step = accel * self.dt
        diff = target - cur
        if abs(diff) <= step:
            return target
        return cur + step if diff > 0 else cur - step

    def _publish(self, vx, vy, omega):
        msg = Twist()
        msg.linear.x = self.vx_sign * vx
        msg.linear.y = vy
        msg.angular.z = omega
        self.pub.publish(msg)
