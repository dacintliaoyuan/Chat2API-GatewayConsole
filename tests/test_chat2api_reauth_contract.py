# -*- coding: utf-8 -*-
"""Reauthentication trigger and budget contract (ticket #63).

The fake UM upstream drives the real Proxy/Handler boundary:

* a 401 or a 403 whose bounded body carries UM authentication text triggers
  at most ONE reauthentication per logical request, shared by every retry;
* a second 401 is the final 503 reauthentication state and a second 403
  authentication-text response is 403 ``reauthentication_failed`` with
  ``detail.reauthentication_attempted`` true;
* a 403 permission response, a Cloudflare challenge, 402 and 404 never open a
  login window;
* ``auth_wait_timeout``, ``auth_wait_capacity`` and
  ``global_attempt_cap`` keep the 503 reauthentication shape and their
  reason, and a definite global-cap window carries ``Retry-After``;
* ``--no-browser`` answers 503 with reason ``BrowserDisabled``.

No real browser is opened: ``browser_login`` is replaced with a token
rotation against the in-process fake UM.
"""

import importlib.util
import sys
import tempfile
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
    FakeUM, MODE_INVALID_AUTH, MODE_NORMAL, MODE_SCRIPTED)

CHAT2API_PATH = Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py"
SPEC = importlib.util.spec_from_file_location(
    "chat2api_reauth_contract", CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)

STUB_TOKEN = "stub-token"
LOCAL_KEY = "local-secret-reauth-contract"
AUTH_TEXT = {"detail": "Not authenticated"}
PERMISSION_TEXT = {"detail": "permission denied"}
CHALLENGE_TEXT = {"detail": "Cloudflare challenge required"}


class ReauthContractTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir, self.usage_log = harness.install_usage_isolation(
            self, chat2api)
        harness.assert_stub_credentials(
            chat2api, local_key=LOCAL_KEY, upstream_token=STUB_TOKEN,
            stub_token_literal=STUB_TOKEN)

        self.slot_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.slot_dir.cleanup)
        slot_patcher = mock.patch.object(
            chat2api, "REAUTH_SLOT_FILE",
            str(Path(self.slot_dir.name) / "reauth.state"))
        slot_patcher.start()
        self.addCleanup(slot_patcher.stop)
        # Fake PIDs are used only for lease ownership; pin the liveness probe
        # to the fake owner so the lease/refusal semantics are deterministic.
        liveness_patcher = mock.patch.object(
            chat2api, "_owner_pid_alive",
            side_effect=lambda pid: pid == 111)
        liveness_patcher.start()
        self.addCleanup(liveness_patcher.stop)

        self.fake = FakeUM(port=0, token=STUB_TOKEN, chunk_delay_s=0.0)
        self.fake.start()
        self.addCleanup(self.fake.stop)

        self.login_calls = []
        self.login_behavior = "success"
        self.login_script = None

        def fake_login(*args, **kwargs):
            self.login_calls.append(time.monotonic())
            if self.login_behavior == "success":
                self.fake.set_mode(MODE_NORMAL)
            elif self.login_behavior == "scripted":
                self.fake.set_mode(MODE_SCRIPTED)
            elif self.login_behavior == "scripted_after_login":
                self.fake.set_script(self.login_script)
                self.fake.set_mode(MODE_SCRIPTED)
            # "invalid" leaves MODE_INVALID_AUTH in place.
            return self.fake.rotate_token()

        login_patcher = mock.patch.object(
            chat2api, "browser_login", side_effect=fake_login)
        login_patcher.start()
        self.addCleanup(login_patcher.stop)
        save_patcher = mock.patch.object(chat2api.TokenStore, "save")
        save_patcher.start()
        self.addCleanup(save_patcher.stop)

        for patch in (
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 5),
            mock.patch.object(chat2api, "UPSTREAM_SLOW_AUTO_RETRIES", 2),
            mock.patch.object(chat2api, "_retry_delay_seconds",
                              return_value=0.0),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRY", False),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRIES", 0),
        ):
            patch.start()
            self.addCleanup(patch.stop)

        args = SimpleNamespace(
            base_url=self.fake.base_url,
            token=STUB_TOKEN,
            profile="profile",
            login_timeout=0,
            use_api_key=False,
            no_browser=False,
        )
        self.proxy = chat2api.Proxy({"token": STUB_TOKEN}, args)
        server = harness.TrackedHTTPServer(("127.0.0.1", 0), chat2api.Handler)
        server.proxy = self.proxy
        server.local_api_key = LOCAL_KEY
        server.require_local_api_key = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        harness.install_tracked_server(self, server, name="chat2api-gateway")
        # Runs BEFORE the gateway drain: stop any auth wait / recovery.
        self.addCleanup(self.proxy.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"

    def _script(self, entries):
        self.fake.set_script(entries)
        self.fake.set_mode(MODE_SCRIPTED)

    def _chat_request(self, timeout=10):
        return requests.post(
            f"{self.base}/v1/chat/completions",
            json={"model": "stub-model",
                  "messages": [{"role": "user", "content": "hi"}],
                  "max_tokens": 16},
            headers={"Authorization": f"Bearer {LOCAL_KEY}"},
            timeout=timeout)

    def _messages_request(self, timeout=10):
        return requests.post(
            f"{self.base}/v1/messages",
            json={"model": "stub-model", "max_tokens": 16,
                  "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {LOCAL_KEY}"},
            timeout=timeout)

    def _responses_request(self, timeout=10):
        return requests.post(
            f"{self.base}/v1/responses",
            json={"model": "stub-model", "input": "hi"},
            headers={"Authorization": f"Bearer {LOCAL_KEY}"},
            timeout=timeout)

    def _posts(self):
        return self.fake.state()["request_count"]

    def _hold_lease(self):
        granted, reason, _ledger = chat2api.acquire_reauth_slot(
            chat2api.REAUTH_SLOT_FILE, pid=111, ttl_s=60)
        self.assertTrue(granted, reason)
        self.addCleanup(chat2api.release_reauth_slot,
                        chat2api.REAUTH_SLOT_FILE, 111)

    @staticmethod
    def _wait_patches(budget=0.2, recheck=0.02, deadline=1.0):
        return (
            mock.patch.object(chat2api, "AUTH_WAIT_BUDGET_S", budget,
                              create=True),
            mock.patch.object(chat2api, "AUTH_WAIT_RECHECK_S", recheck,
                              create=True),
            mock.patch.object(chat2api, "MAX_REQUEST_SECONDS", deadline),
        )

    def _install_wait_slots(self, capacity):
        slots = threading.BoundedSemaphore(capacity)
        patcher = mock.patch.object(chat2api, "_auth_wait_slots", slots)
        patcher.start()
        self.addCleanup(patcher.stop)
        return slots


class ReauthTriggerTests(ReauthContractTestBase):
    def test_401_triggers_one_login_and_resends_once(self):
        self.fake.set_mode(MODE_INVALID_AUTH)
        response = self._chat_request()
        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(self.login_calls), 1)
            self.assertEqual(self._posts(), 2)
        finally:
            response.close()

    def test_401_after_login_is_503_without_a_second_login(self):
        self.fake.set_mode(MODE_INVALID_AUTH)
        self.login_behavior = "invalid"
        response = self._chat_request()
        try:
            self.assertEqual(response.status_code, 503)
            error = response.json()["error"]
            self.assertEqual(error["code"], "reauthentication_required")
            self.assertEqual(error["detail"], "upstream_rejected")
            self.assertEqual(len(self.login_calls), 1)
            self.assertEqual(self._posts(), 2)
        finally:
            response.close()

    def test_403_auth_text_triggers_one_login_and_resends_once(self):
        self._script([{"status": 403, "body": AUTH_TEXT}, "normal"])
        response = self._chat_request()
        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(self.login_calls), 1)
            self.assertEqual(self._posts(), 2)
        finally:
            response.close()

    def test_403_auth_text_after_login_is_403_reauthentication_failed(self):
        self._script([{"status": 403, "body": AUTH_TEXT},
                       {"status": 403, "body": AUTH_TEXT}])
        self.login_behavior = "scripted"
        response = self._chat_request()
        try:
            self.assertEqual(response.status_code, 403)
            error = response.json()["error"]
            self.assertEqual(error["type"], "authentication_error")
            self.assertEqual(error["code"], "reauthentication_failed")
            self.assertTrue(error["detail"]["reauthentication_attempted"])
            self.assertEqual(error["detail"]["upstream_status"], 403)
            self.assertEqual(len(self.login_calls), 1)
            self.assertEqual(self._posts(), 2)
        finally:
            response.close()

    def test_second_auth_failure_across_retries_does_not_login_again(self):
        self.fake.set_mode(MODE_INVALID_AUTH)
        self.login_behavior = "scripted_after_login"
        self.login_script = [
            {"status": 500},
            {"status": 403, "body": AUTH_TEXT},
        ]
        response = self._chat_request()
        try:
            self.assertEqual(response.status_code, 403)
            error = response.json()["error"]
            self.assertEqual(error["code"], "reauthentication_failed")
            self.assertTrue(error["detail"]["reauthentication_attempted"])
            self.assertEqual(len(self.login_calls), 1)
            self.assertEqual(self._posts(), 3)
        finally:
            response.close()

    def test_403_auth_failure_is_final_on_every_protocol(self):
        for name, request in (
                ("chat", self._chat_request),
                ("messages", self._messages_request),
                ("responses", self._responses_request)):
            with self.subTest(protocol=name):
                self.fake.reset()
                self.login_calls.clear()
                self.login_behavior = "scripted"
                self._script([{"status": 403, "body": AUTH_TEXT},
                               {"status": 403, "body": AUTH_TEXT}])
                response = request()
                try:
                    self.assertEqual(response.status_code, 403)
                    error = response.json()["error"]
                    self.assertEqual(error["code"],
                                     "reauthentication_failed")
                    self.assertTrue(
                        error["detail"]["reauthentication_attempted"])
                    self.assertEqual(len(self.login_calls), 1)
                    self.assertEqual(self._posts(), 2)
                finally:
                    response.close()


