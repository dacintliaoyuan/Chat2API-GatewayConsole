r"""share_broker_server.py — SYSTEM broker host entry (D4 prerequisite).

The broker host runs as the SYSTEM scheduled task created by
install-share-broker.ps1 (launched through the generated wrapper). It
listens on the FIXED management pipe ``\\.\pipe\um-share-broker`` and
drives the product coordinator (``share_broker_host.BrokerHostCoordinator``):
{status, publish, start, drain, emergency-stop} plus the restricted
``ts-service-control`` whitelist extension (a dispatch decorator reusing the
same validation pipeline; the share instance can never call these
(A2/A4: two channels, two secrets, two whitelists).

Design decisions fixed here:

* BYTE-mode pipe + the reviewed byte-stream line framing: bounded chunks,
  the 1 MiB cap is checked BEFORE a complete frame is accepted (a
  newline-terminated oversized frame can never reach dispatch), "\n"
  terminator. Message-mode framing stays an L2 option only after H5.
* DACL from an SDDL parameter. ``PLANNED_SDDL`` is a TEMPLATE with SID
  placeholders that intentionally FAILS conversion, so an uncalibrated
  deployment refuses to serve instead of starting with a default ACL
  (H2/H5 calibrate it during the authorized experiment).
* ``FILE_FLAG_FIRST_PIPE_INSTANCE`` is ACTUALLY passed in the openMode and
  any creation failure (NULL or INVALID_HANDLE_VALUE, both forms) is a
  loud refusal — no silent reuse (H4).
* ONE daemon worker per connection cycle with ONE reserved process-wide
  slot (R37 contract): the cancel event and the pipe handle are
  cycle-owned locals, so a late worker can never operate the NEXT cycle's
  connection; cancellation is re-checked BEFORE dispatch (a late read can
  never reach management side effects); a blocked kernel call is released
  only by the outer abandonment path (cancel -> cancel synchronous I/O -> disconnect -> CloseHandle,
  D4); there is NO nested worker and NO nested observation window.
* The single-thread accept loop bottom-catches per-cycle exceptions
  (§27.5): a poisoned cycle is disconnected and serving CONTINUES.

The share-side instance channel wire server is ``InstancePipeServer`` in
``share_instance_pipe.py`` (same bounded base). Deployment wiring:
``build_server``/``main`` below; the instance identity verifier is wired
through ``make_instance_call`` (R41-2), not trusted from a status file.

POSIX (Linux): ``UnixSocketBrokerServer`` is the AF_UNIX SOCK_STREAM
counterpart of ``BoundedPipeServer`` for the instance channel. It keeps the
same bounded cycle contract and framing, creates the endpoint at an absolute
path (parent directory 0700, socket file 0600) and exposes the peer identity
as kernel SO_PEERCRED evidence (pid/uid/gid). The Windows named-pipe
behavior is unchanged.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import socket
import stat
import sys
import threading
import time
from pathlib import Path

from share_broker_client import (UNIX_SOCKET_DIRECTORY_MODE,
                                 UNIX_SOCKET_FILE_MODE,
                                 cancel_synchronous_worker_io,
                                 declare_synchronous_io_cancellation,
                                 unix_peer_credentials,
                                 validate_unix_socket_path)
from share_broker_host import (BrokerDiag, BrokerHostCoordinator,
                               InstanceRegistry, MaintenanceLeaseStore,
                               ReplayRegistry)
from share_broker_protocol import (DEFAULT_DEADLINE_SECONDS, MAX_MESSAGE,
                                   DRAIN_WINDOW_SECONDS,
                                   STOP_BUDGET_SECONDS,
                                   HOST_EVIDENCE_ACTION,
                                   HOST_STOP_ACTION, HOST_VERIFY_ACTION,
                                   HOST_REPAIR_LIMITS_ACTION,
                                   TS_SERVICE_CONTROL_ACTION,
                                   ProtocolError, BrokerResult,
                                   decode_frame,
                                   validate_host_evidence_fields,
                                   validate_host_stop_fields,
                                   validate_host_verify_fields,
                                   validate_management_request,
                                   validate_ts_service_control_fields)

# Planned DACL for the management pipe (D0 §27.2): SYSTEM full,
# Administrators full, the operator account read+write, the restricted
# um_share identity and Anonymous explicitly denied. SDDL cannot spell
# domain\\user names, so the account entries are PLACEHOLDER SIDs that must
# be replaced with the real S-1-5-21-... values during the authorized L2
# experiment (H5 calibrates both the SID values and the deny/allow
# evaluation order). The placeholder form intentionally FAILS the SDDL
# conversion, so an uncalibrated deployment refuses to serve instead of
# starting with a default ACL.
PLANNED_SDDL = ("D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGW;;;%%SID_DACINT%%)"
                "(D;;GA;;;%%SID_UM_SHARE%%)(D;;GA;;;AN)")


def _declared_kernel():
    """Real Win32 declarations (kernel32 + advapi32 SDDL conversion)."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                          wintypes.DWORD, wintypes.DWORD,
                                          wintypes.DWORD, wintypes.DWORD,
                                          wintypes.DWORD, wintypes.LPVOID]
    kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
    kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    kernel32.ConnectNamedPipe.restype = wintypes.BOOL
    kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
    kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID,
                                  wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD),
                                  wintypes.LPVOID]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID,
                                   wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD),
                                   wintypes.LPVOID]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel32.FlushFileBuffers.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetLastError.restype = wintypes.DWORD
    declare_synchronous_io_cancellation(kernel32)
    kernel32.GetNamedPipeClientProcessId.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetNamedPipeClientProcessId.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = \
        wintypes.BOOL
    return kernel32, advapi32


def _invalid_handle(handle):
    """Strict failure forms: NULL (None) and INVALID_HANDLE_VALUE (-1)."""
    if handle is None:
        return True
    try:
        return int(handle) == -1 or int(handle) == (1 << 64) - 1
    except (TypeError, ValueError):
        return False


class _SecurityAttrs:
    """SECURITY_ATTRIBUTES built from an SDDL string (H2/H5: the SDDL
    semantics are L2-verified). Conversion failure raises loudly - the
    server refuses to create a pipe without the intended DACL."""

    _SDDL_REVISION_1 = 1

    def __init__(self, sddl, kernel_pair=None):
        import ctypes
        from ctypes import wintypes
        kernel32, advapi32 = kernel_pair or _declared_kernel()
        descriptor = ctypes.c_void_p(0)
        length = wintypes.DWORD(0)
        ok = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, self._SDDL_REVISION_1, ctypes.pointer(descriptor),
            ctypes.pointer(length))
        if not ok or not descriptor.value:
            raise OSError(f"SDDL conversion failed: "
                          f"{kernel32.GetLastError()}")
        self._descriptor = descriptor      # keep the OS allocation alive
        self._buffer = ctypes.create_string_buffer(
            ctypes.string_at(descriptor.value, length.value))

        class _SA(ctypes.Structure):
            _fields_ = [("nLength", ctypes.c_ulong),
                        ("lpSecurityDescriptor", ctypes.c_void_p),
                        ("bInheritHandle", wintypes.BOOL)]

        self.sa = _SA(ctypes.sizeof(_SA),
                      ctypes.cast(self._buffer, ctypes.c_void_p).value,
                      False)


class _CancelledConnection(Exception):
    """Internal: the connection cycle was abandoned by the deadline
    supervisor; the worker stops without further side effects."""


