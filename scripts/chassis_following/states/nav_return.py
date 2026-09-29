# -*- coding: utf-8 -*-
"""NAV_RETURN：拍完不发手柄，把 map_home 交给厂商导航。"""
import math

import rospy
from chassis_following.runlog import log
from actionlib_msgs.msg import GoalStatus


def _finish(m, nav, ok):
    if not ok:
        nav.cancel()
    elif not nav.succeeded():
        nav.cancel()
    m.record_run(
        ok, "导航已回地图起点" if ok else "回家未到位")
    m.capture_count = 0
    m.nav_sent = False
    m.reset_prompt()
    notify = bool(getattr(m, "_obstacle_return", False) or m._reset_return)
    m._obstacle_return = False
    m._reset_return = False
    m.state = "IDLE"
    if notify and ok:
        m._reset_signal = True
        log.warn("[chassis_following] 已回 home，向上发布复位 safety_state=reset")


def run(m):
    nav = getattr(m, "nav_client", None)
    loc = getattr(m, "map_loc", None)
    if nav is None:
        log.error_throttle(5.0, "[chassis_following] 没有导航客户端，无法回家")
        return 0.0, 0.0, 0.0

    t = (m.now() - m.nav_t0).to_sec()
    timeout = float(getattr(m, "nav_timeout", 90.0))
    hx = float(m.map_home_x)
    hy = float(m.map_home_y)
    hyaw = float(m.map_home_yaw)
    tol_xy = float(m.map_arrive_tol_xy)
    tol_yaw = float(m.map_arrive_tol_yaw)

    if not m.nav_sent:
        # 等扫拍目标 cancel 落地，立刻发回家容易被厂商直接 Abort。
        if t < 0.4:
            return 0.0, 0.0, 0.0
        nav.send_pose(hx, hy, hyaw, tol_xy, tol_yaw, label="home")
        m.nav_sent = True
        return 0.0, 0.0, 0.0

    st = nav.status_of_ours()
    if st is None and t >= 2.0 and t < 2.2:
        log.warn("[chassis_following] 导航未收到 goal，重发一次")
        nav.send_pose(hx, hy, hyaw, tol_xy, tol_yaw, label="home")

    pose = loc.pose() if loc is not None else None
    if pose is not None:
        log.info_throttle(
            2.0,
            "[chassis_following] 导航回家中 map=(%.3f, %.3f, %.1fdeg) "
            "目标=(%.3f, %.3f, %.1fdeg) status=%s",
            pose[0], pose[1], math.degrees(pose[2]),
            hx, hy, math.degrees(hyaw), st)

    arrived = bool(loc is not None and loc.ready() and
                   loc.at_pose(hx, hy, hyaw, tol_xy, tol_yaw))
    moving = st in (GoalStatus.ACTIVE, GoalStatus.SUCCEEDED)
    # 不能一发出目标就用地图旧位姿判到点；0.15m/1°是发给导航的到位误差。
    done = nav.succeeded() or (t > 5.0 and arrived and moving)
    # ACTIVE/PENDING 是正在走，不能当失败（现场曾在 0.9s status=1 被误杀回家）。
    if st in (None, GoalStatus.PENDING, GoalStatus.ACTIVE):
        failed = t >= timeout
    else:
        failed = nav.failed() or t >= timeout
    if done:
        log.info("[chassis_following] 导航已回地图起点")
        _finish(m, nav, True)
    elif failed:
        log.warn(
            "[chassis_following] 导航回家未到位（t=%.1fs status=%s）",
            t, st)
        _finish(m, nav, False)
    return 0.0, 0.0, 0.0
