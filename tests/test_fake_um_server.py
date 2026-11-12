"""Contract tests for the verification-only fake UM upstream.

The fake server is loaded by path (the repository has no ``tests`` package)
and is driven with stdlib ``http.client``. No product code is imported and no
real UM is contacted.
"""

import http.client
import importlib.util
import json
import os
import secrets
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_FAKE_UM_PATH = (Path(__file__).resolve().parent
                 / "fake_um" / "fake_um_server.py")
_SPEC = importlib.util.spec_from_file_location("fake_um_server", _FAKE_UM_PATH)
assert _SPEC is not None and _SPEC.loader is not None
fake_um = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = fake_um
_SPEC.loader.exec_module(fake_um)

_UNSET = object()


def _request(fake, method, path, *, body=_UNSET, raw_body=None, token=None,
              headers=None, timeout=10.0):
    """One HTTP request; returns (status, lowercased headers, body bytes)."""
    connection = http.client.HTTPConnection(fake.host, fake.port,
                                           timeout=timeout)
    request_headers = dict(headers or {})
    payload = raw_body
    if payload is None and body is not _UNSET:
        payload = json.dumps(body).encode("utf-8")
    if payload is not None:
        request_headers.setdefault("Content-Type", "application/json")
        request_headers["Content-Length"] = str(len(payload))
    if token:
        request_headers["Authorization"] = "Bearer " + token
    try:
        connection.request(method, path, body=payload,
                           headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        return (response.status,
                {name.lower(): value for name, value in response.getheaders()},
                data)
    finally:
        connection.close()


def _parse_sse(raw):
    """Split an SSE body into events (event, joined data, comment flag)."""
    text = raw.decode("utf-8").replace("\r\n", "\n")
    events = []
    for block in text.split("\n\n"):
        if not block:
            continue
        event_name = None
        data_lines = []
        comment_only = True
        for line in block.split("\n"):
            if line.startswith(":"):
                continue
            comment_only = False
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                value = line[len("data:"):]
                if value.startswith(" "):
                    value = value[1:]
                data_lines.append(value)
        events.append({"event": event_name,
                       "data": "\n".join(data_lines),
                       "comment_only": comment_only})
    return events


def _chat_payloads(events):
    return [json.loads(event["data"]) for event in events
            if event["data"] and event["data"] != "[DONE]"]


def _chat_text(payloads):
    pieces = []
    for payload in payloads:
        for choice in payload.get("choices") or []:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str):
                pieces.append(content)
    return "".join(pieces)


def _chat_finish_reasons(payloads):
    reasons = []
    for payload in payloads:
        for choice in payload.get("choices") or []:
            reason = choice.get("finish_reason")
            if reason:
                reasons.append(reason)
    return reasons


def _has_done(events):
    return any(event["data"] == "[DONE]" for event in events)


def _barrier(fake):
    status, _headers, raw = _request(fake, "GET", "/__control/barrier")
    if status != 200:
        raise AssertionError("barrier state returned %r" % (status,))
    return json.loads(raw)


