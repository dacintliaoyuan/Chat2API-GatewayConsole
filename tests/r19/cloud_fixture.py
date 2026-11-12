# -*- coding: utf-8 -*-
"""Cloud chain fixture for E19-1-12.

Runs the REAL cloud package on loopback only:

    fake UM (verification upstream)
    share_broker_ops.py::linux_main (AF_UNIX broker, spawns 8100)
    chat2api/share_entry.py (8100 role, spawned by the broker)
    cloud/run_cloud.py --config (18096 role, owns the 8110 backend)

The fixture reuses the process/HTTP helpers from
``tests/linux_share_chain_legacy.py`` but replaces every start method that belongs to
the retired local chain. The broker owns the 8100 process and the cloud
application owns the 8110 process; the fixture never starts them itself.

Every path, secret, token, ledger, projection and SQLite file lives under one
private temporary root. A missing override refuses to start instead of falling
back to a repository default or a production port.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import secrets
import signal
import socket
import sys
import tempfile
import time
from pathlib import Path

AI_GATEWAY = Path(__file__).resolve().parents[2]
TESTS = AI_GATEWAY / "tests"
for _entry in (str(TESTS), str(AI_GATEWAY)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import linux_share_chain_legacy as chain  # noqa: E402

PRODUCTION_PORTS = frozenset((8000, 8001, 8100, 8110, 18096, 5901))
MODEL = "fake-um-model"


def _pick_port(taken=()) -> int:
    for _ in range(100):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in PRODUCTION_PORTS and port not in taken:
            return port
    raise RuntimeError("no free loopback port outside production ports")


def _module_name(prefix: str, path: Path) -> str:
    """A per-package module name so two package copies never share a module."""
    digest = hashlib.sha256(str(Path(path).resolve()).encode("utf-8"))
    return "%s_%s" % (prefix, digest.hexdigest()[:12])


#: ``(flag, CloudConfig field)`` pairs for the broker's OWN required arguments.
#: ``--host-command`` is absent: it is a REMAINDER the CALLER appends (the 8100
#: gateway argv), exactly like ``CloudConfig.broker_args()``.
BROKER_ARG_FIELDS = (
    ("--socket-path", "broker_socket"),
    ("--key-file", "broker_key_file"),
    ("--instance-key-file", "instance_key_file"),
    ("--registration-file", "registration_file"),
    ("--instance-socket-prefix", "instance_prefix"),
    ("--projection-path", "projection_file"),
    ("--broker-dir", "broker_dir"),
    ("--host-pid-file", "host_pid_file"),
    ("--state-dir", "host_state_dir"),
)


def broker_argv(paths: dict) -> tuple:
    """The broker's own argv, from the SAME field names ``CloudConfig`` uses.

    The chain fixture must not hand-write the broker CLI: the previous version
    built its own flag list while the product path
    (``CloudConfig.broker_args``) emitted a completely different,
    non-functional set. Both sides now use ``BROKER_ARG_FIELDS`` — the fixture maps
    its path keys through ``broker_fields`` — and the caller appends
    ``--host-command <gateway argv>`` (a REMAINDER, so it must come last).
    """
    return _broker_argv_from(broker_fields(paths))


def broker_fields(paths: dict) -> dict:
    """Map the fixture's path keys onto the ``CloudConfig`` field names."""
    return {
        "broker_socket": paths["broker_socket"],
        "broker_key_file": paths["broker_key"],
        "instance_key_file": paths["instance_key"],
        "registration_file": paths["registration"],
        "instance_prefix": paths["instance_prefix"],
        "projection_file": paths["projection"],
        "broker_dir": paths["broker"],
        "host_pid_file": paths["host_pid"],
        "host_state_dir": paths["run"],
    }


def _broker_argv_from(values) -> tuple:
    """Build the argv from a ``CloudConfig`` or any field-named mapping."""
    argv = ["share_broker_ops.py"]
    for flag, field in BROKER_ARG_FIELDS:
        if isinstance(values, dict):
            value = values[field]
        else:
            value = getattr(values, field)
        argv.extend([flag, str(value)])
    return tuple(argv)


