"""share/secret_protection.py — POSIX AES-GCM adapter for share keys.

Windows keeps its R11 DPAPI user-scope implementation in ``share_store``
unchanged; this module is imported lazily only on POSIX, so a Windows
deployment whose copy closure predates this file still starts normally.

The POSIX scheme is ``aesgcm-file-key-v1``: AES-256-GCM with a 32-byte key
read from ``CHAT2API_SHARE_SECRET_KEY_FILE`` (default
``<data>/share-secret.key``). The key file must hold 32 raw bytes or 64 hex
characters, must have mode 0600 and must be owned by the current uid. A
missing, damaged or loosely-permissioned key file raises
:class:`SecretProtectionError`; it never degrades to plaintext or to a
hash-only row.

``cryptography`` is imported lazily inside the AES-GCM path: Windows has no
``cryptography`` package and importing this module must keep working there.
Unknown ``secret_algo`` values raise instead of being guessed.

Provisioning the POSIX key (never checked into the repo, never logged)::

    openssl rand -hex 32 > data/share-secret.key
    chmod 600 data/share-secret.key

Losing the key makes existing ``aesgcm-file-key-v1`` rows unreadable (readback
answers undecryptable); their SHA-256 digest still verifies the key itself.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

# Scheme marker stored in keys.secret_algo. It is a stable identifier: a
# stored row must be decryptable by the scheme it names, never by inference.
SECRET_ALGO_AESGCM_FILE_KEY = "aesgcm-file-key-v1"

# POSIX key-file contract. The env override is the only way to relocate the
# file; the default lives next to the other gateway data.
KEY_FILE_ENV = "CHAT2API_SHARE_SECRET_KEY_FILE"
DEFAULT_KEY_FILE = (Path(__file__).resolve().parents[1]
                    / "data" / "share-secret.key")
KEY_SIZE = 32
HEX_KEY_SIZE = KEY_SIZE * 2
NONCE_SIZE = 12
TAG_SIZE = 16
# A valid key file is at most 64 hex characters plus a trailing newline; the
# generous bound only stops an accidental (or planted) huge file from being read.
MAX_KEY_FILE_BYTES = 1024
# Domain separation: a blob protected for this scheme cannot authenticate as a
# different scheme even if the same key material were ever reused.
_AAD = SECRET_ALGO_AESGCM_FILE_KEY.encode("ascii")


class SecretProtectionError(ValueError):
    """Protection, decryption or secret-file access refused.

    Callers must not substitute plaintext, an empty string, or a hash-only row:
    an unreachable plaintext is reported as such, not invented. It is a
    ``ValueError`` so existing fail-closed handlers keep working.
    """


def default_key_file() -> Path:
    """Key file path: the env override, else ``<data>/share-secret.key``."""
    override = os.environ.get(KEY_FILE_ENV)
    if override:
        return Path(override)
    return DEFAULT_KEY_FILE


def load_key_file(path=None) -> bytes:
    """Read and validate the AES-GCM key file.

    The POSIX mode/owner gate is evaluated on the descriptor that is actually
    read (``fstat``), so a file swapped in after the check cannot bypass it.
    The accepted contents are 32 raw bytes (used verbatim) or a 64-character
    hexadecimal text (surrounding whitespace ignored).
    """
    target = Path(path) if path is not None else default_key_file()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except OSError as exc:
        raise SecretProtectionError(
            f"share secret key file unavailable: {target}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SecretProtectionError(
                f"share secret key file is not a regular file: {target}")
        if os.name == "posix":
            if info.st_uid != os.getuid():
                raise SecretProtectionError(
                    "share secret key file is not owned by the current uid: "
                    f"{target}")
            mode = stat.S_IMODE(info.st_mode)
            if mode != 0o600:
                raise SecretProtectionError(
                    f"share secret key file must have mode 0600 "
                    f"(found {mode:04o}): {target}")
        chunks, total = [], 0
        while total <= MAX_KEY_FILE_BYTES:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
    except OSError as exc:
        raise SecretProtectionError(
            f"share secret key file unreadable: {target}") from exc
    finally:
        os.close(fd)
    if len(raw) > MAX_KEY_FILE_BYTES:
        raise SecretProtectionError(
            f"share secret key file is unexpectedly large: {target}")
    return _decode_key(raw, target)


def _decode_key(raw: bytes, target) -> bytes:
    if len(raw) == KEY_SIZE:
        return raw
    text = raw.strip()
    if len(text) == HEX_KEY_SIZE:
        try:
            material = bytes.fromhex(text.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SecretProtectionError(
                f"share secret key file is not valid hexadecimal: {target}") from exc
        if len(material) != KEY_SIZE:
            raise SecretProtectionError(
                "share secret key file must decode to exactly "
                f"{KEY_SIZE} bytes: {target}")
        return material
    raise SecretProtectionError(
        "share secret key file must hold 32 raw bytes or 64 hexadecimal "
        f"characters: {target}")


def _load_aead():
    # Lazy import: Windows has no cryptography package and importing the
    # module must keep working there.
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise SecretProtectionError(
            "AES-GCM share secret protection requires the cryptography "
            "package") from exc
    return AESGCM


def _validated_key(material) -> bytes:
    if not isinstance(material, (bytes, bytearray)) or len(material) != KEY_SIZE:
        raise SecretProtectionError(
            f"AES-GCM share secret key must be {KEY_SIZE} bytes")
    return bytes(material)


def _private_secret_path(directory, name) -> Path:
    """Join an explicit directory with a single validated file name.

    ``name`` must be one basename: no separators, no ``.``/``..`` and no NUL.
    The directory is deployment configuration and must be absolute. This is the
    only join used by the secret reader/writer, so a name can never traverse out
    of the caller's chosen directory.
    """
    if not directory:
        raise SecretProtectionError("secret file directory is required")
    if not isinstance(name, str) or not name or name in (".", "..") \
            or "/" in name or "\\" in name or "\x00" in name:
        raise SecretProtectionError("secret file name is not a basename")
    root = Path(directory).expanduser()
    if not root.is_absolute():
        raise SecretProtectionError(
            "secret file directory must be absolute")
    return root / name


def check_private_secret_info(info, target, *, label="secret file") -> None:
    """Fail closed unless a secret file is regular and, on POSIX, 0600/current uid."""
    if not stat.S_ISREG(info.st_mode):
        raise SecretProtectionError(
            f"{label} is not a regular file: {target}")
    if os.name != "posix":
        return
    if info.st_uid != os.getuid():
        raise SecretProtectionError(
            f"{label} is not owned by the current uid: {target}")
    mode = stat.S_IMODE(info.st_mode)
    if mode != 0o600:
        raise SecretProtectionError(
            f"{label} must have mode 0600 (found {mode:04o}): {target}")


def read_private_secret_file(directory, name, *, max_bytes=1024,
                             label="secret file") -> bytes:
    """Read a local secret after the POSIX 0600/current-uid gate.

    Windows keeps its existing file-read behavior (the ACL boundary is separate);
    POSIX rejects a missing, non-regular, foreign-owned or wider-than-0600 file
    instead of reading it. The caller maps the exception; this helper never logs
    or returns the secret on failure.
    """
    target = _private_secret_path(directory, name)
    try:
        with target.open("rb") as handle:
            check_private_secret_info(os.fstat(handle.fileno()), target,
                                     label=label)
            raw = handle.read(max_bytes + 1)
    except OSError as exc:
        raise SecretProtectionError(
            f"{label} unreadable: {target}") from exc
    if len(raw) > max_bytes:
        raise SecretProtectionError(
            f"{label} is unexpectedly large: {target}")
    return raw


def write_private_secret_file(directory, name, data: bytes) -> None:
    """Write a local secret; on POSIX force the created/truncated file to 0600.

    An existing file is explicitly ``chmod``-ed to 0600, so a wider inherited
    mode cannot survive. A chmod/ownership failure fails closed instead of
    silently leaving a world-readable secret behind. Windows keeps the existing
    write path (the ACL boundary is separate).
    """
    target = _private_secret_path(directory, name)
    if os.name != "posix":
        with target.open("wb") as handle:
            handle.write(data)
        return
    if target.exists():
        # Inspect owner/type BEFORE truncation: a foreign-owned file must be
        # refused without destroying its contents.
        with target.open("r+b") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise SecretProtectionError(
                    f"secret file is not a regular file: {target}")
            if info.st_uid != os.getuid():
                raise SecretProtectionError(
                    f"secret file is not owned by the current uid: {target}")
            os.fchmod(handle.fileno(), 0o600)
            handle.truncate(0)
            handle.seek(0)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return
    with target.open("xb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def read_private_secret_path(path, *, max_bytes=1024,
                             label="secret file") -> bytes:
    """Path-form wrapper: normalise to an absolute directory + basename."""
    target = Path(path).expanduser()
    return read_private_secret_file(target.resolve().parent, target.name,
                                   max_bytes=max_bytes, label=label)


def write_private_secret_path(path, data: bytes) -> None:
    """Path-form wrapper: normalise to an absolute directory + basename."""
    target = Path(path).expanduser()
    target.resolve().parent.mkdir(parents=True, exist_ok=True)
    write_private_secret_file(target.resolve().parent, target.name, data)


def protect_secret_aesgcm(secret: str, key: bytes | None = None) -> bytes:
    """AES-256-GCM seal: ``nonce || ciphertext || tag``.

    ``key`` is only for tests and callers that already hold the key material;
    the default path always reads and validates the key file.
    """
    if os.name != "posix":
        raise SecretProtectionError(
            "aesgcm-file-key-v1 protection can only run on POSIX")
    AESGCM = _load_aead()
    material = _validated_key(key if key is not None else load_key_file())
    nonce = os.urandom(NONCE_SIZE)
    sealed = AESGCM(material).encrypt(nonce, secret.encode("utf-8"), _AAD)
    return nonce + sealed


def unprotect_secret_aesgcm(blob: bytes, key: bytes | None = None) -> str:
    """Open an ``aesgcm-file-key-v1`` blob; authentication failure raises."""
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise SecretProtectionError("share secret ciphertext must be bytes")
    data = bytes(blob)
    if len(data) < NONCE_SIZE + TAG_SIZE:
        raise SecretProtectionError("share secret ciphertext is truncated")
    AESGCM = _load_aead()
    try:
        from cryptography.exceptions import InvalidTag
    except ImportError as exc:  # pragma: no cover - AESGCM implies it
        raise SecretProtectionError(
            "AES-GCM share secret protection requires the cryptography "
            "package") from exc
    material = _validated_key(key if key is not None else load_key_file())
    try:
        plaintext = AESGCM(material).decrypt(
            data[:NONCE_SIZE], data[NONCE_SIZE:], _AAD)
    except InvalidTag as exc:
        raise SecretProtectionError(
            "share secret ciphertext failed authentication (wrong key or "
            "damaged blob)") from exc
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretProtectionError(
            "share secret plaintext is not UTF-8") from exc


def unprotect_secret(blob: bytes, algo: str | None = None,
                     key: bytes | None = None) -> str:
    """Decrypt an ``aesgcm-file-key-v1`` blob strictly by its marker.

    ``algo=None`` is refused: a POSIX adapter never assumes a scheme. Unknown
    or foreign markers (e.g. ``dpapi-user-scope-v1``) also raise here; the
    store owns the cross-platform dispatch. ``key`` is only for callers that
    already hold the material (e.g. verifying a backup key before installing
    it); the default path reads and validates the key file.
    """
    if algo is None:
        raise SecretProtectionError(
            "secret_algo is required to decrypt a share secret")
    if algo != SECRET_ALGO_AESGCM_FILE_KEY:
        raise SecretProtectionError(
            f"unknown share secret algorithm {algo!r}")
    if key is None and os.name != "posix":
        raise SecretProtectionError(
            "aesgcm-file-key-v1 ciphertext can only be decrypted on POSIX")
    return unprotect_secret_aesgcm(blob, key)
