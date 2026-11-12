# -*- coding: utf-8 -*-
"""8100 -> 8110 受信撤销通知（R-C5）的回归测试。

钉死的契约：

* 8100 在投影装载/重载时检测某 Key 变为 disabled 或删除（generation 前进），
  通过 loopback HTTP 向 8110 发送一次受信撤销通知；
* 8100 在「原 disabled/删除 → 新 enabled」时也发送一次 generation 前进通知：
  它只推进 8110 的 per-Key generation 水位，绝不撤销；
* 通知只用稳定 key_id、projection_generation、action、ts、nonce 和版本号做
  HMAC，与归属标记共用同一个共享密钥；不带明文 Key、不写日志、不暴露给公开路径；
* 8110 只接受 loopback、受信签名、匹配版本、时间窗内、未重放的请求；通过后
  撤销通知调用 dispatcher.revoke_key(key_id)，返回 released（PENDING/GRANTED）
  与 aborted（COMMITTED）两个数量；重新启用通知只更新 per-Key 最新 generation，
  不调用 revoke_key；
* 同一 generation 重复通知幂等 no-op，旧 generation 不覆盖新状态；重新启用后
  迟到的旧 generation 撤销通知返回 stale_generation，且不关闭新票据；
* 通知失败不阻断 8100 的撤销本身：有界重试并记录有界诊断，不伪造成功；
* 整批通知并发发送且共享一个总截止时间：一个慢 Key 不串行拖住 load ACK；
* 通知只在专用分享后端（8110）生效：8000 主链即使收到同签名通知也不释放票据；
* 没有通知时 PENDING 票据仍然存在（负例），证明“撤销后 pending=0”不是恒真。

隔离：用例不启动产品服务、不读写生产账本；HTTP 用例只绑定 loopback
临时端口，归因密钥和撤销状态写在私有临时目录；产品默认
``/var/lib/um-share/share-revoke.state`` 与旧 release 的
``data/share-revoke.state`` 只做运行前后的只读指纹比对。
"""

import base64
import hashlib
import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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


share = _load("share_entry", CHAT2API / "share_entry.py")
dispatch = _load("share_dispatch", CHAT2API / "share_dispatch.py")
chat2api = _load("chat2api_revoke_notice", CHAT2API / "chat2api.py")

SECRET = "s" * 48
KEY_ID = "eee3e921-f1ee-4b5a-8fc0-e4d2eb30135f"
PLAINTEXT_KEY = "um-share-plaintext-never-in-the-notice"

DEFAULT_REVOKE_STATE = share.DEFAULT_REVOKE_STATE_FILE
LEGACY_REVOKE_STATE = share.LEGACY_REVOKE_STATE_FILE


def _state_fingerprint(path):
    """Read-only fingerprint of ONE product state file.

    The suite must never create, truncate or rewrite these files: they belong to
    the live ai-gateway instance (the deployment default) and to the release
    tree the legacy adoption reads once.  ``(False,)`` means it is absent.
    """
    try:
        info = path.stat()
    except FileNotFoundError:
        return (False,)
    return (True, info.st_mtime_ns, info.st_size,
            hashlib.sha256(path.read_bytes()).hexdigest())


def _default_revoke_state_fingerprint():
    return _state_fingerprint(DEFAULT_REVOKE_STATE)


def _legacy_revoke_state_fingerprint():
    return _state_fingerprint(LEGACY_REVOKE_STATE)


_DEFAULT_REVOKE_STATE_BEFORE = _default_revoke_state_fingerprint()
_LEGACY_REVOKE_STATE_BEFORE = _legacy_revoke_state_fingerprint()


def _reset_revoke_state_cache():
    """Forget the revoke state caches AND the verified-notice thread facts."""
    share.reset_revoke_state_cache()
    for attribute in ("ts", "action"):
        if hasattr(share._revoke_notice_facts, attribute):
            delattr(share._revoke_notice_facts, attribute)


@unittest.skip("HMAC share path retired with the merged share process")
class _IsolatedRevokeStateTestCase(unittest.TestCase):
    """Run ONE test against a private revoke state file.

    Verifying a notice records a nonce and a generation, and constructing a
    ShareServer persists its projection.  Every such test must point
    ``share.REVOKE_STATE_ENV`` at a private temporary directory before that
    call, so the PRODUCT default state is never read or written.  The
    environment, the module caches and the test's worker threads/servers are
    restored by the per-class cleanups; the default state is fingerprinted
    after every test.
    """

    def setUp(self):
        super().setUp()
        self.revoke_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.revoke_tmp.cleanup)
        self.revoke_state_file = Path(self.revoke_tmp.name) / "revoke.state"
        self.revoke_state_patch = mock.patch.dict(
            os.environ, {share.REVOKE_STATE_ENV: str(self.revoke_state_file)})
        self.revoke_state_patch.start()
        self.addCleanup(self.revoke_state_patch.stop)
        _reset_revoke_state_cache()
        self.addCleanup(_reset_revoke_state_cache)

    def tearDown(self):
        try:
            self.assertEqual(
                _default_revoke_state_fingerprint(),
                _DEFAULT_REVOKE_STATE_BEFORE,
                "the suite modified the PRODUCT default revoke state %s"
                % DEFAULT_REVOKE_STATE)
            self.assertEqual(
                _legacy_revoke_state_fingerprint(),
                _LEGACY_REVOKE_STATE_BEFORE,
                "the suite modified the legacy release state %s"
                % LEGACY_REVOKE_STATE)
        finally:
            super().tearDown()


def setUpModule():
    """Fingerprint the PRODUCT default state immediately before the run."""
    global _DEFAULT_REVOKE_STATE_BEFORE, _LEGACY_REVOKE_STATE_BEFORE
    _DEFAULT_REVOKE_STATE_BEFORE = _default_revoke_state_fingerprint()
    _LEGACY_REVOKE_STATE_BEFORE = _legacy_revoke_state_fingerprint()


def tearDownModule():
    """Fail the whole run if any test touched a PRODUCT state file."""
    after = _default_revoke_state_fingerprint()
    if after != _DEFAULT_REVOKE_STATE_BEFORE:
        raise AssertionError(
            "the suite modified the PRODUCT default revoke state %s "
            "(before=%r, after=%r)"
            % (DEFAULT_REVOKE_STATE, _DEFAULT_REVOKE_STATE_BEFORE, after))
    after = _legacy_revoke_state_fingerprint()
    if after != _LEGACY_REVOKE_STATE_BEFORE:
        raise AssertionError(
            "the suite modified the legacy release state %s "
            "(before=%r, after=%r)"
            % (LEGACY_REVOKE_STATE, _LEGACY_REVOKE_STATE_BEFORE, after))


