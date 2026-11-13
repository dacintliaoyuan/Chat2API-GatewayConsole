# -*- coding: utf-8 -*-
"""E19-1-09 local application fixture (r19).

One fixture owns every path, port, secret and child process the local
composition root needs:

* a private temp root; the fixture config names ONLY paths below that root
  (never the repository ``data/``, ``profile/`` or ``chat2api/token.json``);
* explicit free loopback ports for the app, the local UM runtime and the fake
  UM upstream;
* the fixture ledger, control token and session code;
* the fake UM child process (``tests/fake_um/fake_um_server.py``);
* subprocess helpers for ``local/run_local.py``.

Nothing here reads a real credential, ledger or profile.  The
``package_root`` is explicit so the same fixture can run the in-tree package or
an isolated copy built by ``r19.package_local``.
"""
from __future__ import annotations

import fnmatch
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

AI_GATEWAY = Path(__file__).resolve().parents[2]
PROJECT_ROOT = AI_GATEWAY.parent
GIT_ROOT = AI_GATEWAY.parents[1]

CONTROL_TOKEN = "r19-local-control-token"
UPSTREAM_TOKEN = "r19-local-upstream-token"
MODEL = "fake-um-model"
SESSION_CODE = "r19-local-session"

# Real state/secret paths the fixture config must never name or touch.  The
# isolation test compares every configured path against this list without
# reading or hashing the real documents (the 8000/8001 instances may be
# writing them concurrently).
REAL_STATE_PATHS = (
    AI_GATEWAY / "data",
    AI_GATEWAY / "profile",
    AI_GATEWAY / "chat2api" / "token.json",
    AI_GATEWAY / "chat2api" / ".edge-profile",
    AI_GATEWAY / "chat2api" / "chat2api.log",
    AI_GATEWAY / "data" / "chat2api-reauth.state",
    AI_GATEWAY / "data" / "usage-chat2api.jsonl",
    AI_GATEWAY / "data" / "dsh-ledger-20260902.json",
    AI_GATEWAY / "data" / "console-state.sqlite3",
    AI_GATEWAY / "data" / "console-locks",
    AI_GATEWAY / "data" / "share-store.db",
    AI_GATEWAY / "data" / "admin-control-token",
    AI_GATEWAY / "data" / "usage-session-code",
    # E19-1-L2 / F29: ``console_stats`` binds these two at import time
    # (``QODER_GLOBAL_REQUESTS_LOG`` / ``GEMINI_USAGE_LEDGER``), and they live
    # under ``qoder-global/`` — a root the previous list did not cover.
    AI_GATEWAY / "qoder-global",
    AI_GATEWAY / "qoder-global" / "logs" / "requests.log",
)

# ``data/model-sync-*`` is covered by the whole ``data/`` directory above; the
# pattern is kept explicit so a future path relocation cannot silently skip it.
REAL_STATE_GLOBS = ("model-sync-*",)


def free_port():
    """Allocate one currently free loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# E19-1-05 step 2 / G17: the real deployment listeners. A fixture port that
# happens to match one would collide with (or shadow) a production service.
PRODUCTION_PORTS = frozenset({8000, 8001, 8100, 18096, 5901})


def distinct_free_ports(count):
    """Allocate ``count`` distinct free loopback ports, never a production one."""
    ports = []
    attempts = 0
    while len(ports) < count:
        attempts += 1
        if attempts > count * 100:
            raise RuntimeError(
                "could not allocate %d non-production ports" % count)
        port = free_port()
        if port in PRODUCTION_PORTS or port in ports:
            continue
        ports.append(port)
    # E19-1-L2: the allocator asserts its own contract instead of trusting the
    # filter above to have run (a future edit that drops it fails here).
    assert not PRODUCTION_PORTS.intersection(ports), (
        "allocated production port(s): %s"
        % sorted(PRODUCTION_PORTS.intersection(ports)))
    return ports


def http_request(port, method, path, *, body=None, token=None, host=None,
                 headers=None, timeout=5.0):
    """One bounded loopback request; returns ``(status, parsed_json_or_none)``."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        request_headers = dict(headers or {})
        if token is not None:
            request_headers["X-Admin-Control"] = token
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if host is not None:
            connection.putrequest(method, path, skip_host=True)
            connection.putheader("Host", host)
            for name, value in request_headers.items():
                connection.putheader(name, value)
            if payload is not None:
                connection.putheader("Content-Length", str(len(payload)))
            connection.endheaders()
            connection.send(payload or b"")
        else:
            connection.request(method, path, body=payload,
                               headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        parsed = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError):
                parsed = None
        return response.status, parsed
    finally:
        connection.close()


def _tail(path, limit=4000):
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", "replace")


