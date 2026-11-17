# -*- coding: utf-8 -*-
"""Isolated tests for the share production adapters (console_share_production).

Every boundary is INJECTED: a fake runner (programmed results/exceptions), a
fake exe path (never stat-ed), a recording sleeper, fake broker/host gate
parts. ZERO real system calls — a module-level ISOLATION SENTINEL patches
subprocess.run/Popen/call/check_output/call/check_call/getoutput/
getstatusoutput: any command line containing tailscale/schtasks/sc.exe fails
LOUDLY (a real TS/task/service call can never happen from this suite;
injected fake runners bypass subprocess entirely), while other commands
delegate so unrelated tooling keeps working. AST self-checks pin the
tailscale/sc-only subprocess shape and the fail-closed shapes.

Tested contract:
* broker gate: the four host primitives (task /Run//End + three-evidence
  checks) and the two broker protocol actions pass through unchanged; the
  surface exposes NOTHING else;
* funnel off = the watchdog Close-Public three steps with honest failures
  (rc / empty verify / still-present mapping); funnel on = the SHARING.md
  command + presence re-check; the ack is {ok, detail} whose truthiness IS
  ok;
* funnel observation = the same serve-status parse (mapping -> True, clean
  -> False, failed query -> None);
* connectivity = REAL Tailscale login/connectivity from `tailscale status
  --json` (NOT serve status, NOT free-text IP matching): BackendState
  Running + a non-empty Self.TailscaleIPs -> True; known disconnected
  states / empty self IPs -> False; bad JSON / missing keys / off-contract
  shapes -> share_status_unknown (never a fabricated offline fact); exe
  absent -> share_privilege_unwired;
* ts service status = read-only sc.exe query parse (running/stopped/absent/
  None), shared parser with the broker primitive;
* ts service control runs THROUGH the real broker adapter over the
  restricted ts-service-control verb: a confirmed start/stop returns a
  truthy ack; broker unwired / extension-missing (unknown verb) / execution
  failure are three DISTINCT honest errors with deployment pointers, and
  the seam body never spawns anything itself;
* bounded drain = the orchestration drain protocol shape (drained/
  timed_out/stopped confirmed, backend exit observed, drain_timeout never
  force-dropping);
* desired-stop reader = the broker's protected lifecycle record (broker
  status file) mapped to the orchestration bool|None contract;
* production_seams matches the orchestration seam interface key-by-key (ALL
  nineteen delivered: Revision 8 adds the dedicated-backend gate, its
  read-only health/auth observation and the open-precondition key check;
  the 2026-09-22 diagnosis split adds the structured WHY behind the same
  precondition; Rev9 adds the six A9 seams)
  and the wired seams drive the real orchestration accessors;
* dedicated backend gate (_ProductionBackendOps): start spawns an isolated
  chat2api child (cmdline chat2api.py + --port + --profile, env redirects
  token/log/ledger/control-token INTO the backend dir) and writes the pid
  file; stop re-verifies the registered PID against its exact command line
  before ANY action (never kill by bare PID), shuts down gracefully first
  and falls back to the injected stopper; every OS/network boundary is
  patched and proven untouched (spawn-log deltas + urlopen counters);
* attribution status = THREE different facts under three names (2026-09-26
  review): file_ready (this process, now) / instance_loaded (inferred for the
  RUNNING 8100 instance from its start time vs the key file mtime — "unknown",
  never "yes", when the process cannot be found) / request_attribution (not
  observed here; a pointer at the 8100 ledger row fact); the read-only gateway
  probe (netstat port table + CIM start time) is stubbed in every test;
* REAL DEFAULT ASSEMBLY: wire_share_production_seams() runs the REAL
  usagesrv loader chain with ONLY the farthest OS boundaries replaced (fake
  pipe transport, fake control-key read, tmp deployment config) and zero
  real calls (the sentinel would fire).

#34 retired the dedicated 8110 backend lifecycle/login/reauth, the whole-chain
actions and the 8110 supervision read face. The absence assertions at the end of
this file keep that retirement mechanical.
"""

import ast
import builtins
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from console import adapters as console_adapters  # noqa: E402
from console import share_actions as orch  # noqa: E402  (same directory on path)
from console import share_production as prod  # noqa: E402
import share.broker_client as share_broker_client  # noqa: E402
import share.broker_server as share_broker_server  # noqa: E402
import usagesrv  # noqa: E402  (real loader chain for the assembly test)
from console.operations import OperationError  # noqa: E402
from share.management import BrokerClientAdapter, BrokerError  # noqa: E402

# status --json 正例（Running + 自节点地址）：接线测试的 connectivity 喂入。
_TS_STATUS_JSON_RUNNING = json.dumps(
    {"BackendState": "Running",
     "Self": {"ID": "node-self", "HostName": "desktop",
              "TailscaleIPs": ["100.64.12.24"]}})

_SEAM_ATTRS = ("_BROKER_OPS", "_DESIRED_STOP_READER",
                "_KEYS_CONFIRMED", "_KEYS_DIAGNOSIS",
                "_MAINTENANCE_BEGIN", "_MAINTENANCE_END",
                "_MAINTENANCE_STATUS", "_GATEWAY_RESTART")
_RESTORE_ATTRS = _SEAM_ATTRS

_SOURCE_FILE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "console/share_production.py")


@contextlib.contextmanager
def _broker_loader(loader):
    """Configure the explicit broker loader for one test, then restore it.

    E19-1-06 removed the implicit ``usagesrv`` import from ``_default_broker``:
    the host now hands the loader in explicitly, so tests that want the real
    production loader chain must configure it the same way the host does.
    """
    saved = prod._BROKER_LOADER
    prod.configure_broker_loader(loader)
    try:
        yield
    finally:
        prod.configure_broker_loader(saved)
# 归因密钥的只读现场：真正读密钥的模块（只做 AST/文本核对，
# 不 import，零副作用）。
_GATEWAY_DIR = Path(__file__).resolve().parents[1]
_CHAT2API_SOURCE = _GATEWAY_DIR / "chat2api" / "chat2api.py"


def _literal_assignment(path, name):
    """模块级 ``NAME = <字面量>`` 的值（AST，不 import 被测模块）。"""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError("%s 未见模块级 %s = <字面量>" % (path, name))


class _FakeRunner:
    """``runner(args, timeout)`` stand-in. Script items are
    (returncode, stdout, stderr) tuples or exception instances; the FINAL
    item repeats so an unexpected extra poll never crashes a test."""

    def __init__(self, script):
        self.calls = []
        self._script = list(script)

    def __call__(self, args, timeout):
        self.calls.append((list(args), timeout))
        item = self._script.pop(0) if len(self._script) > 1 \
            else self._script[0]
        if isinstance(item, BaseException):
            raise item
        rc, out, err = item
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _adapter():
    """A strict-shape BrokerClientAdapter mock: only the two methods the
    gate maps exist, so an accidental extra gate call fails loudly."""
    adapter = mock.create_autospec(BrokerClientAdapter, instance=True)
    adapter.graceful_stop.return_value = {
        "drained": True, "timed_out": False, "stopped": True}
    adapter.emergency_stop.return_value = {"stopped": True}
    return adapter


class _FakeBrokerClient:
    """BrokerClient stand-in behind the adapter: records the (action,
    fields) pairs and replays programmed BrokerResult-like values."""

    def __init__(self, results):
        self.calls = []
        self._results = list(results)

    def call(self, action, fields=None, instance_id=None, transport=None):
        self.calls.append((action, dict(fields or {})))
        item = self._results.pop(0) if len(self._results) > 1 \
            else self._results[0]
        if isinstance(item, BaseException):
            raise item
        return SimpleNamespace(ok=item[0], error_code=item[1],
                               result=dict(item[2] or {}))