def _valid_epoch(value):
    """A usable bounded-epoch integer (the maintenance record's
    ``expires_at`` is consumed by the watchdog)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _wire_maintenance_sink(coordinator, sink):
    """Attach ``sink`` to the REAL coordinator behind any dispatch
    decorators (the decorators forward attribute READS only). A coordinator
    that already carries an injected sink is left untouched."""
    target = coordinator
    seen = set()
    while target is not None and id(target) not in seen:
        seen.add(id(target))
        if getattr(target, "maintenance", None) is not None:
            return
        inner = getattr(target, "_inner", None)
        if inner is None:
            target.maintenance = sink
            return
        target = inner


class BoundedPipeServer:
    """Bounded named-pipe server base (R37 contract, one worker per cycle).

    One connection cycle = create instance -> bounded connect -> bounded
    read -> dispatch -> bounded write -> flush -> disconnect -> close. The
    cancel event and the pipe handle are cycle-owned locals: the worker can
    never operate the next cycle's connection, and the blocked kernel call
    is released only by the outer abandonment path (D4)."""

    _PIPE_ACCESS_DUPLEX = 0x00000003
    _PIPE_TYPE_BYTE = 0x00000000        # byte mode + byte readmode + wait
    _PIPE_WAIT = 0x00000000
    _PIPE_UNLIMITED_INSTANCES = 255
    _FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
    _ERROR_PIPE_CONNECTED = 535
    # Bounded worker admission (same contract as the client, R37).
    _MAX_UNCONFIRMED_WORKERS = 8
    _WORKER_EXIT_OBSERVE_SECONDS = 2.0
    _reserved_worker_slots = 0
    _unconfirmed_abandoned = 0
    _accounting_lock = threading.Lock()
    # Status telemetry is rewritten per cycle while consumers poll it; on
    # Windows a consumer holding the file without FILE_SHARE_DELETE makes
    # the atomic replace fail with a sharing violation. The bounded retry
    # covers the transient reader window; an exhausted writer records
    # last_error and NEVER kills the serving loop (a stale status is not a
    # cycle failure).
    _STATUS_REPLACE_RETRIES = 50
    _STATUS_REPLACE_BACKOFF = 0.01

    def __init__(self, pipe_name, sddl=None, kernel=None,
                 deadline_seconds=DEFAULT_DEADLINE_SECONDS, state_dir=None,
                 clock=time.monotonic, dispatch_deadline_seconds=None):
        self.pipe_name = pipe_name
        self.sddl = sddl
        self._kernel_pair = kernel          # tests inject a fake facade
        self._deadline = float(deadline_seconds)
        # R44-4 (contract §6.4): the DISPATCH budget is a separate,
        # OPTIONAL bound. None keeps dispatch inside the connected read
        # window (or the caller's explicit cycle cap); a value enables a
        # separate dispatch join so a slow (bounded) handler - e.g. a
        # gateway-restart - is not abandoned at the 10 s pipe deadline.
        self._dispatch_deadline = (None if dispatch_deadline_seconds is None
                                   else float(dispatch_deadline_seconds))
        self._state_dir = Path(state_dir) if state_dir else None
        self._clock = clock
        self._handle = None
        self._cycles = 0
        self._last_error = None
        self._stop = threading.Event()

    # ------------------------------------------------------------ kernel
    def _kernel(self):
        """The Win32 facade: injectable for offline tests; production
        resolves kernel32/advapi32 with explicit signatures."""
        if self._kernel_pair is not None:
            return self._kernel_pair
        return _declared_kernel()

    def _client_pid(self, handle):
        """Client PID evidence for the connection record (no secrets)."""
        import ctypes
        kernel32, _ = self._kernel()
        pid = ctypes.c_ulong(0)
        if kernel32.GetNamedPipeClientProcessId(handle, ctypes.pointer(pid)):
            return int(pid.value)
        return None

    # ------------------------------------------- bounded worker admission
    def _reserve_worker_slot(self):
        cls = type(self)
        with cls._accounting_lock:
            if (cls._unconfirmed_abandoned + cls._reserved_worker_slots
                    >= cls._MAX_UNCONFIRMED_WORKERS):
                raise OSError(
                    "broker server admission refused: bounded cap of "
                    "in-flight/unconfirmed workers reached")
            cls._reserved_worker_slots += 1

    @classmethod
    def _release_worker_slot(cls):
        with cls._accounting_lock:
            cls._reserved_worker_slots -= 1

    # ------------------------------------------------------------ serving
    def stop(self):
        self._stop.set()

    def stopped(self):
        return self._stop.is_set()

    def serve_forever(self):
        """Single-thread accept loop (§27.5): a poisoned cycle is
        disconnected and serving CONTINUES; stop() ends the loop at the
        cycle boundary. Repeated unconfirmed workers stay bounded by the
        admission cap; the loop backs off briefly on admission refusal."""
        while not self._stop.is_set():
            try:
                record = self.serve_once()
                self._last_error = record.get("error")
            except BaseException as exc:  # noqa: BLE001
                # Bottom catch: never let one poisoned cycle kill serving.
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._release_instance()
                self._write_status()
                time.sleep(0.05)

    def serve_once(self, deadline=None):
        """Run ONE full connection cycle and return an outcome record.

        An explicit deadline is a hard end-to-end cap. Otherwise idle accept
        and connected reading each get one bounded window; dispatch keeps its
        optional separate bound. Finalization is also bounded. The record carries
        the phase reached, the client PID evidence (if available) and the
        outcome - never a fabricated success.
        """
        explicit_deadline = deadline is not None
        if deadline is None:
            deadline = self._clock() + self._deadline
        connected = threading.Event()
        connection_at = {}
        record = {"phase": "create", "ok": False}
        cancel = threading.Event()      # cycle-owned cancel owner (R41-4)
        # Dispatch-deadline observability (contract §6.4): the worker writes
        # the dispatch start time BEFORE setting the event; the main thread
        # reads it only after the event is set (happens-before via the
        # event). The closure ``deadline`` is NEVER rebound.
        dispatch_started = threading.Event()
        dispatch_at = {"t": None}
        self._reserve_worker_slot()
        try:
            outcome = {}
            done = threading.Event()

            def _exchange():
                # R41-4: handle ownership is a cycle local - a worker that
                # wakes after this cycle was released can never operate the
                # NEXT cycle's connection.
                handle = self._create_instance(sddl=self.sddl)
                record["phase"] = "connect"
                if cancel.is_set() or self._clock() >= deadline:
                    raise _CancelledConnection()
                self._bounded_connect(handle, cancel, deadline)
                record["phase"] = "exchange"
                if cancel.is_set() or self._clock() >= deadline:
                    raise _CancelledConnection()
                connection_at["t"] = self._clock()
                read_deadline = (deadline if explicit_deadline else
                                 connection_at["t"] + self._deadline)
                connected.set()
                record["client_pid"] = self._client_pid(handle)
                try:
                    request = self._bounded_read(handle, cancel, read_deadline)
                except ProtocolError as exc:
                    if exc.code == "overlimit":
                        # §27.5: an overlimit frame is answered explicitly
                        # (no request_id association is possible) before
                        # the connection is released; the old projection
                        # is untouched by definition.
                        self._bounded_write(
                            handle,
                            self._frame(None, "overlimit",
                                        "request exceeds bound"),
                            cancel, read_deadline)
                    raise
                # R41-4: cancellation/deadline re-checked BEFORE dispatch -
                # a late read can never reach management side effects.
                if cancel.is_set() or self._clock() >= read_deadline:
                    raise _CancelledConnection()
                dispatch_at["t"] = self._clock()
                dispatch_started.set()
                # Close the cancellation/dispatch handoff: when the owner saw
                # no dispatch yet, its native cancellation cannot race into
                # a newly started handler's unrelated synchronous file I/O.
                if cancel.is_set() or self._clock() >= read_deadline:
                    raise _CancelledConnection()
                frame = self._dispatch(request, record)
                record["phase"] = "reply"
                # Without a separate dispatch budget, the connected read
                # window (or explicit cycle cap) remains the bound. With a
                # dispatch budget it is the REAL dispatch start plus that
                # budget, so a bounded slow dispatch is not cancelled for
                # having exceeded the shorter read deadline.
                if self._dispatch_deadline is None:
                    post_dispatch_bound = read_deadline
                else:
                    post_dispatch_bound = (dispatch_at["t"]
                                           + self._dispatch_deadline)
                if cancel.is_set() \
                        or self._clock() >= post_dispatch_bound:
                    raise _CancelledConnection()
                # The write deadline is re-derived by THIS worker after the
                # dispatch returned (the short per-write budget, unchanged);
                # the main thread's join budget covers read + dispatch.
                self._bounded_write(handle, frame, cancel,
                                    self._clock() + self._deadline)
                return True

            def _worker():
                try:
                    outcome["value"] = _exchange()
                except _CancelledConnection:
                    outcome["cancelled"] = True
                except BaseException as exc:  # noqa: BLE001
                    outcome["error"] = exc
                finally:
                    done.set()

            worker = threading.Thread(target=_worker, daemon=True)
            try:
                worker.start()
            except BaseException:
                self._release_worker_slot()
                raise
            worker.join(max(deadline - self._clock(), 0))
            if (not explicit_deadline and worker.is_alive()
                    and connected.is_set()):
                # Idle acceptance and request reading are separate bounded
                # phases. A real connection near the idle-window boundary
                # must get its own read budget; an explicit caller deadline
                # remains a hard end-to-end cap with no renewal.
                worker.join(max(connection_at["t"] + self._deadline
                                - self._clock(), 0))
            if self._dispatch_deadline is not None \
                    and worker.is_alive() and dispatch_started.is_set():
                # Dispatch budget starts at the REAL dispatch start; the
                # event set/read establishes the happens-before edge.
                worker.join(max(dispatch_at["t"] + self._dispatch_deadline
                                - self._clock(), 0))
            if worker.is_alive():
                # Expiry: cancel FIRST (the SAME cycle-owned event), then
                # release the connection (the blocked call is the OS
                # release mechanism itself, D4).
                cancel.set()
                kernel32, _ = self._kernel()
                if not dispatch_started.is_set() or record.get("phase") == "reply":
                    cancel_synchronous_worker_io(kernel32, worker)
                try:
                    kernel32.DisconnectNamedPipe(self._handle)
                except BaseException:  # noqa: BLE001
                    pass
                self._release_instance()
                if done.wait(self._WORKER_EXIT_OBSERVE_SECONDS):
                    self._release_worker_slot()
                else:
                    cls = type(self)
                    with cls._accounting_lock:
                        cls._reserved_worker_slots -= 1
                        cls._unconfirmed_abandoned += 1
                record["error"] = "connection deadline exceeded"
                record["abandoned"] = True
                return record
            self._release_worker_slot()
            if outcome.get("cancelled"):
                record["error"] = "connection deadline exceeded"
                self._release_instance()
                return record
            error = outcome.get("error")
            if error is not None:
                record["error"] = f"{type(error).__name__}: {error}"
                if isinstance(error, ProtocolError):
                    record["error_code"] = error.code
                self._release_instance()
                return record
            record["ok"] = True
            self._release_instance()   # one cycle = one connection: release
            return record
        finally:
            self._cycles += 1
            self._write_status()

    # ------------------------------------------------------------ pieces
    def _create_instance(self, sddl=None):
        """Create the pipe INSTANCE with the first-instance flag ACTUALLY
        in the openMode; any failure (NULL or INVALID_HANDLE_VALUE) is a
        loud refusal - never a silent reuse of an existing name (H4)."""
        import ctypes
        kernel32, advapi32 = self._kernel()
        security = None
        if sddl:
            # H2/H5: SDDL semantics are L2-verified; a conversion failure
            # refuses loudly instead of serving with a default ACL.
            security = _SecurityAttrs(sddl, (kernel32, advapi32))
        attrs = ctypes.pointer(security.sa) if security else None
        open_mode = (self._PIPE_ACCESS_DUPLEX
                     | self._FILE_FLAG_FIRST_PIPE_INSTANCE)
        handle = kernel32.CreateNamedPipeW(
            self.pipe_name, open_mode,
            self._PIPE_TYPE_BYTE | self._PIPE_WAIT,
            self._PIPE_UNLIMITED_INSTANCES, MAX_MESSAGE + 1,
            MAX_MESSAGE + 1, 0, attrs)
        if _invalid_handle(handle):
            raise OSError(f"CreateNamedPipeW failed: "
                          f"{kernel32.GetLastError()}")
        self._handle = handle
        return handle

    def _bounded_connect(self, handle, cancel, deadline):
        """ConnectNamedPipe ON the connection worker. A blocked connect is
        released only by the outer abandonment path (cancel + disconnect +
        CancelSynchronousIo + CloseHandle - the OS release mechanism); there is NO nested
        worker and NO nested observation window, so the accounting has
        exactly one owner."""
        kernel32, _ = self._kernel()
        if kernel32.ConnectNamedPipe(handle, None) == 0:
            # ERROR_PIPE_CONNECTED (535): the client connected before the
            # blocking call - the connection exists, treat it as success.
            if kernel32.GetLastError() != self._ERROR_PIPE_CONNECTED:
                raise OSError("management pipe connect failed")

    def _bounded_read(self, handle, cancel, deadline):
        """Byte-stream bounded line read ON the connection worker; cancel
        is checked before EVERY chunk and the accumulated length is checked
        BEFORE a complete frame is accepted (a newline-terminated oversized
        frame can never reach dispatch - the 1 MiB contract holds)."""
        import ctypes
        kernel32, _ = self._kernel()
        chunks = bytearray()
        while True:
            if cancel.is_set() or self._clock() >= deadline:
                raise _CancelledConnection()
            buf = ctypes.create_string_buffer(4096)
            read = ctypes.c_ulong(0)
            ok = kernel32.ReadFile(handle, buf, 4096,
                                   ctypes.pointer(read), None)
            if not ok:
                raise OSError("management pipe read failed")
            if not read.value:
                raise ProtocolError("bad_request", "client closed")
            chunks.extend(buf.raw[:read.value])
            if len(chunks) > MAX_MESSAGE:
                raise ProtocolError("overlimit", "request exceeds bound")
            if chunks.endswith(b"\n"):
                return bytes(chunks)

    def _bounded_write(self, handle, frame, cancel, deadline):
        """Bounded write ON the connection worker; cancel is checked before
        every chunk and FlushFileBuffers closes the reply."""
        import ctypes
        kernel32, _ = self._kernel()
        data = frame
        written_total = 0
        while written_total < len(data):
            if cancel.is_set() or self._clock() >= deadline:
                raise _CancelledConnection()
            written = ctypes.c_ulong(0)
            ok = kernel32.WriteFile(handle, data[written_total:],
                                    len(data) - written_total,
                                    ctypes.pointer(written), None)
            if not ok or not written.value:
                raise OSError("management pipe write failed")
            written_total += written.value
        kernel32.FlushFileBuffers(handle)

    def _release_instance(self):
        try:
            if self._handle is not None:
                try:
                    kernel32, _ = self._kernel()
                    kernel32.DisconnectNamedPipe(self._handle)
                except BaseException:  # noqa: BLE001
                    pass
                kernel32, _ = self._kernel()
                kernel32.CloseHandle(self._handle)
        finally:
            self._handle = None

    def _dispatch(self, body, record):
        """Channel-specific dispatch; returns the encoded reply frame."""
        raise NotImplementedError

    @staticmethod
    def _decode_body(body):
        """Wire bytes -> decoded object (ProtocolError on invalid shape).
        The bounded read returns RAW line bytes; the frame must be decoded
        ONCE before any handler sees it (R41-1: a raw-bytes body would
        otherwise fail validation for every request)."""
        if isinstance(body, (bytes, bytearray)):
            return decode_frame(bytes(body))
        return body

    @staticmethod
    def _frame(request_id, code, detail):
        payload = {"v": 2, "request_id": request_id, "ok": False,
                   "error_code": code, "result": {"detail": detail}}
        return (json.dumps(payload) + "\n").encode("utf-8")

    # ------------------------------------------------------------ status
    def _write_status(self):
        if self._state_dir is None:
            return
        cls = type(self)
        payload = {"pid": os.getpid(),
                   "server": cls.__name__,
                   "state": "stopped" if self._stop.is_set() else "serving",
                   "pipe_name": self.pipe_name,
                   "cycles": self._cycles,
                   "last_error": self._last_error,
                   "unconfirmed_workers": cls._unconfirmed_abandoned,
                   "lifecycle": getattr(self, "lifecycle", None),
                   "maintenance": getattr(self, "maintenance", None)}
        path = self._state_dir / "broker-status.json"
        tmp = self._state_dir / "broker-status.json.tmp"
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        for _ in range(self._STATUS_REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(self._STATUS_REPLACE_BACKOFF)
        self._last_error = "status write failed (concurrent reader held " \
                           "the status file without delete sharing)"


class _UnixConnectionCycle:
    """One accepted AF_UNIX connection owned by ONE cycle (R41-4).

    The listener belongs to the server; this handle owns only the accepted
    connection, so a late worker can never operate the next cycle's peer. The
    closed flag makes attach/finish/abort idempotent under the outer
    abandonment path (cancel -> shutdown/close -> release).
    """

    def __init__(self, listener):
        self.listener = listener
        self.conn = None
        self.credentials = None
        self._closed = False
        self._lock = threading.Lock()

    def attach(self, conn, credentials):
        with self._lock:
            closed = self._closed
            if not closed:
                self.conn = conn
                self.credentials = credentials
        if closed:
            try:
                conn.close()
            except OSError:
                pass
            return False
        return True

    def abort(self):
        """Release a blocked worker: shutdown + close the accepted socket.
        The listener is NOT touched - later cycles reuse it."""
        with self._lock:
            self._closed = True
            conn = self.conn
        if conn is None:
            return
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass

    def finish(self):
        """Normal completion: flush the FIN, drain any unread request bytes
        (so close() does not turn the queued reply into an RST), then close."""
        with self._lock:
            conn = self.conn
            if self._closed:
                conn = None
            self._closed = True
        if conn is None:
            return
        try:
            conn.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        try:
            conn.setblocking(False)
            for _ in range(16):
                try:
                    if not conn.recv(4096):
                        break
                except (BlockingIOError, InterruptedError):
                    break
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass


class _UnixSocketFacade:
    """The tiny slice of the Win32 facade the bounded base uses in its
    abandonment path. 'DisconnectNamedPipe' releases a blocked worker by
    shutting the accepted socket down; there is no thread-cancel API, so the
    base helper detects the missing OpenThread/CancelSynchronousIo and reports
    False - the shutdown/close IS the release mechanism (POSIX D4 analogue)."""

    @staticmethod
    def DisconnectNamedPipe(handle):
        if handle is not None:
            handle.abort()

    @staticmethod
    def CloseHandle(handle):
        if handle is not None:
            handle.finish()


def _ensure_private_socket_directory(path):
    """Create the endpoint directory 0700 (never wider) and refuse an existing
    directory another local user could reach."""
    try:
        os.makedirs(path, mode=UNIX_SOCKET_DIRECTORY_MODE, exist_ok=True)
    except FileExistsError as exc:
        raise NotADirectoryError(
            errno.ENOTDIR,
            f"unix socket parent is not a directory: {path}") from exc
    info = os.stat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(
            errno.ENOTDIR,
            f"unix socket parent is not a directory: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError(
            errno.EACCES,
            f"unix socket parent directory must be 0700: {path}")


def _reclaim_stale_socket(path):
    """Refuse a LIVE endpoint; reclaim only a DEAD socket file.

    An existing regular file is refused (never unlinked). A socket file is
    probed with a short connect: success proves a live listener (refuse), a
    refused connect proves the file is stale (unlink and bind). This is the
    restart recovery path; it never relies on a PID/status file.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode):
        raise FileExistsError(
            errno.EEXIST,
            f"unix socket path already exists and is not a socket: {path}")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        probe.connect(path)
    except (ConnectionRefusedError, FileNotFoundError):
        os.unlink(path)
        return
    except socket.timeout as exc:
        raise FileExistsError(
            errno.EADDRINUSE,
            f"unix socket endpoint is busy: {path}") from exc
    finally:
        probe.close()
    raise FileExistsError(
        errno.EADDRINUSE, f"unix socket endpoint is already served: {path}")


