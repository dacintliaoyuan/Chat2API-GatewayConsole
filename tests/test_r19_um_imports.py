# -*- coding: utf-8 -*-
"""E19-1-02: chat2api imports only ``shared`` for the common code.

The private (local) app must not load the cloud share backend: the aggregator
comes from ``shared.nonstream_aggregate``, the Responses/Messages translations
come from ``shared.responses_protocol``, and the share loaders refuse to run when
``share_backend_role()`` is ``False`` (before they look at ``sys.modules`` or the
filesystem).  ``/admin/share/revoke`` then answers 503 without entering the
share handler.

The no-side-effect assertions run in a fresh interpreter so they stay valid when
the whole test suite imports host modules in the same process.
"""

import ast
import http.client
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

AI_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
CHAT2API_PATH = AI_GATEWAY_ROOT / "chat2api" / "chat2api.py"

# Ports the production launchers own. The test allocator below must never hand
# one of them to a real ``chat2api.main()`` child.
PRODUCTION_PORTS = frozenset({8000, 8001, 8096, 8097, 8100, 18096, 5901})

sys.path.insert(0, str(AI_GATEWAY_ROOT))

from shared import nonstream_aggregate as shared_aggregate  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "chat2api_r19_um_imports", CHAT2API_PATH)
assert SPEC is not None and SPEC.loader is not None
chat2api = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)


PROBE = """
import importlib.util
import json
import sys
from pathlib import Path

chat2api_path = Path(sys.argv[1]).resolve()
spec = importlib.util.spec_from_file_location(
    "chat2api_r19_probe", chat2api_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

before = {
    "share_auth": "share_auth" in sys.modules,
}

module._share_auth_module()

print(json.dumps({
    "package_root": str(module._PACKAGE_ROOT),
    "nonstream_is_shared":
        module._nonstream_aggregate is sys.modules.get(
            "shared.nonstream_aggregate"),
    "before": before,
    "after": {
        "share_auth": "share_auth" in sys.modules,
    },
}))
"""


