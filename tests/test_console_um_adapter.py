# -*- coding: utf-8 -*-
"""Read-only UM adapter tests (console P2).

Boots a stub HTTP server on an ephemeral port that impersonates chat2api's
``/admin/auth/status`` endpoint, with the runtime snapshot mocked to point
the adapter at the stub. No real service and no real token file is touched;
the token file path is patched to a temp file (module-constant patch, fixed
here as the chosen mechanism per adapter spec).

``VerifyAdapterTests`` reuses the same stub mechanism for the restricted
``/admin/auth/verify`` endpoint contract agreed with chat2api (not yet
shipped in production); the registry action stays disabled until the
endpoint lands, so the real path cannot execute prematurely.
"""

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, str(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from console import adapters as console_adapters  # noqa: E402  (same directory, sys.path prepared)
from console import um_adapter as console_um_adapter  # noqa: E402
from console.operations import OperationError  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="console-um-adapter-tests-")
_CONTROL_TOKEN = "test-um-control-token-42"

# state -> (label, error) as served by the stub
_FOUR_STATES = [
    ("valid", "有效", None),
    ("needs_login", "需要登录", None),
    ("logging_in", "登录中", None),
    ("failed", "已失效", "LoginError"),
]


class _StubStatusHandler(BaseHTTPRequestHandler):
    """Returns a per-server configured response and captures auth headers."""

    def do_GET(self):
        self.server.captured = {
            "path": self.path,
            "x_admin_control": self.headers.get("X-Admin-Control"),
            "origin": self.headers.get("Origin"),
            "host": self.headers.get("Host"),
        }
        status, body = self.server.respond
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def _fake_snapshot(port):
    return {"ok": True, "services": [{
        "key": "chat2api", "display_name": "UM", "port": port,
        "state": "Running", "healthy": True, "detail": "运行中",
        "process_ids": [1],
    }]}


class UmAuthStatusTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _StubStatusHandler)
        cls.addClassCleanup(cls.server.shutdown)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever,
                         daemon=True).start()
        cls.token_file = os.path.join(_TMP, "admin-control-token")
        with open(cls.token_file, "w", encoding="utf-8") as f:
            # Trailing newline on purpose: the adapter must strip it before
            # sending, matching chat2api's ensure_control_token().strip().
            f.write(_CONTROL_TOKEN + "\n")

    def setUp(self):
        token_patcher = mock.patch.object(
            console_um_adapter, "CONTROL_TOKEN_FILE", self.token_file)
        token_patcher.start()
        self.addCleanup(token_patcher.stop)
        snapshot_patcher = mock.patch.object(
            console_adapters, "service_status",
            return_value=_fake_snapshot(self.port))
        snapshot_patcher.start()
        self.addCleanup(snapshot_patcher.stop)
        self.server.respond = (
            200,
            json.dumps({"state": "valid", "label": "有效",
                        "error": None}).encode("utf-8"))
        self.server.captured = None

    def replace_snapshot(self, **kwargs):
        """Replace the snapshot mock (after stopping the setUp one)."""
        replacer = mock.patch.object(
            console_adapters, "service_status", **kwargs)
        replacer.start()
        self.addCleanup(replacer.stop)


