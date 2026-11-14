# -*- coding: utf-8 -*-
"""A13-1/A13-3 browser login state machine: offline page fakes only.

The REAL ``browser_login`` loop is driven here by an offline page/context
stand-in that exposes exactly the surface ``_LoginPage`` touches
(``url``/``goto``/``evaluate``/``reload``/``wait_for_timeout``/``locator``),
so the two-step jump, the prefill gate, the unknown-origin gate, a swallowed
click, the Duo wait, token rejection, timeout and cancellation are all
exercised without Edge, Playwright or any real network. The last section repeats
the selector-matching contract on a real DOM, because "one control answering
several allowed selectors" is not something a synthetic element dict can prove.

Nothing here reads an account or a password back out of the fake: the stand-in
carries "filled / not filled" as a value the session only ever tests for
non-emptiness, and the session must never print, return or store it.
"""

import atexit
import http.client
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))

# Must be set before chat2api is imported: the module resolves the control
# token file location from the environment at import time.
_CONTROL_TMP = tempfile.mkdtemp(prefix="chat2api-login-window-")
os.environ["CHAT2API_CONTROL_TOKEN_FILE"] = os.path.join(
    _CONTROL_TMP, "admin-control-token")

CHAT2API_PATH = Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py"
SPEC = importlib.util.spec_from_file_location("chat2api_login_under_test",
                                              CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)

import _chat2api_http_harness as harness  # noqa: E402

BASE_URL = "https://chat.genai.um.edu.mo"
OPENWEBUI_URL = "https://chat.genai.um.edu.mo/auth?redirect=%2F"
OPENWEBUI_HOME = "https://chat.genai.um.edu.mo/"
UMPASS_URL = "https://websso.um.edu.mo/adfs/ls/"
DUO_URL = "https://api-1234.duosecurity.com/prompt/xyz"
LOOKALIKE_URL = "https://websso.um.edu.mo.evil.example/adfs/ls/"

SSO_BUTTON = chat2api.OPENWEBUI_SSO_BUTTON_SELECTORS[0]
ACCOUNT_FIELD = chat2api.UMPASS_ACCOUNT_SELECTORS[0]
PASSWORD_FIELD = chat2api.UMPASS_PASSWORD_SELECTORS[0]
SUBMIT_BUTTON = chat2api.UMPASS_SUBMIT_SELECTORS[0]
MFA_MARKER = chat2api.UMPASS_MFA_SELECTORS[0]

ACCOUNT = "uc59xxxx@um.edu.mo"
PASSWORD = "offline-placeholder"


class _FakeElement:
    def __init__(self, site=None, selector=None, visible=True, value="",
                 on_click=None):
        self.site = site
        self.selector = selector
        self.visible = visible
        self.value = value
        self.on_click = on_click

    def is_visible(self):
        return self.visible

    def input_value(self):
        if self.value is None:
            raise RuntimeError("no value for this element")
        return self.value

    def click(self, timeout=None):
        if self.site is not None:
            self.site.clicks.append(self.selector)
        if self.on_click:
            self.on_click()

    def element_handle(self):
        """Identity token of this control.

        In the fake the element object IS the DOM node, so the same object
        registered under two selectors is ONE control and two objects are two
        controls - exactly the distinction the real page draws with
        ``el === other``.
        """
        return self


class _FakeLocator:
    def __init__(self, elements):
        self._elements = elements

    def count(self):
        return len(self._elements)

    def nth(self, index):
        return self._elements[index]


class _FakeSite:
    """Offline stand-in for the visible page.

    Only the surface ``_LoginPage`` touches, driven by clicks the same way a
    browser would be. ``script`` holds one step applied per wait, so a page can
    navigate, or a human can finish the form, while the session waits on it.
    """

    def __init__(self, url, elements=None, token=None):
        self.url = url
        self.elements = dict(elements or {})
        self.token = token
        self.script = []
        self.on_reload = None
        self.goto_calls = []
        self.clicks = []
        self.reloads = 0
        self.waits = 0

    def navigate(self, url):
        self.url = url
        # A real navigation replaces the document, so the previous page's
        # controls are gone: a control the session already clicked can never
        # be "still visible" on the next page.
        self.elements = {}

    def set_token(self, token):
        self.token = token

    def bring_to_front(self):
        pass

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append(url)

    def evaluate(self, script):
        if "removeItem" in script:
            self.token = None
            return None
        return self.token

    def reload(self, wait_until=None, timeout=None):
        self.reloads += 1
        if self.on_reload:
            self.on_reload(self)

    def wait_for_timeout(self, ms):
        self.waits += 1
        if self.script:
            self.script.pop(0)(self)

    def locator(self, selector):
        if selector not in self.elements:
            # A selector the scenario never declared: the real Playwright
            # raises here too (malformed selector), and the state machine
            # must treat that as "control not found", never as a click.
            raise RuntimeError("unknown selector: %s" % selector)
        return _FakeLocator(self.elements[selector])


class _FakeContext:
    def __init__(self, page):
        self.pages = [page]
        self.closed = False

    def new_page(self):
        raise AssertionError("the session must reuse the existing page")

    def close(self):
        self.closed = True


def _element(selector, site, *, value="", visible=True, on_click=None):
    return _FakeElement(site=site, selector=selector, visible=visible,
                          value=value, on_click=on_click)


def _umpass_elements(site, *, submit=True, submit_controls=1,
                       account=ACCOUNT, password=PASSWORD, mfa=False,
                       submit_click=None, fallback_aliases=False):
    """The UMPASS form's controls.

    ``fallback_aliases`` registers each control under EVERY selector of its
    list, which is the ADFS/UMPASS shape: one input carrying both ``name`` and
    ``id`` answers several allowed selectors at once.
    """
    account_field = _element(ACCOUNT_FIELD, site, value=account)
    password_field = _element(PASSWORD_FIELD, site, value=password)
    elements = {
        ACCOUNT_FIELD: [account_field],
        PASSWORD_FIELD: [password_field],
    }
    if fallback_aliases:
        for selector in chat2api.UMPASS_ACCOUNT_SELECTORS[1:]:
            elements[selector] = [account_field]
        for selector in chat2api.UMPASS_PASSWORD_SELECTORS[1:]:
            elements[selector] = [password_field]
    if submit:
        submit_fields = [_element(SUBMIT_BUTTON, site, on_click=submit_click)
                         for _ in range(submit_controls)]
        elements[SUBMIT_BUTTON] = submit_fields
        if fallback_aliases:
            for selector in chat2api.UMPASS_SUBMIT_SELECTORS[1:]:
                elements[selector] = submit_fields
    if mfa:
        mfa_field = _element(MFA_MARKER, site)
        elements[MFA_MARKER] = [mfa_field]
        if fallback_aliases:
            for selector in chat2api.UMPASS_MFA_SELECTORS[1:]:
                elements[selector] = [mfa_field]
    return elements


