# -*- coding: utf-8 -*-
"""Restricted credential-verify endpoint tests (console P2 seam B / R1).

Covers the change-ticket contract for chat2api's ``GET /admin/auth/verify``
and its ``classify_credential`` probe: single-shot upstream classification
with ZERO side effects (no refresh, no _auth_state writes, no model-cache
seeding). All upstream behaviour is a stub server or patched requests.get;
no real upstream, no real login, no billing request is ever issued.

The fixture matrix mirrors the agreed contract (work ticket E-P2-3):
local-no-credential, valid catalog, invalid catalog shapes, 401/403,
500/502, timeout, connection error, non-JSON 200, wrong token 403 with
zero upstream calls, Origin-denied 403 with zero upstream calls.
"""

import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="chat2api-verify-tests-")
os.environ["CHAT2API_CONTROL_TOKEN_FILE"] = os.path.join(
    _TMP, "admin-control-token")
_CONTROL_TOKEN = "verify-endpoint-test-token"
with open(os.environ["CHAT2API_CONTROL_TOKEN_FILE"], "w",
          encoding="utf-8") as _f:
    _f.write(_CONTROL_TOKEN)

_HERE = os.path.dirname(os.path.abspath(__file__))
_CHAT2API = os.path.join(os.path.dirname(_HERE), "chat2api", "chat2api.py")
_SPEC = importlib.util.spec_from_file_location("chat2api_verify_under_test",
                                               _CHAT2API)
chat2api = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = chat2api
_SPEC.loader.exec_module(chat2api)

UPSTREAM_BASE = "http://upstream.test"


class _StubProxy:
    """Minimal proxy stand-in: bearer/base/state, nothing else."""

    def __init__(self, bearer_value="token-abc"):
        self._bearer = bearer_value
        self.base_url = UPSTREAM_BASE
        self.auth_state_pokes = 0
        self._auth_state = "valid"
        self._auth_error = None
        self.models_cache = {"at": 0.0, "data": {"data": [{"id": "seed"}]}}

    def bearer(self):
        return self._bearer

    def auth_status(self):
        self.auth_state_pokes += 1
        return {"state": self._auth_state, "label": "认证有效",
                "error": self._auth_error}


def _response(status, body=b"", content_type="application/json"):
    resp = mock.Mock()
    resp.status_code = status
    resp.json.side_effect = ValueError("no json") if body == b"" else (
        lambda: json.loads(body.decode("utf-8")))
    resp.text = body.decode("utf-8", errors="replace")
    return resp


class ClassifyCredentialTests(unittest.TestCase):
    def setUp(self):
        self.proxy = _StubProxy()

    def run_classify(self, status=None, body=b"", exc=None,
                     bearer="token-abc"):
        payload = {"base_url": UPSTREAM_BASE, "bearer": bearer}
        if exc is not None:
            patcher = mock.patch.object(
                chat2api.requests, "get", side_effect=exc)
        else:
            patcher = mock.patch.object(
                chat2api.requests, "get", return_value=_response(status,
                                                                 body))
        with patcher as mocked:
            result = chat2api.classify_credential(**payload)
        return result, mocked

    def test_local_no_credential_needs_login_without_upstream(self):
        result, mocked = self.run_classify(bearer="")
        self.assertEqual(result, ("needs_login", "no_local_credential",
                                  None))
        self.assertEqual(mocked.call_count, 0)

    def test_valid_catalog_shape(self):
        result, mocked = self.run_classify(
            200, json.dumps({"data": [{"id": "m1"}]}).encode("utf-8"))
        self.assertEqual(result, ("valid", None, 200))
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(mocked.call_args[0][0],
                         UPSTREAM_BASE + "/api/models")

    def test_invalid_catalog_shapes_are_unknown(self):
        for body in (b'{"foo": 1}', b'{"data": "x"}', b"[1, 2]"):
            with self.subTest(body=body):
                result, _mocked = self.run_classify(200, body)
                self.assertEqual(result[0], "unknown")
                self.assertEqual(result[1], "invalid_catalog_shape")
                self.assertEqual(result[2], 200)

    def test_rejected_on_401_and_403(self):
        for status in (401, 403):
            with self.subTest(status=status):
                result, _mocked = self.run_classify(status)
                self.assertEqual(result, ("rejected", "upstream_rejected",
                                          status))

    def test_upstream_5xx_is_unknown(self):
        for status in (500, 502):
            with self.subTest(status=status):
                result, _mocked = self.run_classify(status)
                self.assertEqual(result, ("unknown", "upstream_5xx", status))

    def test_timeout_and_connection_error_are_unknown(self):
        import requests as real_requests
        for exc in (real_requests.Timeout("slow"),
                    real_requests.ConnectionError("refused")):
            with self.subTest(exc=type(exc).__name__):
                result, _mocked = self.run_classify(exc=exc)
                self.assertEqual(result[0], "unknown")
                self.assertIsNone(result[2])

    def test_non_json_200_is_unknown(self):
        result, _mocked = self.run_classify(200, b"<html>not json</html>")
        self.assertEqual(result, ("unknown", "non_json_response", 200))

    def test_probe_never_mutates_proxy_state(self):
        before_cache = dict(self.proxy.models_cache)
        self.run_classify(401)
        self.assertEqual(self.proxy.auth_state_pokes, 0)
        self.assertEqual(self.proxy.models_cache, before_cache)
        self.assertEqual(self.proxy._auth_state, "valid")


