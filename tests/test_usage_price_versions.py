# -*- coding: utf-8 -*-
"""Price-table versions (C1, 2026-09-25) and the read-only share-usage mount.

What this file pins:
  - the compat red line: an OLD-SHAPE prices.json (no "versions" key) keeps
    working field-for-field — the version machinery is inert, every instant
    (including a missing/unparseable one) resolves to the single always-on
    default segment, and the personal /api/summary payload keeps its existing
    values;
  - a versioned prices.json ("versions": [...]) resolves each ledger record
    against the segment in effect at THAT record's time: price switch, exchange
    rate and peak rules all follow the version, and adding a later segment never
    recomputes an earlier record's amount;
  - unknown stays unknown instead of being folded to 0: missing usage, missing
    price and an un-attributable record time are counted separately, and the CNY
    subtotal says so (costCnyPartial / costCny=null);
  - the optional GET /api/share/usage mount: read-only, on the existing console
    auth surface, honest errors when share_metering is absent or reading fails.

All file inputs are redirected to temp dirs (the repo's data/ files are never
touched) and the 60s price-table cache is reset per test.
"""

import http.client
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


AI_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
if str(AI_GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_GATEWAY_ROOT))
from shared import pricing_rules  # noqa: E402

USAGE_SERVICE_PATH = AI_GATEWAY_ROOT / "usagesrv.py"

SPEC = importlib.util.spec_from_file_location("usage_price_versions", USAGE_SERVICE_PATH)
usage_service = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = usage_service
SPEC.loader.exec_module(usage_service)

# The paths STATS_ENV was constructed from, captured before any test redirects
# the usagesrv module constants (PriceFixture patches them per test).
_STATS_ENV_PATHS = {
    "chat2api_log": usage_service.CHAT2API_LOG,
    "dsh_snapshot": usage_service.DSH_SNAPSHOT_FILE,
    "dsh_merge_day": usage_service.DSH_MERGE_DAY,
    "magpie_price_snapshot": usage_service.MAGPIE_PRICE_SNAPSHOT_FILE,
    "magpie_price_status": usage_service.MAGPIE_PRICE_STATUS_FILE,
}


# Guard before any test can construct a default management client. Patch the
# constants and the deployment loader, and count attempted production access
# separately because a route may swallow the exception.
_ISOLATION_ATTEMPTS = []
_ISOLATION_PATCHERS = []
_ISOLATION_MODULES = []
_ISOLATION_TEMP = None


def _refuse_real_kernel(*args, **kwargs):
    _ISOLATION_ATTEMPTS.append("real_named_pipe_kernel")
    raise AssertionError("offline test attempted a real named-pipe kernel")


def _guard_usage_module(module):
    if any(item[0] is module for item in _ISOLATION_MODULES):
        return
    root = Path(_ISOLATION_TEMP.name)
    _ISOLATION_MODULES.append((module, module._console_manager,
                               module._share_manager))
    values = {"SHARE_MANAGEMENT_CONFIG_FILE": str(root / "absent-deployment.json"),
              "CONSOLE_STATE_FILE": str(root / (module.__name__ + ".sqlite3")),
              "CONSOLE_LOCK_DIR": str(root / (module.__name__ + "-locks")),
              "_console_manager": None, "_share_manager": None}
    original_loader = module._load_share_management_deployment

    def guarded_loader():
        target = Path(module.SHARE_MANAGEMENT_CONFIG_FILE).resolve()
        if not target.is_relative_to(root.resolve()):
            _ISOLATION_ATTEMPTS.append("production_deployment_loader")
            raise AssertionError("deployment config is outside the test sandbox")
        return original_loader()

    values["_load_share_management_deployment"] = guarded_loader
    for name, value in values.items():
        patcher = mock.patch.object(module, name, value)
        patcher.start()
        _ISOLATION_PATCHERS.append(patcher)


def setUpModule():
    global _ISOLATION_TEMP, _ISOLATION_SAVED_SEAMS
    import share.broker_client as share_broker_client
    import share.broker_server as share_broker_server
    from console import share_actions as console_share_actions
    _ISOLATION_ATTEMPTS.clear()
    _ISOLATION_TEMP = tempfile.TemporaryDirectory(prefix="offline-management-")
    root = Path(_ISOLATION_TEMP.name)
    patcher = mock.patch.dict(os.environ, {
        "SHARE_MANAGEMENT_CONFIG": str(root / "absent-deployment.json"),
        "SHARE_STORE_FILE": str(root / "share.db"),
        "CONSOLE_STATE_FILE": str(root / "console.sqlite3"),
        "CONSOLE_LOCK_DIR": str(root / "console-locks")})
    patcher.start()
    _ISOLATION_PATCHERS.append(patcher)
    import usagesrv as canonical_usage_service
    _guard_usage_module(canonical_usage_service)
    _guard_usage_module(usage_service)
    _ISOLATION_SAVED_SEAMS = {
        name: value for name, value in vars(console_share_actions).items()
        if name.startswith("_") and name.upper() == name}
    for owner, name in ((share_broker_client.NamedPipeBrokerTransport,
                         "_declare_real_kernel"),
                        (share_broker_server, "_declared_kernel")):
        patcher = mock.patch.object(owner, name, _refuse_real_kernel)
        patcher.start()
        _ISOLATION_PATCHERS.append(patcher)


def tearDownModule():
    from console import share_actions as console_share_actions
    try:
        for module, old_console, _old_share in _ISOLATION_MODULES:
            current = module._console_manager
            if current is not None and current is not old_console:
                current.close(wait=True)
    finally:
        for patcher in reversed(_ISOLATION_PATCHERS):
            patcher.stop()
        _ISOLATION_PATCHERS.clear()
        _ISOLATION_MODULES.clear()
        for name, value in _ISOLATION_SAVED_SEAMS.items():
            setattr(console_share_actions, name, value)
        _ISOLATION_TEMP.cleanup()
    if _ISOLATION_ATTEMPTS:
        raise AssertionError("management isolation breached: " +
                             repr(_ISOLATION_ATTEMPTS))


def reload_usage_service():
    """Re-exec the module in place (fresh import-time state) — the 'restart'."""
    usage_service._price_cache["data"] = None
    SPEC.loader.exec_module(usage_service)


FLASH_V1 = {
    "cacheHit": 0.007, "cacheMiss": 0.22, "output": 0.66,
    "offPeak": {"cacheHit": 0.007, "cacheMiss": 0.22, "output": 0.66},
    "peak": {"cacheHit": 0.014, "cacheMiss": 0.44, "output": 1.32},
    "legacyBase": {"cacheHit": 0.0028, "cacheMiss": 0.14, "output": 0.28},
}
PEAK_WINDOWS = [{"start": 1, "end": 4}]
LUNA = {"input": 0.2, "cachedInput": 0.02, "output": 1.2, "billingMode": "flat"}

TS_OFF = usage_service._parse_ts("2026-09-25T12:00:00Z")      # Beijing 20:00, off-peak
TS_PEAK = usage_service._parse_ts("2026-09-25T02:00:00Z")     # Beijing 10:00, peak window
TS_WEEKEND = usage_service._parse_ts("2026-09-26T02:00:00Z")   # Beijing Sat 10:00, weekend
TS_LEGACY = usage_service._parse_ts("2026-08-10T02:00:00Z")    # before LEGACY_BASE_BOUNDARY
TS_V1_ERA = usage_service._parse_ts("2026-09-25T00:00:00Z")    # inside version v1
TS_V2_ERA = usage_service._parse_ts("2026-10-05T00:00:00Z")    # inside version v2


