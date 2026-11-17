# -*- coding: utf-8 -*-
"""Isolated tests for the UM re-login orchestration adapter (console P2-R1).

Every case runs against a stub chat2api admin HTTP server (ThreadingHTTPServer
on an ephemeral port) with ``console_adapters.service_status`` mocked to a
controlled state sequence and ``console_adapters._invoke_wrapper`` mocked to a
recording ``Mock``. No real service, no real wrapper, no real token file and
no real upstream is touched. The token path is never exercised here because
the happy-path token comes from a patched ``_read_control_token``.

Sequence contract being tested (snapshot call order on the start path):
initial judge -> wrapper start -> start-poll snapshots -> base-URL resolve
snapshot -> credential verify GET -> optional login POST -> status polling
(HTTP stub) -> wrapper stop ->
re-verify snapshot.
"""

import itertools
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import login_contract  # noqa: E402
from console import adapters as console_adapters  # noqa: E402  (same directory, sys.path prepared)
from console import um_adapter as console_um_adapter  # noqa: E402
from console import um_login as console_um_login  # noqa: E402
from console.operations import OperationCancelled, OperationError  # noqa: E402


class _Ctx:
    """Task-layer context stand-in with a settable cancel event."""

    def __init__(self):
        self.event = threading.Event()
        self.steps = []

    def report_step(self, note):
        self.steps.append(note)

    def raise_if_cancelled(self):
        if self.event.is_set():
            raise OperationCancelled()


class _StubAdminServer:
    """Programmable chat2api admin stub: verify + login + status."""

    def __init__(self, status_value="valid", login_code=202, on_status=None,
                 verify_value="needs_login", verify_code=200,
                 verify_payload=None, on_verify=None, status_payload=None,
                 status_code=200, status_raw=None, login_payload=None):
        self.requests = []  # (method, path, headers-dict)
        self.login_bodies = []
        self.login_code = login_code
        self.login_payload = ({"state": "logging_in", "label": "", "error": None}
                              if login_payload is None else login_payload)
        self.on_status = on_status
        self.verify_code = verify_code
        self.verify_payload = ({"verify": verify_value, "reason": None,
                                "upstream_status": None, "checked_epoch": 1}
                               if verify_payload is None else verify_payload)
        self.on_verify = on_verify
        self.status_payload = status_payload
        self.status_code = status_code
        self.status_raw = status_raw
        self._status_next = (status_value if callable(status_value)
                             else itertools.repeat(status_value).__next__)
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def _record(self):
                outer.requests.append(
                    (self.command, self.path, dict(self.headers)))

            def _json(self, code, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self._record()
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                outer.login_bodies.append(raw)
                self._json(outer.login_code, outer.login_payload)

            def do_GET(self):
                self._record()
                if self.path == "/admin/auth/verify":
                    if outer.on_verify is not None:
                        outer.on_verify()
                    if outer.verify_code is None:
                        self.close_connection = True
                        return
                    self._json(outer.verify_code, outer.verify_payload)
                elif self.path == "/admin/auth/status":
                    if outer.on_status is not None:
                        outer.on_status()
                    if outer.status_code is None:
                        self.close_connection = True
                        return
                    if outer.status_raw is not None:
                        self.send_response(outer.status_code)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length",
                                           str(len(outer.status_raw)))
                        self.end_headers()
                        self.wfile.write(outer.status_raw)
                    elif outer.status_payload is not None:
                        self._json(outer.status_code, outer.status_payload)
                    else:
                        self._json(outer.status_code,
                                   {"state": outer._status_next(),
                                    "label": "", "error": None})
                else:
                    self._json(404, {"error": "unknown endpoint"})

            def log_message(self, fmt, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self):
        return self._server.server_address[1]

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _state_producer(states):
    """Snapshot state sequence: str = constant; list = consumed in order,
    the last value repeats once the list is exhausted (extra reads never
    crash a test because of an unexpected extra snapshot call)."""
    if isinstance(states, str):
        while True:
            yield states
    remaining = list(states)
    while True:
        if len(remaining) > 1:
            yield remaining.pop(0)
        elif remaining:
            yield remaining[0]
        else:
            yield "Stopped"


def _make_service_status_mock(port, states):
    producer = _state_producer(states)

    def side_effect():
        return {"services": [
            {"key": "chat2api", "state": next(producer), "port": port}]}

    return mock.Mock(side_effect=side_effect)


