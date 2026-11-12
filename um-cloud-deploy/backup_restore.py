"""Encrypted, machine-separated backup and restore for the UM share chain.

Scope of this module (P1/A10 drill, no real R2/OSS/Alibaba account involved):

* take a consistent SQLite snapshot through :meth:`ShareStore.backup` (the online
  backup API), so the database copy is never a torn file copy;
* snapshot the share usage ledger only up to the last newline.  A JSONL line that a
  live writer is still appending is excluded and reported as ``partial_bytes``;
* seal the package with AES-256-GCM from :mod:`cryptography`; the 32-byte key is
  read from a separate key file with mode ``0600`` (POSIX is enforced, Windows
  cannot express POSIX mode bits through ``chmod`` and documents that limit);
* write the sealed object into a caller-supplied directory that stands in for the
  remote store, atomically via a temporary file and ``os.replace``;
* verify magic, key fingerprint, GCM tag, package sizes, SHA-256 of both payloads,
  SQLite ``PRAGMA integrity_check`` and the embedded ``configgen`` generation
  before any restore target is written;
* restore to a new directory only (repeated restore refuses to overwrite an existing
  target), then re-open the restored store read-only for verification.

The module deliberately does not know about R2, OSS, credentials or HTTP.  Uploading
the sealed object and fetching it from a real remote store stay outside this file and
are reported as not verified by the P1 local simulation.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import struct
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_RELEASE_ROOT = Path(__file__).resolve().parents[1]
if str(_RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(_RELEASE_ROOT))

MAGIC = b"UMCLDBK1"
FORMAT = "um-share-backup-v1"
KEY_SIZE = 32
HEX_KEY_SIZE = KEY_SIZE * 2
NONCE_SIZE = 12
TAG_SIZE = 16
FINGERPRINT_SIZE = 16
MAX_KEY_FILE_BYTES = 1024
MAX_HEADER_BYTES = 1024 * 1024
MAX_LEDGER_BYTES = 256 * 1024 * 1024
MAX_EXTRA_BYTES = 256 * 1024 * 1024
MAX_BUDGET_BYTES = 64 * 1024 * 1024
MAX_SECURITY_BYTES = 16 * 1024 * 1024
AAD = MAGIC + FORMAT.encode("ascii")
BACKUP_SUFFIX = ".umbak"
SECURITY_FORMAT = "um-share-security-state-v1"
SECURITY_PREFIX = "security/"
BACKUP_PREFIX = "backups/"
# Magpie reference prices are a separate, read-only price source.  The backup
# carries the snapshot AND its status file so a restore never pairs an old ledger
# with a newer price version (or vice versa).  The product module owns the format;
# this module only records its version closure and re-validates it on restore.
MAGPIE_SNAPSHOT_NAME = "magpie-prices.snapshot.json"
MAGPIE_STATUS_NAME = "magpie-prices.status.json"
MAGPIE_SNAPSHOT_FORMAT = "um-magpie-price-snapshot-v1"
MAGPIE_STATUS_FORMAT = "um-magpie-price-status-v1"
MAGPIE_CLOSURE_FORMAT = "um-magpie-price-backup-closure-v1"
PRICE_TABLE_NAME = "prices.json"
# The POSIX share-key encryption key (AES-GCM) is deployment state, not release
# material.  A backup that does not carry it cannot decrypt the store rows on
# another machine, so the deployment closure includes it by name.
SHARE_SECRET_NAME = "share-secret.key"
# The share store rows are AES-GCM encrypted with this file.  It is deployment
# configuration, not release material, so backup/restore must follow the effective
# path instead of inventing the release-relative default.
SHARE_SECRET_KEY_ENV = "CHAT2API_SHARE_SECRET_KEY_FILE"
RECOVERY_KIT_MAGIC = b"UMCLRK1"
RECOVERY_KIT_FORMAT = "um-recovery-kit-v1"
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_SIZE = 16
MIN_PASSPHRASE_CHARS = 12


class BackupError(Exception):
    """Base for predictable backup/restore refusals."""


class BackupKeyError(BackupError):
    """The key file is missing, malformed, or not protected as required."""


class BackupKeyMismatch(BackupError):
    """The backup was sealed with a different key (plaintext fingerprint)."""


class BackupAuthenticationError(BackupError):
    """GCM authentication failed: damaged ciphertext or wrong key material."""


class BackupIntegrityError(BackupError):
    """The decrypted package is structurally invalid or hash-mismatched."""


class BackupGenerationError(BackupError):
    """The snapshot generation does not satisfy the caller's restore floor."""


class RestoreTargetExists(BackupError):
    """Restore refused because the target path already exists."""


class BackupBudgetError(BackupError):
    """The backup does not carry the budget state required for recovery."""


class BackupSecurityError(BackupError):
    """The independent latest security state is missing or unconfirmed."""


class BackupConfigError(BackupError):
    """The backup does not carry the price/config required for restore."""


class BackupMagpieError(BackupError):
    """The Magpie price snapshot/status is missing, corrupt or inconsistent."""


class DeploymentAssemblyError(BackupError):
    """A deployment unit/env is missing the budget/backup wiring it requires."""


class ObjectStoreError(BackupError):
    """The object-store adapter refused or failed an operation."""


class BackupUploadError(ObjectStoreError):
    """A backup object could not be uploaded (it stays in the spool)."""


class RecoveryKitError(BackupError):
    """The recovery kit is missing, malformed or the passphrase is wrong."""


# --------------------------------------------------------------------- key file
def _key_fingerprint(key: bytes) -> bytes:
    return hashlib.sha256(b"um-share-backup-key-v1" + key).digest()[:FINGERPRINT_SIZE]


def _validate_key(key) -> bytes:
    if not isinstance(key, (bytes, bytearray, memoryview)):
        raise BackupKeyError("backup key must be bytes")
    material = bytes(key)
    if len(material) != KEY_SIZE:
        raise BackupKeyError(f"backup key must be {KEY_SIZE} bytes")
    return material


