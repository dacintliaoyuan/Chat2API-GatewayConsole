# -*- coding: utf-8 -*-
"""Regression tests for chat2api streaming (SSE) forwarding contracts.

Covers the 2026-09-06 DSH incident: UM stalled streams that send zero events
and close at ~50s were forwarded as a clean EOF, which openai-based clients
misread as "Stream ended without finish_reason". chat2api must surface abnormal
stream ends in-band, release the handler promptly on client disconnect, bound
the first-byte wait, and record the stream end status in the usage ledger.

The stub upstream mirrors UM's wire format (chunked SSE); the non-chunked
scenarios isolate the clean-EOF variant. Test clients read byte-by-byte
because requests buffers read-until-close bodies in 512-byte blocks.

The stream end trace fields (x-request-id / server / connection) are pinned
too: a claimable value must reach the line byte-for-byte, while a folded header
value (CRLF inside an obs-fold continuation, which HTTP parsing keeps) or any
other control character, and an oversized value, must be reported as unavailable
instead of forging a log line or looking like a claimable id.
"""

import importlib.util
import json
import queue
import re
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _chat2api_http_harness as harness  # noqa: E402

CHAT2API_PATH = Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py"
SPEC = importlib.util.spec_from_file_location("chat2api_stream", CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)

STUB_TOKEN = "stub-token"


def _sse(payload: str) -> bytes:
    return f"data: {payload}\n\n".encode("utf-8")


def _chunk(content: str = None, finish_reason=None, usage=None) -> str:
    delta = {}
    if content is not None:
        delta["content"] = content
    chunk = {"choices": [{"delta": delta, "finish_reason": finish_reason}]}
    if usage is not None:
        chunk["usage"] = usage
    return json.dumps(chunk, ensure_ascii=False)


def _tool_chunk(name: str = None, arguments: str = None,
                tool_id: str = "call_t1", finish_reason=None,
                role: str = None) -> str:
    """One chat.completion.chunk carrying a tool_calls delta.

    A chunk with `name` is the announcement of a tool call (its arguments
    are still empty); a chunk with only `arguments` is a continuation of the
    argument stream. `role` alone is the metadata-only opener. These are the
    three shapes a client sees while a tool call is still being streamed.
    """
    delta = {}
    if role is not None:
        delta["role"] = role
    if name is not None:
        delta["tool_calls"] = [{
            "index": 0, "id": tool_id, "type": "function",
            "function": {"name": name, "arguments": arguments or ""},
        }]
    elif arguments is not None:
        delta["tool_calls"] = [{"index": 0, "function": {
            "arguments": arguments}}]
    chunk = {"choices": [{"delta": delta, "finish_reason": finish_reason}]}
    return json.dumps(chunk, ensure_ascii=False)


# The per-request correlation id the trace stub returns (Envoy returns one per
# request): a production stall must be claimable on the UM side by exactly the id
# that appears in the stream end log line.
TRACE_UPSTREAM_REQUEST_ID = "um-trace-7f3c1a2b"

# A claimable id outside the uuid shape: every character here is legal inside a
# header value, and none of them may be escaped or rewritten, or the id stops
# being lookup-able on the UM side.
TRACE_VERBATIM_REQUEST_ID = "um-trace-7F3c-1a2b_ok.v1"

# A folded (obs-fold) header value. The CRLF plus the continuation line survive
# HTTP parsing (checked in this environment against requests/urllib3), so logging
# the value verbatim writes a second, attacker-chosen log line.
FORGED_LOG_LINE = ("stream end model=forged status=ok data_lines=99 "
                   "duration_ms=1 upstream_request_id=fake-id")
TRACE_FORGED_REQUEST_ID = f"{TRACE_UPSTREAM_REQUEST_ID}\r\n\t{FORGED_LOG_LINE}"

# The trace value bound the stub and the length test share; the test also pins
# chat2api._UPSTREAM_TRACE_VALUE_MAX_CHARS to this number.
TRACE_VALUE_MAX_CHARS = 200

# The four credential headers a response may carry; none of these values may ever
# appear in a chat2api log record.
CREDENTIAL_HEADER_VALUES = (
    "Bearer SECRET-AUTH-4d1e",
    "SECRET_COOKIE=9a7c; Path=/",
    "SECRET_REQUEST_COOKIE=51be",
    "SECRET-APIKEY-77f0",
)

# Scenarios whose body is the plain OK stream: only the response headers differ,
# and each one pins a property of the stream end trace fields.
OK_STREAM_SCENARIOS = (
    "ok",
    "trace-headers",
    "trace-headers-verbatim",
    "trace-headers-forged",
    "trace-headers-control-char",
    "trace-headers-length-boundary",
    "trace-headers-secrets",
    "trace-headers-absent",
)

# The stream end line as a consumer parses it: the historical fields first, in
# their historical order and with their historical separators, then the appended
# trace fields in their fixed order. `server` and `connection` values may contain
# spaces (e.g. "BaseHTTP/0.6 Python/3.14"), so those two are matched lazily up to
# the next field marker.
STREAM_END_FIELDS_RE = re.compile(
    r"stream end model=(?P<model>\S+) status=(?P<status>\S+) "
    r"data_lines=(?P<data_lines>\d+) duration_ms=(?P<duration_ms>\d+) "
    r"upstream_request_id=(?P<request_id>\S*) "
    r"upstream_server=(?P<server>.*?) "
    r"upstream_connection=(?P<connection>.*?)"
    r"(?: exception=(?P<exception>\S+))?$")


