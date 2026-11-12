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
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

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
                 verify_payload=None, on_verify=None, status_payload=None):
        self.requests = []  # (method, path, headers-dict)
        self.login_code = login_code
        self.on_status = on_status
        self.verify_code = verify_code
        self.verify_payload = ({"verify": verify_value, "reason": None,
                                "upstream_status": None, "checked_epoch": 1}
                               if verify_payload is None else verify_payload)
        self.on_verify = on_verify
        self.status_payload = status_payload
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
                self._json(outer.login_code,
                           {"state": "logging_in", "label": "", "error": None})

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
                    if outer.status_payload is not None:
                        self._json(200, outer.status_payload)
                    else:
                        self._json(200, {"state": outer._status_next(),
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
                    status_payload=None):
        stub = _StubAdminServer(
            status_value=status_value, login_code=login_code,
            on_status=on_status, verify_value=verify_value,
            verify_code=verify_code, verify_payload=verify_payload,
            on_verify=on_verify, status_payload=status_payload)
        self.addCleanup(stub.stop)
        token_patcher = mock.patch.object(
            console_um_login.console_um_adapter, "_read_control_token",
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
        self.assertNotIn("Origin", posts[0][2])

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


if __name__ == "__main__":
    unittest.main()
