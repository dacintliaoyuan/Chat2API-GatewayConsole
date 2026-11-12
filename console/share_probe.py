"""Explicit version-bound sharing-key tests through the public Funnel or the
loopback share gateway."""
import http.client
import ipaddress
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

from console.public_probe import (MAX_OUTPUT_TOKENS, MAX_RESPONSE_BYTES,
                                 PREFERRED_MODEL_IDS, TIMEOUT_SECONDS,
                                 _http_reason, public_host)
from share.store import (Disabled, NotFound, SecretUnavailable,
                         SecretUndecryptable, VersionMismatch)
from share.management import BrokerError


_ACTIVE_KEYS = set()
_ACTIVE_LOCK = threading.Lock()
_REASONS = frozenset((
    'ok', 'public_url_unavailable', 'public_dns_failed', 'dns_failed',
    'dns_empty', 'dns_nxdomain', 'dns_no_answer', 'dns_servfail',
    'dns_disagreement', 'dns_unconfirmed', 'dns_timeout',
    'dns_not_checked',
    'tls_certificate_invalid', 'tls_handshake_failed', 'key_rejected',
    'rate_limited', 'redirect_refused', 'upstream_unavailable', 'http_error',
    'invalid_response', 'no_models', 'timeout', 'connection_failed', 'probe_failed'))

_DNS_SOURCE_STATES = frozenset((
    'ok', 'empty', 'nxdomain', 'error', 'timeout', 'not_checked'))
_DNS_CLASSIFICATIONS = frozenset((
    'ok', 'no_answer', 'nxdomain', 'servfail', 'rcode', 'http_error',
    'invalid_json', 'timeout', 'connection_error', 'not_checked'))
_DNS_AGG_STATES = frozenset((
    'ok', 'empty', 'nxdomain', 'error', 'timeout', 'disagreement',
    'unconfirmed', 'not_checked'))


def _valid_dns(value):
    """Accept only the additive, bounded two-source detail (never raw bodies)."""
    if value is None:
        return True
    if not isinstance(value, dict) or value.get('state') not in _DNS_AGG_STATES:
        return False
    reason = value.get('reason')
    if reason is not None and (not isinstance(reason, str) or len(reason) > 64):
        return False
    if not _valid_addresses(value.get('addresses')):
        return False
    for key in ('ttl_s', 'negative_ttl_s'):
        item = value.get(key)
        if item is not None and (type(item) is not int or not 0 <= item <= 2147483647):
            return False
    if value.get('addresses_truncated') not in (None, True, False):
        return False
    checked = value.get('checked_at_epoch')
    if checked is not None and (isinstance(checked, bool)
                               or not isinstance(checked, (int, float))
                               or checked <= 0):
        return False
    sources = value.get('sources')
    if not isinstance(sources, list) or len(sources) > 2:
        return False
    for source in sources:
        if not isinstance(source, dict) or source.get('source') not in ('cloudflare', 'google', 'nextdns'):
            return False
        if source.get('state') not in _DNS_SOURCE_STATES:
            return False
        if source.get('classification') not in _DNS_CLASSIFICATIONS:
            return False
        if not _valid_addresses(source.get('addresses')):
            return False
        if source.get('addresses_truncated') not in (None, True, False):
            return False
        status = source.get('status')
        if status is not None and (type(status) is not int or not 0 <= status <= 65535):
            return False
        for key in ('ttl_s', 'negative_ttl_s'):
            item = source.get(key)
            if item is not None and (type(item) is not int or not 0 <= item <= 2147483647):
                return False
        checked = source.get('checked_at_epoch')
        if checked is not None and (isinstance(checked, bool)
                                   or not isinstance(checked, (int, float))
                                   or checked <= 0):
            return False
        elapsed = source.get('elapsed_ms')
        if elapsed is not None and (type(elapsed) is not int or not 0 <= elapsed <= 600000):
            return False
    return True


def _valid_addresses(value):
    if not isinstance(value, list) or len(value) > 4:
        return False
    for address in value:
        if not isinstance(address, str):
            return False
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return False
        if parsed.version != 4 or not parsed.is_global:
            return False
    return True