class StubUpstreamHandler(BaseHTTPRequestHandler):
    """Fake Open WebUI /api/chat/completions with per-model scenarios."""

    protocol_version = "HTTP/1.1"
    reset_seen = False  # set when chat2api aborts a stream mid-flight
    posts = 0           # total /api/chat/completions calls seen

    def log_message(self, *args):
        pass

    def _send(self, data: bytes, chunked: bool):
        if chunked:
            self.wfile.write(f"{len(data):X}\r\n".encode("ascii")
                             + data + b"\r\n")
        else:
            self.wfile.write(data)
        self.wfile.flush()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        StubUpstreamHandler.posts += 1
        scenario = body.get("model", "")
        if scenario == "reject-400":
            # Non-SSE 400 rejection (triggers chat2api's stream_options retry).
            payload = json.dumps(
                {"error": {"message": "stream_options rejected"}}
            ).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True
            return
        chunked = scenario not in ("eof-no-done", "silence-close",
                                   "empty-data-only")
        if scenario == "trace-headers-absent":
            # No Server/Date either: every one of the three trace headers is
            # absent, so all three fields must be logged as null.
            self.send_response_only(200)
        else:
            self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Connection", "close")
        if scenario == "trace-headers":
            # The upstream response carries a correlation id plus explicit
            # routing metadata; chat2api must copy all three into the stream
            # end line of the visible stream.
            self.send_header("x-request-id", TRACE_UPSTREAM_REQUEST_ID)
            self.send_header("Connection", "close")
        elif scenario == "trace-headers-retry":
            # Each attempt gets its OWN id: the stream end line must report
            # the retry's id, never the discarded attempt's.
            self.send_header(
                "x-request-id",
                f"{TRACE_UPSTREAM_REQUEST_ID}-attempt{StubUpstreamHandler.posts}")
            self.send_header("Connection", "close")
        elif scenario == "trace-headers-verbatim":
            # A legal id: it must reach the log line byte-for-byte, or the id
            # cannot be looked up on the UM side.
            self.send_header("x-request-id", TRACE_VERBATIM_REQUEST_ID)
            self.send_header("Connection", "close")
        elif scenario == "trace-headers-forged":
            # Folded header: the CRLF and its continuation line survive HTTP
            # parsing, so logging the value verbatim forges a log line.
            self.send_header("x-request-id", TRACE_FORGED_REQUEST_ID)
            self.send_header("Connection", "close")
        elif scenario == "trace-headers-control-char":
            # One control character per field, from three different blocks:
            # C0 (vertical tab), DEL, and the C1 block.
            self.send_header("x-request-id", "um-trace-vt\x0b7f3c")
            self.send_header("server", "BaseHTTP/0.6\x7fPython/3")
            self.send_header("connection", "close\x8a")
        elif scenario == "trace-headers-length-boundary":
            # At the bound the id is still claimable verbatim; one character
            # over it must be marked unavailable, never truncated into a value
            # that still looks like a real id.
            self.send_header("x-request-id", "L" * TRACE_VALUE_MAX_CHARS)
            self.send_header("server", "M" * (TRACE_VALUE_MAX_CHARS + 1))
            self.send_header("Connection", "close")
        elif scenario == "trace-headers-secrets":
            # Credential headers on the response: none of these values may ever
            # reach a chat2api log record.
            self.send_header("Authorization", CREDENTIAL_HEADER_VALUES[0])
            self.send_header("Set-Cookie", CREDENTIAL_HEADER_VALUES[1])
            self.send_header("Cookie", CREDENTIAL_HEADER_VALUES[2])
            self.send_header("x-api-key", CREDENTIAL_HEADER_VALUES[3])
            self.send_header("x-request-id", TRACE_UPSTREAM_REQUEST_ID)
            self.send_header("Connection", "close")
        elif scenario == "trace-headers-retry-forged":
            # The VISIBLE attempt (the hidden retry) carries the folded value
            # while the discarded first attempt carries a clean id: the stream end
            # line must report the visible attempt's field — marked unavailable —
            # and never the discarded attempt's id nor the forged payload.
            if StubUpstreamHandler.posts == 1:
                self.send_header(
                    "x-request-id", f"{TRACE_UPSTREAM_REQUEST_ID}-attempt1")
            else:
                self.send_header("x-request-id", TRACE_FORGED_REQUEST_ID)
            self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            if scenario in OK_STREAM_SCENARIOS:
                self._send(_sse(_chunk()), chunked)
                self._send(_sse(_chunk(content="OK")), chunked)
                self._send(_sse(_chunk(
                    finish_reason="stop",
                    usage={"prompt_tokens": 5, "completion_tokens": 1,
                           "total_tokens": 6})), chunked)
                self._send(b"data: [DONE]\n\n", chunked)
                # Hold the connection after [DONE]: chat2api must stop reading
                # and close the downstream instead of waiting for EOF.
                time.sleep(2)
            elif scenario == "trace-headers-retry":
                # First attempt: only the metadata-only role chunk arrives,
                # then the stream dies, so nothing substantive reached the
                # client and the attempt is hidden-retryable. The second attempt
                # is a clean ok stream with its own x-request-id.
                if StubUpstreamHandler.posts == 1:
                    self._send(_sse(_tool_chunk(role="assistant")), chunked)
                    time.sleep(0.3)
                else:
                    self._send(_sse(_chunk()), chunked)
                    self._send(_sse(_chunk(content="OK")), chunked)
                    self._send(_sse(_chunk(
                        finish_reason="stop",
                        usage={"prompt_tokens": 5, "completion_tokens": 1,
                               "total_tokens": 6})), chunked)
                    self._send(b"data: [DONE]\n\n", chunked)
                    time.sleep(2)
            elif scenario == "trace-headers-retry-forged":
                # Same shape as trace-headers-retry: the first attempt dies
                # after the metadata-only role chunk, so nothing substantive
                # reached the client and the retry stays hidden.
                if StubUpstreamHandler.posts == 1:
                    self._send(_sse(_tool_chunk(role="assistant")), chunked)
                    time.sleep(0.3)
                else:
                    self._send(_sse(_chunk()), chunked)
                    self._send(_sse(_chunk(content="OK")), chunked)
                    self._send(_sse(_chunk(
                        finish_reason="stop",
                        usage={"prompt_tokens": 5, "completion_tokens": 1,
                               "total_tokens": 6})), chunked)
                    self._send(b"data: [DONE]\n\n", chunked)
                    time.sleep(2)
            elif scenario == "eof-no-done":
                # Read-until-close upstream that ends without [DONE]: a clean
                # EOF mid-completion.
                self._send(_sse(_chunk()), chunked)
                self._send(_sse(_chunk(content="partial")), chunked)
                time.sleep(0.3)
            elif scenario == "chunked-truncated":
                # Chunked upstream that dies without the terminating chunk:
                # urllib3 raises instead of returning a clean EOF.
                self._send(_sse(_chunk()), chunked)
                self._send(_sse(_chunk(content="partial")), chunked)
                time.sleep(0.3)
            elif scenario == "silence-close":
                # The incident signature: zero events, then a clean close.
                time.sleep(1.0)
            elif scenario == "zero-data-chunked-truncate":
                # The 2026-09-08 UM incident signature: 200 + chunked headers
                # accepted, zero data events, then the connection dies without
                # the terminating chunk (~50s in production), so the producer
                # sees ChunkedEncodingError with data_lines=0 downstream.
                time.sleep(0.8)
            elif scenario == "comment-then-data":
                # SSE comments/blank lines before the first data event: held
                # back with the pending 200, replayed verbatim once data
                # proves the stream alive.
                self._send(b": keepalive ping\n\n", chunked)
                self._send(b"\n\n", chunked)
                time.sleep(0.2)
                self._send(_sse(_chunk(content="after-comments")), chunked)
                self._send(_sse(_chunk(finish_reason="stop")), chunked)
                self._send(b"data: [DONE]\n\n", chunked)
                time.sleep(0.3)
            elif scenario == "empty-data-only":
                # An empty data payload is not content: it must neither commit
                # the 200 nor stop the first-data budget. Clean non-chunked
                # EOF right after, so the failure is eof_without_done.
                self._send(b"data: \n\n", chunked)
                time.sleep(0.3)
            elif scenario == "heartbeat-then-stall":
                # A comment (heartbeat) followed by silence: the comment must
                # not cancel the first-data budget (A2); a short mocked budget
                # must still fire while the stub holds silence (3s).
                self._send(b": heartbeat\n\n", chunked)
                time.sleep(3)
            elif scenario == "prelude-flood":
                # Many small comment lines: the bounded prelude must fail the
                # request explicitly instead of buffering without limit. Tests
                # mock _PRELUDE_MAX_BYTES below this ~5 KiB volume.
                for _ in range(80):
                    self._send(b": " + b"x" * 64 + b"\n\n", chunked)
                    time.sleep(0.02)
                time.sleep(1.0)
            elif scenario == "single-long-line":
                # One comment line far longer than the mocked bound: the
                # producer's line buffer must fail the request before the
                # line ever reaches the handler prelude.
                self._send(b": " + b"y" * 9000 + b"\n\n", chunked)
                time.sleep(1.0)
            elif scenario == "big-data-event":
                # Legitimate large single data events (~200 KiB each), as the
                # committing first event AND as a mid-stream event: both must
                # be forwarded intact (R1). The 1 MiB producer line bound is a
                # local full-stream compatibility limit (see chat2api.py), not
                # a promise about any valid event size.
                self._send(_sse(_chunk(content="x" * 200_000)), chunked)
                self._send(_sse(_chunk(content="y" * 200_000)), chunked)
                self._send(_sse(_chunk(finish_reason="stop")), chunked)
                self._send(b"data: [DONE]\n\n", chunked)
                time.sleep(0.3)
            elif scenario == "lineless-flood":
                # Line-less upstream: many bytes, no newline ever. The
                # producer's line buffer bound must fail the request; tests
                # mock the bound below this ~8 KiB volume for speed.
                for _ in range(64):
                    self._send(b"z" * 128, chunked)
                    time.sleep(0.01)
                time.sleep(1.0)
            elif scenario == "done-only":
                # Only [DONE] with no content and no finish_reason: zero
                # content, so the stream fails before the 200 like any other
                # zero-data stream (R3: converged from the former 200 +
                # in-band behaviour).
                self._send(b"data: [DONE]\n\n", chunked)
                time.sleep(0.5)
            elif scenario == "silent-stall":
                # Stall without closing; only the chat2api first-byte budget
                # can end this request.
                time.sleep(8)
            elif scenario == "silent-stall-short":
                # Like silent-stall but bounded to 3s so cancellation tests
                # reclaim the reader via the stub's own close, not a mocked
                # socket timeout that would race the disconnect detection.
                time.sleep(3)
            elif scenario == "no-space-done":
                self._send(b'data:{"choices":[{"delta":{"content":"hi"},'
                           b'"finish_reason":null}]}\n\n', chunked)
                self._send(b'data:{"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
                           chunked)
                self._send(b"data:[DONE]\n\n", chunked)
                time.sleep(0.5)
            elif scenario == "done-without-finish":
                # Reaches [DONE] without ever carrying a finish_reason: per the
                # OpenAI contract the completion is incomplete.
                self._send(_sse(_chunk(content="oops")), chunked)
                self._send(b"data: [DONE]\n\n", chunked)
                time.sleep(0.5)
            elif scenario == "done-empty-finish":
                # finish_reason "" is NOT a completion per the contract; it
                # must be treated the same as a missing one.
                self._send(_sse(_chunk(content="oops", finish_reason="")),
                           chunked)
                self._send(b"data: [DONE]\n\n", chunked)
                time.sleep(0.5)
            elif scenario == "continue-stream":
                # Continuous events; a client abort must make chat2api cancel
                # us promptly so our next write observes the reset.
                self._send(_sse(_chunk(content="a")), chunked)
                self._send(_sse(_chunk(content="b")), chunked)
                for _ in range(40):
                    try:
                        self._send(_sse(_chunk(content="x")), chunked)
                        time.sleep(0.2)
                    except (ConnectionError, BrokenPipeError, OSError):
                        StubUpstreamHandler.reset_seen = True
                        break
            elif scenario == "stall-after-two":
                self._send(_sse(_chunk(content="a")), chunked)
                self._send(_sse(_chunk(content="b")), chunked)
                time.sleep(12)
            elif scenario == "tool-args-truncated":
                # A tool announcement commits the 200 (a tool_calls delta is
                # substantive output), then the stream dies before the
                # arguments finish and before any terminal event: the client
                # must get the announcement plus an in-band error, never a
                # fabricated finish_reason and never a retried attempt.
                self._send(_sse(_tool_chunk(role="assistant")), chunked)
                self._send(_sse(_tool_chunk(name="Bash", arguments="")),
                            chunked)
                self._send(_sse(_tool_chunk(arguments='{"command": "echo tr')),
                            chunked)
                time.sleep(0.3)
            elif scenario == "tool-args-complete":
                # Control for tool-args-truncated: the same announcement
                # followed by the complete arguments, a terminal
                # finish_reason and [DONE] ends the stream normally.
                self._send(_sse(_tool_chunk(role="assistant")), chunked)
                self._send(_sse(_tool_chunk(name="Bash", arguments="")),
                            chunked)
                self._send(_sse(_tool_chunk(
                    arguments='{"command": "echo tool-ok"}')), chunked)
                self._send(_sse(_tool_chunk(finish_reason="tool_calls")),
                            chunked)
                self._send(b"data: [DONE]\n\n", chunked)
                time.sleep(0.3)
            elif scenario == "tool-args-truncated-then-ok":
                # First attempt dies right after the announcement; the second
                # attempt is a complete tool call. A hidden retry after the
                # committed announcement would replay it and deliver [DONE]
                # instead of the in-band error, so this is how the
                # no-retry-after-commit contract is observed.
                if StubUpstreamHandler.posts == 1:
                    self._send(_sse(_tool_chunk(role="assistant")), chunked)
                    self._send(_sse(_tool_chunk(name="Bash", arguments="")),
                                chunked)
                    self._send(_sse(_tool_chunk(
                        arguments='{"command": "echo tr')), chunked)
                    time.sleep(0.3)
                else:
                    self._send(_sse(_tool_chunk(role="assistant")), chunked)
                    self._send(_sse(_tool_chunk(name="Bash", arguments="")),
                                chunked)
                    self._send(_sse(_tool_chunk(
                        arguments='{"command": "echo tool-ok"}')), chunked)
                    self._send(_sse(_tool_chunk(finish_reason="tool_calls")),
                                chunked)
                    self._send(b"data: [DONE]\n\n", chunked)
                    time.sleep(0.3)
            elif scenario == "tool-prelude-truncated-then-ok":
                # First attempt dies while only the metadata-only role chunk
                # arrived: nothing substantive reached the client, so the 200
                # was never committed and the retry stays hidden. The second
                # attempt completes, so the client sees one clean stream.
                if StubUpstreamHandler.posts == 1:
                    self._send(_sse(_tool_chunk(role="assistant")), chunked)
                    time.sleep(0.3)
                else:
                    self._send(_sse(_tool_chunk(role="assistant")), chunked)
                    self._send(_sse(_tool_chunk(name="Bash", arguments="")),
                                chunked)
                    self._send(_sse(_tool_chunk(
                        arguments='{"command": "echo tool-ok"}')), chunked)
                    self._send(_sse(_tool_chunk(finish_reason="tool_calls")),
                                chunked)
                    self._send(b"data: [DONE]\n\n", chunked)
                    time.sleep(0.3)
            elif scenario == "slow-tool-args-truncated-then-ok":
                # Same shape as tool-args-truncated-then-ok, but slow enough
                # (with a mocked-down threshold) to be classified class=slow:
                # the slow budget must not rescue a committed announcement.
                if StubUpstreamHandler.posts == 1:
                    self._send(_sse(_tool_chunk(role="assistant")), chunked)
                    self._send(_sse(_tool_chunk(name="Bash", arguments="")),
                                chunked)
                    self._send(_sse(_tool_chunk(
                        arguments='{"command": "echo tr')), chunked)
                    time.sleep(0.45)
                else:
                    self._send(_sse(_tool_chunk(role="assistant")), chunked)
                    self._send(_sse(_tool_chunk(name="Bash", arguments="")),
                                chunked)
                    self._send(_sse(_tool_chunk(
                        arguments='{"command": "echo tool-ok"}')), chunked)
                    self._send(_sse(_tool_chunk(finish_reason="tool_calls")),
                                chunked)
                    self._send(b"data: [DONE]\n\n", chunked)
                    time.sleep(0.3)
            elif scenario == "slow-tool-prelude-truncated-then-ok":
                # Slow-class failure before anything substantive: the slow
                # budget is reachable and does retry, so the pair to
                # slow-tool-args-truncated-then-ok proves that posts==1
                # there is the commit gate, not an unreachable slow path.
                if StubUpstreamHandler.posts == 1:
                    self._send(_sse(_tool_chunk(role="assistant")), chunked)
                    time.sleep(0.45)
                else:
                    self._send(_sse(_tool_chunk(role="assistant")), chunked)
                    self._send(_sse(_tool_chunk(name="Bash", arguments="")),
                                chunked)
                    self._send(_sse(_tool_chunk(
                        arguments='{"command": "echo tool-ok"}')), chunked)
                    self._send(_sse(_tool_chunk(finish_reason="tool_calls")),
                                chunked)
                    self._send(b"data: [DONE]\n\n", chunked)
                    time.sleep(0.3)
        except (ConnectionError, BrokenPipeError, OSError):
            pass  # chat2api cancelled the upstream read


