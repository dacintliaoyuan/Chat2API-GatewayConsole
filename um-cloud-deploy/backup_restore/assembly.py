"""Deployment path assembly and explicit recovery entry points."""
from __future__ import annotations

import os
from pathlib import Path

from .backup import run_backup_cycle
from .constants import SHARE_SECRET_KEY_ENV, SHARE_SECRET_NAME
from .errors import BackupError, DeploymentAssemblyError
from .keyfile import _validate_key, load_backup_key
from .objectstore import LocalObjectStore
from .package import read_security_state
from .products import _load_share_store
from .restore import restore_for_recovery

def _effective_share_secret_key(etc, console_var, current) -> str:
    """The share-key path the running console actually uses.

    Precedence matches ``share_secret_protection.default_key_file`` and the
    installed console environment:

    1. the process environment override (systemd may inject it);
    2. ``<etc>/console.env`` as installed by ``install.sh``;
    3. the product's release-relative default ``<current>/data/share-secret.key``.

    The third step must be the product default, not a different console-state
    path: an older deployment without a console.env line encrypted its rows with
    the release-relative key.  Returning another path would make the backup and
    the restore follow a key the console never used.  A console.env that exists
    but cannot be read is refused instead of silently falling back to a different
    key.  The returned value is never logged.
    """
    override = os.environ.get(SHARE_SECRET_KEY_ENV, "").strip()
    if override:
        return override
    env_path = Path(etc) / "console.env"
    try:
        text = env_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except (OSError, UnicodeDecodeError) as exc:
        raise BackupError(
            f"console environment file unreadable: {env_path}: {exc}") from exc
    configured = _env_assignments(text).get(SHARE_SECRET_KEY_ENV, "").strip()
    if configured:
        return configured
    return f"{current}/data/{SHARE_SECRET_NAME}"


def deployment_paths(*, current="/opt/um-share/current",
                     etc="/etc/um-share",
                     var="/var/lib/um-share",
                     console_var="/var/lib/um-share-console") -> dict:
    """The ONE deployment layout shared by the scripts and the offline tools.

    Release code stays in the release directory; every mutable file (backup
    state/spool/remote, security journal, console store and its AES-GCM share-key
    file) lives under the writable state directories.  ``budget_db`` remains only
    so an older offline package can be inspected/restored explicitly; D18-11
    removed it from the default assembly.
    """
    return {
        "current": current,
        "etc": etc,
        "var": var,
        "console": console_var,
        "console_var": console_var,
        "store": f"{console_var}/share-store.db",
        "ledger": f"{console_var}/share-backend/usage-share-backend.jsonl",
        "key_file": f"{etc}/backup.key",
        "remote_dir": f"{var}/backup-remote",
        "state": f"{var}/backup-freshness.json",
        "spool": f"{var}/backup-spool",
        "budget_db": f"{var}/budget.db",
        "config": f"{etc}/share-management.json",
        "security_state": f"{var}/security-state.json",
        # The POSIX AES-GCM share-key file is persistent deployment state; it
        # lives where the running console's effective config names it, so a
        # release switch never moves it.  With no console.env line the product
        # default is the release-relative <current>/data/share-secret.key.
        "share_secret_key": _effective_share_secret_key(
            etc, console_var, current),
    }


BROKER_REQUIRED_ARGS = ()
CONSOLE_REQUIRED_ENV = (
    "UM_CLOUD_SECURITY_STATE",
    # The share store rows are AES-GCM encrypted with this file.  An env that
    # omits it would make the console fall back to the release-relative
    # default, which vanishes on the next version switch.
    "CHAT2API_SHARE_SECRET_KEY_FILE",
)


def _env_assignments(text):
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] \
                and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def deployment_assembly_problems(role, unit_text, env_text) -> list:
    """Return every missing security fence item for one unit role.

    The service must fail its START when the installed unit/env no longer names
    the security read face (console).  Silently starting without those facts
    would make a drifting deployment look like a working one.
    """
    problems = []
    if not isinstance(unit_text, str):
        raise DeploymentAssemblyError("unit text must be a string")
    if not isinstance(env_text, str):
        raise DeploymentAssemblyError("env text must be a string")
    values = _env_assignments(env_text)
    if role == "broker":
        if "EnvironmentFile=" not in unit_text:
            problems.append(
                "broker unit does not load an EnvironmentFile")
        if not values.get("UM_SHARE_UID"):
            problems.append(
                "broker.env does not set UM_SHARE_UID")
    elif role == "console":
        if "EnvironmentFile=" not in unit_text:
            problems.append(
                "console unit does not load an EnvironmentFile")
        for key in CONSOLE_REQUIRED_ENV:
            if not values.get(key):
                problems.append(
                    f"console.env does not set {key}; the console read "
                    "face would report configured=false")
        if not values.get("UM_SHARE_UID"):
            problems.append(
                "console.env does not set UM_SHARE_UID")
    else:
        problems.append(f"unknown deployment role: {role!r}")
    return problems


