r"""share_broker_protocol.py — schema v2 for the REAL broker control plane.

Pure protocol layer shared by both control channels (D0 §27.5):

* the MANAGEMENT pipe (\\.\pipe\um-share-public-broker): an ordinary-privilege
  8096 client talks to a SYSTEM broker host; fixed actions only, no paths,
  commands, ports or envs (A2).
* the INSTANCE pipe (\\.\pipe\um-share-public-instance-<id>): the SYSTEM broker
  talks to the in-process control endpoint of a share instance; the share
  can only report itself and ACK a loaded projection — it can never invoke
  management actions (A2/A4 privilege separation).

Transport-agnostic: framing, size bounds, HMAC, replay/ts windows and the
total-deadline read (R7-deadline semantics: re-derive remaining time from a
monotonic deadline before EVERY recv) live here; transports (named pipe,
loopback TCP for offline tests) only move bytes. The named-pipe transport
depends on UNVERIFIED OS assumptions (§27.7 H1–H6) and is therefore a
replaceable adapter — offline tests use loopback/in-memory transports and
never count as OS permission evidence.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time

SCHEMA_VERSION = 2
MAX_MESSAGE = 1 * 1024 * 1024          # request/response bound (D0 §27.5)
TS_WINDOW_SECONDS = 300
DEFAULT_DEADLINE_SECONDS = 10.0

# The graceful close is a BOUNDED two-phase exchange (contract §9.2): the
# in-flight drain WINDOW, then the RESERVED final stop + closure verification.
# BOTH sides read these numbers - the broker may legitimately spend their sum,
# so the client's exchange deadline must cover it. A client that gave up earlier
# would abandon a drain the broker is still performing, and the retry would race
# the in-flight stop. The sum stays inside the wrapper's 130 s dispatch budget.
DRAIN_WINDOW_SECONDS = 60.0
STOP_BUDGET_SECONDS = 20.0
# Client-side exchange deadlines: the broker-side work plus a bounded margin for
# the transport and the instance notification, capped at the frozen 180 s bound.
DRAIN_DEADLINE_SECONDS = DRAIN_WINDOW_SECONDS + STOP_BUDGET_SECONDS + 10.0
EMERGENCY_DEADLINE_SECONDS = STOP_BUDGET_SECONDS + 10.0

# ts-service-control: the restricted TS service verb (whitelist extension).
# The action enum and the service name are PROTOCOL CONSTANTS - fixed here,
# never caller supplied parameters.
TS_SERVICE_CONTROL_ACTION = "ts-service-control"
TS_SERVICE_CONTROL_ACTIONS = frozenset(("start", "stop"))
TS_SERVICE_NAME = "Tailscale"

# host-verify / host-stop: the restricted share-host evidence verbs
# (whitelist extension). The host evidence queries (schtasks/netstat/CIM)
# need SYSTEM privilege, so they run INSIDE the broker and are exposed as
# read-only (verify) / task-gate (stop) verbs; the task name, image and port
# never cross the wire - the broker-side WindowsHostOps pins them to the
# deployment configuration.
HOST_VERIFY_ACTION = "host-verify"
HOST_VERIFY_CHECKS = frozenset(("running", "closed"))
HOST_STOP_ACTION = "host-stop"

# Temporary maintenance lease (A9-3 8110 restart pauses 8100 acceptance via
# this lease; A9-4 8100 restart carries its own maintenance record). The
# lease is a MAINTENANCE TRANSIENT, never a confirmed stop: it binds
# (component, instance_id, round, expires_at) and a stopped instance can
# never be re-opened by it.
MAINTENANCE_BEGIN_ACTION = "maintenance-begin"
MAINTENANCE_END_ACTION = "maintenance-end"
MAINTENANCE_STATUS_ACTION = "maintenance-status"
# 8100 single-layer restart (A9-4); the broker pins the host task/port and
# the wire carries the component enum + a bounded budget only.
GATEWAY_RESTART_ACTION = "gateway-restart"
# Restricted host-side READ-ONLY evidence (security gate + 8100 fault tail),
# same whitelist discipline as host-verify: the wire carries the check enum
# only, the object (task/run root/port/fault file) stays broker-side
# deployment configuration.
HOST_EVIDENCE_ACTION = "host-evidence"
HOST_EVIDENCE_CHECKS = frozenset(("limits", "faults"))

# Component enum: protocol constants; the concrete object is pinned by the
# broker-side deployment configuration (the TS_SERVICE_NAME pattern).
COMPONENT_SHARE_GATEWAY = "share_gateway"      # 8100
COMPONENT_SHARE_BACKEND = "share_backend"      # 8110 (no lease this round)
MAINTENANCE_COMPONENTS = frozenset((COMPONENT_SHARE_GATEWAY,))
GATEWAY_RESTART_COMPONENTS = frozenset((COMPONENT_SHARE_GATEWAY,))

MAINTENANCE_LEASE_DEFAULT_S = 300
MAINTENANCE_LEASE_MAX_S = 600
GATEWAY_RESTART_BUDGET_DEFAULT_S = 120
GATEWAY_RESTART_BUDGET_MAX_S = 180

# Instance-side verb: RESUME acceptance after a bounded drain (pause reuses
# the EXISTING drain verb). An instance that does not know this verb answers
# bad_request/"unknown action"; the broker maps that to capability_missing and
# NEVER downgrades to a host stop/start of the whole chain.
INSTANCE_RESUME_ACTION = "resume"

# Management actions (8096 -> SYSTEM broker). Fixed schema, no caller
# supplied paths, commands, PIDs, ports, URLs or environment variables.
# R8-A7 revoke-and-terminate: a publish may carry an OPTIONAL
# terminate_keys list (key_ids whose in-flight share requests must be
# interrupted immediately). Optional fields are declared as (type,)
# tuples in the schemas below; a missing optional field is legal, a
# present one is validated against the tuple's type. One-shot per
# publish: the list never persists beyond this publication.
MANAGEMENT_ACTIONS = {
    "status": {},
    "publish": {"generation": int, "fingerprint": str, "projection": dict,
                "terminate_keys": (list,)},
    "start": {},
    "drain": {},
    "emergency-stop": {},
    # Restricted whitelist extension (console share production adapter,
    # deployment authorization package section 5): TS service start/stop.
    # The ONLY wire field is the fixed action enum below; the SERVICE NAME
    # never crosses the wire at all - the broker-side primitive pins it to
    # the TS_SERVICE_NAME literal, so no page can inject a service name.
    TS_SERVICE_CONTROL_ACTION: {"action": str},
    # Restricted host-evidence extension: the ONLY wire field is the fixed
    # check enum; the task/port/image identity stays broker-side deployment
    # configuration.
    HOST_VERIFY_ACTION: {"check": str},
    HOST_STOP_ACTION: {},
    # Temporary maintenance lease (A9-3/A9-4 broker side). The generic layer
    # checks field presence/type only; the SPECIALIZED validators below
    # enforce the component enum and the bounded lease_s/budget_s (the
    # generic int rule has no upper bound).
    MAINTENANCE_BEGIN_ACTION: {"component": str, "lease_s": int},
    MAINTENANCE_END_ACTION: {"component": str, "lease_id": str},
    MAINTENANCE_STATUS_ACTION: {},
    GATEWAY_RESTART_ACTION: {"component": str, "budget_s": int},
    # Restricted host-evidence extension (read-only): the ONLY wire field is
    # the fixed check enum (limits/faults).
    HOST_EVIDENCE_ACTION: {"check": str},
}

# Instance actions (SYSTEM broker -> share in-process endpoint). The share
# answers with its own identity and in-flight state; it has NO management
# ability. A `load` request tells the instance which generation/fingerprint
# the writer has atomically placed at its fixed config path; the instance
# installs it under the accept lock and its response IS the ACK.
INSTANCE_ACTIONS = {
    "instance-status": {},
    "load": {"generation": int, "fingerprint": str,
             "terminate_keys": (list,)},
    "drain": {},
    "emergency": {},
    # Resume acceptance after a bounded drain (the maintenance lease end /
    # lazy expiry). A stopped instance answers blocked - a terminal stop is
    # NEVER re-opened by a maintenance lease.
    INSTANCE_RESUME_ACTION: {},
}

_RESULT_CODES = (
    "ok", "not_installed", "stale_generation", "fingerprint_mismatch",
    "instance_mismatch", "replay", "stale_ts", "overlimit",
    "deadline_exceeded", "unconfirmed", "blocked", "bad_request",
    "forbidden", "internal_error", "empty_request", "unknown_instance",
    # The instance does not know a requested verb (old instance answered
    # bad_request/"unknown action"): an EXPLICIT capability refusal that is
    # NEVER downgraded to a host stop/start or a whole-chain restart.
    "capability_missing",
    # R13-T: the ts-service-control verb answers this when the OS refused
    # the service control command with ERROR_ACCESS_DENIED (a PERMANENT
    # 权限不足 refusal - never folded into the pending "unconfirmed" shape,
    # which would only burn the console's bounded confirmation poll).
    "access_denied",
)
RESULT_CODES = frozenset(_RESULT_CODES)

MANAGEMENT_SIGNED_KEYS = ("v", "action", "request_id", "ts", "fields")
INSTANCE_SIGNED_KEYS = ("v", "action", "request_id", "ts", "instance_id",
                        "fields")


def _mac(secret: bytes, payload: bytes) -> str:
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def sign(secret: str, body: dict, signed_keys) -> dict:
    body.pop("mac", None)
    body["mac"] = _mac(secret.encode(), json.dumps(
        {k: body[k] for k in signed_keys if k in body},
        sort_keys=True).encode())
    return body


def build_request(secret: str, action: str, fields=None,
                  instance_id=None, now=None):
    """Build a signed request for the given channel.

    The request id is a RANDOM nonce (uuid4), not a hash of the content:
    two legitimate status polls within the same second must not collide,
    and each new transport exchange carries its own nonce (R29-3). Replay
    protection is the SERVER's one-time consumption of that nonce, which is
    separate from any business idempotency key.
    """
    import uuid
    body = {"v": SCHEMA_VERSION, "action": action,
            "request_id": str(uuid.uuid4()),
            "ts": int(now if now is not None else time.time()),
            "fields": fields or {}}
    if instance_id is not None:
        body["instance_id"] = instance_id
    return sign(secret, body,
                INSTANCE_SIGNED_KEYS if instance_id is not None
                else MANAGEMENT_SIGNED_KEYS)


def _check_common(body: dict, secret: str, actions: dict, signed_keys,
                  require_instance: bool, now=None) -> dict:
    if not isinstance(body, dict):
        raise ProtocolError("bad_request", "request must be an object")
    if body.get("v") != SCHEMA_VERSION:
        raise ProtocolError("bad_request", "unsupported protocol version")
    action = body.get("action")
    if action not in actions:
        raise ProtocolError("bad_request", "unknown action")
    ts = body.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, int):
        raise ProtocolError("bad_request", "request must include integer ts")
    now = int(now if now is not None else time.time())
    if abs(ts - now) > TS_WINDOW_SECONDS:
        raise ProtocolError("stale_ts", "request timestamp outside window")
    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise ProtocolError("bad_request", "invalid request_id")
    instance_id = body.get("instance_id")
    if require_instance:
        if not isinstance(instance_id, str) or not 1 <= len(instance_id) <= 64:
            raise ProtocolError("bad_request", "invalid instance_id")
    elif instance_id is not None:
        raise ProtocolError("bad_request", "management request must not "
                                            "carry instance_id")
    expected = _mac(secret.encode(), json.dumps(
        {k: body[k] for k in signed_keys if k in body},
        sort_keys=True).encode())
    supplied = body.get("mac", "")
    if not isinstance(supplied, str) or \
            not hmac.compare_digest(expected, supplied):
        raise ProtocolError("forbidden", "forged or missing mac")
    fields = body.get("fields")
    if not isinstance(fields, dict):
        raise ProtocolError("bad_request", "fields must be an object")
    allowed = actions[action]
    unknown = set(fields) - set(allowed)
    if unknown:
        raise ProtocolError("bad_request",
                            f"unknown field(s): {sorted(unknown)}")
    normalized = {}
    for key, expected_type in allowed.items():
        optional = isinstance(expected_type, tuple)
        if optional:
            expected_type = expected_type[0]
        if key not in fields:
            if optional:
                continue
            raise ProtocolError("bad_request", f"missing field: {key}")
        value = fields[key]
        if expected_type is list:
            if not isinstance(value, list) or not 1 <= len(value) <= 16                     or any(not isinstance(v, str) or not 1 <= len(v) <= 64
                           for v in value):
                raise ProtocolError(
                    "bad_request", f"field {key} must be a list of "
                    "1..16 key ids (each 1..64 chars)")
        elif expected_type is int:
            if isinstance(value, bool) or not isinstance(value, int) \
                    or value < 1:
                raise ProtocolError("bad_request", f"field {key} must be a "
                                                   "positive integer")
        elif expected_type is str:
            if not isinstance(value, str) or not 1 <= len(value) <= 128:
                raise ProtocolError("bad_request", f"field {key} must be a "
                                                   "1..128 char string")
        elif expected_type is dict:
            if not isinstance(value, dict):
                raise ProtocolError("bad_request", f"field {key} must be an "
                                                   "object")
        normalized[key] = value
    return {"action": action, "request_id": request_id,
            "instance_id": instance_id, "ts": ts, "fields": normalized}


def validate_management_request(body: dict, secret: str, now=None) -> dict:
    """Validate + authenticate a management-channel request (8096 -> broker)."""
    return _check_common(body, secret, MANAGEMENT_ACTIONS,
                         MANAGEMENT_SIGNED_KEYS, require_instance=False,
                         now=now)


def validate_ts_service_control_fields(fields: dict) -> dict:
    """Fixed whitelist for the ts-service-control verb: exactly one field,
    ``action``, limited to the TS_SERVICE_CONTROL_ACTIONS enum.

    A field whitelist on top of the generic schema check keeps the verb
    closed: no service name, no extra parameter and no unknown value can
    ever reach the broker primitive - the service name is the broker-side
    TS_SERVICE_NAME literal, never a wire parameter."""
    unknown = set(fields) - {"action"}
    if unknown:
        raise ProtocolError("bad_request",
                            f"unknown field(s): {sorted(unknown)}")
    action = fields.get("action")
    if action not in TS_SERVICE_CONTROL_ACTIONS:
        raise ProtocolError(
            "bad_request",
            f"ts service action must be one of "
            f"{sorted(TS_SERVICE_CONTROL_ACTIONS)}")
    return {"action": action}


def validate_host_verify_fields(fields: dict) -> dict:
    """Fixed whitelist for the host-verify verb: exactly one field, ``check``,
    limited to the HOST_VERIFY_CHECKS enum. The verdict itself (task state,
    gateway processes, port listener) is computed entirely on the broker
    side from its own deployment configuration - the wire carries the check
    enum only."""
    unknown = set(fields) - {"check"}
    if unknown:
        raise ProtocolError("bad_request",
                            f"unknown field(s): {sorted(unknown)}")
    check = fields.get("check")
    if check not in HOST_VERIFY_CHECKS:
        raise ProtocolError(
            "bad_request",
            f"host verify check must be one of {sorted(HOST_VERIFY_CHECKS)}")
    return {"check": check}


def validate_host_stop_fields(fields: dict) -> dict:
    """Fixed whitelist for the host-stop verb: NO fields at all. The task
    gate is the broker-side WindowsHostOps over its deployment-configured
    task name - nothing about it can be steered from the wire."""
    unknown = set(fields)
    if unknown:
        raise ProtocolError("bad_request",
                            f"unknown field(s): {sorted(unknown)}")
    return {}


def _reject_unknown_fields(fields, allowed):
    """The shared unknown-key gate for the specialized whitelists below: an
    unknown field is a bad_request, never silently ignored. This is NOT a
    generic field-TYPE validator - every caller below still validates its own
    values explicitly."""
    unknown = set(fields) - set(allowed)
    if unknown:
        raise ProtocolError("bad_request",
                            f"unknown field(s): {sorted(unknown)}")


def _bounded_int(fields, key, maximum):
    """A required positive integer field bounded by ``maximum``: out-of-range
    values are bad_request (NO silent clamp; the generic schema layer has no
    upper bound at all)."""
    value = fields.get(key)
    if isinstance(value, bool) or not isinstance(value, int) \
            or not 1 <= value <= maximum:
        raise ProtocolError("bad_request",
                            f"{key} must be an integer in 1..{maximum}")
    return value


def _enum_field(fields, key, allowed):
    """A required field limited to the given fixed enum: an unknown value (or a
    non-string value) is a bad_request."""
    value = fields.get(key)
    if not isinstance(value, str) or value not in allowed:
        raise ProtocolError("bad_request",
                            f"{key} must be one of {sorted(allowed)}")
    return value


def validate_maintenance_begin_fields(fields: dict) -> dict:
    """Exactly {component: MAINTENANCE_COMPONENTS enum, lease_s:
    1..MAINTENANCE_LEASE_MAX_S}; an unknown field, an unknown component or
    an out-of-range lease_s is a bad_request (no silent clamp)."""
    _reject_unknown_fields(fields, ("component", "lease_s"))
    return {"component": _enum_field(fields, "component",
                                    MAINTENANCE_COMPONENTS),
            "lease_s": _bounded_int(fields, "lease_s",
                                    MAINTENANCE_LEASE_MAX_S)}


def validate_maintenance_end_fields(fields: dict) -> dict:
    """Exactly {component: MAINTENANCE_COMPONENTS enum, lease_id: 1..64
    char string}; unknown fields, unknown components and malformed lease ids
    are bad_request."""
    _reject_unknown_fields(fields, ("component", "lease_id"))
    lease_id = fields.get("lease_id")
    if not isinstance(lease_id, str) or not 1 <= len(lease_id) <= 64:
        raise ProtocolError("bad_request",
                            "lease_id must be a 1..64 char string")
    return {"component": _enum_field(fields, "component",
                                    MAINTENANCE_COMPONENTS),
            "lease_id": lease_id}


def validate_maintenance_status_fields(fields: dict) -> dict:
    """Fixed whitelist for maintenance-status: NO fields at all (same
    discipline as validate_host_stop_fields)."""
    unknown = set(fields)
    if unknown:
        raise ProtocolError("bad_request",
                            f"unknown field(s): {sorted(unknown)}")
    return {}


def validate_gateway_restart_fields(fields: dict) -> dict:
    """Exactly {component: GATEWAY_RESTART_COMPONENTS enum, budget_s:
    1..GATEWAY_RESTART_BUDGET_MAX_S}; unknown fields, unknown components
    and out-of-range budgets are bad_request (no silent clamp)."""
    _reject_unknown_fields(fields, ("component", "budget_s"))
    return {"component": _enum_field(fields, "component",
                                    GATEWAY_RESTART_COMPONENTS),
            "budget_s": _bounded_int(fields, "budget_s",
                                     GATEWAY_RESTART_BUDGET_MAX_S)}


def validate_host_evidence_fields(fields: dict) -> dict:
    """Exactly {check: HOST_EVIDENCE_CHECKS enum}; the concrete object the
    check reads (watchdog script, run root, firewall rule, fault file) stays
    broker-side deployment configuration - nothing about it crosses the wire."""
    _reject_unknown_fields(fields, ("check",))
    return {"check": _enum_field(fields, "check", HOST_EVIDENCE_CHECKS)}


def validate_instance_request(body: dict, secret: str, now=None) -> dict:
    """Validate + authenticate an instance-channel request (broker -> share)."""
    return _check_common(body, secret, INSTANCE_ACTIONS,
                         INSTANCE_SIGNED_KEYS, require_instance=True,
                         now=now)


class ProtocolError(Exception):
    """A bounded, sanitized protocol rejection (single code, no secrets)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        if code not in RESULT_CODES:
            raise ValueError(f"unknown result code: {code}")


