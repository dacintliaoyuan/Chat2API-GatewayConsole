# -*- coding: utf-8 -*-
"""Upstream 4xx request-rejection semantics and diagnostics.

2026-09-18 production incident: UM returned HTTP 400 ~270 times in one day
against three giant "接手" conversations (3.5-6.2 MB histories) while
healthy conversations ran fine. chat2api used to map EVERY upstream non-200
to a downstream 502, so ZCode kept replaying the same payload as a transient
failure and re-rejecting it forever. Contract now: upstream 400/422 surface
as their real status, never enter transient retry, and emit structure-only
diagnostics (counts/ids/role shape/shape hash) with an upstream error
message truncated - never prompt content, tool arguments/results or
credentials.
"""

import importlib.util
import json
import sys
import threading
import time
import unittest
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _chat2api_http_harness as harness  # noqa: E402

CHAT2API_PATH = Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py"
SPEC = importlib.util.spec_from_file_location(
    "chat2api_reject", CHAT2API_PATH)
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
    """Per-model scenarios for upstream 4xx behaviour and diagnostics."""

    protocol_version = "HTTP/1.1"

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

    def _sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.close_connection = True

    def _send(self, data: bytes):
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii")
                         + data + b"\r\n")
        self.wfile.flush()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        scenario = body.get("model", "")
        posts.append((scenario, self.headers.get("Authorization", ""),
                      "stream_options" in body))
        if scenario == "reject-400":
            self._send_json(400, {"error": {
                "message": "request validation failed",
                "type": "invalid_request_error", "code": "bad_payload"}})
        elif scenario == "reject-422":
            self._send_json(422, {"error": {
                "message": "unprocessable tool call sequence",
                "type": "invalid_request_error"}})
        elif scenario == "reject-400-long":
            # Longer than the old 300-char diagnostic capture: the decisive
            # tail (the exact prompt token count) used to be lost.
            self._send_json(400, {"detail":
                "This model's maximum context length is 524288 tokens. "
                "However, you requested 128000 output tokens, leaving only "
                "396288 tokens. Your prompt has 412345 tokens. Please reduce "
                "the prompt length or the maximum output tokens, or both."})
        elif scenario == "slow-504":
            self.send_response(504)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(b"timeout")))
            self.end_headers()
            self.wfile.write(b"timeout")
            self.close_connection = True
            time.sleep(30)  # the read itself costs the slow threshold
        elif scenario == "fast-503":
            self._send_json(503, {"error": {"message": "overloaded"}})
        else:
            self._send_json(200, {})


posts = []


