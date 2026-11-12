"""Recovery kit sealing/opening and backup freshness tracking."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import struct
import time
from pathlib import Path

from .constants import (
    KEY_SIZE, MIN_PASSPHRASE_CHARS, NONCE_SIZE, RECOVERY_KIT_FORMAT,
    RECOVERY_KIT_MAGIC, SALT_SIZE, SCRYPT_N, SCRYPT_P, SCRYPT_R, TAG_SIZE,
)
from .errors import BackupError, BackupKeyError, RecoveryKitError
from .keyfile import _validate_key
from .package import _atomic_write, _json_bytes, _load_aead, _utc_iso

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
