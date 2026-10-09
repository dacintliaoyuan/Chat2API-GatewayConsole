# -*- coding: utf-8 -*-
"""ai-gateway/share_backend_supervision.py - 8110 bounded crash recovery.

Independent of 8096 (the console) and of 8110 (the dedicated backend): this
module makes NO side effect beyond the decision logic (read-only intent and
evidence reads) plus the ONE shared recovery handler
``_ProductionBackendOps.start()`` (contract Rev9 A9-5, section 7). It NEVER
constructs an OperationManager - a second manager over the shared SQLite marks
the 8096 in-flight operations interrupted at construction
(console_operations.py :243, :306-314; regression pin
tests/test_console_operations.py:254-281), so this process would corrupt the
console's state just by starting.

Import discipline (contract section 7.1): module-level imports are stdlib plus
``console_operations`` (``ResourceFileLock``/``LockBusy``) ONLY. Every production
binding (``console_share_production``, ``usagesrv``) is imported lazily inside a
function, exactly like ``console_share_production._default_broker`` does, so
importing this module has no network/install/write/process-spawn side effect and
the console action layer never imports it (the ONLY wiring point is
``console_share_production.production_seams`` -> ``_INTENT_WRITE``).

Path discipline: only the fixed file names of this deployment are ever read or
written (``share-backend-desired.json``, ``share-backend-watch-state.json``,
``share-backend-watch.log``, ``share-backend.log``, ``backend-lifecycle.log``).
Every path is normalized, ``..`` components are refused and the base name is
re-checked against the expected fixed name before any I/O, the same fail-closed
discipline as ``_ProductionBackendOps._instance_path``
(console_share_production.py:1293-1303). Truncating writes go through
``os.open`` with explicit flags and mode 0o600, never a builtin truncating
open with a text mode.

Decision discipline (contract section 7.2), in order, ONE function for both
``--check`` and ``--apply``:

1. desired intent - running, written during THIS boot;
2. no unexpired maintenance lease (an expired lease gets ONE bounded
   ``maintenance-end`` and a re-read);
3. the broker lifecycle record is not ``stopped`` (a human close is never
   auto-undone);
4. no graceful 8110 exit newer than the intent (and ``unknown`` exit evidence
   with the instance gone refuses - insufficient evidence is never read as a
   crash);
5. the instance is either alive-and-verified (no action) or confirmed absent with
   no unknown port occupant;
6. two consecutive candidate observations (FAILURE_THRESHOLD);
7. the hourly budget (MAX_STARTS_PER_HOUR); the attempt timestamp is
   persisted BEFORE ``start()`` so a crash cannot lose the consumed budget;
8. the SAME ``share:orchestration`` ResourceFileLock the console actions use
   (non-reentrant: a held console lock means "busy, do nothing");
9. re-check 1-7 inside the lock, then call the shared handler.

The decision log is one bounded JSON line per applied run (the same record is
returned, stored as ``last_decision`` and used by the PowerShell entry for the
exit code). ``--check`` is read-only: it decides but writes nothing and never
starts anything.
"""

import json
import os
import re
import subprocess
import sys
import time

from console_operations import LockBusy, ResourceFileLock

STATE_VERSION = 1
DEFAULT_WATCH_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                 "AIGatewayPublic", "watch")
DESIRED_FILE_NAME = "share-backend-desired.json"
STATE_FILE_NAME = "share-backend-watch-state.json"
LOG_FILE_NAME = "share-backend-watch.log"
SERVICE_LOG_NAME = "share-backend.log"
LIFECYCLE_LOG_NAME = "backend-lifecycle.log"
DEFAULT_DESIRED_PATH = os.path.join(DEFAULT_WATCH_DIR, DESIRED_FILE_NAME)
DEFAULT_STATE_PATH = os.path.join(DEFAULT_WATCH_DIR, STATE_FILE_NAME)
DEFAULT_LOG_PATH = os.path.join(DEFAULT_WATCH_DIR, LOG_FILE_NAME)
LOCK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "console-locks")
LOCK_RESOURCE = "share:orchestration"      # the console actions' own lock
FAILURE_THRESHOLD = 2                      # two confirmations
MAX_STARTS_PER_HOUR = 3                    # at most 3 starts per hour
LOCK_TIMEOUT_S = 3.0
LOG_MAX_BYTES = 1024 * 1024
BOOT_QUERY_TIMEOUT_S = 10.0
EXIT_ALIVE = 0        # no action (satisfied / no intent / refused, no act)
EXIT_REFUSED = 1      # refused: precondition, evidence, budget or lock busy
EXIT_UNCONFIRMED = 2  # a start was attempted but is not confirmed

DESIRED_KEY = "share_backend"
DESIRED_STATES = ("running", "stopped")
BACKEND_PORT = 8110
LOG_TAIL_BYTES = 256 * 1024
VERIFY_BUDGET_S = 30.0
VERIFY_POLL_INTERVAL_S = 0.5
WATCH_OWNER = "share-backend-watch"

# Stable decision codes (the record's ``code`` field). ``instance_alive``,
# ``awaiting_confirmation`` and ``would_start`` are benign no-ops; every other
# code is a refusal with its own reason (contract section 7.2). ``would_start``
# only appears in ``--check`` mode (nothing is executed).
_BENIGN_CODES = frozenset(("instance_alive", "awaiting_confirmation",
                          "would_start"))

# Test-only injection seams (module-level, default None = production). The
# frozen function signatures stay unchanged; the offline self-test and the unit
# tests set these to control the boot identity, the broker lifecycle record and
# the timezone the naive local log stamps are read in, without reading or
# writing any production path.
_BOOT_ID_READER = None        # () -> str|None
_DESIRED_STOP_READER = None    # () -> dict|None (broker-status.json lifecycle)
_LOCAL_TZ = None               # tzinfo for naive log stamps; None = system local


def _bounded(text, limit=200):
    """One-line bounded text for details/messages (contract section 3.1)."""
    return " ".join(str(text).split())[:limit]


def _utc_iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(
        epoch, tz=datetime.timezone.utc).isoformat()


def _parse_epoch(text):
    """ISO-8601 text -> epoch float, or None (never a guessed time)."""
    import datetime
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        stamp = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp.timestamp()


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _write_text_file(target, text):
    """Truncating write of an already validated fixed-name path.

    The builtin truncating text open is deliberately NOT used here: the write
    goes through ``os.open`` with explicit flags and mode 0o600, and an OSError
    propagates to the caller's fail-safe branch. The path guard stays
    :func:`_fixed_name_path` - only the deployment's fixed file names inside
    its own directory are ever written.
    """
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        data = text.encode("utf-8")
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        os.fsync(fd)
    finally:
        os.close(fd)


def _fixed_name_path(path, name):
    """Normalize + containment-check a fixed-name state file path.

    The supervisor only ever touches the fixed file names of this deployment,
    so a damaged/foreign path must be refused BEFORE any I/O: ``..``
    components raise and the base name must be the expected one (the same
    fail-closed discipline as _ProductionBackendOps._instance_path,
    console_share_production.py:1293-1303). The returned path is absolute
    and normalized.
    """
    raw = str(path)
    if not raw:
        raise ValueError("path must not be empty")
    if ".." in re.split(r"[\\/]+", raw):
        raise ValueError("path must not contain '..'")
    target = os.path.normpath(os.path.abspath(raw))
    if os.path.basename(target) != name:
        raise ValueError("path must name %r" % name)
    return target


def _watch_paths(state_dir=None):
    """``(base, desired, state, log)`` for the watch directory.

    The three files are fixed names directly inside the resolved base dir;
    the fixed-name check guarantees a hostile ``state_dir`` can never make the
    supervisor touch a file outside its own watch directory.
    """
    base = os.path.normpath(os.path.abspath(state_dir or DEFAULT_WATCH_DIR))
    desired = _fixed_name_path(os.path.join(base, DESIRED_FILE_NAME),
                              DESIRED_FILE_NAME)
    state = _fixed_name_path(os.path.join(base, STATE_FILE_NAME),
                             STATE_FILE_NAME)
    log = _fixed_name_path(os.path.join(base, LOG_FILE_NAME), LOG_FILE_NAME)
    return base, desired, state, log


def current_boot_id():
    """Stable per-boot identity ``"<COMPUTERNAME>|<LastBootUpTime-UTC-ISO>"``.

    The ONE implementation: the writer and every reader share this function.
    The bounded CIM query runs through an argument list (never a shell) with a
    10 second timeout; an unreadable boot time returns None and callers REFUSE
    (``boot_identity_unavailable``) - it is never faked.
    """
    if _BOOT_ID_READER is not None:
        try:
            value = _BOOT_ID_READER()
        except Exception:  # noqa: BLE001 - an injected reader stays honest
            return None
        return value if isinstance(value, str) and value else None
    command = ("(Get-CimInstance Win32_OperatingSystem)."
               "LastBootUpTime.ToUniversalTime().ToString('o')")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, timeout=BOOT_QUERY_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = out.stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    return "%s|%s" % (os.environ.get("COMPUTERNAME", ""), text)