def _build_site(token="fresh-token", *, sso_button=True, submit=True,
                submit_controls=1, account=ACCOUNT, password=PASSWORD,
                mfa=False, duo=True, swallowed=0, landing=OPENWEBUI_URL,
                final_url=OPENWEBUI_HOME, fallback_aliases=False):
    """Wire the fake two-step scenario the real loop must drive.

    ``swallowed`` is how many initial UMPASS submit clicks produce NO page
    movement at all (an eaten click), so the session must observe that
    before it may retry. ``fallback_aliases`` gives the UMPASS page the ADFS
    shape where one control answers several allowed selectors.
    """
    site = _FakeSite(landing)
    eaten = {"left": swallowed}

    def arrive():
        site.navigate(final_url)
        site.set_token(token)

    def leave_duo(_site):
        arrive()

    def ump_ass_submit_click():
        if eaten["left"] > 0:
            eaten["left"] -= 1
            return  # eaten: the page does not move at all
        if duo:
            site.navigate(DUO_URL)
            # Duo approves a trusted device on its own, but only after the
            # session has actually waited on the prompt (one poll beat here).
            site.script = [lambda _site: None, leave_duo]
        else:
            arrive()

    def leave_openwebui():
        site.navigate(UMPASS_URL)
        site.elements = _umpass_elements(site, submit=submit,
                                          submit_controls=submit_controls,
                                          account=account, password=password,
                                          mfa=mfa,
                                          submit_click=ump_ass_submit_click,
                                          fallback_aliases=fallback_aliases)

    if sso_button:
        site.elements[SSO_BUTTON] = [
            _element(SSO_BUTTON, site, on_click=leave_openwebui)]
    return site


class _Harness:
    """Drives the real ``browser_login`` against an offline fake site."""

    def __init__(self, site, verdicts=("valid",)):
        self.site = site
        self.verdicts = list(verdicts)
        self.probes = []
        self.notes = []
        self.created = []
        self.reported = []
        self.profile_dir = tempfile.mkdtemp(
            prefix="chat2api-browser-login-")
        atexit.register(shutil.rmtree, self.profile_dir, ignore_errors=True)

    def _context_factory(self, _edge, _profile_dir):
        context = _FakeContext(self.site)
        self.created.append(context)
        return context

    @staticmethod
    def _no_processes(_profile_dir):
        return []

    @staticmethod
    def _never_signal(pid, sig):
        raise AssertionError("unexpected browser cleanup signal: %s %s"
                             % (pid, sig))

    def _credential_probe(self, base_url, token):
        self.probes.append((base_url, token))
        # The LAST verdict is the steady state: an upstream that answered
        # "unknown" once keeps answering "unknown".
        if len(self.verdicts) > 1:
            verdict = self.verdicts.pop(0)
        else:
            verdict = self.verdicts[0]
        return (verdict, None, 200)

    def _on_progress(self, progress):
        self.notes.append(dict(progress))

    def _on_context(self, context):
        self.reported.append(context)

    def run(self, timeout_s=2, cancel_event=None):
        with (
            mock.patch.object(chat2api, "find_edge",
                              return_value=r"C:\fake\msedge.exe"),
            redirect_stdout(io.StringIO()) as output,
        ):
            result = chat2api.browser_login(
                BASE_URL,
                self.profile_dir,
                timeout_s,
                on_context=self._on_context,
                cancel_event=cancel_event,
                context_factory=self._context_factory,
                credential_probe=self._credential_probe,
                on_progress=self._on_progress,
                process_lister=self._no_processes,
                process_signal=self._never_signal,
            )
        self.output = output.getvalue()
        return result

    def click_selectors(self):
        return list(self.site.clicks)

    def phases(self):
        return [note["phase"] for note in self.notes]

    def manual_reasons(self):
        return [note["manual_reason"] for note in self.notes
                 if note.get("manual_reason")]

    def human_step(self, reason, action):
        """A human acts only AFTER the session reports it needs one.

        Until then the step re-queues itself, exactly as a person would only
        reach for the window once the console says it is needed.
        """
        def step(site):
            if reason not in self.manual_reasons():
                site.script.insert(0, step)
                return
            action(site)
        return step


def _patch_click_budget(testcase, observe_s=0.05, settle_s=0.05):
    for name, value in (("LOGIN_CLICK_OBSERVE_TIMEOUT_S", observe_s),
                        ("LOGIN_STAGE_SETTLE_TIMEOUT_S", settle_s)):
        patcher = mock.patch.object(chat2api, name, value)
        patcher.start()
        testcase.addCleanup(patcher.stop)


