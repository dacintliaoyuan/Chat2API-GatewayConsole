# -*- coding: utf-8 -*-
"""Regression tests for the usage-token normalization boundary (W5-2).

record_usage is the single funnel from the upstream usage shapes into the
ledger row. Before the normalization boundary it read only the canonical
OpenAI names through ``or`` fallbacks, which had two real defects:

  * a trusted ZERO was replaced by the local estimate (``0 or in_est``);
  * the newer alias fields (input_tokens/output_tokens) were not read at
    all, so a usage block reporting BOTH sides only through the aliases was
    recorded as "partial" with estimated counts instead of "upstream".

Contract pinned here (see handoff Plan-Revision 5 W5-2):

  * trusted counts are non-bool, non-negative ints (``_reported_tokens``);
    bool/negative/str/float/non-finite values are NOT counts and never
    become 0 - the row falls back to the local estimate with the honest
    coverage label ("estimated"/"none"), never "upstream";
  * "0" is a credible count and never falls back to an estimate;
  * prompt_tokens/completion_tokens and input_tokens/output_tokens name the
    SAME sides; when both spellings carry legal values the canonical OpenAI
    name wins with a FIXED priority, and sides are never summed;
  * usage split across several SSE events is merged by the CUMULATIVE
    SNAPSHOT contract every current producer follows (OpenAI include_usage's
    final chunk, UM's unified usage merge and the Anthropic counters are all
    running totals): the LATEST legal value per field wins, a later event
    that omits a field never erases the earlier report, and event values
    are never summed;
  * single-side usage is "partial" (upstream side + local estimate), full
    absence with estimates is "estimated", absence without estimates is
    "none"; estimates never masquerade as upstream evidence;
  * cached_tokens stays a SUBSET of the input side and reasoning_tokens a
    subset of the output side (readers subtract the subset, never re-add);
  * the per-key fee read end (share_metering) keeps bucketing new rows
    correctly and historical rows keep reading as before.

The stub upstream mirrors real chat SSE usage chunks. All paths land in
private temp directories; the ledger redirect is ordered by
tests/_chat2api_http_harness.py (B8-0): tracked servers drain every in-flight
request BEFORE the redirect is released, so no post-response record_usage
tail can reach the PRODUCTION ledger (the 2026-10-02 pollution incident).
"""

import importlib.util
import json
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

HERE = Path(__file__).resolve().parents[1]


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, HERE / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


chat2api = _load("chat2api_usage_normalize", "chat2api/chat2api.py")
metering = _load("share_metering_usage_normalize", "share_metering.py")

STUB_TOKEN = "stub-token"


def _sse(payload: str) -> bytes:
    return f"data: {payload}\n\n".encode("utf-8")


def _chunk(content: str = None, finish_reason=None, usage=None) -> str:
    delta = {}
    if content is not None:
        delta["content"] = content
    chunk = {"choices": [{"delta": delta, "finish_reason": finish_reason}]}
    if usage is not None:
        chunk["usage"] = usage
    return json.dumps(chunk, ensure_ascii=False)


