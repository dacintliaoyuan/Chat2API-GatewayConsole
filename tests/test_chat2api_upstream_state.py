import importlib.util
import pathlib
import sys
import types
import unittest
from unittest import mock


CHAT2API_PATH = pathlib.Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py"


def _load_chat2api():
    # Use a fresh module name for every test. This prevents module-level caches,
    # logger handlers, and environment-derived constants from leaking between
    # alphabetically ordered unittest cases.
    name = f"chat2api_upstream_state_test_{id(object())}"
    spec = importlib.util.spec_from_file_location(name, CHAT2API_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return name, module


def _args():
    return types.SimpleNamespace(
        base_url="https://example.invalid",
        no_browser=True,
        profile="unused",
        login_timeout=1,
        use_api_key=False,
    )


class UpstreamReachabilityTests(unittest.TestCase):
    def setUp(self):
        self.module_name, self.chat2api = _load_chat2api()

    def tearDown(self):
        # Per-case cleanup is intentional: importing chat2api must never let one
        # test's globals or patches influence the next test by name order.
        sys.modules.pop(self.module_name, None)

    def test_fast_retry_backoff_is_half_one_two_seconds_capped(self):
        delay = self.chat2api._retry_delay_seconds
        self.assertEqual(delay(1, 0.01, slow=False), 0.5)
        self.assertEqual(delay(2, 0.01, slow=False), 1.0)
        self.assertEqual(delay(3, 0.01, slow=False), 2.0)
        self.assertEqual(delay(4, 5.0, slow=False), 2.0)

    def test_health_stays_alive_when_upstream_is_unreachable(self):
        proxy = self.chat2api.Proxy({"token": "test-token"}, _args())
        with mock.patch.object(
                self.chat2api, "classify_credential",
                return_value=("unknown", "network_error", None)):
            ready, readiness = proxy.readiness_snapshot()

        self.assertFalse(ready)
        self.assertEqual(readiness["status"], "not_ready")
        self.assertEqual(readiness["reason"], "network_error")
        self.assertEqual(
            readiness["upstream_reachability"]["state"], "unreachable")

        health = proxy.health_snapshot()
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["upstream_reachability"]["state"], "unreachable")
        # Existing health keys remain present; the new reachability object is
        # additive and does not reshape the established contract.
        for key in (
                "chat2api_version", "chat2api_build", "backend", "models",
                "authentication", "upstream", "docs"):
            self.assertIn(key, health)

    def test_no_local_credential_exits_without_upstream_request(self):
        with mock.patch.object(self.chat2api.requests, "get") as get:
            result = self.chat2api.classify_credential(
                "https://example.invalid", "")
        self.assertEqual(result, ("needs_login", "no_local_credential", None))
        get.assert_not_called()

        proxy = self.chat2api.Proxy({}, _args())
        with mock.patch.object(
                self.chat2api, "classify_credential",
                return_value=("needs_login", "no_local_credential", None)):
            ready, snapshot = proxy.readiness_snapshot()
        self.assertFalse(ready)
        self.assertEqual(snapshot["authentication"], "needs_login")
        self.assertEqual(snapshot["upstream_reachability"]["state"], "unknown")

    def test_http_error_still_proves_transport_reachability(self):
        proxy = self.chat2api.Proxy({"token": "test-token"}, _args())
        with mock.patch.object(
                self.chat2api, "classify_credential",
                return_value=("unknown", "upstream_5xx", 503)):
            ready, snapshot = proxy.readiness_snapshot()
        self.assertFalse(ready)
        self.assertEqual(snapshot["upstream_status"], 503)
        self.assertEqual(snapshot["upstream_reachability"]["state"], "ok")

    def test_ok_unreachable_ok_transition_logs_outage_and_recovery(self):
        proxy = self.chat2api.Proxy({"token": "test-token"}, _args())
        observations = [
            ("valid", None, 200),
            ("unknown", "network_error", None),
            ("valid", None, 200),
        ]
        with mock.patch.object(
                self.chat2api.time, "time",
                side_effect=[101.0, 106.0, 111.0]), mock.patch.object(
                self.chat2api, "classify_credential",
                side_effect=observations), self.assertLogs(
                self.chat2api._logger, level="WARNING") as captured:
            ready1, snap1 = proxy.readiness_snapshot()
            ready2, snap2 = proxy.readiness_snapshot()
            ready3, snap3 = proxy.readiness_snapshot()

        self.assertTrue(ready1)
        self.assertEqual(snap1["upstream_reachability"]["state"], "ok")
        self.assertFalse(ready2)
        self.assertEqual(snap2["upstream_reachability"]["state"], "unreachable")
        self.assertEqual(
            snap2["upstream_reachability"]["outage_started_epoch"], 106.0)
        self.assertTrue(ready3)
        self.assertEqual(snap3["upstream_reachability"]["state"], "ok")
        self.assertIsNone(
            snap3["upstream_reachability"]["outage_started_epoch"])

        logs = "\n".join(captured.output)
        self.assertIn("to=unreachable", logs)
        self.assertIn("outage_started_epoch=106.000", logs)
        self.assertIn("upstream reachability recovered", logs)
        self.assertIn("recovered_epoch=111.000", logs)
        self.assertIn("duration_s=5.000", logs)

    def test_ambiguous_config_failure_does_not_clobber_known_state(self):
        proxy = self.chat2api.Proxy({"token": "test-token"}, _args())
        proxy._record_upstream_reachability(
            "unreachable", reason="network_error")
        before = proxy.upstream_reachability_snapshot()
        with mock.patch.object(
                self.chat2api, "fetch_upstream_config", return_value=None):
            self.assertIsNone(proxy.probe_config())
        after = proxy.upstream_reachability_snapshot()
        self.assertEqual(after["state"], "unreachable")
        self.assertEqual(after["checked_epoch"], before["checked_epoch"])


if __name__ == "__main__":
    unittest.main()
