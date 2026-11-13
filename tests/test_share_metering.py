# -*- coding: utf-8 -*-
"""分享 Key 可信归因与原始用量的隔离回归（C2/C3 线）。

覆盖三组契约：

1. ``ShareMeteringReadTests``：``share_metering.read_share_usage`` 只读、有界、
   脱敏，按 Key 归因，未知/未报告/中断分列；只输出原始用量（请求数、Token、
   缓存报告），费用/价格字段已随 #30 退役。
2. ``ShareMeteringRequestPageTests``：逐请求页同一账本、同一筛选、游标分页，
   逐行保留原始用量。
3. ``PrivateSummaryIsolationTests``：同一私人账本夹具下加入分享请求前后
   ``usagesrv.build_summary()`` 逐字段不变（不读生产账本）。

全部文件输入重定向到临时目录：不读生产账本、不重启任何服务、不发真实模型请求。
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]

_SUMMARY_TMP = tempfile.TemporaryDirectory(prefix="share-metering-summary-")
_SUMMARY_DIR = Path(_SUMMARY_TMP.name)
_ATTRIBUTION_TMP = tempfile.TemporaryDirectory(prefix="share-metering-key-")
_REDIRECTED_ENV = {
    "CHAT2API_USAGE_LOG": str(_SUMMARY_DIR / "usage-chat2api.jsonl"),
    "DSH_LEDGER_SNAPSHOT_FILE": str(_SUMMARY_DIR / "dsh.json"),
    "USAGE_SESSION_CODE_FILE": str(_SUMMARY_DIR / "session-code"),
    "USAGE_CONTROL_TOKEN_FILE": str(_SUMMARY_DIR / "control-token"),
    "CHAT2API_SHARE_ATTRIBUTION_KEY_FILE": str(
        Path(_ATTRIBUTION_TMP.name) / "share-attribution.key"),
}
_SAVED_ENV = {name: os.environ.get(name) for name in _REDIRECTED_ENV}
os.environ.update(_REDIRECTED_ENV)
os.environ.pop("SHARE_ATTRIBUTION_KEY_FILE", None)


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, HERE / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


metering = _load("share_metering", "share/metering.py")
usagesrv = _load("usagesrv_metering", "usagesrv.py")

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

for _name, _value in _SAVED_ENV.items():
    if _value is None:
        os.environ.pop(_name, None)
    else:
        os.environ[_name] = _value
del _name, _value


def record(**fields):
    """一条账本记录夹具：字段与写盘端 record_usage 一致，可局部覆盖。"""
    rec = {
        "time": "2026-09-25T10:00:00+08:00",
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
        "key_id": "key-a",
        "request_id": "r1",
        "attribution": "verified",
    }
    rec.update(fields)
    return rec


class MeteringFixture(unittest.TestCase):
    """一个临时分享账本；每条记录一行 JSON。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = Path(self._tmp.name) / "usage-share-backend.jsonl"

    def write(self, records, name=None):
        path = Path(self._tmp.name) / (name or self.ledger.name)
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                                 for r in records) + "\n", encoding="utf-8")
        return path


