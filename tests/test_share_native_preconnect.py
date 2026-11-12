"""Use Windows itself to verify a client connecting before ConnectNamedPipe."""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
import uuid


def native_child():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from share.broker_server import BoundedPipeServer
    from share.broker_client import NamedPipeBrokerTransport

    name = r'\\.\pipe\SelfUseAI-share-preconnect-' + uuid.uuid4().hex
    server = BoundedPipeServer(name)
    client = NamedPipeBrokerTransport(name)
    try:
        handle = server._create_instance()
        client.open(deadline=time.monotonic() + 1)
        kernel, _ = server._kernel()
        returned = kernel.ConnectNamedPipe(handle, None)
        code = kernel.GetLastError()
        assert returned == 0 and code == 535, (returned, code)
        server._bounded_connect(handle, threading.Event(), time.monotonic() + 1)
    finally:
        client.close()
        server._release_instance()


def native_delayed_child(explicit=False, silent=False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from share.broker_server import BoundedPipeServer
    from share.broker_client import NamedPipeBrokerTransport
    created = threading.Event()
    records = []
    class EchoServer(BoundedPipeServer):
        def _create_instance(self, sddl=None):
            handle = super()._create_instance(sddl=sddl)
            created.set()
            return handle
        def _dispatch(self, body, record):
            return b'{"accepted":true}\n'
    name = r'\\.\pipe\SelfUseAI-share-delayed-' + uuid.uuid4().hex
    server = EchoServer(name, deadline_seconds=2.0)
    def serve():
        records.append(server.serve_once(deadline=time.monotonic() + 2.0)
                       if explicit else server.serve_once())
    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    assert created.wait(1), 'native listener was not created'
    client = NamedPipeBrokerTransport(name)
    received = False
    try:
        time.sleep(1.4)
        client.open(deadline=time.monotonic() + 3)
        if silent:
            worker.join(3)
            assert not worker.is_alive(), 'connected silent client exceeded its bounded read window'
            assert records and not records[0]['ok'] and records[0]['abandoned'], records
            assert type(server)._unconfirmed_abandoned == 0
            client.close()
            created.clear()
            next_worker = threading.Thread(target=serve, daemon=True)
            next_worker.start()
            assert created.wait(1), 'next native listener was not created'
            client.open(deadline=time.monotonic() + 1)
            client.send_all(b'{}\n', time.monotonic() + 1)
            assert client.recv_line(time.monotonic() + 1) == b'{"accepted":true}\n'
            next_worker.join(1)
            assert not next_worker.is_alive() and records[-1]['ok'], records
            return
        time.sleep(.9)
        try:
            client.send_all(b'{}\n', time.monotonic() + 2)
            received = client.recv_line(time.monotonic() + 2) == b'{"accepted":true}\n'
        except OSError:
            if not explicit:
                raise
    finally:
        client.close()
        worker.join(4)
        server.stop()
        server._release_instance()
    assert not worker.is_alive(), 'native server did not release its worker'
    assert received is (not explicit), (received, records)
    assert records[0]['ok'] is (not explicit), records


def native_client_timeout_child():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from share.broker_server import BoundedPipeServer
    from share.broker_client import NamedPipeBrokerTransport
    server = BoundedPipeServer(r'\\.\pipe\SelfUseAI-share-client-timeout-' + uuid.uuid4().hex)
    client = NamedPipeBrokerTransport(server.pipe_name)
    try:
        handle = server._create_instance()
        client.open(deadline=time.monotonic() + 1)
        server._bounded_connect(handle, threading.Event(), time.monotonic() + 1)
        start = time.monotonic()
        try:
            client.recv_line(start + .2)
            raise AssertionError('silent peer unexpectedly returned data')
        except TimeoutError:
            pass
        assert time.monotonic() - start < 3, 'client native read did not honor its bounded cleanup'
        assert client.abandoned_workers_unreaped == 0
    finally:
        client.close()
        server._release_instance()


def native_idle_accept_child():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from share.broker_server import BoundedPipeServer
    from share.broker_client import NamedPipeBrokerTransport
    import json
    class EchoServer(BoundedPipeServer):
        def _dispatch(self, body, record):
            return b'{"accepted":true}\n'
    server = EchoServer(r'\\.\pipe\SelfUseAI-share-idle-' + uuid.uuid4().hex,
                        deadline_seconds=.2)
    start = time.monotonic()
    record = server.serve_once()
    assert time.monotonic() - start < 3 and record.get('abandoned'), record
    assert type(server)._unconfirmed_abandoned == 0
    records = []
    worker = threading.Thread(target=lambda: records.append(server.serve_once()), daemon=True)
    worker.start()
    client = NamedPipeBrokerTransport(server.pipe_name)
    try:
        client.open(deadline=time.monotonic() + 1)
        client.send_all(b'{}\n', time.monotonic() + 1)
        assert json.loads(client.recv_line(time.monotonic() + 1))['accepted']
    finally:
        client.close()
        worker.join(3)
        server.stop()
        server._release_instance()
    assert not worker.is_alive() and records[-1]['ok'], records


@unittest.skipUnless(os.name == 'nt', 'Windows native pipe contract')
class NativePreconnectionTests(unittest.TestCase):
    def test_preconnected_client_is_accepted_using_real_windows_error_code(self):
        child = subprocess.run([sys.executable, '-X', 'utf8', __file__, '--native-child'],
                               capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(child.returncode, 0, child.stderr)

    def test_idle_accept_wait_does_not_consume_a_new_connections_read_budget(self):
        child = subprocess.run([sys.executable, '-X', 'utf8', __file__, '--native-delayed'],
                               capture_output=True, text=True, encoding='utf-8', timeout=8)
        self.assertEqual(child.returncode, 0, child.stderr)

    def test_an_explicit_cycle_deadline_is_still_a_hard_bound(self):
        child = subprocess.run([sys.executable, '-X', 'utf8', __file__, '--native-explicit'],
                               capture_output=True, text=True, encoding='utf-8', timeout=8)
        self.assertEqual(child.returncode, 0, child.stderr)

    def test_connected_silent_client_still_releases_the_bounded_worker(self):
        child = subprocess.run([sys.executable, '-X', 'utf8', __file__, '--native-silent'],
                               capture_output=True, text=True, encoding='utf-8', timeout=8)
        self.assertEqual(child.returncode, 0, child.stderr)

    def test_client_timeout_cancels_native_read_before_closing_the_handle(self):
        child = subprocess.run([sys.executable, '-X', 'utf8', __file__, '--native-client-timeout'],
                               capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(child.returncode, 0, child.stderr)

    def test_idle_listener_timeout_releases_native_accept_and_serves_the_next_request(self):
        child = subprocess.run([sys.executable, '-X', 'utf8', __file__, '--native-idle-accept'],
                               capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(child.returncode, 0, child.stderr)


if __name__ == '__main__':
    if '--native-idle-accept' in sys.argv:
        native_idle_accept_child()
    elif '--native-client-timeout' in sys.argv:
        native_client_timeout_child()
    elif '--native-child' in sys.argv:
        native_child()
    elif '--native-delayed' in sys.argv:
        native_delayed_child()
    elif '--native-silent' in sys.argv:
        native_delayed_child(silent=True)
    elif '--native-explicit' in sys.argv:
        native_delayed_child(explicit=True)
    else:
        unittest.main()