class _NoticeHandler(BaseHTTPRequestHandler):
    """Record ONE notice body and answer 200; never log the request line."""

    def log_message(self, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.server.notice_body = self.rfile.read(length)
        body = b'{"ok": true, "released": 1, "generation": 1}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


class _SlowNoticeHandler(BaseHTTPRequestHandler):
    """Accept the connection, then answer far too slowly for the batch bound."""

    def log_message(self, *_args):
        return

    def do_POST(self):
        time.sleep(5.0)
        body = b'{"ok": true}'
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass


class _FakeDispatcher:
    def __init__(self):
        self.calls = []

    def revoke_key(self, key_id, reason="revoke"):
        self.calls.append((key_id, reason))
        return 1, 0


@unittest.skip("HMAC share path retired with the merged share process")
class TestRevokeNoticeProtocol(_IsolatedRevokeStateTestCase):
    def setUp(self):
        super().setUp()
        self.state_file = self.revoke_state_file

    def _notice(self, generation=7, now=1000, nonce="a" * 32):
        return share.build_revoke_notice(
            SECRET, KEY_ID, generation, now=now, nonce=nonce)

    def test_build_and_verify_round_trip(self):
        notice = self._notice()
        self.assertEqual(
            share.verify_revoke_notice(notice, SECRET, now=1000),
            (KEY_ID, 7, "verified"))

    def test_reenable_notice_round_trip_is_signed_and_distinct(self):
        notice = share.build_revoke_notice(
            SECRET, KEY_ID, 7, now=1000, nonce="a" * 32,
            action="reenable")
        self.assertEqual(
            share.verify_revoke_notice(notice, SECRET, now=1000),
            (KEY_ID, 7, "verified"))
        self.assertEqual(share._verified_revoke_notice_action(), "reenable")
        # The action is part of the signed bytes: flipping it without
        # re-signing is a bad signature, not a valid revoke/re-enable.
        flipped = dict(notice, action="revoke")
        self.assertEqual(
            share.verify_revoke_notice(flipped, SECRET, now=1000),
            (None, None, "bad_signature"))

    def test_builder_refuses_an_unknown_action(self):
        self.assertIsNone(share.build_revoke_notice(
            SECRET, KEY_ID, 1, action="delete"))
        self.assertIsNone(share.build_revoke_notice(
            SECRET, KEY_ID, 1, action=""))

    def test_signature_covers_key_generation_ts_and_nonce(self):
        notice = self._notice()
        for field, value in (("key_id", "b3RoZXIta2V5"),
                             ("projection_generation", 8),
                             ("ts", "1001"),
                             ("nonce", "b" * 32)):
            tampered = dict(notice)
            tampered[field] = value
            self.assertEqual(
                share.verify_revoke_notice(tampered, SECRET, now=1000),
                (None, None, "bad_signature"))

    def test_version_mismatch_is_rejected(self):
        notice = dict(self._notice(), version="2")
        self.assertEqual(
            share.verify_revoke_notice(notice, SECRET, now=1000),
            (None, None, "bad_version"))

    def test_timestamp_outside_window_is_rejected(self):
        notice = self._notice(now=1000)
        self.assertEqual(
            share.verify_revoke_notice(notice, SECRET, now=1000 + 301),
            (None, None, "ts_out_of_window"))

    def test_replayed_nonce_is_rejected(self):
        notice = self._notice()
        self.assertEqual(
            share.verify_revoke_notice(notice, SECRET, now=1000)[2],
            "verified")
        self.assertEqual(
            share.verify_revoke_notice(notice, SECRET, now=1000),
            (None, None, "replayed"))

    def test_replayed_nonce_is_still_rejected_after_restart(self):
        # The replay registry is persisted: clearing the in-process cache is a
        # restart of 8110, not an amnesty for an already-seen nonce.
        notice = self._notice()
        self.assertEqual(
            share.verify_revoke_notice(notice, SECRET, now=1000)[2],
            "verified")
        share.reset_revoke_state_cache()
        self.assertEqual(
            share.verify_revoke_notice(notice, SECRET, now=1000),
            (None, None, "replayed"))

    def test_corrupt_state_file_fails_closed(self):
        self.state_file.write_bytes(b"{not-json")
        share.reset_revoke_state_cache()
        self.assertEqual(
            share.verify_revoke_notice(self._notice(), SECRET, now=1000),
            (None, None, "state_unavailable"))

    def test_first_start_creates_the_state_file_with_a_schema_version(self):
        # Review #12: a missing file is a real first start only because the
        # file is CREATED with its schema version before the empty state is
        # accepted.  A later start can therefore tell first start from a
        # deleted/corrupt file.
        self.assertFalse(self.state_file.exists())
        share.reset_revoke_state_cache()
        self.assertEqual(share.load_revoke_state(),
                         {"nonces": {}, "generations": {}})
        self.assertTrue(self.state_file.exists())
        payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], share.REVOKE_STATE_VERSION)
        self.assertEqual(payload["nonces"], {})
        self.assertEqual(payload["generations"], {})
        if os.name == "posix":
            self.assertEqual(self.state_file.stat().st_mode & 0o777, 0o600)

    def test_corrupt_state_file_is_not_treated_as_empty(self):
        # Review #12: an existing but unreadable/corrupt file is an explicit
        # failure, never an empty state and never a silent file rewrite.
        corrupt = b"{not-json"
        self.state_file.write_bytes(corrupt)
        share.reset_revoke_state_cache()
        with self.assertRaises(ValueError):
            share.load_revoke_state()
        with self.assertRaises(ValueError):
            share.load_revoke_generation(KEY_ID)
        with self.assertRaises(ValueError):
            share.record_revoke_generation(KEY_ID, 1, 0)
        self.assertEqual(self.state_file.read_bytes(), corrupt)

    def test_corrupt_state_file_does_not_overwrite_the_in_memory_cache(self):
        # Review #12: a reload that hits a corrupt file leaves the already
        # loaded cache untouched instead of replacing it with an empty state.
        share.record_revoke_generation(KEY_ID, 5, 1000)
        loaded = dict(share._revoke_applied_generations)
        self.state_file.write_bytes(b"{broken")
        share._revoke_state_loaded = False
        with self.assertRaises(ValueError):
            share.load_revoke_state()
        self.assertEqual(share._revoke_applied_generations, loaded)
        self.assertFalse(share._revoke_state_loaded)

    def test_bad_shape_and_generation_are_rejected(self):
        notice = self._notice()
        extra = dict(notice, plaintext_key=PLAINTEXT_KEY)
        self.assertEqual(
            share.verify_revoke_notice(extra, SECRET, now=1000),
            (None, None, "bad_shape"))
        for value in (-1, True, "7"):
            bad = dict(notice, projection_generation=value)
            self.assertEqual(
                share.verify_revoke_notice(bad, SECRET, now=1000),
                (None, None, "bad_generation"))

    def test_missing_secret_and_bad_nonce_are_rejected(self):
        notice = self._notice()
        self.assertEqual(
            share.verify_revoke_notice(notice, "", now=1000),
            (None, None, "no_key"))
        self.assertEqual(
            share.verify_revoke_notice(notice, "short", now=1000),
            (None, None, "no_key"))
        bad_nonce = dict(notice, nonce="z" * 32)
        self.assertEqual(
            share.verify_revoke_notice(bad_nonce, SECRET, now=1000),
            (None, None, "bad_nonce"))

    def test_builder_refuses_unusable_input(self):
        self.assertIsNone(share.build_revoke_notice("short", KEY_ID, 1))
        self.assertIsNone(share.build_revoke_notice(SECRET, "", 1))
        self.assertIsNone(share.build_revoke_notice(SECRET, KEY_ID, -1))
        self.assertIsNone(share.build_revoke_notice(SECRET, KEY_ID, True))

    def test_notice_carries_no_plaintext_key_or_secret(self):
        text = json.dumps(self._notice())
        self.assertNotIn(PLAINTEXT_KEY, text)
        self.assertNotIn(SECRET, text)


