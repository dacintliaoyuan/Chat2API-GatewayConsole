# -*- coding: utf-8 -*-
"""Regression tests for chat2api credential refresh behavior."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


CHAT2API_PATH = Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py"
SPEC = importlib.util.spec_from_file_location("chat2api_runtime", CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def isolate_reauth_slot(testcase):
    """Keep the cross-instance lease file out of the repository.

    ``refresh()`` takes a real lease in ``chat2api.REAUTH_SLOT_FILE``; a test
    that exercised it in the source directory would leave state behind, so every
    test that starts a session points the module at a temp path instead.
    """
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    testcase.addCleanup(tmp.cleanup)
    patcher = mock.patch.object(chat2api, "REAUTH_SLOT_FILE",
                               str(Path(tmp.name) / "reauth.state"))
    patcher.start()
    testcase.addCleanup(patcher.stop)


# R13-1: the cross-instance mutex must hold across PROCESSES, so the regressions
# below drive the real functions in a REAL second process instead of only in
# threads of the test process. The child loads chat2api.py by path (the same way
# this file does), waits for a per-step signal file so both children call the
# acquire at the same instant, and writes one JSON result per step.
_LOCK_CHILD_SCRIPT = '''\
import importlib.util
import json
import os
import sys
import time


def load_chat2api(path):
    spec = importlib.util.spec_from_file_location("chat2api_lock_child", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def wait_for(path, timeout=60.0):
    deadline = time.time() + timeout
    while not os.path.exists(path):
        if time.time() > deadline:
            return False
        time.sleep(0.002)
    return True


def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def main():
    module_path, plan_path, pid, out_path = sys.argv[1:5]
    chat2api = load_chat2api(module_path)
    with open(plan_path, encoding="utf-8") as f:
        plan = json.load(f)
    write_json(out_path + ".ready", {"pid": os.getpid()})
    for index, step in enumerate(plan["steps"]):
        result = {"step": index}
        if not wait_for(step["signal"]):
            result["error"] = "no_signal"
        elif step["mode"] == "session":
            # The wait budget is per step here too: two processes racing for
            # one damaged slot must both report the damaged slot, and the loser
            # needs the slack a loaded machine requires to reach the mutex at
            # all instead of the production 1.0s.
            if step.get("lock_wait_s") is not None:
                chat2api.REAUTH_LOCK_WAIT_S = float(step["lock_wait_s"])
            granted, reason, ledger = chat2api.acquire_reauth_slot(
                step["slot"], pid=int(pid), ttl_s=step["ttl_s"],
                now=step["now"])
            result.update({"granted": granted, "reason": reason,
                           "ledger": ledger})
        elif step["mode"] == "hold":
            # Take the raw mutex and stay inside it: this is what proves the
            # "only one holder at any instant" property across processes.
            # The wait budget is per step: the production 1.0s covers a
            # millisecond critical section, while a test that holds the lock on
            # purpose needs slack for a loaded machine (the suite runs 1500 tests
            # in parallel with these children).
            if step.get("lock_wait_s") is not None:
                chat2api.REAUTH_LOCK_WAIT_S = float(step["lock_wait_s"])
            handle = chat2api._acquire_reauth_lock(step["slot"])
            started = time.time()
            result["granted"] = handle is not None
            if handle is not None:
                with open(step["marker"], "a", encoding="utf-8") as f:
                    f.write("hold\\n")
                time.sleep(step["hold_s"])
                chat2api._release_reauth_lock(handle)
            result["started"] = started
            result["ended"] = time.time()
        write_json("%s.%d" % (out_path, index), result)
    write_json(out_path, {"done": True})


main()
'''


class CrossProcessLockHarness(unittest.TestCase):
    """Spawn real second processes that drive the real lock functions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.script = self.dir / "lock_child.py"
        self.script.write_text(_LOCK_CHILD_SCRIPT, encoding="utf-8")
        self.children = []
        self.logs = {}

    def spawn(self, plan_path, pid, out_path):
        # Child stdout/stderr go to files (never a pipe nobody drains: a filled
        # pipe buffer would deadlock the child) and are read back on failure.
        log_path = self.dir / ("child.%s.log" % pid)
        log = open(str(log_path), "wb")
        try:
            proc = subprocess.Popen(
                [sys.executable, str(self.script), str(CHAT2API_PATH),
                 str(plan_path), str(pid), str(out_path)],
                cwd=str(CHAT2API_PATH.parent), stdout=log, stderr=log)
        finally:
            log.close()
        self.children.append(proc)
        self.logs[proc.pid] = log_path
        self.addCleanup(self.reap, proc)
        return proc

    def reap(self, proc):
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - wedged child
            pass

    def child_report(self, procs):
        parts = []
        for proc in procs:
            log_path = self.logs.get(proc.pid)
            tail = ""
            if log_path is not None and log_path.exists():
                tail = log_path.read_text(encoding="utf-8",
                                         errors="replace")[-2000:]
            parts.append("pid=%s rc=%s log=%s" % (proc.pid, proc.poll(), tail))
        return " | ".join(parts)

    def write_plan(self, steps, now=1000.0):
        plan_path = self.dir / "plan.json"
        plan_path.write_text(json.dumps({"now": now, "steps": steps}),
                             encoding="utf-8")
        return plan_path

    def wait_for_path(self, path, timeout=120.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if Path(path).exists():
                return True
            time.sleep(0.01)
        return False

    def start(self, steps, pids=(111, 222), now=1000.0):
        """Write the plan, start one child per pid, wait until all are ready."""
        plan_path = self.write_plan(steps, now=now)
        out_paths = {pid: self.dir / ("out.%s.json" % pid) for pid in pids}
        procs = [self.spawn(plan_path, pid, out_paths[pid]) for pid in pids]
        for pid in pids:
            ready = Path(str(out_paths[pid]) + ".ready")
            self.assertTrue(self.wait_for_path(ready),
                            "child %s never started: %s"
                            % (pid, self.child_report(procs)))
        return out_paths, procs

    def run_step(self, out_paths, procs, index, signal):
        Path(signal).touch()
        results = {}
        for pid, out_path in out_paths.items():
            target = Path("%s.%d" % (out_path, index))
            self.assertTrue(self.wait_for_path(target),
                            "child %s produced no result for step %d: %s"
                            % (pid, index, self.child_report(procs)))
            with open(target, encoding="utf-8") as f:
                results[pid] = json.load(f)
        return results


class TestCrossInstanceLockAcrossProcesses(CrossProcessLockHarness):
    """R13-1：两个真实进程争用同一把锁——同一瞬间只有一方拿到。"""

    ROUNDS = 3

    def test_two_processes_racing_for_one_slot_grant_one_session(self):
        # 每一轮用独立的槽位：赢家写下的租约不能影响下一轮的判定。
        slots = [str(self.dir / ("race%d.state" % index))
                 for index in range(self.ROUNDS)]
        steps = [
            {"mode": "session", "slot": slot, "ttl_s": 60.0, "now": 1000.0,
             "lock_wait_s": 30.0,
             "signal": str(self.dir / ("go%d" % index))}
            for index, slot in enumerate(slots)]
        out_paths, procs = self.start(steps)

        for index, slot in enumerate(slots):
            results = self.run_step(out_paths, procs, index,
                                   steps[index]["signal"])
            self.assertNotIn("error", results[111], self.child_report(procs))
            self.assertNotIn("error", results[222], self.child_report(procs))
            granted = [pid for pid, r in results.items() if r["granted"]]
            self.assertEqual(len(granted), 1, results)
            refused = [r for r in results.values() if not r["granted"]]
            # 被拒方必须如实报出原因：拿不到锁是 coordination_unavailable，
            # 拿到锁但看到对方的租约是 global_session_active。
            self.assertIn(refused[0]["reason"],
                          ("global_session_active", "coordination_unavailable"),
                          results)
            self.assertEqual(
                results[granted[0]]["ledger"]["attempts_in_window"], 1,
                results)
            # 两次提交只记一次：账本里只有赢家那一次尝试。
            state, error = chat2api._read_reauth_slot(slot)
            self.assertIsNone(error)
            self.assertEqual(len(state["attempts"]), 1, state)
            self.assertEqual(state["owner_pid"], granted[0])

    def test_two_processes_never_hold_the_mutex_at_the_same_time(self):
        # 双方同时抢原始锁，各自在临界区内停一会儿：旧实现（按 mtime 回收）
        # 会让后到者删掉前者的锁并重建，于是两方同时持锁；现在只能排队。
        slot = str(self.dir / "mutex.state")
        hold_s = 0.25
        steps = [{"mode": "hold", "slot": slot, "hold_s": hold_s,
                  "lock_wait_s": 30.0,
                  "signal": str(self.dir / "mutex.go"),
                  "marker": str(self.dir / "mutex.holds.txt")}]
        out_paths, procs = self.start(steps)

        results = self.run_step(out_paths, procs, 0, steps[0]["signal"])
        self.assertTrue(all(r["granted"] for r in results.values()), results)
        windows = sorted((r["started"], r["ended"]) for r in results.values())
        self.assertLessEqual(windows[0][1], windows[1][0], results)

    def test_the_mutex_is_released_when_the_holder_process_dies(self):
        # 崩溃恢复不需要 mtime 启发：持有者进程死亡即由内核释放。
        slot = str(self.dir / "crash.state")
        marker = self.dir / "crash.holds.txt"
        steps = [{"mode": "hold", "slot": slot, "hold_s": 30.0,
                  "signal": str(self.dir / "crash.go"),
                  "marker": str(marker)}]
        out_paths, procs = self.start(steps, pids=(111,))
        proc = procs[0]
        Path(steps[0]["signal"]).touch()
        self.assertTrue(self.wait_for_path(marker),
                        "holder never took the lock: %s"
                        % self.child_report(procs))
        # 持有中：本进程拿不到同一把锁（这是真实的第二个进程在持有）。
        self.assertIsNone(chat2api._acquire_reauth_lock(slot))
        proc.kill()
        proc.wait(timeout=30)
        handle = chat2api._acquire_reauth_lock(slot)
        self.assertIsNotNone(handle, self.child_report(procs))
        chat2api._release_reauth_lock(handle)

    def test_a_seed_write_race_on_an_empty_lock_file_still_waits(self):
        # 两个进程同时打开仍为空的锁文件时，后到者的 fstat 可能仍看到 0 字节，
        # 它「保证文件至少 1 字节」的写入便撞上先到者已锁住的字节而得到
        # Permission denied。那是「对方正在持锁」，不是「协调失败」：区间锁对
        # 零长度文件同样有效，必须继续等待，不能立即放弃。
        slot = str(self.dir / "seed.state")
        holder = chat2api._acquire_reauth_lock(slot)
        self.assertIsNotNone(holder)
        saved_wait = chat2api.REAUTH_LOCK_WAIT_S
        real_fstat = os.fstat
        chat2api.REAUTH_LOCK_WAIT_S = 0.2
        # 让竞争者的 fstat 仍看到空文件，从而走写入分支（其余读法不变）。
        os.fstat = lambda fd: SimpleNamespace(st_size=0)
        started = time.time()
        try:
            contender = chat2api._acquire_reauth_lock(slot)
        finally:
            elapsed = time.time() - started
            os.fstat = real_fstat
            chat2api.REAUTH_LOCK_WAIT_S = saved_wait
            chat2api._release_reauth_lock(holder)
        self.assertIsNone(contender)
        self.assertGreaterEqual(elapsed, 0.15,
                                "写入被拒后必须继续等待，而不是立即返回")


class TestAuthenticate(unittest.TestCase):
    def test_rejected_saved_token_triggers_browser_refresh(self):
        args = SimpleNamespace(
            base_url="https://example.invalid",
            token=None,
            profile="profile",
            login_timeout=1,
            use_api_key=False,
        )
        saved = {"token": "stale-token", "api_key": None}

        with (
            mock.patch.object(chat2api.TokenStore, "load", return_value=saved),
            mock.patch.object(chat2api.TokenStore, "save") as save,
            mock.patch.object(
                chat2api, "classify_credential",
                return_value=("rejected", "upstream_rejected", 401),
            ) as validate,
            mock.patch.object(
                chat2api, "browser_login", return_value="fresh-token"
            ) as browser_login,
        ):
            credentials = chat2api.authenticate(args)

        self.assertEqual(credentials, {"token": "fresh-token", "api_key": None})
        validate.assert_called_once_with(args.base_url, "stale-token")
        browser_login.assert_called_once_with(
            args.base_url, args.profile, args.login_timeout
        )
        save.assert_called_once_with("fresh-token", None)

    def test_valid_saved_api_key_avoids_browser_refresh(self):
        args = SimpleNamespace(
            base_url="https://example.invalid",
            token=None,
            profile="profile",
            login_timeout=1,
            use_api_key=True,
        )
        saved = {"token": "session-token", "api_key": "long-lived-key"}

        with (
            mock.patch.object(chat2api.TokenStore, "load", return_value=saved),
            mock.patch.object(
                chat2api, "classify_credential",
                return_value=("valid", None, 200),
            ) as validate,
            mock.patch.object(chat2api, "browser_login") as browser_login,
        ):
            credentials = chat2api.authenticate(args)

        self.assertEqual(credentials, saved)
        validate.assert_called_once_with(args.base_url, "long-lived-key")
        browser_login.assert_not_called()

    def test_unverifiable_saved_token_keeps_credentials_and_no_browser(self):
        # A13-2：探测无法判断（超时/5xx/非 JSON）不是过期证据，
        # 不得因此开登录窗，也不得丢弃已保存凭据。
        args = SimpleNamespace(
            base_url="https://example.invalid",
            token=None,
            profile="profile",
            login_timeout=1,
            use_api_key=False,
        )
        saved = {"token": "saved-token", "api_key": None}

        with (
            mock.patch.object(chat2api.TokenStore, "load", return_value=saved),
            mock.patch.object(
                chat2api, "classify_credential",
                return_value=("unknown", "timeout", None),
            ),
            mock.patch.object(chat2api, "browser_login") as browser_login,
        ):
            credentials = chat2api.authenticate(args)

        self.assertEqual(credentials, saved)
        browser_login.assert_not_called()


class TestEdgeBrowser(unittest.TestCase):
    def test_find_edge_uses_windows_installation(self):
        candidates = [r"C:\missing\msedge.exe", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]

        with mock.patch.object(
            chat2api.os.path,
            "exists",
            side_effect=lambda path: path == candidates[1],
        ):
            found = chat2api.find_edge(candidates)

        self.assertEqual(found, candidates[1])

    def test_default_profile_is_edge_specific(self):
        self.assertEqual(Path(chat2api.PROFILE_DIR).name, ".edge-profile")


class TestReauthentication(unittest.TestCase):
    def setUp(self):
        isolate_reauth_slot(self)

    def _args(self):
        return SimpleNamespace(
            base_url="https://example.invalid",
            profile="profile",
            login_timeout=2,
            use_api_key=False,
            no_browser=False,
        )

    def test_concurrent_refreshes_share_one_visible_session(self):
        proxy = chat2api.Proxy({"token": "stale-token", "api_key": None}, self._args())
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def fake_login(*args, **kwargs):
            calls.append(args)
            entered.set()
            self.assertTrue(release.wait(2))
            return "fresh-token"

        results = []
        with (
            mock.patch.object(chat2api, "browser_login", side_effect=fake_login),
            mock.patch.object(chat2api.TokenStore, "save"),
        ):
            first = threading.Thread(target=lambda: results.append(proxy.refresh()))
            second = threading.Thread(target=lambda: results.append(proxy.refresh()))
            first.start()
            self.assertTrue(entered.wait(1))
            second.start()
            time.sleep(0.05)
            release.set()
            first.join(2)
            second.join(2)

        self.assertEqual(len(calls), 1)
        self.assertEqual(results, [True, True])
        self.assertEqual(proxy.auth_status()["state"], "valid")

    def test_failed_refresh_has_explicit_state(self):
        proxy = chat2api.Proxy({"token": "stale-token", "api_key": None}, self._args())

        with mock.patch.object(
            chat2api, "browser_login", side_effect=RuntimeError("expected failure")
        ):
            self.assertFalse(proxy.refresh())

        status = proxy.auth_status()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["error"], "RuntimeError")

    def test_failed_credential_does_not_reopen_edge_for_every_request(self):
        proxy = chat2api.Proxy({"token": "stale-token", "api_key": None}, self._args())
        response = mock.Mock(status_code=401)

        with (
            mock.patch.object(
                chat2api, "browser_login", side_effect=RuntimeError("expected failure")
            ) as browser_login,
            mock.patch.object(chat2api.requests, "request", return_value=response),
        ):
            with self.assertRaises(chat2api.ReauthenticationRequired):
                proxy.call("GET", "/api/models")
            with self.assertRaises(chat2api.ReauthenticationRequired):
                proxy.call("GET", "/api/models")

        self.assertEqual(browser_login.call_count, 1)

    def test_late_401_after_shared_refresh_does_not_open_second_edge(self):
        proxy = chat2api.Proxy({"token": "stale-token", "api_key": None}, self._args())
        rejected = mock.Mock(status_code=401)
        accepted = mock.Mock(status_code=200)

        def upstream_request(*args, **kwargs):
            if upstream_request.calls == 0:
                upstream_request.calls += 1
                proxy.creds = {"token": "fresh-token", "api_key": None}
                return rejected
            return accepted

        upstream_request.calls = 0
        with (
            mock.patch.object(chat2api.requests, "request", side_effect=upstream_request),
            mock.patch.object(chat2api, "browser_login") as browser_login,
        ):
            result = proxy.call("GET", "/api/models")

        self.assertIs(result, accepted)
        browser_login.assert_not_called()


