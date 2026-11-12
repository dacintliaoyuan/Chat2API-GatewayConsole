# -*- coding: utf-8 -*-
"""``request_budget_estimate.py``（动作门 §④ 费用上限预估）的隔离夹具测试。

覆盖四条契约：

* **两个 token 口径**：已知模型在「默认口径」（驱动工具的 DEFAULT_PROMPT 字符数
  + DEFAULT_MAX_TOKENS）与「显式口径」（``--prompt-tokens``/``--completion-tokens``）
  下各算出一个确定的 USD 上限；
* **缺价即拒绝**：模型不在价目表里、或价目表把它标为 ``unpriced`` → 退出码 2，
  费用字段是 ``None``，**不是 0**；
* **请求数为 0 或负数即拒绝**：``--requests 0`` / ``-1`` → 退出码 2；
* **上限随 N 线性**：N=4 的合计恰好是 N=1 的 4 倍，逐请求上限不随 N 变；
* **完成后入口状态默认关闭**（维护者 2026-09-28 裁定）：默认声明「测试后关闭公网分享入口
  （8100 停止、Funnel 关闭）」，可用 ``--post-entry-state`` 显式覆盖，且输出保留
  「该阈值是执行停止线，不是上游账单保证。」这句；
* **关闭次序**：终态是「8100 停止 + Funnel 关闭」，但次序必须是「先关 Funnel、8100 保持监听
  到驱动运行后判据核对完成、再停 8100」——先停 8100 会让驱动 gate_check 的运行后两门判据
  FAIL（``metering_request_probe.py:239-262``、``metering_acceptance.py:85-86``）。

隔离：价目表夹具写在 ``tempfile`` 临时目录里并直接打补丁到 ``usagesrv.PRICE_FILE``
（``tests/test_usage_service.py:114-131`` 的既有先例）+ 重置 ``_price_cache``；不读生产
账本、不写仓库 ``data/``、不发任何请求、不重启任何服务。价目表时间戳一律显式给出，
不依赖真实系统时钟。

运行（工作目录 ``ai-gateway/``）：

    python -B -m unittest tests.test_request_budget_estimate -q
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import usagesrv  # noqa: E402 — 与工具同一个模块对象，补丁才生效

TOOLS_DIR = HERE / "tools" / "um-share"
TOOL_PATHS = {
    "request_budget_estimate": TOOLS_DIR / "readonly" / "request_budget_estimate.py",
    "metering_request_probe": TOOLS_DIR / "actions" / "metering_request_probe.py",
}


def _load(module_name):
    path = TOOL_PATHS[module_name]
    spec = importlib.util.spec_from_file_location(
        "um_share_" + module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


budget = _load("request_budget_estimate")
probe = _load("metering_request_probe")

# 显式时刻：2026-09-02T02:00:00Z（峰时段窗口 01-04 UTC 内，且晚于
# LEGACY_BASE_BOUNDARY 2026-08-16T16:00:00Z）——不依赖真实系统时钟。
NOW_MS = usagesrv._parse_ts("2026-09-02T02:00:00Z")

# 夹具模型的三个档位：基础档与谷档同价、峰档翻倍；峰档在 NOW_MS 可达，
# 因此「逐桶取最大」应得到峰档单价（输入 4.0、输出 6.0 USD/百万 token）。
PEAK_MODEL = {
    "cacheHit": 1.0, "cacheMiss": 2.0, "output": 3.0,
    "offPeak": {"cacheHit": 1.0, "cacheMiss": 2.0, "output": 3.0},
    "peak": {"cacheHit": 2.0, "cacheMiss": 4.0, "output": 6.0},
}
FLAT_MODEL = {"input": 10.0, "cachedInput": 1.0, "output": 20.0}
PEAK_WINDOWS = [{"start": 1, "end": 4}, {"start": 6, "end": 10}]


def fixture_prices():
    return {
        "checkedAt": "2026-09-27",
        "exchangeRate": 7.2,
        "peak": {"enabled": True, "effectiveAt": "2026-08-01T00:00:00Z",
                 "windows": PEAK_WINDOWS},
        "models": {"fixture-peak-model": dict(PEAK_MODEL),
                   "fixture-unpriced-model": {"unpriced": True}},
        "providers": {"fixture-provider": {"models": {
            "fixture-flat-model": dict(FLAT_MODEL)}}},
    }


class BudgetFixtureTest(unittest.TestCase):
    """价目表与缓存重定向到临时夹具；每个用例自行重置价格缓存。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="budget-estimate-")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.prices_path = base / "prices.json"
        self.override_path = base / "price.override.json"
        self.prices_path.write_text(json.dumps(fixture_prices()),
                                    encoding="utf-8")
        for attr, path in (("PRICE_FILE", self.prices_path),
                           ("OVERRIDE_FILE", self.override_path)):
            patcher = mock.patch.object(usagesrv, attr, str(path))
            patcher.start()
            self.addCleanup(patcher.stop)
        cache = mock.patch.object(usagesrv, "_price_cache",
                                 {"at": 0.0, "data": None})
        cache.start()
        self.addCleanup(cache.stop)

    @staticmethod
    def run_cli(argv):
        """跑 CLI，捕获 stdout；返回 ``(exit_code, text)``。"""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = budget.main(argv)
        return code, out.getvalue()


