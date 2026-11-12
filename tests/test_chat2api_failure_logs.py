# -*- coding: utf-8 -*-
"""Failure-classification log lines for chat2api's 2026-09-14 502 storm.

The 2026-09-14 upstream outage produced dozens of fast HTTP 502s while
chat2api.log stayed silent: _error() answers the client but never logs, so
the discriminating fact (upstream status? connection-level exception? which
failure stage?) was unrecoverable from either side. Each failure origin in
_chat() now emits one "chat failure" line carrying a per-request id, the
failure stage (first call vs. the stream_options compat retry), the upstream
status code or exception class name, the downstream status, the model, the
stream flag and the elapsed time - and deliberately no response body (the
detail passed to _error may contain up to 300 chars of it), no request
messages and no credentials.

These tests drive a real local chat2api against stub upstreams on ephemeral
ports and pin the log-line contract plus the absence of sensitive content.
HTTP/SSE/retry/release behaviour is covered by test_chat2api_stream.py; these
tests only add observability assertions.
"""

import importlib.util
import json
import re
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _chat2api_http_harness as harness  # noqa: E402

CHAT2API_PATH = Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py"
SPEC = importlib.util.spec_from_file_location(
    "chat2api_failure_logs", CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)

STUB_TOKEN = "stub-token"

# Distinct marker that must never appear in a log line: it lives only in the
# upstream response body, which _error() may put into the JSON detail.
SECRET_BODY = "SECRET-UPSTREAM-BODY-7f3a"

_FIELDS = re.compile(r"(\w+)=(\S+)")


class StubUpstreamHandler(BaseHTTPRequestHandler):
    """Per-model failure scenarios; never streams for the tested models."""

    protocol_version = "HTTP/1.1"
    posts = 0

    def log_message(self, *args):
        pass

    def _send_json(self, code, obj):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        StubUpstreamHandler.posts += 1
        scenario = body.get("model", "")
        if scenario == "fail-non200":
            # Plain non-200 upstream rejection on the first call.
            self._send_json(503, {"error": {"message": SECRET_BODY}})
        elif scenario == "compat-retry-non200":
            # First call still carries stream_options -> 400 triggers the
            # compat retry; the retried call (no stream_options) then hits a
            # 503, so the log must say compat_retry_non_200.
            if "stream_options" in body:
                self._send_json(400, {"error": {"message": "no options"}})
            else:
                self._send_json(503, {"error": {"message": SECRET_BODY}})
        elif scenario == "compat-dead":
            # First call 400, retry dies before any response header so
            # requests raises ConnectionError (a RequestException subclass).
            if "stream_options" in body:
                self._send_json(400, {"error": {"message": "no options"}})
            else:
                self.close_connection = True
            return
        elif scenario == "ok-200":
            # 200 SSE start; used to reach the producer-start branch.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(b"data: {\"choices\":[]}\n\n")
            self.wfile.flush()
        else:
            self._send_json(200, {})


