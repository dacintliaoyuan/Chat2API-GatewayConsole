# -*- coding: utf-8 -*-
"""Generate the Rust S1a gateway fixture from the real Python 8003 handler.

The Rust candidate must match the Python merged gateway, so the expected status
codes, bodies, headers and JSON shapes are recorded by running the Python
``chat2api.Handler`` in-process against a stub upstream. The Rust tests only read
the generated fixture; they never hand-write Python behavior.

Only loopback ephemeral servers and temporary directories are used. No production
port is bound and no production data path is read or written.
"""

import hashlib
import http.client
import importlib.util
import io
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
CHAT2API_DIR = ROOT / "chat2api"
if str(CHAT2API_DIR) not in sys.path:
    sys.path.insert(0, str(CHAT2API_DIR))

SPEC = importlib.util.spec_from_file_location(
    "chat2api_gateway_fixture", CHAT2API_DIR / "chat2api.py")
chat2api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)
from share_auth import ShareGate  # noqa: E402

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import usagesrv  # noqa: E402


class StubUpstream(BaseHTTPRequestHandler):
    """Deterministic upstream for config, version, models and failures."""

    protocol_version = "HTTP/1.1"
    delay_s = 0.0

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def do_GET(self):
        if self.path == "/api/config":
            self._json(200, {
                "name": "Open WebUI",
                "version": "0.7.0-fixture",
                "features": {"enable_api_keys": False},
                "oauth": {"providers": {"fixture-sso": {}}},
            })
            return
        if self.path == "/api/version":
            if self.headers.get("X-Stub-Fail") == "version":
                self._json(500, {"error": "version failed"})
                return
            self._json(200, {"version": "0.7.0-fixture"})
            return
        if self.path == "/api/models":
            auth = self.headers.get("Authorization", "")
            if "rejected-token" in auth:
                self._json(401, {"detail": "rejected"})
                return
            if "error-token" in auth:
                self._json(500, {"detail": "upstream failed"})
                return
            if "invalid-shape-token" in auth:
                self._json(200, {"not": "a catalog"})
                return
            if self.delay_s:
                import time
                time.sleep(self.delay_s)
            self._json(200, {
                "object": "list",
                "data": [
                    {"id": "fixture-model-a", "name": "Fixture A"},
                    {"id": "fixture-model-b", "name": "Fixture B"},
                ],
            })
            return
        self._json(404, {"detail": "not found"})


def start_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubUpstream)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def start_gateway(stub_port, share_config, control_token_file):
    args = SimpleNamespace(
        base_url=f"http://127.0.0.1:{stub_port}",
        token="fixture-upstream-token",
        profile="fixture-profile",
        login_timeout=1,
        use_api_key=False,
        no_browser=True,
    )
    proxy = chat2api.Proxy({"token": "fixture-upstream-token"}, args)
    server = ThreadingHTTPServer(("127.0.0.1", 0), chat2api.Handler)
    server.daemon_threads = True
    server.proxy = proxy
    server.share_gate = ShareGate(share_config) if share_config else None
    server.local_api_key = ""
    server.require_local_api_key = False
    chat2api.Handler.log_message = lambda *args: None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


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
            "will_close": response.will_close,
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


def share_config_document(keys):
    return {"clients": {
        key_id: hashlib.sha256(secret.encode()).hexdigest()
        for key_id, secret in keys
    }}


