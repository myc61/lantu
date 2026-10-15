# # # -*- coding: utf-8 -*-
# # """NAV_RETURN：拍完不发手柄，把 map_home 交给厂商导航。"""
# # import math

# # import rospy
# # from chassis_following.runlog import log
# # from actionlib_msgs.msg import GoalStatus


# # def _finish(m, nav, ok):
# #     if not ok:
# #         nav.cancel()
# #     elif not nav.succeeded():
# #         nav.cancel()
# #     m.record_run(
# #         ok, "导航已回地图起点" if ok else "回家未到位")
# #     m.capture_count = 0
# #     m.nav_sent = False
# #     m.reset_prompt()
# #     notify = bool(getattr(m, "_obstacle_return", False) or m._reset_return)
# #     m._obstacle_return = False
# #     m._reset_return = False
# #     m.state = "IDLE"
# #     if notify and ok:
# #         m._reset_signal = True
# #         log.warn("[chassis_following] 已回 home，向上发布复位 safety_state=reset")


# # def run(m):
# #     nav = getattr(m, "nav_client", None)
# #     loc = getattr(m, "map_loc", None)
# #     if nav is None:
# #         log.error_throttle(5.0, "[chassis_following] 没有导航客户端，无法回家")
# #         return 0.0, 0.0, 0.0

# #     t = (m.now() - m.nav_t0).to_sec()
# #     timeout = float(getattr(m, "nav_timeout", 90.0))
# #     hx = float(m.map_home_x)
# #     hy = float(m.map_home_y)
# #     hyaw = float(m.map_home_yaw)
# #     tol_xy = float(m.map_arrive_tol_xy)
# #     tol_yaw = float(m.map_arrive_tol_yaw)

# #     if not m.nav_sent:
# #         # 等扫拍目标 cancel 落地，立刻发回家容易被厂商直接 Abort。
# #         if t < 0.4:
# #             return 0.0, 0.0, 0.0
# #         nav.send_pose(hx, hy, hyaw, tol_xy, tol_yaw, label="home")
# #         m.nav_sent = True
# #         return 0.0, 0.0, 0.0

# #     st = nav.status_of_ours()
# #     if st is None and t >= 2.0 and t < 2.2:
# #         log.warn("[chassis_following] 导航未收到 goal，重发一次")
# #         nav.send_pose(hx, hy, hyaw, tol_xy, tol_yaw, label="home")

# #     pose = loc.pose() if loc is not None else None
# #     if pose is not None:
# #         log.info_throttle(
# #             2.0,
# #             "[chassis_following] 导航回家中 map=(%.3f, %.3f, %.1fdeg) "
# #             "目标=(%.3f, %.3f, %.1fdeg) status=%s",
# #             pose[0], pose[1], math.degrees(pose[2]),
# #             hx, hy, math.degrees(hyaw), st)

# #     arrived = bool(loc is not None and loc.ready() and
# #                    loc.at_pose(hx, hy, hyaw, tol_xy, tol_yaw))
# #     moving = st in (GoalStatus.ACTIVE, GoalStatus.SUCCEEDED)
# #     # 不能一发出目标就用地图旧位姿判到点；0.15m/1°是发给导航的到位误差。
# #     done = nav.succeeded() or (t > 5.0 and arrived and moving)
# #     # ACTIVE/PENDING 是正在走，不能当失败（现场曾在 0.9s status=1 被误杀回家）。
# #     if st in (None, GoalStatus.PENDING, GoalStatus.ACTIVE):
# #         failed = t >= timeout
# #     else:
# #         failed = nav.failed() or t >= timeout
# #     if done:
# #         log.info("[chassis_following] 导航已回地图起点")
# #         _finish(m, nav, True)
# #     elif failed:
# #         log.warn(
# #             "[chassis_following] 导航回家未到位（t=%.1fs status=%s）",
# #             t, st)
# #         _finish(m, nav, False)
# #     return 0.0, 0.0, 0.0








# # -*- coding: utf-8 -*-
# """NAV_RETURN：拍完不发手柄，把 map_home 交给厂商导航。

# 到位判定与 loop_nav.py 相同：位置差转到目标航向，纵向、横向都进入
# 容差且航向差够小，就取消这次导航，视为到家。
# """
# import math

# import rospy
# from chassis_following.localize import ang_diff
# from chassis_following.runlog import log
# from actionlib_msgs.msg import GoalStatus

# _HOME_NEAR_XY = 0.03
# _HOME_NEAR_YAW = math.radians(3.0)


