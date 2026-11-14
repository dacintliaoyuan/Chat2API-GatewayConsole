"""Bounded public-path checks through the configured public origin.

Two independent entry points share the same public DNS and TLS boundary:
``probe_public_model`` performs the credential-bound model round trip, while
``check_public_connection`` only resolves DNS and completes TLS handshakes.
This stdlib-only worker receives credentials on stdin, never in argv or logs.
The parent can terminate it even when the operating system DNS resolver stalls.
"""
from datetime import datetime, timezone
import http.client
import ipaddress
import json
import math
import os
import socket
import ssl
import sys
import threading
import time
from urllib.parse import urlencode, urlsplit


PUBLIC_ORIGIN_ENV = 'CONSOLE_PUBLIC_ORIGIN'
PUBLIC_ORIGIN_UNCONFIGURED = 'public_origin_unconfigured'
PUBLIC_ORIGIN_INVALID = 'public_origin_invalid'


def origin_host(value):
    """The host of a STRICT https origin, or None.

    This is the ONE parser for ``CONSOLE_PUBLIC_ORIGIN``. The accepted shape
    is ``https://<host>`` with an
    optional ``:443``; a path, query, userinfo, fragment, other scheme/port,
    backslash, whitespace or a malformed DNS label is refused (never guessed
    or repaired). It lives here because the probe worker runs isolated
    (``python -I``) and cannot import a sibling module; the console-side
    ``console_public_url`` re-exports it so both ends share this ONE parser.
    """
    if not isinstance(value, str) or not value or len(value) > 253 \
            or not value.isascii():
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        if (parsed.scheme != 'https' or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or parsed.path not in ('', '/') or '?' in value or '#' in value
                or parsed.netloc not in (host, host + ':443')
                or '\\' in value
                or any(c.isspace() or ord(c) < 32 for c in value)
                or any(not label or len(label) > 63 or label.startswith('-')
                       or label.endswith('-')
                       or any(not (c.isalnum() or c == '-') for c in label)
                       for label in host.split('.'))):
            return None
    except ValueError:
        return None
    return host


def public_origin(environ=None):
    """``(normalized_origin, reason)`` from ``CONSOLE_PUBLIC_ORIGIN``.

    ``reason`` is empty on success, ``public_origin_unconfigured`` when the key
    is absent/empty, and ``public_origin_invalid`` for a malformed value. An
    invalid value never falls back to another host.
    """
    environ = os.environ if environ is None else environ
    raw = environ.get(PUBLIC_ORIGIN_ENV)
    if raw is None:
        return None, PUBLIC_ORIGIN_UNCONFIGURED
    if not isinstance(raw, str):
        return None, PUBLIC_ORIGIN_INVALID
    if not raw.strip():
        return None, PUBLIC_ORIGIN_UNCONFIGURED
    host = origin_host(raw)
    if host is None:
        return None, PUBLIC_ORIGIN_INVALID
    return 'https://' + host, ''


def public_host(url):
    """Accept only the configured public origin or a discovered TS origin.

    ``CONSOLE_PUBLIC_ORIGIN`` (D18-1) makes its host the ONLY accepted host;
    an invalid configured value accepts no host at all instead of silently
    falling back to a different public name. With the key absent the historical
    server-discovered Tailscale HTTPS origin rule is unchanged.
    """
    host = origin_host(url)
    if host is None:
        return None
    configured, reason = public_origin()
    if reason == PUBLIC_ORIGIN_INVALID:
        return None
    if configured is not None:
        return host if host == origin_host(configured) else None
    return host if host.endswith('.ts.net') else None


def public_source(host):
    """Which configured origin a valid probe host came from (D18-1)."""
    configured, _reason = public_origin()
    if configured is not None and host == origin_host(configured):
        return 'configured'
    return 'tailscale'