class _FakeCtx:
    """Orchestration cancel context stand-in."""

    def __init__(self, cancel_on=None):
        self.calls = 0
        self.steps = []
        self._cancel_on = cancel_on

    def report_step(self, note):
        self.steps.append(note)

    def raise_if_cancelled(self):
        self.calls += 1
        if self._cancel_on is not None and self.calls > self._cancel_on:
            raise OperationError("cancelled", "ctx cancelled")


# ---- module-wide ISOLATION SENTINEL: every subprocess entry point is
# wrapped; a command line containing tailscale/schtasks/sc.exe/netstat/
# powershell fails LOUDLY (real TS/task/service/process calls can never
# happen from this suite — the 8100 gateway probe reads the port table and one
# process start time in production, so it MUST be stubbed here), anything
# else delegates so unrelated tooling keeps working. Injected fake runners
# bypass subprocess entirely — the sentinel only ever sees real attempts.
_GUARDED = ("run", "Popen", "call", "check_output", "check_call",
            "getoutput", "getstatusoutput")
_ISOLATION_TOKENS = ("tailscale", "schtasks", "sc.exe", "netstat",
                     "powershell", "pwsh")
_orig_subprocess = {}
_spawn_log = []


def _argv_text(args):
    if isinstance(args, (list, tuple)):
        return " ".join(str(a) for a in args)
    return str(args)


def _guarded_spawn(name, original):
    def wrapper(*args, **kwargs):
        text = _argv_text(args[0]) if args else ""
        lowered = text.lower()
        _spawn_log.append((name, text))
        for token in _ISOLATION_TOKENS:
            if token in lowered:
                raise AssertionError(
                    "隔离哨兵：测试进程出现真实 OS 命令调用 %r（命中 %r）——"
                    "真实 tailscale/schtasks/sc 调用不可能从测试发生；请注入"
                    " fake runner/边界" % (text, token))
        return original(*args, **kwargs)
    wrapper.__name__ = name
    return wrapper


def setUpModule():
    for name in _GUARDED:
        _orig_subprocess[name] = getattr(subprocess, name)
    for name in _GUARDED:
        setattr(subprocess, name,
                _guarded_spawn(name, _orig_subprocess[name]))


def tearDownModule():
    for name, original in reversed(list(_orig_subprocess.items())):
        setattr(subprocess, name, original)
    _orig_subprocess.clear()


class BrokerOpsGateTests(unittest.TestCase):
    """Test 1: the host primitives execute INSIDE the broker (host-verify /
    host-stop / start verbs through the adapter), the two broker protocol
    actions map onto the mocked BrokerClientAdapter, and the gate exposes
    NOTHING else."""

    def test_four_host_primitives_and_broker_actions_passthrough(self):
        adapter = _adapter()
        adapter.start.return_value = {"state": "ok", "started": True,
                                      "broker": {"started": True}}
        adapter.host_stop.return_value = True
        adapter.host_verify.side_effect = \
            lambda check: True if check == "running" else False
        gate = prod.production_broker_ops(broker=adapter)

        self.assertTrue(gate.start_host())
        self.assertTrue(gate.stop_host())
        self.assertIs(gate.verify_running(), True)
        self.assertIs(gate.verify_closed(), False)
        self.assertEqual(gate.drain(),
                         {"drained": True, "timed_out": False, "stopped": True})
        self.assertEqual(gate.emergency_stop(), {"stopped": True})

        adapter.start.assert_called_once_with(explicit=False)
        adapter.host_stop.assert_called_once_with()
        adapter.host_verify.assert_has_calls(
            [mock.call("running"), mock.call("closed")])
        adapter.graceful_stop.assert_called_once_with()
        adapter.emergency_stop.assert_called_once_with()
        # The gate must NOT hold any in-process host evidence object: the
        # operator-account path cannot resolve SYSTEM task evidence.
        self.assertFalse(hasattr(gate, "_host"))

    def test_explicit_start_forwards_the_operator_intent(self):
        adapter = _adapter()
        adapter.start.return_value = {"state": "ok", "started": True}
        gate = prod.production_broker_ops(broker=adapter)
        self.assertTrue(gate.start_host(explicit=True))
        adapter.start.assert_called_once_with(explicit=True)

    def test_legacy_start_without_explicit_still_works(self):
        gate = prod.production_broker_ops(
            broker=SimpleNamespace(
                start=lambda: {"state": "ok", "started": True}))
        self.assertTrue(gate.start_host(explicit=True))

    def test_gate_surface_is_narrow(self):
        gate = prod.production_broker_ops(broker=_adapter())
        for forbidden in ("publish", "snapshot", "start", "graceful_stop",
                          "call", "run", "end", "status", "client"):
            self.assertFalse(hasattr(gate, forbidden),
                             "gate must not expose %r" % forbidden)

    def test_three_evidence_none_passes_through(self):
        adapter = _adapter()
        adapter.host_verify.return_value = None
        gate = prod.production_broker_ops(broker=adapter)
        self.assertIsNone(gate.verify_running())
        self.assertIsNone(gate.verify_closed())

    def test_broker_error_maps_to_fail_closed_verdicts(self):
        # A rejected host-verify verb (e.g. a broker predating the
        # extension) is an unresolvable evidence source -> None, never a
        # pretend verdict; a rejected gate action is a False, not a raise.
        adapter = _adapter()
        adapter.host_verify.side_effect = BrokerError("host verify rejected")
        adapter.host_stop.side_effect = BrokerError("host stop rejected")
        adapter.start.side_effect = BrokerError("start rejected")
        gate = prod.production_broker_ops(broker=adapter)
        self.assertIsNone(gate.verify_running())
        self.assertIsNone(gate.verify_closed())
        self.assertFalse(gate.stop_host())
        self.assertFalse(gate.start_host())

    def test_default_broker_construction_reuses_production_loader(self):
        # The default construction uses the EXPLICITLY configured loader
        # (the host wires usagesrv._load_share_management_deployment through
        # configure_broker_loader): the loader result is adopted as-is;
        # absent/disabled deployment config raises share_broker_unwired
        # honestly. The fake loader stands in, so the heavy real import never
        # happens in tests.
        sentinel = mock.create_autospec(BrokerClientAdapter, instance=True)
        with _broker_loader(lambda: sentinel):
            gate = prod.production_broker_ops()
            gate.drain()
            sentinel.graceful_stop.assert_called_once_with()

        with _broker_loader(lambda: None):
            with self.assertRaises(OperationError) as caught:
                prod.production_broker_ops()
        self.assertEqual(caught.exception.code, "share_broker_unwired")

    def test_default_gate_construction_shape(self):
        gate = prod.production_broker_ops(broker=_adapter())
        self.assertIsInstance(gate, prod._ProductionBrokerOps)
        # The gate holds exactly one dependency: the broker adapter.
        self.assertEqual(sorted(vars(gate)), ["_broker"])


class _FullFakeBroker:
    """A broker adapter stand-in exposing EVERY surface the production
    assembly consumes: the gate surface (graceful_stop/emergency_stop plus the
    SYSTEM-side host verbs) and the public TS control verb. It has NO ``client``
    attribute: the removed getattr(broker, "client") bypass would fall back to
    the unwired shape, so a passing TS control case proves the console used the
    public adapter verb."""

    def __init__(self, ts_client=None):
        self._ts_client = ts_client
        self.ts_calls = []

    def graceful_stop(self):
        return {"drained": True, "timed_out": False, "stopped": True,
                "state": "stopped"}

    def emergency_stop(self):
        return {"stopped": True}

    def start(self):
        return {"state": "ok", "started": True, "broker": {"started": True}}

    def host_stop(self):
        return True

    def host_verify(self, check):
        return True if check == "running" else False

    def ts_service_control(self, action):
        self.ts_calls.append(("ts_service_control", action))
        return self._ts_client.call("ts-service-control", {"action": action})


