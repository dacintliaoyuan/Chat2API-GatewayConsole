# -*- coding: utf-8 -*-
"""N1: non-streaming Chat requests served by an aggregated SSE upstream.

The switch ``NONSTREAM_UPSTREAM_AGGREGATE`` is OFF by default (native
non-stream transport). When it is ON, the client still sends ``stream=false``
and receives ONE complete JSON body, while chat2api sends ``stream=true`` plus
``stream_options`` to the fake UM, reads the SSE through ``_UpstreamStream`` and
aggregates it locally.

Every HTTP test goes through the real ``Handler`` -> loopback fake UM boundary
(``_chat2api_http_harness`` provides the tracked servers, the private usage
ledger and the production-ledger sentinels). The fake UM only decides the wire
bytes; it never sees the client's request mode except through the forwarded
body, which is asserted explicitly.

The pure state machine is tested without any server so fragmented UTF-8, CRLF,
multi-line data, tool-call fragments, reasoning separation and the terminal
rules are pinned at the seam itself.
"""

import gc
import http.client
import importlib.util
import json
import os
import queue
import socket
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
import weakref
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
CHAT2API_PATH = Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(CHAT2API_PATH.parent))
import _chat2api_http_harness as harness  # noqa: E402
import nonstream_aggregate  # noqa: E402
import share_entry  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "chat2api_nonstream_aggregate", CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)


def _write_private(path, text):
    """Fixture secrets must pass the POSIX 0600/current-uid gate."""
    from share_secret_protection import write_private_secret_path
    write_private_secret_path(Path(path), str(text).encode("utf-8"))

STUB_TOKEN = "stub-token"
LOCAL_KEY = "local-secret-n1"

# R13: the trusted aggregate scope is a STARTUP environment value; tests build
# it with the real parser and sign attribution markers with the real share
# gateway builder, so a changed wire format or parser fails here instead of
# silently leaving every request native.
ATTRIBUTION_SECRET = "s" * 48
ATTRIBUTION_KEY_ID = "synthetic-selected-key"
UNSELECTED_KEY_ID = "unselected-key"
# The exact canonical JSON console_share_production._backend_aggregate_scope_json
# hands to the dedicated 8110 child: an EXPLICITLY empty (disabled) scope. Kept
# as a literal so this suite stays independent of the console module; the console
# suite pins the builder's output to this same string.
PRODUCTION_EMPTY_SCOPE_JSON = '{"key_ids":[],"models":[],"protocols":[]}'


def install_attribution_key(test):
    """Give this suite a private, non-production attribution key file."""
    tmp = tempfile.TemporaryDirectory(prefix="chat2api-aggregate-attribution-")
    test.addCleanup(tmp.cleanup)
    key_file = Path(tmp.name) / "share-attribution.key"
    _write_private(key_file, ATTRIBUTION_SECRET)
    patch = mock.patch.object(chat2api, "SHARE_ATTRIBUTION_KEY_FILE",
                              str(key_file))
    patch.start()
    test.addCleanup(patch.stop)
    chat2api._attribution_key_cache["stat"] = None
    test.addCleanup(chat2api._attribution_seen.clear)


def marker_headers(key_id=ATTRIBUTION_KEY_ID):
    headers, _ = share_entry.build_attribution(ATTRIBUTION_SECRET, key_id)
    return headers


def aggregate_scope(*, models, protocols, key_ids=(ATTRIBUTION_KEY_ID,)):
    """Build the real parsed scope shape from a JSON startup value."""
    return chat2api.parse_nonstream_aggregate_scope(json.dumps({
        "key_ids": list(key_ids),
        "models": list(models),
        "protocols": list(protocols),
    }))


def tampered_marker_headers(key_id=ATTRIBUTION_KEY_ID):
    headers = dict(marker_headers(key_id))
    headers[share_entry.X_ATTRIBUTION_SIGNATURE] = "0" * 64
    return headers


