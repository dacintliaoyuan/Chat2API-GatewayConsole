"""Same-origin noVNC launcher and WebSocket-to-RFB relay.

The console listener stays on loopback (8096).  Tailscale Serve terminates
HTTPS on the tailnet and forwards only the configured owner/device to that
listener.  This module adds the private display surface:

* ``/display/`` serves a small noVNC launcher page;
* ``/display/launcher.js`` serves that page's module script, which lives next
  to the launcher HTML (the page CSP is ``script-src 'self'``, so the launcher
  cannot be an inline script);
* ``/display/<asset>`` serves the installed noVNC JavaScript/CSS tree;
* ``/display/websockify`` upgrades to WebSocket and relays RFB bytes to the
  loopback x11vnc port (8097 by default).

The console's origin gate is not an administrator identity.  An SSH ``-L``
tunnel also reaches the console as loopback, so a display grant is required:

* an authenticated management request (Tailscale Serve owner/device, the admin
  control token, or the console's ``usage_session`` cookie) issues a short-lived
  single-use grant;
* ``POST /display/grant`` exchanges that grant for an HttpOnly, SameSite=Strict
  cookie scoped to ``/display/``; the grant token itself never reaches a URL
  query, the server log, or browser storage (the launcher removes the fragment);
* the WebSocket handshake accepts only that display session, an admin session,
  the control token, or the exact Tailscale Serve identity.

VNC authentication is completed by the console proxy, not by the browser.  The
proxy performs the RFB 3.8/3.7/3.3 security handshake with x11vnc using a
challenge-response helper.  The helper runs inside the display service, which
already holds the root-only VNC credential, and returns only the DES response for
a challenge; the password never enters the console process, HTML, JavaScript, a
URL, browser storage or a log.  x11vnc keeps ``-localhost`` and ``-rfbauth``.
Query strings are rejected outright except for the single ``operator`` generation
on the WebSocket path; every other query value, duplicate, fragment, userinfo or
password is still rejected.  No request headers, frame payloads, RFB bytes or
credentials are logged.

One page at a time owns the login display's keyboard and pointer.  The launcher
acquires a server-assigned generation before opening the WebSocket, and the relay
drops ``KeyEvent``/``PointerEvent`` frames from any older generation.  Watching
(pixel format, encodings and framebuffer updates) stays available to every
authorized page; closing a viewer never releases another page's ownership.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import secrets
import select
import socket
import stat
import struct
import threading
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from console.tailnet import has_proxy_metadata

DISPLAY_PREFIX = "/display"
WEBSOCKET_PATH = "/display/websockify"
GRANT_PATH = "/display/grant"
SESSION_PATH = "/display/session"
DISPLAY_SESSION_COOKIE = "um_display_session"
# The launcher page loads this module script instead of an inline script, because
# the page CSP is ``script-src 'self'``.  It is a launcher asset, not a noVNC
# asset: it is served from the launcher file's own directory.
LAUNCHER_SCRIPT_PATH = "launcher.js"
LAUNCHER_SCRIPT_NAME = "novnc-launcher.js"
# The display-state mapping is a second launcher asset.  The launcher module
# imports it as ``./launcher-state.js``; it is served from the launcher file's
# own directory and never resolved against the noVNC root.
LAUNCHER_STATE_SCRIPT_PATH = "launcher-state.js"
LAUNCHER_STATE_SCRIPT_NAME = "novnc-launcher-state.mjs"
LAUNCHER_ASSETS = {
    LAUNCHER_SCRIPT_PATH: LAUNCHER_SCRIPT_NAME,
    LAUNCHER_STATE_SCRIPT_PATH: LAUNCHER_STATE_SCRIPT_NAME,
}
# A non-secret label shown when another page owns the operator.
OPERATOR_OWNER_LABEL = "display-session"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME_PAYLOAD = 8 * 1024 * 1024
UPSTREAM_READ_SIZE = 64 * 1024
PING_INTERVAL = 30.0
MAX_CLOSE_REASON = 123
CLIENT_FRAME_TIMEOUT = 2.0
# A peer that stopped reading leaves a data frame parked in sendall().  A
# bounded send timeout releases that worker on its own; shutdown()/close() alone
# is not guaranteed to wake a blocked sender on every platform.  The frame is
# still written whole under the send lock, so the one-writer-per-frame rule is
# unchanged.
SEND_FRAME_TIMEOUT = 2.0
# A close frame is best effort.  A client that stopped reading leaves a data
# frame parked in sendall() with _send_lock held, so the close frame may wait at
# most this long for the lock and for its own socket write.
CLOSE_FRAME_TIMEOUT = 0.25
MAX_GRANT_BODY = 4096
MAX_GRANT_TOKEN = 256
GRANT_TTL = 60.0
DISPLAY_SESSION_TTL = 300.0
MAX_PENDING_GRANTS = 64
MAX_DISPLAY_SESSIONS = 64
# systemd's LoadCredential copy is root:root 0440 with an ACL for the service
# user (measured on Ubuntu 26.04); the persistent source is 0600 root:root.
_VNC_AUTH_FILE_MODES = (0o400, 0o440, 0o600, 0o640)
_VNC_AUTH_OWNERS = (0,)
_AUTH_UNSET = object()

# Expand-contract (2026-11-14): x11vnc moved 5901 -> 8097 so the display
# listener sits inside the governed 8090-8099 segment.  During the one-release
# window the old port is still accepted when CONSOLE_DISPLAY_VNC_PORT names it
# explicitly; it is never a second default and there is never a second listener.
DEFAULT_VNC_PORT = 8097
LEGACY_VNC_COMPAT_PORTS = frozenset({5901})

STATIC_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".wasm": "application/wasm",
}

SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
    ("Content-Security-Policy",
     "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
     "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
     "worker-src 'self' blob:; base-uri 'none'; form-action 'none'; "
     "frame-ancestors 'none'"),
)


class ProtocolError(Exception):
    """A client WebSocket frame violated RFC 6455."""

    def __init__(self, message, code=1002):
        super().__init__(message)
        self.code = code


class VncAuthError(Exception):
    """VNC challenge-response or credential access failed closed."""


class DisplayGrantError(Exception):
    """A display grant cannot be issued (capacity or invalid binding)."""


@dataclass(frozen=True)
class DisplayConfig:
    enabled: bool = False
    vnc_host: str = "127.0.0.1"
    vnc_port: int = DEFAULT_VNC_PORT
    novnc_root: str = "/usr/share/novnc"
    launcher_file: str = ""
    max_connections: int = 2
    idle_timeout: float = 900.0
    max_session_seconds: float = 4 * 3600.0
    handshake_timeout: float = 10.0
    error: str = ""
    vnc_auth_socket: str = ""
    vnc_auth_file: str = ""
    relay_join_timeout: float = 5.0


def _env(environ, name, default=""):
    if environ is None:
        return os.environ.get(name, default)
    return environ.get(name, default)


def _enabled(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _integer(value, minimum, maximum):
    number = int(str(value).strip(), 10)
    if not minimum <= number <= maximum:
        raise ValueError("integer out of range")
    return number


def _number(value, minimum, maximum):
    number = float(str(value).strip())
    if not minimum <= number <= maximum:
        raise ValueError("number out of range")
    return number


def _absolute_path(value):
    """Accept a POSIX absolute path on every host, or a native absolute path."""
    text = str(value or "")
    return text.startswith("/") or Path(text).is_absolute()


def _default_launcher():
    return str(Path(__file__).resolve().parents[1]
               / "um-cloud-deploy" / "display" / "novnc-launcher.html")


def load_config(environ=None):
    """Load the display surface configuration; invalid values fail closed."""
    enabled = _enabled(_env(environ, "CONSOLE_DISPLAY_ENABLED", "0"))
    if not enabled:
        return DisplayConfig()

    try:
        vnc_host = str(_env(environ, "CONSOLE_DISPLAY_VNC_HOST",
                           "127.0.0.1")).strip()
        address = ipaddress.ip_address(vnc_host)
        if not address.is_loopback:
            raise ValueError("vnc host must be loopback")
        vnc_port = _integer(
            _env(environ, "CONSOLE_DISPLAY_VNC_PORT", str(DEFAULT_VNC_PORT)),
            1, 65535)
        novnc_root = str(_env(environ, "CONSOLE_DISPLAY_NOVNC_ROOT",
                            "/usr/share/novnc")).strip()
        if not novnc_root or not _absolute_path(novnc_root):
            raise ValueError("novnc root must be absolute")
        launcher_file = str(_env(environ, "CONSOLE_DISPLAY_LAUNCHER_FILE",
                               _default_launcher())).strip()
        if not launcher_file or not _absolute_path(launcher_file):
            raise ValueError("launcher file must be absolute")
        max_connections = _integer(
            _env(environ, "CONSOLE_DISPLAY_MAX_CONNECTIONS", "2"), 1, 4)
        idle_timeout = _number(
            _env(environ, "CONSOLE_DISPLAY_IDLE_TIMEOUT", "900"), 30, 7200)
        max_session_seconds = _number(
            _env(environ, "CONSOLE_DISPLAY_MAX_SESSION", "14400"),
            60, 4 * 3600)
        handshake_timeout = _number(
            _env(environ, "CONSOLE_DISPLAY_HANDSHAKE_TIMEOUT", "10"), 1, 30)
        relay_join_timeout = _number(
            _env(environ, "CONSOLE_DISPLAY_SHUTDOWN_GRACE", "5"), 1, 30)
        vnc_auth_file = str(_env(
            environ, "CONSOLE_DISPLAY_VNC_AUTH_FILE", "")).strip()
        vnc_auth_socket = str(_env(
            environ, "CONSOLE_DISPLAY_VNC_AUTH_SOCKET",
            "" if vnc_auth_file else "/run/um-share/vnc-auth.sock")).strip()
        if vnc_auth_socket and not _absolute_path(vnc_auth_socket):
            raise ValueError("vnc auth socket must be absolute")
        if vnc_auth_file and not _absolute_path(vnc_auth_file):
            raise ValueError("vnc auth file must be absolute")
        if vnc_auth_socket and vnc_auth_file:
            raise ValueError("vnc auth source must be exclusive")
    except (TypeError, ValueError):
        return DisplayConfig(error="display_config_invalid")

    if not Path(novnc_root).is_dir():
        return DisplayConfig(error="display_novnc_missing")
    if not Path(launcher_file).is_file():
        return DisplayConfig(error="display_launcher_missing")
    if vnc_auth_file and not Path(vnc_auth_file).is_file():
        return DisplayConfig(error="display_vnc_auth_missing")
    return DisplayConfig(
        True, str(address), vnc_port, novnc_root, launcher_file,
        max_connections, idle_timeout, max_session_seconds,
        handshake_timeout, vnc_auth_socket=vnc_auth_socket,
        vnc_auth_file=vnc_auth_file,
        relay_join_timeout=relay_join_timeout)


class ConnectionLimiter:
    """Bound concurrent display sessions and release every slot exactly once."""

    def __init__(self, limit):
        self._limit = limit
        self._active = 0
        self._lock = threading.Lock()

    @property
    def active(self):
        with self._lock:
            return self._active

    def acquire(self):
        with self._lock:
            if self._active >= self._limit:
                return False
            self._active += 1
            return True

    def release(self):
        with self._lock:
            if self._active > 0:
                self._active -= 1


def _header(headers, name):
    if hasattr(headers, "get_all"):
        rows = headers.get_all(name, [])
        if len(rows) != 1:
            return None
        return rows[0]
    value = headers.get(name)
    return value if isinstance(value, str) else None


def _cookie_value(cookie_header, name):
    if not cookie_header:
        return None
    try:
        jar = SimpleCookie()
        jar.load(cookie_header)
        morsel = jar.get(name)
        return morsel.value if morsel else None
    except Exception:
        return None


def _digest(token):
    return hashlib.sha256(str(token).encode("utf-8")).digest()


@dataclass(frozen=True)
class DisplayGrant:
    subject: str
    origin: str
    expires_at: float


@dataclass(frozen=True)
class DisplaySession:
    token: str
    subject: str
    origin: str
    expires_at: float


@dataclass(frozen=True)
class DisplayOperator:
    """One display session's exclusive claim to keyboard/pointer input.

    The generation is monotonic for the lifetime of this ``DisplayGrants``.  A new
    page never reuses an old generation, so late input from an old page is
    distinguishable even after the operator was released and acquired again.
    """

    session_digest: bytes
    generation: int
    label: str


class DisplayGrants:
    """Short-lived, single-use grants and origin-bound display sessions.

    Only SHA-256 digests of bearer values are stored, so a memory dump does not
    expose a usable token.  A grant is consumed by the first correct-origin
    exchange; a wrong origin does not consume it.  Sessions are not single-use
    (a page may reconnect), but they are origin-bound and short-lived.

    One display session at a time owns the operator generation.  The generation
    is monotonic, so an old page's late input stays distinguishable after a
    takeover or after the operator was released and acquired again.
    """

    def __init__(self, ttl=GRANT_TTL, session_ttl=DISPLAY_SESSION_TTL,
                 max_pending=MAX_PENDING_GRANTS,
                 max_sessions=MAX_DISPLAY_SESSIONS):
        self.ttl = float(ttl)
        self.session_ttl = float(session_ttl)
        self._max_pending = int(max_pending)
        self._max_sessions = int(max_sessions)
        self._lock = threading.Lock()
        self._grants = {}
        self._sessions = {}
        self._operator = None
        self._operator_generation = 0

    def issue(self, subject, origin, now=None):
        subject = str(subject or "")
        origin = str(origin or "")
        if not subject or len(subject) > 256 or not origin \
                or len(origin) > 256 or any(c in subject + origin
                                            for c in "\r\n\x00"):
            raise DisplayGrantError("display_grant_invalid_binding")
        now = time.time() if now is None else float(now)
        token = secrets.token_urlsafe(32)
        expires_at = now + self.ttl
        with self._lock:
            self._prune_locked(now)
            if len(self._grants) >= self._max_pending:
                raise DisplayGrantError("display_grant_capacity")
            self._grants[_digest(token)] = DisplayGrant(
                subject, origin, expires_at)
        return token, expires_at

    def redeem(self, token, origin, now=None):
        if not token or len(token) > MAX_GRANT_TOKEN:
            return None
        now = time.time() if now is None else float(now)
        key = _digest(token)
        with self._lock:
            self._prune_locked(now)
            grant = self._grants.get(key)
            if grant is None or grant.expires_at <= now:
                return None
            if not secrets.compare_digest(grant.origin, str(origin or "")):
                return None
            del self._grants[key]
            if len(self._sessions) >= self._max_sessions:
                return None
            session_token = secrets.token_urlsafe(32)
            session = DisplaySession(
                session_token, grant.subject, grant.origin,
                now + self.session_ttl)
            self._sessions[_digest(session_token)] = session
        return session

    def validate_session(self, token, origin, now=None):
        if not token or len(token) > MAX_GRANT_TOKEN:
            return None
        now = time.time() if now is None else float(now)
        with self._lock:
            self._prune_locked(now)
            return self._session_locked(token, origin, now)

    def operator_state(self, session_token=None, origin=None, now=None):
        """Return the single-page operator state for one display session.

        ``owned_by_me`` needs a live display session; an expired session is
        reported as a stale owner and may be replaced by a new page.
        """
        now = time.time() if now is None else float(now)
        with self._lock:
            self._prune_locked(now)
            operator = self._operator
            if operator is None:
                return {"owned_by_me": False, "owner_present": False,
                        "generation": 0, "stale": False}
            owner = self._sessions.get(operator.session_digest)
            stale = owner is None or owner.expires_at <= now
            session = self._session_locked(session_token, origin, now)
            owned = bool(
                session is not None
                and _digest(session.token) == operator.session_digest)
            return {"owned_by_me": owned, "owner_present": True,
                    "generation": operator.generation, "stale": stale}

    def acquire_operator(self, session_token, origin=None, now=None):
        """Acquire the operator for a live display session.

        Re-acquiring as the current owner is idempotent.  Another live owner
        returns ``display_operator_exists``; a stale owner is replaced with a new
        generation.
        """
        now = time.time() if now is None else float(now)
        with self._lock:
            self._prune_locked(now)
            session = self._session_locked(session_token, origin, now)
            if session is None:
                return {"acquired": False,
                        "error": "display_session_required"}
            digest = _digest(session.token)
            operator = self._operator
            if operator is not None and operator.session_digest == digest:
                return {"acquired": True, "generation": operator.generation,
                        "took_over": False}
            if operator is not None:
                owner = self._sessions.get(operator.session_digest)
                if owner is not None and owner.expires_at > now:
                    return {"acquired": False,
                            "error": "display_operator_exists",
                            "generation": operator.generation,
                            "owner_label": operator.label}
            return self._assign_operator_locked(
                digest, took_over=operator is not None)

    def takeover_operator(self, session_token, origin=None, now=None):
        """Replace the current owner and return a fresh generation.

        The current owner taking over is idempotent, so a page cannot bump its
        own generation by accident.
        """
        now = time.time() if now is None else float(now)
        with self._lock:
            self._prune_locked(now)
            session = self._session_locked(session_token, origin, now)
            if session is None:
                return {"acquired": False,
                        "error": "display_session_required"}
            digest = _digest(session.token)
            operator = self._operator
            if operator is not None and operator.session_digest == digest:
                return {"acquired": True, "generation": operator.generation,
                        "took_over": False}
            return self._assign_operator_locked(
                digest, took_over=operator is not None)

    def operator_matches(self, session_token, generation):
        """Whether this connection still owns the current generation.

        The check is deliberately independent of session expiry: an established
        WebSocket keeps its operator through a 30-minute login window even after
        the short display session used at handshake time expires.
        """
        if not session_token or len(session_token) > MAX_GRANT_TOKEN:
            return False
        try:
            generation = int(generation)
        except (TypeError, ValueError):
            return False
        digest = _digest(session_token)
        with self._lock:
            operator = self._operator
            return bool(
                operator is not None
                and operator.generation == generation
                and operator.session_digest == digest)

    def release_operator(self, session_token, generation):
        """Release only this page's own generation, never a newer owner."""
        if not session_token or len(session_token) > MAX_GRANT_TOKEN:
            return False
        try:
            generation = int(generation)
        except (TypeError, ValueError):
            return False
        digest = _digest(session_token)
        with self._lock:
            operator = self._operator
            if operator is None or operator.generation != generation \
                    or operator.session_digest != digest:
                return False
            self._operator = None
            return True

    def _assign_operator_locked(self, session_digest, took_over):
        generation = self._operator_generation + 1
        self._operator_generation = generation
        self._operator = DisplayOperator(
            session_digest, generation, OPERATOR_OWNER_LABEL)
        return {"acquired": True, "generation": generation,
                "took_over": bool(took_over)}

    def _session_locked(self, token, origin, now):
        if not token or len(token) > MAX_GRANT_TOKEN:
            return None
        session = self._sessions.get(_digest(token))
        if session is None or session.expires_at <= now:
            return None
        if not secrets.compare_digest(session.origin, str(origin or "")):
            return None
        return session

    def _prune_locked(self, now):
        for key, grant in list(self._grants.items()):
            if grant.expires_at <= now:
                del self._grants[key]
        for key, session in list(self._sessions.items()):
            if session.expires_at <= now:
                del self._sessions[key]


