"""REAL Linux AF_UNIX control-channel tests (share broker).

Linux-only by design: real sockets, real filesystem modes and real
SO_PEERCRED. On every other platform the module is still collected and each
test is skipped, so Windows collection stays green.

Covered contract (share_broker_client.UnixSocketBrokerTransport +
share_broker_server.UnixSocketBrokerServer + the POSIX instance channel):

* a normal signed request/response over a real socket, and the SAME replay
  registry consuming a replayed frame;
* the socket file is 0600 and its parent directory 0700;
* an overlimit frame is answered explicitly and never dispatched;
* a half-read / half-write peer times out within the bounded cycle and the
  connection worker falls back so the next connection still serves;
* peer identity is KERNEL evidence (SO_PEERCRED) matching the real process,
  including a real child process;
* permission refusals (other uid, non-executable directory, wider modes) are
  explicit access refusals;
* bad paths (relative, too long, parent is a file, live endpoint, stale file)
  fail early and never fabricate readiness.
"""
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chat2api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import share.broker_protocol as bp  # noqa: E402
import share.broker_server as srv  # noqa: E402
import share.instance_pipe as sip  # noqa: E402
from share.broker_client import (BrokerClient, BrokerTransport,  # noqa: E402
                                UnixSocketBrokerTransport,
                                validate_unix_socket_path)
from _share_instance_double import FakeShareServer as ShareServer  # noqa: E402

IS_LINUX = sys.platform.startswith("linux")
LINUX_ONLY = unittest.skipUnless(
    IS_LINUX, "AF_UNIX/SO_PEERCRED control-channel tests require Linux")


def _projection(generation=1):
    return {"schema_version": 1, "config_generation": generation,
            "data": [{"key_id": "u1", "name": "f1",
                      "secret_hash": "a" * 64, "secret_version": 1,
                      "enabled": True}]}


class _UnixServerFixture:
    """One real Unix-socket instance server bound to a private temp dir."""

    def __init__(self, testcase, deadline_seconds=1.0, secret="inst-k"):
        self.temp = tempfile.TemporaryDirectory()
        testcase.addCleanup(self.temp.cleanup)
        self.dir = self.temp.name
        self.config = Path(self.dir) / "um-share-projection.json"
        self.config.write_text(json.dumps(_projection()), encoding="utf-8")
        self.share = ShareServer(("127.0.0.1", 0), self.config,
                                auth_mode="projection")
        testcase.addCleanup(self.share.server_close)
        self.secret = secret
        self.path = os.path.join(self.dir, "run", "live",
                                 "instance-" + self.share.instance_id)
        self.server = sip.UnixSocketInstanceServer(
            self.share, secret, self.path,
            deadline_seconds=deadline_seconds)
        testcase.addCleanup(self.stop)
        self.thread = None

    def start(self, serve=True):
        ok, error = sip.probe_instance_listener(self.server)
        if not ok:
            raise AssertionError(f"unix instance listener probe failed: "
                                 f"{error}")
        if serve:
            self.thread = threading.Thread(target=self.server.serve_forever,
                                           daemon=True)
            self.thread.start()
        return self

    def stop(self):
        self.server.stop()
        if self.thread is not None:
            self.thread.join(5)
        self.server.close()


def _connect(path, timeout=5.0):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(path)
    return sock


def _read_line(sock):
    return json.loads(sock.makefile("rb").readline())


class _BigReplyShare:
    """A stub instance endpoint whose reply cannot fit in a socket buffer."""

    instance_id = "big-reply"

    def handle_instance_control(self, body, secret):
        return {"v": 2, "request_id": body.get("request_id"), "ok": True,
                "error_code": "ok", "result": {"blob": "y" * 4_000_000}}


@LINUX_ONLY
class UnixSocketContractTests(unittest.TestCase):
    """The classes are real adapters, not stand-ins."""

    def test_adapter_hierarchy(self):
        self.assertTrue(issubclass(UnixSocketBrokerTransport, BrokerTransport))
        self.assertTrue(issubclass(srv.UnixSocketBrokerServer,
                                   srv.BoundedSocketServer))
        self.assertTrue(issubclass(sip.UnixSocketInstanceServer,
                                   srv.UnixSocketBrokerServer))


