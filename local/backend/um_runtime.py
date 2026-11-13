# -*- coding: utf-8 -*-
"""Local UM runtime owner (E19-1-08).

``UmRuntime`` starts the in-package ``chat2api.py`` as a private child process:

* every path, port, endpoint and token comes from the explicit
  ``UmRuntimeConfig`` — no environment fallback and no repository default. The
  upstream token is passed through ``CHAT2API_UPSTREAM_TOKEN`` in the child
  environment, never in ``argv``;
* the child environment is built from scratch: only the package root
  ``PYTHONPATH``, the explicit ``UmRuntimeConfig.home_dir`` as
  ``HOME``/``USERPROFILE`` (with ``tmp`` beside it for ``TMPDIR``/``TEMP``/
  ``TMP``), the ``CHAT2API_*`` paths, ``CHAT2API_SHARE_BACKEND=0`` and
  ``NO_PROXY``/``no_proxy``. A parent ``CHAT2API_SHARE_BACKEND=1`` or
  ``UM_SHARE_ROLE=cloud`` can never leak into the private runtime;
* ``wait_ready`` polls ``GET /health`` within the configured timeout;
* ``stop`` asks ``POST /admin/shutdown`` with the child's control token, waits
  a bounded time and then terminates; an unconfirmed stop raises instead of
  reporting a fake success.

The production 8000 endpoint is never used: the port is explicit and the local
configuration refuses to reuse it.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

CONSOLE_LOG_NAME = "chat2api.console.log"


class UmRuntimeError(RuntimeError):
    """The local UM runtime could not be confirmed in the wanted state."""


def _read_control_token(path):
    """Read the child control token; any failure returns None (deny)."""
    try:
        from share.secret_protection import read_private_secret_path
        raw = read_private_secret_path(path, max_bytes=1024,
                                       label="control token")
        value = raw.decode("utf-8").strip()
    except (OSError, ValueError, UnicodeError, ImportError):
        return None
    return value or None


def _url_host(host):
    return "[%s]" % host if ":" in host and not host.startswith("[") else host


def _port_is_listening(host, port):
    """True when something already accepts connections on ``host:port``.

    standards-lifecycle-17: ``start`` spawned the child unconditionally. After a
    hard kill the previous runtime keeps listening (nothing reaps it), so a restart
    on the SAME port spawned a second process and ``wait_ready`` could be answered
    by the ORPHAN — the instance reported "started" for a child it does not own
    and then never stopped the orphan. The port is probed before spawning so a
    live listener is refused instead of adopted.
    """
    try:
        connection = socket.create_connection((host, port), timeout=0.5)
    except OSError:
        return False
    try:
        connection.close()
    except OSError:
        pass
    return True


class UmRuntime:
    """Start/stop/status the in-package chat2api child process."""

    def __init__(self, config, *, price_file=None, popen=None,
                 clock=time.monotonic):
        self.config = config
        self.price_file = price_file
        self._popen = popen or subprocess.Popen
        self._clock = clock
        self._process = None
        self._console = None

    # ------------------------------------------------------------ properties
    @property
    def base_url(self):
        return "http://%s:%d" % (_url_host(self.config.host),
                                self.config.port)

    def status(self):
        process = self._process
        alive = process is not None and process.poll() is None
        return {
            "pid": process.pid if process is not None else None,
            "port": self.config.port,
            "alive": alive,
            "base_url": self.base_url,
        }

    # ------------------------------------------------------------- lifecycle
    def _home(self):
        """The explicit isolated HOME (never ``dirname(profile_dir)``).

        ``UmRuntimeConfig.home_dir`` is required and validated to equal or live
        under ``LocalConfig.home_dir``; deriving the child HOME from the profile
        path instead would place ``~/.dsh``/``~/.zcode``/temp wherever the
        operator happened to put the profile.
        """
        return os.path.abspath(self.config.home_dir)

    def _child_env(self):
        """Build the private child environment from scratch (no inheritance)."""
        home = self._home()
        tmp = os.path.join(home, "tmp")
        package_root = os.path.dirname(
            os.path.abspath(self.config.source_dir).rstrip("/\\"))
        env = {}
        for name in ("PATH", "SystemRoot", "WINDIR", "COMSPEC",
                      "PATHEXT", "SYSTEMDRIVE"):
            value = os.environ.get(name)
            if value:
                env[name] = value
        env.update({
            "HOME": home,
            "USERPROFILE": home,
            "TMPDIR": tmp,
            "TEMP": tmp,
            "TMP": tmp,
            "PYTHONPATH": package_root,
            "PYTHONDONTWRITEBYTECODE": "1",
            "CHAT2API_TOKEN_FILE": self.config.token_file,
            "CHAT2API_SERVICE_LOG": self.config.service_log,
            "CHAT2API_USAGE_LOG": self.config.usage_log,
            "CHAT2API_CONTROL_TOKEN_FILE": self.config.control_token_file,
            "CHAT2API_REAUTH_SLOT_FILE": self.config.reauth_slot_file,
            "CHAT2API_PRICE_TABLE_FILE": self.price_file or os.path.join(
                home, "prices.json"),
            # The private role never verifies a share marker; pointing the
            # attribution key at the isolated home keeps even a crafted marker
            # from reading the production key file.
            "CHAT2API_SHARE_ATTRIBUTION_KEY_FILE": os.path.join(
                home, "share-attribution.key"),
            "SHARE_ATTRIBUTION_KEY_FILE": os.path.join(
                home, "share-attribution.key"),
            # Explicitly NOT the share backend: the local runtime must not load
            # share_entry (E19-1-02).
            "CHAT2API_SHARE_BACKEND": "0",
            "CHAT2API_MAX_REQUEST_SECONDS": str(self.config.request_max_s),
            "CHAT2API_AUTH_WAIT_BUDGET_S": str(
                self.config.auth_wait_budget_s),
            # E19-1-04 step 9: the upstream token travels in the environment,
            # never in argv (a process list would expose it to every local
            # account). chat2api's ``main()`` reads this when ``--token`` is
            # absent; the explicit flag keeps priority over it.
            "CHAT2API_UPSTREAM_TOKEN": self.config.upstream_token,
            "NO_PROXY": "*",
            "no_proxy": "*",
        })
        return env

    def _command(self):
        return [
            self.config.python,
            str(Path(self.config.source_dir) / "chat2api.py"),
            "--host", self.config.host,
            "--port", str(self.config.port),
            "--base-url", self.config.base_url,
            "--no-browser",
            "--profile", self.config.profile_dir,
            "--auth-wait-budget-seconds",
            str(self.config.auth_wait_budget_s),
        ]

    def _prepare_directories(self):
        home = self._home()
        for directory in (
                home, os.path.join(home, "tmp"), self.config.profile_dir,
                os.path.dirname(os.path.abspath(self.config.service_log)),
                os.path.dirname(os.path.abspath(self.config.usage_log)),
                os.path.dirname(os.path.abspath(self.config.token_file)),
                os.path.dirname(os.path.abspath(
                    self.config.control_token_file)),
                os.path.dirname(os.path.abspath(
                    self.config.reauth_slot_file))):
            os.makedirs(directory, exist_ok=True)

    def start(self):
        if self._process is not None and self._process.poll() is None:
            raise UmRuntimeError("local UM runtime is already running")
        if not os.path.isfile(self.config.python):
            raise UmRuntimeError(
                "local UM runtime interpreter is missing: %s"
                % self.config.python)
        source = Path(self.config.source_dir) / "chat2api.py"
        if not source.is_file():
            raise UmRuntimeError(
                "local UM runtime source is missing: %s" % source)
        # standards-lifecycle-17: a port that already answers belongs to a
        # process this instance did not start (a runtime orphaned by a hard
        # kill). Spawning here would double-bind on Windows and let
        # ``wait_ready`` report the ORPHAN as this instance's success.
        if _port_is_listening(self.config.host, self.config.port):
            raise UmRuntimeError(
                "local UM runtime port %s:%d is already listening (another "
                "runtime survived a previous run; stop it before restarting)"
                % (self.config.host, self.config.port))
        self._prepare_directories()
        console_path = os.path.join(
            os.path.dirname(os.path.abspath(self.config.service_log)),
            CONSOLE_LOG_NAME)
        self._console = open(console_path, "ab")
        try:
            self._process = self._popen(
                self._command(),
                cwd=os.path.dirname(
                    os.path.abspath(self.config.source_dir).rstrip("/\\")),
                env=self._child_env(),
                stdin=subprocess.DEVNULL,
                stdout=self._console,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            self._console.close()
            self._console = None
            raise UmRuntimeError(
                "local UM runtime failed to start: %s" % exc) from exc
        return True

    def wait_ready(self):
        process = self._process
        if process is None:
            raise UmRuntimeError("local UM runtime is not started")
        deadline = self._clock() + self.config.start_timeout_s
        while self._clock() < deadline:
            if process.poll() is not None:
                raise UmRuntimeError(
                    "local UM runtime exited before readiness (rc=%s)"
                    % process.returncode)
            try:
                status, payload = self._request("GET", "/health")
            except (OSError, ValueError):
                status, payload = None, None
            if status == 200 and isinstance(payload, dict) \
                    and payload.get("status") == "ok":
                return True
            time.sleep(0.05)
        self._terminate()
        raise UmRuntimeError(
            "local UM runtime did not become ready within %.1fs"
            % self.config.start_timeout_s)

    def stop(self):
        process = self._process
        if process is None:
            self._close_console()
            return True
        if process.poll() is None:
            token = _read_control_token(self.config.control_token_file)
            if token:
                try:
                    self._request("POST", "/admin/shutdown", token=token)
                except (OSError, ValueError):
                    pass
            deadline = self._clock() + self.config.stop_timeout_s
            while self._clock() < deadline and process.poll() is None:
                time.sleep(0.05)
            if process.poll() is None:
                self._terminate()
        self._process = None
        self._close_console()
        return True

    def _terminate(self):
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=max(1.0, self.config.stop_timeout_s))
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired as exc:
                    raise UmRuntimeError(
                        "local UM runtime could not be terminated") from exc
        self._process = None
        self._close_console()

    def _close_console(self):
        handle = self._console
        self._console = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def _request(self, method, path, *, token=None):
        connection = http.client.HTTPConnection(
            self.config.host, self.config.port, timeout=1.0)
        try:
            headers = {}
            body = None
            if method == "POST":
                body = b"{}"
                headers["Content-Type"] = "application/json"
            if token:
                headers["X-Admin-Control"] = token
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            if not raw:
                return response.status, None
            try:
                return response.status, json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError):
                return response.status, None
        finally:
            connection.close()
