# -*- coding: utf-8 -*-
"""E19-1-04A: ``shared.pricing_rules`` is pure, table-explicit pricing logic.

What this file pins:
  - the time helpers (``parse_ts``/``beijing_day``/``day_start_ms``) resolve
    hand-computed instants, and the Beijing weekend/peak rules follow the
    existing dsh-cost-meter semantics;
  - model matching (``canon_model_id``/``strip_id_decor``/``tokens_of``/
    ``match_model_id_uncached``) returns the same matches as the existing
    usagesrv golden points, without any cache;
  - ``complete_tier``/``normalize_price``/``tier_for``/``cost_of`` select the
    same buckets as the existing ledger amounts (0.1795 / 0.359 / 0.0994 USD);
  - ``version_at``/``version_rate``/``current_version`` pick the segment in
    effect at the record time and never guess for unknown times;
  - ``resolve_price``/``estimate_record``/``estimate_record_detail`` take the
    price table as an explicit argument, never read a file, and raise TypeError
    when the table is omitted;
  - the ten cross-end rules have public names that are the *same function objects*
    as the private spellings ``usagesrv.py`` / ``local/backend/stats_env.py`` still
    call (``CrossEndPublicNameTests``);
  - ``usagesrv.match_model_id`` keeps its own bounded cache (the 97% stats fix)
    and agrees with the shared uncached implementation.

The price tables below are plain in-memory dicts in the shape produced by
``usagesrv.load_price_table``; no file or environment input is used.
"""

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AI_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
SHARED_PATH = AI_GATEWAY_ROOT / "shared" / "pricing_rules.py"

# The cross-end names both hosts are moving to, and the private spellings that
# ``usagesrv.py`` (all ten) and ``local/backend/stats_env.py`` (six) still call
# until E19-1-02 / E19-1-04 rename their call sites. Written from the plan's list,
# not read back from the module, so a missing public name cannot pass silently.
PUBLIC_PRICING_ALIASES = {
    "beijing_weekday": "_beijing_weekday",
    "checked_age_days": "_checked_age_days",
    "current_version": "_current_version",
    "peak_of": "_peak_of",
    "price_version": "_price_version",
    "rate_of": "_rate_of",
    "strip_id_decor": "_strip_id_decor",
    "tiers_of": "_tiers_of",
    "tokens_of": "_tokens_of",
    "version_id": "_version_id",
}

sys.path.insert(0, str(AI_GATEWAY_ROOT))

import shared.pricing_rules as pricing  # noqa: E402


FLASH_V1 = {
    "cacheHit": 0.007, "cacheMiss": 0.22, "output": 0.66,
    "offPeak": {"cacheHit": 0.007, "cacheMiss": 0.22, "output": 0.66},
    "peak": {"cacheHit": 0.014, "cacheMiss": 0.44, "output": 1.32},
    "legacyBase": {"cacheHit": 0.0028, "cacheMiss": 0.14, "output": 0.28},
}
PEAK_WINDOWS = [{"start": 1, "end": 4}]
LUNA = {"input": 0.2, "cachedInput": 0.02, "output": 1.2, "billingMode": "flat"}

# Hand-computed epoch milliseconds (UTC); no datetime round-trip in the tests.
TS_EFF = 1785542400000         # 2026-08-01T00:00:00Z
TS_OFF = 1790337600000          # 2026-09-25T12:00:00Z, Beijing 20:00, off-peak
TS_PEAK = 1790301600000         # 2026-09-25T02:00:00Z, Beijing 10:00, peak
TS_WEEKEND = 1790388000000      # 2026-09-26T02:00:00Z, Beijing Sat 10:00
TS_LEGACY = 1786327200000       # 2026-08-10T02:00:00Z, before LEGACY_BASE_BOUNDARY
TS_V1_ERA = 1790294400000       # 2026-09-25T00:00:00Z, inside version v1
TS_V2_ERA = 1791158400000       # 2026-10-05T00:00:00Z, inside version v2
TS_V2_PEAK = 1791165600000      # 2026-10-05T02:00:00Z, inside version v2 peak


def peak_config():
    return {"enabled": True, "effectiveAtMs": TS_EFF, "windows": list(PEAK_WINDOWS)}


