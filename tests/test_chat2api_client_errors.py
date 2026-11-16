# -*- coding: utf-8 -*-
"""Client-facing error semantics and response headers (spec #58 / ticket #62).

Covers the locally produced client errors that used to escape the JSON error
contract: an unimplemented method on an API path (405 + Allow instead of the
historical 501 HTML), an unsupported Content-Encoding (415), a stalled request
body (408 within the body budget), the preserved share 32 MiB limit (413),
``Cache-Control: no-store`` and ``X-Request-Id`` on errors, the same id in an
in-band SSE error event, and ``tries``/``physical_attempts`` in the final
failure log.

Every test drives a real local chat2api handler against a stub upstream on an
ephemeral loopback port; no real UM request, token, or production ledger is
touched.
"""

import http.client
import importlib.util
import json
import re
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _chat2api_http_harness as harness  # noqa: E402

CHAT2API_PATH = Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py"
SPEC = importlib.util.spec_from_file_location(
    "chat2api_client_errors", CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)

STUB_TOKEN = "stub-token"
_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_FIELDS = re.compile(r"(\w+)=(\S+)")


def _parse_headers(head: bytes):
    headers = {}
    for line in head.split(b"\r\n")[1:]:
        if b":" not in line:
            continue
        name, _, value = line.partition(b":")
        headers[name.strip().lower().decode("ascii")] = value.strip().decode(
            "utf-8", "replace")
    return headers


