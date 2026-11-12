# -*- coding: utf-8 -*-
"""Isolated stub tests for the UM model speedtest module.

Covers ONLY ``console_speedtest`` (``speedtest_model`` and the credential
helpers). Every HTTP touchpoint is an in-process ``http.server`` stub on
127.0.0.1 with a random port; no real network, no real gateway service and
no real credential file is used. Sentinel literals stand in for secrets;
the module under test must never echo them (asserted on serialized results
and error messages). Timing assertions use monotonic deltas only (> 0 and
inside the injected budget) — no millisecond-precision claims.

Stub modes (per-scenario script on ``_StubState``):
* ``sse``        — 200 + scripted SSE lines with tiny per-line delays.
* ``drip``        — 200 + the first scripted line, then ``: keepalive``
                   comment lines every 50 ms until the client closes; gives
                   the client a read-granular cancellation checkpoint.
* ``wait_close`` — 200 + first SSE line, then block until the client closes
                   (recv EOF is the stub-side observable of the module
                   closing its connection on cancellation).
* ``silent``     — request read, then connection dropped with NO response
                   (the upstream silent-disconnect shape).
* ``json``       — fixed HTTP status + JSON body (um 401 scenario).

The stub also records request start/end intervals and the peak number of
concurrent requests, so the tests can prove that batch/single submissions
serialize on the ONE global speedtest resource.
"""