@LINUX_ONLY
class UnixSocketRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.fx = _UnixServerFixture(self).start()

    def test_request_response_roundtrip(self):
        client = BrokerClient(
            lambda: UnixSocketBrokerTransport(self.fx.path), self.fx.secret)
        result = client.call("instance-status",
                             instance_id=self.fx.share.instance_id)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.result["instance_id"],
                         self.fx.share.instance_id)
        self.assertEqual(result.result["state"], "running")

    def test_replayed_frame_is_consumed_by_real_handler(self):
        frame = bp.encode_frame(bp.build_request(
            self.fx.secret, "instance-status",
            instance_id=self.fx.share.instance_id))
        replies = []
        for _ in range(2):
            sock = _connect(self.fx.path)
            try:
                sock.sendall(frame)
                sock.shutdown(socket.SHUT_WR)
                replies.append(_read_line(sock))
            finally:
                sock.close()
        self.assertTrue(replies[0]["ok"], replies[0])
        self.assertFalse(replies[1]["ok"])
        self.assertEqual(replies[1]["error_code"], "replay")

    def test_socket_file_and_directory_permissions(self):
        info = os.stat(self.fx.path)
        self.assertTrue(stat.S_ISSOCK(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        parent = os.stat(os.path.dirname(self.fx.path))
        self.assertEqual(stat.S_IMODE(parent.st_mode), 0o700)

    def test_peer_identity_is_kernel_evidence(self):
        closed = UnixSocketBrokerTransport(self.fx.path)
        self.assertIsNone(closed.peer_server_pid())
        self.assertIsNone(closed.peer_credentials())
        transport = UnixSocketBrokerTransport(self.fx.path).open()
        try:
            credentials = transport.peer_credentials()
            self.assertEqual(credentials, {"pid": os.getpid(),
                                         "uid": os.getuid(),
                                         "gid": os.getgid()})
            self.assertEqual(transport.peer_server_pid(), os.getpid())
        finally:
            transport.close()

    def test_server_records_the_real_peer_credentials(self):
        client = BrokerClient(
            lambda: UnixSocketBrokerTransport(self.fx.path), self.fx.secret)
        result = client.call("instance-status",
                             instance_id=self.fx.share.instance_id)
        self.assertTrue(result.ok, result.to_dict())
        credentials = self.fx.server.last_peer_credentials
        self.assertEqual(credentials["pid"], os.getpid())
        self.assertEqual(credentials["uid"], os.getuid())
        self.assertEqual(credentials["gid"], os.getgid())

    def test_peer_identity_matches_a_real_child_process(self):
        frame = bp.encode_frame(bp.build_request(
            self.fx.secret, "instance-status",
            instance_id=self.fx.share.instance_id))
        child_code = (
            "import socket, sys\n"
            "frame = bytes.fromhex(sys.argv[2])\n"
            "sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "sock.settimeout(5)\n"
            "sock.connect(sys.argv[1])\n"
            "sock.sendall(frame)\n"
            "sock.shutdown(socket.SHUT_WR)\n"
            "sys.stdout.write(sock.makefile('rb').readline().decode())\n")
        child = subprocess.Popen(
            [sys.executable, "-c", child_code, self.fx.path, frame.hex()],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = child.communicate(timeout=15)
        self.assertEqual(child.returncode, 0, err)
        reply = json.loads(out)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["instance_id"],
                         self.fx.share.instance_id)
        credentials = self.fx.server.last_peer_credentials
        self.assertEqual(credentials["pid"], child.pid)
        self.assertEqual(credentials["uid"], os.getuid())
        self.assertEqual(credentials["gid"], os.getgid())

    def test_overlimit_frame_is_explicit_and_never_dispatched(self):
        class _NeverDispatched:
            instance_id = "never-dispatched"
            calls = 0

            def handle_instance_control(self, body, secret):
                _NeverDispatched.calls += 1
                return {"v": 2, "request_id": body.get("request_id"),
                        "ok": True, "error_code": "ok", "result": {}}

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = os.path.join(temp.name, "instance-overlimit")
        server = sip.UnixSocketInstanceServer(
            _NeverDispatched(), "k", path, deadline_seconds=3.0)
        self.addCleanup(server.close)
        ok, error = sip.probe_instance_listener(server)
        self.assertTrue(ok, error)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.stop(), thread.join(5)))
        sock = _connect(path)
        try:
            sock.sendall(b"x" * (bp.MAX_MESSAGE + 1) + b"\n")
            sock.shutdown(socket.SHUT_WR)
            reply = _read_line(sock)
        finally:
            sock.close()
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error_code"], "overlimit")
        self.assertEqual(_NeverDispatched.calls, 0)

    def test_poisoned_cycle_keeps_serving(self):
        sock = _connect(self.fx.path)
        try:
            sock.sendall(b"not-json\n")
            sock.shutdown(socket.SHUT_WR)
            reply = _read_line(sock)
        finally:
            sock.close()
        self.assertEqual(reply["error_code"], "bad_request")
        self.assertFalse(self.fx.server.stopped())
        result = BrokerClient(
            lambda: UnixSocketBrokerTransport(self.fx.path),
            self.fx.secret).call("instance-status",
                                  instance_id=self.fx.share.instance_id)
        self.assertTrue(result.ok, result.to_dict())
        self.assertGreaterEqual(self.fx.server._cycles, 2)


