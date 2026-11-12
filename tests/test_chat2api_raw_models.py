"""Raw-fresh model-catalog contract on chat2api.

Proxy level exercises chat2api.Proxy.list_models freshness semantics (no stale
fallback when fresh_required; raw_only skips virtual aliases; default callers
keep the historical stale fallback + aliases). Handler level exercises the real
HTTP /v1/models with the opt-in header and the freshness response marker.
"""

import http.client
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

_TMP = tempfile.TemporaryDirectory(prefix="chat2api-rawmodels-")
os.environ["CHAT2API_CONTROL_TOKEN_FILE"] = os.path.join(_TMP.name, "data", "admin-control-token")

SPEC = importlib.util.spec_from_file_location(
    "chat2api", Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py")
chat2api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(chat2api)

RAW_FRESH = chat2api.RAW_FRESH_VALUE


def make_proxy(cache_data, upstream_status, upstream_json, fresh_required_result=None):
    p = object.__new__(chat2api.Proxy)  # skip __init__ (edge/browser deps)
    p.models_cache = {"data": cache_data, "at": 0.0}

    def call(method, path, **kwargs):
        return SimpleNamespace(status_code=upstream_status,
                               json=lambda: upstream_json,
                               headers={}, content=b"")

    p.call = call
    return p


class ProxyFreshnessContractTests(unittest.TestCase):
    def setUp(self):
        self._saved_effort = list(chat2api.EFFORT_MODELS)
        self.addCleanup(self._restore_effort)

    def _restore_effort(self):
        chat2api.EFFORT_MODELS[:] = self._saved_effort

    def test_fresh_success_raw_only_returns_upstream_without_aliases(self):
        p = make_proxy(None, 200, {"data": [{"id": "B"}, {"id": "A"}]})
        result = p.list_models(force=True, raw_only=True, fresh_required=True)
        self.assertEqual([m["id"] for m in result["data"]], ["B", "A"])

    def test_fresh_failure_with_fresh_required_returns_none_not_stale(self):
        p = make_proxy({"data": [{"id": "CACHED"}]}, 500, {"error": "down"})
        self.assertIsNone(p.list_models(force=True, raw_only=False, fresh_required=True))

    def test_non_fresh_caller_keeps_stale_fallback(self):
        # A consumer that does NOT use the opt-in keeps the historical behavior:
        # an upstream failure does not surface as an error, the stale cache is
        # served (200-equivalent data) so other consumers are unchanged.
        p = make_proxy({"data": [{"id": "CACHED"}]}, 500, {"error": "down"})
        result = p.list_models(force=True, raw_only=False, fresh_required=False)
        self.assertEqual([m["id"] for m in result["data"]], ["CACHED"])

    def test_default_mode_appends_virtual_aliases(self):
        chat2api.EFFORT_MODELS[:] = ["B"]
        p = make_proxy(None, 200, {"data": [{"id": "B"}]})
        result = p.list_models(force=True, raw_only=False)
        ids = [m["id"] for m in result["data"]]
        self.assertIn("B", ids)
        self.assertIn("B-Fast", ids)
        self.assertIn("B-High", ids)

    def test_raw_only_never_appends_virtual_aliases(self):
        chat2api.EFFORT_MODELS[:] = ["B"]
        p = make_proxy(None, 200, {"data": [{"id": "B"}]})
        result = p.list_models(force=True, raw_only=True)
        self.assertEqual([m["id"] for m in result["data"]], ["B"])


class RawFreshStubProxy:
    def __init__(self):
        self.last_kwargs = {}
        self.fail = False

    def list_models(self, force=False, raw_only=False, fresh_required=False,
                    auth_wait_budget=None, request_deadline=None,
                    cancel_check=None):
        self.last_kwargs = {
            "force": force, "raw_only": raw_only,
            "fresh_required": fresh_required,
            "auth_wait_budget": auth_wait_budget,
            "request_deadline": request_deadline,
            "cancel_check": cancel_check,
        }
        if self.fail:
            return None
        return {"object": "list", "data": [{"id": "raw-a"}, {"id": "raw-b"}]}


def ensure_server(proxy):
    server = chat2api.ThreadingHTTPServer(("127.0.0.1", 0), chat2api.Handler)
    server.proxy = proxy
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class HandlerRawFreshTests(unittest.TestCase):
    def setUp(self):
        self.proxy = RawFreshStubProxy()
        self.server, self.thread = ensure_server(self.proxy)
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path="/v1/models", header_value=None):
        headers = {}
        if header_value is not None:
            headers["X-Chat2Api-Models"] = header_value
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_opt_in_returns_marker_and_raw_only(self):
        status, headers, body = self.request(header_value=RAW_FRESH)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Chat2Api-Raw-Models"), "1")
        self.assertTrue(self.proxy.last_kwargs["raw_only"])
        self.assertTrue(self.proxy.last_kwargs["fresh_required"])
        self.assertEqual(json.loads(body)["data"], [{"id": "raw-a"}, {"id": "raw-b"}])

    def test_without_opt_in_no_marker_and_not_raw_only(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertNotIn("X-Chat2Api-Raw-Models", headers)
        self.assertFalse(self.proxy.last_kwargs["raw_only"])
        self.assertFalse(self.proxy.last_kwargs["fresh_required"])

    def test_opt_in_upstream_failure_is_502(self):
        self.proxy.fail = True
        status, _, body = self.request(header_value=RAW_FRESH)
        self.assertEqual(status, 502)


if __name__ == "__main__":
    unittest.main()