def _imports_of(tree):
    """Yield (module, names, lineno) for every ImportFrom in the module."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            yield (node.module or "", [alias.name for alias in node.names],
                   node.lineno)


class Chat2apiSharedImportTests(unittest.TestCase):

    def test_nonstream_aggregator_comes_from_shared(self):
        self.assertIs(chat2api._nonstream_aggregate, shared_aggregate)
        self.assertIs(chat2api._NonStreamAggregateError,
                      shared_aggregate.AggregateError)
        self.assertIs(chat2api._NonStreamChatAggregator,
                      shared_aggregate.ChatCompletionAggregator)
        self.assertIs(chat2api._NonStreamAnthropicAggregator,
                      shared_aggregate.AnthropicMessageAggregator)

    def test_package_root_is_on_sys_path(self):
        self.assertEqual(
            Path(chat2api._PACKAGE_ROOT).resolve(), AI_GATEWAY_ROOT.resolve(),
            "chat2api must expose the bundled package root")
        self.assertIn(chat2api._PACKAGE_ROOT, sys.path,
                      "the package root must be importable as a script run")

    def test_chat2api_imports_the_share_gate(self):
        tree = ast.parse(CHAT2API_PATH.read_text(encoding="utf-8-sig"),
                         filename=str(CHAT2API_PATH))
        offenders = [
            "{}:{}".format(lineno, module)
            for module, names, lineno in _imports_of(tree)
            if module == "share_auth"
            or (module in ("", ".") and "share_auth" in names)
        ]
        self.assertTrue(
            offenders,
            "the merged 8100 process must import the in-process share gate lazily")

    def test_responses_translations_come_from_shared_protocol(self):
        tree = ast.parse(CHAT2API_PATH.read_text(encoding="utf-8-sig"),
                         filename=str(CHAT2API_PATH))
        shared = [
            names for module, names, _lineno in _imports_of(tree)
            if module == "shared.responses_protocol"
        ]
        self.assertGreaterEqual(
            len(shared), 2,
            "both Responses entry points must import shared.responses_protocol")
        imported = {name for names in shared for name in names}
        for required in ("valid_responses_request",
                         "responses_to_chat_request",
                         "responses_to_messages_request",
                         "messages_stream_events"):
            self.assertIn(required, imported,
                          "shared.responses_protocol must export {}"
                          .format(required))

    def test_both_responses_entry_points_import_from_shared_protocol(self):
        """Each entry point imports the translations itself, not via a local copy.

        The two Responses entry points are ``Handler._responses`` (chat
        translation) and ``Handler._responses_via_messages`` (image-bearing tool
        results switch upstream protocol). A single shared import at module top
        would not prove either entry point uses the shared translation, so the
        import is asserted INSIDE each function body.
        """
        tree = ast.parse(CHAT2API_PATH.read_text(encoding="utf-8-sig"),
                         filename=str(CHAT2API_PATH))
        entry_points = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in (
                    "_responses", "_responses_via_messages"):
                entry_points[node.name] = node
        self.assertEqual(sorted(entry_points),
                         ["_responses", "_responses_via_messages"],
                         "both Responses entry points must exist")
        for name, node in entry_points.items():
            with self.subTest(entry_point=name):
                modules = {
                    child.module
                    for child in ast.walk(node)
                    if isinstance(child, ast.ImportFrom)
                }
                self.assertIn(
                    "shared.responses_protocol", modules,
                    "{} must import shared.responses_protocol".format(name))

    def test_revoke_endpoint_without_gate_answers_503(self):
        original_log_message = chat2api.Handler.log_message
        chat2api.Handler.log_message = lambda *_args: None
        server = ThreadingHTTPServer(("127.0.0.1", 0), chat2api.Handler)
        server.proxy = None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(setattr, chat2api.Handler, "log_message",
                        original_log_message)
        self.addCleanup(stop)
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=5)
        try:
            connection.request(
                "POST", "/admin/share/revoke",
                body=json.dumps({"ok": True}).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Connection": "close"})
            response = connection.getresponse()
            status = response.status
            payload = json.loads(response.read() or b"{}")
        finally:
            connection.close()
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["type"], "api_error")
        self.assertEqual(payload["error"]["code"],
                         "share_revoke_unassembled")
        self.assertEqual(payload["error"]["detail"]["released"], 0)
        self.assertEqual(payload["error"]["detail"]["reason"],
                         "share_backend_unassembled")

    def test_clean_interpreter_imports_do_not_load_share_backend(self):
        env = {key: value for key, value in os.environ.items()
               if key.upper() != "PYTHONPATH"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["CHAT2API_BUILD"] = "r19-test"
        with tempfile.TemporaryDirectory() as isolated_cwd:
            env["HOME"] = isolated_cwd
            env["USERPROFILE"] = isolated_cwd
            completed = subprocess.run(
                [sys.executable, "-I", "-c", PROBE, str(CHAT2API_PATH)],
                cwd=isolated_cwd,
                env=env,
                capture_output=True,
                encoding="utf-8",
                timeout=60,
            )
        self.assertEqual(completed.returncode, 0,
                         "clean interpreter probe failed: {}".format(
                             completed.stderr))
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(Path(payload["package_root"]).resolve(),
                         AI_GATEWAY_ROOT.resolve())
        self.assertIs(payload["nonstream_is_shared"], True,
                      "chat2api must load the aggregator from shared")
        self.assertEqual(payload["before"], {"share_auth": False},
                         "the private import must not load the share gate")
        self.assertEqual(payload["after"], {"share_auth": True},
                         "the lazy loader must import the in-process share gate")

    def test_missing_shared_package_fails_the_import_closed(self):
        """No second copy: a bundle without ``shared`` must fail, not degrade.

        The module is copied ALONE into an empty directory, so the package root
        the module inserts on ``sys.path`` is that empty directory and the
        bundled ``shared`` sibling cannot be found. A silent fallback (a second
        in-file aggregator) would import cleanly and hide a broken package.
        """
        with tempfile.TemporaryDirectory() as bundle:
            copied = Path(bundle) / "chat2api.py"
            shutil.copyfile(CHAT2API_PATH, copied)
            env = {key: value for key, value in os.environ.items()
                   if key.upper() not in ("PYTHONPATH", "PYTHONHOME")}
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            completed = subprocess.run(
                [sys.executable, "-I", "-c",
                 "import sys; sys.path.insert(0, '.'); import chat2api"],
                cwd=bundle, env=env, capture_output=True,
                encoding="utf-8", errors="replace", timeout=60)
        self.assertNotEqual(
            completed.returncode, 0,
            "importing chat2api without shared must fail, not fall back")
        self.assertIn("ModuleNotFoundError", completed.stderr)
        self.assertIn("No module named 'shared'", completed.stderr,
                      "the failure must name the missing shared package; "
                      "stderr was: %r" % (completed.stderr[-800:],))


class _FakeUpstream(BaseHTTPRequestHandler):
    """Minimal UM stand-in: records the Authorization header of every probe."""

    def do_GET(self):
        self.server.observed.append(
            (self.path, self.headers.get("Authorization")))
        if self.path == "/api/config":
            payload = {"version": "r19-test", "features": {}}
        elif self.path == "/api/models":
            payload = {"data": [{"id": "fake-model"}]}
        else:
            payload = {}
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def _free_loopback_port():
    """One free loopback port that is NOT a production listener.

    A real 8000/8001/8096/8097/8100/18096/5901 could otherwise be handed to
    a real ``chat2api.main()`` child.
    """
    for _ in range(100):
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        finally:
            sock.close()
        if port not in PRODUCTION_PORTS:
            return port
    raise RuntimeError("no free loopback port outside the production ports")


class FreeLoopbackPortTests(unittest.TestCase):
    def test_the_allocator_never_returns_a_production_port(self):
        ports = {_free_loopback_port() for _ in range(60)}
        self.assertFalse(ports & PRODUCTION_PORTS,
                         "the allocator returned a production port: %r"
                         % sorted(ports & PRODUCTION_PORTS))


class Chat2apiUpstreamTokenEnvTests(unittest.TestCase):
    """E19-1-S2 step 5: ``CHAT2API_UPSTREAM_TOKEN`` keeps the token out of argv.

    ``chat2api.main()`` resolves the upstream credential in this order:
    explicit ``--token`` > ``CHAT2API_UPSTREAM_TOKEN`` > the ``TokenStore``
    file. ``--no-browser`` accepts either source. Every case runs the REAL
    ``main()`` in a child interpreter against a fake upstream, with HOME, the
    token store, service log, usage ledger, control token, reauth slot and
    attribution key all redirected into a temp directory; PYTHONPATH
    is dropped so the repo cannot be reached through an inherited path.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
        self.upstream.observed = []
        self.upstream.daemon_threads = True
        upstream_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True)
        upstream_thread.start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        self.upstream_url = "http://127.0.0.1:%d" % (
            self.upstream.server_address[1])

        self.children = []
        self.addCleanup(self._kill_children)

    def _kill_children(self):
        for proc in self.children:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass

    def _env(self, upstream_token=None):
        env = {key: value for key, value in os.environ.items()
               if key.upper() not in ("PYTHONPATH", "PYTHONHOME")}
        env.pop("HTTP_PROXY", None)
        env.pop("HTTPS_PROXY", None)
        env.pop("CHAT2API_LOCAL_API_KEY", None)
        env.pop("CHAT2API_UPSTREAM_TOKEN", None)
        env.update({
            "HOME": str(self.dir),
            "USERPROFILE": str(self.dir),
            "TMPDIR": str(self.dir),
            "TEMP": str(self.dir),
            "TMP": str(self.dir),
            "NO_PROXY": "*",
            "PYTHONDONTWRITEBYTECODE": "1",
            "CHAT2API_BUILD": "r19-test",
            "CHAT2API_TOKEN_FILE": str(self.dir / "token.json"),
            "CHAT2API_SERVICE_LOG": str(self.dir / "service.log"),
            "CHAT2API_USAGE_LOG": str(self.dir / "usage.jsonl"),
            "CHAT2API_CONTROL_TOKEN_FILE": str(self.dir / "control-token"),
            "CHAT2API_REAUTH_SLOT_FILE": str(self.dir / "reauth.state"),
            "CHAT2API_SHARE_ATTRIBUTION_KEY_FILE": str(self.dir / "attr.key"),
        })
        if upstream_token is not None:
            env["CHAT2API_UPSTREAM_TOKEN"] = upstream_token
        self._assert_isolation_is_complete(env)
        return env

    def _assert_isolation_is_complete(self, env):
        """Refuse to launch when a state path is NOT covered by this temp dir.

        A missing override would send the real ``main()`` at a production file (the
        service log, the token store, the ledger, the control token, the reauth
        slot or the attribution key) - exactly the accident this
        suite exists to prevent. Every one of those variables is set by the dict
        above, so a future edit that drops one fails here instead of touching
        real state.
        """
        required = (
            "CHAT2API_TOKEN_FILE", "CHAT2API_SERVICE_LOG",
            "CHAT2API_USAGE_LOG", "CHAT2API_CONTROL_TOKEN_FILE",
            "CHAT2API_REAUTH_SLOT_FILE",
            "CHAT2API_SHARE_ATTRIBUTION_KEY_FILE",
        )
        missing = [name for name in required if not env.get(name)]
        self.assertEqual(missing, [],
                         "isolation is incomplete; unset: {}".format(
                             ", ".join(missing)))
        escaped = [
            name for name in required
            if not Path(env[name]).resolve().is_relative_to(
                self.dir.resolve())
        ]
        self.assertEqual(escaped, [],
                         "these state paths escape the temp dir: {}".format(
                             ", ".join(escaped)))
        for name in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
            self.assertTrue(
                Path(env[name]).resolve().is_relative_to(self.dir.resolve()),
                "{} must point into the temp dir".format(name))

    def _argv(self, port, extra):
        # -I implies -E, which ignores only the PYTHON* variables (PYTHONPATH,
        # PYTHONHOME); an ordinary variable such as CHAT2API_UPSTREAM_TOKEN still
        # reaches the child. Isolation therefore comes from -I plus the scrubbed
        # environment, and a script run puts only its own directory on sys.path[0],
        # so cwd cannot reach the repository.
        return [sys.executable, "-I", str(CHAT2API_PATH),
                "--host", "127.0.0.1", "--port", str(port),
                "--base-url", self.upstream_url,
                "--profile", str(self.dir / "profile")] + list(extra)

    def _start(self, upstream_token=None, extra_argv=()):
        port = _free_loopback_port()
        proc = subprocess.Popen(
            self._argv(port, extra_argv),
            cwd=str(self.dir), env=self._env(upstream_token),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(proc)
        self._await_health(port, proc)
        return proc, port

    def _await_health(self, port, proc, timeout=60.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.fail("chat2api exited before becoming healthy (rc=%s)"
                          % proc.poll())
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", port, timeout=5)
                try:
                    connection.request("GET", "/health")
                    if connection.getresponse().status == 200:
                        return
                finally:
                    connection.close()
            except OSError:
                pass
            time.sleep(0.1)
        self.fail("chat2api never answered /health on port %d" % port)

    def _await_models_probe(self, timeout=30.0):
        """Wait for the startup credential probe, then return its bearer."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for path, authorization in list(self.upstream.observed):
                if path == "/api/models":
                    return authorization
            time.sleep(0.1)
        self.fail("the startup credential probe never reached the upstream; "
                  "observed: %r" % (self.upstream.observed,))

    def test_env_token_with_no_browser_starts_and_reaches_upstream(self):
        proc, _port = self._start(upstream_token="env-token-7f3a",
                                  extra_argv=["--no-browser"])
        self.assertIsNone(proc.poll(), "the instance must stay up")
        self.assertEqual(self._await_models_probe(), "Bearer env-token-7f3a")
        self.assertFalse((self.dir / "token.json").exists(),
                         "an env credential must not be persisted to the store")

    def test_missing_token_and_env_with_no_browser_exits_two(self):
        port = _free_loopback_port()
        completed = subprocess.run(
            self._argv(port, ["--no-browser"]),
            cwd=str(self.dir), env=self._env(),
            stdin=subprocess.DEVNULL, capture_output=True,
            encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(completed.returncode, 2,
                         "an unusable --no-browser run must exit 2: %r"
                         % (completed.stderr[-800:],))
        self.assertIn("--no-browser", completed.stderr)
        self.assertIn("CHAT2API_UPSTREAM_TOKEN", completed.stderr,
                      "the refusal must name the environment alternative")

    def test_explicit_token_wins_over_env(self):
        self._start(upstream_token="env-token-7f3a",
                    extra_argv=["--no-browser", "--token", "cli-token-1"])
        self.assertEqual(self._await_models_probe(), "Bearer cli-token-1")

    def test_env_token_wins_over_token_store_file(self):
        (self.dir / "token.json").write_text(json.dumps({
            "url": self.upstream_url,
            "token": "store-token-2",
            "api_key": None,
        }), encoding="utf-8")
        self._start(upstream_token="env-token-7f3a",
                    extra_argv=["--no-browser"])
        self.assertEqual(self._await_models_probe(), "Bearer env-token-7f3a")

    def test_login_keeps_its_sign_in_meaning_with_the_env_set(self):
        """``--login`` signs in; a borrowed env token must not fake success.

        The interactive entry is the documented "sign in and save the token, then
        exit" path. Feeding it the launcher's environment credential would print
        ``Signed in, token saved`` while nothing was signed in or saved, so the env
        token applies to the START path only. ``--login --no-browser`` is the
        cheapest observable form: the token check still refuses (exit 2) because the
        env value never becomes ``args.token`` on this entry.
        """
        port = _free_loopback_port()
        completed = subprocess.run(
            self._argv(port, ["--login", "--no-browser"]),
            cwd=str(self.dir), env=self._env(upstream_token="env-token-7f3a"),
            stdin=subprocess.DEVNULL, capture_output=True,
            encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(completed.returncode, 2,
                         "--login must not consume the launcher's env token: %r"
                         % (completed.stderr[-800:],))
        self.assertIn("--no-browser requires", completed.stderr)
        self.assertFalse((self.dir / "token.json").exists(),
                         "a refused sign-in must not write a credential")


if __name__ == "__main__":
    unittest.main()
