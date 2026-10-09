r"""share_broker_host.py — REAL broker host coordination (product logic).

This module owns the coordination business logic that previously lived in
the test fixture (R29-1/2/3): replay consumption on BOTH channels, trusted
instance registration, the writer→load→ACK publication chain, bounded drain
with a real final stop, and emergency stop with verified closure. Fixtures
only substitute transports, OS actions and the clock — they never host the
business rules.

Privilege separation stays physical: management actions (publish/start/
drain/emergency-stop/status) run on the broker host; the share instance can
only answer instance-channel requests. The trusted CURRENT instance comes
from an explicit registration (deployment/instance registry; the real OS
identity check is D4, but the registration call and its rejection branches
are testable offline). A stale or forged ACK can never pass by being a
non-empty string.
"""
from __future__ import annotations

import collections
import inspect
import threading
import time
import uuid

from share_broker_protocol import (BrokerResult, ProtocolError,
                                   ReplayRegistry,
                                   DRAIN_WINDOW_SECONDS,
                                   STOP_BUDGET_SECONDS,
                                   GATEWAY_RESTART_ACTION,
                                   MAINTENANCE_BEGIN_ACTION,
                                   MAINTENANCE_END_ACTION,
                                   MAINTENANCE_STATUS_ACTION,
                                   RESULT_CODES,
                                   parse_projection_payload,
                                   validate_gateway_restart_fields,
                                   validate_instance_request,
                                   validate_maintenance_begin_fields,
                                   validate_maintenance_end_fields,
                                   validate_management_request)
from share_runtime import BrokerError


def idle_maintenance_record(last_outcome=None) -> dict:
    """The fail-safe maintenance record (schema §11.1): no lease, no restart
    window, no recorded outcome. ``last_outcome`` is NEVER cleared by a later
    success - it is only ever replaced by a NEW failed outcome (恢复不擦除失败)."""
    return {"schema_version": 1, "state": "idle", "component": None,
            "lease_id": None, "instance_id": None, "round": None,
            "created_at": 0, "expires_at": None, "by": "startup",
            "last_outcome": last_outcome}


def _valid_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _instance_error_code(resp):
    code = resp.get("error_code")
    return code if code in RESULT_CODES else "unconfirmed"


class MaintenanceLeaseStore:
    """The ONE in-process maintenance lease slot (single lease, wall clock).

    The lease is a MAINTENANCE TRANSIENT: it is deliberately NOT the
    lifecycle record (``lifecycle.json`` keeps the broker's protected
    running/stopped authority) and it never survives as a stop - a confirmed
    drain/emergency-stop clears it, and a stopped instance can never be
    re-opened by it. Timestamps are wall-clock epochs because they are
    persisted and read by the watchdog; ``clock`` is injectable for offline
    tests only.

    ``begin`` returns the LIVE stored dict (callers annotate it with the
    observed active count); ``current`` returns a copy so a reader can never
    mutate the stored lease by accident.
    """

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lease = None

    def now(self) -> int:
        return int(self._clock())

    def current(self) -> dict | None:
        return dict(self._lease) if self._lease is not None else None

    def begin(self, component: str, instance_id: str, round_: int,
              lease_s: int) -> dict:
        lease_id = uuid.uuid4().hex
        created = self.now()
        lease = {"lease_id": lease_id, "component": component,
                 "instance_id": instance_id, "round": int(round_),
                 "created_at": created,
                 "expires_at": created + int(lease_s),
                 "active_count_at_begin": None}
        self._lease = lease
        return lease

    def end(self, component: str, lease_id: str, now=None) -> tuple:
        """Consume the lease ONLY when component + lease_id match and the
        lease is still unexpired. -> (ok, lease): on refusal the CURRENT
        lease is returned (the caller reports it honestly); a missing lease
        returns (False, None)."""
        lease = self._lease
        if lease is None:
            return False, None
        if lease["component"] != component or lease["lease_id"] != lease_id:
            return False, dict(lease)
        current = int(now if now is not None else self._clock())
        if current >= lease["expires_at"]:
            return False, dict(lease)
        self._lease = None
        return True, dict(lease)

    def expire_due(self, now=None) -> dict | None:
        """Reclaim the lease ONLY when it is due. The caller enforces the
        second half of the binding rule - the bound instance/round must still
        be the CURRENT trusted instance - before it sends the resume; this
        store has no registry access by contract."""
        lease = self._lease
        if lease is None:
            return None
        current = int(now if now is not None else self._clock())
        if current < lease["expires_at"]:
            return None
        self._lease = None
        return dict(lease)

    def clear(self, reason: str) -> None:
        """Drop the current lease (stop-priority drain/emergency-stop, or a
        completed resume). ``reason`` is diagnostic only."""
        self._lease = None

    def restore(self, record) -> bool:
        """Startup recovery: re-adopt a persisted LEASED record so a broker
        restart does not silently forget an in-force lease (the lazy expiry and
        maintenance-end then still bind to it). A record that is not a
        complete unexpired lease is refused."""
        if not isinstance(record, dict) or record.get("state") != "leased":
            return False
        lease_id = record.get("lease_id")
        instance_id = record.get("instance_id")
        round_ = record.get("round")
        expires_at = record.get("expires_at")
        if not isinstance(lease_id, str) or not lease_id:
            return False
        if not isinstance(instance_id, str) or not instance_id:
            return False
        if not _valid_int(round_) or not _valid_int(expires_at):
            return False
        self._lease = {"lease_id": lease_id,
                       "component": record.get("component"),
                       "instance_id": instance_id,
                       "round": int(round_),
                       "created_at": int(record.get("created_at") or 0),
                       "expires_at": int(expires_at),
                       "active_count_at_begin":
                           record.get("active_count_at_begin")}
        return True


