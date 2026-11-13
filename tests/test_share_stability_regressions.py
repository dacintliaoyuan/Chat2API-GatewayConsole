"""Regression loops for the observed sharing recovery and public-path failures."""
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_console_public_probe import (
    ANSWER, CATALOG, PUBLIC_IP, PUBLIC_URL, SECRET, isolated_http, probe, reply,
)
from console import share_actions as actions
import share.broker_client as client_module
from share.broker_server import BoundedPipeServer
from share.broker_ops import WindowsHostOps


class PublicIngressRecoveryTests(unittest.TestCase):
    def test_first_ingress_tls_failure_does_not_hide_working_public_ingress(self):
        second_ip = '203.0.113.11'
        attempts = []
        with isolated_http([reply(CATALOG), reply(ANSWER)]) as (seen, _, factory):
            def connect(host, address, timeout):
                attempts.append(address)
                if address == PUBLIC_IP:
                    raise ssl.SSLEOFError('fixture ingress closed during TLS')
                return factory(host, address, timeout)

            with mock.patch.object(probe, '_model_addresses',
                                   return_value=[PUBLIC_IP, second_ip]), \
                    mock.patch.object(probe, '_public_connection', connect):
                result = probe.probe_public_model(SECRET, PUBLIC_URL)

        self.assertTrue(result['connected'], result)
        self.assertEqual(result['public_ip'], second_ip)
        self.assertEqual(attempts, [PUBLIC_IP, second_ip, second_ip])
        self.assertEqual([(row['method'], row['path']) for row in seen],
                         [('GET', '/v1/models'), ('POST', '/v1/chat/completions')])

    def test_blackholed_ingress_leaves_budget_for_the_next_address(self):
        now = [0.0]
        second_ip = '203.0.113.11'
        attempts = []
        with isolated_http([reply(CATALOG), reply(ANSWER)]) as (_, _, factory):
            def connect(host, address, timeout):
                attempts.append((address, timeout))
                if address == PUBLIC_IP:
                    now[0] += timeout
                    raise TimeoutError('fixture ingress connection timed out')
                return factory(host, address, timeout)

            with mock.patch.object(probe, '_model_addresses',
                                   return_value=[PUBLIC_IP, second_ip]), \
                    mock.patch.object(probe, '_public_connection', connect), \
                    mock.patch.object(probe.time, 'monotonic', side_effect=lambda: now[0]):
                result = probe.probe_public_model(SECRET, PUBLIC_URL)

        self.assertTrue(result['connected'], result)
        self.assertLess(attempts[0][1], probe.TIMEOUT_SECONDS)
        self.assertLess(now[0], probe.TIMEOUT_SECONDS)

    def test_authentication_rejection_does_not_try_another_ingress(self):
        with isolated_http([reply({'error': 'invalid_api_key'}, status=401)]) as (seen, connections, factory):
            with mock.patch.object(probe, '_model_addresses',
                                   return_value=[PUBLIC_IP, '203.0.113.11']), \
                    mock.patch.object(probe, '_public_connection', factory):
                result = probe.probe_public_model(SECRET, PUBLIC_URL)
        self.assertFalse(result['connected'])
        self.assertEqual(result['reason'], 'key_rejected')
        self.assertEqual(len(connections), 1)
        self.assertEqual(len(seen), 1)

    def test_inference_response_loss_never_replays_the_model_request(self):
        responses = [reply(CATALOG), reply(b'{"choices":', length=40, truncate=True)]
        with isolated_http(responses) as (seen, connections, factory):
            with mock.patch.object(probe, '_model_addresses',
                                   return_value=[PUBLIC_IP, '203.0.113.11']), \
                    mock.patch.object(probe, '_public_connection', factory):
                result = probe.probe_public_model(SECRET, PUBLIC_URL)
        self.assertFalse(result['connected'])
        self.assertEqual(result['stage'], 'inference')
        self.assertEqual(sum(row['method'] == 'POST' for row in seen), 1)
        self.assertEqual(len(connections), 2)

    def test_address_attempts_remain_bounded_when_all_ingresses_fail(self):
        addresses = ['203.0.113.' + str(n) for n in range(10, 20)]
        attempts = []

        def connect(host, address, timeout):
            attempts.append(address)
            raise ssl.SSLEOFError('fixture ingress closed during TLS')

        with mock.patch.object(probe, '_model_addresses', return_value=addresses), \
                mock.patch.object(probe, '_public_connection', connect):
            result = probe.probe_public_model(SECRET, PUBLIC_URL)
        self.assertFalse(result['connected'])
        self.assertEqual(attempts, addresses[:4])


