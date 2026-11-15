# -*- coding: utf-8 -*-
"""Regression tests for the usage aggregation service (raw usage only).

The cost/price engine was retired with #30: ``usagesrv`` no longer loads a
price table, estimates a record's cost, or converts USD to CNY. This suite keeps
the raw usage regression (DSH frozen-day snapshot) and pins the retirement.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


USAGE_SERVICE_PATH = Path(__file__).resolve().parents[1] / "usagesrv.py"
SPEC = importlib.util.spec_from_file_location("usage_service", USAGE_SERVICE_PATH)
usage_service = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = usage_service
SPEC.loader.exec_module(usage_service)


class TestDshLedger(unittest.TestCase):
    def test_only_configured_merge_day_is_included(self):
        ledger = {
            "config": {"pricingCurrency": "USD"},
            "days": {
                "2026-09-02": {
                    "calls": 2,
                    "cost": 1.5,
                    "input": 10,
                    "output": 20,
                    "cacheRead": 30,
                    "reasoning": 40,
                    "byProviderModel": {
                        "provider:kept": {"calls": 2, "cost": 1.5,
                                          "input": 10, "output": 20,
                                          "cacheRead": 30, "reasoning": 40}
                    },
                },
                "2026-09-03": {
                    "calls": 99,
                    "cost": 88.0,
                    "byProviderModel": {
                        "provider:excluded": {"calls": 99, "cost": 88.0}
                    },
                },
            },
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_path = Path(temp_dir) / "dsh-ledger-20260902.json"
            snapshot_path.write_text(json.dumps(ledger), encoding="utf-8")
            with mock.patch.object(usage_service, "DSH_SNAPSHOT_FILE",
                                   str(snapshot_path)):
                result = usage_service.read_dsh_ledger()

        self.assertEqual(result["requests"], 2)
        self.assertEqual(result["tokens"], {
            "input": 10,
            "output": 20,
            "cache": 30,
            "reasoning": 40,
        })
        self.assertEqual([day["day"] for day in result["days"]], ["2026-09-02"])
        self.assertEqual([model["model"] for model in result["models"]],
                         ["provider:kept"])
        self.assertNotIn("costUsd", result)
        self.assertNotIn("pricingCurrency", result)


class PriceEngineRetirementTests(unittest.TestCase):
    def test_price_engine_names_are_gone(self):
        for name in (
            "PRICE_FILE",
            "OVERRIDE_FILE",
            "MAGPIE_PRICE_SNAPSHOT_FILE",
            "MAGPIE_PRICE_STATUS_FILE",
            "_price_cache",
            "_price_lock",
            "load_price_table",
            "resolve_price",
            "estimate_record",
            "estimate_record_with_version",
            "estimate_record_with_version_and_rate",
            "budget_cost_micros",
            "cost_of",
            "canon_model_id",
            "match_model_id",
            "version_at",
            "version_rate",
            "_Money",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(usage_service, name), name)

    def test_build_summary_has_no_money_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "usage-chat2api.jsonl"
            ledger.write_text(json.dumps({
                "time": "2026-09-10T10:00:00+08:00",
                "service": "chat2api",
                "model": "glm-5.3",
                "input_tokens": 100,
                "output_tokens": 50,
                "status": "ok",
            }) + "\n", encoding="utf-8")
            with mock.patch.object(usage_service, "CHAT2API_LOG", str(ledger)):
                summary = usage_service.build_summary()
        self.assertEqual(summary["services"]["chat2api"]["requests"], 1)
        self.assertEqual(
            summary["services"]["chat2api"]["tokens"]["input"], 100)
        self.assertNotIn("estimated_cost_cny",
                         summary["services"]["chat2api"])
        self.assertNotIn("exchangeRate", summary)
        self.assertNotIn("priceVersions", summary)
        self.assertNotIn("umReference", summary)


class RetiredQuotaSnapshotTests(unittest.TestCase):
    """B0 (2026-11-01): the Qoder-CN quota read face is removed.

    The retired source's old token files and desired-state marker are never
    read, no request is sent to 8095 and no old snapshot is reused. The
    payload builder and the HTTP route are gone with the source; there is no
    read face left to assemble or fake.
    """

    def test_quota_snapshot_payload_is_gone(self):
        with mock.patch("socket.create_connection") as connect, \
                mock.patch("urllib.request.urlopen") as urlopen:
            self.assertFalse(hasattr(usage_service, "_quota_snapshot_payload"))
        connect.assert_not_called()
        urlopen.assert_not_called()

    def test_retired_token_helpers_are_gone(self):
        for name in ("_qoder_cn_token", "_qoder_global_token",
                      "_upstream_read_gate", "_read_desired_state",
                      "_desired_state_marker_path"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(usage_service, name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
