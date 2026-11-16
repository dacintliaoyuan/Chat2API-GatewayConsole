# -*- coding: utf-8 -*-
"""A3—A6: per-request auth wait, early failure, cancel/stop and capacity.

Contract:

* A3: one HTTP request owns a cumulative auth wait budget (production 30s).
  Internal 401 re-sends, compatibility retries and hidden retries share the
  remaining budget; the existing overall request deadline always wins.
* A4: deterministic impossible recovery fails 503 immediately. A definite next
  attempt carries a bounded integer Retry-After; a live lease's old expiry is
  NOT an early-failure signal.
* A5: a client disconnect ends only that request's wait and prevents its later
  inference; the shared recovery cycle continues. Service stop ends the wait and
  the recovery; late results keep the existing generation rules.
* A6: auth-wait slots (128 in production) are separate from the 128 stream
  slots, reject excess fast, release on every path, and never add a per-request
  recovery thread.

The real Handler and Proxy run; only the final upstream response and the final
browser login are replaced. The private ledger, handler drain and temporary
coordination file come from _chat2api_http_harness.py.
"""

import importlib.util
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _chat2api_http_harness as harness  # noqa: E402

CHAT2API_PATH = Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py"
sys.path.insert(0, str(CHAT2API_PATH.parent))
SPEC = importlib.util.spec_from_file_location(
    "chat2api_auth_wait", CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)

LOCAL_KEY = "local-secret-auth-wait"
STUB_TOKEN = "stub-token"
AUTH_WAIT_TIMEOUT = "auth_wait_timeout"
AUTH_WAIT_CAPACITY = "auth_wait_capacity"


