"""Backup key file creation, reading and validation."""
from __future__ import annotations

import hashlib
import os
import secrets
import stat
from pathlib import Path

from .constants import FINGERPRINT_SIZE, HEX_KEY_SIZE, KEY_SIZE, MAX_KEY_FILE_BYTES
from .errors import BackupKeyError

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
