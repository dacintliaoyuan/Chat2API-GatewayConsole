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
import http.client
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
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