class _UnixSocketTransport(BoundedPipeServer):
    """POSIX AF_UNIX SOCK_STREAM transport for the bounded cycle (R37/R41-4).

    One connection cycle = accept -> bounded read -> dispatch -> bounded write
    -> close, one daemon worker per cycle, the 1 MiB cap checked BEFORE a
    complete frame is accepted. Only the transport pieces are overridden:

    * ``_create_instance`` keeps ONE listener per server: the first cycle
      binds the absolute path (parent directory 0700, socket file 0600) and
      later cycles reuse it. ``probe_instance_listener`` therefore fails
      EARLY on a bad path, a wider directory, a live endpoint or a failed
      bind.
    * ``_bounded_connect`` accepts with a bounded poll so cancel/deadline
      release a parked accept; the accepted socket is attached to the
      cycle-owned handle.
    * ``_bounded_read``/``_bounded_write`` reuse the base framing rule
      (newline-terminated, 1 MiB checked BEFORE a complete frame, cancel
      checked before every chunk) over blocking socket I/O bounded by the
      remaining deadline.
    * peer identity is KERNEL evidence: SO_PEERCRED supplies pid/uid/gid,
      exposed as ``peer_credentials(handle)``/``last_peer_credentials``,
      with ``_client_pid`` feeding the base connection record exactly like
      the named-pipe server's client PID. No status file or self-reported
      PID is ever trusted.

    ``stop()`` ends the loop at the cycle boundary; the serving loop's final
    cleanup closes the listener and unlinks only the socket file it created.
    """

    _LISTEN_BACKLOG = 16
    _ACCEPT_POLL_SECONDS = 0.2

    def __init__(self, socket_path, deadline_seconds=DEFAULT_DEADLINE_SECONDS,
                 state_dir=None, clock=time.monotonic,
                 dispatch_deadline_seconds=None):
        if os.name != "posix":
            raise OSError("unix domain sockets require a POSIX platform")
        if not hasattr(socket, "AF_UNIX"):
            raise OSError("AF_UNIX sockets are unavailable on this platform")
        path = validate_unix_socket_path(socket_path)
        super().__init__(path, sddl=None, kernel=None,
                         deadline_seconds=deadline_seconds,
                         state_dir=state_dir, clock=clock,
                         dispatch_deadline_seconds=dispatch_deadline_seconds)
        self.socket_path = path
        self._facade = _UnixSocketFacade()
        self._listener = None
        self._socket_identity = None
        self._listener_lock = threading.Lock()
        self.last_peer_credentials = None

    # -------------------------------------------------------- transport
    def _kernel(self):
        return self._facade, None

    def _create_instance(self, sddl=None):
        with self._listener_lock:
            if self._listener is None:
                self._bind_listener_locked()
            cycle = _UnixConnectionCycle(self._listener)
            self._handle = cycle
            return cycle

    def _bind_listener_locked(self):
        path = self.socket_path
        _ensure_private_socket_directory(os.path.dirname(path))
        _reclaim_stale_socket(path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound = False
        try:
            listener.bind(path)
            bound = True
            os.chmod(path, UNIX_SOCKET_FILE_MODE)
            listener.listen(self._LISTEN_BACKLOG)
            listener.settimeout(self._ACCEPT_POLL_SECONDS)
            info = os.stat(path)
            if not stat.S_ISSOCK(info.st_mode) or \
                    stat.S_IMODE(info.st_mode) != UNIX_SOCKET_FILE_MODE:
                raise PermissionError(
                    errno.EACCES,
                    f"unix socket file must be 0600: {path}")
        except BaseException:
            listener.close()
            if bound:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            raise
        self._listener = listener
        self._socket_identity = (info.st_dev, info.st_ino)

    def _close_listener(self):
        with self._listener_lock:
            listener = self._listener
            identity = self._socket_identity
            self._listener = None
            self._socket_identity = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if identity is None:
            return
        try:
            info = os.lstat(self.socket_path)
            if stat.S_ISSOCK(info.st_mode) and \
                    (info.st_dev, info.st_ino) == identity:
                os.unlink(self.socket_path)
        except OSError:
            pass

    def close(self):
        """Stop serving and remove THIS server's socket file (never a
        replacement another process created at the same path)."""
        self.stop()
        self._close_listener()

    def serve_forever(self):
        try:
            super().serve_forever()
        finally:
            self._close_listener()

    def _bounded_connect(self, handle, cancel, deadline):
        listener = handle.listener
        while True:
            if cancel.is_set() or self._clock() >= deadline:
                raise _CancelledConnection()
            remaining = deadline - self._clock()
            listener.settimeout(max(min(remaining, self._ACCEPT_POLL_SECONDS),
                                   0.001))
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if cancel.is_set() or self._clock() >= deadline:
                    raise _CancelledConnection() from exc
                raise
            credentials = unix_peer_credentials(conn)
            if not handle.attach(conn, credentials):
                raise _CancelledConnection()
            self.last_peer_credentials = credentials
            return

    def _bounded_read(self, handle, cancel, deadline):
        conn = handle.conn
        if conn is None:
            raise OSError("unix socket connection is not established")
        chunks = bytearray()
        while True:
            if cancel.is_set() or self._clock() >= deadline:
                raise _CancelledConnection()
            remaining = deadline - self._clock()
            conn.settimeout(max(remaining, 0.001))
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                if cancel.is_set() or self._clock() >= deadline:
                    raise _CancelledConnection() from exc
                raise
            if not chunk:
                raise ProtocolError("bad_request", "client closed")
            chunks.extend(chunk)
            if len(chunks) > MAX_MESSAGE:
                raise ProtocolError("overlimit", "request exceeds bound")
            if chunks.endswith(b"\n"):
                return bytes(chunks)

    def _bounded_write(self, handle, frame, cancel, deadline):
        conn = handle.conn
        if conn is None:
            raise OSError("unix socket connection is not established")
        data = frame
        total = 0
        while total < len(data):
            if cancel.is_set() or self._clock() >= deadline:
                raise _CancelledConnection()
            remaining = deadline - self._clock()
            conn.settimeout(max(remaining, 0.001))
            try:
                sent = conn.send(data[total:])
            except socket.timeout:
                continue
            except OSError as exc:
                if cancel.is_set() or self._clock() >= deadline:
                    raise _CancelledConnection() from exc
                raise
            if sent <= 0:
                raise OSError("unix socket write failed")
            total += sent

    def _client_pid(self, handle):
        credentials = self.peer_credentials(handle)
        return None if credentials is None else credentials["pid"]

    def peer_credentials(self, handle=None):
        """SO_PEERCRED identity of the current (or given) connection."""
        if handle is None:
            handle = self._handle
        if handle is None or handle.credentials is None:
            return None
        return dict(handle.credentials)

    def _release_instance(self):
        handle = self._handle
        try:
            if handle is not None:
                handle.finish()
        finally:
            self._handle = None


class _TsServiceControlCoordinator:
    """Management dispatch decorator adding ONLY the restricted
    ``ts-service-control`` verb (the whitelist extension the console TS
    service seam drives; deployment authorization package section 5).

    Identity validation follows the EXISTING pipeline - every other request
    is delegated UNTOUCHED to the real coordinator; the new verb is
    validated with the coordinator's OWN control key through the SAME
    protocol validator and consumed through the SAME replay registry (no
    second key copy, no second validation rule). The service name never
    crosses the wire: the injected TailscaleServiceOps pins it to the
    protocol literal, and the enum validator refuses any other field or
    value. The result mirrors the coordinator's start contract: an ok
    reply only with an EXPLICIT ``ts_service_started/stopped`` flag from a
    re-confirmed state; anything else is an ``unconfirmed`` refusal - except
    a PERMANENT permission refusal (R13-T: the ops' last_control_rc reads
    ERROR_ACCESS_DENIED, rc 5), which answers the DISTINCT ``access_denied``
    code so the console reports 权限不足 instead of burning its bounded
    confirmation poll on a misleading "已执行" story.

    ``__getattr__`` forwards to the inner coordinator so the decorated
    object stays attribute-compatible with every existing consumer (the
    server layer keeps holding ONE coordinator).
    """

    def __init__(self, inner, ts_service_ops):
        self._inner = inner
        self._ts = ts_service_ops

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def handle_management(self, body, now=None):
        if not (isinstance(body, dict)
                and body.get("action") == TS_SERVICE_CONTROL_ACTION):
            return self._inner.handle_management(body, now=now)
        try:
            req = validate_management_request(body, self._inner.mgmt_secret,
                                              now=now)
        except ProtocolError as exc:
            frame = BrokerResult.err(exc.code, str(exc)).to_dict()
            frame["request_id"] = (body.get("request_id")
                                   if isinstance(body, dict) else None)
            return frame
        try:
            # One-time consumption BEFORE any side effect (R29-3) - the SAME
            # registry the inner coordinator uses for its own verbs.
            self._inner.replay.consume(req["request_id"], req["ts"], now=now)
        except ProtocolError as exc:
            frame = BrokerResult.err(exc.code, str(exc)).to_dict()
            frame["request_id"] = req["request_id"]
            return frame
        try:
            action = validate_ts_service_control_fields(req["fields"])["action"]
        except ProtocolError as exc:
            return self._reply(req, BrokerResult.err(exc.code, str(exc)))
        accepted = self._ts.start() if action == "start" else self._ts.stop()
        if accepted is not True:
            # R13-T: a PERMANENT refusal (sc.exe rc 5 ERROR_ACCESS_DENIED,
            # exposed by TailscaleServiceOps.last_control_rc) must not be
            # folded into the pending-confirmation "did not re-confirm"
            # shape - the command never executed, so the console's bounded
            # poll could only burn its budget on a misleading "已执行" story.
            # The distinct access_denied code reports 权限不足 honestly;
            # every other rejection keeps the existing unconfirmed contract.
            if getattr(self._ts, "last_control_rc", None) == \
                    getattr(type(self._ts), "RC_ACCESS_DENIED", 5):
                return self._reply(req, BrokerResult.err(
                    "access_denied",
                    "ts service %s denied by the OS (rc 5)" % action))
            return self._reply(req, BrokerResult.err(
                "unconfirmed",
                "ts service %s did not re-confirm the %s state"
                % (action, "running" if action == "start" else "stopped")))
        flag = ("ts_service_started" if action == "start"
                else "ts_service_stopped")
        return self._reply(req, BrokerResult.ok({flag: True}))

    @staticmethod
    def _reply(req, result):
        frame = result.to_dict()
        frame["request_id"] = req["request_id"]
        return frame


class _HostOpsControlCoordinator:
    """Management dispatch decorator adding ONLY the restricted
    ``host-verify`` (read-only evidence), ``host-stop`` (task gate) and
    ``host-evidence`` (read-only limits/faults/watchdog evidence) verbs (the
    whitelist extension the console share-host seams drive).

    The host evidence queries (task state, gateway processes, port
    listener, WFP limits, fault tail) need SYSTEM privilege: they execute
    INSIDE the broker through its own WindowsHostOps - the wire carries the
    fixed check enum and nothing else. The verdict is passed through
    VERBATIM (True/False/None for limits; a bounded dict or None for
    faults): an ``ok`` reply whose payload field is ``null`` means the
    evidence was unresolvable - it is an honest unconfirmed, never a
    fabricated verdict. Identity validation, replay consumption and the
    ok-flag shape follow the same discipline as the ts-service-control
    decorator."""

    def __init__(self, inner, host_ops):
        self._inner = inner
        self._host = host_ops

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def handle_management(self, body, now=None):
        if not (isinstance(body, dict)
                and body.get("action") in (HOST_VERIFY_ACTION,
                                           HOST_STOP_ACTION,
                                           HOST_REPAIR_LIMITS_ACTION,
                                           HOST_EVIDENCE_ACTION)):
            return self._inner.handle_management(body, now=now)
        try:
            req = validate_management_request(body, self._inner.mgmt_secret,
                                              now=now)
        except ProtocolError as exc:
            frame = BrokerResult.err(exc.code, str(exc)).to_dict()
            frame["request_id"] = (body.get("request_id")
                                   if isinstance(body, dict) else None)
            return frame
        try:
            self._inner.replay.consume(req["request_id"], req["ts"], now=now)
        except ProtocolError as exc:
            frame = BrokerResult.err(exc.code, str(exc)).to_dict()
            frame["request_id"] = req["request_id"]
            return frame
        action = body.get("action")
        if action == HOST_VERIFY_ACTION:
            try:
                check = validate_host_verify_fields(req["fields"])["check"]
            except ProtocolError as exc:
                return self._reply(req, BrokerResult.err(exc.code, str(exc)))
            verdict = (self._host.verify_running() if check == "running"
                       else self._host.verify_closed())
            return self._reply(req, BrokerResult.ok(
                {"host_running" if check == "running" else "host_closed":
                 verdict}))
        if action == HOST_EVIDENCE_ACTION:
            try:
                check = validate_host_evidence_fields(req["fields"])["check"]
            except ProtocolError as exc:
                return self._reply(req, BrokerResult.err(exc.code, str(exc)))
            return self._reply(req, self._host_evidence(check))
        if action == HOST_REPAIR_LIMITS_ACTION:
            try:
                validate_host_stop_fields(req["fields"])
            except ProtocolError as exc:
                return self._reply(req, BrokerResult.err(exc.code, str(exc)))
            repair = getattr(self._host, "repair_limits", None)
            if repair is None:
                return self._reply(req, BrokerResult.err("capability_missing", "repair helper unavailable"))
            result = repair()
            if not isinstance(result, dict) or result.get("repaired") is not True:
                return self._reply(req, BrokerResult.err(
                    result.get("code", "unconfirmed") if isinstance(result, dict) else "unconfirmed",
                    "limits repair not confirmed"))
            return self._reply(req, BrokerResult.ok({"repaired": True, "code": "ok"}))
        try:
            validate_host_stop_fields(req["fields"])
        except ProtocolError as exc:
            return self._reply(req, BrokerResult.err(exc.code, str(exc)))
        if self._host.stop_host() is not True:
            return self._reply(req, BrokerResult.err(
                "unconfirmed", "host stop did not confirm success"))
        # host-stop must verify CLOSURE, not only the stop call's return value:
        # a stale/missing pid file can make stop_host() return True while the
        # 8100 process is still alive, and the console would then start a
        # second owner.
        verify_closed = getattr(self._host, "verify_closed", None)
        if verify_closed is not None and verify_closed() is not True:
            return self._reply(req, BrokerResult.err(
                "unconfirmed", "host stop did not verify closure"))
        return self._reply(req, BrokerResult.ok({"host_stopped": True}))

    def _host_evidence(self, check):
        """Read-only host evidence: the verdict is passed through VERBATIM
        (None = unresolvable, never folded into True/False)."""
        checked_at = int(time.time())
        if check == "watchdog":
            observe = getattr(self._host, "read_watchdog", None)
            verdict = observe() if observe is not None else None
            detail = (verdict.get("query_error") or "ok") \
                if isinstance(verdict, dict) else "watchdog_capability_missing"
        elif check == "limits":
            observe = getattr(self._host, "verify_limits_evidence", None)
            if observe is not None:
                observed = observe()
                verdict, detail = observed["verdict"], observed["detail"]
            else:
                verify = getattr(self._host, "verify_limits", None)
                verdict = verify() if verify is not None else None
                detail = "ok" if verdict is not None else "limits_unavailable"
        else:
            read = getattr(self._host, "read_faults", None)
            verdict = read() if read is not None else None
            detail = ("read tail ok" if verdict is not None
                      else "fault_log_absent")
        return BrokerResult.ok({"check": check, "verdict": verdict,
                               "detail": detail,
                               "checked_at": checked_at})

    @staticmethod
    def _reply(req, result):
        frame = result.to_dict()
        frame["request_id"] = req["request_id"]
        return frame


class _ManagementDispatch:
    """Management-channel dispatch shared by BOTH transports.

    {status, publish, start, drain, emergency-stop} plus the restricted
    ts-service-control extension (via the dispatch decorator wrapping the
    coordinator) only. HMAC validation, replay consumption and the action
    whitelist all belong to the product coordinator - this layer never holds a
    second copy of the control key.

    ``lifecycle`` is a PROTECTED record of the last VERIFIED management
    transition ({state: running|stopped, at, by}), persisted in the broker
    dir (SYSTEM/admin write, operator read-only) and restored on start. The
    watchdog consumes it to distinguish a DELIBERATE management stop from an
    anomalous disappearance: a confirmed drain/emergency-stop keeps the
    ordinary stop valid across broker restarts; anomaly / limit-loss still
    close regardless of this record. The record is DURABLE-OR-UNCONFIRMED: a
    verified transition whose protected write fails is answered
    ``unconfirmed`` and leaves the in-memory record untouched, so a reload can
    never silently revert a state the caller was told was recorded.

    ``maintenance`` is a SEPARATE protected record of the temporary
    maintenance transient (idle|leased|restarting, contract §11.1): it is NEVER
    the lifecycle record and a maintenance window never writes a stop. It is
    persisted atomically (tmp+replace) and restored on start; the coordinator
    is the only writer (through the injected sink) and the watchdog consumes
    it to hold off bounded recovery while a window is in force.

    The Windows named-pipe server and the POSIX AF_UNIX server both use THIS
    class: the Linux entry must not fall back to the base transport's
    NotImplementedError, and it must not fake an ACK.
    """

    _LIFECYCLE_FILE = "lifecycle.json"
    _MAINTENANCE_FILE = "maintenance.json"
    _MAINTENANCE_STATES = ("idle", "leased", "restarting")

    def _init_management(self):
        """Initialise the protected records and wire the coordinator sink.

        Runs after the transport base ``__init__`` has set ``_state_dir``.
        """
        self.lifecycle = self._load_lifecycle()
        self.maintenance = self._load_maintenance()
        # The server layer is the ONLY writer of the protected record: a
        # coordinator assembled without an injected sink (direct
        # construction) gets THIS server's protected writer, so the
        # protected file is always the single persisted authority.
        _wire_maintenance_sink(self.coordinator, self._set_maintenance)
        # Startup recovery: re-adopt the persisted record BEFORE serving,
        # so a lease that was in force across a broker restart still binds.
        self._restore_maintenance_lease()

    def _load_lifecycle(self):
        """Restore the PROTECTED lifecycle record from the broker dir; an
        absent or malformed record falls back to running (@0) - fail-safe:
        without a persisted confirmed stop the watchdog keeps treating share
        absence as suspicious."""
        if self._state_dir is not None:
            path = Path(self._state_dir) / self._LIFECYCLE_FILE
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) \
                        and payload.get("state") in ("running", "stopped"):
                    return {"state": payload["state"],
                            "at": int(payload.get("at") or 0),
                            "by": str(payload.get("by") or "startup")}
            except (OSError, ValueError):
                pass
        return {"state": "running", "at": 0, "by": "startup"}

    def _set_lifecycle(self, state, by):
        """Record a VERIFIED management transition and persist it into the
        protected broker dir (atomic tmp+replace).

        Returns True ONLY when the record is DURABLE. A failed protected write
        must never leave the in-memory record claiming a transition the
        persisted authority (and the watchdog consuming it) never saw: the
        caller turns ``False`` into an honest ``unconfirmed`` answer instead of
        reporting a success whose reload would silently revert."""
        record = {"state": state, "at": int(time.time()), "by": by}
        if self._state_dir is not None:
            path = Path(self._state_dir) / self._LIFECYCLE_FILE
            tmp = Path(self._state_dir) / (self._LIFECYCLE_FILE + ".tmp")
            try:
                tmp.write_text(json.dumps(record), encoding="utf-8")
                os.replace(tmp, path)
            except OSError:
                return False
        self.lifecycle = record
        return True

    def _load_maintenance(self) -> dict:
        """Restore the PROTECTED maintenance record from the broker dir.
        An absent/malformed record or an unknown state falls back to the
        fail-safe ``{"state": "idle", "last_outcome": None}``: without a
        valid record there is NO maintenance in force. A record that claims
        leased/restarting WITHOUT a usable bounded expiry is malformed (the
        watchdog consumes ``expires_at``; a missing one could otherwise hold
        recovery off forever)."""
        fallback = {"state": "idle", "last_outcome": None}
        if self._state_dir is None:
            return fallback
        path = Path(self._state_dir) / self._MAINTENANCE_FILE
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return fallback
        if not isinstance(payload, dict) \
                or payload.get("state") not in self._MAINTENANCE_STATES:
            return fallback
        expires_at = payload.get("expires_at")
        if payload["state"] in ("leased", "restarting") \
                and not _valid_epoch(expires_at):
            return fallback
        record = dict(payload)
        outcome = record.get("last_outcome")
        record["last_outcome"] = outcome if isinstance(outcome, dict) else None
        return record

    def _set_maintenance(self, record) -> None:
        """Persist the PROTECTED maintenance record (atomic tmp+replace).
        Best-effort: a failed write only fails safe - the in-process record
        stays authoritative and the watchdog falls back to no maintenance."""
        self.maintenance = dict(record)
        if self._state_dir is None:
            return
        path = Path(self._state_dir) / self._MAINTENANCE_FILE
        tmp = Path(self._state_dir) / (self._MAINTENANCE_FILE + ".tmp")
        try:
            tmp.write_text(json.dumps(self.maintenance), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def _restore_maintenance_lease(self) -> None:
        """Startup recovery: re-adopt the persisted record into the
        coordinator's in-process state so a lease that was in force before a
        broker restart still binds (maintenance-end / lazy expiry)."""
        adopt = getattr(self.coordinator, "adopt_maintenance_record", None)
        if adopt is None:
            return
        adopt(dict(self.maintenance))

    def _dispatch(self, body, record):
        if getattr(self, "coordinator", None) is None:
            raise NotImplementedError(
                "management dispatch requires a coordinator")
        try:
            body = self._decode_body(body)
            response = self.coordinator.handle_management(body)
            # Watchdog coordination: persist the lifecycle transition ONLY
            # from a VERIFIED outcome (the coordinator's explicit result
            # flags), never from a request's intent. A transition whose
            # PROTECTED record cannot be persisted is answered ``unconfirmed``:
            # the in-memory record must not claim a durable state change the
            # watchdog never sees (a reload would silently revert it).
            if isinstance(body, dict):
                action = body.get("action")
                result = response.get("result") if isinstance(
                    response, dict) else None
                if isinstance(result, dict):
                    transition = None
                    if action in ("drain", "emergency-stop") \
                            and result.get("stopped") is True:
                        transition = ("stopped", action)
                    elif action == "start" \
                            and result.get("started") is True:
                        transition = ("running", "start")
                    if transition is not None \
                            and not self._set_lifecycle(*transition):
                        return self._frame(
                            body.get("request_id"), "unconfirmed",
                            "protected lifecycle record could not be "
                            "persisted; the transition is NOT recorded")
        except ProtocolError as exc:
            # Decode/shape rejection: answer with a structured refusal (the
            # request_id is unavailable from an unparsable body).
            request_id = body.get("request_id") if isinstance(body, dict) \
                else None
            return self._frame(request_id, exc.code, str(exc))
        except BaseException as exc:  # noqa: BLE001
            record["dispatch_error"] = f"{type(exc).__name__}: {exc}"
            request_id = body.get("request_id") if isinstance(body, dict) \
                else None
            return self._frame(request_id, "internal_error",
                               "coordinator failure")
        return (json.dumps(response) + "\n").encode("utf-8")


class ManagementPipeServer(_ManagementDispatch, BoundedPipeServer):
    """Windows named-pipe management server (same dispatch as AF_UNIX)."""

    def __init__(self, coordinator, pipe_name, sddl=None, kernel=None,
                 deadline_seconds=DEFAULT_DEADLINE_SECONDS, state_dir=None,
                 clock=time.monotonic, dispatch_deadline_seconds=None):
        self.coordinator = coordinator
        super().__init__(pipe_name, sddl=sddl, kernel=kernel,
                         deadline_seconds=deadline_seconds,
                         state_dir=state_dir, clock=clock,
                         dispatch_deadline_seconds=dispatch_deadline_seconds)
        self._init_management()


class UnixSocketBrokerServer(_ManagementDispatch, _UnixSocketTransport):
    """POSIX AF_UNIX management server (same dispatch as Windows).

    The product Linux entry assembles this class with its REAL coordinator,
    so publish/load/drain/host-stop/gateway-restart reach the same
    HMAC/replay/whitelist pipeline as the Windows ``ManagementPipeServer``
    and never the base transport's ``NotImplementedError``. Instance-channel
    subclasses override ``_dispatch`` exactly as the Windows instance server
    does.
    """

    def __init__(self, socket_path, deadline_seconds=DEFAULT_DEADLINE_SECONDS,
                 state_dir=None, clock=time.monotonic,
                 dispatch_deadline_seconds=None, coordinator=None):
        self.coordinator = coordinator
        super().__init__(socket_path, deadline_seconds=deadline_seconds,
                         state_dir=state_dir, clock=clock,
                         dispatch_deadline_seconds=dispatch_deadline_seconds)
        if coordinator is not None:
            self._init_management()
        else:
            # Instance-channel transport subclasses supply their own
            # ``_dispatch``; a management server without a coordinator must
            # fail loudly rather than serve an unauthenticated stub.
            self.lifecycle = None
            self.maintenance = None


def read_control_key(path):
    """Load a controlled secret file. The value is never logged; a
    missing/oversized key fails closed.

    On POSIX the same 0600/current-uid gate as the AES-GCM key file is
    enforced (Q11/R-posix secret boundary): a foreign-owned or
    world-readable control/instance key is refused instead of being used.
    Windows keeps its existing read path (the ACL boundary is separate).
    """
    from share_secret_protection import read_private_secret_path
    try:
        data = read_private_secret_path(
            path, max_bytes=128, label="control key").strip()
    except (OSError, ValueError) as exc:
        raise ValueError("control key is missing or malformed") from exc
    if not data:
        raise ValueError("control key is missing or malformed")
    return data.decode("utf-8")


def load_registration(path):
    """Read the share instance's SELF-published registration record
    (instance-registration.json beside the guard status). Returns None
    when the instance has not registered yet — an EXPLICIT not-ready state
    (the broker then serves status only and every publish reports
    unconfirmed); it never fabricates an instance id. The registration is
    only the DEPLOYMENT FACT for the initial target; the identity trust of
    every instance connection is the verifier's (R41-2), never this file."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    instance_id = payload.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        return None
    return payload


def _required_host_owner(args):
    """R51.4: the host-side owner expectation in DOMAIN\\user form. A
    missing config REFUSES assembly (the host evidence would silently
    skip the owner check otherwise); the SID form (--expect-owner-sid)
    stays a SEPARATE deployment knob for the per-connection verifier -
    the two shapes are never interchangeable."""
    owner = getattr(args, "expected_owner_account", None)
    if not owner:
        raise ValueError(
            "broker assembly refused: host owner expectation missing "
            "(--expected-owner-account, DOMAIN\\user form, required)")
    return owner


def build_server(args, kernel=None, instance_kernel_factory=None,
                 verifier=None, host_ops=None, acl_checker=None,
                 ts_service_ops=None, maintenance=None):
    """Assemble the broker host from deployment arguments (offline-testable
    injection points for every OS boundary). ``acl_checker`` overrides the
    default IcaclsAclChecker for offline assembly tests; production leaves
    it None so the REAL icacls-backed checker is built here.
    ``ts_service_ops`` is the restricted TS service primitive for the
    ``ts-service-control`` whitelist extension; production builds the REAL
    TailscaleServiceOps here (the service name is the protocol literal).
    ``maintenance`` optionally seeds the protected maintenance record (a
    dict with the §11.1 shape) - tests use it to simulate a record
    restored at startup; None loads it from the broker dir.

    Registration chain (R41-2): the registration file provides the INITIAL
    deployment fact only; every instance_call runs through the identity
    verifier against the ACTUAL pipe peer, a verified connection REFRESHES
    the registration (a replaced instance bumps the version, R31-3), and a
    failed/mismatched identity can never drive a load — a forged status
    file alone cannot make the broker serve an instance.
    """
    from share_broker_ops import (INSTANCE_PIPE_PREFIX, IcaclsAclChecker,
                                  TailscaleServiceOps, WindowsHostOps,
                                  make_instance_call,
                                  WindowsInstanceVerifier)
    from share_runtime import ProtectedProjectionWriter

    # R41-2/R44-1: the trust chain is mandatory, and the REAL assembly
    # refuses to run without explicit security expectations - defaulting
    # them silently skips identity comparison, and the placeholder SDDL is
    # NOT the protection to rely on.
    if verifier is None:
        if not args.expect_image or not args.expect_owner_sid:
            raise ValueError(
                "broker assembly refused: instance identity expectations "
                "missing (--expect-image/--expect-owner-sid required)")
        verifier = WindowsInstanceVerifier()

    control_secret = read_control_key(args.key_file)
    instance_secret = read_control_key(args.instance_key_file)
    writer = ProtectedProjectionWriter(
        Path(args.projection_path),
        acl_checker=acl_checker or IcaclsAclChecker.default(
            str(Path(args.projection_path))))
    # R46.5: the registration FILE is only the deployment fact that names
    # the addressing candidate - read exclusively by the discovery hook.
    # An unverified candidate can never occupy the trusted registry: it
    # starts EMPTY and is populated exclusively by on_verified AFTER the
    # identity verifier accepted the real pipe peer (a forged file then
    # only produces a pipe name that fails to connect, never a trusted
    # instance; broker-before-share recovers through the same hook).
    instances = InstanceRegistry()

    # PID-reuse guard: the creation time observed on the FIRST verified
    # connection becomes the expectation for every later connection to the
    # same instance; a different creation time (PID reused by another
    # process) is a refusal. Retired instances' expectations are dropped on
    # generation change (instance_call only ever targets the current id),
    # keeping the cache bounded without removing evidence still in use.
    creation_by_instance = {}

    def on_verified(instance_id, evidence):
        if instances.current() != instance_id:
            # A verified connection from a DIFFERENT instance id is a
            # generation change: refresh the registration (version bump)
            # and retire the replaced instance's creation expectation.
            retired = instances.current()
            if retired is not None:
                creation_by_instance.pop(retired, None)
            instances.register(instance_id)

    diag = BrokerDiag()
    instance_call = make_instance_call(
        secret_file=args.instance_key_file,
        pipe_prefix=getattr(args, "instance_pipe_prefix", None)
        or INSTANCE_PIPE_PREFIX,
        verifier=verifier,
        expect_image=args.expect_image,
        expect_owner_sid=args.expect_owner_sid,
        creation_by_instance=creation_by_instance,
        on_verified=on_verified,
        kernel_factory=instance_kernel_factory,
        diag_event=diag.record)
    registration_path = args.registration_file

    def discover_instance():
        """R44-2: re-read the CURRENT registration candidate on every
        attempt (the share may register itself AFTER the broker started,
        or restart under a new instance id). The candidate only ADDRESSES
        the connection; the trusted registry advances exclusively through
        the verifier inside instance_call (on_verified)."""
        candidate = load_registration(registration_path)
        if candidate is None:
            diag.record("registration-read", ok=False,
                        error_code="registration-absent")
            return None
        diag.record("registration-read", ok=True,
                    instance_id=candidate.get("instance_id"))
        try:
            resp = instance_call("instance-status", {},
                                 candidate["instance_id"])
        except Exception as exc:  # noqa: BLE001  already diaged in the call
            diag.record("instance-status", ok=False,
                        exception_type=type(exc).__name__,
                        instance_id=candidate.get("instance_id"))
            raise
        diag.record("instance-status", ok=bool(resp.get("ok")),
                    error_code=resp.get("error_code"),
                    instance_id=candidate.get("instance_id"))
        if not resp.get("ok"):
            return None
        return candidate["instance_id"]

    resolved_host_ops = host_ops or WindowsHostOps(
        task_name=args.share_task,
        share_port=getattr(args, "share_port", 8100),
        # R51.4: the REAL assembly wires the identity expectations into
        # the host evidence - a missing owner-account config REFUSES
        # (both forms are required; SID vs DOMAIN\\user stay separate,
        # the account form is the host-side normalized source).
        expected_image_path=args.expect_image,
        expected_owner=_required_host_owner(args),
        # Read-only host evidence (contract §2.5/§11.2): the limits check
        # reuses the watchdog's own Get-LimitsState and the fault
        # evidence is a bounded tail read; every path is deployment
        # config and None means the evidence is honestly unavailable.
        run_root=getattr(args, "run_root", None),
        watchdog_script=getattr(args, "watchdog_script", None),
        firewall_rule=getattr(args, "firewall_rule", None),
        share_faults_file=getattr(args, "share_faults_file", None))
    # The protected maintenance record is persisted by the SERVER layer;
    # the coordinator is the only writer and calls the sink on every change.
    # The cell breaks the coordinator -> server construction cycle (the
    # coordinator exists before the server).
    maintenance_cell = {}

    def maintenance_sink(record):
        server = maintenance_cell.get("server")
        if server is not None:
            server._set_maintenance(record)

    coordinator = BrokerHostCoordinator(
        writer=writer, mgmt_secret=control_secret,
        instance_secret=instance_secret, instances=instances,
        replay=ReplayRegistry(),
        host_ops=resolved_host_ops,
        instance_call=instance_call,
        discover_instance=discover_instance,
        diag=diag,
        leases=MaintenanceLeaseStore(),
        maintenance=maintenance_sink,
        # R8-A7: the graceful-close drain window is up to 60 seconds (the
        # orchestration's drain budget contract); the broker default 5.0s
        # silently truncated it (S3-R6A3 finding R-S3-1).
        # The final stop + closure verification get their OWN reserved budget:
        # a drain window that times out must still stop the host. The numbers
        # are the SHARED protocol contract (both sides read them; the client's
        # exchange deadline covers their sum).
        drain_timeout=DRAIN_WINDOW_SECONDS,
        stop_timeout=STOP_BUDGET_SECONDS)
    # The ts-service-control whitelist extension decorates the coordinator:
    # only the new verb is intercepted, everything else delegates unchanged.
    coordinator = _TsServiceControlCoordinator(
        coordinator, ts_service_ops or TailscaleServiceOps())
    # The host-evidence extension decorates on top: host-verify (read-only
    # SYSTEM evidence), host-stop (task gate) and host-evidence (read-only
    # limits/faults) intercept only their own verbs; every management
    # request the coordinator already serves passes through both decorators
    # untouched. The decorator shares the SAME WindowsHostOps instance as
    # the coordinator (one evidence source).
    coordinator = _HostOpsControlCoordinator(coordinator, resolved_host_ops)
    server = ManagementPipeServer(
        coordinator, args.pipe_name, sddl=args.sddl, kernel=kernel,
        deadline_seconds=args.deadline, state_dir=Path(args.broker_dir),
        # Contract §6.4: the dispatch budget is a SEPARATE bound (default
        # 130 s in the CLI) so a slow bounded management action - the
        # gateway-restart - is not abandoned at the 10 s pipe deadline.
        dispatch_deadline_seconds=getattr(args, "dispatch_deadline", None))
    maintenance_cell["server"] = server
    if maintenance is not None:
        # Test/deployment seam: seed the protected record before serving
        # (the coordinator re-adopts it through startup recovery).
        server.maintenance = dict(maintenance)
        server._restore_maintenance_lease()
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="UM share broker host (SYSTEM scheduled task entry)")
    parser.add_argument("--broker-dir", required=True,
                        help="broker state dir (RunRoot\\broker)")
    parser.add_argument("--key-file", required=True,
                        help="broker-control-key path")
    parser.add_argument("--instance-key-file", required=True,
                        help="instance channel secret (shared with the "
                             "share instance)")
    parser.add_argument("--registration-file", required=True,
                        help="instance-registration.json self-published by "
                             "the share instance (may be absent = "
                             "explicitly not ready)")
    parser.add_argument("--pipe-name", default=r"\\.\pipe\um-share-broker")
    parser.add_argument("--instance-pipe-prefix",
                        default=r"\\.\pipe\um-share-instance-",
                        help="instance channel prefix; experiments MUST pass "
                             "their own prefix so the two channels can never "
                             "collide with production")
    parser.add_argument("--sddl", default=PLANNED_SDDL,
                        help="management pipe DACL (SDDL, L2-calibrated)")
    parser.add_argument("--projection-path", required=True,
                        help="protected projection landing path")
    parser.add_argument("--share-task", required=True,
                        help="scheduled task name of the share host")
    parser.add_argument("--share-port", type=int, default=8100,
                        help="share listener TCP port (closure evidence "
                             "also checks for a listener on this port)")
    parser.add_argument("--expect-image", default=None,
                        help="FULL image path the instance pipe peer must "
                             "have (deployment config, not a status file)")
    parser.add_argument("--expect-owner-sid", default=None,
                        help="owner SID the instance pipe peer must have")
    parser.add_argument("--expected-owner-account", default=None,
                        help="host-side owner expectation in DOMAIN\\user "
                             "form (assembly refuses without it; NOT "
                             "interchangeable with the SID form)")
    parser.add_argument("--deadline", type=float,
                        default=DEFAULT_DEADLINE_SECONDS)
    # Contract §6.4/§11.2: the DISPATCH deadline is a separate bound
    # (the wrapper passes 130 so a bounded gateway-restart is not
    # abandoned at the 10 s pipe deadline); the read-only host evidence
    # needs the run root + the watchdog script + the firewall rule name +
    # the share gateway's fault file. None of these is ever a wire field.
    parser.add_argument("--dispatch-deadline", type=float, default=130.0,
                        help="bounded dispatch budget for ONE management "
                             "cycle (covers a bounded gateway-restart; the "
                             "default 130 s exceeds the 10 s read deadline)")
    parser.add_argument("--run-root", default=r"C:\ProgramData\UMShareRun",
                        help="protected share run root (limits check + fault "
                             "file location)")
    parser.add_argument("--watchdog-script", default=None,
                        help="watchdog-umshare.ps1 path dot-sourced with "
                             "-FunctionsOnly for the read-only limits check")
    parser.add_argument("--firewall-rule", default="um-share-outbound-block",
                        help="share outbound firewall rule display name")
    parser.add_argument("--share-faults-file", default=None,
                        help="share-faults.jsonl path (bounded tail read)")
    args = parser.parse_args(argv)

    server = build_server(args)
    server._write_status()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.stop()
    server._write_status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
