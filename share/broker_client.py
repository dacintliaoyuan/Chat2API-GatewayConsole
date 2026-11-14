r"""share/broker_client.py — ordinary-privilege client for the REAL broker.

The 8001 management process (and, through ShareManager, every mutating
command) reaches the SYSTEM broker host through this client. It binds a
fixed loopback endpoint chosen by the injected transport, enforces the
protocol schema v2 (HMAC, total-deadline read, associated request id) and
returns structured BrokerResult values — an arbitrary dict or exit code
never counts as completion (D1/A4).

Transports are replaceable adapters (D0 §27):
* ``LoopbackTcpBrokerTransport`` — offline tests / explicit fallback; it is
  NOT a production adapter and never counts as OS permission evidence.
* ``UnixSocketBrokerTransport`` — the POSIX (Linux) production transport:
  an AF_UNIX SOCK_STREAM endpoint at an absolute path whose parent directory
  is 0700 and whose socket file is 0600. Peer identity is kernel evidence
  (SO_PEERCRED), never a status file or a self-reported PID.

When no broker component is installed the client reports ``not_installed``
instead of falling back to any test broker (A4: no fake success).
"""
from __future__ import annotations

import errno
import inspect
import math
import os
import socket
import stat
import struct
import time

from share.broker_protocol import (DEFAULT_DEADLINE_SECONDS, BrokerResult,
                                   ProtocolError, build_request, decode_frame,
                                   DRAIN_DEADLINE_SECONDS,
                                   EMERGENCY_DEADLINE_SECONDS,
                                   encode_frame, read_bounded_line)


class _CancelledExchange(Exception):
    """Internal: the exchange was abandoned by the deadline supervisor and
    the worker must stop WITHOUT issuing the next chunk I/O (R35-1)."""


class BrokerTransport:
    """Byte transport for one request/response exchange."""

    def open(self):            # pragma: no cover - abstract
        raise NotImplementedError

    def close(self):           # pragma: no cover - abstract
        raise NotImplementedError

    def send_all(self, data: bytes, deadline=None):   # pragma: no cover - abstract
        raise NotImplementedError

    def recv_line(self, deadline) -> bytes:    # pragma: no cover - abstract
        raise NotImplementedError


def declare_synchronous_io_cancellation(kernel):
    """Declare the thread-handle APIs on a real Win32 facade only."""
    import ctypes
    kernel.OpenThread.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenThread.restype = ctypes.c_void_p
    kernel.CancelSynchronousIo.argtypes = [ctypes.c_void_p]
    kernel.CancelSynchronousIo.restype = ctypes.c_int


def cancel_synchronous_worker_io(kernel, worker):
    """Cancel this process-owned worker's pending I/O before handle cleanup.

    The cancellation request is not completion evidence: callers still observe
    the worker's exit within their existing bounded finalization window.
    """
    open_thread = getattr(kernel, 'OpenThread', None)
    cancel_io = getattr(kernel, 'CancelSynchronousIo', None)
    thread_id = getattr(worker, 'native_id', None)
    if open_thread is None or cancel_io is None or thread_id is None:
        return False
    handle = open_thread(0x0001, False, thread_id)  # THREAD_TERMINATE
    if not handle or handle in (-1, 0xFFFFFFFFFFFFFFFF):
        return False
    try:
        # Hold the handle before rechecking liveness: an exited worker may
        # have had its numeric thread ID reused, but can never cancel it.
        if not worker.is_alive():
            return False
        return bool(cancel_io(handle))
    finally:
        kernel.CloseHandle(handle)


# Linux AF_UNIX sun_path is 108 bytes including the trailing NUL; 107 bytes
# is the longest usable absolute path. The bound is checked HERE so a bad
# deployment path fails with a clear ValueError before any syscall.
UNIX_SOCKET_MAX_PATH_BYTES = 107
UNIX_SOCKET_DIRECTORY_MODE = 0o700
UNIX_SOCKET_FILE_MODE = 0o600
_UNIX_CREDENTIAL_SIZE = struct.calcsize("3i")   # struct ucred: pid, uid, gid