def read_desired_state(path=None):
    """The share runtime intent (``{version, key, state, at_utc, boot_id,
    source, writer_pid}``), or None.

    Absent, malformed, unknown state, unknown version/key or a missing boot_id
    all read as None: the caller then refuses ``desired_state_unknown`` - a
    foreign or damaged marker is never read as an intent (fail-closed).
    """
    target = _fixed_name_path(path, DESIRED_FILE_NAME) if path \
        else DEFAULT_DESIRED_PATH
    try:
        with open(target, encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("version") != STATE_VERSION:
        return None
    if record.get("key") != DESIRED_KEY:
        return None
    if record.get("state") not in DESIRED_STATES:
        return None
    boot = record.get("boot_id")
    if not isinstance(boot, str) or not boot:
        return None
    epoch = _parse_epoch(record.get("at_utc"))
    if epoch is None:
        return None
    out = dict(record)
    out["at_epoch"] = epoch
    return out


def write_desired_state(state, *, source, path=None, boot_id=None):
    """Write the share runtime intent atomically (tmp + replace).

    ``state`` is ``"running"`` or ``"stopped"`` (anything else raises
    ValueError). Fail-safe, byte-for-byte the same discipline as
    ``Write-GatewayDesiredState`` (gateway-runtime.ps1:693-699): when writing
    ``stopped`` fails, the OLD marker is REMOVED instead - a stale ``running``
    marker would let the watch auto-start 8110 after a human stop (hard
    boundary #4); a failed ``running`` write only returns False. A missing boot
    identity cannot produce a usable marker either, so a ``stopped`` write then
    clears the old marker and returns False.
    """
    if state not in DESIRED_STATES:
        raise ValueError("desired state must be running/stopped")
    target = _fixed_name_path(path, DESIRED_FILE_NAME) if path \
        else DEFAULT_DESIRED_PATH
    if boot_id is None:
        boot_id = current_boot_id()
    if not boot_id:
        if state == "stopped":
            _remove_quietly(target)
        return False
    record = {
        "version": STATE_VERSION,
        "key": DESIRED_KEY,
        "state": state,
        "at_utc": _utc_iso(time.time()),
        "boot_id": boot_id,
        "source": str(source),
        "writer_pid": os.getpid(),
    }
    tmp = target + ".%d.tmp" % os.getpid()
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        _write_text_file(tmp, json.dumps(record, ensure_ascii=False))
        os.replace(tmp, target)
        return True
    except OSError:
        _remove_quietly(tmp)
        if state == "stopped":
            _remove_quietly(target)
        return False


def read_watch_state(path=None):
    """The supervisor's persistent state, or an honest refusal to fold it.

    Missing, unreadable, malformed or a version mismatch returns
    ``{"unreadable": True, "reason": <code>}`` - the caller NEVER treats
    it as an empty budget: a supervisor restart must not clear the consumed
    quota (A9-5). A missing file is the bootstrap case the writer may
    create; a damaged one (I/O error, malformed, version mismatch) is
    REFUSED (``watch_state_unreadable``) and never overwritten.
    """
    target = _fixed_name_path(path, STATE_FILE_NAME) if path \
        else DEFAULT_STATE_PATH
    if not os.path.exists(target):
        return {"unreadable": True, "reason": "missing"}
    try:
        with open(target, encoding="utf-8") as f:
            record = json.load(f)
    except OSError:
        return {"unreadable": True, "reason": "unreadable"}
    except ValueError:
        return {"unreadable": True, "reason": "malformed"}
    if not isinstance(record, dict):
        return {"unreadable": True, "reason": "malformed"}
    if record.get("version") != STATE_VERSION:
        return {"unreadable": True, "reason": "version_mismatch"}
    boot = record.get("boot_id")
    if not isinstance(boot, str):
        return {"unreadable": True, "reason": "malformed"}
    failures = record.get("consecutive_failures")
    if isinstance(failures, bool) or not isinstance(failures, int) \
            or failures < 0:
        return {"unreadable": True, "reason": "malformed"}
    attempts = record.get("restart_attempts_utc")
    if not isinstance(attempts, list) \
            or any(not isinstance(x, str) for x in attempts):
        return {"unreadable": True, "reason": "malformed"}
    last = record.get("last_decision")
    if last is not None and not isinstance(last, dict):
        return {"unreadable": True, "reason": "malformed"}
    idle = record.get("idle_reason")
    return {
        "unreadable": False,
        "version": STATE_VERSION,
        "boot_id": boot,
        "consecutive_failures": failures,
        "restart_attempts_utc": list(attempts),
        "idle_reason": idle if isinstance(idle, str) else "",
        "last_decision": last,
    }


def write_watch_state(state, path=None):
    """Atomic (tmp + replace) write of the supervisor's state.

    Raises OSError when the state cannot be persisted: the caller must then
    refuse to start (an unwritten attempt timestamp would let a crash forget
    the consumed quota).
    """
    target = _fixed_name_path(path, STATE_FILE_NAME) if path \
        else DEFAULT_STATE_PATH
    payload = {
        "version": STATE_VERSION,
        "boot_id": state.get("boot_id"),
        "consecutive_failures": int(state.get("consecutive_failures") or 0),
        "restart_attempts_utc": [str(x)
                                for x in state.get("restart_attempts_utc")
                                or []],
        "idle_reason": str(state.get("idle_reason") or ""),
        "last_decision": state.get("last_decision"),
    }
    tmp = target + ".%d.tmp" % os.getpid()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    _write_text_file(tmp, json.dumps(payload, ensure_ascii=False))
    os.replace(tmp, target)


def _default_ops():
    from console_share_production import production_backend_ops
    return production_backend_ops()


def _liveness(ops, pid):
    """Read-only PID liveness (the evidence verify_closed/verify_running use).

    The private ``_pid_alive`` tasklist query of the shared backend ops is the
    ONLY way to tell "the registered PID is gone" from "the PID was reused by
    another process" (``backend_pid_state`` reports both as ``pid_stale``); this
    module never writes and never kills by PID. A missing seam or a failing
    query returns None (unconfirmed), never False.
    """
    fn = getattr(ops, "_pid_alive", None)
    if not callable(fn):
        return None
    try:
        return bool(fn(pid))
    except Exception:  # noqa: BLE001 - an unconfirmed liveness stays None
        return None


def observe_share_backend(*, ops=None):
    """Read-only dedicated-backend observation (contract section 7.1).

    Returns ``{alive: bool|None, identity: "verified|reduced|unknown",
    pid_file: {present, path, pid, state, pid_matches},
    port: {listening: bool|None, occupant: "known|unknown"},
    reason: code|None}``. It reads the same evidence ``verify_running`` uses
    (the pid-file report plus the port table plus one tasklist liveness query)
    and NEVER writes: a stale pid file stays byte-for-byte in place.
    """
    ops = ops if ops is not None else _default_ops()
    report = ops.backend_pid_state()
    state = report.get("state")
    listening = report.get("port_listening")
    pid = report.get("pid")
    pid_file = {
        "present": bool(report.get("pid_file_present")),
        "path": report.get("pid_file"),
        "pid": pid,
        "state": state,
        "pid_matches": report.get("pid_matches"),
    }
    identity = report.get("identity")
    if identity not in ("verified", "reduced", "unknown"):
        identity = "unknown"
    port = {"listening": listening, "occupant": "unknown"}
    if listening is False:
        # No listener at all: the (empty) occupant is known.
        port["occupant"] = "known"
    alive = None
    reason = report.get("identity_reason") or state
    if state == "running":
        alive = True
        port["occupant"] = "known"
    elif state == "port_not_listening":
        # The registered PID still matches but nothing serves there: the
        # process is alive, the port fact is the honest reason.
        alive = True
        reason = "port_not_listening"
    elif state == "pid_file_absent":
        if listening is False:
            alive = False
            reason = "pid_file_absent"
        else:
            reason = "port_occupied_unknown"
    else:
        # pid_file_unreadable / pid_stale / anything unknown: only the
        # read-only liveness query can tell "gone" from "PID reused".
        live = _liveness(ops, pid) if pid is not None else None
        identity = "unknown"
        if state == "pid_stale" and live is False and listening is False:
            alive = False
            reason = "pid_gone"
        elif listening is True:
            port["occupant"] = "unknown"
            reason = "port_occupied_unknown"
        else:
            reason = report.get("identity_reason") or state
    return {"alive": alive, "identity": identity, "pid_file": pid_file,
            "port": port, "reason": reason}


def _read_tail(path, max_bytes=LOG_TAIL_BYTES):
    """Bounded tail read: the last ``max_bytes`` bytes, or None.

    The file is opened for read only and closed in ``finally``; a
    missing/unreadable file returns None (never a fabricated empty content).
    """
    try:
        stream = open(path, "rb")
    except OSError:
        return None
    try:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        take = min(size, max_bytes)
        stream.seek(size - take)
        data = stream.read(take)
    except OSError:
        return None
    finally:
        try:
            stream.close()
        except OSError:
            pass
    return data.decode("utf-8", errors="replace")


def _resolve_log_paths(log_path):
    """``(service_log, lifecycle_log)`` for the dedicated backend.

    The explicit ``log_path`` (tests, ``--service-log``) overrides the service
    log and must name ``share-backend.log``; the parent lifecycle log always
    sits next to it. The production default resolves the SAME deployment paths
    ``_ProductionBackendOps`` uses (``_paths()``), lazily so importing this
    module never touches the disk.
    """
    if log_path:
        service = _fixed_name_path(log_path, SERVICE_LOG_NAME)
    else:
        ops = _default_ops()
        paths = ops._paths()
        service = _fixed_name_path(paths["log"], SERVICE_LOG_NAME)
    directory = os.path.dirname(service)
    lifecycle = _fixed_name_path(
        os.path.join(directory, LIFECYCLE_LOG_NAME), LIFECYCLE_LOG_NAME)
    return service, lifecycle


def _log_local_epoch(naive):
    """Naive LOCAL wall-clock stamp (both evidence logs) -> true epoch.

    Both evidence writers stamp naive LOCAL time: the 8110 service log through
    ``logging``'s default ``asctime`` (chat2api.py:492-493) and the parent
    ``backend-lifecycle.log`` through ``time.strftime``
    (console_share_production.py:1326). The intent's ``at_utc`` is real UTC
    (``_utc_iso(time.time())``, :293), so a naive evidence stamp must be
    attached to the system timezone BEFORE ``timestamp()``. Reading it as UTC
    (``replace(tzinfo=timezone.utc)``) shifted every evidence time by the
    host's UTC offset - in UTC+8 a graceful exit 2 h BEFORE the intent
    compared as 6 h AFTER it, so ``graceful_shutdown_outside_runtime`` refused
    the crash recovery this module exists for (forever, not once).

    ``fold=0`` and ``fold=1`` are both resolved and the LATER epoch wins:
    when the wall clock is ambiguous (DST fall-back) or nonexistent
    (spring-forward gap) the evidence may never look older than it could
    really be - an over-estimated evidence time can only refuse a restart,
    never allow one (the conservative direction of contract section 7.2: a human
    close is never auto-undone, a missed recovery is not).
    """
    import datetime
    tz = _LOCAL_TZ
    if tz is not None:
        stamps = [naive.replace(tzinfo=tz, fold=fold).timestamp()
                  for fold in (0, 1)]
    else:
        stamps = [naive.replace(fold=fold).astimezone().timestamp()
                  for fold in (0, 1)]
    return max(stamps)


def _service_exit(service_text):
    """``(last_pid, graceful_at)`` from the 8110 service log tail.

    The last ``listening ... port=8110 pid=N`` line plus, after that same pid,
    ``exit begin port=8110 pid=N reason=admin_shutdown`` (the instance's own
    control-token stop; lib/watch-chat2api.ps1:320-326 accepts the same two
    reasons). No listening line means no instance evidence at all. The stamps are
    naive LOCAL wall clock and are resolved by :func:`_log_local_epoch`.
    """
    import datetime
    stamp = (r"^(?P<at>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
             r"INFO ")
    last_pid = None
    graceful_at = None
    for raw_line in service_text.splitlines():
        line = raw_line.lstrip("\ufeff")
        listen = re.match(
            # R32-KEY-AUTH: the line may carry the instance's own
            # downstream_key=... after pid (chat2api.py:6419-6421); the suffix is
            # optional so records written by either deployment still match.
            stamp + r"listening host=\S+ port=%d pid=(?P<pid>\d+)"
                    r"(?: downstream_key=\S+)?$"
            % BACKEND_PORT, line)
        if listen:
            last_pid = int(listen.group("pid"))
            graceful_at = None
            continue
        if last_pid is None:
            continue
        stop = re.match(
            stamp + (r"exit begin port=%d pid=(?P<pid>\d+) "
                     r"reason=(?:admin_shutdown|normal)$" % BACKEND_PORT),
            line)
        if stop and int(stop.group("pid")) == last_pid \
                and graceful_at is None:
            local = datetime.datetime.strptime(
                stop.group("at"), "%Y-%m-%d %H:%M:%S,%f")
            graceful_at = _log_local_epoch(local)
    return last_pid, graceful_at


def _lifecycle_stop(lifecycle_text):
    """``(state, last_pid, graceful_at)`` from the parent lifecycle tail.

    The console writes ``stopped pid=N port=8110 by=graceful`` for a verified
    stop and ``force stop pid=N port=8110 by=taskkill result=dead`` for a
    forced one (console_share_production.py:1300-1309); anything else is
    ``unknown`` and is never folded into graceful/abrupt.
    """
    import datetime
    last = None
    for raw_line in lifecycle_text.splitlines():
        stopped = re.search(
            r"pid=(?P<pid>\d+) port=%d by=(?P<by>\S+)"
            % BACKEND_PORT, raw_line)
        if stopped:
            last = (int(stopped.group("pid")), stopped.group("by"), raw_line)
    if last is None:
        return "unknown", None, None
    pid, by, raw_line = last
    if by == "graceful":
        match = re.match(
            r"(?P<at>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", raw_line)
        graceful_at = None
        if match:
            try:
                local = datetime.datetime.strptime(
                    match.group("at"), "%Y-%m-%d %H:%M:%S")
                graceful_at = _log_local_epoch(local)
            except ValueError:
                graceful_at = None
        return "graceful", pid, graceful_at
    if by == "taskkill":
        return "abrupt", pid, None
    # "by=console" (a requested stop that never confirmed) is no terminal
    # record: it stays unknown, never folded into graceful/abrupt.
    return "unknown", pid, None


def read_exit_evidence(*, log_path=None, now=None):
    """Read-only 8110 exit evidence (contract section 7.1).

    Returns ``{state: "graceful"|"abrupt"|"unknown", last_pid: int|None,
    graceful_at: float|None, source: "service_log"|"parent_log"|"none",
    reason: code|None}``.

    * the child's service log (``<backend dir>\\share-backend.log``, the
      ``CHAT2API_SERVICE_LOG`` env target at
      console_share_production.py:1264): the last ``listening ... port=8110
      pid=N`` plus, after that same pid, ``exit begin port=8110 pid=N
      reason=admin_shutdown`` -> ``graceful``;
    * the parent ``backend-lifecycle.log`` (written by the console,
      console_share_production.py:1283-1309): the last ``stopped ... by=graceful``
      -> ``graceful``; ``by=taskkill`` -> ``abrupt``;
    * a missing/unreadable file or no listening line -> ``unknown`` (NEVER
      folded into graceful/abrupt). ``now`` is accepted for the frozen
      signature; the evidence times come from the files themselves.
    """
    try:
        service_path, lifecycle_path = _resolve_log_paths(log_path)
    except Exception:  # noqa: BLE001 - an unresolvable path is no authority
        return {"state": "unknown", "last_pid": None, "graceful_at": None,
                "source": "none", "reason": "log_path_unresolvable"}
    service_text = _read_tail(service_path)
    lifecycle_text = _read_tail(lifecycle_path)
    last_pid = None
    graceful_at = None
    if service_text is not None:
        last_pid, graceful_at = _service_exit(service_text)
    if last_pid is not None and graceful_at is not None:
        return {"state": "graceful", "last_pid": last_pid,
                "graceful_at": graceful_at, "source": "service_log",
                "reason": "admin_shutdown"}
    if lifecycle_text is not None:
        state, pid, graceful_at = _lifecycle_stop(lifecycle_text)
        if state != "unknown":
            return {"state": state, "last_pid": pid, "graceful_at": graceful_at,
                    "source": "parent_log",
                    "reason": "graceful" if state == "graceful" else "taskkill"}
        if pid is not None:
            last_pid = pid
    return {"state": "unknown", "last_pid": last_pid,
            "graceful_at": None,
            "source": "service_log" if last_pid is not None else "none",
            "reason": "no_exit_record" if last_pid is not None
            else "no_listening_record"}


def _read_maintenance_status(broker):
    """Broker ``maintenance-status``, or None (no authority, honest)."""
    from console_share_production import production_maintenance_status
    try:
        return production_maintenance_status(broker=broker)
    except Exception:  # noqa: BLE001 - unwired/unreachable stays honest
        return None


def _end_maintenance(component, lease_id, broker):
    """ONE bounded ``maintenance-end``; refusals stay honest."""
    from console_share_production import production_maintenance_end
    try:
        return production_maintenance_end(component, lease_id, broker=broker)
    except Exception:  # noqa: BLE001 - a refusal leaves the lease in force
        return None


def _read_desired_stop_record():
    """Broker lifecycle record ``{state, stopped_at}``, or None.

    Production reads the SAME protected ``broker-status.json`` through the G2
    reader (console_share_production.production_desired_stop_record); the
    offline self-test/tests inject ``_DESIRED_STOP_READER`` instead.
    """
    if _DESIRED_STOP_READER is not None:
        try:
            record = _DESIRED_STOP_READER()
        except Exception:  # noqa: BLE001 - an unreadable record is no authority
            return None
        return record if isinstance(record, dict) else None
    from console_share_production import production_desired_stop_record
    try:
        record = production_desired_stop_record()
    except Exception:  # noqa: BLE001
        return None
    return record if isinstance(record, dict) else None


def _prune_attempts(attempts, now_epoch):
    """Attempt timestamps inside the last hour (a bounded, stable window)."""
    keep = []
    for raw in attempts:
        epoch = _parse_epoch(raw)
        if epoch is None:
            continue
        if now_epoch - 3600.0 <= epoch <= now_epoch + 60.0:
            keep.append(raw)
    return keep


def _normalized_watch_state(watch_state, boot_id):
    """A readable watch state bound to ``boot_id``, or None.

    A state written during a previous boot resets the failure counter and the
    hourly budget (the private watch's own ``boot_changed`` discipline,
    lib/watch-chat2api.ps1:162-167); the consumed quota is NEVER cleared
    by a supervisor restart during the SAME boot (A9-5).

    A MISSING file is the bootstrap case, not a damaged one: there is no
    consumed budget to preserve and the contract requires the attempt to be
    recorded before the start (A9-5 「原子记录尝试后启动」), so the first
    record must be able to create the file. Every other unreadable form
    (I/O error, malformed, version mismatch) stays ``None``: overwriting
    one of those could silently drop a consumed attempt.
    """
    if watch_state.get("unreadable"):
        if watch_state.get("reason") != "missing":
            return None
        return {"boot_id": boot_id, "consecutive_failures": 0,
                "restart_attempts_utc": [], "idle_reason": "",
                "last_decision": None, "boot_changed": False}
    if watch_state.get("boot_id") != boot_id:
        return {"boot_id": boot_id, "consecutive_failures": 0,
                "restart_attempts_utc": [], "idle_reason": "",
                "last_decision": None, "boot_changed": True}
    out = dict(watch_state)
    out["boot_changed"] = False
    return out


def _in_lock_state(check, boot_id):
    """The IN-LOCK state a write must be based on (R-BUDGET-LEASE).

    Re-derived from the re-check's own ``watch_state`` — never the outside
    copy: holding the lock is not enough, the write must carry the latest
    complete state or another executor's consumed attempt is erased. ``None``
    only when that state is unreadable and damaged (a MISSING file is the
    bootstrap state instead); the caller then refuses WITHOUT writing
    anything. A state bound to another boot is NOT ``None``: it
    normalizes to the fresh new-boot state (``_normalized_watch_state``),
    which resets the counter and the hourly budget per the existing boot
    discipline while a same-boot restart keeps the consumed quota (A9-5).
    """
    fresh = check.get("watch_state")
    if not isinstance(fresh, dict):
        return None
    return _normalized_watch_state(fresh, boot_id)


def check_preconditions(*, now=None, ops=None, broker=None, state_dir=None,
                        log_path=None):
    """Steps 1-7 of the frozen precondition order (contract section 7.2).

    Returns ``{ok, reason, detail, desired, maintenance, exit_evidence,
    instance, confirmations, attempts_this_hour, attempts_utc,
    watch_state, boot_id, instance_record}``. ``ok=True`` means "candidate
    confirmed and the budget allows a start"; any failed step returns
    ``ok=False`` with its stable reason. Read-only: nothing is written.
    """
    now_epoch = time.time() if now is None else float(now)
    _, desired_path, state_path, _ = _watch_paths(state_dir)
    watch_state = read_watch_state(state_path)
    attempts_existing = []
    if not watch_state.get("unreadable"):
        attempts_existing = _prune_attempts(
            watch_state.get("restart_attempts_utc", []), now_epoch)
    base = {
        "ok": False, "reason": None, "detail": "", "desired": None,
        "maintenance": None, "exit_evidence": None, "instance": None,
        "confirmations": 0, "attempts_this_hour": len(attempts_existing),
        "attempts_utc": attempts_existing, "watch_state": watch_state,
        "boot_id": None, "instance_record": None,
    }

    def _fail(reason, detail):
        """A refusal carrying every evidence field collected so far."""
        out = dict(base)
        out.update({"ok": False, "reason": reason,
                    "detail": _bounded(detail)})
        return out

    # 1. Desired intent: running, written during THIS boot.
    desired = read_desired_state(desired_path)
    base["desired"] = desired
    if desired is None:
        return _fail("desired_state_unknown",
                       "分享运行意图缺失/畸形/不可读；不自动开启")
    if desired.get("state") != "running":
        return _fail(
            "desired_stopped",
            "分享运行意图为 stopped（source=%s）；人工停止绝不自动开启"
            % desired.get("source"))
    boot_id = current_boot_id()
    base["boot_id"] = boot_id
    if not boot_id:
        return _fail(
            "boot_identity_unavailable",
            "本次开机标识读取失败：无法证明意图属于本次开机，拒绝启动")
    if desired.get("boot_id") != boot_id:
        return _fail(
            "desired_state_from_previous_boot",
            "分享运行意图来自上一次开机（意图 boot_id 与本次开机不符）；"
            "重启不自动恢复")

    # 2. No unexpired maintenance lease (an expired one is ended once).
    status = _read_maintenance_status(broker)
    base["maintenance"] = status
    if status is None:
        return _fail(
            "broker_unavailable",
            "broker maintenance-status 不可取得（未部署/不可达/被拒）："
            "无权威维护视图，拒绝启动")
    lease = status.get("lease")
    if isinstance(lease, dict):
        if status.get("lease_expired") is not True:
            return _fail(
                "maintenance_in_force",
                "维护租约仍在生效（lease_id=%s component=%s）："
                "维护窗口内不自动启动"
                % (lease.get("lease_id"), lease.get("component")))
        # An expired lease is NOT ended here: this function is the READ-ONLY
        # precondition view (``--check`` must not write). The ONE
        # ``maintenance-end`` runs in the APPLY path inside the
        # share:orchestration lock (contract §7.2 step 2 / §7.3); this
        # reports the lease as still in force so no caller mistakes the
        # expired lease for a cleared one.
        base["expired_lease"] = {
            "component": lease.get("component"),
            "lease_id": lease.get("lease_id"),
        }
        return _fail(
            "maintenance_lease_expired",
            "维护租约已过期（lease_id=%s component=%s）："
            "apply 模式在 share:orchestration 锁内结束该租约并复核后才启动；"
            "只读检查不执行 maintenance-end"
            % (lease.get("lease_id"), lease.get("component")))

    # 3. The broker lifecycle record is not "stopped".
    record = _read_desired_stop_record()
    base["instance_record"] = record
    if isinstance(record, dict) and record.get("state") == "stopped":
        return _fail(
            "managed_stop_in_force",
            "broker lifecycle 记录为 stopped（at=%s）：人工关闭绝不自动开启"
            % record.get("stopped_at"))

    # 4. No graceful 8110 exit newer than the intent.
    evidence = read_exit_evidence(log_path=log_path, now=now_epoch)
    base["exit_evidence"] = evidence
    desired_epoch = desired.get("at_epoch")
    if evidence.get("state") == "graceful":
        graceful_at = evidence.get("graceful_at")
        if graceful_at is None or desired_epoch is None \
                or graceful_at > desired_epoch:
            return _fail(
                "graceful_shutdown_outside_runtime",
                "8110 在运行意图之后发生过人工/安全关闭（graceful_at=%s "
                "desired_at=%s）：绝不重启"
                % (graceful_at, desired_epoch))

    # 5. Instance: alive-and-verified, or confirmed absent with no unknown
    #    port occupant.
    instance = observe_share_backend(ops=ops)
    base["instance"] = instance
    if instance.get("alive") is True and instance.get("identity") == "verified":
        return _fail("instance_alive",
                       "8110 实例运行中且身份 verified；无动作")
    if instance.get("alive") is True:
        return _fail(
            "instance_identity_unknown",
            "8110 实例存活但身份非 verified（identity=%s reason=%s）："
            "绝不按裸 PID 处置" % (instance.get("identity"),
                                   instance.get("reason")))
    if instance.get("port", {}).get("listening") is True \
            and instance.get("port", {}).get("occupant") == "unknown":
        return _fail(
            "port_occupied_unknown",
            "端口 %d 被未知占用（身份不可归属）：拒绝启动第二个实例"
            % BACKEND_PORT)
    if instance.get("alive") is not False:
        return _fail(
            "instance_identity_unknown",
            "8110 实例证据不足（reason=%s）：既不确认存活也不确认消失，"
            "拒绝启动" % instance.get("reason"))
    if evidence.get("state") == "unknown":
        return _fail(
            "exit_evidence_unknown",
            "8110 已确认消失但退出证据不可裁定（source=%s reason=%s）："
            "证据不足绝不当作异常退出"
            % (evidence.get("source"), evidence.get("reason")))

    # 6. Two consecutive candidate observations.
    state = _normalized_watch_state(watch_state, boot_id)
    if state is None:
        return _fail(
            "watch_state_unreadable",
            "监督状态文件不可读（reason=%s）：不折成空预算，拒绝启动"
            % watch_state.get("reason"))
    confirmations = min(int(state.get("consecutive_failures", 0)) + 1,
                       FAILURE_THRESHOLD)
    base["confirmations"] = confirmations
    if confirmations < FAILURE_THRESHOLD:
        return _fail(
            "awaiting_confirmation",
            "候选缺席观测 %d/%d：需连续两次确认后才尝试启动"
            % (confirmations, FAILURE_THRESHOLD))

    # 7. The hourly budget (the attempt timestamp is written before start).
    attempts = _prune_attempts(state.get("restart_attempts_utc", []), now_epoch)
    base["attempts_utc"] = attempts
    base["attempts_this_hour"] = len(attempts)
    if len(attempts) >= MAX_STARTS_PER_HOUR:
        return _fail(
            "budget_exhausted",
            "本小时启动预算已耗尽（%d/%d）：只报告并给人工入口——"
            "查看监督日志 %s 或 console「分享」面板后人工决定"
            % (len(attempts), MAX_STARTS_PER_HOUR,
               os.path.join(state_dir or DEFAULT_WATCH_DIR, LOG_FILE_NAME)))
    base["ok"] = True
    base["reason"] = "instance_absent"
    base["detail"] = "8110 已确认消失且退出证据可裁定（%s）；候选启动" \
        % evidence.get("state")
    return base


def _record(action, code, detail, confirmations, attempts, instance, started):
    """One bounded decision record (contract section 7.3)."""
    return {
        "at_utc": _utc_iso(time.time()),
        "action": action,
        "code": code,
        "detail": _bounded(detail),
        "confirmations": int(confirmations),
        "attempts_this_hour": int(attempts),
        "instance": instance,
        "duration_ms": round((time.monotonic() - started) * 1000.0, 1),
    }


def _instance_summary(instance):
    """The bounded ``{pid, identity}`` summary of an observation."""
    pid = None
    identity = "unknown"
    if isinstance(instance, dict):
        pid_file = instance.get("pid_file")
        if isinstance(pid_file, dict):
            pid = pid_file.get("pid")
        if instance.get("identity"):
            identity = instance["identity"]
    return {"pid": pid, "identity": identity}


def _append_log(path, record):
    """One bounded JSON line in the decision log (best effort)."""
    try:
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _verify_started(ops, budget_s=VERIFY_BUDGET_S,
                    interval_s=VERIFY_POLL_INTERVAL_S):
    """Bounded confirmation poll over the shared handler's verify_running."""
    deadline = time.monotonic() + max(0.0, float(budget_s))
    while True:
        try:
            if ops.verify_running() is True:
                return True
        except Exception:  # noqa: BLE001 - a failing verify is no confirmation
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval_s)