def _recv_response(sock):
    """Read one HTTP response (headers + Content-Length body) from a socket."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("peer closed before headers: %r" % buf[:120])
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    headers = _parse_headers(head)
    length = int(headers.get("content-length") or 0)
    body = rest
    while len(body) < length:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("peer closed before body: %r" % body[:120])
        body += chunk
    return head, body


class StubUpstreamHandler(BaseHTTPRequestHandler):
    """Stub UM surface: one transient failure and one truncated SSE stream."""

    protocol_version = "HTTP/1.1"
    posts = 0

    def log_message(self, *args):
        pass

    def _send_json(self, code, obj):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        StubUpstreamHandler.posts += 1
        model = body.get("model", "")
        if model == "reject-503":
            self._send_json(503, {"error": {"message": "upstream overloaded"}})
        elif model == "stream-truncated":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.close_connection = True
            chunk = (b'data: {"choices":[{"index":0,'
                     b'"delta":{"content":"hi"}}]}\n\n')
            self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii")
                             + chunk + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        else:
            self._send_json(200, {})


class TestChat2ApiClientErrors(unittest.TestCase):
    def setUp(self):
        self.tmp_dir, self.usage_log = harness.install_usage_isolation(
            self, chat2api, ledger_name="usage-chat2api.jsonl")
        harness.assert_stub_credentials(
            chat2api, local_key=None, upstream_token=STUB_TOKEN,
            stub_token_literal=STUB_TOKEN)

        StubUpstreamHandler.posts = 0
        stub = harness.TrackedHTTPServer(("127.0.0.1", 0),
                                         StubUpstreamHandler)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        harness.install_tracked_server(self, stub, name="upstream-stub",
                                       require_drained=False)
        self.stub = stub

        args = SimpleNamespace(
            base_url=f"http://127.0.0.1:{stub.server_port}",
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
        harness.install_tracked_server(self, server, name="chat2api-gateway")
        self.host = "127.0.0.1"
        self.port = server.server_port

        # These cases pin one logical attempt unless a test enables retry
        # explicitly; the retry policy itself is covered elsewhere.
        for patch in (
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", False),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 0),
            mock.patch.object(chat2api, "UPSTREAM_SLOW_AUTO_RETRIES", 0),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRY", False),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRIES", 0),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        chat2api._reader_slots = threading.BoundedSemaphore(
            chat2api.STREAM_MAX_CONCURRENT_READERS)

    def _request(self, method, path, body=None, headers=None, timeout=10):
        connection = http.client.HTTPConnection(
            self.host, self.port, timeout=timeout)
        try:
            connection.request(method, path, body=body,
                               headers=headers or {})
            response = connection.getresponse()
            data = response.read()
        finally:
            connection.close()
        return response, data

    def _chat_body(self, model="ok-model", stream=True):
        return json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": stream,
        }).encode("utf-8")

    def _assert_error_headers(self, response):
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertRegex(response.getheader("X-Request-Id") or "",
                         _REQUEST_ID_RE)

    # ---------- 405 + Allow ----------

    def test_put_chat_completions_is_405_with_allow_post(self):
        response, data = self._request(
            "PUT", "/v1/chat/completions", body=b"{}",
            headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 405)
        self.assertEqual(response.getheader("Allow"), "POST")
        self._assert_error_headers(response)
        error = json.loads(data)["error"]
        self.assertEqual(error["type"], "invalid_request_error")
        self.assertEqual(error["code"], "method_not_allowed")

    def test_put_messages_is_405_in_anthropic_shape(self):
        response, data = self._request(
            "PUT", "/v1/messages", body=b"{}",
            headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 405)
        self.assertEqual(response.getheader("Allow"), "POST")
        self._assert_error_headers(response)
        payload = json.loads(data)
        self.assertEqual(payload["type"], "error")
        self.assertEqual(payload["error"]["code"], "method_not_allowed")
        self.assertNotIn("detail", payload["error"])

    def test_delete_models_is_405_with_allow_get(self):
        response, data = self._request("DELETE", "/v1/models")
        self.assertEqual(response.status, 405)
        self.assertEqual(response.getheader("Allow"), "GET")
        self._assert_error_headers(response)
        self.assertEqual(json.loads(data)["error"]["code"],
                         "method_not_allowed")

    # ---------- 415 unsupported Content-Encoding ----------

    def test_unsupported_content_encoding_is_415(self):
        for encoding in ("br", "zstd", "gzip"):
            with self.subTest(encoding=encoding):
                response, data = self._request(
                    "POST", "/v1/chat/completions",
                    body=self._chat_body(),
                    headers={"Content-Type": "application/json",
                             "Content-Encoding": encoding})
                self.assertEqual(response.status, 415)
                self._assert_error_headers(response)
                error = json.loads(data)["error"]
                self.assertEqual(error["code"],
                                 "unsupported_content_encoding")
                self.assertEqual(error["param"], "Content-Encoding")

    def test_identity_content_encoding_is_accepted(self):
        response, _ = self._request(
            "POST", "/v1/chat/completions",
            body=self._chat_body("ok-model", stream=False),
            headers={"Content-Type": "application/json",
                     "Content-Encoding": "identity"})
        self.assertEqual(response.status, 200)

    # ---------- 408 slow body ----------

    def test_slow_body_is_408_request_timeout(self):
        with mock.patch.object(chat2api, "REQUEST_BODY_TIMEOUT_S", 0.2):
            sock = socket.create_connection(
                (self.host, self.port), timeout=5)
            sock.settimeout(5)
            try:
                head = (
                    "POST /v1/chat/completions HTTP/1.1\r\n"
                    f"Host: {self.host}:{self.port}\r\n"
                    "Content-Type: application/json\r\n"
                    "Content-Length: 100\r\n"
                    "\r\n"
                ).encode("ascii")
                sock.sendall(head + b'{"messages"')
                response_head, body = _recv_response(sock)
            finally:
                sock.close()
        self.assertIn(b" 408 ", response_head.split(b"\r\n")[0])
        headers = _parse_headers(response_head)
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertRegex(headers.get("x-request-id") or "",
                          _REQUEST_ID_RE)
        self.assertIn("connection", headers)
        self.assertEqual(headers["connection"].lower(), "close")
        error = json.loads(body)["error"]
        self.assertEqual(error["code"], "request_timeout")
        self.assertEqual(error["type"], "invalid_request_error")

    # ---------- 413 share body limit ----------

    def test_share_body_limit_still_returns_413(self):
        class ShareAdmitHandler(chat2api.Handler):
            def _share_gate_admit(self, path, raw_path, allow_api_key):
                return True

        server = harness.TrackedHTTPServer(
            ("127.0.0.1", 0), ShareAdmitHandler)
        server.proxy = self.proxy
        server.share_gate = object()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        harness.install_tracked_server(
            self, server, name="chat2api-share-gateway")
        self.assertEqual(chat2api.SHARE_MAX_REQUEST_BYTES,
                         32 * 1024 * 1024)
        oversized = 33 * 1024 * 1024
        self.assertGreater(oversized, chat2api.SHARE_MAX_REQUEST_BYTES)
        sock = socket.create_connection(
            (self.host, server.server_port), timeout=5)
        sock.settimeout(5)
        try:
            head = (
                "POST /v1/chat/completions HTTP/1.1\r\n"
                f"Host: {self.host}:{server.server_port}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {oversized}\r\n"
                "\r\n"
            ).encode("ascii")
            sock.sendall(head)
            response_head, body = _recv_response(sock)
        finally:
            sock.close()
        self.assertIn(b" 413 ", response_head.split(b"\r\n")[0])
        headers = _parse_headers(response_head)
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertRegex(headers.get("x-request-id") or "",
                          _REQUEST_ID_RE)
        self.assertEqual(headers.get("connection", "").lower(), "close")
        error = json.loads(body)["error"]
        self.assertEqual(error["code"], "payload_too_large")

    # ---------- in-band stream error id ----------

    def test_stream_error_event_reuses_response_request_id(self):
        response, data = self._request(
            "POST", "/v1/chat/completions",
            body=self._chat_body("stream-truncated", stream=True),
            headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 200)
        header_id = response.getheader("X-Request-Id")
        self.assertRegex(header_id or "", _REQUEST_ID_RE)
        errors = []
        for line in data.decode("utf-8", "replace").splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except ValueError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("error"), dict):
                errors.append(obj["error"])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["request_id"], header_id)

    # ---------- final failure log ----------

    def test_failure_log_has_tries_and_physical_attempts(self):
        with self.assertLogs("chat2api", level="WARNING") as cm:
            response, _ = self._request(
                "POST", "/v1/chat/completions",
                body=self._chat_body("reject-503", stream=True),
                headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 503)
        lines = [line for line in cm.output if "chat failure" in line]
        self.assertEqual(len(lines), 1)
        fields = dict(_FIELDS.findall(lines[0]))
        self.assertEqual(fields["tries"], "1")
        self.assertRegex(fields["physical_attempts"], r"^1/\d+$")

    def test_failure_log_counts_retry_exhaustion(self):
        with mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True), \
                mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 1), \
                mock.patch.object(chat2api, "UPSTREAM_SLOW_AUTO_RETRIES", 0), \
                mock.patch.object(chat2api, "_retry_delay_seconds",
                                  return_value=0.0):
            with self.assertLogs("chat2api", level="WARNING") as cm:
                response, _ = self._request(
                    "POST", "/v1/chat/completions",
                    body=self._chat_body("reject-503", stream=True),
                    headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 503)
        self.assertEqual(StubUpstreamHandler.posts, 2)
        fields = dict(_FIELDS.findall(
            [line for line in cm.output if "chat failure" in line][0]))
        self.assertEqual(fields["tries"], "2")
        self.assertRegex(fields["physical_attempts"], r"^2/\d+$")


if __name__ == "__main__":
    unittest.main()