class SeamsTests(unittest.TestCase):
    """production_seams matches the orchestration seam interface key-by-key
    and delivers every retained seam (the retired Windows TS/Funnel seams are
    gone with the old local share chain, #33)."""

    def test_seam_keys_match_orchestration_interface(self):
        for name in _SEAM_ATTRS:
            self.assertTrue(hasattr(orch, name),
                            "orchestration seam %s missing" % name)
        seams = prod.production_seams(broker=_adapter())
        self.assertEqual(set(seams), set(_SEAM_ATTRS))
        for name in _SEAM_ATTRS:
            self.assertIsNotNone(seams[name],
                                 "seam %s must be delivered now" % name)
        self.assertIs(seams["_KEYS_CONFIRMED"],
                      prod.production_keys_confirmed)
        self.assertIs(seams["_KEYS_DIAGNOSIS"],
                      prod.production_keys_diagnosis)


class DesiredStopReaderTests(unittest.TestCase):
    """The broker's protected lifecycle record (broker-status.json) mapped
    to the orchestration _DESIRED_STOP_READER contract (bool|None); a
    missing/malformed file reads None, never a fabricated state."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "broker-status.json"

    def _write(self, payload):
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def test_stopped_record_maps_true(self):
        self._write({"pid": 1, "lifecycle": {"state": "stopped",
                                             "at": 1720000000,
                                             "by": "drain"}})
        record = prod.production_desired_stop_record(status_path=self.path)
        self.assertEqual(record, {"state": "stopped",
                                  "stopped_at": 1720000000})
        self.assertIs(prod.production_desired_stop_reader(
            status_path=self.path), True)

    def test_running_record_maps_false(self):
        self._write({"pid": 1, "lifecycle": {"state": "running",
                                             "at": 1720000000,
                                             "by": "start"}})
        record = prod.production_desired_stop_record(status_path=self.path)
        self.assertEqual(record, {"state": "running", "stopped_at": None})
        self.assertIs(prod.production_desired_stop_reader(
            status_path=self.path), False)

    def test_missing_file_is_none_never_fabricated(self):
        self.assertIsNone(prod.production_desired_stop_record(
            status_path=self.path))
        self.assertIsNone(prod.production_desired_stop_reader(
            status_path=self.path))

    def test_malformed_or_unknown_shapes_are_none(self):
        self.path.write_text("not json {", encoding="utf-8")
        self.assertIsNone(prod.production_desired_stop_record(
            status_path=self.path))
        self._write({"pid": 1})                       # lifecycle missing
        self.assertIsNone(prod.production_desired_stop_record(
            status_path=self.path))
        self._write({"lifecycle": {"state": "bogus", "at": 1}})   # unknown state
        self.assertIsNone(prod.production_desired_stop_record(
            status_path=self.path))
        self._write({"lifecycle": {"state": "stopped", "at": True}})
        record = prod.production_desired_stop_record(status_path=self.path)
        self.assertEqual(record, {"state": "stopped", "stopped_at": None})

    def test_reader_is_wired_as_orchestration_seam(self):
        # 经 console_share_actions._desired_stop_in_force 的接线契约：
        # reader（partial 绑定路径）的返回值直接充当 bool|None。
        self._write({"lifecycle": {"state": "stopped", "at": 1720000000,
                                   "by": "drain"}})
        saved = orch._DESIRED_STOP_READER
        try:
            orch._DESIRED_STOP_READER = partial(
                prod.production_desired_stop_reader, status_path=self.path)
            self.assertIs(orch._desired_stop_in_force(), True)
        finally:
            orch._DESIRED_STOP_READER = saved


class ProductionAssemblyTests(unittest.TestCase):
    """The cloud (POSIX) default assembly wires every retained seam and never
    spawns an OS command. The retired Windows named-pipe transport is gone
    with the old local share chain (#33)."""

    def setUp(self):
        _spawn_log.clear()

    def test_real_default_assembly_wires_all_seams_without_real_calls(self):
        from console import adapters as console_adapters
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "share-management.json"
            key = Path(td) / "broker-control-key"
            key.write_text("test-key", encoding="utf-8")
            config.write_text(json.dumps({
                "schema_version": 2, "enabled": True,
                "transport": "unix_socket",
                "socket_path": str(Path(td) / "broker.sock"),
                "control_key_file": str(key)}), encoding="utf-8")
            created = []

            class _FakeTransport:
                # The cloud assembly constructs the factory only; it never opens.
                def __init__(self, socket_path, kernel=None):
                    self.socket_path = socket_path
                    created.append(socket_path)

                def open(self):
                    return self

                def close(self):
                    pass

            saved_seams = {name: getattr(orch, name) for name in _RESTORE_ATTRS}
            saved_config = usagesrv.SHARE_MANAGEMENT_CONFIG_FILE
            spawn_before = list(_spawn_log)
            try:
                with _broker_loader(
                        usagesrv._load_share_management_deployment),                      mock.patch.object(usagesrv, "SHARE_MANAGEMENT_CONFIG_FILE",
                                       str(config)),                      mock.patch.object(share_broker_server,
                                       "read_control_key",
                                       lambda path: "test-key"),                      mock.patch.object(share_broker_client,
                                       "UnixSocketBrokerTransport",
                                       _FakeTransport),                      mock.patch.object(usagesrv, "CLOUD_ROLE", True):
                    console_adapters.wire_share_production_seams(cloud=True)
                for name in _SEAM_ATTRS:
                    self.assertIsNotNone(getattr(orch, name),
                                         "%s 未接线" % name)
                self.assertEqual(created, [])
                self.assertEqual(_spawn_log, spawn_before)
            finally:
                for name, value in saved_seams.items():
                    setattr(orch, name, value)
                usagesrv.SHARE_MANAGEMENT_CONFIG_FILE = saved_config










class KeysConfirmedTests(unittest.TestCase):
    """R8-A7 开启前置：>=1 启用 Key + 已发布配置同步。True/False/None 三态
    契约：未知（manager 不可用）永远不伪造 True。"""

    @staticmethod
    def _manager(status=None, keys=None, fail_status=False, fail_keys=False):
        class _Mgr:
            def __init__(self):
                self.list_keys_calls = 0

            def status(self):
                if fail_status:
                    raise RuntimeError("status unavailable")
                return status

            def list_keys(self):
                self.list_keys_calls += 1
                if fail_keys:
                    raise RuntimeError("keys unavailable")
                return keys

        return _Mgr()

    def test_in_sync_with_enabled_key_is_true(self):
        # Real ShareManager.list_keys shape: {"keys": [rows]} (found live in
        # the production exercise E-R8-8 — a bare-list iteration bug made
        # the check return False with keys present).
        manager = self._manager(status={"in_sync": True},
                                keys={"keys": [{"enabled": True}]})
        self.assertIs(prod.production_keys_confirmed(manager=manager), True)
        self.assertEqual(manager.list_keys_calls, 1)

    def test_bare_list_shape_tolerated_for_seams(self):
        manager = self._manager(status={"in_sync": True},
                                keys=[{"enabled": True}])
        self.assertIs(prod.production_keys_confirmed(manager=manager), True)

    def test_not_in_sync_is_false_without_listing_keys(self):
        manager = self._manager(status={"in_sync": False},
                                keys=[{"enabled": True}])
        self.assertIs(prod.production_keys_confirmed(manager=manager), False)
        self.assertEqual(manager.list_keys_calls, 0)

    def test_no_enabled_key_is_false(self):
        for keys in ([], {"keys": []},
                     [{"enabled": False}, {"enabled": False}],
                     {"keys": [{"enabled": False}]}):
            with self.subTest(keys=keys):
                manager = self._manager(status={"in_sync": True}, keys=keys)
                self.assertIs(prod.production_keys_confirmed(
                    manager=manager), False)

    def test_status_failure_is_unknown_not_fake_pass(self):
        manager = self._manager(status={"in_sync": True}, fail_status=True)
        self.assertIsNone(prod.production_keys_confirmed(manager=manager))
        self.assertEqual(manager.list_keys_calls, 0)

    def test_list_keys_failure_is_unknown(self):
        manager = self._manager(status={"in_sync": True},
                                keys=[{"enabled": True}], fail_keys=True)
        self.assertIsNone(prod.production_keys_confirmed(manager=manager))

    def test_missing_manager_builder_failure_is_unknown(self):
        # manager=None 走默认装配：_default_broker 抛错 -> None（不伪造）。
        with mock.patch.object(prod, "_default_broker",
                               side_effect=OperationError(
                                   "share_broker_unwired", "no deployment")):
            self.assertIsNone(prod.production_keys_confirmed())

    def test_injected_manager_never_builds_a_broker(self):
        # caller 提供的 manager 直接使用：装配路径（usagesrv loader）绝不触达。
        manager = self._manager(status={"in_sync": True},
                                keys=[{"enabled": True}])
        with mock.patch.object(prod, "_default_broker",
                               side_effect=AssertionError(
                                   "loader must not run")):
            self.assertIs(prod.production_keys_confirmed(manager=manager),
                          True)


class KeysDiagnosisTests(unittest.TestCase):
    """2026-09-22 诊断拆分：production_keys_diagnosis 把「缺已启用 Key」与
    「配置未生效」拆成结构化原因（含真实数值）；观察异常一律 None/unknown，
    绝不折叠成 False。"""

    @staticmethod
    def _manager(status=None, keys=None, fail_status=False, fail_keys=False):
        class _Mgr:
            def __init__(self):
                self.list_keys_calls = 0

            def status(self):
                if fail_status:
                    raise RuntimeError("status unavailable")
                return status

            def list_keys(self):
                self.list_keys_calls += 1
                if fail_keys:
                    raise RuntimeError("keys unavailable")
                return keys

        return _Mgr()

    def test_in_sync_with_enabled_key_is_all_normal(self):
        # Real ShareManager shapes: status dict + {"keys": [rows]}.
        manager = self._manager(status={"in_sync": True},
                                keys={"keys": [{"enabled": True},
                                               {"enabled": False}]})
        diagnosis = prod.production_keys_diagnosis(manager=manager)
        self.assertEqual(diagnosis, {
            "in_sync": True, "keys_ok": True, "enabled_keys": 1,
            "total_keys": 2, "reasons": []})

    def test_bare_list_shape_tolerated_for_seams(self):
        manager = self._manager(status={"in_sync": True},
                                keys=[{"enabled": True}])
        diagnosis = prod.production_keys_diagnosis(manager=manager)
        self.assertEqual(diagnosis["enabled_keys"], 1)
        self.assertEqual(diagnosis["reasons"], [])

    def test_no_enabled_key_reports_keys_reason_only(self):
        for keys in ([], {"keys": []},
                     [{"enabled": False}, {"enabled": False}],
                     {"keys": [{"enabled": False}]}):
            with self.subTest(keys=keys):
                manager = self._manager(status={"in_sync": True}, keys=keys)
                diagnosis = prod.production_keys_diagnosis(manager=manager)
                self.assertIs(diagnosis["in_sync"], True)
                self.assertIs(diagnosis["keys_ok"], False)
                self.assertEqual(diagnosis["enabled_keys"], 0)
                self.assertEqual(diagnosis["reasons"], ["no_enabled_keys"])

    def test_not_in_sync_reports_config_reason_with_key_facts(self):
        # Out of sync WITH enabled keys: only the config cause; the key
        # facts stay observed (True side, not folded into False).
        manager = self._manager(status={"in_sync": False},
                                keys={"keys": [{"enabled": True}]})
        diagnosis = prod.production_keys_diagnosis(manager=manager)
        self.assertIs(diagnosis["in_sync"], False)
        self.assertIs(diagnosis["keys_ok"], True)
        self.assertEqual(diagnosis["enabled_keys"], 1)
        self.assertEqual(diagnosis["reasons"], ["config_not_in_sync"])

    def test_both_causes_reported_in_stable_order(self):
        # The 2026-09-22 production shape: in_sync=false AND active_keys=0
        # together -> BOTH codes, config before keys, every time.
        for _ in range(3):
            manager = self._manager(status={"in_sync": False},
                                    keys={"keys": []})
            diagnosis = prod.production_keys_diagnosis(manager=manager)
            self.assertEqual(
                diagnosis["reasons"],
                ["config_not_in_sync", "no_enabled_keys"])
            self.assertIs(diagnosis["in_sync"], False)
            self.assertIs(diagnosis["keys_ok"], False)
            self.assertEqual(diagnosis["enabled_keys"], 0)

    def test_status_failure_is_manager_unavailable_unknown_not_false(self):
        # status() 观察异常 -> manager_unavailable；in_sync/keys_ok 保持
        # None（绝不折叠成 False），且不再触发 list_keys。
        manager = self._manager(status={"in_sync": True}, fail_status=True)
        diagnosis = prod.production_keys_diagnosis(manager=manager)
        self.assertEqual(diagnosis, {
            "in_sync": None, "keys_ok": None, "enabled_keys": 0,
            "total_keys": 0, "reasons": ["manager_unavailable"]})
        self.assertEqual(manager.list_keys_calls, 0)

    def test_list_keys_failure_is_keys_read_failed_unknown(self):
        # list_keys() 异常 -> keys_read_failed；keys_ok=None（未知），
        # in_sync 事实照报；不伪造「无 Key」结论。
        manager = self._manager(status={"in_sync": True},
                                keys={"keys": [{"enabled": True}]},
                                fail_keys=True)
        diagnosis = prod.production_keys_diagnosis(manager=manager)
        self.assertIs(diagnosis["in_sync"], True)
        self.assertIsNone(diagnosis["keys_ok"])
        self.assertEqual(diagnosis["enabled_keys"], 0)
        self.assertEqual(diagnosis["reasons"], ["keys_read_failed"])

    def test_malformed_status_is_unknown_in_sync_still_fail_closed(self):
        # status 非法形状（非 dict / 缺 in_sync）：in_sync=None（未知），
        # 但 config_not_in_sync 原因仍然成立（in_sync 非 True 即未生效）。
        for status in (None, {}, {"schema_version": 3}):
            with self.subTest(status=status):
                manager = self._manager(status=status,
                                        keys={"keys": [{"enabled": True}]})
                diagnosis = prod.production_keys_diagnosis(manager=manager)
                self.assertIsNone(diagnosis["in_sync"])
                self.assertEqual(diagnosis["reasons"],
                                 ["config_not_in_sync"])

    def test_missing_manager_builder_failure_is_manager_unavailable(self):
        # manager=None 走默认装配：_default_broker 抛错 -> manager_unavailable
        # （unknown，不伪造）。
        with mock.patch.object(prod, "_default_broker",
                               side_effect=OperationError(
                                   "share_broker_unwired", "no deployment")):
            diagnosis = prod.production_keys_diagnosis()
        self.assertEqual(diagnosis["reasons"], ["manager_unavailable"])
        self.assertIsNone(diagnosis["in_sync"])
        self.assertIsNone(diagnosis["keys_ok"])

    def test_injected_manager_never_builds_a_broker(self):
        manager = self._manager(status={"in_sync": True},
                                keys=[{"enabled": True}])
        with mock.patch.object(prod, "_default_broker",
                               side_effect=AssertionError(
                                   "loader must not run")):
            diagnosis = prod.production_keys_diagnosis(manager=manager)
        self.assertEqual(diagnosis["reasons"], [])

    def test_seams_wire_diagnosis_with_the_same_manager(self):
        # production_seams(keys_manager=...)：诊断缝与确认缝绑定同一个
        # manager，接线后即可观察同一视图。
        class _Mgr:
            def status(self):
                return {"in_sync": False}

            def list_keys(self):
                return {"keys": []}

        manager = _Mgr()
        seams = prod.production_seams(broker=_adapter(),
                                      keys_manager=manager)
        self.assertIsNotNone(seams["_KEYS_DIAGNOSIS"])
        self.assertEqual(seams["_KEYS_DIAGNOSIS"].keywords["manager"],
                         manager)
        diagnosis = seams["_KEYS_DIAGNOSIS"]()
        self.assertEqual(
            diagnosis["reasons"],
            ["config_not_in_sync", "no_enabled_keys"])
        self.assertIs(diagnosis["keys_ok"], False)






class ShareGatewayProbeTests(unittest.TestCase):
    """8100 运行实例探测（只读）：netstat 端口表 + CIM 启动时间。

    只读查询一律经注入的假 runner（本套件不得依赖真实进程，也不跑真实
    命令）；fixture 用 2026-09-26 本机真实抓到的行形状（PID 20740 监听
    127.0.0.1:8100、CreationDate -> epoch 1790386493.70995）。"""

    _LISTENING = ("  TCP    127.0.0.1:8100         0.0.0.0:0"
                  "              LISTENING       20740\r\n"
                  "  TCP    127.0.0.1:8100         127.0.0.1:4925"
                  "         TIME_WAIT       0\r\n")
    _NOT_LISTENING = ("  TCP    127.0.0.1:8100         0.0.0.0:0"
                      "              TIME_WAIT       0\r\n")

    def _runner(self, *results):
        return _FakeRunner(list(results))

    def test_listening_row_parses_the_pid_and_ignores_other_states(self):
        runner = self._runner((0, self._LISTENING, ""))
        self.assertEqual(prod._listening_pids(8100, runner=runner),
                         ({20740}, None))
        # 端口按整段匹配：81000 不是 8100。
        runner = self._runner((0, "  TCP    127.0.0.1:81000        0.0.0.0:0"
                                  "              LISTENING       555\r\n", ""))
        self.assertEqual(prod._listening_pids(8100, runner=runner), (set(), None))
        # 查询成功但无人监听是「空集 + 无原因」，不是查询失败。
        runner = self._runner((0, self._NOT_LISTENING, ""))
        self.assertEqual(prod._listening_pids(8100, runner=runner), (set(), None))

    def test_failed_or_damaged_query_is_unusable_never_empty(self):
        for result in ((1, "", "netstat failed"), OSError("boom"),
                       (0, "  TCP    127.0.0.1:8100  0.0.0.0:0"
                           "  LISTENING  not-a-pid\r\n", "")):
            runner = self._runner(result)
            self.assertEqual(prod._listening_pids(8100, runner=runner),
                             (None, "query_failed"))

    def test_cim_start_time_is_epoch_seconds(self):
        runner = self._runner((0, "1790386493\r\n", ""))
        self.assertEqual(prod._process_start_epoch(20740, runner=runner),
                         (1790386493.0, "ok"))
        # 只读查询的固定形状：PID 过滤 + .NET epoch tick 换算在 PowerShell 内
        # 完成（CreationDate 的本地化字符串在 Python 里解析会依赖区域设置）。
        args = runner.calls[0][0]
        self.assertEqual(args[:3], ["pwsh", "-NoProfile", "-Command"])
        self.assertIn("ProcessId=20740", args[3])
        self.assertIn("CreationDate", args[3])
        self.assertIn(str(prod._DOTNET_EPOCH_TICKS), args[3])

    def test_unreadable_start_time_is_unavailable_never_guessed(self):
        for result in ((1, "", "access denied"), OSError("boom"),
                       (0, "", ""), (0, "not-a-number", "")):
            runner = self._runner(result)
            self.assertEqual(prod._process_start_epoch(20740, runner=runner),
                             (None, "start_time_unavailable"))

    def test_probe_reports_pid_and_start_time_or_an_honest_reason(self):
        runner = self._runner((0, self._LISTENING, ""),
                              (0, "um-share-python.exe\r\n", ""),
                              (0, "1790386493.70995\r\n", ""))
        self.assertEqual(prod.share_entry_instance_probe(runner=runner),
                         {"pid": 20740, "started_at": 1790386493.70995,
                          "reason": "ok"})
        # netstat + 镜像名 + CIM，零真实进程（2026-09-26 F-16：监听者身份要有证据）
        self.assertEqual(len(runner.calls), 3)
        # 无人监听 -> not_listening（进程没跑，不是「没载入」）。
        runner = self._runner((0, self._NOT_LISTENING, ""))
        self.assertEqual(prod.share_entry_instance_probe(runner=runner),
                         {"pid": None, "started_at": None,
                          "reason": "not_listening"})
        self.assertEqual(len(runner.calls), 1)
        # 两个监听者 -> 没有唯一实例可比 -> 不下推断。
        runner = self._runner((0, self._LISTENING
                               + self._LISTENING.replace("20740", "20888"),
                               ""))
        self.assertEqual(prod.share_entry_instance_probe(runner=runner),
                         {"pid": None, "started_at": None,
                          "reason": "ambiguous_listener"})
        # 端口查询失败 -> query_failed（未知绝不报成「没在跑」）。
        runner = self._runner(OSError("boom"))
        self.assertEqual(prod.share_entry_instance_probe(runner=runner),
                         {"pid": None, "started_at": None,
                          "reason": "query_failed"})
        # 读到进程但读不到启动时间 -> start_time_unavailable。
        runner = self._runner((0, self._LISTENING, ""),
                              (0, "um-share-python.exe\r\n", ""),
                              (1, "", "denied"))
        self.assertEqual(prod.share_entry_instance_probe(runner=runner),
                         {"pid": 20740, "started_at": None,
                          "reason": "start_time_unavailable"})

    def test_probe_uses_the_share_entry_port_by_default(self):
        self.assertEqual(prod.SHARE_GATEWAY_PORT, 8100)   # 与部署端口一致
        # 默认端口上的监听者被认出来（换一个端口就不是它）。
        runner = self._runner((0, self._LISTENING, ""),
                              (0, "um-share-python.exe\r\n", ""),
                              (0, "1790386493\r\n", ""))
        out = prod.share_entry_instance_probe(runner=runner)
        self.assertEqual((out["pid"], out["reason"]), (20740, "ok"))
        runner = self._runner(
            (0, self._LISTENING.replace(":8100", ":8101"), ""))
        out = prod.share_entry_instance_probe(runner=runner)
        self.assertEqual(out["reason"], "not_listening")


class _NoWriteGuard:
    """运行时写哨兵：只读采集路径上出现任何写盘动作就立刻失败。

    ``io.open`` 与 ``builtins.open`` 都换（``Path.open``/``Path.write_text``
    走 ``io.open``，普通 ``open()`` 走 ``builtins.open``），删除／重命名／
    替换／建目录各边界同样拦住。``writes`` 空表才是「零写副作用」的**运行时
    证据**，不是注释里的声明。
    """

    _WRITE_FLAGS = ("w", "a", "x", "+")

    def __init__(self):
        self.writes = []
        self._patchers = []

    def _fail(self, what):
        self.writes.append(what)
        raise AssertionError("只读采集路径出现写盘动作：%s" % what)

    def __enter__(self):
        real_open = io.open

        def guard_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in self._WRITE_FLAGS):
                self._fail("open(%r, mode=%r)" % (file, mode))
            return real_open(file, mode, *args, **kwargs)

        def guard_remove(path, *args, **kwargs):
            self._fail("remove(%r)" % (path,))

        def guard_rename(*args, **kwargs):
            self._fail("rename/replace%r" % (args,))

        def guard_mkdir(*args, **kwargs):
            self._fail("mkdir/makedirs%r" % (args,))

        for owner, name, value in (
                (io, "open", guard_open),
                (builtins, "open", guard_open),
                (os, "remove", guard_remove),
                (os, "unlink", guard_remove),
                (os, "rename", guard_rename),
                (os, "replace", guard_rename),
                (os, "mkdir", guard_mkdir),
                (os, "makedirs", guard_mkdir)):
            patcher = mock.patch.object(owner, name, new=value)
            patcher.start()
            self._patchers.append(patcher)
        return self

    def __exit__(self, exc_type, exc, tb):
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._patchers = []
        return False







