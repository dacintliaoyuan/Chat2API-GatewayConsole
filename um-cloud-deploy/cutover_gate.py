#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cutover acceptance gate (#72/#78): parity, cloud smoke and rollback.

The gate is deliberately split by what it may execute:

* ``candidate-parity`` runs the local Python/Rust parity tests.  It never binds a
  production port or touches a production data path.
* ``console-parity`` starts the Rust candidate console on an ephemeral loopback
  port with the same data-directory environment as the Python console and diffs
  the summary, keys, usage, logs and display read-only surfaces.
* ``cloud-smoke`` only PRINTS the exact cloud commands and their expected
  results.  It never executes ``curl``, never reads a share Key and never contacts
  a real cloud host; the operator runs the plan and records the evidence.
* ``rollback-drill`` runs ``rollback-drill.sh`` in its plan mode by default.
  ``--execute`` is required before the drill stops or starts anything.
* ``console-rollback-drill`` switches the 8002 console to the Python 8096
  rollback unit, proves the console and the 8003 gateway, then switches back.
  It also plans by default and never deletes a unit.

The evidence document is the record #73 consumes.  It names every command, whether
it was executed and the observed exit code, so a planned smoke check is never
reported as passed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "um-cutover-gate-v1"
DEFAULT_BASE_URL = "http://127.0.0.1:8003"
DEFAULT_CONSOLE_URL = "http://127.0.0.1:8002"
DEFAULT_SHARE_KEY_ENV = "UM_SHARE_KEY"
DEFAULT_MODEL = "um-share-model"
DEFAULT_HEALTH_PORT = "8100"
DEFAULT_CURRENT_HEALTH_PORT = "8003"
DEFAULT_PYTHON_CONSOLE_UNIT = "um-share-console-python.service"
DEFAULT_RUST_CONSOLE_UNIT = "um-share-console.service"
DEFAULT_PYTHON_CONSOLE_PORT = "8096"
DEFAULT_RUST_CONSOLE_PORT = "8002"
DATA_DIRS = [
    "/var/lib/um-share",
    "/var/lib/um-share-console",
    "/etc/um-share",
]
# The read-only console surfaces the 8002 cutover compares.  Every entry is a
# surface name plus the exact GET paths Python and Rust must answer identically.
CONSOLE_SURFACES = (
    ("summary", ("/api/summary",)),
    ("keys", ("/api/console/share-management/keys",)),
    ("usage", ("/api/console/share-usage",
               "/api/console/share-usage/requests")),
    ("logs", ("/api/console/logs",)),
    # display-session is the data-directory availability face: it reports the
    # relay state without folding in the external chat2api auth observation.
    ("display", ("/api/console/share-management/display-session",)),
)
# Values that legitimately differ between two reads of the same data: the read
# time, the cache age and display lease deadlines.  They are normalized away so
# the diff still catches a real payload change.
VOLATILE_CONSOLE_KEYS = frozenset({
    "generatedAt",
    "generated_epoch",
    "_generated_epoch",
    "_cached_age_seconds",
    "cached_age_seconds",
    "cache_age_seconds",
    "checkedAt",
    "checked_at",
    "observed_at",
    "expires_at",
    "login_window_deadline_epoch",
    "cached",
})


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


def _normalize_console(value):
    if isinstance(value, dict):
        return {
            key: _normalize_console(item)
            for key, item in value.items()
            if key not in VOLATILE_CONSOLE_KEYS
        }
    if isinstance(value, list):
        return [_normalize_console(item) for item in value]
    return value


def _probe_http(url: str, timeout: float) -> int | None:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _fetch_console_surface(base_url: str, path: str, timeout: float) -> dict:
    url = base_url.rstrip("/") + path
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            data = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            data = error.read()
        finally:
            error.close()
    except (urllib.error.URLError, OSError, ValueError) as error:
        return {"path": path, "status": None,
                "error": "%s: %s" % (type(error).__name__, error)}
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        body = data.decode("utf-8", "replace")
    return {"path": path, "status": status, "body": body}