class _FailOnWrite:
    """wfile proxy that raises ConnectionResetError on the first write whose
    bytes start with `prefix`, letting every other write pass through. Used
    to pin a downstream write failure to an exact stage: b"data:" for a
    body-data payload (the prelude replay already succeeded), or b":" for a
    held comment inside the prelude replay loop. `triggered` keeps the
    offending bytes so a test can prove which write raised."""

    def __init__(self, real, prefix=b"data:"):
        self._real = real
        self._prefix = prefix
        self.triggered = None

    def write(self, b):
        if self.triggered is None and b.startswith(self._prefix):
            self.triggered = b
            raise ConnectionResetError("downstream write failed")
        return self._real.write(b)

    def __getattr__(self, name):
        # Delegate the rest (closed, close, flush, ...) to the real stream so
        # the server's request teardown can close the connection.
        return getattr(self._real, name)

    def flush(self):
        return self._real.flush()


class TestChat2ApiStreaming(unittest.TestCase):
    def setUp(self):
        # B8-0 isolation lifecycle (see _chat2api_http_harness): the ledger
        # redirect is registered FIRST so it is released LAST - after every
        # handler drained and every server closed. The pre-fix LIFO bug
        # released the patch while a post-response handler was still writing
        # record_usage, which is how test rows reached the PRODUCTION
        # data/usage-chat2api.jsonl on 2026-10-02.
        self.tmp_dir, self.usage_log = harness.install_usage_isolation(
            self, chat2api)
        harness.assert_stub_credentials(
            chat2api, local_key=None, upstream_token=STUB_TOKEN,
            stub_token_literal=STUB_TOKEN)

        stub = harness.TrackedHTTPServer(("127.0.0.1", 0),
                                         StubUpstreamHandler)
        self.stub = stub
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        harness.install_tracked_server(self, stub, name="upstream-stub",
                                         require_drained=False)

        args = SimpleNamespace(
            base_url=f"http://127.0.0.1:{stub.server_port}",
            token=STUB_TOKEN,
            profile="profile",
            login_timeout=1,
            use_api_key=False,
            no_browser=True,
        )
        proxy = chat2api.Proxy({"token": STUB_TOKEN}, args)
        server = harness.TrackedHTTPServer(("127.0.0.1", 0), chat2api.Handler)
        server.proxy = proxy
        self.server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        # Registered AFTER the redirect: this teardown (shutdown → bounded
        # drain of in-flight requests → close) runs BEFORE the USAGE_LOG
        # patch stop, so the post-response record_usage tail of the LAST
        # request lands in the temp ledger, never the production one.
        harness.install_tracked_server(self, server, name="chat2api-gateway")
        self.base = f"http://127.0.0.1:{server.server_port}"

        for patch in (
            # These tests pin single-stream forwarding contracts (budgets,
            # prelude, [DONE], cancel), NOT the auto-retry policy: disable
            # the transparent retry budgets so one stub failure stays one
            # upstream attempt (retry behaviour has its own dedicated tests).
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", False),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 0),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRY", False),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRIES", 0),
        ):
            patch.start()
            self.addCleanup(patch.stop)

        # Isolate the module-level reader semaphore per test: cancelled
        # producers from a previous test may still be draining their slots.
        chat2api._reader_slots = threading.BoundedSemaphore(
            chat2api.STREAM_MAX_CONCURRENT_READERS)
        StubUpstreamHandler.posts = 0

    # ---------- helpers ----------

    def stream_request(self, model, timeout=15):
        return requests.post(
            f"{self.base}/v1/chat/completions",
            json={"model": model,
                  "messages": [{"role": "user", "content": "hi"}],
                  "stream": True, "max_tokens": 16},
            stream=True, timeout=timeout)

    @staticmethod
    def read_to_eof(response, stop_after=None, timeout_s=15):
        """Read SSE events as they arrive; return (events, elapsed_s).

        Byte-level reads because requests buffers read-until-close bodies in
        512-byte blocks, which would hide the real-time event flow.
        """
        started = time.monotonic()
        deadline = started + timeout_s
        events = []
        buf = b""
        for byte in response.iter_content(chunk_size=1):
            if time.monotonic() > deadline:
                break
            buf += byte
            while b"\n\n" in buf:
                event, buf = buf.split(b"\n\n", 1)
                if event.strip():
                    events.append(event.strip().decode("utf-8"))
            if stop_after is not None and len(events) >= stop_after:
                break
        return events, time.monotonic() - started

    def wait_usage_records(self, count=1, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with open(self.usage_log, encoding="utf-8") as f:
                    records = [json.loads(line) for line in f if line.strip()]
            except FileNotFoundError:
                records = []
            if len(records) >= count:
                return records
            time.sleep(0.1)
        self.fail(f"usage records not written within {timeout}s")

    def current_records(self):
        try:
            with open(self.usage_log, encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            return []

    @staticmethod
    def reader_thread_count():
        return sum(1 for t in threading.enumerate()
                   if t.name == "chat2api-upstream-reader")

    def wait_reader_threads_reclaimed(self, baseline, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.reader_thread_count() <= baseline:
                return
            time.sleep(0.1)
        self.fail(f"reader threads not reclaimed within {timeout}s")

    def wait_reader_slots_restored(self, before, timeout=6):
        """Wait until the reader semaphore is back to `before` slots.

        The handler thread releases the slot AFTER the response bytes are
        already on the wire, so a client-side assert can race the release.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if chat2api._reader_slots._value >= before:
                return
            time.sleep(0.05)
        self.fail(f"reader slots not restored within {timeout}s "
                  f"({chat2api._reader_slots._value} != {before})")

    def assert_in_band_error(self, event, reason):
        payload = json.loads(event[len("data:"):])
        self.assertIn("error", payload)
        self.assertEqual(payload["error"]["type"], "upstream_stream_error")
        self.assertEqual(payload["error"]["detail"]["reason"], reason)

    @staticmethod
    def stream_end_line(log_output):
        """The one stream end line logged for the request under test."""
        lines = [line for line in log_output if "stream end" in line]
        if len(lines) != 1:
            raise AssertionError(
                f"expected exactly one stream end line, got {lines}")
        return lines[0]

    @staticmethod
    def stream_end_fields(log_output):
        """Parse the stream end line the way a log consumer does.

        Fails when the historical fields, their order or the trace fields'
        order changed, and when the line carries an embedded newline.
        """
        line = TestChat2ApiStreaming.stream_end_line(log_output)
        match = STREAM_END_FIELDS_RE.search(line)
        if match is None:
            raise AssertionError(f"stream end line not parseable: {line!r}")
        return match

    def assert_single_line_records(self, log_output):
        """No captured record may contain an embedded line break."""
        for record in log_output:
            self.assertNotIn("\r", record, record)
            self.assertNotIn("\n", record, record)

    # ---------- scenarios ----------

    def test_stream_end_logs_upstream_request_id(self):
        # A production stall is cut upstream and cannot be claimed on the UM
        # side: the stream end line carries no upstream response id. It must
        # report the x-request-id of the response that fed the visible stream,
        # plus the literal server/connection values.
        with self.assertLogs("chat2api", level="INFO") as cm:
            with self.stream_request("trace-headers") as response:
                self.assertEqual(response.status_code, 200)
                lines, _ = self.read_to_eof(response)
        self.assertEqual(lines[-1], "data: [DONE]")
        end_line = self.stream_end_line(cm.output)
        self.assertIn(
            f"upstream_request_id={TRACE_UPSTREAM_REQUEST_ID}", end_line)
        self.assertIn("upstream_server=BaseHTTP/", end_line)
        self.assertIn("upstream_connection=close", end_line)

    def test_stream_end_logs_null_without_upstream_request_id(self):
        # The ok stub returns no x-request-id: the field must be null, never a
        # fabricated default, so a stall without an upstream id is not mistaken
        # for a claimable one.
        with self.assertLogs("chat2api", level="INFO") as cm:
            with self.stream_request("ok") as response:
                self.assertEqual(response.status_code, 200)
                lines, _ = self.read_to_eof(response)
        self.assertEqual(lines[-1], "data: [DONE]")
        end_line = self.stream_end_line(cm.output)
        self.assertIn("upstream_request_id=None", end_line)

    def test_stream_end_logs_the_visible_attempt_request_id(self):
        # A hidden retry replaces the upstream response: the stream end line must
        # report the retry's x-request-id, never the discarded attempt's, or a
        # production stall would be claimed by the wrong id.
        with mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True), \
             mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 5):
            with self.assertLogs("chat2api", level="INFO") as cm:
                with self.stream_request("trace-headers-retry") as response:
                    self.assertEqual(response.status_code, 200)
                    lines, _ = self.read_to_eof(response)
        self.assertEqual(StubUpstreamHandler.posts, 2)
        self.assertEqual(lines[-1], "data: [DONE]")
        end_line = self.stream_end_line(cm.output)
        self.assertIn(
            f"upstream_request_id={TRACE_UPSTREAM_REQUEST_ID}-attempt2",
            end_line)
        self.assertNotIn(f"{TRACE_UPSTREAM_REQUEST_ID}-attempt1", end_line)

    def test_stream_end_trace_fields_are_null_when_headers_absent(self):
        # The upstream response carries none of the three trace headers: every
        # field must be null — never a fabricated default, and never the
        # unavailable marker, which means "sent but unusable".
        with self.assertLogs("chat2api", level="INFO") as cm:
            with self.stream_request("trace-headers-absent") as response:
                self.assertEqual(response.status_code, 200)
                lines, _ = self.read_to_eof(response)
        self.assertEqual(lines[-1], "data: [DONE]")
        end_line = self.stream_end_line(cm.output)
        self.assertIn("upstream_request_id=None", end_line)
        self.assertIn("upstream_server=None", end_line)
        self.assertIn("upstream_connection=None", end_line)
        self.assertNotIn("unavailable", end_line)

    def test_stream_end_trace_id_stays_verbatim_when_claimable(self):
        # A legal id must be claimable byte-for-byte: any escaping or rewriting
        # of a printable id would make it impossible to look the request up on
        # the UM side by the id in the log line.
        with self.assertLogs("chat2api", level="INFO") as cm:
            with self.stream_request("trace-headers-verbatim") as response:
                self.assertEqual(response.status_code, 200)
                lines, _ = self.read_to_eof(response)
        self.assertEqual(lines[-1], "data: [DONE]")
        self.assert_single_line_records(cm.output)
        end_line = self.stream_end_line(cm.output)
        fields = self.stream_end_fields(cm.output)
        self.assertEqual(fields.group("request_id"), TRACE_VERBATIM_REQUEST_ID)
        self.assertIn(f"upstream_request_id={TRACE_VERBATIM_REQUEST_ID} ",
                      end_line)
        self.assertNotIn("unavailable", end_line)

    def test_stream_end_trace_id_rejects_folded_header_forgery(self):
        # The folded x-request-id value carries a CRLF plus a forged "stream
        # end" line. Logging it verbatim writes a second, attacker-chosen log
        # line; the field must be marked unavailable instead, and neither the
        # forged text nor any raw line break may reach the log.
        with self.assertLogs("chat2api", level="INFO") as cm:
            with self.stream_request("trace-headers-forged") as response:
                self.assertEqual(response.status_code, 200)
                lines, _ = self.read_to_eof(response)
        self.assertEqual(lines[-1], "data: [DONE]")
        self.assert_single_line_records(cm.output)
        end_line = self.stream_end_line(cm.output)
        self.assertIn("upstream_request_id=unavailable:control_chars", end_line)
        # The raw forged payload never reaches the log, so the forged line
        # cannot be mistaken for a real stream end record.
        joined = "\n".join(cm.output)
        self.assertNotIn(FORGED_LOG_LINE, joined)
        self.assertNotIn("model=forged", joined)
        self.assertNotIn("fake-id", joined)

    def test_stream_end_trace_fields_reject_other_control_characters(self):
        # CR/LF are not the only characters a header value can smuggle in: a C0
        # control (VT), DEL and a C1 control must each mark their field
        # unavailable, for all three trace fields.
        with self.assertLogs("chat2api", level="INFO") as cm:
            with self.stream_request("trace-headers-control-char") as response:
                self.assertEqual(response.status_code, 200)
                lines, _ = self.read_to_eof(response)
        self.assertEqual(lines[-1], "data: [DONE]")
        self.assert_single_line_records(cm.output)
        end_line = self.stream_end_line(cm.output)
        self.assertIn("upstream_request_id=unavailable:control_chars", end_line)
        self.assertIn("upstream_server=unavailable:control_chars", end_line)
        self.assertIn("upstream_connection=unavailable:control_chars", end_line)
        for smuggled in ("\x0b", "\x7f", "\x8a"):
            self.assertNotIn(smuggled, end_line)

    def test_stream_end_trace_value_length_boundary(self):
        # An oversized trace value must not be written unbounded, and must not
        # be truncated into a shorter value that still looks like a real id. At
        # the bound the value stays claimable; one character over it is marked
        # unavailable.
        with self.assertLogs("chat2api", level="INFO") as cm:
            with self.stream_request("trace-headers-length-boundary") as response:
                self.assertEqual(response.status_code, 200)
                lines, _ = self.read_to_eof(response)
        self.assertEqual(lines[-1], "data: [DONE]")
        self.assert_single_line_records(cm.output)
        end_line = self.stream_end_line(cm.output)
        self.assertIn(
            f"upstream_request_id={'L' * TRACE_VALUE_MAX_CHARS} ", end_line)
        self.assertIn("upstream_server=unavailable:too_long", end_line)
        # Neither the full oversized value nor a truncated prefix of it appears.
        self.assertNotIn("M" * TRACE_VALUE_MAX_CHARS, end_line)
        self.assertNotIn("M" * (TRACE_VALUE_MAX_CHARS + 1), end_line)
        self.assertLess(len(end_line), TRACE_VALUE_MAX_CHARS + 300)
        self.assertEqual(chat2api._UPSTREAM_TRACE_VALUE_MAX_CHARS,
                         TRACE_VALUE_MAX_CHARS)

    def test_stream_end_trace_visible_attempt_marked_unavailable_when_forged(self):
        # The VISIBLE attempt (the hidden retry) carries the folded value while
        # the discarded attempt's id is clean: the line must report the visible
        # attempt's field as unavailable, never the discarded attempt's id and
        # never the forged payload.
        with mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True), \
             mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 5):
            with self.assertLogs("chat2api", level="INFO") as cm:
                with self.stream_request(
                        "trace-headers-retry-forged") as response:
                    self.assertEqual(response.status_code, 200)
                    lines, _ = self.read_to_eof(response)
        self.assertEqual(StubUpstreamHandler.posts, 2)
        self.assertEqual(lines[-1], "data: [DONE]")
        self.assert_single_line_records(cm.output)
        end_line = self.stream_end_line(cm.output)
        self.assertIn("upstream_request_id=unavailable:control_chars", end_line)
        self.assertNotIn(f"{TRACE_UPSTREAM_REQUEST_ID}-attempt1", end_line)
        joined = "\n".join(cm.output)
        self.assertNotIn(FORGED_LOG_LINE, joined)
        self.assertNotIn("fake-id", joined)

    def test_stream_end_log_never_carries_credential_headers(self):
        # Only the three trace names are ever read, so a credential header on
        # the upstream response must not appear in any chat2api log record.
        with self.assertLogs("chat2api", level="INFO") as cm:
            with self.stream_request("trace-headers-secrets") as response:
                self.assertEqual(response.status_code, 200)
                lines, _ = self.read_to_eof(response)
        self.assertEqual(lines[-1], "data: [DONE]")
        joined = "\n".join(cm.output)
        for secret in CREDENTIAL_HEADER_VALUES:
            self.assertNotIn(secret, joined)
        end_line = self.stream_end_line(cm.output)
        self.assertIn(
            f"upstream_request_id={TRACE_UPSTREAM_REQUEST_ID} ", end_line)

    def test_stream_end_line_keeps_legacy_fields_and_order(self):
        # A consumer of the historical "stream end" line must keep working: the
        # historical field names, values and separators are unchanged, and the
        # trace fields stay appended in their fixed order.
        with self.assertLogs("chat2api", level="INFO") as cm:
            with self.stream_request("trace-headers") as response:
                self.assertEqual(response.status_code, 200)
                lines, _ = self.read_to_eof(response)
        self.assertEqual(lines[-1], "data: [DONE]")
        self.assert_single_line_records(cm.output)
        end_line = self.stream_end_line(cm.output)
        body = end_line[end_line.index("stream end "):]
        legacy_prefix = body[:body.index(" upstream_request_id=")]
        self.assertRegex(
            legacy_prefix,
            r"^stream end model=trace-headers status=ok data_lines=\d+ "
            r"duration_ms=\d+$")
        fields = self.stream_end_fields(cm.output)
        self.assertEqual(fields.group("model"), "trace-headers")
        self.assertEqual(fields.group("status"), "ok")
        self.assertGreaterEqual(int(fields.group("data_lines")), 1)
        self.assertIsNotNone(int(fields.group("duration_ms")))
        self.assertEqual(fields.group("request_id"), TRACE_UPSTREAM_REQUEST_ID)
        self.assertTrue(fields.group("server").startswith("BaseHTTP/"))
        self.assertEqual(fields.group("connection"), "close")
        self.assertLess(end_line.index("upstream_request_id="),
                        end_line.index("upstream_server="))
        self.assertLess(end_line.index("upstream_server="),
                        end_line.index("upstream_connection="))

    def test_ok_stream_forwards_finish_reason_and_done(self):
        with self.stream_request("ok") as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream", response.headers["Content-Type"])
            lines, elapsed = self.read_to_eof(response)
        # [DONE] is last, no synthetic events; the stub holds the connection
        # for 2s after [DONE], so a prompt close proves gotcha #2 still holds.
        self.assertLess(elapsed, 1.5)
        self.assertEqual(lines[-1], "data: [DONE]")
        self.assertFalse(any('"error"' in line for line in lines))
        finish = [line for line in lines if '"finish_reason": "stop"' in line
                  or '"finish_reason":"stop"' in line]
        self.assertTrue(finish)

        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "ok")
        self.assertFalse(record["estimated"])
        self.assertEqual(record["output_tokens"], 1)

    def test_upstream_eof_without_done_is_reported_in_band(self):
        with self.stream_request("eof-no-done") as response:
            self.assertEqual(response.status_code, 200)
            lines, _ = self.read_to_eof(response)
        self.assertNotIn("data: [DONE]", lines)
        self.assert_in_band_error(lines[-1], "upstream_eof_without_done")

        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "upstream_eof_without_done")
        self.assertTrue(record["estimated"])
        # "partial" (7 chars) was delivered downstream before the cut: an
        # honest lower-bound estimate, not zero and not a fabricated floor.
        self.assertEqual(record["output_tokens"], 1)

    def test_truncated_chunked_stream_is_reported_in_band(self):
        with self.stream_request("chunked-truncated") as response:
            self.assertEqual(response.status_code, 200)
            lines, _ = self.read_to_eof(response)
        self.assertNotIn("data: [DONE]", lines)
        self.assert_in_band_error(lines[-1], "upstream_read_error")

        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "upstream_read_error")

    def test_silent_zero_event_stream_fails_before_200(self):
        # Zero data events, then a clean close: with the 200 still pending,
        # the failure is an HTTP 502 retry-capable clients can act on, not an
        # in-band event after a committed 200.
        started = time.monotonic()
        with self.stream_request("silence-close") as response:
            self.assertEqual(response.status_code, 502)
            error = response.json()["error"]
            self.assertEqual(error["type"], "upstream_stream_error")
            self.assertEqual(error["detail"]["reason"],
                             "upstream_eof_without_done")
            self.assertEqual(error["detail"]["events"], 0)
        self.assertGreaterEqual(time.monotonic() - started, 0.9)  # held ~1s

        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "upstream_eof_without_done")

    def test_zero_data_chunked_truncation_fails_before_200(self):
        # The exact 2026-09-08 wire combination that reached ZCode as
        # reason=unknown retryable=false: chunked headers, zero data events,
        # then a cut without the terminating chunk. The 502 carries the same
        # detail the in-band event used to, and ZCode demonstrably retries
        # HTTP 502 (unlike in-band stream errors).
        started = time.monotonic()
        with self.stream_request("zero-data-chunked-truncate") as response:
            self.assertEqual(response.status_code, 502)
            error = response.json()["error"]
            self.assertEqual(error["type"], "upstream_stream_error")
            self.assertEqual(error["detail"]["reason"], "upstream_read_error")
            self.assertEqual(error["detail"]["events"], 0)
            self.assertEqual(error["detail"]["exception"],
                             "ChunkedEncodingError")
        self.assertGreaterEqual(time.monotonic() - started, 0.8)

        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "upstream_read_error")

    def test_prelude_replay_preserves_blank_lines_and_order(self):
        # Comments and blank lines that arrive before the first data event are
        # held with the pending 200, then replayed verbatim ahead of the data.
        # Byte-level equality proves order, blank lines and the \n\n event
        # separation contract all survive the hold-and-replay.
        with self.stream_request("comment-then-data") as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream",
                          response.headers["Content-Type"])
            body = response.content
        # Byte-level equality proves order, blank lines and the event
        # separation contract all survive hold-and-replay. The forwarding
        # wire is line-based: every held line replays as line + "\n\n", and
        # each upstream SSE event ("data: ...\n\n") arrives as the data line
        # plus its separating blank line, exactly as for non-held lines.
        data_row = _sse(_chunk(content="after-comments"))
        stop_row = _sse(_chunk(finish_reason="stop"))
        self.assertEqual(
            body,
            b": keepalive ping\n\n"
            b"\n\n\n\n\n\n"          # three held blank-line items
            + data_row + b"\n\n"     # commit event + its blank separator
            + stop_row + b"\n\n"
            + b"data: [DONE]\n\n")   # [DONE] ends the stream, no extra blank

        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "ok")

    def test_empty_data_payload_does_not_commit_200(self):
        # An empty data payload is not content: the 200 stays pending and a
        # clean EOF afterwards is a 502, not a committed stream.
        with self.stream_request("empty-data-only") as response:
            self.assertEqual(response.status_code, 502)
            error = response.json()["error"]
            self.assertEqual(error["detail"]["reason"],
                             "upstream_eof_without_done")
            self.assertEqual(error["detail"]["events"], 1)

        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "upstream_eof_without_done")

    def test_heartbeat_comment_does_not_stop_first_data_budget(self):
        # A comment (heartbeat) must not cancel the first-data budget (A2):
        # with a 1s budget and a silently stalling stub, the request fails
        # with first_byte_timeout well before the stub would close (3s).
        started = time.monotonic()
        with mock.patch.object(chat2api, "STREAM_FIRST_BYTE_TIMEOUT_S", 1):
            with self.stream_request("heartbeat-then-stall",
                                     timeout=10) as response:
                self.assertEqual(response.status_code, 502)
                error = response.json()["error"]
                self.assertEqual(error["detail"]["reason"],
                                 "upstream_first_byte_timeout")
        self.assertLess(time.monotonic() - started, 5)

        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "upstream_first_byte_timeout")

    def test_prelude_flood_fails_with_explicit_502(self):
        # Rapid small comment lines must hit the prelude byte bound and fail
        # with a stable reason while the 200 was never sent - no unbounded
        # buffering, no indefinite header wait.
        with mock.patch.object(chat2api, "_PRELUDE_MAX_BYTES", 1024):
            with self.stream_request("prelude-flood", timeout=10) as response:
                self.assertEqual(response.status_code, 502)
                error = response.json()["error"]
                self.assertEqual(error["detail"]["reason"],
                                 "upstream_prelude_overflow")

        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "upstream_prelude_overflow")

    def test_single_line_longer_than_bound_fails(self):
        # A line that cannot complete within the producer's line-buffer bound
        # fails there, before it ever reaches the handler prelude (R1: the
        # producer bound is independent of the 64 KiB prelude bound).
        with mock.patch.object(chat2api, "_PRODUCER_LINE_BUFFER_MAX_BYTES",
                               2048):
            with self.stream_request("single-long-line",
                                     timeout=10) as response:
                self.assertEqual(response.status_code, 502)
                error = response.json()["error"]
                self.assertEqual(error["detail"]["reason"],
                                 "upstream_read_error")
                self.assertEqual(error["detail"]["exception"], "ValueError")

        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "upstream_read_error")

    def test_large_data_event_is_forwarded_intact(self):
        # R1: legitimate ~200 KiB single data events must be committed and
        # forwarded intact - neither the 64 KiB prelude bound nor the 1 MiB
        # full-stream line bound may truncate them, for the committing first
        # event AND for mid-stream events alike. The 1 MiB bound is a local
        # full-stream compatibility limit (chat2api.py constant comment and
        # README), not a promise about any valid event size.
        with self.stream_request("big-data-event", timeout=30) as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream",
                          response.headers["Content-Type"])
            body = response.content
        self.assertIn(b'{"delta": {"content": "' + b"x" * 200_000, body)
        self.assertIn(b'{"delta": {"content": "' + b"y" * 200_000, body)
        self.assertEqual(body.count(b'"content": "'), 2)  # both events intact
        self.assertTrue(body.rstrip().endswith(b"data: [DONE]"))

        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "ok")

    def test_lineless_flood_fails_at_line_buffer_bound(self):
        # R1: a streamless line-less upstream (no newline ever) must fail at
        # the producer's line-buffer bound with a stable reason, not pin
        # memory.
        with mock.patch.object(chat2api, "_PRODUCER_LINE_BUFFER_MAX_BYTES",
                               4096):
            with self.stream_request("lineless-flood",
                                     timeout=10) as response:
                self.assertEqual(response.status_code, 502)
                error = response.json()["error"]
                self.assertEqual(error["detail"]["reason"],
                                 "upstream_read_error")
                self.assertEqual(error["detail"]["exception"], "ValueError")

        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "upstream_read_error")

    def test_done_only_stream_fails_before_200(self):
        # R3: a stream whose only event is [DONE] (no content, no
        # finish_reason) carries zero content, so it fails before the 200
        # like every other zero-data stream - this converges from the former
        # 200 + in-band missing-finish behaviour and is now asserted here.
        with self.stream_request("done-only") as response:
            self.assertEqual(response.status_code, 502)
            error = response.json()["error"]
            self.assertEqual(error["detail"]["reason"],
                             "upstream_missing_finish_reason")
            self.assertEqual(error["detail"]["events"], 1)

        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "upstream_missing_finish_reason")

    def test_body_data_write_failure_is_client_disconnected(self):
        # A downstream that dies on the first BODY data payload write - after
        # the headers AND the held prelude have been replayed - ends as
        # client_disconnected and never receives a second status line (A4).
        # The replay loop has already run and succeeded here, so this pins
        # the body-data write try/except; the replay-loop variant is
        # test_prelude_replay_write_failure_is_client_disconnected.
        before = chat2api._reader_slots._value
        records_before = len(self.current_records())
        error_calls = []

        original_setup = chat2api.Handler.setup

        def failing_setup(handler):
            original_setup(handler)
            handler.wfile = _FailOnWrite(handler.wfile)  # fires on "data:"

        with mock.patch.object(chat2api.Handler, "setup", failing_setup), \
                mock.patch.object(chat2api.Handler, "_error",
                                  side_effect=lambda *a, **k:
                                  error_calls.append(a)):
            response = self.stream_request("comment-then-data", timeout=10)
            self.assertEqual(response.status_code, 200)  # headers committed
            # The first data payload write fails: the client keeps whatever
            # prelude bytes already landed, the connection closes mid-stream
            # (a clean EOF for the client, no protocol error), and no data
            # event and no second status line may ever arrive.
            body = response.content
            response.close()
        self.assertIn(b": keepalive ping", body)  # prelude replay landed
        self.assertNotIn(b"data:", body)          # first data never reached
        self.wait_reader_slots_restored(before)
        self.assertEqual(error_calls, [])
        (record,) = self.wait_usage_records(count=records_before + 1,
                                            timeout=6)
        self.assertEqual(record["status"], "client_disconnected")

    def test_prelude_replay_write_failure_is_client_disconnected(self):
        # A downstream that dies while a HELD COMMENT is replayed inside the
        # prelude loop (`for held in prelude` in the commit/replay try block)
        # must end as client_disconnected, get no second status line, and
        # release the reader slot. _FailOnWrite with prefix b":" fires only
        # on a comment line - headers and data payloads never start with ":" -
        # so the injected failure provably lands in the replay loop, unlike
        # the body-data variant above.
        before = chat2api._reader_slots._value
        records_before = len(self.current_records())
        error_calls = []
        writers = []

        original_setup = chat2api.Handler.setup

        def failing_setup(handler):
            original_setup(handler)
            writer = _FailOnWrite(handler.wfile, prefix=b":")
            writers.append(writer)
            handler.wfile = writer

        with mock.patch.object(chat2api.Handler, "setup", failing_setup), \
                mock.patch.object(chat2api.Handler, "_error",
                                  side_effect=lambda *a, **k:
                                  error_calls.append(a)):
            response = self.stream_request("comment-then-data", timeout=10)
            self.assertEqual(response.status_code, 200)  # headers committed
            body = response.content
            response.close()
        # The failing write was a comment line (": ..."): it died on the very
        # first replay write, so not even the held comment reached the
        # client, which proves the failure happened inside the replay loop.
        self.assertEqual(body, b"")
        self.assertTrue(writers and writers[0].triggered
                        and writers[0].triggered.startswith(b":"))
        self.wait_reader_slots_restored(before)
        self.assertEqual(error_calls, [])
        (record,) = self.wait_usage_records(count=records_before + 1,
                                            timeout=6)
        self.assertEqual(record["status"], "client_disconnected")

    def test_client_disconnect_before_first_data_cancels_stream(self):
        # A client leaving before any data must release the handler promptly
        # even though nothing was ever written to it: the disconnect check in
        # the consume loop is the only signal (no write can fail yet).
        # requests' response.close() on an unread stream merely returns the
        # connection to its pool and never sends a FIN, so the disconnect is
        # driven with a raw socket instead.
        baseline = self.reader_thread_count()
        records_before = len(self.current_records())
        body = json.dumps({"model": "silent-stall-short",
                           "messages": [{"role": "user", "content": "hi"}],
                           "stream": True, "max_tokens": 16})
        request = (f"POST /v1/chat/completions HTTP/1.1\r\n"
                   f"Host: 127.0.0.1:{self.server.server_port}\r\n"
                   f"Content-Type: application/json\r\n"
                   f"Content-Length: {len(body)}\r\n\r\n{body}").encode()
        sock = socket.create_connection(("127.0.0.1", self.server.server_port))
        sock.sendall(request)
        time.sleep(0.5)  # reach the consume loop; upstream stays silent
        sock.shutdown(socket.SHUT_RDWR)
        sock.close()
        (record,) = self.wait_usage_records(count=records_before + 1,
                                            timeout=6)
        self.assertEqual(record["status"], "client_disconnected")
        self.wait_reader_threads_reclaimed(baseline)

    def test_first_byte_stall_is_bounded_by_budget(self):
        started = time.monotonic()
        with mock.patch.object(chat2api, "STREAM_FIRST_BYTE_TIMEOUT_S", 1):
            with self.stream_request("silent-stall", timeout=10) as response:
                self.assertEqual(response.status_code, 502)
                error = response.json()["error"]
                self.assertEqual(error["detail"]["reason"],
                                 "upstream_first_byte_timeout")
        # Budget 1s + producer handoff; far below the 8s the stall would run.
        self.assertLess(time.monotonic() - started, 5)

        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "upstream_first_byte_timeout")

    def test_done_without_space_still_completes(self):
        with self.stream_request("no-space-done") as response:
            self.assertEqual(response.status_code, 200)
            lines, _ = self.read_to_eof(response)
        self.assertEqual(lines[-1], "data:[DONE]")
        self.assertFalse(any('"error"' in line for line in lines))
        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "ok")

    def test_done_without_finish_reason_is_reported_in_band(self):
        # Regression: reaching [DONE] without any finish_reason must NOT be
        # forwarded as a successful completion (pi-ai rejects it and would
        # surface "Stream ended without finish_reason").
        with self.stream_request("done-without-finish") as response:
            self.assertEqual(response.status_code, 200)
            lines, _ = self.read_to_eof(response)
        self.assertNotIn("data: [DONE]", lines)  # termination marker withheld
        self.assert_in_band_error(lines[-1], "upstream_missing_finish_reason")
        # "oops" (4 chars) was delivered downstream; estimate reflects it.
        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "upstream_missing_finish_reason")
        self.assertTrue(record["estimated"])
        self.assertEqual(record["output_tokens"], 1)

    def test_empty_finish_reason_is_not_a_completion(self):
        # Regression: finish_reason "" is falsy and must NOT satisfy the
        # completion contract ("非空" finish_reason required).
        with self.stream_request("done-empty-finish") as response:
            self.assertEqual(response.status_code, 200)
            lines, _ = self.read_to_eof(response)
        self.assertNotIn("data: [DONE]", lines)
        self.assert_in_band_error(lines[-1], "upstream_missing_finish_reason")

        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "upstream_missing_finish_reason")

    def test_reader_slot_cap_rejects_excess_streams_without_upstream_call(self):
        with mock.patch.object(chat2api, "STREAM_MAX_CONCURRENT_READERS", 1):
            chat2api._reader_slots = threading.BoundedSemaphore(1)
            holder_done = []

            def hold():
                with self.stream_request("ok") as response:
                    self.read_to_eof(response)
                holder_done.append(True)

            holder = threading.Thread(target=hold)
            holder.start()
            time.sleep(0.4)  # the first stream holds the single slot
            calls_before = StubUpstreamHandler.posts
            excess = self.stream_request("ok")
            try:
                self.assertEqual(excess.status_code, 503)
                error = excess.json()["error"]
                self.assertEqual(error["type"], "too_many_concurrent_streams")
            finally:
                excess.close()
            # The slot is acquired before any upstream access: a refused
            # request must never have touched the upstream (no model call).
            self.assertEqual(StubUpstreamHandler.posts, calls_before,
                             "rejected request must not call the upstream")
            holder.join(10)
            self.assertTrue(holder_done)

    def test_slot_returned_when_first_upstream_call_times_out(self):
        # R5: a network-level failure on the FIRST upstream call (before any
        # headers) must not leak the reader slot, and the next normal request
        # must still work end to end.
        before = chat2api._reader_slots._value
        calls = {"n": 0}
        real_request = chat2api.requests.request

        def times_out_first(method, url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise requests.exceptions.ReadTimeout("upstream timed out")
            return real_request(method, url, **kwargs)

        with mock.patch.object(chat2api.requests, "request",
                               side_effect=times_out_first):
            response = self.stream_request("ok")
            try:
                self.assertEqual(response.status_code, 502)
                self.assertEqual(
                    response.json()["error"]["type"], "chat2api_error")
            finally:
                response.close()
        self.wait_reader_slots_restored(before)
        self.assertEqual(StubUpstreamHandler.posts, 0,
                         "failed request must not have called the upstream")

        with self.stream_request("ok") as response:
            self.assertEqual(response.status_code, 200)
            lines, _ = self.read_to_eof(response)
        self.assertEqual(lines[-1], "data: [DONE]")

    def test_slot_returned_when_400_retry_fails(self):
        # R5: the stream_options 400-retry path must also return the slot when
        # the retry itself fails at the network level.
        before = chat2api._reader_slots._value
        calls = {"n": 0}
        real_request = chat2api.requests.request

        def four_hundred_then_error(method, url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_request(method, url, **kwargs)  # stub -> 400
            raise requests.exceptions.ConnectionError("retry failed")

        with mock.patch.object(chat2api.requests, "request",
                               side_effect=four_hundred_then_error):
            response = self.stream_request("reject-400")
            try:
                self.assertEqual(response.status_code, 502)
            finally:
                response.close()
        self.wait_reader_slots_restored(before)
        self.assertEqual(StubUpstreamHandler.posts, 1)  # only the 400 call

        with self.stream_request("ok") as response:
            self.assertEqual(response.status_code, 200)
            lines, _ = self.read_to_eof(response)
        self.assertEqual(lines[-1], "data: [DONE]")

    def test_slot_returned_when_response_header_write_fails(self):
        # R4-2 + A4: a client that dies while the 200 headers are being
        # written must not leak the reader slot (end_headers raises on the
        # dead socket), must be accounted as client_disconnected, and must
        # never receive a second status line (no 502 retry after a partial
        # header write).
        before = chat2api._reader_slots._value
        records_before = len(self.current_records())
        error_calls = []
        with mock.patch.object(
                chat2api.Handler, "end_headers",
                side_effect=ConnectionResetError("client gone")), \
                mock.patch.object(chat2api.Handler, "_error",
                                  side_effect=lambda *a, **k:
                                  error_calls.append(a)):
            with self.assertRaises(requests.exceptions.RequestException):
                self.stream_request("ok")
        self.wait_reader_slots_restored(before)
        self.assertEqual(error_calls, [])
        (record,) = self.wait_usage_records(count=records_before + 1,
                                            timeout=6)
        self.assertEqual(record["status"], "client_disconnected")

    def test_slot_returned_when_producer_start_fails(self):
        # R4-2: if the producer thread cannot start, the handler must still
        # return the slot and close the upstream response it already owns.
        # The 200 is still pending at that point, so the client gets a 502
        # instead of a connection dropped without any response.
        before = chat2api._reader_slots._value
        with mock.patch.object(
                chat2api._UpstreamStream, "start",
                side_effect=RuntimeError("thread start failed")):
            response = self.stream_request("ok")
            try:
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json()["error"]["type"],
                                 "upstream_stream_error")
            finally:
                response.close()
        self.wait_reader_slots_restored(before)

    # ---------- _UpstreamStream unit tests ----------

    class _FullLinesResponse:
        """Response whose iter_content yields exactly maxsize lines then ends."""

        def __init__(self, lines, raise_after=None):
            self.lines = lines
            self.raise_after = raise_after
            self.closed = False

        def iter_content(self, chunk_size, decode_unicode=False):
            for i in range(len(self.lines) + 1):
                if self.raise_after is not None and i >= self.raise_after:
                    raise RuntimeError("upstream exploded")
                if i < len(self.lines):
                    yield (self.lines[i] + "\n").encode("utf-8")

        def close(self):
            self.closed = True

    class _TrickleResponse:
        """Response that drips bytes forever without ever completing a line."""

        def __init__(self):
            self.closed = False

        def iter_content(self, chunk_size, decode_unicode=False):
            for _ in range(5000):
                time.sleep(0.002)
                yield b"x"

        def close(self):
            self.closed = True

    def test_termination_reaches_consumer_with_full_queue(self):
        # Regression (R3-1): the upstream ends exactly when the bounded queue
        # is full; the termination must still reach the consumer. The consumer
        # deliberately does not drain while the producer fills the queue.
        stream = chat2api._UpstreamStream(self._FullLinesResponse(
            ["seed-%d" % i for i in range(chat2api._UPSTREAM_QUEUE_MAX)]))
        stream.start()
        deadline = time.monotonic() + 5
        while not stream._finished and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(stream._finished, "producer never finished")
        count = 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            item = stream.next_item(2)
            if item is chat2api._UPSTREAM_END:
                break
            count += 1
        else:
            self.fail("consumer never received the terminal marker")
        self.assertEqual(count, chat2api._UPSTREAM_QUEUE_MAX)

    def test_error_reaches_consumer_with_full_queue(self):
        stream = chat2api._UpstreamStream(self._FullLinesResponse(
            ["seed-%d" % i for i in range(chat2api._UPSTREAM_QUEUE_MAX)],
            raise_after=chat2api._UPSTREAM_QUEUE_MAX))
        stream.start()
        deadline = time.monotonic() + 5
        while not stream._finished and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(stream._finished)
        for _ in range(chat2api._UPSTREAM_QUEUE_MAX):
            stream.next_item(2)
        with self.assertRaisesRegex(RuntimeError, "upstream exploded"):
            stream.next_item(2)

    def test_cancel_exits_during_byte_trickle(self):
        # Regression (R3-2): an upstream that drips bytes without finishing a
        # line must not starve the cancel check (it used to reset the silence
        # timer forever). With per-byte reads the producer exits at the next
        # byte after cancel.
        stream = chat2api._UpstreamStream(self._TrickleResponse())
        stream.start()
        time.sleep(0.2)
        stream.cancel()
        deadline = time.monotonic() + 3
        while not stream._finished and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(stream._finished,
                        "cancelled producer survived a byte trickle")

    def test_cancel_reclaims_reader_thread_and_connection_promptly(self):
        baseline = self.reader_thread_count()
        StubUpstreamHandler.reset_seen = False
        records_before = len(self.current_records())

        response = self.stream_request("continue-stream")
        try:
            self.read_to_eof(response, stop_after=2)
            response.close()
        finally:
            (record,) = self.wait_usage_records(count=records_before + 1,
                                                timeout=6)
        self.assertEqual(record["status"], "client_disconnected")
        # The producer drops out at its next line, closes the upstream
        # response, and the stub observes the abort on its following write -
        # long before the scenario's ~8s stream would end on its own. The
        # stub's observe lands on its next 0.2s tick, so poll for it.
        self.wait_reader_threads_reclaimed(baseline)
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if StubUpstreamHandler.reset_seen:
                break
            time.sleep(0.1)
        self.assertTrue(StubUpstreamHandler.reset_seen)

    def test_silent_upstream_reader_is_bounded_and_reclaimed(self):
        # A cancelled reader over a silently-stalled upstream must still be
        # reclaimed by the per-read socket timeout, not live until the
        # upstream closes (12s) or the old 600s requests default.
        baseline = self.reader_thread_count()
        records_before = len(self.current_records())
        with mock.patch.object(chat2api, "STREAM_SOCKET_TIMEOUT_S", 1):
            response = self.stream_request("stall-after-two")
            try:
                self.read_to_eof(response, stop_after=2)
                response.close()
            finally:
                (record,) = self.wait_usage_records(count=records_before + 1,
                                                    timeout=6)
        self.assertEqual(record["status"], "client_disconnected")
        self.wait_reader_threads_reclaimed(baseline)

    def test_client_disconnect_releases_handler_promptly(self):
        started = time.monotonic()
        response = self.stream_request("stall-after-two")
        try:
            self.read_to_eof(response, stop_after=2)
            response.close()
            # The stub goes silent for 12s after two events; the handler must
            # notice the disconnect and write the usage record long before.
            (record,) = self.wait_usage_records(timeout=6)
        finally:
            pass
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(record["status"], "client_disconnected")
        self.assertEqual(record["output_tokens"], 0)

    def test_record_usage_defaults_to_ok_status(self):
        chat2api.record_usage("m", False, time.time() - 1, None, 3, 2)
        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "ok")


    # ---------- tool-input truncation contract locks ----------
    #
    # These six cases pin how the gateway treats a tool call that is still
    # being streamed when the upstream dies. They are characterisation
    # (contract) locks, not red-to-green regressions: they pass against the
    # unmodified product. A failure means the forwarding contract moved -
    # re-pin the evidence object and re-run before changing anything.

    @staticmethod
    def sse_payloads(events):
        """Parse data events into JSON payloads, keeping [DONE] as a marker."""
        payloads = []
        for event in events:
            if not event.startswith("data:"):
                continue
            raw = event[len("data:"):].strip()
            payloads.append("[DONE]" if raw == "[DONE]" else json.loads(raw))
        return payloads

    @staticmethod
    def tool_call_deltas(payloads):
        """(name, argument fragment) pairs from tool_calls deltas, in order."""
        seen = []
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            for choice in payload.get("choices") or []:
                delta = (choice or {}).get("delta") or {}
                for call in delta.get("tool_calls") or []:
                    function = call.get("function") or {}
                    seen.append((function.get("name"),
                                  function.get("arguments")))
        return seen

    def test_truncated_tool_arguments_are_reported_in_band(self):
        # A: a tool_calls delta is substantive output, so the 200 is already
        # committed by the time the arguments finish - here by the metadata
        # role chunk, which is itself a non-empty data event. A stream that
        # then dies before the arguments finish must surface an in-band
        # error: the announced name and the argument fragment reach the
        # client verbatim and nothing is synthesised for the missing tail,
        # because a client must never read a half tool call as complete.
        with self.stream_request("tool-args-truncated") as response:
            self.assertEqual(response.status_code, 200)
            events, _ = self.read_to_eof(response)
        payloads = self.sse_payloads(events)
        deltas = self.tool_call_deltas(payloads)
        self.assertEqual(deltas[0], ("Bash", ""))
        self.assertEqual(deltas[1], (None, '{"command": "echo tr'))
        self.assertNotIn("[DONE]", payloads)
        self.assertFalse(any(
            (choice or {}).get("finish_reason")
            for payload in payloads if isinstance(payload, dict)
            for choice in payload.get("choices") or []))
        self.assert_in_band_error(events[-1], "upstream_read_error")
        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "upstream_read_error")

    def test_complete_tool_call_stream_is_forwarded_verbatim(self):
        # B: control for A. With the arguments complete and a terminal
        # finish_reason the same announcement is forwarded untouched and
        # the stream ends normally, so A's in-band error cannot be blamed
        # on tool calls being rejected outright.
        with self.stream_request("tool-args-complete") as response:
            self.assertEqual(response.status_code, 200)
            events, _ = self.read_to_eof(response)
        payloads = self.sse_payloads(events)
        deltas = self.tool_call_deltas(payloads)
        self.assertEqual(deltas, [("Bash", ""),
                                 (None, '{"command": "echo tool-ok"}')])
        self.assertEqual(events[-1], "data: [DONE]")
        self.assertFalse(any(
            "error" in payload
            for payload in payloads if isinstance(payload, dict)))
        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "ok")
        self.assertEqual(StubUpstreamHandler.posts, 1)

    def test_no_hidden_retry_after_tool_announcement(self):
        # C: with the transparent retry budget enabled, a failure after the
        # committed tool announcement must not be retried - the retry would
        # replay the announcement the client already saw, and a tool whose
        # arguments were already streamed could be re-run. The stub would
        # complete on a second attempt, so posts==1 and the absence of
        # [DONE] are the proof that no retry happened.
        with mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True), \
             mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 5):
            with self.stream_request("tool-args-truncated-then-ok") as response:
                self.assertEqual(response.status_code, 200)
                events, _ = self.read_to_eof(response)
        self.assertEqual(StubUpstreamHandler.posts, 1)
        self.assertNotIn("data: [DONE]", events)
        self.assert_in_band_error(events[-1], "upstream_read_error")

    def test_prelude_truncation_before_tool_announcement_is_retried_hidden(self):
        # D: the same failure before anything substantive (only the
        # metadata-only role chunk arrived, which the prelude holds back) is
        # still hidden-retryable, so the client sees one clean completion
        # and two upstream attempts happened. Paired with C: the commit
        # point, not the retry switch, is what stops the replay.
        with mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True), \
             mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 5):
            with self.stream_request("tool-prelude-truncated-then-ok") as response:
                self.assertEqual(response.status_code, 200)
                events, _ = self.read_to_eof(response)
        payloads = self.sse_payloads(events)
        self.assertEqual(StubUpstreamHandler.posts, 2)
        self.assertEqual(events[-1], "data: [DONE]")
        # The held-back role chunk is replayed exactly once: a retry that
        # replayed the first attempt's buffer would duplicate it.
        role_deltas = []
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            for choice in payload.get("choices") or []:
                delta = (choice or {}).get("delta") or {}
                if delta.get("role"):
                    role_deltas.append(delta["role"])
        self.assertEqual(role_deltas, ["assistant"])
        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "ok")

    def test_slow_class_truncation_after_announcement_is_not_retried(self):
        # E: the slow class has its own retry budget (the original 32
        # cases never touch the slow path, and F proves that budget is
        # reachable). A slow-class failure after the committed
        # announcement must still not be retried, so the commit gate holds
        # on both budgets - and because no retry decision is taken here,
        # no auto-retry line is logged at all.
        with mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True), \
             mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 5), \
             mock.patch.object(chat2api, "UPSTREAM_SLOW_AUTO_RETRIES", 5), \
             mock.patch.object(chat2api, "UPSTREAM_SLOW_RETRY_THRESHOLD_S", 0.2):
            with self.assertLogs("chat2api", level="INFO") as cm:
                with self.stream_request(
                        "slow-tool-args-truncated-then-ok") as response:
                    self.assertEqual(response.status_code, 200)
                    events, _ = self.read_to_eof(response)
        combined = "\n".join(cm.output)
        self.assertEqual(StubUpstreamHandler.posts, 1)
        self.assertNotIn("data: [DONE]", events)
        self.assert_in_band_error(events[-1], "upstream_read_error")
        self.assertNotIn("upstream auto-retry", combined)

    def test_slow_class_prelude_truncation_uses_slow_budget(self):
        # F: the same slow-class failure before anything substantive is
        # retried on the slow budget - the pair to E, so E's posts==1
        # cannot be explained by the slow path being unreachable.
        with mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True), \
             mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 5), \
             mock.patch.object(chat2api, "UPSTREAM_SLOW_AUTO_RETRIES", 5), \
             mock.patch.object(chat2api, "UPSTREAM_SLOW_RETRY_THRESHOLD_S", 0.2):
            with self.assertLogs("chat2api", level="WARNING") as cm:
                with self.stream_request(
                        "slow-tool-prelude-truncated-then-ok") as response:
                    self.assertEqual(response.status_code, 200)
                    events, _ = self.read_to_eof(response)
        combined = "\n".join(cm.output)
        self.assertEqual(StubUpstreamHandler.posts, 2)
        self.assertIn("slow_retry=1/5", combined)
        self.assertIn("class=slow", combined)
        self.assertEqual(events[-1], "data: [DONE]")


if __name__ == "__main__":
    unittest.main()
