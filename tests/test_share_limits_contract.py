"""D18-3/D18-4 Key lifecycle and concurrency contract (W1).

These tests pin the r18 caps: a NEW Key defaults to 16, a single Key may be
configured up to 48 and the GLOBAL share limit is 64. Old v1/v2 rows written
under the legacy 1/2 and 64/128 contracts are still readable and are CLAMPED
in the projection; the stored values are normalized only by the explicit
``normalize_limits`` action, which drives the existing ``limit_lower``
revocation direction and its runtime security ACK. A lowering that the runtime
does not confirm stays blocked — the tests never accept a silent rewrite.
"""
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chat2api"))

import share.broker_protocol as bp  # noqa: E402
import share_entry as share  # noqa: E402
from share.management import ShareManager  # noqa: E402
from share.store import (KEY_MAX_CONCURRENCY_MAX,  # noqa: E402
                         NEW_KEY_DEFAULT_MAX_CONCURRENCY,
                         SHARE_MAX_CONCURRENCY_MAX, ShareStore, StoreError)


class _AckBroker:
    """Broker double whose runtime ACK watermark is controlled explicitly."""

    def __init__(self, cover=True):
        self.published = []
        self.cover = cover
        self.emergency_stops = 0

    def publish(self, projection, terminate_keys=None):
        self.published.append(projection)
        ack = {"applied_generation": projection["config_generation"],
               "instance_id": "test-instance"}
        if self.cover:
            ack["security_generation"] = projection["config_generation"]
        return ack

    def snapshot(self):
        return {"name": "test", "state": "ok"}

    def emergency_stop(self):
        self.emergency_stops += 1
        return {"state": "stopped", "stopped": True}


def _make_v1_db(path):
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
    conn.execute("INSERT INTO configgen VALUES(1, 7, 6)")
    conn.execute(
        "INSERT INTO keys(key_id,name,secret_hash,secret_version,masked_key,"
        "enabled,created_at,updated_at,deleted_at,row_version)"
        " VALUES('legacy-1','friend1',?,1,'um-share-****abcd',1,"
        "'2026-09-01T00:00:00.000000Z','2026-09-01T00:00:00.000000Z',NULL,1)",
        ("a" * 64,))
    conn.commit()
    conn.close()


class _TempStore:
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.td.cleanup)
        self.path = Path(self.td.name) / "share.db"

    def _store(self):
        store = ShareStore(self.path)
        self.addCleanup(store.close)
        return store


