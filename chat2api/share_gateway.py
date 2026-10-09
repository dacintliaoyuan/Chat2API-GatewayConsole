"""Small, opt-in UM sharing boundary. Only this listener may face a tunnel."""

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import os
import secrets
import socket
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "data" / "um-share.json"
ALLOWED = {("GET", "/v1/models"), ("POST", "/v1/chat/completions"),
           ("POST", "/v1/messages")}
# No Open WebUI server-side tools, pipelines or arbitrary upstream routes.
CHAT_FIELDS = {"model", "messages", "stream", "stream_options", "temperature",
               "top_p", "max_tokens", "max_completion_tokens", "stop", "seed",
               "presence_penalty", "frequency_penalty", "tools", "tool_choice",
               "parallel_tool_calls", "response_format", "reasoning_effort"}
# Anthropic Messages is a SEPARATE request contract, not an alias of chat: it gets
# its own whitelist (so an OpenAI-only field such as stream_options is refused
# instead of being forwarded) and its own required-field rule. Model ids stay the
# REAL upstream ids - the share invents no virtual alias or thinking level.
MESSAGES_PATH = "/v1/messages"
MESSAGES_FIELDS = {"model", "messages", "max_tokens", "system", "stream",
                   "temperature", "top_p", "top_k", "stop_sequences",
                   "metadata", "tools", "tool_choice", "thinking"}
# Deliberately NOT in ALLOWED: Anthropic files/batches and count_tokens. The
# dedicated backend serves no /v1/messages/count_tokens route (chat2api.py's POST
# dispatch ends at "Unknown path" for it), and the share never counts tokens
# itself, so those requests keep failing closed with 404 route_not_shared.

# Model-catalog freshness contract with chat2api (opt-in, neutral for other
# consumers): the share asks for the CURRENT upstream catalog, fresh, with no
# local effort/thinking virtual aliases appended. chat2api confirms with a
# response marker; an absent marker means the reply's freshness is unproven and
# the share fails closed instead of serving a stale or alias-polluted catalog.
# This replaces the earlier "-Fast/-Low/-Medium/-High suffix guessing", which
# could delete a real upstream model whose id happens to end with such a suffix.
X_MODELS_HEADER = "X-Chat2Api-Models"
X_MODELS_MARKER = "X-Chat2Api-Raw-Models"
RAW_FRESH_VALUE = "raw-fresh"

# Legacy effective limits (R6-A2): the old hardcodes stay the fallback for
# every projection/legacy config that does not carry the new fields, so a
# schema 1 projection behaves exactly as before (per-key 1, global 2).
LEGACY_KEY_MAX_CONCURRENCY = 1
LEGACY_SHARE_MAX_CONCURRENCY = 2
# The TCP connection gate scales WITH the business limit (never bottlenecks
# it) while keeping the slow-connection protection semantics: a bounded,
# non-blocking gate of share_max_concurrency + 8.
CONNECTION_GATE_HEADROOM = 8

# Upstream silence bounds, mirrored from the dedicated backend's own budgets
# (chat2api.py: STREAM_FIRST_BYTE_TIMEOUT_S=120 withholds the 200 until the
# first data event, STREAM_SOCKET_TIMEOUT_S=180 bounds ONE silent read, and
# every upstream request defaults to a 600s read cap). The share must OUTLAST
# those budgets so the backend's own answer — first data, its explicit 502, or
# the JSON body — always wins the race instead of the share cutting a healthy
# request. 2026-09-25 incident: a flat 60s bound cut every long
# GLM-5.3-Flash turn of a friend (the backend ledger shows
# client_disconnected at 60.3-60.5s while the thinking model stayed silent
# past it). There is deliberately NO total stream-duration cap: every received
# chunk restarts the silence bound, exactly like the backend.
STREAM_SOCKET_TIMEOUT_S = 180
NONSTREAM_SOCKET_TIMEOUT_S = 600

# ------------------------------------------------------ verified attribution (C2)
# The share proves WHICH key a forwarded request belongs to with a per-request
# HMAC over (key_id, request_id, ts). The marker is built AFTER the credential
# was authenticated and from the authenticated entry only - a client-supplied header
# of the same name is dropped like every other client header (the relay whitelist is
# the single gate, so the marker below is the only source of these headers).
#
# It carries NO credential material: the share Key's plaintext never leaves the
# client side, never reaches the dedicated backend and never enters a log line or a
# ledger row. The shared secret lives OUTSIDE the repository (`data/` is gitignored,
# see .gitignore `/ai-gateway/data/`) and is read once at startup. A missing,
# unreadable or too-short key file makes the share forward WITHOUT a marker; the
# backend then records the request as UNATTRIBUTED instead of guessing an owner.
#
# The key_id is carried base64url-encoded so ANY authenticated key id (the legacy
# format uses the human-chosen client name, which may be non-ASCII) fits one bounded
# ASCII header value without ever being rewritten or truncated.
ATTRIBUTION_VERSION = "1"
X_ATTRIBUTION_VERSION = "X-Share-Attribution-Version"
X_ATTRIBUTION_KEY_ID = "X-Share-Key-Id"
X_ATTRIBUTION_REQUEST_ID = "X-Share-Request-Id"
X_ATTRIBUTION_TS = "X-Share-Ts"
X_ATTRIBUTION_SIGNATURE = "X-Share-Signature"
ATTRIBUTION_HEADER_NAMES = (X_ATTRIBUTION_VERSION, X_ATTRIBUTION_KEY_ID,
                           X_ATTRIBUTION_REQUEST_ID, X_ATTRIBUTION_TS,
                           X_ATTRIBUTION_SIGNATURE)
# Bounds mirrored by the backend's verifier: the share refuses to attach a
# marker the backend would reject for a shape reason, so an out-of-bound key id
# stays an explicit "unattributed" fact instead of a silently dropped marker.
ATTRIBUTION_REQUEST_ID_BYTES = 16          # -> 32 hex characters
ATTRIBUTION_MAX_ENCODED_KEY_ID = 512
ATTRIBUTION_KEY_MIN_BYTES = 32
# Repository default: <ai-gateway>/data/share-attribution.key (gitignored).
# The deployed guard runs from a RunRoot copy, so the deployment must pass the
# SAME file explicitly (--attribution-key-file here, and the matching env var to
# the dedicated backend) - an unreadable file is not an error, it is the explicit
# "no attribution" state.
DEFAULT_ATTRIBUTION_KEY_FILE = (Path(__file__).resolve().parents[1]
                               / "data" / "share-attribution.key")
ATTRIBUTION_KEY_ENV = "SHARE_ATTRIBUTION_KEY_FILE"

# ------------------------------------------------------ bounded fault evidence (A9-6)
# Every classified failure of a shared request appends ONE bounded JSON line to the
# append-only fault log (when the deployment passes --fault-log) and to the
# process-local ring. The record is a FIXED key set of server-side facts: it
# carries NO key material, no UM credential, no cookie, no request body, no query
# string and no arbitrary upstream object. Client-controlled text (a model name, an
# upstream error sentence) is never a record field; the two text-shaped fields are
# sanitized to a bounded single-line token. Recovery never erases evidence: the log
# is append-only (rotation keeps the previous file as ``.1``) and no maintenance
# transition clears the ring.
FAULT_LOG_NAME = "share-faults.jsonl"
FAULT_LOG_MAX_BYTES = 256 * 1024          # 超出即 os.replace(path, path + ".1")
FAULT_RING_MAX = 64                        # 进程内有界 ring（只进 diagnostics 读面）
FAULT_PHASES = ("auth", "config", "body", "limit", "upstream", "stream")
FAULT_RECORD_KEYS = ("at", "phase", "status", "code", "upstream_status",
                     "exception_type", "duration_ms", "request_id",
                     "stream_started")
FAULT_LINE_MAX_BYTES = 512
FAULT_TOKEN_MAX_CHARS = 64


