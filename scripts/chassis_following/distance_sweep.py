# -*- coding: utf-8 -*-
"""欧式距离扫拍。导航仍去扫拍终点，拍照改成 robot_s 对齐检测点。

不走定时连拍。全部点完成且机器人超过车辆前端后，调用现有 go_home。
"""
import math
import os
import threading

import rospy
from chassis_following.runlog import log
import yaml
from actionlib_msgs.msg import GoalStatus
from std_msgs.msg import Float64

try:
    import rospkg
except ImportError:
    rospkg = None


def _cfg_flag(value, default):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def parse_capture_sides(value):
    """both=左右都拍，left=只拍左，right=只拍右。"""
    text = str("both" if value is None else value).strip().lower()
    if text == "both":
        return ("left", "right")
    if text == "left":
        return ("left",)
    if text == "right":
        return ("right",)
    raise ValueError(
        "capture.sides 只能是 both、left、right，收到 %r" % value)


class CaptureConfig(object):
    def __init__(self, tolerance=0.15, sides=None):
        self.tolerance = float(tolerance)
        self.sides = parse_capture_sides(sides)


WHOLE_JOINTS = 22
POSE_AFTER_SHOT_SEC = 0.5


def parse_joints(value):
    """全身 22 关节。空表示这个点不发 movej。"""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        parts = [p for p in value.replace(",", " ").split() if p]
        nums = [float(p) for p in parts]
    else:
        nums = [float(x) for x in value]
    if not nums:
        return None
    if len(nums) != WHOLE_JOINTS:
        raise ValueError("全身关节必须是 %d 个，收到 %d 个" % (
            WHOLE_JOINTS, len(nums)))
    return nums


class DetectionPoint(object):
    def __init__(self, point_id, s, enabled=True, capture=None,
                 joints=None, pose_t=3.0):
        self.id = point_id
        self.s = float(s)
        self.enabled = bool(enabled)
        self.capture = capture if capture is not None else CaptureConfig()
        self.joints = joints
        self.pose_t = float(pose_t)
        self.state = "WAITING"


class VehicleGeometry(object):
    def __init__(self, gripper_to_rear=0.0, gripper_to_front=0.0):
        self.gripper_to_rear = float(gripper_to_rear)
        self.gripper_to_front = float(gripper_to_front)


class VehicleConfig(object):
    def __init__(self, vehicle_type, geometry, detection_points):
        self.vehicle_type = vehicle_type
        self.geometry = geometry
        self.detection_points = detection_points


def vehicle_models_path():
    if rospkg is not None:
        return os.path.join(
            rospkg.RosPack().get_path("chassis_following"),
            "config", "vehicle_models.yaml")
    return os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "..", "config", "vehicle_models.yaml"))


class VehicleConfigManager(object):
    def __init__(self, config_path):
        self.config_path = config_path
        self.data = {}

    def load(self):
        with open(self.config_path, "r") as f:
            self.data = yaml.safe_load(f) or {}

    def has_vehicle(self, vehicle_type):
        models = self.data.get("vehicle_models", {})
        return vehicle_type in models

    def get_vehicle_config(self, vehicle_type):
        data = self.data["vehicle_models"][vehicle_type]
        geometry_data = data.get("geometry", {})
        geometry = VehicleGeometry(
            gripper_to_rear=geometry_data.get("gripper_to_rear", 0.0),
            gripper_to_front=geometry_data.get("gripper_to_front", 0.0))
        points = []
        for item in data.get("detection_points", []):
            if "id" not in item:
                raise ValueError("检测点缺少 id")
            capture_data = item.get("capture", {})
            position_data = item.get("position", {})
            pose_data = item.get("pose") or {}
            points.append(DetectionPoint(
                point_id=str(item["id"]),
                s=position_data.get("s", 0.0),
                enabled=item.get("enabled", True),
                capture=CaptureConfig(
                    tolerance=capture_data.get("tolerance", 0.15),
                    sides=capture_data.get("sides", "both")),
                joints=parse_joints(pose_data.get("joints")),
                pose_t=pose_data.get("t", 3.0)))
        return VehicleConfig(
            vehicle_type=vehicle_type,
            geometry=geometry,
            detection_points=points)


class RobotPositionManager(object):
    def __init__(self):
        self.start_x = None
        self.start_y = None

    def set_start_pose(self, x, y):
        self.start_x = float(x)
        self.start_y = float(y)

    def initialized(self):
        return self.start_x is not None and self.start_y is not None

    def get_robot_s(self, current_x, current_y):
        if not self.initialized():
            return None
        return math.hypot(
            float(current_x) - self.start_x,
            float(current_y) - self.start_y)


