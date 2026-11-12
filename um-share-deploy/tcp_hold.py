# tcp_hold.py — bind a TCP listener and hold it for N seconds (test seam to
# create a process that does NOT match the share identity, owned by the caller).
import socket
import sys
import time

host, port, seconds = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind((host, port))
s.listen(1)
s.settimeout(1)
deadline = time.time() + seconds
while time.time() < deadline:
    try:
        s.accept()
    except socket.timeout:
        pass
    except OSError:
        break
s.close()
