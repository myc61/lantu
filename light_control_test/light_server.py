import socket
import json
import threading
from light import Set_Light

from typing import Any

# ===================== 配置参数 =====================
HOST = "0.0.0.0"      # 监听所有网卡，客户端用 172.16.8.136 连接即可
PORT = 9091           # 与客户端保持一致
CHANNEL_IDS = (1, 2)  # 支持的灯光通道
BUFFER_SIZE = 1024    # 单次接收缓冲区大小
CLIENT_TIMEOUT = 10.0 # 客户端连接超时，防止挂死

# ===================== 硬件控制接口 =====================
def set_light_hardware(ch_id: int, brightness: int) -> None:
    """
    真实灯光硬件控制入口，按需替换为实际调用
    （比如串口发指令、控制 GPIO、调用硬件 SDK 等）
    当前为模拟实现，仅打印日志
    """
    print(f"[硬件执行] 通道 {ch_id} → 亮度设置为 {brightness}")

# ===================== 消息业务处理 =====================
def handle_message(msg: dict[str, Any]) -> dict[str, Any]:
    """解析单条JSON消息，执行业务逻辑，返回响应结果"""
    try:
        action = msg.get("action")
        # 只支持 set_light 指令
        if action != "set_light":
            return {"status": "error", "reason": f"不支持的指令: {action}"}

        ch_id = msg.get("ch_id")
        brightness = msg.get("brightness")

        # 参数合法性校验
        if ch_id not in CHANNEL_IDS:
            return {"status": "error", "reason": f"通道号非法，仅支持 {CHANNEL_IDS}"}
        if not isinstance(brightness, int) or not 0 <= brightness <= 255:
            return {"status": "error", "reason": "亮度必须是 0-255 的整数"}

        # 执行硬件控制
        set_light_hardware(ch_id, brightness)
        return {"status": "ok", "ch_id": ch_id, "brightness": brightness}

    except Exception as e:
        return {"status": "error", "reason": f"处理异常: {str(e)}"}

# ===================== 单客户端连接处理 =====================
def handle_client(conn: socket.socket, addr: tuple[str, int]) -> None:
    """
    处理单个TCP客户端连接
    核心：解决TCP粘包问题，按 \n 分割完整JSON行
    """
    print(f"[新连接] 客户端 {addr[0]}:{addr[1]} 接入")
    buffer = b""  # 数据缓冲区，处理粘包

    try:
        conn.settimeout(CLIENT_TIMEOUT)

        while True:
            data = conn.recv(BUFFER_SIZE)
            if not data:  # 客户端主动关闭连接
                break

            buffer += data

            # 按换行符拆分出完整的消息行
            while b"\n" in buffer:
                line_bytes, buffer = buffer.split(b"\n", 1)
                if not line_bytes.strip():
                    continue  # 跳过空行

                # 解析JSON
                try:
                    msg = json.loads(line_bytes.decode("utf-8"))
                    print(f"[收到指令] 来自 {addr[0]}: {msg}")

                    # 处理消息
                    response = handle_message(msg)
                    print(f"[处理结果] {response}")

                    # --- 可选：向客户端回复响应 ---
                    # 如果你的客户端需要接收执行结果，打开下面两行注释
                    # resp_packet = json.dumps(response, ensure_ascii=False) + "\n"
                    # conn.sendall(resp_packet.encode("utf-8"))

                except json.JSONDecodeError:
                    print(f"[格式错误] 来自 {addr[0]} 的JSON解析失败")
                    # 也可以选择回错误包给客户端

    except socket.timeout:
        print(f"[超时] 客户端 {addr[0]}:{addr[1]} 连接超时")
    except Exception as e:
        print(f"[连接异常] 客户端 {addr[0]}:{addr[1]}: {e}")
    finally:
        conn.close()
        print(f"[连接断开] 客户端 {addr[0]}:{addr[1]} 已关闭\n")

# ===================== 主服务入口 =====================
def main() -> int:
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # 端口复用，重启服务时不会报"地址已占用"
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server_sock.bind((HOST, PORT))
        server_sock.listen(5)  # 最大挂起连接数
        print("=" * 50)
        print(f"灯光控制TCP服务启动成功")
        print(f"监听地址: {HOST}:{PORT}")
        print(f"支持通道: {CHANNEL_IDS}")
        print("=" * 50 + "\n")

        # 循环接收客户端连接
        while True:
            conn, addr = server_sock.accept()
            # 每个客户端开独立线程，支持多客户端同时接入
            client_thread = threading.Thread(
                target=handle_client,
                args=(conn, addr),
                daemon=True
            )
            client_thread.start()

    except KeyboardInterrupt:
        print("\n[停止服务] 收到退出信号")
    except OSError as e:
        print(f"[启动失败] {e}")
        return 1
    finally:
        server_sock.close()
        print("[服务结束] 套接字已释放")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