class KeyLimitContractTests(_TempStore, unittest.TestCase):
    def test_new_key_defaults_to_16_at_store_and_management(self):
        store = ShareStore(self.path)
        try:
            created = store.create_key("store-default")
            self.assertEqual(
                store.get_key(created["key_id"])["max_concurrency"], 16)
        finally:
            store.close()
        broker = _AckBroker()
        mgr = ShareManager(store_path=self.path, broker=broker)
        created = mgr.create_key("management-default")
        self.assertEqual(
            mgr.get_key(created["key_id"])["max_concurrency"], 16)
        self.assertEqual(NEW_KEY_DEFAULT_MAX_CONCURRENCY, 16)

    def test_per_key_cap_is_48(self):
        self.assertEqual(KEY_MAX_CONCURRENCY_MAX, 48)
        store = ShareStore(self.path)
        try:
            created = store.create_key("cap", max_concurrency=48)
            self.assertEqual(
                store.get_key(created["key_id"])["max_concurrency"], 48)
            for bad in (0, 49, 65, True, "48", 1.5):
                with self.subTest(bad=bad):
                    with self.assertRaises(StoreError):
                        store.create_key("bad-" + str(bad),
                                         max_concurrency=bad)
            with self.assertRaises(StoreError):
                store.update_key(created["key_id"], max_concurrency=49)
        finally:
            store.close()

    def test_global_cap_is_64(self):
        self.assertEqual(SHARE_MAX_CONCURRENCY_MAX, 64)
        store = ShareStore(self.path)
        try:
            self.assertEqual(store.set_share_max_concurrency(64)
                             ["share_max_concurrency"], 64)
            for bad in (0, 65, 129, True, "64", 1.5):
                with self.subTest(bad=bad):
                    with self.assertRaises(StoreError):
                        store.set_share_max_concurrency(bad)
        finally:
            store.close()

    def test_management_global_and_key_bounds(self):
        mgr = ShareManager(store_path=self.path, broker=_AckBroker())
        for bad in (0, 65, 129, True, "64", 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(StoreError):
                    mgr.set_share_limits(bad)
        for bad in (0, 49, 65, True, "48", 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(StoreError):
                    mgr.create_key("bad-" + str(bad), max_concurrency=bad)


class ProjectionClampTests(_TempStore, unittest.TestCase):
    def _raw_set_global(self, value):
        conn = sqlite3.connect(str(self.path))
        try:
            conn.execute(
                "UPDATE meta SET value=? WHERE key='share_max_concurrency'",
                (str(value),))
            conn.commit()
        finally:
            conn.close()

    def _raw_set_key(self, key_id, value):
        conn = sqlite3.connect(str(self.path))
        try:
            conn.execute(
                "UPDATE keys SET max_concurrency=? WHERE key_id=?",
                (value, key_id))
            conn.commit()
        finally:
            conn.close()

    def test_projection_clamps_stored_legacy_values(self):
        store = ShareStore(self.path)
        try:
            created = store.create_key("legacy", max_concurrency=48)
        finally:
            store.close()
        self._raw_set_global(128)
        self._raw_set_key(created["key_id"], 64)
        store = ShareStore(self.path)
        try:
            projection = store.render_projection()
            self.assertEqual(projection["share_max_concurrency"], 64)
            self.assertEqual(projection["data"][0]["max_concurrency"], 48)
            # The stored values stay untouched: the clamp is read-only.
            self.assertEqual(store.get_share_max_concurrency(), 128)
            self.assertEqual(
                store.get_key(created["key_id"])["max_concurrency"], 64)
        finally:
            store.close()

    def test_status_reports_effective_stored_and_normalization(self):
        store = ShareStore(self.path)
        try:
            created = store.create_key("legacy", max_concurrency=48)
        finally:
            store.close()
        self._raw_set_global(128)
        self._raw_set_key(created["key_id"], 64)
        mgr = ShareManager(store_path=self.path, broker=_AckBroker())
        status = mgr.status()
        self.assertEqual(status["share_max_concurrency"], 64)
        self.assertEqual(status["share_max_concurrency_stored"], 128)
        self.assertTrue(status["limits_normalization_required"])
        self.assertEqual(status["limits_clamped_keys"], 1)

    def test_parser_clamps_legacy_projection_and_rejects_beyond_old_caps(self):
        parsed = bp.parse_projection_payload({
            "schema_version": 2, "config_generation": 1,
            "share_max_concurrency": 128,
            "data": [{"key_id": "k", "name": "f", "secret_hash": "a" * 64,
                       "secret_version": 1, "enabled": True,
                       "max_concurrency": 64}]})
        self.assertEqual(parsed["share_max_concurrency"], 64)
        self.assertEqual(parsed["data"][0]["max_concurrency"], 48)
        for bad in (129, 200):
            with self.subTest(global_bad=bad):
                with self.assertRaises(bp.ProtocolError):
                    bp.parse_projection_payload({
                        "schema_version": 2, "config_generation": 1,
                        "share_max_concurrency": bad, "data": []})
        for bad in (65, 200):
            with self.subTest(entry_bad=bad):
                with self.assertRaises(bp.ProtocolError):
                    bp.parse_projection_payload({
                        "schema_version": 2, "config_generation": 1,
                        "data": [{"key_id": "k", "name": "f",
                                  "secret_hash": "a" * 64,
                                  "secret_version": 1, "enabled": True,
                                  "max_concurrency": bad}]})
        # Missing legacy fields keep the old hardcodes.
        legacy = bp.parse_projection_payload({
            "schema_version": 1, "config_generation": 1, "data": []})
        self.assertEqual(legacy["share_max_concurrency"], 2)


class NormalizeLimitsTests(_TempStore, unittest.TestCase):
    def _seed_global(self, value):
        store = ShareStore(self.path)
        try:
            store.set_share_max_concurrency(64)
        finally:
            store.close()
        conn = sqlite3.connect(str(self.path))
        try:
            conn.execute(
                "UPDATE meta SET value=? WHERE key='share_max_concurrency'",
                (str(value),))
            conn.commit()
        finally:
            conn.close()

    def test_raises_migrated_v1_global_2_to_64(self):
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        path = Path(td.name) / "legacy-v1.db"
        _make_v1_db(path)
        broker = _AckBroker()
        mgr = ShareManager(store_path=path, broker=broker)
        self.assertEqual(mgr.status()["share_max_concurrency"], 2)
        result = mgr.normalize_limits()
        self.assertTrue(result["changed"])
        self.assertFalse(result["blocked"])
        self.assertEqual(result["global"], {"before": 2, "after": 64})
        status = mgr.status()
        self.assertEqual(status["share_max_concurrency"], 64)
        self.assertEqual(status["share_max_concurrency_stored"], 64)
        self.assertFalse(status["limits_normalization_required"])

    def test_lowers_old_global_128_through_limit_lower_ack(self):
        self._seed_global(128)
        broker = _AckBroker(cover=True)
        mgr = ShareManager(store_path=self.path, broker=broker)
        before = mgr.status()
        self.assertEqual(before["share_max_concurrency"], 64)
        self.assertEqual(before["share_max_concurrency_stored"], 128)
        self.assertTrue(before["limits_normalization_required"])
        result = mgr.normalize_limits()
        self.assertTrue(result["changed"])
        self.assertFalse(result["blocked"])
        self.assertEqual(result["global"], {"before": 128, "after": 64})
        self.assertEqual(mgr.status()["share_max_concurrency_stored"], 64)
        self.assertFalse(mgr.status()["limits_normalization_required"])

    def test_unconfirmed_global_lowering_is_blocked(self):
        self._seed_global(128)
        broker = _AckBroker(cover=False)
        mgr = ShareManager(store_path=self.path, broker=broker)
        result = mgr.normalize_limits()
        self.assertTrue(result["changed"])
        self.assertTrue(result["blocked"])
        self.assertEqual(result["global"], {"before": 128, "after": 64})
        status = mgr.status()
        self.assertEqual(status["share_max_concurrency"], 64)
        self.assertEqual(status["share_max_concurrency_stored"], 64)
        self.assertFalse(status["in_sync"])

    def test_lowers_old_key_64_through_limit_lower_ack(self):
        store = ShareStore(self.path)
        try:
            created = store.create_key("legacy-key", max_concurrency=48)
        finally:
            store.close()
        conn = sqlite3.connect(str(self.path))
        try:
            conn.execute("UPDATE keys SET max_concurrency=64 WHERE key_id=?",
                         (created["key_id"],))
            conn.commit()
        finally:
            conn.close()
        broker = _AckBroker(cover=True)
        mgr = ShareManager(store_path=self.path, broker=broker)
        before = mgr.status()
        self.assertEqual(before["limits_clamped_keys"], 1)
        self.assertTrue(before["limits_normalization_required"])
        result = mgr.normalize_limits()
        self.assertFalse(result["blocked"])
        self.assertEqual(result["keys"], [{
            "id": created["key_id"], "before": 64, "after": 48}])
        self.assertEqual(
            mgr.get_key(created["key_id"])["max_concurrency"], 48)
        self.assertFalse(mgr.status()["limits_normalization_required"])

    def test_unconfirmed_key_lowering_is_blocked(self):
        store = ShareStore(self.path)
        try:
            created = store.create_key("legacy-key", max_concurrency=48)
        finally:
            store.close()
        conn = sqlite3.connect(str(self.path))
        try:
            conn.execute("UPDATE keys SET max_concurrency=64 WHERE key_id=?",
                         (created["key_id"],))
            conn.commit()
        finally:
            conn.close()
        mgr = ShareManager(store_path=self.path,
                            broker=_AckBroker(cover=False))
        result = mgr.normalize_limits()
        self.assertTrue(result["blocked"])
        self.assertEqual(result["keys"], [{
            "id": created["key_id"], "before": 64, "after": 48}])
        self.assertFalse(mgr.status()["in_sync"])


class DisabledKeyContractTests(_TempStore, unittest.TestCase):
    def test_disabled_key_cannot_be_reenabled(self):
        store = ShareStore(self.path)
        try:
            created = store.create_key("disabled")
            disabled = store.update_key(created["key_id"], enabled=False)
            self.assertFalse(disabled["enabled"])
            with self.assertRaises(StoreError):
                store.update_key(created["key_id"], enabled=True)
            self.assertFalse(store.verify_secret(
                created["key_id"], created["secret"]))
            self.assertFalse(hasattr(store, "set_enabled"))
        finally:
            store.close()

    def test_management_update_key_refuses_any_enabled_write(self):
        mgr = ShareManager(store_path=self.path, broker=_AckBroker())
        created = mgr.create_key("revocable")
        with self.assertRaises(StoreError):
            mgr.update_key(created["key_id"], enabled=False)
        with self.assertRaises(StoreError):
            mgr.update_key(created["key_id"], enabled=True)
        revoked = mgr.revoke_and_terminate_key(created["key_id"],
                                               expected_row_version=1)
        self.assertFalse(revoked["enabled"])


class AdminCliContractTests(unittest.TestCase):
    def test_create_defaults_to_16_and_patch_has_no_enabled(self):
        import share.admin as share_admin
        args = share_admin._parse_args(["create", "--name", "friend"])
        self.assertEqual(args.max_concurrency, 16)
        with self.assertRaises(SystemExit):
            share_admin._parse_args(
                ["patch", "k", "--expected-version", "1", "--enable"])

    def test_limits_normalize_flag(self):
        import share.admin as share_admin
        self.assertFalse(
            share_admin._parse_args(["limits"]).normalize)
        self.assertTrue(
            share_admin._parse_args(["limits", "--normalize"]).normalize)


class _AdmissionCase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.td.cleanup)
        self.config = Path(self.td.name) / "projection.json"

        def _close_server():
            server = getattr(self, "server", None)
            if server is not None:
                server.server_close()

        self.addCleanup(_close_server)

    def _entry(self, key_id, max_concurrency):
        return {"key_id": key_id, "name": "friend-" + key_id,
                "secret_hash": hashlib.sha256(
                    ("secret-" + key_id).encode()).hexdigest(),
                "secret_version": 1, "enabled": True,
                "max_concurrency": max_concurrency}

    def _write(self, entries, share_max=64):
        self.config.write_text(json.dumps({
            "schema_version": 2, "config_generation": 1,
            "share_max_concurrency": share_max, "data": entries}),
            encoding="utf-8")

    def _server(self):
        self.server = share.ShareServer(("127.0.0.1", 0), self.config)
        return self.server


class AdmissionContractTests(_AdmissionCase):
    def _acquire(self, server, key_id, count):
        # The per-Key 20/minute sliding window is a SEPARATE limit from
        # concurrency; clearing its test-only bookkeeping isolates the
        # concurrency contract under test (the product rate limit stays
        # untouched and is covered by the Linux chain).
        for _ in range(count):
            self.assertTrue(server.acquire(key_id))
            with server.guard:
                server.history.pop(key_id, None)

    def test_a48_b16_equals_64_and_65th_is_rejected(self):
        entries = [self._entry("a", 48), self._entry("b", 48)]
        self._write(entries)
        server = self._server()
        self._acquire(server, "a", 48)
        self._acquire(server, "b", 16)
        self.assertEqual(sum(server.active.values()), 64)
        reason = {}
        self.assertFalse(server.acquire("b", reason_out=reason))
        self.assertEqual(reason["reason"], "share_concurrency")

    def test_a48_b48_config_is_legal_but_total_stays_64(self):
        entries = [self._entry("a", 48), self._entry("b", 48)]
        self._write(entries)
        server = self._server()
        self._acquire(server, "a", 48)
        self._acquire(server, "b", 16)
        self.assertEqual(sum(server.active.values()), 64)
        reason = {}
        self.assertFalse(server.acquire("b", reason_out=reason))
        self.assertEqual(reason["reason"], "share_concurrency")

    def test_49th_request_of_one_key_is_rejected(self):
        self._write([self._entry("a", 48)])
        server = self._server()
        self._acquire(server, "a", 48)
        reason = {}
        self.assertFalse(server.acquire("a", reason_out=reason))
        self.assertEqual(reason["reason"], "per_key_concurrency")


if __name__ == "__main__":
    unittest.main()
