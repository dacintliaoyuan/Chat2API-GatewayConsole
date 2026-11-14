# -*- coding: utf-8 -*-
"""HTTP-level regression tests for the console surface (P1 local foundation).

Boots a real usagesrv instance on an ephemeral port with a mocked runtime
snapshot (no PowerShell spawn, no real service touched) and exercises the
management boundary: session bootstrap, CSRF gate, forged-host/origin
rejection, registry honesty (enablement computed from the deployment
authorization policy data: proven read-only diagnostics, the authorized
service targets and the section-5 approved writes are enabled; every
remaining real write stays disabled with its precise reason) and the
unauthorized-target submit path.
"""

import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TMP = tempfile.mkdtemp(prefix="console-api-tests-")
# These import-time writes must happen BEFORE this module's own usagesrv object is
# exec'd below (it reads them at import). They are recorded here so tearDownModule
# can put the PRE-IMPORT values back: mock.patch.dict restores only to the values
# seen when ITS patch starts, so without this restore the module would leak its
# finished temp layout to every later test module in the same process.
_PRE_IMPORT_ENV = {name: os.environ.get(name) for name in (
    "CONSOLE_STATE_FILE", "CONSOLE_LOCK_DIR", "USAGE_SESSION_CODE_FILE",
    "USAGE_CONTROL_TOKEN_FILE", "SHARE_STORE_FILE", "SHARE_MANAGEMENT_CONFIG")}
os.environ["CONSOLE_STATE_FILE"] = os.path.join(_TMP, "console-state.sqlite3")
os.environ["CONSOLE_LOCK_DIR"] = os.path.join(_TMP, "console-locks")
os.environ["USAGE_SESSION_CODE_FILE"] = os.path.join(_TMP, "usage-session-code")
os.environ["USAGE_CONTROL_TOKEN_FILE"] = os.path.join(_TMP, "admin-control-token")
# Isolate both the store and broker deployment BEFORE manager assembly.
# A deleted fixture key carries a revoking intent; production recovery must
# never be reachable from this HTTP test server.
os.environ["SHARE_STORE_FILE"] = os.path.join(_TMP, "share-store.db")
os.environ["SHARE_MANAGEMENT_CONFIG"] = os.path.join(_TMP, "no-share-deployment.json")
_CONTROL_TOKEN = "test-control-token-console-api"
from share.secret_protection import write_private_secret_path
write_private_secret_path(
    os.environ["USAGE_CONTROL_TOKEN_FILE"], _CONTROL_TOKEN.encode("utf-8"))

_USAGESRV_PATH = Path(__file__).resolve().parents[1] / "usagesrv.py"
_SPEC = importlib.util.spec_from_file_location("usage_service_console_api",
                                               _USAGESRV_PATH)
usagesrv = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = usagesrv
_SPEC.loader.exec_module(usagesrv)

from console import adapters as console_adapters  # noqa: E402  (same directory, sys.path prepared)
from console import share_actions as orch  # noqa: E402  (share observe seam)
import share.metering as share_metering  # noqa: E402  (share-usage payload clock seam)
import share.store as share_store  # noqa: E402  (B8-6 key-name projection fixture store)


# Guard before any test can construct a default management client. The
# module-level env redirection alone is NOT enough: an earlier test module in a
# full discover run imports the canonical usagesrv first, so its constants are
# already the production paths when this module loads. Patch the constants and the
# deployment loader on BOTH module objects, and count attempted production access
# separately because a route may swallow the exception.
_ISOLATION_ATTEMPTS = []
_ISOLATION_PATCHERS = []
_ISOLATION_MODULES = []
_ISOLATION_TEMP = None

class _ConsoleApiStatsEnvironment:
    """StatsEnvironment fixture for this module's statistics routes.

    Paths point into this module's fixture directory, so the console_stats reads
    behind the HTTP routes never touch a production ledger.
    """

    chat2api_log = property(
        lambda self: os.path.join(_TMP, "usage-chat2api.jsonl"))
    dsh_snapshot = property(
        lambda self: os.path.join(_TMP, "dsh-snapshot.json"))
    dsh_merge_day = property(lambda self: usagesrv.DSH_MERGE_DAY)
    parse_ts = staticmethod(lambda value: usagesrv._parse_ts(value))
    beijing_day = staticmethod(lambda ms: usagesrv._beijing_day(ms))
    day_start_ms = staticmethod(lambda day: usagesrv._day_start_ms(day))
    display_path = staticmethod(lambda path: usagesrv._display_path(path))


def _guard_usage_module(module, root=None, patchers=None, modules=None):
    """Patch ONE usagesrv module object into an offline sandbox.

    ``root`` is the sandbox directory the module's deployment/store constants
    must point into; ``patchers``/``modules`` collect the started patches and
    the (module, old_console, old_share) snapshots so the caller that started
    the guard also ends it. The defaults are the module-level lists used by
    ``setUpModule``; ``ConsoleApiTestBase`` passes class-scoped lists, because
    the reused base class must re-guard the canonical module AFTER
    test_console_api's module guard has been torn down (a reusing module like
    test_model_settings_http_contract imports the canonical usagesrv first via
    console_stats, so its constants are the production paths).
    """
    patchers = _ISOLATION_PATCHERS if patchers is None else patchers
    modules = _ISOLATION_MODULES if modules is None else modules
    # A module already sandboxed by the module-level guard stays sandboxed for
    # the whole module run (that guard outlives every class), and re-patching
    # its constants under a DIFFERENT root would make the module guard's loader
    # reject its own sandboxed path. Skip it and keep one sandbox per module.
    if any(item[0] is module for item in _ISOLATION_MODULES):
        return False
    if any(item[0] is module for item in modules):
        return False
    root = Path(_ISOLATION_TEMP.name) if root is None else Path(root)
    modules.append((module, module._console_manager, module._share_manager))
    values = {"SHARE_MANAGEMENT_CONFIG_FILE": str(root / "absent-deployment.json"),
              "CONSOLE_STATE_FILE": str(root / (module.__name__ + ".sqlite3")),
              "CONSOLE_LOCK_DIR": str(root / (module.__name__ + "-locks")),
              "_console_manager": None, "_share_manager": None}
    original_loader = module._load_share_management_deployment

    def guarded_loader():
        target = Path(module.SHARE_MANAGEMENT_CONFIG_FILE).resolve()
        if not target.is_relative_to(root.resolve()):
            _ISOLATION_ATTEMPTS.append("production_deployment_loader")
            raise AssertionError("deployment config is outside the test sandbox")
        return original_loader()

    values["_load_share_management_deployment"] = guarded_loader
    for name, value in values.items():
        patcher = mock.patch.object(module, name, value)
        patcher.start()
        patchers.append(patcher)
    return True


def setUpModule():
    global _ISOLATION_TEMP, _ISOLATION_SAVED_SEAMS
    import share.broker_client as share_broker_client
    import share.broker_server as share_broker_server
    from console import share_actions as console_share_actions
    _ISOLATION_ATTEMPTS.clear()
    _ISOLATION_TEMP = tempfile.TemporaryDirectory(prefix="offline-management-")
    root = Path(_ISOLATION_TEMP.name)
    patcher = mock.patch.dict(os.environ, {
        "SHARE_MANAGEMENT_CONFIG": str(root / "absent-deployment.json"),
        "SHARE_STORE_FILE": str(root / "share.db"),
        "CONSOLE_STATE_FILE": str(root / "console.sqlite3"),
        "CONSOLE_LOCK_DIR": str(root / "console-locks")})
    patcher.start()
    _ISOLATION_PATCHERS.append(patcher)
    import usagesrv as canonical_usage_service
    _guard_usage_module(canonical_usage_service)
    _guard_usage_module(usagesrv)
    _ISOLATION_SAVED_SEAMS = {
        name: value for name, value in vars(console_share_actions).items()
        if name.startswith("_") and name.upper() == name}
    # The retired Windows named-pipe transport is gone with the old local
    # share chain (#33); the POSIX socket transport needs no kernel facade.
    _ = (share_broker_client, share_broker_server)


def tearDownModule():
    from console import share_actions as console_share_actions
    try:
        for module, old_console, _old_share in _ISOLATION_MODULES:
            current = module._console_manager
            if current is not None and current is not old_console:
                current.close(wait=True)
    finally:
        for patcher in reversed(_ISOLATION_PATCHERS):
            patcher.stop()
        _ISOLATION_PATCHERS.clear()
        _ISOLATION_MODULES.clear()
        for name, value in _ISOLATION_SAVED_SEAMS.items():
            setattr(console_share_actions, name, value)
        _ISOLATION_TEMP.cleanup()
        # 导入期的 env 写入（本文件 31-46 行）不在任何 patch.dict 的还原
        # 范围内：这里把它们还原到导入前的值，否则本模块结束之后所有后续
        # 测试模块都会继承这份已结束的临时目录路径。
        for name, value in _PRE_IMPORT_ENV.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    if _ISOLATION_ATTEMPTS:
        raise AssertionError("management isolation breached: " +
                             repr(_ISOLATION_ATTEMPTS))

FAKE_SNAPSHOT = {
    "ok": True,
    "services": [{
        "key": "usage", "display_name": "用量统计", "port": 8001,
        "state": "Running", "healthy": True, "detail": "运行中",
        "process_ids": [1],
    }],
    "meta": {"source": "gateway-runtime", "generated_epoch": 99},
}


_FROZEN_NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    """秒级冻结的时刻源，只覆盖 now()。

    share_metering 只经 now / fromisoformat / fromtimestamp 三个入口取时刻；
    这里冻结 now() 是为了让 checkedAt（「本次读取的时刻」，契约里就是读取
    当时的时间戳）在多次请求之间可比，从而让「逐字段一致」成为一条全等断言，
    而不是把该字段从断言里排除掉。
    """

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return _FROZEN_NOW.replace(tzinfo=None)
        return _FROZEN_NOW.astimezone(tz)


