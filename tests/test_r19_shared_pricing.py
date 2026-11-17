# -*- coding: utf-8 -*-
"""Retired shared pricing: assert it is gone, not exercised.

The pure price rules (``shared/pricing_rules.py``) and the share-metering price
derivation were retired with the cost/price feature set (#30). This suite keeps the
regression runnable while pinning the retirement:

* ``shared.pricing_rules`` does not import;
* ``share/metering.py`` has no price function, cost/CNY/version/peak fields;
* the same raw share usage (requests, tokens, cache evidence, results) is still
  reported without any price.

All inputs are in-memory or in temp dirs; no production ledger is touched.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

AI_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
if str(AI_GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_GATEWAY_ROOT))

import share.metering as metering  # noqa: E402

RETIRED_METERING_NAMES = (
    "_price",
    "_valid_rate",
    "_CnySum",
    "_CnyTotals",
)


def _record(**overrides):
    record = {
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
        "request_id": "a" * 32,
        "attribution": "verified",
    }
    record.update(overrides)
    return record


class RetiredSharedPricingModuleTests(unittest.TestCase):
    def test_shared_pricing_rules_module_does_not_import(self):
        import importlib.util
        self.assertIsNone(importlib.util.find_spec("shared.pricing_rules"))
        with self.assertRaises(ModuleNotFoundError):
            __import__("shared.pricing_rules")

    def test_metering_has_no_price_surface(self):
        for name in RETIRED_METERING_NAMES:
            with self.subTest(name=name):
                self.assertFalse(hasattr(metering, name), name)
        self.assertNotIn("price_fn", metering.read_share_usage.__code__.co_varnames)
        self.assertNotIn("price_fn", metering.read_share_requests.__code__.co_varnames)


class RawShareUsageStillWorksTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ledger = Path(tmp.name) / "usage-share-backend.jsonl"
        self.ledger.write_text(
            "\n".join(json.dumps(row) for row in (
                _record(),
                _record(key_id="k2", input_tokens=0, output_tokens=0,
                         total_tokens=0),
                _record(key_id="k2", usage_source="none", input_tokens=0,
                         output_tokens=0, total_tokens=0),
            )) + "\n",
            encoding="utf-8")

    def test_usage_reports_requests_tokens_and_results(self):
        result = metering.read_share_usage(str(self.ledger))
        self.assertEqual(result["requests"], 3)
        self.assertEqual(result["results"]["success"], 3)
        key = next(item for item in result["keys"] if item["key_id"] == "k1")
        self.assertEqual(key["requests"], 1)
        self.assertEqual(key["inputTokens"], 100)
        self.assertEqual(key["outputTokens"], 10)
        self.assertEqual(key["tokenUsage"]["inputTokens"], 100)
        for field in (
            "costUsd", "knownCostUsd", "estimatedCostUsd",
            "costCny", "knownCostCny", "estimatedCostCny",
            "costCnyPartial", "knownCostCnyPartial",
            "estimatedCostCnyPartial", "priceVersions",
        ):
            with self.subTest(field=field):
                self.assertNotIn(field, key)
        self.assertNotIn("costCnyPartial", result["coverage"])
        self.assertNotIn("rateFn", result["coverage"])
        self.assertNotIn("priceFn", result["coverage"])
        self.assertNotIn("unpricedRequests", result["coverage"])
        self.assertNotIn("priceVersions", result["coverage"])

    def test_request_page_keeps_raw_usage_without_amounts(self):
        page = metering.read_share_requests(str(self.ledger))
        self.assertEqual(len(page["rows"]), 3)
        row = next(item for item in page["rows"]
                   if item["inputTokens"] == 100)
        self.assertEqual(row["inputTokens"], 100)
        self.assertEqual(row["outputTokens"], 10)
        self.assertEqual(row["result"], "success")
        for field in ("costUsd", "costCny", "costCnyPartial", "priced",
                      "priceVersion"):
            with self.subTest(field=field):
                self.assertNotIn(field, row)
        self.assertNotIn("costCnyPartial", page["coverage"])
        self.assertNotIn("pricedRequests", page["coverage"])
        self.assertNotIn("unpricedRequests", page["coverage"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
