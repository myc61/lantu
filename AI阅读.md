# chassis_following：给后续 AI 的代码说明

汽车底盘从机器人旁经过时，机器人导航扫拍底盘螺栓，拍完导航回地图起点。本文件只讲代码框架和设计约束。线协议、字段和 rosbridge JSON 以 `对外接口.md` 为准。欧式距离的算法推导在 `汽车底盘螺栓移动拍照系统技术方案_Python_欧式距离版.md`，那份方案里的 `WAIT_TASK` 等状态没有进当前状态机，不要按它改状态名。

宿主路径 `/home/naviai/navi_project/containers/shared/catkin_ws` 与容器内 `/shared/catkin_ws` 是同一棵树。编译在容器 `naviai_rosbridge` 里做。宿主 shell 没有 rospy。

```bash
docker exec naviai_rosbridge bash -lc 'source /opt/ros/noetic/setup.bash && cd /shared/catkin_ws && catkin_make --pkg chassis_following -DCATKIN_WHITELIST_PACKAGES=chassis_following'
```

改 `.action` / `.srv` 才需要重新生成消息。只改 Python 时重启节点即可。`devel/.../chassis_following/srv/__init__.py` 若在删服务后仍 import 旧模块，且文件属 root，要在容器里改。

## 设计约束

- 底盘不由本包发速度。扫拍、回家、去充电都把目标位姿交给厂商导航 Action `/zj_humanoid/navigation/navigation`。`driver.py`、`manual_drive.py`、`back_forward_test.py` 会发 `/jzhw/joy_ctrl`，只给手动测试，状态机不调用它们。
- 扫拍节点读磁盘上的 `config/chassis_following.yaml`，不把扫拍参数放上参数服务器。每次 `run_sweep` 开始，以及 `IDLE` 时文件 mtime 变化，都会重读。相机、光源、独立充电脚本仍由各自 launch `rosparam load` 同一份 yaml。
- 上层不选拍摄模式。模式是 yaml 的 `sweep_mode`。`run_sweep` 的 goal 只有 `task_id`、`vehicle_type`。
- 状态机在 10 Hz 定时器线程里 `FollowingMachine.step()`。`run_sweep` 的 action 回调在 actionlib 自己的线程里等结果，等的时候不占状态机的锁。不要把会阻塞的调用放进 `step()` 或持锁路径。
- 扫拍节点日志走 `chassis_following.runlog.log`，写到包目录 `logs/chassis_following_YYYY-MM-DD.log`，并打印到终端。不要在扫拍节点里改回 `rospy.log*`。独立的 `run_charge.py` / `run_cancel_charge.py` 仍用 `rospy.log`。
- 拍照一组 = 左腕 + 右腕各一张，`shot_count` 按组计。扫拍走 TCP 后台触发，不占用 `/chassis_following/capture`。该服务只给外部手动拍。

## 线程与结果怎么分开

| 谁 | 在哪跑 | 做什么 |
|---|---|---|
| `rospy.Timer` 10 Hz | 定时器线程 | `machine.step()`，并在状态变化时发布 `safety_state` |
| `SimpleActionServer` | action 线程 | 读 yaml、切到扫拍状态，然后轮询 `snapshot_run()` 和 `pop_sweep_notes()` |
| 腕部传图 | 拍照触发后的后台线程 | 存盘。回家途中可以继续传，不挡状态机 |

反馈和结果不是一回事：

- **feedback** 表示扫拍阶段。欧式距离每拍成一组一条。三种模式在离开扫拍、进入回家时再发一条。此时还没到 `map_home`。`sweep_ok=true` 只对应这四个原因：`扫拍终点到位`、`欧式距离导航到位`、`检测点完成且已超过车头`、`已超过车头`。导航失败回家也会发 feedback，但 `sweep_ok=false`。
- **result** 表示回到 `map_home`。`success=true` 只表示回家到位。扫拍没拍成，只要后来回到家，result 仍可以是成功。外部 `stop` 或自身避障会立刻把本趟记成失败，action 马上以失败结束；回家还在后面走。`record_run` 发现 `trip_done` 已经为真就不再改这条失败原因。

