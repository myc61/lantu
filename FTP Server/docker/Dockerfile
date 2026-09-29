# chassis_following FTP 测试容器
#
# 为什么不用 python:3.11-slim:
#   本机 Docker Hub 不可达 (registry-1.docker.io i/o timeout)，
#   只能复用内部仓库已缓存的镜像作为基础镜像。
#   demos:v1.0.2 = Ubuntu 20.04 + Python 3.8.10 + pip 20.0.2，
#   是本地镜像中体积最小且自带可用 pip 的一个 (content size 338MB)。
ARG BASE_IMAGE=10.51.33.201:30002/navi_project/demos:v1.0.2
FROM ${BASE_IMAGE}

# 覆盖 demos 镜像自带的 /etc/demos/entrypoint.sh
ENTRYPOINT []

# 离线安装 pyftpdlib。
# vendor/ 下是宿主机预先生成的 py3-none-any 纯 Python wheel，
# 版本与宿主机运行环境 (pyftpdlib 2.1.0) 严格一致 —— 这一点很重要，
# MountableFS 对 format_list / on_file_sent 的重写依赖 2.1.0 的具体行为，
# 换版本可能让虚拟挂载点在 LIST 中重新消失。
COPY vendor/ /tmp/vendor/
RUN python3 -m pip install --no-index --find-links=/tmp/vendor --no-cache-dir \
        "pyftpdlib==2.1.0" \
    && rm -rf /tmp/vendor \
    && python3 -c "import pyftpdlib; print('[build] pyftpdlib', pyftpdlib.__ver__)"

# 健康检查脚本 (仅依赖标准库 ftplib)
COPY healthcheck.py /opt/ftp/healthcheck.py

# 业务代码与数据全部由 compose 挂载到 /app，镜像不打包代码，
# 改完宿主机代码 docker restart 即可生效，无需重新构建。
WORKDIR /app/ftp

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FTP_CONFIG=/app/ftp/docker/server_config.docker.json \
    FTP_LOG=/app/ftp/logs/ftp_server_docker.log \
    FTP_EXTRA_ARGS=""

# 2121 = 控制通道; 60000-60100 = 被动模式数据通道 (ftp_server.py 内硬编码)
EXPOSE 2121 60000-60100

HEALTHCHECK --interval=30s --timeout=15s --start-period=15s --retries=3 \
    CMD ["python3", "/opt/ftp/healthcheck.py"]

# exec 让 python 成为 PID 1，直接收到 docker stop 发出的 SIGTERM，
# 走 ftp_server.py 里的 _graceful_exit 优雅关闭并输出流量汇总。
#
# 日志文件可写性预检: 容器降权到普通 uid 运行，如果 logs/ 里残留了
# 之前 root 容器创建的同名日志，FileHandler 会直接 PermissionError 把服务打死，
# 叠加 restart: unless-stopped 就是无限重启循环。这里探测失败就丢掉 --log-file，
# setup_logging 的 stdout handler 仍然在，docker logs 照样能看全量日志。
CMD ["sh", "-c", "if [ -n \"$FTP_LOG\" ] && touch \"$FTP_LOG\" 2>/dev/null; then LOGARG=\"--log-file $FTP_LOG\"; else echo '[WARN] 日志文件 '\"$FTP_LOG\"' 不可写(可能是 root 属主残留)，降级为仅 stdout' >&2; LOGARG=''; fi; exec python3 /app/ftp/ftp_server.py --config \"$FTP_CONFIG\" $LOGARG $FTP_EXTRA_ARGS"]
