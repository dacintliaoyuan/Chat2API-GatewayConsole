# -*- coding: utf-8 -*-
import ipaddress
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
HERE = os.path.dirname(os.path.abspath(__file__))
PROTOCOL = 'openai-compatible'
PROTOCOL_PATH = '/v1'
CHAT2API_KEY_ENV = 'CHAT2API_LOCAL_API_KEY'
CHAT2API_SERVICE_LOG_ENV = 'CHAT2API_SERVICE_LOG'
CHAT2API_LOG_BACKUP_COUNT = 5
_SERVICE_LOG_MAX_BYTES = 10 * 1024 * 1024
CHAT2API_LAUNCH_FILES = ('start_chat2api.bat', 'start_chat2api_hidden.vbs', 'lib/gateway-runtime.ps1')
_LAUNCH_SCAN_MAX_BYTES = 1 << 20
_CMDLINE_TIMEOUT_S = 10.0
SERVICES = {'chat2api': {'health_path': '/health', 'protocol': PROTOCOL, 'protocol_path': PROTOCOL_PATH, 'key_kind': 'env', 'key_source': 'env:' + CHAT2API_KEY_ENV, 'key_in_use': 'startup_env', 'key_in_use_note': 'chat2api.py:121 进程导入时读取 ' + CHAT2API_KEY_ENV + '（启动后环境变更不生效）'}}
_HOST_FLAG_RE = re.compile('--host[=\\s]+(\\S+)', re.IGNORECASE)
_PORT_FLAG_RE = re.compile('--port[=\\s]+(\\S+)', re.IGNORECASE)
_HOST_FLAG_BARE_RE = re.compile('--host(\\s|$)', re.IGNORECASE)
_PORT_FLAG_BARE_RE = re.compile('--port(\\s|$)', re.IGNORECASE)
_LISTENING_RE = re.compile('^(?P<stamp>\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2},\\d{3}) INFO listening host=\\S+ port=(?P<port>\\d+) pid=(?P<pid>\\d+)(?: downstream_key=(?P<key>\\S+))?\\s*$')
_LOG_STAMP_FORMAT = '%Y-%m-%d %H:%M:%S,%f'
_CIM_DATE_RE = re.compile('/Date\\((?P<ms>\\d+)(?:[+-]\\d{4})?\\)/')

class _CommandLineUnavailable(Exception):
    """运行实例命令行读取失败；调用方降级为 unknown，绝不猜绑定形态。"""

def _service_log_path():
    """chat2api 服务日志路径：``CHAT2API_SERVICE_LOG`` 覆盖或默认
    ``chat2api/chat2api.log``（chat2api.py:95-96 的同一解析；
    console_log_reader.py:611 已有先例，不导入 chat2api 模块）。"""
    override = (os.environ.get(CHAT2API_SERVICE_LOG_ENV) or '').strip()
    if override:
        return override
    return os.path.join(HERE, 'chat2api', 'chat2api.log')

