"""Exit records (round 19, L1-L4): one real chat2api main() per test, isolated.

Each instance runs the REAL ``chat2api.main()`` in a child process on an
ephemeral port against a local fake upstream, with every state file and the
service log redirected into a temp directory (never the production
``chat2api.log``, token store, control token or port 8000). Only the last-mile
``browser_login`` is replaced, and it fails loudly if reached.

Contract pinned here:
  * startup names its instance: role, host, port, pid, ppid, launched_by;
  * ``listening`` follows only after the bind, with the same port and pid;
  * a graceful /admin/shutdown leaves exit begin (reason=admin_shutdown) ->
    exit complete, in that order, for that pid;
  * an external TerminateProcess leaves NO exit record (the documented blind
    spot: a missing exit line means "unknown", not "running");
  * the control token never appears in the log; an unexpected launch source
    value is recorded as ``invalid`` rather than echoed.
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CHAT2API_PATH = Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py"
CONTROL_HEADER = "X-Admin-Control"
CONTROL_TOKEN = "exit-records-control-secret-7f3a"

_CHILD_SCRIPT = r'''
import importlib.util, json, sys

def main():
    module_path, plan_path = sys.argv[1:3]
    spec = importlib.util.spec_from_file_location("chat2api_exit_child", module_path)
    chat2api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chat2api)
    with open(plan_path, encoding="utf-8") as f:
        plan = json.load(f)

    def no_browser(*_args, **_kwargs):
        raise RuntimeError("exit-records test must never open a login window")

    chat2api.browser_login = no_browser
    sys.argv = [module_path] + plan["argv"]
    chat2api.main()

main()
'''


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _Upstream(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/config":
            payload = json.dumps({"version": "test", "features": {}}).encode()
        else:
            payload = json.dumps({"object": "list", "data": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


class Chat2ApiExitRecordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
        cls.upstream_url = "http://127.0.0.1:%d" % cls.upstream.server_port
        cls.upstream_thread = threading.Thread(
            target=cls.upstream.serve_forever, daemon=True)
        cls.upstream_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.upstream_thread.join()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self.tmp.name)
        self.script = self.dir / "child.py"
        self.script.write_text(_CHILD_SCRIPT, encoding="utf-8")
        self.children = []

    def tearDown(self):
        for proc in self.children:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        self.tmp.cleanup()

    def _start(self, name, profile_name="profile", launch_source="testsuite"):
        token_file = self.dir / ("%s-token.json" % name)
        token_file.write_text(json.dumps({"token": "t-%s" % name, "api_key": None}),
                              encoding="utf-8")
        control_file = self.dir / ("%s-control" % name)
        control_file.write_text(CONTROL_TOKEN, encoding="utf-8")
        port = _free_port()
        plan = {"argv": ["--base-url", self.upstream_url, "--port", str(port),
                         "--profile", str(self.dir / profile_name),
                         "--token", "t-%s" % name, "--login-timeout", "30"]}
        plan_path = self.dir / ("%s-plan.json" % name)
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        service_log = self.dir / ("%s.log" % name)
        env = dict(os.environ)
        env.update({
            "CHAT2API_TOKEN_FILE": str(token_file),
            "CHAT2API_SERVICE_LOG": str(service_log),
            "CHAT2API_USAGE_LOG": str(self.dir / ("%s-usage.jsonl" % name)),
            "CHAT2API_CONTROL_TOKEN_FILE": str(control_file),
            "CHAT2API_REAUTH_SLOT_FILE": str(self.dir / ("%s-reauth.state" % name)),
            "CHAT2API_LAUNCH_SOURCE": launch_source,
            "NO_PROXY": "*",
        })
        env.pop("HTTP_PROXY", None)
        env.pop("HTTPS_PROXY", None)
        with open(str(self.dir / ("%s-child.out" % name)), "wb") as out:
            proc = subprocess.Popen(
                [sys.executable, str(self.script), str(CHAT2API_PATH), str(plan_path)],
                cwd=str(CHAT2API_PATH.parent), env=env,
                stdin=subprocess.DEVNULL, stdout=out, stderr=out)
        self.children.append(proc)
        self._await_health(port, proc, name)
        return proc, port, service_log

    def _await_health(self, port, proc, name, timeout=60.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    conn.request("GET", "/health")
                    if conn.getresponse().status == 200:
                        return
                finally:
                    conn.close()
            except OSError as exc:
                last = exc
            time.sleep(0.2)
        out = (self.dir / ("%s-child.out" % name)).read_text(
            encoding="utf-8", errors="replace")[-1500:]
        self.fail("instance %s never became healthy (rc=%s, last=%r): %s"
                  % (name, proc.poll(), last, out))

    @staticmethod
    def _lines(service_log):
        return service_log.read_text(encoding="utf-8").splitlines()

    def _index(self, lines, needle):
        for i, line in enumerate(lines):
            if needle in line:
                return i
        self.fail("missing log record %r in:\n%s" % (needle, "\n".join(lines)))

    def test_graceful_shutdown_leaves_ordered_identity_bound_records(self):
        proc, port, service_log = self._start("graceful")
        pid = proc.pid
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("POST", "/admin/shutdown", body=b"{}",
                         headers={CONTROL_HEADER: CONTROL_TOKEN,
                                  "Content-Type": "application/json"})
            self.assertEqual(conn.getresponse().status, 202)
        finally:
            conn.close()
        self.assertEqual(proc.wait(timeout=30), 0)

        lines = self._lines(service_log)
        startup = self._index(
            lines, "role=private host=127.0.0.1 port=%d pid=%d ppid=%d launched_by=testsuite"
            % (port, pid, os.getpid()))
        self.assertIn("startup build=", lines[startup])
        listening = self._index(lines, "listening host=127.0.0.1 port=%d pid=%d" % (port, pid))
        begin = self._index(
            lines, "exit begin port=%d pid=%d reason=admin_shutdown" % (port, pid))
        complete = self._index(lines, "exit complete port=%d pid=%d" % (port, pid))
        self.assertLess(startup, listening)
        self.assertLess(listening, begin)
        self.assertLess(begin, complete)
        self.assertNotIn(CONTROL_TOKEN, service_log.read_text(encoding="utf-8"))

    def test_external_termination_leaves_no_exit_record(self):
        proc, port, service_log = self._start("killed")
        pid = proc.pid
        proc.kill()  # TerminateProcess: no finally, no atexit
        proc.wait(timeout=30)

        lines = self._lines(service_log)
        self._index(lines, "listening host=127.0.0.1 port=%d pid=%d" % (port, pid))
        text = "\n".join(lines)
        self.assertNotIn("exit begin port=%d pid=%d" % (port, pid), text)

    def test_share_backend_role_and_invalid_launch_source(self):
        proc, port, service_log = self._start(
            "share", profile_name="share-backend-profile", launch_source="Not A Token")
        lines = self._lines(service_log)
        line = lines[self._index(lines, "port=%d pid=%d" % (port, proc.pid))]
        self.assertIn("role=share-backend", line)
        self.assertIn("launched_by=invalid", line)
        self.assertNotIn("Not A Token", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
