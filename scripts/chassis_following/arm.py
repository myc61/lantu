# -*- coding: utf-8 -*-
"""举手：/zj_humanoid/upperlimb/movej/whole_body，arm_type=15（左1+右2+颈4+腰8）。

关节来自配置文件的 raise_joints；文件里没有才用下面这份兜底。
节点构造 ArmController 时立刻发一次 movej。改姿态后重启节点。
"""
import os

import rospy
from chassis_following.runlog import log

try:
    from upperlimb.srv import MoveJ, MoveJRequest
    _IMPORT_ERR = None
except Exception as e:
    MoveJ = None
    MoveJRequest = None
    _IMPORT_ERR = e

SERVICE = "/zj_humanoid/upperlimb/movej/whole_body"
# 仅当配置文件没有 raise_joints 时使用
JOINTS = [
    0.2632574758217743,
    0.1856835858507111,
    0.46076949198322836,
    -0.17064338317595684,
    -0.8285335464454647,
    -0.05406084005244338,
    0.6690175941232588,
    -0.44267105741435464,
    -0.19245467311066022,
    0.23322500737322116,
    -0.5364858270422701,
    0.274714395150113,
    -0.8453713829060462,
    -0.19755995306240948,
    0.6514032747917563,
    0.3407520444604398,
    -0.0007273000851635534,
    0.0011541639494878674,
    -0.803793970994775,
    -0.1042867280284554,
    -0.0003954794328819844,
    0.7771710146266742,
]


class ArmController(object):
    def __init__(self, cfg=None, *args, **kwargs):
        self.raise_time = 5.0
        self.raise_joints = None
        self.charge_time = 5.0
        self.charge_joints = None
        self.charge_pose_joints = None
        self.apply_config(cfg)
        try:
            self.raise_arms()
        except rospy.ROSException as e:
            log.error("[arm] 启动举手失败，movej 服务不可用: %s", e)
        except Exception as e:
            log.error("[arm] 启动举手失败: %s", e)

    def apply_config(self, cfg):
        """用配置文件里的举手时间和关节角。缺关节时举手仍用本文件兜底。"""
        cfg = cfg or {}
        self.raise_time = float(cfg.get("raise_time", 5.0))
        raw = cfg.get("raise_joints")
        self.raise_joints = [float(x) for x in raw] if raw else None
        self.charge_time = float(cfg.get("charge_time", 5.0))
        raw_charge = cfg.get("charge_joints")
        self.charge_joints = [float(x) for x in raw_charge] if raw_charge else None
        raw_pose = cfg.get("charge_pose_joints")
        self.charge_pose_joints = [float(x) for x in raw_pose] if raw_pose else None

    def raise_arms(self):
        if MoveJ is None:
            log.error("[arm] import 不到 upperlimb: %s", _IMPORT_ERR)
            return False
        joints = self.raise_joints if self.raise_joints else [float(x) for x in JOINTS]
        t = float(self.raise_time)
        if t < 3.0:
            t = 5.0
        src = "yaml" if self.raise_joints else "arm.py兜底"
        log.warn(
            "[arm] 开始举手 %s arm_type=15 t=%.1f 来源=%s 文件=%s",
            SERVICE, t, src, os.path.abspath(__file__))
        log.warn("[arm] joints(%d)=%s", len(joints), joints)
        return self.move_whole_body(
            joints, t, is_async=False, arm_type=15, label="举手")

    def _charge_move(self, joints, label):
        if MoveJ is None:
            log.error("[arm] import 不到 upperlimb: %s", _IMPORT_ERR)
            return False
        if not joints or len(joints) != 22:
            log.error("[arm] %s关节不是 22 个，不发", label)
            return False
        t = float(self.charge_time)
        if t < 3.0:
            t = 5.0
        log.warn(
            "[arm] %s %s arm_type=15 t=%.1f joints(%d)",
            label, SERVICE, t, len(joints))
        return self.move_whole_body(
            joints, t, is_async=False, arm_type=15, label=label)

    def leave_home_pose(self):
        """离开 home 前的全身姿态。is_async=false，返回后才发第一段导航。"""
        return self._charge_move(self.charge_joints, "离开home姿态")

    def charge_pose(self):
        """到达姿态变换点后的全身姿态。is_async=false，返回后才发准备点导航。"""
        return self._charge_move(self.charge_pose_joints, "充电姿态")

    def move_whole_body(self, joints, t, is_async=True, arm_type=15, label="全身"):
        """movej/whole_body。检测点姿态用 is_async=true，不占用扫拍节拍。"""
        if MoveJ is None:
            log.error("[arm] import 不到 upperlimb: %s", _IMPORT_ERR)
            return False
        joints = [float(x) for x in joints]
        t = float(t)
        log.warn(
            "[arm] %s %s arm_type=%s t=%.3f is_async=%s joints(%d)",
            label, SERVICE, arm_type, t, bool(is_async), len(joints))
        rospy.wait_for_service(SERVICE, timeout=10.0)
        client = rospy.ServiceProxy(SERVICE, MoveJ)
        req = MoveJRequest()
        req.joints = joints
        req.v = 0.0
        req.acc = 0.0
        req.t = t
        req.is_async = bool(is_async)
        req.arm_type = int(arm_type)
        resp = client(req)
        log.warn("[arm] 服务返回 success=%s message=%s",
                      getattr(resp, "success", None),
                      getattr(resp, "message", resp))
        ok = bool(getattr(resp, "success", True))
        if not ok:
            log.error("[arm] %s被拒绝: %s", label, getattr(resp, "message", resp))
            return False
        log.warn(
            "[arm] 命令已接受（厂家 success 不代表已经摆到目标，应看到手臂在动）")
        return True
