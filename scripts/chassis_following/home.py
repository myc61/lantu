# -*- coding: utf-8 -*-
"""记下作业等待区起点（里程计位姿）。回家由厂商导航完成。"""


class HomeController(object):
    def __init__(self):
        self.home_x = 0.0
        self.home_y = 0.0
        self.home_yaw = 0.0
        self.has_home = False

    def set_home(self, x, y, yaw):
        self.home_x = float(x)
        self.home_y = float(y)
        self.home_yaw = float(yaw)
        self.has_home = True
