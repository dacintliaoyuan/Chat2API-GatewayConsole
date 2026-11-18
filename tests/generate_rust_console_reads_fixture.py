# -*- coding: utf-8 -*-
"""Generate the Rust S5b console read-face fixture (#52).

The Rust candidate must match the real Python cloud console, so every expected
status code, body, shape and coverage fact is recorded by running the Python
`usagesrv.Handler` in-process. The Rust tests only read the generated fixture;
they never hand-write Python behavior.

Only loopback ephemeral servers and temporary directories are used. No production
port is bound and no production data path is read or written.
"""

import hashlib
from datetime import datetime, timedelta, timezone
import http.client
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "rust_console_reads_rev1.json"

sys.path[:0] = [str(ROOT)]

FIXTURE_KEY = bytes(range(32))
CONTROL_TOKEN = "fixture-control-token"
PUBLIC_ORIGIN = "https://share.example.test"
LEDGER_NAME = "usage-share-backend.jsonl"
SECURITY_STATE = {
    "format": "um-share-security-state-v1",
    "generation_floor": 3,
    "exported_at": "2026-11-15T00:00:00+00:00",
    "records": [
        {
            "record_id": "sec-1",
            "key_id": "key-active-1",
            "generation": 2,
            "action": "rotate",
            "secret_version": 2,
            "status": "remote_confirmed",
            "recorded_at": "2026-11-14T00:00:00+00:00",
            "applied_at": "2026-11-14T00:00:01+00:00",
            "applied_generation": 2,
            "instance_id": "instance-1",
            "remote_confirmed_at": "2026-11-14T00:00:02+00:00",
            "remote_receipt": {"key": "remote/2.json"},
        },
        {
            "record_id": "sec-2",
            "key_id": "key-active-2",
            "generation": 3,
            "action": "limit_lower",
            "secret_version": 1,
            "status": "pending",
            "recorded_at": "2026-11-15T00:00:00+00:00",
            "applied_at": None,
            "applied_generation": None,
            "instance_id": None,
            "remote_confirmed_at": None,
            "remote_receipt": None,
        },
    ],
}

KEYS = {
    "active1": {
        "key_id": "key-active-1",
        "name": "alpha",
        "secret": "um-share-alpha-0123456789abcdef",
        "max_concurrency": 8,
        "row_version": 1,
        "created_at": "2026-11-15T00:00:01+00:00",
        "updated_at": "2026-11-15T00:00:01+00:00",
    },
    "active2": {
        "key_id": "key-active-2",
        "name": "beta",
        "secret": "um-share-beta-0123456789abcdef",
        "max_concurrency": 16,
        "row_version": 1,
        "created_at": "2026-11-15T00:00:02+00:00",
        "updated_at": "2026-11-15T00:00:02+00:00",
    },
    "deleted": {
        "key_id": "key-deleted-1",
        "name": "deleted",
        "secret": "um-share-deleted-0123456789abc",
        "max_concurrency": 16,
        "row_version": 2,
        "created_at": "2026-11-15T00:00:03+00:00",
        "updated_at": "2026-11-15T00:00:09+00:00",
        "deleted_at": "2026-11-15T00:00:09+00:00",
    },
    "legacy": {
        "key_id": "key-legacy-1",
        "name": "legacy",
        "secret": "um-share-legacy-0123456789abcd",
        "max_concurrency": 16,
        "row_version": 1,
        "created_at": "2026-11-15T00:00:04+00:00",
        "updated_at": "2026-11-15T00:00:04+00:00",
        "secret_blob": None,
        "secret_algo": None,
    },
    "undecryptable": {
        "key_id": "key-undecryptable-1",
        "name": "undecryptable",
        "secret": "um-share-undecryptable-012345",
        "max_concurrency": 16,
        "row_version": 1,
        "created_at": "2026-11-15T00:00:05+00:00",
        "updated_at": "2026-11-15T00:00:05+00:00",
        "secret_algo": "bogus-scheme-v9",
    },
    "rotated": {
        "key_id": "key-rotated-1",
        "name": "rotated",
        "secret": "um-share-rotated-0123456789abc",
        "max_concurrency": 16,
        "row_version": 2,
        "created_at": "2026-11-15T00:00:06+00:00",
        "updated_at": "2026-11-15T00:00:07+00:00",
        "secret_version": 2,
    },
    "version": {
        "key_id": "key-version-1",
        "name": "version",
        "secret": "um-share-version-0123456789abc",
        "max_concurrency": 16,
        "row_version": 1,
        "created_at": "2026-11-15T00:00:07+00:00",
        "updated_at": "2026-11-15T00:00:07+00:00",
    },
}


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