def raw_http_request(server, path, body, headers=None):
    """One raw HTTP/1.1 request; used where requests' keep-alive would
    hide the client-cancel boundary."""
    raw = body.encode("utf-8")
    lines = [f"POST {path} HTTP/1.1",
             f"Host: 127.0.0.1:{server.server_port}",
             "Content-Type: application/json"]
    for name, value in (headers or {}).items():
        lines.append(f"{name}: {value}")
    lines.append(f"Content-Length: {len(raw)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + raw

DEFAULT_CHAT_JSON = {
    "id": "chatcmpl-native", "object": "chat.completion",
    "created": 1234567890, "model": "native-model",
    "choices": [{"index": 0, "message": {"role": "assistant",
                                         "content": "hi"},
                  "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}


def _sse(payload):
    return f"data: {payload}\n\n".encode("utf-8")


def _chunk(content=None, finish_reason=None, usage=None, role=None,
           tool_calls=None, reasoning=None, reasoning_content=None,
           function_call=None):
    delta = {}
    if role is not None:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning"] = reasoning
    if reasoning_content is not None:
        delta["reasoning_content"] = reasoning_content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    if function_call is not None:
        delta["function_call"] = function_call
    chunk = {"choices": [{"index": 0, "delta": delta,
                          "finish_reason": finish_reason}]}
    if usage is not None:
        chunk["usage"] = usage
    return json.dumps(chunk, ensure_ascii=False)


def _ok_frames():
    """One complete id/created/model SSE completion."""
    first = {
        "id": "chatcmpl-n1", "created": 1234567890, "model": "chat-model",
        "choices": [{"index": 0, "delta": {"role": "assistant",
                                          "content": "hel"},
                      "finish_reason": None}],
    }
    second = {
        "choices": [{"index": 0, "delta": {"content": "lo"},
                      "finish_reason": None}],
    }
    final = {
        "choices": [{"index": 0, "delta": {},
                      "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                   "total_tokens": 5},
    }
    return [
        _sse(json.dumps(first, ensure_ascii=False)),
        _sse(json.dumps(second, ensure_ascii=False)),
        _sse(json.dumps(final, ensure_ascii=False)),
        _sse("[DONE]"),
    ]


class StubUpstreamHandler(BaseHTTPRequestHandler):
    """Fake UM chat surface; only the wire bytes are faked."""

    protocol_version = "HTTP/1.1"
    chat_bodies = []
    posts = 0
    slow_done = threading.Event()

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

    def _send_chunk(self, data):
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii")
                         + data + b"\r\n")
        self.wfile.flush()

    def _send_headers(self, chunked=True):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _write_events(self, scenario):
        if scenario == "ok-sse":
            for frame in _ok_frames():
                self._send_chunk(frame)
        elif scenario == "explicit-error":
            self._send_chunk(_sse(_chunk(content="partial")))
            self._send_chunk(_sse(json.dumps({
                "error": {"message": "boom"}})))
            self._send_chunk(_sse("[DONE]"))
        elif scenario == "bad-frame":
            self._send_chunk(_sse(_chunk(content="partial")))
            self._send_chunk(_sse("{not json"))
            self._send_chunk(_sse("[DONE]"))
        elif scenario == "usage-missing":
            self._send_chunk(_sse(_chunk(content="hi")))
            self._send_chunk(_sse(_chunk(finish_reason="stop")))
            self._send_chunk(_sse("[DONE]"))
        elif scenario == "tool-args":
            first = {
                "choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "id": "c1", "type": "function",
                     "function": {"name": "get_time",
                                  "arguments": '{"zone":'}},
                    {"index": 1, "id": "c2", "type": "function",
                     "function": {"name": "echo",
                                  "arguments": '{"text":'}},
                ]}, "finish_reason": None}],
            }
            second = {
                "choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": '"UTC"}'}},
                    {"index": 1, "function": {"arguments": '"hi"}'}},
                ]}, "finish_reason": None}],
            }
            final = {
                "choices": [{"index": 0, "delta": {},
                              "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2,
                           "total_tokens": 6},
            }
            for frame in (first, second, final):
                self._send_chunk(_sse(json.dumps(frame, ensure_ascii=False)))
            self._send_chunk(_sse("[DONE]"))
        elif scenario == "reasoning":
            self._send_chunk(_sse(_chunk(reasoning="think-a")))
            self._send_chunk(_sse(_chunk(reasoning_content="think-b")))
            self._send_chunk(_sse(_chunk(content="answer")))
            self._send_chunk(_sse(_chunk(
                finish_reason="stop",
                usage={"prompt_tokens": 3, "completion_tokens": 2,
                       "total_tokens": 5})))
            self._send_chunk(_sse("[DONE]"))
        elif scenario == "large-output":
            piece = "x" * 4096
            for _ in range(256):
                self._send_chunk(_sse(_chunk(content=piece)))
            self._send_chunk(_sse(_chunk(
                finish_reason="stop",
                usage={"prompt_tokens": 3, "completion_tokens": 2,
                       "total_tokens": 5})))
            self._send_chunk(_sse("[DONE]"))
        elif scenario == "client-cancel":
            self._send_chunk(_sse(_chunk(content="first")))
            for _ in range(60):
                self._send_chunk(_sse(_chunk(content="x")))
                time.sleep(0.1)
            self._send_chunk(_sse(_chunk(finish_reason="stop")))
            self._send_chunk(_sse("[DONE]"))
        elif scenario == "usage-then-bad-frame":
            # A usage block arrives before the malformed frame: the failure
            # row must keep the reported counts instead of replacing them
            # with an estimate or an unknown.
            self._send_chunk(_sse(_chunk(
                content="partial",
                usage={"prompt_tokens": 8, "completion_tokens": 2,
                       "total_tokens": 10})))
            self._send_chunk(_sse("{not json"))
            self._send_chunk(_sse("[DONE]"))
        elif scenario == "usage-only-tail":
            # The include_usage shape: content, a finish frame WITHOUT usage,
            # then one final block with ``choices: []`` and the usage tail.
            self._send_chunk(_sse(_chunk(content="hi")))
            self._send_chunk(_sse(_chunk(finish_reason="stop")))
            self._send_chunk(_sse(json.dumps({
                "choices": [],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2,
                          "total_tokens": 6,
                          "prompt_tokens_details": {"cached_tokens": 1}},
            })))
            self._send_chunk(_sse("[DONE]"))
        elif scenario == "fragmented-utf8":
            # A multibyte character split across HTTP chunks at arbitrary
            # byte offsets (including mid-character): the producer must join
            # the bytes of one line before decoding it.
            frame = _sse(_chunk(content="你好世界"))
            for offset in range(0, len(frame), 2):
                self._send_chunk(frame[offset:offset + 2])
            self._send_chunk(_sse(_chunk(
                finish_reason="stop",
                usage={"prompt_tokens": 3, "completion_tokens": 2,
                       "total_tokens": 5})))
            self._send_chunk(_sse("[DONE]"))
        elif scenario == "done-no-finish":
            # Q12=A negative: a real [DONE] with no finish_reason anywhere.
            # The failure row must still keep the usage block this upstream
            # reported before the sentinel.
            self._send_chunk(_sse(_chunk(
                content="hi",
                usage={"prompt_tokens": 3, "completion_tokens": 2,
                       "total_tokens": 5})))
            self._send_chunk(_sse("[DONE]"))
        elif scenario == "finish-no-done":
            # Q12=A negative: a non-empty finish_reason followed by a plain
            # EOF with no real [DONE] sentinel.
            self._send_chunk(_sse(_chunk(
                content="hi", finish_reason="stop",
                usage={"prompt_tokens": 3, "completion_tokens": 2,
                       "total_tokens": 5})))
        elif scenario == "aux-fields":
            first = {
                "system_fingerprint": "fp_n1",
                "service_tier": "default",
                "choices": [{
                    "index": 0,
                    "delta": {
                        "refusal": "no ",
                        "annotations": [{"type": "url_citation",
                                        "url": "https://a"}],
                        "audio": {"id": "audio_1", "data": "QUJD",
                                  "transcript": "hel", "expires_at": 1000},
                    },
                    "logprobs": {"content": [{"token": "he"}]},
                    "finish_reason": None,
                }],
            }
            second = {
                "choices": [{
                    "index": 0,
                    "delta": {
                        "content": "answer",
                        "refusal": "thanks",
                        "annotations": [{"type": "url_citation",
                                         "url": "https://b"}],
                        "audio": {"data": "REVG", "transcript": "lo"},
                    },
                    "logprobs": {"content": [{"token": "llo"}]},
                    "finish_reason": None,
                }],
            }
            final = {
                "choices": [{"index": 0, "delta": {},
                              "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                           "total_tokens": 5},
            }
            for frame in (first, second, final):
                self._send_chunk(_sse(json.dumps(frame, ensure_ascii=False)))
            self._send_chunk(_sse("[DONE]"))
        elif scenario == "legacy-function-call":
            first = {"choices": [{"index": 0, "delta": {
                "function_call": {"name": "get_time",
                                  "arguments": '{"zone":'}},
                "finish_reason": None}]}
            second = {"choices": [{"index": 0, "delta": {
                "function_call": {"arguments": '"UTC"}'}},
                "finish_reason": None}]}
            final = {
                "choices": [{"index": 0, "delta": {},
                              "finish_reason": "function_call"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2,
                           "total_tokens": 6},
            }
            for frame in (first, second, final):
                self._send_chunk(_sse(json.dumps(frame, ensure_ascii=False)))
            self._send_chunk(_sse("[DONE]"))
        elif scenario == "slow-downstream":
            # A 256 KiB answer: the fake upstream finishes quickly, but the
            # client reads nothing for a while, so the downstream write is
            # still pending when the test checks the reader slot.
            piece = "d" * 4096
            for _ in range(64):
                self._send_chunk(_sse(_chunk(content=piece)))
            self._send_chunk(_sse(_chunk(
                finish_reason="stop",
                usage={"prompt_tokens": 3, "completion_tokens": 2,
                       "total_tokens": 5})))
            self._send_chunk(_sse("[DONE]"))
            StubUpstreamHandler.slow_done.set()
        elif scenario in ("reject-stream-options", "ok-sse"):
            for frame in _ok_frames():
                self._send_chunk(frame)
        else:
            raise AssertionError(f"unknown aggregate scenario {scenario!r}")

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        StubUpstreamHandler.posts += 1
        if path != "/api/chat/completions":
            self._send_json(404, {"detail": "not found"})
            return
        StubUpstreamHandler.chat_bodies.append(body)
        scenario = body.get("model", "")
        if scenario == "auth-401":
            self._send_json(401, {"error": {"message": "unauthorized"}})
            return
        if scenario == "reject-stream-options" and "stream_options" in body:
            # The pre-existing compatibility retry: the aggregate request
            # carries stream_options, this upstream rejects it with a plain
            # non-SSE 400, and chat2api re-sends once without the option.
            self._send_json(400, {"error": {
                "message": "stream_options is not supported"}})
            return
        if scenario == "native-504":
            self._send_json(504, {"detail": "gateway timeout"})
            return
        if not body.get("stream"):
            self._send_json(200, DEFAULT_CHAT_JSON)
            return
        if scenario == "missing-terminal":
            self._send_headers(chunked=False)
            self.wfile.write(_sse(_chunk(content="partial")))
            self.wfile.flush()
            return
        if scenario == "mid-stream-disconnect":
            self._send_headers(chunked=True)
            self._send_chunk(_sse(_chunk(content="partial")))
            return  # no terminating chunk: the upstream dies mid-body
        self._send_headers(chunked=True)
        try:
            self._write_events(scenario)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class _FakeResponse:
    """Minimal requests response for the _UpstreamStream unit test."""

    def __init__(self, payload):
        self._payload = payload
        self.closed = False

    def iter_content(self, chunk_size=1):
        for offset in range(0, len(self._payload), chunk_size):
            yield self._payload[offset:offset + chunk_size]

    def close(self):
        self.closed = True


def _drain_upstream(upstream, timeout=2.0):
    lines = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("upstream stream did not end")
        try:
            item = upstream.next_item(min(0.05, remaining))
        except queue.Empty:
            continue
        if item is chat2api._UPSTREAM_END:
            return lines
        lines.append(item)


class TestNonStreamAggregateUnit(unittest.TestCase):
    """The pure SSE state machine, no server or ledger involved."""

    def _aggregator(self, model="chat-model"):
        return nonstream_aggregate.ChatCompletionAggregator(
            default_model=model)

    def _feed(self, aggregator, **kwargs):
        aggregator.feed_line(_chunk_line(**kwargs))
        aggregator.feed_line("")

    def test_crlf_multiline_data_and_fragmented_utf8(self):
        aggregator = self._aggregator()
        # Multi-line data: the two data lines are joined with "\n", so the
        # first event is valid JSON split at a token boundary. CRLF line
        # endings and an empty chunk must not change the decoded text; the
        # multibyte characters are split across byte chunks and must be
        # joined before decoding.
        wire = (
            b'data: {"id":"c1","created":7,"model":"m","choices":\r\n'
            b'data: [{"index":0,"delta":{"content":"he"}}]}\r\n'
            b'\r\n'
            b'data: {"choices":[{"index":0,"delta":{"content":"'
            + "llo你好".encode("utf-8") + b'"}}]}\r\n'
            b'\r\n'
            b'data: {"choices":[{"index":0,"delta":{},'
            b'"finish_reason":"stop"}],"usage":{"prompt_tokens":3,'
            b'"completion_tokens":2,"total_tokens":5}}\r\n'
            b'\r\n'
            b'data: [DONE]\r\n\r\n'
        )
        for offset in range(0, len(wire), 3):
            aggregator.feed_bytes(wire[offset:offset + 3])
            aggregator.feed_bytes(b"")  # empty chunks are legal
        result = aggregator.finish()
        message = result["choices"][0]["message"]
        self.assertEqual(message["content"], "hello你好")
        self.assertEqual(result["id"], "c1")
        self.assertEqual(result["created"], 7)
        self.assertEqual(result["model"], "m")
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")
        self.assertEqual(result["usage"]["prompt_tokens"], 3)

    def test_comments_empty_events_and_other_fields_are_ignored(self):
        aggregator = self._aggregator()
        aggregator.feed_line(": keepalive")
        aggregator.feed_line("")
        aggregator.feed_line("event: message")
        aggregator.feed_line("data: ")
        aggregator.feed_line("")
        aggregator.feed_line("id: 42")
        self._feed(aggregator, content="hi", finish_reason="stop")
        aggregator.feed_line("data: [DONE]")
        aggregator.feed_line("")
        result = aggregator.finish()
        self.assertEqual(result["choices"][0]["message"]["content"], "hi")

    def test_bad_frame_is_a_failure(self):
        aggregator = self._aggregator()
        self._feed(aggregator, content="ok")
        aggregator.feed_line("data: {not json")
        with self.assertRaises(nonstream_aggregate.AggregateError) as ctx:
            aggregator.feed_line("")
        self.assertEqual(ctx.exception.reason, "upstream_bad_frame")

    def test_no_terminal_credentials_is_a_failure(self):
        # Q12=A control case: neither [DONE] nor finish_reason is present.
        aggregator = self._aggregator()
        self._feed(aggregator, content="partial")
        with self.assertRaises(nonstream_aggregate.AggregateError) as ctx:
            aggregator.finish()
        self.assertEqual(ctx.exception.reason, "upstream_missing_terminal")

    def test_explicit_error_is_a_failure(self):
        aggregator = self._aggregator()
        self._feed(aggregator, content="partial")
        aggregator.feed_line('data: {"error":{"message":"boom"}}')
        with self.assertRaises(nonstream_aggregate.AggregateError) as ctx:
            aggregator.feed_line("")
        self.assertEqual(ctx.exception.reason, "upstream_explicit_error")
        self.assertEqual(ctx.exception.message, "boom")

    def test_done_without_finish_reason_is_a_missing_terminal_failure(self):
        # Q12=A: the real [DONE] alone is NOT enough. The streaming path
        # reports upstream_missing_finish_reason after committing a 200; this
        # path has committed nothing, so it fails instead of returning
        # finish_reason=null. Neither credential is invented.
        aggregator = self._aggregator()
        self._feed(aggregator, content="hi")
        aggregator.feed_line("data: [DONE]")
        aggregator.feed_line("")
        with self.assertRaises(nonstream_aggregate.AggregateError) as ctx:
            aggregator.finish()
        self.assertEqual(ctx.exception.reason, "upstream_missing_terminal")

    def test_finish_reason_without_done_is_a_missing_terminal_failure(self):
        # Q12=A: a non-empty finish_reason followed by plain EOF is still an
        # incomplete terminal: the real [DONE] sentinel is required too.
        aggregator = self._aggregator()
        self._feed(aggregator, content="hi")
        self._feed(aggregator, finish_reason="stop")
        self.assertFalse(aggregator.finished)
        with self.assertRaises(nonstream_aggregate.AggregateError) as ctx:
            aggregator.finish()
        self.assertEqual(ctx.exception.reason, "upstream_missing_terminal")

    def test_empty_finish_reason_with_done_is_a_missing_terminal_failure(self):
        # Q12=A requires a NON-EMPTY finish_reason: "" is not a credential.
        aggregator = self._aggregator()
        self._feed(aggregator, content="hi", finish_reason="")
        aggregator.feed_line("data: [DONE]")
        aggregator.feed_line("")
        with self.assertRaises(nonstream_aggregate.AggregateError) as ctx:
            aggregator.finish()
        self.assertEqual(ctx.exception.reason, "upstream_missing_terminal")

    def test_both_terminals_complete_the_aggregate(self):
        # Q12=A positive control: a non-empty finish_reason AND [DONE] succeed.
        aggregator = self._aggregator()
        self._feed(aggregator, content="hi", finish_reason="stop")
        self.assertFalse(aggregator.finished)
        aggregator.feed_line("data: [DONE]")
        aggregator.feed_line("")
        self.assertTrue(aggregator.finished)
        result = aggregator.finish()
        self.assertEqual(result["choices"][0]["message"]["content"], "hi")
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")

    def test_usage_tail_missing_stays_unknown(self):
        aggregator = self._aggregator()
        self._feed(aggregator, content="hi", finish_reason="stop")
        aggregator.feed_line("data: [DONE]")
        aggregator.feed_line("")
        result = aggregator.finish()
        self.assertNotIn("usage", result)

    def test_usage_null_does_not_erase_reported_counts(self):
        aggregator = self._aggregator()
        self._feed(aggregator, content="hi", finish_reason="stop",
                   usage={"prompt_tokens": 10, "completion_tokens": 4,
                          "total_tokens": 14,
                          "prompt_tokens_details": {"cached_tokens": 5}})
        # A later snapshot that reports null for every count must not erase the
        # counts the earlier snapshot already reported, including the nested
        # detail block.
        self._feed(aggregator, usage={
            "prompt_tokens": None, "completion_tokens": None,
            "total_tokens": None,
            "prompt_tokens_details": {"cached_tokens": None}})
        aggregator.feed_line("data: [DONE]")
        aggregator.feed_line("")
        usage = aggregator.finish()["usage"]
        self.assertEqual(usage["prompt_tokens"], 10)
        self.assertEqual(usage["completion_tokens"], 4)
        self.assertEqual(usage["total_tokens"], 14)
        self.assertEqual(usage["prompt_tokens_details"]["cached_tokens"], 5)

    def test_usage_invalid_values_do_not_erase_reported_counts(self):
        aggregator = self._aggregator()
        self._feed(aggregator, content="hi", finish_reason="stop",
                   usage={"prompt_tokens": 10, "completion_tokens": 4,
                          "total_tokens": 14,
                          "prompt_tokens_details": {"cached_tokens": 5}})
        # Negative, fractional, boolean and string values are not legal counts:
        # none may overwrite an already reported value.
        self._feed(aggregator, usage={
            "prompt_tokens": -1, "completion_tokens": 1.5,
            "total_tokens": True,
            "prompt_tokens_details": {"cached_tokens": "5"}})
        aggregator.feed_line("data: [DONE]")
        aggregator.feed_line("")
        usage = aggregator.finish()["usage"]
        self.assertEqual(usage["prompt_tokens"], 10)
        self.assertEqual(usage["completion_tokens"], 4)
        self.assertEqual(usage["total_tokens"], 14)
        self.assertEqual(usage["prompt_tokens_details"]["cached_tokens"], 5)

    def test_usage_invalid_only_stays_unknown(self):
        aggregator = self._aggregator()
        self._feed(aggregator, content="hi", finish_reason="stop",
                   usage={"prompt_tokens": None, "completion_tokens": -1,
                          "total_tokens": "14"})
        aggregator.feed_line("data: [DONE]")
        aggregator.feed_line("")
        self.assertNotIn("usage", aggregator.finish())

    def test_usage_nested_details_merge_recursively(self):
        aggregator = self._aggregator()
        self._feed(aggregator, content="hi", finish_reason="stop",
                   usage={"prompt_tokens_details": {"cached_tokens": 5,
                                                    "audio_tokens": 2},
                          "completion_tokens_details": {
                              "reasoning_tokens": 3}})
        self._feed(aggregator, usage={
            "prompt_tokens_details": {"cached_tokens": None},
            "completion_tokens_details": {"reasoning_tokens": 7,
                                          "audio_tokens": 0}})
        aggregator.feed_line("data: [DONE]")
        aggregator.feed_line("")
        usage = aggregator.finish()["usage"]
        self.assertEqual(usage["prompt_tokens_details"]["cached_tokens"], 5)
        self.assertEqual(usage["prompt_tokens_details"]["audio_tokens"], 2)
        self.assertEqual(
            usage["completion_tokens_details"]["reasoning_tokens"], 7)
        # A trusted ZERO is a reported count and overwrites the old value.
        self.assertEqual(usage["completion_tokens_details"]["audio_tokens"], 0)

    def test_legacy_function_call_maps_to_one_tool_call(self):
        aggregator = self._aggregator()
        self._feed(aggregator, function_call={"name": "get_time",
                                              "arguments": '{"zone":'})
        self._feed(aggregator, function_call={"arguments": '"UTC"}'})
        self._feed(aggregator, finish_reason="function_call")
        aggregator.feed_line("data: [DONE]")
        aggregator.feed_line("")
        result = aggregator.finish()
        self.assertEqual(result["choices"][0]["finish_reason"], "function_call")
        tool_calls = result["choices"][0]["message"]["tool_calls"]
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["type"], "function")
        self.assertEqual(tool_calls[0]["function"]["name"], "get_time")
        self.assertEqual(tool_calls[0]["function"]["arguments"],
                         '{"zone":"UTC"}')
        # The legacy shape carries no id; inventing one would fabricate data.
        self.assertNotIn("id", tool_calls[0])

    def test_legacy_function_call_is_not_duplicated_with_tool_calls(self):
        # tool_calls first, then the legacy mirror of the same call: one entry.
        aggregator = self._aggregator()
        self._feed(aggregator, tool_calls=[
            {"index": 0, "id": "c1", "type": "function",
             "function": {"name": "echo", "arguments": "{}"}}])
        self._feed(aggregator, function_call={"name": "echo",
                                              "arguments": "{}"})
        self._feed(aggregator, finish_reason="tool_calls")
        aggregator.feed_line("data: [DONE]")
        aggregator.feed_line("")
        tool_calls = aggregator.finish()["choices"][0]["message"]["tool_calls"]
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["id"], "c1")
        self.assertEqual(tool_calls[0]["function"]["arguments"], "{}")

        # function_call first, then tool_calls: the indexed call wins and the
        # legacy channel is not emitted a second time.
        aggregator = self._aggregator()
        self._feed(aggregator, function_call={"name": "echo",
                                              "arguments": '{"x":'})
        self._feed(aggregator, tool_calls=[
            {"index": 0, "id": "c2", "type": "function",
             "function": {"name": "echo", "arguments": '{"x":1}'}}])
        self._feed(aggregator, finish_reason="tool_calls")
        aggregator.feed_line("data: [DONE]")
        aggregator.feed_line("")
        tool_calls = aggregator.finish()["choices"][0]["message"]["tool_calls"]
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["id"], "c2")
        self.assertEqual(tool_calls[0]["function"]["arguments"], '{"x":1}')

    def test_upstream_auxiliary_fields_are_preserved(self):
        aggregator = self._aggregator()
        first = {
            "system_fingerprint": "fp_n1",
            "service_tier": "default",
            "choices": [{
                "index": 0,
                "delta": {
                    "refusal": "no ",
                    "annotations": [{"type": "url_citation",
                                    "url": "https://a"}],
                    "audio": {"id": "audio_1", "data": "QUJD",
                              "transcript": "hel", "expires_at": 1000},
                },
                "logprobs": {"content": [{"token": "he"}]},
                "finish_reason": None,
            }],
        }
        second = {
            "system_fingerprint": "fp_later",
            "service_tier": "default",
            "choices": [{
                "index": 0,
                "delta": {
                    "content": "answer",
                    "refusal": "thanks",
                    "annotations": [{"type": "url_citation",
                                     "url": "https://b"}],
                    "audio": {"data": "REVG", "transcript": "lo"},
                },
                "logprobs": {"content": [{"token": "llo"}]},
                "finish_reason": None,
            }],
        }
        nulls = {
            "system_fingerprint": None,
            "service_tier": None,
            "choices": [{
                "index": 0,
                "delta": {"refusal": None, "annotations": [],
                          "audio": None},
                "logprobs": None,
                "finish_reason": None,
            }],
        }
        final = {
            "choices": [{"index": 0, "delta": {},
                          "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                       "total_tokens": 5},
        }
        for chunk in (first, second, nulls, final):
            aggregator.feed_line("data: " + json.dumps(chunk,
                                                       ensure_ascii=False))
            aggregator.feed_line("")
        aggregator.feed_line("data: [DONE]")
        aggregator.feed_line("")
        result = aggregator.finish()
        # The first reported stable metadata wins; null never erases it.
        self.assertEqual(result["system_fingerprint"], "fp_n1")
        self.assertEqual(result["service_tier"], "default")
        choice = result["choices"][0]
        message = choice["message"]
        self.assertEqual(message["content"], "answer")
        self.assertEqual(message["refusal"], "no thanks")
        self.assertEqual(message["annotations"], [
            {"type": "url_citation", "url": "https://a"},
            {"type": "url_citation", "url": "https://b"},
        ])
        self.assertEqual(message["audio"], {
            "id": "audio_1", "data": "QUJDREVG",
            "transcript": "hello", "expires_at": 1000,
        })
        self.assertEqual(choice["logprobs"]["content"],
                         [{"token": "he"}, {"token": "llo"}])
        self.assertNotIn("no thanks", message["content"])
        self.assertNotIn("hello", message["content"])

    def test_choice_level_auxiliary_fields_are_preserved(self):
        # An upstream may put refusal/annotations/audio/logprobs on the choice
        # instead of its delta; both locations are preserved in the one
        # non-stream body (message for the message fields, choice for
        # logprobs) instead of being dropped.
        aggregator = self._aggregator()
        first = {
            "choices": [{
                "index": 0,
                "delta": {"content": "hi"},
                "refusal": "no ",
                "annotations": [{"type": "url_citation", "url": "https://c"}],
                "audio": {"id": "audio_2", "data": "QQ==",
                          "transcript": "x"},
                "logprobs": {"content": [{"token": "hi"}]},
                "finish_reason": None,
            }],
        }
        second = {
            "choices": [{
                "index": 0,
                "delta": {},
                "refusal": "thanks",
                "annotations": [{"type": "url_citation", "url": "https://d"}],
                "audio": {"data": "Qg==", "transcript": "y"},
                "logprobs": {"content": [{"token": "!"}]},
                "finish_reason": "stop",
            }],
        }
        for chunk in (first, second):
            aggregator.feed_line("data: " + json.dumps(chunk,
                                                       ensure_ascii=False))
            aggregator.feed_line("")
        aggregator.feed_line("data: [DONE]")
        aggregator.feed_line("")
        result = aggregator.finish()
        choice = result["choices"][0]
        message = choice["message"]
        self.assertEqual(message["content"], "hi")
        self.assertEqual(message["refusal"], "no thanks")
        self.assertEqual(message["annotations"], [
            {"type": "url_citation", "url": "https://c"},
            {"type": "url_citation", "url": "https://d"},
        ])
        self.assertEqual(message["audio"], {
            "id": "audio_2", "data": "QQ==Qg==",
            "transcript": "xy",
        })
        self.assertEqual(choice["logprobs"]["content"],
                         [{"token": "hi"}, {"token": "!"}])

    def test_auxiliary_fields_missing_are_not_fabricated(self):
        aggregator = self._aggregator()
        self._feed(aggregator, content="hi", finish_reason="stop")
        aggregator.feed_line("data: [DONE]")
        aggregator.feed_line("")
        result = aggregator.finish()
        self.assertNotIn("system_fingerprint", result)
        self.assertNotIn("service_tier", result)
        choice = result["choices"][0]
        self.assertNotIn("logprobs", choice)
        message = choice["message"]
        self.assertNotIn("refusal", message)
        self.assertNotIn("annotations", message)
        self.assertNotIn("audio", message)

    def test_upstream_stream_keeps_reasoning_channels(self):
        # D18-5: the share profile no longer reshapes upstream SSE lines.
        # The aggregate path and the client-visible stream keep BOTH reasoning
        # channels exactly like the private 8000 chain.
        wire = (_sse(_chunk(reasoning="think", content="answer"))
                + _sse("[DONE]"))
        upstream = chat2api._UpstreamStream(_FakeResponse(wire))
        upstream.start()
        kept = _drain_upstream(upstream)
        kept_deltas = [
            json.loads(line[len("data:"):].strip())["choices"][0]["delta"]
            for line in kept
            if line.startswith("data:") and line.strip() != "data: [DONE]"]
        self.assertEqual(kept_deltas, [{"reasoning": "think",
                                       "content": "answer"}])

    def test_tool_arguments_join_across_frames_and_parallel_indices(self):
        aggregator = self._aggregator()
        self._feed(aggregator, tool_calls=[
            {"index": 0, "id": "c1", "type": "function",
             "function": {"name": "get_time", "arguments": '{"zone":'}},
            {"index": 1, "id": "c2", "type": "function",
             "function": {"name": "echo", "arguments": '{"text":'}},
        ])
        self._feed(aggregator, tool_calls=[
            {"index": 0, "function": {"arguments": '"UTC"}'}},
            {"index": 1, "function": {"arguments": '"hi"}'}},
        ])
        self._feed(aggregator, finish_reason="tool_calls")
        aggregator.feed_line("data: [DONE]")
        aggregator.feed_line("")
        result = aggregator.finish()
        tool_calls = result["choices"][0]["message"]["tool_calls"]
        self.assertEqual([call["id"] for call in tool_calls], ["c1", "c2"])
        self.assertEqual(tool_calls[0]["id"], "c1")
        self.assertEqual(tool_calls[0]["type"], "function")
        self.assertEqual(tool_calls[0]["function"]["name"], "get_time")
        self.assertEqual(tool_calls[0]["function"]["arguments"],
                         '{"zone":"UTC"}')
        self.assertEqual(tool_calls[1]["id"], "c2")
        self.assertEqual(tool_calls[1]["function"]["name"], "echo")
        self.assertEqual(tool_calls[1]["function"]["arguments"],
                         '{"text":"hi"}')

    def test_single_tool_call_without_index_stays_one_call(self):
        aggregator = self._aggregator()
        self._feed(aggregator, tool_calls=[
            {"id": "c1", "function": {"name": "only",
                                       "arguments": "{}"}},
        ])
        self._feed(aggregator, finish_reason="tool_calls")
        aggregator.feed_line("data: [DONE]")
        aggregator.feed_line("")
        result = aggregator.finish()
        tool_calls = result["choices"][0]["message"]["tool_calls"]
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["id"], "c1")
        self.assertEqual(tool_calls[0]["function"]["name"], "only")

    def test_parallel_tool_calls_without_indices_are_a_bad_frame(self):
        aggregator = self._aggregator()
        with self.assertRaises(nonstream_aggregate.AggregateError) as ctx:
            self._feed(aggregator, tool_calls=[
                {"id": "c1", "function": {"name": "a", "arguments": "{}"}},
                {"id": "c2", "function": {"name": "b", "arguments": "{}"}},
            ])
        self.assertEqual(ctx.exception.reason, "upstream_bad_frame")

    def test_reasoning_is_never_part_of_content(self):
        aggregator = self._aggregator()
        self._feed(aggregator, reasoning="think-a")
        self._feed(aggregator, reasoning_content="think-b")
        self._feed(aggregator, content="answer")
        self._feed(aggregator, finish_reason="stop")
        aggregator.feed_line("data: [DONE]")
        aggregator.feed_line("")
        result = aggregator.finish()
        message = result["choices"][0]["message"]
        self.assertEqual(message["content"], "answer")
        self.assertEqual(message["reasoning"], "think-a")
        self.assertEqual(message["reasoning_content"], "think-b")
        self.assertNotIn("think-a", message["content"])
        self.assertNotIn("think-b", message["content"])

    def test_empty_deltas_do_not_accumulate_state(self):
        aggregator = self._aggregator()
        empty_call = {"index": 0, "id": "", "type": "",
                      "function": {"name": "", "arguments": ""}}
        index_only = {"index": 0}
        # A long keepalive-heavy stream: every frame carries empty content,
        # both empty reasoning channels and tool calls whose only real field is
        # the index. None of them may leave a list slot or a tool-call state.
        for _ in range(100000):
            self._feed(aggregator, content="", reasoning="",
                       reasoning_content="",
                       tool_calls=[empty_call, index_only])
        state = aggregator._choices[0]
        self.assertEqual(state.content, [])
        self.assertEqual(state.reasoning["reasoning"], [])
        self.assertEqual(state.reasoning["reasoning_content"], [])
        self.assertEqual(state.tool_calls, {})
        self.assertEqual(aggregator.output_estimate(), 0)
        empty_message = aggregator.result()["choices"][0]["message"]
        self.assertEqual(empty_message["content"], "")
        self.assertNotIn("tool_calls", empty_message)
        self.assertNotIn("reasoning", empty_message)
        self.assertNotIn("reasoning_content", empty_message)

        # Real deltas still aggregate exactly after the empty flood.
        self._feed(aggregator, content="hel")
        self._feed(aggregator, content="lo")
        self._feed(aggregator, reasoning="think")
        self._feed(aggregator, reasoning_content="deep")
        self._feed(aggregator, tool_calls=[
            {"index": 0, "id": "call-1", "type": "function",
             "function": {"name": "echo", "arguments": ""}},
        ])
        self._feed(aggregator, tool_calls=[
            {"index": 0, "function": {"arguments": '{"x":1}'}},
        ])
        self.assertEqual(state.content, ["hel", "lo"])
        self.assertEqual(state.reasoning["reasoning"], ["think"])
        self.assertEqual(state.reasoning["reasoning_content"], ["deep"])
        self.assertEqual(state.tool_calls[0].arguments, ['{"x":1}'])
        self._feed(aggregator, finish_reason="tool_calls")
        aggregator.feed_line("data: [DONE]")
        aggregator.feed_line("")
        message = aggregator.finish()["choices"][0]["message"]
        self.assertEqual(message["content"], "hello")
        self.assertEqual(message["reasoning"], "think")
        self.assertEqual(message["reasoning_content"], "deep")
        self.assertEqual(message["tool_calls"][0]["id"], "call-1")
        self.assertEqual(message["tool_calls"][0]["type"], "function")
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "echo")
        self.assertEqual(message["tool_calls"][0]["function"]["arguments"],
                         '{"x":1}')


