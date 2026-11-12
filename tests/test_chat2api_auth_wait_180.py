# -*- coding: utf-8 -*-
"""A2-1—A2-5 / D18-5: the private and share instances wait up to 180 seconds.

Contract:

* the private direct instance and the dedicated share backend both default to
  180 seconds (D18-5); an explicit CLI or env value overrides the role default
  and is clamped to 1..180, so neither a leaked local env nor an operator value
  can disable the budget or exceed the hard ceiling; the generic default stays 30;
* verification and recovery share ONE cumulative request budget, so a recovery
  that used more than the old 30-second budget can still continue the same
  logical request inside 180;
* only an explicit ``rejected``/``needs_login`` pre-output verdict joins the
  shared recovery cycle; ``valid``/``unknown`` keep the original error path;
* the bounded pre-output check happens at most once per logical request;
* a client cancel, a spent request deadline or a service stop never sends a late
  upstream call;
* content that was already delivered is never replayed.

The real Handler and Proxy run; only the final upstream, the credential probe and
the final browser login are replaced. The private ledger, handler drain and
temporary coordination file come from ``_chat2api_http_harness.py``.
"""

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_chat2api_auth_wait import (  # noqa: E402
    AuthWaitTestBase, StubUpstream, chat2api)


class LocalAuthWaitPolicyTests(unittest.TestCase):
    def test_private_and_share_default_to_180_and_generic_stays_30(self):
        self.assertEqual(chat2api.resolve_auth_wait_budget(None, False), 180.0)
        self.assertEqual(chat2api.resolve_auth_wait_budget(None, True), 180.0)
        self.assertEqual(chat2api.resolve_auth_wait_budget(None, None), 30.0)

    def test_explicit_value_is_capped_at_180(self):
        self.assertEqual(chat2api.resolve_auth_wait_budget(90, False), 90.0)
        self.assertEqual(chat2api.resolve_auth_wait_budget(90, True), 90.0)
        self.assertEqual(chat2api.resolve_auth_wait_budget(1000, False), 180.0)
        self.assertEqual(chat2api.resolve_auth_wait_budget(1000, True), 180.0)
        self.assertEqual(chat2api.resolve_auth_wait_budget(0, False), 1.0)
        self.assertEqual(chat2api.resolve_auth_wait_budget(0, True), 1.0)

    def test_share_role_reads_an_explicit_env_value(self):
        # D18-5: the share backend now defaults to 180 and honours the
        # existing env name, but never above the hard ceiling.
        with mock.patch.dict(
                os.environ, {"CHAT2API_AUTH_WAIT_BUDGET_S": "120"}):
            self.assertEqual(chat2api.resolve_auth_wait_budget(None, False),
                             120.0)
            self.assertEqual(chat2api.resolve_auth_wait_budget(None, True),
                             120.0)
            self.assertEqual(chat2api.resolve_auth_wait_budget(None, None),
                             120.0)

    def test_env_value_is_clamped_to_1_and_180(self):
        with mock.patch.dict(
                os.environ, {"CHAT2API_AUTH_WAIT_BUDGET_S": "0"}):
            self.assertEqual(chat2api.resolve_auth_wait_budget(None, False), 1.0)
            self.assertEqual(chat2api.resolve_auth_wait_budget(None, True), 1.0)
        with mock.patch.dict(
                os.environ, {"CHAT2API_AUTH_WAIT_BUDGET_S": "900"}):
            self.assertEqual(chat2api.resolve_auth_wait_budget(None, False), 180.0)
            self.assertEqual(chat2api.resolve_auth_wait_budget(None, True), 180.0)

    def test_invalid_env_falls_back_to_the_role_default(self):
        with mock.patch.dict(
                os.environ, {"CHAT2API_AUTH_WAIT_BUDGET_S": "not-a-number"}):
            self.assertEqual(chat2api.resolve_auth_wait_budget(None, False),
                             180.0)
            self.assertEqual(chat2api.resolve_auth_wait_budget(None, True),
                             180.0)
            self.assertEqual(chat2api.resolve_auth_wait_budget(None, None),
                             30.0)

    def test_import_default_stays_generic_30(self):
        # A2-1: importing the module without main() keeps the generic 30s;
        # only the private startup selects 180.
        self.assertEqual(chat2api._AuthWaitBudget().limit, 30.0)
        self.assertEqual(chat2api._AuthWaitBudget(1000).limit, 180.0)


