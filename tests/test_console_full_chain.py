# -*- coding: utf-8 -*-
"""Full-chain regression: task layer -> adapter -> PowerShell JSON wrapper ->
runtime bridge -> an isolated dummy process (TEST-ONLY hook seam).

P1 acceptance: start/stop/cancel/restart-recovery through the complete console
chain against a real (but inert) child process. No real gateway service is
touched: the hook seam only replaces the launcher/stopper.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_OPS_PATH = _ROOT / "console_operations.py"
_SPEC = importlib.util.spec_from_file_location("console_operations_full",
                                               _OPS_PATH)
console_operations = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = console_operations
_SPEC.loader.exec_module(console_operations)

from console_operations import ActionSpec, OperationManager  # noqa: E402

_WRAPPER = _ROOT / "invoke-console-runtime-json.ps1"
_POWERSHELL = os.environ.get("CONSOLE_POWERSHELL") or "pwsh"

_HOOKS_TEMPLATE = """
$ConsoleTestStartHook = {
    param($Key)
    if ($env:CONSOLE_TEST_START_DELAY_MS) {
        Start-Sleep -Milliseconds ([int]$env:CONSOLE_TEST_START_DELAY_MS)
    }
    # Start-Process joins arguments verbatim: the -c payload must carry its
    # own quotes or the semicolon terminates the joined command line.
    $proc = Start-Process -FilePath 'python' `
        -ArgumentList @('-c', '"import time; time.sleep(90)"') `
        -PassThru -WindowStyle Hidden
    Set-Content -LiteralPath $env:CONSOLE_TEST_PID_FILE -Value $proc.Id
    for ($i = 0; $i -lt 30; $i++) {
        if (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue) { break }
        Start-Sleep -Milliseconds 100
    }
    return [pscustomobject]@{ Key = $Key; Status = 'Started'; Action = 'Start' }
}
$ConsoleTestStopHook = {
    param($Key)
    $target = [int](Get-Content -LiteralPath $env:CONSOLE_TEST_PID_FILE -Raw)
    Stop-Process -Id $target -Force -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 30; $i++) {
        if (-not (Get-Process -Id $target -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 100
    }
    Remove-Item -LiteralPath $env:CONSOLE_TEST_PID_FILE -ErrorAction SilentlyContinue
    return [pscustomobject]@{ Key = $Key; Status = 'Stopped'; Action = 'Stop' }
}
"""


class HookSeamAdapter:
    """Drives the real wrapper entry with the TEST-ONLY hook seam. The hook
    file spawns/kills an inert python child; production never passes this.

    Lock contract mirrors the production adapter: the OperationManager worker
    holds the resource lock (``lock_dir``, the SAME namespace this adapter
    passes via -LockDir), so the wrapper is told -CallerHoldsLock and must
    not acquire it again. If either half drifted (missing switch or a
    different directory), the wrapper would block on its parent's handle and
    every operation here would fail — the shared namespace is the point.
    """

    def __init__(self, hooks_path, pid_file, lock_dir, timeout_s=40.0):
        self._hooks = str(hooks_path)
        self._pid_file = pid_file
        self._lock_dir = str(lock_dir)
        self._timeout = timeout_s

    def _call(self, action):
        cmd = [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", str(_WRAPPER), "-Action", action, "-Key", "chat2api",
               "-CallerHoldsLock", "-RuntimeHooksFile", self._hooks,
               "-LockDir", self._lock_dir]
        env = dict(os.environ)
        env["CONSOLE_TEST_PID_FILE"] = self._pid_file
        proc = subprocess.run(cmd, capture_output=True, timeout=self._timeout,
                              env=env)
        try:
            payload = json.loads(proc.stdout.decode("utf-8", errors="replace"))
        except ValueError:
            raise AssertionError("wrapper returned non-JSON (rc=%d): %r"
                                 % (proc.returncode, proc.stdout[:120]))
        if proc.returncode != 0 or not payload.get("ok"):
            raise AssertionError("wrapper failed: %s" % payload)
        return payload

    def service_start(self, target, params, ctx=None):
        payload = self._call("start")
        return payload.get("result") or {"key": target}

    def service_stop(self, target, params, ctx=None):
        payload = self._call("stop")
        return payload.get("result") or {"key": target}


class FullChainTestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_dir = tmp.name
        self.db_path = os.path.join(self.tmp_dir, "console-state.sqlite3")
        self.lock_dir = os.path.join(self.tmp_dir, "console-locks")
        self.hooks_path = os.path.join(self.tmp_dir, "console-test-hooks.ps1")
        self.pid_file = os.path.join(self.tmp_dir, "console-test-pid.txt")
        Path(self.hooks_path).write_text(_HOOKS_TEMPLATE, encoding="utf-8")
        self.extra_env = {}
        self.adapter = HookSeamAdapter(self.hooks_path, self.pid_file,
                                       self.lock_dir)
        self.registry = [
            ActionSpec("service.start", "test chain start",
                       handler=self.adapter.service_start,
                       resource_fn=lambda t, p: "service:" + t,
                       bump_generation_on_success=False),
            ActionSpec("service.stop", "test chain stop",
                       handler=self.adapter.service_stop,
                       resource_fn=lambda t, p: "service:" + t),
        ]
        self.managers = []

    def make_manager(self):
        mgr = OperationManager(self.db_path, self.registry,
                               os.path.join(self.tmp_dir, "console-locks"))
        self.managers.append(mgr)
        self.addCleanup(mgr.close)
        return mgr

    def wait_terminal(self, mgr, op_id, timeout=30.0):
        deadline = time.time() + timeout
        op = mgr.get_operation(op_id)
        while op["state"] not in ("succeeded", "failed", "cancelled",
                                  "interrupted"):
            time.sleep(0.05)
            op = mgr.get_operation(op_id)
            self.assertLess(time.time(), deadline, "op did not finish: %r" % op)
        return op

    def wait_state(self, mgr, op_id, state, timeout=20.0):
        deadline = time.time() + timeout
        while mgr.get_operation(op_id)["state"] != state:
            self.assertLess(time.time(), deadline)
            time.sleep(0.05)
        return mgr.get_operation(op_id)

    def dummy_alive(self, pid):
        proc = subprocess.run(
            [_POWERSHELL, "-NoProfile", "-Command",
             "if (Get-Process -Id %d -ErrorAction SilentlyContinue) { exit 0 }"
             " else { exit 1 }" % pid],
            capture_output=True, timeout=10)
        return proc.returncode == 0

    def kill_dummy_if_left(self):
        try:
            with open(self.pid_file, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            return
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=10)


class FullChainLifecycleTests(FullChainTestBase):
    def test_start_then_stop_real_process(self):
        mgr = self.make_manager()
        started = mgr.submit("service.start", "chat2api")
        started = self.wait_terminal(mgr, started["id"])
        self.assertEqual(started["state"], "succeeded", started)
        self.assertEqual(started["result"],
                         {"key": "chat2api", "status": "Started",
                          "action": "Start"})
        with open(self.pid_file, "r", encoding="utf-8") as f:
            dummy_pid = int(f.read().strip())
        self.assertTrue(self.dummy_alive(dummy_pid), "dummy process must run")
        stopped = mgr.submit("service.stop", "chat2api")
        stopped = self.wait_terminal(mgr, stopped["id"])
        self.assertEqual(stopped["state"], "succeeded")
        self.assertEqual(stopped["result"],
                         {"key": "chat2api", "status": "Stopped",
                          "action": "Stop"})
        self.assertFalse(os.path.exists(self.pid_file), "stop hook removes pid file")
        self.assertFalse(self.dummy_alive(dummy_pid), "dummy must be gone after stop")
        mgr.close()


class FullChainCancelRestartTests(FullChainTestBase):
    def test_cancel_queued_never_spawns_process(self):
        # first start holds the wrapper lock for 1.5s inside the hook
        self.extra_env["CONSOLE_TEST_START_DELAY_MS"] = "1500"
        os.environ["CONSOLE_TEST_START_DELAY_MS"] = "1500"
        self.addCleanup(os.environ.pop, "CONSOLE_TEST_START_DELAY_MS", None)
        mgr = self.make_manager()
        first = mgr.submit("service.start", "chat2api")
        second = mgr.submit("service.start", "chat2api")
        self.wait_state(mgr, first["id"], "running")
        cancelled = mgr.cancel(second["id"])
        self.assertEqual(cancelled["state"], "cancelled")
        second = self.wait_terminal(mgr, second["id"])
        first = self.wait_terminal(mgr, first["id"])
        self.assertEqual(first["state"], "succeeded")
        self.assertEqual(second["state"], "cancelled")
        self.assertIsNone(second["result"])  # queued cancel: never executed
        mgr.close()

    def test_restart_marks_running_interrupted(self):
        self.extra_env["CONSOLE_TEST_START_DELAY_MS"] = "2500"
        os.environ["CONSOLE_TEST_START_DELAY_MS"] = "2500"
        self.addCleanup(os.environ.pop, "CONSOLE_TEST_START_DELAY_MS", None)
        mgr = self.make_manager()
        op_id = mgr.submit("service.start", "chat2api")["id"]
        self.wait_state(mgr, op_id, "running")
        # simulated restart: a fresh manager marks unfinished ops interrupted
        mgr2 = self.make_manager()
        row = mgr2.get_operation(op_id)
        self.assertEqual(row["state"], "interrupted")
        self.assertEqual(row["error"]["code"], "interrupted_by_restart")
        # the in-flight wrapper keeps running; its late completion is
        # discarded by the guarded transition (no write-back, no replay)
        self.wait_terminal(mgr, op_id)
        self.assertEqual(mgr2.get_operation(op_id)["state"], "interrupted")
        self.assertIsNone(mgr2.get_operation(op_id)["result"])
        # wait (bounded) for the wrapper to finish and record the dummy pid
        deadline = time.time() + 15
        while not os.path.exists(self.pid_file):
            self.assertLess(time.time(), deadline, "wrapper never recorded pid")
            time.sleep(0.2)
        mgr.close()

    def tearDown(self):
        # any dummy left by an interrupted/cancelled chain must not survive
        self.kill_dummy_if_left()


if __name__ == "__main__":
    unittest.main()
