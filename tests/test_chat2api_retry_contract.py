# -*- coding: utf-8 -*-
"""Retry matrix and Retry-After contract (ticket #61).

The gateway has ONE upstream candidate, so retry is always the same request:
transient statuses (408/429/5xx/529/520-524) use the fast/slow budgets,
402/403 get one probe each, 400/422 get one conditional probe only when the
upstream text names a transient condition, and 404/405/409/413/415 never retry.
A Retry-After above the 8s local wait budget is returned to the client with
the original status instead of being slept off; X-Should-Retry: false stops the
retry; Responses non-200 uses the same matrix. A final transport failure is 502,
and a retry wait that hits the 540s wall budget is 504 with the last status.
"""

import importlib.util
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _chat2api_http_harness as harness  # noqa: E402
from fake_um.fake_um_server import (  # noqa: E402
    FakeUM,
    MODE_DISCONNECT_AFTER_FIRST,
    MODE_DISCONNECT_BEFORE_FIRST,
    MODE_EXPLICIT_STREAM_ERROR,
    MODE_EXPLICIT_STREAM_ERROR_AFTER_FIRST,
    MODE_MISSING_TERMINAL,
    MODE_NORMAL,
    MODE_SILENT,
)

CHAT2API_PATH = Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py"
SPEC = importlib.util.spec_from_file_location(
    "chat2api_retry_contract", CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)

STUB_TOKEN = "stub-token"


class RetryContractTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir, self.usage_log = harness.install_usage_isolation(
            self, chat2api)
        harness.assert_stub_credentials(
            chat2api, local_key=None, upstream_token=STUB_TOKEN,
            stub_token_literal=STUB_TOKEN)

        self.fake = FakeUM(port=0, token=STUB_TOKEN, chunk_delay_s=0.0)
        self.fake.start()
        self.addCleanup(self.fake.stop)

        args = SimpleNamespace(
            base_url=self.fake.base_url,
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
        self.base = f"http://127.0.0.1:{server.server_port}"

        for patch in (
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 5),
            mock.patch.object(chat2api, "UPSTREAM_SLOW_AUTO_RETRIES", 2),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRY", False),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRIES", 0),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        chat2api._reader_slots = threading.BoundedSemaphore(
            chat2api.STREAM_MAX_CONCURRENT_READERS)

    def script(self, entries):
        self.fake.set_script(entries)
        self.fake.set_mode("scripted")

    def no_delay(self):
        return mock.patch.object(
            chat2api, "_retry_delay_seconds", return_value=0.0)

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

    def post_responses(self, stream=False, timeout=15, body=None):
        if body is None:
            body = {"model": "stub-model", "input": "hi", "stream": stream}
        return requests.post(
            f"{self.base}/v1/responses",
            json=body,
            timeout=timeout, stream=stream)

    def assert_posts(self, count):
        self.assertEqual(self.fake.state()["request_count"], count)

    def assert_path_posts(self, path, count):
        recent = self.fake.state()["recent"]
        self.assertEqual(
            sum(1 for entry in recent if entry["path"] == path), count)