def old_shape_table(override=None):
    """The table shape load_price_table returns for an OLD-shape prices.json."""
    peak = peak_config()
    tiers = [
        ({"deepseek-v4-flash": dict(FLASH_V1)}, "deepseek-peak", "prices"),
        ({"gpt-5.6-luna": dict(LUNA),
          "deepseek-v4-flash": {"cacheHit": 0.007, "cacheMiss": 0.22,
                               "output": 9.9}},
         "flat", "prices"),
    ]
    version = {
        "versionId": None, "effectiveAt": None, "effectiveAtMs": None,
        "exchangeRate": 7.2, "peak": peak, "tiers": tiers,
        "checkedAt": "2026-09-01", "checkedAgeDays": 24,
    }
    return {
        "exchangeRate": 7.2, "peak": peak, "tiers": tiers,
        "override": dict(override or {}), "versions": [version],
        "versioned": False,
    }


def versioned_table(override=None):
    """The table shape load_price_table returns for a versioned prices.json."""
    v1 = {
        "versionId": "v1", "effectiveAt": "2026-08-01T00:00:00Z",
        "effectiveAtMs": TS_EFF, "exchangeRate": 7.2, "peak": peak_config(),
        "tiers": [({"deepseek-v4-flash": dict(FLASH_V1)},
                   "deepseek-peak", "prices")],
        "checkedAt": "2026-09-01", "checkedAgeDays": 24,
    }
    v2_peak = {"enabled": True, "effectiveAtMs": 1790812800000,
               "windows": list(PEAK_WINDOWS)}
    v2 = {
        "versionId": "v2", "effectiveAt": "2026-10-01T00:00:00Z",
        "effectiveAtMs": 1790812800000, "exchangeRate": 8.0,
        "peak": v2_peak,
        "tiers": [({"deepseek-v4-flash": {
            "cacheHit": 0.007, "cacheMiss": 0.44, "output": 1.32,
            "offPeak": {"cacheHit": 0.007, "cacheMiss": 0.44, "output": 1.32},
            "peak": {"cacheHit": 0.014, "cacheMiss": 0.88, "output": 2.64}}},
            "deepseek-peak", "prices")],
        "checkedAt": "2026-10-01", "checkedAgeDays": 0,
    }
    return {
        "exchangeRate": 8.0,
        "peak": {"enabled": False, "effectiveAtMs": None, "windows": []},
        "tiers": v2["tiers"], "override": dict(override or {}),
        "versions": [v1, v2], "versioned": True,
    }


def assert_estimate(test, actual, expected, matched="deepseek-v4-flash"):
    cost, priced, hit, peak = actual
    test.assertTrue(priced)
    test.assertEqual(hit, matched)
    test.assertEqual(peak, expected[1])
    test.assertAlmostEqual(cost, expected[0], places=9)


class PureTimeRulesTests(unittest.TestCase):

    def test_parse_ts_reads_iso_utc_and_treats_naive_as_utc(self):
        self.assertEqual(pricing.parse_ts("2026-09-25T12:00:00Z"), TS_OFF)
        self.assertEqual(pricing.parse_ts("2026-09-25T12:00:00"), TS_OFF)
        self.assertEqual(pricing.parse_ts("2026-09-25T20:00:00+08:00"), TS_OFF)
        for bad in (None, "", "not-a-time"):
            with self.subTest(bad=bad):
                self.assertIsNone(pricing.parse_ts(bad))

    def test_beijing_day_uses_utc_plus_eight(self):
        self.assertEqual(pricing.beijing_day(TS_OFF), "2026-09-25")
        # 2026-09-25T16:00:00Z is 2026-09-26 00:00 in Beijing.
        self.assertEqual(pricing.beijing_day(1790352000000), "2026-09-26")
        self.assertIsNone(pricing.beijing_day(None))

    def test_day_start_ms_is_beijing_midnight(self):
        self.assertEqual(pricing.day_start_ms("2026-09-25"), 1790265600000)
        self.assertEqual(pricing.day_start_ms("2026-09-26"), 1790352000000)
        for bad in (None, "2026-13-01", "not-a-day"):
            with self.subTest(bad=bad):
                self.assertIsNone(pricing.day_start_ms(bad))

    def test_beijing_weekday_index(self):
        self.assertEqual(pricing._beijing_weekday(0), 4)   # 1970-01-01 was Thursday
        self.assertEqual(pricing._beijing_weekday(1), 5)
        self.assertEqual(pricing._beijing_weekday(2), 6)
        self.assertEqual(pricing._beijing_weekday(3), 0)

    def test_weekend_zone_covers_beijing_saturday_and_sunday(self):
        self.assertEqual(pricing.weekend_zone_at(TS_WEEKEND),
                         (1790352000000, 1790524800000))
        self.assertEqual(pricing.weekend_zone_at(TS_WEEKEND + 86_400_000),
                         (1790352000000, 1790524800000))
        for at_ms in (TS_OFF, TS_LEGACY, None, float("nan")):
            with self.subTest(at_ms=at_ms):
                self.assertIsNone(pricing.weekend_zone_at(at_ms))

    def test_is_peak_hour_uses_utc_windows_and_skips_weekends(self):
        self.assertTrue(pricing.is_peak_hour(TS_PEAK, TS_EFF, PEAK_WINDOWS))
        self.assertFalse(pricing.is_peak_hour(TS_OFF, TS_EFF, PEAK_WINDOWS))
        self.assertFalse(pricing.is_peak_hour(TS_WEEKEND, TS_EFF, PEAK_WINDOWS))
        self.assertFalse(pricing.is_peak_hour(TS_PEAK, TS_PEAK + 1, PEAK_WINDOWS))
        self.assertFalse(pricing.is_peak_hour(TS_PEAK, TS_EFF, []))
        self.assertFalse(pricing.is_peak_hour(None, TS_EFF, PEAK_WINDOWS))


