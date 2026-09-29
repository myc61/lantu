# Chassis-following

流水线：汽车底盘从机器人上方经过。车头到位后，机器人导航去扫拍终点并边走边拍，拍完导航回起点。

## 文件

```
Chassis-following/
├── README.md
├── config/chassis_following.yaml          # 全部可调参数
├── launch/chassis_following.launch
├── action/RunDistanceSweep.action         # 一整趟：扫完 feedback，到家 result
├── srv/Capture.srv                        # 左右腕拍一组
├── srv/SetLight.srv                       # 光源亮度
├── CMakeLists.txt / package.xml / setup.py
└── scripts/
    ├── chassis_following_node.py          # ROS 入口：订话题、跑状态机
    ├── qvps_camera_node.py                # 前端拍照服务 /chassis_following/capture（扫拍不走这里）
    ├── light_control_node.py              # 光源服务 /chassis_following/light_control/set
    ├── run_sweep_client.py                # 调 run_sweep 动作，扫完看 feedback，到家看 result
    └── chassis_following/
        ├── nav_client.py                  # 厂商导航 Action 客户端
        ├── localize.py                    # 地图定位
        ├── home.py                        # 记下作业等待区起点
        ├── arm.py                         # 举手
        ├── capture.py                     # 腕部拍照（TCP + 服务客户端）
        ├── machine.py                     # 状态机
        └── states/
            ├── idle.py                    # 等 run_sweep
            ├── nav_sweep.py               # 导航扫拍
            ├── nav_return.py              # 导航回家
            └── fence_hold.py              # 外部停障停车
```

## 流程

```
IDLE 等 run_sweep
  → NAV_SWEEP 导航去扫拍终点，离开发车点后按间隔拍照
  → NAV_RETURN 导航回 map_home
  → IDLE 等下一辆

任意时刻 rosservice call /chassis_following/stop "{}" → 取消导航，进入 STOP_HOLD 并停住
仅在 STOP_HOLD 时 rosservice call /chassis_following/reset "{}" → 导航回 map_home，到位后进 IDLE
外部充电 /chassis_following/charge → 从待命去充电点，到位后打开充电
外部结束充电 /chassis_following/end_charge → 关掉充电，进入 NAV_RETURN 导航回 map_home。关充电失败也回家
```

手动开一整趟（阻塞到回家）：

```bash
rosrun chassis_following run_sweep_client.py
# 模式在 config/chassis_following.yaml 的 sweep_mode
# 0 定时到终点；1 欧式距离；2 定时、超过车头结束
# 模式 1、2 再加：_task_id:=T001 _vehicle_type:=SUV_A
```

## 接口

| 用途 | 话题 / 服务 |
|---|---|
| 地图定位 | `/zj_humanoid/navigation/odom_info` |
| 导航 | `/zj_humanoid/navigation/navigation`（厂商 Action） |
| 一整趟扫拍+回家 | `/chassis_following/run_sweep`（Action `RunDistanceSweep`；模式见 yaml `sweep_mode`；扫完 feedback，到家 result） |
| 前端拍照一组 | `/chassis_following/capture`（`chassis_following/Capture`，给外部用；扫拍直接 TCP） |
| 光源亮度 | `/chassis_following/light_control/set`（`chassis_following/SetLight`，channel 1/2，brightness 0-255） |
| 举手 | `/chassis_following/raise`；时间和关节角见 yaml `raise_time`、`raise_joints` |
| 外部停止 | `/chassis_following/stop`（`std_srvs/Trigger`，`{}`：取消导航，进入 STOP_HOLD） |
| 外部复位 | `/chassis_following/reset`（`std_srvs/Trigger`，`{}`：仅 STOP_HOLD 时回 home，到位后进 IDLE） |
| 外部充电 | `/chassis_following/charge`（`std_srvs/Trigger`，`{}`） |
| 外部结束充电 | `/chassis_following/end_charge`（`std_srvs/Trigger`，`{}`） |

扫拍终点、回家点、拍照间隔在 yaml。导航速度在厂商导航配置里改，本包不发 `/jzhw/joy_ctrl`。

## 运行

```bash
roslaunch chassis_following chassis_following.launch
```
