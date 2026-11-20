#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail when current docs point at a file that no longer exists.

Docs use backticked ``path:line`` references as evidence. A deleted Python
closure leaves those references dangling; this guard catches them before a reader
follows one into a 404.

Historical records (ADRs, cutover logs, plans) are allowed to cite retired paths:
they describe what was true at the time. The frozen contract keeps its Python
source references as behavioural provenance. Everything else must resolve.

Exit codes: 0 clean; 1 dangling reference.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = [ROOT / "docs", ROOT / "README.md", ROOT / "AGENTS.md", ROOT / "GLOSSARY.md"]
REFERENCE = re.compile(r"`([^`\s]+\.(?:py|rs|sh|md|ts|js|json|ps1)):(\d+(?:-\d+)?)`")

# Historical/retired sources stay cited as history, not as live files.
ALLOW_PREFIXES = (
    "docs/adr/",
    "docs/cutover-",
    "docs/rust-contract.md",
    "docs/rust-plan.md",
    "docs/rust-s3b-acceptance.md",
    "docs/share-chain-map.md",
    "docs/headless-login-acceptance.md",
    "docs/research/",
    "docs/rust-s",
)


def _markdown_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(sorted(root.rglob("*.md")))
    return files


def main() -> int:
    problems = []
    for path in _markdown_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(ALLOW_PREFIXES):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in REFERENCE.finditer(text):
            target = match.group(1)
            if (ROOT / target).exists():
                continue
            problems.append(f"{relative}: {match.group(0)}")
    if problems:
        print("docs cite files that do not exist:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
