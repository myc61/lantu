#!/usr/bin/env bash
# chassis_following FTP 测试容器 —— 一键控制脚本
#
#   ./ftp_docker.sh build     构建镜像
#   ./ftp_docker.sh up        启动容器并等待健康检查通过
#   ./ftp_docker.sh down      停止并移除容器
#   ./ftp_docker.sh restart   重启（宿主机改了代码/配置后用这个）
#   ./ftp_docker.sh status    容器状态 + 端口监听 + 健康检查详情
#   ./ftp_docker.sh logs      看日志 (追加 -f 持续跟随)
#   ./ftp_docker.sh test      端到端自测: LIST / 下载 / 上传
#   ./ftp_docker.sh shell     进容器 bash
#
# 环境变量:
#   FTP_UID / FTP_GID   容器运行身份，默认取当前用户
#   FTP_EXTRA_ARGS      追加给 ftp_server.py 的命令行参数
#   KEEP_SELFTEST=1     test 后保留下载的临时产物
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FTP_DIR="$(dirname "${SCRIPT_DIR}")"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
CONTAINER="naviai_ftp_test"
# 必须显式隔离项目名: 本机 shell 全局导出了 COMPOSE_PROJECT_NAME=navi_project，
# 实测它比 compose 文件里的 top-level name 优先级高，不传 -p 就会把本容器
# 归入机器人栈项目，后续 down --remove-orphans 会误删整套机器人容器。
# -p 是优先级最高的指定方式，能压过环境变量。
PROJECT="naviai-ftp-test"

cd "${SCRIPT_DIR}"

# 生效端口的推导顺序与 ftp_server.py 的 load_config 保持一致:
#   FTP_HC_PORT > FTP_EXTRA_ARGS 里的 --port > 配置文件 > 内置默认 2121
# 不一致的话，端口占用检查和 test 会对着错误的端口做。
resolve_port() {
    if [[ -n "${FTP_HC_PORT:-}" ]]; then
        echo "${FTP_HC_PORT}"
        return 0
    fi
    local p
    p="$(printf '%s' "${FTP_EXTRA_ARGS:-}" \
         | { grep -oE -- '--port[= ][0-9]+' || true; } \
         | { grep -oE '[0-9]+' || true; } | tail -1)"
    if [[ -n "${p}" ]]; then
        echo "${p}"
        return 0
    fi
    python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("port",2121))' \
        "${SCRIPT_DIR}/server_config.docker.json"
}
PORT="$(resolve_port)"

# 本机 ~/.docker 属 root:root 且 755，当前用户无法在其中创建 buildx/，
# docker compose build 会直接失败在 "mkdir ~/.docker/buildx: permission denied"。
# 这里把 DOCKER_CONFIG 指到可写目录，并把原 config.json 拷过去 ——
# 里面存着内部仓库 (10.51.33.201:30002) 的 registry 认证，丢了会拉不到基础镜像。
ensure_docker_config() {
    if [[ -w "${HOME}/.docker" ]] || mkdir -p "${HOME}/.docker/buildx" 2>/dev/null; then
        rmdir "${HOME}/.docker/buildx" 2>/dev/null || true
        return 0
    fi
    local alt="${HOME}/.cache/docker-config"
    mkdir -p "${alt}"
    if [[ -r "${HOME}/.docker/config.json" && ! -e "${alt}/config.json" ]]; then
        cp "${HOME}/.docker/config.json" "${alt}/config.json"
    fi
    export DOCKER_CONFIG="${alt}"
    echo "[INFO] ~/.docker 不可写，改用 DOCKER_CONFIG=${alt}"
}

# 容器以当前用户身份运行，避免上传文件在宿主机上变成 root 属主删不掉
export FTP_UID="${FTP_UID:-$(id -u)}"
export FTP_GID="${FTP_GID:-$(id -g)}"

