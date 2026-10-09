# -*- coding: utf-8 -*-
import json
import os
import re
import sys
import threading
import time
from collections import namedtuple
from datetime import date as _date_type, datetime, timedelta, timezone
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import usagesrv
TIMEZONE_NAME = 'Asia/Shanghai'
MAX_RANGE_DAYS = 92
CREDITS_CURRENCY = 'reference'
_SOURCE_KINDS = {'chat2api': 'estimate'}
_DATE_RE = re.compile('^\\d{4}-\\d{2}-\\d{2}$')
_LEDGER_RECORD_LIMIT = 200000

def default_ledgers():
    return {'chat2api': usagesrv.CHAT2API_LOG}

def _parse_date(value):
    """Strict 'YYYY-MM-DD' -> datetime.date (date/datetime passthrough);
    anything else raises ValueError."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, _date_type):
        return value
    if isinstance(value, str) and _DATE_RE.match(value.strip()):
        try:
            return datetime.strptime(value.strip(), '%Y-%m-%d').date()
        except ValueError:
            pass
    raise ValueError('invalid date %r: expected YYYY-MM-DD (Asia/Shanghai calendar day)' % (value,))

def _parse_range(date_from, date_to):
    """Validate the half-open day interval and enforce the bounded-query cap.

    Returns (from_date, to_date, range_days). from > to or a span above
    MAX_RANGE_DAYS raises ValueError (the HTTP layer maps that to 400).
    """
    lo = _parse_date(date_from)
    hi = _parse_date(date_to)
    if lo > hi:
        raise ValueError('invalid date range: date_from %s > date_to %s' % (lo.isoformat(), hi.isoformat()))
    range_days = (hi - lo).days + 1
    if range_days > MAX_RANGE_DAYS:
        raise ValueError('date range too large: %d days exceeds the %d-day query limit' % (range_days, MAX_RANGE_DAYS))
    return (lo, hi, range_days)

def _iter_dates(lo, hi):
    cur = lo
    while cur <= hi:
        yield cur
        cur += timedelta(days=1)

def _read_ledger_jsonl(path):
    """Read one JSONL ledger file, mirroring usagesrv._read_jsonl
    (usagesrv.py:500-521) field-for-field: strip lines, skip blanks, parse
    each line as JSON, keep dicts only, attach rec["_ms"] via
    usagesrv._parse_ts, sort ascending by _ms, cap at the same default limit.

    Single deliberate difference: usagesrv silently drops malformed lines,
    while this reader counts them so coverage can flag ledger incompleteness
    honestly (console coverage contract; no records are invented either way).

    Returns (records, diagnostics)."""
    records = []
    bad_lines = 0
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    bad_lines += 1
                    continue
                if isinstance(rec, dict):
                    rec['_ms'] = usagesrv._parse_ts(rec.get('time'))
                    records.append(rec)
    except FileNotFoundError:
        return (records, {'missing': True, 'bad_lines': bad_lines})
    except OSError as exc:
        return (records, {'read_error': str(exc), 'bad_lines': bad_lines})
    records.sort(key=lambda r: r.get('_ms') or 0)
    return (records[-_LEDGER_RECORD_LIMIT:], {'bad_lines': bad_lines})

def _resolve_reader(source, handle):
    if callable(handle):

        def reader():
            return (handle(), None)
        return (reader, None)
    if isinstance(handle, (str, os.PathLike)):
        paths = [os.fspath(handle)]
    elif isinstance(handle, (list, tuple)) and handle:
        paths = [os.fspath(p) for p in handle]
    else:
        raise TypeError('unsupported ledger handle: %r' % (handle,))

    def reader():
        merged = []
        diagnostics = {'bad_lines': 0, 'missing_files': 0}
        for p in paths:
            recs, diag = _read_ledger_jsonl(p)
            if diag.get('missing'):
                diagnostics['missing_files'] += 1
            diagnostics['bad_lines'] += diag.get('bad_lines', 0)
            if diag.get('read_error'):
                diagnostics['read_error'] = diag['read_error']
            merged.extend(recs)
        merged.sort(key=lambda r: r.get('_ms') or 0)
        return (merged[-_LEDGER_RECORD_LIMIT:], diagnostics)
    return (reader, paths)

def _new_token_evidence():
    return {'records': 0, 'complete_records': 0, 'partial_records': 0, 'estimated_records': 0, 'unknown_records': 0, 'complete_input': 0, 'complete_output': 0, 'partial_input': 0, 'partial_output': 0, 'estimated_input': 0, 'estimated_output': 0, 'partial_input_known': 0, 'partial_output_known': 0, 'estimated_input_known': 0, 'estimated_output_known': 0}

def _trusted_token_count(value):
    """Narrow evidence validation; legacy fee conversion keeps its own contract."""
    return value if isinstance(value, int) and (not isinstance(value, bool)) and (value >= 0) else None

def _accumulate_token_evidence(acc, rec, *, snapshot=False):
    evidence = acc['token_evidence']
    evidence['records'] += 1
    input_tok = _trusted_token_count(rec.get('input' if snapshot else 'input_tokens'))
    output_tok = _trusted_token_count(rec.get('output' if snapshot else 'output_tokens'))
    source = rec.get('usage_source')
    if source == 'none' or (input_tok is None and output_tok is None):
        evidence['unknown_records'] += 1
        return
    complete = input_tok is not None and output_tok is not None
    estimated = source == 'estimated' or (source is None and rec.get('estimated') is True)
    if estimated:
        prefix = 'estimated'
    elif complete and source != 'partial' and (not _non_ok_status(rec.get('status'))):
        evidence['complete_records'] += 1
        evidence['complete_input'] += input_tok
        evidence['complete_output'] += output_tok
        return
    else:
        prefix = 'partial'
    evidence[prefix + '_records'] += 1
    for side, value in (('input', input_tok), ('output', output_tok)):
        if value is not None:
            evidence[prefix + '_' + side] += value
            evidence[prefix + '_' + side + '_known'] += 1

def _emit_token_evidence(evidence, kind):
    fields = {'token_coverage': {key: evidence[key] for key in ('records', 'complete_records', 'partial_records', 'estimated_records', 'unknown_records')}}
    coverage = fields['token_coverage']
    coverage['complete'] = coverage['records'] > 0 and coverage['complete_records'] == coverage['records']
    coverage['unit'] = 'request'
    fields['complete_input_tokens'] = evidence['complete_input'] if coverage['complete_records'] else None
    fields['complete_output_tokens'] = evidence['complete_output'] if coverage['complete_records'] else None
    fields['token_total'] = evidence['complete_input'] + evidence['complete_output'] if coverage['complete_records'] else None
    for prefix in ('partial', 'estimated'):
        for side in ('input', 'output'):
            fields[prefix + '_' + side + '_tokens'] = evidence[prefix + '_' + side] if evidence[prefix + '_' + side + '_known'] else None
        fields[prefix + '_token_total'] = evidence[prefix + '_input'] + evidence[prefix + '_output'] if evidence[prefix + '_input_known'] or evidence[prefix + '_output_known'] else None
    return fields

def _new_entry():
    return {'requests': 0, 'input': 0, 'output': 0, 'cache': 0, 'reasoning': 0, 'cost_usd': 0.0, 'priced': 0, 'credits': 0.0, 'billable': 0, 'token_evidence': _new_token_evidence()}

def _empty_agg():
    return {'days': {}, 'undated': 0, 'unpriced': set(), 'error': None}

def _collect_jsonl_records(kind, records, models_filter, lo_iso, hi_iso):
    """Bucket raw JSONL records into per-(day, model) accumulators.

    Field extraction and cost semantics are usagesrv build_summary/_bucket
    同源 (line refs inline); the finer (day x model) granularity is the point
    of this module, not a second parsing implementation.
    """
    days = {}
    undated = 0
    unpriced = set()
    failed = 0
    last_error = None
    for rec in records:
        token_seen = False
        token_group = None
        try:
            ms = rec.get('_ms')
            if ms is None:
                ms = usagesrv._parse_ts(rec.get('time'))
            day = usagesrv._beijing_day(ms)
            if day is None:
                undated += 1
                continue
            if day < lo_iso or day > hi_iso:
                continue
            raw_model = str(rec.get('model') or 'unknown')
            if models_filter is not None and raw_model not in models_filter:
                continue
            token_group = (day, raw_model)
            in_tok = int(rec.get('input_tokens') or 0)
            out_tok = int(rec.get('output_tokens') or 0)
            cache_tok = int(rec.get('cached_input_tokens') or rec.get('cache_tokens') or 0)
            reason_tok = int(rec.get('reasoning_tokens') or 0)
            gated = False
            if gated:
                cost_usd, priced, matched = (0.0, None, None)
            else:
                cost_usd, priced, matched, _peak = usagesrv.estimate_record(raw_model, ms, in_tok, out_tok, cache_tok, reason_tok)
            key = matched or raw_model
            acc = days.setdefault(day, {}).setdefault(key, _new_entry())
            _accumulate_token_evidence(acc, rec)
            token_seen = True
            acc['requests'] += 1
            acc['input'] += in_tok
            acc['output'] += out_tok
            acc['cache'] += cache_tok
            acc['reasoning'] += reason_tok
            acc['cost_usd'] += cost_usd
            if priced:
                acc['priced'] += 1
            else:
                unpriced.add(key)
        except Exception as exc:
            if token_group is not None and (not token_seen):
                day, raw_model = token_group
                acc = days.setdefault(day, {}).setdefault(raw_model, _new_entry())
                _accumulate_token_evidence(acc, rec)
            failed += 1
            last_error = '%s: %s' % (type(exc).__name__, exc)
    error = None
    if failed:
        error = {'type': 'record_error', 'failed_records': failed, 'detail': last_error}
    return {'days': days, 'undated': undated, 'unpriced': unpriced, 'error': error}

def _emit_models(kind, entries, rate):
    """Emit per-model rows sorted by money desc then model name（排序对齐
    usagesrv models 列表 usagesrv.py:587/691，附加名称决胜保证确定性）。
    Money/credits rounding mirrors usagesrv（_round_service usagesrv.py:593-599
    / build_summary usagesrv.py:696）; parent levels emit rounded sums of
    their children to keep totals == sum(day_totals) exact up to float dust
    (see module docstring)."""
    rows = []
    for key, acc in entries.items():
        tokens = _emit_token_evidence(acc.get('token_evidence', _new_token_evidence()), kind)
        if acc['priced'] > 0:
            cost = round(acc['cost_usd'] * rate, 4)
        else:
            cost = None
        rows.append(((-(cost or 0.0), key), {'model': key, 'requests': acc['requests'], 'input_tokens': acc['input'], 'output_tokens': acc['output'], **tokens, 'cache_tokens': acc['cache'], 'reasoning_tokens': acc['reasoning'], 'estimated_cost_cny': cost}))
    rows.sort(key=lambda pair: pair[0])
    return [row for _, row in rows]

def _emit_source_totals(kind, models_out, entries):
    return {'requests': sum((m['requests'] for m in models_out)), 'input_tokens': sum((m['input_tokens'] for m in models_out)), 'output_tokens': sum((m['output_tokens'] for m in models_out)), 'cache_tokens': sum((m['cache_tokens'] for m in models_out)), 'reasoning_tokens': sum((m['reasoning_tokens'] for m in models_out)), 'estimated_cost_cny': round(sum((m['estimated_cost_cny'] or 0.0 for m in models_out)), 4)}

def _new_totals():
    return {'requests': 0, 'input_tokens': 0, 'output_tokens': 0, 'cache_tokens': 0, 'reasoning_tokens': 0, 'estimated_cost_cny': 0.0, 'credits': 0.0, 'credits_currency': CREDITS_CURRENCY}

def _add_totals(totals, other):
    for key in ('requests', 'input_tokens', 'output_tokens', 'cache_tokens', 'reasoning_tokens', 'estimated_cost_cny', 'credits'):
        value = other.get(key)
        totals[key] += 0 if value is None else value

def _normalize_totals(totals):
    totals['estimated_cost_cny'] = round(totals['estimated_cost_cny'], 4)
    totals['credits'] = round(totals['credits'], 4)
    return totals

def _load_source(source, handle, models_filter, lo_iso, hi_iso):
    """Resolve one ledger handle, read it, and bucket its records.
    Returns (agg, coverage) — coverage marks presence, missing files and
    read errors honestly; a missing file never fabricates zero-filled days."""
    kind = _SOURCE_KINDS[source]
    coverage = {'ledger_present': False, 'days_with_records': 0, 'kind': kind, 'ledger_files': []}
    try:
        reader, paths = _resolve_reader(source, handle)
    except (TypeError, ValueError) as exc:
        coverage['errors'] = [{'type': 'invalid_handle', 'detail': str(exc)}]
        return (_empty_agg(), coverage)
    if paths is not None:
        coverage['ledger_files'] = [usagesrv._display_path(p) for p in paths]
    try:
        content, diag = reader()
    except Exception as exc:
        coverage['ledger_present'] = paths is None
        coverage['errors'] = [{'type': 'reader_error', 'detail': '%s: %s' % (type(exc).__name__, exc)}]
        return (_empty_agg(), coverage)
    diag = diag or {}
    records = content
    if not isinstance(records, list):
        coverage['ledger_present'] = paths is None or bool(paths)
        coverage['errors'] = [{'type': 'invalid_reader_output', 'detail': 'expected a record list'}]
        return (_empty_agg(), coverage)
    missing = diag.get('missing_files', 0)
    coverage['ledger_present'] = paths is None or missing < len(paths)
    if missing and coverage['ledger_present']:
        coverage['errors'] = [{'type': 'missing_files', 'count': missing}]
    read_error = diag.get('read_error')
    if read_error:
        coverage.setdefault('errors', []).append({'type': 'read_error', 'detail': read_error})
    bad = diag.get('bad_lines', 0)
    if bad:
        coverage.setdefault('errors', []).append({'type': 'malformed_lines', 'bad_lines': bad})
    agg = _collect_jsonl_records(kind, records, models_filter, lo_iso, hi_iso)
    if agg['error']:
        coverage.setdefault('errors', []).append(agg['error'])
    if agg['undated']:
        coverage['undated_records'] = agg['undated']
    coverage['days_with_records'] = len(agg['days'])
    return (agg, coverage)

def bounded_stats(ledgers, date_from, date_to, sources=None, models=None):
    if not isinstance(ledgers, dict):
        raise TypeError('ledgers must be a dict of source -> ledger handle')
    lo, hi, range_days = _parse_range(date_from, date_to)
    models_filter = None if models is None else {str(m) for m in models}
    selected = sorted(set(sources) if sources is not None else set(ledgers))
    table = usagesrv.load_price_table()
    rate = table['exchangeRate']
    lo_iso, hi_iso = (lo.isoformat(), hi.isoformat())
    per_source = {}
    coverage_sources = {}
    for source in selected:
        handle = ledgers.get(source)
        kind = _SOURCE_KINDS.get(source)
        if handle is None or kind is None:
            coverage_sources[source] = {'ledger_present': False, 'days_with_records': 0, 'kind': kind, 'ledger_files': [], 'errors': [{'type': 'unknown_source' if handle is None else 'unsupported_source', 'detail': 'requested source has no usable ledger'}]}
            per_source[source] = _empty_agg()
            continue
        agg, cov = _load_source(source, handle, models_filter, lo_iso, hi_iso)
        per_source[source] = agg
        coverage_sources[source] = cov
    day_keys = sorted({day for agg in per_source.values() for day in agg['days']})
    totals = _new_totals()
    days_out = []
    for day in day_keys:
        blocks = []
        day_total = _new_totals()
        for source in sorted(per_source):
            agg = per_source[source]
            entries = agg['days'].get(day)
            if not entries:
                continue
            kind = _SOURCE_KINDS[source]
            models_out = _emit_models(kind, entries, rate)
            src_totals = _emit_source_totals(kind, models_out, entries)
            blocks.append({'source': source, 'kind': kind, 'models': models_out, 'source_totals': src_totals})
            _add_totals(day_total, src_totals)
        day_token_rows = []
        token_coverage = {key: 0 for key in ('records', 'complete_records', 'partial_records', 'estimated_records', 'unknown_records')}
        for block in blocks:
            for row in block['models']:
                for key in token_coverage:
                    token_coverage[key] += row['token_coverage'][key]
                if row['token_total'] is None:
                    continue
                day_token_rows.append({'source': block['source'], 'model': row['model'], 'input_tokens': row['complete_input_tokens'], 'output_tokens': row['complete_output_tokens'], 'token_total': row['token_total'], 'partial_token_total': row['partial_token_total'], 'estimated_token_total': row['estimated_token_total'], 'token_coverage': row['token_coverage']})
        day_token_rows.sort(key=lambda row: (-row['token_total'], row['source'], row['model']))
        days_out.append({'date': day, 'sources': blocks, 'model_token_ranking': day_token_rows, 'token_coverage': {**token_coverage, 'complete': token_coverage['records'] > 0 and token_coverage['complete_records'] == token_coverage['records']}, 'day_total': _normalize_totals(day_total)})
        _add_totals(totals, day_total)
    in_range = {d.isoformat() for d in _iter_dates(lo, hi)}
    missing_days = sorted(in_range - set(day_keys))
    unpriced = set()
    for agg in per_source.values():
        unpriced |= agg['unpriced']
    checked_at = table['meta'].get('checkedAt')
    return {'days': days_out, 'totals': _normalize_totals(totals), 'coverage': {'sources': coverage_sources, 'missing_days': missing_days, 'unpriced_models': sorted(unpriced)}, 'meta': {'timezone': TIMEZONE_NAME, 'range_days': range_days, 'price_checked_at': checked_at if isinstance(checked_at, str) else None}}
BEIJING_TZ = timezone(timedelta(hours=8))
MAX_TOKEN_RANGE_DAYS = 31
MAX_RATE_RANGE_HOURS = 168
MAX_ROWS_PER_ENDPOINT = 5000
MAX_READ_BYTES = 32 * 1024 * 1024
_SOURCE_SPECS = {'chat2api': {'kind': 'jsonl', 'time': ('time',), 'input': ('input_tokens',), 'output': ('output_tokens',), 'duration': ('duration_ms',), 'unknown_fields': [], 'time_source': '账本 time 字段（ISO 8601，带偏移或 Z）'}}
_MISSING_REASONS = ('output_only', 'no_input', 'no_tokens', 'invalid_time')
_SKIPPED_REASONS = ('missing_output_tokens', 'missing_duration', 'invalid_duration', 'invalid_time')
ERROR_TYPE_READ = 'read_error'
ERROR_TYPE_CORRUPT_LEDGER = 'corrupt_ledger'
ERROR_TYPE_MALFORMED_LINES = 'malformed_lines'
UNTRUSTED_EVENT_ERRORS = frozenset({ERROR_TYPE_READ, ERROR_TYPE_CORRUPT_LEDGER})

def _events_trusted(coverage):
    """一个来源的 coverage 说明本次事件集是否可信（单点判定，两个端点与
    usagesrv 共用）。

    errors 里出现 UNTRUSTED_EVENT_ERRORS 任一类型即不可信：该来源必须整源排除
    （days[]/hours[]/missing[]/skipped 都不出现该来源的行与计数），错误本身仍由
    coverage errors 如实披露（不静默）。无 errors 或只有 malformed_lines 时可信。
    """
    for err in coverage.get('errors') or []:
        if isinstance(err, dict) and err.get('type') in UNTRUSTED_EVENT_ERRORS:
            return False
    return True
_Event = namedtuple('_Event', 'ms model input_tokens output_tokens duration_ms duration_state status')

def default_sources():
    return {'chat2api': usagesrv.CHAT2API_LOG}
_FILE_CACHE = {}
_FILE_CACHE_LOCK = threading.Lock()

def _reset_cache_for_tests():
    """清空进程内增量读缓存（测试隔离；账本轮转/整体重读时也走这里）。"""
    with _FILE_CACHE_LOCK:
        _FILE_CACHE.clear()

def _cache_state(path):
    """只读快照：一个路径当前的缓存状态（测试与诊断用，不含可变引用）。"""
    with _FILE_CACHE_LOCK:
        state = _FILE_CACHE.get(path)
        if state is None:
            return None
        snapshot = dict(state)
        snapshot['events'] = list(state['events'])
        return snapshot

def _int_or_none(value):
    """整数化：bool 不是计数；非整数数值与不可解析字符串一律 None（不折零）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None

