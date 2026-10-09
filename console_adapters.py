# -*- coding: utf-8 -*-
import json
import os
import subprocess
import console_model_actions
import console_share_actions
import console_share_production
import console_speedtest
import console_um_adapter
import console_um_login
from console_operations import ActionSpec, OperationError, OperationUnavailable
HERE = os.path.dirname(os.path.abspath(__file__))
WRAPPER = os.path.join(HERE, 'invoke-console-runtime-json.ps1')
SERVICE_START_ENABLED_TARGETS: frozenset = frozenset({'chat2api'})
SERVICE_STOP_ENABLED_TARGETS: frozenset = frozenset({'chat2api'})
SERVICE_RESTART_ENABLED_TARGETS: frozenset = frozenset({'chat2api'})
RUNTIME_ACTIONS_DISABLED_REASON = '目标不在本项目策略允许的授权集合内（按动作+目标启用）；usage 为网页宿主自身，既有契约：停止全部排除宿主'

def runtime_target_enabled(action, target):
    """(enabled, reason) for one real service action under the policy.

    The policy is the ONLY enablement authority for real start/stop/restart:
    the target must appear in the action's explicit allow-set. A target
    outside the allow-set is refused with the policy reason (fail-closed; the
    resource function enforces this at submit time and the handler re-checks
    at execution time as defense in depth).
    """
    allowed = SERVICE_START_ENABLED_TARGETS if action == 'service.start' else SERVICE_RESTART_ENABLED_TARGETS if action == 'service.restart' else SERVICE_STOP_ENABLED_TARGETS
    if target in allowed:
        return (True, None)
    return (False, RUNTIME_ACTIONS_DISABLED_REASON)
_LOGIN_SCHEMA = {'start_wait_s': {'type': 'int', 'min': 1, 'max': 120}, 'login_budget_s': {'type': 'int', 'min': 1, 'max': 3600}, 'poll_interval_s': {'type': 'int', 'min': 1, 'max': 10}}
_SHARE_BUDGET_SCHEMA = {'poll_interval_s': {'type': 'int', 'min': 1, 'max': 10}, 'ts_confirm_budget_s': {'type': 'int', 'min': 1, 'max': 600}, 'connectivity_budget_s': {'type': 'int', 'min': 1, 'max': 600}, 'backend_budget_s': {'type': 'int', 'min': 1, 'max': 600}, 'drain_budget_s': {'type': 'int', 'min': 1, 'max': 600}, 'backend_confirm_budget_s': {'type': 'int', 'min': 1, 'max': 600}}
_SHARE_OPEN_BUDGET_SCHEMA = dict(_SHARE_BUDGET_SCHEMA, backend_auth_budget_s={'type': 'int', 'min': 1, 'max': 600})
_SHARE_RETRY_SCHEMA = dict(_SHARE_BUDGET_SCHEMA, step_interval_ms={'type': 'int', 'min': 1, 'max': 60000})
_SHARE_BACKEND_RESTART_SCHEMA = {'drain_budget_s': {'type': 'int', 'min': 1, 'max': 600}, 'poll_interval_s': {'type': 'int', 'min': 1, 'max': 10}, 'stop_budget_s': {'type': 'int', 'min': 1, 'max': 120}, 'start_budget_s': {'type': 'int', 'min': 1, 'max': 180}, 'lease_s': {'type': 'int', 'min': 60, 'max': 600}}
_SHARE_GATEWAY_RESTART_SCHEMA = {'restart_budget_s': {'type': 'int', 'min': 10, 'max': 180}}
_SPEEDTEST_SCHEMA = {'source': {'type': 'str', 'max_len': 16}, 'model': {'type': 'str', 'max_len': 128}, 'max_tokens': {'type': 'int', 'min': 1, 'max': 512}}
_MODEL_SYNC_SCOPE_SCHEMA = {'scope_mode': {'type': 'str', 'max_len': 16}, 'sources': {'type': 'str', 'max_len': 512}, 'consumers': {'type': 'str', 'max_len': 512}, 'models': {'type': 'object'}}
MODEL_TASK_DISABLE_DISABLED_REASON = '公开版本不提供从网页禁用计划任务；请使用 manage-model-sync-task.ps1 明确管理本副本的任务'