class StubUpstream(BaseHTTPRequestHandler):
    """Final upstream boundary: configured 401/200 per GET/POST, nothing else."""

    protocol_version = "HTTP/1.1"
    statuses = [401]
    posts = 0
    gets = 0
    sse = False
    truncate_after_content = False
    empty_post_indices = frozenset()
    lock = threading.Lock()

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

    def _next_status(self, method):
        with StubUpstream.lock:
            if method == "POST":
                index = StubUpstream.posts
                StubUpstream.posts += 1
            else:
                index = StubUpstream.gets
                StubUpstream.gets += 1
        statuses = StubUpstream.statuses
        return statuses[min(index, len(statuses) - 1)]

    def do_GET(self):
        path = self.path.split("?")[0]
        status = self._next_status("GET")
        if status == 401:
            self._send_json(401, {"error": {"message": "unauthorized"}})
            return
        if status != 200:
            self._send_json(status, {"error": {"message": "upstream"}})
            return
        if path == "/api/config":
            self._send_json(200, {"version": "test", "features": {}})
            return
        self._send_json(200, {"data": []})

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        status = self._next_status("POST")
        if status == 401:
            self._send_json(401, {"error": {"message": "unauthorized"}})
            return
        if status != 200:
            self._send_json(status, {"error": {"message": "upstream"}})
            return
        index = StubUpstream.posts - 1
        if index in StubUpstream.empty_post_indices:
            # A 200 that ends before any event: the hidden-retry preflight
            # sees a zero-data EOF and opens a fresh attempt.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return
        if StubUpstream.truncate_after_content:
            # One visible content delta, then EOF without [DONE]: the preflight
            # has already released this to the client, so no retry is allowed.
            payload = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True
            return
        if StubUpstream.sse:
            # A real SSE stream so the streaming path (reader lease, producer
            # ownership, [DONE] termination) runs end to end.
            payload = (b'data: {"choices":[{"delta":{"content":"hi"},'
                       b'"finish_reason":"stop"}]}\n\n'
                       b'data: [DONE]\n\n')
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True
            return
        if path == "/api/v1/messages":
            self._send_json(200, {
                "id": "msg_auth_wait",
                "type": "message",
                "role": "assistant",
                "model": "stub-model",
                "content": [{"type": "text", "text": "hi"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            })
            return
        if path == "/api/v1/embeddings":
            self._send_json(200, {"object": "list", "data": []})
            return
        self._send_json(200, {
            "id": "chatcmpl-auth-wait",
            "object": "chat.completion",
            "model": "stub-model",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2},
        })


class AuthWaitTestBase(unittest.TestCase):
    def setUp(self):
        self.usage_dir, self.usage_log = harness.install_usage_isolation(
            self, chat2api, ledger_name="usage-chat2api.jsonl")
        harness.assert_stub_credentials(
            chat2api, local_key=LOCAL_KEY,
            upstream_token=STUB_TOKEN, stub_token_literal=STUB_TOKEN)

        self.slot_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.slot_dir.cleanup)
        slot_patcher = mock.patch.object(
            chat2api, "REAUTH_SLOT_FILE",
            str(Path(self.slot_dir.name) / "reauth.state"))
        slot_patcher.start()
        self.addCleanup(slot_patcher.stop)
        # These tests drive FAKE PIDs through the lease/wait semantics; pin the
        # owner-liveness probe to "alive" so the TTL contract is what they
        # measure on every platform. The liveness behaviour has its own class in
        # test_chat2api_auth.py.
        liveness_patcher = mock.patch.object(
            chat2api, "_owner_pid_alive", return_value=True)
        liveness_patcher.start()
        self.addCleanup(liveness_patcher.stop)

        self._original_wait_max = getattr(
            chat2api, "AUTH_WAIT_MAX_CONCURRENT", None)
        self._original_wait_slots = getattr(chat2api, "_auth_wait_slots", None)
        self.addCleanup(self._restore_wait_slots)

        self.control_token = "test-control-token"
        control_file = Path(self.slot_dir.name) / "control-token"
        control_file.write_text(self.control_token, encoding="utf-8")
        control_patcher = mock.patch.object(
            chat2api, "CONTROL_TOKEN_FILE", str(control_file))
        control_patcher.start()
        self.addCleanup(control_patcher.stop)

        upstream = harness.TrackedHTTPServer(("127.0.0.1", 0), StubUpstream)
        self.upstream = upstream
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        harness.install_tracked_server(self, upstream, name="upstream-stub",
                                      require_drained=False)

        args = SimpleNamespace(
            base_url=f"http://127.0.0.1:{upstream.server_port}",
            token=STUB_TOKEN,
            profile="profile",
            login_timeout=0,
            use_api_key=False,
            no_browser=False,
        )
        self.proxy = chat2api.Proxy({"token": STUB_TOKEN}, args)

        server = harness.TrackedHTTPServer(("127.0.0.1", 0), chat2api.Handler)
        self.server = server
        server.proxy = self.proxy
        server.local_api_key = LOCAL_KEY
        server.require_local_api_key = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        harness.install_tracked_server(self, server, name="chat2api-gateway")
        # Runs BEFORE the gateway drain: stop the handler waiting on auth.
        self.addCleanup(self.proxy.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"

        StubUpstream.statuses = [401]
        StubUpstream.posts = 0
        StubUpstream.gets = 0
        StubUpstream.sse = False
        StubUpstream.truncate_after_content = False
        StubUpstream.empty_post_indices = frozenset()

    def _restore_wait_slots(self):
        if self._original_wait_slots is not None:
            chat2api._auth_wait_slots = self._original_wait_slots
        else:
            try:
                del chat2api._auth_wait_slots
            except AttributeError:
                pass
        if self._original_wait_max is not None:
            chat2api.AUTH_WAIT_MAX_CONCURRENT = self._original_wait_max
        else:
            try:
                del chat2api.AUTH_WAIT_MAX_CONCURRENT
            except AttributeError:
                pass

    def _install_wait_slots(self, capacity):
        chat2api.AUTH_WAIT_MAX_CONCURRENT = capacity
        chat2api._auth_wait_slots = threading.BoundedSemaphore(capacity)
        return chat2api._auth_wait_slots

    @staticmethod
    def _wait_patches(budget=0.2, recheck=0.02, deadline=0.5):
        stack = ExitStack()
        stack.enter_context(
            mock.patch.object(chat2api, "AUTH_WAIT_BUDGET_S", budget,
                              create=True))
        stack.enter_context(
            mock.patch.object(chat2api, "AUTH_WAIT_RECHECK_S", recheck,
                              create=True))
        stack.enter_context(
            mock.patch.object(chat2api, "MAX_REQUEST_SECONDS", deadline))
        return stack

    def _hold_lease(self):
        granted, reason, _ledger = chat2api.acquire_reauth_slot(
            chat2api.REAUTH_SLOT_FILE, pid=111, ttl_s=60)
        self.assertTrue(granted, reason)
        self.addCleanup(chat2api.release_reauth_slot,
                        chat2api.REAUTH_SLOT_FILE, 111)

    def _release_lease_later(self, delay):
        def release():
            time.sleep(delay)
            chat2api.release_reauth_slot(chat2api.REAUTH_SLOT_FILE, 111)
        thread = threading.Thread(target=release, daemon=True)
        thread.start()
        return thread

    def _chat_request(self, timeout=5):
        return requests.post(
            f"{self.base}/v1/chat/completions",
            json={"model": "stub-model",
                  "messages": [{"role": "user", "content": "hi"}],
                  "max_tokens": 8},
            headers={"Authorization": f"Bearer {LOCAL_KEY}"},
            timeout=timeout)

    def _request(self, method, path, body=None, control=False, timeout=5):
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LOCAL_KEY}",
        }
        if control:
            headers[chat2api.CONTROL_HEADER] = self.control_token
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=timeout)
        try:
            conn.request(
                method, path,
                body=json.dumps(body) if body is not None else None,
                headers=headers)
            response = conn.getresponse()
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
        finally:
            conn.close()

    def _raw_chat_request(self):
        body = json.dumps({
            "model": "stub-model",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8,
        }).encode("utf-8")
        sock = socket.create_connection(
            ("127.0.0.1", self.server.server_port), timeout=5)
        sock.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer " + LOCAL_KEY.encode() + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"\r\n" + body)
        return sock

    def _raw_stream_chat_request(self):
        body = json.dumps({
            "model": "stub-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "max_tokens": 8,
        }).encode("utf-8")
        sock = socket.create_connection(
            ("127.0.0.1", self.server.server_port), timeout=5)
        sock.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer " + LOCAL_KEY.encode() + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"\r\n" + body)
        return sock

    def _stream_chat_request(self, timeout=5):
        return requests.post(
            f"{self.base}/v1/chat/completions",
            json={"model": "stub-model",
                  "messages": [{"role": "user", "content": "hi"}],
                  "stream": True, "max_tokens": 8},
            headers={"Authorization": f"Bearer {LOCAL_KEY}"},
            stream=True, timeout=timeout)

    @staticmethod
    def _read_stream(response, timeout_s=5):
        """Read a small SSE response to EOF and return the decoded body."""
        deadline = time.monotonic() + timeout_s
        chunks = []
        for chunk in response.iter_content(chunk_size=1):
            chunks.append(chunk)
            if time.monotonic() > deadline:
                break
        return b"".join(chunks).decode("utf-8", "replace")

    @staticmethod
    def _wait_until(predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    @staticmethod
    def _recovery_threads():
        return [thread for thread in threading.enumerate()
                if thread.name == "chat2api-reauthentication"]

    def _assert_retry_after(self, response, expected):
        value = response.headers.get("Retry-After")
        if expected is None:
            self.assertIsNone(value)
            return
        self.assertIsNotNone(value)
        self.assertTrue(value.isdigit(), value)
        self.assertGreaterEqual(int(value), 1)
        self.assertLessEqual(int(value), 900)

    def _usage_records(self):
        try:
            with open(self.usage_log, encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            return []

    def _wait_usage(self, count=1, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            records = self._usage_records()
            if len(records) >= count:
                return records
            time.sleep(0.02)
        return self._usage_records()


class AuthWaitBudgetTests(AuthWaitTestBase):
    def test_a3_second_401_is_final_without_another_login(self):
        # Ticket #63: the first 401 spends this logical request's ONE
        # reauthentication attempt. A later 401 after an intermediate 503 is
        # final: it must not open a second login window.
        self._hold_lease()
        StubUpstream.statuses = [401, 503, 401]
        calls = []

        def fake_login(*args, **kwargs):
            calls.append(time.monotonic())
            time.sleep(0.03)
            return "fresh-token"

        self._release_lease_later(0.05)
        with (
            self._wait_patches(budget=0.2, recheck=0.02, deadline=0.5),
            mock.patch.object(chat2api, "browser_login",
                              side_effect=fake_login),
            mock.patch.object(chat2api.TokenStore, "save"),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 5),
            mock.patch.object(chat2api, "_retry_delay_seconds",
                              return_value=0.0),
        ):
            started = time.monotonic()
            response = self._chat_request()
            elapsed = time.monotonic() - started

        self.assertEqual(response.status_code, 503)
        error = response.json()["error"]
        self.assertEqual(error["type"], "upstream_error")
        self.assertEqual(error["code"], "reauthentication_required")
        self.assertIsNone(error["param"])
        self.assertEqual(error["detail"], "upstream_rejected")
        self.assertEqual(response.headers.get("Content-Type"),
                         "application/json; charset=utf-8")
        self._assert_retry_after(response, expected=None)
        self.assertEqual(len(calls), 1,
                         "a second 401 must not open another login")
        self.assertEqual(StubUpstream.posts, 3)
        self.assertLess(elapsed, 1.0)

    def test_a3_overall_deadline_keeps_the_existing_deadline_error(self):
        self._hold_lease()
        StubUpstream.statuses = [401]
        with (
            self._wait_patches(budget=5.0, recheck=0.02, deadline=0.15),
            mock.patch.object(chat2api, "browser_login") as login,
        ):
            started = time.monotonic()
            response = self._chat_request()
            elapsed = time.monotonic() - started

        self.assertEqual(response.status_code, 504)
        error = response.json()["error"]
        self.assertEqual(error["type"], "upstream_error")
        self.assertEqual(error["code"], "request_deadline_exceeded")
        self.assertIsNone(error["param"])
        self.assertEqual(error["detail"]["reason"],
                         "_RequestDeadlineExceeded")
        self.assertLess(elapsed, 0.5)
        login.assert_not_called()

    def test_a3_sync_lease_lock_wait_is_inside_the_budget(self):
        # The synchronous entry may wait up to 1s inside the shared lease
        # lock; that wait is part of the same request budget.
        def slow_refusal(*args, **kwargs):
            time.sleep(0.2)
            return False, "global_session_active", {
                "owner_pid": 999,
                "expires_epoch": time.time() + 60,
                "attempts_in_window": 1,
                "next_allowed_epoch": None,
            }

        StubUpstream.statuses = [401]
        with (
            self._wait_patches(budget=0.1, recheck=0.02, deadline=0.5),
            mock.patch.object(chat2api, "acquire_reauth_slot",
                              side_effect=slow_refusal),
            mock.patch.object(chat2api, "browser_login") as login,
        ):
            started = time.monotonic()
            response = self._chat_request()
            elapsed = time.monotonic() - started

        self.assertEqual(response.status_code, 503)
        error = response.json()["error"]
        self.assertEqual(error["code"], "reauthentication_required")
        self.assertEqual(error["detail"], AUTH_WAIT_TIMEOUT)
        self._assert_retry_after(response, expected=None)
        self.assertLess(elapsed, 0.5)
        login.assert_not_called()

    def test_a3_normal_upstream_processing_is_not_charged_to_the_budget(self):
        # R2: the 30s budget is cumulative AUTHENTICATION wait, not the whole
        # request's wall clock. A slow normal upstream response before the
        # first 401 must not consume it; only the login wait does. The
        # non-billed delay stays well above the budget so a loaded full-repo
        # run cannot blur the contrast: charging it would exhaust the budget
        # by itself, not only through the login wait. The budget is 2s (not the
        # production 30s) and the 4s non-billed delay leaves a full 2s of
        # scheduler headroom for the real login wait, which a full Windows run
        # can delay on a loaded machine.
        StubUpstream.statuses = [401, 200]
        original_status = StubUpstream._next_status
        login_calls = []

        def delayed_status(handler, method):
            if method == "POST" and StubUpstream.posts == 0:
                time.sleep(4.0)
            return original_status(handler, method)

        def fake_login(*args, **kwargs):
            login_calls.append(time.monotonic())
            time.sleep(0.05)
            return "fresh-token"

        with (
            self._wait_patches(budget=2.0, recheck=0.02, deadline=8.0),
            mock.patch.object(StubUpstream, "_next_status",
                              new=delayed_status),
            mock.patch.object(chat2api, "browser_login",
                              side_effect=fake_login),
            mock.patch.object(chat2api.TokenStore, "save"),
        ):
            response = self._chat_request(timeout=15)

        self.assertEqual(
            response.status_code, 200,
            "normal upstream processing was charged as authentication wait")
        self.assertEqual(StubUpstream.posts, 2)
        self.assertEqual(len(login_calls), 1)

    def test_a3_upstream_after_auth_wait_is_not_charged(self):
        # R2: the normal inference after the request's one auth wait is not
        # authentication time. Only the login wait is charged; the non-billed
        # inference stays above the budget so a loaded full-repo run cannot
        # blur the contrast: charging it would exhaust the budget by itself.
        StubUpstream.statuses = [401, 503, 200]
        original_status = StubUpstream._next_status
        login_calls = []

        def delayed_status(handler, method):
            if method == "POST" and StubUpstream.posts == 1:
                time.sleep(1.0)
            return original_status(handler, method)

        def fake_login(*args, **kwargs):
            login_calls.append(time.monotonic())
            time.sleep(0.03)
            return "fresh-token"

        with (
            self._wait_patches(budget=0.5, recheck=0.02, deadline=8.0),
            mock.patch.object(StubUpstream, "_next_status",
                              new=delayed_status),
            mock.patch.object(chat2api, "browser_login",
                              side_effect=fake_login),
            mock.patch.object(chat2api.TokenStore, "save"),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 5),
            mock.patch.object(chat2api, "_retry_delay_seconds",
                              return_value=0.0),
        ):
            response = self._chat_request()

        self.assertEqual(
            response.status_code, 200,
            "normal upstream inference after the auth wait was charged")
        self.assertEqual(StubUpstream.posts, 3)
        self.assertEqual(len(login_calls), 1)

    def test_a3_retry_delay_after_auth_wait_is_not_charged(self):
        # R2: the bounded retry delay between two upstream attempts is normal
        # processing, not authentication wait. One auth wait spends the
        # request's single reauthentication attempt; the delay stays above the
        # budget so a loaded full-repo run cannot blur the contrast: charging
        # it would exhaust the budget by itself.
        StubUpstream.statuses = [401, 503, 200]
        login_calls = []

        def fake_login(*args, **kwargs):
            login_calls.append(time.monotonic())
            time.sleep(0.03)
            return "fresh-token"

        with (
            self._wait_patches(budget=0.5, recheck=0.02, deadline=8.0),
            mock.patch.object(chat2api, "browser_login",
                              side_effect=fake_login),
            mock.patch.object(chat2api.TokenStore, "save"),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 5),
            mock.patch.object(chat2api, "_retry_delay_seconds",
                              return_value=1.0),
        ):
            response = self._chat_request()

        self.assertEqual(
            response.status_code, 200,
            "the retry delay was charged as authentication wait")
        self.assertEqual(StubUpstream.posts, 3)
        self.assertEqual(len(login_calls), 1)

    def test_a3_request_body_processing_is_not_charged_to_the_budget(self):
        # R2: reading and validating the request body happens before any
        # authentication entry and must not consume the 30s budget. The body
        # delay stays above the budget so a loaded full-repo run cannot blur
        # the contrast: charging it would exhaust the budget by itself.
        StubUpstream.statuses = [401, 200]
        original_read_body = chat2api.Handler._read_body

        def slow_read_body(handler):
            time.sleep(1.0)
            return original_read_body(handler)

        with (
            self._wait_patches(budget=0.5, recheck=0.02, deadline=8.0),
            mock.patch.object(chat2api.Handler, "_read_body",
                              new=slow_read_body),
            mock.patch.object(chat2api, "browser_login",
                              return_value="fresh-token"),
            mock.patch.object(chat2api.TokenStore, "save"),
        ):
            response = self._chat_request()

        self.assertEqual(
            response.status_code, 200,
            "request body processing was charged as authentication wait")
        self.assertEqual(StubUpstream.posts, 2)

    def test_a3_exhausted_budget_keeps_its_first_expiry_instant(self):
        # §11.2: once the cumulative budget is really spent, a later wait
        # segment starts AFTER the instant the charge crossed the limit. That
        # instant is the FIRST real expiry and must never move forward: the
        # ordering against the overall request deadline is decided from it.
        clock = [100.0]
        budget = chat2api._AuthWaitBudget(0.2, now=lambda: clock[0])
        budget.begin_wait()
        first_expiry = budget.deadline
        self.assertAlmostEqual(first_expiry, 100.2, places=3)
        clock[0] += 0.5  # this one segment overruns the whole budget
        budget.end_wait()
        self.assertEqual(budget.remaining, 0)
        clock[0] += 5.0  # a much later second wait segment
        budget.begin_wait()
        try:
            self.assertAlmostEqual(
                budget.deadline, first_expiry, places=3,
                msg="an exhausted budget moved its expiry to the new "
                    "segment start")
        finally:
            budget.end_wait()
        self.assertEqual(budget.remaining, 0)

    def test_a3_remaining_budget_projects_the_later_segment(self):
        # The freeze applies ONLY after exhaustion: while charged time is below
        # the limit, a later segment may project its own expiry so normal
        # inference between two waits is still excluded from the budget.
        clock = [200.0]
        budget = chat2api._AuthWaitBudget(1.0, now=lambda: clock[0])
        budget.begin_wait()
        self.assertAlmostEqual(budget.deadline, 201.0, places=3)
        clock[0] += 0.4
        budget.end_wait()
        clock[0] = 300.0
        budget.begin_wait()
        try:
            self.assertAlmostEqual(
                budget.deadline, 300.6, places=3,
                msg="a segment with remaining budget must project its own "
                    "expiry")
        finally:
            budget.end_wait()