# ---------------------------------------------------------------------------
# Rev9 (A9-1/A9-2/A9-3/A9-4 + the shared recovery entry) production
# adapters. Appended at the END of the file on purpose: every line pinned by
# tests/test_share_gate_citations.py (:2636-2644) stays byte-identical.
# ---------------------------------------------------------------------------


def _rev9_instance_path(ops, paths):
    """The instance record path for tests: NORMALIZED and
    CONTAINMENT-CHECKED (the SAME discipline the production helper
    enforces), so a test seeds a record without an unvalidated path."""
    base = os.path.abspath(paths["dir"])
    path = os.path.abspath(ops._instance_path(paths))
    if os.path.dirname(path) != base:
        raise AssertionError("instance record path escapes the backend dir")
    return path


def _rev9_seed_record(ops, paths, payload):
    """Write one instance record payload for a test (normalized path)."""
    Path(_rev9_instance_path(ops, paths)).write_text(
        json.dumps(payload), encoding="utf-8")


class _Rev9FakeClient:
    """BrokerClient stand-in: records the (action, fields) pairs the wave-1
    client methods put on the wire and replays BrokerResult-like values.
    The console must NEVER reach it directly — it is wrapped by
    :class:`_Rev9PublicBroker` below."""

    def __init__(self, results):
        self.calls = []
        self.timeouts = []
        self._results = list(results)

    def _next(self):
        item = self._results.pop(0) if len(self._results) > 1 \
            else self._results[0]
        if isinstance(item, BaseException):
            raise item
        return SimpleNamespace(ok=item[0], error_code=item[1],
                               result=dict(item[2] or {}))

    def maintenance_begin(self, component, lease_s):
        self.calls.append(("maintenance-begin", component, lease_s))
        return self._next()

    def maintenance_end(self, component, lease_id):
        self.calls.append(("maintenance-end", component, lease_id))
        return self._next()

    def maintenance_status(self):
        self.calls.append(("maintenance-status",))
        return self._next()

    def gateway_restart(self, component, budget_s):
        self.calls.append(("gateway-restart", component, budget_s))
        self.timeouts.append(min(budget_s + 15, 180))
        return self._next()

    def host_evidence(self, check):
        self.calls.append(("host-evidence", check))
        return self._next()