def _sanitize_detail(raw, limit=300):
    return ' '.join(str(raw or '').split())[:limit]

def _invoke_wrapper(action, key=None, timeout_s=60.0, caller_holds_lock=False):
    """Run the fixed-parameter JSON wrapper (argument list, never a shell).

    ``caller_holds_lock``: start/stop invoked by the task-layer worker while
    it already owns the resource lock — the wrapper must skip its own
    acquisition, otherwise the child process waits on its parent's exclusive
    handle until timeout (self-deadlock).
    """
    cmd = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', WRAPPER, '-Action', action]
    if key:
        cmd += ['-Key', key]
    if caller_holds_lock:
        cmd += ['-CallerHoldsLock']
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise OperationError('runtime_unavailable', 'console runtime wrapper timed out after %.0fs' % timeout_s)
    except OSError as exc:
        raise OperationError('runtime_unavailable', 'cannot run console runtime wrapper: %s' % type(exc).__name__)
    try:
        payload = json.loads(proc.stdout.decode('utf-8', errors='replace'))
    except ValueError:
        raise OperationError('runtime_unavailable', 'console runtime wrapper returned non-JSON output (rc=%d)' % proc.returncode)
    if not isinstance(payload, dict) or not payload.get('ok'):
        detail = ''
        if isinstance(payload, dict):
            detail = payload.get('detail') or payload.get('error') or ''
        raise OperationError('runtime_error' if proc.returncode == 0 else 'runtime_unavailable', _sanitize_detail(detail or proc.stderr))
    return payload

def service_status():
    """Immediate read-only snapshot of the five gateway services."""
    payload = _invoke_wrapper('status-all')
    services = payload.get('services')
    if not isinstance(services, list):
        raise OperationError('runtime_schema', 'runtime snapshot has an unexpected shape')
    return payload

class RuntimeServiceAdapter:
    """Narrow adapter used by the task layer for start/stop operations.

    Handler contract matches the task layer call ``handler(target, params,
    ctx)``. Lock ownership is one acquirer per path: the task-layer worker
    holds the cross-process resource lock for ``service:<target>`` BEFORE
    invoking this adapter, so start/stop declare ``-CallerHoldsLock`` and the
    wrapper must not open the same lock file again (non-reentrant exclusive
    handle: the child would wait on its own parent until timeout). CLI paths
    self-lock and call gateway-runtime without the wrapper; a standalone
    wrapper invocation without the switch acquires the lock itself.

    Cancel cooperation is pre-spawn only: the context is checked right before
    the subprocess call (P1 start/stop are short single subprocess calls).
    """

    def service_start(self, target, params, ctx=None):
        if ctx is not None:
            ctx.raise_if_cancelled()
        enabled, reason = runtime_target_enabled('service.start', target)
        if not enabled:
            raise OperationUnavailable('service.start', reason)
        payload = _invoke_wrapper('start', key=target, caller_holds_lock=True)
        result = payload.get('result')
        return result if isinstance(result, dict) else {'key': target}

    def service_stop(self, target, params, ctx=None):
        if ctx is not None:
            ctx.raise_if_cancelled()
        enabled, reason = runtime_target_enabled('service.stop', target)
        if not enabled:
            raise OperationUnavailable('service.stop', reason)
        payload = _invoke_wrapper('stop', key=target, caller_holds_lock=True)
        result = payload.get('result')
        return result if isinstance(result, dict) else {'key': target}

    def service_restart(self, target, params, ctx=None):
        if ctx is not None:
            ctx.raise_if_cancelled()
        enabled, reason = runtime_target_enabled('service.restart', target)
        if not enabled:
            raise OperationUnavailable('service.restart', reason)
        payload = _invoke_wrapper('restart', key=target, timeout_s=120.0, caller_holds_lock=True)
        result = payload.get('result')
        return result if isinstance(result, dict) else {'key': target}

def _refuse_unauthorized_target(action, target):
    """Submit-time policy gate for real service actions (fail-closed).

    A target outside the action's allow-set raises OperationUnavailable
    BEFORE any operation row is created (the manager calls ``resource_fn``
    during submit, so a refused target never gets queued); the handler
    re-checks at execution time as defense in depth.
    """
    enabled, reason = runtime_target_enabled(action, target)
    if not enabled:
        raise OperationUnavailable(action, reason)

