# -*- coding: utf-8 -*-
"""E19-1-10 local isolation package test (r19).

The local package built by ``r19.package_local`` is copied to TWO unrelated
temp directories and started from an arbitrary CWD with a polluted
``PYTHONPATH``.  The test asserts:

* the package carries no cloud/share/broker/display source (only
  ``share_secret_protection.py`` is allowed for the local control token);
* ``runtime_info.sys_path`` does not contain the original checkout, the
  project directory or the Git root;
* ``runtime_info.modules`` does not load ``cloud``/``usagesrv``/
  ``console_adapters``/``share_management``/``share_store``/
  ``share_broker_*``/``share_gateway``/``share_dispatch``;
* every configured path is below the package fixture root and never equal to
  or inside the real state paths (the test does not read or hash real data).
"""
from __future__ import annotations

import json
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
GIT_ROOT = AI_GATEWAY.parents[1]
sys.path.insert(0, str(TESTS_DIR))

from r19.local_fixture import (  # noqa: E402
    AI_GATEWAY as FIXTURE_GATEWAY,
    PRODUCTION_PORTS,
    LocalFixture,
    assert_paths_isolated,
    distinct_free_ports,
    http_request,
)
from r19.package_local import (  # noqa: E402
    assert_modules_inside,
    build_package,
    copy_package,
    module_files_outside,
    verify_package,
)

FORBIDDEN_MODULES = (
    "cloud",
    "usagesrv",
    "console_adapters",
    "share_management",
    "share_store",
    "share_gateway",
    "share_dispatch",
)
FORBIDDEN_FILES = (
    "usagesrv.py",
    "console_adapters.py",
    "console_share_actions.py",
    "console_share_production.py",
    "share_management.py",
    "share_store.py",
    "share_gateway.py",
    "share_dispatch.py",
    "share_instance_pipe.py",
    "share_runtime.py",
    "share_metering.py",
    "share_public_supervision.py",
    "share_backend_supervision.py",
)
TERMINAL_STATES = ("succeeded", "failed", "cancelled", "interrupted")


def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


class LocalPackageIsolationTests(unittest.TestCase):
    """The built local package carries only local sources."""

    def test_package_excludes_cloud_and_share_sources(self):
        package = build_package()
        copied = copy_package(package)
        try:
            self.assertEqual(verify_package(package), [])
            self.assertEqual(verify_package(copied), [])
            for name in FORBIDDEN_FILES:
                self.assertFalse((package / name).exists(), name)
                self.assertFalse((copied / name).exists(), name)
            self.assertFalse((copied / "cloud").exists())
            self.assertFalse((package / "web").exists())
            self.assertFalse((package / "data").exists())
            self.assertFalse((package / "profile").exists())
            self.assertTrue(
                (package / "share_secret_protection.py").is_file(),
                "the local control-token gate must travel with the package")
            self.assertTrue((package / "local" / "run_local.py").is_file())
            self.assertTrue((package / "chat2api" / "chat2api.py").is_file())
        finally:
            shutil.rmtree(package, ignore_errors=True)
            shutil.rmtree(copied, ignore_errors=True)

    def test_planted_other_end_source_is_reported(self):
        """E19-1-L2 / F5: a whole ``cloud/`` tree must be a violation.

        The previous ``verify_package`` only compared file names, so a planted
        ``cloud/run_cloud.py`` produced ``[]`` and the package passed as clean.
        """
        package = build_package()
        try:
            self.assertEqual(verify_package(package), [])
            cloud = package / "cloud"
            cloud.mkdir(parents=True, exist_ok=True)
            (cloud / "run_cloud.py").write_text(
                "raise SystemExit(0)\n", encoding="utf-8")
            problems = verify_package(package)
            self.assertTrue(problems, "a planted cloud/ tree was not reported")
            self.assertIn("cloud/", problems)

            (cloud / "run_cloud.py").unlink()
            cloud.rmdir()
            (package / "usagesrv.py").write_text(
                "raise SystemExit(0)\n", encoding="utf-8")
            problems = verify_package(package)
            self.assertIn("usagesrv.py", problems)
        finally:
            shutil.rmtree(package, ignore_errors=True)

    def test_fixture_ports_never_collide_with_production(self):
        """E19-1-L2 / F33: the allocator honours the constraint it verifies."""
        ports = distinct_free_ports(4)
        self.assertEqual(len(set(ports)), 4)
        self.assertFalse(PRODUCTION_PORTS.intersection(ports),
                         sorted(PRODUCTION_PORTS.intersection(ports)))