import collections
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, str(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import console_adapters  # noqa: E402  (same directory, sys.path prepared)
import console_speedtest  # noqa: E402
from console_operations import (  # noqa: E402
    OperationCancelled,
    OperationContext,
    OperationError,
    OperationManager,
)

_TMP = tempfile.mkdtemp(prefix="console-speedtest-tests-")


def _chunk(delta=None, finish_reason=None):
    body = {"choices": [{"delta": delta or {}, "index": 0}]}
    if finish_reason:
        body["choices"][0]["finish_reason"] = finish_reason
    return "data: " + json.dumps(body, ensure_ascii=False)


ROLE_CHUNK = _chunk({"role": "assistant", "content": ""})
REASONING_CHUNK = _chunk({"reasoning_content": "thinking, not first char"})
CONTENT_CHUNK = _chunk({"content": "你好，世界。SpeedTest。"})
FINISH_CHUNK = _chunk({}, finish_reason="stop")
DONE_LINE = "data: [DONE]"
# OpenAI-shaped usage chunk (chat2api final usage): the canonical
# completion_tokens value is the ONLY numerator the speed facts may use.
USAGE_TOKENS_LINE = ('data: {"choices":[],"usage":{"prompt_tokens":7,'
                     '"completion_tokens":40,"total_tokens":47}}')
GARBAGE_DATA_LINE = "data: not-json at all"


class _StubState:
    """Thread-safe scenario script and request log shared with the handler."""

    def __init__(self):
        self.mutex = threading.Lock()
        self.mode = "sse"
        self.items = []          # [(delay_before_write, sse_line), ...]
        self.first_line = ""     # wait_close / drip mode
        self.json_reply = (500, {"error": {"message": "stub"}})
        self.fail_models = set()  # json_by_model: models answered 429
        self.counts = collections.Counter()
        self.requests = []
        self.client_closed = False
        # Concurrency observation: one interval per POST, start and end.
        self.intervals = []
        self.active = 0
        self.max_active = 0


class _StubHandler(BaseHTTPRequestHandler):
    server_version = "StubSpeedtest/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # keep unittest output clean

    def do_POST(self):
        state = self.server.state
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        with state.mutex:
            state.counts[("POST", self.path)] += 1
            state.requests.append({
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            })
            mode = state.mode
            state.active += 1
            state.max_active = max(state.max_active, state.active)
            interval = [time.monotonic(), None]
            state.intervals.append(interval)
        try:
            if mode == "silent":
                # Drop the connection without any response bytes (no status).
                self.close_connection = True
                return
            if mode == "json":
                self._send_json(*state.json_reply)
                return
            if mode == "json_by_model":
                model = ""
                try:
                    model = json.loads(body.decode("utf-8")).get("model", "")
                except (ValueError, AttributeError):
                    pass
                if model in state.fail_models:
                    self._send_json(429, {"error": {"message": "rate limited"}})
                else:
                    self._send_sse(state.items, state)
                return
            if mode == "wait_close":
                self._send_sse_wait_close(state)
                return
            if mode == "drip":
                self._send_sse_drip(state)
                return
            self._send_sse(state.items, state)
        finally:
            with state.mutex:
                state.active -= 1
                interval[1] = time.monotonic()

    def _send_json(self, status, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_sse(self, items, state):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for delay, line in items:
            if delay:
                time.sleep(delay)
            try:
                self.wfile.write((line + "\n").encode("utf-8"))
                self.wfile.flush()
            except OSError:
                # Client timed out / cancelled and dropped the connection.
                return

    def _send_sse_wait_close(self, state):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write((state.first_line + "\n").encode("utf-8"))
            self.wfile.flush()
        except OSError:
            return
        # Block until the client closes its connection (recv EOF) or 5s pass;
        # the EOF is the stub-side observable of a bounded client release.
        try:
            self.connection.settimeout(5.0)
            if self.connection.recv(1) == b"":
                with state.mutex:
                    state.client_closed = True
        except OSError:
            pass

    def _send_sse_drip(self, state):
        """200 + one line, then a keepalive comment every 50 ms.

        The client checks cancellation between line reads, so a cancel request
        takes effect within one keepalive interval instead of waiting for the
        whole streaming budget.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write((state.first_line + "\n").encode("utf-8"))
            self.wfile.flush()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                time.sleep(0.05)
                self.wfile.write(b": keepalive\n")
                self.wfile.flush()
        except OSError:
            with state.mutex:
                state.client_closed = True


class _StubServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Expected when the client times out / cancels and drops mid-write.
        pass


def _dead_port():
    """A loopback port with no listener (bound once, then released)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _snapshot(key, port):
    return {"ok": True, "services": [{
        "key": key, "display_name": key, "port": port,
        "state": "Running", "healthy": True, "detail": "运行中",
        "process_ids": [1],
    }]}


def _cancelled_ctx():
    event = threading.Event()
    event.set()
    return OperationContext("test-op", event, lambda note: None)


class _CancelBeforeSecondRead:
    """Duck-typed ctx for read-granular cancel testing.

    ``raise_if_cancelled`` passes for the entry check and the pre-read-1
    check, then fires before read 2 — deterministic, no timing race.
    """

    def __init__(self):
        self.calls = 0

    def raise_if_cancelled(self):
        self.calls += 1
        if self.calls >= 3:
            raise OperationCancelled()

    def report_step(self, note):
        pass


class SpeedtestStubBase(unittest.TestCase):
    def setUp(self):
        self.state = _StubState()
        self.server = _StubServer(("127.0.0.1", 0), _StubHandler)
        self.server.state = self.state
        threading.Thread(
            target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]
        self.addCleanup(self._stop_server)
        self.case_root = os.path.join(_TMP, self._testMethodName)
        os.makedirs(self.case_root, exist_ok=True)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()

    def total_requests(self):
        return sum(self.state.counts.values())

    def patch_snapshot(self, key, port=None):
        patcher = mock.patch.object(
            console_adapters, "service_status",
            return_value=_snapshot(key, self.port if port is None else port))
        patcher.start()
        self.addCleanup(patcher.stop)

    def patch_snapshot_side_effect(self, exc):
        patcher = mock.patch.object(
            console_adapters, "service_status", side_effect=exc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def wait_client_closed(self, timeout_s=2.0):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self.state.mutex:
                if self.state.client_closed:
                    return True
            time.sleep(0.02)
        return self.state.client_closed

    def manager(self, acquire_timeout_s=5.0):
        """A real OperationManager on the production registry (stub HTTP only).

        The manager's worker invokes the registered ``model.speedtest`` handler,
        so these tests exercise the real resource lock/cancel/completion chain
        without touching any real gateway service.
        """
        manager = OperationManager(
            os.path.join(self.case_root, "console-state.sqlite3"),
            console_adapters_registry(),
            os.path.join(self.case_root, "console-locks"),
            acquire_timeout_s=acquire_timeout_s)
        self.addCleanup(manager.close)
        return manager


def console_adapters_registry():
    # Built through the production factory: the registry's ``model.speedtest``
    # spec (handler/schema/resource) is the object under test here, and its
    # handler reaches the in-process stub through the patched service_status.
    return console_adapters.build_registry()


def wait_terminal(manager, op_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    op = manager.get_operation(op_id)
    while op["state"] not in ("succeeded", "failed", "cancelled",
                              "interrupted"):
        if time.monotonic() >= deadline:
            raise AssertionError("operation did not finish: %r" % op)
        time.sleep(0.02)
        op = manager.get_operation(op_id)
    return op


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class UmSpeedtestTests(SpeedtestStubBase):
    """um source: chat2api :8000 chat route, auth-free (no Bearer header)."""

    def test_01_ok_stream_no_auth_header_honest_timing(self):
        self.patch_snapshot("chat2api")
        self.state.items = [
            (0, ROLE_CHUNK),
            (0, REASONING_CHUNK),   # reasoning increments never count
            (0, CONTENT_CHUNK),
            (0.002, FINISH_CHUNK),
            (0, DONE_LINE),
        ]
        result = console_speedtest.speedtest_model(
            "model.speedtest", {"source": "um", "model": "gpt-test"}, None)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source"], "um")
        self.assertEqual(result["model"], "gpt-test")
        self.assertIsNotNone(result["ttfb_s"])
        self.assertGreater(result["ttfb_s"], 0)
        self.assertGreater(result["total_s"], result["ttfb_s"])
        self.assertIn("你好", result["first_content_preview"])
        self.assertLessEqual(len(result["first_content_preview"]), 60)
        self.assertIsNone(result["error"])
        self.assertIsNone(result["usage"])
        self.assertEqual(result["billing_note"],
                         console_speedtest.BILLING_NOTE)
        request = self.state.requests[0]
        self.assertEqual(request["path"], "/v1/chat/completions")
        # The chat endpoint is auth-free: NO Authorization header at all.
        self.assertNotIn("authorization", request["headers"])
        # Request body contract: the max_tokens=64 billing bound is part
        # of every speedtest request (no rate-limit / stream_options).
        self.assertEqual(json.loads(request["body"]), {
            "model": "gpt-test", "stream": True, "max_tokens": 64,
            "messages": [{"role": "user", "content": "Hi"}]})

    def test_02_um_401_single_shot_no_retry_with_sideeffect_note(self):
        self.patch_snapshot("chat2api")
        self.state.mode = "json"
        self.state.json_reply = (401, {"error": {"message": "token expired"}})
        result = console_speedtest.speedtest_model(
            "model.speedtest", {"source": "um", "model": "gpt-test"}, None)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "http_401")
        message = result["error"]["message"]
        self.assertIn("401", message)
        self.assertIn("不重试", message)
        self.assertIn("refresh", message)
        self.assertIn("浏览器", message)
        # Exactly ONE request: a 401 can trigger the server-side refresh
        # (possibly a real browser login); the module never retries.
        self.assertEqual(
            self.state.counts[("POST", "/v1/chat/completions")], 1)

    def test_03_um_connection_refused_reports_unreachable_with_port(self):
        dead = _dead_port()
        self.patch_snapshot("chat2api", port=dead)
        result = console_speedtest.speedtest_model(
            "model.speedtest", {"source": "um", "model": "gpt-test"}, None)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "unreachable")
        self.assertIn(str(dead), result["error"]["message"])
        self.assertIn("拒绝", result["error"]["message"])
        self.assertIsNone(result["ttfb_s"])


class BudgetTimeoutTests(SpeedtestStubBase):
    def test_40_budget_timeout_reports_timeout_with_ttfb_none(self):
        self.patch_snapshot("chat2api")
        # Stub stalls BEFORE any content for longer than the 1s budget.
        self.state.items = [(1.5, CONTENT_CHUNK), (0, FINISH_CHUNK),
                            (0, DONE_LINE)]
        with mock.patch.object(console_speedtest, "_TOTAL_BUDGET_S", 1.0):
            result = console_speedtest.speedtest_model(
                "model.speedtest", {"source": "um", "model": "gpt-test"},
                None)
        self.assertEqual(result["status"], "timeout")
        self.assertIsNone(result["ttfb_s"])
        self.assertIsNone(result["first_content_preview"])
        self.assertGreaterEqual(result["total_s"], 0.9)
        self.assertLess(result["total_s"], 3.0)


class CancelTests(SpeedtestStubBase):
    def test_41_cancel_between_reads_closes_connection_observably(self):
        self.patch_snapshot("chat2api")
        self.state.mode = "wait_close"
        self.state.first_line = ROLE_CHUNK
        ctx = _CancelBeforeSecondRead()
        with self.assertRaises(OperationCancelled):
            console_speedtest.speedtest_model(
                "model.speedtest", {"source": "um", "model": "gpt-test"}, ctx)
        # Entry check + pre-read-1 pass, then the cancel fires before read 2.
        self.assertEqual(ctx.calls, 3)
        # The stub observes the client-side connection close (bounded release
        # on the cancel path, not a server-side EOF).
        self.assertTrue(self.wait_client_closed())
        self.assertEqual(
            self.state.counts[("POST", "/v1/chat/completions")], 1)


class SnapshotAndParamsTests(SpeedtestStubBase):
    def test_50_snapshot_failure_maps_to_status_unavailable(self):
        self.patch_snapshot_side_effect(RuntimeError("wrapper exploded"))
        with self.assertRaises(OperationError) as ctx:
            console_speedtest.speedtest_model(
                "model.speedtest", {"source": "um", "model": "gpt-test"},
                None)
        self.assertEqual(ctx.exception.code, "status_unavailable")
        self.assertIn("chat2api", ctx.exception.message)
        self.assertEqual(self.total_requests(), 0)

    def test_51_invalid_params_rejected_locally_zero_http(self):
        bad_params = [
            {},
            {"source": "nope", "model": "m"},
            {"source": "um", "model": ""},
            {"source": "um", "model": "x" * 129},
            {"source": "um"},
            "notdict",
            None,
        ]
        for params in bad_params:
            with self.subTest(params=params):
                with self.assertRaises(OperationError) as ctx:
                    console_speedtest.speedtest_model(
                        "model.speedtest", params, None)
                self.assertEqual(ctx.exception.code, "invalid_params")
        self.assertEqual(self.total_requests(), 0)

    def test_52_cancelled_ctx_checked_before_params_and_snapshot(self):
        with self.assertRaises(OperationCancelled):
            console_speedtest.speedtest_model(
                "model.speedtest", {}, _cancelled_ctx())
        self.assertEqual(self.total_requests(), 0)


class BatchSpeedtestTests(SpeedtestStubBase):
    """B4: explicit UM model set, one short request per model, serial, no retry."""

    def setUp(self):
        super().setUp()
        self.patch_snapshot("chat2api")
        self.state.items = [
            (0, ROLE_CHUNK),
            (0, CONTENT_CHUNK),
            (0.002, FINISH_CHUNK),
            (0, DONE_LINE),
        ]

    def test_60_single_model_legacy_shape_unchanged(self):
        # The SAME entry keeps the legacy single-model params and result shape:
        # no batch envelope, no extra request, the existing billing bound.
        result = console_speedtest.speedtest_action(
            "model.speedtest", {"source": "um", "model": "gpt-test"}, None)
        self.assertNotIn("batch", result)
        self.assertNotIn("results", result)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["model"], "gpt-test")
        self.assertIsNotNone(result["ttfb_s"])
        self.assertGreater(result["ttfb_s"], 0)
        self.assertGreater(result["total_s"], result["ttfb_s"])
        self.assertIsNone(result["usage"])
        self.assertIsNone(result["output_tokens"])
        self.assertEqual(result["output_tokens_status"], "missing")
        self.assertEqual(result["speed_status"], "missing_usage")
        self.assertEqual(len(self.state.requests), 1)
        self.assertEqual(json.loads(self.state.requests[0]["body"]),
                         {"model": "gpt-test", "stream": True, "max_tokens": 64,
                          "messages": [{"role": "user", "content": "Hi"}]})

    def test_61_batch_dedupes_and_one_request_per_model(self):
        result = console_speedtest.speedtest_action(
            "model.speedtest",
            {"source": "um", "models": '["m-a", "m-b", "m-a"]'}, None)
        self.assertTrue(result["batch"])
        self.assertEqual(result["source"], "um")
        self.assertEqual(result["requested_count"], 3)
        self.assertEqual(result["model_count"], 2)
        self.assertEqual(result["deduplicated_count"], 1)
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["status_counts"],
                         {"ok": 2, "failed": 0, "timeout": 0})
        self.assertEqual([entry["model"] for entry in result["results"]],
                         ["m-a", "m-b"])
        # Exactly one request per deduplicated model, in the selection order,
        # each with the existing max_tokens=64 billing bound.
        self.assertEqual(len(self.state.requests), 2)
        self.assertEqual(
            [json.loads(r["body"])["model"] for r in self.state.requests],
            ["m-a", "m-b"])
        for request in self.state.requests:
            self.assertEqual(json.loads(request["body"])["max_tokens"], 64)
        for entry in result["results"]:
            self.assertEqual(entry["status"], "ok")
            self.assertIsNotNone(entry["ttfb_s"])
            self.assertGreater(entry["ttfb_s"], 0)
            self.assertGreater(entry["total_s"], entry["ttfb_s"])
            self.assertIsNone(entry["usage"])
            self.assertIsNone(entry["output_tokens"])
            self.assertEqual(entry["speed_status"], "missing_usage")
            self.assertIsNone(entry["whole_avg_tokens_per_s"])
            self.assertIsNone(entry["generation_tokens_per_s"])

    def test_62_batch_usage_speed_has_two_honest_denominators(self):
        self.state.items = [
            (0, ROLE_CHUNK),
            (0, CONTENT_CHUNK),
            (0, USAGE_TOKENS_LINE),
            (0.002, FINISH_CHUNK),
            (0, DONE_LINE),
        ]
        result = console_speedtest.speedtest_models(
            "model", {"source": "um", "models": '["m-a"]'}, None)
        entry = result["results"][0]
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["usage"]["completion_tokens"], 40)
        self.assertEqual(entry["output_tokens"], 40)
        self.assertEqual(entry["output_tokens_status"], "reported")
        self.assertEqual(entry["speed_status"], "measured")
        self.assertAlmostEqual(entry["whole_avg_tokens_per_s"],
                               40 / entry["total_s"], places=3)
        self.assertAlmostEqual(
            entry["generation_tokens_per_s"],
            40 / entry["generation_window_s"], places=3)
        self.assertAlmostEqual(
            entry["generation_window_s"],
            entry["total_s"] - entry["ttfb_s"], places=9)
        self.assertGreater(entry["generation_tokens_per_s"],
                           entry["whole_avg_tokens_per_s"])
        self.assertEqual(entry["speed_note"], console_speedtest.SPEED_NOTE)

    def test_63_batch_cap_is_20_models(self):
        models = ["m-%02d" % i for i in range(20)]
        result = console_speedtest.speedtest_models(
            "model", {"source": "um", "models": json.dumps(models)}, None)
        self.assertEqual(result["model_count"], 20)
        self.assertEqual(result["attempt_count"], 20)
        self.assertEqual(len(self.state.requests), 20)
        with self.assertRaises(OperationError) as caught:
            console_speedtest.speedtest_models(
                "model", {"source": "um",
                          "models": json.dumps(models + ["m-over"])}, None)
        self.assertEqual(caught.exception.code, "invalid_params")
        self.assertEqual(len(self.state.requests), 20)

    def test_64_batch_rejects_bad_selection_zero_http(self):
        bad_selections = [
            {"source": "um", "models": "[]"},
            {"source": "um", "models": ""},
            {"source": "um", "models": "not-json"},
            {"source": "um", "models": '"m"'},
            {"source": "um", "models": "{}"},
            {"source": "um", "models": "null"},
            {"source": "um", "models": "[1]"},
            {"source": "um", "models": '[""]'},
            {"source": "um", "models": json.dumps(["x" * 129])},
            {"source": "other", "models": '["m-a"]'},
            {"models": '["m-a"]'},
            {"source": "um", "model": "m-a", "models": '["m-a"]'},
            {"source": "um", "models": None},
        ]
        for params in bad_selections:
            with self.subTest(params=params):
                with self.assertRaises(OperationError) as caught:
                    console_speedtest.speedtest_action("model", params, None)
                self.assertEqual(caught.exception.code, "invalid_params")
        self.assertEqual(self.total_requests(), 0)

    def test_65_batch_partial_failure_continues_without_filling_success(self):
        self.state.mode = "json_by_model"
        self.state.fail_models = {"m-a"}
        result = console_speedtest.speedtest_models(
            "model", {"source": "um", "models": '["m-a", "m-b"]'}, None)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["status_counts"],
                         {"ok": 1, "failed": 1, "timeout": 0})
        self.assertEqual([entry["status"] for entry in result["results"]],
                         ["failed", "ok"])
        failed = result["results"][0]
        self.assertEqual(failed["error"]["code"], "http_429")
        self.assertIsNone(failed["ttfb_s"])
        self.assertEqual(failed["speed_status"], "not_ok")
        self.assertIsNone(failed["whole_avg_tokens_per_s"])
        self.assertIsNone(failed["generation_tokens_per_s"])
        # The failed model is not retried: exactly one request for it.
        self.assertEqual(len(self.state.requests), 2)
        self.assertEqual(
            [json.loads(r["body"])["model"] for r in self.state.requests],
            ["m-a", "m-b"])

    def test_67_batch_reuses_existing_output_cap(self):
        result = console_speedtest.speedtest_models(
            "model",
            {"source": "um", "models": '["m-a"]', "max_tokens": 16}, None)
        self.assertEqual(result["max_tokens"], 16)
        self.assertEqual(json.loads(self.state.requests[0]["body"])["max_tokens"],
                         16)
        with self.assertRaises(OperationError) as caught:
            console_speedtest.speedtest_models(
                "model",
                {"source": "um", "models": '["m-a"]', "max_tokens": 513}, None)
        self.assertEqual(caught.exception.code, "invalid_params")
        self.assertEqual(len(self.state.requests), 1)

    def test_71_batch_resolves_the_target_once(self):
        with mock.patch.object(
                console_adapters, "service_status",
                return_value=_snapshot("chat2api", self.port)) as status:
            console_speedtest.speedtest_models(
                "model", {"source": "um", "models": '["m-a", "m-b"]'}, None)
        # ONE snapshot/credential resolution for the whole batch; each model
        # still runs the exact single-model probe.
        self.assertEqual(status.call_count, 1)
        self.assertEqual(len(self.state.requests), 2)