class ConsoleApiTestBase(unittest.TestCase):
    @classmethod
    def _start_class_isolation(cls):
        """Sandbox the management assembly for THIS class.

        Isolation belongs to the reusable BASE CLASS, not to the module that
        happens to define it. In a full discover run the canonical usagesrv is
        imported FIRST by test_console_stats (test_console_stats -> import
        usagesrv), so it carries the production deployment/store constants; test_console_api's module guard covers it
        only while its own module runs and is torn down before a reusing
        module's classes start. The inherited setUpClass would then assemble the
        console manager through wire_share_production_seams -> _default_broker ->
        the canonical loader and read the production share-management.json (the
        one production-boundary attempt the plain subset run records).
        Re-asserting the sandbox here — BOTH usagesrv module objects, the
        deployment loader guard and the named-pipe kernel refusal, plus the env
        redirect when the module guard does not already own the canonical
        module — keeps the assembly offline for every subclass, wherever it lives.
        """
        cls._isolation_dir = tempfile.TemporaryDirectory(
            prefix="console-api-class-")
        cls.addClassCleanup(cls._isolation_dir.cleanup)
        root = Path(cls._isolation_dir.name)
        cls._isolation_patchers = []
        cls._isolation_modules = []
        cls._isolation_attempts_before = len(_ISOLATION_ATTEMPTS)
        import usagesrv as canonical_usage_service
        guarded = _guard_usage_module(canonical_usage_service, root,
                                      cls._isolation_patchers,
                                      cls._isolation_modules)
        guarded = _guard_usage_module(usagesrv, root,
                                      cls._isolation_patchers,
                                      cls._isolation_modules) or guarded
        # The class-level env redirect exists for the modules that read the env
        # at CALL time; it must NOT fight the module-level guard, which already
        # owns the canonical module (its root is a DIFFERENT directory, and
        # re-pointing env under it would leave env and the module constants on
        # two different sandboxes).
        if guarded:
            cls._isolation_env = mock.patch.dict(os.environ, {
                "SHARE_MANAGEMENT_CONFIG": str(root / "absent-deployment.json"),
                "SHARE_STORE_FILE": str(root / "share.db"),
                "CONSOLE_STATE_FILE": str(root / "console.sqlite3"),
                "CONSOLE_LOCK_DIR": str(root / "console-locks")})
            cls._isolation_env.start()
            cls.addClassCleanup(cls._isolation_env.stop)
        import share.broker_client as share_broker_client
        import share.broker_server as share_broker_server
        # The retired Windows named-pipe transport is gone with the old
        # local share chain (#33).
        _ = (share_broker_client, share_broker_server)
        cls._isolation_seams = {
            name: value for name, value in vars(orch).items()
            if name.startswith("_") and name.upper() == name}
        # Registered last => runs FIRST among the isolation cleanups, but still
        # AFTER the manager/server cleanups registered later in setUpClass
        # (LIFO): an attempt made while those close still fails the class,
        # and the module-level guard cannot be relied on here (a reusing
        # module never runs test_console_api's tearDownModule check).
        cls.addClassCleanup(cls._stop_class_isolation)

    @classmethod
    def _stop_class_isolation(cls):
        try:
            for module, old_console, _old_share in cls._isolation_modules:
                current = module._console_manager
                if current is not None and current is not old_console:
                    current.close(wait=True)
        finally:
            from console import stats as console_stats
            console_stats._STATS_ENVIRONMENT = cls._isolation_stats_before
            for patcher in reversed(cls._isolation_patchers):
                patcher.stop()
            for name, value in cls._isolation_seams.items():
                setattr(orch, name, value)
        if len(_ISOLATION_ATTEMPTS) > cls._isolation_attempts_before:
            raise AssertionError(
                "management isolation breached in %s: %r"
                % (cls.__name__,
                   _ISOLATION_ATTEMPTS[cls._isolation_attempts_before:]))

    @classmethod
    def setUpClass(cls):
        cls.status_patcher = mock.patch(
            "console.adapters.service_status", return_value=FAKE_SNAPSHOT)
        cls.status_patcher.start()
        cls.addClassCleanup(cls.status_patcher.stop)
        cls._start_class_isolation()
        # 控制台状态库再指向本类夹具目录：_start_class_isolation 已把
        # CONSOLE_STATE_FILE/CONSOLE_LOCK_DIR 指进类沙箱，这里再指一次是
        # 同一沙箱内的双保险；顺序仍要紧——get_console_manager 首次装配时把
        # db_path 绑在 manager 上（模块级缓存），装配之后再改路径不会生效。
        cls._state_dir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._state_dir.cleanup)
        cls._saved_state_file = usagesrv.CONSOLE_STATE_FILE
        cls._saved_lock_dir = usagesrv.CONSOLE_LOCK_DIR
        usagesrv.CONSOLE_STATE_FILE = os.path.join(
            cls._state_dir.name, "console-state.sqlite3")
        usagesrv.CONSOLE_LOCK_DIR = os.path.join(
            cls._state_dir.name, "console-locks")
        cls.addClassCleanup(setattr, usagesrv, "CONSOLE_STATE_FILE",
                            cls._saved_state_file)
        cls.addClassCleanup(setattr, usagesrv, "CONSOLE_LOCK_DIR",
                            cls._saved_lock_dir)
        # Fresh manager per class: get_console_manager caches a module-level
        # singleton; closing it in one class would poison the next class.
        usagesrv._console_manager = None
        # R9-R34: local console reads are open now, so ANY read in this class
        # can populate the services TTL cache; reset it per class so the
        # cache-metadata assertion starts cold regardless of alphabetical
        # test order.
        usagesrv._console_status_cache["value"] = None
        usagesrv._console_status_cache["at"] = 0.0
        cls.manager = usagesrv.get_console_manager()
        cls.addClassCleanup(cls.manager.close)
        cls.server = usagesrv.ThreadingHTTPServer(("127.0.0.1", 0),
                                                  usagesrv.Handler)
        cls.addClassCleanup(cls.server.shutdown)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        from console import stats as console_stats
        # standards-tests-04: the class installed a PROCESS-WIDE environment and
        # never restored it, so every later module of the same ``discover`` run
        # inherited an environment whose paths pointed into this class's temp
        # directory. Save and restore it around the class.
        cls._isolation_stats_before = console_stats._STATS_ENVIRONMENT
        console_stats.configure(_ConsoleApiStatsEnvironment())

    def req(self, method, path, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
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

    def exchange_session(self):
        """One-time bootstrap code -> HttpOnly session cookie value."""
        status, _ = self.req("POST", "/api/session",
                             body={"code": usagesrv.session_bootstrap_code()})
        self.assertEqual(status, 200)
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/session",
                     body=json.dumps(
                         {"code": usagesrv.session_bootstrap_code()}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        set_cookie = resp.getheader("Set-Cookie")
        self.assertTrue(set_cookie and set_cookie.startswith("usage_session="),
                        set_cookie)
        return set_cookie.split(";")[0]


def _write_console_identity(directory, role, *, line="production",
                            source_hash="a" * 64):
    """Write a build identity into a throw-away console tree."""
    path = os.path.join(directory, "candidate-build.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"line": line, "role": role,
                   "sourceHash": source_hash}, f)
    return path


class ConsoleReadTests(ConsoleApiTestBase):
    def test_console_static_page_removed(self):
        # 2026-09-23 retirement: the old six-page console static face is gone
        # (web/index.html, web/console/ and the /console route were deleted).
        # /console* must answer the generic unknown-path 404 — no static file
        # serving, and the traversal shape stays refused.
        for path in ("/console", "/console/index.html", "/console/console.js",
                     "/console/..%2fusagesrv.py", "/gw/usagesrvfoo"):
            status, raw = self.req("GET", path)
            self.assertEqual(status, 404, path)
            self.assertEqual(json.loads(raw)["error"], "unknown path")

    def test_root_serves_console_dist_index(self):
        # Root path serves ONLY the console build artifact whose identity names
        # this process's role. The real dist is never touched here: a tempfile
        # stub stands in for web/dist via the module seams.
        with tempfile.TemporaryDirectory() as td:
            index = os.path.join(td, "index.html")
            with open(index, "wb") as f:
                f.write(b"<html><body>console-dist-stub</body></html>")
            identity = _write_console_identity(td, "local")
            with mock.patch.object(usagesrv, "INDEX_FILE", index), \
                    mock.patch.object(usagesrv, "CONSOLE_BUILD_IDENTITY_FILE",
                                      identity):
                status, raw = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"console-dist-stub", raw)

    def test_root_html_is_not_cacheable(self):
        # A rebuild replaces the entry chunk; the root HTML must not be reused
        # from a browser cache under the previous role's page.
        with tempfile.TemporaryDirectory() as td:
            index = os.path.join(td, "index.html")
            with open(index, "wb") as f:
                f.write(b"<html><body>no-store-console-stub</body></html>")
            identity = _write_console_identity(td, "local")
            with mock.patch.object(usagesrv, "INDEX_FILE", index), \
                    mock.patch.object(usagesrv, "CONSOLE_BUILD_IDENTITY_FILE",
                                      identity):
                conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
                conn.request("GET", "/")
                resp = conn.getresponse()
                status = resp.status
                cache_control = resp.getheader("Cache-Control")
                resp.read()
                conn.close()
        self.assertEqual(status, 200)
        self.assertEqual(cache_control, "no-store")

    def test_root_missing_dist_honest_error(self):
        # dist missing -> honest JSON 500 naming the missing artifact; the
        # service never falls back to the retired old page.
        with tempfile.TemporaryDirectory() as td:
            identity = _write_console_identity(td, "local")
            missing = os.path.join(_TMP, "no-such-dist", "index.html")
            with mock.patch.object(usagesrv, "INDEX_FILE", missing), \
                    mock.patch.object(usagesrv, "CONSOLE_BUILD_IDENTITY_FILE",
                                      identity):
                status, raw = self.req("GET", "/")
        self.assertEqual(status, 500)
        self.assertIn("web/dist", json.loads(raw)["error"])

    def test_root_missing_build_identity_honest_error(self):
        # A tree without candidate-build.json cannot prove which role it belongs
        # to; root must answer 500 instead of hosting an unverified page.
        with tempfile.TemporaryDirectory() as td:
            index = os.path.join(td, "index.html")
            with open(index, "wb") as f:
                f.write(b"<html><body>unverified-console-stub</body></html>")
            missing = os.path.join(td, "no-such-identity.json")
            with mock.patch.object(usagesrv, "INDEX_FILE", index), \
                    mock.patch.object(usagesrv, "CONSOLE_BUILD_IDENTITY_FILE",
                                      missing):
                status, raw = self.req("GET", "/")
        self.assertEqual(status, 500)
        self.assertIn("candidate-build.json", json.loads(raw)["error"])
        self.assertNotIn(b"unverified-console-stub", raw)

    def test_root_refuses_cloud_tree_on_local_process(self):
        # 8001 is the local process: a cloud-role tree must never be hosted
        # there, even when its index.html exists.
        with tempfile.TemporaryDirectory() as td:
            index = os.path.join(td, "index.html")
            with open(index, "wb") as f:
                f.write(b"<html><body>cloud-console-stub</body></html>")
            identity = _write_console_identity(td, "cloud")
            with mock.patch.object(usagesrv, "INDEX_FILE", index), \
                    mock.patch.object(usagesrv, "CONSOLE_BUILD_IDENTITY_FILE",
                                      identity):
                status, raw = self.req("GET", "/")
        self.assertEqual(status, 500)
        error = json.loads(raw)["error"]
        self.assertIn("cloud", error)
        self.assertIn("local", error)
        self.assertNotIn(b"cloud-console-stub", raw)

    def test_root_cloud_process_serves_cloud_identity(self):
        # The cloud package carries its own web/dist with a cloud identity; the
        # same check must accept it when the process itself is the cloud role.
        with tempfile.TemporaryDirectory() as td:
            index = os.path.join(td, "index.html")
            with open(index, "wb") as f:
                f.write(b"<html><body>cloud-process-stub</body></html>")
            identity = _write_console_identity(td, "cloud")
            with mock.patch.object(usagesrv, "CLOUD_ROLE", True), \
                    mock.patch.object(usagesrv, "INDEX_FILE", index), \
                    mock.patch.object(usagesrv, "CONSOLE_BUILD_IDENTITY_FILE",
                                      identity):
                status, raw = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"cloud-process-stub", raw)

    def test_root_cloud_process_refuses_local_identity(self):
        # The reverse mismatch is refused too: a cloud process must not host the
        # local tree, which would otherwise show the wrong console on 8096.
        with tempfile.TemporaryDirectory() as td:
            index = os.path.join(td, "index.html")
            with open(index, "wb") as f:
                f.write(b"<html><body>local-tree-on-cloud-stub</body></html>")
            identity = _write_console_identity(td, "local")
            with mock.patch.object(usagesrv, "CLOUD_ROLE", True), \
                    mock.patch.object(usagesrv, "INDEX_FILE", index), \
                    mock.patch.object(usagesrv, "CONSOLE_BUILD_IDENTITY_FILE",
                                      identity):
                status, raw = self.req("GET", "/")
        self.assertEqual(status, 500)
        error = json.loads(raw)["error"]
        self.assertIn("local", error)
        self.assertIn("cloud", error)
        self.assertNotIn(b"local-tree-on-cloud-stub", raw)

    def test_root_refuses_candidate_line_with_matching_role(self):
        # A candidate-local tree copied into web/dist still says role=local,
        # but its line is candidate-local; the production host must not serve it.
        with tempfile.TemporaryDirectory() as td:
            index = os.path.join(td, "index.html")
            with open(index, "wb") as f:
                f.write(b"<html><body>candidate-line-stub</body></html>")
            identity = _write_console_identity(td, "local",
                                               line="candidate-local")
            with mock.patch.object(usagesrv, "INDEX_FILE", index), \
                    mock.patch.object(usagesrv, "CONSOLE_BUILD_IDENTITY_FILE",
                                      identity):
                status, raw = self.req("GET", "/")
        self.assertEqual(status, 500)
        self.assertIn("line", json.loads(raw)["error"])
        self.assertNotIn(b"candidate-line-stub", raw)

    def test_root_refuses_identity_without_source_hash(self):
        # role and line alone do not prove which source built the tree.
        with tempfile.TemporaryDirectory() as td:
            index = os.path.join(td, "index.html")
            with open(index, "wb") as f:
                f.write(b"<html><body>no-source-hash-stub</body></html>")
            identity = _write_console_identity(td, "local", source_hash="")
            with mock.patch.object(usagesrv, "INDEX_FILE", index), \
                    mock.patch.object(usagesrv, "CONSOLE_BUILD_IDENTITY_FILE",
                                      identity):
                status, raw = self.req("GET", "/")
        self.assertEqual(status, 500)
        self.assertIn("sourceHash", json.loads(raw)["error"])
        self.assertNotIn(b"no-source-hash-stub", raw)

    def test_root_refuses_identity_with_invalid_source_hash(self):
        # A malformed source identity is not a production build identity.
        with tempfile.TemporaryDirectory() as td:
            index = os.path.join(td, "index.html")
            with open(index, "wb") as f:
                f.write(b"<html><body>bad-source-hash-stub</body></html>")
            identity = _write_console_identity(td, "local",
                                               source_hash="not-a-sha256")
            with mock.patch.object(usagesrv, "INDEX_FILE", index), \
                    mock.patch.object(usagesrv, "CONSOLE_BUILD_IDENTITY_FILE",
                                      identity):
                status, raw = self.req("GET", "/")
        self.assertEqual(status, 500)
        self.assertIn("sourceHash", json.loads(raw)["error"])
        self.assertNotIn(b"bad-source-hash-stub", raw)

    def test_console_static_assets_served_from_dist(self):
        # Every regular file under the dist root is servable (our own build
        # output); a tempfile stub stands in for the real dist via the
        # module CONSOLE_DIST_DIR seam, mirroring the INDEX_FILE stubs above.
        with tempfile.TemporaryDirectory() as td:
            assets = os.path.join(td, "assets")
            os.makedirs(assets)
            script = os.path.join(assets, "um-ui-STUBHASH.js")
            with open(script, "wb") as f:
                f.write(b"console.log('console-stub');")
            with mock.patch.object(usagesrv, "CONSOLE_DIST_DIR", td):
                status, raw = self.req("GET", "/assets/um-ui-STUBHASH.js")
        self.assertEqual(status, 200)
        self.assertIn(b"console-stub", raw)

    def test_console_static_rejects_traversal_and_missing(self):
        # Traversal outside the dist root, absent files and directory paths
        # all answer the honest "unknown path" 404; no directory listing.
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(usagesrv, "CONSOLE_DIST_DIR", td):
                for path in ("/assets/../../secret.txt", "/assets/nope.js",
                             "/assets"):
                    status, raw = self.req("GET", path)
                    self.assertEqual(status, 404, path)
                    self.assertEqual(json.loads(raw)["error"], "unknown path")

    def test_gw_usagesrv_alias_equivalent_to_native(self):
        # /gw/usagesrv/* strips the prefix and dispatches through the same
        # routes as the 5173 dev-proxy rewrite; /api/summary answers
        # identically through both paths. This pins ROUTE equivalence, not
        # aggregation: the module summary cache is seeded so neither request
        # triggers the ~30s cold build (R9-R39), and the per-call cache-age
        # stamp (the only volatile field) is stripped before comparing.
        payload = {"ok": True, "exchangeRate": 7.2, "services": {}}
        saved = dict(usagesrv._SUMMARY_CACHE)
        usagesrv._SUMMARY_CACHE["value"] = payload
        usagesrv._SUMMARY_CACHE["at"] = time.time()
        try:
            status, native = self.req("GET", "/api/summary")
            self.assertEqual(status, 200)
            status, alias = self.req("GET", "/gw/usagesrv/api/summary")
        finally:
            usagesrv._SUMMARY_CACHE.update(saved)
        self.assertEqual(status, 200)
        native_json = json.loads(native)
        alias_json = json.loads(alias)
        native_json.pop("_cached_age_seconds", None)
        alias_json.pop("_cached_age_seconds", None)
        self.assertEqual(native_json, alias_json)

    def test_gw_usagesrv_alias_gates_apply_equally(self):
        # The route gates run BEFORE the alias rewrite: forged Host and
        # foreign Origin are rejected on the alias path exactly as on the
        # native path.
        status, _ = self.req("GET", "/gw/usagesrv/api/summary",
                             headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.req("GET", "/gw/usagesrv/api/summary",
                             headers={"Origin": "http://127.0.0.1:9999"})
        self.assertEqual(status, 403)

    def test_services_snapshot_with_cache_metadata(self):
        # R9-R34: reads are open now, so a preceding local-read test in this
        # class (alphabetical order) may have warmed the TTL cache; this
        # assertion needs a cold cache, so reset it here.
        usagesrv._console_status_cache["value"] = None
        usagesrv._console_status_cache["at"] = 0.0
        sid = self.exchange_session()
        auth = {"Cookie": sid}
        status, raw = self.req("GET", "/api/console/services", headers=auth)
        data = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(data["services"][0]["display_name"], "用量统计")
        self.assertIs(data["meta"]["cached"], False)
        status, raw = self.req("GET", "/api/console/services", headers=auth)
        data = json.loads(raw)
        self.assertIs(data["meta"]["cached"], True)
        self.assertIn("cache_age_seconds", data["meta"])

    def test_console_fully_open_cross_origin_gated(self):
        # R9-R35: the console face is open-and-use for local same-origin
        # callers (reads AND real side-effect writes); the public share
        # face keeps its auth. The local boundary is the route layer:
        # forged Host and foreign Origins are rejected before dispatch.
        status, raw = self.req("GET", "/api/console/services")
        self.assertEqual(status, 200)
        status, raw = self.req("GET", "/api/console/operations")
        self.assertEqual(status, 200)
        status, raw = self.req("GET", "/api/console/model-pending")
        # Ticket #32: the sync-pending read face is retired with the model
        # sync actions; the unknown GET route answers 404.
        self.assertEqual(status, 404)
        status, raw = self.req("POST", "/api/console/operations",
                               body={"action": "no.such", "target": "x"})
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(raw)["error"], "invalid_action")
        status, _ = self.req("POST", "/api/console/operations",
                             headers={"Origin": "http://127.0.0.1:9999"},
                             body={"action": "service.start", "target": "chat2api"})
        self.assertEqual(status, 403)

    def test_forged_host_and_cross_origin_rejected(self):
        status, _ = self.req("GET", "/api/console/services",
                             headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.req("GET", "/api/console/services",
                             headers={"Origin": "http://127.0.0.1:9999"})
        self.assertEqual(status, 403)

    def test_control_token_read_channel(self):
        status, raw = self.req("GET", "/api/console/actions",
                               headers={"X-Admin-Control": _CONTROL_TOKEN})
        self.assertEqual(status, 200)


class LogOverviewRouteTests(ConsoleApiTestBase):
    """P3/R3：日志总览的窄只读口 `GET /api/console/log-overview`。

    一次聚合全部注册目标（不再只读每个来源的首分类），每目标独立游标，
    归一时间排序；汇总的 coverage 如实标明来源覆盖 / 截断 / 未读 / 失败，
    读取失败 / 缺失的目标行数不计为 0，也不称「全部日志已读完」。夹具把
    reader 的三个路径根都指进临时沙箱，绝不读真实生产日志目录。
    """

    def test_overview_aggregates_all_targets_and_states_coverage(self):
        from console import log_reader as console_log_reader
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "chat2api").mkdir()
            (root / "LocalAppData" / "SelfUseAI" / "ai-gateway"
             / "share-backend").mkdir(parents=True)
            (root / "chat2api" / "chat2api.log").write_text(
                "2026-09-21 22:00:00 INFO main service line\n",
                encoding="utf-8")
            (root / "chat2api" / "chat2api.console.log").write_text(
                "2026-09-21 22:01:00 INFO console line\n", encoding="utf-8")
            (root / "LocalAppData" / "SelfUseAI" / "ai-gateway"
             / "share-backend" / "share-backend.log").write_text(
                '{"time": "2026-09-21T22:02:00+08:00", "model": "m"}\n',
                encoding="utf-8")
            with mock.patch.object(console_log_reader, "_HERE", root), \
                 mock.patch.object(console_log_reader, "SHARE_RUN_ROOT",
                                   root / "UMShareRun"), \
                 mock.patch.dict(os.environ,
                                  {"LOCALAPPDATA": str(root / "LocalAppData")}):
                status, raw = self.req(
                    "GET", "/api/console/log-overview?since=2026-09-21")
            self.assertEqual(status, 200)
            payload = json.loads(raw)
            self.assertTrue(payload["ok"])
            messages = "\n".join(row["message"] for row in payload["rows"])
            # 第二分类（console）与另一来源（share）都在，不是首分类截断。
            self.assertIn("console line", messages)
            self.assertIn("model=m", messages)
            self.assertIn("main service line", messages)
            self.assertEqual(payload["coverage"]["scope"],
                             "bounded_tail_window")
            self.assertFalse(payload["coverage"]["full_history"])
            self.assertIn("不是完整历史", payload["coverage"]["note"])
            targets = {(t["source"], t["category"]): t
                       for t in payload["targets"]}
            # 缺失目标如实上报，行数不计为 0。
            self.assertEqual(targets[("share", "faults")]["status"],
                             "missing")
            self.assertIsNone(targets[("share", "faults")]["rows"])
            self.assertGreaterEqual(
                payload["coverage"]["targets_missing"], 1)


