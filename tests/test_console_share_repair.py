"""Fixed privileged repair, signed dispatch and orchestration; fully isolated."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import console_share_actions as actions
import share_broker_protocol as protocol
import share_broker_server as server
from share_broker_host import ReplayRegistry
from share_broker_ops import WindowsHostOps
from console_operations import OperationError
from share_broker_client import BrokerClient


class RepairTests(unittest.TestCase):
    def setUp(self):
        import share_public_supervision as supervision

        sandbox = tempfile.TemporaryDirectory(prefix='share-repair-cancel-')
        self.addCleanup(sandbox.cleanup)
        self.public_state_dir = str(Path(sandbox.name) / 'watch')
        # The cancellation boundary is file I/O, not a subprocess: redirect it
        # explicitly even if an earlier assembly test left production seams.
        cancel = mock.patch.multiple(actions,
            _PUBLIC_CANCEL_REQUEST=lambda reason: supervision.request_cancel(
                reason, state_dir=self.public_state_dir),
            _PUBLIC_CANCEL_CLEAR=lambda: supervision.clear_cancel(
                state_dir=self.public_state_dir))
        cancel.start()
        self.addCleanup(cancel.stop)
        state = mock.patch.object(actions, '_STATE', actions._ManagedStopState())
        state.start()
        self.addCleanup(state.stop)

    def test_signed_empty_verb_and_replay_boundary(self):
        calls = []
        host = SimpleNamespace(repair_limits=lambda: calls.append('repair') or
                               {'repaired': True, 'code': 'ok'})
        inner = SimpleNamespace(mgmt_secret='fixture', replay=ReplayRegistry())
        gate = server._HostOpsControlCoordinator(inner, host)
        body = protocol.build_request('fixture', 'host-repair-limits', {})
        self.assertTrue(gate.handle_management(body)['result']['repaired'])
        self.assertEqual(gate.handle_management(body)['error_code'], 'replay')
        for fields in ({'path': 'foreign.ps1'}, {'port': 1234}, {'command': 'x'}):
            reply = gate.handle_management(protocol.build_request('fixture', 'host-repair-limits', fields))
            self.assertEqual(reply['error_code'], 'bad_request')
        reply = gate.handle_management(protocol.build_request('wrong', 'host-repair-limits'))
        self.assertEqual(reply['error_code'], 'forbidden')
        self.assertEqual(calls, ['repair'])

    def test_orchestration_withdraws_then_stops_then_repairs_and_checks(self):
        calls = []
        ops = SimpleNamespace(emergency_stop=lambda: calls.append('stop') or {'stopped': True},
            verify_closed=lambda: True,
            repair_limits=lambda: calls.append('repair') or {'repaired': True, 'code': 'ok'})
        security = {'state': 'ok', 'verdict': 'confirmed', 'checked_at': 1,
                    'evidence': {'verdict': True, 'detail': 'ok'}}
        with mock.patch.multiple(actions, _BROKER_OPS=ops,
                _FUNNEL_CONTROL=lambda value: calls.append(value) or True,
                _FUNNEL_OBSERVED=lambda: False,
                _INTENT_WRITE=lambda *a, **k: True), \
                mock.patch.object(actions, '_diag_security_gate', return_value=security):
            result = actions.repair_limits('share', {})
        self.assertEqual(calls, ['off', 'stop', 'repair'])
        self.assertTrue(result['repaired'])
        self.assertTrue(result['public_closed'])
        self.assertTrue(result['gateway_stopped'])

    def test_failed_public_close_never_rebuilds_rules(self):
        repair = mock.Mock()
        with mock.patch.multiple(actions, _BROKER_OPS=SimpleNamespace(repair_limits=repair),
                _FUNNEL_CONTROL=lambda value: True, _FUNNEL_OBSERVED=lambda: True):
            with self.assertRaises(OperationError):
                actions.repair_limits('share', {})
        repair.assert_not_called()

    def test_client_uses_empty_fixed_verb_and_bounded_timeout(self):
        client = BrokerClient(lambda: None, 'fixture')
        with mock.patch.object(client, 'call', return_value=object()) as call:
            client.repair_limits()
        call.assert_called_once_with('host-repair-limits', timeout_s=120)

    def test_failed_stop_intent_never_rebuilds_rules(self):
        repair = mock.Mock()
        stop = mock.Mock()
        with mock.patch.multiple(actions, _BROKER_OPS=SimpleNamespace(repair_limits=repair, emergency_stop=stop),
                _FUNNEL_CONTROL=lambda value: True, _FUNNEL_OBSERVED=lambda: False,
                _INTENT_WRITE=lambda *a, **k: False):
            with self.assertRaises(OperationError):
                actions.repair_limits('share', {})
        repair.assert_not_called()
        stop.assert_not_called()

    def test_failed_final_evidence_never_claims_success(self):
        ops = SimpleNamespace(emergency_stop=lambda: {'stopped': True}, verify_closed=lambda: True,
            repair_limits=lambda: {'repaired': True})
        with mock.patch.multiple(actions, _BROKER_OPS=ops,
                _FUNNEL_CONTROL=lambda value: True, _FUNNEL_OBSERVED=lambda: False,
                _INTENT_WRITE=lambda *a, **k: True), \
                mock.patch.object(actions, '_diag_security_gate', return_value={'state': 'unknown', 'verdict': 'unconfirmed'}):
            with self.assertRaises(OperationError):
                actions.repair_limits('share', {})

    def test_host_pins_protected_script_and_rejects_false_success(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory, 'repair-limits.ps1')
            helper.write_text('# private fixture', encoding='utf-8')
            calls = []
            def runner(args, timeout):
                calls.append(args)
                return SimpleNamespace(returncode=0, stdout='REPAIR_RESULT=' +
                    json.dumps({'repaired': True, 'code': 'ok'}))
            host = WindowsHostOps('fixture', runner=runner, run_root=directory)
            with mock.patch.object(host, 'verify_closed', return_value=True), \
                    mock.patch.object(host, 'verify_limits_evidence', return_value={'verdict': False, 'detail': 'wfp-filter-missing'}):
                self.assertFalse(host.repair_limits()['repaired'])
            self.assertIn(str(helper), calls[0])
            self.assertNotIn('-Command', calls[0])


if __name__ == '__main__':
    unittest.main()
