# -*- coding: utf-8 -*-
"""#81: 本机 8000 Rust/Python parity 探针的离线测试。

只启动两个 loopback 假服务并读取临时 token 文件；不启动真实 8000、不打开
浏览器、不访问 UM。测试验证状态码、响应体形状、错误信封与 token 边界。
"""

import contextlib
import copy
import io
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import local_8000_parity as parity


def _json(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


MATCHING_RESPONSES = {
    ("GET", "/health"): (
        200,
        "application/json",
        _json({
            "status": "ok",
            "chat2api_version": "1.0.0",
            "chat2api_build": "build-a",
            "backend": "https://chat.genai.um.edu.mo",
            "models": 2,
            "authentication": {
                "state": "valid",
                "label": "认证有效",
                "error": None,
                "primary": None,
            },
            "watch": {
                "thread_alive": True,
                "last_round_epoch": 1.0,
                "last_round_outcome": "valid",
                "next_round_due": 2.0,
                "rounds": 1,
            },
            "recovery": {
                "state": "idle",
                "session_starts": 1,
                "max_sessions": 6,
                "next_attempt_epoch": None,
            },
            "login_window_deadline_epoch": 3.0,
            "login_window_s": 180.0,
            "upstream": {"name": "fake", "version": "1.0"},
            "upstream_reachability": {"state": "ok"},
            "docs": "POST /v1/chat/completions",
        }),
    ),
    ("GET", "/v1/models"): (
        200,
        "application/json",
        _json({"data": [{"id": "alpha"}, {"id": "beta"}]}),
    ),
    ("GET", "/api/config"): (
        200,
        "application/json",
        _json({"status": True, "name": "fake", "version": "0.12.3"}),
    ),
    ("GET", "/api/v1/auths/"): (
        200,
        "application/json",
        _json({"id": "user-1", "name": "User", "role": "user"}),
    ),
    ("POST", "/api/echo"): (
        405,
        "application/json",
        _json({
            "error": {
                "message": "Upstream request failed",
                "type": "invalid_request_error",
                "code": "upstream_request_rejected",
                "param": None,
                "detail": {
                    "upstream_status": 405,
                    "upstream_message": "Method Not Allowed",
                },
            },
        }),
    ),
    ("GET", "/admin/auth/status"): (
        200,
        "application/json",
        _json({
            "state": "valid",
            "label": "认证有效",
            "error": None,
            "primary": None,
            "evidence": {
                "verdict": "valid",
                "reason": None,
                "upstream_status": 200,
                "checked_epoch": 1.0,
            },
            "login": None,
            "gate": None,
            "watch": {
                "thread_alive": True,
                "last_round_epoch": 1.0,
                "last_round_outcome": "valid",
                "next_round_due": 2.0,
                "rounds": 1,
            },
            "recovery": {
                "state": "idle",
                "session_starts": 1,
                "max_sessions": 6,
                "next_attempt_epoch": None,
            },
            "login_window_deadline_epoch": None,
            "login_window_s": 180.0,
        }),
    ),
    ("GET", "/admin/auth/verify"): (
        200,
        "application/json",
        _json({
            "verify": "valid",
            "reason": None,
            "upstream_status": 200,
            "checked_epoch": 1.0,
            "note": "single-shot upstream /api/models probe; auth state and model cache untouched",
        }),
    ),
}


class _FakeService:
    def __init__(self, responses):
        self.responses = responses
        self.seen = []
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                self._serve("POST", body)

            def _serve(self, method, body=b""):
                service.seen.append({
                    "method": method,
                    "path": self.path,
                    "headers": {
                        key.lower(): value
                        for key, value in self.headers.items()
                    },
                    "body": body,
                })
                status, content_type, payload = service.responses.get(
                    (method, self.path),
                    (404, "application/json", _json({
                        "error": {
                            "message": "missing",
                            "type": "not_found_error",
                            "code": "not_found",
                            "param": None,
                            "detail": None,
                        },
                    })),
                )
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class Local8000ParityTests(unittest.TestCase):
    def setUp(self):
        self.services = []
        self.token = "test-control-token-0123456789"

    def tearDown(self):
        for service in self.services:
            service.close()

    def _service(self, responses=None):
        service = _FakeService(
            copy.deepcopy(responses or MATCHING_RESPONSES))
        self.services.append(service)
        return service

    def _evidence(self, python, rust):
        return parity.build_evidence(
            python.url, rust.url, self.token, timeout=5.0)

    def test_identical_services_report_parity(self):
        python = self._service()
        rust = self._service()

        document = self._evidence(python, rust)

        self.assertEqual("passed", document["parity"])
        self.assertEqual([], document["differences"])
        self.assertEqual([], document["probeErrors"])
        names = {item["name"] for item in document["rust"]["surfaces"]}
        self.assertEqual(
            {"health", "models", "api_config", "api_auths",
             "api_echo_post", "admin_auth_status", "admin_auth_verify"},
            names,
        )

    def test_matching_expired_token_503_is_parity(self):
        responses = copy.deepcopy(MATCHING_RESPONSES)
        expired = _json({
            "error": {
                "message": "需要交互登录",
                "type": "authentication_error",
                "code": "reauthentication_required",
                "param": None,
                "detail": {"reason": "upstream_rejected"},
            },
        })
        for key in (("GET", "/v1/models"), ("GET", "/api/config"),
                    ("GET", "/api/v1/auths/"), ("POST", "/api/echo"),
                    ("GET", "/admin/auth/status"),
                    ("GET", "/admin/auth/verify")):
            responses[key] = (503, "application/json", expired)
        python = self._service(responses)
        rust = self._service(responses)

        document = self._evidence(python, rust)

        self.assertEqual("passed", document["parity"])
        self.assertEqual([], document["differences"])

    def test_status_mismatch_is_reported(self):
        python = self._service()
        responses = copy.deepcopy(MATCHING_RESPONSES)
        responses[("GET", "/v1/models")] = (
            503,
            "application/json",
            _json({
                "error": {
                    "message": "需要交互登录",
                    "type": "authentication_error",
                    "code": "reauthentication_required",
                    "param": None,
                    "detail": {"reason": "upstream_rejected"},
                },
            }),
        )
        rust = self._service(responses)

        document = self._evidence(python, rust)

        self.assertEqual("failed", document["parity"])
        fields = {item["field"] for item in document["differences"]}
        self.assertIn("status", fields)

    def test_shape_mismatch_is_reported(self):
        python = self._service()
        responses = copy.deepcopy(MATCHING_RESPONSES)
        responses[("GET", "/api/config")] = (
            200,
            "application/json",
            _json({"status": True, "name": "fake", "version": "0.12.3",
                   "extra": 1}),
        )
        rust = self._service(responses)

        document = self._evidence(python, rust)

        self.assertEqual("failed", document["parity"])
        fields = {item["field"] for item in document["differences"]}
        self.assertIn("shape", fields)

    def test_model_catalog_ids_are_compared(self):
        python = self._service()
        responses = copy.deepcopy(MATCHING_RESPONSES)
        responses[("GET", "/v1/models")] = (
            200,
            "application/json",
            _json({"data": [{"id": "alpha"}]}),
        )
        rust = self._service(responses)

        document = self._evidence(python, rust)

        self.assertEqual("failed", document["parity"])
        fields = {item["field"] for item in document["differences"]}
        self.assertIn("semantic", fields)

    def test_control_token_is_only_sent_to_controlled_surfaces(self):
        python = self._service()
        rust = self._service()

        self._evidence(python, rust)

        controlled = {"/api/config", "/api/v1/auths/", "/api/echo",
                      "/admin/auth/status", "/admin/auth/verify"}
        for request in python.seen + rust.seen:
            headers = request["headers"]
            self.assertNotIn("origin", headers)
            if request["path"] in controlled:
                self.assertEqual(
                    self.token, headers.get("x-admin-control"),
                    request["path"],
                )
            else:
                self.assertNotIn("x-admin-control", headers, request["path"])

    def test_evidence_never_contains_the_control_token(self):
        python = self._service()
        rust = self._service()

        document = self._evidence(python, rust)
        serialized = json.dumps(document, ensure_ascii=False)

        self.assertNotIn(self.token, serialized)
        self.assertNotIn("X-Admin-Control", serialized)

    def test_cli_writes_evidence_and_returns_zero_on_parity(self):
        python = self._service()
        rust = self._service()
        with tempfile.TemporaryDirectory(prefix="local-8000-parity-") as tmp:
            token_file = Path(tmp) / "admin-control-token"
            token_file.write_text(self.token, encoding="utf-8")
            evidence = Path(tmp) / "evidence.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = parity.main([
                    "--python-url", python.url,
                    "--rust-url", rust.url,
                    "--control-token-file", str(token_file),
                    "--timeout", "5",
                    "--evidence", str(evidence),
                ])
            document = json.loads(stdout.getvalue())
            written = json.loads(evidence.read_text(encoding="utf-8"))

        self.assertEqual(0, code)
        self.assertEqual("passed", document["parity"])
        self.assertEqual(document, written)

    def test_cli_returns_one_on_a_parity_difference(self):
        python = self._service()
        responses = copy.deepcopy(MATCHING_RESPONSES)
        responses[("GET", "/v1/models")] = (
            200,
            "application/json",
            _json({"data": [{"id": "different"}]}),
        )
        rust = self._service(responses)
        with tempfile.TemporaryDirectory(prefix="local-8000-parity-") as tmp:
            token_file = Path(tmp) / "admin-control-token"
            token_file.write_text(self.token, encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = parity.main([
                    "--python-url", python.url,
                    "--rust-url", rust.url,
                    "--control-token-file", str(token_file),
                    "--timeout", "5",
                ])
            document = json.loads(stdout.getvalue())

        self.assertEqual(1, code)
        self.assertEqual("failed", document["parity"])

    def test_cli_returns_two_when_a_side_is_unreachable(self):
        python = self._service()
        with tempfile.TemporaryDirectory(prefix="local-8000-parity-") as tmp:
            token_file = Path(tmp) / "admin-control-token"
            token_file.write_text(self.token, encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = parity.main([
                    "--python-url", python.url,
                    "--rust-url", "http://127.0.0.1:1",
                    "--control-token-file", str(token_file),
                    "--timeout", "0.5",
                ])
            document = json.loads(stdout.getvalue())

        self.assertEqual(2, code)
        self.assertEqual("failed", document["parity"])
        self.assertTrue(document["probeErrors"])


if __name__ == "__main__":
    unittest.main()
