# -*- coding: utf-8 -*-
"""Regression tests for the usage aggregation service."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


USAGE_SERVICE_PATH = Path(__file__).resolve().parents[1] / "usagesrv.py"
SPEC = importlib.util.spec_from_file_location("usage_service", USAGE_SERVICE_PATH)
usage_service = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = usage_service
SPEC.loader.exec_module(usage_service)


def reload_usage_service():
    """Re-executes the module in place so import-time constants (env-dependent
    paths) are recomputed. importlib.reload cannot be used here: it re-finds
    the spec by module name, which fails for a location-loaded module whose
    name is not importable."""
    usage_service._price_cache["data"] = None
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
                        "provider:kept": {"calls": 2, "cost": 1.5}
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
            with mock.patch.object(usage_service, "DSH_SNAPSHOT_FILE", str(snapshot_path)):
                result = usage_service.read_dsh_ledger()

        self.assertEqual(result["requests"], 2)
        self.assertEqual(result["costUsd"], 1.5)
        self.assertEqual(result["tokens"], {
            "input": 10,
            "output": 20,
            "cache": 30,
            "reasoning": 40,
        })
        self.assertEqual([day["day"] for day in result["days"]], ["2026-09-02"])
        self.assertEqual([model["model"] for model in result["models"]], ["provider:kept"])


# ---------------------------------------------------------------------------
# Price-engine regression suite.
#
# The estimation engine in usagesrv.py is a Python port of the dsh-cost-meter
# plugin's lib/pricing.js (v1.7.7 semantics; pricing.js is byte-identical in
# v1.7.10). These tests pin that port:
#   - expectations marked "plugin-verified" were produced by running the
#     installed plugin's pricing.js directly (node) with identical inputs;
#   - price fixtures are the ledger-price snapshot transplanted into the
#     self-owned data/prices.json at the 2026-09-11 de-coupling (the plugin
#     itself is no longer read at runtime);
#   - behaviour deltas vs the plugin are labelled D1/D2/D3 and registered in
#     handoffs/handoff-ai-gateway-费用统计与cost-meter升级同步调研-20260905.md
#     (§9 rev.3); the user opted to keep current behaviour, so the tests pin
#     what usagesrv.py does today and must be revisited if that changes.
# All file inputs are redirected to temp dirs and the 60s price-table cache is
# reset per test, so no real user files are ever touched.

FLASH_ENTRY = {
    "cacheHit": 0.007, "cacheMiss": 0.22, "output": 0.66,
    "offPeak": {"cacheHit": 0.007, "cacheMiss": 0.22, "output": 0.66},
    "peak": {"cacheHit": 0.014, "cacheMiss": 0.44, "output": 1.32},
    "legacyBase": {"cacheHit": 0.0028, "cacheMiss": 0.14, "output": 0.28},
}
LUNA_ENTRY = {"input": 0.2, "cachedInput": 0.02, "output": 1.2, "billingMode": "flat"}
PEAK_WINDOWS = [{"start": 1, "end": 4}, {"start": 6, "end": 10}]
PEAK_CFG = {
    "enabled": True,
    "effectiveAtMs": usage_service._parse_ts("2026-08-01T00:00:00Z"),
    "windows": PEAK_WINDOWS,
}
TS_PEAK = usage_service._parse_ts("2026-09-02T02:00:00Z")     # Beijing Wed 10:00, peak window
TS_OFF = usage_service._parse_ts("2026-09-02T12:00:00Z")      # Beijing Wed 20:00, off-peak
TS_WEEKEND = usage_service._parse_ts("2026-09-05T02:00:00Z")  # Beijing Sat 10:00, weekend off-peak
TS_LEGACY = usage_service._parse_ts("2026-08-10T02:00:00Z")   # before LEGACY_BASE_BOUNDARY


class PriceEngineFixtureTest(unittest.TestCase):
    """Redirects both price inputs to temp files and resets the cache."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.prices_path = base / "prices.json"
        self.override_path = base / "price.override.json"
        self.write_prices(self.default_prices())
        for attr, path in (("PRICE_FILE", self.prices_path),
                           ("OVERRIDE_FILE", self.override_path)):
            patcher = mock.patch.object(usage_service, attr, str(path))
            patcher.start()
            self.addCleanup(patcher.stop)
        cache_patcher = mock.patch.object(
            usage_service, "_price_cache", {"at": 0.0, "data": None})
        cache_patcher.start()
        self.addCleanup(cache_patcher.stop)

    @staticmethod
    def today_iso():
        # A checkedAt that is always "today" keeps the default fixture
        # non-stale regardless of when the suite runs.
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @classmethod
    def default_prices(cls):
        return {
            "checkedAt": cls.today_iso(),
            "exchangeRate": 7.2,
            "peak": {
                "enabled": True,
                "effectiveAt": "2026-08-01T00:00:00Z",
                "windows": PEAK_WINDOWS,
            },
            "models": {"deepseek-v4-flash": dict(FLASH_ENTRY)},
            "providers": {"openai": {"models": {
                "gpt-5.6-luna": dict(LUNA_ENTRY),
                # Same model id as the models tier but a different price, to
                # prove the models tier wins.
                "deepseek-v4-flash": {"cacheHit": 0.007, "cacheMiss": 0.22, "output": 9.9},
            }}},
        }

    def write_prices(self, obj):
        self.prices_path.write_text(json.dumps(obj), encoding="utf-8")

    def write_override(self, obj):
        self.override_path.write_text(json.dumps(obj), encoding="utf-8")

    def reset_price_cache(self):
        usage_service._price_cache["at"] = 0.0
        usage_service._price_cache["data"] = None


