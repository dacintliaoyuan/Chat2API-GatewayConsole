"""Constants shared by the backup/restore package."""
from __future__ import annotations

import sys
from pathlib import Path

_RELEASE_ROOT = Path(__file__).resolve().parents[2]
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
