"""A36-2 console share-management facade boundary tests.

End-to-end over a temporary SQLite store and fake broker: the seven
whitelisted routes, the console trust surface (credential-free reads,
same-origin-marker or control-token writes), the reshaped contract
(id/masked/plaintext), and the plaintext-once rule. Never touches
data/share-management.json, never starts or stops a live host component,
never calls tailscale/schtasks/sc, and makes no billable request.
"""
import http.client
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from share.management import ShareManager  # noqa: E402
from console import share_actions as _share_orchestration  # noqa: E402



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
    local = globals().get("usagesrv")
    if local is not None:
        _guard_usage_module(local)
    _ISOLATION_SAVED_SEAMS = {
        name: value for name, value in vars(console_share_actions).items()
        if name.startswith("_") and name.upper() == name}
    # The retired Windows named-pipe transport is gone with the old local
    # share chain (#33).
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
    if _ISOLATION_ATTEMPTS:
        raise AssertionError("management isolation breached: " +
                             repr(_ISOLATION_ATTEMPTS))


class FakeBroker:
    def __init__(self):
        self.published = []
        self.terminate_keys_seen = []
        self.start_calls = 0
        self.graceful_stop_calls = 0
        self.emergency_stop_calls = 0
        self.emergency_stop_result = {"state": "stopped", "stopped": True}
        # S2: model the REAL 8100 runtime's security watermark. The runtime
        # advances its own applied revoke watermark ONLY when a Key transitions
        # from enabled to disabled/absent (a REVOKE). A rotate (same Key, new
        # secret_version) leaves it untouched, so the pending rotate intent is
        # NEVER confirmed by this ACK and the management fence fails closed.
        self._enabled = set()
        self._security_generation = 0

    def host_verify(self, state):
        return state == "closed"

    def snapshot(self):
        return {"name": "fake", "state": "fake", "published": len(self.published)}

    def publish(self, projection, terminate_keys=None):
        self.published.append(projection)
        if terminate_keys:
            self.terminate_keys_seen.append(list(terminate_keys))
        enabled = {entry["key_id"] for entry in projection["data"]
                   if entry["enabled"]}
        generation = projection["config_generation"]
        if self._enabled - enabled:
            self._security_generation = max(self._security_generation,
                                            generation)
        self._enabled = enabled
        return {"applied_generation": generation,
                "security_generation": self._security_generation,
                # D18-3: the facade must carry every count, not only
                # `terminated`; distinct values catch a dropped field.
                "terminated": 2 if terminate_keys else None,
                "released": 3 if terminate_keys else None,
                "aborted": 4 if terminate_keys else None,
                "upstream_closed": 5 if terminate_keys else None}

    def start(self):
        self.start_calls += 1
        return {"state": "ok", "started": True}

    def graceful_stop(self):
        self.graceful_stop_calls += 1
        return {"state": "stopped", "drained": True, "stopped": True}

    def emergency_stop(self):
        self.emergency_stop_calls += 1
        return dict(self.emergency_stop_result)


