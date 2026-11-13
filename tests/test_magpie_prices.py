# -*- coding: utf-8 -*-
"""Retired price surface: assert it is gone, not exercised.

The Magpie effective-price synchronizer (``console/magpie_prices.py``), the UM
reference-cost read face and the local price-table loader were retired with the
cost/price feature set (#30). This suite keeps the regression runnable while
pinning the retirement:

* ``console.magpie_prices`` does not import;
* ``usagesrv`` exposes no price-table paths, loaders or estimate helpers;
* ``console.stats`` exposes no price helpers and its environment carries no
  price/magpie fields;
* the raw usage read face still reports requests and tokens.

No network, no real ledger and no price file is touched here.
"""

import importlib.util
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

RETIRED_USAGESRV_NAMES = (
    "PRICE_FILE",
    "OVERRIDE_FILE",
    "MAGPIE_PRICE_SNAPSHOT_FILE",
    "MAGPIE_PRICE_STATUS_FILE",
    "load_price_table",
    "resolve_price",
    "estimate_record",
    "estimate_record_with_version",
    "estimate_record_with_version_and_rate",
    "budget_cost_micros",
    "_bucket_price_estimate",
    "_estimate_record_detail",
    "_Money",
    "version_at",
    "version_rate",
    "cost_of",
    "canon_model_id",
    "match_model_id",
)
RETIRED_STATS_NAMES = (
    "_um_price_paths",
    "_um_price_one",
    "_um_price_meta",
    "_um_cost_reading",
    "_um_cost_model_reading",
    "_um_cost_unavailable",
    "_um_cost_payload",
    "_um_price_reading",
    "_um_event_record",
)
RETIRED_ENV_FIELDS = (
    "magpie_price_snapshot",
    "magpie_price_status",
    "estimate_record",
    "price_table_loader",
)


class RetiredModuleTests(unittest.TestCase):
    def test_magpie_prices_module_does_not_import(self):
        self.assertIsNone(importlib.util.find_spec("console.magpie_prices"))
        with self.assertRaises(ModuleNotFoundError):
            __import__("console.magpie_prices")

    def test_shared_pricing_rules_module_does_not_import(self):
        self.assertIsNone(importlib.util.find_spec("shared.pricing_rules"))
        with self.assertRaises(ModuleNotFoundError):
            __import__("shared.pricing_rules")


class RetiredUsagesrvSurfaceTests(unittest.TestCase):
    def test_price_paths_and_estimators_are_gone(self):
        for name in RETIRED_USAGESRV_NAMES:
            with self.subTest(name=name):
                self.assertFalse(hasattr(usagesrv, name), name)

    def test_price_routes_and_fields_are_gone(self):
        source = Path(usagesrv.__file__).read_text(encoding="utf-8")
        for token in (
            "USAGE_PRICE_FILE",
            "CHAT2API_PRICE_TABLE_FILE",
            "MAGPIE_PRICE_SNAPSHOT",
            "MAGPIE_PRICE_STATUS",
            "prices.json",
            "price.override.json",
            "price_table",
            "umReference",
            "exchangeRate",
            "priceVersions",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, source)


class RetiredStatsSurfaceTests(unittest.TestCase):
    def test_price_helpers_are_gone(self):
        for name in RETIRED_STATS_NAMES:
            with self.subTest(name=name):
                self.assertFalse(hasattr(console_stats, name), name)

    def test_stats_environment_has_no_price_fields(self):
        fields = console_stats.StatsEnvironment.__dataclass_fields__
        for name in RETIRED_ENV_FIELDS:
            with self.subTest(name=name):
                self.assertNotIn(name, fields)


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


class RawUsageStillWorksTests(unittest.TestCase):
    def test_bounded_stats_reports_requests_and_tokens_without_money(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "usage-chat2api.jsonl"
            ledger.write_text(json.dumps({
                "time": "2026-09-10T10:00:00+08:00",
                "model": "glm-5.3",
                "input_tokens": 1000,
                "output_tokens": 500,
                "cached_input_tokens": 100,
                "reasoning_tokens": 0,
                "status": "ok",
            }) + "\n", encoding="utf-8")
            environment = _environment(ledger, Path(directory) / "absent.json")
            result = console_stats.bounded_stats(
                {"chat2api": str(ledger)}, "2026-09-10", "2026-09-10",
                environment=environment)
        self.assertEqual(result["totals"]["requests"], 1)
        self.assertEqual(result["totals"]["input_tokens"], 1000)
        self.assertEqual(result["totals"]["output_tokens"], 500)
        self.assertEqual(result["totals"]["cache_tokens"], 100)
        self.assertNotIn("estimated_cost_cny", result["totals"])
        row = result["days"][0]["sources"][0]["models"][0]
        self.assertEqual(row["requests"], 1)
        self.assertEqual(row["input_tokens"], 1000)
        self.assertNotIn("estimated_cost_cny", row)
        self.assertNotIn("unpriced_models", result["coverage"])

    def test_um_today_has_no_cost_face(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "usage-chat2api.jsonl"
            ledger.write_text(json.dumps({
                "time": "2026-09-10T10:00:00+08:00",
                "model": "glm-5.3",
                "input_tokens": 1000,
                "output_tokens": 500,
                "status": "ok",
            }) + "\n", encoding="utf-8")
            console_stats.configure(_environment(
                ledger, Path(directory) / "absent.json"))
            payload = console_stats.um_today(now=datetime(
                2026, 9, 10, 10, 0,
                tzinfo=timezone(timedelta(hours=8))).timestamp())
        self.assertNotIn("cost", payload)
        self.assertEqual(payload["today"]["tokens"]["input"]["value"], 1000)
        self.assertEqual(payload["today"]["tokens"]["output"]["value"], 500)


if __name__ == "__main__":
    unittest.main(verbosity=2)
