# -*- coding: utf-8 -*-
"""E19-1-11 cloud composition root regression (r19).

The cloud application is an INDEPENDENT composition root:

* ``CloudConfig`` validates every field (missing/invalid -> exit 2) without
  binding ports, creating directories or starting children;
* ``KeyApi`` applies the explicit process environment BEFORE constructing the
  ``ShareManager``/``BrokerClientAdapter`` and delegates create/get/list/
  revoke/rotate/status/drain;
* ``BackendProcess`` starts/stops 8110 through ``production_backend_ops`` with
  an explicit ``BackendConfig`` and reports status through the READ-ONLY
  ``verify_running(forget_stale=False)`` + ``backend_pid_state()`` evidence
  (the backend ops object has no ``status`` method);
* ``CloudApp`` registers ONLY cloud routes, enforces the control token and the
  loopback Host/Origin gates, and never imports ``usagesrv`` /
  ``console_adapters`` / ``local`` / ``model_sync``;
* the Windows full start honestly refuses with exit 3 (requires POSIX); the
  ``--check-config`` path still works on Windows.

The HTTP tests use a real loopback listener with fake keys/backend seams, so no
product process, state directory or port is touched.
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
RUN_CLOUD = ROOT / "cloud" / "run_cloud.py"
sys.path.insert(0, str(ROOT))

import cloud.backend.app as app_module  # noqa: E402
from cloud.backend.app import CloudApp  # noqa: E402
from console.share_production import (  # noqa: E402
    BackendConfig, production_backend_ops)
from cloud.backend.backend_process import (  # noqa: E402
    BackendProcess,
    BackendProcessError,
)
from cloud.backend.config import (  # noqa: E402
    FORBIDDEN_PORTS, PROCESS_ENV_FIELDS, CloudConfig, CloudConfigError)

from cloud.backend.keys_api import KeyApi  # noqa: E402
from cloud.backend.summary import build_cloud_summary  # noqa: E402
from share.management import BrokerError  # noqa: E402

PROBE_MARKER = "R19_PROBE_JSON:"
FIXTURE_CLOUD_MODEL = "cloud-fixture-model"
PROCESS_ENV_NAMES = tuple(name for name, _ in PROCESS_ENV_FIELDS)


# --------------------------------------------------------------------- helpers
def _free_port():
    """One currently free loopback port that is NOT a production listener.

    The previous version only bound port 0 and accepted whatever came back, so
    the test's own allocator ignored the constraint it verifies
    (``FORBIDDEN_PORTS``). 8000/8001/8100/8110/18096/5901 may be listening on
    this machine right now; allocating one would collide with a live service.
    """
    for _ in range(100):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in FORBIDDEN_PORTS:
            return port
    raise RuntimeError("no free loopback port outside the production ports")


def _write_private(path, text):
    """Write one test secret with the 0600 mode the POSIX gate requires.

    ``share_secret_protection`` refuses a control/instance key that is wider than
    0600, so a fixture that writes with the default umask makes every POSIX run fail
    with ``control key is missing or malformed``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if os.name == "posix":
        os.chmod(path, 0o600)
    return path


def _save_env(names):
    return {name: os.environ.get(name) for name in names}


def _restore_env(saved):
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _write_config(root, values=None, *, omit=None, name="cloud-config.json"):
    """Write one valid config document under ``root`` and return its path.

    F30: the tests used to read ``REPO/tmp/r19-e19-1/cloud-config.json``, but
    ``tmp/`` is gitignored (``git ls-files tmp/r19-e19-1`` -> 0), so the
    document only existed on the machine that produced it. Every case now builds
    its own temporary root instead.
    """
    root = Path(root)
    values = dict(values if values is not None else _fixture_config(root))
    if omit is not None:
        values.pop(omit, None)
    path = root / "config" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values, indent=2), encoding="utf-8")
    return path


def _fixture_config(root, **overrides):
    root = Path(root)
    port = overrides.pop("port", None) or _free_port()
    values = {
        "schema_version": 1,
        "host": "127.0.0.1",
        "port": port,
        "state_root": str(root / "state"),
        "home_dir": str(root / "home"),
        "config_dir": str(root / "config"),
        "log_dir": str(root / "logs"),
        "lock_dir": str(root / "locks"),
        "pid_file": str(root / "state" / "cloud-app.pid"),
        "control_token_file": str(root / "config" / "admin-control-token"),
        "share_run_root": str(root / "run"),
        "share_store_file": str(root / "state" / "share-store.db"),
        "share_secret_key_file": str(root / "config" / "share-secret.key"),
        "console_state_file": str(root / "state" / "console-state.sqlite3"),
        "console_lock_dir": str(root / "locks" / "console"),
        "share_management_config":
            str(root / "config" / "share-management.json"),
        "broker_socket": str(root / "run" / "broker.sock"),
        "broker_key_file": str(root / "config" / "broker-control.key"),
        "projection_file": str(root / "run" / "share-projection.json"),
        "attribution_key_file": str(root / "config" / "share-attribution.key"),
        "instance_key_file": str(root / "config" / "instance.key"),
        "registration_file": str(root / "run" / "instance-registration.json"),
        "instance_prefix": str(root / "run" / "instance-"),
        "revoke_state_file": str(root / "state" / "share-revoke.state"),
        "host_state_dir": str(root / "run"),
        "host_pid_file": str(root / "run" / "share-host.pid"),
        "broker_dir": str(root / "broker"),
        "backend_dir": str(root / "backend"),
        "backend_port": 28110,
        "backend_base_url": "http://127.0.0.1:28999",
        "backend_gateway_dir": str(ROOT / "chat2api"),
        "backend_token_file": str(root / "backend" / "token.json"),
        "backend_profile_dir": str(root / "backend" / "edge-profile"),
        "backend_service_log": str(root / "backend" / "share-backend.log"),
        "backend_usage_log":
            str(root / "backend" / "usage-share-backend.jsonl"),
        "backend_control_token_file": str(root / "backend" / "control-token"),
        "backend_reauth_slot_file":
            str(root / "backend" / "chat2api-reauth.state"),
        "backend_python": sys.executable,
        "backend_start_timeout_s": 5.0,
        "backend_stop_timeout_s": 5.0,
    }
    values.update(overrides)
    return values


class _FakeManager:
    def __init__(self):
        self.calls = []

    def status(self):
        self.calls.append(("status",))
        return {"desired_generation": 1, "applied_generation": 1,
                "in_sync": True, "share_max_concurrency": 64}

    def create_key(self, name, max_concurrency=None, idempotency_key=None):
        self.calls.append(("create", name, max_concurrency, idempotency_key))
        return {"key_id": "key-1", "secret": "um-share-secret",
                "name": name, "max_concurrency": max_concurrency or 16,
                "published": True, "desired_generation": 1,
                "applied_generation": 1, "in_sync": True}

    def get_key(self, key_id):
        self.calls.append(("get", key_id))
        return {"key_id": key_id, "name": "fixture-key"}

    def list_keys(self, include_deleted=False):
        self.calls.append(("list", include_deleted))
        return {"keys": [{"key_id": "key-1"}]}

    def revoke_and_terminate_key(self, key_id, expected_row_version=None,
                                 idempotency_key=None):
        self.calls.append(("revoke", key_id, expected_row_version,
                           idempotency_key))
        return {"key_id": key_id, "enabled": False,
                "published": True, "terminated": 1}

    def rotate_key(self, key_id, idempotency_key=None,
                   expected_row_version=None):
        self.calls.append(("rotate", key_id, idempotency_key,
                           expected_row_version))
        return {"key_id": key_id, "secret": "um-share-rotated",
                "published": True}


class _FakeBrokerClient:
    def __init__(self):
        self.drain_calls = 0

    def drain(self):
        self.drain_calls += 1
        # The confirmed shape from plan §2.6: drained with no timed-out wait, no
        # failed notification and an OBSERVED zero in-flight count. ``in_flight``
        # is not optional — ``None`` means "no observation resolved a count",
        # which is not a confirmed stop (D18-10).
        return _BrokerResult(True, {"drained": True, "timed_out": False,
                                    "stopped": True,
                                    "notification_failed": False,
                                    "in_flight": 0}, None)


class _BrokerResult:
    def __init__(self, ok, result, error_code):
        self.ok = ok
        self.result = result
        self.error_code = error_code


class _FakeBroker:
    def __init__(self):
        self.client = _FakeBrokerClient()


class _FakeBackend:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1
        return True

    def stop(self):
        self.stopped += 1
        return True

    def status(self):
        return {"running": False,
                "pid_state": {"state": "pid_file_absent", "pid": None},
                "port": 28110}


class _FakeOps:
    def __init__(self, *, running=False):
        self.running = running
        self.start_calls = 0
        self.stop_calls = 0
        self.verify_calls = []
        self.status_calls = 0

    def start(self):
        self.start_calls += 1
        self.running = True
        return True

    def stop(self):
        self.stop_calls += 1
        self.running = False
        return True

    def verify_running(self, forget_stale=True):
        self.verify_calls.append(forget_stale)
        return self.running

    def backend_pid_state(self):
        self.status_calls += 1
        return {"state": "running" if self.running else "pid_file_absent",
                "pid": 4242 if self.running else None}