def _origin_for_local_request(headers, console_port, websocket):
    host = _header(headers, "Host")
    if not host or any(char in host for char in "/\\?#@ \t\r\n"):
        return None
    try:
        parsed = urlsplit(f"//{host}")
        port = parsed.port
        hostname = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if hostname not in {"127.0.0.1", "localhost", "::1"}:
        return None
    if console_port is None or port != console_port:
        return None
    origin = _header(headers, "Origin")
    expected = f"http://{host}"
    if origin is None:
        return None if websocket else expected
    return expected if origin == expected else None


def _loopback_client(client_ip):
    try:
        return ipaddress.ip_address(client_ip).is_loopback
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------- VNC auth
# VNC's d3des uses the standard DES tables with the key bytes in reverse bit
# order (bytebit = 01, 02, 04, ... instead of 80, 40, 20, ...).  The password
# file created by ``x11vnc -storepasswd`` is the 8-byte password encrypted with
# the fixed vnckey below; the challenge response is the 16-byte challenge
# encrypted in two ECB blocks with the password as key.  This is a pure-Python
# implementation so the console keeps working on Windows, where the deployment
# venv's ``cryptography`` package is not available to the local console.
_VNC_FIXED_KEY = bytes((23, 82, 107, 6, 35, 78, 88, 7))
_IP = (
    58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4,
    62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40, 32, 24, 16, 8,
    57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
    61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7,
)
_FP = (
    40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31,
    38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29,
    36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
    34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25,
)
_E = (
    32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11,
    12, 13, 12, 13, 14, 15, 16, 17, 16, 17, 18, 19, 20, 21,
    20, 21, 22, 23, 24, 25, 24, 25, 26, 27, 28, 29, 28, 29,
    30, 31, 32, 1,
)
_P = (
    16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10,
    2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25,
)
_PC1 = (
    57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2,
    59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36, 63, 55, 47, 39,
    31, 23, 15, 7, 62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37,
    29, 21, 13, 5, 28, 20, 12, 4,
)
_PC2 = (
    14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10, 23, 19, 12, 4,
    26, 8, 16, 7, 27, 20, 13, 2, 41, 52, 31, 37, 47, 55, 30, 40,
    51, 45, 33, 48, 44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32,
)
_SHIFTS = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)
_SBOX = (
    (14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7,
     0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
     4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0,
     15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13),
    (15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10,
     3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
     0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15,
     13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9),
    (10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8,
     13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7,
     1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12),
    (7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15,
     13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4,
     3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14),
    (2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9,
     14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
     4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14,
     11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3),
    (12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11,
     10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
     9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6,
     4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13),
    (4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1,
     13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
     1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2,
     6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12),
    (13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7,
     1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
     7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8,
     2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11),
)