def load_cloud_usagesrv():
    spec = importlib.util.spec_from_file_location(
        "usagesrv_rust_console_reads_fixture", ROOT / "usagesrv.py")
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
        return result
    finally:
        connection.close()


class FakeBroker:
    """Deterministic broker surface for the status read face."""

    def __init__(self, snapshot):
        self._snapshot = snapshot

    def snapshot(self):
        return dict(self._snapshot)

    def publish(self, projection, terminate_keys=None):
        digest = hashlib.sha256(
            json.dumps(projection, sort_keys=True).encode("utf-8")).hexdigest()
        return {
            "applied_generation": projection["config_generation"],
            "instance_id": "fixture-instance",
            "fingerprint": digest,
            "security_generation": projection["config_generation"],
        }


class FakeBrokerOps:
    def __init__(self, running):
        self._running = running

    def verify_running(self):
        return self._running

    def verify_closed(self):
        return None

    def start_host(self, explicit=False):
        return True

    def stop_host(self):
        return True

    def drain(self):
        return {"drained": True}

    def emergency_stop(self):
        return {"stopped": True}

    def repair_limits(self):
        return True

    def ensure_control_ready(self):
        return None


def _protect(secret):
    """Seal one `aesgcm-file-key-v1` blob with a fixed nonce.

    `share/secret_protection.py` refuses to protect on Windows (the Windows
    owner writes DPAPI rows), so the fixture seals the POSIX layout directly:
    `nonce || ciphertext || tag`, AAD `aesgcm-file-key-v1`.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = bytes(range(12))
    sealed = AESGCM(FIXTURE_KEY).encrypt(
        nonce, secret.encode("utf-8"), b"aesgcm-file-key-v1")
    return nonce + sealed


def seed_share_store(path):
    """Create schema 3 through the real Python owner, then insert fixed rows."""
    from share.store import ShareStore

    store = ShareStore(path)
    store.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
            ("share_max_concurrency", "32"))
        connection.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
            ("schema_version", "3"))
        connection.execute("DELETE FROM configgen")
        connection.execute(
            "INSERT INTO configgen"
            "(schema_version,config_generation,applied_generation)"
            " VALUES(3,4,4)")
        columns = (
            "key_id,name,secret_hash,secret_version,masked_key,secret_blob,"
            "secret_algo,enabled,max_concurrency,created_at,updated_at,"
            "deleted_at,row_version")
        for spec in KEYS.values():
            secret = spec["secret"]
            blob = spec.get("secret_blob", _protect(secret))
            algo = spec.get("secret_algo", "aesgcm-file-key-v1")
            connection.execute(
                f"INSERT INTO keys ({columns})"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    spec["key_id"], spec["name"],
                    hashlib.sha256(secret.encode("utf-8")).hexdigest(),
                    spec.get("secret_version", 1),
                    "um-share-****" + secret[-4:],
                    blob, algo,
                    0 if spec.get("deleted_at") else 1,
                    spec["max_concurrency"], spec["created_at"],
                    spec["updated_at"], spec.get("deleted_at"),
                    spec["row_version"],
                ))
        connection.execute(
            "INSERT INTO ops(id,kind,idempotency_key,status,payload,created_at)"
            " VALUES(?,?,?,?,?,?)",
            ("op-1", "host", None, "succeeded", '{"ok":true}',
             "2026-11-15T00:00:00+00:00"))
        connection.commit()
    finally:
        connection.close()
    return {"keys": KEYS, "store": export_store(path)}


def export_store(path):
    connection = sqlite3.connect(path)
    try:
        objects = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master"
            " WHERE sql IS NOT NULL"
            " ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, type,name"
        ).fetchall()
        ddl = [row[3] for row in objects]
        tables = ("meta", "keys", "ops", "configgen", "security")
        rows = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in tables
        }
        columns = {
            table: [row[1] for row in connection.execute(
                f"PRAGMA table_info({table})")]
            for table in tables
        }
        blob_columns = []
        encoded = {}
        for table in tables:
            encoded[table] = []
            for row in rows[table]:
                values = []
                for index, value in enumerate(row):
                    if isinstance(value, bytes):
                        blob_columns.append([table, columns[table][index]])
                        values.append(value.hex())
                    else:
                        values.append(value)
                encoded[table].append(values)
        return {
            "ddl": ddl,
            "columns": columns,
            "rows": encoded,
            "blobColumns": blob_columns,
        }
    finally:
        connection.close()


def cloud_env(root):
    backend = root / "share-backend"
    backend.mkdir()
    (backend / LEDGER_NAME).write_text("", encoding="utf-8")
    key_file = root / "share-secret.key"
    key_file.write_bytes(FIXTURE_KEY)
    control = root / "control-token"
    control.write_text(CONTROL_TOKEN, encoding="utf-8")
    security = root / "security.json"
    security.write_text(json.dumps(SECURITY_STATE), encoding="utf-8")
    return {
        "UM_SHARE_ROLE": "cloud",
        "USAGESRV_PORT": "8002",
        "UM_SHARE_GATEWAY_PORT": "8003",
        "UM_SHARE_GATEWAY_PID_FILE": str(root / "share-gateway.pid"),
        "UM_SHARE_HOST_STATE_DIR": str(root / "run"),
        "SHARE_MANAGEMENT_CONFIG": str(root / "absent.json"),
        "CONSOLE_STATE_FILE": str(root / "console.sqlite3"),
        "CONSOLE_LOCK_DIR": str(root / "locks"),
        "USAGE_SESSION_CODE_FILE": str(root / "session-code"),
        "USAGE_CONTROL_TOKEN_FILE": str(control),
        "SHARE_STORE_FILE": str(root / "share.db"),
        "CONSOLE_SHARE_BACKEND_DIR": str(backend),
        "CHAT2API_SHARE_SECRET_KEY_FILE": str(key_file),
        "CONSOLE_PUBLIC_ORIGIN": PUBLIC_ORIGIN,
        "UM_CLOUD_SECURITY_STATE": str(security),
    }


def status_section(module, server, root, store_seed):
    from console import share_actions as actions

    snapshots = {
        "confirmedRunning": {
            "name": "broker-client",
            "state": "ok",
            "broker": {"version": "2.0.0", "pid": 4242,
                       "diag": [{"stage": "publish", "ok": True}]},
        },
        "confirmedStopped": {
            "name": "broker-client",
            "state": "unavailable",
            "error_code": "deadline_exceeded",
        },
        "unconfirmed": {
            "name": "noop",
            "state": "offline",
            "note": "未安装/未授权高权限 broker；变更已保存但未在线发布",
        },
    }
    running_cases = {
        "confirmedRunning": True,
        "confirmedStopped": False,
        "unconfirmed": None,
    }
    cases = {}
    for name, snapshot in snapshots.items():
        module._share_manager = None
        module.get_share_manager()
        manager = module._share_manager
        manager.broker = FakeBroker(snapshot)
        actions._BROKER_OPS = FakeBrokerOps(running_cases[name])
        actions._MAINTENANCE_STATUS = lambda: {
            "checked_at": 1700000000,
            "instance": {"active_count": 3},
        }
        actions._share_entry_attribution = lambda: {
            "file_ready": True,
            "file_reason": "ok",
            "key_file": "/run/um-share/run/share-attribution.key",
            "key_chars": 43,
            "instance_loaded": {"value": "unknown"},
            "request_attribution_observed": False,
        }
        result = request(
            server, "GET", "/api/console/share-management/status")
        body = result["body"]
        last_check_at = body.pop("last_check_at", None)
        cases[name] = {
            "status": result["status"],
            "body": body,
            "lastCheckAtType": shape(last_check_at),
            "cacheControl": result["headers"].get("cache-control"),
        }
    actions._BROKER_OPS = None
    actions._MAINTENANCE_STATUS = None
    actions._share_entry_attribution = _default_attribution
    module._share_manager = None
    return {"cases": cases, "storeSeed": store_seed}


def _default_attribution():
    return None


def keys_section(server):
    result = request(server, "GET", "/api/console/share-management/keys")
    return {
        "status": result["status"],
        "body": result["body"],
        "cacheControl": result["headers"].get("cache-control"),
    }


def secret_section(server, seeded):
    """Pin the four readback outcomes through the real console read path.

    The Python owner refuses `aesgcm-file-key-v1` decryption on Windows (the
    Windows owner writes DPAPI rows), so this generator substitutes ONLY the
    platform-guarded decrypt primitive with the same Python AES-GCM scheme the
    POSIX owner uses. The manager, store lookup and HTTP read face stay real.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import share.store as store_module

    def posix_unprotect(blob, algo=None):
        if algo != "aesgcm-file-key-v1":
            raise store_module.StoreError("unknown share secret algorithm")
        return AESGCM(FIXTURE_KEY).decrypt(
            bytes(blob[:12]), bytes(blob[12:]), b"aesgcm-file-key-v1"
        ).decode("utf-8")

    original = store_module.unprotect_secret
    store_module.unprotect_secret = posix_unprotect
    try:
        active = seeded["keys"]["active1"]
        rotated = seeded["keys"]["rotated"]
        cases = {
            "plaintext": (active["key_id"], None),
            "plaintextExpected": (active["key_id"], 1),
            "versionConflict": (active["key_id"], 2),
            "legacy": (seeded["keys"]["legacy"]["key_id"], None),
            "undecryptable": (seeded["keys"]["undecryptable"]["key_id"], None),
            "deleted": (seeded["keys"]["deleted"]["key_id"], None),
            "unknown": ("no-such-key", None),
            "invalidExpected": (active["key_id"], "abc"),
            "zeroExpected": (active["key_id"], 0),
            "rotated": (rotated["key_id"], rotated["row_version"]),
        }
        results = {}
        for name, (key_id, expected) in cases.items():
            target = f"/api/console/share-management/keys/{key_id}/secret"
            if expected is not None:
                target += f"?expected_row_version={expected}"
            result = request(server, "GET", target)
            results[name] = {
                "status": result["status"],
                "body": result["body"],
                "cacheControl": result["headers"].get("cache-control"),
            }
        return results
    finally:
        store_module.unprotect_secret = original