def today_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def record(time, model="deepseek-v4-flash", **tokens):
    rec = {"time": time, "model": model}
    rec.update(tokens)
    rec["_ms"] = usage_service._parse_ts(time)
    return rec


class PriceFixture(unittest.TestCase):
    """Redirects every price/ledger input to temp files; resets caches."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.prices_path = base / "prices.json"
        self.override_path = base / "price.override.json"
        self.chat2api_log = base / "usage-chat2api.jsonl"
        self.dsh_snapshot = base / "dsh-ledger-20260902.json"
        for name, path in (("chat2api_log", "[]"), ("dsh_snapshot", "{}")):
            getattr(self, name).write_text(path, encoding="utf-8")
        self.write_prices(self.old_shape_prices())
        session_dir = base / "session"
        session_dir.mkdir(parents=True, exist_ok=True)
        for attr, path in (("PRICE_FILE", self.prices_path),
                           ("OVERRIDE_FILE", self.override_path),
                           ("CHAT2API_LOG", self.chat2api_log),
                           ("DSH_SNAPSHOT_FILE", self.dsh_snapshot),
                           # 会话/管理令牌文件按属性重定向：不改全局 env
                           # （多个测试模块共用同一环境变量，改 env 会互相踩）。
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

    @staticmethod
    def old_shape_prices():
        return {
            "checkedAt": today_iso(),
            "exchangeRate": 7.2,
            "peak": {"enabled": True,
                     "effectiveAt": "2026-08-01T00:00:00Z",
                     "windows": PEAK_WINDOWS},
            "models": {"deepseek-v4-flash": dict(FLASH_V1)},
            "providers": {"openai": {"models": {
                "gpt-5.6-luna": dict(LUNA),
                # 同一模型 id 在两档都有价：models 档优先（既有断言）。
                "deepseek-v4-flash": {"cacheHit": 0.007, "cacheMiss": 0.22, "output": 9.9},
            }}},
        }

    @staticmethod
    def versioned_prices():
        return {
            "checkedAt": today_iso(),
            "exchangeRate": 8.0,          # 顶层「现行」视图：版本化时取当前生效段
            "peak": {"enabled": False},
            "models": {},
            "versions": [
                {"versionId": "v1", "effectiveAt": "2026-08-01T00:00:00Z",
                 "checkedAt": "2026-09-01", "exchangeRate": 7.2,
                 "peak": {"enabled": True, "effectiveAt": "2026-08-01T00:00:00Z",
                          "windows": PEAK_WINDOWS},
                 "models": {"deepseek-v4-flash": dict(FLASH_V1)},
                 "providers": {"openai": {"models": {"gpt-5.6-luna": dict(LUNA)}}}},
                {"versionId": "v2", "effectiveAt": "2026-10-01T00:00:00Z",
                 "checkedAt": today_iso(), "exchangeRate": 8.0,
                 "peak": {"enabled": True, "effectiveAt": "2026-10-01T00:00:00Z",
                          "windows": PEAK_WINDOWS},
                 "models": {"deepseek-v4-flash": {
                     "cacheHit": 0.007, "cacheMiss": 0.44, "output": 1.32,
                     "offPeak": {"cacheHit": 0.007, "cacheMiss": 0.44, "output": 1.32},
                     # v2 的峰价与 v1 不同：峰谷规则与价格都按版本。
                     "peak": {"cacheHit": 0.014, "cacheMiss": 0.88, "output": 2.64}}}},
            ],
        }

    def write_prices(self, obj):
        self.prices_path.write_text(json.dumps(obj), encoding="utf-8")
        usage_service._price_cache["at"] = 0.0
        usage_service._price_cache["data"] = None

    def write_ledger(self, path, records):
        lines = [json.dumps({k: v for k, v in rec.items() if k != "_ms"})
                 for rec in records]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def write_dsh_snapshot(self, days):
        self.dsh_snapshot.write_text(json.dumps(
            {"config": {"pricingCurrency": "USD"}, "days": days}), encoding="utf-8")

    def summary(self):
        return usage_service.build_summary()


PEAK_WINDOWS = [{"start": 1, "end": 4}, {"start": 6, "end": 10}]


class OldShapeCompatibilityTest(PriceFixture):
    """旧形状（当前 prices.json，无 versions 键）必须逐字段不变。"""

    def test_default_segment_is_always_in_effect(self):
        table = usage_service.load_price_table()
        self.assertIs(table["versioned"], False)
        self.assertEqual(len(table["versions"]), 1)
        version = table["versions"][0]
        self.assertIsNone(version["versionId"])
        self.assertIsNone(version["effectiveAtMs"])
        self.assertEqual(version["exchangeRate"], 7.2)
        self.assertEqual(version["tiers"], table["tiers"])
        self.assertEqual(version["peak"], table["peak"])
        # 任何时刻（含缺失/不可解析）都命中同一段。
        for at_ms in (TS_OFF, TS_PEAK, TS_LEGACY, None, float("nan")):
            with self.subTest(at_ms=at_ms):
                self.assertIs(usage_service.version_at(table, at_ms), version)
                self.assertEqual(usage_service.version_rate(table, at_ms), 7.2)

    def test_meta_and_top_level_fields_unchanged(self):
        table = usage_service.load_price_table()
        self.assertEqual(table["meta"], {
            "pricesFound": True,
            "checkedAt": today_iso(),
            "checkedAgeDays": 0,
            "checkedStale": False,
            "overrideFound": False,
        })
        self.assertEqual(table["exchangeRate"], 7.2)
        self.assertEqual(table["peak"], {
            "enabled": True,
            "effectiveAtMs": usage_service._parse_ts("2026-08-01T00:00:00Z"),
            "windows": PEAK_WINDOWS,
        })
        self.assertEqual([t[1] for t in table["tiers"]], ["deepseek-peak", "flat"])
        self.assertEqual(table["versionMeta"], {
            "versioned": False,
            "count": 1,
            "invalidCount": 0,
            "current": {"versionId": None, "effectiveAt": None,
                        "exchangeRate": 7.2, "checkedAt": today_iso(),
                        "checkedStale": False},
        })

    def test_one_arg_resolve_price_matches_two_arg(self):
        for at_ms in (TS_OFF, TS_PEAK, None, "not-a-time"):
            with self.subTest(at_ms=at_ms):
                self.assertEqual(usage_service.resolve_price("deepseek-v4-flash"),
                                 usage_service.resolve_price("deepseek-v4-flash", at_ms))
        entry, mode, matched, source = usage_service.resolve_price("deepseek-v4-flash")
        self.assertEqual((mode, matched, source), ("deepseek-peak", "deepseek-v4-flash", "prices"))
        self.assertEqual(entry["output"], 0.66)
        entry, mode, matched, source = usage_service.resolve_price("gpt-5.6-luna")
        self.assertEqual((mode, matched, source), ("flat", "gpt-5.6-luna", "prices"))
        self.assertEqual((entry["cacheMiss"], entry["cacheHit"]), (0.2, 0.02))

    def test_estimate_record_golden_points_unchanged(self):
        cases = [(TS_OFF, 0.1795, False), (TS_PEAK, 0.359, True),
                 (TS_WEEKEND, 0.1795, False), (TS_LEGACY, 0.0994, True),
                 (None, 0.1795, False)]
        for at_ms, expected, in_peak in cases:
            with self.subTest(at_ms=at_ms):
                cost, priced, matched, peak = usage_service.estimate_record(
                    "openwebui:DeepSeek-V4-Flash-0731", at_ms, 1000000, 100000, 500000, 0)
                self.assertTrue(priced)
                self.assertEqual(matched, "deepseek-v4-flash")
                self.assertEqual(peak, in_peak)
                self.assertAlmostEqual(cost, expected, places=9)
                # 版本化契约：旧形状无显式版本段，标识为 None，成本一致。
                versioned = usage_service.estimate_record_with_version(
                    "openwebui:DeepSeek-V4-Flash-0731", at_ms, 1000000, 100000, 500000, 0)
                self.assertEqual(versioned, (cost, priced, matched, peak, None))

    def test_override_still_wins_and_carries_the_record_time(self):
        self.override_path.write_text(json.dumps(
            {"fixture:my-alias": {"target": "deepseek-v4-flash"}}), encoding="utf-8")
        usage_service._price_cache["at"] = 0.0
        usage_service._price_cache["data"] = None
        entry, mode, matched, source = usage_service.resolve_price(
            "fixture:my-alias", TS_PEAK)
        self.assertEqual((matched, source), ("deepseek-v4-flash", "prices"))
        self.assertEqual(entry["cacheMiss"], 0.22)


class SummaryOldShapeUnchangedTest(PriceFixture):
    """个人 /api/summary：旧形状下的既有断言逐字段不变。"""

    def setUp(self):
        super().setUp()
        self.write_ledger(self.chat2api_log, [
            record("2026-09-25T12:00:00Z", "openwebui:DeepSeek-V4-Flash-0731",
                   input_tokens=1000000, output_tokens=100000,
                   cached_input_tokens=500000, reasoning_tokens=0),
            record("2026-09-25T02:00:00Z", "openwebui:DeepSeek-V4-Flash-0731",
                   input_tokens=1000000, output_tokens=100000,
                   cached_input_tokens=500000, reasoning_tokens=0),
        ])
        self.write_dsh_snapshot({"2026-09-02": {
            "calls": 2, "cost": 1.5, "input": 10, "output": 20,
            "cacheRead": 30, "reasoning": 40,
            "byProviderModel": {"provider:kept": {"calls": 2, "cost": 1.5}},
        }})

    def test_existing_summary_fields_are_unchanged(self):
        payload = self.summary()
        self.assertEqual(payload["exchangeRate"], 7.2)
        self.assertEqual(payload["currency"], "CNY")
        # B0：默认汇总只装配 UM 主链与冻结的 DSH 快照。
        self.assertEqual(sorted(payload["services"]), ["chat2api", "dsh"])
        self.assertEqual(payload["sources"]["priceMeta"], {
            "pricesFound": True, "checkedAt": today_iso(), "checkedAgeDays": 0,
            "checkedStale": False, "overrideFound": False,
        })
        self.assertEqual(payload["peak"]["enabled"], True)
        self.assertEqual(payload["peak"]["effectiveAt"], "2026-08-01")
        self.assertEqual(payload["peak"]["weekendOffpeak"], True)
        self.assertEqual(payload["peak"]["windows"],
                         [{"beijing": "09:00-12:00", "rate": 2.0},
                          {"beijing": "14:00-18:00", "rate": 2.0}])

        for retired in ("qoder", "m365"):
            self.assertNotIn(retired, payload["services"])

        c2a = payload["services"]["chat2api"]
        # 0.1795 (off-peak) + 0.359 (peak) = 0.5385 USD → 3.8772 CNY
        self.assertEqual(c2a["requests"], 2)
        self.assertEqual(c2a["pricedRequests"], 2)
        self.assertEqual(c2a["peakRequests"], 1)
        self.assertEqual(c2a["costCny"], 3.8772)
        self.assertEqual(c2a["tokens"], {"input": 2000000, "output": 200000,
                                         "cache": 1000000, "reasoning": 0})
        self.assertEqual([m["model"] for m in c2a["models"]], ["deepseek-v4-flash"])
        self.assertEqual(c2a["models"][0]["costCny"], 3.8772)
        self.assertEqual(c2a["days"], [{"day": "2026-09-25", "requests": 2,
                                       "costCny": 3.8772}])

        # 既有破坏性 pop：响应里不再有 USD 小计（逐字段不变）。
        for service in payload["services"].values():
            self.assertNotIn("costUsd", service)
            for node in service["models"] + service["days"]:
                self.assertNotIn("cost_usd", node)
                self.assertNotIn("costUsd", node)

        dsh = payload["services"]["dsh"]
        self.assertEqual(dsh["requests"], 2)
        self.assertEqual(dsh["costCny"], round(1.5 * 7.2, 4))
        self.assertEqual(payload["dshMergedIntoTotal"]["day"], "2026-09-02")
        self.assertEqual(payload["dshMergedIntoTotal"]["costCny"], round(1.5 * 7.2, 4))

    def test_version_machinery_is_inert_on_the_old_shape(self):
        payload = self.summary()
        self.assertIs(payload["priceVersions"]["versioned"], False)
        self.assertEqual(payload["priceVersions"]["invalidCount"], 0)
        self.assertEqual(payload["priceVersions"]["versions"],
                         [{"versionId": None, "effectiveAt": None,
                           "exchangeRate": 7.2, "checkedAt": today_iso(),
                           "checkedStale": False}])
        c2a = payload["services"]["chat2api"]
        self.assertEqual(c2a["unknownVersionRequests"], 0)
        self.assertEqual(c2a["unknownUsageRequests"], 0)
        self.assertEqual(c2a["unpricedRequests"], 0)
        self.assertNotIn("costCnyPartial", c2a)


class VersionSwitchTest(PriceFixture):
    """价格切换前后：只有新记录按新版本计，历史金额不变。"""

    def setUp(self):
        super().setUp()
        self.write_prices(self.versioned_prices())

    def test_price_switch_applies_only_after_the_boundary(self):
        before = usage_service.estimate_record_with_version(
            "deepseek-v4-flash", TS_V1_ERA, 1000000, 0, 0, 0)
        after = usage_service.estimate_record_with_version(
            "deepseek-v4-flash", TS_V2_ERA, 1000000, 0, 0, 0)
        self.assertEqual(before, (0.22, True, "deepseek-v4-flash", False, "v1"))
        self.assertEqual(after, (0.44, True, "deepseek-v4-flash", False, "v2"))

    def test_appending_a_segment_does_not_recompute_history(self):
        rec = record("2026-09-25T00:00:00Z", input_tokens=1000000)
        before = usage_service.estimate_record_with_version(
            "deepseek-v4-flash", rec["_ms"], 1000000, 0, 0, 0)
        # 再加一段（更晚生效）：同一条历史记录逐字段不变。
        prices = self.versioned_prices()
        prices["versions"].append({
            "versionId": "v3", "effectiveAt": "2026-11-01T00:00:00Z",
            "exchangeRate": 9.0,
            "models": {"deepseek-v4-flash": {"cacheMiss": 9.9, "output": 9.9}},
        })
        self.write_prices(prices)
        after = usage_service.estimate_record_with_version(
            "deepseek-v4-flash", rec["_ms"], 1000000, 0, 0, 0)
        self.assertEqual(before, after)
        self.assertEqual(usage_service.version_rate(usage_service.load_price_table(),
                                                    rec["_ms"]), 7.2)

    def test_peak_rules_follow_the_version(self):
        # v1 峰价 0.44（01:00-04:00 UTC），v2 峰价 0.88 —— 同一墙钟小时
        # 在 v1 记峰、在 v2 也记峰，但价目与汇率都取各自版本。
        v1_peak = usage_service.estimate_record_with_version(
            "deepseek-v4-flash", TS_PEAK, 1000000, 0, 0, 0)
        v2_peak = usage_service.estimate_record_with_version(
            "deepseek-v4-flash", usage_service._parse_ts("2026-10-05T02:00:00Z"),
            1000000, 0, 0, 0)
        self.assertEqual(v1_peak, (0.44, True, "deepseek-v4-flash", True, "v1"))
        self.assertEqual(v2_peak, (0.88, True, "deepseek-v4-flash", True, "v2"))

    def _table_at(self, ts_ms):
        """load_price_table() with its clock pinned: the current view follows the
        wall clock, so an unpinned test flips when the real date crosses the
        fixture's v2 effectiveAt (2026-10-01)."""
        clock = mock.Mock(wraps=usage_service.time)
        clock.time.return_value = ts_ms / 1000
        with mock.patch.object(usage_service, "time", clock):
            return usage_service.load_price_table()

    def test_current_view_is_the_version_in_effect(self):
        table = self._table_at(TS_V1_ERA)
        self.assertIs(table["versioned"], True)
        self.assertEqual(table["versionMeta"]["count"], 2)
        self.assertEqual(table["versionMeta"]["invalidCount"], 0)
        # 现时刻（2026-09-25）生效的是 v1，顶层单值口径取它。
        self.assertEqual(table["versionMeta"]["current"]["versionId"], "v1")
        self.assertEqual(table["exchangeRate"], 7.2)
        self.assertEqual(table["peak"]["enabled"], True)
        self.assertEqual(len(table["tiers"]), 2)
        # 现时刻进入 v2 的生效段后，顶层视图随之切到 v2。
        self.assertEqual(self._table_at(TS_V2_ERA)["versionMeta"]["current"]["versionId"], "v2")


