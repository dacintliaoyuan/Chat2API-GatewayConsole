# -*- coding: utf-8 -*-
"""A.2(a)：两个完整 chat2api 实例共享一个重认证租约（跨进程，无真实 Edge）。

既有跨进程用例只驱动锁/槽函数（``acquire_reauth_slot``）；这里起**两个真实
chat2api 子进程**，各自带自己的 HTTP 面、token 文件、服务日志、用量账本和控制
秘密，共享同一个 ``CHAT2API_REAUTH_SLOT_FILE``。只有最末端的 ``browser_login``
被替换成「写标记文件 + 等释放文件」的替身，因此：

* 真实 Edge 绝不被打开，没有任何真实网络或账号交互；
* 真实 ``main()``、真实 ``Proxy.refresh``、真实 ``acquire_reauth_slot`` 与真实
  HTTP 处理器全部在链路上。

断言的是节流契约：A 持有活会话时，B 的重认证触发与业务请求都被拒绝且如实上报
（``reauth_refused:global_session_active``），B 绝不开第二个提交窗口；A 的会话
结束后租约释放，B 才被授予——拒绝来自「有活会话」这个事实，而不是永久阻塞。

A1 用例（``EarlyReleaseHandoverTests``）复用同一装配：A 提前释放租约后，等待中的
B 必须在 2 秒内自行接续并打开自己的窗口，不得睡到陈旧的租约到期点。
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


CHAT2API_PATH = Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py"
CONTROL_HEADER = "X-Admin-Control"
CONTROL_TOKEN_A = "instance-a-control-secret"
CONTROL_TOKEN_B = "instance-b-control-secret"


# 子进程替身：按路径载入真实 chat2api.py，替换最末端的 browser_login，然后
# 调用真实 main()（真实 HTTP 面、真实 Proxy.refresh、真实跨进程租约）。
_INSTANCE_CHILD_SCRIPT = '''\
import importlib.util
import json
import os
import sys
import time


def load_chat2api(path):
    spec = importlib.util.spec_from_file_location("chat2api_instance_child", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main():
    module_path, plan_path = sys.argv[1:3]
    chat2api = load_chat2api(module_path)
    with open(plan_path, encoding="utf-8") as f:
        plan = json.load(f)
    marker = plan["marker"]

    def fake_browser_login(base_url, profile_dir, timeout_s, **kwargs):
        # 会话窗口的替身：只记录「窗口被打开」，绝不打开真实 Edge。
        with open(marker, "a", encoding="utf-8") as f:
            f.write("window\\n")
        release = plan.get("release")
        if release:
            deadline = time.time() + 60.0
            while not os.path.exists(release):
                if time.time() > deadline:
                    raise RuntimeError("hold deadline exceeded")
                time.sleep(0.02)
        return plan["token"]

    chat2api.browser_login = fake_browser_login
    sys.argv = [module_path] + plan["argv"]
    chat2api.main()


main()
'''


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _TwoInstanceHarness:
    """两个真实 chat2api 子进程的共享装配（每个测试类独占一套实例）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.dir = Path(cls.tmp.name)
        cls.script = cls.dir / "instance_child.py"
        cls.script.write_text(_INSTANCE_CHILD_SCRIPT, encoding="utf-8")
        cls.slot = str(cls.dir / "reauth.state")
        cls.release = cls.dir / "release-a"
        cls.requests = []
        cls.model_status = [200]
        requests_log = cls.requests
        model_status = cls.model_status

        class Upstream(BaseHTTPRequestHandler):
            def do_GET(inner):
                requests_log.append(inner.path)
                if inner.path == "/api/config":
                    payload = json.dumps({"version": "test",
                                          "features": {}}).encode()
                    inner.send_response(200)
                elif model_status[0] != 200:
                    payload = b'{"error": "rejected"}'
                    inner.send_response(model_status[0])
                else:
                    payload = json.dumps({"object": "list",
                                          "data": []}).encode()
                    inner.send_response(200)
                inner.send_header("Content-Type", "application/json")
                inner.send_header("Content-Length", str(len(payload)))
                inner.end_headers()
                inner.wfile.write(payload)

            def log_message(inner, *_args):
                pass

        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        cls.upstream_url = "http://127.0.0.1:%d" % cls.upstream.server_port
        cls.upstream_thread = threading.Thread(
            target=cls.upstream.serve_forever, daemon=True)
        cls.upstream_thread.start()
        cls.children = []
        cls.ports = {}
        cls.controls = {}
        cls.markers = {}
        cls.pid_a = None
        cls.pid_b = None
        try:
            cls.pid_a = cls._start_instance("a", CONTROL_TOKEN_A,
                                             hold=True)
            cls.pid_b = cls._start_instance("b", CONTROL_TOKEN_B)
            for name in ("a", "b"):
                cls._await_health(name)
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def _start_instance(cls, name, control_token, hold=False):
        token_file = cls.dir / ("%s-token.json" % name)
        token_file.write_text(json.dumps({
            "token": "%s-initial-token" % name, "api_key": None}),
            encoding="utf-8")
        control_file = cls.dir / ("%s-control" % name)
        control_file.write_text(control_token, encoding="utf-8")
        marker = cls.dir / ("%s-login.marker" % name)
        port = _free_port()
        plan = {
            "token": "%s-fresh-token" % name,
            "marker": str(marker),
            "argv": ["--base-url", cls.upstream_url, "--port", str(port),
                     "--profile", str(cls.dir / ("%s-profile" % name)),
                     "--token", "%s-initial-token" % name,
                     "--login-timeout", "60"],
        }
        if hold:
            plan["release"] = str(cls.release)
        plan_path = cls.dir / ("%s-plan.json" % name)
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        env = dict(os.environ)
        env["CHAT2API_TOKEN_FILE"] = str(token_file)
        env["CHAT2API_SERVICE_LOG"] = str(cls.dir / ("%s.log" % name))
        env["CHAT2API_USAGE_LOG"] = str(cls.dir / ("%s-usage.jsonl" % name))
        env["CHAT2API_CONTROL_TOKEN_FILE"] = str(control_file)
        env["CHAT2API_REAUTH_SLOT_FILE"] = cls.slot
        # The business request must use a small auth-wait budget: the default
        # 30s would outlive this test's 15s HTTP client timeout. The A1
        # 2s continuation uses the recovery loop, not this request budget.
        env["CHAT2API_AUTH_WAIT_BUDGET_S"] = "1"
        env["NO_PROXY"] = "*"
        env.pop("HTTP_PROXY", None)
        env.pop("HTTPS_PROXY", None)
        log = open(str(cls.dir / ("%s-child.log" % name)), "wb")
        try:
            proc = subprocess.Popen(
                [sys.executable, str(cls.script), str(CHAT2API_PATH),
                 str(plan_path)],
                cwd=str(CHAT2API_PATH.parent), env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        finally:
            log.close()
        cls.children.append(proc)
        cls.ports[name] = port
        cls.controls[name] = control_token
        cls.markers[name] = marker
        return proc.pid

    @classmethod
    def _await_health(cls, name, timeout=60.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                status, payload = cls._request(name, "GET", "/health")
                if status == 200 and payload.get("status") == "ok":
                    return payload
                last = (status, payload)
            except OSError as exc:
                last = exc
            time.sleep(0.2)
        raise AssertionError("instance %s never became healthy: %r\n%s"
                             % (name, last, cls.child_report()))

    @classmethod
    def _request(cls, name, method, path, control=False, body=None,
                 timeout=15):
        headers = {"Content-Type": "application/json"}
        if control:
            headers[CONTROL_HEADER] = cls.controls[name]
        conn = http.client.HTTPConnection("127.0.0.1", cls.ports[name],
                                          timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            conn.close()

    @classmethod
    def child_report(cls):
        parts = []
        for child in cls.children:
            log_path = cls.dir / ("%s-child.log"
                                  % ("a" if child.pid == cls.pid_a else "b"))
            tail = ""
            if log_path.exists():
                tail = log_path.read_text(encoding="utf-8",
                                           errors="replace")[-1500:]
            parts.append("pid=%s rc=%s tail=%s" % (child.pid, child.poll(), tail))
        return " | ".join(parts)

    @classmethod
    def tearDownClass(cls):
        cls.release.touch()
        for child in cls.children:
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.upstream_thread.join()
        cls.tmp.cleanup()

    @classmethod
    def _await_slot(cls, predicate, timeout=30.0, message=""):
        deadline = time.monotonic() + timeout
        state = None
        while time.monotonic() < deadline:
            try:
                with open(cls.slot, encoding="utf-8") as f:
                    state = json.load(f)
            except (OSError, ValueError):
                state = None
            if state is not None and predicate(state):
                return state
            time.sleep(0.05)
        raise AssertionError("slot never matched %s (last=%r) %s"
                              % (message, state, cls.child_report()))

    def _await_status(self, name, predicate, timeout=30.0, message=""):
        deadline = time.monotonic() + timeout
        payload = None
        while time.monotonic() < deadline:
            status, payload = self._request(name, "GET", "/admin/auth/status",
                                            control=True)
            self.assertEqual(status, 200, payload)
            if predicate(payload):
                return payload
            time.sleep(0.1)
        raise AssertionError("instance %s status never matched %s (last=%r)\n%s"
                              % (name, message, payload, self.child_report()))

    def _marker_lines(self, name):
        marker = type(self).markers[name]
        if not marker.exists():
            return 0
        return len([line for line in marker.read_text(encoding="utf-8").splitlines()
                    if line.strip()])

    def _wait_for_marker(self, name, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._marker_lines(name):
                return True
            time.sleep(0.05)
        return False

    @classmethod
    def _read_slot(cls):
        try:
            with open(cls.slot, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _await_marker_after(self, name, released_at, timeout):
        """Seconds between ``released_at`` and the marker, or None on timeout."""
        deadline = released_at + timeout
        while True:
            if self._marker_lines(name):
                return time.monotonic() - released_at
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.02)


class TwoInstanceReauthenticationTests(_TwoInstanceHarness, unittest.TestCase):
    """A.2(a)：两个实例并发时，只有一个提交窗口，另一个被如实节流。"""

    def test_one_live_session_throttles_the_second_instance(self):
        cls = type(self)
        # 触发之前：共享槽位还没有任何租约（拒绝不是来自既有状态）。
        self.assertFalse(Path(cls.slot).exists())

        # ---- 阶段 1：A 打开会话并持有租约（替身在释放文件出现前不返回）。
        status, payload = self._request("a", "POST", "/admin/auth/login",
                                        control=True)
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload["state"], "logging_in", payload)
        self.assertEqual(payload["gate"]["granted"], True, payload)
        self.assertEqual(payload["gate"]["owner_pid"], cls.pid_a, payload)
        self.assertTrue(self._wait_for_marker("a"),
                        "A 的会话没有进入窗口替身\n" + self.child_report())
        slot = self._await_slot(lambda state: state.get("owner_pid") == cls.pid_a,
                                message="owner_pid==A")
        self.assertEqual(len(slot["attempts"]), 1, slot)

        # ---- 阶段 2：B 的重认证触发被拒绝，且 B 绝不开第二个窗口。
        status, payload = self._request("b", "POST", "/admin/auth/login",
                                        control=True)
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload["gate"]["granted"], False, payload)
        self.assertEqual(payload["gate"]["reason"], "global_session_active",
                          payload)
        self.assertEqual(payload["gate"]["owner_pid"], cls.pid_a, payload)
        self.assertEqual(payload["error"],
                          "reauth_refused:global_session_active", payload)
        # 拒绝不是认证失败：凭据从未被判断，状态不得被改写成 failed。
        self.assertNotEqual(payload["state"], "failed", payload)
        self.assertFalse(cls.markers["b"].exists(),
                          "B 在 A 持有会话时打开了第二个提交窗口")

        # ---- 阶段 3：B 的业务请求同样被节流，而不是伪造成成功。
        # 它用 1 秒小预算等待共享恢复；活跃租约的旧到期点不是提前
        # 失败依据，等待耗尽后以 auth_wait_timeout 如实返回 503。
        cls.model_status[0] = 401
        status, payload = self._request("b", "GET", "/v1/models")
        self.assertEqual(status, 503, payload)
        self.assertEqual(payload["error"]["type"], "reauthentication_required",
                          payload)
        self.assertEqual(payload["error"]["detail"], "auth_wait_timeout",
                          payload)
        self.assertFalse(cls.markers["b"].exists(),
                          "业务路径触发后 B 打开了提交窗口")
        # A 的会话仍在原地：既没有第二个窗口，也没有被 B 顶掉。
        self.assertEqual(self._marker_lines("a"), 1)

        # ---- 阶段 4：A 的会话结束（迟到结果被接受）后租约释放；等待中的
        # B 在 2 秒内自行接续，不需要第二次手动触发。
        cls.release.touch()
        self._await_status("a", lambda s: s["state"] == "valid",
                            message="A 会话完成")
        self.assertTrue(
            self._wait_for_marker("b", timeout=2.0),
            "A 释放租约后 B 没有在 2 秒内自行接续\n"
            + self.child_report())
        self._await_status("b", lambda s: s["state"] == "valid",
                            message="B 自行接续完成")
        # A4：成功只重置当前实例的恢复轮次；滚动窗口内的全局会话启动
        # 记录保留（3 次/15 分钟是硬上限，不因成功提前清空）。
        # A 的 1 次 + B 自动接续的 1 次 = 2 次，且 B 结束后租约释放。
        self._await_slot(
            lambda state: (state.get("owner_pid") is None
                           and len(state.get("attempts") or []) == 2),
            message="A 成功后租约释放且两次启动记录保留")
        self.assertEqual(self._marker_lines("a"), 1,
                          "A 的登录窗口被重新打开")
        self.assertEqual(self._marker_lines("b"), 1,
                          "B 打开了多个登录窗口")


class EarlyReleaseHandoverTests(_TwoInstanceHarness, unittest.TestCase):
    """A1：等待中的实例在对方提前释放租约后自行接续，无需第二次手动触发。"""

    def test_a1_early_release_continues_without_a_second_manual_trigger(self):
        cls = type(self)
        self.assertFalse(
            Path(cls.slot).exists(),
            "A1-EARLY-RELEASE: the shared slot already exists before the "
            "test started; the harness is not isolated")

        # ---- 阶段 1：A 以长租约进入登录窗口并保持。
        status, payload = self._request("a", "POST", "/admin/auth/login",
                                        control=True)
        self.assertEqual(status, 202, payload)
        self.assertIs(payload["gate"]["granted"], True, payload)
        self.assertEqual(payload["gate"]["owner_pid"], cls.pid_a, payload)
        self.assertTrue(
            self._wait_for_marker("a"),
            "A's login window never opened\n" + self.child_report())
        slot = self._await_slot(
            lambda state: state.get("owner_pid") == cls.pid_a,
            message="owner_pid==A")
        self.assertEqual(len(slot["attempts"]), 1, slot)
        self.assertGreater(
            float(slot["expires_epoch"]) - time.time(), 30.0,
            "A's shared lease is too short for the early-release scenario; "
            "the harness needs a lease that outlives the 2s continuation "
            "window\n" + self.child_report())

        # ---- 阶段 2：B 只触发一次，进入共享恢复等待，但绝不开窗口。
        status, payload = self._request("b", "POST", "/admin/auth/login",
                                        control=True)
        self.assertEqual(status, 202, payload)
        self.assertIs(payload["gate"]["granted"], False, payload)
        self.assertEqual(payload["gate"]["reason"], "global_session_active",
                          payload)
        self.assertEqual(payload["gate"]["owner_pid"], cls.pid_a, payload)
        self.assertEqual(
            payload["recovery"]["state"], "waiting",
            "A1-EARLY-RELEASE: B did not enter the shared recovery wait "
            "after its single manual trigger; it must wait for the shared "
            "lease, not open a window or stop\n%s" % self.child_report())
        self.assertEqual(
            self._marker_lines("b"), 0,
            "A1-EARLY-RELEASE: B opened its own login window while A "
            "still held the shared lease")

        # ---- 阶段 3：A 提前释放；B 必须在 2 秒内自行接续并打开自己的窗口。
        released_at = time.monotonic()
        cls.release.touch()
        elapsed = self._await_marker_after("b", released_at, 2.0)
        self.assertIsNotNone(
            elapsed,
            "A1-EARLY-RELEASE: B did not open its own login window within "
            "2.0s after A released the shared lease; it kept sleeping to the "
            "stale lease expiry instead of re-reading the shared slot. "
            "elapsed=%.2fs slot=%r\n%s"
            % (time.monotonic() - released_at, self._read_slot(),
               self.child_report()))
        self.assertLessEqual(
            elapsed, 2.0,
            "A1-EARLY-RELEASE: B opened its own login window %.2fs after "
            "A released the shared lease (limit 2.0s); the handover must not "
            "wait for the stale lease expiry." % elapsed)
        self.assertEqual(
            self._marker_lines("a"), 1,
            "A1-EARLY-RELEASE: A reopened its login window after "
            "releasing the shared lease, so the two windows overlapped "
            "(marker_a=%d)." % self._marker_lines("a"))


if __name__ == "__main__":
    unittest.main()
