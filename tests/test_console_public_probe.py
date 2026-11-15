"""Public sharing probe contracts over isolated HTTP endpoints; no live traffic."""
from collections import deque
from contextlib import contextmanager, redirect_stdout
import http.client
import http.server
import io
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from console import public_probe as probe


PUBLIC_URL = 'https://fixture.tail-test.ts.net'
PUBLIC_HOST = 'fixture.tail-test.ts.net'
PUBLIC_IP = '203.0.113.10'
SECRET = 'fixture-secret-never-serialize'
CATALOG = {'object': 'list', 'data': [{'id': 'fixture-model'}]}
ANSWER = {'choices': [{'message': {'role': 'assistant', 'content': 'OK'}}]}


def reply(payload, status=200, **options):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode('utf-8')
    return dict(status=status, raw=raw, **options)


def dns_source(source, state, *, addresses=(), ttl=None, negative_ttl=None,
               status=None, classification=None):
    return {
        'source': source, 'state': state, 'status': status,
        'classification': classification or state,
        'addresses': list(addresses), 'addresses_truncated': False,
        'ttl_s': ttl, 'negative_ttl_s': negative_ttl,
        'checked_at_epoch': 1.0, 'elapsed_ms': 1,
    }


def dns_resolution(addresses, reason, state='ok', *, sources=()):
    detail = {
        'version': 1, 'checked_at_utc': '2026-10-26T00:00:00+00:00',
        'checked_at_epoch': 1.0, 'state': state, 'reason': reason,
        'addresses': list(addresses), 'addresses_truncated': False,
        'ttl_s': None, 'negative_ttl_s': None,
        'sources': list(sources),
    }
    return probe.PublicDnsResolution(list(addresses), reason, detail)


def reset_doh_workers():
    with probe._DOH_WORKERS_LOCK:
        probe._DOH_WORKERS.clear()


def doh_worker_stats():
    with probe._DOH_WORKERS_LOCK:
        return {'live': sum(1 for pending in probe._DOH_WORKERS.values()
                            if pending['thread'].is_alive()),
                'total': len(probe._DOH_WORKERS)}


