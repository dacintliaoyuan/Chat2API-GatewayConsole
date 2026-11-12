# udp_receiver.py — real UDP receiver for the egress-comparison test.
# Binds the given host:port, collects datagrams for <seconds>, then writes the
# received count to the log path. Prints nothing else.
import select
import socket
import sys
import time

host, port, seconds, logpath = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
family = socket.AF_INET6 if ":" in host else socket.AF_INET
# resolve a numeric literal DIRECTLY (AI_NUMERICHOST) - the system resolver may
# be intercepted (proxy fake-IP) which would otherwise break bind for '::1'
try:
    infos = socket.getaddrinfo(host, port, family, socket.SOCK_DGRAM, 0,
                               socket.AI_NUMERICHOST)
    bindaddr = infos[0][4]
except OSError as exc:
    with open(logpath, "w", encoding="utf-8") as f:
        f.write("bind-error:%s" % exc)
    raise SystemExit(1)
s = socket.socket(family, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(bindaddr)
except OSError as exc:
    with open(logpath, "w", encoding="utf-8") as f:
        f.write("bind-error:%s" % exc)
    raise SystemExit(1)
count = 0
deadline = time.time() + seconds
while time.time() < deadline:
    ready, _, _ = select.select([s], [], [], 0.3)
    if ready:
        try:
            s.recvfrom(4096)
            count += 1
        except OSError:
            pass
with open(logpath, "w", encoding="utf-8") as f:
    f.write(str(count))
s.close()