class LocalIsolatedRuntimeTests(unittest.TestCase):
    """The copied package starts and serves without the original checkout."""

    def setUp(self):
        self.package = build_package()
        self.copied = copy_package(self.package)
        self.fixture = LocalFixture(
            self.copied, root=self.copied / "fixture")
        self.arbitrary_cwd = Path(tempfile.mkdtemp(prefix="r19-iso-cwd-"))
        self.polluted = self.copied / "fixture" / "polluted"
        self.polluted.mkdir(parents=True, exist_ok=True)
        (self.polluted / "console_stats.py").write_text(
            "raise RuntimeError('polluted console_stats imported')\n",
            encoding="utf-8")

    def tearDown(self):
        try:
            self.fixture.close()
        finally:
            shutil.rmtree(self.arbitrary_cwd, ignore_errors=True)
            shutil.rmtree(self.copied, ignore_errors=True)
            shutil.rmtree(self.package, ignore_errors=True)

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

    def test_isolated_package_starts_without_the_checkout(self):
        info_result = self.fixture.run_runtime_info(
            cwd=self.arbitrary_cwd, pythonpath=self.polluted)
        self.assertEqual(info_result.returncode, 0, info_result.stderr)
        info = json.loads(info_result.stdout)
        sys_path = [Path(entry).resolve() for entry in info["sys_path"]]
        self.assertIn(self.copied.resolve(), sys_path)
        self.assertNotIn(AI_GATEWAY.resolve(), sys_path,
                         "the original checkout leaked into sys.path")
        self.assertNotIn(PROJECT_ROOT.resolve(), sys_path)
        self.assertNotIn(GIT_ROOT.resolve(), sys_path)
        self.assertNotIn(str(self.polluted), info.get("PYTHONPATH") or "")
        for name in FORBIDDEN_MODULES:
            self.assertNotIn(name, info["modules"])
        # E19-1-L2 / F23: the module NAMES cannot prove the code came from the
        # package — a same-named module loaded from the checkout looks identical
        # in ``modules``. Every resolved file must be the package, the stdlib or
        # the host site-packages.
        self.assertIn("module_files", info)
        assert_modules_inside(self.copied, info["module_files"])
        self.assertEqual(
            module_files_outside(self.copied, info["module_files"]), [])
        assert_paths_isolated(self, info["config_paths"],
                              self.fixture.root)
        self.assertFalse(
            Path(info["config_paths"]["um_runtime.profile_dir"]).resolve()
            == (FIXTURE_GATEWAY / "profile").resolve())

        self.fixture.start_app(cwd=self.arbitrary_cwd,
                                pythonpath=self.polluted)
        self.fixture.wait_health()

        status, payload = self.fixture.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["role"], "local")

        status, payload = self.fixture.request(
            "GET", "/api/console/actions",
            token=self.fixture.control_token)
        self.assertEqual(status, 200)
        # E19-1-04 step 8 / G5: the frontend contract is an OBJECT with an
        # ``actions`` row array (a bare array throws ContractError at
        # normalizeCapabilities' requiredObject). Shape assertion only.
        self.assertIsInstance(payload, dict, payload)
        self.assertIsInstance(payload.get("actions"), list, payload)
        self.assertEqual([row["action"] for row in payload["actions"]],
                         ["model.catalog"])

        status, payload = self.fixture.request(
            "POST", "/api/console/operations",
            body={"action": "model.catalog", "target": "all"},
            token=self.fixture.control_token)
        self.assertEqual(status, 201, payload)
        operation = self._wait_operation(payload["operation"]["id"])
        self.assertEqual(operation["state"], "succeeded", operation)
        result = operation["result"]
        self.assertEqual(result["warnings"], [], result)
        self.assertEqual(
            [model["id"] for model in result["catalogs"]["um"]],
            [self.fixture.model])

        status, payload = self.fixture.um_models()
        self.assertEqual(status, 200, payload)
        self.assertEqual(
            [model["id"] for model in payload["data"]],
            [self.fixture.model])

        status, payload = self.fixture.um_chat("r19 isolated closure")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["model"], self.fixture.model)
        self.assertEqual(
            payload["choices"][0]["message"]["content"],
            "r19 isolated closure")

        # E19-1-L2 / F25: the real 8000/8001/18096 chain may be listening
        # while this test runs. The isolated package must use only its own
        # temporary ports, and its traffic must land on the fixture's upstream —
        # never on a production listener. Asserted from the fixture's own control
        # plane (no connection is opened to a production port).
        values = self.fixture.config_mapping()
        for field, value in (("port", values["port"]),
                             ("um_runtime.port",
                              values["um_runtime"]["port"])):
            self.assertNotIn(value, PRODUCTION_PORTS, field)
        upstream_port = int(
            values["um_runtime"]["base_url"].rsplit(":", 1)[1])
        self.assertEqual(upstream_port, self.fixture.fake_um_port)
        self.assertNotIn(upstream_port, PRODUCTION_PORTS)
        status, state = http_request(
            self.fixture.fake_um_port, "GET", "/__control/state")
        self.assertEqual(status, 200, state)
        # ``/api/config`` is the fixture's own readiness probe; the proof is that
        # the chat completion the isolated package relayed arrived HERE, on the
        # fixture's temporary upstream, and not on a production listener.
        paths = [entry.get("path") for entry in state.get("recent") or []]
        self.assertIn("/api/chat/completions", paths, paths)

        status, payload = self.fixture.request(
            "GET", "/api/summary?from=2026-11-07&to=2026-11-07",
            token=self.fixture.control_token)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["totals"]["requests"], 1)

        returncode = self.fixture.shutdown_app()
        self.assertEqual(returncode, 0)
        self.assertFalse(_port_open(self.fixture.app_port),
                         "isolated package listener stayed open")


if __name__ == "__main__":
    unittest.main()
