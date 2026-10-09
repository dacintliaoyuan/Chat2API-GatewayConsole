# -*- coding: utf-8 -*-
"""Regression tests for the bounded upstream-config refresh (W5-3).

The public Open WebUI /api/config snapshot used to be observed only at
startup or inside specific authentication flows, and ``config_summary``
carried no evidence of WHEN it was observed: a summary could quietly report
a version captured days earlier.

Contract pinned here (handoff Plan-Revision 5 W5-3):

  * the config state owner records the last SUCCESSFUL observation epoch,
    the last attempt epoch (success or failure), the in-flight flag and the
    last failure's kind (timeout / network_error / upstream_auth_rejected /
    upstream_5xx / unexpected_status / invalid_json / invalid_config_shape);
    a failed refresh never downgrades data or the success epoch;
  * the refresh rides the EXISTING auth-watch maintenance loop: TTL-gated,
    single-flight, network timeout bounded, failure backoff. No new timer,
    no resident thread, and no state lock is held across the network call;
  * config_summary / health_snapshot stay read-only memory reads: /health
    or a UI poll never issues an upstream request. Freshness fields are
    additive and carry the REAL observation epoch (time.time(), epoch
    seconds), never the moment the summary happened to be read;
  * a refresh failure records config evidence only: it never touches the
    auth state, never restarts anything and never erases a valid
    credential; a successful fetch proves nothing about authentication;
  * an absent enable_api_keys stays None/unknown, never False;
  * two instances keep independent config caches and freshness.

The stub upstream is a loopback server with a mutable /api/config payload,
a request counter and an optional blocking delay. Timing is controlled by
patching the module's TTL/backoff/timeout constants and (where a clock
value itself is asserted) chat2api.time.time. All paths land in private
temp directories; no real upstream is ever contacted.
"""

import importlib.util
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

HERE = Path(__file__).resolve().parents[1]


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, HERE / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


chat2api = _load("chat2api_config_refresh", "chat2api/chat2api.py")

# setUp 把这些常量逐用例补丁成确定性值；真实默认值在导入时捕获，供
# 「失败退避不得快于健康刷新」的不变量测试使用（测试环境未设覆盖环境变量）。
_REAL_TTL_S = chat2api.CONFIG_REFRESH_TTL_S
_REAL_BACKOFF_S = chat2api.CONFIG_REFRESH_BACKOFF_S


def _config_payload(version="0.9.2", api_keys=True, providers=None):
    return {"version": version,
            "features": {"enable_api_keys": api_keys},
            "oauth": {"providers": providers or {}}}


