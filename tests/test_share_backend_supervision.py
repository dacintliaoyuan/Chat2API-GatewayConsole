# -*- coding: utf-8 -*-
"""Offline tests for the 8110 dedicated-backend supervision (Rev9 A9-5).

Every test runs against TEMP dirs and injected stubs: no production watch
directory, no broker, no task, no process and no OperationManager is ever
touched. A subprocess guard turns any real powershell/tasklist/netstat/schtasks/
sc.exe invocation into a loud failure, so the production evidence seams can
never be reached from this suite.
"""

import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import share_backend_supervision as supervision  # noqa: E402
import console_share_production  # noqa: E402

BOOT = "test-boot"

_GUARDED = ("run", "Popen", "call", "check_output", "check_call",
             "getoutput", "getstatusoutput")
_ISOLATION_TOKENS = ("powershell", "pwsh", "tasklist", "netstat",
                     "schtasks", "sc.exe", "tailscale")
_orig_subprocess = {}


def _argv_text(args):
    if isinstance(args, (list, tuple)):
        return " ".join(str(a) for a in args)
    return str(args)


def _guarded_spawn(name, original):
    def wrapper(*args, **kwargs):
        text = _argv_text(args[0]) if args else ""
        lowered = text.lower()
        for token in _ISOLATION_TOKENS:
            if token in lowered:
                raise AssertionError(
                    "isolation sentinel: a real OS command was invoked from "
                    "this suite (%r hit %r) - inject a stub instead"
                    % (text, token))
        return original(*args, **kwargs)
    wrapper.__name__ = name
    return wrapper


def setUpModule():
    for name in _GUARDED:
        _orig_subprocess[name] = getattr(subprocess, name)
    for name in _GUARDED:
        setattr(subprocess, name,
                _guarded_spawn(name, _orig_subprocess[name]))


def tearDownModule():
    for name, original in reversed(list(_orig_subprocess.items())):
        setattr(subprocess, name, original)
    _orig_subprocess.clear()


class _StubResult:
    def __init__(self, ok, result=None, error_code="ok"):
        self.ok = bool(ok)
        self.result = result if result is not None else {}
        self.error_code = error_code


class _StubBroker:
    """Broker ADAPTER double: no pipe, no secret, no production path.

    The console seams call the ``BrokerClientAdapter`` public control surface
    directly — ``production_maintenance_status``/``production_maintenance_end``
    invoke the adapter verbs and NEVER ``broker.client`` (pinned by
    tests/test_console_share_production.py::test_console_never_reaches_the_inner_client),
    so the double exposes the adapter verbs itself and deliberately has no
    ``client`` attribute: a seam that regressed to the inner client would read
    as unavailable (``broker_unavailable``) instead of silently passing here.
    """

    def __init__(self):
        self.status_result = _StubResult(
            True, {"checked_at": 0, "lease": None, "lease_expired": None,
                    "instance": None, "last_outcome": None})
        self.end_result = _StubResult(True, {"resumed": True})
        self.end_calls = []

    def set_status(self, ok, payload):
        self.status_result = _StubResult(ok, payload)

    def maintenance_status(self):
        return self.status_result

    def maintenance_end(self, component, lease_id):
        self.end_calls.append((component, lease_id))
        return self.end_result


class _ClearingBroker(_StubBroker):
    """A broker whose maintenance-end clears the expired lease."""

    def maintenance_end(self, component, lease_id):
        self.end_calls.append((component, lease_id))
        self.status_result = _StubResult(
            True, {"checked_at": 1000, "lease": None, "lease_expired": None,
                    "instance": None, "last_outcome": None})
        return self.end_result


class _StubOps:
    """Backend-ops double: pid report + liveness + shared start handler."""

    def __init__(self, state_path=None):
        self.state_path = state_path
        self.pid_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": False,
            "pid": None, "pid_matches": None, "port_listening": False,
            "state": "pid_file_absent", "identity": "unknown",
            "identity_reason": "pid_file_absent",
        }
        self.alive = None
        self.starts = 0
        self.verify_result = True
        self.state_at_start = None

    def backend_pid_state(self, paths=None):
        return dict(self.pid_state)

    def _pid_alive(self, pid):
        return self.alive

    def start(self):
        self.starts += 1
        if self.state_path:
            # The attempt timestamp must already be persisted BEFORE the
            # shared handler runs (a crash must not lose the budget).
            self.state_at_start = supervision.read_watch_state(self.state_path)
        return True

    def verify_running(self, forget_stale=True):
        return self.verify_result


class _SequencedOps(_StubOps):
    """Observe sequence: the instance comes back between check and recheck."""

    def __init__(self, states, state_path=None):
        super(_SequencedOps, self).__init__(state_path)
        self._states = list(states)
        self.observations = 0

    def backend_pid_state(self, paths=None):
        index = min(self.observations, len(self._states) - 1)
        self.observations += 1
        return dict(self._states[index])


class BaseSupervisionTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(
            prefix="SelfUseAI-share-backend-supervision-test-")
        self.state_dir = os.path.join(self.root, "state")
        os.makedirs(self.state_dir, exist_ok=True)
        self.lock_dir = os.path.join(self.root, "locks")
        self.desired_path = os.path.join(
            self.state_dir, supervision.DESIRED_FILE_NAME)
        self.state_path = os.path.join(
            self.state_dir, supervision.STATE_FILE_NAME)
        self.service_log = os.path.join(
            self.root, supervision.SERVICE_LOG_NAME)
        self.lifecycle_log = os.path.join(
            self.root, supervision.LIFECYCLE_LOG_NAME)
        self.decision_log = os.path.join(
            self.state_dir, supervision.LOG_FILE_NAME)
        self.broker = _StubBroker()
        self.ops = _StubOps(self.state_path)
        self._saved_boot = supervision._BOOT_ID_READER
        self._saved_stop = supervision._DESIRED_STOP_READER
        supervision._BOOT_ID_READER = lambda: BOOT
        supervision._DESIRED_STOP_READER = lambda: None

    def tearDown(self):
        supervision._BOOT_ID_READER = self._saved_boot
        supervision._DESIRED_STOP_READER = self._saved_stop
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    # -- fixture helpers -------------------------------------------------

    def write_desired(self, state="running", boot=BOOT, at_utc=None):
        self.assertTrue(supervision.write_desired_state(
            state, source="test", path=self.desired_path, boot_id=boot))
        if at_utc is not None:
            marker = supervision.read_desired_state(self.desired_path)
            marker["at_utc"] = at_utc
            Path(self.desired_path).write_text(
                json.dumps(marker), encoding="utf-8")

    def write_watch_state(self, failures, attempts=(), boot=BOOT):
        supervision.write_watch_state(
            {"boot_id": boot, "consecutive_failures": failures,
             "restart_attempts_utc": list(attempts),
             "idle_reason": "", "last_decision": None},
            self.state_path)

    def write_abrupt_evidence(self):
        # A listening line with no graceful exit plus the console's own
        # forced-stop record: an ABNORMAL exit, not a human close.
        Path(self.service_log).write_text(
            "2026-10-03 09:00:00,000 INFO listening host=127.0.0.1 "
            "port=%d pid=4242\n" % supervision.BACKEND_PORT,
            encoding="utf-8")
        Path(self.lifecycle_log).write_text(
            "2026-10-03 09:02:00 force stop pid=4242 port=%d "
            "by=taskkill result=dead\n" % supervision.BACKEND_PORT,
            encoding="utf-8")

    def candidate(self, failures=2, attempts=(), alive=None):
        self.write_desired()
        self.write_watch_state(failures, attempts)
        self.write_abrupt_evidence()
        self.ops.pid_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": True,
            "pid": 4242, "pid_matches": False, "port_listening": False,
            "state": "pid_stale", "identity": "unknown",
            "identity_reason": "identity_mismatch"}
        self.ops.alive = False if alive is None else alive
        return supervision.check_preconditions(
            now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)