TIMEOUT_SECONDS = 60
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_OUTPUT_TOKENS = 32
MAX_PUBLIC_ADDRESSES = 4
CATALOG_ADDRESS_TIMEOUT_SECONDS = 8
# Two independent DoH sources answer every automatic/manual resolution. Both
# queries run in parallel inside the SAME bounded round deadline; a slow source
# cannot add its own timeout to the round. The per-source cap only keeps one
# stalled resolver from consuming the whole round budget.
DOH_SOURCES = (
    ('cloudflare', 'cloudflare-dns.com', '/dns-query'),
    ('nextdns', 'dns.nextdns.io', '/dns-query'),
)
DOH_TIMEOUT_SECONDS = 8.0
DOH_MAX_RESPONSE_BYTES = 65536
DNS_TTL_MAX_SECONDS = 2147483647
DOH_MAX_WORKERS = 8
# Prefer a catalog model this deployment is known to serve. The first catalog
# entry can be an upstream model that is temporarily returning 5xx; the manual
# key test must not fail the whole public path for that. The ID still comes from
# the catalog and the request budget stays exactly one inference.
PREFERRED_MODEL_IDS = ("DeepSeek-V4.1-Flash-1017",)
PUBLIC_CHECK_TIMEOUT_S = 15.0
PUBLIC_CHECK_MAX_TIMEOUT_S = 60.0
PUBLIC_CHECK_ADDRESS_TIMEOUT_S = 8.0
PUBLIC_CHECK_MAX_ADDRESSES = MAX_PUBLIC_ADDRESSES


def _read_json(response, sock, deadline, limit=MAX_RESPONSE_BYTES):
    raw = bytearray()
    while len(raw) <= limit:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError()
        sock.settimeout(budget)
        chunk = response.read1(min(65536, limit + 1 - len(raw)))
        if not chunk:
            if isinstance(response.length, int) and response.length > 0:
                raise http.client.IncompleteRead(bytes(raw), response.length)
            break
        raw.extend(chunk)
        if response.length == 0:
            break
    if len(raw) > limit:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


class PublicDnsResolution(tuple):
    """``(addresses, reason)`` plus the structured two-source detail.

    It stays a tuple so every existing caller can keep unpacking the two
    historical values; ``detail`` carries the additive DNS evidence.
    """

    def __new__(cls, addresses, reason, detail):
        result = super().__new__(cls, (addresses, reason))
        result.addresses = addresses
        result.reason = reason
        result.detail = detail
        return result


def _valid_ttl(value):
    if type(value) is not int or not 0 <= value <= DNS_TTL_MAX_SECONDS:
        return None
    return value


def _negative_ttl(data):
    """RFC 2308 negative TTL: min(SOA TTL, SOA MINIMUM), or unknown.

    A missing/invalid SOA leaves the TTL unknown; it is never replaced with a
    hard-coded 300 seconds.
    """
    authority = data.get('Authority')
    for item in authority if isinstance(authority, list) else []:
        if not isinstance(item, dict) or item.get('type') != 6:
            continue
        record_ttl = _valid_ttl(item.get('TTL'))
        fields = item.get('data').split() if isinstance(item.get('data'), str) else []
        minimum = None
        if len(fields) >= 7 and fields[6].isdigit():
            minimum = _valid_ttl(int(fields[6]))
        if record_ttl is not None and minimum is not None:
            return min(record_ttl, minimum)
    return None


def _addresses_from_answers(data):
    """Global IPv4 answers in first-seen order, de-duplicated, bounded."""
    addresses = []
    ttl = None
    truncated = False
    answers = data.get('Answer')
    for item in answers if isinstance(answers, list) else []:
        if not isinstance(item, dict) or item.get('type') != 1:
            continue
        try:
            address = ipaddress.ip_address(item.get('data', ''))
        except (TypeError, ValueError):
            continue
        if address.version != 4 or not address.is_global:
            continue
        value = str(address)
        if value in addresses:
            continue
        if len(addresses) >= MAX_PUBLIC_ADDRESSES:
            truncated = True
            continue
        addresses.append(value)
        record_ttl = _valid_ttl(item.get('TTL'))
        if record_ttl is not None:
            ttl = record_ttl if ttl is None else min(ttl, record_ttl)
    return addresses, ttl, truncated