def _wait_barrier(fake, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = _barrier(fake)
        if predicate(last):
            return last
        time.sleep(0.02)
    raise AssertionError("barrier state did not match: %r" % (last,))


class FakeUMTestBase(unittest.TestCase):
    def setUp(self):
        self.fake = fake_um.FakeUM(port=0, chunk_delay_s=0.0,
                                  slow_first_chunk_s=0.6, silent_s=0.4)
        self.fake.start()
        self.addCleanup(self.fake.stop)
        self.token = self.fake.token

    def chat_body(self, marker, stream):
        return {
            "model": "fake-um-model",
            "stream": stream,
            "messages": [{"role": "user", "content": marker}],
        }

    def messages_body(self, marker, stream):
        return {
            "model": "fake-um-model",
            "max_tokens": 32,
            "stream": stream,
            "messages": [{"role": "user", "content": marker}],
        }


class TestPublicAndAuthEndpoints(FakeUMTestBase):
    def test_config_is_public_and_reports_api_keys(self):
        status, headers, raw = _request(self.fake, "GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["content-type"])
        payload = json.loads(raw)
        self.assertIsInstance(payload["version"], str)
        self.assertTrue(payload["features"]["enable_api_keys"])

    def test_config_ignores_forced_invalid_auth_mode(self):
        # /api/config is public in Open WebUI; the invalid_auth mode models a
        # rejected CREDENTIAL, and this endpoint never carries one.
        status, _headers, _raw = _request(
            self.fake, "GET", "/api/config",
            headers={fake_um.MODE_HEADER: fake_um.MODE_INVALID_AUTH})
        self.assertEqual(status, 200)

    def test_config_explicit_5xx_mode_is_a_real_error(self):
        status, _headers, raw = _request(
            self.fake, "GET", "/api/config",
            headers={fake_um.MODE_HEADER: fake_um.MODE_EXPLICIT_5XX})
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(raw)["detail"], "fake upstream failure")

    def test_models_requires_bearer(self):
        status, _headers, raw = _request(self.fake, "GET", "/api/models")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(raw)["detail"], "Not authenticated")

        status, _headers, raw = _request(self.fake, "GET", "/api/models",
                                         token="wrong-token")
        self.assertEqual(status, 401)

        status, _headers, raw = _request(self.fake, "GET", "/api/models",
                                         token=self.token)
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertIsInstance(payload["data"], list)
        self.assertEqual(payload["data"][0]["id"], self.fake.config.model)

    def test_models_v1_alias_matches_catalog(self):
        status, _headers, raw = _request(self.fake, "GET", "/api/v1/models",
                                         token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["data"][0]["id"],
                         self.fake.config.model)

    def test_session_validation_endpoint(self):
        status, _headers, raw = _request(self.fake, "GET", "/api/v1/auths/",
                                         token=self.token)
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(payload["role"], "admin")
        self.assertNotIn("token", payload)

        status, _headers, _raw = _request(self.fake, "GET", "/api/v1/auths/",
                                          token="wrong-token")
        self.assertEqual(status, 401)

        status, _headers, _raw = _request(self.fake, "GET", "/api/v1/auths/")
        self.assertEqual(status, 401)

    def test_api_key_endpoint_get_and_post(self):
        status, _headers, raw = _request(
            self.fake, "GET", "/api/v1/auths/api_key", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["api_key"], self.fake.api_key)

        status, _headers, raw = _request(
            self.fake, "POST", "/api/v1/auths/api_key", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["api_key"], self.fake.api_key)

        status, _headers, _raw = _request(
            self.fake, "GET", "/api/v1/auths/api_key", token="wrong-token")
        self.assertEqual(status, 401)

    def test_unknown_path_is_404(self):
        status, _headers, raw = _request(self.fake, "GET", "/api/unknown",
                                         token=self.token)
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(raw)["detail"], "Not Found")

        status, _headers, _raw = _request(self.fake, "POST", "/api/unknown",
                                         body={"a": 1}, token=self.token)
        self.assertEqual(status, 404)

    def test_bad_request_body_is_400(self):
        status, _headers, raw = _request(
            self.fake, "POST", "/api/chat/completions",
            raw_body=b"not-json", token=self.token)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw)["detail"], "invalid JSON body")

    def test_missing_messages_is_422(self):
        status, _headers, raw = _request(
            self.fake, "POST", "/api/chat/completions",
            body={"model": "m", "stream": False}, token=self.token)
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(raw)["detail"], "messages must be a list")

    def test_unknown_mode_header_is_400(self):
        status, _headers, raw = _request(
            self.fake, "POST", "/api/chat/completions",
            body=self.chat_body("marker", True), token=self.token,
            headers={fake_um.MODE_HEADER: "no-such-mode"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw)["detail"], "unknown mode")


