#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#85 本机 8001 parity 探针。

Rust candidate 以 local 角色绑定临时 loopback 端口，Python 8001 保持生产页面；
逐面比较状态码与归一化后的 JSON。比较口径写在 docs/rust-contract.md：
不透明游标/行键、实现诊断元数据与已知能力差异单独记录，不折成假通过。
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import time
from pathlib import Path

VOLATILE_KEYS = frozenset({
    "checked_epoch",
    "checkedAt",
    "generatedAt",
    "generated_epoch",
    "_generated_epoch",
    "_cached_age_seconds",
    "cache_age_seconds",
    "cached",
    "collected_at",
    "created_at",
    "started_at",
    "finished_at",
})

OPAQUE_KEYS = frozenset({
    "key",
    "file_key",
    "next_cursor",
    "next_cursors",
    "earlier",
    "reader",
})

DIAGNOSTIC_KEYS = frozenset({
    "scanned_bytes",
    "window_start_offset",
    "rows_read",
    "rows_dropped",
    "rows_dropped_by_budget",
    "ms",
    "ts_norm",
    "at",
})


def request(addr, method, path, body=None, headers=None, timeout=180):
    host, port = addr
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    request_headers = {"Host": f"{host}:{port}"}
    if headers:
        request_headers.update(headers)
    connection.request(method, path, body=body, headers=request_headers)
    response = connection.getresponse()
    payload = response.read()
    result = {
        "status": response.status,
        "content_type": response.getheader("Content-Type"),
        "body": payload,
    }
    connection.close()
    return result


def normalized(value):
    if isinstance(value, dict):
        result = {
            key: normalized(item)
            for key, item in value.items()
            if key not in VOLATILE_KEYS
            and key not in OPAQUE_KEYS
            and key not in DIAGNOSTIC_KEYS
        }
        event = result.get("event")
        if isinstance(event, dict) and all(item is None for item in event.values()):
            # Python 对无法解析的行保留 event=None；Rust 保留同形的
            # 全 null 事件对象。两者都表示「没有结构化事件」。
            result["event"] = None
        return result
    if isinstance(value, list):
        return [normalized(item) for item in value]
    return value


