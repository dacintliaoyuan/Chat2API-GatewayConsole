# -*- coding: utf-8 -*-
"""Regression tests for the console operation layer (P1 local foundation)."""

import importlib.util
import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

_OPS_PATH = Path(__file__).resolve().parents[1] / "console/operations.py"
_SPEC = importlib.util.spec_from_file_location("console_operations_test",
                                               _OPS_PATH)
console_operations = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = console_operations
_SPEC.loader.exec_module(console_operations)

from console.operations import (ActionSpec, ConsoleStateError, LockBusy,
                                OperationCancelled, OperationError,
                                OperationManager, OperationUnavailable,
                                ResourceFileLock)


def default_ok(target, params, ctx):
    return {"ok": True, "key": target}


def make_spec(name="service.start", handler=None, enabled=True,
              disabled_reason=None, bump=True, resource_fn=None,
              param_schema=None):
    return ActionSpec(
        name, "test action",
        handler=handler if handler is not None else default_ok,
        resource_fn=resource_fn or (lambda t, p: "service:" + (t or "x")),
        param_schema=param_schema, enabled=enabled,
        disabled_reason=disabled_reason, bump_generation_on_success=bump)


def wait_terminal(mgr, op_id, timeout=6.0):
    deadline = time.time() + timeout
    op = mgr.get_operation(op_id)
    while op["state"] not in ("succeeded", "failed", "cancelled",
                              "interrupted"):
        time.sleep(0.02)
        op = mgr.get_operation(op_id)
        assert time.time() < deadline, "operation did not finish: %r" % op
    return op


class ConsoleTestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_dir = tmp.name
        self.db_path = os.path.join(self.tmp_dir, "console-state.sqlite3")
        self.lock_dir = os.path.join(self.tmp_dir, "console-locks")

    def manager(self, specs, acquire_timeout_s=15.0):
        mgr = OperationManager(self.db_path, specs, self.lock_dir,
                               acquire_timeout_s=acquire_timeout_s)
        self.addCleanup(mgr.close)
        return mgr


class SubmitValidationTests(ConsoleTestBase):
    def test_unknown_action_rejected(self):
        mgr = self.manager([make_spec()])
        with self.assertRaises(OperationError) as cm:
            mgr.submit("no.such", "x")
        self.assertEqual(cm.exception.code, "invalid_action")

    def test_disabled_action_is_explicit(self):
        mgr = self.manager(
            [make_spec(enabled=False, disabled_reason="真实闭环待授权")])
        with self.assertRaises(OperationUnavailable) as cm:
            mgr.submit("service.start", "x")
        self.assertEqual(cm.exception.code, "action_disabled")
        self.assertIn("真实闭环待授权", cm.exception.reason)

    def test_unknown_param_rejected(self):
        mgr = self.manager([make_spec(param_schema={"note": {"type": "str",
                                                             "max_len": 8}})])
        with self.assertRaises(OperationError) as cm:
            mgr.submit("service.start", "x", {"bogus": 1})
        self.assertEqual(cm.exception.code, "invalid_params")

    def test_param_type_and_size_rejected(self):
        schema = {"note": {"type": "str", "max_len": 8}}
        mgr = self.manager([make_spec(param_schema=schema)])
        with self.assertRaises(OperationError):
            mgr.submit("service.start", "x", {"note": 3})
        with self.assertRaises(OperationError):
            mgr.submit("service.start", "x", {"note": "y" * 5000})
        with self.assertRaises(OperationError):
            mgr.submit("service.start", "x", {"note": {"deep": True}})

    def test_empty_target_rejected_for_targeted_action(self):
        mgr = self.manager([make_spec()])
        with self.assertRaises(OperationError) as cm:
            mgr.submit("service.start", "")
        self.assertEqual(cm.exception.code, "invalid_target")