class TwoStepFlowTests(unittest.TestCase):
    """A13-1：两步跳转（Open WebUI「使用 UMPASS 继续」→ UMPASS「登录」）。"""

    def setUp(self):
        _patch_click_budget(self)

    def test_two_step_flow_returns_the_token(self):
        site = _build_site()
        harness = _Harness(site)

        token = harness.run()

        self.assertEqual(token, "fresh-token")
        self.assertEqual(site.goto_calls, [BASE_URL])
        self.assertEqual(harness.click_selectors(), [SSO_BUTTON, SUBMIT_BUTTON])
        self.assertEqual(harness.probes, [(BASE_URL, "fresh-token")])
        self.assertIn("openwebui_clicked", harness.phases())
        self.assertIn("umpass_submitted", harness.phases())
        self.assertIn("awaiting_duo", harness.phases())
        self.assertEqual(harness.manual_reasons(), [])
        self.assertEqual([c.closed for c in harness.created], [True])
        self.assertNotIn(PASSWORD, harness.output)
        self.assertNotIn(ACCOUNT, harness.output)

    def test_duo_is_waited_on_and_never_clicked(self):
        site = _build_site()
        harness = _Harness(site)

        harness.run()

        self.assertIn("awaiting_duo", harness.phases())
        # The Duo prompt's own control is never clicked: the session waits
        # the prompt out and only then reads the token.
        self.assertEqual(harness.click_selectors(), [SSO_BUTTON, SUBMIT_BUTTON])

    def test_missing_prefill_never_submits_and_the_human_finishes(self):
        site = _build_site(password="")
        harness = _Harness(site)

        def human_fills_the_form(fake):
            fake.elements[PASSWORD_FIELD] = [
                _element(PASSWORD_FIELD, fake, value=PASSWORD)]
            fake.set_token("human-token")

        site.script = [harness.human_step("umpass_prefill_missing",
                                           human_fills_the_form)]

        token = harness.run()

        self.assertEqual(token, "human-token")
        self.assertEqual(harness.click_selectors(), [SSO_BUTTON])
        self.assertIn("umpass_prefill_missing", harness.manual_reasons())

    def test_mfa_prompt_is_left_to_a_human(self):
        site = _build_site(mfa=True)
        harness = _Harness(site)

        def human_completes_the_mfa(fake):
            fake.elements.pop(MFA_MARKER)
            fake.set_token("mfa-token")

        site.script = [harness.human_step("umpass_mfa_required",
                                          human_completes_the_mfa)]

        token = harness.run()

        self.assertEqual(token, "mfa-token")
        self.assertEqual(harness.click_selectors(), [SSO_BUTTON])
        self.assertIn("umpass_mfa_required", harness.manual_reasons())

    def test_swallowed_submit_is_retried_once_then_accepted(self):
        site = _build_site(swallowed=1)
        harness = _Harness(site)

        token = harness.run()

        self.assertEqual(token, "fresh-token")
        # One initial submit plus exactly one evidence-backed retry.
        self.assertEqual(harness.click_selectors(),
                         [SSO_BUTTON, SUBMIT_BUTTON, SUBMIT_BUTTON])

    def test_never_more_than_one_retry_for_a_swallowed_click(self):
        site = _build_site(swallowed=5)
        harness = _Harness(site)

        def human_submits(fake):
            fake.set_token("human-token")

        site.script = [harness.human_step("umpass_click_swallowed",
                                          human_submits)]

        token = harness.run()

        self.assertEqual(token, "human-token")
        self.assertEqual(harness.click_selectors(),
                         [SSO_BUTTON, SUBMIT_BUTTON, SUBMIT_BUTTON])
        self.assertIn("umpass_click_swallowed", harness.manual_reasons())

    def test_rejected_token_is_dropped_and_the_session_continues(self):
        site = _build_site()
        site.on_reload = lambda fake: fake.set_token("second-token")
        harness = _Harness(site, verdicts=("rejected", "valid"))

        token = harness.run()

        self.assertEqual(token, "second-token")
        self.assertEqual(site.reloads, 1)
        self.assertEqual(harness.probes,
                         [(BASE_URL, "fresh-token"),
                          (BASE_URL, "second-token")])

    def test_inconclusive_probe_never_discards_the_token(self):
        site = _build_site()
        harness = _Harness(site, verdicts=("unknown",))

        with self.assertRaises(chat2api.LoginStalled) as ctx:
            harness.run(timeout_s=0.2)

        # The token stays (no drop, no reload) and the session reports the
        # inconclusive probe instead of claiming an expiry or a success.
        self.assertEqual(ctx.exception.reason, "credential_probe_inconclusive")
        self.assertEqual(site.reloads, 0)
        self.assertEqual(site.token, "fresh-token")
        self.assertEqual(harness.probes[-1], (BASE_URL, "fresh-token"))
        self.assertEqual(harness.click_selectors(), [SSO_BUTTON, SUBMIT_BUTTON])


class ClickSafetyGateTests(unittest.TestCase):
    """A13-3：只在已核对页面且预期控件唯一可见时才点击。"""

    def setUp(self):
        _patch_click_budget(self)

    def test_unknown_origin_is_never_clicked(self):
        site = _FakeSite(LOOKALIKE_URL)
        site.elements = {SSO_BUTTON: [_element(SSO_BUTTON, site)]}
        harness = _Harness(site)

        with self.assertRaises(chat2api.LoginStalled) as ctx:
            harness.run(timeout_s=0.3)

        self.assertEqual(ctx.exception.reason, "unrecognized_page")
        self.assertEqual(harness.click_selectors(), [])
        self.assertEqual(harness.notes[0]["origin"], LOOKALIKE_URL)

    def test_lookalike_sso_host_is_not_the_sso_page(self):
        site = _FakeSite(LOOKALIKE_URL)
        site.elements = {
            ACCOUNT_FIELD: [_element(ACCOUNT_FIELD, site, value=ACCOUNT)],
            PASSWORD_FIELD: [_element(PASSWORD_FIELD, site, value=PASSWORD)],
            SUBMIT_BUTTON: [_element(SUBMIT_BUTTON, site)],
        }
        harness = _Harness(site)

        with self.assertRaises(chat2api.LoginStalled) as ctx:
            harness.run(timeout_s=0.3)

        self.assertEqual(ctx.exception.reason, "unrecognized_page")
        self.assertEqual(harness.click_selectors(), [])

    def test_missing_button_on_the_login_page_never_clicks(self):
        site = _FakeSite(OPENWEBUI_URL)
        harness = _Harness(site)

        with self.assertRaises(chat2api.LoginStalled) as ctx:
            harness.run(timeout_s=0.3)

        self.assertEqual(ctx.exception.reason, "openwebui_button_unrecognized")
        self.assertEqual(harness.click_selectors(), [])

    def test_ambiguous_submit_controls_are_never_clicked(self):
        site = _build_site(submit_controls=2)
        harness = _Harness(site)

        with self.assertRaises(chat2api.LoginStalled) as ctx:
            harness.run(timeout_s=0.3)

        self.assertEqual(ctx.exception.reason, "umpass_submit_unrecognized")
        self.assertEqual(harness.click_selectors(), [SSO_BUTTON])

    def test_hidden_controls_are_not_operated(self):
        site = _build_site()
        site.elements[SSO_BUTTON] = [_element(SSO_BUTTON, site, visible=False)]
        harness = _Harness(site)

        with self.assertRaises(chat2api.LoginStalled) as ctx:
            harness.run(timeout_s=0.3)

        self.assertEqual(ctx.exception.reason, "openwebui_button_unrecognized")
        self.assertEqual(harness.click_selectors(), [])


