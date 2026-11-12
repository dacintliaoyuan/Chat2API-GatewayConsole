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
  observed here; a pointer at the 8110 ledger row fact); the read-only gateway
  probe (netstat port table + CIM start time) is stubbed in every test;
* REAL DEFAULT ASSEMBLY: wire_share_production_seams() runs the REAL
  usagesrv loader chain with ONLY the farthest OS boundaries replaced (fake
  pipe transport, fake control-key read, tmp deployment config) and zero
  real calls (the sentinel would fire).
"""

import ast
import builtins
import contextlib
import io
import json
import os
import re
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

import console_share_actions as orch  # noqa: E402  (same directory on path)
import console_share_production as prod  # noqa: E402
import share_broker_client  # noqa: E402
import share_broker_server  # noqa: E402
import usagesrv  # noqa: E402  (real loader chain for the assembly test)
from console_operations import OperationError  # noqa: E402
from share_broker_ops import TailscaleServiceOps  # noqa: E402
from share_management import BrokerClientAdapter, BrokerError  # noqa: E402

_EXE = "fake-tailscale.exe"
_OFF_ARGS = [_EXE, "funnel", "--https=443", "off"]
_STATUS_ARGS = [_EXE, "serve", "status"]
_TSCON_ARGS = [_EXE, "status", "--json"]
_ON_ARGS = [_EXE, "funnel", "--bg", "--https=443", "http://127.0.0.1:8100"]
_MAPPING_OUT = "https://dacint.tailae8db5.ts.net:443 (funnel)"
_SC_QUERY_ARGS = ["sc.exe", "query", "Tailscale"]
# status --json 正例（Running + 自节点地址）：接线测试的 connectivity 喂入。
_TS_STATUS_JSON_RUNNING = json.dumps(
    {"BackendState": "Running",
     "Self": {"ID": "node-self", "HostName": "desktop",
              "TailscaleIPs": ["100.64.12.24"]}})

_SEAM_ATTRS = ("_BROKER_OPS", "_TS_SERVICE_CONTROL", "_TS_SERVICE_STATUS", "_CONNECTIVITY_OK",
                "_CONNECTIVITY_STATE",
                "_FUNNEL_CONTROL", "_FUNNEL_OBSERVED", "_DRAIN", "_DESIRED_STOP_READER",
                "_BACKEND_OPS", "_BACKEND_OBSERVE", "_BACKEND_LOGIN", "_KEYS_CONFIRMED",
                "_KEYS_DIAGNOSIS", "_MAINTENANCE_BEGIN", "_MAINTENANCE_END", "_MAINTENANCE_STATUS",
                "_GATEWAY_RESTART", "_BACKEND_REAUTH", "_INTENT_WRITE", "_SUPERVISION_STATE")

# S5（契约 §5.4）：wire_share_production_seams() 还会设置公网巡检的两个
# 取消缝；它们不在 production_seams() 的 19 键字典里，但同样属于
# console_share_actions 的模块级可变状态，测试必须恢复。
_CANCEL_SEAM_ATTRS = ("_PUBLIC_CANCEL_REQUEST", "_PUBLIC_CANCEL_CLEAR")
_RESTORE_ATTRS = _SEAM_ATTRS + _CANCEL_SEAM_ATTRS

_SOURCE_FILE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "console_share_production.py")


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
# 归因密钥两端接线的只读现场：8100 的部署脚本（只解析、不执行、不部署）与
# 两端真正读密钥的模块（只做 AST/文本核对，不 import，零副作用）。
_GATEWAY_DIR = Path(__file__).resolve().parents[1]
_DEPLOY_SCRIPT = _GATEWAY_DIR / "um-share-deploy" / "deploy-elevated.ps1"
_SHARE_GATEWAY_SOURCE = _GATEWAY_DIR / "chat2api" / "share_entry.py"
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


def _ps_literal(text, name):
    """PowerShell ``$Name = '<字面量>'`` 的值（只解析文本，不执行脚本）。"""
    match = re.search(r"^\$%s = '([^']+)'" % name, text, re.M)
    if match is None:
        raise AssertionError("未见 PowerShell 字面量赋值 $%s = '...'" % name)
    return match.group(1)


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


class _FakeHost:
    """WindowsHostOps stand-in: recorded calls, programmable returns."""

    def __init__(self, running=True, closed=False, start=True, stop=True):
        self.calls = []
        self._running = running
        self._closed = closed
        self._start = start
        self._stop = stop

    def start_host(self):
        self.calls.append("start_host")
        return self._start

    def stop_host(self):
        self.calls.append("stop_host")
        return self._stop

    def verify_running(self):
        self.calls.append("verify_running")
        return self._running

    def verify_closed(self):
        self.calls.append("verify_closed")
        return self._closed


def _no_sleep(seconds):
    return None


# 8100 运行实例只读探测的替身：真实实现会跑 netstat + Get-CimInstance，
# 测试一律替身化（本套件不得依赖真实进程）。字段与真实探测同形。
def _gateway_probe(pid=20740, started_at=None, reason="ok"):
    return {"pid": pid, "started_at": started_at, "reason": reason}


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


class FunnelOffTests(unittest.TestCase):
    """Test 2: funnel off mirrors the watchdog Close-Public three steps and
    reports every failure honestly (rc / empty verify / still-present)."""

    def test_off_three_steps_success_shape(self):
        sleeps = []
        runner = _FakeRunner([(0, "", ""), (0, "no mappings here", "")])
        ack = prod.production_funnel_control(
            "off", exe_path=_EXE, runner=runner,
            sleeper=lambda s: sleeps.append(s))
        self.assertEqual(runner.calls,
                         [(_OFF_ARGS, 10.0), (_STATUS_ARGS, 10.0)])
        self.assertEqual(sleeps, [prod.FUNNEL_OFF_SETTLE_S])
        self.assertEqual(sorted(ack), ["detail", "ok"])
        self.assertIs(ack["ok"], True)
        self.assertTrue(ack)  # truthy ack: orchestration reads success
        self.assertIn("映射已移除", ack["detail"])

    def test_off_rc_failure_is_honest_and_stops(self):
        runner = _FakeRunner([(3, "", "")])
        ack = prod.production_funnel_control("off", exe_path=_EXE,
                                             runner=runner,
                                             sleeper=_no_sleep)
        self.assertEqual([c[0] for c in runner.calls], [_OFF_ARGS])
        self.assertIs(ack["ok"], False)
        self.assertFalse(ack)  # falsy ack: never a fake success
        self.assertIn("rc=3", ack["detail"])

    def test_off_serve_status_failure_is_honest(self):
        runner = _FakeRunner([(0, "", ""), (5, "", "query failed")])
        ack = prod.production_funnel_control("off", exe_path=_EXE,
                                             runner=runner, sleeper=_no_sleep)
        self.assertEqual([c[0] for c in runner.calls],
                         [_OFF_ARGS, _STATUS_ARGS])
        self.assertIs(ack["ok"], False)
        self.assertFalse(ack)
        self.assertIn("rc=5", ack["detail"])

    def test_off_empty_verify_cannot_confirm(self):
        runner = _FakeRunner([(0, "", ""), (0, "", "")])
        ack = prod.production_funnel_control("off", exe_path=_EXE,
                                             runner=runner, sleeper=_no_sleep)
        self.assertIs(ack["ok"], False)
        self.assertFalse(ack)
        self.assertIn("无法确认", ack["detail"])

    def test_off_still_present_is_honest_failure(self):
        runner = _FakeRunner([(0, "", ""), (0, _MAPPING_OUT, "")])
        ack = prod.production_funnel_control("off", exe_path=_EXE,
                                             runner=runner, sleeper=_no_sleep)
        self.assertIs(ack["ok"], False)
        self.assertFalse(ack)
        self.assertIn("映射", ack["detail"])
        self.assertIn("https://", ack["detail"])

    def test_runner_crash_is_honest_failure(self):
        runner = _FakeRunner([OSError("boom")])
        ack = prod.production_funnel_control("off", exe_path=_EXE,
                                             runner=runner, sleeper=_no_sleep)
        self.assertIs(ack["ok"], False)
        self.assertFalse(ack)

    def test_exe_absent_raises_unwired(self):
        runner = _FakeRunner([FileNotFoundError(2, "no such file")])
        with self.assertRaises(OperationError) as caught:
            prod.production_funnel_control("off", exe_path=_EXE, runner=runner,
                                           sleeper=_no_sleep)
        self.assertEqual(caught.exception.code, "share_privilege_unwired")

    def test_unknown_action_refused_without_running(self):
        runner = _FakeRunner([(0, "", "")])
        with self.assertRaises(OperationError) as caught:
            prod.production_funnel_control("restart", exe_path=_EXE,
                                           runner=runner, sleeper=_no_sleep)
        self.assertEqual(caught.exception.code, "invalid_params")
        self.assertEqual(runner.calls, [])


class FunnelOnTests(unittest.TestCase):
    """Test 3: funnel on uses the SHARING.md open command shape and
    re-checks the mapping IS present; failures stay honest."""

    def test_on_command_shape_and_presence_verify(self):
        sleeps = []
        runner = _FakeRunner([(0, "", ""), (0, _MAPPING_OUT, "")])
        ack = prod.production_funnel_control(
            "on", exe_path=_EXE, runner=runner,
            sleeper=lambda s: sleeps.append(s))
        self.assertEqual(runner.calls,
                         [(_ON_ARGS, 10.0), (_STATUS_ARGS, 10.0)])
        self.assertEqual(sleeps, [])  # the watchdog settle belongs to OFF only
        self.assertIs(ack["ok"], True)
        self.assertTrue(ack)

    def test_on_open_rc_failure_is_honest(self):
        runner = _FakeRunner([(1, "", "")])
        ack = prod.production_funnel_control("on", exe_path=_EXE,
                                             runner=runner, sleeper=_no_sleep)
        self.assertEqual([c[0] for c in runner.calls], [_ON_ARGS])
        self.assertIs(ack["ok"], False)
        self.assertFalse(ack)
        self.assertIn("rc=1", ack["detail"])

    def test_on_verify_absent_mapping_is_honest(self):
        runner = _FakeRunner([(0, "", ""), (0, "nothing served", "")])
        ack = prod.production_funnel_control("on", exe_path=_EXE,
                                             runner=runner, sleeper=_no_sleep)
        self.assertIs(ack["ok"], False)
        self.assertFalse(ack)

    def test_on_empty_status_cannot_confirm(self):
        runner = _FakeRunner([(0, "", ""), (0, "", "")])
        ack = prod.production_funnel_control("on", exe_path=_EXE,
                                             runner=runner, sleeper=_no_sleep)
        self.assertIs(ack["ok"], False)
        self.assertFalse(ack)

    def test_exe_absent_raises_unwired(self):
        runner = _FakeRunner([FileNotFoundError(2, "missing")])
        with self.assertRaises(OperationError) as caught:
            prod.production_funnel_control("on", exe_path=_EXE, runner=runner,
                                           sleeper=_no_sleep)
        self.assertEqual(caught.exception.code, "share_privilege_unwired")


class ConnectivityTests(unittest.TestCase):
    """Test 4 (structured): REAL Tailscale connectivity from
    `tailscale status --json` — BackendState + Self.TailscaleIPs decide;
    peer addresses and stale offline self IPs can never fabricate a True;
    bad JSON / missing keys / off-contract shapes -> share_status_unknown
    (never a fabricated offline fact), exe absent -> share_privilege_unwired.
    Fixtures mirror the real CLI shape (BackendState / Self.TailscaleIPs
    keys as calibrated on the authorized environment)."""

    RUNNING_IPS = ["100.64.12.24",
                   "fd7a:115c:a1e0:ab12:4843:cd96:626b:2c18"]

    def test_default_runner_decodes_utf8_json_independent_of_windows_locale(self):
        payload = json.dumps({"label": "星✨"},
                             ensure_ascii=False).encode("utf-8")
        script = "import sys; sys.stdout.buffer.write(%r)" % payload
        proc = prod._default_runner([sys.executable, "-c", script], 5)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["label"], "星✨")

    @staticmethod
    def _status_json(state, self_ips, peers=None):
        payload = {"Version": "1.82.0", "BackendState": state,
                   "Self": {"ID": "node-self", "HostName": "desktop",
                            "TailscaleIPs": self_ips},
                   "Health": []}
        if peers is not None:
            payload["Peer"] = peers
        return json.dumps(payload)

    def test_running_with_self_ips_is_true_even_without_mapping(self):
        # 确切在线正例（Running + 非空 Self.TailscaleIPs）-> True；连接状态
        # 来自 status JSON，与 serve status 的 HTTPS 映射无关（规划端明示）。
        runner = _FakeRunner(
            [(0, self._status_json("Running", self.RUNNING_IPS), "")])
        self.assertIs(prod.production_connectivity_ok(exe_path=_EXE,
                                                      runner=runner), True)
        self.assertEqual([c[0] for c in runner.calls], [_TSCON_ARGS])

    def test_argument_shape_is_status_json_not_serve_status(self):
        runner = _FakeRunner(
            [(0, self._status_json("Running", self.RUNNING_IPS), "")])
        prod.production_connectivity_ok(exe_path=_EXE, runner=runner)
        self.assertNotIn(_STATUS_ARGS, [c[0] for c in runner.calls])
        self.assertEqual([c[0] for c in runner.calls], [_TSCON_ARGS])

    def test_peer_only_ip_is_false(self):
        # 仅对端在线（Self.TailscaleIPs 空 + Peer 带地址）-> False：对端
        # 地址永远不能冒充自节点在线（旧全文 IP 正则会误判，已删除）。
        runner = _FakeRunner([(0, self._status_json(
            "Running", [],
            peers={"node-b": {"HostName": "nas",
                              "TailscaleIPs": ["100.99.9.9"],
                              "Online": True}}), "")])
        self.assertIs(prod.production_connectivity_ok(exe_path=_EXE,
                                                      runner=runner), False)

    def test_stopped_self_with_stale_ip_is_false(self):
        # 离线自节点带旧地址（BackendState Stopped + Self 有旧 IP）-> False；
        # 正是旧 _SELF_NODE_RE 会误判 True 的回归保护。
        runner = _FakeRunner([(0, self._status_json(
            "Stopped", ["100.64.12.24"]), "")])
        self.assertIs(prod.production_connectivity_ok(exe_path=_EXE,
                                                      runner=runner), False)

    def test_needslogin_is_false_even_with_peer_addresses(self):
        # 未登录（NeedsLogin）是工具自身给出的明确事实 -> False，
        # 即使对端列表里有在线地址。
        runner = _FakeRunner([(0, self._status_json(
            "NeedsLogin", [],
            peers={"node-b": {"TailscaleIPs": ["100.99.9.9"],
                              "Online": True}}), "")])
        self.assertIs(prod.production_connectivity_ok(exe_path=_EXE,
                                                      runner=runner), False)

    def test_running_with_none_self_ips_is_false(self):
        # Running 但 Self.TailscaleIPs=None（本机实测的空地址形状）-> False。
        runner = _FakeRunner([(0, self._status_json("Running", None), "")])
        self.assertIs(prod.production_connectivity_ok(exe_path=_EXE,
                                                      runner=runner), False)

    def test_unknown_state_with_empty_ips_is_unknown_not_offline(self):
        # 规划端 L6-3 追加点 1：未知 BackendState + 空 IP -> share_status_
        # unknown（不是离线）——未识别状态永远不能被折叠成确定的离线事实。
        runner = _FakeRunner([(0, self._status_json("Watermelon", []), "")])
        with self.assertRaises(OperationError) as caught:
            prod.production_connectivity_ok(exe_path=_EXE, runner=runner)
        self.assertEqual(caught.exception.code, "share_status_unknown")
        self.assertIn("Watermelon", caught.exception.message)

    def test_error_exit_with_running_claim_is_unknown_not_true(self):
        # 规划端 L6-3 追加点 2：rc=1 但 stdout 含 Running+IPs -> 未知（错误
        # 退出下的 Running 主张是矛盾证据，不得作为在线正证）。
        runner = _FakeRunner([(1, self._status_json("Running",
                                                    self.RUNNING_IPS),
                               "some CLI warning")])
        with self.assertRaises(OperationError) as caught:
            prod.production_connectivity_ok(exe_path=_EXE, runner=runner)
        self.assertEqual(caught.exception.code, "share_status_unknown")
        self.assertIn("rc=1", caught.exception.message)

    def test_rc1_with_explicit_needslogin_is_still_false(self):
        # rc≠0 但状态是工具自身的明确负例（NeedsLogin）-> False：错误退出
        # 只信任显式否定事实，不冒充 unknown。
        runner = _FakeRunner([(1, self._status_json("NeedsLogin", []), "")])
        self.assertIs(prod.production_connectivity_ok(exe_path=_EXE,
                                                      runner=runner), False)

    def test_bad_json_raises_share_status_unknown(self):
        runner = _FakeRunner([(0, "<html>backend error page</html>", "")])
        with self.assertRaises(OperationError) as caught:
            prod.production_connectivity_ok(exe_path=_EXE, runner=runner)
        self.assertEqual(caught.exception.code, "share_status_unknown")
        self.assertIn("不冒充", caught.exception.message)

    def test_query_failure_raises_share_status_unknown(self):
        for item in ((7, "", "backend down"), OSError("boom"),
                     subprocess.TimeoutExpired("tailscale", 10)):
            runner = _FakeRunner([item])
            with self.assertRaises(OperationError) as caught:
                prod.production_connectivity_ok(exe_path=_EXE, runner=runner)
            self.assertEqual(caught.exception.code, "share_status_unknown")
            self.assertIn("不冒充", caught.exception.message)

    def test_empty_successful_status_is_unknown_not_offline(self):
        runner = _FakeRunner([(0, "", "")])
        with self.assertRaises(OperationError) as caught:
            prod.production_connectivity_ok(exe_path=_EXE, runner=runner)
        self.assertEqual(caught.exception.code, "share_status_unknown")

    def test_missing_or_off_contract_keys_are_unknown(self):
        # 键缺失/类型异常/契约外状态一律 unknown，不折算成确定离线。
        cases = [
            json.dumps({"Self": {"TailscaleIPs": ["100.64.1.1"]}}),
            json.dumps({"BackendState": "Running"}),
            json.dumps({"BackendState": "Running", "Self": "weird"}),
            json.dumps({"BackendState": "Running",
                        "Self": {"TailscaleIPs": "100.64.1.1"}}),
            json.dumps({"BackendState": "Watermelon",
                        "Self": {"TailscaleIPs": ["100.64.1.1"]}}),
            "[1, 2, 3]",
        ]
        for body in cases:
            with self.subTest(body=body):
                runner = _FakeRunner([(0, body, "")])
                with self.assertRaises(OperationError) as caught:
                    prod.production_connectivity_ok(exe_path=_EXE,
                                                    runner=runner)
                self.assertEqual(caught.exception.code,
                                 "share_status_unknown")
                self.assertIn("不冒充", caught.exception.message)

    def test_exe_absent_raises_unwired(self):
        runner = _FakeRunner([FileNotFoundError(2, "missing")])
        with self.assertRaises(OperationError) as caught:
            prod.production_connectivity_ok(exe_path=_EXE, runner=runner)
        self.assertEqual(caught.exception.code, "share_privilege_unwired")


class ConnectivityStateTests(unittest.TestCase):
    """R13-T: the read-only BackendState channel for the connectivity wait
    (``production_connectivity_state``) - the SAME ``status --json`` surface
    as the verdict seam, keyed on the CLI's own structured BackendState so
    the orchestration can stop EXPLICITLY on a 需人工登录 reading instead of
    burning the whole wait budget on a generic "not ready" poll.

    Trust rule mirrors the verdict contract: the state is returned only when
    rc==0 OR the state is an explicit offline fact (_TAILSCALE_OFFLINE_
    STATES - NeedsLogin stays trusted under an error exit); a Running claim
    under an error exit is contradictory evidence -> None; ANY unreadable
    reading (bad JSON, missing key, failure, timeout, absent exe) -> None
    (the channel never fabricates a state - the ok seam stays the verdict
    authority)."""

    @staticmethod
    def _status_json(state):
        return json.dumps({"Version": "1.82.0", "BackendState": state,
                           "Self": {"ID": "node-self", "HostName": "desktop",
                                    "TailscaleIPs": []}})

    def test_states_pass_through_with_the_status_json_argument_shape(self):
        for state in ("Running", "NeedsLogin", "NoState", "Stopped",
                      "RequiresAuth"):
            runner = _FakeRunner([(0, self._status_json(state), "")])
            self.assertEqual(
                prod.production_connectivity_state(exe_path=_EXE,
                                                   runner=runner), state)
            self.assertEqual([c[0] for c in runner.calls], [_TSCON_ARGS])

    def test_needs_login_stays_trusted_under_error_exit(self):
        # rc≠0 但状态是工具自身的明确否定事实 -> NeedsLogin 照常透传
        #（镜像 production_connectivity_ok 的裁定次序）。
        runner = _FakeRunner([(1, self._status_json("NeedsLogin"), "warn")])
        self.assertEqual(prod.production_connectivity_state(exe_path=_EXE,
                                                            runner=runner),
                         "NeedsLogin")

    def test_running_claim_under_error_exit_is_none(self):
        # rc≠0 下的 Running 主张是矛盾证据 -> None（永不冒充可信状态）。
        runner = _FakeRunner([(1, self._status_json("Running"), "warn")])
        self.assertIsNone(prod.production_connectivity_state(exe_path=_EXE,
                                                             runner=runner))

    def test_unknown_state_is_none_never_a_confirmed_fact(self):
        # 契约外状态 -> None：未识别状态不能经这条通道成为任何确定事实。
        runner = _FakeRunner([(0, self._status_json("Watermelon"), "")])
        self.assertIsNone(prod.production_connectivity_state(exe_path=_EXE,
                                                             runner=runner))

    def test_unreadable_readings_are_none(self):
        # 坏 JSON / 缺键 / 非 JSON 对象 / 调用失败 / 超时 / exe 缺席 → None
        #（通道不可读 ≠ 任何确定状态；裁定仍归 ok 缝）。
        cases = [
            (0, "<html>backend error page</html>", ""),
            (0, json.dumps({"Self": {"TailscaleIPs": []}}), ""),
            (0, "[1, 2, 3]", ""),
            (7, "", "backend down"),
            OSError("boom"),
            subprocess.TimeoutExpired("tailscale", 10),
            FileNotFoundError(2, "missing"),
        ]
        for item in cases:
            with self.subTest(item=item):
                runner = _FakeRunner([item])
                self.assertIsNone(
                    prod.production_connectivity_state(exe_path=_EXE,
                                                       runner=runner))


class PublicShareUrlTests(unittest.TestCase):
    """``production_share_public_url``: the public Funnel host from the SAME
    ``tailscale status --json`` surface. Only a Running backend with a
    non-empty Self.DNSName yields ``https://<name>``; every unreadable or
    off-contract reading is None (the public check then reports
    public_url_unavailable instead of probing a guessed host)."""

    @staticmethod
    def _status_json(state, dns_name):
        self_node = {"ID": "node-self", "HostName": "desktop",
                     "TailscaleIPs": ["100.64.12.24"]}
        if dns_name is not None:
            self_node["DNSName"] = dns_name
        return json.dumps({"Version": "1.82.0", "BackendState": state,
                           "Self": self_node})

    def test_running_with_dns_name_yields_the_public_url(self):
        runner = _FakeRunner([(0, self._status_json(
            "Running", "share.example.ts.net."), "")])
        self.assertEqual(
            prod.production_share_public_url(exe_path=_EXE, runner=runner),
            "https://share.example.ts.net")
        self.assertEqual([c[0] for c in runner.calls], [_TSCON_ARGS])

    def test_non_running_backend_is_none(self):
        runner = _FakeRunner([(0, self._status_json(
            "NeedsLogin", "share.example.ts.net."), "")])
        self.assertIsNone(prod.production_share_public_url(
            exe_path=_EXE, runner=runner))

    def test_missing_or_empty_dns_name_is_none(self):
        for body in (
                self._status_json("Running", None),
                self._status_json("Running", ""),
                self._status_json("Running", "."),
                json.dumps({"BackendState": "Running", "Self": "weird"}),
                json.dumps({"BackendState": "Running"}),
                "[1, 2, 3]"):
            with self.subTest(body=body):
                runner = _FakeRunner([(0, body, "")])
                self.assertIsNone(prod.production_share_public_url(
                    exe_path=_EXE, runner=runner))

    def test_unreadable_readings_are_none(self):
        cases = [
            (0, "<html>backend error page</html>", ""),
            (7, "", "backend down"),
            OSError("boom"),
            subprocess.TimeoutExpired("tailscale", 10),
            FileNotFoundError(2, "missing"),
        ]
        for item in cases:
            with self.subTest(item=item):
                runner = _FakeRunner([item])
                self.assertIsNone(prod.production_share_public_url(
                    exe_path=_EXE, runner=runner))

    def test_public_supervision_wrapper_resolves_the_production_url(self):
        # The wrapper path (``_public_url(None)``) must reach the production
        # seam, not depend on an injected url_provider.
        import share_public_supervision as supervision

        runner = _FakeRunner([(0, self._status_json(
            "Running", "share.example.ts.net."), "")])
        with mock.patch.object(prod, "_default_runner", runner):
            self.assertEqual(supervision._public_url(None),
                             "https://share.example.ts.net")


class _TsPublicBroker:
    """BrokerClientAdapter stand-in exposing ONLY the public TS control verb
    (``ts_service_control``) and deliberately NO ``client`` attribute: the
    removed ``getattr(broker, "client")`` bypass would fall back to the unwired
    shape, so every passing success case here proves the console calls the
    adapter's PUBLIC SURFACE, never the inner client."""

    def __init__(self, client):
        self._inner = client
        self.calls = []

    def ts_service_control(self, action):
        self.calls.append(("ts_service_control", action))
        return self._inner.call("ts-service-control", {"action": action})


def _ts_broker(client):
    return _TsPublicBroker(client)