# def _body_error(px, py, pyaw, tx, ty, tyaw):
#     """位置差转到目标航向：纵向沿目标朝向，横向为其左侧。"""
#     dx = float(px) - float(tx)
#     dy = float(py) - float(ty)
#     c = math.cos(float(tyaw))
#     s = math.sin(float(tyaw))
#     longitudinal = dx * c + dy * s
#     lateral = -dx * s + dy * c
#     return longitudinal, lateral, ang_diff(pyaw, tyaw)


# def _near_home(pose, hx, hy, hyaw):
#     if pose is None:
#         return None
#     lon, lat, dyaw = _body_error(pose[0], pose[1], pose[2], hx, hy, hyaw)
#     near = (
#         abs(lon) <= _HOME_NEAR_XY and abs(lat) <= _HOME_NEAR_XY
#         and dyaw <= _HOME_NEAR_YAW)
#     return near, lon, lat, dyaw


# def _finish(m, nav, ok, already_cancelled=False):
#     if not already_cancelled and (not ok or not nav.succeeded()):
#         st = nav.status_of_ours()
#         if st in (None, GoalStatus.PENDING, GoalStatus.ACTIVE):
#             nav.cancel()
#     m.record_run(
#         ok, "导航已回地图起点" if ok else "回家未到位")
#     m.capture_count = 0
#     m.nav_sent = False
#     m.reset_prompt()
#     notify = bool(getattr(m, "_obstacle_return", False) or m._reset_return)
#     m._obstacle_return = False
#     m._reset_return = False
#     m.state = "IDLE"
#     if notify and ok:
#         m._reset_signal = True
#         log.warn("[chassis_following] 已回 home，向上发布复位 safety_state=reset")


# def run(m):
#     nav = getattr(m, "nav_client", None)
#     loc = getattr(m, "map_loc", None)
#     if nav is None:
#         log.error_throttle(5.0, "[chassis_following] 没有导航客户端，无法回家")
#         return 0.0, 0.0, 0.0

#     t = (m.now() - m.nav_t0).to_sec()
#     timeout = float(getattr(m, "nav_timeout", 90.0))
#     hx = float(m.map_home_x)
#     hy = float(m.map_home_y)
#     hyaw = float(m.map_home_yaw)
#     tol_xy = float(m.map_arrive_tol_xy)
#     tol_yaw = float(m.map_arrive_tol_yaw)

#     if not m.nav_sent:
#         # 等扫拍目标 cancel 落地，立刻发回家容易被厂商直接 Abort。
#         if t < 0.4:
#             return 0.0, 0.0, 0.0
#         nav.send_pose(hx, hy, hyaw, tol_xy, tol_yaw, label="home")
#         m.nav_sent = True
#         return 0.0, 0.0, 0.0

#     st = nav.status_of_ours()
#     pose = loc.pose() if loc is not None else None
#     near = _near_home(pose, hx, hy, hyaw)
#     if near is not None:
#         hit, lon, lat, dyaw = near
#         log.info_throttle(
#             2.0,
#             "[chassis_following] 导航回家中 纵向=%.3f 横向=%.3f "
#             "航向差=%.1fdeg status=%s",
#             lon, lat, math.degrees(dyaw), st)
#         if hit:
#             log.info(
#                 "[chassis_following] 回家到位 纵向=%.3f 横向=%.3f "
#                 "航向差=%.1fdeg，取消导航",
#                 lon, lat, math.degrees(dyaw))
#             if st in (None, GoalStatus.PENDING, GoalStatus.ACTIVE):
#                 nav.cancel()
#             _finish(m, nav, True, already_cancelled=True)
#             return 0.0, 0.0, 0.0

#     if st is None and t >= 2.0 and t < 2.2:
#         log.warn("[chassis_following] 导航未收到 goal，重发一次")
#         nav.send_pose(hx, hy, hyaw, tol_xy, tol_yaw, label="home")

#     # ACTIVE/PENDING 是正在走，不能当失败（现场曾在 0.9s status=1 被误杀回家）。
#     if st in (None, GoalStatus.PENDING, GoalStatus.ACTIVE):
#         failed = t >= timeout
#     else:
#         failed = nav.failed() or t >= timeout
#     if nav.succeeded():
#         log.info("[chassis_following] 导航已回地图起点")
#         _finish(m, nav, True)
#     elif failed:
#         log.warn(
#             "[chassis_following] 导航回家未到位（t=%.1fs status=%s）",
#             t, st)
#         _finish(m, nav, False)
#     return 0.0, 0.0, 0.0


