# -*- coding: utf-8 -*-
"""B1 Magpie effective-price synchronization and UM reference cost.

What this file pins:

* the installed Magpie read surface exposes per-token USD catalogue prices and a
  per-million effective price detail; the synchronizer converts the unit once and
  keeps the source unit explicit;
* manual effective prices win over the catalogue, and their tier rules and
  correction source are kept;
* the UM model map is exact: aliases are explicit, never fuzzy same-name
  matches; a duplicate UM key is refused;
* missing components, missing FX and a bad snapshot are explicit, never folded
  to zero; the first failure stays unpriced;
* Q11 cache status is the only authority: missing/unknown is not a measured
  zero, only an explicit reported 0 is, and unreported is a marked upper bound;
* a failed sync keeps the last successful snapshot and reports a separate status;
* the atomic replace failure leaves the old snapshot intact and no temp file;
* history is stable: a record before a newer snapshot's effective time uses the
  older version, not the newest price;
* the share price path (``estimate_record_with_version_and_rate`` and
  ``data/prices.json``) is byte-for-byte unchanged;
* ``usagesrv`` shows the UM reference CNY cost with price/FX versions, labels
  it 「参考」 and never bills thinking tokens twice.

All file inputs are redirected to temp dirs. The real Magpie, the real
``data/prices.json`` and the real ledgers are never written.
"""

import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from console import magpie_prices as magpie

USAGE_SERVICE_PATH = Path(__file__).resolve().parents[1] / "usagesrv.py"
import importlib.util

_SPEC = importlib.util.spec_from_file_location("usage_service_magpie",
                                               USAGE_SERVICE_PATH)
usage_service = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = usage_service
_SPEC.loader.exec_module(usage_service)


NOW1 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc).timestamp()
NOW2 = datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc).timestamp()

CATALOG = {
    "data": [{
        "id": "zcode/glm-test",
        "display_name": "GLM Test",
        "pricing": {
            "prompt": "0.000002",
            "completion": "0.00001",
            "input_cache_read": "0.0000001",
            "input_cache_write": "0.0000025",
        },
    }, {
        "id": "zcode/glm-other",
        "display_name": "GLM Other",
        "pricing": {
            "prompt": "0.000001",
            "completion": "0.000002",
            "input_cache_read": "0.0000001",
            "input_cache_write": "0.0000001",
        },
    }],
}

DETAIL_LIST = """$2/$10 per 1M in/out · zcode/glm-test
  · cache read $0.1, cache write $2.5
  · what its provider lists, else its maker's on models.dev · magpie model price zcode/glm-test <in>,<out>,<cache read>,<cache write> to change it
"""

DETAIL_LIST_ZERO_WRITE = """$2/$10 per 1M in/out · zcode/glm-test
  · cache read $0.1, cache write $0
  · what its provider lists, else its maker's on models.dev · magpie model price zcode/glm-test <in>,<out>,<cache read>,<cache write> to change it
"""

DETAIL_MANUAL = """$3/$15 per 1M in/out · zcode/glm-test
  · cache read $0.3, cache write $0
  · over 200K input tokens, the whole request: $6/$30 in/out, cache $0.6/$0
  · what you said this model costs · magpie model price zcode/glm-test <in>,<out>,<cache read>,<cache write> to change it
"""

CATALOG_MISSING = {
    "data": [{
        "id": "zcode/glm-missing",
        "display_name": "GLM Missing",
        "pricing": {"prompt": "0.000002"},
    }],
}

PRICES_OLD_SHAPE = {
    "checkedAt": "2026-11-01",
    "exchangeRate": 7.2,
    "peak": {"enabled": False, "windows": []},
    "models": {
        "deepseek-v4-flash": {
            "cacheHit": 0.007,
            "cacheMiss": 0.22,
            "output": 0.66,
        },
    },
}


def _record(time, model="GLM-Test", **tokens):
    rec = {"time": time, "model": model}
    rec.update(tokens)
    rec["_ms"] = magpie._parse_ts(time)
    return rec


class _FakeMagpie:
    def __init__(self, catalog=None, details=None, fail=None):
        self.catalog = copy.deepcopy(catalog or CATALOG)
        self.details = dict(details or {})
        self.fail = fail
        self.paths = []

    def fetch(self, url, timeout):
        self.paths.append(url)
        if self.fail:
            raise self.fail
        if url.endswith("/v1/models"):
            return copy.deepcopy(self.catalog)
        if url.endswith("/"):
            return {"version": "0.1.2431"}
        raise AssertionError("unexpected Magpie path: %s" % url)

    def cli(self, args, timeout):
        if self.fail:
            raise self.fail
        return self.details.get(args[-1], DETAIL_LIST)


class MagpiePriceFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.snapshot = self.base / "magpie-prices.snapshot.json"
        self.status = self.base / "magpie-prices.status.json"
        self.fake = _FakeMagpie()
        self._saved_constants = {
            name: getattr(usage_service, name) for name in (
                "PRICE_FILE", "OVERRIDE_FILE", "CHAT2API_LOG",
                "DSH_SNAPSHOT_FILE", "MAGPIE_PRICE_SNAPSHOT_FILE",
                "MAGPIE_PRICE_STATUS_FILE")}
        self.addCleanup(self._restore_constants)

    def _restore_constants(self):
        for name, value in self._saved_constants.items():
            setattr(usage_service, name, value)
        usage_service._price_cache["at"] = 0.0
        usage_service._price_cache["data"] = None

    def config(self, model_map=None, **overrides):
        values = {
            "base_url": "http://127.0.0.1:3425",
            "model_map": model_map or {"GLM-Test": "zcode/glm-test"},
            "snapshot_path": str(self.snapshot),
            "status_path": str(self.status),
            "cli_path": "C:/fake/magpie.exe",
            "exchange_rate": 8.0,
            "fx_version": "fx-2026-11-01",
        }
        values.update(overrides)
        return magpie.SyncConfig(**values)

    def sync(self, config=None, now=NOW1):
        return magpie.sync(config or self.config(), fetch=self.fake.fetch,
                           cli_runner=self.fake.cli, now=now)

    def prices_for_usage(self):
        self.prices = self.base / "prices.json"
        self.override = self.base / "price.override.json"
        self.prices.write_text(json.dumps(PRICES_OLD_SHAPE), encoding="utf-8")
        usage_service.PRICE_FILE = str(self.prices)
        usage_service.OVERRIDE_FILE = str(self.override)
        usage_service._price_cache["at"] = 0.0
        usage_service._price_cache["data"] = None
        return self.prices


class UnitAndManualPriceTest(MagpiePriceFixture):
    def test_catalog_per_token_values_become_per_million(self):
        self.sync(self.config(cli_path=None))
        snapshot = magpie.load_snapshot(str(self.snapshot))
        entry = snapshot["versions"][-1]["models"]["GLM-Test"]
        self.assertEqual(entry["unit"], "per_million_tokens")
        self.assertEqual(entry["currency"], "USD")
        self.assertEqual(entry["sourceUnit"], "per_token")
        self.assertEqual(entry["input"], 2.0)
        self.assertEqual(entry["output"], 10.0)
        self.assertEqual(entry["cacheRead"], 0.1)
        self.assertEqual(entry["cacheWrite"], 2.5)
        self.assertEqual(entry["magpieModel"], "zcode/glm-test")
        self.assertEqual(entry["priceSource"], "magpie-catalog")
        self.assertTrue(entry["complete"])

    def test_manual_effective_price_wins_and_tier_is_kept(self):
        self.fake.details = {"zcode/glm-test": DETAIL_MANUAL}
        self.sync()
        snapshot = magpie.load_snapshot(str(self.snapshot))
        entry = snapshot["versions"][-1]["models"]["GLM-Test"]
        self.assertTrue(entry["manualPrice"])
        self.assertEqual(entry["correctionSource"], "magpie-manual")
        self.assertEqual(entry["priceSource"], "magpie-cli-effective")
        self.assertEqual(entry["input"], 3.0)
        self.assertEqual(entry["output"], 15.0)
        self.assertEqual(entry["cacheRead"], 0.3)
        self.assertEqual(entry["cacheWrite"], 0.0)
        self.assertTrue(entry["catalogMismatch"])
        self.assertEqual(entry["tiers"], [{
            "overInputTokens": 200000,
            "input": 6.0,
            "output": 30.0,
            "cacheRead": 0.6,
            "cacheWrite": 0.0,
        }])
        self.assertTrue(entry["tiersReported"])

        record = _record("2026-11-01T00:10:00Z", input_tokens=300000,
                         output_tokens=1000, cached_input_tokens=0,
                         cache_report_status="reported", reasoning_tokens=500)
        result = magpie.compute_reference(
            [record], str(self.snapshot), str(self.status))
        self.assertEqual(result["cny"], 14.64)
        self.assertEqual(result["costUsd"], 1.83)
        self.assertEqual(result["coverage"]["pricedRequests"], 1)
        self.assertEqual(result["coverage"]["estimatedRequests"], 0)

    def test_thinking_tokens_are_not_billed_again(self):
        self.fake.details = {"zcode/glm-test": DETAIL_MANUAL}
        self.sync()
        record = _record("2026-11-01T00:10:00Z", input_tokens=100000,
                         output_tokens=1000, cached_input_tokens=0,
                         cache_report_status="reported", reasoning_tokens=500)
        result = magpie.compute_reference(
            [record], str(self.snapshot), str(self.status))
        # 0.1M input * $3 + 1K output * $15 = $0.315; reasoning is part
        # of output and is not added a second time.
        self.assertEqual(result["costUsd"], 0.315)
        self.assertEqual(result["cny"], 2.52)
        self.assertEqual(result["label"], "参考")
        self.assertEqual(result["fx"], {"rate": 8.0,
                                      "version": "fx-2026-11-01"})