## 状态机

状态字符串：`IDLE`、`NAV_SWEEP`、`NAV_DISTANCE`、`NAV_RETURN`、`STOP_HOLD`、`NAV_CHARGE`、`CHARGING`。

`step()` 每次先 `_sync_stop()`：`stop_flag` 为真且不在 `STOP_HOLD` 就进入 `STOP_HOLD`。扫拍状态跑完后 `_poll_obstacle()`。

```text
IDLE
  ├─ run_sweep 且 sweep_mode 0 或 2 → NAV_SWEEP
  ├─ run_sweep 且 sweep_mode 1       → NAV_DISTANCE
  └─ /chassis_following/charge       → NAV_CHARGE → CHARGING
NAV_SWEEP / NAV_DISTANCE
  ├─ 扫拍结束或导航失败 → NAV_RETURN → IDLE
  ├─ 外部 stop          → STOP_HOLD（action 失败，不自动回家）
  └─ 自身避障           → 记下失败后直接 NAV_RETURN（不留在 STOP_HOLD）
STOP_HOLD
  └─ 仅 /chassis_following/reset → NAV_RETURN → 到家后 IDLE，并发 safety_state=reset
CHARGING 或 NAV_CHARGE
  └─ /chassis_following/end_charge → 关充电 → NAV_RETURN
```

自身避障只在 `NAV_SWEEP` 和 `NAV_DISTANCE` 里判断。订阅 `/zj_humanoid/cmd_vel/calib`（`geometry_msgs/Twist`）。本趟速度曾经非 0，之后六个分量都小于 `1e-3` 并持续 `obstacle_zero_hold_sec`（默认 0.3 秒），且还没在扫拍终点，才触发。触发时先 `_enter_stop_hold` 记下失败并取消导航，然后必须把 `stop_flag` 清掉再 `go_home`。不清的话，下一步 `_sync_stop` 会把回家目标取消掉。到家成功才把 `_reset_signal` 设上。

`/chassis_following/safety_state` 是 latch 的 `std_msgs/String`，`data` 只有 `stop`、`reset`、`none`。`stop` 表示停在外部 `STOP_HOLD`。`reset` 只在两种到家成功之后发：自身避障回家、外部 reset 回家。发送时刻是到家之后，不是刚开始往家走。正常扫拍回家、结束充电回家不发 `reset`。下一趟 `start_sweep` / `start_distance_sweep` / `start_front_sweep` / `start_charge` 会清掉，变回 `none`。

外部停止、复位、充电、结束充电、举手都是 `std_srvs/Trigger`，请求体 `{}`。没有充电话题。充电点在 yaml 的 `charge_x/y/yaw_deg`。内部打开或关闭充电仍是 `/zj_humanoid/chassis/agv_charge`（`chassis_msgs/ChargeControl`，字段 `enable`）。状态机只用 `AgvCharge`，不要从状态机再开 `ChargeClient` 那条导航客户端。`AgvCharge.enable` 会 `wait_for_service` 最多约 5 秒；它若在定时器线程或持 `_lock` 时调用，会卡住状态机。这是已知缺口，不要在修别的逻辑时把更多同步等待加进去。

`go_home` 自己不拿 `_lock`，只在 `_note_sweep_done` 里拿。`_poll_obstacle` 已经持锁，并且先把状态改成 `STOP_HOLD` 再调用 `go_home`，这样不会再次进入 `_note_sweep_done` 造成同锁重入。不要在持锁段里对仍处于 `NAV_SWEEP` / `NAV_DISTANCE` 的状态调用 `go_home`。

## 三种拍摄模式

都从 `IDLE` 且地图定位就绪时进入。导航目标仍是 yaml 的 `sweep_end_*`，拍完回 `map_home_*`。

