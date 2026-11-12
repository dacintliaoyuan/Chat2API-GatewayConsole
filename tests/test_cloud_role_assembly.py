# -*- coding: utf-8 -*-
"""显式云端角色装配与 Linux 受限服务适配（2026-10-30）。

Covers the explicit cloud role contract:

* unset role -> the Windows local console is byte-for-byte unchanged
  (8001, Qoder/CLIProxy/model_sync cards and probes retained);
* ``UM_SHARE_ROLE=cloud`` / ``UM_SHARE_DEPLOYMENT=cloud`` assembles ONLY the
  UM share stack, never reads/probes Qoder / CLIProxy / model_sync / zcode,
  keeps the loopback Host/Origin/CSRF/session gates and defaults to 18096
  with STRICT port validation;
* the cloud registry marks the Tailscale/Funnel chain actions as honestly
  unassembled (disabled + explicit reason; submit refused before any row) while
  the Windows local registry keeps them enabled;
* the Linux restricted host ops use a pid file + REAL ``/proc`` process
  identity (never PowerShell/tasklist/netstat), keep a persistent manual/budget
  stop intent across restarts (a watchdog start cannot resurrect it) and never
  signal a bad pid;
* ``share_broker_ops``' Linux entry uses the AF_UNIX transport (the same
  bounded frames/HMAC/replay/instance identity/deadline contract), and never
  fabricates a Windows DACL or SYSTEM task;
* the management API -> Store -> projection -> instance load -> REAL admission
  chain uses the real implementations (no fake ACK).

Linux-only kernel paths are exercised through injected OS boundaries (fake
``/proc`` root + kill table), so this module runs on Windows too; the real
AF_UNIX transport is covered by tests/test_share_broker_unix_socket.py on Linux.
"""
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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "chat2api"))

import console_adapters  # noqa: E402
import console_share_actions  # noqa: E402
import console_share_production  # noqa: E402
import share_broker_ops  # noqa: E402
import share_broker_protocol as bp  # noqa: E402
import usagesrv  # noqa: E402  (no role env set -> the local default)
from share_broker_client import BrokerClient  # noqa: E402
from share_broker_protocol import BrokerResult  # noqa: E402
from share_entry import ShareServer  # noqa: E402
from share_management import BrokerClientAdapter, ShareManager  # noqa: E402
from console_operations import (  # noqa: E402
    OperationError,
    OperationManager,
    OperationUnavailable,
)
import share_management  # noqa: E402


# --------------------------------------------------------------------- helpers
def _load_cloud_usagesrv(tmp, **extra_env):
    """Load a fresh usagesrv under UM_SHARE_ROLE=cloud with isolated paths."""
    env = {
        "UM_SHARE_ROLE": "cloud",
        "SHARE_MANAGEMENT_CONFIG": str(Path(tmp.name) / "absent.json"),
        "CONSOLE_STATE_FILE": str(Path(tmp.name) / "console.sqlite3"),
        "CONSOLE_LOCK_DIR": str(Path(tmp.name) / "locks"),
        "USAGE_SESSION_CODE_FILE": str(Path(tmp.name) / "session-code"),
        "USAGE_CONTROL_TOKEN_FILE": str(Path(tmp.name) / "control-token"),
        "SHARE_STORE_FILE": str(Path(tmp.name) / "share.db"),
        "CONSOLE_SHARE_BACKEND_DIR": str(Path(tmp.name) / "backend"),
    }
    env.update(extra_env)
    with mock.patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location(
            "usagesrv_cloud_assembly_test", ROOT / "usagesrv.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


def _write_proc(root, pid, argv, *, btime=1700000000, start_ticks=100):
    directory = Path(root) / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cmdline").write_bytes(
        b"\x00".join(str(part).encode() for part in argv) + b"\x00")
    fields = " ".join(["S"] + ["0"] * 18 + [str(start_ticks)])
    (directory / "stat").write_text(
        "%d (python) %s\n" % (pid, fields), encoding="ascii")
    (Path(root) / "stat").write_text(
        "cpu 1 2 3\nbtime %d\n" % btime, encoding="ascii")


def _write_net_tcp(root, port, address="0100007F"):
    net = Path(root) / "net"
    net.mkdir(parents=True, exist_ok=True)
    (net / "tcp").write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue\n"
        "   0: %s:%04X 00000000:0000 0A 00000000:00000000\n"
        % (address, int(port)), encoding="ascii")


def _write_net_tcp6(root, port, address="00000000000000000000000001000000"):
    net = Path(root) / "net"
    net.mkdir(parents=True, exist_ok=True)
    (net / "tcp6").write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue\n"
        "   0: %s:%04X 00000000:0000 0A 00000000:00000000\n"
        % (address, int(port)), encoding="ascii")


class _ProcTable:
    """A fake kernel process table: liveness + signals + /proc identity."""

    def __init__(self, root):
        self.root = Path(root)
        self.alive = set()
        self.ports = set()
        self.signals = []

    def add(self, pid, argv, port=None):
        _write_proc(self.root, pid, argv)
        self.alive.add(int(pid))
        if port is not None:
            self.ports.add(int(port))
            _write_net_tcp(self.root, port)

    def kill(self, pid, sig):
        self.signals.append((int(pid), int(sig)))
        if int(sig) == 0:
            if int(pid) in self.alive:
                return
            raise ProcessLookupError(int(pid))
        if int(sig) in (9, 15):
            self.alive.discard(int(pid))
            import shutil
            shutil.rmtree(self.root / str(pid), ignore_errors=True)
            self.ports.clear()
            net = self.root / "net" / "tcp"
            try:
                net.unlink()
            except OSError:
                pass


class _SnapshotBroker:
    """Minimal broker for assembly-only tests: snapshot, never I/O."""

    name = "assembly-test"

    def snapshot(self):
        return {"name": self.name, "state": "ok"}


class _KillRecorder:
    """Records signals without touching any real process."""

    def __init__(self):
        self.signals = []

    def __call__(self, pid, sig):
        self.signals.append((int(pid), int(sig)))