class _RouteHarness:
    """Real chat2api Handler over a real ephemeral HTTP server."""

    def __init__(self, proxy):
        self.proxy = proxy
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          chat2api.Handler)
        self.server.proxy = proxy
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class VerifyRouteTests(unittest.TestCase):
    def setUp(self):
        self.proxy = _StubProxy()
        self.harness = _RouteHarness(self.proxy)
        self.addCleanup(self.harness.close)

    def test_verify_route_classifies_upstream_401(self):
        with mock.patch.object(chat2api.requests, "get",
                               return_value=_response(401)):
            status, raw = self.harness.get(
                "/admin/auth/verify",
                {"X-Admin-Control": _CONTROL_TOKEN})
        self.assertEqual(status, 200)
        payload = json.loads(raw.decode("utf-8"))
        self.assertEqual(payload["verify"], "rejected")
        self.assertEqual(payload["reason"], "upstream_rejected")
        self.assertEqual(payload["upstream_status"], 401)
        self.assertIsInstance(payload["checked_epoch"], int)
        self.assertIn("note", payload)
        # The bearer value never appears anywhere in the response.
        self.assertNotIn("token-abc", raw.decode("utf-8"))
        self.assertEqual(self.proxy.auth_state_pokes, 0)

    def test_verify_route_needs_login_without_bearer(self):
        self.proxy._bearer = ""
        status, raw = self.harness.get(
            "/admin/auth/verify", {"X-Admin-Control": _CONTROL_TOKEN})
        self.assertEqual(status, 200)
        payload = json.loads(raw.decode("utf-8"))
        self.assertEqual(payload["verify"], "needs_login")
        self.assertEqual(payload["reason"], "no_local_credential")
        self.assertIsNone(payload["upstream_status"])

    def test_wrong_token_is_403_with_zero_upstream(self):
        with mock.patch.object(chat2api.requests, "get") as mocked:
            status, _raw = self.harness.get(
                "/admin/auth/verify", {"X-Admin-Control": "wrong-token"})
        self.assertEqual(status, 403)
        self.assertEqual(mocked.call_count, 0)

    def test_origin_header_denied_with_zero_upstream(self):
        with mock.patch.object(chat2api.requests, "get") as mocked:
            status, _raw = self.harness.get(
                "/admin/auth/verify",
                {"X-Admin-Control": _CONTROL_TOKEN,
                 "Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(mocked.call_count, 0)

    def test_unknown_verify_path_still_404(self):
        status, _raw = self.harness.get(
            "/admin/auth/no-such",
            {"X-Admin-Control": _CONTROL_TOKEN})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