class ModelMatchTest(unittest.TestCase):
    """match_model_id vs the plugin's matchModelId (plugin-verified)."""

    LEDGER_KEYS = ["deepseek-v4-flash", "deepseek-v4-pro"]
    ALIBABA_KEYS = ["qwen3-plus", "qwen3.5-plus", "qwen3.6-plus",
                    "qwen3.7-max", "qwen3.7-plus", "qwen3.8-max"]

    def test_exact_hit(self):
        self.assertEqual(
            usage_service.match_model_id("deepseek-v4-flash", self.LEDGER_KEYS),
            "deepseek-v4-flash")

    def test_canon_equivalence(self):
        self.assertEqual(
            usage_service.match_model_id("GPT-5.6 Luna", ["gpt-5.6-luna"]),
            "gpt-5.6-luna")
        self.assertEqual(
            usage_service.match_model_id("DeepSeek-V4-FLASH", self.LEDGER_KEYS),
            "deepseek-v4-flash")

    def test_containment_takes_longest_candidate(self):
        self.assertEqual(
            usage_service.match_model_id("openwebui:DeepSeek-V4-Flash-0731", self.LEDGER_KEYS),
            "deepseek-v4-flash")

    def test_date_snapshot_and_capacity_suffixes_still_match(self):
        # Plugin-verified: >=3 digit remainders (date snapshots) and non-digit
        # capacity suffixes pass the containment guard.
        self.assertEqual(
            usage_service.match_model_id("DeepSeek-V4-Flash-20260901", self.LEDGER_KEYS),
            "deepseek-v4-flash")
        self.assertEqual(
            usage_service.match_model_id("deepseek-v4-flash-128k", self.LEDGER_KEYS),
            "deepseek-v4-flash")

    def test_digit_fork_guards(self):
        # Plugin-verified: a 1-2 digit version fork never matches a sibling
        # version, nor a differently named variant.
        self.assertIsNone(usage_service.match_model_id("glm-5.3", ["glm-5.2"]))
        self.assertIsNone(usage_service.match_model_id("gpt-5.9", ["gpt-5-nano"]))

    def test_family_similarity(self):
        # Plugin-verified: '27b' vs 'max' divergence is allowed (not a pure
        # digit fork), so the family match lands on qwen3.8-max.
        self.assertEqual(
            usage_service.match_model_id("Qwen3.8-27B", self.ALIBABA_KEYS),
            "qwen3.8-max")
        # Plugin-verified: a request with fewer tokens still matches the more
        # specific candidate.
        self.assertEqual(
            usage_service.match_model_id("glm-5", ["glm-5.2", "glm-5.3"]),
            "glm-5.2")

    def test_provider_prefix_breaks_family_match(self):
        # D4 (registered deviation): usagesrv matches the raw "provider:model"
        # string, so the leading provider token blocks family similarity, while
        # the plugin matches the bare model id and finds qwen3.8-max. Such ids
        # only appear in the plugin's own ledger block today, which 8001 reads
        # verbatim, so estimates are unaffected.
        self.assertIsNone(
            usage_service.match_model_id("m365:Qwen3.8-27B", self.ALIBABA_KEYS))

    def test_prefix_stage_and_digit_guard(self):
        # The candidate canon is shorter than the containment stage's 4-char
        # floor, so these requests exercise the prefix stage directly.
        self.assertEqual(usage_service.match_model_id("k2-chat", ["k2"]), "k2")
        self.assertIsNone(usage_service.match_model_id("k2-5", ["k2"]))

    def test_canon_and_decor_helpers(self):
        # Plugin-verified: parenthetical annotations are dropped entirely.
        # (usagesrv.py's docstring example claims 'glm52go' — a docstring typo;
        # the code matches the plugin.)
        self.assertEqual(usage_service.canon_model_id("GLM-5.2 (go)"), "glm52")
        self.assertEqual(usage_service.canon_model_id("GPT-5.6 Luna（Go）"), "gpt56luna")
        self.assertEqual(
            usage_service._strip_id_decor("deepseek-v4-flash-2026-01-01"),
            "deepseek-v4-flash")
        self.assertEqual(usage_service._strip_id_decor("kimi-k2.6-v1.5"), "kimi-k2.6")
        # Bare numeric suffixes are part of the model name, not version decor.
        self.assertEqual(usage_service._strip_id_decor("glm-5.3"), "glm-5.3")