class TsServiceControlTests(unittest.TestCase):
    """Test 5 (revised): the TS service seam runs THROUGH the real broker
    adapter's public ``ts_service_control`` verb over the restricted
    ts-service-control verb; the three broker failure shapes are DISTINCT honest
    errors with deployment pointers, and the seam body contains zero direct OS
    surface (AST proof)."""

    def test_start_confirmed_returns_truthy_ack(self):
        client = _FakeBrokerClient([(True, "ok", {"ts_service_started": True})])
        broker = _ts_broker(client)
        ack = prod.production_ts_service_control("start", broker=broker)
        # 假 broker 没有 `client` 属性：旧的 getattr(broker, "client") 绕行
        # 若回归会退回 unwired 形态，故此成功证明走的是适配器公开面。
        self.assertFalse(hasattr(broker, "client"))
        self.assertEqual(broker.calls, [("ts_service_control", "start")])
        self.assertEqual(client.calls,
                         [("ts-service-control", {"action": "start"})])
        self.assertEqual(sorted(ack), ["detail", "ok"])
        self.assertIs(ack["ok"], True)
        self.assertTrue(ack)

    def test_stop_confirmed_returns_truthy_ack(self):
        client = _FakeBrokerClient([(True, "ok",
                                     {"ts_service_stopped": True})])
        broker = _ts_broker(client)
        ack = prod.production_ts_service_control("stop", broker=broker)
        self.assertFalse(hasattr(broker, "client"))
        self.assertEqual(broker.calls, [("ts_service_control", "stop")])
        self.assertEqual(client.calls,
                         [("ts-service-control", {"action": "stop"})])
        self.assertTrue(ack)
        self.assertIn("复核确认", ack["detail"])

    def test_broker_unwired_loader_absent(self):
        # loader 返回 None（部署配置缺席/未启用）→ broker 未部署形态。
        with _broker_loader(lambda: None):
            with self.assertRaises(OperationError) as caught:
                prod.production_ts_service_control("start")
        exc = caught.exception
        self.assertEqual(exc.code, "share_broker_unwired")
        self.assertIn("share-management.json", exc.message)
        self.assertIn("install-share-broker.ps1", exc.message)

    def test_pipe_not_installed_is_broker_unwired_shape(self):
        client = _FakeBrokerClient([(False, "not_installed",
                                     {"detail": "pipe missing"})])
        with self.assertRaises(OperationError) as caught:
            prod.production_ts_service_control(
                "start", broker=_ts_broker(client))
        exc = caught.exception
        self.assertEqual(exc.code, "share_broker_unwired")
        self.assertIn("install-share-broker.ps1", exc.message)

    def test_pending_confirmation_shape_is_distinct_code(self):
        # The broker EXECUTED the command but the immediate re-query caught
        # the service in a transition state: a dedicated pending code the
        # orchestration poll consumes - NOT a plain share_layer_failed.
        client = _FakeBrokerClient([(False, "unconfirmed",
                                     {"detail": "ts service stop did not "
                                                "re-confirm the stopped "
                                                "state"})])
        with self.assertRaises(OperationError) as caught:
            prod.production_ts_service_control(
                "stop", broker=_ts_broker(client))
        exc = caught.exception
        self.assertEqual(exc.code, "share_ts_pending_confirmation")
        self.assertIn("过渡态", exc.message)

    def test_access_denied_is_a_distinct_permission_failure(self):
        # R13-T: the broker executed nothing (sc.exe rc 5 access denied) and
        # answers the DISTINCT access_denied code - the console maps it to
        # its own frozen 权限不足 code, NOT the pending-confirmation shape
        # (the command never ran, so a bounded confirmation poll could only
        # burn its budget on a misleading "启动已执行" story) and NOT a fake
        # success. The deployment pointer stays honest: this is an execution
        # refusal, not a missing broker.
        client = _FakeBrokerClient([(False, "access_denied",
                                     {"detail": "ts service start denied "
                                                "by the OS (rc 5)"})])
        with self.assertRaises(OperationError) as caught:
            prod.production_ts_service_control(
                "start", broker=_ts_broker(client))
        exc = caught.exception
        self.assertEqual(exc.code, "share_ts_access_denied")
        self.assertIn("权限不足", exc.message)
        self.assertIn("rc 5", exc.message)
        self.assertIn("不冒充", exc.message)
        self.assertNotIn("share_ts_pending_confirmation", exc.code)

    def test_pending_confirmation_shape_requires_detail_match(self):
        # Other unconfirmed rejections stay share_layer_failed.
        client = _FakeBrokerClient([(False, "unconfirmed",
                                     {"detail": "some other refusal"})])
        with self.assertRaises(OperationError) as caught:
            prod.production_ts_service_control(
                "stop", broker=_ts_broker(client))
        self.assertEqual(caught.exception.code, "share_layer_failed")

    def test_unknown_verb_rejection_is_extension_missing(self):
        # broker 拒绝未知动词 = 扩展未部署（broker 任务代码早于扩展）。
        client = _FakeBrokerClient([(False, "bad_request",
                                     {"detail": "unknown action"})])
        with self.assertRaises(OperationError) as caught:
            prod.production_ts_service_control(
                "start", broker=_ts_broker(client))
        exc = caught.exception
        self.assertEqual(exc.code, "share_broker_extension_missing")
        self.assertIn("ts-service-control", exc.message)
        self.assertIn("扩展未部署", exc.message)
        self.assertIn("重启", exc.message)

    def test_other_rejection_is_execution_failed(self):
        client = _FakeBrokerClient([(False, "unconfirmed",
                                     {"detail": "did not verify"})])
        with self.assertRaises(OperationError) as caught:
            prod.production_ts_service_control(
                "stop", broker=_ts_broker(client))
        exc = caught.exception
        self.assertEqual(exc.code, "share_layer_failed")
        self.assertIn("unconfirmed", exc.message)
        self.assertIn("不冒充", exc.message)

    def test_ok_without_explicit_flag_is_execution_failure(self):
        client = _FakeBrokerClient([(True, "ok", {"detail": "???"})])
        with self.assertRaises(OperationError) as caught:
            prod.production_ts_service_control(
                "start", broker=_ts_broker(client))
        self.assertEqual(caught.exception.code, "share_layer_failed")

    def test_transport_failure_is_broker_unwired(self):
        client = _FakeBrokerClient([OSError("pipe gone")])
        with self.assertRaises(OperationError) as caught:
            prod.production_ts_service_control(
                "stop", broker=_ts_broker(client))
        exc = caught.exception
        self.assertEqual(exc.code, "share_broker_unwired")
        self.assertIn("install-share-broker.ps1", exc.message)

    def test_adapter_without_the_control_verb_is_broker_unwired(self):
        # 适配器没有公开控制方法（形状不符）→ 仍是 broker 未部署形态，
        # 部署指针保留，不冒充可用。
        with self.assertRaises(OperationError) as caught:
            prod.production_ts_service_control("start", broker=object())
        exc = caught.exception
        self.assertEqual(exc.code, "share_broker_unwired")
        self.assertIn("ts_service_control", exc.message)
        self.assertIn("install-share-broker.ps1", exc.message)

    def test_invalid_action_refused_without_any_call(self):
        client = _FakeBrokerClient([(True, "ok", {"ts_service_started": True})])
        broker = _ts_broker(client)
        with self.assertRaises(OperationError) as caught:
            prod.production_ts_service_control("restart", broker=broker)
        self.assertEqual(caught.exception.code, "invalid_params")
        self.assertEqual(broker.calls, [])
        self.assertEqual(client.calls, [])

    def test_ast_seam_body_has_no_direct_os_surface(self):
        tree = ast.parse(Path(_SOURCE_FILE).read_text("utf-8"))
        fn = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef)
                  and node.name == "production_ts_service_control")
        for node in ast.walk(fn):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, ("subprocess", "os", "ctypes",
                                           "win32", "sc", "net"))
            if isinstance(node, ast.Call):
                # 只有经注入 broker 适配器公开控制方法的调用；没有任何本地
                # 命令执行面。
                self.assertFalse(
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("system", "popen", "run"))

    def test_module_privacy_ast_no_file_env_or_spawn_surface(self):
        # Revision 8 scoping: the BROKER/TS/FUNNEL/CONNECTIVITY/KEYS seams
        # still carry ZERO direct OS surface (no os module, no environ/
        # getenv, no open, no subprocess, no urllib) — the dedicated-backend
        # gate (_ProductionBackendOps/_backend_auth_observation) legitimately
        # owns its own file/env/network boundary instead, and every one of those
        # boundaries is isolation-tested in DedicatedBackendOpsTests.
        privacy_fn_names = (
            "production_ts_service_control", "production_ts_service_status",
            "production_funnel_control", "production_funnel_observed",
            "production_connectivity_ok", "production_connectivity_state",
            "production_share_public_url",
            "production_desired_stop_record",
            "production_desired_stop_reader", "_drain_with",
            "production_keys_confirmed", "production_keys_diagnosis",
            "production_supervision_state",
            "production_seams")
        tree = ast.parse(Path(_SOURCE_FILE).read_text("utf-8"))
        functions = {node.name: node for node in ast.walk(tree)
                     if isinstance(node, ast.FunctionDef)
                     and node.name in privacy_fn_names}
        for name in privacy_fn_names:
            self.assertIn(name, functions, "function %s missing" % name)
            for node in ast.walk(functions[name]):
                if isinstance(node, ast.Name):
                    # subprocess is allowed ONLY as an exception-class
                    # reference (SubprocessError); os/ctypes/urllib not at all.
                    self.assertNotIn(node.id, ("os", "ctypes", "urllib"),
                                     "%s references %s" % (name, node.id))
                if isinstance(node, ast.Call) \
                        and isinstance(node.func, ast.Attribute) \
                        and isinstance(node.func.value, ast.Name) \
                        and node.func.value.id == "subprocess":
                    self.fail("%s spawns via subprocess.%s at line %d"
                              % (name, node.func.attr, node.lineno))
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr,
                                     ("environ", "getenv", "system",
                                      "urlopen", "Popen", "run"),
                                     "%s touches %s" % (name, node.attr))
                if isinstance(node, ast.Call) and isinstance(node.func,
                                                             ast.Name):
                    self.assertNotEqual(node.func.id, "open",
                                        "%s calls open()" % name)

    def test_fail_closed_without_broker_never_spawns_even_under_guard(self):
        # 部署配置缺席（loader 返回 None）时缝如实拒绝：哨兵在场也证明
        # 没有任何 spawn/管道尝试发生（日志增量断言，不受其他用例影响）。
        before = list(_spawn_log)
        with _broker_loader(lambda: None):
            with self.assertRaises(OperationError) as caught:
                prod.production_ts_service_control("stop")
        self.assertEqual(caught.exception.code, "share_broker_unwired")
        self.assertEqual(_spawn_log, before)


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
    """Test 6 (expanded): production_seams matches the orchestration seam
    interface key-by-key and delivers ALL NINETEEN seams (Revision 8 adds
    the dedicated-backend gate/observation and the key precondition; the
    2026-09-22 diagnosis split adds the structured WHY behind the same
    precondition; Rev9 adds the six A9 seams, wired accessors unchanged)."""

    def test_seam_keys_match_orchestration_interface(self):
        # Keys ARE the orchestration's injection seam globals (exact names),
        # so wiring is a direct setattr per entry.
        for name in _SEAM_ATTRS:
            self.assertTrue(hasattr(orch, name),
                            "orchestration seam %s missing" % name)
        seams = prod.production_seams(broker=_adapter(),
                                      exe_path=_EXE,
                                      runner=_FakeRunner([(0, "", "")]),
                                      sleeper=_no_sleep)
        self.assertEqual(set(seams), set(_SEAM_ATTRS))
        for name in _SEAM_ATTRS:
            self.assertIsNotNone(seams[name],
                                 "seam %s must be delivered now" % name)
        # _BROKER_OPS 与 _DRAIN 共享同一 gate 实例；_TS_SERVICE_CONTROL 与
        # 两者共用同一 broker 适配器。
        ops = seams["_BROKER_OPS"]
        self.assertEqual(seams["_TS_SERVICE_CONTROL"].keywords["broker"],
                         ops._broker)
        # Revision 8 dedicated-backend seams: the gate object, the read-only
        # observation and the key precondition are each delivered (8 -> 11).
        self.assertIsInstance(seams["_BACKEND_OPS"],
                              prod._ProductionBackendOps)
        self.assertIs(seams["_BACKEND_OBSERVE"], prod.production_backend_observe)
        self.assertIs(seams["_KEYS_CONFIRMED"], prod.production_keys_confirmed)
        # 2026-09-22 diagnosis split (12): same key-precondition seam
        # family, the bare function when no manager is injected.
        self.assertIs(seams["_KEYS_DIAGNOSIS"], prod.production_keys_diagnosis)

    def test_wired_seams_drive_orchestration_accessors(self):
        client = _FakeBrokerClient([
            (True, "ok", {"ts_service_started": True}),
            (False, "bad_request", {"detail": "unknown action"})])
        broker = _FullFakeBroker(ts_client=client)
        host = _FakeHost(running=True)
        # 脚本次序：ts 状态 sc 查询 -> connectivity 的 status --json ->
        # funnel off（cmd + serve status 复核）-> 第二次 funnel off（rc 失败）。
        runner = _FakeRunner([
            (0, "STATE              : 4  RUNNING", ""),
            (0, _TS_STATUS_JSON_RUNNING, ""),
            (0, "", ""),
            (0, "clean", ""),
            (5, "", ""),
        ])
        seams = prod.production_seams(broker=broker,
                                      exe_path=_EXE, runner=runner,
                                      sleeper=_no_sleep)
        saved = {name: getattr(orch, name) for name in _RESTORE_ATTRS}
        try:
            orch._BROKER_OPS = seams["_BROKER_OPS"]
            orch._TS_SERVICE_CONTROL = seams["_TS_SERVICE_CONTROL"]
            orch._TS_SERVICE_STATUS = seams["_TS_SERVICE_STATUS"]
            orch._CONNECTIVITY_OK = seams["_CONNECTIVITY_OK"]
            orch._FUNNEL_CONTROL = seams["_FUNNEL_CONTROL"]

            # TS 状态观察经接线缝：sc 查询解析 RUNNING
            self.assertEqual(orch._ts_service_status(), "running")

            # connectivity 经接线缝：tailscale status 自节点 -> True
            self.assertIs(orch._connectivity_ok(), True)

            # funnel off：真值成功 -> 假值失败（从不假成功）
            ack = orch._funnel_control("off")
            self.assertIs(ack["ok"], True)
            self.assertTrue(ack)
            ack = orch._funnel_control("off")
            self.assertIs(ack["ok"], False)
            self.assertFalse(ack)

            # TS 服务控制经接线缝走 broker 适配器的公开控制动词
            ack = orch._ts_service_control("start")
            self.assertTrue(ack)
            self.assertEqual(broker.ts_calls,
                             [("ts_service_control", "start")])
            self.assertEqual(client.calls[0],
                             ("ts-service-control", {"action": "start"}))
            with self.assertRaises(OperationError) as caught:
                orch._ts_service_control("start")
            self.assertEqual(caught.exception.code,
                             "share_broker_extension_missing")

            # broker gate passes through to the injected host/adapter
            self.assertIs(orch._broker_ops().verify_running(), True)
        finally:
            for name, value in saved.items():
                setattr(orch, name, value)

    def test_wired_drain_seam_and_desired_stop_reader(self):
        broker = _FullFakeBroker()
        host = _FakeHost(running=False, closed=True)
        with tempfile.TemporaryDirectory() as td:
            status_path = Path(td) / "broker-status.json"
            status_path.write_text(json.dumps(
                {"lifecycle": {"state": "stopped", "at": 1720000000,
                               "by": "drain"}}), encoding="utf-8")
            seams = prod.production_seams(broker=broker,
                                          status_path=str(status_path))
            saved = {name: getattr(orch, name) for name in _RESTORE_ATTRS}
            try:
                orch._DRAIN = seams["_DRAIN"]
                orch._DESIRED_STOP_READER = seams["_DESIRED_STOP_READER"]
                # 排空缝：编排排空协议形状（drained/broker/observed）
                outcome = seams["_DRAIN"](0.05, 0.01, None)
                self.assertEqual(sorted(outcome),
                                 ["broker", "drained", "observed"])
                self.assertIs(outcome["drained"], True)
                self.assertEqual(
                    outcome["observed"],
                    "broker SYSTEM 端已停后端并三证确认关闭")
                self.assertTrue(outcome["broker"]["stopped"])
                # desired-stop reader：broker 生命周期记录 stopped -> True
                self.assertIs(orch._desired_stop_in_force(), True)
                # 文件移除 -> None（不冒充权威）
                status_path.unlink()
                self.assertIsNone(orch._desired_stop_in_force())
            finally:
                for name, value in saved.items():
                    setattr(orch, name, value)

    def test_wired_seams_read_only_status_stays_honest(self):
        broker = _FullFakeBroker()
        host = _FakeHost(running=True, closed=False)
        # share_status 观察顺序：专用后端（无 runner）-> ts 状态（sc 查询）->
        # connectivity -> backend（host verify）-> funnel（serve status）。
        runner = _FakeRunner([
            (0, "STATE              : 4  RUNNING", ""),
            (0, _TS_STATUS_JSON_RUNNING, ""),
            (0, _MAPPING_OUT, ""),
        ])
        with tempfile.TemporaryDirectory() as td:
            # 专用后端路径与归因密钥 RunRoot 都隔离到临时目录：观察缝只读
            # 一个缺席 pid 文件与一个缺席密钥文件，从不触碰真实
            # LOCALAPPDATA/端口/进程/部署密钥（真实密钥由维护者按
            # SHARING.md 生成，可能已存在；归因读面的文件就绪判定不依赖它）。
            env = mock.patch.dict(os.environ, {
                "CONSOLE_SHARE_BACKEND_DIR": td,
                prod.SHARE_RUN_ROOT_ENV: td})
            env.start()
            self.addCleanup(env.stop)
            status_path = Path(td) / "broker-status.json"
            status_path.write_text(json.dumps(
                {"lifecycle": {"state": "running", "at": 1720000000,
                               "by": "start"}}), encoding="utf-8")
            seams = prod.production_seams(
                broker=broker, exe_path=_EXE, runner=runner,
                status_path=str(status_path))
            saved = {name: getattr(orch, name) for name in _RESTORE_ATTRS}
            try:
                for name in _SEAM_ATTRS:
                    setattr(orch, name, seams[name])
                status = orch.share_status("um-share", {}, None)
                # 专用后端观察缝：无 pid 文件 -> running=False -> stopped
                self.assertEqual(status["layers"]["share_backend"], "stopped")
                # 归因密钥路径已重定向到临时目录：文件事实确定性「未就绪」，
                # 8100 实例探测因此不被调用（有真实密钥时读面探测实例是正确
                # 行为，本用例不依赖它）。
                self.assertIs(
                    status["backend_auth"]["attribution"]["file_ready"], False)
                self.assertEqual(status["layers"]["ts_service"], "running")
                self.assertEqual(status["layers"]["connectivity"], "ok")
                self.assertEqual(status["layers"]["backend"], "running")
                self.assertEqual(status["layers"]["funnel"], "on")
                # broker 生命周期记录：running -> False（开启已确认）
                self.assertIs(status["desired_stop_in_force"], False)
            finally:
                for name, value in saved.items():
                    setattr(orch, name, value)

    def test_wired_seams_report_unavailable_when_queries_fail(self):
        broker = _FullFakeBroker()
        host = _FakeHost(running=True, closed=False)
        runner = _FakeRunner([
            (7, "", "sc query failed"),   # ts 状态查询失败 -> unavailable
            (5, "", "status failed"),     # connectivity 查询失败 -> unknown→异常?
        ])
        # connectivity 查询失败现在抛 share_status_unknown（不冒充离线），
        # share_status 层的只读观察会如实上抛。
        seams = prod.production_seams(broker=broker,
                                      exe_path=_EXE, runner=runner)
        saved = {name: getattr(orch, name) for name in _RESTORE_ATTRS}
        try:
            orch._TS_SERVICE_STATUS = seams["_TS_SERVICE_STATUS"]
            self.assertIsNone(orch._ts_service_status())
            orch._CONNECTIVITY_OK = seams["_CONNECTIVITY_OK"]
            with self.assertRaises(OperationError) as caught:
                orch.share_status("um-share", {}, None)
            self.assertEqual(caught.exception.code, "share_status_unknown")
        finally:
            for name, value in saved.items():
                setattr(orch, name, value)


class SubprocessHygieneTests(unittest.TestCase):
    """Test 7 (AST half): the module's ONLY subprocess surface is
    subprocess.run / subprocess.Popen with controlled argument arrays and
    NEVER shell=True — no other spawn entry point. (The zero-real-spawn half
    is the module-wide isolation sentinel; the sc.exe query reaches the
    console only through the shared restricted TailscaleServiceOps
    primitive, and the dedicated-backend gate's tasklist/wmic/netstat/
    taskkill/urlopen surfaces are isolated-tested in
    DedicatedBackendOpsTests with every boundary patched.)"""

    def test_module_subprocess_shape_ast_controlled_arrays_only(self):
        tree = ast.parse(Path(_SOURCE_FILE).read_text("utf-8"))
        seen = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) \
                    and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id == "subprocess":
                self.assertIn(node.func.attr, ("run", "Popen"),
                              "unexpected subprocess entry point "
                              "%s at line %d" % (node.func.attr, node.lineno))
                seen += 1
                for kw in node.keywords:
                    if kw.arg == "shell":
                        self.assertIsInstance(kw.value, ast.Constant)
                        self.assertIs(kw.value.value, False,
                                      "shell=True at line %d" % node.lineno)
        self.assertGreaterEqual(seen, 1)

    def test_isolation_sentinel_fails_on_tokens_and_delegates_rest(self):
        # 哨兵机制单元验证（不经过真实进程）：命中命令响亮失败；其余委托。
        # 本用例产生的哨兵日志条目先快照再复原，不污染其他用例的零调用断言。
        before = list(_spawn_log)
        calls = []

        def fake_original(*args, **kwargs):
            calls.append(args)
            return "delegated"

        try:
            wrapper = _guarded_spawn("run", fake_original)
            for cmd in (["tailscale.exe", "status"], ["schtasks", "/Query"],
                        ["sc.exe", "query", "Tailscale"]):
                with self.assertRaises(AssertionError) as caught:
                    wrapper(cmd)
                self.assertIn("隔离哨兵", str(caught.exception))
                self.assertEqual(calls, [])
            self.assertEqual(wrapper(["python", "-c", "print(1)"]),
                             "delegated")
            self.assertEqual(calls, [(["python", "-c", "print(1)"],)])
        finally:
            _spawn_log[:] = before


class TsServiceStatusTests(unittest.TestCase):
    """Read-only TS service observation: the shared restricted primitive's
    sc.exe query parse (running/stopped/absent/None), fixed argument array,
    no privilege needed."""

    def test_argument_array_is_fixed_sc_query_shape(self):
        runner = _FakeRunner([(0, "STATE              : 4  RUNNING", "")])
        state = prod.production_ts_service_status(runner=runner)
        self.assertEqual([c[0] for c in runner.calls], [_SC_QUERY_ARGS])
        self.assertEqual(state, "running")
        # 与 broker 受限原语共用同一解析裁定（复用既有逻辑，不另起一套）。
        self.assertEqual(TailscaleServiceOps(runner=runner).status(), state)

    def test_stopped_and_absent_and_pending_states(self):
        runner = _FakeRunner([(0, "STATE              : 1  STOPPED", "")])
        self.assertEqual(prod.production_ts_service_status(runner=runner),
                         "stopped")
        runner = _FakeRunner([(1060, "", "service does not exist")])
        self.assertEqual(prod.production_ts_service_status(runner=runner),
                         "absent")
        runner = _FakeRunner([(0, "STATE              : 2  START_PENDING",
                               "")])
        self.assertEqual(prod.production_ts_service_status(runner=runner),
                         "start_pending")

    def test_failed_or_unparseable_query_is_none(self):
        runner = _FakeRunner([(7, "", "query failed")])
        self.assertIsNone(prod.production_ts_service_status(runner=runner))
        runner = _FakeRunner([(0, "garbage output", "")])
        self.assertIsNone(prod.production_ts_service_status(runner=runner))
        runner = _FakeRunner([OSError("boom")])
        self.assertIsNone(prod.production_ts_service_status(runner=runner))
        runner = _FakeRunner([FileNotFoundError(2, "no sc.exe")])
        self.assertIsNone(prod.production_ts_service_status(runner=runner))

    def test_wired_status_seam_uses_injected_runner(self):
        runner = _FakeRunner([(0, "STATE              : 1  STOPPED", "")])
        seams = prod.production_seams(broker=_adapter(),
                                      exe_path=_EXE, runner=runner)
        self.assertEqual(seams["_TS_SERVICE_STATUS"](), "stopped")
        self.assertEqual([c[0] for c in runner.calls], [_SC_QUERY_ARGS])


class FunnelObservedTests(unittest.TestCase):
    """Funnel observation seam: the SAME serve-status parse as the funnel
    control re-checks — mapping -> True, clean query -> False, failed query
    -> None, exe absent -> share_privilege_unwired."""

    def test_mapping_present_is_on(self):
        runner = _FakeRunner([(0, _MAPPING_OUT, "")])
        self.assertIs(prod.production_funnel_observed(exe_path=_EXE,
                                                      runner=runner), True)
        self.assertEqual([c[0] for c in runner.calls], [_STATUS_ARGS])

    def test_clean_status_is_off(self):
        runner = _FakeRunner([(0, "", "")])
        self.assertIs(prod.production_funnel_observed(exe_path=_EXE,
                                                      runner=runner), False)

    def test_failed_query_is_none_never_off(self):
        runner = _FakeRunner([(5, "", "err")])
        self.assertIsNone(prod.production_funnel_observed(exe_path=_EXE,
                                                          runner=runner))
        runner = _FakeRunner([OSError("boom")])
        self.assertIsNone(prod.production_funnel_observed(exe_path=_EXE,
                                                          runner=runner))

    def test_exe_absent_raises_unwired(self):
        runner = _FakeRunner([FileNotFoundError(2, "missing")])
        with self.assertRaises(OperationError) as caught:
            prod.production_funnel_observed(exe_path=_EXE, runner=runner)
        self.assertEqual(caught.exception.code, "share_privilege_unwired")


