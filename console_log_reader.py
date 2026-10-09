# -*- coding: utf-8 -*-
from __future__ import annotations
import base64
import json
import os
import re
import time
from pathlib import Path
MAX_TAIL_BYTES = 262144
MAX_ROWS = 400
MAX_LINE_BYTES = 8192
MAX_QUERY_CHARS = 120
SCAN_SECONDS = 3.0
MAX_ROTATION_INDEX = 5
_HERE = Path(__file__).resolve().parent
_TS_RE = re.compile('^(\\d{4}-\\d{2}-\\d{2}[ T]\\d{2}:\\d{2}:\\d{2}(?:[.,]\\d+)?(?:\\s*(?:[+-]\\d{2}:?\\d{2})?)?)\\s+(TRACE|DEBUG|INFO|NOTICE|WARNING|WARN|ERROR|ERR|CRITICAL|CRIT|FATAL)\\b[,:]?\\s*(.*)$', re.IGNORECASE)
_SECRET_RE = [(re.compile('(?i)\\b(cookie\\s*:\\s*).+$'), '\\1<redacted>'), (re.compile('(?i)\\b(x-admin-control|x-csrf-token|x-api-key|x-auth-token|x-session-token)\\s*:\\s*\\S+'), '\\1 <redacted>'), (re.compile('(?i)(authorization\\s*:\\s*bearer\\s+)\\S+'), '\\1<redacted>'), (re.compile('(?i)(authorization\\s*:\\s*basic\\s+)\\S+'), '\\1<redacted>'), (re.compile('(?i)\\b(api[_-]?key|apikey|password|passwd|pwd|token|secret|credential|authorization)\\b(\\s*[=:]\\s*)\\S+'), '\\1\\2<redacted>'), (re.compile('(?ix)("(?:api[_-]?key|apikey|password|passwd|pwd|token|secret|credential|cookie|authorization|access_token|refresh_token|session_token|sign|signature|code)")\\s*:\\s*"[^"]*"'), '\\1: "<redacted>"'), (re.compile('\\bsk-[A-Za-z0-9_-]{8,}\\b'), '<redacted-key>'), (re.compile('([?&](?:password|passwd|pwd|token|secret|api[_-]?key|apikey|access_token|key|code|credential)=)[^&\\s\\"\']+', re.IGNORECASE), '\\1<redacted>')]

def redact(text: str) -> str:
    """Redact common secret carriers. Applied to every line before send;
    never writes the source file."""
    for pattern, repl in _SECRET_RE:
        text = pattern.sub(repl, text)
    return text

def _file_identity(target) -> str | None:
    """Generation identity of a log file as ``ino:dev:size``.

    Taken from the OPEN handle whenever possible (an append keeps the inode
    contiguous; a same-named replacement or rotation switches the
    inode/volume — exactly the signal a stale cursor needs). Accepts an open
    binary handle, a fileno, or a Path (opened briefly, e.g. in tests).
    ``None`` when the file cannot be read.
    """
    try:
        if isinstance(target, int):
            st = os.fstat(target)
        elif hasattr(target, 'fileno'):
            st = os.fstat(target.fileno())
        else:
            with open(target, 'rb') as opened:
                st = os.fstat(opened.fileno())
        return f'{st.st_ino}:{st.st_dev}:{st.st_size}'
    except (OSError, TypeError):
        return None

def _cursor_meta(key: str | None) -> tuple[int, int, int] | None:
    """Parse a cursor file identity into (ino, dev, size); None if malformed.

    Legacy cursors (``name:size:mtime``) do not parse here, so every legacy
    cursor is re-anchored once — honest, one-time, and never silent."""
    if not isinstance(key, str):
        return None
    parts = key.rsplit(':', 2)
    if len(parts) != 3:
        return None
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None

def _registry_here() -> Path:
    return _HERE