class ConsoleShareUsageRouteTests(ConsoleApiTestBase):
    """R3 审查阻塞 1：控制台面的分享 Key 费用只读口。

    `GET /api/console/share-usage` 与 share 管理面的 `GET /api/share/usage`
    （要求管理令牌）共用同一个载荷构造点：同一账本、同一口径、同一版本化
    计价器，逐字段一致——绝不建第二套聚合，也绝不把管理令牌放进网页。
    控制台面按 R9-R35 本机同源页面免凭据直读，本地边界由路由层承担
    （跨源 Origin 与伪造 Host 一律拒绝）；写方法落到 reader 之前就被拒；
    share_metering 缺席时如实返回错误码，绝不折成 0 请求 / 0 费用。
    """

    MODEL_A = "fixture-model-a"
    MODEL_B = "fixture-model-b"
    LEDGER_NAME = "usage-share-backend.jsonl"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="console-share-usage-")
        self.addCleanup(self._tmp.cleanup)
        self.ledger_dir = os.path.join(self._tmp.name, "share-backend")
        os.makedirs(self.ledger_dir)
        # Rev8 B8-6：费用读面把每个 Key 的白名单名称/删除状态从管理库投影
        # 进来，所以管理库也必须重定向到夹具（否则会读真实 share-store.db）；
        # 夹具账本的 key_id 就用夹具管理库真正生成的 id。
        self.share_db = os.path.join(self._tmp.name, "share-store.db")
        self._orig_share_store = os.environ.get("SHARE_STORE_FILE")
        self._orig_share_manager = usagesrv._share_manager
        usagesrv._share_manager = None
        os.environ["SHARE_STORE_FILE"] = self.share_db
        self.addCleanup(self._restore_share_env)
        store = share_store.ShareStore(self.share_db)
        try:
            self.key_a = store.create_key("甲钥匙")["key_id"]
            self.key_b = store.create_key("乙钥匙")["key_id"]
            store.delete_key(self.key_b)
        finally:
            store.close()
        self.ledger = os.path.join(self.ledger_dir, self.LEDGER_NAME)
        with open(self.ledger, "w", encoding="utf-8") as handle:
            for rec in self._ledger_records():
                handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
        env = mock.patch.dict(os.environ,
                             {"CONSOLE_SHARE_BACKEND_DIR": self.ledger_dir})
        env.start()
        self.addCleanup(env.stop)

    def _restore_share_env(self):
        usagesrv._share_manager = self._orig_share_manager
        if self._orig_share_store is None:
            os.environ.pop("SHARE_STORE_FILE", None)
        else:
            os.environ["SHARE_STORE_FILE"] = self._orig_share_store

    def _ledger_records(self):
        """夹具账本：已归因（上游用量 / 本地估算）、无价、未归因各一条。"""
        base = {
            "service": "chat2api", "stream": False, "status": "ok",
            "cached_input_tokens": 0, "reasoning_tokens": 0,
            "duration_ms": 1000, "protocol": "chat", "attribution": "verified",
        }
        return [
            {**base, "time": "2026-09-25T10:00:00+08:00",
             "model": self.MODEL_A, "input_tokens": 1000000,
             "output_tokens": 0, "total_tokens": 1000000, "estimated": False,
             "usage_source": "upstream", "key_id": self.key_a, "request_id": "r1"},
            {**base, "time": "2026-09-25T11:00:00+08:00",
             "model": self.MODEL_A, "input_tokens": 1000000,
             "output_tokens": 0, "total_tokens": 1000000, "estimated": True,
             "usage_source": "estimated", "key_id": self.key_a, "request_id": "r2"},
            {**base, "time": "2026-09-25T12:00:00+08:00",
             "model": self.MODEL_B, "input_tokens": 10,
             "output_tokens": 5, "total_tokens": 15, "estimated": False,
             "usage_source": "upstream", "key_id": self.key_b, "request_id": "r3"},
            {**base, "time": "2026-09-25T13:00:00+08:00",
             # 7,000 input tokens × 1.0 USD/1M = 0.007 USD → × 7.2 = 0.0504 CNY；
             # 与两条 1.0 USD 记录合计 2.007 USD → 14.4504 CNY（逐条换算后相加）。
             "model": self.MODEL_A, "input_tokens": 7000, "output_tokens": 0,
             "total_tokens": 7000, "estimated": False, "usage_source": "upstream",
             "request_id": "r4"},
        ]

    @staticmethod
    def _key(payload, key_id):
        for row in payload["keys"]:
            if row["key_id"] == key_id:
                return row
        raise AssertionError(f"key {key_id} missing from payload")

    def test_results_are_counted_without_folding_unknown_into_success(self):
        # D18-2/C8: the chart's outcome counts come from the SAME filtered
        # ledger read; an unknown outcome is its own bucket, never success.
        status, raw = self.req("GET", "/api/console/share-usage")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["results"], {
            "success": 4, "failed": 0, "cancelled": 0, "unknown": 0,
            "total": 4, "coverage": {"unknownResult": 0}})

    def test_local_read_is_credential_free_and_matches_the_management_route(self):
        # checkedAt 就是「本次读取的时刻」：秒级冻结后三次请求逐字段可比，
        # 断言不必排除任何字段。
        with mock.patch.object(share_metering, "datetime", _FrozenDatetime):
            status, console_raw = self.req("GET", "/api/console/share-usage")
            self.assertEqual(status, 200)
            # 前端统一走 /gw/usagesrv 别名（vite dev 代理与 8001 生产同一路径）。
            status, alias_raw = self.req(
                "GET", "/gw/usagesrv/api/console/share-usage")
            self.assertEqual(status, 200)
            # share 管理面：同一份载荷，但本机普通页面拿不到（需管理令牌）。
            status, anon_raw = self.req("GET", "/api/share/usage")
            self.assertEqual(status, 401)
            status, management_raw = self.req(
                "GET", "/api/share/usage",
                headers={"X-Admin-Control": _CONTROL_TOKEN})
            self.assertEqual(status, 200)
        console_payload = json.loads(console_raw)
        self.assertEqual(json.loads(alias_raw), console_payload)
        self.assertEqual(console_payload, json.loads(management_raw))
        self.assertEqual(sorted(console_payload),
                         ["checkedAt", "coverage", "filters", "keys",
                          "requests", "results", "unattributed"])
        self.assertEqual(console_payload["filters"], {
            "window": "all", "key": None, "model": None, "result": None})
        # 载荷有内容：已知 / 估算 / 无价 / 未归因分列，绝不折零。
        self.assertEqual(console_payload["requests"], 4)
        coverage = console_payload["coverage"]
        self.assertEqual(coverage["ledger"], self.LEDGER_NAME)
        self.assertIs(coverage["ledgerPresent"], True)
        self.assertIs(coverage["ledgerReadable"], True)
        self.assertEqual(coverage["attributed"], 3)
        self.assertEqual(coverage["unattributed"], 1)
        # 人民币口径：计价器是六元组（带每条记录所属版本的汇率）。
        # 有记录无法折算（key_b 的无价行）→ 整页金额是下界，不是确定总额。
        key_a = self._key(console_payload, self.key_a)
        self.assertEqual(key_a["requests"], 2)
        # 旧形状价表只有恒生效的默认段（7.2）：已知/估算各自逐条换算。
        # B8-6：白名单名称投影——活动 Key 名字可读、deleted=False。
        self.assertEqual(key_a["keyName"], "甲钥匙")
        self.assertIs(key_a["deleted"], False)
        key_b = self._key(console_payload, self.key_b)
        # 无价记录不折 0：人民币总额未知且显式标 partial；唯一记录是
        # upstream，估算侧没有记录，因此 estimatedCostCny 是真实 0.0。
        # 删除过的 Key：名字仍可读，deleted=True；密钥明文/密文/哈希不在响应里。
        self.assertEqual(key_b["keyName"], "乙钥匙")
        self.assertIs(key_b["deleted"], True)
        raw_text = json.dumps(console_payload, ensure_ascii=False)
        self.assertNotIn("secret", raw_text)
        self.assertNotIn("um-share-", raw_text)
        self.assertEqual(console_payload["unattributed"]["requests"], 1)
        self.assertNotIn("keyName", console_payload["unattributed"])
        self.assertEqual(coverage["keyNames"], 2)

    def test_usage_read_never_assembles_management_or_opens_broker(self):
        # 每次拒绝都记进 _ISOLATION_ATTEMPTS：若路由把异常吞掉，入口仍会
        # 因累计 attempts 失败（与 58-63 行的设计一致），证据不再只剩
        # assert_not_called 的连接错误。
        def _refuse_assembly(*args, **kwargs):
            _ISOLATION_ATTEMPTS.append("management_assembled")
            raise AssertionError("management assembled")

        def _refuse_deployment(*args, **kwargs):
            _ISOLATION_ATTEMPTS.append("production_deployment_loader")
            raise AssertionError("deployment read")

        before = Path(self.share_db).read_bytes()
        with mock.patch.object(usagesrv, "get_share_manager",
                               side_effect=_refuse_assembly) as manager,              mock.patch.object(usagesrv, "_load_share_management_deployment",
                               side_effect=_refuse_deployment) as deployment:
            status, raw = self.req("GET", "/api/console/share-usage")
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(self._key(payload, self.key_a)["keyName"], "甲钥匙")
        self.assertEqual(self._key(payload, self.key_b)["keyName"], "乙钥匙")
        self.assertIs(self._key(payload, self.key_b)["deleted"], True)
        manager.assert_not_called()
        deployment.assert_not_called()
        self.assertEqual(Path(self.share_db).read_bytes(), before)

    def test_unknown_names_and_unreadable_store_stay_unknown(self):
        """未知 key_id / 读不到管理库：名称如实未知（None），不是空名或 False。"""
        real_reader = usagesrv.share_usage_reader()[0]
        self.assertIsNotNone(real_reader)

        def reader(ledger, *, limit=200000):
            payload = real_reader(ledger, limit=limit)
            payload["keys"].append({"key_id": "k-unknown", "requests": 1})
            return payload

        with mock.patch.object(usagesrv, "share_usage_reader",
                               return_value=(reader, None)):
            status, raw = self.req("GET", "/api/console/share-usage")
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        unknown = self._key(payload, "k-unknown")
        self.assertIsNone(unknown["keyName"])
        self.assertIsNone(unknown["deleted"])
        self.assertEqual(payload["coverage"]["keyNames"], 2)
        # 管理库读不到：全部名称如实未知（不是空名/False），coverage 记下状态。
        with mock.patch.dict(os.environ, {
                "SHARE_STORE_FILE": os.path.join(self._tmp.name,
                                                  "missing-store.db")}):
            status, raw = self.req("GET", "/api/console/share-usage")
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertIsNone(self._key(payload, self.key_a)["keyName"])
        self.assertIsNone(self._key(payload, self.key_a)["deleted"])
        self.assertIsNone(payload["coverage"]["keyNames"])

    def test_route_answers_before_the_console_manager_assembly(self):
        # 排在 console-manager 装配之前：账本读取不依赖运行态子系统，
        # 子系统起不来也不该把这个只读口变成 503。
        with mock.patch.object(usagesrv, "get_console_manager",
                               side_effect=RuntimeError("subsystem down")):
            status, raw = self.req("GET", "/api/console/share-usage")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["requests"], 4)

    def test_write_methods_and_subpaths_never_reach_the_reader(self):
        calls = []

        def reader(ledger, *, limit=200000):
            calls.append(ledger)
            return {"requests": 0, "keys": [], "unattributed": {},
                    "checkedAt": "2026-09-25T12:00:00+00:00", "coverage": {}}

        with mock.patch.object(usagesrv, "share_usage_reader",
                              return_value=(reader, None)):
            for method in ("POST", "PATCH", "DELETE"):
                status, raw = self.req(method, "/api/console/share-usage",
                                       body={"unexpected": True})
                self.assertEqual(status, 404, method)
                self.assertEqual(json.loads(raw)["error"], "unknown path", method)
            status, raw = self.req("GET", "/api/console/share-usage/extra")
            self.assertEqual(status, 404)
            self.assertEqual(json.loads(raw)["error"], "unknown path")
        self.assertEqual(calls, [])

    def test_foreign_origin_and_forged_host_are_rejected(self):
        status, _ = self.req("GET", "/api/console/share-usage",
                             headers={"Origin": "http://127.0.0.1:9999"})
        self.assertEqual(status, 403)
        status, _ = self.req("GET", "/api/console/share-usage",
                             headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.req("GET", "/api/console/share-usage",
                             headers={"Origin": "null"})
        self.assertEqual(status, 403)

    def test_missing_share_metering_reports_an_error_not_zero(self):
        # 模块缺席不是「零用量」：503 错误码原样透出，绝不返回 0 请求载荷。
        with mock.patch.dict(sys.modules, {"share.metering": None}):
            status, raw = self.req("GET", "/api/console/share-usage")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw), {"error": "share_metering_unavailable"})

    def test_unresolvable_ledger_path_reports_an_error_not_zero(self):
        with mock.patch.dict(os.environ,
                             {"CONSOLE_SHARE_BACKEND_DIR": "relative/dir"}):
            status, raw = self.req("GET", "/api/console/share-usage")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw),
                         {"error": "share_ledger_path_unresolved"})


    def test_request_page_matches_management_and_paginates(self):
        status, console_raw = self.req(
            "GET", "/api/console/share-usage/requests?limit=2")
        self.assertEqual(status, 200)
        page = json.loads(console_raw)
        self.assertEqual(sorted(page), ["checkedAt", "coverage", "filters",
                                       "nextCursor", "rows", "truncated"])
        self.assertEqual([row["requestId"] for row in page["rows"]],
                         ["r4", "r3"])
        self.assertEqual(page["nextCursor"], "3")
        self.assertIs(page["truncated"], True)
        self.assertEqual(page["filters"], {
            "window": "all", "key": None, "model": None, "result": None})
        self.assertEqual(page["coverage"]["recordsMatched"], 4)
        self.assertEqual(page["coverage"]["recordsReturned"], 2)
        by_id = {row["requestId"]: row for row in page["rows"]}
        self.assertIsNone(by_id["r4"]["keyId"])
        self.assertIsNone(by_id["r4"]["keyName"])
        self.assertIsNone(by_id["r4"]["deleted"])
        self.assertEqual(by_id["r3"]["keyId"], self.key_b)
        self.assertEqual(by_id["r3"]["keyName"], "乙钥匙")
        self.assertIs(by_id["r3"]["deleted"], True)
        # 前端统一走 /gw/usagesrv 别名；管理面同一份载荷。
        status, alias_raw = self.req(
            "GET", "/gw/usagesrv/api/console/share-usage/requests?limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(alias_raw), page)
        status, management_raw = self.req(
            "GET", "/api/share/usage/requests?limit=2",
            headers={"X-Admin-Control": _CONTROL_TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(management_raw), page)
        # 第二页只含更旧记录；到底后 nextCursor 为 null。
        status, second_raw = self.req(
            "GET", "/api/console/share-usage/requests?limit=2&cursor=3")
        self.assertEqual(status, 200)
        second = json.loads(second_raw)
        self.assertEqual([row["requestId"] for row in second["rows"]],
                         ["r2", "r1"])
        self.assertIsNone(second["nextCursor"])
        self.assertIs(second["truncated"], False)

    def test_request_page_filters_and_rejects_invalid_queries(self):
        status, raw = self.req(
            "GET", f"/api/console/share-usage/requests?key={self.key_a}")
        self.assertEqual(status, 200)
        self.assertEqual([row["requestId"] for row in json.loads(raw)["rows"]],
                         ["r2", "r1"])
        status, raw = self.req(
            "GET", f"/api/console/share-usage/requests?model={self.MODEL_B}")
        self.assertEqual(status, 200)
        self.assertEqual([row["requestId"] for row in json.loads(raw)["rows"]],
                         ["r3"])
        status, raw = self.req(
            "GET", "/api/console/share-usage/requests?result=failed")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["rows"], [])
        for query in ("limit=0", "limit=201", "window=2d", "cursor=x",
                       "unknown=1", "key=a&key=b"):
            with self.subTest(query=query):
                status, raw = self.req(
                    "GET", "/api/console/share-usage/requests?" + query)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(raw)["error"],
                                 "invalid_share_usage_query")

    def test_request_page_write_methods_are_rejected(self):
        for method in ("POST", "PATCH", "DELETE"):
            with self.subTest(method=method):
                status, raw = self.req(
                    method, "/api/console/share-usage/requests",
                    body={"unexpected": True})
                self.assertEqual(status, 404)
                self.assertEqual(json.loads(raw)["error"], "unknown path")

    def test_request_page_missing_metering_reports_an_error_not_empty(self):
        with mock.patch.dict(sys.modules, {"share.metering": None}):
            status, raw = self.req(
                "GET", "/api/console/share-usage/requests")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw), {"error": "share_metering_unavailable"})


