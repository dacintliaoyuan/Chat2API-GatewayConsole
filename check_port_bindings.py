#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audit live listening ports against the allocation table (AGENTS.md).

Read-only audit: runs netstat -ano, parses TCP LISTENING and UDP rows, and
classifies every socket inside the governed ranges this repository owns.
Sockets outside the governed ranges are counted as out-of-scope information
and never count as violations (Windows system ports, third-party apps).

Exit codes: 0 = conform; 1 = violations found; 2 = query or parse failure
(fail-closed: an empty or unparseable netstat result is a failure, never
conformity).
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

# Governance scope: segments owned by this repository per AGENTS.md, plus the
# individually listed ports that predate the segments. A listener inside a
# governed segment is reported as UNREGISTERED; a port outside every segment is
# out-of-scope information.
GOVERNED_RANGES: tuple[tuple[int, int], ...] = (
    (8000, 8009), (8090, 8099), (8100, 8119), (8300, 8399))
GOVERNED_SINGLETONS = frozenset({5173})

# Allocated ports: port -> (service label, allowed binding scope).
# 2026-11-01: the local console backend moved 8096 -> 8001 (cloud stays
# 18096); 8001 is registered inside the 8000-8009 segment by maintainer
# decision, while a listener on the old 8096 is now UNREGISTERED.
PORT_ALLOCATIONS: dict[int, tuple[str, str]] = {
    8000: ("chat2api", "loopback"),
    8001: ("usagesrv console backend", "loopback"),
    8100: ("um-share gateway", "loopback"),
    5173: ("Console dev server (vite)", "loopback"),
}

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "[::1]", "::1"})


@dataclass(frozen=True)
class Listener:
    proto: str
    host: str
    port: int
    pid: str


def run_netstat() -> str:
    """Run netstat -ano and return stdout; raise RuntimeError on failure."""
    proc = subprocess.run(
        ["netstat", "-ano"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30)
    if proc.returncode != 0:
        raise RuntimeError("netstat exited %s: %s" % (proc.returncode, proc.stderr.strip()))
    if not proc.stdout.strip():
        raise RuntimeError("netstat produced no output (query failure, not conformity)")
    return proc.stdout


def parse_listeners(text: str) -> list[Listener]:
    """Parse listening TCP sockets and UDP rows from netstat output.

    Data rows are ASCII even on localized Windows; header/blank lines whose
    first field is not TCP/UDP are skipped.
    """
    listeners: list[Listener] = []
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] not in ("TCP", "UDP"):
            continue
        if fields[0] == "TCP":
            if len(fields) != 5 or fields[3] != "LISTENING":
                continue
            proto, local, pid = fields[0], fields[1], fields[4]
        else:
            if len(fields) != 4:
                continue
            proto, local, pid = fields[0], fields[1], fields[3]
        host, sep, port_text = local.rpartition(":")
        if not sep or not host or not port_text.isdigit():
            continue
        listeners.append(Listener(proto, host, int(port_text), pid))
    return listeners


def is_governed(port: int) -> bool:
    if port in GOVERNED_SINGLETONS:
        return True
    return any(low <= port <= high for low, high in GOVERNED_RANGES)


def classify(listeners: list[Listener]) -> tuple[list[str], list[str]]:
    """Classify governed sockets; return (violations, information lines)."""
    violations: list[str] = []
    ok_lines: list[str] = []
    seen: set[tuple[str, str, int]] = set()
    out_of_scope = 0
    for item in listeners:
        key = (item.proto, item.host, item.port)
        if key in seen:
            continue
        seen.add(key)
        if not is_governed(item.port):
            out_of_scope += 1
            continue
        allocation = PORT_ALLOCATIONS.get(item.port)
        if allocation is None:
            violations.append(
                "UNREGISTERED       %s %s:%d  pid=%s  （治理段内未在 AGENTS.md 登记）"
                % (item.proto, item.host, item.port, item.pid))
            continue
        label, scope = allocation
        if scope == "loopback" and item.host not in _LOOPBACK_HOSTS:
            violations.append(
                "BINDING-MISMATCH   %s %s:%d  pid=%s  （%s 要求仅 loopback）"
                % (item.proto, item.host, item.port, item.pid, label))
            continue
        ok_lines.append(
            "OK                 %s %s:%d  pid=%s  （%s）"
            % (item.proto, item.host, item.port, item.pid, label))
    info = ["治理段外监听条目（仅展示，不算违规）: %d" % out_of_scope]
    return violations, ok_lines + info


def main() -> int:
    try:
        text = run_netstat()
        listeners = parse_listeners(text)
    except Exception as exc:  # noqa: BLE001 — audit must fail closed, loudly
        print("[端口审计] 查询失败：%s" % (exc,))
        print("结论：无法判定合规性（fail-closed），退出码 2。")
        return 2
    violations, info = classify(listeners)
    print("== 端口绑定审计（对照 AGENTS.md）==")
    for line in info:
        print(line)
    for line in violations:
        print(line)
    print("== 结论 ==")
    if violations:
        print("发现 %d 处违规：治理段内未登记监听或绑定越界，逐条核对后处置。" % len(violations))
        return 1
    print("治理段内全部监听已登记且绑定合规。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
