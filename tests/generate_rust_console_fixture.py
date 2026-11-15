# -*- coding: utf-8 -*-
"""Generate the Rust S5a console fixture from the real Python 8002 console.

The Rust candidate must match the Python cloud console, so the expected status
codes, bodies, shapes and cache metadata are recorded by running the Python
`usagesrv.Handler` in-process. The Rust tests only read the generated fixture;
they never hand-write Python behavior.

Only loopback ephemeral servers and temporary directories are used. The Python
console is started on `127.0.0.1:0`; `USAGESRV_PORT=8002` is only the reported
deployment port, so no production port is bound and no production data path is
read or written.
"""

import http.client
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "rust_console_rev1.json"

CLOUD_PREFIXES = (
    "usage-tokens",
    "usage-rates",
    "gateway-connection",
    "logs",
    "log-sources",
    "log-entries",
    "log-overview",
    "stats",
    "um",
)


def shape(value):
    if isinstance(value, dict):
        return {"object": {key: shape(value[key]) for key in sorted(value)}}
    if isinstance(value, list):
        return {"array": [shape(value[0])] if value else []}
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    return type(value).__name__


def cloud_env(tmp):
    return {
        "UM_SHARE_ROLE": "cloud",
        "USAGESRV_PORT": "8002",
        "UM_SHARE_GATEWAY_PORT": "8003",
        "UM_SHARE_GATEWAY_PID_FILE": str(Path(tmp.name) / "share-gateway.pid"),
        "UM_SHARE_HOST_STATE_DIR": str(Path(tmp.name) / "run"),
        "SHARE_MANAGEMENT_CONFIG": str(Path(tmp.name) / "absent.json"),
        "CONSOLE_STATE_FILE": str(Path(tmp.name) / "console.sqlite3"),
        "CONSOLE_LOCK_DIR": str(Path(tmp.name) / "locks"),
        "USAGE_SESSION_CODE_FILE": str(Path(tmp.name) / "session-code"),
        "USAGE_CONTROL_TOKEN_FILE": str(Path(tmp.name) / "control-token"),
        "SHARE_STORE_FILE": str(Path(tmp.name) / "share.db"),
        "CONSOLE_SHARE_BACKEND_DIR": str(Path(tmp.name) / "backend"),
    }


def load_cloud_usagesrv():
    spec = importlib.util.spec_from_file_location(
        "usagesrv_rust_console_fixture", ROOT / "usagesrv.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def request(server, method, target, headers=None, body=None):
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_address[1], timeout=10)
    try:
        connection.putrequest(method, target, skip_host=True,
                              skip_accept_encoding=True)
        supplied = [name.lower() for name, _ in (headers or [])]
        if "host" not in supplied:
            connection.putheader("Host", f"127.0.0.1:{server.server_address[1]}")
        for name, value in headers or []:
            connection.putheader(name, value)
        if body is not None:
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders()
        if body is not None:
            connection.send(body)
        response = connection.getresponse()
        raw = response.read()
        result = {
            "status": response.status,
            "headers": {
                key.lower(): value for key, value in response.getheaders()
            },
        }
        try:
            result["body"] = json.loads(raw) if raw else None
        except ValueError:
            result["body_raw"] = raw.decode("utf-8", "replace")
        try:
            connection.request(method, target, body=b"")
            connection.getresponse()
            result["closed_after_response"] = False
        except (ConnectionError, OSError):
            result["closed_after_response"] = True
        return result
    finally:
        connection.close()


