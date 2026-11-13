"""Offline tests for the share-side instance-channel wire server
(ai-gateway/share/instance_pipe.py, R41-1).

Fake kernels only - no real pipe. Verifies: the bounded wire layer
dispatches to the test double's handle_instance_control (signed schema
v2, instance binding, replay), oversized frames are refused before
dispatch, a failed listener is an EXPLICIT not-ready state that never
writes the registration file, and a successful start self-publishes an
atomic registration record.
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chat2api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import share.broker_protocol as bp  # noqa: E402
import share.instance_pipe as sip  # noqa: E402
from _share_instance_double import FakeShareServer as ShareServer  # noqa: E402


def _projection(generation=1):
    return {"schema_version": 1, "config_generation": generation,
            "data": [{"key_id": "u1", "name": "f1",
                      "secret_hash": "a" * 64, "secret_version": 1,
                      "enabled": True}]}


class FakeServerKernel:
    """Server-side fake kernel32: serves ONE prepared request frame."""

    def __init__(self, request_frame=b"", fail_create=False):
        self.request_frame = request_frame
        self.fail_create = fail_create
        self.created_args = None
        self.written = bytearray()
        self.disconnects = 0

    def CreateNamedPipeW(self, name, open_mode, pipe_mode, instances,
                         out_size, in_size, timeout, security):
        self.created_args = (name, open_mode)
        if self.fail_create:
            return None
        return 0x2000

    def ConnectNamedPipe(self, h, overlapped):
        return 1

    def ReadFile(self, h, buf, n, read, overlapped):
        data = self.request_frame[:n]
        buf[:len(data)] = data
        read.contents.value = len(data)
        return 1

    def WriteFile(self, h, data, n, written, overlapped):
        self.written += bytes(data[:n])
        written.contents.value = n
        return 1

    def FlushFileBuffers(self, h):
        return 1

    def DisconnectNamedPipe(self, h):
        self.disconnects += 1
        return 1

    def CloseHandle(self, h):
        return 1

    def GetLastError(self):
        return 0

    def GetNamedPipeClientProcessId(self, h, p):
        p.contents.value = 4242
        return 1


class InstancePipeServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name) / "um-share-projection.json"
        self.config.write_text(json.dumps(_projection()), encoding="utf-8")
        self.share = ShareServer(("127.0.0.1", 0), self.config,
                                 auth_mode="projection")
        self.addCleanup(self.share.server_close)

    def tearDown(self):
        self.temp.cleanup()

    def _pipe_name(self):
        return sip.instance_pipe_name(r"\\.\pipe\um-share-instance-",
                                      self.share.instance_id)

    def test_dispatch_to_real_instance_handler(self):
        # 有签名的 instance-status 请求 → 替身 handle_instance_control 校验
        # （mac + instance 绑定 + 重放消费）→ 结构化 ok 响应写回。
        frame = bp.encode_frame(bp.build_request(
            "inst-k", "instance-status", instance_id=self.share.instance_id))
        kernel = FakeServerKernel(frame)
        server = sip.InstancePipeServer(self.share, "inst-k",
                                        self._pipe_name(), kernel=(kernel, None))
        record = server.serve_once()
        self.assertTrue(record["ok"], record)
        response = json.loads(bytes(kernel.written))
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["instance_id"],
                         self.share.instance_id)
        self.assertEqual(response["result"]["state"], "running")

    def test_wrong_secret_refused_by_real_handler(self):
        # 坏密钥 → 真实 handler 返回 forbidden，wire 层原样转发。
        frame = bp.encode_frame(bp.build_request(
            "WRONG", "instance-status", instance_id=self.share.instance_id))
        kernel = FakeServerKernel(frame)
        server = sip.InstancePipeServer(self.share, "inst-k",
                                        self._pipe_name(), kernel=(kernel, None))
        record = server.serve_once()
        self.assertTrue(record["ok"])
        response = json.loads(bytes(kernel.written))
        self.assertFalse(response["ok"])
        self.assertEqual(response["error_code"], "forbidden")

    def test_wrong_instance_binding_refused(self):
        # 指向另一实例的请求 → 真实 handler 返回 instance_mismatch。
        frame = bp.encode_frame(bp.build_request(
            "inst-k", "instance-status", instance_id="i-other"))
        kernel = FakeServerKernel(frame)
        server = sip.InstancePipeServer(self.share, "inst-k",
                                        self._pipe_name(), kernel=(kernel, None))
        server.serve_once()
        response = json.loads(bytes(kernel.written))
        self.assertEqual(response["error_code"], "instance_mismatch")

    def test_unparsable_frame_is_bad_request(self):
        kernel = FakeServerKernel(b"not-json\n")
        server = sip.InstancePipeServer(self.share, "inst-k",
                                        self._pipe_name(), kernel=(kernel, None))
        record = server.serve_once()
        self.assertTrue(record["ok"])
        response = json.loads(bytes(kernel.written))
        self.assertEqual(response["error_code"], "bad_request")

    def test_oversized_frame_refused_before_dispatch(self):
        kernel = FakeServerKernel(b"x" * (bp.MAX_MESSAGE + 1) + b"\n")
        server = sip.InstancePipeServer(self.share, "inst-k",
                                        self._pipe_name(), kernel=(kernel, None))
        record = server.serve_once()
        self.assertFalse(record["ok"])
        self.assertEqual(record.get("error_code"), "overlimit")
        response = json.loads(bytes(kernel.written))
        self.assertEqual(response["error_code"], "overlimit")

    def test_pipe_name_carries_instance_id(self):
        # 管道名 = 前缀 + instance_id（寻址）；身份由签名帧与 verifier 承担。
        name = self._pipe_name()
        self.assertTrue(name.startswith(r"\\.\pipe\um-share-instance-"))
        with self.assertRaises(ValueError):
            sip.instance_pipe_name(r"\\.\pipe\um-share-instance-", "")


class StartInstanceChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name) / "um-share-projection.json"
        self.config.write_text(json.dumps(_projection()), encoding="utf-8")
        self.share = ShareServer(("127.0.0.1", 0), self.config,
                                 auth_mode="projection")
        self.addCleanup(self.share.server_close)
        self.registration = Path(self.temp.name) / "run" / "live" \
            / "instance-registration.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_listener_failure_is_explicit_not_ready(self):
        # R41-1：监听创建失败 → 明确未就绪；HTTP 主服务不受影响、登记文件
        # 绝不写入。
        kernel = FakeServerKernel(b"", fail_create=True)
        record = sip.start_instance_channel(
            self.share, "inst-k", r"\\.\pipe\um-share-instance-",
            self.registration, kernel=(kernel, None))
        self.assertFalse(record["ready"])
        self.assertIn("error", record)
        self.assertFalse(self.registration.exists())
        self.assertIsNotNone(self.share.instance_id)   # 主服务仍活着

    def test_ready_start_writes_atomic_registration(self):
        kernel = FakeServerKernel(b"")
        record = sip.start_instance_channel(
            self.share, "inst-k", r"\\.\pipe\um-share-instance-",
            self.registration, kernel=(kernel, None))
        self.assertTrue(record["ready"], record)
        try:
            payload = json.loads(self.registration.read_text(encoding="utf-8"))
            self.assertEqual(payload["instance_id"], self.share.instance_id)
            self.assertEqual(payload["pipe_name"], record["pipe_name"])
            self.assertIsInstance(payload["pid"], int)
        finally:
            record["server"].stop()

    def test_unwritable_registration_reports_not_ready(self):
        # 监听就绪但登记文件写不进（父目录是文件）→ 明确未就绪，不伪装成功。
        blocker = Path(self.temp.name) / "run"
        blocker.write_text("not a directory", encoding="utf-8")
        kernel = FakeServerKernel(b"")
        record = sip.start_instance_channel(
            self.share, "inst-k", r"\\.\pipe\um-share-instance-",
            self.registration, kernel=(kernel, None))
        self.assertFalse(record["ready"])
        self.assertIn("registration write failed", record["error"])
        record["server"].stop()

    def test_registration_write_remove_roundtrip(self):
        payload = sip.write_registration(
            self.registration, "i-9", r"\\.\pipe\um-share-instance-i-9")
        self.assertEqual(payload["instance_id"], "i-9")
        self.assertTrue(self.registration.exists())
        sip.remove_registration(self.registration)
        self.assertFalse(self.registration.exists())
        sip.remove_registration(self.registration)   # 幂等


if __name__ == "__main__":
    unittest.main()
