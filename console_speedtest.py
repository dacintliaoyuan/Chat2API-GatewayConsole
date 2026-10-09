# -*- coding: utf-8 -*-
import http.client
import json
import os
import socket
import time
import urllib.parse
from gateway_credentials import chat2api_local_key
from console_operations import OperationCancelled, OperationError
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TOTAL_BUDGET_S = 30.0
_TOTAL_BUDGET_S = DEFAULT_TOTAL_BUDGET_S
_DEFAULT_MAX_TOKENS = 64
_MAX_TOKENS_CAP = 512
SOURCES = {'um': {'auth': None}}
BILLING_NOTE = '测速为真实计费请求；用量/费用以各来源账本为准'
_SOURCE_SERVICE_KEYS = {'um': 'chat2api'}
_CHAT_PATH = '/v1/chat/completions'
_PREVIEW_CHARS = 60

def _resolve_base_url(service_key):
    import console_adapters
    try:
        snapshot = console_adapters.service_status()
    except Exception:
        raise OperationError('status_unavailable', '无法从 runtime 快照解析 %s 服务地址' % service_key)
    services = snapshot.get('services') if isinstance(snapshot, dict) else None
    if not isinstance(services, list):
        raise OperationError('status_unavailable', '无法从 runtime 快照解析 %s 服务地址' % service_key)
    for entry in services:
        if not isinstance(entry, dict) or entry.get('key') != service_key:
            continue
        try:
            port = int(entry.get('port'))
        except (TypeError, ValueError):
            continue
        return 'http://127.0.0.1:%d' % port
    raise OperationError('status_unavailable', '无法从 runtime 快照解析 %s 服务地址' % service_key)

def _base_for_source(source):
    if source != 'um':
        raise OperationError('invalid_source', '仅支持chat2api')
    return _resolve_base_url('chat2api')

