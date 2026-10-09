# -*- coding: utf-8 -*-
"""Regression tests for the Open WebUI 0.11.4 explicit SSE error contract.

Open WebUI 0.11.4 changed its chat stream wrapper: when the chat completion
fails mid-flight it now emits a NORMAL SSE event ``data: {"error": {...}}``
followed by ``data: [DONE]`` instead of only closing the connection. The
chat SSE consumer of chat2api had no top-level error branch, so such an
event was treated as ordinary stream content: the 200 was committed for it
(or, without a finish_reason, the completion ended as
upstream_missing_finish_reason), and an error arriving after an earlier
finish_reason was recorded as a plain "ok" completion.

Contract pinned here:
  * an explicit error event recognized BEFORE the 200 is committed fails
    the request as the zero-content contract (one HTTP 502, no SSE body);
  * after headers/body went out it fails with exactly ONE in-band error of
    the existing compatible shape and never forwards a success [DONE];
  * the upstream error reason is stable, sanitized and length-bounded; the
    raw upstream object is never relayed (it may carry credential material);
  * an explicit error is a definitive failure: no transparent retry hides it;
  * one ledger row per request with a non-"ok" status and the observed lower
    bound of the output already streamed;
  * reader slot / producer thread are released through the single owner.

The Anthropic Messages branch already treats an upstream error event as
terminal (its byte-fidelity contract relays the frame in-band); the
messages test pins that existing behavior so the chat-side fix cannot bleed
into that branch.

The stub upstream mirrors the 0.11.4 wire shape. Isolation follows
tests/_chat2api_http_harness.py (B8-0): tracked servers drain every in-flight
request BEFORE the ledger redirect is released (private temp ledger, retry
budgets disabled by default, loopback-only stubs, no real network).
"""

import importlib.util
import json
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
SPEC = importlib.util.spec_from_file_location(
    "chat2api_webui_compat", CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)

STUB_TOKEN = "stub-token"

# The exact failure signature Open WebUI 0.11.4 sends mid-stream, followed
# by data: [DONE].
WEBUI_ERROR_MESSAGE = "Chat completion stream failed"

# A message long enough to overflow the bounded reason; the tail must never
# reach a client body or a log record.
OVERSIZED_MESSAGE = "E" * 5000

# Junk fields an upstream error object may carry; none of them may ever be
# echoed by chat2api (only the message text is extracted, bounded).
SECRET_FIELD_VALUES = ("SECRET-API-KEY-51be", "SECRET-SESSION-9a7c")

LOCAL_KEY = "local-webui-compat-key"


def _sse(payload: str) -> bytes:
    return f"data: {payload}\n\n".encode("utf-8")


def _chunk(content: str = None, finish_reason=None) -> str:
    delta = {}
    if content is not None:
        delta["content"] = content
    return json.dumps({"choices": [{"delta": delta,
                                    "finish_reason": finish_reason}]})


def _webui_error_event(message=WEBUI_ERROR_MESSAGE, **extra_fields) -> str:
    """The upstream error event exactly as Open WebUI 0.11.4 sends it."""
    error = {"message": message}
    error.update(extra_fields)
    return json.dumps({"error": error})


