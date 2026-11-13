# -*- coding: utf-8 -*-
"""Isolation self-test for the shared chat2api HTTP harness (B8-0).

The 2026-10-02 incident: chat/Messages suites released the USAGE_LOG patch
(LIFO cleanup) while a post-response handler thread was still inside
``record_usage`` — the late write then landed on the PRODUCTION
``data/usage-chat2api.jsonl``. These tests pin the fixed lifecycle:

  1. A ledger write that is still in flight after the client received its
     response (a "delayed response" shape) drains into the PRIVATE temp
     ledger; the redirect is provably still active after full drain.
  2. The LIFO registration order puts the server teardown BEFORE the patch
     release and the temp-dir release LAST.
  3. A wedged handler makes the teardown fail loudly; the real TestCase LIFO
     cleanup still restores patches, while the request keeps its fixed private
     ledger and the directory survives until its accepted handler exits.
  4. The sentinels fail on a non-loopback bind, a real service port, a
     live-environment local key, or a non-stub upstream credential.
"""

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

sys_path_marker = str(Path(__file__).resolve().parent)
if sys_path_marker not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path_marker)
import _chat2api_http_harness as harness  # noqa: E402

import importlib.util

CHAT2API_PATH = (Path(__file__).resolve().parents[1] / "chat2api"
                 / "chat2api.py")
SPEC = importlib.util.spec_from_file_location(
    "chat2api_http_isolation", CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
__import__("sys").modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)

LOCAL_KEY = "local-secret-http-isolation"
STUB_TOKEN = "stub-token"


class _ImmediateUpstream(BaseHTTPRequestHandler):
    """Minimal upstream: one 200 chat completion, then done."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "hi"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


class _UsageGateLock:
    """Ledger-write gate: record_usage blocks until the test releases it.

    This pins the exact incident shape — the client already has its response
    while the handler thread is still inside ``record_usage`` — without any
    sleep-based guess about scheduler timing.
    """

    def __init__(self):
        self._inner = threading.Lock()
        self.entered = threading.Event()
        self.release_gate = threading.Event()

    def __enter__(self):
        self.entered.set()
        self.release_gate.wait(timeout=30)
        self._inner.acquire()
        return self

    def __exit__(self, *_exc):
        self._inner.release()
        return False


class _FakeTest:
    """Minimal addCleanup recorder to pin the harness's LIFO contract."""

    def __init__(self):
        self.cleanups = []

    def addCleanup(self, function, *args, **kwargs):
        self.cleanups.append((function, args, kwargs))