class BatchOperationTests(SpeedtestStubBase):
    """The batch rides the ONE global model:speedtest resource and its cancel."""

    def setUp(self):
        super().setUp()
        self.patch_snapshot("chat2api")
        self.state.items = [
            (0.05, ROLE_CHUNK),
            (0.05, CONTENT_CHUNK),
            (0, FINISH_CHUNK),
            (0, DONE_LINE),
        ]

    def test_66_empty_selection_fails_before_any_request(self):
        manager = self.manager()
        op = manager.submit("model.speedtest", target="model",
                            params={"source": "um", "models": "[]"})
        final = wait_terminal(manager, op["id"])
        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["error"]["code"], "invalid_params")
        self.assertEqual(self.total_requests(), 0)

    def test_68_batch_cancel_releases_global_resource(self):
        manager = self.manager()
        self.state.mode = "drip"
        self.state.first_line = ROLE_CHUNK
        op = manager.submit(
            "model.speedtest", target="model",
            params={"source": "um", "models": '["m-a", "m-b"]'})
        self.assertTrue(wait_until(
            lambda: self.state.counts[("POST", "/v1/chat/completions")] >= 1))
        manager.cancel(op["id"])
        final = wait_terminal(manager, op["id"])
        self.assertEqual(final["state"], "cancelled")
        self.assertTrue(self.wait_client_closed())
        # Cancellation before the second model: exactly one request, no retry.
        self.assertEqual(self.state.counts[("POST", "/v1/chat/completions")], 1)
        # The global resource lock fell back: the next single speedtest runs.
        self.state.mode = "sse"
        op2 = manager.submit("model.speedtest", target="model",
                              params={"source": "um", "model": "m-c"})
        final2 = wait_terminal(manager, op2["id"])
        self.assertEqual(final2["state"], "succeeded")
        self.assertEqual(final2["result"]["status"], "ok")
        self.assertEqual(self.state.counts[("POST", "/v1/chat/completions")], 2)

    def test_69_repeated_submissions_serialize_and_release(self):
        manager = self.manager()
        first = manager.submit(
            "model.speedtest", target="model",
            params={"source": "um", "models": '["m-a", "m-b"]'})
        second = manager.submit(
            "model.speedtest", target="model",
            params={"source": "um", "models": '["m-a", "m-b"]'})
        final1 = wait_terminal(manager, first["id"])
        final2 = wait_terminal(manager, second["id"])
        self.assertEqual(final1["state"], "succeeded")
        self.assertEqual(final2["state"], "succeeded")
        self.assertEqual(final1["result"]["model_count"], 2)
        self.assertEqual(final2["result"]["model_count"], 2)
        # 2 models * 2 submissions: no duplicate request per model, no retry.
        self.assertEqual(len(self.state.requests), 4)
        self.assertEqual(self.state.max_active, 1)

    def test_70_batch_and_single_never_overlap(self):
        manager = self.manager()
        batch = manager.submit(
            "model.speedtest", target="model",
            params={"source": "um", "models": '["m-a", "m-b"]'})
        single = manager.submit(
            "model.speedtest", target="model",
            params={"source": "um", "model": "m-c"})
        final_batch = wait_terminal(manager, batch["id"])
        final_single = wait_terminal(manager, single["id"])
        self.assertEqual(final_batch["state"], "succeeded")
        self.assertEqual(final_single["state"], "succeeded")
        self.assertEqual(self.state.max_active, 1)
        ordered = sorted(self.state.intervals)
        self.assertEqual(len(ordered), 3)
        for previous, following in zip(ordered, ordered[1:]):
            self.assertLessEqual(previous[1], following[0])
        # A failed batch does not block the next single request either.
        self.state.mode = "json"
        self.state.json_reply = (500, {"error": {"message": "stub"}})
        failed = manager.submit(
            "model.speedtest", target="model",
            params={"source": "um", "models": '["m-a", "m-b"]'})
        final_failed = wait_terminal(manager, failed["id"])
        self.assertEqual(final_failed["state"], "succeeded")
        self.assertEqual(final_failed["result"]["status"], "failed")
        self.state.mode = "sse"
        after = manager.submit("model.speedtest", target="model",
                                params={"source": "um", "model": "m-c"})
        final_after = wait_terminal(manager, after["id"])
        self.assertEqual(final_after["state"], "succeeded")
        self.assertEqual(final_after["result"]["status"], "ok")