@unittest.skip("HMAC share path retired with the merged share process")
class TestProjectionTransitions(_IsolatedRevokeStateTestCase):
    def _server(self, generation, enabled, known=None):
        server = share.ShareServer.__new__(share.ShareServer)
        server._projection_generation = generation
        server._projection_enabled = set(enabled)
        server._projection_known = OrderedDict.fromkeys(
            enabled if known is None else known)
        return server

    @staticmethod
    def _parsed(generation, entries):
        return {"config_generation": generation, "data": [
            {"key_id": key_id, "enabled": enabled}
            for key_id, enabled in entries]}

    @staticmethod
    def _spec(key_id, generation, action):
        return {"key_id": key_id, "generation": generation,
                "action": action}

    def test_disabled_key_is_revoked_once(self):
        server = self._server(10, {"a", "b", "c"})
        notices = server._projection_revoke_transitions_locked(
            self._parsed(11, [("a", True), ("b", False), ("c", True)]))
        self.assertEqual(notices, [self._spec("b", 11, "revoke")])
        self.assertEqual(server._projection_generation, 11)
        self.assertEqual(server._projection_enabled, {"a", "c"})
        self.assertEqual(set(server._projection_known), {"a", "b", "c"})

    def test_deleted_key_is_revoked(self):
        server = self._server(10, {"a", "b"})
        notices = server._projection_revoke_transitions_locked(
            self._parsed(11, [("a", True)]))
        self.assertEqual(notices, [self._spec("b", 11, "revoke")])
        self.assertEqual(set(server._projection_known), {"a", "b"})

    def test_repeated_generation_is_a_noop(self):
        server = self._server(11, {"a"})
        self.assertEqual(
            server._projection_revoke_transitions_locked(
                self._parsed(11, [("a", True)])), [])
        self.assertEqual(server._projection_generation, 11)
        self.assertEqual(server._projection_enabled, {"a"})

    def test_older_generation_does_not_regress(self):
        server = self._server(12, {"a"})
        self.assertEqual(
            server._projection_revoke_transitions_locked(
                self._parsed(10, [])), [])
        self.assertEqual(server._projection_generation, 12)
        self.assertEqual(server._projection_enabled, {"a"})

    def test_re_enabled_key_gets_one_advance_notice_not_a_revoke(self):
        server = self._server(11, {"a"}, known={"a", "b"})
        notices = server._projection_revoke_transitions_locked(
            self._parsed(12, [("a", True), ("b", True)]))
        self.assertEqual(notices, [self._spec("b", 12, "reenable")])
        self.assertEqual(server._projection_enabled, {"a", "b"})
        self.assertEqual(set(server._projection_known), {"a", "b"})

    def test_deleted_then_re_added_key_gets_advance_notice(self):
        server = self._server(10, {"a", "b"}, known={"a", "b"})
        notices = server._projection_revoke_transitions_locked(
            self._parsed(11, [("a", True)]))
        self.assertEqual(notices, [self._spec("b", 11, "revoke")])
        notices = server._projection_revoke_transitions_locked(
            self._parsed(12, [("a", True), ("b", True)]))
        self.assertEqual(notices, [self._spec("b", 12, "reenable")])
        self.assertEqual(server._projection_enabled, {"a", "b"})
        self.assertEqual(set(server._projection_known), {"a", "b"})

    def test_brand_new_key_gets_no_notice(self):
        server = self._server(10, {"a"}, known={"a"})
        notices = server._projection_revoke_transitions_locked(
            self._parsed(11, [("a", True), ("new", True)]))
        self.assertEqual(notices, [])
        self.assertEqual(server._projection_enabled, {"a", "new"})
        self.assertEqual(set(server._projection_known), {"a", "new"})

    def test_known_set_is_bounded_to_recent_removals(self):
        # Review #15: the known set keeps every Key in the CURRENT
        # projection plus only the most recently removed Keys; a long
        # add/remove churn must not grow it without bound.
        limit = share.REVOKE_PROJECTION_KNOWN_MAX
        server = self._server(1, {"k0"}, known={"k0"})
        generation = 1
        for index in range(1, limit + 2):
            generation += 1
            server._projection_revoke_transitions_locked(
                self._parsed(generation, [("k%d" % index, True)]))
        known = list(server._projection_known)
        self.assertLessEqual(len(known), limit)
        self.assertIn("k%d" % (generation - 1), known)
        self.assertIn("k%d" % (generation - 2), known)
        self.assertNotIn("k0", known)