class FallbackSelectorIdentityTests(unittest.TestCase):
    """一个控件命中多个回退选择器 ≠ 控件歧义（ADFS/UMPASS 的真实形状）。

    两份回退选择器列表的存在理由就是真实页面里一个 input 同时带 name 与 id；
    这种页面上命中的仍是一个控件，自动流程必须照常提交。只有**不同**的可见控件
    才是「控件歧义」，才停给人工。
    """

    def setUp(self):
        _patch_click_budget(self)

    def test_one_control_behind_two_fallbacks_is_one_control(self):
        site = _FakeSite(UMPASS_URL)
        account = _element(ACCOUNT_FIELD, site, value=ACCOUNT)
        site.elements[ACCOUNT_FIELD] = [account]
        site.elements[chat2api.UMPASS_ACCOUNT_SELECTORS[1]] = [account]
        page = chat2api._LoginPage(site)

        self.assertEqual(page.one_visible(chat2api.UMPASS_ACCOUNT_SELECTORS),
                         (ACCOUNT_FIELD, 0))
        self.assertTrue(page.field_filled(chat2api.UMPASS_ACCOUNT_SELECTORS))

    def test_two_controls_behind_two_fallbacks_are_ambiguous(self):
        site = _FakeSite(UMPASS_URL)
        other = chat2api.UMPASS_ACCOUNT_SELECTORS[1]
        site.elements[ACCOUNT_FIELD] = [
            _element(ACCOUNT_FIELD, site, value=ACCOUNT)]
        site.elements[other] = [_element(other, site, value=ACCOUNT)]
        page = chat2api._LoginPage(site)

        self.assertIsNone(page.one_visible(chat2api.UMPASS_ACCOUNT_SELECTORS))
        self.assertFalse(page.field_filled(chat2api.UMPASS_ACCOUNT_SELECTORS))

    def test_adfs_shaped_page_is_submitted_by_the_real_loop(self):
        site = _build_site(fallback_aliases=True)
        harness = _Harness(site)

        token = harness.run()

        self.assertEqual(token, "fresh-token")
        self.assertEqual(harness.click_selectors(), [SSO_BUTTON, SUBMIT_BUTTON])
        self.assertEqual(harness.manual_reasons(), [])

    def test_fallback_aliases_do_not_weaken_the_mfa_gate(self):
        site = _build_site(mfa=True, fallback_aliases=True)
        harness = _Harness(site)

        with self.assertRaises(chat2api.LoginStalled) as ctx:
            harness.run(timeout_s=0.3)

        self.assertEqual(ctx.exception.reason, "umpass_mfa_required")
        self.assertEqual(harness.click_selectors(), [SSO_BUTTON])


# ------------------------------------------------------- real DOM (A.2(c))
# 既有用例全部跑在合成元素字典上，从不经过真实浏览器引擎，因此选择器的实际匹配
# 语义（同一个元素命中多个回退选择器、隐藏控件、真实点击与导航）从未被验证。
# 下面用 Playwright 自带的无头 Chromium 在**本机**加载脱敏夹具：所有导航由
# page.route 离线应答，不联网、不打开 Edge、不做任何账号交互。

FIXTURES = Path(__file__).resolve().parent / "fixtures"
WEBSO_ORIGIN = "https://websso.um.edu.mo"
OTHER_ORIGIN = "https://idp.other.example"