class PeakWindowTest(unittest.TestCase):
    """weekend_zone_at / is_peak_hour vs the plugin (plugin-verified)."""

    def test_weekend_zone_covers_beijing_sat_and_sun(self):
        start, end = usage_service.weekend_zone_at(TS_WEEKEND)
        self.assertEqual(start, usage_service._parse_ts("2026-09-04T16:00:00Z"))
        self.assertEqual(end, usage_service._parse_ts("2026-09-06T16:00:00Z"))

    def test_first_weekend_is_clamped_to_effective_at(self):
        # Beijing Sunday 00:00 on the first weekend: the zone starts at the
        # rule's effective instant and only covers that Sunday.
        at = usage_service._parse_ts("2026-08-22T16:00:00Z")
        start, end = usage_service.weekend_zone_at(at)
        self.assertEqual(start, at)
        self.assertEqual(end, usage_service._parse_ts("2026-08-23T16:00:00Z"))

    def test_weekday_and_pre_effective_times_return_none(self):
        self.assertIsNone(
            usage_service.weekend_zone_at(usage_service._parse_ts("2026-09-04T10:00:00Z")))
        self.assertIsNone(
            usage_service.weekend_zone_at(usage_service._parse_ts("2026-08-22T10:00:00Z")))

    def test_is_peak_hour_windows(self):
        eff = PEAK_CFG["effectiveAtMs"]
        self.assertTrue(usage_service.is_peak_hour(TS_PEAK, eff, PEAK_WINDOWS))
        self.assertFalse(
            usage_service.is_peak_hour(usage_service._parse_ts("2026-09-02T04:00:00Z"),
                                       eff, PEAK_WINDOWS))
        self.assertFalse(usage_service.is_peak_hour(TS_OFF, eff, PEAK_WINDOWS))

    def test_is_peak_hour_cross_midnight_window(self):
        eff = PEAK_CFG["effectiveAtMs"]
        windows = [{"start": 22, "end": 2}]
        self.assertTrue(
            usage_service.is_peak_hour(usage_service._parse_ts("2026-09-02T23:00:00Z"),
                                       eff, windows))
        self.assertTrue(
            usage_service.is_peak_hour(usage_service._parse_ts("2026-09-02T01:00:00Z"),
                                       eff, windows))
        self.assertFalse(usage_service.is_peak_hour(TS_OFF, eff, windows))

    def test_weekend_effective_gate_and_empty_windows_suppress_peak(self):
        eff = PEAK_CFG["effectiveAtMs"]
        self.assertFalse(usage_service.is_peak_hour(TS_WEEKEND, eff, PEAK_WINDOWS))
        self.assertFalse(
            usage_service.is_peak_hour(TS_PEAK,
                                       usage_service._parse_ts("2026-09-03T00:00:00Z"),
                                       PEAK_WINDOWS))
        self.assertFalse(usage_service.is_peak_hour(TS_PEAK, eff, []))

    def test_beijing_weekday_origin(self):
        self.assertEqual(usage_service._beijing_weekday(0), 4)  # 1970-01-01: Thursday