class ModelMatchingTests(unittest.TestCase):

    def test_canon_model_id_strips_annotations_and_non_alnum(self):
        self.assertEqual(pricing.canon_model_id("GLM-5.2 (go)"), "glm52")
        self.assertEqual(pricing.canon_model_id("GLM-5.2（go）"), "glm52")
        self.assertEqual(
            pricing.canon_model_id("openwebui:DeepSeek-V4-Flash-0731"),
            "openwebuideepseekv4flash0731")
        self.assertEqual(pricing.canon_model_id(None), "")

    def test_strip_id_decor_removes_date_snapshot_and_version_suffix(self):
        self.assertEqual(pricing._strip_id_decor("deepseek-v4-flash-20260101"),
                         "deepseek-v4-flash")
        self.assertEqual(pricing._strip_id_decor("glm-5.2@2026-01-01"), "glm-5.2")
        self.assertEqual(pricing._strip_id_decor("glm-5.2-v1.5"), "glm-5.2")

    def test_tokens_of_splits_on_separators(self):
        self.assertEqual(pricing._tokens_of("openwebui:DeepSeek-V4-Flash-0731"),
                         ["openwebui", "deepseek", "v4", "flash", "0731"])

    def test_uncached_match_prefers_exact_then_normalized(self):
        candidates = ["deepseek-v4-flash", "gpt-5.6-luna"]
        self.assertEqual(
            pricing.match_model_id_uncached("deepseek-v4-flash", candidates),
            "deepseek-v4-flash")
        self.assertEqual(
            pricing.match_model_id_uncached(
                "openwebui:DeepSeek-V4-Flash-0731", candidates),
            "deepseek-v4-flash")
        self.assertEqual(
            pricing.match_model_id_uncached(
                "deepseek-v4-flash-20260101", candidates),
            "deepseek-v4-flash")
        self.assertEqual(
            pricing.match_model_id_uncached(
                "deepseek-v4-flash-latest", candidates),
            "deepseek-v4-flash")

    def test_uncached_match_falls_back_to_family_similarity(self):
        self.assertEqual(
            pricing.match_model_id_uncached("glm-5", ["glm-5.3"]), "glm-5.3")

    def test_uncached_match_returns_none_when_absent(self):
        self.assertIsNone(
            pricing.match_model_id_uncached("unknown-model",
                                           ["deepseek-v4-flash"]))
        self.assertIsNone(pricing.match_model_id_uncached("", ["a"]))
        self.assertIsNone(pricing.match_model_id_uncached("a", []))