class ConsoleUmLoginTests(unittest.TestCase):
    """Real orchestration with isolated service and HTTP boundaries."""

    def _start_case(self, states, *, status_value="valid", login_code=202,
                    on_status=None, params=None, with_ctx=True,
                    verify_value="needs_login", verify_code=200,
                    verify_payload=None, on_verify=None,
                    status_payload=None, status_code=200,
                    status_raw=None, login_payload=None):
        stub = _StubAdminServer(
            status_value=status_value, login_code=login_code,
            on_status=on_status, verify_value=verify_value,
            verify_code=verify_code, verify_payload=verify_payload,
            on_verify=on_verify, status_payload=status_payload,
            status_code=status_code, status_raw=status_raw,
            login_payload=login_payload)
        self.addCleanup(stub.stop)
        token_patcher = mock.patch.object(
            console_um_login.console_um_adapter, "read_control_token",
            return_value="isolated-test-control-token")
        token_patcher.start()
        self.addCleanup(token_patcher.stop)
        status_mock = _make_service_status_mock(stub.port, states)
        status_patcher = mock.patch.object(
            console_adapters, "service_status", status_mock)
        status_patcher.start()
        self.addCleanup(status_patcher.stop)
        wrapper = mock.Mock()
        wrapper_patcher = mock.patch.object(
            console_adapters, "_invoke_wrapper", wrapper)
        wrapper_patcher.start()
        self.addCleanup(wrapper_patcher.stop)
        merged = {"start_wait_s": 5.0, "login_budget_s": 5.0,
                  "poll_interval_s": 0.02}
        if params:
            merged.update(params)
        ctx = _Ctx() if with_ctx else None
        return SimpleNamespace(stub=stub, wrapper=wrapper, ctx=ctx,
                               params=merged, requests=stub.requests)

    # ------------------------------------------------------------- case 1

    def test_01_was_running_login_valid_no_wrapper_calls(self):
        case = self._start_case("Running")
        result = console_um_login.relogin_um("um", case.params, case.ctx)
        self.assertEqual(case.wrapper.call_count, 0)  # no start, no stop
        self.assertEqual(console_adapters.service_status.call_count, 2)
        self.assertTrue(result["was_running"])
        self.assertFalse(result["restored"])
        self.assertEqual(result["final_state"], "valid")
        self.assertFalse(result["login_skipped"])
        self.assertIsInstance(result["login_waited_s"], float)
        self.assertIn("保持运行", result["note"])
        self.assertEqual([(r[0], r[1]) for r in case.requests], [
            ("GET", "/admin/auth/verify"), ("POST", "/admin/auth/login"),
            ("GET", "/admin/auth/status")])
        posts = [r for r in case.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], "/admin/auth/login")
        self.assertIn("X-Admin-Control", posts[0][2])
        self.assertEqual(posts[0][2]["X-Admin-Control"],
                         "isolated-test-control-token")
        self.assertEqual(posts[0][2]["Content-Type"], "application/json")
        self.assertNotIn("Origin", posts[0][2])
        self.assertEqual(json.loads(case.stub.login_bodies[0]),
                         {"visible": False})

    # ------------------------------------------------------------- case 2

    def test_02_was_stopped_start_login_stop_restored(self):
        case = self._start_case(
            ["Stopped", "Stopped", "Running", "Running", "Stopped"],
            status_value="valid")
        result = console_um_login.relogin_um("um", case.params, case.ctx)
        self.assertEqual(case.wrapper.call_count, 2)
        case.wrapper.assert_any_call(
            "start", key="chat2api", caller_holds_lock=False)
        case.wrapper.assert_any_call(
            "stop", key="chat2api", caller_holds_lock=False)
        self.assertEqual(console_adapters.service_status.call_count, 5)
        self.assertFalse(result["was_running"])
        self.assertTrue(result["restored"])
        self.assertEqual(result["final_state"], "valid")
        self.assertIsInstance(result["login_waited_s"], float)
        self.assertIn("已停止", result["note"])

    # ------------------------------------------------------------- case 3

    def test_03_login_timeout_recheck_logging_in_still_restores(self):
        case = self._start_case(
            ["Stopped", "Stopped", "Running", "Running", "Stopped"],
            status_value="logging_in",
            params={"login_budget_s": 2.0, "poll_interval_s": 0.2})
        with self.assertRaises(OperationError) as caught:
            console_um_login.relogin_um("um", case.params, case.ctx)
        error = caught.exception
        self.assertEqual(error.code, "login_timeout_not_settled")
        self.assertIn("不保证后端线程已结束", error.message)
        self.assertIn("logging_in", error.message)
        self.assertIn("服务已停止", error.message)  # finally restored anyway
        stop_calls = [c for c in case.wrapper.call_args_list
                      if c.args and c.args[0] == "stop"]
        self.assertEqual(len(stop_calls), 1)
        self.assertEqual(stop_calls[0].kwargs.get("caller_holds_lock"), False)
        self.assertEqual(console_adapters.service_status.call_count, 5)

    # ------------------------------------------------------------- case 4

    def test_04_cooperative_cancel_during_polling_still_restores(self):
        case = self._start_case(
            ["Stopped", "Stopped", "Running", "Running", "Stopped"],
            status_value="logging_in",
            # Late-bound hook: the first status GET flips the ctx cancel
            # event, simulating a cancel request arriving mid-polling.
            on_status=lambda: case.ctx.event.set(),
            params={"login_budget_s": 5.0, "poll_interval_s": 0.05})
        with self.assertRaises(OperationCancelled):
            console_um_login.relogin_um("um", case.params, case.ctx)
        stop_calls = [c for c in case.wrapper.call_args_list
                      if c.args and c.args[0] == "stop"]
        self.assertEqual(len(stop_calls), 1)
        self.assertEqual(stop_calls[0].kwargs.get("caller_holds_lock"), False)
        self.assertEqual(console_adapters.service_status.call_count, 5)
        self.assertTrue(any("服务已停止并复验 Stopped" in s for s in case.ctx.steps))

    # ------------------------------------------------------------- case 5

    def test_05_start_never_confirmed_no_login_no_stop(self):
        case = self._start_case(
            "Stopped", params={"start_wait_s": 0.1, "poll_interval_s": 0.02})
        with self.assertRaises(OperationError) as caught:
            console_um_login.relogin_um("um", case.params, case.ctx)
        error = caught.exception
        self.assertEqual(error.code, "start_failed")
        self.assertEqual(case.wrapper.call_count, 1)
        case.wrapper.assert_any_call(
            "start", key="chat2api", caller_holds_lock=False)
        self.assertEqual(case.requests, [])  # zero login POST

    # ------------------------------------------------------------- case 6

    def test_06_stop_fails_reports_residual_not_fake_stop(self):
        case = self._start_case(
            ["Stopped", "Stopped", "Running", "Running", "Running"])
        with self.assertRaises(OperationError) as caught:
            console_um_login.relogin_um("um", case.params, case.ctx)
        error = caught.exception
        self.assertEqual(error.code, "stop_failed")
        self.assertIn("残留", error.message)
        self.assertIn("Running", error.message)  # actual observed state
        self.assertIn("登录已完成", error.message)  # login itself settled
        stop_calls = [c for c in case.wrapper.call_args_list
                      if c.args and c.args[0] == "stop"]
        self.assertEqual(stop_calls[0].kwargs.get("caller_holds_lock"), False)

    # ------------------------------------------------------------- case 7

    def test_07_login_not_202_maps_login_unreachable_and_restores(self):
        case = self._start_case(
            ["Stopped", "Stopped", "Running", "Running", "Stopped"],
            login_code=500)
        with self.assertRaises(OperationError) as caught:
            console_um_login.relogin_um("um", case.params, case.ctx)
        error = caught.exception
        self.assertEqual(error.code, "login_unreachable")
        self.assertIn("500", error.message)
        self.assertIn("服务已停止", error.message)  # finally restored anyway
        stop_calls = [c for c in case.wrapper.call_args_list
                      if c.args and c.args[0] == "stop"]
        self.assertEqual(stop_calls[0].kwargs.get("caller_holds_lock"), False)

    def test_27_force_login_skips_verify_and_sends_visible_body(self):
        case = self._start_case(
            "Running", verify_value="valid",
            params={"force": True, "visible": True})
        result = console_um_login.relogin_um("um", case.params, case.ctx)
        self.assertFalse(result["login_skipped"])
        self.assertEqual(result["final_state"], "valid")
        self.assertEqual([(r[0], r[1]) for r in case.requests], [
            ("POST", "/admin/auth/login"),
            ("GET", "/admin/auth/status")])
        posts = [r for r in case.requests if r[0] == "POST"]
        self.assertEqual(posts[0][2]["Content-Type"], "application/json")
        self.assertEqual(json.loads(case.stub.login_bodies[0]),
                         {"visible": True})
        self.assertIn("强制重新登录", " ".join(case.ctx.steps))

    def test_28_force_login_without_visible_sends_headless_body(self):
        case = self._start_case(
            "Running", verify_value="valid", params={"force": True})
        result = console_um_login.relogin_um("um", case.params, case.ctx)
        self.assertFalse(result["login_skipped"])
        self.assertEqual(json.loads(case.stub.login_bodies[0]),
                         {"visible": False})

    def test_29_visible_without_force_rejected_before_any_io(self):
        case = self._start_case("Running", params={"visible": True})
        with self.assertRaises(OperationError) as caught:
            console_um_login.relogin_um("um", case.params, case.ctx)
        self.assertEqual(caught.exception.code, "invalid_params")
        self.assertIn("force", caught.exception.message)
        self.assertEqual(case.requests, [])
        self.assertEqual(case.wrapper.call_count, 0)
        self.assertEqual(console_adapters.service_status.call_count, 0)

    def test_30_force_login_busy_maps_login_busy(self):
        case = self._start_case(
            "Running", login_code=409,
            login_payload={"error": {"type": "login_busy",
                                      "message": "profile busy"}},
            params={"force": True})
        with self.assertRaises(OperationError) as caught:
            console_um_login.relogin_um("um", case.params, case.ctx)
        self.assertEqual(caught.exception.code, "login_busy")
        self.assertIn("login_busy", caught.exception.message)
        self.assertEqual([(r[0], r[1]) for r in case.requests], [
            ("POST", "/admin/auth/login")])

    def test_31_force_visible_unavailable_maps_login_unavailable(self):
        case = self._start_case(
            "Running", login_code=503,
            login_payload={"error": {"type": "visible_login_unavailable",
                                      "message": "no DISPLAY"}},
            params={"force": True, "visible": True})
        with self.assertRaises(OperationError) as caught:
            console_um_login.relogin_um("um", case.params, case.ctx)
        self.assertEqual(caught.exception.code, "login_unavailable")
        self.assertIn("visible_login_unavailable",
                      caught.exception.message)
        self.assertEqual([(r[0], r[1]) for r in case.requests], [
            ("POST", "/admin/auth/login")])

    def test_32_status_login_busy_maps_login_busy_without_waiting(self):
        case = self._start_case("Running", status_payload={
            "state": "failed", "error": "login_busy",
            "recovery": {"state": "exhausted", "pause_reason": "login_busy"},
        }, params={"force": True, "login_budget_s": 5.0})
        started = time.monotonic()
        with self.assertRaises(OperationError) as caught:
            console_um_login.relogin_um("um", case.params, case.ctx)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(caught.exception.code, "login_busy")

    def test_33_status_visible_login_unavailable_maps_unavailable(self):
        case = self._start_case("Running", status_payload={
            "state": "failed", "error": "visible_login_unavailable",
            "recovery": {"state": "exhausted",
                          "pause_reason": "visible_login_unavailable"},
        }, params={"force": True, "visible": True, "login_budget_s": 5.0})
        started = time.monotonic()
        with self.assertRaises(OperationError) as caught:
            console_um_login.relogin_um("um", case.params, case.ctx)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(caught.exception.code, "login_unavailable")

    def test_08_verified_valid_skips_login_and_keeps_running(self):
        case = self._start_case("Running", verify_value="valid")
        result = console_um_login.relogin_um("UM", case.params, case.ctx)
        self.assertEqual([(r[0], r[1]) for r in case.requests], [
            ("GET", "/admin/auth/verify")])
        self.assertEqual(case.wrapper.call_count, 0)
        self.assertEqual(console_adapters.service_status.call_count, 2)
        self.assertTrue(result["login_skipped"])
        self.assertEqual(result["final_state"], "valid")
        self.assertEqual(result["login_waited_s"], 0.0)
        self.assertIn("无需重新登录", result["note"])
        headers = case.requests[0][2]
        self.assertEqual(headers["X-Admin-Control"],
                         "isolated-test-control-token")
        self.assertNotIn("Origin", headers)
        self.assertNotIn("isolated-test-control-token", str(result))
        self.assertNotIn("isolated-test-control-token", str(case.ctx.steps))

    def test_09_verified_valid_restores_original_stopped_state(self):
        case = self._start_case(
            ["Stopped", "Running", "Running", "Stopped"],
            verify_value="valid")
        result = console_um_login.relogin_um("UM", case.params, case.ctx)
        self.assertTrue(result["login_skipped"])
        self.assertTrue(result["restored"])
        self.assertFalse(result["was_running"])
        self.assertEqual(case.wrapper.call_args_list, [
            mock.call("start", key="chat2api", caller_holds_lock=False),
            mock.call("stop", key="chat2api", caller_holds_lock=False)])
        self.assertIn("服务已停止并复验 Stopped", result["note"])
        self.assertEqual([(r[0], r[1]) for r in case.requests], [
            ("GET", "/admin/auth/verify")])

    def test_10_rejected_credential_follows_existing_login_sequence(self):
        case = self._start_case("Running", verify_value="rejected")
        result = console_um_login.relogin_um("UM", case.params, case.ctx)
        self.assertFalse(result["login_skipped"])
        self.assertEqual(result["final_state"], "valid")
        self.assertEqual([(r[0], r[1]) for r in case.requests], [
            ("GET", "/admin/auth/verify"), ("POST", "/admin/auth/login"),
            ("GET", "/admin/auth/status")])

    def test_11_unknown_verification_refuses_login_and_restores(self):
        case = self._start_case(
            ["Stopped", "Running", "Running", "Stopped"],
            verify_value="unknown")
        with self.assertRaises(OperationError) as caught:
            console_um_login.relogin_um("UM", case.params, case.ctx)
        error = caught.exception
        self.assertEqual(error.code, "login_verification_unknown")
        self.assertIn("无法确认当前认证", error.message)
        self.assertIn("暂不打开登录", error.message)
        self.assertIn("服务已停止并复验 Stopped", error.message)
        self.assertEqual([(r[0], r[1]) for r in case.requests], [
            ("GET", "/admin/auth/verify")])
        self.assertEqual(case.wrapper.call_count, 2)

    def test_12_verification_http_error_refuses_login_and_restores(self):
        case = self._start_case(
            ["Stopped", "Running", "Running", "Stopped"],
            verify_code=503)
        with self.assertRaises(OperationError) as caught:
            console_um_login.relogin_um("UM", case.params, case.ctx)
        error = caught.exception
        self.assertEqual(error.code, "verify_query_failed")
        self.assertIn("无法确认当前认证", error.message)
        self.assertIn("暂不打开登录", error.message)
        self.assertIn("503", error.message)
        self.assertIn("服务已停止并复验 Stopped", error.message)
        self.assertEqual([(r[0], r[1]) for r in case.requests], [
            ("GET", "/admin/auth/verify")])

    def test_13_invalid_verification_response_refuses_login(self):
        for payload in ({"state": "valid"}, {"verify": "unrecognized"}, []):
            with self.subTest(payload=payload):
                case = self._start_case("Running", verify_payload=payload)
                with self.assertRaises(OperationError) as caught:
                    console_um_login.relogin_um("UM", case.params, case.ctx)
                error = caught.exception
                self.assertEqual(error.code, "verify_schema")
                self.assertIn("无法确认当前认证", error.message)
                self.assertIn("暂不打开登录", error.message)
                self.assertEqual([(r[0], r[1]) for r in case.requests], [
                    ("GET", "/admin/auth/verify")])
                self.doCleanups()

    def test_14_cancel_during_verification_sends_no_login_and_restores(self):
        case = self._start_case(
            ["Stopped", "Running", "Running", "Stopped"],
            on_verify=lambda: case.ctx.event.set())
        with self.assertRaises(OperationCancelled):
            console_um_login.relogin_um("UM", case.params, case.ctx)
        self.assertEqual([(r[0], r[1]) for r in case.requests], [
            ("GET", "/admin/auth/verify")])
        self.assertEqual(case.wrapper.call_count, 2)
        self.assertTrue(any("服务已停止并复验 Stopped" in step
                            for step in case.ctx.steps))

    def test_15_skipped_login_reports_restore_failure_honestly(self):
        case = self._start_case(
            ["Stopped", "Running", "Running", "Running"],
            verify_value="valid")
        with self.assertRaises(OperationError) as caught:
            console_um_login.relogin_um("UM", case.params, case.ctx)
        error = caught.exception
        self.assertEqual(error.code, "stop_failed")
        self.assertIn("无需重新登录", error.message)
        self.assertIn("残留", error.message)
        self.assertNotIn("登录已完成", error.message)
        self.assertEqual([(r[0], r[1]) for r in case.requests], [
            ("GET", "/admin/auth/verify")])


    def test_16_verification_connection_closed_refuses_login_and_restores(self):
        case = self._start_case(
            ["Stopped", "Running", "Running", "Stopped"], verify_code=None)
        with self.assertRaises(OperationError) as caught:
            console_um_login.relogin_um("UM", case.params, case.ctx)
        error = caught.exception
        self.assertEqual(error.code, "status_unreachable")
        self.assertIn("无法确认当前认证", error.message)
        self.assertIn("暂不打开登录", error.message)
        self.assertIn("服务已停止并复验 Stopped", error.message)
        self.assertEqual([(r[0], r[1]) for r in case.requests], [
            ("GET", "/admin/auth/verify")])
        self.assertEqual(case.wrapper.call_count, 2)

    def test_17_cancelled_before_start_performs_no_io(self):
        case = self._start_case("Stopped")
        case.ctx.event.set()
        with self.assertRaises(OperationCancelled):
            console_um_login.relogin_um("UM", case.params, case.ctx)
        self.assertEqual(case.requests, [])
        self.assertEqual(case.wrapper.call_count, 0)
        self.assertEqual(console_adapters.service_status.call_count, 0)

    def test_18_valid_without_context_still_skips_login(self):
        case = self._start_case(
            "Running", verify_value="valid", with_ctx=False)
        result = console_um_login.relogin_um("UM", case.params)
        self.assertTrue(result["login_skipped"])
        self.assertEqual(result["final_state"], "valid")
        self.assertEqual([(r[0], r[1]) for r in case.requests], [
            ("GET", "/admin/auth/verify")])

    # ------------------------------------------------------- ticket #15

    @staticmethod
    def _terminal_status(state, error, pause_reason):
        return {
            "state": state,
            "label": "认证失败（需人工：umpass_mfa_required）",
            "error": error,
            "recovery": {
                "state": "exhausted", "round": 1, "session_starts": 1,
                "max_sessions": 6, "next_attempt_epoch": None,
                "pause_reason": pause_reason,
            },
            "login_window_deadline_epoch": 2800.0,
            "login_window_s": 1800.0,
        }

    def test_20_manual_takeover_maps_to_reason_without_waiting(self):
        case = self._start_case("Running", status_payload=self._terminal_status(
            "failed",
            "manual_takeover_required:umpass_mfa_required",
            "manual_takeover_required:umpass_mfa_required"),
            params={"login_budget_s": 5.0})
        started = time.monotonic()
        with self.assertRaises(OperationError) as caught:
            console_um_login.relogin_um("um", case.params, case.ctx)
        self.assertLess(time.monotonic() - started, 1.0)
        error = caught.exception
        self.assertEqual(error.code, "login_manual_takeover_required")
        self.assertIn(
            "manual_takeover_required:umpass_mfa_required", error.message)
        self.assertNotIn("isolated-test-control-token", error.message)
        self.assertEqual(case.wrapper.call_count, 0)  # was running: no restart

    def test_21_cancelled_round_maps_to_cancelled_without_waiting(self):
        case = self._start_case("Running", status_payload=self._terminal_status(
            "cancelled", "cancelled", "cancelled"),
            params={"login_budget_s": 5.0})
        started = time.monotonic()
        with self.assertRaises(OperationCancelled):
            console_um_login.relogin_um("um", case.params, case.ctx)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(case.wrapper.call_count, 0)  # was running: no restart

    def test_22_terminal_credential_failure_maps_to_failed_state(self):
        case = self._start_case("Running", status_payload=self._terminal_status(
            "failed", "upstream_credential_rejected",
            "upstream_credential_rejected"),
            params={"login_budget_s": 5.0})
        started = time.monotonic()
        with self.assertRaises(OperationError) as caught:
            console_um_login.relogin_um("um", case.params, case.ctx)
        self.assertLess(time.monotonic() - started, 1.0)
        error = caught.exception
        self.assertEqual(error.code, "login_failed")
        self.assertIn("upstream_credential_rejected", error.message)

    def test_23_retrying_failure_still_waits_for_the_budget(self):
        # 只把 recovery 已结束的终态当作终态；仍在 retrying 的 failed
        # 继续等，符合 chat2api 内部可能重试的既有约定。
        case = self._start_case("Running", status_payload={
            "state": "failed", "label": "认证失败", "error": "window_closed",
            "recovery": {
                "state": "running", "round": 1, "session_starts": 1,
                "max_sessions": 6, "next_attempt_epoch": None,
                "pause_reason": None,
            },
        }, params={"login_budget_s": 0.2, "poll_interval_s": 0.02})
        started = time.monotonic()
        with self.assertRaises(OperationError) as caught:
            console_um_login.relogin_um("um", case.params, case.ctx)
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        self.assertEqual(caught.exception.code, "login_timeout_not_settled")

    def test_24_soft_manual_progress_does_not_end_the_wait(self):
        # ticket #41：只有真正 manual 的三类原因结束本轮；
        # click-swallowed / unrecognized-control 仍在轮询 token。
        # ticket #74：duo_prompt_waiting 已改为立即返回人工接管，
        # 不再属于这里的“继续等待”集合。
        for reason in ("openwebui_click_swallowed",
                       "openwebui_button_unrecognized",
                       "umpass_click_swallowed",
                       "umpass_submit_unrecognized"):
            with self.subTest(reason=reason):
                status = {
                    "state": "logging_in",
                    "error": "manual_takeover_required:" + reason,
                    "recovery": {"state": "running", "pause_reason": None},
                }
                self.assertIsNone(
                    console_um_login._terminal_login_failure(status),
                    "契约：%s 不是 terminal，控制台必须继续等待" % reason)

    def test_25_only_the_three_manual_reasons_end_the_round(self):
        for reason in ("umpass_mfa_required", "umpass_prefill_missing",
                       "unrecognized_page"):
            with self.subTest(reason=reason):
                status = {
                    "state": "failed",
                    "error": "manual_takeover_required:" + reason,
                    "recovery": {"state": "exhausted",
                                 "pause_reason": "manual_takeover_required:"
                                 + reason},
                }
                failure = console_um_login._terminal_login_failure(status)
                self.assertIsNotNone(failure)
                self.assertEqual(failure[0], "login_manual_takeover_required")
                self.assertIn("manual_takeover_required:" + reason,
                              failure[1])

    def test_26_soft_manual_status_keeps_polling_until_the_budget(self):
        case = self._start_case("Running", status_payload={
            "state": "logging_in",
            "label": "登录中（需人工：openwebui_click_swallowed）",
            "error": "manual_takeover:openwebui_click_swallowed",
            "recovery": {
                "state": "running", "round": 1, "session_starts": 1,
                "max_sessions": 6, "next_attempt_epoch": None,
                "pause_reason": None,
            },
        }, params={"login_budget_s": 0.2, "poll_interval_s": 0.02})
        started = time.monotonic()
        with self.assertRaises(OperationError) as caught:
            console_um_login.relogin_um("um", case.params, case.ctx)
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        self.assertEqual(caught.exception.code, "login_timeout_not_settled")

    def test_28_duo_wait_returns_manual_takeover_without_waiting(self):
        # ticket #74：控制台看到 login.manual_reason=duo_prompt_waiting
        # 立即返回稳定的 login_manual_takeover_required，不再等预算耗尽；
        # 后端轮次仍在等待，消息说明可打开可见窗口。
        case = self._start_case("Running", status_payload={
            "state": "logging_in",
            "label": "登录中（需人工：duo_prompt_waiting）",
            "error": None,
            "login": {
                "phase": "awaiting_duo",
                "origin": "https://api-1234.duosecurity.com/prompt/xyz",
                "manual_reason": "duo_prompt_waiting",
            },
            "recovery": {
                "state": "running", "round": 1, "session_starts": 1,
                "max_sessions": 6, "next_attempt_epoch": None,
                "pause_reason": None,
            },
        }, params={"login_budget_s": 5.0, "poll_interval_s": 0.02})
        started = time.monotonic()
        with self.assertRaises(OperationError) as caught:
            console_um_login.relogin_um("um", case.params, case.ctx)
        self.assertLess(time.monotonic() - started, 1.0)
        error = caught.exception
        self.assertEqual(error.code, "login_manual_takeover_required")
        self.assertEqual(
            error.message,
            "登录需要本人确认 Duo 推送；本轮仍在等待，可打开可见窗口")
        self.assertNotIn("isolated-test-control-token", error.message)
        self.assertEqual(case.wrapper.call_count, 0)  # was running: no restart

    def test_19_unknown_next_attempt_projects_as_null(self):
        # A8/A7: while another instance's live lease is the only reason for
        # waiting, the next attempt is UNKNOWN (null), not the old lease
        # expiry. The console projection must keep that null and copy only the
        # whitelisted progress fields; the lease expiry stays private.
        case = self._start_case("Running", status_payload={
            "state": "waiting", "label": "", "error": None,
            "recovery": {
                "state": "waiting", "round": 2, "session_starts": 1,
                "max_sessions": 3, "next_attempt_epoch": None,
                "pause_reason": None,
                "lease_expires_epoch": 9999999999.0}})
        result = console_um_adapter.um_auth_status("um", case.params)
        self.assertEqual(result["state"], "waiting")
        self.assertEqual(result["source"], "memory-state")
        recovery = result["recovery"]
        self.assertEqual(recovery["state"], "waiting")
        self.assertEqual(recovery["round"], 2)
        self.assertEqual(recovery["session_starts"], 1)
        self.assertEqual(recovery["max_sessions"], 3)
        self.assertIsNone(recovery["next_attempt_epoch"])
        self.assertNotIn("lease_expires_epoch", recovery)