def _fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class RealDomLoginPageTests(unittest.TestCase):
    """真实 DOM 上的选择器匹配与点击安全闸（离线无头 Chromium）。"""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - 未安装 playwright
            raise unittest.SkipTest("playwright unavailable: %s" % exc)
        cls.manager = sync_playwright().start()
        try:
            cls.browser = cls.manager.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - 未安装浏览器
            cls.manager.stop()
            raise unittest.SkipTest("headless Chromium unavailable: %s: %s"
                                    % (type(exc).__name__, exc))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.manager.stop()

    def setUp(self):
        self.served = []
        self.notes = []
        self.probes = []
        self.profile_dir = tempfile.mkdtemp(prefix="chat2api-dom-profile-")
        self.addCleanup(shutil.rmtree, self.profile_dir, ignore_errors=True)
        self.entry_html = _fixture("um_openwebui_entry_desensitized.html")
        self.websso_html = _fixture("um_websso_adfs_desensitized.html")
        self.context = type(self).browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.route("**/*", self._route)

    def _route(self, route):
        url = route.request.url
        self.served.append(url)
        if url.startswith(BASE_URL):
            body = self.entry_html
        elif url.startswith(WEBSO_ORIGIN):
            body = self.websso_html
        elif url.startswith(OTHER_ORIGIN):
            body = self.websso_html
        else:
            body = "<html><body></body></html>"
        route.fulfill(status=200, content_type="text/html; charset=utf-8",
                      body=body)

    def _context_factory(self, _edge, _profile_dir):
        return self.context

    def _probe(self, base_url, token):
        self.probes.append((base_url, token))
        return ("valid", None, 200)

    def _run(self, timeout_s=3):
        with (
            mock.patch.object(chat2api, "find_edge",
                              return_value=r"C:\fake\msedge.exe"),
            redirect_stdout(io.StringIO()),
        ):
            return chat2api.browser_login(
                BASE_URL, self.profile_dir, timeout_s,
                context_factory=self._context_factory,
                credential_probe=self._probe,
                on_progress=self.notes.append,
                process_lister=lambda _profile: [],
                process_signal=lambda _pid, _sig: True)

    def _run_stalled(self, timeout_s=3):
        with self.assertRaises(chat2api.LoginStalled) as caught:
            self._run(timeout_s)
        return caught.exception

    def _manual_notes(self):
        return [note for note in self.notes if note.get("manual_reason")]

    def test_adfs_shaped_prefilled_form_is_submitted_on_a_real_dom(self):
        # 契约：账号与密码都已预填、提交控件唯一可见时，自动流程必须点提交并
        # 取回 token。夹具覆盖实测的 span#submitButton[role=button]，并保留
        # 账号/密码同一元素同时命中 name 与 id 的回退选择器形状。
        try:
            token = self._run(timeout_s=10)
        except chat2api.LoginStalled as exc:
            self.fail("ADFS 形状的预填表单未被自动提交：manual_reason=%s；"
                      "同一元素命中多个允许的选择器被读成「控件歧义」"
                      % exc.reason)
        self.assertEqual(token, "dom-submit-token")
        self.assertIn(WEBSO_ORIGIN + "/adfs/ls/submitted", self.served)
        self.assertEqual([base for base, _token in self.probes], [BASE_URL])

    def test_span_submit_plus_second_control_stays_ambiguous(self):
        # Adding the real span selector must not weaken the one-control gate.
        self.websso_html = self.websso_html.replace(
            "</form>", '<input type="submit" value="other">\n  </form>')
        with mock.patch.object(chat2api, "LOGIN_STAGE_SETTLE_TIMEOUT_S", 0.05):
            exc = self._run_stalled(timeout_s=5)

        self.assertEqual(exc.reason, "umpass_submit_unrecognized")
        self.assertNotIn(WEBSO_ORIGIN + "/adfs/ls/submitted", self.served)
        self.assertEqual(self.probes, [])

    def test_mfa_control_on_a_real_dom_page_stops_for_a_human(self):
        # 真实 DOM 上的一次性验证码控件可见时：停给人工，绝不点验证码控件，
        # 也绝不点提交控件（两个控件的 onclick 都会留下导航请求作为证据）。
        self.websso_html = _fixture("um_websso_mfa_desensitized.html")
        exc = self._run_stalled(timeout_s=3)

        self.assertEqual(exc.reason, "umpass_mfa_required")
        self.assertIn(WEBSO_ORIGIN + "/adfs/ls/", self.served)
        self.assertNotIn(WEBSO_ORIGIN + "/adfs/ls/submitted", self.served)
        self.assertNotIn(WEBSO_ORIGIN + "/adfs/ls/otp-clicked", self.served)
        self.assertEqual(self.probes, [])

    def test_unknown_origin_real_dom_page_is_never_clicked(self):
        # DOM 形状与 UMPASS 表单完全相同，但 origin 未识别：绝不点任何控件，
        # 如实报出 unrecognized_page 与它看到的页面。
        self.entry_html = self.entry_html.replace(
            WEBSO_ORIGIN + "/adfs/ls/", OTHER_ORIGIN + "/sso")
        exc = self._run_stalled(timeout_s=3)

        self.assertEqual(exc.reason, "unrecognized_page")
        # 页面确实被加载（导航发生了），但页面上的控件一次都没被点。
        self.assertIn(OTHER_ORIGIN + "/sso", self.served)
        self.assertNotIn(WEBSO_ORIGIN + "/adfs/ls/submitted", self.served)
        self.assertIn(OTHER_ORIGIN + "/sso", str(exc))
        # 未知页每轮都会重复上报同一个原因；页面集合里只有这一个 origin。
        self.assertEqual(set(note["origin"] for note in self._manual_notes()),
                         {OTHER_ORIGIN + "/sso"})
        self.assertEqual(set(note["manual_reason"]
                             for note in self._manual_notes()),
                         {"unrecognized_page"})


