# -*- coding: utf-8 -*-
"""E19-1-09 local application fixture and RED/GREEN test (r19).

The local application is exercised as a REAL subprocess through
``local/run_local.py``:

* a missing/invalid config exits 2 without binding a port or creating the state
  directories;
* ``--print-runtime-info`` runs under an arbitrary CWD with a polluted
  ``PYTHONPATH`` and still loads the package-local modules (no cloud/share
  host module, no repository ``SelfUseAI``/Git root on ``sys.path``);
* a full start creates only the fixture directories, serves the control-token
  gated local routes, runs the ``model.catalog`` operation and exits cleanly.

Every path lives under the fixture temp root; no real ledger, price document,
profile or credential is read.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
TESTS_DIR = HERE.parent
AI_GATEWAY = TESTS_DIR.parent
PROJECT_ROOT = AI_GATEWAY.parent


def _dead_pid():
    """A pid this process has already reaped (never alive again)."""
    import subprocess
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=30)
    return process.pid
GIT_ROOT = AI_GATEWAY.parents[1]
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(AI_GATEWAY))

from local.backend import app as local_app  # noqa: E402
from local.backend.config import (  # noqa: E402
    LocalConfig,
    LocalConfigError,
)
from r19.local_fixture import (  # noqa: E402
    AI_GATEWAY as FIXTURE_GATEWAY,
    LocalFixture,
    assert_paths_isolated,
)

TERMINAL_STATES = ("succeeded", "failed", "cancelled", "interrupted")
FORBIDDEN_MODULES = (
    "usagesrv",
    "console.adapters",
    "share_management",
    "share_store",
    "share_auth",
    "share_dispatch",
    "console_display_proxy",
    "cloud",
)


def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


class LocalConfigTests(unittest.TestCase):
    """Fail-closed configuration and path hygiene (no service start)."""

    def setUp(self):
        self.fixture = LocalFixture(FIXTURE_GATEWAY, start_fake_um=False)

    def tearDown(self):
        self.fixture.close()

    def test_missing_config_is_refused_without_side_effects(self):
        missing = self.fixture.write_config(
            name="local-missing.json", omit="control_token_file")
        for key in ("home_dir", "state_root", "log_dir"):
            self.assertFalse(Path(self.fixture.paths[key]).exists(), key)
        result = self.fixture.run_check_config(missing)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("control_token_file", result.stderr)
        for key in ("home_dir", "state_root", "log_dir"):
            self.assertFalse(
                Path(self.fixture.paths[key]).exists(),
                "--check-config created %s" % key)
        self.assertFalse(_port_open(self.fixture.app_port),
                         "--check-config bound the app port")

    def test_external_source_dir_is_refused(self):
        """The UM runtime source must be the in-package ``chat2api`` dir.

        Another checkout's ``chat2api`` is not a runtime dependency: accepting
        it would let the package run source outside itself and hand its parent
        to the child through ``PYTHONPATH``.
        """
        values = self.fixture.config_mapping()
        external = Path(tempfile.mkdtemp(prefix="r19-external-")) / "chat2api"
        values["um_runtime"]["source_dir"] = str(external)
        with self.assertRaises(LocalConfigError) as caught:
            LocalConfig.from_mapping(values)
        self.assertEqual(caught.exception.field, "um_runtime.source_dir")

    def test_price_override_file_is_optional_but_absolute(self):
        values = self.fixture.config_mapping()
        self.assertIsNone(LocalConfig.from_mapping(values).price_override_file)
        values["price_override_file"] = "relative/price.override.json"
        with self.assertRaises(LocalConfigError) as caught:
            LocalConfig.from_mapping(values)
        self.assertEqual(caught.exception.field, "price_override_file")

    def test_print_runtime_info_is_isolated_and_does_not_load_cloud(self):
        polluted = self.fixture.root / "polluted"
        polluted.mkdir(parents=True, exist_ok=True)
        (polluted / "console").mkdir(parents=True, exist_ok=True)
        (polluted / "console/stats.py").write_text(
            "raise RuntimeError('polluted console_stats imported')\n",
            encoding="utf-8")
        arbitrary_cwd = Path(tempfile.mkdtemp(prefix="r19-arbitrary-cwd-"))
        try:
            result = self.fixture.run_runtime_info(
                cwd=arbitrary_cwd, pythonpath=polluted)
        finally:
            shutil.rmtree(arbitrary_cwd, ignore_errors=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        sys_path = [Path(entry).resolve() for entry in info["sys_path"]]
        self.assertIn(FIXTURE_GATEWAY.resolve(), sys_path)
        self.assertNotIn(PROJECT_ROOT.resolve(), sys_path)
        self.assertNotIn(GIT_ROOT.resolve(), sys_path)
        self.assertNotIn(str(polluted), info.get("PYTHONPATH") or "")
        assert_paths_isolated(self, info["config_paths"], self.fixture.root)
        for name in FORBIDDEN_MODULES:
            self.assertNotIn(name, info["modules"])
        self.assertNotIn(str(polluted), info["sys_path"])


class PidFileMutualExclusionTests(unittest.TestCase):
    """standards-lifecycle-07/08: the pid file is the ONE owner fact.

    Three defects are pinned here:

    * a bare ``os.kill(pid, 0)`` probe answered "alive" for a process this
      process already reaped (Windows keeps the process object while a handle is
      open), so a crashed run blocked every restart;
    * a recycled PID (an unrelated live process) was treated as the previous
      owner, so the start was refused forever;
    * the read -> check -> O_EXCL -> unlink sequence deleted a concurrent
      starter's half-written file and let two instances claim the same config.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="r19-pid-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.pid_file = self.root / "app.pid"

    def test_a_recycled_pid_is_not_the_previous_owner(self):
        # A live process that is NOT ours (this test process) recorded with a
        # creation time that cannot belong to it: the start must be accepted.
        self.pid_file.write_text(
            "%d 1000000000.000000" % os.getpid(), encoding="utf-8")
        local_app._write_pid_file(self.pid_file, os.getpid())
        self.assertEqual(local_app._read_pid_file(self.pid_file), os.getpid())

    def test_a_fresh_half_written_pid_file_is_never_taken_over(self):
        """A concurrent starter's half-written file must not be replaced.

        The file exists but has no readable pid yet: that is another starter in
        the middle of its own ``O_EXCL`` create, so taking it over would give two
        instances the same configuration. Refusing is the fail-closed answer.
        """
        self.pid_file.write_text("", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            local_app._write_pid_file(self.pid_file, os.getpid())
        self.assertEqual(self.pid_file.read_text(encoding="utf-8"), "",
                         "the half-written file must be left untouched")

    def test_an_abandoned_half_written_pid_file_is_replaced(self):
        """An old unreadable file is abandoned, not a live starter."""
        self.pid_file.write_text("", encoding="utf-8")
        old = time.time() - (local_app.PID_FILE_STALE_AFTER_S + 60)
        os.utime(self.pid_file, (old, old))
        local_app._write_pid_file(self.pid_file, os.getpid())
        self.assertEqual(local_app._read_pid_file(self.pid_file), os.getpid())

    def test_only_one_of_two_live_owners_can_claim_the_file(self):
        """A second live owner must be refused; a stale one is replaced."""
        first = os.getpid()
        # A different live process: the test runner's parent is alive and is
        # NOT this process, so it is a real second owner.
        second = os.getppid()
        if second == first:
            self.skipTest("no distinct live pid is available")
        local_app._write_pid_file(self.pid_file, first)
        with self.assertRaises(RuntimeError):
            local_app._write_pid_file(self.pid_file, second)
        self.assertEqual(local_app._read_pid_file(self.pid_file), first,
                         "the refused owner must not take the file over")

    def test_a_dead_owner_is_replaced_by_a_new_starter(self):
        """A crashed previous run must not block every restart."""
        dead = _dead_pid()
        if dead is None:
            self.skipTest("no reaped child pid is available")
        self.pid_file.write_text(
            "%d %s" % (dead, "%.6f" % 1000.0), encoding="utf-8")
        local_app._write_pid_file(self.pid_file, os.getpid())
        self.assertEqual(local_app._read_pid_file(self.pid_file), os.getpid())


class LocalEndToEndTests(unittest.TestCase):
    """A real local app subprocess: fake UM, UM runtime, routes and exit."""

    def setUp(self):
        self.fixture = LocalFixture(FIXTURE_GATEWAY)
        self.arbitrary_cwd = Path(tempfile.mkdtemp(prefix="r19-e2e-cwd-"))
        self.polluted = self.fixture.root / "polluted"
        self.polluted.mkdir(parents=True, exist_ok=True)
        (self.polluted / "console").mkdir(parents=True, exist_ok=True)
        (self.polluted / "console/stats.py").write_text(
            "raise RuntimeError('polluted console_stats imported')\n",
            encoding="utf-8")
        self.fixture.start_app(cwd=self.arbitrary_cwd,
                                pythonpath=self.polluted)
        self.fixture.wait_health()

    def tearDown(self):
        self.fixture.close()
        shutil.rmtree(self.arbitrary_cwd, ignore_errors=True)

    def _wait_operation(self, op_id, timeout=90.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            status, payload = self.fixture.request(
                "GET", "/api/console/operations/" + op_id,
                token=self.fixture.control_token)
            self.assertEqual(status, 200, payload)
            operation = payload["operation"]
            last = operation
            if operation.get("state") in TERMINAL_STATES:
                return operation
            time.sleep(0.05)
        self.fail("operation %s did not finish: %r" % (op_id, last))

    def test_directories_requests_operations_and_exit(self):
        root = self.fixture.root.resolve()
        for key in ("state_root", "home_dir", "log_dir", "lock_dir"):
            path = Path(self.fixture.paths[key]).resolve()
            self.assertTrue(path.exists(), "%s was not created" % key)
            self.assertTrue(path == root or root in path.parents,
                            "%s escapes the fixture root" % key)
        self.assertTrue(
            Path(self.fixture.paths["ledger_file"]).is_file(),
            "fixture ledger was not used")
        assert_paths_isolated(self, self.fixture.paths, root)

        status, payload = self.fixture.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["role"], "local")
        self.assertIn("pid", payload)

        status, payload = self.fixture.request(
            "GET", "/api/console/actions",
            token=self.fixture.control_token)
        self.assertEqual(status, 200)
        # E19-1-04 step 8 / G5: the frontend's normalizeCapabilities
        # requires an OBJECT with an ``actions`` row array; a bare array throws
        # ContractError at requiredObject. Shape assertion only — the row fields
        # belong to console_operations.
        self.assertIsInstance(payload, dict, payload)
        self.assertIsInstance(payload.get("actions"), list, payload)
        self.assertEqual([row["action"] for row in payload["actions"]],
                         ["model.catalog"])
        self.assertIsInstance(payload["actions"][0]["enabled"], bool)

        status, payload = self.fixture.request(
            "POST", "/api/console/operations",
            body={"action": "model.catalog", "target": "all"},
            token=self.fixture.control_token)
        self.assertEqual(status, 201, payload)
        op_id = payload["operation"]["id"]
        operation = self._wait_operation(op_id)
        self.assertEqual(operation["state"], "succeeded", operation)
        result = operation["result"]
        self.assertEqual(result["warnings"], [], result)
        self.assertEqual(
            [model["id"] for model in result["catalogs"]["um"]],
            [self.fixture.model])

        # The plan's local closure includes the UM runtime requests, not just
        # the console catalog action: /v1/models and /v1/chat/completions.
        status, payload = self.fixture.um_models()
        self.assertEqual(status, 200, payload)
        self.assertEqual(
            [model["id"] for model in payload["data"]],
            [self.fixture.model])

        status, payload = self.fixture.um_chat("r19 local closure")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["model"], self.fixture.model)
        self.assertEqual(
            payload["choices"][0]["message"]["content"],
            "r19 local closure")

        status, payload = self.fixture.request(
            "GET", "/api/summary?from=2026-11-07&to=2026-11-07",
            token=self.fixture.control_token)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["totals"]["requests"], 1)
        self.assertEqual(payload["role"], "local")

        # E19-1-04 step 8 / G5: the frontend's readSummary(signal?) passes
        # NO date range and normalizeUmSummary reads only role/
        # assembly.role/umReference — so the bare call must succeed.
        status, payload = self.fixture.request(
            "GET", "/api/summary", token=self.fixture.control_token)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["role"], "local")
        self.assertIsInstance(payload.get("umReference"), dict, payload)

        # E19-1-04 step 7: the session code file is really consumed.
        status, payload = self.fixture.request(
            "POST", "/api/session", body={"code": "wrong-code"})
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload.get("error"), "invalid bootstrap code")
        status, payload = self.fixture.request(
            "POST", "/api/session", body={"code": self.fixture.session_code})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload, {"ok": True})
        # One-time: the same code cannot be replayed.
        status, payload = self.fixture.request(
            "POST", "/api/session", body={"code": self.fixture.session_code})
        self.assertEqual(status, 403, payload)
        # Reissue needs the control token and returns a NEW persisted code.
        status, _ = self.fixture.request("POST", "/api/session/reissue",
                                          body={})
        self.assertEqual(status, 401)
        status, payload = self.fixture.request(
            "POST", "/api/session/reissue", body={},
            token=self.fixture.control_token)
        self.assertEqual(status, 200, payload)
        new_code = payload.get("code")
        self.assertIsInstance(new_code, str)
        self.assertNotEqual(new_code, self.fixture.session_code)
        self.assertEqual(
            Path(self.fixture.paths["session_code_file"]).read_text(
                encoding="utf-8").strip(),
            new_code)
        status, payload = self.fixture.request(
            "POST", "/api/session", body={"code": new_code})
        self.assertEqual(status, 200, payload)

        # E19-1-05 step 7: log_dir/local-app.log exists and carries the
        # lifecycle lines (never a request line or body).
        log_path = Path(self.fixture.paths["log_dir"]) / "local-app.log"
        self.assertTrue(log_path.is_file(), log_path)
        log_text = log_path.read_text(encoding="utf-8")
        self.assertIn("start pid=", log_text)
        self.assertNotIn(self.fixture.control_token, log_text)

        # The pid file is consumed: it names this process while running. The
        # first field is the PID; the second is the owner's creation time, the
        # identity that keeps a recycled pid from being mistaken for it.
        pid_path = Path(self.fixture.paths["pid_file"])
        self.assertTrue(pid_path.is_file(), pid_path)
        pid_fields = pid_path.read_text(encoding="utf-8").split()
        self.assertEqual(int(pid_fields[0]), self.fixture.app.pid)
        self.assertEqual(len(pid_fields), 2,
                         "the pid file must record the owner identity: %r"
                         % (pid_fields,))
        float(pid_fields[1])

        returncode = self.fixture.shutdown_app()
        self.assertEqual(returncode, 0)
        self.assertFalse(_port_open(self.fixture.app_port),
                         "local app listener stayed open after shutdown")
        # The pid file is removed on close; the log now has the stop line.
        self.assertFalse(pid_path.exists(), pid_path)
        self.assertIn("stop pid=", log_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