class TierAndCostTests(unittest.TestCase):

    def test_complete_tier_fills_base_buckets(self):
        self.assertEqual(
            pricing.complete_tier(
                {"input": 0.2, "cachedInput": 0.02, "output": 1.2}),
            {"cacheHit": 0.02, "cacheMiss": 0.2, "output": 1.2,
             "reasoning": None, "unpriced": False})
        self.assertEqual(
            pricing.complete_tier({"cacheMiss": 0.3, "unpriced": True}),
            {"cacheHit": 0.3, "cacheMiss": 0.3, "output": 0,
             "reasoning": None, "unpriced": True})
        self.assertIsNone(pricing.complete_tier(None))

    def test_normalize_price_keeps_peak_subtiers(self):
        entry = pricing.normalize_price(FLASH_V1)
        self.assertEqual(entry["cacheMiss"], 0.22)
        self.assertEqual(entry["peak"]["cacheMiss"], 0.44)
        self.assertEqual(entry["offPeak"]["cacheMiss"], 0.22)
        self.assertEqual(entry["legacyBase"]["cacheMiss"], 0.14)
        self.assertIsNone(pricing.normalize_price(None))

    def test_tier_for_selects_peak_offpeak_legacy_and_base(self):
        entry = pricing.normalize_price(FLASH_V1)
        peak_cfg = peak_config()
        self.assertEqual(pricing.tier_for(entry, TS_PEAK, peak_cfg)["cacheMiss"], 0.44)
        self.assertEqual(pricing.tier_for(entry, TS_OFF, peak_cfg)["cacheMiss"], 0.22)
        self.assertEqual(pricing.tier_for(entry, TS_LEGACY, peak_cfg)["cacheMiss"],
                         0.14)
        self.assertEqual(
            pricing.tier_for(entry, TS_OFF,
                             {"enabled": False, "effectiveAtMs": None,
                              "windows": []})["cacheMiss"], 0.22)
        self.assertEqual(pricing.tier_for(entry, None, peak_cfg)["cacheMiss"], 0.22)

    def test_cost_of_matches_the_ledger_golden_points(self):
        entry = pricing.normalize_price(FLASH_V1)
        peak_cfg = peak_config()
        tokens = {"input": 500000, "output": 100000,
                  "cache": 500000, "reasoning": 0}
        self.assertAlmostEqual(pricing.cost_of(tokens, entry, TS_OFF, peak_cfg),
                               0.1795, places=9)
        self.assertAlmostEqual(pricing.cost_of(tokens, entry, TS_PEAK, peak_cfg),
                               0.359, places=9)
        self.assertAlmostEqual(
            pricing.cost_of(tokens, entry, TS_LEGACY, peak_cfg), 0.0994,
            places=9)

    def test_tiers_of_puts_models_before_provider_tables(self):
        tiers = pricing._tiers_of(
            {"a": {"cacheMiss": 1}},
            {"openai": {"models": {"b": {"cacheMiss": 2}}}})
        self.assertEqual(tiers, [
            ({"a": {"cacheMiss": 1}}, "deepseek-peak", "prices"),
            ({"b": {"cacheMiss": 2}}, "flat", "prices"),
        ])
        self.assertEqual(pricing._tiers_of(None, None), [])

    def test_rate_of_falls_back_to_the_default_exchange_rate(self):
        self.assertEqual(pricing._rate_of(8), 8.0)
        for bad in (0, -1, "8", None):
            with self.subTest(bad=bad):
                self.assertEqual(pricing._rate_of(bad),
                                 pricing.DEFAULT_EXCHANGE_RATE)

    def test_peak_of_normalizes_missing_fields(self):
        self.assertEqual(pricing._peak_of(
            {"enabled": True, "effectiveAt": "2026-08-01T00:00:00Z",
             "windows": list(PEAK_WINDOWS)}),
            {"enabled": True, "effectiveAtMs": TS_EFF,
             "windows": list(PEAK_WINDOWS)})
        self.assertEqual(pricing._peak_of(None),
                         {"enabled": False, "effectiveAtMs": None,
                          "windows": []})
        self.assertEqual(pricing._peak_of({"enabled": "true"})["enabled"], False)

    def test_checked_age_days_is_floor_and_never_negative(self):
        self.assertEqual(pricing._checked_age_days("2026-09-01", TS_OFF / 1000),
                         24)
        self.assertEqual(pricing._checked_age_days("2026-09-30", TS_OFF / 1000), 0)
        self.assertIsNone(pricing._checked_age_days(None, TS_OFF / 1000))
        self.assertIsNone(pricing._checked_age_days("not-a-date",
                                                  TS_OFF / 1000))

    def test_version_id_prefers_explicit_id_then_effective_at(self):
        self.assertEqual(pricing._version_id({"versionId": " v1 "}, TS_EFF), "v1")
        self.assertEqual(
            pricing._version_id({"effectiveAt": "2026-08-01T00:00:00Z"}, TS_EFF),
            "2026-08-01T00:00:00Z")
        self.assertIsNone(pricing._version_id({}, None))

    def test_price_version_builds_a_self_contained_segment(self):
        raw = {
            "versionId": "v1", "effectiveAt": "2026-08-01T00:00:00Z",
            "exchangeRate": 0, "peak": {
                "enabled": True, "effectiveAt": "2026-08-01T00:00:00Z",
                "windows": list(PEAK_WINDOWS)},
            "models": {"deepseek-v4-flash": dict(FLASH_V1)},
            "checkedAt": "2026-09-01",
        }
        version = pricing._price_version(raw, TS_EFF, TS_OFF / 1000)
        self.assertEqual(version["versionId"], "v1")
        self.assertEqual(version["effectiveAtMs"], TS_EFF)
        self.assertEqual(version["exchangeRate"], 7.2)
        self.assertTrue(version["peak"]["enabled"])
        self.assertEqual(version["tiers"][0][1], "deepseek-peak")
        self.assertEqual(version["checkedAgeDays"], 24)