class WriteIntentTests(BaseSupervisionTest):
    def test_running_round_trip(self):
        self.write_desired()
        record = supervision.read_desired_state(self.desired_path)
        self.assertEqual(record["state"], "running")
        self.assertEqual(record["boot_id"], BOOT)
        self.assertEqual(record["key"], supervision.DESIRED_KEY)
        self.assertEqual(record["source"], "test")

    def test_malformed_and_unknown_state_read_as_none(self):
        Path(self.desired_path).write_text("{broken", encoding="utf-8")
        self.assertIsNone(supervision.read_desired_state(self.desired_path))
        Path(self.desired_path).write_text(json.dumps({
            "version": 1, "key": supervision.DESIRED_KEY,
            "state": "maybe", "at_utc": "2026-10-03T00:00:00+00:00",
            "boot_id": BOOT}), encoding="utf-8")
        self.assertIsNone(supervision.read_desired_state(self.desired_path))
        Path(self.desired_path).write_text(json.dumps({
            "version": 1, "key": supervision.DESIRED_KEY,
            "state": "running",
            "at_utc": "2026-10-03T00:00:00+00:00"}), encoding="utf-8")
        self.assertIsNone(supervision.read_desired_state(self.desired_path))

    def test_failed_stopped_write_removes_the_old_marker(self):
        self.write_desired()
        blocker = self.desired_path + ".%d.tmp" % os.getpid()
        os.mkdir(blocker)
        try:
            written = supervision.write_desired_state(
                "stopped", source="test", path=self.desired_path,
                boot_id=BOOT)
        finally:
            os.rmdir(blocker)
        self.assertFalse(written)
        self.assertFalse(os.path.exists(self.desired_path))
        self.assertIsNone(supervision.read_desired_state(self.desired_path))

    def test_missing_boot_identity_writes_nothing(self):
        self.assertFalse(supervision.write_desired_state(
            "running", source="test", path=self.desired_path, boot_id=""))
        self.assertFalse(os.path.exists(self.desired_path))
        self.write_desired()
        self.assertFalse(supervision.write_desired_state(
            "stopped", source="test", path=self.desired_path, boot_id=""))
        self.assertFalse(os.path.exists(self.desired_path))

    def test_invalid_state_raises(self):
        with self.assertRaises(ValueError):
            supervision.write_desired_state(
                "maybe", source="test", path=self.desired_path,
                boot_id=BOOT)

    def test_foreign_path_is_refused(self):
        with self.assertRaises(ValueError):
            supervision.read_desired_state(
                os.path.join(self.root, "other-name.json"))
        with self.assertRaises(ValueError):
            supervision.write_desired_state(
                "running", source="test",
                path=os.path.join(self.root, "..", "escape.json"),
                boot_id=BOOT)


class WatchStateTests(BaseSupervisionTest):
    def test_missing_and_corrupt_are_unreadable(self):
        state = supervision.read_watch_state(self.state_path)
        self.assertTrue(state["unreadable"])
        self.assertEqual(state["reason"], "missing")
        Path(self.state_path).write_text("{broken", encoding="utf-8")
        state = supervision.read_watch_state(self.state_path)
        self.assertTrue(state["unreadable"])
        self.assertEqual(state["reason"], "malformed")
        Path(self.state_path).write_text(
            json.dumps({"version": 99, "boot_id": BOOT}),
            encoding="utf-8")
        state = supervision.read_watch_state(self.state_path)
        self.assertTrue(state["unreadable"])
        self.assertEqual(state["reason"], "version_mismatch")

    def test_round_trip_keeps_the_budget(self):
        self.write_watch_state(2, ["2026-10-03T08:59:00+00:00"])
        state = supervision.read_watch_state(self.state_path)
        self.assertFalse(state["unreadable"])
        self.assertEqual(state["consecutive_failures"], 2)
        self.assertEqual(state["restart_attempts_utc"],
                         ["2026-10-03T08:59:00+00:00"])

    def test_supervisor_restart_same_boot_keeps_the_quota(self):
        # A supervisor restart must not clear the consumed budget: the
        # readable state of the SAME boot is reused as-is.
        attempts = [supervision._utc_iso(900.0), supervision._utc_iso(950.0),
                    supervision._utc_iso(960.0)]
        self.write_watch_state(2, attempts)
        check = self.candidate(failures=2, attempts=attempts)
        self.assertFalse(check["ok"])
        self.assertEqual(check["reason"], "budget_exhausted")
        self.assertEqual(check["attempts_this_hour"], 3)
        record = supervision.run_once(
            apply=True, now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, lock_dir=self.lock_dir,
            log_path=self.service_log)
        self.assertEqual(record["code"], "budget_exhausted")
        final = supervision.read_watch_state(self.state_path)
        self.assertEqual(len(final["restart_attempts_utc"]), 3)
        self.assertEqual(self.ops.starts, 0)

    def test_boot_change_resets_failure_and_budget(self):
        self.write_desired()
        self.write_abrupt_evidence()
        self.write_watch_state(1, [], boot="older-boot")
        self.ops.pid_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": True,
            "pid": 4242, "pid_matches": False, "port_listening": False,
            "state": "pid_stale", "identity": "unknown",
            "identity_reason": "identity_mismatch"}
        self.ops.alive = False
        check = supervision.check_preconditions(
            now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)
        self.assertFalse(check["ok"])
        self.assertEqual(check["reason"], "awaiting_confirmation")
        self.assertEqual(check["confirmations"], 1)


