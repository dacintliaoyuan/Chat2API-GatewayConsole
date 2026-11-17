#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机 8000 的 Rust/Python parity 探针（#81）。

切流前用同一份 token/profile 启动 Rust candidate（临时 loopback 端口），再对
Python 8000 与 candidate 逐面比较状态码、响应体形状和稳定语义字段：

* ``/health`` 与 ``/v1/models`` 是公开面；
* 通用 ``/api/*`` GET/POST 与 ``/admin/auth/status|verify`` 带本地控制令牌；
* 控制令牌只发给 8000 的本机控制面，绝不写进证据文件或日志。

本脚本只读；``/admin/shutdown`` 不在探针列表内，由切流/回滚步骤单独验证。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Surface:
    name: str
    method: str
    path: str
    control: bool = False
    body: bytes | None = None


SURFACES = (
    Surface("health", "GET", "/health"),
    Surface("models", "GET", "/v1/models"),
    Surface("api_config", "GET", "/api/config", control=True),
    Surface("api_auths", "GET", "/api/v1/auths/", control=True),
    Surface("api_echo_post", "POST", "/api/echo", control=True,
            body=b'{"ping":true}'),
    Surface("admin_auth_status", "GET", "/admin/auth/status", control=True),
    Surface("admin_auth_verify", "GET", "/admin/auth/verify", control=True),
)

# These keys are process-local or clock-dependent. They stay in the shape
# comparison only when both sides can be expected to have them; the local 8000
# candidate starts without the Python auth-watch thread, so its watch/gate and
# login-window fields are not parity evidence.
ADMIN_STATUS_VOLATILE_KEYS = frozenset({
    "watch",
    "gate",
    "login_window_deadline_epoch",
    "login_window_s",
})
HEALTH_VOLATILE_KEYS = frozenset({
    "chat2api_build",
    "models",
    "watch",
    "recovery",
    "login_window_deadline_epoch",
    "login_window_s",
    "upstream",
    "upstream_reachability",
})


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _content_type(headers) -> str:
    if headers is None:
        return ""
    value = headers.get("Content-Type") or ""
    return value.split(";", 1)[0].strip().lower()


def shape_of(value):
    """Return a value-free JSON shape: keys and scalar types only."""
    if isinstance(value, dict):
        return {key: shape_of(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        shapes = []
        for item in value:
            item_shape = shape_of(item)
            if item_shape not in shapes:
                shapes.append(item_shape)
        return {"array": shapes}
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    return type(value).__name__


def _shape_body(surface: Surface, body):
    if not isinstance(body, dict):
        return body
    if surface.name == "health":
        return {
            key: value
            for key, value in body.items()
            if key not in HEALTH_VOLATILE_KEYS
        }
    if surface.name == "admin_auth_status":
        return {
            key: value
            for key, value in body.items()
            if key not in ADMIN_STATUS_VOLATILE_KEYS
        }
    return body


def _error_projection(body):
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, dict):
        detail = error.get("detail")
        return {
            "type": error.get("type"),
            "code": error.get("code"),
            "param": error.get("param"),
            "upstream_status": (
                detail.get("upstream_status")
                if isinstance(detail, dict) else None
            ),
        }
    if isinstance(error, str):
        return {"error": error}
    return None


def _model_ids(body):
    data = body.get("data")
    if not isinstance(data, list):
        return None
    ids = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            ids.append(item["id"])
    return sorted(ids)


def semantic_of(surface: Surface, probe: dict):
    body = probe.get("body")
    status = probe["status"]
    if status is None:
        return {"status": None}
    if surface.name == "health":
        if not isinstance(body, dict):
            return {"bodyShape": probe["shape"]}
        authentication = body.get("authentication")
        authentication = authentication if isinstance(authentication, dict) else {}
        return {
            "statusField": body.get("status"),
            "authState": authentication.get("state"),
            "authLabel": authentication.get("label"),
            "authError": authentication.get("error"),
            "docs": body.get("docs"),
        }
    if surface.name == "models":
        if isinstance(body, dict) and "data" in body:
            return {"modelIds": _model_ids(body)}
        error = _error_projection(body)
        if error is not None:
            return {"error": error}
        return {"bodyShape": probe["shape"]}
    if surface.name.startswith("api_"):
        error = _error_projection(body)
        if error is not None:
            return {"error": error}
        if isinstance(body, dict):
            return {
                "keys": sorted(body.keys()),
                "statusField": body.get("status"),
            }
        return {"bodyShape": probe["shape"]}
    if surface.name == "admin_auth_status":
        if not isinstance(body, dict):
            return {"bodyShape": probe["shape"]}
        evidence = body.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        recovery = body.get("recovery")
        recovery = recovery if isinstance(recovery, dict) else {}
        return {
            "keys": sorted(body.keys()),
            "state": body.get("state"),
            "label": body.get("label"),
            "error": body.get("error"),
            "primary": body.get("primary"),
            "verdict": evidence.get("verdict"),
            "reason": evidence.get("reason"),
            "upstream_status": evidence.get("upstream_status"),
            "loginIsNull": body.get("login") is None,
            "recoveryState": recovery.get("state"),
        }
    if surface.name == "admin_auth_verify":
        if not isinstance(body, dict):
            return {"bodyShape": probe["shape"]}
        return {
            "keys": sorted(body.keys()),
            "verify": body.get("verify"),
            "reason": body.get("reason"),
            "upstream_status": body.get("upstream_status"),
        }
    return {"bodyShape": probe["shape"]}


def probe_surface(base_url: str, surface: Surface, control_token: str,
                  timeout: float = 10.0) -> dict:
    """Probe one surface. The control token is attached only when declared."""
    url = base_url.rstrip("/") + surface.path
    headers = {"Accept": "application/json"}
    if surface.control:
        headers["X-Admin-Control"] = control_token
    if surface.body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=surface.body,
        headers=headers,
        method=surface.method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
            content_type = _content_type(response.headers)
    except urllib.error.HTTPError as error:
        try:
            raw = error.read()
            status = error.code
            content_type = _content_type(error.headers)
        finally:
            error.close()
    except (urllib.error.URLError, OSError, ValueError,
            http.client.HTTPException) as error:
        return {
            "name": surface.name,
            "method": surface.method,
            "path": surface.path,
            "status": None,
            "contentType": "",
            "shape": None,
            "semantic": {"status": None},
            "body": None,
            "error": "%s: %s" % (type(error).__name__, error),
        }

    body = None
    shape = "text"
    if raw and (content_type == "application/json"
                or raw.lstrip()[:1] in (b"{", b"[")):
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            shape = "invalid-json"
        else:
            shape = shape_of(_shape_body(surface, body))
    elif not raw:
        shape = "empty"

    probe = {
        "name": surface.name,
        "method": surface.method,
        "path": surface.path,
        "status": status,
        "contentType": content_type,
        "shape": shape,
        "body": body,
        "error": None,
    }
    probe["semantic"] = semantic_of(surface, probe)
    return probe


def compare_probes(python: dict, rust: dict) -> list[dict]:
    differences = []
    for field in ("status", "contentType", "shape", "semantic"):
        if python[field] != rust[field]:
            differences.append({
                "surface": python["name"],
                "field": field,
                "python": python[field],
                "rust": rust[field],
            })
    return differences


def _sanitize_probe(probe: dict) -> dict:
    return {
        "name": probe["name"],
        "method": probe["method"],
        "path": probe["path"],
        "status": probe["status"],
        "contentType": probe["contentType"],
        "shape": probe["shape"],
        "semantic": probe["semantic"],
        "error": probe["error"],
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_evidence(python_url: str, rust_url: str, control_token: str,
                   timeout: float = 10.0) -> dict:
    python = [
        probe_surface(python_url, surface, control_token, timeout)
        for surface in SURFACES
    ]
    rust = [
        probe_surface(rust_url, surface, control_token, timeout)
        for surface in SURFACES
    ]
    differences = []
    for python_probe, rust_probe in zip(python, rust):
        differences.extend(compare_probes(python_probe, rust_probe))
    probe_errors = []
    for side, probes in (("python", python), ("rust", rust)):
        for probe in probes:
            if probe["error"] is not None:
                probe_errors.append({
                    "side": side,
                    "surface": probe["name"],
                    "error": probe["error"],
                })
    passed = not differences and not probe_errors
    return {
        "ticket": 81,
        "generatedAt": _now_iso(),
        "python": {
            "baseUrl": python_url,
            "surfaces": [_sanitize_probe(probe) for probe in python],
        },
        "rust": {
            "baseUrl": rust_url,
            "surfaces": [_sanitize_probe(probe) for probe in rust],
        },
        "differences": differences,
        "probeErrors": probe_errors,
        "parity": "passed" if passed else "failed",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare local 8000 Python and Rust candidate surfaces.")
    parser.add_argument("--python-url", required=True)
    parser.add_argument("--rust-url", required=True)
    parser.add_argument("--control-token-file", required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--evidence", default="")
    args = parser.parse_args(argv)

    try:
        control_token = Path(args.control_token_file).read_text(
            encoding="utf-8").strip()
    except OSError as error:
        print("local_8000_parity: cannot read control token file: %s"
              % error, file=sys.stderr)
        return 2
    if not control_token:
        print("local_8000_parity: control token file is empty", file=sys.stderr)
        return 2

    document = build_evidence(
        args.python_url, args.rust_url, control_token, args.timeout)
    text = json.dumps(document, ensure_ascii=False, indent=2)
    print(text)
    if args.evidence:
        path = Path(args.evidence)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text + "\n", encoding="utf-8")
        os.replace(temporary, path)

    if document["probeErrors"]:
        for error in document["probeErrors"]:
            print("local_8000_parity: %s %s: %s" % (
                error["side"], error["surface"], error["error"]),
                file=sys.stderr)
        return 2
    if document["differences"]:
        for difference in document["differences"]:
            print("local_8000_parity: %s %s differs" % (
                difference["surface"], difference["field"]),
                file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
