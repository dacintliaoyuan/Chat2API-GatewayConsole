# -*- coding: utf-8 -*-
"""E19-1-09/10 local package builder (r19).

Build one isolated local package from the repository allowlist (§4.7) into an
unrelated ``tempfile.mkdtemp()`` directory.  The allowlist is explicit:

* ``local/**`` and ``shared/**`` (excluding ``__pycache__``/``*.pyc``);
* ``chat2api/chat2api.py`` and the local host modules it imports;
* ``share/secret_protection.py`` — the ONLY allowed ``share_*`` module (the
  local control-token gate needs it);
* ``tests/fake_um/fake_um_server.py`` and its package marker.

No cloud/share/broker/display source, no ``usagesrv.py``/``console/adapters.py``,
no ``web/``, ``data/``, ``profile/`` or root deployment script is copied.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

AI_GATEWAY = Path(__file__).resolve().parents[2]

COPY_TREES = (
    "local",
    "shared",
)

COPY_FILES = (
    "chat2api/chat2api.py",
    "app/console_backend_app.py",
    "share/secret_protection.py",
    "console/stats.py",
    "console/operations.py",
    "console/model_actions.py",
    "model_sync/sync.py",
    "model_sync/sync_core.py",
    "tests/fake_um/__init__.py",
    "tests/fake_um/fake_um_server.py",
)

FORBIDDEN_FILES = (
    "usagesrv.py",
    "console/adapters.py",
    "console/share_actions.py",
    "console/share_production.py",
    "share/management.py",
    "share/store.py",
    "chat2api/share_auth.py",
    "share/instance_pipe.py",
    "share/runtime.py",
    "share/metering.py",
    "share/public_supervision.py",
    "console/display_proxy.py",
    "console/log_reader.py",
    "console/magpie_prices.py",
    "console/speedtest.py",
    "console/gateway_connection.py",
    "console/tailnet.py",
    "data",
    "profile",
    "web",
    "lib",
    "um-cloud-deploy",
    "um-share-deploy",
    "chat2api/.edge-profile",
)

FORBIDDEN_GLOBS = (
    "share/broker_*.py",
    "console/um_*.py",
    "*.ps1",
    "*.bat",
    "*.vbs",
)

# E19-1-L2 / F5: the other end's package tree. ``verify_package`` used to look
# only at file names, so a planted ``cloud/run_cloud.py`` produced ``[]``. ``local``
# is NOT forbidden: it IS the package under test. ``shared`` is carried on purpose.
FORBIDDEN_DIRS = (
    "cloud",
)

# A module imported from the package may resolve to the package itself, the
# standard library, or the host site-packages (declared dependency, see the E19-5
# wheelhouse note). Anything else means the isolated package silently loaded code
# from the checkout it was copied out of.
_STDLIB_ALLOWED = frozenset(("built-in", "frozen"))

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")


def build_package(destination=None, *, source_root=None):
    """Copy the explicit allowlist into ``destination`` (a new temp dir)."""
    source = Path(source_root or AI_GATEWAY).resolve()
    if destination is None:
        destination = Path(tempfile.mkdtemp(prefix="r19-local-pkg-"))
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    for tree in COPY_TREES:
        source_tree = source / tree
        if not source_tree.is_dir():
            raise AssertionError("missing package tree: %s" % source_tree)
        shutil.copytree(source_tree, destination / tree, ignore=_IGNORE)
    for relative in COPY_FILES:
        source_file = source / relative
        if not source_file.is_file():
            raise AssertionError("missing package file: %s" % source_file)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target)
    verify_package(destination)
    return destination


def copy_package(source, destination=None):
    """Copy an already-built package to a second unrelated temp dir."""
    source = Path(source).resolve()
    if destination is None:
        destination = Path(tempfile.mkdtemp(prefix="r19-local-pkg-copy-"))
    destination = Path(destination).resolve()
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, ignore=_IGNORE)
    # §4.7 step 2: a copy must not carry cloud residue even if the source
    # build was tampered with after verification.
    shutil.rmtree(destination / "cloud", ignore_errors=True)
    return destination


def verify_package(package_root):
    """Return the relative paths that violate the local allowlist (empty = OK)."""
    root = Path(package_root)
    problems = []
    for relative in FORBIDDEN_FILES:
        if (root / relative).exists():
            problems.append(relative)
    # E19-1-L2 / F5: a whole directory of the other end is a violation even
    # when none of its files is individually listed.
    for relative in FORBIDDEN_DIRS:
        if (root / relative).exists():
            problems.append(relative + "/")
    for pattern in FORBIDDEN_GLOBS:
        for path in root.glob(pattern):
            problems.append(str(path.relative_to(root)))
    if not (root / "local" / "run_local.py").is_file():
        problems.append("local/run_local.py")
    if not (root / "chat2api" / "chat2api.py").is_file():
        problems.append("chat2api/chat2api.py")
    if not (root / "share/secret_protection.py").is_file():
        problems.append("share/secret_protection.py")
    return problems


def module_files_outside(package_root, module_files):
    """Return ``(name, path)`` for modules loaded from outside the package.

    E19-1-L2 / F23: ``verify_package`` only checks the file list. A package can
    pass that and still run because a module resolved to the checkout it was
    copied out of. ``module_files`` is the ``{name: __file__}`` map printed by
    ``--print-runtime-info``'s ``module_files`` key.

    Allowed: the package itself, standard-library modules (``__file__`` is
    ``None`` for built-ins/frozen ones, or lives under the stdlib root), and the
    host ``site-packages`` (a declared dependency this stage does not bundle).
    """
    root = Path(package_root).resolve()
    stdlib_roots = _stdlib_roots()
    problems = []
    for name, raw in sorted(module_files.items()):
        if not raw:
            continue
        try:
            path = Path(raw).resolve()
        except (OSError, ValueError):
            problems.append((name, str(raw)))
            continue
        if path == root or root in path.parents:
            continue
        if any(path == entry or entry in path.parents
               for entry in stdlib_roots):
            continue
        if "site-packages" in path.parts or "dist-packages" in path.parts:
            continue
        problems.append((name, str(path)))
    return problems


def assert_modules_inside(package_root, module_files):
    """Raise ``AssertionError`` naming every module loaded from outside."""
    problems = module_files_outside(package_root, module_files)
    if problems:
        raise AssertionError(
            "modules loaded from outside the package: %s"
            % ", ".join("%s=%s" % item for item in problems))


def _stdlib_roots():
    roots = set()
    for raw in {getattr(sys, "base_prefix", None), sys.prefix}:
        if not raw:
            continue
        base = Path(raw)
        roots.add(base)
        roots.add(base / "Lib")
        for pattern in ("python3.*", "python2.*"):
            for candidate in base.glob(pattern):
                roots.add(candidate)
    # ``sysconfig`` knows the exact stdlib directory on this interpreter.
    try:
        import sysconfig
        for key in ("stdlib", "platstdlib", "purelib", "platlib"):
            value = sysconfig.get_paths().get(key)
            if value:
                roots.add(Path(value))
    except (ImportError, KeyError, OSError):
        pass
    return frozenset(roots)


def assert_package(package_root):
    """Raise ``AssertionError`` naming every forbidden package path."""
    problems = verify_package(package_root)
    if problems:
        raise AssertionError(
            "local package contains forbidden paths: %s"
            % ", ".join(sorted(set(problems))))