class AuthWaitCompletedBudgetTests(AuthWaitTestBase):
    """R2: a completed recovery never erases this request's spent budget.

    The synchronous auth entry may consume the whole request budget while the
    shared recovery round is still running. When that round publishes "valid"
    before the request joins it, the wait must still report the request-scoped
    timeout instead of returning success and sending another upstream call.
    Only time inside a real authentication-wait segment is charged; the shared
    cycle keeps running independently and normal inference stays free.
    """

    def _exercise_completed_round(self, budget):
        StubUpstream.statuses = [401, 200]
        acquire = chat2api.acquire_reauth_slot
        wait = self.proxy._wait_recovery
        first_acquire = [True]
        observed = {}

        def slow_first_acquire(*args, **kwargs):
            if first_acquire[0]:
                first_acquire[0] = False
                # The real synchronous auth entry charges this coordination
                # boundary; the real file lock and slot implementation stay.
                time.sleep(0.2)
            return acquire(*args, **kwargs)

        def join_after_background_completion(deadline, auth_wait_budget=None,
                                             cancel_check=None):
            # A legal scheduling order, not a fake authentication state: the
            # real background round finishes before the handler joins it.
            with self.proxy._auth_condition:
                restored = self.proxy._auth_condition.wait_for(
                    lambda: self.proxy._auth_state == "valid", timeout=1)
            if not restored:
                raise AssertionError("the real recovery did not finish")
            observed.update(charged_s=auth_wait_budget.used,
                            remaining_s=auth_wait_budget.remaining,
                            auth=self.proxy._auth_state)
            return wait(deadline, auth_wait_budget, cancel_check)

        with (
            self._wait_patches(budget=budget, recheck=0.01, deadline=2.0),
            mock.patch.object(chat2api, "acquire_reauth_slot",
                              side_effect=slow_first_acquire),
            mock.patch.object(self.proxy, "_wait_recovery",
                              side_effect=join_after_background_completion),
            mock.patch.object(chat2api, "browser_login",
                              return_value="fresh-stub-token"),
            mock.patch.object(chat2api.TokenStore, "save"),
        ):
            response = self._chat_request()
            try:
                observed.update(
                    http=response.status_code,
                    detail=(response.json().get("error") or {}).get("detail"),
                    upstream_posts=StubUpstream.posts,
                )
            finally:
                response.close()
        return observed

    def test_a3_finished_recovery_cannot_bypass_consumed_auth_budget(self):
        observed = self._exercise_completed_round(budget=0.1)
        self.assertGreater(
            observed["charged_s"], 0.1,
            "the synchronous auth entry was not really charged")
        self.assertEqual(observed["remaining_s"], 0)
        self.assertEqual(
            observed["http"], 503,
            "an exhausted auth budget must fail before the resend")
        self.assertEqual(observed["detail"], AUTH_WAIT_TIMEOUT)
        self.assertEqual(
            observed["upstream_posts"], 1,
            "an exhausted auth budget must not send a second upstream POST")
        self.assertEqual(
            chat2api._auth_wait_slots._value,
            chat2api.AUTH_WAIT_MAX_CONCURRENT,
            "the auth-wait slot was not released after the timeout")

    def test_a3_finished_recovery_with_remaining_budget_can_resend(self):
        observed = self._exercise_completed_round(budget=0.5)
        self.assertGreater(observed["charged_s"], 0.1)
        self.assertGreater(observed["remaining_s"], 0)
        self.assertEqual(observed["http"], 200)
        self.assertIsNone(observed["detail"])
        self.assertEqual(observed["upstream_posts"], 2)

    def test_a3_wait_loop_terminal_success_cannot_bypass_consumed_budget(self):
        StubUpstream.statuses = [401, 200]
        login_release = threading.Event()
        inner_slots = threading.BoundedSemaphore(
            chat2api.AUTH_WAIT_MAX_CONCURRENT)
        first = [True]
        first_lock = threading.Lock()
        observed = {}

        class GatedWaitSlots:
            def acquire(inner_self, blocking=False):
                acquired = inner_slots.acquire(blocking=blocking)
                if not acquired:
                    return False
                with first_lock:
                    is_first = first[0]
                    first[0] = False
                if is_first:
                    # The real background round finishes only AFTER the wait
                    # loop has started: publish its terminal result, then let
                    # this request's charged wait segment pass the budget.
                    login_release.set()
                    with self.proxy._auth_condition:
                        restored = self.proxy._auth_condition.wait_for(
                            lambda: self.proxy._auth_state == "valid",
                            timeout=1)
                    if not restored:
                        inner_slots.release()
                        raise AssertionError(
                            "the real recovery did not finish")
                    time.sleep(0.2)
                return True

            def release(inner_self):
                inner_slots.release()

        def gated_login(*args, **kwargs):
            if not login_release.wait(2):
                raise AssertionError("login gate timed out")
            return "fresh-stub-token"

        with (
            self._wait_patches(budget=0.1, recheck=0.01, deadline=2.0),
            mock.patch.object(chat2api, "_auth_wait_slots", GatedWaitSlots()),
            mock.patch.object(chat2api, "browser_login",
                              side_effect=gated_login),
            mock.patch.object(chat2api.TokenStore, "save"),
        ):
            try:
                response = self._chat_request()
                try:
                    observed.update(
                        http=response.status_code,
                        detail=(response.json().get("error")
                                or {}).get("detail"),
                        upstream_posts=StubUpstream.posts,
                    )
                finally:
                    response.close()
            finally:
                login_release.set()
                self.proxy.shutdown()

        self.assertEqual(
            observed["http"], 503,
            "the wait loop's terminal success bypassed the spent budget")
        self.assertEqual(observed["detail"], AUTH_WAIT_TIMEOUT)
        self.assertEqual(
            observed["upstream_posts"], 1,
            "the wait loop's terminal success must not resend upstream")
        self.assertEqual(
            inner_slots._value, chat2api.AUTH_WAIT_MAX_CONCURRENT,
            "the auth-wait slot was not released after the timeout")

    def test_a3_check_before_upstream_rechecks_the_auth_budget(self):
        budget = chat2api._AuthWaitBudget(0.1)
        budget.begin_wait()
        time.sleep(0.2)
        budget.end_wait()
        StubUpstream.statuses = [200]
        with self.assertRaises(chat2api.ReauthenticationThrottled) as caught:
            self.proxy.call(
                "POST", "/api/chat/completions", json={}, timeout=5,
                auth_wait_budget=budget)
        self.assertEqual(caught.exception.reason, AUTH_WAIT_TIMEOUT)
        self.assertEqual(
            StubUpstream.posts, 0,
            "an exhausted auth budget must not reach upstream")

    def test_a3_check_before_upstream_keeps_the_earlier_deadline_error(self):
        budget = chat2api._AuthWaitBudget(0.1)
        charge_started = time.monotonic()
        budget.begin_wait()
        time.sleep(0.2)
        budget.end_wait()
        StubUpstream.statuses = [200]
        with self.assertRaises(chat2api._RequestDeadlineExceeded):
            self.proxy.call(
                "POST", "/api/chat/completions", json={}, timeout=5,
                request_deadline=charge_started + 0.05,
                auth_wait_budget=budget)
        self.assertEqual(StubUpstream.posts, 0)


