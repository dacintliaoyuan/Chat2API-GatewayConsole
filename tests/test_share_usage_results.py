# -*- coding: utf-8 -*-
"""ShareUsage result counts (D18-2 / C8).

The overview chart needs request counts split by real outcome.  ``unknown`` is a
record whose outcome was never recorded; it is NEVER folded into ``success``.
The counts use the same filters as every other field in the payload.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import share_metering  # noqa: E402


BASE = {
    "time": "2026-09-25T10:00:00+08:00",
    "service": "chat2api",
    "model": "fixture-model",
    "input_tokens": 10,
    "output_tokens": 5,
    "total_tokens": 15,
    "usage_source": "upstream",
    "key_id": "key-a",
    "request_id": "r1",
    "attribution": "verified",
}


def record(**overrides):
    return {**BASE, **overrides}


class ResultCountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="share-usage-results-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = os.path.join(self.tmp.name, "usage-share-backend.jsonl")

    def write(self, records):
        with open(self.ledger, "w", encoding="utf-8") as handle:
            for item in records:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    def read(self, **filters):
        return share_metering.read_share_usage(
            self.ledger, now=1790000000, **filters)

    def test_unknown_is_never_folded_into_success(self):
        self.write([
            record(status="ok", request_id="ok-1"),
            record(status="ok", request_id="ok-2"),
            record(status="upstream_error", request_id="failed-1"),
            record(status="timeout", request_id="failed-2"),
            record(status="client_disconnected", request_id="cancelled-1"),
            record(status="cancelled", request_id="cancelled-2"),
            {key: value for key, value in record(request_id="unknown-1").items()
             if key != "status"},
        ])
        payload = self.read()
        self.assertEqual(payload["results"], {
            "success": 2, "failed": 2, "cancelled": 2, "unknown": 1,
            "total": 7, "coverage": {"unknownResult": 1},
        })
        self.assertEqual(payload["requests"], 7)
        # The per-key buckets keep the same real outcome counts.
        key = payload["keys"][0]
        self.assertEqual(key["requests"], 7)

    def test_empty_ledger_is_a_real_zero_not_missing(self):
        self.write([])
        payload = self.read()
        self.assertEqual(payload["results"], {
            "success": 0, "failed": 0, "cancelled": 0, "unknown": 0,
            "total": 0, "coverage": {"unknownResult": 0},
        })

    def test_counts_follow_the_same_filters(self):
        self.write([
            record(status="ok", request_id="ok-1"),
            record(status="upstream_error", request_id="failed-1"),
            record(status="client_disconnected", request_id="cancelled-1"),
            {key: value for key, value in record(request_id="unknown-1").items()
             if key != "status"},
        ])
        failed = self.read(result="failed")
        self.assertEqual(failed["results"], {
            "success": 0, "failed": 1, "cancelled": 0, "unknown": 0,
            "total": 1, "coverage": {"unknownResult": 0},
        })
        cancelled = self.read(result="cancelled")
        self.assertEqual(cancelled["results"]["cancelled"], 1)
        unknown = self.read(result="unknown")
        self.assertEqual(unknown["results"]["unknown"], 1)
        self.assertEqual(unknown["results"]["success"], 0)
        keyed = self.read(key_id="key-a")
        self.assertEqual(keyed["results"]["total"], 4)
        other_key = self.read(key_id="key-b")
        self.assertEqual(other_key["results"]["total"], 0)

    def test_cancelled_spellings_and_absent_status_stay_distinct(self):
        self.write([
            record(status="canceled", request_id="c1"),
            record(status="cancelled", request_id="c2"),
            record(status="client_disconnected", request_id="c3"),
            record(status="ok", request_id="ok"),
            {key: value for key, value in record(request_id="u1").items()
             if key != "status"},
            record(status="", request_id="u2"),
        ])
        payload = self.read()
        self.assertEqual(payload["results"], {
            "success": 1, "failed": 0, "cancelled": 3, "unknown": 2,
            "total": 6, "coverage": {"unknownResult": 2},
        })

    def test_priceless_read_keeps_the_results_shape(self):
        # A pricer-less read keeps the same result counts; only the money
        # fields are absent (never a fabricated 0).
        self.write([record(status="ok")])
        payload = share_metering.read_share_usage(self.ledger, now=1790000000)
        self.assertEqual(payload["results"]["success"], 1)
        self.assertNotIn("costCny", payload["keys"][0])


if __name__ == "__main__":
    unittest.main()