class TestNonStreamAggregateMemoryBounds(unittest.TestCase):
    """R14: the aggregate state and its one serialization are O(output).

    The module docstring claims one list slot per non-empty piece, one body only
    after the stream ended, and no truncation or output cap. These tests pin that
    shape with tracemalloc: a long non-empty answer stays complete, 128
    simultaneously live aggregators keep independent state and become garbage
    after their result exists, and one json.dumps+encode pass is bounded by the
    body size. The ratios were measured on this interpreter and are asserted
    with a safety margin; they are a resource bound, not a promise that any
    arbitrary answer fits in a fixed number of bytes.
    """

    LONG_LINES = 8000
    LONG_PIECE = "x" * 128            # 8000 x 128 = 1,024,000 chars
    SMALL_REQUESTS = 64
    MEDIUM_REQUESTS = 64
    SMALL_FRAMES = 8
    SMALL_PIECE = "s" * 128           # 1 KiB per small answer
    MEDIUM_FRAMES = 16
    MEDIUM_PIECE = "m" * 512           # 8 KiB per medium answer

    def _feed(self, aggregator, piece, frames):
        for _ in range(frames):
            aggregator.feed_line(_chunk_line(content=piece))
            aggregator.feed_line("")
        aggregator.feed_line(_chunk_line(
            finish_reason="stop",
            usage={"prompt_tokens": 3, "completion_tokens": 2,
                   "total_tokens": 5}))
        aggregator.feed_line("")
        aggregator.feed_line("data: [DONE]")
        aggregator.feed_line("")

    def test_long_output_is_complete_and_peak_tracks_the_output(self):
        expected = self.LONG_PIECE * self.LONG_LINES
        gc.collect()
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            aggregator = nonstream_aggregate.ChatCompletionAggregator(
                default_model="chat-model")
            for _ in range(self.LONG_LINES):
                aggregator.feed_line(_chunk_line(content=self.LONG_PIECE))
                aggregator.feed_line("")
            aggregator.feed_line(_chunk_line(
                finish_reason="stop",
                usage={"prompt_tokens": 3, "completion_tokens": 2,
                       "total_tokens": 5}))
            aggregator.feed_line("")
            aggregator.feed_line("data: [DONE]")
            aggregator.feed_line("")
            feed_current, feed_peak = tracemalloc.get_traced_memory()
            result = aggregator.finish()
            result_current, result_peak = tracemalloc.get_traced_memory()
            # The Handler's own _json does exactly this ONE pass
            # (json.dumps(..., ensure_ascii=False).encode()).
            body = json.dumps(result, ensure_ascii=False)
            encoded = body.encode()
            serialize_current, serialize_peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        content = result["choices"][0]["message"]["content"]
        # Full equality, not a prefix: every one of the 8000 lines survived.
        self.assertEqual(len(content), len(expected))
        self.assertEqual(content, expected)
        self.assertEqual(len(aggregator._choices[0].content),
                         self.LONG_LINES)  # one slot per non-empty piece
        self.assertEqual(aggregator.output_estimate(), len(expected) // 4)
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")
        self.assertEqual(result["usage"]["total_tokens"], 5)
        self.assertEqual(json.loads(body), result)
        # O(output): 1.39x for the feed, 2.39x for result() (joined copy plus
        # the per-line slots), 4.89x for the one dumps+encode pass. The bounds
        # below keep a safety margin instead of pinning allocator internals.
        self.assertLessEqual(feed_peak, 3 * len(encoded),
                             f"feed peak {feed_peak} > 3x body")
        self.assertLessEqual(result_peak, 4 * len(encoded),
                             f"result peak {result_peak} > 4x body")
        self.assertLessEqual(serialize_peak, 8 * len(encoded),
                             f"serialize peak {serialize_peak} > 8x body")

    def test_128_concurrent_aggregators_keep_independent_state_and_release(self):
        total = (self.SMALL_REQUESTS * self.SMALL_FRAMES * len(self.SMALL_PIECE)
                 + self.MEDIUM_REQUESTS * self.MEDIUM_FRAMES
                 * len(self.MEDIUM_PIECE))
        count = self.SMALL_REQUESTS + self.MEDIUM_REQUESTS
        results = [None] * count
        refs = [None] * count
        barrier = threading.Barrier(count)
        lock = threading.Lock()

        def worker(index):
            small = index < self.SMALL_REQUESTS
            piece = self.SMALL_PIECE if small else self.MEDIUM_PIECE
            frames = self.SMALL_FRAMES if small else self.MEDIUM_FRAMES
            barrier.wait()
            aggregator = nonstream_aggregate.ChatCompletionAggregator(
                default_model="chat-model")
            with lock:
                refs[index] = weakref.ref(aggregator)
            self._feed(aggregator, piece, frames)
            result = aggregator.finish()
            content = result["choices"][0]["message"]["content"]
            with lock:
                results[index] = {
                    "content": content,
                    "expected": piece * frames,
                    "finish_reason": result["choices"][0]["finish_reason"],
                    "total_tokens": (result.get("usage") or {}).get(
                        "total_tokens"),
                }

        gc.collect()
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            threads = [
                threading.Thread(target=worker, args=(index,),
                                 name=f"aggregate-memory-{index}",
                                 daemon=True)
                for index in range(count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
            current, peak = tracemalloc.get_traced_memory()
            gc.collect()
            after_gc = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        self.assertEqual([thread.name for thread in threads
                          if thread.is_alive()], [])
        for index, record in enumerate(results):
            self.assertIsNotNone(record, index)
            self.assertEqual(len(record["content"]),
                             len(record["expected"]), index)
            self.assertEqual(record["content"], record["expected"], index)
            self.assertEqual(record["finish_reason"], "stop", index)
            self.assertEqual(record["total_tokens"], 5, index)
        # Every aggregator is garbage once its result exists: no retained
        # per-request state after the round.
        self.assertEqual(sum(1 for ref in refs if ref() is not None), 0)
        # The 128 states and the joined answers fall back to the interpreter's
        # baseline; measured 375 KiB here (the results list itself).
        self.assertLessEqual(after_gc, 4 * 1024 * 1024)
        # Measured 4.4x the 576 KiB of output. This is a peak bound for 128
        # simultaneously live aggregators, not a fixed-memory claim.
        self.assertLessEqual(peak, 12 * total,
                             f"128-aggregator peak {peak} > 12x {total}")


def _chunk_line(**kwargs):
    return "data: " + _chunk(**kwargs)


class TestNonStreamAggregateScopeUnit(unittest.TestCase):
    """R13: the trusted (key, model, protocol) scope is parsed strictly and
    every component must match; anything missing or malformed stays native."""

    def setUp(self):
        switch = mock.patch.object(chat2api, "NONSTREAM_UPSTREAM_AGGREGATE",
                                   True)
        switch.start()
        self.addCleanup(switch.stop)
        self.scope = aggregate_scope(models=("model-a",),
                                      protocols=("chat", "messages"))
        scope_patch = mock.patch.object(chat2api, "NONSTREAM_AGGREGATE_SCOPE",
                                        self.scope)
        scope_patch.start()
        self.addCleanup(scope_patch.stop)
        self.addCleanup(chat2api._request_facts.__dict__.clear)

    def _facts(self, *, key_id=ATTRIBUTION_KEY_ID, protocol="chat",
               attribution="verified"):
        chat2api.set_request_facts(protocol, key_id, "request-id",
                                   attribution)

    # ---------- parser ----------

    def test_valid_scope_parses_to_three_sets(self):
        scope = aggregate_scope(
            key_ids=("key-a", "key-b"), models=("model-a",),
            protocols=("chat", "responses"))
        self.assertEqual(scope["key_ids"], frozenset({"key-a", "key-b"}))
        self.assertEqual(scope["models"], frozenset({"model-a"}))
        self.assertEqual(scope["protocols"],
                         frozenset({"chat", "responses"}))

    def test_absent_or_blank_scope_is_none(self):
        for raw in (None, "", "   "):
            self.assertIsNone(
                chat2api.parse_nonstream_aggregate_scope(raw), raw)

    def test_malformed_json_scope_is_none(self):
        for raw in ("{", "[]", '"x"', "null", "42", "true", "{}"):
            self.assertIsNone(
                chat2api.parse_nonstream_aggregate_scope(raw), raw)

    def test_partial_or_extra_fields_are_none(self):
        for data in (
                {"key_ids": ["k"]},
                {"models": ["m"], "protocols": ["chat"]},
                {"key_ids": ["k"], "models": ["m"]},
                {"key_ids": ["k"], "models": ["m"],
                 "protocols": ["chat"], "extra": []},
        ):
            self.assertIsNone(chat2api.parse_nonstream_aggregate_scope(
                json.dumps(data)), data)

    def test_wrong_typed_or_duplicate_values_are_none(self):
        base = {"key_ids": ["k"], "models": ["m"], "protocols": ["chat"]}
        for field, value in (
                ("key_ids", "k"), ("models", {"m": True}),
                ("protocols", [1]), ("key_ids", [""]),
                ("models", ["m", "m"]), ("protocols", ["chat", "chat"]),
                ("models", [" m"]),
        ):
            data = dict(base)
            data[field] = value
            self.assertIsNone(chat2api.parse_nonstream_aggregate_scope(
                json.dumps(data)), (field, value))

    def test_partially_empty_scope_is_none(self):
        # Only the exact all-empty object is the explicit "disabled" state; a
        # partial selection is ambiguous and must not silently become "disabled".
        for data in (
                {"key_ids": [], "models": ["m"], "protocols": ["chat"]},
                {"key_ids": ["k"], "models": [], "protocols": ["chat"]},
                {"key_ids": ["k"], "models": ["m"], "protocols": []},
        ):
            self.assertIsNone(chat2api.parse_nonstream_aggregate_scope(
                json.dumps(data)), data)

    def test_production_empty_scope_is_disabled_and_matches_nothing(self):
        # The exact JSON console_share_production hands to the dedicated 8110
        # child. It is a VALID disabled scope (not a parse failure), and no
        # verified real key/model/protocol triple matches it.
        scope = chat2api.parse_nonstream_aggregate_scope(
            PRODUCTION_EMPTY_SCOPE_JSON)
        self.assertIsNotNone(scope)
        self.assertEqual(scope["key_ids"], frozenset())
        self.assertEqual(scope["models"], frozenset())
        self.assertEqual(scope["protocols"], frozenset())
        with mock.patch.object(chat2api, "NONSTREAM_AGGREGATE_SCOPE", scope), \
                mock.patch.object(chat2api, "_NONSTREAM_AGGREGATE_SCOPE_RAW",
                                  PRODUCTION_EMPTY_SCOPE_JSON):
            self.assertEqual(
                chat2api.nonstream_aggregate_scope_label(), "disabled")
            for protocol in ("chat", "messages", "responses"):
                for key_id in (ATTRIBUTION_KEY_ID, UNSELECTED_KEY_ID,
                               "real-user-key-c"):
                    self._facts(key_id=key_id, protocol=protocol)
                    for model in ("model-a", "DeepSeek-V4.1-Flash-1017"):
                        self.assertIs(
                            chat2api.nonstream_aggregate_allowed(protocol,
                                                                 model),
                            False, (key_id, protocol, model))

    def test_unknown_protocol_name_is_none(self):
        data = {"key_ids": ["k"], "models": ["m"],
                "protocols": ["chat", "embeddings"]}
        self.assertIsNone(chat2api.parse_nonstream_aggregate_scope(
            json.dumps(data)))

    # ---------- gate ----------

    def test_selected_triple_is_allowed(self):
        self._facts()
        self.assertIs(chat2api.nonstream_aggregate_allowed("chat", "model-a"),
                      True)

    def test_switch_off_is_native(self):
        self._facts()
        with mock.patch.object(chat2api, "NONSTREAM_UPSTREAM_AGGREGATE",
                               False):
            self.assertIs(
                chat2api.nonstream_aggregate_allowed("chat", "model-a"), False)

    def test_missing_scope_is_native(self):
        self._facts()
        with mock.patch.object(chat2api, "NONSTREAM_AGGREGATE_SCOPE", None):
            self.assertIs(
                chat2api.nonstream_aggregate_allowed("chat", "model-a"), False)

    def test_unverified_attribution_is_native(self):
        self._facts(attribution="absent")
        self.assertIs(chat2api.nonstream_aggregate_allowed("chat", "model-a"),
                      False)
        self._facts(key_id=None, attribution="absent")
        self.assertIs(chat2api.nonstream_aggregate_allowed("chat", "model-a"),
                      False)

    def test_unselected_key_is_native(self):
        self._facts(key_id=UNSELECTED_KEY_ID)
        self.assertIs(chat2api.nonstream_aggregate_allowed("chat", "model-a"),
                      False)

    def test_unselected_model_is_native(self):
        self._facts()
        self.assertIs(chat2api.nonstream_aggregate_allowed("chat", "model-b"),
                      False)
        self.assertIs(chat2api.nonstream_aggregate_allowed("chat", ""), False)
        self.assertIs(chat2api.nonstream_aggregate_allowed("chat", None),
                      False)

    def test_unselected_protocol_is_native(self):
        self._facts()
        self.assertIs(
            chat2api.nonstream_aggregate_allowed("messages", "model-a"), False)

    def test_protocol_fact_mismatch_is_native(self):
        # A handler cannot claim a protocol the HTTP boundary did not install.
        self._facts(protocol="messages")
        self.assertIs(chat2api.nonstream_aggregate_allowed("chat", "model-a"),
                      False)

    def test_malformed_scope_shape_is_native(self):
        self._facts()
        for scope in ({"key_ids": "k", "models": {"model-a"},
                       "protocols": {"chat"}},
                      {"key_ids": frozenset({"k"})}):
            with mock.patch.object(chat2api, "NONSTREAM_AGGREGATE_SCOPE", scope):
                self.assertIs(chat2api.nonstream_aggregate_allowed(
                    "chat", "model-a"), False, scope)


@unittest.skip("HMAC attribution retired with the merged share process")
class TestNonStreamAggregateHttp(unittest.TestCase):
    """Real Handler -> loopback fake UM; only the UM boundary is faked."""

    def setUp(self):
        self._usage_dir, self.usage_log = harness.install_usage_isolation(
            self, chat2api, ledger_name="usage-chat2api.jsonl")
        install_attribution_key(self)
        harness.assert_stub_credentials(
            chat2api, local_key=None, upstream_token=STUB_TOKEN,
            stub_token_literal=STUB_TOKEN)

        stub = harness.TrackedHTTPServer(("127.0.0.1", 0),
                                         StubUpstreamHandler)
        self.stub = stub
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        harness.install_tracked_server(self, stub, name="upstream-stub",
                                         require_drained=False)
        StubUpstreamHandler.chat_bodies = []
        StubUpstreamHandler.posts = 0
        StubUpstreamHandler.slow_done = threading.Event()

        args = SimpleNamespace(
            base_url=f"http://127.0.0.1:{stub.server_port}",
            token=STUB_TOKEN,
            profile="profile",
            login_timeout=1,
            use_api_key=False,
            no_browser=True,
        )
        proxy = chat2api.Proxy({"token": STUB_TOKEN}, args)
        server = harness.TrackedHTTPServer(("127.0.0.1", 0),
                                           chat2api.Handler)
        server.proxy = proxy
        self.server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        harness.install_tracked_server(self, server, name="chat2api-gateway")
        self.base = f"http://127.0.0.1:{server.server_port}"

        for patch in (
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", False),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 0),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRY", False),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRIES", 0),
            mock.patch.object(chat2api, "NONSTREAM_UPSTREAM_AGGREGATE", False),
        ):
            patch.start()
            self.addCleanup(patch.stop)

        chat2api._reader_slots = threading.BoundedSemaphore(
            chat2api.STREAM_MAX_CONCURRENT_READERS)

    # ---------- helpers ----------

    def _post(self, model, *, aggregate, stream=False, timeout=30,
              key_id=ATTRIBUTION_KEY_ID, protocols=("chat",),
              sign_headers=None, install_scope=None,
              scope_models=None, scope_key_ids=(ATTRIBUTION_KEY_ID,),
              tamper_signature=False, extra_headers=None):
        body = {"model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": stream, "max_tokens": 16}
        sign_headers = aggregate if sign_headers is None else sign_headers
        install_scope = aggregate if install_scope is None else install_scope
        headers = None
        if sign_headers:
            headers = (tampered_marker_headers(key_id)
                       if tamper_signature else marker_headers(key_id))
        if extra_headers:
            headers = dict(headers or {})
            headers.update(extra_headers)
        scope = (aggregate_scope(
            models=(model,) if scope_models is None else scope_models,
            protocols=protocols, key_ids=scope_key_ids)
            if install_scope else None)
        with mock.patch.object(chat2api, "NONSTREAM_UPSTREAM_AGGREGATE",
                               aggregate), \
                mock.patch.object(chat2api, "NONSTREAM_AGGREGATE_SCOPE",
                                  scope):
            return requests.post(f"{self.base}/v1/chat/completions",
                                  json=body, headers=headers, timeout=timeout)

    def _ledger_rows(self, timeout=5.0, expected=1):
        deadline = time.monotonic() + timeout
        rows = []
        while time.monotonic() < deadline:
            try:
                rows = [json.loads(line) for line in
                        Path(chat2api.USAGE_LOG).read_text(
                            encoding="utf-8").splitlines() if line.strip()]
            except FileNotFoundError:
                rows = []
            if len(rows) >= expected:
                return rows
            time.sleep(0.02)
        self.fail(f"ledger rows not written within {timeout}s")

    def _assert_no_ledger_rows(self, grace=0.5):
        time.sleep(grace)
        try:
            rows = [line for line in Path(chat2api.USAGE_LOG).read_text(
                encoding="utf-8").splitlines() if line.strip()]
        except FileNotFoundError:
            rows = []
        self.assertEqual(rows, [])

    def _assert_one_aggregate_row(self, status):
        """Exactly ONE ledger row, the client's stream=False and the status."""
        self._ledger_rows()
        # A second row would be a duplicate accounting write; give the
        # handler's tail a moment to prove there is none.
        time.sleep(0.1)
        rows = [json.loads(line) for line in
                Path(chat2api.USAGE_LOG).read_text(
                    encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], status)
        self.assertIs(row["stream"], False)
        return row

    @staticmethod
    def reader_thread_count():
        return sum(1 for t in threading.enumerate()
                   if t.name == "chat2api-upstream-reader")

    def wait_reader_slots_restored(self, before, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if chat2api._reader_slots._value >= before:
                return
            time.sleep(0.05)
        self.fail(f"reader slots not restored within {timeout}s "
                  f"({chat2api._reader_slots._value} != {before})")

    def wait_reader_threads_reclaimed(self, baseline, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.reader_thread_count() <= baseline:
                return
            time.sleep(0.05)
        self.fail(f"reader threads not reclaimed within {timeout}s")

    def _production_ledger(self):
        return Path(harness.production_ledger_path(chat2api))

    # ---------- native path ----------

    def test_native_504_keeps_the_non_stream_error_mapping(self):
        response = self._post("native-504", aggregate=False)
        self.assertEqual(response.status_code, 502)
        self.assertIn("application/json",
                      response.headers.get("Content-Type", ""))
        error = response.json()["error"]
        self.assertIn("HTTP 504", error["detail"])
        self.assertIs(StubUpstreamHandler.chat_bodies[-1]["stream"], False)
        self.assertNotIn("stream_options",
                         StubUpstreamHandler.chat_bodies[-1])
        self._assert_no_ledger_rows()

    def test_native_non_stream_still_returns_native_json(self):
        response = self._post("native-model", aggregate=False)
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/json",
                      response.headers.get("Content-Type", ""))
        self.assertEqual(response.json(), DEFAULT_CHAT_JSON)
        self.assertIs(StubUpstreamHandler.chat_bodies[-1]["stream"], False)
        self.assertNotIn("stream_options",
                         StubUpstreamHandler.chat_bodies[-1])
        row = self._ledger_rows()[0]
        self.assertIs(row["stream"], False)
        self.assertEqual(row["status"], "ok")

    # ---------- R13 trusted scope ----------

    def _assert_native_chat(self, response):
        self.assertEqual(response.status_code, 200)
        forwarded = StubUpstreamHandler.chat_bodies[-1]
        self.assertIs(forwarded["stream"], False)
        self.assertNotIn("stream_options", forwarded)

    def test_selected_key_model_protocol_aggregates(self):
        before = chat2api._reader_slots._value
        response = self._post("ok-sse", aggregate=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["message"]["content"],
                         "hello")
        forwarded = StubUpstreamHandler.chat_bodies[-1]
        self.assertIs(forwarded["stream"], True)
        self.assertEqual(forwarded["stream_options"],
                         {"include_usage": True})
        row = self._assert_one_aggregate_row("ok")
        self.assertEqual(row["model"], "ok-sse")
        self.wait_reader_slots_restored(before)

    def test_unselected_key_stays_native(self):
        # The marker is correctly signed but its key id is not selected.
        response = self._post("ok-sse", aggregate=True,
                             key_id=UNSELECTED_KEY_ID)
        self._assert_native_chat(response)

    def test_unselected_model_stays_native(self):
        response = self._post("native-model", aggregate=True,
                             scope_models=("ok-sse",))
        self._assert_native_chat(response)

    def test_unselected_protocol_stays_native(self):
        response = self._post("native-model", aggregate=True,
                             protocols=("messages",))
        self._assert_native_chat(response)

    def test_missing_scope_stays_native(self):
        response = self._post("native-model", aggregate=True,
                             install_scope=False)
        self._assert_native_chat(response)

    def test_direct_request_without_attribution_stays_native(self):
        # Direct 8110: no marker, hence no verified key id.
        response = self._post("native-model", aggregate=True,
                             sign_headers=False)
        self._assert_native_chat(response)

    def test_forged_attribution_stays_native(self):
        # A client-supplied marker with a bad signature is not an identity.
        response = self._post("native-model", aggregate=True,
                             tamper_signature=True)
        self._assert_native_chat(response)

    def test_client_header_cannot_widen_the_scope(self):
        # The scope is read from the startup environment only; a request
        # header carrying the same name is not configuration and cannot turn
        # aggregation on for a direct 8110 request.
        response = self._post(
            "native-model", aggregate=True, install_scope=False,
            sign_headers=False,
            extra_headers={chat2api.NONSTREAM_AGGREGATE_SCOPE_ENV: json.dumps({
                "key_ids": [ATTRIBUTION_KEY_ID],
                "models": ["native-model"],
                "protocols": ["chat"],
            })})
        self._assert_native_chat(response)

    # ---------- aggregate path ----------

    def test_success_sse_returns_one_complete_json(self):
        before = chat2api._reader_slots._value
        response = self._post("ok-sse", aggregate=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/json",
                      response.headers.get("Content-Type", ""))
        payload = response.json()
        self.assertEqual(payload["id"], "chatcmpl-n1")
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["created"], 1234567890)
        self.assertEqual(payload["model"], "chat-model")
        choice = payload["choices"][0]
        self.assertEqual(choice["index"], 0)
        self.assertEqual(choice["message"]["role"], "assistant")
        self.assertEqual(choice["message"]["content"], "hello")
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertEqual(payload["usage"]["prompt_tokens"], 3)
        self.assertEqual(payload["usage"]["completion_tokens"], 2)
        forwarded = StubUpstreamHandler.chat_bodies[-1]
        self.assertIs(forwarded["stream"], True)
        self.assertEqual(forwarded["stream_options"],
                         {"include_usage": True})
        row = self._assert_one_aggregate_row("ok")
        self.assertEqual(row["model"], "ok-sse")
        self.assertEqual(row["input_tokens"], 3)
        self.assertEqual(row["output_tokens"], 2)
        self.wait_reader_slots_restored(before)

    def test_aggregate_504_keeps_the_non_stream_error_mapping(self):
        response = self._post("native-504", aggregate=True)
        self.assertEqual(response.status_code, 502)
        error = response.json()["error"]
        self.assertIn("HTTP 504", error["detail"])
        forwarded = StubUpstreamHandler.chat_bodies[-1]
        self.assertIs(forwarded["stream"], True)
        self.assertEqual(forwarded["stream_options"],
                         {"include_usage": True})
        self._assert_no_ledger_rows()

    def test_explicit_error_is_a_502_not_partial_json(self):
        before = chat2api._reader_slots._value
        response = self._post("explicit-error", aggregate=True)
        self.assertEqual(response.status_code, 502)
        error = response.json()["error"]
        self.assertEqual(error["type"], "upstream_explicit_error")
        self.assertEqual(error["detail"]["reason"],
                         "upstream_explicit_error")
        self.assertEqual(error["detail"]["upstream_message"], "boom")
        row = self._assert_one_aggregate_row(
            "aggregate_upstream_explicit_error")
        # No usage block arrived: unknown, never a fabricated zero. The partial
        # content is the lower-bound output estimate.
        self.assertEqual(row["usage_source"], "estimated")
        self.assertIs(row["estimated"], True)
        self.assertEqual(row["output_tokens"], 1)
        self.wait_reader_slots_restored(before)

    def test_failure_row_keeps_the_reported_usage(self):
        # A usage block arrived before the malformed frame: the ONE failure row
        # uses the aggregator's reported usage, not a local estimate.
        response = self._post("usage-then-bad-frame", aggregate=True)
        self.assertEqual(response.status_code, 502)
        row = self._assert_one_aggregate_row("aggregate_upstream_bad_frame")
        self.assertEqual(row["input_tokens"], 8)
        self.assertEqual(row["output_tokens"], 2)
        self.assertEqual(row["usage_source"], "upstream")
        self.assertIs(row["estimated"], False)

    def test_mid_stream_disconnect_is_a_502_not_partial_json(self):
        baseline = self.reader_thread_count()
        before = chat2api._reader_slots._value
        response = self._post("mid-stream-disconnect", aggregate=True)
        self.assertEqual(response.status_code, 502)
        error = response.json()["error"]
        self.assertEqual(error["detail"]["reason"], "upstream_read_error")
        row = self._assert_one_aggregate_row("aggregate_upstream_read_error")
        self.assertEqual(row["usage_source"], "estimated")
        self.wait_reader_threads_reclaimed(baseline)
        self.wait_reader_slots_restored(before)

    def test_bad_frame_is_a_502_not_partial_json(self):
        before = chat2api._reader_slots._value
        response = self._post("bad-frame", aggregate=True)
        self.assertEqual(response.status_code, 502)
        error = response.json()["error"]
        self.assertEqual(error["detail"]["reason"], "upstream_bad_frame")
        self._assert_one_aggregate_row("aggregate_upstream_bad_frame")
        self.wait_reader_slots_restored(before)

    def test_no_terminal_credentials_is_a_502_not_partial_json(self):
        # Q12=A control case: neither [DONE] nor finish_reason is present.
        before = chat2api._reader_slots._value
        response = self._post("missing-terminal", aggregate=True)
        self.assertEqual(response.status_code, 502)
        error = response.json()["error"]
        self.assertEqual(error["detail"]["reason"],
                         "upstream_missing_terminal")
        self._assert_one_aggregate_row("aggregate_upstream_missing_terminal")
        self.wait_reader_slots_restored(before)

    def test_client_cancel_stops_and_records_client_disconnected(self):
        baseline = self.reader_thread_count()
        before = chat2api._reader_slots._value
        body = json.dumps({"model": "client-cancel",
                           "messages": [{"role": "user", "content": "hi"}],
                           "stream": False, "max_tokens": 16})
        request = raw_http_request(self.server, "/v1/chat/completions",
                                    body, marker_headers())
        with mock.patch.object(chat2api, "NONSTREAM_UPSTREAM_AGGREGATE",
                               True), \
                mock.patch.object(chat2api, "NONSTREAM_AGGREGATE_SCOPE",
                                  aggregate_scope(models=("client-cancel",),
                                                 protocols=("chat",))):
            sock = socket.create_connection(
                ("127.0.0.1", self.server.server_port))
            sock.sendall(request)
            deadline = time.monotonic() + 5.0
            while (StubUpstreamHandler.posts == 0
                   and time.monotonic() < deadline):
                time.sleep(0.02)
            self.assertGreater(StubUpstreamHandler.posts, 0)
            time.sleep(0.2)  # let the aggregate loop consume the first chunk
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()
            row = self._ledger_rows()[0]
        self.assertEqual(row["status"], "client_disconnected")
        self.assertIs(row["stream"], False)
        self.wait_reader_threads_reclaimed(baseline)
        self.wait_reader_slots_restored(before)

    def test_usage_tail_missing_stays_unknown(self):
        before = chat2api._reader_slots._value
        response = self._post("usage-missing", aggregate=True)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["choices"][0]["message"]["content"], "hi")
        self.assertNotIn("usage", payload)
        row = self._ledger_rows()[0]
        self.assertIs(row["stream"], False)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["usage_source"], "estimated")
        self.assertIs(row["estimated"], True)
        self.wait_reader_slots_restored(before)

    def test_usage_only_tail_block_is_merged(self):
        # include_usage shape: the finish frame has no usage, the final block
        # has choices: [] plus the usage tail. The tail must reach the JSON
        # body and the ledger, never be dropped as an "empty" frame.
        before = chat2api._reader_slots._value
        response = self._post("usage-only-tail", aggregate=True)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["choices"][0]["message"]["content"], "hi")
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")
        self.assertEqual(payload["usage"]["prompt_tokens"], 4)
        self.assertEqual(payload["usage"]["completion_tokens"], 2)
        self.assertEqual(payload["usage"]["total_tokens"], 6)
        self.assertEqual(
            payload["usage"]["prompt_tokens_details"]["cached_tokens"], 1)
        row = self._assert_one_aggregate_row("ok")
        self.assertEqual(row["input_tokens"], 4)
        self.assertEqual(row["output_tokens"], 2)
        self.assertEqual(row["usage_source"], "upstream")
        self.wait_reader_slots_restored(before)

    def test_fragmented_utf8_at_http_boundary(self):
        # The fake upstream splits one SSE line at arbitrary byte offsets,
        # including mid-character; the producer must reassemble the line before
        # decoding so no multibyte character is mangled.
        before = chat2api._reader_slots._value
        response = self._post("fragmented-utf8", aggregate=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["message"]["content"],
                         "你好世界")
        self._assert_one_aggregate_row("ok")
        self.wait_reader_slots_restored(before)

    def test_done_without_finish_reason_is_a_502(self):
        # Q12=A: a real [DONE] alone is NOT a success. The failure is
        # explicit, keeps the upstream usage the stream reported, and records
        # exactly ONE failure row.
        before = chat2api._reader_slots._value
        response = self._post("done-no-finish", aggregate=True)
        self.assertEqual(response.status_code, 502)
        error = response.json()["error"]
        self.assertEqual(error["detail"]["reason"],
                         "upstream_missing_terminal")
        row = self._assert_one_aggregate_row(
            "aggregate_upstream_missing_terminal")
        self.assertEqual(row["input_tokens"], 3)
        self.assertEqual(row["output_tokens"], 2)
        self.assertEqual(row["usage_source"], "upstream")
        self.assertIs(row["estimated"], False)
        self.wait_reader_slots_restored(before)

    def test_finish_reason_without_done_is_a_502(self):
        # Q12=A: a non-empty finish_reason followed by plain EOF is NOT a
        # success either; the real [DONE] sentinel is required too.
        before = chat2api._reader_slots._value
        response = self._post("finish-no-done", aggregate=True)
        self.assertEqual(response.status_code, 502)
        error = response.json()["error"]
        self.assertEqual(error["detail"]["reason"],
                         "upstream_missing_terminal")
        row = self._assert_one_aggregate_row(
            "aggregate_upstream_missing_terminal")
        self.assertEqual(row["input_tokens"], 3)
        self.assertEqual(row["output_tokens"], 2)
        self.assertEqual(row["usage_source"], "upstream")
        self.assertIs(row["estimated"], False)
        self.wait_reader_slots_restored(before)

    def test_both_terminals_return_one_json(self):
        # Q12=A positive control: finish_reason AND [DONE] both present.
        before = chat2api._reader_slots._value
        response = self._post("ok-sse", aggregate=True)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")
        self.assertEqual(payload["choices"][0]["message"]["content"],
                         "hello")
        row = self._assert_one_aggregate_row("ok")
        self.assertEqual(row["usage_source"], "upstream")
        self.assertIs(row["estimated"], False)
        self.wait_reader_slots_restored(before)

    def test_stream_options_400_is_retried_without_the_option(self):
        # Pre-existing compatibility retry: the aggregate request carries
        # stream_options, the upstream rejects it with a plain 400, and the
        # request is re-sent once without the option.
        before = chat2api._reader_slots._value
        response = self._post("reject-stream-options", aggregate=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["message"]["content"],
                         "hello")
        self.assertEqual(StubUpstreamHandler.posts, 2)
        self.assertIn("stream_options", StubUpstreamHandler.chat_bodies[0])
        self.assertNotIn("stream_options", StubUpstreamHandler.chat_bodies[1])
        self.assertIs(StubUpstreamHandler.chat_bodies[1]["stream"], True)
        self._assert_one_aggregate_row("ok")
        self.wait_reader_slots_restored(before)

    def test_401_is_reauthentication_without_an_aggregate_row(self):
        # A 401 is an authentication failure raised before the aggregator
        # exists: it keeps the existing 503 mapping and writes no usage row.
        before = chat2api._reader_slots._value
        response = self._post("auth-401", aggregate=True)
        self.assertEqual(response.status_code, 503)
        error = response.json()["error"]
        self.assertEqual(error["type"], "reauthentication_required")
        self._assert_no_ledger_rows()
        self.wait_reader_slots_restored(before)

    def test_legacy_function_call_over_http(self):
        response = self._post("legacy-function-call", aggregate=True)
        self.assertEqual(response.status_code, 200)
        choice = response.json()["choices"][0]
        self.assertEqual(choice["finish_reason"], "function_call")
        tool_calls = choice["message"]["tool_calls"]
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["type"], "function")
        self.assertEqual(tool_calls[0]["function"]["name"], "get_time")
        self.assertEqual(tool_calls[0]["function"]["arguments"],
                         '{"zone":"UTC"}')
        self.assertNotIn("id", tool_calls[0])
        self._assert_one_aggregate_row("ok")

    def test_auxiliary_fields_over_http(self):
        response = self._post("aux-fields", aggregate=True)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["system_fingerprint"], "fp_n1")
        self.assertEqual(payload["service_tier"], "default")
        choice = payload["choices"][0]
        message = choice["message"]
        self.assertEqual(message["content"], "answer")
        self.assertEqual(message["refusal"], "no thanks")
        self.assertEqual(message["annotations"], [
            {"type": "url_citation", "url": "https://a"},
            {"type": "url_citation", "url": "https://b"},
        ])
        self.assertEqual(message["audio"], {
            "id": "audio_1", "data": "QUJDREVG",
            "transcript": "hello", "expires_at": 1000,
        })
        self.assertEqual(choice["logprobs"]["content"],
                         [{"token": "he"}, {"token": "llo"}])
        self.assertNotIn("no thanks", message["content"])
        self._assert_one_aggregate_row("ok")

    def test_share_profile_keeps_reasoning_in_aggregate(self):
        # D18-5: the share profile keeps upstream fields exactly like the
        # private chain. The aggregate JSON keeps both reasoning channels and
        # never folds them into content.
        before = chat2api._reader_slots._value
        response = self._post("reasoning", aggregate=True)
        self.assertEqual(response.status_code, 200)
        message = response.json()["choices"][0]["message"]
        self.assertEqual(message["content"], "answer")
        self.assertEqual(message["reasoning"], "think-a")
        self.assertEqual(message["reasoning_content"], "think-b")
        self.assertNotIn("think-a", message["content"])
        self.assertNotIn("think-b", message["content"])
        self._assert_one_aggregate_row("ok")
        self.wait_reader_slots_restored(before)

    def test_share_profile_keeps_reasoning_in_client_stream(self):
        # D18-5: a client that asked for SSE receives the upstream reasoning
        # increments unchanged - the Qoder-only filter is gone.
        before = chat2api._reader_slots._value
        response = self._post("reasoning", aggregate=False, stream=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["Content-Type"])
        self.assertEqual(response.text.count("data: [DONE]"), 1)
        deltas = self._sse_deltas(response.text)
        self.assertTrue(deltas)
        self.assertIn({"reasoning": "think-a"}, deltas)
        self.assertIn({"reasoning_content": "think-b"}, deltas)
        self.assertEqual(
            "".join(delta.get("content") or "" for delta in deltas), "answer")
        self.wait_reader_slots_restored(before)

    @staticmethod
    def _sse_deltas(text):
        """Every relayed data event's delta, [DONE] excluded."""
        deltas = []
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                continue
            deltas.append(json.loads(payload)["choices"][0]["delta"])
        return deltas

    def test_tool_arguments_across_frames_and_parallel_indices(self):
        response = self._post("tool-args", aggregate=True)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        choice = payload["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        tool_calls = choice["message"]["tool_calls"]
        self.assertEqual([call["id"] for call in tool_calls], ["c1", "c2"])
        self.assertEqual(tool_calls[0]["id"], "c1")
        self.assertEqual(tool_calls[0]["type"], "function")
        self.assertEqual(tool_calls[0]["function"]["name"], "get_time")
        self.assertEqual(tool_calls[0]["function"]["arguments"],
                         '{"zone":"UTC"}')
        self.assertEqual(tool_calls[1]["id"], "c2")
        self.assertEqual(tool_calls[1]["function"]["name"], "echo")
        self.assertEqual(tool_calls[1]["function"]["arguments"],
                         '{"text":"hi"}')
        row = self._ledger_rows()[0]
        self.assertEqual(row["status"], "ok")

    def test_reasoning_is_separate_from_content(self):
        response = self._post("reasoning", aggregate=True)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        message = payload["choices"][0]["message"]
        self.assertEqual(message["content"], "answer")
        self.assertEqual(message["reasoning"], "think-a")
        self.assertEqual(message["reasoning_content"], "think-b")
        self.assertNotIn("think-a", message["content"])
        self.assertNotIn("think-b", message["content"])

    def test_one_megabyte_output_is_not_truncated(self):
        before = chat2api._reader_slots._value
        response = self._post("large-output", aggregate=True, timeout=60)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        self.assertEqual(len(content), 1024 * 1024)
        self.assertEqual(content, "x" * (1024 * 1024))
        self.wait_reader_slots_restored(before)

    def test_slow_downstream_releases_the_reader_slot_before_the_body(self):
        # R14: a slow client must not pin the reader slot. The producer owns
        # the slot and returns it at upstream EOF; the downstream write of the
        # 256 KiB body is still pending while this test has read NOTHING.
        baseline = self.reader_thread_count()
        before = chat2api._reader_slots._value
        body = json.dumps({"model": "slow-downstream",
                           "messages": [{"role": "user", "content": "hi"}],
                           "stream": False, "max_tokens": 16})
        request = raw_http_request(self.server, "/v1/chat/completions",
                                    body, marker_headers())
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8192)
            sock.settimeout(120)
            sock.connect(("127.0.0.1", self.server.server_port))
            with mock.patch.object(chat2api, "NONSTREAM_UPSTREAM_AGGREGATE",
                                    True), \
                    mock.patch.object(
                        chat2api, "NONSTREAM_AGGREGATE_SCOPE",
                        aggregate_scope(models=("slow-downstream",),
                                        protocols=("chat",))):
                sock.sendall(request)
                self.assertTrue(
                    StubUpstreamHandler.slow_done.wait(30),
                    "the fake upstream did not finish its 256 KiB answer")
                # The upstream is fully sent, so the producer returned the
                # slot even though the downstream body is still unread.
                deadline = time.monotonic() + 10
                while (chat2api._reader_slots._value < before
                       and time.monotonic() < deadline):
                    time.sleep(0.02)
                self.assertEqual(chat2api._reader_slots._value, before)
                response = http.client.HTTPResponse(sock)
                response.begin()
                self.assertEqual(response.status, 200)
                raw = response.read()
                payload = json.loads(raw)
                content = payload["choices"][0]["message"]["content"]
                self.assertEqual(len(content), 64 * 4096)
                self.assertEqual(content, "d" * 4096 * 64)
                self.assertEqual(payload["choices"][0]["finish_reason"], "stop")
                row = self._assert_one_aggregate_row("ok")
                self.assertEqual(row["output_tokens"], 2)
        finally:
            sock.close()
        self.wait_reader_threads_reclaimed(baseline)
        self.wait_reader_slots_restored(before)

    def test_reader_slot_is_returned_after_aggregate(self):
        before = chat2api._reader_slots._value
        response = self._post("ok-sse", aggregate=True)
        self.assertEqual(response.status_code, 200)
        self.wait_reader_slots_restored(before)

    def test_reader_slot_cap_rejects_aggregate_without_upstream_call(self):
        posts_before = StubUpstreamHandler.posts
        with mock.patch.object(chat2api, "_reader_slots",
                                threading.BoundedSemaphore(1)) as slots:
            self.assertTrue(slots.acquire(blocking=False))
            try:
                response = self._post("ok-sse", aggregate=True)
            finally:
                slots.release()
        self.assertEqual(response.status_code, 503)
        error = response.json()["error"]
        self.assertEqual(error["type"], "too_many_concurrent_streams")
        self.assertEqual(StubUpstreamHandler.posts, posts_before)

    def test_aggregate_path_does_not_write_the_production_ledger(self):
        # The harness pins the sink: the private temp ledger is the module's
        # USAGE_LOG, the request's row lands there, and the production path
        # is never the module's sink (a test handler writing it would be
        # refused by _UsageRouter, so a production write cannot pass silently).
        production = self._production_ledger()
        redirected = os.path.normcase(os.path.abspath(chat2api.USAGE_LOG))
        self.assertNotEqual(redirected, os.path.normcase(str(production)))
        self.assertTrue(redirected.startswith(
            os.path.normcase(str(self._usage_dir))))
        response = self._post("ok-sse", aggregate=True)
        self.assertEqual(response.status_code, 200)
        row = self._ledger_rows()[0]
        self.assertEqual(row["status"], "ok")
        self.assertIs(row["stream"], False)
        self.assertEqual(os.path.normcase(os.path.abspath(chat2api.USAGE_LOG)),
                         redirected)


