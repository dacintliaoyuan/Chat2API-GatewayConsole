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
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        for name in ("candidate-parity", "console-parity", "cloud-smoke",
                      "rollback-drill", "console-rollback-drill"):
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

    def test_all_writes_four_gate_evidence(self):
        gate = _load_gate()
        payloads = _console_payloads()
        python = _FakeConsole(payloads)
        rust = _FakeConsole(payloads)

        def fake_run(command, **kwargs):
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        try:
            with tempfile.TemporaryDirectory(prefix="cutover-evidence-") as tmp:
                evidence = Path(tmp) / "gate.json"
                with mock.patch.object(gate.subprocess, "run", fake_run):
                    code, document = _run_json(gate, [
                        "--json", "--evidence", str(evidence),
                        "all", "--to", "python-release", "--skip-rust",
                        "--python-url", python.url, "--rust-url", rust.url,
                    ])
                self.assertEqual(code, 0)
                committed = json.loads(evidence.read_text(encoding="utf-8"))
        finally:
            python.close()
            rust.close()
        self.assertEqual(committed, document)
        self.assertEqual(committed["schema"], "um-cutover-gate-v1")
        self.assertEqual(committed["candidate_parity"]["status"], "passed")
        self.assertEqual(committed["console_parity"]["status"], "passed")
        self.assertEqual(committed["cloud_smoke"]["status"], "planned")
        self.assertFalse(committed["cloud_smoke"]["executed"])
        self.assertEqual(committed["rollback_drill"]["status"], "planned")
        self.assertFalse(committed["rollback_drill"]["executed"])
        self.assertEqual(committed["console_rollback_drill"]["status"],
                         "planned")
        self.assertFalse(committed["console_rollback_drill"]["executed"])

    def test_the_gate_is_part_of_the_release_payload(self):
        verify = (DEPLOY / "verify-release.ps1").read_text(encoding="utf-8")
        self.assertIn("'um-cloud-deploy/cutover_gate.py'", verify)
        self.assertIn("'um-cloud-deploy/cutover-gate.sh'", verify)
        self.assertTrue((DEPLOY / "cutover-gate.sh").is_file())


def _console_payloads():
    return {
        "/api/summary": (200, {
            "generatedAt": "2026-11-17T10:00:00+08:00",
            "role": "cloud",
            "assembly": {"role": "cloud", "services": ["share"]},
            "services": {},
            "_generated_epoch": 100.0,
            "_cached_age_seconds": 0.0,
        }),
        "/api/console/share-management/keys": (200, {
            "keys": [{
                "id": "key-alpha", "name": "alpha", "masked": "um-****",
                "enabled": True, "max_concurrency": 8, "row_version": 3,
                "created_at": "2026-11-16T00:00:01.000000Z",
            }],
        }),
        "/api/console/share-usage": (200, {
            "requests": 6,
            "checkedAt": "2026-11-17T10:00:00+08:00",
            "keys": [{"key_id": "key-alpha", "requests": 4}],
        }),
        "/api/console/share-usage/requests": (200, {
            "rows": [{"requestId": "r1", "keyId": "key-alpha"}],
            "checkedAt": "2026-11-17T10:00:00+08:00",
        }),
        "/api/console/logs": (503, {
            "error": "capability_not_assembled", "capability": "logs",
            "role": "cloud",
        }),
        "/api/console/share-management/display-session": (200, {
            "available": False, "url": None, "reason": "display_disabled",
            "expires_at": None, "mode": "novnc",
            "operator": {"owned_by_me": False, "owner_present": False,
                         "generation": 0, "stale": False},
        }),
    }


class _ConsoleHandler(BaseHTTPRequestHandler):
    payloads = {}
    requests = []

    def do_GET(self):
        type(self).requests.append(self.path)
        status, body = type(self).payloads.get(
            self.path, (404, {"error": "unknown path"}))
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