class LimitsFailureClassificationTests(unittest.TestCase):
    def test_composite_security_check_can_finish_beyond_one_command_budget(self):
        calls = []

        def runner(args, timeout):
            calls.append(timeout)
            if timeout < 12:
                raise subprocess.TimeoutExpired('fixture', timeout)
            return SimpleNamespace(returncode=0, stdout='LIMITS=OK\n')

        ops = WindowsHostOps('fixture', runner=runner,
                             run_root='fixture', watchdog_script='fixture.ps1')
        self.assertIs(ops.verify_limits_evidence()['verdict'], True)
        self.assertLessEqual(calls[0], 30, 'the check must fit its 35-second exchange')

    def test_check_timeout_is_distinct_from_a_missing_or_denied_executable(self):
        errors = [
            (subprocess.TimeoutExpired('fixture', 10), 'limits_timeout'),
            (FileNotFoundError('fixture'), 'limits_executable_missing'),
            (PermissionError('fixture'), 'limits_permission_denied'),
        ]
        for error, reason in errors:
            with self.subTest(reason=reason):
                def runner(*args):
                    raise error

                ops = WindowsHostOps('fixture', runner=runner,
                                     run_root='fixture', watchdog_script='fixture.ps1')
                observed = ops.verify_limits_evidence()
                self.assertIsNone(observed['verdict'])
                self.assertEqual(observed['detail'], reason)


class MaintenanceReadTests(unittest.TestCase):
    def test_uncertain_previous_gateway_state_never_grants_rollback_ownership(self):
        owned = []
        ledger = SimpleNamespace(note=lambda note: None,
                                 own=lambda layer, note: owned.append(layer))
        broker = SimpleNamespace(verify_running=mock.Mock(side_effect=[None, True]),
                                 start_host=lambda: True)
        with mock.patch.object(actions, '_BROKER_OPS', broker):
            actions._open_backend(None, ledger, {'backend': 1.0}, .01)
        self.assertEqual(owned, [], 'an unknown previous state is not proof this operation started it')

    def test_not_running_without_closed_evidence_is_not_a_newly_owned_gateway(self):
        owned = []
        ledger = SimpleNamespace(note=lambda note: None,
                                 own=lambda layer, note: owned.append(layer))
        broker = SimpleNamespace(verify_running=mock.Mock(side_effect=[False, True]),
                                 verify_closed=lambda: False, start_host=lambda: True)
        with mock.patch.object(actions, '_BROKER_OPS', broker):
            actions._open_backend(None, ledger, {'backend': 1.0}, .01)
        self.assertEqual(owned, [], 'a failed running proof is not proof the previous process was closed')

    def test_an_existing_funnel_is_not_owned_by_a_repeated_open(self):
        owned = []
        ledger = SimpleNamespace(note=lambda note: None,
                                 own=lambda layer, note: owned.append(layer))
        with mock.patch.object(actions, '_FUNNEL_OBSERVED', lambda: True), \
                mock.patch.object(actions, '_FUNNEL_CONTROL', lambda action: True):
            actions._open_funnel(None, ledger)
        self.assertEqual(owned, [], 'a later failure must not close a pre-existing public mapping')

    def test_backend_comparison_reads_only_the_two_returned_layers(self):
        backend = {'state': 'running', 'verdict': 'confirmed'}
        auth = {'state': 'valid', 'verdict': 'confirmed'}
        with mock.patch.object(actions, '_diag_share_backend', return_value=backend), \
                mock.patch.object(actions, '_diag_backend_auth', return_value=auth), \
                mock.patch.object(actions, '_diag_share_entry', return_value={}) as unrelated:
            result = actions._diag_pair()
        self.assertEqual(result, {'share_backend': backend, 'backend_auth': auth})
        unrelated.assert_not_called()

    def test_cancel_during_security_check_prevents_the_restart_side_effect(self):
        context = SimpleNamespace(cancelled=False, report_step=lambda note: None)

        def check_cancel():
            if context.cancelled:
                raise actions.OperationCancelled()

        context.raise_if_cancelled = check_cancel

        def limits(check):
            context.cancelled = True
            return {'verdict': True}

        restart = mock.Mock(return_value={
            'restarted': True, 'stopped_confirmed': True, 'running_confirmed': True})
        with mock.patch.object(actions, 'diagnostics', return_value={'layers': {}}), \
                mock.patch.object(actions, '_BROKER_OPS', SimpleNamespace(host_evidence=limits)), \
                mock.patch.object(actions, '_GATEWAY_RESTART', restart), \
                mock.patch.object(actions, '_maintenance_status_read', return_value=(None, None)):
            with self.assertRaises(actions.OperationCancelled):
                actions.gateway_restart('share', {}, context)
        restart.assert_not_called()


