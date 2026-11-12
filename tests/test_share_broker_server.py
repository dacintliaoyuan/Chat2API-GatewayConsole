"""D4-prerequisite offline tests for the SYSTEM broker management pipe
server (ai-gateway/share_broker_server.py).

Everything runs on injected fake kernels and fixture coordinators/writers
in temp dirs - no real pipe, no SYSTEM task, no RunRoot access. The server
holds NO product coordination rules (they live in BrokerHostCoordinator,
already covered by test_share_broker.py); these tests verify the WIRE
layer (framing, bounded reads/writes, R41-4 cycle ownership and the
R37 admission contract) and the R41-1/R41-2 assembly closed loop: a REAL
signed management request through build_server's coordinator, a REAL
instance-channel signature frame validated by ShareServer.handle_instance_control,
and an ACK - never an echo fixture standing in for the instance.
"""
import argparse
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _write_secret(path, text):
    """Fixture secret files must pass the POSIX 0600/current-uid gate."""
    path = Path(path)
    path.write_text(text, encoding="utf-8")
    if os.name == "posix":
        os.chmod(path, 0o600)
    return path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chat2api"))

import share_broker_protocol as bp  # noqa: E402
import share_broker_server as srv  # noqa: E402
from share_broker_host import (BrokerHostCoordinator,  # noqa: E402
                               InstanceRegistry, MaintenanceLeaseStore,
                               ReplayRegistry)
from share_broker_ops import WindowsInstanceVerifier  # noqa: E402
from share_entry import ShareServer  # noqa: E402
from share_runtime import ProtectedProjectionWriter  # noqa: E402


def _projection(generation=1, entries=None):
    return {"schema_version": 1, "config_generation": generation,
            "data": [{"key_id": "uuid-1", "name": "friend1",
                      "secret_hash": "a" * 64, "secret_version": 1,
                      "enabled": True}]}


class RecordingOps:
    """Fixture host ops with observable state (never product logic)."""

    def __init__(self):
        self.running = False
        self.status_calls = 0

    def start_host(self):
        self.running = True
        return True

    def stop_host(self):
        self.running = False
        return True

    def verify_running(self):
        return self.running

    def verify_closed(self):
        return not self.running

    def status(self):
        self.status_calls += 1
        return {"host": "fixture", "running": self.running}


class _ServerFixture:
    """Real coordinator + real protected writer + fake instance channel."""

    def __init__(self, td):
        self.ops = RecordingOps()
        self.config = Path(td) / "um-share-projection.json"
        self.config.write_text(json.dumps(_projection()), encoding="utf-8")
        self.ack_calls = []

        def fake_instance_call(action, fields, instance_id):
            self.ack_calls.append(action)
            return {"ok": True,
                    "result": {"instance_id": instance_id,
                               "generation": fields["generation"],
                               "fingerprint": fields["fingerprint"],
                               "installed": True}}

        self.writer = ProtectedProjectionWriter(
            self.config, acl_checker=lambda path: None)
        self.instances = InstanceRegistry()
        self.instances.register("i-1")
        self.coordinator = BrokerHostCoordinator(
            writer=self.writer, mgmt_secret="mgmt-k",
            instance_secret="inst-k", instances=self.instances,
            replay=bp.ReplayRegistry(), host_ops=self.ops,
            instance_call=fake_instance_call)


class FakeK32:
    """Fake kernel32 half of the server's injectable facade. Handles are
    per-cycle distinct values and every op is tagged with the handle it
    operated on, so R41-4 cycle ownership is OBSERVABLE."""

    def __init__(self, request_frame=b""):
        self.request_frame = request_frame
        self.gate = None            # when set: ConnectNamedPipe/ReadFile block
        self.created_args = None
        self.read_calls = 0
        self.written = bytearray()
        self.disconnects = 0
        self.close_calls = 0
        self.fail_create = None     # None | 'null' | 'invalid'
        self.client_pid = 4242
        self._next_handle = 0x2000
        self.ops = []               # (handle, op) in call order
        self.gates = {}

    def CreateNamedPipeW(self, name, open_mode, pipe_mode, instances,
                         out_size, in_size, timeout, security):
        self.created_args = (name, open_mode, instances)
        if self.fail_create == 'null':
            return None
        if self.fail_create == 'invalid':
            return (1 << 64) - 1
        handle = self._next_handle
        self._next_handle += 0x100
        return handle

    def ConnectNamedPipe(self, h, overlapped):
        self.ops.append((h, "connect"))
        if self.gate is not None:
            self.gate.wait(30)
        return 1

    def ReadFile(self, h, buf, n, read, overlapped):
        self.ops.append((h, "read"))
        self.read_calls += 1
        if self.gate is not None:
            self.gate.wait(30)
        data = self.request_frame[:n]
        buf[:len(data)] = data
        read.contents.value = len(data)
        return 1

    def WriteFile(self, h, data, n, written, overlapped):
        self.ops.append((h, "write"))
        self.written += bytes(data[:n])
        written.contents.value = n
        return 1

    def FlushFileBuffers(self, h):
        self.ops.append((h, "flush"))
        return 1

    def DisconnectNamedPipe(self, h):
        self.ops.append((h, "disconnect"))
        self.disconnects += 1
        return 1

    def CloseHandle(self, h):
        self.ops.append((h, "close"))
        self.close_calls += 1
        if self.gate is not None:
            self.gate.set()          # cancel-close releases the blocked call
        return 1

    def GetLastError(self):
        return 0

    def GetNamedPipeClientProcessId(self, h, p):
        p.contents.value = self.client_pid
        return 1


def _server(td, kernel, coordinator, deadline=5.0, state_dir=None):
    return srv.ManagementPipeServer(
        coordinator, r"\\.\pipe\um-share-broker", kernel=(kernel, None),
        deadline_seconds=deadline, state_dir=state_dir)


class ServerPipeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.fx = _ServerFixture(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_roundtrip_reaches_coordinator_and_replies(self):
        # 闭环雏形：connect→读一帧→dispatch→写回→flush→断开；客户端身份
        # PID 进入证据记录；关闭资源恰好一次。
        kernel = FakeK32(bp.encode_frame(bp.build_request("mgmt-k", "status")))
        server = _server(self.temp.name, kernel, self.fx.coordinator)
        record = server.serve_once()
        self.assertTrue(record["ok"], record)
        self.assertEqual(record["phase"], "reply")
        self.assertEqual(record["client_pid"], 4242)
        response = json.loads(bytes(kernel.written))
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["host"], "fixture")
        self.assertEqual(kernel.disconnects, 1)
        self.assertEqual(kernel.close_calls, 1)
        self.assertEqual(self.fx.ops.status_calls, 1)

    def test_invalid_mac_is_refused_before_any_action(self):
        # HMAC 在 coordinator 验证：坏签名请求得到结构化拒绝且不触达
        # 任何 host 动作（fake ops 零调用）。
        kernel = FakeK32(bp.encode_frame(bp.build_request("WRONG", "status")))
        server = _server(self.temp.name, kernel, self.fx.coordinator)
        record = server.serve_once()
        self.assertTrue(record["ok"])
        response = json.loads(bytes(kernel.written))
        self.assertFalse(response["ok"])
        self.assertNotEqual(response["error_code"], "ok")
        self.assertEqual(self.fx.ops.status_calls, 0)

    def test_overlimit_frame_is_explicit_never_success(self):
        # 超过 1 MiB 无换行帧 → 明确 overlimit 响应码 + 连接释放，绝不成功。
        kernel = FakeK32(b"x" * (bp.MAX_MESSAGE + 16))
        server = _server(self.temp.name, kernel, self.fx.coordinator)
        record = server.serve_once()
        self.assertFalse(record["ok"])
        self.assertEqual(record.get("error_code"), "overlimit")
        response = json.loads(bytes(kernel.written))
        self.assertEqual(response["error_code"], "overlimit")
        self.assertFalse(response["ok"])
        self.assertEqual(kernel.disconnects, 1)

    def test_newline_terminated_oversized_frame_never_dispatched(self):
        # R41-4：带换行的超限帧在累计长度检查处被拒——绝不进入 dispatch。
        kernel = FakeK32(b"x" * (bp.MAX_MESSAGE + 1) + b"\n")
        server = _server(self.temp.name, kernel, self.fx.coordinator)
        record = server.serve_once()
        self.assertFalse(record["ok"])
        self.assertEqual(record.get("error_code"), "overlimit")
        self.assertEqual(self.fx.ops.status_calls, 0)
        response = json.loads(bytes(kernel.written))
        self.assertEqual(response["error_code"], "overlimit")

    def test_unparsable_frame_is_bad_request(self):
        kernel = FakeK32(b"not-json\n")
        server = _server(self.temp.name, kernel, self.fx.coordinator)
        record = server.serve_once()
        self.assertTrue(record["ok"])
        response = json.loads(bytes(kernel.written))
        self.assertEqual(response["error_code"], "bad_request")

    def test_create_passes_first_instance_flag_and_duplex(self):
        # R41-4：FIRST_PIPE_INSTANCE 真正并入 openMode；DUPLEX 打开。
        kernel = FakeK32(bp.encode_frame(bp.build_request("mgmt-k", "status")))
        server = _server(self.temp.name, kernel, self.fx.coordinator)
        server.serve_once()
        name, open_mode, instances = kernel.created_args
        self.assertEqual(name, r"\\.\pipe\um-share-broker")
        self.assertEqual(
            open_mode, srv.BoundedPipeServer._PIPE_ACCESS_DUPLEX
            | srv.BoundedPipeServer._FILE_FLAG_FIRST_PIPE_INSTANCE)

    def test_invalid_handle_value_is_loud_refusal(self):
        # R41-4：INVALID_HANDLE_VALUE（与 NULL 不同的失败形态）同样响亮拒绝。
        kernel = FakeK32(b"")
        kernel.fail_create = 'invalid'
        server = _server(self.temp.name, kernel, self.fx.coordinator)
        record = server.serve_once()
        self.assertFalse(record["ok"])
        self.assertEqual(record["phase"], "create")
        self.assertIn("CreateNamedPipeW failed", record["error"])

    def test_create_failure_is_loud_not_silent_reuse(self):
        # H4 离线行为面：创建失败（如 FIRST_INSTANCE 冲突）→ 明确错误、
        # 不复用旧句柄、记录 phase=create。
        kernel = FakeK32(b"")
        kernel.fail_create = 'null'
        server = _server(self.temp.name, kernel, self.fx.coordinator)
        record = server.serve_once()
        self.assertFalse(record["ok"])
        self.assertEqual(record["phase"], "create")
        self.assertIn("CreateNamedPipeW failed", record["error"])

    def test_publish_dispatches_full_chain(self):
        # publish：writer 替换 + 实例 ACK 假通道 → ok 响应；投影落点已更新。
        projection = _projection(generation=2)
        request = bp.encode_frame(bp.build_request(
            "mgmt-k", "publish",
            {"generation": 2,
             "fingerprint": self.fx.writer.fingerprint(projection),
             "projection": projection}))
        kernel = FakeK32(request)
        server = _server(self.temp.name, kernel, self.fx.coordinator)
        record = server.serve_once()
        self.assertTrue(record["ok"], record)
        response = json.loads(bytes(kernel.written))
        self.assertTrue(response["ok"], response)
        self.assertTrue(response["result"]["installed"])
        self.assertEqual(self.fx.ack_calls, ["load"])

    def test_connection_deadline_abandons_bounded(self):
        # connect 阻塞→cycle 到期→取消+释放连接（fake CloseHandle 释放
        # 阻塞）→ worker 有界窗口内确认退出→额度释放、记录 abandoned。
        kernel = FakeK32(b"")
        kernel.gate = threading.Event()
        cls = srv.ManagementPipeServer
        saved_unconfirmed = cls._unconfirmed_abandoned
        server = _server(self.temp.name, kernel, self.fx.coordinator)
        record = server.serve_once(deadline=time.monotonic() + 0.3)
        self.assertFalse(record["ok"])
        self.assertTrue(record.get("abandoned"))
        self.assertIn("deadline", record["error"])
        self.assertGreaterEqual(kernel.disconnects, 1)
        self.assertEqual(cls._reserved_worker_slots, 0)
        self.assertEqual(cls._unconfirmed_abandoned, saved_unconfirmed)

    def test_timeout_full_frame_zero_dispatch(self):
        # R41-4：超时取消后完整管理帧才到达/放行——零 dispatch、零管理副作用。
        kernel = FakeK32(bp.encode_frame(bp.build_request("mgmt-k", "status")))
        kernel.gate = threading.Event()
        server = _server(self.temp.name, kernel, self.fx.coordinator)
        record = server.serve_once(deadline=time.monotonic() + 0.3)
        self.assertFalse(record["ok"])
        self.assertTrue(record.get("abandoned"))
        # 释放阻塞（模拟迟到完成）；worker 必须在 dispatch 前停止。
        kernel.gate.set()
        self.assertEqual(self.fx.ops.status_calls, 0)
        self.assertEqual(kernel.read_calls, 0)   # 迟到读取也不发生

    def test_old_cycle_never_touches_new_connection(self):
        # R41-4：跨 cycle 的 handle/cancel 所有权——cycle1 的迟到 worker 不
        # 可能触碰 cycle2 的连接（句柄按 cycle 独立且操作按句柄记账）。
        cls = srv.ManagementPipeServer
        saved = (cls._unconfirmed_abandoned, cls._reserved_worker_slots)
        kernel = FakeK32(b"")
        kernel.gate = threading.Event()          # 只阻塞第一个句柄的 connect
        server = _server(self.temp.name, kernel, self.fx.coordinator)
        record1 = server.serve_once(deadline=time.monotonic() + 0.3)
        self.assertFalse(record1["ok"])
        self.assertTrue(record1.get("abandoned"))
        handle1 = kernel.ops[0][0]               # cycle1 的连接句柄
        # 释放阻塞（fake CloseHandle 会放行）：迟到 worker 在 dispatch 前
        # 停止——旧句柄上绝不允许出现 read/write。
        kernel.gate.set()
        # cycle2：新句柄、新 cancel、完整成功交换。
        kernel.request_frame = bp.encode_frame(
            bp.build_request("mgmt-k", "status"))
        kernel.gate = None
        record2 = server.serve_once()
        self.assertTrue(record2["ok"], record2)
        handle2 = next(h for h, op in kernel.ops if h != handle1)
        ops1 = [op for h, op in kernel.ops if h == handle1]
        ops2 = [op for h, op in kernel.ops if h == handle2]
        self.assertNotIn("read", ops1)
        self.assertNotIn("write", ops1)
        for op in ("connect", "read", "write", "flush", "disconnect", "close"):
            self.assertEqual(ops2.count(op), 1, ops2)
        self.assertEqual(cls._reserved_worker_slots, saved[1])

    def test_connect_that_never_releases_is_not_claimed_reaped(self):
        # 不释放阻塞的 fake：未确认退出 → 永久占位计数 +1，不声称回收。
        class StuckK32(FakeK32):
            def CloseHandle(self, h):
                self.close_calls += 1      # 不释放阻塞（D4 反例 fake）
                return 1

        cls = srv.ManagementPipeServer
        kernel = StuckK32(b"")
        kernel.gate = threading.Event()
        saved = (cls._unconfirmed_abandoned, cls._reserved_worker_slots)
        server = _server(self.temp.name, kernel, self.fx.coordinator)
        record = server.serve_once(deadline=time.monotonic() + 0.3)
        self.assertFalse(record["ok"])
        self.assertTrue(record.get("abandoned"))
        # 永不释放的阻塞：一个 cycle 恰一个 worker、恰一个未确认占位
        # （不回收），预留额度回到进入前值。
        self.assertEqual(cls._unconfirmed_abandoned, saved[0] + 1)
        self.assertEqual(cls._reserved_worker_slots, saved[1])
        # 占位持续：后续 exchange 在 cap 内仍可取得额度（该计数 < cap=8）。
        kernel2 = FakeK32(bp.encode_frame(bp.build_request("mgmt-k",
                                                           "status")))
        server2 = _server(self.temp.name, kernel2, self.fx.coordinator)
        record = server2.serve_once()
        self.assertTrue(record["ok"], record)

    def test_admission_cap_refuses_before_worker_start(self):
        # cap=0：新 cycle 在启动 worker 前被拒（零 I/O、零创建）。
        cls = srv.ManagementPipeServer
        saved = (cls._MAX_UNCONFIRMED_WORKERS, cls._unconfirmed_abandoned,
                 cls._reserved_worker_slots)
        cls._MAX_UNCONFIRMED_WORKERS = 0
        cls._unconfirmed_abandoned = 0
        cls._reserved_worker_slots = 0
        try:
            kernel = FakeK32(b"")
            server = _server(self.temp.name, kernel, self.fx.coordinator)
            with self.assertRaises(OSError) as ctx:
                server.serve_once()
            self.assertIn("admission refused", str(ctx.exception))
            self.assertIsNone(kernel.created_args)   # 未创建任何实例
        finally:
            (cls._MAX_UNCONFIRMED_WORKERS, cls._unconfirmed_abandoned,
             cls._reserved_worker_slots) = saved

    def test_serve_forever_survives_poison_and_stops(self):
        # 单线程兜底循环（§27.5）：坏 cycle 被断开并继续服务；stop() 在
        # cycle 边界结束循环。
        kernel = FakeK32(bp.encode_frame(bp.build_request("mgmt-k",
                                                          "status")))
        server = _server(self.temp.name, kernel, self.fx.coordinator)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.15)
        server.stop()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(server._cycles, 1)

    def test_status_file_has_no_secrets(self):
        # 状态文件记录 pid/pipe/cycles 等，绝不包含控制密钥。
        state_dir = Path(self.temp.name) / "broker"
        state_dir.mkdir()
        kernel = FakeK32(bp.encode_frame(bp.build_request("mgmt-k",
                                                          "status")))
        server = _server(self.temp.name, kernel, self.fx.coordinator,
                         state_dir=state_dir)
        server.serve_once()
        payload = json.loads((state_dir / "broker-status.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(payload["pipe_name"], r"\\.\pipe\um-share-broker")
        self.assertIn("cycles", payload)
        self.assertNotIn("mgmt-k", json.dumps(payload))


class LifecycleRecordTests(unittest.TestCase):
    """Watchdog-coordination lifecycle record: the Protected broker record
    flips to 'stopped' ONLY on a VERIFIED drain/emergency-stop closure and
    back to 'running' on a verified start; it is persisted in the broker
    dir (restored across broker restarts) and exposed in broker-status.json
    for the watchdog - never derived from a request's intent."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _server(self, state_dir=None):
        fixture = _ServerFixture(self.temp.name)
        calls = []

        def inst_call(action, fields, instance_id):
            calls.append(action)
            if action == "instance-status":
                return {"ok": True,
                        "result": {"instance_id": instance_id,
                                   "active_count": 0}}
            return {"ok": True, "result": {
                "instance_id": instance_id,
                "generation": fields.get("generation"),
                "fingerprint": fields.get("fingerprint"),
                "installed": True, "state": "stopped"}}

        coord = BrokerHostCoordinator(
            writer=fixture.writer, mgmt_secret="mgmt-k",
            instance_secret="inst-k", instances=fixture.instances,
            replay=bp.ReplayRegistry(), host_ops=fixture.ops,
            instance_call=inst_call, drain_timeout=0.2)
        server = srv.ManagementPipeServer(
            coord, r"\\.\pipe\um-share-broker", deadline_seconds=5.0,
            state_dir=state_dir)
        return server, calls

    def _status_payload(self, state_dir):
        # 用一个 status 请求内核跑一轮 serve_once，读回状态文件。
        fixture = _ServerFixture(self.temp.name)
        kernel = FakeK32(bp.encode_frame(bp.build_request("mgmt-k",
                                                          "status")))
        state_server = srv.ManagementPipeServer(
            fixture.coordinator, r"\\.\pipe\um-share-broker",
            kernel=(kernel, None), deadline_seconds=5.0, state_dir=state_dir)
        state_server.serve_once()
        return json.loads((state_dir / "broker-status.json")
                          .read_text(encoding="utf-8"))

    def test_default_lifecycle_running_in_status(self):
        state_dir = Path(self.temp.name) / "broker"
        state_dir.mkdir()
        server, _ = self._server(state_dir=state_dir)
        self.assertEqual(server.lifecycle["state"], "running")
        payload = self._status_payload(state_dir)
        self.assertEqual(payload["lifecycle"]["state"], "running")

    def test_drain_verified_records_stopped_and_persists(self):
        state_dir = Path(self.temp.name) / "broker"
        state_dir.mkdir()
        server, _ = self._server(state_dir=state_dir)
        kernel = FakeK32(bp.encode_frame(
            bp.build_request("mgmt-k", "drain")))
        server3 = srv.ManagementPipeServer(
            server.coordinator, r"\\.\pipe\um-share-broker",
            kernel=(kernel, None), deadline_seconds=5.0, state_dir=state_dir)
        record = server3.serve_once()
        self.assertTrue(record["ok"], record)
        self.assertEqual(server3.lifecycle["state"], "stopped")
        self.assertEqual(server3.lifecycle["by"], "drain")
        persisted = json.loads(
            (state_dir / "lifecycle.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["state"], "stopped")
        payload = json.loads((state_dir / "broker-status.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(payload["lifecycle"]["state"], "stopped")

    def test_emergency_verified_records_stopped(self):
        state_dir = Path(self.temp.name) / "broker"
        state_dir.mkdir()
        server, _ = self._server(state_dir=state_dir)
        kernel = FakeK32(bp.encode_frame(
            bp.build_request("mgmt-k", "emergency-stop")))
        server3 = srv.ManagementPipeServer(
            server.coordinator, r"\\.\pipe\um-share-broker",
            kernel=(kernel, None), deadline_seconds=5.0, state_dir=state_dir)
        record = server3.serve_once()
        self.assertTrue(record["ok"], record)
        self.assertEqual(server3.lifecycle["state"], "stopped")
        self.assertEqual(server3.lifecycle["by"], "emergency-stop")

    def test_start_verified_records_running(self):
        state_dir = Path(self.temp.name) / "broker"
        state_dir.mkdir()
        server, _ = self._server(state_dir=state_dir)
        kernel = FakeK32(bp.encode_frame(bp.build_request("mgmt-k", "start")))
        server3 = srv.ManagementPipeServer(
            server.coordinator, r"\\.\pipe\um-share-broker",
            kernel=(kernel, None), deadline_seconds=5.0, state_dir=state_dir)
        record = server3.serve_once()
        self.assertTrue(record["ok"], record)
        self.assertEqual(server3.lifecycle["state"], "running")
        self.assertEqual(server3.lifecycle["by"], "start")

    def test_verified_start_flips_a_stopped_lifecycle_to_running(self):
        # C2：先前 emergency-stop 的受保护记录是 stopped，随后一次
        # VERIFIED 受管 start 必须把它刷成 running（并持久化），监督判据
        # 才不再以 managed_stop_in_force 拦下；只读观察绝不写这个状态。
        state_dir = Path(self.temp.name) / "broker"
        state_dir.mkdir()
        (state_dir / "lifecycle.json").write_text(
            json.dumps({"state": "stopped", "at": 1234,
                        "by": "emergency-stop"}), encoding="utf-8")
        server, _ = self._server(state_dir=state_dir)
        self.assertEqual(server.lifecycle["state"], "stopped")
        kernel = FakeK32(bp.encode_frame(bp.build_request("mgmt-k", "start")))
        server3 = srv.ManagementPipeServer(
            server.coordinator, r"\\.\pipe\um-share-broker",
            kernel=(kernel, None), deadline_seconds=5.0, state_dir=state_dir)
        record = server3.serve_once()
        self.assertTrue(record["ok"], record)
        self.assertEqual(server3.lifecycle["state"], "running")
        self.assertEqual(server3.lifecycle["by"], "start")
        persisted = json.loads(
            (state_dir / "lifecycle.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["state"], "running")
        self.assertEqual(persisted["by"], "start")
        payload = json.loads((state_dir / "broker-status.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(payload["lifecycle"]["state"], "running")

    def _unpersistable(self, state_dir):
        """Make the atomic lifecycle write fail (tmp path occupied by a dir)."""
        (state_dir / "lifecycle.json.tmp").mkdir()

    def test_start_with_unpersistable_lifecycle_is_unconfirmed(self):
        # C2 返修：受保护写入失败时，VERIFIED start 不得报成功——否则内存为
        # running、磁盘仍 stopped，重载后又静默回退成停止。
        state_dir = Path(self.temp.name) / "broker"
        state_dir.mkdir()
        (state_dir / "lifecycle.json").write_text(
            json.dumps({"state": "stopped", "at": 1234,
                        "by": "emergency-stop"}), encoding="utf-8")
        self._unpersistable(state_dir)
        server, _ = self._server(state_dir=state_dir)
        self.assertEqual(server.lifecycle["state"], "stopped")
        kernel = FakeK32(bp.encode_frame(bp.build_request("mgmt-k", "start")))
        server3 = srv.ManagementPipeServer(
            server.coordinator, r"\\.\pipe\um-share-broker",
            kernel=(kernel, None), deadline_seconds=5.0, state_dir=state_dir)
        server3.serve_once()
        reply = json.loads(bytes(kernel.written))
        self.assertFalse(reply["ok"], reply)
        self.assertEqual(reply["error_code"], "unconfirmed")
        self.assertIn("lifecycle", reply["result"]["detail"])
        # 内存不分叉：仍是加载时的 stopped，而不是「内存 running / 磁盘 stopped」。
        self.assertEqual(server3.lifecycle["state"], "stopped")
        self.assertEqual(server3.lifecycle["by"], "emergency-stop")
        reloaded = srv.ManagementPipeServer(
            server.coordinator, r"\\.\pipe\um-share-broker",
            deadline_seconds=5.0, state_dir=state_dir)
        self.assertEqual(reloaded.lifecycle["state"], "stopped")

    def test_drain_with_unpersistable_lifecycle_is_unconfirmed(self):
        # 停止方向的同一契约：受保护记录写不进去时，回复未确认、内存保持
        # running（磁盘上的权威值），重载不会静默变成 stopped。
        state_dir = Path(self.temp.name) / "broker"
        state_dir.mkdir()
        (state_dir / "lifecycle.json").write_text(
            json.dumps({"state": "running", "at": 1, "by": "start"}),
            encoding="utf-8")
        self._unpersistable(state_dir)
        server, _ = self._server(state_dir=state_dir)
        kernel = FakeK32(bp.encode_frame(bp.build_request("mgmt-k", "drain")))
        server3 = srv.ManagementPipeServer(
            server.coordinator, r"\\.\pipe\um-share-broker",
            kernel=(kernel, None), deadline_seconds=5.0, state_dir=state_dir)
        server3.serve_once()
        reply = json.loads(bytes(kernel.written))
        self.assertFalse(reply["ok"], reply)
        self.assertEqual(reply["error_code"], "unconfirmed")
        self.assertEqual(server3.lifecycle["state"], "running")
        self.assertEqual(server3.lifecycle["by"], "start")
        reloaded = srv.ManagementPipeServer(
            server.coordinator, r"\\.\pipe\um-share-broker",
            deadline_seconds=5.0, state_dir=state_dir)
        self.assertEqual(reloaded.lifecycle["state"], "running")

    def test_lifecycle_restored_from_protected_file(self):
        # 持久化记录跨 broker 重启保持：先停再重启仍是 stopped。
        state_dir = Path(self.temp.name) / "broker"
        state_dir.mkdir()
        (state_dir / "lifecycle.json").write_text(
            json.dumps({"state": "stopped", "at": 1234, "by": "drain"}),
            encoding="utf-8")
        server, _ = self._server(state_dir=state_dir)
        self.assertEqual(server.lifecycle["state"], "stopped")
        self.assertEqual(server.lifecycle["at"], 1234)

    def test_lifecycle_malformed_falls_back_to_running(self):
        # 损坏/缺字段记录回退到 fail-safe 默认（running）——绝不当作已确认停止。
        state_dir = Path(self.temp.name) / "broker"
        state_dir.mkdir()
        (state_dir / "lifecycle.json").write_text("not-json{",
                                                  encoding="utf-8")
        server, _ = self._server(state_dir=state_dir)
        self.assertEqual(server.lifecycle["state"], "running")
        (state_dir / "lifecycle.json").write_text(
            json.dumps({"state": "bogus", "at": 5}), encoding="utf-8")
        server2, _ = self._server(state_dir=state_dir)
        self.assertEqual(server2.lifecycle["state"], "running")


class ServerHelperTests(unittest.TestCase):
    def test_read_control_key_failures(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "broker-control-key"
            path.write_text("", encoding="utf-8")
            if os.name == "posix":
                os.chmod(path, 0o600)
            with self.assertRaises(ValueError):
                srv.read_control_key(path)
            path.write_text("x" * 200, encoding="utf-8")
            if os.name == "posix":
                os.chmod(path, 0o600)
            with self.assertRaises(ValueError):
                srv.read_control_key(path)
            path.write_text("k" * 32 + "\n", encoding="utf-8")
            if os.name == "posix":
                os.chmod(path, 0o600)
            self.assertEqual(srv.read_control_key(path), "k" * 32)

    def test_load_registration_explicit_not_ready(self):
        # R41-1：registration 缺失/损坏 = 显式未就绪（None），绝不伪造
        # instance id；带 instance_id 的合法记录原样返回。
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "instance-registration.json"
            self.assertIsNone(srv.load_registration(path))
            path.write_text("not-json", encoding="utf-8")
            self.assertIsNone(srv.load_registration(path))
            path.write_text(json.dumps({"pid": 7}), encoding="utf-8")
            self.assertIsNone(srv.load_registration(path))
            path.write_text(json.dumps({"instance_id": "i-9", "pid": 7}),
                            encoding="utf-8")
            self.assertEqual(srv.load_registration(path)["instance_id"],
                             "i-9")


class _InstanceFacadeIdentityKernel:
    """Fake Win32 facade for the verifier's real decision path in the
    assembly closed loop (image/owner/creation deterministic)."""

    def __init__(self, image, owner_sid):
        self.image = image
        self.owner_sid = owner_sid
        self._sid_buffers = []

    def __getitem__(self, key):
        k = self
        import ctypes
        if key == "kernel32":
            class K32:
                def OpenProcess(self, access, inherit, pid):
                    return 0x4000

                def QueryFullProcessImageNameW(self, h, flags, buf, size):
                    buf.value = k.image
                    size[0] = len(k.image)
                    return 1

                def GetProcessTimes(self, h, c, e, ki, u):
                    c.contents.dwLowDateTime = 100
                    c.contents.dwHighDateTime = 0
                    return 1

                def CloseHandle(self, h):
                    return 1
            return K32()
        if key == "advapi32":
            class ADV:
                def OpenProcessToken(self, h, access, token):
                    token[0] = 0x3000
                    return 1

                def GetTokenInformation(self, token, cls, buf, length,
                                        needed):
                    if buf is None:
                        needed[0] = 64
                        return 0
                    ctypes.memmove(buf, b"\xef\xbe\xad\xde\x00\x00\x00\x00",
                                   8)
                    return 1

                def ConvertSidToStringSidW(self, sid, out):
                    buf = ctypes.create_unicode_buffer(k.owner_sid)
                    k._sid_buffers.append(buf)
                    out[0] = ctypes.cast(buf, ctypes.c_wchar_p)
                    return 1
            return ADV()
        raise KeyError(key)


class _InstanceClientKernel:
    """Fake kernel32 for the instance-pipe CLIENT: forwards the signed
    instance frame to the REAL ShareServer.handle_instance_control and
    returns the REAL encoded response. ``fail_pipes`` makes CreateFileW
    fail (INVALID_HANDLE_VALUE) for selected pipe names - the R44-2 A->B
    restart and forged-candidate cases exercise the discovery refresh
    against a dead candidate connection."""

    def __init__(self, share, instance_secret):
        self.share = share
        self.instance_secret = instance_secret
        self.request_bodies = []
        self.fail_pipes = set()
        self.open_calls = 0

    def CreateFileW(self, name, *a):
        self.open_calls += 1
        if name in self.fail_pipes:
            raise OSError("instance pipe not available: " + name)
        return 0x1000

    def WriteFile(self, h, data, n, written, o):
        self.request_bodies.append(
            json.loads(bytes(data[:n]).decode("utf-8")))
        written.contents.value = n
        return 1

    def ReadFile(self, h, buf, n, read, o):
        req = self.request_bodies[-1]
        resp = self.share.handle_instance_control(req, self.instance_secret)
        payload = (json.dumps(resp, sort_keys=True) + "\n").encode("utf-8")
        buf[:len(payload)] = payload
        read.contents.value = len(payload)
        return 1

    def CloseHandle(self, h):
        return 1

    def GetLastError(self):
        return 0

    def GetNamedPipeServerProcessId(self, h, p):
        p.contents.value = 4242
        return 1


class AssemblyClosedLoopTests(unittest.TestCase):
    """R41-1/R41-2 端到端装配闭环（离线）：build_server → 真实签名管理
    请求 → 真实 ProtectedProjectionWriter 落盘 → make_instance_call 真实
    协议签名帧（带 instance_id）→ verifier 信任链核验 → 真实
    handle_instance_control 校验并 ACK → 协调器消费 ACK → ok。"""

    def test_build_server_publish_via_real_instance_protocol(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            config = run / "um-share-projection.json"
            config.write_text(json.dumps(_projection(generation=1)),
                              encoding="utf-8")
            share = ShareServer(("127.0.0.1", 0), config,
                                auth_mode="projection")
            self.addCleanup(share.server_close)
            (run / "broker").mkdir()
            key_file = run / "broker" / "broker-control-key"
            _write_secret(key_file, "mgmt-k")
            instance_key = run / "instance-channel-key"
            _write_secret(instance_key, "inst-k")
            registration = run / "run" / "live" / "instance-registration.json"
            registration.parent.mkdir(parents=True)
            registration.write_text(json.dumps({
                "instance_id": share.instance_id,
                "pipe_name": r"\\.\pipe\um-share-instance-" + share.instance_id,
                "pid": 4242}), encoding="utf-8")

            image = r"C:\ProgramData\UMShareRun\um-share-python.exe"
            verifier = WindowsInstanceVerifier(
                facade=_InstanceFacadeIdentityKernel(image, "S-1-5-21-1001"))
            args = argparse.Namespace(
                broker_dir=str(run / "broker"), key_file=str(key_file),
                instance_key_file=str(instance_key),
                registration_file=str(registration),
                pipe_name=r"\\.\pipe\um-share-broker", sddl=None,
                projection_path=str(config), share_task="umshare-live",
                expect_image=image, expect_owner_sid="S-1-5-21-1001",
                deadline=5.0)
            client_kernel = _InstanceClientKernel(share, "inst-k")
            server = srv.build_server(
                args, kernel=(None, None), verifier=verifier,
                host_ops=RecordingOps(), acl_checker=lambda path: None,
                instance_kernel_factory=lambda: client_kernel)

            projection = _projection(generation=2)
            request = bp.encode_frame(bp.build_request(
                "mgmt-k", "publish",
                {"generation": 2,
                 "fingerprint": server.coordinator.writer.fingerprint(
                     projection),
                 "projection": projection}))
            mgmt_kernel = FakeK32(request)
            server._kernel_pair = (mgmt_kernel, None)
            record = server.serve_once()
            self.assertTrue(record["ok"], record)
            response = json.loads(bytes(mgmt_kernel.written))
            self.assertTrue(response["ok"], response)
            self.assertTrue(response["result"]["installed"])
            # ACK 内容绑定 generation/fingerprint/instance。
            self.assertEqual(response["result"]["generation"], 2)
            self.assertEqual(response["result"]["instance_id"],
                             share.instance_id)
            # 实例签名帧确实携带 instance_id（管道名只是寻址）。
            self.assertEqual(client_kernel.request_bodies[0]["instance_id"],
                             share.instance_id)
            # 分享实例真实安装了新投影（watermark 前进）。
            self.assertEqual(share.last_seen_generation, 2)
            # R41-2: 同一代次重复连接不 bump 登记（R31-3 门禁保持）。
            self.assertEqual(server.coordinator.instances.current(),
                             share.instance_id)

    def test_forged_registration_alone_cannot_serve_load(self):
        # 伪造登记文件 ≠ 可信实例：verifier 拒绝对端身份 → publish 拿到
        # unconfirmed（实例 load 失败），投影不被 ACK。
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            config = run / "um-share-projection.json"
            config.write_text(json.dumps(_projection(generation=1)),
                              encoding="utf-8")
            share = ShareServer(("127.0.0.1", 0), config,
                                auth_mode="projection")
            self.addCleanup(share.server_close)
            (run / "broker").mkdir()
            key_file = run / "broker" / "broker-control-key"
            _write_secret(key_file, "mgmt-k")
            instance_key = run / "instance-channel-key"
            _write_secret(instance_key, "inst-k")
            registration = run / "instance-registration.json"
            # 伪造登记：指向一个 verifier 无法核验的假身份。
            registration.write_text(json.dumps({
                "instance_id": share.instance_id, "pid": 4242}),
                encoding="utf-8")
            image = r"C:\ProgramData\UMShareRun\um-share-python.exe"
            verifier = WindowsInstanceVerifier(
                facade=_InstanceFacadeIdentityKernel(
                    r"C:\Windows\System32\cmd.exe", "S-1-5-21-1001"))
            args = argparse.Namespace(
                broker_dir=str(run / "broker"), key_file=str(key_file),
                instance_key_file=str(instance_key),
                registration_file=str(registration),
                pipe_name=r"\\.\pipe\um-share-broker", sddl=None,
                projection_path=str(config), share_task="umshare-live",
                expect_image=image, expect_owner_sid="S-1-5-21-1001",
                deadline=5.0)
            client_kernel = _InstanceClientKernel(share, "inst-k")
            server = srv.build_server(
                args, kernel=(None, None), verifier=verifier,
                host_ops=RecordingOps(), acl_checker=lambda path: None,
                instance_kernel_factory=lambda: client_kernel)
            projection = _projection(generation=2)
            request = bp.encode_frame(bp.build_request(
                "mgmt-k", "publish",
                {"generation": 2,
                 "fingerprint": server.coordinator.writer.fingerprint(
                     projection),
                 "projection": projection}))
            mgmt_kernel = FakeK32(request)
            server._kernel_pair = (mgmt_kernel, None)
            record = server.serve_once()
            self.assertTrue(record["ok"], record)
            response = json.loads(bytes(mgmt_kernel.written))
            self.assertFalse(response["ok"], response)
            self.assertEqual(response["error_code"], "unconfirmed")
            # 分享实例从未收到 load（verifier 先于任何请求拒绝）。
            self.assertEqual(client_kernel.request_bodies, [])

    def _assembly(self, run, share, registration_path, verifier,
                  client_kernel):
        (run / "broker").mkdir(exist_ok=True)
        key_file = run / "broker" / "broker-control-key"
        _write_secret(key_file, "mgmt-k")
        instance_key = run / "instance-channel-key"
        _write_secret(instance_key, "inst-k")
        image = r"C:\ProgramData\UMShareRun\um-share-python.exe"
        args = argparse.Namespace(
            broker_dir=str(run / "broker"), key_file=str(key_file),
            instance_key_file=str(instance_key),
            registration_file=str(registration_path),
            pipe_name=r"\\.\pipe\um-share-broker", sddl=None,
            projection_path=str(run / "um-share-projection.json"),
            share_task="umshare-live", share_port=8100,
            expect_image=image, expect_owner_sid="S-1-5-21-1001",
            deadline=5.0)
        return srv.build_server(
            args, kernel=(None, None), verifier=verifier,
            host_ops=RecordingOps(), acl_checker=lambda path: None,
            instance_kernel_factory=lambda: client_kernel)

    @staticmethod
    def _serve_publish(server, projection):
        request = bp.encode_frame(bp.build_request(
            "mgmt-k", "publish",
            {"generation": projection["config_generation"],
             "fingerprint": server.coordinator.writer.fingerprint(projection),
             "projection": projection}))
        mgmt_kernel = FakeK32(request)
        server._kernel_pair = (mgmt_kernel, None)
        record = server.serve_once()
        response = json.loads(bytes(mgmt_kernel.written))
        return record, response

    def test_broker_first_share_registers_later(self):
        # R44-2：broker 先启动（无登记文件）→ share 稍后自发布 → publish 的
        # 受控发现阶段读取候选 → 核验成功后进入受信任 registry → ACK ok。
        # 无需重启 broker 或手工改其内存状态。
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            config = run / "um-share-projection.json"
            config.write_text(json.dumps(_projection(generation=1)),
                              encoding="utf-8")
            share = ShareServer(("127.0.0.1", 0), config,
                                auth_mode="projection")
            self.addCleanup(share.server_close)
            registration = run / "run" / "live" / "instance-registration.json"
            registration.parent.mkdir(parents=True)
            image = r"C:\ProgramData\UMShareRun\um-share-python.exe"
            verifier = WindowsInstanceVerifier(
                facade=_InstanceFacadeIdentityKernel(image, "S-1-5-21-1001"))
            client_kernel = _InstanceClientKernel(share, "inst-k")
            server = self._assembly(run, share, registration, verifier,
                                    client_kernel)
            # broker 启动时登记不存在：registry 为空、显式未就绪。
            self.assertIsNone(server.coordinator.instances.current())
            # share 稍后写入登记。
            registration.write_text(json.dumps({
                "instance_id": share.instance_id,
                "pipe_name": r"\\.\pipe\um-share-instance-"
                             + share.instance_id}), encoding="utf-8")
            projection = _projection(generation=2)
            record, response = self._serve_publish(server, projection)
            self.assertTrue(record["ok"], record)
            self.assertTrue(response["ok"], response)
            self.assertEqual(response["result"]["instance_id"],
                             share.instance_id)
            self.assertEqual(share.last_seen_generation, 2)
            self.assertEqual(server.coordinator.instances.current(),
                             share.instance_id)

    def test_restarted_instance_recovered_by_discovery(self):
        # R44-2：A 重启为 B —— 旧 A 的管道不可达（连接失败），登记文件已是
        # B；publish 失败后一次受控刷新 → B 核验成功并登记换代 → 重试 ACK ok。
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            config = run / "um-share-projection.json"
            config.write_text(json.dumps(_projection(generation=1)),
                              encoding="utf-8")
            share_b = ShareServer(("127.0.0.1", 0), config,
                                  auth_mode="projection")
            self.addCleanup(share_b.server_close)
            registration = run / "instance-registration.json"
            # 初始登记指向已死的 A（管道名不可打开）。
            stale_a = "stale-instance-a"
            registration.write_text(json.dumps({
                "instance_id": stale_a,
                "pipe_name": r"\\.\pipe\um-share-instance-" + stale_a}),
                encoding="utf-8")
            image = r"C:\ProgramData\UMShareRun\um-share-python.exe"
            verifier = WindowsInstanceVerifier(
                facade=_InstanceFacadeIdentityKernel(image, "S-1-5-21-1001"))
            client_kernel = _InstanceClientKernel(share_b, "inst-k")
            client_kernel.fail_pipes = {
                r"\\.\pipe\um-share-instance-" + stale_a}
            server = self._assembly(run, share_b, registration, verifier,
                                    client_kernel)
            # R46.5：登记文件只是寻址候选——未核验的候选不占用受信任
            # registry（空 registry，显式未就绪）。
            self.assertIsNone(server.coordinator.instances.current())
            # A 重启为 B：登记文件自发布更新为 B（A 的管道已不可达）。
            registration.write_text(json.dumps({
                "instance_id": share_b.instance_id,
                "pipe_name": r"\\.\pipe\um-share-instance-"
                             + share_b.instance_id}), encoding="utf-8")
            projection = _projection(generation=2)
            record, response = self._serve_publish(server, projection)
            self.assertTrue(record["ok"], record)
            self.assertTrue(response["ok"], response)
            self.assertEqual(response["result"]["instance_id"],
                             share_b.instance_id)
            self.assertEqual(share_b.last_seen_generation, 2)
            self.assertEqual(server.coordinator.instances.current(),
                             share_b.instance_id)

    def test_forged_candidate_never_registers(self):
        # R44-2：伪造候选（管道不可达）→ 核验无法发生 → 受信任 registry
        # 不变，publish 明确 unconfirmed。
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            config = run / "um-share-projection.json"
            config.write_text(json.dumps(_projection(generation=1)),
                              encoding="utf-8")
            share = ShareServer(("127.0.0.1", 0), config,
                                auth_mode="projection")
            self.addCleanup(share.server_close)
            registration = run / "instance-registration.json"
            forged = "forged-candidate-id"
            registration.write_text(json.dumps({
                "instance_id": forged,
                "pipe_name": r"\\.\pipe\um-share-instance-" + forged}),
                encoding="utf-8")
            image = r"C:\ProgramData\UMShareRun\um-share-python.exe"
            verifier = WindowsInstanceVerifier(
                facade=_InstanceFacadeIdentityKernel(image, "S-1-5-21-1001"))
            client_kernel = _InstanceClientKernel(share, "inst-k")
            client_kernel.fail_pipes = {
                r"\\.\pipe\um-share-instance-" + forged}
            server = self._assembly(run, share, registration, verifier,
                                    client_kernel)
            # R46.5：登记文件只是寻址候选——未核验的候选不占用受信任
            # registry（空 registry，显式未就绪）；伪造 id 只会产生一个
            # 连不上的管道名，受信任 registry 从头到尾为空。
            self.assertIsNone(server.coordinator.instances.current())
            projection = _projection(generation=2)
            record, response = self._serve_publish(server, projection)
            self.assertTrue(record["ok"], record)
            self.assertFalse(response["ok"], response)
            self.assertEqual(response["error_code"], "unconfirmed")
            self.assertEqual(client_kernel.request_bodies, [])
            # R46.5 新语义：伪造候选全程不占用受信任 registry——零签名请求
            # 发出、publish 明确 unconfirmed、registry 始终为空。
            self.assertIsNone(server.coordinator.instances.current())

    def test_production_assembly_refuses_missing_expectations(self):
        # R44-1：真实装配（无注入 verifier）缺少安全期望时拒绝组装——不能
        # 依赖 SDDL 占位恰好挡住启动。
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            registration = run / "instance-registration.json"
            registration.write_text(json.dumps({"instance_id": "i-1"}),
                                    encoding="utf-8")
            (run / "broker").mkdir()
            key_file = run / "broker" / "broker-control-key"
            _write_secret(key_file, "mgmt-k")
            instance_key = run / "instance-channel-key"
            _write_secret(instance_key, "inst-k")
            config = run / "um-share-projection.json"
            config.write_text(json.dumps(_projection(generation=1)),
                              encoding="utf-8")
            args = argparse.Namespace(
                broker_dir=str(run / "broker"), key_file=str(key_file),
                instance_key_file=str(instance_key),
                registration_file=str(registration),
                pipe_name=r"\\.\pipe\um-share-broker", sddl=None,
                projection_path=str(config), share_task="umshare-live",
                share_port=8100, expect_image=None, expect_owner_sid=None,
                deadline=5.0)
            with self.assertRaises(ValueError) as ctx:
                srv.build_server(args)
            self.assertIn("identity expectations missing", str(ctx.exception))

    def test_production_host_ops_identity_wiring(self):
        # R51.4：不注入 host_ops → 走真实 WindowsHostOps 构造——身份期望
        # 必须接线（expected_image_path = --expect-image 完整路径；
        # expected_owner = DOMAIN\\user 形态），缺 owner 配置拒装。
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            registration = run / "instance-registration.json"
            registration.write_text(json.dumps({"instance_id": "i-1"}),
                                    encoding="utf-8")
            (run / "broker").mkdir()
            key_file = run / "broker" / "broker-control-key"
            _write_secret(key_file, "mgmt-k")
            instance_key = run / "instance-channel-key"
            _write_secret(instance_key, "inst-k")
            config = run / "um-share-projection.json"
            config.write_text(json.dumps(_projection(generation=1)),
                              encoding="utf-8")
            image = r"C:\ProgramData\UMShareRun\um-share-python.exe"
            base = dict(
                broker_dir=str(run / "broker"), key_file=str(key_file),
                instance_key_file=str(instance_key),
                registration_file=str(registration),
                pipe_name=r"\\.\pipe\um-share-broker", sddl=None,
                projection_path=str(config), share_task="umshare-live",
                share_port=8100, expect_image=image,
                expect_owner_sid="S-1-5-21-1001", deadline=5.0)
            # 缺 owner account → 拒装。
            with self.assertRaises(ValueError) as ctx:
                srv.build_server(argparse.Namespace(**base))
            self.assertIn("host owner expectation missing",
                          str(ctx.exception))
            # 提供 DOMAIN\\user 形态 → 接线进 host_ops。
            args = argparse.Namespace(**dict(
                base, expected_owner_account="MACHINE\\dacint"))
            server = srv.build_server(
                args, kernel=(None, None),
                instance_kernel_factory=lambda: _InstanceClientKernel(
                    None, "inst-k"))
            host_ops = server.coordinator.host_ops
            self.assertEqual(host_ops.expected_image_path, image)
            self.assertEqual(host_ops.expected_owner, "MACHINE\\dacint")


class _FakeTsOps:
    """受限 TS 服务原语替身：记录调用、可编程裁定（含 R13-T 的被拒 rc）。"""

    def __init__(self, result=True, last_control_rc=None):
        self.calls = []
        self._result = result
        self.last_control_rc = last_control_rc

    def start(self):
        self.calls.append("start")
        return self._result

    def stop(self):
        self.calls.append("stop")
        return self._result


class TsServiceControlTests(unittest.TestCase):
    """受限 ``ts-service-control`` 动词（白名单扩展）：签名/MAC/重放消费
    沿用现有校验管道（同一协调器密钥引用、同一重放注册表，零第二份校验
    逻辑）；服务名不在线上（字段白名单拒绝注入）；裁定 = 复核状态，非
    True 即 unconfirmed；其余管理动词原样委派不受影响。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.fx = _ServerFixture(self.temp.name)
        self.ts = _FakeTsOps(True)
        self.fx.coordinator = srv._TsServiceControlCoordinator(
            self.fx.coordinator, self.ts)

    def _serve(self, request):
        kernel = FakeK32(bp.encode_frame(request))
        server = _server(self.temp.name, kernel, self.fx.coordinator)
        record = server.serve_once()
        self.assertTrue(record["ok"], record)
        return json.loads(bytes(kernel.written))

    def test_signed_start_confirms_with_explicit_flag(self):
        request = bp.build_request("mgmt-k", "ts-service-control",
                                   {"action": "start"})
        response = self._serve(request)
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["request_id"], request["request_id"])
        self.assertIs(response["result"]["ts_service_started"], True)
        self.assertEqual(self.ts.calls, ["start"])

    def test_signed_stop_unconfirmed_refuses(self):
        self.ts._result = False
        request = bp.build_request("mgmt-k", "ts-service-control",
                                   {"action": "stop"})
        response = self._serve(request)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error_code"], "unconfirmed")
        self.assertEqual(response["request_id"], request["request_id"])
        self.assertEqual(self.ts.calls, ["stop"])

    def test_access_denied_rc_is_a_distinct_refusal_not_unconfirmed(self):
        # R13-T: sc.exe rc 5 (ERROR_ACCESS_DENIED) is a PERMANENT refusal -
        # the command never executed, so folding it into the generic
        # "did not re-confirm" unconfirmed shape would send the console into
        # a doomed pending-confirmation poll. The broker reads the rejected
        # rc off the ops and answers the DISTINCT access_denied code; the
        # identity/replay pipeline stays the existing one.
        self.ts._result = False
        self.ts.last_control_rc = 5
        request = bp.build_request("mgmt-k", "ts-service-control",
                                   {"action": "start"})
        response = self._serve(request)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error_code"], "access_denied")
        self.assertIn("denied", (response["result"].get("detail") or ""))
        self.assertEqual(self.ts.calls, ["start"])

    def test_pending_rc_stays_the_unconfirmed_shape(self):
        # A rejected rc that is NOT access denied (e.g. the service never
        # re-confirmed because it sat in START_PENDING) keeps the existing
        # unconfirmed reply - the console pending poll decides.
        self.ts._result = False
        self.ts.last_control_rc = 0
        request = bp.build_request("mgmt-k", "ts-service-control",
                                   {"action": "start"})
        response = self._serve(request)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error_code"], "unconfirmed")
        self.assertIn("did not re-confirm",
                      (response["result"].get("detail") or ""))

    def test_enum_and_extra_fields_refused_before_any_control(self):
        for fields in ({"action": "restart"},                      # 非法枚举
                       {"action": "start", "service": "LanmanWorkstation"}):
            request = bp.build_request("mgmt-k", "ts-service-control", fields)
            response = self._serve(request)
            self.assertFalse(response["ok"], response)
            self.assertEqual(response["error_code"], "bad_request")
        self.assertEqual(self.ts.calls, [])

    def test_forged_mac_refused(self):
        request = bp.build_request("wrong-key", "ts-service-control",
                                   {"action": "start"})
        response = self._serve(request)
        self.assertFalse(response["ok"], response)
        self.assertEqual(response["error_code"], "forbidden")
        self.assertEqual(self.ts.calls, [])

    def test_replay_consumed_through_the_same_registry(self):
        request = bp.build_request("mgmt-k", "ts-service-control",
                                   {"action": "stop"})
        first = self._serve(request)
        self.assertTrue(first["ok"], first)
        second = self._serve(request)             # 同一 request_id 重放
        self.assertFalse(second["ok"], second)
        self.assertEqual(second["error_code"], "replay")
        self.assertEqual(self.ts.calls, ["stop"])
        # 重放拒绝先于任何副作用：原语只被调了一次。

    def test_other_verbs_delegate_untouched(self):
        request = bp.build_request("mgmt-k", "status")
        response = self._serve(request)
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["host"], "fixture")
        self.assertEqual(self.fx.ops.status_calls, 1)
        self.assertEqual(self.ts.calls, [])

    def test_ts_state_never_touches_share_lifecycle(self):
        # ts 动词不写分享生命周期记录（那是 drain/emergency-stop/start 的
        # VERIFIED 结果专用语义）。
        with tempfile.TemporaryDirectory() as td:
            request = bp.build_request("mgmt-k", "ts-service-control",
                                       {"action": "stop"})
            kernel = FakeK32(bp.encode_frame(request))
            server = _server(self.temp.name, kernel, self.fx.coordinator,
                             state_dir=td)
            server.lifecycle = {"state": "running", "at": 1, "by": "startup"}
            record = server.serve_once()
            self.assertTrue(record["ok"], record)
            self.assertEqual(server.lifecycle, {"state": "running", "at": 1,
                                                "by": "startup"})
            written = json.loads((Path(td) / "broker-status.json")
                                 .read_text(encoding="utf-8"))
            self.assertEqual(written["lifecycle"]["state"], "running")


