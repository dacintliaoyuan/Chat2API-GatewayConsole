# -*- coding: utf-8 -*-
"""chat2api 自动重新认证恢复的时序与持久化契约回归。

目标契约：

* E2a：旧 rejected 探测结果返回后，不得再开第二个可见登录会话；
* E2b：旧 valid 探测结果返回后，不得让进行中的登录提前写回 valid；
* E4：周期巡检不得清除 failed 终态的失败原因；
* E5a：TokenStore.save 的受控替换失败不得截断或损坏原凭据文件；
* E5b：shutdown 取代会话后不得迟到保存凭据或写回 valid。

所有浏览器登录、上游分类和 API-key 获取均替换为内存替身；控制秘密、
凭据文件与共享槽都指向临时目录。测试不打开真实 Edge、不访问真实网络、
不使用真实 token，所有阻塞用 threading.Event，总耗时远小于 10 秒。
"""

import importlib.util
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


# 导入前隔离控制秘密路径：模块导入不得读写真实管理状态。
_TMP = tempfile.mkdtemp(prefix="chat2api-reauth-recovery-tests-")
_CONTROL_TOKEN_FILE = os.path.join(_TMP, "admin-control-token")
os.environ["CHAT2API_CONTROL_TOKEN_FILE"] = _CONTROL_TOKEN_FILE
with open(_CONTROL_TOKEN_FILE, "w", encoding="utf-8") as _control:
    _control.write("reauth-recovery-test-control-token")

