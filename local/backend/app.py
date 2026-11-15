# -*- coding: utf-8 -*-
"""Local composition root (E19-1-08).

This module assembles ONLY the local application:

* the statistics read environment built from the explicit ``LocalConfig``;
* the local UM runtime through the configured runtime control;
* the ``OperationManager`` with the local-only registry (``model.catalog``);
* the loopback HTTP listener with the local route table.

It never imports ``console_adapters``, ``share_management``, ``share_store``,
``share_entry``, ``console_display_proxy`` or
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
import time
from pathlib import Path

from console import stats as console_stats
from model_sync import sync as model_sync
from console.operations import OperationManager

from .config import LocalConfigError
from .handlers import LocalHandler
from .registry import build_registry
from .service_runtime import build_runtime_control
from .stats_env import build_stats_environment

from app.console_backend_app import (
    BackendHTTPServer, RoleApp, RoleCapabilities, RoleProfile, RouteSpec,
    PID_FILE_STALE_AFTER_S,
    SHUTDOWN_DRAIN_TIMEOUT_S,
    read_pid_file as _read_pid_file,
    write_pid_file as _write_pid_file,
)

# Test seams: tests/test_r19_local_app.py drives the pid-file and drain
# constants through this composition module, and local/tests reads the drain
# budget here. Listed explicitly so the re-export is not an accidental import.
__all__ = [
    "PID_FILE_STALE_AFTER_S",
    "SHUTDOWN_DRAIN_TIMEOUT_S",
    "_read_pid_file",
    "_write_pid_file",
]

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
# non-UM action (share.*) is never mixed in.
UM_MAINTENANCE_ACTIONS = frozenset((
    "account.um_status", "account.um_verify", "account.login",
    "service.start", "service.stop", "service.restart"))
# Bounded append log (step 7): one 1 MiB file, rotated by truncation.
LOG_FILE_NAME = "local-app.log"
LOG_MAX_BYTES = 1024 * 1024

# The bounded-drain server is shared; this name is the role's handle.
_LocalHTTPServer = BackendHTTPServer


def _url_host(host):
    return "[%s]" % host if ":" in host and not host.startswith("[") else host


def _catalog_host(host):
    """model_sync only accepts IP-literal endpoints; localhost is IPv4 here."""
    return "127.0.0.1" if host == "localhost" else host


LOCAL_GET_ROUTES = (
    RouteSpec(("api", "summary"), "_send_summary", mode="query"),
    RouteSpec(("api", "console", "actions"), "_send_actions",
              capability="operations"),
    RouteSpec(("api", "console", "operations"), "_send_operations",
              mode="query", capability="operations"),
    RouteSpec(("api", "console", "operations"), "_send_operation",
              mode="segments", match="length", length=4,
              capability="operations"),
    RouteSpec(("api", "console", "um"), "_send_um_read_face",
              mode="segments_query", match="prefix", capability="um"),
    RouteSpec(("api", "console", "services"), "_send_services",
              capability="services"),
    RouteSpec(("api", "console", "gateway-connection"),
              "_send_gateway_connection", mode="query",
              capability="gateway_connection"),
)
LOCAL_POST_ROUTES = (
    RouteSpec(("admin", "shutdown"), "_shutdown",
              auth="control", stopping="allow", body="read"),
    RouteSpec(("api", "session"), "_send_session", mode="body",
              auth="public", stopping="allow", body="read",
              capability="session"),
    RouteSpec(("api", "session", "reissue"), "_send_session_reissue",
              auth="control", stopping="before_auth",
              capability="session"),
    RouteSpec(("api", "console", "operations"), "_submit_operation",
              mode="body", auth="control", stopping="before_auth",
              body="read", capability="operations"),
)
LOCAL_GET_FALLBACK = RouteSpec(
    (), "_send_unknown", auth="control", stopping="before_auth")
LOCAL_POST_FALLBACK = RouteSpec(
    (), "_send_unknown", auth="control", stopping="before_auth")

LOCAL_CAPABILITIES = RoleCapabilities(
    features=frozenset(("session", "operations", "um", "services",
                        "gateway_connection")),
    actions=LOCAL_ACTIONS,
    maintenance_actions=UM_MAINTENANCE_ACTIONS,
)


class LocalApp(RoleApp):
    """The local composition root: stats + UM runtime + operations + HTTP."""

    def __init__(self, config, *, runtime_control, stats_reader,
                 clock=time.time, manager=None):
        super().__init__(config, clock=clock)
        self.runtime_control = runtime_control
        self.stats_reader = stats_reader
        self.manager = manager
        # Only a runtime THIS instance started is ever stopped (ownership).
        self._owns_runtime = False
        # spec-local-003 (b): the ONE browser session this host issued. It is
        # bounded by SESSION_TTL_SECONDS and replaced by each new exchange.
        self.session = None

    # -------------------------------------------------------------- lifecycle
    def _start_role(self):
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
                    (self.config.host, self.config.port),
                    LOCAL_PROFILE.handler_class)
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
        try:
            return self._start_server_thread(server)
        except Exception:
            self._stop_runtime()
            self._close_manager()
            self.manager = None
            raise

    def _stop_role(self):
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
        if failure is not None:
            raise failure

    # --------------------------------------------------------------- helpers
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


LOCAL_PROFILE = RoleProfile(
    role="local",
    app_class=LocalApp,
    handler_class=LocalHandler,
    get_routes=LOCAL_GET_ROUTES,
    post_routes=LOCAL_POST_ROUTES,
    get_fallback=LOCAL_GET_FALLBACK,
    post_fallback=LOCAL_POST_FALLBACK,
    capabilities=LOCAL_CAPABILITIES,
    log_file_name=LOG_FILE_NAME,
    get_path_aliases=(
        (("gw", "usagesrv", "api"), ("api",)),
    ),
)
LocalApp.profile = LOCAL_PROFILE


def build_app(config):
    """Assemble the local application from the explicit configuration."""
    stats_reader = build_stats_environment(config)
    runtime_control = build_runtime_control(config)
    return LOCAL_PROFILE.app_class(config, runtime_control=runtime_control,
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