class TestChat2ApiFailureLogs(unittest.TestCase):
    def setUp(self):
        # B8-0 isolation lifecycle (see _chat2api_http_harness): the ledger
        # redirect is registered FIRST so it is released LAST - after every
        # handler drained and every server closed. The pre-fix LIFO bug
        # released the patch while a post-response handler was still writing
        # record_usage, which is how test rows reached the PRODUCTION
        # data/usage-chat2api.jsonl on 2026-10-02.
        self.tmp_dir, self.usage_log = harness.install_usage_isolation(
            self, chat2api)
        harness.assert_stub_credentials(
            chat2api, local_key=None, upstream_token=STUB_TOKEN,
            stub_token_literal=STUB_TOKEN)

        stub = harness.TrackedHTTPServer(("127.0.0.1", 0),
                                         StubUpstreamHandler)
        self.stub = stub
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        harness.install_tracked_server(self, stub, name="upstream-stub",
                                         require_drained=False)

        # A bound-then-closed port: connections are refused immediately, so
        # proxy.call raises requests.ConnectionError before any header.
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        self.dead_port = dead.getsockname()[1]
        dead.close()

        StubUpstreamHandler.posts = 0

    def start_chat2api(self, base_url):
        args = SimpleNamespace(
            base_url=base_url,
            token=STUB_TOKEN,
            profile="profile",
            login_timeout=1,
            use_api_key=False,
            no_browser=True,
        )
        proxy = chat2api.Proxy({"token": STUB_TOKEN}, args)
        server = harness.TrackedHTTPServer(("127.0.0.1", 0),
                                           chat2api.Handler)
        server.proxy = proxy
        threading.Thread(target=server.serve_forever, daemon=True).start()
        # Registered AFTER the redirect: this teardown (shutdown → bounded
        # drain of in-flight requests → close) runs BEFORE the USAGE_LOG
        # patch stop, so the post-response record_usage tail of the LAST
        # request lands in the temp ledger, never the production one.
        harness.install_tracked_server(self, server, name="chat2api-gateway")
        self.base = f"http://127.0.0.1:{server.server_port}"

        for patch in (
            # These tests pin the failure-classification contract, NOT the
            # auto-retry policy: disable the transparent retry budgets so one
            # stub failure stays one upstream attempt (retry behaviour has
            # its own dedicated tests).
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", False),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 0),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRY", False),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRIES", 0),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        chat2api._reader_slots = threading.BoundedSemaphore(
            chat2api.STREAM_MAX_CONCURRENT_READERS)

    def chat_post(self, model, stream=True):
        return requests.post(
            f"{self.base}/v1/chat/completions",
            json={"model": model,
                  "messages": [{"role": "user", "content": "hi"}],
                  "stream": stream},
            timeout=10)

    @staticmethod
    def failure_lines(cm):
        # assertLogs output is "WARNING:chat2api:<message>" on this runtime,
        # so match by substring rather than a bare startswith.
        return [line for line in cm.output if "chat failure" in line]

    def parse_fields(self, line):
        return dict(_FIELDS.findall(line))

    # ---------- failure stages ----------

    def test_upstream_non200_logged(self):
        self.start_chat2api(f"http://127.0.0.1:{self.stub.server_port}")
        with self.assertLogs("chat2api", level="WARNING") as cm:
            response = self.chat_post("fail-non200")
        self.assertEqual(response.status_code, 502)
        lines = self.failure_lines(cm)
        self.assertEqual(len(lines), 1)
        fields = dict(_FIELDS.findall(lines[0]))
        self.assertEqual(fields["stage"], "upstream_non_200")
        self.assertEqual(fields["upstream_status"], "503")
        self.assertEqual(fields["downstream"], "502")
        self.assertEqual(fields["exception"], "-")
        self.assertEqual(fields["stream"], "True")
        self.assertRegex(fields["request"], r"^[0-9a-f]{12}$")
        self.assertGreater(int(fields["duration_ms"]), 0)
        # Only the discriminating bits: no upstream body, no messages, no
        # credentials anywhere in the captured log output.
        self.assert_no_sensitive(cm.output)

    def test_non_stream_non200(self):
        self.start_chat2api(f"http://127.0.0.1:{self.stub.server_port}")
        with self.assertLogs("chat2api", level="WARNING") as cm:
            response = self.chat_post("fail-non200", stream=False)
        self.assertEqual(response.status_code, 502)
        fields = dict(_FIELDS.findall(
            self.failure_lines(cm)[0]))
        self.assertEqual(fields["stream"], "False")
        self.assertEqual(fields["stage"], "upstream_non_200")

    def test_compat_retry_non200_distinguished(self):
        self.start_chat2api(f"http://127.0.0.1:{self.stub.server_port}")
        with self.assertLogs("chat2api", level="WARNING") as cm:
            response = self.chat_post("compat-retry-non200")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(StubUpstreamHandler.posts, 2)
        lines = self.failure_lines(cm)
        self.assertEqual(len(lines), 1)
        fields = dict(_FIELDS.findall(lines[0]))
        self.assertEqual(fields["stage"], "compat_retry_non_200")
        self.assertEqual(fields["upstream_status"], "503")
        self.assertEqual(fields["downstream"], "502")
        self.assert_no_sensitive(cm.output)

    def test_connection_refused_classified(self):
        self.start_chat2api(f"http://127.0.0.1:{self.dead_port}")
        with self.assertLogs("chat2api", level="WARNING") as cm:
            response = self.chat_post("any-model")
        self.assertEqual(response.status_code, 502)
        fields = dict(_FIELDS.findall(
            self.failure_lines(cm)[0]))
        self.assertEqual(fields["stage"], "upstream_request_exception")
        self.assertEqual(fields["upstream_status"], "-")
        self.assertEqual(fields["exception"], "ConnectionError")
        self.assert_no_sensitive(cm.output)

    def test_compat_retry_connection_dead(self):
        self.start_chat2api(f"http://127.0.0.1:{self.stub.server_port}")
        with self.assertLogs("chat2api", level="WARNING") as cm:
            response = self.chat_post("compat-dead")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(StubUpstreamHandler.posts, 2)
        fields = dict(_FIELDS.findall(
            self.failure_lines(cm)[0]))
        self.assertEqual(fields["stage"], "compat_retry_request_exception")
        self.assertNotEqual(fields["exception"], "-")
        self.assert_no_sensitive(cm.output)

    def test_producer_start_failure_logged(self):
        self.start_chat2api(f"http://127.0.0.1:{self.stub.server_port}")
        with self.assertLogs("chat2api", level="WARNING") as cm, \
                mock.patch.object(chat2api._UpstreamStream, "start",
                                  side_effect=RuntimeError("producer boom")):
            response = self.chat_post("ok-200")
        self.assertEqual(response.status_code, 502)
        fields = dict(_FIELDS.findall(
            self.failure_lines(cm)[0]))
        self.assertEqual(fields["stage"], "producer_start_failed")
        self.assertEqual(fields["exception"], "RuntimeError")
        self.assertEqual(fields["upstream_status"], "-")
        self.assert_no_sensitive(cm.output)

    # ---------- helpers ----------

    def assert_no_sensitive(self, output):
        blob = "\n".join(output)
        self.assertNotIn(SECRET_BODY, blob)
        self.assertNotIn("stub-token", blob)
        self.assertNotIn('"hi"', blob)


if __name__ == "__main__":
    unittest.main()
