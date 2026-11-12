"""Version, concurrency and subprocess boundaries for public sharing probes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import console_share_probe as probe

URL = 'https://fixture.tail-test.ts.net'
SECRET = 'fixture-secret'
SUCCESS = {'connected': True, 'reason': 'ok', 'stage': 'inference',
           'http_status': 200, 'elapsed_ms': 42, 'model': 'fixture-model',
           'public_ip': '1.1.1.1', 'dns': None}


def manager():
    value = mock.MagicMock()
    value.get_key.return_value = {'row_version': 3, 'enabled': True, 'deleted_at': None}
    value.broker.host_verify.return_value = True
    value.reveal_key_secret.return_value = {'secret': SECRET}
    return value


class WorkerBoundaryTests(unittest.TestCase):
    def test_only_stdin_carries_secret_and_worker_has_a_hard_deadline(self):
        process = subprocess.CompletedProcess([], 0, json.dumps(SUCCESS).encode())
        with mock.patch.object(probe.subprocess, 'run', return_value=process) as run:
            result = probe.probe_models(SECRET, URL)
        args, options = run.call_args
        self.assertEqual(args[0][0], sys.executable)
        self.assertIn('-I', args[0])
        self.assertEqual(Path(args[0][-1]).name, 'console_public_probe.py')
        self.assertNotIn(SECRET, repr(args))
        self.assertEqual(json.loads(options['input']), {'secret': SECRET, 'public_url': URL})
        self.assertEqual(options['timeout'], 65)
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        self.assertEqual(options['stdout'], subprocess.PIPE)
        self.assertFalse(options.get('shell', False))
        self.assertEqual(result, SUCCESS)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_timeout_or_worker_errors_cannot_report_success_or_leak(self):
        outcomes = (subprocess.TimeoutExpired(['worker'], 65, output=SECRET.encode()),
                    OSError(SECRET),
                    subprocess.CompletedProcess([], 1, SECRET.encode()),
                    subprocess.CompletedProcess([], 0, SECRET.encode()),
                    subprocess.CompletedProcess([], 0, b'x' * 8193))
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__):
                option = {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome}
                with mock.patch.object(probe.subprocess, 'run', **option):
                    result = probe.probe_models(SECRET, URL)
                self.assertFalse(result['connected'])
                self.assertNotIn(SECRET, json.dumps(result))
                self.assertEqual(result['reason'], 'timeout' if isinstance(outcome, subprocess.TimeoutExpired) else 'probe_failed')

    def test_catalog_only_or_internal_ip_is_never_a_success(self):
        changes = ({'stage': 'models'}, {'public_ip': '127.0.0.1'},
                   {'public_ip': '100.119.187.73'}, {'public_ip': '198.18.2.161'},
                   {'public_ip': None}, {'http_status': 401}, {'model': None},
                   {'connected': 1}, {'elapsed_ms': True}, {'reason': 'private body'},
                   {'elapsed_ms': 65001}, {'http_status': True})
        for change in changes:
            with self.subTest(change=change):
                process = subprocess.CompletedProcess([], 0, json.dumps({**SUCCESS, **change}).encode())
                with mock.patch.object(probe.subprocess, 'run', return_value=process):
                    result = probe.probe_models(SECRET, URL)
                self.assertFalse(result['connected'])
                self.assertEqual(result['reason'], 'probe_failed')

    def test_real_isolated_worker_startup_with_invalid_origin_has_no_network(self):
        result = probe.probe_models(SECRET, 'http://127.0.0.1:8100')
        self.assertFalse(result['connected'])
        self.assertEqual(result['reason'], 'public_url_unavailable')
        self.assertEqual(result['stage'], 'configuration')

    def test_dns_detail_is_preserved_and_malformed_detail_fails(self):
        dns = {
            'version': 1, 'checked_at_utc': '2026-10-26T00:00:00+00:00',
            'checked_at_epoch': 1000.0, 'state': 'disagreement',
            'reason': 'dns_disagreement', 'addresses': ['8.8.8.8'],
            'addresses_truncated': False, 'ttl_s': 60,
            'negative_ttl_s': 300,
            'sources': [
                {'source': 'cloudflare', 'state': 'nxdomain', 'status': 3,
                 'classification': 'nxdomain', 'addresses': [],
                 'addresses_truncated': False, 'ttl_s': None,
                 'negative_ttl_s': 300, 'checked_at_epoch': 1000.0,
                 'elapsed_ms': 20},
                {'source': 'google', 'state': 'ok', 'status': 0,
                 'classification': 'ok', 'addresses': ['8.8.8.8'],
                 'addresses_truncated': False, 'ttl_s': 60,
                 'negative_ttl_s': None, 'checked_at_epoch': 1000.0,
                 'elapsed_ms': 18},
            ],
        }
        process = subprocess.CompletedProcess(
            [], 0, json.dumps({**SUCCESS, 'dns': dns}).encode())
        with mock.patch.object(probe.subprocess, 'run', return_value=process):
            result = probe.probe_models(SECRET, URL)
        self.assertEqual(result['dns']['state'], 'disagreement')
        self.assertEqual(len(result['dns']['sources']), 2)
        self.assertNotIn(SECRET, json.dumps(result))
        for broken in (
                {**dns, 'state': 'healthy'},
                {**dns, 'addresses': ['127.0.0.1']},
                {**dns, 'sources': [{'source': 'other', 'state': 'ok'}]},
                {**dns, 'sources': [{'source': 'cloudflare', 'state': 'ok',
                                      'classification': 'ok',
                                      'addresses': ['127.0.0.1']}]},
                {**dns, 'ttl_s': -1}):
            with self.subTest(broken=broken):
                process = subprocess.CompletedProcess(
                    [], 0, json.dumps({**SUCCESS, 'dns': broken}).encode())
                with mock.patch.object(probe.subprocess, 'run', return_value=process):
                    result = probe.probe_models(SECRET, URL)
                self.assertFalse(result['connected'])
                self.assertEqual(result['reason'], 'probe_failed')


class ConfiguredOriginTests(unittest.TestCase):
    def test_configured_origin_is_the_only_public_origin(self):
        configured = 'https://dacintapi.de5.net'
        with mock.patch.dict(os.environ,
                             {'CONSOLE_PUBLIC_ORIGIN': configured}):
            value = manager()
            with mock.patch.object(probe, 'probe_models',
                                   return_value=SUCCESS) as network:
                result = probe.test_key(value, 'fixture-key', 3,
                                        public_url=configured)
            self.assertTrue(result['connected'])
            self.assertEqual(result['public_url'], configured)
            network.assert_called_once_with(SECRET, configured)
            value = manager()
            with mock.patch.object(probe, 'probe_models') as network:
                result = probe.test_key(value, 'fixture-key', 3, public_url=URL)
            self.assertFalse(result['connected'])
            self.assertEqual(result['reason'], 'public_url_unavailable')
            value.reveal_key_secret.assert_not_called()
            network.assert_not_called()

    def test_invalid_configured_origin_accepts_no_public_origin(self):
        value = manager()
        with mock.patch.dict(os.environ, {
                'CONSOLE_PUBLIC_ORIGIN': 'https://dacintapi.de5.net/v1'}), \
                mock.patch.object(probe, 'probe_models') as network:
            result = probe.test_key(value, 'fixture-key', 3, public_url=URL)
        self.assertFalse(result['connected'])
        self.assertEqual(result['reason'], 'public_url_unavailable')
        value.reveal_key_secret.assert_not_called()
        network.assert_not_called()


class KeyProbeTests(unittest.TestCase):
    def test_public_origin_and_version_are_bound_without_returning_secret(self):
        value = manager()
        with mock.patch.object(probe, 'probe_models', return_value=SUCCESS) as network:
            result = probe.test_key(value, 'fixture-key', 3, public_url=URL)
        self.assertTrue(result['connected'])
        self.assertEqual(result['tested_from'], 'public')
        self.assertEqual(result['public_url'], URL)
        self.assertEqual(result['row_version'], 3)
        network.assert_called_once_with(SECRET, URL)
        value.broker.host_verify.assert_called_once_with('running')
        self.assertNotIn(SECRET, json.dumps(result))

    def test_key_test_keeps_the_dns_detail_without_returning_secret(self):
        dns = {
            'version': 1, 'checked_at_utc': '2026-10-26T00:00:00+00:00',
            'checked_at_epoch': 1000.0, 'state': 'disagreement',
            'reason': 'dns_disagreement', 'addresses': ['8.8.8.8'],
            'addresses_truncated': False, 'ttl_s': 60,
            'negative_ttl_s': 300, 'sources': [],
        }
        value = manager()
        with mock.patch.object(probe, 'probe_models',
                               return_value={**SUCCESS, 'dns': dns}):
            result = probe.test_key(value, 'fixture-key', 3, public_url=URL)
        self.assertTrue(result['connected'])
        self.assertEqual(result['dns']['state'], 'disagreement')
        self.assertNotIn(SECRET, json.dumps(result))

    def test_invalid_or_missing_origin_never_reveals_or_sends_the_key(self):
        for url in (None, 'http://127.0.0.1:8100', 'https://example.com'):
            value = manager()
            with mock.patch.object(probe, 'probe_models') as network:
                result = probe.test_key(value, 'fixture-key', 3, public_url=url)
            self.assertFalse(result['connected'])
            self.assertEqual(result['reason'], 'public_url_unavailable')
            value.reveal_key_secret.assert_not_called()
            network.assert_not_called()

    def test_disabled_stale_and_unverified_keys_never_send_a_credential(self):
        cases = (({'row_version': 4, 'enabled': True}, True, 'key_changed'),
                 ({'row_version': 3, 'enabled': False}, True, 'key_disabled'),
                 ({'row_version': 3, 'enabled': True}, None, 'gateway_unconfirmed'))
        for row, verified, reason in cases:
            value = manager()
            value.get_key.return_value = row
            value.broker.host_verify.return_value = verified
            with mock.patch.object(probe, 'probe_models') as network:
                result = probe.test_key(value, 'fixture-key', 3, public_url=URL)
            self.assertFalse(result['connected'])
            self.assertEqual(result['reason'], reason)
            value.reveal_key_secret.assert_not_called()
            network.assert_not_called()

    def test_rotated_disabled_or_deleted_key_during_probe_invalidates_success(self):
        for current in ({'row_version': 4, 'enabled': True},
                        {'row_version': 3, 'enabled': False},
                        {'row_version': 3, 'enabled': True, 'deleted_at': 123}):
            value = manager()
            value.get_key.side_effect = [{'row_version': 3, 'enabled': True}, current]
            with mock.patch.object(probe, 'probe_models', return_value=SUCCESS):
                result = probe.test_key(value, 'fixture-key', 3, public_url=URL)
            self.assertFalse(result['connected'])
            self.assertEqual(result['reason'], 'key_changed')

    def test_duplicate_or_excess_probe_is_rejected_and_capacity_is_released(self):
        value = manager()
        entered, release = threading.Event(), threading.Event()
        results = []

        def blocked(*args):
            entered.set()
            if not release.wait(2):
                raise AssertionError('test synchronization timeout')
            return SUCCESS

        discovery = mock.Mock(return_value=URL)
        with mock.patch.object(probe, 'probe_models', side_effect=blocked) as network:
            thread = threading.Thread(target=lambda: results.append(
                probe.test_key(value, 'fixture-key', 3, discover_url=discovery)))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                duplicate = probe.test_key(value, 'fixture-key', 3, discover_url=discovery)
                self.assertEqual(duplicate['reason'], 'test_in_progress')
                self.assertEqual(network.call_count, 1)
                discovery.assert_called_once_with()
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(results[0]['connected'])
        with mock.patch.object(probe, 'probe_models', return_value=SUCCESS):
            self.assertTrue(probe.test_key(value, 'fixture-key', 3, public_url=URL)['connected'])
        with probe._ACTIVE_LOCK:
            probe._ACTIVE_KEYS.update(('busy-a', 'busy-b'))
        try:
            with mock.patch.object(probe, 'probe_models') as network:
                result = probe.test_key(value, 'another-key', 3, public_url=URL)
            self.assertEqual(result['reason'], 'test_in_progress')
            network.assert_not_called()
        finally:
            with probe._ACTIVE_LOCK:
                probe._ACTIVE_KEYS.difference_update(('busy-a', 'busy-b'))


if __name__ == '__main__':
    unittest.main()
