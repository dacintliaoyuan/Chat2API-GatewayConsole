"""share/store.py — single-authority store for the UM sharing key lifecycle.

Offline/authoritative persistence (SQLite, single writer) replacing the plain
JSON CLI for the management stage:

* stable, non-reusable ``key_id`` (UUID), display ``name``, secret SHA-256,
  ``secret_version``, mask, enabled/soft-delete (tombstone) and ``row_version``.
* config generations: every mutation increments the *desired*
  ``config_generation``; the share confirms by committing an *applied*
  generation. The management layer distinguishes desired vs applied so a
  revocation that has not propagated never reports itself as "已撤销".
* idempotent create/rotate via idempotency keys (no clean replay; a replayed
  secret request returns metadata + ``secret_unavailable``).
* migration from the legacy name->digest JSON (source authoritative; refuses to
  auto-merge when source and a run copy conflict, and never revives entries
  absent from the source, e.g. friend2).

Secrets: only high-entropy random keys are stored hashed (SHA-256); plaintext
is returned once at create/rotate. Never log or persist plaintext.

Plan-Revision 11 (2026-09-23 decision closure): keys created or manually
ROTATED from this version on ALSO store a reversible ciphertext blob
(``secret_blob`` + ``secret_algo``). On Windows the blob is protected by DPAPI
user scope — readable only under the SAME Windows machine + same runtime user
identity (the 8001 console host runs as the logged-in user; B0 verified all five
gateway processes share the DAICNT dacint user). Plaintext is therefore reviewable
across page reloads and service restarts under that identity, and is NOT
promised to survive a different account, OS reinstall, or machine
migration. On POSIX the blob is AES-256-GCM under a file-held key (see
``share_secret_protection``); a missing, damaged or loosely-permissioned key
file ABORTS the mutation instead of silently writing a hash-only row, because a
readable row must never degrade. Legacy rows keep ``secret_blob IS NULL`` and
answer readback with a distinct ``SecretUnavailable``; rows whose ciphertext
fails to decrypt (or fails the digest cross-check, or carries no algorithm
marker) answer ``SecretUndecryptable``. Deleting a key clears its ciphertext in
the same mutation. Old hash-only keys stay valid and keep their hash-only
contract until a manual rotation. The store never logs plaintext; the only
plaintext-carrying responses are the immediate create/rotate reply and the
narrow per-key readback.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 3
# R11-B1 (Plan-Revision 11): the DB/meta migration marker is deliberately
# SPLIT from the share-side projection marker. The share protocol whitelist
# (share_broker_protocol.PROJECTION_SCHEMA_VERSIONS) only accepts {1, 2}, so the
# projection keeps answering 2 while the local DB marker advances to 3 — a shared
# constant would make the publish chain reject our own projection.
PROJECTION_SCHEMA_VERSION = 2

# DPAPI user-scope scheme marker stored alongside the blob so future
# schemes are distinguishable without guessing from blob bytes. The POSIX
# ``aesgcm-file-key-v1`` marker lives in share_secret_protection, which is
# imported lazily on POSIX so Windows deployments without that module keep
# working.
SECRET_ALGO_DPAPI_USER = "dpapi-user-scope-v1"

# Schema v2 (R6-A2 concurrency limits): keys.max_concurrency column +
# meta ``share_max_concurrency``. Migration (v1 -> v2) happens in ONE
# transaction and preserves the OLD effective limits: migrated rows get
# max_concurrency=1 (the old per-key hardcode) and share_max_concurrency=2
# (the old global hardcode). A brand-new (fresh) store starts a new
# deployment at the confirmed total limit of 64.
LEGACY_KEY_MAX_CONCURRENCY = 1
LEGACY_SHARE_MAX_CONCURRENCY = 2
NEW_DEPLOYMENT_SHARE_MAX_CONCURRENCY = 64
# D18-4 (Plan-Revision 18): a NEW Key defaults to 16 concurrent requests,
# a single Key may be configured up to 48 and the GLOBAL share limit is 64
# (all keys and every entry point together). The LEGACY_* values above stay
# only for the v1 -> v2 migration and for old rows/projections; they are no
# longer a new-Key default. The two bounds must never be shared again - a
# single shared validator would either cap the global limit at the per-key 48
# or let one key reserve the whole global budget.
NEW_KEY_DEFAULT_MAX_CONCURRENCY = 16
KEY_MAX_CONCURRENCY_MIN = 1
KEY_MAX_CONCURRENCY_MAX = 48
SHARE_MAX_CONCURRENCY_MIN = 1
SHARE_MAX_CONCURRENCY_MAX = 64

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _now() -> str:
    return datetime.now(timezone.utc).strftime(_TS_FORMAT)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _mask(secret: str) -> str:
    # Never derive anything that could reconstruct the key; show scheme + tail.
    return "um-share-****" + secret[-4:] if len(secret) >= 4 else "um-share-****"


def _secret_eq(secret: str, digest: str) -> bool:
    return hmac.compare_digest(_hash(secret), digest)


class StoreError(Exception):
    """Base for predictable store errors (mapped to HTTP codes by the API)."""


class Conflict(StoreError):
    pass


class NotFound(StoreError):
    pass


class Disabled(StoreError):
    pass


class VersionMismatch(StoreError):
    pass


class MigrationConflict(StoreError):
    pass


class SecretUnavailable(StoreError):
    """The row stores no ciphertext (legacy hash-only key or protection
    was unavailable at create/rotate time); the plaintext cannot be shown
    and the hash-only contract applies until a manual rotation."""


class SecretUndecryptable(StoreError):
    """A ciphertext blob exists but cannot be decrypted under this runtime
    identity (different Windows user, migrated machine, missing/wrong POSIX key
    file, damaged blob), or the decrypted material fails the digest
    cross-check."""


def _load_secret_protection():
    """Import the POSIX AES-GCM adapter lazily.

    Windows never needs it (DPAPI stays in this module), so a Windows
    deployment whose copy closure predates that module still imports and
    protects normally. POSIX requires it and fails with a clear
    :class:`StoreError` when it is missing.
    """
    try:
        share_secret_protection = __import__("importlib").import_module("share.secret_protection")
    except ImportError as exc:
        raise StoreError(
            "share secret protection module is not available") from exc
    return share_secret_protection


def _protect_secret_dpapi(secret: str) -> bytes:
    """Protect plaintext with Windows DPAPI user scope (R11 contract).

    User scope (no extra entropy) is exactly the decided contract: the
    blob decrypts only under the SAME Windows machine + same user identity
    — a different account, OS reinstall or machine migration cannot
    decrypt it, which the readback path reports honestly instead of
    pretending recoverability. Raises on non-Windows platforms and on any
    DPAPI failure; the Windows create/rotate path degrades to the legacy
    hash-only row rather than storing invented material.
    """
    if sys.platform != "win32":
        raise StoreError("secret protection requires Windows DPAPI")
    import ctypes
    import ctypes.wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_byte))]

    def _blob(data: bytes) -> _DATA_BLOB:
        buf = (ctypes.c_byte * len(data)).from_buffer_copy(data)
        return _DATA_BLOB(len(data), ctypes.cast(buf,
                                                 ctypes.POINTER(ctypes.c_byte)))

    raw = secret.encode("utf-8")
    in_blob = _blob(raw)
    out_blob = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob), "um-share-console-key", None, None, None,
        0x01,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(out_blob))
    if not ok:
        raise StoreError("DPAPI protection failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def _unprotect_secret_dpapi(blob: bytes) -> str:
    """Reverse of :func:`_protect_secret_dpapi` (DPAPI user scope)."""
    import ctypes
    import ctypes.wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_byte))]

    in_blob = _DATA_BLOB(len(blob),
                         ctypes.cast((ctypes.c_byte * len(blob))
                                     .from_buffer_copy(blob),
                                     ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0,
        ctypes.byref(out_blob))
    if not ok:
        raise StoreError("DPAPI unprotection failed")
    try:
        data = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    return data.decode("utf-8")


def protect_secret(secret: str) -> bytes:
    """Protect plaintext for a NEW row with this platform's scheme.

    Windows keeps the R11 DPAPI user-scope contract unchanged; POSIX uses
    AES-256-GCM with the file-held key. The scheme is never guessed and an
    unsupported platform raises :class:`StoreError`.
    """
    if sys.platform == "win32":
        return _protect_secret_dpapi(secret)
    if os.name == "posix":
        protection = _load_secret_protection()
        try:
            return protection.protect_secret_aesgcm(secret)
        except protection.SecretProtectionError as exc:
            raise StoreError(str(exc)) from exc
    raise StoreError("no share secret protection scheme for this platform")


def unprotect_secret(blob: bytes, algo: str | None = None) -> str:
    """Decrypt ``blob`` strictly according to ``algo``.

    Rows always pass their stored ``secret_algo``. ``algo=None`` is accepted
    only for the legacy direct-call form on Windows (DPAPI was the sole scheme
    ever written there); POSIX raises instead of assuming DPAPI. A DPAPI blob on
    POSIX and an unknown algorithm raise explicitly — never an empty string.
    """
    if algo is None:
        if sys.platform != "win32":
            raise StoreError(
                "secret_algo is required to decrypt a share secret on this "
                "platform")
        return _unprotect_secret_dpapi(blob)
    if algo == SECRET_ALGO_DPAPI_USER:
        if sys.platform != "win32":
            raise StoreError(
                "dpapi-user-scope-v1 ciphertext cannot be decrypted on "
                "POSIX; it requires the original Windows user identity")
        return _unprotect_secret_dpapi(blob)
    protection = _load_secret_protection()
    try:
        return protection.unprotect_secret(blob, algo)
    except protection.SecretProtectionError as exc:
        raise StoreError(str(exc)) from exc


def _protect_for_new_row(secret: str):
    """``(blob, algo)`` for a NEW or rotated row.

    Windows preserves the R11 graceful degradation: a protection failure leaves
    the row hash-only and readback answers ``SecretUnavailable``. POSIX fails
    closed: a missing, damaged or loosely-permissioned key file aborts the
    mutation instead of silently writing a hash-only row.
    """
    if sys.platform == "win32":
        try:
            return protect_secret(secret), SECRET_ALGO_DPAPI_USER
        except Exception:  # noqa: BLE001 — R11 Windows-only degradation
            return None, None
    if os.name != "posix":
        raise StoreError("no share secret protection scheme for this platform")
    try:
        blob = protect_secret(secret)
        return blob, _load_secret_protection().SECRET_ALGO_AESGCM_FILE_KEY
    except StoreError:
        raise
    except Exception as exc:  # noqa: BLE001 — POSIX fails closed
        raise StoreError(
            f"share secret protection failed: {exc}") from exc


def _key_columns(conn):
    """Column names of the keys table; a missing table is a StoreError.

    A legacy store predates the ``enabled`` column: this read must keep working
    against it instead of requiring a schema migration it may not perform.
    """
    rows = conn.execute("PRAGMA table_info(keys)").fetchall()
    if not rows:
        raise sqlite3.OperationalError("no such table: keys")
    return {row[1] for row in rows}


def read_key_labels(path, key_ids):
    """Read only requested public labels, without schema setup or recovery.

    A missing/damaged store raises StoreError; callers must retain unknown
    names instead of creating a database or treating failure as an empty store.
    mode=ro and the narrow SELECT prevent credentials/mutations from entering
    this read path. BEGIN keeps chunked reads in one consistent snapshot.
    """
    wanted = sorted({key_id for key_id in key_ids
                     if isinstance(key_id, str) and key_id})
    if not wanted:
        return {}
    conn = None
    try:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        # ``enabled`` only exists after the revoke-as-enabled=0 migration; a
        # legacy store keeps the deleted_at-only reading.
        columns = _key_columns(conn)
        enabled_column = "enabled," if "enabled" in columns else ""
        labels = {}
        for offset in range(0, len(wanted), 400):
            batch = wanted[offset:offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                "SELECT key_id,name," + enabled_column
                + "deleted_at FROM keys WHERE key_id IN ("
                + placeholders + ")", batch)
            for row in rows:
                key_id, name, deleted_at = row[0], row[1], row[-1]
                enabled = row[2] if enabled_column else None
                # 存量 enabled=0 的行（迁移前的旧撤销语义）与新撤销同样按
                # 「已撤销」归因：请求明细要显示「（已删除）」后缀，所以
                # deleted 不能只看 deleted_at。
                labels[key_id] = {
                    "key_id": key_id,
                    "name": name,
                    "deleted_at": deleted_at,
                    # SQLite stores the column as INTEGER: ``enabled == 0``
                    # (not ``is False``) is the revoked fact.
                    "deleted": deleted_at is not None or enabled == 0,
                }
        return labels
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise StoreError("share key labels unavailable") from exc
    finally:
        if conn is not None:
            conn.close()


class ShareStore:
    """Thin wrapper around a transaction-protected SQLite file."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = None
        try:
            self._conn = sqlite3.connect(str(self.path), timeout=30,
                                         check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._init()
        except sqlite3.Error as exc:
            self.close()
            raise StoreError(f"cannot open share store: {exc}") from exc
        except StoreError:
            # Unsupported schema_version and other init refusals fail closed
            # AND must not leak the open connection (the file handle would
            # otherwise keep the database locked, e.g. on Windows).
            self.close()
            raise

    def close(self):
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ ddl
    def _init(self):
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS keys (
                    key_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    secret_hash TEXT NOT NULL,
                    secret_version INTEGER NOT NULL DEFAULT 1,
                    masked_key TEXT NOT NULL,
                    secret_blob BLOB,
                    secret_algo TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    max_concurrency INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT,
                    row_version INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(name, deleted_at)
                );
                CREATE TABLE IF NOT EXISTS ops (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    idempotency_key TEXT,
                    status TEXT NOT NULL,
                    payload TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS configgen (
                    schema_version INTEGER NOT NULL,
                    config_generation INTEGER NOT NULL,
                    applied_generation INTEGER NOT NULL DEFAULT 0
                );
                -- Cross-machine security journal (S2 recovery fence): a
                -- revoke/rotate intent is written in the SAME transaction as
                -- the local key change, BEFORE that change.  The row survives
                -- restart and is copied into the independent security state and
                -- the encrypted backup; a pending intent is conservative.
                CREATE TABLE IF NOT EXISTS security (
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
                CREATE INDEX IF NOT EXISTS idx_security_generation
                    ON security(generation);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_keys_active_name
                    ON keys(name) WHERE deleted_at IS NULL;
                """
            )
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                # Fresh store: a new deployment starts at the confirmed total
                # limit of 64 (R6-A2); migrated stores keep the legacy 2.
                self._conn.execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version',?)",
                    (str(SCHEMA_VERSION),))
                self._conn.execute(
                    "INSERT INTO meta(key,value) VALUES('share_max_concurrency',?)",
                    (str(NEW_DEPLOYMENT_SHARE_MAX_CONCURRENCY),))
            else:
                version = int(row["value"])
                if version == SCHEMA_VERSION:
                    pass
                elif version == 2:
                    # v2 -> v3 (Plan-Revision 11): add the reversible
                    # ciphertext columns; existing rows stay NULL (legacy
                    # hash-only contract untouched). Key data is unchanged,
                    # so — like v1 -> v2 — no config generation bump and no
                    # publish intent: the first real mutation publishes v3.
                    self._upgrade_v2_to_v3_conn()
                elif version == 1:
                    # First upgrade path (schema v1 -> v2 -> v3), ONE
                    # transaction (the enclosing ``with self._conn``): add
                    # the max_concurrency column (existing rows default to
                    # the old effective per-key limit 1), write the legacy
                    # global limit 2, then add the ciphertext columns.
                    # VersionMismatch (row-level optimistic lock) and
                    # MigrationConflict (legacy JSON migration) semantics
                    # are untouched.
                    self._upgrade_v1_to_v2_conn()
                    self._upgrade_v2_to_v3_conn()
                else:
                    raise StoreError(
                        f"unsupported schema_version {version} (supported {SCHEMA_VERSION})")
            gen = self._conn.execute("SELECT * FROM configgen").fetchone()
            if gen is None:
                self._conn.execute(
                    "INSERT INTO configgen(schema_version,config_generation,applied_generation)"
                    " VALUES(?,?,0)", (SCHEMA_VERSION, 1))
            self._conn.commit()

    def _upgrade_v1_to_v2_conn(self):
        """v1 -> v2 upgrade INSIDE the caller's transaction.

        Adds keys.max_concurrency (existing rows keep the old effective
        per-key limit 1 via the column default), writes meta
        ``share_max_concurrency`` = the old hardcoded global limit 2 (old
        behavior preserved; adjustable 1..64 afterwards), and sets both the
        meta and configgen schema markers to 2. The VersionMismatch row-lock
        and MigrationConflict legacy-JSON semantics are not touched; no
        config generation bump happens here (key data is unchanged — the
        first real mutation publishes the v2 projection).
        """
        columns = {r[1] for r in self._conn.execute("PRAGMA table_info(keys)")}
        if "max_concurrency" not in columns:
            self._conn.execute(
                "ALTER TABLE keys ADD COLUMN max_concurrency"
                " INTEGER NOT NULL DEFAULT "
                + str(LEGACY_KEY_MAX_CONCURRENCY))
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key,value)"
            " VALUES('share_max_concurrency',?)",
            (str(LEGACY_SHARE_MAX_CONCURRENCY),))
        self._conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION),))
        self._conn.execute("UPDATE configgen SET schema_version=?",
                           (SCHEMA_VERSION,))

    def _upgrade_v2_to_v3_conn(self):
        """v2 -> v3 upgrade INSIDE the caller's transaction.

        Adds ``keys.secret_blob`` (BLOB NULL) and ``keys.secret_algo``
        (TEXT NULL). Existing rows keep NULL — the legacy hash-only
        contract is untouched and readback answers ``SecretUnavailable``
        for them until a manual rotation. Key data (hashes, versions,
        masks) is unchanged, so no config generation bump and no publish
        intent; the first real mutation publishes the v3 projection.
        """
        columns = {r[1] for r in self._conn.execute("PRAGMA table_info(keys)")}
        if "secret_blob" not in columns:
            self._conn.execute("ALTER TABLE keys ADD COLUMN secret_blob BLOB")
        if "secret_algo" not in columns:
            self._conn.execute("ALTER TABLE keys ADD COLUMN secret_algo TEXT")
        self._conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION),))
        self._conn.execute("UPDATE configgen SET schema_version=?",
                           (SCHEMA_VERSION,))

    # ------------------------------------------------------------ generations
    def generation(self):
        row = self._conn.execute("SELECT * FROM configgen").fetchone()
        pending_security = self._security_pending_count()
        return {
            "schema_version": row["schema_version"],
            "config_generation": row["config_generation"],
            "applied_generation": row["applied_generation"],
            # S2 security fence: the config generation can be applied while a
            # revoke/rotate intent is still unconfirmed.  ``in_sync`` means
            # FULLY applied (config + security); ``security_in_sync`` states
            # the security dimension explicitly and is never folded away.
            "in_sync": (row["config_generation"] == row["applied_generation"]
                        and pending_security == 0),
            "security_pending": pending_security,
            "security_in_sync": pending_security == 0,
        }

    def commit_applied(self, generation: int, *, instance_id=None,
                       security_generation=None):
        """Share-side acknowledgement: projection of this generation is loaded.

        The runtime ACK also carries its OWN applied revoke watermark.  Only
        revoke/rotate intents at or below ``security_generation`` are marked
        applied by THIS runtime; a missing (None) or 0 watermark marks NO row, so
        the security journal stays pending and is never pretended to be fully
        applied.  A row above the watermark stays pending, so a partially applied
        projection never confirms a newer revocation.  The config applied
        generation still advances: the projection itself was loaded.
        """
        if security_generation is not None:
            if isinstance(security_generation, bool) \
                    or not isinstance(security_generation, int) \
                    or security_generation < 0:
                raise StoreError(
                    "security generation must be a non-negative integer")
        with self._conn:
            row = self._conn.execute("SELECT config_generation FROM configgen").fetchone()
            desired = row["config_generation"]
            if generation > desired:
                raise StoreError("applied generation newer than desired")
            self._conn.execute(
                "UPDATE configgen SET applied_generation=? WHERE applied_generation < ?",
                (generation, generation))
            security_applied = []
            if security_generation is not None and security_generation > 0:
                security_applied = self._mark_security_applied_conn(
                    min(generation, security_generation), instance_id)
            self._conn.commit()
        result = self.generation()
        result["security_applied"] = security_applied
        return result

    # --------------------------------------------------- security journal (S2)
    SECURITY_ACTIONS = ("revoke", "rotate", "delete", "limit_lower")
    SECURITY_STATUSES = ("pending", "applied", "remote_confirmed",
                         "superseded")

    def _security_record_conn(self, key_id, action, generation,
                              secret_version=None, status="pending"):
        """Append ONE recoverable revoke/rotate intent (same transaction).

        The row is written BEFORE the key row is changed, so a crash between the
        intent and the change can never leave an unrecorded revocation.  A pending
        row is conservative: the independent security state and the restore floor
        include it until the runtime ACK or the remote store confirms it.
        """
        if action not in self.SECURITY_ACTIONS:
            raise StoreError(f"unsupported security action {action!r}")
        if isinstance(generation, bool) or not isinstance(generation, int) \
                or generation < 1:
            raise StoreError("security generation must be a positive integer")
        if key_id is not None and (not isinstance(key_id, str) or not key_id):
            raise StoreError("security key_id must be a non-empty string")
        if secret_version is not None and (
                isinstance(secret_version, bool)
                or not isinstance(secret_version, int)
                or secret_version < 1):
            raise StoreError("security secret_version must be positive")
        record_id = str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO security(record_id,key_id,generation,action,"
            " secret_version,status,recorded_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (record_id, key_id, generation, action, secret_version,
             status, _now()))
        return record_id

    def _next_generation_conn(self):
        return self.generation()["config_generation"] + 1

    def security_records(self):
        rows = self._conn.execute(
            "SELECT * FROM security ORDER BY generation, recorded_at, "
            "record_id").fetchall()
        return [self._security_public_row(row) for row in rows]

    def _security_pending_count(self):
        """Unconfirmed revoke/rotate intents (the security fence's own flag)."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM security WHERE status='pending'"
        ).fetchone()
        return int(row["n"])

    def _security_public_row(self, row):
        return {
            "record_id": row["record_id"],
            "key_id": row["key_id"],
            "generation": int(row["generation"]),
            "action": row["action"],
            "secret_version": (None if row["secret_version"] is None
                               else int(row["secret_version"])),
            "status": row["status"],
            "recorded_at": row["recorded_at"],
            "applied_at": row["applied_at"],
            "applied_generation": (
                None if row["applied_generation"] is None
                else int(row["applied_generation"])),
            "instance_id": row["instance_id"],
            "remote_confirmed_at": row["remote_confirmed_at"],
            "remote_receipt": row["remote_receipt"],
        }

    def security_floor(self):
        row = self._conn.execute(
            "SELECT MAX(generation) FROM security").fetchone()
        return 0 if row is None or row[0] is None else int(row[0])

    def security_state(self):
        """Independent, serialisable latest security state.

        ``generation_floor`` is the newest recorded intent INCLUDING pending
        rows: an unconfirmed revoke still fences an older backup.  The state is
        copied to the independent recovery file and into the backup package.
        """
        records = self.security_records()
        floor = max((record["generation"] for record in records), default=0)
        pending_remote = sum(
            1 for record in records
            if record["status"] != "remote_confirmed")
        return {
            "format": "um-share-security-state-v1",
            "generation_floor": floor,
            "records": records,
            "pending_remote": pending_remote,
            "exported_at": _now(),
        }

    def mark_security_remote_confirmed(self, record_id, receipt=None):
        """Bind ONE intent to a remote security record after a real ACK.

        Only the caller that actually published the record to the independent
        object store may call this.  A failed/unconfirmed upload must leave the
        row pending (fail-closed).
        """
        if not isinstance(record_id, str) or not record_id:
            raise StoreError("security record_id must be a non-empty string")
        if receipt is not None and not isinstance(receipt, str):
            raise StoreError("security receipt must be a string")
        with self._conn:
            row = self._conn.execute(
                "SELECT record_id FROM security WHERE record_id=?",
                (record_id,)).fetchone()
            if row is None:
                raise NotFound("security record not found")
            self._conn.execute(
                "UPDATE security SET status='remote_confirmed',"
                " remote_confirmed_at=?, remote_receipt=? "
                "WHERE record_id=?",
                (_now(), receipt, record_id))
        return self._security_record_by_id(record_id)

    def _security_record_by_id(self, record_id):
        row = self._conn.execute(
            "SELECT * FROM security WHERE record_id=?",
            (record_id,)).fetchone()
        return None if row is None else self._security_public_row(row)

    def _mark_security_applied_conn(self, generation, instance_id=None):
        """Bind a runtime ACK to the journal rows it confirms.

        ``generation`` is the projection generation the share ACKed.  Every
        pending intent at or below it is now known to be applied by the running
        share; the row keeps its generation so the independent floor is never
        lowered.  Rows above the ACK stay pending.
        """
        rows = self._conn.execute(
            "SELECT record_id FROM security WHERE generation<=?"
            " AND status IN ('pending','applied')"
            " ORDER BY generation, recorded_at",
            (generation,)).fetchall()
        record_ids = [row["record_id"] for row in rows]
        if record_ids:
            self._conn.execute(
                "UPDATE security SET status='applied', applied_at=?,"
                " applied_generation=?, instance_id=? "
                "WHERE generation<=? AND status IN ('pending','applied')",
                (_now(), generation, instance_id, generation))
        return record_ids

    def apply_security_fence(self, state):
        """Apply an independent latest security state to this store.

        A recovery restores an OLD backup, then replays the newer security
        journal: every revoke/rotate intent newer than the restored generation
        disables its Key (a rotate also invalidates the old secret version),
        the desired generation is raised to the journal floor and a revoking
        publish intent is recorded.  An older re-enable can therefore never
        resurrect a Key revoked after the backup was taken.

        A replayed ``revoke`` keeps the journal's historical shape: it only
        disables the Key, like ``rotate``/``limit_lower``.  The ciphertext is
        not cleared here - a migration must not rewrite legacy rows; a live
        revoke (``update_key(enabled=False)``) is the only path that writes the
        tombstone and clears the secret.
        """
        state = validate_security_state(state)
        floor = state["generation_floor"]
        current = self.generation()["config_generation"]
        applied, disabled, skipped = [], [], []

        def _do():
            nonlocal current
            for record in sorted(state["records"],
                                  key=lambda item: (item["generation"],
                                                    item["recorded_at"])):
                generation = record["generation"]
                if generation < current:
                    # A newer local generation may already contain a later
                    # re-enable; an older record must not undo it.
                    skipped.append(record["record_id"])
                    continue
                key_id = record["key_id"]
                if key_id is not None:
                    row = self._conn.execute(
                        "SELECT * FROM keys WHERE key_id=?",
                        (key_id,)).fetchone()
                    if row is None:
                        skipped.append(record["record_id"])
                        continue
                    # 迁移重放的是历史意图（旧语义只置 enabled=0）：spec 要求
                    # 「迁移时不清 secret_blob，保留现状，只让列表过滤掉」，
                    # 所以 delete 与 revoke 在这里同一条路——只禁用，不写
                    # deleted_at、不清密文。新撤销才写 deleted_at 并清密文。
                    self._conn.execute(
                        "UPDATE keys SET enabled=0, updated_at=?,"
                        " row_version=row_version+1 WHERE key_id=?",
                        (_now(), key_id))
                    if key_id not in disabled:
                        disabled.append(key_id)
                self._conn.execute(
                    "UPDATE security SET status='applied', applied_at=?,"
                    " applied_generation=? WHERE record_id=?"
                    " AND status IN ('pending','applied')",
                    (_now(), generation, record["record_id"]))
                applied.append(record["record_id"])
            if floor > current:
                self._conn.execute(
                    "UPDATE configgen SET config_generation=?", (floor,))
                current = floor
                self._record_publish_intent_conn(
                    revoking=True, action_kind="security_fence")
            elif applied:
                self._record_publish_intent_conn(
                    revoking=True, action_kind="security_fence")

        self._run_immediate(_do)
        return {
            "generation_floor": floor,
            "config_generation": self.generation()["config_generation"],
            "applied": applied,
            "disabled_keys": disabled,
            "skipped": skipped,
        }

    # ------------------------------------------------------------- key ops
    def _run_immediate(self, fn):
        """Serialize mutations with a single BEGIN IMMEDIATE transaction.

        Every idempotency check, key change, generation bump and op record must
        live in one transaction; otherwise concurrent callers with the same
        idempotency key could both create identities and each see a plaintext.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn()
            self._conn.commit()
            return result
        except BaseException:
            self._conn.rollback()
            raise

    def _bump_desired_conn(self):
        self._conn.execute(
            "UPDATE configgen SET config_generation = config_generation + 1")

    def _record_publish_intent_conn(self, revoking, action_kind, key_id=None):
        """Record the publish intent INSIDE the mutation transaction (R4-a.1).

        The key change and its publication intent share one BEGIN IMMEDIATE
        transaction, so a crash between the change and the publication can
        never leave an unrecorded intent: restart reconciliation either finds
        the pending publish op or — if desired > applied with NO pending op —
        synthesizes a fail-closed blocked record instead of assuming safety.
        """
        generation = self.generation()["config_generation"]
        self._conn.execute(
            "INSERT INTO ops(id,kind,idempotency_key,status,payload,created_at)"
            " VALUES(?,'publish',NULL,'pending',?,?)",
            (str(uuid.uuid4()),
             json.dumps({"target_generation": generation,
                         "revoking": bool(revoking),
                         "action_kind": action_kind, "key_id": key_id},
                        ensure_ascii=False),
             _now()))

    def find_pending_publish_intent(self, generation):
        """Newest pending publish intent for this generation, or None."""
        for op in self.operations(kind="publish", status="pending"):
            payload = op["payload"]
            target = payload.get("target_generation")
            if isinstance(target, int) and target == generation:
                return op
        return None

    def _validate_key_max_concurrency(self, value):
        """Per-key bound: 1..48 (D18-4; a single key never reserves the
        global budget)."""
        if isinstance(value, bool) or not isinstance(value, int) \
                or not (KEY_MAX_CONCURRENCY_MIN <= value
                        <= KEY_MAX_CONCURRENCY_MAX):
            raise StoreError(
                f"max_concurrency must be an integer in "
                f"{KEY_MAX_CONCURRENCY_MIN}..{KEY_MAX_CONCURRENCY_MAX}")

    def _validate_share_max_concurrency(self, value):
        """GLOBAL bound: 1..64 across every key and entry point (D18-4)."""
        if isinstance(value, bool) or not isinstance(value, int) \
                or not (SHARE_MAX_CONCURRENCY_MIN <= value
                        <= SHARE_MAX_CONCURRENCY_MAX):
            raise StoreError(
                f"share_max_concurrency must be an integer in "
                f"{SHARE_MAX_CONCURRENCY_MIN}..{SHARE_MAX_CONCURRENCY_MAX}")

    def get_share_max_concurrency(self):
        """Global share concurrency limit (meta ``share_max_concurrency``)."""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='share_max_concurrency'"
        ).fetchone()
        if row is None:
            # Only reachable for a store whose meta row is missing (pre-v2
            # reader against an un-migrated file): fall back to the legacy
            # global hardcode, never to an invented limit.
            return LEGACY_SHARE_MAX_CONCURRENCY
        try:
            return int(row["value"])
        except (TypeError, ValueError) as exc:
            raise StoreError(
                f"invalid stored share_max_concurrency {row['value']!r}") from exc

    def set_share_max_concurrency(self, value, expected=None,
                                  idempotency_key=None):
        """Update the global share limit with the SAME transaction discipline
        as every other mutation: BEGIN IMMEDIATE, the meta write, the
        desired bump and the publish intent commit together.

        ``expected`` is an optimistic gate on the CURRENT stored value: a
        mismatch raises VersionMismatch (mapped to 409 by status_code_for —
        choice documented in the contract notes: reusing the existing
        conflict mechanism instead of a new exception type). Lowering the
        limit is recorded as a REVOKING publish intent (revocation
        direction); raising is the safe direction.
        """
        self._validate_share_max_concurrency(value)
        if expected is not None:
            if isinstance(expected, bool) or not isinstance(expected, int):
                raise StoreError("expected_share_max must be an integer")

        def _do():
            if idempotency_key:
                existing = self._find_any_op_conn(idempotency_key)
                if existing is not None:
                    if existing["kind"] != "set_share_limits":
                        raise Conflict(
                            "idempotency key already used for a different "
                            "operation kind")
                    return self._replay_share_limit_op(existing)
            current = self.get_share_max_concurrency()
            if expected is not None and expected != current:
                raise VersionMismatch(
                    f"share_max_concurrency conflict: expected {expected}, "
                    f"current {current}")
            if value == current:
                return {"share_max_concurrency": current, "previous": current,
                        "changed": False, "replayed": False,
                        "config_generation":
                            self.generation()["config_generation"]}
            if value < current:
                self._security_record_conn(
                    None, "limit_lower", self._next_generation_conn())
            self._conn.execute(
                "UPDATE meta SET value=? WHERE key='share_max_concurrency'",
                (str(value),))
            self._bump_desired_conn()
            self._record_publish_intent_conn(
                revoking=value < current, action_kind="set_share_limits",
                key_id=None)
            if idempotency_key:
                self._record_op_conn("set_share_limits", idempotency_key,
                                     {"share_max_concurrency": value}, "ok")
            return {"share_max_concurrency": value, "previous": current,
                    "changed": True, "replayed": False,
                    "config_generation": self.generation()["config_generation"]}

        return self._run_immediate(_do)

    def _replay_share_limit_op(self, row):
        # Idempotent settings replay: no new mutation, no re-publish, and
        # nothing secret exists here to withhold (kept out of the plaintext
        # replay path of create/rotate).
        payload = json.loads(row["payload"] or "{}")
        return {"share_max_concurrency": payload.get("share_max_concurrency"),
                "replayed": True, "changed": False,
                "config_generation": self.generation()["config_generation"]}

    def _insert_key_conn(self, name, secret, max_concurrency):
        key_id = str(uuid.uuid4())
        now = _now()
        blob, algo = _protect_for_new_row(secret)
        try:
            self._conn.execute(
                "INSERT INTO keys(key_id,name,secret_hash,secret_version,masked_key,"
                "secret_blob,secret_algo,enabled,max_concurrency,created_at,updated_at,"
                "deleted_at,row_version)"
                " VALUES(?,?,?,1,?,?,?,1,?,?,?,NULL,1)",
                (key_id, name, _hash(secret), _mask(secret),
                 sqlite3.Binary(blob) if blob is not None else None,
                 algo, max_concurrency, now, now))
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"name '{name}' already in use as an active identity") from exc
        self._bump_desired_conn()
        return key_id, blob is not None

    def _find_op_conn(self, kind, idem):
        return self._conn.execute(
            "SELECT * FROM ops WHERE idempotency_key=? AND kind=?",
            (idem, kind)).fetchone()

    def _find_any_op_conn(self, idem):
        return self._conn.execute(
            "SELECT * FROM ops WHERE idempotency_key=? LIMIT 1",
            (idem,)).fetchone()

    def _record_op_conn(self, kind, idem, payload, status):
        if idem is None:
            return
        self._conn.execute(
            "INSERT OR IGNORE INTO ops(id,kind,idempotency_key,status,payload,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()), kind, idem, status,
             json.dumps(payload, ensure_ascii=False), _now()))

    def create_key(self, name, secret=None, idempotency_key=None,
                   max_concurrency=NEW_KEY_DEFAULT_MAX_CONCURRENCY):
        """Create a key identity.

        ``max_concurrency`` defaults to the D18-4 new-Key default 16 (a
        migrated v1 row keeps the legacy 1); validation is 1..48. The
        plaintext is returned once here and the idempotency replay semantics
        are unchanged (a replayed create never returns plaintext again).
        """
        if not name or not isinstance(name, str) or len(name) > 64:
            raise StoreError("invalid name (1..64 chars)")
        if max_concurrency is None:
            max_concurrency = NEW_KEY_DEFAULT_MAX_CONCURRENCY
        self._validate_key_max_concurrency(max_concurrency)
        secret = secret or ("um-share-" + secrets.token_urlsafe(32))

        def _do():
            if idempotency_key:
                existing = self._find_any_op_conn(idempotency_key)
                if existing is not None:
                    if existing["kind"] != "create":
                        raise Conflict(
                            "idempotency key already used for a different operation kind")
                    return self._replay_op(existing)
            key_id, viewable = self._insert_key_conn(name, secret, max_concurrency)
            self._record_publish_intent_conn(
                revoking=False, action_kind="create", key_id=key_id)
            if idempotency_key:
                self._record_op_conn("create", idempotency_key,
                                     {"key_id": key_id, "name": name}, "ok")
            return {"key_id": key_id, "name": name, "secret": secret,
                    "mk": _mask(secret),
                    "plaintext_viewable": viewable,
                    "config_generation": self.generation()["config_generation"]}

        return self._run_immediate(_do)

    def _require_key(self, key_id):
        row = self._conn.execute(
            "SELECT * FROM keys WHERE key_id=?", (key_id,)).fetchone()
        if row is None:
            raise NotFound("key identity not found")
        return row

    def get_key(self, key_id):
        row = self._require_key(key_id)
        return self._public_row(row)

    def _public_row(self, row):
        """Public key metadata (never secret_hash, never secret_blob)."""
        return {
            "key_id": row["key_id"],
            "name": row["name"],
            "secret_version": row["secret_version"],
            "masked_key": row["masked_key"],
            "plaintext_viewable": row["secret_blob"] is not None,
            "enabled": bool(row["enabled"]),
            "max_concurrency": row["max_concurrency"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "row_version": row["row_version"],
            "deleted_at": row["deleted_at"] if row["deleted_at"] is not None else None,
        }

    def reveal_key_secret(self, key_id, expected_row_version=None):
        """Narrow per-key plaintext readback (Plan-Revision 11 contract).

        On Windows the ciphertext decrypts only under the same machine and the
        same runtime user identity the 8001 console host runs as; on POSIX it
        decrypts only with the configured key file. The decrypted material must
        match the stored SHA-256 digest or the answer fails closed, and the
        stored ``secret_algo`` selects the scheme — a blob without a marker is
        never guessed at. Outcomes are distinct: legacy/protection-less rows
        raise :class:`SecretUnavailable`; undecryptable material (or a digest
        mismatch, or a scheme that cannot be used on this platform, e.g. a DPAPI
        row on POSIX) raises :class:`SecretUndecryptable`; deleted identities
        raise :class:`Disabled` (their ciphertext was cleared at delete time);
        unknown ids raise :class:`NotFound`; an ``expected_row_version`` that no
        longer matches the row raises :class:`VersionMismatch` (the caller's view
        is stale — it must not read a plaintext belonging to a version it never
        saw). This method is the ONLY plaintext-returning read besides the
        once-only create/rotate replies, and it never logs."""
        row = self._require_key(key_id)
        if row["deleted_at"] is not None:
            raise Disabled("deleted identity has no readable secret")
        if expected_row_version is not None:
            if isinstance(expected_row_version, bool) \
                    or not isinstance(expected_row_version, int) \
                    or expected_row_version < 1:
                raise StoreError(
                    "expected_row_version must be a positive integer")
            if expected_row_version != row["row_version"]:
                raise VersionMismatch(
                    f"row_version conflict: expected {expected_row_version}, "
                    f"current {row['row_version']}")
        blob = row["secret_blob"]
        if not blob:
            raise SecretUnavailable(
                "legacy or unprotected row stores no ciphertext; rotate to "
                "enter the reviewable contract")
        algo = row["secret_algo"]
        if not algo:
            raise SecretUndecryptable(
                "stored ciphertext has no algorithm marker; refusing to "
                "guess a scheme")
        try:
            secret = unprotect_secret(bytes(blob), algo)
        except Exception as exc:  # noqa: BLE001 — a failed unprotection
            # is a distinct, honest outcome: the key itself stays valid
            # (hash verification is untouched), only the plaintext is
            # unreachable.
            raise SecretUndecryptable(
                "stored ciphertext cannot be decrypted under this runtime "
                "identity") from exc
        if not _secret_eq(secret, row["secret_hash"]):
            raise SecretUndecryptable(
                "decrypted secret does not match the stored digest")
        return {"key_id": key_id, "name": row["name"],
                "masked_key": row["masked_key"],
                "secret_version": row["secret_version"],
                "secret": secret}

    def list_keys(self, include_deleted=False):
        """List key metadata.

        The default list is the ACTIVE identities only: a revoked identity is a
        tombstone (``deleted_at`` set) and disappears, exactly like a deleted
        one.  Legacy ``enabled=0`` rows are treated as revoked by the same
        filter without rewriting their ciphertext.  ``include_deleted=True``
        keeps every row for accounting.
        """
        sql = "SELECT * FROM keys"
        if not include_deleted:
            sql += " WHERE enabled=1 AND deleted_at IS NULL"
        rows = self._conn.execute(sql + " ORDER BY created_at").fetchall()
        return [self._public_row(r) for r in rows]

    def _resolve_expected_version(self, row, expected):
        """Validate/coerce the optimistic-concurrency gate for a mutation."""
        current = row["row_version"]
        if expected is None:
            return current
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
            raise StoreError("expected_row_version must be a positive integer")
        if expected != current:
            raise VersionMismatch(
                f"row_version conflict: expected {expected}, current {current}")
        return current

    def _update_checked(self, key_id, expected, set_sql, params):
        """Run UPDATE gated on row_version; 0 rows => VersionMismatch."""
        cursor = self._conn.execute(
            f"UPDATE keys SET {set_sql} WHERE key_id=? AND row_version=?",
            tuple(params) + (key_id, expected))
        if cursor.rowcount == 0:
            raise VersionMismatch("row_version conflict")

    def rename_key(self, key_id, new_name, expected_row_version=None):
        if not new_name or not isinstance(new_name, str) or len(new_name) > 64:
            raise StoreError("invalid new name")

        def _do():
            row = self._require_key(key_id)
            if row["deleted_at"] is not None:
                raise Disabled("deleted identity cannot be renamed")
            expected = self._resolve_expected_version(row, expected_row_version)
            if row["name"] == new_name:
                return self.get_key(key_id)
            try:
                self._update_checked(key_id, expected,
                                     "name=?, updated_at=?, row_version=row_version+1",
                                     [new_name, _now()])
            except sqlite3.IntegrityError as exc:
                raise Conflict("display name already in use") from exc
            self._bump_desired_conn()
            self._record_publish_intent_conn(
                revoking=False, action_kind="rename", key_id=key_id)
            return self.get_key(key_id)

        return self._run_immediate(_do)

    def update_key(self, key_id, new_name=None, enabled=None,
                   max_concurrency=None, expected_row_version=None):
        """Atomic PATCH: rename, DISABLE and/or change the per-key
        concurrency limit in one desired bump.

        D18-3: ``enabled`` is a READ-ONLY fact — only ``False`` (the
        revoke direction) is accepted. There is no public re-enable path:
        ``enabled=True`` raises instead of silently reviving a disabled or
        revoked identity. ``revoke_and_terminate_key`` uses the ``False``
        direction internally.

        Revoke = TOMBSTONE + ciphertext clear: ``enabled=False`` writes
        ``deleted_at`` and clears ``secret_blob``/``secret_algo``, the same
        shape as ``delete_key``.  The row leaves the default list and its
        plaintext can no longer be revealed (410).  This also applies to a
        legacy ``enabled=0`` row when it is explicitly revoked; the migration
        path itself never rewrites those rows' ciphertext.

        LOWERING ``max_concurrency`` is a REVOCATION-direction change: it is
        recorded as a revoking publish intent and the management layer
        fail-closes (``_require_applied``) exactly like ``enabled=False``.
        RAISING is the safe direction (best-effort publish). The sliding
        20/min limit is unrelated to this column and untouched.
        """
        if new_name is not None and (not isinstance(new_name, str)
                                     or not new_name or len(new_name) > 64):
            raise StoreError("invalid new name")
        if enabled is True:
            raise StoreError(
                "enabled is not writable: a disabled or revoked identity "
                "cannot be re-enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise StoreError("enabled must be bool")
        if max_concurrency is not None:
            self._validate_key_max_concurrency(max_concurrency)
        if new_name is None and enabled is None and max_concurrency is None:
            raise StoreError("nothing to update")

        def _do():
            row = self._require_key(key_id)
            if row["deleted_at"] is not None:
                raise Disabled("deleted identity cannot be updated")
            expected = self._resolve_expected_version(row, expected_row_version)
            columns, params = [], []
            revoking = False
            if new_name is not None and new_name != row["name"]:
                columns.append("name=?")
                params.append(new_name)
            if enabled is False:
                # Revoke is a tombstone: same write shape as delete_key, so
                # the identity leaves the list and its plaintext is gone.
                columns.extend(["enabled=0", "deleted_at=?",
                                "secret_blob=NULL", "secret_algo=NULL"])
                params.append(_now())
            elif enabled is not None and bool(row["enabled"]) != enabled:
                columns.append("enabled=?")
                params.append(1 if enabled else 0)
            if max_concurrency is not None \
                    and max_concurrency != row["max_concurrency"]:
                columns.append("max_concurrency=?")
                params.append(max_concurrency)
                if max_concurrency < row["max_concurrency"]:
                    revoking = True   # lower limit = revoke direction
            if not columns:
                return self.get_key(key_id)
            columns.append("updated_at=?")
            columns.append("row_version=row_version+1")
            params.append(_now())
            if enabled is False or revoking:
                self._security_record_conn(
                    key_id, "limit_lower" if revoking and enabled is not False
                    else "revoke", self._next_generation_conn(),
                    secret_version=row["secret_version"])
            try:
                self._update_checked(key_id, expected,
                                     ", ".join(columns), params)
            except sqlite3.IntegrityError as exc:
                raise Conflict("display name already in use") from exc
            self._bump_desired_conn()
            self._record_publish_intent_conn(
                revoking=revoking or enabled is False,
                action_kind="update", key_id=key_id)
            return self.get_key(key_id)

        return self._run_immediate(_do)

    def rotate_key(self, key_id, idempotency_key=None, expected_row_version=None):
        def _do():
            if idempotency_key:
                existing = self._find_any_op_conn(idempotency_key)
                if existing is not None:
                    if existing["kind"] != "rotate":
                        raise Conflict(
                            "idempotency key already used for a different operation kind")
                    # R2-target: the idempotency key is bound to its TARGET
                    # identity — another key's old rotation success must
                    # never stand in for this rotation (B would silently keep
                    # its old secret while the API reports success).
                    prior = json.loads(existing["payload"] or "{}")
                    if prior.get("key_id") != key_id:
                        raise Conflict(
                            "idempotency key already used to rotate a "
                            "different key")
                    return self._replay_op(existing)
            row = self._require_key(key_id)
            if row["deleted_at"] is not None:
                raise Disabled("deleted identity cannot be rotated")
            expected = self._resolve_expected_version(row, expected_row_version)
            secret = "um-share-" + secrets.token_urlsafe(32)
            blob, algo = _protect_for_new_row(secret)
            new_version = row["secret_version"] + 1
            self._security_record_conn(
                key_id, "rotate", self._next_generation_conn(),
                secret_version=new_version)
            self._update_checked(
                key_id, expected,
                "secret_hash=?, secret_version=?, masked_key=?, secret_blob=?,"
                " secret_algo=?, updated_at=?, row_version=row_version+1",
                [_hash(secret), new_version, _mask(secret),
                 sqlite3.Binary(blob) if blob is not None else None,
                 algo, _now()])
            self._bump_desired_conn()
            self._record_publish_intent_conn(
                revoking=True, action_kind="rotate", key_id=key_id)
            if idempotency_key:
                self._record_op_conn("rotate", idempotency_key,
                                     {"key_id": key_id,
                                      "secret_version": new_version}, "ok")
            return {"key_id": key_id, "secret": secret, "mk": _mask(secret),
                    "secret_version": new_version,
                    "plaintext_viewable": blob is not None,
                    "config_generation": self.generation()["config_generation"]}

        return self._run_immediate(_do)

    def delete_key(self, key_id, expected_row_version=None):
        def _do():
            row = self._require_key(key_id)
            expected = self._resolve_expected_version(row, expected_row_version)
            if row["deleted_at"] is not None:
                return {"key_id": key_id, "deleted_at": row["deleted_at"],
                        "already_deleted": True}
            now = _now()
            self._security_record_conn(
                key_id, "delete", self._next_generation_conn(),
                secret_version=row["secret_version"])
            self._update_checked(
                key_id, expected,
                "enabled=0, deleted_at=?, secret_blob=NULL, secret_algo=NULL,"
                " updated_at=?, row_version=row_version+1",
                [now, now])
            self._bump_desired_conn()
            self._record_publish_intent_conn(
                revoking=True, action_kind="delete", key_id=key_id)
            return {"key_id": key_id, "deleted_at": now}

        return self._run_immediate(_do)

    def backup(self, target_path):
        """Consistent SQLite backup (online backup API) for the D3 migration
        flow. Since R11-B1 the copy ALSO carries the protected ciphertext
        blobs (never plaintext) — DPAPI blobs decrypt only under the same
        Windows machine + runtime identity, and AES-GCM blobs need the same
        key file, so restoring a backup without them leaves the ciphertext
        unreadable (SecretUndecryptable), never silently hash-only.
        Restoring a backup must never overwrite post-migration revocations;
        the restore path stays offline and reviewed (D3 contract)."""
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(str(target))
        try:
            self._conn.backup(dest)
            dest.commit()
        finally:
            dest.close()
        return target

    def verify_secret(self, key_id, secret):
        row = self._require_key(key_id)
        if row["deleted_at"] is not None or not row["enabled"]:
            return False
        return _secret_eq(secret, row["secret_hash"])

    # -------------------------------------------------------------- ops/IDP
    def _replay_op(self, row):
        # Replayed create/rotate: never return a plaintext secret again.
        payload = json.loads(row["payload"] or "{}")
        out = {"key_id": payload.get("key_id"), "secret_version": payload.get(
            "secret_version"),
               "secret_unavailable": True, "replayed": True,
               "config_generation": self.generation()["config_generation"]}
        return out

    def record_operation(self, kind, status, payload, idempotency_key=None):
        """Persist a management operation (create/rotate/start/stop/...).

        Reuses the single ops table so operation ids survive restart and the
        management API can resolve GET /api/share/operations/{id}. An
        idempotency key maps to exactly ONE operation: the key is claimed and
        the row inserted in the same transaction, so a concurrent caller with
        the same key gets the winner's id back instead of a second row.

        Returns ``(operation_id, created)``: ``created`` is False when the
        caller REUSED an existing operation (its own key claim or a concurrent
        winner) — the caller must then never launch its own executor for it
        (R6-a: one idempotency key drives at most one execution).
        """
        op_id = str(uuid.uuid4())
        payload_json = json.dumps(payload, ensure_ascii=False)
        now = _now()
        try:
            with self._conn:
                if idempotency_key:
                    row = self._conn.execute(
                        "SELECT id, kind FROM ops WHERE idempotency_key=?",
                        (idempotency_key,)).fetchone()
                    if row is not None:
                        # R6-a.2: an idempotency key is bound to ONE action
                        # kind — replaying it for a different action must
                        # return 409, never a foreign success.
                        if row["kind"] != kind:
                            raise Conflict(
                                "idempotency key already used for a different "
                                "operation kind")
                        return row["id"], False
                self._conn.execute(
                    "INSERT INTO ops(id,kind,idempotency_key,status,payload,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (op_id, kind, idempotency_key, status, payload_json, now))
        except sqlite3.IntegrityError:
            # A concurrent caller claimed the same idempotency key first:
            # replay the winner's operation id (bound to the SAME kind),
            # never a second row and never a foreign action's success.
            if idempotency_key:
                row = self._conn.execute(
                    "SELECT id, kind FROM ops WHERE idempotency_key=?",
                    (idempotency_key,)).fetchone()
                if row is not None:
                    if row["kind"] != kind:
                        raise Conflict(
                            "idempotency key already used for a different "
                            "operation kind")
                    return row["id"], False
            raise
        return op_id, True

    def update_operation(self, operation_id, status, payload_merge=None):
        """Advance an operation's status, merging payload keys (R4-a/R6-a)."""
        with self._conn:
            row = self._conn.execute(
                "SELECT payload FROM ops WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                return False
            payload = json.loads(row["payload"] or "{}")
            if payload_merge:
                payload.update(payload_merge)
            self._conn.execute(
                "UPDATE ops SET status=?, payload=? WHERE id=?",
                (status, json.dumps(payload, ensure_ascii=False), operation_id))
        return True

    def claim_active_operation(self, kind, payload):
        """Atomically claim the SINGLE active operation of this kind (R6-a.3).

        Reuse-or-insert happens inside one BEGIN IMMEDIATE transaction, so
        two concurrent keyless requests for the same kind can never both
        become "newest" and both start an executor: the second caller sees
        the first caller's still-active operation and reuses it. Returns
        ``(operation_id, created)``; ``created=False`` means REUSE — the
        caller must not launch its own executor.
        """
        op_id = str(uuid.uuid4())
        payload_json = json.dumps(payload, ensure_ascii=False)
        now = _now()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT id FROM ops WHERE kind=?"
                " AND status IN ('pending', 'running')"
                " ORDER BY created_at DESC LIMIT 1", (kind,)).fetchone()
            if row is not None:
                self._conn.commit()
                return row["id"], False
            self._conn.execute(
                "INSERT INTO ops(id,kind,idempotency_key,status,payload,created_at)"
                " VALUES(?,?,NULL,'pending',?,?)",
                (op_id, kind, payload_json, now))
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return op_id, True

    def operations(self, kind=None, status=None):
        """Operation rows (payload parsed), oldest first."""
        clauses, params = [], []
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        sql = ("SELECT id, kind, idempotency_key, status, payload, created_at"
               " FROM ops")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at"
        return [{"id": r["id"], "kind": r["kind"],
                 "idempotency_key": r["idempotency_key"],
                 "status": r["status"],
                 "payload": json.loads(r["payload"] or "{}"),
                 "created_at": r["created_at"]}
                for r in self._conn.execute(sql, params)]

    def find_operation_by_idempotency_key(self, idempotency_key):
        row = self._conn.execute(
            "SELECT id, kind, status, payload, created_at FROM ops"
            " WHERE idempotency_key=? LIMIT 1", (idempotency_key,)).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "kind": row["kind"], "status": row["status"],
                "payload": json.loads(row["payload"] or "{}"),
                "created_at": row["created_at"]}

    def find_active_operation(self, kind):
        """Newest non-terminal operation of this kind, or None (R6-a)."""
        row = self._conn.execute(
            "SELECT id, kind, status, payload, created_at FROM ops"
            " WHERE kind=? AND status IN ('pending', 'running')"
            " ORDER BY created_at DESC LIMIT 1", (kind,)).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "kind": row["kind"], "status": row["status"],
                "payload": json.loads(row["payload"] or "{}"),
                "created_at": row["created_at"]}

    def get_operation(self, operation_id):
        row = self._conn.execute(
            "SELECT id, kind, status, payload, created_at FROM ops WHERE id=?",
            (operation_id,)).fetchone()
        if row is None:
            raise NotFound("operation not found")
        return {
            "operation_id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "payload": json.loads(row["payload"] or "{}"),
            "created_at": row["created_at"],
        }

    # ----------------------------------------------------------- projection
    def render_projection(self):
        """Minimal auth projection the SHARE side may hold (no mask, no
        plaintext, no secret hashes; entries carry key_id + display name +
        enabled + per-key concurrency + generation — the protocol whitelist
        requires the name, S3-R6A3 R-S3-5).

        Schema v2: every entry adds ``max_concurrency`` and the top level
        adds ``share_max_concurrency``; ``schema_version`` is 2. Rows, the
        share limit and the generation are read from ONE consistent snapshot
        (a read transaction), so a concurrent mutation can never pair new
        keys with an old generation or old keys with a new one (R5-b).
        """
        started = False
        if not self._conn.in_transaction:
            self._conn.execute("BEGIN")
            started = True
        try:
            rows = self._conn.execute(
                "SELECT key_id, name, secret_hash, secret_version, enabled,"
                " max_concurrency FROM keys"
                " WHERE deleted_at IS NULL ORDER BY created_at").fetchall()
            share_max = self.get_share_max_concurrency()
            gen = self.generation()
        finally:
            if started:
                self._conn.rollback()
        # D18-4: the projection NEVER publishes a value above the effective
        # caps. Existing rows/meta written under the old 64/128 contract are
        # clamped here (read-only); the stored values stay untouched until
        # ``normalize_limits`` drives the explicit limit_lower ACK.
        share_max = min(share_max, SHARE_MAX_CONCURRENCY_MAX)
        return {
            # R11-B1: the projection keeps the SHARE-SIDE schema version
            # (whitelist {1,2}) — the local DB marker (SCHEMA_VERSION) is a
            # separate constant precisely so advancing it never breaks publish.
            "schema_version": PROJECTION_SCHEMA_VERSION,
            "config_generation": gen["config_generation"],
            "share_max_concurrency": share_max,
            "data": [
                {"key_id": r["key_id"], "name": r["name"],
                 "secret_hash": r["secret_hash"],
                 "secret_version": r["secret_version"],
                 "enabled": bool(r["enabled"]),
                 "max_concurrency": min(r["max_concurrency"],
                                        KEY_MAX_CONCURRENCY_MAX)}
                for r in rows
            ],
        }

    # ------------------------------------------------------------ migration
    def migration_preview(self, source_digests: dict, run_copy_digests: dict):
        """Compare the authoritative source (repo data) with a run copy.

        Returns a preview dict. Refuses (MigrationConflict) when a name exists
        in both with differing digests, or when a name exists in the run copy
        but not in the source (e.g. a revoked-but-online identity like friend2
        must NOT be revived by taking the union).
        """
        names = sorted(set(source_digests) | set(run_copy_digests))
        to_create = []
        problems = []
        for name in names:
            if name in source_digests and name in run_copy_digests:
                if source_digests[name] != run_copy_digests[name]:
                    problems.append(f"conflict:{name}")
            elif name in run_copy_digests and name not in source_digests:
                problems.append(f"absent-from-source:{name}")
            elif name in source_digests:
                to_create.append({"name": name, "digest": source_digests[name]})
        if problems:
            raise MigrationConflict(";".join(problems))
        return {
            "source_count": len(source_digests),
            "run_copy_count": len(run_copy_digests),
            "to_create": to_create,
            "problems": [],
        }

    def migrate_apply(self, source_digests: dict, idem_map=None):
        """Idempotent migration of legacy digests -> stable key_id.

        ``idem_map`` (name -> key_id) makes a re-run return the SAME key_id
        (never a new identity). Secrets are the legacy ones already hashed in
        the source; no plaintext is involved. Returns the applied mapping.

        R4-a.1: rows, the desired bump and the publish intent share ONE
        transaction owner (``_run_immediate`` / BEGIN IMMEDIATE); the
        transaction-internal bump never commits on its own, so a failure
        while writing the intent rolls the migration rows AND the generation
        back together. Key, idempotency-mapping and migration-conflict
        contracts are unchanged.
        """
        idem_map = idem_map or {}
        applied = {}

        def _do():
            for name, digest in source_digests.items():
                existing = self._conn.execute(
                    "SELECT key_id, secret_hash FROM keys WHERE name=? AND deleted_at IS NULL",
                    (name,)).fetchone()
                if existing is not None and existing["secret_hash"] != digest:
                    raise MigrationConflict(
                        f"existing active identity for {name} differs from source")
                key_id = existing["key_id"] if existing else idem_map.get(name)
                if key_id is None:
                    key_id = str(uuid.uuid4())
                masked = _mask_of_digest(digest)
                now = _now()
                self._conn.execute(
                    "INSERT OR IGNORE INTO keys(key_id,name,secret_hash,secret_version,"
                    "masked_key,enabled,created_at,updated_at,deleted_at,row_version)"
                    " VALUES(?,?,?,1,?,1,?,?,NULL,1)",
                    (key_id, name, digest, masked, now, now))
                applied[name] = key_id
                idem_map.setdefault(name, key_id)
            self._bump_desired_conn()
            self._record_publish_intent_conn(
                revoking=False, action_kind="migrate", key_id=None)

        self._run_immediate(_do)
        return applied, idem_map


def _mask_of_digest(_digest):
    # Cannot reconstruct a mask from a hash without the plaintext; the plan
    # requires marking legacy entries as "旧密钥" instead of deriving a mask.
    return "legacy-**** (旧密钥)"


def validate_security_state(payload):
    """Strictly validate an independent security state document.

    Used by the recovery fence and the encrypted backup restore.  A malformed
    state is refused instead of being treated as "no revocations": an unreadable
    security record must keep the recovery fail-closed.
    """
    if not isinstance(payload, dict):
        raise StoreError("security state must be an object")
    if payload.get("format") != "um-share-security-state-v1":
        raise StoreError("security state format is not supported")
    floor = payload.get("generation_floor")
    if isinstance(floor, bool) or not isinstance(floor, int) or floor < 0:
        raise StoreError("security state generation_floor is invalid")
    records = payload.get("records")
    if not isinstance(records, list):
        raise StoreError("security state records must be a list")
    clean = []
    seen = set()
    for item in records:
        if not isinstance(item, dict):
            raise StoreError("security state record is not an object")
        record_id = item.get("record_id")
        key_id = item.get("key_id")
        generation = item.get("generation")
        action = item.get("action")
        status = item.get("status")
        secret_version = item.get("secret_version")
        recorded_at = item.get("recorded_at")
        if not isinstance(record_id, str) or not record_id:
            raise StoreError("security record_id is invalid")
        if record_id in seen:
            raise StoreError("security state has a duplicate record_id")
        seen.add(record_id)
        if key_id is not None and (not isinstance(key_id, str) or not key_id):
            raise StoreError("security record key_id is invalid")
        if isinstance(generation, bool) or not isinstance(generation, int) \
                or generation < 1:
            raise StoreError("security record generation is invalid")
        if action not in ShareStore.SECURITY_ACTIONS:
            raise StoreError("security record action is invalid")
        if status not in ShareStore.SECURITY_STATUSES:
            raise StoreError("security record status is invalid")
        if secret_version is not None and (
                isinstance(secret_version, bool)
                or not isinstance(secret_version, int)
                or secret_version < 1):
            raise StoreError("security record secret_version is invalid")
        if not isinstance(recorded_at, str) or not recorded_at:
            raise StoreError("security record recorded_at is invalid")
        if generation > floor:
            raise StoreError(
                "security record generation is newer than generation_floor")
        clean.append({
            "record_id": record_id,
            "key_id": key_id,
            "generation": generation,
            "action": action,
            "secret_version": secret_version,
            "status": status,
            "recorded_at": recorded_at,
            "applied_at": item.get("applied_at"),
            "applied_generation": item.get("applied_generation"),
            "instance_id": item.get("instance_id"),
            "remote_confirmed_at": item.get("remote_confirmed_at"),
            "remote_receipt": item.get("remote_receipt"),
        })
    return {
        "format": "um-share-security-state-v1",
        "generation_floor": floor,
        "records": clean,
        "pending_remote": sum(
            1 for record in clean if record["status"] != "remote_confirmed"),
        "exported_at": payload.get("exported_at"),
    }


# ------------------------------------------------------------------ helpers
def load_digest_config(path):
    """Read the legacy name->digest JSON config (never echo digests)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StoreError(f"cannot read legacy config: {exc}") from exc
    if not isinstance(data, dict):
        raise StoreError("legacy config must be an object")
    clients = data.get("clients", {})
    if not isinstance(clients, dict):
        raise StoreError("legacy config has no clients dict")
    for name, digest in clients.items():
        if not isinstance(name, str) or not isinstance(digest, str) or len(digest) != 64:
            raise StoreError("invalid legacy client entry")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise StoreError("invalid legacy digest") from exc
    return clients
