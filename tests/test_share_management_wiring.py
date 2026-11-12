"""W3: usagesrv.get_share_manager management-deployment wiring tests.

Covers the explicit deployment-config consumption contract (§210.5/§214.2):
absent config / explicit disable -> NoopBroker; a present-but-corrupt config
never silently falls back to NoopBroker (BrokerError, structured 503 on the
API); the BrokerClient receives a per-exchange transport FACTORY bound to the
configured pipe; the control key is read from the key file (never inlined in
the config). Everything runs on isolated tmp paths; no live pipe is required.
"""
import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from share_broker_client import (BrokerClient, NamedPipeBrokerTransport)  # noqa: E402
from share_broker_protocol import DEFAULT_DEADLINE_SECONDS  # noqa: E402
from share_management import BrokerError, NoopBroker  # noqa: E402

_HERE = Path(__file__).resolve().parents[1]



# Guard before any test can construct a default management client. A route may
# swallow an exception, so attempted production access is checked separately.
_ISOLATION_ATTEMPTS = []
_ISOLATION_PATCHERS = []
_ISOLATION_MODULES = []
_ISOLATION_TEMP = None


def _refuse_real_kernel(*args, **kwargs):
    _ISOLATION_ATTEMPTS.append("real_named_pipe_kernel")
    raise AssertionError("offline test attempted a real named-pipe kernel")


def _guard_usage_module(module):
    if any(item[0] is module for item in _ISOLATION_MODULES):
        return
    root = Path(_ISOLATION_TEMP.name)
    _ISOLATION_MODULES.append((module, module._console_manager,
                               module._share_manager))
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
        _ISOLATION_PATCHERS.append(patcher)


def setUpModule():
    global _ISOLATION_TEMP, _ISOLATION_SAVED_SEAMS
    import share_broker_client
    import share_broker_server
    import console_share_actions
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
    local = globals().get("usagesrv")
    if local is not None:
        _guard_usage_module(local)
    _ISOLATION_SAVED_SEAMS = {
        name: value for name, value in vars(console_share_actions).items()
        if name.startswith("_") and name.upper() == name}
    for owner, name in ((share_broker_client.NamedPipeBrokerTransport,
                         "_declare_real_kernel"),
                        (share_broker_server, "_declared_kernel")):
        patcher = mock.patch.object(owner, name, _refuse_real_kernel)
        patcher.start()
        _ISOLATION_PATCHERS.append(patcher)


def tearDownModule():
    import console_share_actions
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
    if _ISOLATION_ATTEMPTS:
        raise AssertionError("management isolation breached: " +
                             repr(_ISOLATION_ATTEMPTS))


def _load_usagesrv():
    spec = importlib.util.spec_from_file_location(
        "usagesrv_wiring_test", _HERE / "usagesrv.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _guard_usage_module(module)
    return module


class _MissingPipeKernel:
    def CreateFileW(self, *args):
        return 0xFFFFFFFFFFFFFFFF

    def GetLastError(self):
        return 2  # ERROR_FILE_NOT_FOUND, injected before any real OS boundary.


class _MissingPipeTransport(NamedPipeBrokerTransport):
    def __init__(self, pipe_name, kernel=None):
        super().__init__(pipe_name, kernel=kernel or _MissingPipeKernel())


class WiringTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.usagesrv = _load_usagesrv()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True,
                                               prefix="share-wiring-",
                                               dir=_ISOLATION_TEMP.name)
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / "share-management.json"
        # 每个测试独立重置惰性单例：配置在首次构造时消费一次。
        with self.usagesrv._share_manager_lock:
            self.usagesrv._share_manager = None
        self.addCleanup(self._reset_manager)
        old_store = os.environ.get("SHARE_STORE_FILE")
        os.environ["SHARE_STORE_FILE"] = str(Path(self.tmp.name) / "share.db")
        self.addCleanup(self._restore_env, "SHARE_STORE_FILE", old_store)
        old_config = self.usagesrv.SHARE_MANAGEMENT_CONFIG_FILE
        self.usagesrv.SHARE_MANAGEMENT_CONFIG_FILE = str(self.config_path)
        self.addCleanup(setattr, self.usagesrv,
                        "SHARE_MANAGEMENT_CONFIG_FILE", old_config)
        import share_broker_client
        patcher = mock.patch.object(share_broker_client,
                                   "NamedPipeBrokerTransport", _MissingPipeTransport)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _reset_manager(self):
        with self.usagesrv._share_manager_lock:
            self.usagesrv._share_manager = None

    @staticmethod
    def _restore_env(key, old):
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old

    def write_config(self, payload):
        self.config_path.write_text(json.dumps(payload), encoding="utf-8")

    def write_key_file(self, content="w3-test-control-key-secret"):
        key = Path(self.tmp.name) / "broker-control-key"
        key.write_text(content, encoding="utf-8")
        return str(key)

    @staticmethod
    def valid_config(**overrides):
        cfg = {
            "schema_version": 1,
            "enabled": True,
            "pipe_name": r"\\.\pipe\um-w3-test-broker",
            "control_key_file": None,  # filled by caller
        }
        cfg.update(overrides)
        return cfg


class ConfigPresenceTests(WiringTestBase):
    def test_absent_config_is_noop(self):
        manager = self.usagesrv.get_share_manager()
        self.assertIsInstance(manager.broker, NoopBroker)

    def test_explicit_disabled_is_noop(self):
        self.write_config(self.valid_config(enabled=False,
                                            control_key_file="unused"))
        manager = self.usagesrv.get_share_manager()
        self.assertIsInstance(manager.broker, NoopBroker)


