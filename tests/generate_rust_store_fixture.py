#!/usr/bin/env python3
"""Generate the Rust share-store fixture from the Python store owner.

The Rust store tests never hand-write the SQLite schema, key lifecycle results,
security journal rows or projection payloads. They deserialize
``tests/fixtures/rust_store_rev1.json``, which this generator produces by
running the REAL ``share.store.ShareStore`` against a temporary database with
deterministic uuid/timestamp sources.

Run from the repository root:

    python tests/generate_rust_store_fixture.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_RELATIVE = "share/store.py"
DESTINATION = ROOT / "tests" / "fixtures" / "rust_store_rev1.json"

sys.path[:0] = [str(ROOT)]

# Fixed secrets: the fixture is about behavior, not entropy.
ALPHA_SECRET = "um-share-alpha-0123456789abcdef"
BETA_SECRET = "um-share-beta-0123456789abcdef"
ROTATED_SECRET = "um-share-rotated-0123456789abc"


def _load_store_module():
    """Load the store module under a private name (test-order independent)."""
    path = ROOT / SOURCE_RELATIVE
    spec = importlib.util.spec_from_file_location("rust_store_fixture_store", path)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load %s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["rust_store_fixture_store"] = module
    spec.loader.exec_module(module)
    return module


store = _load_store_module()


class _Deterministic:
    """Deterministic uuid/timestamp sources with a restore point."""

    def __init__(self):
        self._uuid_index = 0
        self._now_index = 0
        self._saved_uuid4 = store.uuid.uuid4
        self._saved_now = store._now
        self._saved_token = store.secrets.token_urlsafe

    def uuid4(self):
        self._uuid_index += 1
        return uuid.UUID(int=self._uuid_index)

    def now(self):
        self._now_index += 1
        return "2026-11-15T00:00:%02d.%06dZ" % (
            self._now_index, self._now_index)

    def token_urlsafe(self, _bytes):
        return "rotated-token-0123456789abcdefghijklmnopqrstuvwxyz"

    def __enter__(self):
        store.uuid.uuid4 = self.uuid4
        store._now = self.now
        store.secrets.token_urlsafe = self.token_urlsafe
        return self

    def __exit__(self, *_exc):
        store.uuid.uuid4 = self._saved_uuid4
        store._now = self._saved_now
        store.secrets.token_urlsafe = self._saved_token
        return False


def _normalize_sql(sql):
    return " ".join((sql or "").split())


def _schema_section(path):
    instance = store.ShareStore(path)
    try:
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master"
                " WHERE sql IS NOT NULL ORDER BY type,name").fetchall()
        finally:
            conn.close()
    finally:
        instance.close()
    tables = {}
    for kind, name, _table, sql in rows:
        tables[f"{kind}:{name}"] = _normalize_sql(sql)
    conn = sqlite3.connect(path)
    try:
        columns = {}
        for table in ("meta", "keys", "ops", "configgen", "security"):
            columns[table] = [
                [row[1], row[2], row[3], row[4], row[5]]
                for row in conn.execute(f"PRAGMA table_info({table})")]
        meta = dict(conn.execute("SELECT key,value FROM meta").fetchall())
        configgen = [
            {"schema_version": row[0], "config_generation": row[1],
             "applied_generation": row[2]}
            for row in conn.execute(
                "SELECT schema_version,config_generation,applied_generation"
                " FROM configgen")]
        indexes = [
            [row[1], row[2]]
            for row in conn.execute("PRAGMA index_list(keys)")
        ]
    finally:
        conn.close()
    return {
        "schema_version": store.SCHEMA_VERSION,
        "projection_schema_version": store.PROJECTION_SCHEMA_VERSION,
        "objects": tables,
        "columns": columns,
        "meta": meta,
        "configgen": configgen,
        "indexes": indexes,
    }


def _migration_v1_section(root):
    """A schema-1 store upgraded by the real Python code (v1 -> v2 -> v3)."""
    path = os.path.join(root, "migrated-v1.db")
    conn = sqlite3.connect(path)
    setup_sql = """
        CREATE TABLE meta (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE keys (
            key_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            secret_hash TEXT NOT NULL,
            secret_version INTEGER NOT NULL DEFAULT 1,
            masked_key TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
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
        CREATE TABLE security (
            record_id TEXT PRIMARY KEY,
            key_id TEXT,
            generation INTEGER NOT NULL,
            action TEXT NOT NULL,
            secret_version INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            recorded_at TEXT NOT NULL,
            applied_at TEXT,
            applied_generation INTEGER,
            instance_id TEXT,
            remote_confirmed_at TEXT,
            remote_receipt TEXT
        );
        INSERT INTO meta(key,value) VALUES('schema_version','1');
        INSERT INTO configgen(schema_version,config_generation,applied_generation)
            VALUES(1,1,0);
        INSERT INTO keys(key_id,name,secret_hash,secret_version,masked_key,
            enabled,created_at,updated_at,deleted_at,row_version)
            VALUES('legacy-key','legacy','""" + "a" * 64 + """',3,'legacy-****',1,
            '2026-11-14T00:00:00.000000Z','2026-11-14T00:00:00.000000Z',
            NULL,4);
        """
    conn.executescript(setup_sql)
    conn.commit()
    conn.close()
    instance = store.ShareStore(path)
    try:
        migrated = _schema_columns(instance)
    finally:
        instance.close()
    conn = sqlite3.connect(path)
    try:
        meta = dict(conn.execute("SELECT key,value FROM meta").fetchall())
        configgen = [
            {"schema_version": row[0], "config_generation": row[1],
             "applied_generation": row[2]}
            for row in conn.execute(
                "SELECT schema_version,config_generation,applied_generation"
                " FROM configgen")]
        legacy = [
            {"key_id": row[0], "name": row[1], "secret_version": row[2],
             "masked_key": row[3], "enabled": row[4],
             "max_concurrency": row[5], "row_version": row[6],
             "deleted_at": row[7], "secret_blob": row[8],
             "secret_algo": row[9]}
            for row in conn.execute(
                "SELECT key_id,name,secret_version,masked_key,enabled,"
                "max_concurrency,row_version,deleted_at,secret_blob,secret_algo"
                " FROM keys")]
    finally:
        conn.close()
    return {"setup_sql": setup_sql, "columns": migrated, "meta": meta,
            "configgen": configgen, "legacy_row": legacy}


def _schema_columns(instance):
    conn = instance._conn
    return {
        table: [[row[1], row[2], row[3], row[4], row[5]]
                for row in conn.execute(f"PRAGMA table_info({table})")]
        for table in ("meta", "keys", "ops", "configgen", "security")
    }


def _generation(instance):
    return instance.generation()


def _security(instance):
    return {"records": instance.security_records(),
            "floor": instance.security_floor(),
            "state": instance.security_state()}


def _operations(instance):
    return [
        {"kind": op["kind"], "idempotency_key": op["idempotency_key"],
         "status": op["status"], "payload": op["payload"]}
        for op in instance.operations()
    ]


def _snapshot(instance):
    return {
        "generation": _generation(instance),
        "security": _security(instance),
        "operations": _operations(instance),
        "projection": instance.render_projection(),
        "keys": instance.list_keys(include_deleted=True),
    }


def _lifecycle_section(root):
    path = os.path.join(root, "lifecycle.db")
    with _Deterministic():
        instance = store.ShareStore(path)
        try:
            steps = []

            def step(name, fn):
                steps.append({"step": name, "result": fn()})

            created = instance.create_key(
                "alpha", secret=ALPHA_SECRET,
                idempotency_key="create-alpha", max_concurrency=16)
            step("create-alpha", lambda: created)
            beta = instance.create_key("beta", secret=BETA_SECRET)
            step("create-beta", lambda: beta)
            step("create-duplicate", lambda: _capture_error(
                lambda: instance.create_key("alpha", secret=ALPHA_SECRET)))
            step("replay-create", lambda: instance.create_key(
                "alpha", secret=ALPHA_SECRET,
                idempotency_key="create-alpha"))
            alpha_id = created["key_id"]
            beta_id = beta["key_id"]
            step("get-alpha", lambda: instance.get_key(alpha_id))
            step("rename-alpha", lambda: instance.rename_key(
                alpha_id, "alpha-renamed", expected_row_version=1))
            step("stale-rename", lambda: _capture_error(
                lambda: instance.rename_key(alpha_id, "alpha-stale",
                                           expected_row_version=1)))
            step("lower-concurrency", lambda: instance.update_key(
                alpha_id, max_concurrency=8, expected_row_version=2))
            step("reject-enable", lambda: _capture_error(
                lambda: instance.update_key(alpha_id, enabled=True)))
            step("rotate-beta", lambda: instance.rotate_key(
                beta_id, idempotency_key="rotate-beta"))
            step("replay-rotate", lambda: instance.rotate_key(
                beta_id, idempotency_key="rotate-beta"))
            step("rotate-other-target", lambda: _capture_error(
                lambda: instance.rotate_key(alpha_id,
                                           idempotency_key="rotate-beta")))
            step("delete-beta", lambda: instance.delete_key(beta_id))
            step("delete-again", lambda: instance.delete_key(beta_id))
            step("reveal-deleted", lambda: _capture_error(
                lambda: instance.reveal_key_secret(beta_id)))
            step("verify-alpha", lambda: instance.verify_secret(
                alpha_id, ALPHA_SECRET))
            step("verify-wrong", lambda: instance.verify_secret(
                alpha_id, BETA_SECRET))
            step("security-records", lambda: instance.security_records())
            step("commit-applied", lambda: instance.commit_applied(
                3, instance_id="instance-a", security_generation=2))
            step("security-after-ack", lambda: instance.security_records())
            step("set-limit-lower", lambda: instance.set_share_max_concurrency(
                32, expected=64, idempotency_key="limits-1"))
            step("replay-limit", lambda: instance.set_share_max_concurrency(
                32, expected=64, idempotency_key="limits-1"))
            step("snapshot", lambda: _snapshot(instance))
            step("pending-intent", lambda: instance.find_pending_publish_intent(
                instance.generation()["config_generation"]))
        finally:
            instance.close()
    return steps


def _capture_error(fn):
    try:
        fn()
    except store.StoreError as exc:
        return {"error": type(exc).__name__, "message": str(exc)}
    return None


def _projection_section(root):
    """Projection payloads accepted/rejected by the shared parser."""
    import importlib.util as importlib_util

    path = ROOT / "share/broker_protocol.py"
    spec = importlib_util.spec_from_file_location(
        "rust_store_fixture_protocol", path)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load %s" % path)
    protocol = importlib_util.module_from_spec(spec)
    sys.modules["rust_store_fixture_protocol"] = protocol
    spec.loader.exec_module(protocol)

    accepted = [
        {"schema_version": 2, "config_generation": 3,
         "share_max_concurrency": 64,
         "data": [{"key_id": "k1", "name": "n1",
                   "secret_hash": "a" * 64, "secret_version": 1,
                   "enabled": True, "max_concurrency": 16}]},
        {"schema_version": 1, "config_generation": 1, "data": []},
        {"schema_version": 2, "config_generation": 2,
         "share_max_concurrency": 128,
         "data": [{"key_id": "k1", "name": "n1",
                   "secret_hash": "b" * 64, "secret_version": 2,
                   "enabled": False, "max_concurrency": 64}]},
    ]
    rejected = [
        {"schema_version": True, "config_generation": 1, "data": []},
        {"schema_version": 3, "config_generation": 1, "data": []},
        {"schema_version": 2, "config_generation": -1, "data": []},
        {"schema_version": 2, "config_generation": 1, "data": "x"},
        {"schema_version": 2, "config_generation": 1, "data": [],
         "unknown": 1},
        {"schema_version": 2, "config_generation": 1,
         "data": [{"key_id": "k1", "name": "n1", "secret_hash": "a" * 64,
                   "secret_version": 1, "enabled": True,
                   "max_concurrency": 16, "extra": 1}]},
        {"schema_version": 2, "config_generation": 1,
         "data": [{"key_id": "k1", "name": "n1", "secret_hash": "a" * 63,
                   "secret_version": 1, "enabled": True,
                   "max_concurrency": 16}]},
        {"schema_version": 2, "config_generation": 1,
         "data": [{"key_id": "k1", "name": "n1", "secret_hash": "g" * 64,
                   "secret_version": 1, "enabled": True,
                   "max_concurrency": 16}]},
        {"schema_version": 2, "config_generation": 1,
         "data": [{"key_id": "k1", "name": "n1", "secret_hash": "a" * 64,
                   "secret_version": 0, "enabled": True,
                   "max_concurrency": 16}]},
        {"schema_version": 2, "config_generation": 1,
         "data": [{"key_id": "k1", "name": "n1", "secret_hash": "a" * 64,
                   "secret_version": 1, "enabled": 1,
                   "max_concurrency": 16}]},
        {"schema_version": 2, "config_generation": 1,
         "data": [{"key_id": "k1", "name": "n1", "secret_hash": "a" * 64,
                   "secret_version": 1, "enabled": True,
                   "max_concurrency": 0}]},
        {"schema_version": 2, "config_generation": 1,
         "data": [{"key_id": "k1", "name": "n1", "secret_hash": "a" * 64,
                   "secret_version": 1, "enabled": True,
                   "max_concurrency": 65}]},
        {"schema_version": 2, "config_generation": 1,
         "data": [{"key_id": "k1", "name": "n1", "secret_hash": "a" * 64,
                   "secret_version": 1, "enabled": True,
                   "max_concurrency": 16},
                  {"key_id": "k1", "name": "n2", "secret_hash": "b" * 64,
                   "secret_version": 1, "enabled": True,
                   "max_concurrency": 16}]},
    ]
    parsed = []
    for payload in accepted:
        parsed.append({"input": payload,
                       "output": protocol.parse_projection_payload(payload)})
    refused = []
    for payload in rejected:
        try:
            protocol.parse_projection_payload(payload)
        except protocol.ProtocolError as exc:
            refused.append({"input": payload, "code": exc.code,
                            "message": str(exc)})
        else:
            refused.append({"input": payload, "code": "accepted",
                            "message": ""})
    return {"accepted": parsed, "rejected": refused}


def _secret_vectors():
    """AES-256-GCM vectors: the POSIX scheme the Rust store writes."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = bytes(range(32))
    vectors = []
    for index, secret in enumerate((ALPHA_SECRET, BETA_SECRET)):
        nonce = bytes([index]) * 12
        blob = AESGCM(key).encrypt(
            nonce, secret.encode("utf-8"), b"aesgcm-file-key-v1")
        vectors.append({"key_hex": key.hex(), "nonce_hex": nonce.hex(),
                        "aad": "aesgcm-file-key-v1", "plaintext": secret,
                        "blob_hex": blob.hex()})
    return vectors


def generate():
    root = tempfile.mkdtemp(prefix="rust-store-fixture-")
    try:
        schema = _schema_section(os.path.join(root, "fresh.db"))
        migration = _migration_v1_section(root)
        lifecycle = _lifecycle_section(root)
        projection = _projection_section(root)
        vectors = _secret_vectors()
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)
    return {
        "schema": schema,
        "migration": migration,
        "lifecycle": lifecycle,
        "projection": projection,
        "secret_vectors": vectors,
    }


def main():
    payload = generate()
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    DESTINATION.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")
    print(f"wrote {DESTINATION}")


if __name__ == "__main__":
    main()
