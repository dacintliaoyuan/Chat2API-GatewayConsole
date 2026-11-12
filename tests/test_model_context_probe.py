# -*- coding: utf-8 -*-
"""Offline tests for the declared-context probe queue (L-E.4).

Everything runs against injected endpoints and an injectable ``post_json``:
NO real endpoint is contacted, nothing is billed, nothing touches the data
directory.  The fake transport validates the queue's own wire contract (the
reply must quote the request's final anchor) by echoing the FINAL anchor it
finds in the request body — so the tests exercise the real marker mechanism.
"""

import contextlib
import http.server
import json
import re
import sys
import tempfile
import threading
import time
import unittest

from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import console_model_actions  # noqa: E402
import console_operations  # noqa: E402
import model_sync  # noqa: E402
from console_operations import OperationError  # noqa: E402
from model_sync_core import CatalogModel, empty_state  # noqa: E402

FINAL_ANCHOR = re.compile(r"\[ctxprobe (run\d+)\] FINAL anchor")


def _record(source="um", model_id="m", status="pending", floor=None,
            declared=128000):
    return {"source": source, "model_id": model_id,
            "declared_context": declared, "declared_at": "t", "status": status,
            "observed_floor": floor, "attempts": 0, "last_run": None,
            "last_error": None}


def _fake_post_ok(result_log, sizes=None):
    """Transport that echoes the request's final anchor (real tail-quote)."""

    def post(url, body, timeout=90, token=None):
        content = body["messages"][0]["content"]
        marker = FINAL_ANCHOR.search(content).group(1)
        result_log.append({
            "url": url, "token": token, "max_tokens": body["max_tokens"],
            "chars": len(content),
        })
        if sizes is not None:
            sizes.append(body["messages"][0]["content"].count("anchor "))
        return {"status": 200, "body": {"choices": [{"message": {
            "content": "[ctxprobe %s] FINAL anchor — quote this line verbatim"
                       % marker}}]}}

    return post


ENDPOINTS = {"um": ("http://127.0.0.1:1/v1/chat/completions", None)}