class VersionRateAggregationTest(PriceFixture):
    """汇率与峰谷按版本：_round_service 按记录所属版本换算再聚合。"""

    def setUp(self):
        super().setUp()
        self.write_prices(self.versioned_prices())

    def test_round_service_converts_per_version(self):
        records = [
            record("2026-09-25T00:00:00Z", input_tokens=1000000),   # v1 @7.2
            record("2026-10-05T00:00:00Z", input_tokens=1000000),   # v2 @8.0
        ]
        svc = usage_service._round_service(
            usage_service._bucket(records, "estimate",
                                 usage_service._bucket_price_estimate), 8.0)
        self.assertEqual(svc["costCny"], round(0.22 * 7.2 + 0.44 * 8.0, 4))
        self.assertEqual(svc["requests"], 2)
        self.assertEqual(svc["unknownVersionRequests"], 0)
        self.assertNotIn("costCnyPartial", svc)
        self.assertEqual(svc["days"], [
            {"day": "2026-09-25", "requests": 1, "costCny": round(0.22 * 7.2, 4)},
            {"day": "2026-10-05", "requests": 1, "costCny": round(0.44 * 8.0, 4)},
        ])

    def test_single_version_conversion_is_bit_identical(self):
        # 同一版本（单组汇率）：与既有口径逐位一致——服务级沿用先
        # round(usd, 6) 再乘汇率，模型/日级用未取整小计。
        records = [
            record("2026-09-25T00:00:00Z", input_tokens=333333, output_tokens=7777),
            record("2026-09-25T02:00:00Z", input_tokens=12345, output_tokens=99),
            record("2026-09-25T12:00:00Z", input_tokens=1, output_tokens=2),
        ]
        usd = sum(usage_service.estimate_record(
            "deepseek-v4-flash", rec["_ms"], rec["input_tokens"],
            rec["output_tokens"], 0, 0)[0] for rec in records)
        svc = usage_service._round_service(
            usage_service._bucket(records, "estimate",
                                 usage_service._bucket_price_estimate), 7.2)
        self.assertEqual(svc["costCny"], round(round(usd, 6) * 7.2, 4))
        self.assertEqual(svc["models"][0]["costCny"], round(usd * 7.2, 4))
        self.assertEqual(svc["days"][0]["costCny"], round(usd * 7.2, 4))

    def test_service_level_keeps_the_legacy_six_decimal_rounding(self):
        # 既有口径：服务级美元小计先 round(usd, 6) 再乘汇率，模型/日级不取整。
        # 3 条 input_tokens=10 的记录：usd=6.6e-06，先取整得 7e-06 → 0.0001，
        # 不取整则 4.752e-05 → 0.0。取整步骤在 4 位小数上可观测，必须保留。
        records = [record("2026-09-25T00:00:00Z", input_tokens=10)
                   for _ in range(3)]
        svc = usage_service._round_service(
            usage_service._bucket(records, "estimate",
                                 usage_service._bucket_price_estimate), 7.2)
        self.assertEqual(svc["costCny"], 0.0001)
        self.assertEqual(svc["models"][0]["costCny"], 0.0)

    def test_dsh_ledger_converts_with_the_day_version(self):
        self.write_dsh_snapshot({"2026-09-02": {
            "calls": 1, "cost": 2.0, "byProviderModel": {"p:m": {"calls": 1, "cost": 2.0}},
        }})
        dsh = usage_service.read_dsh_ledger()
        usage_service._round_service(dsh, 8.0)
        # 2026-09-02 属于 v1（7.2），不是现价 8.0。
        self.assertEqual(dsh["costCny"], round(2.0 * 7.2, 4))
        self.assertEqual(dsh["days"], [{"day": "2026-09-02", "requests": 1,
                                      "costCny": round(2.0 * 7.2, 4)}])


