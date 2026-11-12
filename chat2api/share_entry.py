"""Legacy two-process share gateway (test-only).

Production no longer launches this server: the merged 8100 process
(``chat2api.py``) authenticates the share key in-process. This module is
kept for the legacy Windows chain and its offline tests.
"""

import argparse
import base64
import hashlib
import hmac
import http.client
import itertools
import json
import os
import queue
import secrets
import socket
import sys
import tempfile
import threading
import time
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The broker runs this file as a script (``python chat2api/share_entry.py``),
# so sys.path[0] is ``chat2api/``; the bundled ``shared`` package is a sibling
# of that directory and needs the package root on the path.
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))

# Re-exported for existing callers (chat2api, tests, the broker) so the pure
# translations keep their single implementation in ``shared``. The full surface is
# kept on purpose: ``test_r19_shared_responses`` pins every re-exported name, so
# narrowing this list is a contract change, not a cleanup.
from shared.responses_protocol import (  # noqa: E402
    MESSAGES_FIELDS,
    RESPONSES_FIELDS,
    _anthropic_tool_input,
    _chat_usage,
    _messages_usage,
    _responses_chat_content,
    _responses_image_block,
    _responses_output_parts,
    _responses_text,
    _responses_text_blocks,
    _responses_tool_result_blocks,
    _sse_lines,
    chat_to_responses_response,
    messages_stream_events,
    messages_to_responses_response,
    responses_requires_messages,
    responses_stream_events,
    responses_to_chat_messages,
    responses_to_chat_request,
    responses_to_chat_tool_choice,
    responses_to_chat_tools,
    responses_to_messages_request,
    responses_to_messages_tool_choice,
    valid_responses_request,
)


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "data" / "um-share.json"
RESPONSES_PATH = "/v1/responses"
ALLOWED = {("GET", "/v1/models"), ("POST", "/v1/chat/completions"),
           ("POST", "/v1/messages"), ("POST", RESPONSES_PATH)}
# A definite upstream retry hint is relayed as an integer number of seconds,
# bounded by the local login rate-limit window (chat2api uses the same 900s
# bound). Anything else - HTTP-date, fractional, zero/negative, oversized - is
# dropped: the share never fabricates a retry time.
RETRY_AFTER_MAX_S = 900
# Review #13: the share backend's retryable dispatch refusal carries a
# definite whole-second hint (same contract as chat2api's 429).
SHARE_DISPATCH_RETRY_AFTER_S = 1
# No Open WebUI server-side tools, pipelines or arbitrary upstream routes.
CHAT_FIELDS = {"model", "messages", "stream", "stream_options", "temperature",
               "top_p", "max_tokens", "max_completion_tokens", "stop", "seed",
               "presence_penalty", "frequency_penalty", "tools", "tool_choice",
               "parallel_tool_calls", "response_format", "reasoning_effort", "store"}
# Anthropic Messages is a SEPARATE request contract, not an alias of chat: it gets
# its own whitelist (so an OpenAI-only field such as stream_options is refused
# instead of being forwarded) and its own required-field rule. Model ids stay the
# REAL upstream ids - the share invents no virtual alias or thinking level.
MESSAGES_PATH = "/v1/messages"
# Review #13: the 8110 share backend refuses a request that never reached the
# upstream with a retryable 429 (revoked/timeout) or 503 (revoke state
# unavailable).  The 8100 relays ONLY these bounded error types (never the
# upstream body) so the terminal sees the documented status, Retry-After and type
# instead of a bare connection close.
SHARE_DISPATCH_ERROR_TYPES = frozenset({
    "share_dispatch_timeout", "share_dispatch_revoked",
    "share_dispatch_state_unavailable"})
SHARE_DISPATCH_REFUSAL_MAX_BYTES = 8192


def model_catalog_limit(path):
    """Accept only Harness's bounded catalog query, never arbitrary queries.

    The query is consumed locally; only canonical /v1/models reaches chat2api.
    Harness's Anthropic SDK appends ``beta=true`` and the catalog probe appends
    one ``limit``; both are recognized by name, never by position.
    """
    if path == "/v1/models":
        return None
    prefix = "/v1/models?"
    if not path.startswith(prefix):
        raise ValueError("not a shared catalog path")
    query = path[len(prefix):]
    if not query:
        raise ValueError("empty catalog query")
    limit = None
    seen = set()
    for part in query.split("&"):
        name, separator, value = part.partition("=")
        if not separator or not name or name in seen:
            raise ValueError("invalid catalog query")
        seen.add(name)
        if name == "beta" and value == "true":
            continue
        if name != "limit":
            raise ValueError("invalid catalog query")
        if not value.isascii() or not value.isdecimal() \
                or not 1 <= len(value) <= 4:
            raise ValueError("invalid catalog limit")
        limit = int(value)
        if not 1 <= limit <= 1000 or str(limit) != value:
            raise ValueError("invalid catalog limit")
    return limit


def messages_query(path):
    """Accept the SDK's beta query on Messages, never an arbitrary one."""
    if path in (MESSAGES_PATH, MESSAGES_PATH + "?beta=true"):
        return
    raise ValueError("not a shared Messages path")


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
# non-blocking gate of share_max_concurrency + 8. The global bound itself is
# owned by the ONE shared projection parser (share_broker_protocol): the gate
# is only ever installed from a projection that parser already accepted, so
# this module keeps no second copy of the 128 bound.
CONNECTION_GATE_HEADROOM = 8


class _ConnectionGate:
    """Adjustable, non-blocking TCP connection gate (accept boundary only).

    The gate OBJECT identity never changes: every acquire/release pair stays
    on the same owner across a limit change, so a connection admitted under
    an older limit releases exactly its own slot (replacing the object would
    make a late ``finally`` release into a fresh counter). Raising the limit
    admits more new connections; lowering it only refuses NEW connections
    above the new bound while in-flight connections keep their slots until
    they finish.
    """

    def __init__(self, limit):
        self._lock = threading.Lock()
        self._limit = int(limit)
        self._active = 0

    @property
    def limit(self):
        with self._lock:
            return self._limit

    @property
    def active(self):
        with self._lock:
            return self._active

    def set_limit(self, limit):
        with self._lock:
            self._limit = int(limit)

    def acquire(self):
        with self._lock:
            if self._active >= self._limit:
                return False
            self._active += 1
            return True

    def release(self):
        with self._lock:
            # A release always belongs to an acquire; clamping at zero keeps a
            # double release from inventing capacity.
            if self._active > 0:
                self._active -= 1


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


def retry_after_seconds(value):
    """Validated integer Retry-After seconds, or None.

    The share relays ONLY a definite upstream retry suggestion, and only in the
    wire form every HTTP client understands: a positive whole number of seconds
    no larger than ``RETRY_AFTER_MAX_S``. A header that is an HTTP-date,
    fractional, zero, negative, oversized or non-numeric is dropped - the share
    never converts one into a made-up delay.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not value or not value.isascii() or not value.isdigit():
            return None
        try:
            value = int(value)
        except ValueError:
            return None
    elif not isinstance(value, int):
        return None
    if value < 1 or value > RETRY_AFTER_MAX_S:
        return None
    return value


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
    instead of inventing an attribution. On POSIX the 0600/current-uid gate is
    applied too (Q11/secret boundary); a foreign-owned or wider file is treated
    as unavailable, never read. Never logged, never echoed.
    """
    try:
        secret_module = _load_share_secret_protection()
        secret = secret_module.read_private_secret_path(
            path, max_bytes=1024, label="attribution key").decode(
                "utf-8").strip()
    except (OSError, UnicodeError, ValueError, ImportError, AttributeError):
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


# ------------------------------------------------- revoke notice (R-C5)
# The share entry point (8100) owns the projection file, so it is the only
# process that can observe "this Key became disabled/deleted at generation N".
# The dedicated backend (8110) owns the pre-send tickets, and the 8100 handlers
# can stay blocked in upstream.getresponse() after a revoke closed the CLIENT
# socket - the 8100->8110 socket then never sees EOF, so 8110 cannot release
# the Key's PENDING/GRANTED ticket by itself.  The 8100 therefore sends ONE
# loopback-only signed notice when an APPLIED projection generation disables or
# removes a Key.  The notice carries the stable key_id (never the plaintext Key),
# the applied generation, a timestamp and a one-time nonce; the SAME shared
# attribution secret signs it with the SAME canonical encoding style as the
# per-request attribution marker.  A notice failure never rolls back the
# projection and never changes the load ACK; the bounded retry and the fault
# record are diagnostics, not a fabricated success.
REVOKE_NOTICE_VERSION = "1"
REVOKE_NOTICE_PATH = "/admin/share/revoke"
REVOKE_NOTICE_TS_WINDOW_S = 300
REVOKE_NOTICE_NONCE_CHARS = 32
REVOKE_NOTICE_SEEN_MAX = 20000
# Two signed transition kinds share ONE wire format: a revoke (disabled/deleted)
# closes the Key's pending tickets, while a re-enable only advances the per-Key
# generation watermark so a LATE older revoke cannot close the re-enabled Key's
# newer tickets.  Both are signed, so an unsigned flip is impossible.
REVOKE_NOTICE_ACTIONS = ("revoke", "reenable")
# A local backend answers immediately.  A down/hung backend must not hold the
# 8100 load ACK longer than these bounds; after them the existing cumulative
# dispatch budget remains the only fallback for a PENDING ticket.
REVOKE_NOTICE_TIMEOUT_S = 0.75
REVOKE_NOTICE_ATTEMPTS = 3
REVOKE_NOTICE_RETRY_DELAY_S = 0.1
# Review #13: one slow Key must not serialize the whole batch.  Every notice is
# sent through a BOUNDED worker pool, and the WHOLE batch has ONE deadline.  A
# backend that is slow for many keys then costs at most this bound, after which
# the load ACK proceeds and the unfinished notices are recorded as bounded
# diagnostics.
REVOKE_NOTICE_BATCH_DEADLINE_S = 2.5
# Review #10: a batch of N disabled Keys must never create N daemon threads and
# N loopback connections.  At most this many notices are in flight; the rest wait
# in a bounded FIFO queue and are abandoned at the batch deadline.
REVOKE_NOTICE_MAX_CONCURRENCY = 4
# Review #10: the worker bound is PROCESS-WIDE, not per batch.  A previous
# batch whose workers are still finishing must not let the next batch create
# another N threads/loopback connections; the semaphore is the cross-batch cap.
_revoke_notice_worker_slots = threading.BoundedSemaphore(
    REVOKE_NOTICE_MAX_CONCURRENCY)

_B64URL_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
# Revoke state must survive a process restart: the replay registry and the
# per-Key applied generation are written to ONE bounded state file before the
# notice is ACKed.  The file holds NO secret, NO plaintext Key and NO
# credential - only nonces (already public in the signed notice) and the stable
# key_id/generation/application time.
#
# The broker unit runs the 8100 child with ProtectSystem=strict and only
# /var/lib/um-share, /run/um-share and /var/log/um-share writable, so the
# release directory (/opt/um-share/current) is READ-ONLY.  A state file under
# the release therefore cannot be created or replaced: the first revoke write
# fails, the load ACK carries no applied watermark and the management side keeps
# the security journal generation pending (a production Key delete answers 503).
# The default is the persistent state directory that StateDirectory=um-share
# already creates; CHAT2API_SHARE_REVOKE_STATE_FILE overrides it explicitly.
REVOKE_STATE_ENV = "CHAT2API_SHARE_REVOKE_STATE_FILE"
DEFAULT_REVOKE_STATE_DIR = Path("/var/lib/um-share")
DEFAULT_REVOKE_STATE_FILE = DEFAULT_REVOKE_STATE_DIR / "share-revoke.state"
# The pre-fix default lived INSIDE the release (read-only under the broker
# unit).  It is never written again, and never modified or deleted: a file found
# there is adopted ONCE at the first initialization of the deployment default so
# an upgrade cannot silently drop the pending generations it still carries.
# The resolved path is the CURRENT release only; `_legacy_revoke_state_candidates`
# also lists the sibling release directories (/opt/um-share/releases/*), because an
# upgrade switches `current` to a NEW release whose own data directory is empty
# while the generation that must still be ACKed lives in the release that ran
# before the switch.
LEGACY_REVOKE_STATE_FILE = (Path(__file__).resolve().parents[1]
                            / "data" / "share-revoke.state")
REVOKE_LEGACY_ADOPTED_SUFFIX = ".legacy-adopted"
REVOKE_STATE_VERSION = 1
REVOKE_STATE_MAX_BYTES = 1024 * 1024
# Review #14: applied generations are evicted by TIME, never by insertion
# order.  A notice can only pass the timestamp window for this long, so an entry
# older than the window can never be needed again; within the window the stored
# generation stays absolute.
REVOKE_STATE_GENERATIONS_TTL_S = REVOKE_NOTICE_TS_WINDOW_S
# Review #11/#15: the applied projection snapshot (key_id -> enabled plus the
# generation) is persisted beside the projection config, which in the default
# deployment is the writable /var/lib/um-share/run directory (not the read-only
# release).  Only the current projection's Keys and a bounded number of recently
# removed Keys are retained, so the snapshot cannot grow with Key churn.
REVOKE_PROJECTION_VERSION = 1
REVOKE_PROJECTION_MAX_BYTES = 1024 * 1024
REVOKE_PROJECTION_KNOWN_MAX = 4096
# Review #12: the sibling marker proves a trusted first initialization happened.
# It is tiny, private (0600) and carries the same schema version as the state.
REVOKE_INIT_MARKER_MAX_BYTES = 4096
_revoke_notice_seen = {}
_revoke_applied_generations = {}
# The newest APPLIED projection generation.  Every revoke/rotate/limit_lower
# intent at or below it is applied because the projection rendered after the
# intent's own transaction carries the change; it is persisted so a restart ACKs
# the generation it already applied even before the next load.
_revoke_projection_generation = 0
_revoke_state_lock = threading.RLock()
_revoke_state_loaded = False
# A read-only merge found a genuinely absent state (no file, no initialization
# marker).  Repeated status polls must not re-scan the release directories; the
# first real write clears this flag and initializes the state.
_revoke_state_absent = False
# The verified notice's own timestamp, on THIS handler thread.  It is the
# admission time of a request that 8100 forwarded (or was about to forward)
# before the notice; 8110 compares it with the persisted applied_at so only a
# request admitted before the revoke is refused, while a re-enabled Key's later
# request is not blocked forever.
_revoke_notice_facts = threading.local()