class ExitEvidenceTests(BaseSupervisionTest):
    def test_graceful_service_log(self):
        Path(self.service_log).write_text(
            "2026-10-03 09:00:00,000 INFO listening host=127.0.0.1 "
            "port=%d pid=4242\n" % supervision.BACKEND_PORT +
            "2026-10-03 09:01:00,000 INFO exit begin port=%d "
            "pid=4242 reason=admin_shutdown\n" % supervision.BACKEND_PORT,
            encoding="utf-8")
        evidence = supervision.read_exit_evidence(log_path=self.service_log)
        self.assertEqual(evidence["state"], "graceful")
        self.assertEqual(evidence["source"], "service_log")
        self.assertEqual(evidence["last_pid"], 4242)
        self.assertIsNotNone(evidence["graceful_at"])

    def test_abrupt_parent_log(self):
        Path(self.service_log).write_text(
            "2026-10-03 09:00:00,000 INFO listening host=127.0.0.1 "
            "port=%d pid=4242\n" % supervision.BACKEND_PORT,
            encoding="utf-8")
        Path(self.lifecycle_log).write_text(
            "2026-10-03 09:02:00 force stop pid=4242 port=%d "
            "by=taskkill result=dead\n" % supervision.BACKEND_PORT,
            encoding="utf-8")
        evidence = supervision.read_exit_evidence(log_path=self.service_log)
        self.assertEqual(evidence["state"], "abrupt")
        self.assertEqual(evidence["source"], "parent_log")

    def test_no_records_is_unknown(self):
        evidence = supervision.read_exit_evidence(log_path=self.service_log)
        self.assertEqual(evidence["state"], "unknown")
        self.assertEqual(evidence["source"], "none")
        self.assertIsNone(evidence["last_pid"])

    def test_unconfirmed_console_stop_is_not_folded(self):
        # "stop requested ... by=console" is an in-flight request, not a
        # terminal record: it stays unknown, never graceful/abrupt.
        Path(self.lifecycle_log).write_text(
            "2026-10-03 09:02:00 stop requested pid=4242 port=%d "
            "by=console\n" % supervision.BACKEND_PORT, encoding="utf-8")
        evidence = supervision.read_exit_evidence(log_path=self.service_log)
        self.assertEqual(evidence["state"], "unknown")

    def test_foreign_log_name_is_unresolvable_not_read(self):
        # A foreign file name can never make the reader open another file:
        # the evidence is honestly unresolvable, never a fabricated read.
        evidence = supervision.read_exit_evidence(
            log_path=os.path.join(self.root, "other.log"))
        self.assertEqual(evidence["state"], "unknown")
        self.assertEqual(evidence["reason"], "log_path_unresolvable")


class ObserveTests(BaseSupervisionTest):
    def test_absent_instance_is_confirmed_gone(self):
        observation = supervision.observe_share_backend(ops=self.ops)
        self.assertIs(observation["alive"], False)
        self.assertEqual(observation["port"]["occupant"], "known")

    def test_pid_stale_uses_the_liveness_query(self):
        self.ops.pid_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": True,
            "pid": 4242, "pid_matches": False, "port_listening": False,
            "state": "pid_stale", "identity": "unknown",
            "identity_reason": "identity_mismatch"}
        self.ops.alive = False
        observation = supervision.observe_share_backend(ops=self.ops)
        self.assertIs(observation["alive"], False)
        self.ops.alive = True
        observation = supervision.observe_share_backend(ops=self.ops)
        self.assertIsNone(observation["alive"])
        self.assertEqual(observation["identity"], "unknown")

    def test_unknown_port_occupant(self):
        self.ops.pid_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": False,
            "pid": None, "pid_matches": None, "port_listening": True,
            "state": "pid_file_absent", "identity": "unknown",
            "identity_reason": "pid_file_absent"}
        observation = supervision.observe_share_backend(ops=self.ops)
        self.assertIsNone(observation["alive"])
        self.assertEqual(observation["port"]["occupant"], "unknown")

    def test_live_verified_instance(self):
        self.ops.pid_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": True,
            "pid": 4242, "pid_matches": True, "port_listening": True,
            "state": "running", "identity": "verified",
            "identity_reason": None}
        observation = supervision.observe_share_backend(ops=self.ops)
        self.assertIs(observation["alive"], True)
        self.assertEqual(observation["identity"], "verified")