class LifecycleTests(ConsoleTestBase):
    def test_success_lifecycle_records_steps(self):
        def handler(target, params, ctx):
            ctx.report_step("begin")
            ctx.raise_if_cancelled()
            return {"ok": True, "key": target}

        mgr = self.manager([make_spec(handler=handler)])
        op = mgr.submit("service.start", "chat2api", None)
        op = wait_terminal(mgr, op["id"])
        self.assertEqual(op["state"], "succeeded")
        self.assertEqual(op["result"], {"ok": True, "key": "chat2api"})
        self.assertTrue(op["steps"])
        self.assertEqual(op["steps"][0]["note"], "begin")
        self.assertEqual(mgr.generation_of("service:chat2api"), 1)
        mgr.shutdown(wait=True)

    def test_idempotency_dedupe_and_conflicts(self):
        mgr = self.manager([make_spec(), make_spec("service.stop", bump=False)])
        first = mgr.submit("service.start", "chat2api", None, "key-1")
        again = mgr.submit("service.start", "chat2api", None, "key-1")
        self.assertTrue(again.get("deduplicated"))
        self.assertEqual(first["id"], again["id"])
        with self.assertRaises(OperationError) as cm:
            mgr.submit("service.stop", "chat2api", None, "key-1")
        self.assertEqual(cm.exception.code, "idempotency_key_conflict")
        mgr.shutdown(wait=True)


class CancelTests(ConsoleTestBase):
    def test_cancel_queued_never_executes(self):
        executed = []
        release = threading.Event()

        def slow(target, params, ctx):
            executed.append(target)
            time.sleep(0.2)
            return {"ok": True}

        def blocked(target, params, ctx):
            release.wait(3.0)
            return {"ok": True}

        mgr = self.manager([make_spec(handler=slow)])
        first = mgr.submit("service.start", "x")
        second = mgr.submit("service.start", "x")
        deadline = time.time() + 3
        while mgr.get_operation(first["id"])["state"] != "running":
            self.assertLess(time.time(), deadline)
            time.sleep(0.02)
        mgr.cancel(second["id"])
        second = wait_terminal(mgr, second["id"])
        self.assertEqual(second["state"], "cancelled")
        self.assertEqual(second["error"]["code"], "cancelled")
        release.set()
        wait_terminal(mgr, first["id"])
        self.assertEqual(executed, ["x"])
        mgr.shutdown(wait=True)

    def test_cancel_running_is_cooperative(self):
        def cooperative(target, params, ctx):
            for _ in range(50):
                ctx.raise_if_cancelled()
                time.sleep(0.02)
            return {"ok": True}

        mgr = self.manager([make_spec(handler=cooperative)])
        op_id = mgr.submit("service.start", "x")["id"]
        deadline = time.time() + 3
        while mgr.get_operation(op_id)["state"] != "running":
            self.assertLess(time.time(), deadline)
            time.sleep(0.02)
        mgr.cancel(op_id)
        op = wait_terminal(mgr, op_id)
        self.assertEqual(op["state"], "cancelled")
        self.assertEqual(op["error"]["code"], "cancelled")
        mgr.shutdown(wait=True)

    def test_completed_despite_cancel_request_keeps_result(self):
        def quick(target, params, ctx):
            time.sleep(0.3)
            return {"ok": True}

        mgr = self.manager([make_spec(handler=quick)])
        op_id = mgr.submit("service.start", "x")["id"]
        deadline = time.time() + 3
        while mgr.get_operation(op_id)["state"] != "running":
            self.assertLess(time.time(), deadline)
            time.sleep(0.02)
        mgr.cancel(op_id)
        op = wait_terminal(mgr, op_id)
        self.assertEqual(op["state"], "succeeded")
        self.assertEqual(op["result"], {"ok": True})
        notes = " ".join(step["note"] for step in op["steps"])
        self.assertIn("cancel was requested", notes)
        mgr.shutdown(wait=True)