class TierForTest(unittest.TestCase):
    """tier_for vs the plugin's tierFor (plugin-verified when sub-tiers exist)."""

    def test_legacy_boundary_uses_legacy_base(self):
        tier = usage_service.tier_for(FLASH_ENTRY, TS_LEGACY, PEAK_CFG)
        self.assertEqual(tier["cacheHit"], 0.0028)
        self.assertEqual(tier["cacheMiss"], 0.14)
        self.assertEqual(tier["output"], 0.28)

    def test_legacy_boundary_without_legacy_base_falls_back_to_base(self):
        entry = {k: FLASH_ENTRY[k] for k in ("cacheHit", "cacheMiss", "output")}
        tier = usage_service.tier_for(entry, TS_LEGACY, PEAK_CFG)
        self.assertEqual(tier["cacheMiss"], 0.22)

    def test_peak_hour_uses_peak_sub_tier(self):
        tier = usage_service.tier_for(FLASH_ENTRY, TS_PEAK, PEAK_CFG)
        self.assertEqual(tier["cacheHit"], 0.014)
        self.assertEqual(tier["cacheMiss"], 0.44)
        self.assertEqual(tier["output"], 1.32)

    def test_peak_hour_without_sub_tier_uses_base(self):
        # D1 aligned (user decision 2026-09-05): flat prices keep their base
        # value during peak windows, matching the plugin's tierFor.
        flat = {"cacheHit": 0.02, "cacheMiss": 0.2, "output": 1.2}
        tier = usage_service.tier_for(flat, TS_PEAK, PEAK_CFG)
        self.assertEqual(tier["cacheHit"], 0.02)
        self.assertEqual(tier["cacheMiss"], 0.2)
        self.assertEqual(tier["output"], 1.2)

    def test_off_peak_after_effective(self):
        entry = dict(FLASH_ENTRY)
        entry["offPeak"] = {"cacheHit": 0.001, "cacheMiss": 0.1, "output": 0.5}
        tier = usage_service.tier_for(entry, TS_OFF, PEAK_CFG)
        self.assertEqual(tier["cacheMiss"], 0.1)

    def test_disabled_or_not_yet_effective_uses_base(self):
        disabled = dict(PEAK_CFG, enabled=False)
        self.assertEqual(
            usage_service.tier_for(FLASH_ENTRY, TS_PEAK, disabled)["cacheMiss"], 0.22)
        not_yet = dict(PEAK_CFG,
                       effectiveAtMs=usage_service._parse_ts("2026-09-03T00:00:00Z"))
        self.assertEqual(
            usage_service.tier_for(FLASH_ENTRY, TS_PEAK, not_yet)["cacheMiss"], 0.22)

    def test_weekend_is_off_peak(self):
        self.assertEqual(
            usage_service.tier_for(FLASH_ENTRY, TS_WEEKEND, PEAK_CFG)["cacheMiss"], 0.22)