class TestLocalManagementSecurity(unittest.TestCase):
    def test_loopback_cli_request_is_allowed(self):
        self.assertTrue(
            chat2api.is_local_control_request("127.0.0.1", "127.0.0.1:8000", None)
        )

    def test_browser_origin_is_rejected(self):
        self.assertFalse(
            chat2api.is_local_control_request(
                "127.0.0.1", "127.0.0.1:8000", "https://malicious.invalid"
            )
        )

    def test_non_loopback_request_is_rejected(self):
        self.assertFalse(
            chat2api.is_local_control_request("192.0.2.10", "127.0.0.1:8000", None)
        )


class TestHealthEndpoint(unittest.TestCase):
    def test_health_is_local_liveness_without_upstream_request(self):
        args = SimpleNamespace(
            base_url="https://example.invalid",
            profile="profile",
            login_timeout=1,
            use_api_key=False,
            no_browser=False,
        )
        proxy = chat2api.Proxy({}, args)
        server = chat2api.ThreadingHTTPServer(("127.0.0.1", 0), chat2api.Handler)
        server.proxy = proxy
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(proxy, "list_models") as list_models:
                response = chat2api.requests.get(
                    f"http://127.0.0.1:{server.server_port}/health", timeout=2
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["authentication"]["state"], "needs_login")
            list_models.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)