class DrainTests(unittest.TestCase):
    """The production drain over the broker gate: the adapter contract
    refuses any reply without the explicit stopped=True, so a returned
    result IS the broker's SYSTEM-verified backend closure (bounded
    in-flight wait, host stop and three-evidence check all ran inside the
    broker — the in-process observation is gone)."""

    def test_success_shape_carries_broker_verified_closure(self):
        adapter = _adapter()
        outcome = prod.production_drain(0.05, 0.01, None, broker=adapter,
                                        sleeper=_no_sleep)
        self.assertEqual(sorted(outcome), ["broker", "drained", "observed"])
        self.assertIs(outcome["drained"], True)
        self.assertEqual(
            outcome["observed"],
            "broker SYSTEM 端已停后端并三证确认关闭")
        self.assertEqual(outcome["broker"],
                         {"drained": True, "timed_out": False,
                          "stopped": True})
        adapter.graceful_stop.assert_called_once_with()

    def test_timed_out_but_verified_closure_still_accepted(self):
        # Broker-side deadline: in-flight lingered (timed_out) but the
        # SYSTEM host stop + three-evidence closure DID verify — the drain
        # seam accepts the verified closure as the backend confirmation.
        adapter = _adapter()
        adapter.graceful_stop.return_value = {"drained": False,
                                              "timed_out": True,
                                              "stopped": True}
        outcome = prod.production_drain(0.05, 0.0, None, broker=adapter,
                                        sleeper=_no_sleep)
        self.assertIs(outcome["drained"], True)
        self.assertIs(outcome["broker"]["stopped"], True)

    def test_non_dict_drain_result_refused(self):
        adapter = _adapter()
        adapter.graceful_stop.return_value = "ok-ish"
        with self.assertRaises(OperationError) as caught:
            prod.production_drain(0.05, 0.0, None, broker=adapter,
                                  sleeper=_no_sleep)
        self.assertEqual(caught.exception.code, "share_layer_failed")
        self.assertIn("形状不符合协议", caught.exception.message)

    def test_unconfirmed_shape_refused(self):
        adapter = _adapter()
        adapter.graceful_stop.return_value = {"drained": False,
                                              "timed_out": False,
                                              "stopped": False}
        with self.assertRaises(OperationError) as caught:
            prod.production_drain(0.05, 0.0, None, broker=adapter,
                                  sleeper=_no_sleep)
        self.assertEqual(caught.exception.code, "share_layer_failed")
        self.assertIn("未确认 drained/timed_out/stopped",
                      caught.exception.message)

    def test_adapter_stopped_false_refused(self):
        # The adapter contract raises on any reply lacking stopped=True;
        # a dict that slips through with stopped=False is still refused.
        adapter = _adapter()
        adapter.graceful_stop.side_effect = BrokerError(
            "drain completed but the final stop was not confirmed")
        with self.assertRaises(OperationError) as caught:
            prod.production_drain(0.05, 0.0, None, broker=adapter,
                                  sleeper=_no_sleep)
        self.assertEqual(caught.exception.code, "share_layer_failed")
        self.assertIn("BrokerError", caught.exception.message)

    def test_gate_exception_wraps_as_share_layer_failed(self):
        adapter = _adapter()
        adapter.graceful_stop.side_effect = RuntimeError("pipe gone")
        with self.assertRaises(OperationError) as caught:
            prod.production_drain(0.05, 0.0, None, broker=adapter,
                                  sleeper=_no_sleep)
        self.assertEqual(caught.exception.code, "share_layer_failed")
        self.assertIn("RuntimeError", caught.exception.message)

    def test_operation_error_passes_through_unchanged(self):
        adapter = _adapter()
        adapter.graceful_stop.side_effect = OperationError(
            "share_broker_unwired", "gate refused")
        with self.assertRaises(OperationError) as caught:
            prod.production_drain(0.05, 0.0, None, broker=adapter,
                                  sleeper=_no_sleep)
        self.assertEqual(caught.exception.code, "share_broker_unwired")


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
    """真实默认装配验证：wire_share_production_seams() 走真实生产装配链
    （console_adapters -> production_seams -> usagesrv loader 全链），只替换
    最末端 OS 边界（fake 管道 transport、fake 控制密钥读取、tmp 部署配置；
    fake tailscale 仅路径绑定从不执行），断言装配全程零真实调用——哨兵在场，
    任何 tailscale/schtasks/sc.exe 命令尝试都会响亮失败。"""

    def setUp(self):
        _spawn_log.clear()

    def test_real_default_assembly_wires_all_seams_without_real_calls(self):
        import console_adapters
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "share-management.json"
            key = Path(td) / "broker-control-key"
            key.write_text("test-key", encoding="utf-8")
            config.write_text(json.dumps({
                "schema_version": 1, "enabled": True,
                "pipe_name": "\\\\.\\pipe\\um-share-broker",
                "control_key_file": str(key)}), encoding="utf-8")
            opened = []

            class _FakeTransport:
                # 生产形状的 transport 替身：装配只构造 factory，从不 open。
                def __init__(self, pipe_name, kernel=None):
                    self.pipe_name = pipe_name
                    opened.append(pipe_name)

                def open(self):
                    return self

                def close(self):
                    pass

            saved_seams = {name: getattr(orch, name) for name in _RESTORE_ATTRS}
            saved_config = usagesrv.SHARE_MANAGEMENT_CONFIG_FILE
            spawn_before = list(_spawn_log)
            try:
                with _broker_loader(
                        usagesrv._load_share_management_deployment), \
                     mock.patch.object(usagesrv, "SHARE_MANAGEMENT_CONFIG_FILE",
                                       str(config)), \
                     mock.patch.object(share_broker_server,
                                       "read_control_key",
                                       lambda path: "test-key"), \
                     mock.patch.object(share_broker_client,
                                       "NamedPipeBrokerTransport",
                                       _FakeTransport):
                    console_adapters.wire_share_production_seams()
                for name in _SEAM_ATTRS:
                    self.assertIsNotNone(getattr(orch, name),
                                         "%s 未接线" % name)
                # 管道边界在装配期从未被触碰（factory 未 open）
                self.assertEqual(opened, [])
                # 全程零真实 OS 命令尝试（哨兵在场：命中即响亮失败）
                self.assertEqual(_spawn_log, spawn_before)
            finally:
                for name, value in saved_seams.items():
                    setattr(orch, name, value)
                usagesrv.SHARE_MANAGEMENT_CONFIG_FILE = saved_config

    def test_real_default_assembly_ts_control_goes_through_fake_pipe(self):
        # 装配后的 _TS_SERVICE_CONTROL 缝发真实协议动词；管道边界是替身
        # transport，回帧由替身给出 —— 全链（真实 loader/适配器形状）零真实
        # OS 命令。
        import console_adapters
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "share-management.json"
            key = Path(td) / "broker-control-key"
            key.write_text("test-key", encoding="utf-8")
            config.write_text(json.dumps({
                "schema_version": 1, "enabled": True,
                "pipe_name": "\\\\.\\pipe\\um-share-broker",
                "control_key_file": str(key)}), encoding="utf-8")

            class _FakeTransport:
                def __init__(self, pipe_name, kernel=None):
                    self.pipe_name = pipe_name
                    self._opened = False

                def open(self):
                    self._opened = True
                    return self

                def close(self):
                    pass

                def send_all(self, data, deadline=None):
                    self.reply = json.dumps({
                        "v": 2, "ok": True, "error_code": "ok",
                        "result": {"ts_service_started": True},
                        "request_id": json.loads(data)["request_id"]},
                        ensure_ascii=False).encode("utf-8") + b"\n"

                def recv_line(self, deadline):
                    return self.reply

            saved_seams = {name: getattr(orch, name) for name in _RESTORE_ATTRS}
            saved_config = usagesrv.SHARE_MANAGEMENT_CONFIG_FILE
            spawn_before = list(_spawn_log)
            try:
                with _broker_loader(
                        usagesrv._load_share_management_deployment), \
                     mock.patch.object(usagesrv, "SHARE_MANAGEMENT_CONFIG_FILE",
                                       str(config)), \
                     mock.patch.object(share_broker_server,
                                       "read_control_key",
                                       lambda path: "test-key"), \
                     mock.patch.object(share_broker_client,
                                       "NamedPipeBrokerTransport",
                                       _FakeTransport):
                    console_adapters.wire_share_production_seams()
                ack = orch._ts_service_control("start")
                self.assertTrue(ack)
                self.assertEqual(_spawn_log, spawn_before)
            finally:
                for name, value in saved_seams.items():
                    setattr(orch, name, value)
                usagesrv.SHARE_MANAGEMENT_CONFIG_FILE = saved_config

    # ---- R13-T: the REAL default assembly drives the TS service
    # confirmation/start and the connectivity wait with ONLY the farthest
    # process/system-call ends replaced (fake sc.exe/tailscale runner + fake
    # pipe transport). Tray absence is irrelevant to this path by
    # construction: the privileged service control runs INSIDE the SYSTEM
    # broker, never through a tray process.

    _SC_STOPPED = "STATE              : 1  STOPPED"
    _SC_RUNNING = "STATE              : 4  RUNNING"

    @staticmethod
    def _status_payload(state, ips):
        return json.dumps({"Version": "1.82.0", "BackendState": state,
                           "Self": {"ID": "node-self", "HostName": "desktop",
                                    "TailscaleIPs": ips}})

    def _assembly(self, sc_seq, ts_seq):
        """Wire the REAL default assembly; return (teardown_fn, runner_calls,
        broker_requests) with the sc.exe/tailscale runner scripted by
        `sc_seq`/`ts_seq` (last item repeats) and the fake pipe transport
        recording every request action."""
        import console_adapters
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        config = Path(td.name) / "share-management.json"
        key = Path(td.name) / "broker-control-key"
        key.write_text("test-key", encoding="utf-8")
        config.write_text(json.dumps({
            "schema_version": 1, "enabled": True,
            "pipe_name": "\\\\.\\pipe\\um-share-broker",
            "control_key_file": str(key)}), encoding="utf-8")

        sc_script = list(sc_seq)
        ts_script = list(ts_seq)
        runner_calls = []

        def runner(args, timeout):
            runner_calls.append(list(args))
            if args and args[0] == "sc.exe":
                body = sc_script.pop(0) if len(sc_script) > 1 else sc_script[0]
                return SimpleNamespace(returncode=0, stdout=body, stderr="")
            body = ts_script.pop(0) if len(ts_script) > 1 else ts_script[0]
            return SimpleNamespace(returncode=0, stdout=body, stderr="")

        class _RecordingTransport:
            requests = []

            def __init__(self, pipe_name, kernel=None):
                self.pipe_name = pipe_name

            def open(self):
                return self

            def close(self):
                pass

            def send_all(self, data, deadline=None):
                body = json.loads(data)
                _RecordingTransport.requests.append(body.get("action"))
                self.reply = json.dumps({
                    "v": 2, "ok": True, "error_code": "ok",
                    "result": {"ts_service_started": True},
                    "request_id": body["request_id"]},
                    ensure_ascii=False).encode("utf-8") + b"\n"

            def recv_line(self, deadline):
                return self.reply

        saved_seams = {name: getattr(orch, name) for name in _RESTORE_ATTRS}
        saved_config = usagesrv.SHARE_MANAGEMENT_CONFIG_FILE
        spawn_before = list(_spawn_log)
        _RecordingTransport.requests = []
        # Patchers stay ACTIVE through the drive below (the seam partials
        # resolve prod._default_runner at call time); addCleanup restores.
        for patcher in (
                mock.patch.object(usagesrv, "SHARE_MANAGEMENT_CONFIG_FILE",
                                  str(config)),
                mock.patch.object(share_broker_server, "read_control_key",
                                  lambda path: "test-key"),
                mock.patch.object(share_broker_client,
                                  "NamedPipeBrokerTransport",
                                  _RecordingTransport),
                mock.patch.object(prod, "_default_runner", runner),
                mock.patch.object(TailscaleServiceOps, "_default_runner",
                                  staticmethod(runner))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._restore_seams_and_config, saved_seams,
                        saved_config)
        self.addCleanup(prod.configure_broker_loader, prod._BROKER_LOADER)
        prod.configure_broker_loader(
            usagesrv._load_share_management_deployment)
        console_adapters.wire_share_production_seams()
        self.assertEqual(_spawn_log, spawn_before)
        return runner_calls, _RecordingTransport.requests

    @staticmethod
    def _restore_seams_and_config(saved_seams, saved_config):
        for name, value in saved_seams.items():
            setattr(orch, name, value)
        usagesrv.SHARE_MANAGEMENT_CONFIG_FILE = saved_config

    def test_full_assembly_tray_absent_service_stopped_opens_ts_layers(self):
        # R13-T item 5: 托盘不存在而服务 stopped 的完整默认装配路径 - the
        # REAL production assembly starts the stopped Tailscale service
        # through the broker's restricted verb (no tray involved: the
        # privileged control runs INSIDE the SYSTEM broker), re-confirms
        # running over the read-only sc observation, then waits for the
        # node's own BackendState (NoState transitional -> Running) and the
        # verdict seam. The TS service lands in the ownership ledger.
        runner_calls, broker_requests = self._assembly(
            [self._SC_STOPPED, self._SC_RUNNING],
            [self._status_payload("NoState", []),
             self._status_payload("NoState", []),
             self._status_payload("Running", ["100.64.12.24"]),
             self._status_payload("Running", ["100.64.12.24"])])
        ledger = orch._OpenLedger()
        orch._open_ts_service(None, ledger, {"ts": 5}, 0.01)
        orch._open_connectivity(None, ledger, {"connectivity": 5}, 0.01)
        self.assertEqual(ledger.owned, ["ts_service"])
        facts = " | ".join(ledger.facts)
        self.assertIn("ts_service 已启动并确认 running", facts)
        self.assertIn("connectivity 已确认就绪", facts)
        # 受控进程边界次序：sc 预检 -> broker 动词 -> sc 复核 -> status 观察对。
        sc_calls = [c for c in runner_calls if c[0] == "sc.exe"]
        status_calls = [c for c in runner_calls if c[0] != "sc.exe"]
        self.assertEqual(len(sc_calls), 2)
        self.assertEqual(len(status_calls), 4)
        self.assertEqual(broker_requests, ["ts-service-control"])

    def test_full_assembly_ts_already_running_skips_control(self):
        # 服务已运行：装配后的开启层只观察、不调用受限动词、不归属
        #（回滚永不触碰先前已在运行的服务）。
        runner_calls, broker_requests = self._assembly(
            [self._SC_RUNNING], [])
        ledger = orch._OpenLedger()
        orch._open_ts_service(None, ledger, {"ts": 5}, 0.01)
        self.assertEqual(ledger.owned, [])
        self.assertIn("ts_service 开启前已在运行", " | ".join(ledger.facts))
        self.assertEqual(broker_requests, [])
        self.assertEqual([c for c in runner_calls if c[0] == "sc.exe"],
                         [["sc.exe", "query", "Tailscale"]])

    def test_full_assembly_connectivity_needs_login_stops_explicitly(self):
        # 节点需人工登录经真实装配呈现为明确停点（share_ts_needs_login）：
        # 首个 tick 只问 BackendState 通道一次，裁定缝零调用，预算不烧。
        runner_calls, _ = self._assembly([], [self._status_payload(
            "NeedsLogin", [])])
        ledger = orch._OpenLedger()
        with self.assertRaises(OperationError) as caught:
            orch._open_connectivity(None, ledger, {"connectivity": 300},
                                    0.01)
        self.assertEqual(caught.exception.code, "share_ts_needs_login")
        self.assertIn("需要人工登录", caught.exception.message)
        status_calls = [c for c in runner_calls if c[0] != "sc.exe"]
        self.assertEqual(len(status_calls), 1)


class AssemblyCancelIsolationTests(unittest.TestCase):
    def test_assembly_cleanup_and_following_repair_preserve_default_cancel(self):
        # Exercise the actual cross-test order. The default watch directory is
        # also a sandbox, so a regression cannot reach the operator's files.
        with tempfile.TemporaryDirectory(prefix="share-cancel-isolation-") as td:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": td}):
                import share_public_supervision as supervision
                import console_adapters
                from tests.test_console_share_repair import RepairTests

                watch_dir = Path(td) / "default-watch"
                cancel_file = watch_dir / supervision.CANCEL_FILE_NAME
                request_before = lambda reason: True
                clear_before = lambda: True
                for existing in (False, True):
                    with self.subTest(existing_cancel=existing):
                        watch_dir.mkdir(exist_ok=True)
                        original = None
                        if existing:
                            original = json.dumps({
                                "version": 1, "source": "manual_close",
                                "reason": "manual_close",
                                "at_utc": "2026-10-24T00:00:00+00:00",
                            }).encode("utf-8")
                            cancel_file.write_bytes(original)
                        else:
                            cancel_file.unlink(missing_ok=True)
                        with mock.patch.object(
                                supervision, "DEFAULT_WATCH_DIR", str(watch_dir)), \
                                mock.patch.object(
                                    supervision, "LOCK_DIR", str(Path(td) / "locks")), \
                                mock.patch.multiple(
                                    orch, _PUBLIC_CANCEL_REQUEST=request_before,
                                    _PUBLIC_CANCEL_CLEAR=clear_before):
                            result = unittest.TestResult()
                            assembly = ProductionAssemblyTests(
                                "test_full_assembly_tray_absent_service_stopped_opens_ts_layers")
                            assembly.run(result)
                            self.assertTrue(result.wasSuccessful(),
                                            result.errors + result.failures)
                            # A following open/test must use the restored clear
                            # seam, not silently remove an existing human stop.
                            orch._PUBLIC_CANCEL_CLEAR()
                            # Repair tests must isolate file I/O even when a
                            # previous caller intentionally wired production.
                            with mock.patch.multiple(
                                    orch,
                                    _PUBLIC_CANCEL_REQUEST=console_adapters.production_public_cancel_request,
                                    _PUBLIC_CANCEL_CLEAR=console_adapters.production_public_cancel_clear):
                                RepairTests(
                                    "test_failed_public_close_never_rebuilds_rules"
                                ).run(result)
                            self.assertTrue(result.wasSuccessful(),
                                            result.errors + result.failures)
                            observed = cancel_file.read_bytes() \
                                if cancel_file.exists() else None
                            self.assertEqual(observed, original)
                            self.assertIs(orch._PUBLIC_CANCEL_REQUEST,
                                          request_before)
                            self.assertIs(orch._PUBLIC_CANCEL_CLEAR, clear_before)


class _FakeHttpResponse:
    """Context-manager response stand-in for the patched urlopen (the
    production code reads `with urlopen(req) as resp: resp.read()`)."""

    def __init__(self, body, status=200):
        self._body = body
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeClock:
    """Deterministic monotonic/sleep pair for the stop() deadline loop (no
    real waiting; the patched prod.time attributes are restored on exit)."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class DedicatedBackendOpsTests(unittest.TestCase):
    """R8-A7 专用分享后端门（_ProductionBackendOps）的隔离用例：spawner/
    stopper 缝 + tempfile 目录 + CONSOLE_SHARE_BACKEND_DIR 显式重定向；
    _pid_alive/_pid_cmdline/_pid_matches/_port_listening/_graceful_shutdown
    与 urlopen 一律打补丁——每个用例断言 _spawn_log 零增量（零真实 OS 命令）
    和 urlopen 调用计数（零真实网络）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.backend_dir = os.path.join(self._tmp.name, "backend")
        # 归因密钥是部署事实（默认在 ProgramData 的 RunRoot\run 下）：测试把
        # RunRoot 重定向到临时目录并放一个假密钥（40 字符，非真实密钥），
        # 绝不写真实 data/ 或真实部署文件。
        self.run_root = os.path.join(self._tmp.name, "runroot")
        self.attribution_key = os.path.join(
            self.run_root, "run", prod.SHARE_ATTRIBUTION_KEY_NAME)
        os.makedirs(os.path.dirname(self.attribution_key), exist_ok=True)
        Path(self.attribution_key).write_text("x" * 40, encoding="utf-8")
        os.makedirs(self.backend_dir, exist_ok=True)
        env = mock.patch.dict(os.environ, {
            "CONSOLE_SHARE_BACKEND_DIR": self.backend_dir,
            prod.SHARE_RUN_ROOT_ENV: self.run_root})
        env.start()
        self.addCleanup(env.stop)
        # 密钥路径的覆盖开关同样中性化：外部设置它时 _paths() 会解析到别的
        # 文件，与临时 RunRoot 下的假密钥不一致（patch.dict 退出时还原）。
        os.environ.pop(prod.SHARE_ATTRIBUTION_KEY_FILE_ENV, None)
        self.urlopen_calls = []
        self._spawn_before = list(_spawn_log)

    def _assert_zero_real_spawn(self):
        self.assertEqual(_spawn_log, self._spawn_before,
                         "real OS command attempted: %r"
                         % (_spawn_log[len(self._spawn_before):],))

    def _ops(self, spawner=None, stopper=None, port=8110,
             base_url="https://backend.example"):
        return prod._ProductionBackendOps(
            gateway_dir=str(Path(self.backend_dir) / "chat2api"), port=port,
            base_url=base_url, spawner=spawner, stopper=stopper)

    def _write_pid(self, ops, pid=4242):
        paths = ops._paths()
        with open(paths["pid"], "w", encoding="ascii") as f:
            f.write(str(pid))
        return paths

    # ---- start -----------------------------------------------------------

    def test_start_spawns_isolated_child_and_writes_pid_file(self):
        spawned = []

        def spawner(cmdline, env):
            spawned.append((list(cmdline), dict(env)))
            return SimpleNamespace(pid=4242)

        ops = self._ops(spawner=spawner)
        # No pid file yet: honestly not running (no patched boundary touched).
        with mock.patch.object(ops, "_pid_matches", new=lambda p, pid: True), \
                mock.patch.object(ops, "_port_listening", new=lambda p: False):
            self.assertIs(ops.verify_running(), False)
        self.assertTrue(ops.start())
        # spawner 缝收到参数数组：chat2api.py + 独立 --port + 独立 --profile
        self.assertEqual(len(spawned), 1)
        cmdline, child_env = spawned[0]
        paths = ops._paths()
        self.assertIn("chat2api.py", cmdline)
        self.assertEqual(cmdline[cmdline.index("--port") + 1],
                         str(paths["port"]))
        self.assertEqual(cmdline[cmdline.index("--profile") + 1],
                         paths["profile"])
        self.assertEqual(cmdline[cmdline.index("--base-url") + 1],
                         "https://backend.example")
        # env 四项状态重定向全部落在 backend 目录内（与生产实例零共享）。
        for key, expected in (("CHAT2API_TOKEN_FILE", paths["token"]),
                              ("CHAT2API_SERVICE_LOG", paths["log"]),
                              ("CHAT2API_USAGE_LOG", paths["ledger"]),
                              ("CHAT2API_CONTROL_TOKEN_FILE",
                               paths["control_token"])):
            self.assertEqual(child_env.get(key), expected)
            self.assertTrue(expected.startswith(paths["dir"] + os.sep))
        # pid 文件写入 spawner 返回的 pid；profile 目录已创建。
        with open(paths["pid"], encoding="ascii") as f:
            self.assertEqual(f.read().strip(), "4242")
        self.assertTrue(os.path.isdir(paths["profile"]))
        self._assert_zero_real_spawn()
        # verify_running：verify 缝下 False -> True（pid 匹配 + 端口在听）。
        with mock.patch.object(ops, "_pid_matches", new=lambda p, pid: True), \
                mock.patch.object(ops, "_port_listening", new=lambda p: True):
            self.assertIs(ops.verify_running(), True)
        self._assert_zero_real_spawn()

    def test_start_skips_when_already_running_never_owned_twice(self):
        spawned = []

        def spawner(cmdline, env):
            spawned.append(cmdline)
            return SimpleNamespace(pid=1)

        ops = self._ops(spawner=spawner)
        with mock.patch.object(ops, "verify_running", return_value=True):
            self.assertTrue(ops.start())
        self.assertEqual(spawned, [])
        self.assertFalse(os.path.exists(ops._paths()["pid"]))
        self._assert_zero_real_spawn()

    def test_child_cmdline_matches_paths_and_port(self):
        ops = self._ops(port=8123)
        paths = ops._paths()
        cmdline = ops._child_cmdline(paths)
        self.assertEqual(cmdline[cmdline.index("--port") + 1],
                         str(paths["port"]))
        self.assertEqual(cmdline[cmdline.index("--port") + 1], "8123")
        self.assertEqual(cmdline[cmdline.index("--profile") + 1],
                         paths["profile"])
        # D18-6: the dedicated instance's human login window is 30 minutes
        # (1800s); production chat2api keeps its own default.
        self.assertEqual(cmdline[cmdline.index("--login-timeout") + 1],
                         str(prod._BACKEND_LOGIN_TIMEOUT_S))
        self.assertEqual(prod._BACKEND_LOGIN_TIMEOUT_S, 1800)
        # 独立 profile 位于 backend 目录内（隔离状态目录）
        self.assertTrue(paths["profile"].startswith(paths["dir"] + os.sep))

    def test_child_env_redirects_state_files_into_backend_dir(self):
        ops = self._ops(port=8110)
        paths = ops._paths()
        env = ops._child_env(paths)
        for key, expected in (("CHAT2API_TOKEN_FILE", paths["token"]),
                              ("CHAT2API_SERVICE_LOG", paths["log"]),
                              ("CHAT2API_USAGE_LOG", paths["ledger"]),
                              ("CHAT2API_CONTROL_TOKEN_FILE",
                               paths["control_token"])):
            self.assertEqual(env.get(key), expected)
            self.assertTrue(expected.startswith(paths["dir"] + os.sep))
        # 归因密钥不是私有状态文件：它与 8100 侧指向同一个共享文件，
        # 因此既不在专用后端目录内，也必须与 _paths() 解析出的键一致。
        self.assertEqual(env.get(prod.BACKEND_ATTRIBUTION_KEY_ENV),
                         paths["attribution_key"])
        self.assertEqual(paths["attribution_key"], self.attribution_key)
        self.assertFalse(paths["attribution_key"].startswith(
            paths["dir"] + os.sep))

    # ---- stop ------------------------------------------------------------

    def test_stop_pid_match_graceful_shutdown_confirmed(self):
        ops = self._ops(port=8110)
        paths = self._write_pid(ops, 4242)
        graceful_calls = []

        def graceful(paths_dict):
            graceful_calls.append(dict(paths_dict))

        with mock.patch.object(ops, "_pid_matches", new=lambda p, pid: True), \
                mock.patch.object(ops, "_graceful_shutdown", new=graceful), \
                mock.patch.object(ops, "_pid_alive", new=lambda pid: False):
            self.assertIs(ops.stop(), True)
        self.assertEqual(len(graceful_calls), 1)
        self.assertEqual(graceful_calls[0]["port"], 8110)
        self.assertFalse(os.path.exists(paths["pid"]))   # forgotten
        self._assert_zero_real_spawn()   # no tasklist/wmic/netstat/taskkill

    def test_stop_falls_back_to_stopper_after_deadline(self):
        ops = self._ops(port=8110, stopper=lambda pid: stopper_calls.append(pid))
        paths = self._write_pid(ops, 4242)
        stopper_calls = []
        clock = _FakeClock()
        with mock.patch.object(ops, "_pid_matches", new=lambda p, pid: True), \
                mock.patch.object(ops, "_graceful_shutdown",
                                  new=lambda p: False), \
                mock.patch.object(ops, "_pid_alive",
                                  new=lambda pid: clock.t < 10.0), \
                mock.patch.object(prod.time, "monotonic", clock.monotonic), \
                mock.patch.object(prod.time, "sleep", clock.sleep):
            self.assertIs(ops.stop(), True)
        self.assertEqual(stopper_calls, [4242])   # stopper seam got the pid
        self.assertFalse(os.path.exists(paths["pid"]))
        self._assert_zero_real_spawn()

    def test_stop_pid_mismatch_refuses_to_act_and_forgets(self):
        # Registered PID no longer runs the dedicated-backend command line:
        # refuse to act on it (never kill by bare PID) — no taskkill/stopper,
        # the pid file is dropped and the port state decides the verdict.
        ops = self._ops(port=8110, stopper=lambda pid: stopper_calls.append(pid))
        paths = self._write_pid(ops, 4242)
        stopper_calls = []
        with mock.patch.object(ops, "_pid_matches", new=lambda p, pid: False), \
                mock.patch.object(ops, "_port_listening", new=lambda p: True):
            self.assertIs(ops.stop(), False)   # port still listening: residual
        self.assertEqual(stopper_calls, [])
        self.assertFalse(os.path.exists(paths["pid"]))
        self._assert_zero_real_spawn()

    def test_stop_without_pid_file_follows_port_state(self):
        ops = self._ops(port=8110, stopper=lambda pid: stopper_calls.append(pid))
        stopper_calls = []
        with mock.patch.object(ops, "_port_listening", new=lambda p: False):
            self.assertIs(ops.stop(), True)    # port not listening -> closed
        with mock.patch.object(ops, "_port_listening", new=lambda p: True):
            self.assertIs(ops.stop(), False)   # port listening -> residual
        self.assertEqual(stopper_calls, [])
        self._assert_zero_real_spawn()

    # ---- verify ----------------------------------------------------------

    def test_verify_running_refuses_on_pid_mismatch(self):
        # Registered PID no longer matches the command line: forget the pid
        # and report NOT running (never fabricate a running verdict).
        ops = self._ops(port=8110)
        paths = self._write_pid(ops, 4242)
        with mock.patch.object(ops, "_pid_matches", new=lambda p, pid: False), \
                mock.patch.object(ops, "_port_listening",
                                  new=lambda p: True):
            self.assertIs(ops.verify_running(), False)
        self.assertFalse(os.path.exists(paths["pid"]))
        self._assert_zero_real_spawn()


