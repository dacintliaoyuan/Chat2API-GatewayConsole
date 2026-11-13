# -*- coding: utf-8 -*-
"""Isolated tests for the UM share / TS layered orchestration adapter.

Every privileged and OS boundary is INJECTED: broker ops stub (programmable
success/failure sequences), TS service / connectivity / funnel / drain seam
stubs, a recording sleep. No real scheduled task, no real TS binary, no pipe,
no network is touched. An AST self-check asserts the module contains ZERO
subprocess/task-tool/binary-path literals — every OS access goes through a
seam.

Tested contract (Revision 8, five-layer):
* LAYERS = (share_backend, ts_service, connectivity, backend, funnel); open
  runs the FIVE layers in order; failure rolls back ONLY the layers this run
  owns and reports layered residual facts (an owned dedicated-backend start
  is rolled back with its stop fact);
* share_backend open layer: the key precondition (>=1 enabled share key +
  config in sync) runs first, then the dedicated-backend observation — a
  pre-existing running backend with valid upstream auth is SKIPPED without
  ownership, anything unconfirmed/invalid fails with the relogin guidance;
* close = entry withdrawal -> bounded drain -> backend stop -> dedicated
  backend stop -> TS stop, with the managed-stop intent expressed BEFORE the
  stopped terminal is written; an unconfirmed share_backend residual keeps
  the stop intent in force and NEVER writes the stopped terminal;
* a partial close never displays "all closed" and names the retry entry;
* emergency stop is a quick chain that withdraws the entry first and records
  the dedicated-backend failure honestly (never a fake all-closed);
* share_status is read-only and honest about unwired seams, observing the
  dedicated backend through its own three-state seam.
"""

import ast
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlparse

