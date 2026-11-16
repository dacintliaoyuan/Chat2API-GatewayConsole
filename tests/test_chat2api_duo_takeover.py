# -*- coding: utf-8 -*-
"""Ticket #74: the gateway names a Duo push wait instead of a timeout.

The REAL ``Proxy.call`` gets one 401 from a synthetic upstream response and the
shared recovery round reports ``_auth_login.manual_reason=duo_prompt_waiting``.
The request-scoped wait then ends with ``ReauthenticationThrottled`` whose reason
is the Duo wait, and the REAL handler answers 503
``reauthentication_required`` with that detail and ``Retry-After: 30``.

No real browser, upstream, control token or credential is touched.
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

_CONTROL_TMP = tempfile.mkdtemp(prefix="chat2api-duo-takeover-")
os.environ["CHAT2API_CONTROL_TOKEN_FILE"] = os.path.join(
    _CONTROL_TMP, "admin-control-token")

CHAT2API_PATH = Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py"
_SPEC = importlib.util.spec_from_file_location(
    "chat2api_duo_takeover_under_test", CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = chat2api
_SPEC.loader.exec_module(chat2api)

BASE_URL = "https://example.invalid"
STUB_TOKEN = "stub-duo-takeover-token"


class DuoTakeoverGatewayTests(unittest.TestCase):
    def setUp(self):
        slot_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(slot_dir.cleanup)
        slot_patcher = mock.patch.object(
            chat2api, "REAUTH_SLOT_FILE",
            str(Path(slot_dir.name) / "reauth.state"))
        slot_patcher.start()
        self.addCleanup(slot_patcher.stop)

        args = SimpleNamespace(
            base_url=BASE_URL,
            token=STUB_TOKEN,
            profile="profile",
            login_timeout=1,
            use_api_key=False,
            no_browser=False,
        )
        self.proxy = chat2api.Proxy({"token": STUB_TOKEN, "api_key": None},
                                   args)
        self.addCleanup(self.proxy.shutdown)
        self.proxy._auth_login = {
            "phase": "awaiting_duo",
            "origin": "https://api-1234.duosecurity.com/prompt/xyz",
            "manual_reason": chat2api.DUO_PROMPT_WAITING_REASON,
            "detail": None,
        }

    @staticmethod
    def _fake_401(*_args, **_kwargs):
        response = requests.Response()
        response.status_code = 401
        response.raw = io.BytesIO()
        response._content = b'{"detail":"Not authenticated"}'
        return response

    def test_proxy_call_reports_duo_prompt_waiting_not_auth_timeout(self):
        with (
            mock.patch.object(
                chat2api.requests, "request", side_effect=self._fake_401),
            mock.patch.object(
                self.proxy, "refresh",
                side_effect=chat2api._AuthWaitTimeout("budget expired")),
        ):
            with self.assertRaises(chat2api.ReauthenticationThrottled) as caught:
                self.proxy.call(
                    "POST", "/v1/chat/completions", json={}, timeout=5)

        self.assertEqual(caught.exception.reason,
                         chat2api.DUO_PROMPT_WAITING_REASON)

    def test_handler_503_keeps_the_duo_reason_and_retry_after(self):
        class _Handler(chat2api.Handler):
            def do_GET(self):
                try:
                    self.server.proxy.call("GET", "/api/models", timeout=5)
                except chat2api.ReauthenticationThrottled as exc:
                    self._reauthentication_error(exc.reason)

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.proxy = self.proxy
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)

        with (
            mock.patch.object(
                chat2api.requests, "request", side_effect=self._fake_401),
            mock.patch.object(
                self.proxy, "refresh",
                side_effect=chat2api._AuthWaitTimeout("budget expired")),
        ):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(
                    "http://127.0.0.1:%d/" % server.server_port,
                    timeout=5)
            error = caught.exception
            self.addCleanup(error.close)
            self.assertEqual(error.code, 503)
            self.assertEqual(error.headers.get("Retry-After"), "30")
            payload = json.loads(error.read().decode("utf-8"))
        self.assertEqual(payload["error"]["code"],
                         "reauthentication_required")
        self.assertEqual(payload["error"]["detail"],
                         chat2api.DUO_PROMPT_WAITING_REASON)


if __name__ == "__main__":
    unittest.main()