class ConsoleCloudOpsRouteTests(ConsoleApiTestBase):
    """r14/D18-11: the cloud-ops read face is security-only."""

    def test_cloud_ops_route_returns_the_security_read_face(self):
        payload = {
            "available": True,
            "read_at": 1790000000,
            "security": {"configured": True, "available": True,
                          "generation_floor": 3, "records": 0,
                          "pending_remote": 0, "exported_at": None},
            "reasons": [],
            "unresolved": {"remote_export_confirmed": "not_checked_here",
                            "runtime_generation_ack": "not_checked_here"},
        }
        with mock.patch.object(usagesrv, "cloud_ops_status",
                               return_value=payload) as read:
            status, raw = self.req("GET", "/api/console/cloud-ops")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), payload)
        read.assert_called_once_with()

    def test_cloud_ops_route_is_read_only(self):
        status, raw = self.req("POST", "/api/console/cloud-ops",
                               body={"action": "resume"})
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(raw)["error"], "unknown path")
        status, raw = self.req("GET", "/api/console/cloud-ops/extra")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(raw)["error"], "unknown path")


class ConsoleShareSettingsRouteTests(ConsoleApiTestBase):
    """r14: the console facade can adjust the configured global admission cap."""

    class _Manager:
        def __init__(self):
            self.calls = []

        def set_share_limits(self, value, expected_share_max=None):
            self.calls.append((value, expected_share_max))
            return {"share_max_concurrency": value,
                    "expected_share_max": expected_share_max}

    def test_settings_patch_is_validated_and_gated(self):
        manager = self._Manager()
        with mock.patch.object(usagesrv, "get_share_manager",
                               return_value=manager):
            status, _ = self.req(
                "PATCH", "/api/console/share-management/settings",
                body={"share_max_concurrency": 64})
            self.assertEqual(status, 403)
            self.assertEqual(manager.calls, [])
            status, raw = self.req(
                "PATCH", "/api/console/share-management/settings",
                body={"share_max_concurrency": 64,
                      "expected_share_max": 32},
                headers={"X-Requested-With": "XMLHttpRequest"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)["share_max_concurrency"], 64)
            self.assertEqual(manager.calls, [(64, 32)])
            # D18-4: the global admission cap is 1..64; the old 128 is
            # rejected, and 65 is the first rejected value above the cap.
            for bad in (0, 65, 128, True, "64"):
                with self.subTest(value=bad):
                    status, _ = self.req(
                        "PATCH", "/api/console/share-management/settings",
                        body={"share_max_concurrency": bad},
                        headers={"X-Requested-With": "XMLHttpRequest"})
                    self.assertEqual(status, 400)
            status, _ = self.req(
                "PATCH", "/api/console/share-management/settings",
                body={"share_max_concurrency": 64, "url": "http://evil"},
                headers={"X-Requested-With": "XMLHttpRequest"})
            self.assertEqual(status, 400)


