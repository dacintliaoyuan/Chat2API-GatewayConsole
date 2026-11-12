# -*- coding: utf-8 -*-
"""Merged 8100 process: the share gate lives in chat2api.py."""

import hashlib
import http.client
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from email.message import Message
from http.server import ThreadingHTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve()
_CHAT2API_DIR = _HERE.parents[1] / "chat2api"
if str(_CHAT2API_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT2API_DIR))
_SPEC = importlib.util.spec_from_file_location(
    "chat2api_merged_probe", _CHAT2API_DIR / "chat2api.py")
chat2api = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = chat2api
_SPEC.loader.exec_module(chat2api)
from share_entry import ShareGate


def _headers(secret):
    headers = Message()
    headers["Authorization"] = "Bearer " + secret
    return headers


class _Ctx:
    def __init__(self):
        self.closed = False

    def terminate(self):
        self.closed = True
        return True


class _FakeProxy:
    def list_models(self, **_kwargs):
        return {"object": "list", "data": []}


class ShareGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "share.json"
        self.config.write_text(json.dumps({
            "clients": {
                "friend": hashlib.sha256(b"secret").hexdigest(),
            },
        }), encoding="utf-8")
        self.gate = ShareGate(self.config)

    def test_authenticates_the_key_and_enforces_per_key_concurrency(self):
        auth = self.gate.authenticate(_headers("secret"), allow_api_key=True)
        self.assertIsNotNone(auth)
        entry, _cfg = auth
        self.assertEqual(entry["key_id"], "friend")
        self.assertTrue(self.gate.acquire("friend", 0, 1))
        self.assertFalse(self.gate.acquire("friend", 0, 1))
        self.gate.release("friend")
        self.assertTrue(self.gate.acquire("friend", 0, 1))

    def test_unknown_key_is_refused(self):
        self.assertIsNone(
            self.gate.authenticate(_headers("wrong"), allow_api_key=True))

    def test_terminate_closes_the_key_inflight_contexts(self):
        ctx = _Ctx()
        self.gate.register_inflight("friend", ctx)
        self.assertEqual(self.gate.terminate_key("friend"), 1)
        self.assertTrue(ctx.closed)

    def test_share_body_limit_is_32_mib(self):
        self.assertEqual(chat2api.SHARE_MAX_REQUEST_BYTES, 32 * 1024 * 1024)


class MergedHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = Path(self.tmp.name) / "share.json"
        config.write_text(json.dumps({
            "clients": {
                "friend": hashlib.sha256(b"secret").hexdigest(),
            },
        }), encoding="utf-8")
        original_log = chat2api.Handler.log_message
        chat2api.Handler.log_message = lambda *_args: None
        self.addCleanup(setattr, chat2api.Handler, "log_message", original_log)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), chat2api.Handler)
        self.server.proxy = _FakeProxy()
        self.server.share_gate = ShareGate(config)
        self.server.local_api_key = ""
        self.server.require_local_api_key = False
        self.thread = threading.Thread(target=self.server.serve_forever,
                                        daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _request(self, headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            connection.request("GET", "/v1/models", headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            connection.close()

    def test_models_requires_the_share_key(self):
        status, _payload = self._request()
        self.assertEqual(status, 401)

    def test_models_accepts_the_authenticated_share_key(self):
        status, payload = self._request(
            {"Authorization": "Bearer secret"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"], [])


if __name__ == "__main__":
    unittest.main()