class VersionRateTest(PriceFixture):
    """六元组计价器（含汇率）：USD/版本与五元组同源，汇率随版本。

    分享计量的人民币折算只走这一条链：``estimate_record_with_version`` 的
    五元组契约保持不变（既有消费者不动），新增的六元组函数在同一金额与版本
    之上额外给出该记录所属版本的汇率；``read_share_usage`` 逐条按自己的汇率
    折算，跨版本时不是「USD 小计 × 现价」。
    """

    def test_rate_contract_matches_the_five_tuple_contract(self):
        for at_ms in (TS_OFF, TS_PEAK, TS_WEEKEND, TS_LEGACY, None):
            with self.subTest(at_ms=at_ms):
                five = usage_service.estimate_record_with_version(
                    "openwebui:DeepSeek-V4-Flash-0731", at_ms,
                    1000000, 100000, 500000, 0)
                six = usage_service.estimate_record_with_version_and_rate(
                    "openwebui:DeepSeek-V4-Flash-0731", at_ms,
                    1000000, 100000, 500000, 0)
                self.assertEqual(six[:5], five)
                # 旧形状只有恒生效的默认段：任何时刻（含缺失）都用它的汇率。
                self.assertEqual(six[5], 7.2)

    def test_versioned_rates_follow_the_record_segment(self):
        self.write_prices(self.versioned_prices())
        cases = {TS_V1_ERA: (0.22, "v1", 7.2),
                 TS_V2_ERA: (0.44, "v2", 8.0)}
        for at_ms, (cost, version_id, rate) in cases.items():
            with self.subTest(at_ms=at_ms):
                self.assertEqual(
                    usage_service.estimate_record_with_version_and_rate(
                        "deepseek-v4-flash", at_ms, 1000000, 0, 0, 0),
                    (cost, True, "deepseek-v4-flash", False, version_id, rate))
        # 时刻缺失/早于首段：版本未知 → 金额与汇率都未知（不折算、不猜现价）。
        for at_ms in (None,
                      usage_service._parse_ts("2026-01-01T00:00:00Z")):
            with self.subTest(at_ms=at_ms):
                self.assertEqual(
                    usage_service.estimate_record_with_version_and_rate(
                        "deepseek-v4-flash", at_ms, 1000000, 0, 0, 0),
                    (0.0, False, None, False, None, None))

    def test_read_share_usage_converts_each_record_at_its_own_rate(self):
        self.write_prices(self.versioned_prices())
        ledger = self.prices_path.parent / "usage-share-backend.jsonl"
        self.write_ledger(ledger, [
            # v1: 1,000,000 input × 0.22 USD → × 7.2
            record("2026-09-25T00:00:00Z", input_tokens=1000000,
                   estimated=False, key_id="k1", attribution="verified"),
            # v2: 1,000,000 input × 0.44 USD → × 8.0
            record("2026-10-05T00:00:00Z", input_tokens=1000000,
                   estimated=False, key_id="k1", attribution="verified"),
        ])
        reader, error = usage_service.share_usage_reader()
        self.assertIsNone(error)
        payload = reader(str(ledger),
                         usage_service.estimate_record_with_version_and_rate)
        key = payload["keys"][0]
        self.assertEqual(key["costUsd"], 0.66)
        # 逐条换算：0.22×7.2 + 0.44×8.0，不是 0.66×任一现价。
        self.assertEqual(key["knownCostCny"],
                         round(0.22 * 7.2 + 0.44 * 8.0, 4))
        self.assertEqual(key["costCny"], key["knownCostCny"])
        self.assertEqual(key["estimatedCostCny"], 0.0)
        self.assertFalse(key["costCnyPartial"])
        self.assertTrue(payload["coverage"]["rateFn"])
        self.assertFalse(payload["coverage"]["costCnyPartial"])