@unittest.skip("HMAC share path retired with the merged share process")
class TestProjectionPersistence(_IsolatedRevokeStateTestCase):
    """Review #11: the applied projection survives an 8100 restart.

    The scenario is the one the review names: a Key is disabled (the applied
    snapshot is persisted), 8100 restarts while the Key is still disabled, and
    the NEXT load enables it again.  The restarted process must still know the
    Key was disabled and send ONE ``reenable`` notice instead of treating it as
    a brand-new Key.
    """

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "projection.json"
        self.entry = {"key_id": KEY_ID, "name": "friend",
                      "secret_hash": hashlib.sha256(b"secret").hexdigest(),
                      "secret_version": 1, "enabled": True,
                      "max_concurrency": 4}
        self._write(generation=1, enabled=True)

    def _write(self, *, generation, enabled):
        entry = dict(self.entry, enabled=enabled)
        self.config.write_text(json.dumps({
            "schema_version": 2,
            "config_generation": generation,
            "share_max_concurrency": 8,
            "data": [entry],
        }), encoding="utf-8")

    def _server(self):
        server = share.ShareServer(("127.0.0.1", 0), self.config)
        self.addCleanup(server.server_close)
        return server

    def _signed_load(self, server, *, generation, enabled):
        from share_broker_protocol import INSTANCE_SIGNED_KEYS, sign
        self._write(generation=generation, enabled=enabled)
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

        snapshot_path = share.revoke_projection_path(self.config)
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["version"],
                         share.REVOKE_PROJECTION_VERSION)
        self.assertEqual(snapshot["generation"], 2)
        self.assertEqual(snapshot["keys"], {KEY_ID: False})
        if os.name == "posix":
            self.assertEqual(snapshot_path.stat().st_mode & 0o777, 0o600)

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
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["generation"], 3)
        self.assertEqual(snapshot["keys"], {KEY_ID: True})


@unittest.skip("HMAC share path retired with the merged share process")
class TestRevokeNoticeSender(_IsolatedRevokeStateTestCase):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _NoticeHandler)
        self.server.notice_body = None
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.addCleanup(self._drain_notice_workers)

        self.sender = share.ShareServer.__new__(share.ShareServer)
        self.sender.upstream_port = self.server.server_address[1]
        self.sender.attribution_secret = SECRET
        self.sender.fault_ring = deque(maxlen=8)
        self.sender.fault_log = None
        self.sender._projection_generation = 9
        # S2 budget wiring: a bare instance (no __init__) has no guard; the
        # notice sender's egress accounting must degrade to a no-op, not raise.
        self.sender.budget = None
        self.sender.budget_cost_model = None

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _drain_notice_workers(self):
        """Join this test's bounded notice workers before the next test."""
        deadline = time.monotonic() + 5.0
        while True:
            workers = [
                thread for thread in threading.enumerate()
                if thread.name == "share-revoke-notice"
                and thread is not threading.current_thread()]
            if not workers:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail("share-revoke-notice workers did not stop")
            for thread in workers:
                thread.join(min(0.1, remaining))

    def test_sender_sends_one_signed_notice(self):
        self.assertTrue(self.sender._send_revoke_notice(KEY_ID, 9))
        body = json.loads(self.server.notice_body)
        self.assertEqual(body["key_id"], share._b64url(KEY_ID.encode()))
        self.assertEqual(body["projection_generation"], 9)
        self.assertEqual(body["action"], "revoke")
        self.assertEqual(share.verify_revoke_notice(
            body, SECRET, now=int(body["ts"]))[0], KEY_ID)
        text = self.server.notice_body.decode("utf-8")
        self.assertNotIn(PLAINTEXT_KEY, text)
        self.assertNotIn(SECRET, text)
        self.assertEqual(len(self.sender.fault_ring), 0)

    def test_sender_sends_a_signed_reenable_notice(self):
        self.assertTrue(self.sender._send_revoke_notice(
            KEY_ID, 9, "reenable"))
        body = json.loads(self.server.notice_body)
        self.assertEqual(body["projection_generation"], 9)
        self.assertEqual(body["action"], "reenable")
        self.assertEqual(
            share.verify_revoke_notice(body, SECRET, now=int(body["ts"]))[:2],
            (KEY_ID, 9))
        self.assertEqual(share._verified_revoke_notice_action(), "reenable")
        self.assertEqual(len(self.sender.fault_ring), 0)

    def test_sender_uses_the_snapshot_generation_after_the_guard(self):
        # Review #12: the generation belongs to the applied transition.  A
        # later load that advances the live projection must NOT relabel a
        # notice that was already snapshotted under the accept lock.
        self.sender._projection_generation = 10
        self.sender._projection_enabled = {"a", "b"}
        self.sender._projection_known = {"a", "b"}
        self.sender.guard = threading.Lock()
        with self.sender.guard:
            notices = self.sender._projection_revoke_transitions_locked(
                {"config_generation": 11,
                 "data": [{"key_id": "a", "enabled": True},
                          {"key_id": "b", "enabled": False}]})
        # A next generation is applied after the snapshot; the send must keep
        # the snapshotted 11, never observe the live 99.
        self.sender._projection_generation = 99
        self.sender._send_revoke_notices(notices)
        body = json.loads(self.server.notice_body)
        self.assertEqual(body["projection_generation"], 11)
        self.assertEqual(body["action"], "revoke")

    def test_sender_failure_is_bounded_and_recorded(self):
        import socket
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
        probe.close()
        self.sender.upstream_port = closed_port
        self.assertFalse(self.sender._send_revoke_notice(KEY_ID, 9))
        self.assertEqual(self.sender.fault_ring[-1]["code"],
                          "share_revoke_notice_failed")
        record = json.dumps(self.sender.fault_ring[-1])
        self.assertNotIn(PLAINTEXT_KEY, record)
        self.assertNotIn(SECRET, record)

    def test_notice_failure_never_breaks_the_load_ack(self):
        def explode(*_args):
            raise RuntimeError("notice failed")

        self.sender._send_revoke_notice = explode
        self.sender._send_revoke_notices([{
            "key_id": KEY_ID, "generation": 9, "action": "revoke"}])
        self.assertEqual(self.sender.fault_ring[-1]["code"],
                          "share_revoke_notice_failed")

    def test_many_slow_keys_share_one_bounded_batch_deadline(self):
        # Review #13: ten keys, every backend answer far slower than the
        # (shortened) per-attempt timeout, and ONE total batch deadline.  The
        # call returns after the deadline instead of ten serial waits; each
        # unfinished notice is a bounded fault.
        slow = ThreadingHTTPServer(("127.0.0.1", 0), _SlowNoticeHandler)
        slow.daemon_threads = True
        slow.request_queue_size = 32
        slow_thread = threading.Thread(target=slow.serve_forever, daemon=True)
        slow_thread.start()

        def stop_slow():
            slow.shutdown()
            slow.server_close()
            slow_thread.join(timeout=5)

        self.addCleanup(stop_slow)
        sender = share.ShareServer.__new__(share.ShareServer)
        sender.upstream_port = slow.server_address[1]
        sender.attribution_secret = SECRET
        sender.fault_ring = deque(maxlen=64)
        sender.fault_log = None
        keys = ["slow-key-%d" % i for i in range(10)]
        notices = [{"key_id": key, "generation": 12, "action": "revoke"}
                   for key in keys]
        with mock.patch.object(share, "REVOKE_NOTICE_TIMEOUT_S", 0.2), \
                mock.patch.object(share, "REVOKE_NOTICE_RETRY_DELAY_S", 0.05), \
                mock.patch.object(share, "REVOKE_NOTICE_BATCH_DEADLINE_S",
                                  0.2):
            started = time.monotonic()
            sender._send_revoke_notices(notices)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5)
        codes = [record["code"] for record in sender.fault_ring]
        self.assertLessEqual(len(codes), 2 * len(keys))
        self.assertLessEqual(len(codes), share.FAULT_RING_MAX)
        self.assertTrue(codes)
        self.assertTrue(all(code in ("share_revoke_notice_timeout",
                                    "share_revoke_notice_failed")
                            for code in codes))
        self.assertIn("share_revoke_notice_timeout", codes)

    def test_twenty_slow_keys_use_a_bounded_worker_pool(self):
        # Review #10: 20 disabled Keys must not create 20 daemon threads and
        # 20 loopback connections.  At most REVOKE_NOTICE_MAX_CONCURRENCY
        # senders run at once, the rest wait in the FIFO, and the whole
        # batch still returns at the ONE deadline with bounded faults.
        sender = share.ShareServer.__new__(share.ShareServer)
        sender.upstream_port = 1
        sender.attribution_secret = SECRET
        sender.fault_ring = deque(maxlen=64)
        sender.fault_log = None
        active = 0
        peak_active = 0
        peak_threads = 0
        lock = threading.Lock()

        def slow_notice(*_args, **_kwargs):
            nonlocal active, peak_active, peak_threads
            with lock:
                active += 1
                peak_active = max(peak_active, active)
                peak_threads = max(
                    peak_threads,
                    len([thread for thread in threading.enumerate()
                         if thread.name == "share-revoke-notice"]))
            try:
                time.sleep(1.0)
            finally:
                with lock:
                    active -= 1
            return True

        sender._send_revoke_notice = slow_notice
        notices = [{"key_id": "slow-%d" % index, "generation": 20,
                    "action": "revoke"} for index in range(20)]
        started = time.monotonic()
        sender._send_revoke_notices(notices)
        elapsed = time.monotonic() - started
        self.assertLessEqual(
            peak_active, share.REVOKE_NOTICE_MAX_CONCURRENCY)
        self.assertGreaterEqual(peak_active, 1)
        self.assertLessEqual(
            peak_threads, share.REVOKE_NOTICE_MAX_CONCURRENCY)
        self.assertLessEqual(
            elapsed, share.REVOKE_NOTICE_BATCH_DEADLINE_S + 0.5)
        self.assertTrue(sender.fault_ring)
        self.assertLessEqual(len(sender.fault_ring), len(notices))
        self.assertTrue(all(
            record["code"] == "share_revoke_notice_timeout"
            for record in sender.fault_ring))


