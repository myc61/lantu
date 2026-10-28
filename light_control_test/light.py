import socket
import sys

def to_hex(n):
    return n + 48 if n < 10 else n + 55

def Set_Light(ch:int,lumi:int):
    # 0：左手 192.168.217.2:2000
    # 1：右手 192.168.217.3:2001
    # 报文通道都是 1，亮度上限 230。帧格式与 skci/light_control_test/light.py 相同。
    if ch == 0:
        host = '192.168.217.2'
        port = 2000
    elif ch == 1:
        host = '192.168.217.3'
        port = 2001
    else:
        return 4
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    if lumi>230:
        lumi=230
    elif lumi<0:
        lumi=0

    data=bytearray(8)
    data[0]=0x24    #$
    data[1]=0x33    #3
    data[2]=49      #报文通道 1
    data[3]=0
    data[4]=to_hex(lumi >> 4)
    data[5]=to_hex(lumi & 0x0F)

    check=0
    for i in data[:6]:
        check^=i
    data[6]=to_hex(check >> 4)
    data[7]=to_hex(check & 0x0F)
    
    try:
        client_socket.connect((host, port))
        client_socket.sendall(data)
        return_data = client_socket.recv(1024)
        return 1
    except ConnectionRefusedError:
        return 2
    except socket.timeout:
        return 3
    except Exception as e:
        return 4

    client_socket.close()
    return 0

def main():
    arg1=int(sys.argv[1])
    arg2=int(sys.argv[2])

    Set_Light(arg1,arg2)

if __name__=="__main__":
    main()