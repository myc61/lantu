#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫拍节点。收到任务后导航扫拍，扫完发 action feedback，回到 home 再给结果。

  一整趟：action /chassis_following/run_sweep
  模式见 yaml sweep_mode：0=定时到终点，1=欧式距离，2=定时、超过车头结束
"""
import math
import os
import sys
import threading
import time

# 节点在 scripts/ 下，sys.path[0] 会指向这里，源码包会盖住 devel 里的 srv。
_scripts_dir = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [
    p for p in sys.path
    if os.path.abspath(p or ".") != os.path.abspath(_scripts_dir)]

import actionlib
import rospy
from chassis_following.runlog import log
import yaml
from geometry_msgs.msg import Twist
from std_msgs.msg import String
from std_srvs.srv import Trigger, TriggerResponse
try:
    import rospkg
except ImportError:
    rospkg = None

from chassis_following.arm import ArmController
from chassis_following.capture import WristCameraCapture
from chassis_following.charge import AgvCharge
from chassis_following.distance_sweep import DistanceSweep
from chassis_following.home import HomeController
from chassis_following.localize import MapLocalization
from chassis_following.machine import FollowingMachine
from chassis_following.msg import (
    RunDistanceSweepAction, RunDistanceSweepFeedback, RunDistanceSweepResult)
from chassis_following.nav_client import NavHomeClient


def _cfg_get(cfg, key, default):
    if key not in cfg or cfg[key] is None:
        return default
    return cfg[key]


def _cfg_bool(cfg, key, default):
    value = _cfg_get(cfg, key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class ChassisFollowingNode(object):
    def __init__(self):
        self._yaml_mtime = None
        self._cfg = {}
        self._load_yaml()
        rate = float(_cfg_get(self._cfg, "rate", 10.0))
        self.map_loc = MapLocalization(_cfg_get(
            self._cfg, "map_odom_topic", "/zj_humanoid/navigation/odom_info"))
        self.nav_client = NavHomeClient(_cfg_get(
            self._cfg, "nav_action", "/zj_humanoid/navigation/navigation"))
        yaw_deg = float(_cfg_get(self._cfg, "map_home_yaw_deg", -11.0))
        yaw_tol_deg = float(_cfg_get(self._cfg, "map_arrive_tol_yaw_deg", 1.0))
        sweep_yaw_deg = float(_cfg_get(self._cfg, "sweep_end_yaw_deg", -10.6))
        sweep_end_x = float(_cfg_get(self._cfg, "sweep_end_x", 1.31))
        sweep_end_y = float(_cfg_get(self._cfg, "sweep_end_y", -0.23))
        sweep_photo_delay = float(_cfg_get(self._cfg, "sweep_photo_delay", 0.2))
        sweep_photo_period = float(_cfg_get(self._cfg, "sweep_photo_period", 0.2))
        map_home_x = float(_cfg_get(self._cfg, "map_home_x", -1.77))
        map_home_y = float(_cfg_get(self._cfg, "map_home_y", 0.27))
        plan = {"mode": "nav_sweep"}
        plan_text = (
            "导航扫拍  终点 map=(%.3f, %.3f, %.1fdeg)  "
            "起步后 %.2fs 拍第1组，之后每 %.2fs 一组，到终点停拍再回家；"
            "触发立刻拍照，传图在后台，回家途中继续存"
            % (sweep_end_x, sweep_end_y, sweep_yaw_deg,
               sweep_photo_delay, sweep_photo_period))
        log.warn(
            "[chassis_following] 导航扫拍。终点 map=(%.3f, %.3f, %.1fdeg)，"
            "起步后 %.2fs 拍第1组，间隔 %.2fs，到终点再导航回 "
            "map=(%.3f, %.3f, %.1fdeg)。速度由厂商导航配置，本包不发手柄。"
            "配置 %s",
            sweep_end_x, sweep_end_y, sweep_yaw_deg,
            sweep_photo_delay, sweep_photo_period,
            map_home_x, map_home_y, yaw_deg, self._yaml_path())

        arm = ArmController(self._cfg)

        self.machine = FollowingMachine(
            home_controller=HomeController(),
            wrist=WristCameraCapture(
                right_host=_cfg_get(self._cfg, "qvps_right_host", "192.168.217.51"),
                left_host=_cfg_get(self._cfg, "qvps_left_host", ""),
                port=_cfg_get(self._cfg, "qvps_port", 10008),
                left_port=_cfg_get(self._cfg, "qvps_left_port", 5000),
                program=_cfg_get(self._cfg, "qvps_program", "PN-001"),
                left_program=_cfg_get(self._cfg, "qvps_left_program", "1"),
                timeout=_cfg_get(self._cfg, "qvps_timeout", 15.0),
                http_port=_cfg_get(self._cfg, "qvps_http_port", 80),
                left_http_port=_cfg_get(self._cfg, "qvps_left_http_port", 8000),
                save_images=_cfg_bool(self._cfg, "save_images", True)),
            plan=plan,
            plan_text=plan_text,
            arm=arm,
            capture_dir=_cfg_get(
                self._cfg, "capture_dir", "/tmp/chassis_following"),
            nav_client=self.nav_client,
            map_loc=self.map_loc,
            map_home_x=map_home_x,
            map_home_y=map_home_y,
            map_home_yaw=math.radians(yaw_deg),
            map_arrive_tol_xy=float(
                _cfg_get(self._cfg, "map_arrive_tol_xy", 0.15)),
            map_arrive_tol_yaw=math.radians(yaw_tol_deg),
            nav_timeout=float(_cfg_get(self._cfg, "nav_timeout", 90.0)),
            sweep_end_x=sweep_end_x,
            sweep_end_y=sweep_end_y,
            sweep_end_yaw=math.radians(sweep_yaw_deg),
            sweep_photo_delay=sweep_photo_delay,
            sweep_photo_period=sweep_photo_period,
            sweep_photo_move_m=float(
                _cfg_get(self._cfg, "sweep_photo_move_m", 0.1)))
        self.machine.agv = AgvCharge(_cfg_get(
            self._cfg, "charge_service", "/zj_humanoid/chassis/agv_charge"))
        self._apply_yaml()
        self.distance = DistanceSweep()
        self.distance.refresh_params(self._cfg)
        self.machine.distance = self.distance

        self._as = actionlib.SimpleActionServer(
            "/chassis_following/run_sweep", RunDistanceSweepAction,
            execute_cb=self._execute_run_sweep, auto_start=False)
        self._as.start()
        self._srv_stop = rospy.Service(
            "/chassis_following/stop", Trigger, self._cb_stop)
        self._srv_reset = rospy.Service(
            "/chassis_following/reset", Trigger, self._cb_reset)
        self._srv_charge = rospy.Service(
            "/chassis_following/charge", Trigger, self._cb_charge)
        self._srv_end_charge = rospy.Service(
            "/chassis_following/end_charge", Trigger, self._cb_end_charge)
        cmd_topic = _cfg_get(
            self._cfg, "cmd_vel_calib_topic", "/zj_humanoid/cmd_vel/calib")
        self._sub_cmd = rospy.Subscriber(
            cmd_topic, Twist, self._cb_cmd_calib, queue_size=1)
        self._pub_safety = rospy.Publisher(
            "/chassis_following/safety_state", String, queue_size=1, latch=True)
        self._safety_sent = None
        log.info("[chassis_following] 自身避障速度 %s", cmd_topic)
        log.info(
            "[chassis_following] 外部接口 停=/chassis_following/stop "
            "复位=/chassis_following/reset "
            "充电=/chassis_following/charge "
            "结束充电=/chassis_following/end_charge")
        self._timer = rospy.Timer(rospy.Duration(1.0 / rate), self._loop)
        self._purge_at = 0.0
        self._purge_thread = None
        self._maybe_purge(force=True)
        log.info("[chassis_following] 已启动\n%s", plan_text)
        log.warn(
            "末端等待。一整趟: action /chassis_following/run_sweep  "
            "（模式见 yaml sweep_mode；扫完 feedback，到家 result）。")

    def _yaml_path(self):
        if rospkg is not None:
            return os.path.join(
                rospkg.RosPack().get_path("chassis_following"),
                "config", "chassis_following.yaml")
        return os.path.normpath(os.path.join(
            os.path.dirname(__file__), "..", "config", "chassis_following.yaml"))

    def _load_yaml(self):
        """从磁盘读配置。不写入参数服务器。"""
        path = self._yaml_path()
        try:
            with open(path, "r") as f:
                self._cfg = yaml.safe_load(f) or {}
            self._yaml_mtime = os.path.getmtime(path)
        except Exception as e:
            log.error("[chassis_following] 读 yaml 失败 %s: %s", path, e)
            return False
        self._apply_yaml()
        return True

    def _apply_yaml(self):
        """把已读入的配置写进状态机、欧式距离和举手。"""
        if hasattr(self, "machine"):
            self.machine.map_home_x = float(
                _cfg_get(self._cfg, "map_home_x", -1.77))
            self.machine.map_home_y = float(
                _cfg_get(self._cfg, "map_home_y", 0.27))
            self.machine.map_home_yaw = math.radians(float(
                _cfg_get(self._cfg, "map_home_yaw_deg", -11.0)))
            self.machine.map_arrive_tol_xy = float(
                _cfg_get(self._cfg, "map_arrive_tol_xy", 0.15))
            self.machine.map_arrive_tol_yaw = math.radians(float(
                _cfg_get(self._cfg, "map_arrive_tol_yaw_deg", 1.0)))
            self.machine.nav_timeout = float(
                _cfg_get(self._cfg, "nav_timeout", 90.0))
            self.machine.sweep_end_x = float(
                _cfg_get(self._cfg, "sweep_end_x", 1.31))
            self.machine.sweep_end_y = float(
                _cfg_get(self._cfg, "sweep_end_y", -0.23))
            self.machine.sweep_end_yaw = math.radians(float(
                _cfg_get(self._cfg, "sweep_end_yaw_deg", -10.6)))
            self.machine.sweep_photo_delay = float(
                _cfg_get(self._cfg, "sweep_photo_delay", 0.2))
            self.machine.sweep_photo_period = float(
                _cfg_get(self._cfg, "sweep_photo_period", 0.2))
            self.machine.sweep_photo_move_m = float(
                _cfg_get(self._cfg, "sweep_photo_move_m", 0.1))
            self.machine.charge_x = float(_cfg_get(self._cfg, "charge_x", 0.5))
            self.machine.charge_y = float(_cfg_get(self._cfg, "charge_y", 1.7))
            self.machine.charge_yaw = math.radians(float(
                _cfg_get(self._cfg, "charge_yaw_deg", 90.0)))
            self.machine.charge_tol_xy = float(
                _cfg_get(self._cfg, "charge_arrive_tol_xy", 0.1))
            self.machine.charge_tol_yaw = math.radians(float(
                _cfg_get(self._cfg, "charge_arrive_tol_yaw_deg", 5.73)))
            self.machine.obstacle_zero_hold_sec = float(
                _cfg_get(self._cfg, "obstacle_zero_hold_sec", 0.3))
            if self.machine.agv is not None:
                self.machine.agv._srv = _cfg_get(
                    self._cfg, "charge_service",
                    "/zj_humanoid/chassis/agv_charge")
            if self.machine.arm is not None:
                self.machine.arm.apply_config(self._cfg)
            wrist = getattr(self.machine, "wrist", None)
            if wrist is not None:
                wrist.right_host = str(_cfg_get(
                    self._cfg, "qvps_right_host", "192.168.217.51") or "").strip()
                wrist.left_host = str(_cfg_get(
                    self._cfg, "qvps_left_host", "") or "").strip()
                wrist.port = int(_cfg_get(self._cfg, "qvps_port", 10008))
                wrist.left_port = int(_cfg_get(self._cfg, "qvps_left_port", 5000))
                wrist.program = _cfg_get(self._cfg, "qvps_program", "PN-001")
                wrist.left_program = _cfg_get(
                    self._cfg, "qvps_left_program", "1") or "1"
                wrist.timeout = float(_cfg_get(self._cfg, "qvps_timeout", 15.0))
                wrist.http_port = int(_cfg_get(self._cfg, "qvps_http_port", 80))
                wrist.left_http_port = int(
                    _cfg_get(self._cfg, "qvps_left_http_port", 8000))
                wrist.save_images = _cfg_bool(self._cfg, "save_images", True)
        if getattr(self, "distance", None) is not None:
            self.distance.refresh_params(self._cfg)

    def _reload_yaml_if_changed(self):
        """IDLE 时发现 yaml 已保存，立刻换扫拍终点/拍照间隔，下一趟任务用。"""
        path = self._yaml_path()
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        if self._yaml_mtime is not None and mtime <= self._yaml_mtime:
            return
        self._load_yaml()
        if hasattr(self, "machine"):
            self.machine.set_plan({"mode": "nav_sweep"}, self.machine.nav_text())
            self.machine.reset_prompt()
        log.warn(
            "[chassis_following] yaml 已更新，下一趟扫拍终点/拍照间隔以当前文件为准（不用重启）")

    def _on_run_finished(self):
        log.info("[chassis_following] 本趟结束")

    def _sweep_result(self, ok, message):
        result = RunDistanceSweepResult()
        result.success = bool(ok)
        result.message = message or ""
        return result

    def _execute_run_sweep(self, goal):
        """开扫。扫完发 feedback，回到 home 再 set_succeeded / set_aborted。"""
        self._load_yaml()
        mode = int(_cfg_get(self._cfg, "sweep_mode", 0))
        if mode == 0:
            ok, msg = self.machine.start_sweep("run_sweep")
        elif mode == 1:
            ok, msg = self.machine.start_distance_sweep(
                goal.task_id or "", goal.vehicle_type or "")
        elif mode == 2:
            ok, msg = self.machine.start_front_sweep(
                goal.task_id or "", goal.vehicle_type or "")
        else:
            self._as.set_aborted(self._sweep_result(
                False,
                "yaml sweep_mode=%d 无效（0=定时到终点，1=欧式距离，2=定时超过车头结束）"
                % mode))
            return
        if not ok:
            self._as.set_aborted(self._sweep_result(False, msg))
            return
        log.info(
            "[chassis_following] run_sweep sweep_mode=%d 已开扫，扫完发 feedback，"
            "到家再给结果\n%s",
            mode, self.machine.plan_text)
        timeout = float(self.machine.nav_timeout) * 2.0 + 30.0
        deadline = rospy.Time.now() + rospy.Duration(timeout)
        rate = rospy.Rate(10)
        while not rospy.is_shutdown():
            if self._as.is_preempt_requested():
                self.machine.request_stop()
                done, _last_ok, _shots, _run_dir, message, _state = (
                    self.machine.snapshot_run())
                self._as.set_preempted(self._sweep_result(
                    False, message if done else "动作被取消"))
                return
            for note in self.machine.pop_sweep_notes():
                sweep_ok, sweep_msg, shots, run_dir = note
                fb = RunDistanceSweepFeedback()
                fb.sweep_ok = bool(sweep_ok)
                fb.message = sweep_msg
                fb.shot_count = int(shots)
                fb.run_dir = run_dir or ""
                self._as.publish_feedback(fb)
                log.info(
                    "[chassis_following] run_sweep feedback "
                    "sweep_ok=%s shots=%d dir=%s %s",
                    sweep_ok, shots, run_dir, sweep_msg)
            done, last_ok, _shots, _run_dir, message, state = (
                self.machine.snapshot_run())
            if done:
                result = self._sweep_result(last_ok, message)
                log.info(
                    "[chassis_following] run_sweep 到家结果 sweep_mode=%d success=%s %s",
                    mode, last_ok, message)
                if last_ok:
                    self._as.set_succeeded(result)
                else:
                    self._as.set_aborted(result)
                return
            if rospy.Time.now() > deadline:
                self._as.set_aborted(self._sweep_result(
                    False, "run_sweep 超时（state=%s）" % state))
                return
            rate.sleep()
        self._as.set_aborted(self._sweep_result(False, "节点关闭"))

    def _cb_reset(self, _req):
        ok, msg = self.machine.reset_external()
        return TriggerResponse(success=ok, message=msg)

    def _cb_charge(self, _req):
        ok, msg = self.machine.start_charge()
        return TriggerResponse(success=ok, message=msg)

    def _cb_end_charge(self, _req):
        ok, msg = self.machine.end_charge()
        return TriggerResponse(success=ok, message=msg)

    def _cb_cmd_calib(self, msg):
        self.machine.note_cmd_vel(msg)

    def _publish_safety(self):
        label = self.machine.safety_label()
        if label == self._safety_sent:
            return
        self._pub_safety.publish(String(data=label))
        self._safety_sent = label

    def _maybe_purge(self, force=False):
        """空闲时清掉超过 retain_days 的日志和照片，不占定时器。"""
        if not force and self.machine.state != "IDLE":
            return
        now = time.time()
        if not force and now - self._purge_at < 6 * 3600:
            return
        thread = self._purge_thread
        if thread is not None and thread.is_alive():
            return
        self._purge_at = now
        days = int(float(_cfg_get(self._cfg, "retain_days", 7)))
        log_dir = os.path.normpath(os.path.join(
            os.path.dirname(self._yaml_path()), "..", "logs"))
        capture_dir = self.machine.capture_dir
        keep = [self.machine.run_dir] if self.machine.run_dir else []

        def work():
            try:
                from chassis_following.retain import purge_expired
                stats = purge_expired(log_dir, capture_dir, days, keep)
            except Exception as e:
                log.warn("[retain] 清理失败: %s", e)
                return
            if stats["logs"] or stats["images"] or stats["dirs"]:
                log.info(
                    "[retain] 已删除 %d 天前的日志 %d 个、图片 %d 张、目录 %d 个",
                    days, stats["logs"], stats["images"], stats["dirs"])

        self._purge_thread = threading.Thread(target=work)
        self._purge_thread.daemon = True
        self._purge_thread.start()

    def _cb_stop(self, _req):
        """外部停止：调用一次就取消导航，进入 STOP_HOLD。"""
        ok, msg = self.machine.request_stop()
        log.warn("[chassis_following] 外部停止 success=%s %s", ok, msg)
        return TriggerResponse(success=ok, message=msg)

    def _loop(self, event):
        prev = self.machine.state
        if prev == "IDLE":
            self._reload_yaml_if_changed()
            self._maybe_purge()
        self.machine.step()
        self._publish_safety()
        if prev != "IDLE" and self.machine.state == "IDLE":
            self._on_run_finished()
        log.info_throttle(
            2.0,
            "[chassis_following] state=%s capture=%d "
            "map_ready=%s stop=%s",
            self.machine.state,
            self.machine.capture_count,
            self.map_loc.ready(),
            self.machine.stop_requested())

    def shutdown(self):
        if self.nav_client is not None:
            self.nav_client.cancel()
        log.info("[chassis_following] 节点关闭")


def main():
    rospy.init_node("chassis_following_node", anonymous=False)
    node = ChassisFollowingNode()
    rospy.on_shutdown(node.shutdown)
    rospy.spin()


if __name__ == "__main__":
    main()
