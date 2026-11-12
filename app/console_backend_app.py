# -*- coding: utf-8 -*-
"""Shared backend assembly for the two composition roots (E19-1).

``cloud/backend/app.py`` and ``local/backend/app.py`` are composition roots: each
owns its own routes, its own app lifecycle and its own ``build_app``/``main``. What
they do NOT own separately is the loopback HTTP plumbing and the pid/log/control
token helpers — those were duplicated verbatim and had already drifted (the cloud
copy passed the same FILETIME twice to ``GetProcessTimes``; the local copy had the
stale-pid-file rule). This module is the single copy:

* pid identity: read/write/remove a pid file that records the owner's creation time,
  so a RECYCLED pid is recognised as stale instead of blocking every restart;
* the bounded append log (one file, truncated at ``LOG_MAX_BYTES``);
* the loopback request guard (Host/Origin/proxy metadata) and the control token
  reader (0600/current-uid gate, never an empty expected value);
* ``BackendHTTPServer`` (bounded drain) and ``BackendHandler`` (the shared
  plumbing methods). A role subclasses both and adds its own routes; the role keeps
  its own ``_control_ok`` because the local role also accepts a session cookie.

Nothing here starts a process, opens a port or writes a file at import time.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from share.secret_protection import read_private_secret_path

__all__ = [
    "LOOPBACK_HOSTS", "PROXY_METADATA_HEADERS", "MAX_BODY_BYTES",
    "PID_IDENTITY_TOLERANCE_S", "PID_FILE_STALE_AFTER_S", "LOG_MAX_BYTES",
    "pid_alive", "read_pid_identity", "process_creation_epoch",
    "read_pid_file", "write_pid_file", "remove_pid_file",
    "append_bounded_log", "hostname_of", "has_proxy_metadata",
    "read_control_token", "BackendHTTPServer", "BackendHandler",
]

LOOPBACK_HOSTS = frozenset(("127.0.0.1", "::1", "localhost"))
PROXY_METADATA_HEADERS = ("Forwarded", "X-Forwarded-For",
                          "X-Forwarded-Host", "X-Forwarded-Proto",
                          "X-Real-IP")
MAX_BODY_BYTES = 64 * 1024
# Windows' process creation time and ``os.kill`` round differently; the tolerance
# absorbs the clock's rounding so a RECYCLED pid is recognised as stale instead of
# refusing every restart.
PID_IDENTITY_TOLERANCE_S = 2.0
# A pid file with unreadable content that is younger than this belongs to a
# starter that has not finished writing it; an older one is abandoned state.
PID_FILE_STALE_AFTER_S = 30.0
# Bounded append log: one 1 MiB file, rotated by truncation.
LOG_MAX_BYTES = 1024 * 1024


def pid_alive(pid, *, pid_file=None):
    """Read-only PID liveness probe; never signals the process.

    ``os.kill(pid, 0)`` alone is not enough: on Windows a process that the
    caller still holds a handle for keeps answering "alive" after it exited, and a
    bare PID says nothing about WHO owns it (a recycled PID would block every
    restart). When the pid file is given, its recorded start time is compared
    against the live process's creation time, so a recycled PID is recognised as
    stale instead of refusing the start forever.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    if pid_file is None:
        return True
    recorded = read_pid_identity(pid_file)
    if recorded is None:
        # A file without a recorded start time can only be judged by the PID.
        return True
    created = process_creation_epoch(pid)
    if created is None:
        # No creation time means no ownership evidence: treat as not ours.
        return False
    return abs(created - recorded) < PID_IDENTITY_TOLERANCE_S


def read_pid_identity(path):
    """Read the start time recorded next to the pid; None when absent."""
    try:
        raw = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    parts = raw.split()
    if len(parts) < 2:
        return None
    try:
        return float(parts[1])
    except ValueError:
        return None