class NonAuthFailureTests(ReauthContractTestBase):
    def test_403_permission_is_403_upstream_forbidden_without_login(self):
        self._script([{"status": 403, "body": PERMISSION_TEXT},
                       {"status": 403, "body": PERMISSION_TEXT}])
        response = self._chat_request()
        try:
            self.assertEqual(response.status_code, 403)
            error = response.json()["error"]
            self.assertEqual(error["type"], "permission_error")
            self.assertEqual(error["code"], "upstream_forbidden")
            self.assertEqual(error["detail"]["upstream_status"], 403)
            self.assertEqual(error["detail"]["upstream_message"],
                             "permission denied")
            self.assertEqual(len(self.login_calls), 0)
            self.assertEqual(self._posts(), 2)
        finally:
            response.close()

    def test_cloudflare_challenge_does_not_login(self):
        self._script([{"status": 403, "body": CHALLENGE_TEXT},
                       {"status": 403, "body": CHALLENGE_TEXT}])
        response = self._chat_request()
        try:
            self.assertEqual(response.status_code, 403)
            error = response.json()["error"]
            self.assertEqual(error["code"], "upstream_forbidden")
            self.assertEqual(len(self.login_calls), 0)
            self.assertEqual(self._posts(), 2)
        finally:
            response.close()

    def test_403_after_login_permission_is_403_upstream_forbidden(self):
        self._script([{"status": 403, "body": AUTH_TEXT},
                       {"status": 403, "body": PERMISSION_TEXT},
                       {"status": 403, "body": PERMISSION_TEXT}])
        self.login_behavior = "scripted"
        response = self._chat_request()
        try:
            self.assertEqual(response.status_code, 403)
            error = response.json()["error"]
            self.assertEqual(error["code"], "upstream_forbidden")
            self.assertEqual(len(self.login_calls), 1)
            self.assertEqual(self._posts(), 3)
        finally:
            response.close()

    def test_403_after_login_402_is_402_upstream_quota_exhausted(self):
        self._script([{"status": 403, "body": AUTH_TEXT},
                       {"status": 402}, {"status": 402}])
        self.login_behavior = "scripted"
        response = self._chat_request()
        try:
            self.assertEqual(response.status_code, 402)
            error = response.json()["error"]
            self.assertEqual(error["code"], "upstream_quota_exhausted")
            self.assertEqual(len(self.login_calls), 1)
            self.assertEqual(self._posts(), 3)
        finally:
            response.close()

    def test_403_after_login_429_keeps_retry_after(self):
        self._script([
            {"status": 403, "body": AUTH_TEXT},
            {"status": 429, "headers": {"Retry-After": "30"}},
        ])
        self.login_behavior = "scripted"
        response = self._chat_request()
        try:
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers.get("Retry-After"), "30")
            self.assertEqual(len(self.login_calls), 1)
            self.assertEqual(self._posts(), 2)
        finally:
            response.close()

    def test_402_is_402_upstream_quota_exhausted_without_login(self):
        self._script([{"status": 402}, {"status": 402}])
        response = self._chat_request()
        try:
            self.assertEqual(response.status_code, 402)
            error = response.json()["error"]
            self.assertEqual(error["code"], "upstream_quota_exhausted")
            self.assertEqual(len(self.login_calls), 0)
            self.assertEqual(self._posts(), 2)
        finally:
            response.close()

    def test_404_is_404_without_login(self):
        self._script([{"status": 404}])
        response = self._chat_request()
        try:
            self.assertEqual(response.status_code, 404)
            error = response.json()["error"]
            self.assertEqual(error["code"], "not_found")
            self.assertEqual(len(self.login_calls), 0)
            self.assertEqual(self._posts(), 1)
        finally:
            response.close()

    def test_429_keeps_retry_after_without_login(self):
        self._script([
            {"status": 429, "headers": {"Retry-After": "30"}},
        ])
        response = self._chat_request()
        try:
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers.get("Retry-After"), "30")
            self.assertEqual(len(self.login_calls), 0)
            self.assertEqual(self._posts(), 1)
        finally:
            response.close()


