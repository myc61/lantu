# -*- coding: utf-8 -*-
"""NAV_CHARGE：姿态变换点 → 充电姿态 → 准备点多点导航 → 后向充电。

一次导航目标只有一个 task_type。
姿态变换点是单点普通导航。准备点按顺序放进另一次普通导航的 waypoints。
后向充电再单独发一次，waypoints 里只有充电点。
"""
import math

from chassis_following.runlog import log
from actionlib_msgs.msg import GoalStatus
from navigation.msg import TaskType

# 现场没放充电桩。True 时准备点到位后不再发后向充电，也不请求 agv_charge。
_SKIP_DOCK_AND_AGV = True


def _settled(m, nav, name):
    waited = (m.now() - m.nav_t0).to_sec()
    if nav.still_running() and waited < 2.0:
        return False
    if nav.still_running():
        log.warn(
            "[chassis_following] %s %.1fs 仍未结束 status=%s，继续下一步",
            name, waited, nav.status_of_ours())
    return True


def _leg(m):
    phase = getattr(m, "charge_phase", "transform")
    routine_xy = float(m.map_arrive_tol_xy)
    routine_yaw = float(m.map_arrive_tol_yaw)
    if phase == "transform":
        pose = (
            float(m.charge_transform_x),
            float(m.charge_transform_y),
            float(m.charge_transform_yaw),
        )
        return {
            "name": "姿态变换点",
            "label": "transform",
            "poses": [pose],
            "goal": pose,
            "tol_xy": routine_xy,
            "tol_yaw": routine_yaw,
            "task_type": TaskType.Routine,
        }
    if phase in ("prepare", "exit_prepare"):
        poses = list(getattr(m, "charge_prepare", None) or [])
        # 去充电时途经点在前、最后一点是准备点。结束充电只回到这个准备点。
        if phase == "exit_prepare" and poses:
            poses = [poses[-1]]
        goal = poses[-1] if poses else (0.0, 0.0, 0.0)
        return {
            "name": "准备点",
            "label": "prepare" if phase == "prepare" else "exit_prepare",
            "poses": poses,
            "goal": goal,
            "tol_xy": routine_xy,
            "tol_yaw": routine_yaw,
            "task_type": TaskType.Routine,
        }
    if phase == "exit_transform":
        pose = (
            float(m.charge_transform_x),
            float(m.charge_transform_y),
            float(m.charge_transform_yaw),
        )
        return {
            "name": "姿态变换点",
            "label": "exit_transform",
            "poses": [pose],
            "goal": pose,
            "tol_xy": routine_xy,
            "tol_yaw": routine_yaw,
            "task_type": TaskType.Routine,
        }
    pose = (float(m.charge_x), float(m.charge_y), float(m.charge_yaw))
    return {
        "name": "后向充电",
        "label": "charge",
        "poses": [pose],
        "goal": pose,
        "tol_xy": float(m.charge_tol_xy),
        "tol_yaw": float(m.charge_tol_yaw),
        "task_type": TaskType.Charge,
    }


def _send(nav, leg):
    if not leg["poses"]:
        return None
    return nav.send_waypoints(
        leg["poses"], leg["tol_xy"], leg["tol_yaw"],
        label=leg["label"], task_type=leg["task_type"])


def _run_mid_pose(m):
    arm = getattr(m, "arm", None)
    if arm is None:
        m.finish_charge(False, "没有手臂控制器")
        return 0.0, 0.0, 0.0
    log.info("[chassis_following] 姿态变换点已到，开始摆充电姿态")
    m.charge_phase = "posing"
    try:
        ok = bool(arm.charge_pose())
    except Exception as e:
        ok = False
        log.error("[chassis_following] 充电姿态失败: %s", e)
    if m.charge_phase != "posing" or m.state != "NAV_CHARGE":
        log.info("[chassis_following] 充电姿态期间已改道，不再发准备点")
        return 0.0, 0.0, 0.0
    if not ok:
        m.finish_charge(False, "充电姿态失败，未发准备点导航")
        return 0.0, 0.0, 0.0
    m.charge_phase = "prepare"
    m.nav_sent = False
    m.nav_t0 = m.now()
    log.info(
        "[chassis_following] 充电姿态完成，准备点 %d 个写入 waypoints",
        len(getattr(m, "charge_prepare", None) or []))
    return 0.0, 0.0, 0.0


