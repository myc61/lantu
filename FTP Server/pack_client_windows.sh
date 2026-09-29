#!/usr/bin/env bash
# 打包 chassis_following FTP 压测客户端 (Windows 版) 为 zip。
#
# 产物: dist/ftp-stress-client-windows-YYYYmmdd-HHMM.zip
# 该 zip 可直接拷到 Windows 机器解压后运行 install.ps1。
#
# 用法:
#   ./pack_client_windows.sh
#   ./pack_client_windows.sh --output /path/to/pkg.zip
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WIN_SRC="${SCRIPT_DIR}/client_windows"
DIST_DIR="${SCRIPT_DIR}/dist"
STAMP="$(date +%Y%m%d-%H%M)"
PKG_NAME="ftp-stress-client-windows"
OUTPUT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output|-o) OUTPUT="$2"; shift 2 ;;
        -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

mkdir -p "${DIST_DIR}"
[[ -z "${OUTPUT}" ]] && OUTPUT="${DIST_DIR}/${PKG_NAME}-${STAMP}.zip"

# 校验必需文件
REQUIRED=(
    "${SCRIPT_DIR}/stress_test.py"
    "${WIN_SRC}/install.ps1"
    "${WIN_SRC}/uninstall.ps1"
    "${WIN_SRC}/client_config.json"
    "${WIN_SRC}/run_download.bat"
    "${WIN_SRC}/run_upload.bat"
    "${WIN_SRC}/run_both.bat"
    "${WIN_SRC}/run_fetch.bat"
    "${WIN_SRC}/README_Windows.md"
)
for f in "${REQUIRED[@]}"; do
    if [[ ! -f "${f}" ]]; then
        echo "[ERROR] 缺少文件: ${f}"
        exit 2
    fi
done

# 使用 Python zipfile 打包 (跨平台, 无需 zip 命令; 保证 CRLF + UTF-8)
OUTPUT="${OUTPUT}" PKG_NAME="${PKG_NAME}" SCRIPT_DIR="${SCRIPT_DIR}" WIN_SRC="${WIN_SRC}" \
python3 - <<'PYEOF'
import os
import zipfile

out = os.environ["OUTPUT"]
pkg = os.environ["PKG_NAME"]
script_dir = os.environ["SCRIPT_DIR"]
win_src = os.environ["WIN_SRC"]

# (源文件, zip 内相对路径)
items = [
    (os.path.join(script_dir, "stress_test.py"), f"{pkg}/stress_test.py"),
    (os.path.join(win_src, "install.ps1"),        f"{pkg}/install.ps1"),
    (os.path.join(win_src, "uninstall.ps1"),      f"{pkg}/uninstall.ps1"),
    (os.path.join(win_src, "client_config.json"), f"{pkg}/client_config.json"),
    (os.path.join(win_src, "run_download.bat"),   f"{pkg}/run_download.bat"),
    (os.path.join(win_src, "run_upload.bat"),     f"{pkg}/run_upload.bat"),
    (os.path.join(win_src, "run_both.bat"),       f"{pkg}/run_both.bat"),
    (os.path.join(win_src, "run_fetch.bat"),      f"{pkg}/run_fetch.bat"),
    (os.path.join(win_src, "README_Windows.md"),  f"{pkg}/README_Windows.md"),
]

def to_crlf(data: bytes) -> bytes:
    """Windows 脚本统一 CRLF 换行, 避免记事本/PowerShell 异常。"""
    data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return data.replace(b"\n", b"\r\n")

CRLF_EXTS = (".bat", ".ps1", ".json", ".md", ".py", ".txt")

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for src, arc in items:
        with open(src, "rb") as f:
            data = f.read()
        if arc.endswith(CRLF_EXTS):
            data = to_crlf(data)
        # 去掉 zip 内 UTF-8 文件名问题: 强制 UTF-8 flag
        zi = zipfile.ZipInfo(arc)
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.flag_bits |= 0x800  # UTF-8 filename
        zi.external_attr = 0o644 << 16
        z.writestr(zi, data)

print(f"[OK] 已写入 {len(items)} 个文件")
PYEOF

SIZE=$(du -h "${OUTPUT}" | awk '{print $1}')
SHA=$(sha256sum "${OUTPUT}" | awk '{print $1}' | cut -c1-16)

cat <<EOF

============================================================
✅ Windows 客户端部署包已生成

  文件:   ${OUTPUT}
  大小:   ${SIZE}
  SHA256: ${SHA}...

Windows 端使用:
  1. 把 zip 拷到远端 (172.16.9.173), 解压
  2. 管理员 PowerShell:
       cd 解压目录
       Set-ExecutionPolicy -Scope Process Bypass -Force
       .\\install.ps1
============================================================
EOF
