# -*- coding: utf-8 -*-
"""NAV_SWEEP：导航去扫拍终点，边走边按时间间隔拍照，到位后走 NAV_RETURN。"""
import math

import rospy
from chassis_following.runlog import log
from actionlib_msgs.msg import GoalStatus


def run(m):
    nav = getattr(m, "nav_client", None)
    loc = getattr(m, "map_loc", None)
    if nav is None:
        log.error_throttle(5.0, "[chassis_following] 没有导航客户端，无法扫拍")
        m.go_home(why="扫拍无导航")
        return 0.0, 0.0, 0.0

    t = (m.now() - m.nav_t0).to_sec()
    timeout = float(getattr(m, "nav_timeout", 90.0))
    hx = float(m.sweep_end_x)
    hy = float(m.sweep_end_y)
    hyaw = float(m.sweep_end_yaw)
    tol_xy = float(m.map_arrive_tol_xy)
    tol_yaw = float(m.map_arrive_tol_yaw)

    if not m.nav_sent:
        nav.send_pose(hx, hy, hyaw, tol_xy, tol_yaw, label="sweep")
        m.nav_sent = True
        m.photo_clock_t0 = None
        m.last_shot_stamp = None
        m.sweep_origin = loc.pose() if loc is not None else None
        return 0.0, 0.0, 0.0

    if getattr(m, "end_on_front", False) and _past_vehicle_front(m, loc):
        m.go_home(why="已超过车头")
        return 0.0, 0.0, 0.0

    st = nav.status_of_ours()
    if st is None and t >= 2.0 and t < 2.2:
        log.warn("[chassis_following] 扫拍导航未收到 goal，重发一次")
        nav.send_pose(hx, hy, hyaw, tol_xy, tol_yaw, label="sweep")

    pose = loc.pose() if loc is not None else None
    if pose is not None and getattr(m, "sweep_origin", None) is None:
        m.sweep_origin = pose

    # 厂商 ACTIVE 只表示收下目标，底盘还会在原地规划一两秒。
    # 必须离开发车点才开拍钟，否则前几张都是同一张静图。
    if m.photo_clock_t0 is None and pose is not None and m.sweep_origin is not None:
        dist = math.hypot(pose[0] - m.sweep_origin[0],
                          pose[1] - m.sweep_origin[1])
        move_m = max(0.02, float(getattr(m, "sweep_photo_move_m", 0.1)))
        if dist >= move_m:
            m.photo_clock_t0 = m.now()
            log.info(
                "[chassis_following] 底盘已离开起点 %.2fm，%.2fs 后开始拍照",
                dist, float(m.sweep_photo_delay))
    if pose is not None:
        log.info_throttle(
            2.0,
            "[chassis_following] 导航扫拍中 map=(%.3f, %.3f, %.1fdeg) "
            "终点=(%.3f, %.3f, %.1fdeg) status=%s shots=%d",
            pose[0], pose[1], math.degrees(pose[2]),
            hx, hy, math.degrees(hyaw), st, m.capture_count)

    arrived = bool(loc is not None and loc.ready() and
                   loc.at_pose(hx, hy, hyaw, tol_xy, tol_yaw))
    # 未真正开始导航时不能用地图位姿判到点（可能还停在起点）。
    moving = st in (GoalStatus.ACTIVE, GoalStatus.SUCCEEDED)
    done = nav.succeeded() or (t > 5.0 and arrived and moving)
    if st in (None, GoalStatus.PENDING, GoalStatus.ACTIVE):
        failed = t >= timeout
    else:
        failed = nav.failed() or t >= timeout
    if done:
        log.info(
            "[chassis_following] 已到扫拍终点，共 %d 组，导航回起点",
            m.capture_count)
        m.go_home(why="扫拍终点到位")
        return 0.0, 0.0, 0.0
    if failed:
        log.warn(
            "[chassis_following] 扫拍导航未到位（t=%.1fs status=%s shots=%d），回家",
            t, st, m.capture_count)
        m.go_home(why="扫拍导航失败")
        return 0.0, 0.0, 0.0

    if m.photo_clock_t0 is None:
        return 0.0, 0.0, 0.0
    t_move = (m.now() - m.photo_clock_t0).to_sec()
    delay = max(0.0, float(m.sweep_photo_delay))
    period = max(0.05, float(m.sweep_photo_period))
    if t_move + 1e-6 < delay:
        return 0.0, 0.0, 0.0
    if m.last_shot_stamp is not None:
        dt = (m.now() - m.last_shot_stamp).to_sec()
        if dt + 1e-6 < period:
            return 0.0, 0.0, 0.0
    if not m.trigger_photo():
        return 0.0, 0.0, 0.0
    m.last_shot_stamp = m.now()
    return 0.0, 0.0, 0.0


def _past_vehicle_front(m, loc):
    """模式 2：超过车头就结束。还判不了时继续扫。"""
    ctrl = getattr(m, "distance", None)
    if ctrl is None or loc is None or not loc.ready():
        return False
    pose = loc.pose()
    if pose is None:
        return False
    passed = ctrl.past_vehicle_front(pose[0], pose[1])
    if passed is None:
        log.warn_throttle(
            2.0, "[chassis_following] 夹具位置还不能判断车头，继续定时扫拍")
        return False
    if passed:
        log.info("[chassis_following] 已超过车头，结束定时扫拍并回家")
    return bool(passed)
