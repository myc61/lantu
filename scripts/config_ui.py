#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫拍配置网页。改 yaml、启动节点、开一趟检测、看照片。

在 naviai_rosbridge 里运行。容器是主机网络，浏览器打开 http://127.0.0.1:8090。

  docker exec -d naviai_rosbridge bash -lc \\
    'python3 /shared/catkin_ws/src/chassis_following/scripts/config_ui.py'
"""
import json
import math
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import yaml

PKG = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CFG_PATH = os.path.join(PKG, "config", "chassis_following.yaml")
VEH_PATH = os.path.join(PKG, "config", "vehicle_models.yaml")
WEB_INDEX = os.path.join(PKG, "web", "index.html")
SHOTS_DEFAULT = os.path.join(PKG, "shots")
SWEEP_LOG = os.path.join(PKG, "logs", "ui_sweep.log")
LAUNCH_LOG = os.path.join(PKG, "logs", "ui_launch.log")
PORT = int(os.environ.get("CHASSIS_UI_PORT", "8090"))
_write_lock = threading.Lock()

PARAM_KEYS = [
    "sweep_mode",
    "map_home_x", "map_home_y", "map_home_yaw_deg",
    "sweep_end_x", "sweep_end_y", "sweep_end_yaw_deg",
    "map_arrive_tol_xy", "map_arrive_tol_yaw_deg", "nav_timeout",
    "sweep_photo_move_m", "sweep_photo_delay", "sweep_photo_period",
    "qvps_right_host", "qvps_left_host", "qvps_port", "qvps_left_port",
    "qvps_program", "qvps_left_program", "qvps_http_port", "qvps_left_http_port",
    "qvps_timeout",
    "gripper_scale", "gripper_offset", "gripper_timeout",
    "loc_timeout", "safety_margin", "front_extra_m",
    "charge_x", "charge_y", "charge_yaw_deg",
    "charge_arrive_tol_xy", "charge_arrive_tol_yaw_deg",
    "capture_dir", "save_images", "sweep_whole_body",
]
INT_KEYS = {
    "sweep_mode", "qvps_port", "qvps_left_port",
    "qvps_http_port", "qvps_left_http_port",
}
BOOL_KEYS = {"save_images", "sweep_whole_body"}
STR_KEYS = {
    "qvps_right_host", "qvps_left_host", "qvps_program",
    "qvps_left_program", "capture_dir",
}
SIDES = {"both", "left", "right"}


def _ros_bash(script):
    """在当前环境直接调 ROS。本进程应跑在 naviai_rosbridge 里。"""
    inner = (
        "source /opt/ros/noetic/setup.bash && "
        "if [ -f /navi_ws/devel/setup.bash ]; then source /navi_ws/devel/setup.bash; fi && "
        "source /shared/catkin_ws/devel/setup.bash && "
        + script
    )
    return ["bash", "-lc", inner]


def _run(cmd, timeout=20):
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, text=True)
    except subprocess.TimeoutExpired:
        return 124, "超时"
    except OSError as e:
        return 127, str(e)
    return proc.returncode, proc.stdout or ""


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _yaml_get(text, key):
    match = re.search(
        r"(?m)^[ \t]*" + re.escape(key) + r":[ \t]*([^#\n]*)", text)
    if not match:
        return None
    raw = match.group(1).strip().strip('"').strip("'")
    if key in STR_KEYS:
        return raw
    if key in BOOL_KEYS:
        return raw.lower() in ("1", "true", "yes", "on")
    if key in INT_KEYS:
        return int(float(raw))
    return float(raw)


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _format_value(key, value):
    if key in STR_KEYS:
        return json.dumps(str(value), ensure_ascii=False)
    if key in BOOL_KEYS:
        return "true" if _as_bool(value) else "false"
    if key in INT_KEYS:
        return str(int(value))
    number = float(value)
    text = ("%.6f" % number).rstrip("0").rstrip(".")
    return text if text not in ("", "-") else "0"


def _split_yaml_scalar(rest):
    """拆开「值 + 行尾注释」。引号里的 # 不算注释，注释前的空格原样留下。"""
    in_quote = None
    for i, ch in enumerate(rest):
        if in_quote:
            if ch == in_quote:
                in_quote = None
            continue
        if ch in ("'", '"'):
            in_quote = ch
            continue
        if ch != "#":
            continue
        if i > 0 and rest[i - 1] not in " \t":
            return rest[:i].rstrip(), "  " + rest[i:]
        j = i
        while j > 0 and rest[j - 1] in " \t":
            j -= 1
        spaces = rest[j:i] or "  "
        return rest[:j].rstrip(), spaces + rest[i:]
    return rest.rstrip(), ""