class SessionLifecycleTests(unittest.TestCase):
    """A13-1：超时、取消与资源释放语义保持。"""

    def test_real_playwright_entry_stops_started_playwright_not_manager(self):
        site = _FakeSite(OPENWEBUI_HOME, token="fresh-token")
        context = _FakeContext(site)
        stopped = []

        class FakeManager:
            def start(self):
                return SimpleNamespace(
                    chromium=SimpleNamespace(
                        launch_persistent_context=lambda *args, **kwargs: context),
                    stop=lambda: stopped.append(True),
                )

        with (
            mock.patch.object(chat2api, "find_edge", return_value="fake-edge"),
            mock.patch("playwright.sync_api.sync_playwright", return_value=FakeManager()),
            redirect_stdout(io.StringIO()),
        ):
            with tempfile.TemporaryDirectory(
                    prefix="chat2api-real-playwright-") as profile_dir:
                token = chat2api.browser_login(
                    BASE_URL, profile_dir, 1,
                    credential_probe=lambda base_url, bearer: ("valid", None, 200),
                    process_lister=lambda _profile: [],
                    process_signal=lambda _pid, _sig: True,
                )

        self.assertEqual(token, "fresh-token")
        self.assertTrue(context.closed)
        self.assertEqual(stopped, [True])

    def test_launch_is_headless_and_reuses_the_given_profile(self):
        site = _FakeSite(OPENWEBUI_HOME, token="fresh-token")
        context = _FakeContext(site)
        launches = []

        class FakeManager:
            def start(self):
                return SimpleNamespace(
                    chromium=SimpleNamespace(
                        launch_persistent_context=lambda *args, **kwargs: (
                            launches.append((args, kwargs)) or context),
                    ),
                    stop=lambda: None,
                )

        with (
            mock.patch.object(chat2api, "find_edge", return_value="fake-edge"),
            mock.patch("playwright.sync_api.sync_playwright",
                       return_value=FakeManager()),
            redirect_stdout(io.StringIO()),
        ):
            token = chat2api.browser_login(
                BASE_URL, "profile-dir", 1,
                credential_probe=lambda base_url, bearer: ("valid", None, 200),
                process_lister=lambda _profile: [],
                process_signal=lambda _pid, _sig: True,
            )

        self.assertEqual(token, "fresh-token")
        self.assertEqual(len(launches), 1)
        args, kwargs = launches[0]
        # 同一个 profile 目录交给持久化上下文，不再派生专用目录。
        self.assertEqual(args, ("profile-dir",))
        # 默认无窗口：真实启动参数必须是 headless。
        self.assertIs(kwargs.get("headless"), True)

    def test_posix_without_a_display_drives_the_fake_flow(self):
        site = _build_site()
        harness = _Harness(site)

        with mock.patch.object(chat2api.sys, "platform", "linux"), \
             mock.patch.dict(os.environ, {"DISPLAY": "",
                                         "WAYLAND_DISPLAY": ""}):
            token = harness.run()

        # POSIX 无 DISPLAY/WAYLAND_DISPLAY 也必须走通 headless 自动流程。
        self.assertEqual(token, "fresh-token")
        self.assertEqual(harness.click_selectors(), [SSO_BUTTON, SUBMIT_BUTTON])

    def setUp(self):
        _patch_click_budget(self, settle_s=30.0)

    def test_timeout_reports_the_last_page_and_stops(self):
        site = _FakeSite(OPENWEBUI_URL)
        harness = _Harness(site)

        with self.assertRaises(chat2api.LoginStalled) as ctx:
            harness.run(timeout_s=0.2)

        self.assertEqual(ctx.exception.reason, "timeout")
        self.assertIn(OPENWEBUI_URL, str(ctx.exception))
        self.assertEqual([c.closed for c in harness.created], [True])

    def test_cancel_event_stops_the_session(self):
        site = _FakeSite(OPENWEBUI_URL)
        cancel = threading.Event()
        cancel.set()
        harness = _Harness(site)

        with self.assertRaises(RuntimeError) as ctx:
            harness.run(cancel_event=cancel)

        self.assertIn("cancelled", str(ctx.exception))
        self.assertEqual(harness.click_selectors(), [])
        self.assertEqual([c.closed for c in harness.created], [True])

    def test_missing_edge_is_an_explicit_error(self):
        with mock.patch.object(chat2api, "find_edge", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                chat2api.browser_login(BASE_URL, "profile-dir", 1)
        self.assertIn("Edge was not found", str(ctx.exception))

    def test_context_is_reported_and_released(self):
        site = _build_site()
        harness = _Harness(site)

        harness.run()

        self.assertEqual(len(harness.created), 1)
        self.assertEqual(harness.reported, [harness.created[0], None])
        self.assertEqual([c.closed for c in harness.created], [True])


class AdminLoginWindowTests(unittest.TestCase):
    """D18-6: ONE 30-minute login window, an explicit cancel, no reopen.

    The real Handler and Proxy run; only ``browser_login`` is a blocking stub.
    The window deadline is fixed once when the explicit login starts, a status
    read/ordinary refresh/repeated explicit login never renew it, ``/admin/auth/cancel``
    ends THIS round without stopping the service, and only a later explicit login
    opens a new window.
    """

    STUB_TOKEN = "stub-token"

    def setUp(self):
        self.usage_dir, self.usage_log = harness.install_usage_isolation(
            self, chat2api, ledger_name="usage-login-window.jsonl")
        harness.assert_stub_credentials(
            chat2api, local_key=None, upstream_token=self.STUB_TOKEN,
            stub_token_literal=self.STUB_TOKEN)
        self.state_dir = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True)
        self.addCleanup(self.state_dir.cleanup)
        for name, value in (
                ("REAUTH_SLOT_FILE",
                 str(Path(self.state_dir.name) / "reauth.state")),
                ("TOKEN_FILE",
                 str(Path(self.state_dir.name) / "token.json"))):
            patcher = mock.patch.object(chat2api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.control_token = chat2api.ensure_control_token(
            chat2api.CONTROL_TOKEN_FILE)

        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.login_calls = []

        def fake_login(*args, **kwargs):
            self.login_calls.append(kwargs)
            self.release.wait(10)
            return "fresh-token"

        login_patch = mock.patch.object(chat2api, "browser_login",
                                        side_effect=fake_login)
        login_patch.start()
        self.addCleanup(login_patch.stop)

        args = SimpleNamespace(
            base_url="https://example.invalid",
            token=None,
            profile="profile",
            login_timeout=1800,
            use_api_key=False,
            no_browser=False,
        )
        self.proxy = chat2api.Proxy({"token": None}, args)
        self.addCleanup(self.proxy.shutdown)
        server = harness.TrackedHTTPServer(("127.0.0.1", 0), chat2api.Handler)
        server.proxy = self.proxy
        threading.Thread(target=server.serve_forever, daemon=True).start()
        harness.install_tracked_server(self, server, name="login-window")
        self.port = server.server_port

    def _request(self, method, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=b"" if method == "POST" else None,
                          headers={"X-Admin-Control": self.control_token})
            response = conn.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, payload
        finally:
            conn.close()

    def _await_recovery_thread(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.proxy._auth_condition:
                thread = self.proxy._recovery_thread
            if thread is None:
                return
            thread.join(0.05)
        raise AssertionError("the recovery thread never finished")

    def test_window_is_fixed_once_and_cancel_never_stops_the_service(self):
        status, payload = self._request("POST", "/admin/auth/login")
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload["state"], "logging_in", payload)
        first_deadline = payload["login_window_deadline_epoch"]
        self.assertIsNotNone(first_deadline)
        self.assertEqual(payload["login_window_s"], 1800)

        # A status read, an ordinary refresh (reconnect) and a repeated
        # explicit login never renew the ONE window of this round.
        status, payload = self._request("GET", "/admin/auth/status")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["login_window_deadline_epoch"],
                         first_deadline)
        self.assertTrue(self.proxy.refresh(wait=False))
        deadline = time.monotonic() + 5.0
        while not self.login_calls and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(self.login_calls), 1)
        status, payload = self._request("POST", "/admin/auth/login")
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload["login_window_deadline_epoch"],
                         first_deadline)

        # Cancel ends THIS round without stopping the service.
        status, payload = self._request("POST", "/admin/auth/cancel")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["cancelled"])
        self.assertEqual(payload["state"], "cancelled")
        status, payload = self._request("GET", "/admin/auth/status")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["state"], "cancelled")
        self.assertEqual(payload["login_window_deadline_epoch"],
                         first_deadline)
        status, payload = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

        # An ordinary request never reopens the cancelled round.
        self.assertFalse(self.proxy.refresh(wait=False))
        self.assertEqual(len(self.login_calls), 1)

        # Release the cancelled session and let its loop finish.
        self.release.set()
        self._await_recovery_thread()
        self.assertEqual(self.proxy.auth_status()["state"], "cancelled")

        # Only a later explicit login opens a NEW window.
        status, payload = self._request("POST", "/admin/auth/login")
        self.assertEqual(status, 202, payload)
        self.assertGreater(payload["login_window_deadline_epoch"],
                           first_deadline)
        deadline = time.monotonic() + 5.0
        while len(self.login_calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(self.login_calls), 2)


