# -*- coding: utf-8 -*-
"""撤销/投影持久状态、有界通知池与终端可重试拒绝的回归测试。

钉死的契约（第三轮复审 #9—#15）：

* 状态文件读失败/缺失/损坏/版本不符是明确的 fail-closed 错误，不是空状态，
  也不是客户端取消；8110 返回可重试 503 + Retry-After + 稳定 error type；
* 首次可信初始化创建状态文件与私有初始化标记；已初始化后状态缺失/损坏
  不会被当成空状态，也不会覆盖内存缓存；
* 已应用的投影快照（key_id → enabled、generation）原子持久化（0600），
  8100 在 Key 禁用期间重启后，下一次 load 仍识别边界并补发 revoke/reenable；
* 通知发送使用进程级有界工作池：线程、loopback 连接与跨批次积压都有上限，
  整批保留一个总截止，慢连接/失败/退出都有界；
* generation 按真实通知时间窗裁剪（窗内迟到通知仍正确，窗外可安全回收），
  known 集合只保留当前投影 Key + 最近移除的 Key，长期增删有界；
* 尚未提交且未发送响应头的 GRANTED 请求撤销后终端收到既定 429 +
  Retry-After + error type；已发响应头的流式取消只关闭连接，不发送第二个
  HTTP 状态、不补成功。

隔离：不启动产品服务、不读写生产账本；HTTP 用例只绑定 loopback 临时端口，
撤销状态/投影/归因密钥都写在私有临时目录。
"""

import hashlib
import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parents[1]          # ai-gateway/
CHAT2API = HERE / "chat2api"
for _path in (HERE, CHAT2API):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


def _load(name, path):
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


share = _load("share_gateway", CHAT2API / "share_gateway.py")
dispatch = _load("share_dispatch", CHAT2API / "share_dispatch.py")
chat2api = _load("chat2api_revoke_state", CHAT2API / "chat2api.py")

SECRET = "s" * 48
KEY_ID = "eee3e921-f1ee-4b5a-8fc0-e4d2eb30135f"
PLAINTEXT_KEY = "um-share-plaintext-never-in-the-notice"
STUB_TOKEN = "stub-token"


def _write_private(path, text):
    """Fixture secrets must pass the POSIX 0600/current-uid gate."""
    from share_secret_protection import write_private_secret_path
    write_private_secret_path(Path(path), str(text).encode("utf-8"))