class SpeedFactsTests(unittest.TestCase):
    """Speed facts: canonical/alias/zero, missing, failure never filled."""

    def test_output_tokens_canonical_alias_zero_and_missing(self):
        self.assertEqual(
            console_speedtest._output_tokens(
                {"completion_tokens": 5, "output_tokens": 9}), 5)
        self.assertEqual(
            console_speedtest._output_tokens({"output_tokens": 9}), 9)
        # A reported 0 is a real measured 0, not missing.
        self.assertEqual(
            console_speedtest._output_tokens({"completion_tokens": 0}), 0)
        for usage in ({}, {"completion_tokens": True},
                      {"completion_tokens": -1}, {"completion_tokens": "9"},
                      None):
            with self.subTest(usage=usage):
                self.assertIsNone(console_speedtest._output_tokens(usage))

    def test_failed_probe_keeps_missing_speed(self):
        facts = console_speedtest._speed_facts(
            "failed", {"completion_tokens": 40}, 0.1, 0.2)
        # Usage is still the measured fact, but a failed probe gets no speed.
        self.assertEqual(facts["output_tokens"], 40)
        self.assertEqual(facts["output_tokens_status"], "reported")
        self.assertEqual(facts["speed_status"], "not_ok")
        self.assertIsNone(facts["whole_avg_tokens_per_s"])
        self.assertIsNone(facts["generation_tokens_per_s"])

    def test_reported_zero_tokens_is_a_real_zero_speed(self):
        facts = console_speedtest._speed_facts(
            "ok", {"completion_tokens": 0}, 0.1, 0.2)
        self.assertEqual(facts["speed_status"], "measured")
        self.assertEqual(facts["whole_avg_tokens_per_s"], 0.0)
        self.assertEqual(facts["generation_tokens_per_s"], 0.0)


