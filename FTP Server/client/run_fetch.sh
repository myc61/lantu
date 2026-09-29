#!/usr/bin/env bash
# 按文件夹获取 shots 图片到本地 (保留目录结构, 不删除)
#
# 用法:
#   ./run_fetch.sh                                # 获取全部 session
#   ./run_fetch.sh 2026-09-04_11-18-02            # 只获取指定 session 文件夹
#   ./run_fetch.sh 2026-09-04_11-18-02 --skip-existing
#   LOCAL_DIR=/data/shots ./run_fetch.sh          # 环境变量指定本地目录
#   ./run_fetch.sh --list                         # 先列出有哪些 session
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${CONFIG_FILE:-${SCRIPT_DIR}/client_config.json}"

# stress_test.py 位置: 部署后与本脚本同目录; 源码树中在上一级
ST="${SCRIPT_DIR}/stress_test.py"
[[ -f "${ST}" ]] || ST="${SCRIPT_DIR}/../stress_test.py"

# --list 快捷: 列出远端 session 文件夹
if [[ "${1:-}" == "--list" ]]; then
    exec python3 "${ST}" list --config "${CONFIG_FILE}"
fi

ARGS=(fetch --config "${CONFIG_FILE}")

# 第一个参数若不以 - 开头, 视为 session 文件夹名
if [[ $# -gt 0 && "${1:0:1}" != "-" ]]; then
    ARGS+=(--session "$1")
    shift
fi

# 环境变量覆盖
[[ -n "${LOCAL_DIR:-}" ]]     && ARGS+=(--local-dir "${LOCAL_DIR}")
[[ -n "${CONCURRENCY:-}" ]]   && ARGS+=(--concurrency "${CONCURRENCY}")
[[ -n "${SKIP_EXISTING:-}" ]] && ARGS+=(--skip-existing)
[[ -n "${HOST:-}" ]]          && ARGS+=(--host "${HOST}")
[[ -n "${PORT:-}" ]]          && ARGS+=(--port "${PORT}")

# 追加剩余参数
[[ $# -gt 0 ]] && ARGS+=("$@")

exec python3 "${ST}" "${ARGS[@]}"
