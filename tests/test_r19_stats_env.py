# -*- coding: utf-8 -*-
"""R19 StatsEnvironment: console_stats reads only an injected environment.

The local statistics reader must not reach back into the usagesrv host module:
the host builds a StatsEnvironment from its own path constants and price
loader and passes it explicitly (or installs it once with ``configure``). These
cases pin the three contract points of E19-1-05:

* importing console_stats does not import usagesrv;
* a fixture ledger + fixture price table return the hand-computed request,
  token and money totals through bounded_stats;
* an unconfigured process raises StatsNotConfigured instead of guessing paths.

Every fixture lives in a temp directory; no real ledger, price table, service
log or console database is read.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import console_stats  # noqa: E402
from local.backend import stats_env  # noqa: E402
from shared import pricing_rules  # noqa: E402

GATEWAY_DIR = Path(__file__).resolve().parents[1]

# Fixture money expectation, hand-computed from the fixture price table:
#   input 1.0 USD/Mtok, output 2.0 USD/Mtok, exchangeRate 7.2
#   1M input + 0.5M output = 1.0 + 1.0 = 2.0 USD = 14.4 CNY
FIXTURE_MODEL = "fixture-model"
FIXTURE_RATE = 7.2
EXPECTED_COST_CNY = 14.4

# A document shaped like the real ``data/prices.json``: the top-level models
# are stale, and the CURRENT segment (effective 2026-09-29) carries the model
# the ledger actually uses. Ignoring ``versions`` would mark it unpriced.
VERSIONED_PRICE_DOC = {
    "exchangeRate": 7.2,
    "checkedAt": "2026-11-01",
    "models": {"legacy-model": {"input": 1.0, "output": 2.0}},
    "versions": [
        {
            "versionId": "v1",
            "effectiveAt": "2026-08-01T00:00:00+08:00",
            "exchangeRate": 7.2,
            "checkedAt": "2026-09-01",
            "models": {"legacy-model": {"input": 1.0, "output": 2.0}},
        },
        {
            "versionId": "v2",
            "effectiveAt": "2026-09-29T17:00:28+08:00",
            "exchangeRate": 8.0,
            "checkedAt": "2026-11-01",
            "models": {FIXTURE_MODEL: {"input": 10.0, "output": 20.0}},
        },
    ],
}


def _fixture_price_table():
    """The normalized shape usagesrv.load_price_table returns (flat table)."""
    tier = pricing_rules.normalize_price({"input": 1.0, "output": 2.0})
    models = {FIXTURE_MODEL: tier}
    peak = {"enabled": False, "effectiveAtMs": None, "windows": []}
    version = {
        "versionId": None,
        "effectiveAt": None,
        "effectiveAtMs": None,
        "exchangeRate": FIXTURE_RATE,
        "peak": peak,
        "tiers": [(models, "flat", "fixture")],
        "checkedAt": "2026-11-07",
        "checkedAgeDays": 0,
    }
    return {
        "exchangeRate": FIXTURE_RATE,
        "peak": peak,
        "tiers": [(models, "flat", "fixture")],
        "override": {},
        "versions": [version],
        "versioned": False,
        "versionMeta": {"versioned": False, "count": 1,
                        "invalidCount": 0, "current": None},
        "meta": {"pricesFound": True, "checkedAt": "2026-11-07",
                 "checkedAgeDays": 0, "checkedStale": False,
                 "overrideFound": False},
    }


def _fixture_ledger_record():
    return {
        "time": "2026-11-07T10:00:00+08:00",
        "service": "chat2api",
        "model": FIXTURE_MODEL,
        "stream": True,
        "status": "ok",
        "input_tokens": 1_000_000,
        "output_tokens": 500_000,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 1_500_000,
        "estimated": False,
        "duration_ms": 100,
    }


class ImportIsolationTests(unittest.TestCase):
    """console_stats must not import the usagesrv host module."""

    def test_importing_console_stats_does_not_load_usagesrv(self):
        code = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "import console_stats\n"
            "print('usagesrv' in sys.modules)\n"
            "print('console_stats' in sys.modules)\n"
        ) % str(GATEWAY_DIR)
        proc = subprocess.run(
            [sys.executable, "-I", "-c", code],
            cwd=str(GATEWAY_DIR), capture_output=True, text=True,
            timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split(), ["False", "True"], proc.stderr)


class StatsEnvironmentTests(unittest.TestCase):
    """bounded_stats and the default assemblies use the injected environment."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.ledger = self.dir / "usage-chat2api.jsonl"
        self.ledger.write_text(
            json.dumps(_fixture_ledger_record(), ensure_ascii=False) + "\n",
            encoding="utf-8")
        self.price_table = _fixture_price_table()
        self.environment = self._environment()

    def _environment(self):
        def estimate_record(model_id, at_ms, input_tok, output_tok,
                            cache_tok, reasoning_tok):
            return pricing_rules.estimate_record(
                model_id, at_ms, input_tok, output_tok, cache_tok,
                reasoning_tok, self.price_table)

        return console_stats.StatsEnvironment(
            chat2api_log=str(self.ledger),
            dsh_snapshot=str(self.dir / "dsh-snapshot.json"),
            qoder_log=str(self.dir / "qoder-usage.jsonl"),
            m365_log=str(self.dir / "m365-usage.jsonl"),
            dsh_merge_day="2026-09-02",
            magpie_price_snapshot=str(
                self.dir / "magpie-prices.snapshot.json"),
            magpie_price_status=str(
                self.dir / "magpie-prices.status.json"),
            parse_ts=pricing_rules.parse_ts,
            beijing_day=pricing_rules.beijing_day,
            day_start_ms=pricing_rules.day_start_ms,
            display_path=lambda path: (
                os.path.basename(str(path or "")) or "(未配置)"),
            estimate_record=estimate_record,
            price_table_loader=lambda: self.price_table)

    def test_fixture_ledger_and_price_table_return_expected_totals(self):
        result = console_stats.bounded_stats(
            {"chat2api": str(self.ledger)}, "2026-11-07", "2026-11-07",
            environment=self.environment)
        self.assertEqual(result["totals"]["requests"], 1)
        self.assertEqual(result["totals"]["input_tokens"], 1_000_000)
        self.assertEqual(result["totals"]["output_tokens"], 500_000)
        self.assertEqual(result["totals"]["estimated_cost_cny"],
                         EXPECTED_COST_CNY)
        self.assertEqual(result["coverage"]["missing_days"], [])
        self.assertEqual(result["coverage"]["unpriced_models"], [])
        row = result["days"][0]["sources"][0]["models"][0]
        self.assertEqual(row["model"], FIXTURE_MODEL)
        self.assertEqual(row["requests"], 1)
        self.assertEqual(row["input_tokens"], 1_000_000)
        self.assertEqual(row["output_tokens"], 500_000)
        self.assertEqual(row["estimated_cost_cny"], EXPECTED_COST_CNY)

    def test_default_assemblies_read_the_environment(self):
        self.assertEqual(
            console_stats.default_ledgers(self.environment),
            {"chat2api": str(self.ledger),
             "dsh": str(self.dir / "dsh-snapshot.json")})
        self.assertEqual(
            console_stats.default_sources(self.environment),
            {"chat2api": str(self.ledger),
             "dsh": str(self.dir / "dsh-snapshot.json")})
        self.assertEqual(
            console_stats.retired_ledgers(self.environment),
            {"qoder": str(self.dir / "qoder-usage.jsonl"),
             "m365": str(self.dir / "m365-usage.jsonl")})

    def test_configure_installs_the_process_environment(self):
        with mock.patch.object(console_stats, "_STATS_ENVIRONMENT", None):
            with self.assertRaises(console_stats.StatsNotConfigured):
                console_stats.default_ledgers()
            console_stats.configure(self.environment)
            self.assertEqual(console_stats.default_ledgers()["chat2api"],
                             str(self.ledger))
            self.assertEqual(
                console_stats._um_price_paths(),
                (str(self.dir / "magpie-prices.snapshot.json"),
                 str(self.dir / "magpie-prices.status.json")))

    def test_the_local_read_chain_memoizes_the_model_match(self):
        """standards-tests-01: the LOCAL chain must use the memoized matcher.

        ``build_stats_environment`` used to call
        ``pricing_rules.estimate_record`` without ``matcher=``, so the local
        read chain ran the expensive uncached match once per ledger row while the
        host adapter (``usagesrv.estimate_record``) passed
        ``matcher=match_model_id``. The regression test only pinned the host
        adapter, so this path was never observed.
        """
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            ledger = directory / "usage.jsonl"
            models = ["model-a", "model-b", "model-c"]
            ledger.write_text(
                "\n".join(
                    json.dumps({
                        "time": "2026-11-07T10:00:00+08:00",
                        "model": models[index % len(models)],
                        "status": 200,
                        "input_tokens": 1, "output_tokens": 1,
                        "cached_input_tokens": 0, "reasoning_tokens": 0,
                    }, ensure_ascii=False)
                    for index in range(120)) + "\n",
                encoding="utf-8")
            price = directory / "prices.json"
            price.write_text(json.dumps({
                "exchangeRate": 7.2,
                "checkedAt": "2026-11-01",
                "models": {name: {"input": 1.0, "output": 2.0}
                           for name in models},
            }), encoding="utf-8")

            class _Config:
                price_file = str(price)
                price_override_file = None
                ledger_file = str(ledger)
                state_root = str(directory)

            environment = stats_env.build_stats_environment(_Config())
            calls = []
            original = pricing_rules.match_model_id_uncached

            def counting(model_id, candidates):
                calls.append(model_id)
                return original(model_id, candidates)

            with mock.patch.object(
                    pricing_rules, "match_model_id_uncached", counting):
                result = console_stats.bounded_stats(
                    {"chat2api": str(ledger)}, "2026-11-07", "2026-11-07",
                    environment=environment)
        self.assertEqual(result["totals"]["requests"], 120)
        self.assertLessEqual(
            len(calls), 12,
            "the local read chain re-ran the expensive match %d times for 120 "
            "rows; the matcher must be memoized per (model, candidates)"
            % len(calls))

    def test_unconfigured_environment_raises(self):
        with mock.patch.object(console_stats, "_STATS_ENVIRONMENT", None):
            for call in (
                    lambda: console_stats.bounded_stats(
                        {"chat2api": str(self.ledger)},
                        "2026-11-07", "2026-11-07"),
                    console_stats.default_ledgers,
                    console_stats.retired_ledgers,
                    console_stats.default_sources,
                    console_stats._um_price_paths):
                with self.assertRaises(console_stats.StatsNotConfigured):
                    call()