def probe_models(secret, public_url):
    """Public catalog AND inference, with a hard process-wide network deadline.

    Credentials travel on stdin only. Worker stderr and raw exception messages
    are never returned, and the subprocess has no shell, retries or fallback.
    """
    failed = {'connected': False, 'reason': 'probe_failed', 'stage': 'configuration'}
    try:
        proc = subprocess.run(
            [sys.executable, '-I', str(Path(__file__).with_name('public_probe.py'))],
            input=json.dumps({'secret': secret, 'public_url': public_url}).encode('utf-8'),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=TIMEOUT_SECONDS + 5,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if proc.returncode != 0 or len(proc.stdout) > 8192:
            return failed
        data = json.loads(proc.stdout)
        if (not isinstance(data, dict) or not isinstance(data.get('connected'), bool)
                or data.get('reason') not in _REASONS
                or data.get('stage') not in ('configuration', 'dns', 'models', 'inference')):
            return failed
        status, elapsed = data.get('http_status'), data.get('elapsed_ms')
        model, address = data.get('model'), data.get('public_ip')
        if (status is not None and (type(status) is not int or not 100 <= status <= 599)
                or type(elapsed) is not int or not 0 <= elapsed <= (TIMEOUT_SECONDS + 5) * 1000
                or model is not None and (not isinstance(model, str) or len(model) > 256
                                         or secret in model or any(ord(c) < 32 for c in model))
                or address is not None and (not isinstance(address, str)
                                            or ipaddress.ip_address(address).version != 4
                                            or not ipaddress.ip_address(address).is_global)):
            return failed
        if data['connected'] and (data['reason'] != 'ok' or data['stage'] != 'inference'
                                  or status != 200 or not model or not address):
            return failed
        if not _valid_dns(data.get('dns')):
            return failed
        return {name: data.get(name) for name in (
            'connected', 'reason', 'stage', 'http_status', 'elapsed_ms', 'model',
            'public_ip', 'dns')}
    except subprocess.TimeoutExpired:
        return {'connected': False, 'reason': 'timeout', 'stage': 'network'}
    except (OSError, ValueError, TypeError):
        return failed


def probe_models_private(secret, base_url="http://127.0.0.1:8100"):
    """Same catalog + inference proof against the loopback share gateway.

    The console and 8100 run on the same cloud host, so this path never uses
    public DNS/TLS and never exposes the key or its plaintext. The result says
    ``tested_from: private`` and keeps the public-only fields absent.
    """
    failed = {'connected': False, 'reason': 'probe_failed', 'stage': 'configuration',
              'tested_from': 'private', 'public_url': None, 'dns': None}
    started = time.monotonic()
    connection = None
    headers = {'Authorization': 'Bearer ' + secret, 'Accept': 'application/json'}
    try:
        connection = http.client.HTTPConnection(
            '127.0.0.1', 8100, timeout=TIMEOUT_SECONDS)
        connection.request('GET', '/v1/models', headers=headers)
        response = connection.getresponse()
        status = response.status
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if status != 200:
            return {**failed, 'reason': _http_reason(status),
                    'stage': 'models', 'http_status': status,
                    'elapsed_ms': int((time.monotonic() - started) * 1000)}
        if len(raw) > MAX_RESPONSE_BYTES:
            return {**failed, 'reason': 'invalid_response',
                    'stage': 'models', 'http_status': status,
                    'elapsed_ms': int((time.monotonic() - started) * 1000)}
        data = json.loads(raw.decode('utf-8'))
        models = data.get('data') if isinstance(data, dict) else None
        if (not isinstance(models, list) or not models or 'error' in data
                or not all(isinstance(item, dict)
                           and isinstance(item.get('id'), str)
                           and 0 < len(item['id']) <= 256
                           and secret not in item['id']
                           and not any(ord(c) < 32 for c in item['id'])
                           for item in models)):
            return {**failed, 'reason': 'invalid_response',
                    'stage': 'models', 'http_status': status,
                    'elapsed_ms': int((time.monotonic() - started) * 1000)}
        model = next((item['id'] for item in models
                      if item['id'] in PREFERRED_MODEL_IDS), models[0]['id'])
        # The private probe verifies the Key against the loopback gateway's
        # authenticated model catalog. It deliberately does NOT send an inference
        # request: the public probe owns that separate, cost-bearing check.
        return {**failed, 'connected': True, 'reason': 'ok',
                'stage': 'models', 'http_status': status,
                'model': model,
                'elapsed_ms': int((time.monotonic() - started) * 1000)}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {**failed, 'reason': 'connection_failed',
                'stage': 'models',
                'elapsed_ms': int((time.monotonic() - started) * 1000)}
    finally:
        if connection is not None:
            connection.close()


def test_key(manager, key_id, expected_row_version, *, public_url=None,
              discover_url=None, private=False):
    result = {'key_id': key_id, 'row_version': expected_row_version,
              'connected': False, 'reason': 'unconfirmed', 'stage': 'configuration',
              'tested_from': 'private' if private else 'public',
              'public_url': None if private else public_url if public_host(public_url) else None,
              'dns': None, 'checked_at': int(time.time())}
    with _ACTIVE_LOCK:
        if key_id in _ACTIVE_KEYS or len(_ACTIVE_KEYS) >= 2:
            result['reason'] = 'test_in_progress'
            return result
        _ACTIVE_KEYS.add(key_id)
    try:
        row = manager.get_key(key_id)
        if row['row_version'] != expected_row_version:
            raise VersionMismatch('stale probe')
        if row.get('deleted_at') is not None or not row['enabled']:
            result['reason'] = 'key_disabled'
            return result
        if not private:
            if discover_url is not None:
                url = discover_url()
                result['public_url'] = url if public_host(url) else None
            if not result['public_url']:
                result['reason'] = 'public_url_unavailable'
                return result
        # The credential belongs to this running restricted sharing gateway.
        if manager.broker.host_verify('running') is not True:
            result['reason'] = 'gateway_unconfirmed'
            return result
        secret = manager.reveal_key_secret(
            key_id, expected_row_version=expected_row_version)['secret']
        if private:
            result.update(probe_models_private(secret))
        else:
            result.update(probe_models(secret, result['public_url']))
        current = manager.get_key(key_id)
        if (current['row_version'] != expected_row_version or not current['enabled']
                or current.get('deleted_at') is not None):
            raise VersionMismatch('key changed during probe')
    except (VersionMismatch, Disabled, NotFound):
        result.update(connected=False, reason='key_changed')
    except (SecretUnavailable, SecretUndecryptable):
        result.update(connected=False, reason='secret_unavailable')
    except BrokerError:
        result.update(connected=False, reason='gateway_unconfirmed')
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_KEYS.discard(key_id)
        result['checked_at'] = int(time.time())
    return result
