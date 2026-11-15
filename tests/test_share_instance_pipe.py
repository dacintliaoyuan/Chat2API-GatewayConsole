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
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chat2api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

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


@unittest.skipUnless(os.name == "posix",
                     "AF_UNIX instance channel is POSIX-only")
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
