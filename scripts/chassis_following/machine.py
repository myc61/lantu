# -*- coding: utf-8 -*-
"""状态机：扫拍、回家，以及外部触发的停止、复位、充电。"""
import math
import os
import threading
import time
from datetime import datetime

import rospy
from actionlib_msgs.msg import GoalStatus
from chassis_following.capture import new_trace_pair
from chassis_following.runlog import handoff, log

from chassis_following.states import charging as state_charging
from chassis_following.states import fence_hold as state_fence
from chassis_following.states import idle as state_idle
from chassis_following.states import nav_charge as state_charge
from chassis_following.states import nav_return as state_nav
from chassis_following.states import nav_sweep as state_sweep


class FollowingMachine(object):
    def __init__(self, home_controller, wrist, plan, plan_text,
                 arm=None, capture_dir="/tmp/chassis_following",
                 nav_client=None, map_loc=None,
                 map_home_x=0.0, map_home_y=0.0, map_home_yaw=0.0,
                 map_arrive_tol_xy=0.15, map_arrive_tol_yaw=0.017,
                 nav_timeout=90.0,
                 sweep_end_x=1.31, sweep_end_y=-0.23, sweep_end_yaw=0.0,
                 sweep_photo_delay=0.2, sweep_photo_period=0.2,
                 sweep_photo_move_m=0.1):
        self.home_controller = home_controller
        self.wrist = wrist
        self.plan = plan
        self.plan_text = plan_text
        self.arm = arm
        self.capture_dir = capture_dir
        if not os.path.isdir(self.capture_dir):
            os.makedirs(self.capture_dir)
        log.info("[chassis_following] 照片根目录 %s", self.capture_dir)
        self.nav_client = nav_client
        self.map_loc = map_loc
        self.map_home_x = float(map_home_x)
        self.map_home_y = float(map_home_y)
        self.map_home_yaw = float(map_home_yaw)
        self.map_arrive_tol_xy = float(map_arrive_tol_xy)
        self.map_arrive_tol_yaw = float(map_arrive_tol_yaw)
        self.nav_timeout = float(nav_timeout)
        self.sweep_end_x = float(sweep_end_x)
        self.sweep_end_y = float(sweep_end_y)
        self.sweep_end_yaw = float(sweep_end_yaw)
        self.sweep_photo_delay = float(sweep_photo_delay)
        self.sweep_photo_period = float(sweep_photo_period)
        self.sweep_photo_move_m = float(sweep_photo_move_m)
        self.charge_transform_set = False
        self.charge_transform_x = 0.0
        self.charge_transform_y = 0.0
        self.charge_transform_yaw = 0.0
        self.charge_prepare = []
        self.charge_phase = "transform"
        self._charge_exit = False
        self.charge_x = 0.5
        self.charge_y = 1.7
        self.charge_yaw = math.radians(90.0)
        self.charge_tol_xy = 0.1
        self.charge_tol_yaw = math.radians(5.73)
        self.agv = None
        self.charging = False
        self.distance = None
        self.nav_sent = False
        self.nav_t0 = rospy.Time(0)
        self.photo_clock_t0 = None
        self.last_shot_stamp = None
        self.sweep_origin = None
        self.stop_flag = False
        self.paused = False
        self.await_nav_settle = False
        self._pause_started = None
        self.obstacle_zero_hold_sec = 0.3
        self._cmd_moved = False
        self._cmd_zero = False
        self._cmd_zero_since = None
        self._reset_return = False
        self._obstacle_return = False
        self._reset_signal = False
        self.sweep_notes = []
        self._shot_feedback_pending = 0
        self._deferred_home_note = None
        self.end_on_front = False

        self.state = "IDLE"
        self.pose_x = 0.0
        self.pose_y = 0.0
        self.pose_yaw = 0.0
        self.odom_received = False
        self.home_recorded = False
        self.capture_count = 0
        self.last_trace_id_l = ""
        self.last_trace_id_r = ""
        self.last_shot_x = 0.0
        self.last_shot_y = 0.0
        self.run_dir = None
        self.trip_done = False
        self.last_run_ok = False
        self.last_run_shots = 0
        self.last_run_dir = ""
        self.last_run_message = ""

        self._lock = threading.Lock()
        self._task_lock = threading.Lock()
        self._task_t0 = None
        self._task_last = None
        self._task_once = set()
        self.need_confirm = None
        self._asked = None

    def begin_task_clock(self):
        """本趟计时零点：动作回调刚收到任务。"""
        now = time.monotonic()
        with self._task_lock:
            self._task_t0 = now
            self._task_last = now
            self._task_once = set()
        handoff("后端收到任务")
        log.info("[timing] +0.000s 本段 0.000s 收到任务")

    def mark_task(self, stage, once=False):
        """相对收到任务的时间。本段是距上一条计时日志的间隔。"""
        with self._task_lock:
            t0 = self._task_t0
            if t0 is None:
                return
            if once:
                if stage in self._task_once:
                    return
                self._task_once.add(stage)
            now = time.monotonic()
            elapsed = now - t0
            step = now - (self._task_last if self._task_last is not None else now)
            self._task_last = now
        log.info("[timing] +%.3fs 本段 %.3fs %s", elapsed, step, stage)

    def _reject_task(self, reason):
        self.mark_task("开扫失败：%s" % reason)
        return False, reason

    def set_plan(self, plan, plan_text):
        self.plan = plan
        self.plan_text = plan_text

    def reset_prompt(self):
        with self._lock:
            self._asked = None

    def now(self):
        return rospy.Time.now()

    def set_pose(self, x, y, yaw, odom_received=True):
        self.pose_x = x
        self.pose_y = y
        self.pose_yaw = yaw
        self.odom_received = bool(odom_received)

    def request_stop(self):
        """外部停止：调用一次就取消导航，进入 STOP_HOLD，停住直到复位。"""
        with self._lock:
            if self.state == "STOP_HOLD":
                return True, "已在停止"
            self.stop_flag = True
            self._enter_stop_hold()
            return True, "已取消导航并停止"

    def stop_requested(self):
        return bool(self.stop_flag)

    def start_sweep(self, why=""):
        """车头到位：导航去扫拍终点，起步后按间隔拍照，到点再导航回家。"""
        with self._lock:
            self.mark_task("进入定时开扫")
            if self.stop_requested() or self.state == "STOP_HOLD":
                return self._reject_task("外部停障中，不能开扫")
            if self.state != "IDLE":
                return self._reject_task("已经在跑（%s）" % self.state)
            loc = getattr(self, "map_loc", None)
            if loc is None or not loc.ready():
                return self._reject_task("地图定位还没有，不能开导航扫拍")
            if self.nav_client is None:
                return self._reject_task("没有导航客户端")
            self.mark_task("开扫检查通过")
            self.nav_client.cancel()
            self.mark_task("已请求取消上一段导航")
            self.need_confirm = None
            self._asked = None
            self.capture_count = 0
            self.run_dir = None
            self.trip_done = False
            self.last_run_ok = False
            self.last_run_shots = 0
            self.last_run_dir = ""
            self.last_run_message = ""
            self.nav_sent = False
            self.nav_t0 = rospy.Time.now()
            self.photo_clock_t0 = None
            self.last_shot_stamp = None
            self.sweep_origin = None
            self._cmd_moved = False
            self._cmd_zero_since = None
            self._reset_return = False
            self._reset_signal = False
            self.paused = False
            self.await_nav_settle = False
            self._pause_started = None
            self.sweep_notes = []
            self._shot_feedback_pending = 0
            self._deferred_home_note = None
            self.end_on_front = False
            self.set_plan({"mode": "nav_sweep"}, self.nav_text())
            self.state = "NAV_SWEEP"
            self.mark_task("状态切到 NAV_SWEEP")
            log.info("[chassis_following] 开扫(%s)\n%s", why, self.plan_text)
            return True, "开始导航扫拍"

    def start_front_sweep(self, task_id, vehicle_type, speed, received_at):
        """定时扫拍，结束条件和欧式距离一样：超过车头就停拍并回家。"""
        with self._lock:
            self.mark_task("进入定时开扫（超过车端结束）")
            if self.stop_requested() or self.state == "STOP_HOLD":
                return self._reject_task("外部停障中，不能开扫")
            if self.state != "IDLE":
                return self._reject_task("已经在跑（%s）" % self.state)
            loc = getattr(self, "map_loc", None)
            if loc is None or not loc.ready():
                return self._reject_task("地图定位还没有，不能开导航扫拍")
            if self.nav_client is None:
                return self._reject_task("没有导航客户端")
            ctrl = self.distance
            if ctrl is None:
                return self._reject_task("车型任务未加载")
            pose = loc.pose()
            if pose is None:
                return self._reject_task("地图定位还没有，不能开导航扫拍")
            self.mark_task("开扫检查通过")
            ok, msg = ctrl.begin(
                task_id, vehicle_type, pose[0], pose[1], speed, received_at,
                home_x=self.map_home_x, home_y=self.map_home_y)
            if not ok:
                return self._reject_task(msg)
            self.nav_client.cancel()
            self.mark_task("已请求取消上一段导航")
            self.need_confirm = None
            self._asked = None
            self.capture_count = 0
            self.run_dir = None
            self.trip_done = False
            self.last_run_ok = False
            self.last_run_shots = 0
            self.last_run_dir = ""
            self.last_run_message = ""
            self.nav_sent = False
            self.nav_t0 = rospy.Time.now()
            self.photo_clock_t0 = None
            self.last_shot_stamp = None
            self.sweep_origin = None
            self._cmd_moved = False
            self._cmd_zero_since = None
            self._reset_return = False
            self._reset_signal = False
            self.paused = False
            self.await_nav_settle = False
            self._pause_started = None
            self.sweep_notes = []
            self._shot_feedback_pending = 0
            self._deferred_home_note = None
            self.end_on_front = True
            end_name = ctrl.vehicle_end_name()
            text = (
                "定时扫拍  车型=%s  任务=%s  终点 map=(%.3f, %.3f, %.1fdeg)  "
                "按间隔拍照，超过%s后回家"
                % (vehicle_type, task_id or "-",
                   self.sweep_end_x, self.sweep_end_y,
                   math.degrees(self.sweep_end_yaw), end_name))
            self.set_plan({"mode": "nav_sweep"}, text)
            self.plan_text = text
            self.state = "NAV_SWEEP"
            self.mark_task("状态切到 NAV_SWEEP")
            log.info("[chassis_following] 开扫(超过%s结束)\n%s", end_name, text)
            return True, "开始导航扫拍，超过%s结束" % end_name

    def start_distance_sweep(self, task_id, vehicle_type, speed, received_at):
        """欧式距离扫拍：导航去终点，按检测点窗口拍照，超过车头后再回家。"""
        with self._lock:
            self.mark_task("进入欧式距离开扫")
            if self.stop_requested() or self.state == "STOP_HOLD":
                return self._reject_task("外部停障中，不能开扫")
            if self.state != "IDLE":
                return self._reject_task("已经在跑（%s）" % self.state)
            loc = getattr(self, "map_loc", None)
            if loc is None or not loc.ready():
                return self._reject_task("地图定位还没有，不能开导航扫拍")
            if self.nav_client is None:
                return self._reject_task("没有导航客户端")
            ctrl = self.distance
            if ctrl is None:
                return self._reject_task("欧式距离任务未加载")
            pose = loc.pose()
            if pose is None:
                return self._reject_task("地图定位还没有，不能开导航扫拍")
            self.mark_task("开扫检查通过")
            ok, msg = ctrl.begin(
                task_id, vehicle_type, pose[0], pose[1], speed, received_at,
                home_x=self.map_home_x, home_y=self.map_home_y)
            if not ok:
                return self._reject_task(msg)
            self.nav_client.cancel()
            self.mark_task("已请求取消上一段导航")
            self.need_confirm = None
            self._asked = None
            self.capture_count = 0
            self.run_dir = None
            self.trip_done = False
            self.last_run_ok = False
            self.last_run_shots = 0
            self.last_run_dir = ""
            self.last_run_message = ""
            self.nav_sent = False
            self.nav_t0 = rospy.Time.now()
            self.photo_clock_t0 = None
            self.last_shot_stamp = None
            self.sweep_origin = None
            self._cmd_moved = False
            self._cmd_zero_since = None
            self._reset_return = False
            self._reset_signal = False
            self.paused = False
            self.await_nav_settle = False
            self._pause_started = None
            self.sweep_notes = []
            self._shot_feedback_pending = 0
            self._deferred_home_note = None
            self.end_on_front = False
            end_name = ctrl.vehicle_end_name()
            text = (
                "欧式距离扫拍  车型=%s  任务=%s  终点 map=(%.3f, %.3f, %.1fdeg)  "
                "按检测点窗口拍照，全部完成且超过%s后回家"
                % (vehicle_type, task_id or "-",
                   self.sweep_end_x, self.sweep_end_y,
                   math.degrees(self.sweep_end_yaw), end_name))
            self.set_plan({"mode": "nav_distance"}, text)
            self.plan_text = text
            self.state = "NAV_DISTANCE"
            self.mark_task("状态切到 NAV_DISTANCE")
            log.info("[chassis_following] 开欧式距离扫拍\n%s", text)
            ctrl.pose_at_start(self)
            self.mark_task("开扫姿态已安排")
            return True, msg

    def nav_text(self):
        return (
            "导航扫拍  终点 map=(%.3f, %.3f, %.1fdeg)  "
            "底盘离开起点 %.2fm 后再过 %.2fs 拍第1组，之后每 %.2fs 一组；"
            "触发拍照，传图可叠、回家途中继续存"
            % (self.sweep_end_x, self.sweep_end_y,
               math.degrees(self.sweep_end_yaw),
               self.sweep_photo_move_m,
               self.sweep_photo_delay, self.sweep_photo_period))

    def _finish_distance(self):
        ctrl = getattr(self, "distance", None)
        if ctrl is not None:
            ctrl.finish()

    def request_pause(self):
        """停线：底盘速度记为 0，取消导航。扫拍状态和本趟任务都保持。"""
        with self._lock:
            if self.paused:
                return True, "已暂停，导航已取消"
            if self.state not in ("NAV_SWEEP", "NAV_DISTANCE"):
                return False, "当前不在扫拍（%s），不能暂停" % self.state
            ctrl = self.distance
            if ctrl is None or not ctrl.active:
                return False, "当前没有在按车速扫拍，不能暂停"
            ok, msg = ctrl.pause_line()
            if not ok:
                return False, msg
            self.paused = True
            self.await_nav_settle = True
            self.nav_sent = False
            self.nav_t0 = self.now()
            if self._pause_started is None:
                self._pause_started = self.now()
            self._cmd_moved = False
            self._cmd_zero = False
            self._cmd_zero_since = None
            nav = self.nav_client
        if nav is not None:
            nav.cancel()
        log.warn("[chassis_following] 暂停，底盘速度 0，已取消导航，本趟任务不结束")
        return True, "已暂停，底盘速度 0，导航已取消"

    def request_continue(self):
        """继续：恢复暂停前的底盘速度，重新发原来的扫拍导航，按剩余点位接着拍。"""
        with self._lock:
            if not self.paused:
                return False, "当前没有暂停"
            if self.state not in ("NAV_SWEEP", "NAV_DISTANCE"):
                self.paused = False
                self._pause_started = None
                return False, "当前不在扫拍（%s），不能继续" % self.state
            ctrl = self.distance
            if ctrl is None or not ctrl.line_paused:
                return False, "当前没有暂停的扫拍"
            now = self.now()
            speed = float(ctrl.held_speed())
            self.paused = False
            self.await_nav_settle = True
            self.nav_sent = False
            self.nav_t0 = now
            self._cmd_moved = False
            self._cmd_zero = False
            self._cmd_zero_since = None
            nav = self.nav_client
            if nav is not None:
                nav.cancel()
        log.warn(
            "[chassis_following] 继续扫拍，底盘速度 %.3f m/s，重新导航",
            speed)
        return True, "已继续，按原速度 %.3f m/s 重新导航" % speed

    def try_begin_sweep_nav(self, x, y, yaw, tol_xy, tol_yaw, label):
        """发扫拍导航。暂停中不发。上一段还在取消时先等，避免取消打到新目标上。"""
        with self._lock:
            if self.paused or self.stop_flag or self.state not in (
                    "NAV_SWEEP", "NAV_DISTANCE"):
                return False
            nav = self.nav_client
            if nav is None:
                return False
            if self.await_nav_settle and nav.still_running():
                waited = (self.now() - self.nav_t0).to_sec()
                if waited < 2.0:
                    self.mark_task("等待上一段导航结束再发目标", once=True)
                    log.info_throttle(
                        1.0,
                        "[chassis_following] 等待上一段导航结束再继续扫拍 "
                        "status=%s t=%.1fs",
                        nav.status_of_ours(), waited)
                    return False
                log.warn(
                    "[chassis_following] 上一段导航 %.1fs 仍未结束 status=%s，仍发扫拍",
                    waited, nav.status_of_ours())
            self.await_nav_settle = False
            self._release_pause_motion()
            self.mark_task("发送扫拍导航目标")
            nav.send_pose(x, y, yaw, tol_xy, tol_yaw, label=label)
            self.nav_sent = True
            self.mark_task("扫拍导航目标已发出")
            return True

    def _release_pause_motion(self):
        """新导航发出时才恢复车速，定时拍照不把暂停这段时间算进去。调用方已持锁。"""
        ctrl = self.distance
        if ctrl is not None and ctrl.line_paused:
            ok, msg = ctrl.resume_line()
            if not ok:
                log.warn("[chassis_following] 恢复底盘速度失败: %s", msg)
        if self._pause_started is None:
            return
        dt = (self.now() - self._pause_started).to_sec()
        self._pause_started = None
        if dt <= 0.0:
            return
        shift = rospy.Duration(dt)
        if self.photo_clock_t0 is not None:
            self.photo_clock_t0 = self.photo_clock_t0 + shift
        if self.last_shot_stamp is not None:
            self.last_shot_stamp = self.last_shot_stamp + shift

    def send_sweep_goal(self, x, y, yaw, tol_xy, tol_yaw, label):
        """重发当前扫拍目标。暂停或急停中不发。"""
        with self._lock:
            if self.paused or self.stop_flag or self.state not in (
                    "NAV_SWEEP", "NAV_DISTANCE"):
                return False
            nav = self.nav_client
            if nav is None:
                return False
            self.mark_task("重发扫拍导航目标")
            nav.send_pose(x, y, yaw, tol_xy, tol_yaw, label=label)
            self.nav_sent = True
            self.mark_task("扫拍导航目标已发出")
            return True

    def go_home(self, why="扫拍结束"):
        """导航回地图起点，等车头到位。不另发扫拍结束 feedback，只保留拍摄那几条。"""
        if self.paused and self.state in ("NAV_SWEEP", "NAV_DISTANCE"):
            log.warn("[chassis_following] 暂停中，扫拍不结束（%s）", why)
            return
        if self.state in ("NAV_SWEEP", "NAV_DISTANCE"):
            self._finish_distance()
        self._hush_capture()
        if self.nav_client is None:
            log.error("[chassis_following] 没有导航客户端，无法回家")
            self._record_run_unlocked(False, "没有导航客户端，无法回家")
            self.state = "IDLE"
            return
        if not self.nav_client.succeeded():
            self.nav_client.cancel()
        self.nav_sent = False
        self.nav_t0 = rospy.Time.now()
        self.state = "NAV_RETURN"
        log.info(
            "[chassis_following] %s，导航回 map=(%.3f, %.3f, %.1fdeg)",
            why, self.map_home_x, self.map_home_y,
            math.degrees(self.map_home_yaw))

    def _note_sweep_done(self, ok, message, trace_id_l="", trace_id_r=""):
        """记一条 feedback。每拍一组一条，扫拍结束回家时再记一条。

        回家这条带上一组的 trace_id。拍摄那条在 TCP 请求发出后才记。
        若回家这条来得更早，先按住，等拍摄反馈进队后再记。
        """
        with self._lock:
            item = (
                bool(ok), message or "", int(self.capture_count),
                self.run_dir or "", trace_id_l or "", trace_id_r or "")
            if self._shot_feedback_pending > 0:
                self._deferred_home_note = item
                return
            self.sweep_notes.append(item)

    def _finish_shot_feedback(self, note, trace_id_l, trace_id_r):
        """TCP 请求已发出或确定发不出去之后，把左右 trace_id 放进 feedback。"""
        with self._lock:
            if note:
                self.sweep_notes.append((
                    True, note, int(self.capture_count),
                    self.run_dir or "", trace_id_l or "", trace_id_r or ""))
            self._shot_feedback_pending = max(0, self._shot_feedback_pending - 1)
            if (self._shot_feedback_pending == 0
                    and self._deferred_home_note is not None):
                self.sweep_notes.append(self._deferred_home_note)
                self._deferred_home_note = None

    def pop_sweep_notes(self):
        with self._lock:
            notes = self.sweep_notes
            self.sweep_notes = []
            return notes

    def _hush_capture(self):
        """回家/停障后后台传图"""
        wrist = getattr(self, "wrist", None)
        hush = getattr(wrist, "hush", None)
        if callable(hush):
            hush()

    def enable_charge(self):
        agv = getattr(self, "agv", None)
        if agv is None:
            return False, "没有充电客户端"
        ok, msg = agv.enable(True)
        self.charging = bool(ok)
        return ok, msg

    def disable_charge(self):
        if not self.charging:
            return True, ""
        agv = getattr(self, "agv", None)
        if agv is None:
            self.charging = False
            return False, "没有充电客户端"
        ok, msg = agv.enable(False)
        if ok:
            self.charging = False
        return ok, msg

    def finish_charge(self, ok, message):
        """离开充电：关掉充电后进入 NAV_RETURN，导航回 home。"""
        off_ok, off_msg = self.disable_charge()
        if self.charging and not off_ok:
            log.warn("[chassis_following] 关闭充电失败，仍导航回 home: %s", off_msg)
        log.warn("[chassis_following] 充电结束 success=%s %s", ok, message)
        self.trip_done = False
        self.go_home(why=message or "离开充电")

    def start_charge(self):
        """外部触发：离开 home 的姿态，再到姿态变换点、充电姿态、准备点、后向充电。"""
        with self._lock:
            if self.stop_requested() or self.state == "STOP_HOLD":
                return False, "外部停障中，不能充电"
            if self.state != "IDLE":
                return False, "正在%s，不能充电" % self.state
            loc = getattr(self, "map_loc", None)
            if loc is None or not loc.ready():
                return False, "地图定位还没有，不能去充电"
            if self.nav_client is None:
                return False, "没有导航客户端"
            arm = self.arm
            if arm is None:
                return False, "没有手臂控制器，不能去充电"
            leave = getattr(arm, "charge_joints", None) or []
            posed = getattr(arm, "charge_pose_joints", None) or []
            if len(leave) != 22:
                return False, "未配置离开 home 的姿态，未发导航"
            if len(posed) != 22:
                return False, "未配置充电姿态，未发导航"
            if not getattr(self, "charge_transform_set", False):
                return False, "未配置姿态变换点，未发导航"
            if not getattr(self, "charge_prepare", None):
                return False, "未配置充电准备点，未发导航"
            self.nav_client.cancel()
            self.charging = False
            self.need_confirm = None
            self._asked = None
            self.trip_done = False
            self.last_run_ok = False
            self.last_run_message = ""
            self.nav_sent = False
            self.nav_t0 = rospy.Time.now()
            self._reset_signal = False
            self.paused = False
            self.await_nav_settle = False
            self._pause_started = None
            self.state = "CHARGE_POSE"
        log.info("[chassis_following] 去充电前先摆离开 home 的姿态，完成前不发导航")
        try:
            ok = bool(arm.leave_home_pose())
        except Exception as e:
            ok = False
            log.error("[chassis_following] 离开 home 的姿态失败: %s", e)
        with self._lock:
            if self.state != "CHARGE_POSE":
                return False, "离开 home 的姿态期间已停止，未发导航"
            if not ok:
                self.state = "IDLE"
                return False, "离开 home 的姿态失败，未发导航"
            self.charge_phase = "transform"
            self.nav_sent = False
            self.nav_t0 = self.now()
            self.state = "NAV_CHARGE"
            log.info(
                "[chassis_following] 离开 home 的姿态完成，单点导航到姿态变换点 "
                "map=(%.3f, %.3f, %.1fdeg)",
                self.charge_transform_x, self.charge_transform_y,
                math.degrees(self.charge_transform_yaw))
            return True, "离开 home 的姿态完成，开始去姿态变换点"

    def end_charge(self):
        """外部触发：关充电后先回准备点，再回姿态变换点，摆离开 home 的姿态，再回 home。"""
        with self._lock:
            if self.stop_requested() or self.state == "STOP_HOLD":
                return False, "外部停障中，不能结束充电"
            if self.state == "CHARGE_POSE":
                self.state = "IDLE"
                self.nav_sent = False
                self._charge_exit = False
                return True, "离开 home 的姿态已取消，未发导航"
            if self.state not in ("NAV_CHARGE", "CHARGING") and not self.charging:
                return False, "当前没在充电（%s）" % self.state
            ok, msg = self.disable_charge()
            self.trip_done = False
            self._charge_exit = True
            self.charge_phase = "exit_prepare"
            self.nav_sent = False
            self.nav_t0 = self.now()
            nav = self.nav_client
            if nav is not None and not nav.succeeded():
                nav.cancel()
            self.state = "NAV_CHARGE"
            log.info("[chassis_following] 结束充电，先导航回准备点")
            if self.charging and not ok:
                log.warn(
                    "[chassis_following] 关闭充电失败，仍按原路返回: %s", msg)
                return False, "关闭充电失败，已开始回准备点: %s" % (msg or "")
            return True, "已关充电，先回准备点"

    def reset_external(self):
        """外部复位：仅 STOP_HOLD 时导航回 home，到位后由 NAV_RETURN 进入 IDLE。"""
        with self._lock:
            if self.state != "STOP_HOLD":
                return False, "当前不是停止（%s），不能复位" % self.state
            self.stop_flag = False
            self.disable_charge()
            self.need_confirm = None
            self._asked = None
            self.trip_done = False
            self._reset_return = True
            self.go_home(why="外部复位")
            log.warn("[chassis_following] 外部复位，导航回 home，到位后进 IDLE")
            return True, "已复位，导航回 home，到位后进 IDLE"

    def note_cmd_vel(self, msg):
        """记下避障速度。拍摄任务里曾动过又持续为 0，由 _poll_obstacle 停车。"""
        lin = msg.linear
        ang = msg.angular
        eps = 1e-3
        zero = (
            abs(lin.x) < eps and abs(lin.y) < eps and abs(lin.z) < eps
            and abs(ang.x) < eps and abs(ang.y) < eps and abs(ang.z) < eps)
        with self._lock:
            if self.paused:
                self._cmd_zero = True
                self._cmd_moved = False
                self._cmd_zero_since = None
                return
            self._cmd_zero = zero
            if zero:
                if self._cmd_zero_since is None:
                    self._cmd_zero_since = self.now()
            else:
                self._cmd_moved = True
                self._cmd_zero_since = None

    def safety_label(self):
        """stop=外部停止中。reset=避障或外部复位已经回到 home。其余 none。"""
        with self._lock:
            if self.state == "STOP_HOLD":
                return "stop"
            if self._reset_signal:
                return "reset"
            return "none"

    def _at_sweep_goal(self):
        nav = self.nav_client
        if nav is not None and nav.succeeded():
            return True
        loc = self.map_loc
        if loc is None or not loc.ready():
            return False
        return loc.at_pose(
            self.sweep_end_x, self.sweep_end_y, self.sweep_end_yaw,
            self.map_arrive_tol_xy, self.map_arrive_tol_yaw)

    def _poll_obstacle(self):
        """拍摄中速度曾非 0 又持续全 0：任务失败，随即导航回 home。到家后发复位。"""
        with self._lock:
            if self.paused or self.state not in ("NAV_SWEEP", "NAV_DISTANCE"):
                return
            if self.stop_flag or not self._cmd_moved or not self._cmd_zero:
                return
            if self._cmd_zero_since is None:
                return
            held = (self.now() - self._cmd_zero_since).to_sec()
            if held < float(self.obstacle_zero_hold_sec):
                return
            if self._at_sweep_goal():
                return
            self._enter_stop_hold("自身避障速度为0")
            self.stop_flag = False
            self._obstacle_return = True
            self.go_home(why="自身避障")

    def _enter_stop_hold(self, reason="外部停障"):
        prev = self.state
        self._hush_capture()
        self.disable_charge()
        if self.nav_client is not None:
            self.nav_client.cancel()
        self.need_confirm = None
        self._asked = None
        self.nav_sent = False
        self._reset_return = False
        self._reset_signal = False
        self.paused = False
        self.await_nav_settle = False
        self._pause_started = None
        self._cmd_moved = False
        self._cmd_zero_since = None
        self.state = "STOP_HOLD"
        if prev in ("NAV_SWEEP", "NAV_DISTANCE"):
            self._finish_distance()
        self._record_run_unlocked(False, reason)
        log.warn(
            "[chassis_following] 立即停车（%s，原状态 %s）", reason, prev)

    def _record_run_unlocked(self, ok, message):
        """记下本趟结果。调用方须已持锁或单线程。"""
        if self.trip_done:
            return
        self.last_run_ok = bool(ok)
        self.last_run_shots = int(self.capture_count)
        self.last_run_dir = self.run_dir or ""
        self.last_run_message = message or ""
        self.trip_done = True

    def record_run(self, ok, message):
        with self._lock:
            self._record_run_unlocked(ok, message)

    def snapshot_run(self):
        with self._lock:
            return (
                self.trip_done,
                self.last_run_ok,
                self.last_run_shots,
                self.last_run_dir,
                self.last_run_message,
                self.state,
            )

    def _sync_stop(self):
        with self._lock:
            if self.stop_flag and self.state != "STOP_HOLD":
                self._enter_stop_hold()

    def _ensure_run_dir(self):
        """本趟第一张拍照时建目录，名为本地时间。"""
        if self.run_dir:
            return self.run_dir
        if not os.path.isdir(self.capture_dir):
            os.makedirs(self.capture_dir)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = os.path.join(self.capture_dir, stamp)
        n = 2
        while os.path.exists(path):
            path = os.path.join(self.capture_dir, "%s_%d" % (stamp, n))
            n += 1
        os.makedirs(path)
        self.run_dir = path
        log.info("[chassis_following] 本趟照片目录 %s", path)
        return path

    def trigger_photo(self, sides=None, note=""):
        """立刻触发一组。sides 缺省为左右都拍。

        note 非空时，左右腕 TCP 请求发出后把 trace_id_L、trace_id_R 记进 feedback。
        """
        if sides is None:
            sides = ("left", "right")
        else:
            sides = tuple(sides)
        want_left = "left" in sides
        want_right = "right" in sides
        n = self.capture_count + 1
        folder = self._ensure_run_dir()
        trace_l, trace_r = new_trace_pair()
        if not want_left:
            trace_l = ""
        if not want_right:
            trace_r = ""
        self.last_trace_id_l = trace_l
        self.last_trace_id_r = trace_r
        now = datetime.now()
        stamp = now.strftime("%Y%m%d-%H%M%S-%f")[:-3]
        iso = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        ros_t = self.now().to_sec()
        mx, my, myaw = self.pose_x, self.pose_y, self.pose_yaw
        loc = getattr(self, "map_loc", None)
        pose = loc.pose() if loc is not None else None
        if pose is not None:
            mx, my, myaw = pose
        left_path = os.path.join(
            folder, "shot_%02d_left_%s.jpg" % (n, stamp)) if want_left else ""
        right_path = os.path.join(
            folder, "shot_%02d_right_%s.jpg" % (n, stamp)) if want_right else ""
        has_cam = bool(
            (want_left and self.wrist.left_host)
            or (want_right and self.wrist.right_host))
        self.capture_count = n
        sent = {"done": False}

        def after_send():
            with self._lock:
                if sent["done"]:
                    return
                sent["done"] = True
            if note:
                self._finish_shot_feedback(note, trace_l, trace_r)

        if note:
            with self._lock:
                self._shot_feedback_pending += 1
        try:
            ok = self.wrist.trigger_both(
                left_path, right_path, sides,
                trace_id_l=trace_l or None, trace_id_r=trace_r or None,
                on_sent=after_send if note else None)
        except Exception:
            if note:
                after_send()
            raise
        if not ok:
            if note:
                after_send()
            self.capture_count = n - 1
            return False
        self.last_left_name = os.path.basename(left_path) if left_path else ""
        self.last_right_name = os.path.basename(right_path) if right_path else ""
        self.last_shot_x = mx
        self.last_shot_y = my
        self.capture_count = n
        log_path = os.path.join(folder, "timestamps.csv")
        new_file = not os.path.isfile(log_path)
        with open(log_path, "a") as f:
            if new_file:
                f.write(
                    "index,iso,ros_sec,map_x,map_y,map_yaw_deg,"
                    "left,right,trace_id_L,trace_id_R\n")
            f.write("%d,%s,%.3f,%.4f,%.4f,%.2f,%s,%s,%s,%s\n" % (
                n, iso, ros_t, mx, my, math.degrees(myaw),
                os.path.basename(left_path) if left_path else "",
                os.path.basename(right_path) if right_path else "",
                trace_l, trace_r))
        if pose is not None:
            log.info(
                "[chassis_following] 已触发第%d组 sides=%s "
                "trace_id_L=%s trace_id_R=%s %s "
                "odom_info 机器人坐标 x=%.3f y=%.3f yaw=%.1fdeg%s",
                n, "+".join(sides), trace_l or "-", trace_r or "-", iso,
                mx, my, math.degrees(myaw),
                "" if has_cam else " 无相机")
        else:
            log.warn(
                "[chassis_following] 已触发第%d组 sides=%s "
                "trace_id_L=%s trace_id_R=%s %s "
                "还没有 odom_info，机器人坐标未知%s",
                n, "+".join(sides), trace_l or "-", trace_r or "-", iso,
                "" if has_cam else " 无相机")
        return True

    def _restore_after_charge_exit(self):
        """结束充电回到 home 之后，摆回节点启动时的任务初始姿态。"""
        if self.state != "IDLE" or not self._charge_exit:
            return
        self._charge_exit = False
        if not self.last_run_ok:
            log.warn("[chassis_following] 结束充电回家未到位，不恢复初始姿态")
            return
        log.info("[chassis_following] 已回 home，恢复任务初始姿态")
        self.state = "HOME_POSE"
        self.raise_arms()
        if self.state == "HOME_POSE":
            self.state = "IDLE"

    def raise_arms(self):
        if self.arm is None:
            log.warn("[chassis_following] raise_enable=false，跳过举手")
            return False
        try:
            return bool(self.arm.raise_arms())
        except rospy.ROSException as e:
            log.error("[chassis_following] 举手服务不可用（检查 naviai_robot 是否在跑）: %s", e)
            return False
        except Exception as e:
            log.error("[chassis_following] 举手失败: %s", e)
            return False

    def _note_nav_status(self):
        if self._task_t0 is None or not self.nav_sent:
            return
        if self.state not in ("NAV_SWEEP", "NAV_DISTANCE"):
            return
        nav = self.nav_client
        if nav is None:
            return
        status = nav.status_of_ours()
        if status is None:
            return
        names = {
            GoalStatus.PENDING: "PENDING",
            GoalStatus.ACTIVE: "ACTIVE",
            GoalStatus.PREEMPTED: "PREEMPTED",
            GoalStatus.SUCCEEDED: "SUCCEEDED",
            GoalStatus.ABORTED: "ABORTED",
            GoalStatus.REJECTED: "REJECTED",
            GoalStatus.PREEMPTING: "PREEMPTING",
            GoalStatus.RECALLING: "RECALLING",
            GoalStatus.RECALLED: "RECALLED",
            GoalStatus.LOST: "LOST",
        }
        self.mark_task(
            "导航状态首次回报 %s" % names.get(status, str(status)), once=True)
        if status == GoalStatus.ACTIVE:
            self.mark_task("导航已启动", once=True)

    def step(self):
        self._sync_stop()
        if self.state == "IDLE":
            state_idle.run(self)
        elif self.state == "NAV_SWEEP":
            self.mark_task("状态机开始处理扫拍", once=True)
            if not self.paused:
                state_sweep.run(self)
        elif self.state == "NAV_DISTANCE":
            self.mark_task("状态机开始处理扫拍", once=True)
            if not self.paused and self.distance is not None:
                self.distance.run(self)
        elif self.state == "CHARGE_POSE":
            pass
        elif self.state == "NAV_CHARGE":
            state_charge.run(self)
        elif self.state == "CHARGING":
            state_charging.run(self)
        elif self.state == "STOP_HOLD":
            state_fence.run(self)
        elif self.state == "HOME_POSE":
            pass
        elif self.state == "NAV_RETURN":
            state_nav.run(self)
            self._restore_after_charge_exit()
        self._note_nav_status()
        if not self.paused:
            self._poll_obstacle()
        return 0.0, 0.0, 0.0
