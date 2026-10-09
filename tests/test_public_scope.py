"""Exercise public assembly with no live HTTP, client files or tasks."""
import json
import sys
import tempfile
import os
import unittest
import threading
from contextlib import closing
import urllib.request
from http.server import ThreadingHTTPServer
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import console_adapters
import console_gateway_connection
import console_log_reader
import console_model_actions
import console_operations
import console_speedtest
import console_stats
import model_sync
import model_sync_core
import usagesrv

class PublicScopeTests(unittest.TestCase):
    def test_registry_has_only_supported_actions(self):
        actions=console_adapters.build_registry()
        names=[a.name for a in actions]
        self.assertTrue(names)
        self.assertTrue(all(name.startswith(('service.','account.','model.','share.')) for name in names),names)
        self.assertIn('model.sync_apply',names)
        self.assertIn('share.backend_restart',names)

    def test_removed_service_cannot_execute(self):
        for key in ['Qoder','QoderGlobal','CLIProxy','M365','unknown']:
            self.assertFalse(console_adapters.runtime_target_enabled('service.start',key)[0])
            with self.assertRaises(ValueError):
                console_gateway_connection.connection_info(key,snapshot={})

    def test_only_supported_binding(self):
        self.assertEqual(model_sync.ACTIVE_SOURCES,('um',))
        self.assertEqual(model_sync.KNOWN_CONSUMERS,('ZCode',))
        self.assertEqual([(b.source,b.consumer) for b in model_sync_core.DEFAULT_BINDINGS],[('um','ZCode')])

    def test_other_source_or_client_rejected(self):
        for scope in [{'sources':['cliproxy']},{'consumers':['DSH']},{'sources':[]},{'consumers':[]}]:
            with self.assertRaises(ValueError): model_sync._normalize_scope(scope)

    def test_catalog_reads_only_chat2api(self):
        with patch.object(model_sync,'_get_json',return_value={'data':[{'id':'Gemini-from-OpenWebUI','context_length':32768}]}) as get:
            catalogs,warnings=model_sync.collect_catalogs()
        self.assertEqual(list(catalogs),['um'])
        self.assertEqual(catalogs['um'][0].model_id,'Gemini-from-OpenWebUI')
        self.assertEqual(catalogs['um'][0].context,32768)
        self.assertEqual(warnings,[])
        get.assert_called_once_with(model_sync.UM_MODELS_URL,token=model_sync.chat2api_local_key())

    def test_local_call_key_is_forwarded_without_upstream_token(self):
        with patch.dict(os.environ,{'CHAT2API_LOCAL_API_KEY':'public-test-key'},clear=False):
            with patch.object(model_sync,'_get_json',return_value={'data':[]}) as get:
                model_sync.collect_catalogs()
            get.assert_called_once_with(model_sync.UM_MODELS_URL,token='public-test-key')
            _,token,readable=model_sync.endpoint_credential(model_sync.CONTEXT_PROBE_ENDPOINTS['um'])
            self.assertEqual(token,'public-test-key')
            self.assertTrue(readable)
        with patch.dict(os.environ,{'CHAT2API_LOCAL_API_KEY':''},clear=False):
            _,token,readable=model_sync.endpoint_credential(model_sync.CONTEXT_PROBE_ENDPOINTS['um'])
            self.assertIsNone(token)
            self.assertTrue(readable)

    def test_catalog_failure_is_not_successful_empty(self):
        with patch.object(model_sync,'_get_json',side_effect=OSError('stub')):
            catalogs,warnings=model_sync.collect_catalogs()
        self.assertEqual(catalogs,{})
        self.assertTrue(warnings)

    def test_sync_scope_requires_explicit_intent(self):
        with self.assertRaises(Exception): console_model_actions._console_scope({})
        with self.assertRaises(Exception): console_model_actions._console_scope({'scope_mode':'all','sources':['um']})
        self.assertIsNone(console_model_actions._console_scope({'scope_mode':'all'}))

    def test_speedtest_rejects_other_sources_before_io(self):
        for source in ['qoder_cn','cliproxy','m365','unknown']:
            with self.assertRaises(Exception): console_speedtest._validate_params({'source':source,'model':'stub'})

    def test_ledgers_and_log_sources_are_scoped(self):
        self.assertEqual(set(console_stats.default_ledgers()),{'chat2api'})
        self.assertEqual(set(console_stats.default_sources()),{'chat2api'})
        ids={row['source'] for row in console_log_reader.build_registry()['sources']}
        self.assertEqual(ids,{'chat2api','console','share'})

    def test_empty_ledger_summary_and_unknown_price(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            with patch.object(usagesrv,'CHAT2API_LOG',str(path/'missing.jsonl')), patch.object(usagesrv,'PRICE_FILE',str(path/'missing-prices.json')), patch.object(usagesrv,'OVERRIDE_FILE',str(path/'missing-override.json')):
                usagesrv._price_cache.update(at=0.0,data=None)
                summary=usagesrv.build_summary()
                self.assertEqual(set(summary['services']),{'chat2api'})
                self.assertEqual(summary['services']['chat2api']['requests'],0)
                self.assertEqual(usagesrv.resolve_price('unknown-model')[0],None)

    def test_read_api_dispatch_with_isolated_server(self):
        with tempfile.TemporaryDirectory() as directory:
            manager=console_operations.OperationManager(
                db_path=str(Path(directory)/'operations.sqlite3'),
                registry=console_adapters.build_registry(),
                lock_dir=str(Path(directory)/'locks'))
            with closing(manager), patch.object(usagesrv,'CHAT2API_LOG',str(Path(directory)/'missing.jsonl')), patch.object(usagesrv,'get_console_manager',return_value=manager):
                server=ThreadingHTTPServer(('127.0.0.1',0),usagesrv.Handler)
                thread=threading.Thread(target=server.serve_forever,daemon=True)
                thread.start()
                try:
                    base='http://127.0.0.1:'+str(server.server_port)
                    for route in ['/api/console/quota-snapshot?refresh=1','/api/console/usage-tokens','/api/console/usage-rates']:
                        with urllib.request.urlopen(base+route,timeout=3) as response:
                            self.assertEqual(response.status,200)
                            self.assertIsInstance(json.load(response),dict)
                finally:
                    server.shutdown();server.server_close();thread.join(timeout=3)

    def test_token_and_rate_read_real_temp_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/'ledger.jsonl'
            file.write_text(json.dumps({'time':'2026-10-07T12:00:00+08:00','model':'stub','input_tokens':120,'output_tokens':30,'duration_ms':2000,'status':'ok','usage_source':'upstream'})+'\n',encoding='utf-8')
            result=console_stats.usage_tokens(ledgers={'chat2api':str(file)},days=1,now=datetime.fromisoformat('2026-10-07T13:00:00+08:00'))
            self.assertTrue(result['days'])
            self.assertEqual(result['days'][0]['input_tokens']+result['days'][0]['output_tokens'],150)
            rate=console_stats.usage_rates(ledgers={'chat2api':str(file)},hours=2,now=datetime.fromisoformat('2026-10-07T13:00:00+08:00'))
            self.assertTrue(rate['hours'])

if __name__=='__main__': unittest.main()
