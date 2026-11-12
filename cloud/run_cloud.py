#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cloud composition-root entry point (E19-1-11).

The ONLY way to start the cloud application. Before importing any product
module it:

1. REFUSES to run unless the interpreter is isolated (``python -I``). The check
   is the first statement after ``import sys``: a planted ``json.py``/``argparse.py``
   on ``sys.path`` must never get a chance to run, so ``argparse``/``json`` are
   imported only AFTER the guard passed (exit 2 with the required command line on
   stderr);
2. clears the inherited deployment environment (``CHAT2API_*``, ``SHARE_*``,
   ``UM_SHARE_*``, ``CONSOLE_*``, ``USAGE_*``, ``MAGPIE_*``, ``DSH_*``) BEFORE
   any product module import. ``share_management`` binds ``SHARE_STORE_FILE`` into
   a module constant at import time, so a stale parent value would silently
   override the explicit ``CloudConfig``; the configured values are applied by
   ``CloudConfig.process_env()`` afterwards;
3. removes source-checkout entries (the repository ``ai-gateway`` and anything
   below it, any ancestor of it, plus any directory that carries
   ``usagesrv.py`` and ``chat2api/``) from ``sys.path`` and inserts this
   package root, so a planted or inherited path cannot shadow a
   stdlib/product module; the inherited ``PYTHONPATH`` is cleared as well (the
   child processes get their own explicit ``PYTHONPATH``);
4. loads the explicit ``--config`` document and validates it (exit 2 on a
   missing/invalid field, without binding a port, creating a directory or
   starting a child process);
5. applies the configured isolated ``HOME``/``USERPROFILE`` and disables
   bytecode writes, then calls ``cloud.backend.app.main``.

A full start is POSIX-only: on Windows ``app.main`` returns exit 3
(``requires POSIX``) instead of pretending to run the Linux chain.
"""
from __future__ import annotations

import sys                      # builtin; nothing on sys.path can hijack it

# E19-1-06 step 1: the isolation guard runs BEFORE any non-builtin import.
if not sys.flags.isolated:
    sys.stderr.write(
        "cloud entry must run as: python -I cloud/run_cloud.py ...\n"
        "(-I is required: the inherited sys.path/PYTHONPATH may contain a "
        "planted module and the cloud application must not load it)\n")
    raise SystemExit(2)

import argparse                 # only after isolation is confirmed
import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

# E19-1-06 step 2: every inherited variable whose module-level consumer would
# bind a production path/credential at import time. The cloud root owns these
# values explicitly (``CloudConfig.process_env``), so an inherited one must be
# gone before the first product import.
INHERITED_ENV_PREFIXES = (
    "CHAT2API_",
    "SHARE_",
    "UM_SHARE_",
    "CONSOLE_",
    "USAGE_",
    "MAGPIE_",
    "DSH_",
)


def _clear_inherited_env():
    """Drop every inherited deployment variable (prefix match)."""
    for name in list(os.environ):
        if name.startswith(INHERITED_ENV_PREFIXES):
            os.environ.pop(name, None)


def _is_within(path, root):
    try:
        return path == root or path.is_relative_to(root)
    except (AttributeError, OSError):
        return False


def _is_ancestor(path, root):
    try:
        return path != root and root.is_relative_to(path)
    except (AttributeError, OSError):
        return False


def _looks_like_source_checkout(path):
    """A repository checkout carries ``usagesrv.py`` and ``chat2api/``."""
    try:
        return (path / "usagesrv.py").is_file() \
            and (path / "chat2api").is_dir()
    except OSError:
        return False


def _clean_sys_path():
    """Drop source-checkout/package-internal entries; insert package root."""
    cleaned = []
    for entry in sys.path:
        try:
            resolved = Path(entry or ".").resolve()
        except OSError:
            continue
        if _is_within(resolved, PACKAGE_ROOT):
            continue
        if _is_ancestor(resolved, PACKAGE_ROOT):
            continue
        if _looks_like_source_checkout(resolved):
            continue
        cleaned.append(str(resolved))
    sys.path[:] = [str(PACKAGE_ROOT)] + cleaned
    # The child processes get their own explicit PYTHONPATH; an inherited one
    # must not smuggle the repository (or a Git root above it) back in.
    os.environ.pop("PYTHONPATH", None)


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="UM cloud composition root")
    parser.add_argument("--config", required=True,
                        help="explicit cloud config JSON")
    parser.add_argument("--check-config", action="store_true",
                        help="validate the config and exit without starting")
    parser.add_argument("--start-broker", action="store_true",
                        help="start the broker too (fixture runtime mode only)")
    # REMAINDER: the 8100 gateway argv carries its own option-looking flags
    # (--port/--upstream-port/...), exactly like ``share_broker_ops``'s
    # ``--host-command``. It must come last on the command line.
    parser.add_argument("--host-command", nargs=argparse.REMAINDER,
                        default=None,
                        help="fixed 8100 gateway argv the broker launches "
                             "(required with --start-broker)")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    # E19-1-06 step 2: BEFORE any product module import.
    _clear_inherited_env()
    _clean_sys_path()
    from cloud.backend.config import CloudConfig, CloudConfigError

    try:
        config = CloudConfig.from_file(args.config)
    except CloudConfigError as exc:
        print("cloud config invalid: %s" % exc, file=sys.stderr)
        return 2
    # standards-config-09: the declared ``config_dir`` must be the directory the
    # document is actually read from, exactly like the local entry.
    try:
        config.validate_config_dir(args.config)
    except CloudConfigError as exc:
        print("cloud config invalid: %s" % exc, file=sys.stderr)
        return 2
    if args.start_broker and config.runtime_mode != "fixture":
        print("cloud config invalid: --start-broker is only available in the "
              "fixture runtime mode; the production broker is owned by systemd",
              file=sys.stderr)
        return 2
    if args.start_broker and not args.host_command:
        # The broker parser REQUIRES --host-command (a REMAINDER carrying the
        # 8100 gateway argv). ``broker_args()`` deliberately carries only the
        # broker's own flags, so a start without this input would spawn a child
        # that immediately exits with "the following arguments are required".
        print("cloud config invalid: --start-broker needs --host-command "
              "<gateway argv...> (the broker launches the 8100 gateway, and "
              "the cloud config does not describe it)", file=sys.stderr)
        return 2
    os.environ["HOME"] = config.home_dir
    os.environ["USERPROFILE"] = config.home_dir
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    from cloud.backend.app import main as app_main
    return app_main(config, check_config=args.check_config,
                    start_broker=args.start_broker,
                    host_command=args.host_command)


if __name__ == "__main__":
    sys.exit(main())