class AuthWaitExpiryOrderTests(AuthWaitTestBase):
    """§11.2: a spent budget keeps its FIRST expiry instant.

    The synchronous authentication entry can overrun the whole request budget; the
    handler then joins the shared round with a second wait segment that starts
    after the budget really ran out. That later segment start must never move the
    budget's expiry forward, because the ordering against the overall request
    deadline is decided from the FIRST real expiry: an earlier auth budget is a
    503 auth_wait_timeout, and only an earlier request deadline stays a 502
    request_deadline_exceeded.
    """

    def _exercise(self, budget, deadline, sync_charge_s):
        StubUpstream.statuses = [401, 200]
        acquire = chat2api.acquire_reauth_slot
        wait = self.proxy._wait_recovery
        first_acquire = [True]
        observed = {}

        def slow_first_acquire(*args, **kwargs):
            if first_acquire[0]:
                first_acquire[0] = False
                # The real synchronous auth entry (state lock + coordination
                # file lock) really charges this time to the request budget.
                time.sleep(sync_charge_s)
            return acquire(*args, **kwargs)

        def join_after_background_completion(deadline,
                                             auth_wait_budget=None,
                                             cancel_check=None):
            # A legal scheduling order, not a fake authentication state: the
            # real background round finishes before the handler joins it.
            with self.proxy._auth_condition:
                restored = self.proxy._auth_condition.wait_for(
                    lambda: self.proxy._auth_state == "valid", timeout=2)
            if not restored:
                raise AssertionError("the real recovery did not finish")
            observed.update(
                charged_s=auth_wait_budget.used,
                remaining_s=auth_wait_budget.remaining,
                first_deadline=auth_wait_budget.deadline,
            )
            try:
                return wait(deadline, auth_wait_budget, cancel_check)
            finally:
                observed["deadline_after_join"] = auth_wait_budget.deadline

        with (
            self._wait_patches(budget=budget, recheck=0.01,
                               deadline=deadline),
            mock.patch.object(chat2api, "acquire_reauth_slot",
                              side_effect=slow_first_acquire),
            mock.patch.object(self.proxy, "_wait_recovery",
                              side_effect=join_after_background_completion),
            mock.patch.object(chat2api, "browser_login",
                              return_value="fresh-stub-token"),
            mock.patch.object(chat2api.TokenStore, "save"),
        ):
            response = self._chat_request()
            try:
                error = response.json().get("error") or {}
                observed.update(
                    http=response.status_code,
                    error_code=error.get("code"),
                    detail=error.get("detail"),
                    upstream_posts=StubUpstream.posts,
                )
            finally:
                response.close()
        return observed

    def test_a3_auth_budget_earlier_than_a_later_deadline_is_a_503(self):
        observed = self._exercise(budget=0.2, deadline=0.5,
                                 sync_charge_s=0.7)
        self.assertGreater(
            observed["charged_s"], 0.2,
            "the synchronous auth entry was not really charged")
        self.assertEqual(observed["remaining_s"], 0)
        self.assertEqual(
            observed["http"], 503,
            "an auth budget that expired before the request deadline was "
            "reported as the request deadline: %r" % (observed,))
        self.assertEqual(observed["error_code"], "reauthentication_required")
        self.assertEqual(observed["detail"], AUTH_WAIT_TIMEOUT)
        self.assertAlmostEqual(
            observed["deadline_after_join"], observed["first_deadline"],
            places=3,
            msg="a later wait segment moved the spent budget's expiry")
        self.assertEqual(
            observed["upstream_posts"], 1,
            "the exhausted auth budget must not send a second upstream POST")

    def test_a3_auth_budget_earlier_than_a_far_deadline_is_a_503(self):
        observed = self._exercise(budget=0.2, deadline=2.0,
                                 sync_charge_s=0.7)
        self.assertGreater(observed["charged_s"], 0.2)
        self.assertEqual(observed["http"], 503, observed)
        self.assertEqual(observed["error_code"], "reauthentication_required")
        self.assertEqual(observed["detail"], AUTH_WAIT_TIMEOUT)
        self.assertAlmostEqual(
            observed["deadline_after_join"], observed["first_deadline"],
            places=3)
        self.assertEqual(observed["upstream_posts"], 1)

    def test_a3_earlier_request_deadline_is_a_504(self):
        observed = self._exercise(budget=0.5, deadline=0.2,
                                 sync_charge_s=0.7)
        self.assertGreater(observed["charged_s"], 0.5)
        self.assertEqual(observed["http"], 504, observed)
        self.assertEqual(observed["error_code"],
                         "request_deadline_exceeded")
        self.assertEqual(observed["detail"]["reason"],
                         "_RequestDeadlineExceeded")
        self.assertAlmostEqual(
            observed["deadline_after_join"], observed["first_deadline"],
            places=3)
        self.assertEqual(observed["upstream_posts"], 1)

    def test_a3_remaining_budget_still_resends_upstream(self):
        observed = self._exercise(budget=0.2, deadline=2.0,
                                 sync_charge_s=0.05)
        self.assertEqual(observed["http"], 200, observed)
        self.assertGreater(observed["remaining_s"], 0)
        self.assertEqual(
            observed["upstream_posts"], 2,
            "a request with remaining budget must still resend upstream")