def main():
    tmp = tempfile.TemporaryDirectory(prefix="um-rust-gateway-fixture-")
    root = Path(tmp.name)
    share_path = root / "share.json"
    share_path.write_text(json.dumps(share_config_document([
        ("friend", "secret"),
        ("second", "second-secret"),
        ("third", "third-secret"),
    ])), encoding="utf-8")
    control_path = root / "control.token"
    control_path.write_text("fixture-control-token", encoding="utf-8")

    original_origins = chat2api.ALLOWED_ORIGINS
    original_control = chat2api.CONTROL_TOKEN_FILE
    chat2api.ALLOWED_ORIGINS = frozenset({"https://allowed.example"})
    chat2api.CONTROL_TOKEN_FILE = str(control_path)
    stub = start_stub()
    gateway = start_gateway(stub.server_port, str(share_path),
                           str(control_path))
    try:
        health = request(gateway, "GET", "/health")
        ready = request(gateway, "GET", "/ready")
        version_share = request(gateway, "GET", "/v1/version")
        models = request(gateway, "GET", "/v1/models",
                         [("Authorization", "Bearer secret")])
        raw_fresh = request(
            gateway, "GET", "/v1/models",
            [("Authorization", "Bearer secret"),
             ("X-Chat2Api-Models", "raw-fresh")])
        gateway.share_gate = None
        try:
            version = request(gateway, "GET", "/v1/version")
        finally:
            gateway.share_gate = ShareGate(share_path)
        fixture = {
            "metadata": {
                "fixture": True,
                "source": "tests/generate_rust_gateway_fixture.py",
                "generatedFrom": "chat2api/chat2api.py + chat2api/share_auth.py",
                "upstream": "in-process stub on 127.0.0.1:0",
            },
            "readOnly": {
                "health": {
                    "status": health["status"],
                    "shape": shape(health["body"]),
                },
                "readyValid": {
                    "status": ready["status"],
                    "shape": shape(ready["body"]),
                },
                "version": {
                    "status": version["status"],
                    "shape": shape(version["body"]),
                },
                "versionShare": {
                    "status": version_share["status"],
                    "body": version_share["body"],
                },
                "models": {
                    "status": models["status"],
                    "body": models["body"],
                },
                "modelsRawFresh": {
                    "status": raw_fresh["status"],
                    "body": raw_fresh["body"],
                    "marker": raw_fresh["headers"].get(
                        "x-chat2api-raw-models"),
                },
            },
            "readyRejected": {},
            "shareGate": {
                "credentialCases": [],
                "routeCases": [],
                "limitCases": [],
                "gateReasons": [],
            },
            "bodyRejection": {},
            "cors": {},
            "control": {},
            "closeOnError": {},
        }
        _record_ready_rejected(fixture, gateway)
        _record_share_gate(fixture, gateway, share_path, root)
        _record_body_rejection(fixture, gateway)
        _record_cors(fixture, gateway)
        _record_control(fixture, gateway)
        output = ROOT / "tests" / "fixtures" / "rust_gateway_rev1.json"
        output.write_text(json.dumps(fixture, indent=1, sort_keys=True) + "\n",
                          encoding="utf-8")
        print(f"wrote {output}")
    finally:
        chat2api.ALLOWED_ORIGINS = original_origins
        chat2api.CONTROL_TOKEN_FILE = original_control
        gateway.shutdown()
        gateway.server_close()
        stub.shutdown()
        stub.server_close()
        tmp.cleanup()


def start_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubUpstream)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def start_gateway(stub_port, share_path, control_path):
    args = SimpleNamespace(
        base_url=f"http://127.0.0.1:{stub_port}",
        token="fixture-upstream-token",
        profile="fixture-profile",
        login_timeout=1,
        use_api_key=False,
        no_browser=True,
    )
    proxy = chat2api.Proxy({"token": "fixture-upstream-token"}, args)
    # Populate the public config summary the same way startup validation does.
    proxy.probe_config()
    server = ThreadingHTTPServer(("127.0.0.1", 0), chat2api.Handler)
    server.daemon_threads = True
    server.proxy = proxy
    server.share_gate = ShareGate(share_path)
    server.local_api_key = ""
    server.require_local_api_key = False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _record_ready_rejected(fixture, gateway):
    saved = gateway.proxy.creds.get("token")
    try:
        gateway.proxy.creds["token"] = "rejected-token"
        result = request(gateway, "GET", "/ready")
        fixture["readyRejected"] = {
            "status": result["status"],
            "body": {
                key: result["body"][key]
                for key in ("status", "authentication", "reason",
                            "upstream_status")
            },
        }
    finally:
        gateway.proxy.creds["token"] = saved