def run_once(*, apply=False, now=None, ops=None, broker=None,
              state_dir=None, lock_dir=None, log_path=None):
    """One bounded decision; ``apply=True`` runs steps 8-9 (re-check inside
    the SAME ``share:orchestration`` lock, then the shared ``start()``
    handler) and persists the state + decision log. ``apply=False``
    (``--check``) is read-only: it stops BEFORE the lock (it must not write
    the lock file either) and reports ``would_start``. Returns the decision
    record (contract section 7.3).
    """
    started = time.monotonic()
    now_epoch = time.time() if now is None else float(now)
    _, _, state_path, log_file = _watch_paths(state_dir)
    if ops is None:
        ops = _default_ops()
    watch_state = read_watch_state(state_path)
    check = check_preconditions(now=now_epoch, ops=ops, broker=broker,
                                state_dir=state_dir, log_path=log_path)
    confirmations = check.get("confirmations", 0)
    attempts = check.get("attempts_utc", [])
    instance = _instance_summary(check.get("instance"))
    boot_id = check.get("boot_id") or ""

    if not apply:
        # Read-only: decide, write nothing, start nothing.
        if check.get("ok"):
            record = _record(
                "start", "would_start",
                "只读检查：前提闭合且确认/预算满足；apply 模式才会启动",
                confirmations, len(attempts), instance, started)
        else:
            record = _record("none", check.get("reason"),
                             check.get("detail"), confirmations,
                             len(attempts), instance, started)
        return record

    # A damaged state file is never overwritten with a fresh one (that would
    # silently reset the budget); the decision is still logged. A MISSING file
    # is the bootstrap case instead (no consumed budget exists yet and the
    # attempt must be recorded before the start, A9-5): the fresh state is
    # written INSIDE the lock below, from the in-lock re-read.
    if (watch_state.get("unreadable")
            and watch_state.get("reason") != "missing") or not boot_id:
        record = _record("none", check.get("reason"), check.get("detail"),
                         confirmations, len(attempts), instance, started)
        _append_log(log_file, record)
        return record

    def _persist(new_state, record):
        new_state["last_decision"] = record
        try:
            write_watch_state(new_state, state_path)
        except OSError:
            pass
        _append_log(log_file, record)

    # A pre-lock refusal does NOT write the state here: the state file is the
    # shared recovery budget and only a holder of the share:orchestration lock
    # may write it (a stale write-back would silently drop another executor's
    # consumed attempt). Every refusal except an expired maintenance lease is
    # handled by the in-lock re-check below (contract §7.2 step 9: the re-check
    # of steps 1-7 refuses on any change) — the expired lease is the ONE case
    # the APPLY path resolves there (step 2 / §7.3).

    # 8. The SAME share:orchestration lock the console actions use; a held
    #    console lock means "busy, do nothing" (never a nested acquisition).
    lock = None
    try:
        lock = ResourceFileLock(lock_dir or LOCK_DIR, LOCK_RESOURCE,
                                owner=WATCH_OWNER, timeout_s=LOCK_TIMEOUT_S)
        lock.acquire()
    except LockBusy:
        # Another executor owns the state file right now: we hold NO authority
        # to write it back (a stale write-back would silently drop their
        # consumed attempt and break the hourly cap). The decision is logged.
        record = _record(
            "none", "resource_busy",
            "分享编排资源锁被占用（console 动作进行中）：本轮不动作，"
            "下一轮重试；不写状态（另一执行者持有状态文件）",
            confirmations, len(attempts), instance, started)
        _append_log(log_file, record)
        return record
    except Exception as exc:  # noqa: BLE001 - an unusable lock refuses
        # No lock means no authority over the shared state file: log only.
        record = _record(
            "none", "resource_busy",
            "资源锁打开失败（%s）：不动作；不写状态（未取得锁）"
            % type(exc).__name__,
            confirmations, len(attempts), instance, started)
        _append_log(log_file, record)
        return record
    try:
        # 9. Re-check 1-7 inside the lock: any change refuses.
        recheck = check_preconditions(now=now_epoch, ops=ops, broker=broker,
                                      state_dir=state_dir, log_path=log_path)
        # Every write below must use the IN-LOCK state: a state built outside
        # the lock carries a stale budget, and another executor's consumed
        # attempt would be silently erased (R-BUDGET-LEASE: holding the lock
        # is not enough — the write must be based on the latest complete
        # in-lock state). An unreadable in-lock state refuses without
        # overwriting anything.
        state = _in_lock_state(recheck, boot_id)
        if state is None:
            record = _record(
                "none", "watch_state_unreadable",
                "锁内重核的监督状态不可读：不覆盖既有状态，拒绝启动",
                confirmations, len(recheck.get("attempts_utc", [])),
                instance, started)
            _append_log(log_file, record)
            return record
        if recheck.get("reason") == "maintenance_lease_expired":
            # The ONE expired-lease maintenance-end runs HERE: inside the
            # share:orchestration lock and only in apply mode (contract §7.2
            # step 2 / §7.3) — a read-only check never performs it. The
            # preconditions are re-checked after ending before any start.
            expired = recheck.get("expired_lease") or {}
            ended = _end_maintenance(expired.get("component"),
                                     expired.get("lease_id"), broker)
            status = _read_maintenance_status(broker)
            if status is None:
                record = _record(
                    "none", "broker_unavailable",
                    "过期租约的 maintenance-end 后重读不可取得（%s）："
                    "无权威视图，拒绝启动" % ended,
                    confirmations, len(recheck.get("attempts_utc", [])),
                    instance, started)
                _persist(state, record)
                return record
            lease = status.get("lease")
            if isinstance(lease, dict):
                record = _record(
                    "none", "maintenance_in_force",
                    "过期租约的 maintenance-end 未清除租约（%s）："
                    "维护仍在 force，拒绝启动" % ended,
                    confirmations, len(recheck.get("attempts_utc", [])),
                    instance, started)
                _persist(state, record)
                return record
            recheck = check_preconditions(now=now_epoch, ops=ops,
                                          broker=broker, state_dir=state_dir,
                                          log_path=log_path)
            state = _in_lock_state(recheck, boot_id)
            if state is None:
                record = _record(
                    "none", "watch_state_unreadable",
                    "结束过期租约后锁内重核的监督状态不可读：不覆盖既有状态，"
                    "拒绝启动",
                    confirmations, len(recheck.get("attempts_utc", [])),
                    instance, started)
                _append_log(log_file, record)
                return record
        if not recheck.get("ok"):
            new_confirmations = recheck.get("confirmations", 0)
            state["consecutive_failures"] = new_confirmations
            state["idle_reason"] = recheck.get("reason")
            state["restart_attempts_utc"] = recheck.get("attempts_utc", [])
            record = _record("none", recheck.get("reason"),
                             recheck.get("detail"), new_confirmations,
                             len(recheck.get("attempts_utc", [])),
                             _instance_summary(recheck.get("instance")),
                             started)
            _persist(state, record)
            return record

        # Consume the budget BEFORE start: a crash must not lose it. The list
        # is taken from the RE-CHECK INSIDE THE LOCK — a stale pre-lock
        # snapshot would let two supervisors each write the same count back and
        # silently lose one attempt (breaking the hourly cap).
        attempts = list(recheck.get("attempts_utc", []))
        attempts.append(_utc_iso(now_epoch))
        state["consecutive_failures"] = 0
        state["restart_attempts_utc"] = attempts
        state["idle_reason"] = ""
        try:
            write_watch_state(state, state_path)
        except OSError:
            record = _record(
                "none", "watch_state_unreadable",
                "尝试时间戳写入失败：不消耗额度也不启动（崩溃不丢额度）",
                confirmations, len(attempts), instance, started)
            _append_log(log_file, record)
            return record

        code = "started"
        detail = "共用恢复处理器已启动 8110 并确认运行"
        try:
            if ops.start() is not True:
                code = "start_unconfirmed"
                detail = "共用恢复处理器 start() 未确认成功"
            elif not _verify_started(ops):
                code = "start_unconfirmed"
                detail = "已尝试启动 8110 但 verify_running 未确认"
        except Exception as exc:  # noqa: BLE001 - an unconfirmed start
            code = "start_unconfirmed"
            detail = "共用恢复处理器 start() 失败（%s）：未确认运行" \
                % type(exc).__name__
        record = _record("start", code, detail, 0, len(attempts),
                         instance, started)
        state["idle_reason"] = code
        _persist(state, record)
        return record
    finally:
        try:
            lock.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 - releasing stays best effort
            pass