class CostOfTest(unittest.TestCase):
    """cost_of golden values (plugin-verified: identical numbers from pricing.js)."""

    TOKENS = {"input": 500000, "output": 100000, "cache": 500000, "reasoning": 0}

    def test_golden_four_time_points(self):
        cases = [
            (TS_OFF, 0.1795),      # off-peak base tier
            (TS_PEAK, 0.359),      # peak sub-tier
            (TS_WEEKEND, 0.1795),  # weekend off-peak
            (TS_LEGACY, 0.0994),   # legacy base tier
        ]
        for at_ms, expected in cases:
            with self.subTest(at_ms=at_ms):
                cost = usage_service.cost_of(dict(self.TOKENS), FLASH_ENTRY, at_ms, PEAK_CFG)
                self.assertAlmostEqual(cost, expected, places=9)

    def test_reasoning_tokens_are_priced(self):
        entry = {"cacheMiss": 1.0, "output": 2.0, "reasoning": 3.0}
        tokens = {"input": 1000000, "output": 0, "cache": 0, "reasoning": 1000000}
        self.assertAlmostEqual(
            usage_service.cost_of(tokens, entry, TS_OFF, PEAK_CFG), 4.0, places=9)

    def test_zero_usage_costs_zero(self):
        self.assertEqual(
            usage_service.cost_of(
                {"input": 0, "output": 0, "cache": 0}, FLASH_ENTRY, TS_OFF, PEAK_CFG),
            0.0)


class EstimateRecordTest(PriceEngineFixtureTest):
    """End-to-end estimate: resolve_price -> complete_tier -> cost_of."""

    def test_golden_time_points_via_prices(self):
        cases = [
            (TS_OFF, 0.1795, False),
            (TS_PEAK, 0.359, True),
            (TS_WEEKEND, 0.1795, False),
        ]
        for at_ms, expected, in_peak in cases:
            with self.subTest(at_ms=at_ms):
                cost, priced, matched, peak = usage_service.estimate_record(
                    "openwebui:DeepSeek-V4-Flash-0731", at_ms, 1000000, 100000, 500000, 0)
                self.assertTrue(priced)
                self.assertEqual(matched, "deepseek-v4-flash")
                self.assertEqual(peak, in_peak)
                self.assertAlmostEqual(cost, expected, places=9)

    def test_pre_boundary_estimate_uses_legacy_base(self):
        # D3 closed together with the D1 alignment (2026-09-05): the parse
        # chain now preserves the legacyBase sub-tier, so pre-boundary records
        # bill at 0.0994 exactly like the plugin (plugin-verified).
        cost, priced, matched, peak = usage_service.estimate_record(
            "openwebui:DeepSeek-V4-Flash-0731", TS_LEGACY, 1000000, 100000, 500000, 0)
        self.assertTrue(priced)
        self.assertAlmostEqual(cost, 0.0994, places=9)
        self.assertTrue(peak)

    def test_flat_model_peak_uses_base(self):
        # D1 aligned (user decision 2026-09-05): plugin-verified base cost
        # (0.23) during peak windows for flat entries.
        cost, priced, matched, peak = usage_service.estimate_record(
            "gpt-5.6-luna", TS_PEAK, 1000000, 100000, 500000, 0)
        self.assertTrue(priced)
        self.assertEqual(matched, "gpt-5.6-luna")
        self.assertTrue(peak)
        self.assertAlmostEqual(cost, 0.23, places=9)

    def test_unmatched_model_stays_unpriced(self):
        # D2 (registered deviation): the plugin falls back to prices.default on
        # the deepseek channel; usagesrv has no default fallback. User decision
        # pins current behaviour.
        cost, priced, matched, peak = usage_service.estimate_record(
            "GLM-OCR", TS_OFF, 100000, 1000, 0, 0)
        self.assertFalse(priced)
        self.assertEqual(cost, 0.0)
        self.assertIsNone(matched)
        self.assertFalse(peak)


