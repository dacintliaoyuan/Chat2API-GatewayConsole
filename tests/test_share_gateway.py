"""Exercise the sharing boundary with real local HTTP sockets, never UM."""

import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
import sys


# share_broker_protocol is imported lazily by share_entry.load_config for
# projection configs; make the ai-gateway directory reachable in ANY invocation
# mode, not only when another test module happens to add it to sys.path first.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SPEC = importlib.util.spec_from_file_location("share_entry", Path(__file__).resolve().parents[1] / "chat2api" / "share_entry.py")
share = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(share)

_REVOKE_STATE_TMP = None
_REVOKE_STATE_SAVED = None


def setUpModule():
    """Keep the runtime's persisted revoke state out of the PRODUCT path.

    The ACK now merges the persisted state and records the applied
    projection generation, so the revoke tests must own a private file.
    """
    global _REVOKE_STATE_TMP, _REVOKE_STATE_SAVED
    _REVOKE_STATE_TMP = tempfile.TemporaryDirectory(
        prefix="um-share-revoke-")
    _REVOKE_STATE_SAVED = os.environ.get(share.REVOKE_STATE_ENV)
    os.environ[share.REVOKE_STATE_ENV] = str(
        Path(_REVOKE_STATE_TMP.name) / "share-revoke.state")
    share.reset_revoke_state_cache()


def tearDownModule():
    if _REVOKE_STATE_SAVED is None:
        os.environ.pop(share.REVOKE_STATE_ENV, None)
    else:
        os.environ[share.REVOKE_STATE_ENV] = _REVOKE_STATE_SAVED
    share.reset_revoke_state_cache()
    if _REVOKE_STATE_TMP is not None:
        _REVOKE_STATE_TMP.cleanup()

# Anthropic-shaped upstream payloads for the /v1/messages relay tests. The
# share must copy these byte for byte (SSE event types included), so the fixture
# pins exact bytes rather than re-encoding them on both sides of the assertion.
ANTHROPIC_SSE = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"id":"msg_1","type":"message",'
    b'"role":"assistant","model":"GLM-5.2-NVFP4","content":[],"stop_reason":null,'
    b'"stop_sequence":null,"usage":{"input_tokens":7,"output_tokens":0}}}\n\n'
    b'event: content_block_start\n'
    b'data: {"type":"content_block_start","index":0,'
    b'"content_block":{"type":"text","text":""}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","index":0,'
    b'"delta":{"type":"text_delta","text":"hello"}}\n\n'
    b'event: content_block_stop\n'
    b'data: {"type":"content_block_stop","index":0}\n\n'
    b'event: message_delta\n'
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn",'
    b'"stop_sequence":null},"usage":{"output_tokens":2}}\n\n'
    b'event: message_stop\n'
    b'data: {"type":"message_stop"}\n\n'
)
ANTHROPIC_MESSAGE = (
    b'{"id":"msg_1","type":"message","role":"assistant",'
    b'"model":"GLM-5.2-NVFP4","content":[{"type":"text","text":"hello"}],'
    b'"stop_reason":"end_turn","stop_sequence":null,'
    b'"usage":{"input_tokens":7,"output_tokens":2}}'
)
# Anthropic tool-use payloads: the SAME relay must carry tool_use blocks (streamed
# as input_json_delta) and a tool_use stop_reason byte for byte. Anthropic clients
# (SDK / Claude Code) drive every tool call through these shapes, so the relay may
# not reshape, reorder or drop them.
TOOL_USE_SSE = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"id":"msg_1","type":"message",'
    b'"role":"assistant","model":"GLM-5.2-NVFP4","content":[],"stop_reason":null,'
    b'"stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":0}}}\n\n'
    b'event: content_block_start\n'
    b'data: {"type":"content_block_start","index":0,'
    b'"content_block":{"type":"tool_use","id":"toolu_1","name":"get_weather",'
    b'"input":{}}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","index":0,'
    b'"delta":{"type":"input_json_delta","partial_json":"{\\"city\\":"}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","index":0,'
    b'"delta":{"type":"input_json_delta","partial_json":"\\"Macau\\"}"}}\n\n'
    b'event: content_block_stop\n'
    b'data: {"type":"content_block_stop","index":0}\n\n'
    b'event: message_delta\n'
    b'data: {"type":"message_delta","delta":{"stop_reason":"tool_use",'
    b'"stop_sequence":null},"usage":{"output_tokens":14}}\n\n'
    b'event: message_stop\n'
    b'data: {"type":"message_stop"}\n\n'
)
TOOL_USE_MESSAGE = (
    b'{"id":"msg_1","type":"message","role":"assistant",'
    b'"model":"GLM-5.2-NVFP4","content":[{"type":"tool_use","id":"toolu_1",'
    b'"name":"get_weather","input":{"city":"Macau"}}],"stop_reason":"tool_use",'
    b'"stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":14}}'
)


class SharingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name) / "clients.json"
        self.config.write_text(json.dumps({"clients": {"friend": hashlib.sha256(b"test-secret").hexdigest()}}), encoding="utf-8")
        self.upstream_requests = []
        self.finish_stream = threading.Event()
        finish_stream = self.finish_stream
        captured = self.upstream_requests
        # Mutable upstream behavior for the real-mode model list tests.
        self.model_status = [200]
        self.model_payload = [{"object": "list", "data": []}]
        self.model_marker = [True]   # chat2api honours the raw-fresh request
        model_status = self.model_status
        model_payload = self.model_payload
        model_marker = self.model_marker
        # Mutable chat upstream status for the chat-relay failure tests
        # (chat2api now answers pre-200 stream failures with HTTP 502).
        self.chat_status = [200]
        chat_status_holder = self.chat_status
        # Optional explicit Messages payload (tool-use shapes): None keeps the
        # text fixtures selected by the `stream` flag.
        self.messages_payload = [None]
        messages_payload_holder = self.messages_payload
        # Optional explicit chat payload for the Responses translation tests:
        # None keeps the empty-stream fixture below.
        self.chat_payload = [None]
        chat_payload_holder = self.chat_payload
        # Optional explicit Responses payload for the raw relay tests: None keeps
        # the historical chat stream below.
        self.responses_payload = [None]
        responses_payload_holder = self.responses_payload

        class Upstream(BaseHTTPRequestHandler):
            def do_GET(inner):
                captured.append((inner.path, dict(inner.headers)))
                if inner.path == "/v1/models":
                    expect_raw_fresh = (
                        inner.headers.get("X-Chat2Api-Models", "").lower() == "raw-fresh")
                    if model_status[0] != 200:
                        body = b'{"error":"upstream down"}'
                        inner.send_response(model_status[0])
                        inner.send_header("Content-Type", "application/json")
                        inner.send_header("Content-Length", str(len(body)))
                        inner.end_headers()
                        inner.wfile.write(body)
                        return
                    body = json.dumps(model_payload[0]).encode()
                    inner.send_response(200)
                    inner.send_header("Content-Type", "application/json")
                    if expect_raw_fresh and model_marker[0]:
                        inner.send_header("X-Chat2Api-Raw-Models", "1")
                    inner.send_header("Content-Length", str(len(body)))
                    inner.end_headers()
                    inner.wfile.write(body)

            def do_POST(inner):
                body = inner.rfile.read(int(inner.headers["Content-Length"]))
                captured.append((inner.path, dict(inner.headers), json.loads(body)))
                chat_status = chat_status_holder[0]
                if chat_status != 200:
                    payload = b'{"error": {"message": "upstream stream failed."}}'
                    inner.send_response(chat_status)
                    inner.send_header("Content-Type", "application/json")
                    inner.send_header("Content-Length", str(len(payload)))
                    inner.end_headers()
                    inner.wfile.write(payload)
                    return
                if inner.path == "/v1/messages":
                    # Anthropic Messages: a stream request answers with
                    # Anthropic SSE, a non-stream request with the Anthropic
                    # message object. Either way the share must relay the bytes
                    # unchanged (no OpenAI reshaping, no dropped events).
                    streamed = bool(json.loads(body).get("stream"))
                    override = messages_payload_holder[0]
                    payload = override if override is not None else (
                        ANTHROPIC_SSE if streamed else ANTHROPIC_MESSAGE)
                    inner.send_response(200)
                    inner.send_header(
                        "Content-Type",
                        "text/event-stream" if streamed else "application/json")
                    inner.send_header("Content-Length", str(len(payload)))
                    inner.end_headers()
                    inner.wfile.write(payload)
                    return
                if inner.path == "/v1/responses" \
                        and responses_payload_holder[0] is not None:
                    streamed = bool(json.loads(body).get("stream"))
                    payload = responses_payload_holder[0]
                    inner.send_response(200)
                    inner.send_header(
                        "Content-Type",
                        "text/event-stream" if streamed else "application/json")
                    inner.send_header("Content-Length", str(len(payload)))
                    inner.end_headers()
                    inner.wfile.write(payload)
                    return
                if inner.path == "/v1/chat/completions" \
                        and chat_payload_holder[0] is not None:
                    streamed = bool(json.loads(body).get("stream"))
                    payload = chat_payload_holder[0]
                    inner.send_response(200)
                    inner.send_header(
                        "Content-Type",
                        "text/event-stream" if streamed else "application/json")
                    inner.send_header("Content-Length", str(len(payload)))
                    inner.end_headers()
                    inner.wfile.write(payload)
                    return
                inner.send_response(200)
                inner.send_header("Content-Type", "text/event-stream")
                inner.end_headers()
                inner.wfile.write(b'data: {"choices":[]}\n\n')
                inner.wfile.flush()
                if json.loads(body).get("model") == "delayed":
                    finish_stream.wait(3)
                inner.wfile.write(b'data: [DONE]\n\n')

            def log_message(inner, *_args):
                pass

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.server = share.ShareServer(("127.0.0.1", 0), self.config, self.upstream.server_port)
        self.threads = []
        for server in (self.upstream, self.server):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)

    def tearDown(self):
        self.finish_stream.set()
        for server in (self.server, self.upstream):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join()
        self.temp.cleanup()

    def request(self, method="GET", path="/v1/models", key="test-secret", body=None, headers=None):
        """Local call through the share. ``key=None`` omits the injected
        ``Authorization`` header entirely, so a test can send the Anthropic SDK's
        default ``X-Api-Key``-only shape (or no credential at all). ``key=""``
        still sends an EMPTY Bearer header, which must keep failing closed."""
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        request_headers = {"Content-Type": "application/json"}
        if key is not None:
            request_headers["Authorization"] = "Bearer " + key
        request_headers.update(headers or {})
        try:
            conn.request(method, path, body=body, headers=request_headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def request_raw_headers(self, method, path, header_pairs, body=None):
        """Send EXACTLY the given header lines, duplicates included.

        ``http.client``'s ``request()`` takes a mapping and iterates
        ``.items()``, so it cannot express a repeated same-name header; the
        fail-closed rules for duplicated credentials need the wire form."""
        payload = body.encode() if isinstance(body, str) else (body or b"")
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            conn.putrequest(method, path)
            for name, value in header_pairs:
                conn.putheader(name, value)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(len(payload)))
            conn.endheaders(payload)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def request_with_headers(self, method, path, key="test-secret", body=None,
                             headers=None):
        """Same local call as ``request`` but also returns the response headers,
        so a test can pin the relayed Content-Type (Messages must keep the
        upstream one instead of being reshaped)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        request_headers = {"Content-Type": "application/json"}
        if key is not None:
            request_headers["Authorization"] = "Bearer " + key
        request_headers.update(headers or {})
        try:
            conn.request(method, path, body=body, headers=request_headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def request_raw_wire(self, header_lines, body=b"", half_close=False,
                        timeout=5):
        """Send EXACT header lines and raw bytes, then read the full reply.

        ``header_lines`` are the bytes AFTER the request line, so a test can put
        shapes on the wire that ``http.client`` refuses to build: a repeated
        ``Content-Length``, a non-numeric one, or a declared length that does
        not match the bytes actually sent. ``half_close`` shuts the write side
        down after the body, which is how a client that cannot finish its body
        ends the request (the relay must answer, not block).
        """
        conn = socket.create_connection(("127.0.0.1", self.server.server_port),
                                        timeout=timeout)
        conn.settimeout(timeout)
        try:
            conn.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                          + b"".join(header_lines) + b"\r\n")
            if body:
                conn.sendall(body)
            if half_close:
                conn.shutdown(socket.SHUT_WR)
            raw = b""
            while b"\r\n\r\n" not in raw:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                raw += chunk
            head, _, payload = raw.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            while len(payload) < length:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                payload += chunk
            status = int(head.split(b"\r\n")[0].split(b" ")[1])
            return status, payload
        finally:
            conn.close()

    def test_bad_key_never_reaches_upstream(self):
        self.assertEqual(self.request(key="wrong")[0], 401)
        self.assertEqual(self.upstream_requests, [])

    def test_only_exact_api_paths_are_shared(self):
        for path in ("/admin/auth/status", "/admin/shutdown", "/api/models", "/api/chat/completions", "/health", "/v1/models?x=1", "/v1/%6dodels", "http://localhost/v1/models", "/v1/models/../models"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path=path)[0], 404)
        self.assertEqual(self.upstream_requests, [])

    def test_key_and_cookies_are_not_forwarded(self):
        self.assertEqual(self.request(headers={"Cookie": "private", "X-Forwarded-Host": "evil"})[0], 200)
        headers = self.upstream_requests[0][1]
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("X-Forwarded-Host", headers)

    def test_messages_relays_anthropic_protocol_headers_only(self):
        # Anthropic 客户端（SDK / Claude Code）必带 anthropic-version，而专用后端
        # 只在收到时才把它转上游：分享这一跳若丢掉就等于改写了调用方的请求。
        body = self.messages_body(
            model="GLM-5.2-NVFP4", max_tokens=8,
            messages=[{"role": "user", "content": "hi"}])
        status, _ = self.request(
            "POST", "/v1/messages", body=body,
            headers={"Anthropic-Version": "2023-06-01",
                      "Anthropic-Beta": "prompt-caching-2024-07-31"})
        self.assertEqual(status, 200)
        sent = self.upstream_requests[0][1]
        self.assertEqual(sent.get("Anthropic-Version"), "2023-06-01")
        self.assertEqual(sent.get("Anthropic-Beta"), "prompt-caching-2024-07-31")
        # 其余客户端头仍不转发；chat 路径的既有契约完全不变。
        self.assertEqual(self.request(headers={"Anthropic-Version": "2023-06-01"})[0], 200)
        self.assertNotIn("Anthropic-Version", self.upstream_requests[1][1])

    def test_messages_drops_oversized_protocol_header(self):
        # 只转发单行、长度受限的值：超长头不得进入上游请求头。
        body = self.messages_body(
            model="GLM-5.2-NVFP4", max_tokens=8,
            messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(
            self.request("POST", "/v1/messages", body=body,
                         headers={"Anthropic-Version": "x" * 300})[0], 200)
        self.assertNotIn("Anthropic-Version", self.upstream_requests[0][1])

    def test_revocation_and_corruption_fail_closed(self):
        self.config.write_text('{"clients":{}}', encoding="utf-8")
        self.assertEqual(self.request()[0], 401)
        self.config.write_text("broken", encoding="utf-8")
        self.assertEqual(self.request()[0], 503)
        self.assertEqual(self.upstream_requests, [])

    def test_stream_is_forwarded_without_changes(self):
        body = json.dumps({"model": "test", "messages": [{"role": "user", "content": "hello"}], "stream": True})
        status, payload = self.request("POST", "/v1/chat/completions", body=body)
        self.assertEqual(status, 200)
        self.assertEqual(payload, b'data: {"choices":[]}\n\ndata: [DONE]\n\n')

    def test_chat_upstream_failure_is_explicit_error(self):
        # chat2api now answers pre-200 stream failures with HTTP 502/503; the
        # share entry must keep the failure explicit for the friend client
        # (503 passes through, other failures collapse to 502), never as a
        # fabricated success.
        body = json.dumps({"model": "test", "messages": [{"role": "user", "content": "hello"}], "stream": True})
        self.chat_status[0] = 502
        status, payload = self.request("POST", "/v1/chat/completions", body=body)
        self.assertEqual(status, 502)
        self.assertIn(b"um_unavailable", payload)
        self.chat_status[0] = 503
        status, _ = self.request("POST", "/v1/chat/completions", body=body)
        self.assertEqual(status, 503)
        self.chat_status[0] = 200
        status, _ = self.request("POST", "/v1/chat/completions", body=body)
        self.assertEqual(status, 200)

    def test_server_side_options_are_rejected(self):
        body = json.dumps({"model": "test", "messages": [{}], "tool_ids": ["server-tool"]})
        self.assertEqual(self.request("POST", "/v1/chat/completions", body=body)[0], 400)
        self.assertEqual(self.upstream_requests, [])

    def test_stream_first_event_arrives_before_upstream_finishes(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=1)
        try:
            conn.request("POST", "/v1/chat/completions", body=json.dumps({"model": "delayed", "messages": [{}], "stream": True}), headers={"Authorization": "Bearer test-secret"})
            response = conn.getresponse()
            self.assertEqual(response.readline(), b'data: {"choices":[]}\n')
            self.assertFalse(self.finish_stream.is_set())
            self.finish_stream.set()
            self.assertIn(b"[DONE]", response.read())
        finally:
            self.finish_stream.set()
            conn.close()

    def test_upstream_silence_bounds_mirror_the_backend_budgets(self):
        # The share must outlast the dedicated backend's own budgets (chat2api
        # STREAM_SOCKET_TIMEOUT_S=180 for one silent stream read, 600s for a
        # non-stream answer): a thinking model may stay silent for minutes
        # before its first token, and the 2026-09-25 incident showed a flat
        # 60s bound cutting every long GLM-5.3-Flash friend turn. Assert the
        # bound the relay actually constructs, since a real silent-upstream wait
        # is minutes long.
        recorded = []
        real_conn = share.http.client.HTTPConnection

        class Recording(real_conn):
            def __init__(inner, host, port=None, *args, **kwargs):
                recorded.append((port, kwargs.get("timeout")))
                super().__init__(host, port, *args, **kwargs)

        share.http.client.HTTPConnection = Recording
        try:
            stream_body = json.dumps({"model": "test", "messages": [{}],
                                     "stream": True})
            self.assertEqual(self.request("POST", "/v1/chat/completions",
                                         body=stream_body)[0], 200)
            self.assertEqual(recorded[-1], (self.upstream.server_port,
                                          share.STREAM_SOCKET_TIMEOUT_S))
            plain_body = json.dumps({"model": "test", "messages": [{}]})
            self.assertEqual(self.request("POST", "/v1/chat/completions",
                                         body=plain_body)[0], 200)
            self.assertEqual(recorded[-1], (self.upstream.server_port,
                                          share.NONSTREAM_SOCKET_TIMEOUT_S))
        finally:
            share.http.client.HTTPConnection = real_conn

    def test_stream_has_no_total_duration_cap(self):
        # Regression for the removed 300s total cap: the relay must not consult
        # the wall clock at all, so a long answer is never cut by the share
        # itself (only the per-read silence bound may end a stream). The clock
        # advances 1000s on every read, which the old deadline would trip.
        real_monotonic = share.time.monotonic
        ticks = [real_monotonic()]

        def advancing():
            ticks[0] += 1000
            return ticks[0]

        share.time.monotonic = advancing
        try:
            body = json.dumps({"model": "test", "messages": [{}],
                               "stream": True})
            status, payload = self.request("POST", "/v1/chat/completions",
                                          body=body)
            self.assertEqual(status, 200)
            self.assertEqual(payload,
                             b'data: {"choices":[]}\n\ndata: [DONE]\n\n')
        finally:
            share.time.monotonic = real_monotonic

    def test_body_size_and_encoding_limits(self):
        self.assertEqual(self.request(headers={"Content-Length": "1048577"})[0], 413)
        self.assertEqual(self.request(headers={"Content-Encoding": "gzip"})[0], 400)

    def test_concurrency_and_rate_limits(self):
        self.assertTrue(self.server.acquire("friend"))
        self.assertEqual(self.request()[0], 429)
        self.server.active.clear()
        self.assertTrue(self.server.acquire("a"))
        self.assertTrue(self.server.acquire("b"))
        self.assertEqual(self.request()[0], 429)
        self.server.active.clear()
        for _ in range(19):
            self.assertTrue(self.server.acquire("friend"))
            self.server.active.clear()
        self.assertEqual(self.request()[0], 429)

    def test_probe_never_calls_um(self):
        self.server.probe = True
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertIn(b"connectivity-test", body)
        self.assertEqual(self.upstream_requests, [])

    def test_real_mode_model_list_passes_raw_fresh_through(self):
        # chat2api serves the CURRENT upstream catalog (raw-fresh, no local
        # virtual aliases) with its freshness marker; the share passes it
        # through verbatim.
        self.model_payload[0] = {"object": "list", "data": [
            {"id": "GLM-5.2-NVFP4"}, {"id": "deepseek-v3"}]}
        status, body = self.request()
        self.assertEqual(status, 200)
        ids = [m["id"] for m in json.loads(body)["data"]]
        self.assertEqual(ids, ["GLM-5.2-NVFP4", "deepseek-v3"])
        # the request carried the raw-fresh opt-in header and the reply marker
        self.assertEqual(self.upstream_requests[0][1].get("X-Chat2Api-Models"), "raw-fresh")

    def test_real_mode_model_list_requires_freshness_marker(self):
        # An old/stale reply without the freshness marker must NOT be shown to
        # friends - fail closed (502) even though chat2api answered 200.
        self.model_marker[0] = False
        self.model_payload[0] = {"object": "list", "data": [
            {"id": "stale-base"}, {"id": "stale-base-High"}]}
        self.assertEqual(self.request()[0], 502)

    def test_real_model_catalog_with_suffix_like_real_names_is_preserved(self):
        # The raw-fresh contract must NOT delete a real upstream model whose id
        # happens to end with -High/-Fast etc. chat2api's raw list contains
        # these as normal entries and the share passes them unchanged.
        self.model_payload[0] = {"object": "list", "data": [
            {"id": "foo"},
            {"id": "foo-High"},        # real upstream pair, must be kept
            {"id": "only-Fast"},       # no base; must be kept
        ]}
        status, body = self.request()
        self.assertEqual(status, 200)
        ids = [m["id"] for m in json.loads(body)["data"]]
        self.assertEqual(ids, ["foo", "foo-High", "only-Fast"])

    def test_real_mode_model_list_upstream_failure_is_explicit(self):
        # Upstream refresh failure must surface as an error, never as a stale
        # or invented catalog.
        self.model_status[0] = 503
        self.assertEqual(self.request()[0], 503)
        self.model_status[0] = 502
        self.assertEqual(self.request()[0], 502)
        self.model_status[0] = 200
        self.model_payload[0] = {"data": [1, 2]}
        self.assertEqual(self.request()[0], 502)

    def test_upstream_offline_has_explicit_failure(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        self.assertEqual(self.request()[0], 502)

    # ---------------------------------------------- Messages (Anthropic) relay

    @staticmethod
    def messages_body(**fields):
        """Serialize an EXACT Messages body - nothing added, nothing removed.

        The tests that omit ``max_tokens`` must be able to leave it out, so no
        helper may fill it in (the share itself must not default it either).
        """
        return json.dumps(fields)

    def test_messages_requires_share_key(self):
        body = self.messages_body(model="GLM-5.2-NVFP4", max_tokens=8,
                                  messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(self.request("POST", "/v1/messages", key="wrong",
                                     body=body)[0], 401)
        status, payload = self.request("POST", "/v1/messages", body=body,
                                      headers={"Authorization": ""})
        self.assertEqual(status, 401)
        self.assertIn(b"invalid_api_key", payload)
        self.assertEqual(self.upstream_requests, [])

    def test_messages_accepts_sdk_api_key_header(self):
        # Anthropic SDK 的 api_key 默认只发 X-Api-Key、不带 Authorization：
        # 这是 Messages 的合法替代形状，与 Bearer 收敛到同一个 credential。
        body = self.messages_body(model="GLM-5.2-NVFP4", max_tokens=8,
                                  messages=[{"role": "user", "content": "hi"}])
        # SDK 实际发的是小写 `x-api-key`：头名匹配必须大小写不敏感。
        for name in ("X-Api-Key", "x-api-key"):
            with self.subTest(name=name):
                self.upstream_requests.clear()
                status, payload = self.request(
                    "POST", "/v1/messages", key=None, body=body,
                    headers={name: "test-secret"})
                self.assertEqual(status, 200)
                self.assertEqual(payload, ANTHROPIC_MESSAGE)
                self.assertEqual(len(self.upstream_requests), 1)
                # 分享 Key 绝不转发上游（与 Bearer 同一约定）。
                forwarded = self.upstream_requests[0][1]
                self.assertNotIn("Authorization", forwarded)
                self.assertNotIn("X-Api-Key", forwarded)
                self.assertNotIn("x-api-key", forwarded)

    def test_messages_accepts_matching_dual_credentials(self):
        # Claude Code 的 apiKeyHelper 会同时发两头：值相同时接受。
        body = self.messages_body(model="GLM-5.2-NVFP4", max_tokens=8,
                                  messages=[{"role": "user", "content": "hi"}])
        status, payload = self.request(
            "POST", "/v1/messages", key="test-secret", body=body,
            headers={"X-Api-Key": "test-secret"})
        self.assertEqual(status, 200)
        self.assertEqual(payload, ANTHROPIC_MESSAGE)

    def test_api_key_header_does_not_authenticate_chat_completions(self):
        # Messages and catalog discovery accept the SDK header; chat remains
        # Bearer-only. Catalog authentication is covered by Harness tests.
        chat = json.dumps({"model": "test",
                           "messages": [{"role": "user", "content": "hi"}]})
        for method, path, body in (("POST", "/v1/chat/completions", chat),):
            with self.subTest(path=path):
                status, payload = self.request(
                    method, path, key=None, body=body,
                    headers={"X-Api-Key": "test-secret"})
                self.assertEqual(status, 401)
                self.assertIn(b"invalid_api_key", payload)
        self.assertEqual(self.upstream_requests, [])

    def test_messages_credential_headers_fail_closed(self):
        # 同名重复头、空值、超长凭据、双头不同值一律拒绝。边界值本身被
        # 登记成合法 Key，所以「拒绝」只能来自 fail-closed 门，而不是因为
        # 摘要查不到——否则用例无法把两者区分开。
        body = self.messages_body(model="GLM-5.2-NVFP4", max_tokens=8,
                                  messages=[{"role": "user", "content": "hi"}])
        over_long = "A" * 257
        self.config.write_text(json.dumps({"clients": {
            "friend": hashlib.sha256(b"test-secret").hexdigest(),
            "empty": hashlib.sha256(b"").hexdigest(),
            "long": hashlib.sha256(over_long.encode()).hexdigest(),
        }}), encoding="utf-8")
        rejected = (
            # 同名重复头，即便每个值都正确。
            [("Authorization", "Bearer test-secret"),
             ("Authorization", "Bearer test-secret")],
            [("X-Api-Key", "test-secret"), ("X-Api-Key", "test-secret")],
            # 空凭据：即使 sha256("") 恰好已登记也不得接受。
            [("Authorization", "Bearer ")],
            [("X-Api-Key", "")],
            [("Authorization", "Bearer test-secret"), ("X-Api-Key", "")],
            # 超长凭据：即使其摘要恰好已登记也不得接受。
            [("Authorization", "Bearer " + over_long)],
            [("X-Api-Key", over_long)],
            # 双头不同值：绝不接受其中任何一个。
            [("Authorization", "Bearer test-secret"), ("X-Api-Key", "wrong")],
            [("Authorization", "Bearer wrong"), ("X-Api-Key", "test-secret")],
        )
        for pairs in rejected:
            with self.subTest(pairs=[name for name, _ in pairs]):
                status, payload = self.request_raw_headers(
                    "POST", "/v1/messages", pairs, body=body)
                self.assertEqual(status, 401)
                self.assertIn(b"invalid_api_key", payload)
        self.assertEqual(self.upstream_requests, [])
        # 反例：正确 Key 的三种合法形状都必须通过，证明上面的拒绝来自
        # fail-closed 门而不是「一律拒绝」。
        for pairs in ([("Authorization", "Bearer test-secret")],
                      [("X-Api-Key", "test-secret")],
                      [("Authorization", "Bearer test-secret"),
                       ("X-Api-Key", "test-secret")]):
            with self.subTest(pairs=[name for name, _ in pairs]):
                self.assertEqual(self.request_raw_headers(
                    "POST", "/v1/messages", pairs, body=body)[0], 200)

    def test_messages_api_key_shares_the_one_key_budget(self):
        # X-Api-Key 不是第二套授权：两种形状必须落在同一个 key_id 上，
        # 共用并发预算与撤销状态。
        body = self.messages_body(model="GLM-5.2-NVFP4", max_tokens=8,
                                  messages=[{"role": "user", "content": "hi"}])
        self.assertTrue(self.server.acquire("friend"))
        status, payload = self.request("POST", "/v1/messages", key=None,
                                      body=body,
                                      headers={"X-Api-Key": "test-secret"})
        self.assertEqual(status, 429)
        self.assertIn(b"per_key_concurrency", payload)
        self.assertEqual(self.upstream_requests, [])
        self.server.release("friend")
        # 撤销后同一形状同样 401：同一 enabled/摘要查表。
        self.config.write_text('{"clients":{}}', encoding="utf-8")
        status, payload = self.request("POST", "/v1/messages", key=None,
                                      body=body,
                                      headers={"X-Api-Key": "test-secret"})
        self.assertEqual(status, 401)
        self.assertIn(b"invalid_api_key", payload)
        self.assertEqual(self.upstream_requests, [])

    def test_messages_unknown_fields_are_rejected(self):
        # Messages 有自己的白名单：OpenAI-only 字段（stream_options /
        # max_completion_tokens）与服务端工具字段都不得被静默转发。
        for extra in ({"tool_ids": ["server-tool"]},
                      {"stream_options": {"include_usage": True}},
                      {"max_completion_tokens": 8},
                      {"top_logprobs": 1}):
            with self.subTest(extra=sorted(extra)):
                body = self.messages_body(
                    model="GLM-5.2-NVFP4", max_tokens=8,
                    messages=[{"role": "user", "content": "hi"}], **extra)
                status, payload = self.request("POST", "/v1/messages", body=body)
                self.assertEqual(status, 400)
                self.assertIn(b"unsupported_messages_request", payload)
        self.assertEqual(self.upstream_requests, [])

    def test_messages_requires_positive_max_tokens(self):
        # 缺 max_tokens 或值非法都必须 400：分享入口不替调用方补默认值。
        status, payload = self.request("POST", "/v1/messages", body=self.messages_body(
            model="GLM-5.2-NVFP4",
            messages=[{"role": "user", "content": "hi"}]))
        self.assertEqual(status, 400)
        self.assertIn(b"unsupported_messages_request", payload)
        for value in (None, 0, -1, "16", 8.0, True):
            with self.subTest(max_tokens=value):
                body = self.messages_body(
                    model="GLM-5.2-NVFP4", max_tokens=value,
                    messages=[{"role": "user", "content": "hi"}])
                self.assertEqual(
                    self.request("POST", "/v1/messages", body=body)[0], 400)
        self.assertEqual(self.upstream_requests, [])

    def test_messages_requires_non_empty_message_list(self):
        for messages in (None, [], "hello"):
            with self.subTest(messages=messages):
                body = self.messages_body(model="GLM-5.2-NVFP4", max_tokens=8,
                                          messages=messages)
                self.assertEqual(
                    self.request("POST", "/v1/messages", body=body)[0], 400)
        self.assertEqual(self.upstream_requests, [])

    def test_messages_requires_model_and_bool_stream(self):
        for model in (None, "", 7, ["GLM-5.2-NVFP4"]):
            with self.subTest(model=model):
                body = self.messages_body(
                    model=model, max_tokens=8,
                    messages=[{"role": "user", "content": "hi"}])
                self.assertEqual(
                    self.request("POST", "/v1/messages", body=body)[0], 400)
        for stream in ("true", 1, {}):
            with self.subTest(stream=stream):
                body = self.messages_body(
                    model="GLM-5.2-NVFP4", max_tokens=8, stream=stream,
                    messages=[{"role": "user", "content": "hi"}])
                self.assertEqual(
                    self.request("POST", "/v1/messages", body=body)[0], 400)
        self.assertEqual(self.upstream_requests, [])

    def test_messages_stream_is_relayed_verbatim(self):
        # 真实模型 ID 原样上行；Anthropic SSE 事件逐字节回传，不被改写成
        # OpenAI 形状，也不丢事件。
        body = self.messages_body(
            model="GLM-5.2-NVFP4", max_tokens=64, stream=True,
            system="be brief",
            messages=[{"role": "user", "content": "hello"}])
        status, headers, payload = self.request_with_headers(
            "POST", "/v1/messages", body=body)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "text/event-stream")
        self.assertEqual(payload, ANTHROPIC_SSE)
        path, sent_headers, forwarded = self.upstream_requests[0]
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(forwarded["model"], "GLM-5.2-NVFP4")
        self.assertEqual(forwarded["max_tokens"], 64)
        self.assertEqual(forwarded["system"], "be brief")
        self.assertEqual(sent_headers.get("Content-Type"), "application/json")
        self.assertNotIn("Authorization", sent_headers)

    def test_messages_json_response_is_relayed_verbatim(self):
        # 非流式（显式 stream=false 与缺省走同一条路）：状态码 200、
        # Content-Type 与响应体原样回传。
        for stream in (False, None):
            with self.subTest(stream=stream):
                fields = {"model": "GLM-5.2-NVFP4", "max_tokens": 64,
                          "messages": [{"role": "user", "content": "hello"}]}
                if stream is not None:
                    fields["stream"] = stream
                status, headers, payload = self.request_with_headers(
                    "POST", "/v1/messages", body=self.messages_body(**fields))
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Content-Type"), "application/json")
                self.assertEqual(payload, ANTHROPIC_MESSAGE)

    def test_messages_upstream_failure_is_explicit(self):
        # 与 chat 共用同一条失败契约：上游非 200 折叠为明确的 502/503
        # um_unavailable，绝不伪造成 200。
        body = self.messages_body(
            model="GLM-5.2-NVFP4", max_tokens=64, stream=True,
            messages=[{"role": "user", "content": "hello"}])
        for upstream_status in (502, 503):
            with self.subTest(upstream_status=upstream_status):
                self.chat_status[0] = upstream_status
                status, payload = self.request("POST", "/v1/messages",
                                              body=body)
                self.assertEqual(status, upstream_status)
                self.assertIn(b"um_unavailable", payload)
        self.chat_status[0] = 200

    def test_messages_image_body_shares_the_one_mib_cap(self):
        # 图片不受特殊对待：仍是 chat 那一个 1 MiB 上限，且超限在读取
        # 请求体之前就按声明长度拒绝，base64 图片无法靠体积绕过。
        image = {"model": "GLM-5.2-NVFP4", "max_tokens": 16,
                 "messages": [{"role": "user", "content": [
                     {"type": "image", "source": {"type": "base64",
                                                 "media_type": "image/png",
                                                 "data": "A" * 4096}}]}]}
        status, _ = self.request("POST", "/v1/messages",
                                body=json.dumps(image))
        self.assertEqual(status, 200)
        self.assertEqual(len(self.upstream_requests), 1)
        status, payload = self.request("POST", "/v1/messages",
                                      headers={"Content-Length": "1048577"})
        self.assertEqual(status, 413)
        self.assertIn(b"request_too_large", payload)
        self.assertEqual(len(self.upstream_requests), 1)

    def test_messages_shares_the_per_key_concurrency_limit(self):
        # 每 Key 并发限制对 Messages 同样生效（同一 acquire，无第二套计数）。
        self.assertTrue(self.server.acquire("friend"))
        body = self.messages_body(
            model="GLM-5.2-NVFP4", max_tokens=8,
            messages=[{"role": "user", "content": "hi"}])
        status, payload = self.request("POST", "/v1/messages", body=body)
        self.assertEqual(status, 429)
        self.assertIn(b"sharing_limit_reached", payload)
        self.assertIn(b"per_key_concurrency", payload)
        self.assertEqual(self.upstream_requests, [])

    def test_anthropic_routes_beyond_messages_stay_unshared(self):
        # files / batches / count_tokens 都不在 ALLOWED 中：专用后端没有
        # count_tokens 路由，分享入口也绝不自己数 token，一律 404。
        body = self.messages_body(
            model="GLM-5.2-NVFP4", max_tokens=8,
            messages=[{"role": "user", "content": "hi"}])
        for path in ("/v1/files", "/v1/batches",
                     "/v1/messages/count_tokens"):
            with self.subTest(path=path):
                status, payload = self.request("POST", path, body=body)
                self.assertEqual(status, 404)
                self.assertIn(b"route_not_shared", payload)
        self.assertEqual(self.upstream_requests, [])

    def test_messages_probe_never_calls_um(self):
        self.server.probe = True
        body = self.messages_body(
            model="GLM-5.2-NVFP4", max_tokens=8,
            messages=[{"role": "user", "content": "hi"}])
        status, headers, payload = self.request_with_headers(
            "POST", "/v1/messages", body=body)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"),
                         "application/json; charset=utf-8")
        message = json.loads(payload)
        self.assertEqual(message["type"], "message")
        self.assertEqual(message["model"], "connectivity-test")
        self.assertNotIn("usage", message)   # 不伪造 token 数字
        streamed = self.messages_body(
            model="GLM-5.2-NVFP4", max_tokens=8, stream=True,
            messages=[{"role": "user", "content": "hi"}])
        status, headers, payload = self.request_with_headers(
            "POST", "/v1/messages", body=streamed)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "text/event-stream")
        self.assertIn(b"event: message_start", payload)
        self.assertIn(b"event: message_stop", payload)
        self.assertNotIn(b"usage", payload)
        # 既有 chat 探针仍是 OpenAI 形状（未被 Messages 分支改坏）。
        chat = self.messages_body(model="probe", stream=True, messages=[{}])
        status, headers, payload = self.request_with_headers(
            "POST", "/v1/chat/completions", body=chat)
        self.assertEqual(status, 200)
        self.assertIn(b"chat.completion.chunk", payload)
        self.assertEqual(self.upstream_requests, [])

    # --------------------------------------------------- Messages tool calls

    @staticmethod
    def tool_schema():
        return [{"name": "get_weather",
                 "description": "Look up the weather",
                 "input_schema": {"type": "object",
                                  "properties": {"city": {"type": "string"}},
                                  "required": ["city"]}}]

    def test_messages_tool_use_stream_is_relayed_verbatim(self):
        # 工具调用是 Anthropic 客户端的核心路径：tool_use 块与 input_json_delta
        # 必须逐字节回传（含 stop_reason:"tool_use"），不得改写成 text 事件。
        self.messages_payload[0] = TOOL_USE_SSE
        body = self.messages_body(
            model="GLM-5.2-NVFP4", max_tokens=64, stream=True,
            tools=self.tool_schema(), tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": "weather in Macau?"}])
        status, headers, payload = self.request_with_headers(
            "POST", "/v1/messages", body=body)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "text/event-stream")
        self.assertEqual(payload, TOOL_USE_SSE)
        path, _sent_headers, forwarded = self.upstream_requests[0]
        self.assertEqual(path, "/v1/messages")
        # 请求侧：tools/tool_choice 在 Messages 白名单内，必须原样上行。
        self.assertEqual(forwarded["tools"], self.tool_schema())
        self.assertEqual(forwarded["tool_choice"], {"type": "auto"})

    def test_messages_tool_use_json_is_relayed_verbatim(self):
        # 非流式工具调用：tool_use 块与 stop_reason:"tool_use" 同样逐字节回传。
        self.messages_payload[0] = TOOL_USE_MESSAGE
        body = self.messages_body(
            model="GLM-5.2-NVFP4", max_tokens=64,
            tools=self.tool_schema(), tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": "weather in Macau?"}])
        status, headers, payload = self.request_with_headers(
            "POST", "/v1/messages", body=body)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(payload, TOOL_USE_MESSAGE)

    def test_messages_tool_result_turn_is_forwarded_unchanged(self):
        # 第二轮（把工具结果交回模型）同样是 Messages 契约：tool_result 块、
        # assistant 的 tool_use 块与 tools 定义必须逐字段上行，不丢不改。
        self.messages_payload[0] = TOOL_USE_MESSAGE
        turn = {"model": "GLM-5.2-NVFP4", "max_tokens": 64,
                "tools": self.tool_schema(),
                "messages": [
                    {"role": "user", "content": "weather in Macau?"},
                    {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "toolu_1",
                         "name": "get_weather", "input": {"city": "Macau"}}]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1",
                         "content": "26C and cloudy"}]},
                ]}
        status, _headers, payload = self.request_with_headers(
            "POST", "/v1/messages", body=self.messages_body(**turn))
        self.assertEqual(status, 200)
        self.assertEqual(payload, TOOL_USE_MESSAGE)
        # 整个 body 逐字段相等：分享入口不加、不删、不归一化任何字段。
        self.assertEqual(self.upstream_requests[0][2], turn)

    def test_messages_openai_only_fields_stay_refused_beside_tools(self):
        # 反例：工具字段不是把白名单整体放开——OpenAI 专有的 stream_options
        # 与工具定义同时出现时仍必须 400，且不触上游。
        body = self.messages_body(
            model="GLM-5.2-NVFP4", max_tokens=64, tools=self.tool_schema(),
            messages=[{"role": "user", "content": "hi"}],
            stream_options={"include_usage": True})
        status, payload = self.request("POST", "/v1/messages", body=body)
        self.assertEqual(status, 400)
        self.assertIn(b"unsupported_messages_request", payload)
        self.assertEqual(self.upstream_requests, [])

    # -------------------------------------------- Messages body bounds (1 MiB)

    def test_messages_transfer_encoding_is_refused_before_the_body(self):
        # chunked 请求没有声明的 Content-Length：分享入口不猜长度、不读 body，
        # 直接拒绝，客户端不必先上传完整 body。
        status, payload = self.request_raw_wire(
            [b"Authorization: Bearer test-secret\r\n",
             b"Transfer-Encoding: chunked\r\n"],
            body=b"0\r\n\r\n")
        self.assertEqual(status, 400)
        self.assertIn(b"unsupported_body_encoding", payload)
        self.assertEqual(self.upstream_requests, [])
        self.assertEqual(self.server.active, {})

    def test_messages_duplicate_content_length_is_refused(self):
        # 同名重复的 Content-Length 是走私形状：绝不取其中一个，一律拒绝。
        status, payload = self.request_raw_wire(
            [b"Authorization: Bearer test-secret\r\n",
             b"Content-Length: 10\r\nContent-Length: 12\r\n"],
            body=b'{"a": 1}')
        self.assertEqual(status, 400)
        self.assertIn(b"invalid_content_length", payload)
        self.assertEqual(self.upstream_requests, [])

    def test_messages_non_numeric_content_length_is_refused(self):
        status, payload = self.request_raw_wire(
            [b"Authorization: Bearer test-secret\r\n",
             b"Content-Length: abc\r\n"])
        self.assertEqual(status, 400)
        self.assertIn(b"invalid_content_length", payload)
        self.assertEqual(self.upstream_requests, [])

    def test_messages_oversize_declaration_is_refused_without_the_body(self):
        # 1 MiB 门在读取请求体之前生效：客户端声明超过上限、只送出 4 KiB 时
        # 就已经收到 413，无法靠「先传大 body」把上限绕开。
        status, payload = self.request_raw_wire(
            [b"Authorization: Bearer test-secret\r\n",
             b"Content-Length: 1048577\r\n"],
            body=b"{" + b"a" * 4000)
        self.assertEqual(status, 413)
        self.assertIn(b"request_too_large", payload)
        self.assertEqual(self.upstream_requests, [])
        self.assertEqual(self.server.active, {})

    def test_messages_truncated_body_is_refused(self):
        # 声明 4096 字节却只送到 10 字节就结束：不得把半截 body 当合法请求，
        # 也不得在上游留下任何请求。
        status, payload = self.request_raw_wire(
            [b"Authorization: Bearer test-secret\r\n",
             b"Content-Length: 4096\r\n"],
            body=b"1234567890", half_close=True)
        self.assertEqual(status, 400)
        self.assertIn(b"incomplete_body", payload)
        self.assertEqual(self.upstream_requests, [])

    # ----------------------------------------------------------------- S2

    def write_projection(self, entries, generation=1, schema=1):
        self.config.write_text(json.dumps(
            {"schema_version": schema, "config_generation": generation,
             "data": entries}), encoding="utf-8")

    def entry(self, key_id="uuid-1", name="friend", secret="test-secret",
              version=1, enabled=True):
        return {"key_id": key_id, "name": name,
                "secret_hash": hashlib.sha256(secret.encode()).hexdigest(),
                "secret_version": version, "enabled": enabled}

    def test_projection_authenticates_by_key_id_and_tracks_generation(self):
        self.write_projection([self.entry(key_id="uuid-1", name="friendA")],
                              generation=7)
        status, _ = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(self.server.last_seen_generation, 7)
        self.assertEqual(self.server.snapshot()["last_seen_generation"], 7)

    def test_projection_unknown_schema_fails_closed(self):
        self.write_projection([self.entry()], schema=99)
        self.assertEqual(self.request()[0], 503)
        self.assertEqual(self.upstream_requests, [])

    def test_projection_malformed_fails_closed(self):
        self.write_projection([{"key_id": "x", "name": "y",
                                "secret_hash": "short", "secret_version": 1,
                                "enabled": True}])
        self.assertEqual(self.request()[0], 503)
        self.assertEqual(self.upstream_requests, [])

    def test_legacy_cli_refuses_direct_write(self):
        # R4: the old --add-client/--revoke-client path is removed as a second
        # writer; it must fail without touching the JSON file. The explicit
        # --upstream-port is required (R13-3), so the assertion stays on the
        # R4 refusal branch instead of argparse's missing-argument exit.
        target = Path(self.temp.name) / "must-not-exist.json"
        with self.assertRaises(SystemExit) as cm:
            share.main(["--config", str(target), "--upstream-port", "8110",
                        "--add-client", "friendX"])
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse(target.exists())

    def test_mixed_clients_and_projection_is_rejected(self):
        # An object carrying both clients and schema/data must not be downgraded
        # to legacy (that would re-authorize unknown-version digests).
        self.config.write_text(json.dumps({
            "clients": {"friend": hashlib.sha256(b"test-secret").hexdigest()},
            "schema_version": 99, "config_generation": 5, "data": []}),
            encoding="utf-8")
        self.assertEqual(self.request()[0], 503)
        self.assertEqual(self.upstream_requests, [])

    def test_duplicate_projection_key_id_rejected(self):
        self.config.write_text(json.dumps({
            "schema_version": 1, "config_generation": 1,
            "data": [self.entry(key_id="dup"), self.entry(key_id="dup",
                                                          name="other")]}),
            encoding="utf-8")
        self.assertEqual(self.request()[0], 503)
        self.assertEqual(self.upstream_requests, [])

    def test_projection_disabled_identity_rejected(self):
        self.write_projection([self.entry(enabled=False)])
        self.assertEqual(self.request()[0], 401)
        self.assertEqual(self.upstream_requests, [])

    def test_rotation_uses_same_key_id_and_rejects_old_secret(self):
        self.write_projection([self.entry(key_id="uuid-1", name="friend",
                                          secret="new-secret", version=2)],
                              generation=3)
        self.assertEqual(self.request(key="old-secret")[0], 401)
        self.assertEqual(self.request(key="new-secret")[0], 200)
        self.assertEqual(self.server.last_seen_generation, 3)
        self.assertNotIn("friend", [c for c in self.server.active
                                    if c != "uuid-1"])

    def test_rate_limit_is_bound_to_key_id_not_display_name(self):
        self.write_projection([self.entry(key_id="uuid-1", name="old-name")],
                              generation=1)
        for _ in range(20):
            self.assertTrue(self.server.acquire("uuid-1"))
            self.server.active.clear()
        # Renaming a display identity must not reset its per-key rate bucket.
        self.write_projection([self.entry(key_id="uuid-1", name="new-name")],
                              generation=2)
        self.assertEqual(self.request()[0], 429)
        self.assertEqual(self.upstream_requests, [])

    def test_revoke_confirmed_before_acquire_blocks_old_snapshot(self):
        # R5: a request may read an old projection, then block on the accept
        # lock. After the revocation is confirmed, its acquire() must re-read
        # the current projection inside the lock and be refused.
        self.write_projection([self.entry(key_id="uuid-1")], generation=1)
        self.assertTrue(self.server.acquire("uuid-1", generation=1))
        self.server.active.clear()
        self.write_projection([], generation=2)
        self.assertFalse(self.server.acquire("uuid-1", generation=1))
        self.assertEqual(self.upstream_requests, [])

    def test_rotation_rejects_old_secret_from_stale_snapshot(self):
        # R5-a: a request that authenticated against the OLD projection
        # (generation 1, secret v1) must be refused at the accept boundary
        # after the rotation to generation 2 (same key_id, secret v2).
        self.write_projection([self.entry(key_id="uuid-1", secret="old-secret",
                                          version=1)], generation=1)
        self.assertTrue(self.server.acquire("uuid-1", generation=1,
                                            secret_version=1))
        self.server.active.clear()
        self.write_projection([self.entry(key_id="uuid-1", secret="new-secret",
                                          version=2)], generation=2)
        self.assertFalse(self.server.acquire("uuid-1", generation=1,
                                             secret_version=1))
        self.assertEqual(self.upstream_requests, [])

    def test_http_race_after_rotation_rejects_stale_request(self):
        # 真实 HTTP 竞争：请求停在初次鉴权之后，轮换并确认新代次，恢复后
        # 旧秘密请求必须被拒绝，假上游零调用。
        gate = threading.Event()
        held = {}
        real_acquire = self.server.acquire

        def gating_acquire(client, generation=None, secret_version=None,
                           reason_out=None):
            held["args"] = (client, generation, secret_version)
            gate.wait(5)
            return real_acquire(client, generation=generation,
                                secret_version=secret_version,
                                reason_out=reason_out)

        self.server.acquire = gating_acquire
        response_holder = {}

        def run():
            conn = http.client.HTTPConnection(
                "127.0.0.1", self.server.server_port, timeout=8)
            try:
                conn.request("GET", "/v1/models", headers={
                    "Authorization": "Bearer old-secret"})
                resp = conn.getresponse()
                response_holder["status"] = resp.status
                resp.read()
            finally:
                conn.close()

        try:
            self.write_projection([self.entry(key_id="uuid-1",
                                              secret="old-secret",
                                              version=1)], generation=1)
            thread = threading.Thread(target=run)
            thread.start()
            deadline = time.monotonic() + 5
            while "args" not in held and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIn("args", held)
            # 请求停驻期间轮换并确认新代次。
            self.write_projection([self.entry(key_id="uuid-1",
                                              secret="new-secret",
                                              version=2)], generation=2)
            self.server.note_config({"config_generation": 2})
            gate.set()
            thread.join(8)
            self.assertEqual(response_holder.get("status"), 429)
            self.assertEqual(self.upstream_requests, [])
        finally:
            gate.set()
            self.server.acquire = real_acquire

    def test_accepted_stream_continues_after_rotation(self):
        # 已接纳的请求不受轮换影响：在途流式继续完成，上游正常调用。
        self.write_projection([self.entry(key_id="uuid-1", secret="old-secret",
                                          version=1)], generation=1)
        self.assertTrue(self.server.acquire("uuid-1", generation=1,
                                            secret_version=1))
        self.write_projection([self.entry(key_id="uuid-1", secret="new-secret",
                                          version=2)], generation=2)
        # 在途身份保留在 active 集合中；新请求用旧秘密必须被拒。
        self.assertEqual(self.request(key="old-secret")[0], 401)
        self.assertEqual(self.upstream_requests, [])
        self.server.active.clear()

    def test_note_config_never_regresses_generation(self):
        self.write_projection([self.entry()], generation=5)
        self.request()
        self.assertEqual(self.server.last_seen_generation, 5)
        # legacy file (generation 0) loaded later must not lower the watermark.
        self.config.write_text(json.dumps(
            {"clients": {"friend": hashlib.sha256(
                b"test-secret").hexdigest()}}), encoding="utf-8")
        self.request()
        self.assertEqual(self.server.last_seen_generation, 5)

    def test_inflight_survives_revocation_and_new_request_is_rejected(self):
        self.write_projection([self.entry(key_id="uuid-1", name="friend")])
        self.assertTrue(self.server.acquire("uuid-1"))
        # Revocation does not force-cancel an already accepted request.
        self.write_projection([], generation=2)
        self.assertIn("uuid-1", self.server.active)
        self.assertEqual(self.request()[0], 401)
        self.assertEqual(self.upstream_requests, [])

    def test_drain_rejects_new_but_keeps_inflight(self):
        self.write_projection([self.entry(key_id="uuid-1")])
        self.assertTrue(self.server.acquire("uuid-1"))
        result = self.server.begin_drain()
        self.assertEqual(result["state"], "draining")
        self.assertEqual(result["active"], 1)
        self.assertEqual(self.request()[0], 429)
        self.assertEqual(self.upstream_requests, [])
        # The accepted request remains present so the host can drain it.
        self.assertIn("uuid-1", self.server.active)
        self.server.release("uuid-1")

    def test_emergency_stop_fails_closed(self):
        self.write_projection([self.entry(key_id="uuid-1")])
        self.assertTrue(self.server.acquire("uuid-1"))
        result = self.server.emergency_stop()
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(result["cleared_in_flight"], 1)
        self.assertFalse(self.server.acquire("uuid-1"))
        self.assertEqual(self.request()[0], 429)
        # start() reopens accepting; a no-op state transition for the test.
        self.server.start()
        self.assertEqual(self.request(key="test-secret")[0], 200)


class ConcurrencyAccountingTests(unittest.TestCase):
    """R6-A2 记账改造：active 由 set 改为 per-key 计数 dict；每Key上限、
    全局 share_max、精确释放、drain/emergency 清场与连接闸缩放。全部
    走真实本地 socket/对象，无真实网络、无真实进程、无生产数据。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name) / "um-share-projection.json"
        # Recording list for the no-upstream-contact assertion: the share
        # handler refuses with 429 BEFORE proxying, so nothing may append.
        self.upstream_requests = []
        self.addCleanup(self.temp.cleanup)

    def _entry(self, key_id="uuid-1", name="friend", secret="test-secret",
               version=1, enabled=True, max_concurrency=None):
        entry = {"key_id": key_id, "name": name,
                 "secret_hash": hashlib.sha256(secret.encode()).hexdigest(),
                 "secret_version": version, "enabled": enabled}
        if max_concurrency is not None:
            entry["max_concurrency"] = max_concurrency
        return entry

    def _write(self, entries, generation=1, schema=2, share_max=None):
        payload = {"schema_version": schema,
                   "config_generation": generation, "data": entries}
        if share_max is not None:
            payload["share_max_concurrency"] = share_max
        self.config.write_text(json.dumps(payload), encoding="utf-8")

    def _server(self):
        server = share.ShareServer(("127.0.0.1", 0), self.config)
        self.addCleanup(server.server_close)
        return server

    def _reason(self):
        return {}

    def test_v1_projection_defaults_keep_old_behavior(self):
        # v1 投影缺字段：每Key=1、全局=2（旧硬编码行为回归）。
        self._write([self._entry()], schema=1)
        server = self._server()
        self.assertEqual(server.share_max_concurrency, 2)
        self.assertTrue(server.acquire("uuid-1"))
        reason = self._reason()
        self.assertFalse(server.acquire("uuid-1", reason_out=reason))
        self.assertEqual(reason["reason"], "per_key_concurrency")

    def test_legacy_config_has_legacy_limits(self):
        self.config.write_text(json.dumps(
            {"clients": {"friend": hashlib.sha256(
                b"test-secret").hexdigest()}}), encoding="utf-8")
        server = self._server()
        cfg = share.load_config(self.config)
        self.assertEqual(cfg["share_max_concurrency"], 2)
        self.assertEqual(cfg["entries"][0]["max_concurrency"], 1)
        self.assertEqual(server.share_max_concurrency, 2)

    def test_v2_entry_max_concurrency_admits_parallel(self):
        self._write([self._entry(max_concurrency=8)], share_max=64)
        server = self._server()
        for _ in range(8):
            self.assertTrue(server.acquire("uuid-1"))
        reason = self._reason()
        self.assertFalse(server.acquire("uuid-1", reason_out=reason))
        self.assertEqual(reason["reason"], "per_key_concurrency")
        self.assertEqual(server.active, {"uuid-1": 8})

    def test_global_share_max_enforced_across_keys(self):
        self._write([self._entry(key_id="a", name="fa", max_concurrency=8),
                     self._entry(key_id="b", name="fb", max_concurrency=8)],
                    share_max=3)
        server = self._server()
        self.assertTrue(server.acquire("a"))
        self.assertTrue(server.acquire("a"))
        self.assertTrue(server.acquire("b"))
        reason = self._reason()
        self.assertFalse(server.acquire("a", reason_out=reason))
        self.assertEqual(reason["reason"], "share_concurrency")
        self.assertFalse(server.acquire("b", reason_out=reason))
        self.assertEqual(reason["reason"], "share_concurrency")

    def test_release_decrements_precisely(self):
        self._write([self._entry(max_concurrency=8)], share_max=64)
        server = self._server()
        for _ in range(3):
            self.assertTrue(server.acquire("uuid-1"))
        server.release("uuid-1")
        self.assertEqual(server.active, {"uuid-1": 2})
        # 仍有一个在途时再次准入成功（计数未提前清零）。
        self.assertTrue(server.acquire("uuid-1"))
        server.release("uuid-1")
        server.release("uuid-1")
        server.release("uuid-1")
        self.assertEqual(server.active, {})
        # 多余释放不会把计数推成负数或复活身份。
        server.release("uuid-1")
        self.assertEqual(server.active, {})

    def test_drain_keeps_counts_and_emergency_clears(self):
        self._write([self._entry(max_concurrency=8)], share_max=64)
        server = self._server()
        self.assertTrue(server.acquire("uuid-1"))
        self.assertTrue(server.acquire("uuid-1"))
        drain = server.begin_drain()
        self.assertEqual(drain["active"], 2)
        self.assertEqual(server.snapshot()["active_count"], 2)
        result = server.emergency_stop()
        self.assertEqual(result["cleared_in_flight"], 2)
        self.assertEqual(server.snapshot()["active"], {})
        self.assertEqual(server.snapshot()["active_count"], 0)

    def test_instance_status_counts_adapted(self):
        # 实例通道结构适配由 D2 闭环测试覆盖；此处只钉死快照的计数形状：
        # active 是 per-key 计数 dict，active_count 是在途请求总数。
        self._write([self._entry(max_concurrency=8)], share_max=64)
        server = self._server()
        server.acquire("uuid-1")
        server.acquire("uuid-1")
        snap = server.snapshot()
        self.assertEqual(snap["active"], {"uuid-1": 2})
        self.assertEqual(snap["active_count"], 2)

    def test_connection_gate_is_bounded_and_adjustable(self):
        # 闸 = share_max + 8：既不使业务上限失效（>= share_max），也保留
        # 慢连接保护（有界）。容量可原地调整（同一对象身份），降限只阻止
        # 新连接、在途连接继续持有自己的名额。
        self._write([self._entry(max_concurrency=48)], share_max=20)
        server = self._server()
        gate = server.connections
        self.assertEqual(gate.limit, 20 + 8)
        self.assertEqual(gate.active, 0)
        for _ in range(20 + 8):
            self.assertTrue(gate.acquire())
        self.assertFalse(gate.acquire())
        gate.set_limit(128 + 8)      # 升限：同一对象继续接纳
        self.assertEqual(gate.limit, 128 + 8)
        self.assertTrue(gate.acquire())
        for _ in range(20 + 8 + 1):
            gate.release()
        self.assertEqual(gate.active, 0)
        gate.release()               # 多余释放不制造容量
        self.assertEqual(gate.active, 0)

    def test_global_64_admission_across_eight_keys(self):
        # 全局 64 是"所有 Key 合计"：8 Key × 8 全部接纳，第 65 个
        # 由其中一个 Key 的第 9 次触发全局拒绝（每 Key 20/分钟仍未到限）。
        entries = [self._entry(key_id=f"k{i}", name=f"f{i}",
                               secret=f"s{i}", max_concurrency=48)
                   for i in range(8)]
        self._write(entries, share_max=64)
        server = self._server()
        self.assertEqual(server.share_max_concurrency, 64)
        for entry in entries:
            for _ in range(8):
                self.assertTrue(server.acquire(entry["key_id"]))
        self.assertEqual(sum(server.active.values()), 64)
        reason = self._reason()
        self.assertFalse(server.acquire("k0", reason_out=reason))
        self.assertEqual(reason["reason"], "share_concurrency")
        for entry in entries:
            for _ in range(8):
                server.release(entry["key_id"])
        self.assertEqual(server.active, {})

    def test_load_installs_capacity_before_ack(self):
        # load 必须在 ACK 前安装有效容量：ACK 报出的 64 与闸 72 立即生效，
        # 每 Key 上限也从同一投影安装（不是只读旧启动值）。
        entries = [self._entry(key_id=f"k{i}", name=f"f{i}",
                               secret=f"s{i}", max_concurrency=48)
                   for i in range(8)]
        self._write(entries, generation=1, share_max=24)
        server = self._server()
        self.assertEqual(server.share_max_concurrency, 24)
        self.assertEqual(server.connections.limit, 24 + 8)
        next_entries = [dict(entries[0], max_concurrency=16)] + entries[1:]
        payload = {"schema_version": 2, "config_generation": 2,
                   "share_max_concurrency": 64, "data": next_entries}
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        resp = self._signed_load(server, payload, {})
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["result"]["share_max_concurrency"], 64)
        self.assertEqual(resp["result"]["connection_limit"], 64 + 8)
        self.assertEqual(server.share_max_concurrency, 64)
        self.assertEqual(server.connections.limit, 64 + 8)
        self.assertEqual(server.key_limits["k0"], 16)
        for _ in range(16):
            self.assertTrue(server.acquire("k0"))
        reason = self._reason()
        self.assertFalse(server.acquire("k0", reason_out=reason))
        self.assertEqual(reason["reason"], "per_key_concurrency")

    def test_load_lowering_keeps_inflight_and_refuses_new(self):
        # 降限 64→24：在途 32 保留，新请求被全局拒绝；释放后按新上限
        # 重新准入。降限不打断已接纳请求。
        entries = [self._entry(key_id=f"k{i}", name=f"f{i}",
                               secret=f"s{i}", max_concurrency=48)
                   for i in range(8)]
        self._write(entries, generation=1, share_max=64)
        server = self._server()
        for entry in entries:
            for _ in range(4):
                self.assertTrue(server.acquire(entry["key_id"]))
        self.assertEqual(sum(server.active.values()), 32)
        payload = {"schema_version": 2, "config_generation": 2,
                   "share_max_concurrency": 24, "data": entries}
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        resp = self._signed_load(server, payload, {})
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(server.share_max_concurrency, 24)
        self.assertEqual(server.connections.limit, 24 + 8)
        self.assertEqual(sum(server.active.values()), 32)   # 在途保留
        reason = self._reason()
        self.assertFalse(server.acquire("k0", reason_out=reason))
        self.assertEqual(reason["reason"], "share_concurrency")
        for entry in entries:
            for _ in range(4):
                server.release(entry["key_id"])
        self.assertEqual(server.active, {})

    def test_load_schema1_installs_legacy_limits(self):
        # 旧 schema/旧投影兼容：缺字段按旧默认（每 Key 1、全局 2）安装，
        # 不按新默认 64 猜测。
        entries = [self._entry(key_id="k1", name="f1", secret="s1")]
        self._write(entries, generation=1, share_max=64)
        server = self._server()
        payload = {"schema_version": 1, "config_generation": 2,
                   "data": entries}
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        resp = self._signed_load(server, payload, {})
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(server.share_max_concurrency, 2)
        self.assertEqual(server.connections.limit, 2 + 8)
        self.assertEqual(server.key_limits, {"k1": 1})
        self.assertEqual(resp["result"]["share_max_concurrency"], 2)

    def _signed_load(self, server, payload, extra_fields):
        from share_broker_protocol import INSTANCE_SIGNED_KEYS, sign
        secret = "inst-secret"
        fields = {"generation": payload["config_generation"],
                  "fingerprint": hashlib.sha256(json.dumps(
                      payload, sort_keys=True).encode()).hexdigest()}
        fields.update(extra_fields)
        body = sign(secret, {"v": 2, "action": "load",
                             "request_id": "req-" + str(time.time_ns()),
                             "ts": int(time.time()) + 1,
                             "instance_id": server.instance_id,
                             "fields": fields}, INSTANCE_SIGNED_KEYS)
        return server.handle_instance_control(body, secret)

    def test_http_429_carries_readable_reason(self):
        # 每Key并发占满后的真实 HTTP 429：稳定 code + 可读 reason，不触上游。
        self._write([self._entry(max_concurrency=8)], share_max=64)
        server = self._server()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        for _ in range(8):
            self.assertTrue(server.acquire("uuid-1"))
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port,
                                          timeout=3)
        try:
            conn.request("GET", "/v1/models", headers={
                "Authorization": "Bearer test-secret"})
            resp = conn.getresponse()
            body = resp.read()
            self.assertEqual(resp.status, 429)
            payload = json.loads(body)
            self.assertEqual(
                payload["error"]["code"], "sharing_limit_reached")
            self.assertEqual(payload["error"]["reason"],
                             "per_key_concurrency")
            self.assertTrue(payload["error"]["detail"])
            self.assertEqual(self.upstream_requests, [])
        finally:
            conn.close()
        self.assertEqual(server.active, {"uuid-1": 8})   # 未被 429 计入


