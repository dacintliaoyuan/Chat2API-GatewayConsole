# -*- coding: utf-8 -*-
"""Console operation layer: bounded task queue, cross-process resource locks,
idempotency keys, generation binding, cooperative cancel and sqlite persistence.

Scope: P1 local foundation for the unified AI-gateway console (Plan-Revision 4).
Cross-process mutual exclusion uses an exclusive-handle file lock; the identical
protocol is implemented in lib/console-runtime-bridge.ps1 so the web console and
CLI/runtime share one boundary. The lock file only stores owner information;
the lock itself is the open file handle (no share mode), so a dead owner
releases automatically and no stale-marker heuristic exists.

Persistence: every operation row is written when submitted; on manager start,
unfinished operations are marked interrupted and are never replayed.
"""

import json
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes

import ctypes

HERE = os.path.dirname(os.path.abspath(__file__))

MAX_PARAM_BYTES = 4096
MAX_STEPS = 32
MAX_STEP_NOTE_CHARS = 200
MAX_ERROR_CHARS = 200
SCHEMA_VERSION = 1
TERMINAL_STATES = ("succeeded", "failed", "cancelled", "interrupted")


class ConsoleStateError(Exception):
    """The console state database cannot be opened safely (fail-closed)."""


class OperationError(Exception):
    """Structured operation rejection with a machine-readable code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class OperationUnavailable(OperationError):
    """A registered-but-disabled action; never a simulated success."""

    def __init__(self, action, reason):
        super().__init__(
            "action_disabled",
            "action %s is not available: %s" % (action, reason))
        self.action = action
        self.reason = reason


class OperationCancelled(Exception):
    """Raised by a cooperative adapter after observing the cancel event."""


class LockBusy(Exception):
    """The resource file lock is held by another live owner."""


def _single_line(text, limit=MAX_ERROR_CHARS):
    return " ".join(str(text).split())[:limit]


def _sanitize_resource(resource):
    """Lock file names derive from resource ids; keep them strictly bounded
    so the id can never traverse out of the lock directory."""
    cleaned = "".join(
        ch if (ch.isascii() and (ch.isalnum() or ch in "_-")) else "_"
        for ch in str(resource))
    if not resource or len(str(resource)) > 128 or len(cleaned) > 64:
        raise OperationError("invalid_resource", "resource id is not usable")
    return cleaned


class ResourceFileLock:
    """Exclusive-handle file lock shared with the PowerShell bridge.

    Protocol (both sides MUST implement identically):
    * lock file <lock_dir>/<resource>.lock;
    * acquisition = opening the file with NO share mode (exclusive handle);
      the handle is the lock: process death closes it, the next acquirer
      succeeds, and no stale-marker heuristic is involved;
    * after acquiring, owner-info JSON is written for diagnostics; file
      presence alone never means the resource is locked.
    """

    def __init__(self, lock_dir, resource, owner, timeout_s=15.0,
                 poll_interval=0.05):
        self._dir = lock_dir
        self._resource = str(resource)
        self._name = _sanitize_resource(resource) + ".lock"
        self._owner = owner
        self._timeout = float(timeout_s)
        self._interval = float(poll_interval)
        self._fd = None

    @property
    def path(self):
        return os.path.join(self._dir, self._name)

    def __enter__(self):
        return self.acquire()

    def acquire(self, cancel_event=None):
        """Block up to timeout_s for exclusive ownership.

        Raises LockBusy when the deadline passes and OperationCancelled when
        the cancel event fires while waiting.
        """
        import msvcrt
        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        deadline = time.monotonic() + self._timeout
        os.makedirs(self._dir, exist_ok=True)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise OperationCancelled()
            handle = k32.CreateFileW(
                self.path, 0xC0000000 | 0x40000000, 0, None, 4, 0x80, None)
            # OPEN_ALWAYS=4, share mode 0: success means exclusive ownership.
            if handle != wintypes.HANDLE(-1).value:
                break
            err = ctypes.GetLastError()
            if err != 32:  # ERROR_SHARING_VIOLATION is the only expected loss
                raise OperationError(
                    "lock_open_failed",
                    "console lock open failed (winerror=%d)" % err)
            if time.monotonic() >= deadline:
                raise LockBusy(self._resource)
            time.sleep(self._interval)
        try:
            self._fd = msvcrt.open_osfhandle(handle, os.O_RDWR)
        except OSError:
            k32.CloseHandle(handle)
            raise
        info = json.dumps({
            "pid": os.getpid(), "owner": self._owner,
            "resource": self._resource,
            "created_epoch": round(time.time(), 3),
        }, ensure_ascii=False).encode("utf-8")
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.truncate(self._fd, 0)
            os.write(self._fd, info)
        except OSError:
            pass  # diagnostics only; the handle is the lock
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        return False


class ActionSpec:
    """One whitelisted console action.

    ``enabled`` is an explicit production fact; disabled actions carry a
    human-readable reason and can never execute (no fake success path).
    """

    def __init__(self, name, description, handler=None, resource_fn=None,
                 param_schema=None, enabled=True, disabled_reason=None,
                 bump_generation_on_success=False, cancelable=True):
        self.name = name
        self.description = description
        self.handler = handler
        self.resource_fn = resource_fn
        self.param_schema = param_schema or {}
        self.enabled = enabled
        self.disabled_reason = disabled_reason
        self.bump_generation_on_success = bump_generation_on_success
        self.cancelable = cancelable


class OperationContext:
    """Adapter-facing handle for one running operation."""

    def __init__(self, op_id, cancel_event, report_step):
        self.op_id = op_id
        self.cancel_event = cancel_event
        self._report_step = report_step

    def report_step(self, note):
        if not isinstance(note, str) or not note.strip():
            raise ValueError("step note must be a non-empty string")
        self._report_step(note[:MAX_STEP_NOTE_CHARS])

    def raise_if_cancelled(self):
        if self.cancel_event.is_set():
            raise OperationCancelled()


class OperationManager:
    """Owns operation state, resource locks, idempotency and persistence.

    Threading model: one worker per operation from a bounded pool; the
    resource file lock serializes work on the same resource across processes.
    State transitions are guarded UPDATEs, so cancel racing with the worker
    can only finish an operation once.
    """

    def __init__(self, db_path, registry, lock_dir, acquire_timeout_s=15.0,
                 max_workers=4):
        self._db_path = db_path
        self._lock_dir = lock_dir
        self._acquire_timeout = float(acquire_timeout_s)
        self._specs = {spec.name: spec for spec in registry}
        self._state_lock = threading.RLock()
        self._db_lock = threading.RLock()
        self._cancel_events = {}
        self._generations = {}
        os.makedirs(lock_dir, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False,
                                   isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._init_db()
        except Exception:
            # Fail-closed must not leak the handle: a half-open manager would
            # keep the database file locked (WinError 32) for everyone else.
            try:
                self._db.close()
            except sqlite3.Error:
                pass
            raise
        interrupted = self._mark_interrupted_on_start()
        if interrupted:
            print("[console] marked %d unfinished operation(s) interrupted"
                  % interrupted, flush=True)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="console-op")

    def _create_schema(self):
        self._db.executescript(
            "CREATE TABLE IF NOT EXISTS console_meta("
            " key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS operations("
            " id TEXT PRIMARY KEY,"
            " action TEXT NOT NULL,"
            " target TEXT NOT NULL DEFAULT '',"
            " params_json TEXT NOT NULL DEFAULT '{}',"
            " state TEXT NOT NULL,"
            " idempotency_key TEXT UNIQUE,"
            " resource TEXT NOT NULL,"
            " generation INTEGER NOT NULL DEFAULT 0,"
            " created_at REAL NOT NULL,"
            " started_at REAL,"
            " finished_at REAL,"
            " result_json TEXT,"
            " error_json TEXT,"
            " steps_json TEXT NOT NULL DEFAULT '[]',"
            " cancel_requested INTEGER NOT NULL DEFAULT 0);")
        self._db.execute(
            "INSERT OR REPLACE INTO console_meta(key, value)"
            " VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))

    def _init_db(self):
        # Absent tables are a legal first run (create schema); genuine
        # corruption or foreign tables fail closed and keep the old file.
        try:
            tables = {r[0] for r in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        except sqlite3.DatabaseError as exc:
            raise ConsoleStateError(
                "console state database unreadable: %s" % type(exc).__name__)
        if "console_meta" not in tables:
            if tables - {"operations"}:
                raise ConsoleStateError(
                    "console state database contains foreign tables; "
                    "refusing to touch it")
            self._create_schema()
            return
        try:
            row = self._db.execute(
                "SELECT value FROM console_meta WHERE key='schema_version'"
            ).fetchone()
            version = int(row[0])
        except sqlite3.DatabaseError as exc:
            raise ConsoleStateError(
                "console state database unreadable: %s" % type(exc).__name__)
        except (TypeError, ValueError):
            raise ConsoleStateError("console state schema version is unreadable")
        if version != SCHEMA_VERSION:
            raise ConsoleStateError(
                "console state schema %s is incompatible with %d; refusing to"
                " write (existing database kept)" % (version, SCHEMA_VERSION))
        self._create_schema()

    def _mark_interrupted_on_start(self):
        with self._db_lock:
            cur = self._db.execute(
                "UPDATE operations SET state='interrupted', finished_at=?,"
                " error_json=? WHERE state IN ('queued','running')",
                (time.time(), json.dumps({
                    "code": "interrupted_by_restart",
                    "message": "service restart interrupted the operation"})))
            return cur.rowcount

    # ------------------------------------------------------------- registry

    def action_info(self):
        out = []
        for name in sorted(self._specs):
            spec = self._specs[name]
            out.append({
                "action": spec.name,
                "enabled": bool(spec.enabled),
                "disabled_reason": spec.disabled_reason,
                "cancelable": spec.cancelable,
                "description": spec.description,
                # Rev9 A9-8: additive param_schema (bounded JSON-safe copy;
                # existing consumers keep working, the frontend gains the
                # bounded parameter definitions instead of guessing).
                "param_schema": _bounded_param_schema(spec.param_schema),
            })
        return out

    # --------------------------------------------------------------- queries

    def get_operation(self, op_id):
        with self._db_lock:
            row = self._db.execute(
                "SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
        return self._view(row) if row else None

    def list_operations(self, limit=50):
        limit = max(1, min(int(limit), 200))
        with self._db_lock:
            rows = self._db.execute(
                "SELECT * FROM operations"
                " ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,)).fetchall()
        return [self._view(row) for row in rows]

    def _view(self, row):
        def load(column):
            raw = row[column]
            if raw is None:
                return None
            try:
                return json.loads(raw)
            except ValueError:
                return None
        return {
            "id": row["id"], "action": row["action"], "target": row["target"],
            "state": row["state"], "resource": row["resource"],
            "generation": row["generation"],
            "created_at": row["created_at"], "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "cancel_requested": bool(row["cancel_requested"]),
            "steps": load("steps_json") or [],
            "result": load("result_json"),
            "error": load("error_json"),
            "idempotency_key": row["idempotency_key"],
        }

    # ---------------------------------------------------------------- submit

    def submit(self, action, target="", params=None, idempotency_key=None):
        if not isinstance(action, str) or action not in self._specs:
            raise OperationError("invalid_action", "unknown action %r" % (action,))
        spec = self._specs[action]
        if not spec.enabled:
            raise OperationUnavailable(spec.name, spec.disabled_reason
                                       or "action is disabled")
        if idempotency_key is not None and (
                not isinstance(idempotency_key, str)
                or not 1 <= len(idempotency_key) <= 128):
            raise OperationError(
                "invalid_idempotency_key", "idempotency_key must be 1-128 chars")
        target = self._validate_target(spec, target)
        params = self._validate_params(spec, params)
        resource = spec.resource_fn(target, params)
        with self._state_lock:
            if idempotency_key:
                existing = self._find_by_idempotency(idempotency_key)
                if existing is not None:
                    if (existing["action"] != action
                            or existing["target"] != target):
                        raise OperationError(
                            "idempotency_key_conflict",
                            "idempotency key already used for a different"
                            " action/target")
                    view = self._view(existing)
                    view["deduplicated"] = True
                    return view
            op_id = uuid.uuid4().hex
            generation = self._generations.get(resource, 0)
            with self._db_lock:
                self._db.execute(
                    "INSERT INTO operations(id, action, target, params_json,"
                    " state, idempotency_key, resource, generation, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (op_id, action, target,
                     json.dumps(params, ensure_ascii=False),
                     "queued", idempotency_key, resource, generation,
                     time.time()))
            self._cancel_events[op_id] = threading.Event()
        self._executor.submit(self._worker, op_id)
        return self.get_operation(op_id)

    def _validate_params(self, spec, params):
        params = params or {}
        if not isinstance(params, dict):
            raise OperationError("invalid_params", "params must be an object")
        try:
            raw = json.dumps(params, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError):
            raise OperationError("invalid_params", "params must be finite JSON")
        if len(raw.encode("utf-8")) > MAX_PARAM_BYTES:
            raise OperationError("invalid_params", "params too large")
        schema = spec.param_schema or {}
        unknown = set(params) - set(schema)
        if unknown:
            raise OperationError(
                "invalid_params", "unknown param(s): %s" % sorted(unknown))
        for key, value in params.items():
            rule = schema.get(key, {})
            if value is None and rule.get("nullable") is True:
                continue
            expected = rule.get("type")
            if expected == "str":
                if not isinstance(value, str):
                    raise OperationError("invalid_params",
                                         "%s must be a string" % key)
                if len(value) > rule.get("max_len", 128):
                    raise OperationError("invalid_params", "%s too long" % key)
            elif expected == "int":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise OperationError("invalid_params",
                                         "%s must be an int" % key)
                if "min" in rule and value < rule["min"]:
                    raise OperationError("invalid_params", "%s out of range" % key)
                if "max" in rule and value > rule["max"]:
                    raise OperationError("invalid_params", "%s out of range" % key)
            elif expected == "bool":
                if not isinstance(value, bool):
                    raise OperationError("invalid_params",
                                         "%s must be a bool" % key)
            elif expected == "object":
                if not isinstance(value, dict):
                    raise OperationError("invalid_params",
                                         "%s must be an object" % key)
            else:
                raise OperationError("invalid_params",
                                     "invalid rule for %s" % key)
        return params

    def _validate_target(self, spec, target):
        if target is None:
            target = ""
        if not isinstance(target, str):
            raise OperationError("invalid_target", "target must be a string")
        if not spec.resource_fn:
            return ""
        if not target or len(target) > 64:
            raise OperationError("invalid_target", "target must be 1-64 chars")
        return target

    def _find_by_idempotency(self, key):
        with self._db_lock:
            return self._db.execute(
                "SELECT * FROM operations WHERE idempotency_key=?",
                (key,)).fetchone()

    # ---------------------------------------------------------------- cancel

    def cancel(self, op_id):
        with self._state_lock:
            with self._db_lock:
                row = self._db.execute(
                    "SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
            if row is None:
                raise OperationError("operation_not_found",
                                     "no operation %r" % op_id)
            if row["state"] in TERMINAL_STATES:
                raise OperationError(
                    "operation_already_finished",
                    "operation already finished (%s)" % row["state"])
            with self._db_lock:
                self._db.execute(
                    "UPDATE operations SET cancel_requested=1 WHERE id=?",
                    (op_id,))
            if row["state"] == "queued":
                self._finish_locked(
                    op_id, "cancelled",
                    error={"code": "cancelled",
                           "message": "cancelled while queued; never executed"})
            event = self._cancel_events.get(op_id)
            if event is not None:
                event.set()
        return self.get_operation(op_id)

    # ------------------------------------------------------------ generation

    def generation_of(self, resource):
        with self._state_lock:
            return self._generations.get(resource, 0)

    def bump_generation(self, resource):
        """Invalidate queued operations for the resource; a running task keeps
        its captured generation and its late completion is discarded."""
        with self._state_lock:
            nxt = self._generations.get(resource, 0) + 1
            self._generations[resource] = nxt
            self._db.execute(
                "UPDATE operations SET state='cancelled', finished_at=?,"
                " error_json=? WHERE state='queued' AND resource=?"
                " AND generation<?",
                (time.time(), json.dumps({
                    "code": "superseded",
                    "message": "superseded by a newer operation on the same"
                               " resource"}), resource, nxt))
            return nxt

    # ---------------------------------------------------------------- worker

    def _worker(self, op_id):
        event = self._cancel_events.get(op_id)
        lock = None
        try:
            with self._db_lock:
                row = self._db.execute(
                    "SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
            if row is None or row["state"] != "queued":
                return
            spec = self._specs[row["action"]]
            lock = ResourceFileLock(
                self._lock_dir, row["resource"],
                owner="console-op:%s" % op_id,
                timeout_s=self._acquire_timeout)
            lock.acquire(cancel_event=event)
            if not self._persist_state(op_id, "running",
                                       started_at=time.time()):
                return  # cancelled/finished while waiting for the lock
            ctx = OperationContext(
                op_id, event, lambda note: self._add_step(op_id, note))
            try:
                result = spec.handler(row["target"],
                                      json.loads(row["params_json"]), ctx)
            except OperationCancelled:
                self._finish_state(op_id, "cancelled", error={
                    "code": "cancelled",
                    "message": "adapter honoured the cancel request"})
                return
            self._complete(op_id, spec, result)
        except OperationCancelled:
            self._finish_state(op_id, "cancelled", error={
                "code": "cancelled", "message": "cancelled before execution"})
        except LockBusy:
            self._finish_state(op_id, "failed", error={
                "code": "resource_busy_timeout",
                "message": "resource lock wait exceeded %.1fs"
                           % self._acquire_timeout})
        except OperationError as exc:
            self._finish_state(op_id, "failed", error={
                "code": exc.code, "message": _single_line(exc.message)})
        except Exception as exc:  # adapter bugs must not hang the pool silently
            self._finish_state(op_id, "failed", error={
                "code": "internal_error",
                "message": _single_line("%s: %s" % (type(exc).__name__, exc))})
        finally:
            try:
                lock.__exit__(None, None, None)
            except Exception:
                pass
            with self._state_lock:
                self._cancel_events.pop(op_id, None)

    def _complete(self, op_id, spec, result):
        """Record success with late-result protection.

        A stale generation discards the result: old callbacks must not write
        back after the resource moved on. A completed-but-cancel-requested
        task keeps its result and records that the cancel arrived too late.
        """
        with self._state_lock:
            with self._db_lock:
                row = self._db.execute(
                    "SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
            if row is None or row["state"] != "running":
                print("[console] discarded late result for %s (state=%s)"
                      % (op_id, row["state"] if row else "missing"), flush=True)
                return
            if row["generation"] != self._generations.get(row["resource"], 0):
                self._finish_locked(op_id, "cancelled", error={
                    "code": "superseded",
                    "message": "resource changed; late result discarded"})
                return
            if isinstance(result, dict):
                result = {str(k): v for k, v in result.items()}
            else:
                result = {"value": result}
            if row["cancel_requested"]:
                self._add_step(
                    op_id, "cancel was requested but the operation finished")
            self._finish_locked(op_id, "succeeded", result=result)
            if spec.bump_generation_on_success:
                self._bump_locked(row["resource"])

    def _add_step(self, op_id, note):
        with self._state_lock:
            with self._db_lock:
                row = self._db.execute(
                    "SELECT steps_json FROM operations WHERE id=?",
                    (op_id,)).fetchone()
                if row is None:
                    return
                try:
                    steps = json.loads(row["steps_json"]) if row["steps_json"] else []
                except ValueError:
                    steps = []
                steps.append({"at": round(time.time(), 3), "note": note})
                self._db.execute(
                    "UPDATE operations SET steps_json=? WHERE id=?",
                    (json.dumps(steps[-MAX_STEPS:], ensure_ascii=False), op_id))

    # ------------------------------------------------------ state transitions

    def _persist_state(self, op_id, state, started_at=None, result=None,
                       error=None, finished_at=None):
        """Guarded transition: only queued/running rows may move on."""
        with self._state_lock:
            with self._db_lock:
                cur = self._db.execute(
                    "UPDATE operations SET state=?,"
                    " started_at=COALESCE(?, started_at),"
                    " result_json=?, error_json=?, finished_at=?"
                    " WHERE id=? AND state IN ('queued','running')",
                    (state, started_at,
                     json.dumps(result, ensure_ascii=False)
                     if result is not None else None,
                     json.dumps(error, ensure_ascii=False)
                     if error is not None else None,
                     finished_at, op_id))
                return cur.rowcount == 1

    def _finish_state(self, op_id, state, result=None, error=None):
        if state not in TERMINAL_STATES:
            raise ValueError("not a terminal state: %r" % state)
        return self._persist_state(
            op_id, state, result=result, error=error, finished_at=time.time())

    def _finish_locked(self, op_id, state, result=None, error=None):
        """Terminal transition; caller holds _state_lock."""
        return self._persist_state(
            op_id, state, result=result, error=error, finished_at=time.time())

    def _bump_locked(self, resource):
        """Generation bump; caller holds _state_lock."""
        nxt = self._generations.get(resource, 0) + 1
        self._generations[resource] = nxt
        self._db.execute(
            "UPDATE operations SET state='cancelled', finished_at=?,"
            " error_json=? WHERE state='queued' AND resource=? AND generation<?",
            (time.time(), json.dumps({
                "code": "superseded",
                "message": "superseded by a newer operation on the same"
                           " resource"}), resource, nxt))

    def shutdown(self, wait=True):
        self._executor.shutdown(wait=wait)

    def close(self, wait=True):
        """Shut the worker pool down and release the database handle.

        Safe to call more than once; used by tests to release the sqlite file
        before temporary directories are removed.
        """
        self.shutdown(wait=wait)
        with self._db_lock:
            try:
                self._db.close()
            except sqlite3.ProgrammingError:
                pass


def _bounded_param_schema(schema):
    """JSON-safe bounded copy of one ActionSpec param_schema (Rev9 A9-8).

    The console action catalog publishes each action's parameter definition so
    the frontend does not have to guess. The copy is restricted to at most 32
    keys and the whitelist fields type/min/max/max_len — a JSON scalar only,
    NEVER a function object or an arbitrary nested value.

    Defined at the END of the module on purpose: the action catalog consumers
    (and the line-number citation pins in tests/test_share_gate_citations.py)
    read this file top-down, and appending keeps every existing line number
    stable. ``action_info`` resolves the name at call time.
    """
    out = {}
    if not isinstance(schema, dict):
        return out
    for key in list(schema)[:32]:
        rule = schema.get(key)
        if not isinstance(rule, dict):
            continue
        entry = {}
        for field in ("type", "min", "max", "max_len"):
            value = rule.get(field)
            if isinstance(value, bool):
                continue
            if isinstance(value, str):
                entry[field] = value[:64]
            elif isinstance(value, (int, float)):
                entry[field] = value
        if entry:
            out[str(key)[:64]] = entry
    return out
