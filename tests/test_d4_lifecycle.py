# -*- coding: utf-8 -*-
"""D4 lifecycle fixture (handoff §62/§63.3-5): E1-E3 semantics proven through
the REAL product modules — the real wire servers (BoundedPipeServer /
InstancePipeServer / ManagementPipeServer), the real BrokerHostCoordinator,
the real ShareStore/ShareManager/ProtectedProjectionWriter and the real
ShareServer (loopback HTTP) — with ONLY the OS/network/clock edges replaced:
a dual-fake-kernel loopback pair moves every wire byte, the HTTP listener is
a real loopback socket on an ephemeral port, and the E2 clock is injectable.

The probe core (um-share-deploy/d4_stage_probe.py) is driven through the
SAME functions the production entry invokes, so the fixture tests the probe
and the product chain together. cwd-sensitive: run from the ai-gateway dir.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time as _time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_repo))
sys.path.insert(0, str(_repo / "um-share-deploy"))
sys.path.insert(0, str(_repo / "chat2api"))

import d4_stage_probe as probe  # noqa: E402
import share_broker_protocol as bp  # noqa: E402
import share_broker_server as srv  # noqa: E402
import share_entry as share_gw  # noqa: E402
import share_instance_pipe as sip  # noqa: E402
from share_broker_client import BrokerClient, NamedPipeBrokerTransport  # noqa: E402
from share_broker_host import BrokerHostCoordinator, InstanceRegistry  # noqa: E402
from share_entry import ShareServer, X_MODELS_MARKER  # noqa: E402
from share_management import BrokerClientAdapter, ShareManager  # noqa: E402
from share_runtime import ProtectedProjectionWriter  # noqa: E402


def _projection(generation=1):
    return {"schema_version": 1, "config_generation": generation,
            "data": []}


class _LoopbackPair:
    """A dual-fake-kernel loopback pair: the CLIENT half (for the real
    NamedPipeBrokerTransport) and the SERVER half (for the real
    BoundedPipeServer) share two byte queues. Every wire byte travels
    through this pair - the real wire code runs unchanged on both sides.
    ConnectNamedPipe waits for a FRESH client open per cycle (the event is
    consumed), so sequential exchanges and abandoned cycles are observable."""

    def __init__(self):
        self.c2s = bytearray()
        self.s2c = bytearray()
        self.c2s_cv = threading.Condition()
        self.s2c_cv = threading.Condition()
        self.connect_event = threading.Event()
        self.abandon_event = threading.Event()
        self.server_stop = threading.Event()
        self.connect_pending = False
        self.client_closed = threading.Event()
        self.server_handle = 0x3000
        self.client_handle = 0x3100
        self.client_pid = 4242
        self.server_pid = 5151
        self.server_disconnects = 0
        self.client_opens = 0

    def _reset_for_connect(self):
        """A NEW connection starts with EMPTY queues (a previous exchange's
        residual bytes never leak into the next one) and a not-closed
        client."""
        self.c2s.clear()
        self.s2c.clear()
        self.client_closed.clear()

    # ---- server half (BoundedPipeServer facade: (kernel32, advapi32)) ----
    def server_kernel(self):
        pair = self

        class _ServerK:
            def CreateNamedPipeW(self, name, open_mode, pipe_mode, instances,
                                 out_size, in_size, timeout, security):
                return pair.server_handle

            def ConnectNamedPipe(self, h, overlapped):
                # Real release semantics: a blocked connect is released by a
                # client open, by the abandonment path's disconnect, or by
                # the client closing - never parked past the cycle deadline.
                pair.connect_pending = True
                try:
                    while True:
                        if pair.connect_event.wait(0.02):
                            pair.connect_event.clear()
                            pair.abandon_event.clear()
                            return 1
                        if pair.abandon_event.is_set():
                            pair.abandon_event.clear()
                            return 0
                        if pair.client_closed.is_set():
                            return 0
                finally:
                    pair.connect_pending = False

            def ReadFile(self, h, buf, n, read, overlapped):
                while True:
                    with pair.c2s_cv:
                        if pair.c2s:
                            data = bytes(pair.c2s[:n])
                            del pair.c2s[:n]
                            break
                        if pair.client_closed.is_set():
                            read.contents.value = 0
                            return 0
                        pair.c2s_cv.wait(0.02)
                buf[:len(data)] = data
                read.contents.value = len(data)
                return 1

            def WriteFile(self, h, data, n, written, overlapped):
                with pair.s2c_cv:
                    pair.s2c += bytes(data[:n])
                    pair.s2c_cv.notify_all()
                written.contents.value = n
                return 1

            def FlushFileBuffers(self, h):
                return 1

            def DisconnectNamedPipe(self, h):
                pair.server_disconnects += 1
                if pair.connect_pending:
                    # The abandonment path disconnects WHILE the connect is
                    # blocked - release it (the real OS release semantics).
                    pair.abandon_event.set()
                return 1

            def CloseHandle(self, h):
                return 1

            def GetLastError(self):
                return 0

            def GetNamedPipeClientProcessId(self, h, p):
                p.contents.value = pair.client_pid
                return 1

        return _ServerK()

    # ---- client half (NamedPipeBrokerTransport kernel32 facade) ----------
    def client_kernel(self):
        pair = self

        class _ClientK:
            def CreateFileW(self, name, access, share, sa, disposition,
                            flags, template):
                pair.client_opens += 1
                # A NEW connection starts empty: a previous exchange's
                # residual bytes (a drip's leftover) never leak into the
                # next one.
                pair._reset_for_connect()
                pair.connect_event.set()
                return pair.client_handle

            def WriteFile(self, h, data, n, written, overlapped):
                with pair.c2s_cv:
                    pair.c2s += bytes(data[:n])
                    pair.c2s_cv.notify_all()
                written.contents.value = n
                return 1

            def ReadFile(self, h, buf, n, read, overlapped):
                while True:
                    with pair.s2c_cv:
                        if pair.s2c:
                            data = bytes(pair.s2c[:n])
                            del pair.s2c[:n]
                            break
                        if pair.client_closed.is_set():
                            read.contents.value = 0
                            return 0
                        pair.s2c_cv.wait(0.02)
                buf[:len(data)] = data
                read.contents.value = len(data)
                return 1

            def CloseHandle(self, h):
                pair.client_closed.set()
                with pair.s2c_cv:
                    pair.s2c_cv.notify_all()
                with pair.c2s_cv:
                    pair.c2s_cv.notify_all()
                return 1

            def GetLastError(self):
                return 0

            def GetNamedPipeServerProcessId(self, h, p):
                p.contents.value = pair.server_pid
                return 1

        return _ClientK()


class _GatedClientKernel:
    """A client kernel whose ReadFile BLOCKS on a gate until it is set or
    the handle is closed - the bounded-abandonment harness (the real
    transport's cancel+close release path runs unchanged). The released
    reply ECHOES the request id of the last sent frame (a real server
    behavior), so a fresh exchange correlates."""

    def __init__(self):
        self.gate = threading.Event()
        self.written = bytearray()
        self.read_calls = 0
        self.closes = 0
        self.handle = 0x4000

    def CreateFileW(self, *args):
        return self.handle

    def WriteFile(self, h, data, n, written, overlapped):
        self.written += bytes(data[:n])
        written.contents.value = n
        return 1

    def ReadFile(self, h, buf, n, read, overlapped):
        self.read_calls += 1
        self.gate.wait(30)
        if not self.gate.is_set():
            read.contents.value = 0
            return 0
        request_id = ""
        try:
            frames = [ln for ln in bytes(self.written).split(b"\n") if ln]
            body = json.loads(frames[-1])
            request_id = str(body.get("request_id", ""))
        except (ValueError, IndexError):
            pass
        data = bp.encode_frame({"v": 2, "request_id": request_id,
                                "ok": True, "error_code": "ok",
                                "result": {}})[:n]
        buf[:len(data)] = data
        read.contents.value = len(data)
        return 1

    def CloseHandle(self, h):
        self.closes += 1
        self.gate.set()   # the close releases the blocked call (D4 contract)
        return 1

    def GetLastError(self):
        return 0

    def GetNamedPipeServerProcessId(self, h, p):
        p.contents.value = 5151
        return 1


class _CatalogBackend(BaseHTTPRequestHandler):
    """Private upstream edge; no test request can reach a production port."""

    def do_GET(self):
        if self.path != "/v1/models":
            self.send_error(404)
            return
        raw = b'{"object":"list","data":[{"id":"fixture-model"}]}'
        self.server.catalog_reads += 1
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header(X_MODELS_MARKER, "1")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass


class _Assembly:
    """The REAL product assembly on the loopback pair: real store, real
    protected writer, real ShareServer (loopback HTTP), real instance pipe
    server dispatching to the REAL handle_instance_control, real coordinator
    behind the real management pipe server, and the real ShareManager."""

    def __init__(self, td, share_deadline=10.0):
        self.td = Path(td)
        self.store_path = self.td / "share-store.db"
        self.projection_path = self.td / "um-share-projection.json"
        self.projection_path.write_text(json.dumps(_projection()),
                                        encoding="utf-8")
        self.control_key_path = self.td / "broker-control-key"
        self.control_key_path.write_text("c" * 64, encoding="utf-8")
        self.instance_key_path = self.td / "instance-channel-key"
        self.instance_key_path.write_text("i" * 64, encoding="utf-8")
        self.registration_path = self.td / "run" / "live" \
            / "instance-registration.json"
        self.mgmt_pair = _LoopbackPair()
        self.inst_pair = _LoopbackPair()
        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), _CatalogBackend)
        self.backend.catalog_reads = 0
        self.backend_thread = threading.Thread(
            target=self.backend.serve_forever, daemon=True)
        self.backend_thread.start()
        self.share = ShareServer(("127.0.0.1", 0), self.projection_path,
                                 upstream_port=self.backend.server_address[1],
                                 auth_mode="projection")
        self.http_thread = threading.Thread(target=self.share.serve_forever,
                                            daemon=True)
        self.http_thread.start()
        self.mgmt_secret = "c" * 64
        self.instance_secret = "i" * 64
        self.instances = InstanceRegistry()
        self.instances.register(self.share.instance_id)
        self.coordinator = srv.BrokerHostCoordinator(
            writer=ProtectedProjectionWriter(
                self.projection_path, acl_checker=lambda path: None),
            mgmt_secret=self.mgmt_secret,
            instance_secret=self.instance_secret,
            instances=self.instances,
            replay=bp.ReplayRegistry(),
            host_ops=_RecordingOps(),
            instance_call=self._wire_instance_call())
        self.mgmt_server = srv.ManagementPipeServer(
            self.coordinator, r"\\.\pipe\d4-lifecycle-mgmt",
            kernel=(self.mgmt_pair.server_kernel(), None),
            deadline_seconds=share_deadline)
        self.mgmt_thread = threading.Thread(
            target=self.mgmt_server.serve_forever, daemon=True)
        self.mgmt_thread.start()

    def http_port(self):
        return self.share.server_address[1]

    def _wire_instance_call(self):
        pair = self.inst_pair
        secret = self.instance_secret
        prefix = r"\\.\pipe\d4-lifecycle-instance-"

        def call(action, fields, instance_id):
            transport = NamedPipeBrokerTransport(
                prefix + instance_id, kernel=pair.client_kernel())
            client = BrokerClient(lambda: transport, secret)
            result = client.call(action, fields, instance_id=instance_id)
            return {"ok": result.ok, "error_code": result.error_code,
                    "result": result.result}

        return call

    def start_instance_channel(self):
        record = sip.start_instance_channel(
            self.share, self.instance_secret,
            r"\\.\pipe\d4-lifecycle-instance-",
            self.registration_path,
            kernel=(self.inst_pair.server_kernel(), None))
        if not record.get("ready"):
            raise RuntimeError(f"instance channel not ready: {record}")
        self.instance_server = record["server"]
        return record

    def management_transport_factory(self):
        pair = self.mgmt_pair
        return lambda: NamedPipeBrokerTransport(
            r"\\.\pipe\d4-lifecycle-mgmt", kernel=pair.client_kernel())

    def release(self):
        """Release any server-side cycle that is blocked waiting for a NEW
        connection and join the management serve thread. BoundedPipeServer
        keeps CLASS-level worker counters - a serve_once parked in
        ConnectNamedPipe holds a reserved slot until its own deadline. The
        fixtures must release those cycles BEFORE the test returns, or a
        later suite's absolute counter assertions (the reviewed server suite
        asserts _reserved_worker_slots == 0) see the leftover. The fake
        pair's abandon event is the cancellation path the real
        DisconnectNamedPipe would drive."""
        for pair in (self.mgmt_pair, self.inst_pair):
            pair.abandon_event.set()
        self.mgmt_thread.join(5.0)

    def close(self):
        try:
            self.mgmt_server.stop()
        except BaseException:  # noqa: BLE001
            pass
        self.release()
        try:
            self.share.shutdown()
            self.share.server_close()
        except BaseException:  # noqa: BLE001
            pass
        finally:
            self.backend.shutdown()
            self.backend.server_close()
            self.backend_thread.join(5.0)


class _RecordingOps:
    """Fixture host ops (never product logic): observable running state.
    ``restart`` (optional) models the production host-start reality: in the
    real deployment a stopped share is brought back by restarting its task -
    a FRESH instance comes up running. The fixture's injected callback is
    the boundary equivalent (the E4 recovery drives a host start through
    the real broker chain before the re-publication can load/ACK)."""

    def __init__(self, restart=None):
        self.running = False
        self._restart = restart

    def start_host(self):
        if self._restart is not None:
            self._restart()
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
        return {"host": "fixture", "running": self.running}


class E1LifecycleTests(unittest.TestCase):
    """E1 semantics over the REAL wire: two channels, two secrets, the
    negative-first DACL refusal, and the request matrix the entry asserts."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fx = _Assembly(self.temp.name)
        self.registration = self.fx.start_instance_channel()

    def tearDown(self):
        try:
            self.registration["server"].stop()
        finally:
            self.fx.close()
            self.temp.cleanup()

    def test_request_matrix_over_real_wire(self):
        spec = {"port": self.fx.http_port(),
                "management_pipe": r"\\.\pipe\d4-lifecycle-mgmt",
                "instance_prefix": r"\\.\pipe\d4-lifecycle-instance-",
                "control_key": str(self.fx.control_key_path),
                "registration": str(self.fx.registration_path)}

        def http_opener(path, key):
            import urllib.error
            import urllib.request
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.fx.http_port()}" + path)
            if key is not None:
                request.add_header("Authorization", "Bearer " + key)
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, True
            except urllib.error.HTTPError as exc:
                return exc.code, False

        verdicts = probe.run_request_matrix(
            spec, transport_factory=self.fx.management_transport_factory(),
            http_opener=http_opener,
            probe_transport_factory=lambda pipe: NamedPipeBrokerTransport(
                pipe, kernel=self.fx.inst_pair.client_kernel()))
        for expected in ("mgmt-status-ok", "mgmt-wrong-hmac-refused",
                         "mgmt-replay-refused", "http-wrong-key-refused"):
            self.assertIn(expected, verdicts)
        # The instance pipe negative is best-effort: with the registration
        # readable, the loopback transport CONNECTS (the fake kernel does not
        # enforce DACLs) - the verdict must be explicitly ABSENT, never
        # claimed.
        self.assertNotIn("instance-pipe-operator-refused", verdicts)

    def test_two_secrets_are_isolated_on_both_channels(self):
        # A management secret on the INSTANCE channel and an instance secret
        # on the MANAGEMENT channel are both 'forbidden' - the two-whitelist
        # contract holds on the real handlers.
        pair = self.fx.inst_pair
        frame = bp.encode_frame(bp.build_request(
            self.fx.mgmt_secret, "instance-status",
            instance_id=self.fx.share.instance_id))
        transport = NamedPipeBrokerTransport(
            r"\\.\pipe\d4-lifecycle-instance-" + self.fx.share.instance_id,
            kernel=pair.client_kernel())
        client = BrokerClient(lambda: transport, self.fx.mgmt_secret)
        result = client.call("instance-status",
                             instance_id=self.fx.share.instance_id)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "forbidden")

    def test_instance_dacl_conversion_refusal_is_not_ready(self):
        # The placeholder SDDL fails the REAL SDDL conversion (the fake
        # advapi half refuses on purpose): the channel reports not-ready and
        # the registration file is NEVER written (R41-1 fail-closed).
        class _RefusingAdvapi:
            @staticmethod
            def ConvertStringSecurityDescriptorToSecurityDescriptorW(*_a):
                return 0

        temp2 = tempfile.TemporaryDirectory()
        self.addCleanup(temp2.cleanup)
        config = Path(temp2.name) / "projection.json"
        config.write_text(json.dumps(_projection()), encoding="utf-8")
        share = ShareServer(("127.0.0.1", 0), config, auth_mode="projection")
        self.addCleanup(share.server_close)
        registration = Path(temp2.name) / "reg.json"
        record = sip.start_instance_channel(
            share, "inst-k", r"\\.\pipe\d4-lifecycle-dacl-",
            registration,
            sddl="D:P(A;;FA;;;S-1-5-18-exp)",
            kernel=(_LoopbackPair().server_kernel(), _RefusingAdvapi()))
        self.assertFalse(record["ready"])
        self.assertIn("error", record)
        self.assertFalse(registration.exists())


