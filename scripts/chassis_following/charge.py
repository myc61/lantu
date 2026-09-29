# -*- coding: utf-8 -*-
"""充电：导航用 TaskType.Charge，到位后 call agv_charge。

状态机只调用 AgvCharge。ChargeClient.run/cancel 仍给独立脚本用。
"""
import math

import actionlib
import rospy
from chassis_following.runlog import log
from actionlib_msgs.msg import GoalStatus
from geometry_msgs.msg import Quaternion
from navigation.msg import NavigationAction, NavigationGoal, NavigationState, TaskType, Waypoint

try:
    from chassis_msgs.srv import ChargeControl, ChargeControlRequest
    _CHARGE_IMPORT_ERR = None
except Exception as e:
    ChargeControl = None
    ChargeControlRequest = None
    _CHARGE_IMPORT_ERR = e


def _yaw_to_quat(yaw):
    return Quaternion(x=0.0, y=0.0, z=math.sin(yaw / 2.0), w=math.cos(yaw / 2.0))


def _nav_ok(state, result):
    if int(state) != GoalStatus.SUCCEEDED:
        return False
    if result is None:
        return False
    value = int(getattr(getattr(result, "state", None), "value", -1))
    return value in (NavigationState.Arrived, NavigationState.Succeeded)


class AgvCharge(object):
    """只调充电服务，不另开一条导航客户端。"""

    def __init__(self, service="/zj_humanoid/chassis/agv_charge"):
        self._srv = service or "/zj_humanoid/chassis/agv_charge"

    def enable(self, on, wait_s=5.0):
        if ChargeControl is None:
            log.error("[charge] import 不到 chassis_msgs/ChargeControl: %s",
                      _CHARGE_IMPORT_ERR)
            return False, "ChargeControl 不可用"
        log.info("[charge] 等待服务 %s ...", self._srv)
        try:
            rospy.wait_for_service(self._srv, timeout=float(wait_s))
        except rospy.ROSException:
            msg = "充电服务未就绪: %s" % self._srv
            log.error("[charge] %s", msg)
            return False, msg
        try:
            proxy = rospy.ServiceProxy(self._srv, ChargeControl)
            req = ChargeControlRequest()
            req.enable = bool(on)
            resp = proxy(req)
        except rospy.ServiceException as e:
            msg = "充电服务调用失败: %s" % e
            log.error("[charge] %s", msg)
            return False, msg
        ok = bool(getattr(resp, "success", False))
        message = str(getattr(resp, "message", ""))
        if ok:
            log.warn("[charge] agv_charge enable=%s ok  %s", on, message)
        else:
            log.error("[charge] agv_charge enable=%s 失败  %s", on, message)
        if message:
            return ok, message
        if ok:
            return True, "充电开启成功" if on else "充电关闭成功"
        return False, "充电开启失败" if on else "充电关闭失败"


class ChargeClient(object):
    def __init__(self, action_ns=None, charge_service=None):
        self._ns = (
            action_ns
            or rospy.get_param("~charge_action", "/zj_humanoid/navigation/navigation")
        ).rstrip("/")
        self._agv = AgvCharge(charge_service or rospy.get_param(
            "~charge_service", "/zj_humanoid/chassis/agv_charge"))
        self._srv = self._agv._srv
        self._client = actionlib.SimpleActionClient(self._ns, NavigationAction)

    def wait_server(self, timeout=None):
        wait_s = float(timeout if timeout is not None else rospy.get_param(
            "~charge_wait_server", 30.0))
        log.info("[charge] 等待导航 Action %s ...", self._ns)
        if not self._client.wait_for_server(rospy.Duration(wait_s)):
            log.error("[charge] 导航 Action 未就绪: %s", self._ns)
            return False
        return True

    def send_nav_goal(self, x, y, yaw, dist_tol, yaw_tol,
                      task_type=TaskType.Charge, label="Charge",
                      frame_id="map"):
        goal = NavigationGoal()
        goal.header.stamp = rospy.Time.now()
        goal.header.frame_id = frame_id
        goal.task_type.value = int(task_type)
        wp = Waypoint()
        wp.pose.position.x = float(x)
        wp.pose.position.y = float(y)
        wp.pose.position.z = 0.0
        wp.pose.orientation = _yaw_to_quat(float(yaw))
        wp.distance_tolerance = float(dist_tol)
        wp.heading_tolerance = float(yaw_tol)
        goal.waypoints = [wp]
        goal.translation.enable = False
        goal.translation.heading = 0.0
        log.warn(
            "[charge] 发送 %s goal map=(%.3f, %.3f, yaw=%.1fdeg) "
            "tol_xy=%.2f tol_yaw=%.3frad",
            label, x, y, math.degrees(yaw), dist_tol, yaw_tol)
        self._client.send_goal(goal, done_cb=self._done_cb)
        return True

    def send_charge_goal(self, x, y, yaw, dist_tol, yaw_tol, frame_id="map"):
        return self.send_nav_goal(
            x, y, yaw, dist_tol, yaw_tol,
            task_type=TaskType.Charge, label="Charge", frame_id=frame_id)

    def _done_cb(self, state, result):
        value = int(getattr(getattr(result, "state", None), "value", -1))
        log.info(
            "[charge] 导航结束 action_state=%s nav_state=%s "
            "dist_dev=%s head_dev=%s",
            state, value,
            getattr(result, "distance_deviation", None),
            getattr(result, "heading_deviation", None))

    def wait_result(self, timeout=None):
        if timeout is None:
            timeout = float(rospy.get_param("~nav_timeout", 90.0))
        ok = self._client.wait_for_result(rospy.Duration(float(timeout)))
        if not ok:
            log.error("[charge] 导航超时 t=%.1fs", timeout)
            self._client.cancel_goal()
            return False, None, "导航超时"
        state = self._client.get_state()
        result = self._client.get_result()
        if not _nav_ok(state, result):
            value = int(getattr(getattr(result, "state", None), "value", -1))
            msg = "导航未成功 action_state=%s nav_state=%s" % (state, value)
            log.error("[charge] %s", msg)
            return False, result, msg
        return True, result, "导航到位"

    def call_agv_charge(self, enable=True, wait_s=5.0):
        return self._agv.enable(enable, wait_s=wait_s)

    def run(self, x, y, yaw, dist_tol, yaw_tol):
        """导航去充电点，成功后再开充电。不进状态机，供独立脚本调用。"""
        if not self.wait_server():
            return False, "导航 Action 未就绪"
        self.send_charge_goal(x, y, yaw, dist_tol, yaw_tol)
        ok, _result, msg = self.wait_result()
        if not ok:
            return False, msg
        charge_ok, charge_msg = self.call_agv_charge(enable=True)
        if not charge_ok:
            return False, "导航成功但充电失败: %s" % charge_msg
        return True, "导航成功并已开启充电"

    def cancel(self, x, y, yaw, dist_tol, yaw_tol):
        """先关充电，再导航回起始点。不进状态机。"""
        off_ok, off_msg = self.call_agv_charge(enable=False)
        if not off_ok:
            return False, "取消充电失败，未发回家导航: %s" % off_msg
        if not self.wait_server():
            return False, "已关充电，但导航 Action 未就绪"
        self.send_nav_goal(
            x, y, yaw, dist_tol, yaw_tol,
            task_type=TaskType.Routine, label="Home")
        ok, _result, msg = self.wait_result()
        if not ok:
            return False, "已关充电，但回家导航失败: %s" % msg
        return True, "已关充电并回到起始点"
