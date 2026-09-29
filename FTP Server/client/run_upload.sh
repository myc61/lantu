#!/usr/bin/env bash
# 上行压测快速入口
# 用法:
#   ./run_upload.sh
#   SIZE=5M FILES=30 CONCURRENCY=8 ./run_upload.sh
#   ./run_upload.sh --host 10.0.0.5 --remote-dir /uploads
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${CONFIG_FILE:-${SCRIPT_DIR}/client_config.json}"
EXTRA_ARGS="${*:-}"

ARGS=(upload --config "${CONFIG_FILE}")
[[ -n "${CONCURRENCY:-}" ]] && ARGS+=(--concurrency "${CONCURRENCY}")
[[ -n "${ROUNDS:-}" ]]      && ARGS+=(--rounds "${ROUNDS}")
[[ -n "${FILES:-}" ]]       && ARGS+=(--files "${FILES}")
[[ -n "${SIZE:-}" ]]        && ARGS+=(--size "${SIZE}")
[[ -n "${HOST:-}" ]]        && ARGS+=(--host "${HOST}")
[[ -n "${PORT:-}" ]]        && ARGS+=(--port "${PORT}")
# shellcheck disable=SC2206
[[ -n "${EXTRA_ARGS}" ]]    && ARGS+=(${EXTRA_ARGS})

exec python3 "${SCRIPT_DIR}/stress_test.py" "${ARGS[@]}"
