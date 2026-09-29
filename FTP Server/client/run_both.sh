#!/usr/bin/env bash
# 上下行混合压测（先上传，再回传下载校验）
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${CONFIG_FILE:-${SCRIPT_DIR}/client_config.json}"
EXTRA_ARGS="${*:-}"

ARGS=(both --config "${CONFIG_FILE}")
[[ -n "${CONCURRENCY:-}" ]] && ARGS+=(--concurrency "${CONCURRENCY}")
[[ -n "${ROUNDS:-}" ]]      && ARGS+=(--rounds "${ROUNDS}")
[[ -n "${FILES:-}" ]]       && ARGS+=(--files "${FILES}")
[[ -n "${SIZE:-}" ]]        && ARGS+=(--size "${SIZE}")
[[ -n "${HOST:-}" ]]        && ARGS+=(--host "${HOST}")
[[ -n "${PORT:-}" ]]        && ARGS+=(--port "${PORT}")
# shellcheck disable=SC2206
[[ -n "${EXTRA_ARGS}" ]]    && ARGS+=(${EXTRA_ARGS})

exec python3 "${SCRIPT_DIR}/stress_test.py" "${ARGS[@]}"
