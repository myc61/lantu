#!/usr/bin/env bash
# chassis_following FTP 服务端启动脚本
#
# 用法:
#   ./run_server.sh                     # 使用 server_config.json
#   ./run_server.sh --anonymous         # 追加命令行参数
#   PORT=2122 ./run_server.sh           # 通过环境变量覆盖端口
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${CONFIG_FILE:-${SCRIPT_DIR}/server_config.json}"
LOG_DIR="${LOG_DIR:-${SCRIPT_DIR}/logs}"
LOG_FILE="${LOG_FILE:-${LOG_DIR}/ftp_server.log}"
PORT="${PORT:-}"
EXTRA_ARGS="${*}"

mkdir -p "${LOG_DIR}"

# 确保 pyftpdlib 已安装
if ! python3 -c "import pyftpdlib" 2>/dev/null; then
    echo "[INFO] 未检测到 pyftpdlib, 正在通过 pip 安装..."
    python3 -m pip install --user -r "${SCRIPT_DIR}/requirements.txt"
fi

ARGS=(
    "${SCRIPT_DIR}/ftp_server.py"
    --config "${CONFIG_FILE}"
    --log-file "${LOG_FILE}"
)
if [[ -n "${PORT}" ]]; then
    ARGS+=(--port "${PORT}")
fi
# shellcheck disable=SC2206
[[ -n "${EXTRA_ARGS}" ]] && ARGS+=(${EXTRA_ARGS})

echo "[INFO] 启动 FTP 服务端: python3 ${ARGS[*]}"
echo "[INFO] 日志文件: ${LOG_FILE}"
exec python3 "${ARGS[@]}"