def _request(port, method, path, *, body=None, token=None, host=None,
              timeout=5.0):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        headers = {}
        if token is not None:
            headers["X-Admin-Control"] = token
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if host is not None:
            connection.putrequest(method, path, skip_host=True)
            connection.putheader("Host", host)
            for name, value in headers.items():
                connection.putheader(name, value)
            if payload is not None:
                connection.putheader("Content-Length", str(len(payload)))
            connection.endheaders()
            connection.send(payload or b"")
        else:
            connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        parsed = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError):
                parsed = None
        return response.status, parsed
    finally:
        connection.close()


# ------------------------------------------------------------------ config
class CloudConfigTests(unittest.TestCase):
    def test_missing_required_field_is_named(self):
        values = _fixture_config(tempfile.mkdtemp(prefix="r19-cloud-cfg-"))
        values.pop("share_store_file")
        with self.assertRaises(CloudConfigError) as caught:
            CloudConfig.from_mapping(values)
        self.assertEqual(caught.exception.field, "share_store_file")
        self.assertIn("share_store_file", str(caught.exception))

    def test_invalid_host_port_and_relative_paths_are_refused(self):
        base = _fixture_config(tempfile.mkdtemp(prefix="r19-cloud-cfg-"))
        cases = (
            ("host", "0.0.0.0"),
            ("host", "example.com"),
            ("port", 0),
            ("port", 70000),
            ("port", True),
            ("share_store_file", "relative/share.db"),
            ("backend_port", 0),
            ("backend_base_url", "ftp://example.com"),
            ("backend_gateway_dir", str(ROOT / "usagesrv.py")),
            ("backend_start_timeout_s", 0),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                values = dict(base)
                values[field] = value
                with self.assertRaises(CloudConfigError) as caught:
                    CloudConfig.from_mapping(values)
                self.assertEqual(caught.exception.field, field)

    def test_unknown_field_is_refused(self):
        values = _fixture_config(tempfile.mkdtemp(prefix="r19-cloud-cfg-"))
        values["unexpected_field"] = "x"
        with self.assertRaises(CloudConfigError) as caught:
            CloudConfig.from_mapping(values)
        self.assertEqual(caught.exception.field, "unexpected_field")

    def test_missing_share_run_root_is_refused(self):
        """No production run root default: the field must be explicit.

        Falling back to ``/var/lib/um-share`` would make a production config
        that forgot the override write the real deployment state.
        """
        values = _fixture_config(tempfile.mkdtemp(prefix="r19-cloud-cfg-"))
        values.pop("share_run_root")
        with self.assertRaises(CloudConfigError) as caught:
            CloudConfig.from_mapping(values)
        self.assertEqual(caught.exception.field, "share_run_root")

    def test_external_backend_gateway_dir_is_refused(self):
        """The 8110 source must be the in-package ``chat2api`` directory."""
        values = _fixture_config(tempfile.mkdtemp(prefix="r19-cloud-cfg-"))
        external = Path(tempfile.mkdtemp(prefix="r19-external-")) / "chat2api"
        values["backend_gateway_dir"] = str(external)
        with self.assertRaises(CloudConfigError) as caught:
            CloudConfig.from_mapping(values)
        self.assertEqual(caught.exception.field, "backend_gateway_dir")

    def test_full_fixture_config_loads(self):
        root = Path(tempfile.mkdtemp(prefix="r19-cloud-cfg-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        values = _fixture_config(root)
        config = CloudConfig.from_file(_write_config(root, values))
        self.assertEqual(config.port, values["port"])
        self.assertEqual(config.backend_port, 28110)
        self.assertEqual(config.host, "127.0.0.1")

    def test_fixture_paths_are_isolated(self):
        root = Path(tempfile.mkdtemp(prefix="r19-cloud-cfg-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fixture = json.loads(
            _write_config(root).read_text(encoding="utf-8"))
        resolved = root.resolve()
        source_fields = {"backend_gateway_dir", "backend_python",
                         "host", "backend_base_url"}
        for name, value in fixture.items():
            if not isinstance(value, str) or name in source_fields:
                continue
            path = Path(value).resolve()
            self.assertTrue(path == resolved or resolved in path.parents,
                            "%s=%s escapes %s" % (name, value, resolved))
        self.assertNotIn(fixture["port"], FORBIDDEN_PORTS)
        self.assertNotIn(fixture["backend_port"], FORBIDDEN_PORTS)
        self.assertNotEqual(fixture["port"], 18096)
        self.assertNotEqual(fixture["backend_port"], 8110)
        self.assertEqual(fixture["backend_base_url"],
                         "http://127.0.0.1:28999")

    def test_check_config_cli_exit_codes(self):
        root = Path(tempfile.mkdtemp(prefix="r19-cloud-cfg-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fixture = _write_config(root)
        missing = _write_config(root, omit="share_store_file",
                                name="cloud-missing.json")
        good = subprocess.run(
            [sys.executable, "-I", str(RUN_CLOUD),
             "--config", str(fixture), "--check-config"],
            capture_output=True, text=True, timeout=120,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertIn('"status": "ok"', good.stdout)
        bad = subprocess.run(
            [sys.executable, "-I", str(RUN_CLOUD),
             "--config", str(missing), "--check-config"],
            capture_output=True, text=True, timeout=120,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(bad.returncode, 2)
        self.assertIn("share_store_file", bad.stderr)

    @unittest.skipIf(os.name == "posix",
                     "Windows-only honest refusal check")
    def test_the_entry_refuses_a_non_isolated_interpreter(self):
        """standards-tests-06: the cloud ``-I`` guard has a regression case.

        Without ``-I`` a planted ``json.py``/``argparse.py`` on the inherited
        ``sys.path`` would execute during import. No test ran the entry WITHOUT
        ``-I``, so deleting the guard would not turn anything red.
        """
        result = subprocess.run(
            [sys.executable, str(RUN_CLOUD), "--help"],
            capture_output=True, text=True, timeout=120,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("must run as", result.stderr)
        self.assertIn("-I", result.stderr)

    def test_windows_full_start_exits_3_requires_posix(self):
        root = Path(tempfile.mkdtemp(prefix="r19-cloud-cfg-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fixture = _write_config(root)
        result = subprocess.run(
            [sys.executable, "-I", str(RUN_CLOUD),
             "--config", str(fixture)],
            capture_output=True, text=True, timeout=120,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(result.returncode, 3)
        self.assertIn("requires POSIX", result.stderr)

    @unittest.skipIf(os.name == "posix",
                     "Windows-only honest refusal check")
    def test_windows_check_config_still_exits_0(self):
        """The POSIX refusal must not swallow the validation path.

        ``--check-config`` answers 0 on Windows (it binds nothing); only a real
        start returns 3. A guard that refused both would make the config
        unverifiable on the operator's own machine.
        """
        root = Path(tempfile.mkdtemp(prefix="r19-cloud-cfg-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fixture = _write_config(root)
        result = subprocess.run(
            [sys.executable, "-I", str(RUN_CLOUD),
             "--config", str(fixture), "--check-config"],
            capture_output=True, text=True, timeout=120,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("requires POSIX", result.stderr)
        self.assertIn('"runtime_mode": "production"', result.stdout)

    @unittest.skipIf(os.name == "posix",
                     "the Windows refusal branch only runs on Windows")
    def test_windows_main_returns_3_for_a_full_start(self):
        """``app.main`` (no ``--check-config``) must return 3, not start.

        The entry returns the code; it must not raise, and it must not have
        built the app (which would try to bind a listener and spawn children).
        """
        root = Path(tempfile.mkdtemp(prefix="r19-cloud-cfg-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        config = CloudConfig.from_mapping(_fixture_config(root))
        built = []
        with mock.patch.object(app_module, "build_app",
                               lambda config, **kwargs: built.append(config)):
            code = app_module.main(config)
        self.assertEqual(code, 3)
        self.assertEqual(built, [])


class CloudGuardTests(unittest.TestCase):
    """E19-1-06 steps 3-6: the isolation guards refuse at --check-config.

    Each probe mutates ONE field of a fully valid fixture document and runs the
    real entry; the guard must exit 2 (never 0) and name the field. Nothing is
    bound, created or started: --check-config stops before any side effect.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="r19-cloud-guard-"))
        self.config_dir = self.root / "config"
        self.config_dir.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, *, mutate=None, omit=None):
        values = _fixture_config(self.root)
        if omit:
            values.pop(omit, None)
        if mutate:
            mutate(values)
        path = self.config_dir / "cloud-config.json"
        path.write_text(json.dumps(values, indent=2), encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-I", str(ROOT / "cloud" / "run_cloud.py"),
             "--config", str(path), "--check-config"],
            capture_output=True, text=True, timeout=120,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))

    def test_valid_fixture_passes(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"status": "ok"', result.stdout)

    def test_production_ports_are_refused(self):
        for field, value in (("port", 18096), ("backend_port", 8110),
                             ("port", 8001), ("backend_port", 8000)):
            with self.subTest(field=field, value=value):
                result = self._run(mutate=lambda v, f=field, x=value: v.update(
                    {f: x}))
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(field, result.stderr)

    def test_production_and_remote_upstreams_are_refused(self):
        """E19-1-C1 item 4: the two named refusals, each for its own reason.

        ``http://127.0.0.1:8110`` is loopback but names the production backend
        port; ``https://evil.example.com`` is remote. Both must exit 2 and name
        ``backend_base_url``; asserting the REASON keeps the case from passing for
        the wrong guard.
        """
        cases = (
            ("http://127.0.0.1:8110", "production port"),
            ("https://evil.example.com", "loopback"),
            ("http://127.0.0.1:8000", "production port"),
            ("http://10.0.0.5:28999", "loopback"),
        )
        for url, reason in cases:
            with self.subTest(url=url):
                result = self._run(
                    mutate=lambda v, u=url: v.update(backend_base_url=u))
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("backend_base_url", result.stderr)
                self.assertIn(reason, result.stderr)

    def test_the_common_parent_is_never_a_filesystem_root(self):
        """standards-config-08: a root common parent must not be an allow root.

        When the configured roots sit in different top-level directories their
        common parent is the filesystem root, and every absolute path is "under"
        it — the whole home guard then accepted the real user home. This pins the
        cloud half; the local half is pinned in ``local/tests``.
        """
        from cloud.backend.config import _home_roots
        values = _fixture_config(tempfile.mkdtemp(prefix="r19-cloud-root-"))
        drive = Path(os.path.abspath(os.sep)).resolve()
        for name, relative in (("state_root", "state"),
                                ("log_dir", "logs"),
                                ("lock_dir", "locks"),
                                ("config_dir", "cfg")):
            values[name] = str(drive / relative)
        roots = _home_roots(values)
        self.assertFalse(
            any(root == drive for root in roots),
            "the filesystem root must never be a home allow root: %r"
            % (roots,))

    def test_the_config_dir_must_be_the_config_parent(self):
        """standards-config-09: the cloud entry must verify the document's dir.

        The local entry calls ``validate_config_dir`` so a document that declares
        one ``config_dir`` but is read from another is refused. The cloud entry
        had NO such check: ``config_dir`` was only a ``_home_roots`` allow-list
        source, so it could be declared as an unrelated directory (e.g. the real
        user home) to pull ``home_dir`` under it past the isolation guard.
        """
        root = Path(tempfile.mkdtemp(prefix="r19-cloud-cfgdir-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        other = Path(tempfile.mkdtemp(prefix="r19-cloud-cfgdir-other-"))
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        values = _fixture_config(root)
        values["config_dir"] = str(other)
        fixture = _write_config(root, values)
        result = subprocess.run(
            [sys.executable, "-I", str(RUN_CLOUD),
             "--config", str(fixture), "--check-config"],
            capture_output=True, text=True, timeout=120,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("config_dir", result.stderr)
        self.assertIn("parent directory of --config", result.stderr)

    def test_a_nul_byte_in_a_path_is_refused(self):
        """standards-config-14: the path guard must inspect the OPENED string.

        ``os.path.isabs`` accepts a NUL byte and Windows ``Path.resolve``
        silently turns it into a space, so the guard compared a DIFFERENT path
        than the one ``open()`` later uses. The cloud host refused to refuse
        it (the old ``except: return`` also accepted a resolution failure).
        """
        for field in ("share_store_file", "log_dir", "state_root"):
            with self.subTest(field=field):
                result = self._run(
                    mutate=lambda v, f=field: v.update(
                        {f: v[f] + chr(0) + "bad"}))
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(field, result.stderr)

    def test_real_state_paths_are_refused(self):
        real_data = str((ROOT / "data").resolve())
        for field in ("share_store_file", "console_state_file",
                      "backend_profile_dir"):
            with self.subTest(field=field):
                result = self._run(
                    mutate=lambda v, f=field, d=real_data: v.update(
                        {f: os.path.join(d, "probe.db")}))
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(field, result.stderr)
        token = str((ROOT / "chat2api" / "token.json").resolve())
        result = self._run(mutate=lambda v: v.update(backend_token_file=token))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("backend_token_file", result.stderr)

    def test_the_two_real_roots_the_local_guard_also_names_are_refused(self):
        """The cloud guard must name the SAME real roots as the local one.

        ``backend_profile_dir`` is handed to the 8110 child as ``--profile`` and
        ``backend_service_log`` is where it writes its log. Both live under the
        in-package real state, but the cloud ``REAL_STATE_ROOTS`` used to omit
        ``profile/`` and ``chat2api/chat2api.log``, so a config pointing at them
        was ACCEPTED.
        """
        for field, root in (
                ("backend_profile_dir", ROOT / "profile"),
                ("backend_service_log", ROOT / "chat2api" / "chat2api.log")):
            with self.subTest(field=field):
                result = self._run(
                    mutate=lambda v, f=field, r=root: v.update(
                        {f: str(r)}))
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(field, result.stderr)

    def test_the_optional_paths_pass_the_real_state_guard(self):
        """The OPTIONAL paths are write/read targets too, not free text."""
        for field, target in (
                ("supervision_watch_dir", ROOT / "data"),
                ("supervision_lock_dir", ROOT / "profile"),
                ("price_table_file", ROOT / "data" / "prices.json")):
            with self.subTest(field=field):
                result = self._run(
                    mutate=lambda v, f=field, t=target: v.update({f: str(t)}))
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(field, result.stderr)

    def test_real_user_home_is_refused(self):
        result = self._run(
            mutate=lambda v: v.update(home_dir=str(Path.home())))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("home_dir", result.stderr)

    def test_start_broker_is_refused_outside_the_fixture_runtime(self):
        # The fixture document names no runtime_mode, so it defaults to
        # production: the broker belongs to systemd, not to this root.
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        values = _fixture_config(self.root)
        self.assertNotIn("runtime_mode", values)
        self.assertEqual(CloudConfig.from_mapping(values).runtime_mode,
                         "production")
        start = subprocess.run(
            [sys.executable, "-I", str(ROOT / "cloud" / "run_cloud.py"),
             "--config", str(self.config_dir / "cloud-config.json"),
             "--check-config", "--start-broker"],
            capture_output=True, text=True, timeout=120,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(start.returncode, 2, start.stdout)
        self.assertIn("--start-broker", start.stderr)

    def test_supervision_roots_default_to_the_configured_state(self):
        values = _fixture_config(self.root)
        config = CloudConfig.from_mapping(values)
        self.assertEqual(config.supervision_watch_dir, config.host_state_dir)
        # standards-config-12: ``console_lock_dir`` is no longer a consumed
        # field (the cloud closure has no reader), so the supervision lock root
        # defaults to the cloud's OWN ``lock_dir`` — never to a legacy field
        # whose only reader lives outside this root.
        self.assertEqual(config.supervision_lock_dir, config.lock_dir)
        explicit = dict(values, supervision_watch_dir=str(self.root / "w"),
                        supervision_lock_dir=str(self.root / "l"))
        config = CloudConfig.from_mapping(explicit)
        self.assertEqual(config.supervision_watch_dir,
                         str(self.root / "w"))
        self.assertEqual(config.supervision_lock_dir,
                         str(self.root / "l"))

    def test_broker_args_are_the_complete_ops_cli(self):
        """The argv must be the REAL ``share/broker_ops.py`` CLI (F3/F4).

        The previous assertion pinned ``--socket``/``--share-store`` and
        ``backend_port``. The real parser (``share/broker_ops.py:2605-2640``)
        defines none of them, so the test kept a broker that cannot start green.
        The flags are asserted against the CONFIG-derived argv directly; the
        discriminating end-to-end check is
        ``test_retired_broker_flags_are_rejected_by_the_real_parser`` below
        (``--help`` cannot distinguish a retired flag, plan F20).
        """
        config = CloudConfig.from_mapping(_fixture_config(self.root))
        args = config.broker_args()
        self.assertEqual(args[0], "share/broker_ops.py")
        # ``--host-command`` is a REMAINDER the caller appends; it must never be
        # baked into the config-derived argv.
        self.assertNotIn("--host-command", args)
        for flag in ("--socket-path", "--key-file", "--instance-key-file",
                     "--registration-file", "--instance-socket-prefix",
                     "--projection-path", "--broker-dir", "--host-pid-file",
                     "--state-dir"):
            with self.subTest(flag=flag):
                self.assertIn(flag, args)
        for flag in ("--socket", "--control-key-file", "--share-store",
                     "--share-management-config", "--share-run-root",
                     "--backend-port", "--pid-file"):
            with self.subTest(retired=flag):
                self.assertNotIn(flag, args)

    def test_retired_broker_flags_are_rejected_by_the_real_parser(self):
        """F20: the discriminating check is ``unrecognized arguments``.

        ``--help`` alone cannot discriminate (a REMAINDER swallows it), so the
        retired spellings are handed to the real parser TOGETHER with every new
        required argument and the parser must refuse them by name.
        """
        config = CloudConfig.from_mapping(_fixture_config(self.root))
        retired = ["--control-key-file", "k", "--share-store", "st",
                   "--share-management-config", "m", "--share-run-root", "r",
                   "--backend-port", "28110", "--pid-file", "p"]
        result = subprocess.run(
            [sys.executable, *config.broker_args(), *retired,
             "--host-command", sys.executable, "-c", "pass"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("unrecognized arguments", result.stderr)
        for flag in ("--control-key-file", "--share-store",
                     "--share-management-config", "--share-run-root",
                     "--backend-port", "--pid-file"):
            with self.subTest(flag=flag):
                self.assertIn(flag, result.stderr)

    def test_fixture_broker_argv_reuses_the_product_contract(self):
        """F8: the chain fixture must not hand-write its own broker argv."""
        sys.path.insert(0, str(ROOT / "tests"))
        import r19.cloud_fixture as cloud_fixture
        fixture = cloud_fixture.CloudChainFixture(ROOT)
        try:
            config = CloudConfig.from_mapping(fixture.config_values())
            self.assertEqual(
                cloud_fixture.broker_argv(fixture.paths),
                config.broker_args())
            # The field names are shared, not just the values: the helper's
            # contract names every CloudConfig attribute the broker uses.
            for _flag, field in cloud_fixture.BROKER_ARG_FIELDS:
                self.assertTrue(hasattr(config, field), field)
        finally:
            shutil.rmtree(fixture.root, ignore_errors=True)

    def test_price_table_file_is_optional(self):
        config = CloudConfig.from_mapping(_fixture_config(self.root))
        self.assertIsNone(config.price_table_file)
        values = dict(_fixture_config(self.root),
                      price_table_file=str(self.root / "prices.json"))
        self.assertEqual(
            CloudConfig.from_mapping(values).price_table_file,
            str(self.root / "prices.json"))


# ------------------------------------------------------------ broker launch
class BrokerLaunchTests(unittest.TestCase):
    """F8b: the PRODUCT broker launch path (``_start_broker``) must be covered.

    ``cloud/backend/app.py::_start_broker`` is the only place the cloud root
    spawns the broker, and no test ever passed ``start_broker=True`` — the sole
    related case asserted the REFUSAL. The argv it builds was therefore never
    compared with the real CLI. These cases capture the ``Popen`` call.

    ``broker_args()`` carries only the broker's OWN flags, so the 8100 gateway
    argv (``--host-command``) is an explicit caller input: the root refuses to
    spawn a broker it cannot describe instead of starting a child that immediately
    exits with ``unrecognized arguments``.
    """

    HOST_COMMAND = ("python3", "chat2api/chat2api.py", "--port", "28100")

    def setUp(self):
        self.saved_env = _save_env(PROCESS_ENV_NAMES)
        self.root = Path(tempfile.mkdtemp(prefix="r19-cloud-broker-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.config = CloudConfig.from_mapping(
            dict(_fixture_config(self.root), runtime_mode="fixture"))

    def tearDown(self):
        _restore_env(self.saved_env)

    def _popen_spy(self):
        spawned = []

        class _FakeProcess:
            pid = 4242

            def poll(self):
                return None

        def fake_popen(argv, **kwargs):
            spawned.append((argv, kwargs))
            return _FakeProcess()

        return spawned, fake_popen

    def test_start_broker_spawns_the_real_ops_cli(self):
        spawned, fake_popen = self._popen_spy()
        with mock.patch.object(app_module.subprocess, "Popen", fake_popen):
            process = app_module._start_broker(
                self.config, list(self.HOST_COMMAND))
        self.assertIsNotNone(process)
        self.assertEqual(len(spawned), 1)
        argv, kwargs = spawned[0]
        self.assertEqual(argv[0], sys.executable)
        expected = [*self.config.broker_args(), "--host-command",
                    *self.HOST_COMMAND]
        self.assertEqual(argv[1:], expected)
        self.assertIn("--socket-path", argv)
        self.assertIn(self.config.broker_socket, argv)
        self.assertIn("--key-file", argv)
        self.assertIn("--state-dir", argv)
        self.assertNotIn("--control-key-file", argv)
        self.assertNotIn("--share-store", argv)
        self.assertNotIn("--backend-port", argv)
        self.assertEqual(kwargs.get("cwd"),
                         str(Path(app_module.__file__).resolve().parents[2]))

    def test_start_broker_without_a_host_command_fails_closed(self):
        spawned, fake_popen = self._popen_spy()
        with mock.patch.object(app_module.subprocess, "Popen", fake_popen):
            with self.assertRaises(BrokerError) as caught:
                app_module._start_broker(self.config, None)
        self.assertIn("--host-command", str(caught.exception))
        self.assertEqual(spawned, [], "no child may be spawned without a host")

    def test_build_app_starts_the_broker_only_when_asked(self):
        spawned, fake_popen = self._popen_spy()
        with mock.patch.object(app_module.subprocess, "Popen", fake_popen), \
                mock.patch.object(app_module, "KeyApi", _FakeKeyApi), \
                mock.patch.object(app_module, "BackendProcess",
                                  _FakeBackendProcess):
            app = app_module.build_app(self.config)
            self.assertEqual(spawned, [])
            self.assertIsNone(app.broker_process)
            app = app_module.build_app(
                self.config, start_broker=True,
                host_command=list(self.HOST_COMMAND))
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(app.broker_process)
        self.assertIn("--host-command", spawned[0][0])

    def test_cli_start_broker_without_host_command_exits_2(self):
        """``run_cloud.py --start-broker`` must name the missing input."""
        root = Path(tempfile.mkdtemp(prefix="r19-cloud-broker-cli-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fixture = _write_config(
            root, dict(_fixture_config(root), runtime_mode="fixture"))
        result = subprocess.run(
            [sys.executable, "-I", str(RUN_CLOUD),
             "--config", str(fixture), "--check-config", "--start-broker"],
            capture_output=True, text=True, timeout=120,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("--host-command", result.stderr)


class _FakeKeyApi:
    def __init__(self, config):
        self.config = config


class _FakeBackendProcess:
    def __init__(self, config):
        self.config = config


# ------------------------------------------------------------------ keys api
class KeyApiTests(unittest.TestCase):
    def setUp(self):
        self.saved_env = _save_env(PROCESS_ENV_NAMES)

    def tearDown(self):
        _restore_env(self.saved_env)

    def test_constructor_delegates_after_the_root_applies_the_env(self):
        """E19-1-06 step 10: the ROOT applies the env, not ``KeyApi``.

        The old contract had ``KeyApi.__init__`` mutate ``os.environ``. That let a
        root which forgot ``apply_process_env`` look correct, and it applied the
        mapping twice. Now the composition root owns the one call; the constructor
        only builds the broker/manager from the explicit config.
        """
        root = Path(tempfile.mkdtemp(prefix="r19-cloud-keys-"))
        config = CloudConfig.from_mapping(_fixture_config(root))
        manager = _FakeManager()
        broker = _FakeBroker()
        api = KeyApi(config, manager=manager, broker=broker)
        expected = {
            "SHARE_STORE_FILE": config.share_store_file,
            "CHAT2API_SHARE_SECRET_KEY_FILE": config.share_secret_key_file,
            "CONSOLE_SHARE_RUN_ROOT": config.share_run_root,
            "CONSOLE_SHARE_ATTRIBUTION_KEY_FILE":
                config.attribution_key_file,
            "CONSOLE_SHARE_BACKEND_DIR": config.backend_dir,
            "PORT": str(config.port),
            "BASE_URL": config.backend_base_url,
            "CONSOLE_SHARE_BACKEND_PORT": str(config.backend_port),
            "CONSOLE_SHARE_BACKEND_BASE_URL": config.backend_base_url,
        }
        # Constructing the Key API must NOT touch the process environment.
        for name in expected:
            self.assertIsNone(os.environ.get(name),
                              "KeyApi applied %s itself" % name)
        # The root's single call applies exactly the same mapping.
        app_module.apply_process_env(config)
        for name, value in expected.items():
            self.assertEqual(os.environ.get(name), value, name)
        # standards-config-12: the three legacy console paths have NO consumer
        # in the cloud closure, so the root must NOT apply them to the process
        # environment (a set value would make the operator believe the cloud
        # took over the console state DB/lock dir/share-management config).
        for name in ("SHARE_MANAGEMENT_CONFIG", "CONSOLE_STATE_FILE",
                      "CONSOLE_LOCK_DIR"):
            self.assertIsNone(os.environ.get(name),
                              "the root applied the unconsumed %s" % name)

        self.assertEqual(api.status()["in_sync"], True)
        created = api.create("fixture-key", max_concurrency=8)
        self.assertEqual(created["key_id"], "key-1")
        self.assertEqual(api.get("key-1")["key_id"], "key-1")
        self.assertEqual(api.list()["keys"][0]["key_id"], "key-1")
        self.assertEqual(api.revoke("key-1", 3)["enabled"], False)
        self.assertEqual(api.rotate("key-1", 4)["key_id"], "key-1")
        drained = api.drain()
        self.assertEqual(drained,
                         {"ok": True,
                          "result": {"drained": True, "timed_out": False,
                                     "stopped": True,
                                     "notification_failed": False,
                                     "in_flight": 0},
                          "error_code": None})
        self.assertEqual(broker.client.drain_calls, 1)
        self.assertEqual(
            [call[0] for call in manager.calls],
            ["status", "create", "get", "list", "revoke", "rotate"])


# ---------------------------------------------------------- backend process
class BackendProcessTests(unittest.TestCase):
    def test_status_uses_read_only_evidence(self):
        root = Path(tempfile.mkdtemp(prefix="r19-cloud-backend-"))
        config = CloudConfig.from_mapping(_fixture_config(root))
        ops = _FakeOps(running=True)
        backend = BackendProcess(config, ops=ops)
        status = backend.status()
        self.assertEqual(status["running"], True)
        self.assertEqual(status["port"], config.backend_port)
        self.assertEqual(status["pid_state"]["state"], "running")
        self.assertEqual(ops.verify_calls, [False])

    def test_start_and_stop_delegate_to_backend_ops(self):
        root = Path(tempfile.mkdtemp(prefix="r19-cloud-backend-"))
        config = CloudConfig.from_mapping(_fixture_config(root))
        ops = _FakeOps(running=False)
        backend = BackendProcess(config, ops=ops)
        self.assertEqual(backend.start(), True)
        self.assertEqual(ops.start_calls, 1)
        self.assertEqual(backend.stop(), True)
        self.assertEqual(ops.stop_calls, 1)

    def test_the_child_env_carries_the_identity_paths(self):
        """standards-config-05: the 8110 child must get the SAME paths.

        ``revoke_state_file`` and ``price_table_file`` are CloudConfig fields,
        but they never reached any process: ``run_cloud`` clears every
        ``CHAT2API_*`` variable and neither ``process_env()`` nor
        ``_child_env()`` wrote them back, so 8110 fell back to the deployment
        defaults while the console used the fixture paths.
        """
        root = Path(tempfile.mkdtemp(prefix="r19-cloud-backend-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        config = CloudConfig.from_mapping(_fixture_config(root))
        backend_config = BackendProcess._backend_config(config)
        self.assertEqual(backend_config.revoke_state_file,
                         config.revoke_state_file)
        # The price identity has an EXPLICIT fallback into the configured state
        # root, never the in-package data/prices.json default.
        expected_price = (config.price_table_file
                          or os.path.join(config.host_state_dir,
                                           "prices.json"))
        self.assertEqual(backend_config.price_table_file, expected_price)
        self.assertNotIn(str(ROOT / "data"), expected_price)
        # And the child environment really carries both keys.
        ops = production_backend_ops(config=backend_config)
        env = ops._child_env(ops._paths())
        self.assertEqual(env["CHAT2API_SHARE_REVOKE_STATE_FILE"],
                         config.revoke_state_file)
        self.assertEqual(env["CHAT2API_PRICE_TABLE_FILE"], expected_price)

    def test_backend_config_enforces_the_cloud_isolation_facts(self):
        """standards-config-10: the public BackendConfig must fail closed.

        ``BackendConfig.__post_init__`` only checked non-empty/absolute/port
        range/http(s), so a direct caller could name the production 8110 port, a
        non-loopback upstream, or a gateway dir outside the package — the three
        constraints the cloud root itself already enforces.
        """
        from cloud.backend.config import FORBIDDEN_PORTS
        root = Path(tempfile.mkdtemp(prefix="r19-cloud-backend-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        values = _fixture_config(root)
        config = CloudConfig.from_mapping(values)
        base = BackendProcess._backend_config(config)
        cases = (
            ("backend_port", 8110),
            ("backend_port", 8000),
            ("backend_base_url", "https://chat.genai.um.edu.mo"),
            ("backend_gateway_dir", str(root)),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                fields = {name: getattr(base, name)
                          for name in base.__dataclass_fields__}
                fields[field] = value
                with self.assertRaises(ValueError):
                    BackendConfig(**fields)
        self.assertIn(8110, FORBIDDEN_PORTS)

    def test_unconfirmed_start_raises(self):
        root = Path(tempfile.mkdtemp(prefix="r19-cloud-backend-"))
        config = CloudConfig.from_mapping(_fixture_config(root))

        class _NeverStarts(_FakeOps):
            def start(self):
                self.start_calls += 1
                return False

        ops = _NeverStarts(running=False)
        backend = BackendProcess(config, ops=ops)
        with self.assertRaises(BackendProcessError):
            backend.start()

    def test_backend_interpreter_mismatch_is_refused(self):
        root = Path(tempfile.mkdtemp(prefix="r19-cloud-backend-"))
        config = CloudConfig.from_mapping(
            _fixture_config(root, backend_python=str(root / "other-python")))
        ops = _FakeOps(running=False)
        backend = BackendProcess(config, ops=ops)
        with self.assertRaises(BackendProcessError):
            backend.start()
        self.assertEqual(ops.start_calls, 0)

    def test_default_ops_status_is_read_only(self):
        root = Path(tempfile.mkdtemp(prefix="r19-cloud-backend-"))
        config = CloudConfig.from_mapping(_fixture_config(root))
        backend = BackendProcess(config)
        status = backend.status()
        self.assertEqual(status["running"], False)
        self.assertEqual(status["pid_state"]["state"], "pid_file_absent")
        self.assertEqual(status["port"], config.backend_port)

    def test_backend_profile_dir_is_honoured_by_the_backend_ops(self):
        """``backend_profile_dir`` must not be a dead CloudConfig field.

        The 8110 child's ``--profile`` comes from ``BackendConfig.profile``;
        a deployment that places the profile outside ``backend_dir`` must not be
        silently redirected to ``backend_dir/edge-profile``.
        """
        root = Path(tempfile.mkdtemp(prefix="r19-cloud-profile-"))
        config = CloudConfig.from_mapping(_fixture_config(root))
        explicit = str(root / "explicit-profile")
        config = replace(config, backend_profile_dir=explicit)
        backend_config = BackendProcess._backend_config(config)
        self.assertEqual(backend_config.profile, explicit)
        ops = production_backend_ops(config=backend_config)
        paths = ops._paths()
        self.assertEqual(paths["profile"], explicit)
        self.assertNotEqual(
            paths["profile"],
            os.path.join(config.backend_dir, "edge-profile"))
        cmdline = ops._child_cmdline(paths)
        self.assertEqual(cmdline[cmdline.index("--profile") + 1], explicit)


# --------------------------------------------------------------------- app
class CloudAppTests(unittest.TestCase):
    def setUp(self):
        self.saved_env = _save_env(PROCESS_ENV_NAMES)
        self.root = Path(tempfile.mkdtemp(prefix="r19-cloud-app-"))
        self.port = _free_port()
        self.token = "fixture-control-token"
        values = _fixture_config(self.root, port=self.port)
        token_file = Path(values["control_token_file"])
        _write_private(token_file, self.token)
        self.config = CloudConfig.from_mapping(values)
        self.keys = _FakeManager()
        self.broker = _FakeBroker()
        self.backend = _FakeBackend()
        self.api = KeyApi(self.config, manager=self.keys, broker=self.broker)
        self.app = CloudApp(self.config, keys=self.api, backend=self.backend)

    def tearDown(self):
        self.app.close()
        _restore_env(self.saved_env)

    def test_routes_require_the_control_token_and_loopback(self):
        self.app.start()
        status, payload = _request(self.port, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["role"], "cloud")
        status, _ = _request(self.port, "GET", "/api/share/status")
        self.assertEqual(status, 401)
        status, _ = _request(self.port, "GET", "/api/share/status",
                              token="wrong")
        self.assertEqual(status, 401)
        status, payload = _request(self.port, "GET", "/api/share/status",
                                   token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["in_sync"], True)
        status, _ = _request(self.port, "GET", "/health",
                              host="evil.example")
        self.assertEqual(status, 403)

    def test_key_lifecycle_and_drain_routes(self):
        self.app.start()
        status, payload = _request(
            self.port, "POST", "/api/share/keys",
            body={"name": "fixture-key", "max_concurrency": 8},
            token=self.token)
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["key_id"], "key-1")
        self.assertEqual(payload["published"], True)
        status, payload = _request(
            self.port, "GET", "/api/share/keys/key-1", token=self.token)
        self.assertEqual(status, 200)
        status, payload = _request(
            self.port, "POST",
            "/api/share/keys/key-1/revoke-and-terminate",
            body={"expected_row_version": 3}, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["enabled"], False)
        status, payload = _request(
            self.port, "POST", "/api/share/keys/key-1/rotate",
            body={"expected_row_version": 4}, token=self.token)
        self.assertEqual(status, 200)
        status, payload = _request(
            self.port, "POST", "/api/share/drain", body={}, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["result"]["stopped"], True)

    def test_drain_reports_an_unconfirmed_stop_as_a_failure(self):
        """A structured broker reply is NOT a confirmed drain (D18-10).

        ``BrokerResult.ok`` only means the broker answered; the drain outcome is
        ``result.drained``/``result.timed_out``/``result.in_flight``. The old
        route returned HTTP 200 with top-level ``ok: true`` for
        ``{"drained": false, "timed_out": true, "in_flight": null}`` — the exact
        shape "未排空不得谎报已停止" forbids.
        """
        self.broker.client.drain = lambda: _BrokerResult(
            True,
            {"drained": False, "timed_out": True, "stopped": True,
             "notification_failed": True, "in_flight": None},
            "ok")
        self.app.start()
        status, payload = _request(
            self.port, "POST", "/api/share/drain", body={}, token=self.token)
        self.assertEqual(status, 503, payload)
        self.assertNotEqual(payload.get("ok"), True)
        self.assertEqual(payload["error"], "drain_not_confirmed")

    def test_drain_reports_a_nonzero_in_flight_count_as_a_failure(self):
        self.broker.client.drain = lambda: _BrokerResult(
            True,
            {"drained": True, "timed_out": False, "stopped": True,
             "notification_failed": False, "in_flight": 3},
            "ok")
        self.app.start()
        status, payload = _request(
            self.port, "POST", "/api/share/drain", body={}, token=self.token)
        self.assertEqual(status, 503, payload)
        self.assertEqual(payload["error"], "drain_not_confirmed")

    def test_a_stop_refuses_new_work_before_the_backend_is_torn_down(self):
        """D18-10: a stop refuses new work, then drains with a bound.

        The old ``_finish`` stopped the 8110 backend first and closed the
        listener last, so a ``/api/share/backend/start`` that arrived during the
        stop window was still accepted and spawned a second 8110.
        """
        self.app.start()
        self.app.shutdown()
        self.assertTrue(self.app.is_stopping())
        with self.assertRaises(OSError):
            _request(self.port, "POST", "/api/share/backend/start",
                     body={}, token=self.token)
        self.assertGreaterEqual(self.backend.stopped, 1,
                                "the backend must be stopped after the drain")

    def test_a_failed_start_never_stops_a_backend_it_did_not_start(self):
        """Ownership: a refused start must not stop someone else's backend."""
        root = Path(tempfile.mkdtemp(prefix="r19-cloud-refused-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        values = _fixture_config(root)
        values["control_token_file"] = str(root / "state" / "missing-token")
        config = CloudConfig.from_mapping(values)
        backend = _FakeBackend()
        app = CloudApp(config, keys=_FakeManager(), backend=backend)
        with self.assertRaises(RuntimeError):
            app.start()
        app.close()
        self.assertEqual(backend.started, 0)
        self.assertEqual(backend.stopped, 0)

    def test_the_cloud_root_fails_closed_when_shared_is_missing(self):
        """spec-shared-05: the cloud host must be fail-closed, like the local one.

        The local host imports ``shared`` at module import time, so a missing
        ``shared`` fails the start. The cloud host deferred the
        ``cloud.backend.summary`` import into the request, so a missing
        ``shared`` was swallowed into a 500 for one read face while the
        management host still started and served the Key/status write faces.
        """
        root_dir = Path(tempfile.mkdtemp(prefix="r19-cloud-shared-"))
        self.addCleanup(shutil.rmtree, root_dir, ignore_errors=True)
        # The child runs WITHOUT the package's ``shared`` on sys.path: the
        # parent builds a copy of the cloud closure and hides ``shared``.
        sys.path.insert(0, str(ROOT / "tests"))
        import r19.package_cloud as package_cloud
        isolated = root_dir / "isolated"
        isolated.mkdir()
        built = package_cloud.build_cloud_package(destination=isolated)
        (Path(built) / "shared").rename(Path(built) / "shared-hidden")
        probe = root_dir / "probe.py"
        probe.write_text(
            "\n".join([
                "import json, sys",
                # The child only ever adds the BUILT package root; adding the
                # checkout root would resolve the real ``shared`` again.
                "sys.path.insert(0, %r)" % str(built),
                "try:",
                "    import cloud.backend.app",
                "except ModuleNotFoundError as exc:",
                "    print(json.dumps({'missing': exc.name}))",
                "else:",
                "    print(json.dumps({'missing': None}))",
            ]),
            encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-I", str(probe)], cwd=str(built),
            capture_output=True, text=True, timeout=120,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["missing"], "shared",
                         "the cloud root must import shared at import time")

    def test_a_stop_drains_the_in_flight_request_with_a_bound(self):
        """D18-10 / standards-lifecycle-04: the drain is BOUNDED.

        The handler threads were daemon and ``server_close`` did not join them,
        so the stop returned immediately and cut a request in flight. The stop
        now waits (bounded) for the handlers before stopping the backend.
        """
        self.app.start()
        release = threading.Event()
        original = self.app.backend.stop

        def slow_stop():
            release.wait(timeout=30)
            return original()

        self.app.backend.stop = slow_stop
        worker = threading.Thread(target=self.app.shutdown)
        worker.daemon = True
        worker.start()
        deadline = time.monotonic() + 5.0
        while worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(worker.is_alive(),
                        "the stop must wait for the in-flight handler drain")
        release.set()
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive())

    def test_a_closed_app_cannot_be_restarted_into_a_half_dead_state(self):
        """standards-lifecycle-10: close is final for THIS instance.

        ``_claim_close`` used to set ``_closed`` and never reset it, while
        ``start`` ignored it. A caller that closed (or whose ``main`` finally
        ran before a retry) could start a listener that ``shutdown``/``close``
        then refused to close — the process could never be stopped again. The
        entry now REFUSES a start after close instead of leaking a listener.
        """
        self.app.close()
        with self.assertRaises(RuntimeError):
            self.app.start()
        self.assertIsNone(self.app._server)
        self.assertFalse(Path(self.config.pid_file).exists())

    def test_a_failed_start_is_rolled_back_before_the_retry(self):
        """standards-lifecycle-03: a failed start must roll back its listener.

        ``_start_chain`` created the server and then started the thread with no
        exception rollback. A ``Thread.start`` failure left ``self._server``
        non-None with a listener that ``serve_forever`` never entered, so the
        listener stayed open and ``close()`` blocked on ``server.shutdown()``
        forever. The chain now rolls back on ANY exception.
        """
        attempts = []
        original = app_module._CloudHTTPServer

        def failing_once(address, handler):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("simulated bind failure")
            return original(address, handler)

        with mock.patch.object(app_module, "_CloudHTTPServer", failing_once):
            with self.assertRaises(RuntimeError):
                self.app.start()
            self.assertIsNone(self.app._server,
                              "a failed start must not keep a listener")
            self.assertEqual(self.backend.stopped, 1,
                             "the backend started by the failed attempt must "
                             "be stopped")
            self.app.start()
        self.assertIsNotNone(self.app._server)

    def test_a_thread_start_failure_rolls_back_the_listener_and_the_backend(self):
        """standards-lifecycle-03: a Thread.start() failure must roll back.

        The bind-failure case above covers a raising ``_CloudHTTPServer``, but
        ``threading.Thread.start()`` can also fail (resource exhaustion) — and it
        runs AFTER the socket is bound and AFTER the backend started, while
        ``self._server`` is already assigned. Without a rollback the listener
        stayed open, the 8110 backend kept running, and ``close()`` blocked
        forever in ``server.shutdown()``.
        """
        def failing_start(thread):
            raise RuntimeError("no thread")

        with mock.patch.object(threading.Thread, "start", failing_start):
            with self.assertRaises(RuntimeError):
                self.app.start()
        self.assertIsNone(self.app._server,
                          "a failed start must not keep the listener")
        self.assertEqual(self.backend.stopped, 1,
                         "the backend started by the failed attempt must stop")
        started_at = time.monotonic()
        self.app.close()
        self.assertLess(time.monotonic() - started_at, 10.0,
                        "close() must not hang after a failed start")

    def test_the_listener_loop_crash_reaches_the_entry_exit_code(self):
        """A crashed listener loop must not be reported as a clean exit."""
        def crashing(server):
            raise RuntimeError("listener loop crashed")

        with mock.patch.object(app_module._CloudHTTPServer, "serve_forever",
                               crashing):
            self.app.start()
            with self.assertRaises(RuntimeError):
                self.app.serve_forever()
        self.assertIn("listener loop crashed", str(self.app._failure))

    def test_a_failed_stop_is_reported_and_not_swallowed(self):
        """standards-lifecycle-05/16: the entry must report a failed stop.

        ``_stop_backend`` used to print the failure and return, so ``main``
        returned 0 and DELETED the pid file while the 8110 backend might still
        be running. The close path now raises the failure (after releasing the
        resources) and ``main`` keeps it as the exit code.
        """
        self.app.start()

        def failing_stop():
            raise app_module.BackendProcessError("backend stop was not confirmed")

        self.backend.stop = failing_stop
        with self.assertRaises(app_module.BackendProcessError):
            self.app.close()
        self.assertFalse(
            Path(self.config.pid_file).exists(),
            "the close path must still release the pid file before reporting")

    def test_shutdown_stops_backend_and_listener(self):
        self.app.start()
        status, _ = _request(self.port, "POST", "/admin/shutdown",
                              body={}, token=self.token)
        self.assertEqual(status, 200)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port),
                                             timeout=0.2):
                    time.sleep(0.05)
            except OSError:
                break
        else:
            self.fail("cloud app listener stayed open after shutdown")
        self.assertGreaterEqual(self.backend.stopped, 1)

    def test_composition_root_avoids_local_and_legacy_hosts(self):
        """E19-1-06 step 13: the proof runs in a SUBPROCESS.

        The old version asserted on this process's ``sys.modules``. That made the
        result depend on test ORDER: another r19 test imports ``local.backend``
        first, so the assertion failed only in the full-suite run and passed when
        this file ran alone. A clean subprocess that imports ONLY
        ``cloud.backend.app`` answers the real question: does the cloud
        composition root pull in ``local`` or the legacy hosts?
        """
        source = (ROOT / "cloud" / "backend" / "app.py").read_text(
            encoding="utf-8")
        for name in ("import usagesrv", "import console_adapters",
                     "import model_sync", "import local"):
            self.assertNotIn(name, source)
        probe = Path(tempfile.mkdtemp(prefix="r19-cloud-modules-")) / "probe.py"
        probe.write_text(
            "import json, sys\n"
            "sys.path.insert(0, %r)\n"
            "import cloud.backend.app\n"
            "print(json.dumps({\n"
            "    'forbidden': [name for name in (\n"
            "        'usagesrv', 'console.adapters', 'model_sync', 'local')\n"
            "        if name in sys.modules],\n"
            "    'cloud': cloud.backend.app.__file__}))\n"
            % str(ROOT),
            encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, "-I", str(probe)],
                cwd=str(probe.parent),
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                capture_output=True, text=True, timeout=120)
        finally:
            shutil.rmtree(probe.parent, ignore_errors=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["forbidden"], [])
        self.assertTrue(
            Path(payload["cloud"]).resolve().is_relative_to(ROOT.resolve()),
            payload["cloud"])

    def test_start_refuses_a_missing_control_token(self):
        root = Path(tempfile.mkdtemp(prefix="r19-cloud-app-"))
        config = CloudConfig.from_mapping(_fixture_config(root))
        backend = _FakeBackend()
        app = CloudApp(config, keys=_FakeManager(), backend=backend)
        try:
            with self.assertRaises(RuntimeError):
                app.start()
        finally:
            app.close()
        self.assertEqual(backend.started, 0)

    def test_serve_forever_joins_the_single_listener_thread(self):
        """``main`` calls start() then serve_forever(): ONE serve loop only.

        ``start`` already runs the listener in its own thread. A second
        ``server.serve_forever()`` on the same socket makes two threads accept
        the same listener and races shutdown; the second call must only join the
        first thread.
        """
        calls = []
        original = app_module._CloudHTTPServer.serve_forever

        def counting(server):
            calls.append(1)
            return original(server)

        with mock.patch.object(app_module._CloudHTTPServer, "serve_forever",
                               counting):
            self.app.start()
            waiter = threading.Thread(target=self.app.serve_forever)
            waiter.start()
            deadline = time.monotonic() + 5.0
            while len(calls) < 1 and time.monotonic() < deadline:
                time.sleep(0.02)
            # A wrongly-implemented second loop enters the same socket here.
            time.sleep(0.2)
            self.app.shutdown()
            waiter.join(timeout=10)
        self.assertFalse(waiter.is_alive(),
                         "serve_forever did not return after shutdown")
        self.assertEqual(
            len(calls), 1,
            "serve_forever must join the thread started by start(), "
            "not own a second listener loop")

    def test_main_runs_the_real_start_then_serve_forever_path(self):
        """``main`` (the real entry) must return cleanly after shutdown."""
        calls = []
        original = app_module._CloudHTTPServer.serve_forever

        def counting(server):
            calls.append(1)
            return original(server)

        class _PosixOS:
            name = "posix"

            def __getattr__(self, attribute):
                return getattr(os, attribute)

        timer = threading.Timer(0.5, self.app.shutdown)
        timer.daemon = True
        with mock.patch.object(app_module, "os", _PosixOS()), \
                mock.patch.object(app_module, "build_app",
                                  lambda config, **kwargs: self.app), \
                mock.patch.object(app_module._CloudHTTPServer,
                                  "serve_forever", counting):
            timer.start()
            try:
                code = app_module.main(self.config)
            finally:
                timer.cancel()
                timer.join(timeout=5)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(self.backend.stopped, 1)


# ------------------------------------------------------- package validator
class CloudPackageValidatorTests(unittest.TestCase):
    """E19-1-C1 item 7: the cloud package validator must refuse local code.

    ``FORBIDDEN_NAMES`` already lists ``usagesrv.py``/``console/adapters.py``.
    The gap is that nothing proved the VALIDATOR actually fires: a package that
    carried the local application would have to be caught, otherwise "the cloud
    package contains no local code" is an unverified claim.
    """

    def setUp(self):
        sys.path.insert(0, str(ROOT / "tests"))
        import r19.package_cloud as package_cloud
        self.package_cloud = package_cloud
        self.root = Path(tempfile.mkdtemp(prefix="r19-cloud-pkg-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _plant(self, relative, text="# planted\n"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_clean_cloud_package_reports_nothing(self):
        self._plant("cloud/run_cloud.py")
        self._plant("shared/pricing_rules.py")
        self._plant("share/broker_ops.py")
        self.assertEqual(
            self.package_cloud.forbidden_entries(self.root), [])

    def test_planted_local_entry_is_reported(self):
        self._plant("cloud/run_cloud.py")
        planted = self._plant("local/run_local.py")
        found = self.package_cloud.forbidden_entries(self.root)
        self.assertTrue(found, "a planted local/run_local.py was not reported")
        self.assertIn("local/run_local.py", found)
        self.assertTrue(planted.is_file())

    def test_planted_legacy_hosts_are_reported(self):
        for name in ("usagesrv.py", "console/adapters.py", "model_sync/sync.py",
                     "zcode/hot_sync.py"):
            with self.subTest(name=name):
                root = Path(tempfile.mkdtemp(prefix="r19-cloud-pkg-"))
                self.addCleanup(shutil.rmtree, root, ignore_errors=True)
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("# planted\n", encoding="utf-8")
                self.assertIn(
                    name, self.package_cloud.forbidden_entries(root))


# ----------------------------------------------------------------- summary
class CloudSummaryTests(unittest.TestCase):
    def test_summary_survives_without_the_legacy_pricing_aliases(self):
        """E19-1-C1 item 8: the summary must use the PUBLIC pricing surface.

        ``shared.pricing_rules`` keeps ten underscored aliases for consumers that
        have not migrated. A new cross-package consumer must not depend on them, so
        the test removes every legacy alias from the loaded module and then builds a
        summary. If the summary still touched a private spelling it would raise
        ``AttributeError`` here.
        """
        from shared import pricing_rules

        table = {
            "exchangeRate": 7.2,
            "peak": {"enabled": True, "effectiveAtMs": 0,
                     "windows": [{"start": 0, "end": 24}]},
            "versions": [{
                "versionId": "v1", "effectiveAt": "2026-01-01T00:00:00+08:00",
                "effectiveAtMs": 0, "exchangeRate": 7.2,
                "checkedAt": "2026-01-01T00:00:00+08:00",
                "checkedAgeDays": 0,
            }],
            "versioned": True,
            "versionMeta": {"versioned": True, "count": 1,
                           "invalidCount": 0, "current": None},
            "meta": {"pricesFound": True, "checkedAt": None,
                     "checkedAgeDays": None, "checkedStale": None},
            "tiers": {},
        }
        legacy = [name for name in dir(pricing_rules)
                  if name.startswith("_") and not name.startswith("__")]
        self.assertTrue(legacy, "the legacy aliases are expected to exist")
        saved = {name: getattr(pricing_rules, name) for name in legacy}
        try:
            for name in legacy:
                delattr(pricing_rules, name)
            summary = build_cloud_summary(table)
        finally:
            for name, value in saved.items():
                setattr(pricing_rules, name, value)
        self.assertEqual(summary["role"], "cloud")
        self.assertEqual(summary["exchangeRate"], 7.2)

    def test_load_price_table_normalizes_the_raw_document(self):
        """R19-2: both ends must reach the same pure-rule result.

        ``local/backend/stats_env.normalize_price_table`` parses a raw price
        document into the versioned shape (``versioned``, ``versions[].effectiveAtMs``,
        ``versionMeta``, ``meta``, ``override``). The cloud host used to hand the
        raw document straight to ``build_cloud_summary``, so on the real
        ``data/prices.json`` it reported ``versioned: false`` and selected the
        1970 segment as "current". The two consumers must agree on the SAME
        document.
        """
        raw = {
            "exchangeRate": 7.2,
            "checkedAt": "2026-10-12",
            "models": {"legacy-model": {"input": 1.0, "output": 2.0}},
            "versions": [
                {"versionId": "v1",
                 "effectiveAt": "2026-08-01T00:00:00+08:00",
                 "exchangeRate": 7.2,
                 "checkedAt": "2026-09-01",
                 "models": {"legacy-model": {"input": 1.0, "output": 2.0}}},
                {"versionId": "v2",
                 "effectiveAt": "2026-09-29T17:00:28+08:00",
                 "exchangeRate": 8.0,
                 "checkedAt": "2026-10-12",
                 "models": {FIXTURE_CLOUD_MODEL:
                            {"input": 3.0, "output": 4.0}}},
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "prices.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            table = app_module.load_price_table(path)
        self.assertTrue(table["versioned"], table)
        self.assertEqual(table["versionMeta"]["count"], 2)
        self.assertEqual(table["versionMeta"]["invalidCount"], 0)
        self.assertEqual(table["versionMeta"]["current"]["versionId"], "v2")
        self.assertEqual(table["meta"]["pricesFound"], True)
        self.assertIn("override", table)
        # And the cloud summary must publish the normalized facts, not the raw
        # absence of them.
        summary = build_cloud_summary(table)
        self.assertEqual(summary["priceVersions"]["versioned"], True)
        self.assertEqual(
            summary["priceVersions"]["current"]["versionId"], "v2")
        self.assertEqual(summary["priceVersions"]["invalidCount"], 0)
        self.assertEqual(summary["sources"]["priceMeta"]["pricesFound"], True)
        """A non-numeric/bool/non-positive rate falls back to the public default."""
        from shared.pricing_rules import DEFAULT_EXCHANGE_RATE

        for raw in (None, "7.2", True, 0, -1):
            with self.subTest(raw=raw):
                summary = build_cloud_summary(
                    {"exchangeRate": raw, "versions": [], "versioned": False})
                self.assertEqual(summary["exchangeRate"], DEFAULT_EXCHANGE_RATE)

    def test_summary_is_cloud_only_and_uses_the_injected_table(self):
        table = {
            "exchangeRate": 7.2,
            "peak": {"enabled": True, "effectiveAtMs": 0,
                     "windows": [{"start": 0, "end": 24}]},
            "versions": [{
                "versionId": "v1", "effectiveAt": "2026-01-01T00:00:00+08:00",
                "effectiveAtMs": 0, "exchangeRate": 7.2,
                "checkedAt": "2026-01-01T00:00:00+08:00",
                "checkedAgeDays": 0,
            }],
            "versioned": True,
            "versionMeta": {"versioned": True, "count": 1,
                           "invalidCount": 0, "current": None},
            "meta": {"pricesFound": True, "checkedAt": None,
                     "checkedAgeDays": None, "checkedStale": None},
            "tiers": {},
        }
        summary = build_cloud_summary(table)
        self.assertEqual(summary["role"], "cloud")
        self.assertEqual(summary["exchangeRate"], 7.2)
        self.assertEqual(summary["services"], {})
        self.assertEqual(summary["assembly"]["services"], ["share"])
        self.assertNotIn("qoder", summary["assembly"]["services"])


# ------------------------------------------------------------ sys.path use
class RunCloudPathTests(unittest.TestCase):
    """``run_cloud`` is only importable in an ISOLATED interpreter (F1/F2).

    The entry raises ``SystemExit(2)`` unless ``sys.flags.isolated`` is set, so a
    module-level ``from cloud import run_cloud`` made this whole FILE
    un-importable under the official gate (``python -m unittest discover ...``,
    no ``-I``): ``Ran 1 test / FAILED (errors=1)`` with ``SystemExit: 2``. The
    import is gone; every case that needs ``run_cloud`` runs a probe under
    ``python -I`` instead. The guard itself is NOT removed: a planted
    ``json.py``/``argparse.py`` on ``sys.path`` must still be refused.
    """

    def _run_probe(self, body, *, env_extra=None, timeout=180):
        """Run ``body`` in an isolated child and return its JSON payload.

        The child prints a MARKED line: ``run_cloud.main --check-config`` writes
        its own JSON to stdout, so parsing the whole stream as one document would
        see "Extra data". Only the marked line is read, and the marker is injected
        from ``PROBE_MARKER`` so the two sides cannot drift.
        """
        probe_dir = Path(tempfile.mkdtemp(prefix="r19-cloud-path-"))
        self.addCleanup(shutil.rmtree, probe_dir, ignore_errors=True)
        probe = probe_dir / "probe.py"
        probe.write_text(
            "import importlib.util, json, os, sys\n"
            "from pathlib import Path\n"
            "assert sys.flags.isolated, 'the probe must run with -I'\n"
            "PROBE_MARKER = %r\n"
            "spec = importlib.util.spec_from_file_location(\n"
            "    'r19_run_cloud_probe', %r)\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "%s\n" % (PROBE_MARKER, str(RUN_CLOUD), body),
            encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.update(env_extra or {})
        result = subprocess.run(
            [sys.executable, "-I", str(probe)], cwd=str(probe_dir),
            env=env, capture_output=True, text=True, timeout=timeout)
        return result

    def _payload_of(self, result):
        for line in result.stdout.splitlines():
            if line.startswith(PROBE_MARKER):
                return json.loads(line[len(PROBE_MARKER):])
        self.fail("probe printed no marked payload\nstdout=%s\nstderr=%s"
                  % (result.stdout, result.stderr))

    def test_source_checkout_paths_are_removed(self):
        body = (
            "sys.path.insert(0, %r)\n"
            "sys.path.insert(0, %r)\n"
            "module._clean_sys_path()\n"
            "print(PROBE_MARKER + json.dumps({\n"
            "    'paths': [str(Path(entry or '.').resolve())\n"
            "               for entry in sys.path]}))\n"
            % (str(ROOT), str(ROOT / "chat2api")))
        result = self._run_probe(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = self._payload_of(result)
        self.assertEqual(Path(payload["paths"][0]).resolve(), ROOT.resolve())
        self.assertNotIn(
            str((ROOT / "chat2api").resolve()), payload["paths"])

    def test_clean_sys_path_drops_ancestors_and_pythonpath(self):
        """An inherited repository PYTHONPATH must not survive the entry.

        ``_clean_sys_path`` owns the process ``sys.path``: the package root is
        first, and neither the project root (``SelfUseAI``) nor the Git root
        above it may stay on the path or be handed to a child through
        ``PYTHONPATH``.
        """
        body = (
            "sys.path.insert(0, %r)\n"
            "sys.path.insert(0, %r)\n"
            "module._clean_sys_path()\n"
            "print(PROBE_MARKER + json.dumps({\n"
            "    'paths': [str(Path(entry or '.').resolve())\n"
            "               for entry in sys.path],\n"
            "    'PYTHONPATH': os.environ.get('PYTHONPATH', '')}))\n"
            % (str(REPO), str(REPO.parent)))
        result = self._run_probe(
            body,
            env_extra={"PYTHONPATH": os.pathsep.join(
                [str(REPO), str(REPO.parent)])})
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = self._payload_of(result)
        paths = [str(Path(entry).resolve()) for entry in payload["paths"]]
        self.assertEqual(paths[0], str(ROOT.resolve()))
        self.assertNotIn(str(REPO.resolve()), paths)
        self.assertNotIn(str(REPO.parent.resolve()), paths)
        self.assertEqual(payload["PYTHONPATH"], "")

    def test_entry_sets_isolated_home_and_cleans_path(self):
        root = Path(tempfile.mkdtemp(prefix="r19-cloud-home-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fixture = _write_config(root)
        expected_home = (root / "home").resolve()
        body = (
            "code = module.main(['--config', %r, '--check-config'])\n"
            "print(PROBE_MARKER + json.dumps({\n"
            "    'code': code,\n"
            "    'HOME': os.environ.get('HOME'),\n"
            "    'USERPROFILE': os.environ.get('USERPROFILE'),\n"
            "    'PYTHONPATH': os.environ.get('PYTHONPATH'),\n"
            "    'first_path': str(Path(sys.path[0] or '.').resolve())}))\n"
            % str(fixture))
        result = self._run_probe(
            body, env_extra={"PYTHONPATH": str(REPO)})
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = self._payload_of(result)
        self.assertEqual(payload["code"], 0)
        self.assertEqual(Path(payload["HOME"]).resolve(), expected_home)
        self.assertEqual(Path(payload["USERPROFILE"]).resolve(), expected_home)
        self.assertIsNone(payload["PYTHONPATH"])
        self.assertEqual(Path(payload["first_path"]).resolve(), ROOT.resolve())

    def test_inherited_share_store_file_cannot_override_the_config(self):
        """E19-1-C1 item 3: the parent's ``SHARE_STORE_FILE`` is dropped.

        ``share_management`` binds ``SHARE_STORE_FILE`` into ``DEFAULT_STORE_FILE``
        at import time, so an inherited value would silently win over the explicit
        config. The entry must clear it BEFORE the first product import, and the
        assembled app must open the CONFIGURED store.
        """
        root = Path(tempfile.mkdtemp(prefix="r19-cloud-env-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        values = _fixture_config(root)
        _write_private(root / "config" / "broker-control.key",
                       "fixture-broker-key")
        fixture = _write_config(root, values)
        other = str(root / "other" / "share-store.db")
        body = (
            "code = module.main(['--config', %r, '--check-config'])\n"
            "after_entry = os.environ.get('SHARE_STORE_FILE')\n"
            "from cloud.backend.config import CloudConfig\n"
            "from cloud.backend.app import build_app\n"
            "import share.management as share_management\n"
            "config = CloudConfig.from_file(%r)\n"
            "app = build_app(config)\n"
            "print(PROBE_MARKER + json.dumps({\n"
            "    'code': code,\n"
            "    'after_entry': after_entry,\n"
            "    'applied': os.environ.get('SHARE_STORE_FILE'),\n"
            "    'configured': config.share_store_file,\n"
            "    'store_path': str(app.keys.manager.store_path),\n"
            "    'module_default': str(share_management.DEFAULT_STORE_FILE)}))\n"
            % (str(fixture), str(fixture)))
        result = self._run_probe(
            body, env_extra={"SHARE_STORE_FILE": other})
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = self._payload_of(result)
        self.assertEqual(payload["code"], 0)
        # The inherited value is gone before any product import runs ...
        self.assertIsNone(payload["after_entry"])
        # ... the root applies the CONFIGURED value exactly once ...
        self.assertEqual(payload["applied"], payload["configured"])
        self.assertNotEqual(payload["applied"], other)
        # ... and the assembled manager opens the configured store, not the
        # module-level default and not the inherited one.
        self.assertEqual(payload["store_path"], payload["configured"])
        self.assertNotEqual(payload["store_path"], other)


if __name__ == "__main__":
    unittest.main()
