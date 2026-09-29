#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""底盘手动移动测试。发到 /jzhw/joy_ctrl，10Hz。

命令（输入后回车）：
  f <v>    前进 v m/s        b <v>  后退 v m/s
  s        停止              q      退出

运行：
  rosrun chassis_following manual_drive.py
"""
import threading
import sys
import rospy

from chassis_following.driver import ChassisDriver


def _input_thread(driver, stop_event):
    print_help()
    while not stop_event.is_set():
        try:
            line = raw_input("cmd> ") if sys.version_info[0] == 2 else input("cmd> ")
        except EOFError:
            break
        parts = line.strip().split()
        if not parts:
            continue
        cmd = parts[0].lower()
        try:
            if cmd == "q":
                stop_event.set()
                break
            elif cmd == "s":
                driver.set_target(0.0, 0.0, 0.0)
                print("已停止")
            elif cmd == "f":
                driver.set_target(float(parts[1]), 0.0, 0.0)
            elif cmd == "b":
                driver.set_target(-float(parts[1]), 0.0, 0.0)
            else:
                print_help()
        except (IndexError, ValueError):
            print("参数错误，示例: f 0.3")


def print_help():
    print("--------------------------------------------------")
    print("f <v> 前进   b <v> 后退   s 停止   q 退出")
    print("发到 /jzhw/joy_ctrl ，linear.x>0 前进，<0 后退")
    print("--------------------------------------------------")


def main():
    rospy.init_node("manual_drive")
    rate_hz = rospy.get_param("~rate", 10.0)

    driver = ChassisDriver(
        cmd_vel_topic=rospy.get_param("~cmd_vel_topic", "/jzhw/joy_ctrl"),
        max_vx=rospy.get_param("~max_vx", 0.5),
        max_vy=rospy.get_param("~max_vy", 0.0),
        max_omega=rospy.get_param("~max_omega", 0.0),
        accel_lin=rospy.get_param("~accel_lin", 0.3),
        accel_ang=rospy.get_param("~accel_ang", 0.6),
        rate=rate_hz,
        vx_sign=rospy.get_param("~vx_sign", 1.0))

    stop_event = threading.Event()
    t = threading.Thread(target=_input_thread, args=(driver, stop_event))
    t.daemon = True
    t.start()

    rate = rospy.Rate(rate_hz)
    rospy.loginfo("[manual_drive] 已启动，10Hz 发 /jzhw/joy_ctrl")

    while not rospy.is_shutdown() and not stop_event.is_set():
        driver.step()
        rate.sleep()

    driver.stop()
    rospy.loginfo("[manual_drive] 已退出，底盘停止")


if __name__ == "__main__":
    main()
