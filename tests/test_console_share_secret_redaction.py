# -*- coding: utf-8 -*-
"""Share Key plaintext stays in the ONE immediate response (D18-3).

A full plaintext may ride only the immediate create/rotate/reveal response of
the authorized private console.  It must never appear in an operation record, a
log line, a URL or an error body.  This is a boundary test over the real
usagesrv HTTP handler with an isolated SQLite store and a fake broker.
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from share_management import ShareManager  # noqa: E402
import console_share_actions as _share_orchestration  # noqa: E402

_ISOLATION_TEMP = None
_ISOLATION_PATCHERS = []
_ISOLATION_MODULES = []
_ISOLATION_ATTEMPTS = []


def _refuse_real_kernel(*args, **kwargs):
    _ISOLATION_ATTEMPTS.append("real_named_pipe_kernel")
    raise AssertionError("offline test attempted a real named-pipe kernel")


def _guard_usage_module(module):
    root = Path(_ISOLATION_TEMP.name)
    _ISOLATION_MODULES.append(module)
    values = {"SHARE_MANAGEMENT_CONFIG_FILE": str(root / "absent.json"),
              "CONSOLE_STATE_FILE": str(root / "console.sqlite3"),
              "CONSOLE_LOCK_DIR": str(root / "console-locks"),
              "_console_manager": None, "_share_manager": None}
    for name, value in values.items():
        patcher = mock.patch.object(module, name, value)
        patcher.start()
        _ISOLATION_PATCHERS.append(patcher)


def setUpModule():
    global _ISOLATION_TEMP
    import share_broker_client
    import share_broker_server
    _ISOLATION_TEMP = tempfile.TemporaryDirectory(
        prefix="secret-redaction-")
    root = Path(_ISOLATION_TEMP.name)
    patcher = mock.patch.dict(os.environ, {
        "SHARE_MANAGEMENT_CONFIG": str(root / "absent.json"),
        "SHARE_STORE_FILE": str(root / "share.db"),
        "CONSOLE_STATE_FILE": str(root / "console.sqlite3"),
        "CONSOLE_LOCK_DIR": str(root / "console-locks")})
    patcher.start()
    _ISOLATION_PATCHERS.append(patcher)
    for owner, name in ((share_broker_client.NamedPipeBrokerTransport,
                         "_declare_real_kernel"),
                        (share_broker_server, "_declared_kernel")):
        patcher = mock.patch.object(owner, name, _refuse_real_kernel)
        patcher.start()
        _ISOLATION_PATCHERS.append(patcher)


def tearDownModule():
    try:
        for module in _ISOLATION_MODULES:
            current = module._console_manager
            if current is not None:
                current.close(wait=True)
    finally:
        for patcher in reversed(_ISOLATION_PATCHERS):
            patcher.stop()
        _ISOLATION_PATCHERS.clear()
        _ISOLATION_MODULES.clear()
        _ISOLATION_TEMP.cleanup()
    if _ISOLATION_ATTEMPTS:
        raise AssertionError("management isolation breached: "
                             + repr(_ISOLATION_ATTEMPTS))


class FakeBroker:
    def __init__(self):
        self.published = []

    def snapshot(self):
        return {"name": "fake", "state": "fake"}

    def publish(self, projection, terminate_keys=None):
        self.published.append(projection)
        return {"applied_generation": projection["config_generation"],
                "security_generation": projection["config_generation"],
                "terminated": 0}

    def host_verify(self, state):
        return state == "closed"


class SecretRedactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True, prefix="secret-redaction-case-",
            dir=_ISOLATION_TEMP.name)
        os.environ["USAGE_SESSION_CODE_FILE"] = os.path.join(
            cls.tmp.name, "usage-session-code")
        os.environ["USAGE_CONTROL_TOKEN_FILE"] = os.path.join(
            cls.tmp.name, "usage-control-token")
        os.environ["SHARE_STORE_FILE"] = os.path.join(
            cls.tmp.name, "share.db")
        spec = importlib.util.spec_from_file_location(
            "usagesrv_secret_redaction_test",
            Path(__file__).resolve().parents[1] / "usagesrv.py")
        cls.usagesrv = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.usagesrv
        spec.loader.exec_module(cls.usagesrv)
        _guard_usage_module(cls.usagesrv)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.tmp_store = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True, prefix="redaction-store-",
            dir=self.tmp.name)
        self.addCleanup(self.tmp_store.cleanup)
        db = Path(self.tmp_store.name) / "share.db"
        os.environ["SHARE_STORE_FILE"] = str(db)
        self.broker = FakeBroker()
        self.usagesrv.get_share_manager = lambda: ShareManager(
            store_path=db, broker=self.broker)
        self._install_observation_fixtures()
        self.server = self.usagesrv.ThreadingHTTPServer(
            ("127.0.0.1", 0), self.usagesrv.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                        daemon=True)
        self.thread.start()
        with open(os.environ["USAGE_CONTROL_TOKEN_FILE"], "w",
                  encoding="utf-8") as handle:
            handle.write("test-control-token")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        with self.usagesrv._share_manager_lock:
            self.usagesrv._share_manager = None

    def _install_observation_fixtures(self):
        import console_adapters
        import console_share_production
        import subprocess
        saved = {name: value for name, value in vars(_share_orchestration).items()
                 if name.startswith("_") and name.upper() == name}
        self.addCleanup(lambda: [
            _share_orchestration.__dict__.__setitem__(name, value)
            for name, value in saved.items()])
        _share_orchestration._FUNNEL_OBSERVED = None

        def fake_runner(argv, timeout=None, **kwargs):
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        def wire_fixture(keys_manager=None, cloud=False):
            seams = console_share_production.production_seams(
                broker=self.broker, runner=fake_runner,
                keys_manager=keys_manager, cloud=cloud,
                status_path=str(Path(self.tmp_store.name) / "absent.json"))
            seams["_BACKEND_OBSERVE"] = lambda: {
                "running": True, "auth_state": "valid"}
            for name, value in seams.items():
                setattr(_share_orchestration, name, value)

        patcher = mock.patch.object(console_adapters,
                                     "wire_share_production_seams", wire_fixture)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, path, method="GET", body=None, xrw=None):
        import http.client
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=5)
        headers = {"Host": "127.0.0.1:8001"}
        if xrw:
            headers["X-Requested-With"] = xrw
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_plaintext_never_leaks_outside_the_immediate_responses(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        paths = []
        with contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            status, body = self.request(
                "/api/console/share-management/keys", method="POST",
                xrw="XMLHttpRequest", body={"name": "redaction"})
            self.assertEqual(status, 201)
            created = json.loads(body)
            plaintext = created["plaintext"]
            self.assertTrue(plaintext.startswith("um-share-"))
            key = created["key"]
            self.assertNotEqual(key["masked"], plaintext)
            paths.append("/api/console/share-management/keys")

            # The immediate create response is the ONLY response allowed to
            # carry the full plaintext.
            status, body = self.request("/api/console/share-management/keys")
            self.assertEqual(status, 200)
            self.assertNotIn(plaintext.encode(), body)

            status, body = self.request(
                "/api/console/share-management/status")
            self.assertEqual(status, 200)
            self.assertNotIn(plaintext.encode(), body)

            status, body = self.request(
                f"/api/console/share-management/keys/{key['id']}",
                method="PATCH", xrw="XMLHttpRequest",
                body={"max_concurrency": 0,
                       "expected_row_version": key["row_version"]})
            self.assertEqual(status, 400)
            self.assertNotIn(plaintext.encode(), body)
            paths.append(f"/api/console/share-management/keys/{key['id']}")

            status, body = self.request(
                f"/api/console/share-management/keys/{key['id']}",
                method="PATCH", xrw="XMLHttpRequest",
                body={"enabled": False,
                       "expected_row_version": key["row_version"]})
            self.assertEqual(status, 400)
            self.assertNotIn(plaintext.encode(), body)

            status, body = self.request(
                f"/api/console/share-management/keys/{key['id']}"
                "/revoke-and-terminate",
                method="POST", xrw="XMLHttpRequest",
                body={"expected_row_version": key["row_version"]})
            self.assertEqual(status, 200)
            revoked = json.loads(body)
            self.assertNotIn(plaintext.encode(), body)
            self.assertTrue(revoked["note"])

        self.assertNotIn(plaintext, stdout.getvalue())
        self.assertNotIn(plaintext, stderr.getvalue())

        from share_store import ShareStore
        store = ShareStore(os.environ["SHARE_STORE_FILE"])
        try:
            operations = store.operations()
        finally:
            store.close()
        self.assertNotIn(plaintext, json.dumps(operations, ensure_ascii=False))
        for path in paths:
            self.assertNotIn(plaintext, path)


if __name__ == "__main__":
    unittest.main()
