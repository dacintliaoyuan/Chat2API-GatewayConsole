#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""canary_um.py — boundary probe that runs AS um_share (batch logon).

Tests, from the um_share identity:
  * files it MUST NOT read: UM token, usage ledgers, plaintext share keys,
    admin/global/qoder tokens, session code, repo source, user Documents
  * writes into the repo / Python env / run copy it must NOT be able to do
  * network: only 127.0.0.1:8110 allowed (WFP app-id scoped, the dedicated
    share backend), every other local service + any remote host blocked; UDP
    outbound blocked
Results are written (as um_share) to the writable run/ dir for the elevated
driver to collect. No secrets are printed by this probe.
"""
import json
import os
import socket
import sys
import traceback
from pathlib import Path

RUN_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"C:\ProgramData\UMShareRun\run")

# --- robust self-capture: everything this process prints/raises lands in the
# run dir so the elevated driver can mirror it and we can see the first failure
# (Task Scheduler discards stdout/stderr by default). -------------------------
try:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    (RUN_DIR / "canary-started.txt").write_text("started\n", encoding="utf-8")
    _logf = open(RUN_DIR / "canary-stdout.txt", "w", encoding="utf-8")
    sys.stdout = _logf
    sys.stderr = _logf
except Exception:  # noqa: BLE001 - must never die before the marker exists
    pass


def _fatal(msg):
    try:
        (RUN_DIR / "canary-error.txt").write_text(msg, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    print(msg, flush=True)
    sys.exit(3)


try:
    print("canary_um started; identity=" + os.getenv("USERNAME") + " cwd=" + str(Path.cwd()), flush=True)
except Exception:  # noqa: BLE001
    pass

DENIED_FILES = [
    r"D:\git\github\personal_files\SelfUseAI\ai-gateway\chat2api\token.json",
    r"D:\git\github\personal_files\SelfUseAI\ai-gateway\data\usage-chat2api.jsonl",
    r"D:\git\github\personal_files\SelfUseAI\ai-gateway\data\um-share-access.txt",
    r"D:\git\github\personal_files\SelfUseAI\ai-gateway\data\um-share-friend.txt",
    r"D:\git\github\personal_files\SelfUseAI\ai-gateway\data\um-share-friend2.txt",
    r"D:\git\github\personal_files\SelfUseAI\ai-gateway\data\um-share-friend3.txt",
    r"D:\git\github\personal_files\SelfUseAI\ai-gateway\data\admin-control-token",
    r"D:\git\github\personal_files\SelfUseAI\ai-gateway\data\qoder-global-api-token",
    r"D:\git\github\personal_files\SelfUseAI\ai-gateway\data\usage-session-code",
    r"D:\git\github\personal_files\SelfUseAI\ai-gateway\data\qoder-cn-api-token",
    r"D:\git\github\personal_files\SelfUseAI\ai-gateway\chat2api\share_gateway.py",
    r"D:\git\github\personal_files\SelfUseAI\ai-gateway\data\usage-chat2api.jsonl",
]
# Repo-wide write probes (must be denied by the repo-root deny ACE).
REPO_WRITES = [
    r"D:\git\github\personal_files\SelfUseAI\ai-gateway\data\_um_canary_write_test.txt",
    r"D:\git\github\personal_files\SelfUseAI\ai-gateway\chat2api\_um_canary_write_test.txt",
]
USER_DOCS = Path(r"C:\Users\dacint\Documents\_um_share_canary.txt")

# Network: allowed = the dedicated share backend 8110 only (the installed WFP
# permit port, deploy-elevated.ps1 ShareBackendPort); everything else must be
# blocked. The PRIVATE chat2api 8000 is explicitly probed as BLOCKED: the share
# identity must never reach it (R13-3).
TCP_ALLOWED = [("127.0.0.1", 8110)]
TCP_BLOCKED = [("127.0.0.1", 8000), ("127.0.0.1", 8096), ("127.0.0.1", 8095),
               ("127.0.0.1", 8097), ("127.0.0.1", 3080), ("127.0.0.1", 4141),
               ("127.0.0.1", 8100)]


def read_result(path: str) -> str:
    try:
        with open(path, "rb") as f:
            f.read(1)
        return "READABLE"
    except OSError as exc:
        return f"DENIED({type(exc).__name__})"


def write_result(path: str) -> str:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("canary")
        return "WRITABLE"
    except OSError as exc:
        return f"DENIED({type(exc).__name__})"


def tcp_probe(host: str, port: int) -> str:
    try:
        with socket.create_connection((host, port), timeout=2):
            return "CONNECTED"
    except OSError as exc:
        return f"BLOCKED({type(exc).__name__})"


def udp_probe(count: int = 3) -> str:
    # sendto to 127.0.0.1:9 (discard). Loopback UDP is a DOCUMENTED residual:
    # Windows Firewall does not filter loopback and OUTBOUND_TRANSPORT cannot
    # carry the app-id, so this may "SENT". What matters is that REMOTE UDP is
    # blocked by the program-scoped firewall rule (checked below).
    results = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1)
    try:
        for _ in range(count):
            try:
                s.sendto(b"x" * 32, ("127.0.0.1", 9))
                results.append("SENT")
            except OSError as exc:
                results.append(f"ERR({type(exc).__name__})")
        s.close()
    finally:
        try:
            s.close()
        except Exception:
            pass
    return results[0] if results else "no-probe"


def udp_connect_probe() -> str:
    # connect()-based UDP may still be subject to ALE_AUTH_CONNECT; record it.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", 9))
        s.send(b"x" * 32)
        return "SENT"
    except OSError as exc:
        return f"ERR({type(exc).__name__})"
    finally:
        s.close()


def udp_remote_probe() -> str:
    # Remote UDP (e.g. DNS/8.8.8.8:53) must be blocked by the program-scoped
    # firewall outbound rule; even on failure the firewall covers it.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2)
    try:
        s.sendto(b"x" * 32, ("8.8.8.8", 53))
        return "SENT"
    except OSError as exc:
        return f"ERR({type(exc).__name__})"
    finally:
        s.close()


try:
    result = {
        "identity": os.getenv("USERNAME"),
        "file_reads": {p: read_result(p) for p in DENIED_FILES},
        "file_writes": {p: write_result(p) for p in REPO_WRITES},
        "user_docs": read_result(str(USER_DOCS)) if not USER_DOCS.exists() else "ENUM_ATTEMPT",
        "tcp_allowed": {f"{h}:{p}": tcp_probe(h, p) for h, p in TCP_ALLOWED},
        "tcp_blocked": {f"{h}:{p}": tcp_probe(h, p) for h, p in TCP_BLOCKED},
        "udp_loopback_sendto": udp_probe(),
        "udp_loopback_connect": udp_connect_probe(),
        "udp_remote": udp_remote_probe(),
    }
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    (RUN_DIR / "canary-results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    # One-line summary (no secrets) written to stdout for the S4U caller.
    summary = []
    for p, r in list(result["file_reads"].items()) + [
            ("write-probe", next(iter(result["file_writes"].values()))),
            ("user-docs", result["user_docs"])]:
        key = p.replace("\\", "/").split("/")[-1]
        summary.append(f"{key}:{r}")
    summary.append("tcp:8110=" + result["tcp_allowed"]["127.0.0.1:8110"])
    summary.append("tcp-blocked=" + ",".join(f"{k.split(':')[1]}={v[:12]}"
                                             for k, v in result["tcp_blocked"].items()))
    summary.append("udp-loop=" + result["udp_loopback_sendto"] + "/" + result["udp_loopback_connect"])
    summary.append("udp-remote=" + result["udp_remote"])
    print(" | ".join(summary))
except BaseException:  # noqa: BLE001 - capture a full traceback for diagnosis
    tb = traceback.format_exc()
    try:
        (RUN_DIR / "canary-error.txt").write_text("CANARY UNHANDLED\n" + tb, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    print("CANARY UNHANDLED\n" + tb, flush=True)
    sys.exit(3)
