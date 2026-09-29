#!/usr/bin/env bash
# 下行压测快速入口
# 用法:
#   ./run_download.sh                        # 使用 client_config.json
#   ./run_download.sh --host 10.0.0.5        # 追加/覆盖参数
#   CONCURRENCY=8 FILES=100 ./run_download.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${CONFIG_FILE:-${SCRIPT_DIR}/client_config.json}"
EXTRA_ARGS="${*:-}"

ARGS=(download --config "${CONFIG_FILE}")
[[ -n "${CONCURRENCY:-}" ]] && ARGS+=(--concurrency "${CONCURRENCY}")
[[ -n "${ROUNDS:-}" ]]      && ARGS+=(--rounds "${ROUNDS}")
[[ -n "${FILES:-}" ]]       && ARGS+=(--files "${FILES}")
[[ -n "${HOST:-}" ]]        && ARGS+=(--host "${HOST}")
[[ -n "${PORT:-}" ]]        && ARGS+=(--port "${PORT}")
# shellcheck disable=SC2206
[[ -n "${EXTRA_ARGS}" ]]    && ARGS+=(${EXTRA_ARGS})

exec python3 "${SCRIPT_DIR}/stress_test.py" "${ARGS[@]}"