def _split_base(base_url):
    """(host, port) from a resolved base URL; malformed shape fails closed."""
    try:
        parts = urllib.parse.urlsplit(base_url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        host = port = None
    if not host or not port:
        raise OperationError('status_unavailable', '服务地址形状异常')
    return (host, port)

def _validate_params(params):
    """``{"source": <chat2api source>, "model": <1..128 chars>}`` or reject.

    The task layer validates too (defense in depth); this module never
    trusts its caller's shape.
    """
    if not isinstance(params, dict):
        raise OperationError('invalid_params', 'params must be an object')
    source = params.get('source')
    if source not in SOURCES:
        raise OperationError('invalid_params', 'source must be one of %s' % sorted(SOURCES))
    model = params.get('model')
    if not isinstance(model, str) or not 1 <= len(model) <= 128:
        raise OperationError('invalid_params', 'model must be a string of 1-128 chars')
    return (source, model)

def _single_line(text, limit=200):
    return ' '.join(str(text).split())[:limit]

def _budget_s():
    """Total streaming budget in seconds (injected via ``_TOTAL_BUDGET_S``).

    Read at call time so tests can patch the module attribute; a broken
    override degrades to the documented default instead of an unbounded or
    zero-budget probe.
    """
    try:
        value = float(_TOTAL_BUDGET_S)
    except (TypeError, ValueError):
        return DEFAULT_TOTAL_BUDGET_S
    return value if value > 0 else DEFAULT_TOTAL_BUDGET_S

def _elapsed_since(t0):
    """Monotonic delta since the send anchor; 0.0 when the request was
    never actually written (failure during connect — no streaming window
    ever opened, so an honest zero, not a fabricated duration)."""
    return time.monotonic() - t0 if t0 is not None else 0.0

def _result(source, model, status, ttfb_s, total_s, error, preview=None, usage=None):
    """Honest result dict; measured facts are never overwritten."""
    return {'source': source, 'model': model, 'status': status, 'ttfb_s': ttfb_s, 'total_s': total_s, 'first_content_preview': preview, 'error': error, 'usage': usage, 'billing_note': BILLING_NOTE}

def speedtest_model(target, params, ctx=None):
    if ctx is not None:
        ctx.raise_if_cancelled()
    source, model = _validate_params(params)
    headers = {'Content-Type': 'application/json'}
    key = chat2api_local_key()
    if key:
        headers['Authorization'] = 'Bearer ' + key
    pass
    base_url = _base_for_source(source)
    host, port = _split_base(base_url)
    max_tokens = params.get('max_tokens', _DEFAULT_MAX_TOKENS)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or (not 1 <= max_tokens <= _MAX_TOKENS_CAP):
        raise OperationError('invalid_params', 'max_tokens must be an int in 1..%d' % _MAX_TOKENS_CAP)
    body = json.dumps({'model': model, 'stream': True, 'max_tokens': max_tokens, 'messages': [{'role': 'user', 'content': 'Hi'}]}, ensure_ascii=False).encode('utf-8')
    budget = _budget_s()
    conn = http.client.HTTPConnection(host, port, timeout=budget)
    ttfb_s = None
    preview = None
    usage = None
    t0 = None
    try:
        conn.request('POST', _CHAT_PATH, body=body, headers=headers)
        t0 = time.monotonic()
        response = conn.getresponse()
        if response.status != 200:
            note = ''
            if source == 'um' and response.status == 401:
                note = '；按契约不重试：上游的 401 可能触发 chat2api 服务端 refresh（可能拉起真实浏览器登录），副作用归属上游'
            return _result(source, model, 'failed', ttfb_s=None, total_s=time.monotonic() - t0, error={'code': 'http_%d' % response.status, 'message': _single_line('上游 %s:%d 返回 HTTP %d%s' % (host, port, response.status, note))})
        sock = conn.sock
        done = False
        while not done:
            if ctx is not None:
                ctx.raise_if_cancelled()
            elapsed = time.monotonic() - t0
            if elapsed >= budget:
                return _result(source, model, 'timeout', ttfb_s=ttfb_s, total_s=time.monotonic() - t0, error=None, preview=preview, usage=usage)
            if sock is not None:
                sock.settimeout(max(budget - elapsed, 0.05))
            raw = response.readline()
            if not raw:
                return _result(source, model, 'failed', ttfb_s=ttfb_s, total_s=time.monotonic() - t0, error={'code': 'connection_closed', 'message': _single_line('服务端在流结束前关闭连接（未收到 [DONE]）：%s:%d' % (host, port))}, preview=preview, usage=usage)
            line = raw.decode('utf-8', errors='replace').strip()
            if not line or line.startswith(':'):
                continue
            if not line.startswith('data:'):
                continue
            payload = line[5:].strip()
            if payload == '[DONE]':
                break
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(chunk, dict):
                continue
            error_block = chunk.get('error')
            if error_block is not None:
                message = error_block.get('message') if isinstance(error_block, dict) else None
                return _result(source, model, 'failed', ttfb_s=ttfb_s, total_s=time.monotonic() - t0, error={'code': 'in_stream_error', 'message': _single_line(message or '流中返回未说明错误（带内 error）')}, preview=preview, usage=usage)
            usage_block = chunk.get('usage')
            if isinstance(usage_block, dict):
                usage = usage_block
            choices = chunk.get('choices')
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get('delta')
                if isinstance(delta, dict):
                    content = delta.get('content')
                    if ttfb_s is None and isinstance(content, str) and content:
                        ttfb_s = time.monotonic() - t0
                        preview = content[:_PREVIEW_CHARS]
                if choice.get('finish_reason'):
                    done = True
        return _result(source, model, 'ok', ttfb_s=ttfb_s, total_s=time.monotonic() - t0, error=None, preview=preview, usage=usage)
    except ConnectionRefusedError:
        return _result(source, model, 'failed', ttfb_s=None, total_s=_elapsed_since(t0), error={'code': 'unreachable', 'message': _single_line('连接被拒绝：%s:%d' % (host, port))})
    except (TimeoutError, socket.timeout):
        return _result(source, model, 'timeout', ttfb_s=ttfb_s, total_s=_elapsed_since(t0), error=None, preview=preview, usage=usage)
    except (http.client.RemoteDisconnected, ConnectionError) as exc:
        return _result(source, model, 'failed', ttfb_s=ttfb_s, total_s=_elapsed_since(t0), error={'code': 'unreachable', 'message': _single_line('服务端接受请求后断开连接（无 HTTP 状态码）：%s:%d，%s' % (host, port, type(exc).__name__))}, preview=preview, usage=usage)
    except http.client.HTTPException as exc:
        return _result(source, model, 'failed', ttfb_s=ttfb_s, total_s=_elapsed_since(t0), error={'code': 'connection_error', 'message': _single_line('HTTP 协议层失败：%s %s:%d' % (type(exc).__name__, host, port))}, preview=preview, usage=usage)
    except OSError as exc:
        return _result(source, model, 'failed', ttfb_s=ttfb_s, total_s=_elapsed_since(t0), error={'code': 'connection_error', 'message': _single_line('连接失败：%s %s:%d' % (type(exc).__name__, host, port))}, preview=preview, usage=usage)
    finally:
        try:
            conn.close()
        except OSError:
            pass