class BackendObserveTests(unittest.TestCase):
    """production_backend_observe（只读 /health + auth 观察的编排缝）：
    running 缝打补丁、auth 观察经假 urlopen，断言零真实网络调用。
    8100 运行实例探测同样替身化：本套件不查真实进程。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        # 归因密钥是部署事实（默认在 ProgramData 的 RunRoot 下）：测试里把
        # RunRoot 重定向到临时目录，观察缝因此确定性可读，且绝不碰真实部署
        # 文件（真实密钥文件由维护者按 SHARING.md 生成）。
        env = mock.patch.dict(os.environ, {
            "CONSOLE_SHARE_BACKEND_DIR": self._tmp.name,
            prod.SHARE_RUN_ROOT_ENV: self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        # 8100 探测：默认「查询成功但无人监听」——密钥文件缺失时本就不该
        # 探测进程（没有 mtime 可比），记录调用次数以证明它没有被调用。
        self.gateway_probe_calls = []

        def fake_probe(*args, **kwargs):
            self.gateway_probe_calls.append((args, kwargs))
            return _gateway_probe(pid=None, reason="not_listening")

        patcher = mock.patch.multiple(
            prod, share_entry_instance_probe=fake_probe,
            _backend_health_snapshot=lambda paths, opener=None: (False, None))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.urlopen_calls = []

    def _urlopen_json(self, payload):
        def fake_urlopen(req, timeout=None):
            self.urlopen_calls.append(req)
            return _FakeHttpResponse(json.dumps(payload).encode("utf-8"))
        return fake_urlopen

    def test_not_running_reports_honest_observation(self):
        ops = prod._ProductionBackendOps(port=8110)
        with mock.patch.object(prod, "production_backend_ops",
                               return_value=ops), \
                mock.patch.object(ops, "verify_running", return_value=False):
            out = prod.production_backend_observe()
        self.assertEqual(sorted(out), ["attribution", "auth_error", "auth_evidence", "auth_gate", "auth_label", "auth_recovery", "auth_state", "health_reachable", "observed_at", "running", "upstream_reachability"])
        self.assertEqual(out["running"], False)
        self.assertIsNone(out["auth_state"])
        self.assertIsNone(out["auth_label"])
        # 密钥缺失 -> 文件事实如实报「未就绪」，绝不报已接线；进程没跑也照样
        # 观察（文件/端口事实不是本进程的事实）。运行实例是否载入是另一件事：
        # 文件未就绪时没有 mtime 可比 -> unknown，且探测不被调用。
        self.assertEqual(out["attribution"], {
            "file_ready": False, "file_reason": "missing",
            "key_file": prod.share_attribution_key_path(),
            "key_chars": None,
            "instance_loaded": {
                "value": "unknown", "basis": "inference",
                "rule": prod._INSTANCE_LOADED_RULE,
                "reason": "key_file_not_ready",
                "gateway_pid": None, "gateway_started_at": None,
                "key_file_mtime": None},
            "request_attribution_observed": False,
            "request_attribution_detail": {
                "verdict": "not_observed_here",
                "runtime_fact": prod._LEDGER_ATTRIBUTION_FACT,
                "ledger_file": ops._paths()["ledger"]}})
        self.assertEqual(self.urlopen_calls, [])   # auth never probed
        # 文件未就绪时不做无意义的进程查询：一次只读查询也不多跑。
        self.assertEqual(self.gateway_probe_calls, [])

    def test_running_maps_observed_auth_state(self):
        ops = prod._ProductionBackendOps(port=8110)
        with mock.patch.object(prod, "production_backend_ops",
                               return_value=ops), \
                mock.patch.object(ops, "verify_running", return_value=True), \
                mock.patch.object(prod, "_auth_status_payload", return_value={
                    "state": "valid", "label": "认证有效"}):
            out = prod.production_backend_observe()
        self.assertEqual(out["running"], True)
        self.assertEqual(out["auth_state"], "valid")
        self.assertEqual(out["auth_label"], "认证有效")
        self.assertEqual(out["attribution"]["file_ready"], False)
        self.assertEqual(out["attribution"]["file_reason"], "missing")

    def test_running_reads_the_instances_own_reason(self):
        # Plan-Revision 13：实例自报原因（人工接手/跨实例拒绝）经
        # auth/status 的 error 字段进入专用实例的标签，观察缝原样透出。
        ops = prod._ProductionBackendOps(port=8110)
        paths = ops._paths()
        Path(paths["control_token"]).write_text("ctrl-token", encoding="utf-8")
        payload = {"state": "failed", "label": "登录失败",
                   "error": "manual_takeover:umpass_prefill_missing"}
        with mock.patch.object(prod, "production_backend_ops",
                               return_value=ops), \
                mock.patch.object(ops, "verify_running", return_value=True), \
                mock.patch("console_share_production.urllib.request.urlopen",
                           self._urlopen_json(payload)):
            out = prod.production_backend_observe()
        self.assertEqual(out["auth_state"], "failed")
        self.assertEqual(out["auth_label"], "登录失败")

    def test_running_reads_auth_status_with_dedicated_token(self):
        # 真实观察路径：control-token 文件 + 注入的假 urlopen —— 请求带
        # 专用控制头、打到专用端口，全程零真实网络。
        ops = prod._ProductionBackendOps(port=8110)
        paths = ops._paths()
        Path(paths["control_token"]).write_text("ctrl-token", encoding="utf-8")
        with mock.patch.object(prod, "production_backend_ops",
                               return_value=ops), \
                mock.patch.object(ops, "verify_running", return_value=True), \
                mock.patch("console_share_production.urllib.request.urlopen",
                           self._urlopen_json({"state": "valid",
                                               "label": "认证有效"})):
            out = prod.production_backend_observe()
        self.assertEqual(out["running"], True)
        self.assertEqual(out["auth_state"], "valid")
        self.assertEqual(out["auth_label"], "认证有效")
        req = self.urlopen_calls[0]
        self.assertIn(":8110/admin/auth/status", req.full_url)
        self.assertEqual(req.headers.get("X-admin-control"), "ctrl-token")
        self.assertEqual(len(self.urlopen_calls), 1)   # zero real network

    def test_unreadable_auth_status_is_unknown_never_valid(self):
        ops = prod._ProductionBackendOps(port=8110)
        with mock.patch.object(prod, "production_backend_ops",
                               return_value=ops), \
                mock.patch.object(ops, "verify_running", return_value=True):
            out = prod.production_backend_observe()
        self.assertEqual(out["auth_state"], "unknown")   # no token file
        self.assertIsNone(out["auth_label"])


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


class BackendLoginFlowTests(unittest.TestCase):
    """production_backend_login（控制台「登录分享专用后端」）：专用端口 +
    专用控制秘密触发 POST /admin/auth/login（后端自开独立 profile 浏览器）。
    全隔离：tempfile backend 目录 + 注入假 urlopen + 假 ops；断言
    (a) 全部请求只打专用端口（无任何 8000 访问）；(b) 控制秘密零回显；
    (c) 启动-恢复语义（本流启动、失败恢复到未运行）；(d) token 缺席
    fail-closed 且零 HTTP。"""

    TOKEN = "unit-login-control-secret-9f3a"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(
            os.environ, {"CONSOLE_SHARE_BACKEND_DIR": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.ops_calls = []
        Path(self._tmp.name, "control-token").write_text(
            self.TOKEN, encoding="utf-8")

    def _ops(self, running_seq):
        ops = prod._ProductionBackendOps(port=8110)
        self._running = list(running_seq)

        def rec(name, fn):
            def wrapped(*args, **kwargs):
                self.ops_calls.append(name)
                return fn(*args, **kwargs)
            return wrapped

        ops.start = rec("start", lambda: True)
        ops.stop = rec("stop", lambda: True)
        ops.verify_running = rec(
            "verify_running",
            lambda: self._running.pop(0) if self._running else False)
        return ops

    def _opener(self, script):
        """script: list of (status, body-bytes) or Exception; records urls."""
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            outcome = script.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            status, body = outcome
            return _FakeHttpResponse(body, status=status)

        fake_urlopen.calls = calls
        return fake_urlopen

    def test_01_already_valid_is_noop_no_trigger(self):
        opener = self._opener([(200, json.dumps(
            {"state": "valid", "label": "认证有效"}).encode("utf-8"))])
        ops = self._ops([True])
        out = prod.production_backend_login(urlopen_fn=opener, ops=ops)
        self.assertEqual(out["triggered"], False)
        self.assertEqual(out["state"], "valid")
        self.assertIsNone(out["error"])
        self.assertIsNone(out["recovery"])
        self.assertFalse(out["started_here"])
        self.assertEqual(self.ops_calls, ["verify_running"])
        # exactly ONE status request, on the dedicated port only
        self.assertEqual(len(opener.calls), 1)
        self.assertIn(":8110/admin/auth/status", opener.calls[0].full_url)

    def test_02_triggers_login_and_reports_in_progress(self):
        opener = self._opener([
            (200, json.dumps({"state": "needs_login",
                              "label": "需要登录"}).encode("utf-8")),
            (202, json.dumps({"state": "logging_in",
                              "label": "登录中"}).encode("utf-8")),
            (200, json.dumps({"state": "logging_in",
                              "label": "登录中"}).encode("utf-8")),
        ])
        ops = self._ops([True])
        out = prod.production_backend_login(
            urlopen_fn=opener, ops=ops, poll_interval_s=0.01,
            poll_attempts=3)
        self.assertTrue(out["triggered"])
        self.assertEqual(out["state"], "logging_in")
        self.assertIsNone(out["error"])
        self.assertEqual(out["recovery"]["action"], "wait")
        self.assertFalse(out["started_here"])   # was running already
        self.assertIsNone(out["restored"])
        posts = [c for c in opener.calls if c.get_method() == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertIn(":8110/admin/auth/login", posts[0].full_url)
        headers = {k.lower(): v for k, v in posts[0].headers.items()}
        self.assertEqual(headers.get("x-admin-control"), self.TOKEN)
        # every request stayed on the dedicated port — never 8000
        for c in opener.calls:
            self.assertNotIn(":8000", c.full_url)
        self.assertEqual(self.ops_calls, ["verify_running"])

    def test_03_starts_instance_when_not_running_keeps_on_in_progress(self):
        opener = self._opener([
            (200, b'{"status": "ok"}'),   # cold-start readiness gate /health
            (200, json.dumps({"state": "needs_login",
                              "label": "需要登录"}).encode("utf-8")),
            (202, b""),
            (200, json.dumps({"state": "logging_in",
                              "label": "登录中"}).encode("utf-8")),
        ])
        ops = self._ops([False])
        out = prod.production_backend_login(
            urlopen_fn=opener, ops=ops, poll_interval_s=0.01,
            poll_attempts=2)
        self.assertTrue(out["started_here"])
        self.assertTrue(out["triggered"])
        self.assertIsNone(out["restored"])   # in-progress: no undo
        self.assertEqual(self.ops_calls, ["verify_running", "start"])

    def test_04_trigger_refused_restores_started_instance(self):
        opener = self._opener([
            (200, b'{"status": "ok"}'),   # cold-start readiness gate /health
            (200, json.dumps({"state": "needs_login"}).encode("utf-8")),
            (403, b'{"error":"forbidden"}'),
        ])
        ops = self._ops([False])
        with self.assertRaises(Exception) as ctx:
            prod.production_backend_login(
                urlopen_fn=opener, ops=ops, poll_interval_s=0.01)
        self.assertIn("登录触发被拒", ctx.exception.message)
        self.assertIn("已按原始状态恢复", ctx.exception.message)
        self.assertNotIn(self.TOKEN, ctx.exception.message)
        self.assertEqual(self.ops_calls,
                         ["verify_running", "start", "stop",
                          "verify_running"])

    def test_05_missing_token_fails_closed_zero_http(self):
        Path(self._tmp.name, "control-token").unlink()
        opener = self._opener([])
        ops = self._ops([False])
        with self.assertRaises(Exception) as ctx:
            prod.production_backend_login(urlopen_fn=opener, ops=ops)
        self.assertIn("控制令牌缺失", ctx.exception.message)
        self.assertEqual(opener.calls, [])   # zero HTTP attempts
        self.assertEqual(self.ops_calls, [])  # never even probed running

    def test_05b_child_env_points_the_reauth_slot_at_the_writable_backend_dir(self):
        # D15-4: on POSIX the 8110 child must coordinate reauthentication
        # in a PERSISTENT WRITABLE path. The release data dir is read-only
        # under the console unit's ProtectSystem=strict, so the slot lives in
        # the console's own backend state dir (ReadWritePaths).
        ops = prod._ProductionBackendOps(port=8110)
        paths = ops._paths()
        self.assertEqual(Path(paths["reauth_slot"]).parent,
                         Path(self._tmp.name))
        self.assertNotEqual(Path(paths["reauth_slot"]).parent,
                            Path(prod.__file__).resolve().parent / "data")
        child_env = ops._child_env(paths)
        # Windows local keeps the historical shared default (the private 8000
        # chain shares that slot) - no override. POSIX must override it.
        if os.name == "posix":
            self.assertEqual(child_env["CHAT2API_REAUTH_SLOT_FILE"],
                             paths["reauth_slot"])
            opposite = "nt"
        else:
            self.assertNotIn("CHAT2API_REAUTH_SLOT_FILE", child_env)
            opposite = "posix"
        with mock.patch.object(prod.os, "name", opposite):
            other_env = ops._child_env(paths)
        self.assertEqual("CHAT2API_REAUTH_SLOT_FILE" in other_env,
                         opposite == "posix")

    def test_06_terminal_failed_state_restores_and_raises(self):
        opener = self._opener([
            (200, json.dumps({"state": "needs_login"}).encode("utf-8")),
            (202, b""),
            (200, json.dumps({"state": "failed",
                              "label": "登录失败"}).encode("utf-8")),
        ])
        ops = self._ops([True])
        with self.assertRaises(Exception) as ctx:
            prod.production_backend_login(
                urlopen_fn=opener, ops=ops, poll_interval_s=0.01)
        self.assertIn("登录失败", ctx.exception.message)
        self.assertNotIn(self.TOKEN, ctx.exception.message)
        self.assertEqual(self.ops_calls, ["verify_running"])

    def test_06b_manual_takeover_never_stops_and_never_judges_the_credential(self):
        # 人工接管不是凭据失败：实例必须保持运行，结果如实点名原因，
        # 且 recovery 指向人工处理，绝不把健康进程停掉。
        opener = self._opener([
            (200, json.dumps({"state": "needs_login",
                              "label": "需要登录"}).encode("utf-8")),
            (202, b""),
            (200, json.dumps(
                {"state": "failed", "label": "登录失败",
                 "error": "manual_takeover:umpass_prefill_missing"}
            ).encode("utf-8")),
        ])
        ops = self._ops([True])
        out = prod.production_backend_login(
            urlopen_fn=opener, ops=ops, poll_interval_s=0.01)
        self.assertTrue(out["triggered"])
        self.assertEqual(out["state"], "failed")
        self.assertEqual(out["error"],
                         "manual_takeover:umpass_prefill_missing")
        self.assertEqual(out["recovery"]["action"], "human")
        self.assertIn("人工接管", out["note"])
        self.assertIn("未判凭据失败", out["note"])
        self.assertEqual(self.ops_calls, ["verify_running"])   # no stop

    def test_06b2_superseded_never_stops_and_never_judges_the_credential(self):
        opener = self._opener([
            (200, json.dumps({"state": "needs_login",
                              "label": "需要登录"}).encode("utf-8")),
            (202, b""),
            (200, json.dumps(
                {"state": "failed", "label": "登录失败",
                 "error": "superseded"}).encode("utf-8")),
        ])
        ops = self._ops([True])
        out = prod.production_backend_login(
            urlopen_fn=opener, ops=ops, poll_interval_s=0.01)
        self.assertEqual(out["state"], "failed")
        self.assertEqual(out["error"], "superseded")
        self.assertEqual(out["recovery"]["action"], "refresh")
        self.assertIn("已被新流程取代", out["note"])
        self.assertIn("未判凭据失败", out["note"])
        self.assertEqual(self.ops_calls, ["verify_running"])   # no stop

    def test_06b3_browser_disabled_never_stops_and_never_judges_the_credential(self):
        opener = self._opener([
            (200, json.dumps({"state": "needs_login",
                              "label": "需要登录"}).encode("utf-8")),
            (202, b""),
            (200, json.dumps(
                {"state": "failed", "label": "登录失败",
                 "error": "BrowserDisabled"}).encode("utf-8")),
        ])
        ops = self._ops([True])
        out = prod.production_backend_login(
            urlopen_fn=opener, ops=ops, poll_interval_s=0.01)
        self.assertEqual(out["state"], "failed")
        self.assertEqual(out["error"], "BrowserDisabled")
        self.assertEqual(out["recovery"]["action"], "operator")
        self.assertIn("浏览器不可用", out["note"])
        self.assertIn("未判凭据失败", out["note"])
        self.assertEqual(self.ops_calls, ["verify_running"])   # no stop

    def test_06b4_true_credential_failure_still_restores(self):
        opener = self._opener([
            (200, b'{"status": "ok"}'),   # cold-start readiness gate /health
            (200, json.dumps({"state": "needs_login",
                              "label": "需要登录"}).encode("utf-8")),
            (202, b""),
            (200, json.dumps(
                {"state": "failed", "label": "登录失败",
                 "error": "invalid_credentials"}).encode("utf-8")),
        ])
        ops = self._ops([False])
        with self.assertRaises(Exception) as ctx:
            prod.production_backend_login(
                urlopen_fn=opener, ops=ops, poll_interval_s=0.01)
        self.assertIn("登录失败", ctx.exception.message)
        self.assertEqual(self.ops_calls,
                         ["verify_running", "start", "stop", "verify_running"])

    def test_06d_refusal_is_reported_in_the_common_needs_login_state(self):
        # F2：拒绝时 refresh() 只设 _auth_error、状态停在 needs_login，因此必须
        # 按「拒绝」上报，而不是「登录已受理、必要时再次触发」。
        opener = self._opener([
            (200, json.dumps({"state": "needs_login",
                              "label": "需要登录"}).encode("utf-8")),
            (202, b""),
            (200, json.dumps(
                {"state": "needs_login", "label": "需要登录",
                 "error": "reauth_refused:global_session_active",
                 "recovery": {"state": "waiting",
                              "next_attempt_epoch": 1234.5}
            }).encode("utf-8")),
        ])
        ops = self._ops([True])
        out = prod.production_backend_login(
            urlopen_fn=opener, ops=ops, poll_interval_s=0.01)
        self.assertTrue(out["triggered"])
        self.assertIn("未开启新的提交窗口", out["note"])
        self.assertNotIn("必要时再次触发", out["note"])
        # A1: a refused NEW session is not an authentication failure. The
        # machine-readable fields say so without parsing the note.
        self.assertEqual(out["state"], "needs_login")
        self.assertEqual(out["error"], "reauth_refused:global_session_active")
        self.assertEqual(out["recovery"]["action"], "wait")
        self.assertEqual(out["recovery"]["next_allowed_epoch"], 1234.5)
        self.assertEqual(self.ops_calls, ["verify_running"])   # no stop

    def test_06e_permanent_corruption_advice_names_the_file(self):
        # F4：损坏状态是永久拒绝，重试永远无效——提示必须点名要清理的文件，
        # 而不是让人反复点「再次触发」。
        opener = self._opener([
            (200, json.dumps({"state": "needs_login",
                              "label": "需要登录"}).encode("utf-8")),
            (202, b""),
            (200, json.dumps(
                {"state": "needs_login", "label": "需要登录",
                 "error": "reauth_refused:slot_corrupt"}
            ).encode("utf-8")),
        ])
        ops = self._ops([True])
        out = prod.production_backend_login(
            urlopen_fn=opener, ops=ops, poll_interval_s=0.01)
        self.assertIn("chat2api-reauth.state", out["note"])
        self.assertIn("专用后端", out["note"])
        self.assertNotIn("data/", out["note"])
        self.assertNotIn("请稍后重试", out["note"])
        self.assertEqual(self.ops_calls, ["verify_running"])   # no stop

    def test_06c_refused_session_never_stops_a_running_instance(self):
        # 跨实例拒绝不是登录失败：本次会话根本没开始，实例必须保持运行，
        # 结果如实点名拒绝原因，绝不把健康进程停掉。
        opener = self._opener([
            (200, json.dumps({"state": "needs_login",
                              "label": "需要登录"}).encode("utf-8")),
            (202, b""),
            (200, json.dumps(
                {"state": "failed", "label": "登录失败",
                 "error": "reauth_refused:global_session_active",
                 "recovery": {"state": "exhausted",
                              "next_attempt_epoch": None,
                              "pause_reason":
                                  "reauth_refused:global_session_active"}
            }).encode("utf-8")),
        ])
        ops = self._ops([True])
        out = prod.production_backend_login(
            urlopen_fn=opener, ops=ops, poll_interval_s=0.01)
        self.assertTrue(out["triggered"])
        self.assertEqual(out["state"], "failed")
        self.assertIn("reauth_refused:global_session_active", out["note"])
        self.assertEqual(out["error"], "reauth_refused:global_session_active")
        # A refused NEW session is a wait, not a credential failure and not
        # an operator cleanup (the slot is readable).
        self.assertEqual(out["recovery"]["action"], "wait")
        self.assertEqual(self.ops_calls, ["verify_running"])   # no stop

    def test_07_delayed_readiness_triggers_login_exactly_once(self):
        # 冷启动就绪门：/health 前 2 次连拒后 200 —— 就绪之后才提交登录；
        # POST /admin/auth/login 恰好 1 次，实例保持运行（无需恢复）。
        opener = self._opener([
            prod.urllib.error.URLError("cold start: conn refused"),
            prod.urllib.error.URLError("cold start: conn refused"),
            (200, b'{"status": "ok"}'),   # third /health probe -> ready
            (200, json.dumps({"state": "needs_login",
                              "label": "需要登录"}).encode("utf-8")),
            (202, b""),
            (200, json.dumps({"state": "logging_in",
                              "label": "登录中"}).encode("utf-8")),
        ])
        ops = self._ops([False])
        out = prod.production_backend_login(
            urlopen_fn=opener, ops=ops, poll_interval_s=0.01,
            poll_attempts=2, ready_interval_s=0.01)
        self.assertTrue(out["started_here"])
        self.assertTrue(out["triggered"])
        self.assertEqual(out["state"], "logging_in")
        posts = [c for c in opener.calls if c.get_method() == "POST"]
        self.assertEqual(len(posts), 1)   # exactly one trigger, after ready
        self.assertIn(":8110/admin/auth/login", posts[0].full_url)
        health = [c for c in opener.calls
                  if c.full_url.endswith("/health")]
        self.assertEqual(len(health), 3)  # gate consumed exactly the probes
        self.assertEqual(self.ops_calls, ["verify_running", "start"])

    def test_08_never_ready_restores_and_never_triggers(self):
        # /health 整个就绪窗全未就绪：不提交登录、恢复刚启动的实例；
        # 报错文案如实点名冷启动未就绪与已恢复，POST 计数为 0。
        opener = self._opener([
            prod.urllib.error.URLError("cold start: conn refused"),
            prod.urllib.error.URLError("cold start: conn refused"),
            prod.urllib.error.URLError("cold start: conn refused"),
        ])
        ops = self._ops([False, False])
        with self.assertRaises(Exception) as ctx:
            prod.production_backend_login(urlopen_fn=opener, ops=ops,
                                          ready_interval_s=0.01,
                                          ready_attempts=3)
        self.assertIn("冷启动未就绪", ctx.exception.message)
        self.assertIn("已按原始状态恢复", ctx.exception.message)
        posts = [c for c in opener.calls if c.get_method() == "POST"]
        self.assertEqual(posts, [])   # login NEVER triggered
        self.assertTrue(all(c.full_url.endswith("/health")
                            for c in opener.calls))
        self.assertEqual(self.ops_calls,
                         ["verify_running", "start", "stop",
                          "verify_running"])

    def test_09_status_unreachable_never_claims_browser_waiting(self):
        # 触发 202 受理但状态接口整个窗口不可达：结果 state=unknown，
        # 文案承认「已受理、未确认」，绝不冒充「等待人工/浏览器已弹出」。
        opener = self._opener([
            prod.urllib.error.URLError("status endpoint unreachable"),
            (202, b""),
            prod.urllib.error.URLError("status endpoint unreachable"),
            prod.urllib.error.URLError("status endpoint unreachable"),
            prod.urllib.error.URLError("status endpoint unreachable"),
        ])
        ops = self._ops([True])
        out = prod.production_backend_login(urlopen_fn=opener, ops=ops,
                                            poll_interval_s=0.01,
                                            poll_attempts=3)
        self.assertTrue(out["triggered"])
        self.assertEqual(out["state"], "unknown")
        self.assertEqual(out["error"], "auth_status_unreadable")
        self.assertEqual(out["recovery"]["action"], "refresh")
        self.assertIn("登录触发已受理", out["note"])
        self.assertIn("未确认", out["note"])
        self.assertNotIn("等待", out["note"])
        self.assertNotIn("浏览器", out["note"])
        posts = [c for c in opener.calls if c.get_method() == "POST"]
        self.assertEqual(len(posts), 1)   # trigger WAS accepted (202)
        self.assertEqual(self.ops_calls, ["verify_running"])

    def test_10_needs_login_at_window_end_is_not_browser_waiting(self):
        # 窗口结束时实际观察到 needs_login（不是 logging_in）：文案说明
        # 尚未观察到登录会话，不得沿用「请在本机浏览器完成」的话术。
        needs = (200, json.dumps({"state": "needs_login",
                                  "label": "需要登录"}).encode("utf-8"))
        opener = self._opener([
            needs,   # pre-trigger status
            (202, b""),
            needs,   # poll window: still needs_login at the end
            needs,
        ])
        ops = self._ops([True])
        out = prod.production_backend_login(urlopen_fn=opener, ops=ops,
                                            poll_interval_s=0.01,
                                            poll_attempts=2)
        self.assertTrue(out["triggered"])
        self.assertEqual(out["state"], "needs_login")
        self.assertIn("尚未观察到登录会话", out["note"])
        self.assertNotIn("请在本机浏览器完成", out["note"])
        posts = [c for c in opener.calls if c.get_method() == "POST"]
        self.assertEqual(len(posts), 1)


class BackendLoginCancelTests(unittest.TestCase):
    """D18-8: cancel the current login round WITHOUT stopping the instance.

    The control-token-gated POST /admin/auth/cancel ends the round and closes
    the browser context; the dedicated 8110 process keeps running, no restart
    happens, and ordinary requests never reopen the round.
    """

    TOKEN = "unit-cancel-control-secret-4c1e"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        Path(self._tmp.name, "control-token").write_text(
            self.TOKEN, encoding="utf-8")
        self.ops_calls = []

    def _ops(self):
        paths = {"port": 8110,
                 "control_token": str(Path(self._tmp.name, "control-token"))}
        calls = self.ops_calls

        class _Ops:
            def _paths(self):
                return paths

            def stop(self):
                calls.append("stop")
                raise AssertionError("cancel must never stop the service")

            def start(self):
                calls.append("start")
                raise AssertionError("cancel must never start the service")

        return _Ops()

    def _opener(self, outcome):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            if isinstance(outcome, Exception):
                raise outcome
            status, body = outcome
            return _FakeHttpResponse(body, status=status)

        fake_urlopen.calls = calls
        return fake_urlopen

    def test_01_cancel_posts_to_the_dedicated_port_and_never_stops(self):
        opener = self._opener((200, json.dumps({
            "cancelled": True, "state": "cancelled",
            "detail": None}).encode("utf-8")))
        out = prod.backend_login_cancel(urlopen_fn=opener, ops=self._ops())
        self.assertEqual(out, {"cancelled": True, "state": "cancelled",
                             "detail": None})
        self.assertEqual(len(opener.calls), 1)
        request = opener.calls[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertIn(":8110/admin/auth/cancel", request.full_url)
        headers = {k.lower(): v for k, v in request.headers.items()}
        self.assertEqual(headers.get("x-admin-control"), self.TOKEN)
        self.assertEqual(self.ops_calls, [])

    def test_02_no_login_in_progress_is_reported_honestly(self):
        opener = self._opener((200, json.dumps({
            "cancelled": False, "state": "no_login_in_progress",
            "detail": "当前没有进行中的登录"}).encode("utf-8")))
        out = prod.backend_login_cancel(urlopen_fn=opener, ops=self._ops())
        self.assertFalse(out["cancelled"])
        self.assertEqual(out["state"], "no_login_in_progress")
        self.assertEqual(out["detail"], "当前没有进行中的登录")
        self.assertEqual(self.ops_calls, [])

    def test_03_missing_control_token_fails_closed_with_zero_http(self):
        Path(self._tmp.name, "control-token").unlink()
        opener = self._opener((200, b"{}"))
        with self.assertRaises(Exception) as ctx:
            prod.backend_login_cancel(urlopen_fn=opener, ops=self._ops())
        self.assertIn("控制令牌缺失", ctx.exception.message)
        self.assertEqual(opener.calls, [])
        self.assertEqual(self.ops_calls, [])

    def test_04_rejection_never_echoes_the_secret_or_stops(self):
        opener = self._opener((403, b'{"error":"forbidden"}'))
        with self.assertRaises(Exception) as ctx:
            prod.backend_login_cancel(urlopen_fn=opener, ops=self._ops())
        self.assertIn("登录取消被拒", ctx.exception.message)
        self.assertNotIn(self.TOKEN, ctx.exception.message)
        self.assertEqual(self.ops_calls, [])

    def test_05_transport_failure_is_honest_and_never_stops(self):
        opener = self._opener(OSError("connection refused"))
        with self.assertRaises(Exception) as ctx:
            prod.backend_login_cancel(urlopen_fn=opener, ops=self._ops())
        self.assertIn("登录取消失败", ctx.exception.message)
        self.assertIn("未停止服务", ctx.exception.message)
        self.assertEqual(self.ops_calls, [])


@unittest.skip("share attribution HMAC retired with the merged share process")
class ShareAttributionWiringTests(unittest.TestCase):
    """归因密钥两端接线（2026-09-26）：8100 的 $liveEnv 与 8110 的
    _child_env 必须指向同一个文件；状态读面把三件事分开报（文件就绪 /
    运行实例是否载入 / 请求是否已验证归属），文件缺失/过短/不可读时报
    「文件未就绪」而不是报已接线，实例查不到时报 unknown 而不是已载入。

    8100 侧只解析部署脚本（不执行、不部署、不重启），8110 侧只调解析函数；
    两侧路径一致由同一常量（密钥文件名、默认 RunRoot）与同一解析函数表达，
    所以任何一侧改了字面量都会在这里失败，而不是悄悄指向两个文件。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        # 专用后端目录与 RunRoot 都重定向到临时目录：本类不写真实部署
        # 文件、不跑真实进程（真实密钥文件由维护者按 SHARING.md
        # 生成、可能已存在）；「两端同一路径」用例只对默认 RunRoot 做路径
        # 核对，并替身化 8100 实例探针，绝不跑真实 netstat/CIM。
        self.backend_dir = os.path.join(self._tmp.name, "backend")
        self.run_root = os.path.join(self._tmp.name, "runroot")
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in (prod.SHARE_RUN_ROOT_ENV,
                     prod.SHARE_ATTRIBUTION_KEY_FILE_ENV):
            os.environ.pop(name, None)
        os.environ["CONSOLE_SHARE_BACKEND_DIR"] = self.backend_dir
        os.environ[prod.SHARE_RUN_ROOT_ENV] = self.run_root

    def _deploy_text(self):
        return _DEPLOY_SCRIPT.read_text(encoding="utf-8")

    def _key_path(self):
        return os.path.join(self.run_root, "run",
                            prod.SHARE_ATTRIBUTION_KEY_NAME)

    def test_both_ends_resolve_the_same_key_file(self):
        text = self._deploy_text()
        # 8100 侧：$liveEnv 里恰好一条独立的 set，且不拼进 UM_SHARE_ARGS
        # （D4 实验路径按该串逐字符比对，加参数会让它拒绝启动）。
        lines = [line for line in text.splitlines()
                 if "SHARE_ATTRIBUTION_KEY_FILE=" in line]
        self.assertEqual(len(lines), 1, lines)
        live_env_line = lines[0]
        self.assertNotIn("UM_SHARE_ARGS", live_env_line)
        self.assertIn("(Get-AttributionKeyPath)", live_env_line)
        # 8100 侧注入的环境变量名就是 share_entry 真正读的那个名字。
        gateway_env_name = _literal_assignment(_SHARE_GATEWAY_SOURCE,
                                              "ATTRIBUTION_KEY_ENV")
        self.assertIn('set "%s=' % gateway_env_name, live_env_line)
        # 两侧同一常量：密钥文件名 + 默认 RunRoot（部署脚本的参数默认值）。
        self.assertEqual(_ps_literal(text, "AttributionKeyName"),
                         prod.SHARE_ATTRIBUTION_KEY_NAME)
        run_root = re.search(r"\[string\]\$RunRoot = '([^']+)'", text)
        self.assertIsNotNone(run_root, "deploy-elevated.ps1 未见 -RunRoot 默认值")
        self.assertEqual(run_root.group(1), prod.DEFAULT_SHARE_RUN_ROOT)
        # 8100 侧的路径由 $RunRoot + run/ + 同一文件名派生（同一解析形状）。
        body = re.search(r"function Get-AttributionKeyPath \{(.*?)\n\}",
                         text, re.S).group(1)
        self.assertIn("Join-Path $RunRoot 'run'", body)
        self.assertIn("$AttributionKeyName", body)
        expected = os.path.join(run_root.group(1), "run",
                                prod.SHARE_ATTRIBUTION_KEY_NAME)
        # 8110 侧：_paths()/_child_env() 解析到同一个文件。
        with mock.patch.dict(os.environ,
                              {prod.SHARE_RUN_ROOT_ENV: run_root.group(1)}):
            ops = prod._ProductionBackendOps(port=8110)
            paths = ops._paths()
            child_env = ops._child_env(paths)
        self.assertEqual(paths["attribution_key"], expected)
        self.assertEqual(child_env[prod.BACKEND_ATTRIBUTION_KEY_ENV], expected)
        # 状态读面读的也是同一个文件（同一解析函数，不是第二份字面量）。
        # 默认 RunRoot 下的真实密钥文件在本机可能存在（维护者已按 SHARING.md
        # 生成）：8100 实例探针一律替身化，读面结果与「本机是否有密钥」无关，
        # 也绝不跑真实 netstat/CIM 查询。
        self.assertEqual(prod.share_attribution_status(
            paths["attribution_key"],
            gateway_probe=lambda: _gateway_probe(pid=None,
                                                reason="not_listening")
        )["key_file"], expected)
        # 8110 侧真正读的环境变量名与最小长度就是 8100 侧那两个模块的契约。
        self.assertIn(prod.BACKEND_ATTRIBUTION_KEY_ENV,
                      _CHAT2API_SOURCE.read_text(encoding="utf-8"))
        for module in (_SHARE_GATEWAY_SOURCE, _CHAT2API_SOURCE):
            self.assertEqual(_literal_assignment(module, "ATTRIBUTION_KEY_MIN_BYTES"),
                             prod.SHARE_ATTRIBUTION_KEY_MIN_BYTES)

    def test_deploy_reports_attribution_state_without_failing(self):
        text = self._deploy_text()
        # 部署（Prep）与启动（Start）两条路径都报一次归因状态。
        for stage in ("'Prep'", "'Start'"):
            self.assertIn("Write-AttributionKeyState -Where " + stage, text)
        # 判据是文件内容（缺失/不可读/过短三种原因），不是「文件存在」。
        state = re.search(r"function Get-AttributionKeyState \{(.*?)\n\}",
                          text, re.S).group(1)
        self.assertIn("$secret.Length -lt 32", state)
        for reason in ("'missing'", "'unreadable'", "'too_short'"):
            self.assertIn(reason, state)
        self.assertNotIn("throw", state)   # 告警不是失败
        # BOM 口径必须与运行时一致：运行时用 open(encoding="utf-8") 读，
        # BOM 计入长度；PowerShell 的 Get-Content -Encoding UTF8 会去 BOM，
        # 同一个文件会被报成 too_short/31（2026-09-26 复核 F10）。
        self.assertIn("ReadAllBytes", state)
        self.assertIn("UTF8Encoding($false)", state)
        self.assertNotIn("Get-Content", state)
        report = re.search(r"function Write-AttributionKeyState \{(.*?)\n\}",
                           text, re.S).group(1)
        self.assertIn("WARN", report)
        self.assertNotIn("throw", report)

    # ------------------------------------------------------------------
    # SHARING.md 的生成命令（2026-09-26 审查阻塞：Get-Random 不是密码学安全随机源）
    # ------------------------------------------------------------------
    def _doc_text(self):
        return (_GATEWAY_DIR / "SHARING.md").read_text(encoding="utf-8")

    def _doc_generation_block(self):
        """生成段（## 生成 … 到下一个 ##）里的全部 powershell 代码块。"""
        text = self._doc_text()
        section = text.split("## 生成（", 1)[1].split("\n## ", 1)[0]
        blocks = re.findall(r"```powershell\n(.*?)```", section, re.S)
        self.assertTrue(blocks, "SHARING.md 生成段未见 powershell 代码块")
        return section, "\n".join(blocks)

    def test_doc_generation_uses_a_csprng_never_get_random(self):
        text = self._doc_text()
        section, block = self._doc_generation_block()
        # 生成命令里一律不许出现 Get-Random：它不保证密码学安全随机性
        # （微软文档明说，SDL 也把它列为不适用于密码学的函数）。
        self.assertNotIn("Get-Random", block)
        # 全部 fenced 代码块（不只生成段）都不许出现，避免别处留旧写法。
        code = "\n".join(re.findall(r"```(?:powershell)?\n(.*?)```", text, re.S))
        self.assertNotIn("Get-Random", code)
        # 文档里唯一提到它的地方是「不要用它」的说明本身。
        prose = re.sub(r"```.*?```", "", text, flags=re.S)
        self.assertIn("Get-Random", prose)
        self.assertIn("不保证密码学安全随机性", prose)
        # 生成源 = .NET 系统密码学随机数生成器；PS 5.1 没有静态
        # GetBytes(int)，只能用实例方法填 byte[]。
        self.assertIn(
            "[System.Security.Cryptography.RandomNumberGenerator]::Create()",
            block)
        self.assertIn("GetBytes($bytes)", block)
        self.assertIn("byte[] 32", block)
        # 32 字节 → 64 个 hex 字符（Base64 则 44 字符），两种都 ≥32。
        self.assertIn("ToString('x2')", block)
        self.assertIn("64 个 hex 字符", section)
        self.assertIn("44 字符", section)
        # 位置硬约束仍在：不得放 ai-gateway/data（对 um_share 是显式拒绝）。
        self.assertIn("ai-gateway/data", text)
        self.assertIn("显式拒绝 ACE", text)
        self.assertIn("不要放", text)

    def test_doc_generation_verifies_write_and_acl(self):
        section, block = self._doc_generation_block()
        # 写入：无 BOM、单行 UTF-8。
        self.assertIn("WriteAllText($keyPath, $key", block)
        self.assertIn("UTF8Encoding($false)", block)
        # 写后读回：字节数 + 内容哈希 + strip 后长度，不符就 throw。
        self.assertIn("[IO.File]::ReadAllBytes($keyPath)", block)
        self.assertIn("Get-FileHash -LiteralPath $keyPath "
                      "-Algorithm SHA256", block)
        self.assertIn("Text.Encoding]::UTF8.GetBytes($key)", block)
        self.assertIn("写后读回不一致", block)
        self.assertIn("读回长度 <32", block)
        # ACL：先收紧继承，再断言两个运行身份都有 Allow 读 ACE。
        self.assertIn("/inheritance:r", block)
        self.assertIn("'DAICNT\\um_share:R'", block)
        self.assertIn("'DAICNT\\dacint:R'", block)
        self.assertIn("Get-Acl -LiteralPath $keyPath", block)
        self.assertIn("FileSystemRights]::Read", block)
        self.assertIn("没有 Allow 读 ACE", block)
        self.assertIn("icacls $keyPath", block)
        # 以运行身份自读一次（8110 身份 = 非提升的 dacint），只打印长度。
        self.assertIn("以运行身份自读一次", block)
        self.assertIn("encoding='utf-8'", block)
        self.assertNotIn("Get-Content", block)

    def test_doc_rotation_keeps_the_two_end_contract(self):
        text = self._doc_text()
        rotation = text.split("## 轮换", 1)[1].split("\n## ", 1)[0]
        # 只重启 8100、不重启 8110；只换文件不重启 → 全量 bad_signature。
        self.assertIn("只重启 8100", rotation)
        self.assertIn("不重启 8110", rotation)
        self.assertIn("bad_signature", rotation)
        self.assertIn("全量未归属", rotation)
        self.assertIn("不报错", rotation)
        self.assertIn("不拒绝服务", rotation)
        # 收紧后的文件必须先取回写权限才能轮换（本机实测非提升覆盖写被拒）。
        self.assertIn("/grant:r 'DAICNT\\dacint:F'", rotation)

    def test_missing_key_file_is_reported_not_ready_never_loaded(self):
        path = self._key_path()
        out = prod.share_attribution_status(path)
        # 事实 1：文件就绪（本进程此刻可读 + 长度合格）——不是「已接线」。
        self.assertEqual(out["file_ready"], False)
        self.assertEqual(out["file_reason"], "missing")
        self.assertEqual(out["key_file"], path)
        self.assertIsNone(out["key_chars"])
        # 三个事实各有明确名字；旧的模糊名 wired 不再出现。
        self.assertEqual(sorted(out), ["file_ready", "file_reason",
                                     "instance_loaded", "key_chars",
                                     "key_file",
                                     "request_attribution_detail",
                                     "request_attribution_observed"])
        self.assertIs(out["request_attribution_observed"], False)
        self.assertNotIn("wired", out)
        # 事实 2：文件未就绪时没有 mtime 可比 -> unknown，绝不默认已载入。
        self.assertEqual(out["instance_loaded"]["value"], "unknown")
        self.assertEqual(out["instance_loaded"]["reason"],
                         "key_file_not_ready")
        self.assertEqual(out["instance_loaded"]["basis"], "inference")
        self.assertEqual(out["instance_loaded"]["rule"],
                         prod._INSTANCE_LOADED_RULE)
        # 事实 3：请求已验证归属不在本读面冒充，只指向 8110 账本运行期事实。
        self.assertEqual(out["request_attribution_detail"]["verdict"],
                         "not_observed_here")
        self.assertEqual(out["request_attribution_detail"]["runtime_fact"],
                         prod._LEDGER_ATTRIBUTION_FACT)
        self.assertIsNone(out["request_attribution_detail"]["ledger_file"])
        # 观察缝（既有 share 状态读面）也如实报「文件未就绪」。
        ops = prod._ProductionBackendOps(port=8110)
        with mock.patch.object(prod, "production_backend_ops",
                               return_value=ops), \
                mock.patch.object(ops, "verify_running", return_value=False):
            observed = prod.production_backend_observe()
        self.assertEqual(observed["attribution"]["file_ready"], False)
        self.assertEqual(observed["attribution"]["file_reason"], "missing")

    def test_too_short_key_file_is_reported_not_ready_with_length(self):
        path = self._key_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # strip 后恰好 31 字符：两端都读不到合格密钥（绝不报已接线）。
        Path(path).write_text(" " + "x" * 31 + "\n", encoding="utf-8")
        out = prod.share_attribution_status(path)
        self.assertEqual(out["file_ready"], False)
        self.assertEqual(out["file_reason"], "too_short")
        self.assertEqual(out["key_chars"], 31)
        self.assertNotIn("x" * 31, json.dumps(out))   # 绝不回显内容

    def test_key_at_the_minimum_is_reported_file_ready(self):
        path = self._key_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        secret = "s" * prod.SHARE_ATTRIBUTION_KEY_MIN_BYTES
        Path(path).write_text(" " + secret + "\n", encoding="utf-8")
        # 8100 实例探测替身化：本用例不查真实进程。
        out = prod.share_attribution_status(
            path, gateway_probe=lambda: _gateway_probe(
                started_at=os.stat(path).st_mtime + 60))
        self.assertEqual(out["file_ready"], True)
        self.assertEqual(out["file_reason"], "ok")
        self.assertEqual(out["key_chars"], prod.SHARE_ATTRIBUTION_KEY_MIN_BYTES)
        self.assertNotIn(secret, json.dumps(out))   # 密钥内容绝不离开读面
        # 文件就绪不等于实例已载入：那是另一件事（这里是推断 yes）。
        self.assertEqual(out["instance_loaded"]["value"], "yes")

    def test_unreadable_key_file_is_not_folded_into_missing(self):
        path = self._key_path()
        # 同名目录：stat 得到但读不到 -> 不可读（绝不折叠成「不存在」）。
        Path(path).mkdir(parents=True)
        out = prod.share_attribution_status(path)
        self.assertEqual(out["file_ready"], False)
        self.assertEqual(out["file_reason"], "unreadable")

    # ---- 事实 2：运行中的 8100 实例是否载入「当前这份」密钥 --------------
    def _ready_key_file(self):
        path = self._key_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text("k" * 40, encoding="utf-8")
        return path, os.stat(path).st_mtime

    def test_ready_file_with_older_gateway_process_is_not_loaded(self):
        """文件就绪 + 8100 启动时间早于文件 mtime -> 未载入（不是「已接线」）。

        8100 只在启动时读一次密钥：文件在进程启动之后才创建/轮换，
        运行中的实例用的是启动时的旧内容（或根本没有）。"""
        path, mtime = self._ready_key_file()
        out = prod.share_attribution_status(
            path, gateway_probe=lambda: _gateway_probe(
                pid=20740, started_at=mtime - 60))
        self.assertEqual(out["file_ready"], True)   # 文件确实就绪
        self.assertEqual(out["instance_loaded"]["value"], "no")
        self.assertEqual(out["instance_loaded"]["reason"],
                         "process_started_before_key_mtime")
        # 推断不是证明：读面把依据（pid/启动时间/mtime）与规则一起报出来。
        self.assertEqual(out["instance_loaded"]["basis"], "inference")
        self.assertEqual(out["instance_loaded"]["gateway_pid"], 20740)
        self.assertEqual(out["instance_loaded"]["gateway_started_at"],
                         mtime - 60)
        self.assertEqual(out["instance_loaded"]["key_file_mtime"], mtime)

    def test_ready_file_with_newer_gateway_process_is_inferred_loaded(self):
        path, mtime = self._ready_key_file()
        out = prod.share_attribution_status(
            path, gateway_probe=lambda: _gateway_probe(
                pid=20740, started_at=mtime + 1))
        self.assertEqual(out["instance_loaded"]["value"], "yes")
        self.assertEqual(out["instance_loaded"]["reason"],
                         "process_started_at_or_after_key_mtime")

    def test_unfindable_gateway_process_is_unknown_never_loaded(self):
        """查不到进程 -> unknown（不得默认 True/已载入）。"""
        path, _mtime = self._ready_key_file()
        for probe in (_gateway_probe(pid=None, started_at=None,
                                    reason="not_listening"),
                      _gateway_probe(pid=None, started_at=None,
                                     reason="query_failed"),
                      _gateway_probe(pid=20740, started_at=None,
                                     reason="start_time_unavailable")):
            out = prod.share_attribution_status(
                path, gateway_probe=lambda probe=probe: probe)
            self.assertEqual(out["instance_loaded"]["value"], "unknown",
                             probe["reason"])
            self.assertNotEqual(out["instance_loaded"]["value"], "yes")
            # 文件就绪这一事实不受影响（两件事互不冒充）。
            self.assertEqual(out["file_ready"], True)

    def test_instance_verdict_follows_the_evidence_never_a_constant(self):
        """变异核对：判定被改成恒真/恒假时本用例必须失败。

        同一份「文件就绪」事实与同一个 mtime，只换探测证据：启动早于
        mtime、启动晚于 mtime、查不到进程三种证据必须给出三个不同的取值
        （恒真/恒假都会让这里的断言失败）。"""
        path, mtime = self._ready_key_file()
        verdicts = [
            prod.share_attribution_status(
                path, gateway_probe=lambda at=at: _gateway_probe(started_at=at))
            ["instance_loaded"]["value"]
            for at in (mtime - 60, mtime + 60, None)]
        self.assertEqual(verdicts, ["no", "yes", "unknown"])
        self.assertEqual(len(set(verdicts)), 3)

    def test_request_attribution_points_at_the_backend_ledger(self):
        """事实 3：请求已验证归属是运行期事实，本读面只指向它，不冒充。"""
        path, mtime = self._ready_key_file()
        ledger = os.path.join(self.backend_dir, "usage-share-backend.jsonl")
        out = prod.share_attribution_status(
            path, gateway_probe=lambda: _gateway_probe(started_at=mtime + 1),
            ledger_path=ledger)
        self.assertEqual(out["request_attribution_detail"], {
            "verdict": "not_observed_here",
            "runtime_fact": prod._LEDGER_ATTRIBUTION_FACT,
            "ledger_file": ledger})
        # 无论文件/实例事实如何，本读面都不判断归属是否发生。
        self.assertNotIn("verified", out["request_attribution_detail"]["verdict"])

    def test_start_warns_when_key_file_is_missing_but_still_starts(self):
        spawned = []

        def spawner(cmdline, env):
            spawned.append((list(cmdline), dict(env)))
            return SimpleNamespace(pid=4242)

        ops = prod._ProductionBackendOps(
            gateway_dir=str(Path(self._tmp.name) / "chat2api"), port=8110,
            spawner=spawner)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            started = ops.start()
        # 告警不是失败：分享照常启动，且注入的路径仍然是同一个文件。
        self.assertIs(started, True)
        self.assertEqual(len(spawned), 1)
        child_env = spawned[0][1]
        self.assertEqual(child_env[prod.BACKEND_ATTRIBUTION_KEY_ENV],
                         self._key_path())
        # 告警只说文件事实，且不冒充「8100 已载入」。
        self.assertIn("未就绪", buffer.getvalue())
        self.assertIn(prod.SHARE_ATTRIBUTION_KEY_NAME, buffer.getvalue())
        self.assertIn("未探测", buffer.getvalue())
        self.assertNotIn("已接线", buffer.getvalue())

    def test_start_is_silent_when_key_file_is_ready(self):
        path = self._key_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text("k" * 40, encoding="utf-8")

        def spawner(cmdline, env):
            return SimpleNamespace(pid=4242)

        ops = prod._ProductionBackendOps(
            gateway_dir=str(Path(self._tmp.name) / "chat2api"), port=8110,
            spawner=spawner)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertIs(ops.start(), True)
        self.assertNotIn("未就绪", buffer.getvalue())


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