class MappingAndAliasTest(MagpiePriceFixture):
    def test_alias_conflict_is_explicit_and_exact(self):
        mapping = {
            "GLM-A": "zcode/glm-test",
            "GLM-B": "zcode/glm-test",
            "GLM-Other": "zcode/glm-other",
        }
        self.fake.details = {"zcode/glm-test": DETAIL_MANUAL}
        self.sync(self.config(model_map=mapping))
        snapshot = magpie.load_snapshot(str(self.snapshot))
        models = snapshot["versions"][-1]["models"]
        self.assertEqual(set(models["GLM-A"]["aliases"]),
                         {"GLM Test", "GLM-A", "GLM-B"})
        self.assertEqual(set(models["GLM-B"]["aliases"]),
                         {"GLM Test", "GLM-A", "GLM-B"})
        self.assertEqual(models["GLM-Other"]["magpieModel"],
                         "zcode/glm-other")
        self.assertNotIn("GLM-A", models["GLM-Other"]["aliases"])

        records = [
            _record("2026-11-01T00:10:00Z", model="GLM-A",
                     input_tokens=100000, output_tokens=0,
                     cached_input_tokens=0, cache_report_status="reported"),
            _record("2026-11-01T00:10:00Z", model="GLM-Unmapped",
                     input_tokens=100000, output_tokens=0,
                     cached_input_tokens=0, cache_report_status="reported"),
        ]
        result = magpie.compute_reference(
            records, str(self.snapshot), str(self.status))
        by_model = {row["model"]: row for row in result["models"]}
        self.assertEqual(by_model["GLM-A"]["magpieModel"],
                         "zcode/glm-test")
        self.assertEqual(by_model["GLM-A"]["pricedRequests"], 1)
        self.assertEqual(by_model["GLM-Unmapped"]["pricedRequests"], 0)
        self.assertEqual(result["coverage"]["unpricedRequests"], 1)

    def test_duplicate_um_key_is_refused(self):
        with self.assertRaises(magpie.SyncConfigError) as caught:
            magpie.SyncConfig(
                model_map={"GLM-A": "zcode/glm-test",
                           " GLM-A ": "zcode/glm-other"},
                snapshot_path=str(self.snapshot),
                exchange_rate=8.0)
        self.assertEqual(caught.exception.code, "magpie_model_map_duplicate")


class MissingDataTest(MagpiePriceFixture):
    def test_missing_component_stays_unknown_not_zero(self):
        self.fake.catalog = CATALOG_MISSING
        self.fake.details = {}
        result = self.sync(self.config(
            model_map={"GLM-Missing": "zcode/glm-missing"}, cli_path=None))
        self.assertEqual(result["unpricedModels"], ["GLM-Missing"])
        snapshot = magpie.load_snapshot(str(self.snapshot))
        entry = snapshot["versions"][-1]["models"]["GLM-Missing"]
        self.assertIsNone(entry["output"])
        self.assertIsNone(entry["cacheRead"])
        self.assertIsNone(entry["cacheWrite"])
        self.assertFalse(entry["complete"])

        record = _record("2026-11-01T00:10:00Z", model="GLM-Missing",
                         input_tokens=1000000, output_tokens=0,
                         cached_input_tokens=0,
                         cache_report_status="reported")
        reference = magpie.compute_reference(
            [record], str(self.snapshot), str(self.status))
        self.assertIsNone(reference["cny"])
        self.assertEqual(reference["coverage"]["unpricedRequests"], 1)
        self.assertEqual(reference["models"][0]["output"], None)

    def test_missing_fx_keeps_usd_but_cny_unknown(self):
        self.fake.details = {"zcode/glm-test": DETAIL_MANUAL}
        with mock.patch.object(magpie, "_load_fx", return_value=(None, None)):
            result = self.sync(self.config())
        self.assertTrue(result["ok"])
        self.assertIsNone(result["exchangeRate"])
        snapshot = magpie.load_snapshot(str(self.snapshot))
        self.assertFalse(snapshot["versions"][-1]["fxAvailable"])
        record = _record("2026-11-01T00:10:00Z",
                         input_tokens=1000000, output_tokens=0,
                         cached_input_tokens=0,
                         cache_report_status="reported")
        reference = magpie.compute_reference(
            [record], str(self.snapshot), str(self.status))
        self.assertIsNone(reference["cny"])
        self.assertIsNone(reference["fx"]["rate"])
        self.assertGreater(reference["costUsd"], 0)
        self.assertEqual(reference["coverage"]["fxMissingRequests"], 1)
        self.assertTrue(reference["cnyPartial"])

    def test_invalid_explicit_fx_is_refused(self):
        with self.assertRaises(magpie.SyncConfigError) as caught:
            magpie.SyncConfig(model_map={"A": "B"},
                              snapshot_path=str(self.snapshot),
                              exchange_rate=0)
        self.assertEqual(caught.exception.code, "magpie_fx_invalid")

    def test_bad_snapshot_is_explicit_and_does_not_crash(self):
        self.snapshot.write_text("{not json", encoding="utf-8")
        with self.assertRaises(magpie.SnapshotError) as caught:
            magpie.load_snapshot(str(self.snapshot))
        self.assertEqual(caught.exception.code, "snapshot_corrupt")
        record = _record("2026-11-01T00:10:00Z",
                         input_tokens=1000000, output_tokens=0,
                         cached_input_tokens=0)
        reference = magpie.compute_reference(
            [record], str(self.snapshot), str(self.status))
        self.assertEqual(reference["price"]["state"], "failed")
        self.assertIsNone(reference["cny"])
        self.assertIn("损坏", reference["price"]["failure"])

    def test_first_sync_without_a_snapshot_is_unpriced(self):
        record = _record("2026-11-01T00:10:00Z",
                         input_tokens=1000000, output_tokens=0,
                         cached_input_tokens=0)
        reference = magpie.compute_reference(
            [record], str(self.snapshot), str(self.status))
        self.assertEqual(reference["price"]["state"], "unsynced")
        self.assertIsNone(reference["cny"])
        self.assertIsNone(reference["price"]["source"])
        self.assertIsNone(reference["fx"]["rate"])
        self.assertEqual(reference["coverage"]["unpricedRequests"], 1)
        self.assertEqual(reference["coverage"]["records"], 1)