def _exit_code(record):
    """The frozen exit codes: 0 no action, 1 refused, 2 unconfirmed start."""
    action = record.get("action")
    code = record.get("code")
    if action == "start":
        return EXIT_ALIVE if code == "started" else EXIT_UNCONFIRMED
    if code in _BENIGN_CODES:
        return EXIT_ALIVE
    return EXIT_REFUSED


def main(argv=None):
    """CLI: ``--check`` / ``--apply`` / ``--selftest`` (contract section
    7.1). The decision record is printed as one JSON line; the exit code is
    EXIT_ALIVE / EXIT_REFUSED / EXIT_UNCONFIRMED.
    """
    import argparse
    parser = argparse.ArgumentParser(
        description="8110 dedicated-backend bounded crash recovery")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="read-only decision (writes nothing)")
    mode.add_argument("--apply", action="store_true",
                      help="apply the decision (writes state, may start 8110)")
    mode.add_argument("--selftest", action="store_true",
                      help="offline self-test in TEMP (no production I/O)")
    parser.add_argument("--state-dir", default=None,
                        help="watch directory override (tests)")
    parser.add_argument("--lock-dir", default=None,
                        help="resource lock directory override (tests)")
    parser.add_argument("--service-log", default=None,
                        help="8110 service log override (exit evidence)")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    if not args.check and not args.apply:
        print("必须指定 --check、--apply 或 --selftest；"
              "监督不做任何未请求的动作", file=sys.stderr)
        return EXIT_REFUSED
    record = run_once(apply=bool(args.apply), state_dir=args.state_dir,
                      lock_dir=args.lock_dir, log_path=args.service_log)
    print(json.dumps(record, ensure_ascii=False))
    return _exit_code(record)