# ---------------------------------------------------------------------------
# N2b: Messages and Responses served from an SSE upstream
# ---------------------------------------------------------------------------
# The same fake UM boundary carries both upstream protocols: Anthropic
# Messages events for /api/v1/messages and Chat chunks for
# /api/chat/completions. The fake decides only the wire bytes; the client's
# request mode is asserted from the forwarded body, and the ledger row's
# ``stream`` field must stay the CLIENT's own mode (False).

PROTOCOL_STUB_TOKEN = "stub-token"


def _event(event):
    """One Anthropic SSE event: its event: line plus its JSON data line."""
    return ("event: " + event["type"] + "\n"
            "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            ).encode("utf-8")


def _message_start(model="claude-test", message_id="msg_1", usage=None,
                     content=None):
    message = {"id": message_id, "type": "message", "role": "assistant",
               "model": model,
               "content": [] if content is None else content,
               "stop_reason": None, "stop_sequence": None}
    if usage is not None:
        message["usage"] = usage
    return _event({"type": "message_start", "message": message})


def _block_start(index, block):
    return _event({"type": "content_block_start", "index": index,
                   "content_block": block})


def _block_delta(index, delta):
    return _event({"type": "content_block_delta", "index": index,
                   "delta": delta})


def _block_stop(index):
    return _event({"type": "content_block_stop", "index": index})


def _message_delta(stop_reason="end_turn", usage=None):
    payload = {"type": "message_delta",
               "delta": {"stop_reason": stop_reason,
                          "stop_sequence": None}}
    if usage is not None:
        payload["usage"] = usage
    return _event(payload)


def _message_stop():
    return _event({"type": "message_stop"})


def _ping():
    return _event({"type": "ping"})


def _anthropic_text_frames(model="claude-test"):
    return [
        _message_start(model=model,
                        usage={"input_tokens": 5, "output_tokens": 1}),
        _block_start(0, {"type": "text", "text": ""}),
        _block_delta(0, {"type": "text_delta", "text": "hel"}),
        _block_delta(0, {"type": "text_delta", "text": "lo"}),
        _block_stop(0),
        _message_delta(usage={"output_tokens": 2}),
        _message_stop(),
    ]


def _anthropic_usage_tail_frames():
    return [
        _message_start(usage={"input_tokens": 9, "cache_read_input_tokens": 3,
                               "output_tokens": 1}),
        _block_start(0, {"type": "text", "text": ""}),
        _block_delta(0, {"type": "text_delta", "text": "hi"}),
        _block_stop(0),
        _message_delta(usage={"output_tokens": 5}),
        _message_stop(),
    ]


def _anthropic_tool_frames():
    return [
        _message_start(usage={"input_tokens": 4, "output_tokens": 1}),
        _block_start(0, {"type": "tool_use", "id": "toolu_1",
                          "name": "get_time", "input": {}}),
        _block_delta(0, {"type": "input_json_delta",
                          "partial_json": '{"zone":'}),
        _block_delta(0, {"type": "input_json_delta",
                          "partial_json": '"UTC"}'}),
        _block_stop(0),
        _message_delta(stop_reason="tool_use", usage={"output_tokens": 3}),
        _message_stop(),
    ]


def _anthropic_thinking_frames():
    return [
        _message_start(usage={"input_tokens": 4, "output_tokens": 1}),
        _block_start(0, {"type": "thinking", "thinking": ""}),
        _block_delta(0, {"type": "thinking_delta", "thinking": "think-a"}),
        _block_delta(0, {"type": "signature_delta", "signature": "sig-1"}),
        _block_stop(0),
        _block_start(1, {"type": "text", "text": ""}),
        _block_delta(1, {"type": "text_delta", "text": "answer"}),
        _block_stop(1),
        _message_delta(usage={"output_tokens": 3}),
        _message_stop(),
    ]


def _anthropic_explicit_error_frames():
    return [
        _message_start(usage={"input_tokens": 4, "output_tokens": 1}),
        _block_start(0, {"type": "text", "text": ""}),
        _block_delta(0, {"type": "text_delta", "text": "partial"}),
        _event({"type": "error", "error": {"type": "overloaded_error",
                                           "message": "boom"}}),
    ]


def _anthropic_partial_frames():
    return [
        _message_start(usage={"input_tokens": 4, "output_tokens": 1}),
        _block_start(0, {"type": "text", "text": ""}),
        _block_delta(0, {"type": "text_delta", "text": "partial"}),
    ]


def _anthropic_missing_stop_frames():
    return [
        _message_start(usage={"input_tokens": 4, "output_tokens": 1}),
        _block_start(0, {"type": "text", "text": ""}),
        _block_delta(0, {"type": "text_delta", "text": "partial"}),
        _block_stop(0),
        _message_delta(usage={"output_tokens": 2}),
    ]


def _anthropic_bad_frame_frames():
    return [
        _message_start(usage={"input_tokens": 4, "output_tokens": 1}),
        _block_start(0, {"type": "text", "text": ""}),
        _block_delta(0, {"type": "text_delta", "text": "partial"}),
        b"data: {not json\n\n",
    ]


def _anthropic_no_usage_frames():
    return [
        _message_start(),
        _block_start(0, {"type": "text", "text": ""}),
        _block_delta(0, {"type": "text_delta", "text": "hi"}),
        _block_stop(0),
        _message_stop(),
    ]


def _anthropic_native_message():
    return {"id": "msg_native", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 2, "output_tokens": 1}}