class StubUpstreamHandler(BaseHTTPRequestHandler):
    """Fake Open WebUI 0.11.x chat/messages endpoints keyed by model name."""

    protocol_version = "HTTP/1.1"
    posts = 0

    def log_message(self, *args):
        pass

    def _send(self, data: bytes):
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii")
                         + data + b"\r\n")
        self.wfile.flush()

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        scenario = body.get("model", "")
        if path == "/api/v1/messages":
            self._serve_messages()
            return
        StubUpstreamHandler.posts += 1
        if scenario == "error-nonstream-200":
            # 与流式同一失败形状，但以 200 JSON body 到达：非流式分支必须
            # 对称失败（502 + 净化原因），不得转发成成功补全、更不得记 ok。
            body_bytes = _webui_error_event().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.close_connection = True
            if scenario == "error-first":
                # 0.11.4 failure signature: the error is the FIRST event.
                self._send(_sse(_webui_error_event()))
                self._send(b"data: [DONE]\n\n")
                time.sleep(0.3)
            elif scenario == "comments-then-error":
                # Held non-event prelude bytes before the error: the error
                # must still fail the not-yet-committed stream as 502 and
                # the held bytes must never reach the client.
                self._send(b": keepalive ping\n\n")
                time.sleep(0.1)
                self._send(_sse(_webui_error_event()))
                self._send(b"data: [DONE]\n\n")
                time.sleep(0.3)
            elif scenario == "content-then-error":
                self._send(_sse(_chunk(content="hello world")))
                self._send(_sse(_webui_error_event()))
                self._send(b"data: [DONE]\n\n")
                time.sleep(0.3)
            elif scenario == "finish-then-error":
                # finish_reason arrives BEFORE the error: the explicit error
                # must still fail the stream, never be recorded as ok.
                self._send(_sse(_chunk(content="hello world")))
                self._send(_sse(_chunk(finish_reason="stop")))
                self._send(_sse(_webui_error_event()))
                self._send(b"data: [DONE]\n\n")
                time.sleep(0.3)
            elif scenario == "error-then-eof":
                # Used with retry budgets ENABLED: the error must fail the
                # request immediately instead of being retried as a transient
                # zero-output failure. The stub's second attempt is a clean
                # ok stream, so a hidden retry is observable as posts == 2.
                if StubUpstreamHandler.posts == 1:
                    self._send(_sse(_webui_error_event()))
                    time.sleep(0.2)
                else:
                    self._send(_sse(_chunk()))
                    self._send(_sse(_chunk(content="OK")))
                    self._send(_sse(_chunk(finish_reason="stop")))
                    self._send(b"data: [DONE]\n\n")
                    time.sleep(0.3)
            elif scenario == "error-oversized":
                self._send(_sse(_webui_error_event(OVERSIZED_MESSAGE)))
                self._send(b"data: [DONE]\n\n")
                time.sleep(0.3)
            elif scenario == "error-with-junk":
                self._send(_sse(_chunk(content="visible")))
                self._send(_sse(_webui_error_event(
                    "boom\nmulti\r\nline",
                    api_key=SECRET_FIELD_VALUES[0],
                    session=SECRET_FIELD_VALUES[1],
                    upstream={"authorization": SECRET_FIELD_VALUES[0]})))
                self._send(b"data: [DONE]\n\n")
                time.sleep(0.3)
            else:
                self._send(_sse(_chunk()))
                self._send(_sse(_chunk(finish_reason="stop")))
                self._send(b"data: [DONE]\n\n")
                time.sleep(0.3)
        except (ConnectionError, BrokenPipeError, OSError):
            pass  # chat2api cancelled the upstream read

    def _serve_messages(self):
        # Anthropic-protocol stream with a mid-stream error event. The
        # Messages branch relays upstream bytes verbatim (byte fidelity) and
        # treats the error event as terminal; these tests pin that existing
        # behavior so the chat-side fix cannot bleed into this branch.
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.close_connection = True
            self._send(b'event: message_start\ndata: {"type":"message_start"}'
                       b"\n\n")
            self._send(b'event: content_block_delta\ndata: {"type":'
                       b'"content_block_delta","delta":{"text":"hi"}}\n\n')
            self._send(b'event: error\ndata: {"type":"error","error":'
                       b'{"type":"api_error","message":"stream failed"}}'
                       b"\n\n")
            self._send(b'event: message_stop\ndata: {"type":"message_stop"}'
                       b"\n\n")
            time.sleep(0.3)
        except (ConnectionError, BrokenPipeError, OSError):
            pass