class RetryMatrixTests(RetryContractTestBase):
    def test_default_total_budget_is_540_seconds(self):
        self.assertEqual(chat2api.MAX_REQUEST_SECONDS, 540)

    def test_retryable_status_set_matches_adr_0003(self):
        for status in (408, 429, 500, 502, 503, 504, 520, 521, 522, 523,
                       524, 529):
            self.assertIn(status, chat2api._UPSTREAM_RETRYABLE_STATUS)
        for status in (401, 402, 403, 404, 405, 409, 413, 415, 400, 422):
            self.assertNotIn(status, chat2api._UPSTREAM_RETRYABLE_STATUS)

    def test_retry_after_above_budget_returns_original_status(self):
        self.script([{"status": 429,
                      "headers": {"Retry-After": "30"}}, "normal"])
        with self.no_delay():
            response = self.post_chat()
        try:
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers.get("Retry-After"), "30")
            self.assert_posts(1)
        finally:
            response.close()

    def test_retry_after_within_budget_waits_and_retries(self):
        self.script([{"status": 429,
                      "headers": {"Retry-After": "2"}}, "normal"])
        started = time.monotonic()
        response = self.post_chat()
        elapsed = time.monotonic() - started
        try:
            self.assertEqual(response.status_code, 200)
            self.assertGreaterEqual(elapsed, 1.8)
            self.assert_posts(2)
        finally:
            response.close()

    def test_x_should_retry_false_stops_retry(self):
        self.script([{"status": 503,
                      "headers": {"X-Should-Retry": "false"}}, "normal"])
        with self.no_delay():
            response = self.post_chat()
        try:
            self.assertEqual(response.status_code, 503)
            self.assert_posts(1)
        finally:
            response.close()

    def test_402_and_403_probe_once_without_fast_budget(self):
        for status in (402, 403):
            with self.subTest(status=status):
                self.script([{"status": status}, "normal"])
                with mock.patch.object(
                        chat2api, "UPSTREAM_AUTO_RETRIES", 0), \
                        self.no_delay():
                    response = self.post_chat()
                try:
                    self.assertEqual(response.status_code, 200)
                    self.assert_posts(2)
                finally:
                    response.close()
                    self.fake.reset()

    def test_403_auth_text_without_browser_is_503_browser_disabled(self):
        # Ticket #63: 403 authentication text triggers the ONE reauthentication
        # attempt even before the probe retry. With --no-browser that attempt
        # cannot open a window, so the gateway answers 503
        # reauthentication_required with the honest BrowserDisabled reason.
        self.script([{"status": 403,
                      "body": {"detail": "Not authenticated"}}, "normal"])
        with self.no_delay():
            response = self.post_chat()
        try:
            self.assertEqual(response.status_code, 503)
            error = response.json()["error"]
            self.assertEqual(error["code"], "reauthentication_required")
            self.assertEqual(error["detail"], "BrowserDisabled")
            self.assert_posts(1)
        finally:
            response.close()

    def test_400_422_conditional_probe_matches_transient_text(self):
        for status in (400, 422):
            with self.subTest(status=status):
                self.script([
                    {"status": status,
                     "body": {"detail": "model overloaded, try again"}},
                    "normal",
                ])
                with mock.patch.object(
                        chat2api, "UPSTREAM_AUTO_RETRIES", 0), \
                        self.no_delay():
                    response = self.post_chat()
                try:
                    self.assertEqual(response.status_code, 200)
                    self.assert_posts(2)
                finally:
                    response.close()
                    self.fake.reset()

    def test_400_422_non_transient_text_is_not_retried(self):
        for status in (400, 422):
            with self.subTest(status=status):
                self.script([{"status": status,
                              "body": {"detail": "bad request"}},
                             "normal"])
                with mock.patch.object(
                        chat2api, "UPSTREAM_AUTO_RETRIES", 0), \
                        self.no_delay():
                    response = self.post_chat()
                try:
                    self.assertEqual(response.status_code, status)
                    self.assert_posts(1)
                finally:
                    response.close()
                    self.fake.reset()

    def test_definitive_statuses_are_not_retried(self):
        for status in (404, 405, 409, 413, 415):
            with self.subTest(status=status):
                self.script([{"status": status}, "normal"])
                with self.no_delay():
                    response = self.post_chat()
                try:
                    self.assertEqual(response.status_code, status)
                    self.assert_posts(1)
                finally:
                    response.close()
                    self.fake.reset()

    def test_transient_statuses_use_the_fast_retry_budget(self):
        for status in (408, 500, 502, 504, 520, 521, 522, 523, 524, 529):
            with self.subTest(status=status):
                self.script([{"status": status}, "normal"])
                with self.no_delay():
                    response = self.post_chat()
                try:
                    self.assertEqual(response.status_code, 200)
                    self.assert_posts(2)
                finally:
                    response.close()
                    self.fake.reset()

    def test_retry_exhaustion_keeps_last_status(self):
        self.script([{"status": 503}, {"status": 500}, {"status": 429}])
        with mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 2), \
                self.no_delay():
            response = self.post_chat()
        try:
            self.assertEqual(response.status_code, 429)
            self.assert_posts(3)
        finally:
            response.close()

    def test_retry_wait_deadline_is_504_with_last_status(self):
        self.script([{"status": 503}])
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


