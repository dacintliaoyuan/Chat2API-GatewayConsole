# -*- coding: utf-8 -*-
import json
import os
import subprocess
import time
import model_sync
from console_operations import OperationError, OperationCancelled
HERE = os.path.dirname(os.path.abspath(__file__))
TASK_SCRIPT = os.path.join(HERE, 'manage-model-sync-task.ps1')
TASK_TIMEOUT_S = 120.0
_HISTORY_DEFAULT_LIMIT = 20
_HISTORY_MAX_LIMIT = 50
_ERROR_LIMIT = 200

def _single_line(text, limit=_ERROR_LIMIT):
    return ' '.join(str(text).split())[:limit]

def _operation_failure(code, prefix, exc):
    """Map a model_sync native exception to one single-line Chinese
    OperationError carrying the reason (exception type + message)."""
    detail = _single_line('%s: %s' % (type(exc).__name__, exc))
    return OperationError(code, '%s：%s' % (prefix, detail))

def _read_json_file(path):
    """Read a JSON snapshot file; absent -> (None, None), corrupt -> error note."""
    try:
        return (json.loads(path.read_text(encoding='utf-8')), None)
    except FileNotFoundError:
        return (None, None)
    except (OSError, ValueError) as exc:
        return (None, _single_line('%s: %s' % (type(exc).__name__, exc)))

def _summarize(result):
    """One-line Chinese summary mirroring the old console/notification wording."""
    status = str(result.get('status') or '')
    pending = result.get('pending') or []
    applied = result.get('applied') or []
    warnings = result.get('warnings') or []
    if status == 'pending':
        base = 'ZCode 运行中，%d 项等待应用' % len(pending)
    elif applied:
        base = '已应用 %d 项模型变化' % len(applied)
    elif status == 'warning':
        base = '同步完成，无应用项'
    else:
        base = '配置无变化'
    if warnings:
        base += '；警告 %d 条' % len(warnings)
    return _single_line(base)