class PriceVersionTests(unittest.TestCase):

    def test_version_at_selects_the_segment_in_effect(self):
        table = versioned_table()
        v1, v2 = table["versions"]
        self.assertIs(pricing.version_at(table, TS_V1_ERA), v1)
        self.assertIs(pricing.version_at(table, TS_V2_ERA), v2)
        self.assertIs(pricing.version_at(table, TS_V2_PEAK), v2)
        for at_ms in (TS_EFF - 1, None, float("nan"), "not-a-time"):
            with self.subTest(at_ms=at_ms):
                self.assertIsNone(pricing.version_at(table, at_ms))

    def test_version_at_old_shape_is_always_the_default_segment(self):
        table = old_shape_table()
        version = table["versions"][0]
        for at_ms in (None, TS_LEGACY, TS_OFF, float("nan")):
            with self.subTest(at_ms=at_ms):
                self.assertIs(pricing.version_at(table, at_ms), version)

    def test_version_rate_follows_the_segment(self):
        table = versioned_table()
        self.assertEqual(pricing.version_rate(table, TS_V1_ERA), 7.2)
        self.assertEqual(pricing.version_rate(table, TS_V2_ERA), 8.0)
        self.assertIsNone(pricing.version_rate(table, TS_EFF - 1))

    def test_current_version_uses_now_and_falls_back_to_earliest(self):
        versions = versioned_table()["versions"]
        v1, v2 = versions
        self.assertIs(pricing._current_version(versions, True, TS_V2_ERA), v2)
        self.assertIs(pricing._current_version(versions, True, TS_EFF - 1), v1)
        self.assertIsNone(pricing._current_version([], True, TS_V2_ERA))
        self.assertIs(pricing._current_version(versions, False, TS_V2_ERA), v1)


class ResolvePriceTests(unittest.TestCase):

    def test_resolve_price_returns_the_tier_entry(self):
        table = old_shape_table()
        entry, mode, matched, source = pricing.resolve_price(
            "deepseek-v4-flash", TS_PEAK, table)
        self.assertEqual((mode, matched, source),
                         ("deepseek-peak", "deepseek-v4-flash", "prices"))
        self.assertEqual((entry["cacheMiss"], entry["output"]), (0.22, 0.66))
        entry, mode, matched, source = pricing.resolve_price(
            "gpt-5.6-luna", TS_PEAK, table)
        self.assertEqual((mode, matched, source),
                         ("flat", "gpt-5.6-luna", "prices"))
        self.assertEqual((entry["cacheMiss"], entry["cacheHit"]), (0.2, 0.02))

    def test_resolve_price_uses_the_injected_matcher(self):
        """The host's memoized matcher is the explicit pricing seam.

        ``shared.pricing_rules`` holds no cache: the host injects its matcher,
        so the per-record pricing path keeps the 97% stats fix.
        """
        table = old_shape_table()
        calls = []

        def matcher(model_id, candidates):
            calls.append((model_id, tuple(candidates)))
            return pricing.match_model_id_uncached(model_id, candidates)

        entry, mode, matched, source = pricing.resolve_price(
            "openwebui:DeepSeek-V4-Flash-0731", TS_PEAK, table,
            matcher=matcher)
        self.assertTrue(calls, "the injected matcher was not called")
        self.assertEqual((mode, matched, source),
                         ("deepseek-peak", "deepseek-v4-flash", "prices"))
        self.assertEqual((entry["cacheMiss"], entry["output"]), (0.22, 0.66))

    def test_estimate_record_passes_the_matcher_to_resolution(self):
        table = old_shape_table()
        calls = []

        def matcher(model_id, candidates):
            calls.append(model_id)
            return None

        cost, priced, matched, peak = pricing.estimate_record(
            "openwebui:DeepSeek-V4-Flash-0731", TS_PEAK, 10, 5, 0, 0, table,
            matcher=matcher)
        self.assertTrue(calls, "estimate_record did not use the matcher")
        self.assertEqual((cost, priced, matched, peak),
                         (0.0, False, None, False))

    def test_resolve_price_reads_only_the_given_table(self):
        cheap = old_shape_table()
        expensive = old_shape_table()
        expensive["versions"][0]["tiers"] = [(
            {"deepseek-v4-flash": {"cacheHit": 0.1, "cacheMiss": 9.9,
                                   "output": 9.9}},
            "deepseek-peak", "prices")]
        self.assertEqual(
            pricing.resolve_price("deepseek-v4-flash", TS_PEAK, cheap)[0]["cacheMiss"],
            0.22)
        self.assertEqual(
            pricing.resolve_price("deepseek-v4-flash", TS_PEAK,
                                  expensive)[0]["cacheMiss"], 9.9)

    def test_resolve_price_override_target_stays_in_the_same_table(self):
        table = old_shape_table(
            {"m365:my-alias": {"target": "deepseek-v4-flash"}})
        entry, mode, matched, source = pricing.resolve_price(
            "m365:my-alias", TS_PEAK, table)
        self.assertEqual((mode, matched, source),
                         ("deepseek-peak", "deepseek-v4-flash", "prices"))
        self.assertEqual(entry["cacheMiss"], 0.22)

    def test_resolve_price_unpriced_veto_blocks_a_priced_tier(self):
        table = old_shape_table()
        table["versions"][0]["tiers"] = [
            ({"deepseek-v4-flash": {"unpriced": True}},
             "deepseek-peak", "prices"),
            ({"deepseek-v4-flash": {"cacheHit": 0.007, "cacheMiss": 0.22,
                                     "output": 0.66}},
             "flat", "prices"),
        ]
        self.assertEqual(
            pricing.resolve_price("deepseek-v4-flash", TS_PEAK, table),
            (None, "flat", None, None))

    def test_resolve_price_unknown_version_is_unpriced(self):
        table = versioned_table()
        self.assertEqual(
            pricing.resolve_price("deepseek-v4-flash", TS_EFF - 1, table),
            (None, "flat", None, None))