class PreconditionTests(BaseSupervisionTest):
    def test_desired_unknown_refuses(self):
        self.write_watch_state(2)
        check = supervision.check_preconditions(
            now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)
        self.assertFalse(check["ok"])
        self.assertEqual(check["reason"], "desired_state_unknown")

    def test_desired_stopped_refuses(self):
        self.write_desired(state="stopped")
        check = supervision.check_preconditions(
            now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)
        self.assertEqual(check["reason"], "desired_stopped")

    def test_previous_boot_refuses(self):
        self.write_desired(boot="older-boot")
        check = supervision.check_preconditions(
            now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)
        self.assertEqual(check["reason"], "desired_state_from_previous_boot")

    def test_unreadable_boot_identity_refuses(self):
        self.write_desired()
        supervision._BOOT_ID_READER = lambda: None
        check = supervision.check_preconditions(
            now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)
        self.assertEqual(check["reason"], "boot_identity_unavailable")

    def test_broker_unavailable_refuses(self):
        self.write_desired()
        self.broker.set_status(False, {})
        check = supervision.check_preconditions(
            now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)
        self.assertEqual(check["reason"], "broker_unavailable")

    def test_live_lease_refuses(self):
        self.write_desired()
        self.broker.set_status(True, {
            "checked_at": 1000,
            "lease": {"lease_id": "a" * 32, "component": "share_gateway",
                       "expires_at": 2000},
            "lease_expired": False, "instance": None, "last_outcome": None})
        check = supervision.check_preconditions(
            now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)
        self.assertEqual(check["reason"], "maintenance_in_force")
        self.assertEqual(self.broker.end_calls, [])

    def test_expired_lease_refuses_read_only_without_ending_it(self):
        """A read-only check must NOT perform the maintenance-end write."""
        self.write_desired()
        self.broker.set_status(True, {
            "checked_at": 1000,
            "lease": {"lease_id": "a" * 32, "component": "share_gateway",
                       "expires_at": 999},
            "lease_expired": True, "instance": None, "last_outcome": None})
        check = supervision.check_preconditions(
            now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)
        self.assertEqual(check["reason"], "maintenance_lease_expired")
        # No maintenance-end from the read-only view (contract §7.3).
        self.assertEqual(self.broker.end_calls, [])
        self.assertEqual(check["expired_lease"],
                         {"component": "share_gateway", "lease_id": "a" * 32})

    def test_apply_ends_an_expired_lease_once_and_refuses_if_uncleared(self):
        """The ONE maintenance-end runs in APPLY mode, inside the lock."""
        self.write_desired()
        self.write_watch_state(2)
        self.broker.set_status(True, {
            "checked_at": 1000,
            "lease": {"lease_id": "a" * 32, "component": "share_gateway",
                       "expires_at": 999},
            "lease_expired": True, "instance": None, "last_outcome": None})
        record = supervision.run_once(
            apply=True, now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, lock_dir=self.lock_dir,
            log_path=self.service_log)
        self.assertEqual(self.broker.end_calls,
                         [("share_gateway", "a" * 32)])
        self.assertEqual(record["action"], "none")
        self.assertEqual(record["code"], "maintenance_in_force")
        self.assertEqual(self.ops.starts, 0)

    def test_apply_ends_an_expired_lease_and_starts_when_cleared(self):
        """After the ONE in-lock end clears the lease, the start may run."""
        broker = _ClearingBroker()
        broker.set_status(True, {
            "checked_at": 1000,
            "lease": {"lease_id": "a" * 32, "component": "share_gateway",
                       "expires_at": 999},
            "lease_expired": True, "instance": None, "last_outcome": None})
        self.candidate()
        record = supervision.run_once(
            apply=True, now=1000.0, ops=self.ops, broker=broker,
            state_dir=self.state_dir, lock_dir=self.lock_dir,
            log_path=self.service_log)
        self.assertEqual(broker.end_calls, [("share_gateway", "a" * 32)])
        self.assertEqual(record["action"], "start")
        self.assertEqual(record["code"], "started")
        self.assertEqual(self.ops.starts, 1)

    def test_expired_lease_refusal_keeps_another_executors_attempt(self):
        """The in-lock state must be the LATEST complete one (R-BUDGET-LEASE)."""
        self.write_desired()
        self.write_watch_state(2)
        self.broker.set_status(True, {
            "checked_at": 1000,
            "lease": {"lease_id": "a" * 32, "component": "share_gateway",
                       "expires_at": 999},
            "lease_expired": True, "instance": None, "last_outcome": None})
        state_path = self.state_path
        original_lock = supervision.ResourceFileLock

        class _InjectingLock:
            def __init__(self, *args, **kwargs):
                self._inner = original_lock(*args, **kwargs)

            def acquire(self):
                # Another executor consumed one attempt after our outside
                # read and before we took the lock.
                state = supervision.read_watch_state(state_path)
                state["restart_attempts_utc"] = [supervision._utc_iso(990.0)]
                supervision.write_watch_state(state, state_path)
                return self._inner.acquire()

            def __exit__(self, *args):
                return self._inner.__exit__(*args)

        supervision.ResourceFileLock = _InjectingLock
        try:
            record = supervision.run_once(
                apply=True, now=1000.0, ops=self.ops, broker=self.broker,
                state_dir=self.state_dir, lock_dir=self.lock_dir,
                log_path=self.service_log)
        finally:
            supervision.ResourceFileLock = original_lock
        # maintenance-end clears nothing: the refusal still keeps the budget.
        self.assertEqual(record["code"], "maintenance_in_force")
        self.assertEqual(self.ops.starts, 0)
        state = supervision.read_watch_state(self.state_path)
        self.assertEqual(len(state["restart_attempts_utc"]), 1)

    def test_broker_unreadable_after_end_keeps_another_executors_attempt(self):
        """The read-back failure after end must keep the budget too."""
        self.write_desired()
        self.write_watch_state(2)
        self.broker.set_status(True, {
            "checked_at": 1000,
            "lease": {"lease_id": "a" * 32, "component": "share_gateway",
                       "expires_at": 999},
            "lease_expired": True, "instance": None, "last_outcome": None})
        calls = {"n": 0}
        original_status = self.broker.maintenance_status

        def flaky():
            calls["n"] += 1
            if calls["n"] >= 3:
                # The read-back after maintenance-end fails.
                return _StubResult(False, None, "unreachable")
            return original_status()

        self.broker.maintenance_status = flaky
        state_path = self.state_path
        original_lock = supervision.ResourceFileLock

        class _InjectingLock:
            def __init__(self, *args, **kwargs):
                self._inner = original_lock(*args, **kwargs)

            def acquire(self):
                state = supervision.read_watch_state(state_path)
                state["restart_attempts_utc"] = [supervision._utc_iso(990.0)]
                supervision.write_watch_state(state, state_path)
                return self._inner.acquire()

            def __exit__(self, *args):
                return self._inner.__exit__(*args)

        supervision.ResourceFileLock = _InjectingLock
        try:
            record = supervision.run_once(
                apply=True, now=1000.0, ops=self.ops, broker=self.broker,
                state_dir=self.state_dir, lock_dir=self.lock_dir,
                log_path=self.service_log)
        finally:
            supervision.ResourceFileLock = original_lock
        self.assertEqual(record["code"], "broker_unavailable")
        self.assertEqual(self.ops.starts, 0)
        state = supervision.read_watch_state(self.state_path)
        self.assertEqual(len(state["restart_attempts_utc"]), 1)

    def test_lock_busy_never_drops_another_executors_attempt(self):
        """A busy lock means no authority: the budget must survive."""
        self.candidate()
        state_path = self.state_path
        original = supervision.ResourceFileLock

        class _BusyLock:
            def __init__(self, *args, **kwargs):
                pass

            def acquire(self):
                # Another executor consumed one attempt after our pre-lock read
                # and before our lock attempt.
                state = supervision.read_watch_state(state_path)
                state["restart_attempts_utc"] = [supervision._utc_iso(999.0)]
                supervision.write_watch_state(state, state_path)
                raise supervision.LockBusy("busy")

        supervision.ResourceFileLock = _BusyLock
        try:
            record = supervision.run_once(
                apply=True, now=1000.0, ops=self.ops, broker=self.broker,
                state_dir=self.state_dir, lock_dir=self.lock_dir,
                log_path=self.service_log)
        finally:
            supervision.ResourceFileLock = original
        self.assertEqual(record["code"], "resource_busy")
        self.assertEqual(self.ops.starts, 0)
        state = supervision.read_watch_state(self.state_path)
        self.assertEqual(len(state["restart_attempts_utc"]), 1)

    def test_apply_consumes_the_budget_from_the_in_lock_recheck(self):
        """A stale pre-lock snapshot must not lose a concurrent attempt."""
        self.candidate()
        original = supervision.check_preconditions
        calls = {"n": 0}

        def counting(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                # Another supervisor consumed one attempt after our pre-lock
                # read and before the in-lock re-check.
                state = supervision.read_watch_state(self.state_path)
                state["restart_attempts_utc"] = [supervision._utc_iso(999.0)]
                supervision.write_watch_state(state, self.state_path)
            return original(*args, **kwargs)

        supervision.check_preconditions = counting
        try:
            record = supervision.run_once(
                apply=True, now=1000.0, ops=self.ops, broker=self.broker,
                state_dir=self.state_dir, lock_dir=self.lock_dir,
                log_path=self.service_log)
        finally:
            supervision.check_preconditions = original
        self.assertEqual(record["code"], "started")
        state = supervision.read_watch_state(self.state_path)
        self.assertEqual(len(state["restart_attempts_utc"]), 2)

    def test_managed_stop_refuses(self):
        self.write_desired()
        supervision._DESIRED_STOP_READER = lambda: {
            "state": "stopped", "stopped_at": 1}
        check = supervision.check_preconditions(
            now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)
        self.assertEqual(check["reason"], "managed_stop_in_force")

    def test_running_lifecycle_record_does_not_block(self):
        # C2：显式受管开启成功后 broker lifecycle 记为 running，
        # 监督判据不再以 managed_stop_in_force 拦下——只有 stopped 记录才拦
        # （人工停止/紧急停止的自动恢复禁令不被本修复削弱）。
        self.write_desired(at_utc="2030-01-01T00:00:00+00:00")
        supervision._DESIRED_STOP_READER = lambda: {
            "state": "running", "stopped_at": None}
        Path(self.service_log).write_text(
            "2026-10-03 09:00:00,000 INFO listening host=127.0.0.1 "
            "port=%d pid=4242\n" % supervision.BACKEND_PORT +
            "2026-10-03 09:01:00,000 INFO exit begin port=%d "
            "pid=4242 reason=admin_shutdown\n" % supervision.BACKEND_PORT,
            encoding="utf-8")
        self.write_watch_state(2)
        self.ops.pid_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": False,
            "pid": None, "pid_matches": None, "port_listening": False,
            "state": "pid_file_absent", "identity": "unknown",
            "identity_reason": "pid_file_absent"}
        check = supervision.check_preconditions(
            now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)
        self.assertTrue(check["ok"], check)
        self.assertNotEqual(check.get("reason"), "managed_stop_in_force")
        self.assertEqual(check["instance_record"]["state"], "running")

    def test_unknown_lifecycle_state_is_never_a_stop(self):
        # 未知 lifecycle 形状不得被当成 stopped：读取方返回 None（无权威记录）
        # 时监督按「无记录」处理，绝不凭读不到就把 running 变 stopped。
        self.write_desired(at_utc="2030-01-01T00:00:00+00:00")
        supervision._DESIRED_STOP_READER = lambda: None
        Path(self.service_log).write_text(
            "2026-10-03 09:00:00,000 INFO listening host=127.0.0.1 "
            "port=%d pid=4242\n" % supervision.BACKEND_PORT +
            "2026-10-03 09:01:00,000 INFO exit begin port=%d "
            "pid=4242 reason=admin_shutdown\n" % supervision.BACKEND_PORT,
            encoding="utf-8")
        self.write_watch_state(2)
        self.ops.pid_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": False,
            "pid": None, "pid_matches": None, "port_listening": False,
            "state": "pid_file_absent", "identity": "unknown",
            "identity_reason": "pid_file_absent"}
        check = supervision.check_preconditions(
            now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)
        self.assertTrue(check["ok"], check)
        self.assertIsNone(check["instance_record"])

    def test_graceful_exit_newer_than_intent_refuses(self):
        self.write_desired(at_utc="2020-01-01T00:00:00+00:00")
        Path(self.service_log).write_text(
            "2026-10-03 09:00:00,000 INFO listening host=127.0.0.1 "
            "port=%d pid=4242\n" % supervision.BACKEND_PORT +
            "2026-10-03 09:01:00,000 INFO exit begin port=%d "
            "pid=4242 reason=admin_shutdown\n" % supervision.BACKEND_PORT,
            encoding="utf-8")
        check = supervision.check_preconditions(
            now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)
        self.assertEqual(check["reason"],
                         "graceful_shutdown_outside_runtime")

    def test_graceful_exit_older_than_intent_is_a_candidate(self):
        self.write_desired(at_utc="2030-01-01T00:00:00+00:00")
        Path(self.service_log).write_text(
            "2026-10-03 09:00:00,000 INFO listening host=127.0.0.1 "
            "port=%d pid=4242\n" % supervision.BACKEND_PORT +
            "2026-10-03 09:01:00,000 INFO exit begin port=%d "
            "pid=4242 reason=admin_shutdown\n" % supervision.BACKEND_PORT,
            encoding="utf-8")
        self.write_watch_state(2)
        self.ops.pid_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": False,
            "pid": None, "pid_matches": None, "port_listening": False,
            "state": "pid_file_absent", "identity": "unknown",
            "identity_reason": "pid_file_absent"}
        check = supervision.check_preconditions(
            now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)
        self.assertTrue(check["ok"])

    def test_unknown_exit_evidence_refuses(self):
        # The instance is confirmed gone but NO exit record exists: the
        # evidence is unresolvable, never read as a crash.
        self.write_desired()
        self.write_watch_state(2)
        self.ops.pid_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": True,
            "pid": 4242, "pid_matches": False, "port_listening": False,
            "state": "pid_stale", "identity": "unknown",
            "identity_reason": "identity_mismatch"}
        self.ops.alive = False
        check = supervision.check_preconditions(
            now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)
        self.assertEqual(check["reason"], "exit_evidence_unknown")
        self.assertEqual(check["instance"]["alive"], False)

    def test_instance_alive_refuses(self):
        self.write_desired()
        self.write_watch_state(2)
        self.ops.pid_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": True,
            "pid": 4242, "pid_matches": True, "port_listening": True,
            "state": "running", "identity": "verified",
            "identity_reason": None}
        check = supervision.check_preconditions(
            now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)
        self.assertEqual(check["reason"], "instance_alive")

    def test_instance_identity_unknown_refuses(self):
        self.write_desired()
        self.write_watch_state(2)
        self.ops.pid_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": True,
            "pid": 4242, "pid_matches": True, "port_listening": True,
            "state": "running", "identity": "reduced",
            "identity_reason": "instance_record_absent"}
        check = supervision.check_preconditions(
            now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)
        self.assertEqual(check["reason"], "instance_identity_unknown")

    def test_port_occupied_unknown_refuses(self):
        self.write_desired()
        self.write_watch_state(2)
        self.ops.pid_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": False,
            "pid": None, "pid_matches": None, "port_listening": True,
            "state": "pid_file_absent", "identity": "unknown",
            "identity_reason": "pid_file_absent"}
        check = supervision.check_preconditions(
            now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)
        self.assertEqual(check["reason"], "port_occupied_unknown")

    def test_candidate_when_confirmed_absent(self):
        check = self.candidate()
        self.assertTrue(check["ok"])
        self.assertEqual(check["reason"], "instance_absent")
        self.assertEqual(check["confirmations"], 2)