class ProfileSingleInstanceLockTests(unittest.TestCase):
    """ticket #11：同一 profile 同时只允许一个登录浏览器。"""

    def setUp(self):
        self.profile = tempfile.mkdtemp(prefix="chat2api-profile-lock-")
        self.addCleanup(shutil.rmtree, self.profile, ignore_errors=True)

    @staticmethod
    def _empty_lister(_profile):
        return []

    @staticmethod
    def _never_signal(pid, sig):
        raise AssertionError("unexpected signal %s to pid %s" % (sig, pid))

    def test_second_login_is_rejected_while_the_first_is_running(self):
        site = _build_site()
        started = threading.Event()
        release = threading.Event()
        contexts = []
        results = {}
        errors = []

        def factory(_edge, profile):
            contexts.append(profile)
            started.set()
            release.wait(5)
            return _FakeContext(site)

        def first():
            try:
                results["token"] = chat2api.browser_login(
                    BASE_URL, self.profile, 2,
                    context_factory=factory,
                    credential_probe=lambda _base, _token: ("valid", None, 200),
                    process_lister=self._empty_lister,
                    process_signal=self._never_signal,
                )
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(started.wait(5), "the first login never opened")
        with self.assertRaises(chat2api.LoginBusy) as caught:
            chat2api.browser_login(
                BASE_URL, self.profile, 2,
                context_factory=lambda _edge, _profile: self.fail(
                    "the second request must not open a browser"),
                process_lister=lambda _profile: self.fail(
                    "cleanup must not run while another session holds the lock"),
                process_signal=self._never_signal,
            )
        self.assertEqual(caught.exception.reason, "login_already_running")

        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "the first login never finished")
        self.assertEqual(errors, [])
        self.assertEqual(results.get("token"), "fresh-token")
        self.assertEqual(contexts, [self.profile])

    def test_a_different_profile_is_not_blocked(self):
        other_profile = tempfile.mkdtemp(prefix="chat2api-other-profile-")
        self.addCleanup(shutil.rmtree, other_profile, ignore_errors=True)
        lock = chat2api._acquire_profile_login_lock(self.profile)
        self.assertIsNotNone(lock)
        self.addCleanup(chat2api._release_profile_login_lock, lock)

        site = _build_site()
        token = chat2api.browser_login(
            BASE_URL, other_profile, 2,
            context_factory=lambda _edge, _profile: _FakeContext(site),
            credential_probe=lambda _base, _token: ("valid", None, 200),
            process_lister=self._empty_lister,
            process_signal=self._never_signal,
        )
        self.assertEqual(token, "fresh-token")

    def test_lock_file_lives_in_the_profile_and_records_the_owner(self):
        lock = chat2api._acquire_profile_login_lock(self.profile)
        self.assertIsNotNone(lock)
        self.addCleanup(chat2api._release_profile_login_lock, lock)

        self.assertEqual(os.path.dirname(lock.path), os.path.abspath(self.profile))
        self.assertEqual(os.path.basename(lock.path),
                         chat2api.PROFILE_LOGIN_LOCK_NAME)
        owner = chat2api._read_profile_lock_owner(self.profile)
        self.assertIsNotNone(owner)
        self.assertEqual(owner.get("pid"), os.getpid())
        self.assertIn("process_started_epoch", owner)

    def test_lock_is_released_after_a_failed_session(self):
        def failing_factory(_edge, _profile):
            raise RuntimeError("launch failed")

        with self.assertRaises(RuntimeError):
            chat2api.browser_login(
                BASE_URL, self.profile, 1,
                context_factory=failing_factory,
                process_lister=self._empty_lister,
                process_signal=self._never_signal,
            )

        site = _build_site()
        token = chat2api.browser_login(
            BASE_URL, self.profile, 2,
            context_factory=lambda _edge, _profile: _FakeContext(site),
            credential_probe=lambda _base, _token: ("valid", None, 200),
            process_lister=self._empty_lister,
            process_signal=self._never_signal,
        )
        self.assertEqual(token, "fresh-token")

    def test_lock_is_released_when_the_holder_process_dies(self):
        marker = os.path.join(self.profile, "child-locked")
        proc = subprocess.Popen(
            [sys.executable, "-c", _CHILD_LOCK_SCRIPT,
             str(CHAT2API_PATH), self.profile, marker],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(self._stop_child, proc)

        deadline = time.monotonic() + 20
        while not os.path.exists(marker):
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode("utf-8", "replace")
                self.fail("the lock child exited early: %s" % stderr)
            if time.monotonic() >= deadline:
                self.fail("the lock child never acquired the profile lock")
            time.sleep(0.02)

        with open(marker, encoding="utf-8") as handle:
            child_pid = int(handle.read())
        owner = chat2api._read_profile_lock_owner(self.profile)
        self.assertIsNotNone(owner)
        self.assertEqual(owner.get("pid"), child_pid)

        with self.assertRaises(chat2api.LoginBusy):
            chat2api.browser_login(
                BASE_URL, self.profile, 1,
                context_factory=lambda _edge, _profile: self.fail(
                    "a locked profile must not open a browser"),
                process_lister=self._empty_lister,
                process_signal=self._never_signal,
            )

        proc.terminate()
        proc.wait(10)

        site = _build_site()
        token = chat2api.browser_login(
            BASE_URL, self.profile, 2,
            context_factory=lambda _edge, _profile: _FakeContext(site),
            credential_probe=lambda _base, _token: ("valid", None, 200),
            process_lister=self._empty_lister,
            process_signal=self._never_signal,
        )
        self.assertEqual(token, "fresh-token")

    @staticmethod
    def _stop_child(proc):
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                pass
        if proc.stderr is not None:
            proc.stderr.close()


class ProfileBrowserCleanupTests(unittest.TestCase):
    """ticket #11：打开前只清理同 profile 的旧 Chromium，先 TERM 后 KILL。"""

    def setUp(self):
        self.profile = tempfile.mkdtemp(prefix="chat2api-profile-cleanup-")
        self.other_profile = tempfile.mkdtemp(
            prefix="chat2api-other-cleanup-")
        self.addCleanup(shutil.rmtree, self.profile, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.other_profile, ignore_errors=True)
        self.signals = []
        self.alive = {}

    @staticmethod
    def _process(pid, name, argv):
        return chat2api.ProfileBrowserProcess(
            pid=pid, name=name, argv=tuple(argv))

    def _lister(self, _profile):
        return list(self.alive.values())

    def _signal(self, pid, sig):
        self.signals.append((pid, sig))
        if sig == chat2api.PROFILE_BROWSER_TERM_SIGNAL:
            self.alive.pop(pid, None)
        return True

    @staticmethod
    def _empty_lister(_profile):
        return []

    def test_only_chromium_with_the_exact_same_profile_is_terminated(self):
        same_profile = self._process(
            101, "msedge.exe",
            ["msedge.exe", "--user-data-dir=" + self.profile])
        other_profile = self._process(
            202, "chrome.exe",
            ["chrome.exe", "--user-data-dir=" + self.other_profile])
        firefox = self._process(
            303, "firefox.exe",
            ["firefox.exe", "--user-data-dir=" + self.profile])
        prefix_profile = self._process(
            404, "chromium",
            ["chromium", "--user-data-dir=" + self.profile + "-copy"])
        subdirectory = self._process(
            505, "google-chrome",
            ["google-chrome", "--user-data-dir=" + self.profile + "/Default"])
        self.alive = {process.pid: process for process in (
            same_profile, other_profile, firefox, prefix_profile, subdirectory)}

        terminated = chat2api._cleanup_profile_browsers(
            self.profile,
            lister=self._lister,
            signal_process=self._signal,
            term_wait_s=0.05,
            kill_wait_s=0.05,
        )

        self.assertEqual(terminated, [101])
        self.assertEqual(self.signals, [
            (101, chat2api.PROFILE_BROWSER_TERM_SIGNAL)])
        self.assertEqual(set(self.alive), {202, 303, 404, 505})

    def test_sigterm_that_does_not_finish_escalates_to_sigkill(self):
        target = self._process(
            111, "msedge.exe",
            ["msedge.exe", "--user-data-dir", self.profile])
        self.alive = {target.pid: target}

        def stubborn_signal(pid, sig):
            self.signals.append((pid, sig))
            if sig == chat2api.PROFILE_BROWSER_KILL_SIGNAL:
                self.alive.pop(pid, None)
            return True

        terminated = chat2api._cleanup_profile_browsers(
            self.profile,
            lister=self._lister,
            signal_process=stubborn_signal,
            term_wait_s=0.05,
            kill_wait_s=0.05,
        )

        self.assertEqual(terminated, [111])
        self.assertEqual([sig for _pid, sig in self.signals], [
            chat2api.PROFILE_BROWSER_TERM_SIGNAL,
            chat2api.PROFILE_BROWSER_KILL_SIGNAL,
        ])

    def test_stale_browser_is_cleaned_before_the_context_opens(self):
        target = self._process(
            121, "msedge.exe",
            ["msedge.exe", "--user-data-dir=" + self.profile])
        self.alive = {target.pid: target}
        events = []

        def signal(pid, sig):
            events.append(("signal", pid, sig))
            if sig == chat2api.PROFILE_BROWSER_KILL_SIGNAL:
                self.alive.pop(pid, None)
            return True

        def factory(_edge, profile):
            events.append(("open", profile))
            return _FakeContext(_build_site())

        token = chat2api.browser_login(
            BASE_URL, self.profile, 2,
            context_factory=factory,
            credential_probe=lambda _base, _token: ("valid", None, 200),
            process_lister=self._lister,
            process_signal=signal,
        )

        self.assertEqual(token, "fresh-token")
        self.assertEqual(events[0], (
            "signal", 121, chat2api.PROFILE_BROWSER_TERM_SIGNAL))
        self.assertEqual(events[-1], ("open", self.profile))

    def test_unreadable_process_table_refuses_the_session_and_releases_the_lock(self):
        opened = []
        with self.assertRaises(chat2api.LoginUnavailable) as caught:
            chat2api.browser_login(
                BASE_URL, self.profile, 1,
                context_factory=lambda _edge, profile: opened.append(profile),
                process_lister=lambda _profile: None,
                process_signal=lambda _pid, _sig: True,
            )
        self.assertEqual(caught.exception.reason,
                         "profile_cleanup_unavailable")
        self.assertEqual(opened, [])

        site = _build_site()
        token = chat2api.browser_login(
            BASE_URL, self.profile, 2,
            context_factory=lambda _edge, _profile: _FakeContext(site),
            credential_probe=lambda _base, _token: ("valid", None, 200),
            process_lister=self._empty_lister,
            process_signal=lambda _pid, _sig: True,
        )
        self.assertEqual(token, "fresh-token")


_CHILD_LOCK_SCRIPT = """\
import importlib.util
import os
import sys
import time

spec = importlib.util.spec_from_file_location(
    "chat2api_profile_lock_child", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
lock = module._acquire_profile_login_lock(sys.argv[2])
if lock is None:
    raise SystemExit(3)
with open(sys.argv[3], "w", encoding="utf-8") as marker:
    marker.write(str(os.getpid()))
time.sleep(60)
"""


if __name__ == "__main__":
    unittest.main()