class TestRevokeStateInitialization(unittest.TestCase):
    """Review #12: trusted first init vs missing/corrupt after init."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = Path(self.tmp.name) / "revoke.state"
        self.marker = share._initialization_marker_path(self.state_file)
        self.state_patch = mock.patch.dict(
            os.environ, {share.REVOKE_STATE_ENV: str(self.state_file)})
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        share.reset_revoke_state_cache()
        self.addCleanup(share.reset_revoke_state_cache)

    def test_first_start_creates_state_and_private_marker(self):
        self.assertFalse(self.state_file.exists())
        self.assertFalse(self.marker.exists())
        self.assertEqual(share.load_revoke_state(),
                         {"nonces": {}, "generations": {}})
        self.assertTrue(self.state_file.exists())
        self.assertTrue(self.marker.exists())
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        marker = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(state["version"], share.REVOKE_STATE_VERSION)
        self.assertEqual(marker["version"], share.REVOKE_STATE_VERSION)
        if os.name == "posix":
            self.assertEqual(self.state_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.marker.stat().st_mode & 0o777, 0o600)

    def test_missing_state_after_initialization_fails_closed(self):
        # First load proves the trusted initialization; deleting the state
        # afterwards must NOT be treated as a fresh empty state.
        share.load_revoke_state()
        self.state_file.unlink()
        share.reset_revoke_state_cache()
        with self.assertRaises(share.RevokeStateUnavailable) as caught:
            share.load_revoke_state()
        message = str(caught.exception)
        self.assertIn(str(self.state_file), message)
        self.assertIn(str(self.marker), message)
        self.assertIn("restore", message)
        self.assertFalse(self.state_file.exists())
        self.assertFalse(share._revoke_state_loaded)

    def test_corrupt_state_after_initialization_keeps_the_memory_cache(self):
        share.record_revoke_generation(KEY_ID, 5, 1000)
        loaded = dict(share._revoke_applied_generations)
        self.state_file.write_bytes(b"{not-json")
        share._revoke_state_loaded = False
        with self.assertRaises(share.RevokeStateUnavailable):
            share.load_revoke_state()
        self.assertEqual(share._revoke_applied_generations, loaded)
        self.assertFalse(share._revoke_state_loaded)

    def test_marker_version_mismatch_fails_closed(self):
        share.load_revoke_state()
        self.marker.write_text(json.dumps({"version": 99}),
                               encoding="utf-8")
        share.reset_revoke_state_cache()
        with self.assertRaises(share.RevokeStateUnavailable):
            share.load_revoke_state()

    def test_legacy_state_without_marker_is_adopted_and_marked(self):
        # A state file written before the marker existed must not lock an
        # existing deployment out; it is adopted and then marked.
        self.state_file.write_text(json.dumps({
            "version": share.REVOKE_STATE_VERSION,
            "nonces": {},
            "generations": {KEY_ID: {
                "generation": 7, "applied_at": time.time(),
                "updated_at": time.time()}},
        }), encoding="utf-8")
        share.reset_revoke_state_cache()
        state = share.load_revoke_state()
        self.assertEqual(state["generations"][KEY_ID]["generation"], 7)
        self.assertTrue(self.marker.exists())

    def test_unavailable_state_is_retryable_503_not_a_client_cancel(self):
        # Review #9: the closed ticket names revoke_state_unavailable; it must
        # map to the retryable SERVICE failure, never client_disconnected.
        share.load_revoke_state()
        self.state_file.unlink()
        chat2api._reset_share_revoke_state()
        chat2api.set_request_facts(
            "chat", KEY_ID, "state-unavailable", "verified",
            attribution_ts=int(time.time()) + 1)
        self.assertEqual(chat2api._share_dispatch_refusal(KEY_ID),
                         "revoke_state_unavailable")
        dispatcher = dispatch.ShareDispatcher(max_wait=10)
        chat2api._share_dispatcher = dispatcher
        self.addCleanup(self._reset_dispatcher)
        ticket = chat2api.share_dispatch_ticket("chat")
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.state, "closed")
        self.assertEqual(ticket.reason, "revoke_state_unavailable")
        with self.assertRaises(chat2api._ShareDispatchStateUnavailable):
            chat2api._raise_share_dispatch_refusal(
                ticket.outcome, ticket.reason)
        with self.assertRaises(chat2api._ShareDispatchStateUnavailable):
            chat2api._await_share_send_turn(ticket, None, None, None)
        self.assertFalse(issubclass(chat2api._ShareDispatchStateUnavailable,
                                    chat2api._RequestCancelled))
        handler = chat2api.Handler.__new__(chat2api.Handler)
        captured = {}

        def capture(code, message, detail=None,
                    error_type="chat2api_error", extra_headers=None):
            captured.update(code=code, message=message, detail=detail,
                             error_type=error_type,
                             extra_headers=extra_headers)

        handler._error = capture
        handler._share_dispatch_state_unavailable_error()
        self.assertEqual(captured["code"], 503)
        self.assertEqual(captured["error_type"],
                         chat2api.SHARE_DISPATCH_STATE_UNAVAILABLE_STATUS)
        self.assertIn("Retry-After", captured["extra_headers"])

    @staticmethod
    def _reset_dispatcher():
        dispatcher = chat2api._share_dispatcher
        if dispatcher is not None:
            dispatcher.close_all("test")
        chat2api._share_dispatcher = None


class TestLegacyReleaseAdoption(unittest.TestCase):
    """The pre-fix state is adopted from EVERY release directory, ONCE.

    After an upgrade, ``current`` points at a NEW release whose own ``data``
    directory is empty; the pending generation still lives in the release that
    ran before the switch.  Adoption must cover the sibling release
    directories, never overwrite an existing new state, and never resurrect the
    legacy files after the one-time adoption.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state_file = self.root / "state" / "share-revoke.state"
        self.current_legacy = (self.root / "releases" / "rel-current"
                               / "data" / "share-revoke.state")
        self.old_legacy = (self.root / "releases" / "rel-old"
                           / "data" / "share-revoke.state")
        self.default_patch = mock.patch.object(
            share, "DEFAULT_REVOKE_STATE_FILE", self.state_file)
        self.default_patch.start()
        self.addCleanup(self.default_patch.stop)
        self.legacy_patch = mock.patch.object(
            share, "LEGACY_REVOKE_STATE_FILE", self.current_legacy)
        self.legacy_patch.start()
        self.addCleanup(self.legacy_patch.stop)
        self.env_patch = mock.patch.dict(
            os.environ, {share.REVOKE_STATE_ENV: str(self.state_file)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        share.reset_revoke_state_cache()
        self.addCleanup(share.reset_revoke_state_cache)

    @staticmethod
    def _legacy_state(generation, nonce=None, projection_generation=0):
        now = time.time()
        return {
            "version": share.REVOKE_STATE_VERSION,
            "nonces": ({nonce: int(now)} if nonce else {}),
            "generations": {KEY_ID: {
                "generation": generation, "applied_at": now,
                "updated_at": now}},
            "projection_generation": projection_generation,
        }

    def _write(self, path, state):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")

    def test_old_release_sibling_state_is_adopted(self):
        self._write(self.current_legacy, self._legacy_state(3, "nonce-current"))
        self._write(self.old_legacy, self._legacy_state(5, "nonce-old"))
        state = share.load_revoke_state()
        # The newest generation across the merged files wins, and the replay
        # registry is the union: no pending generation disappears.
        self.assertEqual(state["generations"][KEY_ID]["generation"], 5)
        self.assertEqual(set(state["nonces"]), {"nonce-current", "nonce-old"})
        self.assertTrue(self.state_file.exists())
        self.assertTrue(
            share._legacy_adoption_marker_path(self.state_file).exists())
        # The legacy files are sources only: never modified.
        untouched = json.loads(self.old_legacy.read_text(encoding="utf-8"))
        self.assertEqual(untouched["generations"][KEY_ID]["generation"], 5)
        self.assertEqual(set(untouched["nonces"]), {"nonce-old"})

    def test_adoption_merges_the_projection_watermark(self):
        self._write(self.current_legacy,
                     self._legacy_state(3, projection_generation=4))
        self._write(self.old_legacy,
                     self._legacy_state(5, projection_generation=7))
        share.load_revoke_state()
        self.assertEqual(share.applied_security_generation(), 7)

    def test_adoption_happens_once_and_never_resurrects(self):
        self._write(self.old_legacy, self._legacy_state(5))
        self.assertEqual(
            share.load_revoke_state()["generations"][KEY_ID]["generation"], 5)
        share.reset_revoke_state_cache()
        self.state_file.unlink()
        share._initialization_marker_path(self.state_file).unlink()
        # The sibling adoption marker still proves the one-time adoption: an
        # intentional reinitialization starts empty instead of resurrecting the
        # stale release file.
        state = share.load_revoke_state()
        self.assertEqual(state["generations"], {})
        self.assertTrue(self.state_file.exists())

    def test_existing_new_state_is_never_overwritten(self):
        now = time.time()
        self._write(self.state_file, {
            "version": share.REVOKE_STATE_VERSION,
            "nonces": {},
            "generations": {KEY_ID: {
                "generation": 9, "applied_at": now, "updated_at": now}},
            "projection_generation": 9,
        })
        self._write(self.current_legacy, self._legacy_state(5))
        state = share.load_revoke_state()
        self.assertEqual(state["generations"][KEY_ID]["generation"], 9)
        self.assertEqual(share.applied_security_generation(), 9)

    def test_corrupt_old_release_state_fails_closed(self):
        self.old_legacy.parent.mkdir(parents=True)
        self.old_legacy.write_text("{broken", encoding="utf-8")
        with self.assertRaises(share.RevokeStateUnavailable):
            share.load_revoke_state()
        self.assertFalse(self.state_file.exists())


class TestProjectionSnapshotRecovery(unittest.TestCase):
    """Review #11/#12/#15: applied projection survives an 8100 restart."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "projection.json"
        self.snapshot = share.revoke_projection_path(self.config)
        self.marker = share._initialization_marker_path(self.snapshot)
        # A restart now merges the persisted revoke state and records the
        # applied projection generation, so this test owns a private state file
        # instead of the PRODUCT default.
        self.state_file = Path(self.tmp.name) / "revoke.state"
        self.state_patch = mock.patch.dict(
            os.environ, {share.REVOKE_STATE_ENV: str(self.state_file)})
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        share.reset_revoke_state_cache()
        self.addCleanup(share.reset_revoke_state_cache)
        self.entry = {
            "key_id": KEY_ID, "name": "friend",
            "secret_hash": hashlib.sha256(b"secret").hexdigest(),
            "secret_version": 1, "enabled": True,
            "max_concurrency": 4,
        }
        self._write(generation=1, enabled=True)

    def _write(self, *, generation, enabled, secret_version=1,
               max_concurrency=4, share_max_concurrency=8):
        entry = dict(self.entry, enabled=enabled,
                     secret_version=secret_version,
                     max_concurrency=max_concurrency)
        self.config.write_text(json.dumps({
            "schema_version": 2,
            "config_generation": generation,
            "share_max_concurrency": share_max_concurrency,
            "data": [entry],
        }), encoding="utf-8")

    def _server(self):
        server = share.ShareServer(("127.0.0.1", 0), self.config)
        self.addCleanup(server.server_close)
        return server

    def _signed_load(self, server, *, generation, enabled, secret_version=1,
                     max_concurrency=4, share_max_concurrency=8):
        from share_broker_protocol import INSTANCE_SIGNED_KEYS, sign
        self._write(generation=generation, enabled=enabled,
                     secret_version=secret_version,
                     max_concurrency=max_concurrency,
                     share_max_concurrency=share_max_concurrency)
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        secret = "inst-secret"
        fields = {
            "generation": generation,
            "fingerprint": hashlib.sha256(json.dumps(
                payload, sort_keys=True).encode()).hexdigest(),
        }
        body = sign(secret, {
            "v": 2, "action": "load",
            "request_id": "req-load-%d-%d" % (generation, time.time_ns()),
            "ts": int(time.time()) + 1,
            "instance_id": server.instance_id,
            "fields": fields,
        }, INSTANCE_SIGNED_KEYS)
        return server.handle_instance_control(body, secret)

    def test_first_start_writes_snapshot_and_private_marker(self):
        self.assertFalse(self.snapshot.exists())
        server = self._server()
        self.assertTrue(self.snapshot.exists())
        self.assertTrue(self.marker.exists())
        snapshot = json.loads(self.snapshot.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["version"],
                         share.REVOKE_PROJECTION_VERSION)
        self.assertEqual(snapshot["generation"], 1)
        self.assertEqual(snapshot["keys"], {KEY_ID: True})
        if os.name == "posix":
            self.assertEqual(self.snapshot.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.marker.stat().st_mode & 0o777, 0o600)

    def test_restart_while_disabled_still_sends_reenable_notice(self):
        server = self._server()
        first_notices = []
        server._send_revoke_notices = first_notices.extend
        ack = self._signed_load(server, generation=2, enabled=False)
        self.assertTrue(ack["ok"], ack)
        self.assertEqual(first_notices, [{
            "key_id": KEY_ID, "generation": 2, "action": "revoke"}])
        self.assertEqual(server._projection_enabled, set())
        self.assertIn(KEY_ID, server._projection_known)

        # Restart 8100 while the Key is still disabled.
        server.server_close()
        restarted = self._server()
        self.assertEqual(restarted._projection_generation, 2)
        self.assertEqual(restarted._projection_enabled, set())
        self.assertIn(KEY_ID, restarted._projection_known)

        reenable_notices = []
        restarted._send_revoke_notices = reenable_notices.extend
        ack = self._signed_load(restarted, generation=3, enabled=True)
        self.assertTrue(ack["ok"], ack)
        self.assertEqual(reenable_notices, [{
            "key_id": KEY_ID, "generation": 3, "action": "reenable"}])
        self.assertEqual(restarted._projection_enabled, {KEY_ID})

    def test_restart_acks_the_generation_it_already_applied(self):
        # The restart bug: the projection snapshot was already generation 3, the
        # retried load short-circuited and the in-memory watermark was empty,
        # so the ACK reported 0 and the security journal stayed pending.  The
        # persisted state must be merged and the applied generation ACKed.
        server = self._server()
        ack = self._signed_load(server, generation=3, enabled=False)
        self.assertTrue(ack["ok"], ack)
        self.assertEqual(ack["result"]["security_generation"], 3)
        self.assertEqual(share.load_revoke_generation(KEY_ID)["generation"], 3)

        server.server_close()
        share.reset_revoke_state_cache()   # process restart: memory is empty
        restarted = self._server()
        self.assertEqual(share.applied_security_generation(), 3)
        retry = self._signed_load(restarted, generation=3, enabled=False)
        self.assertTrue(retry["ok"], retry)
        self.assertEqual(retry["result"]["security_generation"], 3)

    def test_ack_merges_the_persisted_state_after_a_cache_loss(self):
        # The in-process cache can be empty even though the state file is
        # durable; the ACK itself must merge it instead of answering 0.
        server = self._server()
        ack = self._signed_load(server, generation=2, enabled=False)
        self.assertTrue(ack["ok"], ack)
        self.assertEqual(ack["result"]["security_generation"], 2)
        share.reset_revoke_state_cache()
        retry = self._signed_load(server, generation=2, enabled=False)
        self.assertTrue(retry["ok"], retry)
        self.assertEqual(retry["result"]["security_generation"], 2)

    def test_rotate_records_an_ackable_generation(self):
        # A rotate leaves the Key enabled, so it never appears in the revoked
        # set; the applied projection generation is what covers it.
        server = self._server()
        ack = self._signed_load(server, generation=2, enabled=True,
                                 secret_version=2)
        self.assertTrue(ack["ok"], ack)
        self.assertEqual(ack["result"]["security_generation"], 2)
        self.assertEqual(share.load_revoke_generation(KEY_ID)["generation"], 2)

    def test_limit_lower_records_an_ackable_generation(self):
        # Lowering max_concurrency is a revoke-direction change that also
        # leaves the Key enabled.
        server = self._server()
        ack = self._signed_load(server, generation=2, enabled=True,
                                 max_concurrency=2)
        self.assertTrue(ack["ok"], ack)
        self.assertEqual(ack["result"]["security_generation"], 2)
        self.assertEqual(share.load_revoke_generation(KEY_ID)["generation"], 2)

    def test_global_limit_lower_records_an_ackable_generation(self):
        # The global limit intent has key_id None, so it has no per-Key entry;
        # the applied projection generation is its ACK watermark.
        server = self._server()
        ack = self._signed_load(server, generation=2, enabled=True,
                                 share_max_concurrency=2)
        self.assertTrue(ack["ok"], ack)
        self.assertEqual(ack["result"]["security_generation"], 2)
        self.assertEqual(share.applied_security_generation(), 2)

    def test_missing_snapshot_after_initialization_fails_closed(self):
        server = self._server()
        server.server_close()
        self.snapshot.unlink()
        with self.assertRaises(share.RevokeStateUnavailable) as caught:
            self._server()
        message = str(caught.exception)
        self.assertIn(str(self.snapshot), message)
        self.assertIn(str(self.marker), message)
        self.assertFalse(self.snapshot.exists())

    def test_corrupt_snapshot_fails_closed(self):
        server = self._server()
        server.server_close()
        self.snapshot.write_bytes(b"{broken")
        with self.assertRaises(share.RevokeStateUnavailable):
            self._server()
        self.assertEqual(self.snapshot.read_bytes(), b"{broken")

    def test_known_set_is_bounded_after_long_churn(self):
        # Review #15: current projection Keys + the most recently removed Keys,
        # evicted by last-seen order; Key churn cannot grow it forever.
        server = share.ShareServer.__new__(share.ShareServer)
        server._projection_generation = 0
        server._projection_enabled = set()
        server._projection_known = OrderedDict()
        limit = share.REVOKE_PROJECTION_KNOWN_MAX
        # The applied-generation writes are not the subject of this bounding
        # test; keep them out of the PRODUCT default state file and out of the
        # 4096-iteration loop (they would fsync once per generation).
        for name in ("record_projection_generation", "record_revoke_generation"):
            generation_patch = mock.patch.object(
                share, name, return_value=False)
            generation_patch.start()
            self.addCleanup(generation_patch.stop)
        for index in range(limit + 1000):
            server._projection_revoke_transitions_locked({
                "config_generation": index + 1,
                "data": [{"key_id": "k%d" % index, "enabled": True}],
            })
        known = list(server._projection_known)
        self.assertLessEqual(len(known), limit)
        self.assertIn("k%d" % (limit + 999), known)
        self.assertIn("k%d" % (limit + 998), known)
        self.assertNotIn("k0", known)


class TestRevokeGenerationWindow(unittest.TestCase):
    """Review #14: eviction follows the notice window, not a size cap."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = Path(self.tmp.name) / "revoke.state"
        self.state_patch = mock.patch.dict(
            os.environ, {share.REVOKE_STATE_ENV: str(self.state_file)})
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        share.reset_revoke_state_cache()
        self.addCleanup(share.reset_revoke_state_cache)
        chat2api._reset_share_revoke_state()
        self.addCleanup(chat2api._reset_share_revoke_state)

    def test_generation_inside_the_window_is_never_evicted(self):
        now = time.time()
        with share._revoke_state_lock:
            share._revoke_applied_generations.clear()
            share._revoke_applied_generations[KEY_ID] = {
                "generation": 5, "applied_at": now,
                "updated_at": now - share.REVOKE_STATE_GENERATIONS_TTL_S + 1}
            share._prune_revoke_generations_locked(now=now)
            self.assertIn(KEY_ID, share._revoke_applied_generations)
            share._revoke_applied_generations[KEY_ID]["updated_at"] = (
                now - share.REVOKE_STATE_GENERATIONS_TTL_S - 1)
            share._prune_revoke_generations_locked(now=now)
            self.assertNotIn(KEY_ID, share._revoke_applied_generations)

    def test_late_notice_inside_the_window_is_still_stale(self):
        # A notice whose timestamp is still inside the 300s window must be
        # treated as stale against the newer applied generation, even after
        # many other Keys were applied.
        class _FakeDispatcher:
            def __init__(self):
                self.calls = []

            def revoke_key(self, key_id, reason="revoke"):
                if key_id == KEY_ID:
                    self.calls.append((key_id, reason))
                # D18-3: the dispatcher reports (released, aborted).
                return 1, 0

        dispatcher = _FakeDispatcher()
        chat2api._share_dispatcher = dispatcher
        now = int(time.time())
        first = share.build_revoke_notice(
            SECRET, KEY_ID, 5, now=now, nonce="a" * 32)
        status, payload = chat2api.apply_share_revoke_notice(
            first, SECRET, now=now)
        self.assertEqual((status, payload["released"]), (200, 1))
        for index in range(4200):
            other = share.build_revoke_notice(
                SECRET, "other-%d" % index, 2, now=now,
                nonce="%032x" % (index + 1))
            with mock.patch.object(share, "record_revoke_nonce",
                                   return_value=True), \
                    mock.patch.object(share, "record_revoke_generation",
                                      return_value=None):
                status, payload = chat2api.apply_share_revoke_notice(
                    other, SECRET, now=now)
                self.assertEqual(status, 200, payload)
        late = share.build_revoke_notice(
            SECRET, KEY_ID, 4, now=now - 100, nonce="b" * 32)
        status, payload = chat2api.apply_share_revoke_notice(
            late, SECRET, now=now)
        self.assertEqual(status, 200)
        self.assertEqual(payload["ignored"], "stale_generation")
        self.assertEqual(payload["released"], 0)
        self.assertEqual(len(dispatcher.calls), 1)
        self.assertIn(KEY_ID, chat2api._revoke_applied_generations)


class TestAppliedSecurityGenerationWatermark(unittest.TestCase):
    """S2 load ACK / snapshot watermark: ONE int, never ``max`` over dicts.

    ``record_revoke_generation`` stores a per-Key dict
    (generation/applied_at/updated_at).  The ACK and the snapshot must
    report the MAX ``generation`` as an integer: the old
    ``max(_revoke_applied_generations.values())`` returned a dict for ONE
    revoke and raised TypeError for TWO, which the instance channel
    answered as ``internal_error`` instead of the load ACK.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state_file = self.root / "revoke.state"
        self.state_patch = mock.patch.dict(
            os.environ, {share.REVOKE_STATE_ENV: str(self.state_file)})
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        share.reset_revoke_state_cache()
        self.addCleanup(share.reset_revoke_state_cache)
        # A trusted initialization: the later record_* calls must not re-create
        # the state file and drop the injected entries.
        share.load_revoke_state()

    def _record(self, generations):
        for key_id, generation in generations.items():
            share.record_revoke_generation(key_id, generation, 1000)

    def _server(self):
        config = self.root / "projection.json"
        config.write_text(json.dumps({
            "schema_version": 2,
            "config_generation": 4,
            "share_max_concurrency": 8,
            "data": [{
                "key_id": KEY_ID, "name": "friend",
                "secret_hash": hashlib.sha256(b"secret").hexdigest(),
                "secret_version": 1, "enabled": True,
                "max_concurrency": 4,
            }],
        }), encoding="utf-8")
        server = share.ShareServer(("127.0.0.1", 0), config)
        self.addCleanup(server.server_close)
        return server

    def _ack(self, server):
        """The REAL signed instance-channel load (handle_instance_control)."""
        from share_broker_protocol import INSTANCE_SIGNED_KEYS, sign
        payload = json.loads(server.config.read_text(encoding="utf-8"))
        secret = "inst-secret"
        body = sign(secret, {
            "v": 2, "action": "load",
            "request_id": "req-load-%d" % time.time_ns(),
            "ts": int(time.time()) + 1,
            "instance_id": server.instance_id,
            "fields": {
                "generation": payload["config_generation"],
                "fingerprint": hashlib.sha256(json.dumps(
                    payload, sort_keys=True).encode()).hexdigest(),
            },
        }, INSTANCE_SIGNED_KEYS)
        return server.handle_instance_control(body, secret)

    def _assert_watermark(self, expected):
        server = self._server()
        self.assertEqual(share.applied_security_generation(), expected)
        snapshot = server.snapshot()
        self.assertEqual(snapshot["security_generation"], expected)
        self.assertIs(type(snapshot["security_generation"]), int)
        ack = self._ack(server)
        self.assertTrue(ack["ok"], ack)
        self.assertEqual(ack["error_code"], "ok")
        self.assertEqual(ack["result"]["security_generation"], expected)
        self.assertIs(type(ack["result"]["security_generation"]), int)

    def test_zero_applied_revokes_report_zero(self):
        self._assert_watermark(0)

    def test_one_applied_revoke_reports_its_generation(self):
        self._record({KEY_ID: 7})
        self._assert_watermark(7)

    def test_two_applied_revokes_report_the_max_generation(self):
        # The exact defect: two per-Key dicts made max() raise TypeError and
        # the real load ACK answered internal_error.
        self._record({KEY_ID: 5, "other-key": 9})
        self._assert_watermark(9)

    def test_many_applied_revokes_report_the_max_generation(self):
        generations = {"key-%02d" % index: 3 + (index * 7) % 41
                       for index in range(40)}
        self._record(generations)
        self._assert_watermark(max(generations.values()))

    def test_legacy_int_entries_are_counted_with_dicts(self):
        with share._revoke_state_lock:
            share._revoke_applied_generations.clear()
            share._revoke_applied_generations.update({
                "legacy-int": 6,
                "dict-entry": {"generation": 4, "applied_at": 0,
                               "updated_at": 0}})
        self._assert_watermark(6)

    def test_malformed_entries_never_crash_the_watermark(self):
        with share._revoke_state_lock:
            share._revoke_applied_generations.clear()
            share._revoke_applied_generations.update({
                "missing": {"applied_at": 0, "updated_at": 0},
                "negative": {"generation": -3, "applied_at": 0,
                             "updated_at": 0},
                "bool": {"generation": True, "applied_at": 0,
                         "updated_at": 0},
                "text": {"generation": "9", "applied_at": 0,
                         "updated_at": 0},
                "good": {"generation": 8, "applied_at": 0,
                         "updated_at": 0},
            })
        self._assert_watermark(8)


class _SlowNoticeHandler(BaseHTTPRequestHandler):
    """Accept a notice connection, hold it, then answer after the bound."""

    active = 0
    peak = 0
    lock = threading.Lock()

    def log_message(self, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        with type(self).lock:
            type(self).active += 1
            type(self).peak = max(type(self).peak, type(self).active)
        try:
            time.sleep(0.5)
        finally:
            with type(self).lock:
                type(self).active -= 1
        try:
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass


class _TrackedConnection:
    """Count the CLIENT loopback connections a sender opens at once."""

    active = 0
    peak = 0
    lock = threading.Lock()
    original = None

    def __init__(self, host, port, timeout=None):
        self._connection = type(self).original(
            host, port, timeout=timeout)
        with type(self).lock:
            type(self).active += 1
            type(self).peak = max(type(self).peak, type(self).active)

    def request(self, *args, **kwargs):
        return self._connection.request(*args, **kwargs)

    def getresponse(self):
        return self._connection.getresponse()

    def close(self):
        try:
            self._connection.close()
        finally:
            with type(self).lock:
                type(self).active -= 1


class TestRevokeNoticeWorkerBounds(unittest.TestCase):
    """Review #10: bounded workers/connections and cross-batch backlog."""

    @staticmethod
    def _drain_slots(timeout=5.0):
        slots = share._revoke_notice_worker_slots
        acquired = []
        try:
            for _ in range(share.REVOKE_NOTICE_MAX_CONCURRENCY):
                if not slots.acquire(timeout=timeout):
                    return False
                acquired.append(True)
            return True
        finally:
            for _ in acquired:
                slots.release()

    def setUp(self):
        self.assertTrue(self._drain_slots())
        self.addCleanup(self._drain_slots)

    def _sender(self, port):
        sender = share.ShareServer.__new__(share.ShareServer)
        sender.upstream_port = port
        sender.attribution_secret = SECRET
        sender.fault_ring = deque(maxlen=share.FAULT_RING_MAX)
        sender.fault_log = None
        return sender

    def test_slow_connections_and_threads_stay_bounded(self):
        _SlowNoticeHandler.active = 0
        _SlowNoticeHandler.peak = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0),
                                     _SlowNoticeHandler)
        server.daemon_threads = True
        server.request_queue_size = 32
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        sender = self._sender(server.server_address[1])
        notices = [{"key_id": "slow-%d" % index, "generation": 20,
                    "action": "revoke"} for index in range(20)]
        _TrackedConnection.original = share.http.client.HTTPConnection
        _TrackedConnection.active = 0
        _TrackedConnection.peak = 0
        with mock.patch.object(share.http.client, "HTTPConnection",
                                _TrackedConnection), \
                mock.patch.object(share, "REVOKE_NOTICE_TIMEOUT_S", 0.2), \
                mock.patch.object(share, "REVOKE_NOTICE_RETRY_DELAY_S", 0.05), \
                mock.patch.object(share, "REVOKE_NOTICE_BATCH_DEADLINE_S",
                                  0.8):
            started = time.monotonic()
            sender._send_revoke_notices(notices)
            elapsed = time.monotonic() - started
        self.assertLessEqual(elapsed, 1.3)
        self.assertLessEqual(_TrackedConnection.peak,
                             share.REVOKE_NOTICE_MAX_CONCURRENCY)
        self.assertGreaterEqual(_TrackedConnection.peak, 1)
        self.assertLessEqual(len(sender.fault_ring), len(notices))
        self.assertTrue(all(
            record["code"] in ("share_revoke_notice_timeout",
                                "share_revoke_notice_failed")
            for record in sender.fault_ring))

    def test_cross_batch_backlog_is_bounded(self):
        # One batch's four slow workers are still alive when the next batch
        # starts: the second batch may NOT create more threads/connections.
        sender = self._sender(1)
        release = threading.Event()
        self.addCleanup(release.set)
        active = 0
        peak = 0
        lock = threading.Lock()

        def slow_notice(*_args, **_kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                release.wait(5.0)
            finally:
                with lock:
                    active -= 1
            return True

        sender._send_revoke_notice = slow_notice
        first_notices = [{"key_id": "first-%d" % index, "generation": 30,
                          "action": "revoke"} for index in range(8)]
        second_notices = [{"key_id": "second-%d" % index, "generation": 31,
                           "action": "revoke"} for index in range(4)]
        first_thread = threading.Thread(
            target=sender._send_revoke_notices, args=(first_notices,),
            daemon=True)
        first_thread.start()
        deadline = time.monotonic() + 5.0
        while active < share.REVOKE_NOTICE_MAX_CONCURRENCY:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        second_thread = threading.Thread(
            target=sender._send_revoke_notices, args=(second_notices,),
            daemon=True)
        with mock.patch.object(share, "REVOKE_NOTICE_BATCH_DEADLINE_S", 0.3):
            started = time.monotonic()
            second_thread.start()
            second_thread.join(timeout=2.0)
            elapsed = time.monotonic() - started
        self.assertFalse(second_thread.is_alive())
        self.assertLess(elapsed, 1.0)
        self.assertLessEqual(peak, share.REVOKE_NOTICE_MAX_CONCURRENCY)
        live = [item for item in threading.enumerate()
                if item.name == "share-revoke-notice"]
        self.assertLessEqual(len(live),
                             share.REVOKE_NOTICE_MAX_CONCURRENCY)
        self.assertGreaterEqual(len(sender.fault_ring), 1)
        self.assertTrue(all(
            record["code"] == "share_revoke_notice_timeout"
            for record in sender.fault_ring))
        release.set()
        first_thread.join(timeout=5.0)
        self.assertFalse(first_thread.is_alive())

    def test_worker_start_failure_releases_the_slot(self):
        sender = self._sender(1)
        original_start = threading.Thread.start
        calls = []

        def start(thread):
            if thread.name == "share-revoke-notice" and not calls:
                calls.append(thread)
                raise RuntimeError("cannot start")
            return original_start(thread)

        with mock.patch.object(threading.Thread, "start", autospec=True,
                                side_effect=start):
            sender._send_revoke_notices([{
                "key_id": KEY_ID, "generation": 9, "action": "revoke"}])
        self.assertTrue(calls)
        self.assertTrue(any(
            record["code"] in ("share_revoke_notice_failed",
                                 "share_revoke_notice_timeout")
            for record in sender.fault_ring))
        self.assertTrue(self._drain_slots())

    def test_workers_exit_after_the_deadline_and_release_slots(self):
        sender = self._sender(1)
        release = threading.Event()
        self.addCleanup(release.set)
        sender._send_revoke_notice = (
            lambda *_args, **_kwargs: release.wait(5.0) or True)
        notices = [{"key_id": "exit-%d" % index, "generation": 40,
                    "action": "revoke"} for index in range(4)]
        with mock.patch.object(share, "REVOKE_NOTICE_BATCH_DEADLINE_S", 0.2):
            sender._send_revoke_notices(notices)
        self.assertGreaterEqual(len(sender.fault_ring), 1)
        self.assertTrue(all(
            record["code"] == "share_revoke_notice_timeout"
            for record in sender.fault_ring))
        release.set()
        self.assertTrue(self._drain_slots(timeout=5.0))


class _DispatchRefusalUpstream(BaseHTTPRequestHandler):
    """Answer ONE upstream relay with a configured dispatch refusal."""

    status = [429]
    error_type = ["share_dispatch_revoked"]
    retry_after = ["1"]

    def log_message(self, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        payload = json.dumps({"error": {
            "message": "dispatch refused",
            "type": type(self).error_type[0]}}).encode()
        self.send_response(type(self).status[0])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if type(self).retry_after[0] is not None:
            self.send_header("Retry-After", type(self).retry_after[0])
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)


class TestTerminalDispatchRefusal(unittest.TestCase):
    """Review #13: the terminal sees 429/503, never a bare close."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "clients.json"
        self.config.write_text(json.dumps({"clients": {
            "friend": hashlib.sha256(b"test-secret").hexdigest()}}),
            encoding="utf-8")
        _DispatchRefusalUpstream.status = [429]
        _DispatchRefusalUpstream.error_type = ["share_dispatch_revoked"]
        _DispatchRefusalUpstream.retry_after = ["1"]
        self.upstream = ThreadingHTTPServer(
            ("127.0.0.1", 0), _DispatchRefusalUpstream)
        self.upstream.daemon_threads = True
        self.server = share.ShareServer(
            ("127.0.0.1", 0), self.config,
            self.upstream.server_address[1])
        self.threads = []
        for server in (self.upstream, self.server):
            thread = threading.Thread(target=server.serve_forever,
                                      daemon=True)
            thread.start()
            self.threads.append(thread)

    def tearDown(self):
        for server in (self.server, self.upstream):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=5)
        self.tmp.cleanup()

    def _post(self):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5)
        try:
            body = json.dumps({
                "model": "chat-model",
                "messages": [{"role": "user", "content": "hi"}],
            }).encode()
            conn.request(
                "POST", "/v1/chat/completions", body=body,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer test-secret"})
            response = conn.getresponse()
            return (response.status, response.getheader("Retry-After"),
                    json.loads(response.read()))
        finally:
            conn.close()

    def test_revoked_granted_request_reaches_the_terminal_as_429(self):
        status, retry_after, payload = self._post()
        self.assertEqual(status, 429)
        self.assertEqual(retry_after, "1")
        self.assertEqual(payload["error"]["type"], "share_dispatch_revoked")

    def test_state_unavailable_reaches_the_terminal_as_503(self):
        _DispatchRefusalUpstream.status = [503]
        _DispatchRefusalUpstream.error_type = [
            "share_dispatch_state_unavailable"]
        status, retry_after, payload = self._post()
        self.assertEqual(status, 503)
        self.assertEqual(retry_after, "1")
        self.assertEqual(payload["error"]["type"],
                         "share_dispatch_state_unavailable")

    def test_unknown_refusal_body_keeps_the_um_unavailable_mapping(self):
        _DispatchRefusalUpstream.status = [429]
        _DispatchRefusalUpstream.error_type = ["unknown_type"]
        status, _retry_after, payload = self._post()
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["type"], "share_gateway_error")

    def test_started_stream_cancel_closes_without_a_second_status(self):
        class _Connection:
            def __init__(self):
                self.closed = False
                self.shutdown_called = False

            def shutdown(self, _how):
                self.shutdown_called = True

            def close(self):
                self.closed = True

        connection = _Connection()
        context = share._InflightCtx(connection, retryable_refusal=True)
        context.mark_response_started()
        self.assertTrue(context.terminate())
        self.assertTrue(connection.shutdown_called)
        self.assertTrue(connection.closed)
        self.assertTrue(context.terminated)
        # A second terminate never sends a second status or reopens.
        self.assertFalse(context.terminate())

    def test_no_header_dispatch_cancel_answers_429_instead_of_closing(self):
        class _Connection:
            def __init__(self):
                self.closed = False

            def shutdown(self, _how):
                self.closed = True

            def close(self):
                self.closed = True

        connection = _Connection()
        context = share._InflightCtx(connection, retryable_refusal=True)
        self.assertTrue(context.revoke())
        self.assertFalse(connection.closed)
        self.assertTrue(context.terminated)
        handler = share.ShareHandler.__new__(share.ShareHandler)
        captured = {}

        def capture(status, code, **kwargs):
            captured.update(status=status, code=code, **kwargs)

        handler.error = capture
        handler._dispatch_refusal_error()
        self.assertEqual(captured["status"], 429)
        self.assertEqual(captured["code"], "share_dispatch_revoked")
        self.assertEqual(captured["error_type"], "share_dispatch_revoked")
        self.assertEqual(captured["retry_after"],
                         share.SHARE_DISPATCH_RETRY_AFTER_S)

    def test_granted_not_committed_proxy_call_is_a_retryable_429(self):
        dispatcher = dispatch.ShareDispatcher(max_wait=10)
        ticket = dispatcher.register(KEY_ID, "granted-before-commit")
        self.assertEqual(dispatcher.poll(ticket), dispatch.OUTCOME_GRANTED)
        dispatcher.revoke_key(KEY_ID)
        args = SimpleNamespace(
            base_url="http://127.0.0.1:1", token=STUB_TOKEN,
            profile="profile", login_timeout=1, use_api_key=False,
            no_browser=True)
        proxy = chat2api.Proxy({"token": STUB_TOKEN}, args)
        with self.assertRaises(chat2api._ShareDispatchRevoked):
            proxy.call("POST", "/api/chat/completions",
                       json={"model": "chat-model", "messages": []},
                       dispatch_ticket=ticket)
        handler = chat2api.Handler.__new__(chat2api.Handler)
        captured = {}

        def capture(code, message, detail=None,
                    error_type="chat2api_error", extra_headers=None):
            captured.update(code=code, error_type=error_type,
                             extra_headers=extra_headers)

        handler._error = capture
        handler._share_dispatch_revoked_error()
        self.assertEqual(captured["code"], 429)
        self.assertEqual(captured["error_type"], "share_dispatch_revoked")
        self.assertIn("Retry-After", captured["extra_headers"])


class _PendingStubUpstream(BaseHTTPRequestHandler):
    """An upstream that must never be reached by a revoked request."""

    arrivals = []

    def log_message(self, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        type(self).arrivals.append(self.path)
        payload = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class TestPendingRevokeAtTheTerminal(unittest.TestCase):
    """Review #13: a PENDING ticket revoked before its turn answers 429."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = Path(self.tmp.name) / "revoke.state"
        self.state_patch = mock.patch.dict(
            os.environ, {share.REVOKE_STATE_ENV: str(self.state_file)})
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        chat2api._reset_share_revoke_state()
        self.addCleanup(chat2api._reset_share_revoke_state)

        self.key_file = Path(self.tmp.name) / "attribution.key"
        _write_private(self.key_file, SECRET)
        self.key_patch = mock.patch.object(
            chat2api, "SHARE_ATTRIBUTION_KEY_FILE", str(self.key_file))
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        self._reset_attribution_cache()
        self.addCleanup(self._reset_attribution_cache)

        self.role_patch = mock.patch.object(
            chat2api, "SHARE_BACKEND_ROLE", True)
        self.role_patch.start()
        self.addCleanup(self.role_patch.stop)

        self.upstream = ThreadingHTTPServer(
            ("127.0.0.1", 0), _PendingStubUpstream)
        self.upstream.daemon_threads = True
        upstream_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True)
        upstream_thread.start()
        self.addCleanup(self._stop_upstream, upstream_thread)

        args = SimpleNamespace(
            base_url=f"http://127.0.0.1:{self.upstream.server_address[1]}",
            token=STUB_TOKEN, profile="profile", login_timeout=1,
            use_api_key=False, no_browser=True)
        self.proxy = chat2api.Proxy({"token": STUB_TOKEN}, args)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          chat2api.Handler)
        self.server.daemon_threads = True
        self.server.proxy = self.proxy
        server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        server_thread.start()
        self.addCleanup(self._stop_server, server_thread)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

        self.dispatcher = dispatch.ShareDispatcher(max_wait=10)
        chat2api._share_dispatcher = self.dispatcher
        self.addCleanup(self._reset_dispatcher)
        _PendingStubUpstream.arrivals = []
        self.usage_rows = []
        self.usage_patch = mock.patch.object(
            chat2api, "record_usage",
            side_effect=lambda *args, **kwargs: self.usage_rows.append(
                kwargs))
        self.usage_patch.start()
        self.addCleanup(self.usage_patch.stop)

    @staticmethod
    def _reset_attribution_cache():
        chat2api._attribution_key_cache["stat"] = None
        chat2api._attribution_key_cache["secret"] = None

    @staticmethod
    def _reset_dispatcher():
        dispatcher = chat2api._share_dispatcher
        if dispatcher is not None:
            dispatcher.close_all("test")
        chat2api._share_dispatcher = None

    def _stop_upstream(self, thread):
        self.upstream.shutdown()
        self.upstream.server_close()
        thread.join(timeout=5)

    def _stop_server(self, thread):
        self.server.shutdown()
        self.server.server_close()
        thread.join(timeout=5)

    def test_pending_request_gets_the_retryable_429(self):
        # A same-key blocker keeps the real ticket PENDING; the revoke notice
        # then closes it and the terminal must see 429, not RemoteDisconnected.
        blocker = self.dispatcher.register(KEY_ID, "blocker-request")
        self.assertIsNotNone(blocker)
        headers, _request_id = share.build_attribution(SECRET, KEY_ID)
        result = {}

        def send():
            try:
                import requests
                result["response"] = requests.post(
                    f"{self.base}/v1/chat/completions",
                    json={"model": "chat-model",
                          "messages": [{"role": "user",
                                        "content": "hi"}]},
                    headers=headers, timeout=20)
            except Exception as exc:  # noqa: BLE001 - evidence
                result["error"] = exc

        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10.0
        while self.dispatcher.stats()["pending"] < 2:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        notice = share.build_revoke_notice(
            SECRET, KEY_ID, 5, now=int(time.time()), nonce="c" * 32)
        import requests
        response = requests.post(
            f"{self.base}/admin/share/revoke", json=notice, timeout=10)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertGreaterEqual(response.json().get("released", 0), 1)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", result)
        response = result["response"]
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers.get("Retry-After"), "1")
        self.assertEqual(response.json()["error"]["type"],
                         "share_dispatch_revoked")
        self.assertEqual(self.dispatcher.pending_count(), 0)
        self.assertEqual(_PendingStubUpstream.arrivals, [])
        self.assertTrue(self.usage_rows)
        self.assertEqual(self.usage_rows[-1].get("status"),
                         "share_dispatch_revoked")


if __name__ == "__main__":
    unittest.main()