class BrokerResult:
    """Structured broker result — an arbitrary dict never counts as
    completion (D1/A4); every outcome carries an explicit result code."""

    def __init__(self, ok: bool, error_code: str, result=None):
        if error_code not in RESULT_CODES:
            raise ValueError(f"unknown result code: {error_code}")
        self.ok = bool(ok)
        self.error_code = error_code
        self.result = result or {}

    @classmethod
    def ok(cls, result=None):
        return cls(True, "ok", result)

    @classmethod
    def err(cls, code: str, detail=""):
        return cls(False, code, {"detail": detail} if detail else {})

    def to_dict(self):
        return {"ok": self.ok, "error_code": self.error_code,
                "result": self.result}

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            return cls.err("internal_error", "response must be an object")
        code = data.get("error_code")
        if code not in RESULT_CODES:
            return cls.err("internal_error", f"unknown result code {code!r}")
        return cls(data.get("ok") is True, code, data.get("result") or {})


def encode_frame(obj: dict) -> bytes:
    """Encode one JSON line, rejecting anything that could exceed the
    message bound before it ever reaches the wire."""
    line = json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n"
    data = line.encode("utf-8")
    if len(data) > MAX_MESSAGE:
        raise ProtocolError("overlimit", "encoded message exceeds bound")
    return data


