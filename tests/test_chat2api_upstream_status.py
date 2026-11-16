# -*- coding: utf-8 -*-
"""Upstream status preservation and error-body rebuilding (ticket #60).

The upstream HTTP status is the primary contract: 4xx/5xx surface unchanged
on Chat, Responses and Messages (Messages in the Anthropic envelope), while 401
still becomes the gateway's own 503 reauthentication error. Connection failures
are 502 and the gateway's own waits (response headers, first byte, total budget)
are 504. Retry exhaustion returns the LAST upstream status; a last transport
failure is 502, and a deadline reached while waiting to retry is 504 with
``detail.last_upstream_status``.
"""

import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _chat2api_http_harness as harness  # noqa: E402

CHAT2API_PATH = Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py"
SPEC = importlib.util.spec_from_file_location(
    "chat2api_upstream_status", CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)

STUB_TOKEN = "stub-token"
LOCAL_KEY = "local-secret-upstream-status"

# Statuses named by the ticket's acceptance criteria. 401 is handled separately
# because it must become the gateway's 503 reauthentication error.
PRESERVED_STATUSES = (403, 404, 409, 413, 415, 429, 500, 502, 503, 504)


class ScriptedUpstreamHandler(BaseHTTPRequestHandler):
    """Scripted fake UM: one behavior per conversation request.

    ``behaviors`` is consumed in order; the last entry repeats once the script
    is exhausted. An integer entry is an HTTP status with a JSON body whose
    ``detail`` names that status. ``"close"`` drops the connection without a
    response (the transport-error case). ``"delay_headers"`` waits before any
    response byte. ``"silent_stream"`` sends SSE headers and then no data.
    """

    protocol_version = "HTTP/1.1"
    behaviors = [200]
    delay_headers_s = 0.0
    silent_s = 1.0
    posts = 0
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def _send_json(self, code, obj):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        self.close_connection = True

    def _next_behavior(self):
        with ScriptedUpstreamHandler.lock:
            index = ScriptedUpstreamHandler.posts
            ScriptedUpstreamHandler.posts += 1
        behaviors = ScriptedUpstreamHandler.behaviors
        return behaviors[min(index, len(behaviors) - 1)]

    def _discard_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

    def do_GET(self):
        behavior = self._next_behavior()
        if behavior == "close":
            self.close_connection = True
            return
        if isinstance(behavior, int) and behavior >= 400:
            self._send_json(behavior,
                            {"detail": f"upstream status {behavior}"})
            return
        self._send_json(200, {"ok": True})

    def do_POST(self):
        path = self.path.split("?")[0]
        self._discard_body()
        behavior = self._next_behavior()
        if behavior == "close":
            self.close_connection = True
            return
        if behavior == "delay_headers":
            time.sleep(ScriptedUpstreamHandler.delay_headers_s)
            self._send_json(200, {"ok": True})
            return
        if behavior == "silent_stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                time.sleep(ScriptedUpstreamHandler.silent_s)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            self.close_connection = True
            return
        if isinstance(behavior, int) and behavior >= 400:
            self._send_json(behavior,
                            {"detail": f"upstream status {behavior}"})
            return
        self._send_json(200, {"ok": True})


class UpstreamStatusTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir, self.usage_log = harness.install_usage_isolation(
            self, chat2api)
        harness.assert_stub_credentials(
            chat2api, local_key=None, upstream_token=STUB_TOKEN,
            stub_token_literal=STUB_TOKEN)

        ScriptedUpstreamHandler.behaviors = [200]
        ScriptedUpstreamHandler.posts = 0
        ScriptedUpstreamHandler.delay_headers_s = 0.0
        ScriptedUpstreamHandler.silent_s = 1.0

        stub = harness.TrackedHTTPServer(
            ("127.0.0.1", 0), ScriptedUpstreamHandler)
        self.stub = stub
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        harness.install_tracked_server(
            self, stub, name="upstream-stub", require_drained=False)

        self.start_gateway(f"http://127.0.0.1:{stub.server_port}")

        for patch in (
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", False),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 0),
            mock.patch.object(chat2api, "UPSTREAM_SLOW_AUTO_RETRIES", 0),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRY", False),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRIES", 0),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def start_gateway(self, base_url):
        args = SimpleNamespace(
            base_url=base_url,
            token=STUB_TOKEN,
            profile="profile",
            login_timeout=1,
            use_api_key=False,
            no_browser=True,
        )
        self.proxy = chat2api.Proxy({"token": STUB_TOKEN}, args)
        server = harness.TrackedHTTPServer(("127.0.0.1", 0),
                                          chat2api.Handler)
        server.proxy = self.proxy
        threading.Thread(target=server.serve_forever, daemon=True).start()
        harness.install_tracked_server(
            self, server, name="chat2api-gateway")
        self.base = f"http://127.0.0.1:{server.server_port}"

    def post_chat(self, stream=False, timeout=15):
        return requests.post(
            f"{self.base}/v1/chat/completions",
            json={"model": "stub-model",
                  "messages": [{"role": "user", "content": "hi"}],
                  "stream": stream, "max_tokens": 16},
            timeout=timeout)

    def post_messages(self, stream=False, timeout=15):
        return requests.post(
            f"{self.base}/v1/messages",
            json={"model": "stub-model", "max_tokens": 16,
                  "messages": [{"role": "user", "content": "hi"}],
                  "stream": stream},
            timeout=timeout)

    def post_responses(self, stream=False, timeout=15):
        return requests.post(
            f"{self.base}/v1/responses",
            json={"model": "stub-model", "input": "hi", "stream": stream},
            timeout=timeout)

    def assert_upstream_status(self, response, expected):
        self.assertEqual(response.status_code, expected)
        error = response.json()["error"]
        self.assertEqual(error["detail"]["upstream_status"], expected)
        self.assertEqual(error["detail"]["upstream_message"],
                         f"upstream status {expected}")

    def enable_retries(self, retries=2):
        for patch in (
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", retries),
            mock.patch.object(chat2api, "UPSTREAM_SLOW_AUTO_RETRIES", retries),
            mock.patch.object(chat2api, "_retry_delay_seconds",
                              return_value=0.0),
        ):
            patch.start()
            self.addCleanup(patch.stop)


class TestPreservedUpstreamStatuses(UpstreamStatusTestBase):
    def test_chat_preserves_each_upstream_status(self):
        for status in PRESERVED_STATUSES:
            with self.subTest(status=status):
                ScriptedUpstreamHandler.behaviors = [status]
                ScriptedUpstreamHandler.posts = 0
                response = self.post_chat()
                try:
                    self.assert_upstream_status(response, status)
                    expected_type = (
                        "permission_error" if status == 403 else
                        "not_found_error" if status == 404 else
                        "rate_limit_error" if status == 429 else
                        "invalid_request_error"
                        if status in (400, 405, 409, 413, 415, 422) else
                        "upstream_error")
                    self.assertEqual(response.json()["error"]["type"],
                                     expected_type)
                finally:
                    response.close()

    def test_messages_preserves_each_upstream_status_in_anthropic_shape(self):
        for status in PRESERVED_STATUSES:
            with self.subTest(status=status):
                ScriptedUpstreamHandler.behaviors = [status]
                ScriptedUpstreamHandler.posts = 0
                response = self.post_messages()
                try:
                    self.assertEqual(response.status_code, status)
                    body = response.json()
                    self.assertEqual(body["type"], "error")
                    error = body["error"]
                    self.assertEqual(error["detail"]["upstream_status"],
                                     status)
                    self.assertEqual(error["detail"]["upstream_message"],
                                     f"upstream status {status}")
                    self.assertIn("type", error)
                    self.assertIn("code", error)
                finally:
                    response.close()

    def test_responses_preserves_each_upstream_status(self):
        for status in PRESERVED_STATUSES:
            with self.subTest(status=status):
                ScriptedUpstreamHandler.behaviors = [status]
                ScriptedUpstreamHandler.posts = 0
                response = self.post_responses()
                try:
                    self.assert_upstream_status(response, status)
                finally:
                    response.close()

    def test_upstream_message_is_bounded(self):
        ScriptedUpstreamHandler.behaviors = [403]
        ScriptedUpstreamHandler.posts = 0
        with mock.patch.object(
                chat2api, "_extract_upstream_reject_error",
                return_value=("x" * 5000, "", "")):
            response = self.post_chat()
        try:
            message = response.json()["error"]["detail"]["upstream_message"]
            self.assertLessEqual(
                len(message), chat2api._UPSTREAM_ERROR_MESSAGE_MAX_CHARS)
        finally:
            response.close()

    def test_api_passthrough_rebuilds_openai_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "control-token"
            token_file.write_text("control-token-value", encoding="utf-8")
            ScriptedUpstreamHandler.behaviors = [403]
            ScriptedUpstreamHandler.posts = 0
            with mock.patch.object(chat2api, "CONTROL_TOKEN_FILE",
                                   str(token_file)):
                response = requests.get(
                    f"{self.base}/api/config",
                    headers={"X-Admin-Control": "control-token-value"},
                    timeout=10)
        try:
            self.assert_upstream_status(response, 403)
        finally:
            response.close()