class BuildServerTsExtensionTests(unittest.TestCase):
    """build_server 装配：ts-service-control 扩展以调度装饰器挂在真实
    协调器外层，属性转发保持既有消费面（coordinator.writer/...），注入的
    受限原语接到新动词。"""

    def test_build_server_wires_ts_extension_with_attribute_forwarding(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            config = run / "um-share-projection.json"
            config.write_text(json.dumps(_projection()), encoding="utf-8")
            (run / "broker").mkdir()
            key_file = run / "broker" / "broker-control-key"
            _write_secret(key_file, "mgmt-k")
            instance_key = run / "instance-channel-key"
            _write_secret(instance_key, "inst-k")
            ts = _FakeTsOps(True)
            args = argparse.Namespace(
                broker_dir=str(run / "broker"), key_file=str(key_file),
                instance_key_file=str(instance_key),
                registration_file=str(run / "instance-registration.json"),
                pipe_name=r"\\.\pipe\um-share-broker", sddl=None,
                projection_path=str(config), share_task="umshare-live",
                share_port=8100, expect_image="x.exe",
                expect_owner_sid="S-1-5-21-1", deadline=5.0)
            verifier = WindowsInstanceVerifier()
            server = srv.build_server(
                args, kernel=(None, None), verifier=verifier,
                host_ops=RecordingOps(), acl_checker=lambda path: None,
                ts_service_ops=ts)
            # 装配后的协调器是双层扩展装饰器（宿主证据在外、ts 控制在内）；
            # 产品协调器属性经 __getattr__ 转发可用。
            self.assertIsInstance(server.coordinator,
                                  srv._HostOpsControlCoordinator)
            self.assertIsInstance(server.coordinator._inner,
                                  srv._TsServiceControlCoordinator)
            self.assertIsNotNone(server.coordinator.writer)
            self.assertIsNotNone(server.coordinator.instances)
            # 签名 ts 请求由注入原语受理，确认标志显式在结果里。
            body = bp.decode_frame(bp.encode_frame(bp.build_request(
                "mgmt-k", "ts-service-control", {"action": "stop"})))
            response = server.coordinator.handle_management(body)
            self.assertTrue(response["ok"], response)
            self.assertIs(response["result"]["ts_service_stopped"], True)
            self.assertEqual(ts.calls, ["stop"])
            # 非 ts 动词仍由内层产品协调器处理（status 动作驱动 host_ops）。
            body = bp.decode_frame(bp.encode_frame(
                bp.build_request("mgmt-k", "status")))
            response = server.coordinator.handle_management(body)
            self.assertTrue(response["ok"], response)


class HostOpsExtensionTests(unittest.TestCase):
    """受限 ``host-verify``/``host-stop`` 动词（宿主证据扩展装饰器）：
    SYSTEM 侧证据在 broker 内执行，线上只携带固定 check 枚举；判定
    True/False/None 原样透传（None=证据不可解析，绝不冒充裁定）。"""

    def _decorated(self, host):
        inner = SimpleNamespace(
            mgmt_secret="mgmt-k", replay=ReplayRegistry(),
            handle_management=lambda body, now=None: {"ok": True,
                                                      "result": {"delegated": True}})
        return srv._HostOpsControlCoordinator(inner, host), inner

    def test_host_verify_running_and_closed_verdicts(self):
        host = RecordingOps()
        host.running = True
        deco, _ = self._decorated(host)
        for check, key, expected in (("running", "host_running", True),
                                     ("closed", "host_closed", False)):
            body = bp.decode_frame(bp.encode_frame(bp.build_request(
                "mgmt-k", "host-verify", {"check": check})))
            response = deco.handle_management(body)
            self.assertTrue(response["ok"], response)
            self.assertIs(response["result"][key], expected)

    def test_host_verify_none_verdict_passes_through(self):
        host = SimpleNamespace(verify_running=lambda: None,
                               verify_closed=lambda: None)
        deco, _ = self._decorated(host)
        body = bp.decode_frame(bp.encode_frame(bp.build_request(
            "mgmt-k", "host-verify", {"check": "running"})))
        response = deco.handle_management(body)
        self.assertTrue(response["ok"], response)
        self.assertIsNone(response["result"]["host_running"])

    def test_host_verify_bad_check_is_bad_request(self):
        host = RecordingOps()
        deco, _ = self._decorated(host)
        body = bp.decode_frame(bp.encode_frame(bp.build_request(
            "mgmt-k", "host-verify", {"check": "garbage"})))
        response = deco.handle_management(body)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error_code"], "bad_request")
        body = bp.decode_frame(bp.encode_frame(bp.build_request(
            "mgmt-k", "host-verify", {"check": "closed", "task": "x"})))
        response = deco.handle_management(body)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error_code"], "bad_request")
        self.assertEqual(host.status_calls, 0)

    def test_host_stop_confirmed_and_unconfirmed(self):
        host = RecordingOps()
        host.running = True
        deco, _ = self._decorated(host)
        body = bp.decode_frame(bp.encode_frame(
            bp.build_request("mgmt-k", "host-stop")))
        response = deco.handle_management(body)
        self.assertTrue(response["ok"], response)
        self.assertIs(response["result"]["host_stopped"], True)
        self.assertFalse(host.running)
        # stop_host 未确认 True -> unconfirmed 拒绝，绝不伪报成功。
        failing = SimpleNamespace(stop_host=lambda: False)
        deco2, _ = self._decorated(failing)
        response = deco2.handle_management(body)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error_code"], "unconfirmed")

    def test_non_host_verbs_delegate_to_inner_untouched(self):
        host = RecordingOps()
        deco, inner = self._decorated(host)
        body = bp.decode_frame(bp.encode_frame(
            bp.build_request("mgmt-k", "drain")))
        response = deco.handle_management(body)
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"], {"delegated": True})
        self.assertEqual(host.status_calls, 0)


class DeployProbeDiscriminationTests(unittest.TestCase):
    """Wire-shape pin for deploy-elevated.ps1 Restart-BrokerAndVerify
    (reviewer finding R1 round-trip): the PS probe discriminates the
    refreshed broker via the host-verify VERB because the status result on
    the management pipe NEVER carries share_max_concurrency (that field
    exists only in ShareManager.status(), usagesrv-side, never on the
    wire). These assertions pin exactly the shapes the PS probe depends
    on, against the REAL product coordinator + the REAL decorator — and
    the bare inner coordinator pins what a pre-extension (stale) broker
    answers."""

    def _inner(self, host_ops):
        from share_broker_host import BrokerHostCoordinator
        return BrokerHostCoordinator(
            writer=SimpleNamespace(write=lambda *args, **kwargs: None),
            mgmt_secret="mgmt-k", instance_secret="inst-k",
            instances=SimpleNamespace(is_current=lambda _iid: True),
            replay=ReplayRegistry(),
            host_ops=host_ops,
            instance_call=lambda action, fields, instance_id: {
                "ok": False, "error_code": "instance_mismatch",
                "result": {}})

    def test_status_shape_and_host_verify_discriminator(self):
        # WindowsHostOps.status() wire shape (share_broker_ops.py:257):
        # exactly host/task/state - nothing else ever crosses the pipe.
        host_ops = SimpleNamespace(
            status=lambda: {"host": "scheduled-task",
                            "task": "umshare-live", "state": "Stopped"},
            verify_running=lambda: True, verify_closed=lambda: True,
            start_host=lambda: True, stop_host=lambda: True)
        inner = self._inner(host_ops)
        new_broker = srv._HostOpsControlCoordinator(inner, host_ops)
        status_body = bp.decode_frame(bp.encode_frame(bp.build_request(
            "mgmt-k", "status")))
        status_resp = new_broker.handle_management(status_body)
        self.assertTrue(status_resp["ok"], status_resp)
        # The status result now carries the sanitized diag ring (additive):
        # host/task/state + diag (a list; never share_max_concurrency).
        self.assertEqual(set(status_resp["result"]),
                         {"host", "task", "state", "diag"})
        self.assertIsInstance(status_resp["result"]["diag"], list)
        self.assertNotIn("share_max_concurrency", status_resp["result"])
        hv_body = bp.decode_frame(bp.encode_frame(bp.build_request(
            "mgmt-k", "host-verify", {"check": "running"})))
        hv_resp = new_broker.handle_management(hv_body)
        self.assertTrue(hv_resp["ok"], hv_resp)
        self.assertIn("host_running", hv_resp["result"])

    def test_stale_generation_rejects_host_verify_verb(self):
        host_ops = SimpleNamespace(
            status=lambda: {"host": "scheduled-task",
                            "task": "umshare-live", "state": "Stopped"},
            verify_running=lambda: True, verify_closed=lambda: True,
            start_host=lambda: True, stop_host=lambda: True)
        inner = self._inner(host_ops)
        # The bare inner coordinator IS the pre-extension broker shape:
        # status stays ok (pipe liveness alone proves nothing), while the
        # host-verify verb falls through to the inner whitelist and is
        # rejected - that is the discrimination the deploy probe relies on.
        status_body = bp.decode_frame(bp.encode_frame(bp.build_request(
            "mgmt-k", "status")))
        stale_status = inner.handle_management(status_body)
        self.assertTrue(stale_status["ok"])
        self.assertEqual(set(stale_status["result"]),
                         {"host", "task", "state", "diag"})
        hv_body = bp.decode_frame(bp.encode_frame(bp.build_request(
            "mgmt-k", "host-verify", {"check": "running"})))
        stale_hv = inner.handle_management(hv_body)
        self.assertFalse(stale_hv["ok"])
        self.assertEqual(stale_hv["error_code"], "bad_request")


class BrokerDiagStageTests(unittest.TestCase):
    """诊断缝（维护者方向 2026-09-17，最小脱敏）：instance_call 的阶段区分
    （connect/peer-identity/identity/exchange）+ BrokerDiag 环的固定键集与
    上界。断言两轴：(a) 各阶段失败可区分（错误码/异常类型/原因码）；
    (b) 记录面恒为固定脱敏键集——秘密/签名原文/完整报文不可能进入。"""

    def _collector(self):
        events = []
        return events, (lambda stage, **kw:
                        events.append(dict(stage=stage, **kw)))

    def test_ring_fixed_keys_and_bounded(self):
        from share_broker_host import BrokerDiag
        d = BrokerDiag(maxlen=3)
        for i in range(5):
            d.record("stage%d" % i, ok=False, error_code="e" * 80,
                     exception_type="OSError", detail="x" * 500,
                     instance_id="iid", request_id="rid")
        snap = d.snapshot()
        self.assertEqual(len(snap), 3)          # 上界生效
        entry = snap[-1]
        self.assertEqual(set(entry), {"stage", "ok", "error_code",
                                      "exception_type", "detail",
                                      "instance_id", "request_id", "ts"})
        self.assertLessEqual(len(entry["detail"]), 120)   # 详情截断
        self.assertEqual(entry["error_code"], "e" * 40)   # 错误码封顶
        self.assertNotIn("secret", json.dumps(entry))
        self.assertNotIn("signature", json.dumps(entry))

    def test_instance_call_happy_path_records_identity_and_exchange(self):
        import share_broker_ops as ops_mod
        events, diag = self._collector()
        captured = {}

        class EchoKernel:
            def CreateFileW(self, *a): return 0x1000
            def GetLastError(self): return 0
            def GetNamedPipeServerProcessId(self, h, out):
                out.contents.value = 4321
                return 1
            def WriteFile(self, h, data, n, written, o):
                captured["req"] = bytes(data[:n])
                written.contents.value = n
                return 1
            def ReadFile(self, h, buf, n, read, overlapped):
                req = json.loads(captured["req"].decode("utf-8"))
                payload = json.dumps(
                    {"v": 2, "request_id": req["request_id"], "ok": True,
                     "error_code": "ok",
                     "result": {"instance_id": "iid-1", "state": "running"}},
                    ensure_ascii=False).encode("utf-8") + b"\n"
                buf[:len(payload)] = payload
                read.contents.value = len(payload)
                return 1
            def CloseHandle(self, h): return 1

        from types import SimpleNamespace as _NS
        verifier = _NS(verify=lambda pid, expect_image=None,
                       expect_owner_sid=None,
                       expect_creation_filetime=None:
                       (True, {"creation_filetime": 7}))

        with tempfile.TemporaryDirectory() as td:
            key = Path(td) / "k"
            _write_secret(key, "unit-channel-secret")
            call = ops_mod.make_instance_call(
                secret_file=str(key), kernel_factory=EchoKernel,
                verifier=verifier, diag_event=diag)
            result = call("instance-status", {}, "iid-1")
        self.assertTrue(result["ok"], result)
        stages = [e["stage"] for e in events]
        self.assertEqual(stages, ["identity", "exchange"])
        self.assertTrue(events[0]["ok"])
        self.assertTrue(events[1]["ok"])

    def test_instance_call_connect_failure_records_stage(self):
        import share_broker_ops as ops_mod
        events, diag = self._collector()

        class DeniedKernel:
            def CreateFileW(self, *a): return 0xFFFFFFFFFFFFFFFF
            def GetLastError(self): return 5

        with tempfile.TemporaryDirectory() as td:
            key = Path(td) / "k"
            _write_secret(key, "unit-channel-secret")
            call = ops_mod.make_instance_call(
                secret_file=str(key), kernel_factory=DeniedKernel,
                verifier=lambda *a, **k: (True, {"creation_filetime": 1}),
                diag_event=diag)
            with self.assertRaises(OSError):
                call("instance-status", {}, "iid-1")
        stages = [e["stage"] for e in events]
        self.assertEqual(stages, ["connect"])
        self.assertEqual(events[0]["exception_type"], "OSError")

    def test_instance_call_identity_refusal_records_reason_code(self):
        import share_broker_ops as ops_mod
        events, diag = self._collector()

        class Kernel:
            def CreateFileW(self, *a): return 0x1000
            def GetLastError(self): return 0
            def GetNamedPipeServerProcessId(self, h, out):
                out.contents.value = 4321
                return 1
            def CloseHandle(self, h): return 1

        from types import SimpleNamespace as _NS
        verifier = _NS(verify=lambda pid, expect_image=None,
                       expect_owner_sid=None,
                       expect_creation_filetime=None:
                       (False, {"reason": "image-mismatch"}))

        with tempfile.TemporaryDirectory() as td:
            key = Path(td) / "k"
            _write_secret(key, "unit-channel-secret")
            call = ops_mod.make_instance_call(
                secret_file=str(key), kernel_factory=Kernel,
                verifier=verifier, diag_event=diag)
            with self.assertRaises(PermissionError):
                call("instance-status", {}, "iid-1")
        stages = [e["stage"] for e in events]
        self.assertEqual(stages, ["identity"])
        self.assertEqual(events[0]["error_code"], "identity-refused")
        self.assertEqual(events[0]["detail"], "image-mismatch")

    def test_coordinator_publish_records_resolve_stage(self):
        # 空信任注册表+发现缝未接线 → resolve 阶段如实记录（脱敏），且
        # status 暴露 diag（受保护管理边界）。
        from share_broker_host import BrokerDiag, BrokerHostCoordinator
        diag = BrokerDiag()
        import hashlib
        writer = SimpleNamespace(
            write=lambda *a, **k: None,
            fingerprint=lambda proj: hashlib.sha256(
                json.dumps(proj, sort_keys=True).encode("utf-8")).hexdigest())
        coord = BrokerHostCoordinator(
            writer=writer,
            mgmt_secret="mgmt-k", instance_secret="inst-k",
            instances=InstanceRegistry(), replay=ReplayRegistry(),
            host_ops=SimpleNamespace(
                status=lambda: {"host": "scheduled-task",
                                "task": "umshare-live",
                                "state": "Ready"}),
            instance_call=lambda action, fields, instance_id: {
                "ok": False, "error_code": "x", "result": {}},
            discover_instance=None, diag=diag)
        projection = _projection()
        fingerprint = writer.fingerprint(projection)
        body = bp.decode_frame(bp.encode_frame(bp.build_request(
            "mgmt-k", "publish", {"generation": 5, "fingerprint": fingerprint,
                                  "projection": projection})))
        resp = coord.handle_management(body)
        self.assertFalse(resp["ok"])
        stages = [e["stage"] for e in diag.snapshot()]
        self.assertEqual(stages, ["resolve"])
        entry = diag.snapshot()[0]
        self.assertIs(entry["ok"], False)
        self.assertEqual(entry["error_code"], "no-registered-instance")
        self.assertEqual(entry["detail"], "discovery failed or unwired")
        # status 暴露 diag（受保护管理边界）。
        status_body = bp.decode_frame(bp.encode_frame(bp.build_request(
            "mgmt-k", "status")))
        status_resp = coord.handle_management(status_body)
        self.assertIn("diag", status_resp["result"])
        self.assertEqual(len(status_resp["result"]["diag"]), 1)


class _FakeWallClock:
    """Deterministic wall clock for the lease store (the coordinator's
    monotonic clock stays real)."""

    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, delta):
        self.value += float(delta)
        return self.value