class TestChatCompletions(FakeUMTestBase):
    def test_nonstream_json_has_finish_reason(self):
        status, headers, raw = _request(
            self.fake, "POST", "/api/chat/completions",
            body=self.chat_body("hello-json", False), token=self.token)
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["content-type"])
        payload = json.loads(raw)
        choice = payload["choices"][0]
        self.assertEqual(choice["message"]["content"], "hello-json")
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertEqual(payload["usage"]["total_tokens"],
                         payload["usage"]["prompt_tokens"]
                         + payload["usage"]["completion_tokens"])

    def test_stream_terminal_shape(self):
        status, headers, raw = _request(
            self.fake, "POST", "/api/chat/completions",
            body=self.chat_body("hello-stream", True), token=self.token)
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["content-type"])
        events = _parse_sse(raw)
        payloads = _chat_payloads(events)
        self.assertEqual(_chat_text(payloads), "hello-stream")
        self.assertEqual(_chat_finish_reasons(payloads), ["stop"])
        self.assertTrue(_has_done(events))
        # The success terminal is the LAST event: no data after [DONE].
        self.assertEqual(events[-1]["data"], "[DONE]")
        self.assertEqual(events[-1]["event"], None)

    def test_stream_missing_terminal_keeps_content_without_sentinel(self):
        status, _headers, raw = _request(
            self.fake, "POST", "/api/chat/completions",
            body=self.chat_body("truncated", True), token=self.token,
            headers={fake_um.MODE_HEADER: fake_um.MODE_MISSING_TERMINAL})
        self.assertEqual(status, 200)
        events = _parse_sse(raw)
        payloads = _chat_payloads(events)
        self.assertEqual(_chat_text(payloads), "truncated")
        self.assertEqual(_chat_finish_reasons(payloads), [])
        self.assertFalse(_has_done(events))

    def test_stream_comments_only_has_no_data(self):
        status, _headers, raw = _request(
            self.fake, "POST", "/api/chat/completions",
            body=self.chat_body("ignored", True), token=self.token,
            headers={fake_um.MODE_HEADER: fake_um.MODE_COMMENTS_ONLY})
        self.assertEqual(status, 200)
        events = _parse_sse(raw)
        self.assertTrue(events)
        self.assertTrue(all(event["comment_only"] for event in events))
        self.assertEqual(_chat_payloads(events), [])

    def test_stream_silent_returns_empty_body(self):
        started = time.monotonic()
        status, _headers, raw = _request(
            self.fake, "POST", "/api/chat/completions",
            body=self.chat_body("ignored", True), token=self.token,
            headers={fake_um.MODE_HEADER: fake_um.MODE_SILENT})
        elapsed = time.monotonic() - started
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"")
        self.assertGreaterEqual(elapsed, 0.3)

    def test_stream_slow_first_chunk_delays_data(self):
        started = time.monotonic()
        status, _headers, raw = _request(
            self.fake, "POST", "/api/chat/completions",
            body=self.chat_body("slow", True), token=self.token,
            headers={fake_um.MODE_HEADER: fake_um.MODE_SLOW_FIRST_CHUNK})
        elapsed = time.monotonic() - started
        self.assertEqual(status, 200)
        self.assertGreaterEqual(elapsed, 0.5)
        events = _parse_sse(raw)
        self.assertEqual(_chat_text(_chat_payloads(events)), "slow")
        self.assertTrue(_has_done(events))

    def test_explicit_5xx_is_an_error_not_sse(self):
        status, headers, raw = _request(
            self.fake, "POST", "/api/chat/completions",
            body=self.chat_body("ignored", True), token=self.token,
            headers={fake_um.MODE_HEADER: fake_um.MODE_EXPLICIT_5XX})
        self.assertEqual(status, 500)
        self.assertIn("application/json", headers["content-type"])
        self.assertEqual(json.loads(raw)["detail"], "fake upstream failure")

    def test_explicit_stream_error_has_no_done(self):
        status, _headers, raw = _request(
            self.fake, "POST", "/api/chat/completions",
            body=self.chat_body("ignored", True), token=self.token,
            headers={fake_um.MODE_HEADER:
                     fake_um.MODE_EXPLICIT_STREAM_ERROR})
        self.assertEqual(status, 200)
        events = _parse_sse(raw)
        payloads = _chat_payloads(events)
        self.assertTrue(any("error" in payload for payload in payloads))
        self.assertEqual(_chat_finish_reasons(payloads), [])
        self.assertFalse(_has_done(events))

    def test_nonstream_request_ignores_stream_only_mode(self):
        status, _headers, raw = _request(
            self.fake, "POST", "/api/chat/completions",
            body=self.chat_body("still-json", False), token=self.token,
            headers={fake_um.MODE_HEADER: fake_um.MODE_MISSING_TERMINAL})
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")
        self.assertEqual(payload["choices"][0]["message"]["content"],
                         "still-json")

    def test_invalid_auth_mode_forces_401(self):
        status, _headers, raw = _request(
            self.fake, "POST", "/api/chat/completions",
            body=self.chat_body("ignored", True), token=self.token,
            headers={fake_um.MODE_HEADER: fake_um.MODE_INVALID_AUTH})
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(raw)["detail"], "Not authenticated")

    def test_chat_requires_bearer(self):
        status, _headers, _raw = _request(
            self.fake, "POST", "/api/chat/completions",
            body=self.chat_body("ignored", True))
        self.assertEqual(status, 401)