# ---------------------------------------------------------------------------
# Offline self-test (--selftest): TEMP dirs and injected stubs only; it never
# reads or writes a production path and never starts a process.
# ---------------------------------------------------------------------------

class _StubResult:
    def __init__(self, ok, result=None, error_code="ok"):
        self.ok = bool(ok)
        self.result = result if result is not None else {}
        self.error_code = error_code


class _StubBroker:
    """The broker ADAPTER double for the maintenance-status/end path.

    The console seams call the ``BrokerClientAdapter`` public control surface
    directly — ``production_maintenance_status``/``production_maintenance_end``
    invoke the adapter verbs and NEVER ``broker.client`` — so this double
    exposes the adapter verbs itself and has no ``client`` attribute: a seam that
    regressed to the inner client reads as unavailable (``broker_unavailable``)
    instead of silently passing the offline self-test.
    """

    def __init__(self):
        self.status_result = _StubResult(
            True, {"checked_at": 0, "lease": None, "lease_expired": None,
                    "instance": None, "last_outcome": None})
        self.end_result = _StubResult(True, {"resumed": True})
        self.end_calls = []

    def maintenance_status(self):
        return self.status_result

    def maintenance_end(self, component, lease_id):
        self.end_calls.append((component, lease_id))
        return self.end_result


