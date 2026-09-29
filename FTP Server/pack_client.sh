#!/usr/bin/env bash
# 打包 chassis_following FTP 压测客户端为独立 tar.gz，便于分发到远端机器。
#
# 产物: dist/ftp-stress-client-YYYYmmdd-HHMM.tar.gz
#
# 用法:
#   ./pack_client.sh
#   ./pack_client.sh --output /tmp/myclient.tar.gz
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIENT_SRC="${SCRIPT_DIR}/client"
DIST_DIR="${SCRIPT_DIR}/dist"
STAMP="$(date +%Y%m%d-%H%M)"
PKG_NAME="ftp-stress-client"
OUTPUT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output|-o) OUTPUT="$2"; shift 2 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

mkdir -p "${DIST_DIR}"
[[ -z "${OUTPUT}" ]] && OUTPUT="${DIST_DIR}/${PKG_NAME}-${STAMP}.tar.gz"

# 校验必需文件
REQUIRED=(
    "${SCRIPT_DIR}/stress_test.py"
    "${CLIENT_SRC}/client_config.json"
    "${CLIENT_SRC}/run_download.sh"
    "${CLIENT_SRC}/run_upload.sh"
    "${CLIENT_SRC}/run_both.sh"
    "${CLIENT_SRC}/run_fetch.sh"
    "${CLIENT_SRC}/install.sh"
    "${CLIENT_SRC}/uninstall.sh"
    "${CLIENT_SRC}/stress-client.service"
    "${CLIENT_SRC}/stress-client.timer"
    "${CLIENT_SRC}/README.md"
)
for f in "${REQUIRED[@]}"; do
    if [[ ! -f "${f}" ]]; then
        echo "[ERROR] 缺少文件: ${f}"
        exit 2
    fi
done

# 构建临时目录树（/tmp 不可写时回退到工作区 .build）
TMP_ROOT=""
if TMP_ROOT="$(mktemp -d 2>/dev/null)" && [[ -n "${TMP_ROOT}" && -w "${TMP_ROOT}" ]]; then
    :
else
    TMP_ROOT="${SCRIPT_DIR}/.build"
    rm -rf "${TMP_ROOT}"
    mkdir -p "${TMP_ROOT}"
fi
trap 'rm -rf "${TMP_ROOT}"' EXIT
PKG_DIR="${TMP_ROOT}/${PKG_NAME}"
mkdir -p "${PKG_DIR}"

# 复制客户端主程序 (来自 FTP Server 根目录)
cp "${SCRIPT_DIR}/stress_test.py" "${PKG_DIR}/"
# 复制 client/ 下所有资源
cp "${CLIENT_SRC}/client_config.json"      "${PKG_DIR}/"
cp "${CLIENT_SRC}/run_download.sh"         "${PKG_DIR}/"
cp "${CLIENT_SRC}/run_upload.sh"           "${PKG_DIR}/"
cp "${CLIENT_SRC}/run_both.sh"             "${PKG_DIR}/"
cp "${CLIENT_SRC}/run_fetch.sh"            "${PKG_DIR}/"
cp "${CLIENT_SRC}/install.sh"              "${PKG_DIR}/"
cp "${CLIENT_SRC}/uninstall.sh"            "${PKG_DIR}/"
cp "${CLIENT_SRC}/stress-client.service"   "${PKG_DIR}/"
cp "${CLIENT_SRC}/stress-client.timer"     "${PKG_DIR}/"
cp "${CLIENT_SRC}/README.md"               "${PKG_DIR}/"

# 赋予可执行权限
chmod +x "${PKG_DIR}/stress_test.py"
chmod +x "${PKG_DIR}/"*.sh

# 打包
tar -czf "${OUTPUT}" -C "${TMP_ROOT}" "${PKG_NAME}"

SIZE=$(du -h "${OUTPUT}" | awk '{print $1}')
SHA=$(sha256sum "${OUTPUT}" | awk '{print $1}' | cut -c1-16)

cat <<EOF

============================================================
✅ 客户端部署包已生成

  文件:   ${OUTPUT}
  大小:   ${SIZE}
  SHA256: ${SHA}...

分发到远端:
  scp "${OUTPUT}" user@REMOTE:/tmp/
  ssh user@REMOTE 'cd /tmp && tar -xzf "$(basename "${OUTPUT}")" && cd ${PKG_NAME} && sudo ./install.sh'

或使用一键部署脚本:
  ./deploy.sh --remote user@REMOTE
============================================================
EOF
