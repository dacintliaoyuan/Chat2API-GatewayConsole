# -*- coding: utf-8 -*-
"""分享 Key 可信归因与独立分享计量（C2/C3 线）的隔离回归。

覆盖三组契约：

1. ``ShareMeteringReadTests``：``share_metering.read_share_usage`` 只读、有界、
   脱敏，已知/估算/未知分列，旧无归属记录、轮换/删除 Key、跨重启、归因缺失
   与价格切换前后都可审阅。
2. ``CnyShareMeteringTests``：人民币计价在已知/估算/未知混合时的下界语义。
3. ``PrivateSummaryIsolationTests``：同一私人账本夹具下加入分享请求前后
   ``usagesrv.build_summary()`` 逐字段不变（不读生产账本）。

全部文件输入重定向到临时目录：不读生产账本、不重启任何服务、不发真实模型请求。
"""

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parents[1]

# 私人汇总隔离：导入 usagesrv 期间把所有账本/价目/会话路径重定向到夹具目录
# （与 tests/test_usage_boundary.py 同一先例；绝不读生产账本）。环境变量在
# 导入后立即还原，避免影响同一 pytest 进程里其他测试模块的模块级路径。
_SUMMARY_TMP = tempfile.TemporaryDirectory(prefix="share-metering-summary-")
_SUMMARY_DIR = Path(_SUMMARY_TMP.name)
_ATTRIBUTION_TMP = tempfile.TemporaryDirectory(prefix="share-metering-key-")
_REDIRECTED_ENV = {
    "QODER_USAGE_LOG": str(_SUMMARY_DIR / "usage.jsonl"),
    "CHAT2API_USAGE_LOG": str(_SUMMARY_DIR / "usage-chat2api.jsonl"),
    "M365_USAGE_LOG": str(_SUMMARY_DIR / "usage-m365.jsonl"),
    "DSH_LEDGER_SNAPSHOT_FILE": str(_SUMMARY_DIR / "dsh.json"),
    "USAGE_PRICE_FILE": str(_SUMMARY_DIR / "prices.json"),
    "USAGE_SESSION_CODE_FILE": str(_SUMMARY_DIR / "session-code"),
    "USAGE_CONTROL_TOKEN_FILE": str(_SUMMARY_DIR / "control-token"),
    # 归因共享密钥同样夹具化：8110 的默认密钥文件路径在导入时读取。
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
chat2api = _load("chat2api_metering", "chat2api/chat2api.py")
usagesrv = _load("usagesrv_metering", "usagesrv.py")

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import _chat2api_http_harness as harness  # noqa: E402


STUB_TOKEN = "stub-token"
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
        "price_version": "v1",
    }
    rec.update(fields)
    return rec


# 人民币夹具的版本边界：2026-09-25T00:00:00Z（v1 汇率 7.2，v2 汇率 8.0）。
RATE_BOUNDARY_MS = 1790294400000


def rate_price(model_id, at_ms, input_tok, output_tok, cache_tok, reasoning_tok):
    """六元组夹具：每 1k token 计 0.001 USD，汇率随记录时刻的版本。"""
    if at_ms is None:
        return 0.0, False, None, False, None, None
    version, rate = ("v1", 7.2) if at_ms < RATE_BOUNDARY_MS else ("v2", 8.0)
    cost = (input_tok + output_tok + reasoning_tok) / 1000.0
    return cost, True, model_id, False, version, rate


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

    @staticmethod
    def price(model_id, at_ms, input_tok, output_tok, cache_tok, reasoning_tok):
        """夹具计价器：每 1k input/输出 token 计 0.001 USD，版本随日切段。"""
        version = "v1" if (at_ms or 0) < 1790294400000 else "v2"
        cost = (input_tok + output_tok + reasoning_tok) / 1000.0
        return cost, True, model_id, False, version