class GenerationTests(ConsoleTestBase):
    def test_bump_cancels_queued_same_resource(self):
        release = threading.Event()

        def long_op(target, params, ctx):
            release.wait(3.0)
            return {"ok": True}

        mgr = self.manager([make_spec(handler=long_op)])
        first = mgr.submit("service.start", "x")
        second = mgr.submit("service.start", "x")
        deadline = time.time() + 3
        while mgr.get_operation(second["id"])["state"] != "queued" \
                or mgr.get_operation(first["id"])["state"] != "running":
            self.assertLess(time.time(), deadline)
            time.sleep(0.02)
        mgr.bump_generation("service:x")
        second = wait_terminal(mgr, second["id"])
        self.assertEqual(second["state"], "cancelled")
        self.assertEqual(second["error"]["code"], "superseded")
        release.set()
        wait_terminal(mgr, first["id"])
        mgr.shutdown(wait=True)

    def test_running_op_late_result_discarded_after_bump(self):
        release = threading.Event()

        def long_op(target, params, ctx):
            release.wait(3.0)
            return {"ok": True, "late": True}

        mgr = self.manager([make_spec(handler=long_op)])
        op_id = mgr.submit("service.start", "x")["id"]
        deadline = time.time() + 3
        while mgr.get_operation(op_id)["state"] != "running":
            self.assertLess(time.time(), deadline)
            time.sleep(0.02)
        mgr.bump_generation("service:x")
        release.set()
        op = wait_terminal(mgr, op_id)
        self.assertEqual(op["state"], "cancelled")
        self.assertEqual(op["error"]["code"], "superseded")
        self.assertIsNone(op["result"])
        mgr.shutdown(wait=True)


