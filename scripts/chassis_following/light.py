# -*- coding: utf-8 -*-
"""光源控制：TCP 发 ASCII 帧到光源控制器。

默认 192.168.217.2:2000。通道 1/2，亮度 0-255。
服务端可用 timeout/retries 覆盖默认值。
"""
import socket

import rospy
from chassis_following.runlog import log


def encode_frame(channel, brightness):
    """$ 3 ch 0 hi lo chk_hi chk_lo。校验=前6字节异或，拆成两个 ASCII。"""
    ch = int(channel)
    if ch > 2:
        ch = 2
    elif ch < 1:
        ch = 1
    lumi = int(brightness)
    if lumi > 255:
        lumi = 255
    elif lumi < 0:
        lumi = 0

    data = bytearray([0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
    data[0] = 0x24
    data[1] = 0x33
    data[2] = ch + 48
    data[3] = 0
    data[4] = (lumi // 16) + 48 if (lumi // 16) < 10 else (lumi // 16) + 65
    data[5] = (lumi % 16) + 48 if (lumi % 16) < 10 else (lumi % 16) + 65
    check = 0
    for b in data[:6]:
        check ^= b
    data[6] = (check >> 4) + 48
    data[7] = (check & 0x0F) + 48
    return bytes(data)


class LightController(object):
    def __init__(self, host=None, port=None, timeout=None, retries=None):
        self.host = (host if host is not None else rospy.get_param(
            "~light_host", "192.168.217.2")).strip()
        self.port = int(port if port is not None else rospy.get_param(
            "~light_port", 2000))
        self.timeout = float(timeout if timeout is not None else rospy.get_param(
            "~light_timeout", 3.0))
        self.retries = int(retries if retries is not None else rospy.get_param(
            "~light_retries", 3))
        log.info(
            "[light] 光源控制器 %s:%d timeout=%.1fs retries=%d",
            self.host, self.port, self.timeout, self.retries)

    def set_light(self, channel, brightness):
        ch = int(channel)
        lumi = int(brightness)
        if ch < 1 or ch > 2:
            return False, "channel 只支持 1/2"
        if lumi < 0 or lumi > 255:
            return False, "brightness 只支持 0-255"
        frame = encode_frame(ch, lumi)
        last_err = None
        for _ in range(max(1, self.retries)):
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(self.timeout)
                sock.connect((self.host, self.port))
                sock.sendall(frame)
                try:
                    sock.recv(1024)
                except socket.timeout:
                    pass
                log.info("[light] ch=%d brightness=%d ok", ch, lumi)
                return True, "设置成功"
            except ConnectionRefusedError:
                last_err = "连接被拒绝"
            except socket.timeout:
                last_err = "连接超时"
            except Exception as e:
                last_err = str(e)
        log.error(
            "[light] ch=%d brightness=%d 失败 %s:%d: %s",
            ch, lumi, self.host, self.port, last_err)
        return False, "设置失败: %s" % last_err