def _replace_key(text, key, value):
    pattern = re.compile(
        r"(?m)^([ \t]*)" + re.escape(key) + r":[ \t]*(.*)$")
    formatted = _format_value(key, value)

    def repl(match):
        _old, comment = _split_yaml_scalar(match.group(2))
        return "%s%s: %s%s" % (match.group(1), key, formatted, comment)

    new, count = pattern.subn(repl, text, count=1)
    if count != 1:
        raise KeyError(key)
    return new


def _load_params():
    text = _read(CFG_PATH)
    return {key: _yaml_get(text, key) for key in PARAM_KEYS}


def _save_params(params):
    unknown = [key for key in params if key not in PARAM_KEYS]
    if unknown:
        raise ValueError("不能改这些字段: %s" % ", ".join(unknown))
    if "sweep_mode" in params and int(params["sweep_mode"]) not in (0, 1, 2):
        raise ValueError("sweep_mode 只能是 0、1、2")
    text = _read(CFG_PATH)
    for key, value in params.items():
        if value is None or value == "":
            raise ValueError("%s 不能为空" % key)
        text = _replace_key(text, key, value)
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, CFG_PATH)


def _load_vehicles():
    data = yaml.safe_load(_read(VEH_PATH)) or {}
    models = data.get("vehicle_models") or {}
    out = {}
    for name, model in models.items():
        geometry = (model or {}).get("geometry") or {}
        points = []
        for item in (model or {}).get("detection_points") or []:
            capture = item.get("capture") or {}
            pose = item.get("pose") or {}
            joints = pose.get("joints") or []
            points.append({
                "id": str(item.get("id") or ""),
                "s": float((item.get("position") or {}).get("s", 0.0)),
                "enabled": bool(item.get("enabled", True)),
                "tolerance": float(capture.get("tolerance", 0.15)),
                "sides": str(capture.get("sides") or "both"),
                "pose_t": float(pose.get("t", 3.0)),
                "joints": [float(x) for x in joints],
            })
        out[str(name)] = {
            "origin": str(((model or {}).get("reference") or {}).get("origin") or "rear"),
            "gripper_to_rear": float(geometry.get("gripper_to_rear", 0.0)),
            "gripper_to_front": float(geometry.get("gripper_to_front", 0.0)),
            "points": points,
        }
    return out


def _parse_joints(value):
    if value is None or value == "":
        return []
    if isinstance(value, str):
        parts = [p for p in value.replace(",", " ").split() if p]
        nums = [float(p) for p in parts]
    else:
        nums = [float(x) for x in value]
    if nums and len(nums) != 22:
        raise ValueError("全身关节必须是 22 个，收到 %d 个" % len(nums))
    return nums


