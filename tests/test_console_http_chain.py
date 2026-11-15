# -*- coding: utf-8 -*-
"""HTTP-threaded full-chain lifecycle (closes the review coverage gap).

Chain under test: real usagesrv HTTP assembly -> /api/console routes (session/
control-token auth, body validation) -> task layer -> PRODUCTION adapter
(RuntimeServiceAdapter, the ``handler(target, params, ctx)`` contract) ->
real PowerShell JSON wrapper -> runtime bridge -> isolated dummy process via
the TEST-ONLY hook seam.

The adapter's wrapper call is intercepted at the subprocess boundary only to
assert the fixed-parameter shape and inject the test seams (-RuntimeHooksFile
and -LockDir pointing at the SAME temporary namespace the OperationManager
uses); the real PowerShell entry, bridge and hook then run. Parameter
binding and the lock contract are self-checking: if the adapter stopped
declaring -CallerHoldsLock, or the lock directories diverged, the wrapper
would block on the manager's own handle until timeout and every assertion
here fails. No real gateway service is touched.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="console-http-chain-")
os.environ["CONSOLE_STATE_FILE"] = os.path.join(_TMP, "console-state.sqlite3")
os.environ["CONSOLE_LOCK_DIR"] = os.path.join(_TMP, "console-locks")
os.environ["USAGE_SESSION_CODE_FILE"] = os.path.join(_TMP, "usage-session-code")
os.environ["USAGE_CONTROL_TOKEN_FILE"] = os.path.join(_TMP, "admin-control-token")
_CONTROL_TOKEN = "test-control-token-console-http-chain"
from share.secret_protection import write_private_secret_path
write_private_secret_path(
    os.environ["USAGE_CONTROL_TOKEN_FILE"], _CONTROL_TOKEN.encode("utf-8"))

_USAGESRV_PATH = Path(__file__).resolve().parents[1] / "usagesrv.py"
_SPEC = importlib.util.spec_from_file_location("usage_service_console_http",
                                               _USAGESRV_PATH)
usagesrv = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = usagesrv
_SPEC.loader.exec_module(usagesrv)

from console import adapters as console_adapters  # noqa: E402  (same directory, sys.path via cwd)
from console import operations as console_operations  # noqa: E402

_LOCK_DIR = os.environ["CONSOLE_LOCK_DIR"]
# 本模块的 db/lock 路径在导入期就写进 env；本类装配 manager 时必须用
# 本模块自己的常量，不能回读 env——另一个测试模块的 tearDownModule 可能已把
# env 还原成导入前的值，回读会让 manager 与 CLI holder 落在不同锁目录。
_STATE_FILE = os.path.join(_TMP, "console-state.sqlite3")
_HOOKS_PATH = os.path.join(_TMP, "console-test-hooks.ps1")
_PID_FILE = os.path.join(_TMP, "console-test-pid.txt")
_ROOT = Path(__file__).resolve().parents[1]
_BRIDGE_PS1 = _ROOT / "lib" / "console-runtime-bridge.ps1"
_WRAPPER = console_adapters.WRAPPER
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

FAKE_SNAPSHOT = {
    "ok": True,
    "services": [{
        "key": "usage", "display_name": "用量统计", "port": 8001,
        "state": "Running", "healthy": True, "detail": "运行中",
        "process_ids": [1],
    }],
    "meta": {"source": "gateway-runtime", "generated_epoch": 99},
}

_REAL_RUN = console_adapters.subprocess.run


def _shimmed_run(cmd, capture_output=False, timeout=None, env=None, **kwargs):
    """Intercept ONLY the console adapter's wrapper invocation: assert the
    fixed-parameter shape, inject the TEST-ONLY seams, then run the real
    PowerShell entry. Any other subprocess call passes through untouched."""
    if "-File" not in cmd or cmd[cmd.index("-File") + 1] != _WRAPPER:
        return _REAL_RUN(cmd, capture_output=capture_output, timeout=timeout,
                         env=env, **kwargs)
    assert cmd[0].lower().endswith("pwsh"), cmd
    assert "-NoProfile" in cmd and "-ExecutionPolicy" in cmd, cmd
    assert cmd[cmd.index("-Action") + 1] in ("start", "stop"), cmd
    # Single-lock-owner contract: the task layer owns the resource lock, so
    # the adapter must tell the wrapper to skip its own acquisition. With
    # -LockDir pointing at the SAME namespace the manager uses, a regression
    # here blocks on the manager's handle and the operation fails loudly.
    assert "-CallerHoldsLock" in cmd, "adapter must declare caller-holds-lock"
    merged = dict(os.environ)
    if env:
        merged.update(env)
    merged["CONSOLE_TEST_PID_FILE"] = _PID_FILE
    argv = cmd + ["-RuntimeHooksFile", _HOOKS_PATH, "-LockDir", _LOCK_DIR]
    return _REAL_RUN(argv, capture_output=capture_output, timeout=timeout,
                     env=merged, **kwargs)


class HttpChainTestBase(unittest.TestCase):
    """One real usagesrv server; the manager is the production assembly
    shape (same env-redirected db/lock paths) with the REAL registry whose
    service.start/stop specs are enabled on this test-built instance only."""

    @classmethod
    def setUpClass(cls):
        Path(_HOOKS_PATH).write_text(_HOOKS_TEMPLATE, encoding="utf-8")
        cls.run_patcher = mock.patch.object(
            console_adapters.subprocess, "run", _shimmed_run)
        cls.run_patcher.start()
        cls.addClassCleanup(cls.run_patcher.stop)
        cls.status_patcher = mock.patch(
            "console.adapters.service_status", return_value=FAKE_SNAPSHOT)
        cls.status_patcher.start()
        cls.addClassCleanup(cls.status_patcher.stop)
        registry = console_adapters.build_registry()
        for spec in registry:
            if spec.name in ("service.start", "service.stop"):
                spec.enabled = True  # TEST-ONLY enablement, this instance
        # The handler-side per-action+target policy is the production gate:
        # enabling the spec is not enough, the policy must also authorize
        # the target (this mirrors the deployment-package flip path: the
        # package populates the sets, the registry computes enablement).
        cls.policy_patcher = mock.patch.object(
            console_adapters, "SERVICE_START_ENABLED_TARGETS",
            frozenset({"chat2api"}))
        cls.policy_patcher.start()
        cls.addClassCleanup(cls.policy_patcher.stop)
        cls.policy_patcher2 = mock.patch.object(
            console_adapters, "SERVICE_STOP_ENABLED_TARGETS",
            frozenset({"chat2api"}))
        cls.policy_patcher2.start()
        cls.addClassCleanup(cls.policy_patcher2.stop)
        cls.manager = console_operations.OperationManager(
            db_path=_STATE_FILE,
            registry=registry,
            lock_dir=_LOCK_DIR,
            acquire_timeout_s=2.0)
        cls.addClassCleanup(cls.manager.close)
        usagesrv._console_manager = cls.manager
        cls.server = usagesrv.ThreadingHTTPServer(("127.0.0.1", 0),
                                                  usagesrv.Handler)
        cls.addClassCleanup(cls.server.shutdown)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    def req(self, method, path, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=15)
        head = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body)
            head.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=payload, headers=head)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, raw

    def auth(self):
        return {"X-Admin-Control": _CONTROL_TOKEN}

    def submit(self, body):
        status, raw = self.req("POST", "/api/console/operations", body=body,
                               headers=self.auth())
        self.assertEqual(status, 201, raw)
        return json.loads(raw)["operation"]

    def get_operation(self, op_id):
        status, raw = self.req("GET",
                               "/api/console/operations/" + op_id,
                               headers=self.auth())
        self.assertEqual(status, 200, raw)
        return json.loads(raw)["operation"]

    def wait_operation(self, op_id, states, timeout=30.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.get_operation(op_id)
            if last["state"] in states:
                return last
            time.sleep(0.05)
        self.fail("operation %s did not reach %r: %r" % (op_id, states, last))

    def dummy_alive(self, pid):
        proc = subprocess.run(
            [_POWERSHELL, "-NoProfile", "-Command",
             "if (Get-Process -Id %d -ErrorAction SilentlyContinue)"
             " { exit 0 } else { exit 1 }" % pid],
            capture_output=True, timeout=10)
        return proc.returncode == 0

    def kill_dummy_if_left(self):
        try:
            with open(_PID_FILE, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            return
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=10)

    def tearDown(self):
        self.kill_dummy_if_left()


class ConsoleHttpChainTests(HttpChainTestBase):
    def test_http_start_stop_through_production_adapter(self):
        started = self.submit({"action": "service.start", "target": "chat2api"})
        started = self.wait_operation(started["id"], ("succeeded",))
        self.assertEqual(started["result"],
                         {"key": "chat2api", "status": "Started",
                          "action": "Start"})
        with open(_PID_FILE, "r", encoding="utf-8") as f:
            dummy_pid = int(f.read().strip())
        self.assertTrue(self.dummy_alive(dummy_pid), "dummy must run")
        stopped = self.submit({"action": "service.stop", "target": "chat2api"})
        stopped = self.wait_operation(stopped["id"], ("succeeded",))
        self.assertEqual(stopped["result"],
                         {"key": "chat2api", "status": "Stopped",
                          "action": "Stop"})
        self.assertFalse(os.path.exists(_PID_FILE), "stop hook cleans pid file")
        self.assertFalse(self.dummy_alive(dummy_pid), "dummy must be gone")

    def test_http_cancel_queued_never_spawns_process(self):
        os.environ["CONSOLE_TEST_START_DELAY_MS"] = "1500"
        self.addCleanup(os.environ.pop, "CONSOLE_TEST_START_DELAY_MS", None)
        first = self.submit({"action": "service.start", "target": "chat2api"})
        deadline = time.time() + 15
        while self.get_operation(first["id"])["state"] != "running":
            self.assertLess(time.time(), deadline)
            time.sleep(0.05)
        second = self.submit({"action": "service.start", "target": "chat2api"})
        cancelled = self.req("POST",
                             "/api/console/operations/%s/cancel" % second["id"],
                             headers=self.auth())
        self.assertEqual(cancelled[0], 200)
        second = json.loads(cancelled[1])["operation"]
        self.assertEqual(second["state"], "cancelled")
        self.assertIsNone(second["result"])  # queued cancel: never executed
        first = self.wait_operation(first["id"], ("succeeded",))
        self.assertEqual(first["state"], "succeeded")

    def test_cli_style_lock_holder_blocks_http_operations(self):
        holder_out = os.path.join(_TMP, "cli-holder.out")
        ps_cmd = (". '{bridge}'; "
                  "$l = Enter-ConsoleResourceLock -Resource 'service:chat2api' "
                  "-LockDir '{lock}' -Owner 'cli-invoker'; 'HELD'; "
                  "Start-Sleep -Seconds 6; Exit-ConsoleResourceLock -Lock $l"
                  ).format(bridge=_BRIDGE_PS1, lock=_LOCK_DIR)
        with open(holder_out, "w", encoding="utf-8") as out:
            holder = subprocess.Popen(
                [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", ps_cmd],
                stdout=out, stderr=subprocess.DEVNULL)
        self.addCleanup(holder.kill)
        self.addCleanup(holder.wait)
        deadline = time.time() + 10
        held = False
        while time.time() < deadline:
            try:
                with open(holder_out, "r", encoding="utf-8") as f:
                    if "HELD" in f.read():
                        held = True
                        break
            except OSError:
                pass
            time.sleep(0.1)
        self.assertTrue(held, "CLI-style holder never acquired the lock")
        # independent confirmation through the Python side of the protocol
        probe = console_operations.ResourceFileLock(
            _LOCK_DIR, "service:chat2api", "probe", timeout_s=0.3)
        with self.assertRaises(console_operations.LockBusy):
            probe.acquire()
        # while a CLI-style holder owns the lock, an HTTP operation fails
        # bounded instead of starting a real service action
        blocked = self.submit({"action": "service.start", "target": "chat2api"})
        blocked = self.wait_operation(blocked["id"], ("failed",))
        self.assertEqual(blocked["error"]["code"], "resource_busy_timeout")
        self.assertFalse(os.path.exists(_PID_FILE),
                         "blocked operation must not spawn anything")
        # after the holder exits the OS releases the handle: next operation
        # runs and completes the full chain
        holder.wait(timeout=10)
        started = self.submit({"action": "service.start", "target": "chat2api"})
        started = self.wait_operation(started["id"], ("succeeded",))
        self.assertEqual(started["result"]["status"], "Started")
        with open(_PID_FILE, "r", encoding="utf-8") as f:
            dummy_pid = int(f.read().strip())
        self.assertTrue(self.dummy_alive(dummy_pid))


if __name__ == "__main__":
    unittest.main()