def _auth_args(**overrides):
    values = dict(base_url="https://example.invalid", profile="profile",
                  login_timeout=2, use_api_key=False, no_browser=False)
    values.update(overrides)
    return SimpleNamespace(**values)


class TestCredentialIsValidSameSource(unittest.TestCase):
    """A13-2：布尔视图与 classify_credential 同源，不再自成一判定。"""

    def test_only_a_valid_verdict_is_true(self):
        cases = (
            (("valid", None, 200), True),
            (("rejected", "upstream_rejected", 401), False),
            (("needs_login", "no_local_credential", None), False),
            (("unknown", "timeout", None), False),
            (("unknown", "upstream_5xx", 503), False),
            (("unknown", "non_json_response", 200), False),
        )
        for verdict, expected in cases:
            with self.subTest(verdict=verdict[0], reason=verdict[1]):
                with mock.patch.object(chat2api, "classify_credential",
                                      return_value=verdict) as probe:
                    result = chat2api.credential_is_valid(
                        "https://example.invalid", "bearer")
                self.assertEqual(result, expected)
                # Exactly one probe: the wrapper never issues its own request.
                probe.assert_called_once_with("https://example.invalid", "bearer")


class TestValidateOrRefresh(unittest.TestCase):
    """A13-2：启动/周期校验按 classify_credential 分类收敛。"""

    def setUp(self):
        isolate_reauth_slot(self)

    def _proxy(self, creds=None):
        return chat2api.Proxy(creds or {"token": "saved", "api_key": None},
                             _auth_args())

    def test_valid_verdict_keeps_state_without_a_session(self):
        proxy = self._proxy()
        with (
            mock.patch.object(chat2api, "classify_credential",
                              return_value=("valid", None, 200)),
            mock.patch.object(proxy, "refresh") as refresh,
        ):
            verdict = proxy.validate_or_refresh(source="startup")

        self.assertEqual(verdict, "valid")
        self.assertFalse(refresh.called)
        status = proxy.auth_status()
        self.assertEqual(status["state"], "valid")
        self.assertIsNone(status["error"])
        self.assertEqual(status["evidence"]["source"], "startup")
        self.assertEqual(status["evidence"]["verdict"], "valid")
        self.assertIsNotNone(status["evidence"]["last_valid_epoch"])

    def test_upstream_rejection_starts_exactly_one_session(self):
        proxy = self._proxy()
        with (
            mock.patch.object(chat2api, "classify_credential",
                              return_value=("rejected", "upstream_rejected",
                                            401)),
            mock.patch.object(proxy, "refresh") as refresh,
        ):
            verdict = proxy.validate_or_refresh()

        self.assertEqual(verdict, "rejected")
        self.assertEqual(refresh.call_args, mock.call(wait=False))
        self.assertEqual(proxy.auth_status()["state"], "needs_login")
        self.assertEqual(proxy.auth_status()["evidence"]["verdict"],
                         "rejected")

    def test_missing_local_credential_starts_exactly_one_session(self):
        proxy = self._proxy(creds={})
        with (
            mock.patch.object(chat2api, "classify_credential",
                              return_value=("needs_login",
                                            "no_local_credential", None)),
            mock.patch.object(proxy, "refresh") as refresh,
        ):
            verdict = proxy.validate_or_refresh()

        self.assertEqual(verdict, "needs_login")
        self.assertEqual(refresh.call_args, mock.call(wait=False))

    def test_unknown_verdict_never_opens_a_window_and_keeps_evidence(self):
        proxy = self._proxy()
        with (
            mock.patch.object(chat2api, "classify_credential",
                              return_value=("unknown", "timeout", None)),
            mock.patch.object(proxy, "refresh") as refresh,
        ):
            verdict = proxy.validate_or_refresh()

        self.assertEqual(verdict, "unknown")
        self.assertFalse(refresh.called)
        status = proxy.auth_status()
        # The state keeps its meaning and the label says "cannot tell",
        # instead of being folded into an expiry.
        self.assertEqual(status["state"], "valid")
        self.assertIn("无法判断", status["label"])
        self.assertEqual(status["evidence"]["verdict"], "unknown")
        self.assertEqual(status["evidence"]["reason"], "timeout")

    def test_unknown_never_clears_the_last_valid_epoch(self):
        proxy = self._proxy()
        with mock.patch.object(chat2api, "classify_credential",
                              return_value=("valid", None, 200)):
            proxy.validate_or_refresh()
        last_valid = proxy.auth_status()["evidence"]["last_valid_epoch"]
        self.assertIsNotNone(last_valid)
        with (
            mock.patch.object(chat2api, "classify_credential",
                              return_value=("unknown", "upstream_5xx", 503)),
            mock.patch.object(proxy, "refresh"),
        ):
            proxy.validate_or_refresh()
        self.assertEqual(proxy.auth_status()["evidence"]["last_valid_epoch"],
                         last_valid)