def _service_start_resource(target, params):
    _refuse_unauthorized_target('service.start', target)
    return 'service:' + target

def _service_stop_resource(target, params):
    _refuse_unauthorized_target('service.stop', target)
    return 'service:' + target

def _service_restart_resource(target, params):
    _refuse_unauthorized_target('service.restart', target)
    return 'service:' + target

def _um_account_resource(target, params):
    return 'account:um'

def _speedtest_resource(target, params):
    return 'model:speedtest'

def _model_sync_resource(target, params):
    return 'model:sync'

def _model_catalog_resource(target, params):
    return 'model:catalog'

def _share_resource(target, params):
    return 'share:orchestration'

def wire_share_production_seams(keys_manager=None):
    """Bind the REAL share seams — PRODUCTION ASSEMBLY ONLY.

    console_share_production.production_seams() returns the orchestration's
    seam globals by name (broker restricted protocol, funnel command,
    connectivity observation); the TS privileged seam stays fail-closed
    until the broker whitelist extension is deployed (package section 5).
    Called from usagesrv's production console-manager assembly ONLY —
    never from build_registry (a pure factory that tests must be able to
    call without any OS-level side effect).
    """
    try:
        seams = console_share_production.production_seams(keys_manager=keys_manager)
    except Exception:
        return
    for key, value in seams.items():
        if value is not None:
            setattr(console_share_actions, key, value)