def raw_request(server, method, target, headers, body=None):
    """Send raw headers (duplicates / invalid lengths) and read one response."""
    sock = socket.create_connection(
        ("127.0.0.1", server.server_address[1]), timeout=10)
    try:
        lines = [f"{method} {target} HTTP/1.1",
                 f"Host: 127.0.0.1:{server.server_address[1]}"]
        lines.extend(f"{name}: {value}" for name, value in headers)
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
        sock.sendall(raw)
        if body is not None:
            sock.sendall(body)
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        response = b"".join(chunks)
    finally:
        sock.close()
    header_end = response.find(b"\r\n\r\n")
    head = response[:header_end].decode("latin-1")
    lines = head.split("\r\n")
    status = int(lines[0].split()[1])
    body = response[header_end + 4:]
    result = {"status": status, "headers": {}}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            result["headers"][name.strip().lower()] = value.strip()
    try:
        result["body"] = json.loads(body) if body else None
    except ValueError:
        result["body_raw"] = body.decode("utf-8", "replace")
    return result


def stable_service_row(service):
    return {
        key: service.get(key) for key in (
            "key", "display_name", "port", "state", "healthy", "detail",
            "process_ids")
    }


def stable_operation_row(operation):
    return operation


def generate():
    tmp = tempfile.TemporaryDirectory(prefix="um-rust-console-fixture-")
    root = Path(tmp.name)
    patcher = mock.patch.dict(os.environ, cloud_env(tmp))
    patcher.start()
    module = load_cloud_usagesrv()
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        health = request(server, "GET", "/health")
        summary = request(server, "GET", "/api/summary")
        services_first = request(server, "GET", "/api/console/services")
        services_second = request(server, "GET", "/api/console/services")
        console_health = request(server, "GET", "/api/console/health")
        actions = request(server, "GET", "/api/console/actions")
        operations_empty = request(server, "GET", "/api/console/operations")

        manager = module.get_console_manager()
        schema = [
            row[0] for row in manager._db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table'"
                " AND name IN ('console_meta','operations') ORDER BY name"
            ).fetchall()
        ]
        meta_version = manager._db.execute(
            "SELECT value FROM console_meta WHERE key='schema_version'"
        ).fetchone()[0]
        insert_sql = (
            "INSERT INTO operations (id, action, target, params_json, state,"
            " idempotency_key, resource, generation, created_at, started_at,"
            " finished_at, result_json, error_json, steps_json,"
            " cancel_requested) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        rows = [
            ("op-c", "share.status", "share-gateway", "{}", "succeeded",
             None, "share:orchestration", 2, 3000.0, 3001.0, 3002.0,
             '{"ok":true}', None, '[{"note":"third"}]', 0),
            ("op-a", "service.start", "share-gateway", "{}", "failed",
             "idem-a", "share:orchestration", 1, 1000.0, 1001.0, 1002.0,
             None, '{"code":"runtime_error"}', "[]", 1),
            ("op-b", "share.diagnostics", "share-gateway",
             '{"restart_budget_s":30}', "queued", None,
             "share:orchestration", 0, 2000.0, None, None, None, None,
             "[]", 0),
        ]
        for row in rows:
            manager._db.execute(insert_sql, row)
        operations_seeded = request(
            server, "GET", "/api/console/operations")
        operations_limited = request(
            server, "GET", "/api/console/operations?limit=2")
        operations_zero = request(
            server, "GET", "/api/console/operations?limit=0")
        operations_negative = request(
            server, "GET", "/api/console/operations?limit=-5")
        operations_too_large = request(
            server, "GET", "/api/console/operations?limit=9999")
        operations_non_numeric = request(
            server, "GET", "/api/console/operations?limit=abc")
        operations_blank = request(
            server, "GET", "/api/console/operations?limit=")
        operations_plus = request(
            server, "GET", "/api/console/operations?limit=%2B2")
        operation_detail = request(
            server, "GET", "/api/console/operations/op-a")
        operation_unknown = request(
            server, "GET", "/api/console/operations/zzz")

        capability = {}
        for prefix in CLOUD_PREFIXES:
            result = request(server, "GET", f"/api/console/{prefix}")
            capability[prefix] = {
                "status": result["status"],
                "body": result["body"],
            }

        duplicate_header = raw_request(
            server, "GET", "/api/console/services",
            [("X-Admin-Control", "a"), ("X-Admin-Control", "b")])
        duplicate_content_type = raw_request(
            server, "POST", "/api/console/operations",
            [("Content-Type", "application/json"),
             ("Content-Type", "application/json"),
             ("Content-Length", "2")])
        invalid_content_length = raw_request(
            server, "POST", "/api/console/services",
            [("Content-Length", "not-a-number")])
        too_large = raw_request(
            server, "POST", "/api/console/services",
            [("Content-Length", "65537")])
        transfer_encoding = raw_request(
            server, "POST", "/api/console/services",
            [("Transfer-Encoding", "chunked")])
        content_encoding = raw_request(
            server, "POST", "/api/console/services",
            [("Content-Encoding", "gzip")])
        missing_content_type = raw_request(
            server, "POST", "/api/console/services",
            [("Content-Length", "2")], body=b"{}")
        wrong_content_type = raw_request(
            server, "POST", "/api/console/services",
            [("Content-Length", "2"), ("Content-Type", "text/plain")],
            body=b"{}")
        invalid_json = raw_request(
            server, "POST", "/api/console/services",
            [("Content-Length", "1"), ("Content-Type", "application/json")],
            body=b"{")
        non_object = raw_request(
            server, "POST", "/api/console/services",
            [("Content-Length", "2"), ("Content-Type", "application/json")],
            body=b"[]")
        null_body = raw_request(
            server, "POST", "/api/console/services",
            [("Content-Length", "4"), ("Content-Type", "application/json")],
            body=b"null")
        unknown_fields_body = b'{"unknown":1}'
        unknown_fields = raw_request(
            server, "POST", "/api/console/operations",
            [("Content-Length", str(len(unknown_fields_body))),
             ("Content-Type", "application/json")],
            body=unknown_fields_body)
        empty_post = request(server, "POST", "/api/console/services")
        foreign_host = request(
            server, "GET", "/api/console/services",
            [("Host", "evil.example")])
        foreign_origin = request(
            server, "GET", "/api/console/services",
            [("Origin", "http://127.0.0.1:8001")])
        module._console_status_cache["value"] = None
        module._console_status_cache["at"] = 0.0
        alias = request(server, "GET", "/gw/usagesrv/api/console/services")

        fixture = {
            "metadata": {
                "fixture": True,
                "source": "tests/generate_rust_console_fixture.py",
                "generatedFrom": "usagesrv.py + console/adapters.py"
                                " + console/operations.py",
                "role": "cloud",
            },
            "health": {
                "status": health["status"],
                "body": health["body"],
                "cache_control": health["headers"].get("cache-control"),
            },
            "summary": {
                "status": summary["status"],
                "shape": shape(summary["body"]),
                "role": summary["body"].get("role"),
                "assembly": summary["body"].get("assembly"),
                "services": summary["body"].get("services"),
                "dsh": summary["body"].get("dsh"),
                "dshMergedIntoTotal": summary["body"].get(
                    "dshMergedIntoTotal"),
                "hasSources": "sources" in summary["body"],
                "generatedEpochType": shape(
                    summary["body"].get("_generated_epoch")),
                "cachedAgeType": shape(
                    summary["body"].get("_cached_age_seconds")),
            },
            "services": {
                "status": services_first["status"],
                "shape": shape(services_first["body"]),
                "service": stable_service_row(
                    services_first["body"]["services"][0]),
                "meta": {
                    "source": services_first["body"]["meta"]["source"],
                    "firstCached": services_first["body"]["meta"]["cached"],
                    "secondCached": services_second["body"]["meta"]["cached"],
                    "secondCacheAgeType": shape(
                        services_second["body"]["meta"].get(
                            "cache_age_seconds")),
                },
            },
            "consoleHealth": {
                "status": console_health["status"],
                "shape": shape(console_health["body"]),
                "checkedAtType": shape(console_health["body"]["checked_at"]),
                "cached": console_health["body"]["cached"],
                "service": stable_service_row(
                    console_health["body"]["services"][0]),
                "host": console_health["body"]["services"][1],
            },
            "actions": {
                "status": actions["status"],
                "body": actions["body"],
            },
            "operations": {
                "empty": {
                    "status": operations_empty["status"],
                    "body": operations_empty["body"],
                },
                "seeded": {
                    "status": operations_seeded["status"],
                    "body": operations_seeded["body"],
                    "schema": schema,
                    "metaVersion": meta_version,
                    "insertSql": insert_sql,
                    "rows": [
                        list(row) for row in rows
                    ],
                },
                "limitCases": {
                    "limit2": {
                        "status": operations_limited["status"],
                        "body": operations_limited["body"],
                    },
                    "limit0": {
                        "status": operations_zero["status"],
                        "body": operations_zero["body"],
                    },
                    "limitNegative": {
                        "status": operations_negative["status"],
                        "body": operations_negative["body"],
                    },
                    "limitTooLarge": {
                        "status": operations_too_large["status"],
                        "body": operations_too_large["body"],
                    },
                    "limitNonNumeric": {
                        "status": operations_non_numeric["status"],
                        "body": operations_non_numeric["body"],
                    },
                    "limitBlank": {
                        "status": operations_blank["status"],
                        "body": operations_blank["body"],
                    },
                    "limitPlus": {
                        "status": operations_plus["status"],
                        "body": operations_plus["body"],
                    },
                },
                "detail": {
                    "status": operation_detail["status"],
                    "body": operation_detail["body"],
                },
                "unknown": {
                    "status": operation_unknown["status"],
                    "body": operation_unknown["body"],
                },
            },
            "capability": capability,
            "boundary": {
                "duplicateHeader": {
                    "status": duplicate_header["status"],
                    "body": duplicate_header["body"],
                },
                "duplicateContentType": {
                    "status": duplicate_content_type["status"],
                    "body": duplicate_content_type["body"],
                },
                "invalidContentLength": {
                    "status": invalid_content_length["status"],
                    "body": invalid_content_length["body"],
                },
                "tooLarge": {
                    "status": too_large["status"],
                    "body": too_large["body"],
                },
                "transferEncoding": {
                    "status": transfer_encoding["status"],
                    "body": transfer_encoding["body"],
                },
                "contentEncoding": {
                    "status": content_encoding["status"],
                    "body": content_encoding["body"],
                },
                "missingContentType": {
                    "status": missing_content_type["status"],
                    "body": missing_content_type["body"],
                },
                "wrongContentType": {
                    "status": wrong_content_type["status"],
                    "body": wrong_content_type["body"],
                },
                "invalidJson": {
                    "status": invalid_json["status"],
                    "body": invalid_json["body"],
                },
                "nonObject": {
                    "status": non_object["status"],
                    "body": non_object["body"],
                },
                "nullBody": {
                    "status": null_body["status"],
                    "body": null_body["body"],
                },
                "unknownFields": {
                    "status": unknown_fields["status"],
                    "body": unknown_fields["body"],
                },
                "emptyPost": {
                    "status": empty_post["status"],
                    "body": empty_post["body"],
                },
                "foreignHost": {
                    "status": foreign_host["status"],
                    "body": foreign_host["body"],
                },
                "foreignOrigin": {
                    "status": foreign_origin["status"],
                    "body": foreign_origin["body"],
                },
                "alias": {
                    "status": alias["status"],
                    "shape": shape(alias["body"]),
                    "service": stable_service_row(
                        alias["body"]["services"][0]),
                },
            },
        }
        return fixture
    finally:
        manager = getattr(module, "_console_manager", None)
        if manager is not None:
            manager.close()
        server.shutdown()
        server.server_close()
        patcher.stop()
        tmp.cleanup()


def main():
    fixture = generate()
    FIXTURE.write_text(
        json.dumps(fixture, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {FIXTURE}")


if __name__ == "__main__":
    main()
