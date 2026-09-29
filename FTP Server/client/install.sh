#!/usr/bin/env bash
# chassis_following FTP 压测客户端 —— 远端一键安装脚本
#
# 在远端机器上执行：
#   tar -xzf ftp-stress-client-*.tar.gz
#   cd ftp-stress-client
#   sudo ./install.sh                    # 安装到 /opt/chassis_following/ftp-client
#   sudo ./install.sh --prefix /usr/local/share/ftp-client
#   sudo ./install.sh --with-systemd     # 同时安装 systemd 定时任务
#
set -euo pipefail

PREFIX="/opt/chassis_following/ftp-client"
WITH_SYSTEMD=0
USER_NAME="${SUDO_USER:-$(whoami)}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix)       PREFIX="$2"; shift 2 ;;
        --with-systemd) WITH_SYSTEMD=1; shift ;;
        --user)         USER_NAME="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[INFO] 安装源目录 : ${SRC_DIR}"
echo "[INFO] 安装目标   : ${PREFIX}"
echo "[INFO] 运行用户   : ${USER_NAME}"

# 1) Python 环境检查
if ! command -v python3 >/dev/null 2>&1; then
    echo "[ERROR] 未检测到 python3, 请先安装 (apt install python3)"
    exit 2
fi
PY_VER=$(python3 -c 'import sys;print(".".join(map(str,sys.version_info[:2])))')
echo "[INFO] Python 版本: ${PY_VER}"

# 2) 拷贝文件
mkdir -p "${PREFIX}"
install -m 0755 "${SRC_DIR}/stress_test.py"      "${PREFIX}/stress_test.py"
install -m 0644 "${SRC_DIR}/client_config.json"  "${PREFIX}/client_config.json"
for f in run_download.sh run_upload.sh run_both.sh run_fetch.sh; do
    install -m 0755 "${SRC_DIR}/${f}" "${PREFIX}/${f}"
done
install -m 0644 "${SRC_DIR}/README.md"           "${PREFIX}/README.md" 2>/dev/null || true

# 3) 全局软链
LINK_DIR="/usr/local/bin"
if [[ -w "${LINK_DIR}" ]] || [[ ${EUID} -eq 0 ]]; then
    ln -sf "${PREFIX}/run_download.sh" "${LINK_DIR}/ftp-stress-download"
    ln -sf "${PREFIX}/run_upload.sh"   "${LINK_DIR}/ftp-stress-upload"
    ln -sf "${PREFIX}/run_both.sh"     "${LINK_DIR}/ftp-stress-both"
    ln -sf "${PREFIX}/run_fetch.sh"    "${LINK_DIR}/ftp-fetch"
    echo "[INFO] 已创建全局命令: ftp-stress-download / ftp-stress-upload / ftp-stress-both / ftp-fetch"
fi

# 4) 属主修正
if [[ ${EUID} -eq 0 && "${USER_NAME}" != "root" ]]; then
    chown -R "${USER_NAME}:${USER_NAME}" "${PREFIX}" 2>/dev/null || true
fi

# 5) 可选：安装 systemd 单元 (定时压测)
if [[ ${WITH_SYSTEMD} -eq 1 ]]; then
    if [[ ! -f "${SRC_DIR}/stress-client.service" ]]; then
        echo "[WARN] 未找到 stress-client.service, 跳过 systemd 安装"
    else
        UNIT_DIR="/etc/systemd/system"
        sed "s|__PREFIX__|${PREFIX}|g; s|__USER__|${USER_NAME}|g" \
            "${SRC_DIR}/stress-client.service" > "${UNIT_DIR}/stress-client.service"
        if [[ -f "${SRC_DIR}/stress-client.timer" ]]; then
            sed "s|__PREFIX__|${PREFIX}|g; s|__USER__|${USER_NAME}|g" \
                "${SRC_DIR}/stress-client.timer" > "${UNIT_DIR}/stress-client.timer"
            systemctl daemon-reload
            systemctl enable --now stress-client.timer
            echo "[INFO] 已启用 stress-client.timer (systemctl list-timers 查看)"
        else
            systemctl daemon-reload
            echo "[INFO] 已安装 stress-client.service, 手动启动: systemctl start stress-client"
        fi
    fi
fi

# 6) 冒烟测试
echo "[INFO] 冒烟测试: 显示帮助"
python3 "${PREFIX}/stress_test.py" --help >/dev/null && echo "  ✓ stress_test.py 可执行"

cat <<EOF

============================================================
安装完成 ✅

后续操作:
  1. 编辑配置:    ${PREFIX}/client_config.json
                  修改 host / user / password 指向实际 FTP 服务端
  2. 执行下行:    ${PREFIX}/run_download.sh
     执行上行:    ${PREFIX}/run_upload.sh
     混合压测:    ${PREFIX}/run_both.sh
  3. 查看报告:    /tmp/ftp_stress_report.json (可在配置里改)
============================================================
EOF
