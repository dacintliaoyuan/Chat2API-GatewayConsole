# -*- coding: utf-8 -*-
"""E19-1-12 cloud fixture, RED/GREEN and isolation package (r19).

The cloud chain runs the REAL package processes on loopback only:

    fake UM (verification upstream)
    share/broker_ops.py::linux_main (AF_UNIX broker, spawns 8100)
    chat2api/chat2api.py (8100 role, spawned by the broker)
    cloud/run_cloud.py (18096 role, owns the 8110 chat2api backend)

Every path, secret, token, ledger, projection and SQLite file lives under one
private temporary root. No production port (8000/8001/8100/8110/18096), real UM
credential, repository data or Windows process is touched.

Windows skips this module; a Windows SKIP is never reported as a pass.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

if os.name != "posix":
    raise unittest.SkipTest("cloud chain requires POSIX (Linux/WSL)")

ROOT = Path(__file__).resolve().parents[1]       # ai-gateway/
REPO = ROOT.parent                             # SelfUseAI/
TESTS = ROOT / "tests"
for _entry in (str(TESTS), str(ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import r19.cloud_fixture as cloud_fixture  # noqa: E402
import r19.package_cloud as package_cloud  # noqa: E402

PACKAGE = None


def setUpModule():
    global PACKAGE
    PACKAGE = package_cloud.build_cloud_package()


def tearDownModule():
    if PACKAGE is not None:
        shutil.rmtree(PACKAGE, ignore_errors=True)


class CloudPackageTests(unittest.TestCase):
    def test_package_contains_the_cloud_closure_only(self):
        required = (
            "cloud/run_cloud.py",
            "cloud/backend/app.py",
            "cloud/backend/config.py",
            "shared/__init__.py",
            "chat2api/chat2api.py",
            "chat2api/share_auth.py",
            "chat2api/share_dispatch.py",
            "share/broker_ops.py",
            "share/broker_server.py",
            "share/store.py",
            "share/management.py",
            "console/share_production.py",
            "tests/fake_um/fake_um_server.py",
        )
        for relative in required:
            self.assertTrue((PACKAGE / relative).is_file(), relative)
        self.assertEqual(package_cloud.forbidden_entries(PACKAGE), [])
        self.assertFalse((PACKAGE / "local").exists())
        self.assertFalse((PACKAGE / "usagesrv.py").exists())
        self.assertFalse((PACKAGE / "model_sync/sync.py").exists())
        self.assertFalse((PACKAGE / "console/stats.py").exists())
        self.assertFalse((PACKAGE / "chat2api" / "token.json").exists())
        self.assertFalse(
            (PACKAGE / "chat2api" / ".edge-profile").exists())

    def test_package_is_not_built_inside_the_checkout(self):
        self.assertNotEqual(PACKAGE.resolve(), ROOT.resolve())
        self.assertFalse(PACKAGE.resolve().is_relative_to(ROOT.resolve()))


class CloudConfigTests(unittest.TestCase):
    def test_missing_field_check_config_exits_2(self):
        fixture = cloud_fixture.CloudChainFixture(PACKAGE)
        try:
            values = fixture.config_values()
            values.pop("share_store_file")
            missing = fixture.root / "config" / "cloud-missing.json"
            missing.parent.mkdir(parents=True, exist_ok=True)
            missing.write_text(json.dumps(values), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-I",
                 str(PACKAGE / "cloud" / "run_cloud.py"),
                 "--config", str(missing), "--check-config"],
                cwd=str(fixture.root), env=fixture.env,
                capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("share_store_file", result.stderr)
        finally:
            fixture.stop_all()
            shutil.rmtree(fixture.root, ignore_errors=True)

    def test_fixture_config_paths_are_isolated(self):
        fixture = cloud_fixture.CloudChainFixture(PACKAGE)
        try:
            values = fixture.config_values()
            root = fixture.root.resolve()
            source_fields = {
                "backend_gateway_dir", "backend_python", "host",
                "backend_base_url",
            }
            for name, value in values.items():
                if not isinstance(value, str) or name in source_fields:
                    continue
                path = Path(value).resolve()
                self.assertTrue(
                    path == root or root in path.parents,
                    "%s=%s escapes %s" % (name, value, root))
            self.assertNotEqual(values["port"], 18096)
            self.assertNotEqual(values["backend_port"], 8110)
            self.assertEqual(values["backend_base_url"],
                             fixture.fake_base_url)
        finally:
            shutil.rmtree(fixture.root, ignore_errors=True)


class CloudChainTests(unittest.TestCase):
    def setUp(self):
        self.fixture = cloud_fixture.CloudChainFixture(PACKAGE)
        try:
            self.fixture.start_all()
        except BaseException:
            self.fixture.stop_all()
            raise

    def tearDown(self):
        self.fixture.stop_all()

    def test_key_lifecycle_models_chat_revoke_and_drain(self):
        fixture = self.fixture
        created = fixture.create_key("fixture-key", max_concurrency=4)
        self.assertTrue(created["published"], created)
        self.assertIsNone(created.get("publication_error"), created)
        self.assertTrue(created["in_sync"], created)
        self.assertEqual(created["desired_generation"],
                         created["applied_generation"])
        self.assertTrue(created["secret"].startswith("um-share-"))

        models = fixture.share_models(created["secret"])
        self.assertEqual(models.status, 200, models.text())
        ids = [item.get("id") for item in
               (models.json() or {}).get("data", [])]
        self.assertIn("fake-um-model", ids)

        chat = fixture.chat_backend(created["secret"], "hello-cloud")
        self.assertEqual(chat.status, 200, chat.text())
        payload = chat.json() or {}
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        self.assertEqual(message.get("content"), "hello-cloud")

        fixture.arm_chat_barrier()
        result_holder = {}
        thread = threading.Thread(
            target=lambda: result_holder.setdefault(
                "result",
                fixture.share_chat_stream(created["secret"], "held-cloud")),
            name="r19-inflight", daemon=True)
        thread.start()
        try:
            fixture.wait_barrier_arrivals(1, timeout=20.0)
            revoked = fixture.revoke_key(
                created["key_id"], created["row_version"])
            self.assertFalse(revoked["enabled"], revoked)
            self.assertTrue(revoked["published"], revoked)
            interrupted = ((revoked.get("terminated") or 0)
                           + (revoked.get("aborted") or 0))
            self.assertGreaterEqual(interrupted, 1, revoked)
            thread.join(20.0)
            self.assertFalse(thread.is_alive(),
                             "the in-flight request did not finish")
            inflight = result_holder.get("result")
            self.assertIsNotNone(inflight)
            self.assertTrue(
                inflight.error is not None or inflight.status != 200,
                "the revoked request still completed successfully: %r"
                % (inflight.as_record(),))
        finally:
            fixture.release_barrier()

        denied = fixture.share_models(created["secret"])
        self.assertEqual(denied.status, 401, denied.text())

        projection = fixture.projection()
        rows = [row for row in projection.get("data", [])
                if row.get("key_id") == created["key_id"]]
        self.assertEqual(len(rows), 1, projection)
        self.assertFalse(rows[0]["enabled"], rows[0])

        drained = fixture.drain()
        self.assertEqual(drained.status, 200, drained.text())
        drained_body = drained.json() or {}
        self.assertTrue(drained_body.get("ok"), drained_body)
        drained_result = drained_body.get("result") or {}
        self.assertTrue(drained_result.get("drained"), drained_body)
        self.assertFalse(drained_result.get("timed_out"), drained_body)
        self.assertFalse(drained_result.get("notification_failed"),
                         drained_body)
        self.assertEqual(drained_result.get("in_flight"), 0, drained_body)
        self.assertTrue(fixture.wait_share_port_closed(20.0))

        backend = fixture.management_call("GET", "/api/share/backend")
        self.assertEqual(backend.status, 200, backend.text())
        self.assertTrue((backend.json() or {}).get("running"), backend.text())


class CloudIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.second = Path(tempfile.mkdtemp(prefix="r19-cloud-iso-"))
        cls.package = cls.second / "pkg"
        shutil.copytree(PACKAGE, cls.package)
        local = cls.package / "local"
        if local.exists():
            shutil.rmtree(local)
        cls.local_removed = local.exists()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.second, ignore_errors=True)

    def test_package_starts_without_local_sources(self):
        self.assertFalse(self.local_removed)
        self.assertEqual(
            package_cloud.forbidden_entries(self.package), [])
        fixture = cloud_fixture.CloudChainFixture(self.package)
        try:
            fixture.prepare()
            fixture.start_fake_um()
            fixture.start_management()
            health = fixture.health()
            self.assertEqual(health.status, 200, health.text())
            self.assertEqual((health.json() or {}).get("role"), "cloud")
            status = fixture.management_call("GET", "/api/share/backend")
            self.assertEqual(status.status, 200, status.text())
            self.assertTrue((status.json() or {}).get("running"),
                            status.text())
        finally:
            fixture.stop_all()
            shutil.rmtree(fixture.root, ignore_errors=True)

    def test_the_summary_read_face_answers_in_the_controlled_chain(self):
        """spec-cloud-003 / plan §2.3: the summary read face must really answer.

        The cloud fixture never named ``price_table_file``, so the ONLY
        controlled chain answered ``503 summary_unavailable`` for every request
        and the read face had no passing evidence. The fixture now names the
        explicit price identity, so a 200 with the normalized price facts is the
        expected answer.
        """
        fixture = cloud_fixture.CloudChainFixture(self.package)
        try:
            fixture.prepare()
            fixture.start_fake_um()
            fixture.start_management()
            response = fixture.management_call("GET", "/api/summary")
            self.assertEqual(response.status, 200, response.text())
            payload = response.json() or {}
            self.assertEqual(payload.get("role"), "cloud")
            self.assertIn("priceVersions", payload)
            self.assertNotEqual(payload.get("error"), "summary_unavailable")
        finally:
            fixture.stop_all()
            shutil.rmtree(fixture.root, ignore_errors=True)

    def test_import_surface_excludes_local_and_legacy_hosts(self):
        probe = self.second / "probe_modules.py"
        probe.write_text(
            "import json, sys\n"
            "sys.path.insert(0, %r)\n"
            "import cloud.backend.app\n"
            # The summary module is the ONE place that imports ``shared``; a
            # composition-root import alone would not load it, so the probe
            # imports it too (it is part of the cloud closure).
            "import cloud.backend.summary\n"
            "forbidden = [name for name in (\n"
            "    'usagesrv', 'model_sync', 'local', 'console.adapters',\n"
            "    'console_stats', 'zcode_hot_sync') if name in sys.modules]\n"
            "print(json.dumps({'forbidden': forbidden,\n"
            "                  'cloud': cloud.backend.app.__file__,\n"
            "                  'module_files': {\n"
            "                      name: getattr(module, '__file__', None)\n"
            "                      for name, module in sys.modules.items()\n"
            "                      if name not in ('__main__', 'sitecustomize')\n"
            "                      and not name.startswith('_frozen')}}))\n"
            % str(self.package),
            encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(REPO), str(ROOT / "chat2api")])
        # E19-1-06 step 1: the entry refuses to import without -I, so the
        # probe must itself be isolated (the inherited PYTHONPATH is still
        # handed over: the point is that _clean_sys_path drops it).
        result = subprocess.run(
            [sys.executable, "-I", str(probe)], cwd=str(self.second),
            env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["forbidden"], [])
        self.assertTrue(
            Path(payload["cloud"]).resolve().is_relative_to(
                self.package.resolve()), payload["cloud"])
        # spec-shared-11 / A11: the module __file__ map must prove EVERY
        # loaded module came from inside the package. The old case only checked
        # six names and ``cloud.backend.app.__file__``, so a package that
        # dropped ``shared/pricing_rules.py`` and resolved it from the checkout
        # still passed.
        module_files = payload.get("module_files")
        self.assertIsInstance(module_files, dict, payload)
        self.assertTrue(
            any(name == "shared" or name.startswith("shared.")
                for name in module_files),
            "the probe must report the shared module paths: %r"
            % sorted(module_files))
        package_cloud.assert_modules_inside(self.package, module_files)

    def test_entry_cleans_the_polluted_sys_path(self):
        probe = self.second / "probe_path.py"
        probe.write_text(
            "import importlib.util, json, os, sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, %r)\n"
            "spec = importlib.util.spec_from_file_location(\n"
            "    'r19_run_cloud_probe', %r)\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "module._clean_sys_path()\n"
            "print(json.dumps({\n"
            "    'paths': [str(Path(entry or '.').resolve())\n"
            "               for entry in sys.path],\n"
            "    'PYTHONPATH': os.environ.get('PYTHONPATH', '')}))\n"
            % (str(ROOT),
               str(self.package / "cloud" / "run_cloud.py")),
            encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        # E19-1-06 step 1: the entry refuses to import without -I, so the
        # probe must itself be isolated (the inherited PYTHONPATH is still
        # handed over: the point is that _clean_sys_path drops it).
        result = subprocess.run(
            [sys.executable, "-I", str(probe)], cwd=str(self.second),
            env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        paths = payload["paths"]
        self.assertEqual(Path(paths[0]).resolve(),
                         self.package.resolve())
        self.assertNotIn(str(ROOT.resolve()), paths)
        self.assertEqual(
            payload["PYTHONPATH"], "",
            "an inherited PYTHONPATH must not reach the 8110 child")


if __name__ == "__main__":
    unittest.main()
