# -*- coding: utf-8 -*-
"""Retired price-version ledger: assert it is gone, not exercised.

The price-table version machinery (``shared/pricing_rules.py``), the price
columns of the UM read face and the CNY conversion were retired with the
cost/price feature set (#30). This suite keeps the regression runnable while
pinning the retirement:

* ``shared.pricing_rules`` does not import;
* ``usagesrv`` exposes no price-table loader, resolver or estimate helper;
* the bounded statistics payload carries raw usage only: requests and tokens,
  no USD/CNY amount, no price version and no unpriced model list;
* a record's ``request_started_at`` price-eligibility field is gone while the
  raw usage fields are kept.

All file inputs are redirected to temp dirs; no production ledger is touched.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

AI_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
if str(AI_GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_GATEWAY_ROOT))

import usagesrv  # noqa: E402
from console import stats as console_stats  # noqa: E402


def _environment(ledger_path, snapshot_path):
    return console_stats.StatsEnvironment(
        chat2api_log=str(ledger_path),
        dsh_snapshot=str(snapshot_path),
        dsh_merge_day="",
        parse_ts=usagesrv._parse_ts,
        beijing_day=usagesrv._beijing_day,
        day_start_ms=usagesrv._day_start_ms,
        display_path=usagesrv._display_path,
    )


def _write_ledger(path, records):
    path.write_text("\n".join(json.dumps(record) for record in records)
                     + "\n", encoding="utf-8")


class RetiredPriceVersionTests(unittest.TestCase):
    def test_price_version_machinery_is_gone(self):
        for name in (
            "load_price_table",
            "resolve_price",
            "estimate_record",
            "estimate_record_with_version",
            "estimate_record_with_version_and_rate",
            "version_at",
            "version_rate",
            "_price_cache",
            "_price_lock",
            "_bucket_price_estimate",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(usagesrv, name), name)

    def test_bounded_stats_has_no_price_version_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "usage-chat2api.jsonl"
            _write_ledger(ledger, [{
                "time": "2026-09-25T12:00:00Z",
                "model": "deepseek-v4-flash",
                "input_tokens": 1000000,
                "output_tokens": 0,
                "price_version": "v1",
            }])
            result = console_stats.bounded_stats(
                {"chat2api": str(ledger)}, "2026-09-25", "2026-09-25",
                environment=_environment(
                    ledger, Path(directory) / "absent.json"))
        self.assertEqual(result["totals"]["requests"], 1)
        self.assertEqual(result["totals"]["input_tokens"], 1000000)
        for key in ("estimated_cost_cny", "costUsd", "costCny",
                     "priceVersions", "unpriced_models"):
            with self.subTest(key=key):
                self.assertNotIn(key, result["totals"])
        row = result["days"][0]["sources"][0]["models"][0]
        self.assertNotIn("estimated_cost_cny", row)
        self.assertNotIn("priceVersions", row)
        self.assertNotIn("unpriced_models", result["coverage"])

    def test_um_read_face_has_no_price_version_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "usage-chat2api.jsonl"
            _write_ledger(ledger, [{
                "time": "2026-09-25T12:00:00Z",
                "model": "deepseek-v4-flash",
                "input_tokens": 1000,
                "output_tokens": 500,
                "status": "ok",
                "price_version": "v1",
                "request_started_at": "2026-09-25T12:00:00Z",
            }])
            environment = _environment(
                ledger, Path(directory) / "absent.json")
            now = datetime(2026, 9, 25, 21, 0,
                           tzinfo=timezone(timedelta(hours=8))).timestamp()
            today = console_stats.um_today(ledger=str(ledger), now=now)
            trend = console_stats.um_trend(
                ledger=str(ledger), range="7d", now=now)
            requests = console_stats.um_requests(
                ledger=str(ledger), now=now)
        self.assertNotIn("cost", today)
        self.assertNotIn("costCny", trend["trend"]["all"]["total"])
        self.assertNotIn("costCny", requests["page"]["rows"][0])
        self.assertNotIn("priceVersion", requests["page"]["rows"][0])
        self.assertEqual(requests["page"]["rows"][0]["input"]["value"], 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