def _check_vehicles(models):
    if not isinstance(models, dict) or not models:
        raise ValueError("至少保留一个车型")
    for name, model in models.items():
        if not re.match(r"^[A-Za-z0-9_]+$", str(name)):
            raise ValueError("车型名只能是字母、数字、下划线: %s" % name)
        try:
            float(model["gripper_to_rear"])
            float(model["gripper_to_front"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("%s 的夹具距离无效" % name)
        seen = set()
        for point in model.get("points") or []:
            pid = str(point.get("id") or "").strip()
            if not re.match(r"^[A-Za-z0-9_]+$", pid):
                raise ValueError("%s 的检测点 id 无效" % name)
            if pid in seen:
                raise ValueError("%s 的检测点 %s 重复" % (name, pid))
            seen.add(pid)
            float(point["s"])
            float(point["tolerance"])
            if str(point.get("sides") or "both") not in SIDES:
                raise ValueError("%s %s 的 sides 只能是 both、left、right" % (name, pid))
            joints = _parse_joints(point.get("joints"))
            point["joints"] = joints
            try:
                pose_t = float(point.get("pose_t", 3.0))
            except (TypeError, ValueError):
                raise ValueError("%s %s 的姿态时间 t 无效" % (name, pid))
            if joints and pose_t <= 0:
                raise ValueError("%s %s 的姿态时间 t 要大于 0" % (name, pid))
            point["pose_t"] = pose_t


def _dump_vehicles(models):
    lines = [
        "# 车型几何和检测点。纵向单位都是米。\n",
        "# 正负号由 gripper_to_rear / gripper_to_front 决定，代码不写死方向。\n",
        "# 模式 1 用检测点；模式 2 只用 geometry 判断车头。\n",
        "# capture.sides：both=左右都拍，left=只拍左腕，right=只拍右腕。不写则 both。\n",
        "# pose.joints：模式 1 全身 22 关节。不写则该点不发 movej。t 是运动时间，秒。is_async 固定 true。\n",
        "vehicle_models:\n",
    ]
    for name, model in models.items():
        lines.append("\n  %s:\n" % name)
        lines.append("    reference:\n")
        lines.append("      origin: %s\n" % (model.get("origin") or "rear"))
        lines.append("    geometry:\n")
        lines.append("      gripper_to_rear: %s\n" % _format_value(
            "gripper_to_rear", model["gripper_to_rear"]))
        lines.append("      gripper_to_front: %s\n" % _format_value(
            "gripper_to_front", model["gripper_to_front"]))
        lines.append("    detection_points:\n")
        points = model.get("points") or []
        if not points:
            lines.append("      []\n")
            continue
        for point in points:
            enabled = "true" if point.get("enabled", True) else "false"
            lines.append("      - id: %s\n" % point["id"])
            lines.append("        position:\n")
            lines.append("          s: %s\n" % _format_value("s", point["s"]))
            lines.append("        enabled: %s\n" % enabled)
            lines.append("        capture:\n")
            lines.append("          tolerance: %s\n" % _format_value(
                "tolerance", point["tolerance"]))
            lines.append("          sides: %s\n" % point.get("sides", "both"))
            joints = point.get("joints") or []
            if joints:
                lines.append("        pose:\n")
                lines.append("          t: %s\n" % _format_value(
                    "t", point.get("pose_t", 3.0)))
                lines.append("          joints: [%s]\n" % ", ".join(
                    _format_value("j", x) for x in joints))
    return "".join(lines)


def _save_vehicles(models):
    _check_vehicles(models)
    text = _dump_vehicles(models)
    tmp = VEH_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, VEH_PATH)


def _push_save_images(params):
    """手动拍照节点只在调用时读参数服务器，这里把开关同步过去。"""
    if "save_images" not in params:
        return
    flag = "true" if _as_bool(params["save_images"]) else "false"
    _run(_ros_bash(
        "rosparam set /qvps_camera_node/save_images %s" % flag
    ), timeout=8)


def _save_one_vehicle(name, model):
    name = str(name or "")
    _check_vehicles({name: model})
    current = _load_vehicles()
    current[name] = model
    _save_vehicles(current)
    return name


def _delete_vehicle(name):
    name = str(name or "")
    current = _load_vehicles()
    if name not in current:
        return
    if len(current) <= 1:
        raise ValueError("至少保留一个车型")
    del current[name]
    _save_vehicles(current)


def _shots_root():
    try:
        path = _load_params().get("capture_dir") or SHOTS_DEFAULT
    except Exception:
        path = SHOTS_DEFAULT
    path = os.path.abspath(path)
    if not path.startswith(os.path.abspath(PKG) + os.sep) and path != os.path.abspath(PKG):
        if not os.path.isdir(path):
            return SHOTS_DEFAULT
    return path


def _list_shots():
    root = _shots_root()
    runs = []
    if not os.path.isdir(root):
        return root, runs
    names = []
    for name in os.listdir(root):
        folder = os.path.join(root, name)
        if os.path.isdir(folder):
            names.append(name)
    names.sort(reverse=True)
    for name in names[:30]:
        folder = os.path.join(root, name)
        files = []
        for fname in sorted(os.listdir(folder)):
            if fname.lower().endswith((".jpg", ".jpeg", ".png")):
                files.append(fname)
        runs.append({"name": name, "files": files})
    return root, runs


def _safe_shot(run, fname):
    root = os.path.abspath(_shots_root())
    if not re.match(r"^[A-Za-z0-9._-]+$", run or ""):
        return None
    if not re.match(r"^[A-Za-z0-9._-]+$", fname or ""):
        return None
    if not fname.lower().endswith((".jpg", ".jpeg", ".png")):
        return None
    path = os.path.abspath(os.path.join(root, run, fname))
    if not path.startswith(root + os.sep):
        return None
    if not os.path.isfile(path):
        return None
    return path


def _node_up():
    """名字还在主机上不算在跑。容器重启后登记会留下，端口已经拒绝连接。"""
    script = (
        "python3 - <<'PY'\n"
        "import socket\n"
        "from urllib.parse import urlparse\n"
        "import rosgraph\n"
        "name = '/chassis_following_node'\n"
        "try:\n"
        "    uri = rosgraph.Master('/chassis_ui_check').lookupNode(name)\n"
        "except Exception as e:\n"
        "    print('DOWN', e)\n"
        "    raise SystemExit(0)\n"
        "u = urlparse(uri)\n"
        "try:\n"
        "    s = socket.create_connection((u.hostname, u.port), 0.4)\n"
        "    s.close()\n"
        "    print('UP')\n"
        "except Exception as e:\n"
        "    print('DOWN', e)\n"
        "PY"
    )
    code, out = _run(_ros_bash(script), timeout=8)
    if code != 0:
        return False, (out or "").strip() or "连不上 ROS"
    if (out or "").lstrip().startswith("UP"):
        return True, ""
    return False, ""


def _sweep_running():
    code, out = _run(_ros_bash("pgrep -af run_sweep_client.py || true"), timeout=12)
    if code != 0:
        return False
    for line in out.splitlines():
        if "run_sweep_client.py" in line and "pgrep" not in line:
            return True
    return False


def _tail(path, limit=40):
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[-limit:])


