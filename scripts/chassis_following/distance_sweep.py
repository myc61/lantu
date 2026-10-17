# -*- coding: utf-8 -*-
"""欧式距离扫拍。导航仍去扫拍终点，拍照改成 robot_s 对齐检测点。

不走定时连拍。底盘位置由速度积分得到，速度先用 goal.speed，
/chassis_following/set_speed 收到 true 后把速度加载为 0。
sweep_from=rear：机器人从车尾走向车头，和底盘同向。
sweep_from=front：启动时机器人在车头，走向车尾，和底盘相向。
扫完调用现有 go_home。
"""
import math
import os
import threading

import rospy
from chassis_following.runlog import log
import yaml
from actionlib_msgs.msg import GoalStatus

try:
    import rospkg
except ImportError:
    rospkg = None


def parse_sweep_from(value):
    """rear=车尾开始同向，front=车头开始相向。"""
    text = str("rear" if value is None else value).strip().lower()
    if text == "rear":
        return "rear"
    if text == "front":
        return "front"
    raise ValueError("sweep_from 只能是 rear 或 front，收到 %r" % value)


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
        self.left_file = ""
        self.right_file = ""


class VehicleConfig(object):
    def __init__(self, vehicle_type, length, detection_points):
        self.vehicle_type = vehicle_type
        self.length = float(length)
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
        layout = data.get("capture_layout") or {}
        geometry = data.get("geometry") or {}
        if layout.get("length") not in (None, ""):
            length = float(layout.get("length"))
        else:
            length = (
                float(geometry.get("gripper_to_front", 0.0))
                - float(geometry.get("gripper_to_rear", 0.0)))
        if length <= 0:
            raise ValueError("车长必须大于 0")
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
            length=length,
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