def _fault_token(value, limit=FAULT_TOKEN_MAX_CHARS):
    """Bound ONE record field to an ASCII single-line token.

    Only ``[A-Za-z0-9_-]`` survives and the result is truncated to ``limit``;
    anything else (a model name with dots, an upstream error sentence, a value with
    a newline) never becomes a record field. The record's text fields come from
    fixed internal codes anyway - this is the defense in depth for the case a caller
    passes something else.
    """
    if not isinstance(value, str) or not value:
        return None
    kept = "".join(ch for ch in value
                   if ch.isascii() and (ch.isalnum() or ch in "_-"))
    return kept[:limit] or None


def build_fault_record(*, phase, status, code, upstream_status=None,
                       exception_type=None, duration_ms=None,
                       request_id=None, stream_started=False, at=None):
    """Build ONE bounded fault record with exactly the frozen key set.

    ``status`` is the public status the caller sees (``None`` for an aborted
    stream, which never got one); ``code`` is the stable internal code; the two
    text-shaped fields pass through the bounded single-line sanitizer.
    """
    if phase not in FAULT_PHASES:
        raise ValueError("Invalid fault phase")
    record = {
        "at": round(float(time.time() if at is None else at), 3),
        "phase": phase,
        "status": (status if isinstance(status, int)
                   and not isinstance(status, bool) else None),
        "code": _fault_token(code),
        "upstream_status": (upstream_status
                            if isinstance(upstream_status, int)
                            and not isinstance(upstream_status, bool)
                            else None),
        "exception_type": _fault_token(exception_type),
        "duration_ms": (round(float(duration_ms), 1)
                        if isinstance(duration_ms, (int, float))
                        and not isinstance(duration_ms, bool) else None),
        "request_id": _fault_token(
            request_id, limit=ATTRIBUTION_REQUEST_ID_BYTES * 2),
        "stream_started": bool(stream_started),
    }
    if set(record) != set(FAULT_RECORD_KEYS):
        raise ValueError("Invalid fault record shape")
    return record


def fault_line(record):
    """Serialize ONE record to its bounded single-line JSONL bytes.

    Only the frozen key set is serialized: an extra caller-supplied key (a model
    name, a credential, a body fragment) can never reach the file.
    """
    projected = {key: record.get(key) for key in FAULT_RECORD_KEYS}
    line = (json.dumps(projected, ensure_ascii=True,
                       separators=(",", ":")) + "\n").encode("ascii")
    if len(line) > FAULT_LINE_MAX_BYTES:
        raise ValueError("Fault line exceeds the bounded size")
    return line


def append_fault(path, record):
    """Append ONE bounded line to the append-only fault log.

    When the new line would push the log past ``FAULT_LOG_MAX_BYTES`` the
    current file is rotated to ``<path>.1`` (``os.replace``, never truncated),
    so the previous evidence survives one rotation. Best-effort by contract: the
    evidence must never break the request path, so any write failure returns
    False (and the in-process ring still holds the record).
    """
    if path is None:
        return False
    try:
        line = fault_line(record)
        target = Path(path)
        try:
            size = target.stat().st_size
        except OSError:
            size = 0
        if size and size + len(line) > FAULT_LOG_MAX_BYTES:
            os.replace(str(target), str(target) + ".1")
        with open(target, "ab") as handle:
            handle.write(line)
        return True
    except (OSError, ValueError):
        return False


def attribution_key_path(override=None):
    """Resolve the shared attribution key path: CLI > env > repository default."""
    if override is not None:
        return Path(override)
    env = os.environ.get(ATTRIBUTION_KEY_ENV)
    if env:
        return Path(env)
    return DEFAULT_ATTRIBUTION_KEY_FILE