CHAT2API_PATH = Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py"
_SPEC = importlib.util.spec_from_file_location(
    "chat2api_recovery_under_test", CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = chat2api
_SPEC.loader.exec_module(chat2api)

BASE_URL = "https://example.invalid"


def _auth_args(**overrides):
    values = dict(
        base_url=BASE_URL,
        profile="profile",
        login_timeout=2,
        use_api_key=False,
        no_browser=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class RecoveryContractTestBase(unittest.TestCase):
    """临时控制秘密、凭据文件与共享槽的公共隔离，外加真实调用的安全网。"""

    def setUp(self):
        slot_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(slot_dir.cleanup)
        slot_patcher = mock.patch.object(
            chat2api,
            "REAUTH_SLOT_FILE",
            str(Path(slot_dir.name) / "reauth.state"),
        )
        slot_patcher.start()
        self.addCleanup(slot_patcher.stop)

        # E2a/E2b 会真实调用 TokenStore.save()；把目标文件也移出仓库。
        token_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(token_dir.cleanup)
        token_patcher = mock.patch.object(
            chat2api,
            "TOKEN_FILE",
            str(Path(token_dir.name) / "token.json"),
        )
        token_patcher.start()
        self.addCleanup(token_patcher.stop)

        # 安全网：任何未被用例替身接管的登录/探测都必须让测试失败，而不是
        # 打开真实 Edge 或访问 example.invalid。用例内部的 mock 会覆盖它们。
        login_guard = mock.patch.object(
            chat2api,
            "browser_login",
            side_effect=AssertionError(
                "测试不得打开真实 Edge：browser_login 未被用例替身接管"),
        )
        login_guard.start()
        self.addCleanup(login_guard.stop)
        classify_guard = mock.patch.object(
            chat2api,
            "classify_credential",
            side_effect=AssertionError(
                "测试不得发起真实上游探测：classify_credential 未被用例替身接管"),
        )
        classify_guard.start()
        self.addCleanup(classify_guard.stop)

    def _proxy(self, creds=None, **args_overrides):
        proxy = chat2api.Proxy(
            creds or {"token": "stale-token", "api_key": None},
            _auth_args(**args_overrides),
        )
        self.addCleanup(proxy.shutdown)
        return proxy

    def _finish_session_thread(self, proxy):
        """等待单飞会话线程结束，确保浏览器登录次数已经定稿。"""
        with proxy._auth_condition:
            session_thread = proxy._auth_thread
        if (session_thread is not None
                and session_thread is not threading.current_thread()):
            session_thread.join(2)
            self.assertFalse(
                session_thread.is_alive(),
                "测试无法在 2 秒内等到重新认证会话线程结束，"
                "不能据此判定浏览器登录次数",
            )


class ReauthRecoveryContractTests(RecoveryContractTestBase):
    """E2/E4/E5 目标契约。"""

    def test_stale_rejected_probe_does_not_open_second_session(self):
        proxy = self._proxy()
        classify_entered = threading.Event()
        classify_release = threading.Event()
        self.addCleanup(classify_release.set)

        def fake_classify(base_url, bearer, timeout=15):
            classify_entered.set()
            if not classify_release.wait(5):
                raise AssertionError("测试未释放 classify_credential 的阻塞")
            return "rejected", "upstream_rejected", 401

        def fake_login(*args, **kwargs):
            return "fresh-token"

        worker = threading.Thread(
            target=proxy.validate_or_refresh,
            kwargs={"source": "startup"},
            name="reauth-recovery-stale-rejected",
            daemon=True,
        )
        with (
            mock.patch.object(
                chat2api, "classify_credential", side_effect=fake_classify),
            mock.patch.object(
                chat2api, "browser_login", side_effect=fake_login) as login,
        ):
            worker.start()
            self.assertTrue(
                classify_entered.wait(2),
                "契约：validate_or_refresh 必须先调用 classify_credential",
            )
            self.assertTrue(
                proxy.refresh(force=True, wait=True),
                "契约：新凭据的有效登录应成功完成",
            )
            classify_release.set()
            worker.join(2)
            self.assertFalse(
                worker.is_alive(),
                "契约：旧 rejected 探测线程应在事件释放后返回",
            )
            self._finish_session_thread(proxy)

        self.assertEqual(
            login.call_count, 1,
            "契约（E2a）：旧 rejected 探测结果返回后不得再开第二个重新认证"
            "会话；旧结果只允许记录证据，不能重复已经完成的恢复",
        )
        self.assertEqual(
            proxy.auth_status()["state"], "valid",
            "契约（E2a）：旧 rejected 探测返回后认证状态仍应为 valid",
        )
        self.assertEqual(
            proxy.creds["token"], "fresh-token",
            "契约（E2a）：旧 rejected 探测不得回滚已发布的新凭据",
        )

    def test_stale_valid_probe_does_not_finish_login_early(self):
        proxy = self._proxy()
        classify_entered = threading.Event()
        classify_release = threading.Event()
        login_entered = threading.Event()
        login_release = threading.Event()
        b_started = threading.Event()
        b_returned = threading.Event()
        b_results = []
        self.addCleanup(login_release.set)
        self.addCleanup(classify_release.set)

        def fake_classify(base_url, bearer, timeout=15):
            classify_entered.set()
            if not classify_release.wait(5):
                raise AssertionError("测试未释放 classify_credential 的阻塞")
            return "valid", None, 200

        def fake_login(*args, **kwargs):
            login_entered.set()
            if not login_release.wait(5):
                raise AssertionError("测试未释放 browser_login 的阻塞")
            return "fresh-token"

        def run_refresh_b():
            b_started.set()
            b_results.append(proxy.refresh(force=True, wait=True))
            b_returned.set()

        worker_a = threading.Thread(
            target=proxy.validate_or_refresh,
            kwargs={"source": "startup"},
            name="reauth-recovery-stale-valid",
            daemon=True,
        )
        worker_b = threading.Thread(
            target=run_refresh_b,
            name="reauth-recovery-waiter",
            daemon=True,
        )
        try:
            with (
                mock.patch.object(
                    chat2api, "classify_credential", side_effect=fake_classify),
                mock.patch.object(
                    chat2api, "browser_login", side_effect=fake_login),
            ):
                worker_a.start()
                self.assertTrue(
                    classify_entered.wait(2),
                    "契约：validate_or_refresh 必须先调用 classify_credential",
                )
                self.assertTrue(
                    proxy.refresh(force=True, wait=False),
                    "契约：进行中的登录会话应已启动",
                )
                self.assertTrue(
                    login_entered.wait(2),
                    "契约：browser_login 应在会话线程内进入",
                )
                worker_b.start()
                self.assertTrue(
                    b_started.wait(2),
                    "契约：等待中的 refresh(wait=True) 应已开始",
                )
                classify_release.set()
                # 旧 valid 结果返回后必须被丢弃；短等足以让错误实现写回状态。
                self.assertFalse(
                    b_returned.wait(0.2),
                    "契约（E2b）：旧 valid 探测不得让进行中的登录提前结束；"
                    "等待中的 refresh(wait=True) 仍应阻塞",
                )
                status = proxy.auth_status()
                self.assertEqual(
                    status["state"], "logging_in",
                    "契约（E2b）：旧 valid 探测返回后认证状态仍应为 logging_in，"
                    "不得提前写回 valid",
                )
                self.assertEqual(
                    proxy.creds["token"], "stale-token",
                    "契约（E2b）：旧 valid 探测不得让尚未完成的登录提前发布"
                    "新凭据",
                )
                login_release.set()
                worker_a.join(2)
                worker_b.join(2)
                self.assertFalse(
                    worker_a.is_alive(),
                    "契约：旧 valid 探测线程应在事件释放后返回",
                )
                self.assertFalse(
                    worker_b.is_alive(),
                    "契约：等待中的 refresh 应在新登录完成后返回",
                )
                self._finish_session_thread(proxy)
        finally:
            classify_release.set()
            login_release.set()
            if worker_a.ident is not None:
                worker_a.join(2)
            if worker_b.ident is not None:
                worker_b.join(2)

        self.assertEqual(
            proxy.auth_status()["state"], "valid",
            "契约（E2b）：新登录完成后认证状态应为 valid",
        )
        self.assertEqual(
            proxy.creds["token"], "fresh-token",
            "契约（E2b）：新登录完成后应发布新凭据",
        )
        self.assertEqual(
            b_results, [True],
            "契约（E2b）：等待中的 refresh(wait=True) 应在新登录成功后"
            "返回 True",
        )

    def test_auth_watch_round_preserves_failed_reason(self):
        proxy = self._proxy()
        with mock.patch.object(
            chat2api,
            "browser_login",
            side_effect=RuntimeError("expected failure"),
        ):
            self.assertFalse(
                proxy.refresh(force=True),
                "契约：失败的重认证会话应返回 False",
            )

        status = proxy.auth_status()
        self.assertEqual(
            status["state"], "failed",
            "前置条件：重认证异常后状态应为 failed",
        )
        self.assertEqual(
            status["error"], "RuntimeError",
            "前置条件：重认证异常后应保留失败原因 RuntimeError",
        )

        with mock.patch.object(
            chat2api,
            "classify_credential",
            return_value=("rejected", "upstream_rejected", 401),
        ):
            outcome = proxy.run_auth_watch_round()

        self.assertEqual(
            outcome, "rejected",
            "契约：rejected 巡检应如实报告 rejected",
        )
        status = proxy.auth_status()
        self.assertEqual(
            status["state"], "failed",
            "契约（E4）：rejected 巡检不得把终态 failed 改回 needs_login",
        )
        self.assertEqual(
            status["error"], "RuntimeError",
            "契约（E4）：rejected 巡检不得清除终态失败原因；新探测证据"
            "与原失败原因应分别表达",
        )

    def test_token_store_save_failure_keeps_previous_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "token.json")
            store = chat2api.TokenStore(path, BASE_URL)
            store.save("old-token")
            loaded = store.load()
            self.assertIsNotNone(
                loaded, "前置条件：旧凭据文件应可加载")
            self.assertEqual(
                loaded["token"], "old-token",
                "前置条件：旧凭据文件应保存 old-token",
            )

            failure = None
            with mock.patch.object(
                chat2api.os,
                "replace",
                side_effect=OSError("disk full"),
            ):
                try:
                    store.save("new-token")
                except OSError as exc:
                    failure = exc
            self.assertIsNotNone(
                failure,
                "契约（E5a）：TokenStore.save 在受控替换失败时必须抛出 "
                "OSError，不得静默成功或吞掉异常",
            )
            loaded = store.load()
            self.assertIsNotNone(
                loaded,
                "契约（E5a）：保存失败不得截断原凭据文件；load() 必须仍能"
                "读取旧数据",
            )
            self.assertEqual(
                loaded["token"], "old-token",
                "契约（E5a）：保存失败后原凭据文件必须保持完整，"
                "不得损坏唯一有效副本",
            )

    def test_shutdown_discards_late_save_and_valid(self):
        proxy = self._proxy(use_api_key=True)
        save_calls = []

        def fake_probe_config():
            # 会话在配置探测阶段被 shutdown 取代：后续 API-key 获取与保存
            # 都属于迟到结果，必须整体丢弃。
            proxy.shutdown()
            return {"version": "x", "features": {}}

        def fake_save(self, token, api_key=None):
            save_calls.append((token, api_key))
            return {"url": self.url, "token": token, "api_key": api_key}

        with (
            mock.patch.object(
                chat2api, "browser_login", return_value="fresh-token"),
            mock.patch.object(
                proxy, "probe_config", new=fake_probe_config),
            mock.patch.object(
                chat2api, "fetch_api_key", return_value=None),
            mock.patch.object(
                chat2api.TokenStore, "save", autospec=True,
                side_effect=fake_save) as save,
        ):
            proxy.refresh(force=True)

        save.assert_not_called()
        self.assertEqual(
            save_calls, [],
            "契约（E5b）：shutdown 取代会话后不得迟到保存凭据",
        )
        self.assertEqual(
            proxy.creds, {"token": "stale-token", "api_key": None},
            "契约（E5b）：shutdown 后不得把迟到的 token 写回内存凭据",
        )
        self.assertNotEqual(
            proxy.auth_status()["state"], "valid",
            "契约（E5b）：shutdown 后不得把认证状态写回 valid",
        )


class RecoveryScheduleContractTests(RecoveryContractTestBase):
    """A2/A3 目标契约：失败分类、退避预算与手动轮次。"""

    _LEDGER = {"owner_pid": None, "expires_epoch": 0.0,
               "attempts_in_window": 1, "next_allowed_epoch": None}

    def _grant_patch(self, ledger=None):
        return mock.patch.object(
            chat2api, "acquire_reauth_slot",
            return_value=(True, None, dict(ledger or self._LEDGER)))

    def test_transient_failures_follow_the_bounded_backoff_then_pause(self):
        proxy = self._proxy()
        waits = []
        clock = [1000.0]
        sessions = []

        def fake_now():
            return clock[0]

        def fake_sleep(seconds, wake=None):
            waits.append(round(seconds, 3))
            clock[0] += seconds

        def fake_login(*args, **kwargs):
            sessions.append(clock[0])
            raise chat2api.LoginWindowClosed("window_closed")

        with (
            mock.patch.object(proxy, "_recovery_now", side_effect=fake_now),
            mock.patch.object(proxy, "_recovery_sleep", side_effect=fake_sleep),
            mock.patch.object(
                chat2api, "browser_login", side_effect=fake_login),
            self._grant_patch(),
            mock.patch.object(chat2api, "release_reauth_slot"),
        ):
            self.assertTrue(proxy.refresh(force=True, wait=False))
            self._finish_session_thread(proxy)

        self.assertEqual(
            len(sessions), 6,
            "契约（A3）：首次加最多 5 次重试，共最多 6 次会话",
        )
        self.assertEqual(
            waits, [30.0, 60.0, 300.0, 600.0],
            "契约（A3）：首次失败立即重试，之后依次等待 30/60/300/600 秒",
        )
        status = proxy.auth_status()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["error"], "attempts_exhausted")
        self.assertEqual(status["recovery"]["state"], "exhausted")
        self.assertEqual(status["recovery"]["session_starts"], 6)
        self.assertEqual(status["recovery"]["pause_reason"],
                         "attempts_exhausted")

    def test_manual_takeover_pauses_the_cycle(self):
        proxy = self._proxy()
        with (
            mock.patch.object(
                chat2api, "browser_login",
                side_effect=chat2api.LoginStalled(
                    "umpass_mfa_required",
                    "last page https://websso.um.edu.mo/")),
            self._grant_patch(),
            mock.patch.object(chat2api, "release_reauth_slot"),
        ):
            self.assertFalse(proxy.refresh(force=True))

        status = proxy.auth_status()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["error"], "umpass_mfa_required")
        self.assertEqual(status["recovery"]["state"], "exhausted")
        self.assertEqual(status["recovery"]["session_starts"], 1)
        self.assertEqual(status["recovery"]["pause_reason"],
                         "umpass_mfa_required")

    def test_explicit_credential_rejection_pauses_the_cycle(self):
        proxy = self._proxy()
        with (
            mock.patch.object(
                chat2api, "browser_login",
                side_effect=chat2api.LoginCredentialRejected(
                    "upstream_credential_rejected")),
            self._grant_patch(),
            mock.patch.object(chat2api, "release_reauth_slot"),
        ):
            self.assertFalse(proxy.refresh(force=True))

        status = proxy.auth_status()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["error"], "upstream_credential_rejected")
        self.assertEqual(status["recovery"]["pause_reason"],
                         "upstream_credential_rejected")

    def test_closed_window_is_reopened_within_the_budget(self):
        proxy = self._proxy()
        calls = []

        def fake_login(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise chat2api.LoginWindowClosed("window_closed")
            return "fresh-token"

        with (
            mock.patch.object(
                chat2api, "browser_login", side_effect=fake_login),
            self._grant_patch(),
            mock.patch.object(chat2api, "release_reauth_slot"),
            mock.patch.object(chat2api.TokenStore, "save"),
        ):
            self.assertTrue(proxy.refresh(force=True))

        self.assertEqual(
            len(calls), 2,
            "契约（A2）：窗口关闭属于可重开的临时失败，不得当作密码拒绝",
        )
        self.assertEqual(proxy.auth_status()["state"], "valid")
        self.assertEqual(proxy.auth_status()["recovery"]["session_starts"], 2)


class _EvidencePage:
    """_umpass_credential_rejected 的最小结构替身。"""

    def __init__(self, url, visible, filled):
        self._url = url
        self._visible = visible
        self._filled = filled

    def url(self):
        return self._url

    def any_visible(self, selectors):
        return self._visible

    def field_filled(self, selectors):
        return self._filled


class LoginClassificationContractTests(unittest.TestCase):
    """A2：浏览器登录只按结构化证据分类，不靠异常名称猜测。"""

    def test_rejection_needs_the_visible_error_on_the_umpass_form(self):
        page = _EvidencePage(
            "https://websso.um.edu.mo/adfs/ls/", True, True)
        self.assertTrue(chat2api._umpass_credential_rejected(page))
        page = _EvidencePage(
            "https://websso.um.edu.mo/adfs/ls/", False, True)
        self.assertFalse(
            chat2api._umpass_credential_rejected(page),
            "契约（A2）：没有可见错误区域不得判为密码拒绝",
        )
        page = _EvidencePage(
            "https://websso.um.edu.mo/adfs/ls/", True, False)
        self.assertFalse(
            chat2api._umpass_credential_rejected(page),
            "契约（A2）：密码字段已被清空时不得判为密码拒绝",
        )
        page = _EvidencePage("https://example.invalid/login", True, True)
        self.assertFalse(
            chat2api._umpass_credential_rejected(page),
            "契约（A2）：非 UMPASS 页面一律交给人工处理",
        )

    def test_browser_login_reports_a_missing_browser(self):
        with mock.patch.object(chat2api, "find_edge", return_value=None):
            with self.assertRaises(chat2api.LoginUnavailable):
                chat2api.browser_login(BASE_URL, "profile", 1)

    def test_browser_login_reports_a_closed_window(self):
        class _ClosedPage:
            def url(self):
                return BASE_URL

            def is_closed(self):
                return True

        class _ClosedContext:
            def __init__(self):
                self.pages = [_ClosedPage()]

            def new_page(self):
                return self.pages[0]

            def close(self):
                pass

        with mock.patch.object(
                chat2api, "find_edge", return_value="fake-edge"):
            with self.assertRaises(chat2api.LoginWindowClosed):
                chat2api.browser_login(
                    BASE_URL, "profile", 2,
                    context_factory=lambda edge, profile: _ClosedContext(),
                    credential_probe=lambda base, token: ("valid", None, 200),
                )


class LoginWindowContractTests(RecoveryContractTestBase):
    """D18-6: ONE 30-minute window per explicit login round, never renewed."""

    _LEDGER = {"owner_pid": None, "expires_epoch": 0.0,
               "attempts_in_window": 1, "next_allowed_epoch": None}

    def _grant_patch(self):
        return mock.patch.object(
            chat2api, "acquire_reauth_slot",
            return_value=(True, None, dict(self._LEDGER)))

    def test_window_is_fixed_once_and_never_renewed(self):
        proxy = self._proxy(login_timeout=1800)
        clock = [1000.0]
        proxy._recovery_now = lambda: clock[0]
        release = threading.Event()
        self.addCleanup(release.set)
        sessions = []

        def fake_login(*args, **kwargs):
            sessions.append(kwargs)
            release.wait(5)
            return "fresh-token"

        with (
            self._grant_patch(),
            mock.patch.object(
                chat2api, "_release_reauth_slot_for_round", return_value=True),
            mock.patch.object(
                chat2api, "browser_login", side_effect=fake_login),
        ):
            self.assertTrue(proxy.refresh(force=True, wait=False))
            status = proxy.auth_status()
            self.assertEqual(status["login_window_s"], 1800.0)
            self.assertEqual(
                status["login_window_deadline_epoch"], 2800.0,
                "契约（D18-6）：登录窗口应在显式登录开始时固定为 30 分钟",
            )

            # An ordinary refresh (reconnect) never renews the window.
            clock[0] += 60.0
            self.assertTrue(proxy.refresh(wait=False))
            self.assertEqual(
                proxy.auth_status()["login_window_deadline_epoch"], 2800.0,
                "契约（D18-6）：普通刷新/重连不得续期登录窗口",
            )

            # A repeated explicit login while the round is active never
            # renews it either.
            self.assertTrue(proxy.refresh(force=True, wait=False))
            self.assertEqual(
                proxy.auth_status()["login_window_deadline_epoch"], 2800.0,
                "契约（D18-6）：同一轮重复显式登录不得续期登录窗口",
            )

            # Cancel ends THIS round; the service keeps running.
            deadline = time.monotonic() + 5.0
            while not sessions and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(sessions), 1,
                             "契约（D18-6）：取消前本轮登录窗口应已打开")
            result = proxy.cancel_login()
            self.assertTrue(result["cancelled"])
            self.assertEqual(result["state"], "cancelled")
            self.assertEqual(proxy.auth_status()["state"], "cancelled")
            self.assertFalse(proxy.refresh(wait=False))
            self.assertEqual(len(sessions), 1)

            # The cancelled session ends without writing valid back.
            release.set()
            self._finish_session_thread(proxy)
            self.assertEqual(proxy.auth_status()["state"], "cancelled")

            # Only an explicit login opens a NEW window with a new deadline.
            clock[0] += 60.0
            self.assertTrue(proxy.refresh(force=True, wait=False))
            self.assertEqual(
                proxy.auth_status()["login_window_deadline_epoch"], 2920.0,
                "契约（D18-6）：取消后的显式登录应开启新窗口",
            )
            deadline = time.monotonic() + 5.0
            while len(sessions) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(sessions), 2)


if __name__ == "__main__":
    unittest.main()