class DetectionPointManager(object):
    def __init__(self, vehicle_config, from_front=False):
        self.vehicle_config = vehicle_config
        self.from_front = bool(from_front)
        ordered = list(vehicle_config.detection_points)
        ordered.sort(key=lambda point: point.s, reverse=self.from_front)
        self._points = ordered
        self.current_index = 0
        self.last_error = None
        self._skip_disabled()

    def _skip_disabled(self):
        points = self._points
        while (self.current_index < len(points)
               and not points[self.current_index].enabled):
            self.current_index += 1
            self.last_error = None

    def has_next(self):
        return self.current_index < len(self._points)

    def current_point(self):
        if not self.has_next():
            return None
        return self._points[self.current_index]

    def get_current_point_s(self, rear_s):
        """检测点在机器人行走方向上的位置。正方向是机器人往前走。"""
        point = self.current_point()
        if point is None:
            return None
        rear_s = float(rear_s)
        if self.from_front:
            return rear_s - point.s
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
    """按底盘与机器人的相对位置对齐检测点，触发现有 trigger_photo。

    速度默认用 goal.speed。停线服务收到 true 后从该时刻把速度加载为 0，
    已经走过的位移保留。车身停住，机器人继续走，仍按相对位置触发。
    """

    def __init__(self):
        self.models = VehicleConfigManager(vehicle_models_path())
        self.robot = RobotPositionManager()
        self.points = None
        self.vehicle = None
        self.task_id = ""
        self.speed = 0.0
        self.t0 = None
        self.from_front = False
        self.sweep_from = "rear"
        self.active = False
        self.loc_timeout = 1.0
        self.safety_margin = 0.2
        self.front_extra_m = 0.3
        self.whole_body = True
        self._pose_gen = 0
        self._rear_anchor = 0.0
        self._anchor_t = None
        self._speed_lock = threading.Lock()

    def refresh_params(self, cfg):
        """定位超时、车端余量和扫拍起点来自配置文件字典，不读参数服务器。"""
        cfg = cfg or {}
        self.loc_timeout = float(cfg.get("loc_timeout", 1.0))
        self.safety_margin = float(cfg.get("safety_margin", 0.2))
        self.front_extra_m = float(cfg.get("front_extra_m", 0.3))
        self.whole_body = _cfg_flag(cfg.get("sweep_whole_body", True), True)
        try:
            self.sweep_from = parse_sweep_from(cfg.get("sweep_from", "rear"))
        except ValueError as e:
            log.warn("[chassis_following] %s，仍用 %s", e, self.sweep_from)

    def _signed_speed(self):
        """沿机器人行走方向的底盘速度。相向时车身朝机器人迎面而来，取负。"""
        speed = float(self.speed)
        if self.from_front:
            return -speed
        return speed

    def _rear_unlocked(self, now=None):
        if self._anchor_t is None:
            return None
        now = now or rospy.Time.now()
        elapsed = (now - self._anchor_t).to_sec()
        if elapsed < 0.0:
            elapsed = 0.0
        return float(self._rear_anchor) + self._signed_speed() * elapsed

    def rear_s(self):
        """车尾在机器人行走轴上的位置。还没开扫时返回 None。"""
        with self._speed_lock:
            return self._rear_unlocked()

    def front_s(self, rear):
        """车头。同向时在车尾前方，相向时在车尾后方（机器人出发点）。"""
        rear = float(rear)
        length = float(self.vehicle.length)
        if self.from_front:
            return rear - length
        return rear + length

    def vehicle_end_name(self):
        """这一趟要走过去才结束的那一端。"""
        return "车尾" if self.from_front else "车头"

    def set_speed(self, speed):
        """替换本趟底盘速度。位置连续，扫拍不中断。未开扫时不记。"""
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            return False, "速度必须是数字"
        if not math.isfinite(speed) or speed < 0.0:
            return False, "速度必须是大于等于 0 的有限数，米/秒"
        with self._speed_lock:
            if not self.active or self._anchor_t is None:
                return False, "当前没有在按车速扫拍，速度未改"
            now = rospy.Time.now()
            rear = self._rear_unlocked(now)
            old = float(self.speed)
            self._rear_anchor = rear
            self._anchor_t = now
            self.speed = speed
        if speed == 0.0:
            log.info(
                "[chassis_following] 停线，底盘速度 %.3f -> 0，"
                "rear_s=%.3f 保持，按相对位置继续扫拍",
                old, rear)
            return True, "停线，底盘速度 0，按相对位置继续扫拍"
        log.info(
            "[chassis_following] 底盘速度 %.3f -> %.3f m/s，"
            "rear_s=%.3f 保持，按相对位置继续扫拍",
            old, speed, rear)
        return True, "底盘速度改为 %.3f m/s，按相对位置继续扫拍" % speed

    def finish(self):
        """本趟扫拍结束。之后 set_speed 不再改这一趟。"""
        with self._speed_lock:
            self.active = False

    def progress(self, m):
        """给前端的相对位置。along 是机器人离车尾、朝车头量的距离，米。"""
        pose = None
        loc = getattr(m, "map_loc", None)
        if loc is not None and loc.ready():
            pose = loc.pose()
        robot_s = None
        if pose is not None:
            robot_s = self.robot.get_robot_s(pose[0], pose[1])
        rear = self.rear_s()
        length = None
        if self.vehicle is not None:
            length = float(self.vehicle.length)
        along = None
        if robot_s is not None and rear is not None:
            if self.from_front:
                along = float(rear) - float(robot_s)
            else:
                along = float(robot_s) - float(rear)
        points = []
        ordered = self.points._points if self.points is not None else []
        for point in ordered:
            point_s = None
            if rear is not None:
                if self.from_front:
                    point_s = float(rear) - point.s
                else:
                    point_s = float(rear) + point.s
            points.append({
                "id": point.id,
                "s": point.s,
                "point_s": point_s,
                "state": point.state,
                "enabled": bool(point.enabled),
                "sides": list(point.capture.sides),
                "left": point.left_file or "",
                "right": point.right_file or "",
            })
        folder = getattr(m, "run_dir", None) or ""
        return {
            "active": bool(self.active),
            "state": getattr(m, "state", "") or "",
            "vehicle_type": self.vehicle.vehicle_type if self.vehicle else "",
            "task_id": self.task_id or "",
            "sweep_from": self.sweep_from or "rear",
            "length": length,
            "speed": None if self._anchor_t is None else float(self.speed),
            "robot_s": robot_s,
            "rear_s": rear,
            "from_rear_m": along,
            "from_front_m": None if along is None or length is None else length - along,
            "run": os.path.basename(folder) if folder else "",
            "points": points,
        }

    def begin(self, task_id, vehicle_type, start_x, start_y, speed, received_at):
        """加载车型并记下本趟起点、底盘速度和接到任务的时刻。失败时不改状态机。"""
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
            sweep_from = parse_sweep_from(self.sweep_from)
        except Exception as e:
            return False, "车型 %s 配置无效: %s" % (vehicle_type, e)
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            return False, "速度必须是数字"
        if not math.isfinite(speed) or speed < 0.0:
            return False, "速度必须是大于等于 0 的有限数，米/秒"
        received_at = received_at if received_at is not None else rospy.Time.now()
        from_front = sweep_from == "front"
        with self._speed_lock:
            self.vehicle = vehicle
            self.from_front = from_front
            self.sweep_from = sweep_from
            self.points = DetectionPointManager(vehicle, from_front=from_front)
            self.task_id = task_id or ""
            self.speed = speed
            self.t0 = received_at
            self._anchor_t = received_at
            self._rear_anchor = float(vehicle.length) if from_front else 0.0
            self.active = True
            self._pose_gen += 1
        self.robot.set_start_pose(start_x, start_y)
        where = "车头" if from_front else "车尾"
        toward = "车尾" if from_front else "车头"
        log.info(
            "[chassis_following] 车型任务 %s 车型 %s 起点 map=(%.3f, %.3f) "
            "从%s走向%s 底盘速度 %.3f m/s 检测点 %d 个",
            self.task_id or "-", vehicle_type, start_x, start_y,
            where, toward, self.speed, len(vehicle.detection_points))
        return True, "开始扫拍，从%s走向%s" % (where, toward)

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
        """机器人走过本趟终点那一端时为 True。车尾起点还没有时返回 None。

        rear：超过车头。front：超过车尾。两端都再加 safety_margin 和 front_extra_m。
        """
        if self.vehicle is None or not self.robot.initialized():
            return None
        robot_s = self.robot.get_robot_s(map_x, map_y)
        rear = self.rear_s()
        if robot_s is None or rear is None:
            return None
        if self.from_front:
            limit = float(rear)
        else:
            limit = self.front_s(rear)
        return robot_s > limit + self.safety_margin + self.front_extra_m

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
        rear = self.rear_s()
        if robot_s is None or rear is None:
            return

        self._check_capture(m, pose[0], pose[1], robot_s, rear)
        if m.state != "NAV_DISTANCE":
            return
        if self.points.has_next():
            return
        end_name = self.vehicle_end_name()
        if self.from_front:
            limit = float(rear)
        else:
            limit = self.front_s(rear)
        if robot_s > limit + self.safety_margin + self.front_extra_m:
            log.info(
                "[chassis_following] 检测点已完成且已超过%s "
                "robot_s=%.3f limit_s=%.3f，导航回起点",
                end_name, robot_s, limit)
            m.go_home(why="检测点完成且已超过%s" % end_name)

    def _check_capture(self, m, map_x, map_y, robot_s, rear_s):
        while True:
            point = self.points.current_point()
            if point is None or point.state != "WAITING":
                return
            point_s = self.points.get_current_point_s(rear_s)
            if point_s is None:
                return
            error = robot_s - point_s
            tolerance = float(point.capture.tolerance)
            prev_error = self.points.last_error
            # 相向时点会迎面过来。第一次看到已经落在身后，再等不会回来，跳过以免卡住。
            if self.from_front and prev_error is None and error > tolerance:
                log.warn(
                    "[chassis_following] 检测点 %s 已在身后 error=%.3f，跳过",
                    point.id, error)
                self.points.mark_done()
                continue
            break
        in_window = abs(error) <= tolerance
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
        point.left_file = getattr(m, "last_left_name", "") or ""
        point.right_file = getattr(m, "last_right_name", "") or ""
        self._write_meta(
            m, point.id, map_x, map_y, robot_s, rear_s, point_s, error,
            point.capture.sides)
        self._schedule_pose_after_shot(m, point)
        m._note_sweep_done(True, "检测点 %s 已拍" % point.id)
        log.info(
            "[chassis_following] 检测点 %s 已拍 sides=%s error=%.3f robot_s=%.3f point_s=%.3f",
            point.id, "+".join(point.capture.sides), error, robot_s, point_s)
        self.points.mark_done()

    def _write_meta(self, m, point_id, map_x, map_y,
                    robot_s, rear_s, point_s, error, sides):
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
                    "robot_map_x,robot_map_y,robot_s,rear_s,point_s,error,"
                    "sweep_from\n")
            f.write("%d,%s,%s,%s,%s,%.4f,%.4f,%.4f,%.4f,%.4f,%.4f,%s\n" % (
                m.capture_count,
                cell(self.task_id),
                cell(self.vehicle.vehicle_type),
                cell(point_id),
                cell("+".join(sides)),
                map_x, map_y, robot_s, rear_s, point_s, error,
                cell(self.sweep_from)))
