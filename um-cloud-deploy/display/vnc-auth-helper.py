#!/usr/bin/env python3
"""Narrow VNC challenge-response helper for the loopback display service.

The display service already holds the root-only VNC credential through systemd
``LoadCredential``.  This helper reads that credential, decrypts the 8-byte VNC
key, and answers only the 16-byte RFB challenge on a private Unix socket.  The
console proxy never reads the password; it sends a challenge and receives the
16-byte response.  The password never crosses this socket, the process table,
stdout/stderr, a URL or a log.
"""
import os
import signal
import socket
import stat
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from console.display_proxy import load_vnc_password, vnc_auth_response

MAX_CONNECTIONS = 4
CONNECTION_TIMEOUT = 5.0


def _recv_exact(conn, size):
    data = bytearray()
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise EOFError("peer closed")
        data.extend(chunk)
    return bytes(data)


def handle_connection(conn, password):
    try:
        conn.settimeout(CONNECTION_TIMEOUT)
        challenge = _recv_exact(conn, 16)
        conn.sendall(vnc_auth_response(password, challenge))
    except Exception:  # noqa: BLE001  never log a challenge or response
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _unlink_socket(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode):
        raise SystemExit("refusing to replace a non-socket path")
    os.unlink(path)


def serve(password, socket_path):
    _unlink_socket(socket_path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    os.chmod(socket_path, 0o660)
    server.listen(MAX_CONNECTIONS)
    server.settimeout(0.5)
    stopping = threading.Event()

    def stop(_signum, _frame):
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=handle_connection, args=(conn, password),
                daemon=True).start()
    finally:
        server.close()
        try:
            os.unlink(socket_path)
        except OSError:
            pass


def main(argv):
    password_path = argv[1] if len(argv) > 1 else os.environ.get(
        "UM_VNC_AUTH_PATH", "")
    socket_path = argv[2] if len(argv) > 2 else os.environ.get(
        "UM_VNC_AUTH_SOCKET", "/run/um-share/vnc-auth.sock")
    if not password_path or not socket_path:
        print("vnc-auth-helper: credential and socket path are required",
              file=sys.stderr)
        return 2
    try:
        password = load_vnc_password(password_path)
    except Exception:
        print("vnc-auth-helper: credential unavailable", file=sys.stderr)
        return 1
    try:
        serve(password, socket_path)
    except Exception:
        print("vnc-auth-helper: socket unavailable", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
