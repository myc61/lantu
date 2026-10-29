# run_sweep 动作

上层开一趟扫拍用动作 `/chassis_following/run_sweep`，类型 `chassis_following/RunDistanceSweep`。这不是服务。客户端发 goal 之后保持连接，扫拍过程中收 feedback，回到 `map_home` 之后才收 result。

拍摄模式不在 goal 里。模式是 `config/chassis_following.yaml` 的 `sweep_mode`，方向是同文件的 `sweep_from`。改完保存，下一趟生效，不用重启。当前是 `sweep_mode: 1`、`sweep_from: front`。

走 rosbridge 时连 `ws://<主机>:9090`。每条都是一行 JSON。

## 话题

| 话题 | 类型 | 谁发 |
|---|---|---|
| `/chassis_following/run_sweep/goal` | `chassis_following/RunDistanceSweepActionGoal` | 上层 |
| `/chassis_following/run_sweep/cancel` | `actionlib_msgs/GoalID` | 上层，取消这一单 |
| `/chassis_following/run_sweep/feedback` | `chassis_following/RunDistanceSweepActionFeedback` | 节点 |
| `/chassis_following/run_sweep/result` | `chassis_following/RunDistanceSweepActionResult` | 节点 |
| `/chassis_following/run_sweep/status` | `actionlib_msgs/GoalStatusArray` | 节点 |

`goal_id.id` 由上层自己取一个不重复的字符串。feedback、result、cancel 都用这个 id 对上同一单。

## goal

在 `msg.goal` 里。三个字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| task_id | string | 任务号。模式 1、2 可空。模式 0 可空 |
| vehicle_type | string | 车型名，须是 `config/vehicle_models.yaml` 里的键。模式 1、2 必填。模式 0 可空。不要用纯数字：不带引号的 `1` 会被 YAML 读成整数，字符串 `"1"` 对不上，本单以未知车型失败 |
| speed | float64 | 被检车辆沿机器人行走方向的速度，米/秒，大于等于 0。模式 0 不读 |

`speed` 为 0 表示车停着，车身位置保持在接到任务时的位置。任务照常启动，机器人自己走并拍照。这不是暂停。暂停是另一个服务 `/chassis_following/pause`，调用之后本动作不结束，也不发 result。

仅状态 `IDLE`、地图定位已就绪、且没有外部停障时接受。已经在跑、或处于 `STOP_HOLD`，本单立刻以失败结束，没有 feedback。

## feedback

在 `msg.feedback` 里。人还没到 `map_home`。

| 字段 | 类型 | 含义 |
|---|---|---|
| sweep_ok | bool | 这一组已拍为 true |
| message | string | 模式 1 为 `检测点 P01 已拍` 这种，模式 0、2 为 `第1组已拍` 这种 |
| shot_count | uint32 | 本趟已触发的组数。一次触发算一组，不管这一组拍了左侧、右侧还是两侧 |
| run_dir | string | 本趟照片目录 |
| trace_id_L | string | 这一组发给左腕的 TCP 追踪号，以 `_L` 结尾，如 `20261029-201845-123456_L`。这一组没拍左腕时为空 |
| trace_id_R | string | 这一组发给右腕的 TCP 追踪号，以 `_R` 结尾，如 `20261029-201845-123456_R`。这一组没拍右腕时为空 |

只在拍成一组时发。超过车头、超过车尾、导航到终点、导航失败、开始回家都不另发。到家看 result。

| sweep_mode | feedback |
|---|---|
| 0 定时到终点 | 每拍成一组发一条，`message` 为 `第1组已拍` |
| 1 欧式距离 | 每拍成一组发一条，`message` 为 `检测点 P01 已拍` |
| 2 定时，超过本趟那一端结束 | 每拍成一组发一条，`message` 为 `第1组已拍` |

模式 1 这一组拍哪一侧由该检测点的 `capture.sides` 决定：`both`、`left` 或 `right`。模式 0、2 固定左右都拍。后面回家失败时，result 仍可以是失败。扫拍没拍成，只要后来回到家，result 仍可以是成功。

暂停期间不发新的 feedback，本动作继续挂着。

## result

在 `msg.result` 里。只有回到 `map_home`、或本单被拒绝、取消、超时之后才有。超过车头或车尾不发 feedback。

| 字段 | 类型 | 含义 |
|---|---|---|
| success | bool | 只表示回到了 `map_home`。不表示每个检测点都拍到了 |
| message | string | 说明 |
| point_interval | float64 | 本趟车型启用检测点相邻间隔的均值，米。SUV_A 为 0.6。模式 0、开扫被拒绝、启用点不足两个时为 0。开扫成功后，取消、超时、回家失败也带这个均值 |

同时看外层 `msg.status.status`：

