#!/usr/bin/env python3
"""Fail when the old/new share-chain names are mixed.

Checks the invariants the merged 8100 process depends on:

* ``chat2api/share_gateway.py`` no longer exists;
* production code never uses ``share_entry`` as the read-surface component key
  (the key is ``share_gateway``);
* ``chat2api/chat2api.py`` imports ``share_entry`` lazily, never at module top;
* production code never references the removed ``chat2api/share_gateway.py`` path.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

COMPONENT_KEY_FILES = (
    "console_share_actions.py",
    "share_broker_protocol.py",
    "share_backend_supervision.py",
    "web/src/workbench.ts",
    "web/scripts/backend-contract/check-generated.mjs",
    "tests/fixtures/frontend_contract_rev25.json",
    "web/scripts/console-gate/fixtures/api.json",
)

PRODUCTION_FILES = (
    "chat2api/chat2api.py",
    "chat2api/share_entry.py",
    "console_share_actions.py",
    "console_share_production.py",
    "console_adapters.py",
    "share_broker_ops.py",
    "share_management.py",
    "share_backend_supervision.py",
    "web/src/workbench.ts",
    "web/scripts/backend-contract/check-generated.mjs",
)


def main() -> int:
    errors: list[str] = []

    if (ROOT / "chat2api" / "share_gateway.py").exists():
        errors.append("chat2api/share_gateway.py still exists")

    for relative in COMPONENT_KEY_FILES:
        path = ROOT / relative
        if not path.exists():
            continue
        if '"share_entry"' in path.read_text(encoding="utf-8"):
            errors.append(
                "%s uses \"share_entry\" as a component key; "
                "the read-surface key is \"share_gateway\"" % relative)

    for relative in PRODUCTION_FILES:
        path = ROOT / relative
        if not path.exists():
            continue
        if "chat2api/share_gateway.py" in path.read_text(encoding="utf-8"):
            errors.append(
                "%s references the removed chat2api/share_gateway.py" % relative)

    chat2api = ROOT / "chat2api" / "chat2api.py"
    if chat2api.exists():
        tree = ast.parse(chat2api.read_text(encoding="utf-8-sig"),
                         filename=str(chat2api))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                names = {alias.name for alias in node.names}
                if node.module == "share_entry" or (
                        node.module in (None, "") and "share_entry" in names):
                    errors.append(
                        "chat2api.py imports share_entry at module top; "
                        "the import must stay inside the share branch")
            elif isinstance(node, ast.Import):
                if any(alias.name == "share_entry" for alias in node.names):
                    errors.append(
                        "chat2api.py imports share_entry at module top; "
                        "the import must stay inside the share branch")

    if errors:
        for error in errors:
            print("FAIL " + error, file=sys.stderr)
        return 1
    print("PASS share-chain names")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