class LocalTimeEvidenceTests(BaseSupervisionTest):
    """The evidence logs write naive LOCAL wall-clock stamps
    (chat2api.py:492-493, console_share_production.py:1326) while the intent
    ``at_utc`` is real UTC (``_utc_iso(time.time())``): reading a local
    stamp as UTC shifted every exit by the host's UTC offset, so under UTC+8 a
    graceful exit 2 h BEFORE the intent compared as 6 h AFTER it and
    ``graceful_shutdown_outside_runtime`` refused the crash recovery this
    module exists for. These tests force UTC+8 through the ``_LOCAL_TZ`` seam,
    so the boundary is exercised deterministically on any host.
    """

    def setUp(self):
        super(LocalTimeEvidenceTests, self).setUp()
        self._saved_local_tz = supervision._LOCAL_TZ
        supervision._LOCAL_TZ = datetime.timezone(
            datetime.timedelta(hours=8))

    def tearDown(self):
        supervision._LOCAL_TZ = self._saved_local_tz
        super(LocalTimeEvidenceTests, self).tearDown()

    # -- fixture helpers -------------------------------------------------

    def _local_stamp(self, epoch):
        """``epoch`` formatted the way the logs write it (naive local)."""
        local = datetime.datetime.fromtimestamp(
            epoch, tz=datetime.timezone.utc).astimezone(
            supervision._LOCAL_TZ)
        return local.strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]

    def _write_graceful_service_log(self, exit_epoch):
        Path(self.service_log).write_text(
            "%s INFO listening host=127.0.0.1 port=%d pid=4242\n"
            % (self._local_stamp(exit_epoch - 60.0),
               supervision.BACKEND_PORT) +
            "%s INFO exit begin port=%d pid=4242 reason=admin_shutdown\n"
            % (self._local_stamp(exit_epoch), supervision.BACKEND_PORT),
            encoding="utf-8")

    def _absent_instance(self):
        self.ops.pid_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": False,
            "pid": None, "pid_matches": None, "port_listening": False,
            "state": "pid_file_absent", "identity": "unknown",
            "identity_reason": "pid_file_absent"}
        self.ops.alive = False

    def _check(self, intent_epoch):
        self.write_watch_state(2)
        self._absent_instance()
        return supervision.check_preconditions(
            now=intent_epoch, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)

    # -- the root cause: local stamps map to their REAL epoch -------------

    def test_local_stamp_maps_to_its_real_epoch(self):
        # 09:01 local in UTC+8 is 01:01 UTC, not 09:01 UTC.
        naive = datetime.datetime(2026, 10, 3, 9, 1, 0)
        expected = datetime.datetime(
            2026, 10, 3, 1, 1, 0, tzinfo=datetime.timezone.utc).timestamp()
        self.assertEqual(supervision._log_local_epoch(naive), expected)

    def test_listening_record_with_downstream_key_suffix_still_parses(self):
        # R32-KEY-AUTH: chat2api.py:6419-6421 appends downstream_key=... to
        # the listening line; the anchored parser must still bind the pid and
        # keep reading the same graceful-exit evidence.
        intent_epoch = 1_800_000_000.0
        self.write_desired(at_utc=supervision._utc_iso(intent_epoch))
        Path(self.service_log).write_text(
            "%s INFO listening host=127.0.0.1 port=%d pid=4242 "
            "downstream_key=not_required\n"
            % (self._local_stamp(intent_epoch - 60.0),
               supervision.BACKEND_PORT) +
            "%s INFO exit begin port=%d pid=4242 reason=admin_shutdown\n"
            % (self._local_stamp(intent_epoch), supervision.BACKEND_PORT),
            encoding="utf-8")
        evidence = supervision.read_exit_evidence(log_path=self.service_log)
        self.assertEqual(evidence["state"], "graceful")
        self.assertAlmostEqual(evidence["graceful_at"],
                               intent_epoch, delta=1.0)
        check = self._check(intent_epoch)
        self.assertTrue(check["ok"])
        self.assertEqual(check["reason"], "instance_absent")

    def test_ambiguous_wall_clock_never_reads_older_than_reality(self):
        # A fall-back rule makes 02:30 ambiguous (01:30 and 00:30 UTC); the
        # conservative reading is the LATER epoch, so evidence can never look
        # older than it could really be (a human stop is never auto-undone).
        class _FallBack(datetime.tzinfo):
            def utcoffset(self, dt):
                return datetime.timedelta(hours=1 if dt.fold else 2)

            def dst(self, dt):
                return datetime.timedelta(0)

        saved = supervision._LOCAL_TZ
        supervision._LOCAL_TZ = _FallBack()
        try:
            naive = datetime.datetime(2026, 10, 25, 2, 30, 0)
            later = datetime.datetime(
                2026, 10, 25, 1, 30, 0,
                tzinfo=datetime.timezone.utc).timestamp()
            self.assertEqual(supervision._log_local_epoch(naive), later)
        finally:
            supervision._LOCAL_TZ = saved

    # -- the real boundary: 2 h before the intent is a candidate ---------

    def test_graceful_exit_two_hours_before_intent_is_a_candidate(self):
        intent_epoch = 1_800_000_000.0
        self.write_desired(at_utc=supervision._utc_iso(intent_epoch))
        self._write_graceful_service_log(intent_epoch - 2 * 3600.0)
        evidence = supervision.read_exit_evidence(log_path=self.service_log)
        self.assertEqual(evidence["state"], "graceful")
        self.assertAlmostEqual(evidence["graceful_at"],
                               intent_epoch - 2 * 3600.0, delta=1.0)
        check = self._check(intent_epoch)
        self.assertTrue(check["ok"])
        self.assertEqual(check["reason"], "instance_absent")

    def test_graceful_exit_after_intent_refuses(self):
        intent_epoch = 1_800_000_000.0
        self.write_desired(at_utc=supervision._utc_iso(intent_epoch))
        self._write_graceful_service_log(intent_epoch + 2 * 3600.0)
        check = self._check(intent_epoch)
        self.assertFalse(check["ok"])
        self.assertEqual(check["reason"],
                         "graceful_shutdown_outside_runtime")

    # -- the tightest boundary: one second on either side ---------------

    def test_graceful_exit_one_second_before_intent_is_a_candidate(self):
        intent_epoch = 1_800_000_000.0
        self.write_desired(at_utc=supervision._utc_iso(intent_epoch))
        self._write_graceful_service_log(intent_epoch - 1.0)
        check = self._check(intent_epoch)
        self.assertTrue(check["ok"])
        self.assertEqual(check["reason"], "instance_absent")

    def test_graceful_exit_one_second_after_intent_refuses(self):
        intent_epoch = 1_800_000_000.0
        self.write_desired(at_utc=supervision._utc_iso(intent_epoch))
        self._write_graceful_service_log(intent_epoch + 1.0)
        check = self._check(intent_epoch)
        self.assertFalse(check["ok"])
        self.assertEqual(check["reason"],
                         "graceful_shutdown_outside_runtime")

    # -- the parent lifecycle log is parsed the same way -----------------

    def test_lifecycle_graceful_stamp_two_hours_before_intent_is_a_candidate(
            self):
        intent_epoch = 1_800_000_000.0
        self.write_desired(at_utc=supervision._utc_iso(intent_epoch))
        Path(self.lifecycle_log).write_text(
            "%s stopped pid=4242 port=%d by=graceful\n"
            % (self._local_stamp(intent_epoch - 2 * 3600.0),
               supervision.BACKEND_PORT), encoding="utf-8")
        evidence = supervision.read_exit_evidence(log_path=self.service_log)
        self.assertEqual(evidence["state"], "graceful")
        self.assertEqual(evidence["source"], "parent_log")
        self.assertAlmostEqual(evidence["graceful_at"],
                               intent_epoch - 2 * 3600.0, delta=1.0)
        check = self._check(intent_epoch)
        self.assertTrue(check["ok"])
        self.assertEqual(check["reason"], "instance_absent")

    def test_lifecycle_graceful_stamp_after_intent_refuses(self):
        intent_epoch = 1_800_000_000.0
        self.write_desired(at_utc=supervision._utc_iso(intent_epoch))
        Path(self.lifecycle_log).write_text(
            "%s stopped pid=4242 port=%d by=graceful\n"
            % (self._local_stamp(intent_epoch + 2 * 3600.0),
               supervision.BACKEND_PORT), encoding="utf-8")
        check = self._check(intent_epoch)
        self.assertFalse(check["ok"])
        self.assertEqual(check["reason"],
                         "graceful_shutdown_outside_runtime")