class TestAnthropicMessages(FakeUMTestBase):
    def test_nonstream_message_body(self):
        status, _headers, raw = _request(
            self.fake, "POST", "/api/v1/messages",
            body=self.messages_body("hello-anthropic", False),
            token=self.token)
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(payload["type"], "message")
        self.assertEqual(payload["role"], "assistant")
        self.assertEqual(payload["content"][0]["text"], "hello-anthropic")
        self.assertEqual(payload["stop_reason"], "end_turn")
        self.assertGreater(payload["usage"]["output_tokens"], 0)

    def test_stream_event_order_and_terminal(self):
        status, headers, raw = _request(
            self.fake, "POST", "/api/v1/messages",
            body=self.messages_body("anthropic-stream", True),
            token=self.token)
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["content-type"])
        events = _parse_sse(raw)
        names = [event["event"] for event in events]
        self.assertEqual(names[0], "message_start")
        self.assertEqual(names[1], "content_block_start")
        self.assertEqual(names[-1], "message_stop")
        self.assertIn("message_delta", names)
        self.assertEqual(names.count("content_block_stop"), 1)

        payloads = [json.loads(event["data"]) for event in events]
        self.assertEqual(payloads[0]["message"]["content"], [])
        self.assertEqual(payloads[0]["message"]["usage"]["input_tokens"] > 0,
                         True)
        text = "".join(
            payload["delta"]["text"]
            for payload in payloads
            if payload.get("type") == "content_block_delta")
        self.assertEqual(text, "anthropic-stream")

    def test_stream_missing_message_stop(self):
        status, _headers, raw = _request(
            self.fake, "POST", "/api/v1/messages",
            body=self.messages_body("truncated", True), token=self.token,
            headers={fake_um.MODE_HEADER: fake_um.MODE_MISSING_TERMINAL})
        self.assertEqual(status, 200)
        events = _parse_sse(raw)
        names = [event["event"] for event in events]
        self.assertNotIn("message_stop", names)
        # message_delta is NOT a terminal event; it is the last thing seen.
        self.assertEqual(names[-1], "message_delta")
        text = "".join(
            json.loads(event["data"])["delta"]["text"]
            for event in events
            if event["event"] == "content_block_delta")
        self.assertEqual(text, "truncated")

    def test_stream_comments_only(self):
        status, _headers, raw = _request(
            self.fake, "POST", "/api/v1/messages",
            body=self.messages_body("ignored", True), token=self.token,
            headers={fake_um.MODE_HEADER: fake_um.MODE_COMMENTS_ONLY})
        self.assertEqual(status, 200)
        events = _parse_sse(raw)
        self.assertTrue(events)
        self.assertTrue(all(event["comment_only"] for event in events))

    def test_messages_requires_bearer(self):
        status, _headers, _raw = _request(
            self.fake, "POST", "/api/v1/messages",
            body=self.messages_body("ignored", False), token="wrong-token")
        self.assertEqual(status, 401)