def validate_unix_socket_path(path) -> str:
    """Validate the POSIX control-endpoint path contract (client + server).

    The path must be ABSOLUTE and fit the Linux AF_UNIX ``sun_path`` bound.
    Permission bits are checked at the endpoint itself: the server creates the
    parent 0700 / socket 0600, and the client refuses a wider endpoint before
    connecting.
    """
    try:
        text = os.fspath(path)
    except TypeError as exc:
        raise ValueError("unix socket path must be a filesystem path") from exc
    if isinstance(text, bytes):
        text = os.fsdecode(text)
    if not isinstance(text, str) or not text or not os.path.isabs(text):
        raise ValueError("unix socket path must be an absolute path")
    if len(os.fsencode(text)) > UNIX_SOCKET_MAX_PATH_BYTES:
        raise ValueError(
            f"unix socket path exceeds the AF_UNIX bound of "
            f"{UNIX_SOCKET_MAX_PATH_BYTES} bytes")
    return text


def _private_directory_error(path):
    return PermissionError(
        errno.EACCES,
        f"unix socket parent directory must be 0700: {path}")


def _socket_mode_error(path):
    return PermissionError(
        errno.EACCES, f"unix socket file must be 0600: {path}")


def assert_private_unix_endpoint(path):
    """Refuse an endpoint another local user could reach BEFORE connecting.

    The parent directory must be 0700 (no group/other access) and the endpoint
    must be a 0600 socket. A wider mode is an explicit access refusal, never a
    silent connection to an exposed control channel. Returns the endpoint stat
    so the caller can keep the observed identity if needed.
    """
    parent = os.path.dirname(path)
    parent_stat = os.stat(parent)
    if not stat.S_ISDIR(parent_stat.st_mode):
        raise NotADirectoryError(
            errno.ENOTDIR,
            f"unix socket parent is not a directory: {parent}")
    if stat.S_IMODE(parent_stat.st_mode) & 0o077:
        raise _private_directory_error(parent)
    endpoint_stat = os.stat(path)
    if not stat.S_ISSOCK(endpoint_stat.st_mode):
        raise OSError(errno.ENOTSOCK,
                      f"unix socket endpoint is not a socket: {path}")
    if stat.S_IMODE(endpoint_stat.st_mode) != UNIX_SOCKET_FILE_MODE:
        raise _socket_mode_error(path)
    return endpoint_stat


def unix_peer_credentials(sock):
    """SO_PEERCRED kernel evidence for a connected AF_UNIX socket.

    Returns ``{"pid", "uid", "gid"}`` or None when the platform does not
    expose SO_PEERCRED or the query fails. Callers MUST treat None as a
    refusal, never as a verified peer.
    """
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        return None
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, option, _UNIX_CREDENTIAL_SIZE)
    except OSError:
        return None
    if len(raw) != _UNIX_CREDENTIAL_SIZE:
        return None
    pid, uid, gid = struct.unpack("3i", raw)
    return {"pid": int(pid), "uid": int(uid), "gid": int(gid)}


