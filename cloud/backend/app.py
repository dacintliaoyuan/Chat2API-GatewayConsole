# -*- coding: utf-8 -*-
"""Cloud composition root (E19-1-11).

This module assembles ONLY the cloud application:

* the Key API over ``ShareManager`` + the AF_UNIX ``BrokerClientAdapter``;
* the dedicated 8110 ``BackendProcess``;
* the loopback HTTP listener with the cloud management routes.

It never imports ``usagesrv``, ``console_adapters``, ``local`` or
``model_sync``: the cloud application owns its own routing, authentication,
configuration and lifecycle. The HTTP listener binds loopback only and every
management request must carry the control token; ``/health`` is the only
unauthenticated route. A full start is POSIX-only — on Windows the entry
honestly returns exit 3 (``requires POSIX``) instead of pretending to run the
Linux chain.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from share.management import (BrokerError, Conflict, Disabled, NotFound,
                             StoreError, VersionMismatch)
# spec-shared-05: ``shared`` is a REQUIRED dependency of BOTH roots, so it is
# imported at module import time exactly like the local host does. The summary
# import used to be deferred into the request, so a missing ``shared`` was
# swallowed into a 500 for one read face while the management host still
# started and served the Key/status write faces.
from shared import pricing_rules  # noqa: F401 - the fail-closed import

from .backend_process import BackendProcess, BackendProcessError
from .config import CloudConfigError
from .keys_api import KeyApi, KeyApiError

from app.console_backend_app import (
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

# The bounded-drain server is shared; this name is the role's handle.
_CloudHTTPServer = BackendHTTPServer

LOOPBACK_HOSTS = frozenset(("127.0.0.1", "::1", "localhost"))
PROXY_METADATA_HEADERS = ("Forwarded", "X-Forwarded-For",
                          "X-Forwarded-Host", "X-Forwarded-Proto",
                          "X-Real-IP")
MAX_BODY_BYTES = 64 * 1024
KEY_ID_MAX = 128
NAME_MAX = 64
IDEMPOTENCY_KEY_MAX = 128
# E19-1-06 step 7: bounded append log (one 1 MiB file).
LOG_FILE_NAME = "cloud-app.log"
LOG_MAX_BYTES = 1024 * 1024
# D18-10: a stop drains the in-flight requests with a BOUND instead of waiting
# forever or cutting them off.
SHUTDOWN_DRAIN_TIMEOUT_S = 10.0
# standards-lifecycle-07: a pid file records the owner's creation time next to
# the PID, so a RECYCLED pid is recognised as stale instead of refusing every
# restart. The tolerance absorbs the clock's rounding on the two APIs.
PID_IDENTITY_TOLERANCE_S = 2.0



def load_price_table(path):
    """Read one explicit price table document (never a repository default).

    Kept in the host (the cloud root), not in ``shared.pricing_rules``: the
    shared module must stay a pure rule set with no file access.

    The document is NORMALIZED with the same pure rule the local stats reader
    uses (``shared.pricing_rules.normalize_price_table``). Handing the raw
    document to ``build_cloud_summary`` made the two ends disagree on the SAME
    file: a raw ``data/prices.json`` has no top-level ``versioned``/
    ``versionMeta``/``meta`` keys, so the cloud side reported
    ``versioned: false`` and picked the 1970 segment as "current" while the
    local side reported the 2026-09-29 segment.
    """
    import json
    from shared import pricing_rules
    text = Path(path).read_text(encoding="utf-8")
    table = json.loads(text)
    if not isinstance(table, dict):
        raise ValueError("price table must be a JSON object")
    return pricing_rules.normalize_price_table(
        table, now=time.time())







class _CloudHandler(BackendHandler):
    server_version = "CloudConsole/1.0"

    # --------------------------------------------------------------- routes
    def do_GET(self):
        if not self._request_allowed():
            self.close_connection = True
            self._send_json(403, {"error": "local-only service"})
            return
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(200, {"status": "ok", "role": "cloud",
                                  "pid": os.getpid()})
            return
        # D18-10: once the stop is ordered, every non-health read is refused
        # BEFORE it can touch the backend/broker being torn down.
        if self.app.is_stopping():
            self.close_connection = True
            self._send_json(503, {"error": "shutting_down"})
            return
        if not self._control_ok():
            self.close_connection = True
            self._send_json(401, {"error": "control token required"})
            return
        segments = [part for part in path.split("/") if part]
        if segments == ["api", "summary"]:
            self._send_summary()
            return
        if segments == ["api", "share", "status"]:
            self._send_status()
            return
        if segments == ["api", "share", "keys"]:
            self._send_keys()
            return
        if len(segments) == 4 and segments[:3] == ["api", "share", "keys"]:
            self._send_key(segments[3])
            return
        if segments == ["api", "share", "backend"]:
            self._dispatch(lambda: self.app.backend.status())
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
                             name="cloud-shutdown").start()
            return
        if not self._control_ok():
            self.close_connection = True
            self._send_json(401, {"error": "control token required"})
            return
        # D18-10: once the stop is ordered, a new write is refused BEFORE it
        # can spawn/stop a backend or reach the broker.
        if self.app.is_stopping():
            self.close_connection = True
            self._send_json(503, {"error": "shutting_down"})
            return
        segments = [part for part in path.split("/") if part]
        try:
            body = self._read_body()
        except ValueError as exc:
            self.close_connection = True
            self._send_json(400, {"error": str(exc)})
            return
        if segments == ["api", "share", "keys"]:
            self._create_key(body)
            return
        if len(segments) == 5 and segments[:3] == ["api", "share", "keys"] \
                and segments[4] == "revoke-and-terminate":
            self._revoke_key(segments[3], body)
            return
        if len(segments) == 5 and segments[:3] == ["api", "share", "keys"] \
                and segments[4] == "rotate":
            self._rotate_key(segments[3], body)
            return
        if segments == ["api", "share", "drain"]:
            self._drain()
            return
        if segments == ["api", "share", "backend", "start"]:
            self._backend_start()
            return
        if segments == ["api", "share", "backend", "stop"]:
            self._backend_stop()
            return
        self._send_json(404, {"error": "unknown path"})

    # -------------------------------------------------------------- handlers
    def _dispatch(self, call):
        try:
            self._send_json(200, call())
        except KeyApiError as exc:
            self._send_json(exc.status, {"error": exc.code})
        except NotFound:
            self._send_json(404, {"error": "not_found"})
        except (Conflict, Disabled, VersionMismatch):
            self._send_json(409, {"error": "conflict"})
        except BrokerError as exc:
            self._send_json(503, {"error": getattr(exc, "code", None)
                                  or "share_broker_unavailable"})
        except StoreError:
            self._send_json(400, {"error": "invalid_request"})
        except (OSError, ValueError):
            self._send_json(400, {"error": "invalid_request"})
        except BackendProcessError:
            self._send_json(503, {"error": "share_backend_unavailable"})
        except Exception:  # noqa: BLE001 - never leak details or secrets
            self._send_json(500, {"error": "internal_error"})

    def _send_summary(self):
        """The cloud summary read face (E19-1-06 step 11).

        The price table is INJECTED from ``CloudConfig.price_table_file``; a
        deployment without one answers 503 ``summary_unavailable`` instead of
        inventing prices. The local ledgers are never read.
        """
        path = self.app.config.price_table_file
        if not path:
            self._send_json(503, {"error": "summary_unavailable",
                                   "reason": "price_table_file_not_configured"})
            return
        try:
            from .summary import build_cloud_summary
            table = load_price_table(path)
            payload = build_cloud_summary(table)
        except (OSError, ValueError):
            self._send_json(503, {"error": "summary_unavailable",
                                   "reason": "price_table_unreadable"})
            return
        except Exception:  # noqa: BLE001 - never leak details
            self._send_json(500, {"error": "internal_error"})
            return
        self._send_json(200, payload)

    def _send_status(self):
        def call():
            payload = dict(self.app.keys.status())
            payload.setdefault(
                "config_generation", payload.get("desired_generation"))
            payload["backend"] = self.app.backend.status()
            return payload
        self._dispatch(call)

    def _send_keys(self):
        self._dispatch(lambda: self.app.keys.list())

    def _send_key(self, key_id):
        if not key_id or len(key_id) > KEY_ID_MAX:
            self._send_json(400, {"error": "invalid key_id"})
            return
        self._dispatch(lambda: self.app.keys.get(key_id))

    def _create_key(self, body):
        if self._reject_unknown(
                body, ("name", "max_concurrency", "idempotency_key")):
            return
        name = body.get("name")
        if not isinstance(name, str) or not 1 <= len(name) <= NAME_MAX:
            self._send_json(400, {"error": "invalid name"})
            return
        idempotency_key = body.get("idempotency_key")
        if idempotency_key is not None and (
                not isinstance(idempotency_key, str)
                or not 1 <= len(idempotency_key) <= IDEMPOTENCY_KEY_MAX):
            self._send_json(400, {"error": "invalid idempotency_key"})
            return
        max_concurrency = body.get("max_concurrency")
        if max_concurrency is not None and (
                isinstance(max_concurrency, bool)
                or not isinstance(max_concurrency, int)
                or not 1 <= max_concurrency <= 48):
            self._send_json(
                400, {"error": "max_concurrency must be an integer in 1..48"})
            return
        try:
            payload = self.app.keys.create(
                name, max_concurrency=max_concurrency,
                idempotency_key=idempotency_key)
        except KeyApiError as exc:
            self._send_json(exc.status, {"error": exc.code})
        except NotFound:
            self._send_json(404, {"error": "not_found"})
        except (Conflict, Disabled, VersionMismatch):
            self._send_json(409, {"error": "conflict"})
        except BrokerError as exc:
            self._send_json(503, {"error": getattr(exc, "code", None)
                                  or "share_broker_unavailable"})
        except StoreError:
            self._send_json(400, {"error": "invalid_request"})
        except (OSError, ValueError):
            self._send_json(400, {"error": "invalid_request"})
        except Exception:  # noqa: BLE001 - never leak the secret/body
            self._send_json(500, {"error": "internal_error"})
        else:
            self._send_json(201, payload)

    def _revoke_key(self, key_id, body):
        if self._reject_unknown(
                body, ("expected_row_version", "idempotency_key")):
            return
        if not key_id or len(key_id) > KEY_ID_MAX:
            self._send_json(400, {"error": "invalid key_id"})
            return
        expected = body.get("expected_row_version")
        if isinstance(expected, bool) or not isinstance(expected, int) \
                or expected < 1:
            self._send_json(
                400, {"error": "expected_row_version must be a positive "
                              "integer"})
            return
        idempotency_key = body.get("idempotency_key")
        if idempotency_key is not None and (
                not isinstance(idempotency_key, str)
                or not 1 <= len(idempotency_key) <= IDEMPOTENCY_KEY_MAX):
            self._send_json(400, {"error": "invalid idempotency_key"})
            return
        self._dispatch(lambda: self.app.keys.revoke(
            key_id, expected, idempotency_key=idempotency_key))

    def _rotate_key(self, key_id, body):
        if self._reject_unknown(
                body, ("expected_row_version", "idempotency_key")):
            return
        if not key_id or len(key_id) > KEY_ID_MAX:
            self._send_json(400, {"error": "invalid key_id"})
            return
        expected = body.get("expected_row_version")
        if isinstance(expected, bool) or not isinstance(expected, int) \
                or expected < 1:
            self._send_json(
                400, {"error": "expected_row_version must be a positive "
                              "integer"})
            return
        idempotency_key = body.get("idempotency_key")
        if idempotency_key is not None and (
                not isinstance(idempotency_key, str)
                or not 1 <= len(idempotency_key) <= IDEMPOTENCY_KEY_MAX):
            self._send_json(400, {"error": "invalid idempotency_key"})
            return
        self._dispatch(lambda: self.app.keys.rotate(
            key_id, expected, idempotency_key=idempotency_key))

    def _drain(self):
        def call():
            payload = self.app.keys.drain()
            if payload.get("ok") is not True:
                raise KeyApiError(
                    payload.get("error_code") or "share_broker_unavailable",
                    "broker drain was not confirmed", status=503)
            return payload
        self._dispatch(call)

    def _backend_start(self):
        self._dispatch(lambda: {"ok": self.app.backend.start(),
                                "status": self.app.backend.status()})

    def _backend_stop(self):
        self._dispatch(lambda: {"ok": self.app.backend.stop(),
                                "status": self.app.backend.status()})


class CloudApp:
    """The cloud composition root: keys + backend + loopback HTTP listener."""

    def __init__(self, config, *, keys=None, backend=None, clock=time.time):
        self.config = config
        self.keys = keys if keys is not None else KeyApi(config)
        self.backend = (backend if backend is not None
                        else BackendProcess(config))
        self.clock = clock
        self.broker_process = None
        self._lock = threading.Lock()
        self._server = None
        self._thread = None
        self._closed = threading.Event()
        self._close_done = threading.Event()
        # D18-10: a stop refuses new work, drains with a bound, and only then
        # stops the backend/broker. Only a backend THIS instance started is
        # ever stopped (ownership), and a crash in the listener thread reaches
        # the entry's exit code.
        self._stopping = threading.Event()
        self._owns_backend = False
        self._failure = None

    # -------------------------------------------------------------- lifecycle
    def start(self):
        with self._lock:
            if self._server is not None:
                raise RuntimeError("cloud app is already started")
            if self._closed.is_set():
                raise RuntimeError(
                    "cloud app was closed and cannot be restarted")
            # E19-1-06 step 7: every configured state root is really
            # consumed.
            try:
                os.makedirs(self.config.home_dir, exist_ok=True)
                os.makedirs(self.config.state_root, exist_ok=True)
                os.makedirs(self.config.log_dir, exist_ok=True)
                os.makedirs(self.config.lock_dir, exist_ok=True)
            except OSError as exc:
                raise RuntimeError(
                    "cloud state directories are not usable: %s" % exc)
            if _read_control_token(self.config.control_token_file) is None:
                raise RuntimeError(
                    "cloud control token is missing or unreadable: %s"
                    % self.config.control_token_file)
            _write_pid_file(self.config.pid_file, os.getpid())
            self._log("start pid=%d port=%d"
                      % (os.getpid(), self.config.port))
            try:
                return self._start_chain()
            except Exception:
                _remove_pid_file(self.config.pid_file, os.getpid())
                raise

    def _start_chain(self):
        # E19-1-06 step 8: the supervision roots come from the explicit
        # config, never from the LOCALAPPDATA / in-package defaults.
        from console.share_production import configure_supervision_paths
        configure_supervision_paths(
            watch_dir=self.config.supervision_watch_dir,
            lock_dir=self.config.supervision_lock_dir)
        try:
            if self.backend.start() is not True:
                raise RuntimeError("dedicated backend did not start")
            self._owns_backend = True
            server = _CloudHTTPServer(
                (self.config.host, self.config.port), _CloudHandler)
        except OSError as exc:
            cleanup = None
            try:
                self._stop_backend()
            except Exception as stop_exc:  # noqa: BLE001 - reported below
                cleanup = "%s: %s" % (type(stop_exc).__name__, stop_exc)
            raise RuntimeError(
                "cloud listener bind failed: %s%s"
                % (exc, "" if cleanup is None
                   else "; backend cleanup failed: " + cleanup))
        except Exception:
            self._stop_backend()
            raise
        server.app = self
        self._server = server
        self._thread = threading.Thread(
            target=self._serve, name="cloud-console", daemon=True)
        # standards-lifecycle-03: ``Thread.start()`` runs AFTER the socket is
        # bound and AFTER the backend started, so a failure here must roll both
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
            self._stop_backend()
            raise
        return self

    def _log(self, line):
        """Bounded append log under ``log_dir`` (never a request line/body)."""
        _append_bounded_log(
            os.path.join(self.config.log_dir, LOG_FILE_NAME),
            "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                        line))

    def serve_forever(self):
        thread = self._thread
        if thread is None:
            raise RuntimeError("cloud app is not started")
        # ``start`` already runs the listener in its own thread; this call only
        # waits for it so a second serve_forever loop never races the first
        # one on the same socket (the local app uses the same contract).
        thread.join()
        if self._failure is not None:
            raise self._failure

    def is_stopping(self):
        """True once a stop was requested; the handler refuses new work."""
        return self._stopping.is_set()

    def shutdown(self):
        """Request shutdown from a handler thread.

        Refuses new work, drains the in-flight requests with a bound, then stops
        the backend and the HTTP listener; safe to call twice.
        """
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
            # BEFORE the backend/broker are torn down.
            self._stopping.set()
            return True

    def _serve(self):
        """Run the listener loop and RECORD a crash for the entry's exit code."""
        server = self._server
        try:
            server.serve_forever()
        except BaseException as exc:  # noqa: BLE001 - re-raised in serve_forever
            self._failure = exc
            raise
        finally:
            # A listener loop that exits while the app was never asked to close
            # is a crash: the entry must not report success.
            if not self._closed.is_set() and self._failure is None:
                self._failure = RuntimeError("cloud listener loop exited")

    def _stop_backend(self):
        # Ownership: an instance that never started the backend must never stop
        # another instance's 8110 process.
        if not self._owns_backend:
            return
        try:
            self.backend.stop()
        except BackendProcessError as exc:
            print("[cloud] backend stop failed: %s" % exc, file=sys.stderr)
            raise

    def _finish(self):
        # Stop order (D18-10): refuse new work (already set in
        # ``_claim_close``), stop the listener, drain the in-flight requests
        # with a BOUND, and only then stop the backend/broker. A failed stop
        # is raised after the resources are released so the entry reports it.
        failure = None
        server = self._server
        if server is not None:
            if self._thread is not None and self._thread.is_alive():
                server.shutdown()
            server.server_close()
            if not server.drain(SHUTDOWN_DRAIN_TIMEOUT_S):
                print("[cloud] in-flight requests did not finish within %.1fs"
                      % SHUTDOWN_DRAIN_TIMEOUT_S, file=sys.stderr)
        try:
            self._stop_backend()
        except Exception as stop_exc:  # noqa: BLE001 - raised below
            failure = stop_exc
        finally:
            broker = self.broker_process
            try:
                if broker is not None and broker.poll() is None:
                    broker.terminate()
                    try:
                        broker.wait(timeout=15)
                    except Exception:  # noqa: BLE001 - never block the close
                        broker.kill()
            finally:
                self._log("stop pid=%d" % os.getpid())
                _remove_pid_file(self.config.pid_file, os.getpid())
                self._close_done.set()
        if failure is not None:
            raise failure