class BrokerDiag:
    """Bounded, SANITIZED diagnostic ring for the instance-channel trust
    chain (maintainer direction 2026-09-17). Each event records the STAGE,
    ok flag, error_code, exception TYPE and a short NON-SECRET detail plus
    the correlation id when the caller carries one. Secrets, signature
    material, credentials and full payloads are never recorded — callers
    pass reason CODES and exception class names only. The ring is exposed
    EXCLUSIVELY through the management ``status`` reply (the protected
    local management boundary; never on the instance channel)."""

    def __init__(self, maxlen=24, clock=time.time):
        self._lock = threading.Lock()
        self._events = collections.deque(maxlen=maxlen)
        self._clock = clock

    def record(self, stage, ok=None, error_code=None, exception_type=None,
               detail=None, instance_id=None, request_id=None):
        entry = {
            "stage": str(stage)[:24],
            "ok": ok,
            "error_code": (str(error_code)[:40] if error_code else None),
            "exception_type": (str(exception_type)[:40]
                               if exception_type else None),
            "detail": (str(detail)[:120] if detail else None),
            "instance_id": (str(instance_id)[:64] if instance_id else None),
            "request_id": (str(request_id)[:64] if request_id else None),
            "ts": round(self._clock(), 3),
        }
        with self._lock:
            self._events.append(entry)

    def snapshot(self):
        with self._lock:
            return list(self._events)


class InstanceRegistry:
    """Trusted registry of the CURRENT share instance with a MONOTONIC
    registration version (R31-3): every register() bumps the version, so an
    ACK consumed after a re-registration can be detected as belonging to a
    replaced instance - the consumer snapshots the version BEFORE the load
    and the ACK is accepted only if the version is UNCHANGED (atomic
    through the registry lock). Registration is explicit; the real OS
    identity verification is D4, but offline tests exercise registration,
    replacement and rejection."""

    def __init__(self):
        self._lock = threading.Lock()
        self._current = None
        self._version = 0

    def register(self, instance_id: str):
        if not isinstance(instance_id, str) or not instance_id:
            raise ValueError("instance_id must be a non-empty string")
        with self._lock:
            self._current = instance_id
            self._version += 1

    def snapshot(self):
        """(instance_id, version) atomically - the ACK consumer compares
        the version, not just a string captured earlier."""
        with self._lock:
            return self._current, self._version

    def current(self):
        with self._lock:
            return self._current

    def is_current(self, instance_id):
        with self._lock:
            return instance_id is not None and instance_id == self._current


class HostOps:
    """Host action interface (start/stop/verify) the coordinator drives for
    the OS-facing side of start/drain/emergency. The real Windows
    implementation (precise process identity + listener verification) is
    D4; offline fixtures inject observable fake state. The coordinator owns
    the ORDER and the confirmation contract — fixtures never own it."""

    def start_host(self) -> bool:      # pragma: no cover - abstract
        raise NotImplementedError

    def stop_host(self) -> bool:       # pragma: no cover - abstract
        raise NotImplementedError

    def verify_running(self) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def verify_closed(self) -> bool:   # pragma: no cover - abstract
        raise NotImplementedError


