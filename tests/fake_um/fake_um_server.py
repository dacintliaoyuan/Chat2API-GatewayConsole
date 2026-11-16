"""Fake Open WebUI (UM) upstream, used only for verification.

The server speaks the subset of the Open WebUI HTTP surface that
``chat2api/chat2api.py`` actually calls:

* ``GET  /api/config``                  public capability snapshot
* ``GET  /api/models``                  Bearer-protected model catalog
* ``GET  /api/v1/models``               alias of ``/api/models``
* ``GET  /api/v1/auths/``               Bearer-protected session check
* ``GET|POST /api/v1/auths/api_key``     Bearer-protected API key
* ``POST /api/chat/completions``         OpenAI-shaped JSON or SSE
* ``POST /api/v1/messages``             Anthropic-shaped JSON or SSE

Failure modes are selected per request with the ``X-Fake-UM-Mode`` header or
as a server default through the loopback-only ``/__control/*`` endpoints. The
fake NEVER invents a success terminal: ``missing_terminal`` ends the stream
without ``finish_reason``/``[DONE]`` (Chat) or without ``message_stop``
(Messages), exactly as a truncated upstream would; an explicit error stays an
error and is never followed by a fabricated sentinel.

The loopback-only ``/__control/barrier/*`` endpoints add a verification-only
arrival barrier: when armed, a conversation request is recorded at the upstream
(arrival id, test marker/key marker, protocol, model, arrival order and a bounded
request body) and held BEFORE any response byte is written, until the test releases
it. The barrier is disabled by default, so normal mode is byte-for-byte unchanged;
it never fabricates a broker ACK or a success terminal.

Credentials are never literals here: the accepted bearer is taken from
``FAKE_UM_TOKEN`` when set, otherwise generated with ``secrets`` at startup.
The API key is generated the same way. The server binds loopback addresses only
and makes no outbound requests.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 28999
DEFAULT_MODEL = "fake-um-model"
TOKEN_ENV = "FAKE_UM_TOKEN"
MODE_HEADER = "X-Fake-UM-Mode"
CONTROL_PREFIX = "/__control/"
MAX_BODY_BYTES = 8 * 1024 * 1024
RECENT_LIMIT = 200
BARRIER_MAX_ARRIVALS = 128
BARRIER_TIMEOUT_S = 120.0
BARRIER_PROTOCOLS = frozenset(("chat", "messages"))
_KEY_MARKER_RE = re.compile(r"(?:^|[;\s])key=([A-Za-z0-9_.-]+)")
_SEQ_MARKER_RE = re.compile(r"(?:^|[;\s])seq=(\d+)")

MODE_NORMAL = "normal"
MODE_SLOW_FIRST_CHUNK = "slow_first_chunk"
MODE_SILENT = "silent"
MODE_COMMENTS_ONLY = "comments_only"
MODE_MISSING_TERMINAL = "missing_terminal"
MODE_EXPLICIT_5XX = "explicit_5xx"
MODE_INVALID_AUTH = "invalid_auth"
MODE_EXPLICIT_STREAM_ERROR = "explicit_stream_error"
# Long-request / resource-fallback verification modes. They are deliberately
# separate from the short ``comments_only``/``silent`` modes so existing tests
# keep their exact byte/EOF contract.
MODE_SILENT_AFTER_FIRST = "silent_after_first"
MODE_COMMENTS_UNTIL = "comments_until"
MODE_NONSTREAM_HANG = "nonstream_hang"
MODE_SLOW_5XX = "slow_5xx"
MODE_REASONING_LOOP = "reasoning_loop"
MODE_SCRIPTED = "scripted"

MODES = frozenset((
    MODE_NORMAL,
    MODE_SLOW_FIRST_CHUNK,
    MODE_SILENT,
    MODE_COMMENTS_ONLY,
    MODE_MISSING_TERMINAL,
    MODE_EXPLICIT_5XX,
    MODE_INVALID_AUTH,
    MODE_EXPLICIT_STREAM_ERROR,
    MODE_SILENT_AFTER_FIRST,
    MODE_COMMENTS_UNTIL,
    MODE_NONSTREAM_HANG,
    MODE_SLOW_5XX,
    MODE_REASONING_LOOP,
    MODE_SCRIPTED,
))

# Modes that only change a STREAMING response. A non-stream request answered
# under one of these keeps the normal JSON body (documented in ``_dispatch``).
STREAM_ONLY_MODES = frozenset((
    MODE_SLOW_FIRST_CHUNK,
    MODE_SILENT,
    MODE_COMMENTS_ONLY,
    MODE_MISSING_TERMINAL,
    MODE_EXPLICIT_STREAM_ERROR,
    MODE_SILENT_AFTER_FIRST,
    MODE_COMMENTS_UNTIL,
    MODE_REASONING_LOOP,
))


class FakeUMError(RuntimeError):
    """Configuration or control error raised by the fake upstream itself."""


def _is_loopback_host(host: str) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_loopback_client(address: str) -> bool:
    host = address or ""
    if host.startswith("::ffff:"):
        host = host[len("::ffff:"):]
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_script_behavior(behavior):
    """Validate one scripted entry: a mode name or a response mapping.

    A mapping keeps the mode name optional (``normal`` by default) and may add
    an explicit ``status``, ``headers`` and JSON/text ``body``. This lets the
    gateway's retry contract be exercised without inventing a new mode for every
    upstream status/header combination.
    """
    if isinstance(behavior, str):
        if behavior not in MODES:
            raise FakeUMError("unknown scripted behavior: %r" % (behavior,))
        return
    if not isinstance(behavior, dict):
        raise FakeUMError("unknown scripted behavior: %r" % (behavior,))
    mode = behavior.get("mode", MODE_NORMAL)
    if mode not in MODES:
        raise FakeUMError("unknown scripted behavior: %r" % (behavior,))
    status = behavior.get("status")
    if status is not None and (
            not isinstance(status, int) or isinstance(status, bool)
            or not 100 <= status <= 599):
        raise FakeUMError("invalid scripted status: %r" % (status,))
    headers = behavior.get("headers")
    if headers is not None and not isinstance(headers, dict):
        raise FakeUMError("scripted headers must be a mapping")
    body = behavior.get("body")
    if body is not None and not isinstance(body, (dict, list, str)):
        raise FakeUMError("invalid scripted body: %r" % (body,))


@dataclass
class FakeUMConfig:
    """Static configuration of one fake upstream instance."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    model: str = DEFAULT_MODEL
    token: str | None = None
    api_key: str | None = None
    default_mode: str = MODE_NORMAL
    chunk_size: int = 4
    chunk_delay_s: float = 0.0
    slow_first_chunk_s: float = 1.5
    silent_s: float = 2.0
    # Long-request verification knobs. All are local to this fake upstream.
    silent_after_first_s: float = 2.0
    comments_duration_s: float = 2.0
    comment_interval_s: float = 0.2
    nonstream_hang_s: float = 2.0
    slow_5xx_s: float = 1.0
    reasoning_loop_chunk_chars: int = 700
    script: tuple[str | dict, ...] | None = None
    # Arrival-barrier verification knob. The barrier is off by default; when the
    # test arms it, up to BARRIER_MAX_ARRIVALS conversation requests are held
    # (each with a bounded wait) so several can be in flight at once.
    barrier_timeout_s: float = BARRIER_TIMEOUT_S
    barrier_max_arrivals: int = BARRIER_MAX_ARRIVALS

    def __post_init__(self):
        if not _is_loopback_host(self.host):
            raise FakeUMError(
                "FakeUM binds loopback addresses only; refusing %r"
                % (self.host,))
        if not 0 <= self.port <= 65535:
            raise FakeUMError("port out of range: %r" % (self.port,))
        if self.default_mode not in MODES:
            raise FakeUMError("unknown mode: %r" % (self.default_mode,))
        if self.chunk_size < 1:
            raise FakeUMError("chunk_size must be >= 1")
        if self.reasoning_loop_chunk_chars < 1:
            raise FakeUMError("reasoning_loop_chunk_chars must be >= 1")
        for name in ("chunk_delay_s", "slow_first_chunk_s", "silent_s",
                     "silent_after_first_s", "comments_duration_s",
                     "comment_interval_s", "nonstream_hang_s", "slow_5xx_s",
                     "barrier_timeout_s"):
            if getattr(self, name) < 0:
                raise FakeUMError("%s must be >= 0" % name)
        if self.barrier_max_arrivals < 1:
            raise FakeUMError("barrier_max_arrivals must be >= 1")
        if self.script is not None:
            for behavior in self.script:
                _validate_script_behavior(behavior)


