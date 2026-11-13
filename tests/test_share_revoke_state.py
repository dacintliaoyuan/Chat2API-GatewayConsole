# -*- coding: utf-8 -*-
"""撤销/投影持久状态的回归测试。

钉死的契约（第三轮复审 #9—#15 中仍属于纯状态的部分）：

* 状态文件读失败/缺失/损坏/版本不符是明确的 fail-closed 错误，不是空状态；
* 首次可信初始化创建状态文件与私有初始化标记；已初始化后状态缺失/损坏
  不会被当成空状态，也不会覆盖内存缓存；
* 旧 release 目录里的 pre-fix 状态在首次初始化时一次性采纳并合并，之后
  不会被重新采纳，也不会被覆盖；
* generation 按真实通知时间窗裁剪（窗内迟到通知仍正确，窗外可安全回收），
  known 集合只保留当前投影 Key + 最近移除的 Key，长期增删有界。

隔离：不启动产品服务、不读写生产账本；撤销状态/投影全部写在私有临时目录。
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
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


import importlib.util  # noqa: E402

share = _load("share_auth", CHAT2API / "share_auth.py")
dispatch = _load("share_dispatch", CHAT2API / "share_dispatch.py")
chat2api = _load("chat2api_revoke_state", CHAT2API / "chat2api.py")

KEY_ID = "eee3e921-f1ee-4b5a-8fc0-e4d2eb30135f"


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


class TestAppliedSecurityGenerationWatermark(unittest.TestCase):
    """The applied revoke watermark is ONE int, never ``max`` over dicts.

    ``record_revoke_generation`` stores a per-Key dict
    (generation/applied_at/updated_at).  The watermark must report the MAX
    ``generation`` as an integer: the old
    ``max(_revoke_applied_generations.values())`` returned a dict for ONE
    revoke and raised TypeError for TWO.
    """

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
        # A trusted initialization: the later record_* calls must not re-create
        # the state file and drop the injected entries.
        share.load_revoke_state()

    def _record(self, generations):
        for key_id, generation in generations.items():
            share.record_revoke_generation(key_id, generation, 1000)

    def _assert_watermark(self, expected):
        self.assertEqual(share.applied_security_generation(), expected)
        self.assertIs(type(share.applied_security_generation()), int)

    def test_recorded_generations_survive_a_cache_reset(self):
        self._record({KEY_ID: 7, "other-key": 9})
        share.reset_revoke_state_cache()
        self.assertEqual(share.applied_security_generation(), 9)
        self.assertEqual(
            share.load_revoke_generation(KEY_ID)["generation"], 7)

    def test_zero_applied_revokes_report_zero(self):
        self._assert_watermark(0)

    def test_one_applied_revoke_reports_its_generation(self):
        self._record({KEY_ID: 7})
        self._assert_watermark(7)

    def test_two_applied_revokes_report_the_max_generation(self):
        # The exact defect: two per-Key dicts made max() raise TypeError.
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

    def test_projection_generation_advances_monotonically(self):
        self.assertTrue(share.record_projection_generation(3))
        self.assertFalse(share.record_projection_generation(3))
        self.assertFalse(share.record_projection_generation(2))
        self.assertEqual(share.applied_security_generation(), 3)
        share.reset_revoke_state_cache()
        share.load_revoke_state()
        self.assertEqual(share.applied_security_generation(), 3)


if __name__ == "__main__":
    unittest.main()