def read_bounded_line(reader, deadline, on_before_recv=None) -> bytes:
    """Read exactly one line bounded by MAX_MESSAGE and the TOTAL deadline.

    R7-deadline semantics: a buffered readline resets the idle timeout on
    every underlying recv, so slow-dripping bytes could hold the accept loop
    far beyond the deadline. Read byte-by-byte and re-derive the remaining
    time from the monotonic deadline before every recv; ``on_before_recv``
    lets a transport apply the remaining time to the blocking read itself
    (e.g. socket.settimeout) so a drip client cannot park inside one recv
    beyond the deadline (R29-6). Raises ProtocolError ("overlimit",
    "empty_request") or TimeoutError on deadline expiry.
    """
    chunks = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request deadline exceeded")
        if on_before_recv is not None:
            on_before_recv(remaining)
        byte = reader.read(1)
        if not byte:
            raise ProtocolError(
                "empty_request" if not chunks else "deadline_exceeded",
                "connection closed mid-request")
        if byte == b"\n":
            break
        chunks += byte
        if len(chunks) > MAX_MESSAGE:
            raise ProtocolError("overlimit", "request line exceeds bound")
    return bytes(chunks) + b"\n"


def decode_frame(line: bytes) -> dict:
    if len(line) > MAX_MESSAGE:
        raise ProtocolError("overlimit", "frame exceeds bound")
    try:
        body = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ProtocolError("bad_request", "invalid frame encoding") from exc
    if not isinstance(body, dict):
        raise ProtocolError("bad_request", "frame must be an object")
    return body


