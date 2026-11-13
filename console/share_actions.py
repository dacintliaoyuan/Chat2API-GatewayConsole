# -*- coding: utf-8 -*-
"""UM share orchestration adapter (cloud share chain, Rev 5 L5).

This module keeps the handlers the cloud console registers: read-only layered
status/diagnostics and the restricted broker gateway-restart. The retired Windows
local share chain (TS service start, connectivity wait, Funnel on/off, normal
close / emergency stop / residual retry and public DNS/TLS supervision) is gone
(#33); the dedicated backend lifecycle, login/reauth and whole-chain actions
are gone too (#34). Nothing here starts the production chat2api process,
unregisters accounts, deletes device identity, or claims a close it did not
confirm.

PRIVILEGE / AUTHORIZATION: every privileged action goes through an INJECTED
seam; the production default leaves each seam UNWIRED and reports that
honestly (never a simulated success):

* ``_broker_ops()`` - the restricted broker operations gate: ONLY the
  existing restricted primitives (three-evidence verify, the task gate and the
  broker drain / emergency-stop protocol actions). No arbitrary command,
  service-name or path channel is added here.
* ``_MAINTENANCE_BEGIN`` / ``_MAINTENANCE_END`` / ``_MAINTENANCE_STATUS`` /
  ``_GATEWAY_RESTART`` - the Rev9 single-layer maintenance and restart seams.
* ``_DESIRED_STOP_READER`` - read-only observation seam; unwired reads as
  unavailable, never as a fabricated state.

Handler contract matches the task layer: ``handler(target, params, ctx)``.
Error codes: share_layer_failed / share_broker_unwired /
share_privilege_unwired. This module never unregisters accounts, never
deletes device identity, and never claims a full close it did not confirm.
"""

import math
import threading
import time

from console.operations import OperationCancelled, OperationError
from share.broker_protocol import COMPONENT_SHARE_GATEWAY

# Rev9 (A9-1/A9-4): the single-layer maintenance actions use the 8100
# entry component vocabulary. The name is IMPORTED from the protocol module
# (single source of truth, §2.1) instead of being re-declared as a literal, so
# a wire-side rename can never leave this layer sending a stale component name.

# ------------------------------------------------------------------ budgets
_STOP_CONFIRM_DEFAULT_S = 10.0
_INTERVAL_DEFAULT_S = 1.0
_BUDGET_MAX_S = 3600.0

# ------------------------------------------------------ injection seams
# None = production default = not wired (the authorization package assigns
# the real objects). Tests inject stubs; production never fakes these.
_BROKER_OPS = None             # restricted broker operations gate (object)
_DESIRED_STOP_READER = None    # desired-stop view override (production may
                               # wire the broker's protected record reader)
# R8-A7 open precondition: at least one ENABLED share key exists and the
# published config is in sync (read-only; no key material, no plaintext).
_KEYS_CONFIRMED = None
# Diagnosis split for the same precondition (2026-09-22): returns a dict
# {in_sync, keys_ok, enabled_keys, total_keys, reasons} so the fail-closed
# message can tell "no enabled key" from "config not in sync" with real
# numbers. Unwired = the precondition message stays as-is (no new noise);
# a failing diagnosis reads as unknown and NEVER blocks the fail-closed
# raise itself.
_KEYS_DIAGNOSIS = None
_SLEEP = time.sleep

# ---------------------------------------------- Rev9 single-layer maintenance
# A9-1..A9-4 (contract §3.5/§4.3/§5.4/§6.3): the six frozen injection
# seams. None = production default = not wired (production_seams() assigns the
# real objects); tests inject stubs. Nothing here fakes an unwired seam.
_MAINTENANCE_BEGIN = None   # (component:str, lease_s:int) -> dict
_MAINTENANCE_END = None     # (component:str, lease_id:str) -> dict
_MAINTENANCE_STATUS = None  # () -> dict|None (broker protected view)
_GATEWAY_RESTART = None     # (budget_s:int) -> dict (broker verb)


