# -*- coding: utf-8 -*-
"""Endpoint smoke tests for chat2api's compatibility & readiness surface.

Covers the 2026-09-18 review round: /api/config probe with JWT fallback when
API keys are disabled, side-effect-free /ready (200 valid / 503 rejected,
never an interactive login), /v1/embeddings and Anthropic /v1/messages
compatibility proxying that borrows the STORED upstream bearer only (the
client's local key must never reach UM), the /api/v1/* aliases behind the
same local-key gate, and the refuse-to-start rule for non-loopback binds
without CHAT2API_LOCAL_API_KEY.

Stub upstream mirrors the Open WebUI surface per path (/api/models,
/api/config, /api/v1/embeddings, /api/v1/messages); the recorded
Authorization on the stub is compared against the client's local key to pin
the credential-isolation contract.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
CHAT2API_PATH = Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py"
sys.path.insert(0, str(CHAT2API_PATH.parent))
import _chat2api_http_harness as harness  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "chat2api_endpoints", CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)

LOCAL_KEY = "local-secret-3f9"
STUB_TOKEN = "stub-token"
UPSTREAM_BEARER = "Bearer stub-token"


# The historical /api/v1/messages stream: message_start, one text delta,
# message_stop. A test that needs another sequence sets
# ``StubUpstreamHandler.messages_events`` to a list of complete wire frames
# (each ending in a blank line) and setUp resets the attribute so no case leaks
# its sequence into the next.
DEFAULT_MESSAGES_EVENTS = [
    b'event: message_start\ndata: {"type":"message_start"}\n\n',
    b'event: content_block_delta\ndata: {"type":"content_block_delta",'
    b'"delta":{"text":"hi"}}\n\n',
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
]

ANTHROPIC_MESSAGE = {
    "id": "msg_1", "type": "message", "role": "assistant",
    "model": "chat-model",
    "content": [{"type": "text", "text": "hi"}],
    "stop_reason": "end_turn", "stop_sequence": None,
    "usage": {"input_tokens": 3, "output_tokens": 2},
}

ANTHROPIC_RESPONSES_EVENTS = [
    b'event: message_start\ndata: {"type":"message_start","message":'
    b'{"usage":{"input_tokens":3,"output_tokens":0}}}\n\n',
    b'event: content_block_start\ndata: {"type":"content_block_start",'
    b'"index":0,"content_block":{"type":"text","text":""}}\n\n',
    b'event: content_block_delta\ndata: {"type":"content_block_delta",'
    b'"index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n',
    b'event: content_block_stop\ndata: {"type":"content_block_stop",'
    b'"index":0}\n\n',
    b'event: message_delta\ndata: {"type":"message_delta",'
    b'"delta":{"stop_reason":"end_turn"},'
    b'"usage":{"output_tokens":2}}\n\n',
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
]

DEFAULT_CHAT_JSON = {
    "id": "chatcmpl-1", "object": "chat.completion", "model": "chat-model",
    "choices": [{"index": 0, "message": {"role": "assistant",
                                         "content": "hi"},
                  "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5,
              "prompt_tokens_details": {"cached_tokens": 1}},
}

DEFAULT_CHAT_EVENTS = [
    b'data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n',
    b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n',
    b'data: [DONE]\n\n',
]


def sse_event(event, data):
    """One complete Anthropic wire frame: event/data lines + blank line."""
    return (f"event: {event}\ndata: {data}\n\n").encode("utf-8")


class StubUpstreamHandler(BaseHTTPRequestHandler):
    """Fake Open WebUI surface; records the Authorization it received."""

    protocol_version = "HTTP/1.1"
    seen_authorizations = []
    messages_events = None
    messages_bodies = []
    chat_bodies = []
    chat_events = None

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

    def do_GET(self):
        path = self.path.split("?")[0]
        StubUpstreamHandler.seen_authorizations.append(
            self.headers.get("Authorization", ""))
        if path == "/api/models":
            if self.headers.get("Authorization") == UPSTREAM_BEARER:
                self._send_json(200, {"data": [{"id": "GLM-5.3-Flash"}]})
            else:
                self._send_json(401, {"detail": "unauthorized"})
        elif path == "/api/config":
            self._send_json(200, {"version": "0.9.2", "enable_api_keys": True,
                                  "oauth": {"providers": {}}})
        elif path == "/api/v1/auths/api_key":
            self._send_json(200, {"api_key": "sk-upstream"})
        else:
            self._send_json(404, {"detail": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        StubUpstreamHandler.seen_authorizations.append(
            self.headers.get("Authorization", ""))
        if path == "/api/v1/embeddings":
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            self._send_json(200, {"object": "list",
                                  "echo_model": json.loads(body).get("model")})
        elif path == "/api/chat/completions":
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length))
            StubUpstreamHandler.chat_bodies.append(body)
            if not body.get("stream"):
                self._send_json(200, DEFAULT_CHAT_JSON)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.close_connection = True
            events = StubUpstreamHandler.chat_events
            if events is None:
                events = DEFAULT_CHAT_EVENTS
            for ev in events:
                self.wfile.write(f"{len(ev):X}\r\n".encode() + ev + b"\r\n")
                self.wfile.flush()
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif path == "/api/v1/messages":
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length))
            StubUpstreamHandler.messages_bodies.append(body)
            if not body.get("stream"):
                self._send_json(200, ANTHROPIC_MESSAGE)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.close_connection = True
            events = StubUpstreamHandler.messages_events
            if events is None:
                events = DEFAULT_MESSAGES_EVENTS
            for ev in events:
                self.wfile.write(f"{len(ev):X}\r\n".encode() + ev + b"\r\n")
                self.wfile.flush()
            # Terminate the chunked body: a case whose upstream stops WITHOUT
            # a message_stop must reach the proxy as a clean EOF, not as an
            # unterminated chunk (which surfaces as ChunkedEncodingError).
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            try:
                time.sleep(0.3)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self._send_json(404, {"detail": "not found"})


class TestChat2ApiEndpoints(unittest.TestCase):
    def setUp(self):
        # B8-0 isolation lifecycle (see _chat2api_http_harness): the ledger
        # redirect is registered FIRST so it is released LAST - after every
        # handler drained and every server closed. The pre-fix LIFO bug
        # released the patch while a post-response handler was still writing
        # record_usage, which is how test rows reached the PRODUCTION
        # data/usage-chat2api.jsonl on 2026-10-02.
        self._usage_dir, self.usage_log = harness.install_usage_isolation(
            self, chat2api, ledger_name="usage-chat2api.jsonl")
        harness.assert_stub_credentials(
            chat2api, local_key=LOCAL_KEY, upstream_token=STUB_TOKEN,
            stub_token_literal=STUB_TOKEN)

        stub = harness.TrackedHTTPServer(("127.0.0.1", 0),
                                         StubUpstreamHandler)
        self.stub = stub
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        harness.install_tracked_server(self, stub, name="upstream-stub",
                                         require_drained=False)
        StubUpstreamHandler.seen_authorizations = []
        # Reset the configurable Messages sequence: a case that overrides it
        # must never leak its frames into the next one.
        StubUpstreamHandler.messages_events = None
        StubUpstreamHandler.messages_bodies = []
        StubUpstreamHandler.chat_bodies = []
        StubUpstreamHandler.chat_events = None

        args = SimpleNamespace(
            base_url=f"http://127.0.0.1:{stub.server_port}",
            token=STUB_TOKEN,
            profile="profile",
            login_timeout=1,
            use_api_key=True,
            no_browser=True,
        )
        proxy = chat2api.Proxy({"token": STUB_TOKEN}, args)
        self._proxy = proxy
        server = harness.TrackedHTTPServer(("127.0.0.1", 0),
                                           chat2api.Handler)
        server.proxy = proxy
        server.local_api_key = LOCAL_KEY
        server.require_local_api_key = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        # Registered AFTER the redirect: this teardown (shutdown → bounded
        # drain of in-flight requests → close) runs BEFORE the USAGE_LOG
        # patch stop, so the post-response record_usage tail of the LAST
        # request lands in the temp ledger, never the production one.
        harness.install_tracked_server(self, server, name="chat2api-gateway")
        self.base = f"http://127.0.0.1:{server.server_port}"
        self.auth = {"Authorization": f"Bearer {LOCAL_KEY}"}

    # ---------- /ready ----------

    def test_ready_200_when_credential_valid(self):
        response = requests.get(f"{self.base}/ready", timeout=15)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["authentication"], "valid")

    def test_ready_503_when_credential_rejected(self):
        # Point the proxy's stored token at a value the stub rejects: the
        # readiness probe must classify rejected and answer 503 WITHOUT any
        # interactive login (stub records only the read-only /api/models).
        saved = self._proxy.creds.get("token")
        try:
            self._proxy.creds["token"] = "stale-token"
            response = requests.get(f"{self.base}/ready", timeout=15)
        finally:
            self._proxy.creds["token"] = saved
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["status"], "not_ready")
        self.assertEqual(body["authentication"], "rejected")

    # ---------- compat endpoints behind the local-key gate ----------

    def test_embeddings_passthrough_and_credential_isolation(self):
        StubUpstreamHandler.seen_authorizations = []
        response = requests.post(
            f"{self.base}/v1/embeddings",
            json={"model": "text-embed", "input": "hi"},
            headers=self.auth, timeout=10)
        self.assertEqual(response.status_code, 200)
        # The stored upstream bearer was used upstream; the client's local
        # key must never leave this box.
        self.assertTrue(StubUpstreamHandler.seen_authorizations)
        for saw in StubUpstreamHandler.seen_authorizations:
            self.assertNotEqual(saw, f"Bearer {LOCAL_KEY}")
            self.assertEqual(saw, UPSTREAM_BEARER)

    def test_messages_stream_passthrough(self):
        with requests.post(
                f"{self.base}/v1/messages",
                json={"model": "claude-x", "max_tokens": 16, "stream": True,
                      "messages": [{"role": "user", "content": "hi"}]},
                headers={**self.auth, "Anthropic-Version": "2023-06-01"},
                stream=True, timeout=10) as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream",
                          response.headers.get("Content-Type", ""))
            body = b"".join(response.iter_lines(chunk_size=1))
        self.assertIn(b"message_start", body)
        self.assertIn(b"content_block_delta", body)
        self.assertIn(b"message_stop", body)
        # Regression guard for the wall-clock start: record_usage derives
        # duration_ms from time.time(), so handing it the monotonic start
        # writes ~1.8e12 ms. The row lands just after the last byte.
        deadline = time.time() + 2.0
        rows = []
        while time.time() < deadline:
            try:
                rows = [json.loads(line) for line in
                        Path(chat2api.USAGE_LOG).read_text(
                            encoding="utf-8").splitlines() if line.strip()]
            except FileNotFoundError:
                rows = []
            if rows:
                break
            time.sleep(0.02)
        self.assertEqual([row["model"] for row in rows], ["claude-x"])
        self.assertEqual(rows[0]["status"], "ok")
        self.assertGreaterEqual(rows[0]["duration_ms"], 0)
        self.assertLess(rows[0]["duration_ms"], 60_000)

    # ---------- OpenAI Responses ----------

    def test_responses_non_stream_forwards_tools_and_records_usage(self):
        body = {"model": "responses-model",
                "input": [{"role": "user",
                           "content": [{"type": "input_text", "text": "hi"}]}],
                "tools": [{"type": "function", "name": "get_time",
                           "parameters": {"type": "object"}}],
                "reasoning": {"effort": "low"}}
        response = requests.post(f"{self.base}/v1/responses", json=body,
                                 headers=self.auth, timeout=10)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["output"][0]["content"][0]["text"], "hi")
        forwarded = StubUpstreamHandler.chat_bodies[-1]
        self.assertEqual(forwarded["tools"], [{
            "type": "function",
            "function": {"name": "get_time",
                          "parameters": {"type": "object"}}}])
        self.assertEqual(forwarded["reasoning_effort"], "low")
        self.assertEqual(forwarded["messages"][0]["content"], "hi")
        row = self._ledger_row()
        self.assertEqual(row["protocol"], "responses")
        self.assertEqual(row["model"], "responses-model")
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["input_tokens"], 3)
        self.assertEqual(row["output_tokens"], 2)
        self.assertEqual(row["cached_input_tokens"], 1)

    def test_responses_stream_forwards_tool_calls_and_records_usage(self):
        StubUpstreamHandler.chat_events = [
            b'data: {"choices":[{"index":0,"delta":{"tool_calls":['
            b'{"index":0,"id":"c1","function":{"name":"get_time",'
            b'"arguments":"{}"}}]}}]}\n\n',
            b'data: {"choices":[{"index":0,"delta":{},'
            b'"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":3,'
            b'"completion_tokens":2,"total_tokens":5}}\n\n',
            b'data: [DONE]\n\n',
        ]
        body = {"model": "responses-model", "input": "hi", "stream": True,
                "tools": [{"type": "function", "name": "get_time",
                           "parameters": {"type": "object"}}]}
        with requests.post(f"{self.base}/v1/responses", json=body,
                            headers=self.auth, stream=True,
                            timeout=10) as response:
            self.assertEqual(response.status_code, 200)
            raw = response.content
        self.assertIn(b"event: response.output_item.added", raw)
        self.assertIn(b"event: response.output_item.done", raw)
        self.assertIn(b'"name": "get_time"', raw)
        self.assertIn(b"event: response.completed", raw)
        row = self._ledger_row()
        self.assertEqual(row["protocol"], "responses")
        self.assertEqual(row["status"], "ok")

    def test_responses_stream_without_done_records_the_failure(self):
        StubUpstreamHandler.chat_events = [
            b'data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n',
        ]
        with requests.post(
                f"{self.base}/v1/responses",
                json={"model": "responses-model", "input": "hi",
                      "stream": True},
                headers=self.auth, stream=True, timeout=10) as response:
            self.assertEqual(response.status_code, 200)
            raw = response.content
        self.assertIn(b"event: response.failed", raw)
        self.assertNotIn(b"event: response.completed", raw)
        row = self._ledger_row()
        self.assertEqual(row["status"], "upstream_eof_without_done")

    def test_responses_image_tool_result_uses_anthropic_messages(self):
        body = {"model": "responses-model", "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "function_call", "call_id": "c1", "name": "shot",
             "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1",
             "output": [{"type": "input_text", "text": "shot"},
                        {"type": "input_image",
                         "image_url": "data:image/png;base64,AAAA"}]}],
            "tools": [{"type": "function", "name": "shot",
                       "parameters": {"type": "object"}}],
            "max_output_tokens": 64}
        response = requests.post(f"{self.base}/v1/responses", json=body,
                                 headers=self.auth, timeout=10)
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["output"][0]["content"][0]["text"], "hi")
        forwarded = StubUpstreamHandler.messages_bodies[-1]
        self.assertEqual(forwarded["max_tokens"], 64)
        tool_result = forwarded["messages"][2]["content"][0]
        self.assertEqual(tool_result["type"], "tool_result")
        self.assertEqual(tool_result["content"][1]["type"], "image")
        self.assertEqual(tool_result["content"][1]["source"]["media_type"],
                         "image/png")
        self.assertEqual(tool_result["content"][1]["source"]["data"], "AAAA")
        row = self._ledger_row()
        self.assertEqual(row["protocol"], "responses")
        self.assertEqual(row["status"], "ok")

    def test_responses_image_tool_result_stream_uses_anthropic_messages(self):
        StubUpstreamHandler.messages_events = ANTHROPIC_RESPONSES_EVENTS
        body = {"model": "responses-model", "stream": True, "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "function_call", "call_id": "c1", "name": "shot",
             "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1",
             "output": [{"type": "input_text", "text": "shot"},
                        {"type": "input_image",
                         "image_url": "data:image/png;base64,AAAA"}]}],
            "max_output_tokens": 64}
        with requests.post(f"{self.base}/v1/responses", json=body,
                            headers=self.auth, stream=True,
                            timeout=10) as response:
            self.assertEqual(response.status_code, 200)
            raw = response.content
        self.assertIn(b"event: response.output_text.delta", raw)
        self.assertIn(b"event: response.completed", raw)
        forwarded = StubUpstreamHandler.messages_bodies[-1]
        tool_result = forwarded["messages"][2]["content"][0]
        self.assertEqual(tool_result["content"][1]["type"], "image")
        self.assertEqual(tool_result["content"][1]["source"]["data"], "AAAA")
        row = self._ledger_row()
        self.assertEqual(row["protocol"], "responses")
        self.assertEqual(row["status"], "ok")

    # ---------- Anthropic Messages stream termination ----------

    def _stream_messages(self, events):
        """POST a streaming Messages request against a fixed event list.

        Returns the client's exact bytes. The caller reads the ledger row
        separately: record_usage lands just after the last downstream byte.
        """
        StubUpstreamHandler.messages_events = events
        with requests.post(
                f"{self.base}/v1/messages",
                json={"model": "claude-x", "max_tokens": 16, "stream": True,
                      "messages": [{"role": "user", "content": "hi"}]},
                headers={**self.auth, "Anthropic-Version": "2023-06-01"},
                stream=True, timeout=10) as response:
            self.assertEqual(response.status_code, 200)
            return response.content

    def _ledger_row(self, timeout=2.0):
        """The single Messages ledger row written for the last request."""
        deadline = time.time() + timeout
        rows = []
        while time.time() < deadline:
            try:
                rows = [json.loads(line) for line in
                        Path(chat2api.USAGE_LOG).read_text(
                            encoding="utf-8").splitlines() if line.strip()]
            except FileNotFoundError:
                rows = []
            if rows:
                break
            time.sleep(0.02)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_messages_stream_full_official_sequence_forwards_everything(self):
        # The official Anthropic sequence: message_start (input usage),
        # content_block_delta, message_delta (stop_reason + output usage),
        # message_stop. Every frame must reach the client byte for byte, and
        # the ledger must record the complete upstream usage.
        events = [
            sse_event("message_start", json.dumps({
                "type": "message_start", "message": {
                    "id": "msg_1", "type": "message", "role": "assistant",
                    "model": "claude-x", "content": [], "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 7, "output_tokens": 0}}})),
            sse_event("content_block_delta", json.dumps({
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": "hello"}})),
            sse_event("message_delta", json.dumps({
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 16}})),
            sse_event("message_stop", json.dumps({"type": "message_stop"})),
        ]
        body = self._stream_messages(events)
        self.assertEqual(body, b"".join(events))
        row = self._ledger_row()
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["output_tokens"], 16)

    def test_messages_stream_usage_after_stop_reason_is_merged(self):
        # Maintainer repro (tmp/backend-acceptance-20261005/
        # planner-messages-terminal-repro.json): a message_delta carrying
        # stop_reason is NOT a termination signal - the later usage update
        # (output_tokens 19) and the final message_stop must still be read,
        # forwarded byte for byte, and merged (max wins) into the ledger.
        events = [
            sse_event("message_start", json.dumps({
                "type": "message_start", "message": {
                    "id": "msg_1", "type": "message", "role": "assistant",
                    "model": "claude-x", "content": [], "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 7, "output_tokens": 0}}})),
            sse_event("content_block_delta", json.dumps({
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": "hello"}})),
            sse_event("message_delta", json.dumps({
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 3}})),
            sse_event("message_delta", json.dumps({
                "type": "message_delta",
                "delta": {"stop_reason": None, "stop_sequence": None},
                "usage": {"output_tokens": 19}})),
            sse_event("message_stop", json.dumps({"type": "message_stop"})),
        ]
        body = self._stream_messages(events)
        self.assertEqual(body, b"".join(events))
        self.assertIn(events[3], body)   # the later usage update
        self.assertIn(events[4], body)   # the final message_stop
        row = self._ledger_row()
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["output_tokens"], 19)

    def test_messages_stream_eof_without_message_stop_is_not_ok(self):
        # The upstream dies after a stop_reason with no message_stop: the
        # client sees exactly the upstream's bytes (no synthesized
        # message_stop), and the ledger must not claim "ok" for a truncated
        # completion.
        events = [
            sse_event("message_start", json.dumps({
                "type": "message_start", "message": {
                    "id": "msg_1", "type": "message", "role": "assistant",
                    "model": "claude-x", "content": [], "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 7, "output_tokens": 0}}})),
            sse_event("content_block_delta", json.dumps({
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": "hello"}})),
            sse_event("message_delta", json.dumps({
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 3}})),
        ]
        body = self._stream_messages(events)
        self.assertEqual(body, b"".join(events))
        self.assertNotIn(b"message_stop", body)
        row = self._ledger_row()
        self.assertEqual(row["status"], "upstream_eof_without_event")

    def test_messages_stream_error_event_terminates_immediately(self):
        # An upstream error event is the upstream's own failure: it is relayed
        # to the client and terminates the read; the ledger records the error,
        # never "ok".
        events = [
            sse_event("message_start", json.dumps({
                "type": "message_start", "message": {
                    "id": "msg_1", "type": "message", "role": "assistant",
                    "model": "claude-x", "content": [], "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 7, "output_tokens": 0}}})),
            sse_event("content_block_delta", json.dumps({
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": "hello"}})),
            sse_event("error", json.dumps({
                "type": "error",
                "error": {"type": "overloaded_error", "message": "overloaded"}})),
        ]
        body = self._stream_messages(events)
        self.assertEqual(body, b"".join(events))
        self.assertIn(b"event: error", body)
        row = self._ledger_row()
        self.assertEqual(row["status"], "upstream_stream_error")

    def test_missing_local_key_returns_401(self):
        response = requests.post(
            f"{self.base}/v1/embeddings", json={"model": "m", "input": "x"},
            timeout=10)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["type"],
                         "local_auth_error")

    # ---------- credential fallback helpers ----------

    def test_fetch_api_key_skipped_when_config_disables_keys(self):
        # The gate reads config["features"]["enable_api_keys"] (nested); a
        # nested False must short-circuit BEFORE the api_key endpoint, while
        # a missing field stays None (undetermined → probe the endpoint).
        disabled = {"version": "0.9.2",
                    "features": {"enable_api_keys": False}}
        self.assertIs(chat2api._config_api_keys_enabled(disabled), False)
        self.assertIsNone(chat2api._config_api_keys_enabled({"version": "0.9.2"}))
        self.assertIsNone(chat2api.fetch_api_key(
            f"http://127.0.0.1:{self.stub.server_port}", "token-jwt",
            upstream_config=disabled))

    def test_fetch_upstream_config_probe(self):
        config = chat2api.fetch_upstream_config(
            f"http://127.0.0.1:{self.stub.server_port}")
        self.assertIsInstance(config, dict)
        self.assertEqual(config["version"], "0.9.2")

    # ---------- refuse-to-start guard ----------

    def test_non_loopback_bind_without_key_refuses_startup(self):
        # The real main() writes its startup record before this refusal, so
        # every state file - the service log above all - goes to a temp dir:
        # left at the default, each run wrote a "startup ... port=8000" line
        # into the production chat2api.log (2026-09-30, round 19 S-9).
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        env = dict(os.environ)
        env["CHAT2API_CONTROL_TOKEN_FILE"] = os.path.join(tmp.name, "unused-token")
        env["CHAT2API_SERVICE_LOG"] = os.path.join(tmp.name, "service.log")
        env["CHAT2API_TOKEN_FILE"] = os.path.join(tmp.name, "token.json")
        env["CHAT2API_USAGE_LOG"] = os.path.join(tmp.name, "usage.jsonl")
        env.pop("CHAT2API_LOCAL_API_KEY", None)
        proc = subprocess.run(
            [sys.executable, "chat2api.py", "--host", "0.0.0.0",
             "--no-browser", "--token", "x"],
            capture_output=True, timeout=30, env=env,
            cwd=str(CHAT2API_PATH.parent))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"requires CHAT2API_LOCAL_API_KEY", proc.stderr)


class RequestBodyContractTests(unittest.TestCase):
    def test_deeply_nested_json_is_answered_as_invalid_json(self):
        handler = object.__new__(chat2api.Handler)
        payload = b"[" * 50000 + b"]" * 50000
        handler.headers = {"Content-Length": str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        handler.server = SimpleNamespace(share_gate=None)
        errors = []

        def record_error(status, message, **kwargs):
            errors.append((status, message, kwargs))

        handler._error = record_error
        self.assertIsNone(handler._read_body())
        self.assertEqual(errors[0][0], 400)
        self.assertEqual(errors[0][2].get("error_type"), "invalid_json")


if __name__ == "__main__":
    unittest.main()
