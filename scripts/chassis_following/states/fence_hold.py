# -*- coding: utf-8 -*-
"""STOP_HOLD：已取消导航并停住。复位服务才会导航回 home，到位后进 IDLE。"""
from chassis_following.runlog import log


def run(m):
    log.warn_throttle(
        5.0,
        "[chassis_following] 已停止，导航已取消。等待 /chassis_following/reset")
    return 0.0, 0.0, 0.0
