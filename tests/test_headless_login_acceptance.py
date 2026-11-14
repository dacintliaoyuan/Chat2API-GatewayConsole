# -*- coding: utf-8 -*-
"""Ticket #16：headless 登录的行为验收与 RSS 基线记录契约。

验收记录是交付物：这里只读 docs/headless-login-acceptance.md，不启动
浏览器、不重启服务、不写运行时状态。每条用例对应 ticket #16 的一个验收条件。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "headless-login-acceptance.md"

ACCEPTANCE_MODULES = (
    "tests.test_chat2api_browser_login",
    "tests.test_chat2api_reauth_recovery",
    "tests.test_chat2api_auth",
    "tests.test_console_um_login",
    "tests.test_console_um_adapter",
    "tests.test_cloud_display_on_demand",
    "tests.test_domain_docs_headless_login",
    "tests.test_console_display_route",
)


def _record():
    if not RECORD.is_file():
        raise AssertionError("缺少验收记录：%s" % RECORD)
    return RECORD.read_text(encoding="utf-8")


def _section(text, name):
    """Return one level-2 section's body, up to the next level-2 heading."""
    match = re.search(r"^## %s[^\n]*$" % re.escape(name), text,
                      re.MULTILINE)
    if match is None:
        raise AssertionError("验收记录缺少小节：%s" % name)
    rest = text[match.end():]
    following = re.search(r"^## ", rest, re.MULTILINE)
    return rest[:following.start()] if following else rest


class BehaviorAcceptanceTests(unittest.TestCase):
    def test_every_ticket_has_a_passing_behavior_check(self):
        section = _section(_record(), "行为验收")
        for ticket in range(9, 16):
            self.assertIn("#%d" % ticket, section)
        self.assertIn("Ran 236 tests", section)
        self.assertIn("OK (skipped=1)", section)

    def test_the_record_names_the_acceptance_command_and_its_workdir(self):
        section = _section(_record(), "行为验收")
        self.assertIn("python -m unittest", section)
        self.assertIn(".worktrees\\16", section)
        for module in ACCEPTANCE_MODULES:
            self.assertIn(module, section)
        self.assertIn("RealDomLoginPageTests", section)
        self.assertIn("chromium_headless_shell", section)


class BaselineRecordTests(unittest.TestCase):
    def test_baseline_records_command_workdir_version_and_results(self):
        section = _section(_record(), "RSS 基线")
        self.assertIn("工作目录", section)
        self.assertIn("cd /", section)
        self.assertIn("ps -o rss=", section)
        self.assertIn("s2-20261113b", section)
        self.assertIn("e2d60349+reviewed", section)
        self.assertIn("Ubuntu 26.04.1 LTS", section)
        self.assertIn("Python 3.14.4", section)

    def test_baseline_covers_every_named_process_and_phase(self):
        section = _section(_record(), "RSS 基线")
        for name in ("8100", "18096", "Xvfb", "x11vnc", "Chromium"):
            self.assertIn(name, section)
        for phase in ("idle", "登录中", "登录后"):
            self.assertIn(phase, section)
        for value in ("177,496", "179,868", "113,220", "110,544",
                       "24,924", "24,704", "20,120", "19,900",
                       "852,228"):
            self.assertIn(value, section)


class NumericTargetTests(unittest.TestCase):
    def test_numeric_targets_are_deferred_until_after_the_baseline(self):
        section = _section(_record(), "数值目标")
        self.assertIn("基线后", section)
        self.assertIn("不在本 ticket 设定", section)


class UnverifiedScopeTests(unittest.TestCase):
    def test_unverified_scope_is_explicit(self):
        section = _section(_record(), "未验证范围")
        for phrase in ("真实 headless", "真实 systemd", "真实 UM 登录",
                       "Playwright", "headless_shell", "部署后"):
            self.assertIn(phrase, section)


if __name__ == "__main__":
    unittest.main()
