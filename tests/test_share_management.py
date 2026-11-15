"""S3: share command layer + protected /api/share/* management API tests.

Uses a temporary SQLite store and fake broker; never touches data/um-share.json,
never migrates real keys, and does not start/stop any live host component.
"""
import http.client
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from share.management import (BrokerClientAdapter, BrokerError, NoopBroker,
                             ShareManager)  # noqa: E402
from share.broker_client import BrokerClient  # noqa: E402
import share.broker_protocol as broker_protocol  # noqa: E402
from share.store import (Conflict, Disabled, ShareStore,  # noqa: E402
                         StoreError, VersionMismatch)


class FakeBroker:
    def __init__(self):
        self.published = []
        # R8-A7: one-shot terminate_keys lists carried per publish (audit
        # seam for the revoke-and-terminate regression).
        self.terminate_keys_seen = []
        self.start_calls = 0
        self.explicit_start_calls = 0
        self.graceful_stop_calls = 0
        self.emergency_stop_calls = 0
        self.emergency_stop_result = {"state": "stopped", "stopped": True}
        # S2: model the REAL 8100 runtime's security watermark. The
        # runtime advances its own applied revoke watermark ONLY when a Key
        # transitions from enabled to disabled/absent (a REVOKE). A rotate
        # (same Key, new secret_version) and a lowered limit leave it
        # untouched, so a pending rotate/limit_lower intent is NEVER
        # confirmed by this ACK — the management fence then fails closed
        # exactly as the real end-to-end chain does.
        self._enabled = set()
        self._security_generation = 0

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
                "instance_id": "fake-instance",
                "security_generation": self._security_generation,
                # D18-3: a real instance ACK separates all four counters.
                # These fixture values are deliberately distinct so a dropped
                # field cannot masquerade as another count.
                "terminated": 2 if terminate_keys else None,
                "released": 3 if terminate_keys else None,
                "aborted": 4 if terminate_keys else None,
                "upstream_closed": 5 if terminate_keys else None}

    def start(self, explicit=False):
        self.start_calls += 1
        self.explicit_start_calls += 1 if explicit else 0
        return {"state": "ok", "started": True}

    def graceful_stop(self):
        self.graceful_stop_calls += 1
        # R31-1：drained 只说明排空阶段，完成须显式最终停止确认。
        return {"state": "stopped", "drained": True, "stopped": True}

    def emergency_stop(self):
        self.emergency_stop_calls += 1
        return dict(self.emergency_stop_result)


class ShareManagerTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.td.cleanup)
        self.broker = FakeBroker()
        self.mgr = ShareManager(store_path=Path(self.td.name) / "share.db",
                                broker=self.broker)

    def test_create_publish_and_read_back_without_secret(self):
        res = self.mgr.create_key("friendA")
        self.assertTrue(res["secret"].startswith("um-share-"))
        self.assertTrue(res["in_sync"])
        self.assertEqual(self.broker.published[-1]["config_generation"],
                         res["desired_generation"])
        keys = self.mgr.list_keys()["keys"]
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0]["name"], "friendA")
        self.assertNotIn("secret_hash", keys[0])
        self.assertNotIn("secret", keys[0])

    def test_update_rotate_delete_same_key_id(self):
        created = self.mgr.create_key("friendB")
        kid = created["key_id"]
        updated = self.mgr.update_key(kid, name="friendB2")
        self.assertEqual(updated["name"], "friendB2")
        # D18-3: a revoke tombstones the Key (deleted_at + cleared
        # ciphertext) and advances the runtime's applied revoke watermark.
        revoked = self.mgr.revoke_and_terminate_key(kid)
        self.assertFalse(revoked["enabled"])
        self.assertTrue(revoked["deleted_at"])
        self.assertFalse(revoked["plaintext_viewable"])
        with self.assertRaises(Disabled):
            self.mgr.reveal_key_secret(kid)
        # A tombstoned identity can no longer be rotated: the rotate
        # transaction is refused before any new secret version is written.
        with self.assertRaises(Disabled):
            self.mgr.rotate_key(kid)
        self.assertEqual(self.mgr.get_key(kid)["secret_version"], 1)
        # A second delete is idempotent: the tombstone stays, and the
        # publication still confirms the current generation.
        deleted = self.mgr.delete_key(kid)
        self.assertTrue(deleted["already_deleted"])
        # Tombstone stays retrievable for future accounting, but not listed as
        # an active identity.
        tombstone = self.mgr.get_key(kid)
        self.assertTrue(tombstone["deleted_at"])
        self.assertNotIn(kid, [k["key_id"] for k in self.mgr.list_keys()["keys"]])

    def test_replayed_create_never_returns_secret(self):
        first = self.mgr.create_key("A", idempotency_key="idem-1")
        second = self.mgr.create_key("A2", idempotency_key="idem-1")
        self.assertEqual(first["key_id"], second["key_id"])
        self.assertNotIn("secret", second)
        self.assertTrue(second["secret_unavailable"])

    def test_duplicate_name_conflict(self):
        self.mgr.create_key("dup")
        with self.assertRaises(Conflict):
            self.mgr.create_key("dup")

    def test_noop_broker_means_saved_but_offline(self):
        mgr = ShareManager(store_path=Path(self.td.name) / "offline.db",
                           broker=NoopBroker())
        res = mgr.create_key("offline")
        self.assertFalse(res["published"])
        self.assertFalse(res["in_sync"])
        status = mgr.status()
        self.assertEqual(status["total_keys"], 1)
        self.assertEqual(status["broker"]["state"], "offline")
        self.assertFalse(status["in_sync"])

    def _wait_manager_operation(self, operation_id, expected, timeout=5.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.mgr.get_operation(operation_id)
            if last["status"] == expected:
                return last
            time.sleep(0.05)
        self.fail(f"operation never reached {expected}: {last}")

    def test_host_actions_use_broker(self):
        res = self.mgr.start()
        self.assertEqual(res["status"], "pending")
        op = self._wait_manager_operation(res["operation_id"], "completed")
        self.assertEqual(op["payload"]["broker"]["started"], True)

    def test_explicit_start_uses_a_partial_broker_start(self):
        res = self.mgr.start(explicit=True)
        op = self._wait_manager_operation(res["operation_id"], "completed")
        self.assertEqual(op["payload"]["broker"]["started"], True)
        self.assertEqual(self.broker.start_calls, 1)
        self.assertEqual(self.broker.explicit_start_calls, 1)

    def test_host_action_retry_with_same_idempotency_key_reuses_operation(self):
        first = self.mgr.start(idempotency_key="ops-key-1")
        second = self.mgr.start(idempotency_key="ops-key-1")
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertTrue(second["reused"])
        self._wait_manager_operation(first["operation_id"], "completed")
        self.assertEqual(self.broker.start_calls, 1)

    def test_duplicate_host_action_while_pending_is_reused(self):
        release = threading.Event()
        real_graceful = self.broker.graceful_stop

        def gated_graceful():
            release.wait(5)
            return real_graceful()

        self.broker.graceful_stop = gated_graceful
        first = self.mgr.graceful_stop()
        second = self.mgr.graceful_stop()  # first still pending -> same op
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertTrue(second["reused"])
        release.set()
        self._wait_manager_operation(first["operation_id"], "completed")
        self.assertEqual(self.broker.graceful_stop_calls, 1)

    def test_restart_reconciles_pending_host_action_as_failed(self):
        # 模拟崩溃后重启：pending 的 host action 必须变为可查询的 failed。
        probe = ShareStore(Path(self.td.name) / "share.db")
        try:
            probe.record_operation("emergency_stop", "pending", {})
        finally:
            probe.close()
        # 本测试模拟新进程接管：显式清除同 store 的一次性恢复守卫。
        ShareManager._recovered_paths.discard(
            str((Path(self.td.name) / "share.db").resolve()))
        recovered = ShareManager(store_path=Path(self.td.name) / "share.db",
                                 broker=self.broker)
        store = recovered._open()
        try:
            pending = [o for o in store.operations(kind="emergency_stop")][0]
        finally:
            store.close()
        self.assertEqual(pending["status"], "failed")
        self.assertIn("restarted", pending["payload"]["error"])


class ShareLimitsTests(unittest.TestCase):
    """D18-4 concurrency limits at the command layer: create default 16,
    update lowering = fail-closed revocation direction, raising = best
    effort; set_share_limits validation, 409 conflict and fail-closed."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.td.cleanup)
        self.broker = FakeBroker()
        self.path = Path(self.td.name) / "share.db"
        self.mgr = ShareManager(store_path=self.path, broker=self.broker)

    def test_create_key_defaults_to_16(self):
        res = self.mgr.create_key("friendD8")
        self.assertEqual(self.mgr.get_key(res["key_id"])["max_concurrency"], 16)

    def test_create_key_custom_limit(self):
        res = self.mgr.create_key("friendC", max_concurrency=16)
        self.assertEqual(self.mgr.get_key(res["key_id"])["max_concurrency"], 16)

    def test_create_key_limit_out_of_range_rejected(self):
        for bad in (0, 49, 65, True, "8", 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(StoreError):
                    self.mgr.create_key("friendX", max_concurrency=bad)

    def test_create_key_replay_returns_no_secret(self):
        first = self.mgr.create_key("friendR", max_concurrency=8,
                                    idempotency_key="lim-create-1")
        second = self.mgr.create_key("friendR2", max_concurrency=8,
                                     idempotency_key="lim-create-1")
        self.assertEqual(first["key_id"], second["key_id"])
        self.assertNotIn("secret", second)
        self.assertTrue(second["secret_unavailable"])

    def test_update_key_lowering_fail_closed(self):
        # 调低 = 撤销向：发布未确认 → fail-closed BrokerError（503 同型），
        # 绝不假 200。权威库保存期望值（desired），但发布 blocked、in_sync
        # False —— 在线投影仍是旧值，管理层绝不谎报已生效。
        created = self.mgr.create_key("friendLow")
        gen = self.mgr.status()["desired_generation"]
        self.broker.publish = lambda projection: None   # 从不确认
        with self.assertRaises(BrokerError):
            self.mgr.update_key(created["key_id"], max_concurrency=2,
                                expected_row_version=1)
        self.assertEqual(self.mgr.get_key(created["key_id"])["max_concurrency"],
                         2)                      # desired 已保存
        status = self.mgr.status()
        self.assertFalse(status["in_sync"])       # 未确认在线，绝不谎报生效
        self.assertGreaterEqual(self.broker.emergency_stop_calls, 1)
        blocker = status["blocked_publications"][0]
        self.assertEqual(blocker["status"], "failed")
        self.assertEqual(status["desired_generation"], gen + 1)

    def test_update_key_raising_is_best_effort(self):
        created = self.mgr.create_key("friendUp", max_concurrency=2)
        self.broker.publish = lambda projection: None   # 从不确认
        result = self.mgr.update_key(created["key_id"], max_concurrency=16,
                                     expected_row_version=1)
        self.assertFalse(result["published"])
        self.assertEqual(self.mgr.get_key(created["key_id"])["max_concurrency"],
                         16)
        self.assertFalse(self.mgr.status()["blocked"])

    def test_update_key_lowering_confirmed_online(self):
        # A lowered limit is a REVOKING direction: the runtime's revoke
        # watermark does not advance for it, so the publication fails
        # closed while the desired value is saved.
        created = self.mgr.create_key("friendOK")
        with self.assertRaises(BrokerError):
            self.mgr.update_key(created["key_id"], max_concurrency=3,
                                 expected_row_version=1)
        self.assertEqual(self.mgr.get_key(created["key_id"])["max_concurrency"], 3)
        self.assertFalse(self.mgr.status()["in_sync"])

    def test_revoke_still_fail_closed_for_enabled_false(self):
        created = self.mgr.create_key("friendDis")
        self.broker.publish = lambda projection: None
        with self.assertRaises(BrokerError):
            self.mgr.revoke_and_terminate_key(created["key_id"],
                                              expected_row_version=1)

    def test_set_share_limits_validation(self):
        for bad in (0, 65, 129, True, "8", 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(StoreError):
                    self.mgr.set_share_limits(bad)
        with self.assertRaises(StoreError) as ctx:
            self.mgr.set_share_limits(65)
        self.assertIn("1..64", str(ctx.exception))

    def test_set_share_limits_accepts_global_64_while_keys_stay_48(self):
        # 全局 1..64 与每 Key 1..48 是两套独立契约：64 对全局合法、对
        # Key 仍非法；64 必须真实传播到发布投影。
        result = self.mgr.set_share_limits(64)
        self.assertTrue(result["published"])
        self.assertTrue(result["in_sync"])
        self.assertEqual(result["share_max_concurrency"], 64)
        self.assertEqual(self.mgr.status()["share_max_concurrency"], 64)
        self.assertEqual(self.broker.published[-1]["share_max_concurrency"],
                         64)
        created = self.mgr.create_key("keyAt48", max_concurrency=48)
        self.assertEqual(
            self.mgr.get_key(created["key_id"])["max_concurrency"], 48)
        gen = self.mgr.status()["desired_generation"]
        for bad in (0, 49, 65, True, "48", 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(StoreError):
                    self.mgr.create_key("bad-" + str(bad),
                                        max_concurrency=bad)
        with self.assertRaises(StoreError):
            self.mgr.update_key(created["key_id"], max_concurrency=49)
        # 被拒的 Key 校验既不推进代次也不改写已发布投影。
        self.assertEqual(self.mgr.status()["desired_generation"], gen)
        self.assertEqual(self.mgr.status()["share_max_concurrency"], 64)
        self.assertEqual(self.broker.published[-1]["share_max_concurrency"],
                         64)
        # 调低 64→24 是撤销向：runtime ACK 的 security_generation 未
        # 覆盖这条 limit_lower 意图，发布必须 fail-closed。
        with self.assertRaises(BrokerError):
            self.mgr.set_share_limits(24)
        self.assertEqual(self.mgr.status()["share_max_concurrency"], 24)
        self.assertFalse(self.mgr.status()["in_sync"])

    def test_set_share_limits_expected_conflict_is_version_mismatch(self):
        # 64→10 lowering fails closed (the runtime watermark does not
        # advance on a lowered limit), but the desired value is saved.
        with self.assertRaises(BrokerError):
            self.mgr.set_share_limits(10)
        self.assertEqual(self.mgr.status()["share_max_concurrency"], 10)
        with self.assertRaises(VersionMismatch):
            self.mgr.set_share_limits(12, expected_share_max=9)
        self.assertEqual(self.mgr.status()["share_max_concurrency"], 10)

    def test_set_share_limits_lowering_fail_closed(self):
        self.broker.publish = lambda projection: None
        with self.assertRaises(BrokerError):
            self.mgr.set_share_limits(4)
        status = self.mgr.status()
        self.assertEqual(status["share_max_concurrency"], 4)  # desired 已保存
        self.assertFalse(status["in_sync"])                   # 未确认不谎报
        self.assertGreaterEqual(self.broker.emergency_stop_calls, 1)
        self.assertEqual(status["blocked_publications"][0]["status"], "failed")

    def test_set_share_limits_at_max_is_noop_and_above_rejected(self):
        # 64 is the maximum and the default: the same-value write is a
        # no-op published best-effort, and 65 is refused before any write.
        result = self.mgr.set_share_limits(64)
        self.assertTrue(result["published"])
        self.assertEqual(self.mgr.status()["share_max_concurrency"], 64)
        with self.assertRaises(StoreError):
            self.mgr.set_share_limits(65)

    def test_set_share_limits_lowering_confirmed_online(self):
        # A lowered limit is a REVOKING direction: the runtime's revoke
        # watermark does not advance for it, so the publication fails
        # closed while the desired value is saved.
        with self.assertRaises(BrokerError):
            self.mgr.set_share_limits(48)
        status = self.mgr.status()
        self.assertEqual(status["share_max_concurrency"], 48)
        self.assertFalse(status["in_sync"])

    def test_set_share_limits_replay_keeps_value(self):
        with self.assertRaises(BrokerError):
            self.mgr.set_share_limits(20, idempotency_key="limits-1")
        gen = self.mgr.status()["desired_generation"]
        # The replay returns the stored metadata (no second mutation) and is
        # STILL blocked by the pending limit_lower intent; the best-effort
        # path reports the failure instead of pretending a success.
        replay = self.mgr.set_share_limits(30, idempotency_key="limits-1")
        self.assertTrue(replay["replayed"])
        self.assertFalse(replay["published"])
        self.assertIn("does not cover", replay["publication_error"])
        self.assertEqual(self.mgr.status()["desired_generation"], gen)
        store = self.mgr._open()
        try:
            stored = store.set_share_max_concurrency(
                30, idempotency_key="limits-1")
        finally:
            store.close()
        self.assertTrue(stored["replayed"])
        self.assertEqual(stored["share_max_concurrency"], 20)

    def test_status_reports_v2_schema_and_share_max(self):
        status = self.mgr.status()
        # R11-B1: the status DB marker follows SCHEMA_VERSION (3); the
        # share-side projection stays at PROJECTION_SCHEMA_VERSION (2).
        from share.store import SCHEMA_VERSION
        self.assertEqual(status["schema_version"], SCHEMA_VERSION)
        self.assertEqual(status["share_max_concurrency"], 64)
        self.assertEqual(status["share_max_concurrency_stored"], 64)
        self.assertFalse(status["limits_normalization_required"])
        self.assertEqual(status["limits_clamped_keys"], 0)


class PublicationCoordinationTests(unittest.TestCase):
    """R5-b: publications are serialized; a stale generation is refused
    before the broker sees it and a same-generation publish must carry
    identical content."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.td.cleanup)
        self.broker = FakeBroker()
        self.path = Path(self.td.name) / "share.db"
        self.mgr = ShareManager(store_path=self.path, broker=self.broker)

    def test_stale_generation_publish_is_refused_before_broker(self):
        self.mgr.create_key("friendP1")
        probe = ShareStore(self.path)
        try:
            stale = probe.render_projection()  # 快照早于下一次 bump
        finally:
            probe.close()
        self.mgr.create_key("friendP2")  # 更新一代已发布
        before = len(self.broker.published)
        store = self.mgr._open()
        try:
            store.render_projection = lambda: stale  # 模拟读到旧快照
            published, _, error, op_id = self.mgr._publish(store)
        finally:
            store.close()
        self.assertFalse(published)
        self.assertIn("regression", error)
        # broker 只收到两次合法发布（两次 create），旧代次没有到达 broker。
        self.assertEqual(len(self.broker.published), before)
        self.assertEqual(self.mgr.get_operation(op_id)["status"], "failed")

    def test_pending_publish_becomes_blocked_after_restart(self):
        probe = ShareStore(self.path)
        try:
            probe.record_operation("publish", "pending",
                                   {"target_generation": 7, "revoking": True})
        finally:
            probe.close()
        # 模拟新进程接管：清除同 store 的一次性恢复守卫。
        ShareManager._recovered_paths.discard(str(self.path.resolve()))
        # broker 关停无法确认 -> 恢复后继续 blocked（R4-a.1 fail-closed）。
        self.broker.emergency_stop_result = {"state": "stopped"}
        recovered = ShareManager(store_path=self.path, broker=self.broker)
        status = recovered.status()
        self.assertTrue(status["blocked"])
        blocker = status["blocked_publications"][0]
        self.assertEqual(blocker["status"], "blocked")
        self.assertEqual(blocker["target_generation"], 7)

    def test_pending_publish_recovery_drives_stop_and_confirms(self):
        # 恢复时通过假受限 broker 驱动关停并核验：确认关停则不再 blocked。
        probe = ShareStore(self.path)
        try:
            probe.record_operation("publish", "pending",
                                   {"target_generation": 8, "revoking": True})
        finally:
            probe.close()
        ShareManager._recovered_paths.discard(str(self.path.resolve()))
        recovered = ShareManager(store_path=self.path, broker=self.broker)
        status = recovered.status()
        self.assertGreaterEqual(self.broker.emergency_stop_calls, 1)
        self.assertFalse(status["blocked"])
        blocker = status["blocked_publications"][0]
        self.assertTrue(blocker["stop_confirmed"])

    def test_intent_gap_is_synthesized_blocked_on_restart(self):
        # R4-a.1 崩溃窗口：撤销变更已提交（desired 领先）但 publish intent
        # 从未落库——重启对账绝不按安全推断：合成 blocked 记录并驱动受限
        # 关停。任何已存在的记录都是对领先的解释；完全没有记录才是缺口。
        probe = ShareStore(self.path)
        try:
            probe.create_key("friendGap")
            # 模拟 intent 内嵌之前的崩溃现场：变更已提交且意图从未落库
            #（分诊前无任何 publish 记录）。
            probe._conn.execute("DELETE FROM ops WHERE kind='publish'")
            probe._conn.commit()
        finally:
            probe.close()
        ShareManager._recovered_paths.discard(str(self.path.resolve()))
        self.broker.emergency_stop_result = {"state": "stopped"}  # 未确认
        recovered = ShareManager(store_path=self.path, broker=self.broker)
        status = recovered.status()
        self.assertGreaterEqual(self.broker.emergency_stop_calls, 1)
        self.assertTrue(status["pending_revocation"])
        self.assertTrue(status["blocked"])
        gap_blocker = next(b for b in status["blocked_publications"]
                           if b["status"] == "blocked")
        self.assertEqual(gap_blocker["recovered"], "intent_gap")

    def test_never_used_store_is_not_an_intent_gap(self):
        # 新库初始状态（applied=0/desired=1、无 keys、无 publish 历史）
        # 不是丢失的撤销：恢复不得驱动关停、不得标 blocked。
        ShareManager._recovered_paths.discard(str(self.path.resolve()))
        recovered = ShareManager(store_path=self.path, broker=self.broker)
        status = recovered.status()
        self.assertEqual(self.broker.emergency_stop_calls, 0)
        self.assertFalse(status["blocked"])
        self.assertEqual(status["blocked_publications"], [])


class CrashRecoveryTests(unittest.TestCase):
    """§12 R4-a.1/R6-a.1/R6-a.2/R6-a.3: crash-window reconciliation,
    action-bound idempotency keys and barrier-timed claim concurrency."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.td.cleanup)
        self.path = Path(self.td.name) / "share.db"
        self.broker = FakeBroker()
        # R31-1 收紧后 {"state": "stopped"} 不再算确认——默认确认，需要
        # 未确认的用例自己覆盖 emergency_stop_result。
        self.mgr = ShareManager(store_path=self.path, broker=self.broker)

    def _set_confirmed(self):
        # R31-1 收紧后 host 动作完成需要显式 stopped=True。
        self.broker.emergency_stop_result = {"state": "stopped",
                                             "stopped": True}

    def _recover(self):
        ShareManager._recovered_paths.discard(str(self.path.resolve()))
        return ShareManager(store_path=self.path, broker=self.broker)

    def _wait(self, mgr, operation_id, expected, timeout=5.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = mgr.get_operation(operation_id)
            if last["status"] == expected:
                return last
            time.sleep(0.05)
        self.fail(f"operation never reached {expected}: {last}")

    def test_crash_between_commit_and_publish_is_recovered_fail_closed(self):
        # R4-a.1：delete 与其 publish intent 在同一事务提交；本测试模拟
        # "变更已提交、发布未执行"的崩溃现场，重启对账必须驱动受限关停。
        probe = ShareStore(self.path)
        try:
            created = probe.create_key("friendC")
            probe.delete_key(created["key_id"])
            desired = probe.generation()["config_generation"]
        finally:
            probe.close()
        recovered = self._recover()
        status = recovered.status()
        self.assertGreaterEqual(self.broker.emergency_stop_calls, 1)
        self.assertTrue(status["pending_revocation"])
        blocker = next(b for b in status["blocked_publications"]
                       if b["target_generation"] == desired)
        self.assertEqual(blocker["status"], "blocked")

    def test_intent_is_recorded_in_the_same_transaction_as_the_change(self):
        # 变更提交后 intent 必然已在库中（同一 BEGIN IMMEDIATE）：不再存在
        # "落库后、意图前"的窗口。
        probe = ShareStore(self.path)
        try:
            probe.create_key("friendT")
            desired = probe.generation()["config_generation"]
            intents = [o for o in probe.operations(kind="publish")
                       if o["payload"].get("target_generation") == desired
                       and o["status"] == "pending"]
            self.assertEqual(len(intents), 1)
            self.assertEqual(intents[0]["payload"]["action_kind"], "create")
        finally:
            probe.close()

    def test_running_host_action_is_failed_on_restart(self):
        self._set_confirmed()
        # R6-a.1：running 也是非终态——执行器死亡的 running 操作恢复时标
        # failed，后续请求不复用它（建新 op 并真正执行）。
        probe = ShareStore(self.path)
        try:
            probe.record_operation("emergency_stop", "running", {})
            stale_id = probe.operations(kind="emergency_stop")[0]["id"]
        finally:
            probe.close()
        recovered = self._recover()
        store = recovered._open()
        try:
            op = store.operations(kind="emergency_stop")[0]
        finally:
            store.close()
        self.assertEqual(op["status"], "failed")
        self.assertEqual(op["payload"]["recovered_from"], "running")
        res = recovered.emergency_stop()
        self.assertNotEqual(res["operation_id"], stale_id)
        self.assertNotIn("reused", res)
        self._wait(recovered, res["operation_id"], "completed")

    def test_idempotency_key_is_bound_to_the_action_kind(self):
        self._set_confirmed()
        # R6-a.2：跨动作复用幂等键返回 409，绝不回放无关成功、绝不吞掉
        # 紧急停止。
        first = self.mgr.start(idempotency_key="same-request")
        self._wait(self.mgr, first["operation_id"], "completed")
        with self.assertRaises(Conflict):
            self.mgr.emergency_stop(idempotency_key="same-request")
        with self.assertRaises(Conflict):
            self.mgr.graceful_stop(idempotency_key="same-request")
        # 停止没有被吞：无 key 的 emergency_stop 正常执行。
        stop = self.mgr.emergency_stop()
        self._wait(self.mgr, stop["operation_id"], "completed")
        self.assertEqual(self.broker.emergency_stop_calls, 1)

    def test_barrier_concurrent_keyless_start_executes_once(self):
        self._set_confirmed()
        # R6-a.3：屏障控制并发时序（确定性，不靠连续复跑碰运气）——
        # 同类无 key 并发 claim 在同一事务内认领，至多一个执行器。
        start_gate = threading.Event()
        real_start = self.broker.start

        def gated_start():
            start_gate.wait(5)
            return real_start()

        self.broker.start = gated_start
        barrier = threading.Barrier(6)
        results = []
        lock = threading.Lock()

        def launch():
            barrier.wait()
            r = self.mgr.start()
            with lock:
                results.append(r)

        threads = [threading.Thread(target=launch) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        op_ids = {r["operation_id"] for r in results}
        self.assertEqual(len(op_ids), 1)
        start_gate.set()
        self._wait(self.mgr, results[0]["operation_id"], "completed")
        self.assertEqual(self.broker.start_calls, 1)

    def test_new_operation_after_terminal_one_is_allowed(self):
        self._set_confirmed()
        self._set_confirmed()
        # 终态后的同类新操作正常执行（去重只针对非终态）。
        first = self.mgr.start()
        self._wait(self.mgr, first["operation_id"], "completed")
        second = self.mgr.start()
        self.assertNotEqual(first["operation_id"], second["operation_id"])
        self._wait(self.mgr, second["operation_id"], "completed")
        self.assertEqual(self.broker.start_calls, 2)

    def test_blocked_intent_is_retried_on_next_restart_and_confirmed(self):
        # §14 缺口 1 / §16-B：第一次恢复关停未确认（**首次恰调用 1 次**）
        # → blocked；第二次恢复 broker 已恢复可用 → 确实再次调用停止并更
        # 新确认状态；已确认关闭的记录在第三次恢复不重复无意义关停。
        probe = ShareStore(self.path)
        try:
            probe.record_operation("publish", "pending",
                                   {"target_generation": 9, "revoking": True})
        finally:
            probe.close()
        # 第一次恢复：关停失败（broker 不可用）→ blocked，调用数恰为 1。
        self.broker.emergency_stop_result = {"state": "stopped"}
        first = self._recover()
        self.assertEqual(self.broker.emergency_stop_calls, 1)
        status = first.status()
        blocker = next(b for b in status["blocked_publications"])
        self.assertEqual(blocker["status"], "blocked")
        self.assertFalse(blocker["stop_confirmed"])
        calls_after_first = self.broker.emergency_stop_calls
        # 第二次恢复：broker 可确认 → 有界重试确实调用停止并更新确认。
        self.broker.emergency_stop_result = {"state": "stopped", "stopped": True}
        second = self._recover()
        status2 = second.status()
        self.assertEqual(self.broker.emergency_stop_calls, calls_after_first + 1)
        blocker2 = next(b for b in status2["blocked_publications"])
        self.assertTrue(blocker2["stop_confirmed"])
        # 已确认关闭：第三次恢复不再重复关停。
        third = self._recover()
        self.assertEqual(self.broker.emergency_stop_calls, calls_after_first + 1)
        self.assertFalse(third.status()["blocked"])

    def test_first_recovery_stops_exactly_once_per_intent(self):
        # §16-B 定向：单个 pending 撤销首次恢复失败恰调用 1；第二次启动仍
        # 失败累计 2；第三次确认累计 3；第四次保持 3（同一启动内每个意图
        # 至多一次尝试）。
        probe = ShareStore(self.path)
        try:
            probe.record_operation("publish", "pending",
                                   {"target_generation": 11, "revoking": True})
        finally:
            probe.close()
        self.broker.emergency_stop_result = {"state": "stopped"}  # 未确认
        first = self._recover()
        self.assertEqual(self.broker.emergency_stop_calls, 1)
        self.assertTrue(first.status()["blocked"])
        second = self._recover()
        self.assertEqual(self.broker.emergency_stop_calls, 2)
        self.assertTrue(second.status()["blocked"])
        self.broker.emergency_stop_result = {"state": "stopped", "stopped": True}
        third = self._recover()
        self.assertEqual(self.broker.emergency_stop_calls, 3)
        self.assertFalse(third.status()["blocked"])
        fourth = self._recover()
        self.assertEqual(self.broker.emergency_stop_calls, 3)

    def test_unknown_intent_keeps_dangerous_class_across_restarts(self):
        # §16-A：缺少 revoking 的未知意图——首次恢复失败标 blocked 后分类
        # 不丢失（不靠补写 revoking 字段改类）：第二次启动 broker 恢复时
        # 确实重试并持久化确认；第三次不重复；明确非撤销不被改类。
        probe = ShareStore(self.path)
        try:
            probe.record_operation("publish", "pending",
                                   {"target_generation": 12})  # 无 revoking
        finally:
            probe.close()
        self.broker.emergency_stop_result = {"state": "stopped"}   # 未确认
        first = self._recover()
        self.assertEqual(self.broker.emergency_stop_calls, 1)
        self.assertTrue(first.status()["blocked"])
        store = first._open()
        try:
            op = next(o for o in store.operations(kind="publish"))
            self.assertEqual(op["status"], "blocked")
            self.assertNotIn("revoking", op["payload"])  # 分类未靠补写
        finally:
            store.close()
        self.broker.emergency_stop_result = {"state": "stopped", "stopped": True}
        second = self._recover()
        self.assertEqual(self.broker.emergency_stop_calls, 2)
        blocker = next(b for b in second.status()["blocked_publications"])
        self.assertTrue(blocker["stop_confirmed"])
        third = self._recover()
        self.assertEqual(self.broker.emergency_stop_calls, 2)

    def test_explicitly_non_revoking_intent_is_never_retried(self):
        # §16-A：revoking=False 明确非撤销——pending 恢复标 failed（安全方
        # 向），blocked/failed 不被改类、不驱动关停、不因缺省处理重试。
        probe = ShareStore(self.path)
        try:
            probe.record_operation("publish", "pending",
                                   {"target_generation": 13, "revoking": False})
        finally:
            probe.close()
        self.broker.emergency_stop_result = {"state": "stopped", "stopped": True}
        recovered = self._recover()
        self.assertEqual(self.broker.emergency_stop_calls, 0)
        store = recovered._open()
        try:
            op = next(o for o in store.operations(kind="publish"))
            self.assertEqual(op["status"], "failed")
            self.assertEqual(op["payload"]["revoking"], False)
        finally:
            store.close()
        again = self._recover()
        self.assertEqual(self.broker.emergency_stop_calls, 0)

    def test_gap_with_applied_history_is_still_recognized(self):
        # §18-①：已应用历史只用于归因——applied=2 有已应用记录、撤销提交
        # 为 desired=3 但 gen3 意图缺失 → 必须识别 gap（合成 blocked 并驱
        # 动受限关停），旧记录不能掩盖当前代次缺口。
        probe = ShareStore(self.path)
        try:
            probe.create_key("friendH")
            # gen2：意图收尾为 applied 并确认应用。
            for op in probe.operations(kind="publish"):
                probe.update_operation(op["id"], "applied", {"generation": 2})
            probe.commit_applied(2)
            # 模拟撤销提交为 gen3 但 gen3 意图从未落库（旧版本崩溃/异常）。
            probe._conn.execute("UPDATE configgen SET config_generation = 3")
            probe._conn.commit()
            self.assertEqual(probe.generation()["applied_generation"], 2)
            self.assertEqual(probe.generation()["config_generation"], 3)
        finally:
            probe.close()
        ShareManager._recovered_paths.discard(str(self.path.resolve()))
        self.broker.emergency_stop_result = {"state": "stopped"}  # 未确认
        recovered = ShareManager(store_path=self.path, broker=self.broker)
        status = recovered.status()
        self.assertGreaterEqual(self.broker.emergency_stop_calls, 1)
        gap = next(b for b in status["blocked_publications"]
                   if isinstance(b.get("recovered"), str)
                   and "gap" in b["recovered"])
        self.assertTrue(gap["blocked"])

    def test_newer_nonrevoking_intent_cannot_mask_missing_revocation(self):
        # §18-②：较新的明确非撤销意图（gen3）不能掩盖中间撤销代次（gen2）
        # 的记录缺失——gen2 无有效解释 → gap 合成并驱动关停。
        probe = ShareStore(self.path)
        try:
            probe.record_operation("publish", "failed",
                                   {"target_generation": 3, "revoking": False,
                                    "recovered": True})
            probe._conn.execute("UPDATE configgen SET config_generation = 3")
            probe._conn.commit()
        finally:
            probe.close()
        ShareManager._recovered_paths.discard(str(self.path.resolve()))
        self.broker.emergency_stop_result = {"state": "stopped"}  # 未确认
        recovered = ShareManager(store_path=self.path, broker=self.broker)
        status = recovered.status()
        self.assertGreaterEqual(self.broker.emergency_stop_calls, 1)
        gap = next(b for b in status["blocked_publications"]
                   if isinstance(b.get("recovered"), str)
                   and "gap" in b["recovered"])
        self.assertTrue(gap["blocked"])

    def test_unapplied_range_fully_explained_by_nonrevoking_is_safe(self):
        # §18-③：未应用代次全由明确非撤销意图解释（分诊转 failed）——零
        # 关停、无 gap 合成。
        probe = ShareStore(self.path)
        try:
            probe.record_operation("publish", "pending",
                                   {"target_generation": 2, "revoking": False})
            probe.record_operation("publish", "pending",
                                   {"target_generation": 3, "revoking": False})
            probe._conn.execute("UPDATE configgen SET config_generation = 3")
            probe._conn.commit()
        finally:
            probe.close()
        ShareManager._recovered_paths.discard(str(self.path.resolve()))
        recovered = ShareManager(store_path=self.path, broker=self.broker)
        self.assertEqual(self.broker.emergency_stop_calls, 0)
        status = recovered.status()
        self.assertFalse(status["blocked"])
        store = recovered._open()
        try:
            for op in store.operations(kind="publish"):
                self.assertEqual(op["status"], "failed")
                self.assertEqual(op["payload"]["revoking"], False)
        finally:
            store.close()

    def test_initial_empty_store_has_zero_stops(self):
        # §18-④：初始空库（applied=0/desired=1、无 keys、无记录）不是变更
        # 缺口：零关停、无 blocked。
        ShareManager._recovered_paths.discard(str(self.path.resolve()))
        recovered = ShareManager(store_path=self.path, broker=self.broker)
        status = recovered.status()
        self.assertEqual(self.broker.emergency_stop_calls, 0)
        self.assertFalse(status["blocked"])

    def _scenario2_store(self):
        """§18-② 现场：applied=0、desired=3，gen3 有明确非撤销意图、gen2 缺失。"""
        probe = ShareStore(self.path)
        try:
            probe.record_operation("publish", "failed",
                                   {"target_generation": 3, "revoking": False,
                                    "recovered": True})
            probe._conn.execute("UPDATE configgen SET config_generation = 3")
            probe._conn.commit()
        finally:
            probe.close()

    def _gap_ids(self, mgr):
        store = mgr._open()
        try:
            return [o["id"] for o in store.operations(kind="publish")
                    if isinstance(o["payload"].get("unexplained_generations"),
                                  list)]
        finally:
            store.close()

    def test_confirmed_gap_is_reused_without_resynthesis(self):
        # §20 验收 1：场景②首轮合成 gap 且停止即确认 → 第二/三次恢复不新
        # 增 gap、不增加停止调用，记录数稳定。
        self._scenario2_store()
        self.broker.emergency_stop_result = {"state": "stopped", "stopped": True}
        first = self._recover()
        self.assertEqual(self.broker.emergency_stop_calls, 1)
        store = first._open()
        try:
            gaps = [o for o in store.operations(kind="publish")
                    if isinstance(o["payload"].get("unexplained_generations"),
                                  list)]
            self.assertEqual(len(gaps), 1)
            self.assertEqual(gaps[0]["payload"]["unexplained_generations"], [2])
        finally:
            store.close()
        second = self._recover()
        self.assertEqual(self.broker.emergency_stop_calls, 1)
        third = self._recover()
        self.assertEqual(self.broker.emergency_stop_calls, 1)
        self.assertEqual(len(self._gap_ids(third)), 1)   # 记录数稳定

    def test_unconfirmed_gap_retries_and_reuses_same_record(self):
        # §20 验收 2：首轮未确认、第二次确认——复用同一 gap 工作（次数
        # 1→2），第三次保持 2，记录数稳定（不增殖）。
        self._scenario2_store()
        self.broker.emergency_stop_result = {"state": "stopped"}  # 未确认
        first = self._recover()
        self.assertEqual(self.broker.emergency_stop_calls, 1)
        gap_ids = self._gap_ids(first)
        self.assertEqual(len(gap_ids), 1)
        self.broker.emergency_stop_result = {"state": "stopped", "stopped": True}
        second = self._recover()
        self.assertEqual(self.broker.emergency_stop_calls, 2)   # 复用同一工作
        self.assertEqual(self._gap_ids(second), gap_ids)        # 同一记录
        third = self._recover()
        self.assertEqual(self.broker.emergency_stop_calls, 2)   # 保持 2
        self.assertEqual(self._gap_ids(third), gap_ids)         # 不增殖

    def test_new_missing_generation_beyond_recorded_gap_is_recognized(self):
        # §20 验收 3：已记录缺口之外出现新缺失代次 → 仍能识别并处理；旧
        # gap 不掩盖新缺口。
        self._scenario2_store()
        self.broker.emergency_stop_result = {"state": "stopped", "stopped": True}
        first = self._recover()   # 合成 gap（[2]）并确认关停
        self.assertEqual(self.broker.emergency_stop_calls, 1)
        # 新缺失代次 gen4：desired 推进到 4 且无任何解释。
        probe = ShareStore(self.path)
        try:
            probe._conn.execute("UPDATE configgen SET config_generation = 4")
            probe._conn.commit()
        finally:
            probe.close()
        second = self._recover()
        self.assertGreaterEqual(self.broker.emergency_stop_calls, 2)  # 新缺口处理
        store = second._open()
        try:
            gaps = [o for o in store.operations(kind="publish")
                    if isinstance(o["payload"].get("unexplained_generations"),
                                  list)]
            self.assertEqual(len(gaps), 2)   # 旧 gap + 新缺口各自成记录
            covered = set()
            for g in gaps:
                covered |= set(g["payload"]["unexplained_generations"])
            self.assertEqual(covered, {2, 4})   # 旧 gap 不掩盖新缺口
        finally:
            store.close()


    def test_applied_generation_reconciles_covered_intent_without_stop(self):
        # §14 缺口 3 路径①：commit_applied 已落库但意图 op 未收尾——恢复
        # 按已应用代次收尾，绝不无故关停已正确应用撤销的分享。
        probe = ShareStore(self.path)
        try:
            created = probe.create_key("friendD")
            probe.delete_key(created["key_id"])
            target = probe.generation()["config_generation"]
            probe.commit_applied(target, security_generation=target)   # 分享已确认应用该撤销
        finally:
            probe.close()
        recovered = self._recover()
        status = recovered.status()
        self.assertEqual(self.broker.emergency_stop_calls, 0)  # 绝不误停
        self.assertTrue(status["in_sync"])
        store = recovered._open()
        try:
            op = next(o for o in store.operations(kind="publish")
                      if o["payload"].get("action_kind") == "delete")
            self.assertEqual(op["status"], "applied")
            self.assertEqual(op["payload"]["recovered"], "superseded_by_applied")
        finally:
            store.close()

    def test_newer_generation_covering_older_revocation_is_not_stopped(self):
        # §14 缺口 3 路径②：并发变更产生 g/g+1 意图，发布 CURRENT 只完成
        # g+1 的记录，g 留 pending——g 已被 applied(g+1) 覆盖，恢复收尾 g
        # 且不调用 stop；仍未被覆盖的撤销继续要求关停。
        probe = ShareStore(self.path)
        try:
            created = probe.create_key("friendE")
            probe.delete_key(created["key_id"])   # g：revoking intent pending
            probe.create_key("friendF")           # g+1：CURRENT
            newer = probe.generation()["config_generation"]
            probe.commit_applied(newer, security_generation=newer)   # g+1 已应用（包含 g 的撤销）
            self.assertEqual(newer, probe.generation()["config_generation"])
        finally:
            probe.close()
        recovered = self._recover()
        status = recovered.status()
        self.assertEqual(self.broker.emergency_stop_calls, 0)  # 绝不误停
        self.assertTrue(status["in_sync"])
        store = recovered._open()
        try:
            ops = {o["id"]: o for o in store.operations(kind="publish")}
            # 全部被 applied(g+1) 覆盖的意图（含旧的 g 撤销）都已收尾。
            for op in ops.values():
                self.assertEqual(op["status"], "applied")
                self.assertEqual(op["payload"]["recovered"],
                                 "superseded_by_applied")
        finally:
            store.close()
        # 尚未被覆盖的撤销继续要求关停：再造一个未应用的撤销意图。
        probe2 = ShareStore(self.path)
        try:
            probe2.rotate_key([k["key_id"] for k in probe2.list_keys()][0])
        finally:
            probe2.close()
        ShareManager._recovered_paths.discard(str(self.path.resolve()))
        re_recovered = ShareManager(store_path=self.path, broker=self.broker)
        self.assertGreaterEqual(self.broker.emergency_stop_calls, 1)


class ShareApiBoundaryTests(unittest.TestCase):
    """Loads usagesrv under a unique module name so it does not disturb the
    existing usage-boundary module's import-time session/token paths."""

    @classmethod
    def setUpClass(cls):
        # Keep this test self-contained even when unittest runs every module in
        # one process: save and restore the three env knobs this class changes.
        cls._saved_env = {}
        for key in ("USAGE_SESSION_CODE_FILE", "USAGE_CONTROL_TOKEN_FILE",
                    "SHARE_STORE_FILE"):
            cls._saved_env[key] = os.environ.get(key)
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix="share-api-")
        os.environ["USAGE_SESSION_CODE_FILE"] = os.path.join(
            cls.tmp.name, "usage-session-code")
        os.environ["USAGE_CONTROL_TOKEN_FILE"] = os.path.join(
            cls.tmp.name, "usage-control-token")
        os.environ["SHARE_STORE_FILE"] = os.path.join(cls.tmp.name, "share.db")
        spec = importlib.util.spec_from_file_location(
            "usagesrv_share_mgmt_test",
            Path(__file__).resolve().parents[1] / "usagesrv.py")
        cls.usagesrv = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.usagesrv
        spec.loader.exec_module(cls.usagesrv)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        for key, old in cls._saved_env.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    def setUp(self):
        # 每个测试一份独立的权威库：彻底隔离上一个测试残留的 ops 行与
        # daemon executor——否则上一测试"撤销失败未确认关停"的状态会被
        # 本测试恢复逻辑当作 intent gap 再驱动一次关停，污染计数断言。
        self.tmp_store = tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix="share-api-store-")
        self.addCleanup(self.tmp_store.cleanup)
        db = Path(self.tmp_store.name) / "share.db"
        os.environ["SHARE_STORE_FILE"] = str(db)
        self.broker = FakeBroker()
        self.usagesrv.get_share_manager = lambda: ShareManager(
            store_path=db, broker=self.broker)
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
        # Reset the lazy manager for the next test so the SQLite store is not
        # cached across class instances with an open/locked file.
        with self.usagesrv._share_manager_lock:
            self.usagesrv._share_manager = None

    def request(self, path, method="GET", token=None, cookie=None, body=None,
                xrw=None, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"Host": "127.0.0.1:8001"}
        if token:
            headers["X-Admin-Control"] = token
        if cookie:
            headers["Cookie"] = cookie
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

    def exchange_session(self):
        code = self.usagesrv.session_bootstrap_code()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"Host": "127.0.0.1:8001", "Content-Type": "application/json"}
        try:
            conn.request("POST", "/api/session", body=json.dumps(
                {"code": code}), headers=headers)
            response = conn.getresponse()
            response.read()
            cookie = response.msg.get("Set-Cookie")
            return cookie.split("usage_session=")[1].split(";")[0]
        finally:
            conn.close()

    def test_api_requires_management_credentials(self):
        status, _ = self.request("/api/share/status")
        self.assertEqual(status, 401)
        status, _ = self.request("/api/share/status", token="wrong")
        self.assertEqual(status, 401)
        status, _ = self.request("/api/share/status", token="test-control-token")
        self.assertEqual(status, 200)

    def test_share_key_cannot_act_as_manager(self):
        # A friend's Bearer key is not an X-Admin-Control credential.
        headers = {"Authorization": "Bearer friend-key",
                   "X-Admin-Control": "wrong"}
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request("GET", "/api/share/status", headers=headers)
            response = conn.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
        finally:
            conn.close()

    def test_create_list_patch_rotate_delete_flow(self):
        status, body = self.request(
            "/api/share/keys", method="POST", token="test-control-token",
            body={"name": "friendA"})
        self.assertEqual(status, 201)
        created = json.loads(body)
        kid = created["key_id"]
        self.assertTrue(created["secret"].startswith("um-share-"))
        self.assertNotIn("secret_hash", body.decode())

        status, body = self.request("/api/share/keys", token="test-control-token")
        self.assertEqual(status, 200)
        keys = json.loads(body)["keys"]
        self.assertEqual([k["name"] for k in keys], ["friendA"])
        self.assertNotIn("secret", keys[0])
        self.assertNotIn("secret_hash", keys[0])

        status, body = self.request(
            f"/api/share/keys/{kid}", method="PATCH",
            token="test-control-token",
            body={"name": "friendA2", "expected_row_version": 1})
        self.assertEqual(status, 200)
        patched = json.loads(body)
        self.assertEqual(patched["name"], "friendA2")
        self.assertTrue(patched["enabled"])
        self.assertEqual(patched["row_version"], 2)

        # D18-3: a revoke tombstones the Key and terminates its in-flight
        # requests in one confirmed step.  It leaves the list and its plaintext
        # answers 410.
        status, body = self.request(
            f"/api/share/keys/{kid}/revoke-and-terminate", method="POST",
            token="test-control-token",
            body={"expected_row_version": 2})
        self.assertEqual(status, 200)
        revoked = json.loads(body)
        self.assertFalse(revoked["enabled"])
        self.assertTrue(revoked["deleted_at"])
        self.assertEqual(revoked["row_version"], 3)
        status, body = self.request("/api/share/keys", token="test-control-token")
        self.assertNotIn(kid, [k["key_id"] for k in json.loads(body)["keys"]])
        status, body = self.request(
            f"/api/share/keys/{kid}/secret", token="test-control-token")
        self.assertEqual(status, 410)
        self.assertEqual(json.loads(body)["error"], "key_deleted")

        # A tombstoned identity cannot be rotated: the request is refused with
        # the same conflict mapping and no new secret version is written.
        status, body = self.request(
            f"/api/share/keys/{kid}/rotate", method="POST",
            token="test-control-token",
            body={"expected_row_version": 3})
        self.assertEqual(status, 409)
        self.assertIn("deleted identity cannot be rotated", body.decode())

        # The DELETE of the tombstoned Key is idempotent: it keeps the same
        # tombstone instead of failing.
        status, body = self.request(
            f"/api/share/keys/{kid}", method="DELETE",
            token="test-control-token", body={"expected_row_version": 3})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["already_deleted"])
        status, body = self.request(f"/api/share/keys/{kid}",
                                    token="test-control-token")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["deleted_at"])

    def test_secret_readback_is_version_bound_and_never_in_the_list(self):
        status, body = self.request(
            "/api/share/keys", method="POST", token="test-control-token",
            body={"name": "secretA"})
        self.assertEqual(status, 201)
        created = json.loads(body)
        kid = created["key_id"]
        secret = created["secret"]
        status, body = self.request(f"/api/share/keys/{kid}",
                                    token="test-control-token")
        self.assertEqual(status, 200)
        version = json.loads(body)["row_version"]
        status, body = self.request(
            f"/api/share/keys/{kid}/secret?expected_row_version={version}",
            token="test-control-token")
        self.assertEqual(status, 200)
        readback = json.loads(body)
        self.assertEqual(readback["secret"], secret)
        self.assertEqual(readback["key_id"], kid)
        # A stale page gets the version-conflict state, never another version's
        # plaintext.
        status, body = self.request(
            f"/api/share/keys/{kid}/secret?expected_row_version={version + 1}",
            token="test-control-token")
        self.assertEqual(status, 200)
        conflict = json.loads(body)
        self.assertIs(conflict["secret_unavailable"], True)
        self.assertEqual(conflict["reason"], "version_conflict")
        self.assertNotIn("secret", conflict)
        # The list never carries plaintext, and the readback requires the
        # management credential.
        status, body = self.request("/api/share/keys", token="test-control-token")
        self.assertEqual(status, 200)
        self.assertNotIn(secret.encode(), body)
        status, _ = self.request(f"/api/share/keys/{kid}/secret")
        self.assertEqual(status, 401)

    def test_test_route_requires_expected_version(self):
        status, body = self.request(
            "/api/share/keys", method="POST", token="test-control-token",
            body={"name": "testA"})
        self.assertEqual(status, 201)
        kid = json.loads(body)["key_id"]
        status, _ = self.request(
            f"/api/share/keys/{kid}/test", method="POST",
            token="test-control-token", body={})
        self.assertEqual(status, 400)
        status, body = self.request(
            f"/api/share/keys/{kid}/test", method="POST",
            token="test-control-token", body={"expected_row_version": 999})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["reason"], "key_changed")
        self.assertIs(result["connected"], False)
        self.assertNotIn("secret", result)

    def test_cli_exposes_revoke_secret_test_and_settings(self):
        import share.admin as share_admin
        calls = []

        def fake_call(base, method, path, token, body=None):
            calls.append((method, path, body))
            return {"ok": True}

        with mock.patch.object(share_admin, "_call", fake_call):
            for argv in (
                ["revoke", "k", "--expected-version", "2"],
                ["secret", "k", "--expected-version", "2"],
                ["test", "k", "--expected-version", "2"],
                ["create", "--name", "friend", "--max-concurrency", "16"],
                ["settings", "--share-max-concurrency", "64",
                 "--expected-share-max", "64"],
                ["limits", "--normalize"],
            ):
                with self.subTest(argv=argv):
                    self.assertEqual(
                        share_admin.main(["--control-token", "t"] + argv), 0)
        self.assertEqual(calls, [
            ("POST", "/api/share/keys/k/revoke-and-terminate",
             {"expected_row_version": 2}),
            ("GET", "/api/share/keys/k/secret?expected_row_version=2", None),
            ("POST", "/api/share/keys/k/test", {"expected_row_version": 2}),
            ("POST", "/api/share/keys",
             {"name": "friend", "max_concurrency": 16}),
            ("PATCH", "/api/share/settings",
             {"share_max_concurrency": 64, "expected_share_max": 64}),
            ("POST", "/api/share/limits/normalize", {}),
        ])

    def test_missing_name_returns_clean_error(self):
        status, body = self.request(
            "/api/share/keys", method="POST", token="test-control-token",
            body={})
        self.assertEqual(status, 400)
        self.assertIn("invalid name", body.decode())

    def test_revoke_fails_closed_when_publication_not_confirmed(self):
        created = json.loads(self.request(
            "/api/share/keys", method="POST", token="test-control-token",
            body={"name": "friendA"})[1])
        # Broker stops confirming; revoking must NOT fake a 200 because the old
        # projection could still authorize the key online.
        self.broker.publish = lambda projection: None
        status, body = self.request(
            f"/api/share/keys/{created['key_id']}/revoke-and-terminate",
            method="POST", token="test-control-token",
            body={"expected_row_version": 1})
        self.assertEqual(status, 503)
        self.assertIn("revoking operation not applied online", body.decode())

    def test_row_version_conflict_returns_409(self):
        created = json.loads(self.request(
            "/api/share/keys", method="POST", token="test-control-token",
            body={"name": "friendA"})[1])
        status, _ = self.request(
            f"/api/share/keys/{created['key_id']}", method="PATCH",
            token="test-control-token", body={"name": "other",
                                              "expected_row_version": 99})
        self.assertEqual(status, 409)
        status, _ = self.request(
            f"/api/share/keys/{created['key_id']}", method="PATCH",
            token="test-control-token", body={"name": "other",
                                              "expected_row_version": 1})
        self.assertEqual(status, 200)

    def test_cookie_write_requires_csrf_marker(self):
        sid = self.exchange_session()
        cookie = "usage_session=" + sid
        status, _ = self.request("/api/share/keys", method="POST",
                                 cookie=cookie, body={"name": "sessionA"})
        self.assertEqual(status, 403)
        status, body = self.request("/api/share/keys", method="POST",
                                    cookie=cookie, xrw="XMLHttpRequest",
                                    body={"name": "sessionA"})
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["name"], "sessionA")

    def test_cross_origin_write_rejected_before_credentials(self):
        status, _ = self.request(
            "/api/share/keys", method="POST", token="test-control-token",
            origin="http://evil.com", body={"name": "x"})
        self.assertEqual(status, 403)

    def test_unknown_share_route_is_generic_404(self):
        status, body = self.request("/api/share/nope", token="test-control-token")
        self.assertEqual(status, 404)
        self.assertNotIn(b"/api/share/nope", body)

    def test_unknown_field_rejected(self):
        status, _ = self.request(
            "/api/share/keys", method="POST", token="test-control-token",
            body={"name": "x", "probe": True})
        self.assertEqual(status, 400)
        self.assertIn("unknown field", self.request(
            "/api/share/keys", method="POST", token="test-control-token",
            body={"name": "x", "probe": True})[1].decode())

    def test_non_json_content_type_rejected(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        body = b'{"name":"x"}'
        try:
            conn.putrequest("POST", "/api/share/keys")
            conn.putheader("Host", "127.0.0.1:8001")
            conn.putheader("X-Admin-Control", "test-control-token")
            conn.putheader("Content-Type", "text/plain")
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
        finally:
            conn.close()

    def test_duplicate_control_header_rejected(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        body = b'{"name":"x"}'
        try:
            conn.putrequest("POST", "/api/share/keys")
            conn.putheader("Host", "127.0.0.1:8001")
            conn.putheader("X-Admin-Control", "test-control-token")
            conn.putheader("X-Admin-Control", "test-control-token")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            self.assertIn(b"duplicate header", response.read())
        finally:
            conn.close()

    def test_duplicate_header_yields_exactly_one_response_on_raw_socket(self):
        # R7-a: the duplicate-header path previously sent the 400 twice on
        # one connection; the raw socket must observe exactly one response.
        body = b'{"name":"x"}'
        request = (b"POST /api/share/keys HTTP/1.1\r\nHost: 127.0.0.1:8001\r\n"
                   b"X-Admin-Control: test-control-token\r\n"
                   b"X-Admin-Control: test-control-token\r\n"
                   b"Content-Type: application/json\r\nContent-Length: "
                   + str(len(body)).encode() + b"\r\n\r\n" + body)
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        try:
            sock.sendall(request)
            sock.shutdown(socket.SHUT_WR)
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        finally:
            sock.close()
        self.assertEqual(data.count(b"HTTP/1.1 "), 1)
        self.assertIn(b"duplicate header", data)

    def test_duplicate_header_rejected_on_read_paths_too(self):
        # R7-a: read and write entry points reject duplicated sensitive
        # headers consistently, with a single 400 response.
        request = (b"GET /api/share/status HTTP/1.1\r\nHost: 127.0.0.1:8001\r\n"
                   b"X-Admin-Control: test-control-token\r\n"
                   b"X-Admin-Control: test-control-token\r\n\r\n")
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        try:
            sock.sendall(request)
            sock.shutdown(socket.SHUT_WR)
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        finally:
            sock.close()
        self.assertEqual(data.count(b"HTTP/1.1 "), 1)
        self.assertIn(b"duplicate header", data)

    def test_stale_ack_fails_revocation_and_triggers_emergency_stop(self):
        # R4-a: a broker acking the PREVIOUS generation must never count as a
        # successful revocation; the restricted emergency stop is driven and
        # the unconfirmed stop keeps the publication blocked.
        created = json.loads(self.request(
            "/api/share/keys", method="POST", token="test-control-token",
            body={"name": "friendS"})[1])
        real_publish = self.broker.publish

        def stale_ack(projection):
            real_publish(projection)
            return {"applied_generation": projection["config_generation"] - 1}

        self.broker.publish = stale_ack
        self.broker.emergency_stop_result = {"state": "stopped"}  # 未确认关停
        status, body = self.request(
            f"/api/share/keys/{created['key_id']}", method="DELETE",
            token="test-control-token", body={"expected_row_version": 1})
        self.assertEqual(status, 503)
        self.assertIn("revoking operation not applied online", body.decode())
        self.assertGreaterEqual(self.broker.emergency_stop_calls, 1)
        status_body = json.loads(self.request(
            "/api/share/status", token="test-control-token")[1])
        self.assertTrue(status_body["blocked"])
        blocker = status_body["blocked_publications"][0]
        # 删除自身又推进一代：撤销发布的 target_generation 是删除后的代次。
        self.assertEqual(blocker["target_generation"],
                         created["desired_generation"] + 1)

    def test_confirmed_emergency_stop_clears_blocked_flag(self):
        created = json.loads(self.request(
            "/api/share/keys", method="POST", token="test-control-token",
            body={"name": "friendT"})[1])
        self.broker.publish = lambda projection: None  # 从不确认
        # broker 关停结果含显式 stopped=True -> 关停被确认 -> 不再 blocked
        status, body = self.request(
            f"/api/share/keys/{created['key_id']}", method="DELETE",
            token="test-control-token", body={"expected_row_version": 1})
        self.assertEqual(status, 503)
        self.assertIn("emergency stop confirmed", body.decode())
        status_body = json.loads(self.request(
            "/api/share/status", token="test-control-token")[1])
        self.assertFalse(status_body["blocked"])
        self.assertTrue(status_body["blocked_publications"][0]["stop_confirmed"])

    def test_api_cross_key_rotation_replay_returns_409(self):
        a = json.loads(self.request(
            "/api/share/keys", method="POST", token="test-control-token",
            body={"name": "apiRA"})[1])
        b = json.loads(self.request(
            "/api/share/keys", method="POST", token="test-control-token",
            body={"name": "apiRB"})[1])
        # S2 fail-closed: the rotate keeps the Key enabled, so the runtime's
        # revoke watermark does not cover its intent.
        status, _ = self.request(
            f"/api/share/keys/{a['key_id']}/rotate", method="POST",
            token="test-control-token",
            body={"expected_row_version": 1, "idempotency_key": "api-rot-k"})
        self.assertEqual(status, 503)
        status, body = self.request(
            f"/api/share/keys/{b['key_id']}/rotate", method="POST",
            token="test-control-token",
            body={"expected_row_version": 1, "idempotency_key": "api-rot-k"})
        self.assertEqual(status, 409)
        self.assertIn("different key", body.decode())
        # B 未被轮换：行版本仍为 1。
        status, body = self.request(f"/api/share/keys/{b['key_id']}",
                                    token="test-control-token")
        self.assertEqual(json.loads(body)["row_version"], 1)
        # The store replay still answers the stored metadata (no new
        # plaintext); the HTTP publication remains fail-closed.
        store = self.usagesrv.get_share_manager()._open()
        try:
            replay = store.rotate_key(
                a["key_id"], idempotency_key="api-rot-k")
        finally:
            store.close()
        self.assertTrue(replay.get("secret_unavailable"))
        self.assertNotIn("secret", replay)

    def test_status_does_not_expose_absolute_store_path(self):
        status, body = self.request("/api/share/status",
                                    token="test-control-token")
        self.assertEqual(status, 200)
        self.assertNotIn(b"store_path_abs", body)

    def test_unexpected_error_sends_only_500(self):
        class Exploding:
            def status(self):
                raise RuntimeError("boom")

        self.usagesrv.get_share_manager = lambda: Exploding()
        status, body = self.request("/api/share/status",
                                    token="test-control-token")
        self.assertEqual(status, 500)
        text = body.decode("utf-8")
        # R7: exactly one response; it must not fall through to the 404 handler.
        self.assertNotIn("unknown path", text)

    def _wait_operation(self, operation_id, expected, timeout=5.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            status, body = self.request(
                f"/api/share/operations/{operation_id}",
                token="test-control-token")
            self.assertEqual(status, 200)
            last = json.loads(body)
            if last["status"] == expected:
                return last
            time.sleep(0.05)
        self.fail(f"operation never reached {expected}: {last}")

    def test_create_key_with_max_concurrency_via_api(self):
        status, body = self.request(
            "/api/share/keys", method="POST", token="test-control-token",
            body={"name": "friendMC", "max_concurrency": 12})
        self.assertEqual(status, 201)
        created = json.loads(body)
        status, body = self.request(f"/api/share/keys/{created['key_id']}",
                                    token="test-control-token")
        self.assertEqual(json.loads(body)["max_concurrency"], 12)

    def test_create_key_invalid_max_concurrency_rejected(self):
        for bad in (0, 49, 65, True, "8"):
            with self.subTest(bad=bad):
                status, _ = self.request(
                    "/api/share/keys", method="POST",
                    token="test-control-token",
                    body={"name": "friendBad", "max_concurrency": bad})
                self.assertEqual(status, 400)

    def test_patch_key_max_concurrency_passthrough(self):
        created = json.loads(self.request(
            "/api/share/keys", method="POST", token="test-control-token",
            body={"name": "friendPatch"})[1])
        status, body = self.request(
            f"/api/share/keys/{created['key_id']}", method="PATCH",
            token="test-control-token",
            body={"max_concurrency": 16, "expected_row_version": 1})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["max_concurrency"], 16)

    def test_patch_settings_updates_share_max(self):
        # 64→24 is a LOWERING: the limit_lower intent is not covered by
        # the runtime's revoke watermark, so the publication fails closed
        # while the desired value is saved.
        status, _ = self.request(
            "/api/share/settings", method="PATCH",
            token="test-control-token", body={"share_max_concurrency": 24})
        self.assertEqual(status, 503)
        status_body = json.loads(self.request(
            "/api/share/status", token="test-control-token")[1])
        self.assertEqual(status_body["share_max_concurrency"], 24)
        from share.store import SCHEMA_VERSION
        self.assertEqual(status_body["schema_version"], SCHEMA_VERSION)

    def test_patch_settings_expected_conflict_returns_409(self):
        self.request("/api/share/settings", method="PATCH",
                     token="test-control-token",
                     body={"share_max_concurrency": 10})
        status, _ = self.request(
            "/api/share/settings", method="PATCH", token="test-control-token",
            body={"share_max_concurrency": 12, "expected_share_max": 64})
        self.assertEqual(status, 409)

    def test_patch_settings_out_of_range_rejected(self):
        for bad in (0, 65, 129, True, "8", 1.5):
            with self.subTest(bad=bad):
                status, body = self.request(
                    "/api/share/settings", method="PATCH",
                    token="test-control-token",
                    body={"share_max_concurrency": bad})
                self.assertEqual(status, 400)
                if bad in (65, 129):
                    self.assertRegex(json.loads(body)["error"],
                                     r"1\.\.(64|128)")

    def test_patch_settings_accepts_global_64_while_keys_stay_48(self):
        status, body = self.request(
            "/api/share/settings", method="PATCH",
            token="test-control-token", body={"share_max_concurrency": 64})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["share_max_concurrency"], 64)
        status, body = self.request(
            "/api/share/keys", method="POST", token="test-control-token",
            body={"name": "httpKeyAt48", "max_concurrency": 48})
        self.assertEqual(status, 201)
        status, _ = self.request(
            "/api/share/keys", method="POST", token="test-control-token",
            body={"name": "httpKeyAt49", "max_concurrency": 49})
        self.assertEqual(status, 400)
        created = json.loads(self.request(
            "/api/share/keys", method="POST", token="test-control-token",
            body={"name": "httpKeyPatch"})[1])
        self.assertEqual(json.loads(self.request(
            f"/api/share/keys/{created['key_id']}",
            token="test-control-token")[1])["max_concurrency"], 16)
        status, _ = self.request(
            f"/api/share/keys/{created['key_id']}", method="PATCH",
            token="test-control-token",
            body={"max_concurrency": 49, "expected_row_version": 1})
        self.assertEqual(status, 400)
        for bad in (65, 128):
            with self.subTest(global_bad=bad):
                status, _ = self.request(
                    "/api/share/settings", method="PATCH",
                    token="test-control-token",
                    body={"share_max_concurrency": bad})
                self.assertEqual(status, 400)

    def test_patch_settings_unknown_field_rejected(self):
        status, _ = self.request(
            "/api/share/settings", method="PATCH", token="test-control-token",
            body={"share_max_concurrency": 8, "probe": 1})
        self.assertEqual(status, 400)

    def test_host_actions_return_operation_id_and_are_queryable(self):
        status, body = self.request("/api/share/start", method="POST",
                                    token="test-control-token", body={})
        self.assertEqual(status, 202)
        op = json.loads(body)
        self.assertTrue(op["operation_id"])
        self.assertEqual(op["status"], "pending")
        final = self._wait_operation(op["operation_id"], "completed")
        self.assertEqual(final["payload"]["broker"]["started"], True)

    def test_host_action_retry_same_idempotency_key_returns_same_operation(self):
        first = self.request("/api/share/emergency-stop", method="POST",
                             token="test-control-token",
                             body={"idempotency_key": "api-idem-1"})
        self.assertEqual(first[0], 202)
        first_op = json.loads(first[1])["operation_id"]
        second = self.request("/api/share/emergency-stop", method="POST",
                              token="test-control-token",
                              body={"idempotency_key": "api-idem-1"})
        self.assertEqual(second[0], 202)
        second_body = json.loads(second[1])
        self.assertEqual(second_body["operation_id"], first_op)
        self.assertTrue(second_body["reused"])
        self._wait_operation(first_op, "completed")
        self.assertEqual(self.broker.emergency_stop_calls, 1)

    def test_cli_is_a_management_service_client(self):
        # R4: share/admin.py must call the HTTP management service, never open
        # SQLite directly. Run it against the live test server; the isolated
        # port is injected via the test-only SHARE_MGMT_ALLOW_PORT env.
        admin = Path(__file__).resolve().parents[1] / "share/admin.py"
        base = f"http://127.0.0.1:{self.port}"
        env = dict(os.environ, SHARE_MGMT_ALLOW_PORT=str(self.port))
        result = subprocess.run(
            [sys.executable, str(admin), "--base-url", base,
             "--control-token", "test-control-token",
             "create", "--name", "cliFriend"],
            capture_output=True, text=True, timeout=20, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        created = json.loads(result.stdout)
        self.assertTrue(created["secret"].startswith("um-share-"))
        self.assertEqual(created["name"], "cliFriend")

    def test_host_action_fails_closed_when_no_broker(self):
        # Replace the per-test FakeBroker with the fail-closed Noop broker.
        self.usagesrv.get_share_manager = lambda: ShareManager(
            store_path=Path(os.environ["SHARE_STORE_FILE"]))
        status, body = self.request("/api/share/start", method="POST",
                                    token="test-control-token", body={})
        # R6-a: 202 + persisted pending op; the executor records the failure.
        self.assertEqual(status, 202)
        op = json.loads(body)
        final = self._wait_operation(op["operation_id"], "failed")
        self.assertIn("no broker installed", final["payload"]["error"])


class ManagementEndpointGuardTests(unittest.TestCase):
    """R4-b: the CLI validates the management endpoint BEFORE any credential
    is read or sent; a mistyped or hostile --base-url must never become a
    management-token exfiltration path and the connection function must never
    be called for rejected endpoints."""

    def test_rejected_endpoints_never_reach_the_connection_function(self):
        import share.admin as share_admin
        calls = []

        class Guard:
            def __init__(self, *args, **kwargs):
                calls.append(args)

        original = share_admin.http.client.HTTPConnection
        share_admin.http.client.HTTPConnection = Guard
        try:
            for banned in ("http://evil.example:8001",
                           "http://127.0.0.1:8001/x",
                           "http://user@127.0.0.1:8001",
                           "http://127.0.0.1",
                           "https://127.0.0.1:8001",
                           "http://localhost:8001",
                           "http://127.0.0.1:8096"):
                with self.assertRaises(SystemExit):
                    share_admin._validate_management_endpoint(banned)
        finally:
            share_admin.http.client.HTTPConnection = original
        self.assertEqual(calls, [])

    def test_valid_loopback_endpoint_is_accepted(self):
        import share.admin as share_admin
        self.assertEqual(
            share_admin._validate_management_endpoint("http://127.0.0.1:8001"),
            ("127.0.0.1", 8001))

    def test_test_only_port_override_is_isolated(self):
        # 注入端口仅用于测试且与生产配置隔离：未设 env 时固定 8001。
        import share.admin as share_admin
        old = os.environ.get("SHARE_MGMT_ALLOW_PORT")
        os.environ["SHARE_MGMT_ALLOW_PORT"] = "18443"
        try:
            self.assertEqual(share_admin._validate_management_endpoint(
                "http://127.0.0.1:18443"), ("127.0.0.1", 18443))
            with self.assertRaises(SystemExit):
                share_admin._validate_management_endpoint("http://127.0.0.1:8001")
        finally:
            if old is None:
                os.environ.pop("SHARE_MGMT_ALLOW_PORT", None)
            else:
                os.environ["SHARE_MGMT_ALLOW_PORT"] = old


class RotationTargetBindingTests(unittest.TestCase):
    """§23 R2-target: a rotation idempotency key is bound to its target
    identity — replaying it for a DIFFERENT key returns 409, the second
    key's secret is unchanged, and retrying the original key replays
    metadata without new plaintext."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.td.cleanup)
        self.broker = FakeBroker()
        self.mgr = ShareManager(store_path=Path(self.td.name) / "share.db",
                                broker=self.broker)

    def test_cross_key_rotation_replay_returns_conflict(self):
        a = self.mgr.create_key("rotA")
        b = self.mgr.create_key("rotB")
        b_before = self.mgr.get_key(b["key_id"])
        # S2 fail-closed: the rotate keeps the Key enabled, so the
        # runtime's revoke watermark does not advance for its intent.
        with self.assertRaises(BrokerError):
            self.mgr.rotate_key(a["key_id"], idempotency_key="rot-k")
        with self.assertRaises(Conflict) as cm:
            self.mgr.rotate_key(b["key_id"], idempotency_key="rot-k")
        self.assertIn("different key", str(cm.exception))
        b_after = self.mgr.get_key(b["key_id"])
        self.assertEqual(b_before["masked_key"], b_after["masked_key"])
        self.assertEqual(b_before["secret_version"], b_after["secret_version"])
        # 同目标合法重试：原操作元数据、无新明文（store 层重放）。
        store = self.mgr._open()
        try:
            replay = store.rotate_key(a["key_id"], idempotency_key="rot-k")
        finally:
            store.close()
        self.assertEqual(replay["key_id"], a["key_id"])
        self.assertTrue(replay.get("secret_unavailable"))
        self.assertNotIn("secret", replay)
        self.assertEqual(replay["secret_version"], 2)


class RevokeAndTerminateTests(unittest.TestCase):
    """R8-A7 撤销并终止：disable + 一次性 terminate_keys 贯通到 broker 发布；
    ACK 计数如实上浮（双形状容差）；普通撤销不带 terminate_keys。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.td.cleanup)
        self.broker = FakeBroker()
        self.mgr = ShareManager(
            store_path=Path(self.td.name) / "share.db",
            broker=self.broker)
        created = self.mgr.create_key("friend-x", max_concurrency=8)
        self.plaintext = created["secret"]   # 一次性明文（纪律断言基准）
        self.key_id = created["key_id"]
        # create_key's response carries no row_version; the CURRENT version
        # comes from get_key (the same value PATCH/DELETE would expect).
        self.row_version = self.mgr.get_key(self.key_id)["row_version"]

    def test_revoke_and_terminate_carries_one_shot_list(self):
        out = self.mgr.revoke_and_terminate_key(
            self.key_id, expected_row_version=self.row_version)
        self.assertEqual(self.broker.terminate_keys_seen, [[self.key_id]])
        # D18-3: every count travels through the revoke answer; a missing
        # field stays None instead of being folded into another counter.
        self.assertEqual(out["terminated"], 2)
        self.assertEqual(out["released"], 3)
        self.assertEqual(out["aborted"], 4)
        self.assertEqual(out["upstream_closed"], 5)
        self.assertFalse(out["enabled"])
        # 撤销 = 墓碑：列表不再返回该行，明文回查被拒。
        self.assertTrue(out["deleted_at"])
        self.assertFalse(out["plaintext_viewable"])
        self.assertNotIn(self.key_id,
                         [k["key_id"] for k in self.mgr.list_keys()["keys"]])
        with self.assertRaises(Disabled):
            self.mgr.reveal_key_secret(self.key_id)
        # 一次性明文绝不出现在撤销响应（字段与值双侧：secret_version/
        # masked_key 含 "secret" 子串属正常字段名，不构成泄漏）。
        self.assertNotIn(self.plaintext, json.dumps(out))
        self.assertNotIn('"secret"', json.dumps(out))

    def test_normal_revoke_does_not_terminate(self):
        self.mgr.delete_key(self.key_id,
                            expected_row_version=self.row_version)
        self.assertEqual(self.broker.terminate_keys_seen, [])

    def test_version_conflict_refuses(self):
        with self.assertRaises(VersionMismatch):
            self.mgr.revoke_and_terminate_key(
                self.key_id, expected_row_version=self.row_version + 5)

    def test_reply_shapes_both_tolerated(self):
        # 双形状容差：dict（adapter）与 BrokerResult（raw client）都读得到
        # terminated；缺失字段是 None 不是 0。
        from share.management import _reply_count, _reply_terminated
        self.assertEqual(_reply_terminated({"terminated": 3}), 3)
        from share.broker_protocol import BrokerResult
        self.assertEqual(_reply_terminated(
            BrokerResult.ok({"terminated": 2})), 2)
        self.assertIsNone(_reply_terminated({"applied_generation": 3}))
        self.assertIsNone(_reply_terminated(BrokerResult.ok({})))
        # D18-3: the same reader handles every count; a bool is not an int.
        self.assertEqual(_reply_count({"released": 4}, "released"), 4)
        self.assertEqual(_reply_count(
            BrokerResult.ok({"upstream_closed": 5}), "upstream_closed"), 5)
        self.assertIsNone(_reply_count({"released": True}, "released"))
        self.assertIsNone(_reply_count({"released": None}, "released"))

    def test_revoke_without_instance_counts_stays_unknown(self):
        # An older runtime ACK has no released/aborted/upstream_closed fields:
        # the revoke answer reports None for each instead of inventing 0.
        class _NoCountBroker(FakeBroker):
            def publish(self, projection, terminate_keys=None):
                out = super().publish(
                    projection, terminate_keys=terminate_keys)
                for name in ("released", "aborted", "upstream_closed"):
                    out.pop(name, None)
                return out

        self.mgr.broker.__class__ = _NoCountBroker
        out = self.mgr.revoke_and_terminate_key(
            self.key_id, expected_row_version=self.row_version)
        self.assertEqual(out["terminated"], 2)
        self.assertIsNone(out["released"])
        self.assertIsNone(out["aborted"])
        self.assertIsNone(out["upstream_closed"])

    def test_stale_termination_leaves_key_disabled(self):
        # broker 发布失败（unreachable）：撤销向 fail-closed 抛 BrokerError；
        # store 内 Key 已墓碑化（拒新与清密文已生效），终止待重试——如实语义。
        class _DeadBroker(FakeBroker):
            def publish(self, projection, terminate_keys=None):
                from share.management import BrokerError
                raise BrokerError("unconfirmed: instance load failed")

        self.mgr.broker = _DeadBroker()
        from share.management import BrokerError
        with self.assertRaises(BrokerError):
            self.mgr.revoke_and_terminate_key(
                self.key_id, expected_row_version=self.row_version)
        row = self.mgr.get_key(self.key_id)
        self.assertFalse(row["enabled"])
        self.assertTrue(row["deleted_at"])
        self.assertFalse(row["plaintext_viewable"])
        self.assertNotIn(self.key_id,
                         [k["key_id"] for k in self.mgr.list_keys()["keys"]])
        with self.assertRaises(Disabled):
            self.mgr.reveal_key_secret(self.key_id)


class _ControlRecordingTransport:
    """One-exchange NamedPipeBrokerTransport stand-in: records the decoded
    signed request and the exchange deadline, replays one fixed response."""

    def __init__(self, response=None):
        self.response = response or {"ok": True, "error_code": "ok",
                                    "result": {}}
        self.request = None
        self.deadline = None
        self.closed = False

    def open(self):
        return self

    def close(self):
        self.closed = True

    def send_all(self, data, deadline=None):
        self.deadline = deadline
        self.request = broker_protocol.decode_frame(data)

    def recv_line(self, deadline):
        return broker_protocol.encode_frame(dict(
            self.response, request_id=self.request["request_id"]))


class BrokerClientAdapterControlVerbTests(unittest.TestCase):
    """Rev9 A9-1..A9-4: the adapter's six console control verbs are thin
    wrappers over the fixed BrokerClient methods — the frozen wire action and
    fields, the BOUNDED exchange deadline and the structured BrokerResult
    (error_code included) pass through UNCHANGED for the console mapping."""

    def _adapter(self, response=None):
        transport = _ControlRecordingTransport(response)
        adapter = BrokerClientAdapter(BrokerClient(lambda: transport,
                                                 "secret"))
        return adapter, transport

    def test_verbs_map_to_the_frozen_wire_fields(self):
        for method, args, action, fields in (
                ("maintenance_begin", ("share_gateway", 300),
                 "maintenance-begin",
                 {"component": "share_gateway", "lease_s": 300}),
                ("maintenance_end", ("share_gateway", "a" * 32),
                 "maintenance-end",
                 {"component": "share_gateway", "lease_id": "a" * 32}),
                ("maintenance_status", (), "maintenance-status", {}),
                ("gateway_restart", ("share_gateway", 60),
                 "gateway-restart",
                 {"component": "share_gateway", "budget_s": 60}),
                ("host_evidence", ("limits",), "host-evidence",
                 {"check": "limits"}),
                ("ts_service_control", ("start",), "ts-service-control",
                 {"action": "start"})):
            with self.subTest(method=method):
                adapter, transport = self._adapter()
                result = getattr(adapter, method)(*args)
                self.assertTrue(result.ok)
                self.assertEqual(transport.request["action"], action)
                self.assertEqual(transport.request["fields"], fields)

    def test_start_forwards_explicit_to_the_wire(self):
        adapter, transport = self._adapter({
            "ok": True, "error_code": "ok",
            "result": {"started": True}})
        result = adapter.start(explicit=True)
        self.assertTrue(result["started"])
        self.assertEqual(transport.request["action"], "start")
        self.assertEqual(transport.request["fields"], {"explicit": True})

    def test_every_control_exchange_is_bounded(self):
        # The frozen 1..180 protocol bound: no control verb may exceed it.
        for method, args in (("maintenance_begin", ("share_gateway", 300)),
                             ("maintenance_end", ("share_gateway", "a" * 32)),
                             ("maintenance_status", ()),
                             ("gateway_restart", ("share_gateway", 60)),
                             ("host_evidence", ("limits",)),
                             ("ts_service_control", ("stop",))):
            with self.subTest(method=method):
                adapter, transport = self._adapter()
                before = time.monotonic()
                getattr(adapter, method)(*args)
                window = transport.deadline - before
                self.assertGreater(window, 0)
                self.assertLessEqual(window, 180.001)

    def test_gateway_restart_deadline_is_bounded(self):
        adapter, transport = self._adapter()
        before = time.monotonic()
        self.assertTrue(adapter.gateway_restart("share_gateway", 60).ok)
        window = transport.deadline - before
        self.assertGreater(window, 60)
        self.assertLessEqual(window, 76)
        # The frozen 1..180 bound: even budget_s=180 never exceeds 180 s.
        adapter2, transport2 = self._adapter()
        before2 = time.monotonic()
        self.assertTrue(adapter2.gateway_restart("share_gateway", 180).ok)
        self.assertLessEqual(transport2.deadline - before2, 180.001)

    def test_structured_error_code_passes_through_unchanged(self):
        # The console maps these codes (bad_request -> extension missing,
        # capability_missing -> capability missing, blocked/unconfirmed ->
        # maintenance refused): folding them into a message would break it.
        for code in ("bad_request", "capability_missing", "blocked",
                     "unconfirmed", "not_installed", "deadline_exceeded"):
            with self.subTest(code=code):
                adapter, _ = self._adapter({
                    "ok": False, "error_code": code,
                    "result": {"detail": "refused " + code}})
                result = adapter.maintenance_begin("share_gateway", 300)
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, code)
                self.assertEqual(result.result["detail"],
                                 "refused " + code)

    def test_ts_control_result_passes_through_unchanged(self):
        # The console maps the TS control codes too (not_installed ->
        # broker unwired, bad_request -> extension missing, unconfirmed with
        # the re-confirmation detail -> pending confirmation): the adapter must
        # keep the BrokerResult (and its structured error_code) untouched.
        for code in ("bad_request", "unconfirmed", "not_installed",
                     "deadline_exceeded"):
            with self.subTest(code=code):
                adapter, _ = self._adapter({
                    "ok": False, "error_code": code,
                    "result": {"detail": "refused " + code}})
                result = adapter.ts_service_control("stop")
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, code)
                self.assertEqual(result.result["detail"],
                                 "refused " + code)

    def test_ts_control_ok_result_passes_through_unchanged(self):
        adapter, transport = self._adapter({
            "ok": True, "error_code": "ok",
            "result": {"ts_service_started": True}})
        result = adapter.ts_service_control("start")
        self.assertTrue(result.ok)
        self.assertEqual(result.result, {"ts_service_started": True})
        self.assertEqual(transport.request["action"], "ts-service-control")
        self.assertEqual(transport.request["fields"], {"action": "start"})

    def test_unreachable_pipe_is_not_installed(self):
        class _AbsentPipe:
            def __call__(self):
                raise OSError("pipe missing")

        adapter = BrokerClientAdapter(BrokerClient(_AbsentPipe(), "secret"))
        result = adapter.maintenance_status()
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "not_installed")

    def test_noop_broker_control_verbs_are_unwired(self):
        broker = NoopBroker()
        for method, args in (("maintenance_begin", ("share_gateway", 300)),
                             ("maintenance_end", ("share_gateway", "a" * 32)),
                             ("maintenance_status", ()),
                             ("gateway_restart", ("share_gateway", 60)),
                             ("host_evidence", ("limits",)),
                             ("ts_service_control", ("start",))):
            with self.subTest(method=method):
                with self.assertRaises(BrokerError):
                    getattr(broker, method)(*args)


if __name__ == "__main__":
    unittest.main()
