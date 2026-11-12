"""Read-only broker preflight through the real adapter and open handler."""
import json
import http.client
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from console import share_actions as orch
from console import share_production as prod
from console.operations import OperationError
from share.management import BrokerClientAdapter


class BrokerPreflightTests(unittest.TestCase):
    def test_unavailable_control_never_starts_or_logs_in_backend(self):
        client = mock.Mock()
        client.status.return_value = SimpleNamespace(
            ok=False, error_code="deadline_exceeded")
        gate = prod.production_broker_ops(broker=BrokerClientAdapter(client))
        with mock.patch.object(orch, "_BROKER_OPS", gate), \
             mock.patch.object(orch, "_open_share_backend") as backend, \
             mock.patch.object(orch, "_open_ts_service") as ts, \
             mock.patch.object(orch, "_write_intent") as intent:
            backend.side_effect = AssertionError("8110 was touched")
            with self.assertRaises(OperationError) as caught:
                orch.share_open("um-share", {})
        self.assertEqual(caught.exception.code, "share_broker_unavailable")
        self.assertIn("Recover-ShareControl.bat", caught.exception.message)
        client.status.assert_called_once_with()
        backend.assert_not_called()
        ts.assert_not_called()
        intent.assert_not_called()

    def test_confirmed_control_is_read_only(self):
        client = mock.Mock()
        client.status.return_value = SimpleNamespace(ok=True, result={})
        gate = prod.production_broker_ops(broker=BrokerClientAdapter(client))
        self.assertIsNone(gate.ensure_control_ready())
        self.assertEqual(client.mock_calls, [mock.call.status()])

    def test_bad_evidence_is_never_ready_or_leaked(self):
        for snapshot in ({}, None, {"state": "unknown"},
                         {"state": "unavailable", "error_code": "SECRET"},
                         {"state": "ok", "broker": None}):
            with self.subTest(snapshot=snapshot):
                adapter = mock.create_autospec(BrokerClientAdapter, instance=True)
                adapter.snapshot.return_value = snapshot
                with self.assertRaises(OperationError) as caught:
                    prod.production_broker_ops(broker=adapter).ensure_control_ready()
                self.assertEqual(caught.exception.code,
                                 "share_broker_unavailable")
                self.assertNotIn("SECRET", caught.exception.message)

    def test_transport_exception_has_safe_recovery_instruction(self):
        adapter = mock.create_autospec(BrokerClientAdapter, instance=True)
        adapter.snapshot.side_effect = OSError("SECRET")
        with self.assertRaises(OperationError) as caught:
            prod.production_broker_ops(broker=adapter).ensure_control_ready()
        self.assertEqual(caught.exception.code, "share_broker_unavailable")
        self.assertNotIn("SECRET", caught.exception.message)

    def test_unwired_loader_refuses_before_any_transport(self):
        """E19-1-03: the seam wiring is NOT the loader wiring.

        ``console_adapters.wire_share_production_seams()`` binds the
        orchestration seams from ``production_seams()``; the broker
        CONSTRUCTION loader is a separate, host-owned injection
        (``configure_broker_loader``). With no loader configured the honest
        refusal is ``share_broker_unwired`` — never a silent fallback, never a
        None gate that raises AttributeError at the call site, and never an
        uncaught exception that surfaces as a 500.
        """
        from console import adapters as console_adapters
        saved_loader = prod._BROKER_LOADER
        self.addCleanup(prod.configure_broker_loader, saved_loader)
        prod.configure_broker_loader(None)
        saved = {key: value for key, value in vars(orch).items()
                 if key.startswith("_") and key.isupper()}
        try:
            with mock.patch.object(orch, "_BROKER_OPS", None):
                console_adapters.wire_share_production_seams()
                # The unwired seam stays unwired: the wiring failed closed.
                self.assertIsNone(orch._BROKER_OPS)
                with self.assertRaises(OperationError) as caught:
                    orch._broker_ops().ensure_control_ready()
            # The production gate itself carries the deployment pointer: the
            # same refusal from the layer that owns the loader contract.
            with self.assertRaises(OperationError) as gate_caught:
                prod.production_broker_ops().ensure_control_ready()
        finally:
            for key, value in saved.items():
                setattr(orch, key, value)
        self.assertEqual(caught.exception.code, "share_broker_unwired")
        self.assertEqual(gate_caught.exception.code, "share_broker_unwired")
        self.assertIn("share-management.json", gate_caught.exception.message)

    def test_default_assembly_preflight_uses_real_loader_and_protocol(self):
        from console import adapters as console_adapters
        import usagesrv
        import share.broker_client as share_broker_client
        import share.broker_server as share_broker_server
        calls = []

        class Transport:
            def __init__(self, pipe_name):
                self.pipe_name = pipe_name

            def open(self):
                return self

            def send_all(self, data, deadline=None):
                request = json.loads(data)
                calls.append(request["action"])
                self.reply = json.dumps({
                    "v": 2, "ok": True, "error_code": "ok", "result": {},
                    "request_id": request["request_id"]}).encode() + b"\n"

            def recv_line(self, deadline):
                return self.reply

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "management.json"
            config.write_text(json.dumps({
                "schema_version": 1, "enabled": True,
                "pipe_name": r"\\.\pipe\isolated-preflight",
                "control_key_file": str(Path(td) / "test-key")}),
                encoding="utf-8")
            saved = {key: value for key, value in vars(orch).items()
                     if key.startswith("_") and key.isupper()}
            saved_loader = prod._BROKER_LOADER
            self.addCleanup(prod.configure_broker_loader, saved_loader)
            try:
                # The HOST wires the loader (usagesrv.get_console_manager does
                # exactly this before assembling the seams); the test wires the
                # same production loader explicitly instead of relying on the
                # module to reach back into the host.
                prod.configure_broker_loader(
                    usagesrv._load_share_management_deployment)
                with mock.patch.object(usagesrv, "SHARE_MANAGEMENT_CONFIG_FILE", str(config)), \
                     mock.patch.object(share_broker_server, "read_control_key", return_value="test-key"), \
                     mock.patch.object(share_broker_client, "NamedPipeBrokerTransport", Transport):
                    console_adapters.wire_share_production_seams()
                    self.assertIsNotNone(orch._BROKER_OPS)
                    self.assertEqual(calls, [])
                    orch._BROKER_OPS.ensure_control_ready()
                self.assertEqual(calls, ["status"])
            finally:
                for key, value in saved.items():
                    setattr(orch, key, value)