class TestAuthWatchRound(unittest.TestCase):
    """A13-2：有界后台只读检查只走单次 /api/models 分类探测。"""

    def setUp(self):
        isolate_reauth_slot(self)

    def test_valid_round_reads_only_and_never_calls_upstream(self):
        proxy = chat2api.Proxy({"token": "t", "api_key": None}, _auth_args())
        with (
            mock.patch.object(chat2api, "classify_credential",
                              return_value=("valid", None, 200)) as probe,
            mock.patch.object(proxy, "call") as call,
            mock.patch.object(proxy, "list_models") as list_models,
        ):
            outcome = proxy.run_auth_watch_round()

        self.assertEqual(outcome, "valid")
        call.assert_not_called()
        list_models.assert_not_called()
        probe.assert_called_once()
        self.assertEqual(probe.call_args.kwargs["timeout"],
                         chat2api.AUTH_WATCH_PROBE_TIMEOUT_S)
        self.assertEqual(proxy.auth_status()["state"], "valid")

    def test_unknown_round_keeps_state_and_opens_no_window(self):
        proxy = chat2api.Proxy({"token": "t", "api_key": None}, _auth_args())
        with (
            mock.patch.object(chat2api, "classify_credential",
                              return_value=("unknown", "network_error",
                                            None)),
            mock.patch.object(proxy, "refresh") as refresh,
        ):
            outcome = proxy.run_auth_watch_round()

        self.assertEqual(outcome, "unknown")
        self.assertFalse(refresh.called)
        self.assertEqual(proxy.auth_status()["state"], "valid")
        self.assertIn("无法判断", proxy.auth_status()["label"])

    def test_rejected_round_starts_one_session(self):
        proxy = chat2api.Proxy({"token": "t", "api_key": None}, _auth_args())
        with (
            mock.patch.object(chat2api, "classify_credential",
                              return_value=("rejected", "upstream_rejected",
                                            401)),
            mock.patch.object(proxy, "refresh") as refresh,
        ):
            outcome = proxy.run_auth_watch_round()

        self.assertEqual(outcome, "rejected")
        self.assertEqual(refresh.call_args, mock.call(wait=False))

    def test_rejected_round_never_retries_a_failed_credential(self):
        # 每轮都重开登录窗 = 对错误密码无限重试：终态 failed 一旦写入，
        # 后续周期检查只能如实报告，不得把它降回 needs_login 再试一次。
        proxy = chat2api.Proxy({"token": "t", "api_key": None}, _auth_args())
        with (
            mock.patch.object(chat2api, "classify_credential",
                              return_value=("rejected", "upstream_rejected",
                                            401)),
            mock.patch.object(chat2api, "browser_login",
                              side_effect=RuntimeError("wrong password")
                              ) as browser_login,
        ):
            self.assertEqual(proxy.run_auth_watch_round(), "rejected")
            proxy._auth_thread.join(2)     # let the one session finish
            self.assertEqual(proxy.auth_status()["state"], "failed")
            self.assertEqual(proxy.run_auth_watch_round(), "rejected")

        self.assertEqual(proxy.auth_status()["state"], "failed")
        self.assertEqual(browser_login.call_count, 1)   # never a second try

    def test_round_never_duplicates_an_in_flight_session(self):
        proxy = chat2api.Proxy({"token": "t", "api_key": None}, _auth_args())
        with proxy._auth_condition:
            proxy._auth_state = "logging_in"
        with mock.patch.object(chat2api, "classify_credential") as probe:
            outcome = proxy.run_auth_watch_round()
        self.assertEqual(outcome, "logging_in")
        probe.assert_not_called()   # no second upstream probe either

    def test_round_never_refreshes_the_model_cache(self):
        proxy = chat2api.Proxy({"token": "t", "api_key": None}, _auth_args())
        before = dict(proxy.models_cache)
        with mock.patch.object(chat2api, "classify_credential",
                              return_value=("valid", None, 200)):
            proxy.run_auth_watch_round()
        self.assertEqual(proxy.models_cache, before)

    def test_round_after_shutdown_reports_stopped(self):
        proxy = chat2api.Proxy({"token": "t", "api_key": None}, _auth_args())
        proxy.shutdown()
        with mock.patch.object(chat2api, "classify_credential") as probe:
            self.assertEqual(proxy.run_auth_watch_round(), "stopped")
        probe.assert_not_called()


class _RecordingStop:
    """Fake shutdown event: records each wait, then stops the loop.

    ``wait`` returns True once ``stop_after`` rounds have run, so the recorded
    intervals are the waits that gated rounds plus the loop's exit check.
    """

    def __init__(self, rounds, stop_after):
        self.rounds = rounds
        self.intervals = []
        self.stop_after = stop_after

    def is_set(self):
        return False

    def wait(self, timeout):
        self.intervals.append(timeout)
        return len(self.rounds) >= self.stop_after

    def set(self):
        self.stop_after = 0


class _SyncThread:
    """Runs the target inline so the watch loop is deterministic offline."""

    def __init__(self, target=None, name=None, args=(), kwargs=None,
                 daemon=None):
        self._target = target
        self.name = name
        self._args = args

    def start(self):
        self._target(*self._args)

    def is_alive(self):
        # The target ran inline in start(), so this "thread" has already
        # finished by the time anyone can observe it.
        return False


class TestAuthWatchLoop(unittest.TestCase):
    """A13-2：间隔、退避上限与「不与启动探测重复」显式定死。"""

    def _run(self, outcomes, stop_after):
        proxy = chat2api.Proxy({"token": "t", "api_key": None}, _auth_args())
        calls = []
        stop = _RecordingStop(calls, stop_after)
        proxy._shutdown_event = stop

        def fake_round():
            calls.append(1)
            return outcomes[min(len(calls) - 1, len(outcomes) - 1)]

        proxy.run_auth_watch_round = fake_round
        with mock.patch.object(chat2api.threading, "Thread", _SyncThread):
            proxy.begin_auth_watch()
        return stop, calls

    def test_first_round_waits_a_full_interval(self):
        # 首轮不早于一个完整间隔：启动探测已完成，周期检查不与之重复。
        stop, calls = self._run(["valid"], stop_after=1)
        self.assertEqual(calls, [1])
        self.assertEqual(stop.intervals,
                         [chat2api.AUTH_WATCH_INTERVAL_S,
                          chat2api.AUTH_WATCH_INTERVAL_S])

    def test_non_valid_rounds_back_off_up_to_the_cap(self):
        stop, calls = self._run(["unknown"], stop_after=5)
        self.assertEqual(calls, [1] * 5)
        self.assertEqual(stop.intervals,
                         [300.0, 600.0, 1200.0, 1800.0, 1800.0, 1800.0])
        self.assertEqual(chat2api.AUTH_WATCH_MAX_INTERVAL_S, 1800.0)

    def test_valid_round_resets_the_interval(self):
        stop, calls = self._run(["valid"], stop_after=3)
        self.assertEqual(calls, [1] * 3)
        self.assertEqual(stop.intervals, [300.0, 300.0, 300.0, 300.0])