class TestControlEndpoints(FakeUMTestBase):
    def test_control_mode_switch_applies_to_following_requests(self):
        status, _headers, raw = _request(
            self.fake, "POST", "/__control/mode",
            body={"mode": fake_um.MODE_MISSING_TERMINAL})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["mode"],
                         fake_um.MODE_MISSING_TERMINAL)
        self.assertEqual(self.fake.mode, fake_um.MODE_MISSING_TERMINAL)

        status, _headers, raw = _request(
            self.fake, "POST", "/api/chat/completions",
            body=self.chat_body("after-switch", True), token=self.token)
        self.assertEqual(status, 200)
        self.assertFalse(_has_done(_parse_sse(raw)))

        status, _headers, raw = _request(
            self.fake, "POST", "/__control/mode",
            body={"mode": fake_um.MODE_NORMAL})
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.mode, fake_um.MODE_NORMAL)

        status, _headers, raw = _request(
            self.fake, "POST", "/api/chat/completions",
            body=self.chat_body("back-to-normal", True), token=self.token)
        self.assertTrue(_has_done(_parse_sse(raw)))

    def test_control_rejects_unknown_mode(self):
        status, _headers, raw = _request(
            self.fake, "POST", "/__control/mode", body={"mode": "bogus"})
        self.assertEqual(status, 400)
        self.assertIn("unknown mode", json.loads(raw)["detail"])

    def test_control_state_reports_mode_and_count(self):
        _request(self.fake, "GET", "/api/models", token=self.token)
        status, _headers, raw = _request(
            self.fake, "GET", "/__control/state")
        self.assertEqual(status, 200)
        state = json.loads(raw)
        self.assertEqual(state["mode"], fake_um.MODE_NORMAL)
        self.assertEqual(state["model"], self.fake.config.model)
        self.assertGreaterEqual(state["request_count"], 1)
        self.assertIn("modes", state)

    def test_control_state_never_exposes_credentials(self):
        _request(self.fake, "POST", "/api/chat/completions",
                 body=self.chat_body("credential-probe", True),
                 token=self.token)
        _request(self.fake, "GET", "/api/models", token="wrong-token")
        status, _headers, raw = _request(
            self.fake, "GET", "/__control/state")
        self.assertEqual(status, 200)
        text = raw.decode("utf-8")
        self.assertNotIn(self.token, text)
        self.assertNotIn(self.fake.api_key, text)
        self.assertNotIn("authorization", text.lower())

    def test_control_reset_restores_default_mode(self):
        _request(self.fake, "POST", "/__control/mode",
                 body={"mode": fake_um.MODE_SILENT})
        status, _headers, raw = _request(
            self.fake, "POST", "/__control/reset")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["mode"], fake_um.MODE_NORMAL)
        self.assertEqual(self.fake.mode, fake_um.MODE_NORMAL)

    def test_control_method_mismatch_is_405(self):
        status, _headers, _raw = _request(
            self.fake, "GET", "/__control/mode")
        self.assertEqual(status, 405)