def _decode_cursor(cursor: str) -> dict | None:
    """Decode a paging cursor; return None for anything malformed.

    A cursor is ``base64(json({"src", "cat", "off", "key"}))``. Anything that
    fails to decode, has the wrong shape, or carries a non-int / bool /
    negative offset is rejected as None so the caller answers with a reset
    hint instead of guessing continuity.
    """
    if not isinstance(cursor, str):
        return None
    try:
        raw = base64.b64decode(cursor.encode('ascii'), validate=True)
        payload = json.loads(raw.decode('utf-8'))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    src = payload.get('src')
    cat = payload.get('cat')
    off = payload.get('off')
    key = payload.get('key')
    if not isinstance(src, str) or not isinstance(cat, str):
        return None
    if isinstance(off, bool) or not isinstance(off, int) or off < 0:
        return None
    if key is not None and (not isinstance(key, str)):
        return None
    return {'src': src, 'cat': cat, 'off': off, 'key': key}

def _encode_cursor(src: str, cat: str, off: int, key: str | None) -> str:
    """Pack (source, category, offset, file identity) into an opaque cursor."""
    payload = json.dumps({'src': src, 'cat': cat, 'off': int(off), 'key': key}, ensure_ascii=True)
    return base64.b64encode(payload.encode('utf-8')).decode('ascii')

def _norm_level(raw: str) -> str:
    """Fold abbreviated log levels to their canonical names."""
    level = raw.upper()
    if level == 'WARN':
        return 'WARNING'
    if level == 'ERR':
        return 'ERROR'
    return level
_EVENT_KEYS = ('timestamp', 'model', 'request_id', 'phase', 'outcome', 'error_code', 'duration_ms', 'token_rate')

def _event_view(body: dict, ts: str | None) -> dict:
    """Structured run-event view of one ledger row (Rev8 B8-5, additive).

    字段：timestamp/model/request_id/phase/outcome/error_code/duration_ms/
    token_rate。model/request_id 先 redact 再截断（客户端可控值绝不原样透
    出）；outcome 由 status 归类（ok/HTTP2xx→ok，HTTP4xx/5xx/error→error，
    其余 None）；token_rate 仅当可信输出 token 与正耗时同时存在才算——
    输出 Token ÷ 整次耗时（含首字等待），kind 标 "mean_overall"（整次平
    均），绝不以请求排行冒充。无证据即 None（未知不是 0）。
    """
    status = body.get('status')
    outcome = None
    if isinstance(status, str) and status:
        low = status.lower()
        if low == 'ok' or low.startswith('http2'):
            outcome = 'ok'
        elif low.startswith('http4') or low.startswith('http5') or low in ('error', 'client_disconnected', 'failed'):
            outcome = 'error'
    duration = body.get('duration_ms')
    if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
        duration = None
    out_tokens = body.get('output_tokens')
    if isinstance(out_tokens, bool) or not isinstance(out_tokens, int):
        out_tokens = None
    token_rate = None
    token_rate_kind = None
    if out_tokens is not None and duration:
        token_rate = round(out_tokens / (duration / 1000), 2)
        token_rate_kind = 'mean_overall'
    model = body.get('model')
    request_id = body.get('request_id')
    return {'timestamp': ts, 'model': redact(str(model))[:64] if model is not None else None, 'request_id': redact(str(request_id))[:64] if request_id is not None else None, 'phase': body.get('phase') if isinstance(body.get('phase'), str) else None, 'outcome': outcome, 'error_code': str(status)[:32] if outcome == 'error' else None, 'duration_ms': duration, 'token_rate': token_rate, 'token_rate_kind': token_rate_kind}

def _ledger_json_line(text: str) -> dict | None:
    """Parse one structured ledger line into (ts, compact message) or None.

    Call-ledger JSONL rows ({"time": ..., "model": ..., "status": ...})
    become timestamped rows; the compact message keeps the diagnostic keys
    verbatim. Non-dict JSON or lines without a time key return None so they
    fall through to the verbatim path.  Rev8 B8-5: the parsed body rides
    along as ``event`` (structured run-event fields; redact-first, null
    when not provable).
    """
    stripped = text.strip()
    if not (stripped.startswith('{') and stripped.endswith('}')):
        return None
    try:
        body = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    ts = str(body.get('time') or body.get('ts') or '') or None
    bits = []
    for key, label in (('model', 'model'), ('status', 'status'), ('input_tokens', 'in'), ('output_tokens', 'out'), ('cached_input_tokens', 'cached'), ('reasoning_tokens', 'reasoning'), ('duration_ms', 'duration_ms'), ('credits', 'credits')):
        value = body.get(key)
        if value is None or value == '':
            continue
        bits.append(f'{label}={body[key]}')
    if not bits:
        return None
    return {'ts': ts, 'message': ' '.join(bits), 'event': _event_view(body, ts)}
