"""D4-prerequisite offline tests for the SYSTEM broker management pipe
server (ai-gateway/share/broker_server.py).

Everything runs on injected fake kernels and fixture coordinators/writers
in temp dirs - no real pipe, no SYSTEM task, no RunRoot access. The server
holds NO product coordination rules (they live in BrokerHostCoordinator,
already covered by test_share_broker.py); these tests verify the WIRE
layer (framing, bounded reads/writes, R41-4 cycle ownership and the
R37 admission contract) and the R41-1/R41-2 assembly closed loop: a REAL
signed management request through build_server's coordinator, a REAL
instance-channel signature frame validated by the test double's
handle_instance_control, and an ACK - never an echo fixture standing in for
the instance.
"""
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

import share.broker_protocol as bp  # noqa: E402
import share.broker_server as srv  # noqa: E402
from share.broker_host import (BrokerHostCoordinator,  # noqa: E402
                               InstanceRegistry, MaintenanceLeaseStore,
                               ReplayRegistry)
from share.runtime import ProtectedProjectionWriter  # noqa: E402


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


class BrokerDiagStageTests(unittest.TestCase):
    """诊断缝（维护者方向 2026-09-17，最小脱敏）：instance_call 的阶段区分
    （connect/peer-identity/identity/exchange）+ BrokerDiag 环的固定键集与
    上界。断言两轴：(a) 各阶段失败可区分（错误码/异常类型/原因码）；
    (b) 记录面恒为固定脱敏键集——秘密/签名原文/完整报文不可能进入。"""

    def test_ring_fixed_keys_and_bounded(self):
        from share.broker_host import BrokerDiag
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

    def test_coordinator_publish_records_resolve_stage(self):
        # 空信任注册表+发现缝未接线 → resolve 阶段如实记录（脱敏），且
        # status 暴露 diag（受保护管理边界）。
        from share.broker_host import BrokerDiag, BrokerHostCoordinator
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