class TestAuthWatchVisibility(unittest.TestCase):
    """A9-6/§8.4：认证监督线程的加性只读可见性（watch 子字典）。"""

    WATCH_KEYS = ["last_round_epoch", "last_round_outcome",
                  "next_round_due", "rounds", "thread_alive"]

    def setUp(self):
        isolate_reauth_slot(self)

    def _proxy(self):
        return chat2api.Proxy({"token": "t", "api_key": None}, _auth_args())

    def test_watch_is_additive_and_honest_before_the_thread_starts(self):
        # 既有键一个不动、语义不变；watch 是加性新增，未启动时不冒充存活、
        # 不编造 0 作为轮次时刻。
        proxy = self._proxy()
        status = proxy.auth_status()
        self.assertEqual(sorted(status),
                         ["error", "evidence", "gate", "label", "login",
                          "state", "watch"])
        watch = status["watch"]
        self.assertEqual(sorted(watch), self.WATCH_KEYS)
        self.assertFalse(watch["thread_alive"])
        self.assertIsNone(watch["last_round_epoch"])
        self.assertIsNone(watch["last_round_outcome"])
        self.assertIsNone(watch["next_round_due"])
        self.assertEqual(watch["rounds"], 0)

        # /health 同样带 watch；既有 authentication 仍只有 state/label。
        snapshot = proxy.health_snapshot()
        self.assertEqual(sorted(snapshot["authentication"]),
                         ["label", "state"])
        self.assertEqual(snapshot["watch"], watch)
        self.assertEqual(sorted(snapshot["watch"]), self.WATCH_KEYS)

    def test_thread_alive_tracks_the_real_watch_thread(self):
        proxy = self._proxy()
        with (
            mock.patch.object(proxy, "_maybe_refresh_config"),
            mock.patch.object(proxy, "run_auth_watch_round",
                              return_value="valid"),
            mock.patch.object(chat2api, "AUTH_WATCH_INTERVAL_S", 60.0),
        ):
            proxy.begin_auth_watch()
            self.assertTrue(proxy.auth_status()["watch"]["thread_alive"])
            self.assertTrue(
                proxy.health_snapshot()["watch"]["thread_alive"])
        proxy.shutdown()
        self.assertFalse(proxy.auth_status()["watch"]["thread_alive"])
        self.assertFalse(proxy.health_snapshot()["watch"]["thread_alive"])

    def test_round_bookkeeping_is_monotonic_and_interval_bounded(self):
        proxy = self._proxy()
        with mock.patch.object(chat2api, "classify_credential",
                              return_value=("valid", None, 200)):
            self.assertEqual(proxy.run_auth_watch_round(), "valid")
            first = proxy.auth_status()["watch"]
            self.assertEqual(first["rounds"], 1)
            self.assertEqual(first["last_round_outcome"], "valid")
            self.assertIsNotNone(first["last_round_epoch"])
            # valid 轮把退避重置为一个基准间隔：next_round_due 与之同源。
            self.assertEqual(
                first["next_round_due"] - first["last_round_epoch"],
                chat2api.AUTH_WATCH_INTERVAL_S)
            self.assertEqual(proxy.run_auth_watch_round(), "valid")
        second = proxy.auth_status()["watch"]
        self.assertEqual(second["rounds"], 2)
        self.assertGreaterEqual(second["last_round_epoch"],
                                first["last_round_epoch"])  # epoch 单调
        self.assertEqual(
            second["next_round_due"] - second["last_round_epoch"],
            chat2api.AUTH_WATCH_INTERVAL_S)

    def test_skipped_round_is_recorded_as_such(self):
        # 会话在飞时跳过探测：这一轮如实记为 logging_in，而不是一次新的检查。
        proxy = self._proxy()
        with proxy._auth_condition:
            proxy._auth_state = "logging_in"
        with mock.patch.object(chat2api, "classify_credential") as probe:
            outcome = proxy.run_auth_watch_round()
        self.assertEqual(outcome, "logging_in")
        probe.assert_not_called()
        watch = proxy.auth_status()["watch"]
        self.assertEqual(watch["rounds"], 1)
        self.assertEqual(watch["last_round_outcome"], "logging_in")
        self.assertIsNotNone(watch["last_round_epoch"])

    def test_loop_publishes_the_backoff_gating_the_next_round(self):
        # 非 valid 轮次按退避因子翻倍：due 与最后一轮之后实际生效的
        # 等待间隔一致，且始终有界。
        proxy = self._proxy()
        rounds = []
        stop = _RecordingStop(rounds, stop_after=2)
        proxy._shutdown_event = stop
        real_round = proxy.run_auth_watch_round

        def counting_round():
            # 走真实轮次（含 _record_watch_round），只额外让假 stop 计数。
            rounds.append(1)
            return real_round()

        proxy.run_auth_watch_round = counting_round
        with (
            mock.patch.object(proxy, "_maybe_refresh_config"),
            mock.patch.object(chat2api, "classify_credential",
                              return_value=("unknown", "network_error",
                                            None)),
            mock.patch.object(chat2api.threading, "Thread", _SyncThread),
        ):
            proxy.begin_auth_watch()
        watch = proxy.auth_status()["watch"]
        self.assertEqual(stop.intervals, [300.0, 600.0, 1200.0])
        self.assertEqual(watch["rounds"], 2)
        self.assertEqual(watch["last_round_outcome"], "unknown")
        self.assertEqual(watch["next_round_due"] - watch["last_round_epoch"],
                         1200.0)
        self.assertGreaterEqual(
            watch["next_round_due"] - watch["last_round_epoch"],
            chat2api.AUTH_WATCH_INTERVAL_S)
        self.assertLessEqual(
            watch["next_round_due"] - watch["last_round_epoch"],
            chat2api.AUTH_WATCH_MAX_INTERVAL_S)


