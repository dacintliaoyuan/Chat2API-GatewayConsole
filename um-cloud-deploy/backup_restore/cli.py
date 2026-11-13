"""Command-line interface for the backup/restore package."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .assembly import (
    check_deployment_assembly, deployment_paths, deployment_restore,
)
from .backup import (
    freshness_status, retry_pending_uploads, run_backup_cycle,
)
from .constants import BACKUP_PREFIX
from .errors import BackupError, BackupKeyError, DeploymentAssemblyError
from .keyfile import load_backup_key
from .objectstore import LocalObjectStore
from .package import read_security_state
from .products import _load_share_store
from .restore import restore_backup, restore_for_recovery

def _cli_store(path):
    module = _load_share_store()
    return module.ShareStore(path)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="UM share encrypted backup/restore checkpoint")
    parser.add_argument("--store")
    parser.add_argument("--ledger")
    parser.add_argument("--key-file")
    parser.add_argument("--remote-dir")
    parser.add_argument("--remote-prefix", default=BACKUP_PREFIX)
    parser.add_argument("--state")
    parser.add_argument("--spool")
    parser.add_argument("--config", action="append", default=[])
    parser.add_argument("--security-state")
    parser.add_argument("--print-freshness", action="store_true")
    parser.add_argument("--retry-pending", action="store_true")
    parser.add_argument("--check-deployment-assembly", nargs=3,
                        metavar=("ROLE", "UNIT", "ENV"))
    parser.add_argument("--deployment-restore", action="store_true")
    parser.add_argument("--deployment-current", default="/opt/um-share/current")
    parser.add_argument("--deployment-etc", default="/etc/um-share")
    parser.add_argument("--deployment-var", default="/var/lib/um-share")
    parser.add_argument("--deployment-console-var",
                        default="/var/lib/um-share-console")
    parser.add_argument("--share-secret-owner", type=int, default=None,
                        help="uid that must own the restored share-key file")
    parser.add_argument("--share-secret-group", type=int, default=None,
                        help="gid that must own the restored share-key file")
    parser.add_argument("--restore-backup")
    parser.add_argument("--restore-target")
    parser.add_argument("--restore-security-state")
    parser.add_argument("--allow-existing", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    if args.check_deployment_assembly:
        role, unit_path, env_path = args.check_deployment_assembly
        try:
            result = check_deployment_assembly(role, unit_path, env_path)
        except DeploymentAssemblyError as exc:
            print(json.dumps({
                "ok": False, "role": role,
                "problems": [str(exc)],
            }, ensure_ascii=False))
            return 1
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.deployment_restore:
        if not args.restore_backup:
            parser.error("--deployment-restore requires --restore-backup")
        try:
            result = deployment_restore(
                args.restore_backup,
                paths=deployment_paths(
                    current=args.deployment_current,
                    etc=args.deployment_etc,
                    var=args.deployment_var,
                    console_var=args.deployment_console_var),
                security_state_path=args.restore_security_state,
                share_secret_owner=args.share_secret_owner,
                share_secret_group=args.share_secret_group)
        except (BackupError, OSError) as exc:
            print(json.dumps({"ok": False,
                              "error": f"{type(exc).__name__}: {exc}"},
                             ensure_ascii=False))
            return 1
        print(json.dumps({"ok": True, "restore": result},
                         ensure_ascii=False, sort_keys=True))
        return 0

    if args.print_freshness:
        if not args.state:
            parser.error("--state is required with --print-freshness")
        print(json.dumps(freshness_status(args.state), ensure_ascii=False,
                          sort_keys=True))
        return 0
    if args.restore_backup:
        if not (args.key_file and args.restore_target):
            parser.error(
                "--key-file and --restore-target are required with "
                "--restore-backup")
        try:
            key = load_backup_key(args.key_file)
            if args.restore_security_state:
                result = restore_for_recovery(
                    args.restore_backup, key,
                    target_dir=args.restore_target,
                    security_state_path=args.restore_security_state,
                    config_paths=args.config or None,
                    allow_existing=args.allow_existing)
            else:
                result = restore_backup(
                    args.restore_backup, key,
                    target_db_path=Path(args.restore_target) / "share-store.db",
                    target_ledger_path=(Path(args.restore_target)
                                        / "usage-share-backend.jsonl"),
                    target_budget_db_path=(Path(args.restore_target)
                                           / "budget.db"),
                    target_extra_dir=Path(args.restore_target) / "config",
                    require_min_generation=None,
                    allow_existing=args.allow_existing)
        except (BackupError, OSError) as exc:
            print(json.dumps({"ok": False,
                              "error": f"{type(exc).__name__}: {exc}"},
                             ensure_ascii=False))
            return 1
        print(json.dumps({"ok": True, "restore": result}, ensure_ascii=False,
                          sort_keys=True))
        return 0

    missing = [name for name in
               ("store", "ledger", "key_file", "remote_dir", "state", "spool")
               if not getattr(args, name)]
    if missing:
        parser.error("missing required options: " + ", ".join(missing))
    try:
        key = load_backup_key(args.key_file)
    except BackupKeyError as exc:
        parser.error(str(exc))
    object_store = LocalObjectStore(args.remote_dir)
    security_state = None
    if args.security_state:
        try:
            security_state = read_security_state(args.security_state)
        except BackupError as exc:
            print(json.dumps({"ok": False,
                              "error": f"{type(exc).__name__}: {exc}"},
                             ensure_ascii=False))
            return 1
    try:
        if args.retry_pending:
            results = retry_pending_uploads(
                args.spool, key, object_store, state_path=args.state)
            print(json.dumps({"ok": all(item["ok"] for item in results),
                              "retried": results}, ensure_ascii=False,
                              sort_keys=True))
            return 0 if all(item["ok"] for item in results) else 1
        store = _cli_store(args.store)
        try:
            result = run_backup_cycle(
                store, args.ledger, key, object_store,
                state_path=args.state, spool_dir=args.spool,
                config_paths=args.config or None,
                security_state=security_state)
        finally:
            store.close()
    except (BackupError, OSError) as exc:
        print(json.dumps({"ok": False,
                          "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1