class CacheReportStatusTest(MagpiePriceFixture):
    """Q11: only an explicit ``reported`` status is a measured cache zero.

    A missing or unrecognised status is historical/unknown: the compatibility
    ``cached_input_tokens`` value must not be repriced as a measured cache hit.
    ``unreported`` is priced as a cache-miss upper bound and stays marked.
    """

    def setUp(self):
        super().setUp()
        self.fake.details = {"zcode/glm-test": DETAIL_LIST_ZERO_WRITE}
        result = self.sync()
        self.assertTrue(result["ok"])

    def _reference(self, **tokens):
        record = _record("2026-11-01T00:10:00Z", input_tokens=100000,
                         output_tokens=1000, cached_write_tokens=0,
                         **tokens)
        return magpie.compute_reference(
            [record], str(self.snapshot), str(self.status))

    def test_missing_status_is_unknown_cache_not_zero(self):
        result = self._reference(cached_input_tokens=0)
        self.assertEqual(result["coverage"]["pricedRequests"], 0)
        self.assertEqual(result["coverage"]["unpricedRequests"], 1)
        self.assertEqual(result["coverage"]["cacheUnknownRequests"], 1)
        self.assertEqual(result["coverage"]["cacheUnreportedRequests"], 0)
        self.assertEqual(result["coverage"]["estimatedRequests"], 0)
        self.assertEqual(result["costUsd"], 0.0)
        self.assertIsNone(result["cny"])
        self.assertTrue(result["cnyPartial"])

    def test_historical_compat_value_without_status_is_not_repriced(self):
        # A legacy row may carry a compatibility cached_input_tokens value but
        # no Q11 status: it must not be recalculated as a measured cache hit.
        result = self._reference(cached_input_tokens=50000)
        self.assertEqual(result["coverage"]["pricedRequests"], 0)
        self.assertEqual(result["coverage"]["unpricedRequests"], 1)
        self.assertEqual(result["coverage"]["cacheUnknownRequests"], 1)
        self.assertEqual(result["costUsd"], 0.0)
        self.assertIsNone(result["cny"])

    def test_unknown_status_is_unknown_cache_not_zero(self):
        result = self._reference(cached_input_tokens=0,
                                 cache_report_status="unknown")
        self.assertEqual(result["coverage"]["pricedRequests"], 0)
        self.assertEqual(result["coverage"]["unpricedRequests"], 1)
        self.assertEqual(result["coverage"]["cacheUnknownRequests"], 1)
        self.assertEqual(result["coverage"]["cacheUnreportedRequests"], 0)
        self.assertEqual(result["costUsd"], 0.0)
        self.assertIsNone(result["cny"])

    def test_reported_zero_cache_is_a_measured_zero(self):
        result = self._reference(cached_input_tokens=0,
                                 cache_report_status="reported")
        self.assertEqual(result["coverage"]["pricedRequests"], 1)
        self.assertEqual(result["coverage"]["cacheReportedRequests"], 1)
        self.assertEqual(result["coverage"]["cacheUnknownRequests"], 0)
        self.assertEqual(result["coverage"]["estimatedRequests"], 0)
        # 0.1M input at $2 + 1K output at $10 = $0.21; the reported zero
        # cache is a measured zero, not an unknown cache component.
        self.assertEqual(result["costUsd"], 0.21)
        self.assertEqual(result["cny"], 1.68)
        self.assertFalse(result["cnyPartial"])

    def test_reported_cache_uses_the_discount(self):
        result = self._reference(cached_input_tokens=50000,
                                 cache_report_status="reported")
        self.assertEqual(result["coverage"]["pricedRequests"], 1)
        self.assertEqual(result["coverage"]["cacheReportedRequests"], 1)
        self.assertEqual(result["coverage"]["estimatedRequests"], 0)
        # 0.05M miss at $2 + 0.05M hit at $0.1 + 1K output at $10 = $0.115.
        self.assertEqual(result["costUsd"], 0.115)
        self.assertEqual(result["cny"], 0.92)
        self.assertFalse(result["cnyPartial"])

    def test_unreported_cache_is_an_upper_bound_not_a_zero(self):
        result = self._reference(cached_input_tokens=0,
                                 cache_report_status="unreported")
        self.assertEqual(result["coverage"]["pricedRequests"], 1)
        self.assertEqual(result["coverage"]["cacheUnreportedRequests"], 1)
        self.assertEqual(result["coverage"]["cacheUnknownRequests"], 0)
        self.assertEqual(result["coverage"]["estimatedRequests"], 1)
        # The compatibility zero is not charged as a cache hit: 0.1M input at
        # cache-miss $2 + 1K output at $10 = $0.21, an upper bound.
        self.assertEqual(result["costUsd"], 0.21)
        self.assertEqual(result["cny"], 1.68)
        self.assertTrue(result["cnyPartial"])


