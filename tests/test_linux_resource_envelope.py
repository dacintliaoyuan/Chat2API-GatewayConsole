# -*- coding: utf-8 -*-
"""Linux resource-envelope driver 的可移植回归。

`linux_resource_envelope.py` 是 Linux-only 的端到端测量入口，Windows 不能启动
systemd scope。本文件只测它不依赖 /proc、cgroup 或 systemd 的纯函数，使资源
脚本的结论/阈值/文本逻辑在 Windows 与 Linux 的 Python regression 中都被发现；
真实 2G/4G scope 仍由 `run-linux-checks.sh --mode resource` 执行并标为未在
Windows 验证。
"""

import importlib.util
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))


def _load(name, path):
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


resource = _load("linux_resource_envelope",
                 TESTS / "linux_resource_envelope.py")


class TestLongText(unittest.TestCase):
    def test_long_text_has_the_requested_size_and_a_varied_tail(self):
        text = resource._long_text(4096)
        self.assertEqual(len(text), 4096)
        self.assertTrue(text)
        # The tail must not be a 1-8 char repeat; the driver's own note says
        # the tail is never a short repeated fragment.
        tail = text[-8:]
        self.assertNotEqual(tail, tail[0] * len(tail))
        self.assertGreater(len(set(text.split())), 8)

    def test_long_text_is_deterministic(self):
        self.assertEqual(resource._long_text(1024),
                         resource._long_text(1024))


class TestNumericHelpers(unittest.TestCase):
    def test_bytes_to_mb_and_round(self):
        self.assertEqual(resource._bytes_to_mb(1024 * 1024), 1.0)
        self.assertIsNone(resource._bytes_to_mb(None))
        self.assertEqual(resource._round(1.234), 1.23)
        self.assertIsNone(resource._round(None))

    def test_event_delta_handles_missing_values(self):
        self.assertEqual(resource._event_delta(
            {"cgroup": {"events": {"oom": 1}}},
            {"cgroup": {"events": {"oom": 3}}}, "oom"), 2)
        self.assertEqual(resource._event_delta({}, {}, "oom"), 0)
        self.assertEqual(resource._event_delta(
            {"cgroup": {"events": {"oom": "bad"}}}, {}, "oom"), 0)


class TestPeaks(unittest.TestCase):
    def test_peak_ignores_none_and_empty(self):
        samples = [{"app": {"rssMb": None}},
                   {"app": {"rssMb": 12.5}},
                   {"app": {"rssMb": 3.0}}]
        self.assertEqual(resource._peak(
            samples, lambda sample: sample["app"]["rssMb"]), 12.5)
        self.assertIsNone(resource._peak([], lambda sample: 1.0))

    def test_peak_sample_returns_the_owning_sample(self):
        samples = [{"app": {"rssMb": 1.0}, "name": "a"},
                   {"app": {"rssMb": 5.0}, "name": "b"},
                   {"app": {"rssMb": None}, "name": "c"}]
        self.assertEqual(resource._peak_sample(
            samples, lambda sample: sample["app"]["rssMb"])["name"], "b")
        self.assertIsNone(resource._peak_sample(
            [], lambda sample: sample["app"]["rssMb"]))


class TestScopeConclusion(unittest.TestCase):
    def _group(self, **overrides):
        group = {
            "memoryMaxBytes": 2 * 1024 ** 3,
            "cgroup_peak_mb": 700.0,
            "oom": 0,
            "oom_kill": 0,
            "failures": [],
        }
        group.update(overrides)
        return group

    def test_passing_scope_reports_headroom(self):
        text = resource._scope_conclusion(self._group())
        self.assertTrue(text.startswith("达标（本样本）"), text)

    def test_failures_dominate(self):
        text = resource._scope_conclusion(
            self._group(failures=[{"scenario": "x", "error": "boom"}]))
        self.assertTrue(text.startswith("不达标"), text)

    def test_missing_peak_is_incomplete_evidence(self):
        text = resource._scope_conclusion(
            self._group(cgroup_peak_mb=None))
        self.assertTrue(text.startswith("证据不完整"), text)

    def test_oom_is_a_failure(self):
        text = resource._scope_conclusion(self._group(oom=1))
        self.assertTrue(text.startswith("不达标"), text)

    def test_small_headroom_is_reported(self):
        text = resource._scope_conclusion(
            self._group(cgroup_peak_mb=1990.0))
        self.assertTrue(text.startswith("达标但余量不足"), text)


class TestOverallConclusion(unittest.TestCase):
    def test_two_gigabyte_failure_selects_the_test_only_candidate(self):
        groups = [
            {"limit": "2G", "failures": [{"error": "boom"}],
             "cgroup_peak_mb": 1900.0, "rss_peak_mb": 500.0,
             "breakdown": {"outsideScopeUsedPeakMb": 800.0}},
            {"limit": "4G", "failures": [], "oom": 0, "oom_kill": 0,
             "rss_peak_mb": 500.0,
             "breakdown": {"outsideScopeUsedPeakMb": 800.0}},
        ]
        conclusion = resource._overall_conclusion(groups)
        self.assertTrue(conclusion["2G"].startswith("不达标"))
        self.assertIsNotNone(conclusion["testOnlyCandidate"])
        self.assertIn("4G", conclusion["testOnlyCandidate"]["fourGbComparison"])

    def test_missing_group_is_not_reported_as_passing(self):
        conclusion = resource._overall_conclusion([])
        self.assertEqual(conclusion["2G"], "未测量")
        self.assertEqual(conclusion["4G"], "未测量")
        self.assertIsNone(conclusion["testOnlyCandidate"])


if __name__ == "__main__":
    unittest.main()