class TestReauthenticationAndTransport(UpstreamStatusTestBase):
    def _patch_reauth(self):
        for patch in (
            mock.patch.object(chat2api.Proxy, "refresh",
                              return_value=False),
            mock.patch.object(chat2api.Proxy, "auth_status",
                              return_value={"error": "upstream_rejected",
                                             "recovery": {}}),
            mock.patch.object(chat2api.Proxy, "auth_retry_after",
                              return_value=7),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_401_is_503_reauthentication_on_every_protocol(self):
        self._patch_reauth()
        for name, post in (("chat", self.post_chat),
                           ("messages", self.post_messages),
                           ("responses", self.post_responses)):
            with self.subTest(protocol=name):
                ScriptedUpstreamHandler.behaviors = [401]
                ScriptedUpstreamHandler.posts = 0
                response = post()
                try:
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.headers.get("Retry-After"), "7")
                    body = response.json()
                    error = body["error"]
                    self.assertEqual(error["code"],
                                     "reauthentication_required")
                    self.assertEqual(error["detail"], "upstream_rejected")
                finally:
                    response.close()

    def test_connection_failure_is_502_on_every_protocol(self):
        self.start_gateway("http://127.0.0.1:9")
        for name, post in (("chat", self.post_chat),
                           ("messages", self.post_messages),
                           ("responses", self.post_responses)):
            with self.subTest(protocol=name):
                response = post()
                try:
                    self.assertEqual(response.status_code, 502)
                    error = response.json()["error"]
                    self.assertEqual(error["code"], "upstream_unreachable")
                finally:
                    response.close()

    def test_total_budget_deadline_is_504_on_every_protocol(self):
        with mock.patch.object(chat2api, "MAX_REQUEST_SECONDS", 0.0):
            for name, post in (("chat", self.post_chat),
                               ("messages", self.post_messages),
                               ("responses", self.post_responses)):
                with self.subTest(protocol=name):
                    response = post()
                    try:
                        self.assertEqual(response.status_code, 504)
                        error = response.json()["error"]
                        self.assertEqual(error["code"],
                                         "request_deadline_exceeded")
                    finally:
                        response.close()

    def test_header_wait_timeout_is_504(self):
        ScriptedUpstreamHandler.behaviors = ["delay_headers"]
        ScriptedUpstreamHandler.posts = 0
        ScriptedUpstreamHandler.delay_headers_s = 0.8
        with mock.patch.object(chat2api, "STREAM_SOCKET_TIMEOUT_S", 0.2):
            response = self.post_chat(stream=True)
        try:
            self.assertEqual(response.status_code, 504)
            error = response.json()["error"]
            self.assertEqual(error["code"], "upstream_timeout")
        finally:
            response.close()

        ScriptedUpstreamHandler.behaviors = ["delay_headers"]
        ScriptedUpstreamHandler.posts = 0
        with mock.patch.object(chat2api, "MAX_REQUEST_SECONDS", 0.5):
            response = self.post_messages()
        try:
            self.assertEqual(response.status_code, 504)
            error = response.json()["error"]
            self.assertEqual(error["code"], "upstream_timeout")
        finally:
            response.close()

        ScriptedUpstreamHandler.behaviors = ["delay_headers"]
        ScriptedUpstreamHandler.posts = 0
        with mock.patch.object(
                chat2api.requests, "request",
                side_effect=requests.exceptions.ReadTimeout(
                    "upstream header wait timed out")):
            response = self.post_responses()
        try:
            self.assertEqual(response.status_code, 504)
            error = response.json()["error"]
            self.assertEqual(error["code"], "upstream_timeout")
        finally:
            response.close()

    def test_first_byte_timeout_is_504(self):
        ScriptedUpstreamHandler.behaviors = ["silent_stream"]
        ScriptedUpstreamHandler.posts = 0
        ScriptedUpstreamHandler.silent_s = 1.0
        with mock.patch.object(chat2api, "STREAM_FIRST_BYTE_TIMEOUT_S", 0.2):
            response = self.post_chat(stream=True)
        try:
            self.assertEqual(response.status_code, 504)
            error = response.json()["error"]
            self.assertEqual(error["code"], "upstream_timeout")
            self.assertEqual(error["detail"]["reason"],
                             "upstream_first_byte_timeout")
        finally:
            response.close()

        ScriptedUpstreamHandler.behaviors = ["silent_stream"]
        ScriptedUpstreamHandler.posts = 0
        with mock.patch.object(chat2api, "STREAM_FIRST_BYTE_TIMEOUT_S", 0.2):
            response = self.post_messages(stream=True)
        try:
            self.assertEqual(response.status_code, 504)
            error = response.json()["error"]
            self.assertEqual(error["code"], "upstream_timeout")
        finally:
            response.close()

    def test_socket_silence_is_504_before_commit(self):
        ScriptedUpstreamHandler.behaviors = ["silent_stream"]
        ScriptedUpstreamHandler.posts = 0
        ScriptedUpstreamHandler.silent_s = 1.0
        with mock.patch.object(chat2api, "STREAM_FIRST_BYTE_TIMEOUT_S", 10), \
                mock.patch.object(chat2api, "STREAM_SOCKET_TIMEOUT_S", 0.2):
            response = self.post_chat(stream=True)
        try:
            self.assertEqual(response.status_code, 504)
            error = response.json()["error"]
            self.assertEqual(error["code"], "upstream_timeout")
            self.assertEqual(error["detail"]["reason"],
                             "upstream_read_error")
            # requests wraps a read timeout from iter_content in
            # ConnectionError(ReadTimeoutError); the helper still classifies it
            # as the gateway's own wait expiring (504).
            self.assertEqual(error["detail"]["exception"], "ConnectionError")
        finally:
            response.close()