class HistoryAndFailureTest(MagpiePriceFixture):
    def test_history_uses_the_version_in_effect_not_the_newest(self):
        self.fake.details = {"zcode/glm-test": DETAIL_MANUAL}
        self.sync(self.config(), now=NOW1)
        self.fake.details = {"zcode/glm-test": DETAIL_LIST_ZERO_WRITE}
        self.sync(self.config(exchange_rate=7.2,
                               fx_version="fx-2026-11-02"), now=NOW2)
        snapshot = magpie.load_snapshot(str(self.snapshot))
        self.assertEqual(len(snapshot["versions"]), 2)

        old_record = _record("2026-11-01T00:10:00Z",
                              input_tokens=100000, output_tokens=1000,
                              cached_input_tokens=0,
                              cache_report_status="reported")
        new_record = _record("2026-11-01T01:10:00Z",
                              input_tokens=100000, output_tokens=1000,
                              cached_input_tokens=0,
                              cache_report_status="reported")
        old_reference = magpie.compute_reference(
            [old_record], str(self.snapshot), str(self.status))
        new_reference = magpie.compute_reference(
            [new_record], str(self.snapshot), str(self.status))
        self.assertEqual(old_reference["cny"], 2.52)
        self.assertEqual(old_reference["price"]["version"],
                         snapshot["versions"][0]["version"])
        self.assertEqual(new_reference["cny"], 1.512)
        self.assertEqual(new_reference["price"]["version"],
                         snapshot["versions"][1]["version"])
        self.assertNotEqual(old_reference["price"]["version"],
                            new_reference["price"]["version"])

    def test_record_before_every_version_is_unknown(self):
        self.sync(self.config(), now=NOW2)
        record = _record("2026-11-01T00:10:00Z",
                         input_tokens=100000, output_tokens=1000,
                         cached_input_tokens=0)
        reference = magpie.compute_reference(
            [record], str(self.snapshot), str(self.status))
        self.assertEqual(reference["price"]["state"], "synced")
        self.assertIsNone(reference["cny"])
        self.assertEqual(reference["coverage"]["unknownEffectiveRequests"], 1)
        self.assertEqual(reference["coverage"]["unpricedRequests"], 1)

    def test_failed_sync_keeps_last_successful_snapshot_and_status(self):
        self.fake.details = {"zcode/glm-test": DETAIL_MANUAL}
        first = self.sync(self.config(), now=NOW1)
        self.assertTrue(first["ok"])
        before = self.snapshot.read_bytes()
        self.fake.fail = magpie.MagpiePriceError(
            "magpie_unreachable", "Magpie is offline")
        second = self.sync(self.config(), now=NOW2)
        self.assertFalse(second["ok"])
        self.assertEqual(second["state"], "failed")
        self.assertEqual(second["version"], first["version"])
        self.assertEqual(self.snapshot.read_bytes(), before)
        status = magpie.load_status(str(self.status))
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["snapshotVersion"], first["version"])
        self.assertIn("offline", status["failure"])

        record = _record("2026-11-01T00:10:00Z",
                         input_tokens=100000, output_tokens=1000,
                         cached_input_tokens=0,
                         cache_report_status="reported")
        reference = magpie.compute_reference(
            [record], str(self.snapshot), str(self.status))
        self.assertEqual(reference["price"]["state"], "failed")
        self.assertEqual(reference["cny"], 2.52)
        self.assertIn("offline", reference["price"]["failure"])

    def test_atomic_replace_failure_keeps_old_snapshot(self):
        first = self.sync(self.config(), now=NOW1)
        self.assertTrue(first["ok"])
        before = self.snapshot.read_bytes()
        real_replace = os.replace

        def replace(src, dst):
            if Path(dst) == self.snapshot:
                raise OSError("disk full")
            return real_replace(src, dst)

        self.fake.details = {"zcode/glm-test": DETAIL_MANUAL}
        with mock.patch.object(magpie.os, "replace", side_effect=replace):
            second = self.sync(self.config(), now=NOW2)
        self.assertFalse(second["ok"])
        self.assertEqual(self.snapshot.read_bytes(), before)
        status = magpie.load_status(str(self.status))
        self.assertEqual(status["state"], "failed")
        self.assertEqual(list(self.base.glob("magpie-prices.snapshot.json.*.tmp")), [])

    def test_backup_manifest_includes_every_version(self):
        first = self.sync(self.config(), now=NOW1)
        second = self.sync(self.config(), now=NOW2)
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        manifest = magpie.backup_manifest(str(self.snapshot), str(self.status))
        self.assertEqual(len(manifest["versions"]), 2)
        self.assertEqual(manifest["current"]["version"], second["version"])
        self.assertEqual(manifest["versions"][0]["version"], first["version"])
        roles = {item["role"] for item in manifest["files"]}
        self.assertEqual(roles, {"snapshot", "status"})
        for item in manifest["files"]:
            self.assertTrue(item["sha256"])
        for item in manifest["versions"]:
            self.assertTrue(item["sha256"])

    def test_share_price_path_is_unchanged(self):
        prices = self.prices_for_usage()
        before = prices.read_bytes()
        before_estimate = usage_service.estimate_record_with_version_and_rate(
            "deepseek-v4-flash", NOW1, 1000000, 0, 0, 0)
        self.sync(self.config())
        after_estimate = usage_service.estimate_record_with_version_and_rate(
            "deepseek-v4-flash", NOW1, 1000000, 0, 0, 0)
        self.assertEqual(before_estimate, after_estimate)
        self.assertEqual(prices.read_bytes(), before)
        self.assertFalse(self.override.exists())
        self.assertTrue(self.snapshot.exists())