def _start_server(handler, name):
    server = harness.TrackedHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class TestChat2ApiHttpHarnessIsolation(unittest.TestCase):
    def setUp(self):
        self.gate = _UsageGateLock()

    def _gateway_with_stub(self, handler_class=chat2api.Handler):
        upstream = _start_server(_ImmediateUpstream, "upstream-stub")
        args = SimpleNamespace(
            base_url=f"http://127.0.0.1:{upstream.server_port}",
            token=STUB_TOKEN, profile="profile", login_timeout=1,
            use_api_key=False, no_browser=True)
        proxy = chat2api.Proxy({"token": STUB_TOKEN}, args)
        gateway = harness.TrackedHTTPServer(("127.0.0.1", 0), handler_class)
        gateway.proxy = proxy
        gateway.local_api_key = LOCAL_KEY
        gateway.require_local_api_key = True
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        return upstream, gateway

    def test_delayed_ledger_write_lands_in_private_temp_ledger(self):
        """The incident shape: response sent, record_usage still pending.

        The client receives its 200 while the handler is parked inside the
        gated ``record_usage``; a full drain then completes and the row MUST
        appear in the temp ledger — with the redirect still active (the
        release runs only after this drain).
        """
        _, ledger = harness.install_usage_isolation(self, chat2api)
        upstream, gateway = self._gateway_with_stub()
        try:
            with mock.patch.object(chat2api, "USAGE_LOG", str(ledger)), \
                    mock.patch.object(chat2api, "_usage_disabled", False), \
                    mock.patch.object(chat2api, "_usage_lock", self.gate):
                response = requests.post(
                    f"http://127.0.0.1:{gateway.server_port}"
                    f"/v1/chat/completions",
                    json={"model": "Gate-Model-A",
                          "messages": [{"role": "user", "content": "hi"}],
                          "max_tokens": 8, "stream": False},
                    headers={"Authorization": f"Bearer {LOCAL_KEY}"},
                    timeout=15)
                self.assertEqual(response.status_code, 200)
                # The handler is now parked inside record_usage: nothing has
                # been written yet, and the handler thread is still active.
                self.assertTrue(self.gate.entered.wait(5))
                self.assertFalse(ledger.exists())
                self.assertGreaterEqual(gateway.active_requests(), 1)
                self._gate_release_and_drain(gateway)
                rows = [json.loads(line) for line in
                        ledger.read_text(encoding="utf-8").splitlines()
                        if line.strip()]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["model"], "Gate-Model-A")
                self.assertEqual(rows[0]["status"], "ok")
                # Redirect STILL active after the full drain: the patch is
                # released only by a later cleanup, never mid-teardown.
                self.assertEqual(chat2api.USAGE_LOG, str(ledger))
        finally:
            harness.shutdown_drain_close(gateway, name="gateway")
            harness.shutdown_drain_close(upstream, name="upstream-stub")

    def _gate_release_and_drain(self, gateway):
        self.gate.release_gate.set()
        deadline = time.monotonic() + 10
        while gateway.active_requests() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(gateway.drain(10), 0)

    def test_lifo_order_releases_patch_only_after_server_teardown(self):
        """Registration order contract of install_usage_isolation.

        Running a recorded cleanup stack in LIFO order: the server teardown
        must run while the redirect is STILL active, and the redirect must
        only be released by a LATER cleanup. The temp dir is removed last.
        """
        original_log = chat2api.USAGE_LOG
        fake = _FakeTest()
        tmp_dir, ledger = harness.install_usage_isolation(fake, chat2api)
        self.assertEqual(chat2api.USAGE_LOG, str(ledger))
        gateway = harness.TrackedHTTPServer(("127.0.0.1", 0),
                                            chat2api.Handler)
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        harness.install_tracked_server(fake, gateway, name="gateway")
        try:
            cleanups = list(fake.cleanups)
            self.assertEqual(len(cleanups), 4)  # tmp, log, disabled, server
            # LIFO #1: server teardown runs FIRST — redirect still active.
            function, args, kwargs = cleanups.pop()
            function(*args, **kwargs)
            self.assertEqual(chat2api.USAGE_LOG, str(ledger))
            self.assertTrue(tmp_dir.exists())
            # LIFO #2: _usage_disabled patch stop — redirect STILL active.
            function, args, kwargs = cleanups.pop()
            function(*args, **kwargs)
            self.assertEqual(chat2api.USAGE_LOG, str(ledger))
            # LIFO #3: USAGE_LOG patch stop — only now the redirect releases.
            function, args, kwargs = cleanups.pop()
            function(*args, **kwargs)
            self.assertEqual(chat2api.USAGE_LOG, original_log)
            # LIFO #4: the temp dir is released LAST.
            self.assertTrue(tmp_dir.exists())
            function, args, kwargs = cleanups.pop()
            function(*args, **kwargs)
            self.assertFalse(tmp_dir.exists())
        finally:
            chat2api.USAGE_LOG = original_log

    def test_keepalive_waiter_does_not_block_the_drain(self):
        """A connection parked in the keep-alive readline (its request long
        finished, or none ever sent) holds NO pending ledger work: the
        drain must not wait for its thread, and the teardown must still
        close the listener."""
        gateway = harness.TrackedHTTPServer(("127.0.0.1", 0),
                                            chat2api.Handler)
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        sock = socket.create_connection(("127.0.0.1", gateway.server_port),
                                        timeout=5)
        try:
            time.sleep(0.2)  # let the accept loop spawn the handler thread
            self.assertEqual(gateway.active_requests(), 0)
            # No exception: the parked reader is not an in-flight request.
            harness.shutdown_drain_close(gateway, name="parked-readline",
                                         drain_timeout=5)
            self.assertEqual(gateway.socket.fileno(), -1)
        finally:
            sock.close()

    def test_unscoped_server_cannot_borrow_the_next_suites_ledger(self):
        first = _FakeTest()
        harness.install_usage_isolation(first, chat2api)
        for function, args, kwargs in reversed(first.cleanups):
            function(*args, **kwargs)

        class UnscopedHandler(chat2api.Handler):
            def do_GET(self):
                try:
                    chat2api.record_usage("Unscoped-Model", False, time.time(),
                        {"prompt_tokens": 1, "completion_tokens": 1}, 1, 1)
                except AssertionError:
                    raw = b"refused"
                else:
                    raw = b"borrowed"
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        gateway = _start_server(UnscopedHandler, "unscoped")
        second = _FakeTest()
        _, second_ledger = harness.install_usage_isolation(second, chat2api)
        try:
            with requests.get(f"http://127.0.0.1:{gateway.server_port}/", timeout=5) as response:
                self.assertEqual(response.content, b"refused")
            self.assertEqual(gateway.drain(5), 0)
            self.assertFalse(second_ledger.exists())
        finally:
            harness.shutdown_drain_close(gateway, name="unscoped")
            for function, args, kwargs in reversed(second.cleanups):
                function(*args, **kwargs)

    def test_teardown_fails_loudly_when_a_handler_stays_active(self):
        """A handler parked inside record_usage must fail a bounded drain."""
        _, ledger = harness.install_usage_isolation(self, chat2api)
        upstream, gateway = self._gateway_with_stub()
        try:
            with mock.patch.object(chat2api, "USAGE_LOG", str(ledger)), \
                    mock.patch.object(chat2api, "_usage_disabled", False), \
                    mock.patch.object(chat2api, "_usage_lock", self.gate):
                try:
                    requests.post(
                        f"http://127.0.0.1:{gateway.server_port}"
                        f"/v1/chat/completions",
                        json={"model": "Gate-Model-A",
                              "messages": [{"role": "user", "content": "hi"}],
                              "max_tokens": 8, "stream": False},
                        headers={"Authorization": f"Bearer {LOCAL_KEY}"},
                        timeout=15)
                    self.assertTrue(self.gate.entered.wait(5))
                    with self.assertRaises(AssertionError) as caught:
                        harness.shutdown_drain_close(gateway, name="wedged",
                                                     drain_timeout=0.3)
                    self.assertIn("still in flight", str(caught.exception))
                    self.assertEqual(gateway.socket.fileno(), -1)
                finally:
                    self.gate.release_gate.set()
                    self.assertEqual(gateway.drain(10), 0)
        finally:
            harness.shutdown_drain_close(upstream, name="upstream-stub")

    def test_sentinels_reject_nonloopback_real_port_and_production_path(self):
        fake = SimpleNamespace(server_address=("192.0.2.1", 45678))
        with self.assertRaises(AssertionError) as caught:
            harness.assert_loopback_ephemeral_server(fake, name="fake")
        self.assertIn("non-loopback", str(caught.exception))

        real_port = SimpleNamespace(server_address=("127.0.0.1", 8000))
        with self.assertRaises(AssertionError) as caught:
            harness.assert_loopback_ephemeral_server(real_port, name="fake")
        self.assertIn("real service port", str(caught.exception))

        # The production default computed from the module must keep pointing
        # at the real ledger layout, and never inside a harness temp dir.
        production = harness.production_ledger_path(chat2api)
        self.assertTrue(production.endswith(
            os.path.join("data", "usage-chat2api.jsonl")))
        with tempfile.TemporaryDirectory(
                prefix="chat2api-test-usage-") as tmp_name:
            self.assertFalse(production.startswith(str(Path(tmp_name))))

        live = harness.TrackedHTTPServer(("127.0.0.1", 0), chat2api.Handler)
        try:
            harness.assert_loopback_ephemeral_server(live, name="live")
        finally:
            live.server_close()

    def test_stub_credential_sentinel_rejects_live_env_key(self):
        with mock.patch.dict(os.environ,
                             {"CHAT2API_LOCAL_API_KEY": "live-secret-9"}):
            with self.assertRaises(AssertionError) as caught:
                harness.assert_stub_credentials(
                    chat2api, local_key="live-secret-9",
                    upstream_token=STUB_TOKEN,
                    stub_token_literal=STUB_TOKEN)
            self.assertIn("CHAT2API_LOCAL_API_KEY", str(caught.exception))
            harness.assert_stub_credentials(
                chat2api, local_key="some-other-test-key",
                upstream_token=STUB_TOKEN, stub_token_literal=STUB_TOKEN)
        with self.assertRaises(AssertionError):
            harness.assert_stub_credentials(
                chat2api, local_key=LOCAL_KEY,
                upstream_token="real-credential-token",
                stub_token_literal=STUB_TOKEN)
        with self.assertRaises(AssertionError):
            harness.assert_stub_credentials(
                chat2api, local_key=STUB_TOKEN,  # local key must differ
                upstream_token=STUB_TOKEN,
                stub_token_literal=STUB_TOKEN)

    def _failed_cleanup_with_late_record(self, *, new_suite=False,
                                         close_failure=False,
                                         setup_failure=False,
                                         shutdown_failure=False,
                                         cancel_cleanup=False):
        """Run actual TestCase cleanups while a real handler has not recorded.

        The outer default is a synthetic production sentinel in a private
        directory. Even the pre-fix implementation can only write that
        sentinel, never the real ledger. The gate pauses BEFORE record_usage,
        so this also covers a handler which has not yet resolved USAGE_LOG.
        """
        entered, release, finished = (threading.Event() for _ in range(3))
        observed = {}
        failure_result = unittest.TestResult()
        second = _FakeTest()
        with tempfile.TemporaryDirectory(prefix="chat2api-cleanup-proof-") as root:
            sentinel = Path(root) / "synthetic-production.jsonl"
            with mock.patch.object(chat2api, "USAGE_LOG", str(sentinel)):
                outer = self

                class CleanupCase(unittest.TestCase):
                    def setUp(case):
                        temp_dir, ledger = harness.install_usage_isolation(
                            case, chat2api)
                        observed.update(temp_dir=temp_dir, ledger=ledger)
                        recorder = chat2api.record_usage

                        def delayed_record(*args, **kwargs):
                            entered.set()
                            if not release.wait(10):
                                raise AssertionError("usage gate was not released")
                            try:
                                recorder(*args, **kwargs)
                                observed["private_rows"] = (
                                    ledger.read_text(encoding="utf-8").splitlines()
                                    if ledger.exists() else [])
                            finally:
                                finished.set()

                        gate_patch = mock.patch.object(
                            chat2api, "record_usage", delayed_record)
                        gate_patch.start()
                        case.addCleanup(gate_patch.stop)
                        upstream, gateway = outer._gateway_with_stub()
                        observed["gateway"] = gateway
                        harness.install_tracked_server(
                            case, upstream, name="late-write-upstream",
                            require_drained=False)
                        if shutdown_failure or cancel_cleanup:
                            shutdown = gateway.shutdown
                            failed_once = False

                            def failing_shutdown():
                                nonlocal failed_once
                                shutdown()
                                if not failed_once:
                                    failed_once = True
                                    if cancel_cleanup:
                                        raise KeyboardInterrupt("synthetic cleanup cancellation")
                                    raise RuntimeError("synthetic shutdown failure")

                            gateway.shutdown = failing_shutdown
                        if close_failure:
                            close = gateway.server_close

                            def failing_close():
                                close()
                                raise RuntimeError("synthetic server-close failure")

                            gateway.server_close = failing_close
                        # setUp failure deliberately omits this registration:
                        # installing isolation must still protect its server.
                        if not setup_failure:
                            harness.install_tracked_server(
                                case, gateway, name="late-write-gateway",
                                drain_timeout=0.01)
                        response = requests.post(
                            f"http://127.0.0.1:{gateway.server_port}"
                            "/v1/chat/completions",
                            json={"model": "Late-Model", "messages": [
                                {"role": "user", "content": "hi"}],
                                  "max_tokens": 8, "stream": False},
                            headers={"Authorization": f"Bearer {LOCAL_KEY}"},
                            timeout=5)
                        outer.assertEqual(response.status_code, 200)
                        outer.assertTrue(entered.wait(5))
                        if setup_failure:
                            raise RuntimeError("synthetic setUp failure")

                    def runTest(case):
                        pass

                try:
                    case = CleanupCase()
                    try:
                        case.run(failure_result)
                    except KeyboardInterrupt:
                        if not cancel_cleanup:
                            raise
                        observed["cancelled"] = True
                        # The interrupted TestCase leaves remaining cleanups
                        # pending. Explicit resumption must still be safe.
                        case.doCleanups()
                    observed["directory_retained_after_cleanup"] = (
                        observed["temp_dir"].exists())
                    if new_suite:
                        _, second_ledger = harness.install_usage_isolation(
                            second, chat2api)
                        observed["second_ledger"] = second_ledger
                    release.set()
                    self.assertTrue(finished.wait(5))
                    self.assertEqual(observed["gateway"].drain(5), 0)
                    observed["sentinel_written"] = sentinel.exists()
                    observed["second_written"] = (
                        observed.get("second_ledger", Path(root) / "absent").exists())
                finally:
                    release.set()
                    if "gateway" in observed:
                        gateway = observed["gateway"]
                        gateway.shutdown()
                        gateway.close_client_sockets()
                        gateway.drain(5)
                        # Bypass only the synthetic close fault during final
                        # local resource release, never the TestCase cleanups.
                        harness.TrackedHTTPServer.server_close(gateway)
                    for function, args, kwargs in reversed(second.cleanups):
                        function(*args, **kwargs)
                deadline = time.monotonic() + 5
                while observed["temp_dir"].exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(observed["temp_dir"].exists())
        self.assertEqual(len(failure_result.errors) + len(failure_result.failures),
                         0 if cancel_cleanup else 1)
        if cancel_cleanup:
            self.assertTrue(observed.get("cancelled"))
        self.assertTrue(observed["directory_retained_after_cleanup"])
        self.assertFalse(observed["sentinel_written"])
        self.assertFalse(observed["second_written"])
        self.assertEqual(len(observed["private_rows"]), 1)
        self.assertEqual(json.loads(observed["private_rows"][0])["model"],
                         "Late-Model")

    def test_testcase_drain_timeout_keeps_late_record_private(self):
        self._failed_cleanup_with_late_record()

    def test_old_handler_cannot_write_the_next_suites_ledger(self):
        self._failed_cleanup_with_late_record(new_suite=True)

    def test_server_close_failure_does_not_release_late_write_isolation(self):
        self._failed_cleanup_with_late_record(close_failure=True)

    def test_setup_failure_before_server_cleanup_registration_is_safe(self):
        self._failed_cleanup_with_late_record(setup_failure=True)

    def test_shutdown_failure_does_not_release_late_write_isolation(self):
        self._failed_cleanup_with_late_record(shutdown_failure=True)

    def test_cancelled_cleanup_and_resumption_keep_the_late_write_private(self):
        self._failed_cleanup_with_late_record(cancel_cleanup=True)

    def test_unscoped_record_refuses_the_restored_production_default(self):
        with tempfile.TemporaryDirectory(prefix="chat2api-sink-guard-") as root:
            sentinel = Path(root) / "synthetic-production.jsonl"
            with mock.patch.object(chat2api, "USAGE_LOG", str(sentinel)):
                class IsolationCase(unittest.TestCase):
                    def runTest(case):
                        harness.install_usage_isolation(case, chat2api)

                result = unittest.TestResult()
                IsolationCase().run(result)
                self.assertTrue(result.wasSuccessful())
                self.assertEqual(chat2api.USAGE_LOG, str(sentinel))
                with self.assertRaisesRegex(AssertionError, "production path"):
                    chat2api.record_usage("m", False, time.time(), None, 1, 1)
                self.assertFalse(sentinel.exists())


if __name__ == "__main__":
    unittest.main()