def _doh_source(source, server, path, host, deadline):
    """One bounded DoH query; never receives a sharing credential."""
    started = time.monotonic()
    result = {'source': source, 'state': 'error', 'status': None,
              'classification': 'connection_error', 'addresses': [],
              'addresses_truncated': False, 'ttl_s': None,
              'negative_ttl_s': None, 'checked_at_epoch': None,
              'elapsed_ms': 0}
    connection = None
    try:
        connection = http.client.HTTPSConnection(
            server, 443, timeout=max(.001, deadline - time.monotonic()),
            context=ssl.create_default_context())
        connection.connect()
        sock = connection.sock
        sock.settimeout(max(.001, deadline - time.monotonic()))
        connection.request('GET', path + '?' + urlencode({'name': host, 'type': 'A'}),
                           headers={'Accept': 'application/dns-json'})
        sock.settimeout(max(.001, deadline - time.monotonic()))
        response = connection.getresponse()
        if response.status != 200:
            result.update(state='error', status=response.status,
                          classification='http_error')
            return result
        data = _read_json(response, sock, deadline, DOH_MAX_RESPONSE_BYTES)
        if not isinstance(data, dict) or type(data.get('Status')) is not int:
            result.update(classification='invalid_json')
            return result
        status = data['Status']
        result['status'] = status
        if status == 3:
            result.update(state='nxdomain', classification='nxdomain',
                          negative_ttl_s=_negative_ttl(data))
            return result
        if status == 2:
            result.update(state='error', classification='servfail',
                          negative_ttl_s=_negative_ttl(data))
            return result
        if status != 0:
            result.update(state='error', classification='rcode',
                          negative_ttl_s=_negative_ttl(data))
            return result
        addresses, ttl, truncated = _addresses_from_answers(data)
        if addresses:
            result.update(state='ok', classification='ok', addresses=addresses,
                          addresses_truncated=truncated, ttl_s=ttl)
        else:
            result.update(state='empty', classification='no_answer',
                          negative_ttl_s=_negative_ttl(data))
        return result
    except TimeoutError:
        # A timeout is not confirmed DNS failure; the caller reports it.
        result.update(state='timeout', classification='timeout')
    except (OSError, http.client.HTTPException):
        result.update(state='error', classification='connection_error')
    finally:
        if connection is not None:
            connection.close()
        result['checked_at_epoch'] = time.time()
        result['elapsed_ms'] = max(0, int((time.monotonic() - started) * 1000))
    return result


def _dns_timeout_source(source):
    return {'source': source, 'state': 'timeout', 'status': None,
            'classification': 'timeout', 'addresses': [],
            'addresses_truncated': False, 'ttl_s': None,
            'negative_ttl_s': None, 'checked_at_epoch': None,
            'elapsed_ms': 0}


def _dns_not_checked_source(source):
    return {'source': source, 'state': 'not_checked', 'status': None,
            'classification': 'not_checked', 'addresses': [],
            'addresses_truncated': False, 'ttl_s': None,
            'negative_ttl_s': None, 'checked_at_epoch': None,
            'elapsed_ms': 0}


_DOH_WORKERS = {}
_DOH_WORKERS_LOCK = threading.Lock()


def _prune_doh_workers_locked():
    for key, pending in list(_DOH_WORKERS.items()):
        if not pending['thread'].is_alive():
            _DOH_WORKERS.pop(key, None)


def _acquire_doh_worker(source, server, path, host, deadline):
    """At most one live worker per (source, host), capped globally.

    A stuck worker is reused (its wait is already expired) instead of spawning one
    per check; it cannot rewrite a result already harvested because the slot is
    guarded by the same lock. The daemon is a last-resort process-exit boundary.
    """
    key = (source, host)
    now = time.monotonic()
    with _DOH_WORKERS_LOCK:
        _prune_doh_workers_locked()
        pending = _DOH_WORKERS.get(key)
        if pending is not None:
            if pending['result'] is not None or not pending['thread'].is_alive():
                _DOH_WORKERS.pop(key, None)
                pending = None
        if pending is None:
            if sum(1 for item in _DOH_WORKERS.values()
                   if item['thread'].is_alive()) >= DOH_MAX_WORKERS:
                return None
            pending = {
                'source': source, 'thread': None,
                'deadline': min(deadline, now + DOH_TIMEOUT_SECONDS),
                'result': None, 'result_at': None,
                'event': threading.Event(),
            }

            def run(pending=pending, server=server, path=path, host=host):
                try:
                    value = _doh_source(source, server, path, host,
                                        pending['deadline'])
                except Exception:  # noqa: BLE001 - bounded source error
                    value = _dns_error_source(source)
                with _DOH_WORKERS_LOCK:
                    if pending['result'] is None:
                        pending['result'] = value
                        pending['result_at'] = time.monotonic()
                    pending['event'].set()

            thread = threading.Thread(target=run, daemon=True)
            pending['thread'] = thread
            _DOH_WORKERS[key] = pending
            thread.start()
        return pending


