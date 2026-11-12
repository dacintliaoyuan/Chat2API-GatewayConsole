"""share_store tests: key lifecycle, generations, idempotency, migration."""
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from share.store import (Conflict, Disabled, MigrationConflict, SCHEMA_VERSION,
                         ShareStore, StoreError, VersionMismatch,
                         load_digest_config, read_key_labels)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = ShareStore(Path(self.td.name) / "share.db")
        self.addCleanup(self._clean)

    def _clean(self):
        try:
            self.store.close()
        finally:
            self.td.cleanup()

    def generation(self):
        return self.store.generation()["config_generation"]


class LifecycleTests(StoreCase):
    def test_create_project_ack_revoke_reject(self):
        c = self.store.create_key("friendA")
        self.assertTrue(c["secret"].startswith("um-share-"))
        proj1 = self.store.render_projection()
        self.assertEqual(len(proj1["data"]), 1)
        self.assertTrue(self.store.verify_secret(c["key_id"], c["secret"]))
        self.store.commit_applied(proj1["config_generation"])
        self.assertTrue(self.store.generation()["in_sync"])
        self.store.delete_key(c["key_id"])
        proj2 = self.store.render_projection()
        self.assertEqual(len(proj2["data"]), 0)
        self.assertEqual(proj2["config_generation"], proj1["config_generation"] + 1)
        self.assertFalse(self.store.verify_secret(c["key_id"], c["secret"]))
        self.assertFalse(self.store.generation()["in_sync"])

    def test_idempotent_create_returns_no_secret_on_replay(self):
        first = self.store.create_key("A", idempotency_key="idem-1")
        gen = self.generation()
        second = self.store.create_key("A2", idempotency_key="idem-1")
        self.assertEqual(first["key_id"], second["key_id"])
        self.assertTrue(second.get("secret_unavailable"))
        self.assertNotIn("secret", second)
        # A clean replay must not create another identity or bump a generation.
        self.assertEqual(self.generation(), gen)
        self.assertEqual(len(self.store.list_keys()), 1)

    def test_idempotency_key_cannot_cross_kind(self):
        c = self.store.create_key("A", idempotency_key="idem-x")
        with self.assertRaises(Conflict):
            self.store.rotate_key(c["key_id"], idempotency_key="idem-x")
        self.assertTrue(self.store.verify_secret(c["key_id"], c["secret"]))
        self.assertEqual(self.store.get_key(c["key_id"])["secret_version"], 1)

    def test_rotate_changes_hash_and_keeps_identity(self):
        c = self.store.create_key("B")
        rot = self.store.rotate_key(c["key_id"])
        self.assertEqual(rot["key_id"], c["key_id"])
        self.assertNotEqual(rot["secret"], c["secret"])
        self.assertFalse(self.store.verify_secret(c["key_id"], c["secret"]))
        self.assertTrue(self.store.verify_secret(c["key_id"], rot["secret"]))
        self.assertEqual(self.store.get_key(c["key_id"])["secret_version"], 2)

    def test_rotate_replay_returns_no_secret(self):
        c = self.store.create_key("B")
        first = self.store.rotate_key(c["key_id"], idempotency_key="rot-1")
        second = self.store.rotate_key(c["key_id"], idempotency_key="rot-1")
        self.assertEqual(second["secret_version"], first["secret_version"])
        self.assertTrue(second.get("secret_unavailable"))
        self.assertNotIn("secret", second)
        self.assertEqual(self.store.get_key(c["key_id"])["secret_version"], 2)
        # The rotated secret is the one that remains valid.
        self.assertFalse(self.store.verify_secret(c["key_id"], c["secret"]))
        self.assertTrue(self.store.verify_secret(c["key_id"], first["secret"]))

    def test_rotate_idempotency_key_is_bound_to_target_key(self):
        # R2-target: rotate(A, k) then rotate(B, k) must return 409 — B's
        # secret is UNCHANGED and no second rotation happened; retrying A
        # with k still replays A's original metadata without new plaintext.
        a = self.store.create_key("RA")
        b = self.store.create_key("RB")
        a_digest = self.store.get_key(a["key_id"])["masked_key"]
        a_version = self.store.get_key(a["key_id"])["secret_version"]
        first = self.store.rotate_key(a["key_id"], idempotency_key="rot-k")
        # 跨目标：B 的请求返回 409，B 的摘要/版本未变。
        b_before = self.store.get_key(b["key_id"])
        with self.assertRaises(Conflict):
            self.store.rotate_key(b["key_id"], idempotency_key="rot-k")
        b_after = self.store.get_key(b["key_id"])
        self.assertEqual(b_before["masked_key"], b_after["masked_key"])
        self.assertEqual(b_before["secret_version"], b_after["secret_version"])
        self.assertEqual(b_before["row_version"], b_after["row_version"])
        # 同目标合法重试：仍返回 A 的原操作元数据，无新明文。
        replay = self.store.rotate_key(a["key_id"], idempotency_key="rot-k")
        self.assertEqual(replay["key_id"], a["key_id"])
        self.assertTrue(replay.get("secret_unavailable"))
        self.assertNotIn("secret", replay)
        self.assertEqual(replay["secret_version"], first["secret_version"])
        self.assertEqual(self.store.get_key(a["key_id"])["secret_version"],
                         a_version + 1)

    def test_disable_is_terminal_and_delete_tombstone(self):
        c = self.store.create_key("C")
        gen1 = self.generation()
        # A revoke is a tombstone: the identity leaves the projection and
        # its plaintext is cleared, exactly like a delete.
        revoked = self.store.update_key(c["key_id"], enabled=False)
        self.assertTrue(revoked["deleted_at"])
        self.assertFalse(revoked["plaintext_viewable"])
        self.assertFalse(self.store.verify_secret(c["key_id"], c["secret"]))
        self.assertEqual(self.store.render_projection()["data"], [])
        # D18-3: a disabled identity can never be re-enabled.
        with self.assertRaises(StoreError):
            self.store.update_key(c["key_id"], enabled=True)
        self.store.delete_key(c["key_id"])
        with self.assertRaises(Disabled):
            self.store.update_key(c["key_id"], enabled=False)
        with self.assertRaises(Disabled):
            self.store.rename_key(c["key_id"], "C2")
        self.assertNotIn(c["key_id"], [k["key_id"] for k in self.store.list_keys()])
        self.assertIn(c["key_id"],
                      [k["key_id"] for k in self.store.list_keys(include_deleted=True)])
        self.assertGreater(self.generation(), gen1)

    def test_revoke_writes_tombstone_and_hides_identity(self):
        # 撤销 = 墓碑 + 清密文：与 delete_key 同一形态，列表默认只留
        # enabled=1 且 deleted_at IS NULL 的活跃行，名字仍可归因。
        c = self.store.create_key("R")
        revoked = self.store.update_key(c["key_id"], enabled=False)
        self.assertFalse(revoked["enabled"])
        self.assertTrue(revoked["deleted_at"])
        self.assertFalse(revoked["plaintext_viewable"])
        self.assertNotIn(c["key_id"],
                         [k["key_id"] for k in self.store.list_keys()])
        self.assertIn(c["key_id"],
                      [k["key_id"] for k in self.store.list_keys(
                          include_deleted=True)])
        with self.assertRaises(Disabled):
            self.store.reveal_key_secret(c["key_id"])
        labels = read_key_labels(self.store.path, [c["key_id"]])
        self.assertEqual(labels[c["key_id"]]["name"], "R")
        self.assertIsNotNone(labels[c["key_id"]]["deleted_at"])
        self.assertTrue(labels[c["key_id"]]["deleted"])
        conn = sqlite3.connect(str(self.store.path))
        try:
            blob, algo = conn.execute(
                "SELECT secret_blob, secret_algo FROM keys WHERE key_id=?",
                (c["key_id"],)).fetchone()
        finally:
            conn.close()
        self.assertIsNone(blob)
        self.assertIsNone(algo)
        intents = [o for o in self.store.operations(kind="publish")
                   if o["payload"].get("target_generation")
                   == self.generation()]
        self.assertEqual(len(intents), 1)
        self.assertTrue(intents[0]["payload"]["revoking"])

    def test_legacy_disabled_row_is_filtered_without_clearing_ciphertext(self):
        # 存量 enabled=0 行按已撤销处理：只从默认列表过滤，不清它的密文；
        # 显式撤销它时才写 deleted_at 并清密文。
        c = self.store.create_key("legacy")
        conn = sqlite3.connect(str(self.store.path))
        try:
            conn.execute("UPDATE keys SET enabled=0 WHERE key_id=?",
                         (c["key_id"],))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.store.list_keys(), [])
        rows = self.store.list_keys(include_deleted=True)
        self.assertEqual([r["key_id"] for r in rows], [c["key_id"]])
        row = self.store.get_key(c["key_id"])
        self.assertIsNone(row["deleted_at"])
        self.assertTrue(row["plaintext_viewable"])
        self.assertEqual(self.store.reveal_key_secret(c["key_id"])["secret"],
                         c["secret"])
        revoked = self.store.update_key(c["key_id"], enabled=False)
        self.assertTrue(revoked["deleted_at"])
        self.assertFalse(revoked["plaintext_viewable"])
        with self.assertRaises(Disabled):
            self.store.reveal_key_secret(c["key_id"])

    def test_security_fence_replayed_revoke_only_disables_and_keeps_ciphertext(self):
        # 恢复重放的是历史撤销意图（旧语义只置 enabled=0），迁移时不清密文：
        # 只禁用，deleted_at 保持空；重放 rotate 同样只禁用。
        revoked = self.store.create_key("fence-revoke")
        rotated = self.store.create_key("fence-rotate")
        deleted = self.store.create_key("fence-delete")
        generation = self.store.generation()["config_generation"] + 1
        state = {
            "format": "um-share-security-state-v1",
            "generation_floor": generation,
            "records": [
                {"record_id": "revoke-record", "key_id": revoked["key_id"],
                 "generation": generation, "action": "revoke",
                 "secret_version": 1, "status": "pending",
                 "recorded_at": "2026-11-11T00:00:00Z"},
                {"record_id": "rotate-record", "key_id": rotated["key_id"],
                 "generation": generation, "action": "rotate",
                 "secret_version": 2, "status": "pending",
                 "recorded_at": "2026-11-11T00:00:01Z"},
                {"record_id": "delete-record", "key_id": deleted["key_id"],
                 "generation": generation, "action": "delete",
                 "secret_version": 1, "status": "pending",
                 "recorded_at": "2026-11-11T00:00:02Z"},
            ],
        }
        fence = self.store.apply_security_fence(state)
        self.assertIn(revoked["key_id"], fence["disabled_keys"])
        self.assertIn(rotated["key_id"], fence["disabled_keys"])
        self.assertIn(deleted["key_id"], fence["disabled_keys"])
        revoked_row = self.store.get_key(revoked["key_id"])
        self.assertIsNone(revoked_row["deleted_at"])
        self.assertFalse(revoked_row["enabled"])
        self.assertTrue(revoked_row["plaintext_viewable"])
        self.assertEqual(
            self.store.reveal_key_secret(revoked["key_id"])["secret"],
            revoked["secret"])
        rotated_row = self.store.get_key(rotated["key_id"])
        self.assertIsNone(rotated_row["deleted_at"])
        self.assertFalse(rotated_row["enabled"])
        self.assertTrue(rotated_row["plaintext_viewable"])
        self.assertEqual(
            self.store.reveal_key_secret(rotated["key_id"])["secret"],
            rotated["secret"])
        # 旧语义的 delete 记录同样只置 enabled=0：迁移不清密文、不写
        # deleted_at，列表过滤掉它即算已撤销。
        deleted_row = self.store.get_key(deleted["key_id"])
        self.assertIsNone(deleted_row["deleted_at"])
        self.assertFalse(deleted_row["enabled"])
        self.assertTrue(deleted_row["plaintext_viewable"])
        self.assertEqual(
            self.store.reveal_key_secret(deleted["key_id"])["secret"],
            deleted["secret"])

    def test_legacy_disabled_row_labels_as_deleted(self):
        # 存量 enabled=0 行（旧撤销语义、没有 deleted_at）同样按已撤销归因：
        # 请求明细要显示「（已删除）」后缀，不能只看 deleted_at。
        c = self.store.create_key("legacy-label")
        conn = sqlite3.connect(str(self.store.path))
        try:
            conn.execute("UPDATE keys SET enabled=0 WHERE key_id=?",
                         (c["key_id"],))
            conn.commit()
        finally:
            conn.close()
        labels = read_key_labels(self.store.path, [c["key_id"]])
        self.assertIsNone(labels[c["key_id"]]["deleted_at"])
        self.assertTrue(labels[c["key_id"]]["deleted"])

    def test_delete_is_idempotent(self):
        c = self.store.create_key("D")
        self.store.commit_applied(self.store.render_projection()["config_generation"])
        first = self.store.delete_key(c["key_id"])
        gen = self.generation()
        second = self.store.delete_key(c["key_id"])
        self.assertTrue(first.get("deleted_at"))
        self.assertTrue(second.get("already_deleted"))
        self.assertEqual(self.generation(), gen)

    def test_update_key_atomic_rename_and_disable(self):
        c = self.store.create_key("A")
        updated = self.store.update_key(c["key_id"], new_name="A2", enabled=False)
        self.assertEqual(updated["name"], "A2")
        self.assertFalse(updated["enabled"])
        self.assertFalse(self.store.verify_secret(c["key_id"], c["secret"]))
        with self.assertRaises(StoreError):
            self.store.update_key(c["key_id"], enabled=True)

    def test_rename_duplicate_conflict(self):
        a = self.store.create_key("A")
        self.store.create_key("B")
        with self.assertRaises(Conflict):
            self.store.rename_key(a["key_id"], "B")

    def test_row_version_optimistic_concurrency(self):
        c = self.store.create_key("V")
        self.assertEqual(self.store.get_key(c["key_id"])["row_version"], 1)
        with self.assertRaises(VersionMismatch):
            self.store.update_key(c["key_id"], enabled=False,
                                  expected_row_version=9)
        # The rotate/delete version gates are checked on a still-live row;
        # the revoke below tombstones it.
        with self.assertRaises(VersionMismatch):
            self.store.rotate_key(c["key_id"], expected_row_version=9)
        with self.assertRaises(VersionMismatch):
            self.store.delete_key(c["key_id"], expected_row_version=9)
        updated = self.store.update_key(c["key_id"], new_name="V2",
                                        enabled=False, expected_row_version=1)
        self.assertEqual(updated["row_version"], 2)
        self.assertTrue(updated["deleted_at"])
        with self.assertRaises(Disabled):
            self.store.update_key(c["key_id"], new_name="V3",
                                  expected_row_version=1)
        with self.assertRaises(Disabled):
            self.store.rotate_key(c["key_id"], expected_row_version=1)
        with self.assertRaises(VersionMismatch):
            self.store.delete_key(c["key_id"], expected_row_version=1)

    def test_rename_conflict_rolls_back_generation(self):
        a = self.store.create_key("A")
        self.store.create_key("B")
        gen = self.generation()
        with self.assertRaises(Conflict):
            self.store.rename_key(a["key_id"], "B")
        self.assertEqual(self.generation(), gen)
        self.assertEqual(self.store.get_key(a["key_id"])["name"], "A")


