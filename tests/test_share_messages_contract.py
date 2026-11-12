# -*- coding: utf-8 -*-
"""M6-A：Anthropic Messages 分享入口的协议与鉴权契约补充用例。

既有 tests/test_share_gateway.py 已钉死的契约不在此重复（x-api-key 仅
Messages、Bearer 同值共存、冲突/重复头 fail-closed、anthropic-version/beta
窄转发、流式/非流式文本与工具 relay、图片 1 MiB 声明拒绝、每 Key 并发、
撤销 401、Files/Batches/count_tokens 404）。本文件补齐核对时发现缺失的
四块隔离证据，全部穿过真实 ShareServer/ShareHandler（本地真实 socket），
替身只落在最末端上游（一个假 chat2api 8110），零真实推理、零生产路径：

* 客户端伪造的 X-Share-* 归因头与敏感头绝不透传；上游收到的归因标记只能
  是分享自建的那一份（key_id 取自已认证条目，签名可被共享密钥重算验证）。
* thinking 字段在 Messages 白名单内：请求侧原样上行，思考流（thinking_delta
  / signature_delta）逐字节回传。
* 撤销并终止（R8-A7）穿真实 Handler 的端到端：终止只关目标 Key 的在途
  Messages 流，计数由该 handler 自己的 finally 单源减一，另一 Key 的在途
  连接与计数原样，上游不被重复调用。
* 1 MiB 上限的边界值（恰好 1048576 字节）被接受（<= 语义）。

隔离边界：config/临时目录由 tempfile 创建；上游是进程内假服务器；不触碰
8000/8096/8100/8110/8320，不写生产密钥、账本或 chat2api.log，不发真实
模型推理或计数请求。
"""

import base64
import hashlib as _hashlib
import hmac
import http.client
import importlib.util
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 让 share_broker_protocol（延迟导入于 load_config）在任意运行方式下可达，
# 不依赖同进程其他测试文件的 sys.path 副作用。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_SPEC = importlib.util.spec_from_file_location(
    "share_gateway_messages_contract",
    Path(__file__).resolve().parents[1] / "chat2api" / "share_gateway.py")
share = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(share)

SECRET_A = "share-secret-A"
SECRET_B = "share-secret-B"
ATTRIBUTION_SECRET = "a" * 48   # >= ATTRIBUTION_KEY_MIN_BYTES

ANTHROPIC_MESSAGE = (
    b'{"id":"msg_1","type":"message","role":"assistant",'
    b'"model":"GLM-5.2-NVFP4","content":[{"type":"text","text":"hello"}],'
    b'"stop_reason":"end_turn","stop_sequence":null,'
    b'"usage":{"input_tokens":7,"output_tokens":2}}'
)

# 思考（thinking）流形状：thinking 块的 thinking_delta/signature_delta 是
# Anthropic 扩展思考的专属事件，relay 必须逐字节透传，不得改写或丢弃。
THINKING_SSE = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"id":"msg_1","type":"message",'
    b'"role":"assistant","model":"GLM-5.2-NVFP4","content":[],"stop_reason":null,'
    b'"stop_sequence":null,"usage":{"input_tokens":9,"output_tokens":0}}}\n\n'
    b'event: content_block_start\n'
    b'data: {"type":"content_block_start","index":0,'
    b'"content_block":{"type":"thinking","thinking":""}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","index":0,'
    b'"delta":{"type":"thinking_delta","thinking":"\xe8\x80\x83\xe8\x99\x91"}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","index":0,'
    b'"delta":{"type":"signature_delta","signature":"sig=="}}\n\n'
    b'event: content_block_stop\n'
    b'data: {"type":"content_block_stop","index":0}\n\n'
    b'event: content_block_start\n'
    b'data: {"type":"content_block_start","index":1,'
    b'"content_block":{"type":"text","text":""}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","index":1,'
    b'"delta":{"type":"text_delta","text":"answer"}}\n\n'
    b'event: content_block_stop\n'
    b'data: {"type":"content_block_stop","index":1}\n\n'
    b'event: message_delta\n'
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn",'
    b'"stop_sequence":null},"usage":{"output_tokens":5}}\n\n'
    b'event: message_stop\n'
    b'data: {"type":"message_stop"}\n\n'
)
# 在途挂起用例：上游先发首个事件再等释放信号，之后补完剩余思考流。
THINKING_SSE_TAIL = THINKING_SSE.split(b"event: content_block_start", 1)[1]
THINKING_SSE_TAIL = b"event: content_block_start" + THINKING_SSE_TAIL


def _digest(secret):
    return _hashlib.sha256(secret.encode()).hexdigest()


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