class ChatStreamStagingTests(RetryContractTestBase):
    """Chat pre-commit staging and in-band envelope (ticket #64)."""

    def test_precommit_transport_disconnect_is_retried(self):
        # The chunked stream dies before any data event: no 200 was
        # committed, so the hidden reopen cannot concatenate two answers.
        self.script([MODE_DISCONNECT_BEFORE_FIRST, MODE_NORMAL])
        with self.no_delay():
            response = self.post_chat(stream=True)
        try:
            self.assertEqual(response.status_code, 200)
            self.assertIn("data: [DONE]", response.text)
            self.assertNotIn('"error"', response.text)
            self.assert_path_posts("/api/chat/completions", 2)
        finally:
            response.close()

    def test_postcommit_transport_disconnect_is_in_band(self):
        self.script([MODE_DISCONNECT_AFTER_FIRST])
        with self.no_delay():
            response = self.post_chat(stream=True)
        try:
            self.assertEqual(response.status_code, 200)
            body = response.text
            self.assertIn("first-visible", body)
            self.assertNotIn("data: [DONE]", body)
            payloads = [json.loads(line[len("data: "):])
                        for line in body.splitlines()
                        if line.startswith("data: ")
                        and line[len("data: "):] != "[DONE]"]
            error = payloads[-1]["error"]
            self.assertEqual(error["type"], "upstream_error")
            self.assertEqual(error["code"], "upstream_read_error")
            self.assertEqual(error["detail"]["reason"],
                             "upstream_read_error")
            self.assertEqual(error["request_id"],
                             response.headers.get("X-Request-Id"))
            self.assert_path_posts("/api/chat/completions", 1)
        finally:
            response.close()


class MessagesRetryTests(RetryContractTestBase):
    def test_messages_retry_after_above_budget_keeps_status_and_header(self):
        self.script([{"status": 429,
                      "headers": {"Retry-After": "30"}}, "normal"])
        with self.no_delay():
            response = self.post_messages()
        try:
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers.get("Retry-After"), "30")
            self.assert_posts(1)
        finally:
            response.close()

    def test_messages_402_probe_once_without_fast_budget(self):
        self.script([{"status": 402}, "normal"])
        with mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 0), \
                self.no_delay():
            response = self.post_messages()
        try:
            self.assertEqual(response.status_code, 200)
            self.assert_posts(2)
        finally:
            response.close()


class ResponsesRetryTests(RetryContractTestBase):
    def test_responses_non_200_enters_retry(self):
        self.script([{"status": 503}, "normal"])
        with self.no_delay():
            response = self.post_responses()
        try:
            self.assertEqual(response.status_code, 200)
            self.assert_posts(2)
        finally:
            response.close()

    def test_responses_retry_exhaustion_keeps_last_status(self):
        self.script([{"status": 503}, {"status": 500}, {"status": 429}])
        with mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 2), \
                self.no_delay():
            response = self.post_responses()
        try:
            self.assertEqual(response.status_code, 429)
            self.assert_posts(3)
        finally:
            response.close()

    def test_responses_retry_after_above_budget_keeps_header(self):
        self.script([{"status": 429,
                      "headers": {"Retry-After": "30"}}, "normal"])
        with self.no_delay():
            response = self.post_responses()
        try:
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers.get("Retry-After"), "30")
            self.assert_posts(1)
        finally:
            response.close()

    def test_responses_retry_wait_deadline_is_504_with_last_status(self):
        self.script([{"status": 503}])
        with mock.patch.object(chat2api, "MAX_REQUEST_SECONDS", 0.2), \
                mock.patch.object(chat2api, "_retry_delay_seconds",
                                  return_value=0.5):
            response = self.post_responses()
        try:
            self.assertEqual(response.status_code, 504)
            error = response.json()["error"]
            self.assertEqual(error["code"], "request_deadline_exceeded")
            self.assertEqual(error["detail"]["last_upstream_status"], 503)
        finally:
            response.close()