# Observation TTLs (contract §3.4, frozen): stale = age_s > ttl_s; a layer
# whose evidence carries no observation time reads checked_at=None + stale=true
# ("this process obtained no evidence").
_DIAG_TTL_S = {"share_gateway": 60, "security_gate": 600}
_DIAG_LAYER_KEYS = ("share_gateway", "security_gate")
# Frozen defaults (contract §1.3): params are validated by the registry
# schema at submit time; the handler keeps the same defensive fallback the
# legacy _budget() uses for a direct call.
_GATEWAY_RESTART_DEFAULTS = {"restart_budget_s": (120, 10, 180)}


def _broker_ops():
    """Return the injected restricted broker operations gate.

    The gate surface is FIXED to existing restricted primitives only:
    ``verify_running()``/``verify_closed()`` (three-evidence states
    True/False/None), ``start_host()``/``stop_host()`` (the existing task
    /Run//End gate), ``drain()``/``emergency_stop()`` (existing broker
    protocol actions). No command/service-name/path channel is added here.
    Unwired (production default) raises share_broker_unwired — an honest
    refusal, never a fallback.
    """
    if _BROKER_OPS is None:
        raise OperationError(
            "share_broker_unwired",
            "broker 操作门未接线（部署授权包装配后可用）；不冒充可用")
    return _BROKER_OPS


