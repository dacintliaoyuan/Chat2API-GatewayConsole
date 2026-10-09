# -*- coding: utf-8 -*-
"""Output-budget forwarding proof (Plan-Revision 7 step 7 / B7-7).

The 2026-09 upstream 400 for DeepSeek-V4.1-Flash reported "input + reserved
output 384000 > context" while the gateway's diagnostics only listed request
top-level keys, never values - so the forwarded output cap was undecidable
from the logs. Contract now:

  1. The gateway forwards the client's max_tokens / max_completion_tokens
     UNCHANGED (it injects and rewrites nothing) - proven by a fake upstream
     that captures the exact JSON body put on the wire.
  2. Every chat/Messages request logs one "output budget" line stating which
     cap was forwarded, its presence/legality and the cap source - controlled
     values only: never message content, tool results or credentials.
  3. An upstream 400/422 reject line repeats those facts inline, so the next
     "input + output > context" incident is decidable from the log alone.

Isolation: the upstream is an in-process stub on 127.0.0.1 (the ONLY
endpoint the gateway can reach - no real model request is possible), and the
usage ledger is redirected to a private temp file via
tests/_chat2api_http_harness.py (B8-0: tracked servers drain every in-flight
handler BEFORE the redirect is released - the 2026-10-02 production-ledger
pollution incident is the reason this suite no longer orders its cleanups by
hand). cap_source never claims a catalog/sync/config value: with no legal
cap field it is "unknown".
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
    "chat2api_output_budget", CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)

LOCAL_KEY = "local-secret-output-budget"
STUB_TOKEN = "stub-token"
SECRET_PROMPT = "SECRET-PROMPT-CONTENT-7f3a"
SECRET_REPLY = "SECRET-UPSTREAM-REPLY"


class StubUpstreamHandler(BaseHTTPRequestHandler):
    """Fake Open WebUI surface; records the chat/Messages bodies it received."""

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

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        if path == "/api/chat/completions":
            captured.append(body)
            if body.get("model") == "reject-400":
                self._send_json(400, {"error": {
                    "message": "This model's maximum context length is "
                               "1048576 tokens. However, you requested 384000 "
                               "output tokens, leaving only 664576 tokens. "
                               "Your prompt has 664577 tokens.",
                    "type": "invalid_request_error",
                    "code": "context_length_exceeded"}})
                return
            self._send_json(200, {"choices": [{"message": {
                "role": "assistant", "content": SECRET_REPLY}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2}})
        elif path == "/api/v1/messages":
            captured.append(body)
            if body.get("model") == "reject-400":
                self._send_json(400, {"type": "error", "error": {
                    "type": "invalid_request_error",
                    "message": "max_tokens: Input should be less than or "
                               "equal to 64000\nFAKE-SECOND-LOG-LINE"}})
                return
            self._send_json(200, {"id": "msg_1", "type": "message",
                                  "role": "assistant",
                                  "content": [{"type": "text",
                                               "text": SECRET_REPLY}],
                                  "usage": {"input_tokens": 1,
                                            "output_tokens": 1}})
        else:
            self._send_json(404, {"detail": "not found"})


captured = []


class TestChat2ApiOutputBudgetForwarding(unittest.TestCase):
    def setUp(self):
        captured.clear()
        # B8-0 isolation lifecycle (see _chat2api_http_harness): the ledger
        # redirect is registered FIRST so it is released LAST - after every
        # handler drained and every server closed. The pre-fix LIFO bug
        # released the patch while a post-response handler was still writing
        # record_usage, which is how test rows reached the PRODUCTION
        # data/usage-chat2api.jsonl on 2026-10-02.
        self.tmp_dir, self.usage_log = harness.install_usage_isolation(
            self, chat2api)
        harness.assert_stub_credentials(
            chat2api, local_key=LOCAL_KEY, upstream_token=STUB_TOKEN,
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
        server.local_api_key = LOCAL_KEY
        server.require_local_api_key = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        # Registered AFTER the redirect: this teardown (shutdown → bounded
        # drain of in-flight handlers → close) runs BEFORE the USAGE_LOG
        # patch stop, so the post-response record_usage tail of the LAST
        # request lands in the temp ledger, never the production one.
        harness.install_tracked_server(self, server, name="chat2api-gateway")
        self.base = f"http://127.0.0.1:{server.server_port}"
        self.auth = {"Authorization": f"Bearer {LOCAL_KEY}"}

        for patch in (
            # Pin 4xx/forwarding semantics, not retry policy.
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

    def chat_post(self, extra, stream=False):
        payload = {"model": "Budget-Model-A",
                   "messages": [{"role": "user", "content": SECRET_PROMPT}],
                   "stream": stream}
        payload.update(extra)
        return requests.post(f"{self.base}/v1/chat/completions",
                             json=payload, headers=self.auth, timeout=15)

    def budget_lines(self, records):
        return [line for line in records if "output budget" in line]

    def wait_for_summary(self, records, timeout=5.0):
        """Wait for the request-summary line to reach the log capture.

        MUST be called INSIDE the ``assertLogs`` block: the non-stream
        Messages summary is logged after the response body is written back,
        so a poll started after the block exits can never see it (the
        capture handler is already detached and the late record is dropped —
        observed as an intermittent 0 != 1 failure). Poll with a bounded
        deadline instead of sleeping a fixed time; when the line never
        arrives the caller's assertion still fails honestly.
        """
        started = time.time()
        deadline = started + timeout
        while True:
            summaries = [line for line in records if "request summary" in line]
            if summaries or time.time() >= deadline:
                return summaries
            time.sleep(0.02)

    # ---------- forwarding equals the client values ----------

    def test_forwarded_max_tokens_equals_client_value(self):
        with self.assertLogs("chat2api", level="INFO") as cm:
            response = self.chat_post({"max_tokens": 1234})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(captured), 1)
        # The wire body carries the client's own cap, not a gateway value.
        self.assertEqual(captured[0]["max_tokens"], 1234)
        self.assertNotIn("max_completion_tokens", captured[0])
        line = "\n".join(self.budget_lines(cm.output))
        self.assertIn("max_tokens=1234", line)
        self.assertIn("max_tokens_present=True", line)
        self.assertIn("max_tokens_valid=True", line)
        self.assertIn("max_completion_tokens_present=False", line)
        self.assertIn("output_cap_effective=1234", line)
        self.assertIn("cap_source=client_request", line)
        self.assertIn("protocol=chat", line)

    def test_oversized_legal_int_forwarded_but_absent_from_log(self):
        # HTTP-level oversized proof: 10**16 is legal (>0, a real int) and is
        # forwarded verbatim, but its digits stay out of every log line
        # (kind=oversized_int); presence and validity remain true and the
        # source stays client_request.
        with self.assertLogs("chat2api", level="INFO") as cm:
            response = self.chat_post({"max_tokens": 10 ** 16})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["max_tokens"], 10 ** 16)
        line = "\n".join(self.budget_lines(cm.output))
        self.assertIn("max_tokens_present=True", line)
        self.assertIn("max_tokens_valid=True", line)
        self.assertIn("max_tokens_kind=oversized_int", line)
        self.assertIn(" max_tokens=- ", line)
        self.assertIn("output_cap_effective=-", line)
        self.assertIn("cap_source=client_request", line)
        blob = "\n".join(cm.output)
        self.assertNotIn(str(10 ** 16), blob)

    def test_both_fields_forwarded_unchanged_completion_tokens_wins(self):
        with self.assertLogs("chat2api", level="INFO") as cm:
            response = self.chat_post({"max_tokens": 111,
                                       "max_completion_tokens": 222})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(captured), 1)
        # Neither field may be dropped or rewritten: OpenAI precedence says
        # max_completion_tokens supersedes, and the upstream sees BOTH.
        self.assertEqual(captured[0]["max_tokens"], 111)
        self.assertEqual(captured[0]["max_completion_tokens"], 222)
        line = "\n".join(self.budget_lines(cm.output))
        self.assertIn("max_tokens=111", line)
        self.assertIn("max_completion_tokens=222", line)
        self.assertIn("output_cap_effective=222", line)
        self.assertIn("cap_source=client_request", line)

    def test_absent_fields_report_unknown_source_not_config_guess(self):
        with self.assertLogs("chat2api", level="INFO") as cm:
            response = self.chat_post({})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(captured), 1)
        self.assertNotIn("max_tokens", captured[0])
        self.assertNotIn("max_completion_tokens", captured[0])
        line = "\n".join(self.budget_lines(cm.output))
        self.assertIn("max_tokens_present=False", line)
        self.assertIn("max_completion_tokens_present=False", line)
        self.assertIn("output_cap_effective=-", line)
        self.assertIn("cap_source=unknown", line)
        self.assertNotIn("cap_source=client_request", line)
        self.assertNotIn("cap_source=catalog_sync_declaration", line)

    def test_illegal_values_forwarded_unchanged_and_flagged(self):
        with self.assertLogs("chat2api", level="INFO") as cm:
            response = self.chat_post({"max_tokens": "4096",
                                       "max_completion_tokens": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(captured), 1)
        # The gateway never "fixes" a client value: what went out is exactly
        # what came in, and the log flags the legality instead.
        self.assertEqual(captured[0]["max_tokens"], "4096")
        self.assertIs(captured[0]["max_completion_tokens"], True)
        line = "\n".join(self.budget_lines(cm.output))
        self.assertIn("max_tokens_present=True", line)
        self.assertIn("max_tokens_valid=False", line)
        self.assertIn("max_tokens_kind=str", line)
        self.assertIn("max_completion_tokens_valid=False", line)
        self.assertIn("max_completion_tokens_kind=bool", line)
        # No legal cap field: the effective cap is the upstream's own default
        # and the source is unknown - never a static-config guess.
        self.assertIn("output_cap_effective=-", line)
        self.assertIn("cap_source=unknown", line)

    def test_virtual_model_level_forwarded_and_diagnosed(self):
        self._saved_effort_models = chat2api.EFFORT_MODELS
        self.addCleanup(setattr, chat2api, "EFFORT_MODELS",
                        self._saved_effort_models)
        chat2api.EFFORT_MODELS = ["Budget-Model-A"]
        with self.assertLogs("chat2api", level="INFO") as cm:
            response = self.chat_post({"model": "Budget-Model-A-High",
                                       "max_tokens": 64})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(captured), 1)
        # The virtual model was resolved on the wire: base model + effort.
        self.assertEqual(captured[0]["model"], "Budget-Model-A")
        self.assertEqual(captured[0]["reasoning_effort"], "high")
        line = "\n".join(self.budget_lines(cm.output))
        self.assertIn("model=Budget-Model-A", line)
        self.assertIn("level=high", line)
        self.assertIn("max_tokens=64", line)
        self.assertIn("cap_source=client_request", line)

    # ---------- diagnostics content hygiene ----------

    def test_budget_diagnostics_carry_no_content_or_credentials(self):
        with self.assertLogs("chat2api", level="INFO") as cm:
            response = self.chat_post(
                {"max_tokens": 64,
                 "tools": [{"type": "function", "function": {
                     "name": "SECRET-TOOL-NAME",
                     "parameters": {"x": "SECRET-TOOL-SCHEMA"}}}]})
        self.assertEqual(response.status_code, 200)
        lines = self.budget_lines(cm.output)
        self.assertTrue(lines)
        blob = "\n".join(lines)
        for secret in (SECRET_PROMPT, SECRET_REPLY, "SECRET-TOOL-NAME",
                       "SECRET-TOOL-SCHEMA", LOCAL_KEY, "stub-token"):
            self.assertNotIn(secret, blob)
        self.assertIn("model=Budget-Model-A", blob)
        self.assertIn("max_tokens=64", blob)

    # ---------- reject-line correlation (the B7-7 incident shape) ----------

    def test_upstream_400_reject_line_carries_forwarded_budget(self):
        with self.assertLogs("chat2api", level="WARNING") as cm:
            payload = {"model": "reject-400",
                       "messages": [{"role": "user",
                                     "content": SECRET_PROMPT}],
                       "stream": False, "max_tokens": 384000}
            response = requests.post(f"{self.base}/v1/chat/completions",
                                     json=payload, headers=self.auth,
                                     timeout=15)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["type"],
                         "upstream_request_rejected")
        reject_lines = [line for line in cm.output
                        if "upstream_reject" in line]
        self.assertEqual(len(reject_lines), 1)
        line = reject_lines[0]
        # The rejected request's own forwarded facts are inline: the exact
        # cap the upstream saw, so "384000" is decidable from the log.
        self.assertIn("max_tokens_present=True", line)
        self.assertIn("max_tokens=384000", line)
        self.assertIn("cap_source=client_request", line)
        self.assertIn("output_cap_effective=384000", line)
        self.assertNotIn(SECRET_PROMPT, line)

    def test_stream_forwarded_budget_equals_reject_line(self):
        # Line-level equivalence proof over the REAL forwarding boundary:
        # the stub captures the exact bodies put on the wire, and the 400
        # reject line must quote the SAME value the upstream received. A
        # stream 400 triggers the pre-existing stream_options compatibility
        # retry (the option is popped and the request re-sent once), so the
        # cap value must survive BOTH wire sends unchanged.
        with self.assertLogs("chat2api", level="INFO") as cm:
            payload = {"model": "reject-400",
                       "messages": [{"role": "user",
                                     "content": SECRET_PROMPT}],
                       "stream": True, "max_tokens": 777}
            response = requests.post(f"{self.base}/v1/chat/completions",
                                     json=payload, headers=self.auth,
                                     timeout=15)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(captured), 2)
        for wire in captured:
            # The wire bodies carry the client's stream flag and cap
            # unchanged, including after the stream_options pop.
            self.assertIs(wire["stream"], True)
            self.assertEqual(wire["max_tokens"], 777)
        self.assertIn("stream_options", captured[0])
        self.assertNotIn("stream_options", captured[1])
        reject_lines = [line for line in cm.output
                        if "upstream_reject" in line]
        self.assertEqual(len(reject_lines), 1)
        line = reject_lines[0]
        # The reject line reuses the captured wire value: logged cap ==
        # forwarded cap == client cap, no recomputation in between.
        self.assertIn("stream=stream", line)
        self.assertIn("max_tokens=777", line)
        self.assertIn("output_cap_effective=777", line)
        self.assertIn("cap_source=client_request", line)
        self.assertNotIn(SECRET_PROMPT, line)

    def test_virtual_model_400_reject_line_splits_requested_and_forwarded(self):
        # Virtual model + upstream 400: the reject line must distinguish the
        # two model names. forwarded_model= is the RESOLVED base actually
        # forwarded upstream; requested_model= is the virtual name the
        # client asked for. The output_budget segment keeps the forwarded
        # (resolved) model only, so correlating forwarded_model= with the
        # wire can never read the virtual name instead.
        self._saved_effort_models = chat2api.EFFORT_MODELS
        self.addCleanup(setattr, chat2api, "EFFORT_MODELS",
                        self._saved_effort_models)
        chat2api.EFFORT_MODELS = ["reject-400"]
        with self.assertLogs("chat2api", level="WARNING") as cm:
            payload = {"model": "reject-400-High",
                       "messages": [{"role": "user",
                                     "content": SECRET_PROMPT}],
                       "stream": False, "max_tokens": 384000}
            response = requests.post(f"{self.base}/v1/chat/completions",
                                     json=payload, headers=self.auth,
                                     timeout=15)
        self.assertEqual(response.status_code, 400)
        # The wire carried the resolved base, not the virtual name.
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["model"], "reject-400")
        self.assertEqual(captured[0]["reasoning_effort"], "high")
        reject_lines = [line for line in cm.output
                        if "upstream_reject" in line]
        self.assertEqual(len(reject_lines), 1)
        line = reject_lines[0]
        self.assertIn(
            "forwarded_model=reject-400 "
            "requested_model=reject-400-High", line)
        # The output_budget segment repeats the forwarded facts only: its
        # model= field stays the resolved base and carries no
        # requested_model field.
        budget = line.rsplit(" output_budget ", 1)[1]
        self.assertTrue(budget.startswith("model=reject-400 level=high"))
        self.assertNotIn("requested_model", budget)
        self.assertNotIn(SECRET_PROMPT, line)

    # ---------- Anthropic Messages protocol ----------

    def test_anthropic_messages_budget_logged_with_protocol(self):
        with self.assertLogs("chat2api", level="INFO") as cm:
            response = requests.post(
                f"{self.base}/v1/messages",
                json={"model": "claude-x", "max_tokens": 256, "stream": False,
                      "messages": [{"role": "user",
                                    "content": SECRET_PROMPT}]},
                headers=self.auth, timeout=15)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["max_tokens"], 256)
        lines = self.budget_lines(cm.output)
        self.assertTrue(lines)
        blob = "\n".join(lines)
        self.assertIn("protocol=messages", blob)
        self.assertIn("max_tokens=256", blob)
        self.assertIn("cap_source=client_request", blob)
        self.assertNotIn(SECRET_PROMPT, blob)
        self.assertNotIn(SECRET_REPLY, blob)

    def test_messages_400_reject_line_carries_output_budget(self):
        with self.assertLogs("chat2api", level="INFO") as cm:
            response = requests.post(
                f"{self.base}/v1/messages",
                json={"model": "reject-400", "max_tokens": 64000,
                      "stream": False,
                      "messages": [{"role": "user",
                                    "content": SECRET_PROMPT}]},
                headers=self.auth, timeout=15)
            # The summary is logged after the response body, so it is awaited
            # INSIDE the capture window (see wait_for_summary).
            summaries = self.wait_for_summary(cm.output)
        self.assertEqual(response.status_code, 400)
        # The forwarded Anthropic body carries the client's own cap.
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["max_tokens"], 64000)
        reject_lines = [line for line in cm.output
                        if "upstream_reject" in line]
        self.assertEqual(len(reject_lines), 1)
        line = reject_lines[0]
        # Same inline output-budget review as the chat path: the exact cap
        # the upstream saw is decidable from the reject line alone. Messages
        # has no virtual-model resolution, so forwarded_model= and
        # requested_model= carry the SAME value. The rename also keeps a
        # bare "model=" grep's FIRST match on the forwarded value (the
        # field name "forwarded_model=" ends with "model="), never on
        # the requested one; a word-boundary "model=" grep no longer
        # matches this line at all.
        self.assertIn("status=400", line)
        self.assertIn(
            "forwarded_model=reject-400 "
            "requested_model=reject-400", line)
        self.assertIn("stream=nonstream", line)
        self.assertIn("max_tokens_present=True", line)
        self.assertIn("max_tokens=64000", line)
        self.assertIn("output_cap_effective=64000", line)
        self.assertIn("cap_source=client_request", line)
        self.assertIn("protocol=messages", line)
        # The upstream error text is sanitized: the embedded newline in the
        # stub's message must not forge a second log line.
        self.assertNotIn("\n", line)
        self.assertIn("Input should be less than or equal to 64000 "
                      "FAKE-SECOND-LOG-LINE", line)
        self.assertNotIn(SECRET_PROMPT, line)
        self.assertEqual(len(summaries), 1)
        self.assertIn("final=HTTP400", summaries[0])

    def test_messages_stream_400_shares_the_reject_branch(self):
        # HTTP-level isolation proof (verification only, no semantic change):
        # a Messages request with stream=True that the upstream rejects with
        # 400 falls through to the SAME unified reject branch as
        # stream=False. Code check: only stream+200 enters the streaming
        # branch (chat2api.py: "if stream and response.status_code == 200");
        # any non-200 reaches the shared reject/passthrough path below. The
        # client keeps the upstream's own 400, and the reject line carries
        # the inline output-budget segment with stream=stream and no message
        # content or credentials.
        with self.assertLogs("chat2api", level="INFO") as cm:
            response = requests.post(
                f"{self.base}/v1/messages",
                json={"model": "reject-400", "max_tokens": 64000,
                      "stream": True,
                      "messages": [{"role": "user",
                                    "content": SECRET_PROMPT}]},
                headers=self.auth, timeout=15)
        # The client sees the real upstream rejection, passed through.
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["type"],
                         "invalid_request_error")
        # Exactly one wire send: no streaming branch, no retry.
        self.assertEqual(len(captured), 1)
        self.assertIs(captured[0]["stream"], True)
        # Wire↔reject-line cap equality for the STREAM path too: the
        # same captured value the non-stream test pins must appear on
        # this reject line (a bare "shares the branch" claim is not
        # enough).
        self.assertEqual(captured[0]["max_tokens"], 64000)
        reject_lines = [line for line in cm.output
                        if "upstream_reject" in line]
        self.assertEqual(len(reject_lines), 1)
        line = reject_lines[0]
        self.assertIn("status=400", line)
        self.assertIn(
            "forwarded_model=reject-400 "
            "requested_model=reject-400", line)
        self.assertIn("stream=stream", line)
        # The same inline output-budget facts as the non-stream reject.
        self.assertIn("max_tokens_present=True", line)
        self.assertIn("max_tokens=64000", line)
        self.assertIn("output_cap_effective=64000", line)
        self.assertIn("protocol=messages", line)
        # No message content, no credentials on the reject line.
        for secret in (SECRET_PROMPT, SECRET_REPLY, LOCAL_KEY, "stub-token"):
            self.assertNotIn(secret, line)

    def test_messages_200_stays_log_summary_only(self):
        with self.assertLogs("chat2api", level="INFO") as cm:
            response = requests.post(
                f"{self.base}/v1/messages",
                json={"model": "claude-x", "max_tokens": 256,
                      "stream": False,
                      "messages": [{"role": "user",
                                    "content": SECRET_PROMPT}]},
                headers=self.auth, timeout=15)
            # The summary is logged after the response body, so it is awaited
            # INSIDE the capture window (see wait_for_summary).
            summaries = self.wait_for_summary(cm.output)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [line for line in cm.output if "upstream_reject" in line], [])
        self.assertEqual(len(summaries), 1)
        self.assertIn("final=HTTP200", summaries[0])


class TestOutputBudgetDiagnosticsUnit(unittest.TestCase):
    """Direct contract of the controlled diagnostics helper (no HTTP)."""

    def test_missing_body_and_missing_keys(self):
        diag = chat2api._output_budget_diagnostics(None, "m", None, "chat")
        self.assertEqual(diag["max_tokens_present"], False)
        self.assertEqual(diag["max_completion_tokens_present"], False)
        self.assertIsNone(diag["output_cap_effective"])
        self.assertEqual(diag["cap_source"], "unknown")

    def test_non_positive_and_fractional_and_container_kinds(self):
        for value, kind in ((0, "non_positive"), (-5, "non_positive"),
                            (12.5, "float"), ("x", "str"),
                            ([1], "list"), ({"a": 1}, "dict"),
                            (None, "null")):
            diag = chat2api._output_budget_diagnostics(
                {"max_tokens": value}, "m", None, "chat")
            self.assertFalse(diag["max_tokens_valid"], value)
            self.assertEqual(diag["max_tokens_kind"], kind)
            self.assertIsNone(diag["max_tokens"])
            self.assertIsNone(diag["output_cap_effective"])
            self.assertEqual(diag["cap_source"], "unknown")

    def test_bool_is_never_a_legal_cap(self):
        # bool is an int subclass in Python: the contract excludes it
        # explicitly, mirroring _reported_tokens.
        diag = chat2api._output_budget_diagnostics(
            {"max_tokens": True}, "m", None, "chat")
        self.assertFalse(diag["max_tokens_valid"])
        self.assertEqual(diag["max_tokens_kind"], "bool")

    def test_oversized_legal_int_not_quoted_verbatim(self):
        diag = chat2api._output_budget_diagnostics(
            {"max_tokens": 10 ** 30}, "m", None, "chat")
        self.assertTrue(diag["max_tokens_valid"])
        self.assertEqual(diag["max_tokens_kind"], "oversized_int")
        self.assertIsNone(diag["max_tokens"])
        # Legality decides the source, not the log bound.
        self.assertEqual(diag["cap_source"], "client_request")

    def test_illegal_high_precedence_field_claims_no_effective_cap(self):
        diag = chat2api._output_budget_diagnostics(
            {"max_tokens": 100, "max_completion_tokens": "x"},
            "m", None, "chat")
        self.assertIsNone(diag["output_cap_effective"])
        self.assertEqual(diag["cap_source"], "unknown")

    def test_level_and_model_types_sanitized(self):
        diag = chat2api._output_budget_diagnostics(
            {}, 123, ["high"], "chat")
        self.assertEqual(diag["model"], "")
        self.assertIsNone(diag["level"])
        blob = chat2api._format_output_budget(diag)
        self.assertIn("model=-", blob)
        self.assertIn("level=-", blob)

    def test_format_bounded_for_hostile_strings(self):
        diag = chat2api._output_budget_diagnostics({}, "m" * 5000, "x", "chat")
        blob = chat2api._format_output_budget(diag)
        self.assertLess(len(blob), 1000)
        self.assertNotIn("m" * 300, blob)

    def test_format_control_chars_never_forged_log_lines(self):
        # Client-controlled model/effort values must not inject a new log
        # line: every C0 control character (including \r \n \t) and DEL is
        # replaced by a space and whitespace runs collapse before rendering,
        # so the whole segment stays ONE physical log line.
        model = "Budget-A\nFAKE output budget request=zzz\r\t\x1b[31mred\x7f"
        level = "high\x00\x0b\x0cline2"
        diag = chat2api._output_budget_diagnostics({}, model, level, "chat")
        blob = chat2api._format_output_budget(diag)
        self.assertNotIn("\n", blob)
        self.assertNotIn("\r", blob)
        for code in range(0x20):
            self.assertNotIn(chr(code), blob)
        self.assertNotIn("\x7f", blob)
        # The injected words survive only as collapsed text inside the ONE
        # model= segment, never as a forged second record.
        self.assertEqual(blob.count("model="), 1)
        self.assertIn("level=high line2", blob)

    def test_format_string_fields_bounded_after_sanitizing(self):
        diag = chat2api._output_budget_diagnostics(
            {}, "x" * 250 + "\nEND", "y" * 220, "chat")
        blob = chat2api._format_output_budget(diag)
        model_value = blob[len("model="):blob.index(" level=")]
        self.assertLessEqual(len(model_value),
                             chat2api._OUTPUT_BUDGET_STR_CAP + 3)
        self.assertTrue(model_value.startswith("x" * 200))
        self.assertTrue(model_value.endswith("..."))
        level_value = blob[blob.index(" level=") + len(" level="):
                           blob.index(" max_tokens_present=")]
        self.assertLessEqual(len(level_value),
                             chat2api._OUTPUT_BUDGET_STR_CAP + 3)
        self.assertTrue(level_value.endswith("..."))

    def test_truncation_takes_the_raw_head_before_sanitizing(self):
        # The cap cuts the raw string first: a control character beyond the
        # cap is dropped by the cut, not smuggled in after sanitizing.
        diag = chat2api._output_budget_diagnostics(
            {}, "a" * 200 + "\nTAIL", None, "chat")
        blob = chat2api._format_output_budget(diag)
        model_value = blob[len("model="):blob.index(" level=")]
        self.assertEqual(model_value, "a" * 200 + "...")
        self.assertNotIn("TAIL", blob)

    def test_reject_line_sanitizes_both_model_fields(self):
        # Both model fields are client-controlled (chat passes the forwarded
        # model and the raw requested model; Messages passes messages_model
        # for both): a newline in either must not forge a second log line,
        # exactly like error_message.
        diag = chat2api._output_budget_diagnostics(
            {"model": "evil\nmodel", "max_tokens": 256}, "evil\nmodel",
            None, "chat")
        with self.assertLogs("chat2api", level="INFO") as cm:
            chat2api._log_upstream_reject(
                "req-1", 400, "evil\nmodel", "evil\nmodel WARNING forged",
                False, {"model": "evil\nmodel", "messages": []}, "", diag)
        lines = [line for line in cm.output if "upstream_reject" in line]
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertNotIn("\n", line)
        # One ordered pair pins BOTH sanitized fields. The distinct key
        # names also keep a bare "forwarded_model=" match from being
        # confused with the requested value.
        self.assertIn("forwarded_model=evil model "
                      "requested_model=evil model WARNING forged", line)
        self.assertIn("output_budget", line)


if __name__ == "__main__":
    unittest.main()