# -*- coding: utf-8 -*-
"""NAV_RETURN：拍完不发手柄，把 map_home 交给厂商导航。

到位判定与 loop_nav.py 相同：位置差转到目标航向，纵向、横向都进入
容差且航向差够小，就取消这次导航，视为到家。
"""
import math

import rospy
from chassis_following.localize import ang_diff
from chassis_following.runlog import log
from actionlib_msgs.msg import GoalStatus

_HOME_NEAR_XY = 0.03
_HOME_NEAR_YAW = math.radians(3.0)
# 上一段目标还在走或正在被取消时不发回家。最多等这么久，避免定时器线程里 sleep。
_PREV_SETTLE_SEC = 2.0


def _prev_goal_running(st):
    return st in (
        GoalStatus.PENDING, GoalStatus.ACTIVE,
        GoalStatus.PREEMPTING, GoalStatus.RECALLING)


def _body_error(px, py, pyaw, tx, ty, tyaw):
    """位置差转到目标航向：纵向沿目标朝向，横向为其左侧。"""
    dx = float(px) - float(tx)
    dy = float(py) - float(ty)
    c = math.cos(float(tyaw))
    s = math.sin(float(tyaw))
    longitudinal = dx * c + dy * s
    lateral = -dx * s + dy * c
    return longitudinal, lateral, ang_diff(pyaw, tyaw)


def _near_home(pose, hx, hy, hyaw):
    if pose is None:
        return None
    lon, lat, dyaw = _body_error(pose[0], pose[1], pose[2], hx, hy, hyaw)
    near = (
        abs(lon) <= _HOME_NEAR_XY and abs(lat) <= _HOME_NEAR_XY
        and dyaw <= _HOME_NEAR_YAW)
    return near, lon, lat, dyaw


def _finish(m, nav, ok, already_cancelled=False):
    if not already_cancelled and (not ok or not nav.succeeded()):
        st = nav.status_of_ours()
        if st in (None, GoalStatus.PENDING, GoalStatus.ACTIVE):
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
        # 上一段还在 PENDING/ACTIVE/PREEMPTING 时发回家，取消会落到新目标上。
        st_prev = nav.status_of_ours()
        if _prev_goal_running(st_prev) and t < _PREV_SETTLE_SEC:
            log.info_throttle(
                1.0,
                "[chassis_following] 等待上一段导航结束再回家 "
                "status=%s t=%.1fs",
                st_prev, t)
            return 0.0, 0.0, 0.0
        if _prev_goal_running(st_prev):
            log.warn(
                "[chassis_following] 上一段导航 %.1fs 仍未结束 status=%s，仍发回家",
                t, st_prev)
        nav.send_pose(hx, hy, hyaw, tol_xy, tol_yaw, label="home")
        m.nav_sent = True
        return 0.0, 0.0, 0.0

    st = nav.status_of_ours()
    pose = loc.pose() if loc is not None else None
    near = _near_home(pose, hx, hy, hyaw)
    if near is not None:
        hit, lon, lat, dyaw = near
        log.info_throttle(
            2.0,
            "[chassis_following] 导航回家中 纵向=%.3f 横向=%.3f "
            "航向差=%.1fdeg status=%s",
            lon, lat, math.degrees(dyaw), st)
        if hit:
            log.info(
                "[chassis_following] 回家到位 纵向=%.3f 横向=%.3f "
                "航向差=%.1fdeg，取消导航",
                lon, lat, math.degrees(dyaw))
            if st in (None, GoalStatus.PENDING, GoalStatus.ACTIVE):
                nav.cancel()
            _finish(m, nav, True, already_cancelled=True)
            return 0.0, 0.0, 0.0

    if st is None and t >= 2.0 and t < 2.2:
        log.warn("[chassis_following] 导航未收到 goal，重发一次")
        nav.send_pose(hx, hy, hyaw, tol_xy, tol_yaw, label="home")

    # ACTIVE/PENDING 是正在走，不能当失败（现场曾在 0.9s status=1 被误杀回家）。
    if st in (None, GoalStatus.PENDING, GoalStatus.ACTIVE):
        failed = t >= timeout
    else:
        failed = nav.failed() or t >= timeout
    if nav.succeeded():
        log.info("[chassis_following] 导航已回地图起点")
        _finish(m, nav, True)
    elif failed:
        log.warn(
            "[chassis_following] 导航回家未到位（t=%.1fs status=%s）",
            t, st)
        _finish(m, nav, False)
    return 0.0, 0.0, 0.0