class MigrationTests(StoreCase):
    def test_source_runcopy_conflict_refused(self):
        with self.assertRaises(MigrationConflict):
            self.store.migration_preview({"friend1": "a" * 64},
                                         {"friend1": "b" * 64})
        # A run-copy identity absent from the source (e.g. friend2) is not revived.
        with self.assertRaises(MigrationConflict):
            self.store.migration_preview({"friend1": "a" * 64},
                                         {"friend1": "a" * 64, "friend2": "c" * 64})

    def test_apply_is_idempotent_and_stable(self):
        src = {"friend1": "a" * 64, "friend3": "d" * 64}
        mapping1, idem = self.store.migrate_apply(src)
        mapping2, idem2 = self.store.migrate_apply(src, idem_map=idem)
        self.assertEqual(mapping1, mapping2)
        rows = {r["name"]: r["key_id"] for r in self.store.list_keys()}
        self.assertEqual(rows["friend1"], mapping1["friend1"])
        self.assertEqual(rows["friend3"], mapping1["friend3"])

    def test_apply_same_source_on_reopened_store_is_stable(self):
        path = Path(self.td.name) / "share.db"
        src = {"friend1": "a" * 64, "friend3": "d" * 64}
        mapping1, _ = self.store.migrate_apply(src)
        self.store.close()
        store2 = ShareStore(path)
        try:
            mapping2, _ = store2.migrate_apply(src)
            self.assertEqual(mapping1, mapping2)
        finally:
            store2.close()

    def test_apply_detects_existing_mismatch(self):
        src = {"friend1": "a" * 64}
        self.store.migrate_apply(src)
        with self.assertRaises(MigrationConflict):
            self.store.migrate_apply({"friend1": "b" * 64})

    def test_apply_rolls_back_on_late_conflict(self):
        # new1 is inserted first, then friend2 conflicts. The whole migration
        # transaction must roll back, not leave a partial apply.
        self.store.migrate_apply({"friend2": "b" * 64})
        gen = self.generation()
        with self.assertRaises(MigrationConflict):
            self.store.migrate_apply(
                {"new1": "c" * 64, "friend2": "d" * 64})
        self.assertEqual(self.generation(), gen)
        self.assertNotIn("new1", [r["name"] for r in self.store.list_keys()])

    def test_migrate_intent_failure_rolls_back_rows_and_generation(self):
        # R4-a.1（§14 缺口 2）：迁移行、代次与 publish intent 共享同一事务
        # 所有者——写 intent 注入失败时，迁移行与代次全部回滚，重新打开库
        # 两者均未改变；成功时三者一起存在。
        original = self.store._record_publish_intent_conn

        def _explode(**_kwargs):
            raise StoreError("injected intent failure")

        self.store._record_publish_intent_conn = _explode
        try:
            with self.assertRaises(StoreError):
                self.store.migrate_apply({"friend1": "a" * 64})
        finally:
            self.store._record_publish_intent_conn = original
        # 重新打开库：迁移行与代次均未改变。
        reopened = ShareStore(self.store.path)
        try:
            self.assertEqual(reopened.list_keys(), [])
            gen = reopened.generation()
            self.assertEqual(gen["config_generation"], 1)
            self.assertEqual(gen["applied_generation"], 0)
            self.assertEqual(reopened.operations(kind="publish"), [])
        finally:
            reopened.close()

    def test_migrate_success_persists_rows_generation_and_intent(self):
        # 成功路径：迁移行、代次与意图三者一起存在（同一事务提交）。
        applied, _ = self.store.migrate_apply({"friend1": "a" * 64})
        rows = self.store.list_keys()
        self.assertEqual([r["name"] for r in rows], ["friend1"])
        self.assertEqual(rows[0]["key_id"], applied["friend1"])
        intents = [o for o in self.store.operations(kind="publish")
                   if o["payload"].get("action_kind") == "migrate"]
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["status"], "pending")
        self.assertEqual(intents[0]["payload"]["target_generation"],
                         self.generation())
        self.assertTrue(
            self.store.generation()["config_generation"] > 1)