class ResolvePriceTest(PriceEngineFixtureTest):
    """Two-level self-owned price chain priority, override recursion and metadata."""

    def test_models_tier_beats_providers_tier_on_equal_score(self):
        entry, mode, matched, source = usage_service.resolve_price("deepseek-v4-flash")
        self.assertEqual(source, "prices")
        self.assertEqual(matched, "deepseek-v4-flash")
        self.assertEqual(mode, "deepseek-peak")
        self.assertEqual(entry["output"], 0.66)

    def test_providers_tier_used_when_models_tier_lacks_model(self):
        entry, mode, matched, source = usage_service.resolve_price("gpt-5.6-luna")
        self.assertEqual(source, "prices")
        self.assertEqual(mode, "flat")
        self.assertEqual(entry["cacheMiss"], 0.2)
        self.assertEqual(entry["cacheHit"], 0.02)  # cachedInput fallback
        self.assertEqual(entry["output"], 1.2)

    def test_override_wins_over_both(self):
        self.write_override({"gpt-5.6-luna": {"input": 9}})
        entry, mode, matched, source = usage_service.resolve_price("gpt-5.6-luna")
        self.assertEqual(source, "override")
        self.assertEqual(entry["cacheMiss"], 9)
        self.assertEqual(entry["cacheHit"], 9)  # cacheHit falls back to cacheMiss
        self.assertEqual(entry["output"], 0)

    def test_override_target_recursion(self):
        self.write_override({"m365:my-alias": {"target": "deepseek-v4-flash"}})
        entry, mode, matched, source = usage_service.resolve_price("m365:my-alias")
        self.assertEqual(matched, "deepseek-v4-flash")
        self.assertEqual(source, "prices")
        self.assertEqual(entry["cacheMiss"], 0.22)

    def test_unpriced_entries_are_skipped(self):
        prices = self.default_prices()
        prices["providers"]["openai"]["models"]["gpt-5.6-luna"] = {"unpriced": True}
        self.write_prices(prices)
        self.reset_price_cache()
        entry, mode, matched, source = usage_service.resolve_price("gpt-5.6-luna")
        self.assertIsNone(entry)
        self.assertIsNone(matched)
        self.assertIsNone(source)

    def test_price_meta_reflects_inputs(self):
        meta = usage_service.load_price_table()["meta"]
        self.assertEqual(meta, {
            "pricesFound": True,
            "checkedAt": self.today_iso(),
            "checkedAgeDays": 0,
            "checkedStale": False,
            "overrideFound": False,
        })
        self.write_override({"gpt-5.6-luna": {"input": 9}})
        self.reset_price_cache()
        self.assertTrue(usage_service.load_price_table()["meta"]["overrideFound"])

    def test_peak_config_parsed_from_prices(self):
        table = usage_service.load_price_table()
        self.assertTrue(table["peak"]["enabled"])
        self.assertEqual(table["peak"]["effectiveAtMs"],
                         usage_service._parse_ts("2026-08-01T00:00:00Z"))
        self.assertEqual(table["peak"]["windows"], PEAK_WINDOWS)

    def test_exchange_rate_fallbacks(self):
        self.assertEqual(usage_service.load_price_table()["exchangeRate"], 7.2)
        prices = self.default_prices()
        del prices["exchangeRate"]
        self.write_prices(prices)
        self.reset_price_cache()
        self.assertEqual(usage_service.load_price_table()["exchangeRate"], 7.2)
        prices["exchangeRate"] = 0
        self.write_prices(prices)
        self.reset_price_cache()
        self.assertEqual(usage_service.load_price_table()["exchangeRate"], 7.2)


