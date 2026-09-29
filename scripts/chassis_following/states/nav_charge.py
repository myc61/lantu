# -*- coding: utf-8 -*-
"""NAV_CHARGE：TaskType.Charge 去充电点，到位后打开 agv_charge，进入 CHARGING。"""
import math

from chassis_following.runlog import log
from actionlib_msgs.msg import GoalStatus
from navigation.msg import TaskType


def run(m):
    nav = getattr(m, "nav_client", None)
    loc = getattr(m, "map_loc", None)
    if nav is None:
        log.error_throttle(5.0, "[chassis_following] 没有导航客户端，无法去充电")
        m.finish_charge(False, "没有导航客户端")
        return 0.0, 0.0, 0.0

    t = (m.now() - m.nav_t0).to_sec()
    timeout = float(getattr(m, "nav_timeout", 90.0))
    hx = float(m.charge_x)
    hy = float(m.charge_y)
    hyaw = float(m.charge_yaw)
    tol_xy = float(m.charge_tol_xy)
    tol_yaw = float(m.charge_tol_yaw)

    if not m.nav_sent:
        nav.send_pose(
            hx, hy, hyaw, tol_xy, tol_yaw, label="charge",
            task_type=TaskType.Charge)
        m.nav_sent = True
        return 0.0, 0.0, 0.0

    st = nav.status_of_ours()
    if st is None and t >= 2.0 and t < 2.2:
        log.warn("[chassis_following] 充电导航未收到 goal，重发一次")
        nav.send_pose(
            hx, hy, hyaw, tol_xy, tol_yaw, label="charge",
            task_type=TaskType.Charge)

    pose = loc.pose() if loc is not None else None
    if pose is not None:
        log.info_throttle(
            2.0,
            "[chassis_following] 去充电 map=(%.3f, %.3f, %.1fdeg) "
            "目标=(%.3f, %.3f, %.1fdeg) status=%s",
            pose[0], pose[1], math.degrees(pose[2]),
            hx, hy, math.degrees(hyaw), st)

    arrived = bool(loc is not None and loc.ready() and
                   loc.at_pose(hx, hy, hyaw, tol_xy, tol_yaw))
    moving = st in (GoalStatus.ACTIVE, GoalStatus.SUCCEEDED)
    done = nav.succeeded() or (t > 5.0 and arrived and moving)
    if st in (None, GoalStatus.PENDING, GoalStatus.ACTIVE):
        failed = t >= timeout
    else:
        failed = nav.failed() or t >= timeout
    if failed and not done:
        log.warn(
            "[chassis_following] 去充电未到位（t=%.1fs status=%s）", t, st)
        m.finish_charge(False, "去充电未到位")
        return 0.0, 0.0, 0.0
    if not done:
        return 0.0, 0.0, 0.0

    ok, msg = m.enable_charge()
    if not ok:
        log.warn("[chassis_following] 已到充电点但未打开充电: %s", msg)
        m.finish_charge(False, msg)
        return 0.0, 0.0, 0.0
    m.nav_sent = False
    m.state = "CHARGING"
    log.info("[chassis_following] 已打开充电，等待结束充电")
    return 0.0, 0.0, 0.0
