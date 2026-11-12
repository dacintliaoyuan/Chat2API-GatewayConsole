# -*- coding: utf-8 -*-
"""Offline tests for the public Funnel supervision (S2/S3, A2-A5).

Every test runs against TEMP dirs with an injected clock, an injected sleeper and
terminal seam doubles. A subprocess guard turns any real pwsh/tailscale/sc.exe/
schtasks/tasklist/netstat invocation into a loud failure, so no production path,
network call or process can be reached from this suite.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import share_public_supervision as supervision  # noqa: E402
from console_operations import OperationError  # noqa: E402

BOOT = "test-boot"
URL = "https://share.example.ts.net"

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


class _Clock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += max(0.0, float(seconds))


class _Ops:
    def __init__(self, verdict=True, gateway=True):
        self.verdict = verdict
        self.gateway = gateway

    def host_evidence(self, kind):
        return {"verdict": self.verdict, "kind": kind}

    def verify_running(self):
        return self.gateway


class _Ts:
    def __init__(self):
        self.state = "running"
        self.calls = []
        self.on_stop = None
        self.on_start = None

    def control(self, action):
        self.calls.append(action)
        if action == "stop":
            self.state = "stopped"
            if self.on_stop is not None:
                self.on_stop()
        else:
            self.state = "running"
            if self.on_start is not None:
                self.on_start()
        return {"ok": True, "detail": "stub"}

    def status(self):
        return self.state


def _entry(overall, index=0):
    state = "ok" if overall == "ok" else "failed"
    return {
        "host": "share.example.ts.net", "ip": "203.0.113.%d" % (10 + index),
        "state": state, "dns": "ok",
        "tls": "ok" if state == "ok" else "failed",
        "certificate": "ok" if state == "ok" else "not_checked",
        "reason": "ok" if state == "ok" else "tls_handshake_failed",
        "elapsed_ms": 1, "checked_at_epoch": 1000.0,
    }


def _dns_source(source, state, *, addresses=(), ttl=None, negative=None):
    return {
        "source": source, "state": state,
        "status": 0 if state != "timeout" else None,
        "classification": state, "addresses": list(addresses),
        "addresses_truncated": False, "ttl_s": ttl,
        "negative_ttl_s": negative, "checked_at_epoch": 1000.0,
        "elapsed_ms": 1,
    }


def _dns(state="ok", *, addresses=None, ttl=None, negative=None, sources=None):
    return {
        "version": 1, "checked_at_utc": "2026-10-24T00:00:00+00:00",
        "checked_at_epoch": 1000.0, "state": state, "reason": state,
        "addresses": list(addresses or []), "addresses_truncated": False,
        "ttl_s": ttl, "negative_ttl_s": negative,
        "sources": list(sources or []),
    }


def _probe_result(overall, url=URL, dns=None, reason=None):
    if overall == "degraded":
        entries = [_entry("ok", 0), _entry("failed", 1)]
    elif overall == "ok":
        entries = [_entry("ok", 0)]
    elif overall == "all_failed":
        entries = [_entry("failed", 0)]
    else:
        entries = []
    return {
        "version": 1, "checked_at_utc": "2026-10-24T00:00:00+00:00",
        "checked_at_epoch": 1000.0, "public_url": url,
        "host": "share.example.ts.net", "overall": overall,
        "reason": reason or {"ok": "ok", "degraded": "partial_failure",
                   "all_failed": "all_entries_failed"}.get(
                       overall, "probe_failed"),
        "deadline_s": 20.0, "elapsed_ms": 1, "entries": entries,
        "dns": dns,
    }


class _ProbeSequence:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        index = min(len(self.calls) - 1, len(self.values) - 1)
        return _probe_result(self.values[index], url)


class BasePublicSupervisionTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(
            prefix="SelfUseAI-share-public-supervision-test-")
        self.state_dir = os.path.join(self.root, "state")
        self.lock_dir = os.path.join(self.root, "locks")
        os.makedirs(self.state_dir, exist_ok=True)
        self.paths = supervision._paths(self.state_dir)
        self.clock = _Clock()
        self.desired = {
            "state": "running", "boot_id": BOOT,
            "at_utc": "2026-10-24T00:00:00+00:00", "source": "test"}
        self.desired_stop = None
        self.maintenance = lambda: {"lease": None, "lease_expired": False}
        self.backend = lambda forget_stale=True: {
            "running": True, "auth_state": "valid"}
        self.ops = _Ops()
        self.ts = _Ts()
        self.funnel_calls = []
        self.notify_calls = []
        self.notify_ok = True
        self.saved = (
            supervision._BOOT_ID_READER, supervision._DESIRED_READER,
            supervision._DESIRED_STOP_READER, supervision._WALL_CLOCK,
            supervision._NOTIFY, supervision.LOCK_TIMEOUT_S)
        supervision._BOOT_ID_READER = lambda: BOOT
        supervision._DESIRED_READER = lambda: self.desired
        supervision._DESIRED_STOP_READER = lambda: self.desired_stop
        supervision._WALL_CLOCK = None
        supervision._NOTIFY = self._notify
        supervision.LOCK_TIMEOUT_S = 0.2

    def tearDown(self):
        (supervision._BOOT_ID_READER, supervision._DESIRED_READER,
         supervision._DESIRED_STOP_READER, supervision._WALL_CLOCK,
         supervision._NOTIFY, supervision.LOCK_TIMEOUT_S) = self.saved
        shutil.rmtree(self.root, ignore_errors=True)

    def _notify(self, code):
        self.notify_calls.append(code)
        return self.notify_ok

    def _funnel(self, action):
        self.funnel_calls.append(action)
        return {"ok": True, "detail": "stub"}

    def seams(self, **overrides):
        seams = {
            "_MAINTENANCE_STATUS": self.maintenance,
            "_BROKER_OPS": self.ops,
            "_CONNECTIVITY_OK": lambda: True,
            "_CONNECTIVITY_STATE": lambda: "Running",
            "_BACKEND_OBSERVE": self.backend,
            "_FUNNEL_CONTROL": self._funnel,
            "_TS_SERVICE_CONTROL": self.ts.control,
            "_TS_SERVICE_STATUS": self.ts.status,
        }
        seams.update(overrides)
        return seams

    def cycle(self, probes, *, now=1000.0, apply=True, manual=False,
              seams=None, sleeper=None, monotonic=None, clock=None):
        sequence = probes if callable(probes) else _ProbeSequence(probes)
        previous_clock = supervision._WALL_CLOCK
        if clock is not None:
            supervision._WALL_CLOCK = clock
        try:
            result = supervision.run_cycle(
                apply=apply, manual=manual, now=now,
                state_dir=self.state_dir, lock_dir=self.lock_dir,
                seams=self.seams() if seams is None else seams,
                probe=sequence, url_provider=lambda: URL,
                sleeper=sleeper or self.clock.sleep,
                monotonic=monotonic or self.clock.monotonic)
        finally:
            supervision._WALL_CLOCK = previous_clock
        return result, sequence

    def read_budget(self, now=1000.0):
        return supervision._budget_view(self.paths, now)

    def write_state(self, observed=900.0, overall="ok", recovery=None,
                    reason="ok", entries=None, dns=None):
        record = {
            "version": 1, "boot_id": BOOT,
            "observed_at_utc": supervision._utc_iso(observed),
            "observed_at_epoch": observed,
            "intent": {"state": "running",
                       "at_utc": self.desired["at_utc"],
                       "boot_id": BOOT, "source": "test"},
            "public_url": URL, "host": "share.example.ts.net",
            "round": {"id": "prior-round", "phase": "normal",
                      "started_at_utc": supervision._utc_iso(observed),
                      "finished_at_utc": supervision._utc_iso(observed),
                      "confirmation": 2,
                      "next_check_at_utc": None,
                      "next_action_at_utc": None},
            "overall": overall, "reason": reason,
            "entries": entries if entries is not None
            else [_entry(overall if overall == "ok" else "failed")],
            "dns": dns,
            "local": supervision._local_default(),
            "recovery": recovery or supervision._idle_recovery(),
            "budget": supervision._unreadable_budget("budget_unreadable"),
            "cancel": {"cancelled": False, "reason": None},
            "notification": {"last_key": None, "last_at_utc": None,
                             "last_result": None},
            "state_file": supervision.STATE_FILE_NAME,
            "event_file": supervision.EVENT_FILE_NAME,
            "budget_file": supervision.BUDGET_FILE_NAME,
            "cancel_file": supervision.CANCEL_FILE_NAME,
        }
        Path(self.paths["state"]).write_text(
            json.dumps(record), encoding="utf-8")

    def write_budget(self, mapping=(), restarts=(), service_starts=(),
                     now=1000.0, include_service_starts=True):
        record = {
            "version": 1, "window_s": supervision.BUDGET_WINDOW_S,
            "mapping_attempts_utc": list(mapping),
            "ts_restarts_utc": list(restarts),
            "updated_at_utc": supervision._utc_iso(now),
        }
        if include_service_starts:
            record["service_starts_utc"] = list(service_starts)
        Path(self.paths["budget"]).write_text(
            json.dumps(record), encoding="utf-8")

    def write_event_lines(self, lines):
        Path(self.paths["event"]).write_text(
            "\n".join(lines) + "\n", encoding="utf-8")

    def snapshot(self):
        return supervision.read_status(
            state_dir=self.state_dir, now=1000.0)["snapshot"]


class PathDisciplineTests(BasePublicSupervisionTest):
    def test_state_dir_with_parent_component_is_refused(self):
        with self.assertRaises(ValueError):
            supervision._paths(os.path.join(self.root, "..", "escape"))


class ReadOnlyAndBootstrapTests(BasePublicSupervisionTest):
    def test_check_mode_writes_nothing(self):
        result, sequence = self.cycle(["all_failed"], apply=False)
        self.assertEqual(result["code"], "all_failed")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(len(sequence.calls), 1)
        self.assertFalse(os.path.exists(self.paths["state"]))
        self.assertFalse(os.path.exists(self.paths["budget"]))
        self.assertFalse(os.path.exists(self.paths["event"]))
        self.assertFalse(os.path.exists(self.paths["log"]))

    def test_check_mode_does_not_read_maintenance(self):
        calls = {"count": 0}

        def maintenance():
            calls["count"] += 1
            return {"lease": None, "lease_expired": False}

        result, _ = self.cycle(
            ["ok"], apply=False,
            seams=self.seams(**{"_MAINTENANCE_STATUS": maintenance}))
        self.assertEqual(result["code"], "ok")
        self.assertEqual(calls["count"], 0)

    def test_first_apply_bootstraps_state_and_budget_together(self):
        result, _ = self.cycle(["ok"])
        self.assertEqual(result["code"], "ok")
        self.assertTrue(os.path.exists(self.paths["state"]))
        self.assertTrue(os.path.exists(self.paths["budget"]))
        snapshot = self.snapshot()
        self.assertEqual(snapshot["overall"], "ok")
        self.assertEqual(snapshot["round"]["confirmation"], 0)
        self.assertEqual(self.read_budget()["mapping_used"], 0)

    def test_run_check_bootstraps_state_and_budget(self):
        result = supervision.run_check(
            now=1000.0, state_dir=self.state_dir, lock_dir=self.lock_dir,
            seams=self.seams(), probe=_ProbeSequence(["ok"]),
            url_provider=lambda: URL)
        self.assertEqual(result["code"], "ok")
        self.assertTrue(os.path.exists(self.paths["state"]))
        self.assertTrue(os.path.exists(self.paths["budget"]))
        self.assertFalse(self.funnel_calls)
        self.assertFalse(self.ts.calls)

    def test_intent_previous_boot_does_not_probe(self):
        self.desired = dict(self.desired, boot_id="older-boot")
        result, sequence = self.cycle(["ok"])
        self.assertEqual(result["code"], "intent_previous_boot")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(sequence.calls, [])
        self.assertEqual(self.snapshot()["overall"], "not_checked")

    def test_deadline_exceeded_before_probe(self):
        values = iter([0.0, 1000.0])
        result, sequence = self.cycle(
            ["ok"], monotonic=lambda: next(values, 1000.0))
        self.assertEqual(result["code"], "deadline_exceeded")
        self.assertEqual(sequence.calls, [])


class CancelTests(BasePublicSupervisionTest):
    def test_cancel_before_probe_does_not_probe(self):
        self.assertTrue(supervision.request_cancel(
            "manual_close", state_dir=self.state_dir))
        result, sequence = self.cycle(["ok"])
        self.assertEqual(result["code"], "cancel_requested")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(sequence.calls, [])

    def test_cancel_file_is_idempotent_and_clearable(self):
        self.assertTrue(supervision.request_cancel(
            "manual_estop", state_dir=self.state_dir))
        self.assertTrue(supervision.request_cancel(
            "manual_estop", state_dir=self.state_dir))
        cancel = supervision.read_cancel(state_dir=self.state_dir)
        self.assertTrue(cancel["cancelled"])
        self.assertEqual(cancel["reason"], "manual_estop")
        self.assertTrue(supervision.clear_cancel(state_dir=self.state_dir))
        self.assertFalse(supervision.read_cancel(
            state_dir=self.state_dir)["cancelled"])

    def test_unreadable_cancel_fails_closed(self):
        Path(self.paths["cancel"]).write_text("{broken", encoding="utf-8")
        result, sequence = self.cycle(["ok"])
        self.assertEqual(result["code"], "cancel_unreadable")
        self.assertEqual(sequence.calls, [])

    def test_cancel_during_ten_second_wait_stops_before_second_probe(self):
        state = {"written": False}

        def sleeper(seconds):
            self.clock.sleep(seconds)
            if not state["written"]:
                state["written"] = True
                supervision.request_cancel(
                    "manual_close", state_dir=self.state_dir)

        result, sequence = self.cycle(["all_failed"], sleeper=sleeper)
        self.assertEqual(result["code"], "cancel_requested")
        self.assertEqual(len(sequence.calls), 1)
        self.assertEqual(self.funnel_calls, [])
        snapshot = self.snapshot()
        self.assertEqual(snapshot["overall"], "not_checked")
        self.assertEqual(snapshot["reason"], "cancel_requested")


class IntentBindingTests(BasePublicSupervisionTest):
    def test_new_running_intent_during_mapping_wait_stops_old_recovery(self):
        changed = False

        def sleeper(seconds):
            nonlocal changed
            if self.funnel_calls and not changed:
                changed = True
                self.desired = dict(
                    self.desired, at_utc="2026-10-24T00:01:00+00:00",
                    source="share.open")
            self.clock.sleep(seconds)

        result, _ = self.cycle(["all_failed"], sleeper=sleeper)
        self.assertTrue(changed)
        self.assertEqual(self.funnel_calls, ["on"])
        self.assertEqual(self.ts.calls, [])
        self.assertEqual(result["reason"], "round_superseded")
        self.assertEqual(self.snapshot()["overall"], "not_checked")
        self.assertEqual(self.snapshot()["entries"], [])
        self.assertEqual(self.read_budget()["mapping_used"], 1)
        self.assertEqual(self.read_budget()["ts_restart_used"], 0)
        self.assertNotIn("share_public_recovered", self.notify_calls)

    def test_new_running_intent_during_confirmation_discards_first_failure(self):
        changed = False

        def sleeper(seconds):
            nonlocal changed
            if not changed:
                changed = True
                self.desired = dict(
                    self.desired, at_utc="2026-10-24T00:01:00+00:00")
            self.clock.sleep(seconds)

        result, probes = self.cycle(["all_failed"], sleeper=sleeper)
        self.assertEqual(result["code"], "round_superseded")
        self.assertEqual(len(probes.calls), 1)
        self.assertEqual(self.funnel_calls, [])
        self.assertEqual(self.ts.calls, [])

    def test_new_intent_during_second_failure_does_not_publish_old_failure(self):
        calls = 0

        def probe(url):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.desired = dict(
                    self.desired, at_utc="2026-10-24T00:01:00+00:00")
            return _probe_result("all_failed", url)

        result, _ = self.cycle(probe)
        self.assertEqual(result["code"], "round_superseded")
        self.assertEqual(result["overall"], "not_checked")
        self.assertEqual(self.snapshot()["entries"], [])
        self.assertEqual(self.funnel_calls, [])
        self.assertEqual(self.ts.calls, [])

    def test_close_during_normal_probe_never_reports_or_notifies_recovered(self):
        self.write_state(overall="degraded")
        self.write_budget()

        def probe(url):
            self.assertTrue(supervision.request_cancel(
                "manual_close", state_dir=self.state_dir))
            self.desired = dict(self.desired, state="stopped")
            return _probe_result("ok", url)

        result, _ = self.cycle(probe)
        self.assertEqual(result["code"], "cancel_requested")
        self.assertEqual(result["overall"], "not_checked")
        self.assertEqual(self.snapshot()["overall"], "not_checked")
        self.assertTrue(self.snapshot()["cancel"]["cancelled"])
        self.assertEqual(self.notify_calls, [])

    def test_normal_probe_cannot_be_rebound_to_a_new_running_intent(self):
        def probe(url):
            self.desired["at_utc"] = "2026-10-24T00:01:00+00:00"
            return _probe_result("ok", url)

        result, _ = self.cycle(probe)
        self.assertEqual(result["code"], "round_superseded")
        self.assertEqual(result["overall"], "not_checked")
        self.assertEqual(self.snapshot()["entries"], [])

    def test_changed_public_url_discards_the_completed_probe(self):
        active_url = [URL]

        def probe(url):
            active_url[0] = "https://new.example.ts.net"
            return _probe_result("ok", url)

        result = supervision.run_cycle(
            now=1000.0, state_dir=self.state_dir, lock_dir=self.lock_dir,
            seams=self.seams(), probe=probe,
            url_provider=lambda: active_url[0],
            sleeper=self.clock.sleep, monotonic=self.clock.monotonic)
        self.assertEqual(result["code"], "round_superseded")
        self.assertEqual(result["overall"], "not_checked")
        self.assertEqual(self.snapshot()["entries"], [])

    def test_changed_public_url_after_mapping_never_restarts_ts(self):
        active_url = [URL]

        def sleeper(seconds):
            if self.funnel_calls:
                active_url[0] = "https://new.example.ts.net"
            self.clock.sleep(seconds)

        result = supervision.run_cycle(
            now=1000.0, state_dir=self.state_dir, lock_dir=self.lock_dir,
            seams=self.seams(), probe=_ProbeSequence(["all_failed"]),
            url_provider=lambda: active_url[0], sleeper=sleeper,
            monotonic=self.clock.monotonic)
        self.assertEqual(self.funnel_calls, ["on"])
        self.assertEqual(self.ts.calls, [])
        self.assertEqual(result["reason"], "round_superseded")

    def test_one_click_probe_also_discards_results_after_manual_close(self):
        def probe(url):
            self.assertTrue(supervision.request_cancel(
                "manual_close", state_dir=self.state_dir))
            self.desired = dict(self.desired, state="stopped")
            return _probe_result("ok", url)

        result = supervision.run_check(
            now=1000.0, state_dir=self.state_dir, lock_dir=self.lock_dir,
            seams=self.seams(), probe=probe, url_provider=lambda: URL)
        self.assertEqual(result["code"], "cancel_requested")
        self.assertEqual(result["overall"], "not_checked")
        self.assertEqual(self.snapshot()["entries"], [])

    def test_cancel_before_the_snapshot_lock_prevents_normal_commit(self):
        self.write_state(overall="degraded")
        self.write_budget()
        original = supervision._with_state_lock
        changed = False

        def close_before_lock(lock_dir, fn):
            nonlocal changed
            if not changed:
                changed = True
                supervision._write_cancel(self.paths, "manual_close", 1000.0)
                self.desired = dict(self.desired, state="stopped")
            return original(lock_dir, fn)

        with mock.patch.object(supervision, "_with_state_lock", close_before_lock):
            result, _ = self.cycle(["ok"])
        self.assertEqual(result["code"], "cancel_requested")
        self.assertEqual(result["overall"], "not_checked")
        self.assertEqual(self.notify_calls, [])

    def test_new_intent_before_recovery_commit_never_notifies_recovered(self):
        original = supervision._persist_snapshot
        changed = False

        def change_before_commit(paths, lock_dir, snapshot, **kwargs):
            nonlocal changed
            if not changed and snapshot["recovery"]["state"] == "succeeded":
                changed = True
                self.desired = dict(
                    self.desired, at_utc="2026-10-24T00:01:00+00:00")
            return original(paths, lock_dir, snapshot, **kwargs)

        with mock.patch.object(supervision, "_persist_snapshot", change_before_commit):
            result, _ = self.cycle(["all_failed", "all_failed", "ok"])
        self.assertTrue(changed)
        self.assertEqual(result["code"], "round_superseded")
        self.assertEqual(result["overall"], "not_checked")
        self.assertNotIn("share_public_recovered", self.notify_calls)

    def test_url_changed_during_local_precheck_prevents_first_mapping(self):
        active_url = [URL]

        def observe(forget_stale=True):
            active_url[0] = "https://new.example.ts.net"
            return {"running": True, "auth_state": "valid"}

        result = supervision.run_cycle(
            now=1000.0, state_dir=self.state_dir, lock_dir=self.lock_dir,
            seams=self.seams(_BACKEND_OBSERVE=observe),
            probe=_ProbeSequence(["all_failed"]),
            url_provider=lambda: active_url[0],
            sleeper=self.clock.sleep, monotonic=self.clock.monotonic)
        self.assertEqual(result["code"], "round_superseded")
        self.assertEqual(self.funnel_calls, [])
        self.assertEqual(self.ts.calls, [])

    def test_url_changed_during_ts_stop_gate_never_stops_ts(self):
        active_url = [URL]

        def maintenance():
            if self.funnel_calls and not self.ts.calls:
                active_url[0] = "https://new.example.ts.net"
            return {"lease": None, "lease_expired": False}

        result = supervision.run_cycle(
            now=1000.0, state_dir=self.state_dir, lock_dir=self.lock_dir,
            seams=self.seams(_MAINTENANCE_STATUS=maintenance),
            probe=_ProbeSequence(["all_failed"]),
            url_provider=lambda: active_url[0],
            sleeper=self.clock.sleep, monotonic=self.clock.monotonic)
        self.assertEqual(self.ts.calls, [])
        self.assertEqual(self.funnel_calls, ["on"])
        self.assertEqual(self.read_budget()["ts_restart_used"], 0)
        self.assertEqual(result["reason"], "round_superseded")

    def test_one_click_local_observation_cannot_change_target_before_commit(self):
        active_url = [URL]

        def observe(forget_stale=True):
            active_url[0] = "https://new.example.ts.net"
            return {"running": True, "auth_state": "valid"}

        result = supervision.run_check(
            now=1000.0, state_dir=self.state_dir, lock_dir=self.lock_dir,
            seams=self.seams(_BACKEND_OBSERVE=observe),
            probe=_ProbeSequence(["ok"]), url_provider=lambda: active_url[0])
        self.assertEqual(result["code"], "round_superseded")
        self.assertEqual(result["overall"], "not_checked")
        self.assertEqual(self.snapshot()["entries"], [])

    def test_snapshot_lock_never_queries_the_boot_identity(self):
        held = False
        queried_inside_lock = []
        original = supervision._with_state_lock

        def boot_reader():
            if held:
                queried_inside_lock.append(True)
                raise AssertionError("boot/CIM read inside the state lock")
            return BOOT

        def state_lock(lock_dir, fn):
            def work():
                nonlocal held
                held = True
                try:
                    return fn()
                finally:
                    held = False
            return original(lock_dir, work)

        supervision._BOOT_ID_READER = boot_reader
        with mock.patch.object(supervision, "_with_state_lock", state_lock):
            result, _ = self.cycle(["ok"])
        self.assertEqual(queried_inside_lock, [])
        self.assertEqual(result["code"], "ok")

    def test_fixed_service_can_restart_while_its_url_is_unobservable(self):
        result = supervision.run_cycle(
            now=1000.0, state_dir=self.state_dir, lock_dir=self.lock_dir,
            seams=self.seams(),
            probe=_ProbeSequence([
                "all_failed", "all_failed", "all_failed", "all_failed", "ok"]),
            url_provider=lambda: URL if self.ts.state == "running" else None,
            sleeper=self.clock.sleep, monotonic=self.clock.monotonic)
        self.assertEqual(result["code"], "recovered")
        self.assertEqual(self.ts.calls, ["stop", "start"])
        self.assertEqual(self.funnel_calls, ["on", "on"])


class ProbeAndRecoveryTests(BasePublicSupervisionTest):
    def test_partial_success_is_degraded_without_destructive_recovery(self):
        result, sequence = self.cycle(["all_failed", "degraded"])
        self.assertEqual(result["code"], "degraded")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(len(sequence.calls), 2)
        self.assertEqual(self.funnel_calls, [])
        self.assertEqual(self.ts.calls, [])
        self.assertEqual(self.snapshot()["overall"], "degraded")

    def test_all_failed_twice_resends_mapping_and_recovers(self):
        result, sequence = self.cycle(
            ["all_failed", "all_failed", "ok"])
        self.assertEqual(result["code"], "recovered")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(len(sequence.calls), 3)
        self.assertEqual(self.funnel_calls, ["on"])
        self.assertEqual(self.ts.calls, [])
        self.assertEqual(self.read_budget()["mapping_used"], 1)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["round"]["confirmation"], 2)
        self.assertEqual(snapshot["recovery"]["state"], "succeeded")

    def test_ts_restart_after_failed_mapping_then_second_mapping(self):
        result, sequence = self.cycle(
            ["all_failed", "all_failed", "all_failed", "all_failed", "ok"])
        self.assertEqual(result["code"], "recovered")
        self.assertEqual(len(sequence.calls), 5)
        self.assertEqual(self.funnel_calls, ["on", "on"])
        self.assertEqual(self.ts.calls, ["stop", "start"])
        budget = self.read_budget()
        self.assertEqual(budget["mapping_used"], 2)
        self.assertEqual(budget["ts_restart_used"], 1)

    def test_budget_exhausted_after_two_mappings_and_one_restart(self):
        result, _ = self.cycle(["all_failed"] * 5)
        self.assertEqual(result["code"], "budget_exhausted")
        self.assertEqual(self.funnel_calls, ["on", "on"])
        self.assertEqual(self.ts.calls, ["stop", "start"])
        self.assertEqual(self.read_budget()["mapping_used"], 2)
        self.assertEqual(self.read_budget()["ts_restart_used"], 1)
        # A later cycle inside the same window must not act again.
        self.clock.value += 10
        result, _ = self.cycle(["all_failed"] * 2, now=1010.0, manual=True)
        self.assertEqual(result["code"], "budget_exhausted")
        self.assertEqual(self.funnel_calls, ["on", "on"])
        self.assertEqual(self.ts.calls, ["stop", "start"])

    def test_incomplete_final_probe_after_second_mapping_is_manual(self):
        # The final mapping was sent, but the last probe did not check every
        # entry: this is unverified evidence, not an exhausted budget.
        result, sequence = self.cycle(
            ["all_failed", "all_failed", "all_failed", "all_failed",
             "incomplete"])
        self.assertEqual(result["code"], "manual_required")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(len(sequence.calls), 5)
        self.assertEqual(self.funnel_calls, ["on", "on"])
        self.assertEqual(self.ts.calls, ["stop", "start"])
        self.assertEqual(self.read_budget()["mapping_used"], 2)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["recovery"]["state"], "manual_required")
        self.assertEqual(snapshot["recovery"]["stage"], "verify")
        self.assertEqual(snapshot["recovery"]["actions"][-1]["action"],
                         "mapping_resend_after_ts")

    def test_not_checked_final_probe_after_second_mapping_is_manual(self):
        result, sequence = self.cycle(
            ["all_failed", "all_failed", "all_failed", "all_failed",
             "not_checked"])
        self.assertEqual(result["code"], "manual_required")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(len(sequence.calls), 5)
        self.assertEqual(self.funnel_calls, ["on", "on"])
        self.assertEqual(self.ts.calls, ["stop", "start"])
        self.assertEqual(self.read_budget()["mapping_used"], 2)
        self.assertEqual(self.snapshot()["recovery"]["state"],
                         "manual_required")

    def test_budget_is_persisted_before_the_action(self):
        observed = {"used": None}

        def funnel(action):
            observed["used"] = self.read_budget()["mapping_used"]
            raise OSError("stub failure")

        result, _ = self.cycle(
            ["all_failed", "all_failed"],
            seams=self.seams(**{"_FUNNEL_CONTROL": funnel}))
        self.assertEqual(observed["used"], 1)
        self.assertEqual(result["code"], "mapping_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(self.read_budget()["mapping_used"], 1)
        actions = self.snapshot()["recovery"]["actions"]
        self.assertEqual(actions[-1]["result"], "unconfirmed")

    def test_empty_seams_are_reported_as_unavailable(self):
        result, _ = self.cycle(["all_failed", "all_failed"], seams={})
        self.assertEqual(result["code"], "seams_unavailable")
        self.assertEqual(result["reason"], "seams_unavailable")
        self.assertEqual(self.funnel_calls, [])

    def test_unknown_security_condition_does_not_recover(self):
        self.ops.verdict = None
        result, _ = self.cycle(["all_failed", "all_failed"])
        self.assertEqual(result["code"], "security_unconfirmed")
        self.assertEqual(result["reason"], "security_unconfirmed")
        self.assertEqual(self.funnel_calls, [])
        self.assertEqual(self.ts.calls, [])

    def test_action_lock_busy_returns_busy_without_action(self):
        lock = supervision.ResourceFileLock(
            self.lock_dir, supervision.ACTION_LOCK_RESOURCE,
            owner="test", timeout_s=0.2)
        lock.acquire()
        try:
            result, _ = self.cycle(["all_failed", "all_failed"])
        finally:
            lock.__exit__(None, None, None)
        self.assertEqual(result["code"], "share_public_busy")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(self.funnel_calls, [])

    def test_ts_start_lock_busy_is_a_refusal_not_unconfirmed(self):
        # B's start runs after TS was confirmed stopped. If the action lock is
        # busy at that point, no start command is sent: the result must be the
        # contract's share_public_busy refusal (exit 1), never an invented
        # "ts_start_unconfirmed" (exit 2) for a command that never left.
        lock = supervision.ResourceFileLock(
            self.lock_dir, supervision.ACTION_LOCK_RESOURCE,
            owner="test", timeout_s=0.2)
        state = {"held": False}
        original = self.ts.status

        def status():
            value = original()
            # Connectivity is no longer re-observed after the stop; the busy
            # lock is acquired from the confirmed stop itself.
            if value == "stopped" and not state["held"]:
                lock.acquire()
                state["held"] = True
            return value

        self.ts.status = status
        try:
            result, _ = self.cycle(
                ["all_failed", "all_failed", "all_failed"])
        finally:
            if state["held"]:
                lock.__exit__(None, None, None)
        self.assertEqual(result["code"], "share_public_busy")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["reason"], "share_public_busy")
        self.assertEqual(self.ts.calls, ["stop"])
        # The stop was confirmed and persisted; the start was never sent, so its
        # action entry must not be rewritten as unconfirmed.
        actions = result["snapshot"]["recovery"]["actions"]
        self.assertEqual(actions[-1]["action"], "ts_restart")
        self.assertEqual(actions[-1]["result"], "ok")
        # No state write on a busy lock: the persisted snapshot still records
        # the confirmed stop and never a fabricated unconfirmed start.
        persisted = self.snapshot()
        self.assertEqual(persisted["recovery"]["stage"], "ts_stop")
        self.assertEqual(persisted["recovery"]["actions"][-1]["result"], "ok")
        self.assertNotEqual(persisted["recovery"]["state"], "failed")

    def test_ts_start_retry_after_sent_start_busy_is_unconfirmed(self):
        # The first start returned an unacknowledged result (it may have left).
        # A busy action lock on the retry must not report a refusal that hides
        # the already-sent start.
        original = self.ts.control
        state = {"first": True}

        def control(action):
            if action == "start" and state["first"]:
                state["first"] = False
                self.ts.calls.append(action)
                return {"ok": False, "detail": "unconfirmed"}
            return original(action)

        lock = supervision.ResourceFileLock(
            self.lock_dir, supervision.ACTION_LOCK_RESOURCE,
            owner="test", timeout_s=0.2)
        held = {"lock": None, "stopped": False}

        def control_with_stop(action):
            result = control(action)
            if action == "stop":
                held["stopped"] = True
            return result

        def sleeper(seconds):
            self.clock.sleep(seconds)
            if held["stopped"] and held["lock"] is None:
                lock.acquire()
                held["lock"] = lock

        try:
            result, _ = self.cycle(
                ["all_failed", "all_failed", "all_failed", "all_failed"],
                seams=self.seams(**{"_TS_SERVICE_CONTROL": control_with_stop}),
                sleeper=sleeper)
        finally:
            if held["lock"] is not None:
                lock.__exit__(None, None, None)
        self.assertEqual(result["code"], "ts_start_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "share_public_busy")
        self.assertEqual(self.ts.calls, ["stop", "start"])
        persisted = self.snapshot()
        self.assertEqual(persisted["recovery"]["state"], "failed")
        self.assertEqual(persisted["recovery"]["action"], "ts_restart")
        self.assertEqual(persisted["recovery"]["stage"], "ts_start")
        self.assertEqual(persisted["recovery"]["actions"][-1]["action"],
                         "ts_restart")
        self.assertEqual(persisted["recovery"]["actions"][-1]["result"],
                         "unconfirmed")
        self.assertEqual(self.read_budget()["ts_restart_used"], 1)

    def test_ts_start_retry_after_sent_start_permanent_refusal_is_unconfirmed(
            self):
        # The first start was sent but unacknowledged; the retry's permanent
        # refusal must keep the unconfirmed outcome (exit 2), not exit 1.
        original = self.ts.control
        state = {"first": True}

        def control(action):
            if action == "start" and state["first"]:
                state["first"] = False
                self.ts.calls.append(action)
                return {"ok": False, "detail": "unconfirmed"}
            if action == "start":
                self.ts.calls.append(action)
                raise OperationError("share_ts_access_denied", "denied")
            return original(action)

        result, _ = self.cycle(
            ["all_failed", "all_failed", "all_failed", "all_failed"],
            seams=self.seams(**{"_TS_SERVICE_CONTROL": control}))
        self.assertEqual(result["code"], "ts_start_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "manual_required")
        self.assertEqual(self.ts.calls, ["stop", "start", "start"])
        persisted = self.snapshot()
        self.assertEqual(persisted["recovery"]["state"], "failed")
        self.assertEqual(persisted["recovery"]["stage"], "ts_start")
        self.assertEqual(persisted["recovery"]["actions"][-1]["action"],
                         "ts_restart")
        self.assertEqual(persisted["recovery"]["actions"][-1]["result"],
                         "unconfirmed")
        self.assertEqual(self.read_budget()["ts_restart_used"], 1)

    def test_ts_start_retry_after_sent_start_seams_unavailable_is_unconfirmed(
            self):
        # The first start was sent but unacknowledged; losing the control seam
        # before the retry must keep the unconfirmed outcome (exit 2).
        original = self.ts.control
        seams = self.seams()
        state = {"first": True}

        def control(action):
            if action == "start" and state["first"]:
                state["first"] = False
                self.ts.calls.append(action)
                seams.pop("_TS_SERVICE_CONTROL", None)
                return {"ok": False, "detail": "unconfirmed"}
            return original(action)

        seams["_TS_SERVICE_CONTROL"] = control
        result, _ = self.cycle(
            ["all_failed", "all_failed", "all_failed", "all_failed"],
            seams=seams)
        self.assertEqual(result["code"], "ts_start_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "seams_unavailable")
        self.assertEqual(self.ts.calls, ["stop", "start"])
        persisted = self.snapshot()
        self.assertEqual(persisted["recovery"]["state"], "failed")
        self.assertEqual(persisted["recovery"]["stage"], "ts_start")
        self.assertEqual(persisted["recovery"]["actions"][-1]["result"],
                         "unconfirmed")

    def test_ts_start_retry_rechecks_security_before_sending(self):
        # The security verdict is re-read inside the action lock before the
        # retried start. A verdict lost during the retry window blocks the
        # second start; the first sent-but-unconfirmed attempt is exit 2.
        original = self.ts.control
        state = {"first": True}

        def control(action):
            if action == "start" and state["first"]:
                state["first"] = False
                self.ts.calls.append(action)
                self.ops.verdict = None
                return {"ok": False, "detail": "unconfirmed"}
            return original(action)

        result, _ = self.cycle(
            ["all_failed", "all_failed", "all_failed", "all_failed"],
            seams=self.seams(**{"_TS_SERVICE_CONTROL": control}))
        self.assertEqual(result["code"], "ts_start_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "security_unconfirmed")
        self.assertEqual(self.ts.calls, ["stop", "start"])
        persisted = self.snapshot()
        self.assertEqual(persisted["recovery"]["state"], "failed")
        self.assertEqual(persisted["recovery"]["stage"], "ts_start")
        self.assertEqual(persisted["recovery"]["actions"][-1]["action"],
                         "ts_restart")
        self.assertEqual(persisted["recovery"]["actions"][-1]["result"],
                         "unconfirmed")
        self.assertEqual(self.read_budget()["ts_restart_used"], 1)

    def test_state_lock_busy_during_recovery_snapshot_is_refusal(self):
        # The recovery snapshot is the authorization for every action. If the
        # state lock is busy at that write, no action may be sent.
        lock = supervision.ResourceFileLock(
            self.lock_dir, supervision.STATE_LOCK_RESOURCE,
            owner="test", timeout_s=0.2)
        state = {"held": False}
        sequence = _ProbeSequence(["all_failed", "all_failed"])
        original = sequence.__call__

        def probe(url):
            result = original(url)
            if len(sequence.calls) == 2 and not state["held"]:
                lock.acquire()
                state["held"] = True
            return result

        try:
            result, _ = self.cycle(probe)
        finally:
            if state["held"]:
                lock.__exit__(None, None, None)
        self.assertEqual(result["code"], "share_public_busy")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(self.funnel_calls, [])
        self.assertEqual(self.ts.calls, [])

    def test_state_lock_busy_after_mapping_is_not_success(self):
        # The mapping action was sent, but its result cannot be persisted. The
        # cycle must not report a confirmed recovery.
        lock = supervision.ResourceFileLock(
            self.lock_dir, supervision.STATE_LOCK_RESOURCE,
            owner="test", timeout_s=0.2)
        state = {"held": False}

        def funnel(action):
            self.funnel_calls.append(action)
            if not state["held"]:
                lock.acquire()
                state["held"] = True
            return {"ok": True, "detail": "stub"}

        try:
            result, _ = self.cycle(
                ["all_failed", "all_failed", "all_failed"],
                seams=self.seams(**{"_FUNNEL_CONTROL": funnel}))
        finally:
            if state["held"]:
                lock.__exit__(None, None, None)
        self.assertEqual(result["code"], "mapping_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(self.funnel_calls, ["on"])

    def test_cancel_during_final_probe_is_not_recovered(self):
        # A human close during the last probe wins over the probe result: the
        # cycle must not record a recovery after the close.
        sequence = _ProbeSequence(
            ["all_failed", "all_failed", "all_failed", "all_failed", "ok"])
        original = sequence.__call__

        def probe(url):
            result = original(url)
            if len(sequence.calls) == 5:
                supervision.request_cancel(
                    "manual_close", state_dir=self.state_dir)
            return result

        result, _ = self.cycle(probe)
        self.assertEqual(result["code"], "cancel_requested")
        self.assertEqual(result["exit_code"], 1)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["overall"], "not_checked")
        self.assertEqual(snapshot["recovery"]["state"], "stopped")

    def test_boot_identity_lost_before_the_action_refuses_without_action(self):
        original = supervision._preconditions
        state = {"ok": True}

        def boot():
            return BOOT if state["ok"] else None

        def preconditions(*args, **kwargs):
            result = original(*args, **kwargs)
            state["ok"] = False
            return result

        supervision._BOOT_ID_READER = boot
        with mock.patch.object(supervision, "_preconditions",
                               side_effect=preconditions):
            result, _ = self.cycle(["all_failed", "all_failed"])
        self.assertEqual(result["code"], "boot_identity_unavailable")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(self.funnel_calls, [])
        self.assertEqual(self.snapshot()["recovery"]["actions"], [])

    def test_boot_identity_lost_after_mapping_is_mapping_unconfirmed(self):
        def funnel(action):
            self.funnel_calls.append(action)
            supervision._BOOT_ID_READER = lambda: None
            return {"ok": True, "detail": "stub"}

        result, _ = self.cycle(
            ["all_failed", "all_failed"],
            seams=self.seams(**{"_FUNNEL_CONTROL": funnel}))
        self.assertEqual(result["code"], "mapping_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "boot_identity_unavailable")
        self.assertEqual(self.funnel_calls, ["on"])

    def test_boot_identity_lost_after_second_mapping_is_mapping_unconfirmed(self):
        calls = {"count": 0}

        def funnel(action):
            self.funnel_calls.append(action)
            calls["count"] += 1
            if calls["count"] == 2:
                supervision._BOOT_ID_READER = lambda: None
            return {"ok": True, "detail": "stub"}

        result, _ = self.cycle(
            ["all_failed", "all_failed", "all_failed", "all_failed"],
            seams=self.seams(**{"_FUNNEL_CONTROL": funnel}))
        self.assertEqual(result["code"], "mapping_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "boot_identity_unavailable")
        self.assertEqual(self.funnel_calls, ["on", "on"])

    def test_mapping_resend_after_ts_failure_is_mapping_unconfirmed(self):
        calls = {"count": 0}

        def funnel(action):
            self.funnel_calls.append(action)
            calls["count"] += 1
            if calls["count"] >= 2:
                raise OSError("second mapping stub failure")
            return {"ok": True, "detail": "stub"}

        result, _ = self.cycle(
            ["all_failed", "all_failed", "all_failed", "all_failed"],
            seams=self.seams(**{"_FUNNEL_CONTROL": funnel}))
        self.assertEqual(result["code"], "mapping_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(self.funnel_calls, ["on", "on"])
        actions = result["snapshot"]["recovery"]["actions"]
        self.assertEqual(actions[-1]["action"], "mapping_resend_after_ts")
        self.assertEqual(actions[-1]["result"], "unconfirmed")

    def test_deadline_after_the_mapping_was_sent_is_unconfirmed(self):
        state = {"value": 0.0}

        def monotonic():
            return state["value"]

        def sleeper(seconds):
            state["value"] += max(0.0, float(seconds))

        def funnel(action):
            self.funnel_calls.append(action)
            state["value"] = supervision.CYCLE_DEADLINE_S + 1.0
            return {"ok": True, "detail": "stub"}

        result, _ = self.cycle(
            ["all_failed", "all_failed", "ok"],
            seams=self.seams(**{"_FUNNEL_CONTROL": funnel}),
            sleeper=sleeper, monotonic=monotonic)
        self.assertEqual(result["code"], "mapping_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "deadline_exceeded")
        self.assertEqual(self.funnel_calls, ["on"])
        actions = result["snapshot"]["recovery"]["actions"]
        self.assertEqual(actions[-1]["result"], "unconfirmed")


class BudgetAndStateTests(BasePublicSupervisionTest):
    def test_expired_entries_are_pruned_from_view_not_file(self):
        self.write_state(observed=900.0)
        self.write_budget(
            mapping=[supervision._utc_iso(1000.0 - 2000.0),
                     supervision._utc_iso(900.0)])
        before = Path(self.paths["budget"]).read_bytes()
        view = self.read_budget(now=1000.0)
        self.assertFalse(view["unreadable"])
        self.assertEqual(view["mapping_used"], 1)
        self.assertEqual(Path(self.paths["budget"]).read_bytes(), before)

    def test_future_timestamp_beyond_tolerance_is_unreadable(self):
        self.write_state(observed=900.0)
        self.write_budget(mapping=[supervision._utc_iso(1000.0 + 120.0)])
        before = Path(self.paths["budget"]).read_bytes()
        result, _ = self.cycle(["all_failed", "all_failed"])
        self.assertEqual(result["code"], "budget_unreadable")
        self.assertEqual(result["reason"], "budget_unreadable")
        self.assertEqual(self.funnel_calls, [])
        self.assertEqual(Path(self.paths["budget"]).read_bytes(), before)

    def test_future_timestamp_inside_tolerance_counts_as_used(self):
        self.write_state(observed=900.0)
        self.write_budget(mapping=[supervision._utc_iso(1000.0 + 30.0)])
        view = self.read_budget(now=1000.0)
        self.assertFalse(view["unreadable"])
        self.assertEqual(view["mapping_used"], 1)

    def test_non_string_and_non_finite_timestamps_are_unreadable(self):
        self.write_state(observed=900.0)
        for value in (1000.0, "nan", "inf"):
            self.write_budget(mapping=[value])
            view = self.read_budget(now=1000.0)
            self.assertTrue(view["unreadable"], value)
            self.assertEqual(view["reason"], "budget_unreadable")

    def test_old_budget_version_is_unreadable_and_not_overwritten(self):
        self.write_state(observed=900.0)
        Path(self.paths["budget"]).write_text(json.dumps({
            "version": 99, "window_s": 1800,
            "mapping_attempts_utc": [], "ts_restarts_utc": [],
            "updated_at_utc": supervision._utc_iso(900.0)}),
            encoding="utf-8")
        before = Path(self.paths["budget"]).read_bytes()
        result, _ = self.cycle(["all_failed", "all_failed"])
        self.assertEqual(result["code"], "budget_unreadable")
        self.assertEqual(result["reason"], "budget_unreadable")
        self.assertEqual(Path(self.paths["budget"]).read_bytes(), before)
        self.assertEqual(self.funnel_calls, [])

    def test_missing_budget_with_existing_state_is_unreadable(self):
        self.write_state(observed=900.0)
        self.assertFalse(os.path.exists(self.paths["budget"]))
        result, _ = self.cycle(["all_failed", "all_failed"])
        self.assertEqual(result["code"], "budget_unreadable")
        self.assertEqual(result["reason"], "budget_unreadable")
        self.assertEqual(self.funnel_calls, [])

    def test_corrupt_state_is_quarantined_and_budget_preserved(self):
        Path(self.paths["state"]).write_text("{broken", encoding="utf-8")
        self.write_budget(mapping=[supervision._utc_iso(900.0)])
        before = Path(self.paths["budget"]).read_bytes()
        result, _ = self.cycle(["ok"])
        self.assertEqual(result["code"], "ok")
        self.assertTrue(os.path.exists(self.paths["state"] + ".corrupt"))
        self.assertEqual(self.snapshot()["version"], 1)
        self.assertEqual(Path(self.paths["budget"]).read_bytes(), before)

    def test_corrupt_state_without_budget_does_not_bootstrap_a_budget(self):
        Path(self.paths["state"]).write_text("{broken", encoding="utf-8")
        result, _ = self.cycle(["ok"])
        self.assertEqual(result["code"], "ok")
        self.assertTrue(os.path.exists(self.paths["state"] + ".corrupt"))
        self.assertFalse(os.path.exists(self.paths["budget"]))
        result, _ = self.cycle(["all_failed", "all_failed"], now=1100.0)
        self.assertEqual(result["code"], "budget_unreadable")
        self.assertEqual(result["reason"], "budget_unreadable")
        self.assertEqual(self.funnel_calls, [])

    def test_old_state_version_is_quarantined(self):
        self.write_budget()
        Path(self.paths["state"]).write_text(
            json.dumps({"version": 99, "observed_at_epoch": 900.0}),
            encoding="utf-8")
        result, _ = self.cycle(["ok"])
        self.assertEqual(result["code"], "ok")
        self.assertTrue(os.path.exists(self.paths["state"] + ".corrupt"))
        self.assertEqual(self.snapshot()["version"], 1)

    def test_stale_snapshot_is_not_overwritten(self):
        first, _ = self.cycle(["ok"], now=2000.0)
        self.assertEqual(first["code"], "ok")
        before = Path(self.paths["state"]).read_bytes()
        result = supervision.run_check(
            now=1000.0, state_dir=self.state_dir, lock_dir=self.lock_dir,
            seams=self.seams(), probe=_ProbeSequence(["all_failed"]),
            url_provider=lambda: URL)
        self.assertEqual(result["code"], "stale_result")
        self.assertEqual(Path(self.paths["state"]).read_bytes(), before)

    def test_concurrent_check_supersedes_the_recovery_round(self):
        def funnel(action):
            self.funnel_calls.append(action)
            supervision.run_check(
                now=1000.5, state_dir=self.state_dir,
                lock_dir=self.lock_dir, seams=self.seams(),
                probe=_ProbeSequence(["ok"]), url_provider=lambda: URL)
            return {"ok": True, "detail": "stub"}

        result, _ = self.cycle(
            ["all_failed", "all_failed", "ok"],
            seams=self.seams(**{"_FUNNEL_CONTROL": funnel}))
        # The mapping command WAS sent before the concurrent snapshot; an
        # unconfirmable outcome is exit 2, never a pre-action refusal.
        self.assertEqual(result["code"], "mapping_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "round_superseded")
        self.assertEqual(self.funnel_calls, ["on"])
        self.assertEqual(self.ts.calls, [])
        self.assertEqual(self.snapshot()["overall"], "ok")
        actions = result["snapshot"]["recovery"]["actions"]
        self.assertEqual(actions[-1]["result"], "unconfirmed")

    def test_state_write_failure_keeps_the_probe_conclusion(self):
        # Contract section 2.8: a failed state rename is logged as
        # state_unreadable, but the successful probe conclusion is still
        # returned (the check itself did not fail).
        real = supervision._atomic_write

        def broken(path, text):
            if path.endswith(supervision.STATE_FILE_NAME):
                raise OSError("disk full")
            return real(path, text)

        with mock.patch.object(supervision, "_atomic_write",
                               side_effect=broken):
            result, _ = self.cycle(["ok"])
        self.assertEqual(result["code"], "ok")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["reason"], "ok")
        lines = Path(self.paths["log"]).read_text(
            encoding="utf-8").splitlines()
        self.assertIn("state_unreadable", lines[-1])

    def test_run_check_state_write_failure_keeps_the_probe_conclusion(self):
        # The read-only entry reports the SAME successful probe conclusion when
        # the state write fails: state_unreadable is logged, never folded into a
        # probe refusal.
        real = supervision._atomic_write

        def broken(path, text):
            if path.endswith(supervision.STATE_FILE_NAME):
                raise OSError("disk full")
            return real(path, text)

        with mock.patch.object(supervision, "_atomic_write",
                               side_effect=broken):
            result = supervision.run_check(
                now=1000.0, state_dir=self.state_dir,
                lock_dir=self.lock_dir, seams=self.seams(),
                probe=_ProbeSequence(["ok"]), url_provider=lambda: URL)
        self.assertEqual(result["code"], "ok")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["overall"], "ok")
        self.assertEqual(result["reason"], "ok")
        self.assertTrue(result["snapshot"]["entries"])
        self.assertTrue(result["probe"]["entries"])
        lines = Path(self.paths["log"]).read_text(
            encoding="utf-8").splitlines()
        self.assertIn("state_unreadable", lines[-1])

    def test_read_status_reports_missing_and_corrupt_honestly(self):
        status = supervision.read_status(state_dir=self.state_dir)
        self.assertIsNone(status["snapshot"])
        self.assertFalse(status["unreadable"])
        Path(self.paths["state"]).write_text("{broken", encoding="utf-8")
        status = supervision.read_status(state_dir=self.state_dir)
        self.assertIsNone(status["snapshot"])
        self.assertTrue(status["unreadable"])
        self.assertEqual(status["reason"], "state_unreadable")


class MaintenanceTests(BasePublicSupervisionTest):
    def test_no_lease_is_normal_even_with_lease_expired_true(self):
        self.maintenance = lambda: {"lease": None, "lease_expired": True}
        result, _ = self.cycle(["all_failed", "all_failed", "ok"])
        self.assertEqual(result["code"], "recovered")
        self.assertEqual(self.funnel_calls, ["on"])

    def test_expired_lease_is_reread_once_and_cleared(self):
        calls = {"count": 0}

        def maintenance():
            calls["count"] += 1
            if calls["count"] == 1:
                return {"lease": {"lease_id": "l1", "component": "c"},
                        "lease_expired": True}
            return {"lease": None, "lease_expired": False}

        result, _ = self.cycle(
            ["all_failed", "all_failed", "ok"],
            seams=self.seams(**{"_MAINTENANCE_STATUS": maintenance}))
        self.assertEqual(result["code"], "recovered")
        # The step-0 re-read plus the two in-lock action re-checks.
        self.assertEqual(calls["count"], 4)
        self.assertEqual(self.funnel_calls, ["on"])

    def test_expired_lease_still_present_goes_manual(self):
        self.maintenance = lambda: {
            "lease": {"lease_id": "l1", "component": "c"},
            "lease_expired": True}
        result, _ = self.cycle(["all_failed", "all_failed"])
        self.assertEqual(result["code"], "maintenance_in_force")
        self.assertEqual(result["reason"], "maintenance_in_force")
        self.assertEqual(self.funnel_calls, [])

    def test_unavailable_maintenance_does_not_recover(self):
        self.maintenance = lambda: None
        result, _ = self.cycle(["all_failed", "all_failed"])
        self.assertEqual(result["code"], "maintenance_unavailable")
        self.assertEqual(result["reason"], "maintenance_unavailable")
        self.assertEqual(self.funnel_calls, [])

    def test_lease_appearing_before_the_action_refuses_without_action(self):
        # The full precheck saw no lease; A's in-lock quick re-read sees one.
        # No command may be sent and the result is a refusal (exit 1), never a
        # fabricated unconfirmed action.
        calls = {"count": 0}

        def maintenance():
            calls["count"] += 1
            if calls["count"] == 1:
                return {"lease": None, "lease_expired": False}
            return {"lease": {"lease_id": "l1", "component": "c"},
                    "lease_expired": False}

        result, _ = self.cycle(
            ["all_failed", "all_failed"],
            seams=self.seams(**{"_MAINTENANCE_STATUS": maintenance}))
        self.assertEqual(result["code"], "maintenance_in_force")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(self.funnel_calls, [])
        self.assertEqual(self.ts.calls, [])
        self.assertEqual(self.snapshot()["recovery"]["actions"], [])
        self.assertEqual(self.read_budget()["mapping_used"], 0)

    def test_lease_on_the_final_in_lock_recheck_refuses_without_action(self):
        # The full precheck and the FIRST in-lock re-read see no lease; the
        # final re-read (after the budget reservation) sees one. No command is
        # sent and the round is a refusal (exit 1), never an unconfirmed action.
        calls = {"count": 0}

        def maintenance():
            calls["count"] += 1
            if calls["count"] >= 3:
                return {"lease": {"lease_id": "l1", "component": "c"},
                        "lease_expired": False}
            return {"lease": None, "lease_expired": False}

        result, _ = self.cycle(
            ["all_failed", "all_failed"],
            seams=self.seams(**{"_MAINTENANCE_STATUS": maintenance}))
        self.assertEqual(result["code"], "maintenance_in_force")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(self.funnel_calls, [])
        self.assertEqual(self.snapshot()["recovery"]["actions"], [])

    def test_unavailable_maintenance_before_the_action_refuses(self):
        calls = {"count": 0}

        def maintenance():
            calls["count"] += 1
            if calls["count"] == 1:
                return {"lease": None, "lease_expired": False}
            return None

        result, _ = self.cycle(
            ["all_failed", "all_failed"],
            seams=self.seams(**{"_MAINTENANCE_STATUS": maintenance}))
        self.assertEqual(result["code"], "maintenance_unavailable")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(self.funnel_calls, [])
        self.assertEqual(self.snapshot()["recovery"]["actions"], [])


class TsRestartTests(BasePublicSupervisionTest):
    def test_deadline_after_ts_stop_is_unconfirmed(self):
        state = {"value": 0.0}

        def monotonic():
            return state["value"]

        def sleeper(seconds):
            state["value"] += max(0.0, float(seconds))

        original_status = self.ts.status

        def status():
            value = original_status()
            if value == "stopped":
                state["value"] = supervision.CYCLE_DEADLINE_S + 1.0
            return value

        self.ts.status = status
        result, _ = self.cycle(
            ["all_failed", "all_failed", "all_failed"],
            sleeper=sleeper, monotonic=monotonic)
        self.assertEqual(result["code"], "recovery_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "deadline_exceeded")
        self.assertEqual(self.ts.calls, ["stop"])
        actions = result["snapshot"]["recovery"]["actions"]
        self.assertEqual(actions[-1]["result"], "unconfirmed")

    def test_cancel_after_ts_stop_never_starts_again(self):
        def on_stop():
            supervision.request_cancel(
                "manual_estop", state_dir=self.state_dir)

        self.ts.on_stop = on_stop
        result, _ = self.cycle(
            ["all_failed", "all_failed", "all_failed"])
        self.assertEqual(result["code"], "recovery_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "cancel_requested")
        self.assertEqual(self.ts.calls, ["stop"])
        actions = self.snapshot()["recovery"]["actions"]
        self.assertEqual(actions[-1]["action"], "ts_restart")
        self.assertEqual(actions[-1]["result"], "unconfirmed")

    def test_round_supersede_after_ts_stop_is_unconfirmed(self):
        def on_start():
            supervision.run_check(
                now=1000.5, state_dir=self.state_dir,
                lock_dir=self.lock_dir, seams=self.seams(),
                probe=_ProbeSequence(["ok"]), url_provider=lambda: URL)

        self.ts.on_start = on_start
        result, _ = self.cycle(["all_failed", "all_failed", "all_failed"])
        self.assertEqual(result["code"], "recovery_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "round_superseded")
        self.assertEqual(self.ts.calls, ["stop", "start"])
        actions = self.snapshot()["recovery"]["actions"]
        self.assertEqual(actions[-1]["action"], "ts_restart")
        self.assertIn(actions[-1]["result"], ("ok", "unconfirmed"))

    def test_maintenance_after_ts_stop_blocks_start(self):
        calls = {"count": 0}

        def maintenance():
            calls["count"] += 1
            if calls["count"] > 5:
                return {"lease": {"lease_id": "l1", "component": "c"},
                        "lease_expired": False}
            return {"lease": None, "lease_expired": False}

        result, _ = self.cycle(
            ["all_failed", "all_failed", "all_failed"],
            seams=self.seams(**{"_MAINTENANCE_STATUS": maintenance}))
        self.assertEqual(result["code"], "recovery_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "maintenance_in_force")
        self.assertEqual(self.ts.calls, ["stop"])

    def test_security_change_after_ts_stop_blocks_start(self):
        def on_stop():
            self.ops.verdict = None

        self.ts.on_stop = on_stop
        result, _ = self.cycle(
            ["all_failed", "all_failed", "all_failed"])
        self.assertEqual(result["code"], "recovery_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "security_unconfirmed")
        self.assertEqual(self.ts.calls, ["stop"])

    def test_ts_start_permanent_refusal_is_manual_required_exit_one(self):
        original = self.ts.control

        def control(action):
            if action == "start":
                raise OperationError("share_ts_access_denied", "denied")
            return original(action)

        result, _ = self.cycle(
            ["all_failed", "all_failed", "all_failed"],
            seams=self.seams(**{"_TS_SERVICE_CONTROL": control}))
        self.assertEqual(result["code"], "manual_required")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(self.ts.calls, ["stop"])
        actions = result["snapshot"]["recovery"]["actions"]
        self.assertEqual(actions[-1]["action"], "ts_restart")
        self.assertEqual(actions[-1]["result"], "failed")

    def test_ts_start_missing_seam_is_seams_unavailable_exit_one(self):
        seams = self.seams()

        def on_stop():
            seams.pop("_TS_SERVICE_CONTROL", None)

        self.ts.on_stop = on_stop
        result, _ = self.cycle(
            ["all_failed", "all_failed", "all_failed"], seams=seams)
        self.assertEqual(result["code"], "seams_unavailable")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(self.ts.calls, ["stop"])
        actions = result["snapshot"]["recovery"]["actions"]
        self.assertEqual(actions[-1]["result"], "failed")

    def test_ts_stop_pending_confirmation_is_polled_to_stopped(self):
        # share_ts_pending_confirmation means the stop command was accepted
        # asynchronously: the bounded status poll must confirm "stopped", not
        # report the action as unconfirmed.
        original = self.ts.control
        state = {"raised": False, "polls": 0, "phase": "stop"}

        def control(action):
            if action == "stop" and not state["raised"]:
                state["raised"] = True
                self.ts.calls.append(action)
                raise OperationError("share_ts_pending_confirmation",
                                     "pending")
            if action == "start":
                state["phase"] = "start"
            return original(action)

        def status():
            if state["phase"] != "stop":
                return self.ts.state
            state["polls"] += 1
            return "stopped" if state["polls"] >= 2 else "running"

        result, _ = self.cycle(
            ["all_failed", "all_failed", "all_failed", "ok"],
            seams=self.seams(**{"_TS_SERVICE_CONTROL": control,
                                "_TS_SERVICE_STATUS": status}))
        self.assertEqual(result["code"], "recovered")
        self.assertEqual(result["exit_code"], 0)
        self.assertGreaterEqual(state["polls"], 2)
        self.assertEqual(self.ts.calls, ["stop", "start"])
        self.assertEqual(result["snapshot"]["recovery"]["state"], "succeeded")

    def test_ts_start_pending_confirmation_is_polled_to_running(self):
        original = self.ts.control
        state = {"raised": False, "polls": 0, "phase": "stop"}

        def control(action):
            if action == "start" and not state["raised"]:
                state["raised"] = True
                state["phase"] = "start"
                self.ts.calls.append(action)
                raise OperationError("share_ts_pending_confirmation",
                                     "pending")
            return original(action)

        def status():
            if state["phase"] != "start":
                return self.ts.state
            state["polls"] += 1
            return "running" if state["polls"] >= 2 else "stopped"

        result, _ = self.cycle(
            ["all_failed", "all_failed", "all_failed", "ok"],
            seams=self.seams(**{"_TS_SERVICE_CONTROL": control,
                                "_TS_SERVICE_STATUS": status}))
        self.assertEqual(result["code"], "recovered")
        self.assertEqual(result["exit_code"], 0)
        self.assertGreaterEqual(state["polls"], 2)
        self.assertEqual(self.ts.calls, ["stop", "start"])
        self.assertEqual(result["snapshot"]["recovery"]["state"], "succeeded")

    def test_boot_identity_lost_after_start_is_recovery_unconfirmed(self):
        original = self.ts.control

        def control(action):
            value = original(action)
            if action == "start":
                supervision._BOOT_ID_READER = lambda: None
            return value

        result, _ = self.cycle(
            ["all_failed", "all_failed", "all_failed"],
            seams=self.seams(**{"_TS_SERVICE_CONTROL": control}))
        self.assertEqual(result["code"], "recovery_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "boot_identity_unavailable")
        self.assertEqual(self.ts.calls, ["stop", "start"])

    def test_cancel_after_mapping_does_not_reopen(self):
        def funnel(action):
            self.funnel_calls.append(action)
            supervision.request_cancel(
                "manual_close", state_dir=self.state_dir)
            return {"ok": True, "detail": "stub"}

        result, _ = self.cycle(
            ["all_failed", "all_failed"],
            seams=self.seams(**{"_FUNNEL_CONTROL": funnel}))
        self.assertEqual(result["code"], "cancel_requested")
        self.assertEqual(self.funnel_calls, ["on"])
        self.assertEqual(self.ts.calls, [])


class LockDisciplineTests(BasePublicSupervisionTest):
    def test_cancel_after_budget_reservation_blocks_the_action(self):
        original = supervision._reserve_budget_in_lock

        def reserve(paths, kind, now):
            result = original(paths, kind, now)
            supervision._write_cancel(self.paths, "manual_close", now)
            return result

        with mock.patch.object(supervision, "_reserve_budget_in_lock",
                               side_effect=reserve):
            result, _ = self.cycle(["all_failed", "all_failed"])
        self.assertEqual(result["code"], "cancel_requested")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(self.funnel_calls, [])
        self.assertEqual(self.read_budget()["mapping_used"], 1)

    def test_wait_does_not_hold_the_action_lock(self):
        acquired = {"value": False}

        def sleeper(seconds):
            self.clock.sleep(seconds)
            lock = supervision.ResourceFileLock(
                self.lock_dir, supervision.ACTION_LOCK_RESOURCE,
                owner="probe", timeout_s=0.1)
            try:
                lock.acquire()
            except supervision.LockBusy:
                return
            acquired["value"] = True
            lock.__exit__(None, None, None)

        self.ops.verdict = False
        result, _ = self.cycle(["all_failed", "all_failed"], sleeper=sleeper)
        self.assertEqual(result["code"], "security_not_satisfied")
        self.assertTrue(acquired["value"])


class ManualRetryTests(BasePublicSupervisionTest):
    def test_manual_fast_path_uses_confirmation_one(self):
        self.ops.verdict = False
        first, _ = self.cycle(["all_failed", "all_failed"])
        self.assertEqual(first["code"], "security_not_satisfied")
        self.ops.verdict = True
        result, sequence = self.cycle(
            ["all_failed", "ok"], now=1100.0, manual=True)
        self.assertEqual(result["code"], "recovered")
        self.assertEqual(len(sequence.calls), 2)
        self.assertEqual(self.funnel_calls, ["on"])
        self.assertEqual(self.snapshot()["round"]["confirmation"], 1)

    def test_stale_manual_retry_falls_back_to_ten_second_wait(self):
        self.ops.verdict = False
        first, _ = self.cycle(["all_failed", "all_failed"])
        self.assertEqual(first["code"], "security_not_satisfied")
        result, sequence = self.cycle(
            ["all_failed", "all_failed"], now=2000.0, manual=True)
        self.assertEqual(result["code"], "security_not_satisfied")
        self.assertEqual(result["reason"], "security_not_satisfied")
        self.assertEqual(len(sequence.calls), 2)
        self.assertEqual(self.snapshot()["round"]["confirmation"], 2)


class TimestampTests(BasePublicSupervisionTest):
    def test_recovery_timestamps_follow_the_action_wall_clock(self):
        wall = {"value": 1000.0}

        def clock():
            return wall["value"]

        # The full precheck takes 55 seconds; the budget reservation and the
        # action/event/finished timestamps must use THAT time, not round start.
        original_evidence = self.ops.host_evidence
        advanced = {"done": False}

        def host_evidence(kind):
            if not advanced["done"]:
                advanced["done"] = True
                wall["value"] += 55.0
            return original_evidence(kind)

        self.ops.host_evidence = host_evidence

        result, _ = self.cycle(
            ["all_failed", "all_failed", "ok"], clock=clock)
        self.assertEqual(result["code"], "recovered")
        budget = self.read_budget(now=1055.0)
        self.assertEqual(budget["mapping_attempts_utc"],
                         [supervision._utc_iso(1055.0)])
        recovery = result["snapshot"]["recovery"]
        self.assertEqual(recovery["actions"][0]["at_utc"],
                         supervision._utc_iso(1055.0))
        self.assertEqual(recovery["finished_at_utc"],
                         supervision._utc_iso(1055.0))
        self.assertEqual(result["snapshot"]["round"]["finished_at_utc"],
                         supervision._utc_iso(1055.0))

    def test_budget_reservation_reads_the_wall_clock_at_reservation_time(self):
        wall = {"value": 1000.0}
        original = supervision._quick_recheck
        calls = {"count": 0}

        def quick(**kwargs):
            result = original(**kwargs)
            calls["count"] += 1
            if calls["count"] == 1:
                # The wall clock advances inside the action lock, AFTER the
                # first re-check and BEFORE the reservation is written.
                wall["value"] = 1030.0
            return result

        with mock.patch.object(supervision, "_quick_recheck",
                               side_effect=quick):
            result, _ = self.cycle(
                ["all_failed", "all_failed", "ok"],
                clock=lambda: wall["value"])
        self.assertEqual(result["code"], "recovered")
        self.assertEqual(
            self.read_budget(now=1030.0)["mapping_attempts_utc"],
            [supervision._utc_iso(1030.0)])
        self.assertEqual(
            result["snapshot"]["recovery"]["actions"][0]["at_utc"],
            supervision._utc_iso(1030.0))


class RunRecoveryEntryTests(BasePublicSupervisionTest):
    def test_run_recovery_persists_identity_and_recovers(self):
        self.ops.verdict = False
        first, _ = self.cycle(["all_failed", "all_failed"])
        self.assertEqual(first["code"], "security_not_satisfied")
        self.ops.verdict = True
        snapshot = self.snapshot()
        with mock.patch.object(supervision.time, "sleep",
                               self.clock.sleep), \
                mock.patch.object(supervision.time, "monotonic",
                                  self.clock.monotonic):
            result = supervision.run_recovery(
                snapshot=snapshot, manual=True, now=1100.0,
                state_dir=self.state_dir, lock_dir=self.lock_dir,
                seams=self.seams(), probe=_ProbeSequence(["ok"]),
                url_provider=lambda: URL)
        self.assertEqual(result["code"], "recovered")
        self.assertEqual(self.funnel_calls, ["on"])
        self.assertEqual(self.read_budget(now=1100.0)["mapping_used"], 1)

    def test_run_recovery_reports_budget_unreadable_without_acting(self):
        self.ops.verdict = False
        first, _ = self.cycle(["all_failed", "all_failed"])
        self.assertEqual(first["code"], "security_not_satisfied")
        self.ops.verdict = True
        snapshot = self.snapshot()
        Path(self.paths["budget"]).write_text("{broken", encoding="utf-8")
        with mock.patch.object(supervision.time, "sleep",
                               self.clock.sleep), \
                mock.patch.object(supervision.time, "monotonic",
                                  self.clock.monotonic):
            result = supervision.run_recovery(
                snapshot=snapshot, manual=True, now=1100.0,
                state_dir=self.state_dir, lock_dir=self.lock_dir,
                seams=self.seams(), probe=_ProbeSequence(["ok"]))
        self.assertEqual(result["code"], "budget_unreadable")
        self.assertEqual(result["exit_code"], 1)
        self.assertFalse(self.funnel_calls)
        self.assertFalse(self.ts.calls)


class EventAndNotificationTests(BasePublicSupervisionTest):
    def test_events_drop_old_malformed_and_old_versions(self):
        now = supervision.EVENT_MAX_AGE_S + 5000.0
        self.write_state(observed=now - 100.0, overall="degraded",
                         reason="partial_failure")
        self.write_budget(now=now)
        old = supervision._event_record(supervision._event(
            now - supervision.EVENT_MAX_AGE_S - 10.0,
            "degraded", "old_code"))
        malformed = "{not json"
        old_version = json.dumps({
            "version": 99, "at_utc": supervision._utc_iso(1000.0),
            "status": "degraded", "code": "old_version", "action": "none"})
        recent = supervision._event_record(supervision._event(
            now - 100.0, "degraded", "partial_failure"))
        self.write_event_lines([
            json.dumps(old), malformed, old_version, json.dumps(recent)])
        budget_before = Path(self.paths["budget"]).read_bytes()
        result, _ = self.cycle(["ok"], now=now)
        self.assertEqual(result["code"], "ok")
        events = supervision.read_status(
            state_dir=self.state_dir, now=now)["events"]
        codes = [item["code"] for item in events]
        self.assertNotIn("old_code", codes)
        self.assertNotIn("old_version", codes)
        self.assertIn("ok", codes)
        self.assertIn("partial_failure", codes)
        self.assertTrue(os.path.exists(self.paths["budget"]))
        self.assertEqual(Path(self.paths["budget"]).read_bytes(),
                         budget_before)

    def test_event_size_is_pruned_to_target(self):
        saved = (supervision.EVENT_MAX_BYTES, supervision.EVENT_TARGET_BYTES)
        supervision.EVENT_MAX_BYTES = 700
        supervision.EVENT_TARGET_BYTES = 350
        try:
            self.write_state(observed=900.0, overall="degraded",
                             reason="partial_failure")
            lines = [json.dumps(supervision._event_record(
                supervision._event(1000.0 - index, "degraded",
                                  "code_%02d" % index)))
                for index in range(20)]
            self.write_event_lines(lines)
            result, _ = self.cycle(["ok"])
            self.assertEqual(result["code"], "ok")
            size = os.path.getsize(self.paths["event"])
            self.assertLessEqual(size, supervision.EVENT_MAX_BYTES)
        finally:
            supervision.EVENT_MAX_BYTES, supervision.EVENT_TARGET_BYTES = saved

    def test_event_prune_keeps_budget_and_stop_evidence(self):
        now = supervision.EVENT_MAX_AGE_S + 5000.0
        self.write_state(observed=now - 100.0)
        self.write_budget(mapping=[supervision._utc_iso(now - 50.0)],
                          now=now)
        old = supervision._event_record(supervision._event(
            now - supervision.EVENT_MAX_AGE_S - 10.0,
            "degraded", "old_code"))
        self.write_event_lines([json.dumps(old)])
        self.desired_stop = {"state": "stopped", "stopped_at": "t"}
        before = Path(self.paths["budget"]).read_bytes()
        result, _ = self.cycle(["ok"], now=now)
        # A protected stop remains authoritative even if a late TLS probe passed.
        self.assertEqual(result["code"], "managed_stop_in_force")
        self.assertEqual(result["overall"], "not_checked")
        self.assertEqual(Path(self.paths["budget"]).read_bytes(), before)
        self.assertEqual(self.snapshot()["local"]["lifecycle"], "stopped")
        events = supervision.read_status(
            state_dir=self.state_dir, now=now)["events"]
        self.assertNotIn("old_code", [item["code"] for item in events])

    def test_notification_failure_keeps_records_and_retries_then_dedupes(self):
        self.notify_ok = False
        result, _ = self.cycle(["degraded"])
        self.assertEqual(result["code"], "degraded")
        snapshot = self.snapshot()
        self.assertEqual(snapshot["notification"]["last_result"], "failed")
        self.assertIsNone(snapshot["notification"]["last_key"])
        self.assertEqual(len(self.notify_calls), 1)
        events = supervision.read_status(
            state_dir=self.state_dir, now=1000.0)["events"]
        self.assertIn("notification_failed",
                      [item["code"] for item in events])

        self.notify_ok = True
        self.clock.value += 10
        result, _ = self.cycle(["degraded"], now=1010.0)
        self.assertEqual(result["code"], "degraded")
        snapshot = self.snapshot()
        self.assertEqual(snapshot["notification"]["last_result"], "sent")
        self.assertEqual(snapshot["notification"]["last_key"],
                         "degraded|partial_failure")
        self.assertEqual(len(self.notify_calls), 2)

        self.clock.value += 10
        result, _ = self.cycle(["degraded"], now=1020.0)
        self.assertEqual(len(self.notify_calls), 2)

    def test_two_independent_incidents_each_notify_failure_and_recovery(self):
        first, _ = self.cycle(["all_failed", "all_failed", "ok"], now=1000.0)
        self.assertEqual(first["code"], "recovered")
        second, _ = self.cycle(["all_failed", "all_failed", "ok"], now=1200.0)
        self.assertEqual(second["code"], "recovered")
        self.assertEqual(
            self.notify_calls,
            ["share_public_failed", "share_public_recovered",
             "share_public_failed", "share_public_recovered"])
        status = supervision.read_status(
            state_dir=self.state_dir, now=1200.0)
        # The dedupe key is <overall>|<reason> with NO round id.
        self.assertEqual(
            status["snapshot"]["notification"]["last_key"], "recovered|ok")

    def test_same_persistent_fault_notifies_failure_once(self):
        def on_stop():
            self.ops.verdict = None

        self.ts.on_stop = on_stop
        first, _ = self.cycle(
            ["all_failed", "all_failed", "all_failed"], now=1000.0)
        self.assertEqual(first["code"], "recovery_unconfirmed")
        self.assertEqual(self.notify_calls, ["share_public_failed"])

        # The SAME fault persists into the next round: the failure notification
        # is not repeated (the key carries no round id).
        self.ops.verdict = True
        self.ts.on_stop = None
        second, _ = self.cycle(
            ["all_failed", "all_failed", "all_failed", "all_failed"],
            now=1100.0)
        self.assertEqual(second["code"], "budget_exhausted")
        self.assertEqual(
            self.notify_calls,
            ["share_public_failed", "share_public_budget_exhausted"])

    def test_run_check_displays_an_expired_maintenance_lease_read_only(self):
        self.maintenance = lambda: {
            "lease": {"lease_id": "l1", "component": "c"},
            "lease_expired": True}
        result = supervision.run_check(
            now=1000.0, state_dir=self.state_dir, lock_dir=self.lock_dir,
            seams=self.seams(), probe=_ProbeSequence(["ok"]),
            url_provider=lambda: URL)
        self.assertEqual(result["code"], "ok")
        self.assertEqual(self.snapshot()["local"]["maintenance"], "expired")
        self.assertEqual(self.funnel_calls, [])
        self.assertEqual(self.ts.calls, [])

    def test_export_redacts_injected_credentials(self):
        def backend(forget_stale=True):
            return {"running": True, "auth_state": "valid",
                    "token": "SECRET-TOKEN", "authorization": "Bearer X",
                    "cookie": "session=Y", "password": "P"}

        result = supervision.run_check(
            now=1000.0, state_dir=self.state_dir, lock_dir=self.lock_dir,
            seams=self.seams(**{"_BACKEND_OBSERVE": backend}),
            probe=_ProbeSequence(["ok"]), url_provider=lambda: URL)
        self.assertEqual(result["code"], "ok")
        exported = supervision.export_diagnostics(
            state_dir=self.state_dir, now=1000.0)
        text = json.dumps(exported, ensure_ascii=False)
        for secret in ("SECRET-TOKEN", "Bearer X", "session=Y", '"P"'):
            self.assertNotIn(secret, text)
        local = exported["snapshot"]["local"]
        self.assertEqual(local["backend"], "running")
        self.assertEqual(local["auth"], "valid")

    def test_run_check_preserves_ts_stopped_evidence(self):
        self.write_state(
            observed=900.0, overall="all_failed", reason="all_entries_failed",
            recovery={
                "state": "failed", "action": "ts_restart", "stage": "ts_start",
                "reason": "cancel_requested", "attempted_at_utc": "t",
                "finished_at_utc": "t", "manual": False,
                "actions": [{"action": "ts_restart", "at_utc": "t",
                             "result": "unconfirmed"}]})
        self.write_budget()
        supervision.run_check(
            now=1000.0, state_dir=self.state_dir, lock_dir=self.lock_dir,
            seams=self.seams(), probe=_ProbeSequence(["ok"]),
            url_provider=lambda: URL)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["overall"], "ok")
        self.assertEqual(snapshot["recovery"]["state"], "failed")
        self.assertEqual(snapshot["recovery"]["actions"][-1]["result"],
                         "unconfirmed")


class CycleDriverTests(BasePublicSupervisionTest):
    def test_first_failure_waits_ten_seconds_before_the_second_probe(self):
        self.ops.verdict = None
        sleeps = []

        def sleeper(seconds):
            sleeps.append(float(seconds))
            self.clock.sleep(seconds)

        result, sequence = self.cycle(
            ["all_failed", "all_failed"], sleeper=sleeper)
        self.assertEqual(result["code"], "security_unconfirmed")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(len(sequence.calls), 2)
        self.assertAlmostEqual(sum(sleeps), supervision.CONFIRM_DELAY_S)
        self.assertGreaterEqual(len(sleeps), 1)
        self.assertFalse(self.funnel_calls)
        self.assertFalse(self.ts.calls)
        self.assertEqual(self.snapshot()["round"]["confirmation"], 2)

    def test_cycle_deadline_bounds_the_confirmation_wait(self):
        self.ops.verdict = None

        def sleeper(seconds):
            # Jump past the whole cycle deadline on the first slice.
            self.clock.value += supervision.CYCLE_DEADLINE_S + 1.0

        result, sequence = self.cycle(
            ["all_failed", "all_failed"], sleeper=sleeper)
        self.assertEqual(result["code"], "deadline_exceeded")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(len(sequence.calls), 1)
        self.assertFalse(self.funnel_calls)
        self.assertFalse(self.ts.calls)

    def test_probe_exception_is_not_checked_and_never_recovers(self):
        def broken(url):
            raise RuntimeError("boom")

        result, _ = self.cycle(broken)
        self.assertEqual(result["code"], "not_checked")
        self.assertEqual(result["exit_code"], 1)
        self.assertFalse(self.funnel_calls)
        self.assertFalse(self.ts.calls)

    def test_repeated_ok_rounds_do_not_consume_the_budget(self):
        self.cycle(["ok"])
        before = Path(self.paths["budget"]).read_bytes()
        self.clock.value += supervision.NORMAL_INTERVAL_S
        result, _ = self.cycle(["ok"], now=1060.0)
        self.assertEqual(result["code"], "ok")
        self.assertEqual(Path(self.paths["budget"]).read_bytes(), before)
        self.assertEqual(self.read_budget(now=1060.0)["mapping_used"], 0)


class DefaultPublicUrlTests(BasePublicSupervisionTest):
    """D18-1: the default URL provider prefers the configured origin."""

    CONFIGURED = "https://dacintapi.de5.net"

    def test_configured_origin_wins_without_a_tailscale_read(self):
        with mock.patch.dict(os.environ,
                             {"CONSOLE_PUBLIC_ORIGIN": self.CONFIGURED}), \
                mock.patch(
                    "console_share_production.production_share_public_url",
                    side_effect=AssertionError(
                        "the Tailscale URL must not be read")):
            self.assertEqual(supervision._public_url(None), self.CONFIGURED)

    def test_invalid_configured_origin_is_unavailable_without_fallback(self):
        with mock.patch.dict(os.environ, {
                "CONSOLE_PUBLIC_ORIGIN": self.CONFIGURED + "/v1"}), \
                mock.patch(
                    "console_share_production.production_share_public_url",
                    side_effect=AssertionError(
                        "the Tailscale URL must not be read")):
            self.assertIsNone(supervision._public_url(None))

    def test_unconfigured_keeps_the_tailscale_url(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch(
                    "console_share_production.production_share_public_url",
                    return_value="https://fixture.tail-test.ts.net"):
            os.environ.pop("CONSOLE_PUBLIC_ORIGIN", None)
            self.assertEqual(supervision._public_url(None),
                             "https://fixture.tail-test.ts.net")


class CliTests(BasePublicSupervisionTest):
    def test_cli_apply_returns_the_driver_exit_code(self):
        with mock.patch.object(
                supervision, "run_cycle",
                return_value={"code": "recovery_unconfirmed",
                              "exit_code": 2}):
            code = supervision.main(["--apply", "--state-dir",
                                    self.state_dir])
        self.assertEqual(code, 2)

    def test_cli_check_returns_the_driver_exit_code(self):
        with mock.patch.object(
                supervision, "run_cycle",
                return_value={"code": "all_failed", "exit_code": 1}):
            code = supervision.main(["--check", "--state-dir",
                                    self.state_dir])
        self.assertEqual(code, 1)

    def test_cli_check_writes_nothing(self):
        with mock.patch.object(supervision, "_public_url",
                               return_value=URL), \
                mock.patch.object(supervision, "_default_probe",
                                  return_value=_probe_result("ok")):
            code = supervision.main(
                ["--check", "--state-dir", self.state_dir])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.paths["state"]))
        self.assertFalse(os.path.exists(self.paths["budget"]))

    def test_cli_status_works_without_state(self):
        self.assertEqual(supervision.main(
            ["--status", "--state-dir", self.state_dir]), 0)


class PublicDnsSupervisionTests(BasePublicSupervisionTest):
    def test_dns_detail_is_whitelisted_and_exported(self):
        dns = _dns(
            "ok", addresses=["8.8.8.8", "1.1.1.1", "198.18.2.161"], ttl=30,
            sources=[
                _dns_source("cloudflare", "ok", addresses=["8.8.8.8"], ttl=60),
                _dns_source("google", "ok", addresses=["1.1.1.1"], ttl=30),
            ])
        dns["secret"] = "should-not-survive"
        dns["raw"] = "should-not-survive"
        dns["sources"][0]["raw"] = "should-not-survive"
        dns["sources"][0]["addresses"].append("127.0.0.1")
        self.cycle(lambda url: _probe_result("ok", dns=dns))
        snapshot = self.snapshot()
        self.assertEqual(snapshot["dns"]["state"], "ok")
        self.assertEqual(snapshot["dns"]["ttl_s"], 30)
        self.assertEqual(snapshot["dns"]["addresses"],
                         ["8.8.8.8", "1.1.1.1"])
        self.assertEqual([source["source"] for source in snapshot["dns"]["sources"]],
                         ["cloudflare", "google"])
        text = json.dumps(snapshot)
        self.assertNotIn("should-not-survive", text)
        self.assertNotIn("198.18.2.161", text)
        exported = supervision.export_diagnostics(
            state_dir=self.state_dir, now=1000.0)
        self.assertEqual(exported["snapshot"]["dns"]["state"], "ok")
        self.assertNotIn("should-not-survive", json.dumps(exported))

    def test_old_state_without_dns_stays_readable(self):
        self.write_state()
        self.write_budget()
        snapshot = self.snapshot()
        self.assertIsNone(snapshot["dns"])
        self.assertEqual(snapshot["overall"], "ok")
        self.assertEqual(self.read_budget()["mapping_used"], 0)
        self.assertEqual(snapshot["cancel"]["cancelled"], False)
        exported = supervision.export_diagnostics(
            state_dir=self.state_dir, now=1000.0)
        self.assertIsNone(exported["snapshot"]["dns"])

    def test_pure_dns_disagreement_never_notifies_or_recovers(self):
        dns = _dns(
            "disagreement",
            sources=[
                _dns_source("cloudflare", "nxdomain", negative=300),
                _dns_source("google", "ok", addresses=["8.8.8.8"], ttl=60),
            ])
        probe = _probe_result("degraded", dns=dns, reason="dns_disagreement")
        probe["entries"] = [_entry("ok", 0)]
        result, _ = self.cycle(lambda url: probe)
        self.assertEqual(result["code"], "degraded")
        self.assertEqual(self.notify_calls, [])
        snapshot = self.snapshot()
        self.assertEqual(snapshot["overall"], "degraded")
        self.assertEqual(snapshot["dns"]["state"], "disagreement")
        self.assertEqual(snapshot["recovery"]["state"], "idle")
        budget = self.read_budget()
        self.assertEqual(budget["mapping_used"], 0)
        self.assertEqual(budget["ts_restart_used"], 0)

    def test_mixed_dns_and_tls_failure_keeps_the_real_notification(self):
        dns = _dns(
            "disagreement",
            sources=[
                _dns_source("cloudflare", "nxdomain", negative=300),
                _dns_source("google", "ok", addresses=["8.8.8.8"], ttl=60),
            ])
        result, _ = self.cycle(lambda url: _probe_result("degraded", dns=dns))
        self.assertEqual(result["code"], "degraded")
        self.assertEqual(self.notify_calls, ["share_public_degraded"])
        self.assertEqual(self.snapshot()["dns"]["state"], "disagreement")

    def test_dns_recovery_does_not_notify_recovered(self):
        prior = _dns(
            "disagreement",
            sources=[
                _dns_source("cloudflare", "nxdomain", negative=300),
                _dns_source("google", "ok", addresses=["8.8.8.8"], ttl=60),
            ])
        self.write_state(overall="degraded", reason="dns_disagreement",
                         entries=[_entry("ok", 0)], dns=prior)
        result, _ = self.cycle(lambda url: _probe_result("ok", dns=_dns(
            "ok", addresses=["8.8.8.8"], ttl=60,
            sources=[_dns_source("cloudflare", "ok",
                                 addresses=["8.8.8.8"], ttl=60),
                     _dns_source("google", "ok",
                                 addresses=["8.8.8.8"], ttl=60)])))
        self.assertEqual(result["code"], "ok")
        self.assertEqual(self.notify_calls, [])
        self.assertEqual(self.snapshot()["dns"]["state"], "ok")

    def test_dns_nxdomain_never_enters_recovery(self):
        dns = _dns(
            "nxdomain",
            sources=[
                _dns_source("cloudflare", "nxdomain", negative=300),
                _dns_source("google", "nxdomain", negative=300),
            ])
        result, _ = self.cycle(lambda url: _probe_result(
            "not_checked", dns=dns, reason="dns_nxdomain"))
        self.assertEqual(result["code"], "not_checked")
        self.assertEqual(self.notify_calls, [])
        self.assertEqual(self.snapshot()["dns"]["state"], "nxdomain")
        self.assertEqual(self.snapshot()["recovery"]["state"], "idle")
        budget = self.read_budget()
        self.assertEqual(budget["mapping_used"], 0)
        self.assertEqual(budget["ts_restart_used"], 0)


    def test_truncated_addresses_never_consume_recovery(self):
        dns = _dns(
            "ok",
            addresses=["8.8.8.8", "1.1.1.1", "9.9.9.9",
                       "149.112.112.112", "208.67.222.222"],
            sources=[
                _dns_source("cloudflare", "ok",
                            addresses=["8.8.8.8", "1.1.1.1"], ttl=60),
                _dns_source("google", "ok",
                            addresses=["9.9.9.9", "149.112.112.112",
                                       "208.67.222.222"], ttl=60),
            ])
        dns["addresses_truncated"] = True
        probe = _probe_result("all_failed", dns=dns)
        result, _ = self.cycle(lambda url: probe)
        self.assertEqual(result["code"], "incomplete")
        self.assertEqual(self.funnel_calls, [])
        self.assertEqual(self.ts.calls, [])
        budget = self.read_budget()
        self.assertEqual(budget["mapping_used"], 0)
        self.assertEqual(budget["ts_restart_used"], 0)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["overall"], "incomplete")
        self.assertEqual(snapshot["reason"], "addresses_truncated")
        self.assertTrue(snapshot["dns"]["addresses_truncated"])

    def _run_recovery_sequence(self, count):
        fail_dns = _dns(
            "disagreement",
            sources=[
                _dns_source("cloudflare", "nxdomain", negative=300),
                _dns_source("google", "ok", addresses=["8.8.8.8"], ttl=60),
            ])
        ok_dns = _dns(
            "ok", addresses=["8.8.8.8"], ttl=60,
            sources=[_dns_source("cloudflare", "ok",
                                 addresses=["8.8.8.8"], ttl=60),
                     _dns_source("google", "ok",
                                 addresses=["8.8.8.8"], ttl=60)])
        values = [_probe_result("all_failed", dns=fail_dns)
                  for _ in range(count - 1)]
        values.append(_probe_result("ok", dns=ok_dns))
        result, _ = self.cycle(lambda url: values.pop(0))
        self.assertEqual(result["code"], "recovered")
        snapshot = self.snapshot()
        self.assertEqual(snapshot["overall"], "ok")
        self.assertEqual(snapshot["dns"]["state"], "ok")
        exported = supervision.export_diagnostics(
            state_dir=self.state_dir, now=1000.0)
        self.assertEqual(exported["snapshot"]["dns"]["state"], "ok")

    def test_recovery_probe_refreshes_dns_after_mapping(self):
        self._run_recovery_sequence(3)

    def test_recovery_probe_refreshes_dns_after_ts_and_remapping(self):
        self._run_recovery_sequence(5)

    def test_tls_recovery_notification_survives_dns_only_round(self):
        ok_dns = _dns(
            "ok", addresses=["8.8.8.8"], ttl=60,
            sources=[_dns_source("cloudflare", "ok",
                                 addresses=["8.8.8.8"], ttl=60),
                     _dns_source("google", "ok",
                                 addresses=["8.8.8.8"], ttl=60)])
        dns_only = _dns(
            "disagreement",
            sources=[
                _dns_source("cloudflare", "nxdomain", negative=300),
                _dns_source("google", "ok", addresses=["8.8.8.8"], ttl=60),
            ])
        tls_degraded = _probe_result("degraded", dns=ok_dns)
        dns_round = _probe_result("degraded", dns=dns_only,
                                  reason="dns_disagreement")
        dns_round["entries"] = [_entry("ok", 0)]
        self.cycle(lambda url: tls_degraded, now=1000.0)
        self.cycle(lambda url: dns_round, now=1010.0)
        self.cycle(lambda url: _probe_result("ok", dns=ok_dns), now=1020.0)
        self.assertEqual(self.notify_calls,
                         ["share_public_degraded", "share_public_recovered"])
        self.assertEqual(self.snapshot()["notification"]["last_key"],
                         "recovered|ok")


class ServiceWatchdogTests(BasePublicSupervisionTest):
    """The apply-round TS service watchdog (start-only, gated, budgeted)."""

    def _cycle_with_service_url(self, probe, *, seams=None, apply=True,
                                sleeper=None):
        return supervision.run_cycle(
            apply=apply, now=1000.0, state_dir=self.state_dir,
            lock_dir=self.lock_dir,
            seams=self.seams() if seams is None else seams,
            probe=probe,
            url_provider=lambda: URL if self.ts.state == "running" else None,
            sleeper=sleeper or self.clock.sleep,
            monotonic=self.clock.monotonic)

    def test_stopped_service_is_started_before_a_not_checked_probe(self):
        self.ts.state = "stopped"
        probes = {"calls": []}

        def probe(url):
            probes["calls"].append(url)
            if url is None:
                return _probe_result(
                    "not_checked", url=None, reason="public_url_unavailable")
            return _probe_result("ok", url)

        result = self._cycle_with_service_url(probe)
        self.assertEqual(result["code"], "ok")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["overall"], "ok")
        self.assertEqual(self.ts.calls, ["start"])
        self.assertEqual(probes["calls"], [URL])
        self.assertEqual(self.read_budget()["service_start_used"], 1)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["recovery"]["action"], "ts_service_start")
        self.assertEqual(snapshot["recovery"]["stage"], "ts_service")
        self.assertEqual(snapshot["recovery"]["state"], "succeeded")
        self.assertEqual(snapshot["recovery"]["actions"][-1]["action"],
                         "ts_service_start")
        self.assertEqual(snapshot["recovery"]["actions"][-1]["result"], "ok")
        events = supervision.read_status(
            state_dir=self.state_dir, now=1000.0)["events"]
        self.assertIn("ts_service_start",
                      [item["action"] for item in events])
        self.assertIn("watchdog", Path(self.paths["log"]).read_text(
            encoding="utf-8"))

    def test_stopped_service_then_all_failed_still_recovers(self):
        self.ts.state = "stopped"
        result, _ = self.cycle(["all_failed", "all_failed", "ok"])
        self.assertEqual(result["code"], "recovered")
        self.assertEqual(self.ts.calls, ["start"])
        self.assertEqual(self.funnel_calls, ["on"])
        self.assertEqual(self.read_budget()["service_start_used"], 1)
        actions = result["snapshot"]["recovery"]["actions"]
        self.assertEqual(actions[0]["action"], "ts_service_start")
        self.assertEqual(actions[0]["result"], "ok")

    def test_watchdog_cancel_after_sent_start_is_unconfirmed_exit_two(self):
        # The start command was already sent; a cancel observed while confirming
        # the service must not be reported as "no command was sent".
        self.ts.state = "stopped"
        probes = {"calls": []}

        def on_start():
            supervision.request_cancel(
                "manual_estop", state_dir=self.state_dir)

        def probe(url):
            probes["calls"].append(url)
            return _probe_result("ok", url)

        self.ts.on_start = on_start
        result = self._cycle_with_service_url(probe)
        self.assertEqual(result["code"], "ts_service_start_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "cancel_requested")
        self.assertEqual(self.ts.calls, ["start"])
        self.assertEqual(probes["calls"], [])
        snapshot = self.snapshot()
        self.assertEqual(snapshot["recovery"]["state"], "failed")
        self.assertEqual(snapshot["recovery"]["action"], "ts_service_start")
        self.assertEqual(snapshot["recovery"]["stage"], "ts_service")
        self.assertEqual(snapshot["recovery"]["actions"][-1]["action"],
                         "ts_service_start")
        self.assertEqual(snapshot["recovery"]["actions"][-1]["result"],
                         "unconfirmed")
        self.assertEqual(snapshot["local"]["ts_service"], "unavailable")
        self.assertEqual(self.read_budget()["service_start_used"], 1)

    def test_watchdog_retry_after_sent_start_busy_is_unconfirmed(self):
        # The first start returned an unacknowledged result (it may have left).
        # A busy action lock on the retry must not turn that into a refusal.
        self.ts.state = "stopped"
        original = self.ts.control
        state = {"first": True}

        def control(action):
            if action == "start" and state["first"]:
                state["first"] = False
                self.ts.calls.append(action)
                return {"ok": False, "detail": "unconfirmed"}
            return original(action)

        lock = supervision.ResourceFileLock(
            self.lock_dir, supervision.ACTION_LOCK_RESOURCE,
            owner="test", timeout_s=0.2)
        held = {"lock": None}

        def sleeper(seconds):
            self.clock.sleep(seconds)
            if held["lock"] is None:
                lock.acquire()
                held["lock"] = lock

        probes = {"calls": []}

        def probe(url):
            probes["calls"].append(url)
            return _probe_result("ok", url)

        try:
            result = self._cycle_with_service_url(
                probe, seams=self.seams(**{"_TS_SERVICE_CONTROL": control}),
                sleeper=sleeper)
        finally:
            if held["lock"] is not None:
                lock.__exit__(None, None, None)
        self.assertEqual(result["code"], "ts_service_start_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "share_public_busy")
        self.assertEqual(self.ts.calls, ["start"])
        self.assertEqual(probes["calls"], [])
        snapshot = self.snapshot()
        self.assertEqual(snapshot["recovery"]["state"], "failed")
        self.assertEqual(snapshot["recovery"]["action"], "ts_service_start")
        self.assertEqual(snapshot["recovery"]["stage"], "ts_service")
        self.assertEqual(snapshot["recovery"]["actions"][-1]["action"],
                         "ts_service_start")
        self.assertEqual(snapshot["recovery"]["actions"][-1]["result"],
                         "unconfirmed")
        self.assertEqual(snapshot["local"]["ts_service"], "unavailable")
        self.assertEqual(self.read_budget()["service_start_used"], 1)

    def test_watchdog_retry_after_sent_start_security_change_blocks_start(self):
        # The security verdict must be re-read inside the action lock before the
        # retried start: a sent-but-unconfirmed first attempt is exit 2, and the
        # second start must never leave the process.
        self.ts.state = "stopped"
        original = self.ts.control
        state = {"first": True}

        def control(action):
            if action == "start" and state["first"]:
                state["first"] = False
                self.ts.calls.append(action)
                self.ops.verdict = None
                return {"ok": False, "detail": "unconfirmed"}
            return original(action)

        probes = {"calls": []}

        def probe(url):
            probes["calls"].append(url)
            return _probe_result("ok", url)

        result = self._cycle_with_service_url(
            probe, seams=self.seams(**{"_TS_SERVICE_CONTROL": control}))
        self.assertEqual(result["code"], "ts_service_start_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "security_unconfirmed")
        self.assertEqual(self.ts.calls, ["start"])
        self.assertEqual(probes["calls"], [])
        snapshot = self.snapshot()
        self.assertEqual(snapshot["recovery"]["state"], "failed")
        self.assertEqual(snapshot["recovery"]["stage"], "ts_service")
        self.assertEqual(snapshot["recovery"]["actions"][-1]["action"],
                         "ts_service_start")
        self.assertEqual(snapshot["recovery"]["actions"][-1]["result"],
                         "unconfirmed")
        self.assertEqual(self.read_budget()["service_start_used"], 1)

    def test_watchdog_retry_after_sent_start_permanent_refusal_is_unconfirmed(
            self):
        # The first start was sent but unacknowledged; the retry's permanent
        # refusal must keep the already-sent start unconfirmed (exit 2).
        self.ts.state = "stopped"
        original = self.ts.control
        state = {"first": True}

        def control(action):
            if action == "start" and state["first"]:
                state["first"] = False
                self.ts.calls.append(action)
                return {"ok": False, "detail": "unconfirmed"}
            if action == "start":
                self.ts.calls.append(action)
                raise OperationError("share_ts_access_denied", "denied")
            return original(action)

        probes = {"calls": []}

        def probe(url):
            probes["calls"].append(url)
            return _probe_result("ok", url)

        result = self._cycle_with_service_url(
            probe, seams=self.seams(**{"_TS_SERVICE_CONTROL": control}))
        self.assertEqual(result["code"], "ts_service_start_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "manual_required")
        self.assertEqual(self.ts.calls, ["start", "start"])
        self.assertEqual(probes["calls"], [])
        self.assertEqual(self.read_budget()["service_start_used"], 2)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["recovery"]["state"], "failed")
        self.assertEqual(snapshot["recovery"]["stage"], "ts_service")
        self.assertEqual(snapshot["recovery"]["actions"][-1]["result"],
                         "unconfirmed")

    def test_watchdog_retry_after_sent_start_seams_unavailable_is_unconfirmed(
            self):
        # The first start was sent but unacknowledged; losing the control seam
        # before the retry must not report "no command was sent".
        self.ts.state = "stopped"
        original = self.ts.control
        seams = self.seams()
        state = {"first": True}

        def control(action):
            if action == "start" and state["first"]:
                state["first"] = False
                self.ts.calls.append(action)
                seams.pop("_TS_SERVICE_CONTROL", None)
                return {"ok": False, "detail": "unconfirmed"}
            return original(action)

        seams["_TS_SERVICE_CONTROL"] = control
        probes = {"calls": []}

        def probe(url):
            probes["calls"].append(url)
            return _probe_result("ok", url)

        result = self._cycle_with_service_url(probe, seams=seams)
        self.assertEqual(result["code"], "ts_service_start_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "seams_unavailable")
        self.assertEqual(self.ts.calls, ["start"])
        self.assertEqual(probes["calls"], [])
        snapshot = self.snapshot()
        self.assertEqual(snapshot["recovery"]["state"], "failed")
        self.assertEqual(snapshot["recovery"]["stage"], "ts_service")
        self.assertEqual(snapshot["recovery"]["actions"][-1]["result"],
                         "unconfirmed")

    def test_watchdog_retry_after_sent_start_budget_is_unconfirmed(self):
        # The first start consumed a slot but was unacknowledged. The retry
        # finds the separate service-start budget exhausted; the already-sent
        # start must stay unconfirmed (exit 2), never a budget refusal.
        self.ts.state = "stopped"
        original = self.ts.control
        state = {"first": True}

        def control(action):
            if action == "start" and state["first"]:
                state["first"] = False
                self.ts.calls.append(action)
                self.write_budget(service_starts=[
                    supervision._utc_iso(993.0),
                    supervision._utc_iso(995.0),
                    supervision._utc_iso(999.0)])
                return {"ok": False, "detail": "unconfirmed"}
            return original(action)

        probes = {"calls": []}

        def probe(url):
            probes["calls"].append(url)
            return _probe_result("ok", url)

        result = self._cycle_with_service_url(
            probe, seams=self.seams(**{"_TS_SERVICE_CONTROL": control}))
        self.assertEqual(result["code"], "ts_service_start_unconfirmed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "budget_exhausted")
        self.assertEqual(self.ts.calls, ["start"])
        self.assertEqual(probes["calls"], [])
        self.assertEqual(self.read_budget()["service_start_used"], 3)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["recovery"]["state"], "failed")
        self.assertEqual(snapshot["recovery"]["stage"], "ts_service")
        self.assertEqual(snapshot["recovery"]["actions"][-1]["result"],
                         "unconfirmed")

    def test_watchdog_permanent_refusal_is_an_honest_failure(self):
        self.ts.state = "stopped"
        probes = {"calls": []}

        def control(action):
            self.ts.calls.append(action)
            raise OperationError("share_ts_access_denied", "denied")

        def probe(url):
            probes["calls"].append(url)
            return _probe_result("ok", url)

        result = self._cycle_with_service_url(
            probe, seams=self.seams(**{"_TS_SERVICE_CONTROL": control}))
        self.assertEqual(result["code"], "manual_required")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["overall"], "not_checked")
        self.assertEqual(self.ts.calls, ["start"])
        self.assertEqual(probes["calls"], [])
        snapshot = self.snapshot()
        self.assertEqual(snapshot["overall"], "not_checked")
        self.assertEqual(snapshot["recovery"]["state"], "manual_required")
        self.assertEqual(snapshot["recovery"]["action"], "ts_service_start")
        self.assertEqual(snapshot["recovery"]["actions"][-1]["result"],
                         "failed")
        self.assertIn("share_public_manual_required", self.notify_calls)

    def test_watchdog_does_not_run_in_check_mode(self):
        self.ts.state = "stopped"
        result, sequence = self.cycle(["not_checked"], apply=False)
        self.assertEqual(result["code"], "not_checked")
        self.assertEqual(self.ts.calls, [])
        self.assertFalse(os.path.exists(self.paths["state"]))
        self.assertFalse(os.path.exists(self.paths["budget"]))

    def test_watchdog_respects_maintenance_and_security(self):
        self.ts.state = "stopped"
        self.maintenance = lambda: {
            "lease": {"lease_id": "l1", "component": "c"},
            "lease_expired": False}
        result, _ = self.cycle(["ok"])
        self.assertEqual(result["code"], "maintenance_in_force")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(self.ts.calls, [])
        self.assertEqual(self.read_budget()["service_start_used"], 0)

        self.maintenance = lambda: {"lease": None, "lease_expired": False}
        self.ops.verdict = False
        result, _ = self.cycle(["ok"], now=1100.0)
        self.assertEqual(result["code"], "security_not_satisfied")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(self.ts.calls, [])

    def test_watchdog_respects_a_managed_stop(self):
        self.ts.state = "stopped"
        self.desired_stop = {"state": "stopped", "stopped_at": "t"}
        result, _ = self.cycle(["ok"])
        self.assertEqual(result["code"], "managed_stop")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(self.ts.calls, [])
        self.assertEqual(self.read_budget()["service_start_used"], 0)
        self.assertEqual(self.snapshot()["local"]["lifecycle"], "stopped")

    def test_service_start_budget_exhausted_does_not_probe_or_act(self):
        self.ts.state = "stopped"
        self.write_state(observed=900.0)
        self.write_budget(service_starts=[
            supervision._utc_iso(970.0),
            supervision._utc_iso(980.0),
            supervision._utc_iso(990.0)])
        probes = {"calls": []}

        def probe(url):
            probes["calls"].append(url)
            return _probe_result("ok", url)

        result = self._cycle_with_service_url(probe)
        self.assertEqual(result["code"], "budget_exhausted")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["overall"], "not_checked")
        self.assertEqual(self.ts.calls, [])
        self.assertEqual(probes["calls"], [])
        self.assertEqual(self.read_budget()["service_start_used"], 3)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["recovery"]["state"], "budget_exhausted")
        self.assertEqual(snapshot["recovery"]["action"], "ts_service_start")
        self.assertEqual(snapshot["recovery"]["stage"], "ts_service")
        self.assertEqual(snapshot["recovery"]["actions"][-1]["result"],
                         "skipped")


class TsRestartSafetyTests(BasePublicSupervisionTest):
    """A. ts_restart never leaves Tailscale stopped."""

    def test_connectivity_unconfirmed_before_stop_never_stops(self):
        state = {"mapped": False}

        def funnel(action):
            self.funnel_calls.append(action)
            state["mapped"] = True
            return {"ok": True, "detail": "stub"}

        def connectivity():
            return not state["mapped"]

        result, _ = self.cycle(
            ["all_failed", "all_failed"],
            seams=self.seams(**{"_FUNNEL_CONTROL": funnel,
                                "_CONNECTIVITY_OK": connectivity}))
        self.assertEqual(result["code"], "tailscale_offline")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(self.ts.calls, [])
        self.assertEqual(self.funnel_calls, ["on"])
        recovery = result["snapshot"]["recovery"]
        self.assertEqual(recovery["state"], "failed")
        self.assertEqual(recovery["stage"], "ts_stop")
        self.assertEqual(recovery["actions"][-1]["action"], "mapping_resend")
        self.assertEqual(recovery["actions"][-1]["result"], "ok")

    def test_connectivity_unconfirmed_after_stop_still_starts(self):
        def connectivity():
            return self.ts.state == "running"

        result, _ = self.cycle(
            ["all_failed", "all_failed", "all_failed", "ok"],
            seams=self.seams(**{"_CONNECTIVITY_OK": connectivity}))
        self.assertEqual(result["code"], "recovered")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(self.ts.calls, ["stop", "start"])
        self.assertEqual(self.funnel_calls, ["on"])

    def test_start_temporary_failure_retries_once_and_recovers(self):
        original = self.ts.control
        state = {"failed": False}

        def control(action):
            if action == "start" and not state["failed"]:
                state["failed"] = True
                self.ts.calls.append(action)
                raise OSError("temporary start failure")
            return original(action)

        result, _ = self.cycle(
            ["all_failed", "all_failed", "all_failed", "all_failed", "ok"],
            seams=self.seams(**{"_TS_SERVICE_CONTROL": control}))
        self.assertEqual(result["code"], "recovered")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(self.ts.calls, ["stop", "start", "start"])
        self.assertEqual(self.funnel_calls, ["on", "on"])


class ServiceStartBudgetTests(BasePublicSupervisionTest):
    def test_old_budget_without_service_start_key_reads_as_empty(self):
        self.write_state(observed=900.0)
        self.write_budget(include_service_starts=False)
        view = self.read_budget()
        self.assertFalse(view["unreadable"])
        self.assertEqual(view["service_start_used"], 0)
        self.assertEqual(view["service_start_limit"], 3)
        self.assertEqual(view["service_starts_utc"], [])

        self.ts.state = "stopped"
        result, _ = self.cycle(["ok"])
        self.assertEqual(result["code"], "ok")
        self.assertEqual(self.ts.calls, ["start"])
        raw = json.loads(Path(self.paths["budget"]).read_text(
            encoding="utf-8"))
        self.assertEqual(len(raw["service_starts_utc"]), 1)
        self.assertEqual(self.read_budget()["service_start_used"], 1)

    def test_service_start_budget_is_separate_from_mapping_and_restart(self):
        self.ts.state = "stopped"
        self.write_state(observed=900.0)
        self.write_budget(mapping=[supervision._utc_iso(990.0)],
                           restarts=[supervision._utc_iso(995.0)])
        result, _ = self.cycle(["ok"])
        self.assertEqual(result["code"], "ok")
        budget = self.read_budget()
        self.assertEqual(budget["mapping_used"], 1)
        self.assertEqual(budget["ts_restart_used"], 1)
        self.assertEqual(budget["service_start_used"], 1)


if __name__ == "__main__":
    unittest.main()