def _monotonic():
    import time
    return time.monotonic()


# Projection schema versions accepted by the shared parser (R6-A2 dual-
# version compatibility): the ONE parsing rule for writer pre-check,
# control-end ACK and the HTTP auth loader's projection branch. Schema 2
# adds entry ``max_concurrency`` and top-level ``share_max_concurrency``;
# schema 1 projections keep being accepted and missing fields are filled
# with the LEGACY effective limits (per-key 1, global 2 = the old hardcodes)
# so every consumer sees a uniform shape. New deployments must write schema
# 2; nothing is downgraded to the legacy interpretation.
PROJECTION_SCHEMA_VERSIONS = frozenset((1, 2))
LEGACY_PROJECTION_SCHEMA_VERSION = 1
PROJECTION_DEFAULT_MAX_CONCURRENCY = 1
PROJECTION_DEFAULT_SHARE_MAX_CONCURRENCY = 2
PROJECTION_MAX_CONCURRENCY_MIN = 1
PROJECTION_MAX_CONCURRENCY_MAX = 64
PROJECTION_TOP_LEVEL_KEYS = frozenset(
    ("schema_version", "config_generation", "data", "share_max_concurrency"))
PROJECTION_ENTRY_KEYS = frozenset(("key_id", "name", "secret_hash",
                                   "secret_version", "enabled",
                                   "max_concurrency"))