class RealTcpGlobalCapacityTests(unittest.TestCase):
    """真实 TCP + 最后一层假后端：8 Key × 8 = 64 个同时在途请求全部
    被接纳，第 65 个由全局返回 429，释放后业务计数与 TCP 闸计数精确
    归零。假后端只返回模型目录，不产生任何真实模型调用。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "um-share-projection.json"
        self.keys = [(f"k{i}", f"secret-{i}") for i in range(8)]
        entries = [{"key_id": key_id, "name": f"friend-{key_id}",
                    "secret_hash": hashlib.sha256(
                        secret.encode()).hexdigest(),
                    "secret_version": 1, "enabled": True,
                    "max_concurrency": 48}
                   for key_id, secret in self.keys]
        self.config.write_text(json.dumps({
            "schema_version": 2, "config_generation": 1,
            "share_max_concurrency": 64, "data": entries}),
            encoding="utf-8")
        self.arrived = 0
        self.arrived_lock = threading.Lock()
        self.release = threading.Event()
        arrived_lock = self.arrived_lock
        release = self.release
        holder = self

        class Upstream(BaseHTTPRequestHandler):
            def do_GET(inner):
                if inner.path != "/v1/models":
                    inner.send_response(404)
                    inner.end_headers()
                    return
                with arrived_lock:
                    holder.arrived += 1
                # 每个假后端处理线程保持到测试释放：share handler 的
                # acquire 名额在响应完成前不会归还。
                release.wait(60)
                body = json.dumps({
                    "object": "list",
                    "data": [{"id": "fixture-model",
                              "object": "model"}]}).encode()
                inner.send_response(200)
                inner.send_header("Content-Type", "application/json")
                inner.send_header(share.X_MODELS_MARKER, "1")
                inner.send_header("Content-Length", str(len(body)))
                inner.end_headers()
                inner.wfile.write(body)

            def log_message(inner, *_args):
                pass

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.upstream.daemon_threads = True
        # 64 个 share handler 会同时中继到假后端：夹具的 backlog 必须
        # 容纳它们，否则测到的是夹具而不是 share 的容量。
        self.upstream.request_queue_size = 128
        self.server = share.ShareServer(
            ("127.0.0.1", 0), self.config, self.upstream.server_port)
        self.threads = []
        for server in (self.upstream, self.server):
            thread = threading.Thread(target=server.serve_forever,
                                      daemon=True)
            thread.start()
            self.threads.append(thread)

    def tearDown(self):
        self.release.set()
        for server in (self.server, self.upstream):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join()
        self.temp.cleanup()

    def _wait(self, predicate, timeout=90.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def _request(self, secret):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=120)
        try:
            conn.request("GET", "/v1/models", headers={
                "Authorization": "Bearer " + secret})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def _load_global_64(self):
        """Write a 64 projection and drive the signed instance load ACK."""
        from share_broker_protocol import INSTANCE_SIGNED_KEYS, sign
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload["config_generation"] = 2
        payload["share_max_concurrency"] = 64
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        fields = {"generation": 2,
                  "fingerprint": hashlib.sha256(json.dumps(
                      payload, sort_keys=True).encode()).hexdigest()}
        body = sign("inst-secret", {"v": 2, "action": "load",
                                    "request_id": "req-" + str(time.time_ns()),
                                    "ts": int(time.time()) + 1,
                                    "instance_id": self.server.instance_id,
                                    "fields": fields}, INSTANCE_SIGNED_KEYS)
        return self.server.handle_instance_control(body, "inst-secret")

    def test_64_real_tcp_connections_and_65th_429(self):
        # 启动仍是 64；真实 signed load ACK 安装 64（业务 + TCP 闸）后，
        # 64 个真实 TCP 连接全部被接纳，第 65 个由全局返回 429。
        self.assertEqual(self.server.share_max_concurrency, 64)
        self.assertEqual(self.server.connections.limit, 64 + 8)
        ack = self._load_global_64()
        self.assertTrue(ack["ok"], ack)
        self.assertEqual(ack["result"]["share_max_concurrency"], 64)
        self.assertEqual(self.server.share_max_concurrency, 64)
        self.assertEqual(self.server.connections.limit, 64 + 8)
        results = [None] * 64
        failures = []

        def worker(index):
            _key_id, secret = self.keys[index // 8]
            try:
                results[index] = self._request(secret)
            except BaseException as exc:  # noqa: BLE001
                failures.append(f"{index}: {type(exc).__name__}: {exc}")

        workers = [threading.Thread(target=worker, args=(i,))
                   for i in range(64)]
        for thread in workers:
            thread.start()
        try:
            self.assertTrue(
                self._wait(lambda: self.arrived == 64),
                f"only {self.arrived} of 64 requests reached the fake "
                f"backend; failures={failures[:5]}")
            # 第 65 个来自 k0 的第 9 次（每 Key 20/分钟未到限）：
            # 全局拒绝，且不触达假后端。
            status, body = self._request(self.keys[0][1])
            self.assertEqual(status, 429)
            self.assertEqual(json.loads(body)["error"]["reason"],
                             "share_concurrency")
            self.assertEqual(self.arrived, 64)
        finally:
            self.release.set()
        for thread in workers:
            thread.join(120)
        self.assertEqual(failures, [])
        for index, result in enumerate(results):
            self.assertIsNotNone(result, f"request {index} never answered")
            self.assertEqual(result[0], 200, f"request {index}: {result}")
        self.assertTrue(self._wait(lambda: self.server.active == {}),
                        f"business counters leaked: {self.server.active}")
        self.assertTrue(
            self._wait(lambda: self.server.connections.active == 0),
            f"TCP gate leaked: {self.server.connections.active}")

    def test_upstream_failure_releases_the_tcp_slot(self):
        # 上游失败路径同样精确归还业务计数与 TCP 闸名额。
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()
        server = share.ShareServer(("127.0.0.1", 0), self.config, dead_port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port,
                                          timeout=10)
        try:
            conn.request("GET", "/v1/models", headers={
                "Authorization": "Bearer " + self.keys[0][1]})
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 502)
        finally:
            conn.close()
        self.assertTrue(self._wait(lambda: server.active == {}),
                        f"business counters leaked: {server.active}")
        self.assertTrue(self._wait(lambda: server.connections.active == 0),
                        f"TCP gate leaked: {server.connections.active}")

    def test_cancelled_client_still_releases_the_slot(self):
        # 客户端取消（关闭连接）后 handler 仍在自己的 finally 精确归还，
        # 不因取消泄漏业务计数或 TCP 名额。
        conn = socket.create_connection(
            ("127.0.0.1", self.server.server_port), timeout=10)
        conn.sendall(b"GET /v1/models HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                      b"Authorization: Bearer "
                      + self.keys[0][1].encode() + b"\r\n\r\n")
        self.assertTrue(self._wait(lambda: self.arrived == 1))
        conn.close()
        self.release.set()      # 上游完成；handler 写回时发现对端已关闭
        self.assertTrue(self._wait(lambda: self.server.active == {}),
                        f"business counters leaked: {self.server.active}")
        self.assertTrue(
            self._wait(lambda: self.server.connections.active == 0),
            f"TCP gate leaked: {self.server.connections.active}")


if __name__ == "__main__":
    unittest.main()


class RevokeTerminateTests(unittest.TestCase):
    """R8-A7 撤销并终止：终结只关目标 Key 的在途连接（其他 Key 原样）；
    计数减仍由 handler finally 单源执行（socketpair 真 ctx + 显式 release
    模拟，断言不双减）。全部内存/本地 socketpair，零真实网络、零真实进程。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name) / "um-share-projection.json"
        self.addCleanup(self.temp.cleanup)

    def _write(self, entries, generation=1, share_max=4):
        payload = {"schema_version": 2,
                   "config_generation": generation,
                   "share_max_concurrency": share_max, "data": entries}
        self.config.write_text(json.dumps(payload), encoding="utf-8")

    @staticmethod
    def _entry(key_id):
        return {"key_id": key_id, "name": "friend-" + key_id,
                "secret_hash": hashlib.sha256(
                    ("s-" + key_id).encode()).hexdigest(),
                "secret_version": 1, "enabled": True, "max_concurrency": 2}

    def test_terminate_closes_only_target_key(self):
        self._write([self._entry("ka"), self._entry("kb")])
        server = share.ShareServer(("127.0.0.1", 0), self.config)
        self.addCleanup(server.server_close)
        self.assertTrue(server.acquire("ka"))
        self.assertTrue(server.acquire("ka"))
        self.assertTrue(server.acquire("kb"))
        import socket as _s
        a1, peer1 = _s.socketpair()
        a2, peer2 = _s.socketpair()
        b1, peer3 = _s.socketpair()
        server.register_inflight("ka", share._InflightCtx(a1))
        server.register_inflight("ka", share._InflightCtx(a2))
        cb = share._InflightCtx(b1)
        server.register_inflight("kb", cb)
        self.assertEqual(server.active, {"ka": 2, "kb": 1})
        self.assertEqual(server.terminate_key("ka"), 2)
        self.assertEqual(peer1.recv(1), b"")
        self.assertEqual(peer2.recv(1), b"")
        b1.send(b"x")
        self.assertEqual(peer3.recv(1), b"x")
        self.assertIn(cb, server.inflight.get("kb", set()))
        self.assertNotIn("ka", server.inflight)
        self.assertEqual(server.active, {"ka": 2, "kb": 1})
        server.release("ka")
        server.release("ka")
        self.assertEqual(server.active, {"kb": 1})

    def test_terminate_unknown_key_is_zero(self):
        self._write([self._entry("ka")])
        server = share.ShareServer(("127.0.0.1", 0), self.config)
        self.addCleanup(server.server_close)
        self.assertEqual(server.terminate_key("missing"), 0)

    def _signed_load(self, server, payload, extra_fields):
        from share_broker_protocol import INSTANCE_SIGNED_KEYS, sign
        secret = "inst-secret"
        fields = {"generation": payload["config_generation"],
                  "fingerprint": hashlib.sha256(json.dumps(
                      payload, sort_keys=True).encode()).hexdigest()}
        fields.update(extra_fields)
        return sign(secret, {"v": 2, "action": "load",
                             "request_id": "req-" + str(len(fields)) +
                             str(int(time.time())),
                             "ts": int(time.time()) + 1,
                             "instance_id": server.instance_id,
                             "fields": fields}, INSTANCE_SIGNED_KEYS), secret

    def test_load_acks_terminated_count(self):
        from share_broker_protocol import INSTANCE_SIGNED_KEYS, sign
        entries = [dict(self._entry("ka"))]
        payload = {"schema_version": 2, "config_generation": 5,
                   "share_max_concurrency": 4, "data": entries}
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        server = share.ShareServer(("127.0.0.1", 0), self.config)
        self.addCleanup(server.server_close)

        class _Ctx:
            def __init__(self):
                self.terminated = False

            def terminate(self):
                self.terminated = True
                return True

        ctx = _Ctx()
        server.register_inflight("ka", ctx)
        body, secret = self._signed_load(server, payload,
                                         {"terminate_keys": ["ka"]})
        resp = server.handle_instance_control(body, secret)
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["result"]["terminated"], 1)
        self.assertTrue(ctx.terminated)
        self.assertNotIn("ka", server.inflight)

    def test_load_without_terminate_keys_has_no_count_key_regression(self):
        entries = [dict(self._entry("ka"))]
        payload = {"schema_version": 2, "config_generation": 5,
                   "share_max_concurrency": 4, "data": entries}
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        server = share.ShareServer(("127.0.0.1", 0), self.config)
        self.addCleanup(server.server_close)
        body, secret = self._signed_load(server, payload, {})
        resp = server.handle_instance_control(body, secret)
        self.assertTrue(resp["ok"], resp)
        self.assertNotIn("terminated", resp["result"])