class CheckedAtStaleTest(PriceEngineFixtureTest):
    """prices.json checkedAt age drives the page staleness banner.

    Missing or unparseable checkedAt degrades quietly (checkedStale=None,
    page stays silent), mirroring the old version-detection inconclusive path.
    """

    def test_recent_checked_at_is_not_stale(self):
        meta = usage_service.load_price_table()["meta"]
        self.assertIs(meta["checkedStale"], False)

    def test_old_checked_at_reports_stale(self):
        prices = self.default_prices()
        prices["checkedAt"] = "2020-01-01T00:00:00Z"
        self.write_prices(prices)
        self.reset_price_cache()
        meta = usage_service.load_price_table()["meta"]
        self.assertIs(meta["checkedStale"], True)
        self.assertGreater(meta["checkedAgeDays"], 30)

    def test_missing_checked_at_is_inconclusive(self):
        prices = self.default_prices()
        del prices["checkedAt"]
        self.write_prices(prices)
        self.reset_price_cache()
        meta = usage_service.load_price_table()["meta"]
        self.assertIsNone(meta["checkedAt"])
        self.assertIsNone(meta["checkedAgeDays"])
        self.assertIsNone(meta["checkedStale"])


class EnvPathOverrideTest(unittest.TestCase):
    """USAGE_PRICE_FILE / DSH_LEDGER_SNAPSHOT_FILE override the defaults.

    Paths are resolved at import time, so the test reloads the module with a
    patched environment; a cleanup reload restores the import-time state.
    """

    def test_paths_follow_env_and_are_actually_used(self):
        self.addCleanup(reload_usage_service)
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            prices_path = base / "prices.json"
            prices_path.write_text(json.dumps({
                "checkedAt": "2026-09-11",
                "exchangeRate": 7.2,
                "peak": {"enabled": True,
                         "effectiveAt": "2026-08-01T00:00:00Z",
                         "windows": PEAK_WINDOWS},
                "models": {"deepseek-v4-flash": FLASH_ENTRY},
                "providers": {},
            }), encoding="utf-8")
            snapshot_path = base / "dsh-ledger-20260902.json"
            snapshot_path.write_text(json.dumps({
                "config": {"pricingCurrency": "USD"},
                "days": {"2026-09-02": {
                    "calls": 2, "cost": 1.5, "byProviderModel": {},
                }},
            }), encoding="utf-8")
            env = {
                "USAGE_PRICE_FILE": str(prices_path),
                "DSH_LEDGER_SNAPSHOT_FILE": str(snapshot_path),
            }
            with mock.patch.dict(os.environ, env):
                reload_usage_service()
                self.assertEqual(usage_service.PRICE_FILE, str(prices_path))
                self.assertEqual(usage_service.DSH_SNAPSHOT_FILE, str(snapshot_path))
                # The override is not cosmetic: price resolution really reads it.
                entry, mode, matched, source = usage_service.resolve_price(
                    "deepseek-v4-flash")
                self.assertEqual(source, "prices")
                self.assertEqual(entry["cacheMiss"], 0.22)
                ledger = usage_service.read_dsh_ledger()
                self.assertEqual(ledger["costUsd"], 1.5)