def _scope_str_list(value, name):
    """Console params for sources/consumers: canonical shape is a list[str].

    The shared param-schema vocabulary (str/int/bool/object) has no list
    type, so the HTTP submit path encodes the array as a JSON string; the
    handler accepts both shapes and rejects everything else (fail-closed,
    subset validation happens in model_sync._normalize_scope).
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise OperationError('invalid_params', '%s 必须为字符串数组' % name) from None
    if not isinstance(value, list) or any((not isinstance(item, str) or not item.strip() for item in value)):
        raise OperationError('invalid_params', '%s 必须为字符串数组' % name)
    return value
_SYNC_MODE_ALL = 'all'
_SYNC_MODE_SELECTED = 'selected'

def _console_scope(params, *, require_mode=True):
    params = params or {}
    mode = params.get('scope_mode')
    if require_mode and mode is None:
        raise OperationError('invalid_params', '必须显式提供 scope_mode=all（全量）或 scope_mode=selected（需 sources/consumers/models 选中范围）；缺失不再默认全量')
    if mode not in (_SYNC_MODE_ALL, _SYNC_MODE_SELECTED):
        raise OperationError('invalid_params', 'scope_mode 必须为 all 或 selected')
    if mode == _SYNC_MODE_ALL:
        for key in ('sources', 'consumers', 'models'):
            if key in params:
                raise OperationError('invalid_params', 'scope_mode=all 不接受 %s（全量即启用面全集，如需限定请用 scope_mode=selected）' % key)
        return None
    scope = {}
    if 'sources' in params:
        scope['sources'] = _scope_str_list(params['sources'], 'sources')
    if 'consumers' in params:
        scope['consumers'] = _scope_str_list(params['consumers'], 'consumers')
    if 'models' in params:
        value = params['models']
        if not isinstance(value, dict):
            raise OperationError('invalid_params', 'models 必须为对象')
        for source, ids in value.items():
            if not isinstance(source, str):
                raise OperationError('invalid_params', 'models 键必须为来源名')
            if not isinstance(ids, list) or any((not isinstance(mid, str) or not mid for mid in ids)):
                raise OperationError('invalid_params', 'models[%s] 必须为非空 model_id 列表' % source)
        scope['models'] = value
    if not any(scope.values()):
        raise OperationError('invalid_params', 'scope_mode=selected 必须至少给出非空的 sources/consumers/models；空选不回退为全量')
    try:
        return model_sync._normalize_scope(scope)
    except ValueError as exc:
        raise OperationError('invalid_params', _single_line(exc)) from exc

def model_sync_preview(target, params, ctx=None):
    """仅预览同步计划（无客户端配置写入、无模型请求）。

    作用域必须显式：scope_mode=all/selected（Rev8 契约 3，语义见
    _console_scope）。selected 模式只能收窄本次计划（后端写入面限制），
    空选拒绝、不回退全量。
    """
    if ctx is not None:
        ctx.raise_if_cancelled()
    scope = _console_scope(params)
    try:
        with model_sync.SyncLock():
            result = model_sync.run_sync(apply=False, run_probes=False, confirm=False, scope=scope)
    except Exception as exc:
        raise _operation_failure('sync_failed', '模型预览失败', exc) from exc
    note = '仅预览：未写入任何客户端配置、未发送模型请求；status=%s' % (result.get('status'),)
    return {'preview': result, 'note': _single_line(note)}

def model_sync_pending(target, params, ctx=None):
    """读待处理清单与最近一次应用结果（只读，不触发同步）。

    Rev8：固定（pin）机制已移除——本读面不再返回 ``pinned`` 字段（状态文件
    经迁移升级后也不再携带该键）。
    """
    if ctx is not None:
        ctx.raise_if_cancelled()
    state = model_sync.load_state()
    last_result, last_error = _read_json_file(model_sync.RESULT_FILE)
    probes = state.get('probes')
    return {'pending': list(state.get('pending') or []), 'last_result': last_result, 'last_result_error': last_error, 'probes': {key: value for key, value in probes.items() if isinstance(key, str) and key.split('|', 1)[0] in model_sync.ACTIVE_SOURCES} if isinstance(probes, dict) else probes, 'catalog_counts': {source: count for source, count in (state.get('catalogCounts') or {}).items() if source in model_sync.ACTIVE_SOURCES}, 'last_status': state.get('lastStatus'), 'last_run': state.get('lastRun'), 'bindings': [{'source': b.source, 'consumer': b.consumer, 'provider': b.provider} for b in model_sync.ACTIVE_BINDINGS], 'paused_sources': sorted(model_sync.PAUSED_SOURCES)}

def model_catalog(target, params, ctx=None):
    if ctx is not None:
        ctx.raise_if_cancelled()
    try:
        catalogs, warnings = model_sync.collect_catalogs()
    except Exception as exc:
        raise _operation_failure('catalog_failed', '模型目录查询失败', exc) from exc
    serialized = {}
    for source, models in (catalogs or {}).items():
        serialized[source] = [{'id': model.model_id, 'display_name': model.display_name, 'context': model.context, 'context_state': 'declared' if model.context is not None else 'unknown', 'output_token_cap': {'value': 16384, 'source': 'compat_default'}, 'efforts': list(model.efforts), 'default_effort': model.default_effort, 'publish': bool(model.publish), 'efforts_source': model.efforts_source} for model in models or []]
    return {'catalogs': serialized, 'warnings': list(warnings or []), 'checked_epoch': int(time.time())}

def model_sync_history(target, params, ctx=None):
    """读最近同步历史（只读，limit 有界 1-50，默认 20，与旧入口 tail 20 同款）。"""
    if ctx is not None:
        ctx.raise_if_cancelled()
    limit = params.get('limit', _HISTORY_DEFAULT_LIMIT) if params else _HISTORY_DEFAULT_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int) or (not 1 <= limit <= _HISTORY_MAX_LIMIT):
        raise OperationError('invalid_params', 'limit 必须为 1-50 的整数')
    try:
        lines = model_sync.HISTORY_FILE.read_text(encoding='utf-8').splitlines()
    except OSError:
        lines = []
    entries = []
    skipped = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            skipped += 1
    bounded = entries[-limit:]
    return {'history': bounded, 'total': len(entries), 'skipped': skipped, 'limit': limit, 'truncated': len(entries) > len(bounded)}

def model_sync_apply(target, params, ctx=None):
    """应用同步计划：SyncLock + 备份 + 解析校验全部沿用 model_sync 既有链。

    Rev8 契约（2026-10-02）：同步与探测彻底分离——本动作不再接受 ``probe``
    参数（组合动作「应用并探测」已移除），任何 probe 传入都明确拒绝；能力/
    上下文探测走独立动作（model.context_probe_run：真实计费、独立预算/
    取消/结果持久化，不顺手写客户端配置）。普通同步从不发模型生成请求。
    作用域必须显式：scope_mode=all/selected（HTTP 缺模式拒绝；CLI/定时路径
    的全量是各自明确的内部语义）。路径、内容等无法经参数注入，守卫不可绕过。
    """
    if ctx is not None:
        ctx.raise_if_cancelled()
    params = params or {}
    if 'probe' in params:
        raise OperationError('invalid_params', '组合动作「应用并探测」已移除：应用同步从不发模型请求；能力/上下文探测请使用独立的 model.context_probe_run 动作（独立预算与结果）')
    scope = _console_scope(params)
    try:
        with model_sync.SyncLock():
            result = model_sync.run_sync(apply=True, run_probes=False, confirm=False, scope=scope)
    except Exception as exc:
        raise _operation_failure('sync_failed', '模型同步失败', exc) from exc
    return {'applied': list(result.get('applied') or []), 'pending': list(result.get('pending') or []), 'appliedDetails': list(result.get('appliedDetails') or []), 'pendingDetails': list(result.get('pendingDetails') or []), 'overrideNotices': list(result.get('overrideNotices') or []), 'summary': _summarize(result), 'status': result.get('status'), 'warnings': list(result.get('warnings') or [])}

def _decode_console_output(raw):
    """Decode PowerShell child output; console hosts may emit GBK, prefer UTF-8."""
    if not raw:
        return ''
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode('gbk', errors='replace')

def _run_task_script(action, timeout_s=TASK_TIMEOUT_S):
    """Run the existing manage-model-sync-task.ps1 (argument list, no shell)."""
    cmd = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', TASK_SCRIPT, '-Action', action]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise OperationError('task_timeout', '计划任务脚本超时（%.0f 秒）' % timeout_s) from exc
    except OSError as exc:
        raise OperationError('task_unavailable', '无法启动计划任务脚本：%s' % type(exc).__name__) from exc

def _task_lines(proc):
    text = _decode_console_output(proc.stdout)
    return [line.strip() for line in text.splitlines() if line.strip()]

def _task_detail(proc):
    raw = proc.stderr or proc.stdout
    return _single_line(_decode_console_output(raw))

def _parse_task_status(lines):
    """Best-effort parse of the script's Status output; unparseable -> None."""
    state = None
    for line in lines:
        if line.startswith('状态：'):
            state = line[len('状态：'):].strip() or None
            break
    installed = None if state is None else state != '未安装'
    return (state, installed)