class AuthWaitEarlyFailureTests(AuthWaitTestBase):
    def test_a4_global_attempt_cap_is_immediate_with_bounded_retry_after(self):
        now = time.time()
        for pid in (201, 202, 203):
            granted, reason, _ledger = chat2api.acquire_reauth_slot(
                chat2api.REAUTH_SLOT_FILE, pid=pid, ttl_s=60,
                now=now - 100)
            self.assertTrue(granted, reason)
            chat2api.release_reauth_slot(chat2api.REAUTH_SLOT_FILE, pid)
        StubUpstream.statuses = [401]
        with (
            self._wait_patches(budget=0.2, recheck=0.02, deadline=5.0),
            mock.patch.object(chat2api, "browser_login") as login,
        ):
            started = time.monotonic()
            response = self._chat_request()
            elapsed = time.monotonic() - started

        self.assertEqual(response.status_code, 503)
        error = response.json()["error"]
        self.assertEqual(error["code"], "reauthentication_required")
        self.assertEqual(error["detail"], "global_attempt_cap")
        self._assert_retry_after(response, expected=True)
        self.assertLess(elapsed, 0.5)
        login.assert_not_called()

    def test_a4_active_lease_is_not_an_early_failure(self):
        self._hold_lease()
        StubUpstream.statuses = [401]
        with (
            self._wait_patches(budget=0.2, recheck=0.02, deadline=0.5),
            mock.patch.object(chat2api, "browser_login") as login,
        ):
            started = time.monotonic()
            response = self._chat_request()
            elapsed = time.monotonic() - started

        self.assertEqual(response.status_code, 503)
        error = response.json()["error"]
        self.assertEqual(error["code"], "reauthentication_required")
        self.assertEqual(error["detail"], AUTH_WAIT_TIMEOUT)
        self._assert_retry_after(response, expected=None)
        self.assertGreaterEqual(elapsed, 0.15)
        self.assertLess(elapsed, 0.5)
        self.assertEqual(chat2api._auth_wait_slots._value,
                         chat2api.AUTH_WAIT_MAX_CONCURRENT,
                         "the auth-wait slot was not released after timeout")
        login.assert_not_called()

    def test_a4_coordination_corruption_is_immediate(self):
        Path(chat2api.REAUTH_SLOT_FILE).write_text("{not json",
                                                   encoding="utf-8")
        StubUpstream.statuses = [401]
        with (
            self._wait_patches(budget=5.0, recheck=0.02, deadline=5.0),
            mock.patch.object(chat2api, "browser_login") as login,
        ):
            started = time.monotonic()
            response = self._chat_request()
            elapsed = time.monotonic() - started

        self.assertEqual(response.status_code, 503)
        error = response.json()["error"]
        self.assertEqual(error["code"], "reauthentication_required")
        self.assertEqual(error["detail"], "slot_corrupt")
        self._assert_retry_after(response, expected=None)
        self.assertLess(elapsed, 0.5)
        login.assert_not_called()
        self.assertEqual(StubUpstream.posts, 1)

    def test_a4_rounds_exhausted_is_immediate(self):
        StubUpstream.statuses = [401]
        self.proxy._recovery["max_sessions"] = 1
        with (
            self._wait_patches(budget=5.0, recheck=0.02, deadline=5.0),
            mock.patch.object(
                chat2api, "browser_login",
                side_effect=chat2api.LoginWindowClosed("window_closed")),
            mock.patch.object(chat2api.TokenStore, "save"),
        ):
            started = time.monotonic()
            response = self._chat_request()
            elapsed = time.monotonic() - started

        self.assertEqual(response.status_code, 503)
        error = response.json()["error"]
        self.assertEqual(error["code"], "reauthentication_required")
        self.assertEqual(error["detail"], "attempts_exhausted")
        self._assert_retry_after(response, expected=None)
        self.assertLess(elapsed, 0.5)

    def test_a4_explicit_credential_rejection_is_immediate(self):
        StubUpstream.statuses = [401]
        with (
            self._wait_patches(budget=5.0, recheck=0.02, deadline=5.0),
            mock.patch.object(
                chat2api, "browser_login",
                side_effect=chat2api.LoginCredentialRejected(
                    "upstream_credential_rejected")),
            mock.patch.object(chat2api.TokenStore, "save"),
        ):
            started = time.monotonic()
            response = self._chat_request()
            elapsed = time.monotonic() - started

        self.assertEqual(response.status_code, 503)
        error = response.json()["error"]
        self.assertEqual(error["code"], "reauthentication_required")
        self.assertEqual(error["detail"], "upstream_credential_rejected")
        self._assert_retry_after(response, expected=None)
        self.assertLess(elapsed, 0.5)

    def test_a4_browser_unavailable_is_immediate(self):
        StubUpstream.statuses = [401]
        with (
            self._wait_patches(budget=5.0, recheck=0.02, deadline=5.0),
            mock.patch.object(
                chat2api, "browser_login",
                side_effect=chat2api.LoginUnavailable("edge_missing")),
            mock.patch.object(chat2api.TokenStore, "save"),
        ):
            started = time.monotonic()
            response = self._chat_request()
            elapsed = time.monotonic() - started

        self.assertEqual(response.status_code, 503)
        error = response.json()["error"]
        self.assertEqual(error["code"], "reauthentication_required")
        self.assertEqual(error["detail"], "edge_missing")
        self._assert_retry_after(response, expected=None)
        self.assertLess(elapsed, 0.5)


class AuthWaitCancelStopTests(AuthWaitTestBase):
    def test_a5_client_disconnect_ends_only_this_request_wait(self):
        self._hold_lease()
        StubUpstream.statuses = [401]
        with (
            self._wait_patches(budget=5.0, recheck=0.02, deadline=5.0),
            mock.patch.object(chat2api, "browser_login") as login,
        ):
            sock = self._raw_chat_request()
            self.addCleanup(sock.close)
            self.assertTrue(
                self._wait_until(lambda: self.server.active_requests() == 1),
                "the request was never dispatched")
            self.assertTrue(
                self._wait_until(lambda: StubUpstream.posts == 1),
                "the request never reached the final upstream")
            time.sleep(0.1)
            sock.close()
            self.assertTrue(
                self._wait_until(
                    lambda: self.server.active_requests() == 0, timeout=1.0),
                "the handler did not end this request's wait after disconnect")
            self.assertEqual(StubUpstream.posts, 1,
                             "a disconnected request must not infer again")
            self.assertEqual(self.proxy._recovery["state"], "waiting",
                             "the shared recovery cycle must keep waiting")
            self.assertLessEqual(len(self._recovery_threads()), 1)
            login.assert_not_called()

    def test_a5_service_stop_ends_the_wait_and_recovery(self):
        self._hold_lease()
        StubUpstream.statuses = [401]
        result = {}

        def run_request():
            try:
                result["response"] = self._chat_request()
            except Exception as exc:  # noqa: BLE001 — 客户端可能看到已关闭 socket
                result["error"] = exc

        with (
            self._wait_patches(budget=5.0, recheck=0.02, deadline=5.0),
            mock.patch.object(chat2api, "browser_login") as login,
        ):
            worker = threading.Thread(target=run_request, daemon=True)
            worker.start()
            self.assertTrue(
                self._wait_until(lambda: StubUpstream.posts == 1),
                "the request never reached the final upstream")
            self.assertTrue(
                self._wait_until(
                    lambda: self.proxy._recovery["state"] == "waiting"),
                "the request never entered the shared recovery wait")
            self.proxy.shutdown()
            worker.join(1)
            self.assertFalse(worker.is_alive(),
                             "service stop did not end the request wait")
            self.assertEqual(StubUpstream.posts, 1,
                             "no later inference after service stop")
            recovery_thread = self.proxy._recovery_thread
            self.assertTrue(recovery_thread is None
                            or not recovery_thread.is_alive())
            login.assert_not_called()