class TestBarrierControl(FakeUMTestBase):
    """The verification-only arrival barrier: hold, record, release."""

    def _arm(self, protocols=None):
        status, _headers, raw = _request(
            self.fake, "POST", "/__control/barrier/arm",
            body={"protocols": protocols or []})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["enabled"])

    def _disarm(self):
        _request(self.fake, "POST", "/__control/barrier/disarm", body={})

    def test_barrier_is_disabled_by_default(self):
        state = _barrier(self.fake)
        self.assertFalse(state["enabled"])
        self.assertEqual(state["arrivals"], [])
        # A normal request is not held and keeps its exact SSE contract.
        status, _headers, raw = _request(
            self.fake, "POST", "/api/chat/completions",
            body=self.chat_body("normal-mode", True), token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(_has_done(_parse_sse(raw)))
        self.assertEqual(_barrier(self.fake)["arrivals"], [])

    def test_barrier_holds_multiple_requests_and_records_arrivals(self):
        self._arm(["chat"])
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            futures = [
                pool.submit(
                    _request, self.fake, "POST", "/api/chat/completions",
                    body=self.chat_body("chain key=A seq=1", False),
                    token=self.token, timeout=30.0),
                pool.submit(
                    _request, self.fake, "POST", "/api/chat/completions",
                    body=self.chat_body("chain key=B seq=1", False),
                    token=self.token, timeout=30.0),
            ]
            state = _wait_barrier(
                self.fake, lambda item: len(item["arrivals"]) == 2)
            # Both requests are genuinely in flight at the upstream.
            self.assertFalse(futures[0].done())
            self.assertFalse(futures[1].done())
            self.assertEqual([entry["order"] for entry in state["arrivals"]],
                             [1, 2])
            self.assertEqual([entry["requestId"] for entry in state["arrivals"]],
                             ["arrival-0001", "arrival-0002"])
            by_key = {entry["keyMarker"]: entry
                      for entry in state["arrivals"]}
            self.assertEqual(sorted(by_key), ["A", "B"])
            first = by_key["A"]
            self.assertEqual(first["protocol"], "chat")
            self.assertEqual(first["model"], "fake-um-model")
            self.assertEqual(first["sequence"], 1)
            self.assertEqual(
                first["body"]["messages"][0]["content"],
                "chain key=A seq=1")
            self.assertEqual(
                by_key["B"]["body"]["messages"][0]["content"],
                "chain key=B seq=1")
            text = json.dumps(state)
            self.assertNotIn(self.token, text)
            self.assertNotIn("authorization", text.lower())
            self.assertNotIn(self.fake.api_key, text)

            status, _headers, raw = _request(
                self.fake, "POST", "/__control/barrier/release",
                body={"all": True})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)["released"], 2)
            for future in futures:
                status, _headers, raw = future.result(timeout=10)
                self.assertEqual(status, 200)
                self.assertTrue(json.loads(raw)["choices"])
            self.assertEqual(
                [entry["held"] for entry in
                 _barrier(self.fake)["arrivals"]], [True, True])
        finally:
            self._disarm()
            pool.shutdown(wait=True)

    def test_barrier_release_by_arrival_id(self):
        self._arm(["chat"])
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(
                _request, self.fake, "POST", "/api/chat/completions",
                body=self.chat_body("release-one", False),
                token=self.token, timeout=30.0)
            state = _wait_barrier(
                self.fake, lambda item: len(item["arrivals"]) == 1)
            arrival_id = state["arrivals"][0]["arrivalId"]
            status, _headers, raw = _request(
                self.fake, "POST", "/__control/barrier/release",
                body={"arrivals": [arrival_id]})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)["released"], 1)
            status, _headers, _raw = future.result(timeout=10)
            self.assertEqual(status, 200)
        finally:
            self._disarm()
            pool.shutdown(wait=True)

    def test_barrier_disarm_releases_held_requests(self):
        self._arm(["chat"])
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(
                _request, self.fake, "POST", "/api/chat/completions",
                body=self.chat_body("disarm", False),
                token=self.token, timeout=30.0)
            _wait_barrier(self.fake,
                          lambda item: len(item["arrivals"]) == 1)
            status, _headers, raw = _request(
                self.fake, "POST", "/__control/barrier/disarm", body={})
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(raw)["enabled"])
            status, _headers, _raw = future.result(timeout=10)
            self.assertEqual(status, 200)
        finally:
            self._disarm()
            pool.shutdown(wait=True)

    def test_barrier_protocol_filter_holds_only_selected_protocol(self):
        self._arm(["messages"])
        status, _headers, raw = _request(
            self.fake, "POST", "/api/chat/completions",
            body=self.chat_body("chat-not-selected", False), token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(_barrier(self.fake)["arrivals"], [])

        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(
                _request, self.fake, "POST", "/api/v1/messages",
                body=self.messages_body("messages-selected", False),
                token=self.token, timeout=30.0)
            state = _wait_barrier(
                self.fake, lambda item: len(item["arrivals"]) == 1)
            self.assertEqual(state["arrivals"][0]["protocol"], "messages")
            self.assertEqual(state["arrivals"][0]["path"], "/api/v1/messages")
            status, _headers, _raw = _request(
                self.fake, "POST", "/__control/barrier/release",
                body={"all": True})
            self.assertEqual(status, 200)
            status, _headers, _raw = future.result(timeout=10)
            self.assertEqual(status, 200)
        finally:
            self._disarm()
            pool.shutdown(wait=True)

    def test_barrier_reset_releases_and_clears_arrivals(self):
        self._arm(["chat"])
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(
                _request, self.fake, "POST", "/api/chat/completions",
                body=self.chat_body("reset-held", False),
                token=self.token, timeout=30.0)
            _wait_barrier(self.fake,
                          lambda item: len(item["arrivals"]) == 1)
            status, _headers, _raw = _request(
                self.fake, "POST", "/__control/reset", body={})
            self.assertEqual(status, 200)
            state = _barrier(self.fake)
            self.assertFalse(state["enabled"])
            self.assertEqual(state["arrivals"], [])
            status, _headers, _raw = future.result(timeout=10)
            self.assertEqual(status, 200)
        finally:
            self._disarm()
            pool.shutdown(wait=True)

    def test_barrier_timeout_is_recorded_and_response_still_completes(self):
        self.fake.config.barrier_timeout_s = 0.1
        self._arm(["chat"])
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(
                _request, self.fake, "POST", "/api/chat/completions",
                body=self.chat_body("barrier-timeout", False),
                token=self.token, timeout=30.0)
            state = _wait_barrier(
                self.fake,
                lambda item: (len(item["arrivals"]) == 1
                              and item["timedOut"] >= 1))
            self.assertTrue(state["arrivals"][0]["timedOut"])
            status, _headers, raw = future.result(timeout=10)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(raw)["choices"])
        finally:
            self._disarm()
            pool.shutdown(wait=True)

    def test_barrier_rejects_unknown_protocol(self):
        status, _headers, raw = _request(
            self.fake, "POST", "/__control/barrier/arm",
            body={"protocols": ["bogus"]})
        self.assertEqual(status, 400)
        self.assertIn("unknown barrier protocol",
                      json.loads(raw)["detail"])

    def test_barrier_release_rejects_invalid_arrivals(self):
        self._arm(["chat"])
        status, _headers, raw = _request(
            self.fake, "POST", "/__control/barrier/release",
            body={"arrivals": ["not-an-id"]})
        self.assertEqual(status, 400)
        self.assertIn("arrivals must be a list of integers",
                      json.loads(raw)["detail"])
        self._disarm()

    def test_barrier_method_mismatch_is_405(self):
        status, _headers, _raw = _request(
            self.fake, "GET", "/__control/barrier/arm")
        self.assertEqual(status, 405)