| status | 名字 | 何时 |
|---|---|---|
| 1 | ACTIVE | feedback 里常见。动作还在执行，包括暂停中、回家途中 |
| 2 | PREEMPTED | 上层取消了这一单。`success=false`，message 为 `动作被取消`，若取消时本趟已经记过结果则用那个说明 |
| 3 | SUCCEEDED | 回到了 `map_home`。`success=true`，message 为 `导航已回地图起点` |
| 4 | ABORTED | 本单失败。`success=false` |

失败时常见 message：

| message | 何时 |
|---|---|
| `未知车型 ...`、`未提供车型`、`已经在跑（...）`、`地图定位还没有，不能开导航扫拍` | 开扫被拒绝，没有 feedback |
| `自身避障速度为0` | 扫拍中 `/zj_humanoid/cmd_vel/calib` 曾经非 0，之后全 0 持续了 yaml 的 `obstacle_zero_hold_sec`（当前 3.0 秒），并且还没到扫拍终点。节点取消扫拍并导航回家，到家后 `/chassis_following/safety_state` 为 `reset` |
| `回家未到位` | 回 home 的导航超时或失败 |
| `run_sweep 超时（state=...）` | 整单超过约 `2 * nav_timeout + 30` 秒。暂停期间这个计时会重新计算 |
| `节点关闭` | 动作还在执行时节点退出 |

外部 `/chassis_following/stop` 会使本单失败并进入 `STOP_HOLD`，不自动回家。之后要再调 `/chassis_following/reset`。

## rosbridge 调用顺序

先订阅 feedback 和 result，再声明 goal 的发布者，最后发 goal。先发 goal 会漏掉开头的 feedback。

`id` 是这条 rosbridge 指令自己的编号，和 `goal_id.id` 不是同一个字段。

### 1. 订阅

```json
{"op":"subscribe","id":"fb","topic":"/chassis_following/run_sweep/feedback","type":"chassis_following/RunDistanceSweepActionFeedback"}
{"op":"subscribe","id":"rs","topic":"/chassis_following/run_sweep/result","type":"chassis_following/RunDistanceSweepActionResult"}
```

### 2. 声明并发送 goal

当前现场是模式 1、从车头走向车尾。车速 0 表示车停着，机器人照常扫拍：

```json
{"op":"advertise","id":"adv-goal","topic":"/chassis_following/run_sweep/goal","type":"chassis_following/RunDistanceSweepActionGoal"}
{"op":"publish","id":"pub-goal","topic":"/chassis_following/run_sweep/goal","msg":{"goal_id":{"id":"sweep-1"},"goal":{"task_id":"T001","vehicle_type":"SUV_A","speed":0.0}}}
```

车在动时把 `speed` 换成实际速度，单位米/秒，例如 `0.2`。模式 0 时三项传空和 0：

```json
{"op":"publish","id":"pub-goal-0","topic":"/chassis_following/run_sweep/goal","msg":{"goal_id":{"id":"sweep-2"},"goal":{"task_id":"","vehicle_type":"","speed":0.0}}}
```

### 3. 收到的 feedback

模式 1 每拍一组收到一条。rosbridge 把话题消息转成 `op: publish` 推给订阅方：

```json
{"op":"publish","topic":"/chassis_following/run_sweep/feedback","msg":{"header":{"stamp":{"secs":0,"nsecs":0},"frame_id":""},"status":{"goal_id":{"stamp":{"secs":0,"nsecs":0},"id":"sweep-1"},"status":1,"text":""},"feedback":{"sweep_ok":true,"message":"检测点 P09 已拍","shot_count":1,"run_dir":"/shared/catkin_ws/src/chassis_following/shots/2026-10-28_19-53-36","trace_id_L":"20261029-201845-123456_L","trace_id_R":"20261029-201845-123456_R"}}}
```

超过车头或车尾、开始回家时不再收到 feedback。到家才收到 result。

### 4. 收到的 result

到家成功：

```json
{"op":"publish","topic":"/chassis_following/run_sweep/result","msg":{"status":{"goal_id":{"id":"sweep-1"},"status":3,"text":""},"result":{"success":true,"message":"导航已回地图起点","point_interval":0.6}}}
```

开扫被拒绝，或回家、避障失败：

```json
{"op":"publish","topic":"/chassis_following/run_sweep/result","msg":{"status":{"goal_id":{"id":"sweep-1"},"status":4,"text":""},"result":{"success":false,"message":"未知车型 1","point_interval":0.0}}}
```

### 5. 取消

先声明 cancel，再按同一个 `goal_id.id` 发布。取消后收到的 result 里 `status` 为 2。

```json
{"op":"advertise","id":"adv-cancel","topic":"/chassis_following/run_sweep/cancel","type":"actionlib_msgs/GoalID"}
{"op":"publish","id":"pub-cancel","topic":"/chassis_following/run_sweep/cancel","msg":{"stamp":{"secs":0,"nsecs":0},"id":"sweep-1"}}
```

## 命令行

```bash
rosrun chassis_following run_sweep_client.py _task_id:=T001 _vehicle_type:=SUV_A _speed:=0.0
```

客户端打印每一条 feedback，到家后打印 result。`success=false` 时进程退出码为 1。
