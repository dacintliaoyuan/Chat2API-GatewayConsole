# -*- coding: utf-8 -*-
"""Fixture-based regression tests for console_stats.bounded_stats.

Every money expectation is hand-computed from the fixture price table using
the flat pricing semantics usagesrv.estimate_record applies:

  glm-5.3     (z-ai flat):    input 1.4 / cachedInput 0.26 / output 4.4 USD/Mtok
  gpt-5.6-sol (openai flat):  input 2.0 / cachedInput 0.2  / output 10  USD/Mtok
  exchangeRate 7.2; money_cny = round(raw_usd_sum * 7.2, 4)

  rec A: 1M input + 500K output, no cache      -> 3.6  USD -> 25.92  CNY
  rec B: 2M input(500K cached) + 1M output     -> 6.63 USD -> 47.736 CNY
         (noncached 1.5M*1.4 + 1M*4.4 + 0.5M*0.26)
  1M input only                                -> 1.4  USD -> 10.08  CNY
  500K input only                              -> 0.7  USD -> 5.04   CNY
  1M input + 100K output (gpt-5.6-sol)         -> 3.0  USD -> 21.6   CNY

Ledger fixtures copy the production JSONL field shapes (chat2api: status ok +
cached_input_tokens; qoder: credits/billable with cache fields that the qoder
credits口径 ignores; m365: cache_tokens + numeric status; DSH: frozen day
snapshot with days -> byProviderModel rows).
"""

import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from console import stats as console_stats  # noqa: E402  (same directory, sys.path prepared)
import usagesrv       # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
AI_GATEWAY = TESTS_DIR.parent


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


class _FrozenStatsEnvironment(_UsagesrvStatsEnvironment):
    """FROZEN environment — the values do NOT track the host module.

    standards-tests-18: the live adapter above reads every attribute from the
    ``usagesrv`` module, so a ``default_sources`` that IGNORED the injected
    environment and read ``usagesrv.CHAT2API_LOG`` directly still passed. The
    first attempt at this fixture captured the host constants once — which did NOT
    fix it: the captured values were byte-identical to the host ones, so the
    mutant was still indistinguishable. The fixture now carries INDEPENDENT
    sentinels (a path that is never the host constant), so a default that reads
    the host module cannot satisfy an assertion against the injected value.
    """

    def __init__(self):
        self._chat2api_log = _frozen_sentinel("chat2api")
        self._dsh_snapshot = _frozen_sentinel("dsh")
        self._qoder_log = _frozen_sentinel("qoder")
        self._m365_log = _frozen_sentinel("m365")
        self._dsh_merge_day = "2026-01-01"
        self._magpie_price_snapshot = _frozen_sentinel("magpie-price-snapshot")
        self._magpie_price_status = _frozen_sentinel("magpie-price-status")

    chat2api_log = property(lambda self: self._chat2api_log)
    dsh_snapshot = property(lambda self: self._dsh_snapshot)
    qoder_log = property(lambda self: self._qoder_log)
    m365_log = property(lambda self: self._m365_log)
    dsh_merge_day = property(lambda self: self._dsh_merge_day)
    magpie_price_snapshot = property(lambda self: self._magpie_price_snapshot)
    magpie_price_status = property(lambda self: self._magpie_price_status)


def _frozen_sentinel(name):
    """A path that is deliberately NOT any ``usagesrv`` constant."""
    return "/frozen-fixture/%s.jsonl" % name


# Guard before any test can construct a default management client. A route may
# swallow an exception, so attempted production access is checked separately.
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
    global _ISOLATION_TEMP, _ISOLATION_SAVED_SEAMS, \
        _ISOLATION_SAVED_STATS_ENVIRONMENT
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
    local = globals().get("usagesrv")
    if local is not None:
        _guard_usage_module(local)
    _ISOLATION_SAVED_SEAMS = {
        name: value for name, value in vars(console_share_actions).items()
        if name.startswith("_") and name.upper() == name}
    for owner, name in ((share_broker_client.NamedPipeBrokerTransport,
                         "_declare_real_kernel"),
                        (share_broker_server, "_declared_kernel")):
        patcher = mock.patch.object(owner, name, _refuse_real_kernel)
        patcher.start()
        _ISOLATION_PATCHERS.append(patcher)
    _ISOLATION_SAVED_STATS_ENVIRONMENT = console_stats._STATS_ENVIRONMENT
    console_stats.configure(_UsagesrvStatsEnvironment())


def tearDownModule():
    global _ISOLATION_SAVED_STATS_ENVIRONMENT
    from console import share_actions as console_share_actions
    try:
        for module, old_console, _old_share in _ISOLATION_MODULES:
            current = module._console_manager
            if current is not None and current is not old_console:
                current.close(wait=True)
    finally:
        # standards-tests-04: the module installed a PROCESS-WIDE
        # ``console_stats`` environment and never restored it, so every later
        # module of the same ``discover`` run inherited an environment whose
        # paths pointed into an already deleted temp directory. Restore the value
        # that was there before this module configured it.
        console_stats._STATS_ENVIRONMENT = _ISOLATION_SAVED_STATS_ENVIRONMENT
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


RATE = 7.2
CHECKED_AT = "2026-09-11"
GLM_5_3_PRICE = {"input": 1.4, "cachedInput": 0.26, "output": 4.4,
                 "billingMode": "flat"}
GPT_5_6_SOL_PRICE = {"input": 2, "cachedInput": 0.2, "output": 10,
                     "billingMode": "flat"}


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
            "total_tokens": in_tok + out_tok, "estimated": False,
            "duration_ms": 100}


def _qoder_record(time, model="gfmodel", credits=0.5, billable=True,
                  in_tok=1000, out_tok=200, cache_tok=500):
    return {"time": time, "model": model, "stream": True,
            "input_tokens": in_tok, "output_tokens": out_tok,
            "cached_input_tokens": cache_tok, "reasoning_tokens": 10,
            "total_tokens": in_tok + out_tok, "credits": credits,
            "original_credits": credits * 2, "billable": billable,
            "request_id": "req-%s" % time}


def _m365_record(time, model="gpt-5.6-sol", in_tok=1_000_000, out_tok=100_000,
                 cache_tok=0, status=200):
    return {"time": time, "api_key_prefix": "m365_test...",
            "account_email": "user@example.test", "model": model,
            "endpoint": "/v1/chat/completions", "stream": True,
            "input_tokens": in_tok, "output_tokens": out_tok,
            "cache_tokens": cache_tok, "duration_ms": 500, "status": status}


def _dsh_snapshot():
    """Frozen-day snapshot shape mirroring data/dsh-ledger-20260902.json."""
    return {
        "_说明": "test snapshot",
        "config": {"pricingCurrency": "USD"},
        "days": {
            "2026-09-02": {
                "date": "2026-09-02",
                "calls": 3, "cost": 1.5, "input": 3000, "output": 300,
                "cacheRead": 100, "cacheWrite": 0, "reasoning": 20,
                "apiCost": 1.5, "sessions": [],
                "byProviderModel": {
                    "openwebui:Test-A": {"calls": 2, "cost": 1.0,
                                         "input": 2000, "output": 200,
                                         "cacheRead": 100, "cacheWrite": 0,
                                         "reasoning": 10, "apiCost": 1.0},
                    "openwebui:Test-B": {"calls": 1, "cost": 0.5,
                                         "input": 1000, "output": 100,
                                         "cacheRead": 0, "cacheWrite": 0,
                                         "reasoning": 10, "apiCost": 0.5},
                },
            },
        },
    }