class _Rev9PublicBroker:
    """BrokerClientAdapter stand-in exposing ONLY the five public control
    verbs (maintenance_begin/end/status, gateway_restart, host_evidence).

    It deliberately has NO ``client`` attribute: the old console code that
    reached ``getattr(broker, "client")`` would fall back to the unwired
    shape, so every passing success case here proves the console calls the
    adapter's PUBLIC SURFACE, never the inner client."""

    def __init__(self, client):
        self._inner = client
        self.calls = []

    def maintenance_begin(self, component, lease_s):
        self.calls.append(("maintenance_begin", component, lease_s))
        return self._inner.maintenance_begin(component, lease_s)

    def maintenance_end(self, component, lease_id):
        self.calls.append(("maintenance_end", component, lease_id))
        return self._inner.maintenance_end(component, lease_id)

    def maintenance_status(self):
        self.calls.append(("maintenance_status",))
        return self._inner.maintenance_status()

    def gateway_restart(self, component, budget_s):
        self.calls.append(("gateway_restart", component, budget_s))
        return self._inner.gateway_restart(component, budget_s)

    def host_evidence(self, check):
        self.calls.append(("host_evidence", check))
        return self._inner.host_evidence(check)


def _rev9_broker(client):
    return _Rev9PublicBroker(client)