class EnvironmentRestorationTests(unittest.TestCase):
    """standards-tests-04: the class-level install must be restored.

    ``ConsoleApiTestBase.setUpClass`` installed a process-wide
    ``console_stats`` environment and never restored it, so every later module of
    the same ``discover`` run inherited an environment whose paths pointed into
    an already deleted temp directory. This case pins the restore by running the
    class lifecycle in a child process.
    """

    def test_the_class_restores_the_process_environment(self):
        code = "\n".join([
            "import json, os, sys, unittest",
            "sys.path[:0] = [%r, %r]",
            "from console import stats as console_stats",
            "before = console_stats._STATS_ENVIRONMENT",
            "import test_console_api as module",
            "suite = unittest.TestSuite([",
            "    module.ConsoleReadTests(",
            "        'test_console_fully_open_cross_origin_gated')])",
            "result = unittest.TextTestRunner(verbosity=0).run(suite)",
            "print('R19_ENV:', json.dumps({",
            "    'ok': result.wasSuccessful(),",
            "    'before_is_none': before is None,",
            "    'after_is_none': console_stats._STATS_ENVIRONMENT is None}))",
        ]) % (str(Path(__file__).resolve().parent),
              str(_USAGESRV_PATH.parent))
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code],
            capture_output=True, text=True, timeout=300,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(
            [line for line in completed.stdout.splitlines()
             if line.startswith("R19_ENV:")][-1].split("R19_ENV:", 1)[1])
        self.assertTrue(payload["ok"], completed.stdout[-2000:])
        self.assertTrue(payload["before_is_none"], payload)
        self.assertTrue(payload["after_is_none"],
                        "the class left a process-wide environment installed: "
                        "%r" % (payload,))