@LINUX_ONLY
class UnixSocketDeadlineTests(unittest.TestCase):
    """A half-read/half-write peer cannot park a cycle forever."""

    def _deadline_fixture(self, deadline_seconds=0.4):
        return _UnixServerFixture(
            self, deadline_seconds=deadline_seconds).start(serve=False)

    def _run_serve_once(self, server):
        box = {}

        def run():
            box["record"] = server.serve_once()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        time.sleep(0.15)
        return thread, box

    def test_half_read_times_out(self):
        fx = self._deadline_fixture()
        thread, box = self._run_serve_once(fx.server)
        sock = _connect(fx.path)
        try:
            sock.sendall(b'{"v": 2')
            started = time.monotonic()
            data = sock.recv(16)
            elapsed = time.monotonic() - started
        finally:
            sock.close()
        thread.join(5)
        record = box["record"]
        self.assertEqual(data, b"")
        self.assertFalse(record["ok"])
        self.assertEqual(record["error"], "connection deadline exceeded")
        self.assertTrue(record.get("abandoned"))
        self.assertLess(elapsed, 3.0)

    def test_half_write_times_out(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = os.path.join(temp.name, "instance-big-reply")
        server = sip.UnixSocketInstanceServer(
            _BigReplyShare(), "big-k", path, deadline_seconds=0.4)
        self.addCleanup(server.close)
        ok, error = sip.probe_instance_listener(server)
        self.assertTrue(ok, error)
        thread, box = self._run_serve_once(server)
        sock = _connect(path)
        try:
            sock.sendall(bp.encode_frame(bp.build_request(
                "big-k", "instance-status", instance_id="big-reply")))
            first = sock.recv(16)
            self.assertTrue(first)      # the server started the reply
            started = time.monotonic()
            thread.join(5)
            elapsed = time.monotonic() - started
        finally:
            sock.close()
        record = box["record"]
        self.assertFalse(record["ok"])
        self.assertEqual(record["error"], "connection deadline exceeded")
        self.assertTrue(record.get("abandoned"))
        self.assertLess(elapsed, 3.0)


@LINUX_ONLY
class UnixSocketPathPolicyTests(unittest.TestCase):
    def setUp(self):
        self.share = SimpleNamespace(instance_id="path-policy")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = self.temp.name

    def test_posix_default_prefix_is_an_absolute_socket_path(self):
        prefix = sip.default_instance_pipe_prefix()
        self.assertTrue(os.path.isabs(prefix))
        self.assertEqual(sip.instance_pipe_name(prefix, "i-1"),
                         prefix + "i-1")

    def test_relative_and_overlong_paths_are_refused(self):
        with self.assertRaises(ValueError):
            sip.UnixSocketInstanceServer(self.share, "k", "relative.sock")
        with self.assertRaises(ValueError):
            UnixSocketBrokerTransport("relative.sock")
        too_long = "/tmp/" + "x" * 120
        with self.assertRaises(ValueError):
            sip.UnixSocketInstanceServer(self.share, "k", too_long)
        with self.assertRaises(ValueError):
            UnixSocketBrokerTransport(too_long)
        self.assertEqual(validate_unix_socket_path("/tmp/ok.sock"),
                         "/tmp/ok.sock")

    def test_parent_that_is_a_file_fails_early(self):
        blocker = Path(self.dir) / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        server = sip.UnixSocketInstanceServer(
            self.share, "k", str(blocker / "s.sock"))
        ok, error = sip.probe_instance_listener(server)
        self.assertFalse(ok)
        self.assertIn("not a directory", error)

    def test_existing_regular_file_is_refused(self):
        path = os.path.join(self.dir, "regular")
        Path(path).write_text("not a socket", encoding="utf-8")
        server = sip.UnixSocketInstanceServer(self.share, "k", path)
        ok, error = sip.probe_instance_listener(server)
        self.assertFalse(ok)
        self.assertIn("not a socket", error)

    def test_wider_directory_is_refused_by_server_and_client(self):
        wide = tempfile.mkdtemp()
        self.addCleanup(lambda: os.rmdir(wide))
        os.chmod(wide, 0o755)
        path = os.path.join(wide, "s.sock")
        server = sip.UnixSocketInstanceServer(self.share, "k", path)
        ok, error = sip.probe_instance_listener(server)
        self.assertFalse(ok)
        self.assertIn("0700", error)
        result = BrokerClient(
            lambda: UnixSocketBrokerTransport(path), "k").call(
                "instance-status", instance_id="i-policy")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "access_denied")

    def test_stale_socket_is_reclaimed(self):
        path = os.path.join(self.dir, "stale.sock")
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(path)
        stale.close()
        self.assertTrue(os.path.exists(path))
        server = sip.UnixSocketInstanceServer(
            self.share, "k", path, deadline_seconds=1.0)
        ok, error = sip.probe_instance_listener(server)
        self.assertTrue(ok, error)
        self.assertTrue(stat.S_ISSOCK(os.stat(path).st_mode))
        server.close()
        self.assertFalse(os.path.exists(path))

    def test_live_socket_is_refused(self):
        path = os.path.join(self.dir, "live.sock")
        first = sip.UnixSocketInstanceServer(
            self.share, "k", path, deadline_seconds=1.0)
        ok, error = sip.probe_instance_listener(first)
        self.assertTrue(ok, error)
        self.addCleanup(first.close)
        second = sip.UnixSocketInstanceServer(
            self.share, "k", path, deadline_seconds=1.0)
        ok, error = sip.probe_instance_listener(second)
        self.assertFalse(ok)
        self.assertIn("already served", error)


