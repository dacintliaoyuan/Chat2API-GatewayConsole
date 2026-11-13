# -*- coding: utf-8 -*-
"""Narrow console adapters for the unified AI-gateway console.

B0 (2026-11-01): the DEFAULT assembly is UM-only. The UM main chain is
chat2api on port 8000; the non-UM source action cards were deleted with their
modules. Their action IDs are not registered here and are not imported here
either: no handler can run for them, and a submit naming one of those IDs
reaches the operation manager as ``invalid_action``.

Production facts wired here:
* Read-only service status goes through the PowerShell JSON wrapper;
  gateway-runtime stays the single authority (no copied catalog). The status
  payload is filtered to the UM service keys only, so a retired descriptor
  that may still exist in the runtime script never surfaces here.
* ``service.start`` / ``service.stop`` enablement is COMPUTED from the
  per-action + per-target policy sets below (deployment authorization
  2026-09-16: chat2api only after B0; the usage web host stays excluded —
  stop-all must never stop the console's own process). The handler re-gates
  every target and the resource function refuses unauthorized targets at
  submit time (fail-closed, no queued operation is created for a refused
  target).
* P2 adds PURE read-only queries that are enabled because their
  side-effect freedom is proven from source (no refresh, no credential
  access): ``account.um_status`` reads chat2api's in-memory auth state
  (chat2api.py:511-517 does not touch the upstream).
* ``account.login`` is enabled per the deployment authorization package
  section 5 (approved 2026-09-16; Rev8 2026-10-02 removed the pin actions
  from this set — model.pin_add/model.pin_remove now follow the
  unknown-action contract); every action still carries its precise reason
  when disabled and fake/simulated adapters live only in the test suites.
"""

import json
import os
import subprocess
import sys
import time

from console import share_actions as console_share_actions
from console import share_production as console_share_production
from console.operations import (
    ActionSpec,
    OperationError,
    OperationUnavailable,
)

# B0 (2026-11-01): the retired source action modules were deleted with the
# UM-only assembly. This module never imports them, and the retired action IDs are
# not registered in any registry (the manager answers ``invalid_action``).
HERE = os.path.dirname(os.path.abspath(__file__))
WRAPPER = os.path.join(HERE, os.pardir, "invoke-console-runtime-json.ps1")

# Revision 5 L1 (enabled per the 2026-09-16 deployment authorization):
# per-action + per-target enablement policy. The sets below name the
# targets authorized for each real service action. Basis: 2026-09-16 user
# instruction A3 + authorization package section 1 (deployment) + the old
# console's equivalent-permission start/stop capability. "usage" is
# deliberately ABSENT from both sets: the usage web host is the process
# serving this console itself (existing contract: stop-all excludes the
# host). B0 (2026-11-01): only the UM main chain (chat2api 8000) is
# runtime-managed; no other target is in the policy and none can ever be
# started/stopped through this console.
# The policy is the only enablement authority; the handler re-checks at
# execution time and the resource function refuses unauthorized targets at
# submit time (defense in depth, fail-closed).
SERVICE_START_ENABLED_TARGETS: frozenset = frozenset({"chat2api"})
SERVICE_STOP_ENABLED_TARGETS: frozenset = frozenset({"chat2api"})
SERVICE_RESTART_ENABLED_TARGETS: frozenset = frozenset({"chat2api"})

# The status payload is read from the runtime authority (gateway-runtime) but
# only the UM service keys are surfaced: a retired descriptor that still
# exists there is never shown as an assembled service card.
LOCAL_SERVICE_KEYS: frozenset = frozenset({"chat2api"})

# Explicit cloud (Linux) role: the ONLY runtime-managed target is the UM share
# entry. The local model services do not exist there, so they are not part of the
# policy and can never be started/stopped through this console.
CLOUD_SERVICE_TARGETS: frozenset = frozenset({"share-gateway"})

# Reason reported when a target is NOT in the action's allow-set (submit-
# time refusal via the resource function and the handler's runtime gate).
RUNTIME_ACTIONS_DISABLED_REASON = (
    "目标不在部署授权包批准的授权集合内（按动作+目标启用）；"
    "usage 为网页宿主自身，既有契约：停止全部排除宿主")
CLOUD_RUNTIME_ACTIONS_DISABLED_REASON = (
    "云端角色只装配 UM 分享：运行目标只有 share-gateway；"
    "其他本地服务不在本部署授权集合内")

# Explicit cloud (Linux) role: the deployment terminates HTTPS directly, so it has
# NO Tailscale service, NO Funnel and NO Windows WFP layer. The four layered
# chain actions below are therefore registered DISABLED with this reason — an
# honest "not assembled" card (the console can show WHY) instead of a click that
# can only fail at the first TS/Funnel seam. The manager refuses every submit
# with OperationUnavailable (action_disabled) BEFORE an operation row is created,
# and no handler ever runs.
CLOUD_SHARE_ORCH_DISABLED_REASON = (
    "云端直连 HTTPS：Tailscale/Funnel 编排未装配")
CLOUD_SHARE_ORCH_ACTIONS: frozenset = frozenset({
    "share.start", "share.stop", "share.estop", "share.retry_stop"})


def runtime_target_enabled(action, target, cloud=False):
    """(enabled, reason) for one real service action under the policy.

    The policy is the ONLY enablement authority for real start/stop/restart:
    the target must appear in the action's explicit allow-set. A target
    outside the allow-set is refused with the policy reason (fail-closed; the
    resource function enforces this at submit time and the handler re-checks
    at execution time as defense in depth).

    ``cloud`` selects the explicit cloud role's policy: ONLY the UM share
    entry (``share-gateway``) is runtime-managed, and the Linux pid-file
    adapter executes the action instead of the PowerShell wrapper.
    """
    if cloud:
        if target in CLOUD_SERVICE_TARGETS:
            return True, None
        return False, CLOUD_RUNTIME_ACTIONS_DISABLED_REASON
    allowed = (SERVICE_START_ENABLED_TARGETS if action == "service.start"
               else SERVICE_RESTART_ENABLED_TARGETS if action == "service.restart"
               else SERVICE_STOP_ENABLED_TARGETS)
    if target in allowed:
        return True, None
    return False, RUNTIME_ACTIONS_DISABLED_REASON