def process_creation_epoch(pid):
    """The process's creation time (epoch seconds), or None when unknown.

    This is the ownership fact a recycled PID cannot fake. Windows uses the
    process creation time from the kernel; POSIX reads ``/proc/<pid>/stat`` and
    falls back to None (the PID probe alone then decides).
    """
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(
                process_query_limited_information, False, int(pid))
            if not handle:
                return None
            try:
                created = wintypes.FILETIME()
                exited = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                kernel32.GetProcessTimes.argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(wintypes.FILETIME),
                    ctypes.POINTER(wintypes.FILETIME),
                    ctypes.POINTER(wintypes.FILETIME),
                    ctypes.POINTER(wintypes.FILETIME)]
                if not kernel32.GetProcessTimes(
                        handle, ctypes.byref(created), ctypes.byref(exited),
                        ctypes.byref(kernel), ctypes.byref(user)):
                    return None
                # FILETIME counts 100ns intervals since 1601-01-01 UTC.
                ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
                return ticks / 10_000_000.0 - 11_644_473_600.0
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - no evidence is not "alive"
            return None
    try:
        stat = Path("/proc/%d/stat" % int(pid)).read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return None
    # /proc/<pid>/stat field 22 is starttime in clock ticks since boot.
    fields = stat.rsplit(")", 1)[-1].split()
    try:
        start_ticks = int(fields[19])
        with open("/proc/stat", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("btime "):
                    boot = float(line.split()[1])
                    break
            else:
                return None
        hertz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        return boot + start_ticks / float(hertz)
    except (IndexError, OSError, ValueError):
        return None


def read_pid_file(path):
    """Read the pid recorded in the file (first token); None when unreadable."""
    try:
        raw = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    if not raw:
        return None
    try:
        return int(raw.split()[0])
    except ValueError:
        return None


def write_pid_file(path, pid):
    """Atomic mutual exclusion: the pid file names the ONE live owner.

    A stale file (its recorded PID is gone, or the recorded start time belongs
    to a different process) is replaced - otherwise a crashed previous run would
    block every restart. The file carries the owner's creation time next to the
    PID, so a RECYCLED pid is not mistaken for the previous owner.

    The exclusive create is retried with a fresh identity: the file is never
    unlinked blindly, because a half-written file belongs to a concurrent
    starter, not to us.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    identity = process_creation_epoch(pid)
    body = "%d %s" % (pid, "" if identity is None else "%.6f" % identity)
    existing = read_pid_file(path)
    if existing is not None and existing != pid \
            and pid_alive(existing, pid_file=path):
        raise RuntimeError(
            "another app instance is already running (pid %d registered in %s)"
            % (existing, path))
    for _attempt in range(5):
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # Someone else owns the file right now. Only an ABSENT or STALE
            # owner may be replaced; a half-written file is left to its writer.
            current = read_pid_file(path)
            if current is not None and current != pid \
                    and pid_alive(current, pid_file=path):
                raise RuntimeError(
                    "another app instance is already running (pid %d registered "
                    "in %s)" % (current, path))
            if current is None:
                # Unreadable content. A FRESH file is a concurrent starter's
                # half-written file (never unlinked); an OLD one is abandoned
                # state from a crash, which may be replaced.
                try:
                    age = time.time() - os.path.getmtime(path)
                except OSError:
                    raise RuntimeError(
                        "the pid file %s is not readable" % path) from None
                if age < PID_FILE_STALE_AFTER_S:
                    raise RuntimeError(
                        "the pid file %s is being written by another starter"
                        % path)
            try:
                Path(path).unlink()
            except OSError:
                pass
            continue
        try:
            os.write(handle, body.encode("ascii"))
        finally:
            os.close(handle)
        return
    raise RuntimeError(
        "the pid file %s could not be claimed exclusively" % path)


def remove_pid_file(path, pid):
    """Remove the pid file only when it still names OUR pid."""
    if read_pid_file(path) == pid:
        try:
            Path(path).unlink()
        except OSError:
            pass


def append_bounded_log(path, line):
    """Append one line, truncating first when the file reached its bound."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) >= LOG_MAX_BYTES:
            with open(path, "wb"):
                pass
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line.rstrip("\n") + "\n")
    except OSError:
        # A log write must never fail a request.
        pass


