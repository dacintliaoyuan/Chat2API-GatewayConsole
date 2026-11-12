# udp_send.py — sends N UDP datagrams to host:port, optionally via connect().
# 'connect' exercises the ALE_AUTH_CONNECT path; otherwise plain sendto. Target
# is resolved with AI_NUMERICHOST so a proxy/DNS hook cannot break literal IPv6
# loopback sends. Exits 0 if at least one send call succeeded at the API level.
import socket
import sys

host, port, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
connect_mode = len(sys.argv) > 4 and sys.argv[4] == "connect"
family = socket.AF_INET6 if ":" in host else socket.AF_INET
try:
    infos = socket.getaddrinfo(host, port, family, socket.SOCK_DGRAM, 0,
                               socket.AI_NUMERICHOST)
    target = infos[0][4]
except OSError:
    sys.exit(1)
sent_ok = 0
for _ in range(count):
    try:
        s = socket.socket(family, socket.SOCK_DGRAM)
        if connect_mode:
            s.connect(target)
            s.send(b"x" * 32)
        else:
            s.sendto(b"x" * 32, target)
        sent_ok += 1
    except OSError:
        pass
    finally:
        try:
            s.close()
        except Exception:
            pass
sys.exit(0 if sent_ok > 0 else 1)
