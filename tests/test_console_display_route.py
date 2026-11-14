# -*- coding: utf-8 -*-
"""The private display surface must be reachable through the real 18096 handler.

These tests bind usagesrv's real ThreadingHTTPServer on an ephemeral loopback
port and exercise the same do_GET/_console_share_api paths production uses.  The
VNC endpoint is never contacted; the launcher file and noVNC root are temp files.
"""
import json
import os
import base64
import socket
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

import usagesrv
from console.display_proxy import DisplayConfig, DisplayProxy
from share.secret_protection import write_private_secret_path

_TMP = tempfile.mkdtemp(prefix="display-route-")
_TOKEN_FILE = os.path.join(_TMP, "control-token")
_TOKEN = "test-display-route-token"
write_private_secret_path(_TOKEN_FILE, _TOKEN.encode("utf-8"))
usagesrv.CONTROL_TOKEN_FILE = _TOKEN_FILE


class DisplayRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.novnc = Path(_TMP) / "novnc"
        (cls.novnc / "core").mkdir(parents=True)
        (cls.novnc / "core" / "rfb.js").write_text("/* rfb */", encoding="utf-8")
        cls.launcher = Path(_TMP) / "launcher.html"
        cls.launcher.write_text("<!doctype html><title>display</title>", encoding="utf-8")
        cls.launcher_script = Path(_TMP) / "novnc-launcher.js"
        cls.launcher_script.write_text("/* launcher */", encoding="utf-8")
        cls.server = usagesrv.ThreadingHTTPServer(("127.0.0.1", 0), usagesrv.Handler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.old_port = usagesrv.PORT
        usagesrv.PORT = cls.port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        usagesrv.PORT = cls.old_port

    def setUp(self):
        self.proxy = DisplayProxy(
            DisplayConfig(
                True, "127.0.0.1", 5901, str(self.novnc), str(self.launcher),
                2, 900.0, 14400.0, 10.0),
            session_valid=usagesrv.session_valid,
            control_token_ok=usagesrv.control_token_ok,
        )
        self.open_proxy = DisplayProxy(
            DisplayConfig(
                True, "127.0.0.1", 5901, str(self.novnc), str(self.launcher),
                2, 900.0, 14400.0, 10.0),
            session_valid=lambda _cookie: True,
            control_token_ok=None,
        )
        self.identity_proxy = DisplayProxy(
            DisplayConfig(
                True, "127.0.0.1", 5901, str(self.novnc), str(self.launcher),
                2, 900.0, 14400.0, 10.0),
            session_valid=lambda _cookie: True,
            control_token_ok=usagesrv.control_token_ok,
        )

    def _request(self, path, headers=None, method="GET", body=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def _ws_request(self, headers):
        values = {
            "Host": f"127.0.0.1:{self.port}",
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Version": "13",
            "Sec-WebSocket-Key": base64.b64encode(
                b"0123456789abcdef").decode(),
            "Sec-WebSocket-Protocol": "binary",
            "Origin": f"http://127.0.0.1:{self.port}",
        }
        values.update(headers)
        lines = ["GET /display/websockify HTTP/1.1"]
        for name, value in values.items():
            if value is not None:
                lines.append(f"{name}: {value}")
        lines.extend(("", ""))
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        sock.sendall("\r\n".join(lines).encode("latin-1"))
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        sock.close()
        return data.decode("latin-1")

    def test_display_session_disabled_is_honest(self):
        with mock.patch.object(usagesrv, "DISPLAY_PROXY", None):
            status, _, body = self._request(
                "/api/console/share-management/display-session",
                {"X-Admin-Control": _TOKEN})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertFalse(payload["available"])
        self.assertIsNone(payload["url"])
        self.assertEqual(payload["reason"], "display_proxy_unavailable")
        self.assertIsNone(payload["expires_at"])

    def test_display_session_enabled_returns_same_origin_path(self):
        with mock.patch.object(usagesrv, "DISPLAY_PROXY", self.proxy):
            status, _, body = self._request(
                "/api/console/share-management/display-session",
                {"X-Admin-Control": _TOKEN})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["available"])
        self.assertTrue(payload["url"].startswith("/display/grant#grant="))
        self.assertNotIn("?", payload["url"])
        self.assertEqual(payload["mode"], "novnc")
        self.assertIsInstance(payload["expires_at"], int)

    def test_display_uses_console_origin_trust(self):
        with mock.patch.object(usagesrv, "DISPLAY_PROXY", self.open_proxy):
            status, _, body = self._request("/display/")
        self.assertEqual(status, 200)
        self.assertIn(b"display", body)

    def test_display_serves_launcher_with_same_origin_csp(self):
        with mock.patch.object(usagesrv, "DISPLAY_PROXY", self.proxy):
            status, headers, body = self._request(
                "/display/", {"X-Admin-Control": _TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "text/html; charset=utf-8")
        self.assertIn("connect-src 'self'", headers.get("Content-Security-Policy", ""))
        self.assertNotIn("ws:", headers.get("Content-Security-Policy", ""))
        self.assertIn(b"display", body)

    def test_display_serves_launcher_script_with_same_origin_csp(self):
        with mock.patch.object(usagesrv, "DISPLAY_PROXY", self.proxy):
            status, headers, body = self._request(
                "/display/launcher.js", {"X-Admin-Control": _TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "text/javascript; charset=utf-8")
        self.assertEqual(body, b"/* launcher */")
        csp = headers.get("Content-Security-Policy", "")
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)

    def test_display_disabled_returns_503_not_static_404(self):
        disabled = DisplayProxy(
            DisplayConfig(error="display_novnc_missing"),
            session_valid=usagesrv.session_valid,
            control_token_ok=usagesrv.control_token_ok,
        )
        with mock.patch.object(usagesrv, "DISPLAY_PROXY", disabled):
            status, _, body = self._request(
                "/display/", {"X-Admin-Control": _TOKEN})
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["error"], "display_novnc_missing")

    def test_display_session_without_admin_is_not_available(self):
        with mock.patch.object(usagesrv, "DISPLAY_PROXY", self.proxy):
            status, _, body = self._request(
                "/api/console/share-management/display-session")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason"], "display_authorization_required")

    def test_display_grant_requires_admin_and_is_single_use(self):
        path = "/api/console/share-management/display-grant"
        with mock.patch.object(usagesrv, "DISPLAY_PROXY", self.proxy):
            status, _, _ = self._request(
                path, {"X-Requested-With": "XMLHttpRequest"}, method="POST")
            self.assertEqual(status, 403)
            status, _, _ = self._request(
                path, {"X-Requested-With": "XMLHttpRequest"},
                method="POST", body=b"")
            self.assertEqual(status, 403)
            status, _, body = self._request(
                path, {"X-Admin-Control": _TOKEN,
                       "X-Requested-With": "XMLHttpRequest"},
                method="POST")
            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertTrue(payload["grant"])
            self.assertNotIn(payload["grant"], payload["url"].split("#")[0])
            # The returned grant is single-use at the display exchange.
            status, _, _ = self._request(
                "/display/grant",
                {"Content-Type": "application/json",
                 "X-Requested-With": "XMLHttpRequest"},
                method="POST", body=json.dumps({"grant": payload["grant"]}))
            self.assertEqual(status, 204)
            status, _, _ = self._request(
                "/display/grant",
                {"Content-Type": "application/json",
                 "X-Requested-With": "XMLHttpRequest"},
                method="POST", body=json.dumps({"grant": payload["grant"]}))
            self.assertEqual(status, 403)  # replay is refused

    def test_display_grant_tailscale_identity(self):
        policy = usagesrv.TailnetPolicy.from_document({
            "schema_version": 1, "enabled": True,
            "origin": "https://pc.example.ts.net:8001",
            "allowed_login": "owner@example.com",
            "allowed_peer_ips": ["100.127.119.98"],
        })
        remote = {
            "Host": "pc.example.ts.net:8001",
            "X-Forwarded-Host": "pc.example.ts.net:8001",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "100.127.119.98",
            "Tailscale-User-Login": "owner@example.com",
            "Origin": "https://pc.example.ts.net:8001",
            "X-Requested-With": "XMLHttpRequest",
        }
        path = "/api/console/share-management/display-grant"
        old = usagesrv.TAILNET_POLICY
        usagesrv.TAILNET_POLICY = policy
        try:
            with mock.patch.object(usagesrv, "DISPLAY_PROXY",
                                  self.identity_proxy):
                status, _, body = self._request(path, remote, method="POST")
                self.assertEqual(status, 200)
                self.assertTrue(json.loads(body)["grant"])
                forged = dict(remote, **{
                    "Tailscale-User-Login": "someone@example.com"})
                status, _, _ = self._request(path, forged, method="POST")
                self.assertEqual(status, 403)
                forged = dict(remote, **{"X-Forwarded-For": "100.71.250.7"})
                status, _, _ = self._request(path, forged, method="POST")
                self.assertEqual(status, 403)
                missing = dict(remote)
                missing.pop("X-Forwarded-For")
                status, _, _ = self._request(path, missing, method="POST")
                self.assertEqual(status, 403)
        finally:
            usagesrv.TAILNET_POLICY = old

    def test_display_ws_without_display_session_is_403(self):
        with mock.patch.object(usagesrv, "DISPLAY_PROXY", self.proxy):
            response = self._ws_request({})
        self.assertIn("403", response)

    def test_display_ws_with_forged_tailscale_header_is_403(self):
        with mock.patch.object(usagesrv, "DISPLAY_PROXY", self.proxy):
            response = self._ws_request({
                "Tailscale-User-Login": "owner@example.com",
                "X-Forwarded-For": "100.127.119.98",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": f"pc.example.ts.net:{self.port}",
            })
        self.assertIn("403", response)


class _FakeOperatorDisplay:
    """Route-level double for the display operator/read seams."""

    def __init__(self, *, state=None, result=None):
        self.config = DisplayConfig(
            True, "127.0.0.1", 5901, _TMP, os.path.join(_TMP, "launcher.html"),
            2, 900.0, 14400.0, 10.0)
        self.session_valid = usagesrv.session_valid
        self.control_token_ok = usagesrv.control_token_ok
        self.state = state or {"owned_by_me": False, "owner_present": True,
                              "generation": 2, "stale": False}
        self.result = result or {"acquired": True, "generation": 3,
                                 "took_over": False}
        self.calls = []

    def _display_authorized(self, headers, origin):
        return True

    def operator_state(self, cookie_header=None, origin=None):
        return dict(self.state)

    def acquire_operator(self, cookie_header=None, origin=None,
                         action="acquire"):
        self.calls.append(action)
        return dict(self.result)


class DisplayStateRouteTest(unittest.TestCase):
    """D18-7/D18-8/D18-9 console read/write routes."""

    @classmethod
    def setUpClass(cls):
        cls.server = usagesrv.ThreadingHTTPServer(("127.0.0.1", 0),
                                                 usagesrv.Handler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.old_port = usagesrv.PORT
        usagesrv.PORT = cls.port
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        usagesrv.PORT = cls.old_port

    def _request(self, path, headers=None, method="GET", body=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def _state(self, *, display=None):
        proxy = _FakeOperatorDisplay()
        with mock.patch.object(usagesrv, "DISPLAY_PROXY", proxy), \
\
                mock.patch.object(
                    usagesrv.Handler, "_console_display_availability",
                    return_value=display or {"available": True,
                                            "reason": None}):
            status, _, body = self._request(
                "/api/console/share-management/display-state",
                {"X-Admin-Control": _TOKEN})
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_display_state_not_started_and_disconnected_are_separate(self):
        payload = self._state()
        self.assertEqual(payload["state"], "not_started")
        payload = self._state(
            display={"available": False, "reason": "display_disabled"})
        self.assertEqual(payload["state"], "display_disconnected")
        self.assertEqual(payload["display"], {
            "available": False, "reason": "display_disabled"})

    def test_display_state_is_a_read_without_a_share_manager(self):
        with mock.patch.object(usagesrv, "DISPLAY_PROXY", None), \
                mock.patch.object(usagesrv, "get_share_manager",
                                  side_effect=AssertionError(
                                      "a read must not assemble the manager")):
            status, _, body = self._request(
                "/api/console/share-management/display-state")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["state"], "display_disconnected")
        self.assertEqual(payload["display"]["reason"],
                         "display_proxy_unavailable")

    def test_display_operator_acquire_and_conflicts(self):
        proxy = _FakeOperatorDisplay()
        with mock.patch.object(usagesrv, "DISPLAY_PROXY", proxy):
            status, _, body = self._request(
                "/api/console/share-management/display-operator",
                {"X-Requested-With": "XMLHttpRequest",
                 "Content-Type": "application/json"},
                method="POST", body=json.dumps({"action": "acquire"}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {
            "acquired": True, "generation": 3, "took_over": False})
        self.assertEqual(proxy.calls, ["acquire"])

        proxy.result = {"acquired": False,
                        "error": "display_operator_exists",
                        "generation": 2, "owner_label": "另一页"}
        with mock.patch.object(usagesrv, "DISPLAY_PROXY", proxy):
            status, _, body = self._request(
                "/api/console/share-management/display-operator",
                {"X-Requested-With": "XMLHttpRequest",
                 "Content-Type": "application/json"},
                method="POST", body=json.dumps({"action": "acquire"}))
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body), {
            "error": "display_operator_exists", "generation": 2,
            "owner_label": "另一页"})

        proxy.result = {"acquired": False,
                        "error": "display_session_required"}
        with mock.patch.object(usagesrv, "DISPLAY_PROXY", proxy):
            status, _, body = self._request(
                "/api/console/share-management/display-operator",
                {"X-Requested-With": "XMLHttpRequest",
                 "Content-Type": "application/json"},
                method="POST", body=json.dumps({"action": "takeover"}))
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body), {
            "error": "display_session_required"})

    def test_display_operator_validates_the_action_and_the_write_gate(self):
        proxy = _FakeOperatorDisplay()
        with mock.patch.object(usagesrv, "DISPLAY_PROXY", proxy):
            status, _, _ = self._request(
                "/api/console/share-management/display-operator",
                {"Content-Type": "application/json"},
                method="POST", body=json.dumps({"action": "acquire"}))
            self.assertEqual(status, 403)
            status, _, body = self._request(
                "/api/console/share-management/display-operator",
                {"X-Requested-With": "XMLHttpRequest",
                 "Content-Type": "application/json"},
                method="POST", body=json.dumps({"action": "steal"}))
            self.assertEqual(status, 400)
            self.assertEqual(json.loads(body)["error"],
                             "display_operator_action_invalid")
            status, _, _ = self._request(
                "/api/console/share-management/display-operator",
                {"X-Requested-With": "XMLHttpRequest",
                 "Content-Type": "application/json"},
                method="POST", body=json.dumps({"action": "acquire",
                                                "force": True}))
            self.assertEqual(status, 400)
        self.assertEqual(proxy.calls, [])



if __name__ == "__main__":
    unittest.main()