def _first_value(record, fields):
    """字段别名链上第一个非 null 值；全缺返回 None。"""
    for name in fields:
        if name in record and record.get(name) is not None:
            return record.get(name)
    return None

def _non_ok_status(status):
    if status is None or isinstance(status, bool):
        return False
    if isinstance(status, (int, float)):
        return not 200 <= status < 300
    if isinstance(status, str):
        text = status.strip().lower()
        if not text or text in ('ok', 'success'):
            return False
        try:
            return not 200 <= int(text) < 300
        except ValueError:
            return True
    return False

def _duration_state(record, fields):
    """(duration_ms, state)：state ∈ ok|missing|invalid。

    missing = 字段缺席或为 null；invalid = 非数值或非正值（D12-4 分母只含正耗时）。
    """
    if not fields:
        return (None, 'missing')
    raw = _first_value(record, fields)
    if raw is None:
        return (None, 'missing')
    value = _int_or_none(raw)
    if value is None or value <= 0:
        return (None, 'invalid')
    return (value, 'ok')

def _first_int(record, fields):
    """字段别名链上第一个可解释为整数的值；缺失/非数值一律 None（不折零）。"""
    for name in fields:
        if name in record:
            value = _int_or_none(record.get(name))
            if value is not None:
                return value
    return None