class PipeServingGapTests(unittest.TestCase):
    def test_native_already_connected_code_is_a_valid_connection(self):
        class Kernel:
            def ConnectNamedPipe(self, *args):
                return 0

            def GetLastError(self):
                return 535

        server = BoundedPipeServer(r'\\.\pipe\fixture', kernel=(Kernel(), None))
        server._bounded_connect(1, threading.Event(), time.monotonic() + 1)

    def test_wx86_warning_is_not_a_valid_pipe_connection(self):
        class Kernel:
            def ConnectNamedPipe(self, *args):
                return 0

            def GetLastError(self):
                return 539

        server = BoundedPipeServer(r'\\.\pipe\fixture', kernel=(Kernel(), None))
        with self.assertRaises(OSError):
            server._bounded_connect(1, threading.Event(), time.monotonic() + 1)

    def test_pipe_teardown_errors_are_retried_only_before_a_request_is_sent(self):
        for error in (109, 232, 233):
            with self.subTest(winerror=error):
                now = [0.0]

                class Kernel:
                    def CreateFileW(self, *args):
                        return 42 if now[0] >= .1 else 0

                    def GetLastError(self):
                        return error

                    def CloseHandle(self, handle):
                        return 1

                transport = client_module.NamedPipeBrokerTransport(kernel=Kernel())
                with mock.patch.object(client_module.time, 'monotonic', side_effect=lambda: now[0]), \
                        mock.patch.object(client_module.time, 'sleep', side_effect=lambda delay: now.__setitem__(0, now[0] + delay)):
                    transport.open(deadline=1.0)
                    transport.close()
                self.assertLess(now[0], 1.0)

    def test_busy_pipe_followed_by_serving_gap_retains_remaining_exchange_budget(self):
        now = [0.0]
        class Kernel:
            def CreateFileW(self, *args):
                return 42 if now[0] >= 3.1 else 0
            def GetLastError(self):
                return 231 if now[0] < 3.0 else 2
            def CloseHandle(self, handle):
                return 1
        transport = client_module.NamedPipeBrokerTransport(kernel=Kernel())
        with mock.patch.object(client_module.time, 'monotonic', side_effect=lambda: now[0]), \
                mock.patch.object(client_module.time, 'sleep', side_effect=lambda delay: now.__setitem__(0, now[0] + delay)):
            transport.open(deadline=5.0)
            transport.close()
        self.assertGreaterEqual(now[0], 3.1)
        self.assertLess(now[0], 5.0)

    def test_status_can_queue_behind_bounded_security_read_without_extending_mutations(self):
        import json
        now = [0.0]
        class QueuedTransport:
            def open(self, deadline=None):
                if deadline < 26:
                    raise TimeoutError('fixture security read owns the pipe')
                now[0] = 26.0
            def send_all(self, data, deadline):
                self.request = json.loads(data)
            def recv_line(self, deadline):
                self.bound = deadline
                return json.dumps({'v': 2, 'request_id': self.request['request_id'],
                                   'ok': True, 'error_code': 'ok', 'result': {}}).encode()
            def close(self):
                pass
        transport = QueuedTransport()
        client = client_module.BrokerClient(lambda: transport, 'isolated-key')
        with mock.patch.object(client_module.time, 'monotonic', side_effect=lambda: now[0]):
            result = client.status()
            self.assertTrue(result.ok, result.error_code)
            self.assertLessEqual(transport.bound, 35.0)
            now[0] = 0.0
            mutation = client.start()
        self.assertFalse(mutation.ok)
        self.assertEqual(mutation.error_code, 'deadline_exceeded')

    def test_finished_worker_cannot_cancel_another_thread_after_native_id_reuse(self):
        worker = threading.Thread(target=lambda: None)
        worker.start()
        worker.join()
        cancelled = []
        closed = []
        kernel = SimpleNamespace(OpenThread=lambda *args: 42,
                                 CancelSynchronousIo=lambda handle: cancelled.append(handle),
                                 CloseHandle=lambda handle: closed.append(handle))
        self.assertFalse(client_module.cancel_synchronous_worker_io(kernel, worker))
        self.assertEqual(cancelled, [])
        self.assertEqual(closed, [42])

    def test_permission_denial_is_not_reported_as_an_uninstalled_broker(self):
        calls = []

        class Kernel:
            def CreateFileW(self, *args):
                calls.append('open')
                return 0

            def GetLastError(self):
                return 5

        transport = client_module.NamedPipeBrokerTransport(kernel=Kernel())
        client = client_module.BrokerClient(lambda: transport, 'isolated-key')
        result = client.status()
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, 'access_denied')
        self.assertIn('winerror=5', result.result['detail'])
        self.assertEqual(calls, ['open'], 'permission denial must not be retried')


if __name__ == '__main__':
    unittest.main()
