# -*- coding: utf-8 -*-
"""D18-6/D18-8: the console read face for the dedicated 8110 auth state.

``backend_auth_read()`` is READ-ONLY: it reads the existing observation seam
(``forget_stale=False``) and returns the display card fields — it never triggers
a login, never starts/stops the dedicated instance and never mutates the
orchestration stop state. ``backend_login_cancel()`` cancels the current login
round through the dedicated instance's control-token endpoint and never stops the
service.

Every OS/network boundary is injected; no real request is issued.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from console import share_actions as m  # noqa: E402
from console.operations import OperationError  # noqa: E402

_SEAM_GLOBALS = ("_BACKEND_OBSERVE", "_BACKEND_OPS", "_BROKER_OPS",
                 "_BACKEND_LOGIN", "_BACKEND_LOGIN_CANCEL", "_STATE")


class _ObserveStub:
    """Read-only observation seam stub; records ``forget_stale``."""

    def __init__(self, obs):
        self.obs = obs
        self.calls = []

    def __call__(self, forget_stale=True):
        self.calls.append(forget_stale)
        if isinstance(self.obs, BaseException):
            raise self.obs
        return self.obs


class _GateStub:
    """Backend/broker gate stub that FAILS on any mutation call."""

    def __init__(self):
        self.calls = []

    def _fail(self, name):
        def raise_():
            self.calls.append(name)
            raise AssertionError("%s must not be called by a read" % name)
        return raise_

    def __getattr__(self, name):
        return self._fail(name)


class BackendAuthReadTests(unittest.TestCase):
    def setUp(self):
        self.saved = {name: getattr(m, name) for name in _SEAM_GLOBALS}
        self.addCleanup(self._restore)

    def _restore(self):
        for name, value in self.saved.items():
            setattr(m, name, value)

    def _wire(self, observe, backend=None, broker=None, login=None,
              cancel=None):
        m._BACKEND_OBSERVE = observe
        m._BACKEND_OPS = backend if backend is not None else _GateStub()
        m._BROKER_OPS = broker if broker is not None else _GateStub()
        m._BACKEND_LOGIN = login
        m._BACKEND_LOGIN_CANCEL = cancel

    def test_01_reads_the_observation_fields_without_any_side_effect(self):
        observe = _ObserveStub({
            "running": True,
            "auth_state": "logging_in",
            "auth_label": "登录中（人工窗口已开启）",
            "auth_error": None,
            "auth_recovery": {"action": "wait", "next_allowed_epoch": None},
            "observed_at": 1790000000.5,
            "login_window_deadline_epoch": 1790001800.0,
        })
        backend = _GateStub()
        broker = _GateStub()
        login_calls = []
        self._wire(observe, backend=backend, broker=broker,
                   login=lambda: login_calls.append("login"))
        out = m.backend_auth_read()
        self.assertEqual(out, {
            "state": "logging_in",
            "label": "登录中（人工窗口已开启）",
            "error": None,
            "recovery": {"action": "wait", "next_allowed_epoch": None},
            "observed_at": 1790000000.5,
            "login_window_deadline_epoch": 1790001800.0,
        })
        # Read-only: forget_stale=False (never deletes a stale pid file) and
        # no login/start/stop/drain/emergency call is ever made.
        self.assertEqual(observe.calls, [False])
        self.assertEqual(login_calls, [])
        self.assertEqual(backend.calls, [])
        self.assertEqual(broker.calls, [])

    def test_02_unwired_observation_returns_unknown_without_raising(self):
        self._wire(None)
        out = m.backend_auth_read()
        self.assertEqual(out, {
            "state": None, "label": None, "error": None, "recovery": None,
            "observed_at": None, "login_window_deadline_epoch": None})

    def test_03_observation_failure_is_honest_and_read_only(self):
        self._wire(_ObserveStub(OperationError(
            "share_backend_probe_failed", "probe failed")))
        with self.assertRaises(OperationError) as caught:
            m.backend_auth_read()
        self.assertEqual(caught.exception.code, "share_backend_probe_failed")

    def test_04_login_window_deadline_passes_through_verbatim(self):
        observe = _ObserveStub({
            "running": True, "auth_state": "logging_in",
            "auth_label": "登录中", "auth_error": None,
            "auth_recovery": None, "observed_at": 1790000001,
            "login_window_deadline_epoch": 1790001800})
        self._wire(observe)
        out = m.backend_auth_read()
        self.assertEqual(out["login_window_deadline_epoch"], 1790001800)
        self.assertEqual(out["observed_at"], 1790000001)

    def test_05_login_cancel_calls_the_production_seam_and_never_stops(self):
        sentinel = {"cancelled": True, "state": "cancelled", "detail": None}
        cancel = mock.Mock(return_value=sentinel)
        backend = _GateStub()
        broker = _GateStub()
        self._wire(_ObserveStub(None), backend=backend, broker=broker,
                   cancel=cancel)
        out = m.backend_login_cancel("share", {})
        self.assertIs(out, sentinel)
        cancel.assert_called_once_with()
        self.assertEqual(backend.calls, [])
        self.assertEqual(broker.calls, [])

    def test_06_login_cancel_falls_back_to_the_production_module(self):
        sentinel = {"cancelled": False, "state": "no_login_in_progress",
                    "detail": None}
        backend = _GateStub()
        broker = _GateStub()
        self._wire(_ObserveStub(None), backend=backend, broker=broker)
        from console import share_production as console_share_production
        with mock.patch.object(console_share_production,
                               "backend_login_cancel",
                               return_value=sentinel) as cancel:
            out = m.backend_login_cancel("share", {})
        self.assertIs(out, sentinel)
        cancel.assert_called_once_with()
        self.assertEqual(backend.calls, [])
        self.assertEqual(broker.calls, [])

    def test_07_login_cancel_error_is_never_a_fake_success(self):
        cancel = mock.Mock(side_effect=OperationError(
            "share_backend_login_cancel_failed", "登录取消被拒（HTTP 403）"))
        self._wire(_ObserveStub(None), cancel=cancel)
        with self.assertRaises(OperationError) as caught:
            m.backend_login_cancel("share", {})
        self.assertEqual(caught.exception.code,
                         "share_backend_login_cancel_failed")


if __name__ == "__main__":
    unittest.main()