sys.path.insert(0, str(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from console import share_actions as m  # noqa: E402  (same directory on sys.path)
from console.operations import OperationCancelled, OperationError  # noqa: E402


def setUpModule():
    """Refuse sentinel (planner L6-2): the test-default entry rejects any
    REAL TS/pipe/service command — every test injects its runner or seam
    explicitly, so real subprocess execution of tailscale/schtasks/umshare
    commands from this suite fails loudly instead of touching the system.
    Non-guarded commands (e.g. plain python) delegate to the real run.
    """
    import subprocess as _subprocess

    _real_run = _subprocess.run

    def _guarded_run(cmd, *args, **kwargs):
        joined = " ".join(map(str, cmd)) if isinstance(cmd, (list, tuple))             else str(cmd)
        lowered = joined.lower()
        for token in ("tailscale", "schtasks", "umshare", "pwsh", "sc.exe"):
            if token in lowered:
                raise AssertionError(
                    "test attempted a REAL %s command: %r" % (token, cmd))
        if isinstance(cmd, (list, tuple)) and cmd:
            if os.path.basename(str(cmd[0])).lower() in ("sc", "sc.exe"):
                raise AssertionError(
                    "test attempted a REAL sc.exe command: %r" % (cmd,))
        return _real_run(cmd, *args, **kwargs)

    _subprocess.run = _guarded_run


def _seq(values):
    """Scalar or ordered list -> generator whose FINAL value repeats, so an
    unexpected extra poll never crashes a test; exception instances are
    raised when consumed."""
    if not isinstance(values, (list, tuple)):
        values = [values]
    remaining = list(values)
    while True:
        if len(remaining) > 1:
            yield remaining.pop(0)
        else:
            yield remaining[0]


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






class _BrokerStub:
    """Programmable restricted-gate stub. Keyword values are scalars, ordered
    lists or exception instances; the final value repeats. Defaults match the
    production gate surface shape."""

    _DEFAULTS = {
        "verify_running": False,
        "verify_closed": True,
        "start_host": True,
        "stop_host": True,
        "drain": {"drained": True, "timed_out": False, "stopped": True},
        "emergency_stop": {"stopped": True},
        "host_evidence": {"check": "limits", "verdict": True, "detail": "ok",
                          "checked_at": 1790000000},
    }

    def __init__(self, **seqs):
        self.calls = []
        self.start_host_calls = []
        self._seqs = {k: _seq(v) for k, v in seqs.items()}
        self.on_call = None

    def _next(self, name):
        self.calls.append(name)
        if self.on_call is not None:
            self.on_call(name)
        value = next(self._seqs.get(name) or _seq(self._DEFAULTS[name]))
        if isinstance(value, BaseException):
            raise value
        return value

    def verify_running(self):
        return self._next("verify_running")

    def verify_closed(self):
        return self._next("verify_closed")

    def start_host(self, explicit=False):
        self.start_host_calls.append(explicit)
        return self._next("start_host")

    def stop_host(self):
        return self._next("stop_host")

    def drain(self):
        return self._next("drain")

    def emergency_stop(self):
        return self._next("emergency_stop")

    def host_evidence(self, check):
        self.calls.append(("host_evidence", check))
        if self.on_call is not None:
            self.on_call(("host_evidence", check))
        value = next(self._seqs.get("host_evidence")
                      or _seq(self._DEFAULTS["host_evidence"]))
        if isinstance(value, BaseException):
            raise value
        return value


_UNSET = object()  # distinguishes "not passed" from an explicit None (unwired)

_RUNNING_VALID = {"running": True, "auth_state": "valid",
                  "auth_label": "认证有效"}
_NOT_RUNNING = {"running": False, "auth_state": None, "auth_label": None}
_LOGGING_IN = {"running": True, "auth_state": "logging_in",
                "auth_label": "登录中"}






class _KeysStub:
    """_KEYS_CONFIRMED open-precondition stub: records calls and returns the
    programmed verdict (True/False/None)."""

    def __init__(self, result=True):
        self.calls = 0
        self._result = result

    def __call__(self):
        self.calls += 1
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


class _KeysDiagnosisStub:
    """_KEYS_DIAGNOSIS precondition-diagnosis stub: records calls and
    returns the programmed diagnosis dict (or raises it when given an
    exception)."""

    def __init__(self, result=None):
        self.calls = 0
        self._result = result

    def __call__(self):
        self.calls += 1
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


# Production seam wiring (console_adapters._wire_share_production_seams)
# mutates the orchestration module's seam globals at registry build; these
# tests assert the documented unwired defaults, so the base pins the
# pristine seam values per test and restores whatever preceded it.
_SHARE_SEAM_GLOBALS = ("_BROKER_OPS", "_DESIRED_STOP_READER",
                       "_KEYS_CONFIRMED", "_KEYS_DIAGNOSIS",
                       # Rev9 single-layer seams (contract §3.5/§6.3)
                       "_MAINTENANCE_BEGIN", "_MAINTENANCE_END",
                       "_MAINTENANCE_STATUS", "_GATEWAY_RESTART")


class ShareActionsTestBase(unittest.TestCase):
    """Wires the module seams to fresh stubs; resets the managed-stop state
    per wiring so tests never leak orchestration state."""

    def setUp(self):
        self._seam_backup = {name: getattr(m, name, None)
                             for name in _SHARE_SEAM_GLOBALS}
        for name in _SHARE_SEAM_GLOBALS:
            setattr(m, name, None)
        self.addCleanup(self._restore_seams)

    def _restore_seams(self):
        for name, value in self._seam_backup.items():
            setattr(m, name, value)

    def _wire(self, broker=None, desired_reader=None, keys_confirmed=_UNSET,
              keys_diagnosis=None, maintenance=None, gateway_restart=None):
        sleep_calls = []
        state = m._ManagedStopState()

        def patch(name, value, create=False):
            patcher = mock.patch.object(m, name, value, create=create)
            patcher.start()
            self.addCleanup(patcher.stop)

        keys = keys_confirmed if keys_confirmed is not _UNSET \
            else _KeysStub(True)
        patch("_KEYS_CONFIRMED", keys)
        patch("_KEYS_DIAGNOSIS", keys_diagnosis)
        patch("_BROKER_OPS", broker)          # None = gate unwired
        patch("_DESIRED_STOP_READER", desired_reader)
        # Rev9 single-layer seams: unwired by default (honest refusal).
        patch("_MAINTENANCE_BEGIN",
              maintenance.begin if maintenance is not None else None)
        patch("_MAINTENANCE_END",
              maintenance.end if maintenance is not None else None)
        patch("_MAINTENANCE_STATUS",
              maintenance.status if maintenance is not None else None)
        patch("_GATEWAY_RESTART", gateway_restart)
        patch("_SLEEP", sleep_calls.append)
        patch("_STATE", state)
        return SimpleNamespace(broker=broker, keys=keys,
                               keys_diagnosis=keys_diagnosis,
                               maintenance=maintenance,
                               gateway_restart=gateway_restart,
                               sleep_calls=sleep_calls, state=state)

    def _first_step_index(self, ctx, fragment):
        for index, step in enumerate(ctx.steps):
            if fragment in step:
                return index
        self.fail("step not recorded: %s" % fragment)






class TestCancelAndStatus(ShareActionsTestBase):
    """Cases 9-10: read-only status."""

    def test_10_status_readonly_unavailable_then_injected_passthrough(self):
        ctx = _Ctx()
        # Production default: the broker gate is unwired -> honest unavailable.
        result = m.share_status("um-share", {}, ctx)
        self.assertEqual(result["layers"],
                         {"share_gateway": "unavailable"})
        self.assertIsNone(result["desired_stop_in_force"])
        self.assertIn("如实", result["note"])

        # Injected seam: layered passthrough of the actual observation.
        broker = _BrokerStub(verify_running=True)
        w = self._wire(broker=broker, desired_reader=lambda: True)
        status = m.share_status("um-share", {}, ctx)
        self.assertEqual(status["layers"], {"share_gateway": "running"})
        self.assertIs(status["desired_stop_in_force"], True)
        # Read-only: the restricted gate was observed, never mutated.
        self.assertEqual(broker.calls, ["verify_running"])
        self.assertEqual(w.sleep_calls, [])


_LEASE_ID = "a" * 32


def _lease_begin(**over):
    out = {"accepted": True, "component": "share_gateway",
           "lease_id": _LEASE_ID, "lease_expires_at": 1790000300,
           "instance_id": "inst-1", "round": 7, "state": "draining",
           "active_count": 3}
    out.update(over)
    return out


def _lease_end(**over):
    out = {"resumed": True, "component": "share_gateway",
           "lease_id": _LEASE_ID, "instance_id": "inst-1", "round": 7,
           "state": "running", "active_count": 0}
    out.update(over)
    return out


def _maint_view(**over):
    out = {"checked_at": 1790000000,
           "lease": {"lease_id": _LEASE_ID, "component": "share_gateway",
                     "instance_id": "inst-1", "round": 7,
                     "created_at": 1790000000, "expires_at": 1790000300,
                     "active_count_at_begin": 3},
           "lease_expired": False,
           "instance": {"instance_id": "inst-1", "state": "running",
                        "active_count": 0, "last_seen_generation": 12},
           "last_outcome": None}
    out.update(over)
    return out


class _MaintenanceStub:
    """Injected maintenance seams (A9-3): records calls, returns payloads."""

    def __init__(self, status=None):
        self.status_payloads = list(status or [])
        self.begin_calls = []
        self.end_calls = []
        self.status_calls = 0

    def begin(self, component, lease_s):
        self.begin_calls.append((component, lease_s))
        return _lease_begin()

    def end(self, component, lease_id):
        self.end_calls.append((component, lease_id))
        return _lease_end()

    def status(self):
        self.status_calls += 1
        if self.status_payloads:
            return self.status_payloads.pop(0)
        return _maint_view()


class TestShareDiagnostics(ShareActionsTestBase):
    """Rev9 A9-1: the read-only diagnostics surface."""

    def _wire_diag(self, broker=_UNSET, maintenance=_UNSET,
                   desired_reader=lambda: False):
        broker = broker if broker is not _UNSET else _BrokerStub(
            verify_running=True)
        maintenance = (maintenance if maintenance is not _UNSET
                       else _MaintenanceStub())
        return self._wire(broker=broker, maintenance=maintenance,
                          desired_reader=desired_reader)

    def test_11_diagnostics_retained_layers_confirmed(self):
        now = time.time()
        broker = _BrokerStub(verify_running=True,
                             host_evidence={"check": "limits",
                                            "verdict": True, "detail": "ok",
                                            "checked_at": now})
        self._wire_diag(broker=broker)
        with mock.patch.object(
                m, "_share_entry_probe",
                lambda: {"pid": 54300, "started_at": now,
                         "reason": "ok"}),              mock.patch.object(
                m, "_share_entry_attribution",
                lambda: {"file_ready": True, "reason": "ok",
                         "instance_loaded": {"value": "yes",
                                             "basis": "inference"}}):
            report = m.diagnostics("share", {}, _Ctx())
        self.assertEqual(set(report["layers"]), set(m._DIAG_LAYER_KEYS))
        layers = report["layers"]
        self.assertEqual(layers["share_gateway"]["state"], "running")
        self.assertEqual(layers["share_gateway"]["verdict"], "confirmed")
        self.assertEqual(
            layers["share_gateway"]["evidence"]["attribution"]
            ["instance_loaded"]["value"], "yes")
        self.assertEqual(layers["security_gate"]["state"], "ok")
        for key in m._DIAG_LAYER_KEYS:
            entry = layers[key]
            self.assertEqual(entry["ttl_s"], m._DIAG_TTL_S[key], key)
            self.assertIsNotNone(entry["checked_at"], key)
            self.assertGreaterEqual(entry["age_s"], 0.0, key)
            self.assertFalse(entry["stale"], key)
            self.assertIsNone(entry["reason"], key)
        self.assertEqual(report["maintenance"]["lease"]["lease_id"],
                         _LEASE_ID)
        self.assertIsNone(report["last_error"])
        self.assertIs(report["desired_stop_in_force"], False)
        self.assertIn("进程在", report["note"])

    def test_14_stale_uses_evidence_observation_time(self):
        now = time.time()
        old = now - 1000.0
        broker = _BrokerStub(verify_running=True,
                             host_evidence={"check": "limits",
                                            "verdict": True, "detail": "ok",
                                            "checked_at": old})
        self._wire_diag(broker=broker)
        report = m.diagnostics("share", {}, _Ctx())
        gate = report["layers"]["security_gate"]
        self.assertEqual(gate["checked_at"], old)
        self.assertTrue(gate["stale"])

    def test_15_diagnostics_is_read_only(self):
        broker = _BrokerStub(verify_running=True)
        w = self._wire_diag(broker=broker)
        m.diagnostics("share", {}, _Ctx())
        # Read-only: the broker gate is only observed.
        self.assertEqual(broker.calls, [
            "verify_running", ("host_evidence", "faults"),
            ("host_evidence", "limits")])
        self.assertEqual(w.maintenance.status_calls, 1)
        self.assertEqual(w.maintenance.begin_calls, [])
        self.assertEqual(w.maintenance.end_calls, [])

    def test_16_maintenance_view_and_last_error(self):
        outcome = {"at": 1790000000, "action": "gateway-restart", "ok": False,
                   "code": "unconfirmed", "detail": "stub: 未确认"}
        maint = _MaintenanceStub(status=[_maint_view(last_outcome=outcome)])
        self._wire_diag(maintenance=maint)
        report = m.diagnostics("share", {}, _Ctx())
        self.assertEqual(report["maintenance"]["lease"]["lease_id"],
                         _LEASE_ID)
        self.assertEqual(report["last_error"], outcome)

    def test_17_unwired_diagnostics_is_honest(self):
        self._wire_diag(broker=None, maintenance=None)
        report = m.diagnostics("share", {}, _Ctx())
        self.assertEqual(report["layers"]["share_gateway"]["state"],
                         "unavailable")
        self.assertEqual(report["layers"]["share_gateway"]["reason"],
                         "share_broker_unwired")
        self.assertEqual(report["layers"]["security_gate"]["state"],
                         "unavailable")
        self.assertIn("维护视图未接线", report["note"])



class TestGatewayRestart(ShareActionsTestBase):
    """Rev9 A9-4: restart 8100 only, through the restricted broker verb."""

    def _wire_gateway(self, broker=None, gateway_restart=None,
                      maintenance=None):
        broker = broker if broker is not None else _BrokerStub(
            verify_running=True)
        maintenance = maintenance if maintenance is not None \
            else _MaintenanceStub()
        return self._wire(broker=broker, gateway_restart=gateway_restart,
                          maintenance=maintenance)

    def test_40_limits_false_refuses_without_broker_call(self):
        calls = []
        broker = _BrokerStub(host_evidence={"check": "limits",
                                            "verdict": False,
                                            "detail": "stub: 安全门失败"})
        self._wire_gateway(broker=broker,
                            gateway_restart=lambda budget: calls.append(budget))
        with self.assertRaises(OperationError) as caught:
            m.gateway_restart("share", {}, _Ctx())
        self.assertEqual(caught.exception.code,
                         "share_entry_restart_failed")
        self.assertIn("安全门未通过", caught.exception.message)
        self.assertEqual(calls, [])

    def test_41_limits_none_refuses_without_broker_call(self):
        calls = []
        broker = _BrokerStub(host_evidence={"check": "limits",
                                            "verdict": None,
                                            "detail": "stub: 不可裁定"})
        self._wire_gateway(broker=broker,
                            gateway_restart=lambda budget: calls.append(budget))
        with self.assertRaises(OperationError) as caught:
            m.gateway_restart("share", {}, _Ctx())
        self.assertEqual(caught.exception.code,
                         "share_entry_restart_failed")
        self.assertEqual(calls, [])

    def test_42_unconfirmed_broker_result_fails(self):
        broker = _BrokerStub(verify_running=True)
        self._wire_gateway(broker=broker, gateway_restart=lambda budget: {
            "restarted": True, "component": "share_gateway",
            "stopped_confirmed": False, "running_confirmed": True,
            "elapsed_s": 41.2, "maintenance_state": "restarting"})
        with self.assertRaises(OperationError) as caught:
            m.gateway_restart("share", {}, _Ctx())
        self.assertEqual(caught.exception.code,
                         "share_entry_restart_failed")
        self.assertIn("stopped_confirmed=False", caught.exception.message)

    def test_43_success_returns_before_after_and_gateway(self):
        calls = []
        broker = _BrokerStub(verify_running=True)
        self._wire_gateway(broker=broker, gateway_restart=lambda budget: (
            calls.append(budget) or {
                "restarted": True, "component": "share_gateway",
                "stopped_confirmed": True, "running_confirmed": True,
                "elapsed_s": 41.2, "maintenance_state": "idle"}))
        result = m.gateway_restart("share", {"restart_budget_s": 120},
                                   _Ctx())
        self.assertTrue(result["restarted"])
        self.assertEqual(calls, [120])
        self.assertEqual(result["gateway"]["component"], "share_gateway")
        self.assertIs(result["gateway"]["stopped_confirmed"], True)
        self.assertIs(result["gateway"]["running_confirmed"], True)
        self.assertEqual(result["gateway"]["elapsed_s"], 41.2)
        self.assertEqual(result["gateway"]["maintenance_state"], "idle")
        self.assertEqual(set(result["before"]), set(m._DIAG_LAYER_KEYS))
        self.assertEqual(set(result["after"]), set(m._DIAG_LAYER_KEYS))
        self.assertEqual(result["maintenance"]["lease"]["lease_id"],
                         _LEASE_ID)
        self.assertIn("share.stop→share.start", result["note"])

    def test_44_cancel_after_side_effect_returns_actual_result(self):
        broker = _BrokerStub(verify_running=True)
        ctx = _Ctx()

        def restart(budget):
            ctx.event.set()      # cancel arrives DURING the blocking call
            return {"restarted": True, "component": "share_gateway",
                    "stopped_confirmed": True, "running_confirmed": True,
                    "elapsed_s": 41.2, "maintenance_state": "idle"}

        self._wire_gateway(broker=broker, gateway_restart=restart)
        result = m.gateway_restart("share", {}, ctx)
        self.assertTrue(result["restarted"])
        self.assertIn("取消已请求", " ".join(ctx.steps))
        self.assertIn("按实际结果返回", " ".join(ctx.steps))

    def test_45_gateway_restart_unwired_refuses(self):
        broker = _BrokerStub(verify_running=True)
        self._wire_gateway(broker=broker, gateway_restart=None)
        with self.assertRaises(OperationError) as caught:
            m.gateway_restart("share", {}, _Ctx())
        self.assertEqual(caught.exception.code, "share_broker_unwired")
