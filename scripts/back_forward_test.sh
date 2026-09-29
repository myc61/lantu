#!/usr/bin/env bash
# 0.1 m/s 后退 2s，再 0.1 m/s 前进 2s。Attach 进容器后执行:  bash scripts/back_forward_test.sh
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source ./ros_env.sh
exec rosrun chassis_following back_forward_test.py "$@"