class ShareMeteringReadTests(MeteringFixture):
    def test_two_protocols_one_key_are_one_key(self):
        self.write([
            record(protocol="chat", request_id="r1"),
            record(protocol="messages", request_id="r2", stream=True),
        ])
        result = metering.read_share_usage(str(self.ledger))
        self.assertEqual(result["requests"], 2)
        self.assertEqual([k["key_id"] for k in result["keys"]], ["key-a"])
        key = result["keys"][0]
        self.assertEqual(key["requests"], 2)
        self.assertEqual(key["unknownRequests"], 0)
        self.assertEqual(result["unattributed"]["requests"], 0)

    def test_two_keys_never_cross_attribution(self):
        self.write([
            record(key_id="key-a", request_id="a1"),
            record(key_id="key-b", request_id="b1"),
            record(key_id="key-a", request_id="a2"),
            record(key_id="key-b", request_id="b2", model="other-model"),
        ])
        result = metering.read_share_usage(str(self.ledger))
        by_key = {k["key_id"]: k for k in result["keys"]}
        self.assertEqual(sorted(by_key), ["key-a", "key-b"])
        self.assertEqual(by_key["key-a"]["requests"], 2)
        self.assertEqual(by_key["key-b"]["requests"], 2)
        self.assertEqual([m["model"] for m in by_key["key-a"]["models"]],
                         ["GLM-5.2-NVFP4"])
        self.assertEqual([m["model"] for m in by_key["key-b"]["models"]],
                         ["GLM-5.2-NVFP4", "other-model"])

    def test_rotated_and_deleted_key_history_stays_reviewable(self):
        self.write([
            record(key_id="key-a", request_id="before-rotate",
                   time="2026-09-24T10:00:00+08:00"),
            record(key_id="key-a", request_id="after-rotate"),
            record(key_id="key-gone", request_id="deleted-key",
                   protocol="messages"),
        ])
        result = metering.read_share_usage(str(self.ledger))
        by_key = {k["key_id"]: k for k in result["keys"]}
        self.assertEqual(sorted(by_key), ["key-a", "key-gone"])
        self.assertEqual(by_key["key-a"]["requests"], 2)
        self.assertEqual(by_key["key-gone"]["requests"], 1)

    def test_missing_usage_is_unknown_not_zero(self):
        self.write([
            record(usage_source="none", input_tokens=0, output_tokens=0,
                   total_tokens=0, request_id="unknown-usage"),
            record(input_tokens=0, output_tokens=0, total_tokens=0,
                   request_id="real-zero"),
        ])
        result = metering.read_share_usage(str(self.ledger))
        key = result["keys"][0]
        self.assertEqual(key["requests"], 2)
        self.assertEqual(key["unknownRequests"], 1)
        self.assertEqual([m["requests"] for m in key["models"]], [1])
        self.assertEqual(result["coverage"]["unknownUsageRequests"], 1)

    def test_cache_report_status_distinguishes_unreported_from_zero(self):
        self.write([
            record(cache_report_status="unreported",
                   cached_input_tokens=0, request_id="unreported"),
            record(cache_report_status="reported", cached_input_tokens=0,
                   request_id="explicit-zero"),
            record(cache_report_status="reported", cached_input_tokens=20,
                   request_id="reported-hit"),
        ])
        usage = metering.read_share_usage(str(self.ledger))["keys"][0][
            "tokenUsage"]
        self.assertEqual(usage["cacheReportedRequests"], 2)
        self.assertEqual(usage["cacheUnreportedRequests"], 1)
        self.assertEqual(usage["cacheUnknownRequests"], 0)
        self.assertEqual(usage["cachedInputTokens"], 20)

    def test_legacy_cache_rows_stay_unknown_not_reported(self):
        legacy = record()
        legacy.pop("cache_report_status", None)
        legacy.update({"cached_input_tokens": 20,
                       "request_id": "legacy-cache"})
        self.write([legacy])
        usage = metering.read_share_usage(str(self.ledger))["keys"][0][
            "tokenUsage"]
        self.assertEqual(usage["cacheReportedRequests"], 0)
        self.assertEqual(usage["cacheUnreportedRequests"], 0)
        self.assertEqual(usage["cacheUnknownRequests"], 1)
        self.assertIsNone(usage["cachedInputTokens"])

    def test_legacy_unattributed_records_are_reviewable(self):
        legacy = {
            "time": "2026-09-24T10:00:00+08:00",
            "service": "chat2api",
            "model": "GLM-5.2-NVFP4",
            "stream": False,
            "status": "ok",
            "input_tokens": 1000,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 1000,
            "estimated": True,
            "duration_ms": 100,
        }
        self.write([legacy])
        result = metering.read_share_usage(str(self.ledger))
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["keys"], [])
        self.assertEqual(result["unattributed"]["requests"], 1)
        self.assertEqual(result["unattributed"]["key_id"], None)
        self.assertEqual(result["coverage"]["attributionReasons"], {"legacy": 1})

    def test_interrupted_failed_and_retried_are_lower_bounds(self):
        self.write([
            record(status="client_disconnected", request_id="cut",
                   usage_source="partial", output_tokens=0),
            record(status="upstream_read_error", request_id="failed",
                   usage_source="estimated"),
            record(status="upstream_transient_retried", request_id="retried",
                   usage_source="estimated"),
            record(status="ok", request_id="complete"),
        ])
        result = metering.read_share_usage(str(self.ledger))
        key = result["keys"][0]
        self.assertEqual(key["requests"], 4)
        self.assertEqual(result["coverage"]["incompleteRequests"], 3)
        self.assertEqual(key["unknownRequests"], 0)

    def test_cross_restart_history_is_continuous(self):
        self.write([record(request_id="before-restart")])
        first = metering.read_share_usage(str(self.ledger))
        with open(self.ledger, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record(request_id="after-restart",
                                         key_id="key-b")) + "\n")
        second = metering.read_share_usage(str(self.ledger))
        self.assertEqual(first["keys"][0]["requests"], 1)
        self.assertEqual(second["keys"][0], first["keys"][0])
        by_key = {k["key_id"]: k for k in second["keys"]}
        self.assertEqual(sorted(by_key), ["key-a", "key-b"])
        self.assertEqual(by_key["key-b"]["requests"], 1)

    def test_missing_attribution_is_never_guessed(self):
        self.write([
            record(key_id=None, attribution="absent", request_id="direct"),
            record(key_id=None, attribution="bad_signature", request_id="forged"),
            record(key_id="", attribution="replayed", request_id="replay"),
            record(key_id="key-a", request_id="attributed"),
        ])
        result = metering.read_share_usage(str(self.ledger))
        self.assertEqual([k["key_id"] for k in result["keys"]], ["key-a"])
        self.assertEqual(result["keys"][0]["requests"], 1)
        self.assertEqual(result["unattributed"]["requests"], 3)
        self.assertEqual(result["coverage"]["attributed"], 1)
        self.assertEqual(result["coverage"]["unattributed"], 3)
        self.assertEqual(result["coverage"]["attributionReasons"],
                         {"absent": 1, "bad_signature": 1, "replayed": 1})

    def test_unknown_time_is_reported_without_a_day(self):
        self.write([record(time="not-a-timestamp")])
        key = metering.read_share_usage(str(self.ledger))["keys"][0]
        self.assertEqual(key["requests"], 1)
        self.assertEqual(key["days"], [])
        self.assertEqual(
            metering.read_share_usage(str(self.ledger))[
                "coverage"]["unknownTimeRequests"], 1)

    def test_limit_is_bounded_and_reported(self):
        self.write([record(request_id=f"r{i}") for i in range(5)])
        result = metering.read_share_usage(str(self.ledger), limit=2)
        self.assertEqual(result["requests"], 2)
        self.assertEqual(result["coverage"]["recordsTotal"], 5)
        self.assertTrue(result["coverage"]["truncated"])
        self.assertEqual(result["coverage"]["limit"], 2)
        self.assertEqual(result["keys"][0]["requests"], 2)

    def test_missing_ledger_is_an_empty_result(self):
        missing = Path(self._tmp.name) / "absent.jsonl"
        result = metering.read_share_usage(str(missing))
        self.assertEqual(result["requests"], 0)
        self.assertEqual(result["keys"], [])
        self.assertEqual(result["unattributed"]["requests"], 0)
        self.assertFalse(result["coverage"]["ledgerPresent"])
        self.assertFalse(missing.exists())

    def test_broken_lines_are_counted_not_silently_dropped(self):
        self.write([record()])
        with open(self.ledger, "a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        result = metering.read_share_usage(str(self.ledger))
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["coverage"]["unparsedLines"], 1)

    def test_private_ledger_is_refused(self):
        private = Path(self._tmp.name) / "usage-chat2api.jsonl"
        private.write_text(json.dumps(record()) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            metering.read_share_usage(str(private))

    def test_read_is_read_only(self):
        self.write([record()])
        before = self.ledger.read_bytes()
        stat_before = self.ledger.stat().st_mtime_ns
        metering.read_share_usage(str(self.ledger))
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(self.ledger.stat().st_mtime_ns, stat_before)

    def test_no_secret_material_in_the_result(self):
        self.write([record()])
        blob = json.dumps(metering.read_share_usage(str(self.ledger)))
        self.assertNotIn("secret", blob.lower())
        self.assertNotIn(str(self._tmp.name), blob)

    def test_no_price_fields_or_pricer_in_the_result(self):
        self.write([record()])
        blob = json.dumps(metering.read_share_usage(str(self.ledger)))
        for field in ("costUsd", "knownCostUsd", "estimatedCostUsd",
                      "costCny", "knownCostCny", "estimatedCostCny",
                      "costCnyPartial", "priceVersions", "priceFn",
                      "rateFn", "unpricedRequests", "unpricedModels"):
            with self.subTest(field=field):
                self.assertNotIn(field, blob)
        self.assertNotIn("price_fn", metering.read_share_usage.__code__.co_varnames)
        self.assertNotIn("price_fn",
                         metering.read_share_requests.__code__.co_varnames)


class ShareMeteringRequestPageTests(MeteringFixture):
    """r14 逐请求页：同一账本、同一筛选，新→旧游标分页。"""

    NOW = datetime(2026, 9, 25, 13, 0, tzinfo=timezone.utc).timestamp()

    def _times(self, *hours):
        return [datetime(2026, 9, 25, hour, 0, tzinfo=timezone(timedelta(hours=8)))
                .isoformat() for hour in hours]

    def test_aggregate_uses_the_same_filters_as_the_request_page(self):
        times = self._times(10, 11, 12)
        self.write([
            record(time=times[0], key_id="key-a", model="model-a",
                   request_id="ok"),
            record(time=times[1], key_id="key-b", model="model-b",
                   status="upstream_read_error", request_id="failed"),
            record(time=times[2], key_id=None, model="model-a",
                   request_id="unattributed"),
        ])
        result = metering.read_share_usage(
            str(self.ledger), window="24h", key_id="key-a",
            model="model-a", result="success", now=self.NOW)
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["filters"], {
            "window": "24h", "key": "key-a", "model": "model-a",
            "result": "success"})
        self.assertEqual(result["coverage"]["recordsScanned"], 3)
        self.assertEqual(result["coverage"]["recordsMatched"], 1)

    def test_page_is_newest_first_and_paginates_by_cursor(self):
        times = self._times(10, 11, 12)
        self.write([
            record(time=times[0], request_id="r1"),
            record(time=times[1], request_id="r2"),
            record(time=times[2], request_id="r3"),
        ])
        first = metering.read_share_requests(
            str(self.ledger), limit=2, now=self.NOW)
        self.assertEqual([row["requestId"] for row in first["rows"]],
                         ["r3", "r2"])
        self.assertEqual(first["rows"][0]["durationMs"], 1000)
        self.assertEqual(first["nextCursor"], "2")
        self.assertIs(first["truncated"], True)
        self.assertEqual(first["coverage"]["recordsMatched"], 3)
        self.assertEqual(first["coverage"]["recordsReturned"], 2)
        second = metering.read_share_requests(
            str(self.ledger), limit=2, cursor="2", now=self.NOW)
        self.assertEqual([row["requestId"] for row in second["rows"]], ["r1"])
        self.assertIsNone(second["nextCursor"])
        self.assertIs(second["truncated"], False)

    def test_today_window_is_the_beijing_calendar_day(self):
        times = [
            datetime(2026, 9, 24, 23, 30, tzinfo=timezone(timedelta(hours=8)))
            .isoformat(),
            datetime(2026, 9, 25, 0, 0, tzinfo=timezone(timedelta(hours=8)))
            .isoformat(),
            datetime(2026, 9, 25, 20, 30, tzinfo=timezone(timedelta(hours=8)))
            .isoformat(),
        ]
        self.write([
            record(time=times[0], request_id="yesterday"),
            record(time=times[1], request_id="midnight"),
            record(time=times[2], request_id="tonight"),
        ])
        page = metering.read_share_requests(
            str(self.ledger), window="today", now=self.NOW)
        self.assertEqual({row["requestId"] for row in page["rows"]},
                         {"midnight", "tonight"})
        self.assertEqual(page["filters"]["window"], "today")

    def test_window_key_model_and_result_filters(self):
        times = self._times(10, 11, 12)
        self.write([
            record(time=times[0], key_id="key-a", model="model-a",
                   request_id="ok"),
            record(time=times[1], key_id="key-b", model="model-b",
                   status="upstream_read_error", request_id="failed"),
            record(time=times[2], key_id=None, model="model-a",
                   status="client_disconnected", request_id="cancelled"),
            record(time=None, key_id="key-a", model="model-a",
                   request_id="undated"),
        ])
        windowed = metering.read_share_requests(
            str(self.ledger), window="24h", now=self.NOW)
        self.assertEqual({row["requestId"] for row in windowed["rows"]},
                         {"ok", "failed", "cancelled"})
        self.assertEqual(
            windowed["coverage"]["excludedUnknownTimeRequests"], 1)
        self.assertEqual(windowed["coverage"]["recordsScanned"], 4)
        self.assertEqual(windowed["coverage"]["recordsMatched"], 3)
        self.assertEqual(windowed["filters"]["window"], "24h")
        by_key = metering.read_share_requests(
            str(self.ledger), key_id="unattributed", now=self.NOW)
        self.assertEqual([row["requestId"] for row in by_key["rows"]],
                         ["cancelled"])
        by_model = metering.read_share_requests(
            str(self.ledger), model="model-b", now=self.NOW)
        self.assertEqual([row["requestId"] for row in by_model["rows"]],
                         ["failed"])
        by_result = metering.read_share_requests(
            str(self.ledger), result="failed", now=self.NOW)
        self.assertEqual([row["requestId"] for row in by_result["rows"]],
                         ["failed"])
        self.assertEqual(by_result["rows"][0]["result"], "failed")
        self.assertEqual(by_result["rows"][0]["status"], "upstream_read_error")

    def test_unknown_and_unreported_evidence_is_not_dropped(self):
        self.write([
            record(request_id="unreported", cache_report_status="unreported"),
            record(request_id="unknown-usage", usage_source="none",
                   status="ok"),
            record(request_id="legacy-cache"),
        ])
        page = metering.read_share_requests(str(self.ledger), now=self.NOW)
        rows = {row["requestId"]: row for row in page["rows"]}
        self.assertEqual(rows["unreported"]["cacheReportStatus"], "unreported")
        self.assertIsNone(rows["unreported"]["cacheTokens"])
        self.assertEqual(rows["legacy-cache"]["cacheReportStatus"], "unknown")
        self.assertIsNone(rows["legacy-cache"]["cacheTokens"])
        self.assertEqual(rows["unknown-usage"]["usageSource"], "none")
        self.assertIsNone(rows["unknown-usage"]["inputTokens"])
        coverage = page["coverage"]
        self.assertEqual(coverage["cacheUnreportedRequests"], 1)
        self.assertEqual(coverage["cacheUnknownRequests"], 2)
        self.assertEqual(coverage["unknownUsageRequests"], 1)
        self.assertEqual(coverage["attributedRequests"], 3)
        self.assertEqual(coverage["unattributedRequests"], 0)
        self.assertEqual(page["filters"]["window"], "all")

    def test_invalid_filters_are_rejected(self):
        self.write([record()])
        for kwargs in ({"window": "2d"}, {"key_id": "k" * 129},
                       {"model": "m" * 201}, {"result": "ok"},
                       {"cursor": "not-a-cursor"}, {"cursor": "-1"},
                       {"limit": 0}, {"limit": 201}, {"limit": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    metering.read_share_requests(str(self.ledger), **kwargs)

    def test_missing_ledger_is_an_empty_page_not_an_error(self):
        missing = Path(self._tmp.name) / "missing.jsonl"
        page = metering.read_share_requests(str(missing), now=self.NOW)
        self.assertEqual(page["rows"], [])
        self.assertIsNone(page["nextCursor"])
        self.assertIs(page["truncated"], False)
        self.assertIs(page["coverage"]["ledgerPresent"], False)
        self.assertIs(page["coverage"]["ledgerReadable"], False)

    def test_private_ledger_is_refused(self):
        private = Path(self._tmp.name) / "usage-chat2api.jsonl"
        private.write_text(json.dumps(record()) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            metering.read_share_requests(str(private))

    def test_request_page_never_leaks_secret_or_absolute_path(self):
        self.write([record()])
        page = metering.read_share_requests(str(self.ledger), now=self.NOW)
        blob = json.dumps(page, ensure_ascii=False)
        self.assertNotIn("secret", blob.lower())
        self.assertNotIn(str(self._tmp.name), blob)

    def test_request_page_has_no_amount_or_price_fields(self):
        self.write([record()])
        page = metering.read_share_requests(str(self.ledger), now=self.NOW)
        blob = json.dumps(page, ensure_ascii=False)
        for field in ("costUsd", "costCny", "costCnyPartial", "priced",
                      "priceVersion", "unpricedRequests"):
            with self.subTest(field=field):
                self.assertNotIn(field, blob)


@unittest.skip("HMAC share attribution retired with the merged share process")
class PrivateSummaryIsolationTests(unittest.TestCase):
    """同一私人账本夹具下，加入分享请求前后 build_summary() 逐字段不变。"""

    PRIVATE_ROWS = [
        {"time": "2026-09-25T10:00:00+08:00", "service": "chat2api",
         "model": "GLM-5.2-NVFP4", "stream": False, "status": "ok",
         "input_tokens": 1000, "output_tokens": 500,
         "cached_input_tokens": 200, "reasoning_tokens": 0,
         "total_tokens": 1500, "estimated": False, "duration_ms": 100},
    ]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.private_ledger = base / "usage-chat2api.jsonl"
        self.share_ledger = base / "usage-share-backend.jsonl"
        self.private_ledger.write_text(
            "\n".join(json.dumps(row) for row in self.PRIVATE_ROWS) + "\n",
            encoding="utf-8")
        patcher = mock.patch.object(
            usagesrv, "CHAT2API_LOG", str(self.private_ledger))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_share_requests_never_change_the_private_summary(self):
        before = usagesrv.build_summary()
        self.share_ledger.write_text(
            "\n".join(json.dumps(rec) for rec in (
                record(key_id="friend", protocol="chat", request_id="s1"),
                record(key_id="friend", protocol="messages", request_id="s2"),
            )) + "\n", encoding="utf-8")
        share_result = metering.read_share_usage(str(self.share_ledger))
        after = usagesrv.build_summary()
        self.assertGreater(share_result["requests"], 0)
        self.assertEqual(_stable(before), _stable(after))

    def test_private_summary_sources_never_include_the_share_ledger(self):
        summary = usagesrv.build_summary()
        self.assertEqual(sorted(summary["services"]), ["chat2api", "dsh"])
        self.assertNotIn("share", json.dumps(summary["sources"]))


def _stable(summary):
    stable = json.loads(json.dumps(summary, sort_keys=True))
    stable.pop("generatedAt", None)
    return stable


class KeyBucketSplitTests(MeteringFixture):
    def test_key_models_and_days_split_raw_tokens(self):
        self.write([
            record(input_tokens=1000, output_tokens=100,
                   cached_input_tokens=200, reasoning_tokens=50,
                   cache_report_status="reported", request_id="one"),
            record(input_tokens=2000, output_tokens=200,
                   cached_input_tokens=0, reasoning_tokens=0,
                   cache_report_status="reported", request_id="two"),
        ])
        key = metering.read_share_usage(str(self.ledger))["keys"][0]
        self.assertEqual(key["requests"], 2)
        self.assertEqual(key["inputTokens"], 3000)
        self.assertEqual(key["outputTokens"], 300)
        self.assertEqual(key["tokenUsage"]["completeRequests"], 2)
        self.assertEqual(key["tokenUsage"]["inputTokens"], 3000)
        self.assertEqual(key["tokenUsage"]["outputTokens"], 300)
        self.assertEqual(key["tokenUsage"]["cachedInputTokens"], 200)
        self.assertEqual(key["models"][0]["inputTokens"], 3000)
        self.assertEqual(key["days"][0]["inputTokens"], 3000)


class KeyUsageCoverageTests(MeteringFixture):
    def test_unknown_usage_keeps_its_day_and_model_without_fake_zero(self):
        self.write([
            record(usage_source="none", input_tokens=0, output_tokens=0,
                   total_tokens=0, request_id="unknown"),
            record(request_id="known"),
        ])
        key = metering.read_share_usage(str(self.ledger))["keys"][0]
        self.assertEqual(key["requests"], 2)
        self.assertEqual(key["unknownRequests"], 1)
        self.assertEqual(key["coverage"]["records"], 2)
        self.assertEqual(key["coverage"]["unknownUsageRequests"], 1)
        self.assertEqual(len(key["models"]), 1)
        self.assertEqual(len(key["days"]), 1)
        self.assertEqual(key["models"][0]["tokenUsage"]["totalTokens"], 110)

    def test_true_zero_and_invalid_token_components_stay_distinct(self):
        self.write([
            record(input_tokens=0, output_tokens=0, total_tokens=0,
                   request_id="zero"),
            record(input_tokens="not-a-number", output_tokens=None,
                   request_id="invalid"),
        ])
        key = metering.read_share_usage(str(self.ledger))["keys"][0]
        self.assertEqual(key["requests"], 2)
        self.assertEqual(key["tokenUsage"]["completeRequests"], 1)
        self.assertEqual(key["tokenUsage"]["unknownRequests"], 1)
        self.assertEqual(key["tokenUsage"]["totalTokens"], 0)
        self.assertIs(key["tokenUsage"]["complete"], False)

    def test_undated_usage_has_its_own_bucket_and_beijing_day_is_retained(self):
        self.write([
            record(time="not-a-timestamp", request_id="undated"),
            record(time="2026-09-25T23:30:00-04:00", request_id="day"),
        ])
        key = metering.read_share_usage(str(self.ledger))["keys"][0]
        self.assertEqual([day["day"] for day in key["days"]], ["2026-09-26"])
        self.assertIsNotNone(key["undated"])
        self.assertEqual(key["undated"]["requests"], 1)

    def test_missing_both_sides_does_not_become_complete_via_a_total_field(self):
        self.write([
            record(input_tokens=None, output_tokens=None,
                   total_tokens=None, request_id="total-only"),
        ])
        key = metering.read_share_usage(str(self.ledger))["keys"][0]
        self.assertEqual(key["requests"], 1)
        self.assertEqual(key["unknownRequests"], 1)
        self.assertIsNone(key["tokenUsage"]["totalTokens"])
        self.assertIsNone(key["tokenUsage"]["inputTokens"])
        self.assertIsNone(key["tokenUsage"]["outputTokens"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
