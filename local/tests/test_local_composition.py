# -*- coding: utf-8 -*-
"""E19-1-08 local composition-root regression (r19).

The local application is an INDEPENDENT composition root:

* ``model_sync.configure_runtime`` redirects the catalog/state paths and the UM
  endpoints; ``None`` keeps the historical defaults;
* ``LocalConfig`` validates every field (missing/invalid -> exit 2) without
  binding ports, creating directories or starting children;
* ``run_local.py`` cleans inherited repository/source-checkout ``sys.path``
  entries before importing product modules, inserts the package root, applies the
  isolated ``HOME``/``USERPROFILE`` and parses ``--config`` /
  ``--check-config`` / ``--print-runtime-info``;
* the local registry registers ONLY ``model.catalog``; ``app.py`` never imports
  ``console_adapters`` / ``share_*`` / ``share_entry`` /
  ``console_display_proxy`` / ``usagesrv``;
* ``LocalApp`` serves only the local routes, enforces the control token and the
  loopback Host/Origin gates, starts the UM runtime and the OperationManager.

Every path in the fixture document lives under one private fixture root. The HTTP
test uses a real loopback listener with a fake runtime/manager seam, so no product
process, state directory or production port is touched.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

if os.name == "nt":
    import ctypes

AI_GATEWAY = Path(__file__).resolve().parents[2]
PROJECT_ROOT = AI_GATEWAY.parent
GIT_ROOT = AI_GATEWAY.parents[1]
sys.path.insert(0, str(AI_GATEWAY))

from console import stats as console_stats  # noqa: E402
from model_sync import sync as model_sync  # noqa: E402

from console.operations import ActionSpec, OperationManager  # noqa: E402
from local.backend import app as local_app  # noqa: E402
from local.backend.config import LocalConfig, LocalConfigError  # noqa: E402
from local.backend.registry import build_registry  # noqa: E402
from local.backend.stats_env import build_stats_environment  # noqa: E402

_RUNTIME_GLOBALS = (
    "DATA_DIR", "STATE_FILE", "BACKUP_DIR", "LOCK_FILE",
    "UM_MODELS_URL", "UM_CHAT_URL",
    "UM_MODELS_HOST", "UM_MODELS_PORT", "UM_MODELS_PATH",
    "_CATALOG_ENDPOINTS", "CONTEXT_PROBE_ENDPOINTS",
)


# --------------------------------------------------------------------- helpers
def _free_port():
    """One free loopback port that is never a production listener.

    E19-1-L2 / F33: this helper verified a ``LocalConfig`` that refuses
    ``FORBIDDEN_PORTS`` while itself accepting those very ports. The exclusion
    is applied here so the allocator obeys the constraint it tests.
    """
    from local.backend.config import FORBIDDEN_PORTS
    for _ in range(200):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in FORBIDDEN_PORTS:
            return port
    raise RuntimeError("could not allocate a non-production loopback port")


def _fixture_mapping(root):
    root = Path(root)
    return {
        "schema_version": 1,
        "host": "127.0.0.1",
        "port": 28001,
        "state_root": str(root / "state"),
        "home_dir": str(root / "home"),
        "config_dir": str(root / "config"),
        "log_dir": str(root / "logs"),
        "lock_dir": str(root / "locks"),
        "pid_file": str(root / "state" / "local-app.pid"),
        "ledger_file": str(root / "state" / "usage-chat2api.jsonl"),
        "session_code_file": str(root / "config" / "usage-session-code"),
        "control_token_file": str(root / "config" / "admin-control-token"),
        "console_state_file": str(root / "state" / "console-state.sqlite3"),
        "model_sync_dir": str(root / "state" / "model-sync"),
        "um_runtime": {
            "source_dir": str(AI_GATEWAY / "chat2api"),
            "python": sys.executable,
            "host": "127.0.0.1",
            "port": 28000,
            "profile_dir": str(root / "um" / "profile"),
            "token_file": str(root / "um" / "token.json"),
            "service_log": str(root / "um" / "chat2api.log"),
            # standards-config-06: the summary READS ``ledger_file`` and the
            # child WRITES ``usage_log``; they are ONE identity, so the fixture
            # names the SAME file (the old fixture used two different files and
            # the summary could never see the child's rows).
            "usage_log": str(root / "state" / "usage-chat2api.jsonl"),
            "control_token_file": str(root / "um" / "control-token"),
            "reauth_slot_file": str(root / "um" / "reauth.state"),
            # E19-1-04 step 8: the UM child HOME is explicit and is the same
            # isolated root the application uses, never dirname(profile_dir).
            "home_dir": str(root / "home"),
            "base_url": "http://127.0.0.1:28999",
            "upstream_token": "fixture-upstream-token",
            "auth_wait_budget_s": 180,
            "request_max_s": 360,
            "start_timeout_s": 5.0,
            "stop_timeout_s": 5.0,
        },
        "runtime_control": {
            "mode": "fixture",
            "script": None,
            "timeout_s": 5.0,
        },
    }


_REQUEST_LAST_COOKIE = []


def _request(port, method, path, *, body=None, token=None, host=None,
              cookie=None, timeout=5.0):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        headers = {}
        if token is not None:
            headers["X-Admin-Control"] = token
        if cookie is not None:
            headers["Cookie"] = cookie
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
        set_cookie = response.getheader("Set-Cookie")
        parsed = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError):
                parsed = None
        _REQUEST_LAST_COOKIE.append(set_cookie)
        return response.status, parsed
    finally:
        connection.close()


class _FakeRuntime:
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
        return {"pid": None, "port": 28000, "alive": False,
                "base_url": "http://127.0.0.1:28999"}


class _FakeManager:
    def __init__(self):
        self.submitted = []
        self.closed = 0

    def submit(self, action, target="", params=None, idempotency_key=None):
        self.submitted.append((action, target, params, idempotency_key))
        return {"id": "op-1", "action": action, "target": target,
                "state": "queued"}

    def get_operation(self, op_id):
        if op_id != "op-1":
            return None
        return {"id": "op-1", "action": "model.catalog", "state": "queued"}

    def list_operations(self, limit=50):
        return []

    def action_info(self):
        # The frontend contract: an object with an ``actions`` row array whose
        # rows carry action/enabled/cancelable/disabled_reason/description.
        return [{
            "action": "model.catalog",
            "enabled": True,
            "disabled_reason": None,
            "cancelable": False,
            "description": "模型目录只读查询",
            "param_schema": {},
        }]

    def shutdown(self, wait=True):
        self.closed += 1

    def close(self, wait=True):
        self.closed += 1


# --------------------------------------------------------- model_sync runtime
class ConfigureRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.saved = {name: getattr(model_sync, name)
                      for name in _RUNTIME_GLOBALS}

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(model_sync, name, value)

    def test_none_keeps_the_historical_defaults(self):
        model_sync.configure_runtime()
        self.assertEqual(model_sync.DATA_DIR, AI_GATEWAY / "data")
        self.assertEqual(model_sync.STATE_FILE,
                         AI_GATEWAY / "data" / "model-sync-state.json")
        self.assertEqual(model_sync.UM_MODELS_URL,
                         "http://127.0.0.1:8000/v1/models")

    def test_explicit_values_are_derived(self):
        root = Path(tempfile.mkdtemp(prefix="r19-local-runtime-"))
        data_dir = root / "model-sync"
        model_sync.configure_runtime(
            data_dir=data_dir,
            um_models_url="http://127.0.0.1:28000/v1/models",
            um_chat_url="http://127.0.0.1:28000/v1/chat/completions")
        self.assertEqual(model_sync.DATA_DIR, data_dir)
        self.assertEqual(model_sync.STATE_FILE,
                         data_dir / "model-sync-state.json")
        self.assertEqual(model_sync.BACKUP_DIR,
                         data_dir / "model-sync-backups")
        self.assertEqual(model_sync.LOCK_FILE, data_dir / "model-sync.lock")
        self.assertEqual(model_sync.UM_MODELS_URL,
                         "http://127.0.0.1:28000/v1/models")
        self.assertEqual(model_sync.UM_MODELS_HOST, "127.0.0.1")
        self.assertEqual(model_sync.UM_MODELS_PORT, 28000)
        self.assertEqual(model_sync.UM_MODELS_PATH, "/v1/models")
        self.assertEqual(model_sync._CATALOG_ENDPOINTS,
                         frozenset({"http://127.0.0.1:28000/v1/models"}))
        self.assertEqual(model_sync.UM_CHAT_URL,
                         "http://127.0.0.1:28000/v1/chat/completions")
        self.assertEqual(model_sync.CONTEXT_PROBE_ENDPOINTS["um"],
                         ("http://127.0.0.1:28000/v1/chat/completions",
                          None))

    def test_invalid_values_are_refused(self):
        cases = (
            {"data_dir": ""},
            {"um_models_url": "ftp://127.0.0.1/v1/models"},
            {"um_models_url": "http://127.0.0.1:not-a-port/v1/models"},
            {"um_chat_url": "http://127.0.0.1/v1/chat/completions?x=1"},
        )
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    model_sync.configure_runtime(**values)

    def test_client_config_paths_are_gone(self):
        """Ticket #32: the DSH/ZCode client config paths are retired."""
        for name in ("DSH_YAML", "ZCODE_JSON", "ZCODE_PROVIDER_CONFIG"):
            self.assertFalse(hasattr(model_sync, name), name)