def _parse_creation_epoch(value):
    """``CreationDate``（``/Date(ms)/`` 或 ISO-8601）→ epoch 秒；读不到给 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return None
        return number / 1000.0 if number > 100000000000.0 else number
    if not isinstance(value, str):
        return None
    match = _CIM_DATE_RE.search(value)
    if match:
        return int(match.group('ms')) / 1000.0
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed.timestamp()

def _read_command_lines(pids):
    """有界只读 ``Get-CimInstance Win32_Process`` 读取。

    ``{pid: {"command_line": str | None, "created_epoch": float | None}}``；
    任何查询失败 / 输出不可解析都抛 ``_CommandLineUnavailable``（调用方按
    unknown 处理，绝不把读不到当成「没有」）。``CreationDate`` 用于 PID 复用
    守卫；只读进程表，不发信号、不写、不读进程内存。
    """
    ids = []
    for pid in pids:
        if isinstance(pid, bool) or not isinstance(pid, int):
            continue
        ids.append(int(pid))
    if not ids:
        return {}
    flt = ' OR '.join(('ProcessId=%d' % pid for pid in ids))
    script = "Get-CimInstance Win32_Process -Filter '%s' | Select-Object ProcessId,CommandLine,CreationDate | ConvertTo-Json -Compress" % flt
    try:
        proc = subprocess.run(['powershell', '-NoProfile', '-Command', script], capture_output=True, timeout=_CMDLINE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        raise _CommandLineUnavailable(type(exc).__name__)
    if proc.returncode != 0:
        raise _CommandLineUnavailable('powershell rc=%d' % proc.returncode)
    text = proc.stdout.decode('utf-8', errors='replace').strip()
    if not text:
        return {pid: None for pid in ids}
    try:
        rows = json.loads(text)
    except ValueError:
        raise _CommandLineUnavailable('non-JSON output')
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        raise _CommandLineUnavailable('unexpected shape')
    found = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row.get('ProcessId'))
        except (TypeError, ValueError):
            continue
        line = row.get('CommandLine')
        found[pid] = {'command_line': line.strip() if isinstance(line, str) and line.strip() else None, 'created_epoch': _parse_creation_epoch(row.get('CreationDate'))}
    return {pid: found.get(pid) for pid in ids}

def _is_loopback(host):
    """与 chat2api.py:662-668 ``_is_loopback_bind_host`` 同一口径。"""
    if (host or '').lower() == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

def _flag_value(command_line, value_re, bare_re):
    """(value | None, malformed) from a ``--flag`` declaration.

    A flag that appears without a readable value is malformed evidence — never
    treated as "not declared".
    """
    match = value_re.search(command_line)
    if match:
        return (match.group(1).strip('"\''), False)
    if bare_re.search(command_line):
        return (None, True)
    return (None, False)

def _chat2api_binding(command_line):
    """``(host, declared_port)`` from the running instance command line.

    ``None`` means malformed evidence (a ``--host``/``--port`` without a value):
    the caller must degrade to unknown instead of falling back to the default.
    """
    host, host_bad = _flag_value(command_line, _HOST_FLAG_RE, _HOST_FLAG_BARE_RE)
    if host_bad:
        return None
    port, port_bad = _flag_value(command_line, _PORT_FLAG_RE, _PORT_FLAG_BARE_RE)
    if port_bad:
        return None
    declared_port = None
    if port is not None:
        try:
            declared_port = int(port)
        except ValueError:
            return None
    return (host or '127.0.0.1', declared_port)

def _scan_launch_paths():
    """启动/控制台启动路径的只读扫描证据（绝不驱动状态）。

    出现变量名只说明「启动路径可能设置它」，不可读只说明扫描不完整；
    两者都不等于运行实例已设置或未设置——那只能由实例自己的记录判定。
    """
    evidence = []
    for rel in CHAT2API_LAUNCH_FILES:
        path = rel if os.path.isabs(rel) else os.path.join(HERE, rel)
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            evidence.append('启动路径 %s 不可读（%s）' % (rel, exc.__class__.__name__))
            continue
        if len(raw) >= _LAUNCH_SCAN_MAX_BYTES:
            evidence.append('启动路径 %s 超出扫描上限（%d 字节）' % (rel, _LAUNCH_SCAN_MAX_BYTES))
            continue
        text = raw.decode('utf-8', errors='replace')
        if CHAT2API_KEY_ENV in text:
            evidence.append('启动路径 %s 出现 %s（不等于实例已设置）' % (rel, CHAT2API_KEY_ENV))
        else:
            evidence.append('启动路径 %s 未出现 %s' % (rel, CHAT2API_KEY_ENV))
    return evidence

def _last_listening_record(port, pid, log_path=None):
    """实例自己的 ``listening`` 记录：跨当前日志与轮转副本找最后一条。

    匹配该实例的端口与 ``pid=``；行时间戳不早于进程创建时间的判定由调用方
    用 ``created_epoch`` 完成（PID 复用守卫）。返回
    ``{"record": {...} | None, "gap": str | None}``：记录缺失/不可读一律
    ``record=None`` 并给出缺口说明，调用方按 unknown 处理。
    """
    base = log_path or _service_log_path()
    candidates = [base] + ['%s.%d' % (base, index) for index in range(1, CHAT2API_LOG_BACKUP_COUNT + 1)]
    for path in candidates:
        try:
            if os.path.getsize(path) > 2 * _SERVICE_LOG_MAX_BYTES:
                return {'record': None, 'gap': '服务日志 %s 超出读取上限' % path}
            text = Path(path).read_text(encoding='utf-8', errors='replace')
        except FileNotFoundError:
            continue
        except OSError as exc:
            return {'record': None, 'gap': '服务日志不可读（%s，%s）' % (path, exc.__class__.__name__)}
        record = None
        for number, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.lstrip('\ufeff')
            match = _LISTENING_RE.match(line)
            if match is None:
                continue
            if int(match.group('port')) != port or int(match.group('pid')) != pid:
                continue
            try:
                stamp = datetime.strptime(match.group('stamp'), _LOG_STAMP_FORMAT)
            except ValueError:
                continue
            record = {'file': path, 'line': number, 'stamp': match.group('stamp'), 'ts_epoch': stamp.timestamp(), 'key_flag': match.group('key')}
        if record is not None:
            return {'record': record, 'gap': None}
    return {'record': None, 'gap': '服务日志中没有该实例（port=%d pid=%d）的 listening 记录' % (port, pid)}

def _snapshot_entry(snapshot, service):
    if not isinstance(snapshot, dict):
        return None
    services = snapshot.get('services')
    if not isinstance(services, list):
        return None
    for row in services:
        if isinstance(row, dict) and row.get('key') == service:
            return row
    return None

def _port_from_entry(entry):
    value = entry.get('port')
    if isinstance(value, bool):
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    if not 1 <= port <= 65535:
        return None
    return port

def _chat2api_key_state(payload, entry, cmdline_reader, env):
    pids = [pid for pid in entry.get('process_ids') or [] if isinstance(pid, int) and (not isinstance(pid, bool))]
    if payload['runtime_state'] not in ('Running', 'Degraded'):
        payload['reason'] = '运行实例无法确认：快照状态为 %r（只有运行中的实例才能确认绑定形态）；启动路径不设置 %s' % (payload['runtime_state'], CHAT2API_KEY_ENV)
        return
    if not pids:
        payload['reason'] = '运行实例无法确认：快照未给出运行中的进程号'
        return
    reader = cmdline_reader or _read_command_lines
    try:
        lines = reader(pids)
    except Exception as exc:
        payload['reason'] = '运行实例无法确认：命令行读取失败（%s）' % exc.__class__.__name__
        return
    record = None
    pid = None
    for candidate_pid in pids:
        value = lines.get(candidate_pid) if isinstance(lines, dict) else None
        if isinstance(value, str):
            value = {'command_line': value, 'created_epoch': None}
        if not isinstance(value, dict):
            continue
        command_line = value.get('command_line')
        if isinstance(command_line, str) and command_line.strip():
            record = value
            pid = candidate_pid
            break
    if record is None or pid is None:
        payload['reason'] = '运行实例无法确认：进程命令行不可读（CIM CommandLine 为空）'
        return
    line = record['command_line'].strip()
    binding = _chat2api_binding(line)
    if binding is None:
        payload['reason'] = '运行实例无法确认：命令行 --host/--port 声明缺值，形状异常'
        return
    host, declared_port = binding
    if declared_port is not None and declared_port != payload['port']:
        payload['reason'] = '运行实例无法确认：命令行 --port %s 与快照端口 %s 不一致（8110 分享实例与私有实例共用同一脚本，不能混为一谈）' % (declared_port, payload['port'])
        return
    loopback = _is_loopback(host)
    if loopback:
        if _HOST_FLAG_RE.search(line):
            payload['evidence'].append('回环绑定：运行实例命令行 --host %s' % host)
        else:
            payload['evidence'].append('回环绑定：运行实例命令行未声明 --host（chat2api.py:6323 默认 127.0.0.1）')
    else:
        payload['evidence'].append('非回环绑定：运行实例命令行 --host %s' % host)
    payload['evidence'].append('运行实例命令行：--port %d 与快照端口一致' % declared_port if declared_port is not None else '运行实例命令行：未声明 --port（chat2api.py:6324 默认 8000）')
    env_map = env if env is not None else os.environ
    if str(env_map.get(CHAT2API_KEY_ENV) or '').strip():
        payload['evidence'].append('控制台进程环境已设置 %s（值不显示；不等于实例进程环境）' % CHAT2API_KEY_ENV)
    else:
        payload['evidence'].append('控制台进程环境未设置 %s' % CHAT2API_KEY_ENV)
    payload['evidence'].extend(_scan_launch_paths())
    payload['key_in_use'] = 'startup_env'
    payload['evidence'].append('Key 使用方式：chat2api.py:121 进程导入时读取 %s（启动后环境变更不生效）' % CHAT2API_KEY_ENV)
    found = _last_listening_record(payload['port'], pid)
    if found.get('record') is None:
        payload['key_source_state'] = 'unknown'
        payload['reason'] = '运行实例无法确认鉴权要求：%s；该实例的进程级环境不可读（chat2api.py:121/:6407 在进程导入时读取自己的环境），回环绑定也可能要求 Key：不能显示「没有」，也不得表示可免鉴权' % found.get('gap')
        return
    instance = found['record']
    detail = '记录 %s:%d pid=%d %s downstream_key=%s' % (instance['file'], instance['line'], pid, instance['stamp'], instance['key_flag'])
    payload['evidence'].append('实例启动记录：' + detail)
    created = record.get('created_epoch')
    if created is None:
        payload['key_source_state'] = 'unknown'
        payload['reason'] = '运行实例无法确认鉴权要求：进程创建时间不可读，无法排除 PID 复用（%s）；不显示「没有」' % detail
        return
    if instance['ts_epoch'] < created:
        payload['key_source_state'] = 'unknown'
        payload['reason'] = '运行实例无法确认鉴权要求：启动记录时间戳 %s 早于该进程创建时间，无法排除 PID 复用（%s）；不显示「没有」' % (instance['stamp'], detail)
        return
    if instance['key_flag'] == 'not_required':
        if not loopback:
            payload['key_source_state'] = 'unknown'
            payload['reason'] = '记录与命令行矛盾：启动记录 downstream_key=not_required，但命令行绑定非回环地址 --host %s（chat2api.py:6407-6411 非回环必须携带 Key）：按未知上报，不显示「没有」' % host
            return
        payload['auth'] = 'loopback_no_key'
        payload['key_state'] = 'absent'
        payload['key_source_state'] = 'absent'
        payload['reason'] = '运行实例自己的启动记录确认下游 Key 不需要（downstream_key=not_required；chat2api.py:6407 require_local_api_key=False）：入站鉴权关闭。%s' % detail
        return
    if instance['key_flag'] == 'required':
        payload['auth'] = 'bearer'
        payload['key_state'] = 'needs_key_unconfigured'
        payload['key_source_state'] = 'unknown'
        payload['reason'] = '运行实例自己的启动记录要求下游 Key（downstream_key=required；chat2api.py:6407 require_local_api_key=True），但该实例的进程级环境不可读：需要 Key 但可绑定来源不可读（配置缺失），不得表示可免鉴权。%s' % detail
        return
    payload['key_source_state'] = 'unknown'
    payload['reason'] = '运行实例无法确认鉴权要求：启动记录缺 downstream_key 字段（旧版记录，先于该字段部署）：不显示「没有」。%s' % detail

def connection_info(service, *, snapshot=None, cmdline_reader=None, env=None, now=None):
    """选中网关的连接信息载荷；快照不可用/实例无法确认时如实 unknown。

    ``snapshot`` 是 ``usagesrv.console_services_snapshot()`` 的载荷（或 None，
    表示快照失败）。端口只从快照取；``health_path`` 是描述符常量。明文 Key
    只在 ``key_state == "present"`` 时非空。绝不发任何 HTTP 请求。
    """
    spec = SERVICES.get(service)
    if spec is None:
        raise ValueError('未知或不在范围的 service：%r' % (service,))
    checked = int((now or time.time)())
    payload = {'ok': True, 'service': service, 'display_name': None, 'port': None, 'health_path': spec['health_path'], 'health_path_source': 'descriptor', 'base_url': None, 'protocol': spec['protocol'], 'protocol_path': spec['protocol_path'], 'runtime_state': None, 'runtime_healthy': None, 'auth': 'unknown', 'key_state': 'unknown', 'key_plaintext': None, 'key_source': spec.get('key_source'), 'key_source_state': 'unknown', 'key_in_use': 'unknown', 'reason': '', 'evidence': [], 'checked_epoch': checked}
    if spec.get('alternate_protocols'):
        payload['alternate_protocols'] = [dict(item) for item in spec['alternate_protocols']]
    entry = _snapshot_entry(snapshot, service)
    if entry is None:
        payload['reason'] = '运行实例无法确认：runtime 快照不可用或缺少该服务条目；端口与 Base URL 不从显示名或描述符猜算'
        payload['evidence'].append('runtime 快照不可用（console_services_snapshot 失败或缺少 %s 条目）' % service)
        return payload
    display_name = entry.get('display_name')
    payload['display_name'] = display_name if isinstance(display_name, str) else None
    state = entry.get('state')
    payload['runtime_state'] = state if isinstance(state, str) else None
    healthy = entry.get('healthy')
    payload['runtime_healthy'] = healthy if isinstance(healthy, bool) else None
    port = _port_from_entry(entry)
    if port is None:
        payload['reason'] = '运行实例无法确认：runtime 快照条目的端口不可用（%r）；绝不猜端口' % (entry.get('port'),)
        payload['evidence'].append('runtime 快照端口不可用')
        return payload
    payload['port'] = port
    payload['base_url'] = 'http://127.0.0.1:%d%s' % (port, spec['protocol_path'])
    _chat2api_key_state(payload, entry, cmdline_reader, env)
    return payload