class TestTokenLifecycle(FakeUMTestBase):
    def test_rotate_token_invalidates_the_old_bearer(self):
        old_token = self.fake.token
        status, _headers, raw = _request(
            self.fake, "POST", "/__control/rotate_token")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["rotated"])
        new_token = self.fake.token
        self.assertNotEqual(old_token, new_token)

        status, _headers, _raw = _request(
            self.fake, "GET", "/api/models", token=old_token)
        self.assertEqual(status, 401)

        status, _headers, _raw = _request(
            self.fake, "GET", "/api/models", token=new_token)
        self.assertEqual(status, 200)

    def test_rotate_response_does_not_carry_the_new_token(self):
        status, _headers, raw = _request(
            self.fake, "POST", "/__control/rotate_token")
        self.assertEqual(status, 200)
        self.assertNotIn(self.fake.token, raw.decode("utf-8"))

    def test_token_from_environment_is_honoured(self):
        # The config field wins, but the env fallback is the documented source
        # for the manual run path; verify the resolver order in-process.
        env_token = secrets.token_urlsafe(16)
        explicit_token = secrets.token_urlsafe(16)
        previous = os.environ.get(fake_um.TOKEN_ENV)
        os.environ[fake_um.TOKEN_ENV] = env_token
        try:
            fake = fake_um.FakeUM(port=0)
            self.assertEqual(fake.token, env_token)
            explicit = fake_um.FakeUM(port=0, token=explicit_token)
            self.assertEqual(explicit.token, explicit_token)
        finally:
            if previous is None:
                os.environ.pop(fake_um.TOKEN_ENV, None)
            else:
                os.environ[fake_um.TOKEN_ENV] = previous