# ------------------------------------------------------------- UM child env
class UmRuntimeConfigTests(unittest.TestCase):
    """E19-1-04 steps 8-9: the UM child HOME is explicit and the upstream token
    never appears in argv.

    Seam: ``UmRuntime._child_env()`` / ``UmRuntime._command()`` — the two values
    handed to ``subprocess.Popen``. Both are behaviour: what the child process
    receives, not how the runtime builds it.
    """

    def setUp(self):
        from local.backend.um_runtime import UmRuntime

        self.runtime_cls = UmRuntime
        self.root = Path(tempfile.mkdtemp(prefix="r19-um-env-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.values = _fixture_mapping(self.root)
        self.config = LocalConfig.from_mapping(self.values)

    def test_child_home_comes_from_the_explicit_home_dir(self):
        runtime = self.runtime_cls(self.config.um_runtime)
        env = runtime._child_env()
        home = Path(self.values["um_runtime"]["home_dir"]).resolve()
        self.assertEqual(home, Path(self.config.home_dir).resolve())
        self.assertEqual(Path(env["HOME"]).resolve(), home)
        self.assertEqual(Path(env["USERPROFILE"]).resolve(), home)
        self.assertEqual(Path(env["TMPDIR"]).resolve(), home / "tmp")
        self.assertEqual(Path(env["TEMP"]).resolve(), home / "tmp")
        # The profile parent is NOT the child home any more: it happens to sit
        # under the same root here, so assert the derivation directly.
        self.assertNotEqual(
            Path(env["HOME"]).resolve(),
            Path(self.values["um_runtime"]["profile_dir"]).resolve().parent
            / "profile")

    def test_missing_home_dir_is_refused(self):
        values = _fixture_mapping(self.root)
        values["um_runtime"].pop("home_dir")
        with self.assertRaises(LocalConfigError) as caught:
            LocalConfig.from_mapping(values)
        self.assertEqual(caught.exception.field, "um_runtime.home_dir")

    def test_home_dir_must_be_absolute_and_under_the_local_home(self):
        cases = ("relative/home", str(self.root / "other-home"))
        for value in cases:
            with self.subTest(value=value):
                values = _fixture_mapping(self.root)
                values["um_runtime"]["home_dir"] = value
                with self.assertRaises(LocalConfigError) as caught:
                    LocalConfig.from_mapping(values)
                self.assertEqual(caught.exception.field, "um_runtime.home_dir")

    def test_home_dir_equal_to_the_local_home_is_accepted(self):
        values = _fixture_mapping(self.root)
        values["um_runtime"]["home_dir"] = values["home_dir"]
        config = LocalConfig.from_mapping(values)
        self.assertEqual(Path(config.um_runtime.home_dir).resolve(),
                         Path(config.home_dir).resolve())

    def test_upstream_token_is_passed_by_env_never_by_argv(self):
        runtime = self.runtime_cls(self.config.um_runtime)
        token = self.config.um_runtime.upstream_token
        argv = runtime._command()
        self.assertNotIn("--token", argv)
        self.assertNotIn(token, argv)
        self.assertEqual(runtime._child_env()["CHAT2API_UPSTREAM_TOKEN"], token)

    def test_a_port_owned_by_another_listener_is_refused_before_spawning(self):
        """standards-lifecycle-17: never adopt someone else's runtime.

        ``UmRuntime.start`` spawned unconditionally. After a hard kill the previous
        runtime keeps listening (nothing reaps it), so a restart on the SAME port
        spawned a second process; ``wait_ready`` then polled ``/health`` and could
        be answered by the ORPHAN, reporting "started" for a child this instance
        does not own. On Windows both listeners even bind the same port. The port
        is now probed before spawning and a live listener is refused.
        """
        import socket as _socket

        runtime = self.runtime_cls(self.config.um_runtime)
        blocker = _socket.socket()
        try:
            blocker.bind(("127.0.0.1", self.config.um_runtime.port))
            blocker.listen(1)
            with self.assertRaises(Exception) as caught:
                runtime.start()
            self.assertIn("already listening",
                          str(caught.exception))
            self.assertIsNone(runtime._process,
                              "a refused start must not spawn a child")
        finally:
            blocker.close()


# ------------------------------------------------------------------- config
class LocalConfigTests(unittest.TestCase):
    def test_the_read_ledger_and_the_write_ledger_must_be_the_same_file(self):
        """standards-config-06 / spec-local-006: one ledger, one identity.

        ``ledger_file`` is what the summary reads; ``um_runtime.usage_log`` is
        what the 8100 process writes. The old config accepted them as two
        independent paths, so the summary could read a ledger the child never
        wrote and report a quiet zero. A mismatch is a configuration error.
        """
        root = Path(tempfile.mkdtemp(prefix="r19-local-ledger-"))
        self.addCleanup(shutil.rmtree, root, True)
        values = _fixture_mapping(root)
        values["ledger_file"] = str(root / "state" / "a-ledger.jsonl")
        values["um_runtime"]["usage_log"] = str(root / "um" / "b-usage.jsonl")
        with self.assertRaises(LocalConfigError) as caught:
            LocalConfig.from_mapping(values)
        self.assertEqual(caught.exception.field,
                         "um_runtime.usage_log")

    def test_the_common_parent_is_never_a_filesystem_root(self):
        """standards-config-08: a root common parent must not be an allow root.

        When the configured roots sit in different top-level directories their
        common parent is the filesystem root, and every absolute path is "under"
        it — the whole home guard then accepted the real user home. This pins the
        local half; the cloud half is pinned in ``tests/test_r19_cloud_root.py``.
        """
        from local.backend.config import _home_roots
        root = Path(tempfile.mkdtemp(prefix="r19-local-root-"))
        self.addCleanup(shutil.rmtree, root, True)
        values = _fixture_mapping(root)
        drive = Path(os.path.abspath(os.sep)).resolve()
        for name, relative in (("state_root", "state"),
                                ("log_dir", "logs"),
                                ("lock_dir", "locks"),
                                ("config_dir", "cfg")):
            values[name] = str(drive / relative)
        roots = _home_roots(values)
        self.assertFalse(
            any(entry == drive for entry in roots),
            "the filesystem root must never be a home allow root: %r"
            % (roots,))

    def test_a_nul_byte_in_a_path_is_refused(self):
        """standards-config-14: the path guard must inspect the OPENED string.

        ``os.path.isabs`` accepts a string with a NUL byte, and Windows
        ``Path.resolve`` silently turns the NUL into a space, so the guard
        compared a DIFFERENT path than the one ``open()`` later uses (which
        raises ``ValueError``). The external boundary must refuse it up front.
        """
        root = tempfile.mkdtemp(prefix="r19-local-nul-")
        self.addCleanup(shutil.rmtree, root, True)
        for field in ("log_dir", "ledger_file", "state_root"):
            with self.subTest(field=field):
                values = _fixture_mapping(root)
                values[field] = values[field] + chr(0) + "bad"
                with self.assertRaises(LocalConfigError) as caught:
                    LocalConfig.from_mapping(values)
                self.assertEqual(caught.exception.field, field)

    def test_missing_required_field_is_named(self):
        values = _fixture_mapping(tempfile.mkdtemp(prefix="r19-local-cfg-"))
        values.pop("control_token_file")
        with self.assertRaises(LocalConfigError) as caught:
            LocalConfig.from_mapping(values)
        self.assertEqual(caught.exception.field, "control_token_file")
        self.assertIn("control_token_file", str(caught.exception))

    def test_invalid_host_port_paths_and_um_endpoint_are_refused(self):
        base = _fixture_mapping(tempfile.mkdtemp(prefix="r19-local-cfg-"))
        cases = (
            ("host", "0.0.0.0", "host"),
            ("host", "example.com", "host"),
            ("port", 0, "port"),
            ("port", 70000, "port"),
            ("port", True, "port"),
            ("ledger_file", "relative/ledger.jsonl", "ledger_file"),
            ("port", 28000, "um_runtime.port"),
            ("um_runtime", {"host": "0.0.0.0"}, "um_runtime.host"),
            ("um_runtime", {"port": 0}, "um_runtime.port"),
            ("um_runtime", {"source_dir": str(AI_GATEWAY / "usagesrv.py")},
             "um_runtime.source_dir"),
            ("um_runtime", {"base_url": "ftp://127.0.0.1:28999"},
             "um_runtime.base_url"),
            ("um_runtime", {"base_url": "http://192.168.1.10:28999"},
             "um_runtime.base_url"),
            ("um_runtime", {"base_url": "http://127.0.0.1:28000"},
             "um_runtime.base_url"),
            ("um_runtime", {"auth_wait_budget_s": 0},
             "um_runtime.auth_wait_budget_s"),
            ("um_runtime", {"auth_wait_budget_s": 181},
             "um_runtime.auth_wait_budget_s"),
            ("runtime_control", {"mode": "shell"}, "runtime_control.mode"),
            ("runtime_control", {"mode": "powershell"},
             "runtime_control.script"),
            ("runtime_control", {"timeout_s": 0}, "runtime_control.timeout_s"),
        )
        for field, value, expected_field in cases:
            with self.subTest(field=field, value=value):
                values = dict(base)
                if field == "um_runtime":
                    values[field] = dict(base[field], **value)
                elif field == "runtime_control":
                    values[field] = dict(base[field], **value)
                else:
                    values[field] = value
                with self.assertRaises(LocalConfigError) as caught:
                    LocalConfig.from_mapping(values)
                self.assertEqual(caught.exception.field, expected_field)

    def test_unknown_field_is_refused(self):
        values = _fixture_mapping(tempfile.mkdtemp(prefix="r19-local-cfg-"))
        values["unexpected_field"] = "x"
        with self.assertRaises(LocalConfigError) as caught:
            LocalConfig.from_mapping(values)
        self.assertEqual(caught.exception.field, "unexpected_field")
        nested = _fixture_mapping(tempfile.mkdtemp(prefix="r19-local-cfg-"))
        nested["um_runtime"]["unexpected_field"] = "x"
        with self.assertRaises(LocalConfigError) as caught:
            LocalConfig.from_mapping(nested)
        self.assertEqual(caught.exception.field, "um_runtime.unexpected_field")

    def test_full_fixture_config_loads(self):
        # E19-1-05 step 3 / G10: the document is built in a temp root, not
        # read from the repository ``tmp/r19-e19-1`` artifact.
        root = Path(tempfile.mkdtemp(prefix="r19-full-cfg-"))
        self.addCleanup(shutil.rmtree, root, True)
        config = LocalConfig.from_mapping(_fixture_mapping(root))
        self.assertEqual(config.port, 28001)
        self.assertEqual(config.um_runtime.port, 28000)
        self.assertEqual(config.um_runtime.base_url,
                         "http://127.0.0.1:28999")
        self.assertEqual(config.runtime_control.mode, "fixture")
        self.assertEqual(Path(config.config_dir).resolve(),
                         (root / "config").resolve())

    def test_fixture_paths_are_isolated(self):
        root = Path(tempfile.mkdtemp(prefix="r19-iso-cfg-")).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        fixture = _fixture_mapping(root)
        source_fields = {"source_dir", "python", "host", "base_url",
                         "upstream_token", "mode"}
        for name, value in fixture.items():
            if name in ("um_runtime", "runtime_control"):
                continue
            if name in source_fields or not isinstance(value, str):
                continue
            path = Path(value).resolve()
            self.assertTrue(path == root or root in path.parents,
                            "%s=%s escapes %s" % (name, value, root))
        for name, value in fixture["um_runtime"].items():
            if name in source_fields or not isinstance(value, str):
                continue
            path = Path(value).resolve()
            self.assertTrue(path == root or root in path.parents,
                            "um_runtime.%s=%s escapes %s"
                            % (name, value, root))
        self.assertNotIn(fixture["port"], (8000, 8001, 8100, 18096,
                                         5901))
        self.assertNotIn(fixture["um_runtime"]["port"],
                         (8000, 8001, 8100, 18096, 5901))


class IsolationGuardRegressionTests(unittest.TestCase):
    """spec-local-007: the five live guards need fail-back regression cases.

    The five guards were verified by hand probes only, so deleting any one of them
    would not turn a single test red. Each case names the field and the guard's
    message so a removed/changed guard fails loudly.
    """

    def _mapping(self):
        root = Path(tempfile.mkdtemp(prefix="r19-guard-"))
        self.addCleanup(shutil.rmtree, root, True)
        return _fixture_mapping(root), root

    def _refused(self, mutate):
        values, _root = self._mapping()
        mutate(values)
        with self.assertRaises(LocalConfigError) as caught:
            LocalConfig.from_mapping(values)
        return str(caught.exception)

    def test_a_production_port_is_refused(self):
        for port in (8000, 8001, 18096, 5901):
            with self.subTest(port=port):
                message = self._refused(
                    lambda values, p=port: values.update({"port": p}))
                self.assertIn("must not be a production port", message)

    def test_the_real_deployment_state_is_refused(self):
        for field in ("ledger_file", "state_root"):
            with self.subTest(field=field):
                message = self._refused(
                    lambda values, f=field: values.update(
                        {f: str(AI_GATEWAY / "data" / "probe.jsonl")}))
                self.assertIn("must not name the real deployment state",
                              message)

    def test_the_real_user_home_is_refused(self):
        message = self._refused(
            lambda values: values.update(
                {"home_dir": str(Path.home().resolve())}))
        self.assertIn("must not be an ancestor of the isolated roots",
                      message)

    def test_the_production_runtime_wrapper_is_refused(self):
        message = self._refused(
            lambda values: values.update({"runtime_control": {
                "mode": "powershell",
                "script": str(AI_GATEWAY / "invoke-gateway-service.ps1"),
                "timeout_s": 5.0}}))
        self.assertIn("must not be the production runtime wrapper", message)

    def test_the_config_dir_must_be_the_config_parent(self):
        # ``config_dir`` is checked by ``validate_config_dir`` at the entry (it
        # needs the ``--config`` path), not by ``from_mapping``.
        other = Path(tempfile.mkdtemp(prefix="r19-guard-other-"))
        self.addCleanup(shutil.rmtree, other, True)
        values, root = self._mapping()
        values["config_dir"] = str(other)
        config = LocalConfig.from_mapping(values)
        with self.assertRaises(LocalConfigError) as caught:
            config.validate_config_dir(root / "config" / "local-config.json")
        self.assertEqual(caught.exception.field, "config_dir")
        self.assertIn("must be the parent directory of --config",
                      str(caught.exception))


class RuntimeControlClassificationTests(unittest.TestCase):
    """E19-1-04 step 2 (F32): ``mode=powershell`` is a production action.

    ``service_runtime.PowerShellServiceRuntime`` runs the configured script with
    ``subprocess.run([..., "-File", script, "-Action", action])``. The contract
    documented in ``local/README.md`` and in the plan is:

    * the script must live INSIDE the package (a controlled test fixture);
    * its content must not contain ``Start-Process``,
      ``Invoke-ConsoleRuntimeStart`` or a dot-source of
      ``gateway-runtime.ps1``;
    * any out-of-package path is refused.

    The implementation used to do the exact opposite (it refused in-package
    scripts and accepted any out-of-package copy, including a copy of the real
    8000/8001 starter), so these cases pin the direction.
    """

    def _mapping(self, script):
        root = Path(tempfile.mkdtemp(prefix="r19-runtime-cfg-"))
        self.addCleanup(shutil.rmtree, root, True)
        values = _fixture_mapping(root)
        values["runtime_control"] = {
            "mode": "powershell",
            "script": str(script),
            "timeout_s": 5.0,
        }
        return values

    def _refused(self, script, expected_field="runtime_control.script"):
        with self.assertRaises(LocalConfigError) as caught:
            LocalConfig.from_mapping(self._mapping(script))
        self.assertEqual(caught.exception.field, expected_field)
        return str(caught.exception)

    def test_out_of_package_script_is_refused(self):
        outside = Path(tempfile.mkdtemp(prefix="r19-outside-script-"))
        self.addCleanup(shutil.rmtree, outside, True)
        script = outside / "harmless.ps1"
        script.write_text(
            "param([string]$Action) Write-Output '{\"ok\": true}'\n",
            encoding="utf-8")
        message = self._refused(script)
        self.assertIn("must live inside the deployment package", message)

    def test_out_of_package_copy_of_the_real_wrapper_is_refused(self):
        # The exact regression F32 describes: a copy of the real starter kept
        # outside the package was ACCEPTED by the old guard.
        outside = Path(tempfile.mkdtemp(prefix="r19-outside-real-"))
        self.addCleanup(shutil.rmtree, outside, True)
        copy = outside / "invoke-console-runtime-json.ps1"
        copy.write_text(
            ". (Join-Path $PSScriptRoot 'lib\\gateway-runtime.ps1')\n"
            "Invoke-ConsoleRuntimeStart -Key 'chat2api'\n",
            encoding="utf-8")
        self._refused(copy)

    def test_in_package_fixture_script_is_accepted(self):
        fixture = AI_GATEWAY / "local" / "tests" / "fixture-runtime.ps1"
        self.assertTrue(fixture.is_file(), fixture)
        config = LocalConfig.from_mapping(self._mapping(fixture))
        self.assertEqual(config.runtime_control.mode, "powershell")
        self.assertEqual(Path(config.runtime_control.script).resolve(),
                         fixture.resolve())

    def test_in_package_script_with_start_process_is_refused(self):
        inside = AI_GATEWAY / "local" / "tests" / "_r19_guard_probe_start.ps1"
        inside.write_text(
            "param([string]$Action)\n"
            "Start-Process -FilePath 'chat2api.py'\n",
            encoding="utf-8")
        self.addCleanup(inside.unlink, True)
        message = self._refused(inside)
        self.assertIn("Start-Process", message)

    def test_in_package_script_with_runtime_dot_source_is_refused(self):
        inside = AI_GATEWAY / "local" / "tests" / "_r19_guard_probe_dotsrc.ps1"
        inside.write_text(
            "param([string]$Action)\n"
            ". (Join-Path $PSScriptRoot 'gateway-runtime.ps1')\n",
            encoding="utf-8")
        self.addCleanup(inside.unlink, True)
        message = self._refused(inside)
        self.assertIn("gateway-runtime.ps1", message)

    def test_in_package_script_with_console_runtime_start_is_refused(self):
        inside = AI_GATEWAY / "local" / "tests" / "_r19_guard_probe_start.ps1"
        inside.write_text(
            "param([string]$Action)\n"
            "Invoke-ConsoleRuntimeStart -Key 'chat2api'\n",
            encoding="utf-8")
        self.addCleanup(inside.unlink, True)
        message = self._refused(inside)
        self.assertIn("Invoke-ConsoleRuntimeStart", message)


# -------------------------------------------------------------- run_local
class RunLocalCliTests(unittest.TestCase):
    """The entry point, run as a real subprocess under ``-I``.

    E19-1-05 step 3 / G10: the config document is built HERE from
    ``_fixture_mapping`` and written into a fresh temp root. The old version read
    ``tmp/r19-e19-1/local-config.json`` — a repository artifact whose
    ``fixture/`` directory no longer exists, so the test passed only because it
    never actually walked the fixture paths.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="r19-cli-")).resolve()
        self.config_dir = self.root / "config"
        self.config_dir.mkdir(parents=True)
        values = _fixture_mapping(self.root)
        self.config_path = self.config_dir / "local-config.json"
        self.config_path.write_text(
            json.dumps(values, indent=2), encoding="utf-8")
        self.missing_path = self.config_dir / "local-missing.json"
        broken = dict(values)
        broken.pop("control_token_file")
        self.missing_path.write_text(
            json.dumps(broken, indent=2), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, *args):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTHONPATH", None)
        return subprocess.run(
            [sys.executable, "-I", str(AI_GATEWAY / "local" / "run_local.py"),
             *args],
            capture_output=True, text=True, timeout=120, env=env)

    def test_the_entry_refuses_a_non_isolated_interpreter(self):
        """standards-tests-06: the ``-I`` guard has a regression case.

        The guard (``local/run_local.py``) is the isolation fact the whole
        composition root rests on: without ``-I`` a planted ``json.py`` /
        ``argparse.py`` on the inherited ``sys.path`` would execute during
        import. No test ran the entry WITHOUT ``-I``, so deleting the guard
        would not turn anything red.
        """
        result = subprocess.run(
            [sys.executable, str(AI_GATEWAY / "local" / "run_local.py"),
             "--help"],
            capture_output=True, text=True, timeout=120,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("must run as", result.stderr)
        self.assertIn("-I", result.stderr)

    def test_check_config_cli_exit_codes(self):
        good = self._run("--config", str(self.config_path),
                         "--check-config")
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertIn('"status": "ok"', good.stdout)
        bad = self._run("--config", str(self.missing_path),
                        "--check-config")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("control_token_file", bad.stderr)

    def test_print_runtime_info_is_isolated(self):
        result = self._run("--config", str(self.config_path),
                           "--print-runtime-info")
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.assertEqual(info["role"], "local")
        self.assertEqual(Path(info["config_path"]).resolve(),
                         self.config_path.resolve())
        self.assertEqual(Path(info["module_file"]).resolve(),
                         (AI_GATEWAY / "local" / "run_local.py").resolve())
        # The package root must be present so ``shared`` and ``local`` import;
        # the Git repository root and the project directory above the package
        # must NOT leak into the child's sys.path.
        sys_path = [Path(entry).resolve() for entry in info["sys_path"]]
        self.assertIn(AI_GATEWAY.resolve(), sys_path)
        self.assertNotIn(GIT_ROOT.resolve(), sys_path)
        self.assertNotIn(PROJECT_ROOT.resolve(), sys_path)
        paths = {Path(value).resolve()
                 for value in info["config_paths"].values()}
        for path in paths:
            self.assertTrue(path == self.root or self.root in path.parents,
                            "%s escapes %s" % (path, self.root))
        self.assertNotIn(str(PROJECT_ROOT.resolve()),
                         info["PYTHONPATH"] or "")
        # E19-1-04 step 10: the isolated HOME is NOT the real user home.
        self.assertEqual(Path(info["home_dir"]).resolve(),
                         (self.root / "home").resolve())
        self.assertNotEqual(Path(info["home_dir"]).resolve(),
                            Path(info["real_home"]).resolve())

    def test_print_runtime_info_reports_the_os_level_home(self):
        """E19-1-04 step 10: ``real_home`` is the OS account directory.

        A parent that already exports a fake ``HOME``/``USERPROFILE`` must not
        change it: the isolation proof would otherwise compare the fixture home
        against another environment variable.
        """
        fake = self.root / "fake-parent-home"
        fake.mkdir()
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTHONPATH", None)
        env["HOME"] = str(fake)
        env["USERPROFILE"] = str(fake)
        result = subprocess.run(
            [sys.executable, "-I", str(AI_GATEWAY / "local" / "run_local.py"),
             "--config", str(self.config_path), "--print-runtime-info"],
            capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.assertIn("original_home_env", info)
        self.assertEqual(Path(info["original_home_env"]).resolve(),
                         fake.resolve())
        real_home = Path(info["real_home"]).resolve()
        self.assertNotEqual(real_home, fake.resolve(),
                            "real_home read the inherited HOME, not the OS "
                            "account directory")
        self.assertNotEqual(real_home,
                            Path(info["home_dir"]).resolve())
        self.assertEqual(Path(info["home_dir"]).resolve(),
                         (self.root / "home").resolve())
        if os.name == "nt":
            buf = ctypes.create_unicode_buffer(260)
            ctypes.windll.shell32.SHGetFolderPathW(None, 40, None, 0, buf)
            self.assertEqual(real_home, Path(buf.value).resolve())
        else:
            import pwd
            self.assertEqual(real_home,
                             Path(pwd.getpwuid(os.getuid()).pw_dir).resolve())

    def test_print_runtime_info_does_not_start_services(self):
        info = json.loads(self._run(
            "--config", str(self.config_path),
            "--print-runtime-info").stdout)
        self.assertNotIn("console_adapters", info["modules"])
        self.assertNotIn("share_management", info["modules"])
        self.assertNotIn("share_entry", info["modules"])
        self.assertNotIn("usagesrv", info["modules"])

    def test_the_entry_really_cleans_an_inherited_sys_path(self):
        """standards-tests-17: the cleaning seam has a DISCRIMINATING case.

        The subprocess assertion ``assertNotIn(polluted, info["PYTHONPATH"])`` is
        tautological: ``-I`` implies ``-E``, so the entry never sees the parent's
        ``PYTHONPATH`` in the first place — the assertion holds even if the
        cleaning is deleted. This case calls ``_clean_sys_path()`` directly with a
        planted checkout entry on ``sys.path`` and a planted ``PYTHONPATH``, and
        requires both to be gone afterwards.
        """
        import importlib.util

        entry = AI_GATEWAY / "local" / "run_local.py"
        spec = importlib.util.spec_from_file_location(
            "_r19_run_local_probe", entry)
        module = importlib.util.module_from_spec(spec)
        # The production entry refuses to run without ``python -I``; this
        # in-process probe fakes only the flag it reads, then exercises
        # ``_clean_sys_path()`` directly.
        with mock.patch.object(sys, "flags",
                               types.SimpleNamespace(isolated=True,
                                                     optimize=0,
                                                     verbose=0)):
            spec.loader.exec_module(module)

        checkout = Path(tempfile.mkdtemp(prefix="r19-checkout-"))
        self.addCleanup(shutil.rmtree, checkout, True)
        # A directory that LOOKS like the source checkout (usagesrv.py +
        # chat2api/) is the shape the cleaner must drop.
        (checkout / "usagesrv.py").write_text("", encoding="utf-8")
        (checkout / "chat2api").mkdir()

        saved_path = list(sys.path)
        saved_env = os.environ.get("PYTHONPATH")
        try:
            sys.path.insert(0, str(checkout))
            sys.path.insert(0, str(PROJECT_ROOT))
            os.environ["PYTHONPATH"] = str(checkout)
            module._clean_sys_path()
            cleaned = [Path(item or ".").resolve() for item in sys.path]
            self.assertNotIn(checkout.resolve(), cleaned,
                             "a planted source checkout must be dropped")
            self.assertNotIn(PROJECT_ROOT.resolve(), cleaned,
                             "the project directory above the package must be "
                             "dropped")
            self.assertEqual(cleaned[0], AI_GATEWAY.resolve(),
                             "the package root must come first")
            self.assertNotIn("PYTHONPATH", os.environ,
                             "the inherited PYTHONPATH must be removed so it "
                             "cannot smuggle the repository back in")
        finally:
            sys.path[:] = saved_path
            if saved_env is not None:
                os.environ["PYTHONPATH"] = saved_env
            else:
                os.environ.pop("PYTHONPATH", None)


# ---------------------------------------------------------------------- app
class LocalAppTests(unittest.TestCase):
    def setUp(self):
        self.saved_runtime = {name: getattr(model_sync, name)
                              for name in _RUNTIME_GLOBALS}
        self.saved_stats = console_stats._STATS_ENVIRONMENT
        self.root = Path(tempfile.mkdtemp(prefix="r19-local-app-"))
        self.port = _free_port()
        values = _fixture_mapping(self.root)
        values["port"] = self.port
        token_file = Path(values["control_token_file"])
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text("fixture-control-token", encoding="utf-8")
        # E19-1-05 step 1: the control-token/session-code gate is 0600 on
        # POSIX (the production reader enforces it), so the fixture must set
        # the mode or every POSIX run refuses to start.
        if os.name == "posix":
            os.chmod(token_file, 0o600)
            os.chmod(self.root, 0o700)
        Path(values["ledger_file"]).parent.mkdir(parents=True, exist_ok=True)
        Path(values["ledger_file"]).write_text(
            json.dumps({"time": "2026-11-07T10:00:00+08:00",
                        "model": "fake-um-model", "status": 200,
                        "input_tokens": 10, "output_tokens": 5,
                        "cached_input_tokens": 0,
                        "reasoning_tokens": 0}) + "\n",
            encoding="utf-8")
        # The browser session path: a one-time bootstrap code the fixture
        # writes, and the cookie the host issues on exchange.
        self.session_code = "fixture-session-code"
        code_file = Path(values["session_code_file"])
        code_file.parent.mkdir(parents=True, exist_ok=True)
        code_file.write_text(self.session_code + "\n", encoding="utf-8")
        if os.name == "posix":
            os.chmod(code_file, 0o600)
        self.cookie = None
        self.config = LocalConfig.from_mapping(values)
        self.runtime = _FakeRuntime()
        self.manager = _FakeManager()
        self.env = build_stats_environment(self.config)
        self.app = local_app.LocalApp(
            self.config, runtime_control=self.runtime,
            stats_reader=self.env, manager=self.manager)

    def tearDown(self):
        self.app.close()
        console_stats._STATS_ENVIRONMENT = self.saved_stats
        for name, value in self.saved_runtime.items():
            setattr(model_sync, name, value)

    def test_a_stop_refuses_new_work_before_the_runtime_is_torn_down(self):
        """D18-10: a stop first refuses new work, then drains with a bound.

        The old ``_finish`` stopped the runtime first and only then closed the
        listener, so a submission that arrived during the stop window was accepted
        and executed against an already-stopped runtime. Now the listener is closed
        first (new connections are refused), the in-flight handlers drain with a
        bound, and the runtime stops last.
        """
        self.app.start()
        status, _ = _request(
            self.port, "POST", "/api/console/operations",
            body={"action": "model.catalog", "target": "all", "params": {}},
            token="fixture-control-token")
        self.assertEqual(status, 201)
        self.app.shutdown()
        self.assertTrue(self.app.is_stopping())
        # The listener is closed by the stop: a new submission is refused at the
        # transport level instead of reaching a stopped runtime.
        with self.assertRaises(OSError):
            _request(
                self.port, "POST", "/api/console/operations",
                body={"action": "model.catalog", "target": "all",
                       "params": {}},
                token="fixture-control-token")
        self.assertGreaterEqual(self.runtime.stopped, 1,
                                "the runtime must be stopped after the drain")

    def test_a_stop_does_not_wait_forever_on_an_in_flight_request(self):
        """The drain is BOUNDED: a stuck handler cannot block the stop."""
        self.app.start()
        release = __import__("threading").Event()
        original = self.manager.submit

        def slow_submit(*args, **kwargs):
            release.wait(timeout=30)
            return original(*args, **kwargs)

        self.manager.submit = slow_submit
        submitted = []

        def fire():
            try:
                submitted.append(_request(
                    self.port, "POST", "/api/console/operations",
                    body={"action": "model.catalog", "target": "all",
                           "params": {}},
                    token="fixture-control-token"))
            except OSError:
                pass

        worker = __import__("threading").Thread(target=fire, daemon=True)
        worker.start()
        __import__("time").sleep(0.2)
        started = __import__("time").monotonic()
        self.app.shutdown()
        elapsed = __import__("time").monotonic() - started
        release.set()
        worker.join(timeout=10)
        self.assertLess(
            elapsed, local_app.SHUTDOWN_DRAIN_TIMEOUT_S + 5.0,
            "the stop must not wait unbounded on an in-flight request")

    def test_a_refused_start_never_stops_a_runtime_it_did_not_start(self):
        """Ownership: a start that fails must not stop someone else's runtime."""
        missing = Path(self.root) / "missing-token"
        values = _fixture_mapping(self.root)
        values["port"] = self.port
        values["control_token_file"] = str(missing)
        config = LocalConfig.from_mapping(values)
        runtime = _FakeRuntime()
        app = local_app.LocalApp(
            config, runtime_control=runtime,
            stats_reader=self.env, manager=self.manager)
        with self.assertRaises(RuntimeError):
            app.start()
        app.close()
        self.assertEqual(runtime.started, 0)
        self.assertEqual(runtime.stopped, 0)

    def test_a_failed_start_rolls_back_the_manager_and_can_be_retried(self):
        """A recoverable bind failure must not leave a half-dead process.

        ``_start_listening`` used to close the manager but keep the closed
        instance in ``self.manager``, so a retry reused a closed SQLite handle
        and every later request failed.
        """
        attempts = []
        original = local_app._LocalHTTPServer

        def failing_once(address, handler):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("simulated bind failure")
            return original(address, handler)

        with mock.patch.object(local_app, "_LocalHTTPServer", failing_once):
            with self.assertRaises(RuntimeError):
                self.app.start()
            self.assertIsNone(self.app.manager,
                              "the closed manager must not be reused")
            self.app.start()
        status, payload = _request(
            self.port, "POST", "/api/console/operations",
            body={"action": "model.catalog", "target": "all", "params": {}},
            token="fixture-control-token")
        self.assertEqual(status, 201, payload)
        self.app.shutdown()

    def test_a_thread_start_failure_rolls_back_the_listener_and_the_runtime(self):
        """standards-lifecycle-03: a Thread.start() failure must roll back.

        The bind-failure case is covered above, but ``threading.Thread.start()``
        can also fail (resource exhaustion, interpreter shutdown) — and it runs
        AFTER the socket is bound and AFTER the UM runtime started, while
        ``self._server`` is already assigned. Without a rollback the listener
        stayed open, the runtime kept running, and ``close()`` blocked forever in
        ``server.shutdown()`` (which waits for a ``serve_forever`` that never
        ran).
        """
        import threading as _threading
        import time as _time

        def failing_start(thread):
            raise RuntimeError("no thread")

        with mock.patch.object(_threading.Thread, "start", failing_start):
            with self.assertRaises(RuntimeError):
                self.app.start()
        self.assertIsNone(self.app._server,
                          "a failed start must not keep the listener")
        self.assertEqual(self.runtime.stopped, 1,
                         "the runtime started by the failed attempt must stop")
        # ``close()`` must return promptly instead of waiting on a serve loop
        # that never started.
        started_at = _time.monotonic()
        self.app.close()
        self.assertLess(_time.monotonic() - started_at, 10.0,
                        "close() must not hang after a failed start")

    def test_main_reports_a_failed_stop_and_keeps_the_original_failure(self):
        """standards-lifecycle-16: the close path must not mask the real cause.

        ``main``'s ``finally`` called ``app.close()`` unconditionally, so an
        exception from the stop path escaped the ``finally`` and replaced the
        recorded start failure: the caller saw "stop failed" instead of the real
        reason, and the exit code changed from 1 to an uncaught exception.
        """
        def failing_stop():
            raise OSError("runtime control pipe gone")

        self.runtime.stop = failing_stop
        app = local_app.LocalApp(
            self.config, runtime_control=self.runtime,
            stats_reader=self.env, manager=self.manager)
        original_start = app.start

        def failing_start():
            raise RuntimeError("start failed for the probe")

        app.start = failing_start
        with mock.patch.object(local_app, "build_app", lambda config: app):
            code = local_app.main(self.config)
        self.assertEqual(code, 1,
                         "the original start failure must keep exit code 1")
        self.assertIsNotNone(original_start)

    def test_a_failed_stop_is_not_swallowed_on_close(self):
        """standards-lifecycle-02: a failed stop must be reported, not ignored."""
        self.app.start()

        def failing_stop():
            raise OSError("runtime control pipe gone")

        self.runtime.stop = failing_stop
        with self.assertRaises(OSError):
            self.app.close()

    def test_the_listener_loop_crash_reaches_the_entry_exit_code(self):
        """A crashed listener loop must not be reported as a clean exit."""
        def crashing(server):
            raise RuntimeError("listener loop crashed")

        with mock.patch.object(local_app._LocalHTTPServer, "serve_forever",
                               crashing):
            self.app.start()
            with self.assertRaises(RuntimeError):
                self.app.serve_forever()
        self.assertIn("listener loop crashed", str(self.app._failure))

    def test_registry_registers_only_model_catalog(self):
        registry = build_registry()
        self.assertEqual([spec.name for spec in registry], ["model.catalog"])
        self.assertTrue(registry[0].enabled)
        self.assertEqual(registry[0].resource_fn("", {}), "model:catalog")

    def test_a_disabled_action_carries_an_explicit_reason(self):
        """standards-tests-09 / E19-1-04 step 7 (F15): both states are covered.

        The old assertions were ``assertTrue(registry[0].enabled)`` and
        ``assertIsInstance(..., bool)``; ``isinstance(True, bool)`` is true, so a
        registry that hard-coded ``enabled=True`` could never be caught. This case
        injects a disabled spec and requires a non-empty ``disabled_reason``.
        """
        disabled = ActionSpec(
            "model.catalog", "disabled fixture",
            handler=lambda *args, **kwargs: {},
            resource_fn=lambda target, params: "model:catalog",
            enabled=False,
            disabled_reason="fixture action is disabled",
        )
        self.assertFalse(disabled.enabled)
        self.assertTrue(disabled.disabled_reason)
        manager = OperationManager(
            db_path=str(Path(tempfile.mkdtemp(prefix="r19-disabled-"))
                        / "ops.sqlite3"),
            registry=[disabled], lock_dir=str(Path(tempfile.mkdtemp(
                prefix="r19-disabled-lock-"))))
        self.addCleanup(manager.close, wait=False)
        row = manager.action_info()[0]
        self.assertEqual(row["enabled"], False)
        self.assertEqual(row["disabled_reason"],
                         "fixture action is disabled")

    def test_routes_require_the_control_token_and_loopback(self):
        self.app.start()
        status, payload = _request(self.port, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["role"], "local")
        status, _ = _request(self.port, "GET", "/api/console/actions")
        self.assertEqual(status, 401)
        status, payload = _request(
            self.port, "GET", "/api/console/actions",
            token="fixture-control-token")
        self.assertEqual(status, 200)
        # E19-1-04 step 8 / G5: the frontend's normalizeCapabilities
        # requires an OBJECT with an ``actions`` row array (a bare array throws
        # ContractError at requiredObject). Shape assertion only — the row fields
        # belong to console_operations, not to this test.
        self.assertIsInstance(payload, dict, payload)
        self.assertIsInstance(payload.get("actions"), list, payload)
        self.assertEqual([row["action"] for row in payload["actions"]],
                         ["model.catalog"])
        self.assertIsInstance(payload["actions"][0]["enabled"], bool)
        status, _ = _request(self.port, "GET", "/health",
                              host="evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(self.runtime.started, 1)

    def test_every_frontend_read_face_has_a_local_route(self):
        """spec-local-002 / A9: the local host must serve all 9 read faces.

        The plan's A9 criterion is "9 read faces each have a route and return
        200", and it explicitly removes the old "recorded as not implemented"
        permission. The host only served ``/api/summary``, ``/api/console/
        actions`` and ``/api/console/operations/<id>``; the frontend's eight
        ``read*`` methods plus ``readOperations`` all answered
        404 ``unknown path``.
        """
        self.app.start()
        paths = (
            "/api/console/services",
            "/api/console/um/today",
            "/api/console/um/trend?range=7d",
            "/api/console/um/requests?window=today&result=all&limit=10",
            "/api/console/um/logs?window=today&limit=10",
            # The producer's request identity is a 32-lowercase-hex id; a
            # human-shaped id like ``req-1`` is refused by the reader.
            "/api/console/um/request-logs?request_id=%s&limit=10" % ("a" * 32),
            "/api/console/um/maintenance?limit=10",
            "/api/console/gateway-connection?service=chat2api",
            "/api/console/operations?limit=5",
        )
        for path in paths:
            with self.subTest(path=path):
                status, payload = _request(
                    self.port, "GET", path,
                    token="fixture-control-token")
                self.assertEqual(status, 200, (path, payload))

    def test_the_usagesrv_prefix_is_an_alias_of_the_local_routes(self):
        """spec-local-004: the prefix contract is one of the two documented ways.

        The plan requires the host to EITHER implement the ``/gw/usagesrv/api``
        and ``/gw/usagesrv/api/console`` prefixes OR point the frontend's
        basePath at the real local path. The frontend still hard-codes the
        prefix and the host had no rewrite, so every prefixed request was a 404.
        This pins the alias branch.
        """
        self.app.start()
        status, payload = _request(
            self.port, "GET", "/gw/usagesrv/api/summary",
            token="fixture-control-token")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["role"], "local")
        status, payload = _request(
            self.port, "GET", "/gw/usagesrv/api/console/actions",
            token="fixture-control-token")
        self.assertEqual(status, 200, payload)
        self.assertIn("actions", payload)

    def test_a_session_cookie_reaches_the_console_read_faces(self):
        """spec-local-003: the browser session path must really work.

        The host forced ``X-Admin-Control`` on every console read face and the
        frontend never sends it, while ``POST /api/session`` only wrote a
        ``Set-Cookie`` and the host had NO branch reading/validating it. A
        browser could therefore reach neither the documented (a) old contract nor
        (b) the cookie session. This pins (b): a valid cookie reaches the read
        faces, and a missing/expired cookie is refused.
        """
        self.app.start()
        _REQUEST_LAST_COOKIE.clear()
        status, _ = _request(
            self.port, "POST", "/api/session",
            body={"code": self.session_code})
        self.assertEqual(status, 200)
        set_cookie = _REQUEST_LAST_COOKIE[-1]
        self.assertTrue(set_cookie, "the session exchange must set a cookie")
        cookie = set_cookie.split(";", 1)[0]
        status, payload = _request(
            self.port, "GET", "/api/summary", cookie=cookie)
        self.assertEqual(status, 200, payload)
        status, _ = _request(self.port, "GET", "/api/summary")
        self.assertEqual(status, 401)
        status, _ = _request(
            self.port, "GET", "/api/summary", cookie="um_session=wrong")
        self.assertEqual(status, 401)

    def test_operations_and_summary_and_shutdown(self):
        self.app.start()
        status, payload = _request(
            self.port, "POST", "/api/console/operations",
            body={"action": "model.catalog", "target": "all", "params": {}},
            token="fixture-control-token")
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["operation"]["id"], "op-1")
        self.assertEqual(self.manager.submitted[0][:2],
                         ("model.catalog", "all"))
        status, payload = _request(
            self.port, "GET", "/api/console/operations/op-1",
            token="fixture-control-token")
        self.assertEqual(status, 200)
        status, payload = _request(
            self.port, "GET", "/api/console/operations/missing",
            token="fixture-control-token")
        self.assertEqual(status, 404)
        status, payload = _request(
            self.port, "GET",
            "/api/summary?from=2026-11-07&to=2026-11-07",
            token="fixture-control-token")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["totals"]["requests"], 1)
        status, _ = _request(
            self.port, "POST", "/admin/shutdown", body={},
            token="fixture-control-token")
        self.assertEqual(status, 200)
        deadline = __import__("time").monotonic() + 5.0
        while __import__("time").monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port),
                                             timeout=0.2):
                    __import__("time").sleep(0.05)
            except OSError:
                break
        else:
            self.fail("local app listener stayed open after shutdown")
        self.assertGreaterEqual(self.runtime.stopped, 1)
        self.assertGreaterEqual(self.manager.closed, 1)

    def test_composition_root_avoids_cloud_and_legacy_hosts(self):
        for name in ("app.py", "registry.py", "um_runtime.py",
                     "service_runtime.py", "stats_env.py", "config.py"):
            source = (AI_GATEWAY / "local" / "backend" / name).read_text(
                encoding="utf-8")
            for forbidden in ("import usagesrv", "import console_adapters",
                              "import share_management", "import share_store",
                              "import share_entry",
                              "import console_display_proxy"):
                self.assertNotIn(forbidden, source, name)
        # In-process ``sys.modules`` is order-dependent (other suites import
        # these modules); the child-process check below is the discriminating
        # one, so this test only asserts the source has no such import.

    def test_loaded_module_set_excludes_cloud_and_legacy_hosts(self):
        """R19-1: observed in a CHILD, not through this runner's own imports.

        This test module imports ``console_stats``/``model_sync`` itself, so an
        in-process ``sys.modules`` check could pass for the wrong reason. The child
        imports only the composition root.
        """
        forbidden = ("cloud", "usagesrv", "console.adapters",
                     "share.management", "share.store",
                     "share.instance_pipe", "share.metering",
                     "share.broker_host", "console.display_proxy")
        # R19-5 retained surface: recorded here so a future change that ADDS a
        # module to this set is visible, but NOT asserted absent - retiring them
        # is E19-3's scope (plan step 13).
        retained = ("model_sync.sync", "console.stats",
                    "console.model_actions")
        code = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "import local.backend.app\n"
            "for name in %r:\n"
            "    print('forbidden', name, name in sys.modules)\n"
            "for name in %r:\n"
            "    print('retained', name, name in sys.modules)\n"
        ) % (str(AI_GATEWAY), forbidden, retained)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTHONPATH", None)
        completed = subprocess.run(
            [sys.executable, "-I", "-c", code], capture_output=True,
            text=True, timeout=120, env=env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        seen = {}
        for line in completed.stdout.splitlines():
            group, name, value = line.split()
            seen[(group, name)] = value == "True"
        for name in forbidden:
            with self.subTest(forbidden=name):
                self.assertFalse(seen[("forbidden", name)],
                                 "%s leaked into the local runtime" % name)
        for name in retained:
            with self.subTest(retained=name):
                self.assertTrue(
                    seen[("retained", name)],
                    "%s is no longer loaded; update the R19-5 conflict note "
                    "in this test and in the plan" % name)

    def test_stats_environment_is_configured_before_the_runtime_starts(self):
        """E19-1-04 step 15: order is a contract, not an implementation detail.

        The UM runtime becomes reachable the moment it starts, so a request that
        arrives before ``console_stats.configure`` would hit the unconfigured read
        face. The fake runtime records what the process-wide environment was at
        the instant ``start()`` was called.
        """
        observed = {}
        env = self.env
        original_start = self.runtime.start

        def recording_start():
            observed["configured"] = console_stats._STATS_ENVIRONMENT
            return original_start()

        self.runtime.start = recording_start
        self.app.start()
        self.assertIs(observed.get("configured"), env)

    def test_model_sync_runtime_is_redirected_before_the_listener_binds(self):
        """The catalog action must never read the repository ``data/`` directory."""
        self.app.start()
        self.assertEqual(model_sync.DATA_DIR,
                         Path(self.config.model_sync_dir))
        self.assertEqual(
            model_sync.UM_MODELS_URL,
            "http://127.0.0.1:%d/v1/models" % self.config.um_runtime.port)
        self.assertEqual(
            model_sync.UM_CHAT_URL,
            "http://127.0.0.1:%d/v1/chat/completions"
            % self.config.um_runtime.port)


if __name__ == "__main__":
    unittest.main()