| sweep_mode | 状态 | 何时拍 | 何时结束扫拍 |
|---|---|---|---|
| 0 | `NAV_SWEEP` | 离开起点 `sweep_photo_move_m` 后，按 `sweep_photo_delay` / `sweep_photo_period` | 导航到 `sweep_end`，或导航失败 |
| 1 | `NAV_DISTANCE` | `robot_s` 进入检测点窗口或穿过检测点 | 全部检测点完成且超过车头，或导航到终点，或导航失败 |
| 2 | `NAV_SWEEP`（`end_on_front=True`） | 与模式 0 相同的定时拍照 | `robot_s > gripper_s + gripper_to_front + safety_margin + front_extra_m`。夹具超时先不结束。若先到 `sweep_end` 也会结束 |

超过车头的公式模式 1 和模式 2 共用，实现在 `DistanceSweep.past_vehicle_front`。模式 1 还要求检测点已经没有剩余。`robot_s` 是相对本趟起点的平面距离。`front_extra_m` 是超过车头后再多走的距离，用来兜住 `gripper_s` 不准。车型几何和检测点在 `config/vehicle_models.yaml`。模式 0 不需要车型和夹具。模式 1、2 的 `vehicle_type` 必填，`task_id` 可空。

模式 1 的夹具或定位超时只暂停触发拍照，导航不停。模式 2 的夹具超时只是暂时不能判断车头。

## 先读哪个文件

| 要改什么 | 读 |
|---|---|
| ROS 入口、action 等待、服务回调、yaml 重载 | `scripts/chassis_following_node.py` |
| 状态切换、停止、复位、避障、feedback 队列 | `scripts/chassis_following/machine.py` |
| 模式 0 和模式 2 的导航与定时拍照 | `scripts/chassis_following/states/nav_sweep.py` |
| 模式 1 的检测点、车头判断、夹具订阅 | `scripts/chassis_following/distance_sweep.py` |
| 回家与 `safety_state=reset` 的置位 | `scripts/chassis_following/states/nav_return.py` |
| 去充电点、到位后 `enable(True)` | `scripts/chassis_following/states/nav_charge.py` |
| 充电中空等 `end_charge` | `scripts/chassis_following/states/charging.py` |
| 外部停止后的空等 | `scripts/chassis_following/states/fence_hold.py` |
| 厂商导航客户端 | `scripts/chassis_following/nav_client.py` |
| 地图定位 `/zj_humanoid/navigation/odom_info` | `scripts/chassis_following/localize.py` |
| 内部 `agv_charge`；`ChargeClient` 只给独立脚本 | `scripts/chassis_following/charge.py` |
| 扫拍 TCP 拍照 | `scripts/chassis_following/capture.py` |
| 举手 | `scripts/chassis_following/arm.py` |
| 对外字段与 rosbridge | `对外接口.md` |
| 状态图 | `状态机流转.html` |

`states/__init__.py` 没有导出充电两个状态。`machine.py` 直接 import `nav_charge` 和 `charging`。

`home.py` 的 `HomeController` 仍被节点构造并传进状态机，当前回家不靠它，靠 `map_home_*` 和 `nav_return.py`。`need_confirm`、`reset_prompt`、`_asked` 是已删除的 confirm 服务残留，没有任何路径再把 `need_confirm` 设上。

包内 `Safety_Alam/`、`FTP Server/`、`light_control_test/` 不是这条状态机的一部分。

## 改代码时不要破坏的行为

- 不要把 `sweep_mode` 加回 action goal。
- 不要恢复 `/chassis_following/confirm`，也不要恢复充电的 `std_msgs/Bool` 话题。
- 不要让 `run_sweep` 在扫拍结束时就 `set_succeeded`。扫完只发 feedback，到家才给 result。
- 不要在外部 `stop` 之后自动回家。回家只来自 `/chassis_following/reset`。
- 不要让自身避障停在 `STOP_HOLD` 等外部 reset。它应直接回家，到家后发 `reset`。
- 模式 0 的终点是写死在 yaml 的 `sweep_end_*`，不要改成跟车。
- 扫拍节点继续从磁盘读 yaml。不要改成只信参数服务器。
