"""Control-token boundary for chat2api management and /api/* passthrough.

Real local HTTP server, stub proxy, never touches UM or the token store.
"""

import http.client
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

# Must be set before chat2api is imported: the module resolves the token file
# location from the environment at import time.
_TMP = tempfile.TemporaryDirectory(prefix="chat2api-control-")
os.environ["CHAT2API_CONTROL_TOKEN_FILE"] = os.path.join(_TMP.name, "data", "admin-control-token")

SPEC = importlib.util.spec_from_file_location(
    "chat2api", Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py")
chat2api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(chat2api)


class StubProxy:
    """Only the attributes the tested routes touch; no upstream calls."""

    def __init__(self):
        self.call_log = []
        self.close_response = mock.Mock()

    def auth_status(self):
        return {"state": "valid", "label": "认证有效", "error": None}

    def health_snapshot(self):
        return {"status": "ok"}

    def call(self, method, path, **kwargs):
        self.call_log.append((method, path))
        body = b'{"proxied":true}'
        return SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "application/json",
                     "Content-Length": str(len(body))},
            content=body, close=self.close_response)

    def list_models(self, force=True, raw_only=False, fresh_required=False):
        self.call_log.append(("GET", "/api/models"))
        return {"data": []}


def ensure_module_server(proxy):
    server = chat2api.ThreadingHTTPServer(("127.0.0.1", 0), chat2api.Handler)
    server.proxy = proxy
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class ControlTokenTests(unittest.TestCase):
    def test_token_created_once_and_persisted(self):
        first = chat2api.ensure_control_token(chat2api.CONTROL_TOKEN_FILE)
        self.assertTrue(first)
        self.assertTrue(os.path.exists(chat2api.CONTROL_TOKEN_FILE))
        second = chat2api.ensure_control_token(chat2api.CONTROL_TOKEN_FILE)
        self.assertEqual(first, second)

    def test_unwritable_token_path_denies(self):
        blocker = os.path.join(_TMP.name, "occupied")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("a file, not a directory")
        self.assertIsNone(
            chat2api.ensure_control_token(os.path.join(blocker, "sub", "token")))

    def test_local_control_request_contract(self):
        self.assertTrue(chat2api.is_local_control_request("127.0.0.1", "127.0.0.1:8000", None))
        self.assertTrue(chat2api.is_local_control_request("127.0.0.1", "localhost:8000", None))
        self.assertFalse(chat2api.is_local_control_request("127.0.0.1", "127.0.0.1:8000", "http://x"))
        self.assertFalse(chat2api.is_local_control_request("192.168.1.5", "127.0.0.1:8000", None))
        self.assertFalse(chat2api.is_local_control_request("127.0.0.1", "evil.com", None))


class HandlerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.proxy = StubProxy()
        self.server, self.thread = ensure_module_server(self.proxy)
        self.port = self.server.server_address[1]
        self.token = chat2api.ensure_control_token(chat2api.CONTROL_TOKEN_FILE)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method="GET", path="/health", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_health_stays_open(self):
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_admin_requires_control_token(self):
        status, _ = self.request(path="/admin/auth/status")
        self.assertEqual(status, 403)
        status, _ = self.request(path="/admin/auth/status",
                                 headers={"X-Admin-Control": "wrong"})
        self.assertEqual(status, 403)
        status, body = self.request(path="/admin/auth/status",
                                    headers={"X-Admin-Control": self.token})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["state"], "valid")

    def test_api_wildcard_requires_control_token(self):
        status, _ = self.request(path="/api/version")
        self.assertEqual(status, 403)
        self.assertEqual(self.proxy.call_log, [])  # denied before any upstream use
        status, body = self.request(path="/api/version",
                                    headers={"X-Admin-Control": self.token})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["proxied"], True)
        self.assertEqual(self.proxy.call_log, [("GET", "/api/version")])
        self.proxy.close_response.assert_called_once()

    def test_business_alias_endpoints_stay_open(self):
        # /api/models and /api/chat/completions are /v1-equivalent business
        # routes; the share gateway forwards /v1/* and must keep working.
        status, body = self.request(path="/api/models")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"data": []})
        self.assertEqual(self.proxy.call_log, [("GET", "/api/models")])

    def test_browser_origin_still_denied_on_admin(self):
        status, _ = self.request(path="/admin/auth/status",
                                 headers={"X-Admin-Control": self.token,
                                          "Origin": "http://127.0.0.1:8000"})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