def _permute(value, table, width):
    out = 0
    for position in table:
        out = (out << 1) | ((value >> (width - position)) & 1)
    return out


def _des_key_schedule(key):
    value = int.from_bytes(key, "big")
    permuted = _permute(value, _PC1, 64)
    left = (permuted >> 28) & 0x0FFFFFFF
    right = permuted & 0x0FFFFFFF
    keys = []
    for shift in _SHIFTS:
        left = ((left << shift) | (left >> (28 - shift))) & 0x0FFFFFFF
        right = ((right << shift) | (right >> (28 - shift))) & 0x0FFFFFFF
        keys.append(_permute((left << 28) | right, _PC2, 56))
    return keys


def _des_block(block, key, decrypt):
    keys = _des_key_schedule(key)
    if decrypt:
        keys = list(reversed(keys))
    value = _permute(int.from_bytes(block, "big"), _IP, 64)
    left = (value >> 32) & 0xFFFFFFFF
    right = value & 0xFFFFFFFF
    for key_part in keys:
        expanded = _permute(right, _E, 32)
        mixed = expanded ^ key_part
        substituted = 0
        for index in range(8):
            chunk = (mixed >> (42 - 6 * index)) & 0x3F
            row = ((chunk & 0x20) >> 4) | (chunk & 1)
            column = (chunk >> 1) & 0x0F
            substituted = (substituted << 4) \
                | _SBOX[index][row * 16 + column]
        left, right = right, left ^ _permute(substituted, _P, 32)
    return _permute((right << 32) | left, _FP, 64).to_bytes(8, "big")


def _reverse_byte(value):
    return int(f"{value:08b}"[::-1], 2)