class UmAuthStatusPassthroughTests(UmAuthStatusTestBase):
    def test_four_states_pass_through(self):
        for state, label, error in _FOUR_STATES:
            with self.subTest(state=state):
                self.server.respond = (
                    200,
                    json.dumps({"state": state, "label": label,
                                "error": error}).encode("utf-8"))
                result = console_um_adapter.um_auth_status("chat2api", {}, None)
                self.assertEqual(result["state"], state)
                self.assertEqual(result["label"], label)
                self.assertEqual(result["error"], error)
                self.assertFalse(result["verified"])
                self.assertTrue(result["state_known"])
                self.assertEqual(result["source"], "memory-state")
                self.assertIn("内存态", result["note"])
                self.assertIn("未向上游发起核验", result["note"])
                self.assertIsInstance(result["checked_epoch"], int)
                self.assertNotIn(_CONTROL_TOKEN,
                                 json.dumps(result, ensure_ascii=False))

    def test_manual_reason_and_login_window_pass_through(self):
        # Ticket #15：控制台状态读面必须让操作员看到
        # manual_takeover_required:<reason> 与本轮窗口截止时间；token 不出现。
        self.server.respond = (200, json.dumps({
            "state": "failed",
            "label": "认证失败（需人工：umpass_mfa_required）",
            "error": "manual_takeover_required:umpass_mfa_required",
            "login_window_deadline_epoch": 2800,
            "login_window_s": 1800,
        }).encode("utf-8"))
        result = console_um_adapter.um_auth_status("chat2api", {}, None)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(
            result["error"],
            "manual_takeover_required:umpass_mfa_required")
        self.assertEqual(result["login_window_deadline_epoch"], 2800.0)
        self.assertEqual(result["login_window_s"], 1800.0)
        self.assertNotIn(_CONTROL_TOKEN,
                         json.dumps(result, ensure_ascii=False))

    def test_malformed_login_window_fields_are_unknown_not_guessed(self):
        # Ticket #15：窗口字段缺失或形状不对就是未知（None），不折成 0。
        for deadline, window in ((None, None), (True, "1800"),
                                 ("2800", True)):
            with self.subTest(deadline=deadline, window=window):
                self.server.respond = (200, json.dumps({
                    "state": "valid",
                    "label": "有效",
                    "error": None,
                    "login_window_deadline_epoch": deadline,
                    "login_window_s": window,
                }).encode("utf-8"))
                result = console_um_adapter.um_auth_status(
                    "chat2api", {}, None)
                self.assertIsNone(result["login_window_deadline_epoch"])
                self.assertIsNone(result["login_window_s"])

    def test_non_finite_window_fields_are_unknown_and_serialize_as_json(self):
        # ticket #42 item 1：json.loads 接受裸 NaN/Infinity，但它们不是已知
        # 窗口值；读面必须拒绝，并保证自身状态 JSON 合法。
        for deadline, window in ((float("nan"), float("inf")),
                                  (float("inf"), float("nan")),
                                  (float("-inf"), float("inf"))):
            with self.subTest(deadline=deadline, window=window):
                self.server.respond = (200, json.dumps({
                    "state": "valid",
                    "label": "有效",
                    "error": None,
                    "login_window_deadline_epoch": deadline,
                    "login_window_s": window,
                    "recovery": {
                        "state": "waiting", "round": 1,
                        "session_starts": 1, "max_sessions": 6,
                        "next_attempt_epoch": float("nan"),
                        "pause_reason": None,
                    },
                }).encode("utf-8"))
                result = console_um_adapter.um_auth_status(
                    "chat2api", {}, None)
                self.assertIsNone(result["login_window_deadline_epoch"])
                self.assertIsNone(result["login_window_s"])
                self.assertIsNone(result["recovery"]["next_attempt_epoch"])
                json.dumps(result, allow_nan=False)

    def test_primary_cleanup_outcome_passes_through(self):
        # ticket #42 item 16：清理失败不能吞掉原始会话结果，读面必须保留。
        self.server.respond = (200, json.dumps({
            "state": "failed",
            "label": "认证失败（清理失败）",
            "error": "browser_cleanup_failed",
            "primary": "manual_takeover_required:umpass_mfa_required",
        }).encode("utf-8"))
        result = console_um_adapter.um_auth_status("chat2api", {}, None)
        self.assertEqual(result["error"], "browser_cleanup_failed")
        self.assertEqual(result["primary"],
                         "manual_takeover_required:umpass_mfa_required")

    def test_login_progress_fields_pass_through(self):
        # ticket #74：读面透传当前登录轮次的 phase/origin/manual_reason，
        # 让控制台显示 Duo 推送等待并提供可见窗口动作。
        self.server.respond = (200, json.dumps({
            "state": "logging_in",
            "label": "登录中（需人工：duo_prompt_waiting）",
            "error": None,
            "login": {
                "phase": "awaiting_duo",
                "origin": "https://api-1234.duosecurity.com/prompt/xyz",
                "manual_reason": "duo_prompt_waiting",
                "detail": "not whitelisted",
            },
        }).encode("utf-8"))
        result = console_um_adapter.um_auth_status("chat2api", {}, None)
        self.assertEqual(result["login"], {
            "phase": "awaiting_duo",
            "origin": "https://api-1234.duosecurity.com/prompt/xyz",
            "manual_reason": "duo_prompt_waiting",
        })
        self.assertNotIn("detail", result["login"])

    def test_malformed_login_progress_is_unknown_not_guessed(self):
        for login in (None, "duo_prompt_waiting", [], {"phase": 1}):
            with self.subTest(login=login):
                self.server.respond = (200, json.dumps({
                    "state": "logging_in",
                    "label": "登录中",
                    "error": None,
                    "login": login,
                }).encode("utf-8"))
                result = console_um_adapter.um_auth_status(
                    "chat2api", {}, None)
                self.assertIsNone(result["login"])

    def test_stub_receives_control_header_and_no_origin(self):
        console_um_adapter.um_auth_status("chat2api", {}, None)
        captured = self.server.captured
        self.assertIsNotNone(captured)
        self.assertEqual(captured["path"], "/admin/auth/status")
        self.assertEqual(captured["x_admin_control"], _CONTROL_TOKEN)
        self.assertIsNone(captured["origin"])

    def test_result_never_contains_token(self):
        result = console_um_adapter.um_auth_status("chat2api", {}, None)
        self.assertNotIn(_CONTROL_TOKEN, json.dumps(result, ensure_ascii=False))