class CorruptConfigTests(WiringTestBase):
    """A present config that claims enabled=true must fail closed loudly —
    never degrade into a NoopBroker that pretends nothing is deployed."""

    def _assert_broker_error(self):
        with self.assertRaises(BrokerError) as ctx:
            self.usagesrv.get_share_manager()
        return ctx.exception

    def test_unparsable_json_is_broker_error(self):
        self.config_path.write_text("{not json", encoding="utf-8")
        self._assert_broker_error()

    def test_non_object_config_is_broker_error(self):
        self.config_path.write_text("[1, 2]", encoding="utf-8")
        self._assert_broker_error()

    def test_wrong_schema_is_broker_error(self):
        self.write_config(self.valid_config(schema_version=2,
                                            control_key_file="x"))
        self._assert_broker_error()

    def test_bool_schema_is_broker_error(self):
        # True == 1 陷阱：bool 形式的 schema_version 必须拒绝。
        self.write_config(self.valid_config(schema_version=True,
                                            control_key_file="x"))
        self._assert_broker_error()

    def test_missing_enabled_is_broker_error(self):
        cfg = self.valid_config(control_key_file="x")
        del cfg["enabled"]
        self.write_config(cfg)
        self._assert_broker_error()

    def test_non_bool_enabled_is_broker_error(self):
        self.write_config(self.valid_config(enabled=1,
                                            control_key_file="x"))
        self._assert_broker_error()

    def test_bad_pipe_name_is_broker_error(self):
        for pipe in ("", "127.0.0.1:9100", "\\\\.\\pipe\\", 123, None):
            with self.subTest(pipe=pipe):
                self.write_config(self.valid_config(pipe_name=pipe,
                                                    control_key_file="x"))
                self._assert_broker_error()

    def test_missing_key_path_is_broker_error(self):
        self.write_config(self.valid_config(control_key_file=None))
        self._assert_broker_error()

    def test_absent_key_file_is_broker_error(self):
        self.write_config(self.valid_config(
            control_key_file=str(Path(self.tmp.name) / "nope")))

    def test_oversized_key_file_is_broker_error(self):
        key = self.write_key_file("x" * 200)
        self.write_config(self.valid_config(control_key_file=key))
        self._assert_broker_error()


class ValidConfigTests(WiringTestBase):
    def test_valid_config_builds_broker_client_adapter(self):
        key = self.write_key_file()
        self.write_config(self.valid_config(control_key_file=key))
        manager = self.usagesrv.get_share_manager()
        self.assertEqual(manager.broker.name, "broker-client")
        client = manager.broker.client
        self.assertIsInstance(client, BrokerClient)
        # 密钥只来自密钥文件；配置文件不携带密钥本体。
        self.assertEqual(client._secret, "w3-test-control-key-secret")
        self.assertEqual(client._timeout, DEFAULT_DEADLINE_SECONDS)

    def test_factory_returns_fresh_transport_per_exchange(self):
        # §214.2: 首参必须是 transport 工厂——client.call 内部执行
        # factory().open()，传实例会破坏一次交换一连接的 R44-1 契约。
        key = self.write_key_file()
        self.write_config(self.valid_config(control_key_file=key))
        client = self.usagesrv.get_share_manager().broker.client
        t1 = client._factory()
        t2 = client._factory()
        self.assertIsNot(t1, t2)
        self.assertIsInstance(t1, NamedPipeBrokerTransport)
        self.assertEqual(t1.pipe_name, r"\\.\pipe\um-w3-test-broker")

    def test_call_without_broker_reports_not_installed_quickly(self):
        # An injected Win32 facade returns ERROR_FILE_NOT_FOUND (2); the
        # real transport/client failure path runs without opening an OS pipe.
        key = self.write_key_file()
        self.write_config(self.valid_config(control_key_file=key))
        client = self.usagesrv.get_share_manager().broker.client
        started = time.monotonic()
        result = client.status()
        elapsed = time.monotonic() - started
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "not_installed")
        self.assertLess(elapsed, 8.0)


class ApiRouteTests(WiringTestBase):
    """The /api/share/* route layer must surface assembly failures as a
    structured 503 (BrokerError path inside the route try block)."""

    def setUp(self):
        super().setUp()
        token_path = Path(self.tmp.name) / "usage-control-token"
        token_path.write_text("w3-route-token", encoding="utf-8")
        self.usagesrv.CONTROL_TOKEN_FILE = str(token_path)
        self.server = self.usagesrv.ThreadingHTTPServer(
            ("127.0.0.1", 0), self.usagesrv.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request_status(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/api/share/status", headers={
                "Host": f"127.0.0.1:{self.port}",
                "X-Admin-Control": "w3-route-token"})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_corrupt_config_returns_structured_503(self):
        self.config_path.write_text("{broken", encoding="utf-8")
        status, payload = self.request_status()
        self.assertEqual(status, 503)
        self.assertIn("unreadable", payload.get("error", ""))

    def test_absent_config_status_reports_noop_honestly(self):
        status, payload = self.request_status()
        self.assertEqual(status, 200)
        self.assertEqual(payload["broker"]["name"], "noop")
        self.assertEqual(payload["broker"]["state"], "offline")

    def test_valid_config_unreachable_broker_status_is_unavailable(self):
        # 配置有效但 broker 不在：状态必须显式 unavailable，不能假称在线。
        key = self.write_key_file()
        self.write_config(self.valid_config(control_key_file=key))
        status, payload = self.request_status()
        self.assertEqual(status, 200)
        self.assertEqual(payload["broker"]["name"], "broker-client")
        self.assertEqual(payload["broker"]["state"], "unavailable")


if __name__ == "__main__":
    unittest.main()