def _record_share_gate(fixture, gateway, share_path, root):
    credentials = [
        ("bearer", "GET", "/v1/models",
         [("Authorization", "Bearer secret")]),
        ("duplicate-authorization", "GET", "/v1/models",
         [("Authorization", "Bearer secret"),
          ("Authorization", "Bearer secret")]),
        ("empty-authorization", "GET", "/v1/models",
         [("Authorization", "Bearer ")]),
        ("overlong-authorization", "GET", "/v1/models",
         [("Authorization", "Bearer " + "x" * 257)]),
        ("wrong-prefix", "GET", "/v1/models",
         [("Authorization", "Token secret")]),
        ("different-values", "POST", "/v1/messages",
         [("Authorization", "Bearer secret"),
          ("X-Api-Key", "second-secret")]),
        ("same-values", "POST", "/v1/messages",
         [("Authorization", "Bearer secret"),
          ("X-Api-Key", "secret")]),
        ("api-key-only-messages", "POST", "/v1/messages",
         [("X-Api-Key", "secret")]),
        ("api-key-only-chat", "POST", "/v1/chat/completions",
         [("X-Api-Key", "secret")]),
        ("duplicate-api-key", "POST", "/v1/messages",
         [("X-Api-Key", "secret"), ("X-Api-Key", "secret")]),
        ("unknown-key", "GET", "/v1/models",
         [("Authorization", "Bearer wrong")]),
    ]
    for name, method, target, headers in credentials:
        result = request(gateway, method, target, headers)
        fixture["shareGate"]["credentialCases"].append({
            "name": name,
            "method": method,
            "target": target,
            "status": result["status"],
            "body": result["body"],
        })

    routes = [
        ("version", "GET", "/v1/version",
         [("Authorization", "Bearer secret")]),
        ("embeddings", "POST", "/v1/embeddings",
         [("Authorization", "Bearer secret")]),
        ("unknown-v1", "GET", "/v1/unknown",
         [("Authorization", "Bearer secret")]),
        ("chat", "POST", "/v1/chat/completions",
         [("Authorization", "Bearer secret")]),
        ("messages", "POST", "/v1/messages",
         [("Authorization", "Bearer secret")]),
        ("responses", "POST", "/v1/responses",
         [("Authorization", "Bearer secret")]),
        ("models-beta", "GET", "/v1/models?beta=true",
         [("Authorization", "Bearer secret")]),
        ("models-limit", "GET", "/v1/models?limit=5",
         [("Authorization", "Bearer secret")]),
        ("models-limit-zero", "GET", "/v1/models?limit=0",
         [("Authorization", "Bearer secret")]),
        ("models-limit-leading-zero", "GET", "/v1/models?limit=05",
         [("Authorization", "Bearer secret")]),
        ("models-limit-too-large", "GET", "/v1/models?limit=1001",
         [("Authorization", "Bearer secret")]),
        ("models-duplicate-limit", "GET", "/v1/models?limit=5&limit=6",
         [("Authorization", "Bearer secret")]),
        ("models-unknown-query", "GET", "/v1/models?beta=false",
         [("Authorization", "Bearer secret")]),
        ("messages-beta", "POST", "/v1/messages?beta=true",
         [("Authorization", "Bearer secret")]),
        ("messages-unknown-query", "POST", "/v1/messages?beta=false",
         [("Authorization", "Bearer secret")]),
    ]
    for name, method, target, headers in routes:
        result = request(gateway, method, target, headers)
        fixture["shareGate"]["routeCases"].append({
            "name": name,
            "method": method,
            "target": target,
            "status": result["status"],
            "body": result["body"],
        })

    # Per-key concurrency, global concurrency and the four 429 reasons are
    # recorded at the gate seam so the slow upstream never delays generation.
    gate = ShareGate(share_path)
    auth = gate.authenticate(_headers([("Authorization", "Bearer secret")]), True)
    entry, cfg = auth
    first = gate.acquire(entry["key_id"], cfg["config_generation"],
                         entry["secret_version"])
    assert first
    reason = {}
    refused = gate.acquire(entry["key_id"], cfg["config_generation"],
                           entry["secret_version"], reason_out=reason)
    assert not refused
    fixture["shareGate"]["gateReasons"].append({
        "name": "per_key_concurrency",
        "reason": reason.get("reason"),
        "detail": reason.get("detail"),
    })
    gate.release(entry["key_id"])
    gate.set_state("draining")
    reason = {}
    gate.acquire(entry["key_id"], cfg["config_generation"],
                 entry["secret_version"], reason_out=reason)
    fixture["shareGate"]["gateReasons"].append({
        "name": "state_not_accepting",
        "reason": reason.get("reason"),
        "detail": reason.get("detail"),
    })
    gate.set_state("running")

    # Global concurrency: two keys with per-key limit 1 and a one-key global
    # limit is a config fact, not a handler mutation; record it by constructing
    # a second gate over a projection-shaped config.
    projection_path = root / "share-projection.json"
    projection_path.write_text(json.dumps({
        "schema_version": 2,
        "config_generation": 3,
        "share_max_concurrency": 1,
        "data": [
            {"key_id": "friend", "name": "friend",
             "secret_hash": hashlib.sha256(b"secret").hexdigest(),
             "secret_version": 1, "enabled": True, "max_concurrency": 1},
            {"key_id": "second", "name": "second",
             "secret_hash": hashlib.sha256(b"second-secret").hexdigest(),
             "secret_version": 1, "enabled": True, "max_concurrency": 1},
        ],
    }), encoding="utf-8")
    global_gate = ShareGate(projection_path)
    first = global_gate.authenticate(
        _headers([("Authorization", "Bearer secret")]), True)
    second = global_gate.authenticate(
        _headers([("Authorization", "Bearer second-secret")]), True)
    global_gate.acquire(first[0]["key_id"], first[1]["config_generation"],
                        first[0]["secret_version"])
    reason = {}
    global_gate.acquire(second[0]["key_id"],
                        second[1]["config_generation"],
                        second[0]["secret_version"], reason_out=reason)
    fixture["shareGate"]["gateReasons"].append({
        "name": "share_concurrency",
        "reason": reason.get("reason"),
        "detail": reason.get("detail"),
    })

    # Rate limit: 20 requests in one minute, the 21st is refused.
    rate_gate = ShareGate(share_path)
    rate_entry, rate_cfg = rate_gate.authenticate(
        _headers([("Authorization", "Bearer secret")]), True)
    for _ in range(20):
        reason = {}
        accepted = rate_gate.acquire(
            rate_entry["key_id"], rate_cfg["config_generation"],
            rate_entry["secret_version"], reason_out=reason)
        assert accepted
        rate_gate.release(rate_entry["key_id"])
    reason = {}
    refused = rate_gate.acquire(
        rate_entry["key_id"], rate_cfg["config_generation"],
        rate_entry["secret_version"], reason_out=reason)
    fixture["shareGate"]["gateReasons"].append({
        "name": "key_rate_limit",
        "accepted": refused,
        "reason": reason.get("reason"),
        "detail": reason.get("detail"),
    })


