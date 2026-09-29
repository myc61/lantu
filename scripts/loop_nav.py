#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 map_home 和 sweep_end 之间循环导航。不进扫拍状态机，不拍照。

坐标读 config/chassis_following.yaml。
下发目标后订阅地图定位。相对目标的纵向、横向都在 0.05 米内，
且航向差在 5 度内，就取消这次导航，再发下一个点。
每一圈：map_home -> sweep_end -> map_home。

  source /opt/ros/noetic/setup.bash
  source /shared/catkin_ws/devel/setup.bash
  rosrun chassis_following loop_nav.py
  rosrun chassis_following loop_nav.py _loops:=3

_loops 默认 0，表示一直循环。Ctrl+C 取消当前目标并退出。
"""
import math
import os
import sys

import actionlib
import rospy
import yaml
from actionlib_msgs.msg import GoalStatus
from chassis_following.localize import MapLocalization, ang_diff
from geometry_msgs.msg import Quaternion
from navigation.msg import NavigationAction, NavigationGoal, TaskType, Waypoint

try:
    import rospkg
except ImportError:
    rospkg = None


def _yaw_to_quat(yaw):
    return Quaternion(x=0.0, y=0.0, z=math.sin(yaw / 2.0), w=math.cos(yaw / 2.0))


def _yaml_path():
    if rospkg is not None:
        root = rospkg.RosPack().get_path("chassis_following")
    else:
        root = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), ".."))
    return os.path.join(root, "config", "chassis_following.yaml")


def _load_points():
    path = _yaml_path()
    with open(path, "r") as f:
        cfg = yaml.safe_load(f) or {}
    home = (
        float(cfg.get("map_home_x", -0.37)),
        float(cfg.get("map_home_y", 0.04)),
        math.radians(float(cfg.get("map_home_yaw_deg", 2.5))))
    end = (
        float(cfg.get("sweep_end_x", 3.10)),
        float(cfg.get("sweep_end_y", 0.17)),
        math.radians(float(cfg.get("sweep_end_yaw_deg", 2.5))))
    tol_xy = float(cfg.get("map_arrive_tol_xy", 0.15))
    tol_yaw = math.radians(float(cfg.get("map_arrive_tol_yaw_deg", 1.0)))
    timeout = float(cfg.get("nav_timeout", 90.0))
    action = cfg.get("nav_action", "/zj_humanoid/navigation/navigation")
    odom = cfg.get("map_odom_topic", "/zj_humanoid/navigation/odom_info")
    return home, end, tol_xy, tol_yaw, timeout, action, odom, path


def _body_error(px, py, pyaw, tx, ty, tyaw):
    """位置差转到目标航向：纵向沿目标朝向，横向为其左侧。"""
    dx = float(px) - float(tx)
    dy = float(py) - float(ty)
    c = math.cos(float(tyaw))
    s = math.sin(float(tyaw))
    longitudinal = dx * c + dy * s
    lateral = -dx * s + dy * c
    return longitudinal, lateral, ang_diff(pyaw, tyaw)


def _send(client, x, y, yaw, tol_xy, tol_yaw, label):
    goal = NavigationGoal()
    goal.header.stamp = rospy.Time.now()
    goal.header.frame_id = "map"
    goal.task_type.value = int(TaskType.Routine)
    wp = Waypoint()
    wp.pose.position.x = float(x)
    wp.pose.position.y = float(y)
    wp.pose.position.z = 0.0
    wp.pose.orientation = _yaw_to_quat(float(yaw))
    wp.distance_tolerance = float(tol_xy)
    wp.heading_tolerance = float(tol_yaw)
    goal.waypoints = [wp]
    goal.translation.enable = False
    rospy.loginfo(
        "[loop_nav] 去 %s map=(%.3f, %.3f, %.1fdeg)",
        label, x, y, math.degrees(yaw))
    client.send_goal(goal)


def _stop_goal(client):
    """还在走才取消。已经结束再 cancel，会打出 PREEMPTING 并让下一点被厂商 Abort。"""
    state = client.get_state()
    if state in (GoalStatus.PENDING, GoalStatus.ACTIVE):
        client.cancel_goal()
    t0 = rospy.Time.now()
    rate = rospy.Rate(10)
    while not rospy.is_shutdown():
        state = client.get_state()
        if state not in (GoalStatus.PENDING, GoalStatus.ACTIVE):
            return
        if (rospy.Time.now() - t0).to_sec() > 2.0:
            return
        rate.sleep()


def _wait(client, loc, x, y, yaw, tol_xy, tol_yaw, timeout, label):
    """定位进入 0.05 米 / 5 度后取消导航，视为这一段完成。"""
    xy_tol = 0.05
    yaw_tol_rad = math.radians(5.0)
    t0 = rospy.Time.now()
    rate = rospy.Rate(10)
    seen_active = False
    resent = False
    while not rospy.is_shutdown():
        state = client.get_state()
        if state == GoalStatus.ACTIVE:
            seen_active = True
        pose = loc.pose()
        if pose is not None:
            lon, lat, dyaw = _body_error(pose[0], pose[1], pose[2], x, y, yaw)
            rospy.loginfo_throttle(
                2.0,
                "[loop_nav] %s 纵向=%.3f 横向=%.3f 航向差=%.1fdeg",
                label, lon, lat, math.degrees(dyaw))
            if (abs(lon) <= xy_tol and abs(lat) <= xy_tol
                    and dyaw <= yaw_tol_rad):
                rospy.loginfo(
                    "[loop_nav] %s 到位 纵向=%.3f 横向=%.3f 航向差=%.1fdeg，取消导航",
                    label, lon, lat, math.degrees(dyaw))
                _stop_goal(client)
                rospy.loginfo("[loop_nav] %s 到位后等待 1s", label)
                rospy.sleep(1.0)
                return True
        elapsed = (rospy.Time.now() - t0).to_sec()
        if elapsed >= float(timeout):
            rospy.logerr("[loop_nav] %s 超时 %.1fs，取消", label, timeout)
            _stop_goal(client)
            return False
        if state in (
                GoalStatus.ABORTED, GoalStatus.REJECTED, GoalStatus.LOST):
            if not seen_active and not resent:
                resent = True
                rospy.logwarn(
                    "[loop_nav] %s 目标被中止 status=%s，等上一段结束后重发",
                    label, state)
                _stop_goal(client)
                _send(client, x, y, yaw, tol_xy, tol_yaw, label)
                t0 = rospy.Time.now()
                continue
            rospy.logerr("[loop_nav] %s 导航失败 status=%s", label, state)
            return False
        if state == GoalStatus.SUCCEEDED and seen_active:
            rospy.loginfo("[loop_nav] %s 导航已报到位，等待 1s", label)
            rospy.sleep(1.0)
            return True
        rate.sleep()
    _stop_goal(client)
    return False


def main():
    rospy.init_node("loop_nav", anonymous=True)
    home, end, tol_xy, tol_yaw, timeout, action, odom, path = _load_points()
    loops = int(rospy.get_param("~loops", 0))
    action = rospy.get_param("~nav_action", action)
    odom = rospy.get_param("~odom_topic", odom)
    loc = MapLocalization(odom)
    client = actionlib.SimpleActionClient(action, NavigationAction)
    rospy.loginfo("[loop_nav] 定位话题 %s ，到位：纵/横 <=0.05m 且航向差<=5deg", odom)
    rospy.loginfo("[loop_nav] 等待导航 %s ，点来自 %s", action, path)
    if not client.wait_for_server(rospy.Duration(30.0)):
        rospy.logerr("[loop_nav] 导航 Action 未就绪")
        sys.exit(2)
    rospy.on_shutdown(client.cancel_goal)

    n = 0
    while not rospy.is_shutdown():
        n += 1
        if loops > 0 and n > loops:
            break
        rospy.loginfo("[loop_nav] 第 %d 圈  map_home -> sweep_end", n)
        _send(client, end[0], end[1], end[2], tol_xy, tol_yaw, "sweep_end")
        if not _wait(
                client, loc, end[0], end[1], end[2],
                tol_xy, tol_yaw, timeout, "sweep_end"):
            sys.exit(1)
        if rospy.is_shutdown():
            break
        rospy.loginfo("[loop_nav] 第 %d 圈  sweep_end -> map_home", n)
        _send(client, home[0], home[1], home[2], tol_xy, tol_yaw, "map_home")
        if not _wait(
                client, loc, home[0], home[1], home[2],
                tol_xy, tol_yaw, timeout, "map_home"):
            sys.exit(1)
    rospy.loginfo("[loop_nav] 结束，共 %d 圈", n if loops <= 0 else loops)


if __name__ == "__main__":
    main()