class UnixSocketBrokerTransport(BrokerTransport):
    """POSIX (Linux) AF_UNIX SOCK_STREAM production adapter.

    The endpoint is an ABSOLUTE path. Its parent directory must be 0700 and the
    socket file 0600; a wider endpoint is refused BEFORE connecting, so a
    control channel another local user can reach is never used silently.

    The transport only moves bounded bytes: framing, HMAC, replay and the
    exchange deadlines stay in the protocol layer and BrokerClient. The
    remaining time is re-applied before EVERY blocking send/recv (the same
    R29-6 semantics as the loopback adapter), so a slow peer cannot park the
    exchange beyond its total deadline. Failure classification follows the
    existing client rules: ENOENT/ECONNREFUSED -> ``not_installed``,
    EACCES/EPERM -> ``access_denied``, connect timeout ->
    ``deadline_exceeded``.

    Peer identity is KERNEL evidence: ``peer_credentials()`` and
    ``peer_server_pid()`` read SO_PEERCRED from the connected socket. There is
    no status file and no caller-supplied PID path - the same contract the
    named-pipe adapter exposes to the instance identity verifier (R41-2).
    """

    def __init__(self, path):
        if os.name != "posix":
            raise OSError("unix domain sockets require a POSIX platform")
        if not hasattr(socket, "AF_UNIX"):
            raise OSError("AF_UNIX sockets are unavailable on this platform")
        self.path = validate_unix_socket_path(path)
        self._sock = None

    def open(self, deadline=None):
        if self._sock is not None:
            raise OSError("unix socket transport is already open")
        assert_private_unix_endpoint(self.path)
        timeout = DEFAULT_DEADLINE_SECONDS
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("exchange deadline exceeded before connect")
            timeout = min(timeout, remaining)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(self.path)
        except socket.timeout as exc:
            sock.close()
            raise TimeoutError(
                "unix socket connect exceeded the exchange deadline") from exc
        except BaseException:
            sock.close()
            raise
        sock.settimeout(None)
        self._sock = sock
        return self

    def close(self):
        try:
            if self._sock is not None:
                self._sock.close()
        finally:
            self._sock = None

    def send_all(self, data: bytes, deadline=None):
        if deadline is None:
            raise ValueError("send_all requires an explicit exchange deadline")
        sock = self._sock
        if sock is None:
            raise OSError("unix socket transport is not open")
        total = 0
        while total < len(data):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("exchange deadline exceeded during send")
            sock.settimeout(remaining)
            try:
                sent = sock.send(data[total:])
            except socket.timeout as exc:
                raise TimeoutError(
                    "exchange deadline exceeded during send") from exc
            if sent <= 0:
                raise OSError("unix socket write failed")
            total += sent
        return total

    def recv_line(self, deadline) -> bytes:
        sock = self._sock
        if sock is None:
            raise OSError("unix socket transport is not open")
        reader = sock.makefile("rb")
        try:
            # R29-6: re-apply the remaining time before EVERY blocking read
            # so the total deadline also bounds reads parked inside one recv.
            return read_bounded_line(
                reader, deadline,
                on_before_recv=lambda remaining: sock.settimeout(
                    max(remaining, 0.01)))
        finally:
            reader.close()

    def peer_credentials(self):
        if self._sock is None:
            return None
        return unix_peer_credentials(self._sock)

    def peer_server_pid(self):
        """The named-pipe adapter's interface: the instance-channel client
        verifies the peer's real process identity before trusting the
        connection. SO_PEERCRED is kernel evidence; None (unsupported or
        failed) is a refusal, never a verified peer."""
        credentials = self.peer_credentials()
        if credentials is None or credentials["pid"] <= 0:
            return None
        return credentials["pid"]

    def prepare_peer(self, verifier, *, deadline, **expectations):
        """Bounded connect + identity query before any request is sent.

        Mirrors the named-pipe adapter's seam: the peer identity comes from
        the kernel, a failed query returns None (refusal), and the verifier
        decides. SO_PEERCRED is a local option, so the query itself cannot
        block; the deadline still gates connect and the hand-off.
        """
        if time.monotonic() >= deadline:
            raise TimeoutError("instance preparation deadline exceeded")
        self.open(deadline=deadline)
        if time.monotonic() >= deadline:
            raise TimeoutError("instance preparation deadline exceeded")
        pid = self.peer_server_pid()
        evidence = (None if pid is None
                    else verifier.verify(pid, **expectations))
        if time.monotonic() >= deadline:
            raise TimeoutError("instance preparation deadline exceeded")
        return pid, evidence