def _status():
    up, err = _node_up()
    root, runs = _list_shots()
    return {
        "node_up": up,
        "node_error": err,
        "sweep_running": _sweep_running() if up else False,
        "sweep_log": _tail(SWEEP_LOG),
        "shots_root": root,
        "latest_run": runs[0]["name"] if runs else "",
    }


def _start_node():
    up, err = _node_up()
    if err and not up:
        return False, err or "连不上 ROS"
    if up:
        return True, "检测节点已在运行"
    os.makedirs(os.path.dirname(LAUNCH_LOG), exist_ok=True)
    code, out = _run(_ros_bash(
        "nohup roslaunch chassis_following chassis_following.launch "
        "> %s 2>&1 &" % LAUNCH_LOG
    ), timeout=15)
    if code != 0:
        return False, out.strip() or "启动失败"
    return True, "已启动检测节点"


def _start_sweep(task_id, vehicle_type):
    up, err = _node_up()
    if not up:
        return False, err or "检测节点没在跑，先启动节点"
    if _sweep_running():
        return False, "已有一趟检测在跑"
    task_id = re.sub(r"[^A-Za-z0-9_-]", "", task_id or "")
    vehicle_type = re.sub(r"[^A-Za-z0-9_]", "", vehicle_type or "")
    os.makedirs(os.path.dirname(SWEEP_LOG), exist_ok=True)
    script = (
        "nohup rosrun chassis_following run_sweep_client.py "
        "_task_id:=\"'%s'\" _vehicle_type:=\"'%s'\" "
        "> %s 2>&1 &" % (task_id, vehicle_type, SWEEP_LOG)
    )
    code, out = _run(_ros_bash(script), timeout=15)
    if code != 0:
        return False, out.strip() or "开扫失败"
    return True, "已发送扫拍。模式 1、2 需要车型，并持续发夹爪位置"


def _odom_topic():
    text = _read(CFG_PATH)
    match = re.search(
        r'(?m)^[ \t]*map_odom_topic:[ \t]*["\']?([^"\'#\n]+)', text)
    if not match:
        return "/zj_humanoid/navigation/odom_info"
    return match.group(1).strip()


def _grab(name, block):
    match = re.search(
        r"(?m)^[ \t]*%s:[ \t]*([-+0-9.eE]+)" % name, block)
    if not match:
        return None
    return float(match.group(1))


