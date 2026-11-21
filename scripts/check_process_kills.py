#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail when a script stops the local gateway by process name.

The local App's 7999 gateway and the production 8000 ingress share the
`um-gateway-candidate.exe` name. Stopping by name (or by `Get-Process | Stop-Process`)
kills the production entry too, even when the preview runs on 7999. A safe stop
matches the parent PID, the executable path and the start time instead; see
`docs/agents/desktop-preview.md` and `output/desktop-sse-smoke.py`.

Exit codes: 0 clean; 1 dangerous stop pattern found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ["lib", "scripts", "tests", "output"]
SUFFIXES = {".ps1", ".py", ".bat", ".cmd", ".sh"}
EXCLUDED_DIRS = {".git", ".worktrees", ".worktrees", "node_modules", "target"}

PATTERNS = [
    (
        re.compile(
            r"Get-Process[^\r\n]*(?:um-gateway-candidate|um-desktop)[^\r\n]*\|\s*Stop-Process",
            re.IGNORECASE,
        ),
        "Get-Process <gateway> | Stop-Process stops every same-name gateway",
    ),
    (
        re.compile(
            r"Stop-Process[^\r\n]*-Name\s+['\"]?(?:um-gateway-candidate|um-desktop)",
            re.IGNORECASE,
        ),
        "Stop-Process -Name <gateway> stops every same-name gateway",
    ),
    (
        re.compile(
            r"taskkill[^\r\n]*/IM\s+['\"]?(?:um-gateway-candidate|um-desktop)",
            re.IGNORECASE,
        ),
        "taskkill /IM <gateway> stops every same-name gateway",
    ),
    (
        re.compile(
            r"pkill[^\r\n]*(?:um-gateway-candidate|um-desktop)",
            re.IGNORECASE,
        ),
        "pkill <gateway> stops every same-name gateway",
    ),
]


def iter_files() -> list[Path]:
    files: list[Path] = []
    for name in SCAN_DIRS:
        base = ROOT / name
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in SUFFIXES:
                continue
            if any(part in EXCLUDED_DIRS for part in path.relative_to(ROOT).parts):
                continue
            if path.resolve() == Path(__file__).resolve():
                continue
            files.append(path)
    return files


def main() -> int:
    problems: list[str] = []
    for path in iter_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            problems.append(f"{path.relative_to(ROOT)}: 无法读取（{error}）")
            continue
        for pattern, reason in PATTERNS:
            for line_number, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    problems.append(
                        f"{path.relative_to(ROOT)}:{line_number}: {reason}"
                    )
    if problems:
        print("dangerous gateway stop pattern found:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print(
            "只停止本轮 App PID 的子网关（父 PID + 可执行文件路径 + 启动时间匹配），"
            "或使用 Stop-GatewayScopedProcesses / Stop-Process -Id。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