# ------------------------------------------------------------- role/port
class DeploymentRoleTests(unittest.TestCase):
    def test_unset_role_is_the_local_windows_default(self):
        self.assertEqual(usagesrv.DEPLOYMENT_ROLE, "local")
        self.assertFalse(usagesrv.CLOUD_ROLE)
        self.assertEqual(usagesrv.PORT, 8001)
        self.assertEqual(usagesrv._resolve_deployment_role({}), "local")
        self.assertEqual(usagesrv._resolve_port("local", {}), 8001)
        # B0：本地装配只保留 UM 卡片；退役来源卡片不注册。
        names = {spec.name for spec in console_adapters.build_registry()}
        for name in ("model.catalog", "account.um_status", "share.start"):
            self.assertIn(name, names)
        for retired in ("qoder.status", "cliproxy.status",
                         "qoderglobal.status", "m365.settings_save"):
            self.assertNotIn(retired, names)

    def test_explicit_cloud_role_and_strict_port(self):
        self.assertEqual(
            usagesrv._resolve_deployment_role({"UM_SHARE_ROLE": "cloud"}),
            "cloud")
        self.assertEqual(
            usagesrv._resolve_deployment_role({"UM_SHARE_DEPLOYMENT": "CLOUD"}),
            "cloud")
        self.assertEqual(usagesrv._resolve_port("cloud", {}), 18096)
        self.assertEqual(usagesrv._resolve_port("local", {}), 8001)
        self.assertEqual(
            usagesrv._resolve_port("cloud", {"USAGESRV_PORT": "18097"}), 18097)
        for bad in ("0", "65536", "", " 18096", "+18096", "0x4650",
                     "18096.0", "１８０９６"):
            with self.subTest(port=bad):
                with self.assertRaises(usagesrv.DeploymentRoleError):
                    usagesrv._resolve_port(
                        "cloud", {"USAGESRV_PORT": bad})
        # The strict validation is role-independent: a bad LOCAL override is
        # refused exactly like a bad cloud one (never silently coerced).
        for bad in ("0", "65536", "", " 8001", "+8001", "0x1F41",
                     "8001.0", "８００１"):
            with self.subTest(role="local", port=bad):
                with self.assertRaises(usagesrv.DeploymentRoleError):
                    usagesrv._resolve_port(
                        "local", {"USAGESRV_PORT": bad})
        for bad in ("", "CLOUDX", "prod"):
            with self.subTest(role=bad):
                with self.assertRaises(usagesrv.DeploymentRoleError):
                    usagesrv._resolve_deployment_role({"UM_SHARE_ROLE": bad})
        with self.assertRaises(usagesrv.DeploymentRoleError):
            usagesrv._resolve_deployment_role(
                {"UM_SHARE_ROLE": "cloud", "UM_SHARE_DEPLOYMENT": "local"})

    def test_cloud_module_reads_18096_and_loopback_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = SimpleNamespace(name=directory)
            module = _load_cloud_usagesrv(tmp)
            self.assertEqual(module.DEPLOYMENT_ROLE, "cloud")
            self.assertEqual(module.PORT, 18096)
            self.assertEqual(module.HOST, "127.0.0.1")
            self.assertTrue(module.host_allowed("127.0.0.1:18096"))
            self.assertFalse(module.host_allowed("evil.example:18096"))
            self.assertTrue(module.origin_allowed(
                "http://127.0.0.1:18096"))
            self.assertFalse(module.origin_allowed(
                "http://127.0.0.1:8001"))
            self.assertFalse(module.origin_allowed("https://evil.example"))

    def test_host_and_origin_are_derived_from_the_resolved_port(self):
        # 2026-11-01: Host/Origin are not hardcoded to a port; the same
        # local assembly under USAGESRV_PORT=9101 must accept only that
        # origin, so a stale default port is refused.
        with tempfile.TemporaryDirectory() as directory:
            tmp = SimpleNamespace(name=directory)
            module = _load_cloud_usagesrv(
                tmp, UM_SHARE_ROLE="local", USAGESRV_PORT="9101")
            self.assertEqual(module.DEPLOYMENT_ROLE, "local")
            self.assertEqual(module.PORT, 9101)
            self.assertTrue(module.host_allowed("127.0.0.1:9101"))
            self.assertTrue(module.host_allowed("localhost:9101"))
            self.assertTrue(module.origin_allowed("http://127.0.0.1:9101"))
            self.assertFalse(module.origin_allowed("http://127.0.0.1:8001"))
            self.assertFalse(module.origin_allowed("http://127.0.0.1:18096"))