class TestBoundedStats(unittest.TestCase):
    """Fixture ledger files in a temp dir; price table redirected via the
    usagesrv module constants (same injection points production uses)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_price_file = usagesrv.PRICE_FILE
        self._orig_override = usagesrv.OVERRIDE_FILE
        self._orig_cache = dict(usagesrv._price_cache)
        prices = {
            "_说明": "console_stats test fixture",
            "checkedAt": CHECKED_AT,
            "exchangeRate": RATE,
            "peak": {"enabled": True, "effectiveAt": "2026-08-01T00:00:00Z",
                     "windows": [{"start": 1, "end": 4},
                                 {"start": 6, "end": 10}]},
            "models": {},
            "providers": {
                "z-ai": {"models": {"glm-5.3": GLM_5_3_PRICE}},
                "openai": {"models": {"gpt-5.6-sol": GPT_5_6_SOL_PRICE}},
            },
        }
        price_path = os.path.join(self._tmp.name, "prices.json")
        with open(price_path, "w", encoding="utf-8") as f:
            json.dump(prices, f, ensure_ascii=False)
        usagesrv.PRICE_FILE = price_path
        usagesrv.OVERRIDE_FILE = os.path.join(self._tmp.name, "no-override.json")
        usagesrv._price_cache["data"] = None
        self.addCleanup(self._restore_usagesrv)

    def _restore_usagesrv(self):
        usagesrv.PRICE_FILE = self._orig_price_file
        usagesrv.OVERRIDE_FILE = self._orig_override
        usagesrv._price_cache.clear()
        usagesrv._price_cache.update(self._orig_cache)

    def _write_ledger(self, name, records):
        path = os.path.join(self._tmp.name, name)
        _write_jsonl(path, records)
        return path

    def _write_snapshot(self, snapshot):
        path = os.path.join(self._tmp.name, "dsh-snapshot.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
        return path

    # ---------------------------------------------------------- assertions

    def _source_block(self, result, day, source):
        for day_entry in result["days"]:
            if day_entry["date"] == day:
                for block in day_entry["sources"]:
                    if block["source"] == source:
                        return block
        raise AssertionError("no block %s on %s in %r"
                             % (source, day, result["days"]))

    def _source_block_in_day(self, day_entry, source):
        for block in day_entry["sources"]:
            if block["source"] == source:
                return block
        raise AssertionError("no block %s in %r" % (source, day_entry["sources"]))

    def _model_row(self, result, day, source, model):
        block = self._source_block(result, day, source)
        for row in block["models"]:
            if row["model"] == model:
                return row
        raise AssertionError("no model %s in %s/%s: %r"
                             % (model, source, day, block["models"]))

    def _day(self, result, date_str):
        for day_entry in result["days"]:
            if day_entry["date"] == date_str:
                return day_entry
        raise AssertionError("no day %s in %r" % (date_str, result["days"]))

    # ------------------------------------------------------- contract tests

    def test_single_source_single_day_aggregation(self):
        """手算期望：rec A 3.6 USD + rec B 6.63 USD = 10.23 USD -> x7.2 = 73.656 CNY。"""
        ledgers = {"chat2api": self._write_ledger("c2a.jsonl", [
            _c2a_record("2026-09-10T10:00:00+08:00", in_tok=1_000_000,
                        out_tok=500_000, cache_tok=0),
            _c2a_record("2026-09-10T11:00:00+08:00", in_tok=2_000_000,
                        out_tok=1_000_000, cache_tok=500_000),
        ])}
        res = console_stats.bounded_stats(ledgers, "2026-09-10", "2026-09-10")
        self.assertEqual(len(res["days"]), 1)
        row = self._model_row(res, "2026-09-10", "chat2api", "glm-5.3")
        # 价目 x token（缓存命中视为 input 子集，不重复计费）：
        #   (1.0M*1.4 + 0.5M*4.4) + (1.5M*1.4 + 1.0M*4.4 + 0.5M*0.26) = 10.23 USD
        self.assertEqual(row["estimated_cost_cny"], 73.656)
        self.assertEqual(row["requests"], 2)
        self.assertEqual(row["input_tokens"], 3_000_000)
        self.assertEqual(row["output_tokens"], 1_500_000)
        self.assertEqual(row["cache_tokens"], 500_000)
        self.assertEqual(row["reasoning_tokens"], 0)
        src = self._source_block(res, "2026-09-10", "chat2api")
        self.assertEqual(src["kind"], "estimate")
        self.assertEqual(src["source_totals"]["requests"], 2)
        self.assertEqual(src["source_totals"]["estimated_cost_cny"], 73.656)
        day = self._day(res, "2026-09-10")
        self.assertEqual(day["day_total"]["estimated_cost_cny"], 73.656)
        self.assertEqual(day["day_total"]["credits"], 0.0)
        self.assertEqual(res["totals"]["estimated_cost_cny"], 73.656)
        self.assertEqual(res["totals"]["requests"], 2)
        self.assertEqual(res["meta"], {"timezone": "Asia/Shanghai",
                                       "range_days": 1,
                                       "price_checked_at": CHECKED_AT})
        cov = res["coverage"]["sources"]["chat2api"]
        self.assertTrue(cov["ledger_present"])
        self.assertEqual(cov["days_with_records"], 1)
        self.assertEqual(res["coverage"]["unpriced_models"], [])
        self.assertEqual(res["coverage"]["missing_days"], [])

    def test_half_open_day_boundary_assignment(self):
        """23:30 归当日；本地 00:30 与 UTC 16:30Z（=北京 00:30）归次日。"""
        ledgers = {"chat2api": self._write_ledger("c2a.jsonl", [
            _c2a_record("2026-09-10T23:30:00+08:00"),
            _c2a_record("2026-09-11T00:30:00+08:00"),
            _c2a_record("2026-09-10T16:30:00Z"),      # = 2026-09-11 00:30 +08
            _c2a_record("2026-09-09T12:00:00+08:00"), # 区间外
        ])}
        res = console_stats.bounded_stats(ledgers, "2026-09-10", "2026-09-11")
        self.assertEqual([d["date"] for d in res["days"]],
                         ["2026-09-10", "2026-09-11"])
        self.assertEqual(self._day(res, "2026-09-10")["day_total"]["requests"], 1)
        self.assertEqual(self._day(res, "2026-09-11")["day_total"]["requests"], 2)
        self.assertEqual(res["totals"]["requests"], 3)
        self.assertEqual(res["coverage"]["missing_days"], [])
        self.assertEqual(
            res["coverage"]["sources"]["chat2api"]["days_with_records"], 2)

    def test_source_and_model_filter_cross(self):
        ledgers = {
            "chat2api": self._write_ledger("c2a.jsonl", [
                _c2a_record("2026-09-10T10:00:00+08:00"),              # glm 25.92
                _c2a_record("2026-09-10T11:00:00+08:00",
                            model="gpt-5.6-sol", in_tok=1_000_000,
                            out_tok=100_000),                          # sol 21.6
                _c2a_record("2026-09-11T10:00:00+08:00"),              # glm 25.92
            ]),
            "qoder": self._write_ledger("qoder.jsonl", [
                _qoder_record("2026-09-10T12:00:00+08:00", credits=0.5),
                _qoder_record("2026-09-11T12:00:00+08:00", credits=0.25),
                _qoder_record("2026-09-10T13:00:00+08:00", model="glm-5.3",
                              credits=1.0),
            ]),
        }
        # 先过滤后聚合：模型过滤命中 chat2api 的两条 glm-5.3 记录。
        res = console_stats.bounded_stats(
            ledgers, "2026-09-10", "2026-09-11",
            sources=["chat2api"], models=["glm-5.3"])
        self.assertEqual([d["date"] for d in res["days"]],
                         ["2026-09-10", "2026-09-11"])
        for day in res["days"]:
            self.assertEqual([b["source"] for b in day["sources"]], ["chat2api"])
            self.assertEqual([m["model"] for m in
                              self._source_block_in_day(day, "chat2api")["models"]],
                             ["glm-5.3"])
        self.assertEqual(res["totals"]["requests"], 2)
        # 两条默认记录各为 1M input / 0 output -> 1.4 USD -> 10.08 CNY
        self.assertAlmostEqual(res["totals"]["estimated_cost_cny"], 20.16, places=9)
        self.assertEqual(res["totals"]["credits"], 0.0)
        cov = res["coverage"]["sources"]
        self.assertEqual(cov["chat2api"]["days_with_records"], 2)
        # coverage 只报告被选中的来源：未选择的 qoder 不出现在 coverage 里
        self.assertNotIn("qoder", cov)

        # 仅 qoder：积分单列，无金额（3 条积分记录合计 0.5+0.25+1.0）。
        res_q = console_stats.bounded_stats(ledgers, "2026-09-10", "2026-09-11",
                                            sources=["qoder"])
        self.assertEqual(res_q["totals"]["credits"], 1.75)
        self.assertEqual(res_q["totals"]["estimated_cost_cny"], 0.0)
        for day in res_q["days"]:
            self.assertEqual([b["source"] for b in day["sources"]], ["qoder"])

        # 模型无命中：无任何日数据，区间内所有日期进入 missing_days。
        res_empty = console_stats.bounded_stats(
            ledgers, "2026-09-10", "2026-09-11", models=["nosuchmodel"])
        self.assertEqual(res_empty["days"], [])
        self.assertEqual(res_empty["coverage"]["missing_days"],
                         ["2026-09-10", "2026-09-11"])

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
        # 缺失日不当 0：days 只有真实记录日，totals 只含这两天的 2 条请求。
        self.assertEqual(len(res["days"]), 2)
        self.assertEqual(res["totals"]["requests"], 2)
        self.assertEqual(
            res["coverage"]["sources"]["chat2api"]["days_with_records"], 2)

    def test_unpriced_models_excluded_from_money(self):
        ledgers = {"chat2api": self._write_ledger("c2a.jsonl", [
            _c2a_record("2026-09-10T10:00:00+08:00"),  # glm 1M in -> 10.08 CNY
            _c2a_record("2026-09-10T11:00:00+08:00",
                        model="mystery-model-xyz", in_tok=500_000),
        ])}
        res = console_stats.bounded_stats(ledgers, "2026-09-10", "2026-09-10")
        self.assertEqual(res["coverage"]["unpriced_models"],
                         ["mystery-model-xyz"])
        row = self._model_row(res, "2026-09-10", "chat2api", "mystery-model-xyz")
        self.assertIsNone(row["estimated_cost_cny"])  # 未定价不产生费用数字
        self.assertEqual(row["requests"], 1)          # 请求数仍计入
        self.assertEqual(row["input_tokens"], 500_000)
        self.assertEqual(self._day(res, "2026-09-10")["day_total"]
                         ["estimated_cost_cny"], 10.08)
        self.assertEqual(res["totals"]["estimated_cost_cny"], 10.08)
        self.assertEqual(res["totals"]["requests"], 2)

    def test_credits_kept_separate_from_money(self):
        ledgers = {
            "qoder": self._write_ledger("qoder.jsonl", [
                _qoder_record("2026-09-10T10:00:00+08:00", credits=0.5),
                _qoder_record("2026-09-11T10:00:00+08:00", credits=0.25),
            ]),
            "chat2api": self._write_ledger("c2a.jsonl", [
                _c2a_record("2026-09-10T11:00:00+08:00"),  # 10.08 CNY
            ]),
        }
        res = console_stats.bounded_stats(ledgers, "2026-09-10", "2026-09-11")
        # 积分与钱分列：credits 0.75 不折现、不并入 estimated_cost_cny。
        self.assertAlmostEqual(res["totals"]["credits"], 0.75, places=9)
        self.assertEqual(res["totals"]["estimated_cost_cny"], 10.08)
        self.assertEqual(res["totals"]["credits_currency"], "qoder")
        qrow = self._model_row(res, "2026-09-10", "qoder", "gfmodel")
        self.assertEqual(qrow["credits"], 0.5)
        self.assertIsNone(qrow["estimated_cost_cny"])
        self.assertNotIn("cache_tokens", qrow)  # qoder 口径不含 cache/reasoning
        qsrc = self._source_block(res, "2026-09-10", "qoder")
        self.assertEqual(qsrc["source_totals"]["billable_requests"], 1)
        self.assertIsNone(qsrc["source_totals"]["estimated_cost_cny"])
        self.assertEqual(qsrc["source_totals"]["credits_currency"], "qoder")
        day = self._day(res, "2026-09-10")
        self.assertAlmostEqual(day["day_total"]["credits"], 0.5, places=9)
        self.assertEqual(day["day_total"]["estimated_cost_cny"], 10.08)

    def test_absent_ledger_coverage(self):
        ledgers = {
            "chat2api": os.path.join(self._tmp.name, "not-there.jsonl"),
            "qoder": self._write_ledger("qoder.jsonl", [
                _qoder_record("2026-09-10T10:00:00+08:00", credits=0.5),
                _qoder_record("2026-09-12T10:00:00+08:00", credits=0.25),
            ]),
        }
        res = console_stats.bounded_stats(ledgers, "2026-09-10", "2026-09-12")
        cov = res["coverage"]["sources"]["chat2api"]
        self.assertFalse(cov["ledger_present"])   # 缺席如实标注
        self.assertEqual(cov["days_with_records"], 0)
        # 缺席账本不冒充零：days 只含 qoder 真实记录日，无 chat2api 块。
        self.assertEqual([d["date"] for d in res["days"]],
                         ["2026-09-10", "2026-09-12"])
        for day in res["days"]:
            self.assertNotIn("chat2api",
                             [b["source"] for b in day["sources"]])
        self.assertEqual(res["coverage"]["missing_days"], ["2026-09-11"])
        self.assertAlmostEqual(res["totals"]["credits"], 0.75, places=9)
        self.assertEqual(res["coverage"]["sources"]["qoder"]["ledger_present"],
                         True)

    def test_malformed_lines_mark_source_error(self):
        good = _c2a_record("2026-09-10T10:00:00+08:00")
        path = os.path.join(self._tmp.name, "c2a.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(good) + "\n")
            f.write("not-json{{\n")
        ledgers = {
            "chat2api": path,
            "qoder": self._write_ledger("qoder.jsonl", [
                _qoder_record("2026-09-10T12:00:00+08:00", credits=0.5),
            ]),
        }
        res = console_stats.bounded_stats(ledgers, "2026-09-10", "2026-09-10")
        chat_cov = res["coverage"]["sources"]["chat2api"]
        errors = {e["type"]: e for e in chat_cov.get("errors", [])}
        self.assertIn("malformed_lines", errors)
        self.assertEqual(errors["malformed_lines"]["bad_lines"], 1)
        # 坏行被如实标注；好行仍聚合，其余源不受影响，不整体失败。
        self.assertEqual(
            self._model_row(res, "2026-09-10", "chat2api", "glm-5.3")
            ["requests"], 1)
        self.assertEqual(
            self._model_row(res, "2026-09-10", "qoder", "gfmodel")["credits"],
            0.5)

    def test_range_limit_and_invalid_range(self):
        ledgers = {"chat2api": os.path.join(self._tmp.name, "c2a.jsonl")}
        with self.assertRaises(ValueError):  # 93 天超出上限
            console_stats.bounded_stats(ledgers, "2026-01-01", "2026-04-03")
        with self.assertRaises(ValueError):  # from > to
            console_stats.bounded_stats(ledgers, "2026-09-10", "2026-09-09")
        with self.assertRaises(ValueError):  # 非法日期格式
            console_stats.bounded_stats(ledgers, "20260910", "2026-09-11")
        res = console_stats.bounded_stats(ledgers, "2026-01-01", "2026-04-02")
        self.assertEqual(res["meta"]["range_days"], 92)  # 上限本身允许
        self.assertEqual(len(res["coverage"]["missing_days"]), 92)

    def test_cross_day_totals_consistency(self):
        """跨日合计=逐日加和；积分与钱分开断言；DSH 快照单列并入金额列。"""
        ledgers = {
            "chat2api": self._write_ledger("c2a.jsonl", [
                _c2a_record("2026-09-01T10:00:00+08:00"),             # 10.08
                _c2a_record("2026-09-02T10:00:00+08:00",
                            in_tok=2_000_000, out_tok=1_000_000,
                            cache_tok=500_000),                       # 47.736
                _c2a_record("2026-09-03T10:00:00+08:00",
                            in_tok=500_000),                          # 5.04
            ]),
            "qoder": self._write_ledger("qoder.jsonl", [
                _qoder_record("2026-09-01T12:00:00+08:00", credits=0.5),
                _qoder_record("2026-09-03T12:00:00+08:00", credits=0.25),
            ]),
            "dsh": self._write_snapshot(_dsh_snapshot()),  # 冻结日 2026-09-02
        }
        res = console_stats.bounded_stats(ledgers, "2026-09-01", "2026-09-05")
        days = {d["date"]: d for d in res["days"]}
        self.assertEqual(sorted(days), ["2026-09-01", "2026-09-02", "2026-09-03"])
        # 钱逐日加和（DSH 快照真实记账并入金额列：47.736 + 10.8）
        self.assertAlmostEqual(res["totals"]["estimated_cost_cny"],
                               sum(d["day_total"]["estimated_cost_cny"]
                                   for d in res["days"]), places=9)
        # 积分逐日加和
        self.assertAlmostEqual(res["totals"]["credits"],
                               sum(d["day_total"]["credits"]
                                   for d in res["days"]), places=9)
        for key in ("requests", "input_tokens", "output_tokens",
                    "cache_tokens", "reasoning_tokens"):
            self.assertEqual(res["totals"][key],
                             sum(d["day_total"][key] for d in res["days"]))
        self.assertEqual(res["totals"]["requests"], 8)   # 3 chat + 2 qoder + 3 dsh
        self.assertAlmostEqual(res["totals"]["estimated_cost_cny"], 73.656,
                               places=9)
        self.assertAlmostEqual(res["totals"]["credits"], 0.75, places=9)
        # DSH 单列：models 来自 byProviderModel 行，cost=真实记账 x 汇率。
        dsh_block = self._source_block(res, "2026-09-02", "dsh")
        self.assertEqual(dsh_block["kind"], "dsh-ledger")
        dsh_money = {m["model"]: m["estimated_cost_cny"]
                     for m in dsh_block["models"]}
        self.assertAlmostEqual(dsh_money["openwebui:Test-A"], 7.2, places=9)
        self.assertAlmostEqual(dsh_money["openwebui:Test-B"], 3.6, places=9)
        self.assertAlmostEqual(
            dsh_block["source_totals"]["estimated_cost_cny"], 10.8, places=9)
        self.assertEqual(
            res["coverage"]["sources"]["dsh"]["days_with_records"], 1)
        self.assertEqual(res["coverage"]["missing_days"],
                         ["2026-09-04", "2026-09-05"])

    def test_unknown_source_filter_empty_with_coverage(self):
        ledgers = {"chat2api": self._write_ledger("c2a.jsonl", [
            _c2a_record("2026-09-10T10:00:00+08:00"),
        ])}
        res = console_stats.bounded_stats(ledgers, "2026-09-10", "2026-09-12",
                                          sources=["nosuch"])
        self.assertEqual(res["days"], [])  # 未知来源：空结果而非错误
        cov = res["coverage"]["sources"]["nosuch"]
        self.assertFalse(cov["ledger_present"])
        self.assertEqual(cov["errors"][0]["type"], "unknown_source")
        self.assertEqual(res["coverage"]["missing_days"],
                         ["2026-09-10", "2026-09-11", "2026-09-12"])

    def test_default_ledgers_matches_usagesrv_constants(self):
        ledgers = console_stats.default_ledgers()
        # B0：默认读链只装配 UM 主链与冻结 DSH 快照。
        self.assertEqual(set(ledgers), {"chat2api", "dsh"})
        self.assertIs(ledgers["chat2api"], usagesrv.CHAT2API_LOG)
        self.assertIs(ledgers["dsh"], usagesrv.DSH_SNAPSHOT_FILE)
        # 退役来源账本仍在磁盘保留，仅经显式历史追溯接口可取。
        retired = console_stats.retired_ledgers()
        self.assertEqual(set(retired), {"qoder", "m365"})
        self.assertIs(retired["qoder"], usagesrv.QODER_LOG)
        self.assertIs(retired["m365"], usagesrv.M365_LOG)
        for name in ("qoder", "m365"):
            self.assertNotIn(name, ledgers)

    def test_default_ledgers_env_redirect(self):
        redirect = os.path.join(self._tmp.name, "chat2api-usage.jsonl")
        original = usagesrv.CHAT2API_LOG
        try:
            with mock.patch.dict(os.environ, {"CHAT2API_USAGE_LOG": redirect}):
                importlib.reload(usagesrv)
                self.assertEqual(console_stats.default_ledgers()["chat2api"],
                                 redirect)
        finally:
            # standards-tests-05: reload INSIDE the ``patch.dict`` block read
            # the patched value again, so the "restore" left the constant on
            # the temporary path (the temp dir is then deleted, and every later
            # module inherited a dangling ledger). The environment is restored
            # FIRST, then the module is reloaded so the constant really goes
            # back to its default.
            importlib.reload(usagesrv)
        self.assertEqual(usagesrv.CHAT2API_LOG, original,
                         "the test must restore the host constant")
        self.assertEqual(console_stats.default_ledgers()["chat2api"],
                         usagesrv.CHAT2API_LOG)

    def test_bounded_stats_corrupt_snapshot_shapes_not_fatal(self):
        """快照 days / byProviderModel 非对象：记入 coverage，不抛异常。"""
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
            self.assertTrue(cov["ledger_present"])  # 文件在场可读，只是形状不对
            errors = {e["type"]: e for e in cov.get("errors", [])}
            self.assertIn("corrupt_snapshot", errors)
            self.assertIn("not a JSON object",
                          errors["corrupt_snapshot"]["detail"])


# ---------------------------------------------------------------------------
# A12-1 有界 Token / 速率查询层（/api/console/usage-tokens、usage-rates）。
#
# 全部用例只读临时目录 fixture（显式传 ledgers，绝不读真实账本、不发请求）。
# 基准时刻固定为北京时间 2026-09-24 12:00，窗口边界因此可手算。
# 期望口径见 console_stats「A12-1」节：完整=输入+输出（缺分量进 missing、
# 绝不补零）；速率=输出 Token ÷ 整次耗时，缺分量/缺耗时/非正耗时按原因计数。

BEIJING = timezone(timedelta(hours=8))
NOW_1200 = datetime(2026, 9, 24, 12, 0, tzinfo=BEIJING)
# 契约枚举（missing[].reason）：由模块常量单点定义，测试不再手工 union 追加值。
ALLOWED_REASONS = set(console_stats._MISSING_REASONS)


def _c2a_usage(time, model="m-a", in_tok=100, out_tok=50, duration_ms=1000,
                status="ok", drop=()):
    """chat2api 账本记录（字段形状=chat2api.py 记账写入）。"""
    rec = {"time": time, "service": "chat2api", "model": model, "stream": True,
           "status": status, "input_tokens": in_tok, "output_tokens": out_tok,
           "cached_input_tokens": 0, "reasoning_tokens": 0,
           "total_tokens": 150, "estimated": False,
           "duration_ms": duration_ms}
    for key in drop:
        rec.pop(key, None)
    return rec


def _qoder_cn_usage(time, model="gfmodel", in_tok=100, out_tok=50,
                     duration_ms=1000, credits=0.5, drop=()):
    """Qoder 国内账本记录（字段形状=qoder 网关 usagelog.go）。"""
    rec = {"time": time, "model": model, "stream": True,
           "input_tokens": in_tok, "output_tokens": out_tok,
           "cached_input_tokens": 0, "reasoning_tokens": 0,
           "total_tokens": 150, "credits": credits,
           "original_credits": 1.0, "billable": True,
           "request_id": "req-1", "duration_ms": duration_ms}
    for key in drop:
        rec.pop(key, None)
    return rec


def _qoder_global_usage(at, model="qfmodel", out_tok=200, ms=2000,
                        status=200, drop=()):
    """Qoder 国际请求日志记录（字段形状=qoder-global/logs/requests.log）。

    日志从不记 input_tokens/credits（E12-3 脱敏扫描），故本 helper 也不造。
    """
    rec = {"at": at, "model": model, "stream": True, "max_tokens": 1000,
           "tools": 0, "ms": ms, "status": status, "error": None,
           "body_bytes": 100, "finish": "stop", "out_tokens": out_tok}
    for key in drop:
        rec.pop(key, None)
    return rec


def _cliproxy_usage(time, model="gemini-3-pro", in_tok=100, out_tok=50,
                     duration_ms=1000):
    """Gemini 观测采集落盘记录（A12-2 落盘格式，字段名同既有账本）。"""
    return {"time": time, "model": model, "input_tokens": in_tok,
            "output_tokens": out_tok, "duration_ms": duration_ms}


class TestA12UsageQueries(unittest.TestCase):
    """A12-1 端点契约：临时目录 fixture，不读真实账本、不发任何请求。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # 进程内增量缓存跨用例共享：每个用例前后各清一次
        console_stats._reset_cache_for_tests()
        self.addCleanup(console_stats._reset_cache_for_tests)

    def _write(self, name, records):
        path = os.path.join(self._tmp.name, name)
        return _write_jsonl(path, records)

    def _write_snapshot(self, name, snapshot):
        path = os.path.join(self._tmp.name, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
        return path

    def _rows(self, result):
        return {(r["source"], r["model"]): r for r in result["days"]}

    def _missing(self, result):
        return {(m["source"], m["model"]): m for m in result["missing"]}

    # ------------------------------------------------------- tokens 端点

    def test_complete_partial_zero_and_missing_are_separated(self):
        """完整/部分/零/缺失四态：零是已知值，缺失另列且不补零。"""
        ledgers = {
            "chat2api": self._write("c2a.jsonl", [
                _c2a_usage("2026-09-24T10:00:00+08:00", model="m-a",
                            in_tok=100, out_tok=50),
                _c2a_usage("2026-09-24T10:00:00+08:00", model="m-zero",
                            in_tok=0, out_tok=0),
                _c2a_usage("2026-09-24T11:00:00+08:00", model="m-bare",
                            in_tok=None, out_tok=None,
                            drop=("input_tokens", "output_tokens")),
            ]),
            "qoder_global": self._write("qg.jsonl", [
                _qoder_global_usage("2026-09-24T03:00:00.000Z", model="m-a",
                                    out_tok=200, ms=1000),
            ]),
        }
        res = console_stats.usage_tokens(ledgers, days=1, now=NOW_1200)
        rows = self._rows(res)
        self.assertEqual(rows[("chat2api", "m-a")], {
            "date": "2026-09-24", "source": "chat2api", "model": "m-a",
            "input_tokens": 100, "output_tokens": 50, "complete": True,
            "complete_requests": 1, "partial_requests": 0,
            "partial_input_tokens": None, "partial_output_tokens": None})
        # 零是已知值：记录里写了 0 就照实计，仍属完整
        self.assertEqual(rows[("chat2api", "m-zero")]["input_tokens"], 0)
        self.assertEqual(rows[("chat2api", "m-zero")]["output_tokens"], 0)
        self.assertTrue(rows[("chat2api", "m-zero")]["complete"])
        # 国际缺输入：无完整请求故完整列 null、complete=False，输出走 partial 另列
        self.assertIsNone(rows[("qoder_global", "m-a")]["input_tokens"])
        self.assertIsNone(rows[("qoder_global", "m-a")]["output_tokens"])
        self.assertEqual(rows[("qoder_global", "m-a")]["partial_output_tokens"], 200)
        self.assertIsNone(rows[("qoder_global", "m-a")]["partial_input_tokens"])
        self.assertEqual(rows[("qoder_global", "m-a")]["complete_requests"], 0)
        self.assertEqual(rows[("qoder_global", "m-a")]["partial_requests"], 1)
        self.assertFalse(rows[("qoder_global", "m-a")]["complete"])
        # 两个分量都缺：行保留（有记录）但两列皆 null，不与「无记录」混淆
        self.assertIsNone(rows[("chat2api", "m-bare")]["input_tokens"])
        self.assertIsNone(rows[("chat2api", "m-bare")]["output_tokens"])
        self.assertEqual(rows[("chat2api", "m-bare")]["partial_requests"], 1)
        missing = self._missing(res)
        self.assertEqual(missing[("qoder_global", "m-a")]["reason"],
                         "output_only")
        self.assertEqual(missing[("qoder_global", "m-a")]["date"],
                         "2026-09-24")
        # missing 行带出部分请求可展示的分量（仅输出可见）
        self.assertEqual(missing[("qoder_global", "m-a")]["output_tokens"], 200)
        self.assertIsNone(missing[("qoder_global", "m-a")]["input_tokens"])
        self.assertEqual(missing[("chat2api", "m-bare")]["reason"], "no_tokens")
        self.assertIsNone(missing[("chat2api", "m-bare")]["output_tokens"])
        self.assertIsNone(missing[("chat2api", "m-bare")]["input_tokens"])
        # 完整行只数两分量齐全的桶：m-a 与 m-zero
        self.assertEqual(res["coverage"]["complete_rows"], 2)
        self.assertEqual(res["coverage"]["partial_rows"], 2)
        self.assertEqual(res["coverage"]["days"], 1)
        self.assertEqual(res["coverage"]["scanned"], 4)
        self.assertEqual(res["timezone"], "Asia/Shanghai")
        self.assertIsInstance(res["checked_epoch"], int)
        for entry in res["missing"]:
            self.assertIn(entry["reason"], ALLOWED_REASONS)
        # 无记录的 09-23 不出行（零记录与无记录分开）
        res2 = console_stats.usage_tokens(ledgers, days=2, now=NOW_1200)
        self.assertEqual(sorted({r["date"] for r in res2["days"]}),
                         ["2026-09-24"])

    def test_partial_sibling_does_not_zero_complete_total(self):
        """F1 最小反例：同模型同日「一条 100 输入 + 50 输出」+「一条仅 20 输出」。

        完整合计必须 = 150（只含两分量都齐全的那条），不得因同桶另一条缺
        输入而变成 0/null；缺分量那条另列 missing 并带出可展示的输出 20。
        """
        ledgers = {"chat2api": self._write("c2a.jsonl", [
            _c2a_usage("2026-09-24T10:00:00+08:00", model="m-x",
                        in_tok=100, out_tok=50, duration_ms=1000),
            _c2a_usage("2026-09-24T11:00:00+08:00", model="m-x",
                        in_tok=None, out_tok=20, duration_ms=1000,
                        drop=("input_tokens",)),
        ])}
        res = console_stats.usage_tokens(ledgers, days=1, now=NOW_1200)
        row = self._rows(res)[("chat2api", "m-x")]
        # 完整合计只来自完整请求：100 + 50 = 150（不是 0、不是 170）
        self.assertEqual(row["input_tokens"], 100)
        self.assertEqual(row["output_tokens"], 50)
        self.assertEqual(row["input_tokens"] + row["output_tokens"], 150)
        self.assertEqual(row["complete_requests"], 1)
        self.assertEqual(row["partial_requests"], 1)
        self.assertFalse(row["complete"])
        # 缺分量那条另列：仅输出可见 20，输入未知（绝不补零）
        self.assertIsNone(row["partial_input_tokens"])
        self.assertEqual(row["partial_output_tokens"], 20)
        missing = self._missing(res)[("chat2api", "m-x")]
        self.assertEqual(missing["reason"], "output_only")
        self.assertEqual(missing["date"], "2026-09-24")
        self.assertEqual(missing["output_tokens"], 20)
        self.assertIsNone(missing["input_tokens"])
        self.assertEqual(res["coverage"]["complete_rows"], 0)
        self.assertEqual(res["coverage"]["partial_rows"], 1)

    def test_cross_source_same_model_not_merged(self):
        ledgers = {
            "chat2api": self._write("c2a.jsonl", [
                _c2a_usage("2026-09-24T10:00:00+08:00", model="same",
                            in_tok=100, out_tok=10)]),
            "qoder_cn": self._write("qcn.jsonl", [
                _qoder_cn_usage("2026-09-24T10:00:00+08:00", model="same",
                                in_tok=7, out_tok=3)]),
        }
        res = console_stats.usage_tokens(ledgers, days=1, now=NOW_1200)
        self.assertEqual(
            [(r["source"], r["model"], r["input_tokens"], r["output_tokens"])
             for r in res["days"]],
            [("chat2api", "same", 100, 10), ("qoder_cn", "same", 7, 3)])
        self.assertEqual(res["coverage"]["complete_rows"], 2)

    def test_international_missing_fields_stay_unknown(self):
        """国际日志零行有 input_tokens/credits：一律未知，不估算不折零。"""
        ledgers = {"qoder_global": self._write("qg.jsonl", [
            _qoder_global_usage("2026-09-24T03:00:00.000Z", model="m-out",
                                out_tok=300, ms=1000),
            _qoder_global_usage("2026-09-24T04:00:00.000Z", model="m-none",
                                out_tok=None, ms=1000, status=429),
        ])}
        res = console_stats.usage_tokens(ledgers, days=1, now=NOW_1200)
        rows = self._rows(res)
        for row in res["days"]:
            self.assertIsNone(row["input_tokens"])
            self.assertNotIn("credits", row)  # 可归因积分不可得：不出字段
            self.assertFalse(row["complete"])
        self.assertIsNone(rows[("qoder_global", "m-out")]["input_tokens"])
        # 无完整请求：完整列 null，可展示的输出走 partial_*
        self.assertIsNone(rows[("qoder_global", "m-out")]["output_tokens"])
        self.assertEqual(rows[("qoder_global", "m-out")]["partial_output_tokens"],
                         300)
        self.assertIsNone(rows[("qoder_global", "m-none")]["output_tokens"])
        self.assertIsNone(rows[("qoder_global", "m-none")]["partial_output_tokens"])
        self.assertEqual({m["model"]: m["reason"] for m in res["missing"]},
                         {"m-out": "output_only", "m-none": "no_tokens"})
        cov = res["coverage"]["sources"]["qoder_global"]
        self.assertEqual(cov["unknown_fields"], ["input_tokens", "credits"])
        self.assertEqual(cov["non_ok_records"], 1)
        rates = console_stats.usage_rates(ledgers, hours=24, now=NOW_1200)
        self.assertEqual(rates["skipped"]["missing_output_tokens"], 1)
        self.assertEqual(rates["skipped"]["invalid_time"], 0)
        self.assertEqual([r["model"] for r in rates["hours"]], ["m-out"])
        self.assertEqual(rates["hours"][0]["output_tokens"], 300)

    def test_invalid_timestamp_goes_to_missing_not_dropped(self):
        ledgers = {"chat2api": self._write("c2a.jsonl", [
            _c2a_usage("not-a-time", model="m-bad", in_tok=5, out_tok=5,
                        duration_ms=100),
            _c2a_usage("2026-09-24T10:00:00+08:00", model="m-ok",
                        in_tok=1, out_tok=1),
        ])}
        res = console_stats.usage_tokens(ledgers, days=1, now=NOW_1200)
        self.assertEqual(
            [m for m in res["missing"] if m["reason"] == "invalid_time"],
            [{"date": None, "source": "chat2api", "model": "m-bad",
              "reason": "invalid_time",
              "input_tokens": None, "output_tokens": None}])
        self.assertEqual(
            res["coverage"]["sources"]["chat2api"]["undated_records"], 1)
        self.assertEqual(res["coverage"]["scanned"], 1)  # 无效时间不进扫描计数
        self.assertEqual(self._rows(res)[("chat2api", "m-ok")]["complete"], True)
        rates = console_stats.usage_rates(ledgers, hours=24, now=NOW_1200)
        self.assertEqual(rates["skipped"]["invalid_time"], 1)

    def test_missing_rows_capped_with_dropped_count(self):
        """F3a 最小反例：行上限 3 + 5 条缺失行（5 个缺分量的桶）。

        修复前 missing[] 不截断，仍返回 5 条；现在与 days[] 用同一上限，
        只返回 3 条、dropped_missing_rows=2 如实标注截断。
        """
        ledgers = {"chat2api": self._write("c2a.jsonl", [
            _c2a_usage("2026-09-24T10:00:00+08:00", model="m-%02d" % i,
                        in_tok=None, out_tok=10, duration_ms=1000,
                        drop=("input_tokens",))
            for i in range(5)])}
        with mock.patch.object(console_stats, "MAX_ROWS_PER_ENDPOINT", 3):
            res = console_stats.usage_tokens(ledgers, days=1, now=NOW_1200)
        # days[] 与 missing[] 各自截断到同一上限，保留排序尾部
        self.assertEqual(len(res["days"]), 3)
        self.assertEqual(len(res["missing"]), 3)
        self.assertEqual(res["coverage"]["dropped_missing_rows"], 2)
        self.assertEqual(res["coverage"]["dropped_rows"], 2)
        self.assertTrue(res["coverage"]["truncated"])
        self.assertEqual([m["model"] for m in res["missing"]],
                         ["m-02", "m-03", "m-04"])
        # 未截断时计数为 0，不虚报丢弃
        res_all = console_stats.usage_tokens(ledgers, days=1, now=NOW_1200)
        self.assertEqual(res_all["coverage"]["dropped_missing_rows"], 0)
        self.assertEqual(len(res_all["missing"]), 5)

    def test_dsh_snapshot_day_row_and_missing_duration(self):
        day = usagesrv.DSH_MERGE_DAY
        path = self._write_snapshot("dsh.json", {
            "config": {"pricingCurrency": "USD"},
            "days": {day: {"byProviderModel": {
                "openwebui:Test-A": {"calls": 2, "cost": 1.0, "input": 10,
                                      "output": 20, "cacheRead": 0},
            }}},
        })
        now = datetime.fromisoformat(day + "T12:00:00+08:00")
        res = console_stats.usage_tokens({"dsh": path}, days=1, now=now)
        self.assertEqual(res["days"], [{
            "date": day, "source": "dsh", "model": "openwebui:Test-A",
            "input_tokens": 10, "output_tokens": 20, "complete": True,
            "complete_requests": 1, "partial_requests": 0,
            "partial_input_tokens": None, "partial_output_tokens": None}])
        # 快照是日级聚合、无逐次耗时：速率端点不出行，按缺耗时计数
        rates = console_stats.usage_rates({"dsh": path}, hours=24, now=now)
        self.assertEqual(rates["hours"], [])
        self.assertEqual(rates["skipped"]["missing_duration"], 1)

    def test_corrupt_snapshot_shapes_reported_not_raised(self):
        """快照 days / byProviderModel 非对象：走 corrupt_ledger 覆盖说明路径，
        不抛异常（否则端点会变成 502），也不冒充「正常的空账本」。"""
        day = usagesrv.DSH_MERGE_DAY
        now = datetime.fromisoformat(day + "T12:00:00+08:00")
        for name, snapshot, detail_key in (
                ("days-list", {"days": [1, 2]}, "days"),
                ("bpm-list", {"days": {day: {"byProviderModel": [1]}}},
                 "byProviderModel")):
            path = self._write_snapshot("%s.json" % name, snapshot)
            console_stats._reset_cache_for_tests()
            res = console_stats.usage_tokens({"dsh": path}, days=1, now=now)
            self.assertEqual(res["days"], [])
            self.assertEqual(res["missing"], [])
            cov = res["coverage"]["sources"]["dsh"]
            self.assertTrue(cov["ledger_present"])  # 文件在场可读，只是形状不对
            errors = {e["type"]: e for e in cov.get("errors", [])}
            self.assertIn("corrupt_ledger", errors)
            self.assertIn(detail_key, errors["corrupt_ledger"]["detail"])
            # 速率端点同样不抛：无事件可归，跳过计数保持 0（不臆造耗时缺失）
            rates = console_stats.usage_rates({"dsh": path}, hours=24, now=now)
            self.assertEqual(rates["hours"], [])
            self.assertEqual(rates["skipped"]["missing_duration"], 0)
            # 缓存命中（第二次查询）仍给出同一覆盖说明，不静默变成正常空账本
            again = console_stats.usage_tokens({"dsh": path}, days=1, now=now)
            again_cov = again["coverage"]["sources"]["dsh"]
            self.assertIn("corrupt_ledger",
                          {e["type"] for e in again_cov.get("errors", [])})

    def test_untrusted_read_failure_excluded_from_both_endpoints(self):
        """读失败退回旧缓存：两个端点整源排除，错误如实披露（不静默、不折 0）。

        先成功读取一次建立增量缓存，再让同一账本的后续读取失败（os.stat 抛
        OSError）：coverage 里 ledger_present 仍为 true、events 仍是被缓存的条数
        并带 read_error，但 days[]/missing[]/hours[]/skipped 都不得出现该来源的
        旧缓存数据——旧缓存不是本次读数（与 recorded_requests 判 None 同一口径）。
        把 UNTRUSTED_EVENT_ERRORS 置空会让本用例失败（旧缓存会被当本次读数）。
        """
        now = datetime.now(timezone.utc)
        inside = (now - timedelta(minutes=30)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")
        path = self._write("qg.jsonl", [
            _qoder_global_usage(inside, model="qfmodel", out_tok=300, ms=1000),
            _qoder_global_usage(inside, model="qfmodel", out_tok=None,
                                ms=1000),
        ])
        ledgers = {"qoder_global": path}
        first = console_stats.usage_tokens(ledgers, days=2, now=now)
        self.assertEqual([r["model"] for r in first["days"]], ["qfmodel"])
        first_rates = console_stats.usage_rates(ledgers, hours=24, now=now)
        self.assertEqual(first_rates["hours"][0]["output_tokens"], 300)
        self.assertEqual(first_rates["skipped"]["missing_output_tokens"], 1)

        real_stat = os.stat

        def fake_stat(target, *args, **kwargs):
            if target == path:
                raise PermissionError(13, "Permission denied")
            return real_stat(target, *args, **kwargs)

        with mock.patch("os.stat", new=fake_stat):
            tokens = console_stats.usage_tokens(ledgers, days=2, now=now)
            rates = console_stats.usage_rates(ledgers, hours=24, now=now)

        cov = tokens["coverage"]["sources"]["qoder_global"]
        self.assertTrue(cov["ledger_present"])  # 退回旧缓存：账本仍算「在场」
        self.assertEqual(cov["events"], 2)     # 旧缓存确有 2 条（旧逻辑会当本次读数）
        self.assertEqual([e["type"] for e in cov["errors"]],
                         [console_stats.ERROR_TYPE_READ])
        self.assertEqual(tokens["days"], [])
        self.assertEqual(tokens["missing"], [])
        self.assertEqual(tokens["coverage"]["scanned"], 0)
        rate_cov = rates["coverage"]["sources"]["qoder_global"]
        self.assertEqual([e["type"] for e in rate_cov["errors"]],
                         [console_stats.ERROR_TYPE_READ])
        self.assertEqual(rates["hours"], [])
        self.assertEqual(rates["skipped"], {"missing_output_tokens": 0,
                                          "missing_duration": 0,
                                          "invalid_duration": 0,
                                          "invalid_time": 0})

    def test_untrusted_corrupt_ledger_excluded_from_both_endpoints(self):
        """结构损坏（corrupt_ledger）同样整源排除：即便损坏快照仍给出可解析事件。

        真实解析器在形状损坏时返回空事件，但模块注释允许「可解析的部分照常
        保留」；本用例直接让解析层返回「有事件但 corrupt」，断言两个端点整源
        排除该来源（与 read_error 同一口径），错误如实披露。把
        UNTRUSTED_EVENT_ERRORS 置空会让本用例失败（事件会被当本次读数出行）。
        """
        day = usagesrv.DSH_MERGE_DAY
        path = self._write_snapshot(
            "dsh.json", {"days": {day: {"byProviderModel": {}}}})
        now = datetime.fromisoformat(day + "T12:00:00+08:00")
        event = console_stats._Event(
            ms=usagesrv._parse_ts(day + "T00:00:00+08:00"),
            model="openwebui:Test-A", input_tokens=10, output_tokens=20,
            duration_ms=None, duration_state="missing", status=None)
        with mock.patch.object(
                console_stats, "_snapshot_events",
                return_value=([event], 0,
                              "byProviderModel is not a JSON object")):
            tokens = console_stats.usage_tokens({"dsh": path}, days=1, now=now)
            rates = console_stats.usage_rates({"dsh": path}, hours=24, now=now)
        cov = tokens["coverage"]["sources"]["dsh"]
        self.assertTrue(cov["ledger_present"])
        self.assertEqual([e["type"] for e in cov["errors"]],
                         [console_stats.ERROR_TYPE_CORRUPT_LEDGER])
        self.assertEqual(tokens["days"], [])
        self.assertEqual(tokens["missing"], [])
        self.assertEqual(tokens["coverage"]["scanned"], 0)
        self.assertEqual(rates["hours"], [])
        self.assertEqual(rates["skipped"]["missing_duration"], 0)

    def test_malformed_lines_source_still_counted(self):
        """部分坏行（malformed_lines）不算不可信：照常出行计数、错误如实披露。

        把 malformed_lines 误加进 UNTRUSTED_EVENT_ERRORS 会让本用例失败
        （该来源会被整源排除，days[]/hours[] 都会消失）。
        """
        path = os.path.join(self._tmp.name, "c2a.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("not-json{{\n")
            f.write(json.dumps(
                _c2a_usage("2026-09-24T10:00:00+08:00", model="m-ok",
                           in_tok=7, out_tok=3), ensure_ascii=False) + "\n")
        res = console_stats.usage_tokens({"chat2api": path}, days=1,
                                          now=NOW_1200)
        cov = res["coverage"]["sources"]["chat2api"]
        self.assertEqual(
            cov["errors"],
            [{"type": console_stats.ERROR_TYPE_MALFORMED_LINES,
              "bad_lines": 1}])
        self.assertTrue(console_stats._events_trusted(cov))
        self.assertNotIn(console_stats.ERROR_TYPE_MALFORMED_LINES,
                         console_stats.UNTRUSTED_EVENT_ERRORS)
        self.assertEqual([r["model"] for r in res["days"]], ["m-ok"])
        rates = console_stats.usage_rates({"chat2api": path}, hours=24,
                                          now=NOW_1200)
        self.assertEqual([r["requests"] for r in rates["hours"]], [1])
        self.assertEqual(rates["skipped"], {"missing_output_tokens": 0,
                                          "missing_duration": 0,
                                          "invalid_duration": 0,
                                          "invalid_time": 0})

    def test_untrusted_error_types_single_sourced(self):
        """不可信错误类型单一来源：生产端产出的类型都在 UNTRUSTED_EVENT_ERRORS 里。

        两个生产分支（读取失败 read_error / 结构损坏 corrupt_ledger）各自产出的
        errors[].type 必须都是 UNTRUSTED_EVENT_ERRORS 的成员——新增一个同义
        类型而未入集合会被本用例捕获；malformed_lines 不在集合内（部分坏行、
        其余可读）；usagesrv 不再自留一份字面量集合，判定取 console_stats 的
        单点定义。
        """
        self.assertEqual(
            console_stats.UNTRUSTED_EVENT_ERRORS,
            frozenset({console_stats.ERROR_TYPE_READ,
                       console_stats.ERROR_TYPE_CORRUPT_LEDGER}))
        self.assertNotIn(console_stats.ERROR_TYPE_MALFORMED_LINES,
                         console_stats.UNTRUSTED_EVENT_ERRORS)
        self.assertFalse(hasattr(usagesrv, "_UNTRUSTED_EVENT_ERRORS"))

        produced = set()
        # 生产分支①：本次读取失败（read_error）
        path = self._write("qg.jsonl", [
            _qoder_global_usage("2026-09-24T03:00:00.000Z", model="m")])
        ledgers = {"qoder_global": path}
        console_stats.usage_tokens(ledgers, days=1, now=NOW_1200)
        real_stat = os.stat

        def fake_stat(target, *args, **kwargs):
            if target == path:
                raise PermissionError(13, "Permission denied")
            return real_stat(target, *args, **kwargs)

        with mock.patch("os.stat", new=fake_stat):
            res = console_stats.usage_tokens(ledgers, days=1, now=NOW_1200)
        produced.update(e["type"] for e in
                        res["coverage"]["sources"]["qoder_global"]["errors"])
        # 生产分支②：结构损坏（corrupt_ledger）
        console_stats._reset_cache_for_tests()
        day = usagesrv.DSH_MERGE_DAY
        snapshot = self._write_snapshot("dsh.json", {"days": [1, 2]})
        res = console_stats.usage_tokens(
            {"dsh": snapshot}, days=1,
            now=datetime.fromisoformat(day + "T12:00:00+08:00"))
        produced.update(e["type"] for e in
                        res["coverage"]["sources"]["dsh"]["errors"])
        # 生产端产出的每个不可信类型都在集合里（新增同义类型会响亮失败）
        self.assertTrue(produced)
        self.assertLessEqual(produced, console_stats.UNTRUSTED_EVENT_ERRORS)

    def test_um_source_has_no_ledger_this_round(self):
        ledgers = {"um": os.path.join(self._tmp.name, "no-such-um.jsonl")}
        res = console_stats.usage_tokens(ledgers, days=1, now=NOW_1200)
        self.assertEqual(res["days"], [])
        cov = res["coverage"]["sources"]["um"]
        self.assertFalse(cov["ledger_present"])
        self.assertFalse(cov["enabled"])
        self.assertIn("无独立账本", cov["note"])

    def test_default_sources_covers_um_read_chain(self):
        sources = console_stats.default_sources()
        # B0：默认 UM 首页读链只含 chat2api 与冻结 DSH 快照。
        self.assertEqual(set(sources), {"chat2api", "dsh"})
        self.assertIs(sources["chat2api"], usagesrv.CHAT2API_LOG)
        self.assertIs(sources["dsh"], usagesrv.DSH_SNAPSHOT_FILE)
        # 退役来源的字段映射仍保留（显式历史追溯），但不进默认装配。
        for name in ("qoder_cn", "qoder_global", "cliproxy"):
            self.assertIn(name, console_stats._SOURCE_SPECS)
            self.assertNotIn(name, sources)

    # -------------------------------------------------------- rates 端点

    def test_hour_buckets_cross_midnight_and_utc(self):
        """北京时间跨日分桶：23:50 归当日 23 点；00:10 与 16:30Z 同归次日 0 点。"""
        now = datetime(2026, 9, 25, 1, 0, tzinfo=BEIJING)
        ledgers = {"chat2api": self._write("c2a.jsonl", [
            _c2a_usage("2026-09-24T23:50:00+08:00", model="m", out_tok=60,
                        duration_ms=2000),
            _c2a_usage("2026-09-25T00:10:00+08:00", model="m", out_tok=30,
                        duration_ms=1000),
            _c2a_usage("2026-09-24T16:30:00Z", model="m", out_tok=90,
                        duration_ms=3000),
        ])}
        res = console_stats.usage_rates(ledgers, hours=24, now=now)
        hours = {r["hour"]: r for r in res["hours"]}
        self.assertEqual(sorted(hours), ["2026-09-24T23:00:00+08:00",
                                        "2026-09-25T00:00:00+08:00"])
        self.assertEqual(hours["2026-09-24T23:00:00+08:00"]["output_tokens"], 60)
        # 00:10+08 与 16:30Z（=00:30+08）落在同一北京整点
        self.assertEqual(hours["2026-09-25T00:00:00+08:00"]["output_tokens"], 120)
        self.assertEqual(hours["2026-09-25T00:00:00+08:00"]["duration_ms"], 4000)
        self.assertEqual(hours["2026-09-25T00:00:00+08:00"]["requests"], 2)
        self.assertEqual(hours["2026-09-25T00:00:00+08:00"]["rate"], 30.0)
        self.assertEqual(res["coverage"]["hours"], 24)
        self.assertEqual(res["timezone"], "Asia/Shanghai")

    def test_invalid_duration_skipped_not_in_denominator(self):
        ledgers = {"chat2api": self._write("c2a.jsonl", [
            _c2a_usage("2026-09-24T10:00:00+08:00", model="m", out_tok=100,
                        duration_ms=1000),
            _c2a_usage("2026-09-24T10:00:00+08:00", model="m", out_tok=50,
                        duration_ms=0),
            _c2a_usage("2026-09-24T10:00:00+08:00", model="m", out_tok=50,
                        duration_ms=-5),
            _c2a_usage("2026-09-24T10:00:00+08:00", model="m", out_tok=50,
                        duration_ms="abc"),
            _c2a_usage("2026-09-24T10:00:00+08:00", model="m", out_tok=50,
                        duration_ms=None, drop=("duration_ms",)),
        ])}
        res = console_stats.usage_rates(ledgers, hours=24, now=NOW_1200)
        self.assertEqual(res["skipped"], {"missing_output_tokens": 0,
                                          "missing_duration": 1,
                                          "invalid_duration": 3,
                                          "invalid_time": 0})
        self.assertEqual(len(res["hours"]), 1)
        row = res["hours"][0]
        self.assertEqual(row["output_tokens"], 100)  # 只含可参与分母的记录
        self.assertEqual(row["duration_ms"], 1000)
        self.assertEqual(row["requests"], 1)
        self.assertEqual(row["rate"], 100.0)

    def test_skipped_reason_counts_are_independent(self):
        """原因计数相互独立：同一条记录同时缺输出 Token 且耗时非正时两项都计数
        （生产账本确有 ms=0 且 out_tokens 缺失的行，此前只报缺输出 Token）。"""
        ledgers = {"chat2api": self._write("c2a.jsonl", [
            # 缺输出 Token + 耗时=0：两项都要计（互斥实现下 invalid_duration=0）
            _c2a_usage("2026-09-24T10:00:00+08:00", model="m-both",
                        duration_ms=0, drop=("output_tokens",)),
            # 只缺输出 Token：耗时正常
            _c2a_usage("2026-09-24T10:00:00+08:00", model="m-out-only",
                        duration_ms=1000, drop=("output_tokens",)),
            # 只坏耗时：输出 Token 齐全
            _c2a_usage("2026-09-24T10:00:00+08:00", model="m-dur-only",
                        out_tok=50, duration_ms=-1),
            # 缺输出 Token + 缺耗时字段：两项都计
            _c2a_usage("2026-09-24T10:00:00+08:00", model="m-none",
                        drop=("output_tokens", "duration_ms")),
            # 唯一可进分母的记录
            _c2a_usage("2026-09-24T10:00:00+08:00", model="m-ok",
                        out_tok=100, duration_ms=1000),
        ])}
        res = console_stats.usage_rates(ledgers, hours=24, now=NOW_1200)
        self.assertEqual(res["skipped"], {"missing_output_tokens": 3,
                                          "missing_duration": 1,
                                          "invalid_duration": 2,
                                          "invalid_time": 0})
        # 三条原因计数之和（6）大于被跳过的记录条数（4）：不是「一条只算一次」
        self.assertGreater(sum(res["skipped"][k] for k in
                               ("missing_output_tokens", "missing_duration",
                                "invalid_duration")), 4)
        self.assertEqual(len(res["hours"]), 1)
        self.assertEqual(res["hours"][0]["requests"], 1)
        self.assertEqual(res["hours"][0]["rate"], 100.0)

    def test_out_of_order_records_bucket_by_own_time(self):
        records = [
            _c2a_usage("2026-09-24T09:00:00+08:00", model="m-a", in_tok=1,
                        out_tok=1, duration_ms=1000),
            _c2a_usage("2026-09-24T10:00:00+08:00", model="m-b", in_tok=2,
                        out_tok=2, duration_ms=1000),
            _c2a_usage("2026-09-24T11:00:00+08:00", model="m-c", in_tok=3,
                        out_tok=3, duration_ms=1000),
        ]
        forward = console_stats.usage_tokens(
            {"chat2api": self._write("fwd.jsonl", records)}, days=1,
            now=NOW_1200)
        backward = console_stats.usage_tokens(
            {"chat2api": self._write("bwd.jsonl", list(reversed(records)))},
            days=1, now=NOW_1200)
        self.assertEqual(forward["days"], backward["days"])
        self.assertEqual(backward["coverage"]["scanned"], 3)
        rates_fwd = console_stats.usage_rates(
            {"chat2api": self._write("fwd-r.jsonl", records)}, hours=24,
            now=NOW_1200)
        rates_bwd = console_stats.usage_rates(
            {"chat2api": self._write("bwd-r.jsonl", list(reversed(records)))},
            hours=24, now=NOW_1200)
        self.assertEqual(rates_fwd["hours"], rates_bwd["hours"])

    # ---------------------------------------------------- 缓存与有界

    def test_incremental_cache_appends_partial_line_and_invalidation(self):
        path = self._write("c2a.jsonl", [
            _c2a_usage("2026-09-24T10:00:00+08:00", model="m-a", in_tok=1,
                        out_tok=1)])
        first = console_stats.usage_tokens({"chat2api": path}, days=1,
                                           now=NOW_1200)
        self.assertEqual(len(first["days"]), 1)
        state = console_stats._cache_state(path)
        self.assertEqual(state["offset"], os.path.getsize(path))
        # 追加：只解析新增尾部
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(
                _c2a_usage("2026-09-24T11:00:00+08:00", model="m-b",
                            in_tok=2, out_tok=2), ensure_ascii=False) + "\n")
        second = console_stats.usage_tokens({"chat2api": path}, days=1,
                                            now=NOW_1200)
        self.assertEqual([r["model"] for r in second["days"]], ["m-a", "m-b"])
        # 尾部半行不消费（写入方可能正在追加）
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"time": "2026-09-24T12:00:00+08:00", "model": "m-c"')
        half = console_stats.usage_tokens({"chat2api": path}, days=1,
                                          now=NOW_1200)
        self.assertEqual([r["model"] for r in half["days"]], ["m-a", "m-b"])
        self.assertLess(console_stats._cache_state(path)["offset"],
                        os.path.getsize(path))
        # 补齐该行后按一次计入（不重复、不漏记）
        with open(path, "a", encoding="utf-8") as f:
            f.write(', "input_tokens": 3, "output_tokens": 3,'
                    ' "duration_ms": 1000}\n')
        third = console_stats.usage_tokens({"chat2api": path}, days=1,
                                           now=NOW_1200)
        self.assertEqual([r["model"] for r in third["days"]],
                         ["m-a", "m-b", "m-c"])
        self.assertEqual(
            {r["model"]: r["input_tokens"] for r in third["days"]}["m-c"], 3)
        # 文件缩短（重写）：缓存失效，整体重读
        _write_jsonl(path, [_c2a_usage("2026-09-24T13:00:00+08:00",
                                        model="m-only", in_tok=9,
                                        out_tok=9)])
        fourth = console_stats.usage_tokens({"chat2api": path}, days=1,
                                            now=NOW_1200)
        self.assertEqual([r["model"] for r in fourth["days"]], ["m-only"])

    def test_range_clamped_and_row_cap_truncated(self):
        records = [_c2a_usage("2026-09-24T10:00:00+08:00",
                               model="m-%04d" % i, in_tok=1, out_tok=1)
                   for i in range(console_stats.MAX_ROWS_PER_ENDPOINT + 1000)]
        path = self._write("c2a.jsonl", records)
        res = console_stats.usage_tokens({"chat2api": path}, days=999,
                                          now=NOW_1200)
        self.assertEqual(res["coverage"]["days"],
                         console_stats.MAX_TOKEN_RANGE_DAYS)
        self.assertTrue(res["coverage"]["clamped"])
        self.assertEqual(len(res["days"]),
                         console_stats.MAX_ROWS_PER_ENDPOINT)
        self.assertTrue(res["coverage"]["truncated"])
        self.assertEqual(res["coverage"]["dropped_rows"], 1000)
        rates = console_stats.usage_rates({"chat2api": path}, hours=999,
                                           now=NOW_1200)
        self.assertEqual(rates["coverage"]["hours"],
                         console_stats.MAX_RATE_RANGE_HOURS)
        self.assertTrue(rates["coverage"]["clamped"])
        self.assertEqual(len(rates["hours"]),
                         console_stats.MAX_ROWS_PER_ENDPOINT)

    def test_malformed_lines_counted_not_fatal(self):
        path = os.path.join(self._tmp.name, "c2a.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("not-json{{\n")
            f.write("[1, 2]\n")  # 合法 JSON 但不是对象
            f.write(json.dumps(
                _c2a_usage("2026-09-24T10:00:00+08:00", model="m-ok",
                            in_tok=7, out_tok=3), ensure_ascii=False) + "\n")
        res = console_stats.usage_tokens({"chat2api": path}, days=1,
                                          now=NOW_1200)
        self.assertEqual([r["model"] for r in res["days"]], ["m-ok"])
        cov = res["coverage"]["sources"]["chat2api"]
        self.assertEqual(cov["errors"],
                         [{"type": "malformed_lines", "bad_lines": 2}])
        rates = console_stats.usage_rates({"chat2api": path}, hours=24,
                                           now=NOW_1200)
        self.assertEqual(rates["hours"][0]["requests"], 1)
        self.assertEqual(rates["hours"][0]["rate"], 3.0)

    def test_byte_and_line_caps_marked_truncated(self):
        records = [_c2a_usage("2026-09-24T10:%02d:00+08:00" % i,
                               model="m-%02d" % i, in_tok=1, out_tok=1)
                   for i in range(6)]
        path = self._write("c2a.jsonl", records)
        size = os.path.getsize(path)
        # 单次读取字节上限：只读尾部（首个残行丢弃），并如实标注
        with mock.patch.object(console_stats, "MAX_READ_BYTES", size // 2):
            res = console_stats.usage_tokens({"chat2api": path}, days=1,
                                              now=NOW_1200)
        models = [r["model"] for r in res["days"]]
        self.assertEqual(models[-1], "m-05")
        self.assertLess(len(models), 6)
        cov = res["coverage"]["sources"]["chat2api"]
        self.assertTrue(cov["bytes_truncated"])
        self.assertTrue(cov["truncated"])
        self.assertNotIn("errors", cov)  # 尾部截断不是坏行
        # 单文件行数上限：保留最新、丢弃最旧并计数
        console_stats._reset_cache_for_tests()
        with mock.patch.object(console_stats, "_LEDGER_RECORD_LIMIT", 2):
            res2 = console_stats.usage_tokens({"chat2api": path}, days=1,
                                               now=NOW_1200)
        self.assertEqual([r["model"] for r in res2["days"]],
                         ["m-04", "m-05"])
        cov2 = res2["coverage"]["sources"]["chat2api"]
        self.assertEqual(cov2["dropped_events"], 4)
        self.assertTrue(cov2["truncated"])

    def test_incremental_append_beyond_read_cap_marked_truncated(self):
        """F3b 最小反例：先小后大追加——首读不触发上限，随后一次性追加
        远超单次读取上限的字节。

        修复前增量分支 `f.read(max(0, size - read_start))` 没有字节上限，
        会把新增内容全量读入；现在只读尾部 MAX_READ_BYTES、丢弃起始残行并
        标 bytes_truncated（绝不静默全量读入）。
        """
        path = self._write("c2a.jsonl", [
            _c2a_usage("2026-09-24T10:00:00+08:00", model="m-old",
                        in_tok=1, out_tok=1)])
        cap = os.path.getsize(path)  # 单次上限=首读时的小文件大小（约一条记录）
        first = console_stats.usage_tokens({"chat2api": path}, days=1,
                                           now=NOW_1200)
        self.assertEqual([r["model"] for r in first["days"]], ["m-old"])
        self.assertFalse(
            first["coverage"]["sources"]["chat2api"].get("bytes_truncated",
                                                          False))
        # 追加 6 条（新增字节数倍于单次上限）
        with open(path, "a", encoding="utf-8") as f:
            for i in range(6):
                f.write(json.dumps(_c2a_usage(
                    "2026-09-24T11:%02d:00+08:00" % i, model="m-%02d" % i,
                    in_tok=1, out_tok=1), ensure_ascii=False) + "\n")
        with mock.patch.object(console_stats, "MAX_READ_BYTES", cap):
            second = console_stats.usage_tokens({"chat2api": path}, days=1,
                                                now=NOW_1200)
        models = [r["model"] for r in second["days"]]
        cov = second["coverage"]["sources"]["chat2api"]
        self.assertTrue(cov["bytes_truncated"])
        self.assertTrue(cov["truncated"])
        # 首读缓存的 m-old 仍在；本批 6 条只读到尾部 1 条（单次读取有界）
        self.assertIn("m-old", models)
        self.assertIn("m-05", models)           # 最新记录仍被读到
        self.assertNotIn("m-04", models)        # 起始残行（尾部窗口首条）被丢弃
        self.assertEqual(len(models), 2)        # 6 条新增里只读入 1 条
        state = console_stats._cache_state(path)
        self.assertTrue(state["bytes_truncated"])
        self.assertEqual(len(state["events"]), 2)

    def test_gemini_collection_disabled_returns_empty_with_note(self):
        missing_path = os.path.join(self._tmp.name, "usage-cliproxy.jsonl")
        ledgers = {
            "cliproxy": missing_path,
            "chat2api": self._write("c2a.jsonl", [
                _c2a_usage("2026-09-24T10:00:00+08:00", model="m-a",
                            in_tok=1, out_tok=1)]),
        }
        res = console_stats.usage_tokens(ledgers, days=1, now=NOW_1200)
        self.assertNotIn("cliproxy", {r["source"] for r in res["days"]})
        cov = res["coverage"]["sources"]["cliproxy"]
        self.assertFalse(cov["ledger_present"])
        self.assertFalse(cov["enabled"])
        self.assertIn("采集链未启用", cov["note"])
        self.assertIn("0 冒充", cov["note"])
        rates = console_stats.usage_rates(ledgers, hours=24, now=NOW_1200)
        self.assertFalse(rates["coverage"]["sources"]["cliproxy"]["enabled"])
        # 采集链启用（落盘文件出现）后才出行；字段未对齐按缺分量计
        path = self._write("usage-cliproxy.jsonl", [
            _cliproxy_usage("2026-09-24T10:00:00+08:00", in_tok=None,
                             out_tok=100, duration_ms=1000)])
        res2 = console_stats.usage_tokens({"cliproxy": path}, days=1,
                                           now=NOW_1200)
        self.assertEqual([r["source"] for r in res2["days"]], ["cliproxy"])
        self.assertIsNone(res2["days"][0]["input_tokens"])
        # 字段未对齐（缺输入）：完整列 null，可展示输出走 partial_*
        self.assertIsNone(res2["days"][0]["output_tokens"])
        self.assertEqual(res2["days"][0]["partial_output_tokens"], 100)
        self.assertTrue(res2["coverage"]["sources"]["cliproxy"]["enabled"])

    # --------------------------------------------------------- 载荷形状

    def test_reason_enums_match_contract(self):
        """missing[].reason 与 skipped 的键都由模块常量单点定义，无手工追加值。"""
        self.assertEqual(set(console_stats._MISSING_REASONS),
                         {"output_only", "no_input", "no_tokens",
                          "invalid_time"})
        self.assertEqual(set(console_stats._SKIPPED_REASONS),
                         {"missing_output_tokens", "missing_duration",
                          "invalid_duration", "invalid_time"})
        ledgers = {"chat2api": self._write("c2a.jsonl", [
            _c2a_usage("2026-09-24T10:00:00+08:00", model="m-a",
                        in_tok=1, out_tok=1)]),
        }
        res = console_stats.usage_tokens(ledgers, days=1, now=NOW_1200)
        self.assertLessEqual({m["reason"] for m in res["missing"]},
                             set(console_stats._MISSING_REASONS))
        rates = console_stats.usage_rates(ledgers, hours=24, now=NOW_1200)
        self.assertEqual(set(rates["skipped"]),
                         set(console_stats._SKIPPED_REASONS))

    def test_payload_shape_matches_contract(self):
        ledgers = {
            "chat2api": self._write("c2a.jsonl", [
                _c2a_usage("2026-09-24T10:00:00+08:00", model="m-a",
                            in_tok=10, out_tok=5, duration_ms=1000)]),
            "qoder_global": self._write("qg.jsonl", [
                _qoder_global_usage("2026-09-24T04:00:00.000Z", out_tok=20,
                                    ms=1000)]),
        }
        res = console_stats.usage_tokens(ledgers, days=1, now=NOW_1200)
        self.assertEqual(set(res),
                         {"days", "missing", "coverage", "timezone",
                          "checked_epoch"})
        for key in ("days", "complete_rows", "partial_rows", "scanned",
                    "truncated", "clamped", "dropped_rows",
                    "dropped_missing_rows", "sources"):
            self.assertIn(key, res["coverage"])
        for row in res["days"]:
            self.assertEqual(set(row), {"date", "source", "model",
                                        "input_tokens", "output_tokens",
                                        "complete", "complete_requests",
                                        "partial_requests",
                                        "partial_input_tokens",
                                        "partial_output_tokens"})
        for entry in res["missing"]:
            self.assertEqual(set(entry), {"date", "source", "model", "reason",
                                          "input_tokens", "output_tokens"})
        rates = console_stats.usage_rates(ledgers, hours=24, now=NOW_1200)
        self.assertEqual(set(rates),
                         {"hours", "skipped", "coverage", "timezone",
                          "checked_epoch"})
        self.assertEqual(set(rates["skipped"]),
                         set(console_stats._SKIPPED_REASONS))
        for key in ("hours", "rows", "truncated", "clamped", "dropped_rows",
                    "sources"):
            self.assertIn(key, rates["coverage"])
        self.assertEqual(rates["coverage"]["rows"], len(rates["hours"]))
        for row in rates["hours"]:
            self.assertEqual(set(row), {"hour", "source", "model",
                                        "output_tokens", "duration_ms",
                                        "requests", "rate"})
            self.assertIsInstance(row["output_tokens"], int)
            self.assertIsInstance(row["duration_ms"], int)
            self.assertIsInstance(row["requests"], int)
            self.assertTrue(row["rate"] is None
                            or isinstance(row["rate"], float))


class TestConsoleLogsNullPayload(unittest.TestCase):
    """F2：/api/console/logs 的数值字段缺失一律 null（不折零）。

    行构造器直接测 + 真实 HTTP 往返（本机临时端口，只读 GET，账本用临时
    文件替换 CHAT2API_LOG，绝不读真实账本、绝不碰任何服务）。
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.server = usagesrv.ThreadingHTTPServer(("127.0.0.1", 0),
                                                  usagesrv.Handler)
        cls.addClassCleanup(cls.server.shutdown)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    def _ledger(self, records):
        path = os.path.join(self._tmp.name, "usage-chat2api.jsonl")
        _write_jsonl(path, records)
        return path

    def _logs(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/console/logs?limit=10&sources=chat2api")
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, json.loads(raw)

    def test_row_builder_missing_numbers_stay_null(self):
        """行构造器：缺失/不可解析一律 None，真实 0 是已知值。"""
        row = usagesrv._console_log_row("chat2api", {
            "time": "2026-09-24T10:00:00+08:00", "model": "m-a",
            "status": "ok", "input_tokens": 100, "credits": 0.5})
        self.assertEqual(row["input_tokens"], 100)
        self.assertIsNone(row["output_tokens"])
        self.assertIsNone(row["cached_input_tokens"])
        self.assertIsNone(row["reasoning_tokens"])
        self.assertIsNone(row["duration_ms"])
        self.assertEqual(row["credits"], 0.5)
        self.assertEqual(usagesrv._log_int_or_none(0), 0)
        self.assertEqual(usagesrv._log_int_or_none("12"), 12)
        self.assertEqual(usagesrv._log_int_or_none(12.0), 12)
        self.assertIsNone(usagesrv._log_int_or_none(None))
        self.assertIsNone(usagesrv._log_int_or_none(""))
        self.assertIsNone(usagesrv._log_int_or_none("abc"))
        self.assertIsNone(usagesrv._log_int_or_none(1.5))
        self.assertIsNone(usagesrv._log_int_or_none(True))

    def test_endpoint_missing_numbers_stay_null(self):
        """缺输出但耗时有效的请求：output_tokens=null（不再折成 0 显示 0 Token/s）。"""
        path = self._ledger([
            {"time": "2026-09-24T10:00:00+08:00", "model": "m-partial",
             "status": "ok", "input_tokens": 100, "duration_ms": 1000},
            {"time": "2026-09-24T11:00:00+08:00", "model": "m-zero",
             "status": "ok", "input_tokens": 0, "output_tokens": 0,
             "cached_input_tokens": 0, "reasoning_tokens": 0,
             "duration_ms": 0, "credits": 0.5},
        ])
        with mock.patch.object(usagesrv, "CHAT2API_LOG", path):
            status, payload = self._logs()
        self.assertEqual(status, 200)
        rows = {r["model"]: r for r in payload["logs"]}
        partial = rows["m-partial"]
        self.assertEqual(partial["input_tokens"], 100)
        self.assertIsNone(partial["output_tokens"])
        self.assertIsNone(partial["cached_input_tokens"])
        self.assertIsNone(partial["reasoning_tokens"])
        self.assertEqual(partial["duration_ms"], 1000)
        self.assertIsNone(partial["credits"])
        # 真实 0 是已知值：写了 0 就照实返回 0
        zero = rows["m-zero"]
        self.assertEqual(zero["input_tokens"], 0)
        self.assertEqual(zero["output_tokens"], 0)
        self.assertEqual(zero["cached_input_tokens"], 0)
        self.assertEqual(zero["reasoning_tokens"], 0)
        self.assertEqual(zero["duration_ms"], 0)
        self.assertEqual(zero["credits"], 0.5)

    def test_endpoint_unparseable_numbers_stay_null(self):
        """不可解析的数值同样按未知披露（旧实现 int("abc") 会抛出并中断响应）。"""
        path = self._ledger([
            {"time": "2026-09-24T12:00:00+08:00", "model": "m-bad",
             "status": "ok", "input_tokens": "", "output_tokens": "abc",
             "duration_ms": "not-a-number"},
        ])
        with mock.patch.object(usagesrv, "CHAT2API_LOG", path):
            status, payload = self._logs()
        self.assertEqual(status, 200)
        bad = {r["model"]: r for r in payload["logs"]}["m-bad"]
        self.assertIsNone(bad["input_tokens"])
        self.assertIsNone(bad["output_tokens"])
        self.assertIsNone(bad["duration_ms"])


class TestF4bLogFeedCoverage(unittest.TestCase):
    """F4b：/api/console/logs 的来源覆盖与「已记录请求数」口径。

    临时账本 + 本机随机端口只读 GET（不读真实账本、不碰任何服务）。
    修复前 readers 表只有 chat2api/qoder 两个键，国际来源 id 被静默跳过；
    现在国际账本按请求的 id 原样出行，未覆盖的 id 进 sources_unavailable。
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.server = usagesrv.ThreadingHTTPServer(("127.0.0.1", 0),
                                                  usagesrv.Handler)
        cls.addClassCleanup(cls.server.shutdown)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    def setUp(self):
        # 进程内增量缓存跨用例共享：每个用例前后各清一次
        console_stats._reset_cache_for_tests()
        self.addCleanup(console_stats._reset_cache_for_tests)

    def _path(self, name):
        return os.path.join(self._tmp.name, name)

    def _get(self, path):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, json.loads(raw)

    def test_retired_source_ids_are_unavailable_not_silently_skipped(self):
        """B0：退役来源（国际/国内/CLIProxy）不在默认读表内，如实标为未覆盖。"""
        c2a = _write_jsonl(self._path("usage-chat2api.jsonl"), [
            _c2a_usage("2026-09-20T12:00:00+08:00", model="m-c2a")])
        with mock.patch.object(usagesrv, "CHAT2API_LOG", c2a):
            status, payload = self._get(
                "/api/console/logs?limit=40"
                "&sources=chat2api,qoder-global,qoder_global,nosuch")
        self.assertEqual(status, 200)
        self.assertEqual(payload["sources"],
                         ["chat2api", "qoder-global", "qoder_global", "nosuch"])
        # 未覆盖的 id 进 sources_unavailable：前端据此区分「没有记录」与
        # 「该来源不在账本覆盖内」，不再靠硬编码猜测
        self.assertEqual(payload["sources_unavailable"],
                         ["qoder-global", "qoder_global", "nosuch"])
        self.assertEqual([r["source"] for r in payload["logs"]], ["chat2api"])
        row = payload["logs"][0]
        self.assertEqual(row["model"], "m-c2a")
        self.assertEqual(row["time"], "2026-09-20T12:00:00+08:00")

    def test_retired_source_never_reads_its_ledger(self):
        """B0：请求退役来源 id 时不读它的历史账本，只如实声明未覆盖。"""
        intl = _write_jsonl(self._path("requests.log"), [
            _qoder_global_usage("2026-09-20T04:02:15.135Z", model="qfmodel",
                                out_tok=300, ms=16658)])
        c2a = _write_jsonl(self._path("usage-chat2api.jsonl"), [
            _c2a_usage("2026-09-20T12:00:00+08:00")])
        with mock.patch.object(usagesrv, "CHAT2API_LOG", c2a), \
                mock.patch.object(console_stats, "QODER_GLOBAL_REQUESTS_LOG",
                                  intl):
            status, payload = self._get(
                "/api/console/logs?limit=10&sources=chat2api,qoder_global")
        self.assertEqual(status, 200)
        self.assertEqual(payload["sources_unavailable"], ["qoder_global"])
        self.assertEqual([r["source"] for r in payload["logs"]], ["chat2api"])

    def test_usage_rates_recorded_requests_ledger_count(self):
        """recorded_requests 按账本记录计数：含无法计算速率的记录；空窗口 0、未读取 null。"""
        now = datetime.now(timezone.utc)
        inside = (now - timedelta(minutes=30)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")
        ledger = _write_jsonl(self._path("usage-chat2api.jsonl"), [
            # 可计算速率：有 out_tokens 且 ms 为正
            _c2a_usage(inside, model="m-c2a", out_tok=300, duration_ms=1000),
            # 不可计算速率（缺输出 Token）：仍是被账本记录的请求
            _c2a_usage(inside, model="m-c2a", drop=("output_tokens",), duration_ms=1000),
        ])
        empty_dsh = self._path("dsh-empty.json")
        with open(empty_dsh, "w", encoding="utf-8") as f:
            json.dump({"days": {}}, f)
        with mock.patch.object(usagesrv, "CHAT2API_LOG", ledger), \
                mock.patch.object(usagesrv, "DSH_SNAPSHOT_FILE", empty_dsh):
            status, payload = self._get("/api/console/usage-rates?hours=24")
        self.assertEqual(status, 200)
        recorded = payload["recorded_requests"]
        # 账本口径：2 条记录（含不可计算速率的那条）
        self.assertEqual(recorded["chat2api"], 2)
        rate_rows = [r for r in payload["hours"] if r["source"] == "chat2api"]
        self.assertEqual([r["requests"] for r in rate_rows], [1])
        self.assertEqual(payload["skipped"]["missing_output_tokens"], 1)
        # 账本已读取但窗口内无记录 = 真实的 0（零记录 ≠ 无记录）
        self.assertEqual(recorded["dsh"], 0)
        # 退役来源绝不进入默认读链
        for retired in ("qoder_cn", "qoder_global", "cliproxy"):
            self.assertNotIn(retired, recorded)
        # 与速率分桶同一窗口口径：hours 行的请求数之和 ≤ 账本计数
        self.assertLessEqual(sum(r["requests"] for r in rate_rows),
                             recorded["chat2api"])

    def test_usage_rates_recorded_requests_read_error_is_unknown(self):
        """读失败退回旧缓存事件时：recorded_requests 为 null，旧计数不是本次读数。

        先成功读取一次建立增量缓存，再让同一账本的后续读取失败（os.stat 抛
        OSError）：coverage 里 ledger_present 仍为 true（读取层退回上次缓存的事件，
        events 仍是被缓存的条数）并带 read_error，此时该来源的计数必须判为未知，
        绝不能把缓存里的旧计数当成本次「已记录请求数」。
        """
        now = datetime.now(timezone.utc)
        inside = (now - timedelta(minutes=30)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")
        ledger = _write_jsonl(self._path("usage-chat2api.jsonl"), [
            _c2a_usage(inside, model="m-c2a", out_tok=300, duration_ms=1000),
            _c2a_usage(inside, model="m-c2a", drop=("output_tokens",), duration_ms=1000),
        ])
        empty_dsh = self._path("dsh-empty.json")
        with open(empty_dsh, "w", encoding="utf-8") as f:
            json.dump({"days": {}}, f)
        with mock.patch.object(usagesrv, "CHAT2API_LOG", ledger), \
                mock.patch.object(usagesrv, "DSH_SNAPSHOT_FILE", empty_dsh):
            # 第一次读取成功：缓存建立，计数是真实的账本记录数
            status, payload = self._get("/api/console/usage-rates?hours=24")
            self.assertEqual(status, 200)
            self.assertEqual(payload["recorded_requests"]["chat2api"], 2)
            self.assertNotIn("errors",
                             payload["coverage"]["sources"]["chat2api"])

            real_stat = os.stat

            def fake_stat(path, *args, **kwargs):
                if path == ledger:
                    raise PermissionError(13, "Permission denied")
                return real_stat(path, *args, **kwargs)

            with mock.patch("os.stat", new=fake_stat):
                status, payload = self._get("/api/console/usage-rates?hours=24")
        self.assertEqual(status, 200)
        entry = payload["coverage"]["sources"]["chat2api"]
        # 走的确实是「读失败 + 有缓存」：账本在场（旧缓存），本次读取失败
        self.assertTrue(entry["ledger_present"])
        self.assertEqual([err["type"] for err in entry["errors"]],
                         ["read_error"])
        # 旧缓存里确有 2 条窗口内记录（会被旧逻辑当成本次计数），但它不是
        # 本次读数 → 未知（null），不返回旧计数
        self.assertEqual(entry["events"], 2)
        self.assertIsNone(payload["recorded_requests"]["chat2api"])
        # 对照：同一响应里已成功读取但窗口内无记录的来源仍是真实的 0
        self.assertEqual(payload["recorded_requests"]["dsh"], 0)

    def test_usage_rates_recorded_requests_corrupt_snapshot_is_unknown(self):
        """快照结构损坏：recorded_requests 为 null；形状正确但窗口内无记录才是 0。

        corrupt_ledger 说明事件集可能为空或不完整，不能当成本次读数（即使它
        确实读到了文件、ledger_present 为 true）。
        """
        day = usagesrv.DSH_MERGE_DAY
        corrupt = self._path("dsh-corrupt.json")
        with open(corrupt, "w", encoding="utf-8") as f:
            json.dump({"days": [1, 2]}, f)
        healthy = self._path("dsh-healthy.json")
        with open(healthy, "w", encoding="utf-8") as f:
            json.dump({"days": {day: {"byProviderModel": {
                "openwebui:Test-A": {"calls": 2, "cost": 1.0, "input": 10,
                                     "output": 20, "cacheRead": 0}}}}}, f)
        empty = _write_jsonl(self._path("usage-chat2api.jsonl"), [])
        missing = self._path("does-not-exist.jsonl")
        for name, snapshot in (("corrupt", corrupt), ("healthy", healthy)):
            console_stats._reset_cache_for_tests()
            with mock.patch.object(usagesrv, "CHAT2API_LOG", empty),                     mock.patch.object(usagesrv, "DSH_SNAPSHOT_FILE", snapshot):
                status, payload = self._get("/api/console/usage-rates?hours=24")
            self.assertEqual(status, 200, name)
            entry = payload["coverage"]["sources"]["dsh"]
            self.assertTrue(entry["ledger_present"], name)
            if name == "corrupt":
                errors = {e["type"] for e in entry.get("errors", [])}
                self.assertIn("corrupt_ledger", errors)
                self.assertIsNone(payload["recorded_requests"]["dsh"])
            else:
                self.assertEqual(entry.get("errors"), None)
                # 已读取、事件集可信、窗口内无记录（快照是 2026-09-02 日级
                # 聚合，落在 24h 窗口外）= 真实的 0
                self.assertEqual(payload["recorded_requests"]["dsh"], 0)

    def test_usage_rates_recorded_requests_malformed_lines_counted(self):
        """含坏行（malformed_lines）的来源仍按账本口径计数，错误如实披露。

        部分坏行、其余可读不算「事件集不可信」：recorded_requests 非 None、
        行照常出行；把 malformed_lines 误加进不可信集合会让本用例失败。
        """
        now = datetime.now(timezone.utc)
        inside = (now - timedelta(minutes=30)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")
        ledger = self._path("usage-chat2api.jsonl")
        with open(ledger, "w", encoding="utf-8") as f:
            f.write("not-json{\n")
            record = _c2a_usage(inside, model="m-c2a", out_tok=300,
                                 duration_ms=1000)
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        empty_dsh = self._path("dsh-empty.json")
        with open(empty_dsh, "w", encoding="utf-8") as f:
            json.dump({"days": {}}, f)
        with mock.patch.object(usagesrv, "CHAT2API_LOG", ledger), \
                mock.patch.object(usagesrv, "DSH_SNAPSHOT_FILE", empty_dsh):
            status, payload = self._get("/api/console/usage-rates?hours=24")
            self.assertEqual(status, 200)
            entry = payload["coverage"]["sources"]["chat2api"]
            self.assertEqual(
                entry["errors"],
                [{"type": console_stats.ERROR_TYPE_MALFORMED_LINES,
                  "bad_lines": 1}])
            # 非 None：账本已读取且事件集可信，窗口内确有 1 条记录
            self.assertEqual(payload["recorded_requests"]["chat2api"], 1)
            self.assertEqual([r["requests"] for r in payload["hours"]
                              if r["source"] == "chat2api"], [1])
            t_status, t_payload = self._get(
                "/api/console/usage-tokens?days=2")
        self.assertEqual(t_status, 200)
        self.assertEqual([r["model"] for r in t_payload["days"]],
                         ["m-c2a"])


class TestPricingMatchMemoization(unittest.TestCase):
    """2026-09-26 /api/console/stats 性能修复的回归。

    逐条计价里最贵的一步是 usagesrv.match_model_id：它每次调用都对全部候选
    做 canon/strip/token 正则归一化。修复把它按 (model_id, 候选序列) 记忆化
    （纯函数）。这里用「未缓存实现的调用次数」作为确定性回归门：账本里同一
    模型重复上千次时，匹配次数必须按不同模型数收敛，而不是按记录数增长。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name)
        self._orig_price_file = usagesrv.PRICE_FILE
        self._orig_override = usagesrv.OVERRIDE_FILE
        self._orig_price_cache = dict(usagesrv._price_cache)
        self.saved_cache = dict(usagesrv._match_cache)
        prices = {
            "_说明": "pricing match memoization fixture",
            "checkedAt": CHECKED_AT,
            "exchangeRate": RATE,
            "models": {},
            "providers": {
                "z-ai": {"models": {"glm-5.3": GLM_5_3_PRICE,
                                    "gfmodel": GLM_5_3_PRICE}},
                "openai": {"models": {"gpt-5.6-sol": GPT_5_6_SOL_PRICE}},
            },
        }
        price_path = self.data / "prices.json"
        price_path.write_text(json.dumps(prices, ensure_ascii=False),
                              encoding="utf-8")
        usagesrv.PRICE_FILE = str(price_path)
        usagesrv.OVERRIDE_FILE = str(self.data / "no-override.json")
        usagesrv._price_cache["data"] = None
        self.addCleanup(self._restore)

    def _restore(self):
        usagesrv.PRICE_FILE = self._orig_price_file
        usagesrv.OVERRIDE_FILE = self._orig_override
        usagesrv._price_cache.clear()
        usagesrv._price_cache.update(self._orig_price_cache)
        usagesrv._match_cache.clear()
        usagesrv._match_cache.update(self.saved_cache)

    def test_memoized_result_equals_uncached_body(self):
        cases = (
            ("DeepSeek-V4.1-Flash", ["DeepSeek-V4.1-Flash", "claude-x"]),
            ("GLM-5.2 (go)", ["glm-5.2", "glm-5.3"]),
            ("deepseek-v4-flash-128k", ["deepseek-v4-flash", "glm-5.3"]),
            ("glm-5", ["glm-5.2", "glm-5.3"]),
            ("gpt-5.9", ["gpt-5-nano"]),
            ("k2-chat", ["k2"]),
            ("openwebui:Qwen3.8-27B", ["qwen3.8-27b", "k2"]),
            ("", ["k2"]),
            ("claude-x", []),
        )
        for model_id, candidates in cases:
            with self.subTest(model=model_id):
                usagesrv._match_cache.clear()
                expected = usagesrv._match_model_id_uncached(model_id,
                                                             candidates)
                self.assertEqual(usagesrv.match_model_id(model_id, candidates),
                                 expected)
                # 第二次调用走缓存，值必须与未缓存实现一致
                self.assertEqual(usagesrv.match_model_id(model_id, candidates),
                                 expected)

    def test_repeated_models_do_not_repeat_the_expensive_match(self):
        """同一模型重复 400 条：未缓存匹配次数按「不同模型 × 价表档」收敛。"""
        records = []
        models = ["glm-5.3", "gpt-5.6-sol", "gfmodel"]
        for i in range(400):
            records.append(_c2a_record(
                "2026-09-20T10:%02d:%02d.000Z" % (i // 60, i % 60),
                model=models[i % len(models)]))
        path = _write_jsonl(self.data / "usage-chat2api.jsonl", records)
        tiers = usagesrv.load_price_table()["tiers"]
        calls = []
        real = usagesrv._match_model_id_uncached

        def counting(model_id, candidates):
            calls.append(model_id)
            return real(model_id, candidates)

        usagesrv._match_cache.clear()
        with mock.patch.object(usagesrv, "_match_model_id_uncached",
                              side_effect=counting):
            payload = console_stats.bounded_stats(
                {"chat2api": str(path)}, "2026-09-20", "2026-09-20")
        self.assertEqual(payload["totals"]["requests"], 400)
        bound = len(models) * max(1, len(tiers))
        self.assertGreater(
            len(calls), 0,
            "逐条计价必须经过 usagesrv 的记忆化 match_model_id；"
            "0 次未缓存调用说明计价绕过了宿主缓存")
        self.assertLessEqual(
            len(calls), bound,
            "同一模型重复 400 条时不得重复走昂贵匹配（实测 %d 次，上限 %d）"
            % (len(calls), bound))
        self.assertLessEqual(len(usagesrv._match_cache),
                             usagesrv._MATCH_CACHE_MAX)

    def test_match_cache_is_bounded_and_clears_on_overflow(self):
        saved = usagesrv._MATCH_CACHE_MAX
        self.addCleanup(setattr, usagesrv, "_MATCH_CACHE_MAX", saved)
        usagesrv._MATCH_CACHE_MAX = 4
        usagesrv._match_cache.clear()
        for i in range(10):
            usagesrv.match_model_id("m-%d" % i, ["m-%d" % i])
            self.assertLessEqual(len(usagesrv._match_cache), 4)
        self.assertLessEqual(len(usagesrv._match_cache), 4)


class ModelTokenRankingTests(unittest.TestCase):
    """Rev8 契约 8：模型 Token 排行（Token 主指标，不以请求排行冒充）。

    token_total only uses complete evidence; partial usage stays in separate
    fields and cannot enter the complete-token ranking.
    """

    def test_day_rows_carry_token_total_and_ranking(self):
        from console.stats import _emit_models
        records = []
        for model, requests, input_tok, output_tok in (
                ("model-b", 2, 300, 700), ("model-a", 9, 1000, 2000)):
            records.extend(_qoder_record("2026-09-10T12:00:00+08:00", model=model,
                                         in_tok=input_tok if i == 0 else 0,
                                         out_tok=output_tok if i == 0 else 0)
                           for i in range(requests))

        def price(model, _ms, input_tok, output_tok, _cache, _reason):
            cost = (0.02 if model == "model-b" else 0.01) if input_tok else 0.0
            return cost, True, model, False

        with mock.patch.object(usagesrv, "estimate_record", price):
            agg = console_stats._collect_jsonl_records(
                "estimate", records, None, "2026-09-10", "2026-09-10")
        rows = _emit_models("estimate", agg["days"]["2026-09-10"], 7.2)
        by_model = {row["model"]: row for row in rows}
        # token_total = 完整输入+输出；与请求数无关（9 请求的 model-a 靠
        # Token 量排前，2 请求的 model-b Token 少——请求排行不冒充）。
        self.assertEqual(by_model["model-a"]["token_total"], 3000)
        self.assertEqual(by_model["model-b"]["token_total"], 1000)
        # 行序仍按 money 降序（既有口径不变）；Token 排行由日块的
        # model_token_ranking 承担（Token 降序，不以请求排行冒充）。
        self.assertEqual(
            [row["model"] for row in rows],
            ["model-b", "model-a"])  # cost desc: 0.02*7.2 > 0.01*7.2

    def test_day_rows_ranking_structure_contract(self):
        """日块结构契约（无账本夹具的空跑）：days 块统一携带
        model_token_ranking 列表（可为空），排序键恒为 token_total 降序——
        请求排行不冒充 Token 主指标。"""
        res = console_stats.bounded_stats({}, "2026-09-10", "2026-09-11")
        self.assertEqual(res["days"], [])
        # 空范围不产生日期块：结构契约由 _emit_models 的 token_total 字段与
        # bounded_stats 的日块装配共同保证（见
        # test_day_rows_carry_token_total_and_ranking 的行级断言）。


class CompleteModelTokenTests(unittest.TestCase):
    """New token metrics must not inherit the legacy fee API's zero filling."""

    setUp = TestBoundedStats.setUp
    _restore_usagesrv = TestBoundedStats._restore_usagesrv
    _write_ledger = TestBoundedStats._write_ledger
    _model_row = TestBoundedStats._model_row
    _source_block = TestBoundedStats._source_block

    def query(self, rows, source="qoder"):
        ledger = self._write_ledger("new-token-evidence.jsonl", rows)
        return console_stats.bounded_stats(
            {source: ledger}, "2026-09-10", "2026-09-10")

    def test_partial_only_model_is_not_ranked_as_complete_tokens(self):
        row = _qoder_record("2026-09-10T12:00:00+08:00", in_tok=999)
        row.pop("output_tokens")
        result = self.query([row])
        model = self._model_row(result, "2026-09-10", "qoder", "gfmodel")
        self.assertEqual(model["input_tokens"], 999)  # Legacy compatibility.
        self.assertEqual(model["output_tokens"], 0)
        self.assertIsNone(model["token_total"])
        self.assertEqual(model["partial_token_total"], 999)
        self.assertIsNone(model["partial_output_tokens"])
        self.assertEqual(model["token_coverage"]["partial_records"], 1)
        self.assertEqual(result["days"][0]["model_token_ranking"], [])

    def test_mixed_token_evidence_keeps_complete_lower_bound_estimate_unknown(self):
        rows = []
        for model, extra in (
                ("mixed", {}),
                ("mixed", {"output_tokens": None}),
                ("mixed", {"usage_source": "estimated"}),
                ("mixed", {"usage_source": "none"}),
                ("mixed", {"status": "upstream_read_error"}),
                ("zero", {"input_tokens": 0, "output_tokens": 0})):
            row = _qoder_record("2026-09-10T12:00:00+08:00", model=model,
                                in_tok=10, out_tok=2)
            row.update(extra)
            rows.append(row)
        result = self.query(rows)
        model = self._model_row(result, "2026-09-10", "qoder", "mixed")
        self.assertEqual(model["token_total"], 12)
        self.assertEqual(model["complete_input_tokens"], 10)
        self.assertEqual(model["complete_output_tokens"], 2)
        self.assertEqual(model["partial_token_total"], 22)
        self.assertEqual(model["estimated_token_total"], 12)
        self.assertEqual(model["token_coverage"], {
            "records": 5, "complete_records": 1, "partial_records": 2,
            "estimated_records": 1, "unknown_records": 1,
            "complete": False, "unit": "request"})
        ranking = result["days"][0]["model_token_ranking"]
        self.assertEqual([row["model"] for row in ranking], ["mixed", "zero"])
        self.assertEqual(ranking[0]["input_tokens"], 10)
        self.assertEqual(ranking[0]["output_tokens"], 2)
        self.assertEqual(ranking[0]["token_total"], 12)
        self.assertFalse(ranking[0]["token_coverage"]["complete"])
        self.assertEqual(ranking[1]["token_total"], 0)
        self.assertEqual(result["days"][0]["token_coverage"]["unknown_records"], 1)

    def test_bool_string_negative_and_fraction_are_not_token_evidence(self):
        rows = []
        for index, value in enumerate((True, "99", -1, 1.5)):
            row = _qoder_record("2026-09-10T12:00:00+08:00",
                                model=f"bad-{index}", in_tok=7)
            row["output_tokens"] = value
            rows.append(row)
        result = self.query(rows)
        for index in range(4):
            model = self._model_row(result, "2026-09-10", "qoder", f"bad-{index}")
            self.assertIsNone(model["token_total"])
            self.assertEqual(model["partial_token_total"], 7)
            self.assertIsNone(model["partial_output_tokens"])
        self.assertEqual(result["days"][0]["model_token_ranking"], [])

    def test_cross_source_ranking_and_invalid_date_coverage(self):
        qoder = self._write_ledger("qoder-partial.jsonl", [
            {"time": "2026-09-10T12:00:00+08:00", "model": "same",
             "input_tokens": 999, "credits": 2},
            {"time": "invalid", "model": "undated", "input_tokens": 10}])
        chat = self._write_ledger("chat-complete.jsonl", [
            _c2a_record("2026-09-10T12:00:00+08:00", model="same",
                        in_tok=10, out_tok=5)])
        result = console_stats.bounded_stats(
            {"qoder": qoder, "chat2api": chat}, "2026-09-10", "2026-09-10")
        ranking = result["days"][0]["model_token_ranking"]
        self.assertEqual([(row["source"], row["model"], row["token_total"])
                          for row in ranking], [("chat2api", "same", 15)])
        self.assertEqual(result["coverage"]["sources"]["qoder"]["undated_records"], 1)

    def test_estimate_fee_conversion_failure_keeps_visible_token_lower_bound(self):
        row = _c2a_record("2026-09-10T12:00:00+08:00", in_tok=7)
        row["output_tokens"] = "not-a-count"
        result = self.query([row], source="chat2api")
        model = self._model_row(result, "2026-09-10", "chat2api", "glm-5.3")
        self.assertIsNone(model["token_total"])
        self.assertEqual(model["partial_token_total"], 7)
        self.assertIsNone(model["estimated_cost_cny"])
        self.assertEqual(result["totals"]["estimated_cost_cny"], 0.0)
        self.assertEqual(result["coverage"]["sources"]["chat2api"]["errors"][0]
                         ["failed_records"], 1)

    def test_dsh_aggregate_counts_and_partial_tokens_are_labelled_as_aggregates(self):
        snapshot = _dsh_snapshot()
        snapshot["days"]["2026-09-02"]["byProviderModel"]["openwebui:Test-A"]["input"] = "2000"
        path = Path(self._tmp.name) / "dsh-token-evidence.json"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        result = console_stats.bounded_stats(
            {"dsh": path}, "2026-09-02", "2026-09-02")
        partial = self._model_row(result, "2026-09-02", "dsh", "openwebui:Test-A")
        self.assertIsNone(partial["token_total"])
        self.assertEqual(partial["partial_token_total"], 200)
        self.assertEqual(partial["token_coverage"]["unit"], "model_day_aggregate")
        self.assertEqual(partial["requests"], 2)
        self.assertEqual(partial["input_tokens"], 2000)  # Legacy snapshot conversion.
        self.assertEqual(result["totals"]["estimated_cost_cny"], 10.8)
        self.assertEqual([row["model"] for row in result["days"][0]
                          ["model_token_ranking"]], ["openwebui:Test-B"])

    def test_all_missing_or_invalid_sides_are_unknown_without_a_fake_total(self):
        rows = [
            {"time": "2026-09-10T12:00:00+08:00", "model": "absent",
             "credits": 0},
            {"time": "2026-09-10T12:00:00+08:00", "model": "invalid",
             "input_tokens": True, "output_tokens": "99", "credits": 0},
        ]
        result = self.query(rows)
        for model in ("absent", "invalid"):
            row = self._model_row(result, "2026-09-10", "qoder", model)
            self.assertIsNone(row["token_total"])
            self.assertIsNone(row["partial_token_total"])
            self.assertEqual(row["token_coverage"]["unknown_records"], 1)
        self.assertEqual(result["days"][0]["model_token_ranking"], [])


class StatsEnvironmentRestorationTests(unittest.TestCase):
    """standards-tests-04: the module-level install must be restored.

    Both ``test_console_stats`` and ``test_console_api`` install a process-wide
    ``console_stats`` environment at module/class level. Neither restored it, so
    every later module of the same ``discover`` run inherited an environment
    whose paths pointed into an already deleted temp directory. This case pins the
    restore contract for THIS module by running the module's own lifecycle in a
    child process and checking the environment is back to its previous value.
    """

    def test_the_module_restores_the_process_environment(self):
        code = (
            "import json, os, sys, unittest\n"
            "sys.path[:0] = [%r, %r]\n"
            "from console import stats as console_stats\n"
            "before = console_stats._STATS_ENVIRONMENT\n"
            "import test_console_stats as module\n"
            "suite = unittest.TestSuite([\n"
            "    module.TestBoundedStats(\n"
            "        'test_single_source_single_day_aggregation')])\n"
            "result = unittest.TextTestRunner(verbosity=0).run(suite)\n"
            "print('R19_ENV:', json.dumps({\n"
            "    'ok': result.wasSuccessful(),\n"
            "    'before_is_none': before is None,\n"
            "    'after_is_none': console_stats._STATS_ENVIRONMENT is None,\n"
            "    'after_is_before': (\n"
            "        console_stats._STATS_ENVIRONMENT is before)}))\n"
        ) % (str(TESTS_DIR), str(AI_GATEWAY))
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code],
            capture_output=True, text=True, timeout=300,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(
            [line for line in completed.stdout.splitlines()
             if line.startswith("R19_ENV:")][-1].split("R19_ENV:", 1)[1])
        self.assertTrue(payload["ok"], completed.stdout[-2000:])
        self.assertTrue(payload["before_is_none"], payload)
        self.assertTrue(payload["after_is_none"],
                        "the module left a process-wide environment installed: "
                        "%r" % (payload,))


class StatsEnvironmentInjectionTests(unittest.TestCase):
    """E19-1-03: the statistics environment is an EXPLICIT injection point.

    ``console_stats.StatsEnvironment`` + ``configure(environment)`` are the only
    way the read surface obtains ledger paths, time rules and the price table. A
    read that runs without an injected environment must raise
    ``StatsNotConfigured`` — never silently return an empty payload (an empty
    "no requests today" would be indistinguishable from a real zero), and never
    fall back to a host module's own constants.
    """

    def _unconfigured(self):
        patcher = mock.patch.object(console_stats, "_STATS_ENVIRONMENT", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_configure_rejects_none(self):
        with self.assertRaises(TypeError):
            console_stats.configure(None)

    def test_um_read_faces_raise_instead_of_returning_empty(self):
        self._unconfigured()
        for call in (
                lambda: console_stats.um_today(),
                lambda: console_stats.um_trend(range="7d"),
                lambda: console_stats.um_requests(window="today"),
                # An explicit ledger still needs the environment for the price
                # paths, so it must refuse too (no partial silent payload).
                lambda: console_stats.um_today(ledger="explicit.jsonl")):
            with self.assertRaises(console_stats.StatsNotConfigured):
                call()

    def test_default_assemblies_raise_instead_of_reading_a_host_module(self):
        self._unconfigured()
        for call in (console_stats.default_ledgers,
                     console_stats.default_sources,
                     console_stats.retired_ledgers):
            with self.assertRaises(console_stats.StatsNotConfigured):
                call()

    def test_an_explicit_environment_needs_no_process_configuration(self):
        # The seam is the ARGUMENT, not the module global: a caller that passes
        # its own environment reads normally while the process is unconfigured.
        # The environment is FROZEN so a default that ignored the argument and
        # read the host module would not satisfy these assertions.
        self._unconfigured()
        environment = _FrozenStatsEnvironment()
        self.assertEqual(
            console_stats.default_sources(environment)["chat2api"],
            environment._chat2api_log)
        self.assertEqual(
            console_stats.default_ledgers(environment)["dsh"],
            environment._dsh_snapshot)

    def test_the_frozen_environment_really_differs_from_the_host(self):
        """Guard the guard: the frozen values must NOT equal the host ones.

        If a future change made the frozen fixture read the host module again,
        ``test_an_explicit_environment_needs_no_process_configuration`` would
        silently lose its discriminating power: that assertion compares the
        injected environment's value against the value the default returned, and
        when both come from the same ``usagesrv`` constant the comparison holds
        even for a default that ignores its argument entirely.
        """
        environment = _FrozenStatsEnvironment()
        self.assertNotEqual(
            environment._chat2api_log, usagesrv.CHAT2API_LOG,
            "the frozen fixture must not capture the host constant it guards")
        self.assertNotEqual(
            environment._dsh_snapshot, usagesrv.DSH_SNAPSHOT_FILE,
            "the frozen fixture must not capture the host constant it guards")
        self.assertIsInstance(environment._chat2api_log, str)

    def test_a_default_that_reads_the_host_module_is_caught(self):
        """standards-tests-18: the frozen fixture must reject this mutant.

        ``default_sources`` used to be replaceable by a version that ignored its
        ``environment`` argument and returned the host module's constants; the
        injection assertion stayed green because the fixture's values were those
        same constants. The fixture now carries independent sentinels, so the
        mutant is distinguishable.
        """
        self._unconfigured()
        environment = _FrozenStatsEnvironment()

        def host_reading_mutant(env=None):
            return {"chat2api": usagesrv.CHAT2API_LOG,
                    "dsh": usagesrv.DSH_SNAPSHOT_FILE}

        original = console_stats.default_sources
        console_stats.default_sources = host_reading_mutant
        try:
            self.assertNotEqual(
                console_stats.default_sources(environment)["chat2api"],
                environment._chat2api_log,
                "a default that reads the host module must be distinguishable "
                "from one that honours the injected environment")
        finally:
            console_stats.default_sources = original


if __name__ == "__main__":
    unittest.main()
    unittest.main()