class ConfirmationAndBudgetTests(BaseSupervisionTest):
    def test_first_observation_awaits_confirmation(self):
        self.write_desired()
        self.write_watch_state(0)
        self.write_abrupt_evidence()
        self.ops.pid_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": True,
            "pid": 4242, "pid_matches": False, "port_listening": False,
            "state": "pid_stale", "identity": "unknown",
            "identity_reason": "identity_mismatch"}
        self.ops.alive = False
        check = supervision.check_preconditions(
            now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)
        self.assertEqual(check["reason"], "awaiting_confirmation")
        self.assertEqual(check["confirmations"], 1)

    def test_budget_exhausted_reports_the_manual_entry(self):
        check = self.candidate(
            failures=2, attempts=[supervision._utc_iso(900.0),
                                   supervision._utc_iso(950.0),
                                   supervision._utc_iso(960.0)])
        self.assertEqual(check["reason"], "budget_exhausted")
        self.assertIn("share-backend-watch.log", check["detail"])

    def test_old_attempts_do_not_count(self):
        self.write_desired()
        self.write_watch_state(2, ["2020-01-01T00:00:00+00:00"])
        self.write_abrupt_evidence()
        self.ops.pid_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": True,
            "pid": 4242, "pid_matches": False, "port_listening": False,
            "state": "pid_stale", "identity": "unknown",
            "identity_reason": "identity_mismatch"}
        self.ops.alive = False
        check = supervision.check_preconditions(
            now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)
        self.assertTrue(check["ok"])
        self.assertEqual(check["attempts_this_hour"], 0)

    def test_unreadable_watch_state_never_folds_into_empty_budget(self):
        # A candidate absent instance, then the damaged state file: the
        # refusal names the state file and never resets the budget.
        self.write_desired()
        self.write_abrupt_evidence()
        self.ops.pid_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": True,
            "pid": 4242, "pid_matches": False, "port_listening": False,
            "state": "pid_stale", "identity": "unknown",
            "identity_reason": "identity_mismatch"}
        self.ops.alive = False
        Path(self.state_path).write_text("{broken", encoding="utf-8")
        check = supervision.check_preconditions(
            now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, log_path=self.service_log)
        self.assertEqual(check["reason"], "watch_state_unreadable")
        record = supervision.run_once(
            apply=True, now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, lock_dir=self.lock_dir,
            log_path=self.service_log)
        self.assertEqual(record["code"], "watch_state_unreadable")
        self.assertEqual(self.ops.starts, 0)
        # The damaged file stays byte-for-byte in place (never reset).
        self.assertEqual(Path(self.state_path).read_text(encoding="utf-8"),
                         "{broken")

    def test_missing_watch_state_bootstraps_the_first_observation(self):
        # A MISSING state file is the bootstrap case, not a damaged one
        # (A9-5: the attempt is recorded before the start, so the FIRST
        # record must be able to create the file). The round records the
        # first candidate observation, writes the fresh state and starts
        # nothing yet.
        self.write_desired()
        self.write_abrupt_evidence()
        self.ops.pid_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": True,
            "pid": 4242, "pid_matches": False, "port_listening": False,
            "state": "pid_stale", "identity": "unknown",
            "identity_reason": "identity_mismatch"}
        self.ops.alive = False
        self.assertFalse(os.path.exists(self.state_path))
        record = supervision.run_once(
            apply=True, now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, lock_dir=self.lock_dir,
            log_path=self.service_log)
        self.assertEqual(record["code"], "awaiting_confirmation")
        self.assertEqual(self.ops.starts, 0)
        state = supervision.read_watch_state(self.state_path)
        self.assertFalse(state["unreadable"])
        self.assertEqual(state["boot_id"], BOOT)
        self.assertEqual(state["consecutive_failures"], 1)
        self.assertEqual(state["restart_attempts_utc"], [])
        self.assertEqual(state["last_decision"]["code"],
                         "awaiting_confirmation")

    def test_bootstrap_then_the_second_round_starts(self):
        # The round after the bootstrap reads the fresh state, reaches the
        # two confirmations and consumes the budget through the shared
        # handler: the recovery machinery works end to end after the
        # bootstrap (the same stubs the other start tests use).
        self.write_desired()
        self.write_abrupt_evidence()
        self.ops.pid_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": True,
            "pid": 4242, "pid_matches": False, "port_listening": False,
            "state": "pid_stale", "identity": "unknown",
            "identity_reason": "identity_mismatch"}
        self.ops.alive = False
        first = supervision.run_once(
            apply=True, now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, lock_dir=self.lock_dir,
            log_path=self.service_log)
        self.assertEqual(first["code"], "awaiting_confirmation")
        second = supervision.run_once(
            apply=True, now=1001.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, lock_dir=self.lock_dir,
            log_path=self.service_log)
        self.assertEqual(second["action"], "start")
        self.assertEqual(second["code"], "started")
        self.assertEqual(self.ops.starts, 1)
        state = supervision.read_watch_state(self.state_path)
        self.assertEqual(len(state["restart_attempts_utc"]), 1)
        self.assertEqual(state["consecutive_failures"], 0)


