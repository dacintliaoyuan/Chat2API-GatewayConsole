# -*- coding: utf-8 -*-
"""Local composition root (E19-1-08).

This module assembles ONLY the local application:

* the statistics read environment built from the explicit ``LocalConfig``;
* the local UM runtime through the configured runtime control;
* the ``OperationManager`` with the local-only registry (``model.catalog``);
* the loopback HTTP listener with the local routes.

It never imports ``console_adapters``, ``share_management``, ``share_store``,
``share_entry``, ``share_dispatch``, ``console_display_proxy`` or
``usagesrv``: the local application owns its own routing, authentication,
configuration and lifecycle. The only ``share_*`` import is the control-token
gate helper (``share_secret_protection``), which the isolated local package
carries. The HTTP listener binds loopback only and every management request
must carry the control token; ``/health`` is the only unauthenticated route.

``start()`` is the only place that creates directories or starts a child
process. ``--check-config`` never reaches it: configuration validation and the
runtime-info print happen before any port is bound.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import console_stats
import model_sync
from console_operations import (OperationError, OperationManager,
                               OperationUnavailable)

from .config import LocalConfigError
from .registry import build_registry
from .service_runtime import build_runtime_control
from .stats_env import build_stats_environment

from console_backend_app import (
    BackendHandler, BackendHTTPServer,
    append_bounded_log as _append_bounded_log,
    has_proxy_metadata as _has_proxy_metadata,
    hostname_of as _hostname_of,
    pid_alive as _pid_alive,
    process_creation_epoch as _process_creation_epoch,
    read_control_token as _read_control_token,
    read_pid_file as _read_pid_file,
    read_pid_identity as _read_pid_identity,
    remove_pid_file as _remove_pid_file,
    write_pid_file as _write_pid_file,
)

LOOPBACK_HOSTS = frozenset(("127.0.0.1", "::1", "localhost"))
PROXY_METADATA_HEADERS = ("Forwarded", "X-Forwarded-For",
                          "X-Forwarded-Host", "X-Forwarded-Proto",
                          "X-Real-IP")
MAX_BODY_BYTES = 64 * 1024
OP_ID_MAX = 64
ACTION_MAX = 64
TARGET_MAX = 64
IDEMPOTENCY_KEY_MAX = 128
LOCAL_ACTIONS = ("model.catalog",)
# The UM maintenance table reads these original console actions only; a
# non-UM action (model.speedtest, share.*) is never mixed in.
UM_MAINTENANCE_ACTIONS = frozenset((
    "account.um_status", "account.um_verify", "account.login",
    "service.start", "service.stop", "service.restart"))
# E19-1-04 step 7: the session bootstrap cookie. The code file is consumed
# once; the cookie is HttpOnly/Strict and bounded by the TTL.
SESSION_COOKIE = "um_session"
SESSION_TTL_SECONDS = 12 * 3600
SESSION_CODE_MAX = 256
# Bounded append log (step 7): one 1 MiB file, rotated by truncation.
LOG_FILE_NAME = "local-app.log"
LOG_MAX_BYTES = 1024 * 1024
# D18-10: a stop drains the in-flight requests with a BOUND instead of waiting
# forever or cutting them off.
SHUTDOWN_DRAIN_TIMEOUT_S = 10.0
# standards-lifecycle-07: a pid file records the owner's creation time next to
# the PID, so a RECYCLED pid is recognised as stale instead of refusing every
# restart. The tolerance absorbs the clock's rounding on the two APIs.
PID_IDENTITY_TOLERANCE_S = 2.0
# A pid file with unreadable content that is younger than this belongs to a
# starter that has not finished writing it; an older one is abandoned state.
PID_FILE_STALE_AFTER_S = 30.0


def _url_host(host):
    return "[%s]" % host if ":" in host and not host.startswith("[") else host


def _catalog_host(host):
    """model_sync only accepts IP-literal endpoints; localhost is IPv4 here."""
    return "127.0.0.1" if host == "localhost" else host


def _port_open(port, host="127.0.0.1", timeout=0.25):
    """Read-only loopback probe: is something listening on this port?

    Used by the services read face to report the ONE service this composition
    root owns. A closed port means the instance is not listening (not "healthy"),
    and an unreachable probe never fabricates a running row.
    """
    import socket
    try:
        with socket.create_connection((_catalog_host(host), int(port)),
                                      timeout=timeout):
            return True
    except OSError:
        return False


def _operation_error_status(exc):
    code = getattr(exc, "code", "")
    if code == "invalid_action":
        return code, 404
    if code == "action_disabled":
        return code, 403
    if code in ("invalid_params", "invalid_target", "invalid_idempotency_key",
                 "invalid_resource"):
        return code, 400
    if code == "idempotency_key_conflict":
        return code, 409
    return "console_error", 500



# The bounded-drain server is shared; this name is the role's handle.
_LocalHTTPServer = BackendHTTPServer


class _LocalHandler(BackendHandler):
    server_version = "LocalConsole/1.0"

    # ------------------------------------------------------------- plumbing
    def _local_segments(self, path):
        """The route segments, with the documented prefix aliases applied.

        spec-local-004: the plan requires the host to EITHER implement the
        ``/gw/usagesrv/api`` and ``/gw/usagesrv/api/console`` prefixes OR
        point the frontend's basePath at the real local path. The frontend still
        hard-codes the prefix, so the host maps it here: both prefixed forms
        resolve to the SAME local routes (the alias is a rewrite, not a second
        implementation).
        """
        segments = [part for part in path.split("/") if part]
        prefix = ["gw", "usagesrv", "api"]
        if segments[:len(prefix)] == prefix:
            # ``/gw/usagesrv/api/...`` maps onto the local ``/api/...``
            # routes; ``/gw/usagesrv/api/console/...`` maps onto
            # ``/api/console/...``. The ``console`` segment stays where it
            # is, so both prefixed forms resolve to the SAME routes.
            segments = ["api"] + segments[len(prefix):]
        return segments




    def _control_ok(self):
        """Either the admin control token OR a valid browser session.

        spec-local-003: the plan's §2.2 offers two contracts — (a) the old one
        (console GET needs no credential) or (b) a cookie session the host really
        reads and validates. The host used to force ``X-Admin-Control`` on every
        console read face while the frontend never sends that header, and
        ``POST /api/session`` only WROTE a ``Set-Cookie`` with no read branch.
        This is (b): the one-time bootstrap code buys a bounded session cookie,
        and the read faces accept it. The admin control token still works for
        programmatic callers.
        """
        if self._session_ok():
            return True
        expected = _read_control_token(self.app.config.control_token_file)
        supplied = self.headers.get("X-Admin-Control")
        return bool(expected) and bool(supplied) \
            and secrets.compare_digest(supplied, expected)

    def _session_ok(self):
        """A valid, unexpired session cookie (bounded, constant-time compare)."""
        session = self.app.session
        if not session or not session.get("id"):
            return False
        if self.app.clock() > session.get("expires", 0):
            return False
        raw = self.headers.get("Cookie")
        if not raw:
            return False
        try:
            jar = SimpleCookie()
            jar.load(raw)
            morsel = jar.get(SESSION_COOKIE)
        except Exception:  # noqa: BLE001 - an unreadable cookie is a refusal
            return False
        return bool(morsel) and secrets.compare_digest(
            morsel.value, session["id"])

    # --------------------------------------------------------------- routes
    def do_GET(self):
        if not self._request_allowed():
            self.close_connection = True
            self._send_json(403, {"error": "local-only service"})
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/health":
            self._send_json(200, {"status": "ok", "role": "local",
                                  "pid": os.getpid()})
            return
        # D18-10: once the stop is ordered, every non-health read is refused
        # BEFORE it can touch the runtime/manager that is being torn down.
        if self.app.is_stopping():
            self.close_connection = True
            self._send_json(503, {"error": "shutting_down"})
            return
        if not self._control_ok():
            self.close_connection = True
            self._send_json(401, {"error": "control token required"})
            return
        segments = self._local_segments(path)
        if segments == ["api", "summary"]:
            self._send_summary(parsed.query)
            return
        if segments == ["api", "console", "actions"]:
            # The local registry owns ONE action; a share action must never
            # appear here (R19-1). The shape is the frontend contract:
            # ``{"actions": [...]}`` (normalize.ts normalizeCapabilities
            # requires an object with an ``actions`` row array), never a bare
            # array.
            self._send_json(200, {"actions": self.app.manager.action_info()})
            return
        if len(segments) == 4 and segments[:3] == [
                "api", "console", "operations"]:
            self._send_operation(segments[3])
            return
        if segments == ["api", "console", "operations"]:
            self._send_operations(parsed.query)
            return
        if segments[:3] == ["api", "console", "um"]:
            self._send_um_read_face(segments, parsed.query)
            return
        if segments == ["api", "console", "services"]:
            self._send_services()
            return
        if segments == ["api", "console", "gateway-connection"]:
            self._send_gateway_connection(parsed.query)
            return
        self._send_json(404, {"error": "unknown path"})

    def do_POST(self):
        if not self._request_allowed():
            self.close_connection = True
            self._send_json(403, {"error": "local-only service"})
            return
        path = urlparse(self.path).path
        if path == "/admin/shutdown":
            if not self._control_ok():
                self.close_connection = True
                self._send_json(401, {"error": "control token required"})
                return
            try:
                self._read_body()
            except ValueError as exc:
                self.close_connection = True
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, {"status": "shutting_down"})
            threading.Thread(target=self.app.shutdown, daemon=True,
                             name="local-shutdown").start()
            return
        segments = [part for part in path.split("/") if part]
        # D18-10: a new submission is refused once the stop is ordered, so it
        # never runs against a runtime/manager that is already stopping. The
        # session bootstrap stays reachable (it only issues a cookie).
        if segments != ["api", "session"] and self.app.is_stopping():
            self.close_connection = True
            self._send_json(503, {"error": "shutting_down"})
            return
        if segments == ["api", "session"]:
            # The bootstrap code is NOT the control token: it is the one-time
            # human-provided code, so this route must be reachable by a browser
            # that has no admin control token. It is checked BEFORE the
            # control-token gate on purpose.
            try:
                body = self._read_body()
            except ValueError as exc:
                self.close_connection = True
                self._send_json(400, {"error": str(exc)})
                return
            self._send_session(body)
            return
        if not self._control_ok():
            self.close_connection = True
            self._send_json(401, {"error": "control token required"})
            return
        if segments == ["api", "session", "reissue"]:
            self._send_session_reissue()
            return
        if segments != ["api", "console", "operations"]:
            self._send_json(404, {"error": "unknown path"})
            return
        try:
            body = self._read_body()
        except ValueError as exc:
            self.close_connection = True
            self._send_json(400, {"error": str(exc)})
            return
        self._submit_operation(body)

    # -------------------------------------------------------------- handlers
    def _send_summary(self, query):
        """The UM summary read face.

        The frontend's ``readSummary(signal?)`` passes NO date range and
        ``normalizeUmSummary`` reads only ``role``/``assembly.role``/
        ``umReference``, so a bare call must succeed with those facts. A caller
        that DOES pass ``from``/``to`` additionally gets the bounded stats
        (kept for compatibility with the previous contract).
        """
        params = parse_qs(query)
        date_from = params.get("from", [None])[0]
        date_to = params.get("to", [None])[0]
        if not date_from and not date_to:
            self._send_json(200, {
                "role": "local",
                "umReference": self.app.um_reference(),
            })
            return
        if not date_from or not date_to:
            self._send_json(
                400, {"error": "from and to are required together "
                               "(YYYY-MM-DD)"})
            return
        try:
            payload = console_stats.bounded_stats(
                console_stats.default_ledgers(self.app.stats_reader),
                date_from, date_to, sources=["chat2api"],
                environment=self.app.stats_reader)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - never leak details
            print("[local] summary failed: %s" % type(exc).__name__,
                  file=sys.stderr)
            self._send_json(500, {"error": "summary_unavailable"})
        else:
            payload = dict(payload)
            payload["role"] = "local"
            payload.setdefault("umReference", self.app.um_reference())
            self._send_json(200, payload)

    def _send_session(self, body):
        """One-time bootstrap code -> session cookie (usagesrv semantics)."""
        if self._reject_unknown(body, ("code",)):
            return
        code = body.get("code")
        if not isinstance(code, str) or not 1 <= len(code) <= SESSION_CODE_MAX:
            self._send_json(403, {"error": "invalid bootstrap code"})
            return
        if not self.app.consume_session_code(code):
            self._send_json(403, {"error": "invalid bootstrap code"})
            return
        sid = secrets.token_urlsafe(24)
        # spec-local-003 (b): the host records the session it just issued, so
        # the read faces can really validate the cookie instead of only writing
        # one. The session is bounded by the TTL and belongs to this process.
        self.app.session = {"id": sid,
                            "expires": self.app.clock() + SESSION_TTL_SECONDS}
        self._send_json_with_cookie(200, {"ok": True}, sid)

    def _send_session_reissue(self):
        """Regenerate and persist the bootstrap code (needs the control token)."""
        code = self.app.reissue_session_code()
        if code is None:
            self._send_json(500, {"error": "session_code_unavailable"})
            return
        self._send_json(200, {"ok": True, "code": code})

    def _send_json_with_cookie(self, status, payload, sid):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Set-Cookie",
            "%s=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d"
            % (SESSION_COOKIE, sid, SESSION_TTL_SECONDS))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _send_operation(self, op_id):
        if not op_id or len(op_id) > OP_ID_MAX:
            self._send_json(400, {"error": "invalid operation id"})
            return
        operation = self.app.manager.get_operation(op_id)
        if operation is None:
            self._send_json(404, {"error": "unknown operation"})
            return
        self._send_json(200, {"operation": operation})

    # --------------------------------------------------- frontend read faces
    # spec-local-002 / A9: the frontend's nine ``read*`` methods each need a
    # local route. These handlers are thin: the read logic stays in the product
    # modules (``console_stats`` / ``console_log_reader`` / ``usagesrv`` /
    # ``console_gateway_connection``) and this host only wires the explicit
    # environment. The previous version served three routes and answered
    # ``404 unknown path`` for the other nine, which the plan's A9 explicitly
    # no longer permits.

    def _read_query(self, query, cap=120):
        return parse_qs(query)

    def _send_operations(self, query):
        params = self._read_query(query)
        try:
            limit = int((params.get("limit") or ["50"])[0])
        except (TypeError, ValueError):
            self._send_json(400, {"error": "invalid limit"})
            return
        if not 1 <= limit <= 200:
            self._send_json(400, {"error": "limit must be 1..200"})
            return
        rows = self.app.manager.list_operations(limit=limit)
        self._send_json(200, {"operations": rows})

    def _send_services(self):
        """The services read face, from the LOCAL runtime facts only.

        ``console_adapters.service_status`` is a forbidden host module for this
        composition root (it imports ``console_share_production`` and runs the
        PowerShell wrapper). The local host knows exactly ONE service it owns: the
        UM runtime it started. The entry reports that service's real state from
        the loopback probe, and never invents a row for a source it does not
        assemble (R19-1).
        """
        um = self.app.config.um_runtime
        reachable = _port_open(um.port, um.host)
        services = [{
            "key": "chat2api",
            "display_name": "chat2api（本机 UM runtime）",
            "port": um.port,
            "state": "running" if reachable else "stopped",
            "healthy": True if reachable else None,
            "detail": "" if reachable
            else "loopback 端口未监听（实例可能未就绪）",
            "process_ids": [],
        }]
        self._send_json(200, {
            "ok": True,
            "services": services,
            "meta": {"generated_epoch": int(time.time() * 1000),
                       "scope": "local-um-runtime"},
        })

    def _send_gateway_connection(self, query):
        params = self._read_query(query)
        service = (params.get("service") or [""])[0]
        if not service:
            self._send_json(400, {"error": "invalid_service"})
            return
        try:
            import console_gateway_connection
            payload = console_gateway_connection.connection_info(service)
        except Exception as exc:  # noqa: BLE001 - reported, never silent
            name = type(exc).__name__
            if name == "RetiredServiceError":
                self._send_json(410, {"error": "source_retired",
                                       "service": service})
                return
            if name == "ValueError":
                self._send_json(400, {"error": "invalid_service",
                                       "service": service})
                return
            print("[local] gateway connection read failed: %s" % name,
                  file=sys.stderr)
            self._send_json(502, {"error": "gateway_connection_failed"})
            return
        self._send_json(200, payload)

    def _send_um_maintenance(self, query):
        """UM maintenance actions from the LOCAL operation records."""
        params = self._read_query(query)
        try:
            limit = int((params.get("limit") or ["50"])[0])
        except (TypeError, ValueError):
            self._send_json(400, {"error": "invalid limit"})
            return
        try:
            rows = self.app.manager.list_operations(
                max(1, min(limit, 200)))
        except Exception as exc:  # noqa: BLE001 - reported, never silent
            print("[local] maintenance read failed: %s"
                  % type(exc).__name__, file=sys.stderr)
            self._send_json(502, {"error": "maintenance_unavailable"})
            return
        selected = [row for row in rows
                    if isinstance(row, dict)
                    and (row.get("action") in UM_MAINTENANCE_ACTIONS
                         or (str(row.get("action", "")).split(".")[0]
                             == "service"
                             and str(row.get("target", "")) == "chat2api"))]
        self._send_json(200, {
            "rows": selected,
            "scope": "UM 维护操作（原操作记录，最多 200 条）",
            "earlier": None,
            "coverage": {"status": "ok",
                          "operations_read": len(rows),
                          "rows_returned": len(selected),
                          "bounded": True},
        })

    def _send_um_read_face(self, segments, query):
        """The five UM read faces (today/trend/requests/logs/request-logs)."""
        # ``api/console/um/<face>`` is FOUR segments.
        if len(segments) != 4:
            self._send_json(404, {"error": "unknown path"})
            return
        params = self._read_query(query)

        def _q(key, cap=120):
            return (params.get(key) or [""])[0][:cap] or None

        def _qint(key, default):
            try:
                return int((params.get(key) or [str(default)])[0])
            except (TypeError, ValueError):
                raise ValueError("%s must be an integer" % key)

        sub = segments[3]
        try:
            if sub == "today":
                payload = console_stats.um_today()
            elif sub == "trend":
                payload = console_stats.um_trend(
                    range=_q("range") or "7d")
            elif sub == "requests":
                payload = console_stats.um_requests(
                    window=_q("window") or "today",
                    model=_q("model"),
                    result=_q("result") or "all",
                    search=_q("search") or "",
                    cursor=_q("cursor", 512),
                    limit=_qint("limit", 50))
            elif sub == "logs":
                import console_log_reader
                levels = [v.strip() for v in
                          (_q("levels") or "").split(",") if v.strip()]
                payload = console_log_reader.read_um_logs(
                    window=_q("window") or "today",
                    levels=levels,
                    source=_q("source") or "service",
                    cursor=_q("cursor", 512),
                    limit=_qint("limit", 80),
                    ledger_paths=console_stats.default_ledgers())
                if payload.get("status") == "error":
                    self._send_json(400, {"error": "invalid_um_query",
                                           "detail": payload.get("error")})
                    return
            elif sub == "request-logs":
                import console_log_reader
                payload = console_log_reader.read_um_request_logs(
                    _q("request_id") or "",
                    limit=_qint("limit", 100),
                    ledger_paths=console_stats.default_ledgers())
            elif sub == "maintenance":
                self._send_um_maintenance(query)
                return
            else:
                self._send_json(404, {"error": "unknown path"})
                return
        except ValueError as exc:
            self._send_json(400, {"error": "invalid_um_query",
                                   "detail": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - reported, never silent
            print("[local] UM read face failed: %s"
                  % type(exc).__name__, file=sys.stderr)
            self._send_json(502, {"error": "um_query_failed"})
            return
        self._send_json(200, payload)

    def _submit_operation(self, body):
        if self._reject_unknown(
                body, ("action", "target", "params", "idempotency_key")):
            return
        action = body.get("action")
        target = body.get("target") or ""
        params = body.get("params")
        idempotency_key = body.get("idempotency_key")
        if not isinstance(action, str) or not 1 <= len(action) <= ACTION_MAX:
            self._send_json(400, {"error": "invalid action"})
            return
        if action not in LOCAL_ACTIONS:
            # The local registry owns only model.catalog; a share action is
            # refused before the manager looks it up.
            self._send_json(404, {"error": "invalid_action"})
            return
        if not isinstance(target, str) or len(target) > TARGET_MAX:
            self._send_json(400, {"error": "invalid target"})
            return
        if idempotency_key is not None and (
                not isinstance(idempotency_key, str)
                or not 1 <= len(idempotency_key) <= IDEMPOTENCY_KEY_MAX):
            self._send_json(400, {"error": "invalid idempotency_key"})
            return
        try:
            operation = self.app.manager.submit(
                action, target=target, params=params,
                idempotency_key=idempotency_key)
        except OperationUnavailable as exc:
            self._send_json(403, {"error": exc.code, "reason": exc.reason})
            return
        except OperationError as exc:
            code, status = _operation_error_status(exc)
            self._send_json(status, {"error": code})
            return
        except Exception:  # noqa: BLE001 - never leak details or secrets
            self._send_json(500, {"error": "internal_error"})
            return
        self._send_json(200 if operation.get("deduplicated") else 201,
                        {"operation": operation})


class LocalApp:
    """The local composition root: stats + UM runtime + operations + HTTP."""

    def __init__(self, config, *, runtime_control, stats_reader,
                 clock=time.time, manager=None):
        self.config = config
        self.runtime_control = runtime_control
        self.stats_reader = stats_reader
        self.clock = clock
        self.manager = manager
        self._lock = threading.Lock()
        self._server = None
        self._thread = None
        self._closed = threading.Event()
        self._close_done = threading.Event()
        # D18-10: a stop first refuses new work, then drains the in-flight
        # requests with a bound, and only then stops the runtime. The flag is
        # checked by the handler BEFORE a request is accepted, so a request that
        # arrives during the stop window is refused instead of running against a
        # runtime that is already being torn down.
        self._stopping = threading.Event()
        # Only a runtime THIS instance started is ever stopped (ownership).
        self._owns_runtime = False
        # An exception in the listener thread must reach the entry's exit code.
        self._failure = None
        # spec-local-003 (b): the ONE browser session this host issued. It is
        # bounded by SESSION_TTL_SECONDS and replaced by each new exchange.
        self.session = None

    # -------------------------------------------------------------- lifecycle
    def start(self):
        with self._lock:
            if self._server is not None:
                raise RuntimeError("local app is already started")
            if self._closed.is_set():
                raise RuntimeError(
                    "local app was closed and cannot be restarted")
            try:
                os.makedirs(self.config.home_dir, exist_ok=True)
                os.makedirs(self.config.state_root, exist_ok=True)
                os.makedirs(self.config.log_dir, exist_ok=True)
                os.makedirs(self.config.lock_dir, exist_ok=True)
            except OSError as exc:
                raise RuntimeError(
                    "local state directories are not usable: %s" % exc)
            if _read_control_token(self.config.control_token_file) is None:
                raise RuntimeError(
                    "local control token is missing or unreadable: %s"
                    % self.config.control_token_file)
            # E19-1-04 step 7: the pid file is a real mutual-exclusion fact.
            _write_pid_file(self.config.pid_file, os.getpid())
            self._log("start pid=%d port=%d"
                      % (os.getpid(), self.config.port))
            try:
                return self._start_listening()
            except Exception:
                _remove_pid_file(self.config.pid_file, os.getpid())
                raise

    def _start_listening(self):
        um = self.config.um_runtime
        um_host = _catalog_host(um.host)
        model_sync.configure_runtime(
            data_dir=self.config.model_sync_dir,
            um_models_url="http://%s:%d/v1/models"
            % (_url_host(um_host), um.port),
            um_chat_url="http://%s:%d/v1/chat/completions"
            % (_url_host(um_host), um.port))
        console_stats.configure(self.stats_reader)
        try:
            if self.runtime_control.start() is False:
                raise RuntimeError("local UM runtime did not start")
            self._owns_runtime = True
            if self.manager is None:
                self.manager = OperationManager(
                    db_path=self.config.console_state_file,
                    registry=build_registry(),
                    lock_dir=self.config.lock_dir)
            try:
                server = _LocalHTTPServer(
                    (self.config.host, self.config.port), _LocalHandler)
            except OSError as exc:
                # A bind failure is a startup failure with a named reason, not
                # a raw OSError escaping the composition root.
                raise RuntimeError(
                    "local listener bind failed: %s" % exc) from exc
        except Exception:
            # Roll back every resource acquired above. ``self.manager`` is reset
            # so a retry builds a FRESH manager instead of reusing a closed one
            # (a closed SQLite handle made every later request fail).
            self._stop_runtime()
            self._close_manager()
            self.manager = None
            self._server = None
            raise
        server.app = self
        self._server = server
        self._thread = threading.Thread(
            target=self._serve, name="local-console", daemon=True)
        # standards-lifecycle-03: ``Thread.start()`` runs AFTER the socket is
        # bound and AFTER the runtime started, so a failure here must roll both
        # back. Without this the listener stayed open (``self._server`` was
        # already assigned) and ``close()`` blocked forever in
        # ``server.shutdown()``, which waits for a ``serve_forever`` that never
        # ran.
        try:
            self._thread.start()
        except Exception:
            self._thread = None
            self._server = None
            try:
                server.server_close()
            except OSError:
                pass
            self._stop_runtime()
            self._close_manager()
            self.manager = None
            raise
        return self

    def _serve(self):
        """Run the listener loop and RECORD a crash for the entry's exit code."""
        server = self._server
        try:
            server.serve_forever()
        except BaseException as exc:  # noqa: BLE001 - re-raised in close()
            self._failure = exc
            raise
        finally:
            # A listener loop that exits while the app was never asked to close
            # is a crash: the entry must not report success.
            if not self._closed.is_set() and self._failure is None:
                self._failure = RuntimeError("local listener loop exited")

    def serve_forever(self):
        thread = self._thread
        if thread is None:
            raise RuntimeError("local app is not started")
        # ``start`` already runs the listener in its own thread; this call
        # only waits for it so a second serve_forever loop never races the
        # first one on the same socket.
        thread.join()
        if self._failure is not None:
            raise self._failure

    def is_stopping(self):
        """True once a stop was requested; the handler refuses new work."""
        return self._stopping.is_set()

    def shutdown(self):
        """Request shutdown from a handler thread; safe to call twice."""
        if not self._claim_close():
            self._close_done.wait(timeout=60)
            return
        self._finish()

    def close(self):
        """Close after ``serve_forever`` returned (safe no-op shutdown)."""
        if not self._claim_close():
            self._close_done.wait(timeout=60)
            return
        self._finish()

    @property
    def address(self):
        server = self._server
        if server is None:
            return (self.config.host, self.config.port)
        return server.server_address[:2]

    # --------------------------------------------------------------- helpers
    def _claim_close(self):
        with self._lock:
            if self._closed.is_set():
                return False
            self._closed.set()
            # From this instant the app refuses new work: the stop is ordered
            # BEFORE the runtime/manager are torn down.
            self._stopping.set()
            return True

    def _log(self, line):
        """Bounded append log under ``log_dir`` (never a request line/body)."""
        _append_bounded_log(
            os.path.join(self.config.log_dir, LOG_FILE_NAME),
            "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                       line))

    def um_reference(self):
        """The UM endpoint facts the summary read face publishes."""
        um = self.config.um_runtime
        return {
            "baseUrl": um.base_url,
            "port": um.port,
            "source": "local-um-runtime",
        }

    def consume_session_code(self, code):
        """Consume the one-time bootstrap code (the file is cleared on match).

        A missing/unreadable file is a refusal (never an empty expected value);
        the comparison is constant-time and the file is removed BEFORE the caller
        returns success, so a replay cannot pass twice.
        """
        if not isinstance(code, str) or not code:
            return False
        try:
            expected = Path(self.config.session_code_file).read_text(
                encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return False
        if not expected or not secrets.compare_digest(code, expected):
            return False
        try:
            Path(self.config.session_code_file).unlink()
        except OSError:
            pass
        self._log("session code consumed")
        return True

    def reissue_session_code(self):
        """Generate and persist a new bootstrap code; None when unwritable."""
        code = secrets.token_urlsafe(24)
        try:
            path = Path(self.config.session_code_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC,
                             0o600)
                try:
                    os.write(fd, (code + "\n").encode("utf-8"))
                finally:
                    os.close(fd)
            else:
                path.write_text(code + "\n", encoding="utf-8")
        except OSError as exc:
            print("[local] session code reissue failed: %s" % exc,
                  file=sys.stderr)
            return None
        self._log("session code reissued")
        return code

    def _stop_runtime(self):
        # Ownership: an instance that never started the runtime (start() was
        # refused, or the runtime was not reached) must never stop someone
        # else's process.
        if not self._owns_runtime:
            return
        # standards-lifecycle-02/16: a failed stop is RAISED (after the caller
        # released the other resources), never printed and swallowed. A caller
        # that reports success while the runtime is still running is a lie.
        self.runtime_control.stop()

    def _close_manager(self):
        manager = self.manager
        if manager is None:
            return
        try:
            # D18-10: the stop is BOUNDED. An unbounded wait made
            # /admin/shutdown never return while a long operation ran.
            manager.close(wait=False)
        except TypeError:
            # A manager seam without the ``wait`` keyword still closes.
            manager.close()
        except Exception as exc:  # noqa: BLE001 - reported, never silent
            print("[local] operation manager close failed: %s" % exc,
                  file=sys.stderr)

    def _finish(self):
        # Stop order (D18-10): refuse new work (already set in
        # ``_claim_close``), stop the listener so no new request is accepted,
        # drain the in-flight requests with a BOUND, and only then stop the
        # runtime/manager. ``shutdown()`` is only safe once the serve loop is
        # running: a listener thread that crashed before entering
        # ``serve_forever`` would block it forever.
        server = self._server
        if server is not None:
            if self._thread is not None and self._thread.is_alive():
                server.shutdown()
            server.server_close()
            drained = server.drain(SHUTDOWN_DRAIN_TIMEOUT_S)
            if not drained:
                print("[local] in-flight requests did not finish within %.1fs"
                      % SHUTDOWN_DRAIN_TIMEOUT_S, file=sys.stderr)
        failure = None
        try:
            self._close_manager()
        except Exception as close_exc:  # noqa: BLE001 - raised below
            failure = close_exc
        try:
            self._stop_runtime()
        except Exception as stop_exc:  # noqa: BLE001 - raised below
            if failure is None:
                failure = stop_exc
        finally:
            self._log("stop pid=%d" % os.getpid())
            _remove_pid_file(self.config.pid_file, os.getpid())
            self._close_done.set()
        if failure is not None:
            raise failure


def build_app(config):
    """Assemble the local application from the explicit configuration."""
    stats_reader = build_stats_environment(config)
    runtime_control = build_runtime_control(config)
    return LocalApp(config, runtime_control=runtime_control,
                    stats_reader=stats_reader)


def main(config, *, check_config=False):
    """Run the local composition root from an already-validated config."""
    if check_config:
        print(json.dumps(config.runtime_info(), ensure_ascii=False))
        return 0
    app = None
    exit_code = 0
    try:
        app = build_app(config)
        app.start()
        app.serve_forever()
    except KeyboardInterrupt:
        pass
    except (LocalConfigError, OSError, ValueError, RuntimeError) as exc:
        import traceback
        traceback.print_exc()
        print("local composition root failed: %s" % exc, file=sys.stderr)
        exit_code = 1
    finally:
        if app is not None:
            # The close path must never mask the real failure: a stop error
            # is reported, but the caller still sees the ORIGINAL reason and
            # exit code.
            try:
                app.close()
            except Exception as close_exc:  # noqa: BLE001 - reported
                print("[local] close failed: %s" % close_exc, file=sys.stderr)
                if exit_code == 0:
                    exit_code = 1
    return exit_code
