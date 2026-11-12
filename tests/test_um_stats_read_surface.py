# -*- coding: utf-8 -*-
"""B5 UM statistics / requests / logs / maintenance read surface.

Every fixture is written to a temp directory with the SAME field shape the
production chat2api writer (``record_usage``) and the B1 Magpie snapshot
produce; no real ledger, price table, service log or console database is read
or written. The cases pin:

* today's five readings, with cache part of input and thinking part of output;
* Q11 cache report status: reported / unreported / historical unknown / zero;
* explicit zero, unreported and unknown are distinct, never folded together;
* missing / corrupt / unreadable ledger is not a legal empty list;
* the Beijing day boundary and a 7-day trend with the same-window ranking;
* physical retry rows are not logical requests (request count, speed);
* per-request price/FX version, unpriced/incomplete coverage;
* request filters (time/model/result/search) and opaque pagination;
* logs are bounded and only a reliable 32-hex request id is associated;
* maintenance reads the ORIGINAL operation records, not request rows;
* the cloud role keeps the read surface explicitly unavailable.
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from console import log_reader as console_log_reader  # noqa: E402
from console import stats as console_stats       # noqa: E402
import usagesrv             # noqa: E402

BEIJING = timezone.utc
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=BEIJING)


class _UsagesrvStatsEnvironment:
    """Test adapter over the usagesrv module constants.

    These cases redirect the statistics paths by patching the usagesrv module
    constants (the old injection points), so this fixture resolves them at read
    time instead of freezing them at configure() time. The production local
    host builds a plain StatsEnvironment from the same constants at startup.
    """

    chat2api_log = property(lambda self: usagesrv.CHAT2API_LOG)
    dsh_snapshot = property(lambda self: usagesrv.DSH_SNAPSHOT_FILE)
    qoder_log = property(lambda self: usagesrv.QODER_LOG)
    m365_log = property(lambda self: usagesrv.M365_LOG)
    dsh_merge_day = property(lambda self: usagesrv.DSH_MERGE_DAY)
    magpie_price_snapshot = property(
        lambda self: usagesrv.MAGPIE_PRICE_SNAPSHOT_FILE)
    magpie_price_status = property(
        lambda self: usagesrv.MAGPIE_PRICE_STATUS_FILE)
    parse_ts = staticmethod(lambda value: usagesrv._parse_ts(value))
    beijing_day = staticmethod(lambda ms: usagesrv._beijing_day(ms))
    day_start_ms = staticmethod(lambda day: usagesrv._day_start_ms(day))
    display_path = staticmethod(lambda path: usagesrv._display_path(path))
    estimate_record = staticmethod(
        lambda *args: usagesrv.estimate_record(*args))
    price_table_loader = staticmethod(lambda: usagesrv.load_price_table())


def _record(time, model="GLM-Test", input_tokens=1000, output_tokens=500,
            cache_tokens=0, cache_status="reported", reasoning_tokens=0,
            status="ok", duration_ms=1000, request_id=None,
            usage_source="upstream", estimated=False, drop=()):
    """One production-shaped chat2api usage row."""
    rec = {
        "time": time,
        "service": "chat2api",
        "model": model,
        "stream": True,
        "status": status,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cache_tokens,
        "cache_report_status": cache_status,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": ((input_tokens or 0) + (output_tokens or 0)),
        "estimated": estimated,
        "duration_ms": duration_ms,
        "usage_source": usage_source,
        "protocol": "openai",
        "key_id": None,
        "request_id": request_id,
        "attribution": "verified" if request_id else "absent",
        "price_version": "share-v1",
    }
    for key in drop:
        rec.pop(key, None)
    return rec


def _model_entry(input_price=2.0, output_price=10.0, cache_price=0.1,
                  tiers=()):
    return {
        "umModel": "GLM-Test",
        "magpieModel": "zcode/glm-test",
        "displayName": "GLM Test",
        "aliases": ["GLM-Test"],
        "currency": "USD",
        "unit": "per_million_tokens",
        "sourceUnit": "per_million_tokens",
        "input": input_price,
        "output": output_price,
        "cacheRead": cache_price,
        "cacheWrite": 0.0,
        "tiers": list(tiers) if tiers is not None else [],
        "tiersReported": True,
        "manualPrice": True,
        "correctionSource": "magpie-manual",
        "priceSource": "magpie-cli-effective",
        "source": "magpie:/v1/models",
        "complete": True,
        "catalogMismatch": False,
    }


def _version(version="magpie-20261101T000000Z-test", exchange_rate=7.2,
             effective="2026-10-01T00:00:00+08:00",
             model=None):
    return {
        "version": version,
        "effectiveAt": effective,
        "sourceObservedAt": effective,
        "exchangeRate": exchange_rate,
        "fxVersion": "fx-20261101",
        "correctionSource": "magpie-manual",
        "source": "magpie:/v1/models",
        "models": {"GLM-Test": model or _model_entry()},
        "unpricedModels": [],
    }


def _snapshot(versions=None):
    return {
        "format": "um-magpie-price-snapshot-v1",
        "schemaVersion": 1,
        "generatedAt": "2026-11-01T00:00:00Z",
        "source": {"kind": "fixture"},
        "currentVersion": None,
        "versions": versions or [_version()],
    }


class _UmFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.ledger = self.dir / "usage-chat2api.jsonl"
        self.snapshot = self.dir / "magpie-prices.snapshot.json"
        self.status = self.dir / "magpie-prices.status.json"
        self._saved = {
            name: getattr(usagesrv, name) for name in (
                "CHAT2API_LOG", "MAGPIE_PRICE_SNAPSHOT_FILE",
                "MAGPIE_PRICE_STATUS_FILE")}
        self.addCleanup(self._restore)
        usagesrv.CHAT2API_LOG = str(self.ledger)
        usagesrv.MAGPIE_PRICE_SNAPSHOT_FILE = str(self.snapshot)
        usagesrv.MAGPIE_PRICE_STATUS_FILE = str(self.status)
        console_stats.configure(_UsagesrvStatsEnvironment())
        console_stats._reset_cache_for_tests()
        self.addCleanup(console_stats._reset_cache_for_tests)

    def _restore(self):
        for name, value in self._saved.items():
            setattr(usagesrv, name, value)
        console_stats._reset_cache_for_tests()

    def write_ledger(self, records):
        self.ledger.write_text(
            "\n".join(json.dumps(rec, ensure_ascii=False)
                       for rec in records) + ("\n" if records else ""),
            encoding="utf-8")

    def write_snapshot(self, snapshot=None, status="synced"):
        self.snapshot.write_text(
            json.dumps(snapshot or _snapshot(), ensure_ascii=False),
            encoding="utf-8")
        self.status.write_text(json.dumps({
            "format": "um-magpie-price-status-v1",
            "state": status,
        }), encoding="utf-8")

    def today(self, records, now=NOW, snapshot=None, status="synced"):
        self.write_ledger(records)
        self.write_snapshot(snapshot, status)
        return console_stats.um_today(now=now)

    def trend(self, records, range="7d", now=NOW, snapshot=None,
              status="synced"):
        self.write_ledger(records)
        self.write_snapshot(snapshot, status)
        return console_stats.um_trend(range=range, now=now)

    def requests(self, records, now=NOW, **kwargs):
        self.write_ledger(records)
        self.write_snapshot()
        return console_stats.um_requests(now=now, **kwargs)


class TodayReadingsTests(_UmFixture):
    def test_five_readings_cache_and_thinking_are_not_added_again(self):
        res = self.today([
            _record("2026-11-01T10:00:00+08:00", input_tokens=1000,
                    output_tokens=500, cache_tokens=600,
                    cache_status="reported", reasoning_tokens=200,
                    duration_ms=2000, request_id="a" * 32),
            _record("2026-11-01T11:00:00+08:00", input_tokens=200,
                    output_tokens=100, cache_tokens=0,
                    cache_status="reported", reasoning_tokens=0,
                    duration_ms=1000),
        ])
        today = res["today"]
        # Cache reads are part of input: input stays 1200, total 1800.
        self.assertEqual(today["tokens"]["input"],
                         {"kind": "exact", "value": 1200})
        self.assertEqual(today["tokens"]["output"],
                         {"kind": "exact", "value": 600})
        self.assertEqual(today["tokens"]["total"],
                         {"kind": "exact", "value": 1800})
        self.assertEqual(today["cacheRead"]["tokens"],
                         {"kind": "exact", "value": 600})
        self.assertEqual(today["cacheRead"]["report"], "reported")
        self.assertIn("已含在输入中", today["cacheRead"]["detail"])
        # Thinking is part of output: the sum is a lower bound, not added.
        self.assertEqual(today["reasoning"]["kind"], "atLeast")
        self.assertEqual(today["reasoning"]["value"], 200)
        self.assertIn("已计入输出", today["reasoning"]["note"])
        self.assertEqual(today["requests"]["recorded"],
                         {"kind": "exact", "value": 2})
        self.assertEqual(today["requests"]["failed"],
                         {"kind": "exact", "value": 0})
        # 600 output tokens / 3 s = 200 token/s (whole request average).
        self.assertEqual(today["speed"]["value"],
                         {"kind": "exact", "value": 200.0})
        self.assertEqual(today["speed"]["basis"], "whole_request")
        self.assertEqual(today["speed"]["samples"], 2)
        self.assertEqual(today["boundaries"]["start"],
                         "2026-11-01T00:00:00+08:00")
        self.assertEqual(today["boundaries"]["end"],
                         "2026-11-02T00:00:00+08:00")
        self.assertTrue(today["boundaries"]["half_open"])

        cost = res["cost"]
        self.assertEqual(cost["label"], "参考")
        self.assertEqual(cost["cny"], {"kind": "exact", "value": 0.0523})
        self.assertEqual(cost["price"]["state"], "synced")
        self.assertEqual(cost["price"]["version"],
                         "magpie-20261101T000000Z-test")
        self.assertEqual(cost["fx"]["rate"], 7.2)
        self.assertEqual(cost["fx"]["version"], "fx-20261101")
        self.assertIsInstance(cost["price"]["snapshotAt"], int)
        self.assertEqual(cost["coverage"]["pricedRequests"], 2)
        self.assertEqual(cost["coverage"]["unpricedRequests"], 0)

    def test_explicit_zero_is_zero_not_unreported(self):
        res = self.today([
            _record("2026-11-01T10:00:00+08:00", input_tokens=0,
                    output_tokens=0, cache_tokens=0,
                    cache_status="reported", reasoning_tokens=0,
                    duration_ms=1000),
        ])
        today = res["today"]
        self.assertEqual(today["tokens"]["total"],
                         {"kind": "exact", "value": 0})
        self.assertEqual(today["tokens"]["input"],
                         {"kind": "exact", "value": 0})
        self.assertEqual(today["cacheRead"]["tokens"],
                         {"kind": "exact", "value": 0})
        self.assertEqual(today["cacheRead"]["report"], "reported")
        self.assertEqual(today["requests"]["recorded"],
                         {"kind": "exact", "value": 1})
        self.assertEqual(today["speed"]["value"],
                         {"kind": "exact", "value": 0.0})
        self.assertEqual(res["cost"]["cny"],
                         {"kind": "exact", "value": 0})

    def test_unreported_cache_is_not_a_measured_zero(self):
        res = self.today([
            _record("2026-11-01T10:00:00+08:00", input_tokens=1000,
                    output_tokens=500, cache_tokens=0,
                    cache_status="unreported", reasoning_tokens=0,
                    duration_ms=1000),
        ], snapshot=_snapshot([_version(model=_model_entry(cache_price=0.0))]))
        today = res["today"]
        self.assertEqual(today["cacheRead"]["report"], "unreported")
        self.assertEqual(today["cacheRead"]["tokens"]["kind"], "unreported")
        self.assertNotEqual(today["cacheRead"]["tokens"],
                            {"kind": "exact", "value": 0})
        # Input/output themselves are reported and stay exact.
        self.assertEqual(today["tokens"]["input"],
                         {"kind": "exact", "value": 1000})
        # The cost is a lower bound because cache usage is unknown.
        self.assertEqual(res["cost"]["cny"]["kind"], "atLeast")
        self.assertEqual(res["cost"]["coverage"]["cacheUnreportedRequests"], 1)
        self.assertEqual(res["cost"]["coverage"]["cacheUnknownRequests"], 0)

    def test_historical_unknown_cache_and_reasoning_are_unknown(self):
        res = self.today([
            _record("2026-11-01T10:00:00+08:00", input_tokens=1000,
                    output_tokens=500, cache_tokens=0,
                    drop=("cache_report_status", "reasoning_tokens")),
        ])
        today = res["today"]
        self.assertEqual(today["cacheRead"]["report"], "unknown")
        self.assertEqual(today["cacheRead"]["tokens"]["kind"], "unknown")
        self.assertIn("Q11", today["cacheRead"]["detail"])
        self.assertEqual(today["reasoning"]["kind"], "unknown")
        self.assertIn("旧记录", today["reasoning"]["note"])
        # Historical input/output fields are still real values.
        self.assertEqual(today["tokens"]["input"],
                         {"kind": "exact", "value": 1000})

    def test_usage_source_none_is_unknown_not_zero(self):
        res = self.today([
            _record("2026-11-01T10:00:00+08:00", input_tokens=0,
                    output_tokens=0, cache_tokens=0,
                    cache_status="reported", reasoning_tokens=0,
                    usage_source="none", estimated=True),
        ])
        today = res["today"]
        self.assertEqual(today["tokens"]["total"]["kind"], "unknown")
        self.assertEqual(today["tokens"]["input"]["kind"], "unknown")
        self.assertEqual(today["tokens"]["output"]["kind"], "unknown")
        self.assertEqual(today["requests"]["recorded"],
                         {"kind": "exact", "value": 1})
        self.assertEqual(res["cost"]["coverage"]["usageUnknownRequests"], 1)
        self.assertEqual(res["cost"]["cny"]["kind"], "atLeast")

    def test_missing_ledger_is_missing_not_zero(self):
        res = console_stats.um_today(now=NOW)
        self.assertEqual(res["coverage"]["status"], "missing")
        self.assertEqual(res["today"]["tokens"]["total"]["kind"], "missing")
        self.assertEqual(res["today"]["requests"]["recorded"]["kind"],
                         "missing")
        self.assertEqual(res["cost"]["cny"]["kind"], "missing")
        self.assertNotEqual(res["today"]["tokens"]["total"],
                            {"kind": "exact", "value": 0})

    def test_corrupt_ledger_is_partial_lower_bound(self):
        self.write_snapshot()
        self.ledger.write_text(
            "not-json{{\n" + json.dumps(_record(
                "2026-11-01T10:00:00+08:00", input_tokens=10,
                output_tokens=5)) + "\n", encoding="utf-8")
        res = console_stats.um_today(now=NOW)
        self.assertEqual(res["coverage"]["status"], "partial")
        self.assertEqual(res["coverage"]["errors"][0]["type"],
                         "malformed_lines")
        self.assertEqual(res["today"]["tokens"]["total"],
                         {"kind": "atLeast", "value": 15,
                          "note": "账本有损坏行/截断，合计为下界"})

    def test_unreadable_ledger_is_unavailable_not_zero(self):
        self.write_snapshot()
        # A directory cannot be opened as a JSONL file: read_error, not empty.
        res = console_stats.um_today(str(self.dir), now=NOW)
        self.assertEqual(res["coverage"]["status"], "unavailable")
        self.assertEqual(res["today"]["tokens"]["total"]["kind"],
                         "missing")
        self.assertEqual(res["cost"]["cny"]["kind"], "missing")

    def test_truncated_ledger_is_partial_lower_bound(self):
        self.write_ledger([
            _record("2026-11-01T10:00:00+08:00", input_tokens=10,
                    output_tokens=5, cache_tokens=0,
                    cache_status="reported", duration_ms=1000),
        ])
        self.write_snapshot()
        event = console_stats._Event(
            ms=usagesrv._parse_ts("2026-11-01T10:00:00+08:00"),
            model="GLM-Test", input_tokens=10, output_tokens=5,
            duration_ms=1000, duration_state="ok", status="ok",
            cache_tokens=0, reasoning_tokens=0,
            cache_report_status="reported", request_id=None,
            usage_source="upstream", estimated=False)
        state = {"events": [event], "bad_lines": 0, "dropped_events": 1,
                 "bytes_truncated": False, "control_records": 0,
                 "control_summaries": [], "size": 1, "mtime_ns": 1,
                 "dev": 0, "ino": 0, "offset": 1, "diag": {}}
        with mock.patch.object(
                console_stats, "_refresh_jsonl_cache",
                return_value=(state, {})):
            res = console_stats.um_today(now=NOW)
        self.assertEqual(res["coverage"]["status"], "partial")
        self.assertTrue(res["coverage"]["truncated"])
        self.assertEqual(res["today"]["tokens"]["total"],
                         {"kind": "atLeast", "value": 15,
                          "note": "账本有损坏行/截断，合计为下界"})

    def test_partial_speed_is_a_sample_estimate(self):
        res = self.today([
            _record("2026-11-01T10:00:00+08:00", input_tokens=100,
                    output_tokens=50, cache_tokens=0,
                    cache_status="reported", duration_ms=1000),
            _record("2026-11-01T11:00:00+08:00", input_tokens=100,
                    output_tokens=None, cache_tokens=0,
                    cache_status="reported", duration_ms=1000,
                    drop=("output_tokens",)),
        ])
        speed = res["today"]["speed"]
        self.assertEqual(speed["samples"], 1)
        self.assertEqual(speed["value"]["kind"], "estimate")
        self.assertIn("可计时请求", speed["value"]["note"])
        self.assertEqual(res["today"]["tokens"]["total"]["kind"],
                         "atLeast")

    def test_timezone_boundary_is_beijing_day(self):
        self.write_ledger([
            _record("2026-11-01T23:59:59+08:00", input_tokens=1,
                    output_tokens=1),
            _record("2026-11-02T00:00:00+08:00", input_tokens=2,
                    output_tokens=2),
            _record("2026-11-01T16:30:00Z", input_tokens=3,
                    output_tokens=3),
        ])
        self.write_snapshot()
        res = console_stats.um_today(
            now=datetime(2026, 11, 2, 0, 30, tzinfo=timezone.utc))
        self.assertEqual(res["today"]["day"], "2026-11-02")
        self.assertEqual(res["today"]["tokens"]["total"],
                         {"kind": "exact", "value": 10})
        self.assertEqual(res["today"]["requests"]["recorded"],
                         {"kind": "exact", "value": 2})

    def test_price_versions_are_per_record(self):
        versions = [
            _version(version="v-old", exchange_rate=7.0,
                     effective="2026-10-01T00:00:00+08:00",
                     model=_model_entry(input_price=1.0, output_price=2.0,
                                         cache_price=0.0)),
            _version(version="v-new", exchange_rate=8.0,
                     effective="2026-11-01T12:00:00+08:00",
                     model=_model_entry(input_price=4.0, output_price=20.0,
                                         cache_price=0.0)),
        ]
        res = self.today([
            _record("2026-11-01T11:00:00+08:00", input_tokens=1000,
                    output_tokens=500, cache_tokens=0,
                    cache_status="reported", reasoning_tokens=0,
                    duration_ms=1000),
            _record("2026-11-01T13:00:00+08:00", input_tokens=1000,
                    output_tokens=500, cache_tokens=0,
                    cache_status="reported", reasoning_tokens=0,
                    duration_ms=1000),
        ], snapshot=_snapshot(versions))
        # old: (1*1 + 0.5*2) = 2 USD * 7 = 14 CNY
        # new: (1*4 + 0.5*20) = 14 USD * 8 = 112 CNY
        self.assertEqual(res["cost"]["cny"],
                         {"kind": "exact", "value": 0.126})
        self.assertEqual(res["cost"]["price"]["version"], "v-new")
        self.assertEqual(res["cost"]["fx"]["rate"], 8.0)

    def test_unpriced_model_is_disclosed_and_cost_is_partial(self):
        snapshot = _snapshot([_version(model=_model_entry())])
        snapshot["versions"][0]["models"] = {
            "GLM-Test": _model_entry(),
        }
        res = self.today([
            _record("2026-11-01T10:00:00+08:00", model="GLM-Test",
                    input_tokens=1000, output_tokens=500,
                    cache_tokens=0, cache_status="reported"),
            _record("2026-11-01T10:10:00+08:00", model="Unpriced",
                    input_tokens=1000, output_tokens=500,
                    cache_tokens=0, cache_status="reported"),
        ], snapshot=snapshot)
        self.assertEqual(res["cost"]["coverage"]["unpricedRequests"], 1)
        self.assertEqual(res["cost"]["cny"]["kind"], "atLeast")
        self.assertIn("未定价", res["cost"]["cny"]["note"])

    def test_missing_snapshot_is_not_zero_cost(self):
        res = self.today([
            _record("2026-11-01T10:00:00+08:00", input_tokens=1000,
                    output_tokens=500, cache_tokens=0,
                    cache_status="reported"),
        ], snapshot=None)
        # The snapshot file is absent in this case.
        self.snapshot.unlink()
        self.status.unlink()
        res = console_stats.um_today(now=NOW)
        self.assertEqual(res["cost"]["price"]["state"], "unsynced")
        self.assertEqual(res["cost"]["cny"]["kind"], "missing")
        self.assertNotEqual(res["cost"]["cny"],
                            {"kind": "exact", "value": 0})

    def test_started_before_first_effective_stays_unpriced(self):
        """A2-8: every UM read surface carries the request START.

        This request completed after the first effective snapshot but STARTED
        before it, so today/trend/requests must all keep it unpriced instead
        of repricing it by completion time. The summary path already reads the
        raw ledger record; the normalized event must not drop the field.
        """
        record = _record("2026-11-01T11:00:00+08:00",
                         input_tokens=1000000, output_tokens=0,
                         cache_tokens=0, cache_status="reported")
        record["request_started_at"] = "2026-11-01T09:00:00+08:00"
        event = console_stats._event_from_record(
            console_stats.UM_LEDGER_SOURCE,
            console_stats._SOURCE_SPECS[console_stats.UM_LEDGER_SOURCE],
            record)
        self.assertEqual(
            console_stats._um_event_record(event)["request_started_at"],
            usagesrv._parse_ts(record["request_started_at"]),
            "_um_event_record 必须把 request_started_at 转发给价格契约")
        snapshot = _snapshot([_version(
            effective="2026-11-01T10:00:00+08:00")])

        res = self.today([record], snapshot=snapshot)
        summary = usagesrv._um_reference_summary([record])
        self.assertIsNone(summary["cny"])
        self.assertEqual(
            summary["coverage"]["preEffectiveRequests"], 1)
        self.assertEqual(res["cost"]["cny"]["kind"], "missing")
        self.assertEqual(
            res["cost"]["coverage"]["preEffectiveRequests"], 1)

        trend = self.trend([record], snapshot=snapshot)
        day_index = trend["trend"]["days"].index("2026-11-01")
        model = trend["trend"]["models"][0]
        self.assertEqual(model["daily"][day_index]["costCny"]["kind"],
                         "missing")
        self.assertEqual(
            trend["trend"]["all"]["daily"][day_index]["costCny"]["kind"],
            "missing")

        self.write_ledger([record])
        self.write_snapshot(snapshot)
        page = console_stats.um_requests(now=NOW, window="today")
        self.assertEqual(len(page["page"]["rows"]), 1)
        row = page["page"]["rows"][0]
        self.assertEqual(row["costCny"]["kind"], "missing")
        self.assertIn("首次价格生效前", row["costCny"]["note"])

    def test_historical_row_without_start_keeps_completion_pricing(self):
        """A2-8: a row without start evidence keeps completion-time pricing.

        The read surface must not infer a start from ``time - duration_ms``:
        the normalized event and the ledger dict forwarded to the pricing
        contract both carry ``request_started_at=None``.
        """
        record = _record("2026-11-01T11:00:00+08:00",
                         input_tokens=1000000, output_tokens=0,
                         cache_tokens=0, cache_status="reported")
        self.assertNotIn("request_started_at", record)
        event = console_stats._event_from_record(
            console_stats.UM_LEDGER_SOURCE,
            console_stats._SOURCE_SPECS[console_stats.UM_LEDGER_SOURCE],
            record)
        self.assertIsNone(event.request_started_at)
        self.assertIsNone(
            console_stats._um_event_record(event)["request_started_at"])
        snapshot = _snapshot([_version(
            effective="2026-11-01T10:00:00+08:00")])

        res = self.today([record], snapshot=snapshot)
        self.assertEqual(res["cost"]["cny"],
                         {"kind": "exact", "value": 14.4})
        self.assertEqual(
            res["cost"]["coverage"]["preEffectiveRequests"], 0)


class RetryAndRequestTests(_UmFixture):
    def test_physical_retry_is_not_a_request(self):
        res = self.today([
            _record("2026-11-01T10:00:00+08:00", input_tokens=100,
                    output_tokens=50, cache_tokens=0,
                    cache_status="reported", duration_ms=1000,
                    request_id="a" * 32),
            _record("2026-11-01T10:00:01+08:00", input_tokens=10,
                    output_tokens=5, cache_tokens=0,
                    cache_status="unreported", duration_ms=2000,
                    status="upstream_transient_retried",
                    usage_source="estimated", request_id="a" * 32),
        ])
        today = res["today"]
        self.assertEqual(today["requests"]["recorded"],
                         {"kind": "exact", "value": 1})
        self.assertEqual(today["tokens"]["input"]["kind"], "estimate")
        self.assertEqual(today["tokens"]["input"]["value"], 110)
        self.assertEqual(today["tokens"]["output"]["value"], 55)
        self.assertEqual(today["speed"]["value"],
                         {"kind": "exact", "value": 50.0})
        self.assertEqual(res["coverage"]["retry_records"], 1)

        page = console_stats.um_requests(now=NOW, window="today")
        self.assertEqual(len(page["page"]["rows"]), 1)
        row = page["page"]["rows"][0]
        self.assertEqual(row["requestId"], "a" * 32)
        self.assertEqual(len(row["retries"]), 1)
        self.assertIn("物理重试", row["retries"][0]["note"])

    def test_retry_without_reliable_id_is_disclosed_not_counted(self):
        res = self.today([
            _record("2026-11-01T10:00:00+08:00", input_tokens=100,
                    output_tokens=50, cache_tokens=0,
                    cache_status="reported", duration_ms=1000,
                    request_id=None),
            _record("2026-11-01T10:00:01+08:00", input_tokens=10,
                    output_tokens=5, cache_tokens=0,
                    cache_status="unreported", duration_ms=2000,
                    status="degenerate_repetition_retried",
                    usage_source="estimated", request_id=None),
        ])
        self.assertEqual(res["today"]["requests"]["recorded"],
                         {"kind": "exact", "value": 1})
        page = console_stats.um_requests(now=NOW, window="today")
        row = page["page"]["rows"][0]
        self.assertIsNone(row["requestId"])
        self.assertIsNone(row["retries"])
        self.assertEqual(page["coverage"]["retry_records"], 1)
        self.assertEqual(page["coverage"]["unattached_retries"], 1)

    def test_result_filters_search_and_pagination(self):
        records = [
            _record("2026-11-01T09:00:00+08:00", model="m-a",
                    status="ok", input_tokens=10, output_tokens=5,
                    duration_ms=1000, request_id="a" * 32),
            _record("2026-11-01T10:00:00+08:00", model="m-b",
                    status="HTTP529", input_tokens=20, output_tokens=10,
                    duration_ms=1000, request_id="b" * 32),
            _record("2026-11-01T11:00:00+08:00", model="m-c",
                    status="upstream_missing_finish_reason",
                    input_tokens=30, output_tokens=15,
                    duration_ms=1000, request_id="c" * 32),
        ]
        page = self.requests(records, window="today", limit=2)
        self.assertEqual(len(page["page"]["rows"]), 2)
        self.assertIsNotNone(page["page"]["earlier"])
        self.assertEqual([row["model"] for row in page["page"]["rows"]],
                         ["m-c", "m-b"])
        older = self.requests(
            records, window="today", limit=2,
            cursor=page["page"]["earlier"])
        self.assertEqual([row["model"] for row in older["page"]["rows"]],
                         ["m-a"])

        failed = self.requests(records, window="today", result="failed")
        self.assertEqual([row["model"] for row in failed["page"]["rows"]],
                         ["m-b"])
        truncated = self.requests(records, window="today",
                                  result="truncated")
        self.assertEqual(truncated["page"]["rows"][0]["model"], "m-c")
        self.assertEqual(truncated["page"]["rows"][0]["httpStatus"], 200)
        model = self.requests(records, window="today", model="m-a")
        self.assertEqual([row["model"] for row in model["page"]["rows"]],
                         ["m-a"])
        search = self.requests(records, window="today",
                                search="http529")
        self.assertEqual([row["model"] for row in search["page"]["rows"]],
                         ["m-b"])
        self.assertEqual(search["page"]["rows"][0]["error"]["code"],
                         "HTTP529")

    def test_invalid_filters_and_cursor_are_errors_not_empty(self):
        self.write_ledger([])
        with self.assertRaises(ValueError):
            console_stats.um_requests(window="bogus", now=NOW)
        with self.assertRaises(ValueError):
            console_stats.um_requests(window="today", result="bogus", now=NOW)
        with self.assertRaises(ValueError):
            console_stats.um_requests(window="today", cursor="not-a-cursor",
                                       now=NOW)
        with self.assertRaises(ValueError):
            console_stats.um_trend(range="bogus", now=NOW)

    def test_trend_seven_days_same_window_ranking(self):
        res = self.trend([
            _record("2026-10-26T10:00:00+08:00", model="m-old",
                    input_tokens=100, output_tokens=50, duration_ms=1000,
                    cache_tokens=0, cache_status="reported"),
            _record("2026-11-01T10:00:00+08:00", model="m-new",
                    input_tokens=1000, output_tokens=500, duration_ms=2000,
                    cache_tokens=0, cache_status="reported"),
        ])
        trend = res["trend"]
        self.assertEqual(trend["range"], "7d")
        self.assertEqual(len(trend["days"]), 7)
        self.assertEqual(trend["days"][-1], "2026-11-01")
        self.assertEqual(trend["speedBasis"], "whole_request")
        self.assertEqual(len(trend["all"]["daily"]), 7)
        self.assertEqual([row["model"] for row in trend["models"]],
                         ["m-new", "m-old"])
        self.assertEqual(trend["models"][0]["total"]["tokens"],
                         {"kind": "exact", "value": 1500})
        self.assertEqual(trend["models"][0]["total"]["requests"],
                         {"kind": "exact", "value": 1})
        self.assertEqual(trend["models"][0]["total"]["speed"],
                         {"kind": "exact", "value": 250.0})
        self.assertEqual(trend["all"]["total"]["tokens"],
                         {"kind": "exact", "value": 1650})
        self.assertEqual(trend["all"]["total"]["requests"],
                         {"kind": "exact", "value": 2})


class MaintenanceTests(unittest.TestCase):
    def _manager(self, operations=None, error=None):
        class FakeManager:
            def __init__(self):
                self.operations = operations or []
                self.error = error

            def list_operations(self, limit=50):
                if self.error is not None:
                    raise self.error
                return self.operations[:limit]

        return FakeManager()

    def _operation(self, op_id, action, target="", state="succeeded",
                   created_at=1000.0, finished_at=1001.0, steps=None,
                   result=None, error=None):
        return {
            "id": op_id,
            "action": action,
            "target": target,
            "state": state,
            "created_at": created_at,
            "finished_at": finished_at,
            "steps": steps or [],
            "result": result,
            "error": error,
        }

    def test_original_operations_filtered_to_um_actions(self):
        manager = self._manager([
            self._operation("op-verify", "account.um_verify",
                            state="succeeded", created_at=1000.0,
                            steps=[{"at": 1000.5, "note": "核验开始"}],
                            result={"verify": "valid"}),
            self._operation("op-speed", "model.speedtest",
                            target="GLM-Test", created_at=1001.0),
            self._operation("op-start", "service.start",
                            target="chat2api", state="running",
                            created_at=1002.0),
            self._operation("op-qoder", "service.start",
                            target="qoder-cn", created_at=1003.0),
            self._operation("op-status", "account.um_status",
                            target="um", state="interrupted",
                            created_at=1004.0),
        ])
        res = usagesrv.um_maintenance(manager=manager)
        self.assertEqual([row["key"] for row in res["rows"]],
                         ["op-status", "op-start", "op-verify"])
        self.assertEqual(res["rows"][0]["state"], "interrupted")
        self.assertEqual(res["rows"][1]["state"], "running")
        self.assertEqual(res["rows"][2]["steps"],
                         [{"at": 1000500, "note": "核验开始"}])
        self.assertIn("verify=valid", res["rows"][2]["result"])
        self.assertIsNone(res["earlier"])
        self.assertTrue(res["coverage"]["bounded"])
        self.assertEqual(res["coverage"]["operations_read"], 5)
        self.assertEqual(res["coverage"]["rows_returned"], 3)

    def test_failure_is_unavailable_not_empty(self):
        manager = self._manager(error=RuntimeError("database locked"))
        res = usagesrv.um_maintenance(manager=manager)
        self.assertEqual(res["coverage"]["status"], "unavailable")
        self.assertEqual(res["rows"], [])
        self.assertIn("database locked", res["coverage"]["error"])

    def test_legal_empty_is_ok(self):
        res = usagesrv.um_maintenance(manager=self._manager([]))
        self.assertEqual(res["coverage"]["status"], "ok")
        self.assertEqual(res["rows"], [])


class _FakeManager:
    def __init__(self):
        self.operations = []

    def list_operations(self, limit=50):
        return self.operations[:limit]


class UmHttpRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        root = Path(cls._tmp.name)
        cls.ledger = root / "usage-chat2api.jsonl"
        cls.snapshot = root / "magpie-prices.snapshot.json"
        cls.status = root / "magpie-prices.status.json"
        cls.ledger.write_text(json.dumps(_record(
            "2026-11-01T10:00:00+08:00", input_tokens=1000,
            output_tokens=500, cache_tokens=0, cache_status="reported",
            request_id="a" * 32)) + "\n", encoding="utf-8")
        cls.snapshot.write_text(json.dumps(_snapshot()), encoding="utf-8")
        cls.status.write_text(json.dumps({
            "format": "um-magpie-price-status-v1",
            "state": "synced",
        }), encoding="utf-8")
        cls._saved = {
            name: getattr(usagesrv, name) for name in (
                "CHAT2API_LOG", "MAGPIE_PRICE_SNAPSHOT_FILE",
                "MAGPIE_PRICE_STATUS_FILE", "CONSOLE_STATE_FILE",
                "CONSOLE_LOCK_DIR", "CLOUD_ROLE", "get_console_manager")}
        cls.addClassCleanup(cls._restore)
        usagesrv.CHAT2API_LOG = str(cls.ledger)
        usagesrv.MAGPIE_PRICE_SNAPSHOT_FILE = str(cls.snapshot)
        usagesrv.MAGPIE_PRICE_STATUS_FILE = str(cls.status)
        usagesrv.CONSOLE_STATE_FILE = str(root / "console.sqlite3")
        usagesrv.CONSOLE_LOCK_DIR = str(root / "console-locks")
        console_stats.configure(_UsagesrvStatsEnvironment())
        cls._fake_manager = _FakeManager()
        usagesrv.get_console_manager = lambda: cls._fake_manager
        usagesrv.CLOUD_ROLE = False
        cls._env = mock.patch.dict(os.environ, {
            "CHAT2API_SERVICE_LOG": str(root / "missing-service.log"),
        })
        cls._env.start()
        cls.addClassCleanup(cls._env.stop)
        # The HTTP routes read the wall clock; pin their "now" to NOW so the
        # fixed 2026-11-01 ledger row stays inside the "today" window on any
        # run date (the assertion itself is unchanged).
        cls._now_ms_original = console_stats._now_ms
        cls._now_patch = mock.patch.object(
            console_stats, "_now_ms",
            side_effect=lambda now=None: (
                int(NOW.timestamp() * 1000) if now is None
                else cls._now_ms_original(now)))
        cls._now_patch.start()
        cls.addClassCleanup(cls._now_patch.stop)
        cls.server = usagesrv.ThreadingHTTPServer(
            ("127.0.0.1", 0), usagesrv.Handler)
        cls.addClassCleanup(cls.server.shutdown)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever,
                          daemon=True).start()

    @classmethod
    def _restore(cls):
        for name, value in cls._saved.items():
            setattr(usagesrv, name, value)

    def _get(self, path):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, json.loads(raw)

    def test_read_routes(self):
        status, payload = self._get("/api/console/um/today")
        self.assertEqual(status, 200)
        self.assertIn("today", payload)
        self.assertIn("cost", payload)
        status, payload = self._get("/api/console/um/trend?range=7d")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["trend"]["days"]), 7)
        status, payload = self._get(
            "/api/console/um/requests?window=today&limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["page"]["rows"]), 1)
        status, payload = self._get("/api/console/um/logs?source=service")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "missing")
        self.assertEqual(payload["lines"], [])
        status, payload = self._get("/api/console/um/maintenance")
        self.assertEqual(status, 200)
        self.assertEqual(payload["coverage"]["status"], "ok")

    def test_invalid_query_is_400(self):
        for path in (
                "/api/console/um/requests?window=bogus",
                "/api/console/um/requests?result=bogus",
                "/api/console/um/trend?range=bogus",
                "/api/console/um/logs?source=bogus",
                "/api/console/um/logs?levels=bogus",
                "/api/console/um/request-logs?request_id=short",
                "/api/console/um/requests?cursor=not-a-cursor"):
            status, payload = self._get(path)
            self.assertEqual(status, 400, path)
            self.assertEqual(payload["error"], "invalid_um_query", path)

    def test_cloud_role_is_explicitly_unavailable(self):
        usagesrv.CLOUD_ROLE = True
        try:
            status, payload = self._get("/api/console/um/today")
        finally:
            usagesrv.CLOUD_ROLE = False
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "capability_not_assembled")
        self.assertEqual(payload["capability"], "um")

    def test_unconfigured_environment_is_a_502_never_an_empty_payload(self):
        """E19-1-03: the HTTP face inherits the read surface's refusal.

        With no injected ``StatsEnvironment`` the route must NOT answer 200 with an
        empty "no requests today" payload (that would be a fabricated real zero).
        The read surface raises ``StatsNotConfigured``; the route maps it to its
        documented 502 failure shape.
        """
        saved = console_stats._STATS_ENVIRONMENT
        console_stats._STATS_ENVIRONMENT = None
        try:
            for path in ("/api/console/um/today", "/api/console/um/trend?range=7d",
                         "/api/console/um/requests?window=today"):
                status, payload = self._get(path)
                self.assertEqual(status, 502, path)
                self.assertEqual(payload["error"], "um_query_failed", path)
        finally:
            console_stats._STATS_ENVIRONMENT = saved


class LogReadSurfaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.service = self.dir / "chat2api.log"
        self.ledger = self.dir / "usage-chat2api.jsonl"
        self.ledger.write_text("", encoding="utf-8")
        self._saved = os.environ.get("CHAT2API_SERVICE_LOG")
        self.addCleanup(self._restore)
        os.environ["CHAT2API_SERVICE_LOG"] = str(self.service)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("CHAT2API_SERVICE_LOG", None)
        else:
            os.environ["CHAT2API_SERVICE_LOG"] = self._saved

    def _write_log(self, lines):
        self.service.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_bounded_window_and_reliable_request_id(self):
        self._write_log([
            "2026-11-01 10:00:00 INFO request=%s model=GLM-Test start"
            % ("a" * 32),
            "2026-11-01 10:00:01 INFO request=%s model=GLM-Test short"
            % ("b" * 12),
            "2026-11-01 10:00:02 INFO Authorization: Bearer "
            "sk-supersecretvalue12345 token=abc model=GLM-Test",
        ])
        res = console_log_reader.read_um_logs(
            window="today", now=NOW,
            ledger_paths={"chat2api": str(self.ledger)})
        self.assertEqual(res["status"], "ok")
        self.assertEqual(len(res["lines"]), 3)
        by_text = {line["text"]: line for line in res["lines"]}
        full = next(line for line in res["lines"]
                    if "aaaaaaaa" in line["text"])
        short = next(line for line in res["lines"]
                     if "bbbbbbbb" in line["text"])
        self.assertEqual(full["requestId"], "a" * 32)
        self.assertIsNone(short["requestId"])
        secret = next(line for line in res["lines"]
                      if "Authorization" in line["text"])
        self.assertNotIn("supersecretvalue", secret["text"])
        self.assertIn("<redacted>", secret["text"])
        self.assertEqual(res["coverage"]["rows_read"], 3)

    def test_missing_source_and_cursor_are_distinct(self):
        res = console_log_reader.read_um_logs(
            window="today", now=NOW,
            ledger_paths={"chat2api": str(self.ledger)})
        self.assertEqual(res["status"], "missing")
        self.assertEqual(res["lines"], [])
        self.assertEqual(res["coverage"]["status"], "missing")
        bad = console_log_reader.read_um_logs(
            window="today", source="bogus", now=NOW,
            ledger_paths={"chat2api": str(self.ledger)})
        self.assertEqual(bad["status"], "error")
        self.assertEqual(bad["error"], "unknown_log_source")
        self.assertEqual(bad["lines"], [])
        self._write_log(["2026-11-01 10:00:00 INFO request=%s" % ("a" * 32)])
        bad_cursor = console_log_reader.read_um_logs(
            window="today", cursor="not-a-cursor", now=NOW,
            ledger_paths={"chat2api": str(self.ledger)})
        self.assertEqual(bad_cursor["status"], "error")
        self.assertEqual(bad_cursor["error"], "invalid_cursor")

    def test_log_pagination(self):
        self._write_log([
            "2026-11-01 10:00:00 INFO request=%s first" % ("a" * 32),
            "2026-11-01 10:00:01 INFO request=%s second" % ("a" * 32),
        ])
        page = console_log_reader.read_um_logs(
            window="today", limit=1, now=NOW,
            ledger_paths={"chat2api": str(self.ledger)})
        self.assertEqual(len(page["lines"]), 1)
        self.assertIn("second", page["lines"][0]["text"])
        self.assertIsNotNone(page["earlier"])
        older = console_log_reader.read_um_logs(
            window="today", limit=1, cursor=page["earlier"], now=NOW,
            ledger_paths={"chat2api": str(self.ledger)})
        self.assertEqual(len(older["lines"]), 1)
        self.assertIn("first", older["lines"][0]["text"])

    def test_request_logs_require_reliable_id(self):
        self._write_log([
            "2026-11-01 10:00:00 INFO request=%s model=GLM-Test start"
            % ("a" * 32),
            "2026-11-01 10:00:01 INFO request=%s model=GLM-Test short"
            % ("b" * 12),
        ])
        res = console_log_reader.read_um_request_logs(
            "a" * 32, ledger_paths={"chat2api": str(self.ledger)})
        self.assertEqual(len(res["lines"]), 1)
        self.assertEqual(res["lines"][0]["requestId"], "a" * 32)
        with self.assertRaises(ValueError):
            console_log_reader.read_um_request_logs(
                "b" * 12, ledger_paths={"chat2api": str(self.ledger)})
        empty = console_log_reader.read_um_request_logs(
            "c" * 32, ledger_paths={"chat2api": str(self.ledger)})
        self.assertEqual(empty["lines"], [])
        self.assertIn("bounded", empty["coverage"])


if __name__ == "__main__":
    unittest.main()