class RunOnceTests(BaseSupervisionTest):
    def test_check_mode_writes_nothing(self):
        self.write_desired()
        self.write_watch_state(2)
        self.write_abrupt_evidence()
        self.ops.pid_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": True,
            "pid": 4242, "pid_matches": False, "port_listening": False,
            "state": "pid_stale", "identity": "unknown",
            "identity_reason": "identity_mismatch"}
        self.ops.alive = False
        record = supervision.run_once(
            apply=False, now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, lock_dir=self.lock_dir,
            log_path=self.service_log)
        self.assertEqual(record["action"], "start")
        self.assertEqual(record["code"], "would_start")
        self.assertEqual(self.ops.starts, 0)
        self.assertFalse(os.path.exists(self.decision_log))
        self.assertFalse(os.path.exists(self.lock_dir))

    def test_apply_consumes_the_budget_before_start(self):
        self.candidate()
        record = supervision.run_once(
            apply=True, now=1000.0, ops=self.ops, broker=self.broker,
            state_dir=self.state_dir, lock_dir=self.lock_dir,
            log_path=self.service_log)
        self.assertEqual(record["action"], "start")
        self.assertEqual(record["code"], "started")
        self.assertEqual(self.ops.starts, 1)
        # The attempt timestamp was already persisted when start() ran.
        self.assertIsNotNone(self.ops.state_at_start)
        self.assertEqual(
            len(self.ops.state_at_start["restart_attempts_utc"]), 1)
        final = supervision.read_watch_state(self.state_path)
        self.assertEqual(final["consecutive_failures"], 0)
        self.assertEqual(final["last_decision"]["code"], "started")
        self.assertTrue(os.path.exists(self.decision_log))
        line = Path(self.decision_log).read_text(encoding="utf-8").strip()
        self.assertEqual(json.loads(line)["code"], "started")

    def test_held_console_lock_means_busy_and_no_start(self):
        self.candidate()
        held = supervision.ResourceFileLock(
            self.lock_dir, supervision.LOCK_RESOURCE, owner="test",
            timeout_s=supervision.LOCK_TIMEOUT_S)
        held.acquire()
        try:
            record = supervision.run_once(
                apply=True, now=1000.0, ops=self.ops, broker=self.broker,
                state_dir=self.state_dir, lock_dir=self.lock_dir,
                log_path=self.service_log)
        finally:
            held.__exit__(None, None, None)
        self.assertEqual(record["code"], "resource_busy")
        self.assertEqual(self.ops.starts, 0)

    def test_recheck_under_lock_refuses_when_the_instance_returns(self):
        alive_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": True,
            "pid": 4242, "pid_matches": True, "port_listening": True,
            "state": "running", "identity": "verified",
            "identity_reason": None}
        absent_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": True,
            "pid": 4242, "pid_matches": False, "port_listening": False,
            "state": "pid_stale", "identity": "unknown",
            "identity_reason": "identity_mismatch"}
        self.write_desired()
        self.write_watch_state(2)
        self.write_abrupt_evidence()
        ops = _SequencedOps([absent_state, alive_state],
                            state_path=self.state_path)
        ops.alive = False
        record = supervision.run_once(
            apply=True, now=1000.0, ops=ops, broker=self.broker,
            state_dir=self.state_dir, lock_dir=self.lock_dir,
            log_path=self.service_log)
        self.assertEqual(record["code"], "instance_alive")
        self.assertEqual(ops.starts, 0)

    def test_unconfirmed_start_exit_code(self):
        self.candidate()
        self.ops.verify_result = False
        saved_budget = supervision.VERIFY_BUDGET_S
        saved_interval = supervision.VERIFY_POLL_INTERVAL_S
        supervision.VERIFY_BUDGET_S = 0.05
        supervision.VERIFY_POLL_INTERVAL_S = 0.01
        try:
            record = supervision.run_once(
                apply=True, now=1000.0, ops=self.ops, broker=self.broker,
                state_dir=self.state_dir, lock_dir=self.lock_dir,
                log_path=self.service_log)
        finally:
            supervision.VERIFY_BUDGET_S = saved_budget
            supervision.VERIFY_POLL_INTERVAL_S = saved_interval
        self.assertEqual(record["action"], "start")
        self.assertEqual(record["code"], "start_unconfirmed")
        self.assertEqual(supervision._exit_code(record),
                         supervision.EXIT_UNCONFIRMED)
        self.assertEqual(self.ops.starts, 1)

    def test_exit_code_mapping(self):
        self.assertEqual(
            supervision._exit_code({"action": "none",
                                    "code": "instance_alive"}),
            supervision.EXIT_ALIVE)
        self.assertEqual(
            supervision._exit_code({"action": "none",
                                    "code": "awaiting_confirmation"}),
            supervision.EXIT_ALIVE)
        self.assertEqual(
            supervision._exit_code({"action": "start", "code": "started"}),
            supervision.EXIT_ALIVE)
        self.assertEqual(
            supervision._exit_code({"action": "none",
                                    "code": "budget_exhausted"}),
            supervision.EXIT_REFUSED)


class SharedHandlerAndManagerTests(BaseSupervisionTest):
    def test_start_is_the_shared_production_handler(self):
        ops = supervision._default_ops()
        self.assertIs(
            type(ops).start,
            console_share_production._ProductionBackendOps.start)
        self.assertIs(type(ops), console_share_production._ProductionBackendOps)

    def test_no_operation_manager_is_constructed(self):
        import console_operations
        saved = console_operations.OperationManager

        class _ForbiddenManager:
            def __init__(self, *args, **kwargs):
                raise AssertionError(
                    "supervision constructed an OperationManager")

        console_operations.OperationManager = _ForbiddenManager
        try:
            self.candidate()
            record = supervision.run_once(
                apply=True, now=1000.0, ops=self.ops, broker=self.broker,
                state_dir=self.state_dir, lock_dir=self.lock_dir,
                log_path=self.service_log)
        finally:
            console_operations.OperationManager = saved
        self.assertEqual(record["code"], "started")
        self.assertEqual(self.ops.starts, 1)


if __name__ == "__main__":
    unittest.main()