def _headers(pairs):
    from email.message import Message
    message = Message()
    for name, value in pairs:
        message[name] = value
    return message


def _record_body_rejection(fixture, gateway):
    fixture["bodyRejection"] = {"share": {}, "console": {}}

    def run_share(name, target, body, headers=None):
        result = request(gateway, "POST", target, headers or [
            ("Authorization", "Bearer secret"),
            ("Content-Type", "application/json"),
        ], body)
        fixture["bodyRejection"]["share"][name] = {
            "target": target,
            "status": result["status"],
            "body": result["body"],
        }

    run_share("invalid-content-length", "/v1/chat/completions", b"",
              [("Authorization", "Bearer secret"),
               ("Content-Type", "application/json"),
               ("Content-Length", "not-a-number")])
    run_share("invalid-json", "/v1/chat/completions", b"{")
    run_share("non-object", "/v1/chat/completions", b"[]")
    run_share("unknown-chat-field", "/v1/chat/completions",
              b'{"model":"m","messages":[{"role":"user","content":"hi"}],'
              b'"unknown":1}')
    run_share("unknown-messages-field", "/v1/messages",
              b'{"model":"m","max_tokens":1,"messages":[],"unknown":1}')
    run_share("unknown-responses-field", "/v1/responses",
              b'{"model":"m","input":"hi","unknown":1}')

    original_limit = chat2api.SHARE_MAX_REQUEST_BYTES
    try:
        chat2api.SHARE_MAX_REQUEST_BYTES = 10
        run_share("payload-too-large", "/v1/chat/completions", b"x" * 11)
    finally:
        chat2api.SHARE_MAX_REQUEST_BYTES = original_limit

    # Console (8002) request boundary, called directly on the real method.
    console = SimpleNamespace()
    console.SINGLE_VALUE_HEADERS = usagesrv.Handler.SINGLE_VALUE_HEADERS
    for name, headers, body in (
        ("invalid-content-length", [("Content-Length", "not-a-number")], b""),
        ("duplicate-content-length",
         [("Content-Length", "2"), ("Content-Length", "2")], b"{}"),
        ("transfer-encoding",
         [("Transfer-Encoding", "chunked"),
          ("Content-Type", "application/json")], b"{}"),
        ("content-encoding",
         [("Content-Encoding", "gzip"),
          ("Content-Type", "application/json")], b"{}"),
        ("content-type-missing", [("Content-Length", "2")], b"{}"),
        ("content-type-wrong",
         [("Content-Length", "2"), ("Content-Type", "text/plain")], b"{}"),
        ("invalid-json",
         [("Content-Length", "1"), ("Content-Type", "application/json")],
         b"{"),
        ("non-object",
         [("Content-Length", "2"), ("Content-Type", "application/json")],
         b"[]"),
        ("request-too-large",
         [("Content-Length", "65537"),
          ("Content-Type", "application/json")], b"x" * 65537),
    ):
        console.headers = _headers(headers)
        console.rfile = io.BytesIO(body)
        parsed, status, code = usagesrv.Handler._read_share_json_body(console)
        fixture["bodyRejection"]["console"][name] = {
            "status": status,
            "code": code,
            "parsed": parsed,
        }

    console.headers = _headers([
        ("Content-Type", "application/json"),
        ("Content-Type", "application/json"),
    ])
    duplicate = usagesrv.Handler._duplicate_header_error(console)
    fixture["bodyRejection"]["console"]["duplicate-sensitive-header"] = {
        "status": 400 if duplicate else None,
        "message": duplicate,
    }

    captured = {}

    class _Sender:
        def _send_json(self, status, payload):
            captured["status"] = status
            captured["payload"] = payload

    usagesrv.Handler._reject_unknown_fields(
        _Sender(), {"key_id": "friend", "unknown": 1}, ("key_id",))
    fixture["bodyRejection"]["console"]["unknown-field"] = {
        "status": captured["status"],
        "body": captured["payload"],
    }