def _paths_for(root: Path) -> dict:
    root = Path(root)
    return {
        "state": root / "state",
        "config": root / "config",
        "logs": root / "logs",
        "locks": root / "locks",
        "home": root / "home",
        "tmp": root / "tmp",
        "work": root / "work",
        "run": root / "run",
        "broker": root / "broker",
        "backend": root / "backend",
        "store": root / "state" / "share-store.db",
        "projection": root / "run" / "share-projection.json",
        "attribution": root / "config" / "share-attribution.key",
        "instance_key": root / "config" / "instance.key",
        "broker_key": root / "config" / "broker-control.key",
        "secret_key": root / "config" / "share-secret.key",
        "control_token": root / "config" / "admin-control-token",
        "session_code": root / "config" / "usage-session-code",
        "management_config": root / "config" / "share-management.json",
        "registration": root / "run" / "instance-registration.json",
        "instance_prefix": root / "run" / "instance-",
        "broker_socket": root / "run" / "broker.sock",
        "host_pid": root / "run" / "share-host.pid",
        "backend_dir": root / "backend",
        "backend_control": root / "backend" / "control-token",
        "backend_reauth": root / "backend" / "chat2api-reauth.state",
        "chat2api_token": root / "backend" / "token.json",
        "chat2api_log": root / "logs" / "chat2api.log",
        "chat2api_usage": root / "state" / "usage-chat2api.jsonl",
        "revoke_state": root / "state" / "share-revoke.state",
        "share_faults": root / "state" / "share-faults.jsonl",
        "price_table": root / "config" / "prices.json",
        "console_state": root / "state" / "console-state.sqlite3",
        "console_lock": root / "locks" / "console",
        "cloud_config": root / "config" / "cloud-config.json",
        "management_stdout": root / "logs" / "cloud.stdout.log",
        "broker_stdout": root / "logs" / "broker.stdout.log",
        "fake_um_log": root / "logs" / "fake-um.log",
    }