class AuthWaitCapacityTests(AuthWaitTestBase):
    def test_a6_capacity_is_separate_and_rejects_excess_fast(self):
        slots = self._install_wait_slots(1)
        self._hold_lease()
        StubUpstream.statuses = [401, 401]
        with (
            self._wait_patches(budget=0.6, recheck=0.02, deadline=0.6),
            mock.patch.object(chat2api, "browser_login") as login,
        ):
            first = self._raw_chat_request()
            self.addCleanup(first.close)
            self._wait_until(lambda: StubUpstream.posts == 1)
            self._wait_until(lambda: slots._value == 0, timeout=0.3)
            # A request with no bearer asks for auth BEFORE any upstream
            # call: the capacity refusal must therefore never touch the
            # final upstream (the first request already did its one 401).
            saved_creds = self.proxy.creds
            self.proxy.creds = {}
            try:
                started = time.monotonic()
                response = self._chat_request()
                elapsed = time.monotonic() - started
            finally:
                self.proxy.creds = saved_creds
            self.assertEqual(response.status_code, 503)
            error = response.json()["error"]
            self.assertEqual(error["code"], "reauthentication_required")
            self.assertEqual(error["detail"], AUTH_WAIT_CAPACITY)
            self._assert_retry_after(response, expected=None)
            self.assertLess(elapsed, 0.3)
            self.assertEqual(StubUpstream.posts, 1,
                             "a capacity refusal must not touch upstream")
            self.proxy.shutdown()
            first.close()
            self.assertTrue(
                self._wait_until(lambda: slots._value == 1, timeout=1.0),
                "the auth-wait slot was not released")
            login.assert_not_called()

    def test_a6_wait_slot_released_on_success(self):
        slots = self._install_wait_slots(1)
        self._hold_lease()
        self._release_lease_later(0.05)
        StubUpstream.statuses = [401, 200]
        with (
            self._wait_patches(budget=0.5, recheck=0.02, deadline=0.5),
            mock.patch.object(chat2api, "browser_login",
                              return_value="fresh-token"),
            mock.patch.object(chat2api.TokenStore, "save"),
        ):
            response = self._chat_request()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(StubUpstream.posts, 2)
        self.assertTrue(
            self._wait_until(lambda: slots._value == 1, timeout=1.0),
            "the auth-wait slot was not released after success")
        self.assertLessEqual(len(self._recovery_threads()), 1)

    def test_a6_wait_slot_released_on_terminal_failure(self):
        slots = self._install_wait_slots(1)
        StubUpstream.statuses = [401]
        with (
            self._wait_patches(budget=5.0, recheck=0.02, deadline=5.0),
            mock.patch.object(
                chat2api, "browser_login",
                side_effect=chat2api.LoginCredentialRejected(
                    "upstream_credential_rejected")),
            mock.patch.object(chat2api.TokenStore, "save"),
        ):
            response = self._chat_request()

        self.assertEqual(response.status_code, 503)
        self.assertTrue(
            self._wait_until(lambda: slots._value == 1, timeout=1.0),
            "the auth-wait slot was not released after a terminal failure")
        self.assertLessEqual(len(self._recovery_threads()), 1)

    def test_a6_many_waiters_share_one_recovery_thread(self):
        self._install_wait_slots(2)
        self._hold_lease()
        StubUpstream.statuses = [401, 401]
        sockets = []
        with (
            self._wait_patches(budget=5.0, recheck=0.02, deadline=5.0),
            mock.patch.object(chat2api, "browser_login") as login,
        ):
            for _ in range(2):
                sock = self._raw_chat_request()
                sockets.append(sock)
                self.addCleanup(sock.close)
            self.assertTrue(
                self._wait_until(lambda: StubUpstream.posts == 2),
                "both requests must reach the final upstream")
            self.assertTrue(
                self._wait_until(
                    lambda: self.server.active_requests() == 2),
                "both requests must be waiting")
            self.assertLessEqual(
                len(self._recovery_threads()), 1,
                "waiting requests must share one recovery thread")
            self.proxy.shutdown()
            for sock in sockets:
                sock.close()
            login.assert_not_called()