class UnknownStateTest(PriceFixture):
    """缺用量、缺价格、时间缺失/异常一律显式未知，不折 0。"""

    def setUp(self):
        super().setUp()
        self.write_prices(self.versioned_prices())

    def test_missing_or_bad_time_is_not_billed_at_the_new_price(self):
        for at_ms in (None, "2026-10-05T00:00:00Z"):
            with self.subTest(at_ms=at_ms):
                self.assertEqual(
                    usage_service.estimate_record_with_version(
                        "deepseek-v4-flash", at_ms, 1000000, 0, 0, 0),
                    (0.0, False, None, False, None))
                self.assertEqual(
                    usage_service.estimate_record(
                        "deepseek-v4-flash", at_ms, 1000000, 0, 0, 0),
                    (0.0, False, None, False))
        # 早于首段生效时刻：没有生效版本，同样是未知。
        self.assertEqual(
            usage_service.estimate_record_with_version(
                "deepseek-v4-flash", usage_service._parse_ts("2026-01-01T00:00:00Z"),
                1000000, 0, 0, 0),
            (0.0, False, None, False, None))

    def test_undated_records_are_counted_unknown(self):
        records = [
            record("2026-09-25T00:00:00Z", input_tokens=1000000),
            {"model": "deepseek-v4-flash", "_ms": None, "input_tokens": 1000000},
            record("2026-01-01T00:00:00Z", input_tokens=1000000),
        ]
        svc = usage_service._round_service(
            usage_service._bucket(records, "estimate",
                                 usage_service._bucket_price_estimate), 7.2)
        self.assertEqual(svc["requests"], 3)
        self.assertEqual(svc["unknownVersionRequests"], 2)
        self.assertEqual(svc["pricedRequests"], 1)
        self.assertEqual(svc["unpricedRequests"], 2)
        self.assertEqual(svc["costCny"], round(0.22 * 7.2, 4))
        self.assertEqual(svc["costCnyPartial"], True)
        # 时刻缺失的记录不进任何一天（不猜日期）；该日只有这一条记录，
        # 无法归属版本 → 该日金额未知并显式标注。
        self.assertEqual(svc["days"], [
            {"day": "2026-01-01", "requests": 1, "costCny": None,
             "costCnyPartial": True},
            {"day": "2026-09-25", "requests": 1, "costCny": round(0.22 * 7.2, 4)},
        ])

    def test_missing_usage_is_not_zero(self):
        records = [
            record("2026-09-25T00:00:00Z", input_tokens=1000000),  # 用量已知
            {"time": "2026-09-25T01:00:00Z", "model": "deepseek-v4-flash",
             "_ms": usage_service._parse_ts("2026-09-25T01:00:00Z")},  # 无任何用量字段
        ]
        svc = usage_service._round_service(
            usage_service._bucket(records, "estimate",
                                 usage_service._bucket_price_estimate), 7.2)
        self.assertEqual(svc["requests"], 2)
        self.assertEqual(svc["unknownUsageRequests"], 1)
        # 缺用量的请求不进 token 合计（0 会被读成真实测得值）。
        self.assertEqual(svc["tokens"], {"input": 1000000, "output": 0,
                                        "cache": 0, "reasoning": 0})
        self.assertEqual(svc["costCny"], round(0.22 * 7.2, 4))
        self.assertEqual(svc["costCnyPartial"], True)
        self.assertEqual(svc["models"][0]["unknown_usage"], 1)

    def test_explicit_zero_usage_stays_known(self):
        records = [record("2026-09-25T00:00:00Z", input_tokens=0, output_tokens=0,
                          cached_input_tokens=0, reasoning_tokens=0)]
        svc = usage_service._round_service(
            usage_service._bucket(records, "estimate",
                                 usage_service._bucket_price_estimate), 7.2)
        self.assertEqual(svc["unknownUsageRequests"], 0)
        self.assertEqual(svc["costCny"], 0.0)
        self.assertNotIn("costCnyPartial", svc)

    def test_unreported_cache_is_not_folded_into_zero(self):
        # Q11：上游没有报告缓存量时，不能把兼容字段 cached_input_tokens=0
        # 当成实测 0；金额按 cache-miss 上界计价并标 partial。
        records = [
            record("2026-09-25T00:00:00Z", input_tokens=1000000,
                   output_tokens=0, cached_input_tokens=0,
                   cache_report_status="unreported"),
            record("2026-09-25T23:00:00Z", input_tokens=1000000,
                   output_tokens=0, cached_input_tokens=500000,
                   cache_report_status="reported"),
        ]
        svc = usage_service._round_service(
            usage_service._bucket(records, "estimate",
                                 usage_service._bucket_price_estimate), 7.2)
        self.assertEqual(svc["cacheReportedRequests"], 1)
        self.assertEqual(svc["cacheUnreportedRequests"], 1)
        self.assertEqual(svc["cacheUnknownRequests"], 0)
        self.assertEqual(svc["cacheUnknownPricedRequests"], 1)
        # 只有已报告的 500000 进 cache 合计；未报告的 0 不是实测缓存 0。
        self.assertEqual(svc["tokens"]["cache"], 500000)
        # 0.22（未报告按 cache-miss 上界）+ 0.1135（已报告 50% 命中，谷价）。
        self.assertEqual(svc["costCny"],
                         round(round(0.3335, 6) * 7.2, 4))
        self.assertIs(svc["costCnyPartial"], True)

    def test_legacy_cache_rows_keep_old_amounts(self):
        # 旧记录没有 cache_report_status：按 unknown 分列、不推测缓存量，
        # 但计价仍用旧字段，历史金额不变。
        records = [record("2026-09-25T00:00:00Z", input_tokens=1000000,
                          output_tokens=0, cached_input_tokens=500000)]
        svc = usage_service._round_service(
            usage_service._bucket(records, "estimate",
                                 usage_service._bucket_price_estimate), 7.2)
        self.assertEqual(svc["cacheReportedRequests"], 0)
        self.assertEqual(svc["cacheUnreportedRequests"], 0)
        self.assertEqual(svc["cacheUnknownRequests"], 1)
        self.assertEqual(svc["cacheUnknownPricedRequests"], 0)
        self.assertEqual(svc["tokens"]["cache"], 500000)
        # 谷价 0.11 + 0.0035 = 0.1135 USD → 0.8172 CNY；旧字段计价不变。
        self.assertEqual(svc["costCny"], round(0.1135 * 7.2, 4))
        self.assertNotIn("costCnyPartial", svc)

    def test_unpriced_model_keeps_the_amount_unknown(self):
        records = [record("2026-09-25T00:00:00Z", model="GLM-OCR", input_tokens=10)]
        svc = usage_service._round_service(
            usage_service._bucket(records, "estimate",
                                 usage_service._bucket_price_estimate), 7.2)
        self.assertEqual(svc["pricedRequests"], 0)
        self.assertEqual(svc["unpricedRequests"], 1)
        self.assertEqual(svc["unknownVersionRequests"], 0)
        self.assertEqual(svc["unpricedModels"], ["GLM-OCR"])
        # 全部记录都无价：金额是未知，不是 0。
        self.assertIsNone(svc["costCny"])
        self.assertIs(svc["costCnyPartial"], True)

    def test_interrupted_stream_keeps_its_known_lower_bound(self):
        # 中断流：已记录的用量是已知下界，照常计入；无用量字段的中断流是未知。
        records = [
            record("2026-09-25T00:00:00Z", input_tokens=1000000,
                   output_tokens=10, status="client_disconnected"),
            record("2026-09-25T01:00:00Z", status="client_disconnected"),
        ]
        svc = usage_service._round_service(
            usage_service._bucket(records, "estimate",
                                 usage_service._bucket_price_estimate), 7.2)
        self.assertEqual(svc["requests"], 2)
        self.assertEqual(svc["unknownUsageRequests"], 1)
        self.assertEqual(svc["tokens"]["input"], 1000000)
        self.assertEqual(svc["tokens"]["output"], 10)
        usd = 0.22 + 10 * 0.66 / 1000000
        self.assertEqual(svc["costCny"], round(round(usd, 6) * 7.2, 4))

    def test_m365_pricing_callback_is_gone(self):
        # B0: the M365 source (and its failure-status price callback) is
        # retired with the UM-only assembly; only the chat2api estimator
        # remains, so a retired ledger can never reach the fee summary.
        self.assertFalse(hasattr(usage_service, "_bucket_price_m365"))
        self.assertFalse(hasattr(usage_service, "M365_LOG"))

    def test_versioned_table_without_usable_segment_prices_nothing(self):
        self.write_prices({
            "checkedAt": today_iso(), "exchangeRate": 7.2, "models": {},
            "versions": [{"versionId": "broken", "exchangeRate": 7.2,
                          "models": {"deepseek-v4-flash": dict(FLASH_V1)}}],
        })
        table = usage_service.load_price_table()
        self.assertIs(table["versioned"], True)
        self.assertEqual(table["versions"], [])
        self.assertEqual(table["versionMeta"]["invalidCount"], 1)
        self.assertEqual(table["tiers"], [])
        self.assertIsNone(table["versionMeta"]["current"])
        self.assertEqual(usage_service.resolve_price("deepseek-v4-flash", TS_V1_ERA),
                         (None, "flat", None, None))