class TestChat2ApiRequestRejection(unittest.TestCase):
    def setUp(self):
        posts.clear()
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

        for patch in (
            # Single-attempt semantics: these tests pin 4xx semantics, not
            # retry policy (which has its own dedicated tests).
            # Single-attempt semantics: these tests pin 4xx semantics, not
            # retry policy (which has its own dedicated tests).
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", False),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 0),
            mock.patch.object(chat2api, "UPSTREAM_SLOW_AUTO_RETRIES", 0),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRY", False),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRIES", 0),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        chat2api._reader_slots = threading.BoundedSemaphore(
            chat2api.STREAM_MAX_CONCURRENT_READERS)

    def chat_post(self, model, stream=True, timeout=20):
        return requests.post(
            f"{self.base}/v1/chat/completions",
            json={"model": model,
                  "messages": [{"role": "user", "content": "hi"}],
                  "stream": stream, "max_tokens": 16},
            timeout=timeout)

    # ---------- semantics ----------

    def test_upstream_400_not_transient_downstream_400(self):
        with self.assertLogs("chat2api", level="WARNING") as cm:
            response = self.chat_post("reject-400")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["type"],
                         "upstream_request_rejected")
        # compatibility fallback ran exactly once (two upstream posts), and
        # the failure was never retried as transient.
        self.assertEqual(sum(1 for s in posts if s[0] == "reject-400"), 2)
        self.assertFalse(any(s[0] == "reject-400" for s in posts[2:]))
        reject_lines = [l for l in cm.output if "upstream_reject" in l]
        self.assertEqual(len(reject_lines), 1)

    def test_upstream_422_surfaces_422(self):
        response = self.chat_post("reject-422")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(sum(1 for s in posts if s[0] == "reject-422"), 1)

    def test_long_reject_body_logs_complete_upstream_message(self):
        # >300-char 400 body: the whole message (with the exact prompt token
        # count) must survive capture + extraction, not stop at the old
        # 120-char raw prefix.
        with self.assertLogs("chat2api", level="WARNING") as cm:
            response = self.chat_post("reject-400-long")
        self.assertEqual(response.status_code, 400)
        line = "\n".join(l for l in cm.output if "upstream_reject" in l)
        self.assertIn("maximum context length is 524288", line)
        self.assertIn("Your prompt has 412345 tokens", line)

    # ---------- diagnostics content ----------

    def test_reject_diagnostics_structure_and_no_content_leak(self):
        body = {
            "model": "GLM-5.3-Flash", "stream": True,
            "messages": [
                {"role": "user", "content": "SECRET-PROMPT-TEXT"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_a", "type": "function",
                     "function": {"name": "Bash", "arguments": "SECRET-ARGS"}},
                    {"id": "call_a", "type": "function",
                     "function": {"name": "Bash", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call_b",
                 "content": "SECRET-TOOL-RESULT"},
                {"role": "assistant",
                 "content": [{"type": "text", "text": "part"}]},
            ],
            "tools": [{"type": "function",
                       "function": {"name": "Bash"}}],
        }
        diag = chat2api._upstream_reject_diagnostics(body)
        blob = json.dumps(diag, ensure_ascii=False)
        self.assertEqual(diag["message_count"], 4)
        self.assertEqual(diag["assistant_tool_calls"], 2)
        self.assertEqual(diag["tool_results"], 1)
        # Every call_a lacks a result (duplicated id counted per call).
        self.assertEqual(diag["missing_tool_results"], 2)
        self.assertEqual(diag["orphan_tool_results"], 1)    # call_b result
        self.assertEqual(diag["duplicate_tool_call_ids"], 1)
        self.assertEqual(diag["tools_count"], 1)
        self.assertIn("max_message_chars", diag)
        self.assertIn("payload_shape_hash", diag)
        self.assertNotIn("SECRET-PROMPT-TEXT", blob)
        self.assertNotIn("SECRET-TOOL-RESULT", blob)
        self.assertNotIn("SECRET-ARGUMENTS", blob)
        # Same structure with different bodies -> identical shape hash.
        other = json.loads(json.dumps(body))
        other["messages"][0]["content"] = "COMPLETELY DIFFERENT"
        self.assertEqual(
            diag["payload_shape_hash"],
            chat2api._upstream_reject_diagnostics(other)["payload_shape_hash"])
        # Structural change (a duplicated call id gains its result) -> the
        # missing/orphan structure diverges, so the shape hash must change.
        changed = json.loads(json.dumps(body))
        changed["messages"][2]["tool_call_id"] = "call_a"
        self.assertNotEqual(
            diag["payload_shape_hash"],
            chat2api._upstream_reject_diagnostics(changed)
            ["payload_shape_hash"])

    # ---------- transient policy unchanged ----------

    def test_upstream_503_keeps_fast_retry(self):
        # A transient upstream status must keep the existing behaviour; the
        # 400/422 branch above must not have swallowed it.
        self.assertIn(503, chat2api._UPSTREAM_RETRYABLE_STATUS)
        self.assertIn(504, chat2api._UPSTREAM_RETRYABLE_STATUS)
        self.assertNotIn(400, chat2api._UPSTREAM_RETRYABLE_STATUS)
        self.assertNotIn(422, chat2api._UPSTREAM_RETRYABLE_STATUS)


class TestTruncatedRejectBodyRepair(unittest.TestCase):
    """The diagnostic body capture is byte-capped, so an upstream 4xx body
    can be cut mid-string and fail json.loads. The extractor must re-close
    the prefix and still report the upstream's own words (e.g. the exact
    "your prompt has N tokens" count), falling back to the raw 120-char
    prefix only for bodies that are not JSON objects at all.
    """

    def test_vllm_style_truncated_detail_is_extracted(self):
        # The 2026-09-20 production capture, byte-for-byte: cut inside the
        # detail string, before the decisive "prompt has N tokens" tail.
        text = ('{"detail":"This model\'s maximum context length is 524288 '
                'tokens. However, you requested 128000 output tokens and '
                'your pr')
        message, err_type, err_code = \
            chat2api._extract_upstream_reject_error(text)
        self.assertEqual(
            message,
            "This model's maximum context length is 524288 tokens. However, "
            "you requested 128000 output tokens and your pr")
        self.assertNotIn('{"detail"', message)

    def test_cut_inside_string_with_escapes(self):
        text = r'{"detail":"value with \"quote\" and mor'
        message, _, _ = chat2api._extract_upstream_reject_error(text)
        self.assertEqual(message, 'value with "quote" and mor')

    def test_nested_error_object_cut_mid_string(self):
        # The cut lands inside the "type" literal: the repaired payload
        # honestly carries the partial value, never a guessed completion.
        text = ('{"error": {"message": "tool schema rejected at field '
                'name", "type": "invalid_request_')
        message, err_type, _ = chat2api._extract_upstream_reject_error(text)
        self.assertEqual(message, "tool schema rejected at field name")
        self.assertEqual(err_type, "invalid_request_")

    def test_non_object_or_garbage_keeps_raw_prefix_fallback(self):
        # Truncated JSON array: not an object prefix -> raw 120-char fallback.
        message, _, _ = chat2api._extract_upstream_reject_error(
            '[1, 2, {"a": "b"')
        self.assertEqual(message, '[1, 2, {"a": "b"')
        # Plain garbage keeps the same honest prefix behavior.
        message2, _, _ = chat2api._extract_upstream_reject_error(
            "no json here")
        self.assertEqual(message2, "no json here")

    def test_long_body_over_cap_still_reports_partial_detail(self):
        # A body longer than the diagnostic capture cap still yields the
        # partial message via the repair instead of an unparsed raw slice.
        text = '{"detail":"limit reached: ' + "x" * 3000
        message, _, _ = chat2api._extract_upstream_reject_error(text)
        self.assertTrue(message.startswith("limit reached: "))

    def test_complete_body_parse_unaffected(self):
        message, err_type, err_code = chat2api._extract_upstream_reject_error(
            json.dumps({"detail": "At most 16 image(s) may be provided."}))
        self.assertEqual(message, "At most 16 image(s) may be provided.")
        self.assertEqual(err_type, "")
        self.assertEqual(err_code, "")


if __name__ == "__main__":
    unittest.main()
