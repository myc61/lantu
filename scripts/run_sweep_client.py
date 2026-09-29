#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调一次 /chassis_following/run_sweep。扫完打印 feedback，到家后看 result。

模式在节点 yaml 的 sweep_mode。模式 1、2 需要车型。
"""
import os
import sys

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [
    p for p in sys.path
    if os.path.abspath(p or ".") != os.path.abspath(_scripts_dir)]

import actionlib
import rospy
from chassis_following.msg import RunDistanceSweepAction, RunDistanceSweepGoal


def _on_feedback(fb):
    rospy.loginfo(
        "扫拍结束 feedback  sweep_ok=%s  张数=%d  目录=%s  %s",
        fb.sweep_ok, fb.shot_count, fb.run_dir, fb.message)


def main():
    rospy.init_node("run_sweep_client", anonymous=True)
    name = rospy.get_param("~action", "/chassis_following/run_sweep")
    task_id = str(rospy.get_param("~task_id", "") or "")
    vehicle_type = str(rospy.get_param("~vehicle_type", "") or "")
    wait_s = float(rospy.get_param("~wait_server", 10.0))
    client = actionlib.SimpleActionClient(name, RunDistanceSweepAction)
    rospy.loginfo("等待动作 %s ...", name)
    if not client.wait_for_server(rospy.Duration(wait_s)):
        rospy.logerr("动作没起来：%s（先 roslaunch chassis_following）", name)
        sys.exit(2)
    goal = RunDistanceSweepGoal(
        task_id=task_id, vehicle_type=vehicle_type)
    rospy.loginfo(
        "已发送 车型=%s，扫完会有 feedback，到家后才有 result",
        vehicle_type or "-")
    client.send_goal(goal, feedback_cb=_on_feedback)
    client.wait_for_result()
    state = client.get_state()
    result = client.get_result()
    if result is None:
        rospy.logerr("没有结果 state=%s", state)
        sys.exit(1)
    if result.success:
        rospy.loginfo("到家成功  %s", result.message)
        sys.exit(0)
    rospy.logerr("失败 state=%s  %s", state, result.message)
    sys.exit(1)


if __name__ == "__main__":
    main()