class RegistryHonestyTests(ConsoleApiTestBase):
    def test_production_registry_enablement_is_explicit(self):
        infos = usagesrv.get_console_manager().action_info()
        # B0 (2026-11-01): retired M365 / Qoder CN / Qoder Global /
        # CLIProxy management actions are absent. Ticket #32 retires the
        # model sync/settings/task actions and #33 retires the local share
        # chain: model.catalog, the context probe and the context overrides
        # are the only model actions left.
        self.assertEqual(len(infos), 12)
        by_name = {i["action"]: i for i in infos}
        enabled = {n for n, i in by_name.items() if i["enabled"]}
        # The deployment authorization package (2026-09-16, section 5) plus
        # user instruction A3 flipped the approved actions live: the service
        # start/stop
        # target (chat2api only after B0 — usage stays excluded as the
        # console's own web host), the UM login orchestration, since the
        # maintainer's 2026-09-22 budget approval the billed long-input
        # context probe RUN within its registered caps (≤20 requests,
        # ≤64 tokens), and since Plan-Revision 7 step 6 the state-only
        # context override set/clear.
        self.assertEqual(
            enabled,
            {"account.um_status", "account.um_verify", "account.login",
             "model.catalog", "model.context_override_clear",
             "model.context_override_set", "model.context_overrides",
             "model.context_probe_preview", "model.context_probe_run",
             "service.restart", "service.start",
             "service.stop"})
        self.assertEqual(len(enabled), 12)
        self.assertEqual(len(infos) - len(enabled), 0)
        for retired in (
                "model.sync_preview", "model.sync_pending",
                "model.sync_history", "model.sync_apply",
                "model.settings_save", "model.settings_preview",
                "model.settings_apply",
                "model.task_status", "model.task_install",
                "model.task_disable",
                "qoder.credential_bridge", "qoder.source_diagnostics",
                "qoder.quota_read", "qoder.status", "qoder.models",
                "qoder.poc", "qoderglobal.status", "qoderglobal.models",
                "qoderglobal.quota", "qoderglobal.login",
                "cliproxy.status"):
            self.assertNotIn(retired, by_name)
        for name, info in by_name.items():
            if info["enabled"]:
                self.assertIsNone(info["disabled_reason"], info)
                self.assertTrue(info["description"], info)
            else:
                self.assertTrue(info["disabled_reason"], info)

    def test_new_actions_registered_with_expected_enablement(self):
        infos = usagesrv.get_console_manager().action_info()
        by_name = {i["action"]: i for i in infos}
        # UM model catalog stays enabled with a description; the retired
        # Qoder Global read-only trio and login launcher are gone.
        name = "model.catalog"
        self.assertIn(name, by_name)
        self.assertTrue(by_name[name]["enabled"], name)
        self.assertTrue(by_name[name]["description"], name)
        self.assertIn("只读", by_name[name]["description"])
        for retired in ("qoderglobal.status", "qoderglobal.models",
                        "qoderglobal.quota", "qoderglobal.login"):
            self.assertNotIn(retired, by_name)
        # Ticket #32: the model sync/settings/task actions are gone; the
        # catalog, context probe and context overrides stay registered.
        for retired in ("model.sync_preview", "model.sync_pending",
                        "model.sync_history", "model.sync_apply",
                        "model.settings_save", "model.settings_preview",
                        "model.settings_apply", "model.task_status",
                        "model.task_install", "model.task_disable"):
            self.assertNotIn(retired, by_name)
        for kept in ("model.catalog", "model.context_probe_preview",
                     "model.context_probe_run", "model.context_overrides",
                     "model.context_override_set",
                     "model.context_override_clear"):
            self.assertIn(kept, by_name)
            self.assertTrue(by_name[kept]["enabled"], kept)

    def test_um_verify_enabled_after_endpoint_live(self):
        # B-2 (2026-09-20): the RUNNING production chat2api instance ships
        # GET /admin/auth/verify (live probe answered 200 with a valid
        # verify payload on 2026-09-20), so the pending-endpoint blocker is
        # gone and the registry action is enabled. This suite keeps zero
        # real HTTP: submit-level behavior is covered by the isolated stub
        # tests, so here we only lock the registry fact.
        infos = usagesrv.get_console_manager().action_info()
        spec = {i["action"]: i for i in infos}["account.um_verify"]
        self.assertTrue(spec["enabled"])
        self.assertIsNone(spec["disabled_reason"])
        self.assertTrue(spec["description"])

    def test_unauthorized_target_refused_at_submit(self):
        # Defense in depth made earliest-boundary: the policy sets do NOT
        # contain "usage" (it is the console's own web host), so the
        # resource function refuses at SUBMIT time — no operation row is
        # ever created for a refused target (提交即 OperationUnavailable).
        status, raw = self.req("POST", "/api/console/operations",
                               body={"action": "service.start",
                                     "target": "usage"},
                               headers={"X-Admin-Control": _CONTROL_TOKEN})
        data = json.loads(raw)
        self.assertEqual(status, 403)
        self.assertEqual(data["error"], "action_disabled")
        self.assertIn("授权包", data["reason"])
        self.assertIn("宿主", data["reason"])
        status, raw = self.req("POST", "/api/console/operations",
                               body={"action": "service.stop",
                                     "target": "usage"},
                               headers={"X-Admin-Control": _CONTROL_TOKEN})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(raw)["error"], "action_disabled")

    def test_stats_invalid_range_400(self):
        status, raw = self.req("GET", "/api/console/stats?from=2099-01-10&to=2099-01-01",
                               headers={"X-Admin-Control": _CONTROL_TOKEN})
        self.assertEqual(status, 400)
        data = json.loads(raw)
        self.assertEqual(data["error"], "invalid_stats_query")

    def test_stats_valid_query_with_fixture_ledgers(self):
        from console import stats as console_stats
        fixture = console_stats.bounded_stats(
            {}, "2099-01-01", "2099-01-02")
        # No ledgers: coverage absent, empty days — honest empty, not zeros.
        self.assertEqual(fixture["days"], [])
        with mock.patch.object(console_stats, "default_ledgers",
                               return_value={}):
            status, raw = self.req(
                "GET", "/api/console/stats?from=2099-01-01&to=2099-01-02",
                headers={"X-Admin-Control": _CONTROL_TOKEN})
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertEqual(data["days"], [])
        self.assertIn("coverage", data)
        self.assertIn("meta", data)

    def test_unknown_action_404(self):
        status, raw = self.req("POST", "/api/console/operations",
                               body={"action": "no.such"},
                               headers={"X-Admin-Control": _CONTROL_TOKEN})
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(raw)["error"], "invalid_action")

    def test_unknown_fields_rejected(self):
        status, raw = self.req("POST", "/api/console/operations",
                               body={"action": "service.start", "junk": 1},
                               headers={"X-Admin-Control": _CONTROL_TOKEN})
        self.assertEqual(status, 400)
        self.assertIn("unknown field", json.loads(raw)["error"])


