# -*- coding: utf-8 -*-
"""Repetition-loop guard: cut degenerate reasoning/content streams early.

Observed 2026-09-16..18 upstream behavior: a model run emits one short token
("lock") over and over as the ENTIRE reasoning stream, burning the full
65533-char output budget (~16k tokens) and hanging the turn for 5-10 minutes
before the budget ends it. The guard in chat2api watches the streamed
reasoning/content text and, once the accumulated tail is pure repetition over
REPETITION_GUARD_MIN_TAIL_CHARS, raises the standard in-band error so the
client sees an explicit failure instead of a silent hang.

These tests pin both directions: degenerate loops are cut (and tool-call
arguments never are), and legitimate repeated content below the bound streams
through untouched. The guard is disabled via
mock.patch.object(chat2api, "REPETITION_GUARD_ENABLED", False) for the
disable scenario. HTTP/SSE behaviour of normal streams is covered by
test_chat2api_stream.py.
"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import time
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
    "chat2api_repetition_guard", CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)

STUB_TOKEN = "stub-token"


def _chunk(delta: dict, finish_reason=None) -> bytes:
    payload = json.dumps({"choices": [{"delta": delta,
                                       "finish_reason": finish_reason}]},
                         ensure_ascii=False)
    return f"data: {payload}\n\n".encode("utf-8")


class StubUpstreamHandler(BaseHTTPRequestHandler):
    """Per-model scenarios; loop-dump streams far exceed the guard bound."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, data: bytes):
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii")
                         + data + b"\r\n")
        self.wfile.flush()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        scenario = body.get("model", "")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.close_connection = True
        try:
            if scenario == "degenerate-loop":
                # 20 chunks x 480 chars of pure "lock" repeats, then a budget
                # cut (finish_reason=length) exactly like the real degenerate
                # streams: the guard must cut around the 1200-char tail, long
                # before all 20 chunks, and disabled-mode must run to [DONE].
                for _ in range(20):
                    self._send(_chunk({"reasoning_content": "lock" * 120}))
                self._send(_chunk({}, finish_reason="length"))
                self._send(b"data: [DONE]\n\n")
                time.sleep(1.0)
            elif scenario == "repeat-below-threshold":
                # 8 chunks x 100 chars = 800 chars of repeats: below the
                # 1200-char bound, so the stream must complete untouched.
                for _ in range(8):
                    self._send(_chunk({"reasoning_content": "lock" * 25}))
                self._send(_chunk({"content": "done"},
                                  finish_reason="stop"))
                self._send(b"data: [DONE]\n\n")
                time.sleep(0.5)
            elif scenario == "tool-args-repeat":
                # Repeats inside tool-call arguments must NOT trip the guard.
                args = json.dumps({"cmd": "lock" * 500})
                self._send(_chunk({"tool_calls": [
                    {"id": "t1", "function": {"name": "Bash",
                                              "arguments": args}}]}))
                self._send(_chunk({}, finish_reason="tool_calls"))
                self._send(b"data: [DONE]\n\n")
                time.sleep(0.5)
            elif scenario == "loop-large-deltas":
                # Two consecutive large deltas (>1200 chars each), the same
                # loop signature: must be cut after the second one.
                for _ in range(2):
                    self._send(_chunk({"reasoning_content": "lock" * 325}))
                self._send(b"data: [DONE]\n\n")
                time.sleep(0.5)
            elif scenario == "two-distinct-large-deltas":
                # Two large deltas with different content: distinct
                # signatures, must stream through untouched (200 KiB-class
                # forwarded-intact contract at guard-relevant scale).
                self._send(_chunk({"reasoning_content": "x" * 1300}))
                self._send(_chunk({"reasoning_content": "y" * 1300}))
                self._send(_chunk({"content": "ok"}, finish_reason="stop"))
                self._send(b"data: [DONE]\n\n")
                time.sleep(0.5)
            elif scenario == "multi-line-event":
                # One event split across two data: lines; per SSE rules the
                # data fields join with \n into a single valid JSON payload.
                self._send(b'data: {"choices":[\n'
                           b'data:  {"delta":{"content":"multi"}}]}\n\n')
                self._send(_chunk({}, finish_reason="stop"))
                self._send(b"data: [DONE]\n\n")
                time.sleep(0.5)
            elif scenario == "nonstream-bad-json":
                # Non-stream path: upstream 200 with a non-JSON body must
                # surface as a clean 502, never a handler traceback.
                payload = b"this is not json"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                self.close_connection = True
            else:
                self._send(_chunk({"content": "ok"}, finish_reason="stop"))
                self._send(b"data: [DONE]\n\n")
                time.sleep(0.3)
        except (BrokenPipeError, ConnectionResetError):
            pass