def assert_paths_isolated(testcase, paths, root):
    """Assert every named path is below ``root`` and outside real state."""
    root = Path(root).resolve()
    for name, value in paths.items():
        path = Path(value).resolve()
        testcase.assertTrue(
            path == root or root in path.parents,
            "%s=%s escapes the fixture root %s" % (name, value, root))
        for real in REAL_STATE_PATHS:
            real = real.resolve()
            testcase.assertFalse(
                path == real or real in path.parents,
                "%s=%s touches real state %s" % (name, value, real))
        # E19-1-L2: ``REAL_STATE_GLOBS`` covers whole families under
        # ``data/`` (``model-sync-*``); a fixture path whose NAME matches one
        # while sitting under a real state root is still a real-state hit.
        for pattern in REAL_STATE_GLOBS:
            if not fnmatch.fnmatch(path.name, pattern):
                continue
            for real in REAL_STATE_PATHS:
                real = real.resolve()
                testcase.assertFalse(
                    path == real or real in path.parents,
                    "%s=%s matches real state pattern %s"
                    % (name, value, pattern))


class LocalFixture:
    """One fully isolated local application fixture."""

    def __init__(self, package_root=None, *, root=None, python=None,
                 start_fake_um=True, keep_root=False):
        self.package_root = Path(package_root or AI_GATEWAY).resolve()
        if root is None:
            self.root = Path(tempfile.mkdtemp(
                prefix="r19-local-fixture-")).resolve()
        else:
            self.root = Path(root).resolve()
        self.python = python or sys.executable
        self.keep_root = keep_root
        self.app_port, self.um_port, self.fake_um_port = \
            distinct_free_ports(3)
        self.control_token = CONTROL_TOKEN
        self.upstream_token = UPSTREAM_TOKEN
        self.session_code = SESSION_CODE
        self.model = MODEL
        self.paths = self._paths()
        # E19-1-04 step 7: ``config_dir`` must be the ``--config`` document's
        # parent, so the fixture writes its config INTO the declared
        # ``config_dir`` (never a sibling ``fixture/`` directory).
        self.config_path = Path(self.paths["config_dir"]) / "local-config.json"
        self.fixture_dir = self.root / "fixture"
        self.app_stdout_path = self.root / "fixture-logs" / "app.stdout.log"
        self.app_stderr_path = self.root / "fixture-logs" / "app.stderr.log"
        self.fake_stdout_path = self.root / "fixture-logs" / "fake-um.out.log"
        self.fake_stderr_path = self.root / "fixture-logs" / "fake-um.err.log"
        self.app = None
        self.app_returncode = None
        self._app_stdout = None
        self._app_stderr = None
        self._fake_um = None
        self._fake_stdout = None
        self._fake_stderr = None
        self._write_fixture_files()
        self.write_config()
        if start_fake_um:
            self.start_fake_um()

    # ------------------------------------------------------------- fixtures
    def _paths(self):
        root = self.root
        return {
            "state_root": root / "state",
            "home_dir": root / "home",
            "config_dir": root / "fixture",
            "log_dir": root / "logs",
            "lock_dir": root / "locks",
            "pid_file": root / "state" / "local-app.pid",
            "ledger_file": root / "fixture" / "usage-chat2api.jsonl",
            "session_code_file": root / "fixture" / "usage-session-code",
            "control_token_file": root / "fixture" / "admin-control-token",
            "console_state_file": root / "state" / "console-state.sqlite3",
            "model_sync_dir": root / "state" / "model-sync",
        }

    def _write_fixture_files(self):
        fixture_dir = Path(self.paths["config_dir"])
        fixture_dir.mkdir(parents=True, exist_ok=True)
        (self.root / "fixture-logs").mkdir(parents=True, exist_ok=True)
        self.paths["control_token_file"].write_text(
            self.control_token + "\n", encoding="utf-8")
        self.paths["session_code_file"].write_text(
            SESSION_CODE + "\n", encoding="utf-8")
        self.paths["ledger_file"].write_text(json.dumps({
            "time": "2026-11-07T10:00:00+08:00",
            "model": self.model,
            "status": 200,
            "input_tokens": 10,
            "output_tokens": 5,
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        # E19-1-05 step 1: the two secret files must be 0600 and the fixture
        # root 0700 on POSIX. On Windows the mode bits are not meaningful
        # (the platform ignores them); the production control-token reader
        # enforces the same 0600 gate there through its own check.
        if os.name == "posix":
            os.chmod(self.root, 0o700)
            os.chmod(self.paths["control_token_file"], 0o600)
            os.chmod(self.paths["session_code_file"], 0o600)

    def config_mapping(self, *, omit=None, overrides=None):
        """The explicit fixture document (never a real path or credential)."""
        mapping = {
            "schema_version": 1,
            "host": "127.0.0.1",
            "port": self.app_port,
            "state_root": str(self.paths["state_root"]),
            "home_dir": str(self.paths["home_dir"]),
            "config_dir": str(self.paths["config_dir"]),
            "log_dir": str(self.paths["log_dir"]),
            "lock_dir": str(self.paths["lock_dir"]),
            "pid_file": str(self.paths["pid_file"]),
            "ledger_file": str(self.paths["ledger_file"]),
            "session_code_file": str(self.paths["session_code_file"]),
            "control_token_file": str(self.paths["control_token_file"]),
            "console_state_file": str(self.paths["console_state_file"]),
            "model_sync_dir": str(self.paths["model_sync_dir"]),
            "um_runtime": {
                "source_dir": str(self.package_root / "chat2api"),
                "python": self.python,
                "host": "127.0.0.1",
                "port": self.um_port,
                # E19-1-04 step 8 (F15a): the UM child HOME is an explicit
                # field equal to the application's isolated home, never
                # ``dirname(profile_dir)``. Without it ``LocalConfig`` refuses
                # to load (``um_runtime.home_dir: missing required field``).
                "home_dir": str(self.paths["home_dir"]),
                "profile_dir": str(self.root / "um" / "profile"),
                "token_file": str(self.root / "um" / "token.json"),
                "service_log": str(self.root / "um" / "chat2api.log"),
                # standards-config-06: the summary READS ``ledger_file`` and
                # the child WRITES ``usage_log`` — ONE identity, so the child
                # writes the ledger the summary reads.
                "usage_log": str(self.paths["ledger_file"]),
                "control_token_file": str(self.root / "um" / "control-token"),
                "reauth_slot_file": str(self.root / "um" / "reauth.state"),
                "base_url": self.fake_um_base_url,
                "upstream_token": self.upstream_token,
                "auth_wait_budget_s": 180,
                "request_max_s": 360,
                "start_timeout_s": 60.0,
                "stop_timeout_s": 20.0,
            },
            "runtime_control": {
                "mode": "fixture",
                "script": None,
                "timeout_s": 15.0,
            },
        }
        if omit:
            mapping.pop(omit, None)
        for key, value in (overrides or {}).items():
            if isinstance(value, dict) and isinstance(mapping.get(key), dict):
                mapping[key] = dict(mapping[key], **value)
            else:
                mapping[key] = value
        return mapping

    def write_config(self, name="local-config.json", *, omit=None,
                     overrides=None):
        path = Path(self.paths["config_dir"]) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            self.config_mapping(omit=omit, overrides=overrides),
            ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @property
    def fake_um_base_url(self):
        return "http://127.0.0.1:%d" % self.fake_um_port

    # -------------------------------------------------------------- fake UM
    def start_fake_um(self):
        if self._fake_um is not None and self._fake_um.poll() is None:
            return self._fake_um
        entry = self.package_root / "tests" / "fake_um" / "fake_um_server.py"
        # E19-1-05 step 6 / G20: the fake UM gets the SAME explicit
        # whitelist as the app child (``_app_env``), never ``os.environ``.
        # Inheriting the parent environment would let an unrelated variable
        # (a real credential, a production path) reach the fake upstream.
        env = self._base_env()
        env["FAKE_UM_TOKEN"] = self.upstream_token
        self._fake_stdout = open(self.fake_stdout_path, "wb")
        self._fake_stderr = open(self.fake_stderr_path, "wb")
        self._fake_um = subprocess.Popen(
            [self.python, "-I", str(entry),
             "--host", "127.0.0.1", "--port", str(self.fake_um_port)],
            cwd=str(self.root), env=env, stdin=subprocess.DEVNULL,
            stdout=self._fake_stdout, stderr=self._fake_stderr)
        self._wait_fake_um()
        return self._fake_um

    def _wait_fake_um(self, timeout=30.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            if self._fake_um.poll() is not None:
                raise AssertionError(
                    "fake UM exited rc=%s\n%s"
                    % (self._fake_um.returncode,
                       _tail(self.fake_stderr_path)))
            try:
                status, payload = http_request(
                    self.fake_um_port, "GET", "/api/config", timeout=0.5)
            except OSError as exc:
                last = exc
            else:
                if status == 200 and isinstance(payload, dict):
                    return payload
                last = (status, payload)
            time.sleep(0.05)
        raise AssertionError("fake UM did not become ready: %r" % (last,))

    def stop_fake_um(self):
        process = self._fake_um
        self._fake_um = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        for handle in (self._fake_stdout, self._fake_stderr):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
        self._fake_stdout = None
        self._fake_stderr = None

    # ----------------------------------------------------------- local app
    def _base_env(self):
        """The explicit system-variable whitelist shared by every child.

        E19-1-05 step 6 / G20: the fake UM and the app get the SAME base, so
        neither inherits a repository path, a real credential or an unrelated
        production setting from this test process.
        """
        env = {}
        for name in ("PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT",
                     "SYSTEMDRIVE", "USERNAME"):
            value = os.environ.get(name)
            if value:
                env[name] = value
        env.update({
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(self.paths["home_dir"]),
            "USERPROFILE": str(self.paths["home_dir"]),
            "TMPDIR": str(self.root / "tmp"),
            "TEMP": str(self.root / "tmp"),
            "TMP": str(self.root / "tmp"),
        })
        return env

    def _app_env(self, *, pythonpath=None, extra_env=None):
        """Minimal explicit child environment; no inherited repository paths."""
        env = self._base_env()
        if pythonpath is not None:
            env["PYTHONPATH"] = str(pythonpath)
        env.update(extra_env or {})
        return env

    def _entry_command(self, *args):
        entry = self.package_root / "local" / "run_local.py"
        return [self.python, "-I", str(entry), *args]

    def run_check_config(self, config_path=None, *, cwd=None,
                         pythonpath=None):
        return self._run_entry(
            "--check-config", config_path=config_path, cwd=cwd,
            pythonpath=pythonpath)

    def run_runtime_info(self, config_path=None, *, cwd=None,
                         pythonpath=None):
        return self._run_entry(
            "--print-runtime-info", config_path=config_path, cwd=cwd,
            pythonpath=pythonpath)

    def _run_entry(self, flag, *, config_path=None, cwd=None,
                   pythonpath=None):
        command = self._entry_command(
            "--config", str(config_path or self.config_path), flag)
        return subprocess.run(
            command, cwd=str(Path(cwd) if cwd else self.root),
            env=self._app_env(pythonpath=pythonpath),
            capture_output=True, text=True, timeout=180)

    def start_app(self, *, cwd=None, pythonpath=None, extra_env=None,
                  extra_args=()):
        command = self._entry_command(
            "--config", str(self.config_path), *extra_args)
        self._app_stdout = open(self.app_stdout_path, "wb")
        self._app_stderr = open(self.app_stderr_path, "wb")
        self.app = subprocess.Popen(
            command, cwd=str(Path(cwd) if cwd else self.root),
            env=self._app_env(pythonpath=pythonpath, extra_env=extra_env),
            stdin=subprocess.DEVNULL,
            stdout=self._app_stdout, stderr=self._app_stderr)
        return self.app

    def wait_health(self, timeout=90.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            if self.app is not None and self.app.poll() is not None:
                raise AssertionError(
                    "local app exited rc=%s\n%s"
                    % (self.app.returncode, _tail(self.app_stderr_path)))
            try:
                status, payload = http_request(
                    self.app_port, "GET", "/health", timeout=0.5)
            except OSError as exc:
                last = exc
            else:
                if status == 200 and isinstance(payload, dict) \
                        and payload.get("role") == "local":
                    return payload
                last = (status, payload)
            time.sleep(0.05)
        raise AssertionError(
            "local app did not become healthy: %r\n%s"
            % (last, _tail(self.app_stderr_path)))

    def request(self, method, path, *, body=None, token=None, host=None,
                timeout=5.0):
        return http_request(self.app_port, method, path, body=body, token=token,
                            host=host, timeout=timeout)

    def um_request(self, method, path, *, body=None, headers=None,
                   timeout=30.0):
        """One request against the fixture's own UM runtime (not 8000)."""
        return http_request(self.um_port, method, path, body=body,
                            headers=headers, timeout=timeout)

    def um_models(self):
        return self.um_request("GET", "/v1/models")

    def um_chat(self, prompt="hello"):
        return self.um_request(
            "POST", "/v1/chat/completions",
            body={"model": self.model, "stream": False,
                  "messages": [{"role": "user", "content": prompt}]})

    def shutdown_app(self, timeout=30.0):
        process = self.app
        if process is None:
            return self.app_returncode
        if process.poll() is None:
            try:
                http_request(self.app_port, "POST", "/admin/shutdown",
                              body={}, token=self.control_token,
                              timeout=5.0)
            except OSError:
                pass
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        self.app_returncode = process.returncode
        self._close_app_streams()
        return self.app_returncode

    def _close_app_streams(self):
        for handle in (self._app_stdout, self._app_stderr):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
        self._app_stdout = None
        self._app_stderr = None

    def close(self):
        try:
            self.shutdown_app()
        finally:
            try:
                self.stop_fake_um()
            finally:
                if not self.keep_root:
                    shutil.rmtree(self.root, ignore_errors=True)