def model_task_status(target, params, ctx=None):
    """计划任务安装状态（只读：脚本以 Status 动作查询，不改任务）。"""
    if ctx is not None:
        ctx.raise_if_cancelled()
    proc = _run_task_script('Status')
    lines = _task_lines(proc)
    if proc.returncode != 0:
        raise OperationError('task_failed', '计划任务状态查询失败：' + _task_detail(proc))
    state, installed = _parse_task_status(lines)
    return {'installed': installed, 'state': state, 'output': lines, 'exit_code': 0}

def model_task_install(target, params, ctx=None):
    """安装/修复每天 06:00 计划任务（真实系统写入，边界=旧控制台同款）。

    经现有 manage-model-sync-task.ps1 -Action Repair 入口（与旧控制台菜单
    第 6 项一致；脚本内 Register-ScheduledTask -Force 自身幂等）。取消仅在
    spawn 前协作检查（脚本本身不可中途取消）。结果如实返回脚本输出。
    """
    if ctx is not None:
        ctx.raise_if_cancelled()
    proc = _run_task_script('Repair')
    lines = _task_lines(proc)
    if proc.returncode != 0:
        raise OperationError('task_failed', '计划任务安装/修复失败：' + _task_detail(proc))
    state, installed = _parse_task_status(lines)
    return {'action': 'repair', 'installed': installed, 'state': state, 'output': lines, 'exit_code': 0}

def model_task_disable(target, params, ctx=None):
    """禁用每天 06:00 模型同步计划任务（脚本 Disable-ScheduledTask 真实系统写入）。

    经现有 manage-model-sync-task.ps1 -Action Disable 入口（参数列表调用，
    不经 shell）。动作当前保持 disabled（注册表 enabled=False）：每日 06:00
    目录镜像在用，启用本动作属维护者明确授权；本 handler 只做脚本结果的
    如实转发，不解析伪造成功。取消仅在 spawn 前协作检查。
    """
    if ctx is not None:
        ctx.raise_if_cancelled()
    proc = _run_task_script('Disable')
    lines = _task_lines(proc)
    if proc.returncode != 0:
        raise OperationError('task_failed', '计划任务禁用失败：' + _task_detail(proc))
    return {'disabled': True, 'output': lines, 'exit_code': 0}
_COST_NOTE = '费用未定价/无法核算：本地网关无计价依据，不编造金额'