class ShareRoleSourceTests(unittest.TestCase):
    """D18-5: the share role is independent of the removed reasoning filter.

    main() passes ``"share-backend" in profile`` as the default; the explicit
    CHAT2API_SHARE_BACKEND marker and the attribution-key env stay
    authoritative, and no reasoning-filter switch participates in the role.
    """

    def test_explicit_markers_stay_authoritative(self):
        with mock.patch.dict(
                os.environ, {"CHAT2API_SHARE_BACKEND": "1"}, clear=True):
            self.assertTrue(chat2api._resolve_share_backend_role(False))
            self.assertTrue(chat2api._resolve_share_backend_role(None))
        with mock.patch.dict(
                os.environ,
                {"CHAT2API_SHARE_ATTRIBUTION_KEY_FILE": "/tmp/key"},
                clear=True):
            self.assertTrue(chat2api._resolve_share_backend_role(False))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(chat2api._resolve_share_backend_role(False))
            self.assertTrue(chat2api._resolve_share_backend_role(True))
            self.assertIsNone(chat2api._resolve_share_backend_role(None))

    def test_the_production_share_profile_is_the_default_share_role(self):
        # The dedicated 8110 launcher's own profile is the share-backend
        # default main() resolves; the private 8000 profile does not match.
        import console_share_production
        profile = console_share_production._ProductionBackendOps()._paths()[
            "profile"]
        self.assertIn("share-backend", profile)
        self.assertTrue(chat2api._resolve_share_backend_role(
            "share-backend" in profile))
        self.assertFalse(chat2api._resolve_share_backend_role(
            "share-backend" in "profile"))


class Cumulative180BudgetTests(unittest.TestCase):
    def test_two_segments_may_exceed_the_old_30_but_not_180(self):
        clock = [1000.0]
        budget = chat2api._AuthWaitBudget(180.0, now=lambda: clock[0])
        budget.begin_wait()
        clock[0] += 100.0
        budget.end_wait()
        self.assertAlmostEqual(budget.remaining, 80.0, places=6)
        budget.begin_wait()
        clock[0] += 60.0
        budget.end_wait()
        self.assertAlmostEqual(budget.used, 160.0, places=6)
        self.assertAlmostEqual(budget.remaining, 20.0, places=6)
        budget.begin_wait()
        clock[0] += 20.0
        budget.end_wait()
        self.assertAlmostEqual(budget.remaining, 0.0, places=6)
        self.assertEqual(budget.deadline, 1180.0)

    def test_verification_and_recovery_share_the_same_budget(self):
        clock = [1000.0]
        budget = chat2api._AuthWaitBudget(180.0, now=lambda: clock[0])
        fake_time = SimpleNamespace(
            monotonic=lambda: clock[0], time=lambda: clock[0],
            sleep=time.sleep)

        def classify(*args, **kwargs):
            clock[0] += 100.0
            return "rejected", "upstream_rejected", 401

        def refresh(*args, **kwargs):
            budget.begin_wait()
            clock[0] += 60.0
            budget.end_wait()
            return True

        proxy = SimpleNamespace(
            _shutdown_event=threading.Event(),
            _auth_condition=threading.Condition(),
            _credential_generation=1,
            _auth_evidence={},
            _auth_state="valid",
            bearer=lambda: "token",
            base_url="http://127.0.0.1:1",
        )
        proxy._record_auth_evidence = lambda *args: None
        proxy.refresh = refresh

        with (
            mock.patch.object(chat2api, "time", fake_time),
            mock.patch.object(chat2api, "classify_credential",
                              side_effect=classify),
            mock.patch.object(chat2api.Proxy, "refresh",
                              side_effect=refresh),
        ):
            outcome = chat2api._pre_output_verify(
                proxy, budget, None, None, "test", "req")

        self.assertEqual(outcome, "retry")
        self.assertAlmostEqual(budget.used, 160.0, places=6)
        self.assertAlmostEqual(budget.remaining, 20.0, places=6)

    def test_a_second_call_after_180_is_a_timeout_not_a_new_probe(self):
        clock = [1000.0]
        budget = chat2api._AuthWaitBudget(180.0, now=lambda: clock[0])
        budget.begin_wait()
        clock[0] += 180.0
        budget.end_wait()
        with (
            mock.patch.object(chat2api, "classify_credential") as probe,
            mock.patch.object(chat2api.Proxy, "refresh") as refresh,
        ):
            outcome = chat2api._pre_output_verify(
                SimpleNamespace(
                    _shutdown_event=threading.Event(),
                    _auth_condition=threading.Condition(),
                    _credential_generation=1,
                    _auth_evidence={},
                    _auth_state="valid",
                    bearer=lambda: "token",
                    base_url="http://127.0.0.1:1",
                ),
                budget, None, None, "test", "req")
        self.assertEqual(outcome, "auth_timeout")
        probe.assert_not_called()
        refresh.assert_not_called()

    def test_one_bounded_verification_per_logical_request(self):
        clock = [1000.0]
        budget = chat2api._AuthWaitBudget(180.0, now=lambda: clock[0])
        self.assertTrue(chat2api._claim_pre_output_verification(budget))
        self.assertFalse(chat2api._claim_pre_output_verification(budget))


