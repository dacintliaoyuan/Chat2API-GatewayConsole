"""Keep-alive boundary: error and admin-202 responses must close the connection.

An error (or an admin 202) can be sent before the request body is read; on a
reused HTTP/1.1 socket the unread bytes would be parsed as the next request
line ("501 Unsupported method ({json}POST)"). These tests drive a real local
server on an ephemeral port with raw sockets: the error/202 response must
announce Connection: close, the reused socket must be closed without any HTTP
reply, and a fresh connection must keep working. Never touches UM, the token
store, or the production port.
"""

import importlib.util
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path

# Must be set before chat2api is imported: the module resolves the token file
# location from the environment at import time.
_TMP = tempfile.TemporaryDirectory(prefix="chat2api-keepalive-")
os.environ["CHAT2API_CONTROL_TOKEN_FILE"] = os.path.join(_TMP.name, "admin-control-token")

SPEC = importlib.util.spec_from_file_location(
    "chat2api", Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py")
chat2api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(chat2api)


class StubProxy:
    """Only the attributes the tested routes touch; no upstream calls."""

    def __init__(self):
        self.refresh_calls = []

    def auth_status(self):
        return {"state": "valid", "label": "认证有效", "error": None}

    def health_snapshot(self):
        return {"status": "ok"}

    def refresh(self, force=False, wait=False):
        self.refresh_calls.append((force, wait))

    def shutdown(self):
        pass


def _recv_response(sock):
    """Read one HTTP response (headers + Content-Length body) from a socket."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("peer closed before headers: %r" % buf[:120])
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    body = rest
    while len(body) < length:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("peer closed before body: %r" % body[:120])
        body += chunk
    return head, body


class KeepAliveCloseTests(unittest.TestCase):
    def setUp(self):
        self.proxy = StubProxy()
        self.server = chat2api.GatewayHTTPServer(("127.0.0.1", 0),
                                                chat2api.Handler)
        self.server.proxy = self.proxy
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.token = chat2api.ensure_control_token(chat2api.CONTROL_TOKEN_FILE)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    # ---------- helpers ----------

    def _connect(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        sock.settimeout(3)
        return sock

    def _request_bytes(self, method, path, body=b"", token=None):
        lines = ["%s %s HTTP/1.1" % (method, path),
                 "Host: 127.0.0.1:%d" % self.port]
        if token is not None:
            lines.append("X-Admin-Control: %s" % token)
        if body:
            lines.append("Content-Length: %d" % len(body))
        head = "\r\n".join(lines) + "\r\n\r\n"
        return head.encode() + body

    def _get(self, path):
        return self._request_bytes("GET", path)

    def assert_reuse_is_closed(self, sock):
        """A reuse attempt on this socket must get no HTTP reply at all.

        The keep-alive desync this suite guards against would show up as a
        real response (e.g. "501 Unsupported method ({json}GET)") parsed from
        leftover request bytes; a reset or EOF means the server closed it.
        """
        try:
            sock.sendall(self._get("/health"))
        except OSError:
            return  # send failed: peer already gone
        try:
            data = sock.recv(4096)
        except (ConnectionResetError, ConnectionAbortedError):
            return  # reset/abort: server closed the socket (Windows 10053)
        self.assertEqual(data, b"", "kept-alive socket answered: %r" % data[:120])

    def assert_health_on_fresh_connection(self):
        sock = self._connect()
        try:
            sock.sendall(self._get("/health"))
            head, body = _recv_response(sock)
        finally:
            sock.close()
        self.assertIn(b" 200", head.split(b"\r\n")[0])
        self.assertEqual(b'{"status": "ok"}', body)

    # ---------- the contract ----------

    def test_listen_backlog_is_128(self):
        # A burst of new connections must fit the listen queue instead of the
        # stdlib default of 5. The class attribute is read at bind time.
        self.assertEqual(chat2api.GatewayHTTPServer.request_queue_size, 128)
        self.assertEqual(self.server.request_queue_size, 128)

    def test_error_responses_close_connection(self):
        cases = [
            # (path, token, expected status): POST with an unread body must
            # not leave the connection reusable.
            ("/nowhere", None, b"404"),
            ("/api/version", None, b"403"),
            ("/admin/auth/login", "wrong-token", b"403"),
        ]
        for path, token, status in cases:
            with self.subTest(path=path):
                sock = self._connect()
                try:
                    body = b'{"junk":true}'
                    sock.sendall(self._request_bytes(
                        "POST", path, body=body, token=token))
                    head, _ = _recv_response(sock)
                    first_line = head.split(b"\r\n")[0]
                    self.assertIn(status, first_line)
                    self.assertIn(b"connection: close", head.lower())
                    self.assert_reuse_is_closed(sock)
                finally:
                    sock.close()
                # The refusal must not poison anything else: a brand new
                # connection still serves normal traffic.
                self.assert_health_on_fresh_connection()

    def test_admin_login_202_closes_connection(self):
        sock = self._connect()
        try:
            body = b'{"trigger":"probe"}'
            sock.sendall(self._request_bytes(
                "POST", "/admin/auth/login", body=body, token=self.token))
            head, _ = _recv_response(sock)
            self.assertIn(b" 202", head.split(b"\r\n")[0])
            self.assertIn(b"connection: close", head.lower())
            self.assertEqual([(True, False)], self.proxy.refresh_calls)
            self.assert_reuse_is_closed(sock)
        finally:
            sock.close()

    def test_admin_shutdown_202_announces_close(self):
        sock = self._connect()
        try:
            body = b'{"trigger":"probe"}'
            sock.sendall(self._request_bytes(
                "POST", "/admin/shutdown", body=body, token=self.token))
            head, _ = _recv_response(sock)
            self.assertIn(b" 202", head.split(b"\r\n")[0])
            self.assertIn(b"connection: close", head.lower())
        finally:
            sock.close()

    def test_success_keeps_connection_alive(self):
        # Over-closing guard: plain success responses keep HTTP/1.1 reuse.
        sock = self._connect()
        try:
            for _ in range(2):
                sock.sendall(self._get("/health"))
                head, body = _recv_response(sock)
                self.assertIn(b" 200", head.split(b"\r\n")[0])
                self.assertEqual(b'{"status": "ok"}', body)
                self.assertNotIn(b"connection: close", head.lower())
        finally:
            sock.close()


if __name__ == "__main__":
    unittest.main()