class EstimateRecordTests(unittest.TestCase):

    def test_estimate_record_golden_points(self):
        table = old_shape_table()
        cases = [(TS_OFF, (0.1795, False)), (TS_PEAK, (0.359, True)),
                 (TS_WEEKEND, (0.1795, False)), (TS_LEGACY, (0.0994, True)),
                 (None, (0.1795, False))]
        for at_ms, expected in cases:
            with self.subTest(at_ms=at_ms):
                assert_estimate(
                    self,
                    pricing.estimate_record(
                        "openwebui:DeepSeek-V4-Flash-0731", at_ms,
                        1000000, 100000, 500000, 0, table),
                    expected)

    def test_estimate_record_detail_reports_version_rate_and_cache(self):
        table = versioned_table()
        detail = pricing.estimate_record_detail(
            "openwebui:DeepSeek-V4-Flash-0731", TS_V1_ERA,
            1000000, 100000, 500000, 0, table)
        self.assertEqual(detail[1:], (True, "deepseek-v4-flash", False, "v1", 7.2,
                                    False))
        self.assertAlmostEqual(detail[0], 0.1795, places=9)
        detail = pricing.estimate_record_detail(
            "openwebui:DeepSeek-V4-Flash-0731", TS_V2_ERA,
            1000000, 100000, 500000, 0, table)
        self.assertEqual(detail[1:], (True, "deepseek-v4-flash", False, "v2", 8.0,
                                    False))
        self.assertAlmostEqual(detail[0], 0.3555, places=9)
        detail = pricing.estimate_record_detail(
            "openwebui:DeepSeek-V4-Flash-0731", TS_V2_PEAK,
            1000000, 100000, 500000, 0, table)
        self.assertEqual(detail[1:], (True, "deepseek-v4-flash", True, "v2", 8.0,
                                    False))
        self.assertAlmostEqual(detail[0], 0.711, places=9)

    def test_estimate_record_detail_marks_unknown_cache(self):
        table = old_shape_table()
        unknown = pricing.estimate_record_detail(
            "deepseek-v4-flash", TS_OFF, 1000000, 100000, None, 0, table)
        known = pricing.estimate_record_detail(
            "deepseek-v4-flash", TS_OFF, 1000000, 100000, 0, 0, table)
        self.assertTrue(unknown[-1])
        self.assertFalse(known[-1])
        self.assertAlmostEqual(unknown[0], 0.286, places=9)
        self.assertAlmostEqual(known[0], 0.286, places=9)

    def test_estimate_record_unknown_version_and_unpriced_model(self):
        versioned = versioned_table()
        self.assertEqual(
            pricing.estimate_record_detail("deepseek-v4-flash", TS_EFF - 1,
                                          1000000, 100000, 0, 0, versioned),
            (0.0, False, None, False, None, None, False))
        self.assertEqual(
            pricing.estimate_record("deepseek-v4-flash", TS_EFF - 1,
                                    1000000, 100000, 0, 0, versioned),
            (0.0, False, None, False))
        unpriced = old_shape_table()
        unpriced["versions"][0]["tiers"] = [
            ({"deepseek-v4-flash": {"unpriced": True}},
             "deepseek-peak", "prices")]
        self.assertEqual(
            pricing.estimate_record("deepseek-v4-flash", TS_OFF,
                                    1000000, 100000, 0, 0, unpriced),
            (0.0, False, None, False))

    def test_table_is_an_explicit_required_argument(self):
        for call in (
                lambda: pricing.resolve_price("deepseek-v4-flash", TS_OFF),
                lambda: pricing.estimate_record("deepseek-v4-flash", TS_OFF,
                                               1, 1, 0, 0),
                lambda: pricing.estimate_record_detail("deepseek-v4-flash", TS_OFF,
                                                     1, 1, 0, 0)):
            with self.subTest(call=call):
                with self.assertRaises(TypeError):
                    call()

    def test_pure_calls_never_open_a_file(self):
        table = old_shape_table()
        with mock.patch("builtins.open",
                        side_effect=AssertionError("shared pricing opened a file")):
            self.assertAlmostEqual(
                pricing.estimate_record("deepseek-v4-flash", TS_OFF,
                                        1000000, 100000, 500000, 0, table)[0],
                0.1795, places=9)
            self.assertEqual(
                pricing.resolve_price("deepseek-v4-flash", TS_OFF, table)[2],
                "deepseek-v4-flash")
            self.assertIs(pricing.version_at(table, TS_OFF),
                           table["versions"][0])