class ProtocolStubUpstreamHandler(BaseHTTPRequestHandler):
    """Fake UM for the Messages and the Chat upstream surfaces."""

    protocol_version = "HTTP/1.1"
    posts = []
    bodies = []

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

    def _send_chunk(self, data):
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii")
                         + data + b"\r\n")
        self.wfile.flush()

    def _send_headers(self, chunked=True):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _stream(self, frames):
        self._send_headers(chunked=True)
        try:
            for frame in frames:
                self._send_chunk(frame)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _stream_partial(self, frames):
        # No terminating chunk: the upstream dies mid-body.
        self._send_headers(chunked=True)
        try:
            for frame in frames:
                self._send_chunk(frame)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _stream_clean_eof(self, frames):
        # A clean EOF with no terminating event (Connection: close).
        self._send_headers(chunked=False)
        try:
            for frame in frames:
                self.wfile.write(frame)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _stream_trickle(self, frames, keepalive=b": keepalive\n\n"):
        self._send_headers(chunked=True)
        try:
            for frame in frames:
                self._send_chunk(frame)
            for _ in range(80):
                self._send_chunk(keepalive)
                time.sleep(0.05)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        ProtocolStubUpstreamHandler.posts.append(path)
        ProtocolStubUpstreamHandler.bodies.append(body)
        model = body.get("model", "")
        if path == "/api/chat/completions":
            self._chat(model, body)
        elif path == "/api/v1/messages":
            self._messages(model)
        else:
            self._send_json(404, {"detail": "not found"})

    def _chat(self, model, body):
        if model == "native-504":
            self._send_json(504, {"detail": "gateway timeout"})
            return
        if not body.get("stream"):
            self._send_json(200, DEFAULT_CHAT_JSON)
            return
        if model == "resp-chat-ok":
            self._stream(_ok_frames())
        elif model == "resp-chat-bad-frame":
            self._stream([_sse(_chunk(content="partial")),
                           _sse("{not json"), _sse("[DONE]")])
        elif model == "resp-chat-done-only":
            # Q12=A: a real [DONE] with no finish_reason anywhere.
            self._stream([_sse(_chunk(content="partial")),
                          _sse("[DONE]")])
        elif model == "resp-chat-reason-only":
            # Q12=A: a non-empty finish_reason followed by plain EOF.
            self._stream([_sse(_chunk(content="partial",
                                     finish_reason="stop"))])
        elif model == "resp-chat-no-terminal":
            # Q12=A control: neither [DONE] nor finish_reason.
            self._stream([_sse(_chunk(content="partial"))])
        elif model == "resp-chat-cancel":
            self._stream_trickle([_sse(_chunk(content="first"))])
        else:
            self._send_json(500, {"detail": f"unknown chat {model}"})

    def _messages(self, model):
        if model == "native-504":
            self._send_json(504, {"detail": "gateway timeout"})
            return
        if model.startswith("resp-msg-"):
            scenario = model[len("resp-msg-"):]
        elif model.startswith("msg-"):
            scenario = model[len("msg-"):]
        else:
            scenario = model
        if scenario == "ok":
            self._stream(_anthropic_text_frames())
        elif scenario == "usage-tail":
            self._stream(_anthropic_usage_tail_frames())
        elif scenario == "tool-json":
            self._stream(_anthropic_tool_frames())
        elif scenario == "thinking":
            self._stream(_anthropic_thinking_frames())
        elif scenario == "explicit-error":
            self._stream(_anthropic_explicit_error_frames())
        elif scenario == "mid-stream":
            self._stream_partial(_anthropic_partial_frames())
        elif scenario == "missing-stop":
            self._stream_clean_eof(_anthropic_missing_stop_frames())
        elif scenario == "bad-frame":
            self._stream(_anthropic_bad_frame_frames())
        elif scenario == "cancel":
            self._stream_trickle(_anthropic_partial_frames()[:1],
                                  keepalive=_ping())
        elif scenario == "no-usage":
            self._stream(_anthropic_no_usage_frames())
        elif scenario == "native":
            self._send_json(200, _anthropic_native_message())
        else:
            self._send_json(500, {"detail": f"unknown messages {model}"})


