# -*- coding: utf-8 -*-
"""「新价只作用于后续请求」——价格版本语义的隔离 fixture 验证（执行轮 6）。

本文件回答一个问题：在**现有实现**下，「改价只影响改价之后的请求」是否成立。
做法是两种形状各跑一遍：

* **旧形状**（`prices.json` 无 `versions` 键）：版本机制不生效——任何时刻（含时刻
  缺失/不可解析）都命中唯一的恒生效默认段，金额与版本化前的算法逐位一致；
* **含版本段的形状**（`versions: [...]`）：每条记录按**该记录时刻**生效的那一段
  计价——加一段新版本不会改动更早记录的金额，更晚的记录用新版本的价与汇率。

隔离手法与 `tests/test_usage_price_versions.py` 的 `PriceFixture` 同一先例：价格表、
override、四条账本、会话/控制令牌全部重定向到临时目录（仓库 `data/` 一个字节不写），
并重置 60 秒价格缓存。

运行：`python -m unittest tests.test_round6_price_version_ledger -v`
（工作目录 `ai-gateway/`）
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


USAGE_SERVICE_PATH = Path(__file__).resolve().parents[1] / "usagesrv.py"

SPEC = importlib.util.spec_from_file_location("usage_price_round6", USAGE_SERVICE_PATH)
usage_service = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = usage_service
SPEC.loader.exec_module(usage_service)


def today_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# v1：基础档 + 峰档；v2：基础档与峰档都翻倍（同一 token 数下金额正好 2 倍）。
FLASH_V1 = {
    "cacheHit": 0.007, "cacheMiss": 0.22, "output": 0.66,
    "offPeak": {"cacheHit": 0.007, "cacheMiss": 0.22, "output": 0.66},
    "peak": {"cacheHit": 0.014, "cacheMiss": 0.44, "output": 1.32},
}
FLASH_V2 = {
    "cacheHit": 0.007, "cacheMiss": 0.44, "output": 1.32,
    "offPeak": {"cacheHit": 0.007, "cacheMiss": 0.44, "output": 1.32},
    "peak": {"cacheHit": 0.014, "cacheMiss": 0.88, "output": 2.64},
}
# 峰窗按小时（版本段内 effectiveAtMs 之后才生效）：北京 09:00-12:00。
PEAK_WINDOWS = [{"start": 1, "end": 4}]

TS_V1_OFF = usage_service._parse_ts("2026-09-25T12:00:00Z")   # 北京 20:00，谷
TS_V1_PEAK = usage_service._parse_ts("2026-09-25T02:00:00Z")  # 北京 10:00，峰
TS_V2_OFF = usage_service._parse_ts("2026-10-05T12:00:00Z")   # 北京 20:00，谷
TS_V2_PEAK = usage_service._parse_ts("2026-10-05T02:00:00Z")  # 北京 10:00，峰
TS_BEFORE_FIRST = usage_service._parse_ts("2026-01-05T12:00:00Z")  # 早于首段


def v1_segment():
    return {
        "versionId": "v1", "effectiveAt": "2026-08-01T00:00:00Z",
        "checkedAt": "2026-09-01", "exchangeRate": 7.2,
        "peak": {"enabled": True, "effectiveAt": "2026-08-01T00:00:00Z",
                  "windows": PEAK_WINDOWS},
        "models": {"deepseek-v4-flash": dict(FLASH_V1)},
        "providers": {},
    }


def v2_segment():
    return {
        "versionId": "v2", "effectiveAt": "2026-10-01T00:00:00Z",
        "checkedAt": today_iso(), "exchangeRate": 8.0,
        "peak": {"enabled": True, "effectiveAt": "2026-10-01T00:00:00Z",
                  "windows": PEAK_WINDOWS},
        "models": {"deepseek-v4-flash": dict(FLASH_V2)},
        "providers": {},
    }


def v3_segment():
    seg = v2_segment()
    seg["versionId"] = "v3"
    seg["effectiveAt"] = "2026-11-01T00:00:00Z"
    seg["exchangeRate"] = 9.9
    seg["models"] = {"deepseek-v4-flash": {
        "cacheHit": 0.1, "cacheMiss": 9.0, "output": 9.0,
        "offPeak": {"cacheHit": 0.1, "cacheMiss": 9.0, "output": 9.0},
        "peak": {"cacheHit": 0.2, "cacheMiss": 18.0, "output": 18.0}}}
    return seg


def old_shape_table():
    return {
        "checkedAt": today_iso(),
        "exchangeRate": 7.2,
        "peak": {"enabled": True, "effectiveAt": "2026-08-01T00:00:00Z",
                  "windows": PEAK_WINDOWS},
        "models": {"deepseek-v4-flash": dict(FLASH_V1)},
        "providers": {},
    }


def versioned_table(segments):
    return {
        "checkedAt": today_iso(),
        "exchangeRate": 7.2,   # 顶层「现行」视图；版本化时逐条按段取
        "peak": {"enabled": False},
        "models": {},
        "versions": segments,
    }


class PriceVersionLedgerFixture(unittest.TestCase):
    """两种形状各跑一遍：价格表与账本全部落在临时目录。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.prices_path = base / "prices.json"
        self.override_path = base / "price.override.json"
        self.chat2api_log = base / "usage-chat2api.jsonl"
        self.dsh_snapshot = base / "dsh-ledger-20260902.json"
        for name, content in (("chat2api_log", "[]"), ("dsh_snapshot", "{}")):
            getattr(self, name).write_text(content, encoding="utf-8")
        self.override_path.write_text("{}", encoding="utf-8")
        session_dir = base / "session"
        session_dir.mkdir(parents=True, exist_ok=True)
        for attr, path in (("PRICE_FILE", self.prices_path),
                           ("OVERRIDE_FILE", self.override_path),
                           ("CHAT2API_LOG", self.chat2api_log),
                           ("DSH_SNAPSHOT_FILE", self.dsh_snapshot),
                           ("SESSION_CODE_FILE", session_dir / "session-code"),
                           ("CONTROL_TOKEN_FILE", session_dir / "control-token")):
            patcher = mock.patch.object(usage_service, attr, str(path))
            patcher.start()
            self.addCleanup(patcher.stop)
        cache_patcher = mock.patch.object(
            usage_service, "_price_cache", {"at": 0.0, "data": None})
        cache_patcher.start()
        self.addCleanup(cache_patcher.stop)
        usage_service._summary_cache_reset_for_tests()

    # ---------------------------------------------------------------- 工具

    def write_prices(self, obj):
        self.prices_path.write_text(json.dumps(obj), encoding="utf-8")
        usage_service._price_cache["at"] = 0.0
        usage_service._price_cache["data"] = None

    def write_ledger(self, records):
        self.chat2api_log.write_text(
            "\n".join(json.dumps(rec) for rec in records) + "\n", encoding="utf-8")
        usage_service._summary_cache_reset_for_tests()

    def model_row(self):
        """build_summary 里 chat2api 服务下该模型的一行（无价/版本未知另列）。"""
        summary = usage_service.build_summary()
        services = summary.get("services") or {}
        models = (services.get("chat2api") or {}).get("models") or []
        return next((m for m in models
                     if m.get("model") == "deepseek-v4-flash"), None)

    def day_row(self, day):
        summary = usage_service.build_summary()
        services = summary.get("services") or {}
        days = (services.get("chat2api") or {}).get("days") or []
        return next((d for d in days if d.get("day") == day), None)

    @staticmethod
    def estimate(ts, input_tok=1000000, output_tok=1000000):
        return usage_service.estimate_record_with_version(
            "deepseek-v4-flash", ts, input_tok, output_tok, 0, 0)

    # ------------------------------------------------- 形状 A：旧形状（无 versions）

    def test_old_shape_is_a_single_always_on_segment(self):
        self.write_prices(old_shape_table())
        table = usage_service.load_price_table()
        self.assertIs(table["versioned"], False)
        self.assertEqual(len(table["versions"]), 1)
        segment = table["versions"][0]
        self.assertIsNone(segment["versionId"])
        self.assertIsNone(segment["effectiveAtMs"])
        self.assertEqual(segment["exchangeRate"], 7.2)
        # 任何时刻（含时刻缺失/不可解析）都命中同一段。
        for at_ms in (TS_V1_OFF, TS_V1_PEAK, TS_BEFORE_FIRST, None):
            self.assertIs(usage_service.version_at(table, at_ms), segment)

    def test_old_shape_amount_equals_pre_versioning_math(self):
        """旧形状下金额与「直接用顶层表算」逐位一致（版本机制惰性）。"""
        self.write_prices(old_shape_table())
        table = usage_service.load_price_table()
        for ts in (TS_V1_OFF, TS_V1_PEAK):
            entry, _mode, _matched, _source = usage_service.resolve_price(
                "deepseek-v4-flash", ts)
            peak_cfg = table["peak"]
            expected = usage_service.cost_of(
                {"input": 1000000, "output": 1000000, "cache": 0, "reasoning": 0},
                entry, ts, peak_cfg)
            cost, priced, matched, peak, version_id = self.estimate(ts)
            self.assertTrue(priced)
            self.assertEqual(matched, "deepseek-v4-flash")
            self.assertIsNone(version_id)          # 旧形状：无版本标识
            self.assertAlmostEqual(cost, expected, places=12)

    def test_old_shape_and_one_segment_versioned_shape_agree(self):
        """单段版本化形状（同一份字段）与旧形状逐字段一致。"""
        self.write_prices(old_shape_table())
        old_costs = [self.estimate(TS_V1_OFF)[0], self.estimate(TS_V1_PEAK)[0]]
        self.write_ledger([{"time": "2026-09-25T12:00:00Z",
                           "model": "deepseek-v4-flash",
                           "input_tokens": 1000000, "output_tokens": 0}])
        old_row = self.model_row()
        old_day = self.day_row("2026-09-25")
        self.write_prices(versioned_table([v1_segment()]))
        new_costs = [self.estimate(TS_V1_OFF)[0], self.estimate(TS_V1_PEAK)[0]]
        new_row = self.model_row()
        new_day = self.day_row("2026-09-25")
        self.assertEqual(old_costs, new_costs)
        self.assertEqual(old_row["costCny"], new_row["costCny"])
        self.assertEqual(old_day["costCny"], new_day["costCny"])
        self.assertEqual(old_row["priced"], new_row["priced"])
        self.assertEqual(old_row["unpriced"], new_row["unpriced"])

    # --------------------------------------------- 形状 B：含版本段（v1 + v2）

    def test_history_amount_is_frozen_when_a_new_version_is_added(self):
        """加 v2 段前后，v1 时段的记录金额逐位不变。"""
        self.write_prices(versioned_table([v1_segment()]))
        before = {ts: self.estimate(ts) for ts in (TS_V1_OFF, TS_V1_PEAK)}
        self.write_prices(versioned_table([v1_segment(), v2_segment()]))
        after = {ts: self.estimate(ts) for ts in (TS_V1_OFF, TS_V1_PEAK)}
        self.assertEqual(before, after)
        for ts, result in after.items():
            self.assertTrue(result[1], "v1 时段记录必须有价")
            self.assertEqual(result[4], "v1", "v1 时段记录必须命中 v1 段")

    def test_new_records_use_the_new_version(self):
        """v2 生效之后的记录用 v2 的价与汇率，而不是 v1。"""
        self.write_prices(versioned_table([v1_segment(), v2_segment()]))
        cost_v1, priced_v1, _m1, _p1, version_v1 = self.estimate(TS_V1_OFF)
        cost_v2, priced_v2, _m2, _p2, version_v2 = self.estimate(TS_V2_OFF)
        self.assertTrue(priced_v1 and priced_v2)
        self.assertEqual(version_v1, "v1")
        self.assertEqual(version_v2, "v2")
        # v2 的谷价正好是 v1 的 2 倍（cacheMiss 0.22→0.44、output 0.66→1.32）：
        # 这条断言证明「用的是新版本的价」而不是别的档位。
        self.assertAlmostEqual(cost_v2, 2 * cost_v1, places=12)

    def test_peak_rule_also_follows_the_version(self):
        """峰谷子档同样按版本：窗口内计峰、窗口外计谷，且两段各自加倍。"""
        self.write_prices(versioned_table([v1_segment(), v2_segment()]))
        off_v1, _p, _m, peak_off_v1, _v = self.estimate(TS_V1_OFF)
        pk_v1, _p, _m, peak_pk_v1, _v = self.estimate(TS_V1_PEAK)
        off_v2, _p, _m, peak_off_v2, _v = self.estimate(TS_V2_OFF)
        pk_v2, _p, _m, peak_pk_v2, _v = self.estimate(TS_V2_PEAK)
        # 北京 20:00 在窗口外 → 不计峰；北京 10:00 在窗口内 → 计峰。
        self.assertFalse(peak_off_v1)
        self.assertFalse(peak_off_v2)
        self.assertTrue(peak_pk_v1)
        self.assertTrue(peak_pk_v2)
        # 两段各自的峰价都是自己谷价的 2 倍：峰谷规则随版本，不随最新段。
        self.assertAlmostEqual(pk_v1, 2 * off_v1, places=12)
        self.assertAlmostEqual(pk_v2, 2 * off_v2, places=12)

    def test_exchange_rate_follows_the_version(self):
        """CNY 折算按记录时刻所属版本的汇率：v1=7.2、v2=8.0。"""
        self.write_prices(versioned_table([v1_segment(), v2_segment()]))
        self.write_ledger([
            {"time": "2026-09-25T12:00:00Z", "model": "deepseek-v4-flash",
             "input_tokens": 1000000, "output_tokens": 0},
            {"time": "2026-10-05T12:00:00Z", "model": "deepseek-v4-flash",
             "input_tokens": 1000000, "output_tokens": 0},
        ])
        usd_v1, priced_v1, _m, _p, version_v1 = self.estimate(
            TS_V1_OFF, 1000000, 0)
        usd_v2, priced_v2, _m, _p, version_v2 = self.estimate(
            TS_V2_OFF, 1000000, 0)
        self.assertTrue(priced_v1 and priced_v2)
        self.assertEqual((version_v1, version_v2), ("v1", "v2"))
        self.assertNotAlmostEqual(usd_v1, usd_v2, places=6)
        day_v1 = self.day_row("2026-09-25")
        day_v2 = self.day_row("2026-10-05")
        # 各自按自己版本的汇率折算，不按现价汇率。
        self.assertAlmostEqual(day_v1["costCny"], round(usd_v1 * 7.2, 4), places=4)
        self.assertAlmostEqual(day_v2["costCny"], round(usd_v2 * 8.0, 4), places=4)
        self.assertNotAlmostEqual(day_v1["costCny"], round(usd_v1 * 8.0, 4), places=4)

    def test_adding_a_third_version_does_not_touch_earlier_records(self):
        """再加 v3 段，v1/v2 记录的金额与版本仍逐位不变。"""
        self.write_prices(versioned_table([v1_segment(), v2_segment()]))
        before = {ts: self.estimate(ts)
                  for ts in (TS_V1_OFF, TS_V1_PEAK, TS_V2_OFF, TS_V2_PEAK)}
        self.write_prices(
            versioned_table([v1_segment(), v2_segment(), v3_segment()]))
        after = {ts: self.estimate(ts)
                 for ts in (TS_V1_OFF, TS_V1_PEAK, TS_V2_OFF, TS_V2_PEAK)}
        self.assertEqual(before, after)

    def test_record_before_the_first_segment_is_unknown_not_latest(self):
        """早于首段生效时刻的记录是「未知」，绝不按最新价计。"""
        self.write_prices(versioned_table([v1_segment(), v2_segment()]))
        cost, priced, matched, _peak, version_id = self.estimate(TS_BEFORE_FIRST)
        self.assertFalse(priced)
        self.assertEqual(cost, 0.0)
        self.assertIsNone(matched)
        self.assertIsNone(version_id)
        # 汇总里如实分列「版本未知」，不折 0 冒充有价。
        self.write_ledger([{"time": "2026-01-05T12:00:00Z",
                           "model": "deepseek-v4-flash",
                           "input_tokens": 1000, "output_tokens": 1000}])
        row = self.model_row()
        self.assertIsNotNone(row)
        self.assertEqual(row["unknown_version"], 1)
        self.assertEqual(row["priced"], 0)
        self.assertIsNone(row["costCny"])

    def test_missing_record_time_is_unknown_under_versioned_table(self):
        """版本化价表下，记录时刻缺失是「未知」，不落最新段。"""
        self.write_prices(versioned_table([v1_segment(), v2_segment()]))
        cost, priced, _matched, _peak, version_id = self.estimate(None)
        self.assertFalse(priced)
        self.assertEqual(cost, 0.0)
        self.assertIsNone(version_id)
        # 旧形状下同一情形必须仍然有价（对照，证明上面那条不是「时间全失效」）。
        self.write_prices(old_shape_table())
        _cost, priced_old, _m, _p, version_old = self.estimate(None)
        self.assertTrue(priced_old)
        self.assertIsNone(version_old)


if __name__ == "__main__":
    unittest.main(verbosity=2)
