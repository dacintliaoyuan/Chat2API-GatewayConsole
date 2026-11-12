# -*- coding: utf-8 -*-
"""Long-unit line-loop observation contract (observe only, never cut).

A thinking channel can cycle whole lines ("Hmm — OK." / "Hmm — hmm.") whose
period (~28 chars) exceeds the 1-8 char window of the cutting repetition
signature, so the guard is structurally blind to that loop. The line-cycle
detector ``_line_cycle_repetition`` plus ``_LineLoopObserver`` add production
evidence only: one WARNING per distinct cycle signature and a
``line_loop_hits`` count in the request summary. The stream itself must
complete untouched: no cut, no in-band error frame, no hidden retry, and
exactly one upstream attempt.
"""

import importlib.util
import json
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
    "chat2api_line_loop", CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)

STUB_TOKEN = "stub"

# The documented degenerate thinking cycle: the repeating unit is ~28 chars
# with internal alternation, so the 1-8 char cutting signature can never
# match it and only the line-cycle observer can see it.
CYCLE = "Hmm — OK.\n\nHmm — hmm.\n\nOK.\n\n"
# Seven distinct lines: the period exceeds LINE_LOOP_MAX_CYCLE_LINES (6).
WIDE_CYCLE = "".join(f"line-{i} content here\n" for i in range(7))


def reasoning_chunk(text):
    payload = json.dumps({"choices": [{"delta": {"reasoning_content": text},
                                        "finish_reason": None}]})
    return f"data: {payload}\n\n".encode()


def finish_chunk():
    payload = json.dumps({"choices": [{"delta": {},
                                       "finish_reason": "length"}]})
    return f"data: {payload}\n\n".encode()


def thinking_chunk(text):
    payload = json.dumps({"type": "content_block_delta",
                          "delta": {"type": "thinking_delta",
                                    "thinking": text}})
    return f"event: content_block_delta\ndata: {payload}\n\n".encode()


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
            if model == "line-loop":
                for _ in range(50):
                    self.send_chunk(reasoning_chunk(CYCLE))
                self.send_chunk(finish_chunk())
                self.send_chunk(b"data: [DONE]\n\n")
            elif model == "plain":
                for i in range(40):
                    self.send_chunk(reasoning_chunk(
                        f"Reasoning step {i}: checking item {i}.\n"))
                self.send_chunk(finish_chunk())
                self.send_chunk(b"data: [DONE]\n\n")
            elif model == "anthropic-line-loop":
                for _ in range(50):
                    self.send_chunk(thinking_chunk(CYCLE))
                self.send_chunk(
                    b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


class LineCycleDetectorTests(unittest.TestCase):
    def test_detects_hmm_line_cycle(self):
        observed = chat2api._line_cycle_repetition(CYCLE * 40)
        self.assertIsNotNone(observed)
        signature, repeats, cycle_lines, tail_chars = observed
        self.assertEqual(cycle_lines, 6)
        self.assertGreaterEqual(repeats, 38)
        self.assertGreaterEqual(tail_chars, 600)

    def test_signature_is_phase_stable(self):
        # A rolling window that cuts a cycle in half must not change the
        # canonical signature, or an evolving loop would log repeatedly.
        whole = chat2api._line_cycle_repetition(CYCLE * 40)
        shifted = chat2api._line_cycle_repetition("xx tail" + CYCLE * 40)
        self.assertIsNotNone(whole)
        self.assertIsNotNone(shifted)
        self.assertEqual(whole[0], shifted[0])

    def test_below_tail_gate_returns_none(self):
        self.assertIsNone(chat2api._line_cycle_repetition(CYCLE * 15))

    def test_short_period_pure_loop_is_not_a_line_cycle(self):
        # Pure short-period repetition belongs to the cutting signature; a
        # run with no newlines can never form a line cycle here.
        self.assertIsNone(chat2api._line_cycle_repetition("lock" * 300))

    def test_period_over_six_lines_returns_none(self):
        self.assertIsNone(chat2api._line_cycle_repetition(WIDE_CYCLE * 40))

    def test_overlong_line_in_cycle_returns_none(self):
        cycle = ("x" * 200 + "\n\n") * 20
        self.assertIsNone(chat2api._line_cycle_repetition(cycle))

    def test_normal_prose_returns_none(self):
        prose = "".join(
            f"Reasoning step {i}: checking item {i} in detail.\n"
            for i in range(40))
        self.assertIsNone(chat2api._line_cycle_repetition(prose))


class StubServerTests(unittest.TestCase):
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
        server = harness.TrackedHTTPServer(
            ("127.0.0.1", 0), chat2api.Handler)
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

    def request(self, path, model):
        r = requests.post(
            f"{self.base}{path}",
            json={"model": model,
                  "messages": [{"role": "user", "content": "hi"}],
                  "stream": True},
            stream=True, timeout=10)
        payloads = []
        for raw in r.iter_lines(chunk_size=1):
            line = raw.decode("utf-8", "replace")
            if line.startswith("data: "):
                payloads.append(line[6:])
        return r, payloads

    def test_chat_completions_line_loop_observed_not_cut(self):
        with self.assertLogs("chat2api", level="INFO") as cm:
            r, payloads = self.request("/v1/chat/completions", "line-loop")
        combined = "\n".join(cm.output)
        self.assertIn("line repetition pattern", combined)
        self.assertIn("site=chat_completions", combined)
        self.assertIn("cycle_lines=6", combined)
        # Observe only: the stream completes normally, no in-band error
        # frame, no repetition retry, exactly one upstream attempt.
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any(p == "[DONE]" for p in payloads))
        self.assertNotIn("degenerate_repetition", combined)
        self.assertNotIn("repetition auto-retry", combined)
        self.assertEqual(Stub.counts.get("line-loop"), 1)
        self.assertTrue(any("line_loop_hits=1" in line for line in cm.output))

    def test_plain_reasoning_summary_counts_zero_hits(self):
        with self.assertLogs("chat2api", level="INFO") as cm:
            self.request("/v1/chat/completions", "plain")
        self.assertTrue(any("line_loop_hits=0" in line for line in cm.output))
        self.assertNotIn("line repetition pattern", "\n".join(cm.output))

    def test_anthropic_passthrough_line_loop_observed_not_cut(self):
        with self.assertLogs("chat2api", level="INFO") as cm:
            r, payloads = self.request("/v1/messages", "anthropic-line-loop")
        combined = "\n".join(cm.output)
        self.assertIn("line repetition pattern", combined)
        self.assertIn("site=anthropic_passthrough", combined)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("degenerate_repetition", combined)
        self.assertEqual(Stub.counts.get("anthropic-line-loop"), 1)


if __name__ == "__main__":
    unittest.main()