@LINUX_ONLY
class UnixSocketClientPolicyTests(unittest.TestCase):
    def setUp(self):
        self.fx = _UnixServerFixture(self).start()

    def test_missing_endpoint_is_not_installed(self):
        missing = os.path.join(self.fx.dir, "missing.sock")
        result = BrokerClient(
            lambda: UnixSocketBrokerTransport(missing), "k").call(
                "instance-status", instance_id="i-missing")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "not_installed")

    def test_non_socket_endpoint_is_not_installed(self):
        path = os.path.join(self.fx.dir, "regular")
        Path(path).write_text("not a socket", encoding="utf-8")
        result = BrokerClient(
            lambda: UnixSocketBrokerTransport(path), "k").call(
                "instance-status", instance_id="i-regular")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "not_installed")

    def test_expired_deadline_is_a_timeout(self):
        transport = UnixSocketBrokerTransport(self.fx.path)
        with self.assertRaises(TimeoutError):
            transport.open(deadline=time.monotonic() - 1.0)

    def test_wider_socket_file_is_an_access_refusal(self):
        # Deliberate misconfiguration of a temp socket to prove the client
        # refuses it; restored to 0600 in the finally block.
        os.chmod(self.fx.path, 0o666)
        try:
            with self.assertRaises(PermissionError):
                UnixSocketBrokerTransport(self.fx.path).open()
            result = BrokerClient(
                lambda: UnixSocketBrokerTransport(self.fx.path), "k").call(
                    "instance-status", instance_id="i-mode")
            self.assertEqual(result.error_code, "access_denied")
        finally:
            os.chmod(self.fx.path, 0o600)


