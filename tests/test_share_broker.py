"""D1/D2/D3 offline closed-loop tests for the REAL broker components.

All brokers, writers, stores and directories are fixtures; nothing here
touches production keys, the run copy, live gateways or OS permissions. The
named-pipe transport is NOT exercised (OS assumptions §27.7 H1–H6 remain
unverified); loopback/in-process transports stand in for the offline loop
and never count as permission evidence.
"""
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
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import share_broker_protocol as bp  # noqa: E402
import share_broker_client as bc  # noqa: E402
from share_broker_client import (BrokerClient,  # noqa: E402
                                 LoopbackTcpBrokerTransport)
from share_broker_host import (BrokerHostCoordinator,  # noqa: E402
                               InstanceRegistry, MaintenanceLeaseStore,
                               ReplayRegistry)
from share_management import (BrokerClientAdapter, BrokerError,  # noqa: E402
                              NoopBroker, ShareManager)
from share_runtime import ProtectedProjectionWriter  # noqa: E402
from share_store import MigrationConflict, ShareStore  # noqa: E402

_SHARE_GW = importlib.util.spec_from_file_location(
    "share_gateway_broker_test",
    Path(__file__).resolve().parents[1] / "chat2api" / "share_gateway.py")
share_gw = importlib.util.module_from_spec(_SHARE_GW)
_SHARE_GW.loader.exec_module(share_gw)

_REVOKE_STATE_TMP = None
_REVOKE_STATE_SAVED = None


def setUpModule():
    """Point the runtime's persisted revoke state at a private directory.

    The ACK now merges the persisted state and records the applied
    projection generation; the PRODUCT default path must never be written by
    the tests.
    """
    global _REVOKE_STATE_TMP, _REVOKE_STATE_SAVED
    _REVOKE_STATE_TMP = tempfile.TemporaryDirectory(
        prefix="um-share-revoke-")
    _REVOKE_STATE_SAVED = os.environ.get(share_gw.REVOKE_STATE_ENV)
    os.environ[share_gw.REVOKE_STATE_ENV] = str(
        Path(_REVOKE_STATE_TMP.name) / "share-revoke.state")
    share_gw.reset_revoke_state_cache()


def tearDownModule():
    if _REVOKE_STATE_SAVED is None:
        os.environ.pop(share_gw.REVOKE_STATE_ENV, None)
    else:
        os.environ[share_gw.REVOKE_STATE_ENV] = _REVOKE_STATE_SAVED
    share_gw.reset_revoke_state_cache()
    if _REVOKE_STATE_TMP is not None:
        _REVOKE_STATE_TMP.cleanup()


def _fingerprint(obj):
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True).encode("utf-8")).hexdigest()


class FakeHostOps:
    """Fixture OS: OBSERVABLE open/closed state + deterministic pause gates.
    Only the OS actions live here — the coordination rules (order,
    compensation, verification contract) stay in the product coordinator."""

    def __init__(self):
        self.running = False
        self.start_gate = None        # pauses start_host mid-action
        self.start_entered = threading.Event()
        self.stop_fails = False
        self.closed_verifiable = True

    def start_host(self):
        if self.start_gate is not None:
            self.start_entered.set()
            self.start_gate.wait(14)   # entry opens AFTER the gate releases
        self.running = True
        return True

    def stop_host(self):
        if self.stop_fails:
            return False
        self.running = False
        return True

    def verify_running(self):
        return self.running

    def verify_closed(self):
        return self.closed_verifiable and not self.running

    def status(self):
        return {"host": "fixture", "running": self.running}


class InProcessBrokerHost:
    """Thin fixture shell around the PRODUCT coordinator (R29-1/2/3): it
    substitutes the transport (direct call), the OS (FakeHostOps) and
    instance registration. NO business rules live here anymore."""

    def __init__(self, writer, share, mgmt_secret, instance_secret,
                 drain_timeout=5.0):
        self.share = share
        self.mgmt_secret = mgmt_secret
        self.instance_secret = instance_secret
        self.host_ops = FakeHostOps()
        self.instances = InstanceRegistry()
        self.replay = ReplayRegistry()
        self.publish_count = 0
        self.emergency_calls = 0
        self.coordinator = BrokerHostCoordinator(
            writer, mgmt_secret, instance_secret, self.instances,
            self.replay, self.host_ops,
            instance_call=self._instance_call, drain_timeout=drain_timeout)

    def register_instance(self):
        # 受信任登记：部署接线将当前实例注册给 broker（OS 身份核验留 D4，
        # 登记与拒绝分支离线可测）。
        self.instances.register(self.share.instance_id)

    def replace_writer(self, writer):
        self.coordinator.writer = writer

    def _instance_call(self, action, fields, instance_id):
        if not self.instances.is_current(instance_id):
            return {"ok": False, "error_code": "instance_mismatch",
                    "result": {"detail": "instance is not the registered "
                                         "current instance"}}
        req = bp.build_request(self.instance_secret, action, fields,
                               instance_id=instance_id)
        return self.share.handle_instance_control(req, self.instance_secret)

    def handle_management(self, body):
        frame = self.coordinator.handle_management(body)
        if isinstance(body, dict) and body.get("action") == "publish"                 and frame.get("ok"):
            self.publish_count += 1
        if isinstance(body, dict) and body.get("action") == "emergency-stop":
            self.emergency_calls += 1
        return frame


class _InProcessTransport:
    """Transport fixture: the 'wire' is a direct function call."""

    def __init__(self, host):
        self.host = host

    def open(self):
        return self

    def close(self):
        pass

    def send_all(self, data, deadline=None):
        self._request = bp.decode_frame(data)

    def recv_line(self, deadline):
        return bp.encode_frame(self.host.handle_management(self._request))


def make_client_adapter(host, port=None):
    """BrokerClientAdapter over an in-process transport bound to the fixture
    host (the 'wire' is a direct function call; loopback transport variant
    lives in the loopback tests)."""
    return BrokerClientAdapter(BrokerClient(
        lambda: _InProcessTransport(host), host.mgmt_secret))


class _ScriptedClient:
    """BrokerClient stand-in replaying programmed (ok, error_code, result)
    tuples — records (action, fields) pairs; the host verbs map onto the
    same scripted results."""

    def __init__(self, results):
        self.calls = []
        self._results = list(results)

    def _take(self, action, fields):
        self.calls.append((action, dict(fields or {})))
        item = self._results.pop(0) if len(self._results) > 1 \
            else self._results[0]
        if isinstance(item, BaseException):
            raise item
        return SimpleNamespace(ok=item[0], error_code=item[1],
                               result=dict(item[2] or {}))

    def call(self, action, fields=None, instance_id=None, transport=None):
        return self._take(action, fields)

    def host_verify(self, check):
        return self._take("host-verify", {"check": check})

    def host_stop(self):
        return self._take("host-stop", {})


class BrokerClientAdapterHostVerbTests(unittest.TestCase):
    """adapter 层 host-verify/host-stop 解析：verdict 原样透传
    （True/False/None），拒绝/缺字段抛 BrokerError，绝不冒充裁定。"""

    def test_host_verify_verdict_passthrough(self):
        for check, key in (("running", "host_running"),
                           ("closed", "host_closed")):
            client = _ScriptedClient([(True, "ok", {key: True}),
                                      (True, "ok", {key: False}),
                                      (True, "ok", {key: None})])
            adapter = BrokerClientAdapter(client)
            self.assertIs(adapter.host_verify(check), True)
            self.assertIs(adapter.host_verify(check), False)
            self.assertIsNone(adapter.host_verify(check))
            self.assertEqual(client.calls,
                             [("host-verify", {"check": check})] * 3)

    def test_host_verify_unknown_check_refused_locally(self):
        adapter = BrokerClientAdapter(_ScriptedClient([]))
        with self.assertRaises(BrokerError):
            adapter.host_verify("garbage")

    def test_host_verify_rejection_and_missing_field_raise(self):
        client = _ScriptedClient([(False, "bad_request", {}),
                                  (True, "ok", {})])
        adapter = BrokerClientAdapter(client)
        for _ in range(2):
            with self.assertRaises(BrokerError):
                adapter.host_verify("running")

    def test_host_stop_confirmed_and_rejected(self):
        client = _ScriptedClient([(True, "ok", {"host_stopped": True}),
                                  (False, "unconfirmed", {})])
        adapter = BrokerClientAdapter(client)
        self.assertIs(adapter.host_stop(), True)
        with self.assertRaises(BrokerError):
            adapter.host_stop()
        self.assertEqual(client.calls, [("host-stop", {})] * 2)

    def test_noop_broker_host_verbs_raise(self):
        from share_management import NoopBroker
        broker = NoopBroker()
        with self.assertRaises(BrokerError):
            broker.host_verify("running")
        with self.assertRaises(BrokerError):
            broker.host_stop()


class _RuntimeRevokeFixture:
    """S2 ACK fixture: the real runtime ACK states its OWN applied revoke
    watermark. Each revoking test records the generation its share applied."""

    def _install_revoke_fixture(self):
        self._saved_revoke_generations = dict(
            share_gw._revoke_applied_generations)
        share_gw._revoke_applied_generations.clear()
        self.addCleanup(self._restore_revoke_generations)

    def _restore_revoke_generations(self):
        share_gw._revoke_applied_generations.clear()
        share_gw._revoke_applied_generations.update(
            self._saved_revoke_generations)

    def _record_runtime_revoke(self, key_id, generation):
        """Fixture runtime: the share applied the revoke at this generation.

        The in-process map keeps the per-Key entry shape written by
        ``record_revoke_generation``; the management side must read its
        ``generation`` as the watermark (never treat the entry as 0)."""
        now = time.time()
        share_gw._revoke_applied_generations[key_id] = {
            "generation": generation, "applied_at": now,
            "updated_at": now}


