"""Private noVNC surface and WebSocket-to-RFB relay tests."""
import base64
import contextlib
import importlib.util
import io
import json
import os
import socket
import socketserver
import stat
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from html.parser import HTMLParser
from unittest import mock
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from console import display_proxy as display
from console import tailnet as console_tailnet

DISPLAY_DIR = ROOT / "um-cloud-deploy" / "display"
LAUNCHER = DISPLAY_DIR / "novnc-launcher.html"
LAUNCHER_SCRIPT = DISPLAY_DIR / "novnc-launcher.js"


class _ScriptCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.tags.append(dict(attrs))


def script_tags(document):
    """Return every <script> start tag as an attribute dict."""
    collector = _ScriptCollector()
    collector.feed(document)
    collector.close()
    return collector.tags

DOCUMENT = {
    "schema_version": 1, "enabled": True,
    "origin": "https://pc.example.ts.net:8001",
    "allowed_login": "owner@example.com",
    "allowed_peer_ips": ["100.119.187.73", "100.127.119.98"],
}

REMOTE_HEADERS = {
    "Host": "pc.example.ts.net:8001",
    "X-Forwarded-Host": "pc.example.ts.net:8001",
    "X-Forwarded-Proto": "https",
    "X-Forwarded-For": "100.127.119.98",
    "Tailscale-User-Login": "owner@example.com",
    "Cookie": "usage_session=good",
}


def make_config(**changes):
    values = {
        "enabled": True,
        "vnc_host": "127.0.0.1",
        "vnc_port": 5901,
        "novnc_root": str(DISPLAY_DIR),
        "launcher_file": str(LAUNCHER),
        "max_connections": 2,
        "idle_timeout": 60.0,
        "max_session_seconds": 600.0,
        "handshake_timeout": 3.0,
        "relay_join_timeout": 2.0,
    }
    values.update(changes)
    return display.DisplayConfig(**values)


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    proxy = None
    tailnet_policy = None

    def do_GET(self):
        if not self.proxy.handle_get(
                self, tailnet_policy=self.tailnet_policy,
                console_port=self.server.server_port):
            self.send_error(404)

    def do_POST(self):
        if not self.proxy.handle_post(
                self, tailnet_policy=self.tailnet_policy,
                console_port=self.server.server_port):
            self.send_error(404)

    def log_message(self, fmt, *args):
        pass


@contextlib.contextmanager
def proxy_server(config, *, policy=None, session_valid=None,
                 control_token_ok=None, auth_provider=...):
    proxy = display.DisplayProxy(
        config,
        session_valid=session_valid or (lambda cookie: cookie == "usage_session=good"),
        control_token_ok=control_token_ok or (lambda token: token == "token"),
        auth_provider=display._AUTH_UNSET if auth_provider is ... else auth_provider)
    handler = type("DisplayHandler", (_ProxyHandler,), {
        "proxy": proxy, "tailnet_policy": policy})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield proxy, server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def http_get(port, path, headers=None):
    values = {"Cookie": "usage_session=good"}
    values.update(headers or {})
    conn = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request("GET", path, headers=values)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _recv_exact(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise EOFError("socket closed")
        data += chunk
    return data


def _client_frame(opcode, payload):
    mask = b"\x11\x22\x33\x44"
    header = bytearray((0x80 | opcode,))
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length <= 0xFFFF:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + mask + masked


def _server_frame(sock):
    first, second = _recv_exact(sock, 2)
    opcode = first & 0x0F
    length = second & 0x7F
    if second & 0x80:
        raise AssertionError("server frame is masked")
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    return opcode, _recv_exact(sock, length)


def _read_greeting(sock, server_init):
    """Read the greeting and return (version, security+server init).

    noVNC 1.6 consumes only the 12-byte protocol version from the first
    WebSocket message, so the version must arrive as the first message on its own.
    """
    opcode, version = _server_frame(sock)
    if opcode != 0x02:
        raise AssertionError(f"greeting opcode {opcode:#x}")
    if len(version) != 12:
        raise AssertionError(
            f"server version frame is {len(version)} bytes, expected 12")
    remaining = 2 + 4 + len(server_init)
    rest = b""
    frames = 1
    while len(rest) < remaining:
        opcode, payload = _server_frame(sock)
        if opcode != 0x02:
            raise AssertionError(f"greeting opcode {opcode:#x}")
        rest += payload
        frames += 1
        if frames > 4:
            raise AssertionError("greeting split into more than four messages")
    if len(rest) != remaining:
        raise AssertionError("greeting frames overran the greeting")
    return version, rest


def _server_close(sock):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        opcode, payload = _server_frame(sock)
        if opcode == 0x08:
            return payload
    raise AssertionError("no close frame")


def _read_headers(sock):
    # Read one byte at a time: the 101 and the first WebSocket binary frame
    # can share a TCP segment, and a chunked read would swallow the frame.
    data = b""
    while not data.endswith(b"\r\n\r\n"):
        chunk = sock.recv(1)
        if not chunk:
            break
        data += chunk
    return data.decode("latin-1")


def _read_http_response(sock):
    """Read headers and a bounded Content-Length body from a plain response."""
    headers = _read_headers(sock)
    length = 0
    for line in headers.split("\r\n"):
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-length":
            length = int(value.strip())
    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            break
        body += chunk
    return headers, body


def _ws_connect(port, path="/display/websockify", **overrides):
    values = {
        "Host": f"127.0.0.1:{port}",
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Version": "13",
        "Sec-WebSocket-Key": base64.b64encode(b"0123456789abcdef").decode(),
        "Sec-WebSocket-Protocol": "binary",
        "Origin": f"http://127.0.0.1:{port}",
        "Cookie": "usage_session=good",
    }
    values.update(overrides)
    lines = [f"GET {path} HTTP/1.1"]
    for name, value in values.items():
        if value is not None:
            lines.append(f"{name}: {value}")
    lines.extend(("", ""))
    sock = socket.create_connection(("127.0.0.1", port), timeout=3)
    sock.sendall("\r\n".join(lines).encode("latin-1"))
    return sock, values["Sec-WebSocket-Key"]


def _display_session(proxy, origin, subject="control-token"):
    """Create one valid display session for an origin."""
    token, _ = proxy.grants.issue(subject, origin)
    return proxy.grants.redeem(token, origin)


def _operator_ws(proxy, port, *, origin=None, session=None,
                 action="acquire", **overrides):
    """Connect as the current operator of one display session.

    The default session is cached per proxy and origin so a second connection
    from the same page reuses its generation instead of acquiring a new one.
    """
    origin = origin or f"http://127.0.0.1:{port}"
    if session is None:
        cached = getattr(proxy, "_test_operator", None)
        if cached is None or cached[0] != origin:
            session = _display_session(proxy, origin)
            acquired = proxy.grants.acquire_operator(session.token, origin)
            if not acquired.get("acquired"):
                raise AssertionError(acquired)
            proxy._test_operator = (origin, session, acquired["generation"])
        else:
            session = cached[1]
    acquired = proxy.grants.acquire_operator(session.token, origin)
    if not acquired.get("acquired"):
        raise AssertionError(acquired)
    return _ws_for_session(
        port, session, acquired["generation"], **overrides)


def _ws_for_session(port, session, generation, **overrides):
    values = {
        "path": f"/display/websockify?operator={generation}",
        "Cookie": f"{display.DISPLAY_SESSION_COOKIE}={session.token}",
    }
    values.update(overrides)
    return _ws_connect(port, **values)


class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self):
        while True:
            data = self.request.recv(65536)
            if not data:
                return
            if data == b"quit":
                self.request.close()
                return
            self.request.sendall(data)