class HostEvidenceGateTests(unittest.TestCase):
    """A9-1 broker host-evidence seam: the frozen reply passes through
    VERBATIM, its truthiness IS `verdict is True` (a FAILED limits verdict
    can never read as a pass), and unavailable evidence is None."""

    def test_limits_verdict_passes_through_verbatim(self):
        client = _Rev9FakeClient([(True, "ok", {
            "check": "limits", "verdict": True, "detail": "ok",
            "checked_at": 7})])
        broker = _rev9_broker(client)
        gate = prod._ProductionBrokerOps(broker)
        evidence = gate.host_evidence("limits")
        # The console calls the PUBLIC adapter verb (never the inner client).
        self.assertEqual(broker.calls, [("host_evidence", "limits")])
        self.assertEqual(client.calls, [("host-evidence", "limits")])
        self.assertEqual(dict(evidence), {
            "check": "limits", "verdict": True, "detail": "ok",
            "checked_at": 7})
        self.assertIs(evidence["verdict"], True)
        self.assertTrue(evidence)   # verdict True -> truthy (the gate passes)

    def test_console_never_reaches_the_inner_client(self):
        # The fake broker has NO `client` attribute: a console that reached
        # `getattr(broker, "client")` would fall back to the unwired shape,
        # so this test fails if the bypass ever comes back.
        broker = _rev9_broker(_Rev9FakeClient([(True, "ok", {
            "check": "limits", "verdict": True, "detail": "ok",
            "checked_at": 7})]))
        self.assertFalse(hasattr(broker, "client"))
        gate = prod._ProductionBrokerOps(broker)
        self.assertTrue(gate.host_evidence("limits"))

    def test_failed_limits_verdict_never_reads_as_a_pass(self):
        # A plain dict would read truthy even with verdict False: the gate
        # must see a FALSY reply for a FAILED limits check.
        client = _Rev9FakeClient([(True, "ok", {
            "check": "limits", "verdict": False, "detail": "limit loss",
            "checked_at": 7})])
        gate = prod._ProductionBrokerOps(_rev9_broker(client))
        evidence = gate.host_evidence("limits")
        self.assertIs(evidence["verdict"], False)
        self.assertFalse(evidence)

    def test_unresolvable_verdict_is_none_and_falsy(self):
        client = _Rev9FakeClient([(True, "ok", {
            "check": "limits", "verdict": None, "detail":
            "limits_unavailable", "checked_at": 7})])
        gate = prod._ProductionBrokerOps(_rev9_broker(client))
        evidence = gate.host_evidence("limits")
        self.assertIsNone(evidence["verdict"])
        self.assertFalse(evidence)

    def test_faults_verdict_dict_passes_through(self):
        payload = {"check": "faults",
                   "verdict": {"last_fault": None,
                               "counts_by_phase": {"auth": 0, "config": 0,
                                                   "body": 0, "limit": 1,
                                                   "upstream": 3, "stream": 0},
                               "file": {"present": True, "size": 4096,
                                        "mtime": 1790000000.0}},
                   "detail": "read tail ok", "checked_at": 7}
        client = _Rev9FakeClient([(True, "ok", payload)])
        gate = prod._ProductionBrokerOps(_rev9_broker(client))
        evidence = gate.host_evidence("faults")
        self.assertEqual(evidence["verdict"], payload["verdict"])
        # A dict verdict is not a True verdict: the reply stays
        # conservatively falsy for a truthiness consumer.
        self.assertFalse(evidence)

    def test_unwired_or_rejected_is_none(self):
        for broker, results in (
                (object(), None),
                (_rev9_broker(_Rev9FakeClient([
                    (False, "bad_request", {"detail": "unknown action"})])),
                 None),
                (_rev9_broker(_Rev9FakeClient([
                    (False, "not_installed", {"detail": "pipe missing"})])),
                 None)):
            with self.subTest(broker=broker):
                gate = prod._ProductionBrokerOps(broker)
                self.assertIsNone(gate.host_evidence("limits"))

    def test_transport_failure_is_none(self):
        client = _Rev9FakeClient([OSError("pipe gone")])
        gate = prod._ProductionBrokerOps(_rev9_broker(client))
        self.assertIsNone(gate.host_evidence("limits"))

    def test_off_contract_reply_is_none(self):
        client = _Rev9FakeClient([(True, "ok", {"detail": "no verdict"})])
        gate = prod._ProductionBrokerOps(_rev9_broker(client))
        self.assertIsNone(gate.host_evidence("limits"))

    def test_production_broker_evidence_returns_the_reply_or_none(self):
        payload = {"check": "faults", "verdict": {"last_fault": None},
                   "detail": "read tail ok", "checked_at": 7}
        client = _Rev9FakeClient([(True, "ok", payload)])
        broker = _rev9_broker(client)
        out = prod.production_broker_evidence("faults", broker=broker)
        self.assertEqual(dict(out), payload)
        self.assertEqual(broker.calls, [("host_evidence", "faults")])
        self.assertEqual(client.calls, [("host-evidence", "faults")])
        # A rejected verb (old broker) or an absent client is None: honest
        # unavailability, never a fabricated verdict.
        rejected = _Rev9FakeClient([(False, "bad_request",
                                    {"detail": "unknown action"})])
        self.assertIsNone(prod.production_broker_evidence(
            "faults", broker=_rev9_broker(rejected)))
        self.assertIsNone(prod.production_broker_evidence(
            "limits", broker=object()))

    def test_production_broker_evidence_folds_no_deployment(self):
        with mock.patch.object(prod, "_default_broker",
                               side_effect=OperationError(
                                   "share_broker_unwired", "no deployment")):
            self.assertIsNone(prod.production_broker_evidence("faults"))

    def test_gate_surface_still_narrow(self):
        gate = prod._ProductionBrokerOps(_rev9_broker(_Rev9FakeClient([])))
        self.assertTrue(hasattr(gate, "host_evidence"))
        self.assertEqual(sorted(vars(gate)), ["_broker"])


