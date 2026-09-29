#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""底盘往复测试：0.1 m/s 后退 2s，再 0.1 m/s 前进 2s，然后停车。

运行（容器内）:
  bash scripts/back_forward_test.sh
  或: rosrun chassis_following back_forward_test.py
"""
import rospy

from chassis_following.driver import ChassisDriver


SPEED = 0.1
HOLD_S = 2.0


def hold(driver, vx, duration, rate):
    driver.set_target(vx, 0.0, 0.0)
    t0 = rospy.Time.now()
    while not rospy.is_shutdown():
        if (rospy.Time.now() - t0).to_sec() >= duration:
            break
        driver.step()
        rate.sleep()


def main():
    rospy.init_node("back_forward_test")
    rate_hz = rospy.get_param("~rate", 10.0)
    speed = abs(float(rospy.get_param("~speed", SPEED)))
    hold_s = float(rospy.get_param("~hold_s", HOLD_S))

    driver = ChassisDriver(
        cmd_vel_topic=rospy.get_param("~cmd_vel_topic", "/jzhw/joy_ctrl"),
        max_vx=rospy.get_param("~max_vx", 0.5),
        max_vy=rospy.get_param("~max_vy", 0.0),
        max_omega=rospy.get_param("~max_omega", 0.0),
        accel_lin=rospy.get_param("~accel_lin", 1.0),
        accel_ang=rospy.get_param("~accel_ang", 0.6),
        rate=rate_hz,
        vx_sign=rospy.get_param("~vx_sign", 1.0))
    rospy.on_shutdown(driver.stop)

    rate = rospy.Rate(rate_hz)
    rospy.sleep(0.5)

    rospy.loginfo("[back_forward_test] 后退 %.2f m/s × %.1fs", speed, hold_s)
    hold(driver, -speed, hold_s, rate)
    if rospy.is_shutdown():
        return

    rospy.loginfo("[back_forward_test] 前进 %.2f m/s × %.1fs", speed, hold_s)
    hold(driver, speed, hold_s, rate)
    if rospy.is_shutdown():
        return

    driver.stop()
    rospy.loginfo("[back_forward_test] 完成，已停车")


if __name__ == "__main__":
    main()