_LOGIN_SCHEMA = {
    "start_wait_s": {"type": "int", "min": 1, "max": 120},
    "login_budget_s": {"type": "int", "min": 1, "max": 3600},
    "poll_interval_s": {"type": "int", "min": 1, "max": 10},
}
_SHARE_BUDGET_SCHEMA = {
    "poll_interval_s": {"type": "int", "min": 1, "max": 10},
    "ts_confirm_budget_s": {"type": "int", "min": 1, "max": 600},
    "connectivity_budget_s": {"type": "int", "min": 1, "max": 600},
    "backend_budget_s": {"type": "int", "min": 1, "max": 600},
    "drain_budget_s": {"type": "int", "min": 1, "max": 600},
    "backend_confirm_budget_s": {"type": "int", "min": 1, "max": 600},
}
# R13-A / D18-6: share.start publishes the bounded dedicated-backend auth
# wait budget (logging_in / unknown observations after a fresh start). The upper
# bound 1800 == the dedicated instance's own human login window (chat2api
# --login-timeout, console_share_production._BACKEND_LOGIN_TIMEOUT_S), so the
# wait can outlast the login it is waiting for. share.stop/estop never wait
# for auth, so the param stays OFF those schemas (no dead knob).
_SHARE_OPEN_BUDGET_SCHEMA = dict(
    _SHARE_BUDGET_SCHEMA,
    backend_auth_budget_s={"type": "int", "min": 1, "max": 1800})
_SHARE_RETRY_SCHEMA = dict(_SHARE_BUDGET_SCHEMA,
                           step_interval_ms={"type": "int", "min": 1,
                                             "max": 60000})
# Rev9 A9-3/A9-4 (contract §1.3): the two single-layer restart schemas,
# bounded ints; defaults live in the handlers (缺省即默认).  lease_s upper
# bound 600 == protocol MAINTENANCE_LEASE_MAX_S; restart_budget_s upper bound
# 180 == protocol GATEWAY_RESTART_BUDGET_MAX_S.
# share.gateway_restart publishes NO poll_interval_s: §6.1/§6.3 freeze the
# broker seam call to the budget only and §6.2/the wire give the broker's
# internal poll cadence no field, so the console would only be offering a knob
# that cannot take effect (no dead knob).
_SHARE_BACKEND_RESTART_SCHEMA = {
    "drain_budget_s": {"type": "int", "min": 1, "max": 600},
    "poll_interval_s": {"type": "int", "min": 1, "max": 10},
    "stop_budget_s": {"type": "int", "min": 1, "max": 120},
    "start_budget_s": {"type": "int", "min": 1, "max": 180},
    "lease_s": {"type": "int", "min": 60, "max": 600},
}
_SHARE_GATEWAY_RESTART_SCHEMA = {
    "restart_budget_s": {"type": "int", "min": 10, "max": 180},
}


UNWIRED_REASON = UNWIRED_REASON = "功能未接线（后续阶段）；当前版本明确不提供该动作"


SHARE_ORCH_DISABLED_REASON = (
    "分享编排已实现且生产适配器已接线（broker 受限协议/Funnel 命令/连接观察；"
    "TS 特权层经 broker 白名单扩展部署后开放——部署授权包节 5）；真实启停属"
    "真实特权动作，批准后按启用策略开放")


def _sanitize_detail(raw, limit=300):
    return " ".join(str(raw or "").split())[:limit]


