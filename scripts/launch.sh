#!/usr/bin/env bash
# 启动跟随节点。Attach 进容器后执行:  bash scripts/launch.sh
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source ./ros_env.sh
exec roslaunch chassis_following chassis_following.launch "$@"
