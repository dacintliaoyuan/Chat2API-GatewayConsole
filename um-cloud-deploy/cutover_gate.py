#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cutover acceptance gate (#72): candidate parity, cloud smoke and rollback.

The gate is deliberately split by what it may execute:

* ``candidate-parity`` runs the local Python/Rust parity tests.  It never binds a
  production port or touches a production data path.
* ``cloud-smoke`` only PRINTS the exact cloud commands and their expected
  results.  It never executes ``curl``, never reads a share Key and never contacts
  a real cloud host; the operator runs the plan and records the evidence.
* ``rollback-drill`` runs ``rollback-drill.sh`` in its plan mode by default.
  ``--execute`` is required before the drill stops or starts anything.

The evidence document is the record #73 consumes.  It names every command, whether
it was executed and the observed exit code, so a planned smoke check is never
reported as passed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "um-cutover-gate-v1"
DEFAULT_BASE_URL = "http://127.0.0.1:8003"
DEFAULT_SHARE_KEY_ENV = "UM_SHARE_KEY"
DEFAULT_MODEL = "um-share-model"
DEFAULT_HEALTH_PORT = "8100"
DEFAULT_CURRENT_HEALTH_PORT = "8003"
DATA_DIRS = [
    "/var/lib/um-share",
    "/var/lib/um-share-console",
    "/etc/um-share",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _python() -> str:
    override = os.environ.get("UM_GATE_PYTHON")
    if override:
        return override
    # The gate runs inside the interpreter that owns the project dependencies;
    # a Windows "python3" alias may be the Store stub without site-packages.
    if sys.executable:
        return sys.executable
    for name in ("python3.14", "python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    return "python"


def _cargo() -> str:
    override = os.environ.get("UM_GATE_CARGO")
    if override:
        return override
    return shutil.which("cargo") or "cargo"


def _bash() -> str:
    override = os.environ.get("UM_GATE_BASH")
    if override:
        return override
    return shutil.which("bash") or "bash"


def _run(command: list[str], cwd: Path) -> dict:
    proc = subprocess.run(command, cwd=str(cwd), capture_output=True,
                          text=True)
    return {
        "command": command,
        "exit_code": proc.returncode,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }


def _evidence_file(path: str | None) -> Path | None:
    return Path(path).expanduser().resolve() if path else None


def _write_evidence(path: Path | None, document: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def candidate_parity(repo_root: Path, python: str, cargo: str,
                    skip_rust: bool) -> dict:
    commands = []
    python_result = _run(
        [python, "-m", "unittest", "tests.test_cutover_parity", "-v"],
        repo_root)
    commands.append(python_result)
    rust_result = None
    if not skip_rust:
        rust_result = _run(
            [cargo, "test", "--manifest-path", "rust/Cargo.toml",
             "--workspace", "--offline"],
            repo_root)
        commands.append(rust_result)
    passed = all(item["exit_code"] == 0 for item in commands)
    return {
        "status": "passed" if passed else "failed",
        "executed": True,
        "python": python_result,
        "rust": rust_result,
        "commands": commands,
    }


def cloud_smoke_checks(base_url: str, share_key_env: str, model: str,
                       public_url: str | None) -> list[dict]:
    status = "curl -sS -o /dev/null -w '%{http_code}'"
    checks = [
        {
            "name": "ready",
            "command": f'{status} "$BASE_URL/ready"',
            "expected": "200",
        },
        {
            "name": "health",
            "command": f'{status} "$BASE_URL/health"',
            "expected": "200",
        },
        {
            "name": "models-with-key",
            "command": (
                f'{status} -H "Authorization: Bearer ${share_key_env}" '
                '"$BASE_URL/v1/models"'
            ),
            "expected": "200",
        },
        {
            "name": "models-without-key",
            "command": f'{status} "$BASE_URL/v1/models"',
            "expected": "401 or 403",
        },
        {
            "name": "chat-nonstream",
            "command": (
                f'{status} '
                f'-H "Authorization: Bearer ${share_key_env}" '
                '-H "Content-Type: application/json" '
                f'-d \'{{"model":"{model}","messages":[{{"role":"user",'
                f'"content":"cutover smoke"}}],"stream":false}}\' '
                '"$BASE_URL/v1/chat/completions"'
            ),
            "expected": "200 with choices[] and usage",
        },
        {
            "name": "chat-stream-sse",
            "command": (
                'curl -sS -N --max-time 60 '
                f'-H "Authorization: Bearer ${share_key_env}" '
                '-H "Content-Type: application/json" '
                f'-d \'{{"model":"{model}","messages":[{{"role":"user",'
                f'"content":"cutover smoke"}}],"stream":true}}\' '
                '"$BASE_URL/v1/chat/completions"'
            ),
            "expected": (
                "200; text/event-stream; at least one data: event and "
                "data: [DONE]"
            ),
        },
        {
            "name": "sse-client-disconnect",
            "command": (
                'timeout 5 curl -sS -N '
                f'-H "Authorization: Bearer ${share_key_env}" '
                '-H "Content-Type: application/json" '
                f'-d \'{{"model":"{model}","messages":[{{"role":"user",'
                f'"content":"cutover smoke long stream"}}],"stream":true}}\' '
                '"$BASE_URL/v1/chat/completions"'
            ),
            "expected": (
                "at least one data: event before the timeout; then the share "
                "ledger's newest row has status=client_disconnected"
            ),
        },
        {
            "name": "ledger-row",
            "command": 'tail -n 1 "$UM_LEDGER"',
            "expected": (
                "one JSON row with key_id, model, request_id, usage_source "
                "and status"
            ),
        },
        {
            "name": "key-revoke-in-flight",
            "command": (
                'curl -sS -X POST '
                '-H "X-Admin-Control: $UM_CONTROL_TOKEN" '
                '-H "Content-Type: application/json" '
                '-d \'{"expected_row_version": \'"$UM_KEY_ROW_VERSION"\'}\' '
                '"$UM_CONSOLE_URL/api/console/share-management/keys/'
                '$UM_KEY_ID/revoke-and-terminate"'
            ),
            "expected": (
                "200 with revoked=true; the in-flight request closes and the "
                "new ledger row is attributed to the revoked key"
            ),
        },
    ]
    if public_url:
        checks.extend([
            {
                "name": "public-models-with-key",
                "command": (
                    f'{status} -H "Authorization: Bearer ${share_key_env}" '
                    '"${PUBLIC_URL%/}/v1/models"'
                ),
                "expected": "200",
            },
            {
                "name": "public-health",
                "command": f'{status} "${{PUBLIC_URL%/}}/health"',
                "expected": "200",
            },
        ])
    return checks


def cloud_smoke_plan(base_url: str, share_key_env: str, model: str,
                     public_url: str | None) -> dict:
    return {
        "status": "planned",
        "executed": False,
        "base_url": base_url,
        "public_url": public_url,
        "model": model,
        "share_key_env": share_key_env,
        "note": (
            "operator-only: run these commands on the cloud host and record "
            "each observed status; this gate never executes them"
        ),
        "checks": cloud_smoke_checks(
            base_url, share_key_env, model, public_url),
    }


def rollback_drill_plan(repo_root: Path, target: str, health_port: str,
                        current_health_port: str,
                        data_dirs: list[str]) -> dict:
    script = repo_root / "um-cloud-deploy" / "rollback-drill.sh"
    command = [
        _bash(), str(script), "--to", target,
        "--health-port", health_port,
        "--current-health-port", current_health_port,
    ]
    for directory in data_dirs:
        command.extend(["--data-dir", directory])
    return {
        "status": "planned",
        "executed": False,
        "command": command,
        "script": str(script),
    }


def rollback_drill(repo_root: Path, target: str, health_port: str,
                   current_health_port: str, data_dirs: list[str],
                   execute: bool) -> dict:
    plan = rollback_drill_plan(repo_root, target, health_port,
                              current_health_port, data_dirs)
    if not execute:
        return plan
    command = plan["command"] + ["--execute"]
    result = _run(command, repo_root)
    return {
        "status": "passed" if result["exit_code"] == 0 else "failed",
        "executed": True,
        "command": command,
        "result": result,
    }


def _summary(document: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(document, ensure_ascii=False, indent=2))
        return
    parity = document.get("candidate_parity")
    smoke = document.get("cloud_smoke")
    drill = document.get("rollback_drill")
    print("cutover gate: candidate-parity=%s cloud-smoke=%s "
          "rollback-drill=%s" % (
              parity["status"] if parity else "not-run",
              smoke["status"] if smoke else "not-run",
              drill["status"] if drill else "not-run"))
    if smoke:
        print("cloud smoke is a PLAN only; run its commands on the cloud host")
        for check in smoke["checks"]:
            print("  %s: %s" % (check["name"], check["command"]))
            print("    expected: %s" % check["expected"])
    if parity and parity["status"] != "passed":
        for item in parity["commands"]:
            print("  %s -> %s" % (" ".join(item["command"]),
                                  item["exit_code"]))
    if drill:
        if drill["executed"]:
            print("  rollback drill: %s" % drill["status"])
        else:
            print("  rollback drill plan: %s"
                  % " ".join(drill["command"]))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-root", default=None,
                        help="repository/release root (default: this file's "
                             "parent)")
    parser.add_argument("--python", default=None,
                        help="Python interpreter for candidate parity")
    parser.add_argument("--cargo", default=None,
                        help="cargo executable for candidate parity")
    parser.add_argument("--evidence", default=None,
                        help="write the JSON evidence document here")
    parser.add_argument("--json", action="store_true",
                        help="print the JSON evidence document")

    subparsers = parser.add_subparsers(dest="command", required=True)
    parity = subparsers.add_parser("candidate-parity")
    parity.add_argument("--skip-rust", action="store_true")
    parity.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    parity.add_argument("--evidence", default=argparse.SUPPRESS)

    smoke = subparsers.add_parser("cloud-smoke")
    smoke.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    smoke.add_argument("--evidence", default=argparse.SUPPRESS)
    smoke.add_argument("--base-url", default=DEFAULT_BASE_URL)
    smoke.add_argument("--public-url", default=None)
    smoke.add_argument("--share-key-env", default=DEFAULT_SHARE_KEY_ENV)
    smoke.add_argument("--model", default=DEFAULT_MODEL)

    drill = subparsers.add_parser("rollback-drill")
    drill.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    drill.add_argument("--evidence", default=argparse.SUPPRESS)
    drill.add_argument("--to", required=True)
    drill.add_argument("--health-port", default=DEFAULT_HEALTH_PORT)
    drill.add_argument("--current-health-port",
                       default=DEFAULT_CURRENT_HEALTH_PORT)
    drill.add_argument("--data-dir", action="append", default=None)
    drill.add_argument("--execute", action="store_true")

    all_parser = subparsers.add_parser("all")
    all_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    all_parser.add_argument("--evidence", default=argparse.SUPPRESS)
    all_parser.add_argument("--to", required=True)
    all_parser.add_argument("--health-port", default=DEFAULT_HEALTH_PORT)
    all_parser.add_argument("--current-health-port",
                            default=DEFAULT_CURRENT_HEALTH_PORT)
    all_parser.add_argument("--data-dir", action="append", default=None)
    all_parser.add_argument("--skip-rust", action="store_true")
    all_parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    all_parser.add_argument("--public-url", default=None)
    all_parser.add_argument("--share-key-env", default=DEFAULT_SHARE_KEY_ENV)
    all_parser.add_argument("--model", default=DEFAULT_MODEL)
    all_parser.add_argument("--execute-rollback", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve() if args.repo_root \
        else Path(__file__).resolve().parents[1]
    evidence = _evidence_file(args.evidence)
    python = args.python or _python()
    cargo = args.cargo or _cargo()

    if args.command == "candidate-parity":
        document = {
            "schema": SCHEMA,
            "generated_at": _now(),
            "candidate_parity": candidate_parity(
                repo_root, python, cargo, args.skip_rust),
        }
        _summary(document, args.json)
        _write_evidence(evidence, document)
        return 0 if document["candidate_parity"]["status"] == "passed" else 1

    if args.command == "cloud-smoke":
        document = {
            "schema": SCHEMA,
            "generated_at": _now(),
            "cloud_smoke": cloud_smoke_plan(
                args.base_url, args.share_key_env, args.model,
                args.public_url),
        }
        _summary(document, args.json)
        _write_evidence(evidence, document)
        return 0

    data_dirs = args.data_dir or DATA_DIRS
    if args.command == "rollback-drill":
        document = {
            "schema": SCHEMA,
            "generated_at": _now(),
            "rollback_drill": rollback_drill(
                repo_root, args.to, args.health_port,
                args.current_health_port, data_dirs, args.execute),
        }
        _summary(document, args.json)
        _write_evidence(evidence, document)
        return 0 if document["rollback_drill"]["status"] in (
            "planned", "passed") else 1

    # all: run local parity, then plan the operator-only gates.
    data_dirs = args.data_dir or DATA_DIRS
    document = {
        "schema": SCHEMA,
        "generated_at": _now(),
        "candidate_parity": candidate_parity(
            repo_root, python, cargo, args.skip_rust),
        "cloud_smoke": cloud_smoke_plan(
            args.base_url, args.share_key_env, args.model,
            args.public_url),
        "rollback_drill": rollback_drill(
            repo_root, args.to, args.health_port,
            args.current_health_port, data_dirs, args.execute_rollback),
    }
    _summary(document, args.json)
    _write_evidence(evidence, document)
    return 0 if document["candidate_parity"]["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