class StubUpstreamHandler(BaseHTTPRequestHandler):
    """Fake chat SSE upstream keyed by model name."""

    protocol_version = "HTTP/1.1"
    posts = 0

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
        StubUpstreamHandler.posts += 1
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.close_connection = True
            if scenario == "usage-split":
                # The producer contract is a CUMULATIVE SNAPSHOT per field:
                # an early chunk reports the input side, a later chunk the
                # output side. The final whole-dict replacement of the old
                # code dropped the input side; the merge keeps it.
                self._send(_sse(_chunk(content="a",
                                       usage={"prompt_tokens": 10})))
                self._send(_sse(_chunk(content="b",
                                       usage={"completion_tokens": 3})))
                self._send(_sse(_chunk(finish_reason="stop")))
                self._send(b"data: [DONE]\n\n")
                time.sleep(0.3)
            elif scenario == "usage-snapshot":
                # Running totals restated by a later event: the latest value
                # per field wins, values are never summed (10+10 / 2+6).
                self._send(_sse(_chunk(
                    content="a",
                    usage={"prompt_tokens": 10, "completion_tokens": 2})))
                self._send(_sse(_chunk(
                    finish_reason="stop",
                    usage={"prompt_tokens": 10, "completion_tokens": 6})))
                self._send(b"data: [DONE]\n\n")
                time.sleep(0.3)
            elif scenario == "usage-illegal-later":
                # A later event with an illegal value must not erase the
                # earlier legal report of the same field.
                self._send(_sse(_chunk(content="a",
                                       usage={"prompt_tokens": 10})))
                self._send(_sse(_chunk(
                    finish_reason="stop",
                    usage={"prompt_tokens": "x", "completion_tokens": 4})))
                self._send(b"data: [DONE]\n\n")
                time.sleep(0.3)
            elif scenario == "usage-cache-and-reasoning":
                # R20-1: the nested details (cache read / reasoning) ride
                # along the cumulative snapshot.  The stream path normalizes,
                # merges and normalizes AGAIN on the way to the ledger, so the
                # boundary must be lossless across its own output shape - the
                # old one dropped both counts and still said "upstream".
                self._send(_sse(_chunk(
                    content="a",
                    usage={"prompt_tokens": 100, "completion_tokens": 2,
                           "prompt_tokens_details": {"cached_tokens": 60}})))
                self._send(_sse(_chunk(
                    finish_reason="stop",
                    usage={"prompt_tokens": 100, "completion_tokens": 10,
                           "prompt_tokens_details": {"cached_tokens": 60},
                           "completion_tokens_details": {"reasoning_tokens": 4}})))
                self._send(b"data: [DONE]\n\n")
                time.sleep(0.3)
            elif scenario == "usage-zero-then-interrupt":
                # A usage chunk with a REAL zero output side, then the stream
                # dies without finish_reason/DONE: the zero is trusted (the
                # upstream usage block is the authority for the sides), the
                # interruption stays a non-"ok" status.
                self._send(_sse(_chunk(
                    content="hello world",
                    usage={"prompt_tokens": 10, "completion_tokens": 0})))
                time.sleep(0.3)
            else:
                self._send(_sse(_chunk(content="hi")))
                self._send(_sse(_chunk(finish_reason="stop")))
                self._send(b"data: [DONE]\n\n")
                time.sleep(0.3)
        except (ConnectionError, BrokenPipeError, OSError):
            pass  # chat2api cancelled the upstream read