def _wait_doh_worker(pending, deadline):
    wait_until = min(deadline, pending['deadline'])
    remaining = wait_until - time.monotonic()
    if remaining > 0:
        pending['event'].wait(remaining)
    with _DOH_WORKERS_LOCK:
        result_at = pending['result_at']
        if pending['result'] is not None and result_at is not None \
                and result_at <= wait_until + 0.001:
            return pending['result']
        # A result that landed after this call's deadline is a late sample; it is
        # never returned as this round's evidence.
        return _dns_timeout_source(pending['source'])


class PublicDnsTimeout(TimeoutError):
    """Both DoH sources timed out; the detail still names both sources."""

    def __init__(self, detail):
        super().__init__('dns_timeout')
        self.detail = detail


def _dns_error_source(source):
    return {'source': source, 'state': 'error', 'status': None,
            'classification': 'connection_error', 'addresses': [],
            'addresses_truncated': False, 'ttl_s': None,
            'negative_ttl_s': None, 'checked_at_epoch': time.time(),
            'elapsed_ms': 0}


def _aggregate_dns(sources, started):
    """Combine both sources without turning a DNS difference into a fault.

    One valid answer plus one explicit negative answer is a disagreement. One
    answer plus one unreadable/timeout source is an unconfirmed source. Two
    different valid address sets are not a fault: the union is used.
    """
    addresses = []
    truncated = False
    ttl = None
    negative_ttl = None
    for source in sources:
        for address in source['addresses']:
            if address not in addresses:
                if len(addresses) >= MAX_PUBLIC_ADDRESSES:
                    truncated = True
                    continue
                addresses.append(address)
        truncated = truncated or bool(source.get('addresses_truncated'))
        if source['ttl_s'] is not None:
            ttl = source['ttl_s'] if ttl is None else min(ttl, source['ttl_s'])
        if source['negative_ttl_s'] is not None:
            negative_ttl = source['negative_ttl_s'] if negative_ttl is None \
                else min(negative_ttl, source['negative_ttl_s'])
    states = [source['state'] for source in sources]
    if addresses:
        if all(state == 'ok' for state in states):
            state, reason = 'ok', 'ok'
        elif 'ok' in states and any(state in ('empty', 'nxdomain')
                                   for state in states):
            state, reason = 'disagreement', 'dns_disagreement'
        else:
            state, reason = 'unconfirmed', 'dns_unconfirmed'
    elif all(state in ('empty', 'nxdomain') for state in states):
        if any(state == 'nxdomain' for state in states):
            state, reason = 'nxdomain', 'dns_nxdomain'
        else:
            state, reason = 'empty', 'dns_empty'
    elif all(state == 'timeout' for state in states):
        state, reason = 'timeout', 'dns_timeout'
    elif all(state == 'error' for state in states):
        state, reason = 'error', 'dns_failed'
    elif any(state == 'not_checked' for state in states):
        state, reason = 'not_checked', 'dns_not_checked'
    else:
        state, reason = 'unconfirmed', 'dns_unconfirmed'
    return {'version': 1, 'checked_at_utc': _utc_now_iso(),
            'checked_at_epoch': time.time(), 'state': state,
            'reason': reason, 'addresses': addresses,
            'addresses_truncated': truncated, 'ttl_s': ttl,
            'negative_ttl_s': negative_ttl,
            'elapsed_ms': max(0, int((time.monotonic() - started) * 1000)),
            'sources': sources}


def resolve_public_dns(host, deadline):
    """Query both DoH sources in parallel inside one bounded deadline.

    At most one worker per (source, host) and ``DOH_MAX_WORKERS`` live workers
    overall. A source that never returns is reused and fails fast on later checks;
    it does not accumulate one thread per check.
    """
    if deadline <= time.monotonic():
        raise TimeoutError()
    started = time.monotonic()
    per_source_deadline = min(deadline, started + DOH_TIMEOUT_SECONDS)
    results = [None] * len(DOH_SOURCES)
    pendings = []
    for index, source in enumerate(DOH_SOURCES):
        pending = _acquire_doh_worker(source[0], source[1], source[2], host,
                                     per_source_deadline)
        if pending is None:
            results[index] = _dns_not_checked_source(source[0])
        else:
            pendings.append((index, pending))
    for index, pending in pendings:
        results[index] = _wait_doh_worker(pending, per_source_deadline)
    if all(source['state'] == 'timeout' for source in results):
        raise PublicDnsTimeout(_aggregate_dns(results, started))
    return _aggregate_dns(results, started)