class _MaintenanceInstance:
    """Deterministic instance channel for the maintenance verbs."""

    def __init__(self, state="running", active=0):
        self.state = state
        self.active = active
        self.calls = []

    def __call__(self, action, fields, instance_id):
        self.calls.append(action)
        if action == "instance-status":
            return {"ok": True, "error_code": "ok",
                    "result": {"instance_id": instance_id,
                               "state": self.state,
                               "active_count": self.active,
                               "last_seen_generation": 4}}
        if action == "drain":
            was_active = self.active
            self.state = "draining"
            return {"ok": True, "error_code": "ok",
                    "result": {"instance_id": instance_id,
                               "state": "draining",
                               "active_count": was_active}}
        if action == "resume":
            self.state = "running"
            return {"ok": True, "error_code": "ok",
                    "result": {"instance_id": instance_id,
                               "state": "running",
                               "active_count": self.active,
                               "resumed": True}}
        return {"ok": False, "error_code": "bad_request",
                "result": {"detail": "unknown action"}}

    def count(self, action):
        return self.calls.count(action)


def _maintenance_coordinator(td, instance, clock=None):
    """Real coordinator + real lease store + fake instance channel."""
    fixture = _ServerFixture(td)
    return BrokerHostCoordinator(
        writer=fixture.writer, mgmt_secret="mgmt-k",
        instance_secret="inst-k", instances=fixture.instances,
        replay=bp.ReplayRegistry(), host_ops=fixture.ops,
        instance_call=instance,
        leases=MaintenanceLeaseStore(clock=clock or time.time))


