"""Predictable backup/restore refusal exceptions."""
from __future__ import annotations

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
    """The backup does not carry the config required for restore."""


class DeploymentAssemblyError(BackupError):
    """A deployment unit/env is missing the budget/backup wiring it requires."""


class ObjectStoreError(BackupError):
    """The object-store adapter refused or failed an operation."""


class BackupUploadError(ObjectStoreError):
    """A backup object could not be uploaded (it stays in the spool)."""


class RecoveryKitError(BackupError):
    """The recovery kit is missing, malformed or the passphrase is wrong."""