class UmAuthStatusFailureTests(UmAuthStatusTestBase):
    def test_token_mismatch_maps_to_control_denied(self):
        self.server.respond = (403, json.dumps(
            {"error": {"message": "Local management token missing or "
                                  "invalid"}}).encode("utf-8"))
        with self.assertRaises(OperationError) as ctx:
            console_um_adapter.um_auth_status("chat2api", {}, None)
        self.assertEqual(ctx.exception.code, "control_denied")

    def test_unreachable_service_is_unknown_not_invalid(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
        sock.close()
        dead_snapshot = {"ok": True, "services": [{
            "key": "chat2api", "display_name": "UM", "port": dead_port,
            "state": "Stopped", "healthy": False, "detail": "已停止",
            "process_ids": [],
        }]}
        self.replace_snapshot(return_value=dead_snapshot)
        with self.assertRaises(OperationError) as ctx:
            console_um_adapter.um_auth_status("chat2api", {}, None)
        self.assertEqual(ctx.exception.code, "status_unreachable")
        self.assertIn("未知 ≠ 失效", ctx.exception.message)

    def test_non_json_body_maps_to_status_schema(self):
        self.server.respond = (200, b"this is not json")
        with self.assertRaises(OperationError) as ctx:
            console_um_adapter.um_auth_status("chat2api", {}, None)
        self.assertEqual(ctx.exception.code, "status_schema")

    def test_missing_state_maps_to_status_schema(self):
        self.server.respond = (
            200, json.dumps({"label": "x"}).encode("utf-8"))
        with self.assertRaises(OperationError) as ctx:
            console_um_adapter.um_auth_status("chat2api", {}, None)
        self.assertEqual(ctx.exception.code, "status_schema")

    def test_snapshot_failure_maps_to_status_unavailable(self):
        self.replace_snapshot(side_effect=RuntimeError("wrapper exploded"))
        with self.assertRaises(OperationError) as ctx:
            console_um_adapter.um_auth_status("chat2api", {}, None)
        self.assertEqual(ctx.exception.code, "status_unavailable")
        self.assertIn("chat2api", ctx.exception.message)


class ControlTokenReadTests(unittest.TestCase):
    """Regression tests for the three-way token-read classification.

    Exercises ``_read_control_token`` directly via the module-constant patch
    (same mechanism as the adapter tests); no HTTP server and no real token
    file is touched. Contract under test: absent != unreadable != empty, and
    no error message ever echoes the token value or file content back.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="console-um-token-cls-")
        self.addCleanup(tmp.cleanup)
        self._dir = tmp.name

    def _use_token_path(self, path):
        patcher = mock.patch.object(
            console_um_adapter, "CONTROL_TOKEN_FILE", path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _read_error(self):
        with self.assertRaises(OperationError) as ctx:
            console_um_adapter._read_control_token()
        return ctx.exception

    def test_missing_file_maps_to_absent(self):
        self._use_token_path(
            os.path.join(self._dir, "no-such-token-file"))
        exc = self._read_error()
        self.assertEqual(exc.code, "control_token_absent")
        self.assertEqual(
            exc.message, "管理控制令牌文件缺席，无法查询 UM 认证状态")

    def test_directory_path_maps_to_unreadable_not_absent(self):
        dir_path = os.path.join(self._dir, "token-is-a-directory")
        os.mkdir(dir_path)
        self._use_token_path(dir_path)
        exc = self._read_error()
        self.assertEqual(exc.code, "control_token_unreadable")
        # A directory is not a missing file: the message names the OS
        # exception class observed on this platform (PermissionError on
        # Windows, IsADirectoryError on POSIX) and marks state as unknown.
        try:
            open(dir_path, encoding="utf-8")
        except OSError as probe:
            self.assertIn(type(probe).__name__, exc.message)
        self.assertIn("状态未知", exc.message)
        self.assertNotIn(_CONTROL_TOKEN, exc.message)

    def test_blank_token_file_maps_to_empty(self):
        path = os.path.join(self._dir, "admin-control-token")
        for raw in ("", "   ", "\n \t \n"):
            with self.subTest(raw=repr(raw)):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(raw)
                self._use_token_path(path)
                exc = self._read_error()
                self.assertEqual(exc.code, "control_token_empty")
                self.assertEqual(
                    exc.message,
                    "管理控制令牌文件为空，无法查询 UM 认证状态")

    def test_decode_failure_message_hides_token_content(self):
        path = os.path.join(self._dir, "admin-control-token")
        with open(path, "wb") as f:
            # Invalid UTF-8 lead byte forces UnicodeDecodeError even though
            # the raw content contains the sentinel token: the unreadable
            # message must not echo any of that content back.
            f.write(b"\xff" + _CONTROL_TOKEN.encode("utf-8"))
        self._use_token_path(path)
        exc = self._read_error()
        self.assertEqual(exc.code, "control_token_unreadable")
        self.assertIn("UnicodeDecodeError", exc.message)
        self.assertNotIn(_CONTROL_TOKEN, exc.message)


class VerifyAdapterTests(UmAuthStatusTestBase):
    """Consumption-adapter tests for the restricted /admin/auth/verify path.

    The production chat2api does not ship this endpoint yet; these tests pin
    the agreed response contract against the stub so the console side is
    ready before the endpoint lands.
    """

    def _respond_verify(self, payload):
        self.server.respond = (
            200, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def test_valid_passes_through(self):
        self._respond_verify({
            "verify": "valid", "reason": "目录结构校验通过",
            "upstream_status": 200, "checked_epoch": 1726000000,
            "note": "endpoint note"})
        result = console_um_adapter.um_verify_account("chat2api", {}, None)
        self.assertEqual(result["verify"], "valid")
        self.assertEqual(result["reason"], "目录结构校验通过")
        self.assertEqual(result["upstream_status"], 200)
        self.assertEqual(result["checked_epoch"], 1726000000)
        self.assertEqual(result["note"], console_um_adapter._VERIFY_NOTE)

    def test_rejected_401_passes_through_without_token(self):
        self._respond_verify({
            "verify": "rejected", "reason": "upstream_401",
            "upstream_status": 401, "checked_epoch": 1726000001,
            "note": "endpoint note"})
        result = console_um_adapter.um_verify_account("chat2api", {}, None)
        self.assertEqual(result["verify"], "rejected")
        self.assertEqual(result["reason"], "upstream_401")
        self.assertEqual(result["upstream_status"], 401)
        self.assertNotIn(_CONTROL_TOKEN,
                         json.dumps(result, ensure_ascii=False))

    def test_needs_login_passes_through(self):
        self._respond_verify({
            "verify": "needs_login", "reason": None,
            "upstream_status": None, "checked_epoch": 1726000002,
            "note": "endpoint note"})
        result = console_um_adapter.um_verify_account("chat2api", {}, None)
        self.assertEqual(result["verify"], "needs_login")
        self.assertIsNone(result["reason"])
        self.assertIsNone(result["upstream_status"])
        self.assertEqual(result["checked_epoch"], 1726000002)

    def test_unknown_passes_through(self):
        self._respond_verify({
            "verify": "unknown", "reason": "upstream_5xx",
            "upstream_status": 503, "checked_epoch": 1726000003,
            "note": "endpoint note"})
        result = console_um_adapter.um_verify_account("chat2api", {}, None)
        self.assertEqual(result["verify"], "unknown")
        self.assertEqual(result["reason"], "upstream_5xx")
        self.assertEqual(result["upstream_status"], 503)

    def test_missing_checked_epoch_falls_back_to_local_clock(self):
        before = int(time.time())
        self._respond_verify({
            "verify": "valid", "reason": "目录结构校验通过",
            "upstream_status": 200, "note": "endpoint note"})
        result = console_um_adapter.um_verify_account("chat2api", {}, None)
        self.assertIsInstance(result["checked_epoch"], int)
        self.assertGreaterEqual(result["checked_epoch"], before)
        self.assertIn("端点未带时间", result["note"])
        self.assertTrue(result["note"].startswith(
            console_um_adapter._VERIFY_NOTE))

    def test_malformed_payloads_map_to_verify_schema(self):
        bad_payloads = [
            {"verify": "weird", "reason": None, "upstream_status": None,
             "checked_epoch": 1, "note": "endpoint note"},
            {"reason": "missing-verify", "upstream_status": None,
             "checked_epoch": 1},
            ["not", "a", "dict"],
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                self._respond_verify(payload)
                with self.assertRaises(OperationError) as ctx:
                    console_um_adapter.um_verify_account(
                        "chat2api", {}, None)
                self.assertEqual(ctx.exception.code, "verify_schema")
                self.assertEqual(
                    ctx.exception.message, "受限核验响应形状与预期不符")
        # A 200 body that is not JSON at all is equally "not a dict".
        self.server.respond = (200, b"<html>not json</html>")
        with self.assertRaises(OperationError) as ctx:
            console_um_adapter.um_verify_account("chat2api", {}, None)
        self.assertEqual(ctx.exception.code, "verify_schema")

    def test_control_rejection_maps_to_control_denied(self):
        for code in (401, 403):
            with self.subTest(code=code):
                self.server.respond = (
                    code, json.dumps({"error": "denied"}).encode("utf-8"))
                with self.assertRaises(OperationError) as ctx:
                    console_um_adapter.um_verify_account(
                        "chat2api", {}, None)
                self.assertEqual(ctx.exception.code, "control_denied")

    def test_other_non_200_maps_to_verify_query_failed(self):
        for code in (500, 502):
            with self.subTest(code=code):
                self.server.respond = (
                    code, json.dumps({"error": "boom"}).encode("utf-8"))
                with self.assertRaises(OperationError) as ctx:
                    console_um_adapter.um_verify_account(
                        "chat2api", {}, None)
                self.assertEqual(ctx.exception.code, "verify_query_failed")
                self.assertIn("HTTP %d" % code, ctx.exception.message)
                self.assertIn("未知 ≠ 失效", ctx.exception.message)

    def test_unreachable_service_maps_to_status_unreachable(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
        sock.close()
        dead_snapshot = {"ok": True, "services": [{
            "key": "chat2api", "display_name": "UM", "port": dead_port,
            "state": "Stopped", "healthy": False, "detail": "已停止",
            "process_ids": [],
        }]}
        self.replace_snapshot(return_value=dead_snapshot)
        with self.assertRaises(OperationError) as ctx:
            console_um_adapter.um_verify_account("chat2api", {}, None)
        self.assertEqual(ctx.exception.code, "status_unreachable")
        self.assertIn("未知 ≠ 失效", ctx.exception.message)

    def test_stub_receives_control_header_and_no_origin(self):
        self._respond_verify({
            "verify": "valid", "reason": None, "upstream_status": 200,
            "checked_epoch": 1726000004, "note": "endpoint note"})
        console_um_adapter.um_verify_account("chat2api", {}, None)
        captured = self.server.captured
        self.assertIsNotNone(captured)
        self.assertEqual(captured["path"], "/admin/auth/verify")
        self.assertEqual(captured["x_admin_control"], _CONTROL_TOKEN)
        self.assertIsNone(captured["origin"])


if __name__ == "__main__":
    unittest.main()