def _vnc_key(key):
    return bytes(_reverse_byte(value) for value in key)


def _des_encrypt_block(block, key):
    return _des_block(block, key, False)


def _des_decrypt_block(block, key):
    return _des_block(block, key, True)


def vnc_password_key(file_bytes):
    """Decrypt the 8-byte x11vnc -storepasswd file into the VNC key."""
    if not isinstance(file_bytes, (bytes, bytearray)) or len(file_bytes) != 8:
        raise VncAuthError("display_vnc_auth_invalid")
    return _des_decrypt_block(bytes(file_bytes), _vnc_key(_VNC_FIXED_KEY))


def vnc_auth_response(password, challenge):
    """Compute the 16-byte VncAuth response for a challenge."""
    if not isinstance(password, (bytes, bytearray)) or len(password) != 8 \
            or not isinstance(challenge, (bytes, bytearray)) \
            or len(challenge) != 16:
        raise VncAuthError("display_vnc_auth_invalid")
    key = _vnc_key(bytes(password))
    return (_des_encrypt_block(bytes(challenge[:8]), key)
            + _des_encrypt_block(bytes(challenge[8:]), key))


def load_vnc_password(path):
    """Read and decrypt the root-only VNC password file.

    The descriptor that is actually read is fstat'ed, so a swap after the
    path check cannot bypass the mode/owner gate.  The password is returned
    only to the caller that computes the challenge response; it is never logged.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise VncAuthError("display_vnc_auth_unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != 8:
            raise VncAuthError("display_vnc_auth_invalid")
        if os.name == "posix":
            if stat.S_IMODE(info.st_mode) not in _VNC_AUTH_FILE_MODES:
                raise VncAuthError("display_vnc_auth_permissions")
            if info.st_uid not in (os.getuid(),) + _VNC_AUTH_OWNERS:
                raise VncAuthError("display_vnc_auth_owner")
        raw = os.read(fd, 8)
        if len(raw) != 8:
            raise VncAuthError("display_vnc_auth_invalid")
    except OSError as exc:
        raise VncAuthError("display_vnc_auth_unavailable") from exc
    finally:
        os.close(fd)
    return vnc_password_key(raw)


class PasswordFileVncAuth:
    """Challenge-response provider backed by the root-only password file."""

    def __init__(self, path):
        self.path = path

    def __call__(self, challenge):
        return vnc_auth_response(load_vnc_password(self.path), challenge)


class UnixVncAuthClient:
    """Ask the display service's narrow helper for a challenge response.

    The helper is the only process that reads the credential; the console sees
    only a 16-byte challenge in and a 16-byte response out.  A missing helper
    or a malformed response fails closed.
    """

    def __init__(self, path, timeout=2.0, socket_factory=None):
        self.path = path
        self.timeout = float(timeout)
        self._socket_factory = socket_factory

    def __call__(self, challenge):
        if not isinstance(challenge, (bytes, bytearray)) or len(challenge) != 16:
            raise VncAuthError("display_vnc_auth_invalid")
        sock = self._connect()
        try:
            sock.settimeout(self.timeout)
            sock.sendall(bytes(challenge))
            response = _recv_exact(sock, 16)
        except (OSError, EOFError) as exc:
            raise VncAuthError("display_vnc_auth_unavailable") from exc
        finally:
            try:
                sock.close()
            except OSError:
                pass
        return response

    def _connect(self):
        if self._socket_factory is not None:
            return self._socket_factory(self.path)
        if not hasattr(socket, "AF_UNIX"):
            raise VncAuthError("display_vnc_auth_unavailable")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self.path)
        except OSError as exc:
            try:
                sock.close()
            except OSError:
                pass
            raise VncAuthError("display_vnc_auth_unavailable") from exc
        return sock


def build_auth_provider(config):
    if config.vnc_auth_socket:
        return UnixVncAuthClient(
            config.vnc_auth_socket, timeout=config.handshake_timeout)
    if config.vnc_auth_file:
        return PasswordFileVncAuth(config.vnc_auth_file)
    return None


def _recv_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise EOFError("rfb peer closed")
        data.extend(chunk)
    return bytes(data)


def _read_security_result(upstream, major, minor):
    result = struct.unpack("!I", _recv_exact(upstream, 4))[0]
    if result == 0:
        return
    if (major, minor) >= (3, 8):
        length = struct.unpack("!I", _recv_exact(upstream, 4))[0]
        if 0 < length <= 4096:
            _recv_exact(upstream, length)
    raise VncAuthError("display_vnc_auth_failed")


def _read_server_init(upstream):
    head = _recv_exact(upstream, 24)
    name_length = struct.unpack("!I", head[20:24])[0]
    if name_length > 4096:
        raise VncAuthError("display_vnc_auth_protocol")
    return head + _recv_exact(upstream, name_length)


def _send_vnc_auth(upstream, auth_provider, types):
    if 2 not in types:
        raise VncAuthError("display_vnc_auth_unsupported")
    if auth_provider is None:
        raise VncAuthError("display_vnc_auth_unconfigured")
    # RFB 3.8: the client selects the security type BEFORE the server sends
    # the 16-byte VncAuth challenge. Omitting this byte deadlocks the handshake.
    upstream.sendall(b"")
    challenge = _recv_exact(upstream, 16)
    response = auth_provider(challenge)
    if not isinstance(response, (bytes, bytearray)) or len(response) != 16:
        raise VncAuthError("display_vnc_auth_failed")
    upstream.sendall(bytes(response))


def _security_types(upstream, major, minor):
    if (major, minor) >= (3, 8):
        count = _recv_exact(upstream, 1)[0]
        if count == 0:
            raise VncAuthError("display_vnc_auth_failed")
        return _recv_exact(upstream, count)
    if (major, minor) == (3, 7):
        count = _recv_exact(upstream, 1)[0]
        if count == 0:
            raise VncAuthError("display_vnc_auth_failed")
        return _recv_exact(upstream, count)
    # RFB 3.3 sends exactly one 32-bit security type.
    return bytes((struct.unpack("!I", _recv_exact(upstream, 4))[0],))


def perform_vnc_auth(upstream, auth_provider, timeout):
    """Complete RFB security and return the browser greeting frames.

    The browser always receives an RFB 3.8 ``None`` handshake plus the upstream
    ServerInit.  noVNC 1.6 consumes only the 12-byte protocol version from the
    first WebSocket message, so the version cannot share that frame with the
    security result or ServerInit; those follow in later frames.  The proxy already
    sent ClientInit upstream, so the relay consumes the browser's version,
    security type selection and ClientInit before forwarding.
    """
    upstream.settimeout(timeout)
    version = _recv_exact(upstream, 12)
    if not version.startswith(b"RFB ") or not version.endswith(b"\n"):
        raise VncAuthError("display_vnc_auth_protocol")
    try:
        major = int(version[4:7])
        minor = int(version[8:11])
    except ValueError as exc:
        raise VncAuthError("display_vnc_auth_protocol") from exc
    if (major, minor) not in ((3, 3), (3, 7), (3, 8)):
        raise VncAuthError("display_vnc_auth_protocol")
    upstream.sendall(b"RFB 003.008\n" if (major, minor) >= (3, 8)
                     else b"RFB 003.007\n" if (major, minor) == (3, 7)
                     else b"RFB 003.003\n")
    types = _security_types(upstream, major, minor)
    if 2 in types:
        _send_vnc_auth(upstream, auth_provider, types)
        _read_security_result(upstream, major, minor)
    elif 1 in types:
        if (major, minor) != (3, 3):
            _read_security_result(upstream, major, minor)
    else:
        raise VncAuthError("display_vnc_auth_unsupported")
    upstream.sendall(b"\x01")
    server_init = _read_server_init(upstream)
    # noVNC 3.8 None expects a SecurityResult even for type 1.
    return (b"RFB 003.008\n", b"\x01\x01", b"\x00\x00\x00\x00",
            server_init)


def _console_authorized(headers, session_valid, control_token_ok,
                        display_session_valid=None):
    cookie = _header(headers, "Cookie")
    if display_session_valid is not None \
            and display_session_valid(cookie):
        return True
    if control_token_ok is not None \
            and control_token_ok(_header(headers, "X-Admin-Control")):
        return True
    return bool(session_valid is not None and session_valid(cookie))


def _origin_checked(headers, client_ip, tailnet_policy, console_port,
                     websocket):
    """Return the accepted origin without deciding who the caller is."""
    if has_proxy_metadata(headers):
        if tailnet_policy is None \
                or not tailnet_policy.allows(headers, client_ip):
            return None
        expected = tailnet_policy.origin
        origin = _header(headers, "Origin")
        if websocket and origin is None:
            return None
        if origin not in (None, expected):
            return None
        return expected
    if not _loopback_client(client_ip):
        return None
    return _origin_for_local_request(headers, console_port, websocket)


def request_origin(headers, client_ip, tailnet_policy, console_port,
                   session_valid, control_token_ok, websocket=False,
                   *, require_authorization=True,
                   display_session_valid=None):
    """Return the accepted request origin, or None when access is refused."""
    expected = _origin_checked(
        headers, client_ip, tailnet_policy, console_port, websocket)
    if expected is None:
        return None
    if not require_authorization:
        return expected
    cookie = _header(headers, "Cookie")
    if display_session_valid is not None \
            and display_session_valid(cookie, expected):
        return expected
    if not _console_authorized(
            headers, session_valid, control_token_ok):
        return None
    return expected


def request_subject(headers, client_ip, tailnet_policy, console_port,
                    session_valid, control_token_ok,
                    display_session_valid=None):
    """Return (origin, subject) for a grant issuer, or None when refused.

    A loopback request without a management credential is *not* an administrator:
    an SSH ``-L`` tunnel also arrives as loopback.  Only Tailscale Serve's
    overwritten identity, the admin control token, the console session or an
    existing display session can bind a grant.
    """
    expected = _origin_checked(
        headers, client_ip, tailnet_policy, console_port, False)
    if expected is None:
        return None
    cookie = _header(headers, "Cookie")
    display_ok = bool(
        display_session_valid is not None
        and display_session_valid(cookie, expected))
    control_ok = bool(
        control_token_ok is not None
        and control_token_ok(_header(headers, "X-Admin-Control")))
    session_ok = bool(
        session_valid is not None and session_valid(cookie))
    if not (display_ok or control_ok or session_ok):
        return None
    if has_proxy_metadata(headers):
        login = _header(headers, "Tailscale-User-Login") or ""
        peer = _header(headers, "X-Forwarded-For") or ""
        return expected, "tailscale:%s:%s" % (login.casefold(), peer)
    if display_ok:
        return expected, "display-session"
    if control_ok:
        return expected, "control-token"
    return expected, "usage-session"


def accept_key(key):
    """Compute the RFC 6455 Sec-WebSocket-Accept value.

    SHA-1 is mandated by RFC 6455 section 4.2.2 for the handshake challenge
    only.  It is not a security primitive here: the browser nonce prevents a
    cross-protocol cache attack, and TLS (Tailscale Serve or loopback) protects
    the transport.  Replacing it with SHA-256 would make the handshake invalid.
    """
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def validate_websocket_handshake(headers):
    """Return (status, error, protocol); status 0 means a valid upgrade."""
    upgrade = _header(headers, "Upgrade")
    if upgrade is None or upgrade.strip().lower() != "websocket":
        return 400, "websocket upgrade required", None
    connection = _header(headers, "Connection")
    tokens = [token.strip().lower() for token in (connection or "").split(",")]
    if "upgrade" not in tokens:
        return 400, "websocket upgrade required", None
    if _header(headers, "Sec-WebSocket-Version") != "13":
        return 426, "unsupported websocket version", None
    key = _header(headers, "Sec-WebSocket-Key")
    if key is None:
        return 400, "websocket key required", None
    try:
        raw = base64.b64decode(key, validate=True)
    except (ValueError, TypeError):
        return 400, "invalid websocket key", None
    if len(raw) != 16:
        return 400, "invalid websocket key", None
    protocols = []
    rows = headers.get_all("Sec-WebSocket-Protocol", []) \
        if hasattr(headers, "get_all") else []
    for row in rows:
        protocols.extend(item.strip() for item in row.split(",") if item.strip())
    if protocols and "binary" not in protocols:
        return 400, "unsupported websocket protocol", None
    return 0, "", "binary" if protocols else None


def _send_json(handler, status, error):
    body = json.dumps({"error": error}, ensure_ascii=False).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS:
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    try:
        handler.close_connection = True
    except AttributeError:
        pass


def _send_json_body(handler, status, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS:
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    try:
        handler.close_connection = True
    except AttributeError:
        pass


def _send_empty(handler, status, headers=()):
    try:
        handler.send_response(status)
        handler.send_header("Content-Length", "0")
        handler.send_header("Cache-Control", "no-store")
        for name, value in headers:
            handler.send_header(name, value)
        handler.end_headers()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    try:
        handler.close_connection = True
    except AttributeError:
        pass


def _send_redirect(handler, location):
    try:
        handler.send_response(308)
        handler.send_header("Location", location)
        handler.send_header("Content-Length", "0")
        for name, value in SECURITY_HEADERS:
            handler.send_header(name, value)
        handler.end_headers()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    try:
        handler.close_connection = True
    except AttributeError:
        pass


def _safe_asset(relative):
    if not relative or any(char in relative for char in "\\%\x00:"):
        return False
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    return Path(parts[-1]).suffix.lower() in STATIC_TYPES


def _inside(root, target):
    try:
        return os.path.commonpath([root, target]) == root
    except ValueError:
        return False


def _operator_generation(value):
    """Parse a canonical positive decimal generation, or return None."""
    if not isinstance(value, str) or not value or len(value) > 19:
        return None
    if value[0] == "0" or any(char not in "0123456789" for char in value):
        return None
    generation = int(value, 10)
    return generation if generation > 0 else None


class DisplayProxy:
    """Route ``/display`` requests from the console HTTP handler."""

    def __init__(self, config, *, session_valid=None, control_token_ok=None,
                 grants=None, auth_provider=_AUTH_UNSET):
        self.config = config
        self.session_valid = session_valid
        self.control_token_ok = control_token_ok
        self.grants = grants if grants is not None else DisplayGrants()
        self.auth_provider = build_auth_provider(config) \
            if auth_provider is _AUTH_UNSET else auth_provider
        self._limiter = ConnectionLimiter(config.max_connections)
        self._relays = set()
        self._relays_lock = threading.Lock()

    @property
    def active_connections(self):
        return self._limiter.active

    def close_all(self):
        """Cancel every active display relay (server shutdown/cancel path)."""
        with self._relays_lock:
            relays = list(self._relays)
        for relay in relays:
            relay.cancel(1001)

    def handle_get(self, handler, *, tailnet_policy=None, console_port=None):
        """Handle one GET; return False only when the path is not ours."""
        parsed = urlsplit(getattr(handler, "path", ""))
        path = parsed.path
        if path != DISPLAY_PREFIX and not path.startswith(DISPLAY_PREFIX + "/"):
            return False
        if parsed.fragment or parsed.username or parsed.password:
            _send_json(handler, 400, "display query not allowed")
            return True
        websocket = path == WEBSOCKET_PATH
        operator_generation = None
        if websocket:
            # Only the operator generation may ride a query string.  Every
            # other query value, a duplicate, or a non-canonical generation
            # is rejected before any authorization or upgrade.
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            if pairs:
                if len(pairs) != 1 or pairs[0][0] != "operator":
                    _send_json(handler, 400, "display query not allowed")
                    return True
                operator_generation = _operator_generation(pairs[0][1])
                if operator_generation is None:
                    _send_json(handler, 400, "display query not allowed")
                    return True
        elif parsed.query:
            _send_json(handler, 400, "display query not allowed")
            return True
        if not self.config.enabled:
            _send_json(handler, 503, self.config.error or "display unavailable")
            return True
        try:
            client_ip = handler.client_address[0]
        except (AttributeError, IndexError, TypeError):
            client_ip = ""
        if path == GRANT_PATH:
            # The grant page is static code, not a secret: it may load without
            # a session so its module script can exchange a grant fragment.
            if request_origin(
                    handler.headers, client_ip, tailnet_policy, console_port,
                    self.session_valid, self.control_token_ok,
                    require_authorization=False) is None:
                _send_json(handler, 403, "display access denied")
                return True
            self._serve_static(handler, DISPLAY_PREFIX + "/")
            return True
        if path == SESSION_PATH:
            if request_origin(
                    handler.headers, client_ip, tailnet_policy, console_port,
                    self.session_valid, self.control_token_ok,
                    require_authorization=False) is None:
                _send_json(handler, 403, "display access denied")
                return True
            self._serve_session(handler, client_ip, tailnet_policy,
                                 console_port)
            return True
        websocket = path == WEBSOCKET_PATH
        require_authorization = websocket or path in (
            DISPLAY_PREFIX, DISPLAY_PREFIX + "/")
        origin = request_origin(
            handler.headers, client_ip, tailnet_policy, console_port,
            self.session_valid, self.control_token_ok, websocket,
            require_authorization=require_authorization,
            display_session_valid=self._display_session_valid)
        if origin is None:
            _send_json(handler, 403, "display access denied")
            return True
        if websocket:
            self._serve_websocket(handler, origin, operator_generation)
        else:
            self._serve_static(handler, path)
        return True

    def handle_post(self, handler, *, tailnet_policy=None,
                   console_port=None):
        """Handle one POST; return False only when the path is not ours."""
        parsed = urlsplit(getattr(handler, "path", ""))
        path = parsed.path
        if path != GRANT_PATH:
            if path == DISPLAY_PREFIX or path.startswith(DISPLAY_PREFIX + "/"):
                _send_json(handler, 404, "unknown display path")
                return True
            return False
        if parsed.query or parsed.fragment or parsed.username \
                or parsed.password:
            _send_json(handler, 400, "display query not allowed")
            return True
        if not self.config.enabled:
            _send_json(handler, 503, self.config.error or "display unavailable")
            return True
        try:
            client_ip = handler.client_address[0]
        except (AttributeError, IndexError, TypeError):
            client_ip = ""
        origin = request_origin(
            handler.headers, client_ip, tailnet_policy, console_port,
            self.session_valid, self.control_token_ok,
            require_authorization=False)
        if origin is None:
            _send_json(handler, 403, "display access denied")
            return True
        self._serve_grant_exchange(handler, origin)
        return True

    def _display_session_valid(self, cookie_header, origin):
        return self.grants.validate_session(
            _cookie_value(cookie_header, DISPLAY_SESSION_COOKIE), origin)

    def _display_authorized(self, headers, origin):
        cookie = _header(headers, "Cookie")
        if self._display_session_valid(cookie, origin):
            return True
        if self.control_token_ok is not None \
                and self.control_token_ok(_header(headers, "X-Admin-Control")):
            return True
        return bool(self.session_valid is not None
                    and self.session_valid(cookie))

    def operator_state(self, cookie_header=None, origin=None):
        """Operator state for the management read face.

        The caller passes the raw Cookie header; only a live display session can
        report ``owned_by_me``.  An admin request without a display session
        still sees whether another page holds the operator.
        """
        return self.grants.operator_state(
            _cookie_value(cookie_header, DISPLAY_SESSION_COOKIE), origin)

    def acquire_operator(self, cookie_header=None, origin=None,
                        action="acquire"):
        """Acquire or take over the operator for a display session."""
        token = _cookie_value(cookie_header, DISPLAY_SESSION_COOKIE)
        session = self.grants.validate_session(token, origin)
        if session is None:
            return {"acquired": False, "error": "display_session_required"}
        if action == "takeover":
            return self.grants.takeover_operator(session.token, origin)
        if action != "acquire":
            return {"acquired": False,
                    "error": "display_operator_action_invalid"}
        return self.grants.acquire_operator(session.token, origin)

    def _serve_session(self, handler, client_ip, tailnet_policy,
                       console_port):
        parsed = urlsplit(getattr(handler, "path", ""))
        origin = _origin_checked(
            handler.headers, client_ip, tailnet_policy, console_port, False)
        if origin is None:
            _send_json(handler, 403, "display access denied")
            return
        authorized = self._display_authorized(handler.headers, origin)
        payload = {"authorized": bool(authorized), "expires_at": None}
        if authorized:
            session = self.grants.validate_session(
                _cookie_value(_header(handler.headers, "Cookie"),
                              DISPLAY_SESSION_COOKIE), origin)
            if session is not None:
                payload["expires_at"] = int(session.expires_at)
        _send_json_body(handler, 200, payload)

    def _serve_grant_exchange(self, handler, origin):
        lengths = handler.headers.get_all("Content-Length", []) \
            if hasattr(handler.headers, "get_all") else []
        if len(lengths) != 1:
            _send_empty(handler, 400)
            return
        try:
            length = int(lengths[0])
        except (TypeError, ValueError):
            _send_empty(handler, 400)
            return
        if length <= 0:
            _send_empty(handler, 400)
            return
        if length > MAX_GRANT_BODY:
            # Discard a bounded prefix so the client can read the 413 instead
            # of seeing a reset caused by unread request bytes.
            try:
                handler.connection.settimeout(1.0)
                handler.rfile.read(MAX_GRANT_BODY)
            except (OSError, ValueError):
                pass
            _send_empty(handler, 413)
            return
        try:
            body = handler.rfile.read(length)
        except (OSError, ValueError):
            _send_empty(handler, 400)
            return
        content_type = handler.headers.get("Content-Type", "")
        if not content_type.split(";", 1)[0].strip().lower() \
                == "application/json":
            _send_empty(handler, 415)
            return
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            _send_empty(handler, 400)
            return
        token = document.get("grant") if isinstance(document, dict) else None
        if not isinstance(token, str) or not 16 <= len(token) <= MAX_GRANT_TOKEN \
                or any(c in token for c in "\r\n\x00"):
            _send_empty(handler, 400)
            return
        session = self.grants.redeem(token, origin)
        if session is None:
            _send_empty(handler, 403)
            return
        max_age = max(1, int(session.expires_at - time.time()))
        cookie = (
            "%s=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d"
            % (DISPLAY_SESSION_COOKIE, session.token, max_age))
        if origin.startswith("https://"):
            cookie += "; Secure"
        _send_empty(handler, 204, (("Set-Cookie", cookie),))

    def _serve_static(self, handler, path):
        if path == DISPLAY_PREFIX:
            _send_redirect(handler, DISPLAY_PREFIX + "/")
            return
        relative = path[len(DISPLAY_PREFIX) + 1:]
        if not relative:
            target = Path(self.config.launcher_file).resolve()
            content_type = "text/html; charset=utf-8"
        elif relative in LAUNCHER_ASSETS:
            # A launcher asset, never resolved against the noVNC root: a
            # same-named file there must not shadow it.
            target = (Path(self.config.launcher_file).resolve().parent
                      / LAUNCHER_ASSETS[relative])
            content_type = STATIC_TYPES[".js"]
        elif _safe_asset(relative):
            root = Path(self.config.novnc_root).resolve()
            target = (root / relative).resolve()
            if not _inside(str(root), str(target)):
                _send_json(handler, 404, "unknown display asset")
                return
            content_type = STATIC_TYPES.get(target.suffix.lower(),
                                            "application/octet-stream")
        else:
            _send_json(handler, 404, "unknown display asset")
            return
        try:
            body = target.read_bytes()
        except OSError:
            _send_json(handler, 404, "unknown display asset")
            return
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Content-Length", str(len(body)))
            for name, value in SECURITY_HEADERS:
                handler.send_header(name, value)
            handler.end_headers()
            handler.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _serve_websocket(self, handler, origin, operator_generation):
        status, error, protocol = validate_websocket_handshake(handler.headers)
        if status:
            _send_json(handler, status, error)
            return
        # Only the current operator generation may open the relay.  A missing or
        # stale generation is refused before the upstream is contacted, so an old
        # page can never attach a second control channel.
        session_token = _cookie_value(
            _header(handler.headers, "Cookie"), DISPLAY_SESSION_COOKIE)
        if self.grants.validate_session(session_token, origin) is None:
            _send_json(handler, 403, "display access denied")
            return
        if operator_generation is None \
                or not self.grants.operator_matches(
                    session_token, operator_generation):
            _send_json(handler, 409, "display_operator_stale")
            return
        if not self._limiter.acquire():
            _send_json(handler, 503, "display busy")
            return
        upgraded = False
        upstream = None
        relay = None
        try:
            try:
                upstream = socket.create_connection(
                    (self.config.vnc_host, self.config.vnc_port),
                    timeout=self.config.handshake_timeout)
            except OSError:
                _send_json(handler, 502, "display upstream unavailable")
                return
            greeting_frames = ()
            if self.auth_provider is not None:
                try:
                    greeting_frames = perform_vnc_auth(
                        upstream, self.auth_provider,
                        self.config.handshake_timeout)
                except (VncAuthError, OSError, EOFError) as exc:
                    _send_json(handler, 502,
                                str(exc) or "display vnc auth failed")
                    return
            try:
                relay = WebSocketRelay(
                    handler, upstream,
                    idle_timeout=self.config.idle_timeout,
                    max_session_seconds=self.config.max_session_seconds,
                    join_timeout=self.config.relay_join_timeout,
                    client_discard=14 if greeting_frames else 0,
                    input_allowed=lambda: self.grants.operator_matches(
                        session_token, operator_generation))
            except OSError:
                return
            # Register before the 101 so a shutdown/cancel racing the upgrade
            # can find the relay; cancel() waits for mark_started().
            with self._relays_lock:
                self._relays.add(relay)
            try:
                _send_upgrade(handler, protocol)
                upgraded = True
                for payload in greeting_frames:
                    send_frame(handler.connection, 0x02, payload)
            except OSError:
                return
            relay.mark_started()
            relay.run()
        finally:
            if relay is not None:
                relay.close()
                with self._relays_lock:
                    self._relays.discard(relay)
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass
            if upgraded:
                # An established relay releases only its own generation.  A late
                # close from an old page cannot release a newer owner.
                self.grants.release_operator(
                    session_token, operator_generation)
            self._limiter.release()


def _send_upgrade(handler, protocol):
    key = _header(handler.headers, "Sec-WebSocket-Key")
    lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {accept_key(key)}",
    ]
    if protocol:
        lines.append(f"Sec-WebSocket-Protocol: {protocol}")
    lines.append("Cache-Control: no-store")
    lines.extend(("", ""))
    handler.wfile.write("\r\n".join(lines).encode("ascii"))
    handler.wfile.flush()
    handler.close_connection = True


@dataclass(frozen=True)
class Frame:
    opcode: int
    payload: bytes


def _read_exact(reader, size):
    data = bytearray()
    while len(data) < size:
        chunk = reader.read(size - len(data))
        if not chunk:
            raise EOFError("websocket peer closed")
        data.extend(chunk)
    return bytes(data)


class _SocketReader:
    """Exact-size reader with an explicit buffer for WebSocket frames.

    A ``BufferedReader`` may read ahead a whole next frame into user space, so
    ``select()`` on the socket then reports nothing even though a frame is
    pending.  Keeping the buffer here lets the relay check ``has_data()`` before
    waiting on the socket.
    """

    def __init__(self, sock):
        self.sock = sock
        self.buffer = bytearray()

    def has_data(self):
        return bool(self.buffer)

    def read(self, size):
        while len(self.buffer) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("websocket peer closed")
            self.buffer.extend(chunk)
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data


def read_frame(reader):
    """Read one masked client frame with a bounded payload."""
    first, second = _read_exact(reader, 2)
    final = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if first & 0x70:
        raise ProtocolError("reserved websocket bits set")
    if not masked:
        raise ProtocolError("client websocket frame is not masked")
    if length == 126:
        length = struct.unpack("!H", _read_exact(reader, 2))[0]
        if length < 126:
            raise ProtocolError("non-minimal websocket length")
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(reader, 8))[0]
        if length <= 0xFFFF or length >> 63:
            raise ProtocolError("invalid websocket length")
    if length > MAX_FRAME_PAYLOAD:
        raise ProtocolError("websocket frame too large")
    if opcode & 0x08 and (not final or length > 125):
        raise ProtocolError("invalid websocket control frame")
    mask = _read_exact(reader, 4)
    payload = bytearray(_read_exact(reader, length))
    for index in range(length):
        payload[index] ^= mask[index % 4]
    return Frame(opcode, bytes(payload))


def send_frame(sock, opcode, payload, timeout=None):
    """Write one unmasked server frame.

    ``timeout`` bounds the write so a peer that stopped reading cannot park
    the sender forever.  The whole frame is still one ``sendall`` call, so a
    frame header never interleaves with another frame's payload.
    """
    length = len(payload)
    header = bytearray((0x80 | opcode,))
    if length < 126:
        header.append(length)
    elif length <= 0xFFFF:
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    previous = None
    if timeout is not None:
        try:
            previous = sock.gettimeout()
            sock.settimeout(timeout)
        except OSError:
            previous = None
    try:
        sock.sendall(bytes(header) + payload)
    finally:
        if timeout is not None and previous is not None:
            try:
                sock.settimeout(previous)
            except OSError:
                pass


def send_close(sock, code=1000, reason=b""):
    """Write a close frame without ever logging its payload."""
    payload = struct.pack("!H", code) + reason[:MAX_CLOSE_REASON]
    try:
        send_frame(sock, 0x08, payload)
    except OSError:
        pass


class WebSocketRelay:
    """Bridge one WebSocket client to one raw RFB socket."""

    def __init__(self, handler, upstream, *, idle_timeout, max_session_seconds,
                 join_timeout=5.0, client_discard=0, input_allowed=None):
        self.client_socket = handler.connection
        self.client_reader = _SocketReader(handler.connection)
        # A duplicate socket object gives the sender its own timeout.  Setting a
        # timeout on the shared object would race the reader's frame timeout.
        # A test double without dup() falls back to the same object.
        try:
            self._send_socket = self.client_socket.dup()
        except (AttributeError, OSError):
            self._send_socket = self.client_socket
        self.upstream_socket = upstream
        # The TCP connect timeout only bounds the connect itself.  Leaving it
        # in place made an idle RFB screen raise socket.timeout and close the
        # whole session.  The reader loop selects on the socket plus a wakeup
        # pipe and reads only when data is ready; the socket itself stays
        # blocking so a raw recv() never leaves a BufferedReader stuck.
        upstream.settimeout(None)
        self._wakeup_reader, self._wakeup_writer = socket.socketpair()
        self.idle_timeout = idle_timeout
        self.max_session_seconds = max_session_seconds
        self.join_timeout = join_timeout
        self.client_discard_remaining = max(0, int(client_discard))
        # Ownership applies only to keyboard/pointer input.  Viewing, pixel
        # format, encoding and update requests stay available to every page.
        self.input_allowed = input_allowed
        self.stop_event = threading.Event()
        self.started = time.monotonic()
        self.last_client_activity = self.started
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        # Every WebSocket frame is written by one of four threads (binary from
        # the RFB side, pong and close from the client side, ping from the
        # keepalive).  sendall() may issue several send() calls, so without
        # this lock a frame header from one thread could land inside another
        # thread's payload.
        self._send_lock = threading.Lock()
        self._closed = False
        self._close_sent = False
        self._threads = ()
        self._started = threading.Event()

    @property
    def worker_threads(self):
        return self._threads

    def mark_started(self):
        self._started.set()

    def run(self):
        """Relay until one side ends, is cancelled, or a timeout fires.

        The old implementation joined each worker for 5 seconds and then called
        _stop() unconditionally, which killed a healthy session after 20
        seconds.  The relay now blocks on the stop event; the workers themselves
        call _stop() on client close, upstream EOF, protocol error, idle expiry,
        session expiry or cancellation.  The join below is only a bounded cleanup
        after the session has already ended.
        """
        threads = [
            threading.Thread(target=self._client_to_upstream, daemon=True),
            threading.Thread(target=self._upstream_to_client, daemon=True),
            threading.Thread(target=self._watchdog, daemon=True),
            threading.Thread(target=self._keepalive, daemon=True),
        ]
        self._threads = tuple(threads)
        for thread in threads:
            thread.start()
        self.mark_started()
        try:
            self.stop_event.wait()
        finally:
            self._stop()
            deadline = time.monotonic() + self.join_timeout
            for thread in threads:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    thread.join(remaining)
            self.close()

    def close(self):
        """Release the wakeup pair after the workers have exited."""
        self._stop()
        for wakeup in (self._wakeup_reader, self._wakeup_writer):
            try:
                wakeup.close()
            except OSError:
                pass

    def cancel(self, code=1001):
        """End the session from the server side (shutdown/cancel path)."""
        if not self._started.wait(1.0):
            self._stop()
            return
        self._send_close_once(code)
        self._stop()

    def _stop(self):
        """Close both sockets and release the workers without waiting on sends.

        A client that stopped reading leaves a worker parked in ``sendall()`` while
        holding ``_send_lock``.  This path therefore never acquires that lock:
        ``shutdown()`` releases the parked sender on the usual platforms, the
        bounded send timeout (``SEND_FRAME_TIMEOUT``) releases it where shutdown
        does not wake the syscall, and ``close()`` releases a worker parked in
        ``recv()`` (needed on Windows).  Holding ``_lock`` across the close
        keeps a second caller from returning before both sockets are closed.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.stop_event.set()
            try:
                self._wakeup_writer.send(b"x")
            except OSError:
                pass
            sockets = [self.client_socket, self.upstream_socket]
            if self._send_socket is not self.client_socket:
                sockets.append(self._send_socket)
            for sock in sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                # On Windows a blocking recv() in another worker is not woken by
                # shutdown() alone; closing the socket is what releases the reader.
                try:
                    sock.close()
                except OSError:
                    pass

    def _send(self, opcode, payload):
        """Write one whole frame; frames never interleave across threads.

        The bounded timeout makes a stalled peer end the session instead of
        pinning this worker in ``sendall()`` forever.
        """
        with self._send_lock:
            send_frame(self._send_socket, opcode, payload,
                        timeout=SEND_FRAME_TIMEOUT)

    def _client_to_upstream(self):
        try:
            while not self.stop_event.is_set():
                if not self.client_reader.has_data():
                    try:
                        readable, _, _ = select.select(
                            (self.client_socket, self._wakeup_reader),
                            (), [], 1.0)
                    except (OSError, ValueError):
                        return
                    if self._wakeup_reader in readable:
                        return
                    if self.client_socket not in readable:
                        continue
                # Wait for the first byte of a frame indefinitely (an idle
                # login window produces no input).  Once a frame has started,
                # bound the rest so a stalled peer cannot hold the worker.
                self.client_socket.settimeout(CLIENT_FRAME_TIMEOUT)
                try:
                    frame = read_frame(self.client_reader)
                finally:
                    try:
                        self.client_socket.settimeout(None)
                    except OSError:
                        pass
                if frame.opcode == 0x08:
                    self._send_close_once(self._close_code(frame.payload))
                    return
                if frame.opcode == 0x09:
                    self._send(0x0A, frame.payload)
                    continue
                if frame.opcode == 0x0A:
                    continue
                if frame.opcode != 0x02:
                    raise ProtocolError("only binary RFB frames are accepted", 1003)
                payload = frame.payload
                if payload:
                    # Only real RFB input (including ClientInit) counts as
                    # activity.  Browsers answer keepalive pings automatically;
                    # those pongs must not keep an idle screen alive forever.
                    self.last_client_activity = time.monotonic()
                if payload and self.client_discard_remaining:
                    # The proxy already completed the upstream handshake.  noVNC
                    # still sends its own client version (12 bytes), a security
                    # type selection (1 byte, type None) and ClientInit
                    # (1 byte); discard exactly that prefix, across frame
                    # boundaries, so x11vnc never sees "RFB 003.008" as a
                    # client message (the first byte would be 'F' = 0x46).
                    drop = min(len(payload), self.client_discard_remaining)
                    self.client_discard_remaining -= drop
                    payload = payload[drop:]
                    if not payload:
                        continue
                if payload and self.input_allowed is not None \
                        and payload[0] in (4, 5):
                    # RFB KeyEvent (4) and PointerEvent (5) are input.  A
                    # page whose generation was superseded stops operating but
                    # stays connected as a viewer; a failed ownership read
                    # fails closed.
                    try:
                        allowed = bool(self.input_allowed())
                    except Exception:  # noqa: BLE001  ownership read fails closed
                        allowed = False
                    if not allowed:
                        continue
                if payload:
                    self.upstream_socket.sendall(payload)
        except TimeoutError:
            self._send_close_once(1002)
        except ProtocolError as exc:
            self._send_close_once(exc.code)
        except (EOFError, OSError, ValueError):
            self._send_close_once(1000)
        finally:
            self._stop()

    def _upstream_to_client(self):
        try:
            while not self.stop_event.is_set():
                try:
                    readable, _, _ = select.select(
                        (self.upstream_socket, self._wakeup_reader), (), [], 1.0)
                except (OSError, ValueError):
                    return
                if self._wakeup_reader in readable:
                    return
                if self.upstream_socket not in readable:
                    continue
                try:
                    data = self.upstream_socket.recv(UPSTREAM_READ_SIZE)
                except TimeoutError:
                    continue
                if not data:
                    self._send_close_once(1000)
                    return
                self._send(0x02, data)
        except (EOFError, OSError, ValueError):
            pass
        finally:
            self._stop()

    def _watchdog(self):
        while not self.stop_event.wait(1.0):
            now = time.monotonic()
            if now - self.started > self.max_session_seconds \
                    or now - self.last_client_activity > self.idle_timeout:
                self._send_close_once(1000)
                self._stop()
                return

    def _keepalive(self):
        interval = min(PING_INTERVAL, max(1.0, self.idle_timeout / 3.0))
        while not self.stop_event.wait(interval):
            try:
                self._send(0x09, secrets.token_bytes(8))
            except OSError:
                self._stop()
                return

    def _close_code(self, payload):
        if len(payload) < 2:
            return 1000
        value = struct.unpack("!H", payload[:2])[0]
        if 1000 <= value <= 4999 and value not in (1004, 1005, 1006, 1015):
            return value
        return 1002

    def _send_close_once(self, code):
        """Best-effort close frame; bounded so it cannot block termination.

        When the client stopped reading, a data frame is parked in ``sendall()``
        with ``_send_lock`` held.  Waiting for that lock unbounded made cancel()
        and the expiry watchdog hang before ``_stop()`` ever ran, so the close frame
        is skipped when the lock or the write cannot complete in time.  When the
        lock is taken, the frame is still written whole under it, preserving the
        one-writer-per-frame rule.
        """
        with self._close_lock:
            if self._close_sent:
                return
            self._close_sent = True
        if not self._send_lock.acquire(timeout=CLOSE_FRAME_TIMEOUT):
            return
        previous = None
        try:
            previous = self._send_socket.gettimeout()
            self._send_socket.settimeout(CLOSE_FRAME_TIMEOUT)
            send_frame(self._send_socket, 0x08, struct.pack("!H", code))
            # Half-close only after the close frame is queued: the peer can
            # still read the close code before the relay tears the socket down.
            self.client_socket.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        finally:
            try:
                self._send_socket.settimeout(previous)
            except OSError:
                pass
            self._send_lock.release()