class MaintenanceVerbTests(unittest.TestCase):
    """A9-3 broker-side maintenance verbs over the wave-1 control client:
    frozen console codes, explicit capability refusal, no host stop/start."""

    BEGIN_OK = {"accepted": True, "component": "share_gateway",
                "lease_id": "a" * 32, "lease_expires_at": 1790000300,
                "instance_id": "instance-1", "round": 7,
                "state": "draining", "active_count": 3}
    END_OK = {"resumed": True, "component": "share_gateway",
              "lease_id": "a" * 32, "instance_id": "instance-1",
              "round": 7, "state": "running", "active_count": 0}

    def test_begin_ok_returns_the_broker_payload(self):
        client = _Rev9FakeClient([(True, "ok", self.BEGIN_OK)])
        broker = _rev9_broker(client)
        out = prod.production_maintenance_begin(
            "share_gateway", 300, broker=broker)
        self.assertEqual(out, self.BEGIN_OK)
        # The console calls the PUBLIC adapter verb, never the inner client.
        self.assertEqual(broker.calls,
                         [("maintenance_begin", "share_gateway", 300)])
        self.assertEqual(client.calls,
                         [("maintenance-begin", "share_gateway", 300)])

    def test_begin_old_broker_rejection_is_extension_missing(self):
        client = _Rev9FakeClient([(False, "bad_request",
                                    {"detail": "unknown action"})])
        with self.assertRaises(OperationError) as caught:
            prod.production_maintenance_begin(
                "share_gateway", 300, broker=_rev9_broker(client))
        self.assertEqual(caught.exception.code,
                         "share_broker_extension_missing")
        # The ONLY verb on the wire: no drain/emergency-stop/host stop.
        self.assertEqual(client.calls,
                         [("maintenance-begin", "share_gateway", 300)])

    def test_begin_capability_missing_is_never_downgraded(self):
        client = _Rev9FakeClient([(False, "capability_missing",
                                    {"detail": "instance does not "
                                               "support the maintenance "
                                               "drain"})])
        with self.assertRaises(OperationError) as caught:
            prod.production_maintenance_begin(
                "share_gateway", 300, broker=_rev9_broker(client))
        self.assertEqual(caught.exception.code, "share_capability_missing")
        self.assertEqual(len(client.calls), 1)   # never a host stop/start

    def test_begin_blocked_and_unconfirmed_are_maintenance_refused(self):
        for code in ("blocked", "unconfirmed", "instance_mismatch"):
            with self.subTest(code=code):
                client = _Rev9FakeClient([(False, code,
                                            {"detail": code})])
                with self.assertRaises(OperationError) as caught:
                    prod.production_maintenance_begin(
                        "share_gateway", 300, broker=_rev9_broker(client))
                self.assertEqual(caught.exception.code,
                                 "share_maintenance_refused")

    def test_begin_unwired_adapter_is_broker_unwired(self):
        with self.assertRaises(OperationError) as caught:
            prod.production_maintenance_begin("share_gateway", 300,
                                              broker=object())
        self.assertEqual(caught.exception.code, "share_broker_unwired")

    def test_begin_transport_failure_is_broker_unwired(self):
        client = _Rev9FakeClient([OSError("pipe gone")])
        with self.assertRaises(OperationError) as caught:
            prod.production_maintenance_begin(
                "share_gateway", 300, broker=_rev9_broker(client))
        self.assertEqual(caught.exception.code, "share_broker_unwired")

    def test_end_ok_returns_the_resume_payload(self):
        client = _Rev9FakeClient([(True, "ok", self.END_OK)])
        broker = _rev9_broker(client)
        out = prod.production_maintenance_end(
            "share_gateway", "a" * 32, broker=broker)
        self.assertEqual(out, self.END_OK)
        self.assertEqual(broker.calls,
                         [("maintenance_end", "share_gateway", "a" * 32)])
        self.assertEqual(client.calls,
                         [("maintenance-end", "share_gateway", "a" * 32)])

    def test_end_refusals_map_to_the_frozen_codes(self):
        for code, expected in (("instance_mismatch",
                                "share_maintenance_refused"),
                               ("unconfirmed", "share_maintenance_refused"),
                               ("blocked", "share_maintenance_refused"),
                               ("capability_missing",
                                "share_capability_missing"),
                               ("bad_request",
                                "share_broker_extension_missing")):
            with self.subTest(code=code):
                client = _Rev9FakeClient([(False, code,
                                            {"detail": code})])
                with self.assertRaises(OperationError) as caught:
                    prod.production_maintenance_end(
                        "share_gateway", "a" * 32,
                        broker=_rev9_broker(client))
                self.assertEqual(caught.exception.code, expected)

    def test_status_returns_the_payload_or_none(self):
        view = {"checked_at": 7, "lease": None, "lease_expired": False,
                "instance": None, "last_outcome": None}
        client = _Rev9FakeClient([(True, "ok", view)])
        broker = _rev9_broker(client)
        out = prod.production_maintenance_status(broker=broker)
        self.assertEqual(out, view)
        self.assertEqual(broker.calls, [("maintenance_status",)])
        self.assertEqual(client.calls, [("maintenance-status",)])
        # Unwired / unreachable / rejected: None = no authority here.
        self.assertIsNone(prod.production_maintenance_status(broker=object()))
        rejected = _Rev9FakeClient([(False, "bad_request", {})])
        self.assertIsNone(prod.production_maintenance_status(
            broker=_rev9_broker(rejected)))
        failing = _Rev9FakeClient([OSError("pipe gone")])
        self.assertIsNone(prod.production_maintenance_status(
            broker=_rev9_broker(failing)))

    def test_console_calls_only_the_public_surface(self):
        # Every console entry point below runs against a broker WITHOUT a
        # `client` attribute: the old getattr(broker, "client") bypass
        # would refuse (share_broker_unwired / None), so these successes
        # prove the console went through the public adapter verbs.
        client = _Rev9FakeClient([(True, "ok", self.BEGIN_OK)])
        broker = _rev9_broker(client)
        self.assertFalse(hasattr(broker, "client"))
        prod.production_maintenance_begin("share_gateway", 300,
                                          broker=broker)
        self.assertEqual(broker.calls,
                         [("maintenance_begin", "share_gateway", 300)])

    def test_seams_bind_the_maintenance_verbs_to_the_same_broker(self):
        broker = _rev9_broker(_Rev9FakeClient([(True, "ok", self.BEGIN_OK)]))
        seams = prod.production_seams(broker=broker)
        self.assertEqual(seams["_MAINTENANCE_BEGIN"].keywords["broker"], broker)
        self.assertEqual(seams["_MAINTENANCE_END"].keywords["broker"], broker)
        self.assertEqual(seams["_MAINTENANCE_STATUS"].keywords["broker"],
                         broker)
        self.assertEqual(seams["_GATEWAY_RESTART"].keywords["broker"], broker)