class TestConcurrencyIsolation(FakeUMTestBase):
    def test_per_request_modes_do_not_cross_talk(self):
        def call(index):
            marker = "marker%02d" % index
            mode = (fake_um.MODE_MISSING_TERMINAL if index % 2
                    else fake_um.MODE_NORMAL)
            status, _headers, raw = _request(
                self.fake, "POST", "/api/chat/completions",
                body=self.chat_body(marker, True), token=self.token,
                headers={fake_um.MODE_HEADER: mode})
            return marker, mode, status, raw

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(call, range(16)))

        for marker, mode, status, raw in results:
            self.assertEqual(status, 200)
            events = _parse_sse(raw)
            payloads = _chat_payloads(events)
            self.assertEqual(_chat_text(payloads), marker)
            if mode == fake_um.MODE_NORMAL:
                self.assertEqual(_chat_finish_reasons(payloads), ["stop"])
                self.assertTrue(_has_done(events))
            else:
                self.assertEqual(_chat_finish_reasons(payloads), [])
                self.assertFalse(_has_done(events))
            for other in ("marker%02d" % index for index in range(16)
                          if index != int(marker[-2:])):
                self.assertNotIn(other, raw.decode("utf-8"))

    def test_anthropic_concurrent_markers_are_isolated(self):
        def call(index):
            marker = "anthropic%02d" % index
            status, _headers, raw = _request(
                self.fake, "POST", "/api/v1/messages",
                body=self.messages_body(marker, True), token=self.token)
            return marker, status, raw

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(call, range(12)))

        for marker, status, raw in results:
            self.assertEqual(status, 200)
            events = _parse_sse(raw)
            self.assertEqual(events[-1]["event"], "message_stop")
            text = "".join(
                json.loads(event["data"])["delta"]["text"]
                for event in events
                if event["event"] == "content_block_delta")
            self.assertEqual(text, marker)
            for other in ("anthropic%02d" % index for index in range(12)
                          if index != int(marker[-2:])):
                self.assertNotIn(other, raw.decode("utf-8"))


class TestDefaults(unittest.TestCase):
    def test_default_port_is_28999(self):
        self.assertEqual(fake_um.DEFAULT_PORT, 28999)
        self.assertEqual(fake_um.FakeUMConfig().port, 28999)

    def test_non_loopback_bind_is_refused(self):
        with self.assertRaises(fake_um.FakeUMError):
            fake_um.FakeUM(host="0.0.0.0", port=0)
        with self.assertRaises(fake_um.FakeUMError):
            fake_um.FakeUM(host="192.168.1.10", port=0)


if __name__ == "__main__":
    unittest.main()