class _StubOps:
    """The backend-ops double: pid report + liveness + shared start."""

    def __init__(self):
        self.pid_state = {
            "pid_file": "stub-backend.pid", "pid_file_present": False,
            "pid": None, "pid_matches": None, "port_listening": False,
            "state": "pid_file_absent", "identity": "unknown",
            "identity_reason": "pid_file_absent",
        }
        self.alive = None
        self.starts = 0
        self.verify_result = True

    def backend_pid_state(self, paths=None):
        return dict(self.pid_state)

    def _pid_alive(self, pid):
        return self.alive

    def start(self):
        self.starts += 1
        return True

    def verify_running(self, forget_stale=True):
        return self.verify_result


def _selftest():
    """The frozen ``--selftest``: TEMP dirs, injected stubs, no production
    path read/written and no process started. Success prints
    ``share-backend-supervision self-test: PASS`` and exits 0.
    """
    import shutil
    import tempfile
    from pathlib import Path

    import console_operations as _co

    global _BOOT_ID_READER, _DESIRED_STOP_READER
    global VERIFY_BUDGET_S, VERIFY_POLL_INTERVAL_S

    root = tempfile.mkdtemp(
        prefix="AIGatewayPublic-share-backend-supervision-selftest-")
    saved_boot = _BOOT_ID_READER
    saved_stop = _DESIRED_STOP_READER
    saved_verify = VERIFY_BUDGET_S
    saved_interval = VERIFY_POLL_INTERVAL_S
    saved_manager = _co.OperationManager
    boot = "selftest-boot"

    def _expect(condition, why):
        if not condition:
            raise AssertionError(why)

    try:
        _BOOT_ID_READER = lambda: boot
        # The offline self-test never reads the production broker-status
        # record: the stub says "no lifecycle record" until a case needs one.
        _DESIRED_STOP_READER = lambda: None
        state_dir = os.path.join(root, "state")
        os.makedirs(state_dir, exist_ok=True)
        desired_path = os.path.join(state_dir, DESIRED_FILE_NAME)
        state_path = os.path.join(state_dir, STATE_FILE_NAME)
        service_log = os.path.join(root, SERVICE_LOG_NAME)
        lifecycle_log = os.path.join(root, LIFECYCLE_LOG_NAME)
        lock_dir = os.path.join(root, "locks")
        log_file = os.path.join(state_dir, LOG_FILE_NAME)

        # 1. Desired intent round-trip + malformed/unknown/boot-less reads.
        _expect(write_desired_state("running", source="selftest",
                                   path=desired_path, boot_id=boot) is True,
                "write running")
        desired = read_desired_state(desired_path)
        _expect(desired is not None and desired["state"] == "running"
                and desired["boot_id"] == boot, "read running back")
        Path(desired_path).write_text("{not json", encoding="utf-8")
        _expect(read_desired_state(desired_path) is None, "malformed marker")
        Path(desired_path).write_text(json.dumps({
            "version": 1, "key": DESIRED_KEY, "state": "maybe",
            "at_utc": "2026-10-03T00:00:00+00:00", "boot_id": boot}),
            encoding="utf-8")
        _expect(read_desired_state(desired_path) is None, "unknown state")
        Path(desired_path).write_text(json.dumps({
            "version": 1, "key": DESIRED_KEY, "state": "running",
            "at_utc": "2026-10-03T00:00:00+00:00"}), encoding="utf-8")
        _expect(read_desired_state(desired_path) is None, "missing boot id")

        # 2. Fail-safe: a failed "stopped" write removes the old marker.
        _expect(write_desired_state("running", source="selftest",
                                   path=desired_path, boot_id=boot) is True,
                "re-write running")
        blocker = desired_path + ".%d.tmp" % os.getpid()
        os.mkdir(blocker)
        try:
            failed = write_desired_state("stopped", source="selftest",
                                        path=desired_path, boot_id=boot)
        finally:
            os.rmdir(blocker)
        _expect(failed is False, "stopped write must fail")
        _expect(not os.path.exists(desired_path),
                "failed stopped write must remove the old marker")
        _expect(read_desired_state(desired_path) is None,
                "removed marker reads as unknown")

        # 3. Watch state: missing/corrupt/version mismatch are unreadable.
        _expect(read_watch_state(state_path)["reason"] == "missing",
                "missing watch state")
        Path(state_path).write_text("{broken", encoding="utf-8")
        _expect(read_watch_state(state_path)["reason"] == "malformed",
                "corrupt watch state")
        Path(state_path).write_text(json.dumps({"version": 99, "boot_id": boot}),
                                   encoding="utf-8")
        _expect(read_watch_state(state_path)["reason"] == "version_mismatch",
                "version mismatch")
        write_watch_state({"boot_id": boot, "consecutive_failures": 1,
                           "restart_attempts_utc": [], "idle_reason": "",
                           "last_decision": None}, state_path)
        _expect(read_watch_state(state_path)["consecutive_failures"] == 1,
                "watch state round-trip")

        # 4. Exit evidence: graceful / abrupt / unknown.
        Path(service_log).write_text(
            "2026-10-03 09:00:00,000 INFO listening host=127.0.0.1 "
            "port=%d pid=4242\n" % BACKEND_PORT +
            "2026-10-03 09:01:00,000 INFO exit begin port=%d "
            "pid=4242 reason=admin_shutdown\n" % BACKEND_PORT,
            encoding="utf-8")
        evidence = read_exit_evidence(log_path=service_log)
        _expect(evidence["state"] == "graceful"
                and evidence["source"] == "service_log", "graceful evidence")
        Path(service_log).write_text(
            "2026-10-03 09:00:00,000 INFO listening host=127.0.0.1 "
            "port=%d pid=4242\n" % BACKEND_PORT, encoding="utf-8")
        Path(lifecycle_log).write_text(
            "2026-10-03 09:02:00 force stop pid=4242 port=%d "
            "by=taskkill result=dead\n" % BACKEND_PORT, encoding="utf-8")
        evidence = read_exit_evidence(log_path=service_log)
        _expect(evidence["state"] == "abrupt"
                and evidence["source"] == "parent_log", "abrupt evidence")
        os.remove(service_log)
        os.remove(lifecycle_log)
        _expect(read_exit_evidence(log_path=service_log)["state"] == "unknown",
                "missing logs are unknown")

        # 5. check_preconditions: every refusal path, then a candidate.
        ops = _StubOps()
        broker = _StubBroker()

        def _desired(state, source="selftest"):
            return write_desired_state(state, source=source,
                                      path=desired_path, boot_id=boot)

        _desired("running")
        base = check_preconditions(now=1000.0, ops=ops, broker=broker,
                                    state_dir=state_dir,
                                    log_path=service_log)
        _expect(base["ok"] is False
                and base["reason"] == "exit_evidence_unknown",
                "candidate without exit evidence must refuse")
        _expect(base["instance"]["alive"] is False,
                "confirmed absent instance")

        _desired("stopped")
        stopped_check = check_preconditions(
            now=1000.0, ops=ops, broker=broker, state_dir=state_dir,
            log_path=service_log)
        _expect(stopped_check["reason"] == "desired_stopped",
                "stopped intent refuses")
        _desired("running")
        record = read_desired_state(desired_path)
        record["boot_id"] = "older-boot"
        Path(desired_path).write_text(json.dumps(record), encoding="utf-8")
        _expect(check_preconditions(
            now=1000.0, ops=ops, broker=broker, state_dir=state_dir,
            log_path=service_log)["reason"] == "desired_state_from_previous_boot",
            "previous-boot intent refuses")
        os.remove(desired_path)
        _expect(check_preconditions(
            now=1000.0, ops=ops, broker=broker, state_dir=state_dir,
            log_path=service_log)["reason"] == "desired_state_unknown",
            "missing intent refuses")
        _desired("running")
        _BOOT_ID_READER = lambda: None
        _expect(check_preconditions(
            now=1000.0, ops=ops, broker=broker, state_dir=state_dir,
            log_path=service_log)["reason"] == "boot_identity_unavailable",
            "unreadable boot id refuses")
        _BOOT_ID_READER = lambda: boot
        _desired("running")

        # 5b. Broker maintenance: unreachable, in force, expired then ended.
        broker.status_result = _StubResult(False, {}, "unavailable")
        _expect(check_preconditions(
            now=1000.0, ops=ops, broker=broker, state_dir=state_dir,
            log_path=service_log)["reason"] == "broker_unavailable",
            "unreachable broker refuses")
        broker.status_result = _StubResult(True, {
            "checked_at": 1000, "lease": {"lease_id": "a" * 32,
                                          "component": "share_gateway",
                                          "expires_at": 2000},
            "lease_expired": False, "instance": None, "last_outcome": None})
        _expect(check_preconditions(
            now=1000.0, ops=ops, broker=broker, state_dir=state_dir,
            log_path=service_log)["reason"] == "maintenance_in_force",
            "live lease refuses")
        broker.status_result = _StubResult(True, {
            "checked_at": 1000, "lease": {"lease_id": "a" * 32,
                                          "component": "share_gateway",
                                          "expires_at": 999},
            "lease_expired": True, "instance": None, "last_outcome": None})
        # The READ-ONLY check reports the expired lease without ending it:
        # the ONE maintenance-end runs in the APPLY path inside the
        # share:orchestration lock (contract §7.2 step 2 / §7.3).
        _expect(check_preconditions(
            now=1000.0, ops=ops, broker=broker, state_dir=state_dir,
            log_path=service_log)["reason"] == "maintenance_lease_expired",
            "expired lease refuses read-only")
        _expect(broker.end_calls == [],
                "the read-only check never ends an expired lease")
        broker.status_result = _StubResult(True, {
            "checked_at": 1000, "lease": None, "lease_expired": None,
            "instance": None, "last_outcome": None})

        # 5c. Broker lifecycle stopped refuses.
        _DESIRED_STOP_READER = lambda: {"state": "stopped",
                                        "stopped_at": 1}
        _expect(check_preconditions(
            now=1000.0, ops=ops, broker=broker, state_dir=state_dir,
            log_path=service_log)["reason"] == "managed_stop_in_force",
            "managed stop refuses")
        _DESIRED_STOP_READER = lambda: None

        # 5d. Graceful exit newer than the intent refuses.
        marker = read_desired_state(desired_path)
        marker["at_utc"] = "2020-01-01T00:00:00+00:00"
        Path(desired_path).write_text(json.dumps(marker), encoding="utf-8")
        Path(service_log).write_text(
            "2026-10-03 09:00:00,000 INFO listening host=127.0.0.1 "
            "port=%d pid=4242\n" % BACKEND_PORT +
            "2026-10-03 09:01:00,000 INFO exit begin port=%d "
            "pid=4242 reason=admin_shutdown\n" % BACKEND_PORT,
            encoding="utf-8")
        _expect(check_preconditions(
            now=1000.0, ops=ops, broker=broker, state_dir=state_dir,
            log_path=service_log)["reason"]
            == "graceful_shutdown_outside_runtime",
            "graceful exit newer than intent refuses")

        # 5e. Instance evidence: alive / unknown identity / unknown port.
        os.remove(service_log)
        ops.pid_state = {"pid_file": "x", "pid_file_present": True,
                         "pid": 4242, "pid_matches": True,
                         "port_listening": True, "state": "running",
                         "identity": "verified", "identity_reason": None}
        _expect(check_preconditions(
            now=1000.0, ops=ops, broker=broker, state_dir=state_dir,
            log_path=service_log)["reason"] == "instance_alive",
            "live verified instance refuses")
        ops.pid_state = dict(ops.pid_state, identity="reduced",
                             identity_reason="instance_record_absent")
        _expect(check_preconditions(
            now=1000.0, ops=ops, broker=broker, state_dir=state_dir,
            log_path=service_log)["reason"] == "instance_identity_unknown",
            "live unverified instance refuses")
        ops.pid_state = {"pid_file": "x", "pid_file_present": False,
                         "pid": None, "pid_matches": None,
                         "port_listening": True, "state": "pid_file_absent",
                         "identity": "unknown",
                         "identity_reason": "pid_file_absent"}
        _expect(check_preconditions(
            now=1000.0, ops=ops, broker=broker, state_dir=state_dir,
            log_path=service_log)["reason"] == "port_occupied_unknown",
            "unknown port occupant refuses")

        # 5f. Two confirmations + budget; abrupt evidence is a candidate.
        ops.pid_state = {"pid_file": "x", "pid_file_present": True,
                         "pid": 4242, "pid_matches": False,
                         "port_listening": False, "state": "pid_stale",
                         "identity": "unknown",
                         "identity_reason": "identity_mismatch"}
        ops.alive = False
        Path(service_log).write_text(
            "2026-10-03 09:00:00,000 INFO listening host=127.0.0.1 "
            "port=%d pid=4242\n" % BACKEND_PORT, encoding="utf-8")
        Path(lifecycle_log).write_text(
            "2026-10-03 09:02:00 force stop pid=4242 port=%d "
            "by=taskkill result=dead\n" % BACKEND_PORT, encoding="utf-8")
        write_watch_state({"boot_id": boot, "consecutive_failures": 0,
                           "restart_attempts_utc": [], "idle_reason": "",
                           "last_decision": None}, state_path)
        first = check_preconditions(now=1000.0, ops=ops, broker=broker,
                                    state_dir=state_dir,
                                    log_path=service_log)
        _expect(first["reason"] == "awaiting_confirmation",
                "first candidate observation awaits confirmation")
        write_watch_state({"boot_id": boot, "consecutive_failures": 1,
                           "restart_attempts_utc": [], "idle_reason": "",
                           "last_decision": None}, state_path)
        second = check_preconditions(now=1000.0, ops=ops, broker=broker,
                                     state_dir=state_dir,
                                     log_path=service_log)
        _expect(second["ok"] is True and second["confirmations"] == 2,
                "second candidate observation confirms")
        write_watch_state({"boot_id": boot, "consecutive_failures": 2,
                           "restart_attempts_utc": [], "idle_reason": "",
                           "last_decision": None}, state_path)
        write_watch_state({"boot_id": boot, "consecutive_failures": 2,
                           "restart_attempts_utc": [_utc_iso(900.0),
                                                    _utc_iso(950.0),
                                                    _utc_iso(960.0)],
                           "idle_reason": "", "last_decision": None},
                          state_path)
        _expect(check_preconditions(
            now=1000.0, ops=ops, broker=broker, state_dir=state_dir,
            log_path=service_log)["reason"] == "budget_exhausted",
            "hourly budget exhausted refuses")

        # 6. run_once: read-only would_start, lock busy, then a real start.
        write_watch_state({"boot_id": boot, "consecutive_failures": 2,
                           "restart_attempts_utc": [], "idle_reason": "",
                           "last_decision": None}, state_path)
        would = run_once(apply=False, now=1000.0, ops=ops, broker=broker,
                         state_dir=state_dir, lock_dir=lock_dir,
                         log_path=service_log)
        _expect(would["action"] == "start"
                and would["code"] == "would_start", "check would_start")
        _expect(ops.starts == 0, "check must not start")
        _expect(not os.path.exists(log_file), "check must not write the log")
        held = ResourceFileLock(lock_dir, LOCK_RESOURCE, owner="selftest",
                                timeout_s=LOCK_TIMEOUT_S)
        held.acquire()
        try:
            busy = run_once(apply=True, now=1000.0, ops=ops, broker=broker,
                            state_dir=state_dir, lock_dir=lock_dir,
                            log_path=service_log)
        finally:
            held.__exit__(None, None, None)
        _expect(busy["code"] == "resource_busy", "held lock is busy")
        _expect(ops.starts == 0, "busy lock must not start")
        applied = run_once(apply=True, now=1000.0, ops=ops, broker=broker,
                           state_dir=state_dir, lock_dir=lock_dir,
                           log_path=service_log)
        _expect(applied["action"] == "start"
                and applied["code"] == "started", "applied start confirmed")
        _expect(ops.starts == 1, "exactly one start")
        final = read_watch_state(state_path)
        _expect(final["consecutive_failures"] == 0, "start resets failures")
        _expect(len(final["restart_attempts_utc"]) == 1,
                "attempt timestamp persisted")
        _expect(final["last_decision"]["code"] == "started",
                "last_decision persisted")
        _expect(os.path.exists(log_file), "decision log written")

        # 7. Unconfirmed start: attempt consumed, exit code 2.
        write_watch_state({"boot_id": boot, "consecutive_failures": 2,
                           "restart_attempts_utc": [], "idle_reason": "",
                           "last_decision": None}, state_path)
        ops.verify_result = False
        VERIFY_BUDGET_S = 0.05
        VERIFY_POLL_INTERVAL_S = 0.01
        unconfirmed = run_once(apply=True, now=1000.0, ops=ops, broker=broker,
                               state_dir=state_dir, lock_dir=lock_dir,
                               log_path=service_log)
        _expect(unconfirmed["code"] == "start_unconfirmed"
                and _exit_code(unconfirmed) == EXIT_UNCONFIRMED,
                "unconfirmed start")
        _expect(ops.starts == 2, "second start attempted")
        ops.verify_result = True

        # 8. No OperationManager is ever constructed (hard boundary #1).
        _expect("usagesrv" not in sys.modules,
                "supervision must not import usagesrv")

        class _ForbiddenManager:
            def __init__(self, *args, **kwargs):
                raise AssertionError(
                    "supervision constructed an OperationManager")

        _co.OperationManager = _ForbiddenManager
        try:
            write_watch_state({"boot_id": boot, "consecutive_failures": 2,
                               "restart_attempts_utc": [], "idle_reason": "",
                               "last_decision": None}, state_path)
            again = run_once(apply=True, now=1000.0, ops=ops, broker=broker,
                             state_dir=state_dir, lock_dir=lock_dir,
                             log_path=service_log)
        finally:
            _co.OperationManager = saved_manager
        _expect(again["code"] == "started",
                "start under the OperationManager guard")
        _expect(_exit_code({"action": "none", "code": "instance_alive"})
                == EXIT_ALIVE, "alive exit code")
        _expect(_exit_code({"action": "none", "code": "budget_exhausted"})
                == EXIT_REFUSED, "refused exit code")

        print("share-backend-supervision self-test: PASS")
        return EXIT_ALIVE
    except Exception as exc:  # noqa: BLE001 - a self-test failure is loud
        print("share-backend-supervision self-test: FAIL (%s: %s)"
              % (type(exc).__name__, _bounded(exc)), file=sys.stderr)
        return EXIT_REFUSED
    finally:
        _BOOT_ID_READER = saved_boot
        _DESIRED_STOP_READER = saved_stop
        VERIFY_BUDGET_S = saved_verify
        VERIFY_POLL_INTERVAL_S = saved_interval
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