class ShareMeteringReadTests(MeteringFixture):
    def test_two_protocols_one_key_are_one_key(self):
        # 双协议同 Key：chat 与 messages 各一行都归到同一 key_id。
        self.write([
            record(protocol="chat", request_id="r1"),
            record(protocol="messages", request_id="r2", stream=True),
        ])
        result = metering.read_share_usage(str(self.ledger), self.price)
        self.assertEqual(result["requests"], 2)
        self.assertEqual([k["key_id"] for k in result["keys"]], ["key-a"])
        key = result["keys"][0]
        self.assertEqual(key["requests"], 2)
        self.assertEqual(key["knownCostUsd"], 0.22)
        self.assertEqual(key["estimatedCostUsd"], 0.0)
        self.assertEqual(key["unknownRequests"], 0)
        self.assertEqual(result["unattributed"]["requests"], 0)

    def test_two_keys_never_cross_attribution(self):
        # 双 Key 并发：交错写入不得串号。
        self.write([
            record(key_id="key-a", request_id="a1"),
            record(key_id="key-b", request_id="b1"),
            record(key_id="key-a", request_id="a2"),
            record(key_id="key-b", request_id="b2", model="other-model"),
        ])
        result = metering.read_share_usage(str(self.ledger), self.price)
        by_key = {k["key_id"]: k for k in result["keys"]}
        self.assertEqual(sorted(by_key), ["key-a", "key-b"])
        self.assertEqual(by_key["key-a"]["requests"], 2)
        self.assertEqual(by_key["key-b"]["requests"], 2)
        self.assertEqual([m["model"] for m in by_key["key-a"]["models"]],
                         ["GLM-5.2-NVFP4"])
        # key-b 的两行模型不同：明细按模型分列，不合并成一条。
        self.assertEqual([m["model"] for m in by_key["key-b"]["models"]],
                         ["GLM-5.2-NVFP4", "other-model"])

    def test_rotated_and_deleted_key_history_stays_reviewable(self):
        # 轮换（secret_version 变化）与删除后，历史仍按同一 key_id 可审阅：
        # 本模块不查管理库，账本里出现过的 key_id 一律返回。
        self.write([
            record(key_id="key-a", request_id="before-rotate",
                   time="2026-09-24T10:00:00+08:00"),
            record(key_id="key-a", request_id="after-rotate"),
            record(key_id="key-gone", request_id="deleted-key",
                   protocol="messages"),
        ])
        result = metering.read_share_usage(str(self.ledger), self.price)
        by_key = {k["key_id"]: k for k in result["keys"]}
        self.assertEqual(sorted(by_key), ["key-a", "key-gone"])
        self.assertEqual(by_key["key-a"]["requests"], 2)
        self.assertEqual(by_key["key-gone"]["requests"], 1)
        # 价格切换前后：同一 Key 的历史跨两个价格版本，版本标识都保留。
        self.assertEqual(result["coverage"]["priceVersions"], ["v1", "v2"])

    def test_missing_usage_is_unknown_not_zero(self):
        # 无 usage：用量未知（usage_source="none"）与真实 0 必须区分。
        self.write([
            record(usage_source="none", input_tokens=0, output_tokens=0,
                   total_tokens=0, request_id="unknown-usage"),
            record(input_tokens=0, output_tokens=0, total_tokens=0,
                   request_id="real-zero"),
        ])
        result = metering.read_share_usage(str(self.ledger), self.price)
        key = result["keys"][0]
        self.assertEqual(key["requests"], 2)
        self.assertEqual(key["unknownRequests"], 1)
        self.assertEqual(key["knownCostUsd"], 0.0)
        # 未知用量的记录不进模型明细，真实 0 的记录进明细且金额为 0。
        self.assertEqual([m["requests"] for m in key["models"]], [1])
        self.assertEqual(result["coverage"]["unknownUsageRequests"], 1)

    def test_cache_report_status_distinguishes_unreported_from_zero(self):
        # Q11：上游明确报告 cached=0 与上游没有报告缓存量必须可区分；
        # 未报告不能折成实测 0，也不能按 0 缓存计价（金额是 cache-miss 上界）。
        calls = []

        def price(model_id, at_ms, input_tok, output_tok, cache_tok,
                  reasoning_tok):
            calls.append(cache_tok)
            cost = (input_tok + output_tok) / 1000.0
            return cost, True, model_id, False, "v1", 7.2, cache_tok is None

        self.write([
            record(cache_report_status="unreported",
                   cached_input_tokens=0, request_id="unreported"),
            record(cache_report_status="reported", cached_input_tokens=0,
                   request_id="explicit-zero"),
            record(cache_report_status="reported", cached_input_tokens=20,
                   request_id="reported-hit"),
        ])
        result = metering.read_share_usage(str(self.ledger), price)
        key = result["keys"][0]
        usage = key["tokenUsage"]
        self.assertEqual(calls[0], None)       # 未报告 ≠ 0
        self.assertEqual(calls[1], 0)          # 明确 0 是可信值
        self.assertEqual(calls[2], 20)
        self.assertEqual(usage["cacheReportedRequests"], 2)
        self.assertEqual(usage["cacheUnreportedRequests"], 1)
        self.assertEqual(usage["cacheUnknownRequests"], 0)
        self.assertEqual(usage["cachedInputTokens"], 20)
        self.assertEqual(key["coverage"]["cacheReportedRequests"], 2)
        self.assertEqual(key["coverage"]["cacheUnreportedRequests"], 1)
        self.assertEqual(key["coverage"]["cacheUnknownPricedRequests"], 1)
        self.assertEqual(result["coverage"]["cacheReportedRequests"], 2)
        self.assertEqual(result["coverage"]["cacheUnreportedRequests"], 1)
        self.assertIs(key["costCnyPartial"], True)

    def test_legacy_cache_rows_stay_unknown_not_reported(self):
        # 旧记录没有 cache_report_status：按 unknown 分列，不推测历史缓存量；
        # 计价仍用旧字段，历史金额不变。
        legacy = record()
        legacy.pop("cache_report_status", None)
        legacy.update({"cached_input_tokens": 20,
                       "request_id": "legacy-cache"})
        self.write([legacy])
        result = metering.read_share_usage(str(self.ledger), self.price)
        usage = result["keys"][0]["tokenUsage"]
        self.assertEqual(usage["cacheReportedRequests"], 0)
        self.assertEqual(usage["cacheUnreportedRequests"], 0)
        self.assertEqual(usage["cacheUnknownRequests"], 1)
        self.assertIsNone(usage["cachedInputTokens"])

    def test_legacy_unattributed_records_are_reviewable(self):
        # 旧无归属记录（改动前写的行，没有 key_id/usage_source/attribution）
        # 仍可审阅：按 estimated 分列，且明确标为 unattributed。
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
        result = metering.read_share_usage(str(self.ledger), self.price)
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["keys"], [])
        self.assertEqual(result["unattributed"]["requests"], 1)
        self.assertEqual(result["unattributed"]["key_id"], None)
        self.assertEqual(result["unattributed"]["estimatedCostUsd"], 1.0)
        self.assertEqual(result["unattributed"]["knownCostUsd"], 0.0)
        self.assertEqual(result["coverage"]["attributionReasons"], {"legacy": 1})

    def test_interrupted_failed_and_retried_are_lower_bounds(self):
        # 中断/失败/重试：status 不是 "ok" 只计已知下界，不伪造成完整成功。
        self.write([
            record(status="client_disconnected", request_id="cut",
                   usage_source="partial", output_tokens=0),
            record(status="upstream_read_error", request_id="failed",
                   usage_source="estimated"),
            record(status="upstream_transient_retried", request_id="retried",
                   usage_source="estimated"),
            record(status="ok", request_id="complete"),
        ])
        result = metering.read_share_usage(str(self.ledger), self.price)
        key = result["keys"][0]
        self.assertEqual(key["requests"], 4)
        self.assertEqual(key["knownCostUsd"], 0.11)
        # 中断流只计已知下界：三条非 "ok" 的行按记录里已有的已知量计，
        # 不外推、不补零。
        self.assertEqual(key["estimatedCostUsd"], 0.32)
        self.assertEqual(result["coverage"]["incompleteRequests"], 3)
        self.assertEqual(key["unknownRequests"], 0)

    def test_price_switch_keeps_history_and_names_versions(self):
        # 价格切换前后：费用按记录自己的时刻取价（版本切换不改写历史），
        # 同一记录在同一价目函数下金额可复现，版本标识进 coverage。
        before = record(time="2026-09-25T10:00:00+08:00", request_id="before",
                        input_tokens=1000, output_tokens=0)
        after = record(time="2026-09-25T10:00:00+08:00", request_id="after",
                       input_tokens=1000, output_tokens=0)
        self.write([before, after])
        first = metering.read_share_usage(str(self.ledger), self.price)
        second = metering.read_share_usage(str(self.ledger), self.price)
        self.assertEqual(first["keys"], second["keys"])

        def switched(model_id, at_ms, i, o, c, r):
            return (i / 100.0, True, model_id, False, "v-switched")

        switched_result = metering.read_share_usage(str(self.ledger), switched)
        self.assertEqual(switched_result["keys"][0]["knownCostUsd"], 20.0)
        self.assertEqual(switched_result["coverage"]["priceVersions"],
                         ["v-switched"])

    def test_cross_restart_history_is_continuous(self):
        # 跨重启：追加新行后重读，旧记录的值不变、新记录被追加（append-only）。
        self.write([record(request_id="before-restart")])
        first = metering.read_share_usage(str(self.ledger), self.price)
        with open(self.ledger, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record(request_id="after-restart",
                                         key_id="key-b")) + "\n")
        second = metering.read_share_usage(str(self.ledger), self.price)
        self.assertEqual(first["keys"][0]["requests"], 1)
        self.assertEqual(second["keys"][0], first["keys"][0])
        by_key = {k["key_id"]: k for k in second["keys"]}
        self.assertEqual(sorted(by_key), ["key-a", "key-b"])
        self.assertEqual(by_key["key-b"]["requests"], 1)

    def test_missing_attribution_is_never_guessed(self):
        # 归因缺失：没有 key_id 的记录一律进未归属桶，绝不均摊给某个 Key。
        self.write([
            record(key_id=None, attribution="absent", request_id="direct"),
            record(key_id=None, attribution="bad_signature", request_id="forged"),
            record(key_id="", attribution="replayed", request_id="replay"),
            record(key_id="key-a", request_id="attributed"),
        ])
        result = metering.read_share_usage(str(self.ledger), self.price)
        self.assertEqual([k["key_id"] for k in result["keys"]], ["key-a"])
        self.assertEqual(result["keys"][0]["requests"], 1)
        self.assertEqual(result["unattributed"]["requests"], 3)
        self.assertEqual(result["unattributed"]["knownCostUsd"], 0.33)
        self.assertEqual(result["coverage"]["attributed"], 1)
        self.assertEqual(result["coverage"]["unattributed"], 3)
        self.assertEqual(result["coverage"]["attributionReasons"],
                         {"absent": 1, "bad_signature": 1, "replayed": 1})

    def test_price_fn_none_returns_counts_only(self):
        self.write([record()])
        result = metering.read_share_usage(str(self.ledger))
        key = result["keys"][0]
        self.assertIsNone(key["knownCostUsd"])
        self.assertIsNone(key["estimatedCostUsd"])
        self.assertIsNone(key["models"][0]["costUsd"])
        self.assertIsNone(key["days"][0]["costUsd"])
        self.assertEqual(key["requests"], 1)
        self.assertEqual(key["unpricedRequests"], 0)
        self.assertFalse(result["coverage"]["priceFn"])

    def test_unpriced_model_is_reported_not_zero(self):
        def unpriced(model_id, at_ms, i, o, c, r):
            return 0.0, False, None, False, None

        self.write([record()])
        result = metering.read_share_usage(str(self.ledger), unpriced)
        key = result["keys"][0]
        self.assertEqual(key["requests"], 1)
        self.assertEqual(key["unpricedRequests"], 1)
        self.assertEqual(key["knownCostUsd"], 0.0)
        self.assertEqual(result["coverage"]["unpricedModels"], ["GLM-5.2-NVFP4"])
        self.assertEqual(result["coverage"]["priceVersions"], [])

    def test_unknown_time_is_reported_without_a_day(self):
        self.write([record(time="not-a-timestamp")])
        result = metering.read_share_usage(str(self.ledger), self.price)
        key = result["keys"][0]
        self.assertEqual(key["requests"], 1)
        self.assertEqual(key["days"], [])
        self.assertEqual(result["coverage"]["unknownTimeRequests"], 1)

    def test_limit_is_bounded_and_reported(self):
        self.write([record(request_id=f"r{i}") for i in range(5)])
        result = metering.read_share_usage(str(self.ledger), self.price, limit=2)
        self.assertEqual(result["requests"], 2)
        self.assertEqual(result["coverage"]["recordsTotal"], 5)
        self.assertTrue(result["coverage"]["truncated"])
        self.assertEqual(result["coverage"]["limit"], 2)
        # 有界读取只丢最旧：留下的是最后两条。
        self.assertEqual(result["keys"][0]["requests"], 2)

    def test_missing_ledger_is_an_empty_result(self):
        missing = Path(self._tmp.name) / "absent.jsonl"
        result = metering.read_share_usage(str(missing), self.price)
        self.assertEqual(result["requests"], 0)
        self.assertEqual(result["keys"], [])
        self.assertEqual(result["unattributed"]["requests"], 0)
        self.assertFalse(result["coverage"]["ledgerPresent"])
        self.assertFalse(missing.exists())

    def test_broken_lines_are_counted_not_silently_dropped(self):
        self.write([record()])
        with open(self.ledger, "a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        result = metering.read_share_usage(str(self.ledger), self.price)
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["coverage"]["unparsedLines"], 1)

    def test_private_ledger_is_refused(self):
        private = Path(self._tmp.name) / "usage-chat2api.jsonl"
        private.write_text(json.dumps(record()) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            metering.read_share_usage(str(private), self.price)

    def test_read_is_read_only(self):
        self.write([record()])
        before = self.ledger.read_bytes()
        stat_before = self.ledger.stat().st_mtime_ns
        metering.read_share_usage(str(self.ledger), self.price)
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(self.ledger.stat().st_mtime_ns, stat_before)

    def test_no_secret_material_in_the_result(self):
        self.write([record()])
        blob = json.dumps(metering.read_share_usage(str(self.ledger),
                                                   self.price))
        self.assertNotIn("secret", blob.lower())
        self.assertNotIn(str(self._tmp.name), blob)


class ShareMeteringRequestPageTests(MeteringFixture):
    """r14 逐请求页：同一账本、同一筛选、同一计价链，新→旧游标分页。"""

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
            str(self.ledger), self.price, window="24h", key_id="key-a",
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
            str(self.ledger), self.price, limit=2, now=self.NOW)
        self.assertEqual([row["requestId"] for row in first["rows"]],
                         ["r3", "r2"])
        self.assertEqual(first["rows"][0]["durationMs"], 1000)
        self.assertEqual(first["nextCursor"], "2")
        self.assertIs(first["truncated"], True)
        self.assertEqual(first["coverage"]["recordsMatched"], 3)
        self.assertEqual(first["coverage"]["recordsReturned"], 2)
        second = metering.read_share_requests(
            str(self.ledger), self.price, limit=2, cursor="2", now=self.NOW)
        self.assertEqual([row["requestId"] for row in second["rows"]], ["r1"])
        self.assertIsNone(second["nextCursor"])
        self.assertIs(second["truncated"], False)

    def test_today_window_is_the_beijing_calendar_day(self):
        # NOW is 2026-09-25 21:00 Beijing. A rolling 24h window would still
        # include yesterday 23:30; "today" must start at 2026-09-25 00:00 Beijing.
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
            str(self.ledger), self.price, window="today", now=self.NOW)
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
            str(self.ledger), self.price, window="24h", now=self.NOW)
        self.assertEqual({row["requestId"] for row in windowed["rows"]},
                         {"ok", "failed", "cancelled"})
        self.assertEqual(
            windowed["coverage"]["excludedUnknownTimeRequests"], 1)
        self.assertEqual(windowed["coverage"]["recordsScanned"], 4)
        self.assertEqual(windowed["coverage"]["recordsMatched"], 3)
        self.assertEqual(windowed["filters"]["window"], "24h")
        by_key = metering.read_share_requests(
            str(self.ledger), self.price, key_id="unattributed", now=self.NOW)
        self.assertEqual([row["requestId"] for row in by_key["rows"]],
                         ["cancelled"])
        by_model = metering.read_share_requests(
            str(self.ledger), self.price, model="model-b", now=self.NOW)
        self.assertEqual([row["requestId"] for row in by_model["rows"]],
                         ["failed"])
        by_result = metering.read_share_requests(
            str(self.ledger), self.price, result="failed", now=self.NOW)
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
        page = metering.read_share_requests(
            str(self.ledger), self.price, now=self.NOW)
        rows = {row["requestId"]: row for row in page["rows"]}
        self.assertEqual(rows["unreported"]["cacheReportStatus"], "unreported")
        self.assertIsNone(rows["unreported"]["cacheTokens"])
        self.assertEqual(rows["legacy-cache"]["cacheReportStatus"], "unknown")
        self.assertIsNone(rows["legacy-cache"]["cacheTokens"])
        self.assertEqual(rows["unknown-usage"]["usageSource"], "none")
        self.assertIsNone(rows["unknown-usage"]["inputTokens"])
        self.assertIsNone(rows["unknown-usage"]["costCny"])
        self.assertIsNone(rows["unknown-usage"]["priced"])
        coverage = page["coverage"]
        self.assertEqual(coverage["cacheUnreportedRequests"], 1)
        self.assertEqual(coverage["cacheUnknownRequests"], 2)
        self.assertEqual(coverage["unknownUsageRequests"], 1)
        self.assertEqual(coverage["attributedRequests"], 3)
        self.assertEqual(coverage["unattributedRequests"], 0)
        self.assertEqual(page["filters"]["window"], "all")

    def test_unpriced_model_keeps_null_cost_not_zero(self):
        self.write([record(model="unpriced-model", request_id="unpriced")])
        def unpriced(model_id, at_ms, input_tok, output_tok, cache_tok,
                     reasoning_tok):
            return 0.0, False, model_id, False, "v1", 7.2
        page = metering.read_share_requests(
            str(self.ledger), unpriced, now=self.NOW)
        row = page["rows"][0]
        self.assertIs(row["priced"], False)
        self.assertIsNone(row["costUsd"])
        self.assertIsNone(row["costCny"])
        self.assertIs(row["costCnyPartial"], True)
        self.assertEqual(page["coverage"]["unpricedRequests"], 1)

    def test_invalid_filters_are_rejected(self):
        self.write([record()])
        for kwargs in ({"window": "2d"}, {"key_id": "k" * 129},
                       {"model": "m" * 201}, {"result": "ok"},
                       {"cursor": "not-a-cursor"}, {"cursor": "-1"},
                       {"limit": 0}, {"limit": 201}, {"limit": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    metering.read_share_requests(
                        str(self.ledger), self.price, **kwargs)

    def test_missing_ledger_is_an_empty_page_not_an_error(self):
        missing = Path(self._tmp.name) / "missing.jsonl"
        page = metering.read_share_requests(
            str(missing), self.price, now=self.NOW)
        self.assertEqual(page["rows"], [])
        self.assertIsNone(page["nextCursor"])
        self.assertIs(page["truncated"], False)
        self.assertIs(page["coverage"]["ledgerPresent"], False)
        self.assertIs(page["coverage"]["ledgerReadable"], False)

    def test_private_ledger_is_refused(self):
        private = Path(self._tmp.name) / "usage-chat2api.jsonl"
        private.write_text(json.dumps(record()) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            metering.read_share_requests(str(private), self.price)

    def test_request_page_never_leaks_secret_or_absolute_path(self):
        self.write([record()])
        page = metering.read_share_requests(
            str(self.ledger), self.price, now=self.NOW)
        blob = json.dumps(page, ensure_ascii=False)
        self.assertNotIn("secret", blob.lower())
        self.assertNotIn(str(self._tmp.name), blob)


class CnyShareMeteringTests(MeteringFixture):
    """R3 人民币只读聚合链：逐条按记录自己的汇率折算，未知不折 0。"""

    def test_two_rate_segments_are_converted_per_record(self):
        # v1（7.2）与 v2（8.0）各一条：合计 = 1.0×7.2 + 1.0×8.0，
        # 不是 (1.0+1.0)×任一现价。
        self.write([
            record(time="2026-09-24T10:00:00+08:00", input_tokens=1000,
                   output_tokens=0, request_id="v1"),
            record(time="2026-09-25T10:00:00+08:00", input_tokens=1000,
                   output_tokens=0, request_id="v2"),
        ])
        result = metering.read_share_usage(str(self.ledger), rate_price)
        key = result["keys"][0]
        self.assertEqual(key["costUsd"], 2.0)
        self.assertEqual(key["costCny"], round(1.0 * 7.2 + 1.0 * 8.0, 4))
        self.assertIs(key["costCnyPartial"], False)
        self.assertEqual(result["coverage"]["priceVersions"], ["v1", "v2"])
        self.assertIs(result["coverage"]["rateFn"], True)
        self.assertIs(result["coverage"]["costCnyPartial"], False)

    def test_known_and_estimated_are_converted_separately(self):
        self.write([
            record(time="2026-09-25T10:00:00+08:00", input_tokens=1000,
                   output_tokens=0, usage_source="upstream", request_id="known"),
            record(time="2026-09-25T11:00:00+08:00", input_tokens=1000,
                   output_tokens=0, usage_source="estimated",
                   estimated=True, request_id="estimated"),
        ])
        key = metering.read_share_usage(str(self.ledger),
                                       rate_price)["keys"][0]
        self.assertEqual(key["knownCostCny"], 8.0)
        self.assertEqual(key["estimatedCostCny"], 8.0)
        self.assertEqual(key["costCny"], 16.0)
        self.assertIs(key["costCnyPartial"], False)
        model = key["models"][0]
        self.assertEqual(model["knownCostCny"], 8.0)
        self.assertEqual(model["estimatedCostCny"], 8.0)

    def test_true_zero_is_complete_not_unknown(self):
        self.write([record(input_tokens=0, output_tokens=0, total_tokens=0)])
        key = metering.read_share_usage(str(self.ledger),
                                       rate_price)["keys"][0]
        self.assertEqual(key["costUsd"], 0.0)
        self.assertEqual(key["costCny"], 0.0)
        self.assertEqual(key["knownCostCny"], 0.0)
        self.assertEqual(key["estimatedCostCny"], 0.0)
        self.assertIs(key["costCnyPartial"], False)

    def test_missing_time_is_unknown_not_converted_at_the_current_rate(self):
        self.write([record(time="not-a-timestamp")])
        key = metering.read_share_usage(str(self.ledger),
                                       rate_price)["keys"][0]
        self.assertEqual(key["unpricedRequests"], 1)
        self.assertIsNone(key["costCny"])
        self.assertIsNone(key["knownCostCny"])
        # 该记录是 upstream：估算侧没有任何记录，是真实 0.0（与 USD 侧同口径）。
        self.assertEqual(key["estimatedCostCny"], 0.0)
        self.assertIs(key["costCnyPartial"], True)
        self.assertIsNone(key["undated"]["costCny"])
        self.assertIs(key["undated"]["costCnyPartial"], True)

    def test_priced_record_without_a_rate_keeps_usd_but_cny_unknown(self):
        def no_rate(model_id, at_ms, i, o, c, r):
            return (i + o) / 1000.0, True, model_id, False, "v-no-rate", None

        self.write([record(input_tokens=1000, output_tokens=0)])
        result = metering.read_share_usage(str(self.ledger), no_rate)
        key = result["keys"][0]
        self.assertEqual(key["knownCostUsd"], 1.0)
        self.assertIsNone(key["knownCostCny"])
        self.assertIsNone(key["costCny"])
        self.assertIs(key["costCnyPartial"], True)
        # 六元组确实被观察到（只是汇率未知），与五元组区分。
        self.assertIs(result["coverage"]["rateFn"], True)

    def test_unpriced_record_is_not_folded_to_zero(self):
        def unpriced(model_id, at_ms, i, o, c, r):
            return 0.0, False, None, False, None, 7.2

        self.write([record()])
        key = metering.read_share_usage(str(self.ledger),
                                       unpriced)["keys"][0]
        self.assertEqual(key["unpricedRequests"], 1)
        self.assertEqual(key["costUsd"], 0.0)
        self.assertIsNone(key["costCny"])
        self.assertIs(key["costCnyPartial"], True)
        self.assertIs(key["knownCostCnyPartial"], True)
        self.assertIs(key["estimatedCostCnyPartial"], False)

    def test_mixed_convertible_and_unknown_is_a_lower_bound(self):
        def mixed(model_id, at_ms, i, o, c, r):
            if model_id == "unpriced-model":
                return 0.0, False, None, False, None, 8.0
            return (i + o) / 1000.0, True, model_id, False, "v2", 8.0

        self.write([
            record(input_tokens=1000, output_tokens=0, request_id="known"),
            record(model="unpriced-model", input_tokens=10, output_tokens=0,
                   request_id="unpriced"),
        ])
        key = metering.read_share_usage(str(self.ledger), mixed)["keys"][0]
        self.assertEqual(key["knownCostCny"], 8.0)
        self.assertEqual(key["costCny"], 8.0)
        self.assertIs(key["costCnyPartial"], True)
        # 估算侧没有任何记录：真实 0，不是未知。
        self.assertEqual(key["estimatedCostCny"], 0.0)
        self.assertIs(key["knownCostCnyPartial"], True)
        self.assertIs(key["estimatedCostCnyPartial"], False)

    def test_unknown_usage_is_partial_at_every_node(self):
        self.write([record(usage_source="none", input_tokens=0,
                           output_tokens=0, total_tokens=0)])
        key = metering.read_share_usage(str(self.ledger),
                                       rate_price)["keys"][0]
        for entry in (key, key["models"][0], key["days"][0]):
            self.assertIsNone(entry["costCny"])
            self.assertIs(entry["costCnyPartial"], True)
            # 用量未知没有已知/估算归属：两个小计都是真实 0，不是未知。
            self.assertEqual(entry["knownCostCny"], 0.0)
            self.assertEqual(entry["estimatedCostCny"], 0.0)
            self.assertIs(entry["knownCostCnyPartial"], False)
            self.assertIs(entry["estimatedCostCnyPartial"], False)

    def test_unattributed_and_deleted_key_buckets_carry_cny(self):
        self.write([
            record(key_id=None, attribution="absent", request_id="direct",
                   input_tokens=1000, output_tokens=0),
            record(key_id="key-gone", request_id="deleted",
                   input_tokens=1000, output_tokens=0),
        ])
        result = metering.read_share_usage(str(self.ledger), rate_price)
        self.assertEqual([k["key_id"] for k in result["keys"]], ["key-gone"])
        self.assertEqual(result["keys"][0]["costCny"], 8.0)
        self.assertEqual(result["unattributed"]["costCny"], 8.0)

    def test_five_tuple_pricer_keeps_usd_but_leaves_cny_unknown(self):
        # 旧五元组响应兼容：USD 口径逐字段不变；没有汇率 → 人民币未知。
        self.write([record(input_tokens=1000, output_tokens=0)])
        result = metering.read_share_usage(str(self.ledger), self.price)
        key = result["keys"][0]
        self.assertEqual(key["knownCostUsd"], 1.0)
        self.assertEqual(key["estimatedCostUsd"], 0.0)
        for entry in (key, key["models"][0], key["days"][0]):
            self.assertIsNone(entry["costCny"])
            self.assertIsNone(entry["knownCostCny"])
            self.assertIs(entry["costCnyPartial"], True)
        # 唯一记录是 upstream：估算侧没有记录，是真实 0.0。
        for entry in (key, key["models"][0], key["days"][0]):
            self.assertEqual(entry["estimatedCostCny"], 0.0)
        self.assertIs(result["coverage"]["priceFn"], True)
        self.assertIs(result["coverage"]["rateFn"], False)
        self.assertIs(result["coverage"]["costCnyPartial"], True)

    def test_absent_pricer_has_no_cny_fields_at_all(self):
        self.write([record()])
        result = metering.read_share_usage(str(self.ledger))
        key = result["keys"][0]
        for entry in (key, key["models"][0], key["days"][0]):
            self.assertNotIn("costCny", entry)
            self.assertNotIn("knownCostCny", entry)
            self.assertNotIn("estimatedCostCny", entry)
            self.assertNotIn("costCnyPartial", entry)
        self.assertNotIn("costCny", result["unattributed"])
        self.assertNotIn("costCnyPartial", result["coverage"])
        self.assertNotIn("rateFn", result["coverage"])
        self.assertIs(result["coverage"]["priceFn"], False)

    def test_one_beijing_day_spanning_two_versions_converts_each_record(self):
        # 同一北京时间日 2026-09-25：07:00 属 v1（7.2），09:00 属 v2（8.0）。
        self.write([
            record(time="2026-09-25T07:00:00+08:00", input_tokens=1000,
                   output_tokens=0, request_id="v1-same-day"),
            record(time="2026-09-25T09:00:00+08:00", input_tokens=1000,
                   output_tokens=0, request_id="v2-same-day"),
        ])
        key = metering.read_share_usage(str(self.ledger),
                                       rate_price)["keys"][0]
        self.assertEqual([d["day"] for d in key["days"]], ["2026-09-25"])
        day = key["days"][0]
        self.assertEqual(day["costCny"], round(1.0 * 7.2 + 1.0 * 8.0, 4))
        self.assertEqual(day["costCny"], key["costCny"])
        self.assertIs(day["costCnyPartial"], False)

    def test_empty_ledger_is_zero_cny_not_unknown(self):
        missing = Path(self._tmp.name) / "absent.jsonl"
        result = metering.read_share_usage(str(missing), rate_price)
        self.assertEqual(result["keys"], [])
        self.assertEqual(result["unattributed"]["costCny"], 0.0)
        self.assertEqual(result["unattributed"]["knownCostCny"], 0.0)
        self.assertEqual(result["unattributed"]["estimatedCostCny"], 0.0)
        self.assertIs(result["unattributed"]["costCnyPartial"], False)
        self.assertIs(result["coverage"]["costCnyPartial"], False)
        # 空账本没有任何计价调用：没有可观察的六元组证据。
        self.assertIs(result["coverage"]["rateFn"], False)


@unittest.skip("HMAC share attribution retired with the merged share process")
class PrivateSummaryIsolationTests(unittest.TestCase):
    """同一私人账本夹具下，加入分享请求前后 build_summary() 逐字段不变。"""

    PRIVATE_ROWS = [
        {"time": "2026-09-25T10:00:00+08:00", "service": "chat2api",
         "model": "GLM-5.2-NVFP4", "stream": False, "status": "ok",
         "input_tokens": 1000, "output_tokens": 500,
         "cached_input_tokens": 200, "reasoning_tokens": 0,
         "total_tokens": 1500, "estimated": False, "duration_ms": 100},
        {"time": "2026-09-24T10:00:00+08:00", "service": "chat2api",
         "model": "GLM-5.2-NVFP4", "stream": True, "status": "ok",
         "input_tokens": 2000, "output_tokens": 100,
         "cached_input_tokens": 0, "reasoning_tokens": 50,
         "total_tokens": 2100, "estimated": True, "duration_ms": 200},
    ]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.private_ledger = base / "usage-chat2api.jsonl"
        self.share_ledger = base / "usage-share-backend.jsonl"
        self.price_file = base / "prices.json"
        self.private_ledger.write_text(
            "\n".join(json.dumps(row) for row in self.PRIVATE_ROWS) + "\n",
            encoding="utf-8")
        self.price_file.write_text(json.dumps({
            "checkedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "exchangeRate": 7.2,
            "peak": {"enabled": False, "effectiveAt": "2026-08-01T00:00:00Z",
                     "windows": []},
            "models": {"GLM-5.2-NVFP4": {"cacheHit": 0.007,
                                          "cacheMiss": 0.22,
                                          "output": 0.66}},
            "providers": {}}), encoding="utf-8")
        for attr, value in (("CHAT2API_LOG", str(self.private_ledger)),
                            ("PRICE_FILE", str(self.price_file))):
            patcher = mock.patch.object(usagesrv, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        usagesrv._price_cache["at"] = 0.0
        usagesrv._price_cache["data"] = None

    def test_share_requests_never_change_the_private_summary(self):
        before = usagesrv.build_summary()
        self.share_ledger.write_text(
            "\n".join(json.dumps(rec) for rec in (
                record(key_id="friend", protocol="chat", request_id="s1"),
                record(key_id="friend", protocol="messages", request_id="s2"),
                record(key_id="other", protocol="chat", request_id="s3",
                       model="other-model"),
            )) + "\n", encoding="utf-8")
        share_result = metering.read_share_usage(str(self.share_ledger),
                                               lambda *args: (0.0, True,
                                                              None, False, None))
        after = usagesrv.build_summary()
        self.assertGreater(share_result["requests"], 0)
        self.assertEqual(_stable(before), _stable(after))

    def test_private_summary_sources_never_include_the_share_ledger(self):
        summary = usagesrv.build_summary()
        # B0: the default summary is UM-only; the retired qoder/m365 ledgers
        # stay on disk but never enter the UM home summary.
        self.assertEqual(sorted(summary["services"]), ["chat2api", "dsh"])
        sources = json.dumps(summary["sources"])
        self.assertNotIn("share", sources)


def _stable(summary):
    """逐字段快照：去掉每次调用都变的 generatedAt，其余字段原样比较。"""
    return json.dumps({key: value for key, value in summary.items()
                       if key != "generatedAt"}, ensure_ascii=False,
                      sort_keys=True)


class KeyBucketSplitTests(MeteringFixture):
    """Rev8 B8-6：每 Key 明细层的已知/估算分列与可信 Token 合计。

    upstream 报告=已知，本地估算=估算；可信 Token 只累计可信侧（缺席侧
    不计入——未知不当 0）；轮换同 key_id 自动累计；读面零写入。
    """

    def test_key_models_and_days_split_known_estimated_with_tokens(self):
        rows = [
            {"time": "2026-10-02T12:00:00+08:00", "service": "chat2api",
             "model": "m-a", "status": "ok", "input_tokens": 100,
             "output_tokens": 900, "usage_source": "upstream",
             "key_id": "k1", "request_id": "r1",
             "attribution": "verified", "price_version": "v-test"},
            {"time": "2026-10-02T12:01:00+08:00", "service": "chat2api",
             "model": "m-a", "status": "ok", "input_tokens": 50,
             "output_tokens": 150, "usage_source": "estimated",
             "key_id": "k1", "request_id": "r2",
             "attribution": "verified", "price_version": "v-test"},
        ]
        with self._tmp as directory:
            ledger = Path(directory) / "usage-share-backend.jsonl"
            with open(ledger, "w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            payload = metering.read_share_usage(
                str(ledger), self.price, limit=50)
        key = payload["keys"][0]
        # 夹具计价器：cost = (input+output)/1000；已知行=1000/1000=1.0 USD，
        # 估算行=200/1000=0.2 USD。Key 层：可信 Token 合计 = 两行可信侧之
        # 和；已知/估算按来源分列。
        self.assertEqual(key["inputTokens"], 150)
        self.assertEqual(key["outputTokens"], 1050)
        model_rows = {entry["model"]: entry for entry in key["models"]}
        self.assertEqual(model_rows["m-a"]["knownCostUsd"], 1.0)
        self.assertEqual(model_rows["m-a"]["estimatedCostUsd"], 0.2)
        self.assertEqual(model_rows["m-a"]["inputTokens"], 150)
        self.assertEqual(model_rows["m-a"]["outputTokens"], 1050)
        self.assertEqual(len(key["days"]), 1)
        day = key["days"][0]
        self.assertEqual(day["knownCostUsd"], 1.0)
        self.assertEqual(day["estimatedCostUsd"], 0.2)
        self.assertEqual(day["inputTokens"], 150)
        self.assertEqual(day["outputTokens"], 1050)
        # 读面零写入：账本字节不变。
        self.assertFalse(self.ledger.exists())


class KeyUsageCoverageTests(MeteringFixture):
    """Unknown and lower-bound usage remain visible at every grouping level."""

    def test_unknown_usage_keeps_its_day_and_model_without_fake_zero(self):
        self.write([record(usage_source="none", input_tokens=0,
                           output_tokens=0, total_tokens=0)])
        key = metering.read_share_usage(str(self.ledger), self.price)["keys"][0]
        for entry in (key, key["models"][0], key["days"][0]):
            self.assertEqual(entry["unknownRequests"], 1)
            self.assertEqual(entry["coverage"]["records"], 1)
            self.assertFalse(entry["coverage"]["costComplete"])
            self.assertIsNone(entry["tokenUsage"]["totalTokens"])
            self.assertEqual(entry["tokenUsage"]["unknownRequests"], 1)

    def test_unpriced_and_absent_pricer_are_disclosed_locally(self):
        self.write([record()])
        unpriced = lambda *args: (0.0, False, None, False, None)
        key = metering.read_share_usage(str(self.ledger), unpriced)["keys"][0]
        for entry in (key, key["models"][0], key["days"][0]):
            self.assertEqual(entry["unpricedRequests"], 1)
            self.assertFalse(entry["coverage"]["costComplete"])
            self.assertEqual(entry["knownCostUsd"], 0.0)
        key = metering.read_share_usage(str(self.ledger))["keys"][0]
        for entry in (key, key["models"][0], key["days"][0]):
            self.assertIsNone(entry["knownCostUsd"])
            self.assertIsNone(entry["estimatedCostUsd"])
            self.assertEqual(entry["coverage"]["pricingUnavailableRequests"], 1)
            self.assertFalse(entry["coverage"]["costComplete"])

    def test_mixed_known_estimated_partial_unknown_and_unpriced_are_separate(self):
        rows = [
            record(input_tokens=100, output_tokens=10, request_id="known"),
            record(input_tokens=40, output_tokens=3, usage_source="estimated",
                   request_id="estimated"),
            record(input_tokens=50, output_tokens=None, total_tokens=None,
                   usage_source="partial", request_id="partial"),
            record(input_tokens=20, output_tokens=2, status="upstream_error",
                   request_id="failed"),
            record(input_tokens=0, output_tokens=0, usage_source="none",
                   total_tokens=0, request_id="unknown"),
            record(input_tokens=1, output_tokens=2, model="unpriced",
                   request_id="unpriced"),
        ]
        self.write(rows)

        def price(model, *args):
            return ((0.0, False, None, False, None) if model == "unpriced"
                    else self.price(model, *args))

        key = metering.read_share_usage(str(self.ledger), price)["keys"][0]
        for entry in (key, key["days"][0]):
            usage = entry["tokenUsage"]
            self.assertEqual(usage["totalTokens"], 113)
            self.assertEqual(usage["lowerBoundTokens"], 72)
            self.assertEqual(usage["estimatedTokens"], 43)
            self.assertEqual(usage["completeRequests"], 2)
            self.assertEqual(usage["lowerBoundRequests"], 2)
            self.assertEqual(usage["estimatedRequests"], 1)
            self.assertEqual(usage["unknownRequests"], 1)
            self.assertEqual(entry["coverage"]["records"], 6)
            self.assertEqual(entry["unknownRequests"], 1)
            self.assertEqual(entry["unpricedRequests"], 1)
            self.assertFalse(entry["coverage"]["costComplete"])
        model = {row["model"]: row for row in key["models"]}["unpriced"]
        self.assertEqual(model["tokenUsage"]["totalTokens"], 3)
        self.assertEqual(model["unpricedRequests"], 1)

    def test_true_zero_and_invalid_token_components_stay_distinct(self):
        rows = [record(model="zero", input_tokens=0, output_tokens=0,
                       total_tokens=0)]
        for index, value in enumerate((True, "12", -1, 1.5, float("inf"))):
            rows.append(record(model=f"invalid-{index}", input_tokens=7,
                               output_tokens=value, total_tokens=None))
        self.write(rows)
        # No pricing boundary is needed to classify trustworthy token evidence.
        key = metering.read_share_usage(str(self.ledger))["keys"][0]
        by_model = {row["model"]: row for row in key["models"]}
        zero = by_model["zero"]["tokenUsage"]
        self.assertEqual(zero["totalTokens"], 0)
        self.assertEqual(zero["completeRequests"], 1)
        for index in range(5):
            usage = by_model[f"invalid-{index}"]["tokenUsage"]
            self.assertIsNone(usage["totalTokens"])
            self.assertEqual(usage["lowerBoundTokens"], 7)
            self.assertIsNone(usage["lowerBoundOutputTokens"])

    def test_undated_usage_has_its_own_bucket_and_beijing_day_is_retained(self):
        self.write([record(time="2026-10-01T16:10:00Z"),
                    record(time="not-a-date", usage_source="none",
                           input_tokens=0, output_tokens=0, total_tokens=0)])
        before = self.ledger.read_bytes()
        key = metering.read_share_usage(str(self.ledger), self.price)["keys"][0]
        self.assertEqual([row["day"] for row in key["days"]], ["2026-10-02"])
        self.assertEqual(key["undated"]["coverage"]["records"], 1)
        self.assertEqual(key["undated"]["unknownRequests"], 1)
        self.assertEqual(key["coverage"]["unknownTimeRequests"], 1)
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_invalid_price_is_unpriced_but_trusted_usage_remains_visible(self):
        self.write([record()])
        for cost in (True, float("nan"), float("inf"), -1):
            with self.subTest(cost=cost):
                price = lambda *args: (cost, True, None, False, "v-invalid")
                key = metering.read_share_usage(str(self.ledger), price)["keys"][0]
                for entry in (key, key["models"][0], key["days"][0]):
                    self.assertEqual(entry["unpricedRequests"], 1)
                    self.assertFalse(entry["coverage"]["costComplete"])
                    self.assertEqual(entry["tokenUsage"]["totalTokens"], 110)

    def test_missing_both_sides_does_not_become_complete_via_a_total_field(self):
        self.write([record(input_tokens=None, output_tokens=None,
                           total_tokens=110)])
        key = metering.read_share_usage(str(self.ledger), self.price)["keys"][0]
        for entry in (key, key["models"][0], key["days"][0]):
            self.assertIsNone(entry["tokenUsage"]["totalTokens"])
            self.assertEqual(entry["coverage"]["unknownUsageRequests"], 1)
            self.assertFalse(entry["coverage"]["costComplete"])




if __name__ == "__main__":
    unittest.main()
