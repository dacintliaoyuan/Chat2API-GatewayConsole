# -*- coding: utf-8 -*-
"""Ticket #9：headless 登录的术语与 ADR 契约。

GLOSSARY.md 与 docs/adr/ 是本 ticket 的交付物；这里只读取文本，不启动
浏览器、不触碰运行时状态。每条用例对应 spec #8 的一个验收事实。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLOSSARY = ROOT / "GLOSSARY.md"
ADR = ROOT / "docs" / "adr" / "0002-headless-first-um-login.md"


def _term_body(text, name):
    """Return one glossary entry's body, up to the next term heading."""
    match = re.search(r"^\*\*%s[^*]*\*\*:\s*$" % re.escape(name),
                      text, re.MULTILINE)
    if match is None:
        raise AssertionError("GLOSSARY.md 缺少术语：%s" % name)
    rest = text[match.end():]
    following = re.search(r"^\*\*", rest, re.MULTILINE)
    return rest[:following.start()] if following else rest


class ReauthenticationTermTests(unittest.TestCase):
    def test_reauthentication_session_is_not_bound_to_a_visible_window(self):
        body = _term_body(GLOSSARY.read_text(encoding="utf-8"),
                          "重新认证会话")
        self.assertIn("一次登录交互（可见或 headless）", body)
        self.assertNotIn("可见 Edge 登录交互", body)


class LoginPresentationTermTests(unittest.TestCase):
    def test_login_presentation_mode_matches_the_spec(self):
        body = _term_body(GLOSSARY.read_text(encoding="utf-8"),
                          "登录呈现方式")
        self.assertIn("headless-first", body)
        self.assertIn("DISPLAY", body)
        self.assertIn("WAYLAND_DISPLAY", body)
        self.assertIn("复用", body)
        self.assertIn("profile", body)


class ManualTakeoverTermTests(unittest.TestCase):
    def test_manual_takeover_matches_the_spec(self):
        body = _term_body(GLOSSARY.read_text(encoding="utf-8"), "人工接管")
        self.assertIn("manual_takeover_required:<reason>", body)
        self.assertIn("释放", body)
        self.assertIn("display", body)
        self.assertIn("duo_prompt_waiting", body)
        self.assertIn("继续等待", body)


class SingleLoginBrowserTermTests(unittest.TestCase):
    def test_single_login_browser_matches_the_spec(self):
        body = _term_body(GLOSSARY.read_text(encoding="utf-8"),
                          "登录浏览器单实例")
        self.assertIn("profile", body)
        self.assertIn("文件锁", body)
        self.assertIn("flock", body)
        self.assertIn("msvcrt.locking", body)
        self.assertIn("SIGTERM", body)
        self.assertIn("SIGKILL", body)
        self.assertIn("pid", body)
        self.assertIn("启动时间", body)


class AdrTests(unittest.TestCase):
    def test_adr_records_the_headless_first_decisions(self):
        self.assertTrue(ADR.is_file(), "缺少 ADR：%s" % ADR)
        text = ADR.read_text(encoding="utf-8")
        self.assertIn("headless-first", text)
        self.assertIn("按需", text)
        self.assertIn("profile", text)
        self.assertIn("纯 HTTP", text)
        self.assertIn("否决", text)
        self.assertIn("systemctl start", text)


if __name__ == "__main__":
    unittest.main()