class CrossEndPublicNameTests(unittest.TestCase):
    """The ten cross-end rules are reachable without an underscore prefix.

    ``usagesrv.py`` calls all ten private spellings and
    ``local/backend/stats_env.py`` calls six of them; a cross-module consumer
    reaching for ``pricing_rules._rate_of`` reaches into another package's
    implementation. Each public name is the *same function object* as its private
    spelling (``assertIs``), not a wrapper: two objects would drift and would make
    the pending consumer renames in E19-1-02 / E19-1-04 behaviour-changing.
    """

    def test_every_cross_end_rule_has_a_public_name(self):
        missing = [name for name in PUBLIC_PRICING_ALIASES
                   if not hasattr(pricing, name)]
        self.assertEqual(
            missing, [],
            "shared pricing is missing public name(s): {}".format(
                ", ".join(sorted(missing))))

    def test_public_names_are_the_same_objects_as_the_private_spellings(self):
        for public, private in PUBLIC_PRICING_ALIASES.items():
            with self.subTest(public=public, private=private):
                self.assertTrue(hasattr(pricing, private),
                                "the private spelling must stay callable for "
                                "existing consumers")
                self.assertIs(getattr(pricing, public),
                              getattr(pricing, private),
                              "{} must be the same function object as {}"
                              .format(public, private))

    def test_public_names_compute_the_same_values_as_the_private_spellings(self):
        """Behaviour, not just object identity: the aliases really are callable."""
        self.assertEqual(pricing.beijing_weekday(0), 4)
        self.assertEqual(pricing.strip_id_decor("deepseek-v4-flash-20260101"),
                         "deepseek-v4-flash")
        self.assertEqual(
            pricing.tokens_of("openwebui:DeepSeek-V4-Flash-0731"),
            ["openwebui", "deepseek", "v4", "flash", "0731"])
        self.assertEqual(pricing.rate_of(0), pricing.DEFAULT_EXCHANGE_RATE)
        self.assertEqual(pricing.peak_of(None)["enabled"], False)
        self.assertEqual(
            pricing.checked_age_days("2026-09-01", TS_OFF / 1000), 24)
        self.assertEqual(pricing.version_id({"versionId": " v1 "}, TS_EFF), "v1")
        self.assertEqual(
            pricing.tiers_of({"a": {"cacheMiss": 1}}, None),
            [({"a": {"cacheMiss": 1}}, "deepseek-peak", "prices")])
        versions = versioned_table()["versions"]
        self.assertIs(pricing.current_version(versions, True, TS_V2_ERA),
                      versions[1])
        version = pricing.price_version(
            {"versionId": "v1", "exchangeRate": 8,
             "models": {"m": {"cacheMiss": 1}}}, TS_EFF, TS_OFF / 1000)
        self.assertEqual((version["versionId"], version["exchangeRate"]),
                         ("v1", 8.0))