class CloudChainFixture(chain.ChainFixture):
    """One isolated cloud chain: fake UM + broker/8100 + cloud/8110."""

    def __init__(self, package_root, report_path=None):
        self.package = Path(package_root).resolve()
        if not (self.package / "cloud" / "run_cloud.py").is_file():
            raise RuntimeError(
                "cloud package has no cloud/run_cloud.py: %s" % self.package)
        self.share_port = _pick_port()
        self.backend_port = _pick_port((self.share_port,))
        self.cloud_port = _pick_port(
            (self.share_port, self.backend_port))
        self.fake_port = _pick_port(
            (self.share_port, self.backend_port, self.cloud_port))
        self.model = MODEL
        self.fake_um_token = None
        self.fake_um = None
        self.broker = None
        self.broker_client = None
        self.management_process = None
        self._started = False
        self._barrier_armed = False
        report = report_path or (
            Path(tempfile.mkdtemp(prefix="r19-cloud-report-"))
            / "report.json")
        super().__init__(report)
        self.paths = _paths_for(self.root)
        self.env = self._build_env()
        self.work_dir = self.paths["work"]
        self.config_path = self.paths["cloud_config"]

    @property
    def fake_base_url(self) -> str:
        return "http://127.0.0.1:%d" % self.fake_port

    # ------------------------------------------------------------ environment
    def _build_env(self) -> dict:
        env = dict(os.environ)
        for name in list(env):
            if name in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP",
                         "PYTHONPATH"):
                env.pop(name, None)
            elif name.startswith((
                    "UM_SHARE_", "CHAT2API_", "CONSOLE_", "USAGE_",
                    "SHARE_", "MAGPIE_", "DSH_")):
                env.pop(name, None)
        root = Path(self.root)
        paths = _paths_for(root)
        env.update({
            "HOME": str(paths["home"]),
            "USERPROFILE": str(paths["home"]),
            "TMPDIR": str(paths["tmp"]),
            "TEMP": str(paths["tmp"]),
            "TMP": str(paths["tmp"]),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(self.package),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "http_proxy": "",
            "https_proxy": "",
            "FAKE_UM_TOKEN": self.fake_um_token or "",
            "CHAT2API_TOKEN_FILE": str(paths["chat2api_token"]),
            "CHAT2API_USAGE_LOG": str(paths["chat2api_usage"]),
            "CHAT2API_SERVICE_LOG": str(paths["chat2api_log"]),
            "CHAT2API_CONTROL_TOKEN_FILE": str(paths["backend_control"]),
            "CHAT2API_REAUTH_SLOT_FILE": str(paths["backend_reauth"]),
            "CHAT2API_SHARE_REVOKE_STATE_FILE": str(paths["revoke_state"]),
            "CHAT2API_SHARE_ATTRIBUTION_KEY_FILE": str(
                paths["attribution"]),
            "CHAT2API_SHARE_SECRET_KEY_FILE": str(paths["secret_key"]),
            "CHAT2API_PRICE_TABLE_FILE": str(paths["price_table"]),
            "CHAT2API_UPSTREAM_AUTO_RETRY": "0",
            "CHAT2API_UPSTREAM_AUTO_RETRIES": "0",
            "CHAT2API_REPETITION_AUTO_RETRY": "0",
            "CHAT2API_REPETITION_AUTO_RETRIES": "0",
            "CHAT2API_AUTH_WAIT_BUDGET_S": "5",
            "CHAT2API_MAX_REQUEST_SECONDS": "90",
            "CHAT2API_SHARE_DISPATCH_MAX_WAIT_S": "10",
            "CHAT2API_LAUNCH_SOURCE": "fixture",
            "SHARE_STORE_FILE": str(paths["store"]),
            "SHARE_MANAGEMENT_CONFIG": str(paths["management_config"]),
            "CONSOLE_SHARE_RUN_ROOT": str(paths["run"]),
            "CONSOLE_SHARE_BACKEND_DIR": str(paths["backend"]),
            "CONSOLE_STATE_FILE": str(paths["console_state"]),
            "CONSOLE_LOCK_DIR": str(paths["console_lock"]),
            "CONSOLE_SHARE_BACKEND_PORT": str(self.backend_port),
            "CONSOLE_SHARE_BACKEND_BASE_URL": self.fake_base_url,
            "CONSOLE_SHARE_ATTRIBUTION_KEY_FILE": str(
                paths["attribution"]),
            "USAGE_CONTROL_TOKEN_FILE": str(paths["control_token"]),
            "USAGE_SESSION_CODE_FILE": str(paths["session_code"]),
            "USAGE_PRICE_FILE": str(paths["price_table"]),
        })
        return env

    # -------------------------------------------------------------- processes
    def _process(self, name, argv, log_path, env=None):
        process = chain.ManagedProcess(
            name, argv, env or self.env, self.work_dir, log_path)
        self.processes.append(process)
        return process

    def config_values(self) -> dict:
        paths = self.paths
        return {
            "schema_version": 1,
            "host": "127.0.0.1",
            "port": self.cloud_port,
            "state_root": str(paths["state"]),
            "home_dir": str(paths["home"]),
            "config_dir": str(paths["config"]),
            "log_dir": str(paths["logs"]),
            "lock_dir": str(paths["locks"]),
            "pid_file": str(paths["state"] / "cloud-app.pid"),
            "control_token_file": str(paths["control_token"]),
            "share_run_root": str(paths["run"]),
            "share_store_file": str(paths["store"]),
            "share_secret_key_file": str(paths["secret_key"]),
            "console_state_file": str(paths["console_state"]),
            "console_lock_dir": str(paths["console_lock"]),
            "share_management_config": str(paths["management_config"]),
            "broker_socket": str(paths["broker_socket"]),
            "broker_key_file": str(paths["broker_key"]),
            "projection_file": str(paths["projection"]),
            "attribution_key_file": str(paths["attribution"]),
            "instance_key_file": str(paths["instance_key"]),
            "registration_file": str(paths["registration"]),
            "instance_prefix": str(paths["instance_prefix"]),
            "revoke_state_file": str(paths["revoke_state"]),
            # spec-cloud-003: the summary read face needs the explicit price
            # identity. Without it the only controlled chain answered
            # 503 summary_unavailable for every request, so the read face had
            # no passing evidence at all.
            "price_table_file": str(paths["price_table"]),
            "host_state_dir": str(paths["run"]),
            "host_pid_file": str(paths["host_pid"]),
            "broker_dir": str(paths["broker"]),
            "backend_dir": str(paths["backend"]),
            "backend_port": self.backend_port,
            "backend_base_url": self.fake_base_url,
            "backend_gateway_dir": str(self.package / "chat2api"),
            "backend_token_file": str(paths["chat2api_token"]),
            "backend_profile_dir": str(paths["backend"] / "edge-profile"),
            "backend_service_log": str(paths["chat2api_log"]),
            "backend_usage_log": str(paths["chat2api_usage"]),
            "backend_control_token_file": str(paths["backend_control"]),
            "backend_reauth_slot_file": str(paths["backend_reauth"]),
            "backend_python": sys.executable,
            "backend_start_timeout_s": 30.0,
            "backend_stop_timeout_s": 20.0,
        }

    def write_config(self, missing=None) -> Path:
        values = self.config_values()
        if missing is not None:
            values.pop(missing)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(values, indent=2), encoding="utf-8")
        return self.config_path

    def _write_projection(self):
        path = self.package / "share_store.py"
        module = chain._load_module(
            _module_name("r19_cloud_share_store", path), path)
        store = module.ShareStore(str(self.paths["store"]))
        try:
            projection = store.render_projection()
        finally:
            store.close()
        self.paths["projection"].write_text(
            json.dumps(projection, ensure_ascii=False, indent=2),
            encoding="utf-8")

    def _write_token_file(self):
        token = {
            "url": self.fake_base_url,
            "token": self.fake_um_token,
            "api_key": None,
            "saved_at": datetime.datetime.now(
                datetime.timezone.utc).isoformat(timespec="seconds"),
        }
        self._write_secret(
            self.paths["chat2api_token"],
            json.dumps(token).encode("utf-8"))

    def prepare(self):
        for port in (self.share_port, self.backend_port, self.cloud_port,
                     self.fake_port):
            if not chain._free_port(port):
                raise EnvironmentError(
                    "port %d is already in use before startup" % port)
        for name in ("state", "config", "logs", "locks", "home", "tmp",
                     "work", "run", "broker", "backend"):
            path = self.paths[name]
            path.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(path, 0o700)
        self._write_secret(
            self.paths["attribution"],
            secrets.token_urlsafe(48).encode("utf-8"))
        self._write_secret(
            self.paths["instance_key"],
            secrets.token_urlsafe(32).encode("utf-8"))
        self._write_secret(
            self.paths["broker_key"],
            secrets.token_urlsafe(32).encode("utf-8"))
        self._write_secret(self.paths["secret_key"], os.urandom(32))
        self._write_secret(
            self.paths["control_token"],
            secrets.token_urlsafe(24).encode("utf-8"))
        self._write_secret(
            self.paths["backend_control"],
            secrets.token_urlsafe(24).encode("utf-8"))
        self._write_secret(
            self.paths["session_code"],
            secrets.token_urlsafe(24).encode("utf-8"))
        self.paths["price_table"].write_text(
            '{"version":"fixture","models":{}}', encoding="utf-8")
        self.paths["management_config"].write_text(json.dumps({
            "schema_version": 2,
            "enabled": True,
            "transport": "unix_socket",
            "socket_path": str(self.paths["broker_socket"]),
            "control_key_file": str(self.paths["broker_key"]),
        }, indent=2), encoding="utf-8")
        self._write_projection()
        self.write_config()

    # --------------------------------------------------------------- startup
    def start_fake_um(self):
        path = self.package / "tests" / "fake_um" / "fake_um_server.py"
        module = chain._load_module(
            _module_name("r19_cloud_fake_um", path), path)
        token = secrets.token_urlsafe(32)
        fake = module.FakeUM(
            port=self.fake_port, token=token, model=self.model)
        fake.start()
        self.fake_um = fake
        self.fake_um_token = token
        self.env["FAKE_UM_TOKEN"] = token
        chain._wait_for(
            lambda: chain._free_port(self.fake_port) is False, 5.0)
        self._write_token_file()

    def start_broker(self):
        for name in ("broker_socket", "host_pid", "registration",
                      "share_faults"):
            with contextlib.suppress(OSError):
                self.paths[name].unlink()
        gateway = [
            sys.executable,
            str(self.package / "chat2api" / "share_entry.py"),
            "--port", str(self.share_port),
            "--upstream-port", str(self.backend_port),
            "--config", str(self.paths["projection"]),
            "--auth-mode", "projection",
            "--attribution-key-file", str(self.paths["attribution"]),
            "--instance-key-file", str(self.paths["instance_key"]),
            "--registration-file", str(self.paths["registration"]),
            "--instance-pipe-prefix", str(self.paths["instance_prefix"]),
            "--fault-log", str(self.paths["share_faults"]),
        ]
        argv = [
            sys.executable,
            str(self.package / "share_broker_ops.py"),
            # F8: the broker's own flags come from the SAME helper the product
            # path (``CloudConfig.broker_args``) mirrors, never a hand-written
            # list that can drift from it.
            *broker_argv(self.paths)[1:],
            "--expected-uid", str(os.getuid()),
            "--expect-instance-image", sys.executable,
            "--expect-instance-cmdline-marker", "share_entry.py",
            "--expect-instance-port", str(self.share_port),
            "--deadline", "10",
            "--dispatch-deadline", "30",
            "--host-command", *gateway,
        ]
        self.broker = self._process(
            "broker", argv, self.paths["broker_stdout"])
        self.broker.start()
        chain._wait_for(
            lambda: self.paths["broker_socket"].exists(), 20.0)
        self._broker_start()
        chain._wait_for(
            lambda: self.paths["registration"].exists(), 20.0)
        chain._wait_for(self._share_ready, 20.0)

    def _broker_start(self):
        secret = self.paths["broker_key"].read_text(
            encoding="utf-8").strip()
        sys.path.insert(0, str(self.package))
        try:
            from share_broker_client import (BrokerClient,
                                            UnixSocketBrokerTransport)
            client = BrokerClient(
                lambda: UnixSocketBrokerTransport(
                    str(self.paths["broker_socket"])), secret)
        finally:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(self.package))
        result = client.start(explicit=True)
        self.broker_client = client
        if not result.ok or not (result.result or {}).get("started"):
            raise RuntimeError(
                "broker start failed: %r %r\n%s"
                % (result.error_code, result.result, self.tail()))

    def _share_ready(self):
        result = chain._http_request(
            self.share_port, "GET", "/v1/models", key="probe",
            timeout=1.0)
        return result.status == 401

    def start_management(self):
        # E19-1-06 step 1: the entry REFUSES to import without -I, so the
        # fixture must launch it isolated exactly like the deployment does.
        argv = [
            sys.executable, "-I",
            str(self.package / "cloud" / "run_cloud.py"),
            "--config", str(self.config_path),
        ]
        self.management_process = self._process(
            "cloud", argv, self.paths["management_stdout"])
        self.management_process.start()
        self._wait_ready(
            self.management_process, self._management_ready, 60.0)

    def _wait_ready(self, process, predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not process.alive():
                raise RuntimeError(
                    "%s exited before readiness\n%s"
                    % (process.name, process.tail()))
            if predicate():
                return
            time.sleep(0.1)
        raise RuntimeError(
            "%s did not become ready within %ss\n%s"
            % (process.name, timeout, process.tail()))

    def _management_ready(self):
        return self.health().status == 200

    def start_all(self):
        if self._started:
            raise RuntimeError("cloud chain fixture is already started")
        self.prepare()
        self.start_fake_um()
        self.start_broker()
        self.start_management()
        self._started = True

    # ---------------------------------------------------------------- clients
    def health(self):
        return chain._http_request(
            self.cloud_port, "GET", "/health", timeout=2.0)

    def management_call(self, method, path, body=None, timeout=60.0):
        return chain._http_request(
            self.cloud_port, method, path, body=body,
            headers={"X-Admin-Control": self.control_token},
            timeout=timeout)

    def create_key(self, name, max_concurrency=4):
        result = self.management_call(
            "POST", "/api/share/keys",
            {"name": name, "max_concurrency": max_concurrency})
        if result.status != 201:
            raise RuntimeError(
                "create_key %s failed: %s %s"
                % (name, result.status, result.text()))
        payload = result.json()
        payload.update(self.get_key(payload["key_id"]))
        return payload

    def get_key(self, key_id):
        result = self.management_call(
            "GET", "/api/share/keys/%s" % key_id)
        if result.status != 200:
            raise RuntimeError(
                "get_key %s failed: %s %s"
                % (key_id, result.status, result.text()))
        return result.json()

    def revoke_key(self, key_id, expected_row_version):
        result = self.management_call(
            "POST", "/api/share/keys/%s/revoke-and-terminate" % key_id,
            {"expected_row_version": expected_row_version})
        if result.status != 200:
            raise RuntimeError(
                "revoke_key %s failed: %s %s"
                % (key_id, result.status, result.text()))
        return result.json()

    def drain(self):
        return self.management_call(
            "POST", "/api/share/drain", {}, timeout=90.0)

    def share_models(self, secret):
        return chain._http_request(
            self.share_port, "GET", "/v1/models", key=secret, timeout=30.0)

    def chat_backend(self, secret, prompt="hello"):
        return chain._http_request(
            self.backend_port, "POST", "/v1/chat/completions",
            body={"model": self.model, "stream": False,
                  "messages": [{"role": "user", "content": prompt}]},
            key=secret, timeout=60.0)

    def share_chat_stream(self, secret, prompt="held"):
        return chain._http_request(
            self.share_port, "POST", "/v1/chat/completions",
            body={"model": self.model, "stream": True,
                  "messages": [{"role": "user", "content": prompt}]},
            key=secret, timeout=60.0, stream=True)

    def projection(self):
        return json.loads(
            self.paths["projection"].read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- barrier
    def arm_chat_barrier(self):
        result = chain._http_request(
            self.fake_port, "POST", "/__control/barrier/arm",
            body={"protocols": ["chat"]}, timeout=5.0)
        if result.status != 200:
            raise RuntimeError(
                "barrier arm failed: %s %s" % (result.status, result.text()))
        self._barrier_armed = True

    def wait_barrier_arrivals(self, count, timeout=20.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            result = chain._http_request(
                self.fake_port, "GET", "/__control/barrier", timeout=5.0)
            last = result.json() if result.status == 200 else None
            if len((last or {}).get("arrivals") or []) >= count:
                return last
            time.sleep(0.05)
        raise RuntimeError(
            "barrier arrivals not observed: %r" % (last,))

    def release_barrier(self):
        if not self._barrier_armed:
            return
        self._barrier_armed = False
        with contextlib.suppress(Exception):
            chain._http_request(
                self.fake_port, "POST", "/__control/barrier/release",
                body={}, timeout=5.0)
        with contextlib.suppress(Exception):
            chain._http_request(
                self.fake_port, "POST", "/__control/barrier/disarm",
                body={}, timeout=5.0)

    def wait_share_port_closed(self, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if chain._free_port(self.share_port):
                return True
            time.sleep(0.1)
        return False

    # --------------------------------------------------------------- teardown
    def _owned_pid(self, pid_file, marker):
        """The live process this fixture owns, or None (gone/foreign).

        E19-1-07 step 3 / G18: a pid + cmdline marker is NOT enough proof of
        ownership — another task's process could carry the same script name. The
        fixture owns the process only when ALL of these hold:

        * the pid file is under the fixture root;
        * ``/proc/<pid>/cwd`` is under the fixture root;
        * ``/proc/<pid>/environ`` HOME equals the fixture HOME;
        * ``/proc/<pid>/environ`` PYTHONPATH equals the fixture package root.

        Any failure means an EXTERNAL process: the caller removes the pid file but
        never signals it.
        """
        path = Path(pid_file).resolve()
        root = self.root.resolve()
        if not (path == root or root in path.parents):
            return None
        try:
            raw = path.read_text(encoding="ascii").strip()
        except OSError:
            return None
        if not raw.isdigit():
            return None
        pid = int(raw)
        try:
            cmdline = Path("/proc/%d/cmdline" % pid).read_bytes().decode(
                "utf-8", "replace")
            cwd = Path("/proc/%d/cwd" % pid).resolve()
            environ = Path("/proc/%d/environ" % pid).read_bytes().decode(
                "utf-8", "replace")
        except (OSError, ValueError):
            return None
        if not cmdline or marker not in cmdline:
            return None
        # The owned cwd is the PACKAGE tree: the 8100 gateway runs with
        # cwd = package/chat2api and the 8110 backend is spawned with
        # cwd = the configured backend_gateway_dir (also package/chat2api).
        # The fixture root is accepted too. A cwd outside BOTH is foreign.
        owned_roots = [Path(self.package).resolve(), root]
        if not any(cwd == candidate or candidate in cwd.parents
                   for candidate in owned_roots):
            return None
        env = {}
        for entry in environ.split("\0"):
            name, _, value = entry.partition("=")
            if name:
                env[name] = value
        if Path(env.get("HOME", "") or "/nonexistent").resolve() != \
                Path(self.paths["home"]).resolve():
            return None
        # PYTHONPATH must not point anywhere FOREIGN. The entry deliberately
        # clears it (E19-1-06 step 2), so "absent" is the owned state for the
        # 8110 child; the 8100 child inherits the fixture package root from
        # the broker. Anything else is another task's process.
        raw_pythonpath = env.get("PYTHONPATH", "")
        if raw_pythonpath:
            if Path(raw_pythonpath).resolve() != Path(self.package).resolve():
                return None
        return pid

    def _live_pid(self, pid_file, marker):
        """Backward-compatible alias for the hardened ownership probe."""
        return self._owned_pid(pid_file, marker)

    def _stop_residual(self, pid_file, marker, port):
        path = Path(pid_file)
        pid = self._live_pid(pid_file, marker)
        if pid is None:
            with contextlib.suppress(OSError):
                path.unlink()
            chain._wait_for(lambda: chain._free_port(port), 10.0)
            return
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            except OSError:
                break
            time.sleep(0.1)
        else:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            path.unlink()
        chain._wait_for(lambda: chain._free_port(port), 10.0)

    def stop_all(self):
        errors = []
        self.release_barrier()
        if self.management_process is not None \
                and self.management_process.alive():
            try:
                result = self.management_call(
                    "POST", "/admin/shutdown", {}, timeout=15.0)
                if result.status != 200:
                    errors.append("cloud shutdown: %s %s"
                                  % (result.status, result.text()))
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append("cloud shutdown: %s: %s"
                              % (type(exc).__name__, exc))
        if self.management_process is not None:
            self.management_process.stop()
        gateway_live = self._live_pid(
            self.paths["host_pid"], "share_entry.py")
        if self.broker_client is not None and self.broker is not None \
                and self.broker.alive() and (
                    gateway_live is not None
                    or not chain._free_port(self.share_port)):
            try:
                result = self.broker_client.host_stop()
                if not result.ok:
                    errors.append("broker host-stop: %s"
                                  % (result.error_code,))
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append("broker host-stop: %s: %s"
                              % (type(exc).__name__, exc))
        if self.broker is not None:
            self.broker.stop()
        self._stop_residual(
            self.paths["host_pid"], "share_entry.py", self.share_port)
        self._stop_residual(
            self.paths["backend"] / "backend.pid", "chat2api.py",
            self.backend_port)
        if self.fake_um is not None:
            self.fake_um.stop()
            self.fake_um = None
        self._started = False
        chain._wait_for(
            lambda: chain._free_port(self.share_port), 20.0)
        chain._wait_for(
            lambda: chain._free_port(self.backend_port), 20.0)
        if errors:
            print("cloud fixture cleanup: %s" % "; ".join(errors),
                  file=sys.stderr)

    def tail(self) -> str:
        parts = []
        for name in ("management_stdout", "broker_stdout",
                      "chat2api_log"):
            path = self.paths.get(name)
            if path is not None and Path(path).exists():
                parts.append("== %s ==\n%s"
                             % (name, chain._tail(Path(path))))
        return "\n".join(parts)
