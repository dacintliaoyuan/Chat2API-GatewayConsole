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

from .constants import (
    AAD, BACKUP_PREFIX, BACKUP_SUFFIX, FINGERPRINT_SIZE, FORMAT, HEX_KEY_SIZE,
    KEY_SIZE, MAGIC,
    MAX_BUDGET_BYTES, MAX_EXTRA_BYTES, MAX_HEADER_BYTES,
    MAX_KEY_FILE_BYTES, MAX_LEDGER_BYTES, MAX_SECURITY_BYTES,
    MIN_PASSPHRASE_CHARS, NONCE_SIZE,
    RECOVERY_KIT_FORMAT, RECOVERY_KIT_MAGIC, SALT_SIZE, SCRYPT_N,
    SCRYPT_P, SCRYPT_R, SECURITY_FORMAT, SECURITY_PREFIX,
    SHARE_SECRET_KEY_ENV, SHARE_SECRET_NAME, TAG_SIZE, _RELEASE_ROOT,
)
from .errors import (
    BackupAuthenticationError, BackupBudgetError, BackupConfigError,
    BackupError, BackupGenerationError, BackupIntegrityError,
    BackupKeyError, BackupKeyMismatch,
    BackupSecurityError, BackupUploadError, DeploymentAssemblyError,
    ObjectStoreError, RecoveryKitError, RestoreTargetExists,
)
from .keyfile import (
    _decode_key, _key_fingerprint, _validate_key, load_backup_key,
    write_backup_key,
)
from .package import (
    _atomic_write, _build_package, _inspect_budget_bytes,
    _inspect_sqlite_bytes, _json_bytes, _last_complete_line_offset,
    _last_record_time, _load_aead, _open, _parse_extra_files,
    _parse_package, _seal, _security_state_bytes, _snapshot_sqlite_bytes,
    _utc_iso, _validate_security_state,
    _verify_share_secret_decrypts_store, _write_extra_files,
    read_security_state, snapshot_ledger, write_security_state,
)
from .products import (
    _load_share_secret_protection, _load_share_store,
)
from .objectstore import (
    LocalObjectStore, MemoryObjectStore, ObjectStore, _object_key,
)
from .security import (
    SecurityRecordStoreAdapter, latest_security_state,
    publish_security_records,
)
from .recovery import (
    BackupFreshnessGuard, _read_json, open_recovery_kit, seal_recovery_kit,
)
from .backup import (
    _record_budget_egress, create_backup, freshness_status, inspect_backup,
    remote_backup_status, retry_pending_uploads, run_backup_cycle,
    upload_backup,
)
from .restore import (
    _inspect_sqlite_file, _move_aside, _rollback_target, _sha256_file,
    _stage_bytes, restore_backup, restore_for_recovery,
)
from .assembly import (
    BROKER_REQUIRED_ARGS, CONSOLE_REQUIRED_ENV,
    _effective_share_secret_key, _env_assignments,
    check_deployment_assembly, deployment_assembly_problems,
    deployment_backup, deployment_paths, deployment_restore,
)
from .cli import _cli_store, main


__all__ = [
    "argparse",
    "hashlib",
    "hmac",
    "json",
    "os",
    "secrets",
    "sqlite3",
    "stat",
    "struct",
    "sys",
    "tempfile",
    "time",
    "uuid",
    "datetime",
    "timezone",
    "Path",
    "AAD",
    "BACKUP_PREFIX",
    "BACKUP_SUFFIX",
    "BROKER_REQUIRED_ARGS",
    "CONSOLE_REQUIRED_ENV",
    "FINGERPRINT_SIZE",
    "FORMAT",
    "HEX_KEY_SIZE",
    "KEY_SIZE",
    "MAGIC",
    "MAX_BUDGET_BYTES",
    "MAX_EXTRA_BYTES",
    "MAX_HEADER_BYTES",
    "MAX_KEY_FILE_BYTES",
    "MAX_LEDGER_BYTES",
    "MAX_SECURITY_BYTES",
    "MIN_PASSPHRASE_CHARS",
    "NONCE_SIZE",
    "RECOVERY_KIT_FORMAT",
    "RECOVERY_KIT_MAGIC",
    "SALT_SIZE",
    "SCRYPT_N",
    "SCRYPT_P",
    "SCRYPT_R",
    "SECURITY_FORMAT",
    "SECURITY_PREFIX",
    "SHARE_SECRET_KEY_ENV",
    "SHARE_SECRET_NAME",
    "TAG_SIZE",
    "BackupAuthenticationError",
    "BackupBudgetError",
    "BackupConfigError",
    "BackupError",
    "BackupGenerationError",
    "BackupIntegrityError",
    "BackupKeyError",
    "BackupKeyMismatch",
    "BackupSecurityError",
    "BackupUploadError",
    "DeploymentAssemblyError",
    "ObjectStoreError",
    "RecoveryKitError",
    "RestoreTargetExists",
    "write_backup_key",
    "load_backup_key",
    "snapshot_ledger",
    "read_security_state",
    "write_security_state",
    "create_backup",
    "inspect_backup",
    "restore_backup",
    "ObjectStore",
    "LocalObjectStore",
    "MemoryObjectStore",
    "upload_backup",
    "run_backup_cycle",
    "retry_pending_uploads",
    "freshness_status",
    "remote_backup_status",
    "SecurityRecordStoreAdapter",
    "publish_security_records",
    "latest_security_state",
    "restore_for_recovery",
    "deployment_paths",
    "deployment_assembly_problems",
    "check_deployment_assembly",
    "deployment_backup",
    "deployment_restore",
    "seal_recovery_kit",
    "open_recovery_kit",
    "BackupFreshnessGuard",
    "main",
]
