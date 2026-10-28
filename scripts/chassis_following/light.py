# -*- coding: utf-8 -*-
"""光源控制：TCP 发 ASCII 帧到光源控制器。

与 skci/light_control_test/light.py 同一帧格式。
服务通道 0 是左手灯，192.168.217.2:2000。
服务通道 1 是右手灯，192.168.217.3:2001。
两台控制器的报文通道都是 1。亮度超过 230 按 230 发。
"""
import socket
from datetime import datetime

import rospy
from chassis_following.runlog import log

BRIGHTNESS_MAX = 230


def to_hex(n):
    """0~15 → ASCII '0'~'9'、'A'~'F'。"""
    n = int(n)
    return n + 48 if n < 10 else n + 55


def encode_frame(brightness):
    """$ 3 '1' 0 hi lo chk_hi chk_lo。校验=前 6 字节异或。报文通道固定为 1。"""
    lumi = int(brightness)
    if lumi > BRIGHTNESS_MAX:
        lumi = BRIGHTNESS_MAX
    elif lumi < 0:
        lumi = 0

    data = bytearray(8)
    data[0] = 0x24
    data[1] = 0x33
    data[2] = 49
    data[3] = 0
    data[4] = to_hex(lumi >> 4)
    data[5] = to_hex(lumi & 0x0F)
    check = 0
    for b in data[:6]:
        check ^= b
    data[6] = to_hex(check >> 4)
    data[7] = to_hex(check & 0x0F)
    return bytes(data), lumi


class LightController(object):
    def __init__(self, host=None, port=None, timeout=None, retries=None):
        left_host = rospy.get_param("~light_left_host", "")
        right_host = rospy.get_param("~light_right_host", "")
        fallback_host = (host if host is not None else rospy.get_param(
            "~light_host", "192.168.217.2")).strip()
        fallback_port = int(port if port is not None else rospy.get_param(
            "~light_port", 2000))
        self.left_host = (left_host or fallback_host).strip()
        self.left_port = int(rospy.get_param("~light_left_port", fallback_port))
        self.right_host = (right_host or "192.168.217.3").strip()
        self.right_port = int(rospy.get_param("~light_right_port", 2001))
        self.timeout = float(timeout if timeout is not None else rospy.get_param(
            "~light_timeout", 3.0))
        self.retries = int(retries if retries is not None else rospy.get_param(
            "~light_retries", 3))
        log.info(
            "[light] 左手灯 %s:%d；右手灯 %s:%d；报文通道都是 1，亮度上限 %d；"
            "timeout=%.1fs retries=%d",
            self.left_host, self.left_port, self.right_host, self.right_port,
            BRIGHTNESS_MAX, self.timeout, self.retries)

    def _target(self, channel):
        """服务通道 0 左手，1 右手。报文通道都由 encode_frame 写成 1。"""
        if int(channel) == 0:
            return self.left_host, self.left_port, "左手"
        return self.right_host, self.right_port, "右手"

    def set_light(self, channel, brightness, received_at=None):
        ch = int(channel)
        lumi = int(brightness)
        if ch < 0 or ch > 1:
            return False, "channel 只支持 0（左手）/1（右手）"
        if lumi < 0 or lumi > 255:
            return False, "brightness 只支持 0-255"
        host, port, side = self._target(ch)
        frame, sent = encode_frame(lumi)
        last_err = None
        for _ in range(max(1, self.retries)):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(self.timeout)
                sock.connect((host, port))
                sock.sendall(frame)
                sent_at = datetime.now()
                if received_at is None:
                    gap = ""
                else:
                    gap = " 距收到服务 %.0fms" % (
                        (sent_at - received_at).total_seconds() * 1000.0)
                log.info(
                    "[light] TCP已发送 %s%s %s 服务通道=%d %s:%d "
                    "报文通道=1 brightness=%d",
                    sent_at.strftime("%H:%M:%S.%f")[:-3], gap,
                    side, ch, host, port, sent)
                return True, "设置成功"
            except ConnectionRefusedError:
                last_err = "连接被拒绝"
            except socket.timeout:
                last_err = "连接超时"
            except Exception as e:
                last_err = str(e)
            finally:
                sock.close()
        log.error(
            "[light] %s 服务通道=%d brightness=%d 失败 %s:%d: %s",
            side, ch, lumi, host, port, last_err)
        return False, "设置失败: %s" % last_err