class BatchRegistryTests(unittest.TestCase):
    """Type/permission contract: the batch rides the existing action entry."""

    def _specs(self):
        return {spec.name: spec for spec in console_adapters.build_registry()}

    def test_batch_uses_existing_action_and_global_resource(self):
        specs = self._specs()
        spec = specs["model.speedtest"]
        self.assertTrue(spec.enabled)
        self.assertIsNone(spec.disabled_reason)
        self.assertIs(spec.handler, console_speedtest.speedtest_action)
        self.assertEqual(spec.resource_fn("model", {}), "model:speedtest")
        self.assertEqual(spec.param_schema["models"],
                         {"type": "str",
                          "max_len": console_speedtest.BATCH_MODELS_MAX_CHARS})
        self.assertEqual(spec.param_schema["model"],
                         {"type": "str", "max_len": 128})
        self.assertEqual(spec.param_schema["max_tokens"],
                         {"type": "int", "min": 1, "max": 512})
        # No second speedtest action: a UI loop cannot bypass the mutex.
        self.assertEqual(
            [name for name in specs if "speedtest" in name],
            ["model.speedtest"])

    def test_manager_accepts_models_string_and_refuses_list(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)  # LIFO: manager.close runs first
        manager = OperationManager(
            os.path.join(tmp.name, "state.sqlite3"),
            console_adapters.build_registry(),
            os.path.join(tmp.name, "locks"))
        self.addCleanup(manager.close)
        spec = self._specs()["model.speedtest"]
        validated = manager._validate_params(
            spec, {"source": "um", "models": '["m-a", "m-b"]'})
        self.assertEqual(validated["models"], '["m-a", "m-b"]')
        # The shared schema vocabulary has no list type: an in-process list
        # is refused at submit time, before any operation row exists.
        with self.assertRaises(OperationError) as caught:
            manager.submit("model.speedtest", target="model",
                           params={"source": "um",
                                    "models": ["m-a", "m-b"]})
        self.assertEqual(caught.exception.code, "invalid_params")
        self.assertEqual(manager.list_operations(), [])


if __name__ == "__main__":
    unittest.main()