def _read_env_file(path: str) -> dict:
    values = {}
    text = Path(path).expanduser().read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _rewrite_env_port(path: str, key: str, value: str) -> bytes:
    """Temporarily point one env file at the drill listener.

    systemd's EnvironmentFile overrides the unit's Environment= lines, so the
    Python rollback unit would otherwise bind the installed 8002.  Only the one
    port key is rewritten; every other line (including secrets) is preserved and
    the caller restores the exact original bytes after the drill.
    """
    target = Path(path)
    original = target.read_bytes()
    text = original.decode("utf-8")
    lines = text.splitlines(keepends=True)
    replaced = False
    output = []
    for line in lines:
        ending = "\r\n" if line.endswith("\r\n") else (
            "\n" if line.endswith("\n") else "")
        stripped = line.rstrip("\r\n")
        candidate = stripped[7:].strip() if stripped.startswith("export ") \
            else stripped
        if candidate.startswith(key + "="):
            output.append(key + "=" + value + ending)
            replaced = True
        else:
            output.append(line)
    if not replaced:
        if output and not output[-1].endswith(("\n", "\r")):
            output[-1] += "\n"
        output.append(key + "=" + value + "\n")
    tmp = target.with_name(target.name + ".drill.tmp")
    tmp.write_bytes("".join(output).encode("utf-8"))
    os.replace(tmp, target)
    return original


def _restore_file(path: str, data: bytes) -> None:
    target = Path(path)
    tmp = target.with_name(target.name + ".drill.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def _stop_process(process) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def _start_rust_console(repo_root: Path, command: list[str] | None,
                         rust_console: str | None,
                         env_file: str | None,
                         timeout: float) -> tuple[str, subprocess.Popen]:
    if command is None and rust_console:
        command = [rust_console]
    if command is None:
        binary = repo_root / "rust" / "target" / "release" / \
            "um-console-candidate"
        if not binary.is_file():
            raise RuntimeError(
                "rust candidate console is missing: %s" % binary)
        command = [str(binary)]
    command = list(command)
    if "--mode" not in command:
        command.extend(["--mode", "candidate"])
    env = os.environ.copy()
    if env_file:
        env.update(_read_env_file(env_file))
    process = subprocess.Popen(
        command, cwd=str(repo_root), env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1)
    lines: list[str] = []
    ready = threading.Event()

    def reader() -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            lines.append(line)
            if "um-console listening on http://" in line:
                ready.set()
                break

    threading.Thread(target=reader, name="um-console-startup",
                     daemon=True).start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in list(lines):
            marker = "um-console listening on http://"
            if marker in line:
                address = line.split(marker, 1)[1].strip()
                if not address.startswith("http"):
                    address = "http://" + address
                return address.rstrip("/"), process
        if process.poll() is not None:
            stderr = ""
            if process.stderr is not None:
                stderr = process.stderr.read()[-2000:]
            _stop_process(process)
            raise RuntimeError(
                "rust candidate console exited before it listened: %s"
                % (stderr or "exit %s" % process.returncode))
        time.sleep(0.05)
    _stop_process(process)
    raise RuntimeError(
        "rust candidate console did not print its listen address within %ss"
        % timeout)