class ConfigStubHandler(BaseHTTPRequestHandler):
    """Fake Open WebUI /api/config driven by the server's ``config_state``.

    config_state keys: payloads (one-shot entries consumed before the sticky
    value; None means a 503, 401 means a 401 status, a str is a non-JSON
    body), sticky (the payload answered once the list is exhausted),
    delay_s (blocking-upstream cases), requests (hit counter).
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/config":
            self._serve_config()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_config(self):
        holder = self.server.config_state
        holder["requests"] += 1
        if holder["delay_s"]:
            time.sleep(holder["delay_s"])
        payload = holder["payloads"].pop(0) if holder["payloads"] \
            else holder["sticky"]
        if payload is None:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if payload == 401:
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if isinstance(payload, str):  # a non-JSON body for bad-JSON cases
            body = payload.encode("utf-8")
        else:
            body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _make_stub(version="0.9.2", api_keys=True):
    server = ThreadingHTTPServer(("127.0.0.1", 0), ConfigStubHandler)
    server.daemon_threads = True
    server.config_state = {
        "payloads": [],
        "sticky": _config_payload(version, api_keys),
        "delay_s": 0.0,
        "requests": 0,
    }
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class ConfigRefreshTestBase(unittest.TestCase):
    """Shared fixture: loopback stub, private temp dir, fresh Proxy."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.stub = _make_stub()
        self.addCleanup(self.stub.server_close)
        self.addCleanup(self.stub.shutdown)
        self.state = self.stub.config_state
        args = SimpleNamespace(
            base_url=f"http://127.0.0.1:{self.stub.server_port}",
            token="stub-token",
            profile="profile",
            login_timeout=1,
            use_api_key=False,
            no_browser=True,
        )
        self.proxy = chat2api.Proxy({"token": "stub-token"}, args)
        # Timing-controlled refresh parameters for the deterministic cases.
        for patch in (
            mock.patch.object(chat2api, "CONFIG_REFRESH_TTL_S", 900.0),
            mock.patch.object(chat2api, "CONFIG_REFRESH_BACKOFF_S", 900.0),
            mock.patch.object(chat2api, "CONFIG_REFRESH_TIMEOUT_S", 5.0),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    @property
    def config_requests(self):
        return self.state["requests"]

    def wait_for_config_requests(self, count, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state["requests"] >= count:
                return
            time.sleep(0.05)
        self.fail(f"stub received {self.state['requests']} config requests, "
                  f"expected {count}")

    def wait_for_state(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(f"state not reached within {timeout}s")


class TestConfigFreshnessSummary(ConfigRefreshTestBase):
    """config_summary / health_snapshot stay memory-only and gain evidence."""

    def test_unobserved_config_stays_unknown_never_false(self):
        summary = self.proxy.config_summary()
        self.assertIsNone(summary["name"])
        self.assertIsNone(summary["version"])
        self.assertIsNone(summary["enable_api_keys"])  # unknown, NOT False
        self.assertEqual(summary["oauth_providers"], [])
        # Additive freshness: nothing observed yet.
        self.assertIsNone(summary["config_checked_epoch"])
        self.assertIsNone(summary["config_attempt_epoch"])
        self.assertFalse(summary["config_refreshing"])
        self.assertIsNone(summary["config_last_failure"])

    def test_success_records_the_real_observation_epoch(self):
        with mock.patch.object(chat2api.time, "time", return_value=1000.0):
            config = self.proxy.probe_config()
        self.assertEqual(config["version"], "0.9.2")
        # The success epoch is the OBSERVATION epoch, never the read moment:
        # reading the summary at a later scripted clock still reports 1000.
        with mock.patch.object(chat2api.time, "time", return_value=2000.0):
            summary = self.proxy.config_summary()
        self.assertEqual(summary["config_checked_epoch"], 1000.0)
        self.assertEqual(summary["config_attempt_epoch"], 1000.0)
        self.assertFalse(summary["config_refreshing"])
        self.assertIsNone(summary["config_last_failure"])

    def test_health_snapshot_gains_additive_freshness_without_requests(self):
        before = self.config_requests
        upstream = self.proxy.health_snapshot()["upstream"]
        # Established keys keep their meaning; the freshness block is additive.
        for key in ("name", "version", "enable_api_keys", "oauth_providers"):
            self.assertIn(key, upstream)
        for key in ("config_checked_epoch", "config_attempt_epoch",
                    "config_refreshing", "config_last_failure"):
            self.assertIn(key, upstream)
        self.assertEqual(self.config_requests, before)  # memory read only

    def test_absent_feature_fields_stay_unknown(self):
        self.state["payloads"] = [{"version": "0.11.4"}]  # no features/oauth
        self.proxy.probe_config()
        summary = self.proxy.config_summary()
        self.assertEqual(summary["version"], "0.11.4")
        self.assertIsNone(summary["enable_api_keys"])
        self.assertEqual(summary["oauth_providers"], [])


class TestMaintenanceRefresh(ConfigRefreshTestBase):
    """TTL gate, single flight and lock discipline (maintenance path)."""

    def test_refresh_never_observed_fires_immediately(self):
        with mock.patch.object(chat2api, "CONFIG_REFRESH_TTL_S", 0.0):
            self.proxy._maybe_refresh_config()
        self.assertEqual(self.config_requests, 1)
        summary = self.proxy.config_summary()
        self.assertEqual(summary["version"], "0.9.2")
        self.assertIsNotNone(summary["config_checked_epoch"])

    def test_ttl_within_window_does_not_request(self):
        # The startup probe counts as a fresh observation: TTL-gated rounds
        # inside the window must not touch the upstream at all.
        self.proxy.probe_config()
        self.assertEqual(self.config_requests, 1)
        self.proxy._maybe_refresh_config()
        self.proxy._maybe_refresh_config()
        self.assertEqual(self.config_requests, 1)

    def test_ttl_gate_uses_a_controllable_clock(self):
        # Seed a successful observation at a KNOWN epoch (scripted clock),
        # then drive the gate: within the TTL nothing is requested; past the
        # TTL exactly one refresh happens and the success epoch becomes the
        # observation time under that clock, never the summary read moment.
        with mock.patch.object(chat2api.time, "time", return_value=1000.0):
            self.proxy.probe_config()
        self.assertEqual(self.config_requests, 1)
        with self.subTest("within ttl"):
            with mock.patch.object(chat2api, "CONFIG_REFRESH_TTL_S", 900.0), \
                    mock.patch.object(chat2api.time, "time",
                                      return_value=1005.0):
                self.proxy._maybe_refresh_config()
            self.assertEqual(self.config_requests, 1)
            self.assertEqual(
                self.proxy.config_summary()["config_checked_epoch"], 1000.0)
        with self.subTest("past ttl"):
            with mock.patch.object(chat2api, "CONFIG_REFRESH_TTL_S", 900.0), \
                    mock.patch.object(chat2api.time, "time",
                                      return_value=2000.0):
                self.proxy._maybe_refresh_config()
            self.assertEqual(self.config_requests, 2)
            self.assertEqual(
                self.proxy.config_summary()["config_checked_epoch"], 2000.0)

    def test_ttl_expiry_refreshes_once_then_restarts_the_window(self):
        with mock.patch.object(chat2api, "CONFIG_REFRESH_TTL_S", 0.05):
            self.proxy.probe_config()
            time.sleep(0.15)  # TTL expired
            self.proxy._maybe_refresh_config()
            self.assertEqual(self.config_requests, 2)
            self.proxy._maybe_refresh_config()  # fresh again: no request
            self.assertEqual(self.config_requests, 2)

    def test_concurrent_refreshers_hit_the_upstream_once(self):
        # The upstream blocks long enough for all racing rounds to overlap
        # inside the network call; only one of them may fetch.
        self.state["delay_s"] = 0.6
        with mock.patch.object(chat2api, "CONFIG_REFRESH_TTL_S", 0.0):
            threads = [threading.Thread(
                target=self.proxy._maybe_refresh_config) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
                self.assertFalse(thread.is_alive())
        self.assertEqual(self.config_requests, 1)
        summary = self.proxy.config_summary()
        self.assertFalse(summary["config_refreshing"])
        self.assertEqual(summary["version"], "0.9.2")

    def test_state_lock_is_not_held_across_the_network_call(self):
        # While a slow refresh is in flight, readers must still be served
        # from memory immediately (the freshness flag is visible in-flight).
        self.state["delay_s"] = 1.0
        worker = threading.Thread(target=self.proxy._maybe_refresh_config)
        worker.start()
        try:
            self.wait_for_state(lambda: self.proxy.config_summary()
                                ["config_refreshing"], timeout=5)
            started = time.monotonic()
            summary = self.proxy.config_summary()
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(summary["config_refreshing"])
        finally:
            worker.join(5)
        self.assertFalse(self.proxy.config_summary()["config_refreshing"])
        self.assertEqual(self.config_requests, 1)

    def test_default_backoff_never_polls_faster_than_a_healthy_refresh(self):
        # Regression (independent-review finding): with the old default
        # BACKOFF == the 300 s watch interval, the failure gate
        # (`now - attempt < BACKOFF`) was already false at the very next
        # round, so a failing upstream was probed EVERY round - 3x the 15
        # minute success cadence the backoff comment promised to avoid. The
        # default must be at least the TTL (a failing upstream is retried no
        # faster than a healthy one refreshes) and at least the watch
        # interval (never degrade into probing every round).
        self.assertGreaterEqual(_REAL_BACKOFF_S, _REAL_TTL_S)
        self.assertGreaterEqual(_REAL_BACKOFF_S,
                                chat2api.AUTH_WATCH_INTERVAL_S)

    def test_probe_config_joins_the_single_flight(self):
        # probe_config (startup / post-login entry) participates in the same
        # flight flag: while a maintenance fetch is in flight it must not
        # issue a second concurrent GET and answers from the last observed
        # snapshot (None before anything was observed).
        self.state["delay_s"] = 0.8
        worker = threading.Thread(target=self.proxy._maybe_refresh_config)
        worker.start()
        try:
            self.wait_for_state(lambda: self.proxy.config_summary()
                                ["config_refreshing"], timeout=5)
            self.assertIsNone(self.proxy.probe_config())
        finally:
            worker.join(8)
        self.assertEqual(self.config_requests, 1)
        self.assertFalse(self.proxy.config_summary()["config_refreshing"])
        # After the flight settles, the explicit probe fetches normally.
        self.proxy.probe_config()
        self.assertEqual(self.config_requests, 2)

    def test_maintenance_gate_skips_while_probe_in_flight(self):
        # The reverse ordering: while probe_config's fetch is in flight, a
        # TTL-gated maintenance round must stay silent - one network fetch
        # at a time across BOTH entry points.
        self.state["delay_s"] = 0.8
        probe_thread = threading.Thread(target=self.proxy.probe_config)
        probe_thread.start()
        try:
            self.wait_for_state(lambda: self.proxy.config_summary()
                                ["config_refreshing"], timeout=5)
            self.proxy._maybe_refresh_config()
        finally:
            probe_thread.join(8)
        self.assertEqual(self.config_requests, 1)
        self.assertFalse(self.proxy.config_summary()["config_refreshing"])


class TestRefreshFailureEvidence(ConfigRefreshTestBase):
    """Failures keep the previous valid value, name their kind, back off."""

    def test_failure_keeps_config_and_success_epoch_and_backs_off(self):
        self.proxy.probe_config()
        checked_before = self.proxy.config_summary()["config_checked_epoch"]
        self.state["payloads"] = [None]  # one 503, then sticky 503
        with mock.patch.object(chat2api, "CONFIG_REFRESH_TTL_S", 0.0), \
                mock.patch.object(chat2api, "CONFIG_REFRESH_BACKOFF_S", 3600.0):
            self.proxy._maybe_refresh_config()
            self.assertEqual(self.config_requests, 2)
            summary = self.proxy.config_summary()
            self.assertEqual(summary["version"], "0.9.2")  # data kept
            self.assertEqual(summary["config_checked_epoch"], checked_before)
            failure = summary["config_last_failure"]
            self.assertEqual(failure["reason"], "upstream_5xx")
            self.assertEqual(failure["upstream_status"], 503)
            self.assertGreaterEqual(failure["at_epoch"], checked_before)
            # Failure backoff: the next gated rounds stay silent.
            self.proxy._maybe_refresh_config()
            time.sleep(0.15)
            self.assertEqual(self.config_requests, 2)

    def test_failure_kinds_are_recorded(self):
        # Payload encodings: None -> 503 status, 401 -> 401 status, a str is
        # a non-JSON body, a list is a JSON 200 that is not an object.
        cases = [
            (401, "upstream_auth_rejected"),
            (None, "upstream_5xx"),
            ("not-json", "invalid_json"),
            ([1, 2], "invalid_config_shape"),
        ]
        for payload, reason in cases:
            with self.subTest(payload=payload):
                self.state["payloads"] = [payload]
                with mock.patch.object(chat2api, "CONFIG_REFRESH_TTL_S", 0.0), \
                        mock.patch.object(chat2api, "CONFIG_REFRESH_BACKOFF_S",
                                          0.0):
                    self.proxy._maybe_refresh_config()
                failure = self.proxy.config_summary()["config_last_failure"]
                self.assertEqual(failure["reason"], reason)

    def test_timeout_records_a_timeout_evidence(self):
        with mock.patch.object(chat2api.requests, "get",
                               side_effect=requests.Timeout("slow")), \
                mock.patch.object(chat2api, "CONFIG_REFRESH_TTL_S", 0.0):
            self.proxy._maybe_refresh_config()
        failure = self.proxy.config_summary()["config_last_failure"]
        self.assertEqual(failure["reason"], "timeout")
        self.assertIsNone(failure["upstream_status"])

    def test_network_error_records_a_network_evidence(self):
        with mock.patch.object(chat2api.requests, "get",
                               side_effect=requests.ConnectionError("dead")), \
                mock.patch.object(chat2api, "CONFIG_REFRESH_TTL_S", 0.0):
            self.proxy._maybe_refresh_config()
        failure = self.proxy.config_summary()["config_last_failure"]
        self.assertEqual(failure["reason"], "network_error")

    def test_backoff_then_recovery_updates_version_and_clears_failure(self):
        with mock.patch.object(chat2api, "CONFIG_REFRESH_TTL_S", 0.0), \
                mock.patch.object(chat2api, "CONFIG_REFRESH_BACKOFF_S", 3600.0):
            self.proxy._maybe_refresh_config()  # first success (1 request)
            self.assertEqual(self.config_requests, 1)
            self.state["sticky"] = None  # upstream starts failing
            self.proxy._maybe_refresh_config()  # TTL expired -> try, fail
            self.assertEqual(self.config_requests, 2)
            failure = self.proxy.config_summary()["config_last_failure"]
            self.assertEqual(failure["reason"], "upstream_5xx")
            self.assertEqual(self.proxy.config_summary()["version"], "0.9.2")
            # Still backed off: silent gated rounds.
            self.proxy._maybe_refresh_config()
            time.sleep(0.15)
            self.assertEqual(self.config_requests, 2)
            # Backoff disabled and the upstream recovered: the next gated
            # round updates the version to 0.11.4 and clears the failure.
            self.state["sticky"] = _config_payload("0.11.4")
            with mock.patch.object(chat2api, "CONFIG_REFRESH_BACKOFF_S", 0.0):
                time.sleep(0.05)
                self.proxy._maybe_refresh_config()
            self.assertEqual(self.config_requests, 3)
            summary = self.proxy.config_summary()
            self.assertEqual(summary["version"], "0.11.4")
            self.assertIsNone(summary["config_last_failure"])
            self.assertEqual(summary["config_checked_epoch"],
                             summary["config_attempt_epoch"])

    def test_failure_never_touches_auth_state(self):
        with mock.patch.object(chat2api, "CONFIG_REFRESH_TTL_S", 0.0):
            self.state["sticky"] = None
            self.proxy._maybe_refresh_config()
        self.assertEqual(self.proxy.auth_status()["state"], "valid")
        self.assertIsNone(self.proxy.auth_status()["error"])


class TestRefreshLifecycle(ConfigRefreshTestBase):
    """The refresh rides the existing maintenance loop and ends with it."""

    def test_maintenance_loop_refreshes_after_ttl_and_stops_on_shutdown(self):
        self.state["sticky"] = _config_payload("0.9.2")
        with mock.patch.object(chat2api, "AUTH_WATCH_INTERVAL_S", 0.05), \
                mock.patch.object(chat2api, "CONFIG_REFRESH_TTL_S", 0.1), \
                mock.patch.object(chat2api, "CONFIG_REFRESH_BACKOFF_S", 0.0), \
                mock.patch.object(chat2api, "classify_credential",
                                  return_value=("valid", None, 200)):
            self.proxy.begin_auth_watch()
            # First round: never observed -> refresh. TTL 0.1s expires before
            # a later round -> a second refresh rides the same loop.
            self.wait_for_config_requests(2, timeout=8)
            before = self.config_requests
            self.proxy.shutdown()
            watch_thread = self.proxy._watch_thread
            self.assertIsNotNone(watch_thread)
            watch_thread.join(timeout=5)
            self.assertFalse(watch_thread.is_alive())
            time.sleep(0.3)  # would-be rounds after shutdown stay silent
            self.assertEqual(self.config_requests, before)

    def test_shutdown_stops_a_pending_gated_round(self):
        # A gated round that never started must not fetch after shutdown.
        self.proxy.shutdown()
        with mock.patch.object(chat2api, "CONFIG_REFRESH_TTL_S", 0.0):
            self.proxy._maybe_refresh_config()
        self.assertEqual(self.config_requests, 0)


class TestTwoInstancesIndependent(ConfigRefreshTestBase):
    """Private and dedicated instances keep separate config state."""

    def test_instances_refresh_their_own_upstream_only(self):
        stub_b = _make_stub(version="0.11.4")
        self.addCleanup(stub_b.server_close)
        self.addCleanup(stub_b.shutdown)
        args_b = SimpleNamespace(
            base_url=f"http://127.0.0.1:{stub_b.server_port}",
            token="stub-token", profile="profile", login_timeout=1,
            use_api_key=False, no_browser=True)
        proxy_b = chat2api.Proxy({"token": "stub-token"}, args_b)

        with mock.patch.object(chat2api, "CONFIG_REFRESH_TTL_S", 0.0):
            self.proxy._maybe_refresh_config()
            proxy_b._maybe_refresh_config()
        self.assertEqual(self.config_requests, 1)
        self.assertEqual(stub_b.config_state["requests"], 1)
        summary_a = self.proxy.config_summary()
        summary_b = proxy_b.config_summary()
        self.assertEqual(summary_a["version"], "0.9.2")
        self.assertEqual(summary_b["version"], "0.11.4")
        # One instance's failure evidence never reaches the other instance.
        self.assertIsNone(summary_b["config_last_failure"])
        self.assertIsNone(summary_a["config_last_failure"])


if __name__ == "__main__":
    unittest.main()