class TestRetryExhaustion(UpstreamStatusTestBase):
    def test_chat_retry_exhaustion_keeps_last_status(self):
        self.enable_retries()
        ScriptedUpstreamHandler.behaviors = [503, 500, 429]
        ScriptedUpstreamHandler.posts = 0
        response = self.post_chat()
        try:
            self.assert_upstream_status(response, 429)
            self.assertEqual(ScriptedUpstreamHandler.posts, 3)
        finally:
            response.close()

    def test_messages_retry_exhaustion_keeps_last_status(self):
        self.enable_retries()
        ScriptedUpstreamHandler.behaviors = [503, 500, 429]
        ScriptedUpstreamHandler.posts = 0
        response = self.post_messages()
        try:
            self.assertEqual(response.status_code, 429)
            error = response.json()["error"]
            self.assertEqual(error["detail"]["upstream_status"], 429)
            self.assertEqual(error["detail"]["upstream_message"],
                             "upstream status 429")
            self.assertEqual(ScriptedUpstreamHandler.posts, 3)
        finally:
            response.close()

    def test_last_transport_error_is_502(self):
        self.enable_retries()
        ScriptedUpstreamHandler.behaviors = [503, "close"]
        ScriptedUpstreamHandler.posts = 0
        response = self.post_chat()
        try:
            self.assertEqual(response.status_code, 502)
            error = response.json()["error"]
            self.assertEqual(error["code"], "upstream_unreachable")
        finally:
            response.close()

    def test_retry_wait_deadline_returns_504_with_last_status(self):
        self.enable_retries()
        ScriptedUpstreamHandler.behaviors = [503]
        ScriptedUpstreamHandler.posts = 0
        with mock.patch.object(chat2api, "MAX_REQUEST_SECONDS", 0.2), \
                mock.patch.object(chat2api, "_retry_delay_seconds",
                                  return_value=0.5):
            response = self.post_chat()
        try:
            self.assertEqual(response.status_code, 504)
            error = response.json()["error"]
            self.assertEqual(error["code"], "request_deadline_exceeded")
            self.assertEqual(error["detail"]["last_upstream_status"], 503)
        finally:
            response.close()

    def test_messages_retry_wait_deadline_returns_504_with_last_status(self):
        self.enable_retries()
        ScriptedUpstreamHandler.behaviors = [503]
        ScriptedUpstreamHandler.posts = 0
        with mock.patch.object(chat2api, "MAX_REQUEST_SECONDS", 0.2), \
                mock.patch.object(chat2api, "_retry_delay_seconds",
                                  return_value=0.5):
            response = self.post_messages()
        try:
            self.assertEqual(response.status_code, 504)
            body = response.json()
            self.assertEqual(body["type"], "error")
            self.assertEqual(body["error"]["code"],
                             "request_deadline_exceeded")
            self.assertEqual(
                body["error"]["detail"]["last_upstream_status"], 503)
        finally:
            response.close()


if __name__ == "__main__":
    unittest.main()
