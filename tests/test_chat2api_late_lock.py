# -*- coding: utf-8 -*-
"""Late-lock repetition observability contract (post-output guard cuts).

Pins the behavior of the repetition guard when the loop is cut AFTER visible
output has reached the downstream client (content or a tool call was already
streamed): the terminal cut must be visible at WARNING level with
structure-only fields, and the in-band SSE error must carry a stable
``[chat2api:degenerate_repetition]`` marker plus ``error.code`` and a
structure-only ``detail`` — because the installed local client path
(``@ai-sdk/openai-compatible``) may drop ``error.code`` and only forward
``error.message`` upstream (public SDK issue evidence).

Also pins the retry-switch contract: with CHAT2API_REPETITION_AUTO_RETRY=0
the preflight must not keep hidden repetition retries alive merely because
transport (UPSTREAM_AUTO_RETRY) preflight is enabled.

External-authored tests (web-side AI, based on fe88798d1); integrated into
the repo test layout by the local executor.
"""

import importlib.util
import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _chat2api_http_harness as harness  # noqa: E402

CHAT2API_PATH = Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py"
SPEC = importlib.util.spec_from_file_location(
    "chat2api_late_lock", CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)

STUB_TOKEN = "stub"


def chunk(delta, finish_reason=None):
    payload = json.dumps({"choices": [{"delta": delta,
                                        "finish_reason": finish_reason}]})
    return f"data: {payload}\n\n".encode()


class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    counts = {}

    def log_message(self, *args):
        pass

    def send_chunk(self, data):
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        model = body.get("model", "")
        Stub.counts[model] = Stub.counts.get(model, 0) + 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.close_connection = True
        try:
            if model == "late-content":
                self.send_chunk(chunk({"content": "visible"}))
            elif model == "late-tool":
                self.send_chunk(chunk({"tool_calls": [{
                    "index": 0, "id": "call_1", "type": "function",
                    "function": {"name": "Bash", "arguments": "{}"}
                }]}))
            for _ in range(6):
                self.send_chunk(chunk({"reasoning_content": "lock" * 120}))
            self.send_chunk(chunk({}, finish_reason="length"))
            self.send_chunk(b"data: [DONE]\n\n")
            time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError):
            pass


class LateLockTests(unittest.TestCase):
    def setUp(self):
        Stub.counts = {}
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
        upstream = harness.TrackedHTTPServer(("127.0.0.1", 0), Stub)
        self.upstream = upstream
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        harness.install_tracked_server(self, upstream, name="upstream-stub",
                                          require_drained=False)

        args = SimpleNamespace(
            base_url=f"http://127.0.0.1:{upstream.server_port}",
            token=STUB_TOKEN, profile="profile", login_timeout=1,
            use_api_key=False, no_browser=True)
        proxy = chat2api.Proxy({"token": STUB_TOKEN}, args)
        server = harness.TrackedHTTPServer(("127.0.0.1", 0), chat2api.Handler)
        server.proxy = proxy
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.server = server
        # Registered AFTER the redirect: this teardown (shutdown → bounded
        # drain of in-flight requests → close) runs BEFORE the USAGE_LOG
        # patch stop, so the post-response record_usage tail of the LAST
        # request lands in the temp ledger, never the production one.
        harness.install_tracked_server(self, server, name="chat2api-gateway")
        self.base = f"http://127.0.0.1:{server.server_port}"
        chat2api._reader_slots = threading.BoundedSemaphore(
            chat2api.STREAM_MAX_CONCURRENT_READERS)

    def request(self, model):
        r = requests.post(
            f"{self.base}/v1/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": "hi"}],
                  "stream": True},
            stream=True, timeout=10)
        payloads = []
        for raw in r.iter_lines(chunk_size=1):
            line = raw.decode("utf-8", "replace")
            if line.startswith("data: "):
                payloads.append(line[6:])
        return r, payloads

    def test_late_content_lock_warning_and_error_metadata(self):
        with self.assertLogs("chat2api", level="WARNING") as cm:
            _, payloads = self.request("late-content")
        warning = "\n".join(cm.output)
        self.assertIn("stage=degenerate_repetition", warning)
        self.assertIn("phase=postcommit", warning)
        self.assertIn("downstream_body_sent=True", warning)
        self.assertIn("content_seen=True", warning)
        self.assertIn("tool_action_seen=False", warning)
        errors = [json.loads(p)["error"] for p in payloads if '"error"' in p]
        self.assertTrue(errors)
        err = errors[-1]
        self.assertEqual(err["code"], "degenerate_repetition")
        self.assertIn("[chat2api:degenerate_repetition]", err["message"])
        self.assertTrue(err["detail"]["downstream_body_sent"])
        self.assertFalse(err["detail"]["tool_action_seen"])
        self.assertFalse(err["detail"]["transparent_retry_safe"])

    def test_late_tool_lock_marks_tool_action_seen(self):
        with self.assertLogs("chat2api", level="WARNING") as cm:
            self.request("late-tool")
        warning = "\n".join(cm.output)
        self.assertIn("stage=degenerate_repetition", warning)
        self.assertIn("tool_action_seen=True", warning)

    def test_repetition_retry_switch_is_respected_when_transport_retry_is_on(self):
        with mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRY", False), \
             mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True):
            with self.assertLogs("chat2api", level="INFO") as cm:
                self.request("early-lock")
        combined = "\n".join(cm.output)
        self.assertNotIn("repetition auto-retry", combined)
        self.assertIn("repetition_retries=0", combined)
        self.assertEqual(Stub.counts.get("early-lock"), 1)

    def test_guard_disabled_never_retries_repetition_via_transport_preflight(self):
        # CHAT2API_REPETITION_GUARD=0 means "no repetition detection or
        # retry at all": even with both retry switches on, the transport
        # preflight must not keep hidden repetition retries alive. The
        # buffered repetition passes through verbatim and uncensored, so the
        # stream completes normally with exactly one upstream attempt.
        with mock.patch.object(chat2api, "REPETITION_GUARD_ENABLED", False), \
             mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRY", True), \
             mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", True):
            with self.assertLogs("chat2api", level="INFO") as cm:
                r, payloads = self.request("early-lock")
        combined = "\n".join(cm.output)
        self.assertNotIn("repetition auto-retry", combined)
        self.assertNotIn("stage=degenerate_repetition", combined)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Stub.counts.get("early-lock"), 1)
        self.assertTrue(any(p == "[DONE]" for p in payloads))


if __name__ == "__main__":
    unittest.main()