class SummaryAcrossRestartTest(PriceFixture):
    """跨重启：版本解析是（文件 + 记录时刻）的纯函数，不靠进程内状态。"""

    def setUp(self):
        super().setUp()
        self.write_prices(self.versioned_prices())
        self.write_ledger(self.chat2api_log, [
            record("2026-09-25T00:00:00Z", input_tokens=1000000),
            record("2026-10-05T00:00:00Z", input_tokens=1000000),
        ])
        self.write_dsh_snapshot({})

    def test_same_payload_after_a_module_reload(self):
        first = self.summary()
        self.addCleanup(reload_usage_service)
        reload_usage_service()
        for attr, path in (("PRICE_FILE", self.prices_path),
                           ("OVERRIDE_FILE", self.override_path),
                           ("CHAT2API_LOG", self.chat2api_log),
                           ("DSH_SNAPSHOT_FILE", self.dsh_snapshot)):
            patcher = mock.patch.object(usage_service, attr, str(path))
            patcher.start()
            self.addCleanup(patcher.stop)
        second = self.summary()
        self.assertEqual(first["services"], second["services"])
        self.assertEqual(first["priceVersions"], second["priceVersions"])
        self.assertEqual(second["services"]["chat2api"]["costCny"],
                         round(0.22 * 7.2 + 0.44 * 8.0, 4))


class LegacyRecordTest(PriceFixture):
    """旧数据：峰谷时代前的记录按 legacyBase 计（既有口径不变）。"""

    def test_legacy_record_in_old_shape_and_versioned_shape(self):
        old = usage_service.estimate_record_with_version(
            "openwebui:DeepSeek-V4-Flash-0731", TS_LEGACY, 1000000, 100000, 500000, 0)
        self.assertEqual(old, (0.0994, True, "deepseek-v4-flash", True, None))
        self.write_prices(self.versioned_prices())
        new = usage_service.estimate_record_with_version(
            "openwebui:DeepSeek-V4-Flash-0731", TS_LEGACY, 1000000, 100000, 500000, 0)
        self.assertEqual(new, (0.0994, True, "deepseek-v4-flash", True, "v1"))


