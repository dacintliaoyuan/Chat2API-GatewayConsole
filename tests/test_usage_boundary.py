"""Local management boundary of the usage service (host/origin/session/sanitisation).

Boundary logic only: /api/summary is tested with a stubbed build_summary, so
no ledger files are read. Real ledger aggregation stays covered by
test_usage_service.py.
"""

import http.client
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TMP = tempfile.TemporaryDirectory(prefix="usage-boundary-")
import os  # noqa: E402
_BOUNDARY_ENV = {
    "USAGE_SESSION_CODE_FILE": os.path.join(_TMP.name, "usage-session-code"),
    "USAGE_CONTROL_TOKEN_FILE": os.path.join(_TMP.name, "usage-control-token")}

SPEC = importlib.util.spec_from_file_location(
    "usagesrv", Path(__file__).resolve().parents[1] / "usagesrv.py")
usagesrv = importlib.util.module_from_spec(SPEC)
with mock.patch.dict(os.environ, _BOUNDARY_ENV):
    SPEC.loader.exec_module(usagesrv)



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
        "CONSOLE_LOCK_DIR": str(root / "console-locks"), **_BOUNDARY_ENV})
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


class HostOriginRulesTests(unittest.TestCase):
    def test_host_must_be_loopback(self):
        self.assertTrue(usagesrv.host_allowed("127.0.0.1:8001"))
        self.assertTrue(usagesrv.host_allowed("localhost:8001"))
        self.assertTrue(usagesrv.host_allowed("[::1]:8001"))
        self.assertFalse(usagesrv.host_allowed("evil.com"))
        self.assertFalse(usagesrv.host_allowed("internal.host:8001"))
        self.assertFalse(usagesrv.host_allowed(""))

    def test_host_rejects_malformed(self):
        self.assertFalse(usagesrv.host_allowed("127.0.0.1:bad"))

    def test_origin_only_same_loopback_origin(self):
        self.assertTrue(usagesrv.origin_allowed(None))
        self.assertTrue(usagesrv.origin_allowed("http://127.0.0.1:8001"))
        self.assertTrue(usagesrv.origin_allowed("http://localhost:8001"))
        self.assertFalse(usagesrv.origin_allowed("https://127.0.0.1:8001"))
        self.assertFalse(usagesrv.origin_allowed("http://evil.com"))
        self.assertFalse(usagesrv.origin_allowed("http://127.0.0.1:9999"))
        self.assertFalse(usagesrv.origin_allowed("http://127.0.0.1"))      # 缺端口 ≠ 本服务
        # 2026-11-01 端口迁移：旧默认 8096 不再是本服务来源。
        self.assertFalse(usagesrv.origin_allowed("http://127.0.0.1:8096"))
        self.assertFalse(usagesrv.origin_allowed("http://127.0.0.1:bad"))  # 非法端口拒绝而非抛异常

    def test_summary_sources_do_not_leak_absolute_paths(self):
        self.assertEqual(usagesrv._display_path(r"C:\repo\x\usage.jsonl"), "usage.jsonl")
        self.assertEqual(usagesrv._display_path(""), "(未配置)")
        self.assertEqual(usagesrv._display_path(None), "(未配置)")


class HandlerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self._original_summary = usagesrv.build_summary
        # R9-R39: summary is cache-served now; clear the cache so a mocked
        # build_summary from one class never leaks into a later cache hit.
        usagesrv._summary_cache_reset_for_tests()
        usagesrv.build_summary = lambda: {"stubbed": True}
        self.server = usagesrv.ThreadingHTTPServer(("127.0.0.1", 0), usagesrv.Handler)
        self.port = self.server.server_address[1]
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        # 与生产一致：data/admin-control-token 是程序化访问通道。
        with open(os.environ["USAGE_CONTROL_TOKEN_FILE"], "w", encoding="utf-8") as f:
            f.write("test-control-token")

    def tearDown(self):
        usagesrv.build_summary = self._original_summary
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path="/health", host="127.0.0.1:8001", origin=None, cookie=None,
                control_token=None, method="GET", body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"Host": host}
        if origin:
            headers["Origin"] = origin
        if cookie:
            headers["Cookie"] = cookie
        if control_token:
            headers["X-Admin-Control"] = control_token
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            return response.status, response.read(), response.msg
        finally:
            conn.close()

    def exchange_session(self):
        code = usagesrv.session_bootstrap_code()
        status, body, msg = self.request("/api/session", method="POST", body={"code": code})
        return status, msg

    def test_health_open_with_loopback_host(self):
        status, body, _ = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(usagesrv.json.loads(body)["service"], "usagesrv")

    def test_spoofed_host_rejected_everywhere(self):
        for path in ("/health", "/api/summary", "/"):
            with self.subTest(path=path):
                status, _, _ = self.request(path=path, host="evil.com")
                self.assertEqual(status, 403)

    def test_summary_open_for_local_reads(self):
        # R9-R34 (maintainer decision 2026-09-21): daily viewing must not
        # require exchanging a one-time bootstrap code; local read-only
        # access is open (loopback-only bind + Host/Origin route gates),
        # while the write boundary stays gated.
        status, _, _ = self.request(path="/api/summary")
        self.assertEqual(status, 200)

    def test_summary_rejects_cross_origin(self):
        status, _, _ = self.request(path="/api/summary", origin="http://evil.com")
        self.assertEqual(status, 403)

    def test_session_flow_bootstrap_to_summary(self):
        status, msg = self.exchange_session()
        self.assertEqual(status, 200)
        cookie = msg.get("Set-Cookie")
        self.assertIn("usage_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        status, body, _ = self.request(
            path="/api/summary", origin="http://127.0.0.1:8001",
            cookie="usage_session=" + cookie.split("usage_session=")[1].split(";")[0])
        self.assertEqual(status, 200)
        self.assertEqual(usagesrv.json.loads(body)["stubbed"], True)

    def test_session_exchange_rejects_wrong_code(self):
        status, _, _ = self.request("/api/session", method="POST", body={"code": "nope"})
        self.assertEqual(status, 403)

    def test_summary_accepts_control_token(self):
        status, body, _ = self.request(
            path="/api/summary", control_token="test-control-token")
        self.assertEqual(status, 200)
        self.assertEqual(usagesrv.json.loads(body)["stubbed"], True)

    def test_console_open_share_gated(self):
        # R9-R35: the console face is open-and-use locally (reads AND real
        # side-effect writes); the public share face keeps its credential
        # boundary (wrong/no control token -> 401).
        status, _, _ = self.request(path="/api/summary", control_token="wrong")
        self.assertEqual(status, 200)
        status, _, _ = self.request(
            path="/api/console/operations", method="POST",
            body={"action": "no.such", "target": "x"})
        self.assertEqual(status, 404)
        status, _, _ = self.request(
            path="/api/share/keys", method="POST", body={})
        self.assertEqual(status, 401)

    def test_unknown_path_generic_404(self):
        status, body, _ = self.request(path="/whatever")
        self.assertEqual(status, 404)
        self.assertNotIn(b"/whatever", body)  # 不回显请求路径

    def test_session_reissue_requires_control_token(self):
        status, _, _ = self.request(
            "/api/session/reissue", method="POST", control_token="wrong")
        self.assertEqual(status, 403)
        status, body, _ = self.request(
            "/api/session/reissue", method="POST",
            control_token="test-control-token")
        self.assertEqual(status, 200)
        code = usagesrv.json.loads(body)["code"]
        status, _, _ = self.request(
            "/api/session", method="POST", body={"code": code})
        self.assertEqual(status, 200)


