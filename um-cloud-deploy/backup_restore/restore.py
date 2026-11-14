"""Restore, staging and rollback for sealed backup packages."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from .constants import SHARE_SECRET_NAME
from .errors import (
    BackupBudgetError, BackupConfigError, BackupError,
    BackupIntegrityError, BackupSecurityError, RestoreTargetExists,
)
from .backup import inspect_backup
from .package import (
    _inspect_budget_bytes, _security_state_bytes,
    _validate_security_state, read_security_state,
)
from .products import _load_share_store
from .security import latest_security_state

def _verify_share_secret_decrypts_store(db_bytes, secret_bytes):
    package = sys.modules[__package__]
    return package._verify_share_secret_decrypts_store(
        db_bytes, secret_bytes)


def restore_backup(backup_path, key, *, target_db_path, target_ledger_path,
                   require_min_generation=None, allow_existing=False,
                   target_budget_db_path=None, target_extra_dir=None,
                   target_security_state_path=None,
                   target_share_secret_key_path=None,
                   target_share_secret_owner=None,
                   target_share_secret_group=None,
                   security_state=None, security_state_path=None,
                   require_budget=False, require_config=False,
                   require_security=False,
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
    * ``require_config`` refuses a package without the config files.
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
    share_secret_name = metadata.get("share_secret_name")
    if not isinstance(share_secret_name, str) or not share_secret_name:
        share_secret_name = SHARE_SECRET_NAME
    share_secret_bytes = (
        inspected["extra_files"].get(share_secret_name)
        if metadata.get("share_secret_present") else None)
    config_files = {
        name: data for name, data in inspected["extra_files"].items()
        if name != share_secret_name}
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
    if require_config and not config_files:
        raise BackupConfigError(
            "the backup has no config payload required for restore")
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
    share_secret_target = (Path(target_share_secret_key_path)
                           if target_share_secret_key_path is not None
                           else None)
    if inspected["budget"] and budget_target is None and require_budget:
        raise BackupBudgetError(
            "the backup carries a budget snapshot but no target was named")
    if config_files and extra_target is None and require_config:
        raise BackupConfigError(
            "the backup carries config files but no target was named")
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
                     security_target, share_secret_target):
            if path is not None and path.exists():
                raise RestoreTargetExists(
                    f"restore target already exists: {path}")
        if extra_target is not None and extra_target.exists():
            raise RestoreTargetExists(
                f"restore target already exists: {extra_target}")

    for path in (db_target, ledger_target, budget_target,
                 security_target, share_secret_target):
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
    if extra_target is not None:
        extra_target.mkdir(parents=True, exist_ok=True)
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


def restore_for_recovery(backup_path, key, *, target_dir=None,
                          target_db_path=None, target_ledger_path=None,
                          target_budget_db_path=None, target_extra_dir=None,
                          target_security_state_path=None,
                          target_share_secret_key_path=None,
                          target_share_secret_owner=None,
                          target_share_secret_group=None,
                          security_state_path=None, security_state=None,
                          security_adapter=None,
                          require_min_generation=None,
                          config_paths=None,
                          allow_existing=False,
                          require_share_secret=False, now=None) -> dict:
    """Restore an old backup ONLY behind the independent latest fence.

    The independent security state is required: a caller cannot omit the
    generation floor, and a remote state that cannot be confirmed is
    fail-closed.  The restored store then replays that state so an old backup
    can never resurrect a Key revoked after the backup was taken.

    ``target_dir`` keeps the P1 drill layout; a deployment recovery may pass
    explicit live paths instead (store, ledger, budget, config and security
    state).  ``config_paths`` is retained for P1 call compatibility and does
    NOT name a restore target.

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
        target_share_secret_key_path=target_share_secret_key_path,
        target_share_secret_owner=target_share_secret_owner,
        target_share_secret_group=target_share_secret_group,
        target_security_state_path=target_security_state_path,
        require_min_generation=floor, require_budget=False,
        require_config=True, require_security=True,
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
