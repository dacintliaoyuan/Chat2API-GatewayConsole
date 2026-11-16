# -*- coding: utf-8 -*-
"""#72: the cutover gate plans the cloud smoke and never executes it.

The gate owns three gates: local candidate parity, an operator-only cloud smoke
plan, and the rollback drill (plan-only unless ``--execute`` is explicit).  These
tests drive the real CLI in-process; they patch only ``subprocess.run`` when the
test is about "this must NOT execute", so no cloud host, curl or production path
is touched.
"""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "um-cloud-deploy"
GATE = DEPLOY / "cutover_gate.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "cutover_gate_under_test", GATE)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {GATE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_json(gate, argv):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = gate.main(argv)
    return code, json.loads(output.getvalue())


class CutoverGateTests(unittest.TestCase):
    def test_help_lists_the_three_cutover_gates(self):
        gate = _load_gate()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as caught:
                gate.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        text = output.getvalue()
        for name in ("candidate-parity", "cloud-smoke", "rollback-drill"):
            self.assertIn(name, text)

    def test_cloud_smoke_is_planned_and_never_executes_a_command(self):
        gate = _load_gate()
        calls = []

        def forbidden(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("cloud smoke executed a command")

        with mock.patch.object(gate.subprocess, "run", forbidden):
            code, document = _run_json(gate, [
                "--json", "cloud-smoke",
                "--base-url", "http://127.0.0.1:8003",
                "--public-url", "https://share.example.test",
                "--share-key-env", "UM_SHARE_KEY",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        smoke = document["cloud_smoke"]
        self.assertEqual(smoke["status"], "planned")
        self.assertFalse(smoke["executed"])
        self.assertEqual(smoke["base_url"], "http://127.0.0.1:8003")
        names = {check["name"] for check in smoke["checks"]}
        for name in ("ready", "health", "models-with-key",
                      "models-without-key", "chat-nonstream",
                      "chat-stream-sse", "sse-client-disconnect",
                      "ledger-row", "key-revoke-in-flight",
                      "public-models-with-key", "public-health"):
            self.assertIn(name, names)
        commands = "\n".join(check["command"]
                            for check in smoke["checks"])
        self.assertIn("$UM_SHARE_KEY", commands)
        self.assertIn("$BASE_URL/v1/models", commands)
        self.assertIn("${PUBLIC_URL%/}/v1/models", commands)
        self.assertIn("data: [DONE]",
                      "\n".join(check["expected"]
                                for check in smoke["checks"]))
        self.assertNotIn("Bearer um-share-", commands)
        expected = {check["name"]: check["expected"]
                    for check in smoke["checks"]}
        self.assertEqual(expected["ready"], "200")
        self.assertEqual(expected["models-without-key"], "401 or 403")
        self.assertIn("choices[]", expected["chat-nonstream"])

    def test_rollback_drill_is_planned_unless_execute_is_explicit(self):
        gate = _load_gate()
        calls = []

        def forbidden(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("rollback drill executed without --execute")

        with mock.patch.object(gate.subprocess, "run", forbidden):
            code, document = _run_json(gate, [
                "--json", "rollback-drill", "--to", "python-release",
                "--health-port", "8100",
                "--current-health-port", "8003",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        drill = document["rollback_drill"]
        self.assertEqual(drill["status"], "planned")
        self.assertFalse(drill["executed"])
        command = drill["command"]
        self.assertIn("--to", command)
        self.assertIn("python-release", command)
        self.assertIn("--health-port", command)
        self.assertIn("8100", command)
        self.assertIn("--current-health-port", command)
        self.assertIn("8003", command)
        self.assertNotIn("--execute", command)
        positions = [index for index, value in enumerate(command)
                     if value == "--data-dir"]
        self.assertEqual(
            [command[index + 1] for index in positions],
            ["/var/lib/um-share", "/var/lib/um-share-console",
             "/etc/um-share"])

    def test_rollback_drill_executes_only_with_the_explicit_flag(self):
        gate = _load_gate()
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout="drill ok\n",
                                  stderr="")

        with mock.patch.object(gate.subprocess, "run", fake_run):
            code, document = _run_json(gate, [
                "--json", "rollback-drill", "--to", "python-release",
                "--execute",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("--execute", calls[0][0])
        self.assertEqual(document["rollback_drill"]["status"], "passed")
        self.assertTrue(document["rollback_drill"]["executed"])

    def test_candidate_parity_runs_the_local_parity_test(self):
        gate = _load_gate()
        code, document = _run_json(gate, [
            "--python", sys.executable, "--json",
            "candidate-parity", "--skip-rust",
        ])
        self.assertEqual(code, 0)
        parity = document["candidate_parity"]
        self.assertEqual(parity["status"], "passed")
        self.assertTrue(parity["executed"])
        self.assertEqual(parity["python"]["exit_code"], 0)
        self.assertIsNone(parity["rust"])
        self.assertIn("tests.test_cutover_parity",
                      " ".join(parity["python"]["command"]))

    def test_all_writes_three_gate_evidence(self):
        gate = _load_gate()

        def fake_run(command, **kwargs):
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        with tempfile.TemporaryDirectory(prefix="cutover-evidence-") as tmp:
            evidence = Path(tmp) / "gate.json"
            with mock.patch.object(gate.subprocess, "run", fake_run):
                code, document = _run_json(gate, [
                    "--json", "--evidence", str(evidence),
                    "all", "--to", "python-release", "--skip-rust",
                ])
            self.assertEqual(code, 0)
            committed = json.loads(evidence.read_text(encoding="utf-8"))
        self.assertEqual(committed, document)
        self.assertEqual(committed["schema"], "um-cutover-gate-v1")
        self.assertEqual(committed["candidate_parity"]["status"], "passed")
        self.assertEqual(committed["cloud_smoke"]["status"], "planned")
        self.assertFalse(committed["cloud_smoke"]["executed"])
        self.assertEqual(committed["rollback_drill"]["status"], "planned")
        self.assertFalse(committed["rollback_drill"]["executed"])

    def test_the_gate_is_part_of_the_release_payload(self):
        verify = (DEPLOY / "verify-release.ps1").read_text(encoding="utf-8")
        self.assertIn("'um-cloud-deploy/cutover_gate.py'", verify)
        self.assertIn("'um-cloud-deploy/cutover-gate.sh'", verify)
        self.assertTrue((DEPLOY / "cutover-gate.sh").is_file())


if __name__ == "__main__":
    unittest.main()
