# -*- coding: utf-8 -*-
"""Per-action + per-target enablement policy tests (Revision 5 L1+).

The policy maps in console_adapters are the ONLY enablement authority for
real service start/stop. B0 (2026-11-01): the default assembly keeps only
the UM main chain (chat2api 8000); the retired Qoder CN / Qoder Global /
CLIProxy targets are not in the policy. The usage web host is deliberately
absent from both sets (stop-all must never stop the console's own process).
Fail-closed behaviour is exercised by patching EMPTY sets; the submit-time
resource-function gate is tested against the real production policy (a
refused target never creates an operation row). No test enables real
operations — the authorized-target case stops at the handler gate with a
mocked wrapper, so nothing outside the process can ever be touched.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from console import adapters as console_adapters  # noqa: E402
from console import share_actions as console_share_actions  # noqa: E402
from console.operations import (OperationError, OperationManager,  # noqa: E402
                                OperationUnavailable)


class RuntimePolicyTests(unittest.TestCase):
    def test_production_policy_state(self):
        # B0 (2026-11-01): only the UM main chain (chat2api 8000) is
        # runtime-managed; the web host stays excluded (stop-all contract)
        # and the retired Qoder/CLIProxy targets are not in the policy.
        self.assertEqual(
            console_adapters.SERVICE_START_ENABLED_TARGETS,
            frozenset({"chat2api"}))
        self.assertEqual(
            console_adapters.SERVICE_STOP_ENABLED_TARGETS,
            frozenset({"chat2api"}))
        specs = {s.name: s for s in console_adapters.build_registry()}
        for name in ("service.start", "service.stop"):
            with self.subTest(action=name):
                self.assertTrue(specs[name].enabled)
                self.assertIsNone(specs[name].disabled_reason)

    def test_archived_m365_lifecycle_refuses_without_invoking_runtime(self):
        with mock.patch.object(console_adapters, "_invoke_wrapper") as wrapped:
            adapter = console_adapters.RuntimeServiceAdapter()
            for action in ("start", "stop", "restart"):
                with self.subTest(action=action):
                    enabled, _ = console_adapters.runtime_target_enabled(
                        "service." + action, "M365")
                    self.assertFalse(enabled)
                    with self.assertRaises(OperationUnavailable):
                        getattr(adapter, "service_" + action)("M365", {}, None)
            wrapped.assert_not_called()

    def test_retired_m365_actions_refuse_before_creating_operations(self):
        registry = console_adapters.build_registry()
        self.assertFalse(any(spec.name.startswith("m365.") for spec in registry))
        with tempfile.TemporaryDirectory() as directory:
            manager = OperationManager(
                os.path.join(directory, "state.sqlite3"), registry,
                os.path.join(directory, "locks"))
            try:
                for action in (
                        "m365.settings_save", "m365.supply_status",
                        "m365.candidate_status", "m365.candidate_verify",
                        "m365.mapping_publish", "m365.speedtest_key_setup"):
                    with self.subTest(action=action):
                        with self.assertRaises(OperationError) as caught:
                            manager.submit(action, target="M365", params={})
                        self.assertEqual(caught.exception.code, "invalid_action")
                self.assertEqual(manager.list_operations(), [])
            finally:
                manager.close()

    def test_runtime_target_enabled_unit(self):
        enabled, reason = console_adapters.runtime_target_enabled(
            "service.start", "usage")
        self.assertFalse(enabled)
        self.assertIn("授权集合", reason)
        # Unknown targets refuse under a populated allow-set too.
        with mock.patch.object(console_adapters,
                               "SERVICE_START_ENABLED_TARGETS",
                               frozenset({"chat2api"})):
            enabled2, reason2 = console_adapters.runtime_target_enabled(
                "service.start", "usage")
            self.assertFalse(enabled2)
            self.assertIn("授权集合", reason2)
            enabled3, reason3 = console_adapters.runtime_target_enabled(
                "service.start", "chat2api")
            self.assertTrue(enabled3)
            self.assertIsNone(reason3)
        # A populated set for start does NOT enable stop (per-action sets).
        with mock.patch.object(console_adapters,
                               "SERVICE_START_ENABLED_TARGETS",
                               frozenset({"usage"})):
            enabled4, _reason4 = console_adapters.runtime_target_enabled(
                "service.stop", "usage")
            self.assertFalse(enabled4)

    def test_registry_flip_is_data_only(self):
        # With the production set patched EMPTY the action flips back to
        # disabled with the policy reason; a populated set flips only that
        # action (per-action sets).
        with mock.patch.object(console_adapters,
                               "SERVICE_START_ENABLED_TARGETS",
                               frozenset()), \
                mock.patch.object(console_adapters,
                                  "SERVICE_STOP_ENABLED_TARGETS",
                                  frozenset()):
            specs = {s.name: s for s in console_adapters.build_registry()}
            self.assertFalse(specs["service.start"].enabled)
            self.assertIn("授权集合", specs["service.start"].disabled_reason)
            self.assertFalse(specs["service.stop"].enabled)
            self.assertIn("授权集合", specs["service.stop"].disabled_reason)
        with mock.patch.object(console_adapters,
                               "SERVICE_STOP_ENABLED_TARGETS",
                               frozenset({"chat2api"})):
            specs2 = {s.name: s for s in console_adapters.build_registry()}
            self.assertTrue(specs2["service.stop"].enabled)
            self.assertIsNone(specs2["service.stop"].disabled_reason)
            self.assertTrue(specs2["service.start"].enabled)  # untouched

    def test_handler_gates_unauthorized_target(self):
        # Defense in depth: with the policy set non-empty (spec enabled),
        # the handler still refuses any target outside the allow-set, and
        # the authorized path is exercised only up to the wrapper seam
        # (mocked) — no real service can be touched from a test.
        with mock.patch.object(console_adapters,
                               "SERVICE_START_ENABLED_TARGETS",
                               frozenset({"chat2api"})), \
                mock.patch.object(console_adapters, "_invoke_wrapper",
                                  return_value={"ok": True, "result": {
                                      "key": "chat2api",
                                      "status": "Started"}}) as wrapped:
            adapter = console_adapters.RuntimeServiceAdapter()
            with self.assertRaises(OperationUnavailable) as ctx:
                adapter.service_start("usage", {}, None)
            self.assertIn("授权集合", ctx.exception.reason)
            result = adapter.service_start("chat2api", {}, None)
            self.assertEqual(result["key"], "chat2api")
            self.assertEqual(wrapped.call_count, 1)
            # The authorized call must carry the caller-holds-lock contract
            # (keyword argument; the wrapper skips its own acquisition).
            self.assertEqual(wrapped.call_args[0][0], "start")
            self.assertEqual(wrapped.call_args[1]["key"], "chat2api")
            self.assertTrue(wrapped.call_args[1]["caller_holds_lock"])

    def test_submit_refuses_unauthorized_target_before_queueing(self):
        # Submit-time fail-closed gate: the resource function (invoked by
        # the manager during submit, before any insert) refuses a target
        # outside the production allow-set, so NO operation row is created.
        with self.assertRaises(OperationUnavailable) as ctx:
            console_adapters._service_start_resource("usage", {})
        self.assertIn("授权集合", ctx.exception.reason)
        for target in ("chat2api",):
            with self.subTest(target=target):
                self.assertEqual(
                    console_adapters._service_start_resource(target, {}),
                    "service:" + target)
                self.assertEqual(
                    console_adapters._service_stop_resource(target, {}),
                    "service:" + target)
        for target in ("Qoder", "QoderGlobal", "CLIProxy"):
            with self.subTest(retired=target):
                with self.assertRaises(OperationUnavailable):
                    console_adapters._service_start_resource(target, {})
                with self.assertRaises(OperationUnavailable):
                    console_adapters._service_stop_resource(target, {})


class Rev9ShareMaintenanceRegistryTests(unittest.TestCase):
    """Rev9 A9-1/A9-4: the read-only diagnostics and the 8100 restart stay
    registered with the frozen param/return schemas (contract §1.2/§10.1)."""

    def _specs(self):
        return {s.name: s for s in console_adapters.build_cloud_registry()}

    def test_retained_actions_registered_enabled(self):
        specs = self._specs()
        for name in ("share.diagnostics", "share.gateway_restart"):
            with self.subTest(action=name):
                spec = specs[name]
                self.assertTrue(spec.enabled)
                self.assertIsNone(spec.disabled_reason)
                self.assertTrue(spec.cancelable)
                self.assertTrue(spec.description)
                self.assertEqual(spec.resource_fn("share", {}),
                                 "share:orchestration")

    def test_frozen_param_schemas(self):
        specs = self._specs()
        self.assertEqual(specs["share.diagnostics"].param_schema, {})
        self.assertEqual(
            specs["share.gateway_restart"].param_schema,
            {"restart_budget_s": {"type": "int", "min": 10, "max": 180}})
        # Rev9 §6.1/§6.3: the 8100 restart is ONE broker call carrying the
        # budget only and the broker polls internally at its own fixed cadence
        # (§6.2); the console must not publish a poll_interval_s knob that
        # cannot take effect.
        self.assertNotIn(
            "poll_interval_s",
            specs["share.gateway_restart"].param_schema)

    def test_restart_bumps_generation_only(self):
        specs = self._specs()
        self.assertTrue(
            specs["share.gateway_restart"].bump_generation_on_success)
        # Read-only / diagnostics never cancels queued work on success.
        self.assertFalse(
            specs["share.diagnostics"].bump_generation_on_success)
        self.assertFalse(specs["share.status"].bump_generation_on_success)

    def test_handlers_are_the_module_functions(self):
        specs = self._specs()
        self.assertIs(specs["share.diagnostics"].handler,
                      console_share_actions.diagnostics)
        self.assertIs(specs["share.gateway_restart"].handler,
                      console_share_actions.gateway_restart)



class RetiredSourceRegistryTests(unittest.TestCase):
    """B0 (2026-11-01): retired-source actions are NOT registered.

    The Qoder CN / Qoder Global / CLIProxy action cards are gone from the
    default assembly: no handler can run and no fake success is possible. The
    dedicated retirement gate is gone too; a submit naming one reaches the
    operation manager as an unknown action (``invalid_action``).
    """

    RETIRED = (
        "qoder.credential_bridge", "qoder.source_diagnostics",
        "qoder.quota_read", "qoder.status", "qoder.models", "qoder.poc",
        "qoderglobal.status", "qoderglobal.models", "qoderglobal.quota",
        "qoderglobal.login", "cliproxy.status",
    )

    def test_retired_actions_are_not_registered(self):
        names = {s.name for s in console_adapters.build_registry()}
        for action in self.RETIRED:
            with self.subTest(action=action):
                self.assertNotIn(action, names)

    def test_retired_action_gate_is_gone(self):
        for name in ("RETIRED_ACTION_NAMES", "RETIRED_ACTION_PREFIXES",
                     "SOURCE_RETIRED_REASON", "retired_action",
                     "refuse_retired_action"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(console_adapters, name))

    def test_retired_submit_creates_no_operation_row(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        manager = OperationManager(
            os.path.join(tmp.name, "state.sqlite3"),
            console_adapters.build_cloud_registry(),
            os.path.join(tmp.name, "locks"))
        self.addCleanup(manager.close)
        for action in self.RETIRED:
            with self.subTest(action=action):
                with self.assertRaises(OperationError) as caught:
                    manager.submit(action, target="", params={})
                self.assertEqual(caught.exception.code, "invalid_action")
        self.assertEqual(manager.list_operations(), [])


if __name__ == "__main__":
    unittest.main()