def console_parity(python_url: str, rust_url: str | None,
                   timeout: float = 10.0,
                   surfaces: tuple[str, ...] | None = None,
                   rust_console_command: list[str] | None = None,
                   rust_console: str | None = None,
                   env_file: str | None = None,
                   repo_root: Path | None = None) -> dict:
    repo_root = repo_root or Path(__file__).resolve().parents[1]
    wanted = set(surfaces or [name for name, _ in CONSOLE_SURFACES])
    process = None
    started_error = None
    if rust_url is None:
        try:
            rust_url, process = _start_rust_console(
                repo_root, rust_console_command, rust_console, env_file,
                timeout)
        except (OSError, RuntimeError, ValueError) as error:
            started_error = "%s: %s" % (type(error).__name__, error)
    document = {
        "status": "failed",
        "executed": True,
        "python_url": python_url,
        "rust_url": rust_url,
        "timeout_seconds": timeout,
        "surfaces": [],
        "differences": [],
    }
    try:
        if started_error is not None:
            document["error"] = started_error
            document["differences"].append({
                "surface": "candidate",
                "detail": started_error,
            })
            return document
        for name, paths in CONSOLE_SURFACES:
            if name not in wanted:
                continue
            python_views = [
                _fetch_console_surface(python_url, path, timeout)
                for path in paths
            ]
            rust_views = [
                _fetch_console_surface(rust_url, path, timeout)
                for path in paths
            ]
            differences = []
            for path, python_view, rust_view in zip(
                    paths, python_views, rust_views):
                python_body = _normalize_console(python_view.get("body"))
                rust_body = _normalize_console(rust_view.get("body"))
                if (python_view.get("status") != rust_view.get("status")
                        or python_body != rust_body
                        or python_view.get("error") != rust_view.get("error")):
                    differences.append({
                        "path": path,
                        "python_status": python_view.get("status"),
                        "rust_status": rust_view.get("status"),
                        "python_body": python_body,
                        "rust_body": rust_body,
                        "python_error": python_view.get("error"),
                        "rust_error": rust_view.get("error"),
                    })
            document["surfaces"].append({
                "name": name,
                "paths": list(paths),
                "status": "passed" if not differences else "failed",
                "python": python_views,
                "rust": rust_views,
                "differences": differences,
            })
            document["differences"].extend(
                dict(item, surface=name) for item in differences)
        document["status"] = (
            "passed" if not document["differences"] else "failed")
        return document
    finally:
        if process is not None:
            _stop_process(process)


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