class UsageNormalizeTestBase(unittest.TestCase):
    """Isolation: loopback stubs, private temp ledger, no retry budgets."""

    def setUp(self):
        # B8-0 isolation lifecycle (see _chat2api_http_harness): the ledger
        # redirect is registered FIRST so it is released LAST - after every
        # handler drained and every server closed. The pre-fix LIFO bug
        # released the patch while a post-response handler was still writing
        # record_usage, which is how test rows reached the PRODUCTION
        # data/usage-chat2api.jsonl on 2026-10-02.
        self._tmp, self.usage_log = harness.install_usage_isolation(
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
        self.server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        # Registered AFTER the redirect: this teardown (shutdown → bounded
        # drain of in-flight requests → close) runs BEFORE the USAGE_LOG
        # patch stop, so the post-response record_usage tail of the LAST
        # request lands in the temp ledger, never the production one.
        harness.install_tracked_server(self, server, name="chat2api-gateway")
        self.base = f"http://127.0.0.1:{server.server_port}"

        for patch in (
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", False),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 0),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRY", False),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRIES", 0),
            mock.patch.object(chat2api, "PRICE_TABLE_FILE",
                              str(self._tmp / "prices.json")),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        chat2api._price_version_cache["stat"] = None
        chat2api._reader_slots = threading.BoundedSemaphore(
            chat2api.STREAM_MAX_CONCURRENT_READERS)
        StubUpstreamHandler.posts = 0

    def records(self):
        try:
            with open(self.usage_log, encoding="utf-8") as handle:
                return [json.loads(line) for line in handle if line.strip()]
        except FileNotFoundError:
            return []

    def wait_records(self, count=1, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            records = self.records()
            if len(records) >= count:
                return records
            time.sleep(0.1)
        self.fail(f"usage records not written within {timeout}s")

    @staticmethod
    def read_to_eof(response, timeout_s=15):
        started = time.monotonic()
        deadline = started + timeout_s
        events = []
        buf = b""
        for byte in response.iter_content(chunk_size=1):
            if time.monotonic() > deadline:
                break
            buf += byte
            while b"\n\n" in buf:
                event, buf = buf.split(b"\n\n", 1)
                if event.strip():
                    events.append(event.strip().decode("utf-8"))
        return events, time.monotonic() - started

    def stream_request(self, model, timeout=15):
        return requests.post(
            f"{self.base}/v1/chat/completions",
            json={"model": model,
                  "messages": [{"role": "user", "content": "hi"}],
                  "stream": True, "max_tokens": 16},
            stream=True, timeout=timeout)


class TestRecordUsageNormalization(UsageNormalizeTestBase):
    """record_usage's trusted-field rules (the narrow boundary)."""

    def write_row(self, usage, in_est=3, out_est=2, status="ok"):
        chat2api.record_usage("m", False, time.time() - 1, usage,
                              in_est, out_est, status=status)
        return self.records()[-1]

    def test_real_zero_tokens_are_trusted_not_estimated(self):
        rec = self.write_row({"prompt_tokens": 0, "completion_tokens": 0})
        self.assertEqual(rec["input_tokens"], 0)
        self.assertEqual(rec["output_tokens"], 0)
        self.assertEqual(rec["total_tokens"], 0)
        self.assertEqual(rec["usage_source"], "upstream")
        self.assertFalse(rec["estimated"])

    def test_alias_fields_name_the_same_sides(self):
        rec = self.write_row({"input_tokens": 100, "output_tokens": 10})
        self.assertEqual(rec["input_tokens"], 100)
        self.assertEqual(rec["output_tokens"], 10)
        self.assertEqual(rec["total_tokens"], 110)
        self.assertEqual(rec["usage_source"], "upstream")
        self.assertFalse(rec["estimated"])

    def test_alias_conflict_keeps_the_canonical_name(self):
        # Both spellings legal and different: the canonical OpenAI name wins
        # (fixed priority), sides are never summed (110, not 1159).
        rec = self.write_row({"prompt_tokens": 100, "input_tokens": 999,
                              "completion_tokens": 5, "output_tokens": 50})
        self.assertEqual(rec["input_tokens"], 100)
        self.assertEqual(rec["output_tokens"], 5)
        self.assertEqual(rec["total_tokens"], 105)

    def test_alias_conflict_trusted_zero_beats_alias(self):
        # The canonical input side reported a real 0: 0 is trusted, it
        # neither falls back to the estimate nor to the alias, and it is not
        # summed. Only the input side was reported, so the coverage is
        # "partial" (the output side is still a local estimate).
        rec = self.write_row({"prompt_tokens": 0, "input_tokens": 7},
                             in_est=3, out_est=2)
        self.assertEqual(rec["input_tokens"], 0)
        self.assertEqual(rec["output_tokens"], 2)
        self.assertEqual(rec["usage_source"], "partial")

    def test_alias_only_single_side_is_partial_with_real_count(self):
        rec = self.write_row({"input_tokens": 10})
        self.assertEqual(rec["input_tokens"], 10)
        self.assertEqual(rec["output_tokens"], 2)
        self.assertEqual(rec["total_tokens"], 12)
        self.assertEqual(rec["usage_source"], "partial")
        self.assertFalse(rec["estimated"])

    def test_illegal_values_are_not_trusted_and_not_zero(self):
        for illegal in (-5, True, "5", 1.5, float("inf")):
            with self.subTest(illegal=illegal):
                rec = self.write_row({"prompt_tokens": illegal,
                                      "completion_tokens": illegal})
                # Nothing was legally reported: local estimates stand and
                # the coverage never claims upstream evidence.
                self.assertEqual(rec["input_tokens"], 3)
                self.assertEqual(rec["output_tokens"], 2)
                self.assertEqual(rec["usage_source"], "estimated")
                self.assertTrue(rec["estimated"])

    def test_junk_usage_dict_is_estimated_never_upstream(self):
        # A usage dict without any trusted token field must not let the row
        # claim upstream coverage (estimates never masquerade as upstream).
        rec = self.write_row({"foo": "bar"})
        self.assertEqual(rec["usage_source"], "estimated")
        self.assertTrue(rec["estimated"])
        self.assertEqual(rec["input_tokens"], 3)
        self.assertEqual(rec["output_tokens"], 2)

    def test_no_evidence_without_estimates_is_none(self):
        rec = self.write_row({}, in_est=0, out_est=0)
        self.assertEqual(rec["usage_source"], "none")
        self.assertEqual(rec["input_tokens"], 0)
        self.assertEqual(rec["output_tokens"], 0)
        self.assertTrue(rec["estimated"])

    def test_reported_total_is_kept(self):
        rec = self.write_row({"prompt_tokens": 10, "total_tokens": 100})
        self.assertEqual(rec["input_tokens"], 10)
        self.assertEqual(rec["output_tokens"], 2)
        self.assertEqual(rec["total_tokens"], 100)
        self.assertEqual(rec["usage_source"], "partial")

    def test_cache_and_reasoning_subsets_are_legal_only(self):
        rec = self.write_row({
            "prompt_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 4},
            "completion_tokens": 8,
            "completion_tokens_details": {"reasoning_tokens": 5}})
        self.assertEqual(rec["cached_input_tokens"], 4)
        self.assertEqual(rec["reasoning_tokens"], 5)
        # Subsets never re-add: the reader subtracts them from the sides.
        self.assertEqual(rec["total_tokens"], 18)

        rec = self.write_row({
            "prompt_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": "x"},
            "completion_tokens": 8,
            "completion_tokens_details": {"reasoning_tokens": -1}})
        self.assertEqual(rec["cached_input_tokens"], 0)
        self.assertEqual(rec["reasoning_tokens"], 0)

    def test_normalize_is_idempotent_on_its_own_shape(self):
        merged = chat2api._merge_usage_snapshots(
            None, {"prompt_tokens": 10, "completion_tokens": 3})
        self.assertEqual(chat2api._normalized_usage_fields(merged),
                         {"prompt_tokens": 10, "completion_tokens": 3})

    def test_normalize_is_lossless_across_its_own_output_shape(self):
        # R20-1: the boundary must be idempotent over EVERY field it emits,
        # not just the two sides.  Its output IS the accepted input shape, so a
        # second pass reads exactly what the first emitted - otherwise the
        # stream path (normalize -> merge -> normalize) dropped the cache and
        # reasoning counts while still claiming "upstream" coverage.
        once = chat2api._normalized_usage_fields({
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 60},
            "completion_tokens_details": {"reasoning_tokens": 4},
        })
        self.assertEqual(once, {
            "prompt_tokens": 100, "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 60},
            "completion_tokens_details": {"reasoning_tokens": 4}})
        self.assertEqual(chat2api._normalized_usage_fields(once), once)

    def test_detail_edges_are_ignored_or_kept(self):
        # R20-1 边界：嵌套 details 不是 dict 或值非法 → 该明细缺席（不是 0）；
        # 合法 0 是可信值，保留；没有嵌套 details 就没有明细。
        self.assertEqual(
            chat2api._normalized_usage_fields(
                {"prompt_tokens": 10, "prompt_tokens_details": ["x"]}),
            {"prompt_tokens": 10})
        self.assertEqual(
            chat2api._normalized_usage_fields({
                "prompt_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": -1},
                "completion_tokens_details": {"reasoning_tokens": "x"}}),
            {"prompt_tokens": 10})
        self.assertEqual(
            chat2api._normalized_usage_fields({
                "prompt_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 0}}),
            {"prompt_tokens": 10,
             "prompt_tokens_details": {"cached_tokens": 0}})

    def test_merge_keeps_each_detail_independently(self):
        # R20-1：两个明细按 KEY 分别合并——只报其中一个的快照不会抹掉
        # 另一个的早先报告。
        merged = chat2api._merge_usage_snapshots(
            {"prompt_tokens_details": {"cached_tokens": 60}},
            {"completion_tokens_details": {"reasoning_tokens": 4}})
        self.assertEqual(merged["prompt_tokens_details"],
                          {"cached_tokens": 60})
        self.assertEqual(merged["completion_tokens_details"],
                          {"reasoning_tokens": 4})

    def test_merge_keeps_the_details_a_later_snapshot_omits(self):
        # R20-1: a later cumulative snapshot that omits the details must not
        # erase the earlier cache/reasoning report, and the merged (already
        # normalized) block must survive the final normalization.
        merged = chat2api._merge_usage_snapshots(
            None, {"prompt_tokens": 100, "completion_tokens": 2,
                   "prompt_tokens_details": {"cached_tokens": 60}})
        merged = chat2api._merge_usage_snapshots(
            merged, {"prompt_tokens": 100, "completion_tokens": 10})
        self.assertEqual(merged["prompt_tokens_details"],
                          {"cached_tokens": 60})
        self.assertEqual(chat2api._normalized_usage_fields(merged), merged)


class TestChatSseSegmentedUsage(UsageNormalizeTestBase):
    """Usage split across SSE events, merged by the snapshot contract."""

    def test_later_usage_chunk_does_not_drop_the_input_side(self):
        with self.stream_request("usage-split") as response:
            self.assertEqual(response.status_code, 200)
            self.read_to_eof(response)
        (rec,) = self.wait_records()
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["input_tokens"], 10)
        self.assertEqual(rec["output_tokens"], 3)
        self.assertEqual(rec["total_tokens"], 13)
        self.assertEqual(rec["usage_source"], "upstream")

    def test_snapshots_are_taken_not_summed(self):
        with self.stream_request("usage-snapshot") as response:
            self.assertEqual(response.status_code, 200)
            self.read_to_eof(response)
        (rec,) = self.wait_records()
        self.assertEqual(rec["input_tokens"], 10)
        self.assertEqual(rec["output_tokens"], 6)
        self.assertEqual(rec["total_tokens"], 16)
        self.assertEqual(rec["usage_source"], "upstream")

    def test_illegal_later_chunk_keeps_the_earlier_legal_value(self):
        with self.stream_request("usage-illegal-later") as response:
            self.assertEqual(response.status_code, 200)
            self.read_to_eof(response)
        (rec,) = self.wait_records()
        self.assertEqual(rec["input_tokens"], 10)
        self.assertEqual(rec["output_tokens"], 4)
        self.assertEqual(rec["usage_source"], "upstream")

    def test_cache_and_reasoning_survive_the_stream_merge(self):
        # R20-1 end-to-end through the real Handler: the nested details of
        # the streamed usage chunks must reach the ledger row.  Before the fix
        # the row said "upstream" while carrying cached_input_tokens=0 and
        # reasoning_tokens=0, so cached input was billed as plain input.
        with self.stream_request("usage-cache-and-reasoning") as response:
            self.assertEqual(response.status_code, 200)
            self.read_to_eof(response)
        (rec,) = self.wait_records()
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["input_tokens"], 100)
        self.assertEqual(rec["output_tokens"], 10)
        self.assertEqual(rec["cached_input_tokens"], 60)
        self.assertEqual(rec["reasoning_tokens"], 4)
        self.assertEqual(rec["usage_source"], "upstream")

    def test_real_zero_output_survives_an_interrupted_stream(self):
        with self.stream_request("usage-zero-then-interrupt") as response:
            self.assertEqual(response.status_code, 200)
            self.read_to_eof(response)
        (rec,) = self.wait_records()
        self.assertNotEqual(rec["status"], "ok")
        self.assertEqual(rec["input_tokens"], 10)
        # The upstream reported a real 0 for the output side; the local
        # character estimate of the streamed text must not replace it.
        self.assertEqual(rec["output_tokens"], 0)
        self.assertEqual(rec["usage_source"], "upstream")
        self.assertFalse(rec["estimated"])