class KnownModelTwoTokenBasesTest(BudgetFixtureTest):
    """(a) 已知模型的两个 token 口径各算出一个确定的 USD 上限。"""

    def test_default_basis_matches_probe_declaration(self):
        basis = budget.default_token_basis()
        self.assertEqual(basis["source"], "metering_request_probe")
        self.assertEqual(basis["prompt_tokens"], len(probe.DEFAULT_PROMPT))
        self.assertEqual(basis["completion_tokens"],
                         probe.DEFAULT_MAX_TOKENS)

    def test_peak_tier_ceiling_and_total(self):
        result = budget.estimate("fixture-peak-model", 1, 4, 16, 0, NOW_MS)
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_code"], 0)
        # 峰档 4.0/6.0：4×4.0 + 16×6.0 = 112 → 112e-6 USD。
        self.assertEqual(result["rates"]["input"], "4")
        self.assertEqual(result["rates"]["output"], "6")
        self.assertEqual(result["per_request_usd"], "0.000112")
        self.assertEqual(result["total_usd"], "0.000112")
        self.assertEqual(result["total_cny"], "0.0008064")
        self.assertEqual(result["approval_usd"], "0.01")

    def test_explicit_tokens_override_the_default_basis(self):
        result = budget.estimate("fixture-peak-model", 1, 100, 50, 0, NOW_MS)
        # 100×4.0 + 50×6.0 = 700 → 700e-6 USD。
        self.assertEqual(result["per_request_usd"], "0.0007")
        self.assertEqual(result["total_usd"], "0.0007")

    def test_flat_provider_tier_prices_input_bucket(self):
        result = budget.estimate("fixture-flat-model", 1, 4, 16, 0, NOW_MS)
        # flat 档：输入桶取 max(cacheMiss 10, cacheHit 1) = 10，输出 20。
        self.assertEqual(result["rates"]["input"], "10")
        self.assertEqual(result["rates"]["output"], "20")
        self.assertEqual(result["per_request_usd"], "0.00036")

    def test_cli_prints_four_declaration_lines(self):
        code, text = self.run_cli(["--model", "fixture-flat-model",
                                  "--requests", "2", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(text)
        lines = payload["declaration"]["lines"]
        self.assertEqual(len(lines), 4)
        for item in ("请求数", "模型", "费用上限", "完成后入口状态"):
            self.assertTrue(any(item in line for line in lines),
                            "声明块缺少 %r：%r" % (item, lines))
        self.assertFalse(payload["declaration"]["packaged_with_8096_window"])
        self.assertIn("互不打包", payload["declaration"]["note"])
        self.assertEqual(payload["total_usd"], "0.00072")


class MissingPriceRefusalTest(BudgetFixtureTest):
    """(b) 价目表缺该模型 → 退出码 2，费用字段是 None（不是 0）。"""

    def test_missing_model_refuses_with_exit_2(self):
        code, text = self.run_cli(["--model", "no-such-model",
                                  "--requests", "1"])
        self.assertEqual(code, 2)
        self.assertIn("not_in_table", text)
        self.assertIn("绝不用 0 冒充", text)
        self.assertNotIn("USD 0", text)

    def test_missing_model_json_total_is_none_not_zero(self):
        code, text = self.run_cli(["--model", "no-such-model",
                                  "--requests", "1", "--json"])
        self.assertEqual(code, 2)
        payload = json.loads(text)
        self.assertIsNone(payload["total_usd"])
        self.assertIsNone(payload["per_request_usd"])
        self.assertEqual(payload["exit_code"], 2)

    def test_explicitly_unpriced_model_refuses_with_its_own_reason(self):
        result = budget.estimate("fixture-unpriced-model", 1, 4, 16, 0, NOW_MS)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason_code"], "explicitly_unpriced")
        self.assertIsNone(result["total_usd"])

    def test_engine_returns_none_for_these_models(self):
        """反向核对：拒绝不是因为工具自己出错——引擎本来就给不出价。"""
        entry, _mode, _matched, _source = usagesrv.resolve_price(
            "fixture-unpriced-model", NOW_MS)
        self.assertIsNone(entry)
        entry, _mode, _matched, _source = usagesrv.resolve_price(
            "no-such-model", NOW_MS)
        self.assertIsNone(entry)


class RequestCountRefusalTest(BudgetFixtureTest):
    """(c) 请求数为 0 或负数 → 拒绝（退出码 2）。"""

    def test_zero_requests_refused(self):
        code, text = self.run_cli(["--model", "fixture-peak-model",
                                  "--requests", "0"])
        self.assertEqual(code, 2)
        self.assertIn("bad_arguments", text)
        self.assertIn("不可判定", text)

    def test_negative_requests_refused(self):
        code, text = self.run_cli(["--model", "fixture-peak-model",
                                  "--requests", "-1"])
        self.assertEqual(code, 2)
        self.assertIn("bad_arguments", text)

    def test_pure_layer_refuses_zero_and_negative_without_pricing(self):
        for count in (0, -1):
            result = budget.estimate("fixture-peak-model", count, 4, 16, 0, NOW_MS)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason_code"], "bad_arguments")
            self.assertIsNone(result["total_usd"])

    def test_negative_token_counts_refused(self):
        result = budget.estimate("fixture-peak-model", 1, -1, 16, 0, NOW_MS)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "bad_arguments")


class LinearInRequestsTest(BudgetFixtureTest):
    """(d) 上限随 N 线性：N=4 合计 = 4 × N=1 合计。"""

    def test_total_scales_with_requests(self):
        one = budget.estimate("fixture-peak-model", 1, 4, 16, 0, NOW_MS)
        four = budget.estimate("fixture-peak-model", 4, 4, 16, 0, NOW_MS)
        self.assertEqual(one["total_usd"], "0.000112")
        self.assertEqual(four["total_usd"], "0.000448")
        self.assertEqual(four["per_request_usd"], one["per_request_usd"])
        from decimal import Decimal
        self.assertEqual(Decimal(four["total_usd"]),
                         Decimal(one["total_usd"]) * 4)

    def test_cli_json_is_linear_too(self):
        codes, payloads = [], []
        for count in (1, 4):
            code, text = self.run_cli(["--model", "fixture-flat-model",
                                      "--requests", str(count), "--json"])
            codes.append(code)
            payloads.append(json.loads(text))
        self.assertEqual(codes, [0, 0])
        from decimal import Decimal
        self.assertEqual(Decimal(payloads[1]["total_usd"]),
                         Decimal(payloads[0]["total_usd"]) * 4)
        self.assertEqual(payloads[1]["per_request_usd"],
                         payloads[0]["per_request_usd"])


class CeilingIsUpperBoundTest(BudgetFixtureTest):
    """上限必须是上界：不低于引擎在同一窗口内任一时刻的实际计价。"""

    def test_ceiling_not_below_any_sampled_time(self):
        result = budget.estimate("fixture-peak-model", 1, 4, 16, 0, NOW_MS,
                                 horizon_days=2)
        self.assertTrue(result["ok"])
        ceiling = float(result["total_usd"])
        at, end = NOW_MS, NOW_MS + 2 * 86_400_000
        while at <= end:
            cost, priced, _matched, _peak, _version = \
                usagesrv.estimate_record_with_version(
                    "fixture-peak-model", at, 4, 16, 0, 0)
            self.assertTrue(priced)
            self.assertLessEqual(cost, ceiling * (1 + 1e-9))
            at += 3_600_000

    def test_ceiling_rates_cover_every_reachable_tier(self):
        tiers, _samples = budget.reachable_tiers(
            usagesrv.normalize_price(PEAK_MODEL), {"enabled": True,
                                                  "effectiveAtMs": NOW_MS - 1,
                                                  "windows": PEAK_WINDOWS},
            NOW_MS, horizon_days=2)
        rates = budget.ceiling_rates(tiers)
        for tier in tiers:
            self.assertLessEqual(tier["cacheMiss"], float(rates["input"]))
            self.assertLessEqual(tier["cacheHit"], float(rates["input"]))
            self.assertLessEqual(tier["output"], float(rates["output"]))
        self.assertEqual(rates["input"], Decimal("4"))
        self.assertEqual(rates["output"], Decimal("6"))


class PostEntryStateDefaultTest(BudgetFixtureTest):
    """(e) 完成后入口状态：默认必须是「测试后关闭」，可显式覆盖。"""

    def test_default_declares_closed_entry(self):
        self.assertIn("关闭", budget.POST_ENTRY_STATE_DEFAULT)
        self.assertIn("8100 停止", budget.POST_ENTRY_STATE_DEFAULT)
        self.assertIn("Funnel 关闭", budget.POST_ENTRY_STATE_DEFAULT)
        # 驱动工具的等价取值：closed（驱动工具自身默认仍是 unchanged，本工具不改它）。
        self.assertEqual(budget.POST_ENTRY_STATE_PROBE_FLAG, "closed")

    def test_cli_declaration_line_uses_closed_default(self):
        code, text = self.run_cli(["--model", "fixture-flat-model",
                                  "--requests", "1"])
        self.assertEqual(code, 0)
        self.assertIn(
            "[声明 4/4] 完成后入口状态：" + budget.POST_ENTRY_STATE_DEFAULT,
            text)
        self.assertIn(budget.THRESHOLD_IS_STOP_LINE_NOTE, text)
        self.assertIn("不是上游账单保证", text)

    def test_json_default_and_explicit_override(self):
        code, text = self.run_cli(["--model", "fixture-flat-model",
                                  "--requests", "1", "--json"])
        self.assertEqual(code, 0)
        declaration = json.loads(text)["declaration"]
        self.assertEqual(declaration["post_entry_state"],
                         budget.POST_ENTRY_STATE_DEFAULT)
        self.assertEqual(declaration["post_entry_state_probe_flag"], "closed")
        self.assertEqual(declaration["threshold_note"],
                         budget.THRESHOLD_IS_STOP_LINE_NOTE)
        # 加性：显式覆盖仍然允许。
        code, text = self.run_cli(["--model", "fixture-flat-model",
                                  "--requests", "1", "--json",
                                  "--post-entry-state", "测试后保持开启"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["declaration"]["post_entry_state"],
                         "测试后保持开启")

    def test_refusal_path_keeps_threshold_note(self):
        code, text = self.run_cli(["--model", "no-such-model",
                                  "--requests", "1"])
        self.assertEqual(code, 2)
        self.assertIn(
            "[声明 4/4] 完成后入口状态：" + budget.POST_ENTRY_STATE_DEFAULT,
            text)
        self.assertIn(budget.THRESHOLD_IS_STOP_LINE_NOTE, text)

    def test_order_note_states_funnel_first_then_stop_8100(self):
        """关闭次序：终态是「8100 停止 + Funnel 关闭」，但先关 Funnel、8100 留到驱动核对完成。"""
        code, text = self.run_cli(["--model", "fixture-flat-model",
                                  "--requests", "1"])
        self.assertEqual(code, 0)
        self.assertIn("[关闭次序] " + budget.POST_ENTRY_STATE_ORDER_NOTE, text)
        # 次序必须写明「先关 Funnel」「8100 保持监听」「再停 8100」三件事，且点名驱动会 FAIL。
        note = budget.POST_ENTRY_STATE_ORDER_NOTE
        self.assertIn("先关 Funnel", note)
        self.assertIn("保持监听", note)
        self.assertIn("再停 8100", note)
        self.assertIn("FAIL", note)
        # JSON 里同键给出，机器读面也能看到次序。
        code, text = self.run_cli(["--model", "fixture-flat-model",
                                  "--requests", "1", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(text)["declaration"]["post_entry_state_order_note"],
            note)
        # 拒绝路径也保留次序说明（它同样是只读声明的一部分）。
        code, text = self.run_cli(["--model", "no-such-model",
                                  "--requests", "1"])
        self.assertEqual(code, 2)
        self.assertIn(budget.POST_ENTRY_STATE_ORDER_NOTE, text)


if __name__ == "__main__":
    unittest.main()