class WebUiCompatTestBase(unittest.TestCase):
    """Shared isolation: loopback stubs, private ledger, no retry budgets."""

    def setUp(self):
        # B8-0 isolation lifecycle (see _chat2api_http_harness): the ledger
        # redirect is registered FIRST so it is released LAST - after every
        # handler drained and every server closed. The pre-fix LIFO bug
        # released the patch while a post-response handler was still writing
        # record_usage, which is how test rows reached the PRODUCTION
        # data/usage-chat2api.jsonl on 2026-10-02 (this suite hit exactly
        # that leak on its pre-fix red run, hence the never-restored
        # assignment this replaces).
        self._tmp, self.usage_log = harness.install_usage_isolation(
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
        server = harness.TrackedHTTPServer(("127.0.0.1", 0),
                                           chat2api.Handler)
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
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", False),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 0),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRY", False),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRIES", 0),
        ):
            patch.start()
            self.addCleanup(patch.stop)

        chat2api._reader_slots = threading.BoundedSemaphore(
            chat2api.STREAM_MAX_CONCURRENT_READERS)
        StubUpstreamHandler.posts = 0
        self.slot_before = chat2api._reader_slots._value
        self.reader_baseline = self.reader_thread_count()

    @staticmethod
    def reader_thread_count():
        return sum(1 for t in threading.enumerate()
                   if t.name == "chat2api-upstream-reader")

    def wait_reader_slots_restored(self, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if chat2api._reader_slots._value >= self.slot_before:
                return
            time.sleep(0.05)
        self.fail(f"reader slots not restored within {timeout}s "
                  f"({chat2api._reader_slots._value} != {self.slot_before})")

    def wait_reader_threads_reclaimed(self, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.reader_thread_count() <= self.reader_baseline:
                return
            time.sleep(0.1)
        self.fail(f"reader threads not reclaimed within {timeout}s")

    def wait_usage_records(self, count=1, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            records = self.current_records()
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
    def read_to_eof(response, timeout_s=15):
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
        return events, time.monotonic() - started

    @staticmethod
    def data_payloads(events):
        payloads = []
        for event in events:
            values = [line[5:].lstrip() for line in event.split("\n")
                      if line.startswith("data:")]
            text = "\n".join(values)
            if text and text != "[DONE]":
                payloads.append(json.loads(text))
        return payloads


class TestChatNonStreamExplicitError(WebUiCompatTestBase):
    """OpenAI chat 非流式：200 body 携带显式 error 的对称失败（复核补齐）。

    流式的显式错误分支（W5-1）已钉死；本类钉死同一形状以 200 JSON body 到达
    非流式分支时的行为：终端失败、502 + 净化原因、绝不转发成成功补全、绝不
    记 ok 行（与其余终端上游失败同一条记账规则）。
    """

    def test_nonstream_200_error_body_fails_as_502_and_never_as_ok(self):
        with requests.post(
                f"{self.base}/v1/chat/completions",
                json={"model": "error-nonstream-200",
                      "messages": [{"role": "user", "content": "hi"}],
                      "stream": False, "max_tokens": 16},
                timeout=15) as response:
            self.assertEqual(response.status_code, 502)
            body = response.json()
        error = body["error"]
        self.assertEqual(error["type"], "upstream_explicit_error")
        self.assertEqual(error["detail"]["reason"], "upstream_explicit_error")
        # 只有净化、限长的原因外发；原始对象（可能携带凭据材料）不透传。
        self.assertEqual(error["detail"]["upstream_message"],
                         WEBUI_ERROR_MESSAGE)
        # 终端失败不记账：与其余非 200 上游失败同规则，没有 ok 行。
        self.assertEqual(self.current_records(), [])


class TestChatSseExplicitError(WebUiCompatTestBase):
    """OpenAI chat SSE stream: the top-level error branch (W5-1)."""

    def stream_request(self, model, timeout=15):
        return requests.post(
            f"{self.base}/v1/chat/completions",
            json={"model": model,
                  "messages": [{"role": "user", "content": "hi"}],
                  "stream": True, "max_tokens": 16},
            stream=True, timeout=timeout)

    def test_first_event_error_fails_as_zero_content_502(self):
        # The error event must be recognized BEFORE the first successful
        # response is committed: with no headers sent, the zero-content
        # failure contract answers one HTTP 502 and no SSE body at all.
        with self.stream_request("error-first") as response:
            self.assertEqual(response.status_code, 502)
            body = response.json()
        error = body["error"]
        self.assertEqual(error["type"], "upstream_stream_error")
        self.assertEqual(error["detail"]["reason"], "upstream_stream_error")
        self.assertEqual(error["detail"]["upstream_message"],
                         WEBUI_ERROR_MESSAGE)

        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "upstream_stream_error")
        # No output was ever streamed: the failure record carries no output.
        self.assertEqual(record["output_tokens"], 0)
        self.wait_reader_slots_restored()
        self.wait_reader_threads_reclaimed()

    def test_error_after_content_fails_in_band_without_done(self):
        # Content already reached the client, so the 200 stands; the explicit
        # error ends the stream with exactly ONE in-band error of the
        # existing compatible shape, never a success [DONE], and the raw
        # upstream error object is not relayed.
        with self.stream_request("content-then-error") as response:
            self.assertEqual(response.status_code, 200)
            events, _ = self.read_to_eof(response)
        payloads = self.data_payloads(events)
        self.assertNotIn("data: [DONE]", events)
        self.assertEqual(payloads[0]["choices"][0]["delta"]["content"],
                         "hello world")
        last = payloads[-1]["error"]
        self.assertEqual(last["type"], "upstream_stream_error")
        self.assertEqual(last["code"], "upstream_stream_error")
        self.assertEqual(last["detail"]["reason"], "upstream_stream_error")
        self.assertEqual(last["detail"]["upstream_message"],
                         WEBUI_ERROR_MESSAGE)
        # The raw upstream error frame is never relayed.
        self.assertNotIn({"error": {"message": WEBUI_ERROR_MESSAGE}},
                         payloads)

        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "upstream_stream_error")
        # "hello world" (11 chars) was delivered downstream: the failure row
        # keeps the observed lower bound instead of fabricating a full
        # success.
        self.assertEqual(record["output_tokens"], 2)
        self.wait_reader_slots_restored()
        self.wait_reader_threads_reclaimed()

    def test_error_after_finish_reason_is_not_recorded_ok(self):
        # A finish_reason observed EARLIER must not let the explicit error
        # pass as a completed stream: the error is terminal and the failure
        # is recorded, [DONE] is withheld.
        with self.stream_request("finish-then-error") as response:
            self.assertEqual(response.status_code, 200)
            events, _ = self.read_to_eof(response)
        payloads = self.data_payloads(events)
        self.assertTrue(any(
            p.get("choices") and p["choices"][0].get("finish_reason") == "stop"
            for p in payloads))
        self.assertNotIn("data: [DONE]", events)
        self.assertEqual(payloads[-1]["error"]["type"],
                         "upstream_stream_error")

        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "upstream_stream_error")
        self.assertNotEqual(record["status"], "ok")

    def test_explicit_error_is_not_transparently_retried(self):
        # With retry budgets ENABLED, an explicit upstream error must fail
        # immediately: it is a definitive failure, not a transient one a
        # hidden retry could mask. A second upstream attempt would be
        # observable as posts == 2 (and would deliver a clean completion).
        with mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True), \
                mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 5), \
                mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRY",
                                  True), \
                mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRIES",
                                  5):
            with self.stream_request("error-then-eof") as response:
                self.assertEqual(response.status_code, 502)
                body = response.json()
        self.assertEqual(body["error"]["type"], "upstream_stream_error")
        self.assertEqual(StubUpstreamHandler.posts, 1)
        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "upstream_stream_error")

    def test_error_reason_is_bounded_and_never_echoes_the_raw_object(self):
        # The relayed reason is the sanitized message text only: collapsed to
        # one line, bounded in length, with every other upstream field
        # (credential-shaped or nested) dropped.
        with self.stream_request("error-oversized") as response:
            self.assertEqual(response.status_code, 502)
            body = response.json()
        message = body["error"]["detail"]["upstream_message"]
        self.assertLessEqual(
            len(message), chat2api._UPSTREAM_EXPLICIT_ERROR_MESSAGE_MAX_CHARS)
        self.assertNotIn(OVERSIZED_MESSAGE, json.dumps(body))

        with self.stream_request("error-with-junk") as response:
            self.assertEqual(response.status_code, 200)
            events, _ = self.read_to_eof(response)
        body_text = "".join(events)
        payloads = self.data_payloads(events)
        last = payloads[-1]["error"]
        self.assertEqual(last["detail"]["upstream_message"], "boom multi line")
        for secret in SECRET_FIELD_VALUES:
            self.assertNotIn(secret, body_text)
        # The sanitized reason rides with the response only; the ledger rows
        # (one per request: oversized + junk) keep the failure status.
        records = self.wait_usage_records(count=2)
        self.assertEqual(records[-1]["status"], "upstream_stream_error")

    def test_held_prelude_bytes_are_dropped_on_precommit_error(self):
        # Comments held before the error are never committed: the stream
        # fails as 502 with an application/json body, and the held bytes
        # never reach the client.
        with self.stream_request("comments-then-error") as response:
            self.assertEqual(response.status_code, 502)
            body = response.json()
        self.assertEqual(body["error"]["type"], "upstream_stream_error")
        self.assertNotIn("keepalive ping", json.dumps(body))
        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "upstream_stream_error")