@contextmanager
def isolated_http(responses):
    """Replace only the HTTPS transport endpoint, preserving real HTTP framing."""
    pending = deque(responses)
    seen = []
    connections = []
    unexpected_errors = []
    real_connection = http.client.HTTPConnection
    real_create_connection = socket.create_connection

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def handle_request(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            seen.append({'method': self.command, 'path': self.path,
                         'headers': list(self.headers.items()), 'body': body})
            spec = pending.popleft() if pending else reply({}, status=500)
            callback = spec.get('before_headers')
            if callback:
                callback()
            self.send_response(spec['status'])
            self.send_header('Content-Length', str(spec.get('length', len(spec['raw']))))
            for key, value in spec.get('headers', {}).items():
                self.send_header(key, value)
            if spec.get('truncate'):
                self.send_header('Connection', 'close')
                self.close_connection = True
            self.end_headers()
            try:
                self.wfile.write(spec['raw'])
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # Oversize and deadline rejection can close before the writer.
                pass

        do_GET = handle_request
        do_POST = handle_request

        def log_message(self, *args):
            pass

    class Server(http.server.ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            error = sys.exc_info()[1]
            if not isinstance(error, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
                unexpected_errors.append(error)

    server = Server(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={'poll_interval': .01}, daemon=True)

    class Connection(real_connection):
        default_port = 443

        def __init__(self, host, public_ip, timeout):
            self.target = (host, public_ip)
            self.initial_timeout = timeout
            self.close_calls = 0
            super().__init__(host, 443, timeout=timeout)
            self._create_connection = lambda address, budget, source_address=None: \
                real_create_connection(('127.0.0.1', server.server_port),
                                       budget, source_address)
            connections.append(self)

        def close(self):
            self.close_calls += 1
            return super().close()

    thread.start()
    try:
        yield seen, connections, Connection
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if unexpected_errors:
            raise AssertionError('unexpected isolated server error: ' + repr(unexpected_errors))


class PublicHostTests(unittest.TestCase):
    def test_fixed_https_origins_are_accepted(self):
        for url in (PUBLIC_URL, PUBLIC_URL + '/', PUBLIC_URL + ':443'):
            with self.subTest(url=url):
                self.assertEqual(probe.public_host(url), PUBLIC_HOST)

    def test_non_public_or_non_origin_urls_are_rejected(self):
        rejected = (
            None, {}, 443, '', 'http://' + PUBLIC_HOST,
            'https://127.0.0.1', 'https://localhost', 'https://[::1]',
            'https://example.com', 'https://ts.net',
            PUBLIC_URL + '.evil.invalid', 'https://x.ts.net.evil.invalid',
            'https://user@' + PUBLIC_HOST, 'https://user:pass@' + PUBLIC_HOST,
            PUBLIC_URL + '/v1', PUBLIC_URL + '/v1/models',
            PUBLIC_URL + '?a=b', PUBLIC_URL + '?', PUBLIC_URL + '#x',
            PUBLIC_URL + '#', PUBLIC_URL + ':', PUBLIC_URL + ':80',
            PUBLIC_URL + ':444', PUBLIC_URL + ':abc', PUBLIC_URL + ':-1',
            PUBLIC_URL + ':65536', PUBLIC_URL + '\n', '\r' + PUBLIC_URL,
            'https://fixture\n.tail-test.ts.net', PUBLIC_URL + '\t',
            'https://fixture\\.tail-test.ts.net', 'https://-x.ts.net',
            'https://x-.ts.net', 'https://x..ts.net', 'https://x_.ts.net',
            'https://%66ixture.ts.net', 'https://测试.ts.net',
            'https://' + 'x' * 64 + '.ts.net', 'https://' + 'x' * 254 + '.ts.net',
        )
        for url in rejected:
            with self.subTest(url=url):
                self.assertIsNone(probe.public_host(url))


class ConfiguredPublicOriginTests(unittest.TestCase):
    """D18-1: CONSOLE_PUBLIC_ORIGIN is the only accepted probe host."""

    CONFIGURED = 'https://dacintapi.de5.net'

    def test_configured_origin_is_the_only_accepted_host(self):
        with mock.patch.dict(os.environ,
                             {'CONSOLE_PUBLIC_ORIGIN': self.CONFIGURED}):
            for url in (self.CONFIGURED, self.CONFIGURED + '/',
                        self.CONFIGURED + ':443'):
                with self.subTest(url=url):
                    self.assertEqual(probe.public_host(url), 'dacintapi.de5.net')
            for url in (PUBLIC_URL, 'https://other.de5.net',
                        'https://dacintapi.de5.net.evil.invalid'):
                with self.subTest(url=url):
                    self.assertIsNone(probe.public_host(url))

    def test_invalid_configuration_accepts_no_host(self):
        with mock.patch.dict(os.environ, {
                'CONSOLE_PUBLIC_ORIGIN': 'http://dacintapi.de5.net'}):
            self.assertIsNone(probe.public_host(self.CONFIGURED))
            self.assertIsNone(probe.public_host(PUBLIC_URL))

    def test_check_reports_the_configured_source_and_host(self):
        connection = mock.MagicMock()
        with mock.patch.dict(os.environ,
                             {'CONSOLE_PUBLIC_ORIGIN': self.CONFIGURED}), \
                mock.patch.object(probe, 'resolve_public_addresses',
                                  return_value=dns_resolution(
                                      [PUBLIC_IP], 'ok')), \
                mock.patch.object(probe, '_public_connection',
                                  return_value=connection):
            result = probe.check_public_connection(self.CONFIGURED)
        self.assertEqual(result['overall'], 'ok')
        self.assertEqual(result['source'], 'configured')
        self.assertEqual(result['public_url'], self.CONFIGURED)
        self.assertEqual(result['host'], 'dacintapi.de5.net')


class PublicProbeHTTPTests(unittest.TestCase):
    def run_probe(self, responses, *, clock=None):
        with isolated_http(responses) as (seen, connections, factory):
            with mock.patch.object(probe, '_model_addresses', return_value=[PUBLIC_IP]), \
                    mock.patch.object(probe, '_public_connection', factory):
                if clock is None:
                    result = probe.probe_public_model(SECRET, PUBLIC_URL)
                else:
                    with mock.patch.object(probe.time, 'monotonic', side_effect=clock):
                        result = probe.probe_public_model(SECRET, PUBLIC_URL)
            for connection in connections:
                self.assertGreaterEqual(connection.close_calls, 1)
                self.assertIsNone(connection.sock)
                self.assertGreater(connection.initial_timeout, 0)
                self.assertLessEqual(connection.initial_timeout, 60)
                self.assertEqual(connection.target, (PUBLIC_HOST, PUBLIC_IP))
        self.assertNotIn(SECRET, json.dumps(result))
        return result, seen, connections

    def test_catalog_then_bounded_inference_and_credential_redaction(self):
        result, seen, connections = self.run_probe([reply(CATALOG), reply(ANSWER)])
        self.assertTrue(result['connected'])
        self.assertEqual(result['reason'], 'ok')
        self.assertEqual(result['stage'], 'inference')
        self.assertEqual(result['model'], 'fixture-model')
        self.assertEqual(result['http_status'], 200)
        self.assertEqual(result['public_ip'], PUBLIC_IP)
        self.assertEqual(result['dns_source'], 'cloudflare+nextdns_doh')
        self.assertEqual([(r['method'], r['path']) for r in seen],
                         [('GET', '/v1/models'), ('POST', '/v1/chat/completions')])
        for request in seen:
            headers = dict(request['headers'])
            self.assertEqual(headers['Authorization'], 'Bearer ' + SECRET)
            self.assertEqual(headers['Accept'], 'application/json')
            self.assertEqual(headers['Host'], PUBLIC_HOST)
            self.assertEqual([name for name, value in request['headers'] if SECRET in value],
                             ['Authorization'])
            self.assertNotIn(SECRET.encode(), request['body'])
        self.assertEqual(seen[0]['body'], b'')
        self.assertEqual(dict(seen[1]['headers'])['Content-Type'], 'application/json')
        payload = json.loads(seen[1]['body'])
        self.assertEqual(payload['model'], 'fixture-model')
        self.assertEqual(payload['max_tokens'], 32)
        self.assertIs(payload['stream'], False)
        self.assertEqual(payload['messages'], [{'role': 'user', 'content': 'Reply with OK.'}])
        self.assertEqual(len(connections), 2)

    def test_preferred_catalog_model_is_used_when_present(self):
        catalog = {'object': 'list', 'data': [
            {'id': 'fixture-model'},
            {'id': 'DeepSeek-V4.1-Flash-1017'},
        ]}
        result, seen, _ = self.run_probe([reply(catalog), reply(ANSWER)])
        self.assertTrue(result['connected'])
        self.assertEqual(result['model'], 'DeepSeek-V4.1-Flash-1017')
        payload = json.loads(seen[1]['body'])
        self.assertEqual(payload['model'], 'DeepSeek-V4.1-Flash-1017')
        self.assertEqual(len([r for r in seen if r['method'] == 'POST']), 1)

    def test_empty_or_invalid_catalog_never_starts_inference(self):
        cases = (
            ({'data': []}, 'no_models'),
            ({'data': CATALOG['data'], 'error': {'message': SECRET}}, 'invalid_response'),
            ({'error': {'message': SECRET}}, 'invalid_response'),
            ({}, 'invalid_response'), ([], 'invalid_response'),
            ({'data': {}}, 'invalid_response'),
            ({'data': [{'id': ''}]}, 'invalid_response'),
            ({'data': [{'id': None}]}, 'invalid_response'),
            ({'data': [{'id': 'x\ny'}]}, 'invalid_response'),
            ({'data': [{'id': 'x' * 257}]}, 'invalid_response'),
            ({'data': [{'id': SECRET}]}, 'invalid_response'),
            ({'data': [{'id': 'fixture-model'}, None]}, 'invalid_response'),
            (b'<html>private response</html>', 'invalid_response'),
            (b'\xff\xfe', 'invalid_response'),
        )
        for payload, reason in cases:
            with self.subTest(payload=payload):
                result, seen, _ = self.run_probe([reply(payload)])
                self.assertFalse(result['connected'])
                self.assertEqual(result['reason'], reason)
                self.assertEqual(result['stage'], 'models')
                self.assertEqual(len(seen), 1)

    def test_only_nonempty_assistant_reply_proves_inference(self):
        cases = (
            {}, [], {'choices': []}, {'choices': {}}, {'choices': [None]},
            {'choices': [{'message': None}]},
            {'choices': [{'message': {'role': 'assistant', 'content': ''}}]},
            {'choices': [{'message': {'role': 'assistant', 'content': ' \r\n\t '}}]},
            {'choices': [{'message': {'role': 'assistant', 'content': None}}]},
            {'choices': [{'message': {'role': 'assistant', 'content': ['OK']}}]},
            {'choices': [{'message': {'role': 'user', 'content': 'OK'}}]},
            {'choices': ANSWER['choices'], 'error': {'message': SECRET}},
            {'error': {'message': SECRET}},
            b'private response, not JSON',
        )
        for payload in cases:
            with self.subTest(payload=payload):
                result, seen, _ = self.run_probe([reply(CATALOG), reply(payload)])
                self.assertFalse(result['connected'])
                self.assertEqual(result['reason'], 'invalid_response')
                self.assertEqual(result['stage'], 'inference')
                self.assertEqual(len(seen), 2)

    def test_http_failures_are_classified_at_the_failing_stage(self):
        statuses = ((401, 'key_rejected'), (403, 'key_rejected'),
                    (429, 'rate_limited'), (301, 'redirect_refused'),
                    (302, 'redirect_refused'), (307, 'redirect_refused'),
                    (308, 'redirect_refused'), (502, 'upstream_unavailable'),
                    (503, 'upstream_unavailable'), (404, 'http_error'))
        for stage in ('models', 'inference'):
            for status, reason in statuses:
                with self.subTest(stage=stage, status=status):
                    failure = reply({'error': {'message': SECRET}}, status=status,
                                    headers={'Location': 'https://forbidden.invalid/leak'})
                    responses = [failure] if stage == 'models' else [reply(CATALOG), failure]
                    result, seen, _ = self.run_probe(responses)
                    self.assertFalse(result['connected'])
                    self.assertEqual(result['reason'], reason)
                    self.assertEqual(result['stage'], stage)
                    self.assertEqual(result['http_status'], status)
                    self.assertEqual(len(seen), len(responses))

    def test_oversized_and_truncated_responses_fail_and_close_connections(self):
        bad_responses = (
            (reply(b'x' * (1024 * 1024 + 1)), 'invalid_response'),
            (reply(b'{"data":[]}', length=512, truncate=True), 'connection_failed'),
        )
        for stage in ('models', 'inference'):
            for failure, reason in bad_responses:
                with self.subTest(stage=stage, reason=reason):
                    responses = [failure] if stage == 'models' else [reply(CATALOG), failure]
                    result, seen, _ = self.run_probe(responses)
                    self.assertFalse(result['connected'])
                    self.assertEqual(result['reason'], reason)
                    self.assertEqual(result['stage'], stage)
                    self.assertEqual(len(seen), len(responses))

    def test_catalog_and_inference_share_one_total_deadline(self):
        now = [0.0]

        def advance(value):
            return lambda: now.__setitem__(0, value)

        result, seen, connections = self.run_probe(
            [reply(CATALOG, before_headers=advance(40.0)),
             reply(ANSWER, before_headers=advance(61.0))], clock=lambda: now[0])
        self.assertFalse(result['connected'])
        self.assertEqual(result['reason'], 'timeout')
        self.assertEqual(result['stage'], 'inference')
        self.assertEqual(result['elapsed_ms'], 61000)
        self.assertEqual(len(seen), 2)
        self.assertEqual(connections[0].initial_timeout, 60)
        self.assertEqual(connections[1].initial_timeout, 20)


class PublicProbeBoundaryTests(unittest.TestCase):
    def test_invalid_origin_never_resolves_or_opens_a_connection(self):
        with mock.patch.object(probe, '_model_addresses') as resolver, \
                mock.patch.object(probe, '_public_connection') as factory:
            result = probe.probe_public_model(SECRET, 'http://127.0.0.1:8100')
        self.assertFalse(result['connected'])
        self.assertEqual(result['stage'], 'configuration')
        self.assertEqual(result['reason'], 'public_url_unavailable')
        resolver.assert_not_called()
        factory.assert_not_called()

    def test_manual_probe_keeps_both_timeout_sources(self):
        detail = probe._aggregate_dns(
            [probe._dns_timeout_source('cloudflare'),
             probe._dns_timeout_source('google')],
            probe.time.monotonic())
        with mock.patch.object(probe, '_model_addresses',
                               side_effect=probe.PublicDnsTimeout(detail)):
            result = probe.probe_public_model(SECRET, PUBLIC_URL)
        self.assertFalse(result['connected'])
        self.assertEqual(result['stage'], 'dns')
        self.assertEqual(result['reason'], 'dns_timeout')
        self.assertEqual([source['state'] for source in result['dns']['sources']],
                         ['timeout', 'timeout'])

    def test_transport_errors_remain_distinct_and_close_the_connection(self):
        errors = (
            (ssl.SSLCertVerificationError(1, SECRET), 'tls_certificate_invalid'),
            (ssl.SSLEOFError(8, SECRET), 'tls_handshake_failed'),
            (socket.gaierror(-2, SECRET), 'dns_failed'),
            (TimeoutError(SECRET), 'timeout'),
            (ConnectionResetError(SECRET), 'connection_failed'),
            (http.client.BadStatusLine(SECRET), 'connection_failed'),
        )
        for error, reason in errors:
            with self.subTest(reason=reason, error=type(error).__name__):
                connection = mock.MagicMock()
                connection.connect.side_effect = error
                with mock.patch.object(probe, '_model_addresses', return_value=[PUBLIC_IP]), \
                        mock.patch.object(probe, '_public_connection',
                                          return_value=connection):
                    result = probe.probe_public_model(SECRET, PUBLIC_URL)
                self.assertFalse(result['connected'])
                self.assertEqual(result['reason'], reason)
                self.assertNotIn(SECRET, json.dumps(result))
                connection.close.assert_called_once()

    def test_slow_body_cannot_extend_the_deadline(self):
        now = [0.0]
        connection = mock.MagicMock()
        response = connection.getresponse.return_value
        response.status = 200
        response.length = None

        def first_chunk(*args):
            now[0] = 61.0
            return b'{'

        response.read1.side_effect = first_chunk
        with mock.patch.object(probe, '_model_addresses', return_value=[PUBLIC_IP]), \
                mock.patch.object(probe, '_public_connection', return_value=connection), \
                mock.patch.object(probe.time, 'monotonic', side_effect=lambda: now[0]):
            result = probe.probe_public_model(SECRET, PUBLIC_URL)
        self.assertFalse(result['connected'])
        self.assertEqual(result['reason'], 'timeout')
        self.assertEqual(result['stage'], 'models')
        response.read1.assert_called_once()
        connection.close.assert_called_once()


class PublicDNSAndTransportTests(unittest.TestCase):
    def test_resolver_uses_https_dns_without_the_sharing_credential(self):
        payload = {'Status': 0, 'Answer': [{'type': 1, 'data': '1.1.1.1'}]}
        sources = (('cloudflare', 'cloudflare-dns.com', '/dns-query'),)
        with isolated_http([reply(payload)]) as (seen, connections, factory):
            def dns_connection(host, port, *, timeout, context):
                self.assertEqual((host, port), ('cloudflare-dns.com', 443))
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(context.check_hostname)
                return factory(host, 'fixture-resolver', timeout)

            with mock.patch.object(probe, 'DOH_SOURCES', sources), \
                    mock.patch.object(probe.http.client, 'HTTPSConnection', side_effect=dns_connection):
                addresses = probe._public_addresses(PUBLIC_HOST, probe.time.monotonic() + 5)
        self.assertEqual(addresses, ['1.1.1.1'])
        self.assertEqual(seen[0]['path'], '/dns-query?name=' + PUBLIC_HOST + '&type=A')
        self.assertEqual(dict(seen[0]['headers'])['Accept'], 'application/dns-json')
        self.assertNotIn('Authorization', dict(seen[0]['headers']))
        self.assertTrue(all(connection.close_calls for connection in connections))

    def test_two_sources_are_queried_in_parallel_and_union_their_addresses(self):
        payloads = [reply({'Status': 0, 'Answer': [{'type': 1, 'data': '1.1.1.1'}]}),
                    reply({'Status': 0, 'Answer': [{'type': 1, 'data': '1.0.0.1'}]})]
        with isolated_http(payloads) as (seen, connections, factory):
            with mock.patch.object(probe.http.client, 'HTTPSConnection',
                                   side_effect=lambda host, port, **opts:
                                   factory(host, '', opts['timeout'])):
                resolution = probe.resolve_public_addresses(
                    PUBLIC_HOST, probe.time.monotonic() + 5)
        self.assertEqual(set(resolution.addresses), {'1.1.1.1', '1.0.0.1'})
        self.assertEqual(resolution.reason, 'ok')
        self.assertEqual(resolution.detail['state'], 'ok')
        self.assertEqual({item['path'].split('?')[0] for item in seen},
                         {'/dns-query'})
        self.assertEqual({connection.target[0] for connection in connections},
                         {'cloudflare-dns.com', 'dns.nextdns.io'})
        self.assertTrue(all('Authorization' not in dict(item['headers']) for item in seen))
        self.assertTrue(all(connection.close_calls for connection in connections))

    def test_dns_disagreement_and_unconfirmed_are_not_failures(self):
        cases = (
            ('disagreement', dns_source('cloudflare', 'nxdomain', negative_ttl=300),
             dns_source('google', 'ok', addresses=['1.1.1.1'], ttl=60)),
            ('unconfirmed', dns_source('cloudflare', 'timeout'),
             dns_source('google', 'ok', addresses=['1.1.1.1'], ttl=60)),
            ('ok', dns_source('cloudflare', 'ok', addresses=['1.1.1.1'], ttl=60),
             dns_source('google', 'ok', addresses=['1.0.0.1'], ttl=30)),
        )
        for expected, first, second in cases:
            with self.subTest(expected=expected):
                with mock.patch.object(probe, '_doh_source',
                                       side_effect=(first, second)):
                    resolution = probe.resolve_public_addresses(
                        PUBLIC_HOST, probe.time.monotonic() + 5)
                self.assertEqual(resolution.detail['state'], expected)
                self.assertEqual(resolution.addresses,
                                 ['1.1.1.1'] if expected != 'ok'
                                 else ['1.1.1.1', '1.0.0.1'])

    def test_negative_answers_stay_distinct_and_ttl_unknown_is_null(self):
        cases = (
            (dns_source('cloudflare', 'nxdomain', negative_ttl=300),
             dns_source('google', 'nxdomain', negative_ttl=200),
             'nxdomain', 'dns_nxdomain', 200),
            (dns_source('cloudflare', 'empty', negative_ttl=120),
             dns_source('google', 'empty'), 'empty', 'dns_empty', 120),
        )
        for first, second, expected_state, expected_reason, negative_ttl in cases:
            with self.subTest(expected=expected_reason):
                with mock.patch.object(probe, '_doh_source',
                                       side_effect=(first, second)):
                    resolution = probe.resolve_public_addresses(
                        PUBLIC_HOST, probe.time.monotonic() + 5)
                self.assertEqual(resolution.addresses, [])
                self.assertEqual(resolution.reason, expected_reason)
                self.assertEqual(resolution.detail['state'], expected_state)
                self.assertEqual(resolution.detail['negative_ttl_s'], negative_ttl)
                self.assertIsNone(resolution.detail['ttl_s'])

    def test_both_sources_timeout_raise_and_a_single_timeout_is_unconfirmed(self):
        with mock.patch.object(probe, '_doh_source',
                               side_effect=(dns_source('cloudflare', 'timeout'),
                                            dns_source('google', 'timeout'))):
            with self.assertRaises(TimeoutError):
                probe.resolve_public_addresses(PUBLIC_HOST, probe.time.monotonic() + 5)
        with mock.patch.object(probe, '_doh_source',
                               side_effect=(dns_source('cloudflare', 'timeout'),
                                            dns_source('google', 'empty'))):
            resolution = probe.resolve_public_addresses(
                PUBLIC_HOST, probe.time.monotonic() + 5)
        self.assertEqual(resolution.reason, 'dns_unconfirmed')
        self.assertEqual(resolution.detail['state'], 'unconfirmed')

    def test_resolver_never_accepts_internal_fake_or_invalid_addresses(self):
        payloads = [reply({'Status': 0, 'Answer': [{'type': 1, 'data': address}]})
                    for address in ('127.0.0.1', '100.119.187.73', '198.18.2.161',
                                    '192.168.1.1', '203.0.113.10', '::1', 'invalid')]
        payloads += [reply({'Status': 3}), reply({'Status': 0, 'Answer': []}),
                     reply({'Status': 0, 'Answer': None}), reply(b'<html>invalid</html>'),
                     reply({}, status=302, headers={'Location': 'https://forbidden.invalid'})]
        sources = (('cloudflare', 'cloudflare-dns.com', '/dns-query'),)
        for response in payloads:
            with self.subTest(response=response), isolated_http([response]) as (seen, connections, factory):
                with mock.patch.object(probe, 'DOH_SOURCES', sources), \
                        mock.patch.object(probe.http.client, 'HTTPSConnection',
                                       side_effect=lambda host, port, **opts: factory(host, '', opts['timeout'])):
                    with self.assertRaises(socket.gaierror):
                        probe._public_addresses(PUBLIC_HOST, probe.time.monotonic() + 5)
                self.assertEqual(len(seen), 1)
                self.assertTrue(all(connection.close_calls for connection in connections))

    def test_public_ip_is_bound_to_socket_while_sni_host_and_verification_are_preserved(self):
        real_factory = probe.http.client.HTTPSConnection
        with mock.patch.object(probe.ssl, 'create_default_context', wraps=ssl.create_default_context) as context:
            connection = probe._public_connection(PUBLIC_HOST, '1.1.1.1', 5)
        self.assertIsInstance(connection, real_factory)
        context.assert_called_once()
        self.assertEqual(connection.host, PUBLIC_HOST)
        self.assertEqual(connection.port, 443)
        self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(connection._context.check_hostname)
        sock = mock.MagicMock()
        tls_sock = mock.MagicMock()
        connection._context = mock.MagicMock()
        connection._context.wrap_socket.return_value = tls_sock
        with mock.patch.object(probe.socket, 'create_connection', return_value=sock) as connect:
            connection.connect()
        connect.assert_called_once_with(('1.1.1.1', 443), 5, None)
        connection._context.wrap_socket.assert_called_once_with(sock, server_hostname=PUBLIC_HOST)
        connection.close()
        tls_sock.close.assert_called_once()


class PublicResolverReasonTests(unittest.TestCase):
    def test_resolver_reasons_are_distinct_and_connections_always_close(self):
        sources = (('cloudflare', 'cloudflare-dns.com', '/dns-query'),)
        cases = (
            (reply({'Status': 0, 'Answer': [{'type': 1, 'data': '1.1.1.1', 'TTL': 60},
                                           {'type': 1, 'data': '1.0.0.1', 'TTL': 30},
                                           {'type': 1, 'data': '1.1.1.1', 'TTL': 10}]}),
             (['1.1.1.1', '1.0.0.1'], 'ok')),
            (reply({'Status': 0, 'Answer': [{'type': 1, 'data': '127.0.0.1'},
                                           {'type': 1, 'data': '100.119.187.73'},
                                           {'type': 1, 'data': '198.18.2.161'},
                                           {'type': 1, 'data': '::1'}]}),
             ([], 'dns_empty')),
            (reply({'Status': 0}), ([], 'dns_empty')),
            (reply({'Status': 0, 'Answer': []}), ([], 'dns_empty')),
            (reply({'Status': 3}), ([], 'dns_nxdomain')),
            (reply({}, status=503), ([], 'dns_failed')),
            (reply(b'<html>not dns json</html>'), ([], 'dns_failed')),
        )
        for response, expected in cases:
            with self.subTest(response=response):
                with isolated_http([response]) as (seen, connections, factory):
                    with mock.patch.object(probe, 'DOH_SOURCES', sources), \
                            mock.patch.object(
                            probe.http.client, 'HTTPSConnection',
                            side_effect=lambda host, port, **opts:
                            factory(host, 'fixture-resolver', opts['timeout'])):
                        addresses, reason = probe.resolve_public_addresses(
                            PUBLIC_HOST, probe.time.monotonic() + 5)
                self.assertEqual((addresses, reason), expected)
                self.assertEqual(len(seen), 1)
                self.assertEqual(dict(seen[0]['headers'])['Accept'], 'application/dns-json')
                self.assertNotIn('Authorization', dict(seen[0]['headers']))
                self.assertTrue(all(connection.close_calls for connection in connections))

    def test_resolver_timeout_is_not_reported_as_a_dns_failure(self):
        connection = mock.MagicMock()
        connection.getresponse.side_effect = TimeoutError(SECRET)
        sources = (('cloudflare', 'cloudflare-dns.com', '/dns-query'),)
        with mock.patch.object(probe, 'DOH_SOURCES', sources), \
                mock.patch.object(probe.http.client, 'HTTPSConnection',
                               return_value=connection):
            with self.assertRaises(TimeoutError):
                probe.resolve_public_addresses(PUBLIC_HOST, probe.time.monotonic() + 5)
        connection.close.assert_called_once()


class PublicDnsParsingTests(unittest.TestCase):
    def resolve(self, response):
        sources = (('cloudflare', 'cloudflare-dns.com', '/dns-query'),)
        with isolated_http([response]) as (seen, connections, factory):
            with mock.patch.object(probe, 'DOH_SOURCES', sources), \
                    mock.patch.object(
                    probe.http.client, 'HTTPSConnection',
                    side_effect=lambda host, port, **opts:
                    factory(host, '', opts['timeout'])):
                result = probe._doh_source(
                    'cloudflare', 'cloudflare-dns.com', '/dns-query',
                    PUBLIC_HOST, probe.time.monotonic() + 5)
        self.assertEqual(len(seen), 1)
        self.assertTrue(all(connection.close_calls for connection in connections))
        return result

    def test_positive_ttl_is_the_smallest_answer_ttl(self):
        result = self.resolve(reply({'Status': 0, 'Answer': [
            {'type': 1, 'data': '8.8.8.8', 'TTL': 120},
            {'type': 1, 'data': '1.1.1.1', 'TTL': 30},
        ]}))
        self.assertEqual(result['state'], 'ok')
        self.assertEqual(result['ttl_s'], 30)
        self.assertIsNone(result['negative_ttl_s'])

    def test_negative_ttl_comes_from_soa_and_is_unknown_when_missing(self):
        soa = {'type': 6, 'TTL': 300,
               'data': 'ns.example. hostmaster.example. 1 60 30 600 120'}
        result = self.resolve(reply({'Status': 3, 'Authority': [soa]}))
        self.assertEqual((result['state'], result['classification']),
                         ('nxdomain', 'nxdomain'))
        self.assertEqual(result['negative_ttl_s'], 120)
        self.assertIsNone(result['ttl_s'])
        for authority in ([], [{'type': 6, 'TTL': 300,
                                'data': 'ns.example. hostmaster.example. 1 60 30'}],
                          [{'type': 6, 'TTL': 300,
                            'data': 'ns.example. hostmaster.example. 1 60 30 600 -1'}]):
            with self.subTest(authority=authority):
                result = self.resolve(reply({'Status': 3, 'Authority': authority}))
                self.assertIsNone(result['negative_ttl_s'])

    def test_no_answer_keeps_its_own_state_and_negative_ttl(self):
        soa = {'type': 6, 'TTL': 300,
               'data': 'ns.example. hostmaster.example. 1 60 30 600 120'}
        result = self.resolve(reply({'Status': 0, 'Authority': [soa]}))
        self.assertEqual((result['state'], result['classification']),
                         ('empty', 'no_answer'))
        self.assertEqual(result['negative_ttl_s'], 120)
        self.assertEqual(result['addresses'], [])

    def test_addresses_are_bounded_and_invalid_ttls_stay_unknown(self):
        answers = [{'type': 1, 'data': address, 'TTL': 60} for address in (
            '8.8.8.8', '1.1.1.1', '9.9.9.9', '149.112.112.112',
            '208.67.222.222', '8.8.4.4')]
        result = self.resolve(reply({'Status': 0, 'Answer': answers}))
        self.assertEqual(result['addresses'],
                         ['8.8.8.8', '1.1.1.1', '9.9.9.9', '149.112.112.112'])
        self.assertTrue(result['addresses_truncated'])
        for ttl in (True, -1, 2147483648, '60'):
            with self.subTest(ttl=ttl):
                result = self.resolve(reply({'Status': 0, 'Answer': [
                    {'type': 1, 'data': '8.8.8.8', 'TTL': ttl}]}))
                self.assertIsNone(result['ttl_s'])

    def test_http_and_json_failures_stay_distinct(self):
        cases = (
            (reply({}, status=503), 'http_error', 503),
            (reply(b'<html>not dns json</html>'), 'invalid_json', None),
            (reply({'Status': '0'}), 'invalid_json', None),
            (reply({'Status': 2}), 'servfail', 2),
        )
        for response, classification, status in cases:
            with self.subTest(classification=classification):
                result = self.resolve(response)
                self.assertEqual(result['state'], 'error')
                self.assertEqual(result['classification'], classification)
                self.assertEqual(result['status'], status)
                self.assertEqual(result['addresses'], [])


class PublicConnectionCheckTests(unittest.TestCase):
    def test_each_entry_is_checked_without_any_http_request_and_closes(self):
        second_ip = '203.0.113.11'
        with isolated_http([]) as (seen, connections, factory):
            with mock.patch.object(probe, 'resolve_public_addresses',
                                   return_value=([PUBLIC_IP, second_ip], 'ok')), \
                    mock.patch.object(probe, '_public_connection', factory):
                result = probe.check_public_connection(PUBLIC_URL)
        self.assertEqual(result['version'], 1)
        self.assertEqual(result['overall'], 'ok')
        self.assertEqual(result['reason'], 'ok')
        self.assertEqual(result['public_url'], PUBLIC_URL)
        self.assertEqual(result['host'], PUBLIC_HOST)
        self.assertEqual(result['source'], 'tailscale')
        self.assertEqual(result['deadline_s'], probe.PUBLIC_CHECK_TIMEOUT_S)
        self.assertEqual(len(result['entries']), 2)
        for entry, address in zip(result['entries'], (PUBLIC_IP, second_ip)):
            self.assertEqual(entry['host'], PUBLIC_HOST)
            self.assertEqual(entry['ip'], address)
            self.assertEqual(entry['state'], 'ok')
            self.assertEqual((entry['dns'], entry['tls'], entry['certificate']),
                             ('ok', 'ok', 'ok'))
            self.assertEqual(entry['reason'], 'ok')
            self.assertIsInstance(entry['elapsed_ms'], int)
            self.assertGreaterEqual(entry['elapsed_ms'], 0)
            self.assertIsInstance(entry['checked_at_epoch'], float)
        self.assertEqual([connection.target for connection in connections],
                         [(PUBLIC_HOST, PUBLIC_IP), (PUBLIC_HOST, second_ip)])
        for connection in connections:
            self.assertGreaterEqual(connection.close_calls, 1)
            self.assertIsNone(connection.sock)
            self.assertGreater(connection.initial_timeout, 0)
            self.assertLessEqual(connection.initial_timeout,
                                 probe.PUBLIC_CHECK_ADDRESS_TIMEOUT_S)
        # No request line ever reached the terminal double: no model POST and
        # no catalog GET either.
        self.assertEqual(seen, [])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertIsInstance(result['checked_at_epoch'], float)
        self.assertIsInstance(result['elapsed_ms'], int)

    def test_invalid_origin_never_resolves_or_connects(self):
        with mock.patch.object(probe, 'resolve_public_addresses') as resolver, \
                mock.patch.object(probe, '_public_connection') as factory:
            result = probe.check_public_connection('http://127.0.0.1:8100')
        self.assertEqual(result, {
            'version': 1, 'checked_at_utc': result['checked_at_utc'],
            'checked_at_epoch': 0.0, 'public_url': None, 'host': None,
            'source': None,
            'overall': 'not_checked', 'reason': 'public_url_unavailable',
            'deadline_s': probe.PUBLIC_CHECK_TIMEOUT_S, 'elapsed_ms': 0,
            'entries': [], 'dns': None})
        resolver.assert_not_called()
        factory.assert_not_called()

    def test_dns_reasons_are_not_folded_into_confirmed_failure(self):
        cases = (
            (([], 'dns_empty'), 'dns_empty'),
            (([], 'dns_failed'), 'dns_failed'),
        )
        for resolved, reason in cases:
            with self.subTest(reason=reason):
                with mock.patch.object(probe, 'resolve_public_addresses',
                                       return_value=resolved), \
                        mock.patch.object(probe, '_public_connection') as factory:
                    result = probe.check_public_connection(PUBLIC_URL)
                self.assertEqual(result['overall'], 'not_checked')
                self.assertEqual(result['reason'], reason)
                self.assertEqual(result['entries'], [])
                factory.assert_not_called()
        with mock.patch.object(probe, 'resolve_public_addresses',
                               side_effect=TimeoutError(SECRET)), \
                mock.patch.object(probe, '_public_connection') as factory:
            result = probe.check_public_connection(PUBLIC_URL)
        self.assertEqual(result['overall'], 'not_checked')
        self.assertEqual(result['reason'], 'dns_timeout')
        self.assertEqual(result['entries'], [])
        factory.assert_not_called()
        self.assertNotIn(SECRET, json.dumps(result))

    def test_dns_disagreement_with_working_tls_is_degraded(self):
        detail = dns_resolution(
            [PUBLIC_IP], 'dns_disagreement', 'disagreement',
            sources=[dns_source('cloudflare', 'nxdomain', negative_ttl=300),
                      dns_source('google', 'ok', addresses=[PUBLIC_IP], ttl=60)])
        connection = mock.MagicMock()
        with mock.patch.object(probe, 'resolve_public_addresses',
                               return_value=detail), \
                mock.patch.object(probe, '_public_connection',
                                  return_value=connection):
            result = probe.check_public_connection(PUBLIC_URL)
        self.assertEqual(result['overall'], 'degraded')
        self.assertEqual(result['reason'], 'dns_disagreement')
        self.assertEqual(result['dns']['state'], 'disagreement')
        self.assertEqual(result['entries'][0]['tls'], 'ok')
        self.assertNotIn(SECRET, json.dumps(result))

    def test_nxdomain_without_addresses_is_not_a_confirmed_tls_failure(self):
        detail = dns_resolution(
            [], 'dns_nxdomain', 'nxdomain',
            sources=[dns_source('cloudflare', 'nxdomain', negative_ttl=300),
                      dns_source('google', 'nxdomain', negative_ttl=300)])
        with mock.patch.object(probe, 'resolve_public_addresses',
                               return_value=detail), \
                mock.patch.object(probe, '_public_connection') as factory:
            result = probe.check_public_connection(PUBLIC_URL)
        self.assertEqual(result['overall'], 'not_checked')
        self.assertEqual(result['reason'], 'dns_nxdomain')
        self.assertEqual(result['dns']['state'], 'nxdomain')
        self.assertEqual(result['entries'], [])
        factory.assert_not_called()

    def test_truncated_addresses_never_report_all_failed(self):
        five = ['8.8.8.8', '1.1.1.1', '9.9.9.9',
                '149.112.112.112', '208.67.222.222']
        detail = dns_resolution(
            five, 'ok', 'ok',
            sources=[dns_source('cloudflare', 'ok', addresses=five, ttl=60),
                     dns_source('google', 'ok', addresses=five, ttl=60)])
        detail.detail['addresses_truncated'] = True
        connection = mock.MagicMock()
        connection.connect.side_effect = ssl.SSLError('fixture')
        with mock.patch.object(probe, 'resolve_public_addresses',
                               return_value=detail), \
                mock.patch.object(probe, '_public_connection',
                                  return_value=connection):
            result = probe.check_public_connection(PUBLIC_URL)
        self.assertEqual(result['overall'], 'incomplete')
        self.assertEqual(result['reason'], 'addresses_truncated')
        self.assertEqual(len(result['entries']), 4)
        self.assertTrue(result['dns']['addresses_truncated'])

    def test_slow_source_is_harvested_at_the_per_source_deadline(self):
        self.addCleanup(reset_doh_workers)
        def source(source, server, path, host, deadline):
            if source == 'cloudflare':
                time.sleep(0.5)
                return probe._dns_timeout_source(source)
            return dns_source('google', 'ok', addresses=['8.8.8.8'], ttl=60)

        connection = mock.MagicMock()
        with mock.patch.object(probe, '_doh_source', side_effect=source), \
                mock.patch.object(probe, 'DOH_TIMEOUT_SECONDS', 0.1), \
                mock.patch.object(probe, '_public_connection',
                                  return_value=connection):
            started = time.monotonic()
            result = probe.check_public_connection(PUBLIC_URL, deadline_s=0.5)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.3)
        self.assertEqual(result['dns']['sources'][0]['state'], 'timeout')
        self.assertEqual(result['dns']['sources'][1]['state'], 'ok')
        self.assertEqual(len(result['entries']), 1)
        self.assertEqual(result['entries'][0]['tls'], 'ok')
        self.assertEqual(result['overall'], 'degraded')
        self.assertEqual(result['reason'], 'dns_unconfirmed')

    def test_both_sources_timeout_keep_their_detail(self):
        with mock.patch.object(probe, '_doh_source',
                               side_effect=(
                                   dns_source('cloudflare', 'timeout'),
                                   dns_source('google', 'timeout'))):
            with self.assertRaises(probe.PublicDnsTimeout) as raised:
                probe.resolve_public_addresses(
                    PUBLIC_HOST, probe.time.monotonic() + 5)
        detail = raised.exception.detail
        self.assertEqual(detail['state'], 'timeout')
        self.assertEqual([source['state'] for source in detail['sources']],
                         ['timeout', 'timeout'])
        with mock.patch.object(probe, 'resolve_public_addresses',
                               side_effect=raised.exception):
            result = probe.check_public_connection(PUBLIC_URL)
        self.assertEqual(result['overall'], 'not_checked')
        self.assertEqual(result['reason'], 'dns_timeout')
        self.assertEqual([source['state'] for source in result['dns']['sources']],
                         ['timeout', 'timeout'])

    def test_manual_probe_also_harvests_the_working_source(self):
        self.addCleanup(reset_doh_workers)
        def source(source, server, path, host, deadline):
            if source == 'cloudflare':
                time.sleep(0.5)
                return probe._dns_timeout_source(source)
            return dns_source('google', 'ok', addresses=['8.8.8.8'], ttl=60)

        with isolated_http([reply(CATALOG), reply(ANSWER)]) as (seen, _, factory):
            with mock.patch.object(probe, '_doh_source', side_effect=source), \
                    mock.patch.object(probe, 'DOH_TIMEOUT_SECONDS', 0.1), \
                    mock.patch.object(probe, '_public_connection', factory):
                started = time.monotonic()
                result = probe.probe_public_model(SECRET, PUBLIC_URL)
                elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5)
        self.assertTrue(result['connected'])
        self.assertEqual(result['public_ip'], '8.8.8.8')
        self.assertEqual(result['dns']['sources'][0]['state'], 'timeout')
        self.assertEqual(result['dns']['sources'][1]['state'], 'ok')
        self.assertEqual([(row['method'], row['path']) for row in seen],
                         [('GET', '/v1/models'), ('POST', '/v1/chat/completions')])

    def test_repeated_checks_do_not_accumulate_stuck_workers(self):
        release = threading.Event()
        connections = []
        threads = []

        def source(source, server, path, host, deadline):
            if source == 'cloudflare':
                threads.append(threading.current_thread())
                connection = mock.MagicMock()
                connections.append(connection)
                release.wait(5)
                connection.close()
                return probe._dns_timeout_source(source)
            return dns_source('google', 'ok', addresses=['8.8.8.8'], ttl=60)

        self.addCleanup(release.set)
        self.addCleanup(reset_doh_workers)
        with mock.patch.object(probe, '_doh_source', side_effect=source), \
                mock.patch.object(probe, 'DOH_TIMEOUT_SECONDS', 0.1):
            for _ in range(5):
                resolution = probe.resolve_public_addresses(
                    PUBLIC_HOST, time.monotonic() + 0.5)
                self.assertEqual(resolution.detail['sources'][0]['state'],
                                 'timeout')
                self.assertEqual(resolution.detail['sources'][1]['state'], 'ok')
                with probe._DOH_WORKERS_LOCK:
                    cloudflare = [key for key in probe._DOH_WORKERS
                                  if key[0] == 'cloudflare']
                self.assertEqual(len(cloudflare), 1)
                self.assertEqual(len(connections), 1)
        release.set()
        for thread in threads:
            thread.join(1)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        reset_doh_workers()
        self.assertEqual(doh_worker_stats()['total'], 0)

    def test_global_cap_reports_not_checked_not_timeout(self):
        release = threading.Event()
        threads = []

        def source(source, server, path, host, deadline):
            threads.append(threading.current_thread())
            release.wait(5)
            return probe._dns_timeout_source(source)

        self.addCleanup(release.set)
        self.addCleanup(reset_doh_workers)
        with mock.patch.object(probe, '_doh_source', side_effect=source), \
                mock.patch.object(probe, 'DOH_TIMEOUT_SECONDS', 0.1), \
                mock.patch.object(probe, 'DOH_MAX_WORKERS', 2):
            with self.assertRaises(probe.PublicDnsTimeout):
                probe.resolve_public_dns(
                    'first.tail-test.ts.net', time.monotonic() + 0.5)
            resolution = probe.resolve_public_dns(
                'second.tail-test.ts.net', time.monotonic() + 0.5)
        self.assertEqual(resolution['state'], 'not_checked')
        self.assertEqual(resolution['reason'], 'dns_not_checked')
        self.assertEqual([source['state']
                          for source in resolution['sources']],
                         ['not_checked', 'not_checked'])
        release.set()
        for thread in threads:
            thread.join(1)
        reset_doh_workers()

    def test_late_result_is_not_returned_as_fresh_evidence(self):
        release = threading.Event()
        threads = []

        def source(source, server, path, host, deadline):
            if source == 'cloudflare':
                threads.append(threading.current_thread())
                release.wait(5)
                return dns_source('cloudflare', 'ok', addresses=['8.8.8.8'],
                                  ttl=60)
            return dns_source('google', 'ok', addresses=['1.1.1.1'], ttl=60)

        self.addCleanup(release.set)
        self.addCleanup(reset_doh_workers)
        with mock.patch.object(probe, '_doh_source', side_effect=source), \
                mock.patch.object(probe, 'DOH_TIMEOUT_SECONDS', 0.1):
            first = probe.resolve_public_dns(PUBLIC_HOST, time.monotonic() + 0.5)
            self.assertEqual(first['sources'][0]['state'], 'timeout')
            release.set()
            for thread in threads:
                thread.join(1)
            second = probe.resolve_public_dns(PUBLIC_HOST, time.monotonic() + 0.5)
        self.assertEqual(second['sources'][0]['state'], 'ok')
        reset_doh_workers()

    def test_entry_failures_are_classified_and_connections_close(self):
        cases = (
            (ssl.SSLCertVerificationError(1, SECRET), 'tls_certificate_invalid',
             'failed', 'failed'),
            (ssl.SSLEOFError(8, SECRET), 'tls_handshake_failed', 'failed', 'not_checked'),
            (TimeoutError(SECRET), 'timeout', 'not_checked', 'not_checked'),
            (ConnectionResetError(SECRET), 'connection_failed', 'not_checked', 'not_checked'),
            (http.client.BadStatusLine(SECRET), 'connection_failed',
             'not_checked', 'not_checked'),
        )
        for error, reason, tls_state, certificate_state in cases:
            with self.subTest(reason=reason, error=type(error).__name__):
                connection = mock.MagicMock()
                connection.connect.side_effect = error
                with mock.patch.object(probe, 'resolve_public_addresses',
                                       return_value=([PUBLIC_IP], 'ok')), \
                        mock.patch.object(probe, '_public_connection',
                                          return_value=connection):
                    result = probe.check_public_connection(PUBLIC_URL)
                self.assertEqual(result['overall'], 'all_failed')
                self.assertEqual(result['reason'], 'all_entries_failed')
                entry = result['entries'][0]
                self.assertEqual(entry['state'], 'failed')
                self.assertEqual(entry['dns'], 'ok')
                self.assertEqual(entry['tls'], tls_state)
                self.assertEqual(entry['certificate'], certificate_state)
                self.assertEqual(entry['reason'], reason)
                self.assertIsInstance(entry['checked_at_epoch'], float)
                connection.close.assert_called_once()
                self.assertNotIn(SECRET, json.dumps(result))

    def test_partial_failure_is_degraded_and_keeps_the_working_path(self):
        failures = []

        def connect(host, address, timeout):
            if address == PUBLIC_IP:
                return factory(host, address, timeout)
            failing = mock.MagicMock()
            failing.connect.side_effect = ssl.SSLEOFError(8, SECRET)
            failures.append(failing)
            return failing

        with isolated_http([]) as (seen, connections, factory):
            with mock.patch.object(probe, 'resolve_public_addresses',
                                   return_value=([PUBLIC_IP, '203.0.113.11'], 'ok')), \
                    mock.patch.object(probe, '_public_connection', connect):
                result = probe.check_public_connection(PUBLIC_URL)
        self.assertEqual(result['overall'], 'degraded')
        self.assertEqual(result['reason'], 'partial_failure')
        self.assertEqual([entry['state'] for entry in result['entries']],
                         ['ok', 'failed'])
        self.assertEqual(result['entries'][1]['reason'], 'tls_handshake_failed')
        self.assertTrue(all(connection.close_calls for connection in connections))
        failures[0].close.assert_called_once()
        self.assertEqual(seen, [])

    def test_all_failed_entries_report_all_entries_failed(self):
        connections = []

        def connect(host, address, timeout):
            failing = mock.MagicMock()
            failing.connect.side_effect = ssl.SSLEOFError(8, SECRET)
            connections.append(failing)
            return failing

        with mock.patch.object(probe, 'resolve_public_addresses',
                               return_value=([PUBLIC_IP, '203.0.113.11'], 'ok')), \
                mock.patch.object(probe, '_public_connection', connect):
            result = probe.check_public_connection(PUBLIC_URL)
        self.assertEqual(result['overall'], 'all_failed')
        self.assertEqual(result['reason'], 'all_entries_failed')
        self.assertEqual([entry['state'] for entry in result['entries']],
                         ['failed', 'failed'])
        self.assertTrue(all(connection.close.called for connection in connections))

    def test_failed_entries_with_a_deadline_remain_incomplete(self):
        now = [0.0]

        def connect(host, address, timeout):
            connection = mock.MagicMock()

            def fail():
                now[0] = 16.0
                raise ssl.SSLEOFError(8, SECRET)

            connection.connect.side_effect = fail
            return connection

        with mock.patch.object(probe, 'resolve_public_addresses',
                               return_value=([PUBLIC_IP, '203.0.113.11'], 'ok')), \
                mock.patch.object(probe, '_public_connection', connect), \
                mock.patch.object(probe.time, 'monotonic', side_effect=lambda: now[0]):
            result = probe.check_public_connection(PUBLIC_URL)
        self.assertEqual(result['overall'], 'incomplete')
        self.assertEqual(result['reason'], 'deadline_exceeded')
        self.assertEqual([entry['state'] for entry in result['entries']],
                         ['failed', 'unchecked'])
        self.assertEqual(result['entries'][1]['reason'], 'not_checked')

    def test_per_entry_deadline_never_exceeds_the_round_deadline(self):
        now = [0.0]
        timeouts = []

        class Connection:
            def connect(self):
                pass

            def close(self):
                pass

        def connect(host, address, timeout):
            timeouts.append(timeout)
            return Connection()

        with mock.patch.object(probe, 'resolve_public_addresses',
                               return_value=([PUBLIC_IP, '203.0.113.11'], 'ok')), \
                mock.patch.object(probe, '_public_connection', connect), \
                mock.patch.object(probe.time, 'monotonic', side_effect=lambda: now[0]):
            result = probe.check_public_connection(PUBLIC_URL, deadline_s=5.0)
        self.assertEqual(result['overall'], 'ok')
        self.assertEqual(timeouts, [5.0, 5.0])

    def test_round_deadline_leaves_later_entries_unchecked_not_failed(self):
        now = [0.0]

        class AdvancingConnection:
            def __init__(self):
                self.closed = 0

            def connect(self):
                now[0] = 16.0

            def close(self):
                self.closed += 1

        created = []

        def connect(host, address, timeout):
            connection = AdvancingConnection()
            created.append(connection)
            return connection

        with mock.patch.object(probe, 'resolve_public_addresses',
                               return_value=([PUBLIC_IP, '203.0.113.11'], 'ok')), \
                mock.patch.object(probe, '_public_connection', connect), \
                mock.patch.object(probe.time, 'monotonic', side_effect=lambda: now[0]):
            result = probe.check_public_connection(PUBLIC_URL)
        self.assertEqual(result['overall'], 'degraded')
        self.assertEqual(result['reason'], 'deadline_exceeded')
        self.assertEqual(result['entries'][0]['state'], 'ok')
        unchecked = result['entries'][1]
        self.assertEqual(unchecked['state'], 'unchecked')
        self.assertEqual(unchecked['reason'], 'not_checked')
        self.assertEqual((unchecked['dns'], unchecked['tls'], unchecked['certificate']),
                         ('not_checked', 'not_checked', 'not_checked'))
        self.assertIsNone(unchecked['checked_at_epoch'])
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].closed, 1)
        self.assertEqual(result['elapsed_ms'], 16000)

    def test_round_deadline_before_the_first_entry_is_not_checked(self):
        now = [0.0]

        def resolver(host, deadline):
            now[0] = 16.0
            return [PUBLIC_IP], 'ok'

        with mock.patch.object(probe, 'resolve_public_addresses', side_effect=resolver), \
                mock.patch.object(probe, '_public_connection') as factory, \
                mock.patch.object(probe.time, 'monotonic', side_effect=lambda: now[0]):
            result = probe.check_public_connection(PUBLIC_URL)
        self.assertEqual(result['overall'], 'not_checked')
        self.assertEqual(result['reason'], 'deadline_exceeded')
        self.assertEqual(result['entries'][0]['state'], 'unchecked')
        self.assertEqual(result['entries'][0]['reason'], 'not_checked')
        factory.assert_not_called()

    def test_deadline_is_bounded_and_reported(self):
        cases = (
            (None, probe.PUBLIC_CHECK_TIMEOUT_S),
            ('invalid', probe.PUBLIC_CHECK_TIMEOUT_S),
            (0.0, probe.PUBLIC_CHECK_TIMEOUT_S),
            (0.099, probe.PUBLIC_CHECK_TIMEOUT_S),
            (0.1, 0.1),
            (probe.PUBLIC_CHECK_MAX_TIMEOUT_S, probe.PUBLIC_CHECK_MAX_TIMEOUT_S),
            (probe.PUBLIC_CHECK_MAX_TIMEOUT_S + 0.1, probe.PUBLIC_CHECK_TIMEOUT_S),
            (float('nan'), probe.PUBLIC_CHECK_TIMEOUT_S),
            (float('inf'), probe.PUBLIC_CHECK_TIMEOUT_S),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                with mock.patch.object(probe, 'resolve_public_addresses',
                                       return_value=([], 'dns_failed')):
                    result = probe.check_public_connection(PUBLIC_URL, deadline_s=value)
                self.assertEqual(result['deadline_s'], expected)


class PublicConnectionCheckCliTests(unittest.TestCase):
    def test_check_cli_never_reads_stdin_or_probes_the_model(self):
        out = io.StringIO()
        with isolated_http([]) as (seen, connections, factory), \
                mock.patch.object(probe, 'resolve_public_addresses',
                                  return_value=([PUBLIC_IP], 'ok')), \
                mock.patch.object(probe, '_public_connection', factory), \
                mock.patch.object(probe, 'probe_public_model',
                                  side_effect=AssertionError('model probe must not run')), \
                mock.patch.object(probe.sys, 'stdin', object()), \
                redirect_stdout(out):
            code = probe.main(['--check-connection', '--public-url', PUBLIC_URL])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload['overall'], 'ok')
        self.assertEqual(payload['reason'], 'ok')
        self.assertEqual(seen, [])
        self.assertNotIn(SECRET, out.getvalue())
        self.assertTrue(all(connection.close_calls for connection in connections))

    def test_check_cli_exit_codes_follow_the_overall_state(self):
        cases = (('ok', 0), ('degraded', 1), ('all_failed', 1),
                 ('incomplete', 1), ('not_checked', 2))
        for overall, expected in cases:
            with self.subTest(overall=overall):
                out = io.StringIO()
                with mock.patch.object(probe, 'check_public_connection',
                                       return_value={'overall': overall}) as check, \
                        redirect_stdout(out):
                    code = probe.main(['--check-connection', '--public-url', PUBLIC_URL])
                self.assertEqual(code, expected)
                self.assertEqual(json.loads(out.getvalue()), {'overall': overall})
                check.assert_called_once_with(
                    PUBLIC_URL, deadline_s=probe.PUBLIC_CHECK_TIMEOUT_S)

    def test_check_cli_passes_a_valid_deadline_and_clamps_an_invalid_one(self):
        out = io.StringIO()
        with mock.patch.object(probe, 'check_public_connection',
                               return_value={'overall': 'ok'}) as check, \
                redirect_stdout(out):
            probe.main(['--check-connection', '--public-url', PUBLIC_URL,
                        '--deadline-s', '5'])
        check.assert_called_once_with(PUBLIC_URL, deadline_s=5.0)

        with mock.patch.object(probe, 'resolve_public_addresses',
                               return_value=([], 'dns_failed')), \
                redirect_stdout(io.StringIO()):
            code = probe.main(['--check-connection', '--public-url', PUBLIC_URL,
                               '--deadline-s', '0.01'])
        self.assertEqual(code, 2)

    def test_check_cli_without_a_valid_public_url_does_not_touch_the_network(self):
        out = io.StringIO()
        with mock.patch.object(probe, 'resolve_public_addresses') as resolver, \
                mock.patch.object(probe, '_public_connection') as factory, \
                redirect_stdout(out):
            code = probe.main(['--check-connection'])
        self.assertEqual(code, 2)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload['overall'], 'not_checked')
        self.assertEqual(payload['reason'], 'public_url_unavailable')
        self.assertEqual(payload['entries'], [])
        resolver.assert_not_called()
        factory.assert_not_called()

    def test_model_mode_without_arguments_still_reads_stdin_and_probes_once(self):
        payload = json.dumps({'secret': SECRET, 'public_url': PUBLIC_URL}).encode('utf-8')
        expected = {'connected': True, 'reason': 'ok', 'stage': 'inference',
                    'http_status': 200, 'model': 'fixture-model',
                    'elapsed_ms': 5, 'public_ip': PUBLIC_IP}
        stdin = mock.Mock()
        stdin.buffer = io.BytesIO(payload)
        out = io.StringIO()
        with mock.patch.object(probe.sys, 'stdin', stdin), \
                mock.patch.object(probe, 'probe_public_model',
                                  return_value=expected) as model, \
                redirect_stdout(out):
            code = probe.main([])
        self.assertEqual(code, 0)
        model.assert_called_once_with(SECRET, PUBLIC_URL)
        self.assertEqual(json.loads(out.getvalue()), expected)


if __name__ == '__main__':
    unittest.main()