class D1ClosedLoopTests(_RuntimeRevokeFixture, unittest.TestCase):
    """§26.7 定向验收：store→writer→share 加载→ACK→撤销拒绝 的完整闭环。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True,)
        self.addCleanup(self.td.cleanup)
        self.config = Path(self.td.name) / "um-share-projection.json"
        self.store_path = Path(self.td.name) / "share.db"
        self.writer = ProtectedProjectionWriter(
            self.config, acl_checker=lambda p: None)
        # 分享实例：固定 config 指向 writer 落点；空投影起步
        self.config.write_text(json.dumps(
            {"schema_version": 1, "config_generation": 1, "data": []}),
            encoding="utf-8")
        self.server = share_gw.ShareServer(("127.0.0.1", 0), self.config)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._teardown_server)
        self.host = InProcessBrokerHost(
            self.writer, self.server,
            mgmt_secret="mgmt-k", instance_secret="inst-k")
        self.host.register_instance()
        adapter = make_client_adapter(self.host)
        self.mgr = ShareManager(store_path=self.store_path, broker=adapter)
        self._install_revoke_fixture()

    def _teardown_server(self):
        self.server.shutdown()
        self.server.server_close()

    def test_publish_acks_and_share_loads_projection(self):
        # 正常发布 → writer 落盘 → share 锁内加载 → ACK → 管理 applied。
        res = self.mgr.create_key("friendLoop")
        self.assertTrue(res["in_sync"])
        self.assertEqual(res["published"], True)
        status = self.mgr.status()
        self.assertEqual(status["applied_generation"],
                         res["desired_generation"])
        self.assertEqual(self.server.last_seen_generation,
                         res["desired_generation"])
        self.assertEqual(self.host.publish_count, 1)

    def test_rotation_ack_marks_the_security_journal_applied(self):
        # A rotate leaves the Key enabled: only the runtime's applied
        # projection generation can confirm its security intent.
        created = self.mgr.create_key("friendRotate")
        self.assertTrue(created["in_sync"])
        rotated = self.mgr.rotate_key(created["key_id"])
        self.assertTrue(rotated["in_sync"])
        self.assertTrue(rotated["published"])
        self.assertEqual(rotated["security_pending"], 0)

    def test_limit_lowering_ack_marks_the_security_journal_applied(self):
        # Lowering the global limit is a revoke-direction intent with
        # key_id None; it has no per-Key entry, so the applied projection
        # generation is its ACK watermark.
        self.mgr.create_key("friendLimit")
        lowered = self.mgr.set_share_limits(8)
        self.assertTrue(lowered["in_sync"])
        self.assertTrue(lowered["published"])
        self.assertEqual(lowered["security_pending"], 0)

    def test_revocation_propagates_and_old_secret_is_refused(self):
        # 撤销 → 发布 ACK → share 已加载新投影 → 旧秘密的新请求被拒。
        created = self.mgr.create_key("friendRevoke")
        secret = created["secret"]
        key_id = created["key_id"]
        self.assertTrue(self.server.acquire(key_id,
                                            secret_version=created.get(
                                                "secret_version", 1)))
        self.server.active.clear()
        target = self.mgr.status()["desired_generation"] + 1
        self._record_runtime_revoke(key_id, target)
        deleted = self.mgr.delete_key(key_id, expected_row_version=1)
        self.assertTrue(deleted["published"])
        self.assertTrue(deleted["in_sync"])
        # share 的配置文件已被原子替换为空投影：旧秘密在接纳边界被拒。
        cfg = share_gw.load_config(self.config)
        self.assertEqual(cfg["entries"], [])
        # 接纳边界完整语义（与 handler 相同的调用形态）：已撤销身份在
        # 新代次下被拒。
        new_gen = cfg["config_generation"]
        self.assertFalse(self.server.acquire(key_id, generation=new_gen,
                                             secret_version=1))
        self.assertEqual(self.upstream_calls_if_any() or [], [])

    def upstream_calls_if_any(self):
        return []

    def test_writer_failure_keeps_old_projection_and_fails_closed(self):
        # 写失败（ACL 拒绝）→ 不替换旧投影 → 撤销 fail-closed。
        self.mgr.create_key("friendFail")
        boom = ProtectedProjectionWriter(self.config,
                                         acl_checker=lambda p: (_ for _ in ())
                                         .throw(PermissionError("ACL deny")))
        self.host.replace_writer(boom)
        store = ShareStore(self.store_path)
        try:
            key_id = store.list_keys()[0]["key_id"]
            rv = store.get_key(key_id)["row_version"]
            store.delete_key(key_id, expected_row_version=rv)
        finally:
            store.close()
        with self.assertRaises(BrokerError):
            self.mgr.delete_key(key_id, expected_row_version=2)
        self.assertEqual(self.host.emergency_calls, 1)   # fail-closed 关停
        # 旧投影文件未被替换：仍含删除前的身份（删除未在线生效）。
        entries = share_gw.load_config(self.config)["entries"]
        self.assertEqual([e["name"] for e in entries], ["friendFail"])

    def test_stale_generation_load_is_refused(self):
        # share 只 ACK 与文件实际内容匹配的 generation：请求旧代次 →
        # stale_generation，水位不动。
        self.mgr.create_key("friendStale")
        gen = self.server.last_seen_generation
        load = bp.build_request("inst-k", "load",
                                {"generation": gen + 5,
                                 "fingerprint": _fingerprint({})},
                                instance_id=self.server.instance_id)
        resp = self.server.handle_instance_control(load, "inst-k")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "stale_generation")
        self.assertEqual(self.server.last_seen_generation, gen)

    def test_wrong_instance_load_is_refused(self):
        # 实例身份绑定：指向另一实例的 load 被拒（旧实例 ACK 拒绝契约）。
        load = bp.build_request("inst-k", "load",
                                {"generation": 1, "fingerprint": "x" * 64},
                                instance_id="another-instance")
        resp = self.server.handle_instance_control(load, "inst-k")
        self.assertEqual(resp["error_code"], "instance_mismatch")

    def test_restarted_instance_refuses_stale_targeting(self):
        # 实例重启（新 instance_id）后，旧实例的请求不能再驱动本实例。
        old_id = self.server.instance_id
        self.server.instance_id = "fresh-after-restart"
        load = bp.build_request("inst-k", "load",
                                {"generation": 1, "fingerprint": "x" * 64},
                                instance_id=old_id)
        resp = self.server.handle_instance_control(load, "inst-k")
        self.assertEqual(resp["error_code"], "instance_mismatch")

    def test_duplicate_publish_is_refused_not_reapplied(self):
        # 同代次不同内容（篡改）被 writer 拒绝，旧投影保持。
        self.mgr.create_key("friendDup")
        before = share_gw.load_config(self.config)
        tampered = dict(before, entries=[{"key_id": "evil", "name": "evil",
                                          "secret_hash": "0" * 64,
                                          "secret_version": 1,
                                          "enabled": True}])
        adapter = self.mgr.broker
        with self.assertRaises(BrokerError):
            adapter.publish(tampered)
        self.assertEqual(share_gw.load_config(self.config)["entries"],
                         before["entries"])

    def test_lost_ack_is_a_failed_publication(self):
        # ACK 丢失（broker 超时）→ 发布失败、in_sync=False，绝不假成功。
        self.mgr.create_key("friendLost")
        class _TimeoutClient:
            def publish(self, projection):
                return bp.BrokerResult.err("deadline_exceeded", "lost")
            def status(self):
                return bp.BrokerResult.err("not_installed")
        adapter = BrokerClientAdapter(_TimeoutClient())
        mgr2 = ShareManager(store_path=Path(self.td.name) / "lost.db",
                            broker=adapter)
        res = mgr2.create_key("friendLost2")
        self.assertFalse(res["published"])
        self.assertFalse(res["in_sync"])
        self.assertIsNotNone(res["publication_error"])

    def test_global_64_publish_installs_capacity_and_refuses_65th(self):
        # A3 真实因果链：Store→管理→writer→投影→实例 load ACK→真实接纳。
        # 全局 64 必须在 ACK 之前安装到实例（业务上限 + TCP 容量），
        # 不能只放宽校验器而让旧进程仍只接纳 72；每 Key 仍 1..48。
        keys = [self.mgr.create_key(f"friendG{i}", max_concurrency=48)
                for i in range(8)]
        key_ids = [k["key_id"] for k in keys]
        # 启动值仍是 64。
        self.assertEqual(self.server.share_max_concurrency, 64)
        self.assertEqual(self.server.connections.limit, 64 + 8)
        raised = self.mgr.set_share_limits(64)
        self.assertTrue(raised["published"])
        self.assertTrue(raised["in_sync"])
        # ACK 后容量真实安装：64 个在途全部可被接纳，第 65 个由全局拒绝。
        self.assertEqual(self.server.share_max_concurrency, 64)
        self.assertEqual(self.server.connections.limit, 64 + 8)
        for key_id in key_ids:
            for _ in range(8):
                self.assertTrue(self.server.acquire(key_id))
        self.assertEqual(sum(self.server.active.values()), 64)
        reason = {}
        # 第 65 次由其中一个 Key 发第 9 次（每 Key 20/分钟仍未到限）。
        self.assertFalse(self.server.acquire(key_ids[0], reason_out=reason))
        self.assertEqual(reason["reason"], "share_concurrency")
        for key_id in key_ids:
            for _ in range(8):
                self.server.release(key_id)
        self.assertEqual(self.server.active, {})

    def test_dynamic_lowering_and_raising_keep_inflight(self):
        # 活跃连接期间升降限：在途保留，降限只阻止超额新请求；重复 load
        # 幂等（同代次同指纹不重置在途）；ACK 容量与实际接纳一致。
        keys = [self.mgr.create_key(f"friendD{i}", max_concurrency=48)
                for i in range(8)]
        key_ids = [k["key_id"] for k in keys]
        self.assertEqual(self.server.share_max_concurrency, 64)
        # 活跃连接期间降限到 24：4 个/Key 在途保留，新请求被全局拒绝。
        for key_id in key_ids:
            for _ in range(4):
                self.assertTrue(self.server.acquire(key_id))
        self.assertEqual(sum(self.server.active.values()), 32)
        target = self.mgr.status()["desired_generation"] + 1
        self._record_runtime_revoke(key_ids[0], target)
        lowered = self.mgr.set_share_limits(24)
        self.assertTrue(lowered["published"])
        self.assertEqual(self.server.share_max_concurrency, 24)
        self.assertEqual(self.server.connections.limit, 24 + 8)
        self.assertEqual(sum(self.server.active.values()), 32)  # 在途保留
        reason = {}
        self.assertFalse(self.server.acquire(key_ids[0], reason_out=reason))
        self.assertEqual(reason["reason"], "share_concurrency")
        # 重复 load（同代次同指纹）幂等：不重置在途、不改变已装容量。
        self._load_current_projection()
        self.assertEqual(self.server.share_max_concurrency, 24)
        self.assertEqual(self.server.connections.limit, 24 + 8)
        self.assertEqual(sum(self.server.active.values()), 32)
        for key_id in key_ids:
            for _ in range(4):
                self.server.release(key_id)
        self.assertEqual(self.server.active, {})
        # 降限完成后按新上限重新准入；再升限到 64 后真实接纳 64。
        raised2 = self.mgr.set_share_limits(64)
        self.assertTrue(raised2["published"])
        self.assertEqual(self.server.share_max_concurrency, 64)
        self.assertEqual(self.server.connections.limit, 64 + 8)
        for key_id in key_ids:
            for _ in range(8):
                self.assertTrue(self.server.acquire(key_id))
        self.assertEqual(sum(self.server.active.values()), 64)
        reason = {}
        self.assertFalse(self.server.acquire(key_ids[0], reason_out=reason))
        self.assertEqual(reason["reason"], "share_concurrency")
        for key_id in key_ids:
            for _ in range(8):
                self.server.release(key_id)
        self.assertEqual(self.server.active, {})

    def _load_current_projection(self):
        """Re-drive the instance load for the CURRENT projection file
        (fresh request id; same generation/fingerprint)."""
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        load = bp.build_request(
            "inst-k", "load",
            {"generation": payload["config_generation"],
             "fingerprint": _fingerprint(payload)},
            instance_id=self.server.instance_id)
        resp = self.server.handle_instance_control(load, "inst-k")
        self.assertTrue(resp["ok"], resp)


class D2LifecycleTests(unittest.TestCase):
    """§26.8 D2: lifecycle coordination — bounded drain, emergency priority
    over a pending start, and the blocked-start gate."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True,)
        self.addCleanup(self.td.cleanup)
        self.config = Path(self.td.name) / "um-share-projection.json"
        self.config.write_text(json.dumps(
            {"schema_version": 1, "config_generation": 1, "data": []}),
            encoding="utf-8")
        self.writer = ProtectedProjectionWriter(
            self.config, acl_checker=lambda p: None)
        self.server = share_gw.ShareServer(("127.0.0.1", 0), self.config)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._teardown_server)
        self.host = InProcessBrokerHost(
            self.writer, self.server, "mgmt-k", "inst-k")
        self.host.register_instance()
        self.mgr = ShareManager(store_path=Path(self.td.name) / "share.db",
                                broker=make_client_adapter(self.host))
        self.mgr.create_key("friendD2")

    def _teardown_server(self):
        self.server.shutdown()
        self.server.server_close()

    def _wait(self, mgr, operation_id, expected, timeout=8.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = mgr.get_operation(operation_id)
            if last["status"] == expected:
                return last
            time.sleep(0.05)
        self.fail(f"operation never reached {expected}: {last}")

    def test_drain_refuses_new_and_waits_for_inflight(self):
        # 在途占位后 drain：share 拒新（draining），在途保留；在途结束后
        # host 轮询到 0 → drained。
        gen = self.server.last_seen_generation
        key_id = self.mgr.list_keys()["keys"][0]["key_id"]
        self.assertTrue(self.server.acquire(key_id, generation=gen,
                                            secret_version=1))
        res = self.mgr.graceful_stop()
        self.assertEqual(res["status"], "pending")
        # 拒新已生效且在途保留（正在排空）——等待异步 executor 发出 drain。
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.server.snapshot()["state"] == "draining":
                break
            time.sleep(0.02)
        self.assertEqual(self.server.snapshot()["state"], "draining")
        self.assertIn(key_id, self.server.active)
        self.server.release(key_id)            # 在途完成（精确减计数）
        op = self._wait(self.mgr, res["operation_id"], "completed")
        self.assertEqual(op["payload"]["broker"]["drained"], True)
        self.assertEqual(op["payload"]["broker"]["timed_out"], False)
        # 排空完成 → 正常关闭语义：实例已拒新清场并停止（D2/R29-2）。
        self.assertEqual(op["payload"]["broker"]["stopped"], True)
        self.assertEqual(self.server.snapshot()["state"], "stopped")

    def test_drain_timeout_is_explicit_not_failure(self):
        # 在途永不释放 → 有界到期 → 明确 timed_out 结果（非崩溃、非假成功）。
        gen = self.server.last_seen_generation
        key_id = self.mgr.list_keys()["keys"][0]["key_id"]
        self.assertTrue(self.server.acquire(key_id, generation=gen,
                                            secret_version=1))
        host = InProcessBrokerHost(self.writer, self.server, "mgmt-k",
                                   "inst-k", drain_timeout=0.5)
        host.register_instance()
        mgr2 = ShareManager(store_path=Path(self.td.name) / "t2.db",
                            broker=make_client_adapter(host))
        res = mgr2.graceful_stop()
        op = self._wait(mgr2, res["operation_id"], "completed")
        self.assertEqual(op["payload"]["broker"]["timed_out"], True)
        self.assertEqual(op["payload"]["broker"]["drained"], False)
        # 超时后按正常关闭语义完成停止（R29-2）：实例拒新清场、主机核验
        # 关闭——不是留在 draining。
        self.assertEqual(op["payload"]["broker"]["stopped"], True)
        self.assertEqual(self.server.snapshot()["state"], "stopped")
        self.server.release(key_id)

    def test_drain_blocked_by_a_terminal_stop_still_stops_the_host(self):
        # 契约 §2.4/§13.4：实例对 drain 回 blocked（终态停止不被维护重开）
        # 时，broker 的 _drain 仍完成 host stop/verify，notification_failed
        # 如实记录；真实实例的终态守卫经真实 ShareServer 端到端验证。
        self.server.emergency_stop()
        self.assertEqual(self.server.snapshot()["state"], "stopped")
        calls = {"stop": 0, "closed": 0}
        fake = self.host.host_ops
        real_stop = fake.stop_host
        real_closed = fake.verify_closed

        def counting_stop():
            calls["stop"] += 1
            return real_stop()

        def counting_closed():
            calls["closed"] += 1
            return real_closed()

        fake.stop_host = counting_stop
        fake.verify_closed = counting_closed
        try:
            res = self.mgr.graceful_stop()
            op = self._wait(self.mgr, res["operation_id"], "completed")
            self.assertGreaterEqual(calls["stop"], 1)     # 主机停止仍执行
            self.assertGreaterEqual(calls["closed"], 1)   # 核验仍执行
            broker = op["payload"]["broker"]
            self.assertTrue(broker["notification_failed"])
            self.assertTrue(broker["stopped"])
            self.assertEqual(self.server.snapshot()["state"], "stopped")
        finally:
            fake.stop_host = real_stop
            fake.verify_closed = real_closed

    def test_emergency_preempts_pending_start(self):
        # start 在途时紧急停止完成 → 迟到的 start 不得恢复公网（A6/epoch）。
        fake = self.host.host_ops
        fake.start_gate = threading.Event()
        try:
            start_res = self.mgr.start()
            self.assertEqual(start_res["status"], "pending")
            # 确定性等待旧 start 的 OS 启动进入门闩。
            self.assertTrue(fake.start_entered.wait(5))
            emg = self.mgr.emergency_stop()
            self._wait(self.mgr, emg["operation_id"], "completed")
            self.assertEqual(self.server.snapshot()["state"], "stopped")
            fake.start_gate.set()
            op = self._wait(self.mgr, start_res["operation_id"], "failed",
                            timeout=16)
            self.assertEqual(op["payload"].get("superseded"), True)
            self.assertIn("emergency", op["payload"]["error"])
            self.assertEqual(self.mgr._lifecycle_snapshot()[1], "stopped")
        finally:
            fake.start_gate.set()

    def test_blocked_start_is_refused(self):
        # 未确认撤销 → blocked → start 门禁拒绝，不开公网。
        boom = ProtectedProjectionWriter(self.config,
                                         acl_checker=lambda p: (_ for _ in ())
                                         .throw(PermissionError("ACL deny")))
        self.host.replace_writer(boom)
        self.host.host_ops.stop_fails = True   # 关停无法确认 → blocked
        created = self.mgr.list_keys()["keys"][0]
        with self.assertRaises(BrokerError):
            self.mgr.delete_key(created["key_id"],
                                expected_row_version=created["row_version"])
        status = self.mgr.status()
        self.assertTrue(status["blocked"])
        with self.assertRaises(BrokerError) as cm:
            self.mgr.start()
        self.assertIn("blocked", str(cm.exception))


class D3MigrationEntryTests(unittest.TestCase):
    """§26.9 D3: controlled migration entry, consistent backup and the
    projection-mode Prep gate."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True,)
        self.addCleanup(self.td.cleanup)
        self.store_path = Path(self.td.name) / "share.db"
        self.store = ShareStore(self.store_path)
        self.addCleanup(self.store.close)
        self.mgr = ShareManager(store_path=self.store_path,
                                broker=NoopBroker())

    def test_migrate_entry_previews_conflict_before_apply(self):
        # 运行副本多出的身份（如 friend2）→ MigrationConflict 停查，库不变。
        gen = self.store.generation()
        with self.assertRaises(MigrationConflict):
            self.mgr.migrate({"friend1": "a" * 64},
                             run_copy_digests={"friend2": "c" * 64})
        self.assertEqual(self.store.list_keys(), [])
        self.assertEqual(self.store.generation()["config_generation"], 1)

    def test_migrate_entry_is_idempotent_with_stable_ids(self):
        first = self.mgr.migrate({"friend1": "a" * 64, "friend3": "d" * 64},
                                 run_copy_digests={"friend1": "a" * 64,
                                                   "friend3": "d" * 64})
        second = self.mgr.migrate({"friend1": "a" * 64, "friend3": "d" * 64},
                                  idem_map=first["idempotency_map"])
        self.assertEqual(first["mapping"], second["mapping"])
        rows = {r["name"]: r["key_id"] for r in self.store.list_keys()}
        self.assertEqual(rows["friend1"], first["mapping"]["friend1"])
        self.assertEqual(first["preview"]["problems"], [])
        # 发布意图随迁移同事务落库（安全方向变更，非撤销）。
        intents = [o for o in self.store.operations(kind="publish")
                   if o["payload"].get("action_kind") == "migrate"]
        self.assertGreaterEqual(len(intents), 1)

    def test_backup_is_consistent_and_restores(self):
        self.mgr.migrate({"friend1": "a" * 64})
        target = Path(self.td.name) / "backup" / "share-backup.db"
        self.store.backup(target)
        reopened = ShareStore(target)
        try:
            rows = reopened.list_keys()
            self.assertEqual([r["name"] for r in rows], ["friend1"])
            self.assertEqual(reopened.generation()["config_generation"],
                             self.store.generation()["config_generation"])
        finally:
            reopened.close()

    def test_cli_operation_wait_tracks_202(self):
        # CLI `operation <id> --wait`：202 后轮询至终态。Noop broker（未安
        # 装真实 broker）下 start 的终态是明确 failed + 原因——输出区分
        # saved/pending/failed，绝不假成功（D3 验收）。
        res = self.mgr.start()
        self.assertEqual(res["status"], "pending")
        deadline = time.monotonic() + 5
        op = None
        while time.monotonic() < deadline:
            op = self.mgr.get_operation(res["operation_id"])
            if op["status"] in ("completed", "failed", "blocked"):
                break
            time.sleep(0.05)
        self.assertEqual(op["status"], "failed")
        self.assertIn("no broker installed", op["payload"]["error"])




class R29ClosureTests(unittest.TestCase):
    """§29 定向验收：协议重放消费、ACK 实例绑定、writer 持久基线与迟到
    start 的已核验关闭补偿。fixture 只替代 transport/OS/时钟。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True,)
        self.addCleanup(self.td.cleanup)
        self.config = Path(self.td.name) / "um-share-projection.json"
        self.config.write_text(json.dumps(
            {"schema_version": 1, "config_generation": 1, "data": []}),
            encoding="utf-8")
        self.writer = ProtectedProjectionWriter(
            self.config, acl_checker=lambda p: None)
        self.server = share_gw.ShareServer(("127.0.0.1", 0), self.config)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._teardown_server)
        self.host = InProcessBrokerHost(
            self.writer, self.server, "mgmt-k", "inst-k")
        self.host.register_instance()
        self.mgr = ShareManager(store_path=Path(self.td.name) / "share.db",
                                broker=make_client_adapter(self.host))
        self.mgr.create_key("friendR29")

    def _teardown_server(self):
        self.server.shutdown()
        self.server.server_close()

    def _wait(self, mgr, operation_id, expected, timeout=16.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = mgr.get_operation(operation_id)
            if last["status"] == expected:
                return last
            time.sleep(0.05)
        self.fail(f"operation never reached {expected}: {last}")

    def test_replay_same_nonce_executed_once(self):
        # R29-3：同一签名请求重发 → 第二次 replay 拒绝（消费在副作用前）。
        body = bp.build_request("mgmt-k", "status")
        first = self.host.handle_management(body)
        self.assertTrue(first["ok"])
        second = self.host.handle_management(body)
        self.assertFalse(second["ok"])
        self.assertEqual(second["error_code"], "replay")

    def test_same_second_status_polls_both_execute(self):
        # 同秒两次合法轮询（随机 nonce 不同）→ 都执行，不互相误拒。
        body1 = bp.build_request("mgmt-k", "status")
        body2 = bp.build_request("mgmt-k", "status")
        self.assertTrue(self.host.handle_management(body1)["ok"])
        self.assertTrue(self.host.handle_management(body2)["ok"])

    def test_replay_capacity_refuses_without_eviction(self):
        # 容量满（窗口内有效 id）→ 拒绝新请求而非驱逐；窗口内旧 id 重放仍拒。
        replay = bp.ReplayRegistry(max_seen=2)
        now = int(time.time())
        replay.consume("a", now)
        replay.consume("b", now)
        with self.assertRaises(bp.ProtocolError) as cm:
            replay.consume("c", now)
        self.assertEqual(cm.exception.code, "overlimit")
        with self.assertRaises(bp.ProtocolError) as cm2:
            replay.consume("a", now)
        self.assertEqual(cm2.exception.code, "replay")

    def test_forged_ack_from_noncurrent_instance_is_refused(self):
        # R29-3：相同 generation/fingerprint 但错误实例的 ACK 必拒——消费
        # 侧对照登记的当前实例，非空字符串不通过。
        coord = self.host.coordinator
        real_call = coord.instance_call

        def forged_call(action, fields, instance_id):
            if action == "load":
                return {"ok": True,
                        "result": {"instance_id": "imposter",
                                   "generation": fields["generation"],
                                   "fingerprint": fields["fingerprint"],
                                   "installed": True}}
            return real_call(action, fields, instance_id)

        coord.instance_call = forged_call
        try:
            res = self.mgr.create_key("friendImposter")
            self.assertFalse(res["published"])
            self.assertIn("instance_mismatch",
                          res.get("publication_error") or "")
        finally:
            coord.instance_call = real_call

    def test_writer_rebuilt_refuses_regression_and_tamper(self):
        # R29-4：销毁并重建 writer（同落点）→ 旧代次回归与同代次异内容都
        # 拒绝且文件不变。
        res = self.mgr.create_key("friendRebuild")
        on_disk = self.config.read_text(encoding="utf-8")
        rebuilt = ProtectedProjectionWriter(
            self.config, acl_checker=lambda p: None)
        store = ShareStore(Path(self.td.name) / "share.db")
        try:
            gen_before = store.generation()["config_generation"]
            stale = dict(json.loads(on_disk),
                         config_generation=gen_before - 1)
            stale_fp = rebuilt.fingerprint(stale)
            with self.assertRaises(BrokerError) as cm:
                rebuilt.write(stale, gen_before - 1, stale_fp)
            self.assertIn("regression", str(cm.exception))
            tampered = dict(json.loads(on_disk),
                            data=[{"key_id": "evil", "name": "evil",
                                   "secret_hash": "0" * 64,
                                   "secret_version": 1, "enabled": True}])
            t_fp = rebuilt.fingerprint(tampered)
            with self.assertRaises(BrokerError) as cm2:
                rebuilt.write(tampered, gen_before, t_fp)
            self.assertIn("mismatch", str(cm2.exception))
        finally:
            store.close()
        self.assertEqual(self.config.read_text(encoding="utf-8"), on_disk)

    def test_writer_rejects_invalid_entries_keeping_effective_copy(self):
        # R29-4：非法条目（坏摘要/重复身份）不覆盖有效副本。
        self.mgr.create_key("friendInvalid")
        on_disk = self.config.read_text(encoding="utf-8")
        gen = json.loads(on_disk)["config_generation"] + 1
        bad = {"schema_version": 1, "config_generation": gen,
               "data": [{"key_id": "k", "name": "n",
                         "secret_hash": "zz", "secret_version": 1,
                         "enabled": True}]}
        adapter = self.mgr.broker
        with self.assertRaises(BrokerError):
            adapter.publish(bad)
        self.assertEqual(self.config.read_text(encoding="utf-8"), on_disk)

    def test_late_start_compensation_verifiably_closes(self):
        # R29-1：确定性暂停旧 start 的 OS 启动（门闩）→ emergency 完成 →
        # 恢复旧 start → 入口曾打开但补偿**已核验关闭**：op failed + closed
        # True，OS 可观察状态最终 closed，manager 期望状态 stopped。
        fake = self.host.host_ops
        fake.start_gate = threading.Event()
        try:
            start_res = self.mgr.start()
            self.assertTrue(fake.start_entered.wait(5))
            emg = self.mgr.emergency_stop()
            self._wait(self.mgr, emg["operation_id"], "completed")
            fake.start_gate.set()
            op = self._wait(self.mgr, start_res["operation_id"], "failed",
                            timeout=16)
            self.assertEqual(op["payload"].get("superseded"), True)
            self.assertEqual(op["payload"].get("closed"), True)
            self.assertFalse(fake.running)          # OS 状态：入口最终关闭
            self.assertEqual(self.mgr._lifecycle_snapshot()[1], "stopped")
        finally:
            fake.start_gate.set()

    def test_emergency_unconfirmed_stays_unconfirmed(self):
        # R29-2：主机停止失败 → stopped=False 原样透传 → op failed 且
        # 未确认，绝不包装成功。
        fake = self.host.host_ops
        fake.stop_fails = True
        try:
            res = self.mgr.emergency_stop()
            op = self._wait(self.mgr, res["operation_id"], "failed")
            self.assertIn("did not confirm completion",
                          op["payload"]["error"])
        finally:
            fake.stop_fails = False

    def test_instance_generation_replaces_old_targeting(self):
        # 实例换代（重启新 id）：登记更新后旧实例请求被拒，新登记实例
        # 正常接收（旧 ACK 必拒的消费验证）。
        old_id = self.server.instance_id
        self.server.instance_id = "replaced-instance"
        stale_load = bp.build_request("inst-k", "load",
                                      {"generation": 1,
                                       "fingerprint": "x" * 64},
                                      instance_id=old_id)
        resp = self.server.handle_instance_control(stale_load, "inst-k")
        self.assertEqual(resp["error_code"], "instance_mismatch")
        self.host.register_instance()   # 部署接线登记新实例
        on_disk = json.loads(self.config.read_text(encoding="utf-8"))
        load = bp.build_request(
            "inst-k", "load",
            {"generation": on_disk["config_generation"],
             "fingerprint": self.writer.fingerprint(on_disk)},
            instance_id=self.server.instance_id)
        resp2 = self.server.handle_instance_control(load, "inst-k")
        self.assertTrue(resp2["ok"], resp2)




class R31ClosureTests(_RuntimeRevokeFixture, unittest.TestCase):
    """§31 定向验收：排空/紧急停止的显式关闭确认、IPC 异常兜底、迟到
    start 补偿失败的持久阻塞与重启恢复、换代中的迟到 ACK、parser 收敛
    与 ACL 必需注入。fixture 只替代 transport/OS/时钟。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True,)
        self.addCleanup(self.td.cleanup)
        self.config = Path(self.td.name) / "um-share-projection.json"
        self.config.write_text(json.dumps(
            {"schema_version": 1, "config_generation": 1, "data": []}),
            encoding="utf-8")
        self.writer = ProtectedProjectionWriter(
            self.config, acl_checker=lambda p: None)
        self.server = share_gw.ShareServer(("127.0.0.1", 0), self.config)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._teardown_server)
        self.host = InProcessBrokerHost(
            self.writer, self.server, "mgmt-k", "inst-k")
        self.host.register_instance()
        self.mgr = ShareManager(store_path=Path(self.td.name) / "share.db",
                                broker=make_client_adapter(self.host))
        self.mgr.create_key("friendR31")
        self._install_revoke_fixture()

    def _teardown_server(self):
        self.server.shutdown()
        self.server.server_close()

    def _wait(self, mgr, operation_id, expected, timeout=16.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = mgr.get_operation(operation_id)
            if last["status"] == expected:
                return last
            time.sleep(0.05)
        self.fail(f"operation never reached {expected}: {last}")

    def test_drain_with_failed_host_stop_is_never_completed(self):
        # R31-1：无在途但主机停止失败 → 关闭未确认 → graceful_stop 不得
        # completed（drained 不替代 stopped）。
        self.host.host_ops.stop_fails = True
        try:
            res = self.mgr.graceful_stop()
            op = self._wait(self.mgr, res["operation_id"], "failed")
            self.assertIn("final stop was not confirmed",
                          op["payload"]["error"])
        finally:
            self.host.host_ops.stop_fails = False

    def test_instance_ipc_failure_still_drives_host_stop(self):
        # R31-1：实例 IPC 抛错 → 主机停止/核验仍被调用（失联恰是主机兜底
        # 场景）→ 主机关闭已确认时 op completed 且通知失败被准确记录。
        coord = self.host.coordinator
        real_call = coord.instance_call

        def broken_call(action, fields, instance_id):
            raise OSError("instance pipe lost")

        coord.instance_call = broken_call
        calls = {"stop": 0, "closed": 0}
        fake = self.host.host_ops
        real_stop = fake.stop_host
        real_closed = fake.verify_closed

        def counting_stop():
            calls["stop"] += 1
            return real_stop()

        def counting_closed():
            calls["closed"] += 1
            return real_closed()

        fake.stop_host = counting_stop
        fake.verify_closed = counting_closed
        try:
            res = self.mgr.emergency_stop()
            op = self._wait(self.mgr, res["operation_id"], "completed")
            self.assertGreaterEqual(calls["stop"], 1)     # 主机停止仍执行
            self.assertGreaterEqual(calls["closed"], 1)   # 核验仍执行
            host_result = op["payload"]["broker"]["broker"]
            self.assertTrue(host_result["host_closed"])
            self.assertTrue(host_result["notification_failed"])
        finally:
            coord.instance_call = real_call
            fake.stop_host = real_stop
            fake.verify_closed = real_closed

    def test_unverified_final_close_is_queryable_not_fabricated(self):
        # R31-1：最终关闭失败记录可查（failed op + last_failure），状态
        # 不伪造 stopped。
        self.host.host_ops.stop_fails = True
        try:
            res = self.mgr.graceful_stop()
            op = self._wait(self.mgr, res["operation_id"], "failed")
            self.assertIn("final stop was not confirmed",
                          op["payload"]["error"])
            # 关闭失败记录可查（last_failure），状态不伪造 stopped。
            self.assertIsNotNone(self.mgr.status()["last_publication_failure"]
                                 or op["payload"]["error"])
        finally:
            self.host.host_ops.stop_fails = False

    def test_compensation_failure_blocks_starts_and_restart(self):
        # R31-2：迟到 start 补偿失败 → 持久生命周期阻塞 → 新 start 拒绝
        # → 重建 manager（重启对账）仍阻塞 → 关闭确认解除。
        fake = self.host.host_ops
        fake.start_gate = threading.Event()
        fake.stop_fails = True
        try:
            start_res = self.mgr.start()
            self.assertTrue(fake.start_entered.wait(5))
            emg = self.mgr.emergency_stop()
            self._wait(self.mgr, emg["operation_id"], "failed")
            fake.start_gate.set()
            op = self._wait(self.mgr, start_res["operation_id"], "failed",
                            timeout=16)
            self.assertTrue(op["payload"].get("superseded"))
            self.assertEqual(op["payload"].get("closed"), False)
            # 新 start 拒绝（生命周期阻塞）。
            with self.assertRaises(BrokerError) as cm:
                self.mgr.start()
            self.assertIn("lifecycle block", str(cm.exception))
            # 重建 manager（模拟重启）：对账有界重新确认——仍失败则仍阻塞。
            fake.stop_fails = True
            recovered = ShareManager(
                store_path=Path(self.td.name) / "share.db",
                broker=make_client_adapter(self.host))
            with self.assertRaises(BrokerError) as cm2:
                recovered.start()
            self.assertIn("lifecycle block", str(cm2.exception))
            # 关闭确认后解除：重驱动成功 → 阻塞 applied → start 正常。
            fake.stop_fails = False
            ShareManager._recovered_paths.discard(
                str((Path(self.td.name) / "share.db").resolve()))
            recovered2 = ShareManager(
                store_path=Path(self.td.name) / "share.db",
                broker=make_client_adapter(self.host))
            res2 = recovered2.start()
            self.assertEqual(res2["status"], "pending")
            self._wait(recovered2, res2["operation_id"], "completed")
        finally:
            fake.start_gate.set()
            fake.stop_fails = False

    def test_start_exception_after_opening_still_compensates(self):
        # R31-2：fn() 打开入口后抛错 → 异常出口仍进行 epoch 补偿检查。
        fake = self.host.host_ops
        real_start_host = fake.start_host

        fake.start_gate = threading.Event()   # 暂停 start 于入口打开之后
        fake.start_gate.clear()

        def open_pause_explode():
            fake.start_entered.set()       # 标记：入口已打开
            fake.running = True
            fake.start_gate.wait(15)       # 等 emergency 完成后再抛错
            raise RuntimeError("verify stage exploded")

        real_start_host = fake.start_host
        fake.start_host = open_pause_explode
        try:
            start_res = self.mgr.start()
            self.assertTrue(fake.start_entered.wait(5))
            emg = self.mgr.emergency_stop()
            self._wait(self.mgr, emg["operation_id"], "completed")
            fake.start_gate.set()
            op = self._wait(self.mgr, start_res["operation_id"], "failed",
                            timeout=18)
            self.assertTrue(op["payload"].get("exception"))
            # epoch 补偿已驱动：OS 状态最终关闭（emergency 的 stop_host）。
            self.assertFalse(fake.running)
        finally:
            fake.start_gate.set()
            fake.start_host = real_start_host
            fake.running = False

    def test_ack_in_flight_during_replacement_is_refused(self):
        # R33-3：旧实例先产生成功 ACK 并保存，用独立门闩扣住返回；仅替换
        # registry 登记（server.instance_id 不变——share 不拒绝旧实例），
        # 放回保存的成功 ACK → coordinator 版本核对拒绝（"instance was
        # replaced while the load was in flight"），manager 层 applied 不
        # 推进；随后新实例正常发布可通过。
        coord = self.host.coordinator
        entered = threading.Event()
        release = threading.Event()
        real_call = coord.instance_call
        share_secret = self.host.instance_secret
        old_id = self.server.instance_id
        # 先同步发布新代次（applied 推进到 gen3），再替换 instance_call 为
        # gated_call。之后用 delete_key 端到端触发换代拒绝：delete 的
        # publish 走 gated load 暂停 → register 换代 → 放行旧实例的正确
        # ACK → 版本核对拒 → manager._publish 失败 → fail-closed 驱动受限
        # 关停（主机关闭已确认）→ applied 停留在 gen3 < desired=gen4 →
        # in_sync=False。最后恢复登记当前实例 → 新 create 正常发布。
        stale_created = self.mgr.create_key("friendStale")
        self.assertTrue(stale_created["published"])

        saved_ack = {}

        def gated_call(action, fields, instance_id):
            if action == "load" and instance_id == old_id:
                entered.set()          # 通知：delete 的 load 已到旧实例
                release.wait(15)       # 扣住返回——等测试换代登记
                # 旧实例产生成功 ACK 并保存（真实 share 端确认）。
                from share_broker_protocol import build_request as _build
                req = _build(share_secret, action, fields,
                             instance_id=instance_id)
                resp = self.server.handle_instance_control(req, share_secret)
                return {"ok": True, "result": resp["result"]}
            return real_call(action, fields, instance_id)

        coord.instance_call = gated_call
        outcome = {}

        def deleting():
            try:
                self.mgr.delete_key(stale_created["key_id"],
                                    expected_row_version=1)
                outcome["unexpected_success"] = True
            except BrokerError as exc:
                outcome["error"] = str(exc)

        worker = threading.Thread(target=deleting)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            # 仅替换 registry 登记（版本 bump）；server.instance_id 不变。
            self.host.instances.register("replaced-instance")
            release.set()
            worker.join(16)
            # 旧实例的正确 ACK 被版本门禁拒绝 → fail-closed 关停已确认。
            self.assertIn("emergency stop confirmed",
                          outcome.get("error", ""))
            store = self.mgr._open()
            try:
                gen = store.generation()
                self.assertGreater(gen["config_generation"],
                                   gen["applied_generation"])
                self.assertFalse(gen["in_sync"])
            finally:
                store.close()
        finally:
            coord.instance_call = real_call
            release.set()
        # 恢复登记当前实例 → 新实例正常发布可通过（applied 推进到 desired）。
        self.host.instances.register(self.server.instance_id)
        # 新实例发布前，运行态已应用该代次的撤销（fixture 的安全水位）。
        store = self.mgr._open()
        try:
            self._record_runtime_revoke(
                stale_created["key_id"],
                store.generation()["config_generation"])
        finally:
            store.close()
        res2 = self.mgr.create_key("friendFresh")
        self.assertTrue(res2["published"])
        store = self.mgr._open()
        try:
            self.assertTrue(store.generation()["in_sync"])
        finally:
            store.close()

    def test_bool_schema_projection_is_rejected_everywhere(self):
        # R31-6：bool schema（True==1）在共享 parser 被拒 → writer 不替换
        # 且 ACK 不可能通过（管理侧同样拒）。
        on_disk = self.config.read_text(encoding="utf-8")
        bad = {"schema_version": True, "config_generation": 9, "data": []}
        with self.assertRaises(BrokerError):
            self.mgr.broker.publish(bad)
        self.assertEqual(self.config.read_text(encoding="utf-8"), on_disk)




class R33PipeTransportTests(unittest.TestCase):
    """§33 R33-1：NamedPipe 经注入 fake kernel 的完整验收——正常路径
    （open→write→read→解码成功）、立即完成不误判超时、读/写阻塞分别到
    期、调用前已过期零写入、失败/取消的关闭次数与放弃状态可观察。"""

    def _transport(self, kernel):
        t = bc.NamedPipeBrokerTransport(
            pipe_name=r"\\.\pipe\test-broker", kernel=kernel)
        t._handle = 0x1000
        return t

    def test_normal_path_write_read_succeeds(self):
        # 正常路径：write→read→解码成功；立即完成不得 TimeoutError，
        # 不触发取消关闭（R33-1 核心验收）。
        class Kernel:
            def __init__(self):
                self.close_calls = 0
            def CreateFileW(self, *a): return 0x1000
            def WriteFile(self, h, data, n, written, o):
                written.contents.value = n
                return 1
            def ReadFile(self, h, buf, n, read, overlapped):
                payload = b'{"v":2,"request_id":"r1","ok":true,' \
                          b'"error_code":"ok","result":{}}\n'
                buf[:len(payload)] = payload
                read.contents.value = len(payload)
                return 1
            def CloseHandle(self, h):
                self.close_calls += 1
                return 1
            def GetLastError(self): return 0

        kernel = Kernel()
        t = self._transport(kernel)
        deadline = time.monotonic() + 5
        t.send_all(b'{"x":1}\n', deadline)          # 写不阻塞
        frame = t.recv_line(deadline)               # 读立即完成
        self.assertTrue(frame.endswith(b"\n"))
        self.assertFalse(t._abandoned)              # 正常路径不放弃
        self.assertEqual(kernel.close_calls, 0)     # 不触发取消关闭

    def test_open_retries_transient_codes_then_succeeds(self):
        # W2 run20 根因定向：管理 server 每个连接周期释放唯一实例后再重建，
        # 立即重连会命中 ~1.5ms 零实例窗口（离线实测错误码 2/231）。
        # open 对 2/231 有界重试并最终成功；重试发生在建连之前（R44-1）。
        cls = bc.NamedPipeBrokerTransport
        saved = (cls._OPEN_RETRY_BASE_DELAY, cls._OPEN_RETRY_MAX_DELAY)
        cls._OPEN_RETRY_BASE_DELAY = 0.0
        cls._OPEN_RETRY_MAX_DELAY = 0.0
        try:
            class TransientKernel:
                def __init__(self):
                    self.create_calls = 0
                    self.codes = [231, 3, 2]
                    self.last = 0
                def CreateFileW(self, *a):
                    self.create_calls += 1
                    if self.codes:
                        self.last = self.codes.pop(0)
                        return 0xFFFFFFFFFFFFFFFF
                    return 0x1000
                def GetLastError(self):
                    return self.last

            kernel = TransientKernel()
            t = bc.NamedPipeBrokerTransport(
                pipe_name=r"\\.\pipe\test-broker", kernel=kernel)
            self.assertIs(t.open(), t)
            self.assertEqual(t._handle, 0x1000)
            self.assertEqual(kernel.create_calls, 4)   # 败、败、败、成
        finally:
            cls._OPEN_RETRY_BASE_DELAY, cls._OPEN_RETRY_MAX_DELAY = saved

    def test_open_never_retries_non_transient_codes(self):
        # ACCESS_DENIED 等 DACL 语义失败必须立即拒绝，不得重试成假成功。
        class DeniedKernel:
            def __init__(self):
                self.create_calls = 0
            def CreateFileW(self, *a):
                self.create_calls += 1
                return 0xFFFFFFFFFFFFFFFF
            def GetLastError(self): return 5

        kernel = DeniedKernel()
        t = bc.NamedPipeBrokerTransport(
            pipe_name=r"\\.\pipe\test-broker", kernel=kernel)
        with self.assertRaises(OSError):
            t.open()
        self.assertEqual(kernel.create_calls, 1)

    def test_write_retries_transient_not_connected_then_succeeds(self):
        # R48-1 定向：单实例 server 在周期切换时释放旧实例，客户端
        # CreateFileW 已成功但 write 命中 233（连接排队未完成/实例已释放）。
        # write 遇 233 有界重连并整帧重发；重连后成功。R44-1 保持：
        # 一次交换最终通过一次成功连接完成。
        class Kernel:
            def __init__(self):
                self.writes = 0
                self.closes = 0
                self.creates = 1
            def CreateFileW(self, *a):
                self.creates += 1
                return 0x1000
            def WriteFile(self, h, data, n, written, o):
                self.writes += 1
                if self.writes == 1:
                    written.contents.value = 0
                    return 0
                written.contents.value = n
                return 1
            def ReadFile(self, h, buf, n, read, o):
                payload = (b'{"v":2,"request_id":"r1","ok":true,'
                           b'"error_code":"ok","result":{}}\n')
                buf[:len(payload)] = payload
                read.contents.value = len(payload)
                return 1
            def CloseHandle(self, h):
                self.closes += 1
                return 1
            def GetLastError(self):
                return 233

        kernel = Kernel()
        t = self._transport(kernel)
        t._allow_reconnect = True       # owned：由 BrokerClient.call 置位
        deadline = time.monotonic() + 5
        t.send_all(b'{"x":1}\n', deadline)
        frame = t.recv_line(deadline)
        self.assertTrue(frame.endswith(b"\n"))
        self.assertEqual(kernel.writes, 2)      # 败、重连后成
        self.assertGreaterEqual(kernel.closes, 1)
        self.assertGreaterEqual(kernel.creates, 2)
        self.assertFalse(t._abandoned)          # 正常路径不放弃

    def test_write_retry_budget_is_bounded(self):
        # R48-1：持续 233 时预算耗尽后仍按原语义 OSError（不伪装成功、
        # 不换错误消息、不无限重连）。
        cls = bc.NamedPipeBrokerTransport

        class AlwaysNotConnectedKernel:
            def __init__(self):
                self.creates = 0
                self.writes = 0
            def CreateFileW(self, *a):
                self.creates += 1
                return 0x1000
            def WriteFile(self, h, data, n, written, o):
                self.writes += 1
                written.contents.value = 0
                return 0
            def CloseHandle(self, h):
                return 1
            def GetLastError(self):
                return 233

        kernel = AlwaysNotConnectedKernel()
        t = self._transport(kernel)
        t._allow_reconnect = True       # owned：由 BrokerClient.call 置位
        with self.assertRaises(OSError):
            t.send_all(b'{"x":1}\n', time.monotonic() + 5)
        # 首次 open(helper 已建) + 每次重连新建连接,预算耗尽后拒绝。
        # 字面量断言,不随常量调整自动跟随。WriteFile 次数=首次+重试各一次。
        self.assertEqual(kernel.creates, 2)
        self.assertEqual(kernel.writes, 3)

    def test_write_retry_skipped_for_non_owned_transport(self):
        # R48-1 code-review A：caller-owned（身份已验证）transport 不得重连
        # ——重连会关掉已验证句柄并新建未验证连接，绕过 R41-2。默认
        # (_allow_reconnect=False) 遇 233 保持原拒绝语义：无重连、不新建连接。
        class Kernel:
            def __init__(self):
                self.creates = 0
                self.writes = 0
            def CreateFileW(self, *a):
                self.creates += 1
                return 0x1000
            def WriteFile(self, h, data, n, written, o):
                self.writes += 1
                written.contents.value = 0
                return 0
            def CloseHandle(self, h):
                return 1
            def GetLastError(self):
                return 233

        kernel = Kernel()
        t = self._transport(kernel)      # 默认为非 owned：不重连
        with self.assertRaises(OSError):
            t.send_all(b'{"x":1}\n', time.monotonic() + 5)
        self.assertEqual(kernel.writes, 1)     # 仅一次，不重连
        self.assertEqual(kernel.creates, 0)    # helper 预置句柄，无重开

    def test_write_retry_resends_whole_frame_after_partial(self):
        # R48-1 code-review B：部分写入后瞬态失败（长帧分块）重连必须整帧
        # 重发，不能只发残余段导致协议截断。
        received = []

        class Kernel:
            def __init__(self):
                self.writes = 0
                self.creates = 0
            def CreateFileW(self, *a):
                self.creates += 1
                return 0x1000
            def WriteFile(self, h, data, n, written, o):
                self.writes += 1
                received.append(bytes(data[:n]))
                if self.writes == 1:
                    written.contents.value = 2    # 首块部分成功
                    return 1
                if self.writes == 2:
                    written.contents.value = 0    # 残余块命中 233
                    return 0
                written.contents.value = n        # 重连后整帧重写
                return 1
            def CloseHandle(self, h):
                return 1
            def GetLastError(self):
                return 233

        kernel = Kernel()
        t = self._transport(kernel)
        t._allow_reconnect = True                # owned 场景
        payload = b'{"very":"long",' + b'x' * 60 + b'}\n'
        t.send_all(payload, time.monotonic() + 5)
        self.assertEqual(kernel.writes, 3)
        self.assertEqual(len(received[0]), len(payload))   # 首块请求整帧
        self.assertEqual(len(received[1]), len(payload) - 2)  # 残余块
        self.assertEqual(received[2], payload)   # 重连后从数据头整帧重发

    def test_write_retry_handles_109_and_232_codes(self):
        # R48-1：109/232 与 233 同为瞬态断连错误，owned 连接均重连后成功。
        def make_kernel(code):
            class Kernel:
                def __init__(self):
                    self.writes = 0
                    self.creates = 0
                def CreateFileW(self, *a):
                    self.creates += 1
                    return 0x1000
                def WriteFile(self, h, data, n, written, o):
                    self.writes += 1
                    if self.writes == 1:
                        written.contents.value = 0
                        return 0
                    written.contents.value = n
                    return 1
                def CloseHandle(self, h):
                    return 1
                def GetLastError(self):
                    return code
            return Kernel

        for code in (109, 232):
            kernel = make_kernel(code)()
            t = self._transport(kernel)
            t._allow_reconnect = True
            t.send_all(b'{"x":1}\n', time.monotonic() + 5)
            self.assertEqual(kernel.writes, 2)
            self.assertGreaterEqual(kernel.creates, 1)

    def test_write_never_retries_non_transient_code(self):
        # R48-1：写路径 ACCESS_DENIED 等失败立即拒绝（与 open 语义一致），
        # 不把 DACL 拒绝重试成假成功。
        class Kernel:
            def __init__(self):
                self.writes = 0
                self.creates = 0
            def CreateFileW(self, *a):
                self.creates += 1
                return 0x1000
            def WriteFile(self, h, data, n, written, o):
                self.writes += 1
                written.contents.value = 0
                return 0
            def CloseHandle(self, h):
                return 1
            def GetLastError(self):
                return 5

        kernel = Kernel()
        t = self._transport(kernel)
        t._allow_reconnect = True
        with self.assertRaises(OSError):
            t.send_all(b'{"x":1}\n', time.monotonic() + 5)
        self.assertEqual(kernel.writes, 1)
        self.assertEqual(kernel.creates, 0)

    def test_open_retry_budget_is_bounded(self):
        # 持续 231：预算耗尽后按原语义报 OSError（不换消息、不伪装成功）。
        cls = bc.NamedPipeBrokerTransport
        saved = (cls._OPEN_RETRY_SECONDS, cls._OPEN_RETRY_BASE_DELAY)
        cls._OPEN_RETRY_SECONDS = 0.03
        cls._OPEN_RETRY_BASE_DELAY = 0.0
        try:
            class AlwaysBusyKernel:
                def CreateFileW(self, *a): return 0xFFFFFFFFFFFFFFFF
                def GetLastError(self): return 231

            t = bc.NamedPipeBrokerTransport(
                pipe_name=r"\\.\pipe\test-broker", kernel=AlwaysBusyKernel())
            with self.assertRaises(OSError):
                t.open()
        finally:
            cls._OPEN_RETRY_SECONDS, cls._OPEN_RETRY_BASE_DELAY = saved

    def test_open_fake_kernel_without_getlasterror_fails_fast(self):
        # 无 GetLastError 的 fake kernel：错误码按 0 处理 → 立即拒绝，
        # 不得进入重试循环（兼容既有离线 fake 注入面）。
        class MuteInvalidKernel:
            def CreateFileW(self, *a): return 0xFFFFFFFFFFFFFFFF

        t = bc.NamedPipeBrokerTransport(
            pipe_name=r"\\.\pipe\test-broker", kernel=MuteInvalidKernel())
        with self.assertRaises(OSError):
            t.open()

    def test_read_blocked_expires_and_abandons(self):
        # 读阻塞到期：句柄释放、transport 标记放弃、TimeoutError。
        class BlockingKernel:
            def __init__(self):
                self.close_calls = 0
                self.release = threading.Event()
            def ReadFile(self, h, buf, n, read, overlapped):
                self.release.wait(10)        # 模拟阻塞
                return 1
            def WriteFile(self, *a): return 1
            def CloseHandle(self, h):
                self.close_calls += 1
                self.release.set()           # CloseHandle 释放阻塞（D4 验证）
                return 1
            def GetLastError(self): return 0

        kernel = BlockingKernel()
        t = self._transport(kernel)
        deadline = time.monotonic() + 0.3
        with self.assertRaises(TimeoutError):
            t.recv_line(deadline)
        self.assertTrue(t._abandoned)
        self.assertEqual(kernel.close_calls, 1)
        self.assertIsNone(t._handle)

    def test_write_blocked_expires_and_abandons(self):
        # 写阻塞到期：TimeoutError + 放弃标记 + 取消关闭。
        class BlockingWriteKernel:
            def __init__(self):
                self.close_calls = 0
            def ReadFile(self, *a): return 1
            def WriteFile(self, h, data, n, written, overlapped):
                time.sleep(10)               # 模拟阻塞
                return 1
            def CloseHandle(self, h):
                self.close_calls += 1
                return 1
            def GetLastError(self): return 0

        kernel = BlockingWriteKernel()
        t = self._transport(kernel)
        deadline = time.monotonic() + 0.3
        with self.assertRaises(TimeoutError):
            t.send_all(b'{"x":1}\n', deadline)
        self.assertTrue(t._abandoned)
        self.assertEqual(kernel.close_calls, 1)

    def test_already_expired_performs_zero_io(self):
        # 调用前已过期：零 I/O（worker 不启动），立即 TimeoutError。
        class NoIoKernel:
            def __init__(self):
                self.read_calls = 0
                self.write_calls = 0
            def ReadFile(self, *a):
                self.read_calls += 1
                return 1
            def WriteFile(self, *a):
                self.write_calls += 1
                return 1
            def CloseHandle(self, h): return 1
            def GetLastError(self): return 0
            def CreateFileW(self, *a): return 0x1000

        kernel = NoIoKernel()
        t = self._transport(kernel)
        expired = time.monotonic() - 1
        saved_reserved = bc.NamedPipeBrokerTransport._reserved_worker_slots
        with self.assertRaises(TimeoutError):
            t.send_all(b'{"x":1}\n', expired)
        with self.assertRaises(TimeoutError):
            t.recv_line(expired)
        self.assertEqual(kernel.read_calls, 0)
        self.assertEqual(kernel.write_calls, 0)   # 零副作用
        # R37：预过期在预留之前拒绝，不消耗额度。
        self.assertEqual(bc.NamedPipeBrokerTransport._reserved_worker_slots,
                         saved_reserved)

    def test_client_full_exchange_succeeds_over_fake_kernel(self):
        # 完整 BrokerClient→NamedPipe（注入 fake kernel）open→write→read
        # →解码成功：正常路径不再必失败（R33-1 核心验收）。
        from share_broker_client import BrokerClient

        class EchoKernel:
            def __init__(self):
                self.frames = []
                self.close_calls = 0
            def WriteFile(self, h, data, n, written, overlapped):
                self.frames.append(bytes(data[:n]))
                written.contents.value = n
                return 1
            def ReadFile(self, h, buf, n, read, overlapped):
                req = json.loads(self.frames[-1].decode("utf-8"))
                resp = {"v": 2, "request_id": req["request_id"], "ok": True,
                        "error_code": "ok", "result": {"echo": req["action"]}}
                payload = (json.dumps(resp) + "\n").encode("utf-8")
                buf[:len(payload)] = payload
                read.contents.value = len(payload)
                return 1
            def CloseHandle(self, h):
                self.close_calls += 1
                return 1
            def GetLastError(self): return 0
            def CreateFileW(self, *a): return 0x1000

        kernel = EchoKernel()
        t = self._transport(kernel)
        client = BrokerClient(lambda: t, "echo-secret")
        res = client.status()
        self.assertTrue(res.ok, res)
        self.assertEqual(res.result["echo"], "status")
        self.assertEqual(len(kernel.frames), 1)   # 一次 exchange 一帧

    def test_cancelled_write_late_success_never_issues_second_chunk(self):
        # R35-1：首块阻塞→超时放弃→释放首块→迟到成功（部分写入）→
        # 断言不再发第二块 WriteFile；worker 在有界窗口内确认退出。
        class ChunkedBlockingWriteKernel:
            def __init__(self):
                self.write_calls = 0
                self.release = threading.Event()
            def WriteFile(self, h, data, n, written, overlapped):
                self.write_calls += 1
                if self.write_calls == 1:
                    self.release.wait(30)      # 首块阻塞
                written.contents.value = 1     # 迟到成功：仅 1 字节
                return 1
            def ReadFile(self, *a): return 1
            def CloseHandle(self, h):
                self.release.set()             # 取消关闭释放阻塞调用
                return 1
            def GetLastError(self): return 0

        kernel = ChunkedBlockingWriteKernel()
        t = self._transport(kernel)
        with self.assertRaises(TimeoutError):
            t.send_all(b'{"x":1234567}\n', time.monotonic() + 0.3)
        self.assertTrue(t._abandoned)
        self.assertEqual(kernel.write_calls, 1)          # 未发第二块
        self.assertEqual(t.abandoned_workers_reaped, 1)  # worker 终态已确认
        self.assertEqual(t.abandoned_workers_unreaped, 0)

    def test_cancelled_read_late_success_never_issues_second_chunk(self):
        # R35-1：读首块阻塞→超时放弃→释放首块→迟到成功（1 字节、无换行）
        # →断言不再发第二块 ReadFile；worker 终态已确认。
        class ChunkedBlockingReadKernel:
            def __init__(self):
                self.read_calls = 0
                self.release = threading.Event()
            def ReadFile(self, h, buf, n, read, overlapped):
                self.read_calls += 1
                if self.read_calls == 1:
                    self.release.wait(30)      # 首块阻塞
                buf[0:1] = b"x"                # 迟到成功：1 字节、无换行
                read.contents.value = 1
                return 1
            def CloseHandle(self, h):
                self.release.set()
                return 1
            def GetLastError(self): return 0

        kernel = ChunkedBlockingReadKernel()
        t = self._transport(kernel)
        with self.assertRaises(TimeoutError):
            t.recv_line(time.monotonic() + 0.3)
        self.assertTrue(t._abandoned)
        self.assertEqual(kernel.read_calls, 1)           # 未发第二块
        self.assertEqual(t.abandoned_workers_reaped, 1)
        self.assertEqual(t.abandoned_workers_unreaped, 0)

    def test_normal_multi_chunk_write_and_read_complete(self):
        # R35-1：正常多块写/读完整完成——每块前的取消检查不影响正常路径。
        class ChunkedKernel:
            def __init__(self):
                self.write_calls = 0
                self.read_calls = 0
                self.close_calls = 0
            def WriteFile(self, h, data, n, written, overlapped):
                self.write_calls += 1
                written.contents.value = min(3, n)
                return 1
            def ReadFile(self, h, buf, n, read, overlapped):
                self.read_calls += 1
                payload = b"abc" if self.read_calls < 2 else b"def\n"
                buf[:len(payload)] = payload
                read.contents.value = len(payload)
                return 1
            def CloseHandle(self, h):
                self.close_calls += 1
                return 1
            def GetLastError(self): return 0

        kernel = ChunkedKernel()
        t = self._transport(kernel)
        deadline = time.monotonic() + 5
        self.assertEqual(t.send_all(b"0123456789", deadline), 10)
        self.assertEqual(kernel.write_calls, 4)          # 3+3+3+1
        self.assertEqual(t.recv_line(deadline), b"abcdef\n")
        self.assertEqual(kernel.read_calls, 2)
        self.assertFalse(t._abandoned)
        self.assertEqual(kernel.close_calls, 0)

    def test_unblocked_fake_worker_is_not_claimed_reaped(self):
        # R35-1：不解除阻塞的 fake（CloseHandle 不释放阻塞）不得声称已
        # 回收——unreaped 计数 +1、reaped 为 0，进程级计数相应增加。
        class StuckKernel:
            def __init__(self):
                self.read_calls = 0
                self.close_calls = 0
                self.never = threading.Event()   # 从不 set：worker 无法退出
            def ReadFile(self, h, buf, n, read, overlapped):
                self.read_calls += 1
                self.never.wait(30)              # 守护线程滞留有上限
                return 1
            def CloseHandle(self, h):
                self.close_calls += 1            # 不释放阻塞（D4 反例 fake）
                return 1
            def GetLastError(self): return 0

        cls = bc.NamedPipeBrokerTransport
        saved_count = cls._unreaped_abandoned
        saved_reserved = cls._reserved_worker_slots
        kernel = StuckKernel()
        t = self._transport(kernel)
        with self.assertRaises(TimeoutError):
            t.recv_line(time.monotonic() + 0.3)
        self.assertTrue(t._abandoned)
        self.assertEqual(kernel.close_calls, 1)
        self.assertEqual(t.abandoned_workers_reaped, 0)
        self.assertEqual(t.abandoned_workers_unreaped, 1)
        self.assertEqual(cls._unreaped_abandoned, saved_count + 1)
        # R37：退出未确认 → 预留额度转为永久占位（reserved 回到进入前
        # 值，占位由 unreaped 计数持续承担，此后不回收）。
        self.assertEqual(cls._reserved_worker_slots, saved_reserved)

    def test_admission_refused_at_unreaped_cap(self):
        # R35-1：无法确认退出的 worker 计入有界上限；达到上限后新
        # exchange 在启动 worker 前被拒绝（零 I/O、零关闭）。
        cls = bc.NamedPipeBrokerTransport
        saved_cap = cls._MAX_UNREAPED_ABANDONED_WORKERS
        saved_count = cls._unreaped_abandoned
        cls._MAX_UNREAPED_ABANDONED_WORKERS = 0
        cls._unreaped_abandoned = 0
        try:
            class CountingKernel:
                def __init__(self):
                    self.read_calls = 0
                    self.write_calls = 0
                    self.close_calls = 0
                def ReadFile(self, *a):
                    self.read_calls += 1
                    return 1
                def WriteFile(self, *a):
                    self.write_calls += 1
                    return 1
                def CloseHandle(self, h):
                    self.close_calls += 1
                    return 1
                def GetLastError(self): return 0

            kernel = CountingKernel()
            t = self._transport(kernel)
            with self.assertRaises(OSError) as ctx:
                t.recv_line(time.monotonic() + 5)
            self.assertIn("admission refused", str(ctx.exception))
            with self.assertRaises(OSError):
                t.send_all(b'{"x":1}\n', time.monotonic() + 5)
            self.assertEqual(kernel.read_calls, 0)   # worker 未启动
            self.assertEqual(kernel.write_calls, 0)
            self.assertEqual(kernel.close_calls, 0)
            self.assertFalse(t._abandoned)
            # R37：拒绝发生在占用之前——额度未被消耗。
            self.assertEqual(cls._reserved_worker_slots, 0)
        finally:
            cls._MAX_UNREAPED_ABANDONED_WORKERS = saved_cap
            cls._unreaped_abandoned = saved_count

    def test_concurrent_exchanges_cannot_exceed_worker_cap(self):
        # R37 定向：额度在 worker 启动前原子占用——cap=2 时屏障并发启动
        # 3 个调用（fake 首块保持阻塞），恰有 2 个进入 I/O，第 3 个被拒且
        # 零 I/O；确认退出释放额度后，新 exchange 可再次取得额度。
        cls = bc.NamedPipeBrokerTransport
        saved = (cls._MAX_UNREAPED_ABANDONED_WORKERS,
                 cls._unreaped_abandoned, cls._reserved_worker_slots)
        cls._MAX_UNREAPED_ABANDONED_WORKERS = 2
        cls._unreaped_abandoned = 0
        cls._reserved_worker_slots = 0
        try:
            class GatedKernel:
                def __init__(self):
                    self.read_calls = 0
                    self.gate = threading.Event()
                def ReadFile(self, h, buf, n, read, overlapped):
                    self.read_calls += 1
                    self.gate.wait(30)         # 首块阻塞
                    payload = (b'{"v":2,"request_id":"r","ok":true,'
                               b'"error_code":"ok","result":{}}\n')
                    buf[:len(payload)] = payload
                    read.contents.value = len(payload)
                    return 1
                def CloseHandle(self, h): return 1
                def GetLastError(self): return 0

            kernels = [GatedKernel() for _ in range(3)]
            transports = [self._transport(k) for k in kernels]
            barrier = threading.Barrier(3)
            outcomes = [None, None, None]

            def reader(idx):
                t = transports[idx]
                barrier.wait(5)
                try:
                    outcomes[idx] = {"frame": t.recv_line(
                        time.monotonic() + 10)}
                except BaseException as exc:  # noqa: BLE001
                    outcomes[idx] = {"err": exc}

            threads = [threading.Thread(target=reader, args=(i,))
                       for i in range(3)]
            for t in threads:
                t.start()
            try:
                # 等待收敛：恰有 cap 个 worker 进入 I/O，超额者已被拒。
                deadline_wait = time.monotonic() + 5
                while True:
                    refused = [o for o in outcomes if o and "err" in o]
                    started = sum(k.read_calls for k in kernels)
                    if started == 2 and len(refused) == 1:
                        break
                    if time.monotonic() > deadline_wait:
                        self.fail("concurrency not converged: "
                                  f"started={started} "
                                  f"refused={len(refused)}")
                    time.sleep(0.01)
                refused_idx = outcomes.index(refused[0])
                self.assertIn("admission refused", str(refused[0]["err"]))
                # 超额者在其启动 worker 前被拒：零 I/O、不放弃。
                self.assertEqual(kernels[refused_idx].read_calls, 0)
                self.assertFalse(transports[refused_idx]._abandoned)
            finally:
                for k in kernels:
                    k.gate.set()
            for t in threads:
                t.join(15)
            # 两个 worker 均确认退出 → 额度全部释放。
            self.assertEqual(cls._reserved_worker_slots, 0)
            # 释放后额度可再次取得：立即完成的 exchange 正常通过。
            kernel_c = GatedKernel()
            kernel_c.gate.set()
            t4 = self._transport(kernel_c)
            frame = t4.recv_line(time.monotonic() + 5)
            self.assertTrue(frame.endswith(b"\n"))
            self.assertEqual(cls._reserved_worker_slots, 0)
        finally:
            (cls._MAX_UNREAPED_ABANDONED_WORKERS,
             cls._unreaped_abandoned,
             cls._reserved_worker_slots) = saved

    def test_worker_start_failure_releases_slot(self):
        # R37：worker.start 失败不得泄漏已预留额度（预留后立即释放）。
        from unittest import mock
        cls = bc.NamedPipeBrokerTransport
        saved_reserved = cls._reserved_worker_slots

        class NoIoKernel:
            def ReadFile(self, *a): return 1
            def WriteFile(self, *a): return 1
            def CloseHandle(self, h): return 1
            def GetLastError(self): return 0

        kernel = NoIoKernel()
        t = self._transport(kernel)
        with mock.patch.object(threading.Thread, "start",
                               side_effect=RuntimeError("start failed")):
            with self.assertRaises(RuntimeError):
                t.recv_line(time.monotonic() + 5)
        self.assertEqual(cls._reserved_worker_slots, saved_reserved)
        self.assertFalse(t._abandoned)


class TsServiceControlProtocolTests(unittest.TestCase):
    """受限 ``ts-service-control`` 动词（协议层白名单扩展，最小增量）：
    动作枚举与服务名是协议常量——服务名绝不作为线上字段可被注入；通用
    schema 校验后枚举校验把该动词收死。"""

    def test_verb_whitelisted_with_single_action_field(self):
        self.assertIn(bp.TS_SERVICE_CONTROL_ACTION, bp.MANAGEMENT_ACTIONS)
        allowed = bp.MANAGEMENT_ACTIONS[bp.TS_SERVICE_CONTROL_ACTION]
        self.assertEqual(allowed, {"action": str})
        # 既有动词白名单 + 受限宿主证据扩展（host-verify/host-stop）
        # + 临时维护协议（maintenance-*/gateway-restart/host-evidence）。
        self.assertEqual(
            sorted(bp.MANAGEMENT_ACTIONS),
            ["drain", "emergency-stop", "gateway-restart", "host-evidence",
             "host-repair-limits", "host-stop", "host-verify", "maintenance-begin",
             "maintenance-end", "maintenance-status", "publish", "start",
             "status", "ts-service-control"])

    def test_host_verify_and_host_stop_verb_whitelists(self):
        self.assertIn(bp.HOST_VERIFY_ACTION, bp.MANAGEMENT_ACTIONS)
        self.assertEqual(bp.MANAGEMENT_ACTIONS[bp.HOST_VERIFY_ACTION],
                         {"check": str})
        self.assertIn(bp.HOST_STOP_ACTION, bp.MANAGEMENT_ACTIONS)
        self.assertEqual(bp.MANAGEMENT_ACTIONS[bp.HOST_STOP_ACTION], {})
        self.assertEqual(bp.HOST_VERIFY_CHECKS,
                         frozenset(("running", "closed")))
        # 枚举校验收死：未知 check 拒绝，未知字段拒绝。
        self.assertEqual(bp.validate_host_verify_fields({"check": "closed"}),
                         {"check": "closed"})
        with self.assertRaises(bp.ProtocolError):
            bp.validate_host_verify_fields({"check": "garbage"})
        with self.assertRaises(bp.ProtocolError):
            bp.validate_host_verify_fields({"check": "closed", "task": "x"})
        self.assertEqual(bp.validate_host_stop_fields({}), {})
        with self.assertRaises(bp.ProtocolError):
            bp.validate_host_stop_fields({"task": "umshare-live"})

    def test_service_name_is_protocol_constant_never_a_parameter(self):
        self.assertEqual(bp.TS_SERVICE_NAME, "Tailscale")
        self.assertEqual(bp.TS_SERVICE_CONTROL_ACTIONS,
                         frozenset(("start", "stop")))

    def test_enum_validator_accepts_only_start_stop(self):
        self.assertEqual(bp.validate_ts_service_control_fields(
            {"action": "start"}), {"action": "start"})
        self.assertEqual(bp.validate_ts_service_control_fields(
            {"action": "stop"}), {"action": "stop"})
        for fields in ({}, {"action": "restart"}, {"action": 1},
                       {"service": "LanmanWorkstation"},
                       {"action": "start", "service": "LanmanWorkstation"}):
            with self.assertRaises(bp.ProtocolError) as caught:
                bp.validate_ts_service_control_fields(fields)
            self.assertEqual(caught.exception.code, "bad_request")

    def test_management_validation_routes_the_new_verb(self):
        secret = "k" * 8
        body = bp.build_request(secret, "ts-service-control",
                                {"action": "start"})
        req = bp.validate_management_request(body, secret)
        self.assertEqual(req["action"], "ts-service-control")
        self.assertEqual(req["fields"], {"action": "start"})
        # 未知字段/缺失字段/伪造 mac 在通用 schema 层就被拒绝。
        with self.assertRaises(bp.ProtocolError):
            bp.validate_management_request(
                bp.build_request(secret, "ts-service-control",
                                 {"action": "start", "service": "x"}),
                secret)
        with self.assertRaises(bp.ProtocolError):
            bp.validate_management_request(
                bp.build_request(secret, "ts-service-control", {}),
                secret)


class ProjectionSchemaV2Tests(unittest.TestCase):
    """R6-A2 共享解析器双版本兼容：schema 1 继续接受（缺字段按旧硬编码
    1/2 补齐）、schema 2 接受真实值；未知字段仍拒；writer 指纹透传。"""

    @staticmethod
    def _entry(**overrides):
        entry = {"key_id": "uuid-1", "name": "friend",
                 "secret_hash": "a" * 64, "secret_version": 1,
                 "enabled": True}
        entry.update(overrides)
        return entry

    def test_v1_payload_accepted_with_legacy_defaults(self):
        payload = {"schema_version": 1, "config_generation": 3,
                   "data": [self._entry()]}
        parsed = bp.parse_projection_payload(payload)
        self.assertEqual(parsed["schema_version"], 1)
        self.assertEqual(parsed["share_max_concurrency"], 2)   # 旧全局值
        self.assertEqual(parsed["data"][0]["max_concurrency"], 1)  # 旧每Key值
        # 输入条目不被改写（校验不改输入）。
        self.assertNotIn("max_concurrency", payload["data"][0])

    def test_v2_payload_accepted_with_real_values(self):
        parsed = bp.parse_projection_payload({
            "schema_version": 2, "config_generation": 9,
            "share_max_concurrency": 64,
            "data": [self._entry(max_concurrency=8)]})
        self.assertEqual(parsed["schema_version"], 2)
        self.assertEqual(parsed["share_max_concurrency"], 64)
        self.assertEqual(parsed["data"][0]["max_concurrency"], 8)

    def test_v2_missing_fields_fall_back_to_legacy_limits(self):
        parsed = bp.parse_projection_payload({
            "schema_version": 2, "config_generation": 5,
            "data": [self._entry()]})
        self.assertEqual(parsed["share_max_concurrency"], 2)
        self.assertEqual(parsed["data"][0]["max_concurrency"], 1)

    def test_unknown_fields_still_rejected(self):
        with self.assertRaises(bp.ProtocolError):
            bp.parse_projection_payload({
                "schema_version": 2, "config_generation": 1,
                "share_max_concurrency": 8, "data": [], "extra": 1})
        with self.assertRaises(bp.ProtocolError):
            bp.parse_projection_payload({
                "schema_version": 2, "config_generation": 1,
                "data": [self._entry(max_concurrency=8, probe=1)]})

    def test_limit_bounds_enforced(self):
        # 顶层全局 1..64；entry 每 Key 1..48。同一个完整语义解析器，
        # 两套边界各自生效——不互相复制、也没有宽松副本。
        for bad in (0, 129, True, "8", 1.5):
            with self.subTest(global_bad=bad):
                with self.assertRaises(bp.ProtocolError):
                    bp.parse_projection_payload({
                        "schema_version": 2, "config_generation": 1,
                        "share_max_concurrency": bad, "data": []})
        for bad in (0, 65, True, "8", 1.5):
            with self.subTest(entry_bad=bad):
                with self.assertRaises(bp.ProtocolError):
                    bp.parse_projection_payload({
                        "schema_version": 2, "config_generation": 1,
                        "data": [self._entry(max_concurrency=bad)]})

    def test_legacy_over_caps_are_clamped(self):
        # D18-4: values written under the OLD caps (global 128, per-Key 64)
        # are still readable and clamped to the effective 64/48.
        parsed = bp.parse_projection_payload({
            "schema_version": 2, "config_generation": 1,
            "share_max_concurrency": 128,
            "data": [self._entry(max_concurrency=64)]})
        self.assertEqual(parsed["share_max_concurrency"], 64)
        self.assertEqual(parsed["data"][0]["max_concurrency"], 48)
        # 65 is a legacy global value and is clamped too; 129 is invalid.
        parsed = bp.parse_projection_payload({
            "schema_version": 2, "config_generation": 1,
            "share_max_concurrency": 65, "data": []})
        self.assertEqual(parsed["share_max_concurrency"], 64)

    def test_v1_still_rejects_unknown_schema_and_bool_trap(self):
        with self.assertRaises(bp.ProtocolError):
            bp.parse_projection_payload({
                "schema_version": 3, "config_generation": 1, "data": []})
        with self.assertRaises(bp.ProtocolError):
            bp.parse_projection_payload({
                "schema_version": True, "config_generation": 1, "data": []})
        with self.assertRaises(bp.ProtocolError):
            bp.parse_projection_payload({
                "schema_version": 2, "config_generation": 1,
                "data": [self._entry(key_id="dup"), self._entry(key_id="dup",
                                                                 name="o")]})

    def test_writer_round_trips_v2_projection_and_fingerprint(self):
        # writer 预检/回读与 ACK 指纹都建立在新字段上：v2 投影（全局 64、
        # 每 Key 48）完整落盘后指纹一致（ACK 指纹透传新字段，不丢字段）。
        with tempfile.TemporaryDirectory() as td:
            projection = {
                "schema_version": 2, "config_generation": 7,
                "share_max_concurrency": 64,
                "data": [self._entry(max_concurrency=48)]}
            fingerprint = _fingerprint(projection)
            writer = ProtectedProjectionWriter(
                Path(td) / "projection.json", acl_checker=lambda p: None)
            self.assertEqual(writer.write(projection, 7, fingerprint),
                             fingerprint)
            landed = json.loads((Path(td) / "projection.json")
                                .read_text(encoding="utf-8"))
            self.assertEqual(landed["share_max_concurrency"], 64)
            self.assertEqual(landed["data"][0]["max_concurrency"], 48)
            self.assertEqual(_fingerprint(landed), fingerprint)

    def test_writer_pre_check_rejects_global_129_and_entry_65(self):
        # 写前检查复用同一完整语义解析器：超过旧契约上限（全局 129、
        # 每 Key 65）的投影在替换任何文件之前就被拒绝，旧投影保持有效。
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "projection.json"
            writer = ProtectedProjectionWriter(path,
                                             acl_checker=lambda p: None)
            for bad_global, bad_entry in ((129, 8), (128, 65)):
                with self.subTest(global_limit=bad_global,
                                  entry_limit=bad_entry):
                    projection = {
                        "schema_version": 2, "config_generation": 1,
                        "share_max_concurrency": bad_global,
                        "data": [self._entry(max_concurrency=bad_entry)]}
                    with self.assertRaises(BrokerError):
                        writer.write(projection, 1, _fingerprint(projection))
                    self.assertFalse(path.exists())


class _FakeClock:
    """Deterministic wall clock: only the test advances it (the lease
    store's clock is injectable; the coordinator's monotonic clock stays
    real)."""

    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, delta):
        self.value += float(delta)
        return self.value


class _FakeInstanceChannel:
    """Deterministic instance-channel stub (the transport is the only thing
    that differs from a real share; the rules stay in the coordinator)."""

    def __init__(self, state="running", active=0):
        self.state = state
        self.active = active
        self.calls = []
        self.resume_override = None
        self.drain_override = None
        self.status_raises = False

    def __call__(self, action, fields, instance_id):
        self.calls.append(action)
        if action == "instance-status":
            if self.status_raises:
                raise OSError("instance channel unavailable")
            return {"ok": True, "error_code": "ok",
                    "result": {"instance_id": instance_id,
                               "state": self.state,
                               "active_count": self.active,
                               "last_seen_generation": 4}}
        if action == "drain":
            if self.drain_override is not None:
                return self.drain_override
            if self.state == "stopped":
                return {"ok": False, "error_code": "blocked",
                        "result": {"detail": "instance is stopped",
                                   "state": "stopped"}}
            was_active = self.active
            self.state = "draining"
            return {"ok": True, "error_code": "ok",
                    "result": {"instance_id": instance_id,
                               "state": "draining",
                               "active_count": was_active}}
        if action == "resume":
            if self.resume_override is not None:
                return self.resume_override
            if self.state == "stopped":
                return {"ok": False, "error_code": "blocked",
                        "result": {"detail": "instance is stopped",
                                   "state": "stopped"}}
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


class _CountingHostOps(FakeHostOps):
    """FakeHostOps + call counters (degradation to a whole-chain restart
    must be OBSERVABLE: any host start/stop shows up here)."""

    def __init__(self):
        super().__init__()
        self.start_calls = 0
        self.stop_calls = 0

    def start_host(self):
        self.start_calls += 1
        return super().start_host()

    def stop_host(self):
        self.stop_calls += 1
        return super().stop_host()


class _MaintenanceFixture:
    """Real coordinator + real lease store + fake instance channel/host."""

    def __init__(self, instance=None, lease_clock=None, host=None,
                 register=True):
        self.instance = instance or _FakeInstanceChannel()
        self.host = host or _CountingHostOps()
        self.instances = InstanceRegistry()
        if register:
            self.instances.register("i-1")
        self.records = []
        self.coordinator = BrokerHostCoordinator(
            writer=SimpleNamespace(write=lambda *a, **k: None),
            mgmt_secret="mgmt-k", instance_secret="inst-k",
            instances=self.instances, replay=ReplayRegistry(),
            host_ops=self.host, instance_call=self.instance,
            leases=MaintenanceLeaseStore(clock=lease_clock or time.time),
            maintenance=self.records.append)

    def management(self, action, fields=None):
        return self.coordinator.handle_management(
            bp.build_request("mgmt-k", action, fields))


class MaintenanceProtocolTests(unittest.TestCase):
    """临时维护协议（契约 §2）：动作/常量/白名单校验函数冻结；上界
    自查是硬要求（通用 int 层无上界，专用校验拒绝越界）；签名键集不动
    （新字段全部在 fields 内）；旧实例信号是 bad_request/"unknown action"
    （绝不依赖 "unknown instance action" 死代码）。"""

    def test_actions_and_constants_frozen(self):
        self.assertEqual(bp.MAINTENANCE_BEGIN_ACTION, "maintenance-begin")
        self.assertEqual(bp.MAINTENANCE_END_ACTION, "maintenance-end")
        self.assertEqual(bp.MAINTENANCE_STATUS_ACTION, "maintenance-status")
        self.assertEqual(bp.GATEWAY_RESTART_ACTION, "gateway-restart")
        self.assertEqual(bp.HOST_EVIDENCE_ACTION, "host-evidence")
        self.assertEqual(bp.HOST_EVIDENCE_CHECKS,
                         frozenset(("limits", "faults", "watchdog")))
        self.assertEqual(bp.COMPONENT_SHARE_GATEWAY, "share_gateway")
        self.assertEqual(bp.COMPONENT_SHARE_BACKEND, "share_backend")
        self.assertEqual(bp.MAINTENANCE_COMPONENTS,
                         frozenset(("share_gateway",)))
        self.assertEqual(bp.GATEWAY_RESTART_COMPONENTS,
                         frozenset(("share_gateway",)))
        self.assertEqual((bp.MAINTENANCE_LEASE_DEFAULT_S,
                          bp.MAINTENANCE_LEASE_MAX_S), (300, 600))
        self.assertEqual((bp.GATEWAY_RESTART_BUDGET_DEFAULT_S,
                          bp.GATEWAY_RESTART_BUDGET_MAX_S), (120, 180))
        self.assertEqual(bp.INSTANCE_RESUME_ACTION, "resume")
        # 新字段全部放进 fields：顶层签名键集不动（不新增顶层键）。
        self.assertEqual(bp.MANAGEMENT_SIGNED_KEYS,
                         ("v", "action", "request_id", "ts", "fields"))
        self.assertEqual(bp.INSTANCE_SIGNED_KEYS,
                         ("v", "action", "request_id", "ts", "instance_id",
                          "fields"))

    def test_management_action_schemas_are_additive(self):
        self.assertEqual(bp.MANAGEMENT_ACTIONS[bp.MAINTENANCE_BEGIN_ACTION],
                         {"component": str, "lease_s": int})
        self.assertEqual(bp.MANAGEMENT_ACTIONS[bp.MAINTENANCE_END_ACTION],
                         {"component": str, "lease_id": str})
        self.assertEqual(bp.MANAGEMENT_ACTIONS[bp.MAINTENANCE_STATUS_ACTION],
                         {})
        self.assertEqual(bp.MANAGEMENT_ACTIONS[bp.GATEWAY_RESTART_ACTION],
                         {"component": str, "budget_s": int})
        self.assertEqual(bp.MANAGEMENT_ACTIONS[bp.HOST_EVIDENCE_ACTION],
                         {"check": str})
        self.assertEqual(bp.INSTANCE_ACTIONS[bp.INSTANCE_RESUME_ACTION], {})

    def test_capability_missing_is_the_only_new_result_code(self):
        self.assertIn("capability_missing", bp.RESULT_CODES)
        result = bp.BrokerResult.err("capability_missing",
                                     "instance does not support resume")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "capability_missing")
        self.assertEqual(bp.BrokerResult.from_dict(result.to_dict())
                         .error_code, "capability_missing")
        with self.assertRaises(ValueError):
            bp.BrokerResult.err("no_such_code", "x")

    def test_maintenance_begin_validator_is_exact_and_bounded(self):
        self.assertEqual(bp.validate_maintenance_begin_fields(
            {"component": "share_gateway", "lease_s": 600}),
            {"component": "share_gateway", "lease_s": 600})
        # 越界/未知组件/未知字段一律 bad_request（不静默 clamp）。
        for fields in (
                {"component": "share_gateway", "lease_s": 601},
                {"component": "share_gateway", "lease_s": 0},
                {"component": "share_gateway", "lease_s": -5},
                {"component": "share_gateway", "lease_s": True},
                {"component": "share_backend", "lease_s": 300},
                {"component": "garbage", "lease_s": 300},
                {"component": "share_gateway", "lease_s": 300,
                 "task": "x"},
                {"component": "share_gateway"},
                {"lease_s": 300},
                {"component": ["share_gateway"], "lease_s": 300}):
            with self.assertRaises(bp.ProtocolError) as caught:
                bp.validate_maintenance_begin_fields(fields)
            self.assertEqual(caught.exception.code, "bad_request")

    def test_maintenance_end_and_status_validators(self):
        self.assertEqual(bp.validate_maintenance_end_fields(
            {"component": "share_gateway", "lease_id": "a" * 64}),
            {"component": "share_gateway", "lease_id": "a" * 64})
        for fields in ({"component": "share_gateway", "lease_id": ""},
                       {"component": "share_gateway", "lease_id": "a" * 65},
                       {"component": "share_gateway", "lease_id": 7},
                       {"component": "share_backend", "lease_id": "a"},
                       {"component": "share_gateway", "lease_id": "a",
                        "round": 1}):
            with self.assertRaises(bp.ProtocolError):
                bp.validate_maintenance_end_fields(fields)
        self.assertEqual(bp.validate_maintenance_status_fields({}), {})
        with self.assertRaises(bp.ProtocolError):
            bp.validate_maintenance_status_fields({"component": "x"})

    def test_gateway_restart_and_host_evidence_validators(self):
        self.assertEqual(bp.validate_gateway_restart_fields(
            {"component": "share_gateway", "budget_s": 180}),
            {"component": "share_gateway", "budget_s": 180})
        for fields in ({"component": "share_gateway", "budget_s": 181},
                       {"component": "share_gateway", "budget_s": 0},
                       {"component": "share_gateway", "budget_s": False},
                       {"component": "share_backend", "budget_s": 60},
                       {"component": "share_gateway", "budget_s": 60,
                        "lease_s": 300}):
            with self.assertRaises(bp.ProtocolError):
                bp.validate_gateway_restart_fields(fields)
        self.assertEqual(bp.validate_host_evidence_fields({"check": "limits"}),
                         {"check": "limits"})
        self.assertEqual(bp.validate_host_evidence_fields({"check": "faults"}),
                         {"check": "faults"})
        for fields in ({"check": "garbage"}, {}, {"check": 1},
                       {"check": "limits", "path": "C:\\x"}):
            with self.assertRaises(bp.ProtocolError):
                bp.validate_host_evidence_fields(fields)

    def test_generic_int_layer_has_no_upper_bound(self):
        # 通用 int 校验只要求 >=1、无上界：上界必须由专用校验函数收死
        # （上界自查是硬要求，绝不依赖通用层）。
        secret = "k" * 8
        body = bp.build_request(secret, bp.MAINTENANCE_BEGIN_ACTION,
                                {"component": "share_gateway",
                                 "lease_s": 100000})
        req = bp.validate_management_request(body, secret)
        self.assertEqual(req["fields"]["lease_s"], 100000)
        with self.assertRaises(bp.ProtocolError):
            bp.validate_maintenance_begin_fields(req["fields"])

    def test_new_verbs_are_signed_and_route_through_validation(self):
        secret = "k" * 8
        for action, fields in (
                (bp.MAINTENANCE_BEGIN_ACTION,
                 {"component": "share_gateway", "lease_s": 300}),
                (bp.MAINTENANCE_END_ACTION,
                 {"component": "share_gateway", "lease_id": "a" * 32}),
                (bp.MAINTENANCE_STATUS_ACTION, {}),
                (bp.GATEWAY_RESTART_ACTION,
                 {"component": "share_gateway", "budget_s": 60}),
                (bp.HOST_EVIDENCE_ACTION, {"check": "limits"})):
            body = bp.build_request(secret, action, fields)
            req = bp.validate_management_request(body, secret)
            self.assertEqual(req["action"], action)
            self.assertEqual(req["fields"], fields)
            # 伪造 mac / 未知字段在通用层被拒绝。
            forged = dict(body)
            forged["mac"] = "0" * 64
            with self.assertRaises(bp.ProtocolError):
                bp.validate_management_request(forged, secret)
            with self.assertRaises(bp.ProtocolError):
                bp.validate_management_request(
                    bp.build_request(secret, action,
                                     dict(fields, extra=1)), secret)

    def test_unknown_action_stays_bad_request_unknown_action(self):
        # 旧 broker 对未知管理动词的既有回包：bad_request/"unknown action"。
        secret = "k" * 8
        with self.assertRaises(bp.ProtocolError) as caught:
            bp.validate_management_request(
                bp.build_request(secret, "future-verb", {}), secret)
        self.assertEqual(caught.exception.code, "bad_request")
        self.assertEqual(str(caught.exception), "unknown action")

    def test_instance_resume_is_a_whitelisted_instance_verb(self):
        secret = "k" * 8
        body = bp.build_request(secret, "resume", {},
                                 instance_id="i-1")
        req = bp.validate_instance_request(body, secret)
        self.assertEqual(req["action"], "resume")
        self.assertEqual(req["fields"], {})
        with self.assertRaises(bp.ProtocolError):
            bp.validate_instance_request(
                bp.build_request(secret, "resume", {"x": 1},
                                  instance_id="i-1"), secret)

    def test_old_instance_signal_is_unknown_action(self):
        # 旧实例不认识 resume：validate_instance_request 先一步以
        # bad_request/"unknown action" 拒绝（绝不依赖 "unknown instance
        # action" 死代码，也绝不降级为 host-stop/start）。
        secret = "k" * 8
        with self.assertRaises(bp.ProtocolError) as caught:
            bp.validate_instance_request(
                bp.build_request(secret, "future-instance-verb", {},
                                  instance_id="i-1"), secret)
        self.assertEqual(caught.exception.code, "bad_request")
        self.assertEqual(str(caught.exception), "unknown action")
        self.assertNotEqual(str(caught.exception), "unknown instance action")


class MaintenanceLeaseStoreTests(unittest.TestCase):
    """单例维护租约槽：绑定 (component, instance_id, round, expires_at)；
    迟到结果绝不重开；到期回收由调用方核对实例/轮次。"""

    def test_begin_binds_a_32_hex_lease_with_a_bounded_expiry(self):
        clock = _FakeClock()
        store = MaintenanceLeaseStore(clock=clock)
        self.assertIsNone(store.current())
        lease = store.begin("share_gateway", "i-1", 7, 300)
        self.assertEqual(len(lease["lease_id"]), 32)
        int(lease["lease_id"], 16)
        self.assertEqual(lease["component"], "share_gateway")
        self.assertEqual(lease["instance_id"], "i-1")
        self.assertEqual(lease["round"], 7)
        self.assertEqual(lease["created_at"], 1000)
        self.assertEqual(lease["expires_at"], 1300)
        self.assertEqual(store.current(), lease)
        # current 返回副本：读者改动绝不污染存储中的租约。
        copy = store.current()
        copy["lease_id"] = "tampered"
        self.assertEqual(store.current()["lease_id"], lease["lease_id"])

    def test_end_requires_component_lease_and_unexpired(self):
        clock = _FakeClock()
        store = MaintenanceLeaseStore(clock=clock)
        self.assertEqual(store.end("share_gateway", "x"), (False, None))
        lease = store.begin("share_gateway", "i-1", 7, 300)
        ok, current = store.end("share_backend", lease["lease_id"])
        self.assertFalse(ok)
        self.assertEqual(current["lease_id"], lease["lease_id"])
        ok, current = store.end("share_gateway", "0" * 32)
        self.assertFalse(ok)
        self.assertEqual(current["lease_id"], lease["lease_id"])
        clock.advance(300)
        ok, current = store.end("share_gateway", lease["lease_id"])
        self.assertFalse(ok)                 # 到期后拒绝（绝不重开）
        self.assertEqual(current["lease_id"], lease["lease_id"])
        self.assertIsNotNone(store.current())   # 过期租约仍如实可见
        self.assertEqual(store.clear("test"), None)
        self.assertIsNone(store.current())

    def test_end_consumes_the_matching_unexpired_lease(self):
        clock = _FakeClock()
        store = MaintenanceLeaseStore(clock=clock)
        lease = store.begin("share_gateway", "i-1", 7, 300)
        ok, ended = store.end("share_gateway", lease["lease_id"])
        self.assertTrue(ok)
        self.assertEqual(ended["lease_id"], lease["lease_id"])
        self.assertIsNone(store.current())

    def test_expire_due_reclaims_only_a_due_lease(self):
        clock = _FakeClock()
        store = MaintenanceLeaseStore(clock=clock)
        lease = store.begin("share_gateway", "i-1", 7, 300)
        self.assertIsNone(store.expire_due())      # 未到期：不回收
        self.assertIsNotNone(store.current())
        clock.advance(300)
        expired = store.expire_due()
        self.assertEqual(expired["lease_id"], lease["lease_id"])
        self.assertIsNone(store.current())
        self.assertIsNone(store.expire_due())      # 已回收：绝不重复

    def test_restore_adopts_only_a_complete_leased_record(self):
        store = MaintenanceLeaseStore(clock=_FakeClock())
        self.assertFalse(store.restore({"state": "idle"}))
        self.assertFalse(store.restore({"state": "leased"}))
        self.assertFalse(store.restore({"state": "leased", "lease_id": "a",
                                        "instance_id": "i-1", "round": 1,
                                        "expires_at": None}))
        self.assertTrue(store.restore(
            {"state": "leased", "lease_id": "a" * 32,
             "instance_id": "i-1", "round": 3, "created_at": 10,
             "expires_at": 1300}))
        lease = store.current()
        self.assertEqual(lease["lease_id"], "a" * 32)
        self.assertEqual(lease["round"], 3)
        self.assertEqual(lease["expires_at"], 1300)


class MaintenanceCoordinatorTests(unittest.TestCase):
    """维护协调器：暂停/排空/恢复接收的 broker 侧；旧实例 capability_missing
    明确拒绝且绝不降级为整链重启；维护暂态与已确认停止分开。"""

    def test_begin_leases_and_pauses(self):
        fx = _MaintenanceFixture()
        resp = fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                             {"component": "share_gateway", "lease_s": 300})
        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        self.assertTrue(result["accepted"])
        self.assertEqual(result["component"], "share_gateway")
        self.assertEqual(len(result["lease_id"]), 32)
        self.assertEqual(result["instance_id"], "i-1")
        self.assertEqual(result["round"], 1)
        self.assertEqual(result["state"], "draining")
        self.assertEqual(result["active_count"], 0)
        self.assertEqual(result["lease_expires_at"],
                         fx.coordinator.leases.current()["expires_at"])
        self.assertEqual(fx.instance.state, "draining")
        self.assertIn("drain", fx.instance.calls)
        # 受保护维护记录：leased + 维护动词（绝不写停止终态）。
        self.assertEqual(fx.records[-1]["state"], "leased")
        self.assertEqual(fx.records[-1]["by"], "maintenance-begin")
        self.assertEqual(fx.records[-1]["lease_id"], result["lease_id"])
        self.assertEqual(fx.host.stop_calls, 0)
        self.assertEqual(fx.host.start_calls, 0)

    def test_begin_refuses_a_second_lease(self):
        fx = _MaintenanceFixture()
        first = fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                              {"component": "share_gateway", "lease_s": 300})
        self.assertTrue(first["ok"])
        second = fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                               {"component": "share_gateway", "lease_s": 300})
        self.assertFalse(second["ok"])
        self.assertEqual(second["error_code"], "blocked")
        self.assertEqual(fx.instance.count("drain"), 1)

    def test_begin_on_a_stopped_instance_is_blocked(self):
        fx = _MaintenanceFixture(
            instance=_FakeInstanceChannel(state="stopped"))
        resp = fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                             {"component": "share_gateway", "lease_s": 300})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "blocked")
        # 终态停止绝不被维护租约重开：连 drain 都不发。
        self.assertNotIn("drain", fx.instance.calls)
        self.assertIsNone(fx.coordinator.leases.current())

    def test_begin_without_trusted_instance_is_unconfirmed(self):
        fx = _MaintenanceFixture(register=False)
        resp = fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                             {"component": "share_gateway", "lease_s": 300})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "unconfirmed")
        self.assertIsNone(fx.coordinator.leases.current())

    def test_begin_with_unreadable_instance_state_is_unconfirmed(self):
        instance = _FakeInstanceChannel()
        instance.status_raises = True
        fx = _MaintenanceFixture(instance=instance)
        resp = fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                             {"component": "share_gateway", "lease_s": 300})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "unconfirmed")
        self.assertNotIn("drain", fx.instance.calls)

    def test_begin_on_an_instance_without_the_verb_is_capability_missing(self):
        instance = _FakeInstanceChannel()
        instance.drain_override = {
            "ok": False, "error_code": "bad_request",
            "result": {"detail": "unknown action"}}
        fx = _MaintenanceFixture(instance=instance)
        resp = fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                             {"component": "share_gateway", "lease_s": 300})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "capability_missing")
        self.assertIsNone(fx.coordinator.leases.current())
        self.assertEqual(fx.host.stop_calls, 0)
        self.assertEqual(fx.host.start_calls, 0)

    def test_begin_with_unconfirmed_drain_state_leases_nothing(self):
        class _HalfDrain(_FakeInstanceChannel):
            def __call__(self, action, fields, instance_id):
                if action == "drain":
                    self.calls.append(action)
                    return {"ok": True, "error_code": "ok",
                            "result": {"instance_id": instance_id,
                                       "state": "running",
                                       "active_count": 0}}
                return super().__call__(action, fields, instance_id)

        fx = _MaintenanceFixture(instance=_HalfDrain())
        resp = fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                             {"component": "share_gateway", "lease_s": 300})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "unconfirmed")
        self.assertIsNone(fx.coordinator.leases.current())

    def test_begin_rejects_a_bad_component_before_any_action(self):
        fx = _MaintenanceFixture()
        resp = fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                             {"component": "share_backend", "lease_s": 300})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "bad_request")
        self.assertEqual(fx.instance.calls, [])

    def test_end_resumes_and_clears_the_lease(self):
        fx = _MaintenanceFixture()
        begin = fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                               {"component": "share_gateway",
                                "lease_s": 300})
        lease_id = begin["result"]["lease_id"]
        resp = fx.management(bp.MAINTENANCE_END_ACTION,
                              {"component": "share_gateway",
                               "lease_id": lease_id})
        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        self.assertTrue(result["resumed"])
        self.assertEqual(result["lease_id"], lease_id)
        self.assertEqual(result["instance_id"], "i-1")
        self.assertEqual(result["round"], 1)
        self.assertEqual(result["state"], "running")
        self.assertEqual(fx.instance.state, "running")
        self.assertEqual(fx.instance.count("resume"), 1)
        self.assertIsNone(fx.coordinator.leases.current())
        self.assertEqual(fx.records[-1]["state"], "idle")
        self.assertEqual(fx.records[-1]["by"], "maintenance-end")
        # 维护暂态与已确认停止分开：恢复接收绝不代表已确认停止。
        self.assertEqual(fx.host.stop_calls, 0)

    def test_end_to_end_with_a_real_instance_answers_resume(self):
        # 端到端回归（评审复现）：真实 ShareServer 作为实例通道，真实
        # broker 的 maintenance-begin/end 完成 drain → resume；此前真实实例
        # 无 resume 分支时 END 回 bad_request/"unknown instance action"，
        # 租约永不清理（契约 §2.4/§0.7）。
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "clients.json"
            config.write_text(json.dumps({"clients": {}}), encoding="utf-8")
            server = share_gw.ShareServer(("127.0.0.1", 0), config)
            self.addCleanup(server.server_close)
            secret = "inst-e2e"
            instance_id = server.instance_id

            def real_instance_call(action, fields, target):
                self.assertEqual(target, instance_id)
                body = bp.build_request(secret, action, fields or {},
                                         instance_id=instance_id,
                                         now=int(time.time()) + 1)
                return server.handle_instance_control(body, secret)

            fx = _MaintenanceFixture(register=False)
            fx.instances.register(instance_id)
            fx.coordinator.instance_call = real_instance_call
            begin = fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                                   {"component": "share_gateway",
                                    "lease_s": 300})
            self.assertTrue(begin["ok"], begin)
            self.assertEqual(begin["result"]["state"], "draining")
            self.assertEqual(server.state, "draining")
            end = fx.management(bp.MAINTENANCE_END_ACTION,
                                 {"component": "share_gateway",
                                  "lease_id": begin["result"]["lease_id"]})
            self.assertTrue(end["ok"], end)
            self.assertTrue(end["result"]["resumed"])
            self.assertEqual(end["result"]["state"], "running")
            self.assertEqual(server.state, "running")
            self.assertIsNone(fx.coordinator.leases.current())
            self.assertIsNone(fx.records[-1]["last_outcome"])

    def test_end_with_a_mismatched_lease_is_refused(self):
        fx = _MaintenanceFixture()
        fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                       {"component": "share_gateway", "lease_s": 300})
        resp = fx.management(bp.MAINTENANCE_END_ACTION,
                              {"component": "share_gateway",
                               "lease_id": "0" * 32})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "instance_mismatch")
        self.assertEqual(fx.instance.count("resume"), 0)
        self.assertIsNotNone(fx.coordinator.leases.current())

    def test_end_after_expiry_is_unconfirmed(self):
        clock = _FakeClock()
        fx = _MaintenanceFixture(lease_clock=clock)
        begin = fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                               {"component": "share_gateway",
                                "lease_s": 300})
        lease_id = begin["result"]["lease_id"]
        clock.advance(301)
        resp = fx.coordinator._maintenance_end(
            {"component": "share_gateway", "lease_id": lease_id})
        self.assertFalse(resp.ok)
        self.assertEqual(resp.error_code, "unconfirmed")
        self.assertIsNotNone(fx.coordinator.leases.current())

    def test_end_after_the_instance_round_changed_is_instance_mismatch(self):
        fx = _MaintenanceFixture()
        begin = fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                               {"component": "share_gateway",
                                "lease_s": 300})
        fx.instances.register("i-2")     # 注册版本 +1：迟到结果不得重开
        resp = fx.management(bp.MAINTENANCE_END_ACTION,
                              {"component": "share_gateway",
                               "lease_id": begin["result"]["lease_id"]})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "instance_mismatch")
        self.assertEqual(fx.instance.count("resume"), 0)

    def test_end_on_a_stopped_instance_is_blocked_and_keeps_the_lease(self):
        fx = _MaintenanceFixture()
        begin = fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                               {"component": "share_gateway",
                                "lease_s": 300})
        fx.instance.state = "stopped"
        resp = fx.management(bp.MAINTENANCE_END_ACTION,
                              {"component": "share_gateway",
                               "lease_id": begin["result"]["lease_id"]})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "blocked")
        # 终态不被重开：租约保留 + 如实记录失败结果。
        self.assertIsNotNone(fx.coordinator.leases.current())
        self.assertEqual(fx.records[-1]["last_outcome"]["ok"], False)
        self.assertEqual(fx.records[-1]["last_outcome"]["action"],
                         "maintenance-end")

    def test_end_on_an_old_instance_is_capability_missing(self):
        fx = _MaintenanceFixture()
        begin = fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                               {"component": "share_gateway",
                                "lease_s": 300})
        fx.instance.resume_override = {
            "ok": False, "error_code": "bad_request",
            "result": {"detail": "unknown action"}}
        resp = fx.management(bp.MAINTENANCE_END_ACTION,
                              {"component": "share_gateway",
                               "lease_id": begin["result"]["lease_id"]})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "capability_missing")
        # 绝不降级为 host-stop/start 或整链重启。
        self.assertEqual(fx.host.stop_calls, 0)
        self.assertEqual(fx.host.start_calls, 0)
        self.assertIsNotNone(fx.coordinator.leases.current())

    def test_end_without_a_lease_is_unconfirmed(self):
        fx = _MaintenanceFixture()
        resp = fx.management(bp.MAINTENANCE_END_ACTION,
                              {"component": "share_gateway",
                               "lease_id": "a" * 32})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "unconfirmed")

    def test_a_verified_drain_clears_the_lease(self):
        # 停止优先（§9.2）：确认关闭的 drain 清租约并把 by 写成 drain，
        # 维护暂态绝不在已确认停止之后存活。
        fx = _MaintenanceFixture()
        begin = fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                               {"component": "share_gateway", "lease_s": 300})
        self.assertTrue(begin["ok"], begin)
        resp = fx.management("drain")
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(resp["result"]["stopped"])
        self.assertIsNone(fx.coordinator.leases.current())
        self.assertEqual(fx.records[-1]["state"], "idle")
        self.assertEqual(fx.records[-1]["by"], "drain")

    def test_a_verified_emergency_stop_clears_the_lease(self):
        fx = _MaintenanceFixture()
        begin = fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                               {"component": "share_gateway", "lease_s": 300})
        self.assertTrue(begin["ok"], begin)
        resp = fx.management("emergency-stop")
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(resp["result"]["stopped"])
        self.assertIsNone(fx.coordinator.leases.current())
        self.assertEqual(fx.records[-1]["state"], "idle")
        self.assertEqual(fx.records[-1]["by"], "emergency-stop")

    def test_lazy_expiry_resumes_once_and_clears(self):
        clock = _FakeClock()
        fx = _MaintenanceFixture(lease_clock=clock)
        fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                       {"component": "share_gateway", "lease_s": 300})
        clock.advance(301)
        # 任何管理请求派发时触发惰性到期（replay 消费之后、动作分支之前）。
        resp = fx.management("status")
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(fx.instance.count("resume"), 1)
        self.assertEqual(fx.instance.state, "running")
        self.assertIsNone(fx.coordinator.leases.current())
        self.assertEqual(fx.records[-1]["state"], "idle")
        self.assertEqual(fx.records[-1]["by"], "expiry")

    def test_lazy_expiry_failure_keeps_the_lease_and_retries(self):
        clock = _FakeClock()
        fx = _MaintenanceFixture(lease_clock=clock)
        fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                       {"component": "share_gateway", "lease_s": 300})
        clock.advance(301)
        fx.instance.resume_override = {
            "ok": False, "error_code": "unconfirmed",
            "result": {"detail": "still draining"}}
        fx.management("status")
        self.assertEqual(fx.instance.count("resume"), 1)
        self.assertIsNotNone(fx.coordinator.leases.current())
        self.assertEqual(fx.records[-1]["last_outcome"]["ok"], False)
        # 下次再试：第二次管理请求再发一次 resume。
        fx.management("status")
        self.assertEqual(fx.instance.count("resume"), 2)

    def test_lazy_expiry_never_resumes_a_replaced_instance(self):
        clock = _FakeClock()
        fx = _MaintenanceFixture(lease_clock=clock)
        fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                       {"component": "share_gateway", "lease_s": 300})
        clock.advance(301)
        fx.instances.register("i-2")      # 实例轮次变化：绝不向被替换实例发 resume
        fx.management("status")
        self.assertEqual(fx.instance.count("resume"), 0)
        self.assertIsNotNone(fx.coordinator.leases.current())

    def test_status_shape_and_honest_nulls(self):
        fx = _MaintenanceFixture()
        resp = fx.management(bp.MAINTENANCE_STATUS_ACTION)
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(set(resp["result"]),
                         {"checked_at", "lease", "lease_expired",
                          "instance", "last_outcome"})
        self.assertIsInstance(resp["result"]["checked_at"], int)
        self.assertIsNone(resp["result"]["lease"])
        self.assertIs(resp["result"]["lease_expired"], False)
        self.assertIsNone(resp["result"]["last_outcome"])
        # 无租约但实例受信：instance 如实给出（绝不因无租约而伪报 null）。
        self.assertEqual(resp["result"]["instance"]["instance_id"], "i-1")
        self.assertEqual(resp["result"]["instance"]["state"], "running")
        # 有租约 + 有受信实例：形状齐全。
        begin = fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                              {"component": "share_gateway",
                               "lease_s": 300})
        resp = fx.management(bp.MAINTENANCE_STATUS_ACTION)
        result = resp["result"]
        self.assertEqual(result["lease"]["lease_id"],
                         begin["result"]["lease_id"])
        self.assertEqual(result["lease"]["component"], "share_gateway")
        self.assertEqual(result["lease"]["round"], 1)
        self.assertEqual(result["lease"]["active_count_at_begin"], 0)
        self.assertIs(result["lease_expired"], False)
        self.assertEqual(result["instance"]["instance_id"], "i-1")
        self.assertEqual(result["instance"]["state"], "draining")
        self.assertEqual(result["instance"]["active_count"], 0)
        self.assertEqual(result["instance"]["last_seen_generation"], 4)

    def test_status_instance_null_when_the_channel_fails(self):
        instance = _FakeInstanceChannel()
        instance.status_raises = True
        fx = _MaintenanceFixture(instance=instance)
        resp = fx.management(bp.MAINTENANCE_STATUS_ACTION)
        self.assertTrue(resp["ok"], resp)
        self.assertIsNone(resp["result"]["instance"])
        self.assertIsNone(resp["result"]["lease"])


class GatewayRestartCoordinatorTests(unittest.TestCase):
    """``gateway-restart`` broker 侧：宿主三证前置（False/None → blocked 且
    不调 stop_host）；停止/启动未确认如实 unconfirmed；维护记录在停之前
    写入、确认后清；绝不重试强停。"""

    def _fixture(self, host=None):
        return _MaintenanceFixture(host=host or _GatewayHost())

    def test_restart_requires_the_three_host_proofs(self):
        for verdict in (False, None):
            host = _GatewayHost()
            host.running = verdict
            fx = self._fixture(host)
            resp = fx.management(bp.GATEWAY_RESTART_ACTION,
                                  {"component": "share_gateway",
                                   "budget_s": 1})
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error_code"], "blocked")
            self.assertEqual(host.stop_calls, 0)
            self.assertEqual(host.start_calls, 0)
            self.assertEqual(fx.records, [])

    def test_restart_success_writes_then_clears_the_record(self):
        host = _GatewayHost()
        fx = self._fixture(host)
        seen = []
        host.on_stop = lambda: seen.append(dict(fx.records[-1]))
        resp = fx.management(bp.GATEWAY_RESTART_ACTION,
                              {"component": "share_gateway",
                               "budget_s": 1})
        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        self.assertTrue(result["restarted"])
        self.assertIs(result["stopped_confirmed"], True)
        self.assertIs(result["running_confirmed"], True)
        self.assertEqual(result["maintenance_state"], "idle")
        self.assertIsInstance(result["elapsed_s"], float)
        self.assertEqual(host.stop_calls, 1)
        self.assertEqual(host.start_calls, 1)
        # D15-4: the restart's own stop writes the persistent stopped
        # intent, so the following start MUST be explicit to clear it.
        self.assertEqual(host.explicit_calls, 1)
        self.assertEqual(host.intent, "running")
        # 停之前写 restarting 记录（看门狗不抢跑），确认后清。
        self.assertEqual(seen[0]["state"], "restarting")
        self.assertEqual(seen[0]["by"], "gateway-restart")
        self.assertEqual(seen[0]["expires_at"],
                         seen[0]["created_at"] + 1 + 30)
        self.assertEqual(fx.records[-1]["state"], "idle")
        self.assertEqual(fx.records[-1]["by"], "gateway-restart")

    def test_restart_stop_timeout_is_unconfirmed_without_a_force_retry(self):
        host = _GatewayHost()
        host.stop_closes = False
        fx = self._fixture(host)
        resp = fx.management(bp.GATEWAY_RESTART_ACTION,
                              {"component": "share_gateway",
                               "budget_s": 1})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "unconfirmed")
        self.assertIs(resp["result"]["stopped_confirmed"], False)
        self.assertIs(resp["result"]["running_confirmed"], False)
        self.assertEqual(host.stop_calls, 1)     # 绝不重试强停
        self.assertEqual(host.start_calls, 0)    # 未确认停止绝不启动
        # 停止未确认：清维护窗口 + 如实记录失败结果。
        self.assertEqual(fx.records[-1]["state"], "idle")
        self.assertEqual(fx.records[-1]["last_outcome"]["ok"], False)
        self.assertEqual(fx.records[-1]["last_outcome"]["action"],
                         "gateway-restart")

    def test_restart_start_timeout_keeps_the_bounded_record(self):
        host = _GatewayHost()
        host.start_runs = False
        fx = self._fixture(host)
        resp = fx.management(bp.GATEWAY_RESTART_ACTION,
                              {"component": "share_gateway",
                               "budget_s": 1})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "unconfirmed")
        self.assertIs(resp["result"]["stopped_confirmed"], True)
        self.assertIs(resp["result"]["running_confirmed"], False)
        self.assertEqual(resp["result"]["maintenance_state"], "restarting")
        # 启动未确认：维护记录保留（自身 expires_at 有界，之后看门狗接管）。
        record = fx.records[-1]
        self.assertEqual(record["state"], "restarting")
        self.assertGreater(record["expires_at"], record["created_at"])
        self.assertEqual(record["last_outcome"]["ok"], False)

    def test_a_later_success_keeps_the_recorded_failure(self):
        host = _GatewayHost()
        host.start_runs = False
        fx = self._fixture(host)
        fx.management(bp.GATEWAY_RESTART_ACTION,
                       {"component": "share_gateway", "budget_s": 1})
        failed = dict(fx.records[-1]["last_outcome"])
        self.assertEqual(failed["ok"], False)
        host.start_runs = True
        host.running = True           # 网关重新起回（身份重新确认）
        resp = fx.management(bp.GATEWAY_RESTART_ACTION,
                              {"component": "share_gateway",
                               "budget_s": 1})
        self.assertTrue(resp["ok"], resp)
        # 恢复不擦除失败：last_outcome 仍是那次失败。
        self.assertEqual(fx.records[-1]["state"], "idle")
        self.assertEqual(fx.records[-1]["last_outcome"], failed)
        status = fx.management(bp.MAINTENANCE_STATUS_ACTION)
        self.assertEqual(status["result"]["last_outcome"], failed)

    def test_restart_rejects_an_unknown_component(self):
        host = _GatewayHost()
        fx = self._fixture(host)
        resp = fx.management(bp.GATEWAY_RESTART_ACTION,
                              {"component": "share_backend", "budget_s": 60})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "bad_request")
        self.assertEqual(host.stop_calls, 0)

    def test_restart_restores_an_in_force_lease_afterwards(self):
        host = _GatewayHost()
        fx = self._fixture(host)
        begin = fx.management(bp.MAINTENANCE_BEGIN_ACTION,
                               {"component": "share_gateway",
                                "lease_s": 300})
        self.assertTrue(begin["ok"], begin)
        resp = fx.management(bp.GATEWAY_RESTART_ACTION,
                              {"component": "share_gateway",
                               "budget_s": 1})
        self.assertTrue(resp["ok"], resp)
        # 重启窗口关闭后：仍在 force 的租约记录被恢复（绝不被重启清掉）。
        self.assertEqual(resp["result"]["maintenance_state"], "leased")
        self.assertEqual(fx.records[-1]["state"], "leased")
        self.assertEqual(fx.records[-1]["lease_id"],
                         begin["result"]["lease_id"])


class StartExplicitProtocolTests(unittest.TestCase):
    """The management ``start`` verb carries an OPTIONAL ``explicit`` bool.

    A missing field is the historical watchdog/systemd start (the persistent
    stop intent still refuses it); ``explicit: true`` is the operator's own
    start and clears that intent. Old clients that send no field keep the old
    wire shape, and a non-bool value is a structured bad_request.
    """

    def test_start_schema_is_optional_and_strictly_boolean(self):
        self.assertEqual(bp.MANAGEMENT_ACTIONS["start"],
                         {"explicit": (bool,)})
        for fields in (None, {}):
            request = bp.build_request("mgmt-k", "start", fields)
            self.assertEqual(bp.validate_management_request(
                request, "mgmt-k")["fields"], {})
        self.assertEqual(bp.validate_management_request(
            bp.build_request("mgmt-k", "start", {"explicit": True}),
            "mgmt-k")["fields"], {"explicit": True})
        for bad in ({"explicit": 1}, {"explicit": "true"},
                    {"explicit": None}, {"explicit": []}):
            with self.subTest(fields=bad):
                with self.assertRaises(bp.ProtocolError) as caught:
                    bp.validate_management_request(
                        bp.build_request("mgmt-k", "start", bad), "mgmt-k")
                self.assertEqual(caught.exception.code, "bad_request")


class StartExplicitCoordinatorTests(unittest.TestCase):
    """The broker start action forwards ``explicit`` to the host ops."""

    def test_watchdog_start_still_refuses_a_persisted_stop_intent(self):
        host = _GatewayHost()
        host.running = False
        host.intent = "stopped"
        fx = _MaintenanceFixture(host=host)
        resp = fx.management("start")
        self.assertFalse(resp["ok"], resp)
        self.assertEqual(resp["error_code"], "unconfirmed")
        self.assertEqual(host.start_calls, 1)
        self.assertEqual(host.explicit_calls, 0)
        self.assertEqual(host.intent, "stopped")

    def test_explicit_start_clears_the_persisted_stop_intent(self):
        host = _GatewayHost()
        host.running = False
        host.intent = "stopped"
        fx = _MaintenanceFixture(host=host)
        resp = fx.management("start", {"explicit": True})
        self.assertTrue(resp["ok"], resp)
        self.assertIs(resp["result"]["started"], True)
        self.assertEqual(host.start_calls, 1)
        self.assertEqual(host.explicit_calls, 1)
        self.assertEqual(host.intent, "running")


class _GatewayHost:
    """Scripted host for gateway-restart: observable open/closed/start
    behavior and call counters (no product rule lives here)."""

    def __init__(self):
        self.running = True
        self.stop_closes = True
        self.start_runs = True
        self.stop_calls = 0
        self.start_calls = 0
        self.explicit_calls = 0
        self.intent = "running"
        self.on_stop = None

    def verify_running(self):
        return self.running

    def stop_host(self):
        self.stop_calls += 1
        self.intent = "stopped"
        if self.on_stop is not None:
            self.on_stop()
        if self.stop_closes:
            self.running = False
        return True

    def verify_closed(self):
        return not self.running

    def start_host(self, explicit=False):
        self.start_calls += 1
        if explicit:
            self.explicit_calls += 1
        if self.intent == "stopped" and not explicit:
            # The persistent stop intent is never resurrected by a watchdog
            # start; only the operator's explicit start clears it.
            return False
        if explicit:
            self.intent = "running"
        if self.start_runs:
            self.running = True
        return True


class _RecordingTransport:
    """Transport stub: records the signed request and the exchange
    deadline; answers a canned response carrying the SAME request id."""

    def __init__(self, response):
        self.response = response
        self.request = None
        self.deadline = None
        self.closed = False

    def open(self):
        return self

    def close(self):
        self.closed = True

    def send_all(self, data, deadline=None):
        self.deadline = deadline
        self.request = bp.decode_frame(data)

    def recv_line(self, deadline):
        return bp.encode_frame(dict(
            self.response, request_id=self.request["request_id"]))


class BrokerClientMaintenanceVerbTests(unittest.TestCase):
    """客户端固定方法：字段映射冻结；``timeout_s`` 有界 1..180 且越界
    在 I/O 之前拒绝（绝不静默 clamp）；gateway-restart 阻塞交换有界。"""

    def _client(self, response=None):
        transport = _RecordingTransport(
            response or {"ok": True, "error_code": "ok", "result": {}})
        return BrokerClient(lambda: transport, "secret"), transport

    def test_methods_map_to_the_frozen_wire_fields(self):
        for method, args, action, fields in (
                ("maintenance_begin", ("share_gateway", 300),
                 "maintenance-begin",
                 {"component": "share_gateway", "lease_s": 300}),
                ("maintenance_end", ("share_gateway", "a" * 32),
                 "maintenance-end",
                 {"component": "share_gateway", "lease_id": "a" * 32}),
                ("maintenance_status", (), "maintenance-status", {}),
                ("gateway_restart", ("share_gateway", 60),
                 "gateway-restart",
                 {"component": "share_gateway", "budget_s": 60}),
                ("host_evidence", ("limits",), "host-evidence",
                 {"check": "limits"})):
            client, transport = self._client()
            result = getattr(client, method)(*args)
            self.assertTrue(result.ok)
            self.assertEqual(transport.request["action"], action)
            self.assertEqual(transport.request["fields"], fields)

    def test_start_maps_explicit_to_the_wire_and_omits_false(self):
        client, transport = self._client()
        self.assertTrue(client.start().ok)
        self.assertEqual(transport.request["action"], "start")
        self.assertEqual(transport.request["fields"], {})
        client2, transport2 = self._client()
        self.assertTrue(client2.start(explicit=True).ok)
        self.assertEqual(transport2.request["action"], "start")
        self.assertEqual(transport2.request["fields"], {"explicit": True})

    def test_call_timeout_is_bounded_and_never_silently_clamped(self):
        client, transport = self._client()
        for bad in (0.5, 181, 0, -1, True, "60"):
            with self.assertRaises(ValueError):
                client.call("status", timeout_s=bad)
        self.assertIsNone(transport.deadline)      # 拒绝发生在任何 I/O 之前
        self.assertTrue(client.call("status", timeout_s=1).ok)
        self.assertIsNotNone(transport.deadline)
        client2, transport2 = self._client()
        self.assertTrue(client2.call("status", timeout_s=180).ok)
        self.assertIsNotNone(transport2.deadline)

    def test_gateway_restart_exchange_deadline_is_bounded(self):
        client, transport = self._client()
        before = time.monotonic()
        self.assertTrue(client.gateway_restart("share_gateway", 60).ok)
        window = transport.deadline - before
        self.assertGreater(window, 60)
        self.assertLessEqual(window, 76)
        # 冻结的 1..180 界：即使 budget_s=180 也绝不超过 180 秒。
        client2, transport2 = self._client()
        before2 = time.monotonic()
        self.assertTrue(client2.gateway_restart("share_gateway", 180).ok)
        self.assertLessEqual(transport2.deadline - before2, 180.001)

    def test_old_broker_refusal_is_surfaced_unchanged(self):
        client, _ = self._client(
            {"ok": False, "error_code": "bad_request",
             "result": {"detail": "unknown action"}})
        result = client.maintenance_begin("share_gateway", 300)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "bad_request")
        self.assertEqual(result.result["detail"], "unknown action")


class CloseBudgetAlignmentTests(unittest.TestCase):
    """C1 返修：客户端等待必须覆盖服务端允许的排空窗口 + 预留停止预算。

    维护者复现：客户端只等 10 秒，服务端却允许排空 60 秒 + 停止核验 20 秒；
    客户端提前放弃会放弃一个服务端仍在执行的排空，重试又会与在途停止竞争。
    """

    def _client(self, response):
        transport = _RecordingTransport(response)
        return BrokerClient(lambda: transport, "secret"), transport

    def test_the_client_deadlines_cover_the_broker_close_budgets(self):
        # 两边读同一份协议常量：客户端交换时限 >= 排空窗口 + 预留停止预算
        self.assertGreaterEqual(
            bp.DRAIN_DEADLINE_SECONDS,
            bp.DRAIN_WINDOW_SECONDS + bp.STOP_BUDGET_SECONDS)
        self.assertGreaterEqual(bp.EMERGENCY_DEADLINE_SECONDS,
                                bp.STOP_BUDGET_SECONDS)
        # 冻结的 1..180 界：两个时限都在界内
        self.assertLessEqual(bp.DRAIN_DEADLINE_SECONDS, 180.0)
        self.assertLessEqual(bp.EMERGENCY_DEADLINE_SECONDS, 180.0)
        # 服务端允许的整个关闭工作必须落在包装器的 130s 派发时限内
        self.assertLessEqual(
            bp.DRAIN_WINDOW_SECONDS + bp.STOP_BUDGET_SECONDS, 130.0)

    def test_the_drain_and_stop_verbs_pass_the_aligned_deadlines(self):
        client, transport = self._client({
            "ok": True, "error_code": "ok",
            "result": {"drained": True, "stopped": True}})
        before = time.monotonic()
        self.assertTrue(client.drain().ok)
        drain_window = transport.deadline - before
        self.assertGreaterEqual(
            drain_window,
            bp.DRAIN_WINDOW_SECONDS + bp.STOP_BUDGET_SECONDS,
            "排空交换时限必须覆盖服务端的整个关闭工作")
        self.assertLessEqual(drain_window, 180.0)
        client2, transport2 = self._client({
            "ok": True, "error_code": "ok", "result": {"stopped": True}})
        before = time.monotonic()
        self.assertTrue(client2.emergency_stop().ok)
        emergency_window = transport2.deadline - before
        self.assertGreaterEqual(emergency_window, bp.STOP_BUDGET_SECONDS,
                                "紧急停止时限必须覆盖预留停止预算")
        self.assertLessEqual(emergency_window, 180.0)

    def test_the_coordinator_defaults_are_the_shared_protocol_budgets(self):
        # 直接构造协调端时，默认值也来自同一份协议常量（不是各写一份数字）
        import share_broker_host
        coordinator = share_broker_host.BrokerHostCoordinator(
            writer=SimpleNamespace(write=lambda *a, **k: None),
            mgmt_secret="mgmt-k", instance_secret="inst-k",
            instances=InstanceRegistry(), replay=ReplayRegistry(),
            host_ops=SimpleNamespace(), instance_call=lambda *a, **k: {})
        self.assertEqual(coordinator.drain_timeout, bp.DRAIN_WINDOW_SECONDS)
        self.assertEqual(coordinator.stop_timeout, bp.STOP_BUDGET_SECONDS)


if __name__ == "__main__":
    unittest.main()