class ControlStatusRouteTests(unittest.TestCase):
    def setUp(self):
        import usagesrv
        import share.broker_client as share_broker_client
        self.usagesrv = usagesrv
        self.client = mock.Mock()
        self.client.status.return_value = SimpleNamespace(ok=True, result={})
        # E19-1-03: the route resolves the gate through
        # ``_default_broker`` -> the HOST-owned construction loader. The test
        # must inject that loader explicitly: leaving the module global at
        # whatever a previous test file left behind would let the route reach a
        # real deployment config (a real kernel or a stray assertion surfaces as
        # an unrelated 500). The injected loader returns the controlled adapter.
        self.saved_loader = prod._BROKER_LOADER
        self.addCleanup(prod.configure_broker_loader, self.saved_loader)
        prod.configure_broker_loader(
            lambda: BrokerClientAdapter(self.client))
        for owner, name, value in (
            (usagesrv, "get_share_manager", mock.Mock(
                side_effect=AssertionError("control status must not construct a store"))),
            (share_broker_client.NamedPipeBrokerTransport, "_declare_real_kernel",
             mock.Mock(side_effect=AssertionError("real kernel forbidden"))),
        ):
            patcher = mock.patch.object(owner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = usagesrv.get_share_manager
        self.kernel = share_broker_client.NamedPipeBrokerTransport._declare_real_kernel
        self.server = usagesrv.ThreadingHTTPServer(("127.0.0.1", 0), usagesrv.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.store.assert_not_called()
        self.kernel.assert_not_called()

    def request(self, method="GET", origin=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {"Host": "127.0.0.1:8001"}
        if method != "GET":
            headers["X-Requested-With"] = "XMLHttpRequest"
        if origin:
            headers["Origin"] = origin
        try:
            connection.request(method, "/api/console/share-management/control-status", headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read()), response.getheader("Cache-Control")
        finally:
            connection.close()

    def test_fixed_loopback_read_needs_no_secret_and_uses_only_status(self):
        code, payload, cache = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(payload, {"responsive": True})
        self.assertIn("no-store", cache)
        self.assertEqual(self.client.mock_calls, [mock.call.status()])

    def test_failed_read_returns_safe_unconfirmed_without_store(self):
        self.client.status.return_value = SimpleNamespace(ok=False, error_code="deadline_exceeded")
        code, payload, _ = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(payload, {"responsive": False,
                                   "error_code": "share_broker_unavailable"})

    def test_cross_origin_cannot_probe_control(self):
        code, _, _ = self.request(origin="https://untrusted.invalid")
        self.assertEqual(code, 403)
        self.client.status.assert_not_called()

    def test_unwired_loader_reports_unwired_at_200_not_500(self):
        """The unwired state is a DECLARED refusal, never an uncaught 500."""
        prod.configure_broker_loader(None)
        code, payload, _ = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(payload, {"responsive": False,
                                   "error_code": "share_broker_unwired"})
        self.client.status.assert_not_called()

    def test_control_status_rejects_writes_without_constructing_store(self):
        code, _, _ = self.request(method="POST")
        self.assertEqual(code, 405)
        self.client.status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