class TestAnthropicMessagesErrorPin(WebUiCompatTestBase):
    """The Anthropic branch already handles error events; pin it unchanged."""

    def setUp(self):
        super().setUp()
        self.server.require_local_api_key = True
        self.server.local_api_key = LOCAL_KEY

    def test_messages_stream_error_event_is_relayed_and_recorded(self):
        # Byte-fidelity contract: the upstream error frame is relayed in-band
        # (HTTP 200 stays), the stream is cut at the error event, and the
        # ledger records the failure once with the non-"ok" status.
        with requests.post(
                f"{self.base}/v1/messages",
                json={"model": "messages-stream",
                      "max_tokens": 16, "stream": True,
                      "messages": [{"role": "user", "content": "hi"}]},
                headers={"x-api-key": LOCAL_KEY}, stream=True,
                timeout=15) as response:
            self.assertEqual(response.status_code, 200)
            events, _ = self.read_to_eof(response)
        joined = "\n\n".join(events)
        self.assertIn('"message_start"', joined)
        self.assertIn('"text":"hi"', joined)
        self.assertIn('"type":"error"', joined)
        # The stream is cut AT the error event: the later message_stop is
        # never relayed, and no synthetic error frame is appended.
        self.assertNotIn("message_stop", joined)
        (record,) = self.wait_usage_records()
        self.assertEqual(record["status"], "upstream_stream_error")
        self.assertTrue(record["stream"])
        self.wait_reader_slots_restored()


if __name__ == "__main__":
    unittest.main()
