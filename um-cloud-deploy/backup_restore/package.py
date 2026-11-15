"""Backup package format, ledger snapshot and AEAD helpers."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import struct
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .constants import (
    AAD, FINGERPRINT_SIZE, FORMAT, MAGIC, MAX_HEADER_BYTES,
    MAX_LEDGER_BYTES, MAX_SECURITY_BYTES, NONCE_SIZE, SECURITY_FORMAT,
    TAG_SIZE,
)
from .errors import (
    BackupAuthenticationError, BackupError, BackupIntegrityError,
    BackupKeyError, BackupKeyMismatch, BackupSecurityError,
)
from .keyfile import _decode_key, _key_fingerprint
from .products import _load_share_secret_protection

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