def _event_from_record(source, spec, record):
    """一条账本记录 → 规范化事件（字段口径见 _SOURCE_SPECS；不抛异常）。"""
    ms = usagesrv._parse_ts(_first_value(record, spec['time']))
    duration_ms, duration_state = _duration_state(record, spec['duration'])
    return _Event(ms=ms, model=str(record.get('model') or 'unknown'), input_tokens=_first_int(record, spec['input']), output_tokens=_first_int(record, spec['output']), duration_ms=duration_ms, duration_state=duration_state, status=record.get('status'))

def _refresh_jsonl_cache(path, source, spec):
    with _FILE_CACHE_LOCK:
        state = _FILE_CACHE.get(path)
        try:
            info = os.stat(path)
        except FileNotFoundError:
            _FILE_CACHE.pop(path, None)
            return (None, {'missing': True})
        except OSError as exc:
            return (state, {'read_error': str(exc)})
        size, mtime_ns = (info.st_size, info.st_mtime_ns)
        dev = getattr(info, 'st_dev', 0)
        ino = getattr(info, 'st_ino', 0)
        if state is not None and state['size'] == size and (state['mtime_ns'] == mtime_ns):
            return (state, {})
        if state is None or size < state['offset'] or state['dev'] != dev or (state['ino'] != ino) or (size == state['offset'] and state['mtime_ns'] != mtime_ns):
            state = {'offset': 0, 'size': size, 'mtime_ns': mtime_ns, 'dev': dev, 'ino': ino, 'events': [], 'bad_lines': 0, 'dropped_events': 0, 'bytes_truncated': False, 'control_records': 0, 'control_summaries': []}
        read_start = state['offset']
        bytes_truncated = state['bytes_truncated']
        head_skipped = 0
        clamped_start = size - read_start > MAX_READ_BYTES
        if clamped_start:
            read_start = size - MAX_READ_BYTES
            bytes_truncated = True
        try:
            with open(path, 'rb') as f:
                f.seek(read_start)
                blob = f.read(max(0, size - read_start))
        except OSError as exc:
            return (state, {'read_error': str(exc)})
        if clamped_start:
            cut = blob.find(b'\n')
            head_skipped = cut + 1 if cut >= 0 else len(blob)
        data = blob[head_skipped:]
        end = data.rfind(b'\n')
        if end < 0:
            consumed, new_offset = (b'', state['offset'])
        else:
            consumed = data[:end + 1]
            new_offset = read_start + head_skipped + len(consumed)
        events = state['events']
        bad_lines = state['bad_lines']
        control_records = state['control_records']
        control_summaries = state['control_summaries']
        for raw in consumed.split(b'\n'):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line.decode('utf-8'))
            except Exception:
                bad_lines += 1
                continue
            if not isinstance(record, dict):
                bad_lines += 1
                continue
            try:
                events.append(_event_from_record(source, spec, record))
            except Exception:
                bad_lines += 1
        dropped = state['dropped_events']
        if len(events) > _LEDGER_RECORD_LIMIT:
            dropped += len(events) - _LEDGER_RECORD_LIMIT
            del events[:len(events) - _LEDGER_RECORD_LIMIT]
        new_state = {'offset': new_offset, 'size': size, 'mtime_ns': mtime_ns, 'dev': dev, 'ino': ino, 'events': events, 'bad_lines': bad_lines, 'dropped_events': dropped, 'bytes_truncated': bytes_truncated, 'control_records': control_records, 'control_summaries': control_summaries}
        _FILE_CACHE[path] = new_state
        return (new_state, {})