class _FakeState:
    """Mutable runtime state shared by all handler threads."""

    def __init__(self, config: FakeUMConfig):
        self._lock = threading.Lock()
        self._config = config
        self._default_mode = config.default_mode
        self._mode = config.default_mode
        self._token = (config.token
                       or os.environ.get(TOKEN_ENV)
                       or secrets.token_urlsafe(24))
        self._api_key = (config.api_key
                         or "fake-um-key-" + secrets.token_urlsafe(18))
        self._request_count = 0
        self._recent = deque(maxlen=RECENT_LIMIT)
        self._script = deque(config.script or ())
        # Verification-only arrival barrier. Disabled by default: every request
        # answers exactly as before until a test arms it.
        self._barrier_enabled = False
        self._barrier_protocols = frozenset()
        self._barrier_arrivals = []
        self._barrier_events = {}
        self._barrier_next = 0
        self._barrier_released = 0
        self._barrier_timed_out = 0

    def snapshot_mode(self) -> str:
        with self._lock:
            return self._mode

    def set_mode(self, mode: str) -> str:
        if mode not in MODES:
            raise FakeUMError("unknown mode: %r" % (mode,))
        with self._lock:
            self._mode = mode
            return self._mode

    def set_script(self, script) -> list:
        """Replace the per-chat-request script used by MODE_SCRIPTED."""
        entries = tuple(script or ())
        for behavior in entries:
            _validate_script_behavior(behavior)
        with self._lock:
            self._script = deque(entries)
            return list(entries)

    def next_scripted(self) -> str:
        """Pop one behavior; an exhausted script keeps answering normally."""
        with self._lock:
            if self._script:
                return self._script.popleft()
            return MODE_NORMAL

    def script_remaining(self) -> int:
        with self._lock:
            return len(self._script)

    def token(self) -> str:
        with self._lock:
            return self._token

    def api_key(self) -> str:
        with self._lock:
            return self._api_key

    def rotate_token(self) -> str:
        with self._lock:
            self._token = secrets.token_urlsafe(24)
            return self._token

    def authorize(self, header_value) -> bool:
        token = self.token()
        if not isinstance(header_value, str):
            return False
        scheme, separator, presented = header_value.partition(" ")
        if not separator or scheme.lower() != "bearer":
            return False
        try:
            return hmac.compare_digest(presented.strip().encode("utf-8"),
                                       token.encode("utf-8"))
        except (UnicodeError, TypeError):
            return False

    def record(self, method: str, path: str, mode: str, auth: str) -> None:
        with self._lock:
            self._request_count += 1
            self._recent.append({"method": method, "path": path,
                                 "mode": mode, "auth": auth})

    def reset(self) -> None:
        with self._lock:
            self._mode = self._default_mode
            self._request_count = 0
            self._recent.clear()
            self._script = deque(self._config.script or ())
            self._reset_barrier_locked()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "mode": self._mode,
                "modes": sorted(MODES),
                "request_count": self._request_count,
                "script_remaining": len(self._script),
                "recent": [dict(entry) for entry in self._recent],
            }

    # ---------- verification-only arrival barrier ----------

    def _barrier_snapshot_locked(self) -> dict:
        return {
            "enabled": self._barrier_enabled,
            "protocols": sorted(self._barrier_protocols),
            "arrivals": [dict(entry) for entry in self._barrier_arrivals],
            "released": self._barrier_released,
            "timedOut": self._barrier_timed_out,
            "maxArrivals": self._config.barrier_max_arrivals,
        }

    def barrier_snapshot(self) -> dict:
        with self._lock:
            return self._barrier_snapshot_locked()

    def arm_barrier(self, protocols=None) -> dict:
        """Arm the arrival barrier; optionally limit it to some protocols.

        Arming clears the previous arrivals so each scenario observes only its own
        requests. A request is held only when the barrier is armed AND its
        protocol matches (no protocol selection means every conversation request).
        """
        selected = tuple(protocols or ())
        for protocol in selected:
            if protocol not in BARRIER_PROTOCOLS:
                raise FakeUMError(
                    "unknown barrier protocol: %r" % (protocol,))
        with self._lock:
            self._barrier_enabled = True
            self._barrier_protocols = frozenset(selected)
            self._barrier_arrivals = []
            self._barrier_events = {}
            self._barrier_next = 0
            self._barrier_released = 0
            self._barrier_timed_out = 0
            return self._barrier_snapshot_locked()

    def _release_barrier_locked(self, arrivals=None) -> int:
        if arrivals is None:
            events = list(self._barrier_events.values())
        else:
            events = [self._barrier_events[arrival_id]
                      for arrival_id in arrivals
                      if arrival_id in self._barrier_events]
        for event in events:
            event.set()
        self._barrier_released += len(events)
        return len(events)

    def release_barrier(self, arrivals=None) -> dict:
        """Release held arrivals (all, or the given arrival ids)."""
        with self._lock:
            released = self._release_barrier_locked(arrivals)
            return {"released": released,
                    "barrier": self._barrier_snapshot_locked()}

    def disarm_barrier(self) -> dict:
        """Disarm and release every held arrival; normal mode resumes."""
        with self._lock:
            self._barrier_enabled = False
            self._release_barrier_locked()
            self._barrier_events = {}
            return self._barrier_snapshot_locked()

    def _reset_barrier_locked(self) -> None:
        self._barrier_enabled = False
        self._release_barrier_locked()
        self._barrier_protocols = frozenset()
        self._barrier_arrivals = []
        self._barrier_events = {}
        self._barrier_next = 0
        self._barrier_released = 0
        self._barrier_timed_out = 0

    def register_barrier_arrival(self, protocol, path, payload):
        """Record one arrival and return ``(arrival_id, event)`` when held.

        Returns ``None`` when the barrier is off or this protocol is not
        selected: the request then answers exactly as before. The body copy is
        bounded and redacts credential-shaped fields; it is never written to a
        product log and never forwarded anywhere.
        """
        with self._lock:
            if not self._barrier_enabled:
                return None
            if (self._barrier_protocols
                    and protocol not in self._barrier_protocols):
                return None
            if len(self._barrier_arrivals) >= self._config.barrier_max_arrivals:
                raise FakeUMError("barrier arrival limit reached")
            self._barrier_next += 1
            arrival_id = self._barrier_next
            event = threading.Event()
            # The product does not forward its own request id to the upstream;
            # the barrier assigns a sanitized arrival id instead of guessing it,
            # and the test marker in the last user message maps it to the Key.
            self._barrier_arrivals.append({
                "arrivalId": arrival_id,
                "requestId": "arrival-%04d" % arrival_id,
                "order": arrival_id,
                "protocol": protocol,
                "path": path,
                "model": payload.get("model"),
                "marker": _arrival_marker(payload),
                "keyMarker": _arrival_key_marker(payload),
                "sequence": _arrival_sequence(payload),
                "body": _bounded_payload(payload),
                "held": True,
            })
            self._barrier_events[arrival_id] = event
            return arrival_id, event

    def mark_barrier_timed_out(self, arrival_id: int) -> None:
        with self._lock:
            for entry in self._barrier_arrivals:
                if entry["arrivalId"] == arrival_id:
                    entry["timedOut"] = True
                    break
            self._barrier_timed_out += 1


