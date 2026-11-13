# -*- coding: utf-8 -*-
"""console_stats 原始用量回归（费用/价格口径已随 #30 退役）。

覆盖：
* ``bounded_stats``：按日期/来源/模型聚合原始用量（请求、输入/输出/缓存/
  思考 Token）；缺席账本、坏行、无时刻记录、缺失日如实分列；不输出金额、
  价格版本或未定价模型。
* ``usage_tokens`` / ``usage_rates``：A12-1 有界只读查询层，契约不变。
* ``StatsEnvironment`` 只注入路径与纯时间规则，不注入价目加载器。
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from console import stats as console_stats  # noqa: E402
import usagesrv  # noqa: E402


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def _c2a_record(time, model="glm-5.3", in_tok=1_000_000, out_tok=0,
                cache_tok=0, reasoning_tok=0):
    return {"time": time, "service": "chat2api", "model": model, "stream": True,
            "status": "ok", "input_tokens": in_tok, "output_tokens": out_tok,
            "cached_input_tokens": cache_tok,
            "reasoning_tokens": reasoning_tok,
            "total_tokens": (in_tok + out_tok
                             if in_tok is not None and out_tok is not None
                             else None), "estimated": False,
            "duration_ms": 100}


def _dsh_snapshot():
    return {
        "_说明": "test snapshot",
        "config": {"pricingCurrency": "USD"},
        "days": {
            "2026-09-02": {
                "date": "2026-09-02",
                "calls": 3, "input": 3000, "output": 300,
                "cacheRead": 100, "cacheWrite": 0, "reasoning": 20,
                "sessions": [],
                "byProviderModel": {
                    "openwebui:Test-A": {"calls": 2,
                                         "input": 2000, "output": 200,
                                         "cacheRead": 100, "cacheWrite": 0,
                                         "reasoning": 10},
                    "openwebui:Test-B": {"calls": 1,
                                         "input": 1000, "output": 100,
                                         "cacheRead": 0, "cacheWrite": 0,
                                         "reasoning": 10},
                },
            },
        },
    }


class TestBoundedStats(unittest.TestCase):
    """Fixture ledger files in a temp dir; no production ledger is read."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _write_ledger(self, name, records):
        return _write_jsonl(os.path.join(self._tmp.name, name), records)

    def _write_snapshot(self, snapshot):
        path = os.path.join(self._tmp.name, "dsh-snapshot.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
        return path

    def _source_block(self, result, day, source):
        for day_entry in result["days"]:
            if day_entry["date"] == day:
                for block in day_entry["sources"]:
                    if block["source"] == source:
                        return block
        raise AssertionError("no block %s on %s" % (source, day))

    def _source_block_in_day(self, day_entry, source):
        for block in day_entry["sources"]:
            if block["source"] == source:
                return block
        raise AssertionError("no block %s" % source)

    def _model_row(self, result, day, source, model):
        block = self._source_block(result, day, source)
        for row in block["models"]:
            if row["model"] == model:
                return row
        raise AssertionError("no model %s" % model)

    def _day(self, result, date_str):
        for day_entry in result["days"]:
            if day_entry["date"] == date_str:
                return day_entry
        raise AssertionError("no day %s" % date_str)

    def test_single_source_single_day_aggregation(self):
        ledgers = {"chat2api": self._write_ledger("c2a.jsonl", [
            _c2a_record("2026-09-10T10:00:00+08:00", in_tok=1_000_000,
                         out_tok=500_000, cache_tok=0),
            _c2a_record("2026-09-10T11:00:00+08:00", in_tok=2_000_000,
                         out_tok=1_000_000, cache_tok=500_000),
        ])}
        res = console_stats.bounded_stats(ledgers, "2026-09-10", "2026-09-10")
        self.assertEqual(len(res["days"]), 1)
        row = self._model_row(res, "2026-09-10", "chat2api", "glm-5.3")
        self.assertEqual(row["requests"], 2)
        self.assertEqual(row["input_tokens"], 3_000_000)
        self.assertEqual(row["output_tokens"], 1_500_000)
        self.assertEqual(row["cache_tokens"], 500_000)
        self.assertEqual(row["reasoning_tokens"], 0)
        self.assertNotIn("estimated_cost_cny", row)
        src = self._source_block(res, "2026-09-10", "chat2api")
        self.assertEqual(src["kind"], "estimate")
        self.assertEqual(src["source_totals"]["requests"], 2)
        self.assertEqual(res["totals"]["requests"], 2)
        self.assertNotIn("estimated_cost_cny", res["totals"])
        cov = res["coverage"]["sources"]["chat2api"]
        self.assertTrue(cov["ledger_present"])
        self.assertEqual(cov["days_with_records"], 1)
        self.assertEqual(res["coverage"]["missing_days"], [])
        self.assertNotIn("unpriced_models", res["coverage"])
        self.assertNotIn("price_checked_at", res["meta"])

    def test_half_open_day_boundary_assignment(self):
        ledgers = {"chat2api": self._write_ledger("c2a.jsonl", [
            _c2a_record("2026-09-10T23:30:00+08:00"),
            _c2a_record("2026-09-11T00:30:00+08:00"),
            _c2a_record("2026-09-10T16:30:00Z"),
            _c2a_record("2026-09-09T12:00:00+08:00"),
        ])}
        res = console_stats.bounded_stats(ledgers, "2026-09-10", "2026-09-11")
        self.assertEqual([d["date"] for d in res["days"]],
                         ["2026-09-10", "2026-09-11"])
        self.assertEqual(self._day(res, "2026-09-10")["day_total"]["requests"], 1)
        self.assertEqual(self._day(res, "2026-09-11")["day_total"]["requests"], 2)
        self.assertEqual(res["totals"]["requests"], 3)
        self.assertEqual(res["coverage"]["missing_days"], [])

    def test_source_and_model_filter_cross(self):
        ledgers = {
            "chat2api": self._write_ledger("c2a.jsonl", [
                _c2a_record("2026-09-10T10:00:00+08:00"),
                _c2a_record("2026-09-10T11:00:00+08:00",
                             model="gpt-5.6-sol", in_tok=1_000_000,
                             out_tok=100_000),
                _c2a_record("2026-09-11T10:00:00+08:00"),
            ]),
        }
        res = console_stats.bounded_stats(
            ledgers, "2026-09-10", "2026-09-11",
            sources=["chat2api"], models=["glm-5.3"])
        self.assertEqual([d["date"] for d in res["days"]],
                         ["2026-09-10", "2026-09-11"])
        for day in res["days"]:
            self.assertEqual([b["source"] for b in day["sources"]], ["chat2api"])
            self.assertEqual(
                [m["model"] for m in
                 self._source_block_in_day(day, "chat2api")["models"]],
                ["glm-5.3"])
        self.assertEqual(res["totals"]["requests"], 2)
        res_empty = console_stats.bounded_stats(
            ledgers, "2026-09-10", "2026-09-11", models=["nosuchmodel"])
        self.assertEqual(res_empty["days"], [])
        self.assertEqual(res_empty["coverage"]["missing_days"],
                         ["2026-09-10", "2026-09-11"])

    def test_absent_ledger_coverage(self):
        ledgers = {
            "chat2api": os.path.join(self._tmp.name, "not-there.jsonl"),
            "dsh": self._write_snapshot(_dsh_snapshot()),
        }
        res = console_stats.bounded_stats(ledgers, "2026-09-02", "2026-09-03")
        cov = res["coverage"]["sources"]["chat2api"]
        self.assertFalse(cov["ledger_present"])
        self.assertEqual(cov["days_with_records"], 0)
        self.assertEqual([d["date"] for d in res["days"]], ["2026-09-02"])
        for day in res["days"]:
            self.assertNotIn("chat2api",
                             [b["source"] for b in day["sources"]])
        self.assertEqual(res["coverage"]["missing_days"], ["2026-09-03"])
        self.assertEqual(res["coverage"]["sources"]["dsh"]["ledger_present"],
                         True)

    def test_malformed_lines_mark_source_error(self):
        good = _c2a_record("2026-09-10T10:00:00+08:00")
        path = os.path.join(self._tmp.name, "c2a.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(good) + "\n")
            f.write("not-json{{\n")
        res = console_stats.bounded_stats(
            {"chat2api": path}, "2026-09-10", "2026-09-10")
        chat_cov = res["coverage"]["sources"]["chat2api"]
        errors = {e["type"]: e for e in chat_cov.get("errors", [])}
        self.assertIn("malformed_lines", errors)
        self.assertEqual(errors["malformed_lines"]["bad_lines"], 1)
        self.assertEqual(
            self._model_row(res, "2026-09-10", "chat2api", "glm-5.3")
            ["requests"], 1)

    def test_missing_days_reported_not_zero(self):
        ledgers = {"chat2api": self._write_ledger("c2a.jsonl", [
            _c2a_record("2026-09-10T10:00:00+08:00"),
            _c2a_record("2026-09-12T10:00:00+08:00"),
        ])}
        res = console_stats.bounded_stats(ledgers, "2026-09-10", "2026-09-13")
        self.assertEqual([d["date"] for d in res["days"]],
                         ["2026-09-10", "2026-09-12"])
        self.assertEqual(res["coverage"]["missing_days"],
                         ["2026-09-11", "2026-09-13"])
        self.assertEqual(res["totals"]["requests"], 2)
        self.assertEqual(
            res["coverage"]["sources"]["chat2api"]["days_with_records"], 2)

    def test_range_limit_and_invalid_range(self):
        ledgers = {"chat2api": os.path.join(self._tmp.name, "c2a.jsonl")}
        with self.assertRaises(ValueError):
            console_stats.bounded_stats(ledgers, "2026-01-01", "2026-04-03")
        with self.assertRaises(ValueError):
            console_stats.bounded_stats(ledgers, "2026-09-10", "2026-09-09")
        with self.assertRaises(ValueError):
            console_stats.bounded_stats(ledgers, "20260910", "2026-09-11")
        res = console_stats.bounded_stats(ledgers, "2026-01-01", "2026-04-02")
        self.assertEqual(res["meta"]["range_days"], 92)
        self.assertEqual(len(res["coverage"]["missing_days"]), 92)

    def test_cross_day_totals_consistency(self):
        ledgers = {
            "chat2api": self._write_ledger("c2a.jsonl", [
                _c2a_record("2026-09-01T10:00:00+08:00"),
                _c2a_record("2026-09-02T10:00:00+08:00",
                             in_tok=2_000_000, out_tok=1_000_000,
                             cache_tok=500_000),
                _c2a_record("2026-09-03T10:00:00+08:00",
                             in_tok=500_000),
            ]),
            "dsh": self._write_snapshot(_dsh_snapshot()),
        }
        res = console_stats.bounded_stats(ledgers, "2026-09-01", "2026-09-05")
        days = {d["date"]: d for d in res["days"]}
        self.assertEqual(sorted(days),
                         ["2026-09-01", "2026-09-02", "2026-09-03"])
        for key in ("requests", "input_tokens", "output_tokens",
                     "cache_tokens", "reasoning_tokens"):
            self.assertEqual(res["totals"][key],
                             sum(d["day_total"][key] for d in res["days"]))

    def test_unknown_source_filter_empty_with_coverage(self):
        res = console_stats.bounded_stats(
            {}, "2026-09-10", "2026-09-10", sources=["retired"])
        self.assertEqual(res["days"], [])
        cov = res["coverage"]["sources"]["retired"]
        self.assertFalse(cov["ledger_present"])
        self.assertEqual(cov["errors"][0]["type"], "unknown_source")

    def test_default_ledgers_matches_usagesrv_constants(self):
        ledgers = console_stats.default_ledgers()
        self.assertEqual(ledgers["chat2api"], usagesrv.CHAT2API_LOG)
        self.assertEqual(ledgers["dsh"], usagesrv.DSH_SNAPSHOT_FILE)

    def test_bounded_stats_corrupt_snapshot_shapes_not_fatal(self):
        day = usagesrv.DSH_MERGE_DAY
        for name, snapshot in (("days-list", {"days": [1, 2]}),
                               ("bpm-list", {"days": {day: {
                                   "byProviderModel": [1]}}})):
            path = os.path.join(self._tmp.name, "%s.json" % name)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False)
            res = console_stats.bounded_stats({"dsh": path}, day, day)
            self.assertEqual(res["days"], [])
            cov = res["coverage"]["sources"]["dsh"]
            self.assertTrue(cov["ledger_present"])
            errors = {e["type"]: e for e in cov.get("errors", [])}
            self.assertIn("corrupt_snapshot", errors)


BEIJING = timezone(timedelta(hours=8))
NOW_1200 = datetime(2026, 9, 24, 12, 0, tzinfo=BEIJING)
ALLOWED_REASONS = set(console_stats._MISSING_REASONS)


def _c2a_usage(time, model="m-a", in_tok=100, out_tok=50, duration_ms=1000,
                status="ok", drop=()):
    rec = {"time": time, "service": "chat2api", "model": model, "stream": True,
           "status": status, "input_tokens": in_tok, "output_tokens": out_tok,
           "cached_input_tokens": 0, "reasoning_tokens": 0,
           "total_tokens": 150, "estimated": False,
           "duration_ms": duration_ms}
    for key in drop:
        rec.pop(key, None)
    return rec


class TestA12UsageQueries(unittest.TestCase):
    """A12-1 端点契约：临时目录 fixture，不读真实账本、不发任何请求。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        console_stats._reset_cache_for_tests()
        self.addCleanup(console_stats._reset_cache_for_tests)

    def _write(self, name, records):
        return _write_jsonl(os.path.join(self._tmp.name, name), records)

    def _rows(self, result):
        return {(r["source"], r["model"]): r for r in result["days"]}

    def _missing(self, result):
        return {(m["source"], m["model"]): m for m in result["missing"]}

    def test_complete_partial_zero_and_missing_are_separated(self):
        ledgers = {"chat2api": self._write("c2a.jsonl", [
            _c2a_usage("2026-09-24T10:00:00+08:00", model="m-a",
                        in_tok=100, out_tok=50),
            _c2a_usage("2026-09-24T10:00:00+08:00", model="m-zero",
                        in_tok=0, out_tok=0),
            _c2a_usage("2026-09-24T11:00:00+08:00", model="m-bare",
                        in_tok=None, out_tok=None,
                        drop=("input_tokens", "output_tokens")),
            _c2a_usage("2026-09-24T12:00:00+08:00", model="m-out",
                        in_tok=None, out_tok=200,
                        drop=("input_tokens",)),
        ])}
        res = console_stats.usage_tokens(ledgers, days=1, now=NOW_1200)
        rows = self._rows(res)
        self.assertEqual(rows[("chat2api", "m-a")]["input_tokens"], 100)
        self.assertEqual(rows[("chat2api", "m-a")]["output_tokens"], 50)
        self.assertTrue(rows[("chat2api", "m-a")]["complete"])
        self.assertEqual(rows[("chat2api", "m-zero")]["input_tokens"], 0)
        self.assertEqual(rows[("chat2api", "m-zero")]["output_tokens"], 0)
        self.assertTrue(rows[("chat2api", "m-zero")]["complete"])
        self.assertIsNone(rows[("chat2api", "m-out")]["input_tokens"])
        self.assertIsNone(rows[("chat2api", "m-out")]["output_tokens"])
        self.assertEqual(rows[("chat2api", "m-out")]["partial_output_tokens"], 200)
        self.assertFalse(rows[("chat2api", "m-out")]["complete"])
        missing = self._missing(res)
        self.assertEqual(missing[("chat2api", "m-out")]["reason"], "output_only")
        self.assertEqual(missing[("chat2api", "m-out")]["date"], "2026-09-24")
        self.assertEqual(missing[("chat2api", "m-out")]["output_tokens"], 200)
        self.assertEqual(missing[("chat2api", "m-bare")]["reason"], "no_tokens")
        self.assertIn(rows[("chat2api", "m-out")]["partial_requests"], (0, 1))

    def test_removed_source_is_never_read(self):
        path = self._write("qcn.jsonl", [
            {"time": "2026-09-24T10:00:00+08:00", "model": "same",
             "input_tokens": 7, "output_tokens": 3},
        ])
        for source in ("qoder_cn", "qoder_global", "cliproxy", "m365"):
            with self.subTest(source=source):
                res = console_stats.usage_tokens(
                    {source: path}, days=1, now=NOW_1200)
                self.assertEqual(res["days"], [])
                cov = res["coverage"]["sources"][source]
                self.assertFalse(cov["ledger_present"])
                self.assertFalse(cov["enabled"])
        self.assertNotIn("qoder_cn", console_stats._SOURCE_SPECS)
        self.assertNotIn("qoder_global", console_stats._SOURCE_SPECS)
        self.assertNotIn("cliproxy", console_stats._SOURCE_SPECS)

    def test_invalid_timestamp_goes_to_missing_not_dropped(self):
        ledgers = {"chat2api": self._write("c2a.jsonl", [
            _c2a_usage("not-a-time", model="m-bad", in_tok=5, out_tok=5),
        ])}
        res = console_stats.usage_tokens(ledgers, days=1, now=NOW_1200)
        self.assertEqual(res["days"], [])
        self.assertEqual(len(res["missing"]), 1)
        self.assertEqual(res["missing"][0]["reason"], "invalid_time")
        self.assertIsNone(res["missing"][0]["date"])

    def test_out_of_order_records_bucket_by_own_time(self):
        ledgers = {"chat2api": self._write("c2a.jsonl", [
            _c2a_usage("2026-09-24T11:00:00+08:00", model="m-a"),
            _c2a_usage("2026-09-23T11:00:00+08:00", model="m-b"),
            _c2a_usage("2026-09-24T09:00:00+08:00", model="m-a"),
        ])}
        res = console_stats.usage_tokens(ledgers, days=2, now=NOW_1200)
        by_date = {}
        for row in res["days"]:
            by_date.setdefault(row["date"], set()).add(row["model"])
        self.assertEqual(by_date, {"2026-09-23": {"m-b"},
                                  "2026-09-24": {"m-a"}})

    def test_range_clamped_and_row_cap_truncated(self):
        ledgers = {"chat2api": self._write("c2a.jsonl", [
            _c2a_usage("2026-09-24T10:00:00+08:00", model="m-a"),
            _c2a_usage("2026-09-24T10:00:00+08:00", model="m-b"),
        ])}
        res = console_stats.usage_tokens(ledgers, days=999, now=NOW_1200)
        self.assertEqual(res["coverage"]["days"], 31)
        self.assertEqual(len(res["days"]), 2)
        self.assertTrue(res["coverage"]["clamped"])

    def test_missing_ledger_returns_empty_with_note(self):
        ledgers = {"chat2api": os.path.join(self._tmp.name, "missing.jsonl")}
        res = console_stats.usage_tokens(ledgers, days=1, now=NOW_1200)
        self.assertEqual(res["days"], [])
        cov = res["coverage"]["sources"]["chat2api"]
        self.assertFalse(cov["ledger_present"])
        self.assertIn("note", cov)

    def test_reason_enums_match_contract(self):
        ledgers = {"chat2api": self._write("c2a.jsonl", [
            _c2a_usage("2026-09-24T10:00:00+08:00", in_tok=None,
                        out_tok=None,
                        drop=("input_tokens", "output_tokens")),
        ])}
        res = console_stats.usage_tokens(ledgers, days=1, now=NOW_1200)
        for entry in res["missing"]:
            self.assertIn(entry["reason"], ALLOWED_REASONS)

    def test_usage_rates_are_raw_and_missing_not_zero(self):
        ledgers = {"chat2api": self._write("c2a.jsonl", [
            _c2a_usage("2026-09-24T11:00:00+08:00", model="m-a",
                        out_tok=100, duration_ms=1000),
            _c2a_usage("2026-09-24T12:00:00+08:00", model="m-a",
                        out_tok=None, duration_ms=1000,
                        drop=("output_tokens",)),
        ])}
        res = console_stats.usage_rates(ledgers, hours=24, now=NOW_1200)
        self.assertEqual(len(res["hours"]), 1)
        row = res["hours"][0]
        self.assertEqual(row["output_tokens"], 100)
        self.assertEqual(row["duration_ms"], 1000)
        self.assertEqual(row["requests"], 1)
        self.assertEqual(row["rate"], 100.0)


class ModelTokenRankingTests(unittest.TestCase):
    def setUp(self):
        console_stats.configure(usagesrv.STATS_ENV)
        self.addCleanup(setattr, console_stats, "_STATS_ENVIRONMENT", None)

    def test_day_rows_carry_token_total_and_ranking(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with tempfile.TemporaryDirectory() as directory:
            path = _write_jsonl(os.path.join(directory, "c2a.jsonl"), [
                _c2a_record("2026-09-10T10:00:00+08:00",
                             in_tok=1000, out_tok=500),
            ])
            res = console_stats.bounded_stats(
                {"chat2api": path}, "2026-09-10", "2026-09-10")
        day = res["days"][0]
        self.assertEqual(day["model_token_ranking"][0]["model"], "glm-5.3")
        self.assertEqual(day["model_token_ranking"][0]["token_total"], 1500)
        self.assertTrue(day["token_coverage"]["complete"])


class CompleteModelTokenTests(unittest.TestCase):
    def setUp(self):
        console_stats.configure(usagesrv.STATS_ENV)
        self.addCleanup(setattr, console_stats, "_STATS_ENVIRONMENT", None)

    def _run(self, records, **kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = _write_jsonl(os.path.join(tmp.name, "c2a.jsonl"), records)
        return console_stats.bounded_stats(
            {"chat2api": path}, "2026-09-10", "2026-09-10", **kwargs)

    def test_partial_only_model_is_not_ranked_as_complete_tokens(self):
        res = self._run([
            _c2a_record("2026-09-10T10:00:00+08:00", in_tok=1000),
            _c2a_record("2026-09-10T11:00:00+08:00", in_tok=None,
                         out_tok=50),
        ])
        ranking = res["days"][0]["model_token_ranking"]
        self.assertEqual(ranking[0]["token_total"], 1000)
        self.assertEqual(ranking[0]["token_coverage"]["records"], 2)
        self.assertEqual(ranking[0]["token_coverage"]["complete_records"], 1)

    def test_all_missing_or_invalid_sides_are_unknown_without_a_fake_total(self):
        res = self._run([
            _c2a_record("2026-09-10T10:00:00+08:00", in_tok=None,
                         out_tok=None),
        ])
        row = res["days"][0]["sources"][0]["models"][0]
        self.assertIsNone(row["token_total"])
        self.assertEqual(row["token_coverage"]["unknown_records"], 1)


class StatsEnvironmentRestorationTests(unittest.TestCase):
    def test_the_module_restores_the_process_environment(self):
        saved = dict(os.environ)
        try:
            console_stats.configure(usagesrv.STATS_ENV)
            self.assertIs(console_stats._environment(), usagesrv.STATS_ENV)
        finally:
            os.environ.clear()
            os.environ.update(saved)


class StatsEnvironmentInjectionTests(unittest.TestCase):
    def test_configure_rejects_none(self):
        with self.assertRaises(TypeError):
            console_stats.configure(None)

    def test_um_read_faces_raise_instead_of_returning_empty(self):
        saved = console_stats._STATS_ENVIRONMENT
        console_stats._STATS_ENVIRONMENT = None
        try:
            for call in (console_stats.um_today, console_stats.um_trend,
                          console_stats.um_requests):
                with self.subTest(call=call.__name__):
                    with self.assertRaises(console_stats.StatsNotConfigured):
                        call()
            with self.assertRaises(console_stats.StatsNotConfigured):
                console_stats.bounded_stats({}, "2026-09-10", "2026-09-10")
        finally:
            console_stats._STATS_ENVIRONMENT = saved

    def test_an_explicit_environment_needs_no_process_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = _write_jsonl(os.path.join(directory, "c2a.jsonl"),
                                   [_c2a_record("2026-09-10T10:00:00+08:00")])
            environment = console_stats.StatsEnvironment(
                chat2api_log=ledger,
                dsh_snapshot=os.path.join(directory, "absent.json"),
                dsh_merge_day="",
                parse_ts=usagesrv._parse_ts,
                beijing_day=usagesrv._beijing_day,
                day_start_ms=usagesrv._day_start_ms,
                display_path=usagesrv._display_path)
            res = console_stats.bounded_stats(
                {"chat2api": ledger}, "2026-09-10", "2026-09-10",
                environment=environment)
        self.assertEqual(res["totals"]["requests"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
