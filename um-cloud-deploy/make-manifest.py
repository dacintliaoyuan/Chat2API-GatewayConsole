#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Freeze the UM cloud release closure and write its manifest.

The manifest answers four questions from the CURRENT source bytes:

1. which product Python files are reachable (static superset), and which of
   those the cloud role actually imports (cloud runtime);
2. the SHA-256 of every packaged file and deployment file;
3. the release version, the Python version used to generate the manifest and
   the locked dependency versions from ``dependencies.lock.json``;
4. whether a built console tree is carried, which console role it was built as
   and which module script its index.html loads.

The closure is AST-based: every ``import``/``from`` in any function body counts
(the Windows-only lazy branches are part of the static superset).  Four modules are
loaded by ``importlib.util.spec_from_file_location`` rather than an import
statement; they are named explicitly in ``DYNAMIC_MODULES``.

This script never imports product modules and never writes into a release.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import platform
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2
GENERATOR = "um-cloud-deploy/make-manifest.py"

STATIC_ROOTS = [
    "usagesrv.py",
    "chat2api/share_entry.py",
    "chat2api/chat2api.py",
    "share/broker_ops.py",
    "share/backend_supervision.py",
    # Ops CLI included in the release even though no runtime module imports
    # it: key create/rotate/disable/delete is a supported private operation.
    "share/admin.py",
    # ``usagesrv`` imports this through ``importlib.import_module`` (the AST
    # closure cannot see a dynamic import), so it is named explicitly.
    "share/metering.py",
]

# Modules the cloud role actually imports (directly or through the cloud
# console registry).  This is deliberately narrower than the static superset:
# Qoder / CLIProxy / model_sync / zcode / Windows probes are NOT here.
CLOUD_RUNTIME = [
    "usagesrv.py",
    "console/tailnet.py",
    "console/display_proxy.py",
    "console/adapters.py",
    "console/operations.py",
    "console/public_probe.py",
    "console/share_actions.py",
    "console/share_probe.py",
    "console/share_production.py",
    "console/stats.py",
    "console/log_reader.py",
    "console/magpie_prices.py",
    "share/management.py",
    "share/store.py",
    "share/broker_client.py",
    "share/broker_host.py",
    "share/broker_ops.py",
    "share/broker_protocol.py",
    "share/broker_server.py",
    "share/runtime.py",
    "share/metering.py",
    "share/secret_protection.py",
    "share/instance_pipe.py",
    "chat2api/share_entry.py",
    "chat2api/chat2api.py",
    "chat2api/nonstream_aggregate.py",
    "chat2api/share_dispatch.py",
]

# importlib.util.spec_from_file_location targets: the module name is a literal
# but the path is assembled at runtime, so AST import edges alone miss these.
DYNAMIC_MODULES = {
    "chat2api/chat2api": [
        "chat2api/nonstream_aggregate",
        "chat2api/share_dispatch",
        "chat2api/share_entry",
        "share/secret_protection",
    ],
    "chat2api/share_entry": [
        "share/secret_protection",
        "share/instance_pipe",
    ],
}

# Directories that hold local tool/agent state, never deployment material.  Hooks
# (`.mimosa`) and test caches rewrite them on this machine, so hashing them would
# change the manifest without any change to the release bytes.
LOCAL_STATE_DIRS = {"__pycache__", ".mimosa", ".pytest_cache", ".mypy_cache",
                    ".ruff_cache", ".zcode"}