def _legacy_dns_detail(addresses, reason, started):
    """Only patched test doubles reach this; the real path carries detail."""
    state = 'ok' if reason == 'ok' and addresses else (
        'timeout' if reason == 'dns_timeout'
        else 'nxdomain' if reason == 'dns_nxdomain'
        else 'empty' if reason == 'dns_empty' else 'error')
    return {'version': 1, 'checked_at_utc': _utc_now_iso(),
            'checked_at_epoch': time.time(), 'state': state,
            'reason': reason, 'addresses': list(addresses),
            'addresses_truncated': False, 'ttl_s': None,
            'negative_ttl_s': None,
            'elapsed_ms': max(0, int((time.monotonic() - started) * 1000)),
            'sources': []}


def resolve_public_addresses(host, deadline):
    """Return ``(addresses, reason)`` plus structured two-source detail.

    reason is one of: "ok", "dns_disagreement", "dns_unconfirmed",
    "dns_empty", "dns_nxdomain", "dns_failed", "dns_timeout". Only globally
    routable IPv4 addresses are accepted, in first-seen order, de-duplicated and
    bounded. Raises TimeoutError when BOTH DoH exchanges cannot finish before
    ``deadline``; every other failure is reported in the tuple. The resolver
    receives only a public hostname, never a sharing credential. HTTPS
    authenticates its response; MagicDNS/hosts pointing back into the local
    tailnet are never accepted as public evidence.
    """
    detail = resolve_public_dns(host, deadline)
    return PublicDnsResolution(detail['addresses'], detail['reason'], detail)


def _public_addresses(host, deadline):
    """Legacy model-probe adapter: any answer-less result is a DNS failure."""
    resolution = resolve_public_addresses(host, deadline)
    if not resolution.addresses:
        raise socket.gaierror()
    return resolution.addresses


def _model_addresses(host, deadline):
    """Manual-model adapter: the same resolution, with its DNS detail kept."""
    return resolve_public_addresses(host, deadline)