class ResponsesStreamStagingTests(RetryContractTestBase):
    """Responses pre-commit staging and in-band failures (ticket #64)."""

    @staticmethod
    def response_events(response):
        events = []
        for block in response.text.split("\n\n"):
            if not block.startswith("event: "):
                continue
            lines = block.splitlines()
            name = lines[0][len("event: "):]
            data = next(line[len("data: "):] for line in lines
                        if line.startswith("data: "))
            events.append((name, json.loads(data)))
        return events

    @staticmethod
    def image_tool_body(stream=True):
        return {"model": "stub-model", "stream": stream, "input": [
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

    def assert_no_terminal(self, events):
        names = [name for name, _ in events]
        self.assertNotIn("response.completed", names)
        self.assertEqual(names[-1], "response.failed")

    def test_precommit_transport_disconnect_is_retried(self):
        # The upstream closes the chunked stream before the first substantive
        # event: nothing reached the client, so the hidden reopen is safe.
        self.script([MODE_DISCONNECT_BEFORE_FIRST, MODE_NORMAL])
        with self.no_delay():
            response = self.post_responses(stream=True)
        try:
            self.assertEqual(response.status_code, 200)
            names = [name for name, _ in self.response_events(response)]
            self.assertIn("response.output_text.delta", names)
            self.assertIn("response.completed", names)
            self.assertNotIn("response.failed", names)
            self.assert_posts(2)
        finally:
            response.close()

    def test_precommit_clean_eof_is_retried(self):
        # A clean EOF before any substantive event is the same pre-commit
        # failure: the buffered response.created never committed a 200.
        self.script([MODE_SILENT, MODE_NORMAL])
        with self.no_delay():
            response = self.post_responses(stream=True)
        try:
            self.assertEqual(response.status_code, 200)
            names = [name for name, _ in self.response_events(response)]
            self.assertIn("response.completed", names)
            self.assertNotIn("response.failed", names)
            self.assert_posts(2)
        finally:
            response.close()

    def test_postcommit_transport_disconnect_is_response_failed(self):
        self.script([MODE_DISCONNECT_AFTER_FIRST])
        with self.no_delay():
            response = self.post_responses(stream=True)
        try:
            self.assertEqual(response.status_code, 200)
            events = self.response_events(response)
            names = [name for name, _ in events]
            self.assertIn("response.output_text.delta", names)
            self.assert_no_terminal(events)
            failed = [payload for name, payload in events
                      if name == "response.failed"][-1]
            error = failed["response"]["error"]
            self.assertEqual(error["code"], "upstream_read_error")
            self.assertEqual(error["detail"]["reason"], "upstream_read_error")
            self.assertEqual(error["request_id"],
                             response.headers.get("X-Request-Id"))
            self.assert_posts(1)
        finally:
            response.close()

    def test_postcommit_missing_done_is_response_failed(self):
        self.script([MODE_MISSING_TERMINAL])
        with self.no_delay():
            response = self.post_responses(stream=True)
        try:
            self.assertEqual(response.status_code, 200)
            events = self.response_events(response)
            self.assert_no_terminal(events)
            failed = [payload for name, payload in events
                      if name == "response.failed"][-1]
            error = failed["response"]["error"]
            self.assertEqual(error["code"], "upstream_eof_without_done")
            self.assertEqual(error["detail"]["reason"],
                             "upstream_eof_without_done")
            self.assert_posts(1)
        finally:
            response.close()

    def test_postcommit_upstream_error_is_response_failed(self):
        self.script([MODE_EXPLICIT_STREAM_ERROR_AFTER_FIRST])
        with self.no_delay():
            response = self.post_responses(stream=True)
        try:
            self.assertEqual(response.status_code, 200)
            events = self.response_events(response)
            self.assertIn("response.output_text.delta",
                          [name for name, _ in events])
            self.assert_no_terminal(events)
            failed = [payload for name, payload in events
                      if name == "response.failed"][-1]
            self.assertEqual(failed["response"]["error"]["code"],
                             "upstream_error")
            self.assert_posts(1)
        finally:
            response.close()

    def test_precommit_upstream_error_is_502(self):
        # An explicit upstream error is definitive, never retried, and no 200
        # was committed: the client gets an honest HTTP error.
        self.script([MODE_EXPLICIT_STREAM_ERROR])
        with self.no_delay():
            response = self.post_responses(stream=True)
        try:
            self.assertEqual(response.status_code, 502)
            error = response.json()["error"]
            self.assertEqual(error["code"], "upstream_error")
            self.assert_posts(1)
        finally:
            response.close()

    def test_via_messages_precommit_disconnect_is_retried(self):
        self.script([MODE_DISCONNECT_BEFORE_FIRST, MODE_NORMAL])
        with self.no_delay():
            response = self.post_responses(
                stream=True, body=self.image_tool_body())
        try:
            self.assertEqual(response.status_code, 200)
            names = [name for name, _ in self.response_events(response)]
            self.assertIn("response.completed", names)
            self.assertNotIn("response.failed", names)
            self.assert_posts(2)
        finally:
            response.close()

    def test_via_messages_postcommit_disconnect_is_response_failed(self):
        self.script([MODE_DISCONNECT_AFTER_FIRST])
        with self.no_delay():
            response = self.post_responses(
                stream=True, body=self.image_tool_body())
        try:
            self.assertEqual(response.status_code, 200)
            events = self.response_events(response)
            names = [name for name, _ in events]
            self.assertIn("response.output_text.delta", names)
            self.assert_no_terminal(events)
            failed = [payload for name, payload in events
                      if name == "response.failed"][-1]
            self.assertEqual(failed["response"]["error"]["code"],
                             "upstream_read_error")
            self.assert_posts(1)
        finally:
            response.close()

    def test_via_messages_postcommit_missing_stop_is_response_failed(self):
        self.script([MODE_MISSING_TERMINAL])
        with self.no_delay():
            response = self.post_responses(
                stream=True, body=self.image_tool_body())
        try:
            self.assertEqual(response.status_code, 200)
            events = self.response_events(response)
            self.assert_no_terminal(events)
            failed = [payload for name, payload in events
                      if name == "response.failed"][-1]
            self.assertEqual(failed["response"]["error"]["code"],
                             "upstream_eof_without_message_stop")
            self.assert_posts(1)
        finally:
            response.close()


class MessagesStreamStagingTests(RetryContractTestBase):
    """Messages local in-band errors use the Anthropic shape (ticket #64)."""

    @staticmethod
    def message_events(response):
        events = []
        for block in response.text.split("\n\n"):
            if not block.startswith("event: "):
                continue
            lines = block.splitlines()
            name = lines[0][len("event: "):]
            data = next(line[len("data: "):] for line in lines
                        if line.startswith("data: "))
            events.append((name, json.loads(data)))
        return events

    def test_precommit_disconnect_is_retried(self):
        self.script([MODE_DISCONNECT_BEFORE_FIRST, MODE_NORMAL])
        with self.no_delay():
            response = self.post_messages(stream=True)
        try:
            self.assertEqual(response.status_code, 200)
            names = [name for name, _ in self.message_events(response)]
            self.assertIn("message_stop", names)
            self.assertNotIn("error", names)
            self.assert_path_posts("/api/v1/messages", 2)
        finally:
            response.close()

    def test_postcommit_disconnect_emits_anthropic_error(self):
        self.script([MODE_DISCONNECT_AFTER_FIRST])
        with self.no_delay():
            response = self.post_messages(stream=True)
        try:
            self.assertEqual(response.status_code, 200)
            events = self.message_events(response)
            names = [name for name, _ in events]
            self.assertIn("content_block_delta", names)
            self.assertNotIn("message_stop", names)
            self.assertEqual(names[-1], "error")
            error = events[-1][1]["error"]
            self.assertEqual(events[-1][1]["type"], "error")
            self.assertEqual(error["type"], "api_error")
            self.assertEqual(error["code"], "upstream_read_error")
            self.assertEqual(error["detail"]["reason"],
                             "upstream_read_error")
            self.assertEqual(error["request_id"],
                             response.headers.get("X-Request-Id"))
            self.assert_path_posts("/api/v1/messages", 1)
        finally:
            response.close()

    def test_missing_message_stop_emits_anthropic_error(self):
        self.script([MODE_MISSING_TERMINAL])
        with self.no_delay():
            response = self.post_messages(stream=True)
        try:
            self.assertEqual(response.status_code, 200)
            events = self.message_events(response)
            names = [name for name, _ in events]
            self.assertNotIn("message_stop", names)
            self.assertEqual(names[-1], "error")
            error = events[-1][1]["error"]
            self.assertEqual(error["type"], "api_error")
            self.assertEqual(error["code"], "upstream_eof_without_event")
            self.assertEqual(error["request_id"],
                             response.headers.get("X-Request-Id"))
            self.assert_path_posts("/api/v1/messages", 1)
        finally:
            response.close()


if __name__ == "__main__":
    unittest.main()
