# -*- coding: utf-8 -*-
"""Single-process Linux share chain.

Starts the REAL merged ``chat2api.py`` on 28100 with a projection config and
the fake UM on 28999. The old two-process harness (28100 share gateway +
28110 dedicated backend) is retired for this check.

The fixture reads and writes only temporary state; it never touches production
data or a real UM.
"""

import argparse
import hashlib
import http.client
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
TESTS = HERE / "tests"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(TESTS))

SHARE_PORT = 28100
FAKE_UM_PORT = 28999
MODEL = "test-model"
KEY_ID = "friend"
SECRET = "test-secret"


def _http(port, method, path, *, body=None, headers=None, timeout=10.0):
    connection = http.client.HTTPConnection("127.0.0.1", port,
                                           timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        raw = response.read()
        return response.status, raw
    finally:
        connection.close()


def _wait_for(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


class _Chain:
    def __init__(self, root):
        self.root = root
        self.process = None
        self.fake = None
        self.control_token = root / "control-token"
        self.control_token.write_text("control-" + secrets.token_hex(16),
                                      encoding="utf-8")
        self.projection = root / "projection.json"
        self.projection.write_text(json.dumps({
            "schema_version": 2,
            "config_generation": 1,
            "share_max_concurrency": 4,
            "data": [{
                "key_id": KEY_ID,
                "name": "friend",
                "secret_hash": hashlib.sha256(SECRET.encode()).hexdigest(),
                "secret_version": 1,
                "enabled": True,
                "max_concurrency": 2,
            }],
        }), encoding="utf-8")
        self.ledger = root / "usage.jsonl"
        self.price = root / "prices.json"
        self.price.write_text(json.dumps({"version": "test", "models": {}}),
                              encoding="utf-8")

    def start_fake_um(self):
        from fake_um.fake_um_server import FakeUM
        self.fake_token = secrets.token_urlsafe(32)
        self.fake = FakeUM(port=FAKE_UM_PORT, token=self.fake_token,
                           model=MODEL)
        self.fake.start()
        if not _wait_for(
                lambda: _http(FAKE_UM_PORT, "GET", "/api/models",
                              headers={"Authorization": "Bearer " + self.fake_token})[0] == 200,
                10.0):
            raise SystemExit("fake UM did not start")

    def start_chat2api(self):
        env = dict(os.environ)
        env["CHAT2API_CONTROL_TOKEN_FILE"] = str(self.control_token)
        env["CHAT2API_TOKEN_FILE"] = str(self.root / "token.json")
        env["CHAT2API_SERVICE_LOG"] = str(self.root / "chat2api.log")
        env["CHAT2API_USAGE_LOG"] = str(self.ledger)
        env["CHAT2API_PRICE_TABLE_FILE"] = str(self.price)
        env["CHAT2API_SHARE_MAX_REQUEST_BYTES"] = str(32 * 1024 * 1024)
        command = [
            sys.executable, str(HERE / "chat2api" / "chat2api.py"),
            "--host", "127.0.0.1",
            "--port", str(SHARE_PORT),
            "--base-url", "http://127.0.0.1:%d" % FAKE_UM_PORT,
            "--token", self.fake_token,
            "--no-browser",
            "--profile", str(self.root / "profile"),
            "--share-config", str(self.projection),
            "--auth-mode", "projection",
        ]
        self.process = subprocess.Popen(
            command, cwd=str(HERE), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if not _wait_for(
                lambda: _http(SHARE_PORT, "GET", "/health")[0] == 200,
                30.0):
            output = ""
            if self.process is not None:
                try:
                    self.process.terminate()
                    output = self.process.communicate(timeout=5)[0]
                except (OSError, subprocess.SubprocessError):
                    pass
            raise SystemExit("merged 8100 did not start:\n" + output)

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.fake is not None:
            self.fake.stop()


def _request(path, body=None, key=None):
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    return _http(SHARE_PORT, "POST" if body is not None else "GET", path,
                 body=None if body is None else json.dumps(body).encode(),
                 headers=headers, timeout=30.0)


def _check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ((": " + detail) if detail else ""))
    return bool(ok)


def run():
    with tempfile.TemporaryDirectory(prefix="um-share-single-") as tmp:
        chain = _Chain(Path(tmp))
        checks = []
        try:
            chain.start_fake_um()
            chain.start_chat2api()
            checks.append(_check(
                "models without key is 401",
                _request("/v1/models")[0] == 401))
            status, raw = _request("/v1/models", key=SECRET)
            checks.append(_check("models with key is 200", status == 200,
                                raw[:120].decode("utf-8", "replace")))
            body = {"model": MODEL,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False}
            status, raw = _request("/v1/chat/completions", body=body,
                                   key=SECRET)
            checks.append(_check("chat with key is 200", status == 200,
                                raw[:160].decode("utf-8", "replace")))
            large = {"model": MODEL,
                     "messages": [{"role": "user",
                                   "content": "x" * (1200 * 1024)}],
                     "stream": False}
            status, _raw = _request("/v1/chat/completions", body=large,
                                    key=SECRET)
            checks.append(_check("1.2 MiB body is not 413", status != 413,
                                str(status)))
            projection = json.loads(
                chain.projection.read_text(encoding="utf-8"))
            projection["config_generation"] = 2
            projection["data"][0]["enabled"] = False
            chain.projection.write_text(json.dumps(projection),
                                        encoding="utf-8")
            status, raw = _http(
                SHARE_PORT, "POST", "/admin/share/revoke",
                body=json.dumps({"key_id": KEY_ID}).encode(),
                headers={"Content-Type": "application/json",
                         "X-Admin-Control":
                             chain.control_token.read_text(encoding="utf-8").strip()})
            checks.append(_check("revoke with control token is 200",
                                status == 200,
                                raw[:120].decode("utf-8", "replace")))
            checks.append(_check(
                "revoked key is 401",
                _request("/v1/models", key=SECRET)[0] == 401))
        finally:
            chain.stop()
        return all(checks)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="core")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)
    ok = run()
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({
            "scenario": args.scenario,
            "result": "PASS" if ok else "FAIL",
        }), encoding="utf-8")
    print("RESULT " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