class VehicleTracker(object):
    """上游夹具 1D 位置。超时只表示当前值不可用。"""

    def __init__(self, timeout=1.0):
        self.timeout = float(timeout)
        self._raw = None
        self._last = None
        self._lock = threading.Lock()

    def update(self, gripper_raw):
        with self._lock:
            self._raw = float(gripper_raw)
            self._last = rospy.Time.now()

    def raw_if_valid(self):
        with self._lock:
            if self._raw is None or self._last is None:
                return None
            age = (rospy.Time.now() - self._last).to_sec()
            if age > self.timeout:
                return None
            return self._raw


class DetectionPointManager(object):
    def __init__(self, vehicle_config):
        self.vehicle_config = vehicle_config
        self.current_index = 0
        self.last_error = None
        self._skip_disabled()

    def _skip_disabled(self):
        points = self.vehicle_config.detection_points
        while (self.current_index < len(points)
               and not points[self.current_index].enabled):
            self.current_index += 1
            self.last_error = None

    def has_next(self):
        return self.current_index < len(self.vehicle_config.detection_points)

    def current_point(self):
        if not self.has_next():
            return None
        return self.vehicle_config.detection_points[self.current_index]

    def get_current_point_s(self, gripper_s):
        point = self.current_point()
        if point is None:
            return None
        rear_s = (
            float(gripper_s)
            + self.vehicle_config.geometry.gripper_to_rear)
        return rear_s + point.s

    def mark_done(self):
        point = self.current_point()
        if point is None:
            return
        point.state = "DONE"
        self.current_index += 1
        self.last_error = None
        self._skip_disabled()