def _public_connection(host, public_ip, timeout):
    connection = http.client.HTTPSConnection(
        host, 443, timeout=timeout, context=ssl.create_default_context())
    # Keep HTTPSConnection's certificate verification, SNI and HTTP Host bound
    # to the domain, but connect only to the public resolver's ingress address.
    # HTTPConnection deliberately exposes this instance transport seam.
    connection._create_connection = lambda address, budget, source_address=None: \
        socket.create_connection((public_ip, 443), budget, source_address)
    return connection


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _bounded_check_deadline(value):
    """Clamp the whole-round deadline to the documented range."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return PUBLIC_CHECK_TIMEOUT_S
    if not math.isfinite(number) or not 0.1 <= number <= PUBLIC_CHECK_MAX_TIMEOUT_S:
        return PUBLIC_CHECK_TIMEOUT_S
    return number


def _unchecked_entry(host, address):
    """An entry the round deadline never reached is not a confirmed failure."""
    return {'host': host, 'ip': address, 'state': 'unchecked',
            'dns': 'not_checked', 'tls': 'not_checked',
            'certificate': 'not_checked', 'reason': 'not_checked',
            'elapsed_ms': 0, 'checked_at_epoch': None}


def _check_entry(host, address, round_deadline):
    """TCP + TLS handshake only; never send an HTTP request on this socket."""
    started = time.monotonic()
    entry = {'host': host, 'ip': address, 'state': 'failed',
             'dns': 'ok', 'tls': 'not_checked', 'certificate': 'not_checked',
             'reason': 'connection_failed', 'elapsed_ms': 0,
             'checked_at_epoch': None}
    entry_deadline = min(round_deadline,
                         time.monotonic() + PUBLIC_CHECK_ADDRESS_TIMEOUT_S)
    connection = _public_connection(
        host, address, max(0.001, entry_deadline - time.monotonic()))
    try:
        connection.connect()
    except ssl.SSLCertVerificationError:
        entry.update(state='failed', tls='failed', certificate='failed',
                     reason='tls_certificate_invalid')
    except ssl.SSLError:
        entry.update(state='failed', tls='failed', certificate='not_checked',
                     reason='tls_handshake_failed')
    except TimeoutError:
        entry.update(state='failed', reason='timeout')
    except (OSError, http.client.HTTPException):
        entry.update(state='failed', reason='connection_failed')
    else:
        entry.update(state='ok', tls='ok', certificate='ok', reason='ok')
    finally:
        connection.close()
    entry['elapsed_ms'] = max(0, int((time.monotonic() - started) * 1000))
    entry['checked_at_epoch'] = time.time()
    return entry


def _summarize_check(result):
    entries = result['entries']
    checked = [entry for entry in entries if entry['state'] != 'unchecked']
    has_unchecked = len(checked) != len(entries)
    if not checked:
        result.update(overall='not_checked', reason='deadline_exceeded')
    elif all(entry['state'] == 'ok' for entry in checked):
        if has_unchecked:
            result.update(overall='degraded', reason='deadline_exceeded')
        else:
            result.update(overall='ok', reason='ok')
    elif all(entry['state'] == 'failed' for entry in checked):
        if has_unchecked:
            result.update(overall='incomplete', reason='deadline_exceeded')
        else:
            result.update(overall='all_failed', reason='all_entries_failed')
    else:
        result.update(overall='degraded',
                      reason='deadline_exceeded' if has_unchecked else 'partial_failure')


def check_public_connection(public_url, *, deadline_s=PUBLIC_CHECK_TIMEOUT_S):
    """One bounded public DNS/TLS round. No secret, no HTTP request.

    Returns the frozen shape in contract section 1.4. Never raises for a bad
    URL, DNS failure or TLS failure; those are reported in the result. Only an
    unexpected internal error may propagate to the caller (the supervisor
    catches it and records ``probe_failed``).
    """
    started = time.monotonic()
    bounded = _bounded_check_deadline(deadline_s)
    result = {'version': 1, 'checked_at_utc': _utc_now_iso(),
              'checked_at_epoch': 0.0, 'public_url': None, 'host': None,
              'source': None,
              'overall': 'not_checked', 'reason': 'public_url_unavailable',
              'deadline_s': bounded, 'elapsed_ms': 0, 'entries': [],
              'dns': None}
    host = public_host(public_url)
    if host is None:
        return result

    result.update(public_url='https://' + host, host=host,
                  source=public_source(host))
    deadline = started + bounded
    try:
        resolution = resolve_public_addresses(host, deadline)
    except PublicDnsTimeout as exc:
        result['reason'] = 'dns_timeout'
        result['dns'] = exc.detail
    except TimeoutError:
        result['reason'] = 'dns_timeout'
        result['dns'] = {
            'version': 1, 'checked_at_utc': _utc_now_iso(),
            'checked_at_epoch': time.time(), 'state': 'timeout',
            'reason': 'dns_timeout', 'addresses': [],
            'addresses_truncated': False, 'ttl_s': None,
            'negative_ttl_s': None, 'sources': [],
        }
    else:
        detail = getattr(resolution, 'detail', None)
        if not isinstance(detail, dict):
            detail = _legacy_dns_detail(resolution[0], resolution[1], started)
        result['dns'] = detail
        addresses = resolution[0]
        reason = resolution[1]
        if addresses:
            entries = []
            exhausted = False
            for address in addresses[:PUBLIC_CHECK_MAX_ADDRESSES]:
                if exhausted or time.monotonic() >= deadline:
                    exhausted = True
                    entries.append(_unchecked_entry(host, address))
                else:
                    entries.append(_check_entry(host, address, deadline))
            result['entries'] = entries
            _summarize_check(result)
            dns_state = detail.get('state')
            if result['overall'] == 'ok' and dns_state in (
                    'disagreement', 'unconfirmed'):
                result['overall'] = 'degraded'
                result['reason'] = 'dns_' + dns_state
            if detail.get('addresses_truncated'):
                # Only the first four addresses were checked. Never turn that
                # coverage gap into a confirmed all-entry failure.
                if result['overall'] in ('all_failed', 'not_checked'):
                    result['overall'] = 'incomplete'
                elif result['overall'] == 'ok':
                    result['overall'] = 'degraded'
                result['reason'] = 'addresses_truncated'
        elif reason == 'ok':
            result['reason'] = 'dns_empty'
        else:
            result['reason'] = reason
    result['checked_at_utc'] = _utc_now_iso()
    result['checked_at_epoch'] = time.time()
    result['elapsed_ms'] = max(0, int((time.monotonic() - started) * 1000))
    return result


def _http_reason(status):
    if status in (401, 403):
        return 'key_rejected'
    if status == 429:
        return 'rate_limited'
    if 300 <= status < 400:
        return 'redirect_refused'
    if status >= 500:
        return 'upstream_unavailable'
    return 'http_error'


def probe_public_model(secret, public_url):
    """Bounded public-address failover, with at most one inference request.

    A successful round trip proves this public path at this time, not every
    friend's network. MagicDNS cannot silently substitute a tailnet connection.