# 惰性初始化: 只有真正要调 compose 时才去碰 DOCKER_CONFIG，
# 否则跑 help 也会刷一行无关的权限提示。
_cfg_ready=0
compose() {
    if [[ ${_cfg_ready} -eq 0 ]]; then
        ensure_docker_config
        _cfg_ready=1
    fi
    docker compose -p "${PROJECT}" -f "${COMPOSE_FILE}" "$@"
}

is_running() {
    [[ "$(docker inspect -f '{{.State.Running}}' "${CONTAINER}" 2>/dev/null)" == "true" ]]
}

wait_healthy() {
    local tries="${1:-30}" i status
    echo "[INFO] 等待健康检查 (最多 ${tries}0s)..."
    for ((i = 0; i < tries; i++)); do
        status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
                  "${CONTAINER}" 2>/dev/null || echo missing)"
        case "${status}" in
            healthy)
                echo "[OK] 容器健康"
                docker inspect -f '{{range .State.Health.Log}}{{.Output}}{{end}}' \
                    "${CONTAINER}" 2>/dev/null | { grep -v '^[[:space:]]*$' || true; } | tail -1
                return 0 ;;
            starting) : ;;
            *)
                echo "[WARN] 健康状态=${status}，继续等待..." ;;
        esac
        sleep 10
    done
    echo "[FAIL] 健康检查超时，最近日志:"
    compose logs --tail=40 || true
    return 1
}

cmd_build() {
    echo "[INFO] 构建镜像 naviai-ftp-test:local"
    compose build --pull=false
}

# 容器以 FTP_UID 运行，但 logs/ uploads/ 里如果残留了 root 容器写的文件，
# 降权后就写不进去（日志会 PermissionError，上传会 STOR 失败）。
# 这里提前把这类文件列出来，并给出修复命令。
preflight_perms() {
    local bad
    bad="$(find "${FTP_DIR}/logs" "${FTP_DIR}/uploads" -maxdepth 1 \
              ! -user "${FTP_UID}" -printf '         %p  (%u:%g)\n' 2>/dev/null || true)"
    if [[ -z "${bad}" ]]; then
        return 0
    fi

    echo "[WARN] 以下文件属主不是 uid=${FTP_UID}，容器降权运行时会写失败:"
    echo "${bad}"
    echo "       修复(仅改属主，不删文件):"
    echo "         docker run --rm \\"
    echo "           -v \"${FTP_DIR}/logs:/lg\" -v \"${FTP_DIR}/uploads:/up\" \\"
    echo "           --entrypoint /bin/bash 10.51.33.201:30002/navi_project/demos:v1.0.2 \\"
    echo "           -c 'find /lg /up -maxdepth 1 ! -user ${FTP_UID} -exec chown ${FTP_UID}:${FTP_GID} {} +'"
    echo "       (日志不可写时容器会自动降级为仅 stdout，仍能启动；但上传会真的失败)"
    echo
}

cmd_up() {
    if ss -ltn 2>/dev/null | grep -q ":${PORT}\b"; then
        echo "[FAIL] 端口 ${PORT} 已被占用 —— 宿主机可能正在直接跑 run_server.sh。"
        echo "       容器用 host 网络，二者互斥。请先停掉宿主机服务，或改 server_config.docker.json 的 port。"
        ss -ltnp 2>/dev/null | grep ":${PORT}\b" || true
        exit 1
    fi
    preflight_perms
    compose up -d
    wait_healthy
    cat <<EOF

[OK] FTP 测试容器已就绪
  容器名   : ${CONTAINER}
  网络模式 : host
  控制端口 : ${PORT}
  被动端口 : 60000-60100
  连接地址 : ftp://127.0.0.1:${PORT}  (本机)
             ftp://$(hostname -I 2>/dev/null | awk '{print $1}'):${PORT}  (局域网)
  账号     : stress/stress123  viewer/viewer123  imagetest/imagetest123
  根目录   : /app/shots  <- chassis_following/shots (只读)
  挂载点   : /Image_TEST  /stress_uploads

  自测: ./ftp_docker.sh test      日志: ./ftp_docker.sh logs -f
EOF
}