class MaintenanceRecordTests(unittest.TestCase):
    """受保护 maintenance.json（契约 §11.1）：缺失/畸形/未知 state → idle
    fail-safe；原子写；broker-status.json 镜像；启动恢复重新采用租约；
    惰性到期在有界 resume 成功后才回收；维护暂态绝不写停止终态。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _server(self, state_dir=None, coordinator=None, instance=None,
                clock=None):
        instance = instance or _MaintenanceInstance()
        coord = coordinator or _maintenance_coordinator(
            self.temp.name, instance, clock=clock)
        return srv.ManagementPipeServer(
            coord, r"\\.\pipe\um-share-broker", deadline_seconds=5.0,
            state_dir=state_dir), coord, instance

    def _serve(self, coord, request, state_dir=None):
        kernel = FakeK32(bp.encode_frame(request))
        server = srv.ManagementPipeServer(
            coord, r"\\.\pipe\um-share-broker", kernel=(kernel, None),
            deadline_seconds=5.0, state_dir=state_dir)
        record = server.serve_once()
        return server, record, kernel

    def test_missing_record_falls_back_to_idle(self):
        state_dir = Path(self.temp.name) / "broker"
        state_dir.mkdir()
        server, _, _ = self._server(state_dir=state_dir)
        self.assertEqual(server.maintenance,
                         {"state": "idle", "last_outcome": None})

    def test_malformed_or_unknown_state_falls_back_to_idle(self):
        state_dir = Path(self.temp.name) / "broker"
        state_dir.mkdir()
        path = state_dir / "maintenance.json"
        for payload in ("not-json{", "[]",
                        json.dumps({"state": "bogus"}),
                        json.dumps({"state": "leased"}),
                        json.dumps({"state": "leased", "lease_id": "a" * 32,
                                    "instance_id": "i-1", "round": 1,
                                    "expires_at": "soon"})):
            path.write_text(payload, encoding="utf-8")
            server, _, _ = self._server(state_dir=state_dir)
            self.assertEqual(server.maintenance,
                             {"state": "idle", "last_outcome": None})
            self.assertIsNone(server.coordinator.leases.current())

    def test_set_maintenance_writes_atomically_and_mirrors(self):
        state_dir = Path(self.temp.name) / "broker"
        state_dir.mkdir()
        server, _, _ = self._server(state_dir=state_dir)
        record = {"schema_version": 1, "state": "leased",
                  "component": "share_gateway", "lease_id": "a" * 32,
                  "instance_id": "i-1", "round": 1, "created_at": 1000,
                  "expires_at": 1300, "by": "maintenance-begin",
                  "last_outcome": None}
        server._set_maintenance(record)
        self.assertEqual(server.maintenance["state"], "leased")
        persisted = json.loads((state_dir / "maintenance.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual(persisted, record)
        self.assertFalse((state_dir / "maintenance.json.tmp").exists())
        # broker-status.json 每周期镜像 maintenance（同 lifecycle 模式）。
        kernel = FakeK32(bp.encode_frame(bp.build_request("mgmt-k",
                                                          "status")))
        mirror = srv.ManagementPipeServer(
            server.coordinator, r"\\.\pipe\um-share-broker",
            kernel=(kernel, None), deadline_seconds=5.0,
            state_dir=state_dir)
        mirror.maintenance = dict(record)
        mirror.serve_once()
        payload = json.loads((state_dir / "broker-status.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(payload["maintenance"]["state"], "leased")
        self.assertEqual(payload["maintenance"]["lease_id"], "a" * 32)

    def test_startup_restores_a_leased_record_and_keeps_binding(self):
        state_dir = Path(self.temp.name) / "broker"
        state_dir.mkdir()
        expires_at = int(time.time()) + 300
        (state_dir / "maintenance.json").write_text(json.dumps({
            "schema_version": 1, "state": "leased",
            "component": "share_gateway", "lease_id": "b" * 32,
            "instance_id": "i-1", "round": 1, "created_at": 1000,
            "expires_at": expires_at, "by": "maintenance-begin",
            "last_outcome": None}), encoding="utf-8")
        server, coord, _ = self._server(state_dir=state_dir)
        self.assertEqual(server.maintenance["state"], "leased")
        # 启动恢复：协调器重新采用同一租约（迟到结果仍被拒绝）。
        lease = coord.leases.current()
        self.assertIsNotNone(lease)
        self.assertEqual(lease["lease_id"], "b" * 32)
        status = coord._maintenance_status()
        self.assertEqual(status.result["lease"]["lease_id"], "b" * 32)
        self.assertIs(status.result["lease_expired"], False)

    def test_maintenance_begin_never_writes_a_stop(self):
        # B9-0 隔离验证 ②：维护暂态不经过 drain/emergency-stop 分支 →
        # lifecycle.json 不写 stopped，broker-status.json.lifecycle 不变。
        state_dir = Path(self.temp.name) / "broker"
        state_dir.mkdir()
        coord = _maintenance_coordinator(self.temp.name,
                                        _MaintenanceInstance())
        server, record, kernel = self._serve(
            coord, bp.build_request("mgmt-k", bp.MAINTENANCE_BEGIN_ACTION,
                                    {"component": "share_gateway",
                                     "lease_s": 300}), state_dir)
        self.assertTrue(record["ok"], record)
        response = json.loads(bytes(kernel.written))
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["state"], "draining")
        self.assertEqual(server.lifecycle["state"], "running")
        self.assertFalse((state_dir / "lifecycle.json").exists())
        persisted = json.loads((state_dir / "maintenance.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual(persisted["state"], "leased")
        self.assertEqual(persisted["by"], "maintenance-begin")
        payload = json.loads((state_dir / "broker-status.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(payload["lifecycle"]["state"], "running")
        self.assertEqual(payload["maintenance"]["state"], "leased")

    def test_wire_request_expires_a_due_lease_once(self):
        state_dir = Path(self.temp.name) / "broker"
        state_dir.mkdir()
        clock = _FakeWallClock()
        instance = _MaintenanceInstance()
        coord = _maintenance_coordinator(self.temp.name, instance,
                                         clock=clock)
        # 同一个 server 对象跨两个周期（生产只有一个 broker 进程实例）。
        kernel = FakeK32(bp.encode_frame(bp.build_request(
            "mgmt-k", bp.MAINTENANCE_BEGIN_ACTION,
            {"component": "share_gateway", "lease_s": 300})))
        server = srv.ManagementPipeServer(
            coord, r"\\.\pipe\um-share-broker", kernel=(kernel, None),
            deadline_seconds=5.0, state_dir=state_dir)
        record = server.serve_once()
        self.assertTrue(record["ok"], record)
        self.assertEqual(server.maintenance["state"], "leased")
        clock.advance(301)
        kernel.request_frame = bp.encode_frame(
            bp.build_request("mgmt-k", "status"))
        record2 = server.serve_once()
        self.assertTrue(record2["ok"], record2)
        self.assertEqual(instance.count("resume"), 1)
        self.assertIsNone(coord.leases.current())
        self.assertEqual(server.maintenance["state"], "idle")
        self.assertEqual(server.maintenance["by"], "expiry")
        persisted = json.loads((state_dir / "maintenance.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual(persisted["state"], "idle")
        self.assertEqual(persisted["by"], "expiry")


class HostEvidenceDecoratorTests(unittest.TestCase):
    """受限 ``host-evidence`` 只读动词（装饰器）：verdict 原样透传
    （None=证据不可裁定，绝不折成 True/False）；check 枚举收死；
    伪造 mac / 重放与既有动词同一管线。"""

    def _decorated(self, host):
        inner = SimpleNamespace(
            mgmt_secret="mgmt-k", replay=ReplayRegistry(),
            handle_management=lambda body, now=None: {"ok": True,
                                                      "result": {
                                                          "delegated": True}})
        return srv._HostOpsControlCoordinator(inner, host), inner

    def test_limits_verdict_passes_through_verbatim(self):
        for verdict in (True, False, None):
            host = SimpleNamespace(verify_limits=lambda v=verdict: v)
            deco, _ = self._decorated(host)
            body = bp.decode_frame(bp.encode_frame(bp.build_request(
                "mgmt-k", "host-evidence", {"check": "limits"})))
            response = deco.handle_management(body)
            self.assertTrue(response["ok"], response)
            self.assertEqual(response["result"]["check"], "limits")
            self.assertIs(response["result"]["verdict"], verdict)
            self.assertIsInstance(response["result"]["checked_at"], int)
            # 查询成功（无论裁定 True/False）→ detail=ok；None → 不可得。
            expected = "ok" if verdict is not None else "limits_unavailable"
            self.assertEqual(response["result"]["detail"], expected)

    def test_limits_unavailable_is_null_never_folded(self):
        host = SimpleNamespace()          # 无 verify_limits：诚实不可得
        deco, _ = self._decorated(host)
        body = bp.decode_frame(bp.encode_frame(bp.build_request(
            "mgmt-k", "host-evidence", {"check": "limits"})))
        response = deco.handle_management(body)
        self.assertTrue(response["ok"], response)
        self.assertIsNone(response["result"]["verdict"])
        self.assertEqual(response["result"]["detail"], "limits_unavailable")

    def test_faults_verdict_passes_through_and_absent_is_null(self):
        tail = {"last_fault": None,
                "counts_by_phase": {"auth": 0, "config": 0, "body": 0,
                                     "limit": 1, "upstream": 3, "stream": 0},
                "file": {"present": True, "size": 4096,
                         "mtime": 1790000000.0}}
        host = SimpleNamespace(read_faults=lambda: tail)
        deco, _ = self._decorated(host)
        body = bp.decode_frame(bp.encode_frame(bp.build_request(
            "mgmt-k", "host-evidence", {"check": "faults"})))
        response = deco.handle_management(body)
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["verdict"], tail)
        self.assertEqual(response["result"]["detail"], "read tail ok")
        # 文件读不到 → verdict null + fault_log_absent（绝不折成"无故障"）。
        absent = SimpleNamespace(read_faults=lambda: None)
        deco2, _ = self._decorated(absent)
        response = deco2.handle_management(body)
        self.assertTrue(response["ok"], response)
        self.assertIsNone(response["result"]["verdict"])
        self.assertEqual(response["result"]["detail"], "fault_log_absent")

    def test_bad_check_and_extra_field_are_bad_request(self):
        host = SimpleNamespace(verify_limits=lambda: True,
                               read_faults=lambda: None)
        deco, _ = self._decorated(host)
        for fields in ({"check": "garbage"}, {}, {"check": "limits",
                                                    "path": "C:\\x"}):
            body = bp.decode_frame(bp.encode_frame(bp.build_request(
                "mgmt-k", "host-evidence", fields)))
            response = deco.handle_management(body)
            self.assertFalse(response["ok"])
            self.assertEqual(response["error_code"], "bad_request")

    def test_forged_mac_is_refused_and_replay_consumed(self):
        host = SimpleNamespace(verify_limits=lambda: True)
        deco, inner = self._decorated(host)
        request = bp.build_request("WRONG", "host-evidence",
                                    {"check": "limits"})
        response = deco.handle_management(
            bp.decode_frame(bp.encode_frame(request)))
        self.assertFalse(response["ok"])
        self.assertEqual(response["error_code"], "forbidden")
        # 重放与既有动词共用同一 registry：同一 request_id 第二次被拒。
        signed = bp.build_request("mgmt-k", "host-evidence",
                                   {"check": "limits"})
        first = deco.handle_management(
            bp.decode_frame(bp.encode_frame(signed)))
        self.assertTrue(first["ok"], first)
        replay = deco.handle_management(
            bp.decode_frame(bp.encode_frame(signed)))
        self.assertFalse(replay["ok"])
        self.assertEqual(replay["error_code"], "replay")

    def test_non_evidence_verbs_delegate_untouched(self):
        host = SimpleNamespace(verify_limits=lambda: True)
        deco, inner = self._decorated(host)
        body = bp.decode_frame(bp.encode_frame(
            bp.build_request("mgmt-k", "drain")))
        response = deco.handle_management(body)
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"], {"delegated": True})


class _DispatchProbeServer(srv.ManagementPipeServer):
    """Test-only subclass: a dispatch that blocks a bounded time so the
    two-phase join is observable (no product rule lives here)."""

    def __init__(self, *args, dispatch_seconds=0.0, **kwargs):
        self.dispatch_seconds = dispatch_seconds
        self.dispatch_calls = 0
        super().__init__(*args, **kwargs)

    def _dispatch(self, body, record):
        self.dispatch_calls += 1
        time.sleep(self.dispatch_seconds)
        return b'{"ok": true}\n'


class DispatchDeadlineTests(unittest.TestCase):
    """``serve_once`` 两阶段 join（契约 §6.4）：``dispatch_deadline_seconds
    =None`` 与旧单次 join 逐字等价（不进入第二次 join）；设值时派发
    等待从真实派发起点起算、有界延长。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.fx = _ServerFixture(self.temp.name)

    def _joins(self):
        calls = []
        real_join = threading.Thread.join

        def fake_join(thread, timeout=None):
            calls.append(timeout)
            return real_join(thread, timeout)

        patcher = mock.patch.object(threading.Thread, "join", fake_join)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def _serve(self, dispatch_seconds, dispatch_deadline, cycle_deadline):
        kernel = FakeK32(bp.encode_frame(
            bp.build_request("mgmt-k", "status")))
        server = _DispatchProbeServer(
            self.fx.coordinator, r"\\.\pipe\um-share-broker",
            kernel=(kernel, None), deadline_seconds=cycle_deadline,
            state_dir=None, dispatch_seconds=dispatch_seconds,
            dispatch_deadline_seconds=dispatch_deadline)
        return server, kernel

    def test_none_keeps_the_old_single_join_byte_for_byte(self):
        calls = self._joins()
        server, _kernel = self._serve(0.5, None, 0.2)
        record = server.serve_once(deadline=time.monotonic() + 0.2)
        self.assertEqual(len(calls), 1)       # 绝不进入第二次 join
        self.assertFalse(record["ok"])
        self.assertTrue(record.get("abandoned"))
        self.assertEqual(record["error"], "connection deadline exceeded")
        self.assertEqual(server.dispatch_calls, 1)

    def test_set_extends_the_dispatch_wait_bounded(self):
        calls = self._joins()
        server, kernel = self._serve(0.4, 3.0, 0.2)
        record = server.serve_once(deadline=time.monotonic() + 0.2)
        self.assertEqual(len(calls), 2)       # 读预算 + 派发预算
        self.assertTrue(record["ok"], record)
        self.assertEqual(record["phase"], "reply")
        self.assertEqual(server.dispatch_calls, 1)
        response = json.loads(bytes(kernel.written))
        self.assertTrue(response["ok"])


if __name__ == "__main__":
    unittest.main()