class TestProbeContextQueue(unittest.TestCase):
    def test_success_ladder_verifies_and_raises_floor(self):
        records = {"um|m": _record()}
        calls = []
        outcome = model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=_fake_post_ok(calls))
        self.assertEqual(outcome["used"], len(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(outcome["stopped"], None)
        self.assertEqual(records["um|m"]["status"], "verified")
        self.assertEqual(records["um|m"]["observed_floor"],
                         max(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(records["um|m"]["attempts"],
                         len(model_sync.PROBE_INPUT_STEPS))
        self.assertIsNone(records["um|m"].get("last_error"))
        self.assertIsNotNone(records["um|m"]["last_run"])
        # Ascending bounded inputs; output capped by the budget; the marker
        # mechanism is real (the fake had to parse the anchor to answer).
        chars = [call["chars"] for call in calls]
        self.assertEqual(chars, sorted(chars))
        self.assertGreaterEqual(chars[0], model_sync.PROBE_INPUT_STEPS[0])
        self.assertTrue(all(call["max_tokens"] == 64 for call in calls))

    def test_verified_success_without_usage_keeps_token_evidence_unknown(self):
        # The shared fake reports no usage block: the ladder still verifies,
        # last_success is stamped, and token evidence stays None (Token 容量
        # 未知) — never a char-derived estimate.
        records = {"um|m": _record()}
        model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=_fake_post_ok([]))
        record = records["um|m"]
        self.assertIsNotNone(record["last_success"])
        self.assertIsNone(record.get("token_evidence"))
        self.assertEqual(record["status"], "verified")

    def test_anchor_success_adopts_largest_trusted_usage_report(self):
        # Upstream usage reports are the ONLY token evidence source: each
        # anchor-verified step reports a trusted prompt_tokens count and the
        # record keeps the largest one. Characters never become tokens here.
        reports = iter([100, 200, 300, 400])

        def post(url, body, timeout=90, token=None):
            content = body["messages"][0]["content"]
            marker = FINAL_ANCHOR.search(content).group(1)
            return {"status": 200, "body": {
                "choices": [{"message": {
                    "content": "[ctxprobe %s] FINAL anchor — quote this "
                               "line verbatim" % marker}}],
                "usage": {"prompt_tokens": next(reports)}}}

        records = {"um|m": _record()}
        model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=post)
        self.assertEqual(records["um|m"]["token_evidence"], 400)
        self.assertIsNotNone(records["um|m"]["last_success"])

    def test_input_tokens_alias_is_trusted_when_canonical_absent(self):
        # Same-side alias rule (canonical prompt_tokens wins when both are
        # legal): input_tokens alone is a legal trusted report.

        def post(url, body, timeout=90, token=None):
            content = body["messages"][0]["content"]
            marker = FINAL_ANCHOR.search(content).group(1)
            return {"status": 200, "body": {
                "choices": [{"message": {
                    "content": "[ctxprobe %s] FINAL anchor — quote this "
                               "line verbatim" % marker}}],
                "usage": {"input_tokens": 1234}}}

        records = {"um|m": _record()}
        model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=post)
        self.assertEqual(records["um|m"]["token_evidence"], 1234)

    def test_canonical_prompt_tokens_wins_over_alias(self):
        # When both spellings of the input side are legal, the canonical
        # OpenAI name wins with a FIXED priority; sides are never summed.

        def post(url, body, timeout=90, token=None):
            content = body["messages"][0]["content"]
            marker = FINAL_ANCHOR.search(content).group(1)
            return {"status": 200, "body": {
                "choices": [{"message": {
                    "content": "[ctxprobe %s] FINAL anchor — quote this "
                               "line verbatim" % marker}}],
                "usage": {"prompt_tokens": 700, "input_tokens": 999}}}

        records = {"um|m": _record()}
        model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=post)
        self.assertEqual(records["um|m"]["token_evidence"], 700)

    def test_untrusted_usage_shapes_leave_token_evidence_unknown(self):
        # bool / negative / str / float (incl. non-finite) are NOT trusted
        # token counts (same rule as the gateway usage boundary): the record
        # still verifies and stamps last_success, but token_evidence stays
        # None — Token 容量未知, never fabricated from illegal values.
        illegal = [True, False, -1, "61234", 61234.5, float("nan"),
                   float("inf"), None]
        state = {"index": 0}

        def post(url, body, timeout=90, token=None):
            content = body["messages"][0]["content"]
            marker = FINAL_ANCHOR.search(content).group(1)
            usage = {"prompt_tokens": illegal[state["index"]]}
            state["index"] += 1
            return {"status": 200, "body": {
                "choices": [{"message": {
                    "content": "[ctxprobe %s] FINAL anchor — quote this "
                               "line verbatim" % marker}}],
                "usage": usage}}

        records = {"um|m": _record()}
        model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=post)
        self.assertEqual(records["um|m"]["status"], "verified")
        self.assertIsNotNone(records["um|m"]["last_success"])
        self.assertIsNone(records["um|m"].get("token_evidence"))

    def test_trusted_zero_report_is_kept_verbatim(self):
        # A real upstream report of 0 input tokens is recorded as 0 (真实 0
        # 如实保留，与网关 usage 边界的 trusted-zero 规则一致)；缺席才未知。
        # This is the upstream's own report — never a backend fabrication.
        def post(url, body, timeout=90, token=None):
            content = body["messages"][0]["content"]
            marker = FINAL_ANCHOR.search(content).group(1)
            return {"status": 200, "body": {
                "choices": [{"message": {
                    "content": "[ctxprobe %s] FINAL anchor — quote this "
                               "line verbatim" % marker}}],
                "usage": {"prompt_tokens": 0}}}

        records = {"um|m": _record()}
        model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=post)
        self.assertEqual(records["um|m"]["token_evidence"], 0)

    def test_failed_ceiling_keeps_earlier_token_evidence(self):
        # A later explicit context-limit refusal must not erase the token
        # evidence already earned by an earlier verified step of this run.
        calls = []

        def post(url, body, timeout=90, token=None):
            content = body["messages"][0]["content"]
            marker = FINAL_ANCHOR.search(content).group(1)
            calls.append(1)
            if len(calls) == 1:
                return {"status": 200, "body": {
                    "choices": [{"message": {
                        "content": "[ctxprobe %s] FINAL anchor — quote this "
                                   "line verbatim" % marker}}],
                    "usage": {"prompt_tokens": 512}}}
            return {"status": 400, "body": {"error":
                    "Range of input length should be [1, 983616]"}}

        records = {"um|m": _record()}
        model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=post)
        self.assertEqual(records["um|m"]["status"], "failed")
        self.assertEqual(records["um|m"]["token_evidence"], 512)
        self.assertIsNotNone(records["um|m"]["last_success"])
        self.assertEqual(records["um|m"]["last_error"], "ContextLimitRefused")

    def test_resume_skips_steps_at_or_below_known_floor(self):
        records = {"um|m": _record(floor=16000)}
        calls = []
        model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=_fake_post_ok(calls))
        self.assertEqual(len(calls), 2)  # 64000 + 256000 only
        self.assertEqual(records["um|m"]["observed_floor"], 256000)

    def test_pending_full_ladder_floor_resumes_without_requests(self):
        # A PENDING record whose CURRENT-generation evidence already covers
        # the whole ladder is a legal resume: zero requests, verified.
        records = {"um|m": _record(status="pending", floor=256000)}
        outcome = model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=_fake_post_ok([]))
        self.assertEqual(outcome["used"], 0)
        self.assertEqual(records["um|m"]["status"], "verified")
        self.assertEqual(records["um|m"]["observed_floor"], 256000)

    def test_stale_record_reruns_full_ladder_from_independent_evidence(self):
        # Plan-Revision 7 step 3: a stale record's old floor belongs to a
        # retired generation — it can NEVER be reused to zero-request-verify
        # (the B7-3 flaw). At first touch the old evidence is archived into
        # history, the generation restarts, and the FULL ladder is re-run.
        records = {"um|m": _record(status="stale", floor=256000)}
        calls = []
        outcome = model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=_fake_post_ok(calls))
        self.assertEqual(outcome["used"], len(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(records["um|m"]["status"], "verified")
        self.assertEqual(records["um|m"]["observed_floor"],
                         max(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(records["um|m"]["attempts"],
                         len(model_sync.PROBE_INPUT_STEPS))
        # Old generation archived (old-shape record: revision unknown → None
        # label), new generation starts at revision 1 (first observed bump).
        history = records["um|m"]["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["observed_floor"], 256000)
        self.assertEqual(history[0]["status"], "stale")
        self.assertIsNone(history[0]["evidence_revision"])
        self.assertEqual(records["um|m"]["evidence_revision"], 1)
        self.assertEqual(records["um|m"]["scheme_revision"],
                         model_sync.PROBE_SCHEME_REVISION)

    def test_selection_limits_requests_to_selected_records(self):
        # Plan-Revision 7 step 3: an explicit selection narrows the request
        # set — only the selected record is billed and mutated.
        records = {"um|a": _record(model_id="a"),
                   "um|b": _record(model_id="b")}
        calls = []
        outcome = model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=_fake_post_ok(calls),
            selection={"um": {"a"}})
        self.assertEqual(outcome["used"], len(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual([row["key"] for row in outcome["results"]], ["um|a"])
        self.assertEqual(records["um|a"]["status"], "verified")
        self.assertEqual(records["um|a"]["observed_floor"],
                         max(model_sync.PROBE_INPUT_STEPS))
        # Unselected record: untouched, zero requests, no generation change.
        self.assertEqual(records["um|b"]["status"], "pending")
        self.assertEqual(records["um|b"]["attempts"], 0)
        self.assertIsNone(records["um|b"]["observed_floor"])
        self.assertNotIn("history", records["um|b"])

    def test_selection_of_nothing_consumes_no_budget(self):
        records = {"um|a": _record(model_id="a")}
        outcome = model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=_fake_post_ok([]),
            selection={"um": {"someone-else"}})
        self.assertEqual(outcome["used"], 0)
        self.assertEqual(outcome["results"], [])
        self.assertEqual(records["um|a"]["status"], "pending")
        self.assertEqual(records["um|a"]["attempts"], 0)

    def test_selection_none_keeps_full_queue_semantics(self):
        records = {"um|a": _record(model_id="a"), "um|b": _record(model_id="b")}
        outcome = model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=_fake_post_ok([]))
        self.assertEqual(outcome["used"], 2 * len(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual({row["key"] for row in outcome["results"]},
                         {"um|a", "um|b"})

    def test_selection_with_unconfirmable_shape_is_rejected(self):
        records = {"um|a": _record(model_id="a")}
        with self.assertRaises(ValueError):
            model_sync.probe_context_queue(
                ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
                lambda: True, post_json=_fake_post_ok([]),
                selection="um|a")
        self.assertEqual(records["um|a"]["status"], "pending")

    def test_retest_verified_archives_generation_and_reruns(self):
        # retest=True on a verified record: the current generation moves into
        # history (labelled with its own revision, reason "retest") and the
        # record re-measures the FULL ladder from independent evidence —
        # the old floor cannot skip any step.
        records = {"um|m": _record(status="verified", floor=256000,
                                   declared=128000)}
        records["um|m"].update({
            "attempts": 4, "last_run": "t0", "last_success": "t1",
            "token_evidence": 61234, "evidence_revision": 2,
            "scheme_revision": model_sync.PROBE_SCHEME_REVISION,
            "stale_reason": None})
        calls = []
        outcome = model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=_fake_post_ok(calls), retest=True)
        self.assertEqual(outcome["used"], len(model_sync.PROBE_INPUT_STEPS))
        record = records["um|m"]
        self.assertEqual(record["status"], "verified")
        self.assertEqual(record["observed_floor"],
                         max(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(record["attempts"], len(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(record["evidence_revision"], 3)
        self.assertEqual(record["scheme_revision"],
                         model_sync.PROBE_SCHEME_REVISION)
        self.assertIsNone(record["stale_reason"])
        history = record["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "verified")
        self.assertEqual(history[0]["evidence_revision"], 2)
        self.assertEqual(history[0]["observed_floor"], 256000)
        self.assertEqual(history[0]["token_evidence"], 61234)
        self.assertEqual(history[0]["reason"], "retest")
        self.assertIsNotNone(history[0]["archived_at"])
        # The result row reports the generation reset honestly.
        row = outcome["results"][0]
        self.assertTrue(row["retest"])
        self.assertEqual(row["previous_status"], "verified")

    def test_retest_failed_archives_ceiling_and_reruns(self):
        # A failed record's refusal ceiling is historical evidence of the old
        # generation: retest archives it and re-measures from scratch.
        records = {"um|m": _record(status="failed", floor=None,
                                   declared=128000)}
        records["um|m"].update({
            "observed_ceiling": 4000, "attempts": 1,
            "last_error": "ContextLimitRefused", "evidence_revision": 1})

        def post(url, body, timeout=90, token=None):
            content = body["messages"][0]["content"]
            marker = FINAL_ANCHOR.search(content).group(1)
            return {"status": 200, "body": {"choices": [{"message": {
                "content": "[ctxprobe %s] FINAL anchor — quote this line "
                           "verbatim" % marker}}]}}

        outcome = model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=post, retest=True)
        record = records["um|m"]
        self.assertEqual(record["status"], "verified")
        self.assertEqual(record["observed_ceiling"], None)
        self.assertEqual(record["evidence_revision"], 2)
        history = record["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["observed_ceiling"], 4000)
        self.assertEqual(history[0]["last_error"], "ContextLimitRefused")
        self.assertEqual(history[0]["status"], "failed")
        self.assertEqual(history[0]["reason"], "retest")
        self.assertTrue(outcome["results"][0]["retest"])

    def test_retest_false_leaves_verified_and_failed_untouched(self):
        # covered by test_non_eligible_statuses_are_untouched; here the
        # retest flag is explicitly False on a selected verified record.
        records = {"um|v": _record(status="verified", floor=256000)}
        records["um|v"]["evidence_revision"] = 2
        outcome = model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=_fake_post_ok([]), retest=False,
            selection={"um": {"v"}})
        self.assertEqual(outcome["used"], 0)
        self.assertEqual(outcome["results"], [])
        self.assertEqual(records["um|v"]["status"], "verified")
        self.assertEqual(records["um|v"]["evidence_revision"], 2)
        self.assertNotIn("history", records["um|v"])

    def test_selection_and_retest_combine(self):
        # retest applies ONLY inside the selection: the selected failed
        # record re-measures; the unselected verified record stays untouched.
        records = {"um|f": _record(model_id="f", status="failed",
                                   declared=128000),
                   "um|v": _record(model_id="v", status="verified",
                                   floor=256000, declared=128000)}
        records["um|f"]["observed_ceiling"] = 4000
        outcome = model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=_fake_post_ok([]), retest=True,
            selection={"um": {"f"}})
        self.assertEqual([row["key"] for row in outcome["results"]], ["um|f"])
        self.assertTrue(outcome["results"][0]["retest"])
        self.assertEqual(records["um|f"]["status"], "verified")
        self.assertEqual(records["um|v"]["status"], "verified")
        self.assertIsNone(records["um|v"].get("evidence_revision"))
        self.assertNotIn("history", records["um|v"])

    def test_explicit_context_limit_records_failure_ceiling(self):
        def post(url, body, timeout=90, token=None):
            return {"status": 400, "body": {"error":
                    "Range of input length should be [1, 983616]"}}

        records = {"um|m": _record()}
        outcome = model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=post)
        # The first step is already refused explicitly: one request, then the
        # failure ceiling is recorded and the record stops.
        self.assertEqual(outcome["used"], 1)
        self.assertEqual(records["um|m"]["status"], "failed")
        self.assertEqual(records["um|m"]["observed_ceiling"], 4000)
        self.assertEqual(records["um|m"]["last_error"], "ContextLimitRefused")

    def test_transient_5xx_stops_round_without_capacity_verdict(self):
        def post(url, body, timeout=90, token=None):
            return {"status": 503, "body": {"error": "temporary"}}

        records = {"um|m": _record()}
        model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=post)
        self.assertEqual(records["um|m"]["status"], "pending")
        self.assertEqual(records["um|m"]["last_error"], "HTTPError")
        self.assertEqual(records["um|m"]["attempts"], 1)
        self.assertIsNone(records["um|m"]["observed_floor"])

    def test_transport_exception_is_transient(self):
        def post(url, body, timeout=90, token=None):
            raise TimeoutError("read timed out")

        records = {"um|m": _record(status="stale")}
        model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=post)
        # Step-3 generation rule: the stale flag is consumed at first touch
        # (the record re-queues as pending); a transport failure keeps the
        # record pending WITHOUT any capacity verdict (no fake success).
        self.assertEqual(records["um|m"]["status"], "pending")
        self.assertEqual(records["um|m"]["last_error"], "TimeoutError")
        self.assertEqual(records["um|m"]["attempts"], 1)

    def test_reply_without_tail_anchor_is_inconclusive(self):
        def post(url, body, timeout=90, token=None):
            return {"status": 200, "body": {"choices": [{"message": {
                "content": "I will not quote anchors."}}]}}

        records = {"um|m": _record()}
        model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=post)
        self.assertEqual(records["um|m"]["status"], "pending")
        self.assertEqual(records["um|m"]["last_error"],
                         "InconclusiveResponse")
        self.assertIsNone(records["um|m"]["observed_floor"])

    def test_budget_stops_before_the_next_request(self):
        records = {"um|m": _record()}
        calls = []

        def guard():
            calls.append(1)
            return True

        outcome = model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 2, "max_tokens": 64}, records,
            guard, post_json=_fake_post_ok([]))
        self.assertEqual(outcome["used"], 2)
        self.assertEqual(outcome["stopped"], "budget")
        self.assertEqual(records["um|m"]["status"], "pending")
        self.assertEqual(records["um|m"]["observed_floor"], 16000)
        # The budget gate sits BEFORE the cancel hook: the third request is
        # refused without even consulting the guard (2 guard calls).
        self.assertEqual(len(calls), 2)

    def test_cancel_hook_fires_before_any_request(self):
        records = {"um|m": _record()}
        outcome = model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: False, post_json=_fake_post_ok([]))
        self.assertEqual(outcome["used"], 0)
        self.assertEqual(outcome["stopped"], "cancelled")
        self.assertEqual(records["um|m"]["attempts"], 0)
        self.assertEqual(records["um|m"]["status"], "pending")

    def test_paused_source_is_skipped_with_paused_note(self):
        # M365 stays paused (Plan-Revision 7 step 4): zero requests, honest
        # 来源已暂停 note, record untouched.
        records = {"m365|m": _record(source="m365", model_id="m")}
        outcome = model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=_fake_post_ok([]))
        self.assertEqual(outcome["used"], 0)
        self.assertEqual(outcome["results"][0]["note"],
                         "该来源已暂停，跳过（零请求）")
        self.assertEqual(records["m365|m"]["status"], "pending")
        self.assertEqual(records["m365|m"]["attempts"], 0)

    def test_paused_source_stale_record_keeps_status_and_evidence(self):
        # 端点/凭据检查先于状态归一（F9）：暂停来源的 stale 记录零请求、
        # 零状态变更——status 保持 stale、evidence_revision 不变、history
        # 为空（旧缺陷先把 stale 翻成 pending，记录随后永远出不了队）。
        records = {"m365|m": _record(source="m365", model_id="m",
                                     status="stale", floor=16000)}
        records["m365|m"].update({"evidence_revision": 2,
                                  "stale_reason": "declared_context_changed"})
        outcome = model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=_fake_post_ok([]))
        self.assertEqual(outcome["used"], 0)
        row = outcome["results"][0]
        self.assertEqual(row["note"], "该来源已暂停，跳过（零请求）")
        self.assertEqual(row["status"], "stale")
        record = records["m365|m"]
        self.assertEqual(record["status"], "stale")
        self.assertEqual(record["evidence_revision"], 2)
        self.assertEqual(record["observed_floor"], 16000)
        self.assertNotIn("history", record)

    def test_unwired_source_is_skipped_honestly(self):
        # A source with no registered endpoint (not paused — just unwired) is
        # skipped with the honest zero-request note; the model is NOT deleted.
        records = {"gemini|m": _record(source="gemini", model_id="m")}
        outcome = model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=_fake_post_ok([]))
        self.assertEqual(outcome["used"], 0)
        self.assertEqual(outcome["results"][0]["note"],
                         "该来源未登记探测端点，跳过（零请求）")
        self.assertEqual(records["gemini|m"]["status"], "pending")

    def test_unreadable_credentials_skip_with_zero_requests(self):
        # A token provider that resolves to no token (unreadable credential
        # file, Plan-Revision 7 step 4): zero requests, honest note, record
        # untouched — no unauthenticated attempt, no fabricated success.
        for provider in (lambda: None, lambda: "", ""):
            with self.subTest(provider=provider):
                records = {"cliproxy|g": _record(source="cliproxy",
                                                 model_id="g")}
                outcome = model_sync.probe_context_queue(
                    {"cliproxy": ("http://127.0.0.1:1/v1/chat/completions",
                                  provider)},
                    {"max_requests": 10, "max_tokens": 64}, records,
                    lambda: True, post_json=_fake_post_ok([]))
                self.assertEqual(outcome["used"], 0)
                self.assertEqual(
                    outcome["results"][0]["note"],
                    "该来源凭据不可读（token 缺失），跳过（零请求）")
                self.assertEqual(records["cliproxy|g"]["status"], "pending")
                self.assertEqual(records["cliproxy|g"]["attempts"], 0)

    def test_endpoint_table_is_um_only(self):
        # B0 (2026-11-01): only the UM main chain has a registered local
        # chat route. Retired sources (Qoder CN / Qoder Global / CLIProxy)
        # have no endpoint entry and never enter the billing queue.
        self.assertEqual(set(model_sync.CONTEXT_PROBE_ENDPOINTS), {"um"})
        for retired in ("m365", "qoder_cn", "qoder_global", "cliproxy"):
            self.assertNotIn(retired, model_sync.CONTEXT_PROBE_ENDPOINTS)
        self.assertIs(model_sync.CONTEXT_PROBE_ENDPOINTS["um"][1], None)

    def test_token_provider_is_called_per_record(self):
        records = {"qoder_cn|tok": _record(source="qoder_cn", model_id="tok")}
        calls = []
        model_sync.probe_context_queue(
            {"qoder_cn": ("http://x", lambda: "tok-1")}, {"max_requests": 1,
                                                          "max_tokens": 64},
            records, lambda: True,
            post_json=lambda url, body, timeout=90, token=None:
            calls.append(token) or {"status": 503, "body": {}})
        self.assertEqual(calls, ["tok-1"])

    def test_non_eligible_statuses_are_untouched(self):
        records = {"um|v": _record(status="verified"),
                   "um|f": _record(status="failed")}
        outcome = model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=_fake_post_ok([]))
        self.assertEqual(outcome["used"], 0)
        self.assertEqual(outcome["results"], [])

    def test_stale_record_completing_ladder_becomes_verified(self):
        records = {"um|m": _record(status="stale")}
        model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=_fake_post_ok([]))
        self.assertEqual(records["um|m"]["status"], "verified")

    def test_grandfathered_stale_record_gets_a_revision_on_touch(self):
        # F14: 祖传 stale 记录（无 evidence_revision 字段、无证据）触碰后
        # revision=1，不再永远停在 None；随后照常从全表重测。
        records = {"um|m": _record(status="stale")}
        model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=_fake_post_ok([]))
        self.assertEqual(records["um|m"]["evidence_revision"], 1)
        self.assertEqual(records["um|m"]["status"], "verified")

    def test_garbage_record_values_do_not_crash_the_queue(self):
        # F13: 垃圾值记录级安全——evidence_revision/token_evidence 为字符串
        # 时不抛异常：非整数 revision 按缺省 0 处理（bump 后 1，垃圾不冒充
        # 3），token_evidence 比较前判类型（非 int 按 None，可被可信报告
        # 替换）；已计费证据（attempts/last_run/observed_floor）照常持久化。
        def post(url, body, timeout=90, token=None):
            content = body["messages"][0]["content"]
            marker = FINAL_ANCHOR.search(content).group(1)
            return {"status": 200, "body": {
                "choices": [{"message": {
                    "content": "[ctxprobe %s] FINAL anchor — quote this "
                               "line verbatim" % marker}}],
                "usage": {"prompt_tokens": 300}}}

        records = {"um|m": _record(status="stale", floor=64000)}
        records["um|m"].update({"evidence_revision": "3",
                                "token_evidence": "61234"})
        model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=post)
        record = records["um|m"]
        self.assertEqual(record["status"], "verified")
        # 旧 floor 属归档代次：触碰先归档（attempts/floor 归零），新代次
        # 从全表重测——4 步全跑，已计费证据照常持久化。
        self.assertEqual(record["attempts"], len(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(record["observed_floor"],
                         max(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(record["evidence_revision"], 1)
        self.assertEqual(record["token_evidence"], 300)
        self.assertIsNotNone(record["last_run"])

    def test_history_is_capped_at_probe_history_max(self):
        # F6: 归档历史上限（PROBE_HISTORY_MAX=20）：归档 21 次后 history
        # 长度恰为 20，最旧一代被挤出，最新一代仍在（revision 连续）。
        records = {"um|m": _record(status="verified", floor=256000,
                                   declared=128000)}
        records["um|m"].update({
            "attempts": 4, "last_run": "t0", "last_success": "t1",
            "token_evidence": 61234, "evidence_revision": 1,
            "scheme_revision": model_sync.PROBE_SCHEME_REVISION,
            "stale_reason": None, "history": []})
        for _ in range(21):
            model_sync.probe_context_queue(
                ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
                lambda: True, post_json=_fake_post_ok([]), retest=True)
        record = records["um|m"]
        self.assertEqual(len(record["history"]),
                         model_sync.PROBE_HISTORY_MAX)
        self.assertEqual(record["history"][0]["evidence_revision"], 2)
        self.assertEqual(record["history"][-1]["evidence_revision"], 21)
        self.assertEqual(record["evidence_revision"], 22)
        self.assertEqual(record["status"], "verified")


class TestProbeSelectionNormalization(unittest.TestCase):
    def test_paused_source_selection_is_rejected(self):
        # F10: PAUSED_SOURCES 在选择校验层明确拒绝（与 sync_apply 的 scope
        # 约定一致）：m365 不进入计费队列，选择它一律 ValueError，绝不
        # 静默放宽成全队列。
        for sources, models in ((["m365"], None),
                                (None, {"m365": ["gpt-x"]}),
                                (["um", "m365"], None)):
            with self.subTest(sources=sources, models=models):
                with self.assertRaises(ValueError):
                    model_sync.normalize_probe_selection(sources, models)

    def test_active_source_selection_still_accepted(self):
        # 已启用来源的选择照常通过；退役来源的选择被明确拒绝。
        self.assertEqual(model_sync.normalize_probe_selection(["um"], None),
                         {"um": None})
        for source in ("m365", "qoder_cn", "qoder_global", "cliproxy"):
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, "已暂停"):
                    model_sync.normalize_probe_selection(
                        ["um", source], {"um": ["m"]})
                with self.assertRaisesRegex(ValueError, "已暂停"):
                    model_sync.normalize_probe_selection(
                        None, {source: ["m"]})


class TestSeedContextProbes(unittest.TestCase):
    def _catalogs(self, context=128000):
        return {"um": [CatalogModel("um", "a", context=context),
                       CatalogModel("um", "b")]}

    def test_model_with_declared_context_joins_pending(self):
        state = empty_state()
        model_sync.seed_context_probes(self._catalogs(), state)
        record = state["contextProbes"]["um|a"]
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["declared_context"], 128000)
        self.assertEqual(record["observed_floor"], None)
        self.assertEqual(record["attempts"], 0)
        self.assertIs(record["declared_context_known"], True)
        self.assertIsNone(record["declared_note"])
        for field in ("source", "model_id", "declared_at", "last_run",
                      "last_error"):
            self.assertIn(field, record)
        # Plan-Revision 7 step 4: models WITHOUT a declared context also
        # join — explicitly marked declaration-unknown and still selectable
        # for a real measurement (never zero capacity, never char-derived).
        unknown = state["contextProbes"]["um|b"]
        self.assertIsNone(unknown["declared_context"])
        self.assertIs(unknown["declared_context_known"], False)
        self.assertEqual(unknown["declared_note"],
                         "声明未知，探测仍可测量实际可用长度")
        self.assertEqual(unknown["status"], "pending")

    def test_unknown_declaration_model_is_seeded_and_measurable(self):
        # 无声明上下文的模型可被明确选择：selection resolves it (it IS in
        # the queue) and the runner measures it like any pending record.
        state = empty_state()
        model_sync.seed_context_probes(self._catalogs(), state)
        records = state["contextProbes"]
        outcome = model_sync.probe_context_queue(
            ENDPOINTS, {"max_requests": 10, "max_tokens": 64}, records,
            lambda: True, post_json=_fake_post_ok([]),
            selection={"um": {"b"}})
        self.assertEqual(outcome["used"], len(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(records["um|b"]["status"], "verified")
        self.assertEqual(records["um|b"]["observed_floor"],
                         max(model_sync.PROBE_INPUT_STEPS))
        self.assertIs(records["um|b"]["declared_context_known"], False)
        # The declared sibling was not selected and stays untouched.
        self.assertEqual(records["um|a"]["status"], "pending")
        self.assertEqual(records["um|a"]["attempts"], 0)

    def test_declaration_appearing_or_vanishing_flips_stale(self):
        # Plan-Revision 7 第 3 步（修订后契约）：目录声明改变——含 None↔值
        # （出现或消失）——一律翻 stale：旧证据归档进 history、
        # evidence_revision +1。旧「出现/消失不推翻」的不对称规则已废：
        # 声明是探测方案的输入，声明变了旧证据就是旧代次的。
        state = empty_state()
        model_sync.seed_context_probes(self._catalogs(), state)
        record = state["contextProbes"]["um|b"]  # declared unknown
        record.update({"status": "verified", "observed_floor": 64000,
                       "attempts": 3, "token_evidence": 16384})
        both_declared = {"um": [CatalogModel("um", "a", context=128000),
                                CatalogModel("um", "b", context=128000)]}
        # None → 值：翻 stale，旧证据归档。
        model_sync.seed_context_probes(both_declared, state)
        self.assertEqual(record["status"], "stale")
        self.assertEqual(record["stale_reason"], "declared_context_changed")
        self.assertEqual(record["declared_context"], 128000)
        self.assertIs(record["declared_context_known"], True)
        self.assertIsNone(record["declared_note"])
        self.assertEqual(record["evidence_revision"], 2)
        self.assertIsNone(record["observed_floor"])
        history = record["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "verified")
        self.assertEqual(history[0]["observed_floor"], 64000)
        self.assertEqual(history[0]["token_evidence"], 16384)
        # 值 → None：同样翻 stale、归档、revision+1。
        record["status"] = "verified"
        record["observed_floor"] = 256000
        record["attempts"] = 4
        model_sync.seed_context_probes(self._catalogs(), state)
        self.assertEqual(record["status"], "stale")
        self.assertEqual(record["stale_reason"], "declared_context_changed")
        self.assertIsNone(record["declared_context"])
        self.assertIs(record["declared_context_known"], False)
        self.assertEqual(record["evidence_revision"], 3)
        self.assertEqual(len(record["history"]), 2)

    def test_declared_change_flips_to_stale(self):
        state = empty_state()
        model_sync.seed_context_probes(self._catalogs(context=1000), state)
        model_sync.seed_context_probes(self._catalogs(context=2000), state)
        record = state["contextProbes"]["um|a"]
        self.assertEqual(record["status"], "stale")
        self.assertEqual(record["declared_context"], 2000)

    def test_verified_record_is_reused_when_declaration_unchanged(self):
        state = empty_state()
        model_sync.seed_context_probes(self._catalogs(), state)
        state["contextProbes"]["um|a"].update(
            {"status": "verified", "observed_floor": 256000, "attempts": 4})
        model_sync.seed_context_probes(self._catalogs(), state)
        record = state["contextProbes"]["um|a"]
        self.assertEqual(record["status"], "verified")
        self.assertEqual(record["attempts"], 4)

    def test_invalid_catalog_seeds_nothing(self):
        state = empty_state()
        state["catalogCounts"] = {"um": 5}  # 2 << 5 → drop guard rejects
        model_sync.seed_context_probes(self._catalogs(), state)
        self.assertEqual(state["contextProbes"], {})

    def test_new_record_carries_evidence_contract_fields(self):
        # Plan-Revision 7 step 2: a fresh record starts at evidence_revision
        # 1 with NO measurement — token capacity unknown, nothing measured,
        # no stale reason — and carries the current scheme revision (step 3).
        state = empty_state()
        model_sync.seed_context_probes(self._catalogs(), state)
        record = state["contextProbes"]["um|a"]
        self.assertEqual(record["evidence_revision"], 1)
        self.assertIsNone(record["stale_reason"])
        self.assertIsNone(record["last_success"])
        self.assertIsNone(record["token_evidence"])
        self.assertIsNone(record["observed_floor"])
        self.assertEqual(record["scheme_revision"],
                         model_sync.PROBE_SCHEME_REVISION)
        self.assertEqual(record["history"], [])

    def test_scheme_change_marks_stale_and_archives_generation(self):
        # A probe-scheme bump (PROBE_SCHEME_REVISION) retires the old
        # generation exactly like a declared change: evidence archived,
        # revision bumped, stale_reason="probe_scheme_changed"; re-seeding
        # under the same constant does not bump again.
        state = empty_state()
        model_sync.seed_context_probes(self._catalogs(context=128000), state)
        state["contextProbes"]["um|a"].update(
            {"status": "verified", "observed_floor": 256000, "attempts": 4,
             "token_evidence": 61234})
        with mock.patch.object(model_sync, "PROBE_SCHEME_REVISION", 2):
            model_sync.seed_context_probes(
                self._catalogs(context=128000), state)
        record = state["contextProbes"]["um|a"]
        self.assertEqual(record["status"], "stale")
        self.assertEqual(record["stale_reason"], "probe_scheme_changed")
        self.assertEqual(record["evidence_revision"], 2)
        self.assertEqual(record["scheme_revision"], 2)
        self.assertIsNone(record["observed_floor"])
        history = record["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["observed_floor"], 256000)
        self.assertEqual(history[0]["reason"], "probe_scheme_changed")
        with mock.patch.object(model_sync, "PROBE_SCHEME_REVISION", 2):
            model_sync.seed_context_probes(
                self._catalogs(context=128000), state)
        self.assertEqual(record["evidence_revision"], 2)
        self.assertEqual(len(record["history"]), 1)

    def test_legacy_record_grandfathered_until_scheme_constant_moves(self):
        # Records seeded before scheme tracking existed were measured under
        # revision 1 (the scheme has not changed since the queue was
        # created): a missing scheme_revision is grandfathered and does NOT
        # flip the record — until the constant itself advances.
        state = empty_state()
        state["contextProbes"] = {"um|a": {
            "source": "um", "model_id": "a", "declared_context": 128000,
            "declared_at": "t", "status": "verified",
            "observed_floor": 256000, "attempts": 4, "last_run": "t",
            "last_error": None}}
        model_sync.seed_context_probes(self._catalogs(context=128000), state)
        self.assertEqual(state["contextProbes"]["um|a"]["status"], "verified")
        self.assertEqual(state["contextProbes"]["um|a"]["observed_floor"],
                         256000)
        # Once the scheme revision moves, a missing field no longer
        # grandfather: the record flips stale (probe_scheme_missing).
        with mock.patch.object(model_sync, "PROBE_SCHEME_REVISION", 2):
            model_sync.seed_context_probes(
                self._catalogs(context=128000), state)
        record = state["contextProbes"]["um|a"]
        self.assertEqual(record["status"], "stale")
        self.assertEqual(record["stale_reason"], "probe_scheme_missing")
        self.assertEqual(record["evidence_revision"], 1)
        self.assertEqual(record["scheme_revision"], 2)
        self.assertEqual(len(record["history"]), 1)

    def test_already_stale_declaration_change_refreshes_reason_only(self):
        # A record already stale (evidence-less after its flip) whose
        # declaration changes AGAIN: reason refreshed, no second revision
        # bump, no empty history entry.
        state = empty_state()
        model_sync.seed_context_probes(self._catalogs(context=1000), state)
        model_sync.seed_context_probes(self._catalogs(context=2000), state)
        record = state["contextProbes"]["um|a"]
        self.assertEqual(record["status"], "stale")
        self.assertEqual(record["evidence_revision"], 2)
        model_sync.seed_context_probes(self._catalogs(context=3000), state)
        self.assertEqual(record["status"], "stale")
        self.assertEqual(record["stale_reason"], "declared_context_changed")
        self.assertEqual(record["evidence_revision"], 2)
        self.assertEqual(record["declared_context"], 3000)
        self.assertEqual(record["history"], [])

    def test_garbage_revision_lands_its_first_revision(self):
        # 祖传记录的 evidence_revision 是垃圾值（非 None 但非 int）时，
        # 触碰后按缺省处理落第一个修订号，绝不把垃圾字符串当数字沿用。
        state = empty_state()
        state["contextProbes"] = {"um|a": {
            "source": "um", "model_id": "a", "declared_context": 1000,
            "declared_at": "t", "status": "stale",
            "observed_floor": None, "attempts": 0, "last_run": None,
            "last_error": None, "evidence_revision": "garbage"}}
        model_sync.seed_context_probes(self._catalogs(context=2000), state)
        record = state["contextProbes"]["um|a"]
        self.assertEqual(record["evidence_revision"], 1)
        self.assertEqual(record["status"], "stale")

    def test_probe_history_is_capped(self):
        # history 无上限会让 state 文件无界增长：归档超过上限后只保留
        # 最近的 PROBE_HISTORY_MAX 条。
        record = {"source": "um", "model_id": "h", "declared_context": 1,
                  "status": "pending", "observed_floor": 1, "attempts": 1,
                  "evidence_revision": 1}
        for _ in range(model_sync.PROBE_HISTORY_MAX + 5):
            record.update({"observed_floor": 1, "attempts": 1})
            model_sync._archive_probe_generation(record, reason="x")
        self.assertEqual(len(record["history"]), model_sync.PROBE_HISTORY_MAX)
        self.assertEqual(record["history"][-1]["reason"], "x")

    def test_declared_change_archives_generation_and_flips_stale(self):
        # Plan-Revision 7 step 3: a declared-context change retires the old
        # generation — the evidence moves into ``history`` (preserved with
        # its true revision label, never deleted) and the current fields
        # reset, so no old floor can ever be reused as current evidence.
        state = empty_state()
        model_sync.seed_context_probes(self._catalogs(context=1000), state)
        # Historical evidence from a completed measurement of revision 1.
        state["contextProbes"]["um|a"].update(
            {"status": "verified", "observed_floor": 256000,
             "observed_ceiling": None, "token_evidence": 61234,
             "attempts": 4})
        model_sync.seed_context_probes(self._catalogs(context=2000), state)
        record = state["contextProbes"]["um|a"]
        self.assertEqual(record["status"], "stale")
        self.assertEqual(record["stale_reason"], "declared_context_changed")
        self.assertEqual(record["evidence_revision"], 2)
        self.assertEqual(record["declared_context"], 2000)
        self.assertEqual(record["scheme_revision"],
                         model_sync.PROBE_SCHEME_REVISION)
        # Current generation starts empty (independent evidence); the old
        # generation lives in history, labelled with its own revision.
        self.assertIsNone(record["observed_floor"])
        self.assertIsNone(record["token_evidence"])
        self.assertEqual(record["attempts"], 0)
        history = record["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["observed_floor"], 256000)
        self.assertEqual(history[0]["token_evidence"], 61234)
        self.assertEqual(history[0]["attempts"], 4)
        self.assertEqual(history[0]["status"], "verified")
        self.assertEqual(history[0]["evidence_revision"], 1)
        self.assertEqual(history[0]["reason"], "declared_context_changed")
        self.assertIsNotNone(history[0]["archived_at"])

    def test_old_record_missing_new_fields_stays_readable(self):
        # Additive evolution: an old-state record carrying only the original
        # fields is left untouched while its declaration is unchanged AND no
        # scheme mismatch exists (a missing scheme_revision reads as revision
        # 1 — the tracking was introduced with the scheme unchanged).  When
        # the declaration actually changes, the flip archives the old
        # evidence into history (first observed bump counts from 0 — no
        # fabricated history) and stamps the current scheme revision.
        old_record = {"source": "um", "model_id": "a",
                      "declared_context": 128000, "declared_at": "t",
                      "status": "verified", "observed_floor": 256000,
                      "attempts": 4, "last_run": "t", "last_error": None}
        state = empty_state()
        state["contextProbes"] = {"um|a": dict(old_record)}
        model_sync.seed_context_probes(self._catalogs(context=128000), state)
        self.assertEqual(state["contextProbes"]["um|a"], old_record)
        model_sync.seed_context_probes(self._catalogs(context=64000), state)
        record = state["contextProbes"]["um|a"]
        self.assertEqual(record["status"], "stale")
        self.assertEqual(record["stale_reason"], "declared_context_changed")
        self.assertEqual(record["evidence_revision"], 1)
        self.assertEqual(record["scheme_revision"],
                         model_sync.PROBE_SCHEME_REVISION)
        self.assertIsNone(record["observed_floor"])
        history = record["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["observed_floor"], 256000)
        self.assertEqual(history[0]["attempts"], 4)
        self.assertIsNone(history[0]["evidence_revision"])
        self.assertEqual(history[0]["status"], "verified")

    def test_model_filter_limits_seeding(self):
        state = empty_state()
        model_sync.seed_context_probes(self._catalogs(), state,
                                       model_filter={"um": {"a"}})
        self.assertEqual(set(state["contextProbes"]), {"um|a"})

    def test_grandfathered_stale_seed_bumps_missing_revision(self):
        # F14（seed 侧）：祖传 stale（无 evidence_revision 字段、无证据）遇
        # 声明变化触碰后 revision=1，不再永远停在 None。
        state = empty_state()
        state["contextProbes"] = {"um|a": {
            "source": "um", "model_id": "a", "declared_context": 1000,
            "declared_at": "t", "status": "stale",
            "observed_floor": None, "attempts": 0, "last_run": None,
            "last_error": None}}
        model_sync.seed_context_probes(self._catalogs(context=2000), state)
        record = state["contextProbes"]["um|a"]
        self.assertEqual(record["evidence_revision"], 1)
        self.assertEqual(record["status"], "stale")
        self.assertEqual(record["declared_context"], 2000)
        # 无证据可归档：不凭空造 history（缺席=没有归档代次，additive）。
        self.assertNotIn("history", record)

    def test_garbage_revision_does_not_crash_seeding(self):
        # F13（seed 侧）：evidence_revision/token_evidence 为垃圾值（字符串）
        # 时播种不抛异常：非整数 revision 按缺省 0 处理（bump 后 1，垃圾不
        # 冒充 3）；垃圾值原样进入归档历史，绝不被解释成测量。
        state = empty_state()
        state["contextProbes"] = {"um|a": {
            "source": "um", "model_id": "a", "declared_context": 1000,
            "declared_at": "t", "status": "verified",
            "observed_floor": 64000, "attempts": 2, "last_run": "t",
            "last_error": None, "evidence_revision": "3",
            "token_evidence": "61234"}}
        model_sync.seed_context_probes(self._catalogs(context=2000), state)
        record = state["contextProbes"]["um|a"]
        self.assertEqual(record["status"], "stale")
        self.assertEqual(record["evidence_revision"], 1)
        self.assertEqual(record["history"][0]["evidence_revision"], "3")
        self.assertEqual(record["history"][0]["token_evidence"], "61234")


class TestRunSyncSeedsQueueIntoNextState(unittest.TestCase):
    def test_apply_writes_seeded_queue_into_state_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalogs = {"um": [CatalogModel("um", "a", context=128000)]}
            consumers = {"um|DSH|openwebui": {"providerName": "UM",
                                              "models": {}}}
            dsh_raw = ("llm-pi-ai:\n  providers:\n    openwebui:\n"
                       "      displayName: UM\n      models:\n")
            with mock.patch.object(
                    model_sync, "collect_catalogs",
                    return_value=(catalogs, [])), \
                mock.patch.object(
                    model_sync, "collect_consumers",
                    return_value=(consumers, dsh_raw, "{}")), \
                mock.patch.object(model_sync, "DSH_YAML",
                                  root / "settings.yaml"), \
                mock.patch.object(model_sync, "ZCODE_PROVIDER_CONFIG",
                                  root / "provider_config.json"), \
                mock.patch.object(model_sync, "STATE_FILE",
                                  root / "state.json"), \
                mock.patch.object(model_sync, "RESULT_FILE",
                                  root / "result.json"), \
                mock.patch.object(model_sync, "HISTORY_FILE",
                                  root / "history.jsonl"), \
                mock.patch.object(model_sync, "BACKUP_DIR",
                                  root / "backups"), \
                mock.patch.object(model_sync, "zcode_running",
                                  return_value=False):
                (root / "settings.yaml").write_text(dsh_raw, encoding="utf-8")
                model_sync.run_sync(apply=True, run_probes=False,
                                    confirm=False)
            state = json.loads((root / "state.json").read_text("utf-8"))
        self.assertEqual(state["contextProbes"]["um|a"]["status"], "pending")
        self.assertEqual(state["contextProbes"]["um|a"]["declared_context"],
                         128000)
        self.assertEqual(
            state["contextProbes"]["um|a"]["evidence_revision"], 1)


class _Ctx:
    """OperationContext stand-in with the real public cancel/report surface."""

    def __init__(self, cancelled=False, op_id=None):
        self.cancel_event = threading.Event()
        if cancelled:
            self.cancel_event.set()
        self.op_id = op_id
        self.steps = []

    def raise_if_cancelled(self):
        if self.cancel_event.is_set():
            raise console_operations.OperationCancelled()

    def report_step(self, note):
        self.steps.append(note)


def _operation_ctx():
    """A real OperationContext: the production run entry (operation layer)."""
    return console_operations.OperationContext(
        "op-test", threading.Event(), lambda note: None)


@contextlib.contextmanager
def _temp_probe_env(state=None):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        data = root / "data"
        data.mkdir()
        state_file = data / "state.json"
        if state is not None:
            state_file.write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8")
        with contextlib.ExitStack() as stack:
            for name, value in {
                    "DATA_DIR": data,
                    "STATE_FILE": state_file,
                    "RESULT_FILE": data / "result.json",
                    "PREVIEW_FILE": data / "preview.json",
                    "HISTORY_FILE": data / "history.jsonl",
                    "BACKUP_DIR": data / "backups",
                    "LOCK_FILE": data / "model-sync.lock"}.items():
                stack.enter_context(mock.patch.object(model_sync, name, value))
            yield {"root": root, "data": data, "state": state_file}


class _CatalogHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append({
            "path": self.path, "headers": dict(self.headers)})
        self.send_response(self.server.status)
        for key, value in self.server.response_headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(self.server.body)

    def log_message(self, *args):
        pass


class _FakeCatalogServer:
    """Terminal fake UM: real HTTP over loopback, no real endpoint."""

    def __init__(self, status=200, body=b'{"data": []}', headers=None):
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _CatalogHandler)
        self.server.status = status
        self.server.body = body
        self.server.response_headers = dict(headers or {})
        self.server.requests = []
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    @property
    def port(self):
        return self.server.server_address[1]

    @property
    def requests(self):
        return self.server.requests


@contextlib.contextmanager
def _um_endpoint(server):
    url = "http://127.0.0.1:%d/v1/models" % server.port
    with mock.patch.object(model_sync, "UM_MODELS_URL", url), \
         mock.patch.object(model_sync, "UM_MODELS_HOST", "127.0.0.1"), \
         mock.patch.object(model_sync, "UM_MODELS_PORT", server.port), \
         mock.patch.object(model_sync, "UM_MODELS_PATH", "/v1/models"), \
         mock.patch.object(
             model_sync, "_CATALOG_ENDPOINTS",
             frozenset(set(model_sync._CATALOG_ENDPOINTS) | {url})):
        yield


def _um_catalog_body(*model_ids):
    return json.dumps({"data": [
        {"id": model_id, "max_model_len": 128000}
        for model_id in model_ids]}).encode("utf-8")


def _pending_state(*model_ids, declared=128000):
    state = empty_state()
    state["contextProbes"] = {
        "um|%s" % model_id: {
            "source": "um", "model_id": model_id,
            "declared_context": declared, "declared_at": "t",
            "status": "pending", "observed_floor": None,
            "attempts": 0, "last_run": None, "last_error": None,
        } for model_id in model_ids}
    return state


def _fake_probe_post(calls, usage=100):
    def post(url, body, timeout=90, token=None):
        content = body["messages"][0]["content"]
        marker = FINAL_ANCHOR.search(content).group(1)
        calls.append({
            "model": body["model"], "max_tokens": body["max_tokens"],
            "chars": len(content)})
        return {"status": 200, "body": {
            "choices": [{"message": {
                "content": "[ctxprobe %s] FINAL anchor — quote this line "
                           "verbatim" % marker}}],
            "usage": {"prompt_tokens": usage}}}

    return post


def _probe_spec():
    return console_operations.ActionSpec(
        "model.context_probe_run", "test probe run",
        handler=console_model_actions.model_context_probe_run,
        resource_fn=lambda target, params: "model:sync",
        param_schema={
            "max_requests": {"type": "int", "min": 1, "max": 20},
            "max_tokens": {"type": "int", "min": 1, "max": 64},
            "sources": {"type": "str", "max_len": 512},
            "models": {"type": "object"},
            "retest": {"type": "bool"},
        }, enabled=True)


def _wait_operation(manager, op_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        operation = manager.get_operation(op_id)
        if operation and operation["state"] in (
                "succeeded", "failed", "cancelled", "interrupted"):
            return operation
        time.sleep(0.01)
    raise AssertionError("operation did not finish")


class TestIndependentProbeRun(unittest.TestCase):
    """B3: explicit UM probe runs without the old run_sync/apply path."""

    def test_new_model_can_be_selected_without_old_run_sync(self):
        calls = []
        with _FakeCatalogServer(body=_um_catalog_body("new")) as server, \
             _um_endpoint(server), _temp_probe_env() as paths, \
             mock.patch.object(model_sync, "_post_json",
                               _fake_probe_post(calls)), \
             mock.patch.object(
                 model_sync, "run_sync",
                 side_effect=AssertionError("old sync must not run")) as run_sync, \
             mock.patch.object(
                 model_sync, "collect_consumers",
                 side_effect=AssertionError("client config must not be read")):
            result = console_model_actions.model_context_probe_run(
                "", {"sources": ["um"], "models": {"um": ["new"]}}, _Ctx())
            after = json.loads(paths["state"].read_text("utf-8"))
        run_sync.assert_not_called()
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(result["requests_used"],
                         len(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(result["selection"], {"um": ["new"]})
        self.assertFalse(result["retest"])
        self.assertEqual({call["model"] for call in calls}, {"new"})
        record = after["contextProbes"]["um|new"]
        self.assertEqual(record["status"], "verified")
        self.assertEqual(record["declared_context"], 128000)
        self.assertEqual(record["token_evidence"], 100)

    def test_explicit_multi_select_keeps_unselected_at_zero_requests(self):
        calls = []
        state = _pending_state("a", "b", "c")
        with _temp_probe_env(state=state) as paths, \
             mock.patch.object(model_sync, "_post_json",
                               _fake_probe_post(calls)):
            result = console_model_actions.model_context_probe_run(
                "", {"sources": ["um"], "models": {"um": ["a", "b"]}}, _Ctx())
            after = json.loads(paths["state"].read_text("utf-8"))
        self.assertEqual({call["model"] for call in calls}, {"a", "b"})
        self.assertEqual(len(calls), 2 * len(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(result["requests_used"], len(calls))
        self.assertEqual(after["contextProbes"]["um|a"]["status"], "verified")
        self.assertEqual(after["contextProbes"]["um|b"]["status"], "verified")
        self.assertEqual(after["contextProbes"]["um|c"]["status"], "pending")
        self.assertEqual(after["contextProbes"]["um|c"]["attempts"], 0)
        self.assertIsNone(after["contextProbes"]["um|c"]["observed_floor"])

    def test_explicit_selection_of_tested_model_auto_retests_and_archives(self):
        state = empty_state()
        state["contextProbes"] = {"um|done": {
            "source": "um", "model_id": "done",
            "declared_context": 128000, "declared_at": "t",
            "status": "verified", "observed_floor": 256000,
            "attempts": 4, "last_run": "t0", "last_success": "t1",
            "token_evidence": 61234, "evidence_revision": 2,
            "scheme_revision": model_sync.PROBE_SCHEME_REVISION,
            "stale_reason": None, "history": []}}
        calls = []
        with _temp_probe_env(state=state) as paths, \
             mock.patch.object(model_sync, "_post_json",
                               _fake_probe_post(calls)):
            result = console_model_actions.model_context_probe_run(
                "", {"sources": ["um"], "models": {"um": ["done"]}}, _Ctx())
            after = json.loads(paths["state"].read_text("utf-8"))
        self.assertTrue(result["retest"])
        self.assertEqual(len(calls), len(model_sync.PROBE_INPUT_STEPS))
        record = after["contextProbes"]["um|done"]
        self.assertEqual(record["status"], "verified")
        self.assertEqual(record["evidence_revision"], 3)
        self.assertEqual(len(record["history"]), 1)
        self.assertEqual(record["history"][0]["reason"], "retest")
        self.assertEqual(record["history"][0]["observed_floor"], 256000)

    def test_retired_or_invalid_model_is_rejected_without_requests(self):
        calls = []
        with _FakeCatalogServer(body=_um_catalog_body("live")) as server, \
             _um_endpoint(server), _temp_probe_env() as paths, \
             mock.patch.object(model_sync, "_post_json",
                               _fake_probe_post(calls)):
            with self.assertRaises(OperationError) as caught:
                console_model_actions.model_context_probe_run(
                    "", {"sources": ["um"], "models": {"um": ["retired"]}},
                    _operation_ctx())
            self.assertEqual("invalid_params", caught.exception.code)
            self.assertIn("退役或非法", caught.exception.message)
            self.assertFalse(paths["state"].exists())
        self.assertEqual(calls, [])
        self.assertEqual(len(server.requests), 1)

    def test_queued_retired_model_is_rejected_without_requests(self):
        # S2 high #6: a model already in the probe queue but removed from the
        # CURRENT UM catalog must not be billed.  The operation layer (the
        # production run entry) validates queued models against the same live
        # read-only catalog and rejects the selection with zero requests.
        calls = []
        with _FakeCatalogServer(body=_um_catalog_body("live")) as server, \
             _um_endpoint(server), \
             _temp_probe_env(state=_pending_state("retired")) as paths:
            before = paths["state"].read_bytes()
            with self.assertRaises(OperationError) as caught:
                console_model_actions.model_context_probe_run(
                    "", {"sources": ["um"], "models": {"um": ["retired"]}},
                    _operation_ctx())
            self.assertEqual("invalid_params", caught.exception.code)
            self.assertIn("退役或非法", caught.exception.message)
            self.assertEqual(paths["state"].read_bytes(), before)
        self.assertEqual(calls, [])
        self.assertEqual(len(server.requests), 1)

    def test_catalog_read_failure_is_not_retirement(self):
        # S2 high #6: a catalog READ FAILURE is separate from retirement —
        # catalog_failed, never invalid_params/"退役或非法", and zero requests
        # because the selection could not be confirmed.  A 200 non-JSON
        # login/error page is still an unreadable catalog (not a valid empty
        # one), exercising collect_catalogs' own failure branch.
        calls = []
        with _FakeCatalogServer(
                body=b'<html>login</html>') as server, \
             _um_endpoint(server), \
             _temp_probe_env(state=_pending_state("a")) as paths:
            before = paths["state"].read_bytes()
            with self.assertRaises(OperationError) as caught:
                console_model_actions.model_context_probe_run(
                    "", {"sources": ["um"], "models": {"um": ["a"]}},
                    _operation_ctx())
            self.assertEqual("catalog_failed", caught.exception.code)
            self.assertIn("不是退役", caught.exception.message)
            self.assertNotIn("退役或非法", caught.exception.message)
            self.assertEqual(paths["state"].read_bytes(), before)
        self.assertEqual(calls, [])
        self.assertEqual(len(server.requests), 1)

    def test_queued_live_model_is_confirmed_and_probed(self):
        # The new catalog check must not block a live queued model: it is
        # confirmed against the catalog and the normal budget/ladder runs.
        calls = []
        with _FakeCatalogServer(body=_um_catalog_body("a")) as server, \
             _um_endpoint(server), \
             _temp_probe_env(state=_pending_state("a")) as paths, \
             mock.patch.object(model_sync, "_post_json",
                               _fake_probe_post(calls)):
            result = console_model_actions.model_context_probe_run(
                "", {"sources": ["um"], "models": {"um": ["a"]}},
                _operation_ctx())
            after = json.loads(paths["state"].read_text("utf-8"))
        self.assertEqual(result["requests_used"],
                         len(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(len(calls), len(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(after["contextProbes"]["um|a"]["status"], "verified")
        self.assertEqual(len(server.requests), 1)

    def test_batch_budget_is_cumulative_and_remaining_models_are_not_executed(self):
        calls = []
        state = _pending_state("a", "b", "c", "d", "e", "f")
        with _temp_probe_env(state=state) as paths, \
             mock.patch.object(model_sync, "_post_json",
                               _fake_probe_post(calls)):
            result = console_model_actions.model_context_probe_run(
                "", {"sources": ["um"],
                     "models": {"um": ["a", "b", "c", "d", "e", "f"]},
                     "max_requests": 20, "max_tokens": 64}, _Ctx())
            after = json.loads(paths["state"].read_text("utf-8"))
        self.assertEqual(len(calls), 20)
        self.assertEqual(result["requests_used"], 20)
        self.assertEqual(result["budget"]["requests_used"], 20)
        self.assertEqual(result["budget"]["remaining_requests"], 0)
        self.assertTrue(all(call["max_tokens"] == 64 for call in calls))
        for model_id in ("a", "b", "c", "d", "e"):
            self.assertEqual(
                after["contextProbes"]["um|%s" % model_id]["status"],
                "verified")
        pending = after["contextProbes"]["um|f"]
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["attempts"], 0)
        self.assertEqual([row["key"] for row in result["not_executed"]],
                         ["um|f"])
        self.assertIn("预算耗尽", result["not_executed"][0]["note"])
        self.assertEqual(result["result_total"], 6)
        self.assertFalse(result["results_truncated"])

    def test_cancel_stops_next_request_and_keeps_completed_evidence(self):
        ctx = _Ctx()
        calls = []

        def post(url, body, timeout=90, token=None):
            calls.append(body["model"])
            ctx.cancel_event.set()
            content = body["messages"][0]["content"]
            marker = FINAL_ANCHOR.search(content).group(1)
            return {"status": 200, "body": {"choices": [{"message": {
                "content": "[ctxprobe %s] FINAL anchor — quote this line "
                           "verbatim" % marker}}]}}

        with _temp_probe_env(state=_pending_state("a")) as paths, \
             mock.patch.object(model_sync, "_post_json", post):
            result = console_model_actions.model_context_probe_run(
                "", {"sources": ["um"], "models": {"um": ["a"]}}, ctx)
            after = json.loads(paths["state"].read_text("utf-8"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["stopped"], "cancelled")
        self.assertEqual(result["requests_used"], 1)
        self.assertEqual(after["contextProbes"]["um|a"]["attempts"], 1)
        self.assertEqual(after["contextProbes"]["um|a"]["status"], "pending")
        self.assertEqual(after["contextProbes"]["um|a"]["observed_floor"], 4000)

    def test_transient_statuses_do_not_form_capacity_ceiling(self):
        for status in (401, 429, 500):
            with self.subTest(status=status):
                with _temp_probe_env(state=_pending_state("a")) as paths, \
                     mock.patch.object(
                         model_sync, "_post_json",
                         return_value={"status": status,
                                        "body": {"error": "temporary"}}):
                    console_model_actions.model_context_probe_run(
                        "", {"sources": ["um"], "models": {"um": ["a"]}},
                        _Ctx())
                    after = json.loads(paths["state"].read_text("utf-8"))
                record = after["contextProbes"]["um|a"]
                self.assertEqual(record["status"], "pending")
                self.assertIsNone(record.get("observed_ceiling"))
                self.assertIsNone(record["observed_floor"])
        with _temp_probe_env(state=_pending_state("a")) as paths, \
             mock.patch.object(
                 model_sync, "_post_json",
                 side_effect=TimeoutError("read timed out")):
            console_model_actions.model_context_probe_run(
                "", {"sources": ["um"], "models": {"um": ["a"]}}, _Ctx())
            after = json.loads(paths["state"].read_text("utf-8"))
        record = after["contextProbes"]["um|a"]
        self.assertEqual(record["status"], "pending")
        self.assertIsNone(record.get("observed_ceiling"))

    def test_progress_and_result_surfaces_are_bounded(self):
        ctx = _Ctx()
        calls = []
        state = _pending_state("a", "b")
        with _temp_probe_env(state=state), \
             mock.patch.object(model_sync, "_post_json",
                               _fake_probe_post(calls)):
            result = console_model_actions.model_context_probe_run(
                "", {"sources": ["um"], "models": {"um": ["a", "b"]}}, ctx)
        self.assertLessEqual(len(ctx.steps), _PROGRESS_STEP_LIMIT)
        self.assertEqual(result["result_total"], 2)
        self.assertFalse(result["results_truncated"])
        self.assertEqual(set(result["executed_models"]), {"um|a", "um|b"})
        self.assertEqual(result["not_executed"], [])
        self.assertEqual(result["budget"]["max_requests"], 20)
        self.assertEqual(result["budget"]["requests_used"], 8)

    def test_state_save_failure_preserves_billed_evidence(self):
        calls = []
        state = _pending_state("a")
        with _temp_probe_env(state=state) as paths, \
             mock.patch.object(model_sync, "_post_json",
                               _fake_probe_post(calls)), \
             mock.patch.object(
                 model_sync, "apply_config_transaction",
                 side_effect=RuntimeError("disk full")):
            with self.assertRaises(OperationError) as caught:
                console_model_actions.model_context_probe_run(
                    "", {"sources": ["um"], "models": {"um": ["a"]}},
                    _Ctx())
            self.assertEqual("probe_failed", caught.exception.code)
            self.assertIn("恢复文件", caught.exception.message)
            recovery = json.loads(
                (paths["data"] / "model-sync-probe-recovery.json")
                .read_text("utf-8"))
        self.assertEqual(recovery["persistence"], "failed")
        self.assertEqual(recovery["requests_used"],
                         len(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(recovery["records"]["um|a"]["status"], "verified")
        self.assertEqual(recovery["records"]["um|a"]["attempts"],
                         len(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(len(calls), len(model_sync.PROBE_INPUT_STEPS))

    def test_previous_recovery_evidence_is_merged_and_cleared(self):
        with _temp_probe_env() as paths:
            state = _pending_state("a")
            recovered = dict(state["contextProbes"]["um|a"])
            recovered.update({"status": "verified", "observed_floor": 4000,
                              "attempts": 1, "last_run": "t1"})
            model_sync.write_probe_recovery({
                "persistence": "failed",
                "records": {"um|a": recovered}})
            model_sync.merge_probe_recovery(state)
            self.assertEqual(state["contextProbes"]["um|a"]["status"],
                             "verified")
            self.assertEqual(state["contextProbes"]["um|a"]["last_run"], "t1")
            model_sync.clear_probe_recovery()
            self.assertIsNone(model_sync.read_probe_recovery())
            self.assertFalse(
                (paths["data"] / "model-sync-probe-recovery.json").exists())

    def test_catalog_refresh_stays_read_only(self):
        with _FakeCatalogServer(
                body=_um_catalog_body("m1"),
                headers={"X-Chat2Api-Raw-Models": "1"}) as server, \
             _um_endpoint(server), _temp_probe_env() as paths:
            result = console_model_actions.model_catalog(
                "", {"sources": ["um"], "fresh": True}, _Ctx())
            self.assertFalse(paths["state"].exists())
        self.assertTrue(result["success"])
        self.assertTrue(result["fresh"])
        self.assertEqual(result["catalogs"]["um"][0]["id"], "m1")


class TestProbeOperationLayer(unittest.TestCase):
    """B3: task id / idempotency / cancel / reconnect / restart."""

    def test_operation_entry_rejects_missing_or_sources_only_selection(self):
        with _temp_probe_env(state=_pending_state("a")) as paths:
            before = paths["state"].read_bytes()
            manager = console_operations.OperationManager(
                str(paths["root"] / "ops.db"), [_probe_spec()],
                str(paths["root"] / "locks"))
            try:
                for params in ({}, {"sources": '["um"]'}):
                    with self.subTest(params=params):
                        view = manager.submit(
                            "model.context_probe_run", "model", params)
                        done = _wait_operation(manager, view["id"])
                        self.assertEqual(done["state"], "failed")
                        self.assertEqual(done["error"]["code"],
                                         "invalid_params")
                        self.assertEqual(paths["state"].read_bytes(), before)
            finally:
                manager.close()

    def test_same_idempotency_key_returns_original_task_without_replay(self):
        calls = []
        with _FakeCatalogServer(
                body=_um_catalog_body("a")) as server, \
             _um_endpoint(server), \
             _temp_probe_env(state=_pending_state("a")) as paths:
            manager = console_operations.OperationManager(
                str(paths["root"] / "ops.db"), [_probe_spec()],
                str(paths["root"] / "locks"))
            try:
                with mock.patch.object(model_sync, "_post_json",
                                   _fake_probe_post(calls)):
                    first = manager.submit(
                        "model.context_probe_run", "model",
                        {"sources": '["um"]', "models": {"um": ["a"]}},
                        idempotency_key="probe-a")
                    second = manager.submit(
                        "model.context_probe_run", "model",
                        {"sources": '["um"]', "models": {"um": ["a"]}},
                        idempotency_key="probe-a")
                    done = _wait_operation(manager, first["id"])
                    reread = manager.get_operation(first["id"])
                self.assertEqual(second["id"], first["id"])
                self.assertTrue(second["deduplicated"])
                self.assertEqual(done["state"], "succeeded")
                self.assertEqual(done["result"]["requests_used"],
                                 len(model_sync.PROBE_INPUT_STEPS))
                self.assertEqual(reread["result"], done["result"])
                self.assertEqual(len(calls), len(model_sync.PROBE_INPUT_STEPS))
            finally:
                manager.close()

    def test_concurrent_runs_serialize_and_both_keep_evidence(self):
        calls = []
        with _FakeCatalogServer(
                body=_um_catalog_body("a", "b")) as server, \
             _um_endpoint(server), \
             _temp_probe_env(state=_pending_state("a", "b")) as paths:
            manager = console_operations.OperationManager(
                str(paths["root"] / "ops.db"), [_probe_spec()],
                str(paths["root"] / "locks"))
            try:
                with mock.patch.object(model_sync, "_post_json",
                                   _fake_probe_post(calls)):
                    first = manager.submit(
                        "model.context_probe_run", "model",
                        {"sources": '["um"]', "models": {"um": ["a"]}})
                    second = manager.submit(
                        "model.context_probe_run", "model",
                        {"sources": '["um"]', "models": {"um": ["b"]}})
                    first_done = _wait_operation(manager, first["id"])
                    second_done = _wait_operation(manager, second["id"])
                    after = json.loads(paths["state"].read_text("utf-8"))
                self.assertEqual(first_done["state"], "succeeded")
                self.assertEqual(second_done["state"], "succeeded")
                self.assertEqual(
                    len(calls), 2 * len(model_sync.PROBE_INPUT_STEPS))
                self.assertEqual(
                    after["contextProbes"]["um|a"]["status"], "verified")
                self.assertEqual(
                    after["contextProbes"]["um|b"]["status"], "verified")
            finally:
                manager.close()

    def test_restart_marks_operation_interrupted_and_discards_late_result(self):
        started = threading.Event()
        release = threading.Event()

        def blocking_post(url, body, timeout=90, token=None):
            started.set()
            release.wait(5)
            content = body["messages"][0]["content"]
            marker = FINAL_ANCHOR.search(content).group(1)
            return {"status": 200, "body": {"choices": [{"message": {
                "content": "[ctxprobe %s] FINAL anchor — quote this line "
                           "verbatim" % marker}}]}}

        with _FakeCatalogServer(
                body=_um_catalog_body("a")) as server, \
             _um_endpoint(server), \
             _temp_probe_env(state=_pending_state("a")) as paths:
            first = console_operations.OperationManager(
                str(paths["root"] / "ops.db"), [_probe_spec()],
                str(paths["root"] / "locks"))
            second = None
            try:
                with mock.patch.object(model_sync, "_post_json",
                                   blocking_post):
                    submitted = first.submit(
                        "model.context_probe_run", "model",
                        {"sources": '["um"]', "models": {"um": ["a"]}})
                    self.assertTrue(started.wait(5))
                    # The second manager starts AFTER the operation is
                    # running: startup marks the unfinished row interrupted.
                    second = console_operations.OperationManager(
                        str(paths["root"] / "ops.db"), [_probe_spec()],
                        str(paths["root"] / "locks"))
                    interrupted = second.get_operation(submitted["id"])
                    self.assertEqual(interrupted["state"], "interrupted")
                    release.set()
                    time.sleep(0.2)
                    still = second.get_operation(submitted["id"])
                    self.assertEqual(still["state"], "interrupted")
            finally:
                release.set()
                first.close()
                if second is not None:
                    second.close()


_PROGRESS_STEP_LIMIT = console_model_actions._PROBE_PROGRESS_MAX + 1


if __name__ == "__main__":
    unittest.main()
