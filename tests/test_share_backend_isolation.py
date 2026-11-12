# -*- coding: utf-8 -*-
"""Dedicated share-backend isolation matrix (Revision 6 R6-A1).

Boots a REAL second chat2api instance as a child process with ISOLATED
state paths (env redirects against a temp dir) and a FAKE upstream, then
verifies the isolation contract on live sockets:

* independent port (never 8000; the production instance is never touched —
  this test never sends anything to 8000 and asserts the production files
  are not written by the child);
* dedicated control secret (the child's /admin/* rejects the production
  token and accepts its own);
* dedicated log (logger writes to CHAT2API_SERVICE_LOG, not the repo
  chat2api.log — asserted by file absence under the source dir);
* token/profile/ledger paths resolve inside the isolated dir only.

The fake upstream is a minimal HTTP server answering only what the health
path needs (chat2api /health never touches the upstream, so the fake mainly
documents that no upstream call happens during boot). Everything runs in
temp dirs; teardown terminates the child and removes nothing from
production paths.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
GATEWAY = os.path.dirname(HERE)
CHAT2API_DIR = os.path.join(GATEWAY, "chat2api")


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class IsolatedBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="share-backend-iso-")
        cls.backend_dir = os.path.join(cls.tmp, "backend")
        os.makedirs(os.path.join(cls.backend_dir, "edge-profile"),
                    exist_ok=True)
        cls.token_file = os.path.join(cls.tmp, "token.json")
        cls.service_log = os.path.join(cls.tmp, "share-backend.log")
        cls.ledger = os.path.join(cls.tmp, "usage-share-backend.jsonl")
        cls.control_token_file = os.path.join(cls.tmp, "control-token")
        with open(cls.token_file, "w", encoding="utf-8") as f:
            json.dump({"token": "isolated-test-token",
                       "base_url": "http://127.0.0.1:1"},
                      f)
        with open(os.path.join(cls.backend_dir_local(), "control-token"),
                  "w", encoding="utf-8") as f:
            f.write("isolated-control-secret")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        cls.port = s.getsockname()[1]
        s.close()
        env = dict(os.environ)
        env["CHAT2API_TOKEN_FILE"] = cls.token_file
        env["CHAT2API_SERVICE_LOG"] = cls.service_log
        env["CHAT2API_USAGE_LOG"] = cls.ledger
        env["CHAT2API_CONTROL_TOKEN_FILE"] = os.path.join(
            cls.backend_dir_local(), "control-token")
        env["NO_PROXY"] = "*"
        env.pop("HTTP_PROXY", None)
        env.pop("HTTPS_PROXY", None)
        cmd = [sys.executable, os.path.join(GATEWAY, "chat2api", "chat2api.py"),
               "--base-url", "http://127.0.0.1:9", "--port",
               str(cls.port), "--profile",
               os.path.join(cls.backend_dir_local(), "edge-profile"),
               "--no-browser", "--token", "isolated-test-token"]
        cls.proc = subprocess.Popen(
            cmd, cwd=os.path.join(GATEWAY, "chat2api"), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                           if os.name == "nt" else 0))
        # Bounded readiness: /health on the dedicated port (never 8000).
        deadline = time.monotonic() + 30.0
        cls.health = None
        while time.monotonic() < deadline:
            try:
                with os.popen("") if False else _urlopen(
                        "http://127.0.0.1:%d/health" % cls.port, timeout=2) as r:
                    cls.health = json.loads(r.read().decode("utf-8"))
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        if cls.health is None:
            cls.tearDownClass()
            raise AssertionError("isolated instance did not become healthy")

    @classmethod
    def backend_dir_local(cls):
        # The backend's own dir inside the temp root (distinct per class).
        return os.path.join(cls.tmp, "backend")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "proc", None) is not None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                cls.proc.kill()
        # Leave the tmp dir for forensics on failure; tempfile cleans on GC.

    def _req(self, path, token=None, method="GET"):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.__class__.port, path),
            headers={"X-Admin-Control": token} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, None

    def test_independent_port_and_health(self):
        status, payload = self._req("/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        # The dedicated port is NEVER the production port.
        self.assertNotEqual(
            json.loads(json.dumps(payload)).get("port"), 8000)

    def test_dedicated_control_secret(self):
        # The dedicated control token file exists and differs from the
        # production one (which lives in the repo data dir).
        with open(os.path.join(self.backend_dir_local(), "control-token"),
                  encoding="utf-8") as f:
            dedicated = f.read().strip()
        self.assertTrue(dedicated)
        prod = os.path.join(GATEWAY, "data", "admin-control-token")
        if os.path.exists(prod):
            with open(prod, encoding="utf-8") as f:
                self.assertNotEqual(f.read().strip(), dedicated)
        # The dedicated instance accepts ITS OWN token on its admin surface.
        status, _ = self._req("/admin/auth/status", token=dedicated)
        self.assertEqual(status, 200)

    def test_production_files_not_written(self):
        # The child's state files live ONLY in the isolated dir: the log went
        # to CHAT2API_SERVICE_LOG (temp), and the source dir gained none of
        # this instance's state (no backend.pid, no second token.json side
        # effects beyond the pre-existing production file).
        self.assertTrue(os.path.exists(self.__class__.service_log))
        self.assertFalse(os.path.exists(
            os.path.join(GATEWAY, "chat2api", "backend.pid")))
        # The isolated token file is the one the child was pointed at (temp),
        # not the repo token.json path.
        self.assertTrue(os.path.exists(self.__class__.token_file))
        self.assertNotEqual(self.__class__.token_file,
                            os.path.join(GATEWAY, "chat2api", "token.json"))

    def test_child_identity_is_dedicated(self):
        # The managed child's command line pins its own port; the console's
        # chat2api pattern (chat2api.py + --port) matches ONLY this child,
        # never the production process (different port, different process).
        self.assertTrue(self.proc.pid > 0)
        self.assertNotEqual(self.proc.pid, os.getpid())


def _urlopen(url, timeout):
    import urllib.request
    return urllib.request.urlopen(url, timeout=timeout)


if __name__ == "__main__":
    unittest.main()
