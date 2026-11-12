"""Plan-Revision 11 B1: reversible key-ciphertext storage + narrow readback.

Store-level contract tests over isolated temporary stores (never the
production share store):

* create/rotate store a DPAPI user-scope blob in the SAME mutation; the
  result states ``plaintext_viewable`` honestly (a protection failure
  degrades to the legacy hash-only row instead of inventing ciphertext);
* reveal answers the plaintext only under the same runtime identity, with
  DISTINCT failures for legacy rows (SecretUnavailable), undecryptable or
  digest-mismatched material (SecretUndecryptable), deleted identities
  (Disabled — their ciphertext was cleared) and unknown ids (NotFound);
* public rows never carry the hash or the blob; the backup file never
  contains plaintext; idempotent replays still never return plaintext;
* a hand-built v2 store file upgrades in place (columns added, hashes
  intact, legacy rows read back as unavailable, new keys get blobs).

The real DPAPI round-trip runs once against this Windows user (the same
identity contract B0 verified for the 8001 console host); everything else
monkeypatches the protection boundary so the tests stay deterministic.
"""
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import share_store  # noqa: E402
from share_store import (Disabled, NotFound, SecretUnavailable,  # noqa: E402
                         SecretUndecryptable, ShareStore, StoreError,
                         VersionMismatch)

_LEGACY_SECRET = "legacy-secret-0123456789abcdef"
_LEGACY_DIGEST = hashlib.sha256(_LEGACY_SECRET.encode("utf-8")).hexdigest()


class KeyRevealStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True,
                                               prefix="key-reveal-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "share.db"

    def _store(self):
        return ShareStore(self.path)

    # ------------------------------------------------- create/reveal happy
    def test_create_then_reveal_returns_same_plaintext(self):
        store = self._store()
        try:
            result = store.create_key("alpha")
            self.assertTrue(result["plaintext_viewable"])
            revealed = store.reveal_key_secret(result["key_id"])
            self.assertEqual(revealed["secret"], result["secret"])
            self.assertEqual(revealed["masked_key"], result["mk"])
            self.assertEqual(revealed["secret_version"], 1)
            self.assertEqual(revealed["name"], "alpha")
        finally:
            store.close()

    def test_public_rows_never_carry_hash_or_blob(self):
        store = self._store()
        try:
            result = store.create_key("bravo")
            row = store.get_key(result["key_id"])
            self.assertTrue(row["plaintext_viewable"])
            self.assertNotIn("secret_hash", row)
            self.assertNotIn("secret_blob", row)
            for listed in store.list_keys():
                self.assertNotIn("secret_hash", listed)
                self.assertNotIn("secret_blob", listed)
        finally:
            store.close()

    # ------------------------------------------------------------- rotate
    def test_rotate_replaces_plaintext_and_invalidates_old_secret(self):
        store = self._store()
        try:
            created = store.create_key("charlie")
            old_secret = created["secret"]
            rotated = store.rotate_key(created["key_id"])
            self.assertTrue(rotated["plaintext_viewable"])
            revealed = store.reveal_key_secret(created["key_id"])
            self.assertEqual(revealed["secret"], rotated["secret"])
            self.assertNotEqual(revealed["secret"], old_secret)
            self.assertFalse(store.verify_secret(created["key_id"], old_secret))
            self.assertTrue(store.verify_secret(
                created["key_id"], rotated["secret"]))
            self.assertEqual(revealed["secret_version"],
                             rotated["secret_version"])
        finally:
            store.close()

    # ------------------------------------------------------------- legacy
    def test_legacy_hash_only_row_answers_secret_unavailable(self):
        store = self._store()
        try:
            # migrate_apply writes rows WITHOUT plaintext blobs (the legacy
            # source only ever carried digests).
            store.migrate_apply({"friend1": _LEGACY_DIGEST})
            rows = store.list_keys()
            self.assertFalse(rows[0]["plaintext_viewable"])
            with self.assertRaises(SecretUnavailable):
                store.reveal_key_secret(rows[0]["key_id"])
            # The legacy key itself remains valid (hash contract untouched).
            self.assertTrue(store.verify_secret(rows[0]["key_id"],
                                                _LEGACY_SECRET))
        finally:
            store.close()

    # ------------------------------------------------------------- delete
    def test_delete_clears_ciphertext_and_reveal_answers_disabled(self):
        store = self._store()
        try:
            result = store.create_key("delta")
            store.delete_key(result["key_id"])
            with self.assertRaises(Disabled):
                store.reveal_key_secret(result["key_id"])
            conn = sqlite3.connect(str(self.path))
            try:
                blob = conn.execute(
                    "SELECT secret_blob FROM keys WHERE key_id=?",
                    (result["key_id"],)).fetchone()[0]
            finally:
                conn.close()
            self.assertIsNone(blob)
        finally:
            store.close()

    # ------------------------------------------- undecryptable / mismatch
    def test_undecryptable_blob_answers_secret_undecryptable(self):
        store = self._store()
        try:
            result = store.create_key("echo")
            with mock.patch.object(share_store, "unprotect_secret",
                                   side_effect=OSError("dpapi denied")):
                with self.assertRaises(SecretUndecryptable):
                    store.reveal_key_secret(result["key_id"])
            # The key itself remains valid while the plaintext is unreachable.
            self.assertTrue(store.verify_secret(result["key_id"],
                                                result["secret"]))
        finally:
            store.close()

    def test_digest_mismatch_fails_closed(self):
        store = self._store()
        try:
            result = store.create_key("foxtrot")
            with mock.patch.object(share_store, "unprotect_secret",
                                   return_value="um-share-forged"):
                with self.assertRaises(SecretUndecryptable):
                    store.reveal_key_secret(result["key_id"])
        finally:
            store.close()

    def test_unknown_id_answers_not_found(self):
        store = self._store()
        try:
            with self.assertRaises(NotFound):
                store.reveal_key_secret(
                    "00000000-0000-0000-0000-000000000000")
        finally:
            store.close()

    # ------------------------------------------------- version gate (B1)
    def test_reveal_version_gate_answers_version_mismatch(self):
        store = self._store()
        try:
            result = store.create_key("november")
            row = store.get_key(result["key_id"])
            with self.assertRaises(VersionMismatch):
                store.reveal_key_secret(
                    result["key_id"],
                    expected_row_version=row["row_version"] + 1)
            ok = store.reveal_key_secret(
                result["key_id"],
                expected_row_version=row["row_version"])
            self.assertEqual(ok["secret"], result["secret"])
        finally:
            store.close()

    # -------------------------------------- projection / parser split (B1)
    def test_projection_stays_schema2_and_passes_share_parser(self):
        """R11-B1 split-constant contract: the projection marker stays 2
        (share-side whitelist {1,2}) and the payload must be accepted by the
        SAME parser the publish chain uses."""
        import share_broker_protocol
        store = self._store()
        try:
            store.create_key("proj-check")
            proj = store.render_projection()
            self.assertEqual(proj["schema_version"], 2)
            parsed = share_broker_protocol.parse_projection_payload(proj)
            self.assertTrue(parsed["data"])
            self.assertEqual(parsed["data"][0]["name"], "proj-check")
        finally:
            store.close()

    # ------------------------------------------------ backup restore (B1)
    def test_backup_carries_blob_and_same_identity_restore_readback(self):
        store = self._store()
        try:
            result = store.create_key("oscar")
            target = Path(self.tmp.name) / "backup2.db"
            store.backup(target)
            conn = sqlite3.connect(str(target))
            try:
                blob = conn.execute(
                    "SELECT secret_blob FROM keys WHERE key_id=?",
                    (result["key_id"],)).fetchone()[0]
            finally:
                conn.close()
            # The backup carries the ciphertext, not the plaintext; under the
            # SAME identity it still decrypts back to the same secret.
            self.assertIsNotNone(blob)
            self.assertEqual(share_store.unprotect_secret(bytes(blob)),
                             result["secret"])
            restored = ShareStore(target)
            try:
                self.assertEqual(
                    restored.reveal_key_secret(result["key_id"])["secret"],
                    result["secret"])
            finally:
                restored.close()
        finally:
            store.close()

    # -------------------------------------- protection failure degradation
    def test_protection_failure_degrades_to_hash_only_row(self):
        store = self._store()
        try:
            with mock.patch.object(share_store, "protect_secret",
                                   side_effect=OSError("no dpapi")):
                result = store.create_key("golf")
            self.assertFalse(result["plaintext_viewable"])
            with self.assertRaises(SecretUnavailable):
                store.reveal_key_secret(result["key_id"])
            # Hash verification still works for the degraded row.
            self.assertTrue(store.verify_secret(result["key_id"],
                                                result["secret"]))
        finally:
            store.close()

    # ------------------------------------------------------------- replay
    def test_idempotent_replay_never_returns_plaintext(self):
        store = self._store()
        try:
            first = store.create_key("hotel", idempotency_key="idem-1")
            replay = store.create_key("hotel", idempotency_key="idem-1")
            self.assertTrue(replay["replayed"])
            self.assertTrue(replay.get("secret_unavailable"))
            self.assertNotIn("secret", replay)
            self.assertEqual(replay["key_id"], first["key_id"])
        finally:
            store.close()

    # ------------------------------------------------------------- backup
    def test_backup_contains_blob_but_never_plaintext(self):
        store = self._store()
        try:
            result = store.create_key("india")
            target = Path(self.tmp.name) / "backup.db"
            store.backup(target)
            raw = target.read_bytes()
            self.assertNotIn(result["secret"].encode("utf-8"), raw)
        finally:
            store.close()

    # ------------------------------------------------- real DPAPI (win32)
    @unittest.skipUnless(sys.platform == "win32",
                         "DPAPI round-trip is Windows-only")
    def test_real_dpapi_roundtrip_under_this_identity(self):
        blob = share_store.protect_secret("um-share-roundtrip-probe")
        self.assertEqual(share_store.unprotect_secret(blob),
                         "um-share-roundtrip-probe")

    # ------------------------------------------------- v2 -> v3 migration
    def test_v2_store_upgrades_in_place_and_keeps_contract(self):
        conn = sqlite3.connect(str(self.path))
        try:
            conn.executescript(
                """
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE keys (
                    key_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    secret_hash TEXT NOT NULL,
                    secret_version INTEGER NOT NULL DEFAULT 1,
                    masked_key TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    max_concurrency INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT,
                    row_version INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(name, deleted_at)
                );
                CREATE TABLE ops (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    idempotency_key TEXT,
                    status TEXT NOT NULL,
                    payload TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(idempotency_key)
                );
                CREATE TABLE configgen (
                    schema_version INTEGER NOT NULL,
                    config_generation INTEGER NOT NULL,
                    applied_generation INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO meta(key,value) VALUES('schema_version','2');
                INSERT INTO meta(key,value) VALUES('share_max_concurrency','2');
                INSERT INTO configgen(schema_version,config_generation,
                                      applied_generation) VALUES(2,1,1);
                """)
            conn.execute(
                "INSERT INTO keys(key_id,name,secret_hash,secret_version,"
                "masked_key,enabled,max_concurrency,created_at,updated_at,"
                "deleted_at,row_version) VALUES(?,?,?,?,?,1,1,?,?,NULL,1)",
                ("legacy-key-id", "kilo", _LEGACY_DIGEST, 1, "legacy-****",
                 "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
            conn.commit()
        finally:
            conn.close()
        store = self._store()
        try:
            self.assertEqual(store.generation()["schema_version"], 3)
            legacy = store.get_key("legacy-key-id")
            self.assertFalse(legacy["plaintext_viewable"])
            self.assertEqual(legacy["masked_key"], "legacy-****")
            with self.assertRaises(SecretUnavailable):
                store.reveal_key_secret("legacy-key-id")
            # The legacy hash still verifies.
            self.assertTrue(store.verify_secret("legacy-key-id",
                                                _LEGACY_SECRET))
            fresh = store.create_key("juliet")
            self.assertTrue(fresh["plaintext_viewable"])
            self.assertEqual(
                store.reveal_key_secret(fresh["key_id"])["secret"],
                fresh["secret"])
        finally:
            store.close()

    def test_unsupported_future_schema_fails_closed(self):
        conn = sqlite3.connect(str(self.path))
        try:
            conn.executescript(
                """
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO meta(key,value) VALUES('schema_version','99');
                """)
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(StoreError):
            self._store()


if __name__ == "__main__":
    unittest.main()