def _verified_revoke_notice_ts():
    return getattr(_revoke_notice_facts, "ts", None)


def _verified_revoke_notice_action():
    return getattr(_revoke_notice_facts, "action", None)


def revoke_state_path():
    """The persisted revoke state path: env override, else a writable state dir.

    The default never points inside the release directory: the broker unit keeps
    the release read-only (ProtectSystem=strict) and only /var/lib/um-share is
    writable.  A non-POSIX development checkout keeps the release-relative data
    directory, which is writable there and is never a deployment target.
    """
    override = os.environ.get(REVOKE_STATE_ENV)
    if override:
        return Path(override)
    if os.name == "posix":
        return DEFAULT_REVOKE_STATE_FILE
    return LEGACY_REVOKE_STATE_FILE


def revoke_projection_path(config_path):
    """The persisted projection snapshot path for ONE projection config.

    The snapshot is colocated with the projection config it describes; the
    default deployment passes --config
    /var/lib/um-share/run/share-projection.json, so the snapshot also lands
    under the writable state tree (never inside the read-only release).  Keying
    the name by the config path also keeps separate deployments (and test
    fixtures) from sharing a snapshot.
    """
    config = Path(config_path)
    return config.parent / (config.name + ".revoke-projection")


class RevokeStateUnavailable(ValueError):
    """A persisted revoke/projection state exists but cannot be trusted.

    Review #12: a missing file AFTER a trusted first initialization, a corrupt
    file, an unreadable file and a version mismatch all fail closed.  The
    in-memory caches are never replaced with an empty state and the existing
    file is never silently rewritten.  The message names the exact path and the
    recovery action so an operator can restore a backup or intentionally
    reinitialize.
    """


def _initialization_marker_path(path):
    """The sibling marker proving ``path`` was initialized once.

    The marker is deliberately separate from the state payload: a deleted or
    truncated state file can then be told apart from a real first start.  Both
    files are atomically replaced with the same private (0600) helper.
    """
    path = Path(path)
    return path.with_name(path.name + ".initialized")


def _read_initialization_marker(path):
    """True when the marker proves ``path`` was initialized; fail closed.

    A missing marker is the normal legacy/first-start case (False).  An
    unreadable, oversized, malformed or version-mismatched marker raises
    ``RevokeStateUnavailable``: it is evidence that initialization happened,
    so the caller must not treat the state as absent.
    """
    marker = _initialization_marker_path(path)
    try:
        raw = marker.read_bytes()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RevokeStateUnavailable(
            f"initialization marker {marker} is unreadable ({exc}); restore "
            f"it from backup or remove {marker} to reinitialize") from exc
    if len(raw) > REVOKE_INIT_MARKER_MAX_BYTES:
        raise RevokeStateUnavailable(
            f"initialization marker {marker} is unexpectedly large; restore "
            f"it from backup or remove {marker} to reinitialize")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise RevokeStateUnavailable(
            f"initialization marker {marker} is corrupt; restore it from "
            f"backup or remove {marker} to reinitialize") from exc
    if (not isinstance(payload, dict)
            or payload.get("version") != REVOKE_STATE_VERSION):
        raise RevokeStateUnavailable(
            f"initialization marker {marker} has an unsupported version; "
            f"restore it from backup or remove {marker} to reinitialize")
    return True


def _write_initialization_marker(path):
    """Create the private marker AFTER the state itself was durably written."""
    marker = _initialization_marker_path(path)
    _write_private_json_atomic(
        marker, {"version": REVOKE_STATE_VERSION},
        REVOKE_INIT_MARKER_MAX_BYTES)


def _empty_revoke_state():
    return {"version": REVOKE_STATE_VERSION, "nonces": {},
            "generations": {}, "projection_generation": 0}