class ReauthBudgetTests(ReauthContractTestBase):
    def test_auth_wait_timeout_is_503_reauthentication_required(self):
        self.fake.set_mode(MODE_INVALID_AUTH)
        self._hold_lease()
        patches = self._wait_patches()
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        response = self._chat_request()
        try:
            self.assertEqual(response.status_code, 503)
            error = response.json()["error"]
            self.assertEqual(error["code"], "reauthentication_required")
            self.assertEqual(error["detail"], "auth_wait_timeout")
            self.assertIsNone(response.headers.get("Retry-After"))
            self.assertEqual(len(self.login_calls), 0)
            self.assertEqual(self._posts(), 1)
        finally:
            response.close()

    def test_auth_wait_capacity_is_503_reauthentication_required(self):
        self.fake.set_mode(MODE_INVALID_AUTH)
        self._hold_lease()
        slots = self._install_wait_slots(1)
        self.assertTrue(slots.acquire(blocking=False))
        self.addCleanup(slots.release)
        patches = self._wait_patches(budget=1.0, deadline=1.0)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        response = self._chat_request()
        try:
            self.assertEqual(response.status_code, 503)
            error = response.json()["error"]
            self.assertEqual(error["code"], "reauthentication_required")
            self.assertEqual(error["detail"], "auth_wait_capacity")
            self.assertEqual(len(self.login_calls), 0)
            self.assertEqual(self._posts(), 1)
        finally:
            response.close()

    def test_global_attempt_cap_is_503_with_bounded_retry_after(self):
        now = time.time()
        for pid in (201, 202, 203):
            granted, reason, _ledger = chat2api.acquire_reauth_slot(
                chat2api.REAUTH_SLOT_FILE, pid=pid, ttl_s=60,
                now=now - 100)
            self.assertTrue(granted, reason)
        self.fake.set_mode(MODE_INVALID_AUTH)
        patches = self._wait_patches()
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        response = self._chat_request()
        try:
            self.assertEqual(response.status_code, 503)
            error = response.json()["error"]
            self.assertEqual(error["code"], "reauthentication_required")
            self.assertEqual(error["detail"], "global_attempt_cap")
            retry_after = response.headers.get("Retry-After")
            self.assertIsNotNone(retry_after)
            self.assertTrue(retry_after.isdigit(), retry_after)
            self.assertGreaterEqual(int(retry_after), 1)
            self.assertLessEqual(int(retry_after), 900)
            self.assertEqual(len(self.login_calls), 0)
            self.assertEqual(self._posts(), 1)
        finally:
            response.close()

    def test_no_browser_is_503_browser_disabled(self):
        self.fake.set_mode(MODE_INVALID_AUTH)
        with mock.patch.object(self.proxy.args, "no_browser", True):
            response = self._chat_request()
        try:
            self.assertEqual(response.status_code, 503)
            error = response.json()["error"]
            self.assertEqual(error["code"], "reauthentication_required")
            self.assertEqual(error["detail"], "BrowserDisabled")
            self.assertEqual(len(self.login_calls), 0)
            self.assertEqual(self._posts(), 1)
        finally:
            response.close()


class Bounded403ReadTests(unittest.TestCase):
    def test_403_body_read_is_bounded_and_restored(self):
        class Raw:
            def __init__(self):
                self.calls = []

            def read(self, amount, decode_content=True):
                self.calls.append((amount, decode_content))
                return b"x" * amount

        class Response:
            _content = False
            _content_consumed = False
            raw = Raw()

        response = Response()
        text = chat2api._bounded_403_response_text(response)
        self.assertEqual(response.raw.calls, [(8 * 1024, True)])
        self.assertEqual(response._content, b"x" * 8192)
        self.assertTrue(response._content_consumed)
        self.assertEqual(len(text), 8192)


if __name__ == "__main__":
    unittest.main()
