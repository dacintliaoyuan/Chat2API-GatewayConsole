# -*- coding: utf-8 -*-
"""M6-B：分享每 Key 费用链与双协议归因的全链隔离回归。

既有覆盖（本文件不重复）：tests/test_share_metering.py 已钉死 8100 标记构建
/8110 校验的单元契约、8110 直连的 Messages 归因/分段用量/断流下界、读端的
零值/别名/失败/未定价/未归属桶/轮换删除历史与私人汇总不变；
tests/test_chat2api_usage_normalize.py 钉死用量别名归一化；
tests/test_console_api.py 的 ConsoleShareUsageRouteTests 用真实版本化计价器
钉死两挂载点同一费用读面与错误码。

本文件补齐核对时发现的真空——默认链此前从未被一条用例贯通：

* 8100 真实分享入口（凭据解析 + 标记自建）→ 8110 真实 chat2api Handler
  （verify_share_attribution + set_request_facts + record_usage）→ 夹具分享
  账本 → share_metering.read_share_usage。OpenAI chat 与 Anthropic Messages
  两条协议落在同一服务端 key_id、同一读面同一 Key 桶，各请求恰一行。
* 客户端自报 key_id 到达不了账本：8100 丢弃重建，行内身份只能是已认证条目。
* 写端补强：Messages 上游报告的「真实 0」是可信计数（usage_source=upstream，
  不落回本地估算）；上游失败恰好记一行非 ok 下界。
* 私人账本零写入：分享请求前后夹具私人账本字节不变。

隔离边界：全部文件路径在导入前重定向到 tempfile 临时目录；上游是进程内
桩 UM；不重启生产端口、不读生产密钥与账本、不发真实推理或计数请求、
不操作 Git。
"""

import base64
import hashlib
import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# 照 tests/test_share_metering.py 先例：导入 chat2api 前把生产状态路径重定向
# 到夹具临时目录（导入后立即还原，避免污染同进程其他测试模块）。
_REDIRECT_TMP = tempfile.TemporaryDirectory(prefix="share-metering-chain-")
_REDIRECTED_ENV = {
    "CHAT2API_USAGE_LOG": str(Path(_REDIRECT_TMP.name)
                              / "usage-chat2api.jsonl"),
    "CHAT2API_PRICE_TABLE_FILE": str(Path(_REDIRECT_TMP.name)
                                     / "prices.json"),
    "CHAT2API_SHARE_ATTRIBUTION_KEY_FILE": str(Path(_REDIRECT_TMP.name)
                                               / "share-attribution.key"),
}
_SAVED_ENV = {name: os.environ.get(name) for name in _REDIRECTED_ENV}
os.environ.update(_REDIRECTED_ENV)
os.environ.pop("SHARE_ATTRIBUTION_KEY_FILE", None)


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, HERE / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


share = _load("share_gateway_metering_chain", "chat2api/share_gateway.py")
chat2api = _load("chat2api_metering_chain", "chat2api/chat2api.py")
metering = _load("share_metering_metering_chain", "share_metering.py")


def _write_private(path, text):
    """Fixture secrets must pass the POSIX 0600/current-uid gate."""
    from share_secret_protection import write_private_secret_path
    write_private_secret_path(Path(path), str(text).encode("utf-8"))

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import _chat2api_http_harness as harness  # noqa: E402

STUB_TOKEN = "stub-token"
for _name, _value in _SAVED_ENV.items():
    if _value is None:
        os.environ.pop(_name, None)
    else:
        os.environ[_name] = _value
del _name, _value


def _digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def base64url_decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