_TS_TOKEN_RE = re.compile('\\brequest=(?P<request>\\S+)|\\bfinal=(?P<final>\\S+)|\\bmodel=(?P<model>\\S+)|\\bduration_ms=(?P<duration>\\d+)|\\bstatus=(?P<status>\\d{3})')

def _ts_line_event(ts: str, message: str) -> dict:
    """Light structured view of one runtime log line (Rev8 B8-5, additive).

    只提取受控键（request=/final=/model=/duration_ms=/status=），值先脱敏再
    截断；out 字段 None 时 token_rate 不存在（无法解析留 null+脱敏原文）。
    outcome 由 final= 归类：ok/HTTP200→ok，HTTP4xx/5xx→error，其余 None。
    """
    request_id = None
    model = None
    duration = None
    final = None
    status = None
    for match in _TS_TOKEN_RE.finditer(message):
        if match.group('request'):
            request_id = redact(match.group('request'))[:64]
        elif match.group('model'):
            model = redact(match.group('model'))[:64]
        elif match.group('duration'):
            try:
                value = int(match.group('duration'))
            except ValueError:
                value = None
            if value is not None and 0 < value:
                duration = value
        elif match.group('final'):
            final = match.group('final')
        elif match.group('status'):
            status = match.group('status')
    outcome = None
    error_code = None
    if final is not None:
        upper = final.upper()
        if upper == 'OK' or upper == 'HTTP200':
            outcome = 'ok'
        elif upper.startswith('HTTP4') or upper.startswith('HTTP5'):
            outcome = 'error'
            error_code = upper[:32]
    if outcome is None and status is not None:
        if status.startswith('4') or status.startswith('5'):
            outcome = 'error'
            error_code = f'HTTP{status}'
    return {'timestamp': ts, 'model': model, 'request_id': request_id, 'phase': None, 'outcome': outcome, 'error_code': error_code, 'duration_ms': duration, 'token_rate': None, 'token_rate_kind': None}

def _null_event() -> dict:
    return {key: None for key in _EVENT_KEYS}

def _outcome(status: str, error: str | None=None) -> dict:
    """Uniform shape for non-ok outcomes: empty rows, no cursor, no path."""
    out = {'status': status, 'rows': [], 'scanned_bytes': 0, 'window_start_offset': 0, 'truncated': False, 'next_cursor': None, 'reset_hint': False, 'file_key': None}
    if error is not None:
        out['error'] = error
    return out

def _find_category(source_id: str, category_id: str, ledger_paths: dict[str, str] | None=None) -> dict | None:
    """Resolve one registered (source, category) definition, or None."""
    for src_def in build_registry(ledger_paths)['sources']:
        if src_def.get('source') != source_id:
            continue
        for cat_def in src_def.get('categories', []):
            if cat_def.get('category') == category_id:
                return cat_def
        return None
    return None

