# -*- coding: utf-8 -*-
"""状态机：扫拍、回家，以及外部触发的停止、复位、充电。"""
import math
import os
import threading
from datetime import datetime

import rospy
from chassis_following.runlog import log

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
        self.obstacle_zero_hold_sec = 0.3
        self._cmd_moved = False
        self._cmd_zero = False
        self._cmd_zero_since = None
        self._reset_return = False
        self._obstacle_return = False
        self._reset_signal = False
        self.sweep_notes = []
        self.end_on_front = False

        self.state = "IDLE"
        self.pose_x = 0.0
        self.pose_y = 0.0
        self.pose_yaw = 0.0
        self.odom_received = False
        self.home_recorded = False
        self.capture_count = 0
        self.last_shot_x = 0.0
        self.last_shot_y = 0.0
        self.run_dir = None
        self.trip_done = False
        self.last_run_ok = False
        self.last_run_shots = 0
        self.last_run_dir = ""
        self.last_run_message = ""

        self._lock = threading.Lock()
        self.need_confirm = None
        self._asked = None

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
            if self.stop_requested() or self.state == "STOP_HOLD":
                return False, "外部停障中，不能开扫"
            if self.state != "IDLE":
                return False, "已经在跑（%s）" % self.state
            loc = getattr(self, "map_loc", None)
            if loc is None or not loc.ready():
                return False, "地图定位还没有，不能开导航扫拍"
            if self.nav_client is None:
                return False, "没有导航客户端"
            self.nav_client.cancel()
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
            self.sweep_notes = []
            self.end_on_front = False
            self.set_plan({"mode": "nav_sweep"}, self.nav_text())
            self.state = "NAV_SWEEP"
            log.info("[chassis_following] 开扫(%s)\n%s", why, self.plan_text)
            return True, "开始导航扫拍"

    def start_front_sweep(self, task_id, vehicle_type):
        """定时扫拍，结束条件和欧式距离一样：超过车头就停拍并回家。"""
        with self._lock:
            if self.stop_requested() or self.state == "STOP_HOLD":
                return False, "外部停障中，不能开扫"
            if self.state != "IDLE":
                return False, "已经在跑（%s）" % self.state
            loc = getattr(self, "map_loc", None)
            if loc is None or not loc.ready():
                return False, "地图定位还没有，不能开导航扫拍"
            if self.nav_client is None:
                return False, "没有导航客户端"
            ctrl = self.distance
            if ctrl is None:
                return False, "车型任务未加载"
            pose = loc.pose()
            if pose is None:
                return False, "地图定位还没有，不能开导航扫拍"
            ok, msg = ctrl.begin(task_id, vehicle_type, pose[0], pose[1])
            if not ok:
                return False, msg
            self.nav_client.cancel()
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
            self.sweep_notes = []
            self.end_on_front = True
            text = (
                "定时扫拍  车型=%s  任务=%s  终点 map=(%.3f, %.3f, %.1fdeg)  "
                "按间隔拍照，超过车头后回家"
                % (vehicle_type, task_id or "-",
                   self.sweep_end_x, self.sweep_end_y,
                   math.degrees(self.sweep_end_yaw)))
            self.set_plan({"mode": "nav_sweep"}, text)
            self.plan_text = text
            self.state = "NAV_SWEEP"
            log.info("[chassis_following] 开扫(超过车头结束)\n%s", text)
            return True, "开始导航扫拍，超过车头结束"

    def start_distance_sweep(self, task_id, vehicle_type):
        """欧式距离扫拍：导航去终点，按检测点窗口拍照，超过车头后再回家。"""
        with self._lock:
            if self.stop_requested() or self.state == "STOP_HOLD":
                return False, "外部停障中，不能开扫"
            if self.state != "IDLE":
                return False, "已经在跑（%s）" % self.state
            loc = getattr(self, "map_loc", None)
            if loc is None or not loc.ready():
                return False, "地图定位还没有，不能开导航扫拍"
            if self.nav_client is None:
                return False, "没有导航客户端"
            ctrl = self.distance
            if ctrl is None:
                return False, "欧式距离任务未加载"
            pose = loc.pose()
            if pose is None:
                return False, "地图定位还没有，不能开导航扫拍"
            ok, msg = ctrl.begin(task_id, vehicle_type, pose[0], pose[1])
            if not ok:
                return False, msg
            self.nav_client.cancel()
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
            self.sweep_notes = []
            self.end_on_front = False
            text = (
                "欧式距离扫拍  车型=%s  任务=%s  终点 map=(%.3f, %.3f, %.1fdeg)  "
                "按检测点窗口拍照，全部完成且超过车头后回家"
                % (vehicle_type, task_id or "-",
                   self.sweep_end_x, self.sweep_end_y,
                   math.degrees(self.sweep_end_yaw)))
            self.set_plan({"mode": "nav_distance"}, text)
            self.plan_text = text
            self.state = "NAV_DISTANCE"
            log.info("[chassis_following] 开欧式距离扫拍\n%s", text)
            ctrl.pose_at_start(self)
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

    def go_home(self, why="扫拍结束"):
        """导航回地图起点，等车头到位。扫拍阶段结束时先记下 feedback。"""
        if self.state in ("NAV_SWEEP", "NAV_DISTANCE"):
            ok = why in (
                "扫拍终点到位", "欧式距离导航到位", "检测点完成且已超过车头",
                "已超过车头")
            self._note_sweep_done(ok, why)
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

    def _note_sweep_done(self, ok, message):
        """记一条 feedback。欧式距离每拍一条，扫拍结束回家时再记一条。"""
        with self._lock:
            self.sweep_notes.append((
                bool(ok), message or "", int(self.capture_count),
                self.run_dir or ""))

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
        """外部触发：从待命去充电点，到位后打开充电。"""
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
            self.state = "NAV_CHARGE"
            log.info(
                "[chassis_following] 开始去充电 map=(%.3f, %.3f, %.1fdeg)",
                self.charge_x, self.charge_y, math.degrees(self.charge_yaw))
            return True, "开始去充电"

    def end_charge(self):
        """外部触发：关掉充电，再导航回 map_home。"""
        with self._lock:
            if self.stop_requested() or self.state == "STOP_HOLD":
                return False, "外部停障中，不能结束充电"
            if self.state not in ("NAV_CHARGE", "CHARGING") and not self.charging:
                return False, "当前没在充电（%s）" % self.state
            ok, msg = self.disable_charge()
            self.trip_done = False
            self.go_home(why="结束充电")
            if self.charging and not ok:
                log.warn(
                    "[chassis_following] 关闭充电失败，仍导航回 home: %s", msg)
                return False, "关闭充电失败，已开始导航回 home: %s" % (msg or "")
            return True, "已关充电，导航回家"

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
            if self.state not in ("NAV_SWEEP", "NAV_DISTANCE"):
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
        self._cmd_moved = False
        self._cmd_zero_since = None
        self.state = "STOP_HOLD"
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

    def trigger_photo(self, sides=None):
        """立刻触发一组。sides 缺省为左右都拍。"""
        if sides is None:
            sides = ("left", "right")
        else:
            sides = tuple(sides)
        want_left = "left" in sides
        want_right = "right" in sides
        n = self.capture_count + 1
        folder = self._ensure_run_dir()
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
        ok = self.wrist.trigger_both(left_path, right_path, sides)
        if not ok:
            return False
        self.last_shot_x = mx
        self.last_shot_y = my
        self.capture_count = n
        log_path = os.path.join(folder, "timestamps.csv")
        new_file = not os.path.isfile(log_path)
        with open(log_path, "a") as f:
            if new_file:
                f.write("index,iso,ros_sec,map_x,map_y,map_yaw_deg,left,right\n")
            f.write("%d,%s,%.3f,%.4f,%.4f,%.2f,%s,%s\n" % (
                n, iso, ros_t, mx, my, math.degrees(myaw),
                os.path.basename(left_path) if left_path else "",
                os.path.basename(right_path) if right_path else ""))
        log.info(
            "[chassis_following] 已触发第%d组 sides=%s %s map=(%.3f, %.3f, %.1fdeg)%s",
            n, "+".join(sides), iso, mx, my, math.degrees(myaw),
            "" if has_cam else " 无相机")
        return True

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

    def step(self):
        self._sync_stop()
        if self.state == "IDLE":
            state_idle.run(self)
        elif self.state == "NAV_SWEEP":
            state_sweep.run(self)
        elif self.state == "NAV_DISTANCE":
            if self.distance is not None:
                self.distance.run(self)
        elif self.state == "NAV_CHARGE":
            state_charge.run(self)
        elif self.state == "CHARGING":
            state_charging.run(self)
        elif self.state == "STOP_HOLD":
            state_fence.run(self)
        elif self.state == "NAV_RETURN":
            state_nav.run(self)
        self._poll_obstacle()
        return 0.0, 0.0, 0.0
