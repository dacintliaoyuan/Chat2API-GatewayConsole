"""S2 real-entry wiring: the Key recovery fence (security only).

Run from ``ai-gateway``:

    python -m unittest tests.test_cloud_ops_wiring

The drill stays isolated: real ``ShareStore`` databases in temporary
directories and ``MemoryObjectStore`` instead of R2/OSS.  No production
service is started, no real object store is contacted and no friend Key is
changed.  The budget/automatic-backup features are gone (D18-11); the
independent security floor and its revoke/ACK fence stay.
"""
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]          # ai-gateway/
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "um-cloud-deploy"))
sys.path.insert(0, str(HERE / "chat2api"))
sys.path.insert(0, str(HERE / "tests"))

import backup_restore  # noqa: E402
import usagesrv  # noqa: E402
import share_auth as share  # noqa: E402
from _share_instance_double import FakeShareServer  # noqa: E402
from share.store import ShareStore  # noqa: E402


def _entry(key_id="ka", secret="test-secret"):
    return {
        "key_id": key_id,
        "name": "friend-" + key_id,
        "secret_hash": hashlib.sha256(secret.encode()).hexdigest(),
        "secret_version": 1,
        "enabled": True,
        "max_concurrency": 2,
    }


def _projection(entries, generation=1, share_max=4):
    return {"schema_version": 2, "config_generation": generation,
            "share_max_concurrency": share_max, "data": entries}


def _store(root):
    return ShareStore(root / "share.db")


def _backup_key(root):
    path = root / "backup.key"
    backup_restore.write_backup_key(path)
    return path, backup_restore.load_backup_key(path)


class SecurityFenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True, prefix="security-fence-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = _store(self.root)
        self.addCleanup(self.store.close)

    def test_revoke_intent_is_journaled_before_local_change(self):
        alpha = self.store.create_key("alpha")
        generation = self.store.generation()["config_generation"]
        self.store.delete_key(alpha["key_id"])
        state = self.store.security_state()
        self.assertEqual(state["generation_floor"], generation + 1)
        record = state["records"][-1]
        self.assertEqual(record["action"], "delete")
        self.assertEqual(record["key_id"], alpha["key_id"])
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["generation"], generation + 1)
        self.assertFalse(self.store.verify_secret(
            alpha["key_id"], alpha["secret"]))

    def test_runtime_ack_binds_security_journal(self):
        alpha = self.store.create_key("alpha")
        generation = self.store.generation()["config_generation"]
        self.store.delete_key(alpha["key_id"])
        early = self.store.commit_applied(
            generation, instance_id="share-instance")
        self.assertEqual(early["security_applied"], [])
        self.assertEqual(self.store.security_state()["records"][-1]["status"],
                         "pending")
        acked = self.store.commit_applied(
            generation + 1, instance_id="share-instance",
            security_generation=generation + 1)
        self.assertEqual(len(acked["security_applied"]), 1)
        record = self.store.security_state()["records"][-1]
        self.assertEqual(record["status"], "applied")
        self.assertEqual(record["instance_id"], "share-instance")
        self.assertEqual(record["applied_generation"], generation + 1)

    def test_remote_unconfirmed_security_record_stays_pending(self):
        alpha = self.store.create_key("alpha")
        self.store.delete_key(alpha["key_id"])
        state = self.store.security_state()
        failing = backup_restore.SecurityRecordStoreAdapter(
            backup_restore.MemoryObjectStore(fail_puts=1))
        published = backup_restore.publish_security_records(
            self.store, failing)
        self.assertEqual(published["confirmed"], 0)
        self.assertEqual(published["pending"], 1)
        self.assertEqual(self.store.security_state()["pending_remote"], 1)
        latest = backup_restore.latest_security_state(failing)
        self.assertFalse(latest["confirmed"])

        adapter = backup_restore.SecurityRecordStoreAdapter(
            backup_restore.MemoryObjectStore())
        published = backup_restore.publish_security_records(
            self.store, adapter)
        self.assertEqual(published["confirmed"], 1)
        self.assertEqual(published["pending"], 0)
        self.assertEqual(self.store.security_state()["pending_remote"], 0)
        latest = backup_restore.latest_security_state(adapter)
        self.assertTrue(latest["confirmed"])
        self.assertEqual(latest["state"]["generation_floor"],
                         state["generation_floor"])

    def test_old_backup_after_new_revoke_still_rejects_old_key(self):
        alpha = self.store.create_key("alpha")
        before = self.store.security_state()
        key_path, key = _backup_key(self.root)
        ledger = self.root / "usage.jsonl"
        ledger.write_text("", encoding="utf-8")
        price = self.root / "prices.json"
        price.write_text(json.dumps({"version": "price-v1",
                                     "models": {}, "providers": {}}),
                          encoding="utf-8")
        config = self.root / "share-management.json"
        config.write_text(json.dumps({"schema_version": 2,
                                      "enabled": False}), encoding="utf-8")
        backup = backup_restore.create_backup(
            self.store, ledger, key, self.root / "remote-sim",
            price_table_path=price, config_paths=[config],
            security_state=before)
        self.store.delete_key(alpha["key_id"])
        newer = self.store.security_state()
        security_path = self.root / "security-state.json"
        backup_restore.write_security_state(security_path, newer)
        target = self.root / "recovery"
        with self.assertRaises(backup_restore.BackupGenerationError):
            backup_restore.restore_for_recovery(
                backup["backup_path"], key, target_dir=target,
                security_state_path=security_path,
                price_table_path=price, config_paths=[config])
        self.assertFalse((target / "share.db").exists())

        forced = self.root / "forced"
        backup_restore.restore_backup(
            backup["backup_path"], key,
            target_db_path=forced / "share.db",
            target_ledger_path=forced / "usage.jsonl",
            target_extra_dir=forced / "config",
            require_config=True)
        restored = ShareStore(forced / "share.db")
        self.addCleanup(restored.close)
        fence = restored.apply_security_fence(newer)
        self.assertIn(alpha["key_id"], fence["disabled_keys"])
        self.assertFalse(restored.verify_secret(
            alpha["key_id"], alpha["secret"]))
        self.assertFalse(restored.get_key(alpha["key_id"])["enabled"])

    def test_recovery_requires_independent_security_state(self):
        self.store.create_key("alpha")
        key_path, key = _backup_key(self.root)
        ledger = self.root / "usage.jsonl"
        ledger.write_text("", encoding="utf-8")
        price = self.root / "prices.json"
        price.write_text(json.dumps({"version": "price-v1",
                                     "models": {}, "providers": {}}),
                          encoding="utf-8")
        config = self.root / "share-management.json"
        config.write_text(json.dumps({"schema_version": 2,
                                      "enabled": False}), encoding="utf-8")
        backup = backup_restore.create_backup(
            self.store, ledger, key, self.root / "remote-sim",
            price_table_path=price, config_paths=[config],
            security_state=self.store.security_state())
        with self.assertRaises(backup_restore.BackupSecurityError):
            backup_restore.restore_for_recovery(
                backup["backup_path"], key,
                target_dir=self.root / "recovery",
                price_table_path=price, config_paths=[config])


class CloudOpsReadFaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True, prefix="cloud-ops-read-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_usagesrv_cloud_ops_read_face(self):
        security = self.root / "security-state.json"
        backup_restore.write_security_state(
            security, {"format": "um-share-security-state-v1",
                        "generation_floor": 3, "records": []})
        with mock.patch.dict(os.environ, {
                "UM_CLOUD_SECURITY_STATE": str(security)}):
            payload = usagesrv.cloud_ops_status()
        self.assertTrue(payload["available"])
        self.assertIsInstance(payload["read_at"], int)
        self.assertNotIn("checked_at", payload)
        self.assertNotIn("budget", payload)
        self.assertNotIn("backup", payload)
        self.assertTrue(payload["security"]["available"])
        self.assertEqual(payload["security"]["generation_floor"], 3)
        # D18-11: the budget/backup features are gone; the independent
        # security floor stays and is the only status section.
        self.assertEqual(set(payload), {
            "available", "read_at", "security", "reasons", "unresolved"})
        # These facts are explicitly NOT closed by this read face.
        self.assertEqual(payload["unresolved"]["remote_export_confirmed"],
                         "not_checked_here")
        self.assertEqual(payload["unresolved"]["runtime_generation_ack"],
                         "not_checked_here")

    def test_cloud_ops_read_face_never_fabricates_unconfigured_success(self):
        """Missing configuration is configured:false/available:false, not a
        fabricated healthy security state."""
        saved = {name: os.environ.pop(name, None) for name in (
            "UM_CLOUD_SECURITY_STATE",)}
        self.addCleanup(lambda: [
            os.environ.__setitem__(name, value)
            if value is not None else os.environ.pop(name, None)
            for name, value in saved.items()])
        payload = usagesrv.cloud_ops_status()
        self.assertFalse(payload["available"])
        self.assertFalse(payload["security"]["configured"])
        self.assertFalse(payload["security"]["available"])
        self.assertEqual(payload["reasons"], [])
        self.assertEqual(payload["unresolved"]["remote_export_confirmed"],
                         "not_checked_here")

    def test_share_entry_ack_carries_runtime_security_generation(self):
        config = self.root / "projection.json"
        config.write_text(json.dumps(_projection([_entry()])), encoding="utf-8")
        # The ACK now merges the persisted revoke state, so keep it in a
        # private temporary file instead of the PRODUCT default path.
        state_file = self.root / "share-revoke.state"
        state_patch = mock.patch.dict(
            os.environ, {share.REVOKE_STATE_ENV: str(state_file)})
        state_patch.start()
        self.addCleanup(state_patch.stop)
        share.reset_revoke_state_cache()
        self.addCleanup(share.reset_revoke_state_cache)
        server = FakeShareServer(config=config)
        payload = _projection([_entry()])
        from share.broker_protocol import INSTANCE_SIGNED_KEYS, sign
        fields = {
            "generation": payload["config_generation"],
            "fingerprint": hashlib.sha256(json.dumps(
                payload, sort_keys=True).encode()).hexdigest(),
        }
        request = sign("inst-secret", {
            "v": 2, "action": "load",
            "request_id": "load-1",
            "ts": int(time.time()) + 1,
            "instance_id": server.instance_id,
            "fields": fields,
        }, INSTANCE_SIGNED_KEYS)
        previous = dict(share._revoke_applied_generations)
        share._revoke_applied_generations.clear()
        share._revoke_applied_generations["ka"] = 7

        def restore():
            share._revoke_applied_generations.clear()
            share._revoke_applied_generations.update(previous)

        self.addCleanup(restore)
        reply = server.handle_instance_control(request, "inst-secret")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["security_generation"], 7)
