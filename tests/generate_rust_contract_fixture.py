#!/usr/bin/env python3
"""Generate the Rust contract fixture from the frozen contract and Python code.

The Rust tests never hand-write expected endpoints, error codes, SSE event names or
timeout budgets. They deserialize ``tests/fixtures/rust_contract_rev1.json``, which
this generator produces from:

* ``docs/rust-contract.md`` — the frozen external contract; endpoint tables, error
  tables, SSE event names and the target port table are parsed from it.
* the Python modules that still own the behavior — constants are imported and
  cross-checked against the fixture so a silent Python change fails generation.

The generator performs no live request, starts no process, reads no credential, token
or user ledger, and writes only the fixture file. Run from the repository root:

    python tests/generate_rust_contract_fixture.py
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_RELATIVE = "docs/rust-contract.md"
CONTRACT = ROOT / CONTRACT_RELATIVE
DESTINATION = ROOT / "tests" / "fixtures" / "rust_contract_rev1.json"

sys.path[:0] = [str(ROOT)]


def _load_module(name: str, relative: str):
    """Load a Python source file under a private module name.

    Loading by package name is not safe here: other test modules put
    ``chat2api/`` on ``sys.path`` and register the bare ``chat2api`` name for
    ``chat2api/chat2api.py``. A private name keeps this generator independent
    of test execution order and does not import a second copy of the behavior.
    """
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load %s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gateway = _load_module("rust_contract_chat2api", "chat2api/chat2api.py")
share_auth = _load_module("rust_contract_share_auth", "chat2api/share_auth.py")
display_proxy = _load_module("rust_contract_display_proxy", "console/display_proxy.py")
broker_protocol = _load_module("rust_contract_broker_protocol", "share/broker_protocol.py")
share_metering = _load_module("rust_contract_share_metering", "share/metering.py")
share_store = _load_module("rust_contract_share_store", "share/store.py")

_PORT_SERVICES = {
    8000: "chat2api",
    8001: "local_console",
    8002: "cloud_console",
    8003: "cloud_gateway",
    8097: "display",
    8099: "candidate_preview",
    5173: "vite_dev",
}


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8").replace("\r\n", "\n")


def _section(text: str, heading: str) -> str:
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == heading:
            start = index + 1
            break
    if start is None:
        raise SystemExit("contract heading missing: %s" % heading)
    body = []
    for line in lines[start:]:
        if line.startswith("#"):
            break
        body.append(line)
    return "\n".join(body)


def _table(section: str) -> tuple[list[str], list[list[str]]]:
    rows = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= {"-", ":", " "} for cell in cells):
            continue
        rows.append(cells)
    if not rows:
        raise SystemExit("contract table missing")
    return rows[0], rows[1:]


def _code_spans(cell: str) -> list[str]:
    return [value.strip() for value in re.findall(r"`([^`]+)`", cell)]


_ABSOLUTE_PREFIXES = (
    "/api",
    "/v1",
    "/display",
    "/admin",
    "/health",
    "/ready",
    "/index.html",
    "/gw",
)


def _paths(cell: str) -> list[str]:
    spans = _code_spans(cell)
    if not spans:
        if "任意路径" in cell:
            return ["*"]
        raise SystemExit("endpoint row has no path code span: %s" % cell)
    first = spans[0]
    if not first.startswith("/"):
        raise SystemExit("endpoint row must start with an absolute path: %s" % cell)
    base = first.rsplit("/", 1)[0]
    paths = []
    for raw in spans:
        if raw == "*":
            path = raw
        elif raw.startswith(_ABSOLUTE_PREFIXES):
            path = raw
        elif base in ("", "/"):
            path = "/" + raw.lstrip("/")
        else:
            path = base.rstrip("/") + "/" + raw.lstrip("/")
        paths.append(path)
    return paths


def _endpoints(section: str, surface: str, source: str) -> list[dict]:
    _header, rows = _table(section)
    endpoints = []
    seen = set()
    for row in rows:
        if len(row) < 3:
            continue
        method_cell, path_cell, auth_cell = row[0], row[1], row[2]
        description = row[3] if len(row) > 3 else ""
        methods = [part.strip() for part in method_cell.split("/") if part.strip()]
        for path in _paths(path_cell):
            for method in methods:
                key = (surface, method, path)
                if key in seen:
                    continue
                seen.add(key)
                endpoints.append({
                    "surface": surface,
                    "method": method,
                    "path": path,
                    "auth": auth_cell,
                    "description": description,
                    "source": source,
                })
    return endpoints


def _ports(section: str) -> dict:
    _header, rows = _table(section)
    targets = {}
    for row in rows:
        if len(row) < 5:
            continue
        port_match = re.search(r"\d+", row[0])
        target_match = re.search(r"\d+", row[4])
        if not port_match or not target_match:
            continue
        port = int(port_match.group(0))
        target = int(target_match.group(0))
        if port not in _PORT_SERVICES:
            raise SystemExit("contract port has no stable service name: %s" % port)
        targets[_PORT_SERVICES[port]] = target
    if sorted(targets) != sorted(_PORT_SERVICES.values()):
        raise SystemExit("contract port table is missing a governed service")
    production = sorted(set(targets.values()))
    return {
        "target": targets,
        "production": production,
        "legacyAliases": [],
        "source": CONTRACT_RELATIVE + "#2.1",
    }


def _load_ingress_checker():
    path = ROOT / "um-cloud-deploy" / "cloudflared" / "check-ingress.py"
    spec = importlib.util.spec_from_file_location(
        "rust_contract_ingress_checker", path)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load %s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cloudflare() -> dict:
    checker = _load_ingress_checker()
    paths = list(checker.API_PATHS)
    if not paths:
        raise SystemExit("Cloudflare API_PATHS is empty")
    return {
        "sharePaths": paths,
        "catchAll": checker.CATCH_ALL,
        "readOnly": True,
        "source": "um-cloud-deploy/cloudflared/check-ingress.py:26-30",
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def _doc_number(section: str, pattern: str, label: str) -> float:
    match = re.search(pattern, section)
    if match is None:
        raise SystemExit("contract value missing: %s" % label)
    return float(match.group(1))


def _error_codes(cell: str) -> list[str]:
    return [
        value for value in _code_spans(cell)
        if re.fullmatch(r"[a-z][a-z0-9_*]*", value)
    ]


def _errors(contract: str) -> list[dict]:
    groups = []
    for surface, heading, source in (
            ("gateway", "### 5.4 错误码", CONTRACT_RELATIVE + "#5.4"),
            ("console", "### 6.3 请求/响应与错误码", CONTRACT_RELATIVE + "#6.3")):
        _header, rows = _table(_section(contract, heading))
        for row in rows:
            if len(row) < 2:
                continue
            status = int(row[0]) if row[0].isdigit() else None
            codes = _error_codes(row[1])
            groups.append({
                "surface": surface,
                "status": status,
                "codes": codes,
                "messages": _code_spans(row[1]),
                "description": row[2] if len(row) > 2 else "",
                "source": source,
            })
    groups.append({
        "surface": "broker",
        "status": None,
        "codes": sorted(broker_protocol.RESULT_CODES),
        "messages": [],
        "description": "固定结果码表",
        "source": "share/broker_protocol.py:161-176",
    })
    return groups


def _sse(contract: str) -> dict:
    streaming = _section(contract, "### 5.3 流式、超时、取消与重试")
    responses_line = next(
        (line for line in streaming.splitlines() if "Responses 流事件为" in line), "")
    responses_events = [
        event for event in _code_spans(responses_line)
        if event.startswith("response.")
    ]
    _require(bool(responses_events), "contract Responses SSE events missing")
    _require(all(event.startswith("response.") for event in responses_events),
             "contract Responses SSE event has an unexpected name")
    messages_line = next(
        (line for line in streaming.splitlines() if "Messages 流要求官方事件序列" in line), "")
    message_events = _code_spans(messages_line)
    _require("message_stop" in message_events, "contract Messages terminal event missing")
    _require("error" in message_events, "contract Messages error event missing")
    responses_source = _text("shared/responses_protocol.py")
    for event in responses_events:
        _require('"%s"' % event in responses_source,
                 "Responses SSE event is not emitted by shared/responses_protocol.py: %s"
                 % event)
    return {
        "gateway": {
            "chat": {
                "contentType": "text/event-stream",
                "passthrough": True,
                "source": CONTRACT_RELATIVE + "#5.3",
            },
            "messages": {
                "contentType": "text/event-stream",
                "terminalEvent": "message_stop",
                "errorEvent": "error",
                "eofBeforeTerminalIsSuccess": False,
                "source": CONTRACT_RELATIVE + "#5.3",
            },
            "responses": {
                "contentType": "text/event-stream",
                "events": responses_events,
                "source": CONTRACT_RELATIVE + "#5.3",
            },
        },
    }


def _boundaries(contract: str) -> dict:
    streaming = _section(contract, "### 5.3 流式、超时、取消与重试")
    first_byte = _doc_number(streaming, r"首字节预算 (\d+) 秒", "first byte")
    socket_silence = _doc_number(
        streaming, r"上游单次 socket 静默预算 (\d+) 秒", "socket silence")
    request_total = _doc_number(streaming, r"请求总预算默认 (\d+) 秒", "request total")
    prelude = _doc_number(streaming, r"预提交暂存上限 (\d+) KiB", "prelude") * 1024
    line_buffer = _doc_number(
        streaming, r"未成行字节缓冲上限 (\d+) MiB", "line buffer") * 1024 * 1024
    queue_max = _doc_number(streaming, r"生产者/消费者队列上限 (\d+)", "queue")
    readers = _doc_number(streaming, r"并发流 reader 上限 (\d+)", "readers")
    retry_match = re.search(
        r"快速失败最多 (\d+) 次，慢失败（耗时达到 (\d+) 秒）最多 (\d+) 次，"
        r"物理尝试上限 (\d+)", streaming)
    _require(retry_match is not None, "contract retry budget missing")
    retryable_match = re.search(r"可重试状态为 ([\d/]+)", streaming)
    _require(retryable_match is not None, "contract retryable statuses missing")

    _require(gateway.STREAM_FIRST_BYTE_TIMEOUT_S == first_byte,
             "Python first-byte budget drifted from docs/rust-contract.md#5.3")
    _require(gateway.STREAM_SOCKET_TIMEOUT_S == socket_silence,
             "Python socket silence budget drifted from docs/rust-contract.md#5.3")
    _require(gateway.MAX_REQUEST_SECONDS == request_total,
             "Python request budget drifted from docs/rust-contract.md#5.3")
    _require(gateway._PRELUDE_MAX_BYTES == prelude,
             "Python prelude bound drifted from docs/rust-contract.md#5.3")
    _require(gateway._PRODUCER_LINE_BUFFER_MAX_BYTES == line_buffer,
             "Python line buffer drifted from docs/rust-contract.md#5.3")
    _require(gateway._UPSTREAM_QUEUE_MAX == queue_max,
             "Python queue bound drifted from docs/rust-contract.md#5.3")
    _require(gateway.STREAM_MAX_CONCURRENT_READERS == readers,
             "Python reader bound drifted from docs/rust-contract.md#5.3")
    fast, slow_threshold, slow, physical = (int(value) for value in retry_match.groups())
    _require(gateway.UPSTREAM_AUTO_RETRIES == fast,
             "Python fast retry count drifted from docs/rust-contract.md#5.3")
    _require(gateway.UPSTREAM_SLOW_RETRY_THRESHOLD_S == slow_threshold,
             "Python slow retry threshold drifted from docs/rust-contract.md#5.3")
    _require(gateway.UPSTREAM_SLOW_AUTO_RETRIES == slow,
             "Python slow retry count drifted from docs/rust-contract.md#5.3")
    _require(gateway.MAX_TOTAL_ATTEMPTS == physical,
             "Python physical attempt bound drifted from docs/rust-contract.md#5.3")
    retryable = sorted(int(value) for value in retryable_match.group(1).split("/"))
    _require(sorted(gateway._UPSTREAM_RETRYABLE_STATUS) == retryable,
             "Python retryable statuses drifted from docs/rust-contract.md#5.3")

    console = _section(contract, "### 6.4 超时、取消与幂等")
    display_match = re.search(
        r"display grant TTL (\d+) 秒；display session TTL (\d+) 秒；"
        r"最多 (\d+) 个待用 grant、(\d+) 个会话；最多 (\d+) 个并发 relay；"
        r"idle (\d+) 秒；单会话最长 (\d+) 秒；握手 (\d+) 秒；"
        r"关闭宽限 (\d+) 秒", console)
    _require(display_match is not None, "contract display budgets missing")
    (grant_ttl, session_ttl, pending, sessions, relay, idle, max_session,
     handshake, close_grace) = (int(value) for value in display_match.groups())
    display_defaults = display_proxy.DisplayConfig()
    _require(display_proxy.GRANT_TTL == grant_ttl,
             "Python display grant TTL drifted from docs/rust-contract.md#6.4")
    _require(display_proxy.DISPLAY_SESSION_TTL == session_ttl,
             "Python display session TTL drifted from docs/rust-contract.md#6.4")
    _require(display_proxy.MAX_PENDING_GRANTS == pending,
             "Python display grant capacity drifted from docs/rust-contract.md#6.4")
    _require(display_proxy.MAX_DISPLAY_SESSIONS == sessions,
             "Python display session capacity drifted from docs/rust-contract.md#6.4")
    _require(display_defaults.max_connections == relay,
             "Python display relay capacity drifted from docs/rust-contract.md#6.4")
    _require(display_defaults.idle_timeout == idle,
             "Python display idle budget drifted from docs/rust-contract.md#6.4")
    _require(display_defaults.max_session_seconds == max_session,
             "Python display session budget drifted from docs/rust-contract.md#6.4")
    _require(display_defaults.handshake_timeout == handshake,
             "Python display handshake budget drifted from docs/rust-contract.md#6.4")
    _require(display_defaults.relay_join_timeout == close_grace,
             "Python display close grace drifted from docs/rust-contract.md#6.4")

    broker = _section(contract, "### 7.3 生命周期、超时与副作用")
    broker_match = re.search(
        r"默认 wire deadline (\d+) 秒；管理 dispatch deadline (\d+) 秒；"
        r"排空窗口 (\d+) 秒；最终停止预算 (\d+) 秒；"
        r"客户端排空 deadline (\d+) 秒；紧急停止 deadline (\d+) 秒", broker)
    _require(broker_match is not None, "contract broker budgets missing")
    (wire, dispatch, drain_window, stop_budget, drain_deadline,
     emergency_deadline) = (int(value) for value in broker_match.groups())
    _require(broker_protocol.DEFAULT_DEADLINE_SECONDS == wire,
             "Python broker wire deadline drifted from docs/rust-contract.md#7.3")
    _require(dispatch == 130, "contract broker dispatch deadline changed")
    _require(broker_protocol.DRAIN_WINDOW_SECONDS == drain_window,
             "Python broker drain window drifted from docs/rust-contract.md#7.3")
    _require(broker_protocol.STOP_BUDGET_SECONDS == stop_budget,
             "Python broker stop budget drifted from docs/rust-contract.md#7.3")
    _require(broker_protocol.DRAIN_DEADLINE_SECONDS == drain_deadline,
             "Python broker drain deadline drifted from docs/rust-contract.md#7.3")
    _require(broker_protocol.EMERGENCY_DEADLINE_SECONDS == emergency_deadline,
             "Python broker emergency deadline drifted from docs/rust-contract.md#7.3")
    _require(broker_protocol.DRAIN_DEADLINE_SECONDS <= dispatch,
             "broker drain deadline exceeds the dispatch budget")

    deploy = _section(contract, "### 12.1 安装、更新、回滚")
    deploy_match = re.search(r"--drain-budget (\d+)", deploy)
    _require(deploy_match is not None, "contract deploy drain budget missing")

    return {
        "timeouts": [
            {"surface": "gateway", "name": "first_byte",
             "value": first_byte, "unit": "seconds",
             "source": CONTRACT_RELATIVE + "#5.3"},
            {"surface": "gateway", "name": "socket_silence",
             "value": socket_silence, "unit": "seconds",
             "source": CONTRACT_RELATIVE + "#5.3"},
            {"surface": "gateway", "name": "request_total",
             "value": request_total, "unit": "seconds",
             "source": CONTRACT_RELATIVE + "#5.3"},
            {"surface": "gateway", "name": "prelude_max",
             "value": prelude, "unit": "bytes",
             "source": CONTRACT_RELATIVE + "#5.3"},
            {"surface": "gateway", "name": "line_buffer_max",
             "value": line_buffer, "unit": "bytes",
             "source": CONTRACT_RELATIVE + "#5.3"},
            {"surface": "gateway", "name": "queue_max",
             "value": queue_max, "unit": "entries",
             "source": CONTRACT_RELATIVE + "#5.3"},
            {"surface": "gateway", "name": "reader_max",
             "value": readers, "unit": "concurrent",
             "source": CONTRACT_RELATIVE + "#5.3"},
            {"surface": "broker", "name": "wire_deadline",
             "value": wire, "unit": "seconds",
             "source": CONTRACT_RELATIVE + "#7.3"},
            {"surface": "broker", "name": "dispatch_deadline",
             "value": dispatch, "unit": "seconds",
             "source": CONTRACT_RELATIVE + "#7.3"},
            {"surface": "broker", "name": "drain_window",
             "value": drain_window, "unit": "seconds",
             "source": CONTRACT_RELATIVE + "#7.3"},
            {"surface": "broker", "name": "stop_budget",
             "value": stop_budget, "unit": "seconds",
             "source": CONTRACT_RELATIVE + "#7.3"},
            {"surface": "broker", "name": "drain_deadline",
             "value": drain_deadline, "unit": "seconds",
             "source": CONTRACT_RELATIVE + "#7.3"},
            {"surface": "broker", "name": "emergency_deadline",
             "value": emergency_deadline, "unit": "seconds",
             "source": CONTRACT_RELATIVE + "#7.3"},
            {"surface": "display", "name": "grant_ttl",
             "value": grant_ttl, "unit": "seconds",
             "source": CONTRACT_RELATIVE + "#6.4"},
            {"surface": "display", "name": "session_ttl",
             "value": session_ttl, "unit": "seconds",
             "source": CONTRACT_RELATIVE + "#6.4"},
            {"surface": "display", "name": "idle",
             "value": idle, "unit": "seconds",
             "source": CONTRACT_RELATIVE + "#6.4"},
            {"surface": "display", "name": "max_session",
             "value": max_session, "unit": "seconds",
             "source": CONTRACT_RELATIVE + "#6.4"},
            {"surface": "display", "name": "handshake",
             "value": handshake, "unit": "seconds",
             "source": CONTRACT_RELATIVE + "#6.4"},
            {"surface": "display", "name": "close_grace",
             "value": close_grace, "unit": "seconds",
             "source": CONTRACT_RELATIVE + "#6.4"},
            {"surface": "deploy", "name": "drain",
             "value": float(deploy_match.group(1)), "unit": "seconds",
             "source": CONTRACT_RELATIVE + "#12.1"},
        ],
        "cancellation": [
            {"surface": "gateway", "name": "client_disconnect",
             "check": "every_byte", "outcome": "client_disconnected",
             "source": CONTRACT_RELATIVE + "#5.3"},
            {"surface": "gateway", "name": "pre_commit_failure",
             "check": "before_first_event", "outcome": "502",
             "source": CONTRACT_RELATIVE + "#5.3"},
            {"surface": "gateway", "name": "post_commit_failure",
             "check": "after_first_event", "outcome": "in_band_error",
             "source": CONTRACT_RELATIVE + "#5.3"},
            {"surface": "gateway", "name": "retry",
             "check": "before_first_event", "outcome": "hidden_retry",
             "source": CONTRACT_RELATIVE + "#5.3"},
            {"surface": "console", "name": "operation_cancel",
             "check": "queued", "outcome": "cancelled",
             "source": CONTRACT_RELATIVE + "#6.4"},
            {"surface": "console", "name": "operation_cancel",
             "check": "running", "outcome": "cancel_requested",
             "source": CONTRACT_RELATIVE + "#6.4"},
        ],
        "idempotency": [
            {"surface": "console", "scope": "operation",
             "keyMinLength": 1, "keyMaxLength": 128,
             "reuseStatus": 200, "conflictStatus": 409,
             "conflictCode": "idempotency_key_conflict",
             "source": CONTRACT_RELATIVE + "#6.3"},
            {"surface": "console", "scope": "key_create_rotate",
             "replayReturnsPlaintext": False,
             "source": CONTRACT_RELATIVE + "#6.4"},
            {"surface": "broker", "scope": "request_id",
             "oneShot": True,
             "source": "share/broker_protocol.py:206-280"},
            {"surface": "display", "scope": "grant",
             "oneShot": True,
             "source": CONTRACT_RELATIVE + "#10.2"},
            {"surface": "display", "scope": "operator",
             "acquire": "idempotent",
             "source": CONTRACT_RELATIVE + "#10.2"},
        ],
        "retry": {
            "fastFailuresMax": int(retry_match.group(1)),
            "slowFailureThresholdSeconds": int(retry_match.group(2)),
            "slowFailuresMax": int(retry_match.group(3)),
            "physicalAttemptsMax": int(retry_match.group(4)),
            "retryableStatuses": retryable,
            "source": CONTRACT_RELATIVE + "#5.3",
        },
        "sideEffects": [
            {"surface": "gateway", "kind": "ledger_append",
             "target": "usage-share-backend.jsonl", "blocking": False,
             "source": CONTRACT_RELATIVE + "#5.5"},
            {"surface": "gateway", "kind": "service_log_append",
             "target": "share-backend.log", "blocking": False,
             "source": CONTRACT_RELATIVE + "#5.5"},
            {"surface": "gateway", "kind": "revoke_state_atomic_write",
             "target": "share-revoke.state", "blocking": False,
             "source": CONTRACT_RELATIVE + "#5.5"},
            {"surface": "gateway", "kind": "instance_control_channel",
             "target": "AF_UNIX", "blocking": False,
             "source": CONTRACT_RELATIVE + "#5.5"},
            {"surface": "console", "kind": "sqlite_write",
             "target": "data/console-state.sqlite3", "blocking": False,
             "source": CONTRACT_RELATIVE + "#6.5"},
            {"surface": "console", "kind": "sqlite_write",
             "target": "share-store.db", "blocking": False,
             "source": CONTRACT_RELATIVE + "#6.5"},
            {"surface": "console", "kind": "security_log_append",
             "target": "share-store.db#security", "blocking": False,
             "source": CONTRACT_RELATIVE + "#6.5"},
            {"surface": "console", "kind": "projection_publish",
             "target": "broker", "blocking": True,
             "source": CONTRACT_RELATIVE + "#6.5"},
            {"surface": "broker", "kind": "projection_atomic_write",
             "target": "projection", "blocking": True,
             "source": CONTRACT_RELATIVE + "#7.3"},
            {"surface": "display", "kind": "runtime_dir_write",
             "target": "/run/um-share", "blocking": False,
             "source": CONTRACT_RELATIVE + "#10.3"},
            {"surface": "cloudflare", "kind": "none",
             "target": None, "blocking": False,
             "source": CONTRACT_RELATIVE + "#11.3"},
            {"surface": "deploy", "kind": "release_write",
             "target": "release/current", "blocking": False,
             "source": CONTRACT_RELATIVE + "#12.3"},
        ],
        "forbidden": [
            "gateway_started_by_console",
            "second_sqlite_store",
            "second_broker",
            "second_display_relay",
            "cloudflare_config_change",
            "automatic_backup",
        ],
    }


def _broker(contract: str) -> dict:
    section = _section(contract, "### 7.1 传输与帧")
    max_message = _doc_number(section, r"最大 (\d+) MiB", "broker max message") \
        * 1024 * 1024
    ts_window = _doc_number(section, r"±(\d+) 秒", "broker ts window")
    _require(broker_protocol.MAX_MESSAGE == max_message,
             "Python broker message bound drifted from docs/rust-contract.md#7.1")
    _require(broker_protocol.TS_WINDOW_SECONDS == ts_window,
             "Python broker ts window drifted from docs/rust-contract.md#7.1")
    return {
        "schemaVersion": broker_protocol.SCHEMA_VERSION,
        "maxMessageBytes": broker_protocol.MAX_MESSAGE,
        "tsWindowSeconds": broker_protocol.TS_WINDOW_SECONDS,
        "defaultDeadlineSeconds": broker_protocol.DEFAULT_DEADLINE_SECONDS,
        "drainWindowSeconds": broker_protocol.DRAIN_WINDOW_SECONDS,
        "stopBudgetSeconds": broker_protocol.STOP_BUDGET_SECONDS,
        "drainDeadlineSeconds": broker_protocol.DRAIN_DEADLINE_SECONDS,
        "emergencyDeadlineSeconds": broker_protocol.EMERGENCY_DEADLINE_SECONDS,
        "dispatchDeadlineSeconds": 130.0,
        "managementActions": sorted(broker_protocol.MANAGEMENT_ACTIONS),
        "instanceActions": sorted(broker_protocol.INSTANCE_ACTIONS),
        "resultCodes": sorted(broker_protocol.RESULT_CODES),
        "managementSignedKeys": list(broker_protocol.MANAGEMENT_SIGNED_KEYS),
        "instanceSignedKeys": list(broker_protocol.INSTANCE_SIGNED_KEYS),
        "requestIdOneShot": True,
        "source": "share/broker_protocol.py:29-58,108-180",
    }


def _key_management(contract: str) -> dict:
    section = _section(contract, "### 8.2 语义")
    key_match = re.search(r"默认每 Key 并发 (\d+)，允许 (\d+)\.\.(\d+)", section)
    share_match = re.search(
        r"全局分享并发：(\d+)\.\.(\d+)；新部署默认 (\d+)；旧部署保持 (\d+)", section)
    _require(key_match is not None, "contract key concurrency bounds missing")
    _require(share_match is not None, "contract share concurrency bounds missing")
    key_default, key_min, key_max = (int(value) for value in key_match.groups())
    share_min, share_max, share_default, share_legacy = (
        int(value) for value in share_match.groups())
    _require(share_store.NEW_KEY_DEFAULT_MAX_CONCURRENCY == key_default,
             "Python key concurrency default drifted from docs/rust-contract.md#8.2")
    _require(share_store.KEY_MAX_CONCURRENCY_MIN == key_min,
             "Python key concurrency minimum drifted from docs/rust-contract.md#8.2")
    _require(share_store.KEY_MAX_CONCURRENCY_MAX == key_max,
             "Python key concurrency maximum drifted from docs/rust-contract.md#8.2")
    _require(share_store.SHARE_MAX_CONCURRENCY_MIN == share_min,
             "Python share concurrency minimum drifted from docs/rust-contract.md#8.2")
    _require(share_store.SHARE_MAX_CONCURRENCY_MAX == share_max,
             "Python share concurrency maximum drifted from docs/rust-contract.md#8.2")
    _require(share_store.NEW_DEPLOYMENT_SHARE_MAX_CONCURRENCY == share_default,
             "Python share concurrency default drifted from docs/rust-contract.md#8.2")
    _require(share_store.LEGACY_SHARE_MAX_CONCURRENCY == share_legacy,
             "Python legacy share concurrency drifted from docs/rust-contract.md#8.2")
    return {
        "schemaVersion": share_store.SCHEMA_VERSION,
        "projectionSchemaVersion": share_store.PROJECTION_SCHEMA_VERSION,
        "keyConcurrency": {
            "default": key_default,
            "min": key_min,
            "max": key_max,
        },
        "shareConcurrency": {
            "newDeployment": share_default,
            "legacy": share_legacy,
            "min": share_min,
            "max": share_max,
        },
        "idempotencyKeyMinLength": 1,
        "idempotencyKeyMaxLength": 128,
        "plaintextReturnedOnce": True,
        "source": "share/store.py:54-90,1010-1050",
    }


def _metering(contract: str) -> dict:
    section = _section(contract, "### 9.2 读面")
    page_match = re.search(
        r"`limit` (\d+)\.\.(\d+)，默认 (\d+)", section)
    _require(page_match is not None, "contract metering page bounds missing")
    page_min, page_max, page_default = (int(value) for value in page_match.groups())
    _require(share_metering.SHARE_REQUEST_PAGE_MAX == page_max,
             "Python metering page maximum drifted from docs/rust-contract.md#9.2")
    _require(share_metering.SHARE_REQUEST_PAGE_DEFAULT == page_default,
             "Python metering page default drifted from docs/rust-contract.md#9.2")
    _require(page_min == 1, "contract metering page minimum changed")
    ledger_limit = _doc_number(
        _section(contract, "### 9.2 读面"), r"最多 (\d+) 行", "metering ledger limit")
    _require(share_metering.DEFAULT_LIMIT == ledger_limit,
             "Python metering ledger limit drifted from docs/rust-contract.md#9.2")
    return {
        "usageSources": list(share_metering.USAGE_SOURCES),
        "cacheReportStatuses": list(share_metering.CACHE_REPORT_STATUSES),
        "completeStatus": share_metering.COMPLETE_STATUS,
        "requestResults": list(share_metering.SHARE_REQUEST_RESULTS),
        "windows": list(share_metering.SHARE_USAGE_WINDOWS),
        "pageMin": page_min,
        "pageDefault": share_metering.SHARE_REQUEST_PAGE_DEFAULT,
        "pageMax": share_metering.SHARE_REQUEST_PAGE_MAX,
        "ledgerLimit": share_metering.DEFAULT_LIMIT,
        "privateLedgerBasenames": list(share_metering.PRIVATE_LEDGER_BASENAMES),
        "source": "share/metering.py:30-60",
    }


def _display(contract: str) -> dict:
    section = _section(contract, "### 10.2 语义")
    grant_match = re.search(r"TTL (\d+) 秒；最多 (\d+) 个待用", section)
    session_match = re.search(r"session 可重连、绑定 origin、TTL (\d+) 秒；最多 (\d+) 个", section)
    connection_match = re.search(
        r"连接上限 (\d+)、idle (\d+) 秒、单会话 (\d+) 秒、握手 (\d+) 秒、关闭宽限 (\d+) 秒",
        section)
    _require(grant_match is not None, "contract display grant bounds missing")
    _require(session_match is not None, "contract display session bounds missing")
    _require(connection_match is not None, "contract display connection bounds missing")
    defaults = display_proxy.DisplayConfig()
    _require(display_proxy.GRANT_TTL == int(grant_match.group(1)),
             "Python display grant TTL drifted from docs/rust-contract.md#10.2")
    _require(display_proxy.MAX_PENDING_GRANTS == int(grant_match.group(2)),
             "Python display grant capacity drifted from docs/rust-contract.md#10.2")
    _require(display_proxy.DISPLAY_SESSION_TTL == int(session_match.group(1)),
             "Python display session TTL drifted from docs/rust-contract.md#10.2")
    _require(display_proxy.MAX_DISPLAY_SESSIONS == int(session_match.group(2)),
             "Python display session capacity drifted from docs/rust-contract.md#10.2")
    _require(defaults.max_connections == int(connection_match.group(1)),
             "Python display connection limit drifted from docs/rust-contract.md#10.2")
    _require(defaults.idle_timeout == int(connection_match.group(2)),
             "Python display idle timeout drifted from docs/rust-contract.md#10.2")
    _require(defaults.max_session_seconds == int(connection_match.group(3)),
             "Python display session budget drifted from docs/rust-contract.md#10.2")
    _require(defaults.handshake_timeout == int(connection_match.group(4)),
             "Python display handshake timeout drifted from docs/rust-contract.md#10.2")
    _require(defaults.relay_join_timeout == int(connection_match.group(5)),
             "Python display close grace drifted from docs/rust-contract.md#10.2")
    return {
        "grantTtlSeconds": display_proxy.GRANT_TTL,
        "sessionTtlSeconds": display_proxy.DISPLAY_SESSION_TTL,
        "maxPendingGrants": display_proxy.MAX_PENDING_GRANTS,
        "maxSessions": display_proxy.MAX_DISPLAY_SESSIONS,
        "maxConnections": defaults.max_connections,
        "idleTimeoutSeconds": defaults.idle_timeout,
        "maxSessionSeconds": defaults.max_session_seconds,
        "handshakeTimeoutSeconds": defaults.handshake_timeout,
        "closeGraceSeconds": defaults.relay_join_timeout,
        "maxGrantBodyBytes": display_proxy.MAX_GRANT_BODY,
        "maxGrantTokenChars": display_proxy.MAX_GRANT_TOKEN,
        "source": "console/display_proxy.py:91-107,166-180",
    }


def _share_auth() -> dict:
    return {
        "messagesPath": share_auth.MESSAGES_PATH,
        "chatFields": sorted(share_auth.CHAT_FIELDS),
        "messagesFields": sorted(share_auth.MESSAGES_FIELDS),
        "source": "chat2api/share_auth.py:75-78,1145-1190",
    }


def _harness(ports: dict) -> dict:
    return {
        "bindHost": "127.0.0.1",
        "bindPort": 0,
        "tempDirPrefix": "um-rust-contract-",
        "productionPorts": ports["production"],
        "productionDataRoots": [
            "data",
            "profile",
            "tmp",
            "output",
            "chat2api/token.json",
            "chat2api/.edge-profile",
            "/var/lib/um-share",
            "/var/lib/um-share-console",
            "/etc/um-share",
        ],
    }


def _coverage(endpoints, errors, sse, boundaries) -> dict:
    return {
        "endpoints": bool(endpoints),
        "errors": bool(errors),
        "sseEvents": bool(sse["gateway"]["responses"]["events"]),
        "timeouts": bool(boundaries["timeouts"]),
        "cancellation": bool(boundaries["cancellation"]),
        "idempotency": bool(boundaries["idempotency"]),
        "sideEffects": bool(boundaries["sideEffects"]),
    }


def _source_hashes(names: list[str]) -> dict:
    hashes = {}
    for name in names:
        text = (ROOT / name).read_text(encoding="utf-8").replace("\r\n", "\n")
        hashes[name] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return hashes


def generate() -> dict:
    contract = _text(CONTRACT_RELATIVE)
    endpoints = []
    endpoints.extend(_endpoints(
        _section(contract, "### 5.1 端点与鉴权"), "gateway",
        CONTRACT_RELATIVE + "#5.1"))
    endpoints.extend(_endpoints(
        _section(contract, "### 6.1 端点与角色边界"), "console",
        CONTRACT_RELATIVE + "#6.1"))
    endpoints.sort(key=lambda item: (item["surface"], item["method"], item["path"]))
    if len(endpoints) < 20:
        raise SystemExit("contract endpoint coverage unexpectedly small")
    if any(item["path"] != "*" and not item["path"].startswith("/")
           for item in endpoints):
        raise SystemExit("contract endpoint path must be absolute or *")

    ports = _ports(_section(contract, "### 2.1 当前部署与仓库默认值"))
    errors = _errors(contract)
    sse = _sse(contract)
    boundaries = _boundaries(contract)
    broker = _broker(contract)
    key_management = _key_management(contract)
    metering = _metering(contract)
    display = _display(contract)
    share_auth = _share_auth()
    cloudflare = _cloudflare()
    for path in cloudflare["sharePaths"]:
        if not any(item["surface"] == "gateway" and item["path"] == path
                   for item in endpoints):
            raise SystemExit("share path missing from the gateway endpoint table: %s" % path)

    return {
        "metadata": {
            "fixture": True,
            "contract": CONTRACT_RELATIVE,
            "contractVersion": "rust-contract-v1",
            "generatedFrom": "tests/generate_rust_contract_fixture.py",
            "sourceHashEncoding": "utf8-lf",
            "surfaces": sorted({item["surface"] for item in endpoints}),
            "sourceHashes": _source_hashes([
                CONTRACT_RELATIVE,
                "tests/generate_rust_contract_fixture.py",
                "chat2api/chat2api.py",
                "chat2api/share_auth.py",
                "console/display_proxy.py",
                "share/broker_protocol.py",
                "share/metering.py",
                "share/store.py",
                "shared/responses_protocol.py",
                "um-cloud-deploy/cloudflared/check-ingress.py",
            ]),
        },
        "ports": ports,
        "endpoints": endpoints,
        "errors": errors,
        "sse": sse,
        "boundaries": boundaries,
        "broker": broker,
        "keyManagement": key_management,
        "metering": metering,
        "display": display,
        "shareAuth": share_auth,
        "cloudflare": cloudflare,
        "harness": _harness(ports),
        "coverage": _coverage(endpoints, errors, sse, boundaries),
    }


if __name__ == "__main__":
    result = generate()
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    DESTINATION.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print("Generated Rust contract fixture:", DESTINATION)
