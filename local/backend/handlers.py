# -*- coding: utf-8 -*-
"""Local role HTTP handlers.

The request guard, route matching, health answer, stopping/auth order and the
bounded body read live in ``app.console_backend_app.RoleHandler``. This module
keeps only the local product handlers: the UM summary/session/read faces and the
local operation registry face. The local composition root declares the route table
that points at these methods.
"""
from __future__ import annotations

import json
import secrets
import sys
import time
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

from console import stats as console_stats
from console.operations import OperationError, OperationUnavailable

from app.console_backend_app import RoleHandler, read_control_token

# E19-1-04 step 7: the session bootstrap cookie. The code file is consumed
# once; the cookie is HttpOnly/Strict and bounded by the TTL.
SESSION_COOKIE = "um_session"
SESSION_TTL_SECONDS = 12 * 3600
SESSION_CODE_MAX = 256
OP_ID_MAX = 64
ACTION_MAX = 64
TARGET_MAX = 64
IDEMPOTENCY_KEY_MAX = 128


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


class LocalHandler(RoleHandler):
    server_version = "LocalConsole/1.0"

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
        expected = read_control_token(self.app.config.control_token_file)
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

    # -------------------------------------------------------------- handlers
    def _send_actions(self):
        # The local registry owns ONE action; a share action must never appear
        # here (R19-1). The shape is the frontend contract:
        # ``{"actions": [...]}`` (normalize.ts normalizeCapabilities requires
        # an object with an ``actions`` row array), never a bare array.
        self._send_json(200, {"actions": self.app.manager.action_info()})

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

    def _send_operation(self, segments):
        op_id = segments[3]
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
    # ``404 unknown path`` for the other nine, which the plan's A9 explicitly no
    # longer permits.

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
            from console import gateway_connection as console_gateway_connection
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
        maintenance_actions = self.app.profile.capabilities.maintenance_actions
        selected = [row for row in rows
                    if isinstance(row, dict)
                    and (row.get("action") in maintenance_actions
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
                from console import log_reader as console_log_reader
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
                from console import log_reader as console_log_reader
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
        if action not in self.app.profile.capabilities.actions:
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