def read_entries(source: str, category: str, cursor: str | None=None, q: str | None=None, level: str | None=None, since: str | None=None, until: str | None=None, rotation: int | None=None, ledger_paths: dict[str, str] | None=None, request_id: str | None=None) -> dict:
    """Read one bounded window of a registered run log.

    Tail mode (``cursor=None``) reads at most ``MAX_TAIL_BYTES`` back from
    EOF; cursor mode reads ``[max(0, off-MAX_TAIL_BYTES), off)`` behind a
    validated cursor. Every expected condition (unknown ids, missing or
    denied files, stale cursors, empty results, budget cuts) is reported
    through the ``status``/``truncated``/``reset_hint`` fields instead of
    raising. ``rows`` is always in file order (oldest first).

    ``rotation`` selects a fixed registry rotation generation (1..N for a
    rotating category, e.g. ``chat2api.log.3``); it stays a whitelist
    index, never a client path.

    ``request_id`` matches the parsed event identity exactly, within the
    same bounded window. It never expands the scan to the whole log.
    """
    if request_id is not None and (not isinstance(request_id, str) or re.fullmatch('[A-Za-z0-9_.:-]{1,64}', request_id) is None):
        return _outcome('error', error='invalid_request_id')
    category_def = _find_category(source, category, ledger_paths)
    if category_def is None:
        return _outcome('error', error='unknown_log_source')
    raw_path = category_def.get('path')
    if not raw_path:
        return _outcome('unconfigured', error='not_configured')
    path = Path(raw_path)
    rotation_cap = category_def.get('rotation_index') if isinstance(category_def.get('rotation_index'), int) else 0
    if rotation is not None:
        if not category_def.get('rotating') or not 1 <= rotation <= rotation_cap:
            return _outcome('error', error='invalid_rotation')
        rotated = Path(f'{path}.{rotation}')
        try:
            if rotated.stat().st_size > 0:
                path = rotated
        except OSError:
            pass
    try:
        handle = open(path, 'rb')
    except FileNotFoundError:
        return _outcome('missing', error='file_missing')
    except PermissionError:
        return _outcome('denied', error='permission_denied')
    except OSError as exc:
        return _outcome('error', error=f'io_error:{type(exc).__name__}')
    with handle:
        size = os.fstat(handle.fileno()).st_size
        file_key = _file_identity(handle)
        cur = None
        if cursor is not None:
            cur = _decode_cursor(cursor) if isinstance(cursor, str) else None
            stale = cur is None or cur['src'] != source or cur['cat'] != category or (cur['off'] > size)
            if not stale and cur is not None and (cur['key'] != file_key):
                minted = _cursor_meta(cur['key'])
                current = _cursor_meta(file_key)
                stale = minted is None or current is None or minted[0] != current[0] or (minted[1] != current[1]) or (current[2] < minted[2])
            if stale:
                return {'status': 'ok', 'rows': [], 'scanned_bytes': 0, 'window_start_offset': 0, 'truncated': False, 'next_cursor': None, 'reset_hint': True, 'file_key': file_key}
        older_mode = cur is not None
        off = cur['off'] if older_mode else size
        win_start = max(0, off - MAX_TAIL_BYTES)
        handle.seek(win_start)
        data = handle.read(off - win_start)
        scanned = len(data)
        if win_start > 0:
            handle.seek(win_start - 1)
            at_line_start = handle.read(1) == b'\n'
            handle.seek(win_start)
            if not at_line_start:
                nl = data.find(b'\n')
                if nl == -1:
                    return {'status': 'ok', 'rows': [], 'scanned_bytes': scanned, 'window_start_offset': win_start, 'truncated': True, 'next_cursor': None, 'reset_hint': False, 'file_key': file_key}
                win_start += nl + 1
                data = data[nl + 1:]
        parts = data.split(b'\n')
        trailing = parts.pop()
        trailing_live = bool(trailing) and (not older_mode or off >= size)
        deadline = time.monotonic() + SCAN_SECONDS
        parsed: list[dict] = []
        starts: list[int] = []
        pos = win_start
        timed_out = False
        for part in parts:
            if time.monotonic() > deadline:
                timed_out = True
                break
            cut = len(part) > MAX_LINE_BYTES
            text = (part[:MAX_LINE_BYTES] if cut else part).decode('utf-8', errors='replace')
            match = _TS_RE.match(text)
            if match:
                parsed.append({'ts': match.group(1), 'level': _norm_level(match.group(2)), 'message': redact(match.group(3)), 'parse_ok': True, 'cut': cut, 'event': _ts_line_event(match.group(1), redact(match.group(3)))})
                starts.append(pos)
            elif _ledger_json_line(text):
                row = _ledger_json_line(text)
                parsed.append({'ts': row.get('ts'), 'level': None, 'message': redact(row['message']), 'parse_ok': False, 'cut': cut, 'event': row.get('event')})
                starts.append(pos)
            elif parsed:
                parsed[-1]['message'] += '\n' + redact(text)
                if cut:
                    parsed[-1]['cut'] = True
            else:
                parsed.append({'ts': None, 'level': None, 'message': redact(text), 'parse_ok': False, 'cut': cut, 'event': {key: None for key in _EVENT_KEYS}})
                starts.append(pos)
            pos += len(part) + 1
        q_needle = (q or '')[:MAX_QUERY_CHARS].lower()
        level_needle = _norm_level((level or '').strip()[:MAX_QUERY_CHARS])
        since_needle = (since or '')[:MAX_QUERY_CHARS]
        until_needle = (until or '')[:MAX_QUERY_CHARS]
        kept: list[tuple[int, dict]] = []
        for start, row in zip(starts, parsed):
            if request_id is not None and (row.get('event') or {}).get('request_id') != request_id:
                continue
            if q_needle and q_needle not in row['message'].lower():
                continue
            if level_needle and (not row['parse_ok'] or row['level'] != level_needle):
                continue
            if since_needle and (row['ts'] is None or row['ts'] < since_needle):
                continue
            if until_needle and (row['ts'] is None or not (row['ts'].startswith(until_needle) or row['ts'] <= until_needle)):
                continue
            kept.append((start, row))
        truncated = timed_out or trailing_live
        trimmed = False
        if len(kept) > MAX_ROWS:
            kept = kept[-MAX_ROWS:]
            truncated = True
            trimmed = True
        next_cursor = None
        if older_mode:
            anchor = kept[0][0] if trimmed and kept else win_start
            if anchor > 0:
                next_cursor = _encode_cursor(source, category, anchor, file_key)
        elif win_start > 0:
            next_cursor = _encode_cursor(source, category, win_start, file_key)
        return {'status': 'ok', 'rows': [row for _, row in kept], 'scanned_bytes': scanned, 'window_start_offset': win_start, 'truncated': truncated, 'next_cursor': next_cursor, 'reset_hint': False, 'file_key': file_key, **({'request_id': request_id, 'request_scope': 'read_window'} if request_id is not None else {})}