class AuthWaitReaderSlotTests(AuthWaitTestBase):
    """R3: a streaming auth wait releases the reader slot it will re-acquire."""

    def _run_stream_worker(self, result):
        try:
            result["response"] = self._stream_chat_request(timeout=5)
        except Exception as exc:  # noqa: BLE001 — 客户端可能看到已关闭 socket
            result["error"] = exc

    def test_a6_stream_auth_wait_releases_the_reader_slot(self):
        self._hold_lease()
        auth_slots = self._install_wait_slots(2)
        reader_slots = threading.BoundedSemaphore(1)
        StubUpstream.statuses = [401]
        result = {}
        worker = threading.Thread(target=self._run_stream_worker,
                                  args=(result,), daemon=True)
        with (
            self._wait_patches(budget=1.0, recheck=0.02, deadline=2.0),
            mock.patch.object(chat2api, "_reader_slots", reader_slots),
            mock.patch.object(chat2api, "browser_login",
                              side_effect=AssertionError("unexpected browser")),
        ):
            try:
                worker.start()
                self.assertTrue(
                    self._wait_until(lambda: auth_slots._value == 1,
                                     timeout=1.0),
                    "the request never entered the auth wait")
                self.assertEqual(
                    reader_slots._value, 1,
                    "an authentication waiter reserved a stream-reader slot")
            finally:
                self.proxy.shutdown()
                worker.join(2)
                self.assertFalse(worker.is_alive())
                response = result.get("response")
                if response is not None:
                    response.close()

    def test_a6_second_stream_is_not_rejected_for_concurrency(self):
        # R3: with ONE reader slot and TWO auth-wait slots, the second
        # streaming request must be admitted while the first waits for auth. It
        # may wait for auth and time out, but it must NOT be rejected as
        # too_many_concurrent_streams: no reader is running at that moment.
        self._hold_lease()
        auth_slots = self._install_wait_slots(2)
        reader_slots = threading.BoundedSemaphore(1)
        StubUpstream.statuses = [401]
        result = {}
        worker = threading.Thread(target=self._run_stream_worker,
                                  args=(result,), daemon=True)
        with (
            self._wait_patches(budget=0.3, recheck=0.02, deadline=1.0),
            mock.patch.object(chat2api, "_reader_slots", reader_slots),
            mock.patch.object(chat2api, "browser_login",
                              side_effect=AssertionError("unexpected browser")),
        ):
            try:
                worker.start()
                self.assertTrue(
                    self._wait_until(lambda: auth_slots._value == 1,
                                     timeout=1.0),
                    "the first request never entered the auth wait")
                second = self._stream_chat_request(timeout=5)
                try:
                    self.assertEqual(second.status_code, 503)
                    error = second.json()["error"]
                    self.assertNotEqual(
                        error["code"], "too_many_concurrent_streams",
                        "the second stream was rejected for concurrency while "
                        "no reader was running")
                    self.assertEqual(error["code"],
                                     "reauthentication_required")
                    self.assertEqual(error["detail"], AUTH_WAIT_TIMEOUT)
                    self.assertGreaterEqual(
                        StubUpstream.posts, 2,
                        "the second stream never reached the upstream")
                finally:
                    second.close()
            finally:
                self.proxy.shutdown()
                worker.join(2)
                response = result.get("response")
                if response is not None:
                    response.close()

    def test_a6_stream_auth_wait_timeout_releases_the_reader_slot(self):
        self._hold_lease()
        auth_slots = self._install_wait_slots(2)
        reader_slots = threading.BoundedSemaphore(1)
        StubUpstream.statuses = [401]
        result = {}
        worker = threading.Thread(target=self._run_stream_worker,
                                  args=(result,), daemon=True)
        with (
            self._wait_patches(budget=0.2, recheck=0.02, deadline=1.0),
            mock.patch.object(chat2api, "_reader_slots", reader_slots),
            mock.patch.object(chat2api, "browser_login",
                              side_effect=AssertionError("unexpected browser")),
        ):
            try:
                worker.start()
                self.assertTrue(
                    self._wait_until(lambda: auth_slots._value == 1,
                                     timeout=1.0),
                    "the request never entered the auth wait")
                self.assertEqual(
                    reader_slots._value, 1,
                    "an authentication waiter reserved a stream-reader slot")
                worker.join(2)
                self.assertFalse(worker.is_alive())
                response = result.get("response")
                self.assertIsNotNone(response)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["error"]["detail"],
                                 AUTH_WAIT_TIMEOUT)
                self.assertEqual(reader_slots._value, 1,
                                 "the reader slot was not returned after "
                                 "the auth-wait timeout")
            finally:
                self.proxy.shutdown()
                worker.join(2)
                response = result.get("response")
                if response is not None:
                    response.close()

    def test_a6_stream_auth_wait_cancel_releases_the_reader_slot(self):
        self._hold_lease()
        auth_slots = self._install_wait_slots(2)
        reader_slots = threading.BoundedSemaphore(1)
        StubUpstream.statuses = [401]
        sock = self._raw_stream_chat_request()
        with (
            self._wait_patches(budget=1.0, recheck=0.02, deadline=2.0),
            mock.patch.object(chat2api, "_reader_slots", reader_slots),
            mock.patch.object(chat2api, "browser_login",
                              side_effect=AssertionError("unexpected browser")),
        ):
            try:
                self.assertTrue(
                    self._wait_until(lambda: auth_slots._value == 1,
                                     timeout=1.0),
                    "the request never entered the auth wait")
                self.assertEqual(
                    reader_slots._value, 1,
                    "an authentication waiter reserved a stream-reader slot")
                sock.close()
                self.assertTrue(
                    self._wait_until(
                        lambda: self.server.active_requests() == 0,
                        timeout=2.0),
                    "the disconnected request did not end its auth wait")
                self.assertEqual(
                    reader_slots._value, 1,
                    "the reader slot was not returned after cancellation")
            finally:
                sock.close()
                self.proxy.shutdown()

    def test_a6_stream_auth_recovery_reacquires_and_returns_the_slot(self):
        self._hold_lease()
        self._release_lease_later(0.05)
        self._install_wait_slots(2)
        reader_slots = threading.BoundedSemaphore(1)
        StubUpstream.statuses = [401, 200]
        StubUpstream.sse = True
        with (
            self._wait_patches(budget=1.0, recheck=0.02, deadline=2.0),
            mock.patch.object(chat2api, "_reader_slots", reader_slots),
            mock.patch.object(chat2api, "browser_login",
                              return_value="fresh-token"),
            mock.patch.object(chat2api.TokenStore, "save"),
        ):
            response = self._stream_chat_request(timeout=5)
            try:
                self.assertEqual(response.status_code, 200)
                body = self._read_stream(response)
                self.assertIn("data: [DONE]", body)
            finally:
                response.close()
        self.assertEqual(StubUpstream.posts, 2)
        self.assertTrue(
            self._wait_until(lambda: reader_slots._value == 1, timeout=1.0),
            "the reader slot was not returned after the recovered stream")

    def test_a6_stream_auth_reacquire_failure_is_a_clean_503(self):
        self._hold_lease()
        self._release_lease_later(0.2)
        auth_slots = self._install_wait_slots(2)
        reader_slots = threading.BoundedSemaphore(1)
        holder_acquired = threading.Event()
        holder_release = threading.Event()
        StubUpstream.statuses = [401, 200]
        StubUpstream.sse = True

        def hold_slot():
            reader_slots.acquire()
            holder_acquired.set()
            holder_release.wait(5)
            reader_slots.release()

        holder = threading.Thread(target=hold_slot, daemon=True)

        def fake_login(*args, **kwargs):
            # Give the competing streaming request time to take the slot
            # this request released before authentication.
            time.sleep(0.2)
            return "fresh-token"

        result = {}
        worker = threading.Thread(target=self._run_stream_worker,
                                  args=(result,), daemon=True)
        with (
            self._wait_patches(budget=1.0, recheck=0.02, deadline=3.0),
            mock.patch.object(chat2api, "_reader_slots", reader_slots),
            mock.patch.object(chat2api, "browser_login",
                              side_effect=fake_login),
            mock.patch.object(chat2api.TokenStore, "save"),
        ):
            try:
                worker.start()
                self.assertTrue(
                    self._wait_until(lambda: auth_slots._value == 1,
                                     timeout=1.0),
                    "the request never entered the auth wait")
                holder.start()
                self.assertTrue(holder_acquired.wait(1.0),
                                "the competing stream never took the "
                                "freed reader slot")
                worker.join(3)
                self.assertFalse(worker.is_alive())
                response = result.get("response")
                self.assertIsNotNone(response)
                try:
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(
                        response.json()["error"]["type"],
                        "overloaded_error")
                    self.assertEqual(
                        response.json()["error"]["code"],
                        "too_many_concurrent_streams")
                    self.assertEqual(
                        StubUpstream.posts, 1,
                        "the failed re-acquire must not open another upstream "
                        "connection")
                finally:
                    response.close()
            finally:
                holder_release.set()
                if holder.ident is not None:
                    holder.join(2)
                self.proxy.shutdown()
                worker.join(2)
        self.assertFalse(holder.is_alive())
        self.assertEqual(reader_slots._value, 1)

    def test_a6_stream_hidden_retry_auth_wait_releases_the_slot(self):
        self._hold_lease()
        self._release_lease_later(0.05)
        self._install_wait_slots(2)
        reader_slots = threading.BoundedSemaphore(1)
        # First attempt: 200 with no data -> the pre-output check retries.
        # Hidden retry: 401 -> the request's ONE auth wait runs while the
        # reader slot must be released. Re-send: 200 SSE.
        StubUpstream.statuses = [200, 401, 200]
        StubUpstream.sse = True
        StubUpstream.empty_post_indices = frozenset({0})
        login_entered = threading.Event()
        let_login_finish = threading.Event()
        login_calls = []
        result = {}

        def fake_login(*args, **kwargs):
            login_calls.append(time.monotonic())
            login_entered.set()
            if not let_login_finish.wait(3):
                raise AssertionError("login gate timed out")
            return "fresh-token"

        worker = threading.Thread(target=self._run_stream_worker,
                                  args=(result,), daemon=True)
        with (
            self._wait_patches(budget=1.0, recheck=0.02, deadline=3.0),
            mock.patch.object(chat2api, "_reader_slots", reader_slots),
            mock.patch.object(chat2api, "browser_login",
                              side_effect=fake_login),
            mock.patch.object(chat2api.TokenStore, "save"),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 5),
            mock.patch.object(chat2api, "_retry_delay_seconds",
                              return_value=0.0),
        ):
            try:
                worker.start()
                self.assertTrue(
                    login_entered.wait(2),
                    "the hidden retry never reached its auth wait")
                self.assertEqual(
                    reader_slots._value, 1,
                    "the hidden retry reserved a stream-reader slot while "
                    "waiting for authentication")
                let_login_finish.set()
                worker.join(3)
                self.assertFalse(worker.is_alive())
                response = result.get("response")
                self.assertIsNotNone(response)
                try:
                    self.assertEqual(response.status_code, 200)
                    body = self._read_stream(response)
                    self.assertIn("data: [DONE]", body)
                finally:
                    response.close()
            finally:
                let_login_finish.set()
                self.proxy.shutdown()
                worker.join(2)
                response = result.get("response")
                if response is not None:
                    response.close()
        self.assertEqual(StubUpstream.posts, 3)
        self.assertEqual(len(login_calls), 1)
        self.assertTrue(
            self._wait_until(lambda: reader_slots._value == 1, timeout=1.0),
            "the reader slot was not returned after the hidden retry")


class AuthWaitLeaseRoundTests(AuthWaitTestBase):
    """R4: a finished round's cleanup never releases the next round's lease."""

    def test_old_round_cleanup_cannot_release_the_next_round_lease(self):
        original_release = chat2api.release_reauth_slot
        cleanup_entered = threading.Event()
        cleanup_finished = threading.Event()
        let_cleanup_finish = threading.Event()
        next_login_entered = threading.Event()
        let_next_login_finish = threading.Event()
        counter_lock = threading.Lock()
        release_calls = [0]
        login_calls = [0]
        third_pid = os.getpid() + 1000000
        third_granted = False

        def controlled_release(path, pid, success=False):
            with counter_lock:
                release_calls[0] += 1
                number = release_calls[0]
            if number == 2:
                # Pause the OLD round's final cleanup after its round check.
                cleanup_entered.set()
                if not let_cleanup_finish.wait(3):
                    raise AssertionError("release gate timed out")
            try:
                return original_release(path, pid, success=success)
            finally:
                if number == 2:
                    cleanup_finished.set()

        def login(*args, **kwargs):
            with counter_lock:
                login_calls[0] += 1
                number = login_calls[0]
            if number == 2:
                next_login_entered.set()
                if not let_next_login_finish.wait(3):
                    raise AssertionError("login gate timed out")
            return "fresh-token-%d" % number

        with (
            mock.patch.object(chat2api, "release_reauth_slot",
                              side_effect=controlled_release),
            mock.patch.object(chat2api, "browser_login", side_effect=login),
            mock.patch.object(chat2api.TokenStore, "save"),
        ):
            try:
                self.assertTrue(self.proxy.refresh(wait=False))
                old_thread = self.proxy._recovery_thread
                self.assertTrue(self._wait_until(
                    lambda: self.proxy._recovery["state"] == "idle",
                    timeout=2.0),
                    "the old round never published idle")
                self.assertTrue(cleanup_entered.wait(2),
                                "the old round never entered final cleanup")
                # The release is outside the state lock and is bound to the
                # OLD round by the round stored in the lease, so a plain
                # attribute write is enough to make the next round start while
                # the old release is paused.
                self.proxy._auth_state = "needs_login"
                new_round = threading.Thread(
                    target=self.proxy.refresh, kwargs={"wait": False},
                    name="test-next-recovery-round")
                new_round.start()
                # The release is bound to the OLD round by the round stored in
                # the lease: the next round may acquire while the old release
                # is paused. Wait until the next round REALLY holds the lease
                # and is parked in its login BEFORE releasing the old cleanup.
                self.assertTrue(
                    next_login_entered.wait(2),
                    "the next round did not begin its login before the old "
                    "cleanup was released")
                let_cleanup_finish.set()
                self.assertTrue(cleanup_finished.wait(2),
                                "the old round's final cleanup did not finish")
                before = json.loads(Path(chat2api.REAUTH_SLOT_FILE).read_text(
                    encoding="utf-8"))
                self.assertEqual(
                    before.get("owner_pid"), os.getpid(),
                    "the old round cleanup released the next round's lease")
                self.assertEqual(before.get("round"), 2)
                third_granted, _reason, _ledger = chat2api.acquire_reauth_slot(
                    chat2api.REAUTH_SLOT_FILE, third_pid, ttl_s=30)
                self.assertFalse(
                    third_granted,
                    "the old round cleanup released the lease held by the "
                    "new active login")
            finally:
                let_cleanup_finish.set()
                let_next_login_finish.set()
                self.proxy._shutdown_event.set()
                new_round.join(2)
                if third_granted:
                    original_release(chat2api.REAUTH_SLOT_FILE, third_pid)
                self.proxy.shutdown()
                old_thread.join(2)


