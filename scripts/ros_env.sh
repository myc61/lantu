# 容器内 ROS 环境。被 launch/raise/build 脚本 source，不必在终端里手敲。
# 路径不对时改下面几行，或设环境变量 ROS_SETUP / NAVI_SETUP / CATKIN_SETUP。
: "${ROS_SETUP:=/opt/ros/noetic/setup.bash}"
: "${NAVI_SETUP:=/navi_ws/devel/setup.bash}"
: "${CATKIN_SETUP:=/shared/catkin_ws/devel/setup.bash}"

if [ ! -f "$ROS_SETUP" ]; then
  echo "找不到 ROS: $ROS_SETUP" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$ROS_SETUP"

# 叠加主工程工作空间（导航消息等），没有则跳过
if [ -f "$NAVI_SETUP" ]; then
  # shellcheck disable=SC1090
  source "$NAVI_SETUP"
fi

if [ ! -f "$CATKIN_SETUP" ]; then
  echo "找不到工作空间: $CATKIN_SETUP（先 catkin_make，或设 CATKIN_SETUP）" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$CATKIN_SETUP"