class ShareUsageMountTest(PriceFixture):
    """GET /api/share/usage：只读、走既有鉴权面、失败如实报错。"""

    def setUp(self):
        super().setUp()
        from share.secret_protection import write_private_secret_path
        write_private_secret_path(
            usage_service.CONTROL_TOKEN_FILE, b"test-control-token")
        self.server = usage_service.ThreadingHTTPServer(
            ("127.0.0.1", 0), usage_service.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path="/api/share/usage", method="GET", control_token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": "127.0.0.1:8001"}
        if control_token:
            headers["X-Admin-Control"] = control_token
        try:
            conn.request(method, path, headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            conn.close()

    def fake_share_metering(self, reader):
        module = types.ModuleType("share_metering")
        module.read_share_usage = reader
        return module

    def test_no_credentials_is_rejected(self):
        status, body = self.request()
        self.assertEqual(status, 401)
        self.assertIn("error", body)

    def test_write_is_not_mounted(self):
        # 只读：POST 不落到 reader，按未知路径处理。
        status, body = self.request(method="POST", control_token="test-control-token")
        self.assertEqual(status, 404)

    def test_missing_module_is_reported_not_faked_as_zero(self):
        with mock.patch.dict(sys.modules, {"share.metering": None}):
            status, body = self.request(control_token="test-control-token")
        self.assertEqual(status, 503)
        self.assertEqual(body, {"error": "share_metering_unavailable"})
        self.assertFalse(hasattr(usage_service, "share_metering"))

    def test_reader_is_called_with_the_versioned_price_fn(self):
        calls = {}

        def reader(ledger, price_fn=None, *, limit=200000):
            calls["ledger"] = ledger
            calls["limit"] = limit
            calls["price"] = price_fn
            return {"requests": 1, "keys": [], "unattributed": {},
                    "checkedAt": "2026-09-25T00:00:00+08:00",
                    "coverage": {"ledger_present": True}}

        with mock.patch.dict(sys.modules,
                             {"share.metering": self.fake_share_metering(reader)}), \
                mock.patch.object(usage_service, "SHARE_BACKEND_DIR_ENV",
                                  "CONSOLE_SHARE_BACKEND_DIR"), \
                mock.patch.object(usage_service, "read_key_labels",
                                  side_effect=AssertionError("management read")), \
                mock.patch.dict(os.environ,
                                {"CONSOLE_SHARE_BACKEND_DIR": str(self.prices_path.parent)}):
            status, body = self.request(control_token="test-control-token")
        self.assertEqual(status, 200)
        self.assertEqual(body["requests"], 1)
        self.assertEqual(calls["limit"], 200000)
        self.assertEqual(calls["ledger"],
                         str(self.prices_path.parent / "usage-share-backend.jsonl"))
        # price_fn 是版本化计价器（固定签名 6 元组：含该记录所属版本的汇率）。
        self.assertEqual(calls["price"],
                         usage_service.estimate_record_with_version_and_rate)
        price = calls["price"]("deepseek-v4-flash", TS_V1_ERA, 1000000, 0, 0, 0)
        self.assertEqual(price,
                         (0.22, True, "deepseek-v4-flash", False, None, 7.2))

    def test_reader_failure_is_reported_not_zeroed(self):
        def reader(ledger, price_fn=None, *, limit=200000):
            raise OSError("ledger unreadable")

        with mock.patch.dict(sys.modules,
                             {"share.metering": self.fake_share_metering(reader)}), \
                mock.patch.dict(os.environ,
                                {"CONSOLE_SHARE_BACKEND_DIR": str(self.prices_path.parent)}):
            status, body = self.request(control_token="test-control-token")
        self.assertEqual(status, 502)
        self.assertEqual(body, {"error": "share_usage_unavailable"})

    def test_unresolvable_ledger_path_is_reported(self):
        reader = lambda ledger, price_fn=None, limit=200000: {"requests": 0}
        with mock.patch.dict(sys.modules,
                             {"share.metering": self.fake_share_metering(reader)}), \
                mock.patch.dict(os.environ,
                                {"CONSOLE_SHARE_BACKEND_DIR": "relative/dir"}):
            status, body = self.request(control_token="test-control-token")
        self.assertEqual(status, 503)
        self.assertEqual(body, {"error": "share_ledger_path_unresolved"})


class ShareLedgerIsolationTest(PriceFixture):
    """分享账本不混入个人 /api/summary（只由来源清单隔离，此处加断言）。"""

    def test_share_ledger_never_reaches_the_personal_summary(self):
        share_dir = self.prices_path.parent / "share-backend"
        share_dir.mkdir()
        share_ledger = share_dir / "usage-share-backend.jsonl"
        self.write_ledger(share_ledger, [
            record("2026-09-25T00:00:00Z", input_tokens=9999999),
        ])
        self.write_ledger(self.chat2api_log, [
            record("2026-09-25T00:00:00Z", input_tokens=1000000),
        ])
        self.write_dsh_snapshot({})
        with mock.patch.dict(os.environ,
                             {"CONSOLE_SHARE_BACKEND_DIR": str(share_dir)}):
            self.assertEqual(usage_service.share_usage_ledger_path(), str(share_ledger))
            payload = self.summary()
        self.assertEqual(sorted(payload["services"]), ["chat2api", "dsh"])
        self.assertEqual(payload["services"]["chat2api"]["requests"], 1)
        self.assertEqual(payload["services"]["chat2api"]["tokens"]["input"], 1000000)
        self.assertNotIn(str(share_ledger), json.dumps(payload))


class SharedPricingWrapperTest(PriceFixture):
    """E19-1-04B：usagesrv 的同名价格规则是 shared.pricing_rules 的薄包装。"""

    CASES = (
        ("openwebui:DeepSeek-V4-Flash-0731", TS_OFF,
         1000000, 100000, 500000, 0),
        ("openwebui:DeepSeek-V4-Flash-0731", TS_PEAK,
         1000000, 100000, 500000, 0),
        ("gpt-5.6-luna", TS_OFF, 1000000, 0, 0, 0),
        ("unknown-model", TS_OFF, 100, 20, 0, 0),
    )

    def test_pricing_constants_are_shared_aliases(self):
        self.assertIs(usage_service.DEFAULT_EXCHANGE_RATE,
                      pricing_rules.DEFAULT_EXCHANGE_RATE)
        self.assertIs(usage_service.LEGACY_BASE_BOUNDARY,
                      pricing_rules.LEGACY_BASE_BOUNDARY)
        self.assertIs(usage_service.WEEKEND_OFFPEAK_AT,
                      pricing_rules.WEEKEND_OFFPEAK_AT)

    def test_estimate_record_matches_shared_explicit_table(self):
        table = usage_service.load_price_table()
        for case in self.CASES:
            with self.subTest(model=case[0], at_ms=case[1]):
                self.assertEqual(
                    usage_service.estimate_record(*case),
                    pricing_rules.estimate_record(*case, table))
                self.assertEqual(
                    usage_service.estimate_record_with_version(*case),
                    pricing_rules.estimate_record_detail(*case, table)[:5])
                self.assertEqual(
                    usage_service.estimate_record_with_version_and_rate(*case),
                    pricing_rules.estimate_record_detail(*case, table)[:6])

    def test_thin_wrappers_delegate_to_shared_rules(self):
        table = usage_service.load_price_table()
        self.assertEqual(usage_service._parse_ts("2026-09-25T12:00:00Z"),
                         pricing_rules.parse_ts("2026-09-25T12:00:00Z"))
        self.assertEqual(usage_service._beijing_day(TS_OFF),
                         pricing_rules.beijing_day(TS_OFF))
        self.assertEqual(usage_service._day_start_ms("2026-09-25"),
                         pricing_rules.day_start_ms("2026-09-25"))
        self.assertEqual(usage_service.canon_model_id("GLM-5.2 (go)"),
                         pricing_rules.canon_model_id("GLM-5.2 (go)"))
        self.assertEqual(usage_service.normalize_price(FLASH_V1),
                         pricing_rules.normalize_price(FLASH_V1))
        entry = pricing_rules.normalize_price(FLASH_V1)
        peak = table["peak"]
        tokens = {"input": 10, "output": 5, "cache": 3, "reasoning": 2}
        self.assertEqual(usage_service.tier_for(entry, TS_PEAK, peak),
                         pricing_rules.tier_for(entry, TS_PEAK, peak))
        self.assertEqual(usage_service.cost_of(tokens, entry, TS_PEAK, peak),
                         pricing_rules.cost_of(tokens, entry, TS_PEAK, peak))
        self.assertEqual(usage_service.version_at(table, TS_OFF),
                         pricing_rules.version_at(table, TS_OFF))
        self.assertEqual(usage_service.version_rate(table, TS_OFF),
                         pricing_rules.version_rate(table, TS_OFF))
        self.assertEqual(
            usage_service._current_version(
                table["versions"], table["versioned"], TS_OFF),
            pricing_rules._current_version(
                table["versions"], table["versioned"], TS_OFF))

    def test_host_wrappers_reach_shared_through_public_names(self):
        """E19-1-S2 step 4: the host wrapper segment calls public names only.

        ``usagesrv.py`` used to reach into ``shared.pricing_rules`` with the
        private spellings (``_rate_of``, ``_tiers_of``, ...) - another package's
        implementation detail. E19-1-01 published a public name for each of the
        ten rules as the SAME function object, so the rename cannot change a
        single computed value. This test reads the SOURCE: a runtime check cannot
        see which spelling a wrapper used once both are bound to one object.
        """
        source = USAGE_SERVICE_PATH.read_text(encoding="utf-8-sig")
        private_reaches = sorted(set(
            re.findall(r"_pricing_rules\.(_[a-z][a-z0-9_]*)", source)))
        self.assertEqual(
            private_reaches, [],
            "usagesrv must reach shared pricing through public names only; "
            "still private: {}".format(", ".join(private_reaches)))
        # The public name must be the same object the private spelling aliases,
        # so "renamed" cannot silently become "a second implementation".
        for public, private in (
                ("rate_of", "_rate_of"),
                ("tiers_of", "_tiers_of"),
                ("tokens_of", "_tokens_of"),
                ("strip_id_decor", "_strip_id_decor"),
                ("beijing_weekday", "_beijing_weekday"),
                ("checked_age_days", "_checked_age_days"),
                ("version_id", "_version_id"),
                ("price_version", "_price_version"),
                ("current_version", "_current_version"),
                ("peak_of", "_peak_of")):
            with self.subTest(public=public):
                self.assertIs(getattr(pricing_rules, public),
                              getattr(pricing_rules, private))

    def test_host_public_wrappers_keep_their_existing_names(self):
        """The host keeps its own ``_name`` wrappers for existing callers."""
        for name in ("_strip_id_decor", "_tokens_of", "_beijing_weekday",
                     "_tiers_of", "_rate_of", "_peak_of", "_checked_age_days",
                     "_version_id", "_price_version", "_current_version"):
            with self.subTest(wrapper=name):
                self.assertTrue(hasattr(usage_service, name),
                                "usagesrv must keep the {} wrapper".format(name))

    def test_match_model_id_delegates_to_shared_uncached_body(self):
        saved = dict(usage_service._match_cache)
        self.addCleanup(usage_service._match_cache.update, saved)
        self.addCleanup(usage_service._match_cache.clear)
        usage_service._match_cache.clear()
        cases = (
            ("DeepSeek-V4.1-Flash", ["DeepSeek-V4.1-Flash", "claude-x"]),
            ("GLM-5.2 (go)", ["glm-5.2", "glm-5.3"]),
            ("deepseek-v4-flash-128k", ["deepseek-v4-flash", "glm-5.3"]),
            ("glm-5", ["glm-5.2", "glm-5.3"]),
            ("k2-chat", ["k2"]),
            ("", ["k2"]),
        )
        for model_id, candidates in cases:
            with self.subTest(model=model_id):
                expected = pricing_rules.match_model_id_uncached(
                    model_id, list(candidates))
                self.assertEqual(
                    usage_service.match_model_id(model_id, list(candidates)),
                    expected)
                self.assertEqual(
                    usage_service._match_model_id_uncached(
                        model_id, list(candidates)),
                    expected)


class StatsEnvironmentWiringTest(unittest.TestCase):
    """E19-1-04B：usagesrv 构造并注入 console_stats.StatsEnvironment。"""

    def test_stats_environment_uses_host_constants(self):
        from console import stats as console_stats
        environment = usage_service.STATS_ENV
        self.assertIsInstance(environment, console_stats.StatsEnvironment)
        for name, expected in _STATS_ENV_PATHS.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(environment, name), expected)
        self.assertIs(environment.parse_ts, usage_service._parse_ts)
        self.assertIs(environment.beijing_day, usage_service._beijing_day)
        self.assertIs(environment.day_start_ms, usage_service._day_start_ms)
        self.assertIs(environment.display_path, usage_service._display_path)
        self.assertIs(environment.estimate_record,
                      usage_service.estimate_record)
        self.assertIs(environment.price_table_loader,
                      usage_service.load_price_table)

    def test_stats_call_points_resolve_the_installed_environment(self):
        from console import stats as console_stats
        installed = mock.Mock()
        saved = console_stats._STATS_ENVIRONMENT
        console_stats._STATS_ENVIRONMENT = installed
        self.addCleanup(setattr, console_stats, "_STATS_ENVIRONMENT", saved)
        self.assertIs(usage_service._stats_environment(), installed)

    def test_main_installs_the_process_environment(self):
        from console import stats as console_stats

        class FakeServer:
            def __init__(self, *args, **kwargs):
                pass

            def serve_forever(self):
                raise KeyboardInterrupt()

        policy = types.SimpleNamespace(enabled=False, error=None)
        saved_policy = usage_service.TAILNET_POLICY
        self.addCleanup(setattr, usage_service, "TAILNET_POLICY", saved_policy)
        with mock.patch.object(usage_service, "ThreadingHTTPServer",
                               FakeServer), \
                mock.patch.object(usage_service, "load_policy",
                                  lambda *args, **kwargs: policy), \
                mock.patch.object(usage_service, "session_bootstrap_code",
                                  lambda: None), \
                mock.patch.object(usage_service, "_start_summary_refresh_thread",
                                  lambda: None), \
                mock.patch.object(usage_service, "_console_build_identity_error",
                                  lambda: None), \
                mock.patch.object(console_stats, "configure") as configure:
            usage_service.main()
        configure.assert_called_once_with(usage_service.STATS_ENV)

    def test_console_manager_configures_broker_loader_before_seams(self):
        order = []
        adapters = types.ModuleType("console.adapters")
        adapters.wire_share_production_seams = (
            lambda **kwargs: order.append("wire"))
        adapters.build_registry = lambda **kwargs: {}
        operations = types.ModuleType("console.operations")
        manager = mock.Mock()
        operations.OperationManager = lambda **kwargs: manager
        production = types.ModuleType("console.share_production")
        production.configure_broker_loader = (
            lambda loader: order.append(("loader", loader)))

        saved = usage_service._console_manager
        usage_service._console_manager = None
        self.addCleanup(setattr, usage_service, "_console_manager", saved)
        with mock.patch.dict(sys.modules, {
                "console.adapters": adapters,
                "console.operations": operations,
                "console.share_production": production}), \
                mock.patch.object(usage_service, "get_share_manager",
                                  lambda: object()):
            result = usage_service.get_console_manager()
        self.assertIs(result, manager)
        self.assertEqual(order[0],
                         ("loader",
                          usage_service._load_share_management_deployment))
        self.assertEqual(order[1], "wire")


if __name__ == "__main__":
    unittest.main()