class VersionedPriceTableTests(unittest.TestCase):
    """The local loader must not flatten a versioned price document."""

    def test_versioned_document_uses_the_record_segment(self):
        table = stats_env.normalize_price_table(VERSIONED_PRICE_DOC)
        self.assertTrue(table["versioned"])
        self.assertEqual(table["versionMeta"]["count"], 2)
        self.assertEqual(table["versionMeta"]["invalidCount"], 0)
        self.assertEqual(table["exchangeRate"], 8.0)
        current_ms = pricing_rules.parse_ts("2026-11-07T10:00:00+08:00")
        old_ms = pricing_rules.parse_ts("2026-08-15T10:00:00+08:00")
        self.assertEqual(
            pricing_rules.estimate_record(
                FIXTURE_MODEL, current_ms, 1_000_000, 500_000, 0, 0, table),
            (20.0, True, FIXTURE_MODEL, False))
        self.assertEqual(
            pricing_rules.estimate_record(
                "legacy-model", old_ms, 1_000_000, 500_000, 0, 0, table),
            (2.0, True, "legacy-model", False))
        # The current segment must not price a record from before it.
        self.assertEqual(
            pricing_rules.estimate_record(
                FIXTURE_MODEL, old_ms, 1_000_000, 500_000, 0, 0, table),
            (0.0, False, None, False))

    def test_bounded_stats_prices_the_current_model(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            ledger = directory / "usage-chat2api.jsonl"
            ledger.write_text(
                json.dumps(_fixture_ledger_record(), ensure_ascii=False)
                + "\n", encoding="utf-8")
            table = stats_env.normalize_price_table(VERSIONED_PRICE_DOC)
            environment = console_stats.StatsEnvironment(
                chat2api_log=str(ledger),
                dsh_snapshot=None, qoder_log="", m365_log="",
                dsh_merge_day="",
                magpie_price_snapshot=str(directory / "snapshot.json"),
                magpie_price_status=str(directory / "status.json"),
                parse_ts=pricing_rules.parse_ts,
                beijing_day=pricing_rules.beijing_day,
                day_start_ms=pricing_rules.day_start_ms,
                display_path=lambda path: os.path.basename(str(path or "")),
                estimate_record=lambda *args: pricing_rules.estimate_record(
                    *args, table),
                price_table_loader=lambda: table)
            result = console_stats.bounded_stats(
                {"chat2api": str(ledger)}, "2026-11-07", "2026-11-07",
                environment=environment)
        self.assertEqual(result["totals"]["estimated_cost_cny"], 160.0)
        self.assertEqual(result["coverage"]["unpriced_models"], [])

    def test_load_price_table_reads_the_explicit_override_file(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            price_path = directory / "prices.json"
            price_path.write_text(json.dumps({
                "exchangeRate": 7.2,
                "checkedAt": "2026-11-01",
                "models": {FIXTURE_MODEL: {"input": 1.0, "output": 2.0}},
            }), encoding="utf-8")
            override_path = directory / "price.override.json"
            override_path.write_text(json.dumps({
                "override-model": {"input": 3.0, "output": 4.0},
            }), encoding="utf-8")
            table = stats_env.load_price_table(price_path, override_path)
            self.assertEqual(
                table["override"],
                {"override-model": {"input": 3.0, "output": 4.0}})
            self.assertEqual(
                pricing_rules.estimate_record(
                    "override-model",
                    pricing_rules.parse_ts("2026-11-07T10:00:00+08:00"),
                    1_000_000, 0, 0, 0, table),
                (3.0, True, "override-model", False))


class SharedPricingSurfaceTests(unittest.TestCase):
    """E19-1-04 step 14: the local reader uses the PUBLIC ``shared`` names.

    ``shared.pricing_rules`` exposes the ten formerly private helpers under public
    names; a consumer that keeps calling ``_rate_of``/``_tiers_of``/... would pin
    the private spelling and block the shared module from ever tidying it. The price
    CACHE and the file READ stay in this endpoint (``_PRICE_CACHE``/``_read_json``)
    — ``shared`` is a pure rule module and must not grow I/O.
    """

    PRIVATE_NAMES = ("_rate_of", "_peak_of", "_tiers_of", "_checked_age_days",
                     "_price_version", "_current_version", "_version_id",
                     "_beijing_weekday", "_strip_id_decor", "_tokens_of")

    def test_local_reader_calls_no_private_pricing_name(self):
        source = (GATEWAY_DIR / "local" / "backend" / "stats_env.py").read_text(
            encoding="utf-8")
        for name in self.PRIVATE_NAMES:
            with self.subTest(name=name):
                self.assertNotIn("pricing_rules." + name, source)

    def test_local_reader_still_owns_the_cache_and_the_file_read(self):
        # Behaviour, not a name check: the second read of the same path is served
        # from the endpoint's own cache, and the cache lives in this module.
        self.assertIsInstance(stats_env._PRICE_CACHE, dict)
        self.assertTrue(callable(stats_env._read_json))
        with tempfile.TemporaryDirectory() as temp:
            price_path = Path(temp) / "prices.json"
            price_path.write_text(json.dumps({
                "exchangeRate": 7.2,
                "checkedAt": "2026-11-01",
                "models": {FIXTURE_MODEL: {"input": 1.0, "output": 2.0}},
            }), encoding="utf-8")
            first = stats_env.load_price_table(price_path)
            # A different content at the same path must NOT be observed while the
            # TTL holds: proof the cache is the local one, not a fresh read.
            price_path.write_text(json.dumps({
                "exchangeRate": 9.9,
                "checkedAt": "2026-11-01",
                "models": {FIXTURE_MODEL: {"input": 5.0, "output": 6.0}},
            }), encoding="utf-8")
            second = stats_env.load_price_table(price_path)
        self.assertEqual(second["exchangeRate"], first["exchangeRate"])
        self.assertEqual(second["tiers"], first["tiers"])

    def test_shared_pricing_rules_does_not_import_io_modules(self):
        # ``shared`` stays pure: no file/network/thread module may enter it through
        # pricing_rules. This is the reason the cache must live in the endpoint.
        source = (GATEWAY_DIR / "shared" / "pricing_rules.py").read_text(
            encoding="utf-8")
        for module in ("open(", "threading", "socket", "urllib.request"):
            with self.subTest(module=module):
                self.assertNotIn(module, source)


if __name__ == "__main__":
    unittest.main()
