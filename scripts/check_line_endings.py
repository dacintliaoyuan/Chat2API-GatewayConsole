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
ALLOW: frozenset[str] = frozenset({
    # The Magpie palette rewrite replaces the legacy mixed-ending file; the
    # rewritten tokens are LF like the rest of the desktop UI.
    "desktop/ui/theme.slint",
    # The Lucide-backed icon map is new and follows the desktop UI's LF style.
    "desktop/ui/icons.slint",
})


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


def _rev_bytes(rev: str, path: str) -> bytes | None:
    proc = subprocess.run(
        ["git", "show", f"{rev}:{path}"],
        cwd=ROOT,
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def _since_revision(argv: list[str]) -> str | None:
    args = list(argv[1:])
    since = None
    while args:
        arg = args.pop(0)
        if arg == "--since" and args:
            since = args.pop(0)
        else:
            print(f"usage: check_line_endings.py [--since REV]", file=sys.stderr)
            raise SystemExit(1)
    return since


def main(argv: list[str]) -> int:
    # Default mode compares the worktree with HEAD so the pre-commit gate sees the
    # pending edit. ``--since REV`` compares two commits instead: a branch that
    # already committed a whole-file rewrite would otherwise pass the default mode
    # because the worktree is clean.
    since = _since_revision(argv)
    diff_range = ["HEAD"] if since is None else [since, "HEAD"]
    proc = subprocess.run(
        ["git", "diff", "--name-only", *diff_range],
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
        if since is None:
            before_raw = _rev_bytes("HEAD", name)
            path = ROOT / name
            after_raw = path.read_bytes() if path.is_file() else None
        else:
            before_raw = _rev_bytes(since, name)
            after_raw = _rev_bytes("HEAD", name)
        if before_raw is None or after_raw is None:
            continue
        before = _style(before_raw)
        after = _style(after_raw)
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
    raise SystemExit(main(sys.argv))
