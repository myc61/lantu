# -*- coding: utf-8 -*-
"""厂商导航 Action 客户端。

rosbridge 容器已提供 navigation 包，消息与现场
/zj_humanoid/navigation/navigation 一致（NavigationActionGoal md5=1f79deaf...）。
"""
import math

import rospy
from chassis_following.runlog import log
from actionlib_msgs.msg import GoalID, GoalStatus, GoalStatusArray
from geometry_msgs.msg import Pose, Quaternion
from navigation.msg import NavigationActionGoal, TaskType, Waypoint


def _yaw_to_quat(yaw):
    return Quaternion(x=0.0, y=0.0, z=math.sin(yaw / 2.0), w=math.cos(yaw / 2.0))


class NavHomeClient(object):
    def __init__(self, action_ns="/zj_humanoid/navigation/navigation"):
        self._ns = action_ns.rstrip("/")
        self._pub = rospy.Publisher(
            self._ns + "/goal", NavigationActionGoal, queue_size=1)
        self._cancel = rospy.Publisher(
            self._ns + "/cancel", GoalID, queue_size=1)
        self._status = None
        rospy.Subscriber(
            self._ns + "/status", GoalStatusArray, self._cb_status)
        self._goal_id = ""

    def _cb_status(self, msg):
        self._status = msg

    def send_pose(self, x, y, yaw, dist_tol, yaw_tol, frame_id="map",
                  label="home", task_type=None):
        now = rospy.Time.now()
        tag = (label or "nav").strip() or "nav"
        gid = "chassis_%s_%.3f" % (tag, now.to_sec())
        self._goal_id = gid
        pose = Pose()
        pose.position.x = float(x)
        pose.position.y = float(y)
        pose.position.z = 0.0
        pose.orientation = _yaw_to_quat(float(yaw))
        wp = Waypoint()
        wp.pose = pose
        wp.distance_tolerance = float(dist_tol)
        wp.heading_tolerance = float(yaw_tol)
        msg = NavigationActionGoal()
        msg.header.stamp = now
        msg.header.frame_id = frame_id
        msg.goal_id.stamp = now
        msg.goal_id.id = gid
        msg.goal.header.stamp = now
        msg.goal.header.frame_id = frame_id
        kind = TaskType.Routine if task_type is None else int(task_type)
        msg.goal.task_type.value = kind
        msg.goal.waypoints = [wp]
        msg.goal.translation.enable = False
        self._pub.publish(msg)
        log.warn(
            "[chassis_following] 导航%s goal=%s map=(%.3f, %.3f, yaw=%.1fdeg)",
            tag, gid, x, y, math.degrees(yaw))
        return gid

    def cancel(self):
        if not self._goal_id:
            return
        msg = GoalID()
        msg.stamp = rospy.Time.now()
        msg.id = self._goal_id
        self._cancel.publish(msg)

    def status_of_ours(self):
        if not self._goal_id or self._status is None:
            return None
        for st in self._status.status_list:
            if st.goal_id.id == self._goal_id:
                return int(st.status)
        return None

    def succeeded(self):
        return self.status_of_ours() == GoalStatus.SUCCEEDED

    def failed(self):
        s = self.status_of_ours()
        return s in (
            GoalStatus.ABORTED, GoalStatus.REJECTED,
            GoalStatus.LOST, GoalStatus.PREEMPTED)

    def active(self):
        return self.status_of_ours() == GoalStatus.ACTIVE
