#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local composition-root entry point (E19-1-08).

The ONLY way to start the local application. Before importing any product
module it:

1. REFUSES to run unless the interpreter is isolated (``python -I``). The check
   is the first statement after ``import sys``: a planted ``json.py``/``argparse.py``
   on ``sys.path`` must never get a chance to run, so ``argparse``/``json`` are
   imported only AFTER the guard passed (exit 2 with the required command line on
   stderr);
2. removes inherited source-checkout/repository entries (anything inside the
   package root, any ancestor of it, and any directory that looks like the
   source checkout) from ``sys.path`` and inserts this package root, so a
   planted or inherited path cannot shadow a stdlib/product module; the
   inherited ``PYTHONPATH`` is cleared as well;
3. loads the explicit ``--config`` document and validates it (exit 2 on a
   missing/invalid field, without binding a port, creating a directory or
   starting a child process);
4. applies the configured isolated ``HOME``/``USERPROFILE`` and disables
   bytecode writes, then calls ``local.backend.app.main``.

``--print-runtime-info`` prints the actual ``sys.path``/``cwd``/``PYTHONPATH``/
``module_file``/``config_path`` plus every configured state path, the loaded
module names, the configured ``home_dir``, the OS-level ``real_home`` and the
``HOME``/``USERPROFILE`` the parent exported, and exits without starting a
service.
"""
from __future__ import annotations

import sys                      # builtin; nothing on sys.path can hijack it

# E19-1-04 step 1: the isolation guard runs BEFORE any non-builtin import.
# Without ``-I`` a planted ``json.py``/``argparse.py`` would execute during
# import; with it, ``sys.path`` never contains the script dir or PYTHONPATH.
if not sys.flags.isolated:
    sys.stderr.write(
        "local entry must run as: python -I local/run_local.py ...\n"
        "(-I is required: the inherited sys.path/PYTHONPATH may contain a "
        "planted module and the local application must not load it)\n")
    raise SystemExit(2)

import argparse                 # only after isolation is confirmed
import json
import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


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
    # must not smuggle the repository back in.
    os.environ.pop("PYTHONPATH", None)


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="UM local composition root")
    parser.add_argument("--config", required=True,
                        help="explicit local config JSON")
    parser.add_argument("--check-config", action="store_true",
                        help="validate the config and exit without starting")
    parser.add_argument("--print-runtime-info", action="store_true",
                        help="print the resolved runtime facts and exit")
    return parser.parse_args(argv)


def _os_user_home():
    """The OS account home, independent of ``HOME``/``USERPROFILE``.

    E19-1-04 step 10: the isolation proof compares the configured isolated
    ``home_dir`` against the REAL user home. ``Path.home()`` is not that fact — it
    reads ``HOME``/``USERPROFILE``, so a parent process that already exports a
    fake home would make the comparison meaningless. The OS-level directory is
    obtained from the shell API on Windows and from the password database on
    POSIX.

    Returns ``(path, source)``; ``source`` is ``"windows-shell"``,
    ``"posix-pwd"`` or ``"env-fallback"`` when the OS query is unavailable (so
    a caller never mistakes the fallback for an OS-level answer).
    """
    if os.name == "nt":
        try:
            import ctypes
            buffer = ctypes.create_unicode_buffer(260)
            # CSIDL_PROFILE (40): the current user's profile directory.
            result = ctypes.windll.shell32.SHGetFolderPathW(
                None, 40, None, 0, buffer)
            if result == 0 and buffer.value:
                return buffer.value, "windows-shell"
        except (AttributeError, OSError, ValueError):
            pass
    else:
        try:
            import pwd
            entry = pwd.getpwuid(os.getuid())
            if entry.pw_dir:
                return entry.pw_dir, "posix-pwd"
        except (ImportError, KeyError, OSError, AttributeError):
            pass
    return str(Path.home()), "env-fallback"


def _runtime_info(config, config_path, real_home, original_home_env,
                  real_home_source):
    return {
        "status": "ok",
        "role": "local",
        "sys_path": list(sys.path),
        "cwd": os.getcwd(),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "module_file": str(Path(__file__).resolve()),
        "package_root": str(PACKAGE_ROOT),
        "project_root": str(PACKAGE_ROOT.parent),
        "repo_root": str(PACKAGE_ROOT.parents[1]),
        "config_path": str(Path(config_path).resolve()),
        "config_paths": config.config_paths(),
        # E19-1-04 step 10: the isolation proof compares these two. The real
        # home is the OS account directory, captured BEFORE HOME/USERPROFILE are
        # overridden below; ``original_home_env`` records what the parent
        # exported so a fake inherited HOME is visible rather than silent.
        "home_dir": config.home_dir,
        "real_home": real_home,
        "real_home_source": real_home_source,
        "original_home_env": original_home_env,
        "modules": sorted(sys.modules),
        # E19-1-L2 / F23: the module NAMES alone cannot prove the package ran
        # its own code — a same-named module loaded from the checkout would look
        # identical. The resolved file of every loaded module lets the isolation
        # test assert the package (or stdlib/site-packages) is the only source.
        "module_files": {
            name: getattr(module, "__file__", None)
            for name, module in sorted(sys.modules.items())
        },
    }


def main(argv=None):
    args = _parse_args(argv)
    if args.check_config and args.print_runtime_info:
        print("--check-config and --print-runtime-info are mutually exclusive",
              file=sys.stderr)
        return 2
    # Resolve the explicit config before any path work.
    config_path = str(Path(args.config).resolve())
    _clean_sys_path()
    from local.backend.config import LocalConfig, LocalConfigError

    try:
        config = LocalConfig.from_file(config_path)
        # ``config_dir`` must be the ``--config`` document's parent: otherwise
        # the deployment would read one config and write its session code /
        # price document somewhere else.
        config.validate_config_dir(config_path)
    except LocalConfigError as exc:
        print("local config invalid: %s" % exc, file=sys.stderr)
        return 2
    # Capture the REAL user home BEFORE overriding HOME/USERPROFILE. E19-1-04 step
    # 10: ``Path.home()`` is NOT the real home — it reads HOME/USERPROFILE, so a
    # parent that already exports a fake home would make the isolation proof
    # compare the fixture against itself. The OS account directory is used, and the
    # parent's exported values are recorded separately.
    original_home_env = (os.environ.get("HOME")
                         or os.environ.get("USERPROFILE") or "")
    real_home, real_home_source = _os_user_home()
    os.environ["HOME"] = config.home_dir
    os.environ["USERPROFILE"] = config.home_dir
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    if args.check_config:
        print(json.dumps(config.runtime_info(), ensure_ascii=False))
        return 0
    from local.backend import app as app_module  # noqa: F401
    if args.print_runtime_info:
        # Import the composition root so the printed module list is the REAL
        # loaded set; importing it starts nothing.
        print(json.dumps(_runtime_info(config, config_path, real_home,
                                      original_home_env, real_home_source),
                         ensure_ascii=False))
        return 0
    from local.backend.app import main as app_main
    return app_main(config)


if __name__ == "__main__":
    sys.exit(main())
