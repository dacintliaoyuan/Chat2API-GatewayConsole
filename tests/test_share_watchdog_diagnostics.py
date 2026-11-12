"""Isolated fixed-task evidence and restricted-start error propagation.

The broker transport stays in memory. The optional real PowerShell process
defines every task/filesystem query as a fixture before executing the query.
No scheduled task, production file, network, credential or service is read.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Import-time snapshot of the real runner: sibling suites (notably
# test_console_share_actions.setUpModule) install a process-wide subprocess
# guard for the whole discovery process, but this module's fixture query
# legitimately runs a real pwsh process. Everything else still goes through
# whatever runner is installed, so the guard is not weakened.
_REAL_RUN = subprocess.run

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from console import share_actions as actions
from console import share_production as production
import share.broker_protocol as protocol
from console.operations import OperationError
from share.broker_client import BrokerClient
from share.broker_ops import WindowsHostOps
from share.broker_server import _HostOpsControlCoordinator
from share.management import BrokerClientAdapter, BrokerError


SCRIPT = r"C:\fixture\watchdog-umshare.ps1"
HOST = r"C:\Program Files\PowerShell\7\pwsh.exe"


def task_record(**changes):
    record = {"exists": True, "task_state": "Running", "execute": HOST,
              "execute_matches": True, "script_matches": True,
              "last_task_result": 0x800710e0, "last_run_at": time.time() - 30,
              "heartbeat_at": time.time() - 1, "query_error": None}
    record.update(changes)
    return record


class MemoryTransport:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    def open(self):
        pass

    def send_all(self, payload, deadline):
        self.response = protocol.encode_frame(self.coordinator.handle_management(
            protocol.decode_frame(payload)))

    def recv_line(self, deadline):
        return self.response

    def close(self):
        pass


class WatchdogEvidenceTests(unittest.TestCase):
    def host(self, value, *, returncode=0):
        self.calls = []

        def runner(args, timeout):
            self.calls.append((args, timeout))
            if isinstance(value, Exception):
                raise value
            text = value if isinstance(value, str) else json.dumps(value)
            return subprocess.CompletedProcess(args, returncode, text,
                                               "private diagnostic must not leak")
        return WindowsHostOps("umshare-live", runner=runner,
                              run_root=r"C:\fixture", watchdog_script=SCRIPT)

    def gate(self, host):
        inner = SimpleNamespace(mgmt_secret="fixture-management-secret",
                                replay=protocol.ReplayRegistry())
        coordinator = _HostOpsControlCoordinator(inner, host)
        client = BrokerClient(lambda: MemoryTransport(coordinator),
                              "fixture-management-secret")
        return production._ProductionBrokerOps(BrokerClientAdapter(client))

    def layer(self, value):
        with patch.object(actions, "_BROKER_OPS", self.gate(self.host(value))):
            return actions._diag_watchdog()

    def test_running_with_old_nonzero_result_is_currently_running(self):
        layer = self.layer(task_record())
        self.assertEqual((layer["state"], layer["verdict"]),
                         ("running", "confirmed"))
        evidence = layer["evidence"]
        self.assertEqual(evidence["task_name"], "umshare-watchdog")
        self.assertEqual(evidence["last_task_result"], 0x800710e0)
        self.assertEqual(evidence["last_task_result_hex"], "0x800710E0")
        self.assertEqual(evidence["heartbeat_source"], "watchdog_log_mtime")
        self.assertEqual(evidence["script_path"], SCRIPT)
        self.assertIsNone(layer["reason"])
        self.assertIsInstance(evidence["queried_at"], int)

    def test_explicit_missing_task_is_absent(self):
        layer = self.layer(task_record(exists=False,
                                      query_error="watchdog_task_missing"))
        self.assertEqual(layer["state"], "absent")
        self.assertEqual(layer["reason"], "watchdog_task_missing")
        self.assertIsNone(layer["evidence"]["task_state"])
        self.assertIsNone(layer["evidence"]["last_task_result"])

    def test_ready_retains_specific_exit_result(self):
        layer = self.layer(task_record(task_state="Ready",
                                      last_task_result=0x80070002))
        self.assertEqual(layer["state"], "stopped")
        self.assertEqual(layer["evidence"]["task_state"], "Ready")
        self.assertEqual(layer["evidence"]["last_task_result_hex"], "0x80070002")

    def test_queued_is_waiting_and_never_reported_as_stopped(self):
        layer = self.layer(task_record(task_state="Queued"))
        self.assertEqual((layer["state"], layer["verdict"]), ("queued", "confirmed"))
        self.assertEqual(layer["reason"], "watchdog_task_queued")
        self.assertEqual(layer["evidence"]["task_state"], "Queued")

    def test_execute_and_script_mismatch_do_not_hide_actual_task_state(self):
        for change in ({"execute_matches": False,
                        "execute": r"C:\private\different-host.exe"},
                       {"script_matches": False}):
            with self.subTest(change=change):
                layer = self.layer(task_record(**change))
                self.assertEqual(layer["state"], "unconfirmed")
                self.assertEqual(layer["reason"], "watchdog_execution_mismatch")
                self.assertEqual(layer["evidence"]["task_state"], "Running")
                self.assertNotIn(r"C:\private", json.dumps(layer))

    def test_query_failure_never_becomes_task_missing(self):
        for value in (task_record(exists=None, query_error="watchdog_query_failed"),
                      PermissionError("private permission diagnostic"),
                      "not JSON"):
            with self.subTest(kind=type(value).__name__):
                layer = self.layer(value)
                self.assertEqual(layer["state"], "unavailable")
                self.assertIsNone(layer["evidence"]["exists"])
                self.assertNotIn("private", json.dumps(layer))

    def test_task_info_refusal_preserves_the_known_task_without_claiming_health(self):
        layer = self.layer(task_record(query_error="watchdog_task_info_unavailable"))
        self.assertEqual(layer["state"], "unavailable")
        self.assertIs(layer["evidence"]["exists"], True)
        self.assertEqual(layer["evidence"]["task_state"], "Running")

    def test_arbitrary_execute_text_is_not_exposed(self):
        layer = self.layer(task_record(execute_matches=False,
                                      execute="pwsh.exe -EncodedCommand SECRET"))
        self.assertIsNone(layer["evidence"]["execute"])
        self.assertNotIn("SECRET", json.dumps(layer))

    def test_old_broker_and_access_refusal_are_unavailable(self):
        for code, reason in (("bad_request", "watchdog_capability_missing"),
                             ("capability_missing", "watchdog_capability_missing"),
                             ("access_denied", "watchdog_query_failed"),
                             ("not_installed", "watchdog_broker_unavailable")):
            with self.subTest(code=code):
                broker = SimpleNamespace(host_evidence=lambda check:
                    protocol.BrokerResult.err(code, "private raw stderr"))
                with patch.object(actions, "_BROKER_OPS",
                                  production._ProductionBrokerOps(broker)):
                    layer = actions._diag_watchdog()
                self.assertEqual(layer["state"], "unavailable")
                self.assertEqual(layer["reason"], reason)
                self.assertIsNone(layer["evidence"]["exists"])
                self.assertNotIn("stderr", json.dumps(layer))

    def test_missing_optional_heartbeat_and_result_stay_null(self):
        layer = self.layer(task_record(heartbeat_at=None, last_run_at=None,
                                      last_task_result=None))
        self.assertEqual(layer["state"], "running")
        for key in ("heartbeat_at", "heartbeat_source", "last_run_at",
                    "last_task_result", "last_task_result_hex"):
            self.assertIsNone(layer["evidence"][key])

    def test_only_fixed_check_enum_crosses_management_wire(self):
        self.assertEqual(protocol.validate_host_evidence_fields(
            {"check": "watchdog"}), {"check": "watchdog"})
        for extra in ({"task": "other"}, {"path": "C:\\private"},
                      {"command": "anything"}):
            with self.assertRaises(protocol.ProtocolError):
                protocol.validate_host_evidence_fields({"check": "watchdog", **extra})
        self.layer(task_record())
        args, timeout = self.calls[0]
        self.assertEqual(args[:3], ["pwsh", "-NoProfile", "-NonInteractive"])
        self.assertGreater(timeout, 0)
        script = args[-1]
        self.assertIn("Get-ScheduledTask -TaskName $taskName -TaskPath", script)
        for mutation in ("Start-ScheduledTask", "Set-ScheduledTask",
                         "Register-ScheduledTask", "Stop-Process"):
            self.assertNotIn(mutation, script)

    @unittest.skipUnless(os.name == "nt", "PowerShell fixture is Windows-only")
    def test_actual_query_script_uses_fixture_queries_without_live_access(self):
        self.host(task_record()).read_watchdog()
        query = self.calls[0][0][-1]
        prefix = r'''
function Get-ScheduledTask {
    param($TaskName,$TaskPath,$ErrorAction)
    if ($TaskName -ne 'umshare-watchdog' -or $TaskPath -ne '\') { throw 'wrong fixed task' }
    [pscustomobject]@{State='Running';Actions=@([pscustomobject]@{
        Execute=(Join-Path $PSHOME 'pwsh.exe');
        Arguments='-NoProfile -ExecutionPolicy Bypass -File "C:\fixture\watchdog-umshare.ps1"'})}
}
function Get-ScheduledTaskInfo {
    param($TaskName,$TaskPath,$ErrorAction)
    [pscustomobject]@{LastTaskResult=2147942402;LastRunTime=(Get-Date)}
}
function Get-Item {
    param($LiteralPath,$ErrorAction)
    [pscustomobject]@{LastWriteTimeUtc=[DateTime]::UtcNow}
}
'''
        executable = Path(os.environ["ProgramFiles"]) / "PowerShell/7/pwsh.exe"
        if not executable.is_file():
            self.skipTest("The standard PS7 host is not installed")
        with tempfile.TemporaryDirectory(prefix="task-evidence-fixture-") as directory:
            path = Path(directory) / "query.ps1"
            path.write_text(prefix + query, encoding="utf-8")
            result = _REAL_RUN([str(executable), "-NoProfile", "-NonInteractive",
                                "-File", str(path)], capture_output=True,
                               text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertIs(record["execute_matches"], True)
        self.assertIs(record["script_matches"], True)
        self.assertEqual(record["last_task_result"], 0x80070002)
        self.assertNotIn("Arguments", result.stdout)


class RestrictedStartErrorTests(unittest.TestCase):
    def test_stable_broker_error_code_reaches_open_layer(self):
        for code in ("access_denied", "blocked", "not_installed", "unconfirmed"):
            with self.subTest(code=code):
                client = SimpleNamespace(start=lambda:
                    protocol.BrokerResult.err(code, "SECRET raw command stderr"))
                broker = BrokerClientAdapter(client)
                gate = production._ProductionBrokerOps(broker)
                gate.verify_running = lambda: False
                gate.verify_closed = lambda: True
                with patch.object(actions, "_BROKER_OPS", gate), \
                        self.assertRaises(OperationError) as caught:
                    actions._open_backend(None, actions._OpenLedger(), {"backend": 1}, .01)
                self.assertEqual(caught.exception.code, "share_broker_" + code)
                self.assertIn("stage=broker_start", caught.exception.message)
                self.assertNotIn("SECRET", caught.exception.message)
                self.assertNotIn("stderr", caught.exception.message)

    def test_non_confirming_start_has_a_distinct_safe_stage(self):
        client = SimpleNamespace(start=lambda: protocol.BrokerResult.ok({}))
        gate = production._ProductionBrokerOps(BrokerClientAdapter(client))
        with self.assertRaises(OperationError) as caught:
            gate.start_host()
        self.assertEqual(caught.exception.code, "share_broker_unconfirmed")
        self.assertIn("stage=broker_start_confirmation", caught.exception.message)

    def test_share_open_preserves_specific_error_through_rollback(self):
        client = SimpleNamespace(
            status=lambda: protocol.BrokerResult.ok({}),
            start=lambda: protocol.BrokerResult.err("access_denied", "SECRET raw backend output"))
        gate = production._ProductionBrokerOps(BrokerClientAdapter(client))
        gate.verify_running = lambda: False
        gate.verify_closed = lambda: True
        with patch.object(actions, "_BROKER_OPS", gate), \
                patch.object(actions, "_open_share_backend"), \
                patch.object(actions, "_open_ts_service"), \
                patch.object(actions, "_open_connectivity"), \
                patch.object(actions, "_open_rollback", return_value=[]), \
                self.assertRaises(OperationError) as caught:
            actions.share_open("share", {}, None)
        self.assertEqual(caught.exception.code, "share_broker_access_denied")
        self.assertIn("stage=broker_start", caught.exception.message)
        self.assertIn("只撤销本次拥有的改动", caught.exception.message)
        self.assertNotIn("SECRET", caught.exception.message)

    def test_legacy_broker_error_boolean_contract_is_preserved(self):
        def fail():
            raise BrokerError("private legacy command")
        self.assertIs(production._ProductionBrokerOps(
            SimpleNamespace(start=fail)).start_host(), False)

    def test_existing_broker_error_construction_remains_compatible(self):
        error = BrokerError("old message")
        self.assertEqual(str(error), "old message")
        self.assertEqual(error.args, ("old message",))
        self.assertIsNone(error.code)
        self.assertIsNone(error.stage)


if __name__ == "__main__":
    unittest.main()