def _run_exit_pose(m):
    """回到姿态变换点后，摆成可以离开 home 的姿态，再导航回 home。"""
    arm = getattr(m, "arm", None)
    m.charge_phase = "exiting_pose"
    ok = False
    if arm is None:
        log.error("[chassis_following] 没有手臂控制器，跳过离开 home 的姿态")
    else:
        log.info("[chassis_following] 已回姿态变换点，摆离开 home 的姿态")
        try:
            ok = bool(arm.leave_home_pose())
        except Exception as e:
            log.error("[chassis_following] 离开 home 的姿态失败: %s", e)
    if m.charge_phase != "exiting_pose" or m.state != "NAV_CHARGE":
        log.info("[chassis_following] 离开 home 的姿态期间已改道，不再发回家")
        return 0.0, 0.0, 0.0
    if not ok:
        log.warn("[chassis_following] 离开 home 的姿态未完成，仍导航回 home")
    m.go_home(why="结束充电")
    return 0.0, 0.0, 0.0


def _after_leg(m, nav, phase):
    """当前段到位。上一段还在走时先取消，避免取消打到下一段。"""
    nxt = {
        "transform": ("pose", "transform_settle", "姿态变换点已到"),
        "prepare": (
            ("hold", "prepare_settle", "准备点已到，跳过后向充电和打开充电")
            if _SKIP_DOCK_AND_AGV else
            ("dock", "prepare_settle", "准备点已到")),
        "exit_prepare": ("exit_transform", "exit_prepare_settle", "已回到准备点"),
        "exit_transform": ("exit_pose", "exit_transform_settle", "已回到姿态变换点"),
    }.get(phase)
    if nxt is None:
        return
    nxt, settle, text = nxt
    if nav.still_running():
        nav.cancel()
        m.charge_phase = settle
        m.nav_t0 = m.now()
        log.info("[chassis_following] %s，等该目标结束后继续", text)
        return
    log.info("[chassis_following] %s", text)
    m.charge_phase = nxt
    m.nav_sent = False
    m.nav_t0 = m.now()


