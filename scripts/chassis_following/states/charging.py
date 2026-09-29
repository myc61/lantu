# -*- coding: utf-8 -*-
"""CHARGING：充电已打开，停在充电点，等外部结束充电。"""
from chassis_following.runlog import log


def run(m):
    log.info_throttle(
        10.0, "[chassis_following] 充电中，等 /chassis_following/end_charge")
    return 0.0, 0.0, 0.0