class TestAnthropicAggregateUnit(unittest.TestCase):
    """The pure Anthropic Messages SSE state machine."""

    def _aggregator(self, model="claude-test"):
        return nonstream_aggregate.AnthropicMessageAggregator(
            default_model=model)

    @staticmethod
    def _feed(aggregator, frames):
        for frame in frames:
            aggregator.feed_bytes(frame)

    def test_text_and_usage_tail_aggregate_into_one_message(self):
        aggregator = self._aggregator()
        self._feed(aggregator, _anthropic_text_frames())
        message = aggregator.finish()
        self.assertEqual(message["type"], "message")
        self.assertEqual(message["id"], "msg_1")
        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["model"], "claude-test")
        self.assertEqual(message["stop_reason"], "end_turn")
        self.assertIsNone(message["stop_sequence"])
        self.assertEqual(message["content"],
                         [{"type": "text", "text": "hello"}])
        self.assertEqual(message["usage"],
                         {"input_tokens": 5, "output_tokens": 2})

    def test_message_stop_alone_completes_the_message(self):
        # Messages keeps its own terminal rule: message_stop alone completes a
        # message (with stop_reason None). The Chat Q12=A two-credential rule
        # does not leak into Anthropic's protocol.
        aggregator = self._aggregator()
        self._feed(aggregator, [
            _message_start(usage={"input_tokens": 5}),
            _block_start(0, {"type": "text", "text": ""}),
            _block_delta(0, {"type": "text_delta", "text": "hi"}),
            _block_stop(0),
            _message_stop(),
        ])
        message = aggregator.finish()
        self.assertEqual(message["content"],
                         [{"type": "text", "text": "hi"}])
        self.assertIsNone(message["stop_reason"])

    def test_tool_use_input_json_joins_across_frames(self):
        aggregator = self._aggregator()
        self._feed(aggregator, _anthropic_tool_frames())
        message = aggregator.finish()
        block = message["content"][0]
        self.assertEqual(block["type"], "tool_use")
        self.assertEqual(block["id"], "toolu_1")
        self.assertEqual(block["name"], "get_time")
        self.assertEqual(block["input"], {"zone": "UTC"})
        self.assertEqual(message["stop_reason"], "tool_use")

    def test_thinking_and_signature_stay_separate_from_text(self):
        aggregator = self._aggregator()
        self._feed(aggregator, _anthropic_thinking_frames())
        content = aggregator.finish()["content"]
        self.assertEqual(content[0]["type"], "thinking")
        self.assertEqual(content[0]["thinking"], "think-a")
        self.assertEqual(content[0]["signature"], "sig-1")
        self.assertEqual(content[1]["type"], "text")
        self.assertEqual(content[1]["text"], "answer")
        self.assertNotIn("think-a", content[1]["text"])
        self.assertNotIn("answer", content[0]["thinking"])

    def test_missing_message_stop_is_a_failure(self):
        aggregator = self._aggregator()
        self._feed(aggregator, _anthropic_missing_stop_frames())
        with self.assertRaises(nonstream_aggregate.AggregateError) as ctx:
            aggregator.finish()
        self.assertEqual(ctx.exception.reason, "upstream_missing_terminal")

    def test_explicit_error_is_a_failure(self):
        aggregator = self._aggregator()
        self._feed(aggregator, _anthropic_explicit_error_frames()[:3])
        with self.assertRaises(nonstream_aggregate.AggregateError) as ctx:
            self._feed(aggregator, _anthropic_explicit_error_frames()[3:])
        self.assertEqual(ctx.exception.reason, "upstream_explicit_error")
        self.assertEqual(ctx.exception.message, "boom")

    def test_bad_frame_is_a_failure(self):
        aggregator = self._aggregator()
        self._feed(aggregator, _anthropic_bad_frame_frames()[:3])
        with self.assertRaises(nonstream_aggregate.AggregateError) as ctx:
            self._feed(aggregator, _anthropic_bad_frame_frames()[3:])
        self.assertEqual(ctx.exception.reason, "upstream_bad_frame")

    def test_unknown_event_type_is_a_failure(self):
        aggregator = self._aggregator()
        with self.assertRaises(nonstream_aggregate.AggregateError) as ctx:
            aggregator.feed_bytes(_event({"type": "future_event"}))
        self.assertEqual(ctx.exception.reason, "upstream_bad_frame")

    def test_done_sentinel_is_not_an_anthropic_terminal(self):
        aggregator = self._aggregator()
        with self.assertRaises(nonstream_aggregate.AggregateError) as ctx:
            aggregator.feed_bytes(b"data: [DONE]\n\n")
        self.assertEqual(ctx.exception.reason, "upstream_bad_frame")

    def test_ping_is_ignored(self):
        aggregator = self._aggregator()
        self._feed(aggregator, [_ping()] + _anthropic_text_frames())
        self.assertEqual(aggregator.finish()["content"],
                         [{"type": "text", "text": "hello"}])

    def test_open_block_at_message_stop_is_a_failure(self):
        frames = [
            _message_start(usage={"input_tokens": 4}),
            _block_start(0, {"type": "text", "text": ""}),
            _block_delta(0, {"type": "text_delta", "text": "partial"}),
            _message_stop(),
        ]
        aggregator = self._aggregator()
        self._feed(aggregator, frames)
        with self.assertRaises(nonstream_aggregate.AggregateError) as ctx:
            aggregator.finish()
        self.assertEqual(ctx.exception.reason, "upstream_bad_frame")

    def test_usage_placeholder_output_tokens_is_not_the_final_count(self):
        frames = [
            _message_start(usage={"input_tokens": 5, "output_tokens": 1}),
            _block_start(0, {"type": "text", "text": ""}),
            _block_delta(0, {"type": "text_delta", "text": "hi"}),
            _block_stop(0),
            _message_delta(),
            _message_stop(),
        ]
        aggregator = self._aggregator()
        self._feed(aggregator, frames)
        self.assertEqual(aggregator.finish()["usage"], {"input_tokens": 5})

    def test_usage_null_does_not_erase_reported_counts(self):
        frames = [
            _message_start(usage={"input_tokens": 5,
                                    "cache_read_input_tokens": 2,
                                    "output_tokens": 1}),
            _block_start(0, {"type": "text", "text": ""}),
            _block_delta(0, {"type": "text_delta", "text": "hi"}),
            _block_stop(0),
            _message_delta(usage={"output_tokens": 3}),
            _message_delta(usage={"output_tokens": None,
                                    "input_tokens": None}),
            _message_stop(),
        ]
        aggregator = self._aggregator()
        self._feed(aggregator, frames)
        self.assertEqual(aggregator.finish()["usage"],
                         {"input_tokens": 5,
                          "cache_read_input_tokens": 2,
                          "output_tokens": 3})

    def test_usage_non_count_metadata_is_preserved(self):
        frames = [
            _message_start(usage={"input_tokens": 5, "output_tokens": 1,
                                    "service_tier": "standard",
                                    "cache_creation":
                                        {"ephemeral_5m_input_tokens": 2}}),
            _block_start(0, {"type": "text", "text": ""}),
            _block_delta(0, {"type": "text_delta", "text": "hi"}),
            _block_stop(0),
            _message_delta(usage={"output_tokens": 3}),
            _message_stop(),
        ]
        aggregator = self._aggregator()
        self._feed(aggregator, frames)
        self.assertEqual(aggregator.finish()["usage"],
                         {"input_tokens": 5,
                          "service_tier": "standard",
                          "cache_creation":
                              {"ephemeral_5m_input_tokens": 2},
                          "output_tokens": 3})

    def test_citations_delta_keeps_citations(self):
        frames = [
            _message_start(usage={"input_tokens": 4}),
            _block_start(0, {"type": "text", "text": ""}),
            _block_delta(0, {"type": "text_delta", "text": "hi"}),
            _block_delta(0, {"type": "citations_delta",
                              "citation": {"type": "page_location",
                                            "cited_text": "x"}}),
            _block_stop(0),
            _message_stop(),
        ]
        aggregator = self._aggregator()
        self._feed(aggregator, frames)
        block = aggregator.finish()["content"][0]
        self.assertEqual(block["text"], "hi")
        self.assertEqual(block["citations"],
                         [{"type": "page_location", "cited_text": "x"}])

    def test_unknown_block_type_is_preserved_verbatim(self):
        frames = [
            _message_start(usage={"input_tokens": 4}),
            _block_start(0, {"type": "redacted_thinking", "data": "xyz"}),
            _block_stop(0),
            _message_stop(),
        ]
        aggregator = self._aggregator()
        self._feed(aggregator, frames)
        self.assertEqual(aggregator.finish()["content"],
                         [{"type": "redacted_thinking", "data": "xyz"}])

    def test_crlf_multiline_data_and_fragmented_utf8(self):
        # Same SSE framing as the Chat aggregator: CRLF, multi-line data
        # joined with "\n", and a multibyte character split across byte
        # chunks must all be reassembled.
        wire = (
            b"event: message_start\r\n"
            b'data: {"type":"message_start","message":\r\n'
            b'data: {"id":"msg_1","type":"message","role":"assistant",'
            b'"model":"claude-test","content":[],"usage":'
            b'{"input_tokens":3}}}\r\n\r\n'
            b"event: content_block_start\r\n"
            b'data: {"type":"content_block_start","index":0,'
            b'"content_block":{"type":"text","text":""}}\r\n\r\n'
            b"event: content_block_delta\r\n"
            b'data: {"type":"content_block_delta","index":0,"delta":'
            b'{"type":"text_delta","text":"'
            + "你好".encode("utf-8") + b'"}}\r\n\r\n'
            b"event: content_block_stop\r\n"
            b'data: {"type":"content_block_stop","index":0}\r\n\r\n'
            b"event: message_stop\r\n"
            b'data: {"type":"message_stop"}\r\n\r\n'
        )
        aggregator = self._aggregator()
        for offset in range(0, len(wire), 3):
            aggregator.feed_bytes(wire[offset:offset + 3])
        message = aggregator.finish()
        self.assertEqual(message["content"][0]["text"], "你好")

    def test_finished_follows_message_stop(self):
        aggregator = self._aggregator()
        self._feed(aggregator, _anthropic_text_frames()[:-1])
        self.assertFalse(aggregator.finished)
        self._feed(aggregator, _anthropic_text_frames()[-1:])
        self.assertTrue(aggregator.finished)


