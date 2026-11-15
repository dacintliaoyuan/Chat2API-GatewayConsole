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
ACCEPTANCE = ROOT / "docs" / "headless-login-acceptance.md"
CHAT2API = ROOT / "chat2api" / "chat2api.py"
CONSOLE_ADAPTERS = ROOT / "console" / "adapters.py"


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


class TerminologyDriftTests(unittest.TestCase):
    def test_acceptance_record_uses_the_glossary_term(self):
        # ticket #42 item 5：GLOSSARY.md 把「登录窗口」标为 Avoid；验收记录
        # 必须使用「人工接管窗口截止时间」而不是旧词。
        self.assertTrue(ACCEPTANCE.is_file(), "缺少验收记录：%s" % ACCEPTANCE)
        text = ACCEPTANCE.read_text(encoding="utf-8")
        self.assertIn("人工接管窗口截止时间", text)
        self.assertNotIn("登录窗口截止时间", text)

    def test_real_cloud_login_is_marked_as_out_of_scope(self):
        # ticket #42 item 9：真实云端登录是旧 release 的记录，明确标注
        # 「旧 release、非本票验收」，本票不再执行新的真实登录。
        text = ACCEPTANCE.read_text(encoding="utf-8")
        self.assertIn("旧 release、非本票验收", text)
        self.assertIn("不再执行新的真实云端登录", text)

    def test_acceptance_record_contains_the_41_verification(self):
        # ticket #42 item 20：#41 的测试命令、工作目录、结果与未验证范围
        # 必须写进文档，而不是只留在 commit/报告里。
        text = ACCEPTANCE.read_text(encoding="utf-8")
        self.assertIn("#41 medium 修复验证记录", text)
        self.assertIn("integration-headless-fixes", text)
        self.assertIn("Ran 2310 tests", text)
        self.assertIn("Ran 150 tests", text)
        self.assertIn("未验证范围", text)

    def test_comments_do_not_bind_login_to_a_visible_session(self):
        # ticket #42 item 6：重新认证会话不再绑定 visible window/session；
        # 注释写成「一次登录交互（可见或 headless）」。
        chat2api_text = CHAT2API.read_text(encoding="utf-8")
        adapters_text = CONSOLE_ADAPTERS.read_text(encoding="utf-8")
        self.assertNotIn("visible session", chat2api_text)
        self.assertNotIn("Edge 窗口由用户完成", adapters_text)
        self.assertIn("one login interaction (visible or headless)",
                      chat2api_text)
        self.assertIn("一次登录交互（可见或 headless）", adapters_text)


if __name__ == "__main__":
    unittest.main()