def usage_section(server, root, module):
    """Pin the usage reader with a fixed clock, plus the HTTP boundary.

    Windows filtering depends on the read instant, so the expected summary and
    page bodies are produced by the REAL `share/metering.py` readers with an
    explicit `now`. The HTTP cases pin only the time-independent status
    codes, the no-window bodies and the 400 boundary.
    """
    from share import metering

    now = 1700000000.0
    beijing = datetime.fromtimestamp(now, timezone(timedelta(hours=8)))

    def stamp(delta):
        return (beijing - delta).isoformat(timespec="seconds")

    active1 = KEYS["active1"]["key_id"]
    active2 = KEYS["active2"]["key_id"]
    deleted = KEYS["deleted"]["key_id"]
    records = [
        {"time": stamp(timedelta(minutes=30)), "service": "chat2api",
         "model": "gpt-4o", "stream": True, "status": "ok",
         "input_tokens": 100, "output_tokens": 50,
         "cached_input_tokens": 10, "cache_report_status": "reported",
         "duration_ms": 1200, "usage_source": "upstream",
         "key_id": active1, "request_id": "req-1",
         "attribution": "verified"},
        {"time": stamp(timedelta(hours=2)), "service": "chat2api",
         "model": "gpt-4o", "status": "ok", "input_tokens": 20,
         "output_tokens": 10, "cache_report_status": "unreported",
         "duration_ms": 900, "usage_source": "upstream",
         "key_id": active1, "request_id": "req-2",
         "attribution": "verified"},
        {"time": stamp(timedelta(hours=3)), "service": "chat2api",
         "model": "gpt-4o", "status": "ok", "input_tokens": 30,
         "output_tokens": 15, "cache_report_status": "reported",
         "duration_ms": 700, "usage_source": "upstream",
         "key_id": active1, "request_id": "req-3",
         "attribution": "verified"},
        {"time": stamp(timedelta(hours=4)), "service": "chat2api",
         "model": "gpt-4o", "status": "ok", "input_tokens": 2,
         "output_tokens": 1, "cached_input_tokens": 0,
         "cache_report_status": "reported", "duration_ms": 100,
         "usage_source": "upstream", "key_id": deleted,
         "request_id": "req-4", "attribution": "verified"},
        {"time": stamp(timedelta(hours=5)), "service": "chat2api",
         "model": "gpt-4o", "status": "", "input_tokens": 3,
         "output_tokens": 2, "cache_report_status": "unreported",
         "duration_ms": 100, "usage_source": "upstream",
         "key_id": active2, "request_id": "req-5",
         "attribution": "verified"},
        {"time": stamp(timedelta(hours=6)), "service": "chat2api",
         "model": "gpt-4o", "status": "client_disconnected",
         "input_tokens": 4, "cache_report_status": "unreported",
         "usage_source": "partial", "key_id": active1,
         "request_id": "req-6", "attribution": "verified"},
        {"time": stamp(timedelta(days=2)), "service": "chat2api",
         "model": "claude", "status": "cancelled", "input_tokens": 5,
         "output_tokens": 0, "cached_input_tokens": 3,
         "cache_report_status": "reported", "duration_ms": 300,
         "usage_source": "partial", "key_id": active2,
         "request_id": "req-7", "attribution": "verified"},
        {"time": stamp(timedelta(days=10)), "service": "chat2api",
         "model": None, "status": "ok", "usage_source": "none",
         "attribution": "absent"},
        {"service": "chat2api", "model": "gpt-4o",
         "status": "upstream_failed", "input_tokens": 7,
         "output_tokens": 3, "usage_source": "estimated",
         "key_id": active2, "request_id": "req-9",
         "attribution": "verified"},
        {"time": stamp(timedelta(hours=1)), "service": "chat2api",
         "model": "gpt-4o", "status": "ok", "input_tokens": 11,
         "output_tokens": 4, "estimated": False,
         "duration_ms": 500, "key_id": active2,
         "request_id": "req-10", "attribution": "verified"},
    ]
    ledger = root / "share-backend" / LEDGER_NAME
    ledger.write_text(
        chr(10).join(json.dumps(record) for record in records) + chr(10),
        encoding="utf-8",
    )

    def direct(filters, cursor=None, limit=50, page=False):
        if not page:
            payload = metering.read_share_usage(
                str(ledger), limit=200000, now=now, **filters)
            payload = module.Handler._share_usage_attach_key_labels(
                None, payload, "keys", "key_id")
        else:
            payload = metering.read_share_requests(
                str(ledger), now=now, cursor=cursor, limit=limit,
                **filters)
            payload = module.Handler._share_usage_attach_key_labels(
                None, payload, "rows", "keyId")
        payload.pop("checkedAt", None)
        return payload

    summary = {
        "all": direct({}),
        "window7d": direct({"window": "7d"}),
        "window24h": direct({"window": "24h"}),
        "window1h": direct({"window": "1h"}),
        "unattributed": direct({"key_id": "unattributed"}),
        "keyActive": direct({"key_id": active1}),
        "modelGpt": direct({"model": "gpt-4o"}),
        "resultCancelled": direct({"result": "cancelled"}),
        "combined": direct(
            {"window": "7d", "model": "gpt-4o", "result": "success"}),
    }
    requests = {
        "default": direct({}, page=True),
        "limit2": direct({}, limit=2, page=True),
        "filtered": direct(
            {"window": "7d", "model": "gpt-4o", "result": "success"},
            page=True),
        "unknownKey": direct({"key_id": "no-such-key"}, page=True),
    }
    cursor = requests["limit2"].get("nextCursor")
    requests["cursor"] = direct({}, cursor=cursor, limit=2, page=True)

    def call(target):
        result = request(server, "GET", target)
        body = result["body"]
        if isinstance(body, dict):
            body.pop("checkedAt", None)
        return {
            "status": result["status"],
            "body": body,
            "cacheControl": result["headers"].get("cache-control"),
        }

    http = {
        "all": call("/api/console/share-usage"),
        "invalidWindow": call("/api/console/share-usage?window=2d"),
        "invalidResult": call("/api/console/share-usage?result=bogus"),
        "duplicateWindow": call(
            "/api/console/share-usage?window=1h&window=7d"),
        "unknownQuery": call("/api/console/share-usage?bogus=1"),
        "requestsDefault": call("/api/console/share-usage/requests"),
        "requestsLimit2": call(
            "/api/console/share-usage/requests?limit=2"),
        "requestsInvalidLimitZero": call(
            "/api/console/share-usage/requests?limit=0"),
        "requestsInvalidLimitText": call(
            "/api/console/share-usage/requests?limit=abc"),
        "requestsInvalidCursor": call(
            "/api/console/share-usage/requests?cursor=abc"),
        "requestsUnknownKey": call(
            "/api/console/share-usage/requests?key=no-such-key"),
    }
    return {
        "summary": summary,
        "requests": requests,
        "http": http,
        "ledger": ledger.read_text(encoding="utf-8"),
    }