def _wait_port(port: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", int(port)),
                                          timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def console_rollback_drill_plan(python_console_unit: str,
                               rust_console_unit: str,
                               python_console_port: str,
                               rust_console_port: str,
                               gateway_url: str) -> list[dict]:
    def systemctl(name: str, unit: str) -> dict:
        return {"name": name, "kind": "systemctl",
                "command": ["systemctl", "stop" if name.startswith("stop")
                            else "start", unit]}

    def probe(name: str, url: str) -> dict:
        return {"name": name, "kind": "http",
                "command": ["curl", url], "url": url,
                "expected_status": 200}

    return [
        systemctl("stop-rust-console", rust_console_unit),
        systemctl("start-python-console", python_console_unit),
        {"name": "wait-python-console", "kind": "port",
         "command": ["wait_port_open", "127.0.0.1", python_console_port],
         "port": python_console_port},
        probe("python-console-health",
              "http://127.0.0.1:%s/health" % python_console_port),
        probe("gateway-ready-after-python",
              gateway_url.rstrip("/") + "/ready"),
        systemctl("stop-python-console", python_console_unit),
        systemctl("start-rust-console", rust_console_unit),
        {"name": "wait-rust-console", "kind": "port",
         "command": ["wait_port_open", "127.0.0.1", rust_console_port],
         "port": rust_console_port},
        probe("rust-console-health",
              "http://127.0.0.1:%s/health" % rust_console_port),
        probe("gateway-ready-after-rust",
              gateway_url.rstrip("/") + "/ready"),
    ]


def _run_console_drill_step(step: dict, repo_root: Path,
                            timeout: float) -> dict:
    if step["kind"] == "systemctl":
        result = _run(step["command"], repo_root)
        return {"name": step["name"], "command": step["command"],
                "exit_code": result["exit_code"],
                "status": "passed" if result["exit_code"] == 0 else "failed"}
    if step["kind"] == "port":
        passed = _wait_port(step["port"], timeout)
        return {"name": step["name"], "command": step["command"],
                "exit_code": 0 if passed else 1,
                "status": "passed" if passed else "failed"}
    status = _probe_http(step["url"], timeout)
    passed = status == step["expected_status"]
    return {"name": step["name"], "command": step["command"],
            "exit_code": 0 if passed else 1,
            "observed_status": status,
            "expected_status": step["expected_status"],
            "status": "passed" if passed else "failed"}


def console_rollback_drill(
        python_console_unit: str = DEFAULT_PYTHON_CONSOLE_UNIT,
        rust_console_unit: str = DEFAULT_RUST_CONSOLE_UNIT,
        python_console_port: str = DEFAULT_PYTHON_CONSOLE_PORT,
        rust_console_port: str = DEFAULT_RUST_CONSOLE_PORT,
        gateway_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
        execute: bool = False,
        env_file: str | None = None,
        repo_root: Path | None = None) -> dict:
    repo_root = repo_root or Path(__file__).resolve().parents[1]
    plan = console_rollback_drill_plan(
        python_console_unit, rust_console_unit, python_console_port,
        rust_console_port, gateway_url)
    document = {
        "status": "planned",
        "executed": False,
        "python_console_unit": python_console_unit,
        "rust_console_unit": rust_console_unit,
        "python_console_port": python_console_port,
        "rust_console_port": rust_console_port,
        "gateway_url": gateway_url,
        "env_file": env_file,
        "steps": plan,
        "restored": False,
    }
    if not execute:
        return document
    document["executed"] = True
    results = []
    python_failed = False
    original_env = None
    # Forward leg: stop Rust 8002, point the installed console.env at 8096, start
    # Python, then prove the console and the untouched 8003 gateway still
    # answer.  A failed Python leg restores both the env file and the Rust
    # console immediately; the release links are never moved.
    try:
        result = _run_console_drill_step(plan[0], repo_root, timeout)
        results.append(result)
        if result["status"] != "passed":
            python_failed = True
        else:
            if env_file and Path(env_file).is_file():
                original_env = _rewrite_env_port(
                    env_file, "USAGESRV_PORT", python_console_port)
            for step in plan[1:5]:
                result = _run_console_drill_step(step, repo_root, timeout)
                results.append(result)
                if result["status"] != "passed":
                    python_failed = True
                    break
        if original_env is not None:
            _restore_file(env_file, original_env)
            original_env = None
        if python_failed:
            for step in plan[5:]:
                if step["name"] == "stop-python-console" and                         not any(item["name"] == "start-python-console"
                                and item["status"] == "passed"
                                for item in results):
                    continue
                result = _run_console_drill_step(step, repo_root, timeout)
                results.append(result)
                if result["status"] != "passed":
                    document["status"] = "failed"
                    document["steps"] = results
                    document["restored"] = False
                    document["error"] = (
                        "python console rollback drill failed and the Rust "
                        "console did not come back")
                    return document
            document["status"] = "failed"
            document["steps"] = results
            document["restored"] = True
            document["error"] = (
                "python console rollback drill failed; the Rust console was "
                "restored")
            return document
        # Return leg: stop Python 8096 and start Rust 8002 again.
        for step in plan[5:]:
            result = _run_console_drill_step(step, repo_root, timeout)
            results.append(result)
            if result["status"] != "passed":
                document["status"] = "failed"
                document["steps"] = results
                document["restored"] = False
                document["error"] = (
                    "the Rust console did not come back; manual intervention "
                    "required")
                return document
        document["status"] = "passed"
        document["steps"] = results
        document["restored"] = True
        return document
    finally:
        if original_env is not None:
            _restore_file(env_file, original_env)


def _summary(document: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(document, ensure_ascii=False, indent=2))
        return
    parity = document.get("candidate_parity")
    console = document.get("console_parity")
    smoke = document.get("cloud_smoke")
    drill = document.get("rollback_drill")
    console_drill = document.get("console_rollback_drill")
    print("cutover gate: candidate-parity=%s console-parity=%s "
          "cloud-smoke=%s rollback-drill=%s console-rollback-drill=%s" % (
              parity["status"] if parity else "not-run",
              console["status"] if console else "not-run",
              smoke["status"] if smoke else "not-run",
              drill["status"] if drill else "not-run",
              console_drill["status"] if console_drill else "not-run"))
    if smoke:
        print("cloud smoke is a PLAN only; run its commands on the cloud host")
        for check in smoke["checks"]:
            print("  %s: %s" % (check["name"], check["command"]))
            print("    expected: %s" % check["expected"])
    if parity and parity["status"] != "passed":
        for item in parity["commands"]:
            print("  %s -> %s" % (" ".join(item["command"]),
                                  item["exit_code"]))
    if console:
        for surface in console.get("surfaces", []):
            print("  console parity %s: %s" % (surface["name"],
                                               surface["status"]))
            for difference in surface["differences"]:
                print("    %s: python=%s rust=%s" % (
                    difference["path"], difference["python_status"],
                    difference["rust_status"]))
        if console.get("error"):
            print("  console parity error: %s" % console["error"])
    if drill:
        if drill["executed"]:
            print("  rollback drill: %s" % drill["status"])
        else:
            print("  rollback drill plan: %s"
                  % " ".join(drill["command"]))
    if console_drill:
        if console_drill["executed"]:
            print("  console rollback drill: %s (restored=%s)" % (
                console_drill["status"], console_drill["restored"]))
        else:
            print("  console rollback drill plan:")
            for step in console_drill["steps"]:
                print("    %s: %s" % (step["name"],
                                     " ".join(step["command"])))


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

    console = subparsers.add_parser("console-parity")
    console.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    console.add_argument("--evidence", default=argparse.SUPPRESS)
    console.add_argument("--python-url", default=DEFAULT_CONSOLE_URL)
    console.add_argument("--rust-url", default=None)
    console.add_argument("--rust-console", default=None,
                         help="Rust candidate console binary (default: "
                              "rust/target/release/um-console-candidate)")
    console.add_argument("--rust-console-command", nargs="+", default=None,
                         help="explicit Rust candidate command (testing/WSL)")
    console.add_argument("--env-file", default=None,
                         help="KEY=VALUE data-directory environment shared "
                              "with the Python console; values are never "
                              "printed")
    console.add_argument("--timeout", type=float, default=10.0)
    console.add_argument("--surface", action="append", default=None,
                         choices=[name for name, _ in CONSOLE_SURFACES])

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

    console_drill = subparsers.add_parser("console-rollback-drill")
    console_drill.add_argument("--json", action="store_true",
                              default=argparse.SUPPRESS)
    console_drill.add_argument("--evidence", default=argparse.SUPPRESS)
    console_drill.add_argument("--python-console-unit",
                               default=DEFAULT_PYTHON_CONSOLE_UNIT)
    console_drill.add_argument("--rust-console-unit",
                               default=DEFAULT_RUST_CONSOLE_UNIT)
    console_drill.add_argument("--python-console-port",
                               default=DEFAULT_PYTHON_CONSOLE_PORT)
    console_drill.add_argument("--rust-console-port",
                               default=DEFAULT_RUST_CONSOLE_PORT)
    console_drill.add_argument("--gateway-url", default=DEFAULT_BASE_URL)
    console_drill.add_argument("--env-file", default=None,
                              help="installed console.env the Python unit "
                                   "reads; only USAGESRV_PORT is rewritten "
                                   "for the drill and then restored")
    console_drill.add_argument("--timeout", type=float, default=60.0)
    console_drill.add_argument("--execute", action="store_true")

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
    all_parser.add_argument("--python-url", default=DEFAULT_CONSOLE_URL)
    all_parser.add_argument("--rust-url", default=None)
    all_parser.add_argument("--rust-console", default=None)
    all_parser.add_argument("--rust-console-command", nargs="+",
                            default=None)
    all_parser.add_argument("--env-file", default=None)
    all_parser.add_argument("--console-timeout", type=float, default=10.0)
    all_parser.add_argument("--surface", action="append", default=None,
                            choices=[name for name, _ in CONSOLE_SURFACES])
    all_parser.add_argument("--python-console-unit",
                            default=DEFAULT_PYTHON_CONSOLE_UNIT)
    all_parser.add_argument("--rust-console-unit",
                            default=DEFAULT_RUST_CONSOLE_UNIT)
    all_parser.add_argument("--python-console-port",
                            default=DEFAULT_PYTHON_CONSOLE_PORT)
    all_parser.add_argument("--rust-console-port",
                            default=DEFAULT_RUST_CONSOLE_PORT)
    all_parser.add_argument("--gateway-url", default=DEFAULT_BASE_URL)
    all_parser.add_argument("--console-drill-timeout", type=float,
                            default=60.0)
    all_parser.add_argument("--execute-console-rollback",
                            action="store_true")
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

    if args.command == "console-parity":
        document = {
            "schema": SCHEMA,
            "generated_at": _now(),
            "console_parity": console_parity(
                args.python_url, args.rust_url, timeout=args.timeout,
                surfaces=tuple(args.surface) if args.surface else None,
                rust_console_command=args.rust_console_command,
                rust_console=args.rust_console,
                env_file=args.env_file, repo_root=repo_root),
        }
        _summary(document, args.json)
        _write_evidence(evidence, document)
        return 0 if document["console_parity"]["status"] == "passed" else 1

    if args.command == "console-rollback-drill":
        document = {
            "schema": SCHEMA,
            "generated_at": _now(),
            "console_rollback_drill": console_rollback_drill(
                args.python_console_unit, args.rust_console_unit,
                args.python_console_port, args.rust_console_port,
                args.gateway_url, timeout=args.timeout,
                execute=args.execute, env_file=args.env_file,
                repo_root=repo_root),
        }
        _summary(document, args.json)
        _write_evidence(evidence, document)
        return 0 if document["console_rollback_drill"]["status"] in (
            "planned", "passed") else 1

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

    # all: run local parity and console parity, then plan the operator-only
    # gates.  Console parity is executed because it is the 8002 cutover evidence;
    # a candidate that cannot start or answer is a failure, not a plan.
    data_dirs = args.data_dir or DATA_DIRS
    document = {
        "schema": SCHEMA,
        "generated_at": _now(),
        "candidate_parity": candidate_parity(
            repo_root, python, cargo, args.skip_rust),
        "console_parity": console_parity(
            args.python_url, args.rust_url, timeout=args.console_timeout,
            surfaces=tuple(args.surface) if args.surface else None,
            rust_console_command=args.rust_console_command,
            rust_console=args.rust_console,
            env_file=args.env_file, repo_root=repo_root),
        "cloud_smoke": cloud_smoke_plan(
            args.base_url, args.share_key_env, args.model,
            args.public_url),
        "rollback_drill": rollback_drill(
            repo_root, args.to, args.health_port,
            args.current_health_port, data_dirs, args.execute_rollback),
        "console_rollback_drill": console_rollback_drill(
            args.python_console_unit, args.rust_console_unit,
            args.python_console_port, args.rust_console_port,
            args.gateway_url, timeout=args.console_drill_timeout,
            execute=args.execute_console_rollback, env_file=args.env_file,
            repo_root=repo_root),
    }
    _summary(document, args.json)
    _write_evidence(evidence, document)
    # `all` fails when ANY executed gate failed: a planned smoke/drill is not a
    # pass, but it is also not a failure - it stays visibly unexecuted.
    failed = document["candidate_parity"]["status"] != "passed"
    failed = failed or document["console_parity"]["status"] != "passed"
    failed = failed or document["cloud_smoke"]["status"] == "failed"
    failed = failed or document["rollback_drill"]["status"] == "failed"
    failed = failed or document["console_rollback_drill"]["status"] == "failed"
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