class UsageServiceReferenceTest(MagpiePriceFixture):
    def _prepare_summary_inputs(self):
        self.prices_for_usage()
        self.chat = self.base / "chat2api.jsonl"
        self.dsh = self.base / "dsh.json"
        self.dsh.write_text("{}", encoding="utf-8")
        record = _record("2026-11-01T00:10:00Z", input_tokens=100000,
                         output_tokens=1000, cached_input_tokens=0,
                         cache_report_status="reported", reasoning_tokens=500)
        self.chat.write_text(json.dumps(record) + "\n", encoding="utf-8")
        usage_service.PRICE_FILE = str(self.prices)
        usage_service.OVERRIDE_FILE = str(self.override)
        usage_service.CHAT2API_LOG = str(self.chat)
        usage_service.DSH_SNAPSHOT_FILE = str(self.dsh)
        usage_service.MAGPIE_PRICE_SNAPSHOT_FILE = str(self.snapshot)
        usage_service.MAGPIE_PRICE_STATUS_FILE = str(self.status)
        usage_service._price_cache["at"] = 0.0
        usage_service._price_cache["data"] = None

    def test_usagesrv_reads_snapshot_and_labels_reference(self):
        self._prepare_summary_inputs()
        self.fake.details = {"zcode/glm-test": DETAIL_MANUAL}
        self.sync()
        with mock.patch.object(usage_service, "read_dsh_ledger",
                                  return_value={
                                      "kind": "dsh-ledger",
                                      "pricingCurrency": "USD",
                                      "requests": 0, "costUsd": 0.0,
                                      "tokens": {"input": 0, "output": 0,
                                                 "cache": 0, "reasoning": 0},
                                      "models": [], "days": [],
                                      "unpricedModels": []}):
            payload = usage_service.build_summary()
        reference = payload["umReference"]
        self.assertEqual(reference["label"], "参考")
        self.assertEqual(reference["cny"], 2.52)
        self.assertEqual(reference["costUsd"], 0.315)
        self.assertEqual(reference["price"]["state"], "synced")
        snapshot = magpie.load_snapshot(str(self.snapshot))
        self.assertEqual(reference["price"]["version"],
                         snapshot["versions"][-1]["version"])
        self.assertEqual(reference["fx"], {"rate": 8.0,
                                         "version": "fx-2026-11-01"})
        self.assertEqual(reference["coverage"]["unpricedRequests"], 0)
        self.assertEqual(reference["coverage"]["estimatedRequests"], 0)
        # The share path still prices with data/prices.json, not Magpie.
        self.assertIsNone(payload["services"]["chat2api"]["costCny"])

    def test_usagesrv_cloud_role_does_not_read_local_ledger(self):
        with mock.patch.object(usage_service, "_read_jsonl",
                               side_effect=AssertionError("ledger read")):
            payload = usage_service._build_cloud_summary()
        reference = payload["umReference"]
        self.assertEqual(reference["price"]["state"], "unsynced")
        self.assertIsNone(reference["cny"])
        self.assertIsNone(reference["coverage"]["records"])
        self.assertIsNone(reference["coverage"]["unpricedRequests"])


class HttpReadSurfaceTest(MagpiePriceFixture):
    def test_http_read_surface_has_no_credentials(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path,
                                 self.headers.get("Authorization")))
                if self.path == "/v1/models":
                    body = json.dumps(CATALOG).encode("utf-8")
                elif self.path == "/":
                    body = json.dumps({"version": "0.1.2431"}).encode("utf-8")
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        config = self.config(
            base_url="http://127.0.0.1:%d" % port, cli_path=None)
        result = magpie.sync(config, now=NOW1)
        self.assertTrue(result["ok"])
        self.assertEqual([path for path, _ in requests], ["/v1/models", "/"])
        self.assertEqual([auth for _, auth in requests], [None, None])

    def test_non_loopback_url_is_refused(self):
        with self.assertRaises(magpie.SyncConfigError) as caught:
            magpie.SyncConfig(
                base_url="http://192.0.2.1:3425",
                model_map={"A": "B"},
                snapshot_path=str(self.snapshot),
                exchange_rate=8.0)
        self.assertEqual(caught.exception.code, "magpie_url_not_loopback")

    def test_unparsed_detail_is_explicit(self):
        parsed = magpie.parse_price_detail("not a price line")
        self.assertIsNone(parsed["base"])
        self.assertFalse(parsed["detailComplete"])
        self.assertEqual(parsed["detailError"], "base_not_parsed")