class TestCrossInstanceLock(unittest.TestCase):
    """F1：跨实例租约的读改写必须有跨进程互斥，否则两实例同时被授予会话。"""

    def setUp(self):
        isolate_reauth_slot(self)
        self.slot = chat2api.REAUTH_SLOT_FILE

    def test_lock_is_exclusive_and_released(self):
        handle = chat2api._acquire_reauth_lock(self.slot)
        self.assertIsNotNone(handle)
        # R13-1：锁文件常驻，它的存在不代表被持有。
        self.assertTrue(Path(handle.path).exists())
        # A second holder must not get the same lock while it is held.
        self.assertIsNone(chat2api._acquire_reauth_lock(self.slot))
        chat2api._release_reauth_lock(handle)
        self.assertTrue(Path(handle.path).exists())
        again = chat2api._acquire_reauth_lock(self.slot)
        self.assertIsNotNone(again)
        chat2api._release_reauth_lock(again)

    def test_aged_lock_file_of_a_live_holder_is_not_reclaimed(self):
        # R13-1：旧实现按 mtime 判「持有者已崩溃」再按路径 unlink；双方可以各删
        # 对方刚建好的锁并同时持锁。现在回收完全不存在：人为做旧 mtime 也不能
        # 夺走活持有者的锁，被挡下的一方只能重新观察（这里表现为如实拒绝）。
        handle = chat2api._acquire_reauth_lock(self.slot)
        self.assertIsNotNone(handle)
        try:
            lock_path = Path(handle.path)
            stale = time.time() - 3600
            os.utime(lock_path, (stale, stale))
            self.assertIsNone(chat2api._acquire_reauth_lock(self.slot))
            # 持有句柄本身阻止按路径删除：不存在「删掉对方的锁」这条路。
            with self.assertRaises(OSError):
                os.unlink(lock_path)
            self.assertTrue(lock_path.exists())
            granted, reason, _ledger = chat2api.acquire_reauth_slot(
                self.slot, pid=222, ttl_s=60, now=1000.0)
            self.assertFalse(granted)
            self.assertIn(reason, ("global_session_active",
                                  "coordination_unavailable"))
        finally:
            chat2api._release_reauth_lock(handle)

    def test_unavailable_lock_refuses_the_session(self):
        with mock.patch.object(chat2api.os, "open",
                              side_effect=OSError("no coordination")):
            granted, reason, ledger = chat2api.acquire_reauth_slot(
                self.slot, pid=111, ttl_s=60, now=1000.0)
        self.assertFalse(granted)
        self.assertEqual(reason, "coordination_unavailable")
        self.assertEqual(ledger["attempts_in_window"], 0)

    def test_concurrent_acquire_grants_exactly_one_instance(self):
        # 两个「实例」在同一瞬间申请：只有一方被授予，且账本只记一次提交。
        # 起跑线在 acquire 之前：锁只覆盖读改写，等待绝不能塞进临界区。
        start = threading.Barrier(2)
        results = []

        def acquire(pid):
            start.wait(timeout=5)
            results.append(chat2api.acquire_reauth_slot(
                self.slot, pid=pid, ttl_s=60, now=1000.0))

        threads = [threading.Thread(target=acquire, args=(pid,))
                   for pid in (111, 222)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        granted = [r for r in results if r[0]]
        self.assertEqual(len(granted), 1, results)
        # 被拒方必须如实报出拒绝原因：拿到锁但看到对方的租约是
        # global_session_active，负载下没抢到内核互斥锁是
        # coordination_unavailable（同一容忍集见本文件跨进程用例）。
        self.assertIn([r[1] for r in results if not r[0]][0],
                      ("global_session_active", "coordination_unavailable"),
                      results)
        state, error = chat2api._read_reauth_slot(self.slot)
        self.assertIsNone(error)
        self.assertEqual(len(state["attempts"]), 1)


class TestCrossInstanceGate(unittest.TestCase):
    """A13-3：两个实例共用同一 UM 身份时的跨实例租约与全局尝试上限。"""

    def setUp(self):
        isolate_reauth_slot(self)
        self.slot = chat2api.REAUTH_SLOT_FILE

    def test_second_instance_is_refused_while_a_session_is_live(self):
        granted, reason, ledger = chat2api.acquire_reauth_slot(
            self.slot, pid=111, ttl_s=60, now=1000.0)
        self.assertTrue(granted)
        self.assertIsNone(reason)
        self.assertEqual(ledger["attempts_in_window"], 1)

        granted, reason, ledger = chat2api.acquire_reauth_slot(
            self.slot, pid=222, ttl_s=60, now=1000.5)
        self.assertFalse(granted)
        self.assertEqual(reason, "global_session_active")
        self.assertEqual(ledger["owner_pid"], 111)

    def test_expired_lease_is_reclaimed(self):
        chat2api.acquire_reauth_slot(self.slot, pid=111, ttl_s=60, now=1000.0)
        granted, reason, _ledger = chat2api.acquire_reauth_slot(
            self.slot, pid=222, ttl_s=60, now=1061.0)
        self.assertTrue(granted)
        self.assertIsNone(reason)

    @staticmethod
    def _local_proxy():
        """A Proxy whose auth state can be driven without any network call."""
        return chat2api.Proxy({"token": "saved", "api_key": None}, _auth_args())

    def test_refused_session_raises_the_throttle_exception(self):
        # F3：被跨实例门拒绝的会话从未判断凭据，必须以节流异常上报，
        # 且它仍是 ReauthenticationRequired 的子类，既有处理链不受影响。
        proxy = self._local_proxy()
        with mock.patch.object(proxy, "auth_status",
                              return_value={"error":
                                            "reauth_refused:global_session_active"}):
            with self.assertRaises(chat2api.ReauthenticationThrottled) as caught:
                proxy._raise_auth_required()
        self.assertEqual(caught.exception.reason, "global_session_active")
        self.assertIsInstance(caught.exception,
                              chat2api.ReauthenticationRequired)

    def test_judged_failure_still_raises_the_required_exception(self):
        proxy = self._local_proxy()
        with mock.patch.object(proxy, "auth_status",
                              return_value={"error": "needs_login"}):
            with self.assertRaises(chat2api.ReauthenticationRequired) as caught:
                proxy._raise_auth_required()
        self.assertNotIsInstance(caught.exception,
                                 chat2api.ReauthenticationThrottled)

    def test_login_origin_never_stores_a_query_string(self):
        # F4a：登录页 URL 可能带一次性 SSO 码/SAML artifact，状态里只留 origin+path。
        proxy = self._local_proxy()
        proxy._note_login_progress({
            "phase": "awaiting_duo",
            "origin": "https://websso.um.edu.mo/login?code=ONE-TIME-SECRET#frag",
        })
        self.assertEqual(proxy.auth_status()["login"]["origin"],
                         "https://websso.um.edu.mo/login")

    def test_health_snapshot_carries_only_the_coarse_state(self):
        # F4b：/health 无需控制令牌，因此只带 state/label，细节留在
        # /admin/auth/status。
        proxy = self._local_proxy()
        proxy._note_login_progress({
            "phase": "awaiting_duo",
            "origin": "https://websso.um.edu.mo/login",
            "manual_reason": "mfa_required",
        })
        snapshot = proxy.health_snapshot()
        self.assertEqual(sorted(snapshot["authentication"]),
                         ["label", "state"])
        detail = proxy.auth_status()
        self.assertIn("login", detail)
        self.assertIn("evidence", detail)

    def test_same_pid_reacquire_is_idempotent(self):
        chat2api.acquire_reauth_slot(self.slot, pid=111, ttl_s=60, now=1000.0)
        granted, _reason, ledger = chat2api.acquire_reauth_slot(
            self.slot, pid=111, ttl_s=60, now=1000.2)
        self.assertTrue(granted)
        self.assertEqual(ledger["attempts_in_window"], 1)

    def test_global_attempt_cap_bounds_the_ledger(self):
        cap = chat2api.REAUTH_MAX_ATTEMPTS_PER_WINDOW
        base = time.time()
        for index in range(cap):
            granted, _reason, _ledger = chat2api.acquire_reauth_slot(
                self.slot, pid=100 + index, ttl_s=5, now=base + index)
            self.assertTrue(granted)
            chat2api.release_reauth_slot(self.slot, pid=100 + index)

        granted, reason, ledger = chat2api.acquire_reauth_slot(
            self.slot, pid=999, ttl_s=5, now=base + cap)
        self.assertFalse(granted)
        self.assertEqual(reason, "global_attempt_cap")
        self.assertEqual(ledger["attempts_in_window"], cap)

    def test_window_self_clears_so_the_cap_is_not_permanent(self):
        cap = chat2api.REAUTH_MAX_ATTEMPTS_PER_WINDOW
        for index in range(cap):
            chat2api.acquire_reauth_slot(self.slot, pid=100 + index,
                                         ttl_s=5, now=1000.0 + index)
            chat2api.release_reauth_slot(self.slot, pid=100 + index)

        granted, _reason, _ledger = chat2api.acquire_reauth_slot(
            self.slot, pid=999, ttl_s=5,
            now=1000.0 + chat2api.REAUTH_ATTEMPT_WINDOW_S + cap)
        self.assertTrue(granted)

    def test_successful_session_clears_the_ledger(self):
        chat2api.acquire_reauth_slot(self.slot, pid=111, ttl_s=60, now=1000.0)
        chat2api.release_reauth_slot(self.slot, pid=111, success=True)
        _granted, _reason, ledger = chat2api.acquire_reauth_slot(
            self.slot, pid=222, ttl_s=60, now=1000.5)
        self.assertEqual(ledger["attempts_in_window"], 1)

    def test_release_never_releases_another_instances_lease(self):
        chat2api.acquire_reauth_slot(self.slot, pid=111, ttl_s=60, now=1000.0)
        chat2api.release_reauth_slot(self.slot, pid=222)
        granted, reason, _ledger = chat2api.acquire_reauth_slot(
            self.slot, pid=333, ttl_s=60, now=1000.5)
        self.assertFalse(granted)
        self.assertEqual(reason, "global_session_active")


class TestDamagedSlotIsNotAnEmptySlot(unittest.TestCase):
    """R13-2：只有「文件确定缺席」才按首次初始化处理。

    损坏、截断、非对象、不可读的槽位一律 fail-closed：拒绝新会话并如实
    报出原因，绝不把损坏状态当空槽放行（那会把全局尝试上限从 1 重新计数）。
    """

    def setUp(self):
        isolate_reauth_slot(self)
        self.slot = chat2api.REAUTH_SLOT_FILE

    def _assert_refused(self, expected_reason, pid=111):
        before = Path(self.slot).read_bytes()
        granted, reason, ledger = chat2api.acquire_reauth_slot(
            self.slot, pid=pid, ttl_s=60, now=1000.0)
        self.assertFalse(granted)
        self.assertEqual(reason, expected_reason)
        self.assertEqual(ledger, {"owner_pid": None, "expires_epoch": 0.0,
                                "attempts_in_window": 0})
        # 证据保留：损坏文件逐字节未变，运维仍能看到现场。
        self.assertEqual(Path(self.slot).read_bytes(), before)

    def test_corrupt_slot_file_refuses_new_sessions(self):
        # 原 test_corrupt_slot_file_is_an_empty_slot 固化了错误行为（损坏即放行）。
        Path(self.slot).write_text("{not json", encoding="utf-8")
        self._assert_refused("slot_corrupt")

    def test_truncated_json_refuses_the_session(self):
        Path(self.slot).write_text(
            '{"owner_pid": 111, "attempts": [1.0, 2.0', encoding="utf-8")
        self._assert_refused("slot_corrupt")

    def test_non_object_json_refuses_the_session(self):
        for payload in ("[1, 2]", '"text"', "42", "null"):
            with self.subTest(payload=payload):
                Path(self.slot).write_text(payload, encoding="utf-8")
                self._assert_refused("slot_corrupt")

    def test_junk_ledger_refuses_the_session(self):
        # JSON 合法但账本形状是垃圾：不能当成空账本（会抹掉全局上限）。
        shapes = (
            '{"attempts": ["x", "y", "z"]}',
            '{"attempts": {"1.0": true}}',
            '{"attempts": null}',
            '{"attempts": [1.0, "2.0"]}',
            # 类型错误的持有者/到期时间会被读成「没有活持有者」：同样不可信。
            '{"owner_pid": "111", "expires_epoch": 9999999999.0}',
            '{"owner_pid": 111, "expires_epoch": "later"}',
            '{"owner_pid": true, "expires_epoch": 9999999999.0}',
            '{"owner_pid": 111, "expires_epoch": null}',
        )
        for payload in shapes:
            with self.subTest(payload=payload):
                Path(self.slot).write_text(payload, encoding="utf-8")
                self._assert_refused("slot_corrupt")

    def test_non_finite_numbers_refuse_the_session(self):
        # NaN/Infinity 是 json.loads 接受的裸 token，但 NaN 参与的比较恒为 False：
        # 会被读成「没有活持有者」「条目不在窗口内」，等于放行并抹掉上限（R13-2）。
        shapes = (
            '{"owner_pid": 111, "expires_epoch": NaN}',
            '{"owner_pid": null, "started_epoch": 0.0, "expires_epoch": 0.0,'
            ' "attempts": [NaN]}',
            '{"owner_pid": null, "started_epoch": 0.0, "expires_epoch": 0.0,'
            ' "attempts": [Infinity, Infinity, Infinity]}',
        )
        for payload in shapes:
            with self.subTest(payload=payload):
                Path(self.slot).write_text(payload, encoding="utf-8")
                self._assert_refused("slot_corrupt")

    def test_partial_object_refuses_the_session(self):
        # 半截状态（缺 attempts）不得当成空账本：全局节流事实不能被静默清空。
        shapes = (
            '{"owner_pid": null, "started_epoch": 0.0, "expires_epoch": 0.0}',
            '{"owner_pid": null, "started_epoch": 0.0, "expires_epoch": 0.0,'
            ' "attempts": null}',
        )
        for payload in shapes:
            with self.subTest(payload=payload):
                Path(self.slot).write_text(payload, encoding="utf-8")
                self._assert_refused("slot_corrupt")

    def test_deeply_nested_payload_refuses_the_session(self):
        # 语法合法但深度嵌套：RecursionError 不是 ValueError，若不接住会以 500
        # 穿出，而不是如实的 slot_corrupt 拒绝。
        Path(self.slot).write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
        self._assert_refused("slot_corrupt")

    def test_a_released_slot_written_by_this_code_is_accepted(self):
        # 严格读取不能拒绝本代码自己写出的合法状态（release 后的空槽）。
        Path(self.slot).write_text(json.dumps({
            "owner_pid": None, "started_epoch": 0.0,
            "expires_epoch": 0.0, "attempts": []}), encoding="utf-8")
        granted, reason, ledger = chat2api.acquire_reauth_slot(
            self.slot, pid=111, ttl_s=60, now=1000.0)
        self.assertTrue(granted)
        self.assertIsNone(reason)
        self.assertEqual(ledger["attempts_in_window"], 1)

    def test_unreadable_slot_refuses_the_session(self):
        # 真实不可读：槽位路径上是一个目录（Windows 上 open() 报 Errno 13）。
        os.mkdir(self.slot)
        granted, reason, ledger = chat2api.acquire_reauth_slot(
            self.slot, pid=111, ttl_s=60, now=1000.0)
        self.assertFalse(granted)
        self.assertEqual(reason, "slot_unreadable")
        self.assertEqual(ledger["attempts_in_window"], 0)

    def test_a_denied_open_refuses_the_session(self):
        # 权限错误（在 open 边界模拟）：同样 fail-closed，不冒充空槽。
        real_open = open

        def denied(path, *args, **kwargs):
            if str(path) == self.slot:
                raise PermissionError(13, "Permission denied")
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=denied):
            granted, reason, ledger = chat2api.acquire_reauth_slot(
                self.slot, pid=111, ttl_s=60, now=1000.0)
        self.assertFalse(granted)
        self.assertEqual(reason, "slot_unreadable")
        self.assertEqual(ledger["attempts_in_window"], 0)

    def test_damage_does_not_clear_the_global_attempt_cap(self):
        # 先填满 3 次滚动窗口（正常应拒绝），再把文件写坏：损坏不能绕过上限。
        cap = chat2api.REAUTH_MAX_ATTEMPTS_PER_WINDOW
        base = time.time()
        for index in range(cap):
            granted, _reason, _ledger = chat2api.acquire_reauth_slot(
                self.slot, pid=100 + index, ttl_s=5, now=base + index)
            self.assertTrue(granted)
            chat2api.release_reauth_slot(self.slot, pid=100 + index)
        granted, reason, _ledger = chat2api.acquire_reauth_slot(
            self.slot, pid=999, ttl_s=5, now=base + cap)
        self.assertFalse(granted)
        self.assertEqual(reason, "global_attempt_cap")

        Path(self.slot).write_text('{"attempts": [', encoding="utf-8")
        granted, reason, ledger = chat2api.acquire_reauth_slot(
            self.slot, pid=999, ttl_s=5, now=base + cap + 1)
        self.assertFalse(granted)
        self.assertEqual(reason, "slot_corrupt")
        self.assertEqual(ledger["attempts_in_window"], 0)

    def test_release_leaves_the_damaged_slot_untouched(self):
        # release 读不出来时直接返回：覆盖会把 fail-closed 状态变回空槽。
        Path(self.slot).write_text("{not json", encoding="utf-8")
        before = Path(self.slot).read_bytes()
        chat2api.release_reauth_slot(self.slot, pid=111, success=True)
        self.assertEqual(Path(self.slot).read_bytes(), before)

    def test_an_absent_slot_is_a_first_initialisation(self):
        # 唯一合法的「空槽」：文件确定缺席。
        self.assertFalse(Path(self.slot).exists())
        granted, reason, ledger = chat2api.acquire_reauth_slot(
            self.slot, pid=111, ttl_s=60, now=1000.0)
        self.assertTrue(granted)
        self.assertIsNone(reason)
        self.assertEqual(ledger["attempts_in_window"], 1)

    def test_removing_the_damaged_file_recovers_the_slot(self):
        # 恢复路径：损坏文件是唯一证据，由运维显式移除后才重新按首次初始化。
        Path(self.slot).write_text("{not json", encoding="utf-8")
        self._assert_refused("slot_corrupt")
        os.unlink(self.slot)
        granted, reason, ledger = chat2api.acquire_reauth_slot(
            self.slot, pid=111, ttl_s=60, now=1000.0)
        self.assertTrue(granted)
        self.assertIsNone(reason)
        self.assertEqual(ledger["attempts_in_window"], 1)