def _read_joints():
    topic = "/zj_humanoid/upperlimb/joint_states"
    code, out = _run(_ros_bash("rostopic echo -n 1 %s" % topic), timeout=8)
    match = re.search(r"position:\s*\[([^\]]*)\]", out or "", re.S)
    if code != 0 or not match:
        return None, (out or "").strip() or "没有收到关节 %s" % topic
    nums = [float(x) for x in re.findall(r"[-+0-9.eE]+", match.group(1))]
    if len(nums) != 22:
        return None, "关节数是 %d，需要 22" % len(nums)
    return {"joints": nums, "topic": topic}, ""


def _read_odom():
    topic = _odom_topic()
    code, out = _run(_ros_bash("rostopic echo -n 1 %s" % topic), timeout=8)
    if code != 0 or "orientation:" not in out:
        return None, (out or "").strip() or "没有收到定位 %s" % topic
    pos, ori = out.split("orientation:", 1)
    x, y = _grab("x", pos), _grab("y", pos)
    qx, qy, qz, qw = _grab("x", ori), _grab("y", ori), _grab("z", ori), _grab("w", ori)
    if None in (x, y, qx, qy, qz, qw):
        return None, "定位消息缺位置或姿态"
    yaw = math.atan2(2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz))
    return {
        "x": x,
        "y": y,
        "yaw_deg": math.degrees(yaw),
        "topic": topic,
    }, ""


def _capture(side):
    side = side if side in ("left", "right", "both") else "both"
    folder = os.path.join(_shots_root(), "manual")
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    left = os.path.join(folder, "manual_left_%s.jpg" % stamp) if side != "right" else ""
    right = os.path.join(folder, "manual_right_%s.jpg" % stamp) if side != "left" else ""
    code, out = _run(_ros_bash(
        "rosservice call /chassis_following/capture "
        "\"{left_path: '%s', right_path: '%s'}\"" % (left, right)
    ), timeout=40)
    if code != 0:
        return False, (out or "").strip() or "拍照失败。先启动检测节点"
    return True, (out or "").strip() or "拍照完成"


def _set_light(channel, brightness):
    try:
        channel = int(channel)
        brightness = int(brightness)
    except (TypeError, ValueError):
        return False, "通道和亮度要是整数"
    if channel not in (1, 2):
        return False, "通道只能是 1 或 2"
    if brightness < 0 or brightness > 255:
        return False, "亮度只能是 0 到 255"
    code, out = _run(_ros_bash(
        "rosservice call /chassis_following/light_control/set "
        "\"channel: %d\nbrightness: %d\"" % (channel, brightness)
    ), timeout=15)
    if code != 0:
        return False, (out or "").strip() or "光源设置失败。先启动检测节点"
    return True, (out or "").strip() or "已设置光源"


def _teach_mode(action, arm_type):
    if action not in ("enter", "exit"):
        return False, "示教动作无效"
    try:
        arm_type = int(arm_type)
    except (TypeError, ValueError):
        return False, "arm_type 无效"
    if arm_type not in (1, 2, 3, 4, 8, 15):
        return False, "arm_type 只能是 1 左手、2 右手、3 双手、4 脖子、8 腰、15 全身"
    code, out = _run(_ros_bash(
        "rosservice call /zj_humanoid/upperlimb/teach_mode/%s "
        "\"arm_type: %d\"" % (action, arm_type)
    ), timeout=15)
    if code != 0:
        return False, (out or "").strip() or "示教服务调用失败"
    return True, (out or "").strip() or ("已进入示教" if action == "enter" else "已退出示教")