class PreOutputVerificationTests(AuthWaitTestBase):
    def _proxy(self):
        return self.proxy

    def _budget(self, seconds=2.0):
        return chat2api._AuthWaitBudget(seconds)

    def test_rejected_probe_joins_recovery_and_continues_the_request(self):
        StubUpstream.statuses = [200, 200]
        StubUpstream.empty_post_indices = frozenset({0})
        StubUpstream.sse = True
        login_calls = []

        def fake_login(*args, **kwargs):
            login_calls.append(time.monotonic())
            return "fresh-token"

        with (
            self._wait_patches(budget=2.0, recheck=0.02, deadline=5.0),
            mock.patch.object(chat2api, "classify_credential",
                              return_value=("rejected", "upstream_rejected",
                                            401)),
            mock.patch.object(chat2api, "browser_login",
                              side_effect=fake_login),
            mock.patch.object(chat2api.TokenStore, "save"),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 5),
            mock.patch.object(chat2api, "_retry_delay_seconds",
                              return_value=0.0),
        ):
            response = self._stream_chat_request(timeout=10)
            body = self._read_stream(response)
            response.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(login_calls), 1)
        self.assertEqual(StubUpstream.posts, 2)
        self.assertIn("data: [DONE]", body)

    def test_non_401_pre_output_wait_releases_the_reader_slot(self):
        # Review medium: a non-401 setup failure raises inside proxy.call while
        # the request already owns the streaming-reader slot. The pre-output
        # check waits for recovery; that wait must not hold the slot.
        StubUpstream.statuses = [200]
        StubUpstream.sse = True
        reader_slots = threading.BoundedSemaphore(1)
        observed = []
        real_call = chat2api.Proxy.call
        attempts = [0]

        def flaky_call(proxy_self, *args, **kwargs):
            attempts[0] += 1
            if attempts[0] == 1:
                raise requests.ConnectionError("setup reset")
            return real_call(proxy_self, *args, **kwargs)

        def fake_login(*args, **kwargs):
            observed.append(reader_slots._value)
            return "fresh-token"

        with (
            self._wait_patches(budget=2.0, recheck=0.02, deadline=5.0),
            mock.patch.object(chat2api, "_reader_slots", reader_slots),
            mock.patch.object(chat2api.Proxy, "call", new=flaky_call),
            mock.patch.object(chat2api, "classify_credential",
                              return_value=("rejected", "upstream_rejected",
                                            401)),
            mock.patch.object(chat2api, "browser_login",
                              side_effect=fake_login),
            mock.patch.object(chat2api.TokenStore, "save"),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 5),
            mock.patch.object(chat2api, "_retry_delay_seconds",
                              return_value=0.0),
        ):
            response = self._stream_chat_request(timeout=10)
            body = self._read_stream(response)
            response.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(observed, [1],
                         "the pre-output wait held the only reader slot")
        self.assertEqual(reader_slots._value, 1,
                         "the reader slot was not returned after the request")
        self.assertEqual(StubUpstream.posts, 1)
        self.assertIn("data: [DONE]", body)

    def test_recovery_longer_than_the_old_30_seconds_continues_the_request(self):
        # Review medium: a controlled >30-second positive control on the
        # integrated path. The recovery consumes 45 fake seconds - more than
        # the old 30-second default - and the SAME logical request must
        # still continue to a 200 inside the 180-second budget.
        StubUpstream.statuses = [200, 200]
        StubUpstream.empty_post_indices = frozenset({0})
        StubUpstream.sse = True
        clock = [1000.0]
        login_calls = []

        def fake_login(*args, **kwargs):
            login_calls.append(clock[0])
            clock[0] += 45.0
            return "fresh-token"

        def fake_sleep(seconds):
            clock[0] += max(0.0, float(seconds))

        fake_time = SimpleNamespace(
            monotonic=lambda: clock[0],
            time=lambda: clock[0],
            sleep=fake_sleep,
        )

        with (
            self._wait_patches(budget=180.0, recheck=0.02, deadline=360.0),
            mock.patch.object(chat2api, "time", fake_time),
            mock.patch.object(chat2api, "classify_credential",
                              return_value=("rejected", "upstream_rejected",
                                            401)),
            mock.patch.object(chat2api, "browser_login",
                              side_effect=fake_login),
            mock.patch.object(chat2api.TokenStore, "save"),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 5),
            mock.patch.object(chat2api, "_retry_delay_seconds",
                              return_value=0.0),
        ):
            response = self._stream_chat_request(timeout=10)
            body = self._read_stream(response)
            response.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(login_calls), 1)
        self.assertEqual(StubUpstream.posts, 2,
                         "the request did not continue after recovery")
        self.assertIn("data: [DONE]", body)
        self.assertGreaterEqual(clock[0] - 1000.0, 45.0,
                                "the recovery did not exceed the old 30s")

    def test_valid_probe_keeps_the_original_error_and_never_logs_in(self):
        StubUpstream.statuses = [200, 200]
        StubUpstream.empty_post_indices = frozenset({0, 1})
        StubUpstream.sse = True
        probes = []

        def classify(*args, **kwargs):
            probes.append(time.monotonic())
            return "valid", None, 200

        with (
            self._wait_patches(budget=2.0, recheck=0.02, deadline=5.0),
            mock.patch.object(chat2api, "classify_credential",
                              side_effect=classify),
            mock.patch.object(chat2api, "browser_login") as login,
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 1),
            mock.patch.object(chat2api, "_retry_delay_seconds",
                              return_value=0.0),
        ):
            response = self._stream_chat_request(timeout=10)
            self._read_stream(response)
            response.close()

        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(probes), 1,
                         "the bounded pre-output check must run once")
        self.assertEqual(StubUpstream.posts, 2)
        login.assert_not_called()

    def test_unknown_probe_keeps_the_original_error_and_never_logs_in(self):
        StubUpstream.statuses = [200, 200]
        StubUpstream.empty_post_indices = frozenset({0, 1})
        StubUpstream.sse = True
        probes = []

        def classify(*args, **kwargs):
            probes.append(time.monotonic())
            return "unknown", "network_error", None

        with (
            self._wait_patches(budget=2.0, recheck=0.02, deadline=5.0),
            mock.patch.object(chat2api, "classify_credential",
                              side_effect=classify),
            mock.patch.object(chat2api, "browser_login") as login,
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 1),
            mock.patch.object(chat2api, "_retry_delay_seconds",
                              return_value=0.0),
        ):
            response = self._stream_chat_request(timeout=10)
            self._read_stream(response)
            response.close()

        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(probes), 1)
        self.assertEqual(StubUpstream.posts, 2)
        login.assert_not_called()

    def test_visible_content_is_never_replayed_after_an_eof(self):
        StubUpstream.statuses = [200]
        StubUpstream.truncate_after_content = True
        with self._wait_patches(budget=2.0, recheck=0.02, deadline=5.0):
            response = self._stream_chat_request(timeout=10)
            body = self._read_stream(response)
            response.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(StubUpstream.posts, 1)
        self.assertEqual(StubUpstream.gets, 0,
                         "content was already visible; no probe may run")
        self.assertEqual(body.count('"content":"hi"'), 1,
                         "visible content must be delivered exactly once")
        self.assertIn("upstream_eof_without_done", body)

    def test_spent_budget_maps_to_auth_wait_timeout_without_probe(self):
        budget = self._budget(0.01)
        budget.begin_wait()
        time.sleep(0.02)
        budget.end_wait()
        with (
            mock.patch.object(chat2api, "classify_credential") as probe,
            mock.patch.object(chat2api, "browser_login") as login,
        ):
            outcome = chat2api._pre_output_verify(
                self.proxy, budget, None, None, "test", "req")
        self.assertEqual(outcome, "auth_timeout")
        probe.assert_not_called()
        login.assert_not_called()

    def test_cancel_after_claim_never_starts_a_probe(self):
        budget = self._budget(5.0)
        with (
            mock.patch.object(chat2api, "classify_credential") as probe,
            mock.patch.object(chat2api, "browser_login") as login,
        ):
            outcome = chat2api._pre_output_verify(
                self.proxy, budget, None, lambda: True, "test", "req")
        self.assertEqual(outcome, "cancelled")
        probe.assert_not_called()
        login.assert_not_called()

    def test_cancel_during_probe_never_joins_recovery(self):
        budget = self._budget(5.0)
        cancelled = [False]

        def classify(*args, **kwargs):
            cancelled[0] = True
            return "rejected", "upstream_rejected", 401

        with (
            mock.patch.object(chat2api, "classify_credential",
                              side_effect=classify),
            mock.patch.object(chat2api.Proxy, "refresh") as refresh,
            mock.patch.object(chat2api, "browser_login") as login,
        ):
            outcome = chat2api._pre_output_verify(
                self.proxy, budget, None, lambda: cancelled[0], "test", "req")
        self.assertEqual(outcome, "cancelled")
        refresh.assert_not_called()
        login.assert_not_called()

    def test_service_stop_never_starts_a_probe(self):
        budget = self._budget(5.0)
        self.proxy._shutdown_event.set()
        with (
            mock.patch.object(chat2api, "classify_credential") as probe,
            mock.patch.object(chat2api, "browser_login") as login,
        ):
            outcome = chat2api._pre_output_verify(
                self.proxy, budget, None, None, "test", "req")
        self.assertEqual(outcome, "stopped")
        probe.assert_not_called()
        login.assert_not_called()

    def test_an_earlier_request_deadline_keeps_the_original_error(self):
        budget = self._budget(5.0)
        with (
            mock.patch.object(chat2api, "classify_credential") as probe,
            mock.patch.object(chat2api, "browser_login") as login,
        ):
            outcome = chat2api._pre_output_verify(
                self.proxy, budget, time.monotonic() - 1, None, "test", "req")
        self.assertEqual(outcome, "retry")
        probe.assert_not_called()
        login.assert_not_called()

    def test_new_ledger_row_adds_request_start_and_keeps_completion_time(self):
        started = time.time() - 5
        chat2api.record_usage(
            "stub-model", False, started,
            {"prompt_tokens": 1, "completion_tokens": 1}, 1, 1)
        record = self._usage_records()[-1]
        self.assertIn("time", record)
        self.assertIn("request_started_at", record)
        self.assertLess(record["request_started_at"], record["time"])
        self.assertIn("price_version", record)


if __name__ == "__main__":
    unittest.main()
