# -*- coding: utf-8 -*-
"""Local stats environment: raw usage only (price loader retired with #30)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

AI_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
if str(AI_GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_GATEWAY_ROOT))

import usagesrv  # noqa: E402
from console import stats as console_stats  # noqa: E402
from local.backend import stats_env  # noqa: E402


class RetiredPriceLoaderTests(unittest.TestCase):
    def test_local_stats_env_has_no_price_loader(self):
        for name in ("load_price_table", "normalize_price_table",
                      "match_model_id", "PRICE_CACHE_TTL_S",
                      "PRICE_STALE_DAYS"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(stats_env, name), name)

    def test_shared_pricing_rules_module_is_gone(self):
        import importlib.util
        self.assertIsNone(importlib.util.find_spec("shared.pricing_rules"))

    def test_stats_environment_carries_raw_paths_and_time_rules(self):
        fields = console_stats.StatsEnvironment.__dataclass_fields__
        self.assertEqual(sorted(fields), sorted((
            "chat2api_log", "dsh_snapshot", "dsh_merge_day", "parse_ts",
            "beijing_day", "day_start_ms", "display_path")))


class RawLocalStatsTests(unittest.TestCase):
    def test_bounded_stats_reports_raw_usage_without_money(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "usage-chat2api.jsonl"
            ledger.write_text(json.dumps({
                "time": "2026-11-07T10:00:00+08:00",
                "model": "fake-um-model",
                "status": "ok",
                "input_tokens": 100,
                "output_tokens": 50,
                "cached_input_tokens": 0,
                "reasoning_tokens": 0,
            }) + "\n", encoding="utf-8")
            environment = console_stats.StatsEnvironment(
                chat2api_log=str(ledger),
                dsh_snapshot=str(Path(directory) / "absent.json"),
                dsh_merge_day="",
                parse_ts=usagesrv._parse_ts,
                beijing_day=usagesrv._beijing_day,
                day_start_ms=usagesrv._day_start_ms,
                display_path=usagesrv._display_path)
            result = console_stats.bounded_stats(
                {"chat2api": str(ledger)}, "2026-11-07", "2026-11-07",
                environment=environment)
        self.assertEqual(result["totals"]["requests"], 1)
        self.assertEqual(result["totals"]["input_tokens"], 100)
        self.assertEqual(result["totals"]["output_tokens"], 50)
        self.assertNotIn("estimated_cost_cny", result["totals"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