def _call_trigger(service):
    code, out = _run(_ros_bash(
        'rosservice call %s "{}"' % service), timeout=20)
    if code != 0:
        return False, out.strip() or "调用失败"
    return True, out.strip()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._file(WEB_INDEX, "text/html; charset=utf-8")
            return
        if path == "/api/config":
            self._json(200, {"params": _load_params(), "vehicles": _load_vehicles()})
            return
        if path == "/api/status":
            self._json(200, _status())
            return
        if path == "/api/shots":
            root, runs = _list_shots()
            self._json(200, {"root": root, "runs": runs})
            return
        if path.startswith("/shots/"):
            parts = [unquote(p) for p in path.split("/") if p]
            if len(parts) == 3 and parts[0] == "shots":
                file_path = _safe_shot(parts[1], parts[2])
                if file_path:
                    kind = "image/png" if file_path.lower().endswith(".png") else "image/jpeg"
                    self._file(file_path, kind)
                    return
        self._json(404, {"ok": False, "message": "没有这个地址"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_json()
        except Exception:
            self._json(400, {"ok": False, "message": "请求不是 JSON"})
            return
        try:
            if path == "/api/config":
                with _write_lock:
                    if "params" in body:
                        _save_params(body["params"])
                        _push_save_images(body["params"])
                    if "vehicles" in body:
                        _save_vehicles(body["vehicles"])
                    if "vehicle" in body:
                        name = _save_one_vehicle(body.get("vehicle"), body.get("model") or {})
                        self._json(200, {
                            "ok": True,
                            "message": "已写入车型 %s。下一趟扫拍生效，不用重启" % name,
                        })
                        return
                    if body.get("delete_vehicle"):
                        _delete_vehicle(body.get("delete_vehicle"))
                        self._json(200, {
                            "ok": True,
                            "message": "已删除车型 %s" % body.get("delete_vehicle"),
                        })
                        return
                self._json(200, {"ok": True, "message": "已写入配置。下一趟扫拍生效，不用重启"})
                return
            if path == "/api/node/start":
                ok, msg = _start_node()
                self._json(200 if ok else 400, {"ok": ok, "message": msg})
                return
            if path == "/api/sweep/start":
                ok, msg = _start_sweep(body.get("task_id", ""), body.get("vehicle_type", ""))
                self._json(200 if ok else 400, {"ok": ok, "message": msg})
                return
            if path == "/api/sweep/stop":
                ok, msg = _call_trigger("/chassis_following/stop")
                self._json(200 if ok else 400, {"ok": ok, "message": msg})
                return
            if path == "/api/sweep/reset":
                ok, msg = _call_trigger("/chassis_following/reset")
                self._json(200 if ok else 400, {"ok": ok, "message": msg})
                return
            if path == "/api/joints":
                joints, err = _read_joints()
                if joints is None:
                    self._json(400, {"ok": False, "message": err})
                    return
                joints["ok"] = True
                self._json(200, joints)
                return
            if path == "/api/odom":
                pose, err = _read_odom()
                if pose is None:
                    self._json(400, {"ok": False, "message": err})
                    return
                pose["ok"] = True
                self._json(200, pose)
                return
            if path == "/api/capture":
                ok, msg = _capture(body.get("side", "both"))
                self._json(200 if ok else 400, {"ok": ok, "message": msg})
                return
            if path == "/api/light":
                ok, msg = _set_light(body.get("channel", 1), body.get("brightness", 0))
                self._json(200 if ok else 400, {"ok": ok, "message": msg})
                return
            if path == "/api/teach":
                ok, msg = _teach_mode(body.get("action", ""), body.get("arm_type", 0))
                self._json(200 if ok else 400, {"ok": ok, "message": msg})
                return
        except Exception as e:
            self._json(400, {"ok": False, "message": str(e)})
            return
        self._json(404, {"ok": False, "message": "没有这个地址"})

    def _file(self, path, content_type):
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _purge_old():
    import importlib.util
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "chassis_following", "retain.py")
    spec = importlib.util.spec_from_file_location("retain_files", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        days = int((yaml.safe_load(_read(CFG_PATH)) or {}).get("retain_days", 7))
    except Exception:
        days = 7
    stats = mod.purge_expired(os.path.join(PKG, "logs"), _shots_root(), days)
    if stats["logs"] or stats["images"] or stats["dirs"]:
        print(
            "已删除 %d 天前的日志 %d 个、图片 %d 张、目录 %d 个" % (
                days, stats["logs"], stats["images"], stats["dirs"]),
            flush=True)


def _purge_loop():
    while True:
        try:
            _purge_old()
        except Exception as e:
            print("清理旧日志和图片失败: %s" % e, flush=True)
        time.sleep(6 * 3600)


def main():
    threading.Thread(target=_purge_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("配置页面 http://127.0.0.1:%d" % PORT, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