class ConsoleWriteBoundaryTests(ConsoleApiTestBase):
    def test_cookie_write_reaches_registry_policy(self):
        # R9-R35: console writes are open-and-use locally (no credential or
        # CSRF gate); the REGISTRY policy layer still gates real targets —
        # "usage" is outside both allow-sets and is refused at submit time.
        sid = self.exchange_session()
        status, raw = self.req("POST", "/api/console/operations",
                               body={"action": "service.stop",
                                     "target": "usage"},
                               headers={"Cookie": sid})
        self.assertEqual(status, 403)
        data = json.loads(raw)
        self.assertEqual(data["error"], "action_disabled")
        self.assertIn("宿主", data["reason"])

    def test_alias_write_boundary_unchanged(self):
        # The /gw/usagesrv alias is a pure path rewrite: the write boundary
        # (session exchange refusal, console submit validation) behaves
        # identically through the alias.
        status, _ = self.req("POST", "/gw/usagesrv/api/session",
                             body={"code": "nope"})
        self.assertEqual(status, 403)
        status, raw = self.req("POST", "/gw/usagesrv/api/console/operations",
                               body={"action": "no.such", "target": "x"})
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(raw)["error"], "invalid_action")

    def test_cookie_write_with_csrf_still_gated_by_registry(self):
        sid = self.exchange_session()
        status, raw = self.req("POST", "/api/console/operations",
                               body={"action": "service.stop",
                                     "target": "usage"},
                               headers={"Cookie": sid,
                                        "X-Requested-With": "XMLHttpRequest"})
        data = json.loads(raw)
        self.assertEqual(status, 403)
        self.assertEqual(data["error"], "action_disabled")
        # CSRF alone never bypasses the policy: "usage" is outside both
        # allow-sets and is refused at submit time.
        self.assertIn("宿主", data["reason"])

    def test_retired_source_action_is_unknown_and_creates_no_row(self):
        # B0: retired-source actions are not registered and have no dedicated
        # gate any more, so the operation manager answers invalid_action and
        # no handler runs and no operation row is created.
        for action in ("qoder.status", "qoderglobal.status",
                       "cliproxy.status", "m365.settings_save"):
            with self.subTest(action=action):
                status, raw = self.req("POST", "/api/console/operations",
                                       body={"action": action,
                                             "target": ""})
                self.assertEqual(status, 404)
                self.assertEqual(json.loads(raw)["error"], "invalid_action")
        status, raw = self.req("GET", "/api/console/operations")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["operations"], [])

    def test_cancel_unknown_operation_404(self):
        status, raw = self.req("POST", "/api/console/operations/zzz/cancel",
                               headers={"X-Admin-Control": _CONTROL_TOKEN})
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(raw)["error"], "operation_not_found")

    def test_body_over_transport_bound_413(self):
        status, raw = self.req("POST", "/api/console/operations",
                               body={"action": "service.start",
                                     "params": {"x": "y" * 70000}},
                               headers={"X-Admin-Control": _CONTROL_TOKEN})
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(raw)["error"], "request_too_large")

    def test_operations_list_empty_and_bounded(self):
        status, raw = self.req("GET", "/api/console/operations?limit=9999",
                               headers={"X-Admin-Control": _CONTROL_TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["operations"], [])


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — stdlib handler naming
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the test output clean
        pass


class QuotaSnapshotEndpointTests(ConsoleApiTestBase):
    """B0 (2026-11-01): the retired source quota read face is removed.

    The retired model source is gone, and its payload builder plus route are
    gone with it: the path answers the same unknown-path 404 as any other
    unregistered path, never probes a retired port and never reads an old token
    file or a stale success cache. ``?refresh=1`` changes nothing.
    """

    def test_quota_snapshot_endpoint_is_gone(self):
        for query in ("", "?refresh=1"):
            with self.subTest(query=query):
                status, raw = self.req(
                    "GET", "/api/console/quota-snapshot" + query)
                self.assertEqual(status, 404)
                self.assertEqual(json.loads(raw)["error"], "unknown path")


class ConsoleHealthEndpointTests(ConsoleApiTestBase):
    """R11-6/B2: server-side loopback probes answer through THIS origin."""

    def setUp(self):
        usagesrv._console_health_cache["payload"] = None
        usagesrv._console_health_cache["at"] = 0.0
        self.probe_server = usagesrv.ThreadingHTTPServer(("127.0.0.1", 0),
                                                         _OkHandler)
        self.probe_port = self.probe_server.server_address[1]
        self.probe_thread = threading.Thread(
            target=self.probe_server.serve_forever, daemon=True)
        self.probe_thread.start()
        # LIFO cleanup order: shutdown() must run BEFORE thread.join() —
        # registering join first makes it the LAST cleanup, so the server loop is
        # already stopping when join waits on it (the reverse order deadlocks).
        self.addCleanup(self.probe_thread.join)
        self.addCleanup(self.probe_server.server_close)
        self.addCleanup(self.probe_server.shutdown)
        self._saved_probes = usagesrv._CONSOLE_HEALTH_PROBES
        usagesrv._CONSOLE_HEALTH_PROBES = (
            {"id": "fake-live", "port": self.probe_port, "path": "/healthz"},
        )
        self.addCleanup(setattr, usagesrv, "_CONSOLE_HEALTH_PROBES",
                        self._saved_probes)

    def test_default_probe_and_log_chain_are_um_only(self):
        # B0 (2026-11-01): the default assembly probes only the UM main
        # chain (8000) and reads only the UM log ledger. The retired source
        # ports (8095 / 8097 / 8320) are never probed or read.
        probes = self._saved_probes
        self.assertEqual([probe["id"] for probe in probes], ["chat2api"])
        self.assertEqual([probe["port"] for probe in probes], [8000])
        for retired in (8095, 8097, 8320):
            self.assertNotIn(retired, [probe["port"] for probe in probes])
        self.assertEqual(set(usagesrv._console_log_readers()), {"chat2api"})

    def test_health_classifies_listening_and_not_listening(self):
        status, raw = self.req("GET", "/api/console/health")
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        services = {s["id"]: s for s in payload["services"]}
        self.assertEqual(services["fake-live"]["state"], "listening")
        self.assertEqual(services["fake-live"]["http_status"], 200)
        self.assertGreaterEqual(services["fake-live"]["ms"], 0)
        # The console host itself is always the second, trivially alive.
        self.assertEqual(services["usagesrv"]["state"], "listening")
        self.assertEqual(len(payload["services"]), 2)

    def test_probe_classification_refused_vs_timeout(self):
        # Deterministic classification of the two TCP outcomes the contract
        # distinguishes: a REFUSED connect = not_listening; a TIMEOUT (this
        # machine's firewall turns closed-loopback SYNs into timeouts) = the
        # honest "unknown", never a fabricated not_listening.
        import socket as _socket
        with mock.patch.object(_socket, "create_connection",
                               side_effect=ConnectionRefusedError(
                                   10061, "refused")):
            refused = usagesrv._probe_service(
                {"id": "x", "port": 9, "path": "/"})
        self.assertEqual(refused["state"], "not_listening")
        with mock.patch.object(_socket, "create_connection",
                               side_effect=_socket.timeout("t")):
            timed_out = usagesrv._probe_service(
                {"id": "x", "port": 9, "path": "/"})
        self.assertEqual(timed_out["state"], "unknown")
        self.assertIn("超时", timed_out["note"])

    def test_health_ttl_cache_amortizes_probes(self):
        status, raw = self.req("GET", "/api/console/health")
        first_checked = json.loads(raw)["checked_at"]
        status, raw = self.req("GET", "/api/console/health")
        second = json.loads(raw)
        self.assertTrue(second["cached"])
        self.assertEqual(second["checked_at"], first_checked)

    def test_health_tcp_only_and_non_200_count_as_alive(self):
        # TCP-only spec (path None): listening with NO HTTP request issued at
        # all — the branch that keeps the chat2api probe free of login side
        # effects. Non-200 answer still counts as alive (service answered).
        import socket as _socket
        raw_sock = _socket.socket()
        raw_sock.bind(("127.0.0.1", 0))
        raw_sock.listen(1)
        self.addCleanup(raw_sock.close)
        tcp_port = raw_sock.getsockname()[1]

        class _ErrHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 — stdlib handler naming
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        err = usagesrv.ThreadingHTTPServer(("127.0.0.1", 0), _ErrHandler)
        err_thread = threading.Thread(target=err.serve_forever, daemon=True)
        err_thread.start()
        # LIFO cleanup order: shutdown before join (see ConsoleHealth... setUp).
        self.addCleanup(err_thread.join)
        self.addCleanup(err.server_close)
        self.addCleanup(err.shutdown)
        usagesrv._CONSOLE_HEALTH_PROBES = (
            {"id": "tcp-only", "port": tcp_port, "path": None},
            {"id": "http-500", "port": err.server_address[1], "path": "/x"},
        )
        usagesrv._console_health_cache["payload"] = None
        usagesrv._console_health_cache["at"] = 0.0
        status, raw = self.req("GET", "/api/console/health")
        self.assertEqual(status, 200)
        services = {s["id"]: s for s in json.loads(raw)["services"]}
        self.assertEqual(services["tcp-only"]["state"], "listening")
        self.assertIsNone(services["tcp-only"]["http_status"])
        self.assertIn("TCP", services["tcp-only"]["note"])
        self.assertEqual(services["http-500"]["state"], "listening")
        self.assertEqual(services["http-500"]["http_status"], 500)


class ConsoleModelScopeContractTests(ConsoleApiTestBase):
    """Ticket #32: the model sync/settings/task actions are retired.

    The old scope/selection contract belonged to model.sync_preview/apply; both
    action ids now follow the existing unknown-action contract (404
    invalid_action) and create no operation row. The kept model actions stay
    registered with their exact schemas.
    """

    RETIRED = (
        "model.sync_preview", "model.sync_pending",
        "model.sync_history", "model.sync_apply",
        "model.settings_save", "model.settings_preview",
        "model.settings_apply",
        "model.task_status", "model.task_install",
        "model.task_disable",
    )
    KEPT = (
        "model.catalog", "model.context_probe_preview",
        "model.context_probe_run", "model.context_overrides",
        "model.context_override_set", "model.context_override_clear",
    )

    def _submit(self, action, params=None):
        body = {"action": action, "target": "model"}
        if params is not None:
            body["params"] = params
        status, raw = self.req("POST", "/api/console/operations", body=body,
                              headers={"X-Admin-Control": _CONTROL_TOKEN})
        return status, json.loads(raw)

    def _operation_count(self):
        status, raw = self.req("GET", "/api/console/operations?limit=200",
                              headers={"X-Admin-Control": _CONTROL_TOKEN})
        self.assertEqual(status, 200)
        return len(json.loads(raw)["operations"])

    def test_retired_model_sync_actions_are_unknown_and_create_no_row(self):
        """退役动作按既有未知动作契约诚实拒绝（404 invalid_action）。"""
        for action in self.RETIRED:
            with self.subTest(action=action):
                before = self._operation_count()
                status, data = self._submit(action, {"scope_mode": "all"})
                self.assertEqual(status, 404, action)
                self.assertEqual(data["error"], "invalid_action", action)
                self.assertEqual(before, self._operation_count(),
                                 "拒绝不得入操作账本")

    def test_retired_model_actions_are_not_registered(self):
        specs = {spec.name: spec for spec in console_adapters.build_registry()}
        for action in self.RETIRED:
            self.assertNotIn(action, specs)

    def test_kept_model_actions_are_registered_with_their_schemas(self):
        specs = {spec.name: spec for spec in console_adapters.build_registry()}
        for kept in self.KEPT:
            self.assertIn(kept, specs)
        self.assertEqual(set(specs["model.catalog"].param_schema),
                         {"sources", "fresh"})
        probe_schema = specs["model.context_probe_preview"].param_schema
        self.assertEqual(set(probe_schema), {"sources", "models", "retest"})
        run_schema = specs["model.context_probe_run"].param_schema
        self.assertTrue(set(probe_schema) <= set(run_schema))
        self.assertEqual(set(run_schema),
                         {"sources", "models", "retest",
                          "max_requests", "max_tokens"})
        self.assertEqual(specs["model.context_probe_preview"].resource_fn,
                         specs["model.context_probe_run"].resource_fn)
        self.assertEqual(
            set(specs["model.context_override_set"].param_schema),
            {"source", "model_id", "context", "expected_set_at"})
        self.assertEqual(
            set(specs["model.context_override_clear"].param_schema),
            {"source", "model_id", "expected_set_at"})

    def test_disabled_action_keeps_machine_code_and_reason(self):
        """既有 403 语义不变：机器码 + reason 仍在（detail 不覆盖 reason）。"""
        status, data = self._submit("service.stop", None)
        self.assertEqual(status, 403)
        self.assertEqual(data["error"], "action_disabled")
        self.assertIn("授权包", data["reason"])
        self.assertNotIn("detail", data)

    def test_removed_pin_actions_follow_the_unknown_action_contract(self):
        """Rev8 B8-2（2026-10-02）：固定（pin）机制移除——旧动作名按既有未知
        动作契约诚实拒绝（404 invalid_action），不存在能写 pin 的旁路。"""
        for action in ("model.pin_add", "model.pin_remove"):
            status, raw = self.req(
                "POST", "/api/console/operations",
                body={"action": action,
                      "params": {"key": "um|DSH|openwebui|model-a"}},
                headers={"X-Admin-Control": _CONTROL_TOKEN})
            self.assertEqual(status, 404, action)
            self.assertEqual(json.loads(raw)["error"], "invalid_action", action)

    def test_unknown_parameter_is_still_refused(self):
        """不放宽校验：未知参数仍 400，且说明只点名参数、不回显路径。"""
        status, data = self._submit("model.catalog", {"bogus": True})
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "invalid_params")
        self.assertIn("bogus", data["detail"])
        self.assertNotIn("\\", data["detail"])