class ConsoleAuthStatusReadTests(unittest.TestCase):
    def test_a_finished_round_with_unknown_verification_ends_console_wait(self):
        status = {
            "state": "unknown", "error": None, "login": None,
            "recovery": {"state": "idle"}, "read_error": None,
        }
        with mock.patch.object(console_um_login, "_read_auth_status", side_effect=[
            status, AssertionError("已结束的核验不应继续作为登录中轮询"),
        ]) as read:
            with self.assertRaises(OperationError) as caught:
                console_um_login._wait_login_valid("http://fixture", "fixture", None, 30, 0)
        self.assertEqual(caught.exception.code, "login_verification_unknown")
        self.assertNotIn("需人工", caught.exception.message)
        self.assertEqual(read.call_count, 1)

    """Ticket #41：控制台状态读取复用 um_adapter 的同一错误分类。

    ``console/um_login`` 以前把 401/403、URLError、非 200、JSON 损坏
    统一压成 ``{state: None, error: None}``，登录等待只能看到超时。
    这里要求读面保留 ``control_denied`` / ``status_unreachable`` /
    ``status_query_failed`` / ``status_schema``。
    """

    def _case(self, **kwargs):
        case = ConsoleUmLoginTests._start_case(self, "Running", **kwargs)
        return case

    def _read(self, **kwargs):
        case = self._case(**kwargs)
        return console_um_login._read_auth_status(
            "http://127.0.0.1:%d" % case.stub.port, "token")

    def test_http_401_keeps_control_denied(self):
        status = self._read(status_code=401)
        self.assertIsNone(status["state"])
        self.assertEqual(status["read_error"]["code"], "control_denied")

    def test_closed_connection_keeps_status_unreachable(self):
        status = self._read(status_code=None)
        self.assertIsNone(status["state"])
        self.assertEqual(status["read_error"]["code"], "status_unreachable")

    def test_non_200_keeps_status_query_failed(self):
        status = self._read(status_code=503)
        self.assertIsNone(status["state"])
        self.assertEqual(status["read_error"]["code"],
                         "status_query_failed")

    def test_corrupt_json_keeps_status_schema(self):
        status = self._read(status_raw=b"{not json")
        self.assertIsNone(status["state"])
        self.assertEqual(status["read_error"]["code"], "status_schema")

    def test_missing_state_keeps_status_schema(self):
        status = self._read(status_payload={"label": "no state"})
        self.assertIsNone(status["state"])
        self.assertEqual(status["read_error"]["code"], "status_schema")

    def test_control_denied_ends_the_wait_instead_of_a_timeout(self):
        case = self._case(status_code=401,
                          params={"login_budget_s": 5.0,
                                   "poll_interval_s": 0.02})
        started = time.monotonic()
        with self.assertRaises(OperationError) as caught:
            console_um_login.relogin_um("um", case.params, case.ctx)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(caught.exception.code, "control_denied")

    def test_transient_read_error_keeps_polling_until_the_budget(self):
        case = self._case(status_code=503,
                          params={"login_budget_s": 0.2,
                                   "poll_interval_s": 0.02})
        started = time.monotonic()
        with self.assertRaises(OperationError) as caught:
            console_um_login.relogin_um("um", case.params, case.ctx)
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        self.assertEqual(caught.exception.code, "login_timeout_not_settled")
        self.assertIn("status_query_failed", caught.exception.message)

    def test_status_poll_keeps_the_historical_5s_timeout(self):
        # ticket #42 item 17：复用 adapter 后控制台每次轮询仍只阻塞 5s，
        # 不能悄悄变成 adapter 默认的 10s。
        status = {
            "state": "logging_in", "label": "登录中", "error": None,
            "recovery": {"state": "running"},
            "primary": None,
            "login_window_deadline_epoch": None,
            "login_window_s": None,
            "checked_epoch": 1,
        }
        with mock.patch.object(
                console_um_adapter, "read_auth_status",
                return_value=status) as read:
            console_um_login._read_auth_status("http://127.0.0.1:1", "token")
        self.assertEqual(read.call_args.kwargs.get("timeout"), 5.0)

    def test_cleanup_failure_keeps_the_primary_manual_reason(self):
        # ticket #42 item 16：清理失败不能吞掉原始 manual 原因；控制台必须
        # 同时显示清理失败与原始原因，并仍映射为人工接管。
        status = {
            "state": "failed",
            "label": "认证失败（清理失败）",
            "error": "browser_cleanup_failed",
            "primary": "manual_takeover_required:umpass_mfa_required",
            "recovery": {
                "state": "exhausted", "pause_reason": "browser_cleanup_failed",
            },
        }
        failure = console_um_login._terminal_login_failure(status)
        self.assertIsNotNone(failure)
        self.assertEqual(failure[0], "login_manual_takeover_required")
        self.assertIn("browser_cleanup_failed", failure[1])
        self.assertIn("manual_takeover_required:umpass_mfa_required",
                      failure[1])

    def test_cleanup_failure_keeps_a_non_manual_primary_reason(self):
        status = {
            "state": "failed",
            "label": "认证失败（清理失败）",
            "error": "browser_cleanup_failed",
            "primary": "timeout",
            "recovery": {
                "state": "exhausted", "pause_reason": "browser_cleanup_failed",
            },
        }
        failure = console_um_login._terminal_login_failure(status)
        self.assertIsNotNone(failure)
        self.assertEqual(failure[0], "login_failed")
        self.assertIn("browser_cleanup_failed", failure[1])
        self.assertIn("timeout", failure[1])

    def test_legacy_manual_prefix_still_ends_the_wait(self):
        # 旧实例把软信号写成 manual_takeover:<reason>；控制台仍须把它
        # 当终端人工接管，而不是继续等到预算耗尽。
        status = {
            "state": "failed",
            "error": "manual_takeover:umpass_mfa_required",
            "recovery": {"state": "exhausted",
                         "pause_reason": "manual_takeover:umpass_mfa_required"},
        }
        failure = console_um_login._terminal_login_failure(status)
        self.assertIsNotNone(failure)
        self.assertEqual(failure[0], "login_manual_takeover_required")
        self.assertIn("umpass_mfa_required", failure[1])
        self.assertNotIn("清理失败", failure[1])

    def test_terminal_manual_contract_is_shared_with_the_service(self):
        # ticket #42 item 12：terminal 原因与前缀只有一份定义，console 直接
        # 依赖 chat2api 的公开契约常量，避免两边各自维护而漂移。
        self.assertIs(console_um_login.terminal_manual_reason,
                      login_contract.terminal_manual_reason)
        self.assertEqual(console_um_login._MANUAL_TAKEOVER_PREFIX,
                         login_contract.MANUAL_TAKEOVER_REQUIRED_PREFIX)


if __name__ == "__main__":
    unittest.main()