def _int_or_none(value):
    """One integer guard for the preview's numeric record fields.

    A bool or non-int (str/float/None) is NOT trusted and reads as None
    (unknown): a stale garbage value must never be passed through raw and must
    never crash the read-only preview.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value

def model_context_probe_preview(target, params, ctx=None):
    if ctx is not None:
        ctx.raise_if_cancelled()
    params = params or {}
    selection_request, retest = _probe_run_selection(params)
    state = model_sync.load_state()
    records = state.get('contextProbes')
    if not isinstance(records, dict):
        records = {}
    try:
        selection = model_sync.resolve_probe_selection(selection_request, records)
    except ValueError as exc:
        raise OperationError('invalid_params', _single_line(exc)) from exc
    steps = list(model_sync.PROBE_INPUT_STEPS)
    entries = []
    totals = {'estimated_requests': 0, 'estimated_input_chars': 0, 'runnable_records': 0, 'blocked_records': 0, 'idle_records': 0}
    credential_readable: dict[str, bool] = {}
    for key, record in list(records.items()):
        if not isinstance(record, dict):
            continue
        status = record.get('status')
        floor = _int_or_none(record.get('observed_floor'))
        ceiling = _int_or_none(record.get('observed_ceiling'))
        attempts = _int_or_none(record.get('attempts'))
        declared = _int_or_none(record.get('declared_context'))
        prefix, _, suffix = str(key).partition('|')
        source = str(record.get('source') or prefix)
        model_id = str(record.get('model_id') or suffix)
        endpoint = model_sync.CONTEXT_PROBE_ENDPOINTS.get(source)
        probe_endpoint = endpoint is not None
        paused = source in model_sync.PAUSED_SOURCES
        if endpoint is not None and source not in credential_readable:
            credential_readable[source] = model_sync.endpoint_credential(endpoint)[2]
        executable = probe_endpoint and credential_readable.get(source, False)
        selected = selection is None or (source in selection and model_id in selection[source])
        eligible = status in ('pending', 'stale') or (status in ('verified', 'failed') and retest)
        estimate_floor = floor if status == 'pending' else None
        ladder = [step for step in steps if estimate_floor is None or step > estimate_floor]
        will_request = selected and eligible and executable and bool(ladder)
        estimated_requests = len(ladder) if will_request else 0
        input_chars_estimate = sum(ladder) if will_request else 0
        if not selected:
            estimate_note = '本轮未选中（不在请求集合），实际零请求'
            totals['idle_records'] += 1
        elif not eligible:
            estimate_note = None
            totals['idle_records'] += 1
        elif not probe_endpoint:
            estimate_note = '该来源已暂停，实际零请求' if paused else '该来源未登记探测端点，实际零请求'
            totals['blocked_records'] += 1
        elif not executable:
            estimate_note = '该来源凭据不可读（token 缺失），实际零请求'
            totals['blocked_records'] += 1
        elif not ladder:
            estimate_note = '既有实测证据已覆盖全部步进，无需新请求'
            totals['idle_records'] += 1
        else:
            estimate_note = 'retest：先归档旧代次再按全梯重测' if status in ('verified', 'failed') else None
            totals['runnable_records'] += 1
        totals['estimated_requests'] += estimated_requests
        totals['estimated_input_chars'] += input_chars_estimate
        history = record.get('history')
        known = record['declared_context_known'] if 'declared_context_known' in record else declared is not None
        scheme_revision = record.get('scheme_revision')
        if scheme_revision is None:
            scheme_revision = 1
        entries.append({'source': source, 'model_id': model_id, 'declared_context': declared, 'declared_context_known': known, 'declared_note': record.get('declared_note'), 'declared_at': record.get('declared_at'), 'status': status, 'observed_floor': floor, 'observed_ceiling': ceiling, 'attempts': attempts, 'last_run': record.get('last_run'), 'last_success': record.get('last_success'), 'last_error': record.get('last_error'), 'evidence_revision': record.get('evidence_revision'), 'stale_reason': record.get('stale_reason'), 'token_evidence': record.get('token_evidence'), 'scheme_revision': scheme_revision, 'history_count': len(history) if isinstance(history, list) else 0, 'history': [{**{field: _int_or_none(item.get(field)) for field in ('evidence_revision', 'observed_floor', 'observed_ceiling', 'token_evidence', 'attempts')}, **{field: _single_line(item[field]) if isinstance(item.get(field), str) else None for field in ('status', 'last_run', 'last_success', 'last_error', 'stale_reason', 'reason', 'archived_at')}, 'character_unit': 'chars', 'token_unit': 'tokens'} for item in (history[-model_sync.PROBE_HISTORY_MAX:] if isinstance(history, list) else []) if isinstance(item, dict)], 'probe_endpoint': probe_endpoint, 'paused': paused, 'executable': executable, 'estimate_note': estimate_note, 'input_unit': 'chars', 'output_unit': 'tokens', 'estimated_requests': estimated_requests, 'input_chars_estimate': input_chars_estimate, 'output_token_cap': model_sync.CONTEXT_PROBE_MAX_TOKENS})
    return {'records': entries, 'steps': steps, 'totals': totals, 'unit_note': '步进与 input_chars_estimate 为字符数（input_unit=chars），输出上限为 token（output_unit=tokens）；observed_floor/observed_ceiling 是历史字符步进证据，不是 token 容量；token_evidence 为某次成功请求的上游输入 Token 报告（下界证据），不是容量；0 是合法值，判断未知须用 is None（缺席即 Token 容量未知）；不做字符→token 换算；scheme_revision 缺字段=引入修订号前的祖传记录，按修订 1 解释；evidence_revision None=引入修订号前的祖传证据；declared_context 为空即声明未知（declared_context_known=False），探测仍可测量实际可用长度；记录缺席表示未知，不表示已实测或零容量', 'budget_note': '估算按固定步进表 %s（输入为字符数，不做字符→token 换算）；无探测端点或凭据不可读的来源（含已暂停）runner 零请求跳过，其记录估算为 0，totals 可核对总额；totals 三分口径 runnable+blocked+idle=records 条数：runnable=本轮会消耗请求（pending/stale 或 retest 下的 verified/failed，且有端点且凭据可读），blocked=本轮有资格但无端点或凭据不可读，idle=本轮不消耗请求（verified/failed 未 retest、未选中，或 pending 但既有证据已覆盖全梯——runner 零请求直接 verified）；全局请求预算口径：run 动作单次 max_requests ≤ 20、单请求输出 max_tokens ≤ %d；未定价来源不做金额估算' % (steps, model_sync.CONTEXT_PROBE_MAX_TOKENS), 'cost_note': _COST_NOTE, 'note': '队列在每次目录同步后播种；本预览不发请求、不写状态（凭据文件只读读一次）'}

def _probe_run_selection(params):
    """Validate the explicit probe selection params (sources/models/retest).

    Shared by the run handler AND the read-only preview — the preview must
    estimate exactly the request set the run would bill, so both reuse this
    validator.  Mirrors the sync-apply scope conventions: ``sources`` arrives
    as a list or a JSON-encoded string (the shared param-schema vocabulary
    has no list type), ``models`` as {source: [model_id, ...]}.  Any
    selection that cannot be confirmed raises OperationError("invalid_params")
    — neither handler is ever silently widened to the whole queue.  Returns
    the validated (request, retest) pair or (None, False)-shaped defaults
    when no selection param is present (legacy full-queue semantics).
    """
    sources = None
    models = None
    if params.get('sources') is not None:
        sources = _scope_str_list(params['sources'], 'sources')
    if params.get('models') is not None:
        models = params['models']
        if not isinstance(models, dict) or not models:
            raise OperationError('invalid_params', 'models 必须为非空的 {source: [model_id, ...]} 对象')
        for source, ids in models.items():
            if not isinstance(source, str) or not source:
                raise OperationError('invalid_params', 'models 键必须为非空来源名')
            if not isinstance(ids, list) or not ids or any((not isinstance(mid, str) or not mid for mid in ids)):
                raise OperationError('invalid_params', 'models[%s] 必须为非空 model_id 列表' % source)
    retest = params.get('retest', False)
    if not isinstance(retest, bool):
        raise OperationError('invalid_params', 'retest 必须为布尔值')
    try:
        request = model_sync.normalize_probe_selection(sources, models)
    except ValueError as exc:
        raise OperationError('invalid_params', _single_line(exc)) from exc
    return (request, retest)

def model_context_probe_run(target, params, ctx=None):
    if ctx is not None:
        ctx.raise_if_cancelled()
    params = params or {}
    max_requests = params.get('max_requests', 20)
    max_tokens = params.get('max_tokens', model_sync.CONTEXT_PROBE_MAX_TOKENS)
    for name, value, cap in (('max_requests', max_requests, 20), ('max_tokens', max_tokens, model_sync.CONTEXT_PROBE_MAX_TOKENS)):
        if isinstance(value, bool) or not isinstance(value, int) or (not 1 <= value <= cap):
            raise OperationError('invalid_params', '%s 必须为 1-%d 的整数' % (name, cap))
    selection_request, retest = _probe_run_selection(params)
    budget = {'max_requests': max_requests, 'max_tokens': max_tokens}
    cancel_event = getattr(ctx, 'cancel_event', None) if ctx is not None else None
    if cancel_event is not None:
        should_continue = lambda: not cancel_event.is_set()
    else:
        should_continue = lambda: True
    try:
        with model_sync.SyncLock():
            state = model_sync.load_state()
            records = state.get('contextProbes')
            if not isinstance(records, dict):
                records = {}
                state['contextProbes'] = records
            try:
                selection = model_sync.resolve_probe_selection(selection_request, records)
            except ValueError as exc:
                raise OperationError('invalid_params', _single_line(exc)) from exc
            outcome = model_sync.probe_context_queue(model_sync.CONTEXT_PROBE_ENDPOINTS, budget, records, should_continue, selection=selection, retest=retest)
            model_sync.apply_config_transaction({model_sync.STATE_FILE: json.dumps(state, ensure_ascii=False, indent=2) + '\n'}, model_sync.BACKUP_DIR, keep_backups=30)
    except OperationError:
        raise
    except Exception as exc:
        raise _operation_failure('probe_failed', '上下文探测失败', exc) from exc
    used = int(outcome.get('used') or 0)
    stopped = outcome.get('stopped')
    summary = '上下文探测结束：发送 %d 请求' % used
    if stopped:
        summary += '（%s 停止）' % stopped
    return {'requests_used': used, 'stopped': stopped, 'results': list(outcome.get('results') or []), 'retest': retest, 'selection': {source: sorted(ids) for source, ids in sorted(selection.items())} if selection is not None else None, 'summary': _single_line(summary), 'cost_note': _COST_NOTE}
_OVERRIDE_APPLY_NOTE = '覆盖已保存到 model-sync 状态；真实客户端应用须经模型同步（预览与 apply 同一口径），覆盖保存≠客户端已应用'

def _override_identity_params(params):
    """Validate shared identity params; returns (source, model_id, expected)."""
    source = params.get('source')
    model_id = params.get('model_id')
    expected = params.get('expected_set_at')
    if not isinstance(source, str) or not source.strip():
        raise OperationError('invalid_params', 'source 必须为非空字符串')
    if not isinstance(model_id, str) or not model_id.strip():
        raise OperationError('invalid_params', 'model_id 必须为非空字符串')
    if expected is not None and (not isinstance(expected, str) or not expected.strip()):
        raise OperationError('invalid_params', 'expected_set_at 必须为读面返回的 override_at 字符串或缺省（表示预期无既有覆盖）')
    return (source.strip(), model_id.strip(), expected)

def model_context_overrides(target, params, ctx=None):
    """人工 context 覆盖读面（只读）：声明、覆盖、有效同步值与来源。

    目录经既有只读 /v1/models 采集（与 set 动作的身份校验同一口径）；
    ``context_override_surface`` 覆盖当前目录模型与存量覆盖的并集，inert
    覆盖（模型已不在目录）如实显示为 effective_source="unknown"，绝不
    冒充生效。零写入。
    """
    if ctx is not None:
        ctx.raise_if_cancelled()
    try:
        catalogs, _ = model_sync.collect_catalogs()
    except ValueError as exc:
        raise OperationError('invalid_params', _single_line(exc)) from exc
    except Exception as exc:
        raise _operation_failure('catalog_failed', '目录采集失败', exc) from exc
    state = model_sync.load_state()
    try:
        consumers, _zcode_raw = model_sync.collect_consumers()
    except Exception:
        consumers = None
    note = '只读读面：declared=目录声明、override=已应用设置、effective=有效同步值、client=客户端实际读回值；覆盖保存≠客户端已应用'
    return {'overrides': model_sync.context_override_surface(state, catalogs, consumers=consumers), 'note': note}

def model_context_override_set(target, params, ctx=None):
    """设置/更新一个来源|模型的人工 context 覆盖（仅写 model-sync 状态）。

    校验：context 正整数；模型身份必须存在于当前目录（只读采集确认；
    来源目录不可读与模型不存在分开如实拒绝）；expected_set_at 版本冲突
    规则——不匹配即 version_conflict，绝不静默覆盖并发修改。有效值在
    run_sync 的唯一应用点进入镜像与探测播种；客户端写入仍走既有
    sync_apply（ZCode config 与规则库同一事务，规则库不可用整体延期）。
    """
    if ctx is not None:
        ctx.raise_if_cancelled()
    params = params or {}
    source, model_id, expected = _override_identity_params(params)
    context = params.get('context')
    if isinstance(context, bool) or not isinstance(context, int) or context < 1:
        raise OperationError('invalid_params', 'context 必须为正整数')
    try:
        catalogs, _warnings = model_sync.collect_catalogs(sources=[source])
    except ValueError as exc:
        raise OperationError('invalid_params', _single_line(exc)) from exc
    except Exception as exc:
        raise _operation_failure('catalog_failed', '目录采集失败', exc) from exc
    if source not in catalogs:
        raise OperationError('invalid_params', '来源 %s 当前目录不可用，无法确认模型身份' % source)
    try:
        with model_sync.SyncLock():
            entry = model_sync.set_context_override(catalogs, source, model_id, context, expected)
    except model_sync.ContextOverrideConflict as exc:
        raise OperationError('version_conflict', _single_line(exc)) from exc
    except ValueError as exc:
        raise OperationError('invalid_params', _single_line(exc)) from exc
    except Exception as exc:
        raise _operation_failure('override_failed', '覆盖保存失败', exc) from exc
    return {'source': source, 'model_id': model_id, 'context': entry['context'], 'set_at': entry['set_at'], 'draft_version': entry.get('draft_version'), 'note': '覆盖草稿已保存（保存≠已应用）：定时同步只消费已应用值；发布请用 model.settings_apply 提交草稿版本'}

def model_context_override_clear(target, params, ctx=None):
    """恢复默认＝删除该来源|模型的人工 context 覆盖（不删除历史证据）。

    探测记录与 history 保留原样；expected_set_at 版本规则同 set。清除不
    存在的覆盖是幂等成功（existed=False），如实返回。
    """
    if ctx is not None:
        ctx.raise_if_cancelled()
    params = params or {}
    source, model_id, expected = _override_identity_params(params)
    try:
        with model_sync.SyncLock():
            outcome = model_sync.clear_context_override(source, model_id, expected)
    except model_sync.ContextOverrideConflict as exc:
        raise OperationError('version_conflict', _single_line(exc)) from exc
    except ValueError as exc:
        raise OperationError('invalid_params', _single_line(exc)) from exc
    except Exception as exc:
        raise _operation_failure('override_failed', '覆盖清除失败', exc) from exc
    return {'source': source, 'model_id': model_id, 'cleared': True, 'existed': outcome['existed'], 'changed': outcome.get('changed', outcome['existed']), 'draft_version': outcome.get('draftVersion'), 'note': outcome.get('note') if outcome.get('changed') else '该覆盖不存在（幂等成功，零写入）；目录声明值即为当前有效值'}

def model_settings_save(target, params, ctx=None):
    """保存模型设置草稿（Rev8 契约 5/6）：只写草稿，绝不触碰客户端。

    字段：alias（1-128 字符或 None=恢复目录显示名，仅显示名，真实模型身份
    不变）、contextSource（declared/manual/measured 或 None=恢复声明）、
    contextTokens（manual/measured 必填 1..CONTEXT_OVERRIDE_MAX 正整数）、
    defaultEffort（上游明示档位之一或 None=恢复上游默认）。版本规则：
    expected_draft_version 与读面 draftVersion 不一致 → version_conflict。
    保存不触碰已应用值，不发任何模型请求。
    """
    if ctx is not None:
        ctx.raise_if_cancelled()
    params = params or {}
    source, model_id, _ = _override_identity_params(params)
    _SETTINGS_FIELDS = ('alias', 'contextSource', 'contextTokens', 'defaultEffort')
    unknown = set(params) - {'source', 'model_id', 'expected_draft_version'} - set(_SETTINGS_FIELDS)
    if unknown:
        raise OperationError('invalid_params', '未知设置字段：%s' % ','.join(sorted(unknown)))
    fields = {key: params[key] for key in _SETTINGS_FIELDS if key in params}
    try:
        catalogs, _warnings = model_sync.collect_catalogs(sources=[source])
    except ValueError as exc:
        raise OperationError('invalid_params', _single_line(exc)) from exc
    except Exception as exc:
        raise _operation_failure('catalog_failed', '目录采集失败', exc) from exc
    if source not in catalogs:
        raise OperationError('invalid_params', '来源 %s 当前目录不可用，无法确认模型身份' % source)
    try:
        with model_sync.SyncLock():
            if ctx is not None:
                ctx.raise_if_cancelled()
            record = model_sync.save_model_setting_draft(catalogs, source, model_id, fields, params.get('expected_draft_version'))
    except OperationCancelled:
        raise
    except model_sync.ContextOverrideConflict as exc:
        raise OperationError('version_conflict', _single_line(exc)) from exc
    except ValueError as exc:
        raise OperationError('invalid_params', _single_line(exc)) from exc
    except Exception as exc:
        raise _operation_failure('settings_save_failed', '设置草稿保存失败', exc) from exc
    return {'source': source, 'model_id': model_id, 'draft': record.get('draft'), 'draft_version': record.get('draftVersion'), 'draft_saved_at': record.get('draftSavedAt'), 'applied_version': record.get('appliedVersion'), 'note': '设置草稿已保存（保存≠已应用）：预览绑定 draftVersion 后用 model.settings_apply 显式应用'}

def _settings_selection(params, state):
    selection = params.get('selection')
    for field in ('source', 'model_id'):
        if field in params and (not isinstance(params[field], str) or not params[field].strip()):
            raise OperationError('invalid_params', field + ' 必须为非空字符串')
    if selection is None:
        matching = {key: version for key, version in model_sync.pending_model_setting_versions(state).items() if (params.get('source') is None or key.split('|', 1)[0] == params['source']) and (params.get('model_id') is None or key.split('|', 1)[1] == params['model_id'])}
        if not matching:
            return {}
        raise OperationError('invalid_params', '必须提交非空 selection 并先预览设置')
    if not isinstance(selection, dict) or not selection or any((not isinstance(key, str) or key.count('|') != 1 or (not all(key.split('|'))) or isinstance(version, bool) or (not isinstance(version, int)) or (version < 1) for key, version in selection.items())):
        raise OperationError('invalid_params', 'selection 必须为非空 {source|model_id: 正整数版本} 对象')
    if any((params.get('source') is not None and key.split('|', 1)[0] != params['source'] or (params.get('model_id') is not None and key.split('|', 1)[1] != params['model_id']) for key in selection)):
        raise OperationError('invalid_params', 'selection 与筛选范围不一致')
    return dict(selection)

def _model_settings_round(params, ctx, *, apply):
    params = params or {}
    if set(params) - {'selection', 'source', 'model_id', 'preview_token'}:
        raise OperationError('invalid_params', '存在未知设置应用参数')
    if ctx is not None:
        ctx.raise_if_cancelled()
    try:
        with model_sync.SyncLock():
            state = model_sync.load_state()
            selection = _settings_selection(params, state)
            if not selection:
                return {'applied': [], 'deferred': [], 'skipped': [], 'status': 'normal', 'note': '无可应用草稿（幂等成功，零写入零请求）'}
            records = model_sync._model_settings_of_state(state)
            if any((not isinstance(records.get(key), dict) or not isinstance(records[key].get('draft'), dict) or records[key].get('draftVersion') != version for key, version in selection.items())):
                raise model_sync.ContextOverrideConflict('草稿版本冲突；请重新读取并预览')
            token = params.get('preview_token')
            if apply and (not isinstance(token, str) or len(token) != 64 or any((char not in '0123456789abcdef' for char in token))):
                raise OperationError('invalid_params', '应用必须带回设置预览的 preview_token')
            models = {}
            for key in selection:
                source, model_id = key.split('|', 1)
                models.setdefault(source, []).append(model_id)
            return model_sync.run_sync(apply=apply, run_probes=False, confirm=False, scope={'sources': sorted(models), 'models': models}, state_override=state, settings_selection=selection, preview_token=token, before_publish=ctx.raise_if_cancelled if ctx is not None else None)
    except model_sync.ContextOverrideConflict as exc:
        raise OperationError('version_conflict', _single_line(exc)) from exc
    except (OperationError, OperationCancelled):
        raise
    except ValueError as exc:
        raise OperationError('invalid_params', _single_line(exc)) from exc
    except Exception as exc:
        raise _operation_failure('settings_apply_failed', '设置预览或应用失败', exc) from exc

def model_settings_preview(target, params, ctx=None):
    """Preview selected drafts using the exact publication path; no client writes."""
    result = _model_settings_round(params, ctx, apply=False)
    result['note'] = '设置草稿预览：应用时带回 selection 和 preview_token；不发模型请求'
    return result

def model_settings_apply(target, params, ctx=None):
    """Publish the version-and-input-bound preview in one complete transaction."""
    result = _model_settings_round(params, ctx, apply=True)
    if 'settingsApplied' not in result:
        return result
    applied, deferred = (result['settingsApplied'], result['settingsDeferred'])
    note = '已应用 %d 项设置（草稿已转已应用，草稿清空）' % len(applied) if not deferred else '客户端写集合整体延期：新草稿保留，原已应用值和所有客户端未变更'
    return {'applied': applied, 'deferred': deferred, 'skipped': [], 'deferred_restored': deferred, 'status': result['status'], 'warnings': result['warnings'], 'pendingDetails': result['pendingDetails'], 'selection': result['selection'], 'preview_token': result['preview_token'], 'note': _single_line(note)}