def _decode_key(raw: bytes, target) -> bytes:
    if len(raw) == KEY_SIZE:
        return raw
    text = raw.strip()
    if len(text) == HEX_KEY_SIZE:
        try:
            material = bytes.fromhex(text.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BackupKeyError(
                f"backup key file is not valid hexadecimal: {target}") from exc
        if len(material) != KEY_SIZE:
            raise BackupKeyError(
                f"backup key file must decode to {KEY_SIZE} bytes: {target}")
        return material
    raise BackupKeyError(
        "backup key file must hold 32 raw bytes or 64 hexadecimal "
        f"characters: {target}")


def write_backup_key(path, *, key=None, overwrite=False) -> Path:
    """Create an independent backup key file (32 raw bytes, mode 0600).

    ``overwrite=False`` is the safe default: an existing recovery key is never
    silently replaced by a newly generated one.
    """
    target = Path(path)
    material = secrets.token_bytes(KEY_SIZE) if key is None else _validate_key(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    flags |= getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(target, flags, 0o600)
    except OSError as exc:
        raise BackupKeyError(
            f"backup key file cannot be created (existing or unwritable): "
            f"{target}") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(material)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(target, 0o600)
    except Exception:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise
    return target


def load_backup_key(path) -> bytes:
    """Read and validate the backup key file.

    POSIX requires a regular file owned by the current uid with mode ``0600``
    (checked on the descriptor that is actually read).  Windows has no POSIX mode
    bits; the file is created with ``0600`` on a best-effort basis and the
    deployment must protect it with an ACL.  This platform difference is explicit,
    never silently treated as an equivalent guarantee.
    """
    target = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except OSError as exc:
        raise BackupKeyError(f"backup key file unavailable: {target}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise BackupKeyError(
                f"backup key file is not a regular file: {target}")
        if os.name == "posix":
            if info.st_uid != os.getuid():
                raise BackupKeyError(
                    "backup key file is not owned by the current uid: "
                    f"{target}")
            mode = stat.S_IMODE(info.st_mode)
            if mode != 0o600:
                raise BackupKeyError(
                    f"backup key file must have mode 0600 (found {mode:04o}): "
                    f"{target}")
        chunks, total = [], 0
        while total <= MAX_KEY_FILE_BYTES:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
    except OSError as exc:
        raise BackupKeyError(
            f"backup key file unreadable: {target}") from exc
    finally:
        os.close(fd)
    if len(raw) > MAX_KEY_FILE_BYTES:
        raise BackupKeyError(
            f"backup key file is unexpectedly large: {target}")
    return _decode_key(raw, target)


# ------------------------------------------------------------------ ledger read
def _last_complete_line_offset(raw: bytes) -> int:
    """Return the byte offset just after the last newline (0 if none)."""
    index = raw.rfind(b"\n")
    return 0 if index < 0 else index + 1


def _last_record_time(complete: bytes):
    for line in reversed(complete.split(b"\n")):
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if isinstance(record, dict) and isinstance(record.get("time"), str):
            return record["time"]
        return None
    return None


def snapshot_ledger(path, *, max_bytes=MAX_LEDGER_BYTES) -> dict:
    """Read a usage ledger up to the last complete newline.

    The returned ``data`` never contains a trailing half-written JSONL line.  A
    writer may append after this read; that later data is simply outside the
    snapshot and is reported as ``partial_bytes`` only for the tail observed at
    read time.  The function does not write, truncate or lock the live ledger.
    """
    target = Path(path)
    if not target.exists():
        return {
            "present": False,
            "data": b"",
            "offset": 0,
            "partial_bytes": 0,
            "records": 0,
            "sha256": hashlib.sha256(b"").hexdigest(),
            "last_record_time": None,
        }
    try:
        with target.open("rb") as handle:
            chunks, total = [], 0
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise BackupIntegrityError(
                        f"usage ledger exceeds backup bound ({max_bytes} bytes): "
                        f"{target}")
                chunks.append(chunk)
    except OSError as exc:
        raise BackupError(f"usage ledger unreadable: {target}") from exc
    raw = b"".join(chunks)
    cutoff = _last_complete_line_offset(raw)
    complete = raw[:cutoff]
    partial = raw[cutoff:]
    records = sum(1 for line in complete.split(b"\n") if line.strip())
    return {
        "present": True,
        "data": complete,
        "offset": cutoff,
        "partial_bytes": len(partial),
        "records": records,
        "sha256": hashlib.sha256(complete).hexdigest(),
        "last_record_time": _last_record_time(complete),
    }


# --------------------------------------------------------------------- package
def _utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _json_bytes(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")


def _inspect_sqlite_bytes(db_bytes: bytes) -> dict:
    """Validate a SQLite snapshot in memory and return its generation facts."""
    try:
        conn = sqlite3.connect(":memory:")
        try:
            deserialize = getattr(conn, "deserialize", None)
            if deserialize is None:
                raise sqlite3.Error("sqlite3.Connection.deserialize unavailable")
            deserialize(db_bytes)
            integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
            if integrity != ["ok"]:
                raise BackupIntegrityError(
                    "SQLite snapshot failed integrity_check: "
                    + "; ".join(str(item) for item in integrity[:5]))
            row = conn.execute(
                "SELECT schema_version, config_generation, applied_generation "
                "FROM configgen").fetchone()
            if row is None:
                raise BackupIntegrityError(
                    "SQLite snapshot has no configgen row")
            schema_version, config_generation, applied_generation = row
            key_count = conn.execute(
                "SELECT COUNT(*) FROM keys").fetchone()[0]
        finally:
            conn.close()
    except BackupError:
        raise
    except sqlite3.Error as exc:
        raise BackupIntegrityError(
            f"SQLite snapshot is invalid: {exc}") from exc
    if applied_generation > config_generation:
        raise BackupIntegrityError(
            "SQLite snapshot applied_generation is newer than "
            "config_generation")
    return {
        "schema_version": int(schema_version),
        "config_generation": int(config_generation),
        "applied_generation": int(applied_generation),
        "key_count": int(key_count),
        "integrity_check": "ok",
    }


def _verify_share_secret_decrypts_store(db_bytes: bytes,
                                         secret_bytes: bytes) -> int:
    """Refuse a share key that cannot decrypt the AES-GCM rows it travels with.

    A backup can authenticate (the backup encryption key is right) and still carry
    a share-key file unrelated to the store rows.  Installing that pair would
    replace the database and leave every share Key undecryptable, so this check
    runs BEFORE any restore target is touched.  Only AES-GCM rows are checked;
    legacy DPAPI rows belong to the Windows identity and are not guessed at.  A
    store with no AES-GCM rows is accepted (there is nothing this key can prove).
    """
    material = _decode_key(secret_bytes, "<backup share secret key>")
    protection = _load_share_secret_protection()
    try:
        conn = sqlite3.connect(":memory:")
        try:
            deserialize = getattr(conn, "deserialize", None)
            if deserialize is None:
                raise sqlite3.Error("sqlite3.Connection.deserialize unavailable")
            deserialize(db_bytes)
            rows = conn.execute(
                "SELECT secret_blob, secret_algo, secret_hash FROM keys"
                " WHERE secret_blob IS NOT NULL").fetchall()
        finally:
            conn.close()
    except BackupError:
        raise
    except sqlite3.Error as exc:
        raise BackupIntegrityError(
            f"share secret verification cannot read the SQLite snapshot: "
            f"{exc}") from exc
    checked = 0
    for blob, algo, digest in rows:
        if algo != protection.SECRET_ALGO_AESGCM_FILE_KEY:
            continue
        data = bytes(blob)
        if len(data) < NONCE_SIZE + TAG_SIZE:
            raise BackupKeyError(
                "a stored share secret is truncated; the backup share key "
                "cannot be verified")
        try:
            # The product module owns the algorithm dispatch and the key-file
            # gate; this verifier passes the backup's own material instead of
            # carrying a second decrypt path.
            secret = protection.unprotect_secret(data, algo, key=material)
        except protection.SecretProtectionError as exc:
            raise BackupKeyError(
                "the share secret key cannot decrypt the stored share "
                "keys; refusing a backup/restore that would leave them "
                "unreadable"
            ) from exc
        if digest and not hmac.compare_digest(
                hashlib.sha256(secret.encode("utf-8")).hexdigest(), digest):
            raise BackupKeyError(
                "a stored share secret does not match its digest; the backup "
                "share key cannot be verified")
        checked += 1
    return checked


def _snapshot_sqlite_bytes(path) -> bytes:
    """Online SQLite snapshot of an auxiliary DB (budget guard).

    The source is opened read-only and copied through the SQLite backup API, so
    the bytes are a consistent snapshot, never a torn file copy.
    """
    source_path = Path(path)
    if not source_path.exists():
        raise BackupError(f"SQLite source is missing: {source_path}")
    with tempfile.TemporaryDirectory(prefix="um-sqlite-snap-") as tmp:
        snapshot_path = Path(tmp) / "snapshot.db"
        try:
            source = sqlite3.connect(
                f"file:{source_path}?mode=ro", uri=True, timeout=30)
        except sqlite3.Error as exc:
            raise BackupError(
                f"SQLite source unreadable: {source_path}: {exc}") from exc
        try:
            dest = sqlite3.connect(str(snapshot_path))
            try:
                source.backup(dest)
                dest.commit()
            finally:
                dest.close()
        except sqlite3.Error as exc:
            raise BackupError(
                f"SQLite snapshot failed: {source_path}: {exc}") from exc
        finally:
            source.close()
        try:
            return snapshot_path.read_bytes()
        except OSError as exc:
            raise BackupError(
                f"SQLite snapshot unreadable: {snapshot_path}") from exc


def _inspect_budget_bytes(db_bytes: bytes) -> dict:
    """Read the budget facts from a snapshot WITHOUT modifying it."""
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.row_factory = sqlite3.Row
            deserialize = getattr(conn, "deserialize", None)
            if deserialize is None:
                raise sqlite3.Error("sqlite3.Connection.deserialize unavailable")
            deserialize(db_bytes)
            integrity = [row[0] for row in conn.execute(
                "PRAGMA integrity_check")]
            if integrity != ["ok"]:
                raise BackupIntegrityError(
                    "budget snapshot failed integrity_check")
            meta = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
            counters = conn.execute(
                "SELECT bytes_used,cost_micros_used FROM counters WHERE id=1"
            ).fetchone()
            control = conn.execute(
                "SELECT * FROM control WHERE id=1").fetchone()
        finally:
            conn.close()
    except BackupError:
        raise
    except sqlite3.Error as exc:
        raise BackupIntegrityError(
            f"budget snapshot is invalid: {exc}") from exc
    if counters is None or control is None or meta is None:
        raise BackupIntegrityError("budget snapshot has no counters/control")

    def _control(name, default=None):
        return control[name] if name in control.keys() else default

    return {
        "schema_version": int(meta[0]),
        "bytes_used": int(counters[0]),
        "cost_micros_used": int(counters[1]),
        "paused": bool(int(_control("paused", 0))),
        "pause_reason": _control("pause_reason"),
        "pause_id": _control("pause_id"),
        "pause_source": _control("pause_source"),
        "byte_limit": int(_control("byte_limit", 0)),
        "cost_limit_micros": int(_control("cost_limit_micros", 0)),
        "byte_margin": int(_control("byte_margin", 0)),
        "cost_margin_micros": int(_control("cost_margin_micros", 0)),
        "hard_cost_limit_micros": int(_control(
            "hard_cost_limit_micros", 0)),
        "period_id": _control("period_id"),
        "period_started_at": _control("period_started_at"),
        "billing_stop_confirmed": bool(int(_control(
            "billing_stop_confirmed", 0))),
        "quota_confirmed_at": _control("quota_confirmed_at"),
    }


def _validate_security_state(payload) -> dict:
    if not isinstance(payload, dict):
        raise BackupSecurityError("security state must be an object")
    if payload.get("format") != SECURITY_FORMAT:
        raise BackupSecurityError("security state format is not supported")
    floor = payload.get("generation_floor")
    if isinstance(floor, bool) or not isinstance(floor, int) or floor < 0:
        raise BackupSecurityError("security generation_floor is invalid")
    records = payload.get("records")
    if not isinstance(records, list):
        raise BackupSecurityError("security records must be a list")
    clean = []
    seen = set()
    for item in records:
        if not isinstance(item, dict):
            raise BackupSecurityError("security record is not an object")
        record_id = item.get("record_id")
        key_id = item.get("key_id")
        generation = item.get("generation")
        action = item.get("action")
        status = item.get("status")
        secret_version = item.get("secret_version")
        if not isinstance(record_id, str) or not record_id:
            raise BackupSecurityError("security record_id is invalid")
        if record_id in seen:
            raise BackupSecurityError(
                "security state has a duplicate record_id")
        seen.add(record_id)
        if key_id is not None and (not isinstance(key_id, str) or not key_id):
            raise BackupSecurityError("security key_id is invalid")
        if isinstance(generation, bool) or not isinstance(generation, int) \
                or generation < 1:
            raise BackupSecurityError("security generation is invalid")
        if generation > floor:
            raise BackupSecurityError(
                "security record generation is newer than generation_floor")
        if action not in ("revoke", "rotate", "delete", "limit_lower"):
            raise BackupSecurityError("security action is invalid")
        if status not in ("pending", "applied", "remote_confirmed",
                           "superseded"):
            raise BackupSecurityError("security status is invalid")
        if secret_version is not None and (
                isinstance(secret_version, bool)
                or not isinstance(secret_version, int)
                or secret_version < 1):
            raise BackupSecurityError("security secret_version is invalid")
        recorded_at = item.get("recorded_at")
        if not isinstance(recorded_at, str) or not recorded_at:
            raise BackupSecurityError("security recorded_at is invalid")
        clean.append({
            "record_id": record_id,
            "key_id": key_id,
            "generation": generation,
            "action": action,
            "secret_version": secret_version,
            "status": status,
            "recorded_at": item.get("recorded_at"),
            "applied_at": item.get("applied_at"),
            "applied_generation": item.get("applied_generation"),
            "instance_id": item.get("instance_id"),
            "remote_confirmed_at": item.get("remote_confirmed_at"),
            "remote_receipt": item.get("remote_receipt"),
        })
    return {
        "format": SECURITY_FORMAT,
        "generation_floor": floor,
        "records": clean,
        "pending_remote": sum(
            1 for record in clean if record["status"] != "remote_confirmed"),
        "exported_at": payload.get("exported_at"),
    }


def _security_state_bytes(payload) -> bytes:
    return _json_bytes(_validate_security_state(payload))


def read_security_state(path) -> dict:
    """Read the independent latest security state (fail-closed)."""
    target = Path(path)
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise BackupSecurityError(
            f"security state unreadable: {target}") from exc
    if len(raw) > MAX_SECURITY_BYTES:
        raise BackupSecurityError(
            f"security state is unexpectedly large: {target}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BackupSecurityError(
            f"security state is not valid JSON: {target}") from exc
    return _validate_security_state(payload)


def write_security_state(path, payload) -> Path:
    data = _security_state_bytes(payload)
    target = Path(path)
    _atomic_write(target, data, mode=0o600)
    return target


def _parse_extra_files(metadata, extra_bytes) -> dict:
    """Split the concatenated extra-file payload using the header list."""
    files = metadata.get("extra_files") or []
    if not isinstance(files, list):
        raise BackupIntegrityError("backup extra_files is not a list")
    out = {}
    offset = 0
    for item in files:
        if not isinstance(item, dict):
            raise BackupIntegrityError("backup extra file entry is invalid")
        name = item.get("name")
        size = item.get("size")
        digest = item.get("sha256")
        if (not isinstance(name, str) or not name or "/" in name
                or "\\" in name or name in (".", "..")):
            raise BackupIntegrityError("backup extra file name is invalid")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise BackupIntegrityError("backup extra file size is invalid")
        chunk = extra_bytes[offset:offset + size]
        if len(chunk) != size:
            raise BackupIntegrityError(
                f"backup extra file is truncated: {name}")
        if hashlib.sha256(chunk).hexdigest() != digest:
            raise BackupIntegrityError(
                f"backup extra file SHA-256 mismatch: {name}")
        out[name] = chunk
        offset += size
    if offset != len(extra_bytes):
        raise BackupIntegrityError(
            "backup extra payload has trailing bytes")
    return out


def _write_extra_files(directory, files) -> list:
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, data in files.items():
        path = target_dir / name
        _atomic_write(path, data, mode=0o600)
        written.append(str(path))
    return written


def _load_aead():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise BackupKeyError(
            "AES-GCM backup sealing requires the cryptography package") from exc
    return AESGCM


def _seal(package: bytes, key: bytes) -> bytes:
    AESGCM = _load_aead()
    nonce = os.urandom(NONCE_SIZE)
    sealed = AESGCM(key).encrypt(nonce, package, AAD)
    return MAGIC + _key_fingerprint(key) + nonce + sealed


def _open(data: bytes, key: bytes) -> bytes:
    minimum = len(MAGIC) + FINGERPRINT_SIZE + NONCE_SIZE + TAG_SIZE
    if len(data) < minimum:
        raise BackupIntegrityError("backup object is truncated")
    if data[:len(MAGIC)] != MAGIC:
        raise BackupIntegrityError("backup object magic does not match")
    offset = len(MAGIC)
    fingerprint = data[offset:offset + FINGERPRINT_SIZE]
    if fingerprint != _key_fingerprint(key):
        raise BackupKeyMismatch(
            "backup key fingerprint does not match the supplied key")
    offset += FINGERPRINT_SIZE
    nonce = data[offset:offset + NONCE_SIZE]
    offset += NONCE_SIZE
    AESGCM = _load_aead()
    try:
        from cryptography.exceptions import InvalidTag
    except ImportError as exc:  # pragma: no cover - AESGCM implies it
        raise BackupKeyError(
            "AES-GCM backup sealing requires the cryptography package") from exc
    try:
        return AESGCM(key).decrypt(nonce, data[offset:], AAD)
    except InvalidTag as exc:
        raise BackupAuthenticationError(
            "backup authentication failed (damaged object or wrong key)") from exc


def _build_package(metadata: dict, db_bytes: bytes, ledger_bytes: bytes,
                    budget_bytes: bytes = b"", extra_bytes: bytes = b"",
                    security_bytes: bytes = b"") -> bytes:
    header = _json_bytes(metadata)
    if not header or len(header) > MAX_HEADER_BYTES:
        raise BackupIntegrityError("backup header is empty or too large")
    return (MAGIC + struct.pack(">I", len(header)) + header
            + db_bytes + ledger_bytes + budget_bytes + extra_bytes
            + security_bytes)


def _parse_package(package: bytes) -> tuple[dict, bytes, bytes, bytes,
                                             bytes, bytes]:
    prefix = len(MAGIC) + 4
    if len(package) < prefix:
        raise BackupIntegrityError("backup package is truncated")
    if package[:len(MAGIC)] != MAGIC:
        raise BackupIntegrityError("backup package magic does not match")
    header_len = struct.unpack(">I", package[len(MAGIC):prefix])[0]
    if header_len <= 0 or header_len > MAX_HEADER_BYTES:
        raise BackupIntegrityError(
            f"backup header length is invalid: {header_len}")
    header_end = prefix + header_len
    if header_end > len(package):
        raise BackupIntegrityError("backup package header is truncated")
    try:
        metadata = json.loads(package[prefix:header_end].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BackupIntegrityError("backup header is not valid JSON") from exc
    if not isinstance(metadata, dict) or metadata.get("format") != FORMAT:
        raise BackupIntegrityError("backup header format is not supported")
    try:
        db_size = int(metadata["db_size"])
        ledger_size = int(metadata["ledger_bytes"])
        budget_size = int(metadata.get("budget_size", 0))
        extra_size = int(metadata.get("extra_size", 0))
        security_size = int(metadata.get("security_size", 0))
    except (KeyError, TypeError, ValueError) as exc:
        raise BackupIntegrityError(
            "backup header has no valid payload sizes") from exc
    if (db_size <= 0 or ledger_size < 0 or budget_size < 0
            or extra_size < 0 or security_size < 0):
        raise BackupIntegrityError("backup payload sizes are invalid")
    expected = (header_end + db_size + ledger_size + budget_size
                + extra_size + security_size)
    if len(package) != expected:
        raise BackupIntegrityError(
            f"backup package size mismatch (expected {expected}, "
            f"found {len(package)})")
    offset = header_end
    db_bytes = package[offset:offset + db_size]
    offset += db_size
    ledger_bytes = package[offset:offset + ledger_size]
    offset += ledger_size
    budget_bytes = package[offset:offset + budget_size]
    offset += budget_size
    extra_bytes = package[offset:offset + extra_size]
    offset += extra_size
    security_bytes = package[offset:offset + security_size]
    if hashlib.sha256(db_bytes).hexdigest() != metadata.get("db_sha256"):
        raise BackupIntegrityError("SQLite snapshot SHA-256 mismatch")
    if metadata.get("ledger_present"):
        if hashlib.sha256(ledger_bytes).hexdigest() != metadata.get(
                "ledger_sha256"):
            raise BackupIntegrityError("usage ledger SHA-256 mismatch")
    elif ledger_size:
        raise BackupIntegrityError(
            "backup carries ledger bytes but claims no usage ledger")
    if metadata.get("budget_present"):
        if hashlib.sha256(budget_bytes).hexdigest() != metadata.get(
                "budget_sha256"):
            raise BackupIntegrityError("budget snapshot SHA-256 mismatch")
    elif budget_size:
        raise BackupIntegrityError(
            "backup carries budget bytes but claims no budget snapshot")
    if extra_size:
        if hashlib.sha256(extra_bytes).hexdigest() != metadata.get(
                "extra_sha256"):
            raise BackupIntegrityError("extra config SHA-256 mismatch")
    elif metadata.get("extra_files"):
        raise BackupIntegrityError(
            "backup claims extra files but carries none")
    if metadata.get("security_present"):
        if hashlib.sha256(security_bytes).hexdigest() != metadata.get(
                "security_sha256"):
            raise BackupIntegrityError("security state SHA-256 mismatch")
    elif security_size:
        raise BackupIntegrityError(
            "backup carries security bytes but claims no security state")
    return (metadata, db_bytes, ledger_bytes, budget_bytes, extra_bytes,
            security_bytes)


def _atomic_write(path: Path, data: bytes, *, mode=0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp_name, mode)
        except OSError:
            pass
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    if os.name == "posix":
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)


# --------------------------------------------------------------------- backup
def create_backup(store, ledger_path, key, remote_dir, *, now=None,
                  backup_id=None, budget_db_path=None,
                  price_table_path=None, config_paths=None,
                  security_state=None, extra_files=None,
                  magpie_snapshot_path=None,
                  magpie_status_path=None,
                  share_secret_key_path=None) -> dict:
    """Create one encrypted, consistent backup in ``remote_dir``.

    ``store`` is a :class:`share_store.ShareStore` (duck-typed: any object with
    ``backup(path)`` works in tests).  The SQLite snapshot is created by the
    store's online backup API, then read back and validated in memory.  The
    generation stored in the backup is the generation OF THE SNAPSHOT, not a
    separately read live value, so a concurrent mutation cannot pair a new
    generation with an older database copy.

    S2 additions (all optional; old callers keep the v1 payload):
    * ``budget_db_path`` snapshots the budget guard (counters, period, pause);
    * ``price_table_path`` records the ACTUAL price table bytes + version;
    * ``config_paths``/``extra_files`` carry the restore-required configuration;
    * ``security_state`` carries the latest security journal snapshot;
    * ``magpie_snapshot_path``/``magpie_status_path`` carry the Magpie price
      snapshot AND status plus its version closure.  A MISSING or CORRUPT
      snapshot fails this backup instead of inventing a version; omitting the
      path records ``magpie_present=False`` and recovery then refuses it.
    * ``share_secret_key_path`` carries the POSIX AES-GCM share-key file so a
      backup restored on another machine can still decrypt the store rows.  When
      the path is omitted the metadata records ``share_secret_present=False``.
      When it is supplied, the key is verified against this snapshot before the
      package is sealed; a key that cannot decrypt the AES-GCM rows is refused.
    """
    material = _validate_key(key)
    epoch = time.time() if now is None else float(now)
    identifier = str(backup_id or uuid.uuid4())
    remote = Path(remote_dir)
    remote.mkdir(parents=True, exist_ok=True)
    if magpie_status_path is not None and magpie_snapshot_path is None:
        raise BackupMagpieError(
            "magpie status requires the magpie price snapshot")

    with tempfile.TemporaryDirectory(prefix="um-share-backup-") as tmp:
        snapshot_path = Path(tmp) / "share.db"
        store.backup(snapshot_path)
        try:
            db_bytes = snapshot_path.read_bytes()
        except OSError as exc:
            raise BackupError(
                f"SQLite snapshot is unreadable: {snapshot_path}") from exc
    db_facts = _inspect_sqlite_bytes(db_bytes)
    ledger = snapshot_ledger(ledger_path)
    budget_bytes = b""
    budget_facts = None
    if budget_db_path is not None:
        budget_bytes = _snapshot_sqlite_bytes(budget_db_path)
        budget_facts = _inspect_budget_bytes(budget_bytes)
    if len(budget_bytes) > MAX_BUDGET_BYTES:
        raise BackupError(
            f"budget snapshot exceeds backup bound ({MAX_BUDGET_BYTES})")
    security_bytes = b""
    security = None
    if security_state is not None:
        security = _validate_security_state(security_state)
        security_bytes = _security_state_bytes(security)
    if len(security_bytes) > MAX_SECURITY_BYTES:
        raise BackupError(
            f"security state exceeds backup bound ({MAX_SECURITY_BYTES})")

    files = {}

    def _add_file(name, data):
        if name in files:
            raise BackupError(f"duplicate backup file name: {name!r}")
        files[name] = data

    if extra_files:
        if not isinstance(extra_files, dict):
            raise BackupError("extra_files must be a name->bytes mapping")
        for name, data in extra_files.items():
            _add_file(name, data)
    if config_paths:
        if isinstance(config_paths, (str, os.PathLike)):
            config_paths = [config_paths]
        for path in config_paths:
            target = Path(path)
            try:
                _add_file(target.name, target.read_bytes())
            except OSError as exc:
                raise BackupError(
                    f"config file unreadable: {target}") from exc
    price_table_version = None
    price_table_name = None
    price_table_sha256 = None
    if price_table_path is not None:
        price_path = Path(price_table_path)
        try:
            price_bytes = price_path.read_bytes()
        except OSError as exc:
            raise BackupError(
                f"price table unreadable: {price_path}") from exc
        price_table_name = price_path.name
        price_table_sha256 = hashlib.sha256(price_bytes).hexdigest()
        _add_file(price_table_name, price_bytes)
        try:
            price_payload = json.loads(price_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BackupError(
                f"price table is not valid JSON: {price_path}") from exc
        if not isinstance(price_payload, dict):
            raise BackupError(f"price table is not an object: {price_path}")
        explicit = price_payload.get("version")
        price_table_version = (
            str(explicit).strip() if isinstance(explicit, (str, int))
            and not isinstance(explicit, bool) and str(explicit).strip()
            else hashlib.sha256(price_bytes).hexdigest()[:16])
    magpie = None
    if magpie_snapshot_path is not None:
        magpie_path = Path(magpie_snapshot_path)
        if not magpie_path.is_file():
            # A missing snapshot is fail-closed, never a backup that silently
            # claims the reference price closure is present.
            raise BackupMagpieError(
                f"magpie price snapshot is missing: {magpie_path}")
        status_path = (Path(magpie_status_path)
                       if magpie_status_path is not None
                       else magpie_path.with_name(MAGPIE_STATUS_NAME))
        closure = _magpie_closure(magpie_path, status_path)
        snapshot_bytes = magpie_path.read_bytes()
        status_bytes = None
        if status_path.is_file():
            status_bytes = status_path.read_bytes()
        _add_file(MAGPIE_SNAPSHOT_NAME, snapshot_bytes)
        if status_bytes is not None:
            _add_file(MAGPIE_STATUS_NAME, status_bytes)
        magpie = {
            "present": True,
            "current": closure["current"],
            "versions": closure["versions"],
            "snapshot_sha256": hashlib.sha256(
                snapshot_bytes).hexdigest(),
            "status_present": status_bytes is not None,
            "status_sha256": (
                None if status_bytes is None
                else hashlib.sha256(status_bytes).hexdigest()),
            "status_snapshot_version": None,
            "closure": closure,
        }
        if status_bytes is not None:
            status = _read_magpie_status_bytes(status_bytes)
            magpie["status_snapshot_version"] = status.get(
                "snapshotVersion")
    share_secret = None
    if share_secret_key_path is not None:
        secret_path = Path(share_secret_key_path)
        try:
            secret_bytes = secret_path.read_bytes()
        except OSError as exc:
            raise BackupError(
                f"share secret key unreadable: {secret_path}") from exc
        # A package that names a share key must prove it opens THIS snapshot's
        # AES-GCM rows before it is sealed.  Checking only that the file exists
        # would report a successful backup whose restored store is unreadable.
        _verify_share_secret_decrypts_store(db_bytes, secret_bytes)
        _add_file(SHARE_SECRET_NAME, secret_bytes)
        share_secret = {
            "name": SHARE_SECRET_NAME,
            "sha256": hashlib.sha256(secret_bytes).hexdigest(),
            "size": len(secret_bytes),
        }
    extra_entries = []
    extra_payload = []
    total_extra = 0
    for name in sorted(files):
        data = files[name]
        if not isinstance(name, str) or not name or "/" in name \
                or "\\" in name or name in (".", ".."):
            raise BackupError(f"extra file name is invalid: {name!r}")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise BackupError(f"extra file is not bytes: {name}")
        chunk = bytes(data)
        total_extra += len(chunk)
        if total_extra > MAX_EXTRA_BYTES:
            raise BackupError(
                f"extra files exceed backup bound ({MAX_EXTRA_BYTES})")
        extra_entries.append({
            "name": name,
            "size": len(chunk),
            "sha256": hashlib.sha256(chunk).hexdigest(),
        })
        extra_payload.append(chunk)

    metadata = {
        "format": FORMAT,
        "backup_id": identifier,
        "created_at": _utc_iso(epoch),
        "created_at_epoch": epoch,
        "schema_version": db_facts["schema_version"],
        "config_generation": db_facts["config_generation"],
        "applied_generation": db_facts["applied_generation"],
        "key_count": db_facts["key_count"],
        "db_sha256": hashlib.sha256(db_bytes).hexdigest(),
        "db_size": len(db_bytes),
        "ledger_present": bool(ledger["present"]),
        "ledger_sha256": ledger["sha256"],
        "ledger_offset": int(ledger["offset"]),
        "ledger_bytes": len(ledger["data"]),
        "ledger_partial_bytes": int(ledger["partial_bytes"]),
        "ledger_records": int(ledger["records"]),
        "ledger_last_record_time": ledger["last_record_time"],
        "key_fingerprint": _key_fingerprint(material).hex(),
        "budget_present": budget_facts is not None,
        "budget_sha256": hashlib.sha256(budget_bytes).hexdigest(),
        "budget_size": len(budget_bytes),
        "budget_facts": budget_facts,
        "price_table_present": price_table_version is not None,
        "price_table_version": price_table_version,
        "price_table_name": price_table_name,
        "price_table_sha256": price_table_sha256,
        "magpie_present": bool(magpie and magpie["present"]),
        "magpie_missing": magpie is None,
        "magpie_current_version": (
            None if not magpie
            else magpie["current"]["version"]),
        "magpie_current": (
            None if not magpie
            else {
                "version": magpie["current"]["version"],
                "effectiveAt": magpie["current"].get("effectiveAt"),
                "sourceObservedAt": magpie["current"].get(
                    "sourceObservedAt"),
            }),
        "magpie_versions": (
            [] if not magpie or not magpie["present"]
            else [item["version"] for item in magpie["versions"]]),
        "magpie_closure": (
            None if not magpie or not magpie["present"]
            else magpie["closure"]),
        "magpie_snapshot_sha256": (
            None if not magpie else magpie["snapshot_sha256"]),
        "magpie_status_present": bool(
            magpie and magpie["status_present"]),
        "magpie_status_sha256": (
            None if not magpie else magpie["status_sha256"]),
        "magpie_status_snapshot_version": (
            None if not magpie else magpie["status_snapshot_version"]),
        "share_secret_present": share_secret is not None,
        "share_secret_name": (
            None if share_secret is None else share_secret["name"]),
        "share_secret_sha256": (
            None if share_secret is None else share_secret["sha256"]),
        "share_secret_size": (
            None if share_secret is None else share_secret["size"]),
        "extra_files": extra_entries,
        "extra_size": total_extra,
        "extra_sha256": hashlib.sha256(b"".join(extra_payload)).hexdigest(),
        "security_present": security is not None,
        "security_sha256": hashlib.sha256(security_bytes).hexdigest(),
        "security_size": len(security_bytes),
        "security_generation": (
            None if security is None else security["generation_floor"]),
        "security_records": (
            None if security is None else len(security["records"])),
    }
    package = _build_package(metadata, db_bytes, ledger["data"],
                             budget_bytes, b"".join(extra_payload),
                             security_bytes)
    sealed = _seal(package, material)
    filename = (f"um-share-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(epoch))}"
                f"-{identifier[:8]}{BACKUP_SUFFIX}")
    path = remote / filename
    _atomic_write(path, sealed)
    return {
        "backup_path": str(path),
        "backup_id": identifier,
        "created_at": metadata["created_at"],
        "created_at_epoch": epoch,
        "config_generation": metadata["config_generation"],
        "applied_generation": metadata["applied_generation"],
        "db_sha256": metadata["db_sha256"],
        "db_size": metadata["db_size"],
        "ledger_present": metadata["ledger_present"],
        "ledger_sha256": metadata["ledger_sha256"],
        "ledger_offset": metadata["ledger_offset"],
        "ledger_records": metadata["ledger_records"],
        "ledger_partial_bytes": metadata["ledger_partial_bytes"],
        "ledger_last_record_time": metadata["ledger_last_record_time"],
        "key_fingerprint": metadata["key_fingerprint"],
        "budget_present": metadata["budget_present"],
        "budget_facts": metadata["budget_facts"],
        "price_table_present": metadata["price_table_present"],
        "price_table_version": metadata["price_table_version"],
        "price_table_name": metadata["price_table_name"],
        "price_table_sha256": metadata["price_table_sha256"],
        "magpie_present": metadata["magpie_present"],
        "magpie_missing": metadata["magpie_missing"],
        "magpie_current_version": metadata["magpie_current_version"],
        "magpie_versions": metadata["magpie_versions"],
        "magpie_status_present": metadata["magpie_status_present"],
        "share_secret_present": metadata["share_secret_present"],
        "share_secret_name": metadata["share_secret_name"],
        "share_secret_sha256": metadata["share_secret_sha256"],
        "extra_files": metadata["extra_files"],
        "security_generation": metadata["security_generation"],
        "security_records": metadata["security_records"],
        "size": len(sealed),
    }


def inspect_backup(backup_path, key, *, require_min_generation=None) -> dict:
    """Authenticate and validate a sealed backup without writing anything."""
    material = _validate_key(key)
    try:
        sealed = Path(backup_path).read_bytes()
    except OSError as exc:
        raise BackupError(f"backup object unreadable: {backup_path}") from exc
    package = _open(sealed, material)
    (metadata, db_bytes, ledger_bytes, budget_bytes, extra_bytes,
     security_bytes) = _parse_package(package)
    if metadata.get("key_fingerprint") != _key_fingerprint(material).hex():
        raise BackupIntegrityError(
            "backup header key fingerprint does not match")
    db_facts = _inspect_sqlite_bytes(db_bytes)
    if db_facts["config_generation"] != metadata.get("config_generation"):
        raise BackupIntegrityError(
            "backup generation does not match the SQLite snapshot")
    if db_facts["applied_generation"] != metadata.get("applied_generation"):
        raise BackupIntegrityError(
            "backup applied generation does not match the SQLite snapshot")
    if metadata.get("schema_version") != db_facts["schema_version"]:
        raise BackupIntegrityError(
            "backup schema version does not match the SQLite snapshot")
    if require_min_generation is not None:
        if isinstance(require_min_generation, bool) or not isinstance(
                require_min_generation, int):
            raise BackupGenerationError(
                "require_min_generation must be an integer")
        if int(metadata["config_generation"]) < require_min_generation:
            raise BackupGenerationError(
                "backup generation "
                f"{metadata['config_generation']} is older than required "
                f"{require_min_generation}")
    budget_facts = None
    if metadata.get("budget_present"):
        budget_facts = _inspect_budget_bytes(budget_bytes)
        declared = metadata.get("budget_facts")
        if isinstance(declared, dict):
            for key in ("bytes_used", "cost_micros_used", "paused",
                         "period_id"):
                if key in declared and declared[key] != budget_facts[key]:
                    raise BackupIntegrityError(
                        f"budget snapshot {key} does not match the header")
    extra_files = _parse_extra_files(metadata, extra_bytes)
    share_secret = None
    share_secret_name = metadata.get("share_secret_name")
    if not isinstance(share_secret_name, str) or not share_secret_name:
        share_secret_name = SHARE_SECRET_NAME
    share_secret_bytes = extra_files.get(share_secret_name)
    if metadata.get("share_secret_present"):
        if share_secret_bytes is None:
            raise BackupIntegrityError(
                "backup claims a share secret key but carries none")
        expected_secret = metadata.get("share_secret_sha256")
        if expected_secret is not None and hashlib.sha256(
                share_secret_bytes).hexdigest() != expected_secret:
            raise BackupIntegrityError(
                "backup share secret key SHA-256 mismatch")
        share_secret = {
            "name": share_secret_name,
            "sha256": hashlib.sha256(share_secret_bytes).hexdigest(),
            "size": len(share_secret_bytes),
        }
    elif SHARE_SECRET_NAME in extra_files:
        raise BackupIntegrityError(
            "backup carries a share secret key but claims none")
    price_table = None
    price_name = metadata.get("price_table_name")
    if metadata.get("price_table_present"):
        if not isinstance(price_name, str) or price_name not in extra_files:
            raise BackupIntegrityError(
                "backup claims a price table but carries no payload")
        price_bytes = extra_files[price_name]
        expected_price = metadata.get("price_table_sha256")
        if expected_price is not None and hashlib.sha256(
                price_bytes).hexdigest() != expected_price:
            raise BackupIntegrityError(
                "backup price table SHA-256 mismatch")
        price_table = {
            "name": price_name,
            "sha256": hashlib.sha256(price_bytes).hexdigest(),
            "version": metadata.get("price_table_version"),
        }
    magpie = None
    magpie_names = {MAGPIE_SNAPSHOT_NAME, MAGPIE_STATUS_NAME}
    present_magpie = magpie_names & set(extra_files)
    if metadata.get("magpie_present"):
        snapshot_bytes = extra_files.get(MAGPIE_SNAPSHOT_NAME)
        if snapshot_bytes is None:
            raise BackupIntegrityError(
                "backup claims magpie prices but carries no snapshot")
        expected_snapshot = metadata.get("magpie_snapshot_sha256")
        if expected_snapshot is not None and hashlib.sha256(
                snapshot_bytes).hexdigest() != expected_snapshot:
            raise BackupIntegrityError(
                "backup magpie snapshot SHA-256 mismatch")
        snapshot = _read_magpie_snapshot_bytes(snapshot_bytes)
        versions = [item["version"] for item in snapshot["versions"]]
        current = snapshot["versions"][-1]
        if metadata.get("magpie_current_version") != current["version"]:
            raise BackupIntegrityError(
                "backup magpie current version does not match the snapshot")
        declared_versions = metadata.get("magpie_versions")
        if declared_versions is not None and declared_versions != versions:
            raise BackupIntegrityError(
                "backup magpie version closure does not match the snapshot")
        status = None
        status_bytes = extra_files.get(MAGPIE_STATUS_NAME)
        if metadata.get("magpie_status_present"):
            if status_bytes is None:
                raise BackupIntegrityError(
                    "backup claims magpie status but carries none")
            expected_status = metadata.get("magpie_status_sha256")
            if expected_status is not None and hashlib.sha256(
                    status_bytes).hexdigest() != expected_status:
                raise BackupIntegrityError(
                    "backup magpie status SHA-256 mismatch")
            status = _read_magpie_status_bytes(status_bytes)
            if status.get("snapshotVersion") != current["version"]:
                raise BackupIntegrityError(
                    "backup magpie status version does not match the "
                    "current snapshot version")
        elif status_bytes is not None:
            raise BackupIntegrityError(
                "backup carries magpie status but claims none")
        magpie = {
            "present": True,
            "current": current,
            "versions": snapshot["versions"],
            "snapshot_sha256": hashlib.sha256(
                snapshot_bytes).hexdigest(),
            "status_present": status_bytes is not None,
            "status_sha256": (
                None if status_bytes is None
                else hashlib.sha256(status_bytes).hexdigest()),
            "status_snapshot_version": (
                None if status is None else status.get("snapshotVersion")),
        }
    elif present_magpie:
        raise BackupIntegrityError(
            "backup carries magpie files but claims no magpie closure")
    security = None
    if metadata.get("security_present"):
        try:
            security = _validate_security_state(
                json.loads(security_bytes.decode("utf-8")))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BackupIntegrityError(
                "backup security state is not valid JSON") from exc
        if metadata.get("security_generation") != security["generation_floor"]:
            raise BackupIntegrityError(
                "security generation does not match the security state")
    return {
        "metadata": metadata,
        "db_bytes": db_bytes,
        "ledger_bytes": ledger_bytes,
        "budget_bytes": budget_bytes,
        "budget": budget_facts,
        "extra_files": extra_files,
        "share_secret": share_secret,
        "price_table": price_table,
        "magpie": magpie,
        "security": security,
        "db": db_facts,
    }


def restore_backup(backup_path, key, *, target_db_path, target_ledger_path,
                   require_min_generation=None, allow_existing=False,
                   target_budget_db_path=None, target_extra_dir=None,
                   target_security_state_path=None,
                   target_price_table_path=None, target_magpie_dir=None,
                   target_share_secret_key_path=None,
                   target_share_secret_owner=None,
                   target_share_secret_group=None,
                   security_state=None, security_state_path=None,
                   require_budget=False, require_config=False,
                   require_security=False, require_magpie=False,
                   require_share_secret=False) -> dict:
    """Restore a sealed backup into NEW target paths.

    ``allow_existing=False`` is the safety default: a second restore to the same
    target fails explicitly instead of overwriting the first restored copy.  The
    target is staged and then replaced; the restored SQLite file is re-opened
    read-only and its generation is compared with the authenticated metadata.

    S2 recovery additions:
    * ``security_state``/``security_state_path`` is the INDEPENDENT latest
      security journal.  When supplied it becomes a generation floor (the
      caller's ``require_min_generation`` can only raise it), so an old backup
      taken before a later revoke cannot be restored over that revoke.
    * ``require_budget`` refuses a package without the budget snapshot, so an
      old backup cannot silently reset the spent quota.
    * ``require_config`` refuses a package without the price/config files.
    * ``require_magpie`` refuses a package without the Magpie price closure;
      a present closure is restored to ``target_magpie_dir`` and its current
      version is re-checked against the version recorded with the ledger.
    * ``require_share_secret`` refuses a package without the POSIX AES-GCM
      share-key file; when present it is restored to
      ``target_share_secret_key_path`` so the restored store rows stay
      decryptable on a new machine.  ``target_share_secret_owner``/
      ``target_share_secret_group`` set the restored file's owner (the
      console user); an existing target's owner is preserved when they are
      omitted.
    """
    if security_state is not None and security_state_path is not None:
        raise BackupSecurityError(
            "pass security_state OR security_state_path, not both")
    security = None
    if security_state_path is not None:
        security = read_security_state(security_state_path)
    elif security_state is not None:
        security = _validate_security_state(security_state)
    floor = require_min_generation
    if security is not None:
        floor = max(0 if floor is None else int(floor),
                    security["generation_floor"])
    if require_security and security is None:
        raise BackupSecurityError(
            "an independent security state is required for recovery")
    inspected = inspect_backup(
        backup_path, key, require_min_generation=floor)
    metadata = inspected["metadata"]
    if require_budget and not inspected["budget"]:
        raise BackupBudgetError(
            "the backup has no budget snapshot; the spent quota cannot be "
            "confirmed")
    if require_magpie and not inspected["magpie"]:
        raise BackupMagpieError(
            "the backup has no magpie price snapshot/status; the reference "
            "price version cannot be restored")
    price_name = metadata.get("price_table_name")
    magpie_names = {MAGPIE_SNAPSHOT_NAME, MAGPIE_STATUS_NAME}
    share_secret_name = metadata.get("share_secret_name")
    if not isinstance(share_secret_name, str) or not share_secret_name:
        share_secret_name = SHARE_SECRET_NAME
    price_bytes = (
        inspected["extra_files"].get(price_name)
        if metadata.get("price_table_present") and price_name else None)
    share_secret_bytes = (
        inspected["extra_files"].get(share_secret_name)
        if metadata.get("share_secret_present") else None)
    config_files = {
        name: data for name, data in inspected["extra_files"].items()
        if name != price_name and name not in magpie_names
        and name != share_secret_name}
    magpie_files = {
        name: data for name, data in inspected["extra_files"].items()
        if name in magpie_names}
    if require_share_secret and share_secret_bytes is None:
        raise BackupError(
            "the backup has no share secret key; the restored store rows "
            "cannot be decrypted on another machine")
    share_secret_verified_rows = 0
    if require_share_secret and share_secret_bytes is not None:
        # The key is about to replace the live share-key file.  Prove it opens
        # the AES-GCM rows in this same package before any target is touched,
        # so a wrong key cannot be installed alongside an unreadable store.
        share_secret_verified_rows = _verify_share_secret_decrypts_store(
            inspected["db_bytes"], share_secret_bytes)
    if require_config and not config_files and price_bytes is None:
        raise BackupConfigError(
            "the backup has no price/config payload required for restore")
    if security is not None and metadata.get("security_present"):
        embedded = inspected["security"]
        if embedded is not None and (
                embedded["generation_floor"]
                > security["generation_floor"]):
            raise BackupSecurityError(
                "the backup security state is newer than the independent "
                "latest state")
    db_target = Path(target_db_path)
    ledger_target = Path(target_ledger_path)
    ledger_present = bool(metadata.get("ledger_present"))
    budget_target = (Path(target_budget_db_path)
                     if target_budget_db_path is not None else None)
    extra_target = Path(target_extra_dir) if target_extra_dir is not None else None
    security_target = (Path(target_security_state_path)
                       if target_security_state_path is not None else None)
    price_target = (Path(target_price_table_path)
                    if target_price_table_path is not None else None)
    magpie_target = (Path(target_magpie_dir)
                     if target_magpie_dir is not None else None)
    share_secret_target = (Path(target_share_secret_key_path)
                           if target_share_secret_key_path is not None
                           else None)
    if inspected["budget"] and budget_target is None and require_budget:
        raise BackupBudgetError(
            "the backup carries a budget snapshot but no target was named")
    if config_files and extra_target is None and require_config:
        raise BackupConfigError(
            "the backup carries config files but no target was named")
    if price_bytes is not None and price_target is None \
            and extra_target is None and require_config:
        raise BackupConfigError(
            "the backup carries a price table but no target was named")
    if inspected["magpie"] and magpie_target is None:
        raise BackupMagpieError(
            "the backup carries magpie prices but no target was named")
    if share_secret_bytes is not None and share_secret_target is None \
            and require_share_secret:
        raise BackupError(
            "the backup carries a share secret key but no target was named")
    # A deployment recovery runs as root but the console reads the key as its
    # own user.  Preserve the live file's owner when it exists; otherwise the
    # deployment entry supplies the console uid/gid.  The mode is always 0600
    # because the key-file gate refuses anything else.
    share_secret_owner = target_share_secret_owner
    share_secret_group = target_share_secret_group
    if share_secret_target is not None and share_secret_target.exists():
        try:
            live_secret_info = share_secret_target.stat()
        except OSError as exc:
            raise BackupError(
                "the live share secret key cannot be inspected: "
                f"{share_secret_target}: {exc}") from exc
        if share_secret_owner is None:
            share_secret_owner = live_secret_info.st_uid
        if share_secret_group is None:
            share_secret_group = live_secret_info.st_gid
    share_secret_mode = 0o600
    if not allow_existing:
        for path in (db_target, ledger_target, budget_target,
                     security_target, price_target, share_secret_target,
                     (None if magpie_target is None
                      else magpie_target / MAGPIE_SNAPSHOT_NAME),
                     (None if magpie_target is None
                      else magpie_target / MAGPIE_STATUS_NAME)):
            if path is not None and path.exists():
                raise RestoreTargetExists(
                    f"restore target already exists: {path}")
        if extra_target is not None and extra_target.exists():
            raise RestoreTargetExists(
                f"restore target already exists: {extra_target}")

    for path in (db_target, ledger_target, budget_target,
                 security_target, price_target, share_secret_target):
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
    if extra_target is not None:
        extra_target.mkdir(parents=True, exist_ok=True)
    if magpie_target is not None:
        magpie_target.mkdir(parents=True, exist_ok=True)
    stages = []
    installed = []
    keep_backups = False

    def _install(target, data, *, owner=None, group=None, mode=None):
        stage = _stage_bytes(target, data, owner=owner, group=group, mode=mode)
        stages.append(stage)
        old = _move_aside(target)
        installed.append({"target": target, "old": old, "new": False})
        os.replace(stage, target)
        stages.remove(stage)
        installed[-1]["new"] = True

    try:
        _install(db_target, inspected["db_bytes"])
        if ledger_present:
            _install(ledger_target, inspected["ledger_bytes"])
        if inspected["budget"] and budget_target is not None:
            _install(budget_target, inspected["budget_bytes"])
        if price_bytes is not None:
            if price_target is not None:
                _install(price_target, price_bytes)
            elif extra_target is not None:
                _install(extra_target / price_name, price_bytes)
        if magpie_files and magpie_target is not None:
            for name in sorted(magpie_files):
                _install(magpie_target / name, magpie_files[name])
        if config_files and extra_target is not None:
            for name, data in config_files.items():
                _install(extra_target / name, data)
        if share_secret_bytes is not None and share_secret_target is not None:
            _install(share_secret_target, share_secret_bytes,
                     owner=share_secret_owner, group=share_secret_group,
                     mode=share_secret_mode)
        if security is not None and security_target is not None:
            _install(security_target, _security_state_bytes(security))
        # Verification is part of the same transaction: a generation/hash
        # mismatch after any replace must restore the old files instead of
        # leaving a half-new or corrupt restore in place.
        restored_db = _inspect_sqlite_file(db_target)
        if restored_db["config_generation"] != metadata["config_generation"]:
            raise BackupIntegrityError(
                "restored SQLite generation does not match the backup")
        if ledger_present:
            restored_hash = _sha256_file(ledger_target)
            if restored_hash != metadata["ledger_sha256"]:
                raise BackupIntegrityError(
                    "restored usage ledger SHA-256 does not match the backup")
        if inspected["budget"] and budget_target is not None:
            restored_budget = _inspect_budget_bytes(budget_target.read_bytes())
            for key in ("bytes_used", "cost_micros_used", "paused",
                         "period_id", "pause_id"):
                if restored_budget[key] != inspected["budget"][key]:
                    raise BackupBudgetError(
                        f"restored budget {key} does not match the backup")
        if price_bytes is not None:
            if price_target is not None:
                restored_price = price_target
            elif extra_target is not None:
                restored_price = extra_target / price_name
            else:
                restored_price = None
            if restored_price is not None and _sha256_file(
                    restored_price) != hashlib.sha256(
                        price_bytes).hexdigest():
                raise BackupIntegrityError(
                    "restored price table SHA-256 does not match the backup")
        if magpie_files and magpie_target is not None:
            restored_snapshot = _read_magpie_snapshot_bytes(
                (magpie_target / MAGPIE_SNAPSHOT_NAME).read_bytes())
            restored_current = restored_snapshot["versions"][-1]
            declared_version = metadata.get("magpie_current_version")
            if declared_version is not None \
                    and restored_current["version"] != declared_version:
                raise BackupMagpieError(
                    "restored magpie version does not match the version "
                    "recorded with the ledger snapshot")
            if metadata.get("magpie_status_present"):
                restored_status = _read_magpie_status_bytes(
                    (magpie_target / MAGPIE_STATUS_NAME).read_bytes())
                if restored_status.get("snapshotVersion") != \
                        restored_current["version"]:
                    raise BackupMagpieError(
                        "restored magpie status version does not match the "
                        "restored snapshot version")
        if security is not None and security_target is not None:
            restored_security = read_security_state(security_target)
            if restored_security["generation_floor"] != security["generation_floor"]:
                raise BackupSecurityError(
                    "restored security generation does not match")
        if share_secret_bytes is not None and share_secret_target is not None:
            if _sha256_file(share_secret_target) != hashlib.sha256(
                    share_secret_bytes).hexdigest():
                raise BackupIntegrityError(
                    "restored share secret key SHA-256 does not match the "
                    "backup")
    except BaseException as exc:
        rollback_errors = []
        for item in reversed(installed):
            errors, remaining = _rollback_target(
                item["target"], item["old"], item["new"])
            rollback_errors.extend(errors)
            if remaining is not None:
                keep_backups = True
        if rollback_errors:
            raise BackupError(
                "restore failed and rollback was incomplete: "
                + "; ".join(rollback_errors)) from exc
        raise
    finally:
        for stage in stages:
            try:
                os.unlink(stage)
            except OSError:
                pass
        if not keep_backups:
            for item in installed:
                backup = item["old"]
                if backup is not None:
                    try:
                        os.unlink(backup)
                    except OSError:
                        pass

    return {
        "metadata": metadata,
        "db_path": str(db_target),
        "ledger_path": str(ledger_target) if ledger_present else None,
        "ledger_present": ledger_present,
        "config_generation": restored_db["config_generation"],
        "applied_generation": restored_db["applied_generation"],
        "budget_restored": bool(inspected["budget"] and budget_target),
        "budget": inspected["budget"],
        "budget_path": str(budget_target) if budget_target is not None else None,
        "price_table_path": (
            str(price_target) if price_target is not None else None),
        "price_table_version": metadata.get("price_table_version"),
        "magpie_restored": bool(magpie_files and magpie_target),
        "magpie_current_version": (
            None if not inspected["magpie"]
            else inspected["magpie"]["current"]["version"]),
        "magpie_dir": (
            str(magpie_target) if magpie_target is not None else None),
        "extra_files": sorted(inspected["extra_files"]),
        "extra_dir": str(extra_target) if extra_target is not None else None,
        "share_secret_restored": bool(
            share_secret_bytes is not None and share_secret_target is not None),
        "share_secret_verified_rows": share_secret_verified_rows,
        "share_secret_path": (
            str(share_secret_target)
            if share_secret_target is not None else None),
        "security_floor": (
            None if security is None else security["generation_floor"]),
        "security_path": (
            str(security_target) if security_target is not None else None),
        "security_applied": security is not None,
    }


def _stage_bytes(target: Path, data: bytes, *, owner=None, group=None,
                  mode=None):
    """Write the staged bytes with the target's runtime ownership.

    ``mkstemp`` creates a file owned by the restoring user (root during a
    deployment recovery).  The console runs as its own user and its key-file
    gate requires ``0600`` owned by that same uid, so a restored custom-path
    key would be unreadable after a root restore.  ``fchown``/``fchmod`` run on
    the open descriptor BEFORE the atomic replace, so the new target never
    appears with the wrong owner or mode.
    """
    fd, name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".stage", dir=str(target.parent))
    try:
        if owner is not None and hasattr(os, "fchown"):
            os.fchown(fd, owner, group if group is not None else owner)
        if mode is not None:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return name
    except Exception:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def _move_aside(target: Path):
    """Move an existing target to a unique sibling backup; None when absent.

    The backup name is unique per restore attempt, so a stale backup from an
    earlier failed restore is never silently overwritten or deleted. ``os.replace``
    is atomic on the same filesystem.
    """
    if not target.exists():
        return None
    backup = target.with_name(
        f"{target.name}.restore-backup-{os.getpid()}-{time.time_ns()}")
    os.replace(target, backup)
    return backup


def _rollback_target(target: Path, backup, installed: bool):
    """Undo one target replacement; return (errors, remaining_backup).

    ``installed`` means the NEW staged file reached the target, so it is removed
    first. The old backup (when present) is then atomically restored. A failed
    unlink/restore is reported and the backup is left in place for manual
    recovery instead of being deleted.
    """
    errors = []
    if installed:
        try:
            os.unlink(target)
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(f"cannot remove restored {target.name}: {exc}")
    if backup is not None:
        try:
            os.replace(backup, target)
            backup = None
        except OSError as exc:
            errors.append(f"cannot restore old {target.name}: {exc}")
    return errors, backup


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise BackupError(f"restored file unreadable: {path}") from exc
    return digest.hexdigest()


def _inspect_sqlite_file(path: Path) -> dict:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only=ON")
            integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
            if integrity != ["ok"]:
                raise BackupIntegrityError(
                    f"restored SQLite failed integrity_check: {path}")
            row = conn.execute(
                "SELECT schema_version, config_generation, applied_generation "
                "FROM configgen").fetchone()
            if row is None:
                raise BackupIntegrityError(
                    f"restored SQLite has no configgen row: {path}")
            return {
                "schema_version": int(row[0]),
                "config_generation": int(row[1]),
                "applied_generation": int(row[2]),
            }
        finally:
            conn.close()
    except BackupError:
        raise
    except sqlite3.Error as exc:
        raise BackupIntegrityError(
            f"restored SQLite is invalid: {path}: {exc}") from exc


# ------------------------------------------------------------ object storage
class ObjectStore:
    """Narrow adapter seam for a private object store (R2/OSS/remote-sim).

    The product never imports an S3 SDK here.  A deployment supplies one
    implementation; tests use :class:`LocalObjectStore` or
    :class:`MemoryObjectStore`.  ``key`` is a relative object key and may never
    escape the store root.
    """

    def put(self, key, data):
        raise NotImplementedError

    def get(self, key):
        raise NotImplementedError

    def list(self, prefix=""):
        raise NotImplementedError

    def delete(self, key):
        raise NotImplementedError


def _object_key(key):
    if not isinstance(key, str) or not key or key.startswith("/"):
        raise ObjectStoreError("object key must be a non-empty relative path")
    parts = key.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ObjectStoreError(f"object key is not safe: {key!r}")
    return key


class LocalObjectStore(ObjectStore):
    """Directory-backed object store (remote-sim for isolated drills)."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key):
        key = _object_key(key)
        target = (self.root / key).resolve()
        try:
            target.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ObjectStoreError(
                f"object key escapes the store root: {key!r}") from exc
        return target

    def put(self, key, data):
        target = self._path(key)
        _atomic_write(target, bytes(data), mode=0o600)
        return {"key": _object_key(key), "size": len(data)}

    def get(self, key):
        target = self._path(key)
        try:
            return target.read_bytes()
        except OSError as exc:
            raise ObjectStoreError(
                f"object unreadable: {key}") from exc

    def list(self, prefix=""):
        prefix = str(prefix or "").replace("\\", "/")
        rows = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            key = path.relative_to(self.root).as_posix()
            if prefix and not key.startswith(prefix):
                continue
            stat_info = path.stat()
            rows.append({"key": key, "size": stat_info.st_size,
                          "mtime": stat_info.st_mtime})
        return rows

    def delete(self, key):
        target = self._path(key)
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        return True


class MemoryObjectStore(ObjectStore):
    """In-memory fake object service with an optional bounded failure script."""

    def __init__(self, fail_puts=0, fail_lists=0):
        self._objects = {}
        self._lock = __import__("threading").Lock()
        self.fail_puts = int(fail_puts)
        self.fail_lists = int(fail_lists)
        self.put_attempts = 0

    def put(self, key, data):
        key = _object_key(key)
        with self._lock:
            self.put_attempts += 1
            if self.put_attempts <= self.fail_puts:
                raise ObjectStoreError("injected object-store put failure")
            self._objects[key] = bytes(data)
        return {"key": key, "size": len(data)}

    def get(self, key):
        key = _object_key(key)
        with self._lock:
            if key not in self._objects:
                raise ObjectStoreError(f"object not found: {key}")
            return self._objects[key]

    def list(self, prefix=""):
        with self._lock:
            if self.fail_lists > 0:
                self.fail_lists -= 1
                raise ObjectStoreError("injected object-store list failure")
            rows = []
            for key in sorted(self._objects):
                if prefix and not key.startswith(prefix):
                    continue
                rows.append({"key": key, "size": len(self._objects[key]),
                              "mtime": 0.0})
            return rows

    def delete(self, key):
        key = _object_key(key)
        with self._lock:
            return self._objects.pop(key, None) is not None


def upload_backup(backup_path, object_store, *, key=None, attempts=3,
                  retry_delay=0.0) -> dict:
    """Upload ONE sealed backup with bounded retries.

    A failed upload never deletes the local spool file; the caller keeps it for
    the next checkpoint.  ``attempted_bytes`` counts every attempted object
    upload conservatively (a failed request may already have billed egress).
    """
    if not isinstance(object_store, ObjectStore):
        raise ObjectStoreError("object store adapter is required")
    if isinstance(attempts, bool) or not isinstance(attempts, int) \
            or attempts < 1:
        raise ObjectStoreError("upload attempts must be a positive integer")
    path = Path(backup_path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BackupUploadError(
            f"backup object unreadable: {path}") from exc
    object_key = key or (BACKUP_PREFIX + path.name)
    last_error = None
    attempted = 0
    for attempt in range(1, attempts + 1):
        attempted += len(data)
        try:
            receipt = object_store.put(object_key, data)
        except (ObjectStoreError, OSError) as exc:
            last_error = exc
            if attempt < attempts and retry_delay > 0:
                time.sleep(float(retry_delay))
            continue
        return {
            "ok": True,
            "key": object_key,
            "size": len(data),
            "attempts": attempt,
            "attempted_bytes": attempted,
            "receipt": receipt,
        }
    return {
        "ok": False,
        "key": object_key,
        "size": len(data),
        "attempts": attempts,
        "attempted_bytes": attempted,
        "error": f"{type(last_error).__name__}: {last_error}",
    }


def _record_budget_egress(budget, cost_model, bytes_, source):
    if budget is None or cost_model is None:
        return None
    try:
        if source.startswith("backup"):
            cost = cost_model.backup_cost(bytes_)
        else:
            cost = cost_model.background_cost(bytes_)
        return budget.record_outbound(
            bytes_used=int(bytes_), cost_micros=int(cost),
            source=source)
    except Exception:  # noqa: BLE001 - a budget read never loses the backup
        return None


def run_backup_cycle(store, ledger_path, key, object_store, *, state_path,
                     spool_dir, budget_db_path=None, price_table_path=None,
                     config_paths=None, security_state=None,
                     security_adapter=None, budget=None, cost_model=None,
                     now=None, upload_attempts=3, retry_delay=0.0,
                     backup_id=None, magpie_snapshot_path=None,
                     magpie_status_path=None,
                     share_secret_key_path=None) -> dict:
    """One checkpoint: snapshot, upload, freshness, retry-safe spool.

    C9 is explicit: a backup failure keeps sharing serving traffic and records
    the failure/staleness.  The sealed package stays in the spool for the next
    attempt; it is never deleted on failure.  Budget egress is recorded even
    when the upload fails (the attempt may have left the host).
    """
    epoch = time.time() if now is None else float(now)
    spool = Path(spool_dir)
    spool.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="um-backup-stage-", dir=str(spool)))
    freshness = BackupFreshnessGuard(state_path)
    result = create_backup(
        store, ledger_path, key, staging, now=epoch, backup_id=backup_id,
        budget_db_path=budget_db_path, price_table_path=price_table_path,
        config_paths=config_paths, security_state=security_state,
        magpie_snapshot_path=magpie_snapshot_path,
        magpie_status_path=magpie_status_path,
        share_secret_key_path=share_secret_key_path)
    upload = upload_backup(
        result["backup_path"], object_store, attempts=upload_attempts,
        retry_delay=retry_delay)
    _record_budget_egress(
        budget, cost_model, upload["attempted_bytes"], "backup")
    pending_path = None
    if upload["ok"]:
        status = freshness.record_success(result, now=epoch)
        try:
            os.unlink(result["backup_path"])
        except OSError:
            pass
    else:
        status = freshness.record_failure(
            BackupUploadError(upload["error"]), now=epoch)
        pending_path = result["backup_path"]
    security_publish = None
    if security_adapter is not None:
        security_publish = publish_security_records(
            store, security_adapter, now=epoch)
    return {
        "ok": upload["ok"],
        "sharing_paused": False,
        "upload": upload,
        "freshness": status,
        "pending_path": pending_path,
        "backup_id": result["backup_id"],
        "config_generation": result["config_generation"],
        "magpie_present": result["magpie_present"],
        "magpie_current_version": result["magpie_current_version"],
        "share_secret_present": result["share_secret_present"],
        "security_publish": security_publish,
        "alert": status["alert"],
    }


def retry_pending_uploads(spool_dir, key, object_store, *, state_path,
                           budget=None, cost_model=None,
                           upload_attempts=3, retry_delay=0.0,
                           security_store=None, security_adapter=None,
                           now=None) -> list:
    """Retry every sealed package left in the spool by a failed checkpoint."""
    epoch = time.time() if now is None else float(now)
    spool = Path(spool_dir)
    if not spool.exists():
        return []
    freshness = BackupFreshnessGuard(state_path)
    results = []
    for path in sorted(spool.rglob("*" + BACKUP_SUFFIX)):
        inspected = inspect_backup(path, key)
        upload = upload_backup(
            path, object_store, attempts=upload_attempts,
            retry_delay=retry_delay)
        _record_budget_egress(
            budget, cost_model, upload["attempted_bytes"], "backup_retry")
        if upload["ok"]:
            status = freshness.record_success(
                {"backup_id": path.stem,
                 "config_generation": inspected["metadata"][
                     "config_generation"],
                 "ledger_offset": inspected["metadata"]["ledger_offset"],
                 "ledger_records": inspected["metadata"]["ledger_records"],
                 "ledger_partial_bytes": inspected["metadata"][
                     "ledger_partial_bytes"]},
                now=epoch)
            try:
                os.unlink(path)
            except OSError:
                pass
        else:
            status = freshness.record_failure(
                BackupUploadError(upload["error"]), now=epoch)
        results.append({"path": str(path), "ok": upload["ok"],
                         "upload": upload, "freshness": status})
    if security_store is not None and security_adapter is not None:
        publish_security_records(security_store, security_adapter, now=epoch)
    return results


def freshness_status(state_path, *, now=None) -> dict:
    """Read-only freshness surface (the console/backup status uses this)."""
    return BackupFreshnessGuard(state_path).status(now=now)


def remote_backup_status(object_store, *, prefix=BACKUP_PREFIX) -> dict:
    """Read-only remote listing; an unreadable store is never "no backups"."""
    try:
        rows = object_store.list(prefix)
    except (ObjectStoreError, OSError) as exc:
        return {"available": False, "objects": None, "latest": None,
                "reason": f"{type(exc).__name__}: {exc}"}
    latest = rows[-1] if rows else None
    return {"available": True, "objects": rows, "latest": latest,
            "count": len(rows)}


# ---------------------------------------------------------- security journal
class SecurityRecordStoreAdapter:
    """Object-store adapter for the cross-machine security journal."""

    def __init__(self, object_store, *, prefix=SECURITY_PREFIX):
        if not isinstance(object_store, ObjectStore):
            raise ObjectStoreError("security adapter needs an object store")
        self.object_store = object_store
        self.prefix = prefix

    def put_record(self, record):
        state = _validate_security_state({
            "format": SECURITY_FORMAT,
            "generation_floor": record["generation"],
            "records": [record],
        })
        record = state["records"][0]
        key = (self.prefix
               + f"{record['generation']:020d}-{record['record_id']}.json")
        self.object_store.put(key, _json_bytes(record))
        return {"key": key}

    def latest(self):
        rows = self.object_store.list(self.prefix)
        if not rows:
            raise BackupSecurityError(
                "no remote security record is available")
        rows = sorted(rows, key=lambda item: item["key"])
        key = rows[-1]["key"]
        raw = self.object_store.get(key)
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BackupSecurityError(
                "remote security record is not valid JSON") from exc
        state = _validate_security_state({
            "format": SECURITY_FORMAT,
            "generation_floor": record.get("generation"),
            "records": [record],
        })
        return {"confirmed": True, "key": key, "state": state}


def publish_security_records(store, adapter, *, now=None) -> dict:
    """Push pending security intents to the independent object store.

    A failed/uncertain upload leaves the row pending: the caller must never
    report cross-machine protection as complete.  Only a successful adapter
    receipt marks the row ``remote_confirmed``.
    """
    if adapter is None:
        state = store.security_state()
        return {"confirmed": 0, "pending": len(state["records"]),
                "errors": ["security adapter unavailable"]}
    state = store.security_state()
    confirmed, errors = 0, []
    for record in state["records"]:
        if record["status"] == "remote_confirmed":
            confirmed += 1
            continue
        try:
            receipt = adapter.put_record(record)
        except Exception as exc:  # noqa: BLE001 - pending is fail-closed
            errors.append({"record_id": record["record_id"],
                            "error": f"{type(exc).__name__}: {exc}"})
            continue
        key = receipt.get("key") if isinstance(receipt, dict) else None
        try:
            store.mark_security_remote_confirmed(record["record_id"], key)
        except Exception as exc:  # noqa: BLE001 - stays pending
            errors.append({"record_id": record["record_id"],
                            "error": f"{type(exc).__name__}: {exc}"})
            continue
        confirmed += 1
    return {"confirmed": confirmed,
            "pending": len(state["records"]) - confirmed,
            "errors": errors}


def latest_security_state(adapter) -> dict:
    """Read the independent latest security state; unconfirmed is not "safe"."""
    if adapter is None:
        return {"confirmed": False, "state": None,
                "reason": "security adapter unavailable"}
    try:
        latest = adapter.latest()
    except Exception as exc:  # noqa: BLE001 - fail-closed
        return {"confirmed": False, "state": None,
                "reason": f"{type(exc).__name__}: {exc}"}
    if not isinstance(latest, dict) or not latest.get("confirmed"):
        return {"confirmed": False, "state": None,
                "reason": "remote security state is not confirmed"}
    try:
        state = _validate_security_state(latest["state"])
    except BackupSecurityError as exc:
        return {"confirmed": False, "state": None, "reason": str(exc)}
    return {"confirmed": True, "state": state, "key": latest.get("key")}


def _load_share_store():
    import importlib.util
    module = (sys.modules.get("share.store")
              or sys.modules.get("share_store"))
    if module is not None:
        return module
    path = Path(__file__).resolve().parents[1] / "share/store.py"
    spec = importlib.util.spec_from_file_location("share_store", path)
    if spec is None or spec.loader is None:
        raise BackupError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["share_store"] = module
    spec.loader.exec_module(module)
    return module


def _load_share_secret_protection():
    """Load the product owner of the share-key AES-GCM scheme."""
    import importlib.util
    module = sys.modules.get("share_secret_protection")
    if module is not None:
        return module
    path = Path(__file__).resolve().parents[1] / "share/secret_protection.py"
    spec = importlib.util.spec_from_file_location(
        "share_secret_protection", path)
    if spec is None or spec.loader is None:
        raise BackupKeyError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["share_secret_protection"] = module
    spec.loader.exec_module(module)
    return module


def _load_magpie_prices():
    """Load the product's Magpie price module (format owner)."""
    import importlib.util
    module = sys.modules.get("console_magpie_prices")
    if module is not None:
        return module
    path = Path(__file__).resolve().parents[1] / "console/magpie_prices.py"
    spec = importlib.util.spec_from_file_location(
        "console_magpie_prices", path)
    if spec is None or spec.loader is None:
        raise BackupMagpieError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["console_magpie_prices"] = module
    spec.loader.exec_module(module)
    return module


def _read_magpie_snapshot_bytes(data: bytes) -> dict:
    """Validate snapshot bytes with the product validator (fail-closed)."""
    module = _load_magpie_prices()
    with tempfile.TemporaryDirectory(prefix="um-magpie-snap-") as tmp:
        path = Path(tmp) / MAGPIE_SNAPSHOT_NAME
        try:
            path.write_bytes(data)
        except OSError as exc:
            raise BackupMagpieError(
                f"cannot stage magpie snapshot bytes: {exc}") from exc
        try:
            return module.load_snapshot(path)
        except Exception as exc:  # noqa: BLE001 - product validator refusal
            raise BackupMagpieError(
                "magpie price snapshot is invalid: "
                f"{type(exc).__name__}: {exc}") from exc


def _read_magpie_status_bytes(data: bytes):
    """Validate status bytes with the product loader (fail-closed)."""
    module = _load_magpie_prices()
    with tempfile.TemporaryDirectory(prefix="um-magpie-status-") as tmp:
        path = Path(tmp) / MAGPIE_STATUS_NAME
        try:
            path.write_bytes(data)
        except OSError as exc:
            raise BackupMagpieError(
                f"cannot stage magpie status bytes: {exc}") from exc
        try:
            status = module.load_status(path)
        except Exception as exc:  # noqa: BLE001 - product validator refusal
            raise BackupMagpieError(
                "magpie price status is invalid: "
                f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(status, dict):
        raise BackupMagpieError("magpie price status is not an object")
    if status.get("state") == "failed" and status.get("failure") in (
            "价格同步状态损坏", "价格同步状态格式不受支持"):
        raise BackupMagpieError("magpie price status is corrupt")
    return status


def _magpie_closure(snapshot_path, status_path=None) -> dict:
    """Return the Magpie version closure, never inventing a version."""
    module = _load_magpie_prices()
    try:
        closure = module.backup_manifest(snapshot_path, status_path)
    except Exception as exc:  # noqa: BLE001 - product validator refusal
        raise BackupMagpieError(
            "magpie price snapshot refused: "
            f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(closure, dict) \
            or closure.get("format") != MAGPIE_CLOSURE_FORMAT:
        raise BackupMagpieError("magpie price closure format is not supported")
    current = closure.get("current")
    versions = closure.get("versions")
    if not isinstance(current, dict) or not isinstance(versions, list) \
            or not versions:
        raise BackupMagpieError(
            "magpie price snapshot has no version closure")
    if not isinstance(current.get("version"), str) or not current["version"]:
        raise BackupMagpieError(
            "magpie price snapshot has no current version")
    return closure


def restore_for_recovery(backup_path, key, *, target_dir=None,
                          target_db_path=None, target_ledger_path=None,
                          target_budget_db_path=None, target_extra_dir=None,
                          target_price_table_path=None,
                          target_magpie_dir=None,
                          target_security_state_path=None,
                          target_share_secret_key_path=None,
                          target_share_secret_owner=None,
                          target_share_secret_group=None,
                          security_state_path=None, security_state=None,
                          security_adapter=None,
                          require_min_generation=None,
                          price_table_path=None, config_paths=None,
                          allow_existing=False, require_magpie=False,
                          require_share_secret=False, now=None) -> dict:
    """Restore an old backup ONLY behind the independent latest fence.

    The independent security state is required: a caller cannot omit the
    generation floor, and a remote state that cannot be confirmed is
    fail-closed.  The restored store then replays that state so an old backup
    can never resurrect a Key revoked after the backup was taken.

    ``target_dir`` keeps the P1 drill layout; a deployment recovery may pass
    explicit live paths instead (store, ledger, budget, config, price table,
    Magpie state and security state).  ``price_table_path``/``config_paths`` are
    retained for P1 call compatibility and do NOT name restore targets.

    ``require_share_secret`` makes the deployment entry refuse a package that
    cannot restore the AES-GCM share-key file, before any target is replaced.
    """
    target = Path(target_dir) if target_dir is not None else None
    if target is not None:
        target_db_path = target_db_path or target / "share-store.db"
        target_ledger_path = target_ledger_path or target / "usage-share-backend.jsonl"
        target_budget_db_path = (target_budget_db_path
                                 or target / "budget.db")
        target_extra_dir = target_extra_dir or target / "config"
        target_security_state_path = (target_security_state_path
                                      or target / "security-state.json")
        target_magpie_dir = target_magpie_dir or target / "data"
        target_share_secret_key_path = (
            target_share_secret_key_path
            or target / "data" / SHARE_SECRET_NAME)
    if target_db_path is None or target_ledger_path is None:
        raise BackupError(
            "target_dir or explicit target_db_path/target_ledger_path is "
            "required")
    state = None
    if security_adapter is not None:
        latest = latest_security_state(security_adapter)
        if not latest["confirmed"]:
            raise BackupSecurityError(
                "remote security state is not confirmed: "
                + str(latest.get("reason")))
        state = latest["state"]
    elif security_state_path is not None:
        state = read_security_state(security_state_path)
    elif security_state is not None:
        state = _validate_security_state(security_state)
    else:
        raise BackupSecurityError(
            "an independent security state is required; the generation "
            "floor cannot be omitted")
    floor = max(0 if require_min_generation is None
                else int(require_min_generation),
                state["generation_floor"])
    result = restore_backup(
        backup_path, key,
        target_db_path=target_db_path,
        target_ledger_path=target_ledger_path,
        target_budget_db_path=target_budget_db_path,
        target_extra_dir=target_extra_dir,
        target_price_table_path=target_price_table_path,
        target_magpie_dir=target_magpie_dir,
        target_share_secret_key_path=target_share_secret_key_path,
        target_share_secret_owner=target_share_secret_owner,
        target_share_secret_group=target_share_secret_group,
        target_security_state_path=target_security_state_path,
        require_min_generation=floor, require_budget=False,
        require_config=True, require_security=True,
        require_magpie=require_magpie,
        require_share_secret=require_share_secret,
        security_state=state, allow_existing=allow_existing)
    store_module = _load_share_store()
    store = store_module.ShareStore(target_db_path)
    try:
        fence = store.apply_security_fence(state)
    finally:
        store.close()
    result["security_fence"] = fence
    result["security_floor"] = floor
    result["security_confirmed"] = True
    return result


# --------------------------------------------------- deployment assembly (S2)
def _effective_share_secret_key(etc, console_var, current) -> str:
    """The share-key path the running console actually uses.

    Precedence matches ``share_secret_protection.default_key_file`` and the
    installed console environment:

    1. the process environment override (systemd may inject it);
    2. ``<etc>/console.env`` as installed by ``install.sh``;
    3. the product's release-relative default ``<current>/data/share-secret.key``.

    The third step must be the product default, not a different console-state
    path: an older deployment without a console.env line encrypted its rows with
    the release-relative key.  Returning another path would make the backup and
    the restore follow a key the console never used.  A console.env that exists
    but cannot be read is refused instead of silently falling back to a different
    key.  The returned value is never logged.
    """
    override = os.environ.get(SHARE_SECRET_KEY_ENV, "").strip()
    if override:
        return override
    env_path = Path(etc) / "console.env"
    try:
        text = env_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except (OSError, UnicodeDecodeError) as exc:
        raise BackupError(
            f"console environment file unreadable: {env_path}: {exc}") from exc
    configured = _env_assignments(text).get(SHARE_SECRET_KEY_ENV, "").strip()
    if configured:
        return configured
    return f"{current}/data/{SHARE_SECRET_NAME}"


def deployment_paths(*, current="/opt/um-share/current",
                     etc="/etc/um-share",
                     var="/var/lib/um-share",
                     console_var="/var/lib/um-share-console") -> dict:
    """The ONE deployment layout shared by the scripts and the offline tools.

    Price table and release code stay in the release directory; every mutable
    file (Magpie snapshot/status, backup state/spool/remote, security journal,
    console store and its AES-GCM share-key file) lives under the writable state
    directories.  ``budget_db`` remains only so an older offline package can be
    inspected/restored explicitly; D18-11 removed it from the default assembly.
    """
    return {
        "current": current,
        "etc": etc,
        "var": var,
        "console": console_var,
        "console_var": console_var,
        "store": f"{console_var}/share-store.db",
        "ledger": f"{console_var}/share-backend/usage-share-backend.jsonl",
        "key_file": f"{etc}/backup.key",
        "remote_dir": f"{var}/backup-remote",
        "state": f"{var}/backup-freshness.json",
        "spool": f"{var}/backup-spool",
        "budget_db": f"{var}/budget.db",
        "price_table": f"{etc}/prices.json",
        "magpie_snapshot": f"{var}/magpie-prices.snapshot.json",
        "magpie_status": f"{var}/magpie-prices.status.json",
        "config": f"{etc}/share-management.json",
        "security_state": f"{var}/security-state.json",
        # The POSIX AES-GCM share-key file is persistent deployment state; it
        # lives where the running console's effective config names it, so a
        # release switch never moves it.  With no console.env line the product
        # default is the release-relative <current>/data/share-secret.key.
        "share_secret_key": _effective_share_secret_key(
            etc, console_var, current),
    }


BROKER_REQUIRED_ARGS = ("CHAT2API_PRICE_TABLE_FILE",)
CONSOLE_REQUIRED_ENV = (
    "UM_CLOUD_SECURITY_STATE",
    "USAGE_PRICE_FILE",
    "CHAT2API_PRICE_TABLE_FILE",
    "MAGPIE_PRICE_SNAPSHOT",
    "MAGPIE_PRICE_STATUS",
    # The share store rows are AES-GCM encrypted with this file.  An env that
    # omits it would make the console fall back to the release-relative
    # default, which vanishes on the next version switch.
    "CHAT2API_SHARE_SECRET_KEY_FILE",
)


def _env_assignments(text):
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] \
                and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def deployment_assembly_problems(role, unit_text, env_text) -> list:
    """Return every missing price/security fence item for one unit role.

    The service must fail its START when the installed unit/env no longer names
    the metering price table (broker) or the security/price/Magpie read face
    (console).  Silently starting without those facts would make a drifting
    deployment look like a working one.
    """
    problems = []
    if not isinstance(unit_text, str):
        raise DeploymentAssemblyError("unit text must be a string")
    if not isinstance(env_text, str):
        raise DeploymentAssemblyError("env text must be a string")
    values = _env_assignments(env_text)
    if role == "broker":
        for flag in BROKER_REQUIRED_ARGS:
            if flag not in unit_text:
                problems.append(
                    f"broker unit does not pass {flag}; the 8100 gateway "
                    "would start without a metering price table")
        if "EnvironmentFile=" not in unit_text:
            problems.append(
                "broker unit does not load an EnvironmentFile")
        if not values.get("UM_SHARE_UID"):
            problems.append(
                "broker.env does not set UM_SHARE_UID")
    elif role == "console":
        if "EnvironmentFile=" not in unit_text:
            problems.append(
                "console unit does not load an EnvironmentFile")
        for key in CONSOLE_REQUIRED_ENV:
            if not values.get(key):
                problems.append(
                    f"console.env does not set {key}; the console read "
                    "face would report configured=false")
        if not values.get("UM_SHARE_UID"):
            problems.append(
                "console.env does not set UM_SHARE_UID")
    else:
        problems.append(f"unknown deployment role: {role!r}")
    return problems


def check_deployment_assembly(role, unit_path, env_path) -> dict:
    """Read one installed unit/env pair and refuse a drifting start."""
    try:
        unit_text = Path(unit_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise DeploymentAssemblyError(
            f"unit file unreadable: {unit_path}: {exc}") from exc
    try:
        env_text = Path(env_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise DeploymentAssemblyError(
            f"environment file unreadable: {env_path}: {exc}") from exc
    problems = deployment_assembly_problems(role, unit_text, env_text)
    if problems:
        raise DeploymentAssemblyError("; ".join(problems))
    return {"ok": True, "role": role, "unit": str(unit_path),
            "env": str(env_path)}


def deployment_backup(paths=None, *, key=None, now=None,
                      backup_id=None, require_share_secret=False) -> dict:
    """Run ONE offline backup checkpoint with every deployment path explicit.

    ``require_share_secret`` is the operational default for an explicit manual
    checkpoint: a package without the share-key file would look successful while
    being useless after a machine move.  The path is the one named by
    :func:`deployment_paths` (the console's effective configuration), never a
    newly invented default.  D18-11 removed the automatic timer and the
    pre-update checkpoint; this function remains an offline recovery tool.
    """
    layout = deployment_paths() if paths is None else dict(paths)
    store_path = Path(layout["store"])
    if not store_path.is_file():
        raise BackupError(
            f"share store is missing; refusing to back up an empty database: "
            f"{store_path}")
    share_secret_path = layout.get("share_secret_key")
    share_secret_key_path = None
    if share_secret_path is not None \
            and Path(share_secret_path).is_file():
        share_secret_key_path = share_secret_path
    elif require_share_secret:
        raise BackupError(
            "share secret key is missing; refusing a backup that cannot "
            f"decrypt the store after recovery: {share_secret_path}")
    material = (load_backup_key(layout["key_file"])
                if key is None else _validate_key(key))
    security_state = None
    security_path = Path(layout["security_state"])
    if security_path.is_file():
        security_state = read_security_state(security_path)
    store_module = _load_share_store()
    store = store_module.ShareStore(store_path)
    try:
        return run_backup_cycle(
            store, layout["ledger"], material,
            LocalObjectStore(layout["remote_dir"]),
            state_path=layout["state"], spool_dir=layout["spool"],
            price_table_path=layout["price_table"],
            magpie_snapshot_path=layout["magpie_snapshot"],
            magpie_status_path=layout["magpie_status"],
            config_paths=[layout["config"]],
            security_state=security_state, now=now, backup_id=backup_id,
            share_secret_key_path=share_secret_key_path)
    finally:
        store.close()


def deployment_restore(backup_path, *, key=None, paths=None,
                      security_state_path=None, now=None,
                      share_secret_owner=None,
                      share_secret_group=None) -> dict:
    """EXPLICIT recovery only: restore live data behind the security fence.

    The deployment entry ALWAYS requires the AES-GCM share-key file.  A package
    without it would replace the live database while keeping an unrelated key,
    reporting success even though every restored Key is undecryptable; the restore
    therefore refuses before any target is replaced.

    ``share_secret_owner``/``share_secret_group`` are the console uid/gid the
    restored key file must belong to.  The recovery runs as root, while the
    console's key-file gate accepts only a 0600 file owned by its own uid; without
    this the restored custom-path key is unreadable.
    """
    layout = deployment_paths() if paths is None else dict(paths)
    material = (load_backup_key(layout["key_file"])
                 if key is None else _validate_key(key))
    state_path = (security_state_path or layout["security_state"])
    return restore_for_recovery(
        backup_path, material,
        target_db_path=layout["store"],
        target_ledger_path=layout["ledger"],
        target_budget_db_path=layout["budget_db"],
        target_extra_dir=layout["etc"],
        target_price_table_path=layout["price_table"],
        target_magpie_dir=layout["var"],
        target_security_state_path=layout["security_state"],
        target_share_secret_key_path=layout.get("share_secret_key"),
        target_share_secret_owner=share_secret_owner,
        target_share_secret_group=share_secret_group,
        security_state_path=state_path,
        allow_existing=True, require_magpie=True,
        require_share_secret=True, now=now)


# ---------------------------------------------------------- recovery kit
def seal_recovery_kit(path, *, key, store_identity, passphrase,
                      iterations=SCRYPT_N) -> Path:
    """Seal the backup key + object-store identity under a user passphrase.

    The kit is NOT stored beside the plaintext backup and is never the only
    copy on the machine being recovered.  The user keeps the passphrase
    separately; losing the source machine therefore does not lose the material.
    """
    material = _validate_key(key)
    if not isinstance(store_identity, dict) or not store_identity:
        raise RecoveryKitError("store identity must be a non-empty object")
    if not isinstance(passphrase, str) \
            or len(passphrase) < MIN_PASSPHRASE_CHARS:
        raise RecoveryKitError(
            f"recovery passphrase must be at least {MIN_PASSPHRASE_CHARS} "
            "characters")
    if isinstance(iterations, bool) or not isinstance(iterations, int) \
            or iterations < 2 ** 12:
        raise RecoveryKitError("scrypt iterations are too weak")
    salt = secrets.token_bytes(SALT_SIZE)
    nonce = os.urandom(NONCE_SIZE)
    derived = hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt, n=iterations,
        r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_SIZE)
    payload = _json_bytes({
        "format": RECOVERY_KIT_FORMAT,
        "key": material.hex(),
        "store_identity": store_identity,
        "created_at": _utc_iso(time.time()),
    })
    AESGCM = _load_aead()
    sealed = AESGCM(derived).encrypt(nonce, payload, RECOVERY_KIT_MAGIC)
    header = (RECOVERY_KIT_MAGIC + struct.pack(">I", iterations)
              + salt + nonce)
    target = Path(path)
    _atomic_write(target, header + sealed, mode=0o600)
    return target


def open_recovery_kit(path, passphrase) -> dict:
    """Open the sealed recovery kit with the user-held passphrase."""
    if not isinstance(passphrase, str) or not passphrase:
        raise RecoveryKitError("recovery passphrase is required")
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise RecoveryKitError(
            f"recovery kit unreadable: {path}") from exc
    prefix = len(RECOVERY_KIT_MAGIC) + 4 + SALT_SIZE + NONCE_SIZE
    if len(raw) < prefix + TAG_SIZE:
        raise RecoveryKitError("recovery kit is truncated")
    if raw[:len(RECOVERY_KIT_MAGIC)] != RECOVERY_KIT_MAGIC:
        raise RecoveryKitError("recovery kit magic does not match")
    offset = len(RECOVERY_KIT_MAGIC)
    iterations = struct.unpack(">I", raw[offset:offset + 4])[0]
    offset += 4
    salt = raw[offset:offset + SALT_SIZE]
    offset += SALT_SIZE
    nonce = raw[offset:offset + NONCE_SIZE]
    offset += NONCE_SIZE
    derived = hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt, n=iterations,
        r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_SIZE)
    AESGCM = _load_aead()
    try:
        from cryptography.exceptions import InvalidTag
    except ImportError as exc:  # pragma: no cover
        raise RecoveryKitError("AES-GCM is unavailable") from exc
    try:
        payload = AESGCM(derived).decrypt(nonce, raw[offset:],
                                          RECOVERY_KIT_MAGIC)
    except InvalidTag as exc:
        raise RecoveryKitError(
            "recovery kit authentication failed (wrong passphrase or "
            "damaged kit)") from exc
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RecoveryKitError(
            "recovery kit payload is not valid JSON") from exc
    if not isinstance(decoded, dict) \
            or decoded.get("format") != RECOVERY_KIT_FORMAT:
        raise RecoveryKitError("recovery kit format is not supported")
    try:
        material = _validate_key(bytes.fromhex(decoded["key"]))
    except (KeyError, TypeError, ValueError, BackupKeyError) as exc:
        raise RecoveryKitError("recovery kit key is invalid") from exc
    identity = decoded.get("store_identity")
    if not isinstance(identity, dict) or not identity:
        raise RecoveryKitError("recovery kit store identity is invalid")
    return {"key": material, "store_identity": identity,
            "created_at": decoded.get("created_at")}


# ---------------------------------------------------------- freshness checkpoint
def _read_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise BackupError(f"backup freshness state unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise BackupError(f"backup freshness state is not an object: {path}")
    return value


class BackupFreshnessGuard:
    """Track the last successful backup cutoff without pausing sharing.

    C9 is explicit: a usage-backup failure keeps the share serving traffic and
    raises an alert.  This guard therefore never exposes a pause action; the budget
    guard is the only component allowed to pause sharing, and for a different
    reason (C15).
    """

    def __init__(self, state_path, *, target_seconds=3600):
        self.state_path = Path(state_path)
        if target_seconds <= 0:
            raise ValueError("target_seconds must be positive")
        self.target_seconds = int(target_seconds)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state = _read_json(self.state_path, {})

    def _save(self) -> None:
        _atomic_write(self.state_path, _json_bytes(self._state), mode=0o600)

    def record_success(self, result, *, now=None) -> dict:
        epoch = time.time() if now is None else float(now)
        if not isinstance(result, dict):
            raise BackupError("checkpoint result must be a dict")
        previous = self._state
        state = {
            "last_success_at": _utc_iso(epoch),
            "last_success_epoch": epoch,
            "last_backup_id": result.get("backup_id"),
            "last_generation": result.get("config_generation"),
            "last_protected_offset": int(result.get("ledger_offset", 0)),
            "last_protected_records": int(result.get("ledger_records", 0)),
            "last_protected_partial_bytes": int(
                result.get("ledger_partial_bytes", 0)),
            "consecutive_failures": 0,
            "last_failure_at": None,
            "last_failure_error": None,
            "first_failure_at": None,
            "previous_success_at": previous.get("last_success_at"),
        }
        self._state = state
        self._save()
        return self.status(now=epoch)

    def record_failure(self, error, *, now=None) -> dict:
        epoch = time.time() if now is None else float(now)
        state = dict(self._state)
        failures = int(state.get("consecutive_failures") or 0) + 1
        state["consecutive_failures"] = failures
        state["last_failure_at"] = _utc_iso(epoch)
        state["last_failure_epoch"] = epoch
        state["last_failure_error"] = f"{type(error).__name__}: {error}"
        if not state.get("first_failure_at"):
            state["first_failure_at"] = state["last_failure_at"]
        self._state = state
        self._save()
        return self.status(now=epoch)

    def run_checkpoint(self, checkpoint_fn, *, now=None) -> dict:
        """Run one checkpoint; failure is recorded and sharing stays enabled."""
        epoch = time.time() if now is None else float(now)
        try:
            result = checkpoint_fn()
        except Exception as exc:  # noqa: BLE001 - C9 keeps serving
            status = self.record_failure(exc, now=epoch)
            return {
                "ok": False,
                "sharing_paused": False,
                "alert": status["alert"],
                "status": status,
            }
        status = self.record_success(result, now=epoch)
        return {
            "ok": True,
            "sharing_paused": False,
            "alert": status["alert"],
            "status": status,
        }

    def status(self, *, now=None) -> dict:
        epoch = time.time() if now is None else float(now)
        state = self._state
        last_epoch = state.get("last_success_epoch")
        last_epoch = float(last_epoch) if isinstance(
            last_epoch, (int, float)) else None
        unprotected = None if last_epoch is None else max(0.0, epoch - last_epoch)
        within_target = (unprotected is not None
                         and unprotected <= self.target_seconds)
        failure_epoch = state.get("last_failure_epoch")
        failure_epoch = float(failure_epoch) if isinstance(
            failure_epoch, (int, float)) else None
        stale_failure = (failure_epoch is not None
                         and (last_epoch is None or failure_epoch > last_epoch))
        alert = None
        if last_epoch is None:
            alert = {
                "code": "no_successful_backup",
                "message": "no successful usage backup checkpoint yet",
            }
        elif stale_failure:
            alert = {
                "code": "backup_failed",
                "message": "the latest usage backup checkpoint failed; "
                           "sharing continues under C9",
                "last_failure_at": state.get("last_failure_at"),
                "last_failure_error": state.get("last_failure_error"),
                "consecutive_failures": int(
                    state.get("consecutive_failures") or 0),
            }
        elif not within_target:
            alert = {
                "code": "backup_stale",
                "message": "the last successful usage backup is older than the "
                           "target interval",
                "unprotected_seconds": round(unprotected, 3),
                "target_seconds": self.target_seconds,
            }
        return {
            "target_seconds": self.target_seconds,
            "last_success_at": state.get("last_success_at"),
            "last_success_epoch": last_epoch,
            "last_backup_id": state.get("last_backup_id"),
            "last_generation": state.get("last_generation"),
            "last_protected_offset": state.get("last_protected_offset"),
            "last_protected_records": state.get("last_protected_records"),
            "last_protected_partial_bytes": state.get(
                "last_protected_partial_bytes"),
            "unprotected_seconds": None if unprotected is None else round(
                unprotected, 3),
            "within_target": within_target,
            # A stale FAILURE is degraded even when it happened immediately
            # after a success: the latest checkpoint did not protect anything,
            # so "within_target" alone must not read as healthy.
            "degraded": stale_failure or not within_target,
            "consecutive_failures": int(
                state.get("consecutive_failures") or 0),
            "last_failure_at": state.get("last_failure_at"),
            "last_failure_error": state.get("last_failure_error"),
            "first_failure_at": state.get("first_failure_at"),
            "sharing_paused": False,
            "alert": alert,
        }


# ------------------------------------------------------------------ CLI
def _cli_store(path):
    module = _load_share_store()
    return module.ShareStore(path)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="UM share encrypted backup/restore checkpoint")
    parser.add_argument("--store")
    parser.add_argument("--ledger")
    parser.add_argument("--key-file")
    parser.add_argument("--remote-dir")
    parser.add_argument("--remote-prefix", default=BACKUP_PREFIX)
    parser.add_argument("--state")
    parser.add_argument("--spool")
    parser.add_argument("--price-table")
    parser.add_argument("--magpie-snapshot")
    parser.add_argument("--magpie-status")
    parser.add_argument("--config", action="append", default=[])
    parser.add_argument("--security-state")
    parser.add_argument("--print-freshness", action="store_true")
    parser.add_argument("--retry-pending", action="store_true")
    parser.add_argument("--check-deployment-assembly", nargs=3,
                        metavar=("ROLE", "UNIT", "ENV"))
    parser.add_argument("--deployment-restore", action="store_true")
    parser.add_argument("--deployment-current", default="/opt/um-share/current")
    parser.add_argument("--deployment-etc", default="/etc/um-share")
    parser.add_argument("--deployment-var", default="/var/lib/um-share")
    parser.add_argument("--deployment-console-var",
                        default="/var/lib/um-share-console")
    parser.add_argument("--share-secret-owner", type=int, default=None,
                        help="uid that must own the restored share-key file")
    parser.add_argument("--share-secret-group", type=int, default=None,
                        help="gid that must own the restored share-key file")
    parser.add_argument("--restore-backup")
    parser.add_argument("--restore-target")
    parser.add_argument("--restore-security-state")
    parser.add_argument("--require-magpie", action="store_true")
    parser.add_argument("--allow-existing", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    if args.check_deployment_assembly:
        role, unit_path, env_path = args.check_deployment_assembly
        try:
            result = check_deployment_assembly(role, unit_path, env_path)
        except DeploymentAssemblyError as exc:
            print(json.dumps({
                "ok": False, "role": role,
                "problems": [str(exc)],
            }, ensure_ascii=False))
            return 1
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.deployment_restore:
        if not args.restore_backup:
            parser.error("--deployment-restore requires --restore-backup")
        try:
            result = deployment_restore(
                args.restore_backup,
                paths=deployment_paths(
                    current=args.deployment_current,
                    etc=args.deployment_etc,
                    var=args.deployment_var,
                    console_var=args.deployment_console_var),
                security_state_path=args.restore_security_state,
                share_secret_owner=args.share_secret_owner,
                share_secret_group=args.share_secret_group)
        except (BackupError, OSError) as exc:
            print(json.dumps({"ok": False,
                              "error": f"{type(exc).__name__}: {exc}"},
                             ensure_ascii=False))
            return 1
        print(json.dumps({"ok": True, "restore": result},
                         ensure_ascii=False, sort_keys=True))
        return 0

    if args.print_freshness:
        if not args.state:
            parser.error("--state is required with --print-freshness")
        print(json.dumps(freshness_status(args.state), ensure_ascii=False,
                          sort_keys=True))
        return 0
    if args.restore_backup:
        if not (args.key_file and args.restore_target):
            parser.error(
                "--key-file and --restore-target are required with "
                "--restore-backup")
        try:
            key = load_backup_key(args.key_file)
            if args.restore_security_state:
                result = restore_for_recovery(
                    args.restore_backup, key,
                    target_dir=args.restore_target,
                    security_state_path=args.restore_security_state,
                    price_table_path=args.price_table,
                    config_paths=args.config or None,
                    require_magpie=args.require_magpie,
                    allow_existing=args.allow_existing)
            else:
                result = restore_backup(
                    args.restore_backup, key,
                    target_db_path=Path(args.restore_target) / "share-store.db",
                    target_ledger_path=(Path(args.restore_target)
                                        / "usage-share-backend.jsonl"),
                    target_budget_db_path=(Path(args.restore_target)
                                           / "budget.db"),
                    target_extra_dir=Path(args.restore_target) / "config",
                    target_price_table_path=(Path(args.restore_target)
                                             / "data" / "prices.json"),
                    target_magpie_dir=Path(args.restore_target) / "data",
                    require_min_generation=None,
                    require_magpie=args.require_magpie,
                    allow_existing=args.allow_existing)
        except (BackupError, OSError) as exc:
            print(json.dumps({"ok": False,
                              "error": f"{type(exc).__name__}: {exc}"},
                             ensure_ascii=False))
            return 1
        print(json.dumps({"ok": True, "restore": result}, ensure_ascii=False,
                          sort_keys=True))
        return 0

    missing = [name for name in
               ("store", "ledger", "key_file", "remote_dir", "state", "spool")
               if not getattr(args, name)]
    if missing:
        parser.error("missing required options: " + ", ".join(missing))
    try:
        key = load_backup_key(args.key_file)
    except BackupKeyError as exc:
        parser.error(str(exc))
    object_store = LocalObjectStore(args.remote_dir)
    security_state = None
    if args.security_state:
        try:
            security_state = read_security_state(args.security_state)
        except BackupError as exc:
            print(json.dumps({"ok": False,
                              "error": f"{type(exc).__name__}: {exc}"},
                             ensure_ascii=False))
            return 1
    try:
        if args.retry_pending:
            results = retry_pending_uploads(
                args.spool, key, object_store, state_path=args.state)
            print(json.dumps({"ok": all(item["ok"] for item in results),
                              "retried": results}, ensure_ascii=False,
                              sort_keys=True))
            return 0 if all(item["ok"] for item in results) else 1
        store = _cli_store(args.store)
        try:
            result = run_backup_cycle(
                store, args.ledger, key, object_store,
                state_path=args.state, spool_dir=args.spool,
                price_table_path=args.price_table,
                magpie_snapshot_path=args.magpie_snapshot,
                magpie_status_path=args.magpie_status,
                config_paths=args.config or None,
                security_state=security_state)
        finally:
            store.close()
    except (BackupError, OSError) as exc:
        print(json.dumps({"ok": False,
                          "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