def build_registry():
    """Production registry (explicit enablement facts, never inferred).

    Real service start/stop enablement is COMPUTED from the per-action +
    per-target policy sets above: the populated sets enable the action while
    the resource function refuses every unauthorized target at submit time
    and the handler re-gates at execution time (defense in depth). No
    blanket flip; adding a target is a one-line policy-data change.
    """
    runtime = RuntimeServiceAdapter()
    start_enabled = bool(SERVICE_START_ENABLED_TARGETS)
    stop_enabled = bool(SERVICE_STOP_ENABLED_TARGETS)
    restart_enabled = bool(SERVICE_RESTART_ENABLED_TARGETS)
    return [ActionSpec('service.start', '启动一个网关服务（经 runtime 权威）', handler=runtime.service_start, resource_fn=_service_start_resource, enabled=start_enabled, disabled_reason=None if start_enabled else RUNTIME_ACTIONS_DISABLED_REASON, bump_generation_on_success=False), ActionSpec('service.stop', '停止一个网关服务（经 runtime 权威）', handler=runtime.service_stop, resource_fn=_service_stop_resource, enabled=stop_enabled, disabled_reason=None if stop_enabled else RUNTIME_ACTIONS_DISABLED_REASON, bump_generation_on_success=False), ActionSpec('service.restart', '重启一个网关服务（停止后立即重新启动；中断该服务在途工作）', handler=runtime.service_restart, resource_fn=_service_restart_resource, enabled=restart_enabled, disabled_reason=None if restart_enabled else RUNTIME_ACTIONS_DISABLED_REASON, bump_generation_on_success=True), ActionSpec('account.um_status', '上游认证状态查询（内存态只读，未主动核验）', handler=console_um_adapter.um_auth_status, resource_fn=_um_account_resource, enabled=True, bump_generation_on_success=False), ActionSpec('account.um_verify', '上游账号主动核验（向上游探测凭据有效性）', handler=console_um_adapter.um_verify_account, resource_fn=_um_account_resource, enabled=True, bump_generation_on_success=False), ActionSpec('account.login', '账号登录／重新授权（chat2api 编排）', handler=console_um_login.relogin_um, resource_fn=_um_account_resource, param_schema=_LOGIN_SCHEMA, enabled=True), ActionSpec('model.speedtest', '独立模型测速（流式短请求，首字+总耗时）', handler=console_speedtest.speedtest_model, resource_fn=_speedtest_resource, param_schema=_SPEEDTEST_SCHEMA, enabled=True, disabled_reason=None), ActionSpec('model.sync_preview', '模型同步预览（无写入）', handler=console_model_actions.model_sync_preview, resource_fn=_model_sync_resource, param_schema=_MODEL_SYNC_SCOPE_SCHEMA, enabled=True, bump_generation_on_success=False), ActionSpec('model.sync_pending', '模型同步待处理清单（只读）', handler=console_model_actions.model_sync_pending, resource_fn=_model_sync_resource, enabled=True, bump_generation_on_success=False), ActionSpec('model.sync_history', '模型同步历史（只读，有界）', handler=console_model_actions.model_sync_history, resource_fn=_model_sync_resource, param_schema={'limit': {'type': 'int', 'min': 1, 'max': 50}}, enabled=True, bump_generation_on_success=False), ActionSpec('model.task_status', '模型同步任务状态（只读）', handler=console_model_actions.model_task_status, resource_fn=_model_sync_resource, enabled=True, bump_generation_on_success=False), ActionSpec('model.sync_apply', '执行模型同步（写入受管客户端配置）', handler=console_model_actions.model_sync_apply, resource_fn=_model_sync_resource, param_schema=_MODEL_SYNC_SCOPE_SCHEMA, enabled=True), ActionSpec('model.context_probe_preview', '上下文探测队列预览（只读：队列+计划+估算，零请求零写入）', handler=console_model_actions.model_context_probe_preview, resource_fn=_model_sync_resource, param_schema={'sources': {'type': 'str', 'max_len': 512}, 'models': {'type': 'object'}, 'retest': {'type': 'bool'}}, enabled=True, bump_generation_on_success=False), ActionSpec('model.context_probe_run', '执行上下文探测队列（真实长输入计费请求，集中批准后启用；支持 sources/models 显式选择与 retest 重测）', handler=console_model_actions.model_context_probe_run, resource_fn=_model_sync_resource, param_schema={'max_requests': {'type': 'int', 'min': 1, 'max': 20}, 'max_tokens': {'type': 'int', 'min': 1, 'max': 64}, 'sources': {'type': 'str', 'max_len': 512}, 'models': {'type': 'object'}, 'retest': {'type': 'bool'}}, enabled=True, disabled_reason=None), ActionSpec('model.context_overrides', '人工 context 覆盖读面（声明/覆盖/有效值/来源；只读）', handler=console_model_actions.model_context_overrides, resource_fn=_model_sync_resource, param_schema={}, enabled=True, disabled_reason=None), ActionSpec('model.context_override_set', '设置人工 context 覆盖（仅写 model-sync 状态文件；客户端应用须经同步 apply）', handler=console_model_actions.model_context_override_set, resource_fn=_model_sync_resource, param_schema={'source': {'type': 'str', 'max_len': 128}, 'model_id': {'type': 'str', 'max_len': 128}, 'context': {'type': 'int', 'min': 1, 'max': 10000000}, 'expected_set_at': {'type': 'str', 'max_len': 64}}, enabled=True, disabled_reason=None), ActionSpec('model.context_override_clear', '恢复默认＝删除人工 context 覆盖（探测历史证据保留）', handler=console_model_actions.model_context_override_clear, resource_fn=_model_sync_resource, param_schema={'source': {'type': 'str', 'max_len': 128}, 'model_id': {'type': 'str', 'max_len': 128}, 'expected_set_at': {'type': 'str', 'max_len': 64}}, enabled=True, disabled_reason=None), ActionSpec('model.settings_save', '保存模型设置草稿（alias/context/defaultEffort；只写草稿）', handler=console_model_actions.model_settings_save, resource_fn=_model_sync_resource, param_schema={'source': {'type': 'str', 'max_len': 128}, 'model_id': {'type': 'str', 'max_len': 128}, 'alias': {'type': 'str', 'max_len': 128, 'nullable': True}, 'contextSource': {'type': 'str', 'max_len': 16, 'nullable': True}, 'contextTokens': {'type': 'int', 'min': 1, 'max': 10000000, 'nullable': True}, 'defaultEffort': {'type': 'str', 'max_len': 32, 'nullable': True}, 'expected_draft_version': {'type': 'int', 'min': 0}}, enabled=True, disabled_reason=None), ActionSpec('model.settings_preview', '预览模型设置草稿（绑定版本、证据和客户端快照；不写客户端）', handler=console_model_actions.model_settings_preview, resource_fn=_model_sync_resource, param_schema={'selection': {'type': 'object'}}, enabled=True, disabled_reason=None), ActionSpec('model.settings_apply', '应用模型设置草稿（提交草稿版本+预览绑定；ZCode 主配置与规则库同事务）', handler=console_model_actions.model_settings_apply, resource_fn=_model_sync_resource, param_schema={'selection': {'type': 'object'}, 'preview_token': {'type': 'str', 'max_len': 64}, 'source': {'type': 'str', 'max_len': 128}, 'model_id': {'type': 'str', 'max_len': 128}}, enabled=True, disabled_reason=None), ActionSpec('model.task_install', '模型同步任务安装/修复', handler=console_model_actions.model_task_install, resource_fn=_model_sync_resource, enabled=True), ActionSpec('model.catalog', '模型目录只读查询（各来源 /v1/models；无探测、无计费、无启停）', handler=console_model_actions.model_catalog, resource_fn=_model_catalog_resource, enabled=True, bump_generation_on_success=False), ActionSpec('model.task_disable', '禁用模型同步计划任务（真实系统写入）', handler=console_model_actions.model_task_disable, resource_fn=_model_sync_resource, enabled=False, disabled_reason=MODEL_TASK_DISABLE_DISABLED_REASON), ActionSpec('share.start', '上游分享开启链（TS 服务→连接→后端→Funnel）', handler=console_share_actions.share_open, resource_fn=_share_resource, param_schema=_SHARE_OPEN_BUDGET_SCHEMA, enabled=True), ActionSpec('share.stop', '上游分享正常关闭链（撤入口→排空→后端→TS）', handler=console_share_actions.share_close, resource_fn=_share_resource, param_schema=_SHARE_BUDGET_SCHEMA, enabled=True), ActionSpec('share.estop', '上游分享紧急停止（立即撤入口，不排空）', handler=console_share_actions.share_estop, resource_fn=_share_resource, param_schema=_SHARE_BUDGET_SCHEMA, enabled=True), ActionSpec('share.retry_stop', '上游分享残留关闭重试（按剩余步骤）', handler=console_share_actions.share_retry_stop, resource_fn=_share_resource, param_schema=_SHARE_RETRY_SCHEMA, enabled=True), ActionSpec('share.backend_login', '登录分享专用后端（触发独立 profile 浏览器上游登录）', handler=console_share_actions.backend_login, resource_fn=_share_resource, enabled=True), ActionSpec('share.status', '上游分享分层状态观察（只读）', handler=console_share_actions.share_status, resource_fn=_share_resource, enabled=True, bump_generation_on_success=False), ActionSpec('share.diagnostics', '上游分享八层分层诊断（只读、无副作用；八层证据含维护视图与故障留证）', handler=console_share_actions.diagnostics, resource_fn=_share_resource, param_schema={}, enabled=True, disabled_reason=None, bump_generation_on_success=False, cancelable=True), ActionSpec('share.backend_reauthenticate', '仅重新认证分享专用后端 8110（不冷启动、不重启进程、不绕过人工认证）', handler=console_share_actions.backend_reauth, resource_fn=_share_resource, param_schema={}, enabled=True, disabled_reason=None, bump_generation_on_success=False, cancelable=True), ActionSpec('share.backend_restart', '重启分享专用后端 8110（单层：暂停 8100 接收→有界排空→重启→恢复接收；不动 8100 实例与私人 8000）', handler=console_share_actions.backend_restart, resource_fn=_share_resource, param_schema=_SHARE_BACKEND_RESTART_SCHEMA, enabled=True, disabled_reason=None, bump_generation_on_success=True, cancelable=True), ActionSpec('share.gateway_restart', '重启分享入口 8100（单层：经受限 broker 宿主协议；不动 8110 实例）', handler=console_share_actions.gateway_restart, resource_fn=_share_resource, param_schema=_SHARE_GATEWAY_RESTART_SCHEMA, enabled=True, disabled_reason=None, bump_generation_on_success=True, cancelable=True)]