class CliSyncTests(MagpiePriceFixture):
    """``console/magpie_prices.py --sync`` is the operator/deployment entry.

    install.sh leaves the first encrypted checkpoint fail-closed until a Magpie
    snapshot exists; this CLI reads the deployment configuration (env or
    ``console.env``) and writes the snapshot/status the backup timer consumes.
    """

    def _run_main(self, argv):
        captured = io.StringIO()
        with mock.patch.object(magpie, "_fetch_json",
                               side_effect=self.fake.fetch), \
                contextlib.redirect_stdout(captured):
            code = magpie._main(argv)
        return code, captured.getvalue()

    def _env(self, **overrides):
        env = {
            "MAGPIE_PRICE_URL": "http://127.0.0.1:3425",
            "MAGPIE_PRICE_MODEL_MAP": json.dumps(
                {"GLM-Test": "zcode/glm-test"}),
            "MAGPIE_PRICE_SNAPSHOT": str(self.snapshot),
            "MAGPIE_PRICE_STATUS": str(self.status),
            "MAGPIE_PRICE_FX_RATE": "8.0",
            "MAGPIE_PRICE_FX_VERSION": "fx-2026-11-01",
        }
        env.update(overrides)
        return env

    def test_cli_reads_env_config_and_writes_snapshot_and_status(self):
        with mock.patch.dict(os.environ, self._env(), clear=True):
            code, output = self._run_main(["--sync", "--json"])
        self.assertEqual(code, 0, output)
        result = json.loads(output)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "synced")
        self.assertEqual(result["snapshotPath"], str(self.snapshot))
        self.assertEqual(result["statusPath"], str(self.status))
        snapshot = magpie.load_snapshot(str(self.snapshot))
        status = magpie.load_status(str(self.status))
        self.assertEqual(
            snapshot["versions"][-1]["models"]["GLM-Test"]["input"], 2.0)
        self.assertEqual(status["state"], "synced")
        self.assertEqual(status["snapshotVersion"], result["version"])

    def test_cli_snapshot_override_derives_status_from_it(self):
        env = self._env()
        env.pop("MAGPIE_PRICE_STATUS")
        override = self.base / "override-snapshot.json"
        with mock.patch.dict(os.environ, env, clear=True):
            code, output = self._run_main(
                ["--sync", "--json", "--snapshot", str(override)])
        self.assertEqual(code, 0, output)
        result = json.loads(output)
        self.assertEqual(result["snapshotPath"], str(override))
        self.assertEqual(
            result["statusPath"],
            str(self.base / "override-snapshot.status.json"))
        self.assertTrue(override.is_file())
        self.assertTrue((self.base / "override-snapshot.status.json").is_file())

    def test_cli_invalid_env_map_is_structured_not_a_traceback(self):
        env = self._env(MAGPIE_PRICE_MODEL_MAP="{not-json")
        with mock.patch.dict(os.environ, env, clear=True):
            code, output = self._run_main(["--sync", "--json"])
        self.assertEqual(code, 1)
        result = json.loads(output)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "magpie_model_map_invalid")
        self.assertIn("MAGPIE_PRICE_MODEL_MAP", result["failure"])

    def test_cli_runs_as_a_script_against_a_local_magpie(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/v1/models":
                    body = json.dumps(CATALOG).encode("utf-8")
                elif self.path == "/":
                    body = b'{"version": "0.1.2431"}'
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        env = dict(os.environ)
        env.update(self._env(
            MAGPIE_PRICE_URL="http://127.0.0.1:%d"
            % server.server_address[1]))
        proc = subprocess.run(
            [sys.executable, str(Path(magpie.__file__)), "--sync", "--json"],
            capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads(proc.stdout)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "synced")
        self.assertTrue(self.snapshot.is_file())
        self.assertTrue(self.status.is_file())
        self.assertEqual(
            magpie.load_status(str(self.status))["snapshotVersion"],
            result["version"])


class NewRequestEligibilityTest(MagpiePriceFixture):
    """A2-8: the request START is the first-effective-snapshot boundary.

    Version selection itself stays on the request COMPLETION time (``time``), as
    the confirmed r2 contract says. Only a request that STARTED before the FIRST
    effective snapshot is unpriced; a historical row without start evidence keeps
    its previous completion-time pricing unchanged.
    """

    def setUp(self):
        super().setUp()
        self.fake.details = {"zcode/glm-test": DETAIL_MANUAL}
        result = self.sync(self.config(), now=NOW2)
        self.assertTrue(result["ok"])

    def _reference(self, **record):
        return magpie.compute_reference(
            [_record(**record)], str(self.snapshot), str(self.status))

    def test_started_before_the_first_snapshot_stays_unpriced(self):
        reference = self._reference(
            time="2026-11-01T01:10:00Z",
            request_started_at="2026-11-01T00:30:00Z",
            input_tokens=100000, output_tokens=1000,
            cached_input_tokens=0, cache_report_status="reported")
        self.assertIsNone(reference["cny"])
        self.assertEqual(reference["coverage"]["preEffectiveRequests"], 1)
        self.assertEqual(reference["coverage"]["unpricedRequests"], 1)
        self.assertEqual(reference["coverage"]["unknownEffectiveRequests"], 1)
        self.assertEqual(reference["models"][0]["preEffectiveRequests"], 1)
        self.assertEqual(reference["models"][0]["pricedRequests"], 0)

    def test_started_after_the_first_snapshot_uses_the_completion_version(self):
        reference = self._reference(
            time="2026-11-01T01:10:00Z",
            request_started_at="2026-11-01T01:05:00Z",
            input_tokens=100000, output_tokens=1000,
            cached_input_tokens=0, cache_report_status="reported")
        self.assertEqual(reference["cny"], 2.52)
        self.assertEqual(reference["coverage"]["pricedRequests"], 1)
        self.assertEqual(reference["coverage"]["preEffectiveRequests"], 0)
        self.assertEqual(reference["price"]["version"],
                         magpie.load_snapshot(str(self.snapshot))[
                             "versions"][-1]["version"])

    def test_historical_row_without_start_keeps_completion_pricing(self):
        reference = self._reference(
            time="2026-11-01T01:10:00Z",
            input_tokens=100000, output_tokens=1000,
            cached_input_tokens=0, cache_report_status="reported")
        self.assertEqual(reference["cny"], 2.52)
        self.assertEqual(reference["coverage"]["pricedRequests"], 1)
        self.assertEqual(reference["coverage"]["preEffectiveRequests"], 0)

    def test_completion_time_selects_a_later_version(self):
        self.sync(self.config(), now=NOW1)
        self.fake.details = {"zcode/glm-test": DETAIL_LIST_ZERO_WRITE}
        self.sync(self.config(exchange_rate=7.2,
                              fx_version="fx-2026-11-02"), now=NOW2)
        reference = self._reference(
            time="2026-11-01T01:10:00Z",
            request_started_at="2026-11-01T00:10:00Z",
            input_tokens=100000, output_tokens=1000,
            cached_input_tokens=0, cache_report_status="reported")
        snapshot = magpie.load_snapshot(str(self.snapshot))
        self.assertEqual(reference["price"]["version"],
                         snapshot["versions"][-1]["version"])
        self.assertEqual(reference["cny"], 1.512)

    def test_new_request_without_fx_is_unknown_not_zero(self):
        with mock.patch.object(magpie, "_load_fx", return_value=(None, None)):
            self.sync(self.config(), now=NOW2)
        reference = self._reference(
            time="2026-11-01T01:10:00Z",
            request_started_at="2026-11-01T01:05:00Z",
            input_tokens=100000, output_tokens=1000,
            cached_input_tokens=0, cache_report_status="reported")
        self.assertIsNone(reference["cny"])
        self.assertGreater(reference["costUsd"], 0)
        self.assertEqual(reference["coverage"]["fxMissingRequests"], 1)
        self.assertTrue(reference["cnyPartial"])

    def test_unpriced_new_request_stays_unknown_not_zero(self):
        reference = self._reference(
            time="2026-11-01T01:10:00Z",
            request_started_at="2026-11-01T01:05:00Z",
            model="GLM-Unmapped",
            input_tokens=100000, output_tokens=1000,
            cached_input_tokens=0, cache_report_status="reported")
        self.assertIsNone(reference["cny"])
        self.assertEqual(reference["coverage"]["unpricedRequests"], 1)
        self.assertEqual(reference["models"][0]["pricedRequests"], 0)

    def test_share_price_file_is_not_read_for_fx(self):
        self.prices_for_usage()
        share = json.loads(self.prices.read_text(encoding="utf-8"))
        share["exchangeRate"] = 123.0
        self.prices.write_text(json.dumps(share), encoding="utf-8")
        with mock.patch.dict(os.environ, {
                "USAGE_PRICE_FILE": str(self.prices),
                "MAGPIE_PRICE_FX_FILE": ""}):
            self.sync(self.config(exchange_rate=None,
                                  fx_version=None), now=NOW2)
        reference = self._reference(
            time="2026-11-01T01:10:00Z",
            request_started_at="2026-11-01T01:05:00Z",
            input_tokens=100000, output_tokens=1000,
            cached_input_tokens=0, cache_report_status="reported")
        self.assertIsNone(reference["cny"])
        self.assertEqual(reference["coverage"]["fxMissingRequests"], 1)
        self.assertIsNone(reference["fx"]["rate"])


if __name__ == "__main__":
    unittest.main()