class PersistenceTests(ConsoleTestBase):
    def test_restart_marks_unfinished_interrupted_without_replay(self):
        executed = []
        release = threading.Event()

        def blocked(target, params, ctx):
            executed.append(target)
            release.wait(3.0)
            return {"ok": True}

        specs = [make_spec(handler=blocked)]
        mgr = self.manager(specs)
        op_id = mgr.submit("service.start", "x")["id"]
        deadline = time.time() + 3
        while mgr.get_operation(op_id)["state"] != "running":
            self.assertLess(time.time(), deadline)
            time.sleep(0.02)
        # simulate a restart: a fresh manager opens the same database
        mgr2 = OperationManager(self.db_path, specs, self.lock_dir)
        self.addCleanup(mgr2.close)
        row = mgr2.get_operation(op_id)
        self.assertEqual(row["state"], "interrupted")
        self.assertEqual(row["error"]["code"], "interrupted_by_restart")
        release.set()
        wait_terminal(mgr, op_id)
        self.assertEqual(mgr2.get_operation(op_id)["state"], "interrupted")
        self.assertEqual(executed, ["x"])  # ran exactly once, never replayed
        mgr.shutdown(wait=True)
        mgr2.shutdown(wait=True)

    def test_incompatible_schema_fails_closed(self):
        mgr = self.manager([make_spec()])
        mgr.shutdown(wait=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE console_meta SET value='99' WHERE key='schema_version'")
        conn.commit()
        conn.close()
        before = open(self.db_path, "rb").read()
        with self.assertRaises(ConsoleStateError):
            OperationManager(self.db_path, [make_spec()], self.lock_dir)
        self.assertEqual(open(self.db_path, "rb").read(), before)


class LockTests(ConsoleTestBase):
    def test_second_acquire_fails_while_held(self):
        lock_dir = os.path.join(self.tmp_dir, "manual-locks")
        holder = ResourceFileLock(lock_dir, "service:x", "holder",
                                  timeout_s=1.0)
        holder.acquire()
        try:
            contender = ResourceFileLock(lock_dir, "service:x", "other",
                                         timeout_s=0.2)
            with self.assertRaises(LockBusy):
                contender.acquire()
        finally:
            holder.__exit__(None, None, None)
        contender = ResourceFileLock(lock_dir, "service:x", "third",
                                     timeout_s=1.0)
        contender.acquire()
        contender.__exit__(None, None, None)

    def test_lock_file_records_owner_info(self):
        lock_dir = os.path.join(self.tmp_dir, "manual-locks")
        holder = ResourceFileLock(lock_dir, "service:x", "owner-info",
                                  timeout_s=1.0)
        holder.acquire()
        holder.__exit__(None, None, None)
        # The lock handle is exclusive, so the owner-info file is only
        # readable after release; the file itself persists with the info.
        with open(holder.path, "rb") as f:
            info = json.loads(f.read().decode("utf-8"))
        self.assertEqual(info["owner"], "owner-info")
        self.assertEqual(info["resource"], "service:x")
        self.assertEqual(info["pid"], os.getpid())

    @unittest.skipUnless(os.name == "posix",
                         "POSIX flock path requires POSIX")
    def test_posix_flock_is_exclusive_and_releases_on_close(self):
        """Linux console actions must not fail on the Windows-only msvcrt
        import: the same resource lock uses fcntl.flock and the descriptor
        is the lock (closing releases it, no stale marker)."""
        lock_dir = os.path.join(self.tmp_dir, "posix-locks")
        holder = ResourceFileLock(lock_dir, "share:orchestration",
                                  "holder", timeout_s=1.0)
        holder.acquire()
        try:
            self.assertEqual(stat.S_IMODE(os.stat(holder.path).st_mode),
                             0o600)
            contender = ResourceFileLock(lock_dir, "share:orchestration",
                                         "other", timeout_s=0.2)
            with self.assertRaises(LockBusy):
                contender.acquire()
        finally:
            holder.__exit__(None, None, None)
        contender = ResourceFileLock(lock_dir, "share:orchestration",
                                     "third", timeout_s=1.0)
        contender.acquire()
        contender.__exit__(None, None, None)

    def test_resource_lock_serializes_operations(self):
        events = []

        def tracked(target, params, ctx):
            events.append(("start", target))
            time.sleep(0.15)
            events.append(("end", target))
            return {"ok": True}

        mgr = self.manager([make_spec(handler=tracked, bump=False)])
        first = mgr.submit("service.start", "x")
        second = mgr.submit("service.start", "x")
        wait_terminal(mgr, first["id"])
        wait_terminal(mgr, second["id"])
        self.assertEqual(events, [("start", "x"), ("end", "x"),
                                  ("start", "x"), ("end", "x")])
        mgr.shutdown(wait=True)


class ShareOrchestrationResourceTests(ConsoleTestBase):
    """Rev9 A9-7: the maintenance actions share ONE resource, serialize,
    reuse a duplicate operation by key and are explicitly busy otherwise."""

    def _share_spec(self, name, handler, bump=False):
        return ActionSpec(
            name, "test share action", handler=handler,
            resource_fn=lambda t, p: "share:orchestration",
            enabled=True, bump_generation_on_success=bump)

    def test_share_resource_serializes_and_queued_duplicate_cancels(self):
        events = []
        release = threading.Event()

        def first(target, params, ctx):
            events.append("first-start")
            release.wait(3.0)
            events.append("first-end")
            return {"ok": True}

        def second(target, params, ctx):
            events.append("second")
            return {"ok": True}

        mgr = self.manager([
            self._share_spec("share.backend_restart", first, bump=True),
            self._share_spec("share.gateway_restart", second, bump=True)])
        op1 = mgr.submit("share.backend_restart", "share", {})
        deadline = time.time() + 3
        while mgr.get_operation(op1["id"])["state"] != "running":
            self.assertLess(time.time(), deadline)
            time.sleep(0.02)
        op2 = mgr.submit("share.gateway_restart", "share", {})
        self.assertEqual(mgr.get_operation(op2["id"])["state"], "queued")
        # Stop priority: the queued duplicate can be cancelled and NEVER runs.
        cancelled = mgr.cancel(op2["id"])
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(cancelled["error"]["code"], "cancelled")
        release.set()
        wait_terminal(mgr, op1["id"])
        self.assertEqual(events, ["first-start", "first-end"])
        mgr.shutdown(wait=True)

    def test_share_resource_explicit_busy_when_lock_held(self):
        holder = ResourceFileLock(self.lock_dir, "share:orchestration",
                                 "holder", timeout_s=1.0)
        holder.acquire()
        try:
            mgr = self.manager(
                [self._share_spec("share.diagnostics", default_ok)],
                acquire_timeout_s=0.2)
            op = wait_terminal(
                mgr, mgr.submit("share.diagnostics", "share")["id"])
        finally:
            holder.__exit__(None, None, None)
        self.assertEqual(op["state"], "failed")
        self.assertEqual(op["error"]["code"], "resource_busy_timeout")

    def test_duplicate_request_reuses_operation_by_key(self):
        mgr = self.manager(
            [self._share_spec("share.diagnostics", default_ok)])
        first = mgr.submit("share.diagnostics", "share", {}, "share-dup-1")
        again = mgr.submit("share.diagnostics", "share", {}, "share-dup-1")
        self.assertTrue(again.get("deduplicated"))
        self.assertEqual(first["id"], again["id"])
        wait_terminal(mgr, first["id"])
        mgr.shutdown(wait=True)

    def test_action_info_param_schema_is_bounded_and_json_safe(self):
        schema = {"note": {"type": "str", "max_len": 8},
                  "budget": {"type": "int", "min": 1, "max": 600},
                  "callback": lambda: None,
                  "nested": {"type": "object"}}
        mgr = self.manager([make_spec(param_schema=schema)])
        info = {i["action"]: i for i in mgr.action_info()}["service.start"]
        self.assertEqual(info["param_schema"]["note"],
                         {"type": "str", "max_len": 8})
        self.assertEqual(info["param_schema"]["budget"],
                         {"type": "int", "min": 1, "max": 600})
        self.assertEqual(info["param_schema"]["nested"], {"type": "object"})
        # No function object and no non-whitelisted field survives the copy.
        self.assertNotIn("callback", info["param_schema"])
        json.dumps(info["param_schema"])
        mgr.shutdown(wait=True)

    def test_action_info_works_unbound_like_the_fixture_generator(self):
        # tests/generate_frontend_contract_fixture.py:130 calls
        # OperationManager.action_info(<SimpleNamespace with _specs>): the
        # additive param_schema must not introduce a self attribute lookup.
        from types import SimpleNamespace
        spec = make_spec(param_schema={"a": {"type": "int", "min": 1,
                                             "max": 2}})
        info = OperationManager.action_info(
            SimpleNamespace(_specs={spec.name: spec}))
        self.assertEqual(info[0]["param_schema"],
                         {"a": {"type": "int", "min": 1, "max": 2}})

    def test_share_diagnostics_end_to_end_unwired_is_honest(self):
        # Rev9 A9-8 end-to-end: submit through the REAL registry creates an
        # operation row on share:orchestration; with every seam unwired the
        # read-only report succeeds with seven honestly-unavailable layers
        # (never a raise, never a fabricated state) and is JSON-persisted.
        from console import adapters as console_adapters
        from console import share_actions as console_share_actions
        seam_names = ("_BROKER_OPS", "_BACKEND_OBSERVE",
                      "_BACKEND_OPS", "_MAINTENANCE_STATUS", "_INTENT_WRITE")
        saved = {n: getattr(console_share_actions, n) for n in seam_names}
        try:
            for n in seam_names:
                setattr(console_share_actions, n, None)
            mgr = self.manager(console_adapters.build_cloud_registry(),
                               acquire_timeout_s=2.0)
            op = mgr.submit("share.diagnostics", "share")
            op = wait_terminal(mgr, op["id"])
        finally:
            for n, value in saved.items():
                setattr(console_share_actions, n, value)
        self.assertEqual(op["state"], "succeeded")
        self.assertEqual(op["action"], "share.diagnostics")
        self.assertEqual(op["resource"], "share:orchestration")
        self.assertIsNotNone(op["started_at"])
        self.assertIsNotNone(op["finished_at"])
        self.assertTrue(op["steps"])
        layers = op["result"]["layers"]
        for key in console_share_actions._DIAG_LAYER_KEYS:
            self.assertEqual(layers[key]["state"], "unavailable", key)
        self.assertIn("维护视图未接线", op["result"]["note"])


class SubmitIdempotencyParamTests(ConsoleTestBase):
    """R3: the idempotency identity binds action, target AND params.

    A key replay must only resolve to the original operation when the normalized
    params are the same intent; a different param value is an explicit conflict,
    never a silent reuse of the old operation.
    """

    def _real_host_restart_spec(self, handler):
        # The REAL production param schema (drain_budget_s 1..600), not a test
        # approximation: this is the schema the console exposes for the
        # share.backend_restart host action.
        from console import adapters as console_adapters
        real = {spec.name: spec
                for spec in console_adapters.build_cloud_registry()}[
                    "share.backend_restart"]
        return ActionSpec(
            real.name, real.description, handler=handler,
            resource_fn=real.resource_fn, param_schema=real.param_schema,
            bump_generation_on_success=False)

    def test_real_host_restart_schema_param_conflict_is_rejected(self):
        calls = []
        release = threading.Event()

        def handler(target, params, ctx):
            calls.append(dict(params))
            release.wait(3.0)
            return {"ok": True,
                    "drain_budget_s": params.get("drain_budget_s")}

        mgr = self.manager([self._real_host_restart_spec(handler)])
        first = mgr.submit("share.backend_restart", "share",
                           {"drain_budget_s": 15}, "restart-intent-1")
        deadline = time.time() + 3
        while mgr.get_operation(first["id"])["state"] != "running":
            self.assertLess(time.time(), deadline)
            time.sleep(0.02)

        # Same action/target/key, different normalized params: reject while the
        # original is still running, never queue a second execution.
        with self.assertRaises(OperationError) as cm:
            mgr.submit("share.backend_restart", "share",
                       {"drain_budget_s": 600}, "restart-intent-1")
        self.assertEqual(cm.exception.code, "idempotency_key_conflict")
        self.assertEqual(len(mgr.list_operations()), 1)

        # A lost response / page refresh re-looks up the SAME intent while the
        # original runs: same params dedupe to the original operation.
        replay = mgr.submit("share.backend_restart", "share",
                            {"drain_budget_s": 15}, "restart-intent-1")
        self.assertTrue(replay.get("deduplicated"))
        self.assertEqual(replay["id"], first["id"])

        release.set()
        first = wait_terminal(mgr, first["id"])
        self.assertEqual(first["state"], "succeeded")
        self.assertEqual(first["result"]["drain_budget_s"], 15)
        self.assertEqual(calls, [{"drain_budget_s": 15}])

        # After the terminal result, the same key + same params still dedupe.
        again = mgr.submit("share.backend_restart", "share",
                           {"drain_budget_s": 15}, "restart-intent-1")
        self.assertTrue(again.get("deduplicated"))
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["result"]["drain_budget_s"], 15)

        # An intentionally NEW operation stays distinguishable: a new key is a
        # new intent even with the same params, and a new key with the new
        # params creates and runs another operation.
        independent = mgr.submit("share.backend_restart", "share",
                                {"drain_budget_s": 15}, "restart-intent-3")
        independent = wait_terminal(mgr, independent["id"])
        self.assertNotEqual(independent["id"], first["id"])
        self.assertEqual(independent["result"]["drain_budget_s"], 15)
        second = mgr.submit("share.backend_restart", "share",
                            {"drain_budget_s": 600}, "restart-intent-2")
        second = wait_terminal(mgr, second["id"])
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(second["result"]["drain_budget_s"], 600)
        self.assertEqual(calls, [{"drain_budget_s": 15},
                                {"drain_budget_s": 15},
                                {"drain_budget_s": 600}])
        mgr.shutdown(wait=True)

    def test_normalized_params_legacy_rows_and_defaults_stay_compatible(self):
        calls = []

        def handler(target, params, ctx):
            calls.append(dict(params))
            return {"ok": True}

        schema = {"a": {"type": "int", "min": 1, "max": 10},
                  "b": {"type": "int", "min": 1, "max": 10}}
        mgr = self.manager([make_spec(handler=handler, param_schema=schema,
                                     bump=False)])
        first = mgr.submit("service.start", "x", {"a": 1, "b": 2}, "order-key")
        first = wait_terminal(mgr, first["id"])

        # JSON objects have no key order: the same params in another insertion
        # order are the same intent and dedupe to the original operation.
        again = mgr.submit("service.start", "x", {"b": 2, "a": 1}, "order-key")
        self.assertTrue(again.get("deduplicated"))
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(calls, [{"a": 1, "b": 2}])

        # Omitted params and an explicit empty object are the same default.
        empty = mgr.submit("service.start", "y", None, "default-key")
        empty = wait_terminal(mgr, empty["id"])
        empty_again = mgr.submit("service.start", "y", {}, "default-key")
        self.assertTrue(empty_again.get("deduplicated"))
        self.assertEqual(empty_again["id"], empty["id"])
        self.assertEqual(calls[-1], {})

        # A pre-fix row stored params in insertion order.  The re-lookup must
        # normalize the STORED JSON too, not compare raw text.
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO operations(id, action, target, params_json, state,"
                " idempotency_key, resource, generation, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                ("legacy-op", "service.start", "z", '{"b": 2, "a": 1}',
                 "succeeded", "legacy-key", "service:z", 0, time.time()))
            conn.commit()
        finally:
            conn.close()
        legacy = mgr.submit("service.start", "z", {"a": 1, "b": 2},
                            "legacy-key")
        self.assertTrue(legacy.get("deduplicated"))
        self.assertEqual(legacy["id"], "legacy-op")

        # A different value under the same key is a conflict even after a
        # reordered replay was accepted.
        with self.assertRaises(OperationError) as cm:
            mgr.submit("service.start", "x", {"a": 2, "b": 2}, "order-key")
        self.assertEqual(cm.exception.code, "idempotency_key_conflict")
        self.assertEqual(len(calls), 2)
        mgr.shutdown(wait=True)

    def test_persisted_operation_is_reused_after_restart(self):
        calls = []

        def handler(target, params, ctx):
            calls.append(dict(params))
            return {"ok": True}

        specs = [make_spec(
            handler=handler, bump=False,
            param_schema={"a": {"type": "int", "min": 1, "max": 10}})]
        mgr = self.manager(specs)
        first = mgr.submit("service.start", "x", {"a": 1}, "restart-key")
        first = wait_terminal(mgr, first["id"])
        mgr.close(wait=True)

        # A refreshed page / restarted console has no in-memory dedupe state:
        # the persisted operation row is the lookup entry.
        mgr2 = OperationManager(self.db_path, specs, self.lock_dir)
        self.addCleanup(mgr2.close)
        again = mgr2.submit("service.start", "x", {"a": 1}, "restart-key")
        self.assertTrue(again.get("deduplicated"))
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(calls), 1)
        with self.assertRaises(OperationError) as cm:
            mgr2.submit("service.start", "x", {"a": 2}, "restart-key")
        self.assertEqual(cm.exception.code, "idempotency_key_conflict")
        self.assertEqual(len(calls), 1)
        mgr2.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