class SharedBoundaryTests(unittest.TestCase):

    def test_source_imports_only_pure_stdlib_modules(self):
        tree = ast.parse(SHARED_PATH.read_text(encoding="utf-8-sig"),
                         filename=str(SHARED_PATH))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                roots.add((node.module or "").split(".")[0])
        self.assertLessEqual(roots, {"datetime", "re"})
        self.assertNotIn("threading", roots)
        self.assertNotIn("usagesrv", roots)
        opens = [node for node in ast.walk(tree)
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Name) and node.func.id == "open"]
        self.assertEqual(opens, [], "shared pricing must not call open()")

    def test_module_exposes_no_host_or_cache_state(self):
        for name in ("usagesrv", "load_price_table", "PRICE_FILE",
                      "OVERRIDE_FILE", "_match_cache", "_price_cache",
                      "_match_cache_lock"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(pricing, name),
                                 "shared pricing must not expose " + name)

    def test_fresh_interpreter_import_loads_no_host_module(self):
        """Runtime purity: importing the rules pulls in no host/business module.

        ``threading`` is deliberately NOT in the forbidden list here. This test
        inspects ``sys.modules`` after the interpreter has already started, and a
        stock Ubuntu Python imports ``threading`` through ``sitecustomize`` /
        ``apport_python_hook`` before any user code runs (measured on
        UM-Cloud-P1: ``apport_python_hook``, ``sitecustomize`` and ``threading``
        are all present in a ``-I`` subprocess that only imports
        ``shared.pricing_rules``). That is interpreter startup, not this module.
        The "the source never imports threading" property is covered statically by
        ``test_source_imports_only_pure_stdlib_modules`` above, which reads the
        import statements instead of the interpreter's preloaded table.
        """
        probe = (
            "import json, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import shared.pricing_rules\n"
            "print(json.dumps(sorted(sys.modules)))\n"
        )
        env = {key: value for key, value in os.environ.items()
               if key.upper() != "PYTHONPATH"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        with tempfile.TemporaryDirectory() as isolated_cwd:
            completed = subprocess.run(
                [sys.executable, "-I", "-c", probe, str(AI_GATEWAY_ROOT)],
                cwd=isolated_cwd, env=env, capture_output=True,
                encoding="utf-8", timeout=60)
        self.assertEqual(completed.returncode, 0,
                         "clean interpreter probe failed: "
                         + completed.stderr)
        modules = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertIn("shared.pricing_rules", modules)
        for forbidden in ("usagesrv", "chat2api", "console_adapters",
                          "share_management", "model_sync"):
            self.assertNotIn(forbidden, modules)


class UsagesrvCacheBridgeTests(unittest.TestCase):
    """The host keeps the bounded cache; the shared function stays uncached."""

    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory(prefix="r19-pricing-usagesrv-")
        root = Path(cls._temp.name)
        cls._env = mock.patch.dict(os.environ, {
            "SHARE_MANAGEMENT_CONFIG": str(root / "absent-deployment.json"),
            "SHARE_STORE_FILE": str(root / "share.db"),
            "CONSOLE_STATE_FILE": str(root / "console.sqlite3"),
            "CONSOLE_LOCK_DIR": str(root / "console-locks"),
        })
        cls._env.start()
        import usagesrv
        cls.usagesrv = usagesrv

    @classmethod
    def tearDownClass(cls):
        cls._env.stop()
        cls._temp.cleanup()

    def test_usagesrv_match_model_id_keeps_its_cache_and_agrees_with_shared(self):
        u = self.usagesrv
        model = "openwebui:DeepSeek-V4-Flash-0731"
        candidates = ["deepseek-v4-flash", "gpt-5.6-luna"]
        key = (model, tuple(candidates))
        with u._match_cache_lock:
            u._match_cache.clear()
        try:
            expected = pricing.match_model_id_uncached(model, candidates)
            self.assertEqual(u.match_model_id(model, list(candidates)), expected)
            with u._match_cache_lock:
                self.assertIn(key, u._match_cache)
                self.assertEqual(u._match_cache[key], expected)
            # Poison the cache to prove the host still reads it first.
            with u._match_cache_lock:
                u._match_cache[key] = "cached-sentinel"
            self.assertEqual(u.match_model_id(model, list(candidates)),
                             "cached-sentinel")
            # Non-list/tuple candidates bypass the cache in both implementations.
            self.assertEqual(u.match_model_id(model, frozenset(candidates)),
                             expected)
        finally:
            with u._match_cache_lock:
                u._match_cache.clear()

    def test_usagesrv_keeps_the_bounded_cache_constant(self):
        self.assertEqual(self.usagesrv._MATCH_CACHE_MAX, 4096)


if __name__ == "__main__":
    unittest.main()
