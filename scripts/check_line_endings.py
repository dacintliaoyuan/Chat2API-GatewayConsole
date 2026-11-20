#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail when a modified text file changes its line-ending style.

The repo carries a mix of LF and CRLF files (legacy Windows files vs Rust sources).
Rewriting a CRLF file as LF turns a small edit into a whole-file diff and makes
review/blame unreadable. This guard compares each modified tracked file with HEAD
and refuses a style change; an intentional one-time normalisation must be listed in
``ALLOW`` with the reason.

Exit codes: 0 clean; 1 style change found.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Intentional one-time normalisations go here, one path per line with a reason.
ALLOW: frozenset[str] = frozenset()


def _style(raw: bytes) -> str:
    if b"\x00" in raw:
        return "binary"
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n")
    if crlf == 0:
        return "lf"
    if crlf == lf:
        return "crlf"
    return "mixed"


def _head_bytes(path: str) -> bytes | None:
    proc = subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        cwd=ROOT,
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def main() -> int:
    proc = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        return 1
    problems = []
    for name in proc.stdout.splitlines():
        if not name or name in ALLOW:
            continue
        head = _head_bytes(name)
        if head is None:
            continue
        path = ROOT / name
        if not path.is_file():
            continue
        before = _style(head)
        after = _style(path.read_bytes())
        if before == "binary" or after == "binary":
            continue
        if before != after:
            problems.append(f"{name}: {before} -> {after}")
    if problems:
        print("line-ending style changed on modified files:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print("keep the file's existing style, or add it to ALLOW with a reason.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