SECRET = "s" * 48
SHARE_KEY_NAME = "friend"
SHARE_KEY_PLAINTEXT = "test-secret"
CHAT_OPENAI_JSON = json.dumps({
    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 100, "completion_tokens": 10},
}).encode()
ANTHROPIC_MESSAGE = json.dumps({
    "id": "msg_1", "type": "message", "role": "assistant",
    "model": "GLM-5.2-NVFP4",
    "content": [{"type": "text", "text": "hello"}],
    "stop_reason": "end_turn", "stop_sequence": None,
    "usage": {"input_tokens": 100, "output_tokens": 10},
}).encode()
ANTHROPIC_MESSAGE_ZERO = json.dumps({
    "id": "msg_0", "type": "message", "role": "assistant",
    "model": "GLM-5.2-NVFP4",
    "content": [{"type": "text", "text": ""}],
    "stop_reason": "end_turn", "stop_sequence": None,
    "usage": {"input_tokens": 0, "output_tokens": 0},
}).encode()
# 单侧 usage：上游只报了输入侧（退化/异常上游形状）。coverage 必须由归一化
# 边界推导为 partial，不得把单侧报告硬编码成 upstream。
ANTHROPIC_MESSAGE_INPUT_ONLY = json.dumps({
    "id": "msg_2", "type": "message", "role": "assistant",
    "model": "GLM-5.2-NVFP4",
    "content": [{"type": "text", "text": "hello"}],
    "stop_reason": "end_turn", "stop_sequence": None,
    "usage": {"input_tokens": 100},
}).encode()
# Messages 分段用量：输入侧在 message_start，输出侧在 message_delta；
# cache_read 是输入的子集（读端由价目引擎自己减）。
ANTHROPIC_STREAM = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"id":"msg_1","type":"message",'
    b'"role":"assistant","model":"GLM-5.2-NVFP4","content":[],'
    b'"stop_reason":null,"stop_sequence":null,'
    b'"usage":{"input_tokens":100,"output_tokens":0,'
    b'"cache_read_input_tokens":20}}}\n\n'
    b'event: content_block_start\n'
    b'data: {"type":"content_block_start","index":0,'
    b'"content_block":{"type":"text","text":""}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","index":0,'
    b'"delta":{"type":"text_delta","text":"hello"}}\n\n'
    b'event: content_block_stop\n'
    b'data: {"type":"content_block_stop","index":0}\n\n'
    b'event: message_delta\n'
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn",'
    b'"stop_sequence":null},"usage":{"output_tokens":10}}\n\n'
    b'event: message_stop\n'
    b'data: {"type":"message_stop"}\n\n'
)