class _EchoServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _SplitWriteClient:
    """A client socket that splits every frame into two partial writes.

    ``sendall`` is allowed to issue several ``send`` calls; a relay that does not
    serialise whole frames lets another thread's frame land between the halves.
    """

    def __init__(self):
        self.chunks = []
        self._lock = threading.Lock()

    def gettimeout(self):
        return None

    def settimeout(self, _value):
        pass

    def sendall(self, data):
        half = max(1, len(data) // 2)
        for part in (data[:half], data[half:]):
            with self._lock:
                self.chunks.append(part)
            time.sleep(0.002)

    def shutdown(self, _how):
        pass

    def close(self):
        pass


class _FakeUpstream:
    def __init__(self):
        self.timeout = 3.0

    def settimeout(self, value):
        self.timeout = value

    def makefile(self, _mode):
        return io.BytesIO(b"")

    def shutdown(self, _how):
        pass

    def close(self):
        pass


def _server_frames(data):
    """Parse a server-to-client byte stream of binary frames."""
    frames, offset = [], 0
    while offset < len(data):
        first, second = data[offset], data[offset + 1]
        if first != 0x82 or second & 0x80:
            raise AssertionError("malformed server frame")
        length = second
        offset += 2
        if length == 126:
            length = struct.unpack("!H", data[offset:offset + 2])[0]
            offset += 2
        elif length == 127:
            length = struct.unpack("!Q", data[offset:offset + 8])[0]
            offset += 8
        frames.append(data[offset:offset + length])
        offset += length
    return frames


class DisplayConfigTests(unittest.TestCase):
    def test_default_is_disabled(self):
        config = display.load_config({})
        self.assertFalse(config.enabled)
        self.assertEqual(config.error, "")

    def test_valid_config_is_enabled(self):
        config = display.load_config({
            "CONSOLE_DISPLAY_ENABLED": "1",
            "CONSOLE_DISPLAY_NOVNC_ROOT": str(DISPLAY_DIR),
            "CONSOLE_DISPLAY_LAUNCHER_FILE": str(LAUNCHER),
        })
        self.assertTrue(config.enabled)
        self.assertEqual(config.vnc_host, "127.0.0.1")
        self.assertEqual(config.vnc_port, 5901)
        self.assertEqual(config.vnc_auth_socket, "/run/um-share/vnc-auth.sock")
        self.assertEqual(config.vnc_auth_file, "")

    def test_auth_socket_and_file_are_exclusive(self):
        for change in (
            {"CONSOLE_DISPLAY_VNC_AUTH_SOCKET": "relative.sock"},
            {"CONSOLE_DISPLAY_VNC_AUTH_SOCKET": "/run/a.sock",
             "CONSOLE_DISPLAY_VNC_AUTH_FILE": "/run/a.passwd"},
        ):
            with self.subTest(change=change):
                values = {
                    "CONSOLE_DISPLAY_ENABLED": "1",
                    "CONSOLE_DISPLAY_NOVNC_ROOT": str(DISPLAY_DIR),
                    "CONSOLE_DISPLAY_LAUNCHER_FILE": str(LAUNCHER),
                }
                values.update(change)
                config = display.load_config(values)
                self.assertFalse(config.enabled)
                self.assertEqual(config.error, "display_config_invalid")

    def test_missing_auth_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            config = display.load_config({
                "CONSOLE_DISPLAY_ENABLED": "1",
                "CONSOLE_DISPLAY_NOVNC_ROOT": str(DISPLAY_DIR),
                "CONSOLE_DISPLAY_LAUNCHER_FILE": str(LAUNCHER),
                "CONSOLE_DISPLAY_VNC_AUTH_FILE": str(
                    Path(td) / "missing.passwd"),
            })
            self.assertFalse(config.enabled)
            self.assertEqual(config.error, "display_vnc_auth_missing")

    def test_non_loopback_vnc_host_fails_closed(self):
        for host in ("0.0.0.0", "10.0.0.1", "::", "example.test"):
            with self.subTest(host=host):
                config = display.load_config({
                    "CONSOLE_DISPLAY_ENABLED": "1",
                    "CONSOLE_DISPLAY_VNC_HOST": host,
                    "CONSOLE_DISPLAY_NOVNC_ROOT": str(DISPLAY_DIR),
                    "CONSOLE_DISPLAY_LAUNCHER_FILE": str(LAUNCHER),
                })
                self.assertFalse(config.enabled)
                self.assertEqual(config.error, "display_config_invalid")

    def test_bad_numbers_and_missing_assets_fail_closed(self):
        for change in (
            {"CONSOLE_DISPLAY_VNC_PORT": "0"},
            {"CONSOLE_DISPLAY_MAX_CONNECTIONS": "9"},
            {"CONSOLE_DISPLAY_IDLE_TIMEOUT": "1"},
            {"CONSOLE_DISPLAY_MAX_SESSION": "999999"},
        ):
            with self.subTest(change=change):
                values = {
                    "CONSOLE_DISPLAY_ENABLED": "1",
                    "CONSOLE_DISPLAY_NOVNC_ROOT": str(DISPLAY_DIR),
                    "CONSOLE_DISPLAY_LAUNCHER_FILE": str(LAUNCHER),
                }
                values.update(change)
                config = display.load_config(values)
                self.assertFalse(config.enabled)
                self.assertEqual(config.error, "display_config_invalid")
        with tempfile.TemporaryDirectory() as td:
            config = display.load_config({
                "CONSOLE_DISPLAY_ENABLED": "1",
                "CONSOLE_DISPLAY_NOVNC_ROOT": str(Path(td) / "missing"),
                "CONSOLE_DISPLAY_LAUNCHER_FILE": str(LAUNCHER),
            })
            self.assertFalse(config.enabled)
            self.assertEqual(config.error, "display_novnc_missing")

    def test_missing_launcher_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            config = display.load_config({
                "CONSOLE_DISPLAY_ENABLED": "1",
                "CONSOLE_DISPLAY_NOVNC_ROOT": td,
                "CONSOLE_DISPLAY_LAUNCHER_FILE": str(Path(td) / "missing.html"),
            })
            self.assertFalse(config.enabled)
            self.assertEqual(config.error, "display_launcher_missing")


class DisplayStaticTests(unittest.TestCase):
    def test_launcher_html_only_loads_an_external_module_script(self):
        """The page CSP is script-src 'self'; inline script never executes."""
        body = LAUNCHER.read_text(encoding="utf-8")
        self.assertEqual(
            [(tag.get("type"), tag.get("src")) for tag in script_tags(body)],
            [("module", "/display/launcher.js")])
        self.assertNotIn("location.host", body)
        self.assertNotIn("127.0.0.1", body)
        self.assertNotIn("localhost", body)

    def test_launcher_script_keeps_the_same_origin_endpoints(self):
        body = LAUNCHER_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("location.host", body)
        self.assertIn("/display/websockify", body)
        self.assertIn("./core/rfb.js", body)
        self.assertNotIn("127.0.0.1", body)
        self.assertNotIn("localhost", body)

    def test_launcher_script_is_served_from_the_launcher_directory(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "novnc"
            (root / "core").mkdir(parents=True)
            (root / "core" / "rfb.js").write_text(
                "export default class RFB {}\n", encoding="utf-8")
            # A same-named file inside the noVNC tree must not shadow the
            # launcher's own module script.
            (root / "launcher.js").write_text(
                "/* decoy */\n", encoding="utf-8")
            launcher = base / "novnc-launcher.html"
            launcher.write_text("<!doctype html><title>display</title>",
                                encoding="utf-8")
            (base / "novnc-launcher.js").write_bytes(
                b"const launcher = 1;\n")
            with proxy_server(make_config(
                    novnc_root=str(root),
                    launcher_file=str(launcher))) as (_, port):
                status, headers, body = http_get(port, "/display/launcher.js")
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"],
                                 "text/javascript; charset=utf-8")
                self.assertEqual(body, b"const launcher = 1;\n")
                csp = headers["Content-Security-Policy"]
                self.assertIn("script-src 'self'", csp)
                self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)

    def test_shipped_launcher_script_is_served(self):
        with proxy_server(make_config()) as (_, port):
            status, headers, body = http_get(port, "/display/launcher.js")
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"],
                             "text/javascript; charset=utf-8")
            self.assertIn(b"/display/websockify", body)

    def test_missing_launcher_script_is_404(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "novnc"
            root.mkdir()
            launcher = base / "novnc-launcher.html"
            launcher.write_text("<!doctype html><title>display</title>",
                                encoding="utf-8")
            with proxy_server(make_config(
                    novnc_root=str(root),
                    launcher_file=str(launcher))) as (_, port):
                self.assertEqual(http_get(port, "/display/launcher.js")[0], 404)
                self.assertEqual(
                    http_get(port, "/display/launcher-state.js")[0], 404)

    def test_launcher_state_script_is_served_from_the_launcher_directory(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "novnc"
            root.mkdir()
            launcher = base / "novnc-launcher.html"
            launcher.write_text("<!doctype html><title>display</title>",
                                encoding="utf-8")
            # A same-named file inside the noVNC tree must not shadow the
            # launcher's own state module.
            (root / "launcher-state.js").write_text(
                "/* decoy */\n", encoding="utf-8")
            (base / "novnc-launcher-state.mjs").write_text(
                "export function displayLauncherState() {}\n", encoding="utf-8")
            with proxy_server(make_config(
                    novnc_root=str(root),
                    launcher_file=str(launcher))) as (_, port):
                status, headers, body = http_get(
                    port, "/display/launcher-state.js")
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"],
                                 "text/javascript; charset=utf-8")
                self.assertIn(b"displayLauncherState", body)
                self.assertEqual(
                    http_get(port, "/display/launcher-state.js?x=1")[0], 400)

    def test_shipped_launcher_state_script_is_served(self):
        with proxy_server(make_config()) as (_, port):
            status, headers, body = http_get(
                port, "/display/launcher-state.js")
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"],
                             "text/javascript; charset=utf-8")
            self.assertIn(b"displayLauncherState", body)

    def test_static_assets_and_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "core").mkdir()
            (root / "core" / "rfb.js").write_text(
                "export default class RFB {}\n", encoding="utf-8")
            (root / "package.json").write_text("{}\n", encoding="utf-8")
            with proxy_server(make_config(novnc_root=str(root))) as (_, port):
                status, headers, body = http_get(port, "/display")
                self.assertEqual(status, 308)
                self.assertEqual(headers["Location"], "/display/")

                status, headers, body = http_get(port, "/display/")
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"],
                                 "text/html; charset=utf-8")
                csp = headers["Content-Security-Policy"]
                self.assertIn("default-src 'none'", csp)
                self.assertIn("script-src 'self'", csp)
                self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)
                self.assertIn(b"/display/launcher.js", body)

                status, headers, _ = http_get(port, "/display/core/rfb.js")
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"],
                                 "text/javascript; charset=utf-8")

                for path in ("/display/package.json", "/display/../secret",
                             "/display/%2e%2e/secret", "/display//core/rfb.js"):
                    with self.subTest(path=path):
                        self.assertEqual(http_get(port, path)[0], 404)

                self.assertEqual(http_get(port, "/display/?password=secret")[0],
                                 400)
                self.assertEqual(http_get(port, "/display/?path=evil")[0], 400)

    def test_local_origin_session_and_control_token(self):
        with proxy_server(make_config()) as (_, port):
            self.assertEqual(http_get(port, "/display/")[0], 200)
            self.assertEqual(http_get(port, "/display/", {
                "Origin": f"http://127.0.0.1:{port}"})[0], 200)
            self.assertEqual(http_get(port, "/display/", {
                "Host": f"localhost:{port}"})[0], 200)
            self.assertEqual(http_get(port, "/display/", {
                "Origin": "https://evil.test"})[0], 403)
            self.assertEqual(http_get(port, "/display/", {
                "Host": f"127.0.0.1:{port + 1}"})[0], 403)
            self.assertEqual(http_get(port, "/display/", {
                "Cookie": "usage_session=bad"})[0], 403)
            self.assertEqual(http_get(port, "/display/", {
                "X-Admin-Control": "token"})[0], 200)

    def test_remote_serve_identity_is_required(self):
        policy = console_tailnet.TailnetPolicy.from_document(DOCUMENT)
        with proxy_server(make_config(), policy=policy) as (_, port):
            self.assertEqual(http_get(port, "/display/", REMOTE_HEADERS)[0], 200)
            self.assertEqual(http_get(port, "/display/", dict(
                REMOTE_HEADERS, **{"X-Forwarded-For": "100.71.250.7"}))[0], 403)
            self.assertEqual(http_get(port, "/display/", dict(
                REMOTE_HEADERS, **{"Tailscale-User-Login": "someone@example.com"}))[0], 403)
            self.assertEqual(http_get(port, "/display/", dict(
                REMOTE_HEADERS, **{"Tailscale-Funnel-Request": "?1"}))[0], 403)
            self.assertEqual(http_get(port, "/display/", dict(
                REMOTE_HEADERS, **{"Host": "localhost:8001"}))[0], 403)
            self.assertEqual(http_get(port, "/display/", dict(
                REMOTE_HEADERS, **{"Origin": "https://evil.test"}))[0], 403)
            self.assertEqual(http_get(port, "/display/", dict(
                REMOTE_HEADERS, **{"Cookie": "usage_session=bad"}))[0], 403)
            remote = dict(REMOTE_HEADERS)
            remote.pop("X-Forwarded-For")
            self.assertEqual(http_get(port, "/display/", remote)[0], 403)


class DisplayWebSocketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.echo = _EchoServer(("127.0.0.1", 0), _EchoHandler)
        cls.echo_thread = threading.Thread(
            target=cls.echo.serve_forever, daemon=True)
        cls.echo_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.echo.shutdown()
        cls.echo.server_close()
        cls.echo_thread.join(3)

    def config(self, **changes):
        return make_config(vnc_port=self.echo.server_address[1], **changes)

    def test_handshake_binary_ping_close_and_release(self):
        with proxy_server(self.config(max_connections=1)) as (proxy, port):
            sock, key = _operator_ws(proxy, port)
            try:
                headers = _read_headers(sock)
                self.assertIn("101 Switching Protocols", headers)
                self.assertIn("Sec-WebSocket-Accept: "
                              + display.accept_key(key), headers)
                self.assertIn("Sec-WebSocket-Protocol: binary", headers)
                self.assertIn("Cache-Control: no-store", headers)

                sock.sendall(_client_frame(0x02, b"hello"))
                self.assertEqual(_server_frame(sock), (0x02, b"hello"))

                sock.sendall(_client_frame(0x09, b"p"))
                self.assertEqual(_server_frame(sock), (0x0A, b"p"))

                sock.sendall(_client_frame(0x08, struct.pack("!H", 1000)))
                opcode, payload = _server_frame(sock)
                self.assertEqual(opcode, 0x08)
                self.assertEqual(struct.unpack("!H", payload[:2])[0], 1000)
            finally:
                sock.close()
            self.assertTrue(wait_for(lambda: proxy.active_connections == 0))

    def test_second_connection_is_busy_then_released(self):
        with proxy_server(self.config(max_connections=1)) as (proxy, port):
            first, _ = _operator_ws(proxy, port)
            self.assertIn("101", _read_headers(first))
            second, _ = _operator_ws(proxy, port)
            self.assertIn("503", _read_headers(second))
            second.close()
            first.sendall(_client_frame(0x08, struct.pack("!H", 1000)))
            with contextlib.suppress(EOFError, OSError):
                _server_frame(first)
            first.close()
            self.assertTrue(wait_for(lambda: proxy.active_connections == 0))
            third, _ = _operator_ws(proxy, port)
            self.assertIn("101", _read_headers(third))
            third.close()

    def test_upstream_unavailable_is_502(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        unused = probe.getsockname()[1]
        probe.close()
        with proxy_server(make_config(vnc_port=unused)) as (proxy, port):
            sock, _ = _operator_ws(proxy, port)
            try:
                self.assertIn("502", _read_headers(sock))
            finally:
                sock.close()

    def test_bad_handshake_and_session_are_refused(self):
        with proxy_server(self.config()) as (proxy, port):
            cases = (
                ({"Origin": None}, "403"),
                ({"Origin": "https://evil.test"}, "403"),
                ({"Cookie": "usage_session=bad"}, "403"),
                ({"Sec-WebSocket-Version": "8"}, "426"),
                ({"Sec-WebSocket-Key": "short"}, "400"),
                ({"Sec-WebSocket-Protocol": "chat"}, "400"),
            )
            for change, expected in cases:
                with self.subTest(change=change):
                    sock, _ = _operator_ws(proxy, port, **change)
                    try:
                        self.assertIn(expected, _read_headers(sock))
                    finally:
                        sock.close()

    def test_upstream_close_releases_slot(self):
        with proxy_server(self.config()) as (proxy, port):
            sock, _ = _operator_ws(proxy, port)
            try:
                self.assertIn("101", _read_headers(sock))
                sock.sendall(_client_frame(0x02, b"quit"))
                payload = _server_close(sock)
                self.assertEqual(struct.unpack("!H", payload[:2])[0], 1000)
            finally:
                sock.close()
            self.assertTrue(wait_for(lambda: proxy.active_connections == 0))

    def test_idle_timeout_closes_and_releases(self):
        with proxy_server(self.config(
                idle_timeout=0.3, max_session_seconds=60)) as (proxy, port):
            sock, _ = _operator_ws(proxy, port)
            try:
                self.assertIn("101", _read_headers(sock))
                payload = _server_close(sock)
                self.assertEqual(struct.unpack("!H", payload[:2])[0], 1000)
            finally:
                sock.close()
            self.assertTrue(wait_for(lambda: proxy.active_connections == 0))

    def test_pong_replies_do_not_extend_the_idle_timeout(self):
        """Automatic pong replies are not user activity.

        The keepalive ping runs every second when ``idle_timeout`` is two
        seconds.  A browser answers each ping automatically; if those pongs
        refreshed ``last_client_activity`` the idle timeout would never fire.
        """
        with proxy_server(self.config(
                idle_timeout=2, max_session_seconds=60)) as (proxy, port):
            sock, _ = _operator_ws(proxy, port)
            try:
                self.assertIn("101", _read_headers(sock))
                sock.settimeout(1.0)
                deadline = time.monotonic() + 10
                close_code = None
                while time.monotonic() < deadline:
                    try:
                        opcode, payload = _server_frame(sock)
                    except socket.timeout:
                        continue
                    except (EOFError, OSError):
                        break
                    if opcode == 0x09:
                        sock.sendall(_client_frame(0x0A, payload))
                        continue
                    if opcode == 0x08:
                        close_code = struct.unpack("!H", payload[:2])[0]
                        break
                self.assertEqual(close_code, 1000)
            finally:
                sock.close()
            self.assertTrue(wait_for(lambda: proxy.active_connections == 0))

    def test_connect_timeout_does_not_close_an_idle_screen(self):
        """The TCP connect timeout must not bound the RFB read.

        A person reading or typing in the login window produces no upstream
        bytes for minutes; only ``idle_timeout`` may end that session.
        """
        with proxy_server(self.config(
                handshake_timeout=0.2, idle_timeout=5,
                max_session_seconds=60)) as (proxy, port):
            sock, _ = _operator_ws(proxy, port)
            try:
                self.assertIn("101", _read_headers(sock))
                time.sleep(0.6)
                sock.sendall(_client_frame(0x02, b"still-open"))
                self.assertEqual(_server_frame(sock), (0x02, b"still-open"))
            finally:
                sock.close()
            self.assertTrue(wait_for(lambda: proxy.active_connections == 0))

    def test_concurrent_frames_are_never_interleaved(self):
        """Four relay threads write whole frames, one at a time."""
        client = _SplitWriteClient()
        handler = types.SimpleNamespace(connection=client, rfile=io.BytesIO(b""))
        relay = display.WebSocketRelay(
            handler, _FakeUpstream(), idle_timeout=60, max_session_seconds=60)
        payloads = [bytes([index]) * 64 for index in range(1, 9)]
        threads = [threading.Thread(target=relay._send, args=(0x02, payload))
                   for payload in payloads]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        try:
            frames = _server_frames(b"".join(client.chunks))
            self.assertEqual(sorted(frames), sorted(payloads))
        finally:
            relay.close()

    def test_text_frame_closes_with_1003(self):
        with proxy_server(self.config()) as (proxy, port):
            sock, _ = _operator_ws(proxy, port)
            try:
                self.assertIn("101", _read_headers(sock))
                sock.sendall(_client_frame(0x01, b"text"))
                opcode, payload = _server_frame(sock)
                self.assertEqual(opcode, 0x08)
                self.assertEqual(struct.unpack("!H", payload[:2])[0], 1003)
            finally:
                sock.close()
            self.assertTrue(wait_for(lambda: proxy.active_connections == 0))

    def test_remote_serve_identity_is_required_for_websocket(self):
        policy = console_tailnet.TailnetPolicy.from_document(DOCUMENT)
        with proxy_server(self.config(), policy=policy) as (proxy, port):
            remote = dict(REMOTE_HEADERS)
            remote["Origin"] = DOCUMENT["origin"]
            remote.pop("Cookie", None)
            sock, _ = _operator_ws(
                proxy, port, origin=DOCUMENT["origin"], **remote)
            try:
                self.assertIn("101", _read_headers(sock))
            finally:
                sock.close()
            for change in (
                {"Origin": None},
                {"Origin": "https://evil.test"},
                {"X-Forwarded-For": "100.71.250.7"},
                {"Tailscale-User-Login": "someone@example.com"},
                {"Tailscale-Funnel-Request": "?1"},
            ):
                with self.subTest(change=change):
                    values = dict(remote)
                    values.update(change)
                    sock, _ = _operator_ws(
                        proxy, port, origin=DOCUMENT["origin"], **values)
                    try:
                        self.assertIn("403", _read_headers(sock))
                    finally:
                        sock.close()


class DisplayGrantTests(unittest.TestCase):
    def test_grant_is_origin_bound_single_use_and_expires(self):
        grants = display.DisplayGrants(ttl=30, session_ttl=30)
        token, expires_at = grants.issue(
            "control-token", "http://127.0.0.1:18096")
        self.assertGreater(expires_at, time.time())
        self.assertIsNone(
            grants.redeem(token, "http://127.0.0.1:18097"))
        session = grants.redeem(token, "http://127.0.0.1:18096")
        self.assertIsNotNone(session)
        self.assertEqual(session.subject, "control-token")
        self.assertEqual(session.origin, "http://127.0.0.1:18096")
        self.assertIsNone(
            grants.redeem(token, "http://127.0.0.1:18096"))
        self.assertIsNotNone(grants.validate_session(
            session.token, "http://127.0.0.1:18096"))
        self.assertIsNone(grants.validate_session(
            session.token, "http://127.0.0.1:18097"))

    def test_expired_grant_is_rejected(self):
        grants = display.DisplayGrants(ttl=0.01, session_ttl=30)
        token, _ = grants.issue(
            "control-token", "http://127.0.0.1:18096")
        time.sleep(0.05)
        self.assertIsNone(
            grants.redeem(token, "http://127.0.0.1:18096"))

    def test_grant_capacity_is_bounded(self):
        grants = display.DisplayGrants(
            ttl=30, session_ttl=30, max_pending=1)
        grants.issue("a", "http://127.0.0.1:18096")
        with self.assertRaises(display.DisplayGrantError):
            grants.issue("b", "http://127.0.0.1:18096")


class DisplayGrantRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.echo = _EchoServer(("127.0.0.1", 0), _EchoHandler)
        cls.echo_thread = threading.Thread(
            target=cls.echo.serve_forever, daemon=True)
        cls.echo_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.echo.shutdown()
        cls.echo.server_close()
        cls.echo_thread.join(3)

    def config(self):
        return make_config(vnc_port=self.echo.server_address[1])

    def exchange(self, port, token, origin, *, content_type="application/json",
                 length=None):
        body = json.dumps({"grant": token})
        headers = {
            "Content-Type": content_type,
            "Origin": origin,
            "Content-Length": str(len(body) if length is None else length),
        }
        conn = HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            conn.request("POST", "/display/grant", body=body, headers=headers)
            response = conn.getresponse()
            return (response.status, dict(response.getheaders()),
                    response.read())
        finally:
            conn.close()

    def test_grant_page_is_static_and_same_origin(self):
        with proxy_server(self.config(), session_valid=lambda _cookie: False) \
                as (_, port):
            status, headers, body = http_get(
                port, "/display/grant", {"Cookie": ""})
            self.assertEqual(status, 200)
            self.assertIn(b"/display/launcher.js", body)
            self.assertIn("script-src 'self'",
                          headers["Content-Security-Policy"])

    def test_ws_without_display_session_is_403(self):
        with proxy_server(self.config(), session_valid=lambda _cookie: False) \
                as (proxy, port):
            sock, _ = _ws_connect(port, Cookie=None)
            try:
                self.assertIn("403", _read_headers(sock))
            finally:
                sock.close()
            self.assertEqual(proxy.active_connections, 0)

    def test_grant_exchange_sets_httponly_cookie_and_ws_accepts(self):
        with proxy_server(self.config()) as (proxy, port):
            origin = f"http://127.0.0.1:{port}"
            token, _ = proxy.grants.issue("control-token", origin)
            status, headers, _ = self.exchange(port, token, origin)
            self.assertEqual(status, 204)
            cookie = headers.get("Set-Cookie", "")
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=Strict", cookie)
            self.assertIn("Path=/;", cookie)
            self.assertNotIn(token, cookie)
            session = cookie.split(";", 1)[0]
            display_session = proxy.grants.validate_session(
                session.split("=", 1)[1], origin)
            self.assertIsNotNone(display_session)
            sock, _ = _operator_ws(
                proxy, port, session=display_session)
            try:
                self.assertIn("101", _read_headers(sock))
            finally:
                sock.close()
            # The one-time grant cannot be exchanged twice.
            status, _, _ = self.exchange(port, token, origin)
            self.assertEqual(status, 403)

    def test_grant_exchange_rejects_cross_origin_and_bad_requests(self):
        with proxy_server(self.config()) as (proxy, port):
            origin = f"http://127.0.0.1:{port}"
            token, _ = proxy.grants.issue("control-token", origin)
            self.assertEqual(
                self.exchange(port, token, "https://evil.test")[0], 403)
            self.assertEqual(
                self.exchange(port, token, origin,
                               content_type="text/plain")[0], 415)
            self.assertEqual(
                self.exchange(port, token, origin, length=999999)[0], 413)
            # The rejected cross-origin attempt must not consume the grant.
            self.assertEqual(self.exchange(port, token, origin)[0], 204)

    def test_ws_rejects_cross_origin_display_session(self):
        with proxy_server(self.config()) as (proxy, port):
            origin = f"http://127.0.0.1:{port}"
            token, _ = proxy.grants.issue("control-token", origin)
            status, headers, _ = self.exchange(port, token, origin)
            self.assertEqual(status, 204)
            session = headers["Set-Cookie"].split(";", 1)[0]
            sock, _ = _ws_connect(
                port, Cookie=session, Origin="https://evil.test")
            try:
                self.assertIn("403", _read_headers(sock))
            finally:
                sock.close()

    def test_session_check_and_control_token(self):
        with proxy_server(self.config(), session_valid=lambda _cookie: False) \
                as (proxy, port):
            status, _, body = http_get(port, "/display/session")
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(body)["authorized"])
            status, _, body = http_get(
                port, "/display/session", {"X-Admin-Control": "token"})
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["authorized"])
            origin = f"http://127.0.0.1:{port}"
            token, _ = proxy.grants.issue("control-token", origin)
            _, headers, _ = self.exchange(port, token, origin)
            session = headers["Set-Cookie"].split(";", 1)[0]
            status, _, body = http_get(
                port, "/display/session", {"Cookie": session})
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["authorized"])

    def test_forged_tailscale_header_without_policy_is_403(self):
        with proxy_server(self.config()) as (proxy, port):
            sock, _ = _ws_connect(
                port, **{"Tailscale-User-Login": "owner@example.com",
                         "X-Forwarded-For": "100.127.119.98",
                         "X-Forwarded-Proto": "https",
                         "X-Forwarded-Host": f"pc.example.ts.net:{port}"})
            try:
                self.assertIn("403", _read_headers(sock))
            finally:
                sock.close()
            status, _, _ = http_get(port, "/display/", {
                "Tailscale-User-Login": "owner@example.com",
                "X-Forwarded-For": "100.127.119.98",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": f"pc.example.ts.net:{port}"})
            self.assertEqual(status, 403)


class DisplayOperatorGrantTests(unittest.TestCase):
    ORIGIN = "http://127.0.0.1:18096"

    def session(self, grants, now=None):
        token, _ = grants.issue(subject="control-token", origin=self.ORIGIN,
                               now=now)
        return grants.redeem(token, self.ORIGIN, now=now)

    def test_acquire_is_idempotent_and_conflicts_with_another_page(self):
        grants = display.DisplayGrants(ttl=30, session_ttl=30)
        first = self.session(grants)
        second = self.session(grants)
        self.assertEqual(
            grants.operator_state(first.token, self.ORIGIN),
            {"owned_by_me": False, "owner_present": False,
             "generation": 0, "stale": False})
        acquired = grants.acquire_operator(first.token, self.ORIGIN)
        self.assertTrue(acquired["acquired"])
        self.assertFalse(acquired["took_over"])
        generation = acquired["generation"]
        self.assertGreaterEqual(generation, 1)
        self.assertEqual(
            grants.operator_state(first.token, self.ORIGIN),
            {"owned_by_me": True, "owner_present": True,
             "generation": generation, "stale": False})
        self.assertEqual(
            grants.operator_state(second.token, self.ORIGIN),
            {"owned_by_me": False, "owner_present": True,
             "generation": generation, "stale": False})
        # The current owner re-acquiring is idempotent.
        again = grants.acquire_operator(first.token, self.ORIGIN)
        self.assertEqual(again["generation"], generation)
        self.assertFalse(again["took_over"])
        conflict = grants.acquire_operator(second.token, self.ORIGIN)
        self.assertFalse(conflict["acquired"])
        self.assertEqual(conflict["error"], "display_operator_exists")
        self.assertEqual(conflict["generation"], generation)
        self.assertTrue(conflict["owner_label"])
        self.assertNotIn(first.token, conflict["owner_label"])

    def test_takeover_replaces_the_owner_with_a_new_generation(self):
        grants = display.DisplayGrants(ttl=30, session_ttl=30)
        first = self.session(grants)
        second = self.session(grants)
        generation = grants.acquire_operator(first.token, self.ORIGIN)["generation"]
        taken = grants.takeover_operator(second.token, self.ORIGIN)
        self.assertTrue(taken["acquired"])
        self.assertTrue(taken["took_over"])
        self.assertGreater(taken["generation"], generation)
        self.assertEqual(
            grants.operator_state(first.token, self.ORIGIN)["owned_by_me"], False)
        self.assertEqual(
            grants.operator_state(second.token, self.ORIGIN)["owned_by_me"], True)
        # A late close from the old page cannot release the new owner.
        self.assertFalse(
            grants.release_operator(first.token, generation))
        self.assertTrue(
            grants.operator_state(second.token, self.ORIGIN)["owner_present"])
        self.assertTrue(
            grants.release_operator(second.token, taken["generation"]))
        self.assertFalse(
            grants.operator_state(second.token, self.ORIGIN)["owner_present"])

    def test_operator_requires_a_live_display_session(self):
        grants = display.DisplayGrants(ttl=30, session_ttl=30)
        self.assertEqual(
            grants.acquire_operator("forged", self.ORIGIN),
            {"acquired": False, "error": "display_session_required"})
        self.assertEqual(
            grants.takeover_operator("forged", self.ORIGIN),
            {"acquired": False, "error": "display_session_required"})
        token, _ = grants.issue("control-token", self.ORIGIN, now=1000)
        session = grants.redeem(token, self.ORIGIN, now=1000)
        # The session expires, so the recorded owner becomes stale and can be
        # replaced without an explicit takeover.
        grants.acquire_operator(session.token, self.ORIGIN, now=1000)
        state = grants.operator_state(session.token, self.ORIGIN, now=1100)
        self.assertTrue(state["owner_present"])
        self.assertTrue(state["stale"])
        self.assertFalse(state["owned_by_me"])
        replacement = self.session(grants, now=1100)
        acquired = grants.acquire_operator(
            replacement.token, self.ORIGIN, now=1100)
        self.assertTrue(acquired["acquired"])
        self.assertTrue(acquired["took_over"])

    def test_operator_matches_only_the_current_generation(self):
        grants = display.DisplayGrants(ttl=30, session_ttl=30)
        first = self.session(grants)
        second = self.session(grants)
        generation = grants.acquire_operator(first.token, self.ORIGIN)["generation"]
        self.assertTrue(grants.operator_matches(first.token, generation))
        self.assertFalse(grants.operator_matches(second.token, generation))
        taken = grants.takeover_operator(second.token, self.ORIGIN)
        self.assertFalse(grants.operator_matches(first.token, generation))
        self.assertTrue(
            grants.operator_matches(second.token, taken["generation"]))


class DisplayOperatorRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.echo = _EchoServer(("127.0.0.1", 0), _EchoHandler)
        cls.echo_thread = threading.Thread(
            target=cls.echo.serve_forever, daemon=True)
        cls.echo_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.echo.shutdown()
        cls.echo.server_close()
        cls.echo_thread.join(3)

    def config(self):
        return make_config(vnc_port=self.echo.server_address[1],
                           max_connections=2)

    def origin(self, port):
        return f"http://127.0.0.1:{port}"

    def test_websockify_requires_the_current_operator_generation(self):
        with proxy_server(self.config()) as (proxy, port):
            origin = self.origin(port)
            session = _display_session(proxy, origin)
            no_query, _ = _ws_connect(
                port, Cookie=f"{display.DISPLAY_SESSION_COOKIE}={session.token}")
            try:
                headers, body = _read_http_response(no_query)
                self.assertIn("409", headers)
                self.assertIn("display_operator_stale", body.decode("utf-8"))
            finally:
                no_query.close()
            generation = proxy.grants.acquire_operator(
                session.token, origin)["generation"]
            stale, _ = _ws_for_session(port, session, generation + 1)
            try:
                headers, body = _read_http_response(stale)
                self.assertIn("409", headers)
                self.assertIn("display_operator_stale", body.decode("utf-8"))
            finally:
                stale.close()
            accepted, _ = _ws_for_session(port, session, generation)
            try:
                self.assertIn("101", _read_headers(accepted))
            finally:
                accepted.close()
            # A console session without a display session cannot operate.
            console, _ = _ws_connect(
                port, path=f"/display/websockify?operator={generation}")
            try:
                self.assertIn("403", _read_headers(console))
            finally:
                console.close()

    def test_websockify_query_allows_only_the_operator_key(self):
        with proxy_server(self.config()) as (proxy, port):
            origin = self.origin(port)
            session = _display_session(proxy, origin)
            generation = proxy.grants.acquire_operator(
                session.token, origin)["generation"]
            for path in (
                f"/display/websockify?operator={generation}&path=x",
                f"/display/websockify?operator={generation}&operator={generation}",
                "/display/websockify?operator=abc",
                "/display/websockify?operator=0",
                "/display/websockify?operator=01",
                "/display/websockify?password=x",
                f"/display/launcher.js?operator={generation}",
            ):
                with self.subTest(path=path):
                    sock, _ = _ws_connect(
                        port, path=path,
                        Cookie=f"{display.DISPLAY_SESSION_COOKIE}={session.token}")
                    try:
                        self.assertIn("400", _read_headers(sock))
                    finally:
                        sock.close()

    def test_old_generation_input_is_dropped_and_close_keeps_new_owner(self):
        with proxy_server(self.config()) as (proxy, port):
            origin = self.origin(port)
            first = _display_session(proxy, origin)
            second = _display_session(proxy, origin)
            first_generation = proxy.grants.acquire_operator(
                first.token, origin)["generation"]
            old_page, _ = _ws_for_session(port, first, first_generation)
            try:
                self.assertIn("101", _read_headers(old_page))
                taken = proxy.grants.takeover_operator(second.token, origin)
                self.assertTrue(taken["took_over"])
                new_page, _ = _ws_for_session(
                    port, second, taken["generation"])
                try:
                    self.assertIn("101", _read_headers(new_page))
                    # Keyboard/pointer input from the old page is dropped.
                    old_page.settimeout(0.5)
                    old_page.sendall(_client_frame(0x02, b"\x05\x00\x00\x00\x00\x00"))
                    with self.assertRaises(socket.timeout):
                        _server_frame(old_page)
                    # Viewing (FramebufferUpdateRequest, type 3) is not input.
                    old_page.sendall(_client_frame(0x02, b"\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00"))
                    self.assertEqual(
                        _server_frame(old_page),
                        (0x02, b"\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00"))
                    new_page.sendall(_client_frame(0x02, b"\x04\x01\x00\x00\x00\x00\x00a"))
                    self.assertEqual(
                        _server_frame(new_page),
                        (0x02, b"\x04\x01\x00\x00\x00\x00\x00a"))
                    # The old page closing only releases its own generation.
                    old_page.close()
                    self.assertTrue(wait_for(
                        lambda: proxy.active_connections == 1))
                    state = proxy.grants.operator_state(second.token, origin)
                    self.assertTrue(state["owned_by_me"])
                    self.assertEqual(state["generation"], taken["generation"])
                finally:
                    new_page.close()
            finally:
                with contextlib.suppress(OSError):
                    old_page.close()
            self.assertTrue(wait_for(lambda: proxy.active_connections == 0))
            self.assertFalse(
                proxy.grants.operator_state(second.token, origin)["owner_present"])

    def test_disconnecting_the_viewer_does_not_stop_the_services(self):
        with proxy_server(self.config()) as (proxy, port):
            with mock.patch("subprocess.run") as run, \
                    mock.patch("os.system") as system:
                sock, _ = _operator_ws(proxy, port)
                try:
                    self.assertIn("101", _read_headers(sock))
                finally:
                    sock.close()
            self.assertTrue(wait_for(lambda: proxy.active_connections == 0))
            run.assert_not_called()
            system.assert_not_called()

    def test_proxy_operator_state_and_acquire_use_the_display_session(self):
        with proxy_server(self.config()) as (proxy, port):
            origin = self.origin(port)
            session = _display_session(proxy, origin)
            cookie = f"{display.DISPLAY_SESSION_COOKIE}={session.token}"
            self.assertFalse(
                proxy.operator_state(cookie, origin)["owned_by_me"])
            self.assertEqual(
                proxy.acquire_operator("um_display_session=forged", origin),
                {"acquired": False, "error": "display_session_required"})
            acquired = proxy.acquire_operator(cookie, origin)
            self.assertTrue(acquired["acquired"])
            self.assertTrue(
                proxy.operator_state(cookie, origin)["owned_by_me"])
            self.assertEqual(
                proxy.acquire_operator(cookie, origin, action="takeover")
                ["generation"], acquired["generation"])


class VncAuthTests(unittest.TestCase):
    def test_des_matches_standard_vector(self):
        self.assertEqual(
            display._des_encrypt_block(
                bytes.fromhex("4e6f772069732074"),
                bytes.fromhex("0123456789abcdef")).hex(),
            "3fa40e8a984d4815")

    def test_vnc_auth_response_matches_d3des_vector(self):
        self.assertEqual(
            display.vnc_auth_response(
                bytes.fromhex("0123456789abcdef"),
                bytes.fromhex("0123456789abcdef0123456789abcdef")).hex(),
            "6e09a37726dd560c6e09a37726dd560c")

    def test_storepasswd_file_decrypts_to_password(self):
        self.assertEqual(
            display.vnc_password_key(bytes.fromhex("dbd83cfd727a1458")),
            b"password")

    def test_vnc_auth_response_matches_reference(self):
        self.assertEqual(
            display.vnc_auth_response(b"password", bytes(range(16))).hex(),
            "b866924125c8eebb9debc1db61c538e2")

    def test_password_file_rejects_wrong_length(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "vnc.passwd"
            path.write_bytes(b"short")
            with self.assertRaises(display.VncAuthError):
                display.load_vnc_password(str(path))

    @unittest.skipUnless(os.name == "posix", "POSIX mode gate")
    def test_password_file_rejects_loose_permissions(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "vnc.passwd"
            path.write_bytes(bytes.fromhex("dbd83cfd727a1458"))
            os.chmod(path, 0o644)
            with self.assertRaises(display.VncAuthError):
                display.load_vnc_password(str(path))

    def test_unix_auth_client_uses_the_helper_socket(self):
        client_sock, helper_sock = socket.socketpair()
        challenge = bytes(range(16))
        expected = bytes.fromhex("b866924125c8eebb9debc1db61c538e2")

        def helper():
            try:
                self.assertEqual(_recv_exact(helper_sock, 16), challenge)
                helper_sock.sendall(expected)
            finally:
                helper_sock.close()

        thread = threading.Thread(target=helper, daemon=True)
        thread.start()
        try:
            provider = display.UnixVncAuthClient(
                "/run/um-share/vnc-auth.sock",
                socket_factory=lambda _path: client_sock)
            self.assertEqual(provider(challenge), expected)
        finally:
            client_sock.close()
            thread.join(3)

    def test_helper_short_response_is_rejected(self):
        client_sock, helper_sock = socket.socketpair()

        def helper():
            try:
                _recv_exact(helper_sock, 16)
                helper_sock.sendall(b"short")
            finally:
                helper_sock.close()

        thread = threading.Thread(target=helper, daemon=True)
        thread.start()
        try:
            provider = display.UnixVncAuthClient(
                "/run/um-share/vnc-auth.sock",
                socket_factory=lambda _path: client_sock)
            with self.assertRaises(display.VncAuthError):
                provider(bytes(range(16)))
        finally:
            client_sock.close()
            thread.join(3)


class _VncAuthUpstream(threading.Thread):
    """Minimal x11vnc-style RFB 3.8 VncAuth server for the proxy test."""

    def __init__(self, expected_response, *, fail=False, name=b"test"):
        super().__init__(daemon=True)
        self.expected_response = expected_response
        self.fail = fail
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.server_init = (
            b"\x00\x05\x00\x03" + b"\x00" * 16
            + struct.pack("!I", len(name)) + name)
        self.after_server_init = b""
        self.error = None

    def run(self):
        try:
            conn, _ = self.server.accept()
            with conn:
                conn.settimeout(3)
                conn.sendall(b"RFB 003.008\n")
                if _recv_exact(conn, 12) != b"RFB 003.008\n":
                    raise AssertionError("unexpected client version")
                conn.sendall(b"\x01\x02")
                if _recv_exact(conn, 1) != b"\x02":
                    raise AssertionError("unexpected security type")
                conn.sendall(bytes(range(16)))
                response = _recv_exact(conn, 16)
                if self.fail or response != self.expected_response:
                    conn.sendall(b"\x00\x00\x00\x01"
                                  + struct.pack("!I", 4) + b"fail")
                    return
                conn.sendall(b"\x00\x00\x00\x00")
                if _recv_exact(conn, 1) != b"\x01":
                    raise AssertionError("unexpected client init")
                conn.sendall(self.server_init)
                self.after_server_init = conn.recv(4096)
        except Exception as exc:  # pragma: no cover - surfaced by the test
            self.error = exc
        finally:
            self.server.close()


class RfbAuthTests(unittest.TestCase):
    def test_proxy_authenticates_upstream_and_hides_vnc_auth(self):
        expected = bytes.fromhex("b866924125c8eebb9debc1db61c538e2")
        upstream = _VncAuthUpstream(expected)
        upstream.start()
        with proxy_server(
                make_config(vnc_port=upstream.port, relay_join_timeout=0.05),
                auth_provider=lambda challenge: expected) as (proxy, port):
            sock, _ = _operator_ws(proxy, port)
            try:
                self.assertIn("101", _read_headers(sock))
                version, rest = _read_greeting(sock, upstream.server_init)
                self.assertEqual(version, b"RFB 003.008\n")
                self.assertEqual(
                    rest,
                    b"\x01\x01" + b"\x00\x00\x00\x00"
                    + upstream.server_init)
                # noVNC sends its own client version, security type selection
                # and ClientInit after the synthesized handshake; all are
                # consumed by the proxy. Only the subsequent RFB message
                # reaches x11vnc.
                sock.sendall(_client_frame(0x02, b"RFB 003.008\n"))
                sock.sendall(_client_frame(0x02, b"\x01"))
                sock.sendall(_client_frame(0x02, b"\x01"))
                sock.sendall(_client_frame(
                    0x02, b"\x00" + b"\x00" * 19))
                self.assertTrue(wait_for(
                    lambda: upstream.after_server_init == b"\x00" * 20))
            finally:
                sock.close()
        upstream.join(3)
        self.assertIsNone(upstream.error)
        self.assertNotIn(b"password", version + rest)

    def test_greeting_server_init_is_24_bytes_plus_name(self):
        expected = bytes.fromhex("b866924125c8eebb9debc1db61c538e2")
        name = b"iZwz9er7gkoeymf5r6cwckZ:99"
        self.assertEqual(len(name), 26)
        upstream = _VncAuthUpstream(expected, name=name)
        upstream.start()
        with proxy_server(
                make_config(vnc_port=upstream.port, relay_join_timeout=0.05),
                auth_provider=lambda challenge: expected) as (proxy, port):
            sock, _ = _operator_ws(proxy, port)
            try:
                self.assertIn("101", _read_headers(sock))
                _, rest = _read_greeting(sock, upstream.server_init)
                server_init = rest[6:]
                self.assertEqual(len(server_init), 24 + len(name))
                self.assertEqual(
                    server_init[20:24], struct.pack("!I", len(name)))
                self.assertEqual(server_init[24:], name)
            finally:
                sock.close()
        upstream.join(3)
        self.assertIsNone(upstream.error)

    def test_greeting_includes_security_result_for_type_none(self):
        expected = bytes.fromhex("b866924125c8eebb9debc1db61c538e2")
        upstream = _VncAuthUpstream(expected)
        upstream.start()
        with proxy_server(
                make_config(vnc_port=upstream.port, relay_join_timeout=0.05),
                auth_provider=lambda challenge: expected) as (proxy, port):
            sock, _ = _operator_ws(proxy, port)
            try:
                self.assertIn("101", _read_headers(sock))
                _, rest = _read_greeting(sock, upstream.server_init)
                # RFB 3.8 None: one security type (None) and its 4-byte
                # SecurityResult, which noVNC reads before ServerInit.
                self.assertEqual(rest[:2], b"\x01\x01")
                self.assertEqual(rest[2:6], b"\x00\x00\x00\x00")
            finally:
                sock.close()
        upstream.join(3)
        self.assertIsNone(upstream.error)

    def test_relay_discards_exactly_fourteen_client_bytes_across_frames(self):
        expected = bytes.fromhex("b866924125c8eebb9debc1db61c538e2")
        set_pixel_format = bytes.fromhex(
            "000000002018000100ff00ff00ff100800000000")
        self.assertEqual(len(set_pixel_format), 20)
        upstream = _VncAuthUpstream(expected)
        upstream.start()
        with proxy_server(
                make_config(vnc_port=upstream.port, relay_join_timeout=0.05),
                auth_provider=lambda challenge: expected) as (proxy, port):
            sock, _ = _operator_ws(proxy, port)
            try:
                self.assertIn("101", _read_headers(sock))
                _read_greeting(sock, upstream.server_init)
                sock.sendall(_client_frame(0x02, b"RFB 003.008\n"))
                sock.sendall(_client_frame(0x02, b"\x01"))
                sock.sendall(_client_frame(0x02, b"\x01"))
                sock.sendall(_client_frame(0x02, set_pixel_format))
                self.assertTrue(wait_for(
                    lambda: upstream.after_server_init == set_pixel_format))
            finally:
                sock.close()
        upstream.join(3)
        self.assertIsNone(upstream.error)

    def test_relay_discards_concatenated_fourteen_byte_prefix(self):
        expected = bytes.fromhex("b866924125c8eebb9debc1db61c538e2")
        set_pixel_format = bytes.fromhex(
            "000000002018000100ff00ff00ff100800000000")
        upstream = _VncAuthUpstream(expected)
        upstream.start()
        with proxy_server(
                make_config(vnc_port=upstream.port, relay_join_timeout=0.05),
                auth_provider=lambda challenge: expected) as (proxy, port):
            sock, _ = _operator_ws(proxy, port)
            try:
                self.assertIn("101", _read_headers(sock))
                _read_greeting(sock, upstream.server_init)
                sock.sendall(_client_frame(
                    0x02, b"RFB 003.008\n" + b"\x01" + b"\x01"))
                sock.sendall(_client_frame(0x02, set_pixel_format))
                self.assertTrue(wait_for(
                    lambda: upstream.after_server_init == set_pixel_format))
            finally:
                sock.close()
        upstream.join(3)
        self.assertIsNone(upstream.error)

    def test_upstream_auth_failure_is_502_without_upgrade(self):
        upstream = _VncAuthUpstream(b"\x00" * 16, fail=True)
        upstream.start()
        with proxy_server(
                make_config(vnc_port=upstream.port, relay_join_timeout=0.05),
                auth_provider=lambda challenge: b"\x01" * 16) as (proxy, port):
            sock, _ = _operator_ws(proxy, port)
            try:
                self.assertIn("502", _read_headers(sock))
            finally:
                sock.close()
        upstream.join(3)
        self.assertIsNone(upstream.error)

    def test_auth_provider_error_is_502_without_upgrade(self):
        upstream = _VncAuthUpstream(b"\x00" * 16)
        upstream.start()

        def failing_provider(_challenge):
            raise display.VncAuthError("display_vnc_auth_unavailable")

        with proxy_server(
                make_config(vnc_port=upstream.port, relay_join_timeout=0.05),
                auth_provider=failing_provider) as (proxy, port):
            sock, _ = _operator_ws(proxy, port)
            try:
                self.assertIn("502", _read_headers(sock))
            finally:
                sock.close()
        upstream.join(3)

    def test_missing_auth_socket_is_502_without_upgrade(self):
        with tempfile.TemporaryDirectory() as td:
            missing = str(Path(td) / "missing.sock")
            with proxy_server(
                    make_config(vnc_auth_socket=missing,
                                 relay_join_timeout=0.05)) as (proxy, port):
                sock, _ = _operator_ws(proxy, port)
                try:
                    self.assertIn("502", _read_headers(sock))
                finally:
                    sock.close()


class DisplayRelayLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.echo = _EchoServer(("127.0.0.1", 0), _EchoHandler)
        cls.echo_thread = threading.Thread(
            target=cls.echo.serve_forever, daemon=True)
        cls.echo_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.echo.shutdown()
        cls.echo.server_close()
        cls.echo_thread.join(3)

    def config(self, **changes):
        return make_config(
            vnc_port=self.echo.server_address[1], **changes)

    def test_healthy_session_survives_past_old_join_breakpoint(self):
        """The old run() joined four workers for 5s each and then stopped.

        The short timeout here is the same breakpoint scaled down; the session
        must stay open well past it and keep relaying.
        """
        with proxy_server(self.config(relay_join_timeout=0.05)) as (proxy, port):
            sock, _ = _operator_ws(proxy, port)
            try:
                self.assertIn("101", _read_headers(sock))
                time.sleep(0.35)  # 7x the old 4 x 0.05 breakpoint
                sock.sendall(_client_frame(0x02, b"still-open"))
                self.assertEqual(_server_frame(sock), (0x02, b"still-open"))
                sock.sendall(_client_frame(0x08, struct.pack("!H", 1000)))
                self.assertEqual(
                    struct.unpack("!H", _server_close(sock)[:2])[0], 1000)
            finally:
                sock.close()
            self.assertTrue(wait_for(lambda: proxy.active_connections == 0))

    def test_max_session_closes_1000_and_releases(self):
        with proxy_server(self.config(
                idle_timeout=60, max_session_seconds=0.3)) as (proxy, port):
            sock, _ = _operator_ws(proxy, port)
            try:
                self.assertIn("101", _read_headers(sock))
                self.assertEqual(
                    struct.unpack("!H", _server_close(sock)[:2])[0], 1000)
            finally:
                sock.close()
            self.assertTrue(wait_for(lambda: proxy.active_connections == 0))

    def test_unmasked_client_frame_closes_1002_and_releases(self):
        with proxy_server(self.config()) as (proxy, port):
            sock, _ = _operator_ws(proxy, port)
            try:
                self.assertIn("101", _read_headers(sock))
                sock.sendall(b"\x82\x01x")  # unmasked binary frame
                opcode, payload = _server_frame(sock)
                self.assertEqual(opcode, 0x08)
                self.assertEqual(struct.unpack("!H", payload[:2])[0], 1002)
            finally:
                sock.close()
            self.assertTrue(wait_for(lambda: proxy.active_connections == 0))

    def test_close_all_cancels_active_relay_with_1001(self):
        with proxy_server(self.config()) as (proxy, port):
            sock, _ = _operator_ws(proxy, port)
            try:
                self.assertIn("101", _read_headers(sock))
                proxy.close_all()
                self.assertEqual(
                    struct.unpack("!H", _server_close(sock)[:2])[0], 1001)
            finally:
                sock.close()
            self.assertTrue(wait_for(lambda: proxy.active_connections == 0))

    def test_upstream_eof_releases_both_sockets_and_workers(self):
        client_proxy, client_peer = socket.socketpair()
        upstream_proxy, upstream_peer = socket.socketpair()
        handler = types.SimpleNamespace(
            connection=client_proxy, rfile=client_proxy.makefile("rb"))
        relay = display.WebSocketRelay(
            handler, upstream_proxy, idle_timeout=60,
            max_session_seconds=60, join_timeout=0.05)
        thread = threading.Thread(target=relay.run, daemon=True)
        thread.start()
        try:
            upstream_peer.close()
            self.assertEqual(
                struct.unpack("!H", _server_close(client_peer)[:2])[0], 1000)
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertTrue(relay.stop_event.is_set())
            self.assertEqual(client_peer.recv(1), b"")
            self.assertTrue(all(not worker.is_alive()
                                for worker in relay.worker_threads))
        finally:
            client_proxy.close()
            client_peer.close()
            upstream_proxy.close()
            upstream_peer.close()

    def test_client_close_releases_upstream_and_workers(self):
        client_proxy, client_peer = socket.socketpair()
        upstream_proxy, upstream_peer = socket.socketpair()
        handler = types.SimpleNamespace(
            connection=client_proxy, rfile=client_proxy.makefile("rb"))
        relay = display.WebSocketRelay(
            handler, upstream_proxy, idle_timeout=60,
            max_session_seconds=60, join_timeout=0.05)
        thread = threading.Thread(target=relay.run, daemon=True)
        thread.start()
        try:
            client_peer.sendall(_client_frame(0x08, struct.pack("!H", 1000)))
            self.assertEqual(
                struct.unpack("!H", _server_close(client_peer)[:2])[0], 1000)
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertTrue(relay.stop_event.is_set())
            self.assertEqual(upstream_peer.recv(1), b"")
            self.assertTrue(all(not worker.is_alive()
                                for worker in relay.worker_threads))
        finally:
            client_proxy.close()
            client_peer.close()
            upstream_proxy.close()
            upstream_peer.close()

    def test_cancel_sends_1001_and_returns_promptly(self):
        client_proxy, client_peer = socket.socketpair()
        upstream_proxy, upstream_peer = socket.socketpair()
        handler = types.SimpleNamespace(
            connection=client_proxy, rfile=client_proxy.makefile("rb"))
        relay = display.WebSocketRelay(
            handler, upstream_proxy, idle_timeout=60,
            max_session_seconds=60, join_timeout=0.05)
        thread = threading.Thread(target=relay.run, daemon=True)
        thread.start()
        try:
            relay.cancel()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(
                struct.unpack("!H", _server_close(client_peer)[:2])[0], 1001)
            self.assertEqual(upstream_peer.recv(1), b"")
        finally:
            client_proxy.close()
            client_peer.close()
            upstream_proxy.close()
            upstream_peer.close()


def _flood_until_blocked(sock, total=4 * 1024 * 1024):
    """Fill the peer's receive buffer so the relay's send stalls."""
    chunk = b"x" * 65536
    try:
        sock.settimeout(0.5)
        written = 0
        while written < total:
            written += sock.send(chunk)
    except OSError:
        pass


class _StalledSendSocket:
    """A real client socket whose ``sendall`` blocks until its timeout.

    It models a peer that stopped reading: the send never completes on its own,
    and only the bounded timeout set by the relay releases the sender.  A send
    without a timeout would wait for the test's release event.
    """

    def __init__(self, sock):
        self._sock = sock
        self.send_started = threading.Event()
        self.release = threading.Event()
        self.closed = False
        self.shutdowns = []

    def fileno(self):
        return self._sock.fileno()

    def gettimeout(self):
        return self._sock.gettimeout()

    def settimeout(self, value):
        self._sock.settimeout(value)

    def recv(self, size):
        return self._sock.recv(size)

    def sendall(self, _data):
        self.send_started.set()
        timeout = self.gettimeout()
        if timeout is None:
            self.release.wait(30)
            raise OSError("stalled peer")
        # A real socket send with a timeout fails once the peer's buffer stays
        # full for the whole timeout.
        self.release.wait(timeout)
        raise TimeoutError("stalled peer send timed out")

    def shutdown(self, how):
        # Deliberately does not release the parked send.
        self.shutdowns.append(how)

    def close(self):
        self.closed = True
        self._sock.close()


class _FloodServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _FloodHandler(socketserver.BaseRequestHandler):
    """Sends more RFB bytes than a non-reading WebSocket client absorbs."""

    def handle(self):
        try:
            self.request.sendall(b"y" * (2 * 1024 * 1024))
        except OSError:
            pass


class DisplayRelayBackpressureTests(unittest.TestCase):
    """R17-R1: a client that stops reading must not pin the relay forever.

    A data frame parked in ``sendall`` holds ``_send_lock``.  Cancel, session
    expiry and peer close must still finish: the close frame is best effort, and
    ``_stop()`` shuts both sockets down to release the parked sender.
    """

    def start_relay(self, *, client_factory=None, idle_timeout=60,
                    max_session_seconds=60, join_timeout=0.1):
        client_proxy, client_peer = socket.socketpair()
        upstream_proxy, upstream_peer = socket.socketpair()
        for sock in (client_proxy, client_peer, upstream_proxy, upstream_peer):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        client = client_factory(client_proxy) if client_factory else client_proxy
        handler = types.SimpleNamespace(
            connection=client, rfile=io.BytesIO(b""))
        relay = display.WebSocketRelay(
            handler, upstream_proxy, idle_timeout=idle_timeout,
            max_session_seconds=max_session_seconds,
            join_timeout=join_timeout)
        run_thread = threading.Thread(target=relay.run, daemon=True)
        run_thread.start()
        writer = threading.Thread(
            target=_flood_until_blocked, args=(upstream_peer,), daemon=True)
        writer.start()
        return (relay, run_thread, client_peer, upstream_proxy,
                upstream_peer, writer)

    def assert_backpressured(self, relay):
        self.assertTrue(
            wait_for(lambda: relay._send_lock.locked(), 5),
            "the relay never parked a frame in a client send")
        # A lock held only for an in-flight frame is released within
        # milliseconds; a stalled peer keeps it held across this window.
        time.sleep(0.2)
        self.assertTrue(
            relay._send_lock.locked(), "the client send was not stalled")

    def close_relay(self, relay, run_thread, writer, *threads):
        relay.close()
        for thread in (writer, run_thread) + threads:
            if thread is not None:
                thread.join(3)

    def test_cancel_under_backpressure_returns_and_closes_both_sockets(self):
        relay, run_thread, client_peer, upstream_proxy, upstream_peer, writer = \
            self.start_relay()
        cancel_thread = None
        try:
            self.assert_backpressured(relay)
            started = time.monotonic()
            cancel_thread = threading.Thread(target=relay.cancel, daemon=True)
            cancel_thread.start()
            cancel_thread.join(2.0)
            elapsed = time.monotonic() - started
            self.assertFalse(
                cancel_thread.is_alive(),
                "cancel() blocked on a backpressured client send")
            self.assertLess(elapsed, 1.5)
            run_thread.join(2.0)
            self.assertFalse(run_thread.is_alive(),
                             "run() did not finish after cancel")
            self.assertTrue(relay.stop_event.is_set())
            self.assertEqual(relay.client_socket.fileno(), -1)
            self.assertEqual(relay.upstream_socket.fileno(), -1)
            self.assertTrue(wait_for(
                lambda: not any(t.is_alive() for t in relay.worker_threads), 2),
                "relay workers did not exit")
        finally:
            self.close_relay(relay, run_thread, writer, cancel_thread)
            for sock in (client_peer, upstream_peer):
                with contextlib.suppress(OSError):
                    sock.close()

    def test_max_session_expiry_ends_a_backpressured_relay(self):
        relay, run_thread, client_peer, upstream_proxy, upstream_peer, writer = \
            self.start_relay(max_session_seconds=0.5)
        try:
            self.assert_backpressured(relay)
            run_thread.join(4.0)
            self.assertFalse(
                run_thread.is_alive(),
                "max-session expiry blocked on the client send lock")
            self.assertEqual(relay.client_socket.fileno(), -1)
            self.assertEqual(relay.upstream_socket.fileno(), -1)
            self.assertTrue(wait_for(
                lambda: not any(t.is_alive() for t in relay.worker_threads), 2),
                "relay workers did not exit")
        finally:
            self.close_relay(relay, run_thread, writer)
            for sock in (client_peer, upstream_peer):
                with contextlib.suppress(OSError):
                    sock.close()

    def test_client_close_under_backpressure_releases_both_sockets(self):
        relay, run_thread, client_peer, upstream_proxy, upstream_peer, writer = \
            self.start_relay()
        try:
            self.assert_backpressured(relay)
            # The peer stops sending (FIN) but keeps its receive buffer full, so
            # the relay's parked send is not reset by a peer close.
            client_peer.shutdown(socket.SHUT_WR)
            run_thread.join(3.0)
            self.assertFalse(
                run_thread.is_alive(),
                "a client close blocked behind the parked send")
            self.assertEqual(relay.client_socket.fileno(), -1)
            self.assertEqual(relay.upstream_socket.fileno(), -1)
            self.assertTrue(wait_for(
                lambda: not any(t.is_alive() for t in relay.worker_threads), 2),
                "relay workers did not exit")
        finally:
            self.close_relay(relay, run_thread, writer)
            for sock in (client_peer, upstream_peer):
                with contextlib.suppress(OSError):
                    sock.close()

    def test_run_ends_when_a_worker_never_leaves_sendall(self):
        relay, run_thread, client_peer, upstream_proxy, upstream_peer, writer = \
            self.start_relay(
                client_factory=_StalledSendSocket, join_timeout=0.2)
        client = relay.client_socket
        cancel_thread = None
        try:
            self.assertTrue(client.send_started.wait(3),
                            "the relay never tried to write to the client")
            started = time.monotonic()
            cancel_thread = threading.Thread(target=relay.cancel, daemon=True)
            cancel_thread.start()
            cancel_thread.join(2.0)
            self.assertFalse(
                cancel_thread.is_alive(),
                "cancel() waited for a send that cannot be woken")
            self.assertLess(time.monotonic() - started, 1.5)
            run_thread.join(2.0)
            self.assertFalse(
                run_thread.is_alive(),
                "run() waited for a worker that never joins")
            self.assertTrue(client.closed)
            self.assertEqual(upstream_proxy.fileno(), -1)
            # The bounded send timeout releases the stalled sender without the
            # test touching its release event: no worker outlives the session.
            self.assertTrue(
                wait_for(lambda: not relay.worker_threads[1].is_alive(), 3),
                "the stalled sender was not released by the bounded send")
            self.assertFalse(relay.worker_threads[1].is_alive())
        finally:
            client.release.set()
            self.close_relay(relay, run_thread, writer, cancel_thread)
            for sock in (client_peer, upstream_peer):
                with contextlib.suppress(OSError):
                    sock.close()


    def test_close_all_under_backpressure_releases_the_session_slot(self):
        flood = _FloodServer(("127.0.0.1", 0), _FloodHandler)
        flood_thread = threading.Thread(target=flood.serve_forever, daemon=True)
        flood_thread.start()
        closer = None
        try:
            with proxy_server(make_config(
                    vnc_port=flood.server_address[1],
                    max_connections=1)) as (proxy, port):
                sock, _ = _operator_ws(proxy, port)
                try:
                    self.assertIn("101", _read_headers(sock))
                    # Never read the relayed frames: the relay's send stalls.
                    self.assertTrue(wait_for(
                        lambda: proxy.active_connections == 1, 2))
                    time.sleep(0.3)
                    started = time.monotonic()
                    closer = threading.Thread(
                        target=proxy.close_all, daemon=True)
                    closer.start()
                    closer.join(2.0)
                    self.assertFalse(
                        closer.is_alive(),
                        "close_all() blocked on a backpressured relay")
                    self.assertLess(time.monotonic() - started, 1.5)
                    self.assertTrue(
                        wait_for(lambda: proxy.active_connections == 0, 4),
                        "the display session slot was not released")
                finally:
                    sock.close()
        finally:
            if closer is not None:
                closer.join(2)
            flood.shutdown()
            flood.server_close()
            flood_thread.join(3)


class DisplayDeploymentTests(unittest.TestCase):
    def test_display_unit_uses_root_only_credential(self):
        service = (ROOT / "um-cloud-deploy" / "systemd"
                   / "um-share-display.service").read_text(encoding="utf-8")
        self.assertIn("LoadCredential=vnc.passwd:/etc/um-share/vnc.passwd",
                      service)
        self.assertIn("Environment=UM_VNC_AUTH_PATH=%d/vnc.passwd", service)
        self.assertIn("InaccessiblePaths=-/etc/um-share/vnc.passwd", service)
        self.assertNotIn("UM_VNC_PASSWD=", service)

    def test_display_script_is_loopback_and_reads_credential(self):
        script = (DISPLAY_DIR / "start-loopback-display.sh").read_text(
            encoding="utf-8")
        for flag in ("-localhost", "-noipv6", "-rfbauth", "-norc", "-nolookup"):
            self.assertIn(flag, script)
        self.assertIn("CREDENTIALS_DIRECTORY", script)
        self.assertNotIn("UM_VNC_PASSWD=", script)
        # The service namespace hides /etc/um-share/vnc.passwd via
        # InaccessiblePaths, so the credential copy must be selected and the
        # source stat must live only in the manual-run fallback.
        self.assertLess(
            script.index("CREDENTIALS_DIRECTORY"),
            script.index('[ -f "$VNC_AUTH_SOURCE" ]'),
            "the credential copy must be checked before the hidden source")
        self.assertLess(
            script.index('VNC_AUTH_PATH="$VNC_AUTH_SOURCE"'),
            script.index('[ -f "$VNC_AUTH_SOURCE" ]'),
            "the hidden source may only be selected in the fallback branch")
        # systemd's LoadCredential copy is root:root 0440 with an ACL for the
        # service user (measured on Ubuntu 26.04), so the credential copy must
        # accept 0440 while the persistent source stays 0400/0600.
        self.assertIn("400|440|0400|0440", script)

    def test_display_script_starts_a_narrow_auth_helper(self):
        script = (DISPLAY_DIR / "start-loopback-display.sh").read_text(
            encoding="utf-8")
        self.assertIn("vnc-auth-helper.py", script)
        self.assertIn("UM_VNC_AUTH_SOCKET", script)
        self.assertNotIn("vnc.passwd >", script)
        helper = (DISPLAY_DIR / "vnc-auth-helper.py").read_text(
            encoding="utf-8")
        self.assertIn("vnc_auth_response", helper)
        self.assertIn("AF_UNIX", helper)
        self.assertNotIn("print(password", helper)

    def test_fluxbox_rc_pins_one_workspace_and_hides_the_toolbar(self):
        rc = (DISPLAY_DIR / "fluxbox-init").read_text(encoding="utf-8")
        self.assertIn("session.screen0.workspaces: 1", rc)
        self.assertIn("session.screen0.toolbar.visible: false", rc)
        self.assertIn("session.screen0.workspaceNames:", rc)

    def test_display_script_uses_the_repo_fluxbox_rc(self):
        script = (DISPLAY_DIR / "start-loopback-display.sh").read_text(
            encoding="utf-8")
        # The packaged rc is copied to a READ-ONLY runtime copy first: fluxbox
        # REWRITES its -rc file on every start and silently replaced the
        # package's resources with the stock defaults (that is how the live cloud
        # lost the one-workspace setting).
        self.assertIn('FLUXBOX_RC="$SCRIPT_DIR/fluxbox-init"', script)
        self.assertIn('chmod 0444 "$RUNTIME_RC"', script)
        # fluxbox 1.3.7 draws the toolbar for the rc resource alone, so the
        # documented command-line switch must be passed too.
        self.assertIn("-no-toolbar", script)
        self.assertIn('fluxbox -no-toolbar -rc "$FLUXBOX_RC"', script)
        self.assertIn("-shared", script)

    def test_display_script_shims_fbsetbg_so_no_wallpaper_dialog(self):
        # fluxbox runs fbsetbg for the style background.  This minimal image
        # ships no wallpaper setter, so the real fbsetbg pops a CENTERED
        # xmessage dialog that BLOCKS on the OK click and covers the maximized
        # browser, hiding the UM login page.  The script must put no-op
        # fbsetbg/xsetbg shims first in PATH so that lookup succeeds silently.
        script = (DISPLAY_DIR / "start-loopback-display.sh").read_text(
            encoding="utf-8")
        self.assertIn('SHIM_DIR="$(dirname "$AUTH_FILE")/bin"', script)
        self.assertIn("for shim in fbsetbg xsetbg", script)
        self.assertIn('export PATH="$SHIM_DIR:$PATH"', script)
        # The shim must run BEFORE fluxbox starts, otherwise the first start
        # still pops the dialog.
        self.assertLess(script.index('SHIM_DIR='),
                        script.index('if command -v fluxbox'))

    def test_launcher_script_never_prompts_for_a_password(self):
        body = LAUNCHER_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("window.prompt", body)
        self.assertNotIn("sendCredentials", body)
        self.assertIn("/display/grant", body)
        self.assertIn("location.hash", body)
        self.assertIn("history.replaceState", body)
        self.assertIn("/display/session", body)

    def test_console_env_example_has_private_ingress(self):
        example = (ROOT / "um-cloud-deploy" / "config"
                   / "console.env.example").read_text(encoding="utf-8")
        self.assertIn("USAGE_TAILNET_CONFIG_FILE=", example)
        self.assertIn("CONSOLE_DISPLAY_ENABLED=", example)
        self.assertIn("CONSOLE_DISPLAY_NOVNC_ROOT=", example)
        self.assertIn("CONSOLE_DISPLAY_VNC_AUTH_SOCKET=", example)
        self.assertNotIn("CONSOLE_DISPLAY_VNC_PASSWD=", example)

    def test_display_service_exposes_only_the_helper_socket(self):
        service = (ROOT / "um-cloud-deploy" / "systemd"
                   / "um-share-display.service").read_text(encoding="utf-8")
        self.assertIn("UM_VNC_AUTH_SOCKET=/run/um-share/vnc-auth.sock",
                      service)
        self.assertIn("LoadCredential=vnc.passwd", service)
        self.assertIn("InaccessiblePaths=-/etc/um-share/vnc.passwd", service)

    def test_console_service_does_not_gain_vnc_credential(self):
        service = (ROOT / "um-cloud-deploy" / "systemd"
                   / "um-share-console.service").read_text(encoding="utf-8")
        self.assertNotIn("LoadCredential=vnc.passwd", service)
        self.assertNotIn("UM_VNC_AUTH_PATH", service)
        self.assertNotIn("/etc/um-share/vnc.passwd", service)

    def test_console_service_does_not_pull_in_the_display_unit(self):
        """The display is opt-in; console startup must not start it."""
        service = (ROOT / "um-cloud-deploy" / "systemd"
                   / "um-share-console.service").read_text(encoding="utf-8")
        self.assertNotIn("um-share-display.service", service)


class DisplayGrantScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "issue_display_grant", DISPLAY_DIR / "issue-display-grant.py")
        cls.grant = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.grant)

    def write_token(self, text=b"token\n"):
        handle = tempfile.NamedTemporaryFile(delete=False)
        try:
            handle.write(text)
        finally:
            handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def read_with(self, path, *, owner, mode, effective_uid,
                  service_uid=4321):
        info = types.SimpleNamespace(
            st_mode=stat.S_IFREG | mode, st_uid=owner)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                self.grant.os, "stat", return_value=info))
            stack.enter_context(mock.patch.object(
                self.grant, "_service_user_uid", return_value=service_uid,
                create=True))
            stack.enter_context(mock.patch.object(
                self.grant.os, "getuid", return_value=effective_uid,
                create=True))
            stack.enter_context(mock.patch.object(
                self.grant.os, "geteuid", return_value=effective_uid,
                create=True))
            return self.grant.read_control_token(path)

    def test_root_may_read_a_service_user_token(self):
        path = self.write_token(b"service-token\n")
        self.assertEqual(
            self.read_with(path, owner=4321, mode=0o600, effective_uid=0),
            "service-token")

    def test_root_may_read_a_root_token(self):
        path = self.write_token(b"root-token\n")
        self.assertEqual(
            self.read_with(path, owner=0, mode=0o600, effective_uid=0),
            "root-token")

    def test_root_rejects_another_users_token(self):
        path = self.write_token()
        with self.assertRaises(ValueError):
            self.read_with(path, owner=1234, mode=0o600, effective_uid=0)

    def test_non_root_may_read_only_its_own_token(self):
        path = self.write_token(b"caller-token\n")
        self.assertEqual(
            self.read_with(path, owner=1000, mode=0o600, effective_uid=1000),
            "caller-token")
        with self.assertRaises(ValueError):
            self.read_with(path, owner=4321, mode=0o600, effective_uid=1000)

    def test_token_must_stay_0600(self):
        path = self.write_token()
        with self.assertRaises(ValueError):
            self.read_with(path, owner=0, mode=0o644, effective_uid=0)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes are required")
    def test_real_0600_file_is_read(self):
        path = self.write_token(b"real-token\n")
        os.chmod(path, 0o600)
        self.assertEqual(self.grant.read_control_token(path), "real-token")
        os.chmod(path, 0o644)
        with self.assertRaises(ValueError):
            self.grant.read_control_token(path)


    def test_serve_script_is_18096_only(self):
        script = (DISPLAY_DIR / "configure-tailscale-serve.sh").read_text(
            encoding="utf-8")
        self.assertIn(
            "tailscale serve --bg --https=443 http://127.0.0.1:18096", script)
        self.assertIn("tailscale funnel status --json", script)
        self.assertIn("18096|8100|5901", script)

    def test_console_listener_stays_loopback(self):
        source = (ROOT / "usagesrv.py").read_text(encoding="utf-8")
        self.assertIn('HOST = "127.0.0.1"', source)


class TailscaleServePolicyTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(DISPLAY_DIR))
        import tailscale_serve_policy
        self.policy = tailscale_serve_policy

    def tearDown(self):
        sys.path.remove(str(DISPLAY_DIR))

    def exact_status(self):
        return {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {
                "pc.example.ts.net:443": {
                    "Handlers": {
                        "/": {"Proxy": "http://127.0.0.1:18096"},
                    },
                },
            },
            "AllowFunnel": {},
        }

    def test_exact_private_handler(self):
        self.assertEqual(
            self.policy.validate_serve_status(self.exact_status()), [])

    def test_extra_or_public_handlers_are_refused(self):
        cases = (
            {"TCP": {"80": {"HTTPS": True}}},
            {"Web": {"pc.example.ts.net:443": {"Handlers": {
                "/": {"Proxy": "http://127.0.0.1:18097"}}}}},
            {"Web": {"pc.example.ts.net:443": {"Handlers": {
                "/": {"Proxy": "http://127.0.0.1:18096"},
                "/api": {"Proxy": "http://127.0.0.1:18096"}}}}},
            {"AllowFunnel": {"pc.example.ts.net:443": True}},
        )
        for change in cases:
            with self.subTest(change=change):
                document = self.exact_status()
                document.update(change)
                self.assertTrue(self.policy.validate_serve_status(document))

    def test_funnel_status(self):
        self.assertEqual(self.policy.validate_funnel_status({}), [])
        self.assertEqual(self.policy.validate_funnel_status(
            {"AllowFunnel": {"pc.example.ts.net:443": False}}), [])
        self.assertTrue(self.policy.validate_funnel_status(
            {"AllowFunnel": {"pc.example.ts.net:443": True}}))


class TailnetPolicyHardeningTests(unittest.TestCase):
    def test_concrete_tailnet_addresses_only(self):
        for change in (
            {"origin": "https://.ts.net"},
            {"origin": "https://pc.ts.net"},
            {"allowed_peer_ips": ["100.64.0.0"]},
            {"allowed_peer_ips": ["100.127.255.255"]},
            {"allowed_peer_ips": ["fd7a:115c:a1e0::"]},
            {"allowed_peer_ips": ["100.119.187.73", "100.119.187.73"]},
            {"allowed_peer_ips": ["100.64.0.0/10"]},
        ):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    console_tailnet.TailnetPolicy.from_document(
                        dict(DOCUMENT, **change))


if __name__ == "__main__":
    unittest.main()