def build_app(config, *, start_broker=False, host_command=None):
    """Assemble the cloud application from the explicit configuration.

    E19-1-06 step 10: the explicit environment is applied ONCE here, BEFORE the
    ``KeyApi``/``ShareManager``/``BrokerClientAdapter`` is constructed, so an
    env-driven deployment module and the explicit ``CloudConfig`` can never
    disagree. ``KeyApi.__init__`` no longer mutates ``os.environ`` itself.

    ``host_command`` is the 8100 gateway argv the broker launches as its
    ``--host-command`` REMAINDER. It is an explicit caller input (the systemd
    unit in production, the chain fixture in verification) because
    ``CloudConfig.broker_args()`` deliberately carries only the broker's own
    flags. Asking for a broker without it fails closed.
    """
    apply_process_env(config)
    keys = KeyApi(config)
    backend = BackendProcess(config)
    broker = (_start_broker(config, host_command) if start_broker else None)
    app = CloudApp(config, keys=keys, backend=backend)
    app.broker_process = broker
    return app


def apply_process_env(config):
    """Apply the explicit ``CloudConfig.process_env()`` mapping once."""
    os.environ.update(config.process_env())


def _start_broker(config, host_command=None):
    """Start the broker child (fixture runtime mode only).

    Production refuses earlier (``--start-broker`` requires
    ``runtime_mode == "fixture"``): in a real deployment systemd owns the same
    binary with the same flags.

    ``config.broker_args()`` returns only the broker's OWN required flags
    (``share/broker_ops.py::linux_main``). ``--host-command`` is a REMAINDER
    that carries the 8100 gateway argv and belongs to the caller — this root
    does not own the 8100 child (the broker does, once started). Without it the
    broker parser exits 2 with ``unrecognized arguments``/``required``, so a
    missing host command is refused here instead of spawning a dead child.
    """
    if not host_command:
        raise BrokerError(
            "cloud broker needs an explicit --host-command (the 8100 gateway "
            "argv); broker_args() carries only the broker's own flags")
    process = subprocess.Popen(
        [sys.executable, *config.broker_args(), "--host-command",
         *[str(part) for part in host_command]],
        cwd=str(Path(__file__).resolve().parents[2]),
        stdin=subprocess.DEVNULL)
    return process


def main(config, *, check_config=False, start_broker=False,
         host_command=None):
    """Run the cloud composition root from an already-validated config."""
    if check_config:
        print(json.dumps(config.runtime_info(), ensure_ascii=False))
        return 0
    if os.name != "posix":
        print("cloud composition root requires POSIX (Linux); refusing to "
              "start on %s" % os.name, file=sys.stderr)
        return 3
    app = None
    exit_code = 0
    try:
        app = build_app(config, start_broker=start_broker,
                        host_command=host_command)
        app.start()
        app.serve_forever()
    except KeyboardInterrupt:
        pass
    except (KeyApiError, BackendProcessError, BrokerError, StoreError,
            CloudConfigError, OSError, ValueError, RuntimeError) as exc:
        print("cloud composition root failed: %s" % exc, file=sys.stderr)
        exit_code = 1
    finally:
        if app is not None:
            # The close path must never mask the real failure: a stop error is
            # reported, but the caller still sees the ORIGINAL reason/exit code.
            try:
                app.close()
            except Exception as close_exc:  # noqa: BLE001 - reported
                print("[cloud] close failed: %s" % close_exc, file=sys.stderr)
                if exit_code == 0:
                    exit_code = 1
    return exit_code