class TestChat2ApiRepetitionGuard(unittest.TestCase):
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

        args = SimpleNamespace(
            base_url=f"http://127.0.0.1:{stub.server_port}",
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
        chat2api._reader_slots = threading.BoundedSemaphore(
            chat2api.STREAM_MAX_CONCURRENT_READERS)

    def stream_request(self, model, timeout=15):
        return requests.post(
            f"{self.base}/v1/chat/completions",
            json={"model": model,
                  "messages": [{"role": "user", "content": "hi"}],
                  "stream": True, "max_tokens": 16},
            stream=True, timeout=timeout)

    @staticmethod
    def data_payloads(response):
        """All SSE data payloads after the 200, as decoded strings."""
        payloads = []
        for raw in response.iter_lines(chunk_size=1):
            line = raw.decode("utf-8", "replace")
            if line.startswith("data: "):
                payloads.append(line[6:])
        return payloads

    # ---------- degenerate loop is cut ----------

    def test_degenerate_loop_cut_with_in_band_error(self):
        with self.assertLogs("chat2api", level="INFO") as cm:
            response = self.stream_request("degenerate-loop")
            payloads = self.data_payloads(response)
        # The stream ends early: fewer data events than the stub emitted, the
        # last in-band payload is the repetition error, and no [DONE] arrives.
        self.assertLess(len(payloads), 20)
        self.assertTrue(any('"error"' in p for p in payloads))
        err = [p for p in payloads if '"error"' in p][-1]
        self.assertIn("repetition loop", err)
        self.assertNotIn("[DONE]", payloads)
        # The usage ledger records the degenerate end explicitly.
        end_lines = [l for l in cm.output if "stream end" in l]
        self.assertTrue(any("status=degenerate_repetition" in l
                            for l in end_lines))

    def test_guard_disabled_passes_loop_through(self):
        with mock.patch.object(chat2api, "REPETITION_GUARD_ENABLED", False):
            response = self.stream_request("degenerate-loop", timeout=20)
            payloads = self.data_payloads(response)
        # Disabled: the whole stream (20 chunks + finish_reason + [DONE])
        # passes through untouched, no in-band repetition error.
        self.assertFalse(any('"error"' in p for p in payloads))
        self.assertTrue(any("[DONE]" == p for p in payloads))

    # ---------- legitimate repeats are not cut ----------

    def test_repeats_below_threshold_stream_normally(self):
        response = self.stream_request("repeat-below-threshold")
        payloads = self.data_payloads(response)
        self.assertFalse(any('"error"' in p for p in payloads))
        self.assertTrue(any("[DONE]" == p for p in payloads))
        joined = "".join(payloads)
        self.assertIn("done", joined)

    def test_tool_argument_repeats_not_guarded(self):
        response = self.stream_request("tool-args-repeat")
        payloads = self.data_payloads(response)
        self.assertFalse(any('"error"' in p for p in payloads))
        self.assertTrue(any("[DONE]" == p for p in payloads))

    # ---------- pure predicate ----------

    def test_guard_predicate_boundaries(self):
        hit = chat2api._repetition_guard_hit
        self.assertTrue(hit("lock" * 500))                 # phase-aligned loop
        self.assertTrue(hit("x" + "lock" * 400))           # phase-shifted tail
        self.assertTrue(hit("实现" * 650))                  # CJK 2-char pattern
        self.assertTrue(hit("prefix thought. " + "ab" * 700))  # tail-only loop
        self.assertFalse(hit("x" * 11 + "lock" * 297))     # 1199 chars: below
        self.assertFalse(hit("这是一段正常的思考。" * 50))
        self.assertFalse(hit("for i in range(10):\n    print(lock)\n" * 30))

    # ---------- review-round counterexamples ----------

    def test_multi_line_sse_event_is_merged(self):
        # One SSE event split across two data: lines: per the SSE spec the
        # data fields join with \n before JSON parsing, so the split event
        # must be parsed and forwarded as one completion chunk, not dropped.
        response = self.stream_request("multi-line-event")
        payloads = self.data_payloads(response)
        self.assertFalse(any('"error"' in p for p in payloads))
        joined = "".join(payloads)
        self.assertIn("multi", joined)
        self.assertTrue(any("[DONE]" == p for p in payloads))

    def test_consecutive_large_loop_deltas_are_cut(self):
        # Two consecutive large deltas (>1200 chars each) sharing the same
        # loop signature: the old big-delta exemption never fired here; the
        # signature streak must cut after the second one.
        response = self.stream_request("loop-large-deltas")
        payloads = self.data_payloads(response)
        self.assertTrue(any('"error"' in p for p in payloads))
        err = [p for p in payloads if '"error"' in p][-1]
        self.assertIn("repetition loop", err)

    def test_distinct_large_deltas_pass(self):
        # Two LARGE deltas with DIFFERENT content (200 KiB-class contract):
        # distinct signatures must never trigger, stream runs to [DONE].
        response = self.stream_request("two-distinct-large-deltas")
        payloads = self.data_payloads(response)
        self.assertFalse(any('"error"' in p for p in payloads))
        self.assertTrue(any("[DONE]" == p for p in payloads))

    def test_usage_estimates_reasoning_chars_on_cut(self):
        with self.assertLogs("chat2api", level="INFO"):
            response = self.stream_request("degenerate-loop")
            self.data_payloads(response)
        usage_lines = [json.loads(l) for l in
                       self.usage_log.read_text(encoding="utf-8").splitlines()
                       if l.strip()]
        degenerate = [u for u in usage_lines
                      if u.get("status") == "degenerate_repetition"]
        self.assertTrue(degenerate)
        # The cut happened mid reasoning stream: the local output estimate
        # (output_tokens = estimate over completion+reasoning chars) must
        # count the streamed reasoning, not record zero.
        self.assertTrue(any((u.get("output_tokens") or 0) > 0
                            for u in degenerate))

    def test_non_object_request_body_returns_400(self):
        response = requests.post(
            f"{self.base}/v1/chat/completions", data="[]",
            headers={"Content-Type": "application/json"}, timeout=10)
        self.assertEqual(response.status_code, 400)
        self.assertIn("JSON object", response.text)

    def test_non_stream_upstream_200_invalid_json_returns_502(self):
        response = requests.post(
            f"{self.base}/v1/chat/completions",
            json={"model": "nonstream-bad-json",
                  "messages": [{"role": "user", "content": "hi"}],
                  "stream": False},
            timeout=10)
        self.assertEqual(response.status_code, 502)

    def test_import_does_not_create_service_log(self):
        # A fresh import in a clean cwd must not open/create the service log;
        # configure_logging() owns that side effect (test-pollution fix).
        code = (
            "import importlib.util, os, sys\n"
            "os.environ['CHAT2API_CONTROL_TOKEN_FILE'] = os.path.join("
            "os.environ['TEMP'], 'unused-token')\n"
            "spec = importlib.util.spec_from_file_location('c2g', "
            "'D:/git/github/personal_files/SelfUseAI/ai-gateway"
            "/chat2api/chat2api.py')\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
            "print('created', os.path.exists('chat2api.log'))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, timeout=60,
            cwd=tempfile.mkdtemp())
        self.assertEqual(proc.returncode, 0)
        self.assertIn(b"created False", proc.stdout)


if __name__ == "__main__":
    unittest.main()