class _FakeConsole:
    def __init__(self, payloads):
        handler = type("Handler", (_ConsoleHandler,),
                       {"payloads": payloads, "requests": []})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.requests = handler.requests
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class ConsoleParityTests(unittest.TestCase):
    def test_console_parity_diffs_the_five_read_only_surfaces(self):
        gate = _load_gate()
        payloads = _console_payloads()
        python = _FakeConsole(payloads)
        rust = _FakeConsole(payloads)
        try:
            result = gate.console_parity(
                python.url, rust.url, timeout=2)
        finally:
            python.close()
            rust.close()
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(
            [surface["name"] for surface in result["surfaces"]],
            ["summary", "keys", "usage", "logs", "display"])
        self.assertIn(
            "/api/console/share-usage/requests",
            python.requests)
        self.assertIn(
            "/api/console/share-usage/requests",
            rust.requests)

    def test_console_parity_fails_on_a_surface_difference(self):
        gate = _load_gate()
        payloads = _console_payloads()
        changed = json.loads(json.dumps(payloads))
        changed["/api/console/share-management/keys"][1]["keys"][0][
            "name"] = "different"
        python = _FakeConsole(payloads)
        rust = _FakeConsole(changed)
        try:
            result = gate.console_parity(
                python.url, rust.url, timeout=2)
        finally:
            python.close()
            rust.close()
        self.assertEqual(result["status"], "failed")
        failed = [surface for surface in result["surfaces"]
                  if surface["status"] == "failed"]
        self.assertEqual([surface["name"] for surface in failed], ["keys"])
        self.assertTrue(failed[0]["differences"])

    def test_console_parity_starts_the_rust_candidate_with_the_env_file(self):
        gate = _load_gate()
        payloads = _console_payloads()
        python = _FakeConsole(payloads)
        with tempfile.TemporaryDirectory(prefix="console-parity-env-") as tmp:
            root = Path(tmp)
            env_file = root / "console.env"
            env_file.write_text(
                "UM_SHARE_ROLE=cloud\n"
                "USAGESRV_PORT=8002\n"
                "SHARE_STORE_FILE=/var/lib/um-share-console/share-store.db\n"
                "CONSOLE_SHARE_BACKEND_DIR=/var/lib/um-share-console/share-backend\n",
                encoding="utf-8")
            env_out = root / "candidate-env.json"
            payloads_out = root / "candidate-payloads.json"
            payloads_out.write_text(
                json.dumps(payloads, ensure_ascii=False), encoding="utf-8")
            candidate = root / "candidate.py"
            candidate.write_text(
                "import json, os\n"
                "from http.server import BaseHTTPRequestHandler, "
                "ThreadingHTTPServer\n"
                "with open(os.environ['FAKE_ENV_OUT'], 'w', encoding='utf-8') "
                "as handle:\n"
                "    json.dump(dict(os.environ), handle)\n"
                "with open(os.environ['FAKE_PAYLOADS'], encoding='utf-8') "
                "as handle:\n"
                "    payloads = json.load(handle)\n"
                "class Handler(BaseHTTPRequestHandler):\n"
                "    def do_GET(self):\n"
                "        status, body = payloads.get(self.path, "
                "(404, {'error': 'unknown path'}))\n"
                "        data = json.dumps(body).encode()\n"
                "        self.send_response(status)\n"
                "        self.send_header('Content-Length', str(len(data)))\n"
                "        self.end_headers()\n"
                "        self.wfile.write(data)\n"
                "    def log_message(self, *args):\n"
                "        pass\n"
                "server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)\n"
                "print('um-console listening on http://127.0.0.1:%d' % "
                "server.server_address[1], flush=True)\n"
                "server.serve_forever()\n",
                encoding="utf-8", newline="\n")
            env = {"FAKE_ENV_OUT": str(env_out),
                   "FAKE_PAYLOADS": str(payloads_out)}
            try:
                with mock.patch.dict(gate.os.environ, env):
                    result = gate.console_parity(
                        python.url, None, timeout=2,
                        rust_console_command=[
                            sys.executable, str(candidate)],
                        env_file=str(env_file))
            finally:
                python.close()
            self.assertEqual(result["status"], "passed", result)
            observed = json.loads(env_out.read_text(encoding="utf-8"))
        self.assertEqual(observed["SHARE_STORE_FILE"],
                         "/var/lib/um-share-console/share-store.db")
        self.assertEqual(observed["CONSOLE_SHARE_BACKEND_DIR"],
                         "/var/lib/um-share-console/share-backend")
        self.assertEqual(observed["USAGESRV_PORT"], "8002")

    def test_all_fails_when_console_parity_fails(self):
        gate = _load_gate()
        payloads = _console_payloads()
        changed = json.loads(json.dumps(payloads))
        changed["/api/summary"][1]["role"] = "local"
        python = _FakeConsole(payloads)
        rust = _FakeConsole(changed)

        def fake_run(command, **kwargs):
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        try:
            with mock.patch.object(gate.subprocess, "run", fake_run):
                code, document = _run_json(gate, [
                    "--json", "all", "--to", "python-release", "--skip-rust",
                    "--python-url", python.url, "--rust-url", rust.url,
                ])
        finally:
            python.close()
            rust.close()
        self.assertEqual(code, 1)
        self.assertEqual(document["console_parity"]["status"], "failed")
        self.assertEqual(document["candidate_parity"]["status"], "passed")

    def test_all_passes_when_every_executed_gate_passes(self):
        gate = _load_gate()
        payloads = _console_payloads()
        python = _FakeConsole(payloads)
        rust = _FakeConsole(payloads)

        def fake_run(command, **kwargs):
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        try:
            with mock.patch.object(gate.subprocess, "run", fake_run):
                code, document = _run_json(gate, [
                    "--json", "all", "--to", "python-release", "--skip-rust",
                    "--python-url", python.url, "--rust-url", rust.url,
                ])
        finally:
            python.close()
            rust.close()
        self.assertEqual(code, 0, document)
        self.assertEqual(document["console_parity"]["status"], "passed")

    def test_console_rollback_drill_plans_the_8096_and_8002_health_gates(self):
        gate = _load_gate()
        code, document = _run_json(gate, [
            "--json", "console-rollback-drill",
        ])
        self.assertEqual(code, 0)
        drill = document["console_rollback_drill"]
        self.assertEqual(drill["status"], "planned")
        self.assertFalse(drill["executed"])
        steps = "\n".join(" ".join(step["command"])
                          for step in drill["steps"])
        self.assertIn("um-share-console-python.service", steps)
        self.assertIn("um-share-console.service", steps)
        self.assertIn("8096", steps)
        self.assertIn("8002", steps)
        self.assertIn("http://127.0.0.1:8003/ready", steps)
        self.assertIn("http://127.0.0.1:8096/health", steps)
        self.assertIn("http://127.0.0.1:8002/health", steps)

    def test_console_rollback_drill_restores_rust_when_python_fails(self):
        gate = _load_gate()
        calls = []

        def fake_systemctl(command, cwd):
            calls.append(command)
            return {"command": command, "exit_code": 0,
                    "stdout": "", "stderr": ""}

        def fake_probe(url, timeout):
            if url.endswith(":8096/health"):
                return 500
            return 200

        with mock.patch.object(gate, "_run", fake_systemctl), \
                mock.patch.object(gate, "_probe_http", fake_probe), \
                mock.patch.object(gate, "_wait_port",
                                  lambda port, timeout: True):
            document = gate.console_rollback_drill(execute=True)
        self.assertEqual(document["status"], "failed")
        self.assertTrue(document["restored"])
        commands = [" ".join(step["command"]) for step in document["steps"]]
        self.assertIn("systemctl start um-share-console.service", commands)
        self.assertEqual(commands[-2],
                         "curl http://127.0.0.1:8002/health")
        self.assertEqual(commands[-1],
                         "curl http://127.0.0.1:8003/ready")


if __name__ == "__main__":
    unittest.main()
