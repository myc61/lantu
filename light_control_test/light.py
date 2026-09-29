import socket
import sys

def Set_Light(ch:int,lumi:int):
    host='192.168.217.2'
    port=2000
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    #ch=1        #通道号：0/1
    #lumi=255    #亮度值：0-255
    check=0     #校验值
    data=bytearray([0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00])

    if lumi>255:
        lumi=255
    elif lumi<0:
        lumi=0

    if ch>2:
        ch=2
    elif ch<1:
        ch=1


    data[0]=0x24    #$
    data[1]=0x33    #3/4
    data[2]=ch+48   #1/2
    data[3]=0
    if (lumi//16)<10:
        data[4]=(lumi//16)+48
    else:
        data[4]=(lumi//16)+65

    if (lumi%16)<10:
        data[5]=(lumi%16)+48
    else:
        data[5]=(lumi%16)+65

    for i in data:
        check^=i
    data[6]=(check>>4)+48
    data[7]=(check&0x0F)+48
    
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