def check_deployment_assembly(role, unit_path, env_path) -> dict:
    """Read one installed unit/env pair and refuse a drifting start."""
    try:
        unit_text = Path(unit_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise DeploymentAssemblyError(
            f"unit file unreadable: {unit_path}: {exc}") from exc
    try:
        env_text = Path(env_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise DeploymentAssemblyError(
            f"environment file unreadable: {env_path}: {exc}") from exc
    problems = deployment_assembly_problems(role, unit_text, env_text)
    if problems:
        raise DeploymentAssemblyError("; ".join(problems))
    return {"ok": True, "role": role, "unit": str(unit_path),
            "env": str(env_path)}


def deployment_backup(paths=None, *, key=None, now=None,
                      backup_id=None, require_share_secret=False) -> dict:
    """Run ONE offline backup checkpoint with every deployment path explicit.

    ``require_share_secret`` is the operational default for an explicit manual
    checkpoint: a package without the share-key file would look successful while
    being useless after a machine move.  The path is the one named by
    :func:`deployment_paths` (the console's effective configuration), never a
    newly invented default.  D18-11 removed the automatic timer and the
    pre-update checkpoint; this function remains an offline recovery tool.
    """
    layout = deployment_paths() if paths is None else dict(paths)
    store_path = Path(layout["store"])
    if not store_path.is_file():
        raise BackupError(
            f"share store is missing; refusing to back up an empty database: "
            f"{store_path}")
    share_secret_path = layout.get("share_secret_key")
    share_secret_key_path = None
    if share_secret_path is not None \
            and Path(share_secret_path).is_file():
        share_secret_key_path = share_secret_path
    elif require_share_secret:
        raise BackupError(
            "share secret key is missing; refusing a backup that cannot "
            f"decrypt the store after recovery: {share_secret_path}")
    material = (load_backup_key(layout["key_file"])
                if key is None else _validate_key(key))
    security_state = None
    security_path = Path(layout["security_state"])
    if security_path.is_file():
        security_state = read_security_state(security_path)
    store_module = _load_share_store()
    store = store_module.ShareStore(store_path)
    try:
        return run_backup_cycle(
            store, layout["ledger"], material,
            LocalObjectStore(layout["remote_dir"]),
            state_path=layout["state"], spool_dir=layout["spool"],
            config_paths=[layout["config"]],
            security_state=security_state, now=now, backup_id=backup_id,
            share_secret_key_path=share_secret_key_path)
    finally:
        store.close()


def deployment_restore(backup_path, *, key=None, paths=None,
                      security_state_path=None, now=None,
                      share_secret_owner=None,
                      share_secret_group=None) -> dict:
    """EXPLICIT recovery only: restore live data behind the security fence.

    The deployment entry ALWAYS requires the AES-GCM share-key file.  A package
    without it would replace the live database while keeping an unrelated key,
    reporting success even though every restored Key is undecryptable; the restore
    therefore refuses before any target is replaced.

    ``share_secret_owner``/``share_secret_group`` are the console uid/gid the
    restored key file must belong to.  The recovery runs as root, while the
    console's key-file gate accepts only a 0600 file owned by its own uid; without
    this the restored custom-path key is unreadable.
    """
    layout = deployment_paths() if paths is None else dict(paths)
    material = (load_backup_key(layout["key_file"])
                 if key is None else _validate_key(key))
    state_path = (security_state_path or layout["security_state"])
    return restore_for_recovery(
        backup_path, material,
        target_db_path=layout["store"],
        target_ledger_path=layout["ledger"],
        target_budget_db_path=layout["budget_db"],
        target_extra_dir=layout["etc"],
        target_security_state_path=layout["security_state"],
        target_share_secret_key_path=layout.get("share_secret_key"),
        target_share_secret_owner=share_secret_owner,
        target_share_secret_group=share_secret_group,
        security_state_path=state_path,
        allow_existing=True,
        require_share_secret=True, now=now)