class ConsoleShareStatusAttributionTests(ConsoleApiTestBase):
    """owner 裁定 2026-09-26（同日复核重塑：一个布尔不得冒充三件事）：
    归因状态在控制台状态端点可见（加性）。

    只替换最远边界（运行态管理器与两个只读观察缝），零真实调用：字段
    在场且如实——文件未就绪报未就绪/带原因，实例是否载入与请求是否已
    验证归属各有明确名字，绝不折成「已接线」；状态来源改名（变异核对）
    时端点不得冒充「归因在场」。前端渲染仍是另一步。
    """

    NOT_READY = {
        "file_ready": False, "reason": "missing",
        "key_file": r"C:\ProgramData\UMShareRun\run\share-attribution.key",
        "key_chars": None,
        "instance_loaded": {"value": "unknown", "basis": "inference",
                            "rule": "gateway_process_start_time_vs_"
                                    "key_file_mtime",
                            "reason": "key_file_not_ready",
                            "gateway_pid": None, "gateway_started_at": None,
                            "key_file_mtime": None},
        "request_attribution": {
            "verdict": "not_observed_here",
            "runtime_fact": "backend_ledger_row_attribution_verified",
            "ledger_file": None}}

    def setUp(self):
        # 运行态管理器：最小 status/list_keys 形状（不触 SQLite/broker）。
        self.manager = SimpleNamespace(
            status=lambda: {"in_sync": True, "active_keys": 1,
                            "total_keys": 1, "broker": {}},
            list_keys=lambda: {"keys": []})
        patchers = [
            mock.patch.object(usagesrv, "get_share_manager",
                              lambda: self.manager),
            mock.patch.object(orch, "_BROKER_OPS", None),
            # The status read's capacity block is a SEPARATE read seam. A prior
            # module can leave it wired to a real broker; this class tests the
            # attribution shape, so keep it unwired instead of reading the live
            # process.
            mock.patch.object(orch, "_MAINTENANCE_STATUS", None),
            mock.patch.object(orch, "_share_entry_attribution",
                              lambda: dict(self.NOT_READY)),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _status(self):
        status, raw = self.req("GET", "/api/console/share-management/status")
        self.assertEqual(status, 200, raw)
        return json.loads(raw)

    def test_status_reports_attribution_not_ready_honestly(self):
        payload = self._status()
        self.assertEqual(payload["attribution"], self.NOT_READY)
        self.assertIs(payload["attribution"]["file_ready"], False)
        # 一个布尔不再冒充三件事：实例载入是 unknown，归属是运行期事实。
        self.assertEqual(payload["attribution"]["instance_loaded"]["value"],
                         "unknown")
        self.assertEqual(
            payload["attribution"]["request_attribution"]["verdict"],
            "not_observed_here")
        self.assertNotIn("wired", payload["attribution"])
        # 加性：既有字段的值与语义不变。
        self.assertIs(payload["in_sync"], True)

    def test_status_reports_file_ready_and_instance_not_loaded(self):
        ready = dict(self.NOT_READY, file_ready=True, reason="ok",
                      key_chars=40,
                      instance_loaded=dict(self.NOT_READY["instance_loaded"],
                                           value="no",
                                           reason="process_started_before_"
                                                  "key_mtime"))
        with mock.patch.object(orch, "_share_entry_attribution",
                              lambda: dict(ready)):
            payload = self._status()
        self.assertEqual(payload["attribution"], ready)
        self.assertIs(payload["attribution"]["file_ready"], True)
        self.assertEqual(payload["attribution"]["instance_loaded"]["value"],
                         "no")

    def test_renamed_state_source_is_not_reported_as_attribution(self):
        """变异核对：状态来源改名后端点不得冒充归因在场。"""
        with mock.patch.object(orch, "_share_entry_attribution",
                              lambda: {"attribution_state": {"file_ready": True}}):
            payload = self._status()
        self.assertNotIn("attribution", payload)

    def test_status_prefers_the_configured_public_origin(self):
        # D18-1: the explicit deployment origin is the address the console
        # labels and copies; the Tailscale Funnel read must not even run.
        with mock.patch.dict(os.environ, {
                "CONSOLE_PUBLIC_ORIGIN": "https://dacintapi.de5.net"}):
            payload = self._status()
        self.assertEqual(payload["share_url"], "https://dacintapi.de5.net")
        self.assertEqual(payload["share_url_source"], "configured")
        self.assertEqual(payload["share_url_reason"], "")

    def test_status_invalid_public_origin_never_falls_back(self):
        with mock.patch.dict(os.environ, {
                "CONSOLE_PUBLIC_ORIGIN": "https://dacintapi.de5.net/v1"}):
            payload = self._status()
        self.assertNotIn("share_url", payload)
        self.assertIsNone(payload["share_url_source"])
        self.assertEqual(payload["share_url_reason"], "public_origin_invalid")

    def test_status_unconfigured_origin_reports_the_reason(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONSOLE_PUBLIC_ORIGIN", None)
            payload = self._status()
        self.assertNotIn("share_url", payload)
        self.assertIsNone(payload["share_url_source"])
        self.assertEqual(payload["share_url_reason"],
                         "public_origin_unconfigured")


if __name__ == "__main__":
    unittest.main()
