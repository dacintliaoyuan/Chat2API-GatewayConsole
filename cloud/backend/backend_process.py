# -*- coding: utf-8 -*-
"""Dedicated 8110 backend owner for the cloud composition root.

E19-1-11.

``production_backend_ops(config=BackendConfig(...))`` owns the spawn/stop
mechanics; this module owns the composition-root lifecycle around it:

* every path/port/URL comes from the explicit ``CloudConfig`` — no environment
  fallback, no repository default, no ``usagesrv`` import;
* ``status()`` never calls a non-existent ``status`` verb: it uses the
  READ-ONLY
  ``verify_running(forget_stale=False)`` verdict plus ``backend_pid_state()``;
* start/stop wait for CONFIRMED evidence within the configured timeouts; an
  unconfirmed start/stop raises ``BackendProcessError`` instead of reporting a
  fake success;
* ``backend_python`` is checked against the interpreter the backend ops
  actually spawn (``sys.executable``); a mismatch is refused BEFORE
  spawning anything.
"""
from __future__ import annotations

import os
import sys
import time

from console.share_production import BackendConfig, production_backend_ops

BACKEND_PID_NAME = "backend.pid"


class BackendProcessError(RuntimeError):
    """The dedicated backend could not be confirmed in the wanted state."""


class BackendProcess:
    """Start/stop/status the dedicated 8110 chat2api instance."""

    def __init__(self, config, *, ops=None):
        self.config = config
        if ops is None:
            ops = production_backend_ops(config=self._backend_config(config))
        self._ops = ops

    @staticmethod
    def _backend_config(config):
        return BackendConfig(
            share_run_root=config.share_run_root,
            share_secret_key=config.share_secret_key_file,
            attribution_key=config.attribution_key_file,
            token=config.backend_token_file,
            service_log=config.backend_service_log,
            usage_log=config.backend_usage_log,
            control_token=config.backend_control_token_file,
            reauth_slot=config.backend_reauth_slot_file,
            pid=os.path.join(config.backend_dir, BACKEND_PID_NAME),
            backend_dir=config.backend_dir,
            profile=config.backend_profile_dir,
            backend_port=config.backend_port,
            backend_base_url=config.backend_base_url,
            backend_gateway_dir=config.backend_gateway_dir,
            revoke_state_file=config.revoke_state_file,
            price_table_file=config.backend_price_table_file,
        )

    # --------------------------------------------------------------- read
    def status(self):
        """Read-only report: no write, no stale-pid deletion."""
        running = self._ops.verify_running(forget_stale=False)
        pid_state = self._ops.backend_pid_state()
        return {
            "running": running is True,
            "pid_state": pid_state if isinstance(pid_state, dict) else {},
            "port": self.config.backend_port,
        }

    # -------------------------------------------------------------- lifecycle
    def start(self):
        self._require_interpreter()
        if self._ops.verify_running(forget_stale=False) is True:
            return True
        if self._ops.start() is not True:
            raise BackendProcessError(
                "dedicated backend start was not confirmed")
        if self._wait_running(self.config.backend_start_timeout_s):
            return True
        cleanup = None
        try:
            self._ops.stop()
        except Exception as exc:  # noqa: BLE001 - reported, never hidden
            cleanup = "%s: %s" % (type(exc).__name__, exc)
        raise BackendProcessError(
            "dedicated backend did not become ready within %ss%s"
            % (self.config.backend_start_timeout_s,
               "" if cleanup is None else "; cleanup failed: " + cleanup))

    def stop(self):
        if self._ops.verify_running(forget_stale=False) is not True:
            return True
        if self._ops.stop() is not True:
            deadline = time.monotonic() + self.config.backend_stop_timeout_s
            while time.monotonic() < deadline:
                if self._ops.verify_running(forget_stale=False) is not True:
                    return True
                time.sleep(0.2)
            raise BackendProcessError(
                "dedicated backend stop was not confirmed within %ss"
                % self.config.backend_stop_timeout_s)
        return True

    # --------------------------------------------------------------- helpers
    def _require_interpreter(self):
        configured = os.path.realpath(self.config.backend_python)
        actual = os.path.realpath(sys.executable)
        if configured != actual:
            raise BackendProcessError(
                "backend_python (%s) does not match the interpreter the "
                "backend ops spawn (%s); refusing to start"
                % (self.config.backend_python, sys.executable))

    def _wait_running(self, timeout_s):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._ops.verify_running(forget_stale=False) is True:
                return True
            time.sleep(0.2)
        return False