SKIP_DIRS = {"tests", "tools", "tmp", "output", "data", "web", "um-cloud-deploy",
             ".git"} | LOCAL_STATE_DIRS


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _module_map(root: Path) -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in sorted(root.glob("*.py")):
        modules[path.stem] = path
    chat = root / "chat2api"
    if chat.is_dir():
        for path in sorted(chat.glob("*.py")):
            modules["chat2api/" + path.stem] = path
    # r19 E19-1: ``shared`` is a real package both ends import
    # (``from shared import pricing_rules``).  Without it here the closure
    # silently omitted the whole package and the deployed console died with
    # ``ModuleNotFoundError: No module named 'shared'``.  The package
    # initializer is registered under ``shared/__init__`` and resolved through
    # ``_resolve`` (a bare ``shared`` maps there).
    for package in ("shared", "console", "share", "model_sync", "zcode", "app"):
        directory = root / package
        if directory.is_dir():
            for path in sorted(directory.glob("*.py")):
                modules[package + "/" + path.stem] = path
    return modules


def _resolve(name: str, modules: dict[str, Path]) -> str | None:
    # A dotted module (``shared.pricing_rules``) resolves through the package
    # prefix FIRST: ``shared.pricing_rules`` -> ``shared/pricing_rules``.  The
    # bare ``shared`` package alone would drop the concrete submodule.
    dotted = name.replace(".", "/")
    if dotted in modules:
        return dotted
    # ``shared`` (the bare package) resolves to its initializer, which lives
    # under ``shared/__init__`` in the module map.
    package_init = dotted + "/__init__"
    if package_init in modules:
        return package_init
    name = name.split(".")[0]
    if name in modules:
        return name
    if name in ("nonstream_aggregate", "share_dispatch", "share_entry",
                 "share/secret_protection", "share/instance_pipe"):
        candidate = "chat2api/" + name
        if candidate in modules:
            return candidate
    candidate = "chat2api/" + name
    if candidate in modules:
        return candidate
    return None


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module:
                names.add(node.module)
                # ``from shared import pricing_rules`` names the PACKAGE as
                # ``module`` and the concrete submodule in ``names``.  Both
                # edges are required: the package alone resolves to
                # ``shared/__init__.py`` and would silently drop
                # ``shared/pricing_rules.py`` from the release.
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    names.add(node.module + "." + alias.name)
    return names


def _closure(root: Path, roots: list[str]) -> list[str]:
    modules = _module_map(root)
    seen: set[str] = set()
    queue = [Path(item).with_suffix("").as_posix() for item in roots]
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        if current not in modules:
            raise SystemExit(f"closure root/import missing: {current}")
        seen.add(current)
        path = modules[current]
        for imported in _imported_names(path):
            resolved = _resolve(imported, modules)
            if resolved is not None and resolved not in seen:
                queue.append(resolved)
        for dynamic in DYNAMIC_MODULES.get(current, ()):
            if dynamic not in seen:
                queue.append(dynamic)
    return sorted(item + ".py" for item in seen)


def _files(root: Path, paths: list[str], membership: dict[str, list[str]]) -> list[dict]:
    result = []
    for rel in paths:
        path = root / rel
        if not path.is_file():
            raise SystemExit(f"closure file missing: {rel}")
        result.append({
            "path": rel,
            "sha256": _sha256(path),
            "size": path.stat().st_size,
            "closures": membership.get(rel, []),
        })
    return result


