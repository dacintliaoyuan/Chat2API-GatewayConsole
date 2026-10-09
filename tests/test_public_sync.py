"""Publish through the real sync transaction using only temporary client files."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import model_sync


class PublicSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='public-sync-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        paths = {
            'DATA_DIR': self.root / 'state',
            'STATE_FILE': self.root / 'state' / 'state.json',
            'RESULT_FILE': self.root / 'state' / 'result.json',
            'PREVIEW_FILE': self.root / 'state' / 'preview.json',
            'HISTORY_FILE': self.root / 'state' / 'history.jsonl',
            'BACKUP_DIR': self.root / 'state' / 'backups',
            'LOCK_FILE': self.root / 'state' / 'sync.lock',
            'ZCODE_JSON': self.root / 'client' / 'config.json',
            'ZCODE_PROVIDER_CONFIG': self.root / 'client' / 'provider_config.json',
        }
        for name, value in paths.items():
            self.stack.enter_context(patch.object(model_sync, name, value))
        self.config = paths['ZCODE_JSON']
        self.store = paths['ZCODE_PROVIDER_CONFIG']
        self.config.parent.mkdir()
        self.config.write_text(json.dumps({'provider': {'local:openwebui': {
            'name': 'UM', 'models': {}, 'options': {
                'baseURL': 'http://127.0.0.1:8000/v1', 'apiKey': 'fixture-only'
            }
        }}}), encoding='utf-8')
        self.store.write_text(json.dumps({'schemaVersion': 1, 'config': {
            'providerOrder': ['local:openwebui'],
            'providerConfigRules': {'providerRules': [{
                'providerId': 'local:openwebui', 'providerName': 'UM', 'enabled': True,
                'config': {'group': 'standard-personal', 'personalModelIds': [], 'modelOrder': []}
            }]},
            'modelConfigRules': {'providerModelRules': []}
        }}), encoding='utf-8')
        self.stack.enter_context(patch.object(model_sync, 'zcode_running', return_value=False))
        self.catalog = self.stack.enter_context(patch.object(model_sync, '_get_json', return_value={
            'data': [{'id': 'public-model', 'context_length': 32768}]
        }))
        self.probes = self.stack.enter_context(patch.object(model_sync, 'probe_new_models'))

    def run_sync(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return model_sync.run_sync(apply=True, run_probes=False, confirm=False,
                                       scope={'sources': ['um'], 'consumers': ['ZCode']})

    def test_real_publish_readback_and_idempotent_repeat(self):
        legacy_before = self.config.read_bytes()
        first = self.run_sync()
        self.assertEqual(first['status'], 'changed', first)
        self.assertTrue(first['applied'])
        self.assertEqual(first['pending'], [])
        rules = json.loads(self.store.read_text(encoding='utf-8'))['config']
        provider = rules['providerConfigRules']['providerRules'][0]
        self.assertIn('public-model', provider['config']['personalModelIds'])
        model = next(row for row in rules['modelConfigRules']['providerModelRules']
                     if row['modelId'] == 'public-model')
        self.assertEqual(model['config']['properties']['contextWindow'], 32768)
        self.assertEqual(self.config.read_bytes(), legacy_before)
        self.assertEqual(first['applicationBoundary']['clientLoaded'], 'unverified')
        published_before = self.store.read_bytes()
        second = self.run_sync()
        self.assertEqual(second['applied'], [], second)
        self.assertEqual(second['pending'], [])
        self.assertEqual(self.store.read_bytes(), published_before)
        self.assertEqual(set(model_sync.load_state()['managed']), {'um|ZCode|local:openwebui'})
        self.probes.assert_not_called()

    def test_missing_rule_store_defers_all_client_writes(self):
        self.store.unlink()
        before = self.config.read_bytes()
        result = self.run_sync()
        self.assertEqual(result['status'], 'pending', result)
        self.assertEqual(result['applied'], [])
        self.assertTrue(result['pending'])
        self.assertFalse(self.store.exists())
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(result['applicationBoundary']['filesWritten'], [])
        self.assertEqual(model_sync.load_state()['managed'], {})
        self.probes.assert_not_called()

    def test_running_upgrade_is_decided_by_the_real_protocol_gate(self):
        import zcode_hot_sync
        import zcode_hot_sync_protocol
        from tests.test_zcode_hot_sync import _asar_bytes, _layout_entries, _scan
        from tests.zcode_protocol_fixture import profile
        original = _layout_entries()
        trusted = profile(original['out/host/index.js'],
                          original['out/host/chunk-FNQWK6LT.js'])
        installation = self.root / 'installation'
        (installation / 'resources').mkdir(parents=True)
        for compatible in (False, True):
            with self.subTest(compatible=compatible):
                entries = _layout_entries(version='99.0.0')
                entries['out/host/index.js'] += b';const unrelatedFeature=1;'
                if not compatible:
                    entries['out/host/index.js'] = entries['out/host/index.js'].replace(
                        b'start(){return 1}', b'start(){return 0}')
                (installation / 'resources' / 'app.asar').write_bytes(_asar_bytes(entries))
                before = self.store.read_bytes()
                legacy = self.config.read_bytes()
                with patch.object(model_sync, 'zcode_running', return_value=True), \
                        patch.object(zcode_hot_sync, 'zcode_process_scan',
                                     return_value=_scan(installation)), \
                        patch.object(zcode_hot_sync_protocol, 'PROTOCOL_PROFILE', trusted), \
                        patch('subprocess.run', side_effect=AssertionError('No client restart')):
                    result = self.run_sync()
                self.assertEqual(self.config.read_bytes(), legacy)
                self.assertEqual(result['status'], 'changed' if compatible else 'pending')
                if compatible:
                    self.assertNotEqual(self.store.read_bytes(), before)
                    self.assertTrue(result['applied'])
                else:
                    self.assertEqual(self.store.read_bytes(), before)
                    self.assertEqual(result['applied'], [])
                    self.assertEqual(result['applicationBoundary']['filesWritten'], [])
        self.probes.assert_not_called()


if __name__ == '__main__':
    unittest.main()