class _StubUM(BaseHTTPRequestHandler):
    """最末端替身：8110 的 UM 上游，应答 chat/messages 两种形状。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_GET(self):
        payload = json.dumps({"object": "list", "data": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        holder = self.server.holder
        if self.path.endswith("/messages"):
            if body.get("stream"):
                payload = holder["stream"]
                content_type = "text/event-stream"
            else:
                payload = holder["message"]
                content_type = "application/json"
            status = holder["messages_status"]
        elif self.path.endswith("/chat/completions"):
            payload = holder["chat"]
            content_type = "application/json"
            status = 200
        else:
            payload = b'{"error":"unexpected path"}'
            content_type = "application/json"
            status = 404
        body_bytes = payload if isinstance(payload, bytes) else payload.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)


class _FullChainFixture(unittest.TestCase):
    """8100 真分享入口 → 8110 真后端 Handler → 桩 UM → 夹具账本。"""

    def setUp(self):
        # B8-0 隔离生命周期（见 tests/_chat2api_http_harness.py）：账本重定向
        # 最先注册、最后释放——所有在途 handler 收尾之后才释放 patch。修复前的
        # LIFO 缺陷会在 handler 仍在写 record_usage 时先恢复生产路径，这正是
        # 2026-10-02 生产账本污染行（139307–139312 等）的来源。
        self._tmp, self.share_ledger = harness.install_usage_isolation(
            self, chat2api, ledger_name="usage-share-backend.jsonl")
        harness.assert_stub_credentials(
            chat2api, local_key=None, upstream_token=STUB_TOKEN,
            stub_token_literal=STUB_TOKEN)
        base = self._tmp
        self.config = base / "um-share.json"
        self.config.write_text(json.dumps(
            {"clients": {SHARE_KEY_NAME: _digest(SHARE_KEY_PLAINTEXT)}}),
            encoding="utf-8")
        self.key_file = base / "share-attribution.key"
        _write_private(self.key_file, SECRET)
        self.private_ledger = base / "usage-chat2api.jsonl"
        self.private_ledger.write_text("", encoding="utf-8")
        prices = base / "prices.json"
        prices.write_text(json.dumps({"version": "v-fixture"}),
                          encoding="utf-8")
        self.holder = {
            "chat": CHAT_OPENAI_JSON,
            "message": ANTHROPIC_MESSAGE,
            "stream": ANTHROPIC_STREAM,
            "messages_status": 200,
        }
        self.upstream = harness.TrackedHTTPServer(("127.0.0.1", 0), _StubUM)
        self.upstream.holder = self.holder
        threading.Thread(target=self.upstream.serve_forever,
                         daemon=True).start()
        harness.install_tracked_server(self, self.upstream,
                                       name="upstream-stub",
                                       require_drained=False)
        args = SimpleNamespace(
            base_url=f"http://127.0.0.1:{self.upstream.server_port}",
            token=STUB_TOKEN, profile="profile", login_timeout=1,
            use_api_key=False, no_browser=True)
        self.proxy = chat2api.Proxy({"token": STUB_TOKEN}, args)

        class _QuietHandler(chat2api.Handler):
            # 只静默每请求的 stderr 访问日志；行为与 chat2api.Handler 全同。
            def log_message(self, *_args):
                pass

        self.backend = harness.TrackedHTTPServer(
            ("127.0.0.1", 0), _QuietHandler)
        self.backend.proxy = self.proxy
        threading.Thread(target=self.backend.serve_forever,
                         daemon=True).start()
        self.share = share.ShareServer(
            ("127.0.0.1", 0), self.config,
            upstream_port=self.backend.server_address[1],
            attribution_secret=SECRET)
        self.share_thread = threading.Thread(target=self.share.serve_forever,
                                             daemon=True)
        self.share_thread.start()
        # Registered AFTER the redirect: this teardown (shutdown → bounded
        # drain of in-flight requests → close) runs BEFORE the USAGE_LOG
        # patch stop, so the post-response record_usage tail of the LAST
        # request lands in the temp ledger, never the production one.
        harness.install_tracked_server(self, self.backend,
                                       name="chat2api-backend")
        for patch in (
            mock.patch.object(chat2api, "SHARE_ATTRIBUTION_KEY_FILE",
                              str(self.key_file)),
            mock.patch.object(chat2api, "PRICE_TABLE_FILE", str(prices)),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRY", False),
            mock.patch.object(chat2api, "UPSTREAM_AUTO_RETRIES", 0),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRY", False),
            mock.patch.object(chat2api, "REPETITION_GUARD_AUTO_RETRIES", 0),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        # 两个全局缓存按 (path, mtime, size) 判定，夹具路径必须重新生效；
        # 重放登记与读槽是模块级状态，逐用例重建。
        chat2api._attribution_key_cache["stat"] = None
        chat2api._price_version_cache["stat"] = None
        chat2api._attribution_seen.clear()
        chat2api._reader_slots = threading.BoundedSemaphore(
            chat2api.STREAM_MAX_CONCURRENT_READERS)

    def tearDown(self):
        self.holder["messages_status"] = 200
        # 分享入口不属于 harness 管辖（它写夹具分享账本，不写 chat2api 账本），
        # 沿用原有 Windows 收尾顺序：先让 serve_forever 退出，再关 socket。
        try:
            self.share.shutdown()
        except Exception:   # noqa: BLE001 — 关闭失败不掩盖测试结论
            pass
        self.share_thread.join(5)
        try:
            self.share.server_close()
        except OSError:
            pass

    # ------------------------------------------------------------- helpers

    def share_request(self, path, body, headers=None):
        """经真实分享入口的本地请求（Bearer 认证）。"""
        payload = json.dumps(body).encode()
        request_headers = {"Content-Type": "application/json",
                           "Authorization": "Bearer " + SHARE_KEY_PLAINTEXT}
        request_headers.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1",
                                          self.share.server_port, timeout=15)
        try:
            conn.request("POST", path, body=payload,
                         headers=request_headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def backend_request(self, path, body, headers=None):
        payload = json.dumps(body).encode()
        request_headers = {"Content-Type": "application/json",
                           "Content-Length": str(len(payload))}
        request_headers.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1",
                                          self.backend.server_address[1],
                                          timeout=15)
        try:
            conn.request("POST", path, body=payload,
                         headers=request_headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def marker_headers(self, key_id=SHARE_KEY_NAME):
        headers, _ = share.build_attribution(SECRET, key_id)
        return headers

    def records(self, count=1, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.current_records()
            if len(found) >= count:
                return found
            time.sleep(0.1)
        self.fail(f"usage records not written within {timeout}s")

    def current_records(self):
        try:
            with open(self.share_ledger, encoding="utf-8") as handle:
                return [json.loads(line) for line in handle if line.strip()]
        except FileNotFoundError:
            return []

    @staticmethod
    def price(model_id, at_ms, input_tok, output_tok, cache_tok, reasoning_tok):
        """夹具计价器：input+output 每 token 0.001 USD，版本固定。"""
        return (input_tok + output_tok) / 1000.0, True, model_id, False, "v-x"


class FullChainAttributionTests(_FullChainFixture):
    def test_full_chain_attributes_both_protocols_to_one_server_key(self):
        # 默认链贯通：凭据解析（8100）→ 服务端自建标记 → 8110 校验落行 →
        # 夹具账本 → read_share_usage。四请求：chat 非流式、messages 非流式、
        # messages 分段流式、messages 伪造 key_id（8100 必须丢弃重建）。
        forged = dict(self.marker_headers(key_id="attacker"))
        self.assertEqual(
            base64url_decode(forged[share.X_ATTRIBUTION_KEY_ID]), b"attacker")
        status, _ = self.share_request(
            "/v1/chat/completions",
            {"model": "GLM-5.2-NVFP4",
             "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        # record_usage 在响应之后写入；等本请求的行落盘再发下一个，
        # 否则并行调度可能让后一个请求的行先写，顺序断言变成时序噪声。
        self.records(count=1)
        status, _ = self.share_request(
            "/v1/messages",
            {"model": "GLM-5.2-NVFP4", "max_tokens": 8,
             "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.records(count=2)
        status, _ = self.share_request(
            "/v1/messages",
            {"model": "GLM-5.2-NVFP4", "max_tokens": 8, "stream": True,
             "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.records(count=3)
        # 客户端自报 key_id=attacker：8100 白名单重建后上游只能看到 friend。
        status, _ = self.share_request(
            "/v1/messages",
            {"model": "GLM-5.2-NVFP4", "max_tokens": 8,
             "messages": [{"role": "user", "content": "hi"}]},
            headers=forged)
        self.assertEqual(status, 200)
        rows = self.records(count=4)
        self.assertEqual(len(rows), 4)   # 每请求恰一行，无重复计费
        self.assertEqual([row["key_id"] for row in rows],
                         [SHARE_KEY_NAME] * 4)
        self.assertEqual([row["protocol"] for row in rows],
                         ["chat", "messages", "messages", "messages"])
        self.assertEqual({row["attribution"] for row in rows}, {"verified"})
        request_ids = [row["request_id"] for row in rows]
        self.assertEqual(len(set(request_ids)), 4)   # 各请求各自的标记
        self.assertEqual(
            [row["price_version"] for row in rows], ["v-fixture"] * 4)
        chat, nonstream, streamed, forged_row = rows
        # chat：OpenAI usage 块归一化（canonical 命名）。
        self.assertEqual(chat["input_tokens"], 100)
        self.assertEqual(chat["output_tokens"], 10)
        self.assertEqual(chat["usage_source"], "upstream")
        self.assertFalse(chat["estimated"])
        # messages 非流式：完整 usage 块。
        self.assertEqual(nonstream["input_tokens"], 100)
        self.assertEqual(nonstream["output_tokens"], 10)
        self.assertEqual(nonstream["usage_source"], "upstream")
        # messages 分段流式：message_start 的输入侧（含 cache 子集）+
        # message_delta 的输出侧，同 key 同读面。
        self.assertEqual(streamed["input_tokens"], 120)
        self.assertEqual(streamed["cached_input_tokens"], 20)
        self.assertEqual(streamed["output_tokens"], 10)
        self.assertEqual(streamed["usage_source"], "upstream")
        # 伪造的 key_id 到不了账本：行内身份只能是已认证条目。
        self.assertEqual(forged_row["key_id"], SHARE_KEY_NAME)
        # 私人账本零写入：分享请求只落分享账本。
        self.assertEqual(self.private_ledger.read_bytes(), b"")
        # 同一费用读面：双协议四行合并进同一 Key 桶，known 求和可复现。
        result = metering.read_share_usage(str(self.share_ledger), self.price)
        self.assertEqual([k["key_id"] for k in result["keys"]],
                         [SHARE_KEY_NAME])
        key = result["keys"][0]
        self.assertEqual(key["requests"], 4)
        self.assertAlmostEqual(key["knownCostUsd"], 0.46)
        self.assertEqual(key["unknownRequests"], 0)
        self.assertEqual(result["coverage"]["attributed"], 4)
        self.assertEqual(result["coverage"]["unattributed"], 0)

    def test_messages_zero_usage_is_trusted_upstream_zero(self):
        # 零值（写端）：上游明确报告 0/0 是可信计数——usage_source=upstream、
        # estimated=False，绝不落回本地估算；读端进 known 桶而非未知。
        self.holder["message"] = ANTHROPIC_MESSAGE_ZERO
        status, _ = self.backend_request(
            "/v1/messages",
            {"model": "GLM-5.2-NVFP4", "max_tokens": 8,
             "messages": [{"role": "user", "content": "hi"}]},
            headers=self.marker_headers())
        self.assertEqual(status, 200)
        (row,) = self.records()
        self.assertEqual(row["input_tokens"], 0)
        self.assertEqual(row["output_tokens"], 0)
        self.assertEqual(row["usage_source"], "upstream")
        self.assertFalse(row["estimated"])
        self.assertEqual(row["key_id"], SHARE_KEY_NAME)
        result = metering.read_share_usage(str(self.share_ledger), self.price)
        key = result["keys"][0]
        self.assertEqual(key["requests"], 1)
        self.assertEqual(key["knownCostUsd"], 0.0)
        self.assertEqual(key["unknownRequests"], 0)
        # 真实 0 进模型明细（未知用量的记录不进明细），金额为 0。
        self.assertEqual([m["requests"] for m in key["models"]], [1])

    def test_messages_single_side_usage_is_partial_not_upstream(self):
        # 单侧报告（写端，复核修复回归）：非流式 Messages 的 coverage 走
        # record_usage 的归一化推导边界——只有输入侧时报 partial（输出侧
        # 保持本地估算），绝不把单侧块硬编码成 "upstream"。
        self.holder["message"] = ANTHROPIC_MESSAGE_INPUT_ONLY
        status, _ = self.backend_request(
            "/v1/messages",
            {"model": "GLM-5.2-NVFP4", "max_tokens": 8,
             "messages": [{"role": "user", "content": "hi"}]},
            headers=self.marker_headers())
        self.assertEqual(status, 200)
        (row,) = self.records()
        self.assertEqual(row["input_tokens"], 100)
        # "hello" 的本地字符估算（max(1, 5//4)=1）：非 0 的诚实下界，
        # 绝不冒充上游证据。
        self.assertEqual(row["output_tokens"], 1)
        self.assertEqual(row["usage_source"], "partial")
        self.assertFalse(row["estimated"])

    def test_upstream_failure_records_one_non_ok_row(self):
        # 失败（写端）：上游 502 恰好记一行非 ok 的下界（本地输入估算），
        # 不伪造成完整成功，读端进 incompleteRequests 与估算桶。
        self.holder["messages_status"] = 502
        self.holder["message"] = b'{"error":{"message":"upstream down"}}'
        status, _ = self.backend_request(
            "/v1/messages",
            {"model": "GLM-5.2-NVFP4", "max_tokens": 8,
             "messages": [{"role": "user", "content": "hello world"}]},
            headers=self.marker_headers())
        self.assertEqual(status, 502)
        (row,) = self.records()
        self.assertNotEqual(row["status"], "ok")
        self.assertEqual(row["status"], "HTTP502")
        self.assertEqual(row["key_id"], SHARE_KEY_NAME)
        self.assertEqual(row["attribution"], "verified")
        self.assertEqual(row["usage_source"], "estimated")
        self.assertTrue(row["estimated"])
        self.assertGreater(row["input_tokens"], 0)   # 本地输入估算下界
        self.assertEqual(row["output_tokens"], 0)
        result = metering.read_share_usage(str(self.share_ledger), self.price)
        key = result["keys"][0]
        self.assertEqual(key["requests"], 1)
        # 估算桶只含这一行的已知下界（输入侧），known 桶为 0。
        self.assertAlmostEqual(key["estimatedCostUsd"],
                               row["input_tokens"] / 1000.0)
        self.assertEqual(key["knownCostUsd"], 0.0)
        self.assertEqual(result["coverage"]["incompleteRequests"], 1)


class _HeaderBag:
    """最小 header 容器（与 email.message 同形：大小写不敏感 get_all）。"""

    def __init__(self, pairs):
        self._pairs = [(str(k), v) for k, v in pairs.items()]

    def get_all(self, name, default=None):
        lower = name.lower()
        return [v for k, v in self._pairs if k.lower() == lower]


class VerifyShareAttributionPartialHeadersTests(unittest.TestCase):
    """五个标记头只来一部分时必须 fail-closed，而不是 IndexError（复核修复）。

    生产 8100 恒发全有或全无的标记（share_gateway.build_attribution）；
    直连 8110 的外部客户端才可能发部分头。修复前 `values[...][0]` 直接索引
    空列表，请求以未处理异常告终而不是按未归属落行。
    """

    def setUp(self):
        # 密钥文件指向私有临时目录：全有标记的对照用例要走完整验证链，
        # 部分标记用例在密钥检查之前就已 fail-closed 返回。
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        key_file = Path(self._tmp.name) / "share-attribution.key"
        _write_private(key_file, SECRET)
        patch = mock.patch.object(chat2api, "SHARE_ATTRIBUTION_KEY_FILE",
                                  str(key_file))
        patch.start()
        self.addCleanup(patch.stop)
        chat2api._attribution_key_cache["stat"] = None
        self.addCleanup(chat2api._attribution_seen.clear)

    def test_partial_marker_headers_fail_closed(self):
        headers, _ = share.build_attribution(SECRET, SHARE_KEY_NAME)
        partial = _HeaderBag({
            name: value for name, value in headers.items()
            if name in (share.X_ATTRIBUTION_KEY_ID,
                        share.X_ATTRIBUTION_SIGNATURE)})
        key_id, request_id, reason = chat2api.verify_share_attribution(partial)
        self.assertIsNone(key_id)
        self.assertIsNone(request_id)
        self.assertEqual(reason, "partial_headers")

    def test_single_marker_header_is_partial_not_index_error(self):
        # 只带版本头：同样按部分标记拒绝（绝不索引空列表，也绝不猜身份）。
        headers, _ = share.build_attribution(SECRET, SHARE_KEY_NAME)
        only_version = _HeaderBag({
            share.X_ATTRIBUTION_VERSION: headers[share.X_ATTRIBUTION_VERSION]})
        key_id, request_id, reason = \
            chat2api.verify_share_attribution(only_version)
        self.assertIsNone(key_id)
        self.assertIsNone(request_id)
        self.assertEqual(reason, "partial_headers")

    def test_full_marker_set_still_verifies(self):
        # 全有标记不受影响：修复只加宽 fail-closed 面，不改验证链。
        headers, _ = share.build_attribution(SECRET, SHARE_KEY_NAME)
        key_id, _, reason = chat2api.verify_share_attribution(_HeaderBag(headers))
        self.assertEqual(key_id, SHARE_KEY_NAME)
        self.assertEqual(reason, "verified")


if __name__ == "__main__":
    unittest.main()