def _deployment_files(root: Path, deploy_root: Path) -> list[dict]:
    try:
        deploy_rel = deploy_root.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        deploy_rel = "um-cloud-deploy"
    result = []
    for path in sorted(deploy_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(deploy_root).as_posix()
        if rel in ("RELEASE-MANIFEST.json", "SHA256SUMS"):
            continue
        # Skip local tool state at any depth: `.mimosa` hook bookkeeping appears
        # and disappears between runs, bytecode caches are rebuilt by the
        # interpreter.  Neither is part of the release closure.
        parts = Path(rel).parts
        if any(part in LOCAL_STATE_DIRS for part in parts):
            continue
        if rel.endswith((".pyc", ".pyo")):
            continue
        full = deploy_rel + "/" + rel
        result.append({
            "path": full,
            "sha256": _sha256(path),
            "size": path.stat().st_size,
            "kind": "deployment",
        })
    return result


def _web_entry(index: Path) -> str | None:
    """The module script the built index.html actually loads.

    The manifest binds the entry chunk, not only a directory name: a carried
    dist whose index.html points at the local-role console must not be published
    as the cloud console.
    """
    try:
        html = index.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(
        r'<script[^>]*\btype="module"[^>]*\bsrc="/([^"]+)"', html)
    if match is None:
        match = re.search(
            r'<script[^>]*\bsrc="/([^"]+)"[^>]*\btype="module"', html)
    return match.group(1) if match else None


def _web_build(web_dist: Path) -> dict:
    """The build identity written by vite.config.ts.

    ``candidate-build.json`` names the console role, the source hash and the
    entry assets.  The local production ``dist`` (8001) is never carried and has
    none of them, so the manifest records an empty object and a role-gated build
    fails instead of guessing.
    """
    path = web_dist / "candidate-build.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    keys = ("line", "mode", "role", "builtAt", "gitHead", "sourceHash",
             "entryAssets")
    return {key: data[key] for key in keys if key in data}


def _web_dist(root: Path, web_dist: Path | None, allow_missing: bool) -> dict:
    if web_dist is None:
        if not allow_missing:
            raise SystemExit(
                "web/dist is not supplied: pass --web-dist or --allow-missing-web")
        return {
            "included": False,
            "reason": "frontend in flight; build web/dist separately and carry the whole tree",
            "root": "web/dist",
            "files": [],
        }
    index = web_dist / "index.html"
    if not index.is_file():
        raise SystemExit(f"web/dist incomplete: {index} missing")
    try:
        rel_root = web_dist.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        rel_root = "web/dist"
    files = []
    for path in sorted(web_dist.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(web_dist).as_posix()
        files.append({
            "path": rel_root + "/" + rel,
            "sha256": _sha256(path),
            "size": path.stat().st_size,
        })
    return {
        "included": True,
        "root": rel_root,
        "file_count": len(files),
        "entry": _web_entry(index),
        "build": _web_build(web_dist),
        "files": files,
    }


def _load_dependencies(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"dependencies lock unreadable: {exc}")
    if not isinstance(data, dict):
        raise SystemExit("dependencies lock must be an object")
    return data


def _build(args) -> dict:
    root = args.root.resolve()
    deploy_root = args.deploy_root.resolve()
    dependencies = _load_dependencies(args.dependencies)
    target_python = str((dependencies.get("target") or {}).get("python") or "3.14")
    current = platform.python_version()
    current_minor = ".".join(current.split(".")[:2])

    static = _closure(root, STATIC_ROOTS)
    # The cloud runtime is the EXPLICIT assembly list, not the transitive AST
    # closure of the static superset: following every lazy import would pull the
    # Windows-only Qoder/CLIProxy/model_sync branches back in.
    cloud = sorted(CLOUD_RUNTIME)
    if not set(cloud).issubset(set(static)):
        missing = sorted(set(cloud) - set(static))
        raise SystemExit("cloud runtime is not a subset of the static closure: "
                         + ", ".join(missing))
    for rel in cloud:
        if not (root / rel).is_file():
            raise SystemExit(f"cloud runtime file missing: {rel}")

    web_dist = _web_dist(root, args.web_dist, args.allow_missing_web)
    require_role = getattr(args, "require_web_role", None)
    require_line = getattr(args, "require_web_line", None)
    if require_role or require_line:
        if not web_dist["included"]:
            raise SystemExit(
                "--require-web-role/--require-web-line needs --web-dist")
        build = web_dist.get("build") or {}
        if require_role:
            role = build.get("role")
            if role != require_role:
                raise SystemExit(
                    f"web dist role is {role!r}, expected {require_role!r}: "
                    f"build the {require_role} console with "
                    f"CONSOLE_PRODUCTION_BUILD=1 CONSOLE_ROLE={require_role} "
                    "npm run build")
        if require_line:
            line = build.get("line")
            if line != require_line:
                raise SystemExit(
                    f"web dist line is {line!r}, expected {require_line!r}: "
                    "build the production console with "
                    "CONSOLE_PRODUCTION_BUILD=1 CONSOLE_ROLE=cloud "
                    "npm run build")
        entry = web_dist.get("entry")
        paths = {item["path"] for item in web_dist["files"]}
        if not entry or web_dist["root"] + "/" + entry not in paths:
            raise SystemExit(
                f"web dist entry {entry!r} is not one of the carried files")

    membership: dict[str, list[str]] = {}
    for rel in static:
        membership.setdefault(rel, []).append("static_superset")
    for rel in cloud:
        membership.setdefault(rel, []).append("cloud_runtime")

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "release_version": args.version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": GENERATOR,
        "source_root": ".",
        "python": {
            "implementation": platform.python_implementation(),
            "version": current,
            "major_minor": current_minor,
            "target": target_python,
            "matches_target": current_minor == target_python,
        },
        "dependencies": dependencies,
        "dependencies_lock_sha256": _sha256(args.dependencies),
        "closures": {
            "static_superset": {
                "roots": STATIC_ROOTS,
                "count": len(static),
                "files": static,
            },
            "cloud_runtime": {
                "roots": CLOUD_RUNTIME,
                "count": len(cloud),
                "files": cloud,
            },
            "static_only": sorted(set(static) - set(cloud)),
            "cloud_only": sorted(set(cloud) - set(static)),
        },
        "files": _files(root, static, membership),
        "deployment_files": _deployment_files(root, deploy_root),
        "web_dist": web_dist,
    }
    return manifest