class DistanceSweep(object):
    """订夹具话题，在 NAV_DISTANCE 里按窗口触发现有 trigger_photo。"""

    def __init__(self):
        self.models = VehicleConfigManager(vehicle_models_path())
        self.robot = RobotPositionManager()
        self.tracker = VehicleTracker()
        self.points = None
        self.vehicle = None
        self.task_id = ""
        self.scale = 1.0
        self.offset = 0.0
        self.loc_timeout = 1.0
        self.safety_margin = 0.2
        self.front_extra_m = 0.3
        self.whole_body = True
        self._topic = None
        self._sub = None
        self._pose_gen = 0

    def refresh_params(self, cfg):
        """夹具换算和超时来自配置文件字典，不读参数服务器。"""
        cfg = cfg or {}
        self.scale = float(cfg.get("gripper_scale", 1.0))
        self.offset = float(cfg.get("gripper_offset", 0.0))
        self.tracker.timeout = float(cfg.get("gripper_timeout", 1.0))
        self.loc_timeout = float(cfg.get("loc_timeout", 1.0))
        self.safety_margin = float(cfg.get("safety_margin", 0.2))
        self.front_extra_m = float(cfg.get("front_extra_m", 0.3))
        self.whole_body = _cfg_flag(cfg.get("sweep_whole_body", True), True)
        topic = cfg.get("gripper_topic", "/chassis_following/gripper_s")
        if topic != self._topic:
            if self._sub is not None:
                self._sub.unregister()
            self._topic = topic
            self._sub = rospy.Subscriber(
                topic, Float64, self._on_gripper, queue_size=10)
            log.info("[chassis_following] 夹具位置话题 %s", topic)

    def _on_gripper(self, msg):
        self.tracker.update(msg.data)

    def gripper_s(self):
        raw = self.tracker.raw_if_valid()
        if raw is None:
            return None
        return self.scale * raw + self.offset

    def begin(self, task_id, vehicle_type, start_x, start_y):
        """加载车型并记下本趟起点。失败时不改状态机。"""
        vehicle_type = (vehicle_type or "").strip()
        if not vehicle_type:
            return False, "未提供车型"
        path = self.models.config_path
        try:
            self.models.load()
        except Exception as e:
            return False, "读车型配置失败 %s: %s" % (path, e)
        if not self.models.has_vehicle(vehicle_type):
            return False, "未知车型 %s" % vehicle_type
        try:
            vehicle = self.models.get_vehicle_config(vehicle_type)
        except Exception as e:
            return False, "车型 %s 配置无效: %s" % (vehicle_type, e)
        self.vehicle = vehicle
        self.points = DetectionPointManager(vehicle)
        self.task_id = task_id or ""
        self._pose_gen += 1
        self.robot.set_start_pose(start_x, start_y)
        log.info(
            "[chassis_following] 车型任务 %s 车型 %s 起点 map=(%.3f, %.3f) "
            "检测点 %d 个",
            self.task_id or "-", vehicle_type, start_x, start_y,
            len(vehicle.detection_points))
        return True, "开始欧式距离扫拍"

    def pose_at_start(self, m):
        """模式 1 开扫后立刻异步运动到第一个检测点的全身姿态。"""
        if not self.whole_body:
            return
        point = self.points.current_point() if self.points is not None else None
        if point is None or not point.joints:
            return
        self._schedule_pose(m, point, 0.0, "开扫")

    def _schedule_pose_after_shot(self, m, shot):
        """拍照下发后 0.5s，运动到下一个点；没有下一点就回到举手姿态。"""
        if not self.whole_body:
            return
        nxt = self.points.current_point() if self.points is not None else None
        if nxt is not None:
            if nxt.joints:
                self._schedule_pose(m, nxt, POSE_AFTER_SHOT_SEC, "下一点")
            return
        self._schedule_pose(m, None, POSE_AFTER_SHOT_SEC, "回举手", fallback_t=(
            shot.pose_t if shot is not None else 3.0))

    def _schedule_pose(self, m, point, delay, why, fallback_t=3.0):
        gen = self._pose_gen

        def work():
            if delay:
                rospy.sleep(delay)
            if gen != self._pose_gen:
                return
            if point is not None and point.joints:
                self._send_pose(m, point.joints, point.pose_t, "%s %s" % (why, point.id))
                return
            if point is None:
                arm = getattr(m, "arm", None)
                if arm is None:
                    return
                joints = arm.raise_joints
                if not joints:
                    from chassis_following.arm import JOINTS
                    joints = JOINTS
                self._send_pose(m, joints, fallback_t, why)

        t = threading.Thread(target=work)
        t.daemon = True
        t.start()

    def _send_pose(self, m, joints, t, why):
        arm = getattr(m, "arm", None)
        if arm is None or not joints:
            return
        try:
            arm.move_whole_body(joints, t, is_async=True, arm_type=15, label=why)
        except Exception as e:
            log.warn("[chassis_following] 全身姿态 %s 失败: %s", why, e)

    def past_vehicle_front(self, map_x, map_y):
        """robot_s 超过车头加 safety_margin 和 front_extra_m 时为 True。夹具或起点还没有时返回 None。"""
        if self.vehicle is None or not self.robot.initialized():
            return None
        robot_s = self.robot.get_robot_s(map_x, map_y)
        gripper_s = self.gripper_s()
        if robot_s is None or gripper_s is None:
            return None
        front_s = gripper_s + self.vehicle.geometry.gripper_to_front
        return robot_s > front_s + self.safety_margin + self.front_extra_m

    def run(self, m):
        if not self._advance_nav(m):
            return
        self._maybe_capture(m)

    def _advance_nav(self, m):
        """发扫拍终点、判到位。返回 False 表示本拍不再触发（含已经回家）。"""
        nav = getattr(m, "nav_client", None)
        loc = getattr(m, "map_loc", None)
        if nav is None:
            log.error_throttle(5.0, "[chassis_following] 没有导航客户端，无法扫拍")
            m.go_home(why="欧式距离扫拍无导航")
            return False

        t = (m.now() - m.nav_t0).to_sec()
        timeout = float(getattr(m, "nav_timeout", 90.0))
        hx = float(m.sweep_end_x)
        hy = float(m.sweep_end_y)
        hyaw = float(m.sweep_end_yaw)
        tol_xy = float(m.map_arrive_tol_xy)
        tol_yaw = float(m.map_arrive_tol_yaw)

        if not m.nav_sent:
            nav.send_pose(hx, hy, hyaw, tol_xy, tol_yaw, label="distance")
            m.nav_sent = True
            return False

        st = nav.status_of_ours()
        if st is None and t >= 2.0 and t < 2.2:
            log.warn("[chassis_following] 欧式距离导航未收到 goal，重发一次")
            nav.send_pose(hx, hy, hyaw, tol_xy, tol_yaw, label="distance")

        pose = loc.pose() if loc is not None else None
        if pose is not None:
            log.info_throttle(
                2.0,
                "[chassis_following] 欧式距离扫拍中 map=(%.3f, %.3f, %.1fdeg) "
                "终点=(%.3f, %.3f, %.1fdeg) status=%s shots=%d",
                pose[0], pose[1], math.degrees(pose[2]),
                hx, hy, math.degrees(hyaw), st, m.capture_count)

        arrived = bool(loc is not None and loc.ready() and
                       loc.at_pose(hx, hy, hyaw, tol_xy, tol_yaw))
        moving = st in (GoalStatus.ACTIVE, GoalStatus.SUCCEEDED)
        done = nav.succeeded() or (t > 5.0 and arrived and moving)
        if st in (None, GoalStatus.PENDING, GoalStatus.ACTIVE):
            failed = t >= timeout
        else:
            failed = nav.failed() or t >= timeout
        if done:
            log.info(
                "[chassis_following] 已到扫拍终点，欧式距离共 %d 组，导航回起点",
                m.capture_count)
            m.go_home(why="欧式距离导航到位")
            return False
        if failed:
            log.warn(
                "[chassis_following] 欧式距离导航未到位（t=%.1fs status=%s shots=%d），回家",
                t, st, m.capture_count)
            m.go_home(why="欧式距离导航失败")
            return False
        return True

    def _maybe_capture(self, m):
        loc = getattr(m, "map_loc", None)
        if loc is None or not loc.ready():
            return
        age = loc.age_sec()
        if age is None or age > self.loc_timeout:
            log.warn_throttle(
                2.0, "[chassis_following] 地图定位超时，暂停欧式距离触发")
            return
        pose = loc.pose()
        if pose is None or self.points is None or self.vehicle is None:
            return
        robot_s = self.robot.get_robot_s(pose[0], pose[1])
        gripper_s = self.gripper_s()
        if robot_s is None:
            return
        if gripper_s is None:
            log.warn_throttle(
                2.0, "[chassis_following] 夹具位置超时，暂停欧式距离触发")
            return

        self._check_capture(m, pose[0], pose[1], robot_s, gripper_s)
        if m.state != "NAV_DISTANCE":
            return
        if self.points.has_next():
            return
        front_s = gripper_s + self.vehicle.geometry.gripper_to_front
        if robot_s > front_s + self.safety_margin + self.front_extra_m:
            log.info(
                "[chassis_following] 检测点已完成且已超过车头 "
                "robot_s=%.3f front_s=%.3f，导航回起点",
                robot_s, front_s)
            m.go_home(why="检测点完成且已超过车头")

    def _check_capture(self, m, map_x, map_y, robot_s, gripper_s):
        point = self.points.current_point()
        if point is None or point.state != "WAITING":
            return
        point_s = self.points.get_current_point_s(gripper_s)
        if point_s is None:
            return
        error = robot_s - point_s
        tolerance = float(point.capture.tolerance)
        in_window = abs(error) <= tolerance
        prev_error = self.points.last_error
        crossed = False
        if prev_error is not None:
            crossed = prev_error < 0.0 and error >= 0.0
        self.points.last_error = error
        if not (in_window or crossed):
            return
        point.state = "TRIGGERED"
        if not m.trigger_photo(point.capture.sides):
            point.state = "WAITING"
            self.points.last_error = prev_error
            log.warn(
                "[chassis_following] 检测点 %s 触发失败，下一拍重试", point.id)
            return
        self._write_meta(
            m, point.id, map_x, map_y, robot_s, gripper_s, point_s, error,
            point.capture.sides)
        self._schedule_pose_after_shot(m, point)
        m._note_sweep_done(True, "检测点 %s 已拍" % point.id)
        log.info(
            "[chassis_following] 检测点 %s 已拍 sides=%s error=%.3f robot_s=%.3f point_s=%.3f",
            point.id, "+".join(point.capture.sides), error, robot_s, point_s)
        self.points.mark_done()

    def _write_meta(self, m, point_id, map_x, map_y,
                    robot_s, gripper_s, point_s, error, sides):
        folder = m.run_dir
        if not folder:
            return
        path = os.path.join(folder, "distance_meta.csv")
        new_file = not os.path.isfile(path)

        def cell(text):
            return (text or "").replace(",", "_").replace("\n", " ")

        with open(path, "a") as f:
            if new_file:
                f.write(
                    "index,task_id,vehicle_type,point_id,sides,"
                    "robot_map_x,robot_map_y,robot_s,gripper_s,point_s,error\n")
            f.write("%d,%s,%s,%s,%s,%.4f,%.4f,%.4f,%.4f,%.4f,%.4f\n" % (
                m.capture_count,
                cell(self.task_id),
                cell(self.vehicle.vehicle_type),
                cell(point_id),
                cell("+".join(sides)),
                map_x, map_y, robot_s, gripper_s, point_s, error))