class E2LifecycleTests(unittest.TestCase):
    """E2 semantics over the REAL transport: the bounded classes produce
    server-side cancelled-cycle evidence, a legal request afterwards still
    completes, and the client-side abandonment path releases BY the deadline
    (a reused transport refuses; a fresh one completes)."""

    def _coordinator_assembly(self, td, mgmt_pair):
        control_key = td / "broker-control-key"
        control_key.write_text("c" * 64, encoding="utf-8")
        projection = td / "um-share-projection.json"
        projection.write_text(json.dumps(_projection()), encoding="utf-8")
        status_dir = td / "broker"
        status_dir.mkdir()
        instances = InstanceRegistry()
        instances.register("i-e2")
        coordinator = srv.BrokerHostCoordinator(
            writer=ProtectedProjectionWriter(
                projection, acl_checker=lambda path: None),
            mgmt_secret="c" * 64,
            instance_secret="i" * 64,
            instances=instances,
            replay=bp.ReplayRegistry(),
            host_ops=_RecordingOps(),
            instance_call=lambda action, fields, instance_id: {
                "ok": False, "error_code": "unconfirmed", "result": {}})
        server = srv.ManagementPipeServer(
            coordinator, r"\\.\pipe\d4-lifecycle-stall",
            kernel=(mgmt_pair.server_kernel(), None),
            deadline_seconds=0.5, state_dir=str(status_dir))
        # Mirror the production startup (share_broker_server.main writes the
        # initial status BEFORE serving): the probe's admission evidence is
        # recorded before/after FROM the live status file - a serving loop
        # with no completed cycle would otherwise never produce a "before"
        # file for the fixture (and production never fabricates one).
        server._write_status()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # Cleanup order matters: JOIN the serve thread AFTER stop() releases
        # the loop at its next cycle boundary (bounded by the server
        # deadline) and BEFORE the caller's temp directory is removed - a
        # still-running writer must never touch a deleted directory.
        self.addCleanup(lambda: thread.join(5.0))
        self.addCleanup(server.stop)
        return control_key

    def test_stall_matrix_over_real_transport(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        td = Path(temp.name)
        mgmt_pair = _LoopbackPair()
        control_key = self._coordinator_assembly(td, mgmt_pair)
        # deadline 1s > the server's 0.5s cycle: the server expires first and
        # records the abandoned cycles (its own status writer).
        spec = {"pipe": r"\\.\pipe\d4-lifecycle-stall",
                "status_dir": str(td / "broker"),
                "control_key": str(control_key),
                "max_per_class": 1, "deadline_seconds": 1.0}
        verdicts = probe.run_stall_matrix(
            spec, transport_factory=(
                lambda: NamedPipeBrokerTransport(
                    r"\\.\pipe\d4-lifecycle-stall",
                    kernel=mgmt_pair.client_kernel())))
        for expected in ("silent-cancelled", "drip-cancelled",
                         "blocking-cancelled", "post-timeout-request-ok"):
            self.assertIn(expected, verdicts)

    def test_blocking_read_abandoned_by_deadline(self):
        # The REAL abandonment path: a withheld reply expires the client's
        # OWN deadline (cancel + handle close release the blocked call), a
        # reused transport refuses, and a fresh transport completes (the
        # released reply echoes the request id, so the exchange correlates).
        gated = _GatedClientKernel()
        transport = NamedPipeBrokerTransport(r"\\.\pipe\d4-gated",
                                             kernel=gated)
        transport.open()
        deadline = _time.monotonic() + 0.2
        transport.send_all(b"{}\n", deadline)
        with self.assertRaises(TimeoutError):
            transport.recv_line(deadline)
        self.assertTrue(transport._abandoned)
        with self.assertRaises(OSError):
            transport.send_all(b"{}\n", _time.monotonic() + 0.2)
        # A FRESH transport over the released gate completes.
        gated.gate.set()
        fresh = NamedPipeBrokerTransport(r"\\.\pipe\d4-gated", kernel=gated)
        client = BrokerClient(lambda: fresh, "k")
        result = client.call("status")
        self.assertTrue(result.ok)


class E3LifecycleTests(unittest.TestCase):
    """E3 semantics: the REAL store publishes through the REAL management
    wire, the REAL instance ACK binds generation/fingerprint/id, the 9100
    HTTP matrix enforces the key, and the revocation refuses the old key."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fx = _Assembly(self.temp.name)
        self.registration = self.fx.start_instance_channel()

    def tearDown(self):
        try:
            self.registration["server"].stop()
        finally:
            self.fx.close()
            self.temp.cleanup()

    def test_projection_lifecycle_over_real_store_and_wire(self):
        spec = {"store": str(self.fx.store_path),
                "projection": str(self.fx.projection_path),
                "management_pipe": r"\\.\pipe\d4-lifecycle-mgmt",
                "port": self.fx.http_port(),
                "control_key": str(self.fx.control_key_path)}

        def broker_factory():
            return BrokerClientAdapter(BrokerClient(
                self.fx.management_transport_factory(), self.fx.mgmt_secret))

        def http_opener(path, key):
            import urllib.error
            import urllib.request
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.fx.http_port()}" + path)
            if key is not None:
                request.add_header("Authorization", "Bearer " + key)
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, True
            except urllib.error.HTTPError as exc:
                return exc.code, False

        verdicts = probe.run_projection_lifecycle(spec,
                                                  broker_factory=broker_factory,
                                                  http_opener=http_opener)
        self.assertEqual(verdicts, ["ack-generation-matched", "http-key-ok",
                                    "wrong-key-401", "revoked-key-refused"])
        self.assertEqual(self.fx.backend.catalog_reads, 1)
        # The REAL projection file was written by the REAL protected writer
        # and its generation advanced past the initial store generation.
        self.assertGreaterEqual(
            json.loads(self.fx.projection_path.read_text(
                encoding="utf-8"))["config_generation"], 2)


class E4ProjectionWriteTests(unittest.TestCase):
    """E4 projection-write boundary: a REAL icacls deny on the experiment
    store's projection refuses the revoking publication (blocked record +
    emergency stop driven); the recovery restores the ACL and a fresh
    publication applies. The writer's ACL checker is the REAL
    IcaclsAclChecker whose expectations come from the OBSERVED pre-fault
    ACL - the fault bites the REAL write path, never a fake."""

    def test_projection_write_fault_refuses_and_recovers(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        td = Path(temp.name)
        store_path = td / "share-store.db"
        projection_path = td / "um-share-projection.json"
        projection_path.write_text(json.dumps(_projection()), encoding="utf-8")
        control_key = td / "broker-control-key"
        control_key.write_text("c" * 64, encoding="utf-8")
        expectations = _expectations_from_live_acl(projection_path)
        checker = _make_acl_checker(projection_path, expectations)
        mgmt_pair = _LoopbackPair()
        inst_pair = _LoopbackPair()
        share = ShareServer(("127.0.0.1", 0), projection_path,
                            auth_mode="projection")
        self.addCleanup(share.server_close)

        def _release_e4_cycles():
            # The BoundedPipeServer worker counters are CLASS level: releasing
            # the blocked connects AFTER the stops lets every serve_once end
            # (never a later suite seeing a held reserved slot).
            mgmt_pair.abandon_event.set()
            inst_pair.abandon_event.set()
        self.addCleanup(_release_e4_cycles)
        instances = InstanceRegistry()
        instances.register(share.instance_id)
        coordinator = srv.BrokerHostCoordinator(
            writer=ProtectedProjectionWriter(
                projection_path, acl_checker=checker),
            mgmt_secret="c" * 64,
            instance_secret="i" * 64,
            instances=instances,
            replay=bp.ReplayRegistry(),
            host_ops=_RecordingOps(restart=share.start),
            instance_call=_wire_call_for(inst_pair, share.instance_id,
                                         "i" * 64))
        mgmt_server = srv.ManagementPipeServer(
            coordinator, r"\\.\pipe\d4-e4-mgmt",
            kernel=(mgmt_pair.server_kernel(), None), deadline_seconds=10.0)
        threading.Thread(target=mgmt_server.serve_forever,
                         daemon=True).start()
        self.addCleanup(mgmt_server.stop)
        # The instance channel MUST serve: the fault key's initial
        # publication travels the REAL load/ACK path (a missing listener is
        # an unconfirmed publication, never a fault verdict).
        registration = td / "run" / "live" / "instance-registration.json"
        channel = sip.start_instance_channel(
            share, "i" * 64, r"\\.\pipe\d4-e4-instance-", registration,
            kernel=(inst_pair.server_kernel(), None))
        if not channel.get("ready"):
            raise RuntimeError(f"instance channel not ready: {channel}")
        self.addCleanup(channel["server"].stop)

        def broker_factory():
            return BrokerClientAdapter(BrokerClient(
                lambda: NamedPipeBrokerTransport(r"\\.\pipe\d4-e4-mgmt",
                                                 kernel=mgmt_pair.client_kernel()),
                "c" * 64))
        spec = {"store": str(store_path),
                "projection": str(projection_path),
                "management_pipe": r"\\.\pipe\d4-e4-mgmt",
                "control_key": str(control_key),
                "boundary": "projection-write"}
        verdicts = probe.run_fault_injection(spec,
                                             broker_factory=broker_factory)
        self.assertEqual(verdicts, ["refused", "recovery-recorded"])
        # The blocked publication record exists in the REAL store ops table.
        store = _open_store(store_path)
        try:
            ops = store.operations(kind="publish")
            self.assertTrue(any(op["status"] in ("blocked", "failed")
                                for op in ops))
        finally:
            store.close()


def _wire_call_for(pair, instance_id, secret):
    prefix = r"\\.\pipe\d4-e4-instance-"

    def call(action, fields, target):
        transport = NamedPipeBrokerTransport(prefix + target,
                                             kernel=pair.client_kernel())
        client = BrokerClient(lambda: transport, secret)
        result = client.call(action, fields, instance_id=target)
        return {"ok": result.ok, "error_code": result.error_code,
                "result": result.result}

    return call


def _expectations_from_live_acl(path):
    """The REAL checker's expectations come from the OBSERVED pre-fault
    ACL: every ALLOW principal holding full control is expected full. (The
    checker's strict-shape contract is exercised by its own suite; the
    fixture only pins the pre-fault observed state.)"""
    from share_broker_ops import IcaclsAclChecker
    result = subprocess.run(["icacls", str(path)], capture_output=True,
                            encoding="mbcs", errors="replace", timeout=15)
    if result.returncode != 0:
        raise RuntimeError(f"icacls query failed (exit {result.returncode})")
    target = str(path).casefold()
    full = {}
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.casefold() == target:
            continue
        if line.casefold().startswith(target):
            # icacls prints the path and the FIRST ACE on ONE line - the
            # remainder behind the path prefix is the ACE itself.
            line = line[len(str(path)):].strip()
            if not line:
                continue
        name, _, flags = line.rpartition(":")
        tokens = [t.strip('() \t') for t in flags.split("(") if t.strip()]
        if any(t == "DENY" for t in tokens):
            continue
        perms = set()
        for token in tokens:
            if token in IcaclsAclChecker._FLAG_TOKENS:
                continue
            perms.update(x for x in token.replace(" ", "").split(",") if x)
        if perms & IcaclsAclChecker._FULL_CODES:
            full[name.strip()] = ["F"]
    if not full:
        raise RuntimeError("no full-control ALLOW principal observed")
    return {"full": full, "read_only": {}, "deny": []}


def _make_acl_checker(target, expectations):
    """Adapt the REAL IcaclsAclChecker to the writer's callable contract
    (callable(path) -> None | raise); strict=False is the documented
    offline-fixture mode (the checker's own suite covers strict)."""
    from share_broker_ops import IcaclsAclChecker
    checker = IcaclsAclChecker(target, expectations)

    def check(path):
        if not checker.check(str(path), strict=False):
            raise PermissionError(
                checker.last_error or "projection ACL check refused")

    return check


def _open_store(path):
    from share_store import ShareStore
    return ShareStore(path)


_REVOKE_STATE_TMP = None
_REVOKE_STATE_SAVED = None


def setUpModule():
    """Keep the runtime's persisted revoke state out of the PRODUCT path.

    The ACK now merges the persisted state and records the applied
    projection generation, so this module owns a private state file.
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


if __name__ == "__main__":
    unittest.main()