def _parse_projection_limit(value, field):
    if isinstance(value, bool) or not isinstance(value, int) \
            or not (PROJECTION_MAX_CONCURRENCY_MIN <= value
                    <= PROJECTION_MAX_CONCURRENCY_MAX):
        raise ProtocolError(
            "bad_request",
            f"projection field {field} must be an integer in "
            f"{PROJECTION_MAX_CONCURRENCY_MIN}.."
            f"{PROJECTION_MAX_CONCURRENCY_MAX}")
    return value


def parse_projection_payload(payload) -> dict:
    """Shared FULL semantic projection parser (R31-6): the ONE projection
    parsing rule for writer pre-check, control-end ACK and the HTTP auth
    loader's projection branch — no consumer may be more lenient than the
    real authentication loader.

    Strict schema: bool is NOT a valid schema_version/generation (the
    ``True == 1`` trap), entry fields are a fixed whitelist (unknown entry
    fields are rejected, not ignored), duplicate identities, bad digests and
    bad versions are rejected BEFORE any file is replaced.

    Schema compatibility (R6-A2): schema 1 AND schema 2 payloads are both
    accepted by this ONE parser — the writer pre-check, the instance ACK and
    the auth loader can never disagree. Entries carry ``max_concurrency``
    (1..64) and the payload may carry top-level ``share_max_concurrency``
    (1..64); MISSING fields are filled with the legacy effective limits
    (per-key 1, global 2), so a schema 1 projection behaves exactly as the
    old hardcodes did. The returned ``data`` entries are normalized copies
    with ``max_concurrency`` always present; the input payload is never
    mutated.
    """
    if not isinstance(payload, dict):
        raise ProtocolError("bad_request", "projection must be an object")
    schema = payload.get("schema_version")
    if isinstance(schema, bool) or schema not in PROJECTION_SCHEMA_VERSIONS:
        # R31-6: an explicit type gate — bool schema_version (True == 1)
        # must never be accepted even though it compares equal to 1.
        raise ProtocolError("bad_request", "unsupported projection schema")
    generation = payload.get("config_generation")
    if isinstance(generation, bool) or not isinstance(generation, int) \
            or generation < 0:
        raise ProtocolError("bad_request", "invalid projection generation")
    entries = payload.get("data")
    if not isinstance(entries, list):
        raise ProtocolError("bad_request", "invalid projection data")
    if set(payload) - PROJECTION_TOP_LEVEL_KEYS:
        raise ProtocolError("bad_request", "projection has unknown fields")
    share_max = payload.get("share_max_concurrency")
    if share_max is None:
        # Legacy default: the old hardcoded global limit (missing field =
        # old behavior, for schema 1 and schema 2 alike).
        share_max = PROJECTION_DEFAULT_SHARE_MAX_CONCURRENCY
    else:
        share_max = _parse_projection_limit(share_max, "share_max_concurrency")
    seen_ids, seen_names = set(), set()
    normalized_entries = []
    for item in entries:
        if not isinstance(item, dict):
            raise ProtocolError("bad_request", "invalid projection entry")
        unknown = set(item) - PROJECTION_ENTRY_KEYS
        if unknown:
            # R31-6: entry fields are a whitelist — unknown fields are
            # rejected, not silently ignored as the previous comment claimed.
            raise ProtocolError("bad_request",
                                "projection entry has unknown field(s): "
                                + str(sorted(unknown)))
        key_id = item.get("key_id")
        name = item.get("name")
        digest = item.get("secret_hash")
        version = item.get("secret_version")
        enabled = item.get("enabled")
        if not isinstance(key_id, str) or not key_id \
                or not isinstance(name, str) or not name:
            raise ProtocolError("bad_request", "invalid projection identity")
        if key_id in seen_ids or name in seen_names:
            raise ProtocolError("bad_request", "duplicate projection identity")
        seen_ids.add(key_id)
        seen_names.add(name)
        if not isinstance(digest, str) or len(digest) != 64:
            raise ProtocolError("bad_request", "invalid projection digest")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ProtocolError("bad_request",
                                "invalid projection digest") from exc
        if isinstance(version, bool) or not isinstance(version, int) \
                or version < 1:
            raise ProtocolError("bad_request", "invalid secret version")
        if not isinstance(enabled, bool):
            raise ProtocolError("bad_request", "invalid enabled state")
        max_concurrency = item.get("max_concurrency")
        if max_concurrency is None:
            # Legacy default: the old effective per-key limit.
            max_concurrency = PROJECTION_DEFAULT_MAX_CONCURRENCY
        else:
            max_concurrency = _parse_projection_limit(max_concurrency,
                                                      "max_concurrency")
        # Normalized copy: consumers always see max_concurrency; the input
        # entry dict is never mutated.
        normalized_entries.append(dict(item, max_concurrency=max_concurrency))
    return {"schema_version": schema, "config_generation": generation,
            "share_max_concurrency": share_max,
            "data": normalized_entries}


