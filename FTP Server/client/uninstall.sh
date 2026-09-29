#!/usr/bin/env bash
# 卸载 chassis_following FTP 压测客户端
set -euo pipefail

PREFIX="/opt/chassis_following/ftp-client"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix) PREFIX="$2"; shift 2 ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

echo "[INFO] 卸载目录: ${PREFIX}"

# 停止并禁用 systemd 定时器
if systemctl list-unit-files | grep -q '^stress-client.timer'; then
    systemctl disable --now stress-client.timer 2>/dev/null || true
    rm -f /etc/systemd/system/stress-client.timer
    rm -f /etc/systemd/system/stress-client.service
    systemctl daemon-reload
    echo "[INFO] 已移除 systemd 单元"
fi

# 移除全局软链
for f in ftp-stress-download ftp-stress-upload ftp-stress-both; do
    [[ -L "/usr/local/bin/${f}" ]] && rm -f "/usr/local/bin/${f}"
done

# 移除安装目录
[[ -d "${PREFIX}" ]] && rm -rf "${PREFIX}" && echo "[INFO] 已删除 ${PREFIX}"

echo "[OK] 卸载完成"