class _FakeUpstream(BaseHTTPRequestHandler):
    """最末端替身：只应答本文件涉及的 Messages 形状，记录收到的头与 body。"""

    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.rfile.read(0)
        body = json.dumps({"object": "list", "data": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.captured.append((self.path, dict(self.headers), raw))
        failure_status = self.server.failure_status[0]
        if failure_status is not None:
            # A8 upstream failure shape: an auth body (with a canary
            # credential) plus an optional retry hint. The share must relay
            # only the validated hint, never the body.
            payload = json.dumps({"error": {
                "message": "authentication requires sign-in; "
                           "token=canary-secret",
                "type": "reauthentication_required"}}).encode()
            self.send_response(failure_status)
            self.send_header("Content-Type", "application/json")
            if self.server.retry_after[0] is not None:
                self.send_header("Retry-After", self.server.retry_after[0])
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path != "/v1/messages":
            body = b'{"error":"unexpected path"}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        streamed = bool(json.loads(raw).get("stream"))
        if streamed:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            first = THINKING_SSE.split(b"event: content_block_start", 1)[0]
            try:
                self.wfile.write(first)
                self.wfile.flush()
            except OSError:
                return
            if b"hold-please" in raw:
                self.server.hold_gate.wait(10)
            try:
                self.wfile.write(THINKING_SSE_TAIL)
                self.wfile.flush()
            except OSError:
                pass
            return
        payload = ANTHROPIC_MESSAGE
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class MessagesContractTests(unittest.TestCase):
    """全部经真实 ShareServer/ShareHandler 与本地 socket，替身落末端上游。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.config = Path(self.temp.name) / "um-share-projection.json"
        self.config.write_text(json.dumps({"clients": {
            "friendA": _digest(SECRET_A), "friendB": _digest(SECRET_B)}}),
            encoding="utf-8")
        self.upstream_requests = []
        captured = self.upstream_requests
        hold_gate = threading.Event()

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
        upstream.hold_gate = hold_gate
        upstream.captured = captured
        upstream.failure_status = [None]
        upstream.retry_after = [None]
        upstream.handle_error = lambda *_args: None
        self.upstream = upstream
        self.hold_gate = hold_gate
        self.server = share.ShareServer(
            ("127.0.0.1", 0), self.config, self.upstream.server_port,
            attribution_secret=ATTRIBUTION_SECRET)
        # 终止用例会真实关闭在途 socket，属预期；抑制 handler 线程的
        # traceback 噪音（不改变任何断言路径）。
        self.server.handle_error = lambda *_args: None
        self.threads = []
        for srv in (self.upstream, self.server):
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)
        self.addCleanup(self._teardown_servers)

    def _teardown_servers(self):
        self.hold_gate.set()
        for srv in (self.server, self.upstream):
            srv.shutdown()
            srv.server_close()
        for thread in self.threads:
            thread.join(5)

    # ------------------------------------------------------------- helpers

    def request(self, method="POST", path="/v1/messages", key=SECRET_A,
                body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1",
                                          self.server.server_port, timeout=10)
        request_headers = {"Content-Type": "application/json"}
        if key is not None:
            request_headers["Authorization"] = "Bearer " + key
        request_headers.update(headers or {})
        try:
            conn.request(method, path, body=body, headers=request_headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def request_raw_headers(self, method, path, header_pairs, body=b""):
        conn = http.client.HTTPConnection("127.0.0.1",
                                          self.server.server_port, timeout=10)
        try:
            conn.putrequest(method, path)
            for name, value in header_pairs:
                conn.putheader(name, value)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body if body else None)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    @staticmethod
    def messages_body(**fields):
        """Exact Messages body：不加不减任何字段（含缺 max_tokens 的形状）。"""
        return json.dumps(fields).encode()

    # ------------------------------------------- 伪造归因头 / 敏感头不透传

    def test_forged_attribution_and_sensitive_headers_never_forwarded(self):
        # 客户端伪造的全部 X-Share-* 归因头必须被白名单重建丢弃；上游收到的
        # 归因标记只能是分享在认证之后自建的一份：key_id 是认证条目、request
        # _id 是 32 hex、签名可用共享密钥重算复现（伪造签名不可能复现）。
        body = self.messages_body(
            model="GLM-5.2-NVFP4", max_tokens=8,
            messages=[{"role": "user", "content": "hi"}])
        forged = {
            "X-Share-Attribution-Version": "9",
            "X-Share-Key-Id": base64.urlsafe_b64encode(
                b"attacker").decode().rstrip("="),
            "X-Share-Request-Id": "f" * 32,
            "X-Share-Ts": "1234567890",
            "X-Share-Signature": "0" * 64,
            "Cookie": "private=1",
            "X-Forwarded-Host": "evil.example",
        }
        status, _headers, payload = self.request(
            "POST", "/v1/messages", body=body, headers=forged)
        self.assertEqual(status, 200)
        self.assertEqual(payload, ANTHROPIC_MESSAGE)
        sent = self.upstream_requests[0][1]
        for name in ("Authorization", "Cookie", "X-Forwarded-Host",
                     "X-Api-Key"):
            self.assertNotIn(name, sent)
        self.assertEqual(sent.get("X-Share-Attribution-Version"), "1")
        expected_key_id = base64.urlsafe_b64encode(
            b"friendA").decode().rstrip("=")
        self.assertEqual(sent.get("X-Share-Key-Id"), expected_key_id)
        request_id = sent.get("X-Share-Request-Id")
        self.assertRegex(request_id, r"^[0-9a-f]{32}$")
        self.assertNotEqual(request_id, "f" * 32)
        ts = sent.get("X-Share-Ts")
        self.assertRegex(ts, r"^[0-9]+$")
        signed = "\n".join(("1", expected_key_id, request_id, ts))
        signature = hmac.new(ATTRIBUTION_SECRET.encode(), signed.encode(),
                             _hashlib.sha256).hexdigest()
        self.assertEqual(sent.get("X-Share-Signature"), signature)

    def test_messages_wire_headers_fail_closed_for_missing_prefix(self):
        # 「Authorization: test-secret」（缺 Bearer 前缀）是畸形形状：与空值、
        # 重复头一样 fail-closed，绝不把裸值当凭据。
        body = self.messages_body(
            model="GLM-5.2-NVFP4", max_tokens=8,
            messages=[{"role": "user", "content": "hi"}])
        status, payload = self.request_raw_headers(
            "POST", "/v1/messages",
            [("Authorization", "test-secret"),
             ("X-Api-Key", "test-secret")], body=body)
        self.assertEqual(status, 401)
        self.assertIn(b"invalid_api_key", payload)
        self.assertEqual(self.upstream_requests, [])

    # --------------------------------------------- thinking 字段与思考流

    def test_thinking_field_and_stream_relayed_verbatim(self):
        # thinking 在 Messages 白名单内：请求侧逐字段上行；响应侧
        # thinking_delta/signature_delta 事件逐字节透传，不被改写。
        body = self.messages_body(
            model="GLM-5.2-NVFP4", max_tokens=64, stream=True,
            thinking={"type": "enabled", "budget_tokens": 1024},
            messages=[{"role": "user", "content": "hi"}])
        status, headers, payload = self.request(
            "POST", "/v1/messages", body=body)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "text/event-stream")
        self.assertEqual(payload, THINKING_SSE)
        _path, sent_headers, raw = self.upstream_requests[0]
        self.assertEqual(sent_headers.get("Content-Type"), "application/json")
        forwarded = json.loads(raw)
        self.assertEqual(forwarded["thinking"],
                         {"type": "enabled", "budget_tokens": 1024})
        self.assertEqual(forwarded["max_tokens"], 64)

    def test_thinking_non_stream_relays_field(self):
        body = self.messages_body(
            model="GLM-5.2-NVFP4", max_tokens=64,
            thinking={"type": "enabled", "budget_tokens": 2048},
            messages=[{"role": "user", "content": "hi"}])
        status, headers, payload = self.request(
            "POST", "/v1/messages", body=body)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(payload, ANTHROPIC_MESSAGE)
        forwarded = json.loads(self.upstream_requests[0][2])
        self.assertEqual(forwarded["thinking"],
                         {"type": "enabled", "budget_tokens": 2048})

    # ------------------------------------ A8 Retry-After 与稳定错误包装

    def test_upstream_failure_keeps_wrapper_and_relays_bounded_retry_after(self):
        # A8: the share keeps the frozen share_gateway_error / um_unavailable
        # wrapper and relays only a validated integer Retry-After; the upstream
        # auth body (and the canary credential inside it) is never echoed.
        self.upstream.failure_status[0] = 503
        self.upstream.retry_after[0] = "17"
        body = self.messages_body(
            model="GLM-5.2-NVFP4", max_tokens=8,
            messages=[{"role": "user", "content": "hi"}])
        status, headers, payload = self.request(
            "POST", "/v1/messages", body=body)
        self.assertEqual(status, 503)
        self.assertEqual(headers.get("Retry-After"), "17")
        error = json.loads(payload)["error"]
        self.assertEqual(error["type"], "share_gateway_error")
        self.assertEqual(error["code"], "um_unavailable")
        self.assertEqual(error["upstream_status"], 503)
        self.assertNotIn(b"canary-secret", payload)
        self.assertNotIn(b"reauthentication_required", payload)
        self.assertNotIn(b"authentication requires sign-in", payload)

    def test_invalid_upstream_retry_after_is_dropped(self):
        # Zero, negative, fractional, HTTP-date, oversized and non-numeric
        # values are dropped; the share never invents a retry time.
        self.upstream.failure_status[0] = 503
        body = self.messages_body(
            model="GLM-5.2-NVFP4", max_tokens=8,
            messages=[{"role": "user", "content": "hi"}])
        for raw in ("0", "-5", "12.5", "901", "soon", "",
                    "Wed, 21 Oct 2015 07:28:00 GMT"):
            with self.subTest(retry_after=raw):
                self.upstream.retry_after[0] = raw
                status, headers, payload = self.request(
                    "POST", "/v1/messages", body=body)
                self.assertEqual(status, 503)
                self.assertIsNone(headers.get("Retry-After"))
                self.assertIn(b"um_unavailable", payload)

    # ---------------------------------------- 撤销并终止（真实 Handler 端到端）

    def test_terminate_key_interrupts_only_target_messages_stream(self):
        # 真实 handler 的在途 Messages 流被 R8-A7 终止：只关目标 Key 的
        # socket；计数减一只发生在该 handler 自己的 finally（单源），另一个
        # Key 的在途连接、ctx 与计数完全原样；上游只收到一次请求。
        self.assertTrue(self.server.acquire("friendB"))
        b_sock, b_peer = socket.socketpair()
        ctx_b = share._InflightCtx(b_sock)
        self.server.register_inflight("friendB", ctx_b)
        body = self.messages_body(
            model="GLM-5.2-NVFP4", max_tokens=64, stream=True,
            messages=[{"role": "user", "content": "hold-please"}])
        received = []

        def run():
            conn = socket.create_connection(
                ("127.0.0.1", self.server.server_port), timeout=10)
            conn.settimeout(10)
            try:
                payload = body
                conn.sendall(b"POST /v1/messages HTTP/1.1\r\n"
                             b"Host: 127.0.0.1\r\n"
                             b"Authorization: Bearer " + SECRET_A.encode()
                             + b"\r\nContent-Type: application/json\r\n"
                             + b"Content-Length: "
                             + str(len(payload)).encode() + b"\r\n\r\n"
                             + payload)
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    received.append(chunk)
            except OSError:
                pass   # 终止后连接被关：EOF/错误都不是伪造的成功终态
            finally:
                conn.close()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            # 请求穿过真实鉴权与 acquire 后才会触达上游：此刻两个 Key 的
            # 在途计数都已就位。
            self.assertTrue(_wait_until(
                lambda: len(self.upstream_requests) == 1))
            self.assertEqual(self.server.active,
                             {"friendA": 1, "friendB": 1})
            # 终止只波及目标 Key：关 1 条连接，friendB 的 socketpair 原样。
            self.assertEqual(self.server.terminate_key("friendA"), 1)
            self.assertNotIn("friendA", self.server.inflight)
            self.assertIn(ctx_b, self.server.inflight.get("friendB", set()))
            b_sock.send(b"x")
            self.assertEqual(b_peer.recv(1), b"x")
            # 计数减一发生在 handler 自己的 finally（单源）；上游写完、
            # handler unwind 后 friendA 计数归零，friendB 保持 1。
            self.hold_gate.set()
            self.assertTrue(_wait_until(
                lambda: "friendA" not in self.server.active, timeout=8))
            self.assertEqual(self.server.active, {"friendB": 1})
            thread.join(10)
            self.assertEqual(len(self.upstream_requests), 1)
            self.assertNotIn(b"message_stop", b"".join(received))
        finally:
            self.hold_gate.set()
            thread.join(5)
            self.server.release("friendB")
            b_sock.close()
            b_peer.close()

    # ------------------------------------------------------- 1 MiB 边界值

    def test_exact_one_mib_body_is_accepted(self):
        # 上限语义是 <=：恰好 1048576 字节的 Messages body 必须被接受并
        # 原样转发（1048577 的拒绝侧由 test_share_gateway 钉死）。
        base = {"model": "GLM-5.2-NVFP4", "max_tokens": 16,
                "messages": [{"role": "user", "content": ""}]}
        base_len = len(json.dumps(base))
        padding = 1024 * 1024 - base_len
        self.assertGreater(padding, 0)
        body = self.messages_body(
            model="GLM-5.2-NVFP4", max_tokens=16,
            messages=[{"role": "user", "content": "a" * padding}])
        self.assertEqual(len(body), 1024 * 1024)
        status, _headers, payload = self.request(
            "POST", "/v1/messages", body=body)
        self.assertEqual(status, 200)
        self.assertEqual(payload, ANTHROPIC_MESSAGE)
        path, _sent, forwarded_raw = self.upstream_requests[0]
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(len(forwarded_raw), 1024 * 1024)
        self.assertEqual(json.loads(forwarded_raw)["model"], "GLM-5.2-NVFP4")


if __name__ == "__main__":
    unittest.main()