cmd_down() {
    compose down
    echo "[OK] 容器已移除"
}

cmd_restart() {
    compose restart
    wait_healthy 18
}

cmd_status() {
    compose ps
    echo
    if is_running; then
        echo "--- 健康检查 ---"
        docker inspect -f '{{.State.Health.Status}}' "${CONTAINER}"
        docker inspect -f '{{range .State.Health.Log}}[{{.ExitCode}}] {{.Output}}{{end}}' \
            "${CONTAINER}" 2>/dev/null | tail -3
    fi
    echo "--- 端口监听 ---"
    ss -ltnp 2>/dev/null | grep -E ":${PORT}\b" || echo "(未监听 ${PORT})"
    echo "--- 挂载点 ---"
    docker inspect -f '{{range .Mounts}}{{.Mode}} {{.Source}} -> {{.Destination}} ({{if .RW}}rw{{else}}ro{{end}}){{println}}{{end}}' \
        "${CONTAINER}" 2>/dev/null || true
}

cmd_logs() { compose logs --tail="${TAIL:-80}" "$@"; }

cmd_shell() {
    is_running || { echo "[FAIL] 容器未运行，先 ./ftp_docker.sh up"; exit 1; }
    docker exec -it "${CONTAINER}" bash
}

cmd_test() {
    is_running || { echo "[FAIL] 容器未运行，先 ./ftp_docker.sh up"; exit 1; }
    local work="${SCRIPT_DIR}/.selftest"
    rm -rf "${work}"; mkdir -p "${work}"

    echo "===== 1/4 容器内健康检查 ====="
    docker exec "${CONTAINER}" python3 /opt/ftp/healthcheck.py

    echo
    echo "===== 2/4 LIST / (宿主机视角, 验证虚拟挂载点可见) ====="
    python3 "${FTP_DIR}/stress_test.py" list \
        --host 127.0.0.1 --port "${PORT}" --user stress --password stress123 \
        --remote-dir / 2>&1 | tail -20

    echo
    echo "===== 3/4 下行压测 (从 /Image_TEST 下载) ====="
    python3 "${FTP_DIR}/stress_test.py" download \
        --host 127.0.0.1 --port "${PORT}" --user stress --password stress123 \
        --remote-dir /Image_TEST --pattern '*' \
        --concurrency 4 --rounds 1 --files 8 \
        --work-dir "${work}/down" 2>&1 | tail -15

    echo
    echo "===== 4/4 上行压测 (写入 /stress_uploads) ====="
    # upload 子命令在内存里生成 payload，没有 --work-dir 参数
    python3 "${FTP_DIR}/stress_test.py" upload \
        --host 127.0.0.1 --port "${PORT}" --user stress --password stress123 \
        --remote-dir /stress_uploads --size 1M --payload random \
        --concurrency 2 --rounds 1 --files 4 2>&1 | tail -15

    echo
    echo "--- 上行文件是否真的落到宿主机 uploads/ ---"
    ls -lt "${FTP_DIR}/uploads" 2>/dev/null | head -6 || echo "(uploads/ 为空)"

    # 下载自测会拉下上百 MB，默认跑完就清，需要留证据时 KEEP_SELFTEST=1
    if [[ "${KEEP_SELFTEST:-0}" == "1" ]]; then
        echo
        echo "[OK] 自测完成，产物保留在: ${work} ($(du -sh "${work}" 2>/dev/null | cut -f1))"
    else
        rm -rf "${work}"
        echo
        echo "[OK] 自测完成，临时产物已清理 (需保留请用 KEEP_SELFTEST=1 $0 test)"
    fi
}

case "${1:-help}" in
    build)   cmd_build ;;
    up)      cmd_up ;;
    down)    cmd_down ;;
    restart) cmd_restart ;;
    status)  cmd_status ;;
    logs)    shift; cmd_logs "$@" ;;
    test)    cmd_test ;;
    shell)   cmd_shell ;;
    *)       sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
esac