@unittest.skip("HMAC share path retired with the merged share process")
class TestRevokeNoticeEndpoint(_IsolatedRevokeStateTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.key_file = Path(self.tmp.name) / "attribution.key"
        from share_secret_protection import write_private_secret_path
        write_private_secret_path(self.key_file, SECRET.encode("utf-8"))

        self._original_key_file = chat2api.SHARE_ATTRIBUTION_KEY_FILE
        self._original_cache = dict(chat2api._attribution_key_cache)
        chat2api.SHARE_ATTRIBUTION_KEY_FILE = str(self.key_file)
        chat2api._attribution_key_cache["stat"] = None
        chat2api._attribution_key_cache["secret"] = None
        self.addCleanup(self._restore_key_file)

        self._original_dispatcher = chat2api._share_dispatcher
        self.addCleanup(setattr, chat2api, "_share_dispatcher",
                        self._original_dispatcher)
        self._original_role = chat2api.SHARE_BACKEND_ROLE
        self.addCleanup(setattr, chat2api, "SHARE_BACKEND_ROLE",
                        self._original_role)
        chat2api._reset_share_revoke_state()
        self.addCleanup(chat2api._reset_share_revoke_state)
        self.addCleanup(chat2api._request_facts.__dict__.clear)

        self._original_log_message = chat2api.Handler.log_message
        chat2api.Handler.log_message = lambda *_args: None
        self.addCleanup(setattr, chat2api.Handler, "log_message",
                        self._original_log_message)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), chat2api.Handler)
        self.server.proxy = None
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _restore_key_file(self):
        chat2api.SHARE_ATTRIBUTION_KEY_FILE = self._original_key_file
        chat2api._attribution_key_cache.update(self._original_cache)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _post(self, notice, host=None):
        body = json.dumps(notice).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "Connection": "close"}
        if host is not None:
            headers["Host"] = host
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            connection.request("POST", "/admin/share/revoke", body=body,
                               headers=headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw or b"{}")
        finally:
            connection.close()

    def _notice(self, generation, nonce, action="revoke"):
        return share.build_revoke_notice(
            SECRET, KEY_ID, generation, nonce=nonce, action=action)

    def test_valid_notice_releases_pending_ticket(self):
        dispatcher = dispatch.ShareDispatcher(max_wait=10)
        ticket = dispatcher.register(KEY_ID, "pending-request")
        chat2api._share_dispatcher = dispatcher
        self.assertEqual(dispatcher.pending_count(), 1)
        status, payload = self._post(self._notice(5, "a" * 32))
        self.assertEqual(status, 200)
        self.assertEqual(payload["released"], 1)
        self.assertEqual(payload["generation"], 5)
        self.assertEqual(ticket.state, "closed")
        self.assertEqual(dispatcher.pending_count(), 0)

    def test_valid_notice_releases_granted_ticket(self):
        dispatcher = dispatch.ShareDispatcher(max_wait=10)
        ticket = dispatcher.register(KEY_ID, "granted-request")
        self.assertEqual(dispatcher.poll(ticket), dispatch.OUTCOME_GRANTED)
        chat2api._share_dispatcher = dispatcher
        status, payload = self._post(self._notice(6, "b" * 32))
        self.assertEqual(status, 200)
        self.assertEqual(payload["released"], 1)
        self.assertEqual(ticket.state, "closed")
        # A GRANTED ticket that the notice closed before commit must never
        # send: commit() is False and the caller can name the revoke reason.
        self.assertFalse(ticket.commit())
        self.assertEqual(ticket.outcome, dispatch.OUTCOME_REVOKED)
        # Review #13: the terminal must receive a RETRYABLE refusal, never a
        # silent success or a generic client cancel.  The dispatch boundary
        # raises its own revoke error and the handler maps it to 429 +
        # Retry-After with the stable ``share_dispatch_revoked`` type.
        with self.assertRaises(chat2api._ShareDispatchRevoked):
            chat2api._await_share_send_turn(ticket, None, None, None)
        self.assertFalse(issubclass(
            chat2api._ShareDispatchRevoked, chat2api._RequestCancelled))
        handler = chat2api.Handler.__new__(chat2api.Handler)
        captured = {}

        def capture(code, message, detail=None,
                     error_type="chat2api_error", extra_headers=None):
            captured.update(code=code, message=message, detail=detail,
                            error_type=error_type,
                            extra_headers=extra_headers)

        handler._error = capture
        handler._share_dispatch_revoked_error()
        self.assertEqual(captured["code"], 429)
        self.assertEqual(captured["error_type"], "share_dispatch_revoked")
        self.assertIn("Retry-After", captured["extra_headers"])

    def test_new_ticket_after_notice_is_refused(self):
        # A request 8100 admitted BEFORE the revoke can reach 8110 AFTER the
        # notice. Its verified attribution timestamp is older than the applied
        # generation, so a NEW ticket must be closed instead of granted.
        dispatcher = dispatch.ShareDispatcher(max_wait=10)
        chat2api._share_dispatcher = dispatcher
        status, _payload = self._post(self._notice(5, "a" * 32))
        self.assertEqual(status, 200)
        chat2api.set_request_facts(
            "chat", KEY_ID, "admitted-before-revoke", "verified",
            attribution_ts=int(time.time()) - 10)
        ticket = chat2api.share_dispatch_ticket("chat")
        self.assertEqual(ticket.state, "closed")
        self.assertEqual(ticket.reason, "revoke")
        self.assertEqual(ticket.wait_turn(), dispatch.OUTCOME_REVOKED)
        self.assertFalse(ticket.commit())
        self.assertEqual(dispatcher.pending_count(), 0)

    def test_re_enabled_key_newer_attribution_is_allowed(self):
        # A later re-enable is not a revoke: a request admitted after the
        # applied generation carries a strictly newer timestamp and is admitted.
        dispatcher = dispatch.ShareDispatcher(max_wait=10)
        chat2api._share_dispatcher = dispatcher
        status, _payload = self._post(self._notice(5, "a" * 32))
        self.assertEqual(status, 200)
        chat2api.set_request_facts(
            "chat", KEY_ID, "admitted-after-reenable", "verified",
            attribution_ts=int(time.time()) + 1)
        ticket = chat2api.share_dispatch_ticket("chat")
        self.assertEqual(ticket.state, "pending")
        self.assertEqual(dispatcher.pending_count(), 1)
        ticket.release("test")

    def test_re_enable_advances_and_late_revoke_is_stale(self):
        # Review #8: revoke(gen=N) -> re-enable(gen=N+1) -> late
        # revoke(gen=N).  The re-enable only advances the watermark and
        # never calls revoke_key; the late revoke is stale and must not close
        # the re-enabled Key's NEW in-flight ticket.
        dispatcher = dispatch.ShareDispatcher(max_wait=10)
        chat2api._share_dispatcher = dispatcher
        revoked_ticket = dispatcher.register(KEY_ID, "before-revoke")
        status, first = self._post(self._notice(5, "a" * 32))
        self.assertEqual(status, 200)
        self.assertEqual(first["released"], 1)
        self.assertEqual(revoked_ticket.state, "closed")

        status, reenabled = self._post(
            self._notice(6, "b" * 32, action="reenable"))
        self.assertEqual(status, 200)
        self.assertEqual(reenabled["released"], 0)
        self.assertTrue(reenabled["advanced"])
        self.assertEqual(dispatcher.pending_count(), 0)

        chat2api.set_request_facts(
            "chat", KEY_ID, "after-reenable", "verified",
            attribution_ts=int(time.time()) + 1)
        ticket = chat2api.share_dispatch_ticket("chat")
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.state, "pending")

        status, late = self._post(self._notice(5, "c" * 32))
        self.assertEqual(status, 200)
        self.assertEqual(late["released"], 0)
        self.assertEqual(late["ignored"], "stale_generation")
        self.assertEqual(ticket.state, "pending")
        # The re-enabled Key's new ticket still sends normally.
        self.assertEqual(dispatcher.poll(ticket), dispatch.OUTCOME_GRANTED)
        self.assertTrue(ticket.commit())

    def test_re_enable_never_calls_revoke_key(self):
        # Review #8: a re-enable only advances the per-Key generation; it must
        # NEVER call revoke_key, so the re-enabled Key's in-flight tickets
        # survive.
        dispatcher = _FakeDispatcher()
        chat2api._share_dispatcher = dispatcher
        status, first = self._post(self._notice(5, "a" * 32))
        self.assertEqual(status, 200)
        self.assertEqual(first["released"], 1)
        self.assertEqual(len(dispatcher.calls), 1)
        status, second = self._post(
            self._notice(6, "b" * 32, action="reenable"))
        self.assertEqual(status, 200)
        self.assertEqual(second["released"], 0)
        self.assertTrue(second["advanced"])
        self.assertEqual(len(dispatcher.calls), 1)

    def test_private_role_revoke_endpoint_is_explicitly_unassembled(self):
        # Review #9: the private 8000 role must never release a ticket even
        # for a correctly signed notice.  The response names the unassembled
        # role and released=0 instead of silently pretending success.
        dispatcher = _FakeDispatcher()
        chat2api._share_dispatcher = dispatcher
        chat2api.SHARE_BACKEND_ROLE = False
        status, payload = self._post(self._notice(5, "a" * 32))
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["type"],
                          "share_revoke_unassembled")
        self.assertEqual(payload["error"]["detail"]["released"], 0)
        self.assertEqual(payload["error"]["detail"]["reason"],
                          "share_backend_unassembled")
        self.assertEqual(dispatcher.calls, [])

    def test_private_role_never_assembles_the_dispatcher(self):
        chat2api.SHARE_BACKEND_ROLE = False
        chat2api._share_dispatcher = None
        chat2api.set_request_facts(
            "chat", KEY_ID, "private-request", "verified",
            attribution_ts=int(time.time()) + 1)
        self.assertIsNone(chat2api.share_dispatch_ticket("chat"))
        self.assertIsNone(chat2api._share_dispatcher)

    def test_applied_generation_survives_restart(self):
        dispatcher = _FakeDispatcher()
        chat2api._share_dispatcher = dispatcher
        status, _payload = self._post(self._notice(8, "a" * 32))
        self.assertEqual(status, 200)
        chat2api._reset_share_revoke_state()
        status, payload = self._post(self._notice(7, "b" * 32))
        self.assertEqual(status, 200)
        self.assertEqual(payload["ignored"], "stale_generation")
        self.assertEqual(payload["released"], 0)
        self.assertEqual(len(dispatcher.calls), 1)

    def test_unavailable_revoke_state_is_retryable_not_a_client_cancel(self):
        # Review #9: an unreadable revoke state closes the ticket, but the
        # closed ticket must map to a retryable SERVICE failure (503), never
        # to the client-disconnect cancellation.
        dispatcher = dispatch.ShareDispatcher(max_wait=10)
        chat2api._share_dispatcher = dispatcher
        with mock.patch.object(chat2api, "_share_entry_module",
                               side_effect=ValueError("state unavailable")):
            chat2api.set_request_facts(
                "chat", KEY_ID, "state-unavailable", "verified",
                attribution_ts=int(time.time()) + 1)
            ticket = chat2api.share_dispatch_ticket("chat")
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.state, "closed")
        self.assertEqual(ticket.reason, "revoke_state_unavailable")
        with self.assertRaises(chat2api._ShareDispatchStateUnavailable):
            chat2api._raise_share_dispatch_refusal(
                ticket.outcome, ticket.reason)
        with self.assertRaises(chat2api._ShareDispatchStateUnavailable):
            chat2api._await_share_send_turn(ticket, None, None, None)
        self.assertFalse(issubclass(
            chat2api._ShareDispatchStateUnavailable,
            chat2api._RequestCancelled))

    def test_state_unavailable_error_is_503_with_the_same_ledger_name(self):
        # Review #9: the HTTP error type and the ledger status share ONE
        # stable name; 503 + Retry-After makes the refusal retryable.
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
        self.assertEqual(
            captured["error_type"],
            chat2api.SHARE_DISPATCH_STATE_UNAVAILABLE_STATUS)
        self.assertEqual(
            chat2api.SHARE_DISPATCH_STATE_UNAVAILABLE_STATUS,
            "share_dispatch_state_unavailable")
        self.assertIn("Retry-After", captured["extra_headers"])

    def test_generations_are_evicted_only_after_the_notice_window(self):
        # Review #14: an applied generation inside the 300s notice window is
        # never evicted; only an entry whose last update is older is dropped.
        now = time.time()
        with chat2api._revoke_applied_lock:
            chat2api._revoke_applied_generations.clear()
            chat2api._revoke_applied_generations[KEY_ID] = {
                "generation": 5, "applied_at": now, "updated_at": now}
            chat2api._prune_revoke_generations_locked(now=now)
            self.assertIn(KEY_ID, chat2api._revoke_applied_generations)
            chat2api._revoke_applied_generations[KEY_ID]["updated_at"] = (
                now - chat2api.REVOKE_NOTICE_GENERATIONS_TTL_S - 1)
            chat2api._prune_revoke_generations_locked(now=now)
            self.assertNotIn(KEY_ID, chat2api._revoke_applied_generations)

    def test_stale_generation_is_absolute_beyond_the_old_size_cap(self):
        # Review #14: more than the old 4096-entry insertion-order cap, all
        # updated inside the notice window, must NOT evict KEY_ID's generation.
        dispatcher = _FakeDispatcher()
        chat2api._share_dispatcher = dispatcher
        status, first = self._post(self._notice(5, "a" * 32))
        self.assertEqual((status, first["released"]), (200, 1))
        now = int(time.time())
        notices = [
            share.build_revoke_notice(
                SECRET, "other-%d" % index, 2, now=now,
                nonce="%032x" % (index + 1))
            for index in range(4097)]
        with mock.patch.object(share, "record_revoke_nonce",
                               return_value=True), \
                mock.patch.object(share, "record_revoke_generation",
                                  return_value=None):
            for notice in notices:
                status, payload = chat2api.apply_share_revoke_notice(
                    notice, SECRET, now=now)
                self.assertEqual(status, 200, payload)
        status, late = self._post(self._notice(4, "b" * 32))
        self.assertEqual(status, 200)
        self.assertEqual(late["ignored"], "stale_generation")
        self.assertEqual(late["released"], 0)
        self.assertIn(KEY_ID, chat2api._revoke_applied_generations)

    def test_committed_ticket_is_aborted_by_revoke(self):
        dispatcher = dispatch.ShareDispatcher(max_wait=10)
        ticket = dispatcher.register(KEY_ID, "committed-request")
        self.assertEqual(dispatcher.poll(ticket), dispatch.OUTCOME_GRANTED)
        self.assertTrue(ticket.commit())
        closed = []
        self.assertTrue(ticket.attach_abort(lambda: closed.append("close")))
        chat2api._share_dispatcher = dispatcher
        status, payload = self._post(self._notice(6, "c" * 32))
        self.assertEqual(status, 200)
        self.assertEqual(payload["released"], 0)
        self.assertEqual(payload["aborted"], 1)
        self.assertEqual(closed, ["close"])
        self.assertEqual(ticket.state, "closed")
        self.assertEqual(ticket.reason, "revoke")

    def test_ordinary_cancel_does_not_abort_a_committed_ticket(self):
        # D18-3: only a key revoke aborts a committed send. Ordinary
        # cancel/deadline/exit releases must not close the upstream response.
        dispatcher = dispatch.ShareDispatcher(max_wait=10)
        ticket = dispatcher.register(KEY_ID, "committed-request")
        self.assertEqual(dispatcher.poll(ticket), dispatch.OUTCOME_GRANTED)
        self.assertTrue(ticket.commit())
        closed = []
        self.assertTrue(ticket.attach_abort(lambda: closed.append("close")))
        self.assertTrue(ticket.release("cancel"))
        self.assertEqual(closed, [])
        self.assertEqual(ticket.state, "closed")
        self.assertFalse(dispatcher.revoke_key(KEY_ID)[1])

    def test_same_generation_is_idempotent(self):
        dispatcher = _FakeDispatcher()
        chat2api._share_dispatcher = dispatcher
        status, first = self._post(self._notice(7, "d" * 32))
        self.assertEqual((status, first["released"]), (200, 1))
        status, second = self._post(self._notice(7, "e" * 32))
        self.assertEqual(status, 200)
        self.assertEqual(second["released"], 0)
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(dispatcher.calls), 1)

    def test_older_generation_never_overrides_newer_state(self):
        dispatcher = _FakeDispatcher()
        chat2api._share_dispatcher = dispatcher
        status, first = self._post(self._notice(8, "f" * 32))
        self.assertEqual((status, first["released"]), (200, 1))
        status, older = self._post(self._notice(7, "1" * 32))
        self.assertEqual(status, 200)
        self.assertEqual(older["released"], 0)
        self.assertEqual(older["ignored"], "stale_generation")
        self.assertEqual(len(dispatcher.calls), 1)

    def test_replayed_notice_is_rejected(self):
        dispatcher = _FakeDispatcher()
        chat2api._share_dispatcher = dispatcher
        notice = self._notice(9, "2" * 32)
        self.assertEqual(self._post(notice)[0], 200)
        status, payload = self._post(notice)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["detail"]["reason"], "replayed")
        self.assertEqual(len(dispatcher.calls), 1)

    def test_bad_signature_is_rejected(self):
        dispatcher = _FakeDispatcher()
        chat2api._share_dispatcher = dispatcher
        notice = dict(self._notice(10, "3" * 32),
                      signature="0" * 64)
        status, payload = self._post(notice)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["detail"]["reason"], "bad_signature")
        self.assertEqual(dispatcher.calls, [])

    def test_bad_version_is_rejected(self):
        dispatcher = _FakeDispatcher()
        chat2api._share_dispatcher = dispatcher
        notice = dict(self._notice(10, "4" * 32), version="2")
        status, payload = self._post(notice)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["detail"]["reason"], "bad_version")
        self.assertEqual(dispatcher.calls, [])

    def test_non_loopback_host_is_rejected(self):
        dispatcher = _FakeDispatcher()
        chat2api._share_dispatcher = dispatcher
        status, payload = self._post(
            self._notice(11, "5" * 32), host="10.0.0.5:28110")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["type"], "share_revoke_denied")
        self.assertEqual(dispatcher.calls, [])

    def test_non_loopback_client_ip_is_rejected(self):
        self.assertFalse(chat2api.is_local_control_request(
            "10.0.0.5", "127.0.0.1:28110", None))
        self.assertFalse(chat2api.is_local_control_request(
            "127.0.0.1", "10.0.0.5:28110", None))
        self.assertFalse(chat2api.is_local_control_request(
            "127.0.0.1", "127.0.0.1:28110", "https://example.com"))

    def test_missing_shared_key_is_refused(self):
        chat2api.SHARE_ATTRIBUTION_KEY_FILE = str(
            Path(self.tmp.name) / "missing.key")
        chat2api._attribution_key_cache["stat"] = None
        chat2api._attribution_key_cache["secret"] = None
        status, payload = self._post(self._notice(12, "6" * 32))
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["type"],
                          "share_revoke_unavailable")

    def test_terminated_inflight_context_refuses_the_relay_send(self):
        # The relay checks this flag immediately before its upstream send;
        # terminate_keys marks the same context, so a request admitted before
        # the revoke cannot forward after it.
        class _Connection:
            def __init__(self):
                self.shutdowns = 0
                self.closes = 0

            def shutdown(self, how):
                self.shutdowns += 1

            def close(self):
                self.closes += 1

        connection = _Connection()
        ctx = share._InflightCtx(connection)
        self.assertFalse(ctx.terminated)
        self.assertTrue(ctx.terminate())
        self.assertTrue(ctx.terminated)
        self.assertFalse(ctx.terminate())
        self.assertEqual(connection.shutdowns, 1)
        self.assertEqual(connection.closes, 1)

    def test_without_notice_pending_ticket_stays(self):
        dispatcher = dispatch.ShareDispatcher(max_wait=10)
        ticket = dispatcher.register(KEY_ID, "pending-without-notice")
        chat2api._share_dispatcher = dispatcher
        # Negative: without the signed notice the assertion "pending == 0"
        # MUST fail; the ticket is still waiting.
        self.assertEqual(dispatcher.pending_count(), 1)
        self.assertEqual(ticket.state, "pending")
        status, payload = self._post(self._notice(13, "7" * 32))
        self.assertEqual((status, payload["released"]), (200, 1))
        self.assertEqual(dispatcher.pending_count(), 0)

    def test_partial_cached_share_entry_module_is_evicted(self):
        # A failed import leaves a partially initialized module in
        # sys.modules.  The next request must retry the real sibling source
        # instead of reusing that module and failing on a missing attribute.
        fake = types.ModuleType("share_entry")
        with mock.patch.dict(sys.modules, {"share_entry": fake}):
            module = chat2api._share_entry_module()
            self.assertIsNot(module, fake)
            self.assertTrue(hasattr(module, "load_revoke_state"))

    def test_failed_share_entry_import_does_not_cache_a_partial_module(self):
        original = sys.modules.pop("share_entry", None)

        def restore():
            if original is None:
                sys.modules.pop("share_entry", None)
            else:
                sys.modules["share_entry"] = original

        self.addCleanup(restore)

        class _Loader:
            def create_module(self, spec):
                return None

            def exec_module(self, module):
                raise OSError("boom")

        class _Spec:
            name = "share_entry"
            loader = _Loader()
            origin = None
            submodule_search_locations = None
            cached = None
            has_location = False

        with mock.patch.object(importlib.util, "spec_from_file_location",
                               return_value=_Spec()):
            with self.assertRaises(OSError):
                chat2api._share_entry_module()
        self.assertNotIn("share_entry", sys.modules)


@unittest.skip("HMAC share path retired with the merged share process")
class TestZZDefaultRevokeStateIsUntouched(_IsolatedRevokeStateTestCase):
    def test_default_state_file_is_untouched_by_the_suite(self):
        # Review #6: the PRODUCT default state belongs to the live instance.
        # Its mtime/size/content must be identical before and after the run
        # (or the path must still not exist), and the suite must resolve the
        # private temp path instead of the PRODUCT default.
        self.assertEqual(share.revoke_state_path(), self.revoke_state_file)
        self.assertNotEqual(share.revoke_state_path(), DEFAULT_REVOKE_STATE)
        self.assertNotEqual(share.revoke_state_path(), LEGACY_REVOKE_STATE)
        self.assertEqual(_default_revoke_state_fingerprint(),
                         _DEFAULT_REVOKE_STATE_BEFORE)
        self.assertEqual(_legacy_revoke_state_fingerprint(),
                         _LEGACY_REVOKE_STATE_BEFORE)


if __name__ == "__main__":
    unittest.main()