class TestCrossProcessDamagedSlot(CrossProcessLockHarness):
    """R13-2 跨进程：损坏槽位对两个真实进程同时 fail-closed。

    已有损坏用例都在单进程内直接调 ``acquire_reauth_slot``；这里把同一个预置
    损坏的槽位交给两个真实子进程同时争用，断言双方都被拒绝、原因如实、且
    损坏文件逐字节保留（谁都没有把它当空槽覆盖掉）。
    """

    def test_two_processes_refuse_a_shared_corrupt_slot(self):
        slot = str(self.dir / "corrupt.state")
        Path(slot).write_text("{not json", encoding="utf-8")
        before = Path(slot).read_bytes()
        steps = [{"mode": "session", "slot": slot, "ttl_s": 60.0,
                  "now": 1000.0, "lock_wait_s": 30.0,
                  "signal": str(self.dir / "corrupt.go")}]
        out_paths, procs = self.start(steps)

        results = self.run_step(out_paths, procs, 0, steps[0]["signal"])

        for pid, result in sorted(results.items()):
            self.assertNotIn("error", result, self.child_report(procs))
            self.assertFalse(result["granted"], result)
            self.assertEqual(result["reason"], "slot_corrupt", result)
            # 什么都没读到，就什么都不声称：账本按「未观测」上报。
            self.assertEqual(result["ledger"], {
                "owner_pid": None, "expires_epoch": 0.0,
                "attempts_in_window": 0}, result)
        # 证据保留：损坏文件未被任何一方改写（覆盖它等于把 fail-closed 变回空槽）。
        self.assertEqual(Path(slot).read_bytes(), before)
        state, error = chat2api._read_reauth_slot(slot)
        self.assertEqual(error, "corrupt")


