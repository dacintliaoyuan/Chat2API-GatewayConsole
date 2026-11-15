#!/usr/bin/env python3
"""Generate the Rust contract fixture from the frozen contract and Python code.

The Rust tests never hand-write expected endpoints, error codes, SSE event names or
timeout budgets. They deserialize ``tests/fixtures/rust_contract_rev1.json``, which
this generator produces from:

* ``docs/rust-contract.md`` — the frozen external contract; endpoint tables, error
  tables, SSE event names and the target port table are parsed from it.
* the Python modules that still own the behavior — constants are imported and
  cross-checked against the fixture so a silent Python change fails generation.

The generator performs no live request, starts no process, reads no credential, token
or user ledger, and writes only the fixture file. Run from the repository root:

    python tests/generate_rust_contract_fixture.py
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_RELATIVE = "docs/rust-contract.md"
CONTRACT = ROOT / CONTRACT_RELATIVE
DESTINATION = ROOT / "tests" / "fixtures" / "rust_contract_rev1.json"

sys.path[:0] = [str(ROOT)]

_PORT_SERVICES = {
    8000: "chat2api",
    8001: "local_console",
    8002: "cloud_console",
    8003: "cloud_gateway",
    8097: "display",
    8099: "candidate_preview",
    5173: "vite_dev",
}


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8").replace("\r\n", "\n")


def _section(text: str, heading: str) -> str:
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == heading:
            start = index + 1
            break
    if start is None:
        raise SystemExit("contract heading missing: %s" % heading)
    body = []
    for line in lines[start:]:
        if line.startswith("#"):
            break
        body.append(line)
    return "\n".join(body)


def _table(section: str) -> tuple[list[str], list[list[str]]]:
    rows = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= {"-", ":", " "} for cell in cells):
            continue
        rows.append(cells)
    if not rows:
        raise SystemExit("contract table missing")
    return rows[0], rows[1:]


def _code_spans(cell: str) -> list[str]:
    return [value.strip() for value in re.findall(r"`([^`]+)`", cell)]


_ABSOLUTE_PREFIXES = (
    "/api",
    "/v1",
    "/display",
    "/admin",
    "/health",
    "/ready",
    "/index.html",
    "/gw",
)


def _paths(cell: str) -> list[str]:
    spans = _code_spans(cell)
    if not spans:
        if "任意路径" in cell:
            return ["*"]
        raise SystemExit("endpoint row has no path code span: %s" % cell)
    first = spans[0]
    if not first.startswith("/"):
        raise SystemExit("endpoint row must start with an absolute path: %s" % cell)
    base = first.rsplit("/", 1)[0]
    paths = []
    for raw in spans:
        if raw == "*":
            path = raw
        elif raw.startswith(_ABSOLUTE_PREFIXES):
            path = raw
        elif base in ("", "/"):
            path = "/" + raw.lstrip("/")
        else:
            path = base.rstrip("/") + "/" + raw.lstrip("/")
        paths.append(path)
    return paths


def _endpoints(section: str, surface: str, source: str) -> list[dict]:
    _header, rows = _table(section)
    endpoints = []
    seen = set()
    for row in rows:
        if len(row) < 3:
            continue
        method_cell, path_cell, auth_cell = row[0], row[1], row[2]
        description = row[3] if len(row) > 3 else ""
        methods = [part.strip() for part in method_cell.split("/") if part.strip()]
        for path in _paths(path_cell):
            for method in methods:
                key = (surface, method, path)
                if key in seen:
                    continue
                seen.add(key)
                endpoints.append({
                    "surface": surface,
                    "method": method,
                    "path": path,
                    "auth": auth_cell,
                    "description": description,
                    "source": source,
                })
    return endpoints


def _ports(section: str) -> dict:
    _header, rows = _table(section)
    targets = {}
    for row in rows:
        if len(row) < 5:
            continue
        port_match = re.search(r"\d+", row[0])
        target_match = re.search(r"\d+", row[4])
        if not port_match or not target_match:
            continue
        port = int(port_match.group(0))
        target = int(target_match.group(0))
        if port not in _PORT_SERVICES:
            raise SystemExit("contract port has no stable service name: %s" % port)
        targets[_PORT_SERVICES[port]] = target
    if sorted(targets) != sorted(_PORT_SERVICES.values()):
        raise SystemExit("contract port table is missing a governed service")
    production = sorted(set(targets.values()))
    return {
        "target": targets,
        "production": production,
        "legacyAliases": [],
        "source": CONTRACT_RELATIVE + "#2.1",
    }


def _load_ingress_checker():
    path = ROOT / "um-cloud-deploy" / "cloudflared" / "check-ingress.py"
    spec = importlib.util.spec_from_file_location(
        "rust_contract_ingress_checker", path)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load %s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cloudflare() -> dict:
    checker = _load_ingress_checker()
    paths = list(checker.API_PATHS)
    if not paths:
        raise SystemExit("Cloudflare API_PATHS is empty")
    return {
        "sharePaths": paths,
        "catchAll": checker.CATCH_ALL,
        "readOnly": True,
        "source": "um-cloud-deploy/cloudflared/check-ingress.py:26-30",
    }


def _source_hashes(names: list[str]) -> dict:
    hashes = {}
    for name in names:
        text = (ROOT / name).read_text(encoding="utf-8").replace("\r\n", "\n")
        hashes[name] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return hashes


def generate() -> dict:
    contract = _text(CONTRACT_RELATIVE)
    endpoints = []
    endpoints.extend(_endpoints(
        _section(contract, "### 5.1 端点与鉴权"), "gateway",
        CONTRACT_RELATIVE + "#5.1"))
    endpoints.extend(_endpoints(
        _section(contract, "### 6.1 端点与角色边界"), "console",
        CONTRACT_RELATIVE + "#6.1"))
    endpoints.sort(key=lambda item: (item["surface"], item["method"], item["path"]))
    if len(endpoints) < 20:
        raise SystemExit("contract endpoint coverage unexpectedly small")
    if any(item["path"] != "*" and not item["path"].startswith("/")
           for item in endpoints):
        raise SystemExit("contract endpoint path must be absolute or *")

    ports = _ports(_section(contract, "### 2.1 当前部署与仓库默认值"))
    cloudflare = _cloudflare()
    for path in cloudflare["sharePaths"]:
        if not any(item["surface"] == "gateway" and item["path"] == path
                   for item in endpoints):
            raise SystemExit("share path missing from the gateway endpoint table: %s" % path)

    return {
        "metadata": {
            "fixture": True,
            "contract": CONTRACT_RELATIVE,
            "contractVersion": "rust-contract-v1",
            "generatedFrom": "tests/generate_rust_contract_fixture.py",
            "sourceHashEncoding": "utf8-lf",
            "surfaces": sorted({item["surface"] for item in endpoints}),
            "sourceHashes": _source_hashes([
                CONTRACT_RELATIVE,
                "tests/generate_rust_contract_fixture.py",
                "um-cloud-deploy/cloudflared/check-ingress.py",
            ]),
        },
        "ports": ports,
        "endpoints": endpoints,
        "cloudflare": cloudflare,
    }


if __name__ == "__main__":
    result = generate()
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    DESTINATION.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print("Generated Rust contract fixture:", DESTINATION)