class ReplayRegistry:
    """One-time consumption of request ids per channel (R29-3, protocol
    kernel semantics carried over from the S0 control channel). Expired ids
    — ts outside TS_WINDOW_SECONDS, the same boundary the validation
    enforces — are reclaimed first. A table FULL of still-valid ids refuses
    new requests instead of evicting them (an evicted in-window id would
    let its signed request replay). Consumption happens BEFORE any side
    effect."""

    def __init__(self, max_seen=20000):
        self.max_seen = max_seen
        self._lock = threading.Lock()
        self._seen = {}   # request_id -> ts

    def consume(self, request_id: str, ts: int, now=None):
        """Consume atomically; raises ProtocolError on replay or capacity."""
        if not isinstance(request_id, str) or not request_id:
            raise ProtocolError("bad_request", "invalid request_id")
        now = int(now if now is not None else time.time())
        with self._lock:
            for rid, seen_ts in list(self._seen.items()):
                if now - seen_ts > TS_WINDOW_SECONDS:
                    del self._seen[rid]
            if request_id in self._seen:
                raise ProtocolError("replay", "replayed request_id")
            if len(self._seen) >= self.max_seen:
                raise ProtocolError(
                    "overlimit",
                    "replay table capacity exceeded; retry after expiry")
            self._seen[request_id] = int(ts)

    def size(self):
        with self._lock:
            return len(self._seen)