@LINUX_ONLY
class UnixInstanceChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = self.temp.name
        self.config = Path(self.dir) / "um-share-projection.json"
        self.config.write_text(json.dumps(_projection()), encoding="utf-8")
        self.share = ShareServer(("127.0.0.1", 0), self.config,
                                auth_mode="projection")
        self.addCleanup(self.share.server_close)
        self.secret = "inst-k"
        self.registration = Path(self.dir) / "run" / "live" \
            / "instance-registration.json"

    def test_start_instance_channel_uses_a_socket_path(self):
        record = sip.start_instance_channel(
            self.share, self.secret, os.path.join(self.dir, "instance-"),
            self.registration)
        self.assertTrue(record["ready"], record)
        server = record["server"]
        try:
            path = record["pipe_name"]
            self.assertTrue(os.path.isabs(path))
            self.assertTrue(stat.S_ISSOCK(os.stat(path).st_mode))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            payload = json.loads(self.registration.read_text(encoding="utf-8"))
            self.assertEqual(payload["instance_id"], self.share.instance_id)
            self.assertEqual(payload["pipe_name"], path)
            self.assertEqual(payload["pid"], os.getpid())
            self.assertFalse(Path(str(self.registration) + ".tmp").exists())
            result = BrokerClient(
                lambda: UnixSocketBrokerTransport(path),
                self.secret).call("instance-status",
                                  instance_id=self.share.instance_id)
            self.assertTrue(result.ok, result.to_dict())
        finally:
            server.stop()
            server.close()

    def test_probe_fails_early_on_a_live_endpoint(self):
        fx = _UnixServerFixture(self, secret=self.secret).start(serve=False)
        server = sip.UnixSocketInstanceServer(
            self.share, self.secret, fx.path, deadline_seconds=1.0)
        ok, error = sip.probe_instance_listener(server)
        self.assertFalse(ok)
        self.assertIn("already served", error)

    def test_bad_path_is_an_explicit_not_ready_state(self):
        record = sip.start_instance_channel(
            self.share, self.secret, "relative-instance-", self.registration)
        self.assertFalse(record["ready"])
        self.assertIn("absolute", record["error"])
        self.assertFalse(self.registration.exists())

    def test_registration_write_and_remove_are_atomic(self):
        payload = sip.write_registration(
            self.registration, "i-atomic", "/tmp/instance-i-atomic")
        self.assertEqual(payload["instance_id"], "i-atomic")
        self.assertEqual(payload["pipe_name"], "/tmp/instance-i-atomic")
        self.assertFalse(Path(str(self.registration) + ".tmp").exists())
        sip.remove_registration(self.registration)
        self.assertFalse(self.registration.exists())
        sip.remove_registration(self.registration)   # idempotent


@LINUX_ONLY
class UnixSocketPermissionTests(unittest.TestCase):
    def test_other_uid_is_denied(self):
        if os.getuid() != 0:
            self.skipTest("requires root to switch uid")
        fx = _UnixServerFixture(self).start(serve=False)
        child_code = (
            "import socket, sys\n"
            "sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "try:\n"
            "    sock.connect(sys.argv[1])\n"
            "except OSError as exc:\n"
            "    print(type(exc).__name__, exc.errno)\n"
            "    sys.exit(0)\n"
            "sys.exit(3)\n")

        def drop():
            os.setgroups([])
            os.setgid(65534)
            os.setuid(65534)

        child = subprocess.run([sys.executable, "-c", child_code, fx.path],
                               capture_output=True, text=True,
                               preexec_fn=drop)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), "PermissionError 13")

    def test_no_execute_directory_is_denied(self):
        fx = _UnixServerFixture(self).start(serve=False)
        os.chmod(fx.dir, 0o600)
        child_code = (
            "import socket, sys\n"
            "sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "try:\n"
            "    sock.connect(sys.argv[1])\n"
            "except OSError as exc:\n"
            "    print(type(exc).__name__, exc.errno)\n"
            "    sys.exit(0)\n"
            "sys.exit(3)\n")

        def drop():
            os.setgroups([])
            os.setgid(65534)
            os.setuid(65534)

        preexec = drop if os.getuid() == 0 else None
        child = subprocess.run([sys.executable, "-c", child_code, fx.path],
                               capture_output=True, text=True,
                               preexec_fn=preexec)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), "PermissionError 13")


if __name__ == "__main__":
    unittest.main()
