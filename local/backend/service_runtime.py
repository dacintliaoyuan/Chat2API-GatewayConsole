# -*- coding: utf-8 -*-
"""Local service-runtime control (E19-1-08).

The local composition root controls ONE UM runtime. Two explicit modes exist:

* ``fixture`` starts the in-package ``chat2api.py`` child through
  ``UmRuntime`` — the isolated end-to-end mode used by the local fixture;
* ``powershell`` invokes the configured local runtime wrapper script with an
  explicit action and requires its JSON ``ok`` confirmation.

The mode comes from ``RuntimeControlConfig``; there is no implicit fallback to a
repository script or to a production service.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from .um_runtime import UmRuntime, UmRuntimeError


class ServiceRuntimeError(RuntimeError):
    """The configured runtime control could not be confirmed."""


class FixtureServiceRuntime:
    """Control the in-package chat2api child process directly."""

    def __init__(self, config, *, um_runtime=None):
        self.config = config
        self.um_runtime = (um_runtime if um_runtime is not None
                            else UmRuntime(config.um_runtime,
                                           price_file=config.price_file))

    def start(self):
        if self.um_runtime.start() is False:
            raise ServiceRuntimeError("local UM runtime did not start")
        wait_ready = getattr(self.um_runtime, "wait_ready", None)
        if wait_ready is not None and wait_ready() is False:
            raise ServiceRuntimeError("local UM runtime did not become ready")
        return True

    def stop(self):
        return self.um_runtime.stop()

    def status(self):
        return self.um_runtime.status()


class PowerShellServiceRuntime:
    """Control the local runtime through the configured PowerShell script."""

    def __init__(self, config):
        self.config = config

    def start(self):
        self._invoke("start")
        return True

    def stop(self):
        self._invoke("stop")
        return True

    def status(self):
        return self._invoke("status")

    def _invoke(self, action):
        control = self.config.runtime_control
        script = control.script
        if not script or not os.path.isabs(script):
            raise ServiceRuntimeError(
                "powershell runtime control script is not configured")
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            raise ServiceRuntimeError("PowerShell is not available")
        try:
            completed = subprocess.run(
                [shell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", script, "-Action", action],
                capture_output=True, text=True, timeout=control.timeout_s,
                check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ServiceRuntimeError(
                "powershell runtime %s failed: %s" % (action, exc)) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise ServiceRuntimeError(
                "powershell runtime %s exited %d%s"
                % (action, completed.returncode,
                   "" if not detail else ": " + detail.splitlines()[-1]))
        try:
            payload = json.loads(completed.stdout or "{}")
        except ValueError as exc:
            raise ServiceRuntimeError(
                "powershell runtime %s returned invalid JSON" % action) \
                from exc
        if not isinstance(payload, dict):
            raise ServiceRuntimeError(
                "powershell runtime %s returned a non-object" % action)
        if action in ("start", "stop") and payload.get("ok") is not True:
            raise ServiceRuntimeError(
                "powershell runtime %s was not confirmed" % action)
        return payload


def build_runtime_control(config):
    """Build the explicit runtime control for the configured mode."""
    mode = config.runtime_control.mode
    if mode == "fixture":
        return FixtureServiceRuntime(config)
    if mode == "powershell":
        return PowerShellServiceRuntime(config)
    raise ServiceRuntimeError("unknown runtime control mode: %r" % (mode,))


__all__ = [
    "FixtureServiceRuntime",
    "PowerShellServiceRuntime",
    "ServiceRuntimeError",
    "UmRuntimeError",
    "build_runtime_control",
]