class AuthWaitWiringTests(AuthWaitTestBase):
    """A7: every real entry shares the request budget and keeps its shape."""

    def test_a7_models_entry_shares_the_request_budget(self):
        self._hold_lease()
        StubUpstream.statuses = [401]
        with self._wait_patches(budget=0.2, recheck=0.02, deadline=0.5):
            started = time.monotonic()
            status, payload = self._request("GET", "/v1/models")
            elapsed = time.monotonic() - started
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "reauthentication_required")
        self.assertEqual(payload["error"]["detail"], AUTH_WAIT_TIMEOUT)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(StubUpstream.gets, 1)

    def test_a7_api_get_passthrough_shares_the_request_budget(self):
        self._hold_lease()
        StubUpstream.statuses = [401]
        with self._wait_patches(budget=0.2, recheck=0.02, deadline=0.5):
            status, payload = self._request("GET", "/api/config", control=True)
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "reauthentication_required")
        self.assertEqual(payload["error"]["detail"], AUTH_WAIT_TIMEOUT)
        self.assertEqual(StubUpstream.gets, 1)

    def test_a7_api_post_passthrough_shares_the_request_budget(self):
        self._hold_lease()
        StubUpstream.statuses = [401]
        with self._wait_patches(budget=0.2, recheck=0.02, deadline=0.5):
            status, payload = self._request(
                "POST", "/api/config", body={}, control=True)
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "reauthentication_required")
        self.assertEqual(payload["error"]["detail"], AUTH_WAIT_TIMEOUT)
        self.assertEqual(StubUpstream.posts, 1)

    def test_a7_messages_entry_shares_the_request_budget(self):
        self._hold_lease()
        StubUpstream.statuses = [401]
        body = {"model": "stub-model",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 8}
        with self._wait_patches(budget=0.2, recheck=0.02, deadline=0.5):
            status, payload = self._request("POST", "/v1/messages", body=body)
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "reauthentication_required")
        self.assertEqual(payload["error"]["detail"], AUTH_WAIT_TIMEOUT)
        self.assertEqual(StubUpstream.posts, 1)

    def test_a7_embeddings_entry_shares_the_request_budget(self):
        self._hold_lease()
        StubUpstream.statuses = [401]
        with self._wait_patches(budget=0.2, recheck=0.02, deadline=0.5):
            status, payload = self._request(
                "POST", "/v1/embeddings",
                body={"model": "stub-model", "input": "hi"})
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "reauthentication_required")
        self.assertEqual(payload["error"]["detail"], AUTH_WAIT_TIMEOUT)
        self.assertEqual(StubUpstream.posts, 1)

    def test_a7_responses_entry_shares_the_request_budget(self):
        self._hold_lease()
        StubUpstream.statuses = [401]
        with self._wait_patches(budget=0.2, recheck=0.02, deadline=0.5):
            status, payload = self._request(
                "POST", "/v1/responses",
                body={"model": "stub-model", "input": "hi"})
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "reauthentication_required")
        self.assertEqual(payload["error"]["detail"], AUTH_WAIT_TIMEOUT)
        self.assertEqual(StubUpstream.posts, 1)

    def test_a7_valid_auth_never_touches_the_lease(self):
        StubUpstream.statuses = [200]
        with (
            mock.patch.object(chat2api, "acquire_reauth_slot",
                              side_effect=AssertionError("lease touched")),
            mock.patch.object(chat2api, "release_reauth_slot",
                              side_effect=AssertionError("lease touched")),
        ):
            status, payload = self._request("GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"data": []})
        self.assertEqual(StubUpstream.gets, 1)

    def test_a7_health_ready_and_verify_do_not_wait(self):
        with (
            mock.patch.object(chat2api, "classify_credential",
                              return_value=("valid", None, 200)),
            mock.patch.object(chat2api, "acquire_reauth_slot",
                              side_effect=AssertionError("lease touched")),
            mock.patch.object(chat2api, "browser_login",
                              side_effect=AssertionError("login opened")),
        ):
            for path in ("/health", "/ready"):
                status, _payload = self._request("GET", path)
                self.assertEqual(status, 200)
            status, payload = self._request(
                "GET", "/admin/auth/verify", control=True)
        self.assertEqual(status, 200)
        self.assertEqual(payload["verify"], "valid")

    def test_a7_messages_success_keeps_one_ledger_row(self):
        self._hold_lease()
        self._release_lease_later(0.05)
        StubUpstream.statuses = [401, 200]
        body = {"model": "stub-model",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 8}
        with (
            self._wait_patches(budget=0.5, recheck=0.02, deadline=0.5),
            mock.patch.object(chat2api, "browser_login",
                              return_value="fresh-token"),
            mock.patch.object(chat2api.TokenStore, "save"),
        ):
            status, payload = self._request("POST", "/v1/messages", body=body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["type"], "message")
        self.assertEqual(payload["content"], [{"type": "text", "text": "hi"}])
        records = self._wait_usage(count=1)
        self.assertEqual(len(records), 1, records)
        self.assertEqual(records[0]["protocol"], "messages")


class AuthWaitSecondRejectionTests(AuthWaitTestBase):
    """A4/A7: a 401 AFTER a successful refresh is still ONE explicit
    authentication failure, with the same reason on every direct entry.

    The first 401 waits for the live lease, then the browser login publishes
    a fresh credential. The re-sent request is rejected again: that second 401
    must never leak to the handler as a raw upstream response (502, a
    pass-through 401, or a 503 without a reason).
    """

    BODIES = {
        "/v1/messages": {"model": "stub-model",
                         "messages": [{"role": "user", "content": "hi"}],
                         "max_tokens": 8},
        "/v1/embeddings": {"model": "stub-model", "input": "hi"},
        "/v1/chat/completions": {
            "model": "stub-model",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8},
        "/v1/responses": {"model": "stub-model", "input": "hi"},
    }

    def _second_401(self, method, path, body=None):
        self._hold_lease()
        self._release_lease_later(0.05)
        StubUpstream.statuses = [401, 401]
        with (
            self._wait_patches(budget=5.0, recheck=0.02, deadline=5.0),
            mock.patch.object(chat2api, "browser_login",
                              return_value="fresh-token"),
            mock.patch.object(chat2api.TokenStore, "save"),
        ):
            status, payload = self._request(method, path, body=body)
        self.assertEqual(status, 503, payload)
        self.assertEqual(payload["error"]["code"],
                         "reauthentication_required", payload)
        self.assertEqual(payload["error"]["detail"],
                         "upstream_rejected", payload)

    def test_a4_models_second_401_is_one_explicit_failure(self):
        self._second_401("GET", "/v1/models")

    def test_a4_messages_second_401_is_one_explicit_failure(self):
        self._second_401("POST", "/v1/messages",
                         self.BODIES["/v1/messages"])
        records = self._wait_usage(count=1)
        self.assertEqual(len(records), 1, records)
        self.assertEqual(records[0]["status"], "reauthentication_required")

    def test_a4_embeddings_second_401_is_one_explicit_failure(self):
        self._second_401("POST", "/v1/embeddings",
                         self.BODIES["/v1/embeddings"])

    def test_a4_chat_second_401_is_one_explicit_failure(self):
        self._second_401("POST", "/v1/chat/completions",
                         self.BODIES["/v1/chat/completions"])

    def test_a4_responses_second_401_is_one_explicit_failure(self):
        self._second_401("POST", "/v1/responses",
                         self.BODIES["/v1/responses"])


if __name__ == "__main__":
    unittest.main()