class LoopbackTcpBrokerTransport(BrokerTransport):
    """Loopback TCP adapter for OFFLINE tests and the fallback control
    channel. The production adapter is the named pipe (L2-verified first).
    Binding is restricted to the literal loopback host; hostnames and other
    hosts are rejected before any credential is used."""

    def __init__(self, port, host="127.0.0.1"):
        if host != "127.0.0.1":
            raise ValueError("broker transport must be loopback-only")
        self.host = host
        self.port = int(port)
        self._sock = None

    def open(self):
        self._sock = socket.create_connection(
            (self.host, self.port), timeout=DEFAULT_DEADLINE_SECONDS)
        return self

    def close(self):
        try:
            if self._sock is not None:
                self._sock.close()
        finally:
            self._sock = None

    def send_all(self, data: bytes, deadline=None):
        self._sock.sendall(data)

    def recv_line(self, deadline) -> bytes:
        reader = self._sock.makefile("rb")
        try:
            # R29-6: re-apply the remaining time before EVERY blocking read
            # so the total deadline also bounds reads parked inside one recv.
            return read_bounded_line(
                reader, deadline,
                on_before_recv=lambda remaining: self._sock.settimeout(
                    max(remaining, 0.01)))
        finally:
            reader.close()


class BrokerClient:
    """Ordinary-privilege client for the broker management pipe.

    Every call is a signed schema-v2 request whose response must carry the
    SAME request id; anything else is rejected. When no broker component is
    installed, ``call`` reports ``not_installed`` — there is no fallback to
    any test broker or generic wrapper (A4).
    """

    def __init__(self, transport_factory, secret,
                 timeout_seconds=DEFAULT_DEADLINE_SECONDS):
        self._factory = transport_factory
        self._secret = secret
        self._timeout = timeout_seconds

    def call(self, action: str, fields=None, instance_id=None,
             transport=None, timeout_s=None, deadline=None) -> BrokerResult:
        """Signed schema-v2 request. ``instance_id`` is REQUIRED on the
        instance channel (the signed frame must carry it - a pipe name is
        transport addressing, not authentication evidence) and stays None
        for management-channel requests.

        ``timeout_s`` overrides the exchange deadline for ONE call; it is
        BOUNDED to 1..180 seconds (the frozen protocol bound) and an
        out-of-range value is refused before any I/O - never silently
        clamped. ``None`` keeps the client's configured default (the old
        behavior, byte for byte).

        Internal callers may supply an absolute monotonic ``deadline`` to
        include preparation and retries. It only shortens this call's bound;
        it never starts a new timeout after peer verification.

        R44-1: one exchange owns EXACTLY ONE connection. ``transport`` may
        carry a caller-opened (and identity-verified) transport - it is
        consumed as-is on the SAME handle for send/recv and the CALLER
        closes it; passing one never triggers a second open. Without one,
        the factory opens (and this call closes) its own connection.
        """
        if timeout_s is None:
            timeout = self._timeout
        else:
            if isinstance(timeout_s, bool) \
                    or not isinstance(timeout_s, (int, float)):
                raise ValueError("timeout_s must be a number in 1..180")
            timeout = float(timeout_s)
            if not 1.0 <= timeout <= 180.0:
                raise ValueError("timeout_s must be within the bounded "
                                 "1..180 second range")
        request = build_request(self._secret, action, fields,
                                instance_id=instance_id)
        exchange_deadline = time.monotonic() + timeout
        if deadline is not None:
            if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) \
                    or not math.isfinite(deadline):
                raise ValueError("deadline must be a finite monotonic timestamp")
            exchange_deadline = min(exchange_deadline, deadline)
        deadline = exchange_deadline
        if time.monotonic() >= deadline:
            return BrokerResult.err("deadline_exceeded",
                                    "exchange budget exhausted before I/O")
        owned = transport is None
        if not owned and getattr(transport, "_abandoned", False):
            return BrokerResult.err("not_installed",
                                    "transport abandoned after deadline")
        try:
            if owned:
                transport = self._factory()
                # The deadline rides an OPTIONAL keyword (the seam surface
                # stays fixed): the production transport truncates its open
                # retry sleeps to the remaining budget, while a seam that does
                # not accept it keeps its own bounded open window.
                try:
                    open_parameters = inspect.signature(
                        transport.open).parameters
                except (TypeError, ValueError):
                    open_parameters = {}
                if "deadline" in open_parameters:
                    transport.open(deadline=deadline)
                else:
                    transport.open()
                # R48-1: only this exchange's own connection may reconnect on
                # transient write errors; reopening keeps R44-1 (one exchange
                # finishes on one successful connection) and never touches a
                # caller-verified transport.
                transport._allow_reconnect = True
        except TimeoutError:
            return BrokerResult.err("deadline_exceeded",
                                    "connection exceeded the exchange deadline")
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            error_number = getattr(exc, "errno", None)
            # Windows keeps its winerror==5 mapping byte for byte; on POSIX a
            # denied connect (0700 directory / 0600 socket) is the SAME explicit
            # access refusal, never folded into "not_installed".
            access_refused = winerror == 5 or (
                winerror is None
                and error_number in (errno.EACCES, errno.EPERM))
            code = "access_denied" if access_refused else "not_installed"
            suffix = f" (winerror={winerror})" if type(winerror) is int else ""
            return BrokerResult.err(code,
                                    f"broker component unavailable: "
                                    f"{type(exc).__name__}{suffix}")
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError("exchange deadline exceeded before I/O")
            transport.send_all(encode_frame(request), deadline)
            if time.monotonic() >= deadline:
                raise TimeoutError("exchange deadline exceeded after send")
            line = transport.recv_line(deadline)
            if time.monotonic() >= deadline:
                raise TimeoutError("exchange deadline exceeded after receive")
        except TimeoutError:
            return BrokerResult.err("deadline_exceeded",
                                    "no broker response within the deadline")
        except ProtocolError as exc:
            return BrokerResult.err(exc.code if exc.code in
                                    ("overlimit",) else "internal_error",
                                    str(exc))
        except OSError as exc:
            return BrokerResult.err("not_installed",
                                    f"broker connection failed: "
                                    f"{type(exc).__name__}")
        finally:
            if owned:
                transport.close()
        try:
            response = decode_frame(line)
        except ProtocolError as exc:
            return BrokerResult.err("internal_error", str(exc))
        if response.get("request_id") != request["request_id"]:
            # R29-3: the associated id matches this exchange on the wire —
            # it is TRANSPORT correlation, NOT server authentication. Server
            # identity is established by the channel (DACL/OS identity, D4)
            # and the ACK's instance binding is checked by the consumer.
            return BrokerResult.err("instance_mismatch",
                                    "response request id does not match")
        return BrokerResult.from_dict(response)

    def publish(self, projection: dict, terminate_keys=None) -> BrokerResult:
        generation = projection.get("config_generation")
        fingerprint = _fingerprint(projection)
        fields = {"generation": generation,
                  "fingerprint": fingerprint,
                  "projection": projection}
        if terminate_keys:
            # R8-A7 revoke-and-terminate: validated by the protocol schema
            # (optional list of 1..16 key ids) on the broker side.
            fields["terminate_keys"] = list(terminate_keys)[:16]
        return self.call("publish", fields)

    def status(self) -> BrokerResult:
        # Read-only: a queued 25s security check plus the 6s task query and
        # reply margin must fit one bounded exchange. Mutations keep 10s.
        return self.call("status", timeout_s=35)

    def start(self, explicit: bool = False) -> BrokerResult:
        """Start the host.

        ``explicit=True`` is the OPERATOR's own start: it is the only start
        allowed to supersede a persisted stop intent. The field is omitted for
        a watchdog/systemd start so old brokers keep the historical wire
        shape (and the persisted intent still refuses it).
        """
        if explicit:
            result = self.call("start", {"explicit": True})
            if result.ok or result.error_code != "bad_request":
                return result
            # An OLD broker rejects the unknown `explicit` field before running
            # anything; retry the historical wire shape. The retry can never
            # double-start a new broker: it only accepts `explicit`, and an old
            # broker's persisted stop intent still refuses the plain start.
            return self.call("start")
        return self.call("start")

    def drain(self) -> BrokerResult:
        """Graceful close: the exchange deadline covers the broker's WHOLE
        bounded drain (the in-flight window + the reserved final stop +
        closure verification), so a client that gives up early can never
        abandon a drain the broker is still performing."""
        return self.call("drain", timeout_s=DRAIN_DEADLINE_SECONDS)

    def emergency_stop(self) -> BrokerResult:
        """Emergency close: the exchange deadline covers the reserved stop +
        closure verification (the broker runs no in-flight wait here)."""
        return self.call("emergency-stop",
                         timeout_s=EMERGENCY_DEADLINE_SECONDS)

    def host_verify(self, check: str) -> BrokerResult:
        """Restricted host-evidence verb: the broker-side WindowsHostOps
        answers with its SYSTEM-privilege verdict (True/False/None). The
        check enum is validated by the protocol whitelist; the task name,
        image and port never cross the wire."""
        # Up to three bounded 10s OS evidence reads plus reply margin.
        # This read budget does not change mutation/start deadlines.
        return self.call("host-verify", {"check": check}, timeout_s=35)

    def host_stop(self) -> BrokerResult:
        # The broker's closure check can make up to three bounded 10s evidence
        # reads; the default 10s exchange would report deadline_exceeded before
        # that check can answer.
        return self.call("host-stop", timeout_s=45)

    def repair_limits(self) -> BrokerResult:
        # No caller-selected path/command/port. The broker pins the protected
        # maintenance helper and owns its bounded execution.
        return self.call("host-repair-limits", timeout_s=120)

    def maintenance_begin(self, component: str, lease_s: int) -> BrokerResult:
        """Begin a bounded maintenance lease (pause 8100 acceptance): the
        component enum and the bounded lease_s are validated by the broker-side
        whitelist; nothing about the object crosses the wire."""
        return self.call("maintenance-begin",
                         {"component": component, "lease_s": lease_s})

    def maintenance_end(self, component: str, lease_id: str) -> BrokerResult:
        """End the CURRENT maintenance lease (resume acceptance). A lease
        that expired, a mismatched lease_id or a replaced instance/round is
        refused by the broker - a late result never re-opens anything."""
        return self.call("maintenance-end",
                         {"component": component, "lease_id": lease_id})

    def maintenance_status(self) -> BrokerResult:
        """Read-only maintenance view (lease / instance / last_outcome); the
        broker answers with honest nulls when there is no lease or no trusted
        instance."""
        return self.call("maintenance-status")

    def gateway_restart(self, component: str, budget_s: int) -> BrokerResult:
        """8100 single-layer restart through the restricted broker host path
        (never stop-then-start through the console). The exchange is BLOCKING and bounded:
        budget_s + 15 seconds, capped at the frozen 180 second protocol bound
        (budget_s itself stays wire-bounded by the broker whitelist)."""
        budget = int(budget_s)
        return self.call("gateway-restart",
                         {"component": component, "budget_s": budget},
                         timeout_s=min(budget + 15, 180))

    def host_evidence(self, check: str) -> BrokerResult:
        """Restricted READ-ONLY host evidence verb (limits/faults/watchdog): the
        broker-side WindowsHostOps answers with its SYSTEM-privilege verdict
        (True/False/None or a bounded fault-tail dict); the check enum is
        validated by the protocol whitelist and the object stays broker-side
        deployment configuration."""
        # The one pipe may already be completing another evidence read.
        return self.call("host-evidence", {"check": check}, timeout_s=35)

    def ts_service_control(self, action: str) -> BrokerResult:
        """Restricted TS service control verb ("start"/"stop"): the action enum
        is validated by the broker-side protocol whitelist and the service name
        stays the protocol literal (``TS_SERVICE_NAME``), so neither a service
        name nor a command ever crosses the wire."""
        return self.call("ts-service-control", {"action": action})


def _fingerprint(projection: dict) -> str:
    import hashlib
    import json
    return hashlib.sha256(
        json.dumps(projection, sort_keys=True).encode("utf-8")).hexdigest()