class _FakeTime:
    """只替换 usagesrv 视角的 time.time，其余转发给真实 time 模块。"""

    def __init__(self, start):
        self._current = start

    def advance(self, seconds):
        self._current += seconds

    def time(self):
        return self._current

    def __getattr__(self, name):
        return getattr(usagesrv.time, name)


class SessionLifecycleTests(unittest.TestCase):
    """8001 会话生命周期：引导码一次性原子消费、短时有效，会话时效自成功
    兑换起算，控制台经管理令牌重新申请引导码（无需重启服务）。"""

    def setUp(self):
        with usagesrv._session_lock:
            usagesrv._session.update(
                code=None, code_expires=0.0, session=None, session_expires=0.0)
        self._real_time = usagesrv.time
        self.fake_time = _FakeTime(1_000_000.0)
        usagesrv.time = self.fake_time

    def tearDown(self):
        usagesrv.time = self._real_time
        with usagesrv._session_lock:
            usagesrv._session.update(
                code=None, code_expires=0.0, session=None, session_expires=0.0)

    def _cookie(self, sid):
        return "usage_session=" + sid

    def test_bootstrap_code_is_short_lived(self):
        code = usagesrv.session_bootstrap_code()
        self.fake_time.advance(usagesrv.SESSION_CODE_TTL_SECONDS - 5)
        self.assertIsNotNone(usagesrv.session_exchange(code))

    def test_expired_bootstrap_code_rejected_and_regenerable(self):
        code = usagesrv.session_bootstrap_code()
        self.fake_time.advance(usagesrv.SESSION_CODE_TTL_SECONDS + 5)
        self.assertIsNone(usagesrv.session_exchange(code))
        fresh = usagesrv.session_bootstrap_code()
        self.assertNotEqual(fresh, code)
        self.assertIsNotNone(usagesrv.session_exchange(fresh))

    def test_exchange_consumes_code_replay_rejected(self):
        code = usagesrv.session_bootstrap_code()
        sid = usagesrv.session_exchange(code)
        self.assertIsNotNone(sid)
        self.assertIsNone(usagesrv.session_exchange(code))  # 重放拒绝
        self.assertTrue(usagesrv.session_valid(self._cookie(sid)))

    def test_concurrent_exchanges_admit_exactly_one(self):
        import threading
        code = usagesrv.session_bootstrap_code()
        results = []
        lock = threading.Lock()

        def worker():
            outcome = usagesrv.session_exchange(code)
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        winners = [r for r in results if r is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(results), 8)

    def test_session_clock_starts_at_redemption(self):
        code = usagesrv.session_bootstrap_code()
        self.fake_time.advance(usagesrv.SESSION_CODE_TTL_SECONDS - 5)
        sid = usagesrv.session_exchange(code)
        self.assertIsNotNone(sid)
        self.fake_time.advance(usagesrv.SESSION_TTL_SECONDS - 60)
        self.assertTrue(usagesrv.session_valid(self._cookie(sid)))
        self.fake_time.advance(120)
        self.assertFalse(usagesrv.session_valid(self._cookie(sid)))

    def test_reissue_requires_token_and_invalidates_old_code(self):
        old = usagesrv.session_bootstrap_code()
        self.assertIsNone(usagesrv.reissue_session_code("wrong-token"))
        fresh = usagesrv.reissue_session_code("test-control-token")
        self.assertIsNotNone(fresh)
        self.assertNotEqual(fresh, old)
        self.assertIsNone(usagesrv.session_exchange(old))  # 旧码（含未过期）作废
        self.assertIsNotNone(usagesrv.session_exchange(fresh))

    def test_reissue_after_consumption_restores_login(self):
        code = usagesrv.session_bootstrap_code()
        self.assertIsNotNone(usagesrv.session_exchange(code))
        fresh = usagesrv.reissue_session_code("test-control-token")
        sid = usagesrv.session_exchange(fresh)
        self.assertIsNotNone(sid)
        self.assertTrue(usagesrv.session_valid(self._cookie(sid)))


if __name__ == "__main__":
    unittest.main()