class TestShareFeeReadEnd(UsageNormalizeTestBase):
    """The per-key fee read end over rows the new writer produces."""

    @staticmethod
    def price_fn(model, at_ms, in_tok, out_tok, cache_tok, reason_tok):
        # Billed on the reported sides minus the cached subset; every model
        # is priced so the test isolates the bucketing, not the price table.
        cost = in_tok * 0.001 + out_tok * 0.002
        return cost, True, model, False, "v-test"

    def write_share_rows(self, rows):
        ledger = self._tmp / "usage-share-backend.jsonl"
        with open(ledger, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        return ledger

    @staticmethod
    def row_base():
        return {
            "time": "2026-09-30T12:00:00+08:00",
            "service": "chat2api",
            "model": "GLM-5.2-NVFP4",
            "stream": False,
            "status": "ok",
            "input_tokens": 100,
            "output_tokens": 10,
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 110,
            "estimated": False,
            "duration_ms": 1000,
            "usage_source": "upstream",
            "protocol": "chat",
            "key_id": "k1",
            "request_id": "r1",
            "attribution": "verified",
            "price_version": "v-test",
        }

    @classmethod
    def row_with(cls, **overrides):
        rec = cls.row_base()
        rec.update(overrides)
        return rec

    def test_new_shape_rows_bucket_per_key_correctly(self):
        rows = [
            self.row_with(key_id="k1"),                     # upstream: known
            self.row_with(key_id="k1", input_tokens=0,
                          output_tokens=0, total_tokens=0),  # real 0: known
            self.row_with(key_id="k1", usage_source="partial",
                          input_tokens=50),                  # estimated side
            self.row_with(key_id="k1", usage_source="none",
                          input_tokens=0, output_tokens=0,
                          total_tokens=0),                   # unknown
            self.row_with(key_id="k2", input_tokens=500,
                          output_tokens=20, total_tokens=520),
        ]
        ledger = self.write_share_rows(rows)
        result = metering.read_share_usage(str(ledger), self.price_fn)
        keys = {entry["key_id"]: entry for entry in result["keys"]}
        # k1: the upstream row costs 100*0.001+10*0.002 = 0.12, the real-zero
        # row contributes 0.0 to the SAME known bucket (a trusted zero is
        # known usage, not unknown); the partial row's 50*0.001+10*0.002 =
        # 0.07 goes to the estimated bucket; the "none" row is UNKNOWN
        # (counted, never priced as 0).
        self.assertEqual(keys["k1"]["requests"], 4)
        self.assertAlmostEqual(keys["k1"]["knownCostUsd"], 0.12)
        self.assertAlmostEqual(keys["k1"]["estimatedCostUsd"], 0.07)
        self.assertEqual(keys["k1"]["unknownRequests"], 1)
        self.assertEqual(keys["k2"]["knownCostUsd"], 0.54)

    def test_cached_subset_reaches_the_price_function(self):
        # R20-1 读端：写端现在保留的缓存数必须原样到达计价函数（价目引擎
        # 自己把它当输入子集减掉），否则缓存命中会被按普通输入单价计费。
        seen = {}

        def price_fn(model, at_ms, in_tok, out_tok, cache_tok, reason_tok):
            seen.update({"in": in_tok, "out": out_tok, "cache": cache_tok,
                         "reason": reason_tok})
            return 0.0, True, model, False, "v-test"

        rows = [self.row_with(cached_input_tokens=60, reasoning_tokens=4)]
        ledger = self.write_share_rows(rows)
        metering.read_share_usage(str(ledger), price_fn)
        self.assertEqual(seen, {"in": 100, "out": 10, "cache": 60,
                                "reason": 4})

    def test_historical_rows_without_usage_source_still_bucket(self):
        # Historical rows carry no usage_source key at all; the reader keeps
        # deriving coverage from the legacy "estimated" bool - untouched by
        # the writer-side fix.
        legacy = {k: v for k, v in self.row_base().items()
                  if k != "usage_source"}
        legacy_estimated = dict(legacy, key_id="k2", estimated=True)
        rows = [legacy, legacy_estimated]
        ledger = self.write_share_rows(rows)
        result = metering.read_share_usage(str(ledger), self.price_fn)
        keys = {entry["key_id"]: entry for entry in result["keys"]}
        self.assertAlmostEqual(keys["k1"]["knownCostUsd"], 0.12)
        self.assertAlmostEqual(keys["k2"]["estimatedCostUsd"], 0.12)


if __name__ == "__main__":
    unittest.main()