class TestRefreshUnderGate(unittest.TestCase):
    """A13-3：单飞、租约拒绝与晚到结果代次守卫。"""

    def setUp(self):
        isolate_reauth_slot(self)

    def test_refused_session_is_a_throttle_not_a_failure(self):
        # 另一实例正持有租约：业务请求 401 后触发重认证，本次会话被拒——
        # 状态如实为「需要登录」且带拒绝原因，不冒充认证失败，也不开新窗口。
        chat2api.acquire_reauth_slot(chat2api.REAUTH_SLOT_FILE, pid=111,
                                      ttl_s=60)
        proxy = chat2api.Proxy({"token": "stale", "api_key": None},
                              _auth_args())
        rejected = mock.Mock(status_code=401)
        with (
            mock.patch.object(chat2api.requests, "request",
                              return_value=rejected),
            mock.patch.object(chat2api, "browser_login") as browser_login,
        ):
            with self.assertRaises(chat2api.ReauthenticationRequired):
                proxy.call("GET", "/api/models")

        browser_login.assert_not_called()
        status = proxy.auth_status()
        self.assertEqual(status["state"], "needs_login")   # never "failed"
        self.assertTrue(status["error"].startswith("reauth_refused:"))
        self.assertEqual(status["evidence"]["verdict"], "rejected")
        self.assertEqual(status["evidence"]["source"], "business")
        self.assertEqual(status["gate"]["granted"], False)
        self.assertEqual(status["gate"]["reason"], "global_session_active")

    def test_corrupt_slot_is_reported_as_a_refusal_not_a_failure(self):
        # R13-2 拒绝面：损坏槽位必须如实报出（reauth_refused:slot_corrupt），
        # 走既有 gate/throttle 通道，让 /admin/auth/status 与分享控制台看得见；
        # /health 仍只给 state/label（F4b），细节留在控制令牌门后。
        Path(chat2api.REAUTH_SLOT_FILE).write_text("{not json",
                                                  encoding="utf-8")
        proxy = chat2api.Proxy({"token": "stale", "api_key": None},
                              _auth_args())
        with mock.patch.object(chat2api, "browser_login") as browser_login:
            self.assertFalse(proxy.refresh(force=True))

        browser_login.assert_not_called()
        status = proxy.auth_status()
        # 拒绝不是认证失败：state 不被改写成 failed，原因如实报出。
        self.assertNotEqual(status["state"], "failed")
        self.assertEqual(status["error"], "reauth_refused:slot_corrupt")
        self.assertEqual(status["gate"]["granted"], False)
        self.assertEqual(status["gate"]["reason"], "slot_corrupt")
        self.assertEqual(sorted(proxy.health_snapshot()["authentication"]),
                         ["label", "state"])
        with self.assertRaises(chat2api.ReauthenticationThrottled) as caught:
            proxy._raise_auth_required()
        self.assertEqual(caught.exception.reason, "slot_corrupt")
        self.assertIsInstance(caught.exception,
                              chat2api.ReauthenticationRequired)

    def test_successful_session_clears_the_shared_ledger(self):
        proxy = chat2api.Proxy({"token": "stale", "api_key": None},
                              _auth_args())
        with (
            mock.patch.object(chat2api, "browser_login",
                              return_value="fresh-token"),
            mock.patch.object(chat2api.TokenStore, "save"),
        ):
            self.assertTrue(proxy.refresh(force=True))
        state, error = chat2api._read_reauth_slot(chat2api.REAUTH_SLOT_FILE)
        self.assertIsNone(error)
        self.assertEqual(state.get("attempts"), [])
        self.assertIsNone(state.get("owner_pid"))
        self.assertEqual(proxy.auth_status()["state"], "valid")

    def test_no_browser_still_short_circuits_without_a_lease(self):
        proxy = chat2api.Proxy({"token": "stale", "api_key": None},
                              _auth_args(no_browser=True))
        with mock.patch.object(chat2api, "acquire_reauth_slot") as acquire:
            self.assertFalse(proxy.refresh(force=True, wait=False))
        acquire.assert_not_called()
        self.assertEqual(proxy.auth_status()["error"], "BrowserDisabled")

    def test_late_result_after_shutdown_is_discarded(self):
        proxy = chat2api.Proxy({"token": "stale-token", "api_key": None},
                              _auth_args())
        entered = threading.Event()

        def fake_login(*args, **kwargs):
            # Models the race the guard exists for: the session reads a token
            # in the same window in which shutdown supersedes it.
            cancel = kwargs.get("cancel_event")
            entered.set()
            while not (cancel and cancel.is_set()):
                time.sleep(0.01)
            return "late-token"

        with (
            mock.patch.object(chat2api, "browser_login",
                              side_effect=fake_login),
            mock.patch.object(chat2api.TokenStore, "save") as save,
        ):
            self.assertTrue(proxy.refresh(force=True, wait=False))
            self.assertTrue(entered.wait(2))
            proxy.shutdown()

        self.assertEqual(proxy.creds, {"token": "stale-token", "api_key": None})
        save.assert_not_called()
        status = proxy.auth_status()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["error"], "superseded")

    def test_discarded_late_result_still_releases_the_lease(self):
        # 迟到结果被丢弃后，租约必须由同一个 finally 释放：否则另一实例会被一个
        # 已经停止的会话挡到租约自然过期（login_timeout+30s）为止。失败会话
        # 不清账本，全局尝试上限继续有效。
        proxy = chat2api.Proxy({"token": "stale-token", "api_key": None},
                              _auth_args())
        entered = threading.Event()

        def fake_login(*args, **kwargs):
            cancel = kwargs.get("cancel_event")
            entered.set()
            while not (cancel and cancel.is_set()):
                time.sleep(0.01)
            return "late-token"

        with (
            mock.patch.object(chat2api, "browser_login", side_effect=fake_login),
            mock.patch.object(chat2api.TokenStore, "save") as save,
        ):
            self.assertTrue(proxy.refresh(force=True, wait=False))
            self.assertTrue(entered.wait(2))
            # 会话进行中：另一个 pid 此刻拿不到租约。
            other = chat2api.acquire_reauth_slot(
                chat2api.REAUTH_SLOT_FILE, pid=999999, ttl_s=60)
            self.assertFalse(other[0])
            self.assertEqual(other[1], "global_session_active")
            proxy.shutdown()

        save.assert_not_called()
        state, error = chat2api._read_reauth_slot(chat2api.REAUTH_SLOT_FILE)
        self.assertIsNone(error)
        self.assertIsNone(state["owner_pid"])       # 租约已释放
        self.assertEqual(len(state["attempts"]), 1)  # 失败会话的尝试仍在账本里
        granted, _reason, ledger = chat2api.acquire_reauth_slot(
            chat2api.REAUTH_SLOT_FILE, pid=999999, ttl_s=60)
        self.assertTrue(granted)
        self.assertEqual(ledger["attempts_in_window"], 2)

    def test_cancelled_session_releases_the_lease_and_keeps_the_ledger(self):
        # 取消（chat2api 正在停止）与登录停滞一样是失败会话：它必须释放租约让
        # 另一实例可以重试，同时把这次尝试留在滚动账本里。
        proxy = chat2api.Proxy({"token": "stale", "api_key": None},
                              _auth_args())
        with mock.patch.object(
                chat2api, "browser_login",
                side_effect=RuntimeError(
                    "Login cancelled because chat2api is stopping")):
            self.assertFalse(proxy.refresh(force=True))

        state, error = chat2api._read_reauth_slot(chat2api.REAUTH_SLOT_FILE)
        self.assertIsNone(error)
        self.assertIsNone(state["owner_pid"])
        self.assertEqual(len(state["attempts"]), 1)
        self.assertEqual(proxy.auth_status()["error"], "RuntimeError")

    def test_login_stall_reason_reaches_the_console(self):
        proxy = chat2api.Proxy({"token": "stale", "api_key": None},
                              _auth_args())
        with (
            mock.patch.object(
                chat2api, "browser_login",
                side_effect=chat2api.LoginStalled(
                    "umpass_prefill_missing", "last page https://websso.um.edu.mo/adfs/ls/")),
            mock.patch.object(chat2api.TokenStore, "save"),
        ):
            self.assertFalse(proxy.refresh(force=True))
        status = proxy.auth_status()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["error"], "umpass_prefill_missing")


if __name__ == "__main__":
    unittest.main()