def _record_cors(fixture, gateway):
    result = request(gateway, "OPTIONS", "/v1/models",
                     [("Origin", "https://allowed.example")])
    fixture["cors"]["optionsAllowed"] = {
        "status": result["status"],
        "allowOrigin": result["headers"].get("access-control-allow-origin"),
        "allowMethods": result["headers"].get("access-control-allow-methods"),
        "allowHeaders": result["headers"].get("access-control-allow-headers"),
    }
    result = request(gateway, "OPTIONS", "/v1/models",
                     [("Origin", "https://denied.example")])
    fixture["cors"]["optionsDenied"] = {
        "status": result["status"],
        "allowOrigin": result["headers"].get("access-control-allow-origin"),
    }
    result = request(gateway, "POST", "/v1/chat/completions",
                     [("Authorization", "Bearer secret"),
                      ("Origin", "https://denied.example"),
                      ("Content-Type", "application/json")], b"{}")
    fixture["cors"]["originDenied"] = {
        "status": result["status"],
        "body": result["body"],
        "allowOrigin": result["headers"].get("access-control-allow-origin"),
    }
    result = request(gateway, "POST", "/v1/chat/completions",
                     [("Authorization", "Bearer secret"),
                      ("Origin", "https://allowed.example"),
                      ("Content-Type", "application/json")], b"{}")
    fixture["cors"]["originAllowed"] = {
        "status": result["status"],
        "allowOrigin": result["headers"].get("access-control-allow-origin"),
    }


def _record_control(fixture, gateway):
    cases = [
        ("missing", [("Content-Type", "application/json")], b"{}"),
        ("wrong-token", [("X-Admin-Control", "wrong"),
                         ("Content-Type", "application/json")], b"{}"),
        ("origin-present", [("X-Admin-Control", "fixture-control-token"),
                            ("Origin", "https://allowed.example"),
                            ("Content-Type", "application/json")], b"{}"),
        ("host-foreign", [("X-Admin-Control", "fixture-control-token"),
                          ("Host", "example.com"),
                          ("Content-Type", "application/json")], b"{}"),
        ("valid", [("X-Admin-Control", "fixture-control-token"),
                   ("Content-Type", "application/json")],
         b'{"key_id":"friend"}'),
    ]
    close_result = None
    for name, headers, body in cases:
        result = request(gateway, "POST", "/admin/share/revoke",
                          headers, body)
        fixture["control"][name] = {
            "status": result["status"],
            "body": result["body"],
        }
        if name == "missing":
            close_result = result
    fixture["closeOnError"] = {
        "status": close_result["status"],
        "connection": close_result["headers"].get("connection"),
        "willClose": close_result["will_close"],
    }


if __name__ == "__main__":
    main()
