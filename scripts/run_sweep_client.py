#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调一次 /chassis_following/run_sweep。扫完打印 feedback，到家后看 result。

模式在节点 yaml 的 sweep_mode。模式 1、2 需要车型和车尾速度。
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
from chassis_following.runlog import handoff


def _on_feedback(fb):
    rospy.loginfo(
        "扫拍 feedback  sweep_ok=%s  张数=%d  trace_id_L=%s  trace_id_R=%s  目录=%s  %s",
        fb.sweep_ok, fb.shot_count, fb.trace_id_L, fb.trace_id_R,
        fb.run_dir, fb.message)


def main():
    rospy.init_node("run_sweep_client", anonymous=True)
    name = rospy.get_param("~action", "/chassis_following/run_sweep")
    task_id = str(rospy.get_param("~task_id", "") or "")
    vehicle_type = str(rospy.get_param("~vehicle_type", "") or "")
    speed = float(rospy.get_param("~speed", 0.0))
    wait_s = float(rospy.get_param("~wait_server", 10.0))
    button_at = str(rospy.get_param("~button_at", "") or "").strip().strip("'\"")
    if button_at:
        rospy.loginfo("[handoff] %s 前端按钮触发", button_at)
    client = actionlib.SimpleActionClient(name, RunDistanceSweepAction)
    rospy.loginfo("等待动作 %s ...", name)
    if not client.wait_for_server(rospy.Duration(wait_s)):
        rospy.logerr("动作没起来：%s（先 roslaunch chassis_following）", name)
        sys.exit(2)
    goal = RunDistanceSweepGoal(
        task_id=task_id, vehicle_type=vehicle_type, speed=speed)
    handoff("请求服务")
    rospy.loginfo(
        "已发送 车型=%s 车尾速度=%.3f m/s，扫完会有 feedback，到家后才有 result",
        vehicle_type or "-", speed)
    client.send_goal(goal, feedback_cb=_on_feedback)
    client.wait_for_result()
    state = client.get_state()
    result = client.get_result()
    if result is None:
        rospy.logerr("没有结果 state=%s", state)
        sys.exit(1)
    if result.success:
        rospy.loginfo(
            "到家成功  %s  检测点间隔均值=%.3f 米",
            result.message, result.point_interval)
        sys.exit(0)
    rospy.logerr(
        "失败 state=%s  %s  检测点间隔均值=%.3f 米",
        state, result.message, result.point_interval)
    sys.exit(1)


if __name__ == "__main__":
    main()