class _LedgerAssertions:
    """One-row ledger assertions shared by the HTTP suites."""

    def _ledger_rows(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        rows = []
        while time.monotonic() < deadline:
            try:
                rows = [json.loads(line) for line in
                        Path(chat2api.USAGE_LOG).read_text(
                            encoding="utf-8").splitlines() if line.strip()]
            except FileNotFoundError:
                rows = []
            if rows:
                return rows
            time.sleep(0.02)
        self.fail(f"ledger rows not written within {timeout}s")

    def _assert_one_row(self, status):
        """Exactly ONE ledger row, the client's stream=False and the status."""
        self._ledger_rows()
        time.sleep(0.1)
        rows = [json.loads(line) for line in
                Path(chat2api.USAGE_LOG).read_text(
                    encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], status)
        self.assertIs(row["stream"], False)
        return row

    @staticmethod
    def reader_thread_count():
        return sum(1 for t in threading.enumerate()
                   if t.name == "chat2api-upstream-reader")

    def wait_reader_slots_restored(self, before, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if chat2api._reader_slots._value >= before:
                return
            time.sleep(0.05)
        self.fail(f"reader slots not restored within {timeout}s "
                  f"({chat2api._reader_slots._value} != {before})")

    def wait_reader_threads_reclaimed(self, baseline, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.reader_thread_count() <= baseline:
                return
            time.sleep(0.05)
        self.fail(f"reader threads not reclaimed within {timeout}s")


@unittest.skip("HMAC attribution retired with the merged share process")
class TestProtocolAggregateHttp(_LedgerAssertions, unittest.TestCase):
    """Real Handler -> loopback fake UM for Messages and Responses."""

    def setUp(self):
        self._usage_dir, self.usage_log = harness.install_usage_isolation(
            self, chat2api, ledger_name="usage-chat2api.jsonl")
        install_attribution_key(self)
        harness.assert_stub_credentials(
            chat2api, local_key=None, upstream_token=PROTOCOL_STUB_TOKEN,
            stub_token_literal=PROTOCOL_STUB_TOKEN)

        stub = harness.TrackedHTTPServer(("127.0.0.1", 0),
                                         ProtocolStubUpstreamHandler)
        self.stub = stub
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        harness.install_tracked_server(self, stub, name="protocol-upstream",
                                         require_drained=False)
        ProtocolStubUpstreamHandler.posts = []
        ProtocolStubUpstreamHandler.bodies = []

        args = SimpleNamespace(
            base_url=f"http://127.0.0.1:{stub.server_port}",
            token=PROTOCOL_STUB_TOKEN,
            profile="profile",
            login_timeout=1,
            use_api_key=False,
            no_browser=True,
        )
        proxy = chat2api.Proxy({"token": PROTOCOL_STUB_TOKEN}, args)
        server = harness.TrackedHTTPServer(("127.0.0.1", 0),
                                           chat2api.Handler)
        server.proxy = proxy
        self.server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        harness.install_tracked_server(self, server, name="chat2api-gateway")
        self.base = f"http://127.0.0.1:{server.server_port}"

        for patch in (
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", False),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 0),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRY", False),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRIES", 0),
            mock.patch.object(chat2api, "NONSTREAM_UPSTREAM_AGGREGATE", False),
        ):
            patch.start()
            self.addCleanup(patch.stop)

        chat2api._reader_slots = threading.BoundedSemaphore(
            chat2api.STREAM_MAX_CONCURRENT_READERS)

    # ---------- request helpers ----------

    def _post_messages(self, model, *, aggregate, stream=False, timeout=30,
                       key_id=ATTRIBUTION_KEY_ID, protocols=("messages",),
                       sign_headers=None, install_scope=None,
                       scope_models=None, scope_key_ids=(ATTRIBUTION_KEY_ID,),
                       tamper_signature=False):
        body = {"model": model, "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": stream}
        sign_headers = aggregate if sign_headers is None else sign_headers
        install_scope = aggregate if install_scope is None else install_scope
        headers = None
        if sign_headers:
            headers = (tampered_marker_headers(key_id)
                       if tamper_signature else marker_headers(key_id))
        scope = (aggregate_scope(
            models=(model,) if scope_models is None else scope_models,
            protocols=protocols, key_ids=scope_key_ids)
            if install_scope else None)
        with mock.patch.object(chat2api, "NONSTREAM_UPSTREAM_AGGREGATE",
                               aggregate), \
                mock.patch.object(chat2api, "NONSTREAM_AGGREGATE_SCOPE",
                                  scope):
            return requests.post(f"{self.base}/v1/messages", json=body,
                                  headers=headers, timeout=timeout)

    @staticmethod
    def _responses_body(model, *, stream=False, image_tool=False):
        if image_tool:
            return {"model": model, "stream": stream,
                    "max_output_tokens": 16,
                    "input": [
                        {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "hi"}]},
                        {"type": "function_call_output", "call_id": "call_1",
                         "output": [{"type": "input_image",
                                     "image_url":
                                         "data:image/png;base64,QUJD"}]}]}
        return {"model": model, "stream": stream, "max_output_tokens": 16,
                "input": [{"type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": "hi"}]}]}

    def _post_responses(self, model, *, aggregate, stream=False,
                          image_tool=False, timeout=30,
                          key_id=ATTRIBUTION_KEY_ID, protocols=("responses",),
                          sign_headers=None, install_scope=None,
                          scope_models=None,
                          scope_key_ids=(ATTRIBUTION_KEY_ID,),
                          tamper_signature=False):
        body = self._responses_body(model, stream=stream,
                                     image_tool=image_tool)
        sign_headers = aggregate if sign_headers is None else sign_headers
        install_scope = aggregate if install_scope is None else install_scope
        headers = None
        if sign_headers:
            headers = (tampered_marker_headers(key_id)
                       if tamper_signature else marker_headers(key_id))
        scope = (aggregate_scope(
            models=(model,) if scope_models is None else scope_models,
            protocols=protocols, key_ids=scope_key_ids)
            if install_scope else None)
        with mock.patch.object(chat2api, "NONSTREAM_UPSTREAM_AGGREGATE",
                               aggregate), \
                mock.patch.object(chat2api, "NONSTREAM_AGGREGATE_SCOPE",
                                  scope):
            return requests.post(f"{self.base}/v1/responses", json=body,
                                  headers=headers, timeout=timeout)

    # ---------- direct Messages ----------

    def test_messages_native_504_keeps_the_non_stream_mapping(self):
        response = self._post_messages("native-504", aggregate=False)
        self.assertEqual(response.status_code, 504)
        self.assertIn("application/json",
                      response.headers.get("Content-Type", ""))
        self.assertIs(ProtocolStubUpstreamHandler.bodies[-1]["stream"], False)
        row = self._ledger_rows()[0]
        self.assertEqual(row["status"], "HTTP504")
        self.assertIs(row["stream"], False)
        self.assertEqual(row["protocol"], "messages")

    # ---------- R13 trusted scope ----------

    def _assert_native_messages(self, response):
        self.assertEqual(response.status_code, 504)
        forwarded = ProtocolStubUpstreamHandler.bodies[-1]
        self.assertIs(forwarded["stream"], False)
        self.assertNotIn("stream_options", forwarded)

    def test_messages_selected_triple_aggregates(self):
        before = chat2api._reader_slots._value
        response = self._post_messages("msg-ok", aggregate=True)
        self.assertEqual(response.status_code, 200)
        forwarded = ProtocolStubUpstreamHandler.bodies[-1]
        self.assertIs(forwarded["stream"], True)
        self.assertNotIn("stream_options", forwarded)
        row = self._assert_one_row("ok")
        self.assertEqual(row["protocol"], "messages")
        self.assertEqual(row["model"], "msg-ok")
        self.wait_reader_slots_restored(before)

    def test_messages_unselected_protocol_stays_native(self):
        response = self._post_messages("native-504", aggregate=True,
                                       protocols=("chat",))
        self._assert_native_messages(response)

    def test_messages_unselected_key_stays_native(self):
        response = self._post_messages("native-504", aggregate=True,
                                       key_id=UNSELECTED_KEY_ID)
        self._assert_native_messages(response)

    def test_messages_unselected_model_stays_native(self):
        response = self._post_messages("native-504", aggregate=True,
                                       scope_models=("msg-ok",))
        self._assert_native_messages(response)

    def test_messages_missing_scope_stays_native(self):
        response = self._post_messages("native-504", aggregate=True,
                                       install_scope=False)
        self._assert_native_messages(response)

    def test_messages_direct_request_without_attribution_stays_native(self):
        response = self._post_messages("native-504", aggregate=True,
                                       sign_headers=False)
        self._assert_native_messages(response)

    def test_messages_aggregate_success_returns_one_json(self):
        before = chat2api._reader_slots._value
        response = self._post_messages("msg-ok", aggregate=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/json",
                      response.headers.get("Content-Type", ""))
        payload = response.json()
        self.assertEqual(payload["type"], "message")
        self.assertEqual(payload["id"], "msg_1")
        self.assertEqual(payload["model"], "claude-test")
        self.assertEqual(payload["stop_reason"], "end_turn")
        self.assertEqual(payload["content"],
                         [{"type": "text", "text": "hello"}])
        self.assertEqual(payload["usage"],
                         {"input_tokens": 5, "output_tokens": 2})
        forwarded = ProtocolStubUpstreamHandler.bodies[-1]
        self.assertIs(forwarded["stream"], True)
        self.assertNotIn("stream_options", forwarded)
        row = self._assert_one_row("ok")
        self.assertEqual(row["protocol"], "messages")
        self.assertEqual(row["model"], "msg-ok")
        self.assertEqual(row["input_tokens"], 5)
        self.assertEqual(row["output_tokens"], 2)
        self.assertEqual(row["usage_source"], "upstream")
        self.wait_reader_slots_restored(before)

    def test_messages_usage_tail_is_merged(self):
        before = chat2api._reader_slots._value
        response = self._post_messages("msg-usage-tail", aggregate=True)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["usage"],
                         {"input_tokens": 9,
                          "cache_read_input_tokens": 3,
                          "output_tokens": 5})
        row = self._assert_one_row("ok")
        self.assertEqual(row["input_tokens"], 12)
        self.assertEqual(row["output_tokens"], 5)
        self.assertEqual(row["cached_input_tokens"], 3)
        self.assertEqual(row["usage_source"], "upstream")
        self.wait_reader_slots_restored(before)

    def test_messages_tool_use_input_json_across_frames(self):
        before = chat2api._reader_slots._value
        response = self._post_messages("msg-tool-json", aggregate=True)
        self.assertEqual(response.status_code, 200)
        block = response.json()["content"][0]
        self.assertEqual(block["type"], "tool_use")
        self.assertEqual(block["id"], "toolu_1")
        self.assertEqual(block["name"], "get_time")
        self.assertEqual(block["input"], {"zone": "UTC"})
        self._assert_one_row("ok")
        self.wait_reader_slots_restored(before)

    def test_messages_thinking_stays_separate_from_text(self):
        before = chat2api._reader_slots._value
        response = self._post_messages("msg-thinking", aggregate=True)
        self.assertEqual(response.status_code, 200)
        content = response.json()["content"]
        self.assertEqual(content[0]["type"], "thinking")
        self.assertEqual(content[0]["thinking"], "think-a")
        self.assertEqual(content[0]["signature"], "sig-1")
        self.assertEqual(content[1]["type"], "text")
        self.assertEqual(content[1]["text"], "answer")
        self._assert_one_row("ok")
        self.wait_reader_slots_restored(before)

    def test_messages_explicit_error_is_a_502(self):
        before = chat2api._reader_slots._value
        response = self._post_messages("msg-explicit-error", aggregate=True)
        self.assertEqual(response.status_code, 502)
        self.assertIn("application/json",
                      response.headers.get("Content-Type", ""))
        detail = response.json()["error"]["detail"]
        self.assertEqual(detail["reason"], "upstream_explicit_error")
        self.assertEqual(detail["upstream_message"], "boom")
        self._assert_one_row("aggregate_upstream_explicit_error")
        self.wait_reader_slots_restored(before)

    def test_messages_mid_stream_disconnect_is_a_502(self):
        before = chat2api._reader_slots._value
        response = self._post_messages("msg-mid-stream", aggregate=True)
        self.assertEqual(response.status_code, 502)
        self.assertIn("application/json",
                      response.headers.get("Content-Type", ""))
        detail = response.json()["error"]["detail"]
        self.assertEqual(detail["reason"], "upstream_read_error")
        self._assert_one_row("aggregate_upstream_read_error")
        self.wait_reader_slots_restored(before)

    def test_messages_missing_message_stop_is_a_502(self):
        before = chat2api._reader_slots._value
        response = self._post_messages("msg-missing-stop", aggregate=True)
        self.assertEqual(response.status_code, 502)
        self.assertIn("application/json",
                      response.headers.get("Content-Type", ""))
        detail = response.json()["error"]["detail"]
        self.assertEqual(detail["reason"], "upstream_missing_terminal")
        self._assert_one_row("aggregate_upstream_missing_terminal")
        self.wait_reader_slots_restored(before)

    def test_messages_bad_frame_is_a_502(self):
        before = chat2api._reader_slots._value
        response = self._post_messages("msg-bad-frame", aggregate=True)
        self.assertEqual(response.status_code, 502)
        self.assertIn("application/json",
                      response.headers.get("Content-Type", ""))
        detail = response.json()["error"]["detail"]
        self.assertEqual(detail["reason"], "upstream_bad_frame")
        self._assert_one_row("aggregate_upstream_bad_frame")
        self.wait_reader_slots_restored(before)

    def test_messages_usage_missing_stays_unknown(self):
        before = chat2api._reader_slots._value
        response = self._post_messages("msg-no-usage", aggregate=True)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["content"], [{"type": "text", "text": "hi"}])
        self.assertNotIn("usage", payload)
        row = self._assert_one_row("ok")
        self.assertEqual(row["usage_source"], "estimated")
        self.assertIs(row["estimated"], True)
        self.wait_reader_slots_restored(before)

    def test_messages_reader_slot_cap_rejects_aggregate(self):
        posts_before = len(ProtocolStubUpstreamHandler.posts)
        with mock.patch.object(chat2api, "_reader_slots",
                                threading.BoundedSemaphore(1)) as slots:
            self.assertTrue(slots.acquire(blocking=False))
            try:
                response = self._post_messages("msg-ok", aggregate=True)
            finally:
                slots.release()
        self.assertEqual(response.status_code, 503)
        error = response.json()["error"]
        self.assertEqual(error["type"], "too_many_concurrent_streams")
        # The Messages aggregate acquires its slot after the 200, like the
        # existing streaming passthrough; the ledger row still records the
        # client's own stream=False.
        row = self._assert_one_row("concurrency_limit")
        self.assertIs(row["stream"], False)
        self.assertEqual(len(ProtocolStubUpstreamHandler.posts),
                         posts_before + 1)

    def test_messages_client_cancel_records_client_disconnected(self):
        baseline = self.reader_thread_count()
        before = chat2api._reader_slots._value
        body = json.dumps({"model": "msg-cancel", "max_tokens": 16,
                            "messages": [{"role": "user", "content": "hi"}],
                            "stream": False})
        request = raw_http_request(self.server, "/v1/messages", body,
                                    marker_headers())
        with mock.patch.object(chat2api, "NONSTREAM_UPSTREAM_AGGREGATE",
                               True), \
                mock.patch.object(chat2api, "NONSTREAM_AGGREGATE_SCOPE",
                                  aggregate_scope(models=("msg-cancel",),
                                                 protocols=("messages",))):
            sock = socket.create_connection(
                ("127.0.0.1", self.server.server_port))
            sock.sendall(request)
            deadline = time.monotonic() + 5.0
            while (not ProtocolStubUpstreamHandler.posts
                   and time.monotonic() < deadline):
                time.sleep(0.02)
            self.assertTrue(ProtocolStubUpstreamHandler.posts)
            time.sleep(0.2)
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()
            row = self._ledger_rows()[0]
        self.assertEqual(row["status"], "client_disconnected")
        self.assertIs(row["stream"], False)
        self.assertEqual(row["protocol"], "messages")
        self.wait_reader_threads_reclaimed(baseline)
        self.wait_reader_slots_restored(before)

    # ---------- Responses -> Chat ----------

    def test_responses_via_chat_native_non_stream_stays_native(self):
        response = self._post_responses("resp-chat-native", aggregate=False)
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/json",
                      response.headers.get("Content-Type", ""))
        payload = response.json()
        self.assertEqual(payload["object"], "response")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["output"][0]["content"][0]["text"], "hi")
        forwarded = ProtocolStubUpstreamHandler.bodies[-1]
        self.assertNotIn("stream", forwarded)
        row = self._ledger_rows()[0]
        self.assertEqual(row["status"], "ok")
        self.assertIs(row["stream"], False)
        self.assertEqual(row["protocol"], "responses")

    # ---------- R13 trusted scope ----------

    def _assert_native_responses(self, response):
        self.assertEqual(response.status_code, 200)
        forwarded = ProtocolStubUpstreamHandler.bodies[-1]
        self.assertNotIn("stream", forwarded)
        self.assertNotIn("stream_options", forwarded)

    def test_responses_selected_triple_aggregates(self):
        before = chat2api._reader_slots._value
        response = self._post_responses("resp-chat-ok", aggregate=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "completed")
        forwarded = ProtocolStubUpstreamHandler.bodies[-1]
        self.assertIs(forwarded["stream"], True)
        self.assertEqual(forwarded["stream_options"],
                         {"include_usage": True})
        row = self._assert_one_row("ok")
        self.assertEqual(row["protocol"], "responses")
        self.wait_reader_slots_restored(before)

    def test_responses_unselected_protocol_stays_native(self):
        response = self._post_responses("resp-chat-native", aggregate=True,
                                         protocols=("messages",))
        self._assert_native_responses(response)

    def test_responses_unselected_key_stays_native(self):
        response = self._post_responses("resp-chat-native", aggregate=True,
                                         key_id=UNSELECTED_KEY_ID)
        self._assert_native_responses(response)

    def test_responses_unselected_model_stays_native(self):
        response = self._post_responses("resp-chat-native", aggregate=True,
                                         scope_models=("resp-chat-ok",))
        self._assert_native_responses(response)

    def test_responses_missing_scope_stays_native(self):
        response = self._post_responses("resp-chat-native", aggregate=True,
                                         install_scope=False)
        self._assert_native_responses(response)

    def test_responses_direct_request_without_attribution_stays_native(self):
        response = self._post_responses("resp-chat-native", aggregate=True,
                                         sign_headers=False)
        self._assert_native_responses(response)

    def test_responses_via_chat_aggregate_success(self):
        before = chat2api._reader_slots._value
        response = self._post_responses("resp-chat-ok", aggregate=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/json",
                      response.headers.get("Content-Type", ""))
        payload = response.json()
        self.assertEqual(payload["object"], "response")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["output"][0]["content"][0]["text"],
                         "hello")
        self.assertEqual(payload["usage"]["input_tokens"], 3)
        self.assertEqual(payload["usage"]["output_tokens"], 2)
        self.assertEqual(payload["usage"]["total_tokens"], 5)
        forwarded = ProtocolStubUpstreamHandler.bodies[-1]
        self.assertIs(forwarded["stream"], True)
        self.assertEqual(forwarded["stream_options"],
                         {"include_usage": True})
        row = self._assert_one_row("ok")
        self.assertEqual(row["protocol"], "responses")
        self.assertEqual(row["model"], "resp-chat-ok")
        self.assertEqual(row["input_tokens"], 3)
        self.assertEqual(row["output_tokens"], 2)
        self.assertEqual(row["usage_source"], "upstream")
        self.wait_reader_slots_restored(before)

    def test_responses_via_chat_aggregate_failure_is_a_502(self):
        before = chat2api._reader_slots._value
        response = self._post_responses("resp-chat-bad-frame", aggregate=True)
        self.assertEqual(response.status_code, 502)
        self.assertIn("application/json",
                      response.headers.get("Content-Type", ""))
        detail = response.json()["error"]["detail"]
        self.assertEqual(detail["reason"], "upstream_bad_frame")
        self._assert_one_row("aggregate_upstream_bad_frame")
        self.wait_reader_slots_restored(before)

    def test_responses_via_chat_done_only_is_a_502(self):
        # Q12=A follows the shared Chat aggregator: a real [DONE] with no
        # finish_reason is not a completed Responses answer.
        before = chat2api._reader_slots._value
        response = self._post_responses("resp-chat-done-only", aggregate=True)
        self.assertEqual(response.status_code, 502)
        detail = response.json()["error"]["detail"]
        self.assertEqual(detail["reason"], "upstream_missing_terminal")
        self._assert_one_row("aggregate_upstream_missing_terminal")
        self.wait_reader_slots_restored(before)

    def test_responses_via_chat_reason_only_is_a_502(self):
        # Q12=A: a non-empty finish_reason followed by EOF without [DONE]
        # fails through the same shared aggregator.
        before = chat2api._reader_slots._value
        response = self._post_responses("resp-chat-reason-only",
                                        aggregate=True)
        self.assertEqual(response.status_code, 502)
        detail = response.json()["error"]["detail"]
        self.assertEqual(detail["reason"], "upstream_missing_terminal")
        self._assert_one_row("aggregate_upstream_missing_terminal")
        self.wait_reader_slots_restored(before)

    def test_responses_via_chat_no_terminal_is_a_502(self):
        # Q12=A control: neither terminal credential present.
        before = chat2api._reader_slots._value
        response = self._post_responses("resp-chat-no-terminal",
                                        aggregate=True)
        self.assertEqual(response.status_code, 502)
        detail = response.json()["error"]["detail"]
        self.assertEqual(detail["reason"], "upstream_missing_terminal")
        self._assert_one_row("aggregate_upstream_missing_terminal")
        self.wait_reader_slots_restored(before)

    def test_responses_via_chat_client_cancel_records_client_disconnected(
            self):
        baseline = self.reader_thread_count()
        before = chat2api._reader_slots._value
        body = json.dumps(self._responses_body("resp-chat-cancel"))
        request = raw_http_request(self.server, "/v1/responses", body,
                                    marker_headers())
        with mock.patch.object(chat2api, "NONSTREAM_UPSTREAM_AGGREGATE",
                               True), \
                mock.patch.object(chat2api, "NONSTREAM_AGGREGATE_SCOPE",
                                  aggregate_scope(models=("resp-chat-cancel",),
                                                 protocols=("responses",))):
            sock = socket.create_connection(
                ("127.0.0.1", self.server.server_port))
            sock.sendall(request)
            deadline = time.monotonic() + 5.0
            while (not ProtocolStubUpstreamHandler.posts
                   and time.monotonic() < deadline):
                time.sleep(0.02)
            self.assertTrue(ProtocolStubUpstreamHandler.posts)
            time.sleep(0.2)
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()
            row = self._ledger_rows()[0]
        self.assertEqual(row["status"], "client_disconnected")
        self.assertIs(row["stream"], False)
        self.assertEqual(row["protocol"], "responses")
        self.wait_reader_threads_reclaimed(baseline)
        self.wait_reader_slots_restored(before)

    # ---------- Responses -> Messages ----------

    def test_responses_via_messages_native_non_stream_stays_native(self):
        response = self._post_responses("resp-msg-native", aggregate=False,
                                        image_tool=True)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["object"], "response")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["output"][0]["content"][0]["text"], "hi")
        forwarded = ProtocolStubUpstreamHandler.bodies[-1]
        self.assertNotIn("stream", forwarded)
        row = self._ledger_rows()[0]
        self.assertEqual(row["status"], "ok")
        self.assertIs(row["stream"], False)
        self.assertEqual(row["protocol"], "responses")

    def test_responses_via_messages_unselected_key_stays_native(self):
        response = self._post_responses("resp-msg-native", aggregate=True,
                                        image_tool=True,
                                        key_id=UNSELECTED_KEY_ID)
        self.assertEqual(response.status_code, 200)
        forwarded = ProtocolStubUpstreamHandler.bodies[-1]
        self.assertNotIn("stream", forwarded)

    def test_responses_via_messages_aggregate_success(self):
        before = chat2api._reader_slots._value
        response = self._post_responses("resp-msg-usage-tail", aggregate=True,
                                        image_tool=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/json",
                      response.headers.get("Content-Type", ""))
        payload = response.json()
        self.assertEqual(payload["object"], "response")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["output"][0]["content"][0]["text"], "hi")
        self.assertEqual(payload["usage"]["input_tokens"], 12)
        self.assertEqual(payload["usage"]["output_tokens"], 5)
        self.assertEqual(payload["usage"]["total_tokens"], 17)
        self.assertEqual(
            payload["usage"]["input_tokens_details"]["cached_tokens"], 3)
        forwarded = ProtocolStubUpstreamHandler.bodies[-1]
        self.assertIs(forwarded["stream"], True)
        self.assertEqual(ProtocolStubUpstreamHandler.posts[-1],
                         "/api/v1/messages")
        row = self._assert_one_row("ok")
        self.assertEqual(row["protocol"], "responses")
        self.assertEqual(row["input_tokens"], 12)
        self.assertEqual(row["output_tokens"], 5)
        # The ledger reads prompt_tokens_details, not the Responses wire's
        # input_tokens_details: the native branch drops this cache detail too,
        # so the aggregate path keeps the SAME pre-existing ledger shape.
        self.assertEqual(row["cached_input_tokens"], 0)
        self.wait_reader_slots_restored(before)

    def test_responses_via_messages_aggregate_failure_is_a_502(self):
        before = chat2api._reader_slots._value
        response = self._post_responses("resp-msg-missing-stop",
                                        aggregate=True, image_tool=True)
        self.assertEqual(response.status_code, 502)
        self.assertIn("application/json",
                      response.headers.get("Content-Type", ""))
        detail = response.json()["error"]["detail"]
        self.assertEqual(detail["reason"], "upstream_missing_terminal")
        self._assert_one_row("aggregate_upstream_missing_terminal")
        self.wait_reader_slots_restored(before)

    def test_responses_via_messages_client_cancel(self):
        baseline = self.reader_thread_count()
        before = chat2api._reader_slots._value
        body = json.dumps(self._responses_body("resp-msg-cancel",
                                                image_tool=True))
        request = raw_http_request(self.server, "/v1/responses", body,
                                    marker_headers())
        with mock.patch.object(chat2api, "NONSTREAM_UPSTREAM_AGGREGATE",
                               True), \
                mock.patch.object(chat2api, "NONSTREAM_AGGREGATE_SCOPE",
                                  aggregate_scope(models=("resp-msg-cancel",),
                                                 protocols=("responses",))):
            sock = socket.create_connection(
                ("127.0.0.1", self.server.server_port))
            sock.sendall(request)
            deadline = time.monotonic() + 5.0
            while (not ProtocolStubUpstreamHandler.posts
                   and time.monotonic() < deadline):
                time.sleep(0.02)
            self.assertTrue(ProtocolStubUpstreamHandler.posts)
            time.sleep(0.2)
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()
            row = self._ledger_rows()[0]
        self.assertEqual(row["status"], "client_disconnected")
        self.assertIs(row["stream"], False)
        self.assertEqual(row["protocol"], "responses")
        self.wait_reader_threads_reclaimed(baseline)
        self.wait_reader_slots_restored(before)


if __name__ == "__main__":
    unittest.main()