class BrokerHostCoordinator:
    """Management-channel request handler with ALL product coordination.

    Injected dependencies replace the OS and the wire; every branch below is
    product logic and runs identically under offline fixtures.
    """

    def __init__(self, writer, mgmt_secret, instance_secret, instances,
                 replay, host_ops, instance_call, clock=time.monotonic,
                 drain_timeout=DRAIN_WINDOW_SECONDS,
                 discover_instance=None, diag=None,
                 leases=None, maintenance=None, sleep=time.sleep,
                 start_ready_timeout=6.0, start_poll_interval=0.25,
                 stop_timeout=STOP_BUDGET_SECONDS):
        self.writer = writer
        self.mgmt_secret = mgmt_secret
        self.instance_secret = instance_secret
        self.instances = instances            # InstanceRegistry
        self.replay = replay                  # ReplayRegistry
        self.host_ops = host_ops              # HostOps
        self.instance_call = instance_call    # callable(action, fields, instance_id) -> dict
        self._clock = clock
        self._sleep = sleep
        self.drain_timeout = drain_timeout
        # The FINAL stop + closure verification draw on their OWN reserved
        # budget: the in-flight wait can spend the whole drain budget
        # (``timed_out``) and the stop must still run - reusing the spent
        # drain deadline used to SKIP the stop command entirely
        # (``timed_out=true, stopped=false``, zero stop calls).
        self.stop_timeout = stop_timeout
        # Bounded readiness confirmation for the restricted ``start`` verb:
        # the gateway process needs a moment to bind the share port after the
        # task starts, so the ONE ``start_host`` is followed by a readiness
        # poll on the injected clock instead of a single immediate check.
        # The budget stays strictly under the wire deadline
        # (share_broker_protocol.DEFAULT_DEADLINE_SECONDS = 10s) and under the
        # console's backend layer budget (console_share_actions
        # _BACKEND_DEFAULT_S = 30s), so a late readiness can never arrive
        # after the caller already gave up (no ownerless running instance).
        self.start_ready_timeout = start_ready_timeout
        self.start_poll_interval = start_poll_interval
        # Stop priority (§9.2) on the readiness wait: every VERIFIED closure
        # bumps this epoch, so a start whose readiness poll spans a concurrent
        # drain/emergency-stop can never report a late ``started=True`` over a
        # stop that already closed the host.
        self._closure_epoch = 0
        # R44-2: controlled discovery/refresh hook. The registration FILE
        # only supplies the CANDIDATE address; the trusted registry is
        # updated solely by on_verified AFTER the identity verifier accepted
        # the real pipe peer - a writable file can never register by itself.
        self.discover_instance = discover_instance
        # Sanitized stage diagnostics (maintainer direction 2026-09-17):
        # exposed via the management status reply only.
        self.diag = diag if diag is not None else BrokerDiag()
        # Temporary maintenance lease (A9-3/A9-4 broker side). The lease is
        # a TRANSIENT pause/drain, never the lifecycle authority; the store
        # is a single process-wide slot (the server layer persists the
        # protected record through the injected sink).
        self.leases = leases if leases is not None else MaintenanceLeaseStore()
        # sink(record|None): the server layer persists the protected
        # maintenance.json; best-effort (a failing sink never breaks the
        # management path - the in-process record stays authoritative).
        self.maintenance = maintenance
        self.maintenance_record = idle_maintenance_record()
        # Gateway-restart poll cadence between two bounded host evidence
        # reads (the budget itself is the wire-frozen ``budget_s``).
        self.gateway_poll_interval = 0.25

    # ------------------------------------------------------------ host ops
    def _instance_op(self, action, fields, instance_id, budget_s=None):
        """Call the instance channel, passing the caller's REMAINING budget when
        the seam accepts it (``budget_s``).

        Same narrow pattern as ``_host_op``: the seam surface stays fixed, the
        budget rides an OPTIONAL keyword, and a seam that does not accept it keeps
        its own per-exchange bound. The production ``make_instance_call`` consumes
        it so the WHOLE exchange (including its single transient retry) stays inside
        the phase's remaining budget.
        """
        if budget_s is None:
            return self.instance_call(action, fields, instance_id)
        try:
            parameters = inspect.signature(self.instance_call).parameters
        except (TypeError, ValueError):
            return self.instance_call(action, fields, instance_id)
        if "budget_s" in parameters:
            return self.instance_call(action, fields, instance_id,
                                     budget_s=budget_s)
        return self.instance_call(action, fields, instance_id)

    def _host_op(self, name, budget_s=None):
        """Call ONE host op, passing the caller's REMAINING budget when the
        seam accepts it.

        The HostOps surface itself stays FIXED (the same four verbs, no new
        command/service/path channel): the budget rides an OPTIONAL ``budget_s``
        keyword. A seam that does not accept it keeps its own command bound
        (the previous behaviour), while the production ``WindowsHostOps`` uses it
        to bound EVERY subprocess and to share ONE budget across the
        consecutive queries of a single evidence read - so a slow task run or a
        slow query can never make the whole verb outlive the caller's deadline.
        """
        method = getattr(self.host_ops, name)
        if budget_s is None:
            return method()
        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):        # builtins / exotic callables
            return method()
        if "budget_s" in parameters:
            return method(budget_s=budget_s)
        return method()

    # ------------------------------------------------------------ mgmt channel
    def handle_management(self, body, now=None):
        try:
            req = validate_management_request(body, self.mgmt_secret, now=now)
        except ProtocolError as exc:
            frame = BrokerResult.err(exc.code, str(exc)).to_dict()
            frame["request_id"] = (body.get("request_id")
                                   if isinstance(body, dict) else None)
            return frame
        try:
            # One-time consumption BEFORE any side effect (R29-3).
            self.replay.consume(req["request_id"], req["ts"], now=now)
        except ProtocolError as exc:
            return self._reply(req, BrokerResult.err(exc.code, str(exc)))
        # Lazy lease expiry: AFTER replay consumption, BEFORE the action
        # branch - an expired lease gets ONE bounded resume attempt (only
        # while the bound instance/round is still current); a failed resume
        # keeps the lease and records the outcome, so the next request retries.
        self._expire_maintenance_due()
        action = req["action"]
        if action == "status":
            result = dict(self.host_ops.status())
            # Diagnostics ride ONLY the management status reply (the
            # protected local boundary); the instance channel never sees it.
            result["diag"] = self.diag.snapshot()
            return self._reply(req, BrokerResult.ok(result))
        if action == "publish":
            return self._reply(req, self._publish(req["fields"],
                                                  request_id=req["request_id"]))
        if action == "start":
            return self._reply(req, self._start())
        if action == "drain":
            return self._reply(req, self._drain())
        if action == "emergency-stop":
            return self._reply(req, self._emergency_stop())
        if action == MAINTENANCE_BEGIN_ACTION:
            return self._reply(req, self._maintenance_begin(
                req["fields"], request_id=req["request_id"]))
        if action == MAINTENANCE_END_ACTION:
            return self._reply(req, self._maintenance_end(
                req["fields"], request_id=req["request_id"]))
        if action == MAINTENANCE_STATUS_ACTION:
            return self._reply(req, self._maintenance_status())
        if action == GATEWAY_RESTART_ACTION:
            return self._reply(req, self._gateway_restart(
                req["fields"], request_id=req["request_id"]))
        return self._reply(req, BrokerResult.err("bad_request",
                                                 "unknown action"))

    @staticmethod
    def _reply(req, result):
        frame = result.to_dict()
        frame["request_id"] = req["request_id"]
        return frame

    # ------------------------------------------------------------ discovery
    def _try_discovery(self):
        """Run ONE controlled discovery attempt; a failure stays "not
        ready" and never fabricates a trusted instance."""
        if self.discover_instance is None:
            return False
        try:
            self.discover_instance()
        except Exception:  # noqa: BLE001
            return False
        return True

    def _resolve_instance(self, request_id=None):
        """Trusted current instance with ONE discovery attempt when the
        registry is empty (R44-2: the broker may start BEFORE the share
        writes its registration; a later registration is discovered here
        without restarting the broker)."""
        current, reg_version = self.instances.snapshot()
        if current is None:
            attempted = self._try_discovery()
            current, reg_version = self.instances.snapshot()
            self.diag.record("resolve", ok=current is not None,
                             error_code=(None if current is not None
                                         else "no-registered-instance"),
                             detail=("trusted after discovery" if attempted
                                     else "discovery failed or unwired"),
                             instance_id=current, request_id=request_id)
        return current, reg_version

    def _refresh_discovery(self):
        """Discovery attempt after an instance_call failure; True only when
        the trusted registry actually CHANGED (a verified new candidate) -
        the retry then targets the refreshed instance with a fresh version
        snapshot (the R31-3 ACK gate stays intact)."""
        current, version = self.instances.snapshot()
        self._try_discovery()
        current_after, version_after = self.instances.snapshot()
        return current_after != current or version_after != version

    # ------------------------------------------------------------ actions
    def _publish(self, fields, request_id=None):
        """writer -> load -> ACK coordination (R29-3/R31-3/R44-2): full
        semantic parsing, fingerprint check, atomic replace, discovery of
        the trusted current instance (empty registry or post-failure
        refresh), load of that instance, ACK bound to generation/fingerprint/
        instance AND to the registration version snapshotted BEFORE the
        load - an ACK from an instance replaced while the load was in
        flight NEVER passes. An instance IPC failure is a failed
        publication."""
        projection = fields["projection"]
        generation = fields["generation"]
        fingerprint = fields["fingerprint"]
        try:
            parse_projection_payload(projection)
        except ProtocolError as exc:
            return BrokerResult.err("bad_request", str(exc))
        actual_fp = self.writer.fingerprint(projection)
        if actual_fp != fingerprint:
            return BrokerResult.err("fingerprint_mismatch",
                                    "projection fingerprint mismatch")
        try:
            self.writer.write(projection, generation, fingerprint)
        except BrokerError as exc:
            return BrokerResult.err("internal_error", str(exc))
        load_fields = {"generation": generation, "fingerprint": fingerprint}
        terminate_keys = fields.get("terminate_keys")
        if terminate_keys:
            # R8-A7 revoke-and-terminate: forward the (protocol-validated)
            # one-shot list to the instance load; never persisted beyond
            # this publication.
            load_fields["terminate_keys"] = terminate_keys
        current, reg_version = self._resolve_instance(request_id=request_id)
        if current is None:
            return BrokerResult.err("unconfirmed", "no registered instance")
        try:
            resp = self.instance_call("load", load_fields, current)
        except Exception as exc:  # noqa: BLE001
            self.diag.record("load", ok=False, exception_type=type(exc).__name__,
                             detail=str(exc)[:120], instance_id=current,
                             request_id=request_id)
            # The instance may have been REPLACED (A->B restart): one
            # bounded discovery refresh reads the current candidate and the
            # verified connection registers it; without a registry change
            # the failure stands (unconfirmed, never a pretend success).
            if not self._refresh_discovery():
                return BrokerResult.err("unconfirmed",
                                        "instance load failed")
            current, reg_version = self.instances.snapshot()
            if current is None:
                return BrokerResult.err(
                    "unconfirmed", "no registered instance after refresh")
            try:
                resp = self.instance_call("load", load_fields, current)
            except Exception as exc:  # noqa: BLE001
                self.diag.record("load", ok=False,
                                 exception_type=type(exc).__name__,
                                 detail=str(exc)[:120], instance_id=current,
                                 request_id=request_id)
                return BrokerResult.err("unconfirmed",
                                        f"instance load failed: {exc}")
        self.diag.record("load", ok=bool(resp.get("ok")),
                         error_code=resp.get("error_code"),
                         instance_id=current, request_id=request_id)
        result = resp.get("result") or {}
        if not resp.get("ok"):
            return BrokerResult.err(
                resp.get("error_code", "unconfirmed"),
                result.get("detail", ""))
        # R31-3: the ACK is accepted only if the REGISTRATION is unchanged
        # (same instance AND same registration version) - a re-registration
        # while the load was in flight invalidates the in-flight ACK.
        current_now, version_now = self.instances.snapshot()
        if version_now != reg_version or current_now != current:
            return BrokerResult.err("instance_mismatch",
                                    "instance was replaced while the load "
                                    "was in flight")
        if result.get("instance_id") != current_now:
            return BrokerResult.err("instance_mismatch",
                                    "ACK from a non-current instance")
        if result.get("generation") != generation \
                or result.get("fingerprint") != fingerprint:
            return BrokerResult.err("fingerprint_mismatch",
                                    "ACK content differs from publication")
        return BrokerResult.ok(result)

    def _start(self):
        """Restricted start with a BOUNDED readiness confirmation.

        The task is run ONCE (``start_host``); the gateway process then needs a
        moment to bind the share port, so readiness is confirmed by polling the
        SAME three-evidence check (task + share process + listener) on the
        injected clock. The deadline covers the WHOLE verb - the task run and every
        readiness probe count against it - so a slow task run or a slow evidence
        query can never produce a ``started`` answer after the caller's exchange
        deadline (a late success would leave an ownerless running instance).
        ``None`` (unreadable evidence) is never ready, the task is never re-run
        and nothing is force-killed here.
        """
        deadline = self._clock() + self.start_ready_timeout
        result = self._host_op("start_host", self.start_ready_timeout)
        if result is not True:
            return BrokerResult.err("unconfirmed",
                                    "start did not confirm the host task")
        if self._clock() >= deadline:
            return BrokerResult.err(
                "unconfirmed",
                "start consumed the whole readiness budget before the host "
                "could be verified")
        epoch = self._closure_epoch
        running = self._host_op("verify_running", deadline - self._clock())
        while running is not True and self._clock() < deadline:
            remaining = deadline - self._clock()
            self._sleep(min(self.start_poll_interval, remaining))
            if self._clock() >= deadline:
                break
            running = self._host_op("verify_running", deadline - self._clock())
        if running is not True:
            return BrokerResult.err("unconfirmed",
                                    "start did not verify a running host")
        if self._clock() >= deadline:
            return BrokerResult.err(
                "unconfirmed",
                "readiness was confirmed only after the readiness budget "
                "expired")
        if self._closure_epoch != epoch:
            return BrokerResult.err(
                "unconfirmed",
                "a verified stop landed while the start waited for readiness")
        return BrokerResult.ok({"started": True})

    def _drain(self):
        """Bounded drain with a REAL final stop (R31-1): refuse new, wait
        for in-flight until the deadline, then STOP the instance and verify
        closure. An instance-notification failure does NOT skip the host
        stop/verify - a lost share is exactly when the host must step in.
        The result distinguishes notification failure, VERIFIED closure and
        unconfirmed closure; drained/timed_out never substitute for the
        stop confirmation. The final stop + closure verification draw on the
        coordinator's OWN reserved ``stop_timeout`` budget: the in-flight wait
        can spend the whole drain window and the stop still runs."""
        notification_failed = False
        active_left = None
        # R-WIRE-DRAIN: the WHOLE drain phase (the notification AND every
        # status query) is bounded by the drain window - the LAST query can no
        # longer cross the deadline with a full per-exchange budget.
        deadline = self._clock() + self.drain_timeout
        try:
            resp = self._instance_op("drain", {}, self.instances.current(),
                                     deadline - self._clock())
            if not resp.get("ok"):
                notification_failed = True
        except Exception:  # noqa: BLE001
            notification_failed = True
        while self._clock() < deadline:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            try:
                resp = self._instance_op("instance-status", {},
                                         self.instances.current(), remaining)
                active_left = (resp.get("result") or {}).get("active_count")
                if active_left == 0:
                    break
            except Exception:  # noqa: BLE001
                notification_failed = True
                break
            time.sleep(min(0.05, max(0.0, deadline - self._clock())))
        timed_out = active_left != 0
        # Notification can use at most half of the stop phase; a lost instance
        # must leave time for the authoritative host stop and closure evidence.
        stop_deadline = self._clock() + self.stop_timeout
        try:
            stop = self._instance_op(
                "emergency", {}, self.instances.current(),
                min(self.stop_timeout / 2, stop_deadline - self._clock()))
            if not stop.get("ok"):
                notification_failed = True
        except Exception:  # noqa: BLE001
            notification_failed = True
        host_stopped = self._host_op("stop_host",
                                     stop_deadline - self._clock()) is True
        closed = host_stopped and self._host_op(
            "verify_closed", stop_deadline - self._clock()) is True
        # Stop priority (§9.2): a VERIFIED closure clears any maintenance
        # lease - the stop wins, the lease never survives as a stop, and the
        # protected record keeps the clearing verb in ``by``. The closure
        # epoch is bumped so a readiness wait spanning this stop can never
        # report a late started over it.
        if closed:
            self._closure_epoch += 1
            self._clear_lease_for_stop("drain")
        return BrokerResult.ok({"drained": not timed_out,
                                "timed_out": timed_out,
                                "stopped": closed,
                                "notification_failed": notification_failed})


    def _emergency_stop(self):
        """Emergency: refuse new + clear in-flight, stop the host, VERIFY
        closure. An instance-notification failure still drives the host
        stop/verify (R31-1); the VERIFIED host closure is the authoritative
        close evidence - a lost share is exactly when the host verification
        must be trusted. Instance stopped state is recorded but does not
        gate the confirmation. R-WIRE-DRAIN: the emergency notification, the
        host stop AND the closure verification all share the ONE reserved
        ``stop_timeout`` budget, so the WHOLE verb stays inside the caller's
        exchange deadline."""
        notification_failed = False
        cleared = None
        instance_stopped = False
        stop_deadline = self._clock() + self.stop_timeout
        try:
            stop = self._instance_op("emergency", {},
                                      self.instances.current(),
                                      min(self.stop_timeout / 2,
                                          stop_deadline - self._clock()))
            if not stop.get("ok"):
                notification_failed = True
            instance_stopped = (stop.get("ok") is True and
                                (stop.get("result") or {}).get("state")
                                == "stopped")
            cleared = (stop.get("result") or {}).get("cleared_in_flight")
        except Exception:  # noqa: BLE001
            notification_failed = True
        host_stopped = self._host_op("stop_host",
                                     stop_deadline - self._clock()) is True
        closed = host_stopped and self._host_op(
            "verify_closed", stop_deadline - self._clock()) is True
        confirmed = closed
        result = {"stopped": confirmed,
                  "notification_failed": notification_failed,
                  "host_closed": closed,
                  "instance_stopped": instance_stopped}
        if cleared is not None:
            result["cleared_in_flight"] = cleared
        # Stop priority (§9.2): a VERIFIED closure clears any maintenance
        # lease - the lease is a transient and can never re-open a stopped
        # instance; the protected record keeps the clearing verb. The closure
        # epoch is bumped so a readiness wait spanning this stop can never
        # report a late started over it.
        if confirmed:
            self._closure_epoch += 1
            self._clear_lease_for_stop("emergency-stop")
        return BrokerResult.ok(result)

    # ------------------------------------------------- maintenance lease
    def _clear_lease_for_stop(self, verb):
        """A VERIFIED host closure clears the lease (stop priority) and
        records the clearing verb in the protected record - never a stopped
        state, never a lifecycle write."""
        if self.leases.current() is None:
            return
        self.leases.clear(verb)
        self._publish_maintenance(
            state="idle", component=None, lease_id=None, instance_id=None,
            round=None, expires_at=None, by=verb)

    def adopt_maintenance_record(self, record):
        """Startup recovery (§11.1): re-adopt the persisted protected
        record loaded by the server layer so a lease that was in force before
        a broker restart keeps binding (maintenance-end / lazy expiry)."""
        if not isinstance(record, dict):
            return
        merged = dict(self.maintenance_record)
        for key, value in record.items():
            if key in merged:
                merged[key] = value
        self.maintenance_record = merged
        if merged.get("state") == "leased":
            self.leases.restore(merged)

    def _publish_maintenance(self, **overrides):
        record = dict(self.maintenance_record)
        record.update(overrides)
        self.maintenance_record = record
        if self.maintenance is not None:
            try:
                self.maintenance(dict(record))
            except Exception as exc:  # noqa: BLE001
                # Best-effort persistence: a failing sink is recorded and
                # never breaks the management path.
                self.diag.record("maintenance-sink", ok=False,
                                 exception_type=type(exc).__name__,
                                 detail="record not persisted")
        return record

    def _record_maintenance_outcome(self, action, ok, code, detail=""):
        """Record the LAST FAILED maintenance outcome; a success never
        clears an earlier failure (恢复不擦除失败)."""
        if ok:
            return
        self._publish_maintenance(last_outcome={
            "at": self.leases.now(), "action": action, "ok": False,
            "code": str(code)[:40], "detail": str(detail)[:200]})

    def _sync_maintenance_record(self, by):
        """Rewrite the protected record from the CURRENT lease (idle when no
        lease is in force) - used after a gateway-restart window closes."""
        lease = self.leases.current()
        if lease is None:
            return self._publish_maintenance(
                state="idle", component=None, lease_id=None,
                instance_id=None, round=None, expires_at=None, by=by)
        return self._publish_maintenance(
            state="leased", component=lease["component"],
            lease_id=lease["lease_id"], instance_id=lease["instance_id"],
            round=lease["round"], created_at=lease["created_at"],
            expires_at=lease["expires_at"], by=by)

    def _instance_status(self, instance_id):
        """(status_result, error_detail): ONE bounded instance-status read;
        any failure is an explicit unconfirmed, never a fabricated state."""
        try:
            resp = self.instance_call("instance-status", {}, instance_id)
        except Exception as exc:  # noqa: BLE001
            return None, f"instance status failed: {type(exc).__name__}"
        if not resp.get("ok"):
            return None, ((resp.get("result") or {}).get("detail")
                          or _instance_error_code(resp))
        return resp.get("result") or {}, None

    def _maintenance_begin(self, fields, request_id=None) -> BrokerResult:
        """A9-3 broker side: pause 8100 acceptance with ONE bounded
        lease bound to the CURRENT trusted instance + registration round.

        * no trusted instance / an unreadable instance state -> unconfirmed;
        * a STOPPED instance -> blocked (a terminal stop is never re-opened);
        * an already in-force lease -> blocked;
        * an instance that does not know the verb -> capability_missing.
        """
        try:
            clean = validate_maintenance_begin_fields(fields)
        except ProtocolError as exc:
            return BrokerResult.err("bad_request", str(exc))
        if self.leases.current() is not None:
            return BrokerResult.err("blocked",
                                    "maintenance lease already in force")
        component = clean["component"]
        lease_s = clean["lease_s"]
        current, reg_version = self._resolve_instance(request_id=request_id)
        if current is None:
            return BrokerResult.err("unconfirmed", "no registered instance")
        status, status_error = self._instance_status(current)
        if status is None:
            return BrokerResult.err("unconfirmed", status_error)
        active = status.get("active_count")
        if status.get("state") == "stopped":
            # Terminal stop guard: the lease NEVER re-opens a stopped share.
            return BrokerResult.err("blocked", "instance is stopped")
        try:
            drained = self.instance_call("drain", {}, current)
        except Exception as exc:  # noqa: BLE001
            return BrokerResult.err(
                "unconfirmed", f"instance drain failed: "
                              f"{type(exc).__name__}")
        if not drained.get("ok"):
            detail = (drained.get("result") or {}).get("detail", "")
            if drained.get("error_code") == "bad_request" \
                    and detail == "unknown action":
                return BrokerResult.err(
                    "capability_missing",
                    "instance does not support the maintenance drain")
            if drained.get("error_code") == "blocked":
                return BrokerResult.err("blocked", detail or
                                        "instance refused the maintenance drain")
            return BrokerResult.err(_instance_error_code(drained), detail)
        drain_result = drained.get("result") or {}
        if drain_result.get("state") != "draining":
            return BrokerResult.err("unconfirmed",
                                    "instance drain state unconfirmed")
        if not _valid_int(active):
            fallback = drain_result.get("active_count")
            active = fallback if _valid_int(fallback) else None
        lease = self.leases.begin(component, current, reg_version, lease_s)
        lease["active_count_at_begin"] = active
        self._publish_maintenance(
            state="leased", component=component, lease_id=lease["lease_id"],
            instance_id=current, round=reg_version,
            created_at=lease["created_at"], expires_at=lease["expires_at"],
            by="maintenance-begin")
        self.diag.record("maintenance-begin", ok=True, instance_id=current,
                         request_id=request_id)
        return BrokerResult.ok({
            "accepted": True, "component": component,
            "lease_id": lease["lease_id"],
            "lease_expires_at": lease["expires_at"],
            "instance_id": current, "round": reg_version,
            "state": "draining", "active_count": active})

    def _maintenance_end(self, fields, request_id=None) -> BrokerResult:
        """A9-3 broker side: resume acceptance for the CURRENT lease.

        Refused honestly (never a re-open attempt) when: no lease, an
        expired lease, a component/lease_id that does not match, or a
        changed instance/round (a late result must never re-open a replaced
        instance). A stopped instance answers ``blocked``; an old instance
        answers ``capability_missing``.
        """
        try:
            clean = validate_maintenance_end_fields(fields)
        except ProtocolError as exc:
            return BrokerResult.err("bad_request", str(exc))
        component = clean["component"]
        lease_id = clean["lease_id"]
        lease = self.leases.current()
        if lease is None:
            return BrokerResult.err("unconfirmed",
                                    "no maintenance lease in force")
        if lease["component"] != component or lease["lease_id"] != lease_id:
            return BrokerResult.err("instance_mismatch",
                                    "lease does not match")
        if self.leases.now() >= lease["expires_at"]:
            return BrokerResult.err("unconfirmed", "maintenance lease expired")
        current, version = self.instances.snapshot()
        if current is None or current != lease["instance_id"] \
                or version != lease["round"]:
            return BrokerResult.err("instance_mismatch",
                                    "instance or round changed")
        try:
            resp = self.instance_call("resume", {}, current)
        except Exception as exc:  # noqa: BLE001
            self._record_maintenance_outcome(
                "maintenance-end", False, "unconfirmed",
                f"instance resume failed: {type(exc).__name__}")
            return BrokerResult.err(
                "unconfirmed", f"instance resume failed: "
                              f"{type(exc).__name__}")
        if not resp.get("ok"):
            code = _instance_error_code(resp)
            detail = (resp.get("result") or {}).get("detail", "")
            if resp.get("error_code") == "bad_request" \
                    and detail == "unknown action":
                # The instance does not know resume: an EXPLICIT capability
                # refusal - never downgraded to a host stop/start.
                return BrokerResult.err(
                    "capability_missing",
                    "instance does not support resume")
            self._record_maintenance_outcome("maintenance-end", False,
                                            code, detail)
            if resp.get("error_code") == "blocked":
                return BrokerResult.err("blocked",
                                        detail or "instance is stopped")
            return BrokerResult.err(code, detail)
        result = resp.get("result") or {}
        ok, ended = self.leases.end(component, lease_id)
        if not ok or ended is None:
            return BrokerResult.err("instance_mismatch",
                                    "lease changed while resuming")
        self._publish_maintenance(
            state="idle", component=None, lease_id=None, instance_id=None,
            round=None, expires_at=None, by="maintenance-end")
        self.diag.record("maintenance-end", ok=True, instance_id=current,
                         request_id=request_id)
        return BrokerResult.ok({
            "resumed": True, "component": component, "lease_id": lease_id,
            "instance_id": result.get("instance_id", current),
            "round": version, "state": result.get("state") or "running",
            "active_count": result.get("active_count")})

    def _maintenance_status(self) -> BrokerResult:
        """Read-only maintenance view: the CURRENT lease, whether it is
        expired, the trusted instance state and the last failed outcome.
        Unavailable evidence is ``null`` - never fabricated."""
        now = self.leases.now()
        lease = self.leases.current()
        instance = None
        current, _version = self.instances.snapshot()
        if current is not None:
            status, _error = self._instance_status(current)
            if status is not None:
                instance = {"instance_id": status.get("instance_id", current),
                            "state": status.get("state"),
                            "active_count": status.get("active_count"),
                            "last_seen_generation":
                                status.get("last_seen_generation")}
        return BrokerResult.ok({
            "checked_at": now,
            "lease": lease,
            "lease_expired": bool(lease is not None
                                    and now >= lease["expires_at"]),
            "instance": instance,
            "last_outcome": self.maintenance_record.get("last_outcome")})

    def _expire_maintenance_due(self):
        """Lazy lease expiry (schema §11.1): AFTER replay consumption and
        BEFORE the action branch, an expired lease gets ONE bounded resume
        attempt - only while the bound instance/round is still current (a
        replaced instance is never resumed). A failed resume keeps the lease
        and records the outcome; the next request retries."""
        lease = self.leases.current()
        if lease is None:
            return
        now = self.leases.now()
        if now < lease["expires_at"]:
            return
        current, version = self.instances.snapshot()
        if current is None or current != lease["instance_id"] \
                or version != lease["round"]:
            # The bound instance/round changed: the lease is NOT reclaimed
            # here and no resume is ever sent to a replaced instance.
            return
        try:
            resp = self.instance_call("resume", {}, current)
        except Exception as exc:  # noqa: BLE001
            self._record_maintenance_outcome(
                "maintenance-end", False, "unconfirmed",
                f"expiry resume failed: {type(exc).__name__}")
            return
        if not resp.get("ok"):
            self._record_maintenance_outcome(
                "maintenance-end", False, _instance_error_code(resp),
                (resp.get("result") or {}).get("detail", ""))
            return
        self.leases.expire_due(now)
        self._publish_maintenance(
            state="idle", component=None, lease_id=None, instance_id=None,
            round=None, expires_at=None, by="expiry")
        self.diag.record("maintenance-expiry", ok=True, instance_id=current)

    def _gateway_restart(self, fields, request_id=None) -> BrokerResult:
        """A9-4 broker side: ONE bounded 8100 single-layer restart.

        The THREE host proofs gate the action (False/None -> blocked, the
        restart never doubles as a way to start a stopped gateway); the
        protected record is written BEFORE the stop so the watchdog does not
        race the maintenance window; a stop/start that is not confirmed is
        reported as ``unconfirmed`` with the actual flags and never retried
        with a force-kill.
        """
        try:
            clean = validate_gateway_restart_fields(fields)
        except ProtocolError as exc:
            return BrokerResult.err("bad_request", str(exc))
        component = clean["component"]
        budget_s = clean["budget_s"]
        started = self._clock()
        if self.host_ops.verify_running() is not True:
            return BrokerResult.err(
                "blocked", "gateway identity unconfirmed or stopped")
        now = self.leases.now()
        lease_before = self.leases.current()
        self._publish_maintenance(
            state="restarting", component=component,
            lease_id=(lease_before or {}).get("lease_id"),
            instance_id=(lease_before or {}).get("instance_id"),
            round=(lease_before or {}).get("round"),
            created_at=now, expires_at=now + budget_s + 30,
            by="gateway-restart")
        stopped_confirmed = False
        try:
            self.host_ops.stop_host()
            deadline = self._clock() + budget_s
            while self._clock() < deadline:
                if self.host_ops.verify_closed() is True:
                    stopped_confirmed = True
                    break
                time.sleep(self.gateway_poll_interval)
        except Exception:  # noqa: BLE001
            stopped_confirmed = False
        running_confirmed = False
        if stopped_confirmed:
            try:
                self.host_ops.start_host()
                deadline = self._clock() + budget_s
                while self._clock() < deadline:
                    if self.host_ops.verify_running() is True:
                        running_confirmed = True
                        break
                    time.sleep(self.gateway_poll_interval)
            except Exception:  # noqa: BLE001
                running_confirmed = False
        restarted = stopped_confirmed and running_confirmed
        elapsed = round(self._clock() - started, 3)
        result = {"restarted": restarted, "component": component,
                  "stopped_confirmed": stopped_confirmed,
                  "running_confirmed": running_confirmed,
                  "elapsed_s": elapsed,
                  "maintenance_state": self.maintenance_record["state"]}
        self.diag.record("gateway-restart", ok=restarted,
                         error_code=None if restarted else "unconfirmed",
                         detail=("stop=%s running=%s" % (stopped_confirmed,
                                                          running_confirmed)),
                         request_id=request_id)
        if restarted:
            # Success: the record returns to the CURRENT lease (if one is
            # still in force) or to idle; an earlier failure is retained.
            self._sync_maintenance_record("gateway-restart")
            result["maintenance_state"] = self.maintenance_record["state"]
            return BrokerResult.ok(result)
        if not stopped_confirmed:
            # The stop was never confirmed: clear the restart window (the
            # gateway may still be running) and report the failure.
            self._sync_maintenance_record("gateway-restart")
        # A start that was not confirmed keeps the record (its own
        # expires_at is bounded; the watchdog takes over afterwards).
        self._record_maintenance_outcome(
            "gateway-restart", False, "unconfirmed",
            "stopped_confirmed=%s running_confirmed=%s"
            % (stopped_confirmed, running_confirmed))
        result["maintenance_state"] = self.maintenance_record["state"]
        return BrokerResult(False, "unconfirmed", result)
