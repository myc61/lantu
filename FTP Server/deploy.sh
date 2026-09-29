#!/usr/bin/env bash
# 一键部署 chassis_following FTP 压测客户端到远端机器
#
# 流程: pack_client.sh -> scp -> ssh 解压 -> ssh install.sh
#
# 用法:
#   ./deploy.sh --remote user@192.168.1.50
#   ./deploy.sh --remote user@192.168.1.50 --with-systemd
#   ./deploy.sh --remote user@192.168.1.50 --prefix /usr/local/share/ftp-client
#   ./deploy.sh --remote user@192.168.1.50 --port 22 --sudo
#   ./deploy.sh --remote user@192.168.1.50 --dry-run
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REMOTE=""
SSH_PORT=22
PREFIX="/opt/chassis_following/ftp-client"
WITH_SYSTEMD=0
USE_SUDO=0
DRY_RUN=0
KEEP_REMOTE_TGZ=0

usage() {
    cat <<EOF
用法: $(basename "$0") --remote <user@host> [选项]

必选:
  --remote USER@HOST       远端 SSH 目标

可选:
  --port PORT              SSH 端口, 默认 22
  --prefix PATH            远端安装路径, 默认 ${PREFIX}
  --with-systemd           同时安装 systemd 定时器
  --sudo                   远端 install.sh 使用 sudo 执行
  --keep-tgz               保留远端 /tmp 下的 tar.gz (默认安装后删除)
  --dry-run                只打印将要执行的命令, 不实际执行
  -h, --help               显示帮助

示例:
  ./deploy.sh --remote stress@10.0.0.5 --with-systemd --sudo
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --remote)         REMOTE="$2"; shift 2 ;;
        --port)           SSH_PORT="$2"; shift 2 ;;
        --prefix)         PREFIX="$2"; shift 2 ;;
        --with-systemd)   WITH_SYSTEMD=1; shift ;;
        --sudo)           USE_SUDO=1; shift ;;
        --keep-tgz)       KEEP_REMOTE_TGZ=1; shift ;;
        --dry-run)        DRY_RUN=1; shift ;;
        -h|--help)        usage; exit 0 ;;
        *) echo "[ERROR] 未知参数: $1"; usage; exit 1 ;;
    esac
done

if [[ -z "${REMOTE}" ]]; then
    echo "[ERROR] 必须指定 --remote USER@HOST"
    usage
    exit 1
fi

SSH_OPTS=(-p "${SSH_PORT}" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)
SUDO_CMD=""
[[ ${USE_SUDO} -eq 1 ]] && SUDO_CMD="sudo "
SYSTEMD_FLAG=""
[[ ${WITH_SYSTEMD} -eq 1 ]] && SYSTEMD_FLAG="--with-systemd"

run() {
    if [[ ${DRY_RUN} -eq 1 ]]; then
        echo "[DRY] $*"
    else
        echo "[RUN] $*"
        "$@"
    fi
}

echo "============================================================"
echo " chassis_following FTP 客户端一键部署"
echo "   远端:     ${REMOTE}"
echo "   SSH 端口: ${SSH_PORT}"
echo "   安装路径: ${PREFIX}"
echo "   systemd:  $([[ ${WITH_SYSTEMD} -eq 1 ]] && echo '启用' || echo '不启用')"
echo "   sudo:     $([[ ${USE_SUDO} -eq 1 ]] && echo '是' || echo '否')"
echo "============================================================"

# 1) 本地打包
echo "[STEP 1/4] 打包客户端..."
PACK_OUT="$(bash "${SCRIPT_DIR}/pack_client.sh" | tail -20)"
echo "${PACK_OUT}"
TGZ=$(ls -1t "${SCRIPT_DIR}"/dist/ftp-stress-client-*.tar.gz 2>/dev/null | head -1)
if [[ -z "${TGZ}" ]]; then
    echo "[ERROR] 打包失败, 未找到 tar.gz"
    exit 2
fi
echo "[INFO] 使用部署包: ${TGZ}"

# 2) 连通性检查
echo "[STEP 2/4] 检查 SSH 连通性..."
run ssh "${SSH_OPTS[@]}" "${REMOTE}" "echo ok && uname -a && python3 --version"

# 3) 上传
REMOTE_TGZ="/tmp/$(basename "${TGZ}")"
echo "[STEP 3/4] 上传到 ${REMOTE}:${REMOTE_TGZ} ..."
run scp "${SSH_OPTS[@]}" "${TGZ}" "${REMOTE}:${REMOTE_TGZ}"

# 4) 远端解压 + 安装
echo "[STEP 4/4] 远端解压并安装..."
REMOTE_CMD=$(cat <<EOF
set -e
cd /tmp
rm -rf ftp-stress-client
tar -xzf "${REMOTE_TGZ}"
cd ftp-stress-client
${SUDO_CMD}bash ./install.sh --prefix "${PREFIX}" ${SYSTEMD_FLAG}
EOF
)
if [[ ${KEEP_REMOTE_TGZ} -eq 0 ]]; then
    REMOTE_CMD+=$'\n'"rm -f ${REMOTE_TGZ}"
fi

run ssh "${SSH_OPTS[@]}" "${REMOTE}" "bash -s" <<< "${REMOTE_CMD}"

cat <<EOF

============================================================
✅ 部署完成

远端后续操作:
  1) 修改配置:
     ssh ${REMOTE}
     \${SUDO_CMD}vim ${PREFIX}/client_config.json   # 设置 host/user/password

  2) 执行压测:
     ${PREFIX}/run_download.sh
     ${PREFIX}/run_upload.sh
     ${PREFIX}/run_both.sh

  3) 查看报告:
     cat /tmp/ftp_stress_report.json
$([[ ${WITH_SYSTEMD} -eq 1 ]] && echo "
  4) systemd 定时器:
     systemctl list-timers | grep stress-client
     journalctl -u stress-client.service -f")
============================================================
EOF