class _DisplayDummy:
    _console_display_availability = staticmethod(
        lambda: {"available": False, "reason": "display_proxy_unavailable"})
    _console_operator_default = staticmethod(
        lambda: {"owned_by_me": False, "owner_present": False,
                 "generation": 0, "stale": False})
    _console_display_authorized = staticmethod(lambda: False)
    _console_display_state_of = staticmethod(
        lambda auth, display: (None, None, None))
    _console_operator_state = lambda self: {
        "owned_by_me": False, "owner_present": False,
        "generation": 0, "stale": False}


def _display_payload(module):
    dummy = _DisplayDummy()
    dummy._console_display_availability = staticmethod(
        module.Handler._console_display_availability)
    dummy._console_display_state_of = staticmethod(
        module.Handler._console_display_state_of)
    dummy._console_operator_state = lambda: module.Handler._console_operator_state(dummy)
    dummy._console_operator_default = staticmethod(
        module.Handler._console_operator_default)
    return dummy


def ac4_section(module, server, root):
    security = root / "security.json"
    security.write_text(json.dumps(SECURITY_STATE), encoding="utf-8")
    previous = os.environ.get("UM_CLOUD_SECURITY_STATE")

    def cloud_ops(state_path):
        os.environ["UM_CLOUD_SECURITY_STATE"] = state_path
        payload = module.cloud_ops_status()
        payload.pop("read_at", None)
        return payload

    try:
        cloud_ops_cases = {
            "unconfigured": cloud_ops(""),
            "valid": cloud_ops(str(security)),
            "invalid": cloud_ops("bad-security.json"),
        }
    finally:
        if previous is None:
            os.environ.pop("UM_CLOUD_SECURITY_STATE", None)
        else:
            os.environ["UM_CLOUD_SECURITY_STATE"] = previous

    dummy = _display_payload(module)
    display_session = module.Handler._console_display_session_payload(dummy)
    observations = {
        "unwired": {
            "state": None, "label": None, "error": None, "primary": None,
            "observed_at": None, "login_window_deadline_epoch": None,
            "read_error": {
                "code": "auth_observation_failed", "message": "unwired"},
        },
        "valid": {
            "state": "valid", "label": "登录有效", "error": None,
            "primary": None, "observed_at": 1700000000,
            "login_window_deadline_epoch": 1700000600, "read_error": None,
        },
        "waitingMfa": {
            "state": "logging_in", "label": "正在登录",
            "error": "duo_prompt_waiting", "primary": None,
            "observed_at": 1700000000,
            "login_window_deadline_epoch": 1700000600, "read_error": None,
        },
        "duoAuto": {
            "state": "logging_in", "label": "正在完成 Duo 验证",
            "error": None, "primary": None,
            "observed_at": 1700000000,
            "login_window_deadline_epoch": 1700000600, "read_error": None,
        },
        "manual": {
            "state": "failed", "label": "登录失败",
            "error": "manual_takeover_required:umpass_mfa_required",
            "primary": None, "observed_at": 1700000000,
            "login_window_deadline_epoch": 1700000600, "read_error": None,
        },
        "cancelled": {
            "state": "cancelled", "label": "本轮登录已取消", "error": None,
            "primary": None, "observed_at": 1700000000,
            "login_window_deadline_epoch": None, "read_error": None,
        },
        "readError": {
            "state": None, "label": None, "error": None, "primary": None,
            "observed_at": None, "login_window_deadline_epoch": None,
            "read_error": {
                "code": "auth_observation_failed",
                "message": "URLError: unreachable",
            },
        },
    }
    display_states = {}
    for name, observation in observations.items():
        module._console_auth_observation = lambda: dict(observation)
        display_states[name] = module.Handler._console_display_state_payload(
            dummy)
    module._console_auth_observation = _default_auth_observation
    return {
        "cloudOps": cloud_ops_cases,
        "displaySession": display_session,
        "displayStates": display_states,
    }