class _ManagedStopState:
    """Orchestration-side desired-stop view (watchdog-coordination mirror).

    ``express_stop`` records the stop INTENT before any close layer runs;
    ``confirm_stopped`` writes the stopped terminal ONLY after every close
    target is confirmed — the orchestration never writes stopped ahead of
    the facts (a premature stopped marker would relax the watchdog's
    managed-stop contract). The broker's protected lifecycle record stays
    the production authority; this view serves the console's read-only
    status and the residual/retry semantics.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._desired = None          # None=unknown; True=stop intent in force
        self._expressed_at = None
        self._stopped = False
        self._stopped_at = None

    def express_stop(self):
        with self._lock:
            self._desired = True
            self._expressed_at = time.time()
            self._stopped = False
            self._stopped_at = None

    def confirm_stopped(self):
        with self._lock:
            if self._desired is not True:
                raise OperationError(
                    "share_layer_failed",
                    "stopped 确认被拒绝：未先表达 desired-stop 意图"
                    "（watchdog 协调契约）")
            self._stopped = True
            self._stopped_at = time.time()

    def confirm_open(self):
        with self._lock:
            self._desired = False
            self._stopped = False
            self._expressed_at = None
            self._stopped_at = None

    def snapshot(self):
        with self._lock:
            return {"desired_stop_in_force": self._desired,
                    "stop_expressed_at": self._expressed_at,
                    "stopped_confirmed": self._stopped,
                    "stopped_confirmed_at": self._stopped_at}


_STATE = _ManagedStopState()


def _desired_stop_in_force():
    """bool|None: True = a stop intent is in force; False = a confirmed open
    superseded it; None = no orchestration-side authority (honest default).
    The injected reader (production wiring of the broker's protected record)
    takes precedence when wired."""
    if _DESIRED_STOP_READER is not None:
        return _DESIRED_STOP_READER()
    return _STATE.snapshot()["desired_stop_in_force"]


def _report(ctx, note):
    if ctx is not None:
        ctx.report_step(note)


def _cancel_point(ctx):
    if ctx is not None:
        ctx.raise_if_cancelled()


def _budget(params, key, default):
    raw = params.get(key) if isinstance(params, dict) else None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value < 0.0:
        return default
    return min(value, _BUDGET_MAX_S)



def _int_param(params, key, defaults):
    """Bounded int param (contract §1.3): the registry schema rejects an
    out-of-range value at submit time; a DIRECT handler call keeps the frozen
    default instead of silently widening the bound."""
    default, low, high = defaults
    raw = params.get(key) if isinstance(params, dict) else None
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default
    if raw < low or raw > high:
        return default
    return raw


def _float_param(params, key, default, high):
    """Positive bounded float for polling budgets/deadlines.

    The frozen registry schema still enforces the int 1..N contract at submit
    time; a DIRECT handler call (tests) may pass a small positive float, and
    the value is capped at the frozen upper bound — never widened.
    """
    raw = params.get(key) if isinstance(params, dict) else None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(value) or value <= 0.0:
        return float(default)
    return min(value, float(high))


def _bounded(text, limit=200):
    """One-line bounded text for notes/messages (contract §3.1)."""
    return " ".join(str(text).split())[:limit]


def _obs_word(value, unavailable="unavailable"):
    """A read-only observation word: True/False/None -> running/stopped/..."""
    return {True: "running", False: "stopped"}.get(value, unavailable)











def _keys_precondition_message(keys_ok):
    """Fail-closed message for the open keys precondition.

    With the diagnosis seam wired the concrete causes are SPLIT into the
    message with the observed numbers — "no enabled key" vs "config not
    in sync" (the 2026-09-22 production failures merged both into one
    undiagnosable line). An unwired diagnosis keeps the merged wording,
    a failing or shape-broken diagnosis reads as unknown. Either way the
    caller still raises: this never softens the fail-closed
    precondition."""
    observation = "（观察=%s）" % _obs_word(keys_ok, "unknown")
    if _KEYS_DIAGNOSIS is None:
        return ("开启前置：未确认存在已启用分享 Key 且配置已生效"
                "%s；不开启分享" % observation)
    try:
        diagnosis = _KEYS_DIAGNOSIS()
    except Exception:  # noqa: BLE001  unknown, not a fabricated split
        diagnosis = None
    reasons = diagnosis.get("reasons") if isinstance(diagnosis, dict) \
        else None
    if not reasons:
        return ("开启前置：未确认存在已启用分享 Key 且配置已生效%s；"
                "原因诊断不可得（unknown）；不开启分享" % observation)

    def count(key):
        value = diagnosis.get(key)
        return value if isinstance(value, int) \
            and not isinstance(value, bool) else 0

    in_sync_word = {True: "true", False: "false"}.get(
        diagnosis.get("in_sync"), "unknown")
    parts = []
    if "no_enabled_keys" in reasons:
        parts.append("缺已启用分享 Key（enabled_keys=%d，total_keys=%d）"
                     % (count("enabled_keys"), count("total_keys")))
    if "keys_read_failed" in reasons:
        parts.append("Key 清单读取失败")
    if "config_not_in_sync" in reasons:
        parts.append("配置未生效（in_sync=%s）" % in_sync_word)
    if "manager_unavailable" in reasons:
        parts.append("Key 管理观察不可得（unknown）")
    return "开启前置失败%s：%s；不开启分享" % (observation, "；".join(parts))












































def share_status(target, params, ctx=None):
    """READ-ONLY layered observation (no side effects; enabled).

    The share gateway is observed through the restricted broker gate.
    desired_stop_in_force is the orchestration-side managed-stop view
    (True = stop intent in force, False = confirmed open, None = no
    authority record in this process); the production authority stays the
    broker's protected lifecycle record.
    """
    _cancel_point(ctx)
    _report(ctx, "只读分层观察（无副作用，不触发任何启停）")
    try:
        running = _broker_ops().verify_running()
        gateway = {True: "running", False: "stopped"}.get(
            running, "unconfirmed")
    except OperationError:
        gateway = "unavailable"
    layers = {"share_gateway": gateway}
    desired = _desired_stop_in_force()
    if desired is True:
        desired_note = "停止意图生效中（stopped 终态仅在确认后写入）"
    elif desired is False:
        desired_note = "分享开启已确认（本进程无停止意图）"
    else:
        desired_note = ("无本进程停止意图记录；权威生命周期记录在 broker "
                        "受保护文件，由授权包接线读取")
    note = ("只读分层观察；unavailable 表示对应缝未接线/不可确认（如实，"
            "不冒充）；%s" % desired_note)
    return {"layers": layers, "desired_stop_in_force": desired,
            "note": note}


# ==================================================== Rev9 single-layer actions
# A9-1 share.diagnostics / A9-4 share.gateway_restart.
#
# These handlers do parameter validation, resource locking, operation creation
# and phase reporting ONLY (the registry/resource_fn do the lock and the row);
# the real work is the production adapter seam. 8100 is NEVER restarted via
# share.stop -> share.start (contract §0.6/§6.3): the only channel is the
# restricted broker gateway-restart verb.


def _probe(fn):
    """Run one read-only probe; returns (value, reason).

    Cancellation is never swallowed (it is not evidence); an OperationError
    yields its stable code, any other failure yields "probe_failed" — the caller
    records unavailable + reason, never a fabricated value.
    """
    try:
        return fn(), None
    except OperationCancelled:
        raise
    except OperationError as exc:
        return None, exc.code
    except Exception:  # noqa: BLE001
        return None, "probe_failed"


def _is_epoch(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)) and value > 0)


def _share_entry_probe():
    """Read-only 8100 instance probe (contract §3.2, production function).

    Lazy import keeps this module import-light; a missing production module or
    function reads as None (honest unavailable) — never a fabricated state.
    """
    try:
        from console import share_production as console_share_production
    except Exception:  # noqa: BLE001
        return None
    fn = getattr(console_share_production, "share_entry_instance_probe", None)
    if fn is None:
        return None
    return fn()


def _share_entry_attribution():
    """Read-only attribution status (contract §3.2, production function)."""
    try:
        from console import share_production as console_share_production
    except Exception:  # noqa: BLE001
        return None
    fn = getattr(console_share_production, "share_attribution_status", None)
    if fn is None:
        return None
    return fn()




def _maintenance_status_read():
    """(view, reason): broker protected maintenance view.

    view is a dict or None; reason names why None (unwired / read failed /
    shape broken) — never a fabricated view.
    """
    if _MAINTENANCE_STATUS is None:
        return None, "maintenance_unwired"
    try:
        raw = _MAINTENANCE_STATUS()
    except OperationCancelled:
        raise
    except OperationError as exc:
        return None, exc.code
    except Exception:  # noqa: BLE001
        return None, "maintenance_read_failed"
    if not isinstance(raw, dict):
        return None, "maintenance_unavailable"
    return raw, None


def _maintenance_pair(view):
    """The frozen {lease, instance, last_outcome} shape; null = absent."""
    if not isinstance(view, dict):
        return {"lease": None, "instance": None, "last_outcome": None}

    def sub(key):
        value = view.get(key)
        return value if isinstance(value, dict) else None

    return {"lease": sub("lease"), "instance": sub("instance"),
            "last_outcome": sub("last_outcome")}


def _last_error_from_view(view):
    """§8.5: last_outcome with ok is False passes through; else null."""
    if not isinstance(view, dict):
        return None
    outcome = view.get("last_outcome")
    if isinstance(outcome, dict) and outcome.get("ok") is False:
        return outcome
    return None


def _active_left(view):
    instance = view.get("instance") if isinstance(view, dict) else None
    if isinstance(instance, dict):
        return instance.get("active_count")
    return None


def _diag_layer(layer, state, verdict, source, *, checked_at=None,
                evidence=None, reason=None, timestamp_basis=None):
    """One frozen layer entry (contract §3.1).

    ``checked_at=None`` means THIS process obtained no evidence: age_s is null
    and stale is true. ``reason`` is a stable code — never a path, key, token
    or request body.
    """
    now = time.time()
    ev = dict(evidence) if isinstance(evidence, dict) else {}
    if checked_at is None:
        age = None
        stale = True
        basis = None
    else:
        age = max(0.0, now - checked_at)
        stale = age > _DIAG_TTL_S[layer]
        basis = timestamp_basis or "local_read"
    if basis is not None:
        ev["timestamp_basis"] = basis
    return {"state": state, "verdict": verdict, "source": source,
            "checked_at": checked_at, "age_s": age,
            "ttl_s": _DIAG_TTL_S[layer], "stale": stale,
            "reason": reason, "evidence": ev}


def _diag_share_entry():
    source = "broker:host-verify{running}+instance-probe+attribution"
    ops = _BROKER_OPS
    if ops is None:
        return _diag_layer("share_gateway", "unavailable", "unavailable",
                           source, reason="share_broker_unwired")
    running, reason = _probe(ops.verify_running)
    if reason is not None:
        return _diag_layer("share_gateway", "unavailable", "unavailable",
                           source, reason=reason)
    state = {True: "running", False: "stopped"}.get(running, "unconfirmed")
    verdict = "unconfirmed" if running is None else "confirmed"
    evidence = {}
    probe, probe_reason = _probe(_share_entry_probe)
    evidence["instance_probe"] = probe if isinstance(probe, dict) else None
    if not isinstance(probe, dict):
        evidence["instance_probe_reason"] = \
            probe_reason or "probe_unavailable"
    attribution, attr_reason = _probe(_share_entry_attribution)
    evidence["attribution"] = attribution \
        if isinstance(attribution, dict) else None
    if not isinstance(attribution, dict):
        evidence["attribution_reason"] = \
            attr_reason or "attribution_unavailable"
    faults = None
    faults_fn = getattr(ops, "host_evidence", None)
    if faults_fn is not None:
        faults, _faults_reason = _probe(lambda: faults_fn("faults"))
    evidence["faults"] = faults if isinstance(faults, dict) else None
    if not isinstance(faults, dict):
        # §8.6: no fabricated "no faults" — absent evidence reads absent.
        evidence["faults_reason"] = "fault_log_absent"
    return _diag_layer("share_gateway", state, verdict, source,
                       checked_at=time.time(), evidence=evidence)








def _diag_security_gate():
    source = "broker:host-evidence{limits}"
    ops = _BROKER_OPS
    fn = getattr(ops, "host_evidence", None) if ops is not None else None
    if fn is None:
        return _diag_layer("security_gate", "unavailable", "unavailable",
                           source, reason="host_evidence_unwired")
    raw, reason = _probe(lambda: fn("limits"))
    if reason is not None:
        return _diag_layer("security_gate", "unavailable", "unavailable",
                           source, reason=reason)
    if not isinstance(raw, dict):
        return _diag_layer("security_gate", "unconfirmed", "unconfirmed",
                           source, reason="limits_unconfirmed")
    verdict = raw.get("verdict")
    checked_at = float(raw["checked_at"]) \
        if _is_epoch(raw.get("checked_at")) else None
    evidence = {
        "check": "limits",
        "verdict": verdict if isinstance(verdict, bool) else None,
        "detail": _bounded(raw.get("detail")),
    }
    if verdict is True:
        state = "ok"
    elif verdict is False:
        state = "lost"
    else:
        state = "unconfirmed"
    layer_verdict = "confirmed" if isinstance(verdict, bool) \
        else "unconfirmed"
    return _diag_layer("security_gate", state, layer_verdict, source,
                       checked_at=checked_at, evidence=evidence)






def diagnostics(target, params, ctx=None):
    """Layered read-only diagnostics.

    Global and never-raising per layer: one failing seam only marks THAT layer
    ``unavailable`` + reason, the report itself never raises. Read-only: it
    never writes the intent, never writes a PID/token/ledger and never
    triggers SSO or a model request.
    """
    _cancel_point(ctx)
    _report(ctx, "分层只读诊断：任一缝失败只让该层 unavailable，整体不抛")
    now = time.time()
    layers = {}
    _report(ctx, '核验分享入口：受限任务、进程身份和监听')
    layers["share_gateway"] = _diag_share_entry()
    _cancel_point(ctx)
    _report(ctx, '核验 WFP 隔离和出站防火墙')
    layers["security_gate"] = _diag_security_gate()
    view, maintenance_reason = _maintenance_status_read()
    maintenance = _maintenance_pair(view)
    try:
        desired = _desired_stop_in_force()
    except Exception:  # noqa: BLE001  a failed read is no authority record
        desired = None
    last_error = _last_error_from_view(view)
    unavailable = [key for key in _DIAG_LAYER_KEYS
                   if layers[key]["state"] == "unavailable"]
    parts = ["只读全域诊断：unavailable 表示该层证据不可得（如实，不冒充）",
             "进程在 ≠ 业务就绪；非计费目录就绪 ≠ 真实推理验收"]
    if maintenance_reason == "maintenance_unwired":
        parts.append("维护视图未接线")
    elif maintenance_reason is not None:
        parts.append("维护视图不可得（%s）" % maintenance_reason)
    if unavailable:
        parts.append("证据不可得层：%s" % ",".join(unavailable))
    return {"checked_at": now, "layers": layers, "maintenance": maintenance,
            "desired_stop_in_force": desired, "last_error": last_error,
            "note": _bounded("；".join(parts))}




def _maintenance_begin_call(component, lease_s):
    """Call the injected maintenance-begin seam; its OperationError codes
    (share_broker_unwired / share_broker_extension_missing /
    share_capability_missing / share_maintenance_refused) propagate verbatim.
    """
    if _MAINTENANCE_BEGIN is None:
        raise OperationError(
            "share_broker_unwired",
            "维护租约缝未接线（broker 门未装配）；不重启目标")
    try:
        raw = _MAINTENANCE_BEGIN(component, lease_s)
    except OperationCancelled:
        raise
    except OperationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OperationError(
            "share_broker_unwired",
            "维护租约调用异常（%s）：不重启目标" % type(exc).__name__)
    if not isinstance(raw, dict) or not isinstance(raw.get("lease_id"), str) \
            or not raw["lease_id"]:
        raise OperationError(
            "share_broker_unwired",
            "维护租约结果形状不符合契约（缺 lease_id）：不重启目标")
    return raw


def _maintenance_end_call(lease_id):
    """(ok, fact, view): call the injected maintenance-end seam.

    Never raises: a failed resume is reported as an honest fact (the caller
    decides whether that is a failure of the action).
    """
    if _MAINTENANCE_END is None:
        return False, "维护结束缝未接线", None
    try:
        raw = _MAINTENANCE_END(COMPONENT_SHARE_GATEWAY, lease_id)
    except OperationCancelled:
        return False, "维护结束被取消", None
    except OperationError as exc:
        return False, "维护结束失败（%s）" % exc.code, None
    except Exception as exc:  # noqa: BLE001
        return False, "维护结束异常（%s）" % type(exc).__name__, None
    if not isinstance(raw, dict):
        return False, "维护结束结果形状不符合契约", None
    ok = raw.get("resumed") is True
    fact = "resumed=%s，state=%s，active_count=%s" % (
        raw.get("resumed"), raw.get("state"), raw.get("active_count"))
    return ok, fact, raw


def _resumed_word(resumed):
    if resumed.get("ok") is True:
        return "true"
    if resumed.get("ok") is False:
        return "false"
    return "unknown"


def _lease_expires_at(lease):
    if isinstance(lease, dict):
        return lease.get("lease_expires_at")
    return None


def _drain_wait(ctx, lease_id, round_, drain_budget_s, interval_s):
    """(drained, view, reason): bounded poll of the maintenance view until
    active_count == 0 or the drain budget. NEVER stops the target (§9.4).
    """
    deadline = time.monotonic() + float(drain_budget_s)
    view = None
    while True:
        _cancel_point(ctx)
        view, reason = _maintenance_status_read()
        if view is None:
            return False, None, reason
        lease = view.get("lease")
        if not isinstance(lease, dict) or lease.get("lease_id") != lease_id:
            return False, view, "lease_mismatch"
        if view.get("lease_expired") is True:
            return False, view, "lease_expired"
        instance = view.get("instance")
        if isinstance(instance, dict) and instance.get("state") == "stopped":
            return False, view, "instance_stopped"
        active = instance.get("active_count") \
            if isinstance(instance, dict) else None
        if isinstance(active, int) and not isinstance(active, bool) \
                and active <= 0:
            return True, view, None
        if time.monotonic() >= deadline:
            return False, view, "drain_timeout"
        _SLEEP(interval_s)


def _pre_start_recheck(lease_id, round_):
    """(ok, reason, view): re-verify intent and round right before the
    restart (§9.2/§9.3): a stop in force or a lease/round that moved aborts
    the restart — a late result must never reopen the share.
    """
    if _desired_stop_in_force() is True:
        return False, "desired_stop_in_force", None
    view, reason = _maintenance_status_read()
    if view is None:
        return False, reason, None
    lease = view.get("lease")
    if not isinstance(lease, dict):
        return False, "lease_missing", view
    if lease.get("lease_id") != lease_id:
        return False, "lease_replaced", view
    if round_ is not None and lease.get("round") != round_:
        return False, "round_changed", view
    if view.get("lease_expired") is True:
        return False, "lease_expired", view
    instance = view.get("instance")
    if isinstance(instance, dict) and instance.get("state") == "stopped":
        return False, "instance_stopped", view
    return True, None, view






def gateway_restart(target, params, ctx=None):
    """A9-4 restart 8100 only (contract §6).

    The ONLY channel is the restricted broker gateway-restart verb (host three
    evidence + instance/lease): never share.stop -> share.start. The limits
    security gate must be True before the call; False/None refuses. The broker
    call is blocking and bounded by the client; after it returns, a requested
    cancel is reported but the ACTUAL result is still returned (the side effect
    is committed — no fake rollback).
    """
    _cancel_point(ctx)
    _report(ctx, "8100 单层重启：九层只读快照 → 安全门（host-evidence limits）"
                 " → broker gateway-restart")
    params = params if isinstance(params, dict) else {}
    budget_s = _int_param(params, "restart_budget_s",
                          _GATEWAY_RESTART_DEFAULTS["restart_budget_s"])
    before_report = diagnostics("share", {}, ctx)
    before = before_report.get("layers") \
        if isinstance(before_report, dict) else {}
    ops = _BROKER_OPS
    limits_fn = getattr(ops, "host_evidence", None) if ops is not None else None
    if limits_fn is None:
        raise OperationError(
            "share_entry_restart_failed",
            "安全门证据不可得（broker host-evidence 未接线/缺席）：拒绝"
            " 8100 重启（硬门）")
    limits, limits_reason = _probe(lambda: limits_fn("limits"))
    if limits_reason is not None:
        raise OperationError(
            "share_entry_restart_failed",
            "安全门证据不可裁定（%s）：拒绝 8100 重启（硬门，未知 ≠ 通过）"
            % limits_reason)
    if not isinstance(limits, dict) or limits.get("verdict") is not True:
        raise OperationError(
            "share_entry_restart_failed",
            "安全门未通过（verdict=%s）：拒绝 8100 重启"
            % (limits.get("verdict") if isinstance(limits, dict) else limits))
    if _GATEWAY_RESTART is None:
        raise OperationError(
            "share_broker_unwired",
            "8100 重启缝未接线（broker 门未装配）；不以 share.stop→share.start"
            " 代替")
    _cancel_point(ctx)
    _report(ctx, "安全门已确认，执行受限 8100 单层重启")
    try:
        result = _GATEWAY_RESTART(budget_s)
    except OperationCancelled:
        raise
    except OperationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OperationError(
            "share_broker_unwired",
            "8100 重启调用异常（%s）：实际状态以 share.diagnostics 只读复验"
            "为准" % type(exc).__name__)
    if not isinstance(result, dict):
        raise OperationError(
            "share_entry_restart_failed", "8100 重启结果形状不符合契约")
    restarted = result.get("restarted") is True
    stopped = result.get("stopped_confirmed") is True
    running = result.get("running_confirmed") is True
    if not (restarted and stopped and running):
        raise OperationError(
            "share_entry_restart_failed",
            "8100 重启未三证确认（restarted=%s，stopped_confirmed=%s，"
            "running_confirmed=%s）；不伪报成功"
            % (restarted, stopped, running))
    if ctx is not None:
        try:
            ctx.raise_if_cancelled()
        except OperationCancelled:
            _report(ctx, "取消已请求，但 8100 重启副作用已提交：按实际结果"
                         "返回（不伪报回退）")
    _report(ctx, "8100 已确认重启，正在读取动作后的九层状态")
    after_report = diagnostics("share", {}, None)
    after = after_report.get("layers") \
        if isinstance(after_report, dict) else {}
    view, _reason = _maintenance_status_read()
    maintenance = _maintenance_pair(view)
    gateway = {
        "component": result.get("component", COMPONENT_SHARE_GATEWAY),
        "stopped_confirmed": stopped,
        "running_confirmed": running,
        "elapsed_s": result.get("elapsed_s"),
        "maintenance_state": result.get("maintenance_state"),
    }
    note = _bounded(
        "8100 单层重启完成（component=%s）：经 broker 受限宿主协议，未以"
        " share.stop→share.start 代替；私人 8000、Key/名称/身份及发布配置"
        "不变；入口重启期间业务可能暂时不可用"
        % gateway["component"])
    return {"restarted": True, "gateway": gateway,
            "maintenance": maintenance, "before": before, "after": after,
            "last_error": _last_error_from_view(view), "note": note}