def _share_backend_paths() -> dict[str, str]:
    """Resolve the dedicated share-backend (8110) log files.

    Path contract mirrors ``console_share_production._backend_dir``: an env
    override must be absolute, otherwise the fixed LOCALAPPDATA deployment
    directory applies. Unresolvable -> {} (empty): the source stays registered
    with no categories instead of guessing a path.
    """
    override = os.environ.get('CONSOLE_SHARE_BACKEND_DIR')
    if override:
        if not os.path.isabs(override):
            return {}
        root = override
    else:
        local = os.environ.get('LOCALAPPDATA')
        if not local:
            return {}
        root = os.path.join(local, 'AIGatewayPublic', 'share-backend')
    return {'service': os.path.join(root, 'share-backend.log'), 'ledger': os.path.join(root, 'usage-share-backend.jsonl')}

def build_registry(ledger_paths: dict[str, str] | None=None) -> dict:
    """Server-side registry: source/category ids bound to concrete files.

    ``ledger_paths`` injects the existing ledger constants from usagesrv
    ({source_id: path}); run logs are resolved here (CHAT2API_SERVICE_LOG
    env override resolved server-side and bound to the fixed id).
    """
    here = _HERE
    share_paths = _share_backend_paths()
    sources = [{'source': 'chat2api', 'display': 'chat2api 主链（8000）', 'categories': [{'category': 'service', 'display': '运行日志 chat2api.log', 'path': os.environ.get('CHAT2API_SERVICE_LOG') or str(here / 'chat2api' / 'chat2api.log'), 'rotating': True, 'rotation_index': MAX_ROTATION_INDEX, 'level_filter': True}, {'category': 'ledger', 'display': '调用账本 usage-chat2api.jsonl', 'path': (ledger_paths or {}).get('chat2api'), 'rotating': False}]}, {'source': 'console', 'display': '费用统计服务 8096（本控制台宿主）', 'categories': []}, {'source': 'share', 'display': '分享专用后端 8110（独立实例，非 8000 主链）', 'categories': [{'category': 'service', 'display': '运行日志 share-backend.log', 'path': share_paths.get('service'), 'rotating': True, 'rotation_index': MAX_ROTATION_INDEX, 'level_filter': True}, {'category': 'ledger', 'display': '调用账本 usage-share-backend.jsonl', 'path': share_paths.get('ledger'), 'rotating': False}] if share_paths else []}]
    return {'sources': sources}
