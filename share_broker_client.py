r"""share_broker_client.py — ordinary-privilege client for the REAL broker.

The 8096 management process (and, through ShareManager, every mutating
command) reaches the SYSTEM broker host through this client. It binds a
fixed loopback endpoint chosen by the injected transport, enforces the
protocol schema v2 (HMAC, total-deadline read, associated request id) and
returns structured BrokerResult values — an arbitrary dict or exit code
never counts as completion (D1/A4).

Transports are replaceable adapters (D0 §27):
* ``LoopbackTcpBrokerTransport`` — offline tests / explicit fallback; it is
  NOT a production adapter and never counts as OS permission evidence.
* ``NamedPipeBrokerTransport`` — the D0-selected production transport
  (ctypes, DACL by SID); its wiring depends on UNVERIFIED OS assumptions
  (§27.7 H1–H6) and must be verified in the authorized L2 experiment before
  it is used outside fixtures.

When no broker component is installed the client reports ``not_installed``
instead of falling back to any test broker (A4: no fake success).
"""
from __future__ import annotations

import inspect
import math
import socket
import threading
import time

from share_broker_protocol import (DEFAULT_DEADLINE_SECONDS, MAX_MESSAGE,
                                   BrokerResult, ProtocolError,
                                   build_request, decode_frame,
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


class NamedPipeBrokerTransport(BrokerTransport):
    """ctypes named-pipe adapter (D0 §27.2 production selection).

    Win32 access goes through an INJECTABLE kernel facade (`_kernel()`):
    signatures are declared once and tests may substitute a fake kernel to
    exercise blocking reads/writes, deadline expiry, cancellation, failure
    and chunked transfers OFFLINE (R31-5). The real Windows pipe semantics
    (DACL, blocking-release via CloseHandle) remain D4-verified.

    Total-deadline semantics: every blocking ReadFile/WriteFile runs on a
    daemon worker while the caller waits the REMAINING deadline; on expiry
    the call is abandoned (its result discarded) and the handle is closed -
    closing a handle with pending synchronous I/O releases the blocked call
    (OS behavior verified at D4). No request can park beyond the deadline;
    nothing after an abandoned call is ever reported as success.

    Per-exchange cancellation (R35-1): every send/recv exchange owns a
    cancel event that is checked BEFORE EVERY chunk I/O, so a late success
    of the blocked call after abandonment must not issue the next block.
    The abandonment path sets cancel FIRST, then closes the handle. Worker
    exit is observed in a bounded window. The single already-in-flight call
    on the closed handle is the OS release mechanism itself (D4); no NEW
    I/O is ever issued after cancellation.

    Bounded worker admission (R37): every exchange ATOMICALLY reserves one
    process-wide worker slot BEFORE its daemon worker starts; the cap
    (``_MAX_UNREAPED_ABANDONED_WORKERS``) covers workers that are running
    or whose exit could not be confirmed, so concurrent exchanges can no
    longer bypass the bound by all observing a zero count. The slot is
    released when the worker's exit is confirmed (normal completion, or
    abandonment whose exit the observation window confirmed); an
    unconfirmed exit converts the reservation into the permanent
    process-wide unconfirmed count, which is NEVER reclaimed afterwards
    (conservative bound, no double-release path exists). Consequently a
    caller returns by the exchange deadline PLUS a bounded finalization
    observation window (up to ``_WORKER_EXIT_OBSERVE_SECONDS``) in the
    abandonment case - never claim a strict in-deadline return.
    """

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _OPEN_EXISTING = 3
    _ERROR_PIPE_BUSY = 231
    # Bounded open retry (W2 run20 root cause): the management server owns
    # ONE pipe instance per connection cycle and releases it BEFORE writing
    # the cycle telemetry and re-creating, so an immediate back-to-back
    # reconnect lands in a ~1.5ms no-instance window observed offline as
    # ERROR_FILE_NOT_FOUND (2, zero instances), ERROR_PATH_NOT_FOUND (3, the
    # transient gap between release and re-create) or ERROR_PIPE_BUSY (231,
    # the released instance is no longer listening). All three codes are
    # transient serving gaps and are retried under a bounded budget; every
    # other failure (ACCESS_DENIED and the rest) keeps the immediate refusal
    # so DACL semantics are never retried into a false success. The retry runs
    # BEFORE any connection exists: one exchange still owns exactly one
    # connection (R44-1).
    _TRANSIENT_OPEN_ERRORS = (2, 3, 231)
    _OPEN_RETRY_SECONDS = 2.0
    _OPEN_RETRY_BASE_DELAY = 0.01
    _OPEN_RETRY_MAX_DELAY = 0.05
    # R48-1: the one-instance server releases its cycle instance BEFORE it
    # re-creates one, so a CreateFileW can succeed against an instance the
    # server is about to drop. The open retry above covers the no-instance
    # window BEFORE a connection exists; ERROR_PIPE_NOT_CONNECTED (233, the
    # queued connection is never completed), ERROR_NO_DATA (232) and
    # ERROR_BROKEN_PIPE (109) after a successful open are that SAME serving
    # gap observed on the write. They are retried by dropping the stale
    # handle and re-opening under the bounded open budget; a failed reopen or
    # budget exhaustion keeps the original refusal (never a pretend write).
    _TRANSIENT_WRITE_ERRORS = (109, 232, 233)
    _WRITE_RETRY_ATTEMPTS = 2
    # R35-1/R37 bounded disposal: process-wide cap on daemon workers whose
    # exit could not be confirmed after abandonment; admission refuses
    # beyond. Unconfirmed slots are never reclaimed (conservative bound).
    _MAX_UNREAPED_ABANDONED_WORKERS = 8
    # Bounded window to observe a released worker's exit before counting it
    # as unconfirmed - never an unbounded wait.
    _WORKER_EXIT_OBSERVE_SECONDS = 2.0
    _unreaped_abandoned = 0
    _reserved_worker_slots = 0
    _accounting_lock = threading.Lock()

    def __init__(self, pipe_name=r"\\.\pipe\um-share-public-broker", kernel=None):
        self.pipe_name = pipe_name
        self._handle = None
        self._handle_lock = threading.Lock()
        # R48-1: only broker-owned connections (created by THIS transport for
        # this exchange) may reconnect on transient write errors. A
        # caller-owned transport was identity-verified by the caller before
        # the exchange (R41-2); reopening it would silently downgrade the
        # verified connection, so it never reconnects.
        self._allow_reconnect = False
        self._kernel_override = kernel   # tests inject a fake kernel facade
        self._abandoned = False          # 超时放弃后 transport 不可再用
        # R35-1 observability: abandoned workers whose exit was confirmed
        # within the bounded window vs still unconfirmed at window end.
        self.abandoned_workers_reaped = 0
        self.abandoned_workers_unreaped = 0

    def _kernel(self):
        """The Win32 kernel facade: injectable for offline tests (R31-5);
        production resolves the real kernel32 with explicit signatures."""
        if self._kernel_override is not None:
            return self._kernel_override
        return self._declare_real_kernel()

    @staticmethod
    def _declare_real_kernel():
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong,
                                         ctypes.c_ulong, ctypes.c_void_p,
                                         ctypes.c_ulong, ctypes.c_ulong,
                                         ctypes.c_void_p]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_ulong,
                                      ctypes.POINTER(ctypes.c_ulong),
                                      ctypes.c_void_p]
        kernel32.ReadFile.restype = ctypes.c_int
        kernel32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                       ctypes.c_ulong,
                                       ctypes.POINTER(ctypes.c_ulong),
                                       ctypes.c_void_p]
        kernel32.WriteFile.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.GetLastError.restype = ctypes.c_ulong
        return kernel32

    def _bounded_call(self, fn, cancel, deadline):
        """Run a blocking Win32 call under the TOTAL deadline with an
        explicit per-exchange cancel owner (R35-1) and an atomically
        reserved worker slot (R37).

        Expiry is checked BEFORE the reservation (an already-expired
        exchange performs ZERO I/O and consumes NO slot). A daemon worker
        executes ``fn(cancel)`` while the caller waits the remaining time;
        completion is judged by ``worker.is_alive()`` after ``join`` (join's
        return value is not a completion indicator). On expiry the caller
        sets ``cancel`` FIRST, then closes the handle: the cancel check
        before every chunk in ``fn`` guarantees a late success after
        abandonment never issues the next block. Worker exit is then
        observed in a bounded window; the reserved slot is released on a
        confirmed exit and converted into the permanent process-wide
        unconfirmed count otherwise.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Zero-side-effect expiry: nothing is written or read, and no
            # worker slot is consumed.
            raise TimeoutError("exchange deadline exceeded before I/O")
        # R37: claim the bounded slot BEFORE the worker starts; the check
        # and the reservation happen in ONE critical section, so concurrent
        # exchanges cannot all pass a zero count.
        self._reserve_worker_slot()
        outcome = {}
        done = threading.Event()

        def _worker():
            try:
                outcome["value"] = fn(cancel)
            except _CancelledExchange:
                # Abandoned exchange: the outcome is discarded by design;
                # the worker only needs to stop cleanly.
                outcome["cancelled"] = True
            except BaseException as exc:  # noqa: BLE001
                outcome["error"] = exc
            finally:
                done.set()

        worker = threading.Thread(target=_worker, daemon=True)
        try:
            worker.start()
        except BaseException:
            # A failed start must not leak the reserved slot.
            self._release_worker_slot()
            raise
        worker.join(remaining)
        if not worker.is_alive():
            # 退出已确认（正常或异常完成）→ 释放额度。
            self._release_worker_slot()
            if "error" in outcome:
                raise outcome["error"]
            return outcome["value"]
        # 到期放弃：先置取消（fn 在每块 I/O 前检查，迟到成功不再发下
        # 一块），再关闭句柄释放阻塞调用（真实 OS 释放语义 D4 验证）。
        # 放弃调用的结果被丢弃；worker 收尾在有界窗口内观察——调用方最迟
        # 在 exchange 期限 + 该有界收尾窗口内返回，不声称严格在期限内。
        cancel.set()
        self._abandoned = True
        # R48-1: the abandon close and any worker reopen both mutate the
        # handle; serializing them removes the double-close / stale-close
        # race. After this, open() refuses (abandoned), so no new handle is
        # ever created or orphaned by this transport.
        with self._handle_lock:
            try:
                if self._handle is not None:
                    self._kernel().CloseHandle(self._handle)
            finally:
                self._handle = None
        if done.wait(self._WORKER_EXIT_OBSERVE_SECONDS):
            # 收尾窗口内确认退出 → 释放额度。
            self.abandoned_workers_reaped += 1
            self._release_worker_slot()
        else:
            # 退出未确认 → 额度转为进程级永久占位，此后不回收（保守
            # 有界；不存在双释放路径）。
            with NamedPipeBrokerTransport._accounting_lock:
                NamedPipeBrokerTransport._reserved_worker_slots -= 1
                NamedPipeBrokerTransport._unreaped_abandoned += 1
            self.abandoned_workers_unreaped += 1
        raise TimeoutError("exchange deadline exceeded; blocking I/O "
                           "abandoned")

    def _reserve_worker_slot(self):
        """R37: atomically claim one bounded worker slot BEFORE the worker
        starts. The check and the reservation happen in ONE critical
        section, and the cap covers workers that are running or whose exit
        could not be confirmed - concurrent exchanges can no longer bypass
        the bound by all observing a zero count. Refuses at the cap."""
        cls = NamedPipeBrokerTransport
        with cls._accounting_lock:
            if (cls._unreaped_abandoned + cls._reserved_worker_slots
                    >= cls._MAX_UNREAPED_ABANDONED_WORKERS):
                raise OSError(
                    "broker transport admission refused: bounded cap of "
                    "in-flight/unconfirmed abandoned workers reached")
            cls._reserved_worker_slots += 1

    @classmethod
    def _release_worker_slot(cls):
        """Release a reserved slot after the worker's exit was confirmed
        (normal completion, or abandonment whose exit the bounded
        observation window confirmed)."""
        with cls._accounting_lock:
            cls._reserved_worker_slots -= 1

    def _check_admission(self):
        """An already-abandoned transport is never reused; the bounded
        worker-slot cap is enforced atomically at reservation time
        (``_reserve_worker_slot``, R37), not by a post-hoc counter read."""
        if self._abandoned:
            raise OSError("transport abandoned after a deadline expiry")

    def peer_server_pid(self):
        """Identity evidence for the pipe SERVER process (Win8+): the
        instance-channel client (the broker) uses it to verify the peer's
        actual process identity before trusting the connection (R41-2).
        Returns None when the query is unsupported or fails - callers must
        treat None as a refusal, not as a verified peer."""
        if self._handle is None or self._abandoned:
            return None
        import ctypes
        kernel32 = self._kernel()
        pid = ctypes.c_ulong(0)
        try:
            ok = kernel32.GetNamedPipeServerProcessId(
                self._handle, ctypes.pointer(pid))
        except AttributeError:
            return None
        if ok:
            return int(pid.value)
        return None

    def open(self, deadline=None):
        kernel32 = self._kernel()
        with self._handle_lock:
            if self._abandoned:
                raise OSError("transport abandoned; refused to reopen")
        open_deadline = time.monotonic() + self._OPEN_RETRY_SECONDS
        if deadline is not None:
            open_deadline = min(open_deadline, deadline)
        delay = self._OPEN_RETRY_BASE_DELAY
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("instance preparation deadline exceeded")
            # R48-1: once abandoned, no new handle is ever created (checked
            # under the handle lock so it cannot race the abandon path).
            with self._handle_lock:
                if self._abandoned:
                    raise OSError("transport abandoned; refused to reopen")
            handle = kernel32.CreateFileW(
                self.pipe_name,
                self._GENERIC_READ | self._GENERIC_WRITE, 0, None,
                self._OPEN_EXISTING, 0, None)
            if handle and handle != 0xFFFFFFFFFFFFFFFF:
                if deadline is not None and time.monotonic() >= deadline:
                    kernel32.CloseHandle(handle)
                    raise TimeoutError("instance preparation deadline exceeded")
                with self._handle_lock:
                    if self._abandoned:
                        # Abandon happened between CreateFileW and the
                        # assignment: close this handle, never store it.
                        self._kernel().CloseHandle(handle)
                        raise OSError(
                            "transport abandoned; refused to reopen")
                    self._handle = handle
                return self
            # The error code must be read BEFORE any other Win32 call; fake
            # kernels without GetLastError fail fast exactly as before.
            get_last_error = getattr(kernel32, "GetLastError", None)
            last_error = get_last_error() if get_last_error else 0
            if (last_error not in self._TRANSIENT_OPEN_ERRORS
                    or time.monotonic() >= open_deadline):
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("instance preparation deadline exceeded")
                raise OSError("broker pipe not available")
            time.sleep(min(delay, max(0.0, open_deadline - time.monotonic())))
            delay = min(delay * 2, self._OPEN_RETRY_MAX_DELAY)

    def prepare_peer(self, verifier, *, deadline, **expectations):
        """Bound connection and identity queries before any request is sent.

        Reuse the transport's cancellation and worker accounting. A blocked
        native identity query may finish late, but cannot send a request or
        notify the registry after the caller abandons its preparation.
        """
        def check(cancel):
            if cancel.is_set():
                raise _CancelledExchange()
            if time.monotonic() >= deadline:
                raise TimeoutError("instance preparation deadline exceeded")

        def prepare(cancel):
            check(cancel)
            self.open(deadline=deadline)
            check(cancel)
            pid = self.peer_server_pid()
            check(cancel)
            evidence = None if pid is None else verifier.verify(
                pid, **expectations)
            check(cancel)
            return pid, evidence

        return self._bounded_call(prepare, threading.Event(), deadline)

    def close(self):
        with self._handle_lock:
            try:
                if self._handle is not None:
                    self._kernel().CloseHandle(self._handle)
            finally:
                self._handle = None

    def send_all(self, data: bytes, deadline=None):
        import ctypes
        if deadline is None:
            raise ValueError("send_all requires an explicit exchange deadline")
        self._check_admission()
        cancel = threading.Event()      # R35-1: per-exchange cancel owner

        def _write_all(cancel):
            kernel32 = self._kernel()
            attempts = 0
            handle = self._handle
            written = ctypes.c_ulong(0)
            total = 0
            while total < len(data):
                # R35-1: cancellation is checked BEFORE EVERY chunk; a late
                # success after abandonment must not issue the next block.
                if cancel.is_set():
                    raise _CancelledExchange()
                ok = kernel32.WriteFile(handle, data[total:],
                                        len(data) - total,
                                        ctypes.pointer(written), None)
                if not ok or not written.value:
                    # R48-1: a write into an instance the one-instance server
                    # already released is a transient serving gap (233/232/
                    # 109). ONLY broker-owned connections reconnect (a
                    # caller-owned transport was identity-verified before the
                    # exchange, R41-2). Each retry re-sends the WHOLE frame
                    # from offset 0 - the previous connection's bytes were
                    # discarded with the dropped handle. Any other failure
                    # keeps the immediate refusal.
                    if (self._allow_reconnect
                            and attempts < self._WRITE_RETRY_ATTEMPTS):
                        get_last_error = getattr(
                            kernel32, "GetLastError", None)
                        code = get_last_error() if get_last_error else 0
                        if code in self._TRANSIENT_WRITE_ERRORS:
                            if cancel.is_set():
                                raise _CancelledExchange()
                            attempts += 1
                            self.close()                # drop stale handle
                            self.open(deadline=deadline)
                            handle = self._handle
                            written = ctypes.c_ulong(0)
                            total = 0                   # resend whole frame
                            continue
                    raise OSError("broker pipe write failed")
                total += written.value
            return total

        return self._bounded_call(_write_all, cancel, deadline)

    def recv_line(self, deadline) -> bytes:
        """Bounded read with the TOTAL deadline enforced by the cancellable
        worker (R33-1/R35-1): the whole chunk loop runs inside one
        cancellable worker; cancellation is re-checked before EVERY
        ReadFile chunk, so a late success after abandonment never issues
        the next block. Chunks accumulate; oversized frames and closed
        pipes are explicit errors, never successes."""
        import ctypes
        self._check_admission()
        handle = self._handle
        cancel = threading.Event()      # R35-1: per-exchange cancel owner

        def _read_frame(cancel):
            kernel32 = self._kernel()
            chunks = bytearray()
            while True:
                # R35-1: cancellation is checked BEFORE EVERY chunk.
                if cancel.is_set():
                    raise _CancelledExchange()
                buf = ctypes.create_string_buffer(4096)
                read = ctypes.c_ulong(0)
                ok = kernel32.ReadFile(handle, buf, 4096,
                                       ctypes.pointer(read), None)
                if not ok:
                    raise OSError("broker pipe read failed")
                if not read.value:
                    raise ProtocolError("empty_request", "pipe closed")
                chunks += buf.raw[:read.value]
                if chunks.endswith(b"\n"):
                    return bytes(chunks)
                if len(chunks) > MAX_MESSAGE:
                    raise ProtocolError("overlimit",
                                        "response exceeds bound")

        return self._bounded_call(_read_frame, cancel, deadline)


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
            return BrokerResult.err("not_installed",
                                    f"broker component unavailable: "
                                    f"{type(exc).__name__}")
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
        return self.call("status")

    def start(self) -> BrokerResult:
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
        return self.call("host-verify", {"check": check})

    def host_stop(self) -> BrokerResult:
        return self.call("host-stop")

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
        (never share.stop -> share.start). The exchange is BLOCKING and bounded:
        budget_s + 15 seconds, capped at the frozen 180 second protocol bound
        (budget_s itself stays wire-bounded by the broker whitelist)."""
        budget = int(budget_s)
        return self.call("gateway-restart",
                         {"component": component, "budget_s": budget},
                         timeout_s=min(budget + 15, 180))

    def host_evidence(self, check: str) -> BrokerResult:
        """Restricted READ-ONLY host evidence verb (limits/faults): the
        broker-side WindowsHostOps answers with its SYSTEM-privilege verdict
        (True/False/None or a bounded fault-tail dict); the check enum is
        validated by the protocol whitelist and the object stays broker-side
        deployment configuration."""
        return self.call("host-evidence", {"check": check})

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
