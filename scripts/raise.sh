#!/usr/bin/env bash
# 姿态在节点启动时由 ArmController 发出，不再提供 /chassis_following/raise。
echo "举手已改为节点启动时自动发送。改 yaml 的 raise_joints 后重启扫拍节点。"
exit 0