def _load_source_events(source, handle, spec):
    """一个来源 → (events, state, diagnostics)；只读、有界、不抛异常。

    非路径句柄（未支持形状）返回空事件 + 覆盖说明，绝不猜测内容。
    """
    if not isinstance(handle, (str, os.PathLike)):
        return ([], None, {'unsupported_handle': True})
    path = os.fspath(handle)
    state, diag = _refresh_jsonl_cache(path, source, spec)
    if state is None:
        return ([], None, diag or {})
    return (state['events'], state, diag or {})

def _source_coverage(source, spec, path, events, state, diag):
    """一个来源的覆盖披露：账本在场、是否启用、坏行/截断/未知字段与计数。"""
    present = state is not None
    coverage = {'ledger': usagesrv._display_path(path), 'ledger_present': present, 'enabled': True, 'events': len(events), 'time_source': spec['time_source'], 'unknown_fields': list(spec['unknown_fields'])}
    if not present:
        coverage['enabled'] = True
        coverage['note'] = '账本文件缺席：本来源不出行，不冒充零用量'
        return coverage
    if diag.get('read_error'):
        coverage['errors'] = [{'type': ERROR_TYPE_READ, 'detail': diag['read_error']}]
    if diag.get('corrupt'):
        coverage.setdefault('errors', []).append({'type': ERROR_TYPE_CORRUPT_LEDGER, 'detail': diag['corrupt']})
    if state.get('bad_lines'):
        coverage.setdefault('errors', []).append({'type': ERROR_TYPE_MALFORMED_LINES, 'bad_lines': state['bad_lines']})
    if state.get('dropped_events'):
        coverage['dropped_events'] = state['dropped_events']
        coverage['truncated'] = True
    if state.get('bytes_truncated'):
        coverage['bytes_truncated'] = True
        coverage['truncated'] = True
    coverage['undated_records'] = sum((1 for ev in events if ev.ms is None))
    coverage['non_ok_records'] = sum((1 for ev in events if _non_ok_status(ev.status)))
    return coverage

