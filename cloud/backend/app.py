# -*- coding: utf-8 -*-
"""Cloud composition root (E19-1-11).

This module assembles ONLY the cloud application:

* the Key API over ``ShareManager`` + the AF_UNIX ``BrokerClientAdapter``;
* the dedicated 8110 ``BackendProcess``;
* the loopback HTTP listener with the cloud management route table.

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
import subprocess
import sys
import time
from pathlib import Path

from share.management import BrokerError, StoreError
# spec-shared-05: ``shared`` is a REQUIRED dependency of BOTH roots, so it is
# imported at module import time exactly like the local host does. The summary
# import used to be deferred into the request, so a missing ``shared`` was
# swallowed into a 500 for one read face while the management host still
# started and served the Key/status write faces.
from shared import pricing_rules  # noqa: F401 - the fail-closed import

from .backend_process import BackendProcess, BackendProcessError
from .config import CloudConfigError
from .handlers import CloudHandler, load_price_table
from .keys_api import KeyApi, KeyApiError

from app.console_backend_app import (
    BackendHTTPServer, RoleApp, RoleCapabilities, RoleProfile, RouteSpec,
    PID_IDENTITY_TOLERANCE_S, SHUTDOWN_DRAIN_TIMEOUT_S,
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

CLOUD_GET_ROUTES = (
    RouteSpec(("api", "summary"), "_send_summary"),
    RouteSpec(("api", "share", "status"), "_send_status",
              capability="keys"),
    RouteSpec(("api", "share", "keys"), "_send_keys", capability="keys"),
    RouteSpec(("api", "share", "keys"), "_send_key", mode="segments",
              match="length", length=4, capability="keys"),
    RouteSpec(("api", "share", "backend"), "_send_backend",
              capability="backend"),
)
CLOUD_POST_ROUTES = (
    RouteSpec(("admin", "shutdown"), "_shutdown",
              auth="control", stopping="allow", body="read"),
    RouteSpec(("api", "share", "keys"), "_create_key", mode="body",
              stopping="after_auth", body="read", capability="keys"),
    RouteSpec(("api", "share", "keys"), "_revoke_key",
              mode="segments_body", match="length", length=5,
              tail="revoke-and-terminate", stopping="after_auth", body="read",
              capability="keys"),
    RouteSpec(("api", "share", "keys"), "_rotate_key",
              mode="segments_body", match="length", length=5,
              tail="rotate", stopping="after_auth", body="read",
              capability="keys"),
    RouteSpec(("api", "share", "drain"), "_drain",
              stopping="after_auth", body="read", capability="drain"),
    RouteSpec(("api", "share", "backend", "start"), "_backend_start",
              stopping="after_auth", body="read", capability="backend"),
    RouteSpec(("api", "share", "backend", "stop"), "_backend_stop",
              stopping="after_auth", body="read", capability="backend"),
)
CLOUD_GET_FALLBACK = RouteSpec(
    (), "_send_unknown", auth="control", stopping="before_auth")
CLOUD_POST_FALLBACK = RouteSpec(
    (), "_send_unknown", auth="control", stopping="after_auth", body="read")

CLOUD_CAPABILITIES = RoleCapabilities(
    features=frozenset(("keys", "backend", "drain")),
)


class CloudApp(RoleApp):
    """The cloud composition root: keys + backend + loopback HTTP listener."""

    def __init__(self, config, *, keys=None, backend=None, clock=time.time):
        super().__init__(config, clock=clock)
        self.keys = keys if keys is not None else KeyApi(config)
        self.backend = (backend if backend is not None
                        else BackendProcess(config))
        self.broker_process = None
        # D18-10: only a backend THIS instance started is ever stopped
        # (ownership), and a crash in the listener thread reaches the entry's
        # exit code.
        self._owns_backend = False

    # -------------------------------------------------------------- lifecycle
    def _start_role(self):
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
                (self.config.host, self.config.port),
                CLOUD_PROFILE.handler_class)
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
        try:
            return self._start_server_thread(server)
        except Exception:
            self._stop_backend()
            raise

    def _stop_role(self):
        failure = None
        try:
            self._stop_backend()
        except Exception as stop_exc:  # noqa: BLE001 - raised below
            failure = stop_exc
        finally:
            broker = self.broker_process
            if broker is not None and broker.poll() is None:
                broker.terminate()
                try:
                    broker.wait(timeout=15)
                except Exception:  # noqa: BLE001 - never block the close
                    broker.kill()
        if failure is not None:
            raise failure

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


CLOUD_PROFILE = RoleProfile(
    role="cloud",
    app_class=CloudApp,
    handler_class=CloudHandler,
    get_routes=CLOUD_GET_ROUTES,
    post_routes=CLOUD_POST_ROUTES,
    get_fallback=CLOUD_GET_FALLBACK,
    post_fallback=CLOUD_POST_FALLBACK,
    capabilities=CLOUD_CAPABILITIES,
    log_file_name=LOG_FILE_NAME,
    broker_starter=_start_broker,
)
CloudApp.profile = CLOUD_PROFILE


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
    broker = (CLOUD_PROFILE.broker_starter(config, host_command)
               if start_broker else None)
    app = CLOUD_PROFILE.app_class(config, keys=keys, backend=backend)
    app.broker_process = broker
    return app


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