def _default_auth_observation():
    return {
        "state": None, "label": None, "error": None, "primary": None,
        "observed_at": None, "login_window_deadline_epoch": None,
        "read_error": {
            "code": "auth_observation_failed", "message": "unwired"},
    }


def explicit_capability_section(server):
    paths = (
        "usage-tokens", "usage-rates", "logs", "log-sources",
        "log-entries", "log-overview", "gateway-connection", "um/today",
        "um/trend", "um/requests", "um/logs", "um/request-logs",
        "um/maintenance", "stats",
    )
    results = {}
    for path in paths:
        result = request(server, "GET", f"/api/console/{path}")
        results[path] = {
            "status": result["status"],
            "body": result["body"],
            "cacheControl": result["headers"].get("cache-control"),
        }
    return results


def ac5_section(module, server, root, seeded):
    from share.management import ShareManager

    db = root / "share.db"
    broker = FakeBroker({
        "name": "broker-client",
        "state": "ok",
        "broker": {"version": "2.0.0", "pid": 4242},
    })
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import share.store as store_module

    def posix_unprotect(blob, algo=None):
        if algo != "aesgcm-file-key-v1":
            raise store_module.StoreError("unknown share secret algorithm")
        return AESGCM(FIXTURE_KEY).decrypt(
            bytes(blob[:12]), bytes(blob[12:]), b"aesgcm-file-key-v1"
        ).decode("utf-8")

    original_unprotect = store_module.unprotect_secret
    store_module.unprotect_secret = posix_unprotect
    module._share_manager = ShareManager(store_path=db, broker=broker)
    module._session["session"] = "fixture-session"
    module._session["session_expires"] = time.time() + 3600
    active = seeded["keys"]["active1"]["key_id"]
    control = [("X-Admin-Control", CONTROL_TOKEN)]
    cookie = [("Cookie", "usage_session=fixture-session")]
    targets = {
        "status": "/api/share/status",
        "keys": "/api/share/keys",
        "key": f"/api/share/keys/{active}",
        "secret": f"/api/share/keys/{active}/secret",
        "operation": "/api/share/operations/op-1",
        "usage": "/api/share/usage",
        "usageRequests": "/api/share/usage/requests",
    }
    results = {}
    for name, target in targets.items():
        for auth_name, headers in (
                ("control", control), ("session", cookie)):
            result = request(server, "GET", target, headers)
            body = result["body"]
            if isinstance(body, dict):
                body.pop("checkedAt", None)
            results[f"{auth_name}.{name}"] = {
                "status": result["status"],
                "body": body,
                "cacheControl": result["headers"].get("cache-control"),
            }
    missing = request(server, "GET", "/api/share/status")
    wrong = request(
        server, "GET", "/api/share/status",
        [("X-Admin-Control", "wrong-token")])
    duplicate = request(
        server, "GET", "/api/share/status",
        [("X-Admin-Control", CONTROL_TOKEN),
         ("X-Admin-Control", CONTROL_TOKEN)])
    foreign = request(
        server, "GET", "/api/share/status",
        [("Origin", "http://evil.example")])
    results["noCredentials"] = {
        "status": missing["status"], "body": missing["body"],
        "cacheControl": missing["headers"].get("cache-control"),
    }
    results["wrongControl"] = {
        "status": wrong["status"], "body": wrong["body"],
        "cacheControl": wrong["headers"].get("cache-control"),
    }
    results["duplicateHeader"] = {
        "status": duplicate["status"], "body": duplicate["body"],
        "cacheControl": duplicate["headers"].get("cache-control"),
    }
    results["foreignOrigin"] = {
        "status": foreign["status"], "body": foreign["body"],
        "cacheControl": foreign["headers"].get("cache-control"),
    }
    module._share_manager = None
    module._session["session"] = None
    module._session["session_expires"] = 0.0
    store_module.unprotect_secret = original_unprotect
    return results