def run(m):
    nav = getattr(m, "nav_client", None)
    loc = getattr(m, "map_loc", None)
    if nav is None:
        log.error_throttle(5.0, "[chassis_following] 没有导航客户端，无法去充电")
        m.finish_charge(False, "没有导航客户端")
        return 0.0, 0.0, 0.0

    phase = getattr(m, "charge_phase", "transform")
    if phase == "transform_settle":
        if not _settled(m, nav, "姿态变换点"):
            return 0.0, 0.0, 0.0
        m.charge_phase = "pose"
        phase = "pose"
    elif phase == "prepare_settle":
        if not _settled(m, nav, "准备点"):
            return 0.0, 0.0, 0.0
        m.charge_phase = "hold" if _SKIP_DOCK_AND_AGV else "dock"
        m.nav_sent = False
        m.nav_t0 = m.now()
        phase = m.charge_phase
    elif phase == "exit_prepare_settle":
        if not _settled(m, nav, "准备点"):
            return 0.0, 0.0, 0.0
        m.charge_phase = "exit_transform"
        m.nav_sent = False
        m.nav_t0 = m.now()
        phase = "exit_transform"
    elif phase == "exit_transform_settle":
        if not _settled(m, nav, "姿态变换点"):
            return 0.0, 0.0, 0.0
        m.charge_phase = "exit_pose"
        phase = "exit_pose"

    if _SKIP_DOCK_AND_AGV and phase == "dock":
        phase = "hold"
    if phase == "hold":
        log.info_throttle(
            10.0,
            "[chassis_following] 已在准备点。后向充电和 agv_charge 已跳过，等结束充电")
        return 0.0, 0.0, 0.0
    if phase == "pose":
        return _run_mid_pose(m)
    if phase == "exit_pose":
        return _run_exit_pose(m)
    if phase == "exit_prepare" and not (getattr(m, "charge_prepare", None) or []):
        log.warn("[chassis_following] 没有准备点，结束充电改去姿态变换点")
        m.charge_phase = "exit_transform"
        m.nav_sent = False
        m.nav_t0 = m.now()
        return 0.0, 0.0, 0.0
    if phase == "exit_transform" and not getattr(m, "charge_transform_set", False):
        log.warn("[chassis_following] 没有姿态变换点，结束充电直接摆离开 home 的姿态")
        m.charge_phase = "exit_pose"
        return 0.0, 0.0, 0.0

    t = (m.now() - m.nav_t0).to_sec()
    timeout = float(getattr(m, "nav_timeout", 90.0))
    leg = _leg(m)
    if phase == "prepare" and not leg["poses"]:
        m.finish_charge(False, "未配置充电准备点")
        return 0.0, 0.0, 0.0
    hx, hy, hyaw = leg["goal"]
    tol_xy, tol_yaw = leg["tol_xy"], leg["tol_yaw"]

    if not m.nav_sent:
        if nav.still_running() and t < 2.0:
            return 0.0, 0.0, 0.0
        if _send(nav, leg) is None:
            if phase in ("exit_prepare", "exit_transform"):
                log.warn("[chassis_following] 结束充电 %s下发失败，继续下一步", leg["name"])
                m.charge_phase = (
                    "exit_transform" if phase == "exit_prepare" else "exit_pose")
                m.nav_sent = False
                m.nav_t0 = m.now()
                return 0.0, 0.0, 0.0
            m.finish_charge(False, "%s下发失败" % leg["name"])
            return 0.0, 0.0, 0.0
        m.nav_sent = True
        log.info(
            "[chassis_following] %s waypoints=%d 末点=(%.3f, %.3f, %.1fdeg)",
            leg["name"], len(leg["poses"]), hx, hy, math.degrees(hyaw))
        return 0.0, 0.0, 0.0

    st = nav.status_of_ours()
    if st is None and t >= 2.0 and t < 2.2:
        log.warn("[chassis_following] %s未收到 goal，重发一次", leg["name"])
        _send(nav, leg)

    pose = loc.pose() if loc is not None else None
    if pose is not None:
        log.info_throttle(
            2.0,
            "[chassis_following] %s map=(%.3f, %.3f, %.1fdeg) "
            "末点=(%.3f, %.3f, %.1fdeg) status=%s",
            leg["name"],
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
            "[chassis_following] %s未到位（t=%.1fs status=%s）",
            leg["name"], t, st)
        if phase in ("exit_prepare", "exit_transform"):
            log.warn(
                "[chassis_following] 结束充电 %s未到位，继续下一步", leg["name"])
            if nav.still_running():
                nav.cancel()
            m.charge_phase = (
                "exit_prepare_settle" if phase == "exit_prepare"
                else "exit_transform_settle")
            m.nav_t0 = m.now()
            return 0.0, 0.0, 0.0
        m.finish_charge(False, "%s未到位" % leg["name"])
        return 0.0, 0.0, 0.0
    if not done:
        return 0.0, 0.0, 0.0
    if phase in ("transform", "prepare", "exit_prepare", "exit_transform"):
        _after_leg(m, nav, phase)
        return 0.0, 0.0, 0.0

    # 后向充电到位后才请求 agv_charge。_SKIP_DOCK_AND_AGV 为 True 时走不到这里。
    ok, msg = m.enable_charge()
    if not ok:
        log.warn("[chassis_following] 已到充电点但未打开充电: %s", msg)
        m.finish_charge(False, msg)
        return 0.0, 0.0, 0.0
    m.nav_sent = False
    m.state = "CHARGING"
    log.info("[chassis_following] 已打开充电，等待结束充电")
    return 0.0, 0.0, 0.0