def _without_generated_at(manifest: dict) -> dict:
    copy = dict(manifest)
    copy.pop("generated_at", None)
    return copy


def _check(existing_path: Path, manifest: dict) -> int:
    try:
        existing = json.loads(existing_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"existing manifest unreadable: {exc}", file=sys.stderr)
        return 65
    if _without_generated_at(existing) != _without_generated_at(manifest):
        print("manifest is stale: regenerate with make-manifest.py", file=sys.stderr)
        return 65
    print(f"manifest is current: {existing_path}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Freeze the UM cloud release closure and manifest")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent,
                        help="source/payload root")
    parser.add_argument("--deploy-root", type=Path,
                        default=Path(__file__).resolve().parent,
                        help="um-cloud-deploy directory inside the root")
    parser.add_argument("--version", required=True,
                        help="release version (e.g. s2-20261101)")
    parser.add_argument("--dependencies", type=Path,
                        default=Path(__file__).resolve().parent / "dependencies.lock.json",
                        help="dependencies.lock.json")
    parser.add_argument("--output", type=Path, default=None,
                        help="manifest output path (default: stdout)")
    parser.add_argument("--web-dist", type=Path, default=None,
                        help="built web/dist tree to carry as a whole")
    parser.add_argument("--allow-missing-web", action="store_true",
                        help="explicitly record web_dist.included=false")
    parser.add_argument("--require-web-role", choices=("local", "cloud"),
                        default=None,
                        help="refuse a web dist built for another console role")
    parser.add_argument("--require-web-line", default=None,
                        help="refuse a web dist built on another line "
                             "(release payloads require production)")
    parser.add_argument("--check", type=Path, default=None,
                        help="compare the existing manifest with current bytes")
    parser.add_argument("--print", action="store_true", dest="print_only",
                        help="print the manifest to stdout")
    args = parser.parse_args(argv)
    if not args.version or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                              "abcdefghijklmnopqrstuvwxyz0123456789._-"
                              for ch in args.version):
        print("unsafe release version", file=sys.stderr)
        return 64
    manifest = _build(args)
    if args.check is not None:
        return _check(args.check, manifest)
    text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.output.with_name(args.output.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, args.output)
    if args.output is None or args.print_only:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