def _read_revoke_state(path):
    """Read and strictly validate ONE revoke state file.

    A missing file is reported as ``FileNotFoundError`` so the loader can CREATE
    the first state file with its schema version.  An unreadable, oversized,
    malformed or version-mismatched file raises ``ValueError`` so the caller
    fails closed instead of silently forgetting a previous revoke.
    """
    raw = path.read_bytes()
    if len(raw) > REVOKE_STATE_MAX_BYTES:
        raise ValueError("revoke state is unexpectedly large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("revoke state is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("revoke state is not an object")
    if payload.get("version") != REVOKE_STATE_VERSION:
        raise ValueError("revoke state version is unsupported")
    nonces = payload.get("nonces")
    generations = payload.get("generations")
    if not isinstance(nonces, dict) or not isinstance(generations, dict):
        raise ValueError("revoke state shape is invalid")
    # The applied projection generation is the ACK watermark for the
    # revoke/rotate/limit_lower intents that were applied BEFORE this state
    # file existed.  Old files predate it and read as 0.
    projection_generation = payload.get("projection_generation", 0)
    if (isinstance(projection_generation, bool)
            or not isinstance(projection_generation, int)
            or projection_generation < 0):
        raise ValueError("revoke state projection generation is invalid")
    clean_nonces = {}
    for nonce, ts in nonces.items():
        if (not isinstance(nonce, str) or not nonce
                or isinstance(ts, bool) or not isinstance(ts, int)):
            raise ValueError("revoke state nonce entry is invalid")
        clean_nonces[nonce] = ts
    clean_generations = {}
    for key_id, entry in generations.items():
        if not isinstance(key_id, str) or not key_id \
                or not isinstance(entry, dict):
            raise ValueError("revoke state generation entry is invalid")
        generation = entry.get("generation")
        applied_at = entry.get("applied_at")
        # ``updated_at`` is the eviction clock (review #14); old state files
        # predate it and fall back to ``applied_at``.
        updated_at = entry.get("updated_at", applied_at)
        if (isinstance(generation, bool) or not isinstance(generation, int)
                or generation < 0
                or isinstance(applied_at, bool)
                or not isinstance(applied_at, (int, float))
                or applied_at < 0
                or isinstance(updated_at, bool)
                or not isinstance(updated_at, (int, float))
                or updated_at < 0):
            raise ValueError("revoke state generation entry is invalid")
        clean_generations[key_id] = {
            "generation": generation, "applied_at": applied_at,
            "updated_at": updated_at}
    return {"version": REVOKE_STATE_VERSION, "nonces": clean_nonces,
            "generations": clean_generations,
            "projection_generation": projection_generation}


def _legacy_revoke_state_candidates(path, primary=None):
    """The pre-fix release-relative state files, when they are DIFFERENT files.

    The legacy files are a migration SOURCE only: they are read once at the first
    initialization of the deployment default, and they are never modified or deleted
    (the release may be read-only and the operator owns those files).

    The CURRENT release is ``<prefix>/releases/<v>`` and the pre-fix state lived in
    its own ``data/share-revoke.state``.  After a version switch that file belongs
    to the OLD release, so the siblings of the current release are searched too;
    otherwise the pending generation of the release that ran before the switch would
    never be adopted.  ``primary`` is a test seam; the module constant is used
    when it is omitted.
    """
    primary = Path(LEGACY_REVOKE_STATE_FILE if primary is None else primary)
    candidates = []
    try:
        if Path(path).resolve() == primary.resolve():
            return candidates
    except OSError:
        return candidates
    candidates.append(primary)
    # primary is <release>/data/share-revoke.state.  The release root's parent
    # holds the sibling release directories when `current` is a symlink into
    # <prefix>/releases/<v>; when `current` is a plain directory the releases
    # live one level deeper under <prefix>/releases/*.
    release_root = primary.parent.parent
    releases = [release_root.parent, release_root.parent / "releases"]
    siblings = []
    for root in releases:
        try:
            siblings.extend(sorted(root.glob("*/data/share-revoke.state")))
        except OSError:
            continue
    seen = set()
    try:
        seen.add(primary.resolve())
    except OSError:
        pass
    for sibling in siblings:
        try:
            if not sibling.is_file():
                continue
            resolved = sibling.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        candidates.append(sibling)
    return candidates


def _adopts_legacy_revoke_state(path):
    """True only for the deployment default; an override owns its own state.

    Adopting into an arbitrary override could resurrect one deployment's revoke
    watermarks in another.  The documented default (or an override naming it) is
    the ONE upgrade target, and the code reports an ignored legacy file for any
    other path instead of reading it silently.
    """
    try:
        return Path(path) == DEFAULT_REVOKE_STATE_FILE
    except TypeError:
        return False


def _merge_revoke_states(states):
    """Merge several adopted legacy states into ONE conservative state.

    The nonce registry keeps the newest timestamp seen for each nonce.  A per-Key
    applied generation keeps the NEWEST generation; when two files carry the same
    generation the newest application time wins, so the admission fence never moves
    backwards.  The applied projection watermark keeps the newest generation.
    """
    nonces = {}
    for state in states:
        for nonce, ts in state["nonces"].items():
            if ts > nonces.get(nonce, -1):
                nonces[nonce] = ts
    generations = {}
    for state in states:
        for key_id, entry in state["generations"].items():
            current = generations.get(key_id)
            if current is None or (
                    entry["generation"],
                    entry.get("applied_at", 0)) > (
                        current["generation"],
                        current.get("applied_at", 0)):
                generations[key_id] = entry
    projection_generation = max(
        (state.get("projection_generation", 0) for state in states), default=0)
    return {"version": REVOKE_STATE_VERSION, "nonces": nonces,
            "generations": generations,
            "projection_generation": projection_generation}


def _legacy_adoption_marker_path(path):
    """The sibling marker proving the pre-fix file was adopted ONCE.

    It keeps an intentional reinitialization (state and initialization marker
    removed) from resurrecting a stale release file on the next start.
    """
    path = Path(path)
    return path.with_name(path.name + REVOKE_LEGACY_ADOPTED_SUFFIX)


def _read_legacy_revoke_state(legacy):
    """Read the pre-fix state for ONE-TIME adoption; None when absent.

    A corrupt legacy file fails closed instead of being skipped: it may hold the
    only copy of a pending generation, so the operator must resolve it
    explicitly (restore it from backup or remove it to start empty).
    """
    try:
        return _read_revoke_state(legacy)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RevokeStateUnavailable(
            f"legacy revoke state {legacy} is unreadable or corrupt ({exc}); "
            f"restore it from backup, or remove it to start empty") from exc


def _read_adopted_legacy_revoke_state(candidates):
    """Read and MERGE every present pre-fix state; None when none is present.

    A corrupt candidate fails closed instead of being skipped: it may hold the
    only copy of a pending generation, so the operator must resolve it
    explicitly (restore it from backup or remove it to start empty).
    """
    states = []
    adopted = []
    for legacy in candidates:
        state = _read_legacy_revoke_state(legacy)
        if state is not None:
            states.append(state)
            adopted.append(legacy)
    if not states:
        return None, []
    return _merge_revoke_states(states), adopted


def _report_ignored_legacy_revoke_state(path, candidates, reason):
    """Say ONCE that present pre-fix files are not the active state.

    The files are never modified or deleted, so no pending generation disappears
    silently: the operator can copy them deliberately or remove them.
    """
    for legacy in candidates:
        try:
            present = legacy.exists()
        except OSError:
            present = False
        if present:
            print(f"revoke state: the legacy file {legacy} is ignored "
                  f"({reason}); the active state is {path} and the legacy "
                  f"file is left untouched", file=sys.stderr, flush=True)


def _write_private_json_atomic(path, payload, max_bytes):
    """Atomically replace ONE private JSON file; failure leaves the old one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=True,
                       separators=(",", ":")).encode("ascii")
    if len(data) > max_bytes:
        raise ValueError("state payload is unexpectedly large")
    fd, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name == "posix":
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _write_revoke_state_payload(path, nonces, generations,
                                projection_generation=0):
    """Atomically replace ONE revoke state file with the given payload."""
    _write_private_json_atomic(path, {
        "version": REVOKE_STATE_VERSION,
        "nonces": nonces,
        "generations": generations,
        "projection_generation": projection_generation,
    }, REVOKE_STATE_MAX_BYTES)


def _write_revoke_state_locked():
    """Atomically replace the state file; a failed write leaves the old one."""
    _write_revoke_state_payload(
        revoke_state_path(), _revoke_notice_seen, _revoke_applied_generations,
        _revoke_projection_generation)


def _read_projection_snapshot(path):
    """Read and strictly validate ONE projection snapshot file.

    A missing snapshot is the normal first start (None).  An unreadable,
    oversized, malformed or version-mismatched file raises ``ValueError`` so the
    caller records a fault and keeps the config baseline instead of trusting a
    half-written snapshot.
    """
    raw = path.read_bytes()
    if len(raw) > REVOKE_PROJECTION_MAX_BYTES:
        raise ValueError("revoke projection state is unexpectedly large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("revoke projection state is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("revoke projection state is not an object")
    if payload.get("version") != REVOKE_PROJECTION_VERSION:
        raise ValueError("revoke projection state version is unsupported")
    generation = payload.get("generation")
    keys = payload.get("keys")
    if (isinstance(generation, bool) or not isinstance(generation, int)
            or generation < 0 or not isinstance(keys, dict)):
        raise ValueError("revoke projection state shape is invalid")
    clean_keys = {}
    for key_id, enabled in keys.items():
        if not isinstance(key_id, str) or not key_id \
                or not isinstance(enabled, bool):
            raise ValueError("revoke projection state key entry is invalid")
        clean_keys[key_id] = enabled
    return {"generation": generation, "keys": clean_keys}


def _write_projection_snapshot(path, generation, known_enabled):
    _write_private_json_atomic(path, {
        "version": REVOKE_PROJECTION_VERSION,
        "generation": generation,
        "keys": dict(known_enabled),
    }, REVOKE_PROJECTION_MAX_BYTES)


def _prune_revoke_generations_locked(now=None):
    """Drop applied generations whose notice window has fully expired.

    A notice outside ``REVOKE_NOTICE_TS_WINDOW_S`` is rejected before it can
    reach this state, so an entry older than that window can never be needed
    again.  Within the window every stored generation stays absolute.
    """
    now = time.time() if now is None else now
    cutoff = now - REVOKE_STATE_GENERATIONS_TTL_S
    for key_id, entry in list(_revoke_applied_generations.items()):
        updated_at = (entry.get("updated_at", entry.get("applied_at", 0))
                      if isinstance(entry, dict) else 0)
        if isinstance(updated_at, bool) \
                or not isinstance(updated_at, (int, float)):
            updated_at = 0
        if updated_at < cutoff:
            _revoke_applied_generations.pop(key_id, None)


def _bounded_projection_known(current_keys, old_known, limit):
    """Current projection Keys plus the most recently removed ones.

    Every Key present in the current projection stays known: a later
    disable/delete/re-add must still be detected.  Removed Keys are retained
    only up to the remaining budget, newest first, so Key churn cannot grow the
    set without bound.
    """
    current = OrderedDict()
    for key_id in current_keys:
        if key_id not in current:
            current[key_id] = None
    budget = max(0, limit - len(current))
    removed = [key_id for key_id in old_known if key_id not in current]
    kept = removed[-budget:] if budget else []
    known = OrderedDict((key_id, None) for key_id in kept)
    known.update(current)
    return known


def _load_revoke_state_locked(create=True):
    """Load the persisted state into the in-process caches exactly once.

    Review #12: the FIRST trusted initialization creates the state file and its
    sibling marker.  After that, a missing state file, a corrupt file, an
    unreadable file or a version mismatch fails closed: the in-memory caches are
    left untouched (never replaced with an empty state) and the error names the
    path plus the recovery action.  A legacy state file without a marker (written
    before this marker existed) is adopted and marked, so an existing deployment is
    never locked out.  A failed first initialization is not marked, so a later
    start retries it instead of locking the installation forever.

    ``create=False`` is the read-only startup/ACK merge: a genuinely absent
    state (no file AND no initialization marker) is left for the first real
    write instead of being created by a status read.  Everything else (an
    initialized-but-missing file, a corrupt file, a version mismatch) still
    fails closed.

    Upgrade path: the pre-fix default lived inside the release, which the broker
    unit mounts read-only.  The deployment default therefore adopts every
    release-relative file (the current release plus its sibling release
    directories) ONCE at its first initialization, merging them; a sibling
    adoption marker keeps an intentional reinitialization from resurrecting them,
    and the legacy files themselves are never modified or deleted.  An explicit
    override for another location owns its own state and never adopts; present
    legacy files are reported as ignored there so no pending generation
    disappears silently.
    """
    global _revoke_state_loaded, _revoke_projection_generation, \
        _revoke_state_absent
    if _revoke_state_loaded or (not create and _revoke_state_absent):
        return
    path = revoke_state_path()
    marker = _initialization_marker_path(path)
    legacy_candidates = _legacy_revoke_state_candidates(path)
    adopts_legacy = (bool(legacy_candidates)
                     and _adopts_legacy_revoke_state(path))
    try:
        state = _read_revoke_state(path)
    except FileNotFoundError:
        if _read_initialization_marker(path):
            raise RevokeStateUnavailable(
                f"revoke state {path} is missing after initialization; "
                f"restore it from backup, or remove {marker} to reinitialize")
        if not create:
            # First start / read-only merge: there is no state to merge yet.
            _revoke_state_absent = True
            return
        adopted = None
        adopted_from = []
        if adopts_legacy and not _legacy_adoption_marker_path(path).exists():
            adopted, adopted_from = _read_adopted_legacy_revoke_state(
                legacy_candidates)
        # First start: write the state and THEN its marker.  A failed creation
        # fails closed; it is never treated as an empty state, and without the
        # marker the next start retries the first initialization.
        try:
            if adopted is None:
                _write_revoke_state_locked()
            else:
                _write_revoke_state_payload(
                    path, adopted["nonces"], adopted["generations"],
                    adopted.get("projection_generation", 0))
            _write_initialization_marker(path)
        except (OSError, ValueError) as exc:
            raise RevokeStateUnavailable(
                f"revoke state {path} could not be initialized ({exc}); fix "
                f"the data directory permissions and retry") from exc
        if adopted is not None:
            try:
                _write_private_json_atomic(
                    _legacy_adoption_marker_path(path),
                    {"version": REVOKE_STATE_VERSION},
                    REVOKE_INIT_MARKER_MAX_BYTES)
            except (OSError, ValueError) as exc:
                # The adopted state is already durable and marked; only the
                # one-time guard is missing.  Never fail a valid start here.
                print(f"revoke state: adopted {adopted_from} but could not "
                      f"write its adoption marker ({exc})", file=sys.stderr,
                      flush=True)
            print(f"revoke state adopted from {adopted_from} into {path}",
                  file=sys.stderr, flush=True)
            state = adopted
        else:
            if legacy_candidates:
                _report_ignored_legacy_revoke_state(
                    path, legacy_candidates,
                    "already adopted" if adopts_legacy
                    else "the path is not the deployment default")
            state = _empty_revoke_state()
    except (OSError, ValueError) as exc:
        raise RevokeStateUnavailable(
            f"revoke state {path} is unreadable or corrupt ({exc}); restore "
            f"it from backup, or remove {path} and {marker} to "
            f"reinitialize") from exc
    else:
        if not _read_initialization_marker(path):
            # A state file from before the marker existed: adopt it, then mark
            # it.  If the marker cannot be written the state is NOT accepted.
            try:
                _write_initialization_marker(path)
            except (OSError, ValueError) as exc:
                raise RevokeStateUnavailable(
                    f"revoke state {path} was adopted but its marker could "
                    f"not be written ({exc}); fix the data directory "
                    f"permissions and retry") from exc
        if legacy_candidates:
            # The configured state is authoritative after the one-time upgrade
            # adoption.  Say so instead of silently ignoring the old files; the
            # operator may delete them once the new state is confirmed.
            _report_ignored_legacy_revoke_state(
                path, legacy_candidates, "the configured state already exists")
    latest = max(state["nonces"].values(), default=0)
    horizon = latest - 2 * REVOKE_NOTICE_TS_WINDOW_S
    _revoke_notice_seen.clear()
    _revoke_notice_seen.update({
        nonce: ts for nonce, ts in state["nonces"].items() if ts >= horizon})
    _revoke_applied_generations.clear()
    _revoke_applied_generations.update(state["generations"])
    _revoke_projection_generation = state.get("projection_generation", 0)
    _prune_revoke_generations_locked()
    _revoke_state_absent = False
    _revoke_state_loaded = True


def load_revoke_state(create=True):
    """Return the persisted revoke state (nonces and per-Key generations).

    ``create=False`` merges an existing state without initializing one: a
    genuinely absent state is left absent for the first real write, while a
    missing-after-initialization or corrupt state still fails closed.
    """
    with _revoke_state_lock:
        _load_revoke_state_locked(create=create)
        return {
            "nonces": dict(_revoke_notice_seen),
            "generations": {
                key_id: (dict(entry) if isinstance(entry, dict) else entry)
                for key_id, entry in _revoke_applied_generations.items()},
        }


def load_revoke_generation(key_id):
    """The persisted applied revoke generation for ONE Key, or None."""
    with _revoke_state_lock:
        _load_revoke_state_locked()
        entry = _revoke_applied_generations.get(key_id)
        return dict(entry) if entry is not None else None


def record_revoke_nonce(nonce, ts):
    """Remember ONE verified notice nonce; False when it was already seen.

    The check and the write happen under ONE lock and the state file is replaced
    atomically, so a replay after a process restart is still refused.
    """
    with _revoke_state_lock:
        _load_revoke_state_locked()
        if nonce in _revoke_notice_seen:
            return False
        previous = dict(_revoke_notice_seen)
        _revoke_notice_seen[nonce] = ts
        latest = max(_revoke_notice_seen.values(), default=ts)
        horizon = latest - 2 * REVOKE_NOTICE_TS_WINDOW_S
        for old in [item for item, seen_ts in _revoke_notice_seen.items()
                    if seen_ts < horizon]:
            _revoke_notice_seen.pop(old, None)
        while len(_revoke_notice_seen) > REVOKE_NOTICE_SEEN_MAX:
            _revoke_notice_seen.pop(next(iter(_revoke_notice_seen)))
        try:
            _write_revoke_state_locked()
        except (OSError, ValueError):
            _revoke_notice_seen.clear()
            _revoke_notice_seen.update(previous)
            raise
        return True


def record_revoke_generation(key_id, generation, applied_at):
    """Persist ONE applied revoke generation for a Key.

    The entry is kept across restarts until its notice window expires; within
    the window the stored generation stays absolute, so a late older revoke can
    never be treated as new.  ``updated_at`` is the eviction clock and is
    deliberately separate from ``applied_at``, which a re-enable preserves as
    the admission boundary.  A newer request's attribution timestamp
    (strictly after ``applied_at``) can still be admitted, so a Key that is
    later re-enabled does not stay blocked forever.
    """
    with _revoke_state_lock:
        _load_revoke_state_locked()
        previous = dict(_revoke_applied_generations)
        now = time.time()
        _revoke_applied_generations[key_id] = {
            "generation": generation, "applied_at": applied_at,
            "updated_at": now}
        _prune_revoke_generations_locked(now)
        try:
            _write_revoke_state_locked()
        except (OSError, ValueError):
            _revoke_applied_generations.clear()
            _revoke_applied_generations.update(previous)
            raise


def record_projection_generation(generation):
    """Persist the newest APPLIED projection generation as the ACK watermark.

    The management store records a revoke/rotate/limit_lower intent in the SAME
    transaction that changes the Key and bumps ``config_generation``; the
    projection rendered after that commit therefore CARRIES the intent at that
    generation.  Applying projection generation N is the evidence that every
    security intent at or below N is applied - including ``rotate`` and
    ``limit_lower``, which have no per-Key revoke entry because they leave the
    Key enabled.  The watermark is monotonic: a repeated or older generation is
    a no-op, and a failed write leaves the previous value.

    Only an APPLIED generation is recorded (the caller passes the generation it
    installed or adopted from the persisted projection snapshot), so this never
    turns "no generation" into a confirmation.
    """
    if isinstance(generation, bool) or not isinstance(generation, int) \
            or generation < 0:
        raise ValueError("projection generation must be a non-negative integer")
    global _revoke_projection_generation
    with _revoke_state_lock:
        _load_revoke_state_locked()
        if generation <= _revoke_projection_generation:
            return False
        previous = _revoke_projection_generation
        _revoke_projection_generation = generation
        try:
            _write_revoke_state_locked()
        except (OSError, ValueError):
            _revoke_projection_generation = previous
            raise
        return True


def applied_security_generation():
    """The newest APPLIED security generation across every Key, as ONE int.

    The value MERGES the persisted revoke state (loaded on demand, so a restart
    that has not seen a notice yet still reports the generations it already
    applied) with the applied projection watermark written by
    ``record_projection_generation``.  A per-Key entry written by
    ``record_revoke_generation`` (generation/applied_at/updated_at) and a legacy
    plain int are both accepted.  The result is always an integer watermark (0
    when nothing was applied): a per-Key dict must NEVER reach ``max`` - two of
    them raise TypeError, which the instance channel would answer as
    ``internal_error`` instead of the load ACK.  A malformed entry is ignored
    rather than crashing the ACK, and an unreadable state file falls back to
    the in-memory snapshot (fail-closed: the ACK then reports only what this
    process already confirmed, never an invented watermark).
    """
    with _revoke_state_lock:
        try:
            _load_revoke_state_locked(create=False)
        except (OSError, ValueError):
            pass
        newest = _revoke_projection_generation
        for entry in list(_revoke_applied_generations.values()):
            generation = (entry.get("generation")
                          if isinstance(entry, dict) else entry)
            if isinstance(generation, bool) or not isinstance(generation, int) \
                    or generation < 0:
                continue
            newest = max(newest, generation)
        return newest


def reset_revoke_state_cache():
    """Forget the in-process cache (tests); the state file is untouched."""
    global _revoke_state_loaded, _revoke_projection_generation, \
        _revoke_state_absent
    with _revoke_state_lock:
        _revoke_state_loaded = False
        _revoke_state_absent = False
        _revoke_projection_generation = 0
        _revoke_notice_seen.clear()
        _revoke_applied_generations.clear()


def _b64url_decode(value):
    """Strict canonical base64url decode; None when the value is not one."""
    if not isinstance(value, str) or not value \
            or len(value) > ATTRIBUTION_MAX_ENCODED_KEY_ID:
        return None
    if any(ch not in _B64URL_ALPHABET for ch in value):
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        return None
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != value:
        return None
    return raw


def _valid_revoke_nonce(value):
    return (isinstance(value, str)
            and len(value) == REVOKE_NOTICE_NONCE_CHARS
            and all(ch in "0123456789abcdef" for ch in value))


def _valid_revoke_action(value):
    return isinstance(value, str) and value in REVOKE_NOTICE_ACTIONS


def build_revoke_notice(secret, key_id, projection_generation, now=None,
                        nonce=None, action="revoke"):
    """Build ONE signed transition notice, or None when it cannot be signed.

    The payload contains NO plaintext Key and NO secret: only the stable key_id,
    the applied projection generation, the signed action (revoke / reenable), a
    timestamp, a one-time nonce and the HMAC.  The action is part of the signed
    bytes, so a valid revoke cannot be replayed as a re-enable (or the reverse).
    """
    if not isinstance(secret, str) or len(secret) < ATTRIBUTION_KEY_MIN_BYTES:
        return None
    if not isinstance(key_id, str) or not key_id:
        return None
    try:
        encoded_key_id = _b64url(key_id.encode("utf-8"))
    except UnicodeError:
        return None
    if len(encoded_key_id) > ATTRIBUTION_MAX_ENCODED_KEY_ID:
        return None
    if (isinstance(projection_generation, bool)
            or not isinstance(projection_generation, int)
            or projection_generation < 0):
        return None
    if not _valid_revoke_action(action):
        return None
    if nonce is None:
        nonce = secrets.token_hex(REVOKE_NOTICE_NONCE_CHARS // 2)
    if not _valid_revoke_nonce(nonce):
        return None
    ts = str(int(time.time() if now is None else now))
    signed = "\n".join((REVOKE_NOTICE_VERSION, action, encoded_key_id,
                        str(projection_generation), ts, nonce))
    signature = hmac.new(secret.encode("utf-8"), signed.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    return {"version": REVOKE_NOTICE_VERSION, "action": action,
            "key_id": encoded_key_id,
            "projection_generation": projection_generation, "ts": ts,
            "nonce": nonce, "signature": signature}


def verify_revoke_notice(body, secret, now=None):
    """Verify ONE signed transition notice; returns ``(key_id, generation, reason)``.

    Fail-closed: a malformed shape, a wrong version, an unknown action, an
    unknown key id encoding, an out-of-window timestamp, a replayed nonce, a
    missing/too-short secret and a wrong signature all yield the unattributed
    pair with the reason.  The nonce is remembered ONCE, after every check
    passed, so a replay of the exact same notice is refused while a new nonce for
    the same generation is left to the caller's idempotency rule.
    """
    if not isinstance(body, dict):
        return None, None, "bad_shape"
    if set(body) != {"version", "action", "key_id", "projection_generation",
                     "ts", "nonce", "signature"}:
        return None, None, "bad_shape"
    version = body.get("version")
    action = body.get("action")
    encoded_key_id = body.get("key_id")
    generation = body.get("projection_generation")
    ts_raw = body.get("ts")
    nonce = body.get("nonce")
    signature = body.get("signature")
    if version != REVOKE_NOTICE_VERSION:
        return None, None, "bad_version"
    if not _valid_revoke_action(action):
        return None, None, "bad_action"
    if not isinstance(encoded_key_id, str) or not encoded_key_id \
            or len(encoded_key_id) > ATTRIBUTION_MAX_ENCODED_KEY_ID:
        return None, None, "bad_key_id"
    decoded = _b64url_decode(encoded_key_id)
    if decoded is None:
        return None, None, "bad_key_id"
    try:
        key_id = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, "bad_key_id"
    if not key_id:
        return None, None, "bad_key_id"
    if (isinstance(generation, bool) or not isinstance(generation, int)
            or generation < 0):
        return None, None, "bad_generation"
    if not isinstance(ts_raw, str) or not ts_raw.isdigit():
        return None, None, "bad_ts"
    try:
        ts = int(ts_raw)
    except ValueError:
        return None, None, "bad_ts"
    current = int(time.time() if now is None else now)
    if abs(current - ts) > REVOKE_NOTICE_TS_WINDOW_S:
        return None, None, "ts_out_of_window"
    if not _valid_revoke_nonce(nonce):
        return None, None, "bad_nonce"
    if not isinstance(signature, str) or len(signature) != 64:
        return None, None, "bad_signature"
    try:
        int(signature, 16)
    except ValueError:
        return None, None, "bad_signature"
    if not isinstance(secret, str) or len(secret) < ATTRIBUTION_KEY_MIN_BYTES:
        return None, None, "no_key"
    signed = "\n".join((version, action, encoded_key_id, str(generation),
                        ts_raw, nonce))
    expected = hmac.new(secret.encode("utf-8"), signed.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None, None, "bad_signature"
    try:
        if not record_revoke_nonce(nonce, ts):
            return None, None, "replayed"
    except (OSError, ValueError):
        # A state file that cannot be read/written must not silently forget
        # the nonce: fail closed instead of accepting a possible replay.
        return None, None, "state_unavailable"
    _revoke_notice_facts.ts = ts
    _revoke_notice_facts.action = action
    return key_id, generation, "verified"


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
            from share.broker_protocol import (ProtocolError,
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


def valid_chat_request(data):
    """Shape-only admission for one shared Chat Completions body."""
    if not isinstance(data, dict) or set(data) - CHAT_FIELDS:
        return False
    if not isinstance(data.get("model"), str) or not data["model"]:
        return False
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    if not isinstance(data.get("stream", False), bool):
        return False
    return "store" not in data or data["store"] is False


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
    shape; it is accepted on Messages and model discovery (``allow_api_key``).
    Chat Completions keeps its Bearer-only credential contract.

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


class ShareGate:
    """In-process share-key authentication and admission gate.

    The merged 8100 process authenticates the share credential here and then
    serves the request through the normal chat2api handler. There is no second
    listener, no loopback forward and no HMAC marker: the key_id installed on
    the request is the one this gate authenticated.
    """

    def __init__(self, config_path, auth_mode=None):
        self.config_path = Path(config_path)
        self.auth_mode = auth_mode
        self.guard = threading.Lock()
        self.state = "running"      # running | draining | stopped
        self.active = {}
        self.history = {}
        self.inflight = {}
        self._cfg = load_config(self.config_path, auth_mode=auth_mode)
        self.share_max_concurrency = self._cfg["share_max_concurrency"]
        self.key_limits = {e["key_id"]: e["max_concurrency"]
                           for e in self._cfg["entries"]}
        self.config_generation = self._cfg["config_generation"]

    def snapshot(self):
        with self.guard:
            return {
                "state": self.state,
                "config_generation": self.config_generation,
                "share_max_concurrency": self.share_max_concurrency,
                "active": dict(self.active),
                "keys": len(self.key_limits),
            }

    def set_state(self, state):
        with self.guard:
            self.state = state

    def authenticate(self, headers, allow_api_key):
        """Resolve one enabled share entry, or None (fail-closed)."""
        credential = resolve_share_credential(headers, allow_api_key)
        if credential is None:
            return None
        try:
            cfg = load_config(self.config_path, auth_mode=self.auth_mode)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        digest = hashlib.sha256(credential.encode()).hexdigest()
        entry = next((e for e in cfg["entries"]
                      if e["enabled"]
                      and hmac.compare_digest(e["secret_hash"], digest)), None)
        if entry is None:
            return None
        with self.guard:
            self._cfg = cfg
            self.share_max_concurrency = cfg["share_max_concurrency"]
            self.key_limits = {e["key_id"]: e["max_concurrency"]
                               for e in cfg["entries"]}
            self.config_generation = cfg["config_generation"]
        return entry, cfg

    def acquire(self, key_id, generation=None, secret_version=None,
                reason_out=None):
        with self.guard:
            if self.state != "running":
                if reason_out is not None:
                    reason_out["reason"] = "state_not_accepting"
                    reason_out["detail"] = (
                        "the share is draining or stopped and is not "
                        "accepting new requests")
                return False
            cfg = self._cfg
            if generation is not None or secret_version is not None:
                try:
                    fresh = load_config(self.config_path,
                                        auth_mode=self.auth_mode)
                except (OSError, ValueError, KeyError, TypeError):
                    return False
                if generation is not None                         and fresh["config_generation"] < generation:
                    return False
                entry = next((e for e in fresh["entries"]
                              if e["key_id"] == key_id), None)
                if entry is None or not entry["enabled"]:
                    return False
                if secret_version is not None                         and entry["secret_version"] != secret_version:
                    return False
                key_limit = entry["max_concurrency"]
                share_max = fresh["share_max_concurrency"]
                cfg = fresh
            else:
                key_limit = self.key_limits.get(
                    key_id, LEGACY_KEY_MAX_CONCURRENCY)
                share_max = self.share_max_concurrency
            now = time.monotonic()
            self.history = {k: v for k, v in self.history.items()
                            if v and v[-1] > now - 60}
            recent = self.history.setdefault(key_id, deque())
            while recent and recent[0] <= now - 60:
                recent.popleft()
            if self.active.get(key_id, 0) >= key_limit:
                if reason_out is not None:
                    reason_out["reason"] = "per_key_concurrency"
                    reason_out["detail"] = (
                        "per-key concurrency limit reached "
                        "(max %d in-flight requests for this key)" % key_limit)
                return False
            if sum(self.active.values()) >= share_max:
                if reason_out is not None:
                    reason_out["reason"] = "share_concurrency"
                    reason_out["detail"] = (
                        "global share concurrency limit reached "
                        "(max %d in-flight requests)" % share_max)
                return False
            if len(recent) >= 20:
                if reason_out is not None:
                    reason_out["reason"] = "key_rate_limit"
                    reason_out["detail"] = (
                        "per-key rate limit reached "
                        "(max 20 requests per minute)")
                return False
            recent.append(now)
            self.active[key_id] = self.active.get(key_id, 0) + 1
            return True

    def release(self, key_id):
        with self.guard:
            count = self.active.get(key_id, 0)
            if count <= 1:
                self.active.pop(key_id, None)
            else:
                self.active[key_id] = count - 1

    def register_inflight(self, key_id, ctx):
        with self.guard:
            self.inflight.setdefault(key_id, set()).add(ctx)

    def unregister_inflight(self, key_id, ctx):
        with self.guard:
            ctxs = self.inflight.get(key_id)
            if ctxs is not None:
                ctxs.discard(ctx)
                if not ctxs:
                    self.inflight.pop(key_id, None)

    def terminate_key(self, key_id):
        """Close every live request held by one revoked key."""
        with self.guard:
            ctxs = self.inflight.pop(key_id, set())
            terminated = 0
            for ctx in ctxs:
                terminate = getattr(ctx, "terminate", None)
                if terminate is not None and terminate():
                    terminated += 1
            return terminated


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
        # Review #13: the forwarded contexts captured when a dispatch Key was
        # revoked.  The backend's released count then identifies the committed
        # sends (still live minus released) that keep the original cancel
        # contract; the rest answer the retryable 429.  Bounded by the live
        # request set and cleared by the notice's finalizer.
        self._revoke_contexts = {}
        # Startup per-key limits (key_id -> max_concurrency): plain acquires
        # (no generation/secret_version re-read) use the limits THIS
        # instance loaded, never the legacy fallback — a v2 key with
        # max_concurrency=8 admitted 8 parallel requests without a
        # per-request projection re-read.
        self.key_limits = {e["key_id"]: e.get("max_concurrency",
                                              LEGACY_KEY_MAX_CONCURRENCY)
                           for e in config_data["entries"]}
        # R-C5 revoke notice: the applied projection snapshot is the transition
        # authority. It changes ONLY when an instance-channel load is applied,
        # never on a request-path auth read, so a key that became disabled in
        # between is still detected by the NEXT applied load.
        self._projection_generation = config_data["config_generation"]
        self._projection_enabled = {entry["key_id"]
                                   for entry in config_data["entries"]
                                   if entry["enabled"]}
        # Review #8: a key that was KNOWN in an earlier applied snapshot and
        # is enabled again is a RE-ENABLE, not a brand-new key.  A brand-new
        # key has no stale revoke state to advance, so it gets no notice.
        # Review #15: the set is bounded to the current projection's Keys plus
        # the most recently removed Keys, so Key churn cannot grow it forever.
        self._projection_known = OrderedDict.fromkeys(
            entry["key_id"] for entry in config_data["entries"])
        # Review #11: persist the applied projection snapshot beside the
        # projection config (the default deployment keeps that config and the
        # revoke state in the same data directory).  A restart while a Key is
        # disabled then still recognizes the later re-enable instead of
        # treating it as a brand-new Key.
        self._revoke_projection_path = revoke_projection_path(self.config)
        persisted = self._load_projection_snapshot()
        if persisted is not None:
            self._projection_generation = persisted["generation"]
            self._projection_enabled = {
                key_id for key_id, enabled in persisted["keys"].items()
                if enabled}
            self._projection_known = OrderedDict.fromkeys(persisted["keys"])
        else:
            self._persist_projection_locked()
        # The previous applied projection's security values.  They are only
        # trustworthy when the config file IS the persisted applied generation:
        # a file that already carries an unapplied generation cannot tell a
        # rotate/limit_lower applied HERE from one applied before the restart,
        # so the first advance records the generation for every enabled Key
        # instead of assuming it was already applied (fail-closed).
        self._projection_secret_versions = {
            entry["key_id"]: entry["secret_version"]
            for entry in config_data["entries"]}
        self._projection_limits = {
            entry["key_id"]: entry["max_concurrency"]
            for entry in config_data["entries"]}
        self._projection_share_max = config_data["share_max_concurrency"]
        self._projection_security_baseline_known = (
            persisted is None
            or persisted["generation"] == config_data["config_generation"])
        # Merge the persisted revoke state BEFORE serving, then record the
        # applied projection generation: a restart must ACK the generation it
        # already applied even before the next load arrives, instead of
        # answering 0 because the in-process cache started empty.
        load_revoke_state(create=False)
        if persisted is not None:
            self._record_applied_projection_generation_locked(
                self._projection_generation)
        self.active = {}
        self.history = {}
        # TCP connection gate: scales with the business limit (share_max + 8)
        # so a high per-key/global limit can never be silently bottlenecked by
        # the gate, while slow connections stay bounded. It is ADJUSTABLE in
        # place (same object) and the instance load installs the projection's
        # effective limit before it ACKs.
        self.connections = _ConnectionGate(
            self.share_max_concurrency + CONNECTION_GATE_HEADROOM)
        super().__init__(address, ShareHandler)

    def process_request(self, request, address):
        if not self.connections.acquire():
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
        the non-reentrant accept lock; a nested acquire would deadlock).
        Kept as the historical int return: the number of live connections."""
        terminated, _upstream_closed = self._terminate_key_locked_counts(client)
        return terminated

    def _terminate_key_locked_counts(self, client):
        """Guard-HELD variant returning ``(terminated, upstream_closed)``.

        D18-3: every live context closes its client socket (the original
        contract) and, when the revoke closes it, its upstream connection so
        a request blocked in ``upstream.getresponse()`` unwinds immediately
        and the handler's ``finally`` releases the 8100 slot.  A retryable
        dispatch request that has not started a response keeps its upstream
        open so the backend's released count can still answer the documented
        429 instead of a 502.
        """
        ctxs = self.inflight.pop(client, set())
        retryable = [ctx for ctx in ctxs
                     if getattr(ctx, "retryable_refusal", False)]
        terminated = 0
        upstream_closed = 0
        if retryable:
            # Review #13: capture the forwarded dispatch contexts BEFORE
            # the backend's released count can identify the committed sends;
            # ``revoke`` only closes a started/non-dispatch context.
            forwarded = [ctx for ctx in retryable
                         if getattr(ctx, "forwarded", False)]
            self._revoke_contexts[client] = sorted(
                forwarded, key=lambda ctx: getattr(ctx, "sequence", 0))
            for ctx in ctxs:
                revoke = getattr(ctx, "revoke", None)
                if revoke is not None:
                    if revoke() and getattr(ctx, "socket_closed", True):
                        terminated += 1
                elif ctx.terminate() and getattr(ctx, "socket_closed", True):
                    terminated += 1
        else:
            for ctx in ctxs:
                # A plain test double without ``revoke`` keeps ``terminate``.
                if ctx.terminate() and getattr(ctx, "socket_closed", True):
                    terminated += 1
        for ctx in ctxs:
            if getattr(ctx, "upstream_closed", False):
                upstream_closed += 1
        return terminated, upstream_closed

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

    def _install_capacity_locked(self, parsed):
        """Install a parsed projection's EFFECTIVE limits (caller holds guard).

        The business global limit, the startup per-key limits and the TCP gate
        bound move TOGETHER, BEFORE the load ACK: a 64 ACK must mean the
        instance can actually admit 64 (a validator-only relaxation would
        leave the old 32-slot gate and a 24 business bound in place).
        In-flight requests keep their reservations; lowering the limit only
        refuses NEW admissions above the new bound.
        """
        self.share_max_concurrency = parsed["share_max_concurrency"]
        self.key_limits = {entry["key_id"]: entry["max_concurrency"]
                           for entry in parsed["data"]}
        self.connections.set_limit(
            self.share_max_concurrency + CONNECTION_GATE_HEADROOM)

    # -------------------------------------------------- instance control endpoint
    # D1/D2: the share instance answers the SYSTEM broker on the INSTANCE
    # channel ({instance-status, load, emergency}). A `load` request installs the
    # projection the writer atomically placed at the fixed config path, and
    # the ACK is decided INSIDE the same guard that serializes acquire — a
    # stale snapshot can never be ACKed before it is refused (A4/A5). The
    # share has NO management ability on this channel (A2/A4 separation).
    def _projection_revoke_transitions_locked(self, parsed):
        """Apply ONE projection generation and return transition notices.

        The caller holds ``self.guard``.  A load that does not ADVANCE the
        generation never changes the snapshot and never emits a notice (a repeated
        or stale load is idempotent).  A key is revoked when it was enabled in
        the applied snapshot and is disabled or absent in the new one.  A key that
        was KNOWN in an earlier snapshot (enabled or disabled, and never removed
        from the known set) and is enabled again is a RE-ENABLE: it does NOT
        revoke anything, it only advances the backend's per-Key generation
        watermark so a LATE older revoke cannot close the newer tickets.

        A ``rotate`` (secret_version advanced) and a ``limit_lower``
        (max_concurrency lowered) leave the Key ENABLED, so they are not in
        ``revoked``; they are detected against the previous APPLIED projection
        and record the same per-Key generation, otherwise the management side
        would see an ACK watermark of 0 and keep their journal generation
        pending forever.  When the previous values are not trustworthy (the
        config file already carried an unapplied generation at startup), the
        generation is recorded for every enabled Key instead of assuming the
        change was already applied.

        The returned specs are a LOCAL SNAPSHOT taken under this guard: each
        item carries the generation that belongs to the transition, so the senders
        can never observe a LATER generation after the guard is released.
        """
        generation = parsed["config_generation"]
        if generation <= self._projection_generation:
            return []
        enabled = {entry["key_id"] for entry in parsed["data"]
                   if entry["enabled"]}
        current_keys = [entry["key_id"] for entry in parsed["data"]]
        known_before = OrderedDict.fromkeys(
            getattr(self, "_projection_known", ()))
        known_set = set(known_before)
        revoked = sorted(self._projection_enabled - enabled)
        reenabled = sorted((enabled & known_set)
                           - self._projection_enabled)
        rotated = []
        limit_lowered = []
        share_max_lowered = False
        previous_share_max = getattr(self, "_projection_share_max", None)
        if (previous_share_max is not None
                and parsed.get("share_max_concurrency") is not None
                and parsed["share_max_concurrency"] < previous_share_max):
            # Lowering the GLOBAL limit is a revoke-direction intent with
            # key_id None; it has no per-Key entry, so the applied
            # projection generation is its ACK watermark.
            share_max_lowered = True
        if not getattr(self, "_projection_security_baseline_known", True):
            # The startup config already carried an unapplied generation: a
            # rotate/limit_lower in it cannot be told apart from one applied
            # here, so record the generation for every enabled Key.  This is
            # fail-closed (an extra in-flight request may be refused), never
            # a false "already applied".
            rotated = sorted(enabled)
        else:
            previous_versions = getattr(
                self, "_projection_secret_versions", {})
            previous_limits = getattr(self, "_projection_limits", {})
            for entry in parsed["data"]:
                key_id = entry["key_id"]
                if key_id not in self._projection_enabled:
                    continue  # a newly added Key has no earlier value
                previous_version = previous_versions.get(key_id)
                if (previous_version is not None
                        and entry.get("secret_version") is not None
                        and entry["secret_version"] > previous_version):
                    rotated.append(key_id)
                previous_limit = previous_limits.get(key_id)
                if (previous_limit is not None
                        and entry.get("max_concurrency") is not None
                        and entry["max_concurrency"] < previous_limit):
                    limit_lowered.append(key_id)
        self._projection_generation = generation
        self._projection_enabled = enabled
        self._projection_known = _bounded_projection_known(
            current_keys, known_before, REVOKE_PROJECTION_KNOWN_MAX)
        self._projection_secret_versions = {
            entry["key_id"]: entry["secret_version"]
            for entry in parsed["data"] if "secret_version" in entry}
        self._projection_limits = {
            entry["key_id"]: entry["max_concurrency"]
            for entry in parsed["data"] if "max_concurrency" in entry}
        self._projection_share_max = parsed.get("share_max_concurrency")
        self._projection_security_baseline_known = True
        # Persist BEFORE the notices leave: a restart after this applied
        # generation must still know which Keys were disabled/deleted.
        self._persist_projection_locked()
        # S2 recovery fence: THIS runtime applied the generation, so its own
        # applied security watermark advances HERE, before the load ACK is built.
        # The 8110 notice is only the client-side ticket-release signal; a
        # management ACK that waited for that round-trip would fail a real
        # revoke closed forever (the notice is best-effort and asynchronous).
        # The projection generation is the ACK watermark for the intents
        # (revoke/rotate/limit_lower) that this projection carries; record it
        # only when a security-relevant transition was applied (or when the
        # previous values are unknown, so the generation cannot be assumed
        # already applied).  A plain config change never moves the security
        # watermark.
        if (revoked or rotated or limit_lowered or share_max_lowered
                or not self._projection_security_baseline_known):
            self._record_applied_projection_generation_locked(generation)
        applied = sorted(set(revoked) | set(rotated) | set(limit_lowered))
        for key_id in applied:
            try:
                record_revoke_generation(key_id, generation, time.time())
            except (OSError, ValueError):
                # The projection is already installed; a failed state-file
                # write is a bounded fault, never a rolled-back load ACK.
                # The ACK then carries the last persisted watermark, so the
                # management side stays fail-closed instead of falsely
                # confirming the intent.
                try:
                    self.record_fault(build_fault_record(
                        phase="config", status=None,
                        code="share_revoke_state_unpersisted"))
                except Exception:  # noqa: BLE001 - never break the load ACK
                    pass
        notices = [{"key_id": key_id, "generation": generation,
                    "action": "revoke"} for key_id in revoked]
        notices.extend({"key_id": key_id, "generation": generation,
                        "action": "reenable"} for key_id in reenabled)
        return notices

    def _load_projection_snapshot(self):
        """Load the last APPLIED projection snapshot, or None on first start.

        Review #12: a missing snapshot after a trusted initialization, a corrupt
        snapshot, an unreadable snapshot and a version mismatch fail closed with
        the exact path and recovery action.  The config baseline is only used on a
        real first start (no snapshot and no marker) or for a legacy snapshot
        written before the marker existed (adopted, then marked).
        """
        path = getattr(self, "_revoke_projection_path", None)
        if path is None:
            return None
        marker = _initialization_marker_path(path)
        try:
            snapshot = _read_projection_snapshot(path)
        except FileNotFoundError:
            if _read_initialization_marker(path):
                raise RevokeStateUnavailable(
                    f"revoke projection snapshot {path} is missing after "
                    f"initialization; restore it from backup, or remove "
                    f"{marker} to reinitialize")
            return None
        except (OSError, ValueError) as exc:
            raise RevokeStateUnavailable(
                f"revoke projection snapshot {path} is unreadable or corrupt "
                f"({exc}); restore it from backup, or remove {path} and "
                f"{marker} to reinitialize") from exc
        if not _read_initialization_marker(path):
            try:
                _write_initialization_marker(path)
            except (OSError, ValueError) as exc:
                raise RevokeStateUnavailable(
                    f"revoke projection snapshot {path} was adopted but its "
                    f"marker could not be written ({exc}); fix the data "
                    f"directory permissions and retry") from exc
        return snapshot

    def _persist_projection_locked(self):
        """Atomically persist the applied projection snapshot."""
        path = getattr(self, "_revoke_projection_path", None)
        if path is None:
            return
        known_enabled = {
            key_id: key_id in self._projection_enabled
            for key_id in self._projection_known}
        try:
            _write_projection_snapshot(
                path, self._projection_generation, known_enabled)
            marker = _initialization_marker_path(path)
            if not marker.exists():
                _write_initialization_marker(path)
        except (OSError, ValueError):
            # The projection is already applied; the failed snapshot is a
            # bounded fault, never a rolled-back load ACK.  A first-start
            # failure is retried by the next start (the marker is written only
            # after the snapshot), never treated as a lost initialized state.
            try:
                self.record_fault(build_fault_record(
                    phase="config", status=None,
                    code="share_revoke_projection_unpersisted"))
            except Exception:  # noqa: BLE001 - never break the load ACK
                pass

    def _record_applied_projection_generation_locked(self, generation):
        """Persist the applied projection generation before the load ACK.

        The projection is already installed; a failed state-file write is a
        bounded fault, never a rolled-back load ACK.  The ACK then carries the
        last persisted watermark, so the management side stays fail-closed
        instead of falsely confirming the intent.
        """
        if generation <= 0:
            return
        try:
            record_projection_generation(generation)
        except (OSError, ValueError):
            try:
                self.record_fault(build_fault_record(
                    phase="config", status=None,
                    code="share_revoke_state_unpersisted"))
            except Exception:  # noqa: BLE001 - never break the load ACK
                pass

    def _send_revoke_notices(self, notices):
        """Best-effort bounded loopback notices for ONE applied transition.

        The projection is ALREADY installed when this runs, so a failure here
        never rolls it back and never turns the load ACK into a failure.  A
        BOUNDED worker pool (``REVOKE_NOTICE_MAX_CONCURRENCY``) sends the
        notices, so a batch of N disabled Keys creates at most that many
        threads/connections; the rest wait in a bounded FIFO.  The WHOLE batch
        has ONE deadline.  After it, the load ACK proceeds; each unfinished
        notice is recorded as a bounded fault (the daemon workers finish their
        current bounded attempt and never delay the process exit).
        """
        specs = []
        for notice in notices:
            if isinstance(notice, dict):
                key_id = notice.get("key_id")
                generation = notice.get("generation")
                action = notice.get("action", "revoke")
            else:
                try:
                    key_id, generation, action = notice
                except (TypeError, ValueError):
                    continue
            if not isinstance(key_id, str) or not key_id:
                continue
            specs.append((key_id, generation, action))
        if not specs:
            return None, None
        deadline = time.monotonic() + REVOKE_NOTICE_BATCH_DEADLINE_S
        # Review #10: acquire the process-wide slots BEFORE creating any
        # thread or queue.  A batch that cannot get a slot (another batch's
        # slow workers still hold them) records bounded timeout faults and
        # returns at its deadline, so cross-batch thread/connection/backlog
        # growth is impossible.
        slots = []
        for _ in range(min(REVOKE_NOTICE_MAX_CONCURRENCY, len(specs))):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not _revoke_notice_worker_slots.acquire(timeout=remaining):
                break
            slots.append(True)
        if not slots:
            for _ in specs:
                self.record_fault(build_fault_record(
                    phase="config", status=None,
                    code="share_revoke_notice_timeout"))
            return None, None
        work = queue.Queue()
        for index, spec in enumerate(specs):
            work.put((index, spec))
        stop = threading.Event()
        done = {}
        done_lock = threading.Lock()
        workers = []
        for _ in range(len(slots)):
            thread = threading.Thread(
                target=self._revoke_notice_worker,
                args=(work, stop, done, done_lock),
                name="share-revoke-notice", daemon=True)
            try:
                thread.start()
            except RuntimeError:
                # Cannot start a worker: release its slot and record ONE
                # bounded fault, never a broken load ACK.
                _revoke_notice_worker_slots.release()
                self.record_fault(build_fault_record(
                    phase="config", status=None,
                    code="share_revoke_notice_failed"))
                continue
            workers.append(thread)
        for thread in workers:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                thread.join(remaining)
        stop.set()
        with done_lock:
            unfinished = len(specs) - len(done)
        for _ in range(unfinished):
            self.record_fault(build_fault_record(
                phase="config", status=None,
                code="share_revoke_notice_timeout"))
        # Review #13: close exactly the COMMITTED forwarded sends.  The
        # backend's notice reports how many PENDING/GRANTED tickets it
        # released; the remaining forwarded contexts of that Key are the
        # committed sends and keep the original cancel contract.  A failed
        # notice (no released count) closes them all, as before.
        finalized = set()
        released_total = 0
        aborted_total = 0
        released_known = True
        aborted_known = True
        for index, (key_id, _generation, action) in enumerate(specs):
            if action != "revoke" or key_id in finalized:
                continue
            finalized.add(key_id)
            payload = done.get(index)
            released = (payload.get("released")
                        if isinstance(payload, dict) else None)
            aborted = (payload.get("aborted")
                       if isinstance(payload, dict) else None)
            if isinstance(released, int) and not isinstance(released, bool):
                released_total += released
            else:
                released_known = False
            if isinstance(aborted, int) and not isinstance(aborted, bool):
                aborted_total += aborted
            else:
                aborted_known = False
            # A failed notice has no count and keeps the old behavior: close
            # every forwarded context of that Key.
            self._finalize_revoke(
                key_id, released if isinstance(released, int) else 0)
        return (released_total if released_known else None,
                aborted_total if aborted_known else None)

    def _finalize_revoke(self, key_id, released):
        """Close exactly the COMMITTED forwarded requests of ONE revoked Key.

        ``released`` is the backend's own count of PENDING/GRANTED tickets
        it closed.  Every forwarded context of this Key that the backend did
        NOT release is a committed send: it keeps the original cancel
        contract (close the client socket).  A context that never reached the
        backend is left to the handler's pre-send 429 check.
        """
        try:
            released = max(0, int(released))
        except (TypeError, ValueError):
            released = 0
        guard = getattr(self, "guard", None)
        if guard is None:
            return
        with guard:
            contexts = [
                context for context in
                getattr(self, "_revoke_contexts", {}).pop(key_id, ())
                if not context.socket_closed]
            contexts.sort(key=lambda context: context.sequence)
            close_count = max(0, len(contexts) - released)
            for context in contexts[:close_count]:
                context.close_socket()

    def _revoke_notice_worker(self, work, stop, done, done_lock):
        """Run notices from the bounded queue until it is empty or stopped.

        Review #10: the process-wide slot is released in ``finally``, so a
        worker that exits after the batch deadline frees its slot for the next
        batch and the thread/connection bound is never exceeded.
        """
        try:
            while not stop.is_set():
                try:
                    index, (key_id, generation, action) = work.get_nowait()
                except queue.Empty:
                    return
                if stop.is_set():
                    return
                payload = self._send_revoke_notice_safely(
                    key_id, generation, action)
                with done_lock:
                    done[index] = payload
        finally:
            _revoke_notice_worker_slots.release()

    def _send_revoke_notice_safely(self, key_id, generation, action):
        """Run ONE notice sender; return its payload or None.

        An unexpected exception is ONE bounded fault for that notice; it can
        never escape the thread or break the load ACK.
        """
        try:
            return self._send_revoke_notice(key_id, generation, action)
        except Exception:  # noqa: BLE001 - never break the load ACK
            self.record_fault(build_fault_record(
                phase="config", status=None,
                code="share_revoke_notice_failed"))
            return None

    def _send_revoke_notice(self, key_id, generation, action="revoke"):
        """Send ONE signed transition notice with the SNAPSHOT generation.

        ``generation`` is the transition's own generation, captured under the
        accept lock that applied it.  It is NEVER re-read from the live
        projection here: a later load that advances the generation must not
        relabel an already-snapshotted notice.
        """
        payload = build_revoke_notice(
            self.attribution_secret, key_id, generation, action=action)
        if payload is None:
            # No usable shared secret, an unbounded key id or an unknown
            # action: the projection is still updated, but no unsigned notice
            # is ever sent.
            self.record_fault(build_fault_record(
                phase="config", status=None,
                code="share_revoke_notice_unsigned"))
            return False
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        started = time.monotonic()
        last_status = None
        last_exc = None
        attempts_made = 0
        for attempt in range(REVOKE_NOTICE_ATTEMPTS):
            attempts_made += 1
            connection = None
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", int(self.upstream_port),
                    timeout=REVOKE_NOTICE_TIMEOUT_S)
                connection.request(
                    "POST", REVOKE_NOTICE_PATH, body=body,
                    headers={"Content-Type": "application/json",
                              "Connection": "close"})
                response = connection.getresponse()
                raw = response.read()
                last_status = response.status
                if 200 <= response.status < 300:
                    # The backend's released count is the ONLY fact used to
                    # separate committed sends from revoked tickets; never
                    # invented when the body is not a JSON object.
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                    except (UnicodeError, ValueError):
                        payload = {}
                    return payload if isinstance(payload, dict) else {}
                if 400 <= response.status < 500:
                    # A bad signature/version/action or a stale generation
                    # will not become valid on a retry; keep the diagnostic
                    # instead of pretending the notice succeeded.
                    break
            except (OSError, http.client.HTTPException) as exc:
                last_exc = exc
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except OSError:
                        pass
            if attempt + 1 < REVOKE_NOTICE_ATTEMPTS:
                time.sleep(REVOKE_NOTICE_RETRY_DELAY_S)
        self.record_fault(build_fault_record(
            phase="config", status=None,
            code="share_revoke_notice_failed",
            upstream_status=last_status,
            exception_type=(type(last_exc).__name__
                            if last_exc is not None else None),
            duration_ms=(time.monotonic() - started) * 1000.0))
        return False

    def _apply_projection_load_locked(self, request):
        """Validate/install ONE instance-channel load; caller holds the guard.

        Returns ``(reply, revoke_notices)``.  The reply is the exact existing
        ACK/refusal shape; ``revoke_notices`` names the keys that THIS applied
        generation disabled or removed, and is sent AFTER the guard is released
        (the loopback POST is network I/O and must never run under the accept
        lock).
        """
        if self.state == "stopped":
            return ({"v": 2, "request_id": request["request_id"],
                     "ok": False, "error_code": "unconfirmed",
                     "result": {"detail": "instance is stopped"}}, [])
        generation = request["fields"]["generation"]
        fingerprint = request["fields"]["fingerprint"]
        try:
            from share.broker_protocol import (ProtocolError,
                                               parse_projection_payload)
        except ImportError:
            return ({"v": 2, "request_id": request["request_id"],
                     "ok": False, "error_code": "internal_error",
                     "result": {"detail": "protocol module unavailable"}}, [])
        # R29-4 single-snapshot rule: ONE read of the bytes; the semantic
        # parse and the fingerprint come from the SAME bytes (shared
        # parse_projection_payload), never a mixed pair of reads.  The auth
        # path re-reads per request and re-verifies — a load can never
        # approve a file that the acquire-time read will not see.
        try:
            raw_text = self.config.read_text(encoding="utf-8")
            payload = json.loads(raw_text)
            parsed = parse_projection_payload(payload)
        except (OSError, ValueError, KeyError, TypeError,
                ProtocolError, UnicodeError):
            # ProtocolError included (R35-2): a corrupt file or a legacy swap
            # of the derived file must be a structured refusal here, never an
            # escaping exception.
            return ({"v": 2, "request_id": request["request_id"],
                     "ok": False, "error_code": "fingerprint_mismatch",
                     "result": {"detail": "projection unreadable or "
                                          "invalid"}}, [])
        if payload["config_generation"] != generation:
            return ({"v": 2, "request_id": request["request_id"],
                     "ok": False, "error_code": "stale_generation",
                     "result": {"detail": "loaded generation differs"}}, [])
        import hashlib
        canonical = json.dumps(payload, sort_keys=True)
        actual_fp = hashlib.sha256(
            canonical.encode("utf-8")).hexdigest()
        if actual_fp != fingerprint:
            return ({"v": 2, "request_id": request["request_id"],
                     "ok": False, "error_code": "fingerprint_mismatch",
                     "result": {"detail": "loaded content differs"}}, [])
        # Same-guard install/ACK decision: the projection's EFFECTIVE capacity
        # is installed BEFORE the ACK, so the ACK cannot claim a limit the
        # instance does not enforce.  note_config itself re-acquires the
        # non-reentrant guard, so the watermark is updated inline here.
        self._install_capacity_locked(parsed)
        self.last_seen_generation = max(
            self.last_seen_generation, payload["config_generation"])
        # R-C5: an applied generation that disables or removes a Key is the
        # transition the 8110 cannot observe by itself.  Compute it under the
        # SAME guard as the install, but send it only after the guard is
        # released (below).
        revoke_notices = self._projection_revoke_transitions_locked(parsed)
        # R8-A7 revoke-and-terminate: the publish may carry a one-shot
        # terminate_keys list (protocol-validated); closing exactly those
        # keys' live connections interrupts their in-flight requests.
        # Counting stays with the owning handlers' finally (release), never
        # here.  The ACK carries "terminated" ONLY when a terminate list
        # arrived (the old-shape load reply keeps its exact contract).
        listed = request["fields"].get("terminate_keys") or []
        terminated = 0
        upstream_closed = 0
        for key_id in listed:
            term, up = self._terminate_key_locked_counts(key_id)
            terminated += term
            upstream_closed += up
        result = {"instance_id": self.instance_id,
                  "generation": generation,
                  "fingerprint": fingerprint,
                  "installed": True,
                  "share_max_concurrency": self.share_max_concurrency,
                  "connection_limit": self.connections.limit,
                  # S2 recovery fence: the ACK carries the runtime's own
                  # applied revoke watermark, so the management side can bind
                  # the answer to the security journal instead of trusting
                  # only the projection generation.
                  "security_generation": applied_security_generation()}
        if listed:
            result["terminated"] = terminated
            # D18-3: how many live 8100 requests actually closed their
            # upstream connection (the rest keep it for the backend's 429).
            result["upstream_closed"] = upstream_closed
        return ({"v": 2, "request_id": request["request_id"],
                 "ok": True, "error_code": "ok", "result": result},
                revoke_notices)

    def handle_instance_control(self, body, secret, now=None):
        if not hasattr(self, "instance_id"):
            self.instance_id = secrets.token_hex(16)
        try:
            from share.broker_protocol import (ProtocolError, ReplayRegistry,
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
            load_reply = None
            load_notices = ()
            with self.guard:
                if action == "instance-status":
                    result = {"instance_id": self.instance_id,
                              "state": self.state,
                              "active_count": sum(self.active.values()),
                              "last_seen_generation": self.last_seen_generation}
                    return {"v": 2, "request_id": request_id, "ok": True,
                            "error_code": "ok", "result": result}
                if action == "load":
                    load_reply, load_notices = (
                        self._apply_projection_load_locked(request))
                elif action == "drain":
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
                elif action == "resume":
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
                elif action == "emergency":
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
                else:
                    return {"v": 2, "request_id": request_id, "ok": False,
                            "error_code": "bad_request",
                            "result": {"detail": "unknown instance action"}}
            # The guard is released BEFORE the loopback notice: the notice is
            # network I/O and must never hold the accept lock.  The projection
            # was already installed above, so a notice failure is recorded as a
            # bounded fault and never changes this load ACK.
            if load_reply is not None:
                try:
                    released, aborted = self._send_revoke_notices(load_notices)
                except Exception:  # noqa: BLE001 - never break the load ACK
                    released, aborted = None, None
                # D18-3: the load ACK separates what 8110 released/aborted
                # from what 8100 closed upstream. Add each count only when
                # the backend actually answered it; a missing count stays
                # absent (never fabricated as 0).
                if isinstance(load_reply, dict):
                    result = load_reply.get("result")
                    if isinstance(result, dict):
                        if released is not None:
                            result["released"] = released
                        if aborted is not None:
                            result["aborted"] = aborted
                return load_reply
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
                "security_generation": applied_security_generation(),
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


_INFLIGHT_SEQUENCE = itertools.count(1)


class _InflightCtx:
    """One live share request's client-socket handle (R8-A7).

    ``terminate`` closes the socket ONCE (shutdown then close, both
    guarded); the owning handler observes the closure and unwinds through
    its own ``finally`` — the counter decrement happens exactly there, so
    termination and release never double-account.

    Review #13: a dispatch request that has not sent its response header is
    different.  The 8110 closes its PENDING/GRANTED ticket and answers the
    retryable 429; only a COMMITTED send keeps the original cancel
    contract (close the client socket).  The 8100 cannot read the 8110 ticket
    state, so it uses the backend's OWN ``released`` count: the forwarded
    contexts it does NOT close are exactly the ones still live minus the
    released tickets, i.e. the committed sends.  A request that has not been
    forwarded yet is left to the handler's pre-send 429 check.  Once response
    headers went out (a streaming cancel), the original contract stays: close
    the socket and never send a second HTTP status."""

    def __init__(self, connection, retryable_refusal=False):
        self._connection = connection
        self._closed = False
        self._response_started = False
        self._forwarded = False
        self._socket_closed = False
        self._retryable_refusal = bool(retryable_refusal)
        # D18-3: the live upstream connection this request is blocked on.
        # A revoke closes it so a blocked ``upstream.getresponse()`` (or a
        # streaming read) unwinds immediately instead of holding the 8100
        # in-flight slot until the upstream silence budget expires.
        self._upstream = None
        self._upstream_closed = False
        # One lock serializes attach/revoke/terminate/close so a revoke that
        # races the upstream creation cannot leave a live connection behind.
        self._state_lock = threading.Lock()
        self.sequence = next(_INFLIGHT_SEQUENCE)

    @property
    def terminated(self):
        """True once a revoke terminated this live request.

        The relay checks it immediately before forwarding upstream: a request
        that 8100 admitted before the revoke but had not yet sent must not
        register a new 8110 ticket or reach the UM after the notice.
        """
        return self._closed

    @property
    def forwarded(self):
        """True once the request reached the 8110 backend."""
        return self._forwarded

    @property
    def retryable_refusal(self):
        """True for a dispatch request that may answer a retryable 429."""
        return self._retryable_refusal

    @property
    def socket_closed(self):
        return self._socket_closed

    @property
    def upstream_closed(self):
        """True once this request's upstream connection was closed."""
        return self._upstream_closed

    def attach_upstream(self, upstream):
        """Attach the live upstream connection for this request.

        Returns True when the connection was attached; False when a revoke
        already terminated this context (the connection is closed immediately
        so a request that raced the revoke never leaves it live).
        """
        with self._state_lock:
            if self._closed:
                close_now = True
            else:
                self._upstream = upstream
                close_now = False
        if close_now:
            try:
                upstream.close()
            except OSError:
                pass
            with self._state_lock:
                self._upstream_closed = True
            return False
        return True

    def close_upstream(self):
        """Close the attached upstream connection ONCE; True when closed.

        ``HTTPConnection.close`` closes the socket; a reader blocked in
        ``getresponse()``/``read1`` observes the closure and unwinds instead
        of holding the 8100 in-flight slot until the silence budget expires.
        """
        with self._state_lock:
            upstream = self._upstream
            self._upstream = None
            if upstream is None:
                return False
            self._upstream_closed = True
        try:
            upstream.close()
        except OSError:
            pass
        return True

    def mark_response_started(self):
        """Record that response headers went out (no second status after)."""
        self._response_started = True

    def mark_forwarded(self):
        """Record that the request was sent to the 8110 backend."""
        self._forwarded = True

    def revoke(self):
        """Terminate for a revoke; return True when this context was live.

        A started response (streaming cancel) or a non-dispatch request keeps
        the original close of BOTH the client socket and the upstream
        connection, so a blocked ``getresponse()`` unwinds immediately.  A
        forwarded dispatch request is closed later by the backend's released
        count, so a PENDING/GRANTED request can still answer the retryable
        429; closing its upstream here would turn that 429 into a 502.  A
        not-yet-forwarded dispatch request is left to the handler's pre-send
        429 check.
        """
        with self._state_lock:
            if self._closed:
                return False
            self._closed = True
            close_now = not self._retryable_refusal or self._response_started
        if close_now:
            self._close_socket()
            self.close_upstream()
        return True

    def terminate(self):
        """Original cancel: close the socket and upstream once, never send a
        status."""
        with self._state_lock:
            if self._closed:
                return False
            self._closed = True
        self._close_socket()
        self.close_upstream()
        return True

    def close_socket(self):
        """Close the client socket once (used by the released-count rule)."""
        if self._socket_closed:
            return False
        self._socket_closed = True
        try:
            self._connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._connection.close()
        except OSError:
            pass
        return True

    def _close_socket(self):
        return self.close_socket()


class _CountingWriter:
    """Count every byte actually written to the client (egress evidence)."""

    def __init__(self, raw):
        self._raw = raw
        self._lock = threading.Lock()
        self.written = 0

    def write(self, data):
        length = len(data)
        with self._lock:
            self.written += length
        return self._raw.write(data)

    def flush(self):
        return self._raw.flush()

    def close(self):
        return self._raw.close()

    def __getattr__(self, name):
        return getattr(self._raw, name)


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
        self._egress = _CountingWriter(self.wfile)
        self.wfile = self._egress
        self._body_consumed = False
        # A9-6: per-request fault evidence anchor (reset at handle_api).
        self._fault_started = None
        self._fault_request_id = None
        self._inflight_ctx = None

    def log_message(self, *_args):
        # Never log request paths, bodies, credentials or upstream errors.
        pass

    def send_response(self, code, message=None):
        """Mark the response as started before the first header goes out.

        Review #13: a later revoke may only close the socket for a request
        that already sent a response (the original streaming-cancel
        contract).  A request with no header sent must instead answer the
        documented 429, so the flag must be set at the ONE point every reply
        passes through.  D18-3: a revoke that already closed the client
        socket must never make the handler block on a write.
        """
        if self._client_gone():
            raise OSError("client socket already closed by a revoke")
        ctx = getattr(self, "_inflight_ctx", None)
        if ctx is not None:
            ctx.mark_response_started()
        super().send_response(code, message)

    def _dispatch_refusal_error(self, error_type="share_dispatch_revoked",
                                status=429):
        """Answer ONE retryable dispatch refusal (429/503 + Retry-After).

        The request never committed and no response header went out, so a retry
        is safe; no success is fabricated and the ledger status is the same
        stable error type.
        """
        self.error(
            status, error_type, phase="upstream",
            error_type=error_type,
            retry_after=SHARE_DISPATCH_RETRY_AFTER_S)

    def _relay_dispatch_refusal(self, response):
        """Relay ONE 8110 retryable dispatch refusal; False when unknown.

        Review #13: only a bounded error type is relayed from the backend's
        JSON error (never its body, headers or credentials).  An unknown or
        malformed shape returns False so the caller keeps the existing
        ``um_unavailable`` mapping instead of fabricating a dispatch type.
        """
        if response.status not in (429, 503):
            return False
        try:
            raw = response.read(SHARE_DISPATCH_REFUSAL_MAX_BYTES)
            payload = json.loads(raw.decode("utf-8"))
            error = payload.get("error") if isinstance(payload, dict) else None
            error_type = error.get("type") if isinstance(error, dict) else None
        except (OSError, http.client.HTTPException, UnicodeError, ValueError):
            return False
        if error_type not in SHARE_DISPATCH_ERROR_TYPES:
            return False
        self.error(
            response.status, error_type, phase="upstream",
            upstream_status=response.status,
            retry_after=response.getheader("Retry-After"),
            error_type=error_type)
        return True

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

    def _relay_refused(self, inflight_ctx):
        """True when a revoke terminated this request before its send.

        The instance-control ``terminate_keys`` path closes the live client
        socket; checking the same context immediately before the upstream send
        keeps the 8100->8110 window closed even when the client socket close
        has not yet been observed by a blocked reader.

        Review #13: a dispatch request that has not sent a response header is
        NOT closed (``_InflightCtx.terminate`` only marks it), so the caller
        must answer the documented retryable 429 instead of silently returning.
        """
        return inflight_ctx is not None and inflight_ctx.terminated

    def _is_dispatch_request(self):
        """True for the three dispatch-bearing relay paths (8110 only)."""
        path = (self.path or "").split("?", 1)[0]
        return self.command == "POST" and (
            path == "/v1/chat/completions"
            or path.startswith(MESSAGES_PATH)
            or path == RESPONSES_PATH)

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

    def _client_gone(self):
        """True when a revoke already closed this request's client socket.

        Writing to a socket closed by another thread can block on Windows; the
        handler then must skip its reply and reach ``finally`` (the slot is
        released there, never by the writer).
        """
        ctx = getattr(self, "_inflight_ctx", None)
        return ctx is not None and ctx.socket_closed

    def reply(self, status, data, *, request_id=None, retry_after=None):
        if self._client_gone():
            return
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if request_id is None:
            request_id = getattr(self, "_fault_request_id", None)
        seconds = retry_after_seconds(retry_after)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        if seconds is not None:
            # A8: the one non-200 fact the share relays besides its own
            # stable error wrapper. Validated seconds only; never a raw
            # upstream header value.
            self.send_header("Retry-After", str(seconds))
        if request_id is not None:
            self.send_header(X_ATTRIBUTION_REQUEST_ID, request_id)
        self.end_headers()
        self.wfile.write(payload)

    def error(self, status, code, *, phase=None, request_id=None,
              upstream_status=None, exception_type=None,
              stream_started=False, retry_after=None, error_type=None):
        """Fail ONE request: record its bounded evidence, then reply.

        ``phase`` is the frozen classification of the failure; a call without a
        phase (the pre-share 404 route rejection, which the frozen mapping does
        not classify) keeps the public behavior and records nothing. The
        correlation header is added only when a request id exists - a
        pre-attribution failure has none. ``retry_after`` is the upstream
        retry hint; only a validated integer number of seconds survives.
        Review #13: ``error_type`` names the stable refusal type (for the
        backend's retryable dispatch refusal); every other failure keeps the
        historical ``share_gateway_error`` type.
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
        if self._client_gone():
            return
        error = {"message": code,
                 "type": error_type or "share_gateway_error",
                 "code": code}
        if upstream_status is not None:
            # The upstream status is bounded evidence, not a body: it tells a
            # caller whether the failure was a rejected request or an outage.
            error["upstream_status"] = upstream_status
        self.reply(status, {"error": error}, request_id=request_id,
                   retry_after=retry_after)

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

    def _responses_event(self, name, payload):
        self.wfile.write(("event: " + name + "\ndata: "
                          + json.dumps(payload) + "\n\n").encode())
        self.wfile.flush()

    def relay_responses(self, data, attribution_headers, inflight_ctx=None):
        """Relay one Responses request to the dedicated backend's native route.

        The share entry validates the narrow Responses field contract and then
        forwards the ORIGINAL Responses body to chat2api's /v1/responses. That
        backend owns the single Responses -> Chat translation used by both the
        private and the shared chain, so the share never keeps a second copy of the
        translation (and cannot drift from it).
        """
        if self.server.probe:
            # Probe mode is a connectivity test: it must never borrow the UM
            # session. The text subset answers locally, exactly like the chat
            # and Messages probe shapes.
            response_id = "resp_" + secrets.token_hex(12)
            created_at = int(time.time())
            text = "连接测试成功；尚未接入 UM。"
            if data.get("stream") is True:
                message_id = "msg_" + secrets.token_hex(12)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                if self._fault_request_id is not None:
                    self.send_header(X_ATTRIBUTION_REQUEST_ID,
                                      self._fault_request_id)
                self.end_headers()
                events = [
                    ("response.created", {
                        "type": "response.created",
                        "response": {"id": response_id, "object": "response",
                                     "created_at": created_at,
                                     "status": "in_progress",
                                     "model": data["model"], "output": []}}),
                    ("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {"type": "message", "id": message_id,
                                 "status": "in_progress",
                                 "role": "assistant", "content": []}}),
                    ("response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "item_id": message_id, "output_index": 0,
                        "content_index": 0, "delta": text}),
                    ("response.output_item.done", {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {"type": "message", "id": message_id,
                                 "status": "completed",
                                 "role": "assistant",
                                 "content": [{"type": "output_text",
                                              "text": text,
                                              "annotations": []}]}}),
                    ("response.completed", {
                        "type": "response.completed",
                        "response": {"id": response_id,
                                     "object": "response",
                                     "created_at": created_at,
                                     "status": "completed",
                                     "model": data["model"],
                                     "output": [{
                                         "type": "message", "id": message_id,
                                         "status": "completed",
                                         "role": "assistant",
                                         "content": [{
                                             "type": "output_text",
                                             "text": text,
                                             "annotations": []}]}],
                                     "usage": {"input_tokens": 0,
                                               "output_tokens": 0,
                                               "total_tokens": 0}}}),
                ]
                for name, payload in events:
                    self._responses_event(name, payload)
                return True
            self.reply(200, {
                "id": response_id, "object": "response",
                "created_at": created_at, "status": "completed",
                "model": data["model"],
                "output": [{"type": "message", "id": "msg_" + secrets.token_hex(12),
                            "status": "completed", "role": "assistant",
                            "content": [{"type": "output_text", "text": text,
                                         "annotations": []}]}],
                "usage": {"input_tokens": 0, "output_tokens": 0,
                          "total_tokens": 0}})
            return True
        upstream = None
        started = False
        try:
            body = json.dumps(data).encode()
            stream_flag = data.get("stream") is True
            if self._relay_refused(inflight_ctx):
                self._dispatch_refusal_error()
                return False
            upstream = http.client.HTTPConnection(
                "127.0.0.1", self.server.upstream_port,
                timeout=(STREAM_SOCKET_TIMEOUT_S if stream_flag
                         else NONSTREAM_SOCKET_TIMEOUT_S))
            if inflight_ctx is not None:
                inflight_ctx.attach_upstream(upstream)
            headers = {"Content-Type": "application/json",
                       "Connection": "close"}
            headers.update(attribution_headers)
            if self._relay_refused(inflight_ctx):
                self._dispatch_refusal_error()
                return False
            upstream.request("POST", RESPONSES_PATH, body=body,
                             headers=headers)
            if inflight_ctx is not None:
                inflight_ctx.mark_forwarded()
            response = upstream.getresponse()
            if response.status != 200:
                # A8: relay only a validated integer retry hint; the upstream
                # error body and credential headers are never forwarded.
                retry_after = response.getheader("Retry-After")
                if self._relay_dispatch_refusal(response):
                    return False
                if response.status in (400, 422):
                    # A rejected request is NOT a transient upstream outage:
                    # pass its own status through so a retry-capable client
                    # cannot replay the same rejected payload forever.
                    self.error(response.status, "upstream_request_rejected",
                                phase="upstream",
                                upstream_status=response.status,
                                retry_after=retry_after)
                else:
                    self.error(503 if response.status == 503 else 502,
                                "um_unavailable", phase="upstream",
                                upstream_status=response.status,
                                retry_after=retry_after)
                return False
            content_type = response.getheader("Content-Type", "").split(";")[0].lower()
            if content_type not in {"application/json", "text/event-stream"}:
                self.error(502, "unexpected_upstream_response",
                            phase="upstream")
                return False
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            if self._fault_request_id is not None:
                self.send_header(X_ATTRIBUTION_REQUEST_ID,
                                  self._fault_request_id)
            self.end_headers()
            started = True
            while True:
                chunk = response.read1(16384)
                if not chunk:
                    return True
                if inflight_ctx is not None and inflight_ctx.socket_closed:
                    return started
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException) as exc:
            if not started:
                try:
                    self.error(502, "um_connection_failed", phase="upstream",
                                exception_type=type(exc).__name__,
                                stream_started=False)
                except OSError:
                    pass
            else:
                self._record_fault(phase="stream", status=None,
                                    code="stream_aborted",
                                    exception_type=type(exc).__name__,
                                    stream_started=True)
            return started
        finally:
            if inflight_ctx is not None:
                inflight_ctx.close_upstream()
            if upstream is not None:
                upstream.close()

    def handle_api(self):
        self.close_connection = True
        # A9-6: this request's fault evidence anchor (duration + the
        # server-generated correlation id, set again after attribution).
        self._fault_started = time.monotonic()
        self._fault_request_id = None
        # Only the catalog and Messages have narrow query exceptions. Every
        # other path retains exact raw-path matching, including encodings.
        catalog_limit = None
        if self.command == "GET":
            try:
                catalog_limit = model_catalog_limit(self.path)
            except ValueError:
                self.error(404, "route_not_shared")
                return
        elif self.command == "POST" and self.path.startswith(MESSAGES_PATH):
            try:
                messages_query(self.path)
            except ValueError:
                self.error(404, "route_not_shared")
                return
        elif (self.command, self.path) not in ALLOWED:
            self.error(404, "route_not_shared")
            return
        # Discovery and Messages use the same key identity and fail-closed
        # duplicate/conflicting-header checks. Neither credential is forwarded.
        credential = resolve_share_credential(
            self.headers,
            allow_api_key=self.command == "GET"
            or self.path.startswith(MESSAGES_PATH))
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
        inflight_ctx = _InflightCtx(
            self.connection, retryable_refusal=self._is_dispatch_request())
        self._inflight_ctx = inflight_ctx
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
                if self.path.startswith(MESSAGES_PATH):
                    # Anthropic Messages uses the SAME admission chain as chat
                    # (key digest -> load_config -> size cap -> acquire) and
                    # only swaps the body contract. The query gate above
                    # already accepted only the SDK's beta flag.
                    if not valid_messages_request(data):
                        self.error(400, "unsupported_messages_request",
                                    phase="body")
                        return
                elif self.path == RESPONSES_PATH:
                    # Responses is translated onto the chat upstream; the share
                    # never forwards the Responses wire format to chat2api.
                    if not valid_responses_request(data):
                        self.error(400, "unsupported_responses_request",
                                    phase="body")
                        return
                    started_response = self.relay_responses(
                        data, attribution_headers, inflight_ctx)
                    return
                elif (not isinstance(data, dict) or set(data) - CHAT_FIELDS
                        or not isinstance(data.get("model"), str) or not data["model"]
                        or not isinstance(data.get("messages"), list) or not data["messages"]
                        or not isinstance(data.get("stream", False), bool)
                        or "store" in data and data["store"] is not False):
                    self.error(400, "unsupported_chat_request", phase="body")
                    return
                if not self.path.startswith(MESSAGES_PATH):
                    # Harness sends store=false by default. Accept the explicit
                    # opt-out, but do not request storage semantics upstream.
                    data.pop("store", None)
                body = json.dumps(data).encode()
            elif length:
                self.error(400, "unexpected_body", phase="body")
                return
            if self.server.probe:
                if self.command == "GET":
                    self.reply(200, {"object": "list", "data": [{"id": "connectivity-test", "object": "model", "owned_by": "local-probe"}]})
                elif self.path.startswith(MESSAGES_PATH):
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
                                                      "text": "连接测试成功；尚未接入 UM。"}],
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
                    self.reply(200, {"id": "probe", "object": "chat.completion", "model": "connectivity-test", "choices": [{"index": 0, "message": {"role": "assistant", "content": "连接测试成功；尚未接入 UM。"}, "finish_reason": "stop"}]})
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
                if self._relay_refused(inflight_ctx):
                    self.error(503, "share_instance_stopped", phase="upstream")
                    return
                upstream = http.client.HTTPConnection(
                    "127.0.0.1", self.server.upstream_port,
                    timeout=NONSTREAM_SOCKET_TIMEOUT_S)
                if inflight_ctx is not None:
                    inflight_ctx.attach_upstream(upstream)
                # The attribution marker is ADDED to the forwarded request (the
                # whitelist below is the only source of relayed headers, so a
                # client-supplied header of the same name is dropped).
                model_headers = {"Connection": "close",
                                 X_MODELS_HEADER: RAW_FRESH_VALUE}
                model_headers.update(attribution_headers)
                if self._relay_refused(inflight_ctx):
                    self.error(503, "share_instance_stopped", phase="upstream")
                    return
                upstream.request("GET", "/v1/models", headers=model_headers)
                response = upstream.getresponse()
                if response.status != 200:
                    self.error(503 if response.status == 503 else 502,
                                "um_unavailable", phase="upstream",
                                upstream_status=response.status,
                                retry_after=response.getheader("Retry-After"))
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
                    payload["data"] = data if catalog_limit is None else data[:catalog_limit]
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
            if self._relay_refused(inflight_ctx):
                self._dispatch_refusal_error()
                return
            upstream = http.client.HTTPConnection(
                "127.0.0.1", self.server.upstream_port,
                timeout=(STREAM_SOCKET_TIMEOUT_S if stream_flag
                         else NONSTREAM_SOCKET_TIMEOUT_S))
            if inflight_ctx is not None:
                inflight_ctx.attach_upstream(upstream)
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
            if self.path.startswith(MESSAGES_PATH):
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
            # The SDK's beta query is consumed locally; chat2api receives the
            # canonical Messages path, never the client's query string.
            upstream_path = (MESSAGES_PATH
                             if self.path.startswith(MESSAGES_PATH)
                             else self.path)
            if self._relay_refused(inflight_ctx):
                self._dispatch_refusal_error()
                return
            upstream.request(self.command, upstream_path, body=body,
                             headers=relay_headers)
            if inflight_ctx is not None:
                inflight_ctx.mark_forwarded()
            response = upstream.getresponse()
            if response.status != 200:
                if self._relay_dispatch_refusal(response):
                    return
                self.error(503 if response.status == 503 else 502,
                            "um_unavailable", phase="upstream",
                            upstream_status=response.status,
                            retry_after=response.getheader("Retry-After"))
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
                if inflight_ctx is not None and inflight_ctx.socket_closed:
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
            if inflight_ctx is not None:
                inflight_ctx.close_upstream()
            if upstream is not None:
                upstream.close()
            # Precise release (R6-A2): decrement THIS request's reservation;
            # a key with several in-flight requests keeps its counter until
            # the last one ends.
            self.server.release(client)


def main(argv=None):
    parser = argparse.ArgumentParser(description="UM 熟人分享入口（仅本机监听）")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--upstream-port", type=int, default=None,
                        help="本机上游 chat2api 端口；必填、无默认值——分享入口"
                             "必须显式声明专用分享后端端口（生产 8110，由 "
                             "deploy-elevated.ps1 的 ShareBackendPort 传入），"
                             "绝不静默落到私人 chat2api 的 8000")
    parser.add_argument("--probe", action="store_true", help="只返回连通性测试内容，不调用 UM")
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
                        default=None,
                        help="实例管道名前缀（缺省按平台取默认值：Windows 命名管道，"
                             "POSIX 为 AF_UNIX 套接字路径前缀）")
    parser.add_argument("--instance-pipe-sddl", default=None,
                        help="实例管道 DACL（SDDL）；实验装配必须显式提供，"
                             "占位 SID 转换失败即拒绝服务（绝不使用默认安全描述符）")
    parser.add_argument("--registration-file", type=Path, default=None,
                        help="实例自发布登记文件（run\\live\\instance-"
                             "registration.json）；启用实例通道时必填")
    args = _parse_args(parser, argv)
    if args.add_client or args.revoke_client:
        # Removed as a second writer: direct JSON mutation would bypass the
        # single-authority management store. Operators must use share/admin.py
        # against the management service (see R4).
        print("ERROR: --add-client/--revoke-client no longer mutate config "
              "directly. Use share/admin.py against the single-authority "
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
            from share.instance_pipe import (start_instance_channel,
                                             remove_registration)
            secret_module = _load_share_secret_protection()
            instance_secret = secret_module.read_private_secret_path(
                args.instance_key_file, max_bytes=128,
                label="instance key").decode("utf-8").strip()
            if not instance_secret:
                raise ValueError("instance key is empty")
        except ImportError as exc:
            instance_channel = {"ready": False,
                                "error": f"instance channel module "
                                         f"unavailable: {exc}"}
        except (OSError, ValueError) as exc:
            instance_channel = {"ready": False,
                                "error": f"instance key unreadable: "
                                         f"{type(exc).__name__}"}
        else:
            try:
                instance_channel = start_instance_channel(
                    server, instance_secret,
                    args.instance_pipe_prefix, args.registration_file,
                    sddl=args.instance_pipe_sddl)
            except OSError as exc:
                instance_channel = {"ready": False,
                                    "error": f"instance channel "
                                             f"unavailable: "
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
    print(f"UM 分享入口 http://127.0.0.1:{args.port}；上游后端 "
          f"http://127.0.0.1:{args.upstream_port}；模式："
          f"{'连通性测试' if args.probe else 'UM 转发'}；请求归因："
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


def _load_share_secret_protection():
    """Load the repo-root secret adapter independent of sys.path.

    Same seam as ``_load_nonstream_aggregate``/``_load_share_dispatch`` in
    chat2api: the service runs this file as a script from ``chat2api/`` (the
    repository root is NOT on sys.path), while the adapter lives one directory
    up. A plain ``from share_secret_protection import ...`` therefore raised
    ImportError there and the fail-closed handlers silently denied the
    attribution and instance keys. Reuse the module when it is already imported
    (tests, or a flat deployment), otherwise load the parent-directory file by
    path. No secret is read here.
    """
    import importlib.util
    module = (sys.modules.get("share.secret_protection")
              or sys.modules.get("share_secret_protection"))
    if module is not None:
        return module
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        os.pardir, "share/secret_protection.py")
    spec = importlib.util.spec_from_file_location(
        "share_secret_protection", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["share_secret_protection"] = module
    spec.loader.exec_module(module)
    return module


def default_instance_pipe_prefix():
    """The platform default instance-channel prefix.

    ``share_instance_pipe`` lives in the repository root and resolves the
    Windows named-pipe prefix or the POSIX AF_UNIX socket path prefix. The
    import is lazy so this module still loads when only ``chat2api/`` is on
    sys.path; the caller treats an unavailable module as an explicit not-ready
    state instead of guessing a prefix.
    """
    from share.instance_pipe import default_instance_pipe_prefix as resolve
    return resolve()


def _default_instance_pipe_prefix():
    """Internal alias so the resolver and tests share one default seam."""
    return default_instance_pipe_prefix()


def _resolve_instance_pipe_prefix(value):
    """Explicit ``--instance-pipe-prefix`` wins; otherwise the platform default.

    The parser default is None so "not supplied" is distinguishable from an
    explicit value. Resolution happens at parse time; a missing instance-channel
    module yields None (the HTTP share still serves and the instance channel
    reports itself not ready) rather than a guessed prefix.
    """
    if value is not None:
        return value
    try:
        return _default_instance_pipe_prefix()
    except (ImportError, OSError, AttributeError):
        return None


def _parse_args(parser, argv=None):
    """Parse the CLI and resolve the effective instance pipe prefix.

    ``parser`` is the real parser built by ``main``; the explicit value is never
    replaced, and an omitted value is resolved through
    ``_resolve_instance_pipe_prefix`` at parse time.
    """
    args = parser.parse_args(argv)
    args.instance_pipe_prefix = _resolve_instance_pipe_prefix(
        args.instance_pipe_prefix)
    return args


if __name__ == "__main__":
    main()