def json_body(result):
    if not result["body"]:
        return None
    try:
        return json.loads(result["body"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def compare_json(name, python_result, rust_result, projection=None):
    project = projection or normalized
    python_body = json_body(python_result)
    rust_body = json_body(rust_result)
    python_norm = project(python_body)
    rust_norm = project(rust_body)
    return {
        "surface": name,
        "status": "pass"
        if python_result["status"] == rust_result["status"]
        and python_norm == rust_norm
        else "fail",
        "pythonStatus": python_result["status"],
        "rustStatus": rust_result["status"],
        "python": python_norm,
        "rust": rust_norm,
    }


def compare_bytes(name, python_result, rust_result):
    return {
        "surface": name,
        "status": "pass"
        if python_result["status"] == rust_result["status"]
        and python_result["body"] == rust_result["body"]
        else "fail",
        "pythonStatus": python_result["status"],
        "rustStatus": rust_result["status"],
        "pythonSha256": sha256(python_result["body"]),
        "rustSha256": sha256(rust_result["body"]),
        "pythonBytes": len(python_result["body"]),
        "rustBytes": len(rust_result["body"]),
    }


def summary_projection(body):
    if not isinstance(body, dict):
        return body
    return {
        "sources": body.get("sources"),
        "services": body.get("services"),
        "dsh": body.get("dsh"),
    }


def services_projection(body):
    if not isinstance(body, dict):
        return body
    services = []
    for row in body.get("services") or []:
        services.append({
            key: row.get(key)
            for key in ("key", "display_name", "port", "state", "healthy")
        })
    return {"ok": body.get("ok"), "services": services}


def operations_projection(body):
    if not isinstance(body, dict):
        return body
    operations = []
    for row in body.get("operations") or []:
        operations.append({
            key: row.get(key)
            for key in (
                "action", "target", "state", "resource", "generation",
                "cancel_requested", "error",
            )
        })
    return {"operations": operations}


def health_projection(body):
    if not isinstance(body, dict):
        return body
    services = []
    for row in body.get("services") or []:
        services.append({
            key: row.get(key)
            for key in ("id", "port", "state", "http_status", "note")
        })
    return {"services": services}


def gateway_connection_projection(body):
    if not isinstance(body, dict):
        return body
    return {
        key: body.get(key)
        for key in (
            "ok",
            "service",
            "display_name",
            "port",
            "health_path",
            "health_path_source",
            "base_url",
            "protocol",
            "protocol_path",
            "runtime_state",
            "runtime_healthy",
            "auth",
            "key_state",
            "key_plaintext",
            "key_source",
            "key_source_state",
        )
    }


def poll_operation(addr, operation_id, timeout=180):
    deadline = time.time() + timeout
    result = None
    operation = {}
    while time.time() < deadline:
        result = request(addr, "GET", f"/api/console/operations/{operation_id}")
        operation = (json_body(result) or {}).get("operation") or {}
        if operation.get("state") not in ("queued", "running", None):
            return result, operation
        time.sleep(1)
    return result, operation


def submit_catalog(addr):
    body = json.dumps(
        {"action": "model.catalog", "target": "all", "params": {}},
        ensure_ascii=False,
    ).encode("utf-8")
    submitted = request(
        addr, "POST", "/api/console/operations", body=body,
        headers={
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    operation = (json_body(submitted) or {}).get("operation") or {}
    return submitted, operation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python-port", type=int, default=8001)
    parser.add_argument("--rust-port", type=int, required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    python_addr = ("127.0.0.1", args.python_port)
    rust_addr = ("127.0.0.1", args.rust_port)
    checks = []
    known_differences = []

    identity = json.loads(
        (root / "web" / "dist" / "candidate-build.json").read_text(encoding="utf-8")
    )
    asset = identity["entryAssets"][0]

    for name, path in (
        ("static.index", "/"),
        ("static.asset", "/" + asset),
    ):
        checks.append(compare_bytes(
            name,
            request(python_addr, "GET", path),
            request(rust_addr, "GET", path),
        ))

    reads = (
        ("summary", "/api/summary", summary_projection),
        ("usage-tokens", "/api/console/usage-tokens?days=3", None),
        ("usage-rates", "/api/console/usage-rates?hours=24", None),
        ("stats", "/api/console/stats?from=2026-11-10&to=2026-11-17", None),
        ("log-sources", "/api/console/log-sources", None),
        ("log-entries", "/api/console/log-entries", None),
        ("log-overview", "/api/console/log-overview?limit=2", None),
        ("services", "/api/console/services", services_projection),
        ("health", "/api/console/health", health_projection),
        ("um-today", "/api/console/um/today", None),
        ("um-trend", "/api/console/um/trend", None),
        ("um-requests", "/api/console/um/requests", None),
        ("um-maintenance", "/api/console/um/maintenance", None),
        ("gateway-connection", "/api/console/gateway-connection?service=chat2api",
         gateway_connection_projection),
        ("operations.list", "/api/console/operations", operations_projection),
    )
    for name, path, projection in reads:
        checks.append(compare_json(
            name,
            request(python_addr, "GET", path),
            request(rust_addr, "GET", path),
            projection,
        ))

    session_body = json.dumps({"code": "invalid"}).encode("utf-8")
    checks.append(compare_json(
        "session.invalid",
        request(python_addr, "POST", "/api/session", body=session_body,
                headers={"Content-Type": "application/json"}),
        request(rust_addr, "POST", "/api/session", body=session_body,
                headers={"Content-Type": "application/json"}),
    ))

    python_submitted, python_operation = submit_catalog(python_addr)
    rust_submitted, rust_operation = submit_catalog(rust_addr)
    python_detail, python_final = poll_operation(
        python_addr, python_operation.get("id", "")
    )
    rust_detail, rust_final = poll_operation(
        rust_addr, rust_operation.get("id", "")
    )
    checks.append({
        "surface": "model.catalog",
        "status": "pass"
        if python_submitted["status"] in (200, 201)
        and rust_submitted["status"] in (200, 201)
        and python_final.get("state") == "succeeded"
        and rust_final.get("state") == "succeeded"
        and normalized(python_final.get("result"))
        == normalized(rust_final.get("result"))
        else "fail",
        "pythonState": python_final.get("state"),
        "rustState": rust_final.get("state"),
        "python": normalized(python_final.get("result")),
        "rust": normalized(rust_final.get("result")),
    })

    # Known capability difference outside the read-face parity scope: Python's
    # full local action registry lists 12 enabled actions; the Rust local role
    # lists the UM catalogue with only model.catalog enabled (#84).
    python_actions = json_body(request(python_addr, "GET", "/api/console/actions")) or {}
    rust_actions = json_body(request(rust_addr, "GET", "/api/console/actions")) or {}
    python_names = {row.get("action") for row in python_actions.get("actions", [])}
    rust_names = {row.get("action") for row in rust_actions.get("actions", [])}
    known_differences.append({
        "surface": "actions.catalog",
        "reason": "Rust 本机角色只启用 model.catalog；其余动作在后续票接线",
        "pythonOnly": sorted(python_names - rust_names),
        "rustOnly": sorted(rust_names - python_names),
        "shared": sorted(python_names & rust_names),
    })

    # Known diagnostic difference: the local runtime facts expose the chat2api
    # PID in Python; the Rust LocalTcpFacts reports state without a PID.
    known_differences.append({
        "surface": "services.process_ids",
        "reason": "Python gateway-runtime 读进程表；Rust LocalTcpFacts 只做 TCP 探测，不伪造 PID",
    })

    # Known diagnostic difference: Python reads the whole log tail's window
    # offset and total file size; the Rust um/logs face reports the file size
    # and treats naive timestamps as UTC. The six #86 faces are compared
    # above; this legacy usage face is recorded separately.
    known_differences.append({
        "surface": "um.logs",
        "reason": "时间戳解释（Python 按北京时间、Rust 按 UTC）与不透明行键/游标编码不同；"
                  "不属于 #86 六个只读面，未纳入切流 parity 门槛",
    })

    known_differences.append({
        "surface": "console.logs",
        "reason": "Rust 本机角色未装配旧 usage 账本 /api/console/logs 读面；本机页面使用 #86 的 log-* 面",
    })

    failed = [check for check in checks if check["status"] != "pass"]
    evidence = {
        "ticket": 85,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "pythonPort": args.python_port,
        "rustPort": args.rust_port,
        "parity": "passed" if not failed else "failed",
        "checks": checks,
        "knownDifferences": known_differences,
    }
    Path(args.evidence).write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    for check in checks:
        print(f"{check['status'].upper():5} {check['surface']}")
    for difference in known_differences:
        print(f"KNOWN {difference['surface']}: {difference['reason']}")
    print(f"parity={evidence['parity']}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