class ConsoleShareFacadeTests(unittest.TestCase):
    """Loads usagesrv under a unique module name so the shared
    usage-boundary module's import-time paths stay untouched."""

    @classmethod
    def setUpClass(cls):
        cls._saved_env = {}
        for key in ("USAGE_SESSION_CODE_FILE", "USAGE_CONTROL_TOKEN_FILE",
                    "SHARE_STORE_FILE", "SHARE_MANAGEMENT_CONFIG",
                    "CONSOLE_STATE_FILE", "CONSOLE_LOCK_DIR",
                    "CONSOLE_PUBLIC_ORIGIN"):
            cls._saved_env[key] = os.environ.get(key)
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True,
                                              prefix="console-share-facade-",
                                              dir=_ISOLATION_TEMP.name)
        os.environ["USAGE_SESSION_CODE_FILE"] = os.path.join(
            cls.tmp.name, "usage-session-code")
        os.environ["USAGE_CONTROL_TOKEN_FILE"] = os.path.join(
            cls.tmp.name, "usage-control-token")
        os.environ["SHARE_STORE_FILE"] = os.path.join(cls.tmp.name, "share.db")
        os.environ["CONSOLE_PUBLIC_ORIGIN"] = "https://fixture.tailnet.ts.net"
        # The console status GET ensures the production seam wiring itself
        # (usagesrv's production console-manager assembly), so loading the
        # module mutates console_share_actions' seam globals. Snapshot them
        # here and restore them in tearDownClass: a test that triggers the
        # real assembly must never leak it into later test modules.
        cls._saved_orchestration = {
            name: value
            for name, value in vars(_share_orchestration).items()
            if name.startswith("_") and name.upper() == name}
        spec = importlib.util.spec_from_file_location(
            "usagesrv_console_share_facade_test",
            Path(__file__).resolve().parents[1] / "usagesrv.py")
        cls.usagesrv = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.usagesrv
        spec.loader.exec_module(cls.usagesrv)
        _guard_usage_module(cls.usagesrv)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        for key, old in cls._saved_env.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        for name, value in cls._saved_orchestration.items():
            setattr(_share_orchestration, name, value)

    def setUp(self):
        # One authoritative store per test: no residue leaks between cases.
        self.tmp_store = tempfile.TemporaryDirectory(ignore_cleanup_errors=True,
                                                     prefix="facade-store-",
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
                  encoding="utf-8") as f:
            f.write("test-control-token")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        with self.usagesrv._share_manager_lock:
            self.usagesrv._share_manager = None

    def _install_observation_fixtures(self):
        from console import adapters as console_adapters
        from console import share_production as console_share_production
        import subprocess
        saved = {name: value for name, value in vars(_share_orchestration).items()
                 if name.startswith("_") and name.upper() == name}
        self.addCleanup(lambda: [_share_orchestration.__dict__.__setitem__(name, value)
                                 for name, value in saved.items()])

        def fake_runner(argv, timeout=None, **kwargs):
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        def wire_fixture(keys_manager=None, cloud=False):
            seams = console_share_production.production_seams(
                broker=self.broker, runner=fake_runner, keys_manager=keys_manager,
                cloud=cloud,
                status_path=str(Path(self.tmp_store.name) / "absent-status.json"))
            for name, value in seams.items():
                setattr(_share_orchestration, name, value)
            _share_orchestration._BROKER_OPS = (
                console_share_production.production_broker_ops(
                    broker=self.broker))

        patcher = mock.patch.object(console_adapters, "wire_share_production_seams",
                                    wire_fixture)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, path, method="GET", token=None, body=None,
                xrw=None, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"Host": "127.0.0.1:8001"}
        if token:
            headers["X-Admin-Control"] = token
        if xrw:
            headers["X-Requested-With"] = xrw
        if origin:
            headers["Origin"] = origin
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    # ------------------------------------------------------------- reads

    def test_get_status_credential_free_and_reshaped(self):
        status, body = self.request("/api/console/share-management/status")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("desired_generation", data)
        self.assertIn("in_sync", data)
        self.assertNotIn("secret", body.decode())
        self.assertNotIn("um-share-", body.decode())
        self.assertNotIn("funnel_on", data)

    def test_status_broker_snapshot_mapping(self):
        f = self.usagesrv.Handler._console_share_status_payload
        self.assertEqual(f({"broker": {"state": "ok"}})["broker"],
                         {"running": True})
        self.assertEqual(f({"broker": {"state": "unavailable"}})["broker"],
                         {"running": False})
        # A snapshot proving nothing maps to nothing — the UI's own
        # absent-field fallback, never a fabricated running/not-running.
        self.assertEqual(f({"broker": {"state": "weird"}})["broker"], {})
        self.assertEqual(f({})["broker"] if "broker" in f({}) else None, None)

    def test_get_keys_credential_free_and_row_shape(self):
        created = json.loads(self.request(
            "/api/console/share-management/keys", method="POST", xrw="XMLHttpRequest",
            body={"name": "friendA"})[1])
        status, body = self.request("/api/console/share-management/keys")
        self.assertEqual(status, 200)
        rows = json.loads(body)["keys"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], created["key"]["id"])
        self.assertEqual(row["name"], "friendA")
        self.assertEqual(set(row), {"id", "name", "masked",
                                    "plaintext_viewable", "enabled",
                                    "max_concurrency", "row_version",
                                    "created_at"})
        # The old backend field names must not leak into the console shape.
        self.assertNotIn("key_id", row)
        self.assertNotIn("masked_key", row)
        self.assertNotIn("secret_hash", body.decode())

    def test_non_whitelisted_paths_are_404(self):
        for path in ("/api/console/share-management/operations",
                     "/api/console/share-management/keys/abc",
                     "/api/console/share-management/keys/abc/unknown"):
            status, _ = self.request(path)
            self.assertEqual(status, 404, path)

    # -------------------------------------------------------- write gate

    def test_write_without_marker_or_token_rejected(self):
        status, _ = self.request("/api/console/share-management/keys",
                                 method="POST", body={"name": "x"})
        self.assertEqual(status, 403)
        status, _ = self.request("/api/console/share-management/keys",
                                 method="POST", token="wrong",
                                 body={"name": "x"})
        self.assertEqual(status, 403)

    def test_write_with_marker_and_with_token_accepted(self):
        status, body = self.request(
            "/api/console/share-management/keys", method="POST",
            xrw="XMLHttpRequest", body={"name": "a"})
        self.assertEqual(status, 201)
        self.assertNotIn("secret", body.decode())  # renamed to plaintext
        status, _ = self.request(
            "/api/console/share-management/keys", method="POST",
            token="test-control-token", body={"name": "b"})
        self.assertEqual(status, 201)

    def test_foreign_origin_rejected_before_facade(self):
        status, _ = self.request("/api/console/share-management/status",
                                origin="http://evil.example")
        self.assertEqual(status, 403)
        status, _ = self.request("/api/console/share-management/keys",
                                 method="POST", xrw="XMLHttpRequest",
                                 origin="http://evil.example",
                                 body={"name": "x"})
        self.assertEqual(status, 403)

    # ---------------------------------------------------- plaintext once

    def test_plaintext_only_in_immediate_create_response(self):
        status, body = self.request(
            "/api/console/share-management/keys", method="POST",
            xrw="XMLHttpRequest", body={"name": "one-shot"})
        self.assertEqual(status, 201)
        created = json.loads(body)
        self.assertTrue(created["plaintext"].startswith("um-share-"))
        one_shot = created["plaintext"].encode()
        # Probe with the ACTUAL plaintext value, not the "um-share-" prefix
        # (the prefix legitimately appears in every masked display).
        # No replay surface: list and every later read carry no plaintext;
        # an idempotent REPLAY (second call, same key) never replays it.
        status, body = self.request("/api/console/share-management/keys")
        self.assertNotIn(one_shot, body)
        status, body = self.request(  # first use: plaintext is due
            "/api/console/share-management/keys", method="POST",
            xrw="XMLHttpRequest",
            body={"name": "replayed", "idempotency_key": "replay-1"})
        self.assertIn(b"plaintext", body)
        status, body = self.request(  # true replay: key present but null value
            "/api/console/share-management/keys", method="POST",
            xrw="XMLHttpRequest",
            body={"name": "replayed", "idempotency_key": "replay-1"})
        self.assertIsNone(json.loads(body).get("plaintext"))
        status, body = self.request("/api/console/share-management/status")
        self.assertNotIn(one_shot, body)

    # ------------------------------------------------- validation parity

    def test_unknown_field_rejected(self):
        status, _ = self.request(
            "/api/console/share-management/keys", method="POST",
            xrw="XMLHttpRequest",
            body={"name": "x", "url": "http://evil"})
        self.assertEqual(status, 400)

    def test_create_invalid_max_concurrency(self):
        for bad in (0, 49, 65, True, "8"):
            status, _ = self.request(
                "/api/console/share-management/keys", method="POST",
                xrw="XMLHttpRequest",
                body={"name": "x", "max_concurrency": bad})
            self.assertEqual(status, 400, repr(bad))

    def test_create_uses_the_d18_default_and_accepts_48(self):
        created = self._create("default-concurrency")
        self.assertEqual(created["max_concurrency"], 16)
        status, body = self.request(
            "/api/console/share-management/keys", method="POST",
            xrw="XMLHttpRequest",
            body={"name": "forty-eight", "max_concurrency": 48})
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["key"]["max_concurrency"], 48)

    # ------------------------------------------------------ mutations

    def _create(self, name="m"):
        status, body = self.request(
            "/api/console/share-management/keys", method="POST",
            xrw="XMLHttpRequest", body={"name": name})
        self.assertEqual(status, 201)
        return json.loads(body)["key"]

    def test_patch_flow_and_version_conflict(self):
        key = self._create("patchy")
        status, body = self.request(
            f"/api/console/share-management/keys/{key['id']}",
            method="PATCH", xrw="XMLHttpRequest",
            body={"name": "renamed",
                   "expected_row_version": key["row_version"]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["key"]["name"], "renamed")
        # Stale version → 409, the optimistic gate is live on the facade.
        status, _ = self.request(
            f"/api/console/share-management/keys/{key['id']}",
            method="PATCH", xrw="XMLHttpRequest",
            body={"name": "again",
                   "expected_row_version": key["row_version"]})
        self.assertEqual(status, 409)

    def test_patch_never_re_enables_or_disables_a_key(self):
        # D18-3: `enabled` is a read-only fact; the write face refuses it
        # instead of silently ignoring it.
        key = self._create("enabled-gate")
        for value in (False, True):
            with self.subTest(value=value):
                status, body = self.request(
                    f"/api/console/share-management/keys/{key['id']}",
                    method="PATCH", xrw="XMLHttpRequest",
                    body={"enabled": value,
                           "expected_row_version": key["row_version"]})
                self.assertEqual(status, 400)
                self.assertIn(json.loads(body)["error"],
                              ("unknown_field", "enabled_not_writable"))

    def test_patch_concurrency_is_1_to_48(self):
        key = self._create("limits")
        status, body = self.request(
            f"/api/console/share-management/keys/{key['id']}",
            method="PATCH", xrw="XMLHttpRequest",
            body={"max_concurrency": 48,
                   "expected_row_version": key["row_version"]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["key"]["max_concurrency"], 48)
        for bad in (0, 49, 65, True, "48"):
            with self.subTest(value=bad):
                status, _ = self.request(
                    f"/api/console/share-management/keys/{key['id']}",
                    method="PATCH", xrw="XMLHttpRequest",
                    body={"max_concurrency": bad,
                           "expected_row_version": key["row_version"]})
                self.assertEqual(status, 400)

    def test_rotate_is_fail_closed_until_runtime_catches_up(self):
        key = self._create("rot")
        status, _ = self.request(
            f"/api/console/share-management/keys/{key['id']}/rotate",
            method="POST", xrw="XMLHttpRequest", body={})
        self.assertEqual(status, 400)  # expected_row_version required
        status, _ = self.request(
            f"/api/console/share-management/keys/{key['id']}/rotate",
            method="POST", xrw="XMLHttpRequest",
            body={"expected_row_version": 999})
        self.assertEqual(status, 409)
        # The restricted stop cannot confirm closure for a still-enabled key:
        # the rotate publication fails closed and stays blocked.
        self.broker.emergency_stop_result = {"state": "stopped"}
        status, body = self.request(
            f"/api/console/share-management/keys/{key['id']}/rotate",
            method="POST", xrw="XMLHttpRequest",
            body={"expected_row_version": key["row_version"]})
        # The real 8100 runtime does NOT advance its applied revoke watermark
        # for a rotate (the Key stays enabled), so the publication is refused
        # fail-closed instead of pretending a success.
        self.assertEqual(status, 503)
        self.assertIn("revoking operation not applied online", body.decode())
        # The rotate transaction still committed the new secret version, and the
        # narrow readback reveals the new plaintext exactly once.
        status, body = self.request(
            f"/api/console/share-management/keys/{key['id']}/secret")
        self.assertEqual(status, 200)
        rotated = json.loads(body)
        self.assertTrue(rotated["secret"].startswith("um-share-"))
        self.assertNotEqual(rotated["secret"], key["masked"])
        # The failed publication stays pending + blocked, and its committed
        # intent row names the exact target generation (提交行).
        status, body = self.request("/api/console/share-management/status")
        status_payload = json.loads(body)
        self.assertTrue(status_payload["security_pending"])
        self.assertTrue(status_payload["blocked"])
        blocker = status_payload["blocked_publications"][0]
        self.assertEqual(blocker["target_generation"],
                         status_payload["desired_generation"])
        from share.store import ShareStore
        store = ShareStore(os.environ["SHARE_STORE_FILE"])
        try:
            publish_ops = store.operations(kind="publish")
            generation = store.generation()
        finally:
            store.close()
        intent = next(op for op in publish_ops
                       if op["payload"].get("target_generation")
                       == generation["config_generation"])
        self.assertIn(intent["status"], ("pending", "blocked", "failed"))
        # Restart recovery: the restart reconciliation drives the pending
        # publication to a confirmed stop and clears the blocked flag. The
        # security watermark stays unconfirmed (the runtime never advanced it
        # for a rotate), so the plaintext stays revealable and the key stays
        # enabled.
        ShareManager._recovered_paths.discard(
            str(Path(os.environ["SHARE_STORE_FILE"]).resolve()))
        self.broker.emergency_stop_result = {"state": "stopped",
                                            "stopped": True}
        self.usagesrv.get_share_manager = lambda: ShareManager(
            store_path=Path(os.environ["SHARE_STORE_FILE"]),
            broker=self.broker)
        status, body = self.request("/api/console/share-management/status")
        recovered = json.loads(body)
        self.assertFalse(recovered["blocked"])
        self.assertTrue(recovered["security_pending"])
        self.assertTrue(recovered["blocked_publications"][0]["stop_confirmed"])
        status, body = self.request(
            f"/api/console/share-management/keys/{key['id']}/secret")
        self.assertEqual(json.loads(body)["secret"], rotated["secret"])

    def test_revoke_and_terminate_flow(self):
        key = self._create("rev")
        status, body = self.request(
            f"/api/console/share-management/keys/{key['id']}/revoke-and-terminate",
            method="POST", xrw="XMLHttpRequest",
            body={"expected_row_version": key["row_version"]})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertFalse(result["key"]["enabled"])
        self.assertTrue(result["revoked"])
        self.assertEqual(self.broker.terminate_keys_seen, [[key["id"]]])
        # C1/D18-3: the answer separates the gateway counters and never
        # pretends an already-consumed upstream cost can be withdrawn.
        self.assertEqual(result["terminated"], 2)
        self.assertEqual(result["released"], 3)
        self.assertEqual(result["aborted"], 4)
        self.assertEqual(result["upstream_closed"], 5)
        self.assertIn("不可撤回", result["note"])

    def test_revoke_without_a_terminated_count_does_not_claim_confirmation(self):
        # An older instance ACK carries no `terminated` count: the note must
        # say the in-flight state was NOT confirmed instead of claiming a
        # termination that was never observed.
        key = self._create("rev-none")

        class _NoCountBroker(FakeBroker):
            def publish(self, projection, terminate_keys=None):
                out = super().publish(
                    projection, terminate_keys=terminate_keys)
                out.pop("terminated", None)
                return out

        self.broker.__class__ = _NoCountBroker
        status, body = self.request(
            f"/api/console/share-management/keys/{key['id']}/revoke-and-terminate",
            method="POST", xrw="XMLHttpRequest",
            body={"expected_row_version": key["row_version"]})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertIsNone(result["terminated"])
        self.assertIn("在途确认未完成", result["note"])
        self.assertNotIn("已撤销并确认终止在途", result["note"])

    def test_limits_normalize_route_and_cli_reach_the_manager(self):
        # D18-4/C1: the documented `share/admin.py limits --normalize`
        # action must reach ShareManager.normalize_limits(), not fall through
        # to the unknown-path 404.
        status, body = self.request(
            "/api/share/limits/normalize", method="POST",
            token="test-control-token", body={})
        self.assertEqual(status, 200, body)
        result = json.loads(body)
        for field in ("changed", "global", "keys", "blocked"):
            self.assertIn(field, result)
        self.assertIn("before", result["global"])
        self.assertIn("after", result["global"])

        admin = Path(__file__).resolve().parents[1] / "share/admin.py"
        env = dict(os.environ, SHARE_MGMT_ALLOW_PORT=str(self.port))
        proc = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", str(admin),
             "--base-url", f"http://127.0.0.1:{self.port}",
             "--control-token", "test-control-token",
             "limits", "--normalize"],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
            env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cli = json.loads(proc.stdout)
        self.assertEqual(cli["blocked"], False)
        self.assertIn("before", cli["global"])

    def test_delete_flow_with_body_gate(self):
        key = self._create("del")
        status, _ = self.request(
            f"/api/console/share-management/keys/{key['id']}",
            method="DELETE", xrw="XMLHttpRequest", body={})
        self.assertEqual(status, 400)  # gate required on DELETE too
        status, body = self.request(
            f"/api/console/share-management/keys/{key['id']}",
            method="DELETE", xrw="XMLHttpRequest",
            body={"expected_row_version": key["row_version"]})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        status, body = self.request("/api/console/share-management/keys")
        self.assertEqual(json.loads(body)["keys"], [])

    def test_original_share_surface_unchanged(self):
        # The facade must not have moved the /api/share trust model: that
        # surface still demands management credentials.
        status, _ = self.request("/api/share/status")
        self.assertEqual(status, 401)

    # ------------------------------------------------ B1 narrow readback
    def test_key_probe_route_is_explicit_public_and_version_bound(self):
        from console import share_probe as console_share_probe
        created = json.loads(self.request(
            '/api/console/share-management/keys', method='POST',
            xrw='XMLHttpRequest', body={'name': 'probe-fixture'})[1])
        row = created['key']
        path = '/api/console/share-management/keys/' + row['id'] + '/test'
        body = {'expected_row_version': row['row_version']}
        with mock.patch.object(console_share_probe, 'probe_models', return_value={
                'connected': True, 'reason': 'ok', 'http_status': 200,
                'stage': 'inference', 'model': 'fixture-model',
                'public_ip': '1.1.1.1', 'elapsed_ms': 42}) as probe:
            self.assertEqual(self.request(path, method='POST', body=body)[0], 403)
            self.broker.host_verify = lambda check: check == 'running'
            status, raw = self.request(path, method='POST', xrw='XMLHttpRequest', body=body)
            result = json.loads(raw)
            self.assertEqual(status, 200)
            self.assertTrue(result['connected'])
            self.assertEqual(result['tested_from'], 'public')
            self.assertEqual(result['stage'], 'inference')
            self.assertEqual(result['public_url'], 'https://fixture.tailnet.ts.net')
            probe.assert_called_once_with(created['plaintext'], 'https://fixture.tailnet.ts.net')
            self.assertNotIn(created['plaintext'], raw.decode())
            probe.reset_mock()
            body['expected_row_version'] += 1
            result = json.loads(self.request(path, method='POST', xrw='XMLHttpRequest', body=body)[1])
            self.assertFalse(result['connected'])
            self.assertEqual(result['reason'], 'key_changed')
            probe.assert_not_called()
            body['url'] = 'https://example.invalid'
            self.assertEqual(self.request(path, method='POST', xrw='XMLHttpRequest', body=body)[0], 400)

    def test_key_probe_never_sends_credential_to_unverified_gateway(self):
        from console import share_probe as console_share_probe
        created = json.loads(self.request(
            '/api/console/share-management/keys', method='POST',
            xrw='XMLHttpRequest', body={'name': 'unverified'})[1])
        row = created['key']
        with mock.patch.object(console_share_probe, 'probe_models') as probe:
            result = json.loads(self.request(
                '/api/console/share-management/keys/' + row['id'] + '/test',
                method='POST', xrw='XMLHttpRequest',
                body={'expected_row_version': row['row_version']})[1])
            self.assertFalse(result['connected'])
            self.assertEqual(result['reason'], 'gateway_unconfirmed')
            probe.assert_not_called()

    def test_reveal_returns_created_plaintext_credential_free(self):
        status, body = self.request(
            "/api/console/share-management/keys", method="POST",
            xrw="XMLHttpRequest", body={"name": "reveal-me"})
        self.assertEqual(status, 201)
        created = json.loads(body)
        self.assertTrue(created["plaintext"])
        key_id = created["key"]["id"]
        self.assertTrue(created["key"]["plaintext_viewable"])
        status, body = self.request(
            f"/api/console/share-management/keys/{key_id}/secret")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["secret"], created["plaintext"])
        self.assertEqual(payload["key_id"], key_id)

    def test_reveal_after_rotate_answers_new_secret(self):
        status, body = self.request(
            "/api/console/share-management/keys", method="POST",
            xrw="XMLHttpRequest", body={"name": "rotate-me"})
        key_id = json.loads(body)["key"]["id"]
        old_plain = json.loads(body)["plaintext"]
        row_version = json.loads(body)["key"]["row_version"]
        status, _ = self.request(
            f"/api/console/share-management/keys/{key_id}/rotate",
            method="POST", xrw="XMLHttpRequest",
            body={"expected_row_version": row_version})
        # The runtime does not confirm a rotate: the publication fails closed,
        # but the rotate transaction committed the new secret version.
        self.assertEqual(status, 503)
        status, body = self.request(
            f"/api/console/share-management/keys/{key_id}/secret")
        self.assertEqual(status, 200)
        new_plain = json.loads(body)["secret"]
        self.assertNotEqual(new_plain, old_plain)
        self.assertTrue(new_plain.startswith("um-share-"))

    def test_reveal_deleted_answers_410_and_legacy_answers_inline(self):
        status, body = self.request(
            "/api/console/share-management/keys", method="POST",
            xrw="XMLHttpRequest", body={"name": "delete-me"})
        key_id = json.loads(body)["key"]["id"]
        row_version = json.loads(body)["key"]["row_version"]
        status, body = self.request(
            f"/api/console/share-management/keys/{key_id}",
            method="DELETE", xrw="XMLHttpRequest",
            body={"expected_row_version": row_version})
        self.assertEqual(status, 200)
        status, body = self.request(
            f"/api/console/share-management/keys/{key_id}/secret")
        self.assertEqual(status, 410)
        self.assertEqual(json.loads(body)["error"], "key_deleted")
        # A legacy hash-only row (no ciphertext blob) answers an honest
        # inline unavailable state — an expected condition until rotation.
        from share.store import ShareStore
        legacy = ShareStore(self.usagesrv.os.environ["SHARE_STORE_FILE"])
        try:
            legacy.migrate_apply({"legacy-friend": "a" * 64})
        finally:
            legacy.close()
        status, body = self.request("/api/console/share-management/keys")
        rows = json.loads(body)["keys"]
        legacy_row = [r for r in rows if r["name"] == "legacy-friend"][0]
        self.assertFalse(legacy_row["plaintext_viewable"])
        status, body = self.request(
            f"/api/console/share-management/keys/{legacy_row['id']}/secret")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["secret_unavailable"])
        self.assertEqual(payload["reason"], "legacy")

    def test_reveal_unknown_id_answers_404(self):
        status, body = self.request(
            "/api/console/share-management/keys/does-not-exist/secret")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "key identity not found")

    def test_reveal_version_conflict_and_undecryptable_inline(self):
        status, body = self.request(
            "/api/console/share-management/keys", method="POST",
            xrw="XMLHttpRequest", body={"name": "vc"})
        key_id = json.loads(body)["key"]["id"]
        row_version = json.loads(body)["key"]["row_version"]
        # Stale caller view (row_version mismatch) answers the distinct inline
        # 版本冲突 state instead of a plaintext of a version it never saw.
        status, body = self.request(
            f"/api/console/share-management/keys/{key_id}/secret"
            f"?expected_row_version={row_version + 1}")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["secret_unavailable"])
        self.assertEqual(payload["reason"], "version_conflict")
        # Corrupted ciphertext (same identity cannot decrypt) answers the
        # distinct inline undecryptable state.
        import sqlite3
        conn = sqlite3.connect(os.environ["SHARE_STORE_FILE"])
        try:
            conn.execute("UPDATE keys SET secret_blob=? WHERE key_id=?",
                         (sqlite3.Binary(b"corrupted-blob"), key_id))
            conn.commit()
        finally:
            conn.close()
        status, body = self.request(
            f"/api/console/share-management/keys/{key_id}/secret")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["secret_unavailable"])
        self.assertEqual(payload["reason"], "undecryptable")


if __name__ == "__main__":
    unittest.main()