def _invoke_wrapper(action, key=None, timeout_s=60.0, caller_holds_lock=False):
    """Run the fixed-parameter JSON wrapper (argument list, never a shell).

    ``caller_holds_lock``: start/stop invoked by the task-layer worker while
    it already owns the resource lock — the wrapper must skip its own
    acquisition, otherwise the child process waits on its parent's exclusive
    handle until timeout (self-deadlock).
    """
    cmd = ["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", WRAPPER, "-Action", action]
    if key:
        cmd += ["-Key", key]
    if caller_holds_lock:
        cmd += ["-CallerHoldsLock"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise OperationError(
            "runtime_unavailable",
            "console runtime wrapper timed out after %.0fs" % timeout_s)
    except OSError as exc:
        raise OperationError(
            "runtime_unavailable",
            "cannot run console runtime wrapper: %s" % type(exc).__name__)
    try:
        payload = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        raise OperationError(
            "runtime_unavailable",
            "console runtime wrapper returned non-JSON output (rc=%d)"
            % proc.returncode)
    if not isinstance(payload, dict) or not payload.get("ok"):
        detail = ""
        if isinstance(payload, dict):
            detail = payload.get("detail") or payload.get("error") or ""
        raise OperationError(
            "runtime_error" if proc.returncode == 0 else "runtime_unavailable",
            _sanitize_detail(detail or proc.stderr))
    return payload


def service_status(cloud=False, ops=None):
    """Immediate read-only snapshot of the gateway services.

    Windows/local: the PowerShell JSON wrapper is the single runtime authority.
    Cloud role: ONLY the UM share entry is read, from the Linux pid-file host
    ops (no PowerShell/tasklist/netstat is ever called).
    """
    if cloud:
        return cloud_service_status(ops)
    payload = _invoke_wrapper("status-all")
    services = payload.get("services")
    if not isinstance(services, list):
        raise OperationError(
            "runtime_schema", "runtime snapshot has an unexpected shape")
    # B0: surface ONLY the UM service keys. Any other descriptor key is not
    # part of the default assembly and must not be presented as a manageable
    # service card here.
    filtered = [service for service in services
                if isinstance(service, dict)
                and service.get("key") in LOCAL_SERVICE_KEYS]
    return dict(payload, services=filtered)


# ------------------------------------------------- explicit cloud role (Linux)
CLOUD_GATEWAY_TARGET = "share-gateway"
CLOUD_GATEWAY_PID_FILE_ENV = "UM_SHARE_GATEWAY_PID_FILE"
CLOUD_GATEWAY_STATE_DIR_ENV = "UM_SHARE_HOST_STATE_DIR"
CLOUD_GATEWAY_PORT_ENV = "UM_SHARE_GATEWAY_PORT"
CLOUD_GATEWAY_BACKEND_PORT_ENV = "UM_SHARE_BACKEND_PORT"
DEFAULT_CLOUD_GATEWAY_PID_FILE = "/run/um-share/share-gateway.pid"
DEFAULT_CLOUD_GATEWAY_STATE_DIR = "/run/um-share"
DEFAULT_CLOUD_GATEWAY_PORT = 8100
DEFAULT_CLOUD_BACKEND_PORT = 8110


def _strict_port(name, default):
    """Strict port env: digits only, 1..65535; anything else refuses."""
    raw = os.environ.get(name)
    if raw is None:
        return int(default)
    text = str(raw).strip()
    if not text or not text.isascii() or not text.isdigit():
        raise ValueError("%s must be an integer port" % name)
    port = int(text)
    if not 1 <= port <= 65535:
        raise ValueError("%s must be within 1..65535" % name)
    return port


def _cloud_gateway_port():
    return _strict_port(CLOUD_GATEWAY_PORT_ENV, DEFAULT_CLOUD_GATEWAY_PORT)


def _cloud_host_ops():
    """The Linux pid-file host ops for the cloud role (READ-ONLY face).

    The console unit intentionally mounts ``/run/um-share`` read-only: this
    object may read the pid file/status and the ``/proc`` identity, but it must
    never be the writer of the pid file or the stop intent. Every mutation goes
    through ``_BrokerCloudHostOps`` below, i.e. through the broker process
    that OWNS the state dir.
    """
    from share.broker_ops import LinuxHostOps
    gateway_port = _cloud_gateway_port()
    script = os.path.join(HERE, os.pardir, "chat2api", "chat2api.py")
    return LinuxHostOps(
        os.environ.get(CLOUD_GATEWAY_PID_FILE_ENV)
        or DEFAULT_CLOUD_GATEWAY_PID_FILE,
        [sys.executable, script, "--port", str(gateway_port),
         "--share-config",
         os.environ.get("UM_SHARE_PROJECTION_FILE",
                        "/var/lib/um-share/run/share-projection.json"),
         "--auth-mode", "projection"],
        state_dir=os.environ.get(CLOUD_GATEWAY_STATE_DIR_ENV)
        or DEFAULT_CLOUD_GATEWAY_STATE_DIR,
        expected_port=gateway_port)


def _default_cloud_broker():
    """The deployment's restricted broker adapter, resolved lazily.

    ``console_share_production._default_broker`` is the ONE deployment loader
    (the same object the console share actions use); it raises an honest
    OperationError when the deployment config is absent/disabled. Resolving it
    here (handler call time, never registry build time) keeps
    ``build_cloud_registry`` a pure factory.
    """
    from console.share_production import _default_broker
    return _default_broker()


class _BrokerCloudHostOps:
    """Cloud host ops with ONE writer: mutations go through the broker.

    The console unit's ``ReadOnlyPaths=/run/um-share`` is a deliberate
    single-authority fence: the console may READ the pid file/status, but the
    broker process OWNS the pid file, the persistent stop intent and the only
    ``Popen`` of ``chat2api.py``. Routing ``service.start`` /
    ``service.stop`` / ``service.restart`` through the existing restricted
    broker verbs keeps that boundary intact instead of widening the mount or
    spawning a second 8100 from the console.

    ``start_host`` uses the broker ``start`` verb (the same verb the systemd
    ``ExecStartPost`` uses) and forwards the operator's ``explicit`` intent: a
    plain watchdog start is honestly refused while a persisted operator stop
    intent is in force, while the operator's own explicit start supersedes it.
    """

    def __init__(self, broker=None, read_ops=None):
        self._broker = broker
        self._read_ops = read_ops

    def _read(self):
        if self._read_ops is None:
            self._read_ops = _cloud_host_ops()
        return self._read_ops

    def _broker_client(self):
        if self._broker is None:
            self._broker = _default_cloud_broker()
        return self._broker

    def status(self):
        return self._read().status()

    def start_host(self, explicit=False):
        # The broker start verb carries the operator's explicit intent: a plain
        # watchdog start still respects a persisted stop intent, while the
        # operator's own start supersedes it.
        from console.share_production import _ProductionBrokerOps
        return _ProductionBrokerOps(
            self._broker_client()).start_host(explicit=explicit)

    def stop_host(self, reason="manual"):
        from console.share_production import _ProductionBrokerOps
        return _ProductionBrokerOps(self._broker_client()).stop_host()

    def restart(self):
        # ONE bounded 8100 single-layer restart in the broker: the broker's own
        # stop writes the stop intent and its explicit start clears it.
        from console.share_production import production_gateway_restart
        return production_gateway_restart(broker=self._broker_client())


def cloud_service_status(ops=None):
    """Read-only cloud snapshot: the UM share entry from the Linux host ops."""
    gate = ops or _cloud_host_ops()
    state = gate.status()
    mapped = {"running": "Running", "stopped": "Stopped",
              "conflict": "Conflict"}.get(state.get("state"), "Unknown")
    return {"ok": True,
            "services": [{
                "key": CLOUD_GATEWAY_TARGET,
                "display_name": "UM 分享入口 8100",
                "port": _cloud_gateway_port(),
                "state": mapped,
                "healthy": bool(state.get("healthy")),
                "detail": str(state.get("detail") or ""),
                "process_ids": list(state.get("process_ids") or []),
            }],
            "meta": {"source": "linux-pid-file",
                     "generated_epoch": int(time.time())}}


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

    Cloud role: the SAME class drives the Linux pid-file host ops instead of
    the PowerShell wrapper (no PowerShell/tasklist/netstat is ever called).
    The operator's start is EXPLICIT (it supersedes a persistent stop
    intent); stop writes the intent BEFORE signalling.
    """

    def __init__(self, cloud=False, ops=None, broker=None):
        self.cloud = bool(cloud)
        self._ops = ops
        self._broker = broker

    def _cloud_gate(self):
        if self.cloud:
            # Mutations go through the restricted broker that OWNS
            # /run/um-share; reads keep the console's read-only face.
            return _BrokerCloudHostOps(broker=self._broker,
                                       read_ops=self._ops)
        if self._ops is None:
            self._ops = _cloud_host_ops()
        return self._ops

    def _cloud_action(self, action, target):
        enabled, reason = runtime_target_enabled(action, target, cloud=True)
        if not enabled:
            raise OperationUnavailable(action, reason)

    def _cloud_refusal(self, message):
        return OperationError("runtime_error", message)

    def service_start(self, target, params, ctx=None):
        if ctx is not None:
            ctx.raise_if_cancelled()
        if self.cloud:
            self._cloud_action("service.start", target)
            try:
                started = self._cloud_gate().start_host(explicit=True)
            except OperationError:
                raise
            except Exception as exc:  # noqa: BLE001 - honest failure
                raise self._cloud_refusal(
                    "分享入口启动调用异常（%s）：实际状态以只读状态复验为准"
                    % type(exc).__name__)
            if started is not True:
                raise self._cloud_refusal(
                    "分享入口启动未获 pid/身份确认；不冒充已启动")
            return {"key": target, "status": "Started", "action": "start"}
        enabled, reason = runtime_target_enabled("service.start", target)
        if not enabled:
            raise OperationUnavailable("service.start", reason)
        payload = _invoke_wrapper("start", key=target, caller_holds_lock=True)
        result = payload.get("result")
        return result if isinstance(result, dict) else {"key": target}

    def service_stop(self, target, params, ctx=None):
        if ctx is not None:
            ctx.raise_if_cancelled()
        if self.cloud:
            self._cloud_action("service.stop", target)
            try:
                stopped = self._cloud_gate().stop_host(reason="manual")
            except OperationError:
                raise
            except Exception as exc:  # noqa: BLE001 - honest failure
                raise self._cloud_refusal(
                    "分享入口停止调用异常（%s）：实际状态以只读状态复验为准"
                    % type(exc).__name__)
            if stopped is not True:
                raise self._cloud_refusal(
                    "分享入口停止未获进程确认；停用意图是否持久化未确认，"
                    "不冒充已停止；以只读状态复验为准")
            return {"key": target, "status": "Stopped", "action": "stop"}
        enabled, reason = runtime_target_enabled("service.stop", target)
        if not enabled:
            raise OperationUnavailable("service.stop", reason)
        payload = _invoke_wrapper("stop", key=target, caller_holds_lock=True)
        result = payload.get("result")
        return result if isinstance(result, dict) else {"key": target}

    def service_restart(self, target, params, ctx=None):
        if ctx is not None:
            ctx.raise_if_cancelled()
        if self.cloud:
            self._cloud_action("service.restart", target)
            gate = self._cloud_gate()
            restart = getattr(gate, "restart", None)
            if restart is None:
                raise self._cloud_refusal(
                    "云端宿主未装配受限重启（broker gateway-restart 缺席）："
                    "不冒充已重启")
            try:
                result = restart()
            except OperationError:
                raise
            except Exception as exc:  # noqa: BLE001 - honest failure
                raise self._cloud_refusal(
                    "分享入口重启调用异常（%s）：实际状态以只读状态复验为准"
                    % type(exc).__name__)
            if not isinstance(result, dict) \
                    or result.get("restarted") is not True \
                    or result.get("stopped_confirmed") is not True \
                    or result.get("running_confirmed") is not True:
                raise self._cloud_refusal(
                    "分享入口重启未三证确认（restarted=%r，"
                    "stopped_confirmed=%r，running_confirmed=%r）："
                    "不冒充已重启"
                    % (result.get("restarted") if isinstance(result, dict)
                       else None,
                       result.get("stopped_confirmed")
                       if isinstance(result, dict) else None,
                       result.get("running_confirmed")
                       if isinstance(result, dict) else None))
            return {"key": target, "status": "Restarted",
                    "action": "restart", "gateway": result}
        enabled, reason = runtime_target_enabled("service.restart", target)
        if not enabled:
            raise OperationUnavailable("service.restart", reason)
        # Stop+start run inside one bridge lock acquisition; restart takes
        # roughly twice a bare stop (health wait on both halves), so the
        # wrapper timeout doubles accordingly.
        payload = _invoke_wrapper("restart", key=target, timeout_s=120.0,
                                  caller_holds_lock=True)
        result = payload.get("result")
        return result if isinstance(result, dict) else {"key": target}


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
    _refuse_unauthorized_target("service.start", target)
    return "service:" + target


def _service_stop_resource(target, params):
    _refuse_unauthorized_target("service.stop", target)
    return "service:" + target


def _service_restart_resource(target, params):
    _refuse_unauthorized_target("service.restart", target)
    return "service:" + target


def _um_account_resource(target, params):
    # Single-account actions: the resource is fixed regardless of the
    # display-only target string (prevents arbitrary resource keys).
    return "account:um"


def _model_context_resource(target, params):
    # All retained context operations serialize on model_sync's own SyncLock
    # inside the adapter; the console resource groups them for cancel/
    # generation semantics.
    return "model:context"


def _model_catalog_resource(target, params):
    # Pure catalog read: no shared state is written, so it gets its own
    # resource and never serializes against context writes.
    return "model:catalog"


def _share_resource(target, params):
    # One orchestration resource: open/close/estop/retry/status all
    # serialize (a partial close and a concurrent open must not interleave).
    return "share:orchestration"


def _share_public_resource(target, params):
    """Human public-supervision clicks serialize on their own resource.

    The ACTUAL mutex is the module's ``share:public-state`` (writes) and
    ``share:orchestration`` (side effects); this resource only keeps two
    human clicks from racing each other. The target is fixed to ``share`` —
    no arbitrary URL, path or service is accepted.
    """
    if target != "share":
        raise OperationError(
            "invalid_target",
            "目标必须是 share（公网巡检不接受任意 URL/路径/服务名）")
    return "share:public"


def production_public_cancel_request(reason):
    """Write the public-supervision cancel file (production seam).

    Distinguishes "module not deployed" (None = unwired, no risk to guard)
    from "wired but the write failed" (False); never fakes a write.
    """
    try:
        share_public_supervision = __import__("importlib").import_module("share.public_supervision")
    except Exception:  # noqa: BLE001
        return None
    try:
        return bool(share_public_supervision.request_cancel(reason))
    except Exception:  # noqa: BLE001
        return False


def production_public_cancel_clear():
    """Clear the public-supervision cancel file (production seam)."""
    try:
        share_public_supervision = __import__("importlib").import_module("share.public_supervision")
    except Exception:  # noqa: BLE001
        return None
    try:
        return bool(share_public_supervision.clear_cancel())
    except Exception:  # noqa: BLE001
        return False


def _wire_public_cancel_seams():
    """Bind the two cancel seams ONLY when the module is deployed.

    An import failure keeps them None (unwired) — "module not deployed" must
    never be reported as "cancel write failed" (contract §5.4).
    """
    try:
        share_public_supervision = __import__("importlib").import_module("share.public_supervision")  # noqa: F401
    except Exception:  # noqa: BLE001
        return
    console_share_actions._PUBLIC_CANCEL_REQUEST = \
        production_public_cancel_request
    console_share_actions._PUBLIC_CANCEL_CLEAR = production_public_cancel_clear


def wire_share_production_seams(keys_manager=None, cloud=False):
    """Bind the REAL share seams — PRODUCTION ASSEMBLY ONLY.

    console_share_production.production_seams() returns the orchestration's
    seam globals by name (broker restricted protocol, funnel command,
    connectivity observation); the TS privileged seam stays fail-closed
    until the broker whitelist extension is deployed (package section 5).
    Called from usagesrv's production console-manager assembly ONLY —
    never from build_registry (a pure factory that tests must be able to
    call without any OS-level side effect).

    ``cloud=True`` is the explicit cloud (Linux) role: the Tailscale/Funnel
    seams stay UNWIRED and the persistent stop intent is the Linux file the
    pid-file host ops write.
    """
    try:
        seams = console_share_production.production_seams(
            keys_manager=keys_manager, cloud=cloud)
    except Exception:
        # Fail-closed: leave the honest unwired seams in place. The two
        # public-cancel seams are independent of the share assembly and are
        # still bound below when their module is deployed.
        seams = None
    for key, value in (seams or {}).items():
        if value is not None:
            setattr(console_share_actions, key, value)
    # D18-10: the cloud edge is a READ-ONLY systemd fact. On the local
    # Windows role there is no such unit, so the seam stays unwired (the
    # orchestration then reports "not managed by this deployment").
    console_share_actions._EDGE_STATUS = \
        production_cloud_edge_status if cloud else None
    _wire_public_cancel_seams()


CLOUDFLARED_UNIT = "cloudflared"


def production_cloud_edge_status(*, runner=None):
    """READ-ONLY systemd status of the cloud public-edge unit.

    Returns ``{exists, active, detail}``. A missing unit is a deterministic
    ``exists=False`` ("this deployment does not manage the public edge"), not a
    failure; an unreadable query stays ``exists=None`` (unconfirmed), never a
    fabricated active/inactive fact.
    """
    run = runner or subprocess.run
    try:
        completed = run(
            ["systemctl", "show", CLOUDFLARED_UNIT,
             "--property=LoadState", "--property=ActiveState"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {"exists": None, "active": None,
                "detail": "systemctl_unavailable"}
    if completed.returncode != 0:
        return {"exists": None, "active": None,
                "detail": "systemctl_query_failed"}
    values = {}
    for line in (completed.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    load = values.get("LoadState")
    if load == "not-found":
        return {"exists": False, "active": None, "detail": "unit_not_found"}
    if load != "loaded":
        return {"exists": None, "active": None,
                "detail": "load_state_unknown"}
    active = values.get("ActiveState")
    if active not in ("active", "inactive", "failed", "activating",
                       "deactivating"):
        return {"exists": True, "active": None,
                "detail": "active_state_unknown"}
    return {"exists": True, "active": active == "active", "detail": active}


def _share_action_specs(include_public=True, cloud=False):
    """The UM share action catalogue (the ONLY cloud-role actions).

    Extracted verbatim from ``build_registry`` so the explicit cloud role can
    assemble exactly these actions without importing or touching any non-UM
    subsystem.

    ``include_public=False`` omits the three Tailscale/WFP-specific actions
    (``share.public_check``, ``share.public_retry``, ``share.repair_limits``):
    the cloud deployment exposes direct HTTPS, not a Funnel, and has no Windows
    WFP rules, so those capabilities are honestly NOT assembled there.

    ``cloud=True`` additionally marks the four layered open/close chain actions
    (``CLOUD_SHARE_ORCH_ACTIONS``) DISABLED: their first layer is the Windows
    TS service / Funnel orchestration, which does not exist on the cloud. They
    stay in the catalogue so the console can report WHY they are unavailable;
    the manager refuses every submit before an operation row exists. The
    Windows/local catalogue is built with ``cloud=False`` and is unchanged.
    """
    specs = [
        ActionSpec(
            "share.start", "UM 分享开启链（TS 服务→连接→后端→Funnel）",
            handler=console_share_actions.share_open,
            resource_fn=_share_resource,
            param_schema=_SHARE_OPEN_BUDGET_SCHEMA,
            # 节5 批准记录（2026-09-16）：生产适配器已接线。R13-A：
            # backend_auth_budget_s 为专用后端认证等待预算（logging_in/
            # unknown 观察），仅本动作消费。
            enabled=True),
        ActionSpec(
            "share.stop", "UM 分享正常关闭链（撤入口→排空→后端→TS）",
            handler=console_share_actions.share_close,
            resource_fn=_share_resource,
            param_schema=_SHARE_BUDGET_SCHEMA,
            # 节5 批准记录（2026-09-16）。pre_lock 在 worker 获取
            # share:orchestration 之前写公网巡检取消文件（契约 §5.4）。
            enabled=True,
            pre_lock=console_share_actions.public_cancel_pre_lock(
                "manual_close")),
        ActionSpec(
            "share.estop", "UM 分享紧急停止（立即撤入口，不排空）",
            handler=console_share_actions.share_estop,
            resource_fn=_share_resource,
            param_schema=_SHARE_BUDGET_SCHEMA,
            # 节5 批准记录（2026-09-16）：生产适配器已接线。
            enabled=True,
            pre_lock=console_share_actions.public_cancel_pre_lock(
                "manual_estop")),
        ActionSpec(
            "share.retry_stop", "UM 分享残留关闭重试（按剩余步骤）",
            handler=console_share_actions.share_retry_stop,
            resource_fn=_share_resource,
            param_schema=_SHARE_RETRY_SCHEMA,
            # 节5 批准记录（2026-09-16）。
            enabled=True,
            pre_lock=console_share_actions.public_cancel_pre_lock(
                "manual_retry_stop")),
        ActionSpec(
            "share.backend_login", "登录分享专用后端（触发独立 profile 浏览器 UM 登录）",
            handler=console_share_actions.backend_login,
            resource_fn=_share_resource,
            # 用户指令（2026-09-17）：专用实例登录必须经控制台触发——
            # 专用控制秘密调 POST /admin/auth/login，由后端拉起其独立
            # profile 浏览器；不触本机 8000，不回显秘密，不预开 Funnel。
            enabled=True),
        ActionSpec(
            "share.status", "UM 分享分层状态观察（只读）",
            handler=console_share_actions.share_status,
            resource_fn=_share_resource,
            # Enabled: read-only layer observation through injectable seams;
            # unwired production seams report honestly unavailable.
            enabled=True, bump_generation_on_success=False),
        ActionSpec(
            "share.diagnostics",
            "UM 分享九层分层诊断（只读；含看门狗、维护视图与故障留证）",
            handler=console_share_actions.diagnostics,
            resource_fn=_share_resource,
            param_schema={},
            # Enabled: read-only, global, never raises on one failing layer,
            # never triggers SSO/model requests and never writes intent.
            enabled=True, disabled_reason=None,
            bump_generation_on_success=False, cancelable=True),
        ActionSpec(
            "share.backend_reauthenticate",
            "仅重新认证分享专用后端 8110（不冷启动、不重启进程、不绕过人工认证）",
            handler=console_share_actions.backend_reauth,
            resource_fn=_share_resource,
            param_schema={},
            enabled=True, disabled_reason=None,
            bump_generation_on_success=False, cancelable=True),
        ActionSpec(
            "share.backend_restart",
            "重启分享专用后端 8110（单层：暂停 8100 接收→有界排空→重启→恢复接收；"
            "不动 8100 实例与私人 8000）",
            handler=console_share_actions.backend_restart,
            resource_fn=_share_resource,
            param_schema=_SHARE_BACKEND_RESTART_SCHEMA,
            enabled=True, disabled_reason=None,
            # A restart tears the managed process down and relaunches it:
            # queued work on the same resource is honestly invalidated.
            bump_generation_on_success=True, cancelable=True),
        ActionSpec(
            "share.gateway_restart",
            "重启分享入口 8100（单层：经受限 broker 宿主协议；不动 8110 实例）",
            handler=console_share_actions.gateway_restart,
            resource_fn=_share_resource,
            param_schema=_SHARE_GATEWAY_RESTART_SCHEMA,
            enabled=True, disabled_reason=None,
            bump_generation_on_success=True, cancelable=True),
    ]
    if include_public:
        # S5（契约 §5.2）：公网链路一键复查与受限重试。两者都走
        # ``share:public`` 资源串行化人工点击；真正的互斥由模块内部的
        # ``share:public-state``（状态/事件/预算/取消写入）和
        # ``share:orchestration``（Funnel/TS 副作用）承担。
        # 不设置 pre_lock：它们不写取消文件。
        specs.extend([
            ActionSpec(
                "share.public_check",
                "分享公网链路复查（只读 DNS/TLS，不发送推理请求）",
                handler=console_share_actions.public_check,
                resource_fn=_share_public_resource,
                param_schema={},
                enabled=True, disabled_reason=None,
                bump_generation_on_success=False, cancelable=True),
            ActionSpec(
                "share.public_retry",
                "分享公网链路受限重试（复用滚动 30 分钟预算与编排互斥）",
                handler=console_share_actions.public_retry,
                resource_fn=_share_public_resource,
                param_schema={},
                enabled=True, disabled_reason=None,
                bump_generation_on_success=False, cancelable=True),
            ActionSpec(
                "share.repair_limits",
                "暂停公网并修复隔离规则（成功后保持分享关闭）",
                handler=console_share_actions.repair_limits,
                resource_fn=_share_resource,
                param_schema={},
                enabled=True, bump_generation_on_success=True, cancelable=False,
                # pre_lock 在动作锁之前写公网巡检取消文件（契约 §5.4）。
                pre_lock=console_share_actions.public_cancel_pre_lock(
                    "repair_limits")),
        ])
    if cloud:
        _mark_cloud_share_specs(specs)
    return specs


def _mark_cloud_share_specs(specs):
    """Mark the TS/Funnel chain actions honestly UNASSEMBLED (cloud role).

    ``enabled=False`` is the manager's submit-time refusal authority: it raises
    OperationUnavailable (code ``action_disabled``) with the cloud reason BEFORE
    an operation row is created, so no handler can run and no fake success is
    possible. Mutating only the freshly built cloud list never touches the
    Windows/local catalogue.
    """
    for spec in specs:
        if spec.name in CLOUD_SHARE_ORCH_ACTIONS:
            spec.enabled = False
            spec.disabled_reason = CLOUD_SHARE_ORCH_DISABLED_REASON
    return specs


def _cloud_service_resource(action):
    def resource(target, params):
        enabled, reason = runtime_target_enabled(action, target, cloud=True)
        if not enabled:
            raise OperationUnavailable(action, reason)
        # A2: the service page's start/stop/restart and the share page's
        # share.gateway_restart act on the SAME 8100 resource. ONE resource
        # key makes the operation manager's existing mutex serialize them,
        # instead of two page-local button states pretending to do so.
        return "share:orchestration"
    return resource


def build_cloud_registry():
    """Explicit cloud role registry: ONLY the UM share stack.

    The non-UM cards and the
    Tailscale/WFP-specific public repair are NOT assembled: this factory never
    imports their modules. The four layered open/close chain actions stay in the
    catalogue but are DISABLED (``CLOUD_SHARE_ORCH_DISABLED_REASON``): the
    cloud terminates HTTPS directly and has no Tailscale/Funnel/WFP layer, so a
    submit is refused with an explicit unassembled error before any row exists.
    Real service start/stop/restart drive the Linux pid-file adapter for
    ``share-gateway`` only.
    """
    runtime = RuntimeServiceAdapter(cloud=True)
    specs = [
        ActionSpec(
            "service.start", "启动 UM 分享入口（Linux pid 文件与真实进程身份）",
            handler=runtime.service_start,
            resource_fn=_cloud_service_resource("service.start"),
            enabled=True, disabled_reason=None,
            bump_generation_on_success=False),
        ActionSpec(
            "service.stop", "停止 UM 分享入口（先持久化停用意图，再发信号）",
            handler=runtime.service_stop,
            resource_fn=_cloud_service_resource("service.stop"),
            enabled=True, disabled_reason=None,
            bump_generation_on_success=False),
        ActionSpec(
            "service.restart", "重启 UM 分享入口（停止后显式启动；中断在途工作）",
            handler=runtime.service_restart,
            resource_fn=_cloud_service_resource("service.restart"),
            enabled=True, disabled_reason=None,
            bump_generation_on_success=True),
    ]
    specs.extend(_share_action_specs(include_public=False, cloud=True))
    # D18-10: the CLOUD-ONLY whole-chain lifecycle actions. They drive the
    # restricted broker + the dedicated 8110 only — the cloud deployment has
    # no Tailscale/Funnel layer, so the four legacy layered actions above stay
    # DISABLED while these three are the daily start/stop controls.
    specs.extend([
        ActionSpec(
            "share.chain_start",
            "启动整条 API 链（8110 运行→有界认证等待→8100 三证启动→公网边缘只读核对）",
            handler=console_share_actions.chain_start,
            resource_fn=_share_resource,
            param_schema=_SHARE_OPEN_BUDGET_SCHEMA,
            enabled=True, disabled_reason=None,
            bump_generation_on_success=False, cancelable=True),
        ActionSpec(
            "share.chain_stop",
            "普通停止整条 API 链（先拒新请求，有界等待在途；排空超时明确显示）",
            handler=console_share_actions.chain_stop,
            resource_fn=_share_resource,
            param_schema=_SHARE_BUDGET_SCHEMA,
            enabled=True, disabled_reason=None,
            bump_generation_on_success=False, cancelable=False),
        ActionSpec(
            "share.chain_estop",
            "强制停止整条 API 链（立即取消在途请求并停 8110）",
            handler=console_share_actions.chain_estop,
            resource_fn=_share_resource,
            param_schema=_SHARE_BUDGET_SCHEMA,
            enabled=True, disabled_reason=None,
            bump_generation_on_success=False, cancelable=False),
        ActionSpec(
            "share.backend_login_cancel",
            "取消本轮登录（只结束本轮并清理浏览器；不停止服务）",
            handler=console_share_actions.backend_login_cancel,
            resource_fn=_share_resource,
            param_schema={},
            # D18-8：取消只结束本轮登录，不停止/重启专用实例；普通请求
            # 不自动重开，只有显式登录动作可以重开。
            enabled=True, disabled_reason=None,
            bump_generation_on_success=False, cancelable=False),
    ])
    return specs


def build_registry(cloud=False):
    """Production registry (explicit enablement facts, never inferred).

    Real service start/stop enablement is COMPUTED from the per-action +
    per-target policy sets above: the populated sets enable the action while
    the resource function refuses every unauthorized target at submit time
    and the handler re-gates at execution time (defense in depth). No
    blanket flip; adding a target is a one-line policy-data change.

    ``cloud=True`` assembles ONLY the UM share stack (see
    ``build_cloud_registry``); the local Windows registry keeps every
    existing card and imports the non-UM modules lazily.
    """
    if cloud:
        return build_cloud_registry()
    # LOCAL (Windows) assembly: UM actions + the UM catalog/context
    # management surface. No retired-source action module is imported here
    # (B0, 2026-11-01).
    from console import model_actions as console_model_actions
    from console import um_adapter as console_um_adapter
    from console import um_login as console_um_login
    runtime = RuntimeServiceAdapter()
    start_enabled = bool(SERVICE_START_ENABLED_TARGETS)
    stop_enabled = bool(SERVICE_STOP_ENABLED_TARGETS)
    restart_enabled = bool(SERVICE_RESTART_ENABLED_TARGETS)
    return [
        ActionSpec(
            "service.start", "启动一个网关服务（经 runtime 权威）",
            handler=runtime.service_start, resource_fn=_service_start_resource,
            enabled=start_enabled,
            disabled_reason=None if start_enabled
            else RUNTIME_ACTIONS_DISABLED_REASON,
            # generation bumps are reserved for explicit restart orchestration;
            # a successful start must not cancel legitimately queued work.
            bump_generation_on_success=False),
        ActionSpec(
            "service.stop", "停止一个网关服务（经 runtime 权威）",
            handler=runtime.service_stop, resource_fn=_service_stop_resource,
            enabled=stop_enabled,
            disabled_reason=None if stop_enabled
            else RUNTIME_ACTIONS_DISABLED_REASON,
            bump_generation_on_success=False),
        ActionSpec(
            "service.restart", "重启一个网关服务（停止后立即重新启动；中断该服务在途工作）",
            handler=runtime.service_restart, resource_fn=_service_restart_resource,
            enabled=restart_enabled,
            disabled_reason=None if restart_enabled
            else RUNTIME_ACTIONS_DISABLED_REASON,
            # The comment reserved generation bumps for explicit restart
            # orchestration — this IS that action: the managed process is
            # torn down and relaunched, so queued work for it is honestly
            # invalidated by the generation bump on success.
            bump_generation_on_success=True),
        ActionSpec(
            "account.um_status", "UM 认证状态查询（内存态只读，未主动核验）",
            handler=console_um_adapter.um_auth_status,
            resource_fn=_um_account_resource,
            # Enabled: pure memory read, no refresh/no upstream probe/no
            # thread spawn (chat2api.py:511-517). This is the uncontested
            # P2 seam-B slice; active verification stays unavailable.
            enabled=True, bump_generation_on_success=False),
        ActionSpec(
            "account.um_verify", "UM 账号主动核验（向上游探测凭据有效性）",
            handler=console_um_adapter.um_verify_account,
            resource_fn=_um_account_resource,
            # B-2 (2026-09-20) enablement: the RUNNING chat2api production
            # instance ships GET /admin/auth/verify (live probe answered 200
            # with a valid verify payload on 2026-09-20), so the previous
            # disabled reason ("process predates the commit") no longer
            # holds. Each call is ONE classification probe: no refresh, no
            # auth-state mutation, control token never in any result.
            enabled=True, bump_generation_on_success=False),
        ActionSpec(
            "account.login", "账号登录/重新授权（UM 编排）",
            handler=console_um_login.relogin_um,
            resource_fn=_um_account_resource,
            param_schema=_LOGIN_SCHEMA,
            # 节5 批准记录（2026-09-16）+ 用户指令 A3：登录编排恢复启用。
            # 登录=用户本机交互（Edge 窗口由用户完成）；编排自身的临时启停
            # 恢复/超时复验/协作取消已隔离验证。
            enabled=True),
        ActionSpec(
            "model.context_probe_preview",
            "上下文探测队列预览（只读：队列+计划+估算，零请求零写入）",
            handler=console_model_actions.model_context_probe_preview,
            resource_fn=_model_context_resource,
            # Enabled: pure read of the persisted contextProbes state (no
            # SyncLock, no seeding, no model request, no fabricated cost
            # figures — unpriced capacity is reported as 未定价/无法核算).
            # 显式选择（与 run 动作同款约定，加性）：sources 为来源子集
            # （列表或 JSON 字符串）、models 为 {source: [model_id]}、
            # retest 为布尔——把估算限定到被选记录，未选中记录估算 0、不
            # 进 totals；retest=True 时 verified/failed 按先归档旧代次再
            # 按全梯重测计。选择无法确认（未知来源/空集合/非法类型/不在
            # 队列的模型）在 handler 内 invalid_params 拒绝，绝不静默退回
            # 全队列。预览仍只读：不发模型请求、不写状态（凭据文件经
            # token provider 只读读一次，无副作用）。
            param_schema={
                "sources": {"type": "str", "max_len": 512},
                "models": {"type": "object"},
                "retest": {"type": "bool"},
            },
            enabled=True, bump_generation_on_success=False),
        ActionSpec(
            "model.context_probe_run",
            "执行上下文探测队列（真实长输入计费请求，集中批准后启用；支持 "
            "sources/models 显式选择与 retest 重测）",
            handler=console_model_actions.model_context_probe_run,
            resource_fn=_model_context_resource,
            param_schema={
                "max_requests": {"type": "int", "min": 1, "max": 20},
                "max_tokens": {"type": "int", "min": 1, "max": 64},
                # 显式选择（Plan-Revision 7 第 3 步）：sources 为来源子集
                # （列表或 JSON 字符串，与目录动作的 sources 约定一致）、
                # models 为 {source: [model_id]}、retest 为布尔。选择无法
                # 确认（未知来源/空集合/非法类型/不在队列的模型）在 handler
                # 内明确拒绝，绝不静默退回全队列。
                "sources": {"type": "str", "max_len": 512},
                "models": {"type": "object"},
                "retest": {"type": "bool"},
            },
            # Handler wired against model_sync.probe_context_queue (bounded
            # increasing long-input requests, cancel+budget before each call,
            # evidence persisted via the existing transaction chain). Enabled
            # 2026-09-22 by maintainer authorization (上下文探测预算与请求清单
            # 批准，R9-R44 停点清单①结案): every step is a REAL billed request
            # within the registered budget caps (≤20 requests, ≤64 tokens).
            # Selection narrows the request set (unconfirmed selections are
            # rejected, never widened); retest archives the old generation
            # into the record's history before re-measuring.
            enabled=True,
            disabled_reason=None),
        ActionSpec(
            "model.context_overrides",
            "人工 context 覆盖读面（声明/覆盖/有效值/来源；只读）",
            handler=console_model_actions.model_context_overrides,
            resource_fn=_model_context_resource,
            param_schema={
                "local_only": {"type": "bool"},
                "models": {"type": "object"},
            },
            # Enabled: pure read (collect_catalogs GETs + state read; no
            # SyncLock, no client write, no model request).
            enabled=True,
            disabled_reason=None),
        ActionSpec(
            "model.context_override_set",
            "设置人工 context 覆盖（仅写 model-sync 状态文件；覆盖保存≠客户端已应用）",
            handler=console_model_actions.model_context_override_set,
            resource_fn=_model_context_resource,
            param_schema={
                "source": {"type": "str", "max_len": 128},
                "model_id": {"type": "str", "max_len": 128},
                # context 覆盖边界与 model_sync 同源：下界 1（正整数），上界
                # CONTEXT_OVERRIDE_MAX=10_000_000（schema 与后端校验同口径）。
                "context": {"type": "int", "min": 1, "max": 10_000_000},
                "expected_set_at": {"type": "str", "max_len": 64},
            },
            # Enabled: state-only write (the model-sync state file under the
            # existing lock + transaction), Plan-Revision 7 step 6.
            # Positive-int validation, model identity against the CURRENT
            # catalog (collected read-only), and the expected_set_at version
            # rule (conflicts raise version_conflict, never silent clobber).
            # Saving is state-only: the client synchronization that used to
            # publish the effective value is retired; saving never writes a
            # client configuration.
            enabled=True,
            disabled_reason=None),
        ActionSpec(
            "model.context_override_clear",
            "恢复默认＝删除人工 context 覆盖（探测历史证据保留）",
            handler=console_model_actions.model_context_override_clear,
            resource_fn=_model_context_resource,
            param_schema={
                "source": {"type": "str", "max_len": 128},
                "model_id": {"type": "str", "max_len": 128},
                "expected_set_at": {"type": "str", "max_len": 64},
            },
            # Enabled: state-only write, idempotent clear (existed flag);
            # measured probe history is never deleted by a clear.
            enabled=True,
            disabled_reason=None),
        ActionSpec(
            "model.catalog", "模型目录只读查询（各来源 /v1/models；无探测、无计费、无启停）",
            handler=console_model_actions.model_catalog,
            resource_fn=_model_catalog_resource,
            # Enabled: pure read via model_sync.collect_catalogs() (plain
            # GETs; single-source failures land in warnings). No SyncLock,
            # no client-config write, no probe, no billing.
            # R7（2026-10-10）：sources 为来源子集（列表或 JSON 字符串，
            # 与 scope/probe 选择同一约定）——单来源只读该来源；未知/暂停
            # 来源在 handler 内明确拒绝（invalid_params），绝不静默退回
            # 四源；缺省保持「全部模型」聚合入口。
            param_schema={
                "sources": {"type": "str", "max_len": 512},
                # B2（2026-11-01）：strict-fresh UM refresh. The handler has
                # always accepted it; without this declaration the manager
                # rejected it as an unknown param before the handler ran.
                "fresh": {"type": "bool"},
            },
            enabled=True, bump_generation_on_success=False),
        # UM 分享动作目录（含 TS/Funnel 专用公网巡检与 WFP 隔离修复）
        # 抽到 _share_action_specs()：云端角色只装配其中的非 TS/WFP 子集，
        # 本地 Windows 装配保持全部动作不变。
        *_share_action_specs(),
    ]