def _unsupported_coverage(handle):
    return {'ledger': usagesrv._display_path(handle) if isinstance(handle, (str, os.PathLike)) else None, 'ledger_present': False, 'enabled': False, 'events': 0, 'time_source': None, 'unknown_fields': [], 'note': '本轮无独立账本：本来源不出行（不造别名、不合并同名）'}

def _now_ms(now=None):
    """查询基准时刻 → epoch ms：None=当前；datetime=该时刻（无时区按
    usagesrv._parse_ts 同口径视为 UTC）；数值=epoch 秒。"""
    if now is None:
        return int(time.time() * 1000)
    if isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return int(now.timestamp() * 1000)
    if isinstance(now, (int, float)):
        return int(float(now) * 1000)
    raise TypeError('now must be None, a datetime or epoch seconds')

def _clamp_int(value, default, low, high):
    """整数参数解析 + 收口到 [low, high]；返回 (值, 是否被收口)。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if number < low:
        return (low, number != low)
    if number > high:
        return (high, True)
    return (number, False)

def _window_day_keys(days, now_ms):
    """窗口内北京自然日（升序），末项=基准时刻所在日。"""
    today = datetime.fromtimestamp(now_ms / 1000, tz=BEIJING_TZ).date()
    return [(today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]

def _window_hour_starts(hours, now_ms):
    """窗口内整点（epoch ms 升序），末项=基准时刻所在整点。

    +08:00 为整小时偏移，故 UTC 整点边界即北京时间整点边界。
    """
    current = now_ms // 3600000 * 3600000
    return [current - offset * 3600000 for offset in range(hours - 1, -1, -1)]

def _hour_label(ms):
    return datetime.fromtimestamp(ms / 1000, tz=BEIJING_TZ).strftime('%Y-%m-%dT%H:00:00+08:00')

def _missing_reason(acc):
    """缺分量的原因：_MISSING_REASONS 里的三选一（互斥，见模块说明）。

    invalid_time 不由本函数产生（无时间戳的记录没有可归的日期，单独成条）。
    只对含部分请求的桶调用（complete 桶无原因可言）。

    output_only = 输入分量不完整（可能完全不可见，如国际日志）；no_input =
    输出分量不完整；no_tokens = 两个分量都不完整。
    """
    input_complete = acc['partial_input_known'] == acc['partial_requests']
    output_complete = acc['partial_output_known'] == acc['partial_requests']
    if not input_complete and (not output_complete):
        return 'no_tokens'
    if not input_complete:
        return 'output_only'
    return 'no_input'

def _empty_bucket():
    """桶累加器：完整请求与部分请求分开求和（F1 修复点）。

    complete_* 只累计「输入与输出都齐全」的请求；partial_* 只累计缺任一分量
    的请求，并按分量分别记下可展示合计与可见条数（partial_input_known /
    partial_output_known = 部分请求中该分量可见的条数）。这样一条缺分量的请求
    不会把同桶其它完整请求排除出完整总量，部分请求也不会被丢弃或补零。
    """
    return {'requests': 0, 'complete_requests': 0, 'partial_requests': 0, 'complete_input': 0, 'complete_output': 0, 'partial_input': 0, 'partial_output': 0, 'partial_input_known': 0, 'partial_output_known': 0}

def _accumulate_event(acc, ev):
    """一条规范化事件计入桶（完整/部分拆分口径见 A12-1 节端点契约）。"""
    acc['requests'] += 1
    has_input = ev.input_tokens is not None
    has_output = ev.output_tokens is not None
    if has_input and has_output:
        acc['complete_requests'] += 1
        acc['complete_input'] += ev.input_tokens
        acc['complete_output'] += ev.output_tokens
        return
    acc['partial_requests'] += 1
    if has_input:
        acc['partial_input'] += ev.input_tokens
        acc['partial_input_known'] += 1
    if has_output:
        acc['partial_output'] += ev.output_tokens
        acc['partial_output_known'] += 1

def _token_row(day, source, model, acc):
    """桶 → days[] 行（键与语义见 A12-1 节端点契约，不得改名或改义）。

    input_tokens/output_tokens 只来自「两分量都齐全」的请求，无此类请求时 null；
    partial_* 只来自缺分量的请求（绝不混入完整总量、绝不补零），完全无该分量
    时为 null（真实 0 是已知值，不是缺失）。complete ⇔ partial_requests == 0。
    """
    return {'date': day, 'source': source, 'model': model, 'input_tokens': acc['complete_input'] if acc['complete_requests'] else None, 'output_tokens': acc['complete_output'] if acc['complete_requests'] else None, 'complete': acc['partial_requests'] == 0, 'complete_requests': acc['complete_requests'], 'partial_requests': acc['partial_requests'], 'partial_input_tokens': acc['partial_input'] if acc['partial_input_known'] else None, 'partial_output_tokens': acc['partial_output'] if acc['partial_output_known'] else None}

def _missing_row(day, source, model, reason, acc):
    """桶 → missing[] 行：带该桶「部分请求」可展示的分量数字，便于
    「仅输出可见」直接显示（acc=None 表示无分量可归，如 invalid_time 行）。

    这里 input_tokens/output_tokens 是该桶缺分量请求中该分量的合计（该分量在
    部分请求里完全不可见时为 null）——与 days[] 行不同名同义，故只出现在本
    列表；完整总量一律看 days[] 行的 input_tokens/output_tokens。
    """
    if acc is None:
        input_tokens = output_tokens = None
    else:
        input_tokens = acc['partial_input'] if acc['partial_input_known'] else None
        output_tokens = acc['partial_output'] if acc['partial_output_known'] else None
    return {'date': day, 'source': source, 'model': model, 'reason': reason, 'input_tokens': input_tokens, 'output_tokens': output_tokens}

def _scan_sources(ledgers):
    """读取全部来源 → (per_source, coverage_sources)。

    per_source[source] = {"events": [...], "spec": ..., "events_trusted": bool}
    未知/无账本来源只给覆盖说明，不产出任何行。events_trusted=False 表示本次
    读取失败或结构损坏（读取层可能退回上次缓存的事件）：两个端点都必须整源排除
    该来源的行与计数（见 _events_trusted），不得把旧缓存当本次读数。
    """
    per_source, coverage_sources = ({}, {})
    for source in sorted(ledgers):
        handle = ledgers[source]
        spec = _SOURCE_SPECS.get(source)
        if spec is None or spec.get('kind') is None:
            coverage_sources[source] = _unsupported_coverage(handle)
            per_source[source] = {'events': [], 'spec': None, 'events_trusted': True}
            continue
        path = handle if isinstance(handle, (str, os.PathLike)) else None
        events, state, diag = _load_source_events(source, handle, spec)
        coverage_sources[source] = _source_coverage(source, spec, path, events, state, diag)
        per_source[source] = {'events': events, 'spec': spec, 'events_trusted': _events_trusted(coverage_sources[source])}
    return (per_source, coverage_sources)

def usage_tokens(ledgers=None, days=14, now=None):
    """近 N 个北京自然日（默认 14，上限 31）的按来源+模型 Token 分量。

    返回端点契约载荷（见 A12-1 节端点契约）。days[] 的 input_tokens/
    output_tokens 只统计「输入与输出都齐全」的请求（F1 修复点：一条缺分量的
    请求不得把同桶其它完整请求排除）；缺任一分量的请求另列进 missing，并带上
    该桶部分请求可展示的分量（partial_* / missing[] 的 input_tokens/
    output_tokens），绝不补零、绝不混入完整总量。identity 键 = source + model，
    同名跨来源不混并。无效时间戳的记录进 missing（date=null，
    reason="invalid_time"），不丢弃。days[] 与 missing[] 各自按
    MAX_ROWS_PER_ENDPOINT 截断，丢弃条数在 coverage 里如实标注。
    本次读取失败（read_error）/结构损坏（corrupt_ledger）的来源整源排除：
    days[]/missing[] 不出现其行（退回的旧缓存不是本次读数，不折 0），错误由
    coverage errors 如实披露；malformed_lines 不影响（其余事件仍可计数）。
    """
    if ledgers is None:
        ledgers = default_sources()
    window_days, clamped = _clamp_int(days, 14, 1, MAX_TOKEN_RANGE_DAYS)
    now_ms = _now_ms(now)
    day_keys = _window_day_keys(window_days, now_ms)
    window = set(day_keys)
    buckets, undated, scanned = ({}, {}, 0)
    per_source, coverage_sources = _scan_sources(ledgers)
    for source, entry in per_source.items():
        if not entry['events_trusted']:
            continue
        for ev in entry['events']:
            if ev.ms is None:
                undated.setdefault((source, ev.model), True)
                continue
            day = usagesrv._beijing_day(ev.ms)
            if day not in window:
                continue
            scanned += 1
            _accumulate_event(buckets.setdefault((day, source, ev.model), _empty_bucket()), ev)
    rows, missing = ([], [])
    for day, source, model in sorted(buckets):
        acc = buckets[day, source, model]
        rows.append(_token_row(day, source, model, acc))
        if acc['partial_requests']:
            missing.append(_missing_row(day, source, model, _missing_reason(acc), acc))
    for source, model in sorted(undated):
        missing.append(_missing_row(None, source, model, 'invalid_time', None))
    rows.sort(key=lambda r: (r['date'], r['source'], r['model']))
    missing.sort(key=lambda m: (m['date'] or '', m['source'], m['model'], m['reason']))
    dropped_rows = 0
    truncated = clamped
    if len(rows) > MAX_ROWS_PER_ENDPOINT:
        dropped_rows = len(rows) - MAX_ROWS_PER_ENDPOINT
        rows = rows[-MAX_ROWS_PER_ENDPOINT:]
        truncated = True
    dropped_missing_rows = 0
    if len(missing) > MAX_ROWS_PER_ENDPOINT:
        dropped_missing_rows = len(missing) - MAX_ROWS_PER_ENDPOINT
        missing = missing[-MAX_ROWS_PER_ENDPOINT:]
        truncated = True
    complete_rows = sum((1 for r in rows if r['complete']))
    return {'days': rows, 'missing': missing, 'coverage': {'days': window_days, 'complete_rows': complete_rows, 'partial_rows': len(rows) - complete_rows, 'scanned': scanned, 'truncated': truncated, 'clamped': clamped, 'dropped_rows': dropped_rows, 'dropped_missing_rows': dropped_missing_rows, 'sources': coverage_sources}, 'timezone': TIMEZONE_NAME, 'checked_epoch': now_ms // 1000}

def usage_rates(ledgers=None, hours=24, now=None):
    """近 N 个北京时间整点（默认 24，上限 168）的整次平均输出速率。

    返回端点契约载荷（见本节顶部）。rate = 输出 Token ÷ 整次耗时（含首字
    等待），分母只含耗时为正且输出 Token 齐全的记录；缺输出 Token、缺耗时、
    耗时非正值的记录按原因计数单列，rate 为 null 表示无法计算，绝不填 0。
    本次读取失败（read_error）/结构损坏（corrupt_ledger）的来源整源排除：
    hours[] 不出现其行、也不计入任何 skipped 原因（退回的旧缓存不是本次
    读数，不折 0），错误由 coverage errors 如实披露；malformed_lines 不影响。
    """
    if ledgers is None:
        ledgers = default_sources()
    window_hours, clamped = _clamp_int(hours, 24, 1, MAX_RATE_RANGE_HOURS)
    now_ms = _now_ms(now)
    window_start = _window_hour_starts(window_hours, now_ms)[0]
    window_end = window_start + window_hours * 3600000
    buckets = {}
    skipped = {reason: 0 for reason in _SKIPPED_REASONS}
    per_source, coverage_sources = _scan_sources(ledgers)
    for source, entry in per_source.items():
        if not entry['events_trusted']:
            continue
        for ev in entry['events']:
            if ev.ms is None:
                skipped['invalid_time'] += 1
                continue
            if ev.ms < window_start or ev.ms >= window_end:
                continue
            if ev.output_tokens is None:
                skipped['missing_output_tokens'] += 1
            if ev.duration_state == 'missing':
                skipped['missing_duration'] += 1
            elif ev.duration_state == 'invalid':
                skipped['invalid_duration'] += 1
            if ev.output_tokens is None or ev.duration_state != 'ok':
                continue
            hour = ev.ms // 3600000 * 3600000
            acc = buckets.setdefault((hour, source, ev.model), {'output_tokens': 0, 'duration_ms': 0, 'requests': 0})
            acc['output_tokens'] += ev.output_tokens
            acc['duration_ms'] += ev.duration_ms
            acc['requests'] += 1
    rows = []
    for hour, source, model in sorted(buckets):
        acc = buckets[hour, source, model]
        if acc['duration_ms'] > 0:
            rate = round(acc['output_tokens'] / (acc['duration_ms'] / 1000.0), 6)
        else:
            rate = None
        rows.append({'hour': _hour_label(hour), 'source': source, 'model': model, 'output_tokens': acc['output_tokens'], 'duration_ms': acc['duration_ms'], 'requests': acc['requests'], 'rate': rate})
    dropped_rows = 0
    truncated = clamped
    if len(rows) > MAX_ROWS_PER_ENDPOINT:
        dropped_rows = len(rows) - MAX_ROWS_PER_ENDPOINT
        rows = rows[-MAX_ROWS_PER_ENDPOINT:]
        truncated = True
    return {'hours': rows, 'skipped': skipped, 'coverage': {'hours': window_hours, 'rows': len(rows), 'truncated': truncated, 'clamped': clamped, 'dropped_rows': dropped_rows, 'sources': coverage_sources}, 'timezone': TIMEZONE_NAME, 'checked_epoch': now_ms // 1000}