def load_attribution_key(path):
    """Read the shared attribution secret; ``None`` on ANY failure.

    The file holds ONE secret as text. Fail-closed: a missing, unreadable, empty
    or too-short file yields None, and the share then forwards without a marker
    instead of inventing an attribution. Never logged, never echoed.
    """
    try:
        secret = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError, ValueError):
        return None
    if len(secret) < ATTRIBUTION_KEY_MIN_BYTES:
        return None
    return secret


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def build_attribution(secret, key_id, now=None):
    """Build the marker headers for ONE forwarded request.

    Returns ``(headers, request_id)``; ``headers`` is EMPTY when the marker
    cannot be built (no usable secret, or a key id that cannot be carried as one
    bounded header value). The request is then forwarded without a marker and the
    backend records it as unattributed - the share never guesses an owner and never
    falls back to a client-supplied identity.
    """
    if not isinstance(secret, str) or len(secret) < ATTRIBUTION_KEY_MIN_BYTES:
        return {}, None
    if not isinstance(key_id, str) or not key_id:
        return {}, None
    try:
        encoded_key_id = _b64url(key_id.encode("utf-8"))
    except UnicodeError:
        return {}, None
    if len(encoded_key_id) > ATTRIBUTION_MAX_ENCODED_KEY_ID:
        return {}, None
    request_id = secrets.token_hex(ATTRIBUTION_REQUEST_ID_BYTES)
    ts = str(int(time.time() if now is None else now))
    signed = "\n".join((ATTRIBUTION_VERSION, encoded_key_id, request_id, ts))
    signature = hmac.new(secret.encode("utf-8"), signed.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    return ({X_ATTRIBUTION_VERSION: ATTRIBUTION_VERSION,
             X_ATTRIBUTION_KEY_ID: encoded_key_id,
             X_ATTRIBUTION_REQUEST_ID: request_id,
             X_ATTRIBUTION_TS: ts,
             X_ATTRIBUTION_SIGNATURE: signature}, request_id)


def load_clients(path):
    """Read the legacy name->digest config (used by the old CLI only).

    The HTTP boundary uses ``load_config`` instead, which also accepts the
    versioned key_id projection. This function intentionally remains a strict
    legacy reader so the old direct JSON manipulation cannot silently consume
    an unsupported future projection.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid legacy client configuration")
    clients = data.get("clients")
    if not isinstance(clients, dict):
        raise ValueError("Invalid client configuration")
    for name, digest in clients.items():
        if not isinstance(name, str) or not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("Invalid client entry")
        int(digest, 16)
    return clients


def _validate_digest(value):
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("Invalid digest")
    int(value, 16)


def load_config(path, auth_mode=None):
    """Load and validate the share authorization file.

    ``auth_mode`` is the DEPLOYMENT mode fixed at instance start (R35-2);
    the deployment path always passes it explicitly (the guard's
    ``--auth-mode``) and it is never mutated after construction:

    * ``"projection"``: SQLite is the single authority; the legacy
      ``{"clients": ...}`` digest format is rejected at EVERY config read
      (a runtime swap of the derived file to legacy clients can never be
      consumed as authorization), and the projection branch delegates FULL
      parsing to the ONE shared parser ``parse_projection_payload`` (R31-6:
      no consumer more lenient than the control-end ACK).
    * ``"legacy"``: the explicit not-yet-migrated digest config; a
      projection file is rejected so the mode - not the file - decides the
      format.
    * ``None``: historical auto-detection for direct offline callers
      (migration previews and offline tests); the deployment never relies
      on it.

    Legacy entries get ``key_id == name`` (no stable UUID in the old format),
    the legacy effective limits (per-key max_concurrency 1, global
    share_max_concurrency 2 = the old hardcodes) and generation 0. Projection
    entries carry max_concurrency and the payload may carry
    share_max_concurrency; schema 1 projections keep the legacy defaults for
    missing fields, schema 2 uses the real values. Unknown projection schema
    versions are rejected, never downgraded to the legacy/open interpretation.

    Returns a normalized dict with ``entries``
    (key_id, name, secret_hash, secret_version, enabled, max_concurrency),
    ``config_generation``, ``share_max_concurrency`` and ``schema_version``.
    """
    if auth_mode not in (None, "projection", "legacy"):
        raise ValueError("Invalid auth mode")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid sharing configuration")

    projection_markers = {"schema_version", "config_generation", "data"}
    if "clients" in data:
        if auth_mode == "projection":
            # R35-2: a legacy swap of the derived file is refused under
            # projection mode at EVERY auth read - never downgraded.
            raise ValueError("Legacy client configuration is not allowed "
                             "in projection mode")
        # Formats are mutually exclusive. A mixed object that carries unknown
        # schema/data together with clients must never be downgraded to legacy.
        if projection_markers & set(data.keys()):
            raise ValueError("Mixed sharing configuration format")
        if set(data.keys()) != {"clients"}:
            raise ValueError("Legacy configuration has unknown fields")
        legacy = data.get("clients")
        if not isinstance(legacy, dict):
            raise ValueError("Invalid sharing configuration")
        entries = []
        for name, digest in legacy.items():
            if not isinstance(name, str) or not name:
                raise ValueError("Invalid client name")
            _validate_digest(digest)
            entries.append({"key_id": name, "name": name, "secret_hash": digest,
                            "secret_version": 1, "enabled": True,
                            "max_concurrency": LEGACY_KEY_MAX_CONCURRENCY})
        return {"schema_version": 1, "config_generation": 0, "legacy": True,
                "share_max_concurrency": LEGACY_SHARE_MAX_CONCURRENCY,
                "entries": entries}

    if projection_markers & set(data.keys()):
        if auth_mode == "legacy":
            raise ValueError("Projection configuration is not allowed in "
                             "legacy mode")
        # R35-2: the projection branch reuses the ONE shared pure parser (the
        # same verdict as the writer pre-check and the control-end ACK); a
        # missing protocol module fails CLOSED instead of parsing locally.
        try:
            from share_broker_protocol import (ProtocolError,
                                               parse_projection_payload)
        except ImportError as exc:
            raise ValueError("Shared projection parser unavailable") from exc
        try:
            parsed = parse_projection_payload(data)
        except ProtocolError as exc:
            raise ValueError(str(exc)) from exc
        # The parser normalizes entries (max_concurrency always present) and
        # fills the legacy limits for missing fields, so schema 1 keeps the
        # old behavior and schema 2 carries the real values through.
        entries = [{"key_id": item["key_id"], "name": item["name"],
                    "secret_hash": item["secret_hash"],
                    "secret_version": item["secret_version"],
                    "enabled": item["enabled"],
                    "max_concurrency": item["max_concurrency"]}
                   for item in parsed["data"]]
        return {"schema_version": parsed["schema_version"],
                "config_generation": parsed["config_generation"],
                "share_max_concurrency": parsed["share_max_concurrency"],
                "legacy": False, "entries": entries}
    raise ValueError("Unsupported sharing configuration")


def valid_messages_request(data):
    """Decide whether an Anthropic Messages body may enter the shared relay.

    Shape-only admission, never a rewrite: an accepted body is forwarded
    unchanged, so this must not add, drop or normalize fields.

    * Unknown fields are refused (the share exposes Messages, not the whole
      Anthropic surface: files/batches/count_tokens stay unshared).
    * ``max_tokens`` is REQUIRED and must be a positive integer - Messages has
      no default, and the share must not invent one: filling it in would
      silently change the caller's request and its cost.
    * ``stream`` is optional but must be a bool when present.

    ``bool`` is an ``int`` subclass in Python, so it is excluded explicitly:
    ``max_tokens: true`` is not a positive integer.
    """
    if not isinstance(data, dict) or set(data) - MESSAGES_FIELDS:
        return False
    if not isinstance(data.get("model"), str) or not data["model"]:
        return False
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    max_tokens = data.get("max_tokens")
    if (not isinstance(max_tokens, int) or isinstance(max_tokens, bool)
            or max_tokens <= 0):
        return False
    return isinstance(data.get("stream", False), bool)


# The two accepted credential shapes and their shared bound. The bound applies to
# the raw header, exactly as the original Bearer-only rule did: a credential
# header longer than this is refused before any digest work.
BEARER_PREFIX = "Bearer "
API_KEY_HEADER = "X-Api-Key"
MAX_CREDENTIAL_HEADER = 256


def resolve_share_credential(headers, allow_api_key):
    """Resolve the ONE share credential from the accepted header shapes.

    ``Authorization: Bearer <key>`` is the original contract and stays valid on
    every shared route (Claude Code's ``ANTHROPIC_AUTH_TOKEN``, OpenAI-compatible
    clients). ``X-Api-Key: <key>`` is the Anthropic SDK's default ``api_key``
    shape; it is accepted on ``POST /v1/messages`` only (``allow_api_key``), so
    the OpenAI routes keep exactly their previous credential contract.

    Both shapes yield the SAME credential string, so the caller's digest lookup,
    enabled check, concurrency budget and ledger entry stay single - this adds a
    second header shape, never a second authorization path.

    Fail-closed, returning None (never a partial credential) when: a same-name
    header repeats, the credential is empty, the raw header exceeds
    ``MAX_CREDENTIAL_HEADER``, the Bearer prefix is malformed, or both shapes are
    present with DIFFERENT values. Two different credentials in one request are
    never resolved to either one of them.
    """
    auth = headers.get_all("Authorization", [])
    api_keys = headers.get_all(API_KEY_HEADER, []) if allow_api_key else []
    if len(auth) > 1 or len(api_keys) > 1:
        return None
    bearer = None
    if auth:
        header = auth[0]
        if not header.startswith(BEARER_PREFIX) or len(header) > MAX_CREDENTIAL_HEADER:
            return None
        bearer = header[len(BEARER_PREFIX):]
        if not bearer:
            return None
    api_key = None
    if api_keys:
        api_key = api_keys[0]
        if not api_key or len(api_key) > MAX_CREDENTIAL_HEADER:
            return None
    if bearer is None:
        return api_key
    if api_key is None:
        return bearer
    # Both shapes present: they must name the same key. ``compare_digest`` keeps
    # the comparison constant-time, like the digest lookup below; bytes avoid its
    # TypeError on non-ASCII values (those simply never match a real key).
    if not hmac.compare_digest(bearer.encode(), api_key.encode()):
        return None
    return bearer


class ShareServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, config, upstream_port=8000, probe=False,
                 auth_mode=None, attribution_secret=None, fault_log=None):
        # The default above is for IN-PROCESS wiring only (the offline test
        # harness constructs servers directly). The CLI entry point is the
        # enforcement point: main() REQUIRES an explicit --upstream-port
        # (R13-3) and refuses before this constructor runs, so no launched
        # share process can ever inherit a silent 8000. R13 review asked for
        # the in-process default to go as well; the 80 call sites in the test
        # harness are exactly that in-process wiring, so removing it is a larger
        # refactor than the low-severity finding justifies and is recorded as
        # deferred in the handoff instead.
        self.config = Path(config)
        # R35-2: the deployment mode is FIXED at construction (the guard
        # passes --auth-mode); no public API may change it afterwards, and
        # EVERY config read (initial validation and acquire-time re-read)
        # enforces it - a runtime swap to the other format cannot be consumed.
        self.auth_mode = auth_mode
        config_data = load_config(self.config, auth_mode=auth_mode)
        self.upstream_port = upstream_port
        self.probe = probe
        # Verified attribution (C2): the shared secret is read ONCE at startup
        # (see main()); a missing/unreadable file leaves it None, which makes
        # every forwarded request carry NO marker - the backend then records it
        # as unattributed. Never logged, never persisted.
        self.attribution_secret = attribution_secret
        # A9-6: the fault log path is fixed at construction; None keeps the
        # share silent on disk (the in-process ring still holds the evidence).
        # The ring is bounded and append-only - no maintenance transition
        # (drain/start/emergency) ever clears it.
        self.fault_log = Path(fault_log) if fault_log else None
        self.fault_ring = deque(maxlen=FAULT_RING_MAX)
        self.guard = threading.Lock()
        self.state = "running"      # running | draining | stopped
        self.last_seen_generation = 0
        self.instance_id = secrets.token_hex(16)   # 实例身份（ACK 绑定）
        # R6-A2: in-flight accounting is a per-key COUNTER (key_id -> count),
        # not a set: one key may carry up to its max_concurrency concurrent
        # requests, bounded by the global share_max_concurrency. The size is
        # read from the config the instance started with (legacy/schema-1
        # values fall back to the old hardcodes).
        self.share_max_concurrency = config_data["share_max_concurrency"]
        # R8-A7 revoke-and-terminate: per-key registry of LIVE request
        # contexts (key_id -> set of _InflightCtx). The context wraps the
        # client socket; terminating a key closes exactly ITS sockets, the
        # owning handler threads unwind through their own finally and the
        # precise release() decrement stays the single source of truth.
        self.inflight = {}
        # Startup per-key limits (key_id -> max_concurrency): plain acquires
        # (no generation/secret_version re-read) use the limits THIS
        # instance loaded, never the legacy fallback — a v2 key with
        # max_concurrency=8 admitted 8 parallel requests without a
        # per-request projection re-read.
        self.key_limits = {e["key_id"]: e.get("max_concurrency",
                                              LEGACY_KEY_MAX_CONCURRENCY)
                           for e in config_data["entries"]}
        self.active = {}
        self.history = {}
        # TCP connection gate: scales with the business limit (share_max + 8)
        # so a high per-key/global limit can never be silently bottlenecked by
        # the gate, while slow connections stay bounded.
        self.connections = threading.BoundedSemaphore(
            self.share_max_concurrency + CONNECTION_GATE_HEADROOM)
        super().__init__(address, ShareHandler)

    def process_request(self, request, address):
        if not self.connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.connections.release()

    def acquire(self, client, generation=None, secret_version=None,
                reason_out=None):
        """Atomically admit one request under the accept lock.

        Limits checked in this order (the FIRST exhausted limit wins and is
        reported via ``reason_out`` for a readable 429):

        * the key's own ``max_concurrency`` (per-key in-flight counter;
          schema 1 / legacy configs default to 1 = the old hardcode),
        * the global ``share_max_concurrency`` (sum of all per-key counters;
          legacy default 2 = the old hardcode),
        * the per-key 20/minute sliding window (UNTOUCHED by R6-A2).

        The projection is re-read INSIDE the lock when generation/
        secret_version are supplied, so a request that read an old snapshot
        before a revocation/rotation still cannot enter (R5-a/R5). Returns
        True when admitted; on refusal fills ``reason_out`` (a dict) with
        the readable limit identity without breaking the boolean contract.
        """
        with self.guard:
            if self.state != "running":
                if reason_out is not None:
                    reason_out["reason"] = "state_not_accepting"
                    reason_out["detail"] = (
                        "the share is draining or stopped and is not "
                        "accepting new requests")
                return False
            key_limit = None
            share_max = self.share_max_concurrency
            if generation is not None or secret_version is not None:
                # Re-read the projection INSIDE the accept lock so a request
                # that read an old snapshot before a revocation or rotation
                # still cannot enter after the change was confirmed. The
                # re-check verifies the entry this request ACTUALLY
                # authenticated against: same key_id, still enabled, and the
                # SAME secret_version — a rotation keeps the key_id but makes
                # the old secret stale (R5-a). The lock serializes acceptance;
                # the file re-read is the second verification.
                try:
                    fresh = load_config(self.config,
                                        auth_mode=self.auth_mode)
                except (OSError, ValueError, KeyError, TypeError):
                    return False
                if generation is not None and fresh["config_generation"] < generation:
                    return False
                entry = next((e for e in fresh["entries"]
                              if e["key_id"] == client), None)
                if entry is None or not entry["enabled"]:
                    return False
                if (secret_version is not None
                        and entry["secret_version"] != secret_version):
                    return False
                key_limit = entry["max_concurrency"]
                share_max = fresh["share_max_concurrency"]
            now = time.monotonic()
            # Bound retained identities across key revocations and rotations.
            self.history = {k: v for k, v in self.history.items() if v and v[-1] > now - 60}
            recent = self.history.setdefault(client, deque())
            while recent and recent[0] <= now - 60:
                recent.popleft()
            if key_limit is None:
                # No fresh re-read (plain acquire): use the limits THIS
                # instance loaded at startup; an unknown key falls back to
                # the per-key legacy default (1 = the old behavior).
                key_limit = self.key_limits.get(client,
                                                LEGACY_KEY_MAX_CONCURRENCY)
            if self.active.get(client, 0) >= key_limit:
                if reason_out is not None:
                    reason_out["reason"] = "per_key_concurrency"
                    reason_out["detail"] = (
                        f"per-key concurrency limit reached "
                        f"(max {key_limit} in-flight requests for this key)")
                return False
            if sum(self.active.values()) >= share_max:
                if reason_out is not None:
                    reason_out["reason"] = "share_concurrency"
                    reason_out["detail"] = (
                        f"global share concurrency limit reached "
                        f"(max {share_max} in-flight requests)")
                return False
            if len(recent) >= 20:
                if reason_out is not None:
                    reason_out["reason"] = "key_rate_limit"
                    reason_out["detail"] = (
                        "per-key rate limit reached (max 20 requests per "
                        "minute)")
                return False
            recent.append(now)
            self.active[client] = self.active.get(client, 0) + 1
            return True

    def register_inflight(self, client, ctx):
        with self.guard:
            self.inflight.setdefault(client, set()).add(ctx)

    def unregister_inflight(self, client, ctx):
        with self.guard:
            ctxs = self.inflight.get(client)
            if ctxs is not None:
                ctxs.discard(ctx)
                if not ctxs:
                    self.inflight.pop(client, None)

    def terminate_key(self, client):
        """R8-A7 revoke-and-terminate: close every LIVE connection held by
        ONE key. Socket shutdown only — the owning handler threads unwind
        through their own ``finally`` (release decrements there), so the
        in-flight counters never double-decrement. Other keys' contexts are
        not touched. Returns the number of connections closed."""
        with self.guard:
            return self._terminate_key_locked(client)

    def _terminate_key_locked(self, client):
        """Guard-HELD variant (the instance-control handler already owns
        the non-reentrant accept lock; a nested acquire would deadlock)."""
        closed = 0
        ctxs = self.inflight.pop(client, set())
        for ctx in ctxs:
            if ctx.terminate():
                closed += 1
        return closed

    def release(self, client):
        """Precisely decrement the key's in-flight counter (R6-A2).

        A request that finishes late must release exactly ITS reservation:
        the counter drops by one, and the key disappears only when its last
        in-flight request ends. Called from the request ``finally`` block —
        never earlier, so a still-running upstream occupancy is not freed
        before its owner request completes."""
        with self.guard:
            count = self.active.get(client, 0)
            if count <= 1:
                self.active.pop(client, None)
            else:
                self.active[client] = count - 1

    def note_config(self, cfg):
        """Record a projection generation, monotonically (never regress)."""
        with self.guard:
            generation = cfg.get("config_generation", 0)
            self.last_seen_generation = max(self.last_seen_generation, generation)

    # -------------------------------------------------- instance control endpoint
    # D1/D2: the share instance answers the SYSTEM broker on the INSTANCE
    # channel ({instance-status, load, emergency}). A `load` request installs the
    # projection the writer atomically placed at the fixed config path, and
    # the ACK is decided INSIDE the same guard that serializes acquire — a
    # stale snapshot can never be ACKed before it is refused (A4/A5). The
    # share has NO management ability on this channel (A2/A4 separation).
    def handle_instance_control(self, body, secret, now=None):
        if not hasattr(self, "instance_id"):
            self.instance_id = secrets.token_hex(16)
        try:
            from share_broker_protocol import (ProtocolError, ReplayRegistry,
                                               parse_projection_payload,
                                               validate_instance_request)
            if not hasattr(self, "_instance_replay"):
                # R29-3: instance-channel replay consumption — one-time
                # nonce per exchange, consumed BEFORE any side effect.
                self._instance_replay = ReplayRegistry()
            request = validate_instance_request(body, secret, now=now)
        except ImportError:
            return {"v": 2, "request_id": body.get("request_id", ""),
                    "ok": False, "error_code": "internal_error",
                    "result": {"detail": "protocol module unavailable"}}
        except ProtocolError as exc:
            # ProtocolError was previously unimported here - a malformed or
            # wrongly-signed instance request crashed with a NameError
            # instead of returning the structured refusal (fixed in R35-2).
            return {"v": 2, "request_id": body.get("request_id", ""),
                    "ok": False, "error_code": exc.code,
                    "result": {"detail": str(exc)}}
        action = request["action"]
        request_id = request["request_id"]
        if request["instance_id"] != self.instance_id:
            return {"v": 2, "request_id": request_id, "ok": False,
                    "error_code": "instance_mismatch",
                    "result": {"detail": "request targets another instance"}}
        try:
            self._instance_replay.consume(request_id, request["ts"], now=now)
        except ProtocolError as exc:
            return {"v": 2, "request_id": request_id, "ok": False,
                    "error_code": exc.code,
                    "result": {"detail": str(exc)}}
        try:
            with self.guard:
                if action == "instance-status":
                    result = {"instance_id": self.instance_id,
                              "state": self.state,
                              "active_count": sum(self.active.values()),
                              "last_seen_generation": self.last_seen_generation}
                    return {"v": 2, "request_id": request_id, "ok": True,
                            "error_code": "ok", "result": result}
                if action == "load":
                    if self.state == "stopped":
                        return {"v": 2, "request_id": request_id, "ok": False,
                                "error_code": "unconfirmed",
                                "result": {"detail": "instance is stopped"}}
                    generation = request["fields"]["generation"]
                    fingerprint = request["fields"]["fingerprint"]
                    # R29-4 single-snapshot rule: ONE read of the bytes;
                    # the semantic parse and the fingerprint come from the
                    # SAME bytes (shared parse_projection_payload), never a
                    # mixed pair of reads. The auth path re-reads per request
                    # and re-verifies — a load can never approve a file that
                    # the acquire-time read will not see.
                    try:
                        raw_text = self.config.read_text(encoding="utf-8")
                        payload = json.loads(raw_text)
                        parse_projection_payload(payload)
                    except (OSError, ValueError, KeyError, TypeError,
                            ProtocolError, UnicodeError):
                        # ProtocolError included (R35-2): a corrupt file or a
                        # legacy swap of the derived file must be a structured
                        # refusal here, never an escaping exception.
                        return {"v": 2, "request_id": request_id,
                                "ok": False,
                                "error_code": "fingerprint_mismatch",
                                "result": {"detail": "projection unreadable "
                                                     "or invalid"}}
                    if payload["config_generation"] != generation:
                        return {"v": 2, "request_id": request_id,
                                "ok": False,
                                "error_code": "stale_generation",
                                "result": {"detail": "loaded generation "
                                                     "differs"}}
                    import hashlib
                    canonical = json.dumps(payload, sort_keys=True)
                    actual_fp = hashlib.sha256(
                        canonical.encode("utf-8")).hexdigest()
                    if actual_fp != fingerprint:
                        return {"v": 2, "request_id": request_id,
                                "ok": False,
                                "error_code": "fingerprint_mismatch",
                                "result": {"detail": "loaded content "
                                                     "differs"}}
                    # Same-guard install/ACK decision. note_config itself
                    # re-acquires the non-reentrant guard, so the watermark
                    # is updated inline here instead of nesting the lock.
                    self.last_seen_generation = max(
                        self.last_seen_generation,
                        payload["config_generation"])
                    # R8-A7 revoke-and-terminate: the publish may carry a
                    # one-shot terminate_keys list (protocol-validated);
                    # closing exactly those keys' live connections interrupts
                    # their in-flight requests. Counting stays with the
                    # owning handlers' finally (release), never here.
                    # The ACK carries "terminated" ONLY when a terminate
                    # list arrived (the old-shape load reply keeps its exact
                    # contract); counting stays with the owning handlers'
                    # finally (release), never here.
                    listed = request["fields"].get("terminate_keys") or []
                    terminated = 0
                    for key_id in listed:
                        terminated += self._terminate_key_locked(key_id)
                    result = {"instance_id": self.instance_id,
                              "generation": generation,
                              "fingerprint": fingerprint,
                              "installed": True}
                    if listed:
                        result["terminated"] = terminated
                    return {"v": 2, "request_id": request_id, "ok": True,
                            "error_code": "ok", "result": result}
                if action == "drain":
                    # D2: refuse new requests; already-accepted requests
                    # keep running until they finish or hit the deadline.
                    # A9-3 terminal guard (contract §2.4): a stopped
                    # instance is NEVER flipped back to draining by a
                    # maintenance drain - it answers blocked and its state
                    # stays stopped. The running/draining behavior is
                    # unchanged.
                    if self.state == "stopped":
                        return {"v": 2, "request_id": request_id, "ok": False,
                                "error_code": "blocked",
                                "result": {"detail": "instance is stopped",
                                           "state": "stopped"}}
                    active = sum(self.active.values())
                    self.state = "draining"
                    return {"v": 2, "request_id": request_id, "ok": True,
                            "error_code": "ok",
                            "result": {"instance_id": self.instance_id,
                                       "state": self.state,
                                       "active_count": active}}
                if action == "resume":
                    # A9-3 resume acceptance (contract §2.4): the
                    # maintenance lease end / lazy expiry asks the drained
                    # instance to accept again. draining -> running (ok);
                    # running -> idempotent ok (already_running noted);
                    # stopped -> blocked - a terminal stop is NEVER
                    # re-opened by a maintenance lease. Unknown verbs are
                    # rejected earlier by validate_instance_request
                    # (bad_request / "unknown action"), so this branch
                    # covers exactly the three states above.
                    if self.state == "stopped":
                        return {"v": 2, "request_id": request_id, "ok": False,
                                "error_code": "blocked",
                                "result": {"detail": "instance is stopped",
                                           "state": "stopped"}}
                    active = sum(self.active.values())
                    result = {"instance_id": self.instance_id,
                              "state": "running",
                              "active_count": active,
                              "resumed": True}
                    if self.state == "running":
                        result["already_running"] = True
                    else:
                        self.state = "running"
                    return {"v": 2, "request_id": request_id, "ok": True,
                            "error_code": "ok", "result": result}
                if action == "emergency":
                    # D2: refuse new requests and clear the in-flight
                    # COUNTERS (R6-A2 accounting) — the host-level kill/
                    # verification stays with the broker.
                    cleared = sum(self.active.values())
                    self.state = "stopped"
                    self.active.clear()
                    return {"v": 2, "request_id": request_id, "ok": True,
                            "error_code": "ok",
                            "result": {"instance_id": self.instance_id,
                                       "state": self.state,
                                       "cleared_in_flight": cleared}}
                return {"v": 2, "request_id": request_id, "ok": False,
                        "error_code": "bad_request",
                        "result": {"detail": "unknown instance action"}}
        except Exception:  # noqa: BLE001
            return {"v": 2, "request_id": request_id, "ok": False,
                    "error_code": "internal_error", "result": {}}

    def start(self):
        with self.guard:
            self.state = "running"
        return self.snapshot()

    def begin_drain(self):
        """Refuse new requests; already-accepted requests may finish."""
        with self.guard:
            self.state = "draining"
            active = sum(self.active.values())
        return {"state": self.state, "active": active}

    def emergency_stop(self):
        """Fail closed: stop accepting and clear the in-flight counters.

        This is the HTTP-layer fail-closed signal. A real host emergency stop
        also removes the public Funnel mapping and stops the process; that is
        handled by the deployment layer, not by pretending a counter-clear is
        a host-wide kill.
        """
        with self.guard:
            self.state = "stopped"
            active = sum(self.active.values())
            self.active.clear()
        return {"state": self.state, "cleared_in_flight": active}

    def snapshot(self):
        with self.guard:
            return {
                "state": self.state,
                "active": dict(self.active),
                "active_count": sum(self.active.values()),
                "last_seen_generation": self.last_seen_generation,
            }

    def record_fault(self, record):
        """Append ONE classified fault to the bounded ring and the fault log.

        The ring is the in-process bounded snapshot (``FAULT_RING_MAX`` records,
        oldest evicted first); the log is the durable append-only one. Both keep
        the failure: a later success never clears either, and recovery (drain,
        start, emergency stop) never touches them.
        """
        self.fault_ring.append(record)
        if self.fault_log is not None:
            append_fault(self.fault_log, record)
        return record

    def faults_snapshot(self):
        """Bounded fault read surface (same shape as the broker's faults read).

        ``last_fault`` and ``counts_by_phase`` come from the bounded in-process
        ring (``FAULT_RING_MAX`` records); ``file`` carries the durable log's
        presence/size/mtime facts. A missing/unwritable log is honestly
        ``present: false`` - never a fabricated "no faults".
        """
        ring = list(self.fault_ring)
        counts = {phase: 0 for phase in FAULT_PHASES}
        for record in ring:
            phase = record.get("phase")
            if phase in counts:
                counts[phase] += 1
        file_info = {"present": False, "size": None, "mtime": None}
        if self.fault_log is not None:
            try:
                stat = self.fault_log.stat()
            except OSError:
                pass
            else:
                file_info = {"present": True, "size": stat.st_size,
                             "mtime": stat.st_mtime}
        return {"last_fault": ring[-1] if ring else None,
                "counts_by_phase": counts,
                "file": file_info}


class _InflightCtx:
    """One live share request's client-socket handle (R8-A7).

    ``terminate`` closes the socket ONCE (shutdown then close, both
    guarded); the owning handler observes the closure and unwinds through
    its own ``finally`` — the counter decrement happens exactly there, so
    termination and release never double-account."""

    def __init__(self, connection):
        self._connection = connection
        self._closed = False

    def terminate(self):
        if self._closed:
            return False
        self._closed = True
        try:
            self._connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._connection.close()
        except OSError:
            pass
        return True


class ShareHandler(BaseHTTPRequestHandler):
    server_version = "UMShare"
    sys_version = ""

    # Bounded discard for a rejected request body: every rejection above the body
    # read (auth, credential-header conflicts, encoding/size gates, per-key limits)
    # answers while the body is still in the socket, and closing such a connection
    # makes Windows answer the caller's next read with RST — an SDK client would
    # see a connection abort instead of this documented error. Read and discard at
    # most this many bytes, and never wait on a silent caller (the socket timeout
    # bounds that wait).
    _DISCARD_MAX_BYTES = 1 << 20
    _DISCARD_TIMEOUT_S = 2.0

    def setup(self):
        super().setup()
        self.connection.settimeout(15)
        self._body_consumed = False
        # A9-6: per-request fault evidence anchor (reset at handle_api).
        self._fault_started = None
        self._fault_request_id = None

    def log_message(self, *_args):
        # Never log request paths, bodies, credentials or upstream errors.
        pass

    def _record_fault(self, *, phase, status, code, upstream_status=None,
                      exception_type=None, stream_started=False,
                      request_id=None):
        """Record ONE classified fault with its bounded request evidence.

        ``duration_ms`` is measured from the request entry; ``request_id`` is the
        server-generated correlation id built after attribution (None before it).
        """
        started = getattr(self, "_fault_started", None)
        duration_ms = None
        if started is not None:
            duration_ms = (time.monotonic() - started) * 1000.0
        if request_id is None:
            request_id = getattr(self, "_fault_request_id", None)
        record = build_fault_record(
            phase=phase, status=status, code=code,
            upstream_status=upstream_status,
            exception_type=exception_type, duration_ms=duration_ms,
            request_id=request_id, stream_started=stream_started)
        return self.server.record_fault(record)

    def _discard_unread_body(self):
        """Consume a rejected request body before the reply goes out.

        No-op once the body has been read (a POST that reached the body read), so
        a rejection after it never pays the drain wait.
        """
        if self._body_consumed:
            return
        self._body_consumed = True
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            return
        try:
            remaining = int(lengths[0])
        except (TypeError, ValueError):
            return
        if remaining <= 0:
            return
        remaining = min(remaining, self._DISCARD_MAX_BYTES)
        try:
            self.connection.settimeout(self._DISCARD_TIMEOUT_S)
        except OSError:
            return
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.connection.settimeout(15)
            except OSError:
                pass

    def reply(self, status, data, *, request_id=None):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if request_id is None:
            request_id = getattr(self, "_fault_request_id", None)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        if request_id is not None:
            self.send_header(X_ATTRIBUTION_REQUEST_ID, request_id)
        self.end_headers()
        self.wfile.write(payload)

    def error(self, status, code, *, phase=None, request_id=None,
              upstream_status=None, exception_type=None,
              stream_started=False):
        """Fail ONE request: record its bounded evidence, then reply.

        ``phase`` is the frozen classification of the failure; a call without a
        phase (the pre-share 404 route rejection, which the frozen mapping does
        not classify) keeps the public behavior and records nothing. The
        correlation header is added only when a request id exists - a
        pre-attribution failure has none.
        """
        if request_id is None:
            request_id = getattr(self, "_fault_request_id", None)
        if phase is not None:
            self._record_fault(phase=phase, status=status, code=code,
                               upstream_status=upstream_status,
                               exception_type=exception_type,
                               stream_started=stream_started,
                               request_id=request_id)
        self._discard_unread_body()
        self.reply(status, {"error": {"message": code, "type": "share_gateway_error", "code": code}},
                   request_id=request_id)

    def limit_reached(self, reason, *, request_id=None):
        """429 keeps the stable ``sharing_limit_reached`` code and adds the
        readable identity of the FIRST exhausted limit (per-key concurrency,
        global share concurrency, per-minute rate or not-accepting state)."""
        reason_code = reason.get("reason", "sharing_limit_reached") \
            if isinstance(reason, dict) else "sharing_limit_reached"
        detail = reason.get("detail") if isinstance(reason, dict) else None
        if request_id is None:
            request_id = getattr(self, "_fault_request_id", None)
        self._record_fault(phase="limit", status=429,
                           code="sharing_limit_reached",
                           request_id=request_id)
        self._discard_unread_body()
        self.reply(429, {"error": {
            "message": "sharing_limit_reached",
            "type": "share_gateway_error",
            "code": "sharing_limit_reached",
            "reason": reason_code,
            "detail": detail or "sharing limit reached"}},
            request_id=request_id)

    def do_GET(self):
        self.handle_api()

    def do_POST(self):
        self.handle_api()

    def handle_api(self):
        self.close_connection = True
        # A9-6: this request's fault evidence anchor (duration + the
        # server-generated correlation id, set again after attribution).
        self._fault_started = time.monotonic()
        self._fault_request_id = None
        # Exact raw paths: reject query strings, encoded paths and absolute URLs.
        if (self.command, self.path) not in ALLOWED:
            self.error(404, "route_not_shared")
            return
        # X-Api-Key is a Messages-only alternative to the Bearer header; every
        # other shared route keeps the Bearer-only contract.
        credential = resolve_share_credential(
            self.headers,
            allow_api_key=self.command == "POST" and self.path == MESSAGES_PATH)
        if credential is None:
            self.error(401, "invalid_api_key", phase="auth")
            return
        try:
            # R35-2: the initial auth read enforces the deployment mode like
            # every other config read - a runtime swap of the derived file to
            # the legacy digest format cannot be consumed as authorization.
            cfg = load_config(self.server.config,
                              auth_mode=self.server.auth_mode)
        except (OSError, ValueError, KeyError, TypeError):
            self.error(503, "sharing_configuration_unavailable", phase="config")
            return
        digest = hashlib.sha256(credential.encode()).hexdigest()
        entry = next((e for e in cfg["entries"]
                      if e["enabled"] and hmac.compare_digest(e["secret_hash"], digest)), None)
        if entry is None:
            self.error(401, "invalid_api_key", phase="auth")
            return
        self.server.note_config(cfg)
        client = entry["key_id"]
        # Verified attribution (C2): built AFTER the credential was matched to
        # an enabled entry, so the key id is the AUTHENTICATED identity and
        # never a client-supplied header. A missing key material yields an
        # empty marker: the request is forwarded without attribution.
        attribution_headers, attribution_request_id = build_attribution(
            self.server.attribution_secret, client)
        # A9-6: keep the server-generated correlation id - the same value the
        # 8110 ledger row carries - for this request's replies and faults.
        self._fault_request_id = attribution_request_id
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding"):
            self.error(400, "unsupported_body_encoding", phase="body")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1:
            self.error(400, "invalid_content_length", phase="body")
            return
        try:
            length = int(lengths[0]) if lengths else 0
            if not 0 <= length <= 1024 * 1024:
                self.error(413, "request_too_large", phase="body")
                return
        except ValueError:
            self.error(400, "invalid_content_length", phase="body")
            return
        limit_reason = {}
        if not self.server.acquire(client, cfg["config_generation"],
                                   entry["secret_version"],
                                   reason_out=limit_reason):
            self.limit_reached(limit_reason)
            return
        inflight_ctx = _InflightCtx(self.connection)
        self.server.register_inflight(client, inflight_ctx)
        upstream = None
        started_response = False
        try:
            body = None
            if self.command == "POST":
                raw = self.rfile.read(length)
                self._body_consumed = True
                if len(raw) != length:
                    self.error(400, "incomplete_body", phase="body")
                    return
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeError):
                    self.error(400, "invalid_json", phase="body")
                    return
                if self.path == MESSAGES_PATH:
                    # Anthropic Messages uses the SAME admission chain as chat
                    # (key digest -> load_config -> size cap -> acquire) and
                    # only swaps the body contract. The path here is exact: the
                    # ALLOWED gate already rejected query strings/encodings.
                    if not valid_messages_request(data):
                        self.error(400, "unsupported_messages_request",
                                    phase="body")
                        return
                elif (not isinstance(data, dict) or set(data) - CHAT_FIELDS
                        or not isinstance(data.get("model"), str) or not data["model"]
                        or not isinstance(data.get("messages"), list) or not data["messages"]
                        or not isinstance(data.get("stream", False), bool)):
                    self.error(400, "unsupported_chat_request", phase="body")
                    return
                body = json.dumps(data).encode()
            elif length:
                self.error(400, "unexpected_body", phase="body")
                return
            if self.server.probe:
                if self.command == "GET":
                    self.reply(200, {"object": "list", "data": [{"id": "connectivity-test", "object": "model", "owned_by": "local-probe"}]})
                elif self.path == MESSAGES_PATH:
                    # Anthropic-shaped connectivity test. It carries NO usage
                    # block: real token counts are an upstream fact the share
                    # cannot know, so they are omitted rather than invented.
                    if data.get("stream"):
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Connection", "close")
                        if self._fault_request_id is not None:
                            self.send_header(X_ATTRIBUTION_REQUEST_ID,
                                              self._fault_request_id)
                        self.end_headers()
                        started_response = True
                        events = [
                            {"type": "message_start",
                             "message": {"id": "probe", "type": "message",
                                         "role": "assistant",
                                         "model": "connectivity-test",
                                         "content": [], "stop_reason": None,
                                         "stop_sequence": None}},
                            {"type": "content_block_start", "index": 0,
                             "content_block": {"type": "text", "text": ""}},
                        ]
                        for word in ["连接", "测试", "成功"]:
                            events.append({"type": "content_block_delta",
                                           "index": 0,
                                           "delta": {"type": "text_delta",
                                                     "text": word}})
                        events += [
                            {"type": "content_block_stop", "index": 0},
                            {"type": "message_delta",
                             "delta": {"stop_reason": "end_turn",
                                       "stop_sequence": None}},
                            {"type": "message_stop"},
                        ]
                        for event in events:
                            self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode())
                            self.wfile.flush()
                            if event["type"] == "content_block_delta":
                                time.sleep(0.2)
                    else:
                        self.reply(200, {"id": "probe", "type": "message",
                                         "role": "assistant",
                                         "model": "connectivity-test",
                                         "content": [{"type": "text",
                                                      "text": "连接测试成功；尚未接入上游。"}],
                                         "stop_reason": "end_turn",
                                         "stop_sequence": None})
                elif data.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    if self._fault_request_id is not None:
                        self.send_header(X_ATTRIBUTION_REQUEST_ID,
                                          self._fault_request_id)
                    self.end_headers()
                    started_response = True
                    for word in ["连接", "测试", "成功"]:
                        chunk = {"id": "probe", "object": "chat.completion.chunk", "model": "connectivity-test", "choices": [{"index": 0, "delta": {"content": word}, "finish_reason": None}]}
                        self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                        self.wfile.flush()
                        time.sleep(0.2)
                    self.wfile.write(b'data: [DONE]\n\n')
                else:
                    self.reply(200, {"id": "probe", "object": "chat.completion", "model": "connectivity-test", "choices": [{"index": 0, "message": {"role": "assistant", "content": "连接测试成功；尚未接入上游。"}, "finish_reason": "stop"}]})
                return
            if self.command == "GET":
                # /v1/models: ask chat2api for the CURRENT upstream UM catalog
                # (opt-in header), fresh by contract and without local virtual
                # aliases. chat2api confirms with a response marker; a missing
                # marker means freshness is unproven -> fail closed (never serve
                # a stale cache), and clear 502/503 on upstream failure.
                # The catalog is a non-stream control-plane call: the backend
                # answers it (or fails it) under its own 600s read cap, so the
                # share must not cut it earlier with a shorter bound.
                upstream = http.client.HTTPConnection(
                    "127.0.0.1", self.server.upstream_port,
                    timeout=NONSTREAM_SOCKET_TIMEOUT_S)
                # The attribution marker is ADDED to the forwarded request (the
                # whitelist below is the only source of relayed headers, so a
                # client-supplied header of the same name is dropped).
                model_headers = {"Connection": "close",
                                 X_MODELS_HEADER: RAW_FRESH_VALUE}
                model_headers.update(attribution_headers)
                upstream.request("GET", "/v1/models", headers=model_headers)
                response = upstream.getresponse()
                if response.status != 200:
                    self.error(503 if response.status == 503 else 502,
                                "um_unavailable", phase="upstream",
                                upstream_status=response.status)
                    return
                if (response.getheader(X_MODELS_MARKER, "") or "").strip() != "1":
                    self.error(502, "upstream_catalog_freshness_unconfirmed",
                                phase="upstream")
                    return
                raw = response.read()
                try:
                    payload = json.loads(raw)
                    data = payload.get("data")
                    if (not isinstance(data, list) or
                            any(not isinstance(m, dict) or
                                not isinstance(m.get("id"), str) for m in data)):
                        raise ValueError("model list payload is malformed")
                    payload["data"] = data
                except (ValueError, UnicodeError):
                    self.error(502, "unexpected_upstream_response",
                                phase="upstream")
                    return
                self.reply(200, payload)
                return
            # The upstream silence bound follows the request shape the backend
            # will use (both shapes validated above: ``stream`` is a bool when
            # present): a stream may stay silent between chunks for minutes
            # while a thinking model thinks, while a non-stream answer arrives
            # as one body under the backend's own 600s read cap.
            stream_flag = data.get("stream") is True
            upstream = http.client.HTTPConnection(
                "127.0.0.1", self.server.upstream_port,
                timeout=(STREAM_SOCKET_TIMEOUT_S if stream_flag
                         else NONSTREAM_SOCKET_TIMEOUT_S))
            # SHARED raw relay for chat AND Messages, with no per-shape
            # rewriting: the upstream method/path is replayed unchanged and the
            # response body is copied byte for byte. Anthropic SSE events
            # (message_start/content_block_delta/message_delta/message_stop) thus
            # reach the friend in their original shape and order - the share
            # never converts them to the OpenAI chat shape. A 200 response's
            # status and Content-Type pass through verbatim; an upstream non-200
            # collapses to 502/503 um_unavailable (the share's explicit-failure
            # contract, shared with chat: never a fabricated success).
            relay_headers = {"Content-Type": "application/json",
                             "Connection": "close"}
            # Verified attribution (C2): the marker replaces any client header of
            # the same name - this whitelist is rebuilt from scratch and is the
            # ONLY source of forwarded headers, exactly like the credential
            # headers above (never forwarded, never rewritten in place).
            relay_headers.update(attribution_headers)
            if self.path == MESSAGES_PATH:
                # Anthropic protocol selectors are part of the Messages request
                # contract: the dedicated backend passes them upstream when the
                # caller sent them, so dropping them here would change the
                # caller's request. Nothing else is forwarded - keys, cookies
                # and every other client header still stay client-side.
                for name in ("Anthropic-Version", "Anthropic-Beta"):
                    value = self.headers.get(name)
                    if (value and len(value) <= 200
                            and not any(ch in value for ch in "\r\n")):
                        relay_headers[name] = value
            upstream.request(self.command, self.path, body=body, headers=relay_headers)
            response = upstream.getresponse()
            if response.status != 200:
                self.error(503 if response.status == 503 else 502,
                            "um_unavailable", phase="upstream",
                            upstream_status=response.status)
                return
            content_type = response.getheader("Content-Type", "").split(";")[0].lower()
            if content_type not in {"application/json", "text/event-stream"}:
                self.error(502, "unexpected_upstream_response",
                            phase="upstream")
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            if self._fault_request_id is not None:
                self.send_header(X_ATTRIBUTION_REQUEST_ID,
                                  self._fault_request_id)
            self.end_headers()
            started_response = True
            while True:
                chunk = response.read1(16384)
                if not chunk:
                    return
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException) as exc:
            if not started_response:
                # Connection failure / response timeout before the response:
                # the exception class name is the bounded evidence, never the
                # exception text (which may embed an upstream object).
                try:
                    self.error(502, "um_connection_failed", phase="upstream",
                                exception_type=type(exc).__name__,
                                stream_started=False)
                except OSError:
                    pass  # The caller has already disconnected.
            else:
                # A started stream is aborted, never given a fabricated success
                # marker; the abort itself is the recorded fault (status null).
                self._record_fault(phase="stream", status=None,
                                   code="stream_aborted",
                                   exception_type=type(exc).__name__,
                                   stream_started=True)
        finally:
            self.server.unregister_inflight(client, inflight_ctx)
            if upstream is not None:
                upstream.close()
            # Precise release (R6-A2): decrement THIS request's reservation;
            # a key with several in-flight requests keeps its counter until
            # the last one ends.
            self.server.release(client)


def main(argv=None):
    parser = argparse.ArgumentParser(description="上游熟人分享入口（仅本机监听）")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--upstream-port", type=int, default=None,
                        help="本机上游 chat2api 端口；必填、无默认值——分享入口"
                             "必须显式声明专用分享后端端口（生产 8110，由 "
                             "deploy-elevated.ps1 的 ShareBackendPort 传入），"
                             "绝不静默落到私人 chat2api 的 8000")
    parser.add_argument("--probe", action="store_true", help="只返回连通性测试内容，不调用上游")
    parser.add_argument("--fault-log", type=Path, default=None,
                        help="故障留证文件（有界单行 JSONL，仅追加；超出上限轮转"
                             "为 <路径>.1）。缺省不写文件，仅进程内有界 ring")
    parser.add_argument("--auth-mode", choices=("projection", "legacy"),
                        default=None,
                        help="deployment mode fixed at start (R35-2); the "
                             "guard always passes it explicitly")
    parser.add_argument("--add-client", help="生成朋友密钥；同名操作会轮换旧密钥")
    parser.add_argument("--revoke-client", help="撤销朋友密钥，对后续请求立即生效")
    parser.add_argument("--instance-key-file", type=Path, default=None,
                        help="实例控制通道共享密钥；提供后启用实例管道服务")
    parser.add_argument("--attribution-key-file", type=Path, default=None,
                        help="请求归因共享密钥（与专用后端 8110 同一文件）；"
                             "缺省按 " + ATTRIBUTION_KEY_ENV + " 环境变量、"
                             "再退回仓库 data/ 下的默认路径。密钥不可读时分享"
                             "照常转发但不附归因标记，后端记为未归属")
    parser.add_argument("--instance-pipe-prefix",
                        default=r"\\.\pipe\um-share-public-instance-",
                        help="实例管道名前缀（实验部署必须传独立前缀）")
    parser.add_argument("--instance-pipe-sddl", default=None,
                        help="实例管道 DACL（SDDL）；实验装配必须显式提供，"
                             "占位 SID 转换失败即拒绝服务（绝不使用默认安全描述符）")
    parser.add_argument("--registration-file", type=Path, default=None,
                        help="实例自发布登记文件（run\\live\\instance-"
                             "registration.json）；启用实例通道时必填")
    args = parser.parse_args(argv)
    if args.add_client or args.revoke_client:
        # Removed as a second writer: direct JSON mutation would bypass the
        # single-authority management store. Operators must use share_admin.py
        # against the management service (see R4).
        print("ERROR: --add-client/--revoke-client no longer mutate config "
              "directly. Use share_admin.py against the single-authority "
              "management service: create with an existing name is a 409 "
              "conflict, and rotation is an explicit `rotate` operation.",
              file=sys.stderr, flush=True)
        raise SystemExit(2)
    if args.upstream_port is None:
        # Fail-closed (R13-3): the share entry MUST name its dedicated share
        # backend explicitly. There is NO default - a silent 8000 would forward
        # every friend request into the PRIVATE chat2api instance, which is
        # exactly the boundary the dedicated-backend deployment exists to keep.
        # The refusal happens BEFORE ShareServer is constructed (no bind, no
        # listen, no forward).
        parser.error(
            "--upstream-port is required: name the dedicated share-backend "
            "port explicitly (production: 8110, passed by deploy-elevated.ps1 "
            "ShareBackendPort). There is no default - the private chat2api "
            "port 8000 is never used implicitly.")
    if args.instance_key_file is not None and args.registration_file is None:
        parser.error("--registration-file is required with --instance-key-file")
    # Verified attribution (C2): the shared secret is read ONCE at startup.
    # An unreadable file is NOT an error - the share keeps serving without
    # markers (the backend records every request as unattributed).
    attribution_secret = load_attribution_key(
        attribution_key_path(args.attribution_key_file))
    server = ShareServer(("127.0.0.1", args.port), args.config,
                         upstream_port=args.upstream_port,
                         probe=args.probe, auth_mode=args.auth_mode,
                         attribution_secret=attribution_secret,
                         fault_log=args.fault_log)
    instance_channel = None
    if args.instance_key_file is not None:
        # R41-1: the instance channel NEVER takes the HTTP share down. A
        # listener failure (name taken / DACL refused / kernel error) keeps
        # the share serving WITHOUT a registration file — the broker then
        # serves status-only and every publish reports unconfirmed (an
        # absent registration is the explicit not-ready fact).
        try:
            from share_instance_pipe import (start_instance_channel,
                                             remove_registration)
            instance_channel = start_instance_channel(
                server, args.instance_key_file.read_text(
                    encoding="utf-8").strip(),
                args.instance_pipe_prefix, args.registration_file,
                sddl=args.instance_pipe_sddl)
        except ImportError as exc:
            instance_channel = {"ready": False,
                                "error": f"instance channel module "
                                         f"unavailable: {exc}"}
        except OSError as exc:
            instance_channel = {"ready": False,
                                "error": f"instance key unreadable: "
                                         f"{type(exc).__name__}"}
        if instance_channel.get("ready"):
            print(f"实例控制通道已就绪：{instance_channel['pipe_name']}",
                  flush=True)
        else:
            print(f"实例控制通道未就绪：{instance_channel.get('error')}；"
                  f"分享主服务不受影响，登记文件未写入。",
                  file=sys.stderr, flush=True)
    # The effective upstream port is part of the startup evidence: without it
    # an operator cannot tell from the running process which backend the share
    # actually forwards to (the R13-3 defect survived exactly because the
    # 8000 default was invisible). No secrets are printed here.
    print(f"上游分享入口 http://127.0.0.1:{args.port}；上游后端 "
          f"http://127.0.0.1:{args.upstream_port}；模式："
          f"{'连通性测试' if args.probe else '上游转发'}；请求归因："
          f"{'已启用' if attribution_secret else '未启用（共享密钥不可读，后端记为未归属）'}；"
          f"Ctrl+C 关闭。",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if instance_channel is not None:
            channel_server = instance_channel.get("server")
            if channel_server is not None:
                channel_server.stop()
            if instance_channel.get("ready") and args.registration_file:
                # Shutdown unpublishes the registration: a stale file must
                # never name an instance that no longer exists.
                remove_registration(args.registration_file)
                print("实例登记已撤除。", flush=True)


if __name__ == "__main__":
    main()