def hostname_of(host_header):
    if not isinstance(host_header, str) or not host_header.strip():
        return ""
    try:
        parts = urlparse("//" + host_header.strip())
        parts.port  # a malformed port raises ValueError
    except ValueError:
        return ""
    return (parts.hostname or "").lower()


def has_proxy_metadata(headers):
    return any(headers.get(name) for name in PROXY_METADATA_HEADERS)


def read_control_token(path):
    """Read the control token through the 0600/current-uid gate.

    Returns None on any failure: a missing/unreadable/too-wide token file is
    never treated as an empty expected value.
    """
    try:
        raw = read_private_secret_path(
            path, max_bytes=1024, label="control token")
    except (OSError, ValueError, ImportError):
        return None
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None


class BackendHTTPServer(ThreadingHTTPServer):
    # D18-10: a stop must drain the in-flight requests with a BOUND. The handler
    # threads stay daemon so a stuck request can never keep the process alive,
    # and ``drain()`` is the explicit bounded wait the stop path uses.
    daemon_threads = True
    # standards-lifecycle-12: the explicit ``True`` was a no-op on POSIX and a
    # hazard on Windows, where SO_REUSEADDR lets a SECOND process bind a port
    # that is already listening. A conflicting listener must fail loudly.
    allow_reuse_address = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._inflight = 0
        self._inflight_cond = threading.Condition()

    def process_request_thread(self, request, client_address):
        with self._inflight_cond:
            self._inflight += 1
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._inflight_cond:
                self._inflight -= 1
                self._inflight_cond.notify_all()

    def drain(self, timeout):
        """Wait (bounded) for the in-flight handlers; return True when drained."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._inflight_cond:
            while self._inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._inflight_cond.wait(timeout=remaining)
            return True


class BackendHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- plumbing
    def log_message(self, format, *args):  # noqa: A003 - stdlib signature
        # Never log request lines/bodies: a Key or token must not leak here.
        pass

    @property
    def app(self):
        return self.server.app

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _request_allowed(self):
        if has_proxy_metadata(self.headers):
            return False
        if hostname_of(self.headers.get("Host", "")) not in LOOPBACK_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parts = urlparse(origin)
            port = parts.port
        except ValueError:
            return False
        return (parts.scheme == "http"
                and (parts.hostname or "").lower() in LOOPBACK_HOSTS
                and port == self.app.config.port)

    def _control_ok(self):
        """The admin control token, compared in constant time.

        The local role overrides this to also accept its browser session cookie;
        the cloud role keeps the token as the only credential.
        """
        expected = read_control_token(self.app.config.control_token_file)
        supplied = self.headers.get("X-Admin-Control")
        return bool(expected) and bool(supplied) \
            and secrets.compare_digest(supplied, expected)

    def _read_body(self):
        if self.headers.get("Transfer-Encoding") \
                or self.headers.get("Content-Encoding"):
            raise ValueError("unsupported body encoding")
        length_raw = self.headers.get("Content-Length")
        try:
            length = int(length_raw) if length_raw else 0
        except ValueError:
            raise ValueError("invalid content length")
        if not 0 <= length <= MAX_BODY_BYTES:
            raise ValueError("request body too large")
        if length == 0:
            return {}
        content_type = (self.headers.get("Content-Type") or "").split(";")[0]
        if content_type.strip().lower() != "application/json":
            raise ValueError("content type must be application/json")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("incomplete request body")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise ValueError("invalid JSON body")
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _reject_unknown(self, body, allowed):
        unknown = sorted(set(body) - set(allowed))
        if unknown:
            self._send_json(400, {"error": "unknown_field",
                                  "field": unknown[0]})
            return True
        return False
