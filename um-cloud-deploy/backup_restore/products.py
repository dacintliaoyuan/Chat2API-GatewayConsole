"""Product-module loaders and Magpie price closure helpers."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

from .constants import (
    MAGPIE_CLOSURE_FORMAT, MAGPIE_SNAPSHOT_NAME, MAGPIE_STATUS_NAME,
    _RELEASE_ROOT,
)
from .errors import BackupError, BackupKeyError, BackupMagpieError

def _load_share_store():
    import importlib.util
    module = (sys.modules.get("share.store")
              or sys.modules.get("share_store"))
    if module is not None:
        return module
    path = _RELEASE_ROOT / "share/store.py"
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
    path = _RELEASE_ROOT / "share/secret_protection.py"
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
    path = _RELEASE_ROOT / "console/magpie_prices.py"
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