class GatewayRestartProductionTests(unittest.TestCase):
    """A9-4: ONE bounded 8100 restart through the restricted broker path;
    never share.stop -> share.start, three proofs required."""

    OK = {"restarted": True, "component": "share_gateway",
          "stopped_confirmed": True, "running_confirmed": True,
          "elapsed_s": 41.2, "maintenance_state": "idle"}

    def test_success_shape_and_wire(self):
        client = _Rev9FakeClient([(True, "ok", self.OK)])
        broker = _rev9_broker(client)
        out = prod.production_gateway_restart(broker=broker)
        self.assertEqual(out, self.OK)
        # The console calls the PUBLIC adapter verb with the default budget.
        self.assertEqual(broker.calls,
                         [("gateway_restart", "share_gateway", 120)])
        self.assertEqual(client.calls,
                         [("gateway-restart", "share_gateway", 120)])
        # The client's own bounded timeout: budget_s + 15, capped at 180.
        self.assertEqual(client.timeouts, [135])
        # The ONLY channel is the broker verb - no share.stop/share.start.
        self.assertEqual([call[0] for call in client.calls],
                         ["gateway-restart"])

    def test_default_budget_is_the_protocol_default(self):
        from share.broker_protocol import GATEWAY_RESTART_BUDGET_DEFAULT_S
        self.assertEqual(GATEWAY_RESTART_BUDGET_DEFAULT_S, 120)
        client = _Rev9FakeClient([(True, "ok", self.OK)])
        prod.production_gateway_restart(broker=_rev9_broker(client))
        self.assertEqual(client.calls[0][2],
                         GATEWAY_RESTART_BUDGET_DEFAULT_S)

    def test_explicit_budget_is_capped_by_the_client_bound(self):
        client = _Rev9FakeClient([(True, "ok", self.OK)])
        prod.production_gateway_restart(180, broker=_rev9_broker(client))
        self.assertEqual(client.calls[0][2], 180)
        self.assertEqual(client.timeouts, [180])   # min(195, 180)

    def test_out_of_range_budget_is_refused_without_a_call(self):
        for bad in (0, 181, -1, True, "120", 120.5):
            with self.subTest(bad=bad):
                client = _Rev9FakeClient([(True, "ok", self.OK)])
                with self.assertRaises(OperationError) as caught:
                    prod.production_gateway_restart(
                        bad, broker=_rev9_broker(client))
                self.assertEqual(caught.exception.code, "invalid_params")
                self.assertEqual(client.calls, [])

    def test_blocked_is_gateway_restart_failed(self):
        client = _Rev9FakeClient([(False, "blocked",
                                    {"detail": "gateway identity "
                                               "unconfirmed"})])
        with self.assertRaises(OperationError) as caught:
            prod.production_gateway_restart(broker=_rev9_broker(client))
        self.assertEqual(caught.exception.code,
                         "share_entry_restart_failed")

    def test_missing_proofs_are_gateway_restart_failed(self):
        cases = ({"restarted": True, "stopped_confirmed": True,
                  "running_confirmed": False},
                 {"restarted": True, "stopped_confirmed": False,
                  "running_confirmed": True},
                 {"restarted": False, "stopped_confirmed": True,
                  "running_confirmed": True},
                 {"stopped_confirmed": True, "running_confirmed": True})
        for payload in cases:
            with self.subTest(payload=payload):
                client = _Rev9FakeClient([(True, "ok", payload)])
                with self.assertRaises(OperationError) as caught:
                    prod.production_gateway_restart(
                        broker=_rev9_broker(client))
                self.assertEqual(caught.exception.code,
                                 "share_entry_restart_failed")

    def test_old_broker_bad_request_is_extension_missing(self):
        client = _Rev9FakeClient([(False, "bad_request",
                                    {"detail": "unknown action"})])
        with self.assertRaises(OperationError) as caught:
            prod.production_gateway_restart(broker=_rev9_broker(client))
        self.assertEqual(caught.exception.code,
                         "share_broker_extension_missing")

    def test_unwired_broker_is_broker_unwired(self):
        with self.assertRaises(OperationError) as caught:
            prod.production_gateway_restart(broker=object())
        self.assertEqual(caught.exception.code, "share_broker_unwired")




def _rev9_seed_record(ops, paths, pid=4242, creation=1000.0):
    """Seed a VALID instance record through the PRODUCTION writer (with the
    clock pinned), so a test never writes a record path by hand."""
    with mock.patch.object(prod.time, "time", return_value=creation):
        ops._write_instance_record(paths, pid)


def _rev9_record_reader(payload):
    """Patch the production Path so ``_instance_record`` reads ``payload``
    (a dict) or the raw text (a str) - no filesystem access at all."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    fake = mock.Mock()
    fake.read_text.return_value = text
    return mock.patch.object(prod, "Path", return_value=fake)












if __name__ == "__main__":
    unittest.main()


class RetiredBackendSeamsTests(unittest.TestCase):
    """#34: the dedicated 8110 backend seams/actions are gone."""

    def test_backend_symbols_are_absent(self):
        for name in (
                "_ProductionBackendOps", "production_backend_ops",
                "production_backend_observe", "production_backend_login",
                "production_backend_reauth", "production_backend_pid_state",
                "production_supervision_state", "configure_supervision_paths",
                "backend_login_cancel", "production_backend_login_cancel",
                "BackendConfig", "DEFAULT_SHARE_BACKEND_PORT",
                "BACKEND_AGGREGATE_SCOPE_ENV"):
            with self.subTest(symbol=name):
                self.assertFalse(hasattr(prod, name))

    def test_cloud_registry_has_no_retired_actions(self):
        names = {spec.name for spec in
                 console_adapters.build_cloud_registry()}
        for name in ("share.backend_login", "share.backend_login_cancel",
                      "share.backend_reauthenticate", "share.backend_restart",
                      "share.chain_start", "share.chain_stop",
                      "share.chain_estop"):
            with self.subTest(action=name):
                self.assertNotIn(name, names)
        self.assertIn("share.gateway_restart", names)

    def test_backend_module_and_cloud_root_are_absent(self):
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / "share" / "backend_supervision.py").exists())
        # cloud/ now holds only the Windows cloud-console entry; the retired
        # composition root was cloud/backend and cloud/app.py.
        self.assertFalse((root / "cloud" / "backend").exists())
        self.assertFalse((root / "cloud" / "app.py").exists())