class ReadOnlyCollectionSwitchTests(unittest.TestCase):
    """2026-09-28（复审执行轮 9）：验收采集必须**真正无写副作用**。

    ``verify_running`` 的 ``_forget()``（``os.remove`` 陈旧 ``backend.pid``）是
    加性只读开关 ``forget_stale`` 控制的；``backend_pid_state()`` 全分支无写。
    每个用例的夹具都是 tempfile 临时目录里的**陈旧** pid 文件（PID 不匹配或
    进程已不存在），并断言 size / mtime_ns / 内容逐字不变——「零写副作用」是
    可执行的断言，不是注释里的声明。反向对照证明开关不是恒真：默认那一档仍会
    清理那份陈旧文件。

    只读报告只用 ``verify_running`` 用的同一份证据（CIM 身份 + 端口表），
    不依赖 ``_pid_alive``、也不多跑一条 ``tasklist`` 查询。``_pid_alive``
    本身曾在 2026-09-28 之前对**活着的** PID 也返回 False（``tasklist
    /FO CSV`` 给 PID 字段加引号、裸 needle 不命中）；该缺陷已在本轮修掉
    （改为按 CSV 第二列比对），并加了真实 tasklist 的用例
    ``test_pid_alive_reads_the_quoted_csv_pid_field``。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.backend_dir = os.path.join(self._tmp.name, "backend")
        os.makedirs(self.backend_dir, exist_ok=True)
        # 归因密钥默认在 ProgramData 的 RunRoot 下：把 RunRoot 也重定向到临时
        # 目录，只读观察因此不碰任何真实部署文件。
        env = mock.patch.dict(os.environ, {
            "CONSOLE_SHARE_BACKEND_DIR": self.backend_dir,
            prod.SHARE_RUN_ROOT_ENV: self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.pid_path = os.path.join(self.backend_dir, "backend.pid")
        self._spawn_before = list(_spawn_log)

    def _assert_zero_real_spawn(self):
        self.assertEqual(_spawn_log, self._spawn_before,
                         "real OS command attempted: %r"
                         % (_spawn_log[len(self._spawn_before):],))

    def _write_stale_pid(self, pid=4242, text=None):
        """造一份**陈旧** backend.pid，并把 mtime 固定成一个已知值。

        mtime 用 ``os.utime`` 显式设定，所以「未变」的断言不依赖文件系统
        时间戳粒度。
        """
        with open(self.pid_path, "w", encoding="ascii") as handle:
            handle.write(str(pid) if text is None else text)
        os.utime(self.pid_path, (1000000000.0, 1000000000.0))
        stat = os.stat(self.pid_path)
        with open(self.pid_path, encoding="ascii") as handle:
            content = handle.read()
        return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                "content": content}

    def _assert_untouched(self, before):
        self.assertTrue(os.path.exists(self.pid_path),
                        "陈旧 backend.pid 被删掉了")
        stat = os.stat(self.pid_path)
        self.assertEqual(stat.st_size, before["size"], "size 变了")
        self.assertEqual(stat.st_mtime_ns, before["mtime_ns"], "mtime 变了")
        with open(self.pid_path, encoding="ascii") as handle:
            self.assertEqual(handle.read(), before["content"], "内容变了")

    def _ops(self, matches=False, listening=False):
        """ops 的 OS 边界全部替身化（零真实 tasklist/netstat/powershell）。

        ``alive`` 被记录而不返回：只读报告只用 ``verify_running`` 用的同一份
        证据（CIM 身份 + 端口表），不该多跑一条 ``tasklist`` 查询。
        A9-3：身份/端口证据经 ``_pid_process`` 注入（一次 CIM 读）；身份
        判定另读实例记录（临时目录里不存在 -> reduced/unknown，如实）。
        """
        ops = prod._ProductionBackendOps(
            gateway_dir=os.path.join(self.backend_dir, "chat2api"),
            port=8110, base_url="https://backend.example")
        self.alive_calls = []
        self.pid_process_calls = []
        paths = ops._paths()
        image = sys.executable if matches else "C:\\other\\python.exe"
        cmdline = ("python.exe chat2api.py --port 8110 --profile %s"
                   % paths["profile"]) if matches else "other.exe"

        def fake_process(pid):
            self.pid_process_calls.append(pid)
            return (image, cmdline, 1000.0)

        for name, value in (("_pid_process", fake_process),
                            ("_pid_alive",
                             lambda pid: self.alive_calls.append(pid) or False),
                            ("_port_listening", lambda p: listening)):
            patcher = mock.patch.object(ops, name, new=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return ops

    # ---- 只读档：不删、不改、照实报 -------------------------------------

    def test_pid_alive_reads_the_quoted_csv_pid_field(self):
        """`_pid_alive` 必须把**活着的** PID 认出来（2026-09-28 修）。

        `tasklist /FO CSV /NH` 把每个字段加引号（`"python.exe","54300",…`），
        原实现拿裸 needle ``,54300`` 去比，三种拼法都不命中 → 活 PID 也判 False
        （执行轮 10 的写面审计旁证发现）。本用例用**真实** tasklist（不替身）：
        当前进程自己的 PID 必须 True，一个几乎不可能存在的 PID 必须 False。
        """
        ops = prod._ProductionBackendOps(
            gateway_dir=os.path.join(self.backend_dir, "chat2api"),
            port=8110, base_url="https://backend.example")
        self.assertTrue(ops._pid_alive(os.getpid()),
                        "活着的 PID（本进程）被判成不存在：CSV 引号问题复发")
        self.assertFalse(ops._pid_alive(99999999),
                         "不存在的 PID 被判成活着")

    def test_readonly_verify_running_leaves_the_stale_pid_file(self):
        ops = self._ops(matches=False)
        before = self._write_stale_pid()
        self.assertIs(ops.verify_running(forget_stale=False), False)
        self._assert_untouched(before)
        self._assert_zero_real_spawn()

    def test_readonly_pid_state_reports_stale_without_deleting(self):
        ops = self._ops(matches=False)
        before = self._write_stale_pid()
        facts = ops.backend_pid_state()
        self.assertEqual(facts["state"], "pid_stale")
        self.assertEqual(facts["pid"], 4242)
        self.assertIs(facts["pid_matches"], False)
        self.assertIs(facts["pid_file_present"], True)
        self.assertIsNone(facts["port_listening"])   # 没到端口那一步
        self._assert_untouched(before)
        self._assert_zero_real_spawn()

    def test_readonly_pid_state_reports_running_and_port_states(self):
        ops = self._ops(matches=True, listening=True)
        self._write_stale_pid()
        facts = ops.backend_pid_state()
        self.assertEqual(facts["state"], "running")
        self.assertIs(facts["pid_matches"], True)
        self.assertIs(facts["port_listening"], True)
        # 同一份 pid 文件、同一个只读方法：端口没人听时是 port_not_listening，
        # 既不是 running 也不是陈旧。
        quiet = self._ops(matches=True, listening=False)
        self.assertEqual(quiet.backend_pid_state()["state"], "port_not_listening")
        self._assert_zero_real_spawn()

    def test_readonly_pid_state_reports_absent_and_unreadable(self):
        ops = self._ops()
        # 没有 pid 文件：如实报 pid_file_absent（不折成「陈旧」）。
        facts = ops.backend_pid_state()
        self.assertEqual(facts["state"], "pid_file_absent")
        self.assertIs(facts["pid_file_present"], False)
        self.assertIsNone(facts["pid"])
        # 文件在但内容不是 PID：如实报 pid_file_unreadable，文件照旧留下。
        before = self._write_stale_pid(text="not-a-pid")
        self.assertEqual(ops.backend_pid_state()["state"], "pid_file_unreadable")
        self._assert_untouched(before)
        self._assert_zero_real_spawn()

    def test_readonly_pid_state_never_writes_on_any_branch(self):
        """运行时写哨兵：只读报告路径上任何写盘动作都会立刻失败。"""
        ops = self._ops(matches=False)
        before = self._write_stale_pid()
        guard = _NoWriteGuard()
        with guard:
            ops.verify_running(forget_stale=False)
            ops.backend_pid_state()
        self.assertEqual(guard.writes, [])
        self._assert_untouched(before)
        # 只读报告只用 verify_running 用的同一份证据：不多跑一条 tasklist。
        self.assertEqual(self.alive_calls, [])
        self._assert_zero_real_spawn()

    # ---- 反向对照：默认那一档仍然清理（开关不是恒真） ---------------------

    def test_default_verify_running_still_forgets_the_stale_pid_file(self):
        ops = self._ops(matches=False)
        self._write_stale_pid()
        self.assertIs(ops.verify_running(), False)
        self.assertFalse(os.path.exists(self.pid_path),
                         "默认档必须仍然清理陈旧 pid 文件")
        self._assert_zero_real_spawn()

    def test_the_switch_flips_the_delete_only_not_the_verdict(self):
        """同一份夹具跑两档：判定相同（False），只有「删没删」不同。"""
        ops = self._ops(matches=False)
        before = self._write_stale_pid()
        self.assertIs(ops.verify_running(forget_stale=False), False)
        self._assert_untouched(before)          # 只读档：文件还在
        self.assertIs(ops.verify_running(), False)
        self.assertFalse(os.path.exists(self.pid_path))   # 默认档：删了
        self._assert_zero_real_spawn()

    # ---- 开关转发：production_backend_observe / production_backend_pid_state ----

    def test_backend_observe_forwards_the_readonly_switch(self):
        ops = self._ops()
        seen = []

        def record(forget_stale=True):
            seen.append(forget_stale)
            return False

        with mock.patch.object(prod, "production_backend_ops",
                              return_value=ops), \
                mock.patch.object(ops, "verify_running", new=record):
            readonly = prod.production_backend_observe(forget_stale=False)
            default = prod.production_backend_observe()
        self.assertEqual(seen, [False, True])   # 只读档转发 False；默认档逐字不变
        self.assertEqual(sorted(readonly), ["attribution", "auth_error",
                                          "auth_evidence", "auth_gate",
                                          "auth_label", "auth_recovery",
                                          "auth_state", "health_reachable",
                                          "observed_at", "running",
                                          "upstream_reachability"])
        self.assertEqual(readonly, default)
        self.assertIs(readonly["running"], False)
        self._assert_zero_real_spawn()

    def test_backend_observe_readonly_keeps_the_stale_pid_file(self):
        ops = self._ops(matches=False)
        before = self._write_stale_pid()
        with mock.patch.object(prod, "production_backend_ops",
                              return_value=ops):
            out = prod.production_backend_observe(forget_stale=False)
        self.assertIs(out["running"], False)
        self.assertIn("request_attribution_detail", out["attribution"])
        self._assert_untouched(before)
        self._assert_zero_real_spawn()

    def test_production_backend_pid_state_is_read_only(self):
        ops = self._ops(matches=False)
        before = self._write_stale_pid()
        with mock.patch.object(prod, "production_backend_ops",
                              return_value=ops):
            facts = prod.production_backend_pid_state()
        self.assertEqual(facts["state"], "pid_stale")
        self._assert_untouched(before)
        self._assert_zero_real_spawn()



class DedicatedBackendLifecycleRecordTests(unittest.TestCase):
    """Exit records L5/L6 (round 19): the parent side of the 8110 instance.

    Reuses DedicatedBackendOpsTests' isolation (temp backend dir, temp RunRoot,
    zero real OS commands) by reference, without inheriting its test methods.
    Kept at the end of the file so no line pinned by
    tests/test_share_gate_citations.py moves.
    """

    setUp = DedicatedBackendOpsTests.setUp
    _assert_zero_real_spawn = DedicatedBackendOpsTests._assert_zero_real_spawn
    _ops = DedicatedBackendOpsTests._ops
    _write_pid = DedicatedBackendOpsTests._write_pid

    @staticmethod
    def _lifecycle(ops, paths):
        try:
            with open(ops._lifecycle_path(paths), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def test_start_records_pid_and_names_the_launcher(self):
        spawned = []

        def spawner(cmdline, env):
            spawned.append(dict(env))
            return SimpleNamespace(pid=4242)

        ops = self._ops(spawner=spawner)
        with mock.patch.object(ops, "verify_running", return_value=False):
            self.assertTrue(ops.start())
        paths = ops._paths()
        self.assertEqual(spawned[0].get("CHAT2API_LAUNCH_SOURCE"), "console")
        self.assertTrue(ops._lifecycle_path(paths).startswith(paths["dir"] + os.sep))
        self.assertIn("started pid=4242 port=8110 by=console",
                      self._lifecycle(ops, paths))
        self._assert_zero_real_spawn()

    def test_graceful_stop_is_recorded_without_a_force_line(self):
        ops = self._ops(port=8110)
        paths = self._write_pid(ops, 4242)
        with mock.patch.object(ops, "_pid_matches", new=lambda p, pid: True),                 mock.patch.object(ops, "_graceful_shutdown", new=lambda p: None),                 mock.patch.object(ops, "_pid_alive", new=lambda pid: False):
            self.assertIs(ops.stop(), True)
        text = self._lifecycle(ops, paths)
        self.assertIn("stop requested pid=4242 port=8110 by=console", text)
        self.assertIn("stopped pid=4242 port=8110 by=graceful", text)
        self.assertNotIn("taskkill", text)
        self._assert_zero_real_spawn()

    def test_force_stop_records_the_route_and_its_result(self):
        # The taskkill route is the one the child can never record itself.
        stopper_calls = []
        ops = self._ops(port=8110, stopper=lambda pid: stopper_calls.append(pid))
        paths = self._write_pid(ops, 4242)
        clock = _FakeClock()
        with mock.patch.object(ops, "_pid_matches", new=lambda p, pid: True),                 mock.patch.object(ops, "_graceful_shutdown", new=lambda p: False),                 mock.patch.object(ops, "_pid_alive", new=lambda pid: clock.t < 10.0),                 mock.patch.object(prod.time, "monotonic", clock.monotonic),                 mock.patch.object(prod.time, "sleep", clock.sleep):
            self.assertIs(ops.stop(), True)
        self.assertEqual(stopper_calls, [4242])
        self.assertIn("force stop pid=4242 port=8110 by=taskkill result=dead",
                      self._lifecycle(ops, paths))
        self._assert_zero_real_spawn()

    def test_force_stop_that_fails_is_not_recorded_as_stopped(self):
        ops = self._ops(port=8110, stopper=lambda pid: None)
        paths = self._write_pid(ops, 4242)
        clock = _FakeClock()
        with mock.patch.object(ops, "_pid_matches", new=lambda p, pid: True),                 mock.patch.object(ops, "_graceful_shutdown", new=lambda p: False),                 mock.patch.object(ops, "_pid_alive", new=lambda pid: True),                 mock.patch.object(prod.time, "monotonic", clock.monotonic),                 mock.patch.object(prod.time, "sleep", clock.sleep):
            self.assertIs(ops.stop(), False)
        text = self._lifecycle(ops, paths)
        self.assertIn("force stop pid=4242 port=8110 by=taskkill result=alive", text)
        self.assertNotIn("stopped pid=4242", text)
        self._assert_zero_real_spawn()

    def test_mismatched_pid_is_recorded_as_skipped(self):
        ops = self._ops(port=8110, stopper=lambda pid: None)
        paths = self._write_pid(ops, 4242)
        with mock.patch.object(ops, "_pid_matches", new=lambda p, pid: False),                 mock.patch.object(ops, "_port_listening", new=lambda p: False):
            ops.stop()
        self.assertIn("stop skipped pid=4242 port=8110 reason=pid_not_backend",
                      self._lifecycle(ops, paths))
        self._assert_zero_real_spawn()


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

    def test_supervision_state_reads_the_supervisors_own_state(self):
        # Rev19 A19-2: the supervision-segment read face reads the
        # supervisor's OWN state file (the same single source the watch
        # writes, A9-5) and returns {state, state_path}; an absent or
        # damaged file returns None (the page writes 未提供 ≠ 监督未运行).
        # E19-1-03: the redirection is the explicit injection seam
        # (configure_paths), not a patched module constant — the constants are
        # defaults only now.
        import share_backend_supervision
        with tempfile.TemporaryDirectory() as td:
            # The module's own bounded path helper validates the fixed
            # name and its containment; this test never builds a path.
            _base, _desired, state_path, _log = \
                share_backend_supervision._watch_paths(td)
            share_backend_supervision.configure_paths(
                watch_dir=td, lock_dir=os.path.join(td, "locks"))
            self.addCleanup(share_backend_supervision.configure_paths)
            # Absent state file -> None (never a fabricated segment).
            self.assertIsNone(prod.production_supervision_state())
            share_backend_supervision.write_watch_state({
                "boot_id": "boot-x", "consecutive_failures": 1,
                "restart_attempts_utc": [],
                "idle_reason": "managed_stop_in_force",
                "last_decision": None}, state_path)
            out = prod.production_supervision_state()
            # Damaged state file -> None as well (no fabricated view).
            Path(state_path).write_text("{broken", encoding="utf-8")
            damaged = prod.production_supervision_state()
        self.assertEqual(out["state_path"], state_path)
        self.assertEqual(out["state"]["consecutive_failures"], 1)
        self.assertEqual(out["state"]["idle_reason"], "managed_stop_in_force")
        self.assertEqual(out["state"]["restart_attempts_utc"], [])
        self.assertFalse(out["state"]["unreadable"])
        self.assertIsNone(damaged)

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
        self.assertIs(seams["_BACKEND_REAUTH"],
                      prod.production_backend_reauth)
        self.assertIs(seams["_INTENT_WRITE"], prod._production_intent_write)
        # Rev19 A19-2: the supervision segment's read face is the
        # supervisor's own state reader (lazy import inside the seam).
        self.assertIs(seams["_SUPERVISION_STATE"],
                      prod.production_supervision_state)


class SupervisionPathInjectionTests(unittest.TestCase):
    """E19-1-03: the supervision read/write faces honour the injected roots.

    A composition root that owns its own state tree (the cloud root) injects a
    watch dir and a lock dir ONCE through
    ``console_share_production.configure_supervision_paths``; every later
    desired/state/log/lock access must land there. With nothing injected the
    production defaults stay byte-for-byte (the second half of this test).
    """

    def setUp(self):
        import share_backend_supervision
        self.supervision = share_backend_supervision
        self.saved = (share_backend_supervision.active_watch_dir(),
                      share_backend_supervision.active_lock_dir())
        self.addCleanup(self.supervision.configure_paths)

    def test_injected_roots_carry_every_read_and_write(self):
        import share_backend_supervision as supervision
        with tempfile.TemporaryDirectory(
                prefix="supervision-inject-") as td:
            watch_dir = Path(td) / "watch"
            lock_dir = Path(td) / "locks"
            prod.configure_supervision_paths(watch_dir=str(watch_dir),
                                             lock_dir=str(lock_dir))
            self.assertEqual(supervision.active_watch_dir(),
                             str(watch_dir.resolve()))
            self.assertEqual(supervision.active_lock_dir(),
                             str(lock_dir.resolve()))
            # The write face and the read face resolve the SAME injected file.
            self.assertTrue(supervision.write_desired_state(
                "running", source="injection-test", boot_id="boot|1"))
            self.assertEqual(Path(supervision.active_desired_path()),
                             watch_dir.resolve() / "share-backend-desired.json")
            self.assertTrue(
                Path(supervision.active_desired_path()).is_file())
            desired = supervision.read_desired_state()
            self.assertEqual(desired["state"], "running")
            self.assertEqual(desired["source"], "injection-test")
            supervision.write_watch_state({
                "boot_id": "boot|1", "consecutive_failures": 2,
                "restart_attempts_utc": [], "idle_reason": "injected",
                "last_decision": None})
            self.assertEqual(
                Path(supervision.active_state_path()),
                watch_dir.resolve()
                / "share-backend-watch-state.json")
            view = prod.production_supervision_state()
            self.assertEqual(view["state_path"],
                             supervision.active_state_path())
            self.assertEqual(view["state"]["consecutive_failures"], 2)
            # Nothing was written into the production default roots.
            for default in self.saved:
                self.assertFalse(
                    str(watch_dir.resolve()) == str(default)
                    and Path(default).is_dir()
                    and any(Path(default).iterdir()),
                    "the injection leaked into the default root %s" % default)

    def test_reset_restores_the_production_defaults(self):
        import share_backend_supervision as supervision
        prod.configure_supervision_paths(watch_dir=str(Path(tempfile.gettempdir())
                                                     / "r19-unused-watch"))
        self.assertNotEqual(supervision.active_watch_dir(),
                            supervision.DEFAULT_WATCH_DIR)
        prod.configure_supervision_paths()
        self.assertEqual(supervision.active_watch_dir(),
                         supervision.DEFAULT_WATCH_DIR)
        self.assertEqual(supervision.active_lock_dir(), supervision.LOCK_DIR)

    def test_injected_paths_still_require_the_fixed_file_names(self):
        # The fixed-name discipline survives injection: a caller cannot inject a
        # dir and then read an arbitrary file inside it.
        with self.assertRaises(ValueError):
            self.supervision._fixed_name_path(
                os.path.join(tempfile.gettempdir(), "x", "other.json"),
                "share-backend-desired.json")
        with self.assertRaises(ValueError):
            self.supervision.configure_paths(
                watch_dir=os.path.join(tempfile.gettempdir(), "..", "x"))

    def test_read_and_write_faces_follow_the_injected_root(self):
        """F26 closure: the ASSERTION is a real read, not a constant compare.

        A state file with a DISTINCT marker is planted under the injected root;
        ``read_watch_state()`` without a ``path`` argument must return that
        marker, which is only possible when the reader resolved the injected dir.
        The reverse case proves the unconfigured reader resolves the default
        root: the default is redirected to a second temp dir (never the real
        LOCALAPPDATA watch dir) and the reader must return THAT file.
        """
        import share_backend_supervision as supervision
        with tempfile.TemporaryDirectory(
                prefix="supervision-behaviour-") as td:
            injected = Path(td) / "injected-watch"
            default_root = Path(td) / "default-watch"
            default_root.mkdir()
            # 1. Unconfigured: the reader resolves the default root.
            with mock.patch.object(supervision, "DEFAULT_WATCH_DIR",
                                   str(default_root)):
                supervision.configure_paths()
                self.assertEqual(supervision.active_watch_dir(),
                                 str(default_root))
                supervision.write_watch_state({
                    "boot_id": "boot|default", "consecutive_failures": 5,
                    "restart_attempts_utc": [], "idle_reason": "default-root",
                    "last_decision": None})
                self.assertTrue(
                    (default_root / supervision.STATE_FILE_NAME).is_file())
                default_read = supervision.read_watch_state()
            # 2. Injected: the reader AND the writer move to the injected root.
            prod.configure_supervision_paths(
                watch_dir=str(injected),
                lock_dir=str(Path(td) / "injected-locks"))
            self.assertEqual(supervision.active_watch_dir(),
                             str(injected.resolve()))
            self.assertEqual(supervision.active_lock_dir(),
                             str((Path(td) / "injected-locks").resolve()))
            # Nothing written there yet: the injected root is a DIFFERENT file.
            self.assertEqual(supervision.read_watch_state(),
                             {"unreadable": True, "reason": "missing"})
            supervision.write_watch_state({
                "boot_id": "boot|injected", "consecutive_failures": 1,
                "restart_attempts_utc": [], "idle_reason": "injected-root",
                "last_decision": None})
            injected_read = supervision.read_watch_state()
            self.assertTrue(
                (injected / supervision.STATE_FILE_NAME).is_file())
            # The default-root file is untouched by the injected write: the two
            # roots are separate files, so a reader that still resolved the
            # constant would return the 5-failure record instead.
            default_file = default_root / supervision.STATE_FILE_NAME
            self.assertEqual(
                json.loads(default_file.read_text(encoding="utf-8"))[
                    "consecutive_failures"], 5)
        self.assertFalse(default_read["unreadable"])
        self.assertEqual(default_read["boot_id"], "boot|default")
        self.assertEqual(default_read["idle_reason"], "default-root")
        self.assertFalse(injected_read["unreadable"])
        self.assertEqual(injected_read["boot_id"], "boot|injected")
        self.assertEqual(injected_read["idle_reason"], "injected-root")

    def test_production_read_face_names_the_injected_state_file(self):
        """The page must name the file the supervisor actually writes."""
        import share_backend_supervision as supervision
        with tempfile.TemporaryDirectory(
                prefix="supervision-readface-") as td:
            watch_dir = Path(td) / "watch"
            prod.configure_supervision_paths(
                watch_dir=str(watch_dir),
                lock_dir=str(Path(td) / "locks"))
            self.assertIsNone(prod.production_supervision_state())
            supervision.write_watch_state({
                "boot_id": "boot|readface", "consecutive_failures": 3,
                "restart_attempts_utc": [], "idle_reason": "readface",
                "last_decision": None})
            view = prod.production_supervision_state()
            self.assertEqual(view["state_path"],
                             str(watch_dir.resolve()
                                 / supervision.STATE_FILE_NAME))
            self.assertEqual(view["state"]["consecutive_failures"], 3)
            self.assertEqual(view["state"]["idle_reason"], "readface")


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
        from share_broker_protocol import GATEWAY_RESTART_BUDGET_DEFAULT_S
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


class BackendReauthProductionTests(unittest.TestCase):
    """A9-2 production body: re-authenticate 8110 ONLY - no cold start,
    no process restart, no bypass of the human SSO."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "CONSOLE_SHARE_BACKEND_DIR": self._tmp.name,
            prod.SHARE_RUN_ROOT_ENV: self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(prod.SHARE_ATTRIBUTION_KEY_FILE_ENV, None)
        self.ops_calls = []

    def _ops(self, state="running", identity="verified"):
        ops = prod._ProductionBackendOps(port=8110)
        paths = ops._paths()
        Path(paths["control_token"]).write_text("ctrl-token",
                                                encoding="utf-8")
        self.ops_calls = []

        def rec(name, fn):
            def wrapped(*args, **kwargs):
                self.ops_calls.append(name)
                return fn(*args, **kwargs)
            return wrapped

        ops.start = rec("start", lambda: True)
        ops.stop = rec("stop", lambda: True)
        ops.backend_pid_state = rec(
            "backend_pid_state",
            lambda paths=None: {"state": state, "identity": identity,
                               "identity_reason": "r", "pid": 4242})
        return ops

    def _opener(self, script):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            outcome = script.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            status, payload = outcome
            return _FakeHttpResponse(json.dumps(payload).encode("utf-8"),
                                     status=status)

        fake_urlopen.calls = calls
        return fake_urlopen

    def test_valid_is_a_zero_side_effect_noop(self):
        opener = self._opener([(200, {"state": "valid",
                                      "label": "认证有效"})])
        out = prod.production_backend_reauth(ops=self._ops(),
                                              urlopen_fn=opener,
                                              poll_interval_s=0.01)
        self.assertEqual(out["state"], "valid")
        self.assertIs(out["triggered"], False)
        self.assertIsNone(out["error"])
        self.assertIsNone(out["recovery"])
        self.assertEqual(out["polled"], 0)
        self.assertIs(out["accepted"], True)
        self.assertEqual(out["instance"],
                         {"pid": 4242, "port": 8110,
                          "identity": "verified"})
        self.assertEqual([c.get_method() for c in opener.calls], ["GET"])
        self.assertEqual(self.ops_calls, ["backend_pid_state"])

    def test_logging_in_does_not_open_a_second_window(self):
        opener = self._opener([(200, {"state": "logging_in",
                                      "label": "登录中"})])
        out = prod.production_backend_reauth(ops=self._ops(),
                                              urlopen_fn=opener,
                                              poll_interval_s=0.01)
        self.assertEqual(out["state"], "logging_in")
        self.assertIs(out["triggered"], False)
        self.assertIsNone(out["error"])
        self.assertEqual(out["recovery"]["action"], "wait")
        self.assertEqual([c.get_method() for c in opener.calls], ["GET"])

    def test_failed_refuses_the_rejected_credential(self):
        opener = self._opener([(200, {"state": "failed",
                                      "error": "invalid_credentials"})])
        with self.assertRaises(OperationError) as caught:
            prod.production_backend_reauth(ops=self._ops(),
                                            urlopen_fn=opener,
                                            poll_interval_s=0.01)
        self.assertEqual(caught.exception.code,
                         "share_backend_reauth_refused")
        self.assertIn("invalid_credentials", caught.exception.message)
        self.assertEqual([c.get_method() for c in opener.calls], ["GET"])

    def test_manual_takeover_is_not_refused_and_never_triggers(self):
        opener = self._opener([(200, {
            "state": "failed",
            "error": "manual_takeover:umpass_prefill_missing"})])
        out = prod.production_backend_reauth(
            ops=self._ops(), urlopen_fn=opener, poll_interval_s=0.01)
        self.assertIs(out["triggered"], False)
        self.assertEqual(out["state"], "failed")
        self.assertEqual(out["error"],
                         "manual_takeover:umpass_prefill_missing")
        self.assertEqual(out["recovery"]["action"], "human")
        self.assertIn("人工接管", out["note"])
        self.assertIn("未判凭据失败", out["note"])
        self.assertEqual([c.get_method() for c in opener.calls], ["GET"])

    def test_superseded_is_not_refused_and_never_triggers(self):
        opener = self._opener([(200, {"state": "failed",
                                      "error": "superseded"})])
        out = prod.production_backend_reauth(
            ops=self._ops(), urlopen_fn=opener, poll_interval_s=0.01)
        self.assertIs(out["triggered"], False)
        self.assertEqual(out["state"], "failed")
        self.assertEqual(out["error"], "superseded")
        self.assertEqual(out["recovery"]["action"], "refresh")
        self.assertIn("已被新流程取代", out["note"])
        self.assertIn("未判凭据失败", out["note"])
        self.assertEqual([c.get_method() for c in opener.calls], ["GET"])

    def test_browser_disabled_is_not_refused_and_never_triggers(self):
        opener = self._opener([(200, {"state": "failed",
                                      "error": "BrowserDisabled"})])
        out = prod.production_backend_reauth(
            ops=self._ops(), urlopen_fn=opener, poll_interval_s=0.01)
        self.assertIs(out["triggered"], False)
        self.assertEqual(out["state"], "failed")
        self.assertEqual(out["error"], "BrowserDisabled")
        self.assertEqual(out["recovery"]["action"], "operator")
        self.assertIn("浏览器不可用", out["note"])
        self.assertIn("未判凭据失败", out["note"])
        self.assertEqual([c.get_method() for c in opener.calls], ["GET"])

    def test_unknown_is_unconfirmed_and_never_triggers(self):
        opener = self._opener([(200, {"state": "unknown"})])
        out = prod.production_backend_reauth(ops=self._ops(),
                                              urlopen_fn=opener,
                                              poll_interval_s=0.01)
        self.assertEqual(out["state"], "unconfirmed")
        self.assertIs(out["triggered"], False)
        # The endpoint was readable and reported the state "unknown"; the
        # console names that observation instead of fabricating expiry.
        self.assertEqual(out["error"], "instance_state_unknown")
        self.assertEqual(out["recovery"]["action"], "refresh")
        self.assertEqual([c.get_method() for c in opener.calls], ["GET"])

    def test_needs_login_triggers_then_polls(self):
        opener = self._opener([
            (200, {"state": "needs_login", "label": "需要登录"}),
            (202, {}),
            (200, {"state": "logging_in", "label": "登录中"})])
        out = prod.production_backend_reauth(ops=self._ops(),
                                              urlopen_fn=opener,
                                              poll_interval_s=0.01)
        self.assertIs(out["triggered"], True)
        self.assertEqual(out["state"], "logging_in")
        self.assertEqual(out["polled"], 1)
        posts = [c for c in opener.calls if c.get_method() == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertIn(":8110/admin/auth/login", posts[0].full_url)
        headers = {k.lower(): v for k, v in posts[0].headers.items()}
        self.assertEqual(headers.get("x-admin-control"), "ctrl-token")
        for call in opener.calls:
            self.assertNotIn(":8000", call.full_url)

    def test_trigger_non_202_is_refused(self):
        opener = self._opener([(200, {"state": "needs_login"}),
                              (403, {})])
        with self.assertRaises(OperationError) as caught:
            prod.production_backend_reauth(ops=self._ops(),
                                            urlopen_fn=opener,
                                            poll_interval_s=0.01)
        self.assertEqual(caught.exception.code,
                         "share_backend_reauth_refused")

    def test_window_end_is_manual_never_browser_waiting(self):
        needs = (200, {"state": "needs_login", "label": "需要登录"})
        opener = self._opener([needs, (202, {}), needs, needs])
        with self.assertRaises(OperationError) as caught:
            prod.production_backend_reauth(ops=self._ops(),
                                            urlopen_fn=opener,
                                            poll_interval_s=0.01,
                                            poll_attempts=2)
        self.assertEqual(caught.exception.code,
                         "share_backend_reauth_manual")
        self.assertIn("人工", caught.exception.message)
        self.assertNotIn("浏览器在等", caught.exception.message)

    def test_poll_refusal_preserves_the_instances_reason(self):
        opener = self._opener([
            (200, {"state": "needs_login"}),
            (202, {}),
            (200, {"state": "needs_login",
                   "error": "reauth_refused:slot_corrupt"})])
        with self.assertRaises(OperationError) as caught:
            prod.production_backend_reauth(ops=self._ops(),
                                            urlopen_fn=opener,
                                            poll_interval_s=0.01,
                                            poll_attempts=2)
        self.assertEqual(caught.exception.code,
                         "share_backend_reauth_refused")
        self.assertIn("reauth_refused:slot_corrupt",
                      caught.exception.message)
        self.assertIn("chat2api-reauth.state", caught.exception.message)
        self.assertIn("专用后端", caught.exception.message)
        self.assertNotIn("data/", caught.exception.message)

    def test_poll_manual_takeover_is_not_a_credential_failure(self):
        opener = self._opener([(200, {"state": "needs_login"}),
                              (202, {}),
                              (200, {"state": "failed",
                                     "error": "manual_takeover:x"})])
        out = prod.production_backend_reauth(
            ops=self._ops(), urlopen_fn=opener, poll_interval_s=0.01,
            poll_attempts=2)
        self.assertIs(out["triggered"], True)
        self.assertEqual(out["state"], "failed")
        self.assertEqual(out["error"], "manual_takeover:x")
        self.assertEqual(out["recovery"]["action"], "human")
        self.assertIn("未判凭据失败", out["note"])
        self.assertNotIn("stop", self.ops_calls)

    def test_poll_failed_raises_reauth_failed(self):
        opener = self._opener([(200, {"state": "needs_login"}),
                              (202, {}),
                              (200, {"state": "failed",
                                     "label": "失败"})])
        with self.assertRaises(OperationError) as caught:
            prod.production_backend_reauth(ops=self._ops(),
                                            urlopen_fn=opener,
                                            poll_interval_s=0.01,
                                            poll_attempts=2)
        self.assertEqual(caught.exception.code,
                         "share_backend_reauth_failed")

    def test_not_running_refuses_without_cold_start_or_http(self):
        ops = self._ops(state="pid_file_absent", identity="unknown")
        opener = self._opener([])
        with self.assertRaises(OperationError) as caught:
            prod.production_backend_reauth(ops=ops, urlopen_fn=opener,
                                            poll_interval_s=0.01)
        self.assertEqual(caught.exception.code, "share_backend_not_running")
        self.assertEqual(self.ops_calls, ["backend_pid_state"])
        self.assertEqual(opener.calls, [])   # zero HTTP

    def test_unverified_identity_refuses_without_touching_the_process(self):
        ops = self._ops(identity="reduced")
        opener = self._opener([])
        with self.assertRaises(OperationError) as caught:
            prod.production_backend_reauth(ops=ops, urlopen_fn=opener,
                                            poll_interval_s=0.01)
        self.assertEqual(caught.exception.code,
                         "share_backend_identity_unknown")
        self.assertEqual(opener.calls, [])
        self.assertNotIn("start", self.ops_calls)   # never cold-started

    def test_missing_control_token_refuses_with_zero_http(self):
        ops = self._ops()
        opener = self._opener([])
        with mock.patch.object(prod, "_backend_control_token",
                               return_value=(None, "unreadable")):
            with self.assertRaises(OperationError) as caught:
                prod.production_backend_reauth(ops=ops, urlopen_fn=opener,
                                                poll_interval_s=0.01)
        self.assertEqual(caught.exception.code,
                         "share_backend_login_unwired")
        self.assertEqual(opener.calls, [])

    def test_cancel_check_is_consulted_before_the_trigger(self):
        ops = self._ops()
        opener = self._opener([(200, {"state": "needs_login"})])

        def cancel():
            raise OperationError("cancelled", "ctx cancelled")

        with self.assertRaises(OperationError) as caught:
            prod.production_backend_reauth(ops=ops, urlopen_fn=opener,
                                            poll_interval_s=0.01,
                                            cancel_check=cancel)
        self.assertEqual(caught.exception.code, "cancelled")
        self.assertEqual([c.get_method() for c in opener.calls], [])
        self.assertNotIn("start", self.ops_calls)

    def test_never_cold_starts_or_restarts_the_process(self):
        opener = self._opener([(200, {"state": "valid"})])
        ops = self._ops()
        prod.production_backend_reauth(ops=ops, urlopen_fn=opener,
                                        poll_interval_s=0.01)
        self.assertEqual(self.ops_calls, ["backend_pid_state"])

    def test_seams_wire_the_reauth_seam(self):
        seams = prod.production_seams(broker=_rev9_broker(_Rev9FakeClient(
            [(True, "ok", {"state": "valid"})])))
        self.assertIs(seams["_BACKEND_REAUTH"],
                      prod.production_backend_reauth)


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


class InstanceRecordTests(unittest.TestCase):
    """A9-3 identity evidence: the NEW instance record start() writes, the
    strict five-item check, and the additive identity tri-state."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.backend_dir = os.path.join(self._tmp.name, "backend")
        self.run_root = os.path.join(self._tmp.name, "runroot")
        os.makedirs(self.backend_dir, exist_ok=True)
        env = mock.patch.dict(os.environ, {
            "CONSOLE_SHARE_BACKEND_DIR": self.backend_dir,
            prod.SHARE_RUN_ROOT_ENV: self.run_root})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(prod.SHARE_ATTRIBUTION_KEY_FILE_ENV, None)
        self._spawn_before = list(_spawn_log)

    def _assert_zero_real_spawn(self):
        self.assertEqual(_spawn_log, self._spawn_before,
                         "real OS command attempted: %r"
                         % (_spawn_log[len(self._spawn_before):],))

    def _ops(self, spawner=None, stopper=None):
        return prod._ProductionBackendOps(
            gateway_dir=os.path.join(self.backend_dir, "chat2api"),
            port=8110, base_url="https://backend.example",
            spawner=spawner, stopper=stopper)

    def _spawner(self, pid=4242):
        def spawner(cmdline, env):
            return SimpleNamespace(pid=pid)
        return spawner

    def test_start_writes_the_identity_record(self):
        ops = self._ops(spawner=self._spawner())
        buffer = io.StringIO()
        with mock.patch.object(ops, "verify_running", return_value=False), \
                contextlib.redirect_stdout(buffer):
            self.assertTrue(ops.start())
        paths = ops._paths()
        record = ops._instance_record(paths)
        self.assertIsNotNone(record)
        self.assertEqual(sorted(record), [
            "creation_epoch", "image", "pid", "port", "profile",
            "schema_version", "source", "started_at"])
        self.assertEqual(record["pid"], 4242)
        self.assertEqual(record["port"], 8110)
        self.assertEqual(record["profile"], paths["profile"])
        self.assertEqual(record["image"], sys.executable)
        self.assertEqual(record["source"], "console")
        self.assertEqual(record["schema_version"], 1)
        # The pid file stays the readers' byte-for-byte source.
        with open(paths["pid"], encoding="ascii") as f:
            self.assertEqual(f.read(), "4242")
        # The record lives directly inside the backend dir.
        self.assertTrue(ops._instance_path(paths).startswith(
            os.path.abspath(paths["dir"]) + os.sep))
        self._assert_zero_real_spawn()

    def test_instance_record_rejects_malformed_records(self):
        ops = self._ops()
        paths = ops._paths()
        cases = ("not json {", [], {"schema_version": 2},
                 {"schema_version": 1, "pid": 0, "port": 8110,
                  "image": "x", "profile": "y"},
                 {"schema_version": 1, "pid": 4242, "port": 9999,
                  "image": "x", "profile": "y"},
                 {"schema_version": 1, "pid": 4242, "port": 8110,
                  "image": "", "profile": "y"},
                 {"schema_version": 1, "pid": 4242, "port": 8110,
                  "image": "x", "profile": ""})
        for payload in cases:
            with self.subTest(payload=payload):
                with _rev9_record_reader(payload):
                    self.assertIsNone(ops._instance_record(paths))
        # An absent record: None (the reader path is patched to fail).
        with mock.patch.object(prod, "Path",
                               side_effect=OSError("no record")):
            self.assertIsNone(ops._instance_record(paths))
        self._assert_zero_real_spawn()

    def test_pid_process_reads_the_creation_epoch(self):
        ops = self._ops()
        with mock.patch.object(prod.subprocess, "run") as run:
            run.return_value = SimpleNamespace(
                returncode=0,
                stdout=("C:\\py\\python.exe\n"
                        "python.exe chat2api.py --port 8110\n"
                        "1790475729.9\n").encode("utf-8"))
            image, cmdline, creation = ops._pid_process(4242)
        self.assertEqual(image, "C:\\py\\python.exe")
        self.assertEqual(cmdline, "python.exe chat2api.py --port 8110")
        self.assertEqual(creation, 1790475729.9)
        with mock.patch.object(prod.subprocess, "run") as run:
            run.return_value = SimpleNamespace(
                returncode=0,
                stdout=("C:\\py\\python.exe\n"
                        "python.exe chat2api.py\n\n").encode("utf-8"))
            image, cmdline, creation = ops._pid_process(4242)
        self.assertIsNone(creation)   # no creation time: never guessed
        self._assert_zero_real_spawn()

    def _identity_ops(self, matches=True, profile=None, creation=1000.0):
        ops = self._ops()
        paths = ops._paths()
        Path(paths["pid"]).write_text("4242", encoding="ascii")
        cmdline = ("python.exe chat2api.py --port 8110 --profile %s"
                   % (paths["profile"] if profile is None else profile))
        fake = (sys.executable if matches else "C:\\other\\python.exe",
                cmdline, creation)
        patcher = mock.patch.object(ops, "_pid_process", new=lambda pid: fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return ops, paths

    def test_pid_matches_strict_requires_all_five_items(self):
        ops, paths = self._identity_ops()
        _rev9_seed_record(ops, paths)
        # A complete matching record: verified.
        self.assertIs(ops._pid_matches_strict(paths, 4242), True)
        # A profile mismatch fails the strict check...
        ops, paths = self._identity_ops(profile="C:\\other-profile")
        _rev9_seed_record(ops, paths)
        self.assertIs(ops._pid_matches_strict(paths, 4242), False)
        # ... but the THREE-item check is unaffected (contract §5.1).
        self.assertIs(ops._pid_matches(paths, 4242), True)
        # A creation mismatch (> 2 s) fails.
        ops, paths = self._identity_ops(creation=900.0)
        _rev9_seed_record(ops, paths)
        self.assertIs(ops._pid_matches_strict(paths, 4242), False)
        # An unavailable creation time fails (never assumed).
        ops, paths = self._identity_ops(creation=None)
        _rev9_seed_record(ops, paths)
        self.assertIs(ops._pid_matches_strict(paths, 4242), False)
        # An unusable record: only the three shared checks (reduced).
        ops, paths = self._identity_ops()
        with _rev9_record_reader({"schema_version": 2}):
            self.assertIs(ops._pid_matches_strict(paths, 4242), False)
        self._assert_zero_real_spawn()

    def test_backend_pid_state_identity_tristate(self):
        ops, paths = self._identity_ops()
        _rev9_seed_record(ops, paths)
        with mock.patch.object(ops, "_port_listening", new=lambda p: True):
            state = ops.backend_pid_state(paths)
        self.assertEqual(state["state"], "running")
        self.assertEqual(state["identity"], "verified")
        self.assertEqual(state["identity_reason"],
                         "instance_record_confirmed")
        # No usable record: the three checks pass -> "reduced".
        with _rev9_record_reader({"schema_version": 2}), \
                mock.patch.object(ops, "_port_listening",
                                  new=lambda p: True):
            state = ops.backend_pid_state(paths)
        self.assertEqual(state["state"], "running")
        self.assertEqual(state["identity"], "reduced")
        self.assertEqual(state["identity_reason"],
                         "instance_record_absent")
        # A contradictory record (creation moved): "unknown".
        ops, paths = self._identity_ops(creation=900.0)
        _rev9_seed_record(ops, paths)
        with mock.patch.object(ops, "_port_listening", new=lambda p: True):
            state = ops.backend_pid_state(paths)
        self.assertEqual(state["identity"], "unknown")
        self.assertEqual(state["identity_reason"],
                         "instance_creation_mismatch")
        self._assert_zero_real_spawn()

    def test_start_failure_does_not_write_the_record(self):
        def failing_spawner(cmdline, env):
            raise OSError("spawn failed")

        ops = self._ops(spawner=failing_spawner)
        buffer = io.StringIO()
        with mock.patch.object(ops, "verify_running", return_value=False), \
                contextlib.redirect_stdout(buffer):
            with self.assertRaises(OperationError) as caught:
                ops.start()
        self.assertEqual(caught.exception.code, "share_backend_failed")
        paths = ops._paths()
        self.assertIsNone(ops._instance_record(paths))
        self._assert_zero_real_spawn()


class BackendRestartProductionTests(unittest.TestCase):
    """A9-3 execution body: five-item identity -> stop -> confirm gone ->
    start -> verify_running, with the stage/state reported honestly."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.backend_dir = os.path.join(self._tmp.name, "backend")
        self.run_root = os.path.join(self._tmp.name, "runroot")
        os.makedirs(self.backend_dir, exist_ok=True)
        env = mock.patch.dict(os.environ, {
            "CONSOLE_SHARE_BACKEND_DIR": self.backend_dir,
            prod.SHARE_RUN_ROOT_ENV: self.run_root})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(prod.SHARE_ATTRIBUTION_KEY_FILE_ENV, None)
        self.clock = _FakeClock()
        self.alive = {"alive": True}
        self.stopper_calls = []
        self.graceful_calls = []
        self._spawn_before = list(_spawn_log)

    def _assert_zero_real_spawn(self):
        self.assertEqual(_spawn_log, self._spawn_before,
                         "real OS command attempted: %r"
                         % (_spawn_log[len(self._spawn_before):],))

    def _ops(self, pid_before=4242, record=True):
        ops = prod._ProductionBackendOps(
            gateway_dir=os.path.join(self.backend_dir, "chat2api"),
            port=8110, base_url="https://backend.example",
            stopper=lambda pid: (self.stopper_calls.append(pid),
                                  self.alive.__setitem__("alive", False)))
        paths = ops._paths()
        Path(paths["pid"]).write_text(str(pid_before), encoding="ascii")
        if record:
            _rev9_seed_record(ops, paths, pid_before)
        cmdline = ("python.exe chat2api.py --port 8110 --profile %s"
                   % paths["profile"])
        for name, value in (
                ("_pid_process",
                 lambda pid: (sys.executable, cmdline, 1000.0)),
                ("_port_listening", lambda p: True)):
            patcher = mock.patch.object(ops, name, new=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return ops

    def _patched(self, ops, pid_after=5252):
        started = {"n": 0}

        def fake_start():
            started["n"] += 1
            self.alive["alive"] = True
            paths = ops._paths()
            Path(paths["pid"]).write_text(str(pid_after), encoding="ascii")
            _rev9_seed_record(ops, paths, pid_after)
            return True

        patcher = mock.patch.multiple(
            ops, _pid_alive=lambda pid: self.alive["alive"],
            _graceful_shutdown=lambda p: (self.graceful_calls.append(1),
                                           False),
            start=fake_start, verify_running=lambda *a, **k: True)
        return patcher, started

    def test_restart_success_reports_pids_and_stages(self):
        ops = self._ops()
        steps = []
        patcher, started = self._patched(ops)
        with patcher, \
                mock.patch.object(prod.time, "monotonic",
                                  self.clock.monotonic), \
                mock.patch.object(prod.time, "sleep", self.clock.sleep):
            out = ops.restart(drain_budget_s=5, poll_interval_s=1,
                              stop_budget_s=5, start_budget_s=5,
                              step=steps.append)
        self.assertEqual(out["pid_before"], 4242)
        self.assertEqual(out["pid_after"], 5252)
        self.assertEqual(out["port"], 8110)
        self.assertEqual(out["identity"], "verified")
        self.assertEqual(started["n"], 1)
        self.assertEqual(self.stopper_calls, [4242])   # force path ran
        self.assertIn("正在停止专用后端（pid=4242）", steps)
        self.assertIn("专用后端已重启（pid=5252）", steps)
        self._assert_zero_real_spawn()

    def test_restart_refuses_reduced_identity_without_stopping(self):
        ops = self._ops(record=False)   # no instance record -> reduced
        with self.assertRaises(OperationError) as caught:
            ops.restart()
        self.assertEqual(caught.exception.code,
                         "share_backend_identity_unknown")
        self.assertEqual(self.graceful_calls, [])
        self.assertEqual(self.stopper_calls, [])
        self._assert_zero_real_spawn()

    def test_restart_refuses_when_not_running(self):
        ops = self._ops()
        with mock.patch.object(ops, "_read_pid", return_value=None):
            with self.assertRaises(OperationError) as caught:
                ops.restart()
        self.assertEqual(caught.exception.code,
                         "share_backend_identity_unknown")
        self.assertEqual(self.stopper_calls, [])
        self._assert_zero_real_spawn()

    def test_restart_force_path_rechecks_the_five_items(self):
        ops = self._ops()
        strict_calls = []

        def strict(paths, pid):
            strict_calls.append(pid)
            return True

        patcher, _started = self._patched(ops)
        with patcher, \
                mock.patch.object(ops, "_pid_matches_strict", new=strict), \
                mock.patch.object(prod.time, "monotonic",
                                  self.clock.monotonic), \
                mock.patch.object(prod.time, "sleep", self.clock.sleep):
            out = ops.restart(drain_budget_s=5, poll_interval_s=1,
                              stop_budget_s=5, start_budget_s=5)
        self.assertEqual(strict_calls, [4242])   # re-verified before kill
        self.assertEqual(self.stopper_calls, [4242])
        self.assertEqual(out["pid_after"], 5252)
        self._assert_zero_real_spawn()

    def test_restart_refuses_force_kill_when_identity_changed(self):
        ops = self._ops()
        patcher, _started = self._patched(ops)
        with patcher, \
                mock.patch.object(ops, "_pid_matches_strict",
                                  new=lambda paths, pid: False), \
                mock.patch.object(prod.time, "monotonic",
                                  self.clock.monotonic), \
                mock.patch.object(prod.time, "sleep", self.clock.sleep):
            with self.assertRaises(OperationError) as caught:
                ops.restart(drain_budget_s=5, poll_interval_s=1,
                             stop_budget_s=5, start_budget_s=5)
        self.assertEqual(caught.exception.code,
                         "share_backend_restart_failed")
        self.assertIn("不 taskkill 身份不明的进程",
                      caught.exception.message)
        self.assertEqual(self.stopper_calls, [])
        self._assert_zero_real_spawn()

    def test_restart_unconfirmed_stop_is_failed_with_stage(self):
        ops = self._ops()
        patcher, _started = self._patched(ops)
        with patcher, \
                mock.patch.object(ops, "_pid_matches_strict",
                                  new=lambda paths, pid: True), \
                mock.patch.object(ops, "_pid_alive", new=lambda pid: True), \
                mock.patch.object(prod.time, "monotonic",
                                  self.clock.monotonic), \
                mock.patch.object(prod.time, "sleep", self.clock.sleep):
            with self.assertRaises(OperationError) as caught:
                ops.restart(drain_budget_s=5, poll_interval_s=1,
                             stop_budget_s=5, start_budget_s=5)
        self.assertEqual(caught.exception.code,
                         "share_backend_restart_failed")
        self.assertIn("阶段=stop", caught.exception.message)
        self._assert_zero_real_spawn()

    def test_restart_start_failure_is_failed_with_stage(self):
        ops = self._ops()
        patcher, _started = self._patched(ops)
        with patcher, \
                mock.patch.object(ops, "_pid_matches_strict",
                                  new=lambda paths, pid: True), \
                mock.patch.object(ops, "start", new=lambda: False), \
                mock.patch.object(prod.time, "monotonic",
                                  self.clock.monotonic), \
                mock.patch.object(prod.time, "sleep", self.clock.sleep):
            with self.assertRaises(OperationError) as caught:
                ops.restart(drain_budget_s=5, poll_interval_s=1,
                             stop_budget_s=5, start_budget_s=5)
        self.assertEqual(caught.exception.code,
                         "share_backend_restart_failed")
        self.assertIn("阶段=start", caught.exception.message)
        self._assert_zero_real_spawn()

    def test_restart_verify_running_unconfirmed_is_failed(self):
        ops = self._ops()
        patcher, _started = self._patched(ops)
        with patcher, \
                mock.patch.object(ops, "_pid_matches_strict",
                                  new=lambda paths, pid: True), \
                mock.patch.object(ops, "verify_running",
                                  return_value=False), \
                mock.patch.object(prod.time, "monotonic",
                                  self.clock.monotonic), \
                mock.patch.object(prod.time, "sleep", self.clock.sleep):
            with self.assertRaises(OperationError) as caught:
                ops.restart(drain_budget_s=5, poll_interval_s=1,
                             stop_budget_s=5, start_budget_s=5)
        self.assertEqual(caught.exception.code,
                         "share_backend_restart_failed")
        self.assertIn("阶段=verify_running", caught.exception.message)
        self._assert_zero_real_spawn()

    def test_restart_cancel_before_stop_touches_nothing(self):
        ops = self._ops()

        def cancel():
            raise OperationError("cancelled", "ctx cancelled")

        with mock.patch.object(ops, "_graceful_shutdown",
                               new=lambda p: self.graceful_calls.append(1)):
            with self.assertRaises(OperationError) as caught:
                ops.restart(cancel_check=cancel)
        self.assertEqual(caught.exception.code, "cancelled")
        self.assertEqual(self.graceful_calls, [])
        self.assertEqual(self.stopper_calls, [])
        self._assert_zero_real_spawn()


class IntentWriteTests(unittest.TestCase):
    """A9-5/§7.4: the _INTENT_WRITE seam delegates to the ONE supervision
    writer with a deferred import; a failed write is False, never a fake True."""

    def test_intent_write_delegates_to_the_supervision_writer(self):
        calls = []
        module = SimpleNamespace(
            write_desired_state=lambda state, source=None: (
                calls.append((state, source)) or True))
        with mock.patch.dict(sys.modules,
                              {"share_backend_supervision": module}):
            self.assertIs(prod._production_intent_write(
                "running", source="share.backend_restart"), True)
        self.assertEqual(calls, [("running", "share.backend_restart")])

    def test_intent_write_failure_or_missing_module_is_false(self):
        module = SimpleNamespace(
            write_desired_state=lambda state, source=None: False)
        with mock.patch.dict(sys.modules,
                              {"share_backend_supervision": module}):
            self.assertIs(prod._production_intent_write(
                "stopped", source="share.close"), False)
        # A missing module is False too: the caller reports "意图未写".
        with mock.patch.dict(sys.modules,
                              {"share_backend_supervision": None}):
            self.assertIs(prod._production_intent_write(
                "running", source="share.open"), False)

    def test_intent_write_never_raises_on_a_failing_writer(self):
        def explode(state, source=None):
            raise OSError("disk full")

        module = SimpleNamespace(write_desired_state=explode)
        with mock.patch.dict(sys.modules,
                              {"share_backend_supervision": module}):
            self.assertIs(prod._production_intent_write(
                "running", source="share.open"), False)


class ObserveEvidenceTests(unittest.TestCase):
    """A9-1 additive evidence on the read-only observe seam: observed_at,
    auth evidence/gate, health reachability, upstream_reachability."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "CONSOLE_SHARE_BACKEND_DIR": self._tmp.name,
            prod.SHARE_RUN_ROOT_ENV: self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(prod.SHARE_ATTRIBUTION_KEY_FILE_ENV, None)
        self.gateway_probe = mock.patch.object(
            prod, "share_entry_instance_probe",
            lambda *a, **k: _gateway_probe(pid=None, reason="not_listening"))
        self.gateway_probe.start()
        self.addCleanup(self.gateway_probe.stop)

    def _observe(self, responses):
        ops = prod._ProductionBackendOps(port=8110)
        paths = ops._paths()
        Path(paths["control_token"]).write_text("ctrl-token",
                                                 encoding="utf-8")
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            payload = responses.pop(0)
            return _FakeHttpResponse(json.dumps(payload).encode("utf-8"))

        with mock.patch.object(prod, "production_backend_ops",
                               return_value=ops), \
                mock.patch.object(ops, "verify_running",
                                  return_value=True), \
                mock.patch("console_share_production.urllib.request.urlopen",
                           fake_urlopen):
            out = prod.production_backend_observe(forget_stale=False)
        return out, calls

    def test_observe_reads_health_and_upstream_reachability(self):
        health = {"status": "ok",
                  "upstream_reachability": {
                      "state": "ok", "checked_epoch": 1790000000,
                      "changed": False, "outage_started": None,
                      "last_ok": 1790000000}}
        out, calls = self._observe([
            {"state": "valid", "label": "认证有效",
             "evidence": {"checked_epoch": 1}, "gate": {"wfp": "strong"}},
            health])
        self.assertEqual(out["auth_state"], "valid")
        self.assertEqual(out["auth_evidence"], {"checked_epoch": 1})
        self.assertEqual(out["auth_gate"], {"wfp": "strong"})
        self.assertIsInstance(out["observed_at"], float)
        self.assertIs(out["health_reachable"], True)
        self.assertEqual(out["upstream_reachability"],
                         health["upstream_reachability"])
        self.assertEqual(len(calls), 2)
        self.assertIn(":8110/admin/auth/status", calls[0].full_url)
        self.assertIn(":8110/health", calls[1].full_url)

    def test_observe_off_contract_health_stays_none(self):
        out, _calls = self._observe([{"state": "valid"},
                                     {"status": "ok"}])
        # The health read succeeded but carries no upstream_reachability key:
        # None, never a fabricated reading.
        self.assertIsNone(out["upstream_reachability"])

    def test_observe_unreadable_auth_keeps_unknown_and_no_observed_at(self):
        ops = prod._ProductionBackendOps(port=8110)
        with mock.patch.object(prod, "production_backend_ops",
                               return_value=ops), \
                mock.patch.object(ops, "verify_running",
                                  return_value=True):
            out = prod.production_backend_observe(forget_stale=False)
        self.assertEqual(out["auth_state"], "unknown")
        self.assertIsNone(out["observed_at"])
        self.assertIsNone(out["auth_evidence"])
        self.assertIsNone(out["auth_gate"])


class _ControlWireTransport:
    """One-exchange stand-in for the named-pipe transport: records the decoded
    signed request and the exchange deadline, replays one fixed response."""

    def __init__(self, response=None):
        self.response = response or {"ok": True, "error_code": "ok",
                                    "result": {}}
        self.request = None
        self.deadline = None

    def open(self):
        return self

    def close(self):
        pass

    def send_all(self, data, deadline=None):
        self.deadline = deadline
        self.request = json.loads(data.decode("utf-8").strip())

    def recv_line(self, deadline):
        response = dict(self.response, request_id=self.request["request_id"])
        return (json.dumps(response, ensure_ascii=False, sort_keys=True)
                + "\n").encode("utf-8")


class ControlSurfaceThroughRealAdapterTests(unittest.TestCase):
    """Rev9 end-to-end: console -> REAL BrokerClientAdapter -> REAL
    BrokerClient -> transport. The frozen wire action/fields, the BOUNDED
    exchange deadline and the structured error_code all survive the whole
    chain (the console maps the codes to its frozen console codes)."""

    def _broker(self, response=None):
        transport = _ControlWireTransport(response)
        broker = BrokerClientAdapter(
            share_broker_client.BrokerClient(lambda: transport, "secret"))
        return broker, transport

    def test_success_and_wire_fields_through_the_real_adapter(self):
        begin_ok = {"accepted": True, "component": "share_gateway",
                    "lease_id": "a" * 32, "lease_expires_at": 1790000300,
                    "instance_id": "instance-1", "round": 7,
                    "state": "draining", "active_count": 3}
        broker, transport = self._broker(
            {"ok": True, "error_code": "ok", "result": begin_ok})
        out = prod.production_maintenance_begin(
            "share_gateway", 300, broker=broker)
        self.assertEqual(out, begin_ok)
        self.assertEqual(transport.request["action"], "maintenance-begin")
        self.assertEqual(transport.request["fields"],
                         {"component": "share_gateway", "lease_s": 300})

    def test_error_code_passes_through_the_real_adapter(self):
        for code, expected in (
                ("bad_request", "share_broker_extension_missing"),
                ("capability_missing", "share_capability_missing"),
                ("blocked", "share_maintenance_refused"),
                ("unconfirmed", "share_maintenance_refused"),
                ("instance_mismatch", "share_maintenance_refused")):
            with self.subTest(code=code):
                broker, _ = self._broker(
                    {"ok": False, "error_code": code,
                     "result": {"detail": code}})
                with self.assertRaises(OperationError) as caught:
                    prod.production_maintenance_end(
                        "share_gateway", "a" * 32, broker=broker)
                self.assertEqual(caught.exception.code, expected)

    def test_gateway_restart_deadline_bounded_through_the_real_adapter(self):
        import time
        ok = {"restarted": True, "component": "share_gateway",
              "stopped_confirmed": True, "running_confirmed": True,
              "elapsed_s": 41.2, "maintenance_state": "idle"}
        broker, transport = self._broker(
            {"ok": True, "error_code": "ok", "result": ok})
        before = time.monotonic()
        out = prod.production_gateway_restart(broker=broker)
        self.assertEqual(out, ok)
        self.assertEqual(transport.request["action"], "gateway-restart")
        self.assertEqual(transport.request["fields"],
                         {"component": "share_gateway", "budget_s": 120})
        # budget_s + 15 = 135, capped at the frozen 180 bound.
        window = transport.deadline - before
        self.assertGreater(window, 120)
        self.assertLessEqual(window, 135.001)

    def test_evidence_none_through_the_real_adapter(self):
        # An old broker (bad_request) is honest unavailability: None.
        broker, transport = self._broker(
            {"ok": False, "error_code": "bad_request",
             "result": {"detail": "unknown action"}})
        self.assertIsNone(prod.production_broker_evidence(
            "faults", broker=broker))
        self.assertEqual(transport.request["action"], "host-evidence")

    def test_ts_control_success_and_wire_fields_through_the_real_adapter(self):
        # 控制台 -> 真实适配器 -> 真实客户端 -> 传输：新动词的动作/字段
        # 与显式确认旗标全链保持。
        broker, transport = self._broker(
            {"ok": True, "error_code": "ok",
             "result": {"ts_service_started": True}})
        ack = prod.production_ts_service_control("start", broker=broker)
        self.assertTrue(ack)
        self.assertEqual(transport.request["action"], "ts-service-control")
        self.assertEqual(transport.request["fields"], {"action": "start"})
        # 客户端自身的默认有界期限（读动词用配置期限，不放大）。
        self.assertIsNotNone(transport.deadline)

    def test_ts_control_error_codes_pass_through_the_real_adapter(self):
        for code, detail, expected in (
                ("not_installed", "pipe missing",
                 "share_broker_unwired"),
                ("bad_request", "unknown action",
                 "share_broker_extension_missing"),
                ("unconfirmed", "ts service stop did not re-confirm the "
                                "stopped state",
                 "share_ts_pending_confirmation"),
                ("unconfirmed", "did not verify",
                 "share_layer_failed")):
            with self.subTest(code=code, detail=detail):
                broker, _ = self._broker(
                    {"ok": False, "error_code": code,
                     "result": {"detail": detail}})
                with self.assertRaises(OperationError) as caught:
                    prod.production_ts_service_control("stop", broker=broker)
                self.assertEqual(caught.exception.code, expected)


class BackendAuthWaitOpenChainTests(unittest.TestCase):
    """R13-A：有界认证等待经真实装配边界。

    真实面：production_backend_observe（真 _auth_status_payload + 专用控制
    令牌文件 + 假 urlopen）作为 _BACKEND_OBSERVE 驱动真实 share_open 五层链。
    替身只在最末端：8110 进程门 start/stop/verify（真实 spawn 边界）、
    broker/TS/funnel OS 缝、8100 实例探测。断言零真实 OS 命令、零真实网络，
    且全部请求只打专用端口（无任何 8000 访问）。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.backend_dir = os.path.join(self._tmp.name, "backend")
        self.run_root = os.path.join(self._tmp.name, "runroot")
        self.attribution_key = os.path.join(
            self.run_root, "run", prod.SHARE_ATTRIBUTION_KEY_NAME)
        os.makedirs(os.path.dirname(self.attribution_key), exist_ok=True)
        Path(self.attribution_key).write_text("x" * 40, encoding="utf-8")
        os.makedirs(self.backend_dir, exist_ok=True)
        env = mock.patch.dict(os.environ, {
            "CONSOLE_SHARE_BACKEND_DIR": self.backend_dir,
            prod.SHARE_RUN_ROOT_ENV: self.run_root})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(prod.SHARE_ATTRIBUTION_KEY_FILE_ENV, None)
        self.ops = prod._ProductionBackendOps(
            gateway_dir=str(Path(self.backend_dir) / "chat2api"), port=8110,
            base_url="https://backend.example")
        self.ops_calls = []
        self.verify_calls = []

        def fake_start():
            self.ops_calls.append("start")
            return True

        def fake_stop():
            self.ops_calls.append("stop")
            return True

        def fake_verify_closed():
            self.ops_calls.append("verify_closed")
            return True

        def fake_verify_running(forget_stale=True):
            # 第一次=开启前观察（未运行），此后=本次启动已确认运行。
            self.verify_calls.append(forget_stale)
            self.ops_calls.append("verify_running")
            return len(self.verify_calls) > 1

        for name, fn in (("start", fake_start), ("stop", fake_stop),
                         ("verify_closed", fake_verify_closed),
                         ("verify_running", fake_verify_running)):
            patcher = mock.patch.object(self.ops, name, fn)
            patcher.start()
            self.addCleanup(patcher.stop)
        # 观察缝 = 真实 production_backend_observe（控制令牌真实读取，
        # auth/status 经假 urlopen 打到专用端口）。
        patcher = mock.patch.object(prod, "production_backend_ops",
                                    return_value=self.ops)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            prod, "_backend_health_snapshot",
            lambda paths, opener=None: (False, None))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            prod, "share_entry_instance_probe",
            lambda *a, **k: _gateway_probe(pid=None, reason="not_listening"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.urlopen_calls = []
        self.auth_seq = []

        def fake_urlopen(req, timeout=None):
            self.urlopen_calls.append(req)
            body = self.auth_seq.pop(0) if len(self.auth_seq) > 1 \
                else self.auth_seq[0]
            return _FakeHttpResponse(
                json.dumps({"state": body[0], "label": body[1]})
                .encode("utf-8"))

        patcher = mock.patch("console_share_production.urllib.request.urlopen",
                             fake_urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)
        paths = self.ops._paths()
        Path(paths["control_token"]).write_text("ctrl-token", encoding="utf-8")
        self._wire_orchestration()

    def _wire_orchestration(self):
        """真实编排模块的缝接线：观察缝真实，OS 边界替身。"""
        sleeps = []
        state = orch._ManagedStopState()

        class _BrokerSeq:
            def __init__(self):
                self.calls = []

            def verify_running(self):
                self.calls.append("verify_running")
                return len(self.calls) > 1

            def start_host(self, explicit=False):
                self.calls.append(("start_host", explicit))
                return True

            def stop_host(self):
                self.calls.append("stop_host")
                return True

            def verify_closed(self):
                self.calls.append("verify_closed")
                return True

        ts_status = {"n": 0}
        ts_calls = []

        def ts_status_fn():
            ts_status["n"] += 1
            return "running" if ts_status["n"] > 1 else None

        def ts_control(action):
            ts_calls.append(("control", action))
            return True

        funnel_state = {"on": None}
        funnel_calls = []

        def funnel_control(action):
            funnel_calls.append(("control", action))
            funnel_state["on"] = (action == "on")
            return True

        saved = {name: getattr(orch, name) for name in _RESTORE_ATTRS}
        saved_state = orch._STATE
        saved_sleep = orch._SLEEP
        self.addCleanup(self._restore_orchestration, saved, saved_state,
                        saved_sleep)
        orch._STATE = state
        orch._SLEEP = sleeps.append
        orch._BACKEND_OPS = self.ops
        orch._BACKEND_OBSERVE = prod.production_backend_observe
        orch._KEYS_CONFIRMED = lambda: True
        orch._TS_SERVICE_STATUS = ts_status_fn
        orch._TS_SERVICE_CONTROL = ts_control
        orch._CONNECTIVITY_OK = lambda: True
        # R13-T: the BackendState channel is an OS seam too (real
        # ``tailscale status --json``); pin it so this chain never issues a
        # real command, independent of what earlier modules left wired.
        orch._CONNECTIVITY_STATE = lambda: "Running"
        orch._FUNNEL_CONTROL = funnel_control
        orch._FUNNEL_OBSERVED = lambda: funnel_state["on"]
        self.broker = _BrokerSeq()
        orch._BROKER_OPS = self.broker
        self.login_calls = []
        orch._BACKEND_LOGIN = lambda: self.login_calls.append("login")
        self.ts_calls = ts_calls
        self.funnel_calls = funnel_calls
        self.sleeps = sleeps

    def _restore_orchestration(self, saved, saved_state, saved_sleep):
        for name, value in saved.items():
            setattr(orch, name, value)
        orch._STATE = saved_state
        orch._SLEEP = saved_sleep

    def _params(self, auth_budget=5.0):
        return {"ts_confirm_budget_s": 5, "connectivity_budget_s": 5,
                "backend_budget_s": 5, "backend_auth_budget_s": auth_budget,
                "poll_interval_s": 0.01}

    def test_logging_in_then_valid_open_continues_all_layers(self):
        self.auth_seq = [("logging_in", "认证中（人工窗口已开启）"),
                         ("logging_in", "认证中（人工窗口已开启）"),
                         ("valid", "认证有效")]
        result = orch.share_open("share", self._params(), _FakeCtx())
        self.assertTrue(result["opened"])
        self.assertEqual(result["owned_changes"][0], "share_backend")
        # 门原语面：start 恰一次；成功无回滚（无 stop/verify_closed）；
        # 观察缝内部对 verify_running 的只读复用原样透传 forget_stale。
        self.assertEqual(self.ops_calls.count("start"), 1)
        self.assertNotIn("stop", self.ops_calls)
        self.assertNotIn("verify_closed", self.ops_calls)
        self.assertEqual(self.verify_calls[0], True)
        self.assertEqual(self.login_calls, [])
        # 全部请求只打专用端口（无任何 8000 访问）。
        for req in self.urlopen_calls:
            self.assertTrue(req.full_url.startswith("http://127.0.0.1:8110/"),
                            req.full_url)
            self.assertNotIn(":8000", req.full_url)

    def test_persistent_logging_in_times_out_and_rolls_back_owned_start(self):
        self.auth_seq = [("logging_in", "认证中（人工窗口已开启）")]
        with self.assertRaises(OperationError) as caught:
            orch.share_open("share", self._params(auth_budget=0.05),
                            _FakeCtx())
        self.assertEqual(caught.exception.code, "share_layer_failed")
        self.assertIn("认证等待超限", caught.exception.message)
        self.assertIn("最后观察=登录中", caught.exception.message)
        # 只回滚本次拥有的启动；TS 层从未推进。
        self.assertEqual(self.ops_calls[-2:], ["stop", "verify_closed"])
        self.assertEqual(self.ts_calls, [])
        self.assertEqual(self.login_calls, [])
        for req in self.urlopen_calls:
            self.assertNotIn(":8000", req.full_url)

    def test_needs_login_fails_with_manual_path_and_zero_login_trigger(self):
        self.auth_seq = [("needs_login", "需人工登录")]
        with self.assertRaises(OperationError) as caught:
            orch.share_open("share", self._params(auth_budget=60.0),
                            _FakeCtx())
        self.assertIn("需要人工登录", caught.exception.message)
        self.assertIn("needs_login", caught.exception.message)
        # 编排链绝不触发登录：请求流里没有 /admin/auth/login（无第二个
        # 登录窗口），且只回滚本次拥有的启动。
        for req in self.urlopen_calls:
            self.assertNotIn("/admin/auth/login", req.full_url)
            self.assertNotIn(":8000", req.full_url)
        self.assertEqual(self.login_calls, [])
        self.assertEqual(self.ops_calls[-2:], ["stop", "verify_closed"])


class BackendAggregateScopeTests(unittest.TestCase):
    """R13: the trusted aggregate scope is 8110-only and is EXPLICITLY EMPTY.

    Gray release is not enabled: the dedicated 8110 child receives a valid scope
    that selects no key/model/protocol, so every non-stream request stays native.
    No temporary or user key is selected here.

    Kept in its own class at the END of this file: the seven-key citation
    test pins line numbers inside DedicatedBackendOpsTests, so new methods
    there would re-point those citations.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.backend_dir = os.path.join(self._tmp.name, "backend")
        self.run_root = os.path.join(self._tmp.name, "runroot")
        self.attribution_key = os.path.join(
            self.run_root, "run", prod.SHARE_ATTRIBUTION_KEY_NAME)
        os.makedirs(os.path.dirname(self.attribution_key), exist_ok=True)
        Path(self.attribution_key).write_text("x" * 40, encoding="utf-8")
        os.makedirs(self.backend_dir, exist_ok=True)
        env = mock.patch.dict(os.environ, {
            "CONSOLE_SHARE_BACKEND_DIR": self.backend_dir,
            prod.SHARE_RUN_ROOT_ENV: self.run_root})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(prod.SHARE_ATTRIBUTION_KEY_FILE_ENV, None)
        os.environ.pop(prod.BACKEND_AGGREGATE_SCOPE_ENV, None)

    def _ops(self, port=8110):
        return prod._ProductionBackendOps(
            gateway_dir=str(Path(self.backend_dir) / "chat2api"), port=port,
            base_url="https://backend.example")

    def test_child_env_sets_an_explicitly_empty_scope(self):
        # The 8110 child is the ONLY process that receives the aggregate
        # scope, and the scope is the explicit empty (disabled) selection: no
        # key, no model, no protocol. The literal below is NOT derived from
        # the child env so a widened selection fails this test instead of
        # silently shipping.
        ops = self._ops(port=8110)
        paths = ops._paths()
        env = ops._child_env(paths)
        self.assertEqual(env.get(prod.BACKEND_AGGREGATE_SCOPE_ENV),
                         prod._backend_aggregate_scope_json())
        self.assertEqual(env[prod.BACKEND_AGGREGATE_SCOPE_ENV],
                         '{"key_ids":[],"models":[],"protocols":[]}')
        scope = json.loads(env[prod.BACKEND_AGGREGATE_SCOPE_ENV])
        self.assertEqual(scope, {
            "key_ids": [],
            "models": [],
            "protocols": [],
        })
        # The empty lists above prove no deleted N3 temporary key or unverified
        # user key is selected by the production child.
        # The aggregate switch and the scope reach the SAME 8110 command line.
        cmdline = ops._child_cmdline(paths)
        self.assertIn("--nonstream-upstream-aggregate", cmdline)
        self.assertEqual(cmdline[cmdline.index("--port") + 1], "8110")
        # Building the child env must not mutate the parent environment: the
        # private 8000 chain never inherits the 8110-only scope.
        self.assertNotIn(prod.BACKEND_AGGREGATE_SCOPE_ENV, os.environ)

    def test_aggregate_scope_env_name_is_read_by_chat2api(self):
        # The child env key must be the startup env name chat2api actually
        # parses; a renamed key would silently leave 8110 on the native path.
        self.assertEqual(
            _literal_assignment(_CHAT2API_SOURCE,
                                "NONSTREAM_AGGREGATE_SCOPE_ENV"),
            prod.BACKEND_AGGREGATE_SCOPE_ENV)


if __name__ == "__main__":
    unittest.main()