def main():
    fixture = {"metadata": {
        "fixture": True,
        "source": "tests/generate_rust_console_reads_fixture.py",
        "generatedFrom": "usagesrv.py + share/management.py + share/store.py"
                        " + console/share_actions.py",
        "role": "cloud",
        "fixtureKeyHex": FIXTURE_KEY.hex(),
    }}
    tmp = tempfile.TemporaryDirectory(prefix="um-rust-console-reads-")
    root = Path(tmp.name)
    patcher = mock.patch.dict(os.environ, cloud_env(root))
    patcher.start()
    module = load_cloud_usagesrv()
    seeded = seed_share_store(root / "share.db")
    store_seed = seeded["store"]
    module._share_manager = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        fixture["shareStatus"] = status_section(
            module, server, root, store_seed)
        fixture["shareStatus"]["keys"] = seeded["keys"]
        module._share_manager = None
        module.get_share_manager()
        fixture["shareKeys"] = keys_section(server)
        fixture["shareSecret"] = secret_section(server, seeded)
        fixture["shareUsage"] = usage_section(server, root, module)
        fixture["ac4"] = ac4_section(module, server, root)
        fixture["capabilityExplicit"] = explicit_capability_section(server)
        fixture["apiShare"] = ac5_section(module, server, root, seeded)
        return fixture
    finally:
        server.shutdown()
        server.server_close()
        patcher.stop()
        tmp.cleanup()


if __name__ == "__main__":
    payload = main()
    FIXTURE.write_text(
        json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {FIXTURE}")