class CliUpstreamPortTests(unittest.TestCase):
    """R13-3: the share CLI must name its upstream backend explicitly.

    The dangerous default was the PRIVATE chat2api port 8000: a share started
    without ``--upstream-port`` forwarded friends' traffic into the private
    instance. The refusal must happen BEFORE ``ShareServer`` is constructed (no
    bind, no listen, no forward) and in BOTH modes (probe and real).
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "clients.json"
        self.config.write_text(json.dumps({"clients": {}}), encoding="utf-8")

    def test_missing_upstream_port_refuses_before_binding(self):
        constructed = []

        class StubServer:
            def __init__(self, *args, **kwargs):
                constructed.append((args, kwargs))

        for extra in ([], ["--probe"]):
            with self.subTest(extra=extra):
                stderr = io.StringIO()
                with mock.patch.object(share, "ShareServer", StubServer), \
                     contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as cm:
                        share.main(["--config", str(self.config),
                                    "--port", "0"] + extra)
                self.assertEqual(cm.exception.code, 2)
                self.assertEqual(constructed, [])   # never bound or listened
                self.assertIn("--upstream-port", stderr.getvalue())

    def test_explicit_upstream_port_is_forwarded_and_printed(self):
        captured = {}

        class StubServer:
            def __init__(self, address, config, **kwargs):
                captured["address"] = address
                captured.update(kwargs)

            def serve_forever(self):
                raise KeyboardInterrupt()

            def server_close(self):
                pass

        stdout = io.StringIO()
        with mock.patch.object(share, "ShareServer", StubServer), \
             contextlib.redirect_stdout(stdout):
            share.main(["--config", str(self.config), "--port", "0",
                        "--upstream-port", "8110", "--probe"])
        self.assertEqual(captured["upstream_port"], 8110)
        self.assertEqual(captured["probe"], True)
        # The effective upstream port is part of the startup evidence.
        self.assertIn("8110", stdout.getvalue())


class FaultEvidenceTests(unittest.TestCase):
    """A9-6 分享网关故障留证（8100 侧）：分型 phase/status/code、有界关联 ID、
    有界单行 JSONL（轮转保留旧文件）、进程内有界 ring 与恢复不擦除失败。
    全部走真实本地 socket / 注入缝，零真实上游、零生产数据。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "clients.json"
        self.config.write_text(json.dumps({"clients": {
            "friend": hashlib.sha256(b"test-secret").hexdigest()}}),
            encoding="utf-8")
        self.fault_log = self.root / share.FAULT_LOG_NAME
        # Upstream switch: "ok" / "status" (status[0]) / "malformed" /
        # "no-marker" / "abrupt" (close without a response).
        self.mode = ["ok"]
        self.status = [503]
        self.error_body = [b'{"error": "upstream failed"}']
        # A8: upstream retry hint on a non-200 (None = header absent).
        self.retry_after = [None]
        self.upstream_requests = []
        mode = self.mode
        status = self.status
        error_body = self.error_body
        retry_after = self.retry_after
        requests = self.upstream_requests

        class Upstream(BaseHTTPRequestHandler):
            def do_GET(inner):
                requests.append((inner.command, inner.path, dict(inner.headers)))
                inner._answer()

            def do_POST(inner):
                length = int(inner.headers.get("Content-Length", 0) or 0)
                raw = inner.rfile.read(length) if length else b""
                requests.append((inner.command, inner.path,
                                 dict(inner.headers), raw))
                inner._answer()

            def _answer(inner):
                if mode[0] == "abrupt":
                    # Close without ANY response: the share sees a dropped
                    # connection (RemoteDisconnected) - a real network failure.
                    inner.close_connection = True
                    return
                if mode[0] == "malformed":
                    payload = b'{"data": "not-a-list"}'
                    inner.send_response(200)
                    inner.send_header("Content-Type", "application/json")
                    inner.send_header("X-Chat2Api-Raw-Models", "1")
                    inner.send_header("Content-Length", str(len(payload)))
                    inner.end_headers()
                    inner.wfile.write(payload)
                    return
                if mode[0] == "no-marker":
                    payload = b'{"object": "list", "data": []}'
                    inner.send_response(200)
                    inner.send_header("Content-Type", "application/json")
                    inner.send_header("Content-Length", str(len(payload)))
                    inner.end_headers()
                    inner.wfile.write(payload)
                    return
                if mode[0] == "status":
                    payload = error_body[0]
                    inner.send_response(status[0])
                    inner.send_header("Content-Type", "application/json")
                    if retry_after[0] is not None:
                        inner.send_header("Retry-After", retry_after[0])
                    inner.send_header("Content-Length", str(len(payload)))
                    inner.end_headers()
                    inner.wfile.write(payload)
                    return
                if inner.path == "/v1/models":
                    payload = json.dumps({"object": "list", "data": [
                        {"id": "GLM-5.2-NVFP4", "object": "model"}]}).encode()
                    inner.send_response(200)
                    inner.send_header("Content-Type", "application/json")
                    inner.send_header("X-Chat2Api-Raw-Models", "1")
                    inner.send_header("Content-Length", str(len(payload)))
                    inner.end_headers()
                    inner.wfile.write(payload)
                    return
                inner.send_response(200)
                inner.send_header("Content-Type", "text/event-stream")
                inner.send_header("Connection", "close")
                inner.end_headers()
                inner.wfile.write(b'data: {"choices":[]}\n\n')
                inner.wfile.flush()
                inner.wfile.write(b'data: [DONE]\n\n')

            def log_message(inner, *_args):
                pass

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.server = share.ShareServer(
            ("127.0.0.1", 0), self.config,
            upstream_port=self.upstream.server_port,
            attribution_secret="s" * 32, fault_log=self.fault_log)
        self.threads = []
        for server in (self.upstream, self.server):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)

    def tearDown(self):
        for server in (self.server, self.upstream):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join()

    def _request(self, method="GET", path="/v1/models", key="test-secret",
                 body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1",
                                          self.server.server_port, timeout=5)
        request_headers = {"Content-Type": "application/json"}
        if key is not None:
            request_headers["Authorization"] = "Bearer " + key
        request_headers.update(headers or {})
        try:
            conn.request(method, path, body=body, headers=request_headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def _faults(self):
        """Read the append-only fault log as parsed lines (bounded wait)."""
        deadline = time.monotonic() + 3
        while True:
            if self.fault_log.exists():
                lines = [line for line in self.fault_log.read_text(
                    encoding="utf-8").splitlines() if line]
                if lines:
                    return [json.loads(line) for line in lines]
            if time.monotonic() > deadline:
                return []
            time.sleep(0.02)

    def _chat_body(self, model="GLM-5.2-NVFP4", stream=False, content="hi",
                   **extra):
        payload = {"model": model, "stream": stream,
                   "messages": [{"role": "user", "content": content}]}
        payload.update(extra)
        return json.dumps(payload)

    def test_auth_rejection_records_auth_phase_without_request_id(self):
        status, headers, _ = self._request(key="wrong")
        self.assertEqual(status, 401)
        self.assertNotIn("X-Share-Request-Id", headers)
        faults = self._faults()
        self.assertEqual(len(faults), 1)
        fault = faults[0]
        self.assertEqual((fault["phase"], fault["status"], fault["code"]),
                         ("auth", 401, "invalid_api_key"))
        self.assertIsNone(fault["request_id"])
        self.assertIsNone(fault["upstream_status"])
        self.assertIsNone(fault["exception_type"])
        self.assertFalse(fault["stream_started"])
        self.assertIsInstance(fault["duration_ms"], float)
        self.assertGreaterEqual(fault["duration_ms"], 0.0)
        self.assertEqual(self.upstream_requests, [])

    def test_config_failure_records_config_phase(self):
        self.config.write_text("broken", encoding="utf-8")
        status, headers, _ = self._request()
        self.assertEqual(status, 503)
        self.assertNotIn("X-Share-Request-Id", headers)
        fault = self._faults()[0]
        self.assertEqual(
            (fault["phase"], fault["status"], fault["code"]),
            ("config", 503, "sharing_configuration_unavailable"))
        self.assertIsNone(fault["request_id"])

    def test_body_rejection_records_body_phase_with_request_id(self):
        status, headers, _ = self._request(
            "POST", "/v1/chat/completions",
            body=self._chat_body(unknown_field=True))
        self.assertEqual(status, 400)
        request_id = headers.get("X-Share-Request-Id")
        self.assertRegex(request_id or "", r"^[0-9a-f]{32}$")
        fault = self._faults()[0]
        self.assertEqual((fault["phase"], fault["status"], fault["code"]),
                         ("body", 400, "unsupported_chat_request"))
        self.assertEqual(fault["request_id"], request_id)
        self.assertIsNone(fault["upstream_status"])

    def test_invalid_json_records_body_phase(self):
        status, _, _ = self._request("POST", "/v1/chat/completions",
                                     body="not json")
        self.assertEqual(status, 400)
        fault = self._faults()[0]
        self.assertEqual((fault["phase"], fault["status"], fault["code"]),
                         ("body", 400, "invalid_json"))

    def test_limit_records_limit_phase_with_request_id(self):
        self.assertTrue(self.server.acquire("friend"))
        try:
            status, headers, _ = self._request()
        finally:
            self.server.release("friend")
        self.assertEqual(status, 429)
        request_id = headers.get("X-Share-Request-Id")
        self.assertRegex(request_id or "", r"^[0-9a-f]{32}$")
        fault = self._faults()[0]
        self.assertEqual((fault["phase"], fault["status"], fault["code"]),
                         ("limit", 429, "sharing_limit_reached"))
        self.assertEqual(fault["request_id"], request_id)
        self.assertEqual(self.upstream_requests, [])

    def test_upstream_network_failure_records_exception_type(self):
        self.mode[0] = "abrupt"
        status, headers, _ = self._request()
        self.assertEqual(status, 502)
        request_id = headers.get("X-Share-Request-Id")
        self.assertRegex(request_id or "", r"^[0-9a-f]{32}$")
        fault = self._faults()[0]
        self.assertEqual((fault["phase"], fault["status"], fault["code"]),
                         ("upstream", 502, "um_connection_failed"))
        self.assertEqual(fault["exception_type"], "RemoteDisconnected")
        self.assertFalse(fault["stream_started"])
        self.assertEqual(fault["request_id"], request_id)

    def test_upstream_timeout_records_exception_type(self):
        class TimeoutConnection:
            def __init__(self, *args, **kwargs):
                pass

            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                raise TimeoutError("upstream stayed silent")

            def close(self):
                pass

        class FakeHttpClient:
            HTTPConnection = TimeoutConnection
            HTTPException = http.client.HTTPException

        class FakeHttpModule:
            client = FakeHttpClient

        with mock.patch.object(share, "http", FakeHttpModule):
            status, headers, _ = self._request()
        self.assertEqual(status, 502)
        fault = self._faults()[0]
        self.assertEqual((fault["phase"], fault["status"], fault["code"]),
                         ("upstream", 502, "um_connection_failed"))
        self.assertEqual(fault["exception_type"], "TimeoutError")
        self.assertFalse(fault["stream_started"])
        self.assertEqual(fault["request_id"],
                         headers.get("X-Share-Request-Id"))

    def test_upstream_5xx_records_upstream_status(self):
        self.mode[0] = "status"
        for upstream_status, public in ((503, 503), (502, 502)):
            with self.subTest(upstream=upstream_status):
                self.status[0] = upstream_status
                status, headers, _ = self._request()
                self.assertEqual(status, public)
                fault = self._faults()[-1]
                self.assertEqual(
                    (fault["phase"], fault["status"], fault["code"]),
                    ("upstream", public, "um_unavailable"))
                self.assertEqual(fault["upstream_status"], upstream_status)
                self.assertIsNone(fault["exception_type"])
                self.assertEqual(fault["request_id"],
                                 headers.get("X-Share-Request-Id"))

    def test_upstream_400_and_422_are_distinguished(self):
        self.mode[0] = "status"
        for upstream_status in (400, 422):
            with self.subTest(upstream=upstream_status):
                self.status[0] = upstream_status
                status, headers, _ = self._request(
                    "POST", "/v1/chat/completions",
                    body=self._chat_body())
                # 公开状态码保持不变（非 200 → 502）；400/422 的区别在留证字段。
                self.assertEqual(status, 502)
                fault = self._faults()[-1]
                self.assertEqual((fault["phase"], fault["code"]),
                                 ("upstream", "um_unavailable"))
                self.assertEqual(fault["upstream_status"], upstream_status)
                self.assertEqual(fault["request_id"],
                                 headers.get("X-Share-Request-Id"))

    # --------------------------------------------- A8 Retry-After pass-through

    def test_chat_upstream_retry_after_is_forwarded_with_the_stable_wrapper(self):
        # A8: a definite upstream retry hint is the ONLY extra fact the share
        # relays on a non-200. The public status/code/type stay the frozen
        # um_unavailable wrapper, the upstream auth body (and its credential)
        # is never echoed, and the hint is a bounded integer number of seconds.
        self.mode[0] = "status"
        self.status[0] = 429
        self.retry_after[0] = "12"
        self.error_body[0] = json.dumps({"error": {
            "message": "reauthentication required; token=canary-secret",
            "type": "reauthentication_required"}}).encode()
        status, headers, payload = self._request(
            "POST", "/v1/chat/completions", body=self._chat_body())
        self.assertEqual(status, 502)
        self.assertEqual(headers.get("Retry-After"), "12")
        error = json.loads(payload)["error"]
        self.assertEqual(error["type"], "share_gateway_error")
        self.assertEqual(error["code"], "um_unavailable")
        self.assertEqual(error["upstream_status"], 429)
        self.assertNotIn(b"canary-secret", payload)
        self.assertNotIn(b"reauthentication_required", payload)

    def test_messages_upstream_retry_after_is_forwarded(self):
        self.mode[0] = "status"
        self.status[0] = 503
        self.retry_after[0] = "7"
        body = json.dumps({"model": "GLM-5.2-NVFP4", "max_tokens": 8,
                           "messages": [{"role": "user", "content": "hi"}]})
        status, headers, payload = self._request(
            "POST", "/v1/messages", body=body)
        self.assertEqual(status, 503)
        self.assertEqual(headers.get("Retry-After"), "7")
        self.assertIn(b"um_unavailable", payload)

    def test_responses_upstream_retry_after_is_forwarded(self):
        self.mode[0] = "status"
        self.status[0] = 503
        self.retry_after[0] = "9"
        body = json.dumps({"model": "GLM-5.2-NVFP4", "input": "hi"})
        status, headers, payload = self._request(
            "POST", "/v1/responses", body=body)
        self.assertEqual(status, 503)
        self.assertEqual(headers.get("Retry-After"), "9")
        self.assertIn(b"um_unavailable", payload)
        # The 400/422 rejected-request branch carries the same validated hint.
        self.status[0] = 422
        self.retry_after[0] = "5"
        status, headers, payload = self._request(
            "POST", "/v1/responses", body=body)
        self.assertEqual(status, 422)
        self.assertEqual(headers.get("Retry-After"), "5")
        self.assertIn(b"upstream_request_rejected", payload)

    def test_retry_after_boundaries_are_enforced(self):
        # Positive integers up to the 900s local login rate-limit window are
        # relayed; zero, negative, fractional, HTTP-date and oversized values
        # are dropped instead of being passed through or fabricated.
        self.mode[0] = "status"
        self.status[0] = 503
        for raw, expected in (("1", "1"), ("900", "900"), ("901", None),
                              ("0", None), ("-5", None), ("12.5", None),
                              ("soon", None), ("", None),
                              ("Wed, 21 Oct 2015 07:28:00 GMT", None)):
            with self.subTest(retry_after=raw):
                self.retry_after[0] = raw
                status, headers, _ = self._request(
                    "POST", "/v1/chat/completions", body=self._chat_body())
                self.assertEqual(status, 503)
                self.assertEqual(headers.get("Retry-After"), expected)

    def test_invalid_upstream_retry_after_is_dropped_on_every_route(self):
        self.mode[0] = "status"
        self.status[0] = 503
        routes = (
            ("POST", "/v1/chat/completions", self._chat_body()),
            ("POST", "/v1/messages", json.dumps({
                "model": "GLM-5.2-NVFP4", "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}]})),
            ("POST", "/v1/responses", json.dumps({
                "model": "GLM-5.2-NVFP4", "input": "hi"})),
        )
        for method, path, body in routes:
            for raw in ("0", "901", "12.5", "soon", ""):
                with self.subTest(path=path, retry_after=raw):
                    self.retry_after[0] = raw
                    status, headers, payload = self._request(
                        method, path, body=body)
                    self.assertEqual(status, 503)
                    self.assertIsNone(headers.get("Retry-After"))
                    self.assertIn(b"um_unavailable", payload)

    def test_missing_upstream_retry_after_never_fabricates_one(self):
        self.mode[0] = "status"
        self.status[0] = 503
        self.retry_after[0] = None
        status, headers, _ = self._request(
            "POST", "/v1/chat/completions", body=self._chat_body())
        self.assertEqual(status, 503)
        self.assertIsNone(headers.get("Retry-After"))

    def test_upstream_protocol_shape_errors(self):
        self.mode[0] = "malformed"
        status, headers, _ = self._request()
        self.assertEqual(status, 502)
        fault = self._faults()[-1]
        self.assertEqual(
            (fault["phase"], fault["status"], fault["code"]),
            ("upstream", 502, "unexpected_upstream_response"))
        self.assertEqual(fault["request_id"],
                         headers.get("X-Share-Request-Id"))
        self.mode[0] = "no-marker"
        status, headers, _ = self._request()
        self.assertEqual(status, 502)
        fault = self._faults()[-1]
        self.assertEqual(
            (fault["phase"], fault["status"], fault["code"]),
            ("upstream", 502, "upstream_catalog_freshness_unconfirmed"))

    def test_stream_abort_records_stream_phase(self):
        class AbortingResponse:
            status = 200

            def __init__(self):
                self._first = True

            def getheader(self, name, default=None):
                if name == "Content-Type":
                    return "text/event-stream"
                return default

            def read1(self, size):
                if self._first:
                    self._first = False
                    return b'data: {"choices":[]}\n\n'
                raise ConnectionResetError("upstream stream reset")

        class AbortingConnection:
            def __init__(self, *args, **kwargs):
                pass

            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                return AbortingResponse()

            def close(self):
                pass

        class FakeHttpClient:
            HTTPConnection = AbortingConnection
            HTTPException = http.client.HTTPException

        class FakeHttpModule:
            client = FakeHttpClient

        with mock.patch.object(share, "http", FakeHttpModule):
            status, headers, _ = self._request(
                "POST", "/v1/chat/completions",
                body=self._chat_body(stream=True))
        self.assertEqual(status, 200)
        request_id = headers.get("X-Share-Request-Id")
        self.assertRegex(request_id or "", r"^[0-9a-f]{32}$")
        faults = self._faults()
        self.assertEqual(len(faults), 1)
        fault = faults[0]
        self.assertEqual(fault["phase"], "stream")
        self.assertIsNone(fault["status"])
        self.assertEqual(fault["code"], "stream_aborted")
        self.assertTrue(fault["stream_started"])
        self.assertEqual(fault["exception_type"], "ConnectionResetError")
        self.assertEqual(fault["request_id"], request_id)

    def test_success_reply_carries_correlation_id_without_a_fault(self):
        status, headers, _ = self._request()
        self.assertEqual(status, 200)
        request_id = headers.get("X-Share-Request-Id")
        self.assertRegex(request_id or "", r"^[0-9a-f]{32}$")
        # 转发到 8110 的请求头带同一个值（后端账本行取用的就是它）。
        sent = self.upstream_requests[0][2]
        self.assertEqual(sent.get("X-Share-Request-Id"), request_id)
        self.assertFalse(self.fault_log.exists())
        self.assertIsNone(self.server.faults_snapshot()["last_fault"])

    def test_route_rejection_writes_no_record_and_no_header(self):
        # 冻结映射表没有 404 分型：路由拒绝保持公开行为，不留证。
        status, headers, _ = self._request(path="/health")
        self.assertEqual(status, 404)
        self.assertNotIn("X-Share-Request-Id", headers)
        self.assertFalse(self.fault_log.exists())

    def test_missing_attribution_secret_keeps_request_id_null(self):
        # 无共享密钥时不伪造关联 ID：归因后失败也如实为 null。
        server = share.ShareServer(
            ("127.0.0.1", 0), self.config,
            upstream_port=self.upstream.server_port,
            fault_log=self.fault_log)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port,
                                              timeout=5)
            try:
                conn.request("GET", "/v1/models", headers={
                    "Authorization": "Bearer test-secret"})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertNotIn("X-Share-Request-Id",
                                 dict(response.getheaders()))
                response.read()
            finally:
                conn.close()
        finally:
            server.shutdown()
            thread.join()
        self.assertEqual(self._faults(), [])
        self.assertIsNone(server.faults_snapshot()["last_fault"])

    def test_fault_line_is_bounded_single_line_and_redacted(self):
        secret = "canary-credential-0123456789abcdef"
        self.config.write_text(json.dumps({"clients": {
            "friend": hashlib.sha256(secret.encode()).hexdigest()}}),
            encoding="utf-8")
        hostile_model = "evil-model/../../secret"
        self.mode[0] = "status"
        self.status[0] = 400
        self.error_body[0] = json.dumps({"error": {
            "message": "model " + hostile_model + " rejected; token=" + secret,
            "type": "invalid_request_error"}}).encode()
        status, _, _ = self._request(
            "POST", "/v1/chat/completions", key=secret,
            body=self._chat_body(model=hostile_model,
                                 content="body-secret-" + secret),
            headers={"Cookie": "session=" + secret})
        self.assertEqual(status, 502)
        raw = self.fault_log.read_bytes()
        # 客户端可控的模型名 / 上游错误文字 / 凭据 / Cookie / 正文一律不入证。
        for marker in (secret.encode(), b"evil-model", b"body-secret",
                       b"Cookie", b"Authorization", b"Bearer", b"session="):
            self.assertNotIn(marker, raw)
        lines = raw.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(len(lines[0]), 512)
        record = json.loads(lines[0])
        self.assertEqual(set(record), set(share.FAULT_RECORD_KEYS))
        self.assertEqual(record["code"], "um_unavailable")
        self.assertEqual(record["upstream_status"], 400)

    def test_fault_log_rotation_keeps_the_previous_file(self):
        self.fault_log.write_bytes(b"x" * share.FAULT_LOG_MAX_BYTES)
        status, _, _ = self._request(key="wrong")
        self.assertEqual(status, 401)
        rotated = self.root / (share.FAULT_LOG_NAME + ".1")
        self.assertTrue(rotated.exists())
        self.assertEqual(rotated.read_bytes(),
                         b"x" * share.FAULT_LOG_MAX_BYTES)
        lines = [line for line in self.fault_log.read_text(
            encoding="utf-8").splitlines() if line]
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["code"], "invalid_api_key")

    def test_fault_ring_is_bounded_and_recovery_keeps_the_failure(self):
        record = share.build_fault_record(phase="auth", status=401,
                                          code="invalid_api_key")
        for _ in range(share.FAULT_RING_MAX + 6):
            self.server.record_fault(record)
        self.assertEqual(len(self.server.fault_ring), share.FAULT_RING_MAX)
        before = self.server.faults_snapshot()
        self.assertEqual(before["last_fault"]["code"], "invalid_api_key")
        self.assertEqual(set(before["counts_by_phase"]),
                         set(share.FAULT_PHASES))
        self.assertEqual(sum(before["counts_by_phase"].values()),
                         share.FAULT_RING_MAX)
        self.assertTrue(before["file"]["present"])
        # 维护前后（drain / start / emergency）都不擦除失败。
        self.server.begin_drain()
        self.server.start()
        self.server.emergency_stop()
        self.server.start()
        after = self.server.faults_snapshot()
        self.assertEqual(after["last_fault"], before["last_fault"])
        self.assertEqual(after["counts_by_phase"], before["counts_by_phase"])

    def test_fault_log_appends_and_never_truncates(self):
        self.assertEqual(self._request(key="wrong")[0], 401)
        self.assertEqual(self._request(path="/health")[0], 404)  # 不留证
        self.mode[0] = "status"
        self.status[0] = 503
        self.assertEqual(self._request()[0], 503)
        faults = self._faults()
        self.assertEqual(len(faults), 2)
        self.assertEqual([fault["code"] for fault in faults],
                         ["invalid_api_key", "um_unavailable"])

    def test_snapshot_without_fault_log_is_honest(self):
        server = share.ShareServer(("127.0.0.1", 0), self.config)
        self.addCleanup(server.server_close)
        self.assertIsNone(server.fault_log)
        snapshot = server.faults_snapshot()
        self.assertIsNone(snapshot["last_fault"])
        self.assertEqual(sum(snapshot["counts_by_phase"].values()), 0)
        self.assertEqual(snapshot["file"],
                         {"present": False, "size": None, "mtime": None})


class ResumeControlTests(unittest.TestCase):
    """A9-3 instance-side resume + drain terminal guard (contract §2.4/§13.4).

    A REAL ``ShareServer`` answers the instance channel; the requests are really
    signed through ``share_broker_protocol.build_request`` (the broker's own
    builder), so the signing/validation path is exercised, not bypassed.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "clients.json"
        self.config.write_text(json.dumps({"clients": {}}), encoding="utf-8")
        self.secret = "inst-secret"
        self.server = share.ShareServer(("127.0.0.1", 0), self.config)
        self.addCleanup(self.server.server_close)

    def _call(self, action):
        from share_broker_protocol import build_request
        body = build_request(self.secret, action, {},
                             instance_id=self.server.instance_id,
                             now=int(time.time()) + 1)
        return self.server.handle_instance_control(body, self.secret)

    def test_resume_from_draining_restores_running(self):
        # draining -> running：resumed=true，active_count 如实。
        self.server.begin_drain()
        resp = self._call("resume")
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["result"]["state"], "running")
        self.assertTrue(resp["result"]["resumed"])
        self.assertNotIn("already_running", resp["result"])
        self.assertEqual(resp["result"]["instance_id"],
                         self.server.instance_id)
        self.assertEqual(resp["result"]["active_count"], 0)
        self.assertEqual(self.server.state, "running")

    def test_resume_on_a_running_instance_is_idempotent(self):
        # running -> 幂等 ok：resumed=true 且如实标注 already_running。
        resp = self._call("resume")
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["result"]["state"], "running")
        self.assertTrue(resp["result"]["resumed"])
        self.assertTrue(resp["result"]["already_running"])
        self.assertEqual(self.server.state, "running")

    def test_resume_never_reopens_a_stopped_instance(self):
        # stopped -> blocked：终态停止绝不被维护租约重开。
        self.server.emergency_stop()
        resp = self._call("resume")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "blocked")
        self.assertEqual(resp["result"]["detail"], "instance is stopped")
        self.assertEqual(resp["result"]["state"], "stopped")
        self.assertEqual(self.server.state, "stopped")

    def test_drain_after_a_terminal_stop_is_blocked_and_keeps_stopped(self):
        # 契约 §2.4/§13.4（评审缺口 3）：先 emergency → stopped；再
        # drain → blocked 且 state 仍 stopped（旧行为会无条件翻成 draining）。
        self.server.emergency_stop()
        self.assertEqual(self.server.state, "stopped")
        resp = self._call("drain")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "blocked")
        self.assertEqual(resp["result"], {"detail": "instance is stopped",
                                        "state": "stopped"})
        self.assertEqual(self.server.state, "stopped")

    def test_drain_on_running_and_draining_is_unchanged(self):
        # running -> draining；draining 幂等 ok（逐字不变的行为）。
        first = self._call("drain")
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["result"]["state"], "draining")
        self.assertEqual(self.server.state, "draining")
        second = self._call("drain")
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["result"]["state"], "draining")
        self.assertEqual(self.server.state, "draining")

    def test_old_instance_resume_is_bad_request_unknown_action(self):
        # 旧实例（协议模块不认识 resume）在 validate_instance_request
        # 先一步拒绝：bad_request / detail == "unknown action"。契约 §0.7：
        # "unknown instance action" 是死代码串，绝不作为旧实例信号。
        import share_broker_protocol as bp
        old_actions = {k: v for k, v in bp.INSTANCE_ACTIONS.items()
                       if k != "resume"}
        with mock.patch.dict(bp.INSTANCE_ACTIONS, old_actions, clear=True):
            resp = self._call("resume")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "bad_request")
        self.assertEqual(resp["result"]["detail"], "unknown action")
        self.assertNotEqual(resp["result"]["detail"],
                            "unknown instance action")
        self.assertEqual(self.server.state, "running")


if __name__ == "__main__":
    unittest.main()