# --------------------------------------------------- cloud-only assembly
class CloudAssemblyTests(unittest.TestCase):
    def test_cloud_registry_has_only_um_share(self):
        names = [spec.name for spec in console_adapters.build_cloud_registry()]
        self.assertTrue(names)
        for name in names:
            self.assertTrue(name.startswith("share.")
                            or name.startswith("service."), name)
        for absent in ("qoder.status", "model.catalog", "cliproxy.status",
                        "account.um_status", "model.sync_apply"):
            self.assertNotIn(absent, names)
        # Tailscale/WFP-specific public repair is not assembled on the cloud.
        for absent in ("share.public_check", "share.public_retry",
                        "share.repair_limits"):
            self.assertNotIn(absent, names)
        specs = {spec.name: spec for spec in
                 console_adapters.build_cloud_registry()}
        # The four layered open/close chain actions need the Windows TS service /
        # Funnel orchestration. They stay in the catalogue (the console can show
        # WHY) but NO enabled TS/Funnel action may exist in the cloud role.
        enabled_names = {name for name, spec in specs.items() if spec.enabled}
        self.assertFalse(
            enabled_names & console_adapters.CLOUD_SHARE_ORCH_ACTIONS)
        for name in sorted(console_adapters.CLOUD_SHARE_ORCH_ACTIONS):
            self.assertIn(name, specs)
            self.assertFalse(specs[name].enabled, name)
            self.assertEqual(
                specs[name].disabled_reason,
                console_adapters.CLOUD_SHARE_ORCH_DISABLED_REASON)
            self.assertIn("Tailscale/Funnel 编排未装配",
                          specs[name].disabled_reason)
        # The remaining share cards are genuinely usable on the cloud.
        for name in ("share.status", "share.diagnostics",
                      "share.backend_login", "share.backend_login_cancel",
                      "share.backend_reauthenticate",
                      "share.backend_restart", "share.gateway_restart"):
            self.assertTrue(specs[name].enabled, name)
            self.assertIsNone(specs[name].disabled_reason, name)
        # D18-10: the whole-chain lifecycle actions are CLOUD-ONLY and
        # enabled; they drive the broker + 8110 only (no TS/Funnel seams).
        for name in ("share.chain_start", "share.chain_stop",
                      "share.chain_estop"):
            self.assertTrue(specs[name].enabled, name)
            self.assertIsNone(specs[name].disabled_reason, name)
        self.assertIs(specs["share.chain_start"].handler,
                      console_share_actions.chain_start)
        self.assertIs(specs["share.chain_stop"].handler,
                      console_share_actions.chain_stop)
        self.assertIs(specs["share.chain_estop"].handler,
                      console_share_actions.chain_estop)
        self.assertIs(specs["share.backend_login_cancel"].handler,
                      console_share_actions.backend_login_cancel)
        # D18-6: the bounded auth wait accepts the full 30 minute human
        # login window (1800s), not the old 600s cap.
        auth_budget = specs["share.chain_start"].param_schema[
            "backend_auth_budget_s"]
        self.assertEqual(auth_budget["max"], 1800)
        self.assertEqual(
            console_adapters._SHARE_OPEN_BUDGET_SCHEMA[
                "backend_auth_budget_s"]["max"], 1800)
        # The cloud-only chain actions do not leak into the local catalogue.
        local_names = {spec.name for spec in
                       console_adapters.build_registry()}
        for name in ("share.chain_start", "share.chain_stop",
                       "share.chain_estop"):
            self.assertNotIn(name, local_names)
        self.assertEqual(specs["service.start"].resource_fn(
            "share-gateway", {}), "share:orchestration")
        with self.assertRaises(Exception):
            specs["service.start"].resource_fn("chat2api", {})
        # A2: the two pages that restart the SAME 8100 resource must serialize
        # on ONE backend resource key (the operation manager's mutex), not just
        # on each page's own button state.
        self.assertEqual(
            specs["service.restart"].resource_fn("share-gateway", {}),
            specs["share.gateway_restart"].resource_fn("share", {}))
        self.assertEqual(
            specs["service.restart"].resource_fn("share-gateway", {}),
            "share:orchestration")

    def test_cloud_submit_refuses_unassembled_ts_funnel_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = OperationManager(
                os.path.join(directory, "state.sqlite3"),
                console_adapters.build_cloud_registry(),
                os.path.join(directory, "locks"))
            try:
                for name in sorted(console_adapters.CLOUD_SHARE_ORCH_ACTIONS):
                    with self.subTest(action=name):
                        with self.assertRaises(OperationUnavailable) as cm:
                            manager.submit(name, "share")
                        self.assertEqual(cm.exception.code, "action_disabled")
                        self.assertEqual(
                            cm.exception.reason,
                            console_adapters.CLOUD_SHARE_ORCH_DISABLED_REASON)
                # A refused action never creates an operation row.
                self.assertEqual(manager.list_operations(), [])
            finally:
                manager.close()

    def test_local_registry_keeps_the_ts_funnel_chain_enabled(self):
        # Building the cloud registry first must never mutate the local one.
        cloud = {spec.name: spec for spec in
                 console_adapters.build_cloud_registry()}
        local = {spec.name: spec for spec in console_adapters.build_registry()}
        for name in sorted(console_adapters.CLOUD_SHARE_ORCH_ACTIONS):
            self.assertFalse(cloud[name].enabled, name)
            self.assertTrue(local[name].enabled, name)
            self.assertIsNone(local[name].disabled_reason, name)
        # The local-only public repair cards stay present and enabled.
        for name in ("share.public_check", "share.public_retry",
                      "share.repair_limits"):
            self.assertNotIn(name, cloud)
            self.assertTrue(local[name].enabled, name)

    def test_cloud_backend_login_keeps_the_wired_control_channel(self):
        specs = {spec.name: spec for spec in
                 console_adapters.build_cloud_registry()}
        login = specs["share.backend_login"]
        self.assertTrue(login.enabled)
        self.assertIsNone(login.disabled_reason)
        self.assertIs(login.handler, console_share_actions.backend_login)
        # The cloud production assembly keeps the dedicated-backend control
        # channel (_BACKEND_LOGIN + _BACKEND_OPS) and drops ONLY the
        # Tailscale/Funnel seams.
        seams = console_share_production.production_seams(
            broker=_SnapshotBroker(), cloud=True)
        self.assertIs(seams["_BACKEND_LOGIN"],
                      console_share_production.production_backend_login)
        self.assertIn("_BACKEND_OPS", seams)
        for name in ("_TS_SERVICE_CONTROL", "_TS_SERVICE_STATUS",
                      "_CONNECTIVITY_OK", "_CONNECTIVITY_STATE",
                      "_FUNNEL_CONTROL", "_FUNNEL_OBSERVED"):
            self.assertNotIn(name, seams)
        # The wired handler reaches the control-channel seam (stubbed here):
        # neither a fake success nor an unwired refusal.
        sentinel = {"triggered": True, "state": "logging_in"}
        with mock.patch.object(console_share_actions, "_BACKEND_LOGIN",
                                lambda **_kwargs: sentinel):
            self.assertIs(console_share_actions.backend_login("share", {}),
                          sentinel)
        # An unwired control channel stays an explicit, honest refusal.
        with mock.patch.object(console_share_actions, "_BACKEND_LOGIN", None):
            with self.assertRaises(OperationError) as cm:
                console_share_actions.backend_login("share", {})
            self.assertEqual(cm.exception.code,
                             "share_backend_login_unwired")

    def test_cloud_edge_status_is_a_read_only_systemd_probe(self):
        # D18-10: the whole-chain start reports the public edge as a LAYER
        # FACT. A missing cloudflared unit is "not managed by this
        # deployment" (not a failure); a query failure is unconfirmed.
        calls = []

        def runner(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return SimpleNamespace(
                returncode=0,
                stdout="LoadState=loaded\nActiveState=active\n")

        out = console_adapters.production_cloud_edge_status(runner=runner)
        self.assertEqual(out, {"exists": True, "active": True,
                              "detail": "active"})
        self.assertEqual(calls[0][0][0], "systemctl")
        self.assertIn("cloudflared", calls[0][0])
        self.assertTrue(calls[0][1].get("capture_output"))

        def missing(cmd, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout="LoadState=not-found\nActiveState=inactive\n")

        self.assertEqual(
            console_adapters.production_cloud_edge_status(runner=missing),
            {"exists": False, "active": None, "detail": "unit_not_found"})

        def broken(cmd, **kwargs):
            raise OSError("systemctl unavailable")

        self.assertEqual(
            console_adapters.production_cloud_edge_status(runner=broken),
            {"exists": None, "active": None,
             "detail": "systemctl_unavailable"})

    def test_cloud_service_status_never_calls_powershell(self):
        fake = SimpleNamespace(status=lambda: {
            "state": "running", "healthy": True, "detail": "ok",
            "process_ids": [4242]})
        with mock.patch.object(console_adapters, "_invoke_wrapper",
                               side_effect=AssertionError("PowerShell used")):
            payload = console_adapters.service_status(cloud=True, ops=fake)
        self.assertEqual([s["key"] for s in payload["services"]],
                         ["share-gateway"])
        self.assertEqual(payload["services"][0]["state"], "Running")
        self.assertEqual(payload["services"][0]["process_ids"], [4242])

    def test_cloud_summary_reads_no_non_um_resource(self):
        with tempfile.TemporaryDirectory() as directory:
            module = _load_cloud_usagesrv(SimpleNamespace(name=directory))
            boom = mock.Mock(side_effect=AssertionError("non-UM read"))
            with mock.patch.object(module, "_read_jsonl", boom), \
                    mock.patch.object(module, "read_dsh_ledger", boom):
                payload = module.build_summary()
        self.assertEqual(payload["role"], "cloud")
        self.assertEqual(payload["services"], {})
        self.assertIsNone(payload["qoderQuota"]["cn"])
        self.assertIsNone(payload["qoderQuota"]["global"])
        self.assertNotIn("qoder", payload["services"])
        self.assertNotIn("chat2api", payload["services"])
        self.assertNotIn("dsh", payload["services"])
        self.assertIn("share", payload["assembly"]["services"])

    def test_local_summary_still_reads_every_ledger(self):
        dsh = {"kind": "dsh-ledger", "pricingCurrency": "USD",
               "requests": 0, "costUsd": 0.0,
               "tokens": {"input": 0, "output": 0, "cache": 0,
                          "reasoning": 0},
               "models": [], "days": [], "unpricedModels": []}
        with mock.patch.object(usagesrv, "_read_jsonl",
                               return_value=[]) as reader, \
                mock.patch.object(usagesrv, "read_dsh_ledger",
                                  return_value=dsh):
            payload = usagesrv.build_summary()
        self.assertEqual([call.args[0] for call in reader.call_args_list],
                         [usagesrv.CHAT2API_LOG])
        self.assertEqual(set(payload["services"]), {"chat2api", "dsh"})
        for retired in ("qoder", "m365"):
            self.assertNotIn(retired, payload["services"])
        self.assertNotIn("role", payload)


# --------------------------------------------- cloud HTTP gates and routes
class CloudHttpGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {
            "CONSOLE_SHARE_BACKEND_DIR": str(Path(self.tmp.name) / "backend"),
            "SHARE_MANAGEMENT_CONFIG": str(Path(self.tmp.name) / "absent.json"),
            "CONSOLE_STATE_FILE": str(Path(self.tmp.name) / "console.sqlite3"),
            "CONSOLE_LOCK_DIR": str(Path(self.tmp.name) / "locks"),
            "SHARE_STORE_FILE": str(Path(self.tmp.name) / "share.db"),
            "USAGE_SESSION_CODE_FILE": str(Path(self.tmp.name) / "session-code"),
            "USAGE_CONTROL_TOKEN_FILE": str(Path(self.tmp.name) / "control-token"),
        }
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.module = _load_cloud_usagesrv(SimpleNamespace(name=self.tmp.name))
        backend = Path(self.tmp.name) / "backend"
        backend.mkdir(parents=True, exist_ok=True)
        self.ledger = backend / "usage-share-backend.jsonl"
        self.ledger.write_text(json.dumps({
            "time": "2026-10-30T12:00:00Z", "key_id": "k1",
            "model": "m", "input_tokens": 1, "output_tokens": 2,
            "status": "ok", "usage_source": "upstream",
        }) + "\n", encoding="utf-8")
        (Path(self.tmp.name) / "control-token").write_text(
            "cloud-test-token", encoding="utf-8")
        self.server = self.module.ThreadingHTTPServer(
            ("127.0.0.1", 0), self.module.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        manager = getattr(self.module, "_console_manager", None)
        if manager is not None:
            manager.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def _request(self, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            payload = None if body is None else json.dumps(body)
            headers = dict(headers or {})
            if payload is not None:
                headers.setdefault("Content-Type", "application/json")
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def _session_cookie(self):
        code = self.module.session_bootstrap_code()
        sid = self.module.session_exchange(code)
        return "%s=%s" % (self.module.SESSION_COOKIE, sid)

    def test_non_um_read_faces_are_explicitly_not_assembled(self):
        for path in ("/api/console/stats", "/api/console/logs",
                      "/api/console/quota-snapshot",
                      "/api/console/model-pending",
                      "/api/console/gateway-connection?service=Qoder"):
            with self.subTest(path=path):
                status, payload = self._request(
                    "GET", path,
                    {"Host": "127.0.0.1:%d" % self.port,
                     "X-Admin-Control": "cloud-test-token"})
                self.assertEqual(status, 503)
                self.assertEqual(payload["error"], "capability_not_assembled")
                self.assertEqual(payload["role"], "cloud")

    def test_cloud_unassembled_chain_action_is_refused_over_http(self):
        # The action list shows the TS/Funnel chain cards as DISABLED with the
        # cloud reason, and a submit is refused (403 action_disabled) before any
        # operation row exists — never a fake success or an unhandled seam error.
        status, payload = self._request(
            "GET", "/api/console/actions",
            {"Host": "127.0.0.1:%d" % self.port})
        self.assertEqual(status, 200)
        by_name = {i["action"]: i for i in payload["actions"]}
        for name in sorted(console_adapters.CLOUD_SHARE_ORCH_ACTIONS):
            with self.subTest(action=name):
                self.assertFalse(by_name[name]["enabled"], name)
                self.assertIn("Tailscale/Funnel 编排未装配",
                              by_name[name]["disabled_reason"])
        status, payload = self._request(
            "POST", "/api/console/operations",
            {"Host": "127.0.0.1:%d" % self.port},
            {"action": "share.start", "target": "share"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "action_disabled")
        self.assertIn("Tailscale/Funnel 编排未装配", payload["reason"])
        status, payload = self._request(
            "GET", "/api/console/operations",
            {"Host": "127.0.0.1:%d" % self.port})
        self.assertEqual(status, 200)
        self.assertEqual(payload["operations"], [])

    def _isolate_capacity_seams(self):
        """Keep the status read from wiring the real broker in this test."""
        saved = {name: getattr(console_share_actions, name) for name in (
            "_MAINTENANCE_STATUS", "_BROKER_OPS", "_FUNNEL_OBSERVED")}
        for name in saved:
            self.addCleanup(setattr, console_share_actions, name, saved[name])
            setattr(console_share_actions, name, None)
        patcher = mock.patch.object(
            console_adapters, "wire_share_production_seams",
            lambda **kwargs: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cloud_share_status_reports_capacity_honestly(self):
        """r14 overview: configured cap vs in-flight/queue/upstream capacity.

        The configured 32 is an admission cap, not a measured upstream
        capacity. An unwired broker keeps in-flight null (never 0); the
        dispatcher queue and upstream capacity have no read seam and stay
        null with explicit reasons.
        """
        self._isolate_capacity_seams()
        from share_store import ShareStore
        store = ShareStore(Path(self.tmp.name) / "share.db")
        try:
            store.set_share_max_concurrency(32)
        finally:
            store.close()
        status, payload = self._request(
            "GET", "/api/console/share-management/status",
            {"Host": "127.0.0.1:%d" % self.port})
        self.assertEqual(status, 200)
        self.assertEqual(payload["share_max_concurrency"], 32)
        self.assertEqual(payload["capacity"]["configured"], 32)
        self.assertIsNone(payload["capacity"]["in_flight"])
        self.assertEqual(payload["capacity"]["in_flight_reason"],
                         "maintenance_unwired")
        self.assertIsNone(payload["capacity"]["queued"])
        self.assertEqual(payload["capacity"]["queued_reason"],
                         "not_exposed")
        self.assertIsNone(payload["capacity"]["upstream_capacity"])
        self.assertIs(payload["capacity"]["upstream_capacity_measured"],
                        False)
        self.assertIn("当前部署 32", payload["capacity"]["note"])
        self.assertNotIn("当前部署 128", payload["capacity"]["note"])
        self.assertIn("blocked_publications", payload)
        self.assertIn("pending_revocation", payload)

    def test_cloud_share_status_capacity_note_follows_configured_value(self):
        """The note states the payload's configured cap, never a fixed 128."""
        self._isolate_capacity_seams()
        from share_store import ShareStore
        store = ShareStore(Path(self.tmp.name) / "share.db")
        try:
            store.set_share_max_concurrency(64)
        finally:
            store.close()
        status, payload = self._request(
            "GET", "/api/console/share-management/status",
            {"Host": "127.0.0.1:%d" % self.port})
        self.assertEqual(status, 200)
        self.assertEqual(payload["share_max_concurrency"], 64)
        self.assertEqual(payload["capacity"]["configured"], 64)
        self.assertIn("当前部署 64", payload["capacity"]["note"])
        self.assertNotIn("128", payload["capacity"]["note"])

    def test_cloud_share_capacity_note_reports_missing_configured_as_unreported(self):
        """A payload without a usable cap keeps configured null and says 未上报."""
        capacity = usagesrv.Handler._console_share_capacity({})
        self.assertIsNone(capacity["configured"])
        self.assertIn("未上报", capacity["note"])
        self.assertNotIn("128", capacity["note"])
        self.assertNotIn("64", capacity["note"])

    def test_cloud_share_status_reads_in_flight_from_maintenance_seam(self):
        self._isolate_capacity_seams()
        console_share_actions._MAINTENANCE_STATUS = lambda: {
            "checked_at": 1790000000.0,
            "instance": {"active_count": 3, "state": "running"},
        }
        status, payload = self._request(
            "GET", "/api/console/share-management/status",
            {"Host": "127.0.0.1:%d" % self.port})
        self.assertEqual(status, 200)
        self.assertEqual(payload["capacity"]["in_flight"], 3)
        self.assertIsNone(payload["capacity"]["in_flight_reason"])
        self.assertEqual(payload["capacity"]["checked_at"], 1790000000)

    def test_cloud_share_usage_reads_the_share_ledger(self):
        status, payload = self._request(
            "GET", "/api/console/share-usage",
            {"Host": "127.0.0.1:%d" % self.port})
        self.assertEqual(status, 200)
        self.assertEqual(payload["requests"], 1)

    def test_host_origin_and_csrf_gates_are_retained(self):
        # Foreign Host (DNS rebinding) and the LOCAL 8001 origin are refused.
        status, _payload = self._request(
            "GET", "/api/console/share-usage",
            {"Host": "evil.example:%d" % self.port})
        self.assertEqual(status, 403)
        status, _payload = self._request(
            "GET", "/api/console/share-usage",
            {"Host": "127.0.0.1:%d" % self.port,
             "Origin": "http://127.0.0.1:8001"})
        self.assertEqual(status, 403)
        # A cookie session write without the same-origin JS marker is refused.
        cookie = self._session_cookie()
        status, payload = self._request(
            "POST", "/api/share/keys",
            {"Host": "127.0.0.1:%d" % self.port, "Cookie": cookie},
            {"name": "friend"})
        self.assertEqual(status, 403)
        self.assertIn("CSRF", payload["error"])
        # No credentials at all is an honest 401 (never a fake success).
        status, _payload = self._request(
            "POST", "/api/share/keys",
            {"Host": "127.0.0.1:%d" % self.port}, {"name": "friend"})
        self.assertEqual(status, 401)

    def test_cloud_health_probes_only_the_share_entry(self):
        with mock.patch.object(
                self.module, "_probe_service",
                side_effect=AssertionError("cloud TCP probe")), \
                mock.patch.object(
                    console_adapters, "_invoke_wrapper",
                    side_effect=AssertionError("PowerShell used")):
            status, payload = self._request(
                "GET", "/api/console/health",
                {"Host": "127.0.0.1:%d" % self.port})
        self.assertEqual(status, 200)
        keys = [s.get("key") or s.get("id") for s in payload["services"]]
        self.assertEqual(keys, ["share-gateway", "usagesrv"])

    def test_cloud_services_snapshot_never_calls_powershell(self):
        with mock.patch.object(
                console_adapters, "_invoke_wrapper",
                side_effect=AssertionError("PowerShell used")):
            status, payload = self._request(
                "GET", "/api/console/services",
                {"Host": "127.0.0.1:%d" % self.port})
        self.assertEqual(status, 200)
        self.assertEqual([s["key"] for s in payload["services"]],
                         ["share-gateway"])

    def test_cloud_status_get_never_wires_tailscale_seams(self):
        """The status facade's lazy self-wiring must follow the cloud role.

        A bare GET /api/console/share-management/status wires the share seams
        itself (it must not depend on the operations chain having run first).
        That lazy wiring has to pass the CLOUD flag: a provisioned broker config
        (the real cloud deployment) would otherwise let production_seams() install
        the local Tailscale/Funnel seams into a cloud process.
        """
        watched = ("_TS_SERVICE_CONTROL", "_TS_SERVICE_STATUS",
                   "_CONNECTIVITY_OK", "_CONNECTIVITY_STATE",
                   "_FUNNEL_CONTROL", "_FUNNEL_OBSERVED")
        saved = {name: getattr(console_share_actions, name)
                 for name in watched}
        saved_ops = console_share_actions._BROKER_OPS
        self.addCleanup(lambda: setattr(
            console_share_actions, "_BROKER_OPS", saved_ops))
        for name, value in saved.items():
            self.addCleanup(setattr, console_share_actions, name, value)
            setattr(console_share_actions, name, None)
        console_share_actions._BROKER_OPS = None

        class _Broker:
            def snapshot(self):
                return {"name": "fake", "state": "ok"}

            def host_verify(self, check):
                return BrokerResult.ok({"check": check, "verdict": None,
                                         "detail": "fake",
                                         "checked_at": None})

        with mock.patch.object(
                console_share_production, "_default_broker",
                return_value=_Broker()), \
                mock.patch.object(
                    console_share_production, "production_backend_observe",
                    return_value={"auth_state": None, "running": None}):
            status, payload = self._request(
                "GET", "/api/console/share-management/status",
                {"Host": "127.0.0.1:%d" % self.port})
        self.assertEqual(status, 200)
        # The cloud assembly ran (broker gate + backend observation wired)...
        self.assertIsNotNone(console_share_actions._BROKER_OPS)
        # ...but no Tailscale/Funnel seam was installed into this process.
        for name in watched:
            self.assertIsNone(getattr(console_share_actions, name), name)
        self.assertNotIn("gateway_running", payload)
        self.assertNotIn("funnel_on", payload)


# ------------------------------------------------ Linux host ops (pid file)
class LinuxHostOpsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.state = self.root / "state"
        self.pid_file = self.state / "share-host.pid"
        self.table = _ProcTable(self.proc)
        self.spawned = []

    def _spawner(self, command):
        self.table.add(5001, command, port=8100)
        self.spawned.append(list(command))
        return SimpleNamespace(pid=5001)

    def _ops(self, **overrides):
        options = dict(
            pid_file=str(self.pid_file),
            command=[sys.executable, "chat2api.py", "--port", "8100"],
            state_dir=str(self.state),
            expected_port=8100,
            proc_root=str(self.proc),
            kill_fn=self.table.kill,
            spawner=self._spawner,
            clock=lambda: 0.0,
            sleeper=lambda _seconds: None,
        )
        options.update(overrides)
        return share_broker_ops.LinuxHostOps(**options)

    def test_start_is_idempotent_and_stop_is_confirmed(self):
        ops = self._ops()
        self.assertEqual(ops.status()["state"], "stopped")
        self.assertTrue(ops.start_host())
        self.assertEqual(len(self.spawned), 1)
        # Already running: confirmed again, never owned twice.
        self.assertTrue(ops.start_host())
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(ops.status()["state"], "running")
        self.assertTrue(ops.stop_host())
        self.assertIn((5001, 15), self.table.signals)
        self.assertEqual(ops.read_intent(), "stopped")
        self.assertTrue(ops.verify_closed())

    def test_manual_stop_is_not_resurrected_across_restarts(self):
        ops = self._ops()
        self.assertTrue(ops.start_host())
        self.assertTrue(ops.stop_host(reason="budget"))
        # A NEW instance (the watchdog's restart) reads the SAME intent file.
        restarted = self._ops()
        self.assertEqual(restarted.read_intent(), "stopped")
        self.assertFalse(restarted.start_host())
        self.assertEqual(len(self.spawned), 1)
        # Only the operator's explicit start clears the persistent intent.
        self.assertTrue(restarted.start_host(explicit=True))
        self.assertEqual(len(self.spawned), 2)
        self.assertEqual(restarted.read_intent(), "running")

    def test_bad_pid_is_never_signalled(self):
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text("6002", encoding="ascii")
        self.table.add(6002, ["/usr/bin/sleep", "60"])
        ops = self._ops()
        self.assertFalse(ops.verify_running())
        self.assertFalse(ops.stop_host())
        killed = [sig for pid, sig in self.table.signals
                  if pid == 6002 and sig in (9, 15)]
        self.assertEqual(killed, [])
        self.assertTrue(self.table.alive == {6002})

    def test_stop_intent_write_failure_never_raises_or_signals(self):
        # The console unit's /run/um-share is read-only. If the intent cannot
        # be persisted, stop_host must NOT signal: a stop without a persistent
        # intent could be resurrected by the watchdog across restarts.
        ops = self._ops()
        self.assertTrue(ops.start_host())
        with mock.patch.object(
                share_broker_ops.os, "replace",
                side_effect=OSError(30, "Read-only file system")):
            self.assertFalse(ops.stop_host())
        self.assertEqual(ops.last_error,
                         "stop intent write failed; not signalled")
        killed = [sig for pid, sig in self.table.signals if sig in (9, 15)]
        self.assertEqual(killed, [])
        self.assertEqual(ops.read_intent(), "running")

    def test_limits_evidence_is_the_loopback_only_listener(self):
        _write_net_tcp(self.proc, 8100, "0100007F")
        observed = self._ops().verify_limits_evidence()
        self.assertIs(observed["verdict"], True)
        self.assertEqual(observed["detail"], "loopback_only")
        self.assertIs(self._ops().verify_limits(), True)

    def test_limits_evidence_rejects_a_non_loopback_listener(self):
        _write_net_tcp(self.proc, 8100, "00000000")
        observed = self._ops().verify_limits_evidence()
        self.assertIs(observed["verdict"], False)
        self.assertEqual(observed["detail"], "non_loopback_listener")

    def test_limits_evidence_rejects_a_wildcard_ipv6_listener(self):
        _write_net_tcp(self.proc, 8100, "0100007F")
        _write_net_tcp6(self.proc, 8100,
                       "00000000000000000000000000000000")
        observed = self._ops().verify_limits_evidence()
        self.assertIs(observed["verdict"], False)
        self.assertEqual(observed["detail"], "non_loopback_listener")

    def test_limits_evidence_requires_a_listener_on_the_gateway_port(self):
        _write_net_tcp(self.proc, 8101, "0100007F")
        observed = self._ops().verify_limits_evidence()
        self.assertIs(observed["verdict"], False)
        self.assertEqual(observed["detail"], "gateway_not_listening")

    def test_limits_evidence_is_unresolvable_without_proc_net(self):
        observed = self._ops().verify_limits_evidence()
        self.assertIsNone(observed["verdict"])
        self.assertEqual(observed["detail"], "limits_net_unreadable")
        self.assertIsNone(self._ops().verify_limits())

    def test_linux_backend_ops_use_proc_identity(self):
        backend_dir = self.root / "backend"
        os.environ["CONSOLE_SHARE_BACKEND_DIR"] = str(backend_dir)
        self.addCleanup(os.environ.pop, "CONSOLE_SHARE_BACKEND_DIR", None)

        def spawner(command, env):
            self.table.add(5002, command, port=8110)
            self.spawned.append(list(command))
            return SimpleNamespace(pid=5002)

        ops = console_share_production._LinuxBackendOps(
            gateway_dir=str(self.root / "chat2api"), port=8110,
            spawner=spawner, proc_root=str(self.proc),
            kill_fn=self.table.kill,
            exe_reader=lambda _path: sys.executable,
            clock=lambda: 0.0, sleeper=lambda _seconds: None)
        self.assertTrue(ops.start())
        self.assertTrue(ops.start())          # idempotent: no second spawn
        self.assertEqual(len(self.spawned), 1)
        state = ops.backend_pid_state()
        self.assertEqual(state["state"], "running")
        self.assertTrue(ops.stop())
        self.assertTrue(ops.verify_closed())
        self.assertIn((5002, 15), self.table.signals)


# ------------------------------------------------ Linux instance identity
class LinuxInstanceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        _write_proc(self.proc, 7001, [
            sys.executable, "chat2api/chat2api.py",
            "--port", "8100"])

    def _verifier(self, **overrides):
        options = dict(proc_root=str(self.proc),
                        expected_image=sys.executable,
                        expected_cmdline_marker="chat2api.py",
                        expected_port=8100,
                        exe_reader=lambda _path: sys.executable)
        options.update(overrides)
        return share_broker_ops.LinuxInstanceVerifier(**options)

    def test_identity_is_read_from_proc(self):
        ok, evidence = self._verifier().verify(7001)
        self.assertTrue(ok, evidence)
        self.assertEqual(evidence["uid"], os.stat(self.proc / "7001").st_uid)
        self.assertGreater(evidence["creation_epoch"], 0)
        for overrides in (
                {"expected_port": 8110},
                {"expected_cmdline_marker": "share_entry.py"},
                {"expected_image": "/usr/bin/other-python"}):
            with self.subTest(overrides=overrides):
                ok, _evidence = self._verifier(**overrides).verify(7001)
                self.assertFalse(ok)
        ok, _evidence = self._verifier().verify(7999)
        self.assertFalse(ok)


class _SignedEchoTransport:
    """A real-HMAC fake of the AF_UNIX instance transport.

    It verifies the SIGNED request with the protocol validator (HMAC and
    instance binding), answers with a correlated frame and records every path.
    """

    paths = []
    sent = []
    secret = "instance-test-secret"

    def __init__(self, path):
        self.path = path
        self._response = None
        type(self).paths.append(path)

    def open(self, deadline=None):
        return self

    def close(self):
        pass

    def prepare_peer(self, verifier, *, deadline, **expectations):
        pid = 7001
        return pid, verifier.verify(pid, **expectations)

    def peer_server_pid(self):
        return 7001

    def send_all(self, data, deadline=None):
        type(self).sent.append(data)
        body = bp.decode_frame(data)
        request = bp.validate_instance_request(body, self.secret)
        response = BrokerResult.ok({"host_running": True}).to_dict()
        response["request_id"] = request["request_id"]
        self._response = (json.dumps(response) + "\n").encode("utf-8")
        return len(data)

    def recv_line(self, deadline):
        return self._response


class LinuxBrokerEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        _write_proc(self.proc, 7001, [
            sys.executable, "chat2api/chat2api.py",
            "--port", "8100"])
        self.secret_file = self.root / "instance-key"
        self.secret_file.write_text("instance-test-secret", encoding="utf-8")
        if os.name == "posix":
            os.chmod(self.secret_file, 0o600)

    def _args(self, **overrides):
        values = dict(
            socket_path=str(self.root / "run" / "broker.sock"),
            key_file=str(self.root / "control-key"),
            instance_key_file=str(self.secret_file),
            registration_file=str(self.root / "registration.json"),
            instance_socket_prefix=str(self.root / "run" / "instance-"),
            projection_path=str(self.root / "projection.json"),
            broker_dir=str(self.root / "broker"),
            host_pid_file=str(self.root / "share-host.pid"),
            host_command=[sys.executable, "chat2api.py",
                           "--port", "8100"],
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_linux_entry_plan_uses_the_unix_socket_transport(self):
        plan = share_broker_ops.linux_server_plan(self._args())
        self.assertEqual(plan["transport"], "unix_socket")
        self.assertIs(plan["server_class"],
                      __import__("share_broker_server").UnixSocketBrokerServer)
        for overrides in (
                {"socket_path": "\\\\.\\pipe\\um-share-broker"},
                {"socket_path": "relative.sock"},
                {"instance_socket_prefix": "relative-"},
                {"host_command": []},
                {"host_pid_file": "relative.pid"}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    share_broker_ops.linux_server_plan(self._args(**overrides))

    def test_linux_product_server_has_the_management_dispatch(self):
        """A4/A7/A8: the Linux product class must not inherit the base
        transport's NotImplementedError dispatch. The chain test runs the REAL
        linux_main entry; this cross-platform assertion pins the class wiring."""
        import share_broker_server as srv
        self.assertIs(srv.UnixSocketBrokerServer._dispatch,
                      srv._ManagementDispatch._dispatch)
        self.assertIsNot(srv.UnixSocketBrokerServer._dispatch,
                         srv.BoundedPipeServer._dispatch)
        self.assertIs(srv.ManagementPipeServer._dispatch,
                      srv._ManagementDispatch._dispatch)

    def test_unix_instance_call_verifies_identity_before_sending(self):
        _SignedEchoTransport.paths = []
        _SignedEchoTransport.sent = []
        verifier = share_broker_ops.LinuxInstanceVerifier(
            proc_root=str(self.proc), expected_image=sys.executable,
            expected_cmdline_marker="chat2api.py", expected_port=8100,
            exe_reader=lambda _path: sys.executable)
        call = share_broker_ops.make_unix_instance_call(
            str(self.secret_file), str(self.root / "run" / "instance-"),
            verifier=verifier, expect_image=sys.executable,
            expect_cmdline_marker="chat2api.py", expect_port=8100,
            transport_factory=_SignedEchoTransport)
        result = call("instance-status", {}, "inst-1")
        self.assertTrue(result["ok"], result)
        self.assertEqual(_SignedEchoTransport.paths,
                         [str(self.root / "run" / "instance-inst-1")])
        self.assertTrue(_SignedEchoTransport.sent)
        # A wrong identity refuses BEFORE any request is sent.
        _SignedEchoTransport.sent = []
        bad = share_broker_ops.LinuxInstanceVerifier(
            proc_root=str(self.proc), expected_image=sys.executable,
            expected_cmdline_marker="another_entry.py", expected_port=8100,
            exe_reader=lambda _path: sys.executable)
        call = share_broker_ops.make_unix_instance_call(
            str(self.secret_file), str(self.root / "run" / "instance-"),
            verifier=bad, expect_image=sys.executable,
            expect_cmdline_marker="another_entry.py", expect_port=8100,
            transport_factory=_SignedEchoTransport)
        with self.assertRaises(PermissionError):
            call("instance-status", {}, "inst-1")
        self.assertEqual(_SignedEchoTransport.sent, [])

    def test_management_api_store_projection_instance_admission(self):
        config = self.root / "projection.json"
        if os.name == "posix":
            # POSIX protects a NEW key row with the file-held AES-GCM key
            # (Windows uses DPAPI and needs no file). Provisioning a TEMP key
            # file keeps this real-chain test host-independent without changing
            # anything it verifies; on Windows the env entry is never read.
            import share_secret_protection
            key_file = self.root / "share-secret.key"
            key_file.write_bytes(os.urandom(share_secret_protection.KEY_SIZE))
            os.chmod(key_file, 0o600)
            key_env = mock.patch.dict(os.environ, {
                share_secret_protection.KEY_FILE_ENV: str(key_file)})
            key_env.start()
            self.addCleanup(key_env.stop)

        class _ProjectionBroker:
            name = "projection"

            def publish(self, projection, terminate_keys=None):
                tmp = config.with_name("projection.json.tmp")
                tmp.write_text(json.dumps(projection), encoding="utf-8")
                os.replace(tmp, config)
                return BrokerResult.ok({
                    "generation": projection["config_generation"],
                    "fingerprint": share_management._fingerprint(projection),
                    "instance_id": "instance-real",
                    # S2 ACK fixture: the runtime applied the projection it
                    # acks, so its own revoke watermark is that generation.
                    "security_generation": projection["config_generation"],
                    "terminated": 0,
                })

            def snapshot(self):
                return {"name": self.name, "state": "ok"}

        manager = ShareManager(store_path=self.root / "share.db",
                               broker=BrokerClientAdapter(_ProjectionBroker()))
        created = manager.create_key("friendA")
        self.assertTrue(created["published"])
        server = ShareServer(("127.0.0.1", 0), config,
                              auth_mode="projection")
        self.addCleanup(server.server_close)
        key_id = created["key_id"]
        self.assertTrue(server.acquire(
            key_id, generation=created["desired_generation"],
            secret_version=1))
        self.assertFalse(server.acquire(
            "unknown-key", generation=created["desired_generation"],
            secret_version=1))
        revoked = manager.revoke_and_terminate_key(key_id)
        self.assertTrue(revoked["published"])
        self.assertFalse(revoked["enabled"])
        self.assertFalse(server.acquire(
            key_id, generation=revoked["desired_generation"],
            secret_version=1))


# ------------------------------------------------- cloud share seams
class CloudServiceBrokerRoutingTests(unittest.TestCase):
    """D15-4: cloud service start/stop/restart mutate ONLY through the
    restricted broker. The console unit's /run/um-share stays read-only and no
    second Popen path may exist in the console process."""

    class _ReadOnlyOps:
        def status(self):
            return {"state": "running", "healthy": True, "detail": "ok",
                    "process_ids": [4242]}

        def start_host(self, explicit=False):
            raise AssertionError("console must not spawn the gateway")

        def stop_host(self, reason="manual"):
            raise AssertionError("console must not signal the gateway")

    class _Broker:
        def __init__(self):
            self.calls = []

        def start(self):
            self.calls.append("start")
            return BrokerResult.ok({"started": True})

        def host_stop(self):
            self.calls.append("host_stop")
            return True

        def gateway_restart(self, component, budget_s):
            self.calls.append(("gateway_restart", component, budget_s))
            return BrokerResult.ok({
                "restarted": True, "component": component,
                "stopped_confirmed": True, "running_confirmed": True,
                "elapsed_s": 0.1, "maintenance_state": "idle"})

    class _ExplicitBroker:
        def __init__(self):
            self.calls = []

        def start(self, explicit=False):
            self.calls.append(("start", explicit))
            return BrokerResult.ok({"started": True})

    class _StopUnconfirmedBroker(_Broker):
        def host_stop(self):
            self.calls.append("host_stop")
            return False

    def _adapter(self):
        self.broker = self._Broker()
        return console_adapters.RuntimeServiceAdapter(
            cloud=True, ops=self._ReadOnlyOps(), broker=self.broker)

    def test_start_uses_the_restricted_broker_start(self):
        adapter = self._adapter()
        out = adapter.service_start("share-gateway", {}, None)
        self.assertEqual(out["status"], "Started")
        self.assertEqual(self.broker.calls, ["start"])

    def test_start_forwards_the_operator_explicit_intent(self):
        self.broker = self._ExplicitBroker()
        adapter = console_adapters.RuntimeServiceAdapter(
            cloud=True, ops=self._ReadOnlyOps(), broker=self.broker)
        out = adapter.service_start("share-gateway", {}, None)
        self.assertEqual(out["status"], "Started")
        self.assertEqual(self.broker.calls, [("start", True)])

    def test_stop_unconfirmed_does_not_claim_the_intent_was_persisted(self):
        self.broker = self._StopUnconfirmedBroker()
        adapter = console_adapters.RuntimeServiceAdapter(
            cloud=True, ops=self._ReadOnlyOps(), broker=self.broker)
        with self.assertRaises(console_adapters.OperationError) as caught:
            adapter.service_stop("share-gateway", {}, None)
        self.assertIn("停用意图是否持久化未确认",
                      caught.exception.message)
        self.assertNotIn("已由受限后台持久化", caught.exception.message)
        self.assertIn("只读状态复验", caught.exception.message)

    def test_stop_uses_the_restricted_broker_host_stop(self):
        adapter = self._adapter()
        out = adapter.service_stop("share-gateway", {}, None)
        self.assertEqual(out["status"], "Stopped")
        self.assertEqual(self.broker.calls, ["host_stop"])

    def test_restart_uses_the_restricted_broker_gateway_restart(self):
        adapter = self._adapter()
        out = adapter.service_restart("share-gateway", {}, None)
        self.assertEqual(out["status"], "Restarted")
        self.assertEqual(self.broker.calls,
                         [("gateway_restart", "share_gateway", 120)])

    def test_snapshot_remains_a_read_only_pid_file_read(self):
        # The read face never goes through the broker mutation verbs: it uses the
        # injected read-only host ops (the console unit can read /run/um-share).
        payload = console_adapters.service_status(
            cloud=True, ops=self._ReadOnlyOps())
        self.assertEqual(payload["services"][0]["state"], "Running")
        self.assertEqual(payload["meta"]["source"], "linux-pid-file")


class CloudShareSeamsTests(unittest.TestCase):
    def test_cloud_seams_leave_tailscale_and_wfp_unwired(self):
        class _Broker:
            def snapshot(self):
                return {"name": "broker-client", "state": "ok"}

        seams = console_share_production.production_seams(
            broker=_Broker(), cloud=True)
        for name in ("_TS_SERVICE_CONTROL", "_TS_SERVICE_STATUS",
                      "_CONNECTIVITY_OK", "_CONNECTIVITY_STATE",
                      "_FUNNEL_CONTROL", "_FUNNEL_OBSERVED"):
            self.assertNotIn(name, seams)
        self.assertIsNotNone(seams["_BROKER_OPS"])
        self.assertIsNotNone(seams["_DESIRED_STOP_READER"])

    def test_linux_stop_intent_is_persistent_and_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "share-host-desired.json"
            path.write_text(json.dumps({
                "schema_version": 1, "state": "stopped",
                "reason": "budget", "at": 1700000000}),
                encoding="utf-8")
            record = console_share_production.production_linux_desired_stop_record(
                state_dir=directory)
            self.assertEqual(record["state"], "stopped")
            self.assertEqual(record["reason"], "budget")
            self.assertIs(
                console_share_production.production_linux_desired_stop_reader(
                    state_dir=directory), True)
            # Reading never mutates the intent file.
            self.assertTrue(path.exists())
            path.write_text(json.dumps({
                "schema_version": 1, "state": "running"}), encoding="utf-8")
            self.assertIs(
                console_share_production.production_linux_desired_stop_reader(
                    state_dir=directory), False)
            path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(
                console_share_production.production_linux_desired_stop_reader(
                    state_dir=directory))


if __name__ == "__main__":
    unittest.main()