"""
    started = time.monotonic()
    deadline = started + TIMEOUT_SECONDS
    result = {'connected': False, 'reason': 'public_url_unavailable',
              'stage': 'configuration', 'http_status': None, 'model': None,
              'elapsed_ms': 0, 'public_ip': None,
              'dns_source': 'cloudflare+nextdns_doh', 'dns': None}
    host = public_host(public_url)
    if host is None:
        return result

    def remaining(request_deadline=None):
        bound = deadline if request_deadline is None else min(deadline, request_deadline)
        budget = bound - time.monotonic()
        if budget <= 0:
            raise TimeoutError()
        return budget

    def request(method, path, body=None, request_deadline=None):
        connection = _public_connection(host, result['public_ip'], remaining(request_deadline))
        try:
            connection.connect()
            sock = connection.sock
            sock.settimeout(remaining(request_deadline))
            headers = {'Authorization': 'Bearer ' + secret,
                       'Accept': 'application/json'}
            if body is not None:
                headers['Content-Type'] = 'application/json'
                body = json.dumps(body).encode('utf-8')
            connection.request(method, path, body=body, headers=headers)
            sock.settimeout(remaining(request_deadline))
            response = connection.getresponse()
            result['http_status'] = response.status
            if response.status != 200:
                result['reason'] = _http_reason(response.status)
                return None
            result['reason'] = 'invalid_response'
            read_deadline = deadline if request_deadline is None else min(deadline, request_deadline)
            return _read_json(response, sock, read_deadline)
        finally:
            connection.close()

    try:
        result['stage'] = 'dns'
        resolution = _model_addresses(host, deadline)
        addresses = resolution.addresses \
            if isinstance(resolution, PublicDnsResolution) else list(resolution)
        result['dns'] = resolution.detail \
            if isinstance(resolution, PublicDnsResolution) else None
        if not addresses:
            result['reason'] = resolution.reason \
                if isinstance(resolution, PublicDnsResolution) else 'dns_failed'
            return result
        result['stage'] = 'models'
        # Only the read-only catalog may fail over. One bad ingress must not
        # consume the entire inference budget; every socket still verifies the
        # original hostname. HTTP/JSON failures remain authoritative, and a
        # potentially accepted model POST is never replayed on another address.
        for address in addresses[:MAX_PUBLIC_ADDRESSES]:
            remaining()
            result.update(public_ip=address, http_status=None)
            address_budget = (CATALOG_ADDRESS_TIMEOUT_SECONDS
                              if len(addresses) > 1 else TIMEOUT_SECONDS)
            attempt_deadline = min(deadline, time.monotonic() + address_budget)
            try:
                data = request('GET', '/v1/models', request_deadline=attempt_deadline)
                break
            except ssl.SSLCertVerificationError:
                result['reason'] = 'tls_certificate_invalid'
            except ssl.SSLError:
                result['reason'] = 'tls_handshake_failed'
            except TimeoutError:
                result['reason'] = 'timeout'
            except socket.gaierror:
                result['reason'] = 'dns_failed'
            except (OSError, http.client.HTTPException):
                result['reason'] = 'connection_failed'
        else:
            return result
        models = data.get('data') if isinstance(data, dict) else None
        if data is None:
            return result
        if (not isinstance(models, list) or 'error' in data
                or not all(isinstance(item, dict)
                           and isinstance(item.get('id'), str)
                           and 0 < len(item['id']) <= 256
                           and secret not in item['id']
                           and not any(ord(c) < 32 for c in item['id'])
                           for item in models)):
            return result
        if not models:
            result['reason'] = 'no_models'
            return result
        # The catalog supplies the actual model identity; do not guess an ID.
        # Prefer a known-served catalog model over the first entry (which may
        # be an upstream model temporarily returning 5xx).
        model = next((item['id'] for item in models
                      if item['id'] in PREFERRED_MODEL_IDS), models[0]['id'])
        result.update(stage='inference', model=model, http_status=None)
        data = request('POST', '/v1/chat/completions', {
            'model': model, 'messages': [{'role': 'user', 'content': 'Reply with OK.'}],
            'max_tokens': MAX_OUTPUT_TOKENS, 'stream': False})
        if data is None:
            return result
        choices = data.get('choices')
        message = (choices[0].get('message') if isinstance(choices, list)
                   and choices and isinstance(choices[0], dict) else None)
        content = message.get('content') if isinstance(message, dict) else None
        if ('error' not in data and isinstance(content, str) and content.strip()
                and message.get('role') == 'assistant'):
            result.update(connected=True, reason='ok')
    except ssl.SSLCertVerificationError:
        result['reason'] = ('public_dns_failed' if result['stage'] == 'dns'
                            else 'tls_certificate_invalid')
    except ssl.SSLError:
        result['reason'] = ('public_dns_failed' if result['stage'] == 'dns'
                            else 'tls_handshake_failed')
    except socket.gaierror:
        result['reason'] = 'dns_failed'
    except PublicDnsTimeout as exc:
        result['reason'] = 'dns_timeout'
        result['dns'] = exc.detail
    except TimeoutError:
        result['reason'] = 'timeout'
    except (OSError, http.client.HTTPException):
        result['reason'] = ('public_dns_failed' if result['stage'] == 'dns'
                            else 'connection_failed')
    finally:
        result['elapsed_ms'] = max(0, int((time.monotonic() - started) * 1000))
    return result


def _parse_check_arguments(argv):
    """Parse the check CLI without ever touching stdin or a credential."""
    public_url = None
    deadline_s = PUBLIC_CHECK_TIMEOUT_S
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == '--check-connection':
            index += 1
        elif token == '--public-url':
            if index + 1 < len(argv):
                public_url = argv[index + 1]
                index += 2
            else:
                index += 1
        elif token.startswith('--public-url='):
            public_url = token[len('--public-url='):]
            index += 1
        elif token == '--deadline-s':
            if index + 1 < len(argv):
                try:
                    deadline_s = float(argv[index + 1])
                except (TypeError, ValueError):
                    deadline_s = PUBLIC_CHECK_TIMEOUT_S
                index += 2
            else:
                index += 1
        elif token.startswith('--deadline-s='):
            try:
                deadline_s = float(token[len('--deadline-s='):])
            except (TypeError, ValueError):
                deadline_s = PUBLIC_CHECK_TIMEOUT_S
            index += 1
        else:
            # Unknown switches are a configuration error, not a reason to read
            # stdin or fall back to the credential-bound model probe.
            return None, deadline_s
    return public_url, deadline_s


def _check_exit_code(overall):
    if overall == 'ok':
        return 0
    if overall == 'not_checked':
        return 2
    return 1


def main(argv=None):
    args = list(sys.argv[1:]) if argv is None else list(argv)
    if args:
        if '--check-connection' in args:
            public_url, deadline_s = _parse_check_arguments(args)
        else:
            public_url, deadline_s = None, PUBLIC_CHECK_TIMEOUT_S
        result = check_public_connection(public_url, deadline_s=deadline_s)
        sys.stdout.write(json.dumps(result) + '\n')
        return _check_exit_code(result['overall'])
    try:
        payload = json.loads(sys.stdin.buffer.read(8193))
        secret = payload.get('secret') if isinstance(payload, dict) else None
        if (not isinstance(secret, str) or not 1 <= len(secret) <= 1024
                or any(ord(c) < 33 or ord(c) > 126 for c in secret)):
            raise ValueError()
        result = probe_public_model(secret, payload.get('public_url'))
    except Exception:
        # Never serialize exceptions: HTTP bodies or credentials may be present.
        result = {'connected': False, 'reason': 'probe_failed', 'stage': 'configuration',
                  'http_status': None, 'model': None, 'elapsed_ms': 0}
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
