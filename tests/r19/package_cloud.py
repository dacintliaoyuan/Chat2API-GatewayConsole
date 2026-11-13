# -*- coding: utf-8 -*-
"""Build an isolated cloud package for E19-1-12.

The package carries ONLY the cloud composition root and the shared modules it
actually imports, plus the verification fake UM. The local application, the
legacy console hosts, model-sync/ZCode modules and every real state directory
are refused by construction (the caller's checkout is never copied).

The destination is a fresh temporary directory by default. A destination inside
the source checkout is refused so a test can never rewrite the repository.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # ai-gateway/tests/r19
AI_GATEWAY = HERE.parents[1]                    # ai-gateway/

CLOUD_DIRS = ("cloud", "shared")

ROOT_FILES = (
    "app/__init__.py",
    "app/console_backend_app.py",
    "console/operations.py",
    "console/share_production.py",
    "share/backend_supervision.py",
    "share/broker_client.py",
    "share/broker_host.py",
    "share/broker_ops.py",
    "share/broker_protocol.py",
    "share/broker_server.py",
    "share/instance_pipe.py",
    "share/management.py",
    "share/metering.py",
    "share/runtime.py",
    "share/secret_protection.py",
    "share/store.py",
)

CHAT2API_FILES = (
    "chat2api.py",
    "share_dispatch.py",
    "share_auth.py",
)

FAKE_UM_FILE = "tests/fake_um/fake_um_server.py"

FORBIDDEN_DIR_NAMES = (
    "local",
    "web",
    "um-cloud-deploy",
    "um-share-deploy",
    "lib",
    "data",
    "profile",
)

FORBIDDEN_NAMES = (
    "usagesrv.py",
    "console/adapters.py",
    "console/share_actions.py",
    "console/stats.py",
    "console/model_actions.py",
    "console/log_reader.py",
    "console/magpie_prices.py",
    "model_sync/reconcile.py",
    "model_sync/config_adapters.py",
    "model_sync/sync.py",
    "model_sync/sync_core.py",
    "zcode/hot_sync.py",
    "zcode/file_lock.py",
    "zcode/hot_sync_protocol.py",
)

FORBIDDEN_PREFIXES = ("model_sync", "zcode_", "console_um_")

IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".pytest_cache", ".mypy_cache")


def _copy(source: Path, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def build_cloud_package(source_root=None, destination=None) -> Path:
    """Copy the cloud closure into ``destination`` and return its root."""
    source = Path(source_root or AI_GATEWAY).resolve()
    if destination is None:
        destination = Path(tempfile.mkdtemp(prefix="r19-cloud-package-"))
    target = Path(destination).resolve()
    if target == source or target.is_relative_to(source):
        raise ValueError("cloud package destination is inside the source")
    target.mkdir(parents=True, exist_ok=True)
    for name in CLOUD_DIRS:
        shutil.copytree(source / name, target / name, ignore=IGNORE)
    for name in ROOT_FILES:
        _copy(source / name, target / name)
    for name in CHAT2API_FILES:
        _copy(source / "chat2api" / name, target / "chat2api" / name)
    _copy(source / FAKE_UM_FILE, target / FAKE_UM_FILE)
    forbidden = forbidden_entries(target)
    if forbidden:
        raise RuntimeError(
            "cloud package contains forbidden entries: %r" % (forbidden,))
    return target


def forbidden_entries(package_root) -> list:
    """Every path in the package that must never ship to the cloud."""
    root = Path(package_root)
    found = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or relative.suffix == ".pyc":
            continue
        if any(part in FORBIDDEN_DIR_NAMES for part in relative.parts):
            found.append(str(relative).replace("\\", "/"))
            continue
        relative_posix = str(relative).replace("\\", "/")
        name = relative.name
        if relative_posix in FORBIDDEN_NAMES or name in FORBIDDEN_NAMES or any(
                name.startswith(prefix) or relative_posix.startswith(prefix)
                for prefix in FORBIDDEN_PREFIXES):
            found.append(relative_posix)
    if (root / "chat2api" / "token.json").exists():
        found.append("chat2api/token.json")
    if (root / "chat2api" / ".edge-profile").exists():
        found.append("chat2api/.edge-profile")
    return sorted(set(found))


def package_files(package_root) -> list:
    """Every file in the package, relative and slash-separated."""
    root = Path(package_root)
    return sorted(
        str(path.relative_to(root)).replace("\\", "/")
        for path in root.rglob("*") if path.is_file())


# --------------------------------------------------------------- module paths
# spec-shared-11 / standards-tests-07: the local side already proves every
# loaded module came from inside its own package
# (``package_local.module_files_outside`` / ``assert_modules_inside``). The cloud
# side only checked the file LIST and ``cloud.backend.app.__file__``, so a package
# that dropped ``shared/pricing_rules.py`` and resolved it from the checkout still
# passed. These mirror the local helpers.

def module_files_outside(package_root, module_files):
    """Return ``(name, path)`` for modules loaded from outside the package."""
    import sys
    root = Path(package_root).resolve()
    stdlib_roots = _stdlib_roots()
    problems = []
    for name, raw in sorted((module_files or {}).items()):
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
    import sys
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
                roots.add(candidate / "lib")
    return roots