class NoneTimeRobustnessTest(PriceEngineFixtureTest):
    """D6 fix: unparseable record times bill at base price instead of crashing.

    Plugin semantics for non-finite atMs: legacy/peak/off-peak branches all
    skip (base tier), weekend zone returns null, is_peak_hour returns false.
    """

    def test_weekend_zone_and_peak_hour_reject_invalid_time(self):
        self.assertIsNone(usage_service.weekend_zone_at(None))
        self.assertFalse(
            usage_service.is_peak_hour(None, PEAK_CFG["effectiveAtMs"], PEAK_WINDOWS))

    def test_tier_for_invalid_time_uses_base(self):
        tier = usage_service.tier_for(FLASH_ENTRY, None, PEAK_CFG)
        self.assertEqual(tier["cacheMiss"], 0.22)
        self.assertEqual(tier["output"], 0.66)

    def test_estimate_record_with_unparseable_time_bills_at_base(self):
        cost, priced, matched, peak = usage_service.estimate_record(
            "openwebui:DeepSeek-V4-Flash-0731", None, 1000000, 100000, 500000, 0)
        self.assertTrue(priced)
        self.assertEqual(matched, "deepseek-v4-flash")
        self.assertAlmostEqual(cost, 0.1795, places=9)
        self.assertFalse(peak)


class UnpricedVetoTest(PriceEngineFixtureTest):
    """An explicit unpriced marker must not be bypassed by an equally specific
    priced entry in another provider tier. Real case: z-ai/glm-5.3 is marked
    unpriced while opencode-go/glm-5.3 carries the same id priced."""

    @staticmethod
    def veto_prices():
        prices = PriceEngineFixtureTest.default_prices()
        prices["models"] = {}
        prices["providers"] = {
            "z-ai": {"models": {"glm-5.3": {
                "input": 1.4, "cachedInput": 0.26, "output": 4.4, "unpriced": True}}},
            "opencode-go": {"models": {
                "glm-5.3": {"input": 1.4, "cachedInput": 0.26, "output": 4.4},
                "glm-5.3-flash": {"input": 0.15, "cachedInput": 0.03, "output": 0.6}},
            },
        }
        return prices

    def test_unpriced_marker_vetoes_equal_score_pricing(self):
        self.write_prices(self.veto_prices())
        self.reset_price_cache()
        entry, mode, matched, source = usage_service.resolve_price("glm-5.3")
        self.assertIsNone(entry)
        self.assertIsNone(matched)
        self.assertIsNone(source)

    def test_more_specific_priced_hit_beats_vaguer_unpriced(self):
        # The priced exact hit (1000 + 9) outranks the vague unpriced
        # containment hit on glm-5.3 (0 + 4): the veto applies only when the
        # unpriced marker is at least as specific as the priced hit.
        self.write_prices(self.veto_prices())
        self.reset_price_cache()
        entry, mode, matched, source = usage_service.resolve_price("glm-5.3-flash")
        self.assertEqual(matched, "glm-5.3-flash")
        self.assertEqual(entry["cacheMiss"], 0.15)


class RetiredQuotaSnapshotTests(unittest.TestCase):
    """B0 (2026-11-01): the Qoder-CN quota read is NOT assembled.

    The retired source's old token files and desired-state marker are never
    read, no request is sent to 8095 and no old snapshot is reused. The
    endpoint answers an explicit ``not_assembled`` state with null readings —
    never a fabricated 0.
    """

    def test_quota_snapshot_is_not_assembled_without_any_request(self):
        with mock.patch("socket.create_connection") as connect,                 mock.patch("urllib.request.urlopen") as urlopen:
            payload = usage_service._quota_snapshot_payload(force=True)
        connect.assert_not_called()
        urlopen.assert_not_called()
        self.assertEqual(payload["state"], "not_assembled")
        self.assertIsNone(payload["remaining"])
        self.assertIsNone(payload["used"])
        self.assertIsNone(payload["total"])
        self.assertIsNone(payload["source"])
        self.assertEqual(payload["gate"]["verdict"], "not_assembled")
        self.assertIn("退役来源", payload["error"])

    def test_retired_token_helpers_are_gone(self):
        for name in ("_qoder_cn_token", "_qoder_global_token",
                     "_upstream_read_gate", "_read_desired_state",
                     "_desired_state_marker_path"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(usage_service, name))


if __name__ == "__main__":
    unittest.main()