class _ClientGone(Exception):
    """The downstream client closed the socket while a response was written."""


class _FakeUMServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # ``server_activate()`` calls ``listen(self.request_queue_size)`` inside
    # ``ThreadingHTTPServer.__init__``.  The value must therefore be a class
    # attribute (or set before ``super().__init__``): assigning it after
    # ``FakeUM.start()`` never reaches the already-created listening socket.
    # The 128-way chain fixture needs more than the stdlib default of 5 pending
    # accepts, so set it before bind/listen instead of after.
    request_queue_size = 256

    def __init__(self, address, fake):
        self.fake_um = fake
        super().__init__(address, _FakeUMHandler)


class _FakeUMHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeUM/1.0"
    sys_version = ""

    # ---------- request entry ----------

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        # Never echo request lines or headers: they carry the bearer token.
        return

    @property
    def fake(self):
        return self.server.fake_um

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")

    def _serve(self, method):
        try:
            self._dispatch(method)
        except _ClientGone:
            self.close_connection = True

    def _dispatch(self, method):
        self._scripted_behavior = None
        path = urllib.parse.urlparse(self.path).path
        mode = self._requested_mode()
        if mode is None:
            self._send_json(400, {"detail": "unknown mode"})
            return
        if path.startswith(CONTROL_PREFIX):
            self._handle_control(method, path)
            return
        if mode == MODE_EXPLICIT_5XX:
            self.fake.record(method, path, mode, "not_checked")
            self._send_json(500, {"detail": "fake upstream failure"})
            return
        if path == "/api/config":
            # Public in Open WebUI: queried WITHOUT the user's bearer, so this
            # endpoint never answers 401 and never reports a credential issue.
            self.fake.record(method, path, mode, "public")
            self._send_json(200, self.fake.config_payload())
            return
        if mode == MODE_INVALID_AUTH:
            self.fake.record(method, path, mode, "forced_rejected")
            self._send_json(401, {"detail": "Not authenticated"})
            return
        authorized = self.fake.authorize(self.headers.get("Authorization"))
        self.fake.record(method, path, mode,
                          "valid" if authorized else "rejected")
        if not authorized:
            self._send_json(401, {"detail": "Not authenticated"})
            return
        if method == "GET":
            self._handle_get(path)
        elif method == "POST":
            self._handle_post(path, mode)
        else:
            self._send_json(405, {"detail": "Method Not Allowed"})

    def _requested_mode(self):
        requested = self.headers.get(MODE_HEADER)
        if requested is None:
            return self.fake.snapshot_mode()
        requested = requested.strip()
        if requested not in MODES:
            return None
        return requested

    # ---------- routes ----------

    def _handle_get(self, path):
        if path in ("/api/models", "/api/v1/models"):
            self._send_json(200, self.fake.models_payload())
            return
        if path in ("/api/v1/auths", "/api/v1/auths/"):
            self._send_json(200, self.fake.session_payload())
            return
        if path == "/api/v1/auths/api_key":
            self._send_json(200, {"api_key": self.fake.api_key})
            return
        self._send_json(404, {"detail": "Not Found"})

    def _handle_post(self, path, mode):
        if path == "/api/v1/auths/api_key":
            self._send_json(200, {"api_key": self.fake.api_key})
            return
        if path == "/api/chat/completions":
            payload = self._read_json()
            if payload is None:
                return
            if not self._valid_messages(payload):
                return
            self._barrier_hold("chat", path, payload)
            mode = self._effective_mode(mode)
            if self._scripted_status_response():
                return
            if mode in (MODE_EXPLICIT_5XX, MODE_SLOW_5XX):
                if mode == MODE_SLOW_5XX:
                    self._sleep(self.fake.config.slow_5xx_s)
                self._send_json(500, {"detail": "fake upstream failure"})
                return
            reply = _last_user_text(payload) or "fake-um-reply"
            if payload.get("stream") is True:
                self._chat_stream(payload, reply, mode)
            elif mode == MODE_NONSTREAM_HANG:
                self._nonstream_hang()
            else:
                self._send_json(200, _chat_body(payload, reply,
                                                self.fake.config.model))
            return
        if path == "/api/v1/messages":
            payload = self._read_json()
            if payload is None:
                return
            if not self._valid_messages(payload):
                return
            self._barrier_hold("messages", path, payload)
            mode = self._effective_mode(mode)
            if self._scripted_status_response():
                return
            if mode in (MODE_EXPLICIT_5XX, MODE_SLOW_5XX):
                if mode == MODE_SLOW_5XX:
                    self._sleep(self.fake.config.slow_5xx_s)
                self._send_json(500, {"detail": "fake upstream failure"})
                return
            reply = _last_user_text(payload) or "fake-um-reply"
            if payload.get("stream") is True:
                self._messages_stream(payload, reply, mode)
            else:
                self._send_json(200, _messages_body(payload, reply,
                                                      self.fake.config.model))
            return
        self._discard_body()
        self._send_json(404, {"detail": "Not Found"})

    def _scripted_status_response(self):
        """Answer a scripted status/header entry, if the current one has one."""
        behavior = getattr(self, "_scripted_behavior", None)
        if not isinstance(behavior, dict) or behavior.get("status") is None:
            return False
        status = int(behavior["status"])
        body = behavior.get("body")
        if body is None:
            body = {"detail": "scripted upstream status %d" % status}
        self._send_json(status, body, headers=behavior.get("headers"))
        return True

    def _effective_mode(self, mode):
        """Consume one scripted behavior for a chat/messages request.

        MODE_SCRIPTED is a meta mode: every conversation request pops the next
        behavior, while an exhausted script answers normally. A behavior may be a
        mode name or a mapping with ``mode``/``status``/``headers``/``body``.
        Non-conversation routes (catalog, auth) never consume a script entry.
        """
        if mode != MODE_SCRIPTED:
            return mode
        behavior = self.fake.next_scripted()
        self._scripted_behavior = behavior
        if isinstance(behavior, dict):
            return behavior.get("mode", MODE_NORMAL)
        return behavior

    def _handle_control(self, method, path):
        if not _is_loopback_client(self.client_address[0]):
            self._send_json(403, {"detail": "control is loopback-only"})
            return
        if path == "/__control/state":
            if method != "GET":
                self._send_json(405, {"detail": "Method Not Allowed"})
                return
            self._send_json(200, self.fake.state())
            return
        if path == "/__control/mode":
            if method != "POST":
                self._send_json(405, {"detail": "Method Not Allowed"})
                return
            payload = self._read_json()
            if payload is None:
                return
            try:
                mode = self.fake.set_mode(payload.get("mode"))
            except FakeUMError as exc:
                self._send_json(400, {"detail": str(exc)})
                return
            self._send_json(200, {"mode": mode})
            return
        if path == "/__control/rotate_token":
            if method != "POST":
                self._send_json(405, {"detail": "Method Not Allowed"})
                return
            # The body is ignored, but a declared body must be consumed:
            # closing with unread received data makes Windows answer the
            # client's next read with RST (the intermittent
            # ConnectionAbortedError in TestBarrierControl).
            self._discard_body()
            # The new token is readable in-process; it is never sent back over
            # the wire so a control call cannot hand out a credential.
            self.fake.rotate_token()
            self._send_json(200, {"rotated": True})
            return
        if path == "/__control/reset":
            if method != "POST":
                self._send_json(405, {"detail": "Method Not Allowed"})
                return
            self._discard_body()
            self.fake.reset()
            self._send_json(200, self.fake.state())
            return
        if path == "/__control/barrier":
            if method != "GET":
                self._send_json(405, {"detail": "Method Not Allowed"})
                return
            self._send_json(200, self.fake.barrier_snapshot())
            return
        if path == "/__control/barrier/arm":
            if method != "POST":
                self._send_json(405, {"detail": "Method Not Allowed"})
                return
            payload = self._read_json()
            if payload is None:
                return
            try:
                state = self.fake.arm_barrier(payload.get("protocols"))
            except FakeUMError as exc:
                self._send_json(400, {"detail": str(exc)})
                return
            self._send_json(200, state)
            return
        if path == "/__control/barrier/release":
            if method != "POST":
                self._send_json(405, {"detail": "Method Not Allowed"})
                return
            payload = self._read_json()
            if payload is None:
                return
            arrivals = payload.get("arrivals")
            if arrivals is not None and (
                    not isinstance(arrivals, list)
                    or any(not isinstance(item, int)
                           or isinstance(item, bool) for item in arrivals)):
                self._send_json(400, {
                    "detail": "arrivals must be a list of integers"})
                return
            self._send_json(200, self.fake.release_barrier(arrivals))
            return
        if path == "/__control/barrier/disarm":
            if method != "POST":
                self._send_json(405, {"detail": "Method Not Allowed"})
                return
            # Consume the declared body before responding: closing with unread
            # received data makes Windows answer the client's next read with RST.
            self._discard_body()
            self._send_json(200, self.fake.disarm_barrier())
            return
        self._discard_body()
        self._send_json(404, {"detail": "Not Found"})

    # ---------- body handling ----------

    def _discard_body(self):
        """Consume a declared request body that this route ignores.

        Closing a socket that still holds unread received data makes Windows
        answer the client's next read with RST (the intermittent
        ConnectionAbortedError in TestBarrierControl). A route that ignores the
        body still reads and discards it, bounded by the same body limit.
        """
        header = self.headers.get("Content-Length")
        if header is None:
            return
        try:
            length = int(header)
        except ValueError:
            return
        if length <= 0:
            return
        try:
            self.rfile.read(min(length, MAX_BODY_BYTES))
        except OSError:
            raise _ClientGone() from None

    def _read_json(self):
        header = self.headers.get("Content-Length")
        if header is None:
            self._send_json(411, {"detail": "Content-Length required"})
            return None
        try:
            length = int(header)
        except ValueError:
            self._send_json(400, {"detail": "invalid Content-Length"})
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_json(413, {"detail": "request body too large"})
            return None
        raw = self.rfile.read(length)
        if len(raw) != length:
            self._send_json(400, {"detail": "truncated request body"})
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._send_json(400, {"detail": "invalid JSON body"})
            return None
        if not isinstance(payload, dict):
            self._send_json(400, {"detail": "JSON body must be an object"})
            return None
        return payload

    def _valid_messages(self, payload):
        messages = payload.get("messages")
        if isinstance(messages, list):
            return True
        self._send_json(422, {"detail": "messages must be a list"})
        return False

    def _barrier_hold(self, protocol, path, payload):
        """Hold this request at the upstream until the test releases it.

        The arrival is recorded BEFORE any response byte is written. Returns the
        arrival id when held, ``None`` in the default (unarmed) mode. The wait is
        bounded so a forgotten release can never pin a handler forever; a timed
        out arrival is still recorded and the normal response follows.
        """
        try:
            arrival = self.fake.register_barrier_arrival(protocol, path,
                                                         payload)
        except FakeUMError:
            return None
        if arrival is None:
            return None
        arrival_id, event = arrival
        if not event.wait(self.fake.config.barrier_timeout_s):
            self.fake.mark_barrier_timed_out(arrival_id)
        return arrival_id

    # ---------- response writers ----------

    def _send_json(self, code, obj, headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            self.send_header(str(name), str(value))
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise _ClientGone() from None

    def _start_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _nonstream_hang(self):
        """Send 200 headers and keep the body open without any bytes."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", "1024")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self._sleep(self.fake.config.nonstream_hang_s)
        try:
            self.wfile.write(b'{"choices": []}')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise _ClientGone() from None

    def _write_chunk(self, data):
        try:
            self.wfile.write(("%X" % len(data)).encode("ascii")
                             + b"\r\n" + data + b"\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise _ClientGone() from None

    def _end_stream(self):
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise _ClientGone() from None

    def _sleep(self, seconds):
        if seconds <= 0:
            return
        time.sleep(seconds)

    # ---------- chat completions ----------

    def _chat_stream(self, payload, reply, mode):
        config = self.fake.config
        model = _request_model(payload, config.model)
        include_usage = _include_usage(payload)
        self._start_stream()
        if mode == MODE_SILENT:
            # Headers arrive, no data ever does: the proxy must see a clean
            # zero-event EOF, not a chunked framing error.
            self._sleep(config.silent_s)
            self._end_stream()
            return
        if mode == MODE_COMMENTS_ONLY:
            self._write_chunk(b": keepalive\n\n")
            self._end_stream()
            return
        if mode == MODE_COMMENTS_UNTIL:
            # Comments and blank lines are valid SSE keepalives but are NOT a
            # substantive first data event.  Keep them flowing past the
            # product's first-byte budget to prove they never satisfy it.
            deadline = time.monotonic() + config.comments_duration_s
            while time.monotonic() < deadline:
                self._write_chunk(b": keepalive\n\n")
                self._sleep(config.comment_interval_s)
            self._end_stream()
            return
        if mode == MODE_SILENT_AFTER_FIRST:
            # One substantive content chunk commits the downstream 200; the
            # upstream then goes silent without closing, so only the product's
            # per-socket silence bound can release the reader.
            created = int(time.time())
            completion_id = "chatcmpl-" + secrets.token_hex(8)
            self._chat_chunk(completion_id, created, model,
                             {"role": "assistant", "content": "first-visible"},
                             None)
            self._sleep(config.silent_after_first_s)
            self._end_stream()
            return
        if mode == MODE_REASONING_LOOP:
            # Reasoning-only chunks trip the repetition guard before any visible
            # content.  The tail is one short pattern repeated, which is exactly
            # the degenerate shape the guard cuts.
            created = int(time.time())
            completion_id = "chatcmpl-" + secrets.token_hex(8)
            piece = "lock" * max(1, config.reasoning_loop_chunk_chars // 4)
            for _ in range(3):
                self._chat_chunk(completion_id, created, model,
                                 {"reasoning_content": piece}, None)
            self._end_stream()
            return
        if mode == MODE_SLOW_FIRST_CHUNK:
            self._sleep(config.slow_first_chunk_s)
        if mode == MODE_EXPLICIT_STREAM_ERROR:
            # Open WebUI 0.11.x reports a failed stream as a normal event.
            # No [DONE] follows: an error is never dressed up as success.
            self._write_chunk(
                b'data: {"error": {"message": "fake upstream failure", '
                b'"type": "server_error"}}\n\n')
            self._end_stream()
            return

        created = int(time.time())
        completion_id = "chatcmpl-" + secrets.token_hex(8)
        self._chat_chunk(completion_id, created, model,
                         {"role": "assistant", "content": ""}, None)
        for piece in _split_text(reply, config.chunk_size):
            if config.chunk_delay_s:
                self._sleep(config.chunk_delay_s)
            self._chat_chunk(completion_id, created, model,
                             {"content": piece}, None)
        if mode == MODE_MISSING_TERMINAL:
            # Truncated upstream: content flowed, the terminal never did.
            self._end_stream()
            return
        self._chat_chunk(completion_id, created, model, {}, "stop")
        if include_usage:
            self._write_chunk(("data: " + json.dumps({
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [],
                "usage": _usage(payload, reply),
            }, ensure_ascii=False) + "\n\n").encode("utf-8"))
        self._write_chunk(b"data: [DONE]\n\n")
        self._end_stream()

    def _chat_chunk(self, completion_id, created, model, delta, finish_reason):
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta,
                         "finish_reason": finish_reason}],
        }
        self._write_chunk(("data: " + json.dumps(chunk, ensure_ascii=False)
                           + "\n\n").encode("utf-8"))

    # ---------- anthropic messages ----------

    def _messages_stream(self, payload, reply, mode):
        config = self.fake.config
        model = _request_model(payload, config.model)
        self._start_stream()
        if mode == MODE_SILENT:
            self._sleep(config.silent_s)
            self._end_stream()
            return
        if mode == MODE_COMMENTS_ONLY:
            self._write_chunk(b": keepalive\n\n")
            self._end_stream()
            return
        if mode == MODE_COMMENTS_UNTIL:
            deadline = time.monotonic() + config.comments_duration_s
            while time.monotonic() < deadline:
                self._write_chunk(b": keepalive\n\n")
                self._sleep(config.comment_interval_s)
            self._end_stream()
            return
        if mode == MODE_SILENT_AFTER_FIRST:
            message_id = "msg_" + secrets.token_hex(12)
            self._anthropic_event("message_start", {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            })
            self._anthropic_event("content_block_delta", {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "first-visible"},
            })
            self._sleep(config.silent_after_first_s)
            self._end_stream()
            return
        if mode == MODE_SLOW_FIRST_CHUNK:
            self._sleep(config.slow_first_chunk_s)
        if mode == MODE_EXPLICIT_STREAM_ERROR:
            # Anthropic in-band error; no message_stop follows.
            self._write_chunk(
                b'event: error\ndata: {"type":"error","error":'
                b'{"type":"api_error","message":"fake upstream failure"}}\n\n')
            self._end_stream()
            return

        message_id = "msg_" + secrets.token_hex(12)
        prompt_tokens = _estimate_tokens(_joined_text(payload))
        output_tokens = _estimate_tokens(reply)
        self._anthropic_event("message_start", {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": prompt_tokens, "output_tokens": 1},
            },
        })
        self._anthropic_event("content_block_start", {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        })
        for piece in _split_text(reply, config.chunk_size):
            if config.chunk_delay_s:
                self._sleep(config.chunk_delay_s)
            self._anthropic_event("content_block_delta", {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": piece},
            })
        self._anthropic_event("content_block_stop", {
            "type": "content_block_stop", "index": 0,
        })
        self._anthropic_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        })
        if mode == MODE_MISSING_TERMINAL:
            # message_delta is explicitly NOT a terminal event: the stream
            # ends here without message_stop, as a truncated upstream would.
            self._end_stream()
            return
        self._anthropic_event("message_stop", {"type": "message_stop"})
        self._end_stream()

    def _anthropic_event(self, event_name, payload):
        self._write_chunk(("event: " + event_name + "\ndata: "
                           + json.dumps(payload, ensure_ascii=False)
                           + "\n\n").encode("utf-8"))


def _split_text(text, size):
    if not text:
        return []
    return [text[index:index + size] for index in range(0, len(text), size)]


def _request_model(payload, default):
    model = payload.get("model")
    if isinstance(model, str) and model:
        return model
    return default


def _include_usage(payload):
    options = payload.get("stream_options")
    return isinstance(options, dict) and bool(options.get("include_usage"))


def _message_texts(payload):
    texts = []
    system = payload.get("system")
    if isinstance(system, str):
        texts.append(system)
    elif isinstance(system, list):
        for part in system:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if (isinstance(part, dict)
                            and isinstance(part.get("text"), str)):
                        texts.append(part["text"])
    return texts


def _last_user_text(payload):
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            pieces = [part["text"] for part in content
                      if isinstance(part, dict)
                      and isinstance(part.get("text"), str)]
            if pieces:
                return "".join(pieces)
    return ""


def _joined_text(payload):
    return "".join(_message_texts(payload))


def _arrival_marker(payload):
    """The test marker a chain fixture puts in the last user message."""
    return _last_user_text(payload)[:300]


def _arrival_key_marker(payload):
    match = _KEY_MARKER_RE.search(_last_user_text(payload))
    return match.group(1) if match else None


def _arrival_sequence(payload):
    match = _SEQ_MARKER_RE.search(_last_user_text(payload))
    return int(match.group(1)) if match else None


def _bounded_payload(value, depth=0):
    """Bounded, credential-redacted copy of one request body for evidence."""
    if depth > 8:
        return "..."
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:16]:
            name = str(key)
            if name.lower() in ("authorization", "token", "api_key",
                                "secret", "cookie", "x-api-key"):
                result[name] = "<redacted>"
            else:
                result[name] = _bounded_payload(item, depth + 1)
        return result
    if isinstance(value, list):
        return [_bounded_payload(item, depth + 1) for item in value[:8]]
    if isinstance(value, str):
        return value[:500] + ("..." if len(value) > 500 else "")
    return value


def _estimate_tokens(text):
    return max(1, len(text) // 4)


def _usage(payload, reply):
    prompt_tokens = _estimate_tokens(_joined_text(payload))
    completion_tokens = _estimate_tokens(reply)
    return {"prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens}


def _chat_body(payload, reply, default_model):
    model = _request_model(payload, default_model)
    return {
        "id": "chatcmpl-" + secrets.token_hex(8),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": reply},
            "finish_reason": "stop",
        }],
        "usage": _usage(payload, reply),
    }


def _messages_body(payload, reply, default_model):
    model = _request_model(payload, default_model)
    return {
        "id": "msg_" + secrets.token_hex(12),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": reply}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": _estimate_tokens(_joined_text(payload)),
            "output_tokens": _estimate_tokens(reply),
        },
    }


class FakeUM:
    """One fake Open WebUI upstream instance bound to a loopback port."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, *, token=None,
                 model=DEFAULT_MODEL, default_mode=MODE_NORMAL,
                 chunk_size=4, chunk_delay_s=0.0, slow_first_chunk_s=1.5,
                 silent_s=2.0, silent_after_first_s=2.0,
                 comments_duration_s=2.0, comment_interval_s=0.2,
                 nonstream_hang_s=2.0, slow_5xx_s=1.0,
                 reasoning_loop_chunk_chars=700, script=None,
                 barrier_timeout_s=BARRIER_TIMEOUT_S,
                 barrier_max_arrivals=BARRIER_MAX_ARRIVALS):
        self.config = FakeUMConfig(
            host=host, port=port, model=model, token=token,
            default_mode=default_mode, chunk_size=chunk_size,
            chunk_delay_s=chunk_delay_s,
            slow_first_chunk_s=slow_first_chunk_s,
            silent_s=silent_s,
            silent_after_first_s=silent_after_first_s,
            comments_duration_s=comments_duration_s,
            comment_interval_s=comment_interval_s,
            nonstream_hang_s=nonstream_hang_s,
            slow_5xx_s=slow_5xx_s,
            reasoning_loop_chunk_chars=reasoning_loop_chunk_chars,
            script=script,
            barrier_timeout_s=barrier_timeout_s,
            barrier_max_arrivals=barrier_max_arrivals,
        )
        self._state = _FakeState(self.config)
        self._server = None
        self._thread = None

    # ---------- lifecycle ----------

    def start(self):
        if self._server is not None:
            raise FakeUMError("FakeUM is already started")
        server = _FakeUMServer((self.config.host, self.config.port), self)
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="fake-um", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, traceback):
        self.stop()
        return False

    # ---------- introspection ----------

    @property
    def host(self):
        return self.config.host

    @property
    def port(self):
        if self._server is not None:
            return self._server.server_address[1]
        return self.config.port

    @property
    def base_url(self):
        return "http://%s:%d" % (self.host, self.port)

    @property
    def token(self):
        return self._state.token()

    @property
    def api_key(self):
        return self._state.api_key()

    @property
    def mode(self):
        return self._state.snapshot_mode()

    def set_mode(self, mode):
        return self._state.set_mode(mode)

    def set_script(self, script):
        return self._state.set_script(script)

    def next_scripted(self):
        return self._state.next_scripted()

    def script_remaining(self):
        return self._state.script_remaining()

    def rotate_token(self):
        return self._state.rotate_token()

    def reset(self):
        self._state.reset()

    def state(self):
        snapshot = self._state.snapshot()
        snapshot["model"] = self.config.model
        return snapshot

    def snapshot_mode(self):
        return self._state.snapshot_mode()

    def authorize(self, header_value):
        return self._state.authorize(header_value)

    def record(self, method, path, mode, auth):
        self._state.record(method, path, mode, auth)

    def arm_barrier(self, protocols=None):
        return self._state.arm_barrier(protocols)

    def disarm_barrier(self):
        return self._state.disarm_barrier()

    def release_barrier(self, arrivals=None):
        return self._state.release_barrier(arrivals)

    def barrier_snapshot(self):
        return self._state.barrier_snapshot()

    def register_barrier_arrival(self, protocol, path, payload):
        return self._state.register_barrier_arrival(protocol, path, payload)

    def mark_barrier_timed_out(self, arrival_id):
        return self._state.mark_barrier_timed_out(arrival_id)

    # ---------- payloads ----------

    def config_payload(self):
        return {
            "version": "0.11.0-fake-um",
            "name": "Fake UM",
            "default_models": self.config.model,
            "features": {
                "auth": True,
                "enable_api_keys": True,
                "enable_signup": False,
            },
        }

    def models_payload(self):
        model = self.config.model
        return {"data": [{
            "id": model,
            "name": model,
            "object": "model",
            "created": 0,
            "owned_by": "fake-um",
            "openai": {"id": model, "object": "model"},
        }]}

    def session_payload(self):
        # No token field: the session check must not hand out credentials.
        return {
            "id": "fake-um-user",
            "email": "fake-um@example.invalid",
            "name": "Fake UM",
            "role": "admin",
            "token_type": "bearer",
        }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Fake Open WebUI upstream for chat2api verification.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--mode", default=MODE_NORMAL, choices=sorted(MODES))
    parser.add_argument("--slow-first-chunk-s", type=float, default=1.5)
    parser.add_argument("--silent-s", type=float, default=2.0)
    parser.add_argument("--print-token", action="store_true",
                        help="print the generated bearer token to stdout")
    args = parser.parse_args(argv)

    fake = FakeUM(host=args.host, port=args.port, model=args.model,
                  default_mode=args.mode,
                  slow_first_chunk_s=args.slow_first_chunk_s,
                  silent_s=args.silent_s)
    fake.start()
    print("fake UM listening on %s" % fake.base_url, flush=True)
    if args.print_token:
        print("bearer token: %s" % fake.token, flush=True)
    else:
        print("bearer token: set %s or pass --print-token" % TOKEN_ENV,
              flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        fake.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
