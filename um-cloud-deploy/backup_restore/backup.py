"""Backup creation, inspection, upload and checkpoint cycles."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .constants import (
    BACKUP_PREFIX, BACKUP_SUFFIX, FORMAT, MAGPIE_SNAPSHOT_NAME,
    MAGPIE_STATUS_NAME, MAX_BUDGET_BYTES, MAX_EXTRA_BYTES,
    MAX_SECURITY_BYTES, SHARE_SECRET_NAME,
)
from .errors import (
    BackupError, BackupGenerationError, BackupIntegrityError,
    BackupMagpieError, BackupUploadError, ObjectStoreError,
)
from .keyfile import _key_fingerprint, _validate_key
from .objectstore import ObjectStore
from .package import (
    _atomic_write, _build_package, _inspect_budget_bytes,
    _inspect_sqlite_bytes, _open, _parse_extra_files, _parse_package, _seal,
    _security_state_bytes, _snapshot_sqlite_bytes, _utc_iso,
    _validate_security_state, snapshot_ledger,
)
from .products import (
    _magpie_closure, _read_magpie_snapshot_bytes, _read_magpie_status_bytes,
)
from .recovery import BackupFreshnessGuard
from .security import publish_security_records

def _verify_share_secret_decrypts_store(db_bytes, secret_bytes):
    package = sys.modules[__package__]
    return package._verify_share_secret_decrypts_store(
        db_bytes, secret_bytes)


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
