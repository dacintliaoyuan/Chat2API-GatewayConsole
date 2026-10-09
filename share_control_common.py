"""share_control_common.py — S0 minimal protected control channel.

Loopback-only (127.0.0.1, random/unmapped port) control channel prototype that
the management layer can use to request FIXED deployment actions from a broker.
It intentionally has no client-supplied paths, exes, ports, PIDs or envs: every
server side-effect path is a server-side constant. The real deployment will run
the broker under an admin-protected SYSTEM process and authorize by OS identity
on a Windows named pipe; that cross-user ACL gate is verified in the authorized
isolation experiment (this file proves the strict protocol + auth proof + the
store->projection->ack loop with a fake broker).

Protocol: exactly one JSON object per line.
  request:  {"v":1,"action":"...","request_id":"...","ts":<unix>,"fields":{...},"mac":"..."}
  response: {"v":1,"request_id":"...","ok":true|false,"error":null,"result":{...}}

Framing: line-delimited with a hard size bound; a single over-limit line or any
trailing data after the first newline is rejected. Replay: each request_id is
consumed exactly once and ts must be within a bounded window; the replay table
evicts ids by request validity (ts outside the window), never by chance.

Server reads are bounded by a per-connection deadline (R7-a): a client that
connects and never sends - and never half-closes - is dropped after the
deadline, so the single-threaded accept loop keeps serving control requests.

Actions (fixed whitelist):
  ping                -> {} (no fields)
  status              -> {}
  publish_projection  -> {} (path is server constant)
  start               -> {}   (uses server constants only in prototype)
  graceful_stop       -> {}
  emergency_stop      -> {}

Any unknown action, unknown/extra field, non-expected type, path-like string
(/, \\, :, C:), bad HMAC, stale ts or replayed request_id is rejected BEFORE the
broker acts.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import socket
import threading
import time
import uuid

VERSION = 1
MAX_LINE = 64 * 1024
TS_WINDOW_SECONDS = 300
SERVER_READ_TIMEOUT_SECONDS = 10
ACTIONS = {
    "ping": {},
    "status": {},
    "publish_projection": {},
    "start": {},
    "graceful_stop": {},
    "emergency_stop": {},
}

FORBIDDEN_PATH_HINTS = ("/", "\\", ":", "..", "C:")
SIGNED_KEYS = ("v", "action", "request_id", "ts", "fields")


def _mac(secret: bytes, payload: bytes) -> str:
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def build_request(secret: str, action: str, fields=None):
    body = {"v": VERSION, "action": action, "request_id": str(uuid.uuid4()),
            "ts": int(time.time()), "fields": fields or {}}
    body["mac"] = _mac(secret.encode(), json.dumps(body, sort_keys=True).encode())
    return body


def sign(secret: str, body: dict) -> dict:
    # Re-signing (e.g. after adjusting ts for a bounded replay window) must
    # exclude the PREVIOUS mac from the signing base, or the new mac can
    # never verify.
    body.pop("mac", None)
    body["mac"] = _mac(secret.encode(), json.dumps(body, sort_keys=True).encode())
    return body


class ControlError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def validate_request(body: dict, secret: str, now=None) -> dict:
    """Validate + authenticate; return normalized fields or raise ControlError."""
    if not isinstance(body, dict):
        raise ControlError(400, "request must be an object")
    if body.get("v") != VERSION:
        raise ControlError(400, "unsupported protocol version")
    action = body.get("action")
    if action not in ACTIONS:
        raise ControlError(400, "unknown action")
    ts = body.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, int):
        raise ControlError(400, "request must include integer ts")
    now = now if now is not None else int(time.time())
    if abs(int(ts) - now) > TS_WINDOW_SECONDS:
        raise ControlError(400, "request timestamp outside allowed window")
    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise ControlError(400, "invalid request_id")
    # authenticate BEFORE inspecting fields further (except action/id/ts)
    expected = _mac(secret.encode(), json.dumps(
        {k: body[k] for k in SIGNED_KEYS if k in body}, sort_keys=True).encode())
    supplied = body.get("mac", "")
    if not hmac.compare_digest(expected, supplied):
        raise ControlError(403, "forged or missing mac")
    fields = body.get("fields")
    if not isinstance(fields, dict):
        raise ControlError(400, "fields must be an object")
    allowed = ACTIONS[action]
    unknown = set(fields) - set(allowed)
    if unknown:
        raise ControlError(400, f"unknown field(s): {sorted(unknown)}")
    normalized = {}
    for key, expected_type in allowed.items():
        if key not in fields:
            raise ControlError(400, f"missing field: {key}")
        value = fields[key]
        if expected_type is int and not isinstance(value, int):
            raise ControlError(400, f"field {key} must be int")
        if isinstance(value, str) and any(h in value for h in FORBIDDEN_PATH_HINTS):
            raise ControlError(400, f"field {key} must not be path-like")
        normalized[key] = value
    return normalized


class ControlServer:
    """Minimal loopback control broker for S0; real broker will be SYSTEM
    named-pipe + OS ACL. Accepts exactly one line-delimited request per
    connection and rejects replay/trailing data. Every connection is subject
    to a bounded read deadline (R7-a), so a client that connects and never
    sends - and never half-closes - can never park the single-threaded accept
    loop."""

    def __init__(self, secret: str, on_action,
                 read_timeout: float = SERVER_READ_TIMEOUT_SECONDS,
                 max_seen: int = 20000):
        self.secret = secret
        self.on_action = on_action  # callable(action, fields) -> dict
        self.read_timeout = read_timeout
        self.max_seen = max_seen
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._seen_lock = threading.Lock()
        # request_id -> request ts; eviction is by ts validity, never random.
        self._seen = {}

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass

    def _read_one_request(self, conn, deadline):
        """Read exactly one bounded line before the TOTAL deadline; trailing
        bytes are rejected. The buffered reader is released explicitly.

        R7-deadline: a buffered ``readline`` resets the idle timeout on every
        underlying recv, so a client dripping bytes (never sending a newline)
        could hold the single accept loop far beyond the configured deadline
        even though it never passes HMAC. Read byte-by-byte instead and
        re-derive the remaining time from the monotonic deadline before EVERY
        recv — the deadline is a total-request bound, not an idle bound.
        """
        reader = conn.makefile("rb")
        try:
            chunks = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("request deadline exceeded")
                conn.settimeout(remaining)
                byte = reader.read(1)
                if not byte:
                    raise ControlError(400, "empty request")
                if byte == b"\n":
                    break
                chunks += byte
                if len(chunks) > MAX_LINE:
                    raise ControlError(400, "request line too long")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("request deadline exceeded")
            conn.settimeout(remaining)
            if reader.read(1):
                raise ControlError(400, "trailing data after request")
            return bytes(chunks) + b"\n"
        finally:
            reader.close()

    def _mark_seen(self, request_id, ts):
        """Consume a request_id exactly once (R7-a.2).

        Expired ids — ts no longer able to pass validation (outside
        TS_WINDOW_SECONDS, the same boundary validate_request enforces) — are
        reclaimed first. When the table is FULL of still-valid ids a new
        request is REFUSED instead of evicting an entry: an evicted in-window
        id would let its signed request be replayed.
        """
        with self._seen_lock:
            now = int(time.time())
            for rid, seen_ts in list(self._seen.items()):
                # 与 validate_request 同一过期定义：age > TS_WINDOW_SECONDS
                # 才回收——age 恰等于窗口的请求仍然有效（validate 用 `>`
                # 拒绝），其去重记录绝不能先过期（R7-a.2 端点一致）。
                if now - seen_ts > TS_WINDOW_SECONDS:
                    del self._seen[rid]
            if request_id in self._seen:
                raise ControlError(403, "replayed request_id")
            if len(self._seen) >= self.max_seen:
                raise ControlError(
                    503,
                    "replay table capacity exceeded; retry after the oldest "
                    "requests expire")
            self._seen[request_id] = int(ts)

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                request_id = ""
                deadline = time.monotonic() + self.read_timeout
                try:
                    line = self._read_one_request(conn, deadline)
                    body = json.loads(line.decode("utf-8"))
                    fields = validate_request(body, self.secret)
                    action = body["action"]
                    request_id = body.get("request_id", "")
                    self._mark_seen(request_id, body.get("ts"))
                    try:
                        result = self.on_action(action, fields) or {}
                        resp = {"v": VERSION, "request_id": request_id, "ok": True,
                                "error": None, "result": result}
                    except Exception as exc:  # noqa: BLE001
                        resp = {"v": VERSION, "request_id": request_id, "ok": False,
                                "error": f"{type(exc).__name__}: {exc}", "result": {}}
                    conn.sendall((json.dumps(resp) + "\n").encode())
                except ControlError as exc:
                    try:
                        conn.sendall((json.dumps(
                            {"v": VERSION, "request_id": request_id, "ok": False,
                             "error": f"HTTP-ish {exc.status}: {exc}",
                             "result": {}}) + "\n").encode())
                    except OSError:
                        pass
                except (socket.timeout, TimeoutError):
                    # Bounded read deadline hit (R7-a): drop this connection
                    # without a response and keep serving the next request.
                    pass
                except Exception:  # noqa: BLE001
                    # R7-a.1: a malformed JSON body, invalid UTF-8, a hostile
                    # field type or any other parse/transport error must NEVER
                    # escape the accept loop and kill the control thread. The
                    # error text is sanitized (no exception details leak);
                    # this connection is closed and the next request served.
                    try:
                        conn.sendall((json.dumps(
                            {"v": VERSION, "request_id": request_id, "ok": False,
                             "error": "invalid request", "result": {}})
                            + "\n").encode())
                    except OSError:
                        pass


class ControlClient:
    def __init__(self, port: int, secret: str):
        self.port = port
        self.secret = secret

    def call(self, action: str, fields=None):
        req = build_request(self.secret, action, fields)
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall((json.dumps(req) + "\n").encode())
            # Half-close the write side so the server's trailing-byte check sees
            # EOF after the single request line (no data -> no trailing data).
            s.shutdown(socket.SHUT_WR)
            reader = s.makefile("rb")
            data = reader.readline(MAX_LINE + 1)
        if not data or len(data) > MAX_LINE or not data.endswith(b"\n"):
            raise ControlError(502, "invalid or oversized control response")
        try:
            return json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise ControlError(502, "invalid control response") from exc