class SchemaV2Tests(StoreCase):
    """R6-A2 schema v2: per-key max_concurrency column, global
    share_max_concurrency meta row, the ONE v1->v2 upgrade path, and the v2
    projection shape."""

    def test_fresh_store_is_v2_with_new_deployment_share_max(self):
        # 新部署总上限 64（R6-A2）；存量库迁移才写旧全局值 2。
        # R11-B1: the DB/meta marker advances with SCHEMA_VERSION (3) while
        # the SHARE projection stays PROJECTION_SCHEMA_VERSION (2).
        self.assertEqual(self.store.generation()["schema_version"],
                         SCHEMA_VERSION)
        self.assertEqual(self.store.get_share_max_concurrency(), 64)
        meta = {r["key"]: r["value"] for r in self.store._conn.execute(
            "SELECT key, value FROM meta")}
        self.assertEqual(int(meta["share_max_concurrency"]), 64)
        self.assertEqual(int(meta["schema_version"]), SCHEMA_VERSION)

    def test_create_key_stores_and_reports_max_concurrency(self):
        c = self.store.create_key("friendA", max_concurrency=8)
        row = self.store.get_key(c["key_id"])
        self.assertEqual(row["max_concurrency"], 8)
        # D18-4: the store-level default is now 16 (the management layer
        # passes the same default explicitly).
        d = self.store.create_key("friendB")
        self.assertEqual(self.store.get_key(d["key_id"])["max_concurrency"], 16)
        for bad in (0, 49, 65, True, "8"):
            with self.subTest(bad=bad):
                with self.assertRaises(StoreError):
                    self.store.create_key("x", max_concurrency=bad)

    def test_projection_v2_shape(self):
        c = self.store.create_key("friendP", max_concurrency=5)
        proj = self.store.render_projection()
        self.assertEqual(proj["schema_version"], 2)
        self.assertIn("share_max_concurrency", proj)
        self.assertEqual(proj["data"][0]["max_concurrency"], 5)
        self.assertIn("max_concurrency", proj["data"][0])
        self.assertEqual(proj["data"][0]["key_id"], c["key_id"])

    def test_update_key_lowering_records_revoking_intent(self):
        c = self.store.create_key("friendL", max_concurrency=8)
        gen = self.generation()
        self.store.commit_applied(gen)
        self.store.update_key(c["key_id"], max_concurrency=2)  # 调低
        intents = [o for o in self.store.operations(kind="publish")
                   if o["status"] == "pending"
                   and o["payload"].get("target_generation") == self.generation()]
        self.assertEqual(len(intents), 1)
        self.assertTrue(intents[0]["payload"]["revoking"])
        self.assertEqual(self.generation(), gen + 1)
        self.store.commit_applied(gen + 1)
        self.store.update_key(c["key_id"], max_concurrency=4)  # 调高
        intents = [o for o in self.store.operations(kind="publish")
                   if o["status"] == "pending"
                   and o["payload"].get("target_generation") == self.generation()]
        self.assertEqual(len(intents), 1)
        self.assertFalse(intents[0]["payload"]["revoking"])

    def test_update_key_noop_does_not_bump_or_record(self):
        c = self.store.create_key("friendN", max_concurrency=3)
        gen = self.generation()
        self.store.commit_applied(gen)
        before = [o["id"] for o in
                  self.store.operations(kind="publish", status="pending")]
        self.store.update_key(c["key_id"], max_concurrency=3)
        self.assertEqual(self.generation(), gen)
        after = [o["id"] for o in
                 self.store.operations(kind="publish", status="pending")]
        self.assertEqual(after, before)   # 无新意图（create 意图保持原样）

    def test_share_max_set_read_validate(self):
        self.assertEqual(self.store.set_share_max_concurrency(16)
                         ["share_max_concurrency"], 16)
        self.assertEqual(self.store.get_share_max_concurrency(), 16)
        # Same-value write is a no-op (no bump, no intent).
        gen = self.generation()
        result = self.store.set_share_max_concurrency(16)
        self.assertFalse(result["changed"])
        self.assertEqual(self.generation(), gen)
        # D18-4: the GLOBAL bound is 1..64; 65 and 129 are both invalid.
        for bad in (0, 65, 129, True, "16", 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(StoreError):
                    self.store.set_share_max_concurrency(bad)

    def test_global_accepts_64_while_keys_stay_capped_at_48(self):
        # 全局与每 Key 是两套独立契约：64 对全局合法、对 Key 仍非法。
        self.assertEqual(
            self.store.set_share_max_concurrency(64)
            ["share_max_concurrency"], 64)
        self.assertEqual(self.store.get_share_max_concurrency(), 64)
        self.assertEqual(
            self.store.render_projection()["share_max_concurrency"], 64)
        key48 = self.store.create_key("key48", max_concurrency=48)
        self.assertEqual(
            self.store.get_key(key48["key_id"])["max_concurrency"], 48)
        self.assertEqual(
            self.store.update_key(key48["key_id"], max_concurrency=48)
            ["max_concurrency"], 48)
        gen = self.generation()
        for bad in (0, 49, 65, True, "48", 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(StoreError):
                    self.store.create_key("bad-" + str(bad),
                                         max_concurrency=bad)
        with self.assertRaises(StoreError):
            self.store.update_key(key48["key_id"], max_concurrency=49)
        # 失败的 Key 校验不推进代次，也不改写已有行。
        self.assertEqual(self.generation(), gen)
        self.assertEqual(
            self.store.get_key(key48["key_id"])["max_concurrency"], 48)

    def test_share_max_lowering_is_revoking_raising_is_not(self):
        # 新部署初始 64：降到 10 是撤销向；随后 10→20 调高是安全向。
        self.store.set_share_max_concurrency(10)
        intent = self.store.find_pending_publish_intent(self.generation())
        self.assertTrue(intent["payload"]["revoking"])
        self.store.set_share_max_concurrency(20)
        intent = self.store.find_pending_publish_intent(self.generation())
        self.assertFalse(intent["payload"]["revoking"])

    def test_share_max_expected_mismatch_is_version_mismatch(self):
        self.store.set_share_max_concurrency(10)
        with self.assertRaises(VersionMismatch):
            self.store.set_share_max_concurrency(12, expected=9)
        self.assertEqual(self.store.get_share_max_concurrency(), 10)

    def test_share_max_idempotency_replay(self):
        first = self.store.set_share_max_concurrency(12,
                                                     idempotency_key="lim-1")
        gen = self.generation()
        replay = self.store.set_share_max_concurrency(20,
                                                      idempotency_key="lim-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["share_max_concurrency"],
                         first["share_max_concurrency"])
        self.assertEqual(self.generation(), gen)   # 重放不推进代次
        with self.assertRaises(Conflict):
            self.store.create_key("friendI", idempotency_key="lim-1")

    def test_expected_mismatch_uses_version_mismatch_type(self):
        # 409 同型：复用既有 VersionMismatch 冲突机制（status_code_for 已映射）。
        with self.assertRaises(VersionMismatch):
            self.store.set_share_max_concurrency(8, expected=99)


def _make_v1_db(path, key_rows):
    """Hand-build a REAL schema-v1 database (pre-upgrade shape)."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE keys (
            key_id TEXT PRIMARY KEY, name TEXT NOT NULL,
            secret_hash TEXT NOT NULL,
            secret_version INTEGER NOT NULL DEFAULT 1,
            masked_key TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            deleted_at TEXT, row_version INTEGER NOT NULL DEFAULT 1,
            UNIQUE(name, deleted_at));
        CREATE TABLE ops (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, idempotency_key TEXT,
            status TEXT NOT NULL, payload TEXT, created_at TEXT NOT NULL,
            UNIQUE(idempotency_key));
        CREATE TABLE configgen (
            schema_version INTEGER NOT NULL,
            config_generation INTEGER NOT NULL,
            applied_generation INTEGER NOT NULL DEFAULT 0);
        CREATE UNIQUE INDEX idx_keys_active_name
            ON keys(name) WHERE deleted_at IS NULL;
        """
    )
    conn.execute("INSERT INTO meta VALUES('schema_version','1')")
    conn.execute("INSERT INTO configgen VALUES(1, 42, 40)")
    for row in key_rows:
        conn.execute(
            "INSERT INTO keys(key_id,name,secret_hash,secret_version,masked_key,"
            "enabled,created_at,updated_at,deleted_at,row_version)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)", row)
    conn.commit()
    conn.close()


class UpgradeV1ToV2Tests(unittest.TestCase):
    """The FIRST upgrade path: v1 database -> v2 in one transaction, legacy
    limits preserved (rows=1, global=2), generations untouched."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.path = Path(self.td.name) / "legacy-v1.db"
        _make_v1_db(self.path, [
            ("11111111-1111-1111-1111-111111111111", "friend1",
             "a" * 64, 1, "um-share-****abcd", 1,
             "2026-09-01T00:00:00.000000Z", "2026-09-01T00:00:00.000000Z",
             None, 1),
            ("22222222-2222-2222-2222-222222222222", "friend-old",
             "b" * 64, 1, "um-share-****abcd", 0,
             "2026-08-01T00:00:00.000000Z", "2026-08-01T00:00:00.000000Z",
             "2026-08-02T00:00:00.000000Z", 2),   # tombstone
        ])
        self.addCleanup(self.td.cleanup)

    def _open(self):
        return ShareStore(self.path)

    def test_upgrade_preserves_rows_generations_and_writes_legacy_limits(self):
        store = ShareStore(self.path)
        try:
            # R11-B1: the DB marker is SCHEMA_VERSION (3); the projection
            # keeps the share-side version 2 (whitelist {1,2}).
            self.assertEqual(store.generation()["schema_version"],
                             SCHEMA_VERSION)
            self.assertEqual(store.generation()["config_generation"], 42)
            self.assertEqual(store.generation()["applied_generation"], 40)
            rows = store.list_keys(include_deleted=True)
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertEqual(row["max_concurrency"], 1)
            self.assertEqual(store.get_share_max_concurrency(), 2)
            proj = store.render_projection()
            self.assertEqual(proj["schema_version"], 2)
            self.assertEqual(proj["share_max_concurrency"], 2)
            self.assertEqual([e["max_concurrency"] for e in proj["data"]], [1])
        finally:
            store.close()

    def test_upgrade_is_persisted_across_reopen(self):
        ShareStore(self.path).close()
        store = ShareStore(self.path)
        try:
            self.assertEqual(store.generation()["schema_version"],
                             SCHEMA_VERSION)
            # 二次打开不再重复迁移（幂等）。
            self.assertEqual(store.get_share_max_concurrency(), 2)
            self.assertEqual(store.generation()["config_generation"], 42)
        finally:
            store.close()

    def test_upgrade_keeps_version_mismatch_semantics(self):
        store = ShareStore(self.path)
        try:
            row = store.list_keys()[0]
            with self.assertRaises(VersionMismatch):
                store.update_key(row["key_id"], enabled=False,
                                  expected_row_version=99)
        finally:
            store.close()

    def test_upgrade_keeps_migration_conflict_semantics(self):
        store = ShareStore(self.path)
        try:
            # 活跃身份 friend1 与源摘要不同 → 迁移拒绝语义保留。
            with self.assertRaises(MigrationConflict):
                store.migrate_apply({"friend1": "c" * 64})
        finally:
            store.close()

    def test_unsupported_future_version_still_fails_closed(self):
        conn = sqlite3.connect(str(self.path))
        try:
            # 99 = a genuinely FUTURE version (SCHEMA_VERSION is 3 now; the
            # old fixture reused 3, which is current since R11-B1).
            conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(StoreError):
            ShareStore(self.path)


class RobustnessTests(unittest.TestCase):
    def test_corrupt_db_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "share.db"
            db.write_bytes(b"this is not a sqlite database")
            with self.assertRaises(StoreError):
                ShareStore(db)
            # The corrupt file is not silently deleted or replaced.
            self.assertEqual(db.read_bytes(), b"this is not a sqlite database")

    def test_load_digest_config_rejects_bad_shape(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            p.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
            with self.assertRaises(StoreError):
                load_digest_config(p)
            p.write_text(json.dumps({"clients": {"x": "not-hex"}}), encoding="utf-8")
            with self.assertRaises(StoreError):
                load_digest_config(p)

    def test_parallel_create_unique_names(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "share.db"
            ShareStore(path).close()
            names = [f"friend-{i}" for i in range(8)]

            def create(name):
                s = ShareStore(path)
                try:
                    c = s.create_key(name)
                    return c["key_id"]
                finally:
                    s.close()

            with ThreadPoolExecutor(max_workers=4) as pool:
                ids = list(pool.map(create, names))
            self.assertEqual(len(set(ids)), len(ids))
            s = ShareStore(path)
            try:
                rows = s.list_keys()
                self.assertEqual({r["name"] for r in rows}, set(names))
                self.assertEqual(len(rows), len(names))
            finally:
                s.close()

    def test_parallel_same_idempotency_key_creates_one_identity(self):
        # R2: the idempotency check, key insert, generation bump and op record
        # must be one BEGIN IMMEDIATE transaction. Concurrent losers replay the
        # winner's metadata and must NEVER receive the plaintext again.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "share.db"
            ShareStore(path).close()
            results = []
            lock = threading.Lock()

            def create():
                s = ShareStore(path)
                try:
                    out = s.create_key("friend-same", idempotency_key="idem-same")
                    with lock:
                        results.append(out)
                finally:
                    s.close()

            threads = [threading.Thread(target=create) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            key_ids = {r["key_id"] for r in results if r.get("key_id")}
            plaintexts = [r for r in results if "secret" in r]
            self.assertEqual(len(key_ids), 1)
            self.assertEqual(len(plaintexts), 1)
            self.assertGreaterEqual(len(results), 6)
            for r in results:
                if r.get("secret_unavailable"):
                    self.assertNotIn("secret", r)
            s = ShareStore(path)
            try:
                self.assertEqual(len(s.list_keys()), 1)
            finally:
                s.close()


if __name__ == "__main__":
    unittest.main()
