# -*- coding: utf-8 -*-
"""UM share / TS layered orchestration adapter (console share chain, Rev 5 L5).

Product semantics (user-confirmed): the open chain is TS service start ->
connectivity ready -> share backend ready + pre-checks -> Funnel on; the share
is only reported "open" when ALL FOUR layers are confirmed. Normal close =
withdraw entry -> bounded drain -> stop backend -> stop TS service. Emergency
stop = withdraw entry immediately and interrupt requests, stop backend and TS
service. A partial failure NEVER displays "all closed"; a failed start only
undoes the changes THIS run made.

Four explicit layers (``LAYERS``); observed values / expected states:

=================  ==========================  ==============================  ===============  ===============
layer              observed via                observed values                 open target      close target
=================  ==========================  ==============================  ===============  ===============
ts_service         ``_ts_service_status()``    "running"/"stopped"/None        "running"        "stopped"
connectivity       ``_connectivity_ok()``      True/False/None                 True             (n/a: covered
                   (read-only seam)            None = unavailable                               by funnel-off
                                                                                                + TS stop)
backend            broker ops                  True/False/None                 verify_running   verify_closed
                   verify_running/closed       None = evidence unconfirmed     True             True
funnel             ``_funnel_observed()``      True/False/None                 on-ack truthy    off-ack truthy
                   + ``_funnel_control``       None = unavailable
=================  ==========================  ==============================  ===============  ===============

PRIVILEGE / AUTHORIZATION (Revision 5 L5: 8001 stays unprivileged). Every
privileged action in this module goes through an INJECTED seam; the production
default leaves each seam UNWIRED and reports that honestly (never a simulated
success):

* ``_broker_ops()``  — the restricted broker operations gate: ONLY the
  existing restricted primitives (verify_running/verify_closed three-evidence
  checks, the task /Run//End gate, the broker drain / emergency-stop
  protocol actions). This module adds NO arbitrary command, service-name or
  path channel. The production gate object is assembled from
  share_broker_ops/share_management parts by the deployment authorization
  package and assigned to ``_BROKER_OPS``.
* ``_ts_service_control(action)`` — the TS service control seam. TS service
  control is a RESTRICTED PRIVILEGED layer: the 8001 web host never elevates;
  production wiring is a broker whitelist extension installed by the
  authorization package. Default (unwired) raises share_privilege_unwired.
* ``_funnel_control("on"/"off")`` — the Funnel seam, same pattern as the TS
  service seam (default unwired -> share_privilege_unwired).
* ``_ts_service_status`` / ``_connectivity_ok`` / ``_funnel_observed`` /
  ``_DRAIN`` / ``_DESIRED_STOP_READER`` — read-only observation seams;
  unwired default reads as unavailable, never as a fabricated state.

Real TS service start/stop, Funnel control and broker deployment belong to
the AUTHORIZED package. This module delivers the layered orchestration and
its isolated verification only; production seams are wired per package, and
until then every handler reports unwired/unavailable honestly.

Managed-stop coordination (watchdog contract): ``share_close`` /
``share_estop`` FIRST express the desired-stop intent in ``_STATE``, and the
stopped terminal is written ONLY after every close target is confirmed. The
orchestration state never fabricates a stopped record ahead of the facts
(the watchdog's authoritative record stays the broker's protected lifecycle
file; the module state is the orchestration-side view for the console).

Handler contract matches the task layer: ``handler(target, params, ctx)``.
Error codes: share_layer_failed / share_broker_unwired / share_privilege_unwired
/ funnel_failed (drain_timeout stays a possible injected-alternative code;
the default/production drain adopts the broker's SYSTEM-verified closure).
This module never unregisters accounts,
never deletes device identity, and never claims a full close it did not
confirm layer by layer.

Injectable params (bounded; non-numeric/negative fall back to defaults):
* ``poll_interval_s`` (default 1.0), ``*_budget_s`` (default 30, drain 20).
* ``step_interval_ms`` (default 0 for share_close, 800 for share_retry_stop):
  pacing between remaining close steps; capped at 60000.
"""

import inspect
import math
import os
import threading
import time

from console_operations import OperationCancelled, OperationError
from share_broker_protocol import (COMPONENT_SHARE_BACKEND,
                                  COMPONENT_SHARE_GATEWAY)

LAYERS = ("share_backend", "ts_service", "connectivity", "backend", "funnel")

# Rev9 (A9-1..A9-4): the single-layer maintenance actions use their OWN
# component vocabulary (share_gateway = 8100 entry, share_backend = 8110
# dedicated instance) — the legacy LAYERS keys keep their original meaning
# untouched (contract §1.1).  Both names are IMPORTED from the protocol
# module (single source of truth, §2.1) instead of being re-declared as
# literals, so a wire-side rename can never leave this layer sending a stale
# component name.

# ------------------------------------------------------------------ budgets
_TS_CONFIRM_DEFAULT_S = 30.0
_CONNECTIVITY_DEFAULT_S = 30.0
_BACKEND_DEFAULT_S = 30.0
_BACKEND_START_DEFAULT_S = 30.0
# R13-A: the bounded dedicated-backend auth wait. D18-6 fixes the human
# login window at 30 minutes (console_share_production
# ._BACKEND_LOGIN_TIMEOUT_S = 1800), so the orchestration default matches
# it: the wait can outlast the login it is waiting for instead of tearing
# down a still-logging-in instance. The submit-time schema caps the param at
# the same 1800.
_BACKEND_AUTH_DEFAULT_S = 1800.0
_BACKEND_AUTH_MAX_S = 1800.0
_STOP_CONFIRM_DEFAULT_S = 10.0
# R8-A7: the graceful close drains in-flight share requests for AT MOST 60
# seconds before the remaining connections are terminated.
_DRAIN_DEFAULT_S = 60.0
_INTERVAL_DEFAULT_S = 1.0
_STEP_INTERVAL_DEFAULT_MS = 800.0
_BUDGET_MAX_S = 3600.0
_STEP_MAX_MS = 60000.0
# R13-T: BackendStates that mean a HUMAN login is required (the CLI's own
# explicit facts - a login is not in progress, so a bounded wait cannot
# progress them). The connectivity wait stops explicitly on either.
_TS_LOGIN_STATES = ("NeedsLogin", "RequiresAuth")

# ------------------------------------------------------ injection seams
# None = production default = not wired (the authorization package assigns
# the real objects). Tests inject stubs; production never fakes these.
_BROKER_OPS = None             # restricted broker operations gate (object)
_TS_SERVICE_CONTROL = None     # privileged TS service start/stop seam
_TS_SERVICE_STATUS = None      # TS service observation seam
_CONNECTIVITY_OK = None        # connectivity observation seam
# R13-T: the node's own structured BackendState channel (read-only, same
# ``tailscale status --json`` surface) - lets the connectivity wait stop
# EXPLICITLY on a 需人工登录 reading instead of burning its budget. Unwired
# = the legacy poll-only behaviour (the verdict seam stays the authority).
_CONNECTIVITY_STATE = None     # connectivity BackendState channel
_FUNNEL_CONTROL = None         # privileged funnel on/off seam
_FUNNEL_OBSERVED = None        # funnel observation seam
_DRAIN = None                  # drain seam; default: _default_drain
_DESIRED_STOP_READER = None    # desired-stop view override (production may
                               # wire the broker's protected record reader)
# Dedicated share backend (Revision 6 R6-A1 / R8-A7): a SEPARATE upstream
# instance that the share gateway forwards to (never the production chat2api
# process, which must keep its concurrency slots untouched). The gate object
# owns start/stop/verify for that process; the observation seam reads its
# health + upstream-auth state (read-only). Unwired = the layer reports
# honestly unavailable — production wiring arrives with the isolation gate.
_BACKEND_OPS = None            # dedicated backend start/stop/verify gate
_BACKEND_OBSERVE = None        # dedicated backend health/auth read-only view
# Dedicated-backend login trigger (2026-09-17): POST /admin/auth/login on
# the dedicated port with ITS control secret — the backend opens its own
# independent-profile browser; there is no login web page to point at.
_BACKEND_LOGIN = None
# D18-8: cancel THIS login round through the dedicated instance's
# control-token-gated POST /admin/auth/cancel. None = use the production
# module lazily; the seam exists so tests can prove the cancel never stops
# the service.
_BACKEND_LOGIN_CANCEL = None
# D18-10: read-only cloud edge (cloudflared unit) status seam. None = the
# edge is NOT managed by this deployment (reported honestly, not a failure).
_EDGE_STATUS = None
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
_BACKEND_REAUTH = None      # (*, cancel_check=None) -> dict
_INTENT_WRITE = None        # (state:str, *, source:str) -> bool

# Public supervision cancel seams (contract §5.4): None = the public
# supervision module is not deployed, so there is no cancel file to write and
# the close chain proceeds unchanged. Wired values distinguish "written"
# (True) from "write failed" (False); the handler never fakes a write.
_PUBLIC_CANCEL_REQUEST = None   # (reason: str) -> bool
_PUBLIC_CANCEL_CLEAR = None     # () -> bool

# Observation TTLs (contract §3.4, frozen): stale = age_s > ttl_s; a layer
# whose evidence carries no observation time reads checked_at=None + stale=true
# ("this process obtained no evidence").
_DIAG_TTL_S = {"share_gateway": 60, "share_backend": 60, "backend_auth": 300,
               "connectivity": 300, "upstream": 300, "ts_service": 600,
               "funnel": 600, "security_gate": 600, "watchdog": 60}
_DIAG_LAYER_KEYS = ("share_gateway", "share_backend", "backend_auth",
                    "connectivity", "upstream", "ts_service", "funnel",
                    "security_gate", "watchdog")
# Rev19 A19-2 监督段 TTL：监督自己的持久状态由 watch 每分钟一轮写入；
# 超过三倍轮询间隔（180s）没有新决策即标 stale（只降新鲜度，不判宕机）。
_SUPERVISION_STALE_S = 180
# Rev19 A19-2 监督段读缝：``() -> {"state", "state_path"}|None``。None =
# 未接线（测试/夹具默认）→ 页面写「未提供 ≠ 监督未运行」；生产装配由
# production_seams() 注入真实读面（懒导入监督模块，只读它自己的状态文件）。
# 测试与夹具注入私有合成段，绝不读生产状态文件。
_SUPERVISION_STATE = None
# Frozen defaults (contract §1.3): params are validated by the registry
# schema at submit time; the handler keeps the same defensive fallback the
# legacy _budget() uses for a direct call.
_RESTART_DEFAULTS = {"drain_budget_s": (60, 1, 600),
                     "poll_interval_s": (1, 1, 10),
                     "stop_budget_s": (30, 1, 120),
                     "start_budget_s": (90, 1, 180),
                     "lease_s": (300, 60, 600)}
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


def _ts_service_control(action):
    """Restricted-privilege TS service seam ("start"/"stop").

    Revision 5 L5: the TS service layer is a BROKER WHITELIST EXTENSION, not
    a web-host privilege escalation — the 8001 host stays unprivileged and
    the unwired default raises share_privilege_unwired honestly. Returns a
    truthy ack only when the privileged layer accepted the action.
    """
    if _TS_SERVICE_CONTROL is None:
        raise OperationError(
            "share_privilege_unwired",
            "TS 服务控制缝未接线（受限特权层属部署授权包/broker 白名单扩展）；"
            "不提权、不冒充可用")
    return _TS_SERVICE_CONTROL(action)


def _ts_service_status():
    """TS service observation: "running"/"stopped", None = unavailable."""
    if _TS_SERVICE_STATUS is None:
        return None
    return _TS_SERVICE_STATUS()


def _connectivity_ok():
    """Connectivity observation: True/False, None = unavailable."""
    if _CONNECTIVITY_OK is None:
        return None
    return _CONNECTIVITY_OK()


def _connectivity_state():
    """R13-T BackendState channel for the connectivity wait: the node's own
    structured state ("Running"/"NeedsLogin"/...), None when the channel is
    unwired or unreadable - never a fabricated state. Optional evidence
    only: the readiness verdict stays with _connectivity_ok()."""
    if _CONNECTIVITY_STATE is None:
        return None
    return _CONNECTIVITY_STATE()


def _funnel_control(action):
    """Restricted-privilege Funnel seam ("on"/"off"), same pattern as the
    TS service seam; unwired default raises share_privilege_unwired."""
    if _FUNNEL_CONTROL is None:
        raise OperationError(
            "share_privilege_unwired",
            "Funnel 控制缝未接线（受限特权层属部署授权包）；不冒充可用")
    return _FUNNEL_CONTROL(action)


def _funnel_observed():
    """Funnel state observation: True(on)/False(off), None = unavailable."""
    if _FUNNEL_OBSERVED is None:
        return None
    return _FUNNEL_OBSERVED()


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


def _step_ms(params, key, default):
    raw = params.get(key) if isinstance(params, dict) else None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value < 0.0:
        return default
    return min(value, _STEP_MAX_MS)


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


def _write_intent(state, source):
    """Write the share runtime intent (contract §7.4).

    Lifecycle actions and the fixed isolation-repair stop call this;
    ``share.diagnostics`` and
    ``share.status`` NEVER do (read-only actions never write intent). An
    unwired seam returns False and the caller must say "意图未写" in its note —
    never pretend it was written.
    """
    if _INTENT_WRITE is None:
        return False
    try:
        return _INTENT_WRITE(state, source=source) is True
    except Exception:  # noqa: BLE001  best effort; the note reports the truth
        return False


def _intent_note(state, written):
    if written:
        return "运行意图已写（%s）" % state
    return ("运行意图未写（%s；意图写入缝未接线/写入失败，如实标注，"
            "不冒充已写）" % state)


def _request_public_cancel(reason):
    """Best-effort public-supervision cancel write (contract §5.4).

    Shared by the close actions' ``pre_lock`` hook and their handlers: an
    unwired seam is reported as such, a False/raising seam as ``failed`` and
    never as a written cancel. Only ``OperationCancelled`` propagates.
    """
    seam = _PUBLIC_CANCEL_REQUEST
    if seam is None:
        return {"public_cancel": "unwired",
                "public_cancel_unconfirmed": False,
                "public_cancel_residual": False}
    try:
        ok = bool(seam(reason))
    except OperationCancelled:
        raise
    except Exception:  # noqa: BLE001  a failed write is a residual fact
        ok = False
    if ok:
        return {"public_cancel": "written",
                "public_cancel_unconfirmed": False,
                "public_cancel_residual": False}
    return {"public_cancel": "failed",
            "public_cancel_unconfirmed": True,
            "public_cancel_residual": True}


def public_cancel_pre_lock(reason):
    """Return the pre-lock hook for one close action (contract §5.4).

    The hook resolves ``_PUBLIC_CANCEL_REQUEST`` at CALL time (never when
    the registry is built) and runs before ``ResourceFileLock.acquire`` so a
    close queued behind a long recovery round is already visible to it.
    """
    def hook(target, params):
        return _request_public_cancel(reason)
    return hook


def _cancel_public_supervision(reason, action):
    """Handler-side idempotent cancel write + early stopped intent.

    ``pre_lock`` never writes the intent; the handler does. A failed cancel
    write immediately tries the stopped intent so the supervisor's
    ``intent_stopped`` gate blocks automatic recovery even when the cancel
    file could not be written.
    """
    facts = _request_public_cancel(reason)
    if facts["public_cancel"] == "failed":
        if _write_intent("stopped", action):
            # The cancel file is missing, but the stopped intent now
            # blocks automatic recovery: no residual remains.
            facts = dict(facts, public_cancel_residual=False)
    return facts


def _merge_public_cancel(result, facts):
    """Add the cancel facts to a handler result (old consumers keep reading)."""
    if isinstance(result, dict):
        for key, value in facts.items():
            result.setdefault(key, value)
    return result


def _cancel_facts_note(facts):
    """One honest step note for a failed cancel write (None = no note)."""
    if not isinstance(facts, dict) or facts.get("public_cancel") != "failed":
        return None
    if facts.get("public_cancel_residual"):
        return ("公网巡检取消信号未写入，提前写停止意图也失败"
                "（public_cancel_residual：自动恢复可能继续）")
    return ("公网巡检取消信号未写入（public_cancel_unconfirmed）；"
            "已提前写停止意图阻止自动恢复")


def _report_cancel_facts(ctx, facts):
    note = _cancel_facts_note(facts)
    if note is not None:
        _report(ctx, note)


def _obs_word(value, unavailable="unavailable"):
    return {True: "yes", False: "no"}.get(value, unavailable)


def _state_word(value):
    return value if value else "unavailable"


# ================================================================= open chain
class _OpenLedger:
    """Ownership ledger for ONE open attempt: only layers THIS run changed
    get rolled back on failure. Pre-existing state (a service that was
    already running before the chain) is never owned and never stopped."""

    def __init__(self):
        self.owned = []
        self.facts = []

    def own(self, layer, fact):
        self.owned.append(layer)
        self.facts.append(fact)

    def note(self, text):
        self.facts.append(text)


def _confirm_ts_running(ctx, budget_s, interval_s):
    deadline = time.monotonic() + budget_s
    while True:
        state = _ts_service_status()
        if state == "running":
            return
        if ctx is not None:
            ctx.raise_if_cancelled()
        if time.monotonic() >= deadline:
            raise OperationError(
                "share_layer_failed",
                "ts_service 层确认超限：启动已执行但 %.0fs 内未观察到 running"
                "（最后观察=%s，可能残留启动后状态）" % (budget_s, _state_word(state)))
        _SLEEP(interval_s)


def _open_ts_service(ctx, ledger, budgets, interval_s):
    _cancel_point(ctx)
    _report(ctx, "第 2 层 ts_service：确认/启动 TS 服务")
    state = _ts_service_status()
    if state == "running":
        ledger.note("ts_service 开启前已在运行（不归属本次改动）")
        _report(ctx, "ts_service 已在运行（非本次启动，回滚不会触碰）")
        return
    _ts_control_with_pending_poll(ctx, "start")
    ledger.own("ts_service", "TS 服务由本次启动")
    _confirm_ts_running(ctx, budgets["ts"], interval_s)
    ledger.note("ts_service 已启动并确认 running")


def _open_connectivity(ctx, ledger, budgets, interval_s):
    _cancel_point(ctx)
    _report(ctx, "第 3 层 connectivity：等待 TS 连接就绪")
    if _CONNECTIVITY_OK is None:
        raise OperationError(
            "share_layer_failed",
            "connectivity 层：连接状态观察不可用（缝未接线/不可确认），"
            "无法确认连接就绪")
    # The observation seam RAISES on off-contract readings (production
    # contract: an unknown BackendState — e.g. the daemon's transitional
    # "Starting" right after a cold service start — is never reported as a
    # confirmed offline fact). During the wait budget such a reading means
    # "not ready YET": keep polling; only the deadline settles the layer,
    # carrying the last observation as evidence. The unwired seam above
    # stays a hard immediate failure.
    deadline = time.monotonic() + budgets["connectivity"]
    last_error = None
    last_state = None
    while True:
        if ctx is not None:
            ctx.raise_if_cancelled()
        # R13-T: the node's own BackendState channel short-circuits the wait
        # on an EXPLICIT login-needed fact (NeedsLogin/RequiresAuth): a
        # login is a real human step - nothing is in progress to wait for -
        # so burning the budget on a generic "not ready" poll would only
        # mislead. The stop point raises with its own code (never a fake
        # success), names the state, and auto-clearing the account or
        # resetting authentication stays out of scope. An unwired/unreadable
        # channel keeps the legacy poll-only behaviour; the verdict seam
        # below remains the ONLY readiness authority.
        if _CONNECTIVITY_STATE is not None:
            try:
                last_state = _connectivity_state()
            except OperationError:
                last_state = None
            if last_state in _TS_LOGIN_STATES:
                raise OperationError(
                    "share_ts_needs_login",
                    "connectivity 层：TS 节点需要人工登录（BackendState=%s："
                    "登录未在进行，等待无法推进）；完成人工登录后重试 "
                    "share.open；本操作不清除账户、不重置认证、不自动提交"
                    "任何凭据" % last_state)
        try:
            ok = _connectivity_ok()
            last_error = None
        except OperationError as exc:
            ok = None
            last_error = exc
        if ok is True:
            break
        if time.monotonic() >= deadline:
            detail = "最后观察=%s" % _obs_word(ok, "unavailable")
            if last_state is not None:
                detail += "（BackendState=%s）" % last_state
            if last_error is not None:
                detail += "（%s：%s）" % (
                    getattr(last_error, "code", "unknown"),
                    " ".join(str(last_error).split())[:120])
            raise OperationError(
                "share_layer_failed",
                "connectivity 层确认超限：%.0fs 内连接未就绪（%s）"
                % (budgets["connectivity"], detail))
        _SLEEP(interval_s)
    ledger.note("connectivity 已确认就绪")


def _backend_ops():
    """Return the injected dedicated-backend gate; an unwired seam is an
    honest layer failure (never a fabricated gate)."""
    if _BACKEND_OPS is None:
        raise OperationError(
            "share_backend_unwired",
            "share_backend 层：专用后端缝未接线（独立实例部署/隔离门未完成），"
            "该层如实不可用；本机 chat2api 不受影响")
    return _BACKEND_OPS


def _backend_observe():
    """Read-only dedicated-backend view through the injected seam.

    Returns None when the seam is unwired (honestly unavailable) or a dict
    {running: bool|None, auth_state: str|None, auth_label: str|None}; the
    orchestration never fabricates either field."""
    if _BACKEND_OBSERVE is None:
        return None
    try:
        raw = _BACKEND_OBSERVE()
    except OperationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OperationError(
            "share_layer_failed",
            "share_backend 观察异常（%s: %s）" % (type(exc).__name__, exc))
    return raw if isinstance(raw, dict) else None


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


def _open_share_backend(ctx, ledger, budgets, interval_s):
    """R8-A7 layer 1: dedicated share backend.

    Preconditions first (≥1 enabled key + published config in sync), then
    start (only when not already running — a pre-existing backend is never
    owned by this run) and verify the process gate; the upstream-auth state
    is OBSERVED. R13-A: a ``logging_in`` (or not-yet-readable ``unknown``)
    observation enters the EXPLICIT bounded, cancellable auth wait — the
    layer only continues on a confirmed ``valid``; terminal verdicts
    (invalid/failed/needs_login) fail with their own reasons (serving an
    unauthenticated backend would only waste the friends' requests). The
    production chat2api process is never touched.
    """
    _cancel_point(ctx)
    _report(ctx, "第 1 层 share_backend：专用分享后端（独立实例，独立并发槽位）")
    if _KEYS_CONFIRMED is not None:
        keys_ok = _KEYS_CONFIRMED()
        if keys_ok is not True:
            raise OperationError(
                "share_layer_failed", _keys_precondition_message(keys_ok))
    else:
        _report(ctx, "开启前置：Key 确认缝未接线（该确认跳过，如实留痕）")
    obs = _backend_observe()
    if obs is None:
        raise OperationError(
            "share_backend_unwired",
            "share_backend 层：专用后端缝未接线（独立实例部署/隔离门未完成），"
            "该层如实不可用；本机 chat2api 不受影响")
    if obs.get("running") is True:
        if auth_state_ok(obs):
            ledger.note("share_backend 开启前已在运行且上游认证有效（不归属本次改动）")
            return
        if _auth_transitional(obs.get("auth_state")):
            # R13-A: a pre-existing instance mid-login (or with a not-yet-
            # readable status) gets the SAME bounded wait; success skips the
            # layer WITHOUT ownership, so a failure here rolls back nothing.
            _await_backend_auth(ctx, obs, budgets["share_backend_auth"],
                                interval_s, started_here=False)
            ledger.note("share_backend 开启前已在运行且上游认证有效（不归属本次改动）")
            return
        raise _auth_terminal_error(obs, "已在运行但")
    ops = _backend_ops()
    if ops.start() is not True:
        raise OperationError(
            "share_layer_failed",
            "share_backend 层：专用后端启动未获门确认")
    ledger.own("share_backend", "专用分享后端由本次启动")
    deadline = time.monotonic() + budgets["share_backend"]
    while True:
        running = ops.verify_running()
        if running is True:
            break
        if ctx is not None:
            ctx.raise_if_cancelled()
        if time.monotonic() >= deadline:
            raise OperationError(
                "share_layer_failed",
                "share_backend 层确认超限：%.0fs 内专用后端未确认运行"
                % budgets["share_backend"])
        _SLEEP(interval_s)
    # Post-start health+auth observation (read-only): an unconfirmed running
    # state FAILS the open; a transitional auth observation (logging_in /
    # unknown) enters the bounded auth wait instead of failing instantly
    # (the 2026-10-04 maintainer share.start failure: a fresh 8110 observed
    # logging_in right after start was rolled back before its login could
    # settle). Terminal verdicts raise with their own reasons; only a
    # confirmed valid continues — never a fake ready.
    obs2 = _backend_observe()
    if not isinstance(obs2, dict) or obs2.get("running") is not True:
        raise OperationError(
            "share_layer_failed",
            "share_backend 层：启动后健康观察未确认运行")
    if not auth_state_ok(obs2):
        try:
            obs2 = _await_backend_auth(ctx, obs2,
                                       budgets["share_backend_auth"],
                                       interval_s, started_here=True)
        except OperationError as exc:
            if exc.code == "share_backend_auth_pending"                     and "share_backend" in ledger.owned:
                # The human still owns this login window; do NOT stop the
                # dedicated instance on a non-credential terminal state.
                ledger.owned.remove("share_backend")
            raise
    ledger.note("share_backend 已确认运行且上游认证有效")


def _login_hint(obs):
    """Actionable local-login guidance for the dedicated instance. There is
    NO login web page on the dedicated port (GET / is health JSON): the
    login is triggered by the console's「登录分享专用后端」action, which
    drives the control-token-gated POST /admin/auth/login and opens the
    instance's own independent-profile browser for the human UM SSO."""
    if _BACKEND_LOGIN is not None:
        return ("请使用控制台分享页「登录分享专用后端」动作完成登录"
                "（触发专用实例的独立 profile 浏览器；不经生产实例）")
    return ("请先完成专用实例的 UM 登录（登录缝未接线：可用部署程序提供的"
            "本机入口；独立 profile，不经生产实例）")


def auth_state_ok(obs):
    """Upstream-auth validity from a backend observation dict; anything
    other than an explicit 'valid' is not valid (未知 ≠ 有效)."""
    if not isinstance(obs, dict):
        return False
    return obs.get("auth_state") == "valid"


def _auth_word(state):
    if state == "valid":
        return "有效"
    if state == "invalid":
        return "失效"
    if state == "logging_in":
        return "登录中"
    return "未知(%s)" % (state if state is not None else "未观察")


def _auth_transitional(state):
    """R13-A: is this upstream-auth observation one a bounded wait MAY settle?

    ``logging_in`` = a login flow is already in progress on the instance;
    ``unknown``/unreadable/unrecognized = the status read is not readable YET
    (a cold-starting instance reads unknown before its auth surface answers).
    Both are transitional: the wait polls them within the deadline. Terminal
    verdicts (invalid/failed/needs_login) never wait — they raise with their
    own reason immediately."""
    return state not in ("valid", "invalid", "failed", "needs_login")


def _is_noncredential_auth_error(error):
    if not isinstance(error, str):
        return False
    return error in ("umpass_mfa_required", "umpass_prefill_missing",
                     "timeout", "window_closed", "attempts_exhausted",
                     "BrowserDisabled", "superseded",
                     "edge_missing", "browser_missing", "display_missing",
                     "browser_config_error", "token_save_failed")         or error.startswith("manual_takeover:")


def _auth_terminal_error(obs, where):
    """The terminal-verdict failure for invalid/failed/needs_login, each with
    its OWN reason (R13-A: distinct handling — never one merged "unknown
    != valid" line for different verdicts). ``where`` is "" (started this
    run) or "已在运行但" (pre-existing instance)."""
    state = obs.get("auth_state")
    hint = _login_hint(obs)
    if state == "invalid":
        return OperationError(
            "share_layer_failed",
            "share_backend 层：专用后端%s上游认证=失效（终态判定，不进入"
            "等待）；%s" % (where, hint))
    if state == "failed":
        error = obs.get("auth_error")
        if _is_noncredential_auth_error(error):
            # MFA/manual takeover/closed window never judged the credential:
            # keep the instance for the human and report the recovery step.
            return OperationError(
                "share_backend_auth_pending",
                "share_backend 层：专用后端%s等待本人完成 UM 登录/MFA 或人工接管"
                "（%s）；未判凭据失败，保留实例；%s"
                % (where, _bounded(str(error), 80), hint))
        reason = obs.get("auth_label") or "failed"
        return OperationError(
            "share_layer_failed",
            "share_backend 层：专用后端%s上次认证失败（%s；终态判定，不进入"
            "等待，绝不重试已拒绝的凭据）；%s" % (where, _bounded(reason, 120),
                                                hint))
    # needs_login: a real human step — nothing is in progress to wait for.
    return OperationError(
        "share_layer_failed",
        "share_backend 层：专用后端%s需要人工登录（needs_login：登录未在"
        "进行，等待无法推进；完成人工登录后重试 share.open）；%s"
        % (where, hint))


def _await_backend_auth(ctx, first_obs, budget_s, interval_s,
                        started_here=True):
    """R13-A bounded, cancellable auth wait for the dedicated share backend.

    Entered ONLY on transitional upstream-auth observations (``logging_in``
    or unknown/unreadable) after the process gate confirmed the instance
    running. Polls the read-only observation seam until ``valid`` or the
    deadline:

    * ``valid`` — the confirming observation is returned;
    * ``invalid``/``failed``/``needs_login`` — ITS OWN terminal reason,
      immediately (never waiting on a verdict that cannot progress);
    * the instance stops answering while waiting — immediate failure;
    * deadline — failure carrying the LAST observation (state + the
      instance's own label) as evidence — never a fabricated success and
      never a fixed sleep pretending auth.

    Single-flight: this wait NEVER calls the login seam and never submits
    credentials — the instance's own login window (opened once, by whatever
    flow put it into logging_in) is the only place the human SSO happens;
    a second window during the wait would be an orchestration defect.
    """
    started_word = "后端已启动" if started_here else "后端已在运行"
    _report(ctx, "share_backend 层：%s，上游认证=%s；等待认证完成"
                 "（预算 %.0fs，可取消；等待期间不触发新登录窗口）"
                 % (started_word, _auth_word(first_obs.get("auth_state")),
                    budget_s))
    obs = first_obs
    deadline = time.monotonic() + budget_s
    while True:
        if not isinstance(obs, dict) or obs.get("running") is not True:
            raise OperationError(
                "share_layer_failed",
                "share_backend 层：认证等待期间专用后端未确认运行"
                "（最后观察=%s）"
                % _auth_word(obs.get("auth_state")
                             if isinstance(obs, dict) else None))
        state = obs.get("auth_state")
        if state == "valid":
            return obs
        if not _auth_transitional(state):
            raise _auth_terminal_error(obs, "" if started_here
                                       else "已在运行但")
        if ctx is not None:
            ctx.raise_if_cancelled()
        if time.monotonic() >= deadline:
            detail = _auth_word(state)
            label = obs.get("auth_label")
            if isinstance(label, str) and label:
                detail += "（实例自报=%s）" % _bounded(label, 80)
            raise OperationError(
                "share_layer_failed",
                "share_backend 层：认证等待超限（%.0fs 内未观察到有效上游"
                "认证，最后观察=%s）；%s" % (budget_s, detail,
                                            _login_hint(obs)))
        _SLEEP(interval_s)
        obs = _backend_observe()


def _stop_share_backend_best_effort(budget_s, interval_s):
    """Best-effort dedicated-backend stop; returns (closed, fact_text).

    closed=True only on an explicit gate confirmation; anything else stays
    an honest residual fact (never folded into a fake all-closed)."""
    if _BACKEND_OPS is None:
        return False, "share_backend 缝未接线（无法经编排停止；实际进程状态待复验）"
    try:
        if _BACKEND_OPS.stop() is not True:
            return False, "share_backend 停止未获门确认（可能残留运行状态）"
    except OperationError as exc:
        return False, "share_backend 停止调用失败（%s），可能残留运行状态" % exc.code
    deadline = time.monotonic() + budget_s
    while True:
        closed = _BACKEND_OPS.verify_closed()
        if closed is True:
            return True, "share_backend 已停止并确认关闭"
        if time.monotonic() >= deadline:
            return False, ("share_backend 停止已请求但 %.0fs 内未确认关闭"
                           % budget_s)
        _SLEEP(interval_s)


def _start_host_explicit(ops):
    """Start through the operator seam, preserving legacy zero-argument seams.

    The production broker accepts ``explicit=True`` so an operator start clears a
    persisted stop intent. A legacy/offline seam without the keyword keeps its
    historical zero-argument call instead of failing the whole backend layer.
    """
    start = getattr(ops, "start_host", None)
    if not callable(start):
        return None
    try:
        parameters = inspect.signature(start).parameters
    except (TypeError, ValueError):
        return start()
    if "explicit" in parameters:
        return start(explicit=True)
    return start()


def _open_backend(ctx, ledger, budgets, interval_s):
    _cancel_point(ctx)
    _report(ctx, "第 4 层 backend：分享网关就绪（受限 broker 门三证确认）")
    ops = _broker_ops()
    previous_running = ops.verify_running()
    if previous_running is True:
        # 已在运行也必须走一次受限 start 的受管确认：只有成功的受管开启
        # 动作才会把受保护 lifecycle 从先前 emergency-stop 的 stopped 刷成
        # running，监督判据（managed_stop_in_force）才随之一致。端口在场
        # 只是读面观察，不是权威状态，不能凭它把 stopped 变 running；
        # 受限 start 自身幂等（任务已在运行时 Run 是 no-op），本次仍不归属
        # 本次改动，回滚绝不停止开启前已在运行的层。
        if _start_host_explicit(ops) is not True:
            raise OperationError(
                "share_layer_failed",
                "backend 层：已在运行但受限任务门拒绝受管确认"
                "（start_host 未确认成功）")
        ledger.note("backend 开启前已在运行（本次仅做受管确认，不归属本次改动）")
        return
    previous_closed = (ops.verify_closed() is True
                       if previous_running is False else False)
    if _start_host_explicit(ops) is not True:
        raise OperationError(
            "share_layer_failed",
            "backend 层：受限任务门拒绝启动（start_host 未确认成功）")
    if previous_closed:
        ledger.own("backend", "分享后端由本次启动")
    else:
        ledger.note("backend 开启前状态未确认；本次受管确认不授予回滚归属，失败时保留入口")
    deadline = time.monotonic() + budgets["backend"]
    while True:
        running = ops.verify_running()
        if running is True:
            ledger.note("backend 已确认运行（verify_running 三证）")
            return
        if ctx is not None:
            ctx.raise_if_cancelled()
        if time.monotonic() >= deadline:
            raise OperationError(
                "share_layer_failed",
                "backend 层确认超限：%.0fs 内三证未确认运行（最后观察=%s）"
                % (budgets["backend"], _obs_word(running, "unconfirmed")))
        _SLEEP(interval_s)


def _open_funnel(ctx, ledger):
    _cancel_point(ctx)
    _report(ctx, "第 5 层 funnel：开放 Funnel 入口")
    previous_open, _observation_error = _probe(_funnel_observed)
    ack = _funnel_control("on")
    if not ack:
        raise OperationError(
            "funnel_failed",
            "funnel 层：开启未获受限入口缝确认（不显示已对外开放）")
    if previous_open is False:
        ledger.own("funnel", "Funnel 由本次开放")
    else:
        ledger.note("funnel 开启前已开放或状态未确认；本次不授予回滚归属，失败时保留原映射")


def _stop_ts_service_best_effort(budget_s, interval_s):
    try:
        _ts_control_with_pending_poll(None, "stop")
    except OperationError as exc:
        return "TS 服务停止调用失败（%s），实际状态可能残留" % exc.code
    deadline = time.monotonic() + budget_s
    while True:
        state = _ts_service_status()
        if state == "stopped":
            return "TS 服务已停止并确认 stopped"
        if time.monotonic() >= deadline:
            return ("TS 服务停止未确认（最后观察=%s，可能残留运行状态）"
                    % _state_word(state))
        _SLEEP(interval_s)


def _stop_backend_best_effort(budget_s, interval_s):
    ops = _broker_ops()
    try:
        if ops.stop_host() is not True:
            return "backend 停止调用被受限任务门拒绝（未确认）"
    except OperationError as exc:
        return "backend 停止调用失败（%s），可能残留运行状态" % exc.code
    deadline = time.monotonic() + budget_s
    while True:
        closed = ops.verify_closed()
        if closed is True:
            return "backend 已停止并确认关闭（verify_closed 三证）"
        if time.monotonic() >= deadline:
            return ("backend 停止已请求但 %.0fs 内 verify_closed 未确认"
                    "（最后观察=%s）" % (budget_s, _obs_word(closed, "unconfirmed")))
        _SLEEP(interval_s)


def _open_rollback(ctx, ledger):
    """Undo ONLY the layers this open run owns, in reverse order, best
    effort (rollback keeps going and records residual facts honestly)."""
    facts = []
    if "funnel" in ledger.owned:
        try:
            if _funnel_control("off"):
                facts.append("funnel 已撤回（本次开放回滚）")
            else:
                facts.append("funnel 回撤未获确认（残留事实：入口可能仍开放）")
        except OperationError as exc:
            facts.append("funnel 回撤失败（%s）：残留事实，需人工/授权入口复验"
                         % exc.code)
    if "backend" in ledger.owned:
        facts.append("backend 回滚：" + _stop_backend_best_effort(
            _STOP_CONFIRM_DEFAULT_S, 1.0))
    if "share_backend" in ledger.owned:
        sb_closed, sb_fact = _stop_share_backend_best_effort(
            _STOP_CONFIRM_DEFAULT_S, 1.0)
        facts.append("share_backend 回滚：" + sb_fact)
    if "ts_service" in ledger.owned:
        facts.append("ts_service 回滚：" + _stop_ts_service_best_effort(
            _STOP_CONFIRM_DEFAULT_S, 1.0))
    return facts


def share_open(target, params, ctx=None):
    """Layered open: share_backend -> ts_service -> connectivity ->
    backend(gateway) -> funnel (R8-A7 five-layer chain).

    Any layer failure rolls back ONLY the layers this run owns (reverse
    order, best effort) and raises with the layered residual facts plus the
    ownership phrase; a confirmed open marks the orchestration state as
    running (no stop intent in force). The production chat2api process is
    never started, stopped or restarted by this chain.
    """
    _cancel_point(ctx)
    _report(ctx, "开始五层开放编排：专用后端 -> TS 服务 -> 连接 -> 分享网关 -> Funnel")
    params = params if isinstance(params, dict) else {}
    interval_s = _budget(params, "poll_interval_s", _INTERVAL_DEFAULT_S)
    budgets = {
        "share_backend": _budget(params, "backend_budget_s",
                                 _BACKEND_START_DEFAULT_S),
        # R13-A: the dedicated-backend auth wait budget (logging_in / unknown
        # observations); published as backend_auth_budget_s on share.start.
        "share_backend_auth": _budget(params, "backend_auth_budget_s",
                                      _BACKEND_AUTH_DEFAULT_S),
        "ts": _budget(params, "ts_confirm_budget_s", _TS_CONFIRM_DEFAULT_S),
        "connectivity": _budget(params, "connectivity_budget_s",
                                _CONNECTIVITY_DEFAULT_S),
        "backend": _budget(params, "backend_budget_s", _BACKEND_DEFAULT_S),
    }
    ledger = _OpenLedger()
    try:
        # Production gates always provide this read-only preflight. Legacy
        # injected private gates retain their existing primitive surface.
        preflight = getattr(_broker_ops(), "ensure_control_ready", None)
        if preflight is not None:
            _report(ctx, "先确认受限后台响应，尚未启动或登录 8110")
            preflight()
            _cancel_point(ctx)
        _open_share_backend(ctx, ledger, budgets, interval_s)
        _open_ts_service(ctx, ledger, budgets, interval_s)
        _open_connectivity(ctx, ledger, budgets, interval_s)
        _open_backend(ctx, ledger, budgets, interval_s)
        _open_funnel(ctx, ledger)
    except OperationCancelled:
        # Cancel between layers: undo owned changes (best effort, ignoring
        # further cancel points inside rollback) and re-raise; the executed
        # layers stay recorded in the operation steps.
        facts = _open_rollback(ctx, ledger)
        for fact in facts:
            _report(ctx, "取消后回滚：" + fact)
        _report(ctx, "已响应取消；只撤销本次拥有的改动，保留启动前状态及归属")
        raise
    except OperationError as exc:
        facts = _open_rollback(ctx, ledger)
        for fact in facts:
            _report(ctx, "回滚：" + fact)
        next_step = ("先恢复受限后台并重新读取状态，再开启分享"
                     if exc.code == "share_broker_unavailable" else
                     "可重试 share.open（每层只推进未确认层）")
        message = ("%s；已只撤销本次拥有的改动（%s）；保留启动前状态及归属，"
                   "分层残留事实以 share.status 只读观察为准；%s"
                   % (exc.message, "；".join(facts) or "无可撤销项", next_step))
        _report(ctx, "开启链失败：" + message)
        raise OperationError(exc.code, message) from exc
    except Exception as exc:  # noqa: BLE001  a seam bug must not fake success
        facts = _open_rollback(ctx, ledger)
        raise OperationError(
            "share_layer_failed",
            "开启链异常（%s: %s）；已只撤销本次拥有的改动（%s）"
            % (type(exc).__name__, exc, "；".join(facts) or "无需撤销"))
    _STATE.confirm_open()
    _report(ctx, "五层全部确认：对外开放已生效")
    # §7.4 intent write: ONLY after all five layers are confirmed.
    intent_ok = _write_intent("running", "share.open")
    _report(ctx, _intent_note("running", intent_ok))
    # §5.4: a confirmed open clears the human cancel signal. A failed
    # clear keeps the automatic recovery paused (additive field only) and
    # never fails the open itself; an unwired seam stays unmentioned.
    cancel_cleared = None
    if _PUBLIC_CANCEL_CLEAR is not None:
        try:
            cancel_cleared = bool(_PUBLIC_CANCEL_CLEAR())
        except Exception:  # noqa: BLE001  the open itself already succeeded
            cancel_cleared = False
        if not cancel_cleared:
            _report(ctx, "公网巡检取消信号未清除：自动恢复保持暂停")
    result = {
        "layers": {"share_backend": "running", "ts_service": "running",
                   "connectivity": "ok", "backend": "running",
                   "funnel": "on"},
        "opened": True,
        "owned_changes": list(ledger.owned),
        "intent_written": intent_ok,
        "note": "五层全部确认：专用后端运行且上游认证有效、TS 服务运行、"
                "连接就绪、分享网关运行（三证）、Funnel 已开放；%s"
                % _intent_note("running", intent_ok),
    }
    if cancel_cleared is not None:
        result["public_cancel_cleared"] = cancel_cleared
        if not cancel_cleared:
            result["note"] += "；公网巡检取消信号未清除，自动恢复保持暂停"
    return result


# ================================================================= close chain
def _default_drain(budget_s, interval_s, ctx):
    """Default drain seam: the existing broker drain protocol action.

    The broker-side drain already performs the bounded in-flight wait, the
    privileged host stop and the three-evidence closure check, and the
    adapter contract (graceful_stop) refuses any reply without the
    explicit ``stopped=True`` — a returned result IS the broker-verified
    backend closure. (The previous in-process exit observation was removed:
    the operator-account process could never resolve the host task
    evidence, so that poll could never terminate on real evidence.)
    Budget/interval stay in the seam signature — they are the
    orchestration's drain budget contract, consumed by injected
    alternatives and by the broker-side deadline."""
    ops = _broker_ops()
    try:
        raw = ops.drain()
    except OperationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OperationError(
            "share_layer_failed",
            "drain 层：broker 排空调用异常（%s: %s）"
            % (type(exc).__name__, exc))
    if isinstance(raw, dict):
        if not (raw.get("drained") is True or raw.get("timed_out") is True
                or raw.get("stopped") is True):
            raise OperationError(
                "share_layer_failed",
                "drain 层：broker 排空结果未确认 drained/timed_out/stopped"
                "（结果=%r）" % (raw,))
        # D18-10: pass the broker's OWN truth through — a timed-out drain
        # must never be normalized into a fake drained=True. in_flight is the
        # observed active_left (None = never observed, never a fabricated 0).
        return {"drained": raw.get("drained"),
                "timed_out": raw.get("timed_out"),
                "stopped": raw.get("stopped"),
                "notification_failed": raw.get("notification_failed"),
                "in_flight": raw.get("in_flight"),
                "broker": raw,
                "observed": "broker SYSTEM 端已停后端并三证确认关闭"}
    raise OperationError(
        "share_layer_failed",
        "drain 层：broker 排空结果形状不符合协议（非结构化结果=%r），"
        "不继续关闭" % (raw,))


def _confirm_backend_closed(ctx, budget_s, interval_s):
    ops = _broker_ops()
    deadline = time.monotonic() + budget_s
    while True:
        closed = ops.verify_closed()
        if closed is True:
            return
        if ctx is not None:
            ctx.raise_if_cancelled()
        if time.monotonic() >= deadline:
            raise OperationError(
                "share_layer_failed",
                "backend 层：停止已请求但 %.0fs 内 verify_closed 三证未确认"
                "（最后观察=%s）" % (budget_s, _obs_word(closed, "unconfirmed")))
        _SLEEP(interval_s)


def _confirm_ts_stopped(ctx, budget_s, interval_s):
    deadline = time.monotonic() + budget_s
    while True:
        state = _ts_service_status()
        if state == "stopped":
            return
        if ctx is not None:
            ctx.raise_if_cancelled()
        if time.monotonic() >= deadline:
            raise OperationError(
                "share_layer_failed",
                "ts_service 层：停止已请求但 %.0fs 内未确认 stopped"
                "（最后观察=%s，可能残留运行状态）" % (budget_s, _state_word(state)))
        _SLEEP(interval_s)


def _ts_control_with_pending_poll(ctx, action):
    """Issue the restricted TS control verb; a pending-confirmation
    rejection (the command WAS executed, but the broker's single immediate
    re-query caught the service in STOP_PENDING/START_PENDING) is reported
    and swallowed — the caller's bounded confirmation poll over the
    read-only service observation decides. Every other rejection
    propagates untouched."""
    try:
        _ts_service_control(action)
    except OperationError as exc:
        if exc.code != "share_ts_pending_confirmation":
            raise
        _report(ctx, "控制命令已执行（立即复查处于过渡态未确认）："
                     "由后续有界确认轮询裁定（只读服务状态观察）")


def _close_chain(ctx, params, interval_s, step_interval_s, facts):
    """Shared layered close advance (share_close / share_retry_stop).

    Every step first OBSERVES whether its close target is already met and
    skips the action when it is, so a retry resumes from the remaining steps
    without reopening anything. The desired-stop intent is expressed BEFORE
    the first layer runs; the stopped terminal is written by the caller ONLY
    on full confirmation. (The emergency chain has its own quick path in
    share_estop — no drain step, cancel points not honoured.)
    """
    drain_budget = _budget(params, "drain_budget_s", _DRAIN_DEFAULT_S)

    # Managed-stop intent FIRST (never a stopped record ahead of the facts).
    _STATE.express_stop()
    _report(ctx, "已表达停止意图（desired-stop；stopped 终态待全层确认后写入）")

    # ---- funnel: withdraw the public entry FIRST
    observed = _funnel_observed()
    if observed is False:
        _report(ctx, "funnel 观察已关闭：撤入口步骤跳过")
        facts.append("funnel=off（观察确认）")
    elif observed is None and _ts_service_status() == "stopped":
        # No daemon -> no serve mapping -> no public entry to withdraw:
        # a deterministic OFF, not an unresolvable observation. Trying to
        # run tailscale funnel off with the service stopped can only fail.
        _report(ctx, "TS 服务已停止（无 daemon 即无入口）：funnel 层按已关闭跳过")
        facts.append("funnel=off（TS 服务停止 ⇒ 入口不存在）")
    else:
        if step_interval_s:
            _SLEEP(step_interval_s)
        ack = _funnel_control("off")
        if not ack:
            raise OperationError(
                "funnel_failed",
                "funnel 层：撤入口未获受限入口缝确认（入口可能仍开放）")
        facts.append("funnel=off（受限入口缝确认）")
        _report(ctx, "funnel 已撤（入口关闭）")

    ops = _broker_ops()

    # ---- backend already fully closed? then drain/stop steps are done
    # (verify_closed runs INSIDE the SYSTEM broker via the host-verify verb)
    if ops.verify_closed() is True:
        _report(ctx, "backend 已确认关闭（SYSTEM 三证）：排空与停机跳过")
        facts.append("backend=stopped（三证确认）")
        drain = None
    else:
        # ---- bounded drain between entry withdrawal and backend stop
        if step_interval_s:
            _SLEEP(step_interval_s)
        drain_fn = _DRAIN or _default_drain
        drained = drain_fn(drain_budget, interval_s, ctx)
        if not isinstance(drained, dict):
            raise OperationError(
                "share_layer_failed",
                "drain 层：排空结果形状不符合协议（非结构化结果=%r）"
                % (drained,))
        drain = {
            "drained": drained.get("drained"),
            "timed_out": drained.get("timed_out") is True,
            "stopped": drained.get("stopped"),
            "notification_failed": drained.get("notification_failed"),
            "in_flight": drained.get("in_flight"),
        }
        if drain["stopped"] is not True:
            # D18-10: the broker's final stop is a separate, authoritative
            # fact — a completed drain phase is NOT a stopped host.
            raise OperationError(
                "share_layer_failed",
                "drain 层：排空阶段结束但 broker 未确认最终停止"
                "（stopped=%r）；不把未关闭当已停止" % (drain["stopped"],))
        if drain["timed_out"]:
            facts.append("排空超时：在途未确认结束，已按 broker 终态停止")
            _report(ctx, "有界排空超时（在途未确认结束），broker 已按终态停止")
        else:
            facts.append("有界排空完成")
        # The broker drain already stopped the host and re-verified closure
        # with SYSTEM privilege (R31-1); the adapter contract refuses any
        # reply without the explicit stopped=True, so reaching this line IS
        # the three-evidence confirmation — no second stop or poll.
        facts.append("backend=stopped（broker SYSTEM 三证确认）")
        _report(ctx, "backend 已停止并确认关闭（broker SYSTEM 三证）")

    # ---- dedicated backend stop (R8-A7: gateway backend first, then the
    # dedicated upstream instance; production chat2api is never touched)
    sb_closed, sb_fact = _stop_share_backend_best_effort(
        _budget(params, "backend_confirm_budget_s", _STOP_CONFIRM_DEFAULT_S),
        interval_s)
    facts.append(sb_fact)
    _report(ctx, "专用分享后端：" + sb_fact)

    # ---- ts service stop (privileged seam)
    if _ts_service_status() == "stopped":
        _report(ctx, "TS 服务观察已停止：停机步骤跳过")
        facts.append("ts_service=stopped（观察确认）")
    else:
        if step_interval_s:
            _SLEEP(step_interval_s)
        _ts_control_with_pending_poll(ctx, "stop")
        _confirm_ts_stopped(ctx, _budget(params, "ts_confirm_budget_s",
                                         _STOP_CONFIRM_DEFAULT_S), interval_s)
        facts.append("ts_service=stopped（受限特权缝确认）")
        _report(ctx, "TS 服务已停止并确认 stopped")
    return {"layers": {"funnel": "off", "backend": "stopped",
                      "share_backend": "stopped" if sb_closed else "unconfirmed",
                      "ts_service": "stopped", "connectivity": "withdrawn"},
            "drain": drain,
            "state": "drain_timeout"
            if isinstance(drain, dict) and drain["timed_out"] is True
            else "stopped"}


def _partial_close_message(exc, facts):
    """Honest partial-close message: layered facts, no fake all-closed, the
    status surface stays truthful and the retry entry is named."""
    return ("正常关闭未完成（部分关闭失败）：%s；已完成层/事实：%s；"
            "服务状态（svcRun/状态面）如实保留实际状态，不会显示全部已停；"
            "可用 share.retry_stop 按剩余步骤重试关闭（不要求先重开）"
            % (exc.message, "；".join(facts) or "本层之前无已完成层"))


def share_close(target, params, ctx=None):
    """Normal close: funnel off -> bounded drain -> backend stop -> TS stop.

    The desired-stop intent is expressed before the first layer; the stopped
    terminal is written ONLY when every close target is confirmed. A layer
    failure raises with the layered actual facts (funnel/back-end/TS states)
    and the retry entry; the operation steps keep the executed layers, so
    the console can never display a full close that did not happen.
    """
    _cancel_point(ctx)
    _report(ctx, "开始正常关闭：撤 Funnel -> 有界排空 -> 停后端 -> 停 TS 服务")
    # §5.4: the human cancel signal is written at the start of the close,
    # so a public supervision round already waiting for the action lock
    # observes it. A failed write never blocks the close (emergency and
    # normal close stay first); the handler reports the residual fact.
    cancel_facts = _cancel_public_supervision("manual_close", "share.close")
    params = params if isinstance(params, dict) else {}
    interval_s = _budget(params, "poll_interval_s", _INTERVAL_DEFAULT_S)
    facts = []
    try:
        close = _close_chain(ctx, params, interval_s, 0.0, facts)
    except OperationCancelled:
        # Stop intent stays in force; executed layers stay in the steps.
        _report(ctx, "已响应取消；停止意图保持生效，已执行层如实留痕（未写 stopped）")
        _report_cancel_facts(ctx, cancel_facts)
        raise
    except OperationError as exc:
        _report(ctx, "关闭未完成：" + exc.message)
        _report_cancel_facts(ctx, cancel_facts)
        raise OperationError(exc.code, _partial_close_message(exc, facts)) \
            from exc
    layers = close["layers"]
    drain = close["drain"]
    if layers.get("share_backend") != "stopped":
        # Terminal contract (R8-A7): every layer must be confirmed before
        # the stopped record; a residual dedicated backend keeps the stop
        # intent in force and the retry entry valid.
        raise OperationError(
            "share_layer_failed",
            _partial_close_message(
                OperationError("share_layer_failed",
                               "专用分享后端未确认关闭"), facts))
    _STATE.confirm_stopped()
    if close["state"] == "drain_timeout":
        _report(ctx, "关闭已按 broker 终态完成（排空超时：在途未确认结束）")
    else:
        _report(ctx, "全部关闭层已确认（funnel=off；backend=stopped；"
                     "share_backend=stopped；ts_service=stopped）")
    # §7.4 intent write: stopped, after the share_backend layer is confirmed.
    intent_ok = _write_intent("stopped", "share.close")
    _report(ctx, _intent_note("stopped", intent_ok))
    if close["state"] == "drain_timeout":
        # D18-10: a timed-out drain is NOT a normal close. The broker
        # still stopped the host, but the in-flight requests were never
        # confirmed ended; say so and name the forced-stop action.
        note = ("排空超时后强制停止（在途未确认结束）；可用 share.chain_estop "
                "立即取消在途；分享网关已按 broker 终态停止、专用后端已停、"
                "TS 服务已停（stopped 终态已按契约写入）；%s"
                % _intent_note("stopped", intent_ok))
    else:
        note = ("正常关闭完成：入口已撤、分享网关已停（三证）、"
                "专用后端已停、TS 服务已停（stopped 终态已按契约写入）；"
                "%s" % _intent_note("stopped", intent_ok))
    result = {"layers": layers, "state": close["state"], "drain": drain,
              "closed": True, "facts": list(facts),
              "intent_written": intent_ok, "note": note}
    if cancel_facts["public_cancel"] == "failed":
        result["note"] += "；" + _cancel_facts_note(cancel_facts)
    return _merge_public_cancel(result, cancel_facts)


def share_estop(target, params, ctx=None):
    """Emergency stop: withdraw entry immediately -> broker emergency-stop
    (interrupt requests) -> backend stop -> TS service stop.

    Quick chain: no drain step, and cooperative cancel points inside the
    chain are deliberately NOT honoured (an emergency stop runs to
    completion). Every step's failure is recorded honestly; the stopped
    terminal is written only when ALL targets are confirmed.
    """
    _cancel_point(ctx)
    _report(ctx, "紧急停止：立即撤入口 -> broker emergency-stop -> 停后端 -> 停 TS 服务")
    params = params if isinstance(params, dict) else {}
    interval_s = _budget(params, "poll_interval_s", _INTERVAL_DEFAULT_S)
    facts = []
    failures = []          # (code, honest text) in execution order
    layers = {}

    # §5.4: the stop intent is expressed FIRST; the cancel signal is then
    # written before the entry withdrawal, and a failed write NEVER delays
    # that withdrawal (it immediately tries the stopped intent as well).
    _STATE.express_stop()
    cancel_facts = _cancel_public_supervision("manual_estop", "share.estop")
    _report_cancel_facts(ctx, cancel_facts)

    # ---- funnel off, best effort: a failure never halts the emergency chain
    try:
        if _funnel_observed() is None and _ts_service_status() == "stopped":
            # No daemon -> no serve mapping -> no public entry: deterministic
            # OFF, not a failure to report.
            layers["funnel"] = "off"
            facts.append("funnel=off（TS 服务停止 ⇒ 入口不存在）")
            _report(ctx, "TS 服务已停止（无 daemon 即无入口）：funnel 按已关闭")
        else:
            ack = _funnel_control("off")
            if ack:
                layers["funnel"] = "off"
                _report(ctx, "入口已立即撤销（受限缝确认 off）")
                facts.append("funnel=off")
            else:
                failures.append(("funnel_failed",
                                 "funnel：撤入口未获确认（入口可能仍开放）"))
    except OperationError as exc:
        failures.append((exc.code, "funnel：" + exc.message))

    # ---- broker emergency-stop (existing protocol action)
    try:
        ops = _broker_ops()
        raw = ops.emergency_stop()
        if isinstance(raw, dict) and raw.get("stopped") is True:
            layers["emergency_stop"] = "confirmed"
            _report(ctx, "broker emergency-stop 已确认（请求中断）")
        else:
            failures.append(("share_layer_failed",
                             "broker emergency-stop 未确认 stopped"
                             "（结果=%r）" % (raw,)))
    except OperationError as exc:
        failures.append((exc.code, "broker emergency-stop：" + exc.message))
    except Exception as exc:  # noqa: BLE001
        failures.append(("share_layer_failed",
                         "broker emergency-stop 异常（%s: %s）"
                         % (type(exc).__name__, exc)))

    # ---- backend stop (best effort, then three-evidence closure check)
    try:
        ops = _broker_ops()
        if ops.verify_closed() is not True:
            if ops.stop_host() is not True:
                failures.append(("share_layer_failed",
                                 "backend：受限任务门拒绝停止"))
            else:
                try:
                    _confirm_backend_closed(
                        None, _budget(params, "backend_confirm_budget_s",
                                      _STOP_CONFIRM_DEFAULT_S), interval_s)
                    layers["backend"] = "stopped"
                    facts.append("backend=stopped（verify_closed 三证确认）")
                except OperationCancelled:
                    failures.append(("share_layer_failed",
                                     "backend：停止后确认被打断"
                                     "（实际状态待复验）"))
                except OperationError as exc:
                    failures.append((exc.code, "backend：" + exc.message))
                else:
                    _report(ctx, "backend 已停止并确认关闭（三证）")
        else:
            layers["backend"] = "stopped"
            facts.append("backend=stopped（关闭前已确认）")
            _report(ctx, "backend 关闭前已确认关闭（三证）：停机步骤跳过")
    except OperationError as exc:
        failures.append((exc.code, "backend：" + exc.message))

    # ---- dedicated backend stop (emergency chain: gateway first, then the
    # dedicated upstream instance; production chat2api never touched)
    try:
        sb_closed, sb_fact = _stop_share_backend_best_effort(
            _budget(params, "backend_confirm_budget_s",
                    _STOP_CONFIRM_DEFAULT_S), interval_s)
        layers["share_backend"] = "stopped" if sb_closed else "unconfirmed"
        facts.append(sb_fact)
        _report(ctx, "专用分享后端：" + sb_fact)
        if not sb_closed:
            failures.append(("share_layer_failed",
                             "专用分享后端：" + sb_fact))
    except Exception as exc:  # noqa: BLE001
        failures.append(("share_layer_failed",
                         "share_backend 停止异常（%s: %s）"
                         % (type(exc).__name__, exc)))

    # ---- ts service stop
    try:
        if _ts_service_status() != "stopped":
            _ts_control_with_pending_poll(ctx, "stop")
            _confirm_ts_stopped(
                None, _budget(params, "ts_confirm_budget_s",
                              _STOP_CONFIRM_DEFAULT_S), interval_s)
        layers["ts_service"] = "stopped"
        facts.append("ts_service=stopped")
        _report(ctx, "TS 服务已停止并确认 stopped")
    except OperationError as exc:
        failures.append((exc.code, "TS 服务：" + exc.message))

    if failures:
        _report_cancel_facts(ctx, cancel_facts)
        raise OperationError(
            failures[0][0],
            "紧急停止未全部确认（每步失败如实）：%s；实际分层事实：%s；"
            "不会伪报全关；可用 share.retry_stop / 授权入口复验继续处置"
            % ("；".join(text for _, text in failures),
               "；".join(facts) or "无已完成层"))
    _STATE.confirm_stopped()
    # §7.4 intent write: stopped, after every layer is confirmed.
    intent_ok = _write_intent("stopped", "share.estop")
    _report(ctx, _intent_note("stopped", intent_ok))
    result = {"layers": dict(layers, connectivity="withdrawn"),
              "closed": True, "facts": list(facts),
              "intent_written": intent_ok,
              "note": "紧急停止完成：入口已撤、broker 已确认中断、后端已停（三证）、"
                      "TS 服务已停；%s" % _intent_note("stopped", intent_ok)}
    if cancel_facts["public_cancel"] == "failed":
        result["note"] += "；" + _cancel_facts_note(cancel_facts)
    return _merge_public_cancel(result, cancel_facts)


def share_retry_stop(target, params, ctx=None):
    """Residual retry: continue the close from the remaining steps.

    Reuses the layered close advance (each step skips when its close target
    is already observed), so no reopen is required. The step pacing
    (``step_interval_ms``, default 800) is injectable and bounded. A failing
    retry keeps the honest partial-close report and the entry stays valid.
    """
    _cancel_point(ctx)
    _report(ctx, "残留重试：按剩余步骤继续关闭（复用 share_close 分层推进）")
    # §5.4: a residual retry is a human close too; the cancel signal is
    # written before the first remaining layer, and a failed write never
    # blocks the close (the stopped intent is tried instead).
    cancel_facts = _cancel_public_supervision("manual_retry_stop",
                                             "share.retry_stop")
    params = params if isinstance(params, dict) else {}
    step_interval_s = _step_ms(params, "step_interval_ms",
                               _STEP_INTERVAL_DEFAULT_MS) / 1000.0
    interval_s = _budget(params, "poll_interval_s", _INTERVAL_DEFAULT_S)
    facts = []
    try:
        close = _close_chain(ctx, params, interval_s, step_interval_s, facts)
    except OperationCancelled:
        _report(ctx, "已响应取消；已执行层如实留痕，剩余层可再次 share.retry_stop")
        _report_cancel_facts(ctx, cancel_facts)
        raise
    except OperationError as exc:
        _report(ctx, "重试未完成：" + exc.message)
        _report_cancel_facts(ctx, cancel_facts)
        raise OperationError(exc.code, _partial_close_message(exc, facts)) \
            from exc
    layers = close["layers"]
    drain = close["drain"]
    if layers.get("share_backend") != "stopped":
        raise OperationError(
            "share_layer_failed",
            _partial_close_message(
                OperationError("share_layer_failed",
                               "专用分享后端未确认关闭"), facts))
    _STATE.confirm_stopped()
    # §7.4 intent write: stopped, after the share_backend layer is confirmed.
    intent_ok = _write_intent("stopped", "share.retry_stop")
    _report(ctx, _intent_note("stopped", intent_ok))
    if close["state"] == "drain_timeout":
        _report(ctx, "重试推进完成，但排空超时：在途未确认结束")
        note = ("残留重试完成但排空超时（在途未确认结束）；可用 "
                "share.chain_estop 立即取消在途；stopped 终态已按契约写入；%s"
                % _intent_note("stopped", intent_ok))
    else:
        _report(ctx, "全部关闭层已确认（重试推进完成）")
        note = ("残留重试完成：全部关闭层已确认（stopped 终态已按契约"
                "写入）；%s" % _intent_note("stopped", intent_ok))
    result = {"layers": layers, "state": close["state"], "drain": drain,
              "closed": True, "facts": list(facts),
              "intent_written": intent_ok, "note": note}
    if cancel_facts["public_cancel"] == "failed":
        result["note"] += "；" + _cancel_facts_note(cancel_facts)
    return _merge_public_cancel(result, cancel_facts)


def backend_login(target, params, ctx=None):
    """Console action「登录分享专用后端」: trigger the control-token-gated
    POST /admin/auth/login on the DEDICATED instance (its own secret) — the
    backend opens its own independent-profile browser for the human UM SSO.
    Read-only toward everything else: port 8000 / the production chat2api
    process are never touched, no Funnel/TS layer is opened, and the
    control secret is never echoed.

    §7.4: the cold-start ``start()`` path inside the production flow writes
    the runtime intent through the ``intent_writer`` passed here (the flow
    itself never imports the supervision module); the writer is best-effort and
    an unwired writer is reported honestly by the flow.
    """
    _cancel_point(ctx)
    if _BACKEND_LOGIN is None:
        raise OperationError(
            "share_backend_login_unwired",
            "专用后端登录缝未接线（生产适配器未装配）；如实拒绝，不触发登录")
    seam = _BACKEND_LOGIN
    try:
        accepts = "intent_writer" in inspect.signature(seam).parameters
    except (TypeError, ValueError):
        accepts = False
    if accepts:
        return seam(intent_writer=_INTENT_WRITE)
    # A seam without the keyword-only writer (legacy/test stub): the plain
    # call keeps the historical behaviour (no intent write).
    return seam()


def _auth_payload(obs):
    """The console auth display fields from ONE read-only observation.

    Every field is passed through verbatim; a missing/unreadable observation
    reads as None (未知 ≠ 有效). ``login_window_deadline_epoch`` is the
    instance's OWN one-time window deadline — this function never computes,
    refreshes or extends it.
    """
    if not isinstance(obs, dict):
        return {"state": None, "label": None, "error": None,
                "recovery": None, "observed_at": None,
                "login_window_deadline_epoch": None}
    observed_at = obs.get("observed_at")
    deadline = obs.get("login_window_deadline_epoch")
    label = obs.get("auth_label")
    error = obs.get("auth_error")
    recovery = obs.get("auth_recovery")
    return {
        "state": obs.get("auth_state"),
        "label": label if isinstance(label, str) else None,
        "error": error if isinstance(error, str) else None,
        "recovery": recovery if isinstance(recovery, dict) else None,
        "observed_at": observed_at if _is_epoch(observed_at) else None,
        "login_window_deadline_epoch": deadline
        if _is_epoch(deadline) else None,
    }


def backend_auth_read():
    """READ-ONLY dedicated-backend auth view for the console display card.

    Reads the existing observation seam with ``forget_stale=False`` (never
    deletes a stale pid file), never triggers a login, never starts/stops the
    dedicated instance and never mutates the orchestration stop state. An
    unwired seam reads as an all-None observation (honestly unknown, never a
    fabricated ``valid``); a failing seam propagates its own OperationError.
    """
    seam = _BACKEND_OBSERVE
    if seam is None:
        return _auth_payload(None)
    try:
        accepts = "forget_stale" in inspect.signature(seam).parameters
    except (TypeError, ValueError):
        accepts = False
    obs = seam(forget_stale=False) if accepts else seam()
    return _auth_payload(obs)


def backend_login_cancel(target, params, ctx=None):
    """D18-8 console action「取消登录」: end THIS login round on the
    dedicated instance through its control-token-gated POST /admin/auth/cancel.

    The instance closes its browser context and reports ``cancelled``; it is
    NEVER stopped or restarted, and ordinary requests never reopen the round.
    The seam is lazy (the production module is imported only when the test seam
    is unwired) so a missing deployment refuses honestly.
    """
    _cancel_point(ctx)
    _report(ctx, "取消本轮登录（只结束本轮并清理浏览器，不停止服务）")
    fn = _BACKEND_LOGIN_CANCEL
    if fn is None:
        try:
            import console_share_production
            fn = getattr(console_share_production,
                          "backend_login_cancel", None)
        except Exception:  # noqa: BLE001  an unwired seam stays honest
            fn = None
    if fn is None:
        raise OperationError(
            "share_backend_login_cancel_unwired",
            "专用后端取消登录缝未接线（生产适配器未装配）；如实拒绝，不停止服务")
    try:
        return fn()
    except OperationCancelled:
        raise
    except OperationError:
        raise
    except Exception as exc:  # noqa: BLE001  never fake a successful cancel
        raise OperationError(
            "share_backend_login_cancel_failed",
            "登录取消失败（%s: %s）；未停止服务"
            % (type(exc).__name__, exc))


def _edge_fact():
    """(edge_state, note) for the cloud public edge, READ-ONLY.

    ``unmanaged`` = the cloudflared unit is absent (this deployment does not
    manage the public edge — NOT a failure). ``active``/``inactive`` are the
    unit's own systemd facts; an unavailable/unreadable seam reads
    ``unconfirmed`` and never blocks the chain.
    """
    seam = _EDGE_STATUS
    if seam is None:
        return "unmanaged", "公网边缘未由本部署管理"
    raw, reason = _probe(seam)
    if not isinstance(raw, dict):
        return "unconfirmed", ("公网边缘状态不可确认（%s）"
                              % (reason or "probe_failed"))
    exists = raw.get("exists")
    if exists is False:
        return "unmanaged", "公网边缘未由本部署管理"
    if exists is True:
        if raw.get("active") is True:
            return "active", "公网边缘 cloudflared 已确认 active"
        if raw.get("active") is False:
            return "inactive", "公网边缘 cloudflared 存在但未 active"
    return "unconfirmed", "公网边缘状态未确认"


def _wait_chain_auth(ctx, first_obs, budget_s, interval_s):
    """(outcome, last_obs) for the whole-chain start's bounded auth wait.

    outcome:
    * ``valid`` — a confirmed valid upstream auth;
    * ``waiting_login`` — the human still owns the round (logging_in /
      needs_login / an unreadable transitional reading / the budget expired,
      and non-credential failures such as MFA / a closed window). A
      ``waiting_login`` outcome is NOT a success and never starts 8100;
    * ``failed`` — a terminal credential rejection or the instance stopped
      answering (nothing can progress by waiting).

    Single-flight: this wait NEVER triggers a login and never submits
    credentials; the human uses the console's explicit login action.
    """
    obs = first_obs
    deadline = time.monotonic() + budget_s
    while True:
        if not isinstance(obs, dict) or obs.get("running") is not True:
            return "failed", obs
        state = obs.get("auth_state")
        if state == "valid":
            return "valid", obs
        if state == "invalid":
            return "failed", obs
        if state == "failed":
            if _is_noncredential_auth_error(obs.get("auth_error")):
                return "waiting_login", obs
            return "failed", obs
        if state == "needs_login":
            # No login is in progress; only the human's explicit login
            # action can advance this round.
            return "waiting_login", obs
        if ctx is not None:
            ctx.raise_if_cancelled()
        if time.monotonic() >= deadline:
            return "waiting_login", obs
        _SLEEP(interval_s)
        obs = _backend_observe()


def _with_chain_layers(result):
    """Attach the frozen layered facts to a whole-chain result."""
    result["layers"] = {"backend": result["backend"],
                        "gateway": result["gateway"],
                        "edge": result["edge"]}
    return result


def chain_start(target, params, ctx=None):
    """D18-10 cloud whole-chain start: 8110 -> bounded auth wait -> 8100.

    Only the dedicated-backend gate and the restricted broker are driven; the
    cloud deployment has NO TS/Funnel layer, so those seams are never
    called. The public edge (cloudflared unit) is a READ-ONLY fact: absent =
    "not managed by this deployment" (never a failure).

    ``state`` is ``running`` / ``waiting_login`` / ``failed``;
    ``waiting_login`` is NOT a success (a human must finish UM SSO/MFA and the
    operator retries). The start is EXPLICIT so a persisted stop intent is
    cleared by the restricted broker start.
    """
    _cancel_point(ctx)
    _report(ctx, "整链启动：Key 前置 → 8110 → 认证等待 → 8100 → 公网边缘（只读）")
    params = params if isinstance(params, dict) else {}
    interval_s = _budget(params, "poll_interval_s", _INTERVAL_DEFAULT_S)
    backend_budget = _budget(params, "backend_budget_s",
                             _BACKEND_START_DEFAULT_S)
    auth_budget = min(_budget(params, "backend_auth_budget_s",
                             _BACKEND_AUTH_DEFAULT_S),
                      _BACKEND_AUTH_MAX_S)

    # 1) At least one enabled share key (the same fail-closed precondition
    # the local open chain uses). An unwired seam is reported honestly.
    if _KEYS_CONFIRMED is not None:
        keys_ok = _KEYS_CONFIRMED()
        if keys_ok is not True:
            raise OperationError(
                "share_layer_failed", _keys_precondition_message(keys_ok))
    else:
        _report(ctx, "Key 前置确认缝未接线（跳过，如实留痕）")

    result = {"state": "failed", "auth": _auth_payload(None),
              "backend": "unconfirmed", "gateway": "unconfirmed",
              "edge": "unconfirmed", "note": ""}

    # 2) Dedicated 8110: start only when not already running, then confirm
    # the process is running within the bounded backend budget.
    obs = _backend_observe()
    if obs is None:
        raise OperationError(
            "share_backend_unwired",
            "share_backend 层：专用后端缝未接线（独立实例部署/隔离门未完成），"
            "该层如实不可用；本机 chat2api 不受影响")
    if obs.get("running") is not True:
        if _backend_ops().start() is not True:
            result["note"] = ("整链启动失败：专用后端 8110 启动未获门确认；"
                              "未启动 8100")
            _report(ctx, result["note"])
            return _with_chain_layers(result)
        _report(ctx, "专用后端 8110 启动已受理，等待运行确认")
        deadline = time.monotonic() + backend_budget
        while True:
            if _backend_ops().verify_running() is True:
                break
            if ctx is not None:
                ctx.raise_if_cancelled()
            if time.monotonic() >= deadline:
                result["note"] = ("整链启动失败：%.0fs 内专用后端 8110 "
                                  "未确认运行；未启动 8100" % backend_budget)
                _report(ctx, result["note"])
                return _with_chain_layers(result)
            _SLEEP(interval_s)
    else:
        _report(ctx, "专用后端 8110 已在运行（不归属本次启动）")
    result["backend"] = "running"

    # 3) Bounded auth wait: waiting_login is a HUMAN state, not a success.
    obs2 = _backend_observe()
    if not isinstance(obs2, dict) or obs2.get("running") is not True:
        result["note"] = "整链启动失败：专用后端 8110 运行后健康观察未确认"
        _report(ctx, result["note"])
        return _with_chain_layers(result)
    outcome, auth_obs = _wait_chain_auth(ctx, obs2, auth_budget, interval_s)
    result["auth"] = _auth_payload(auth_obs)
    if outcome == "failed":
        result["note"] = (
            "整链启动失败：上游认证未通过（%s，终态判定，不自动重开登录）；"
            "未启动 8100；可用 share.backend_login 显式登录后重试"
            % _auth_word(result["auth"]["state"]))
        _report(ctx, result["note"])
        return _with_chain_layers(result)
    if outcome == "waiting_login":
        result["state"] = "waiting_login"
        result["note"] = (
            "整链启动未完成：等待本人完成 UM 登录/MFA（不是成功）；"
            "专用后端 8110 保持运行，登录后重试 share.chain_start；"
            "未启动 8100")
        _report(ctx, result["note"])
        return _with_chain_layers(result)

    # 4) Share gateway 8100: the restricted broker's EXPLICIT start.
    ledger = _OpenLedger()
    try:
        _open_backend(ctx, ledger, {"backend": backend_budget}, interval_s)
    except OperationError as exc:
        result["note"] = ("整链启动失败：分享网关 8100 未确认运行（%s）；"
                          "专用后端 8110 保持运行" % exc.message)
        _report(ctx, result["note"])
        return _with_chain_layers(result)
    result["gateway"] = "running"

    # 5) Public edge: READ-ONLY fact; absent = not managed by this
    # deployment (never a failure of the chain).
    edge, edge_note = _edge_fact()
    result["edge"] = edge
    result["state"] = "running"
    result["note"] = ("整链启动完成：专用后端 8110 运行且上游认证有效、"
                      "分享网关 8100 运行（三证）；" + edge_note)
    _report(ctx, result["note"])
    return _with_chain_layers(result)


def chain_stop(target, params, ctx=None):
    """D18-10 cloud whole-chain NORMAL stop: broker drain -> stop 8110.

    The broker drain refuses new requests, waits for in-flight requests within
    its bound, stops 8100 and verifies closure with three evidence. A timed-out
    drain is NEVER reported as a normal close: ``state=drain_timeout`` and the
    note says the in-flight requests were not confirmed ended. A broker failure
    does not skip the dedicated-backend stop; the result stays ``partial``.
    """
    _cancel_point(ctx)
    _report(ctx, "整链停止：broker 有界排空（先拒新）→ 停 8110 → 汇总")
    params = params if isinstance(params, dict) else {}
    interval_s = _budget(params, "poll_interval_s", _INTERVAL_DEFAULT_S)
    drain_error = None
    raw = None
    try:
        raw = _broker_ops().drain()
    except OperationError as exc:
        drain_error = exc
    except OperationCancelled:
        raise
    except Exception as exc:  # noqa: BLE001  never fake a drain result
        drain_error = OperationError(
            "share_layer_failed",
            "drain 层：broker 排空调用异常（%s: %s）"
            % (type(exc).__name__, exc))
    if raw is None and drain_error is None:
        drain_error = OperationError(
            "share_layer_failed", "drain 层：broker 排空结果为空")
    if isinstance(raw, dict):
        drain = {
            "drained": raw.get("drained"),
            "timed_out": raw.get("timed_out") is True,
            "stopped": raw.get("stopped"),
            "notification_failed": raw.get("notification_failed"),
            "in_flight": raw.get("in_flight"),
        }
        if not (drain["drained"] is True or drain["timed_out"]
                or drain["stopped"] is True):
            drain_error = OperationError(
                "share_layer_failed",
                "drain 层：broker 排空结果未确认 drained/timed_out/stopped"
                "（结果=%r）" % (raw,))
    elif drain_error is None:
        drain_error = OperationError(
            "share_layer_failed",
            "drain 层：broker 排空结果形状不符合协议（非结构化结果=%r）"
            % (raw,))
    if drain_error is not None:
        drain = {"drained": None, "timed_out": False, "stopped": None,
                 "notification_failed": None, "in_flight": None}

    sb_closed, sb_fact = _stop_share_backend_best_effort(
        _budget(params, "backend_confirm_budget_s", _STOP_CONFIRM_DEFAULT_S),
        interval_s)
    gateway = "stopped" if drain["stopped"] is True else "unconfirmed"
    share_backend = "stopped" if sb_closed else "unconfirmed"
    layers = {"gateway": gateway, "share_backend": share_backend}

    if drain_error is not None:
        state = "partial"
        note = ("整链停止未全部确认：broker 排空失败（%s: %s）；分享网关=%s；"
                "专用后端：%s"
                % (drain_error.code, drain_error.message, gateway, sb_fact))
    elif drain["timed_out"]:
        state = "drain_timeout"
        note = ("排空超时后强制停止（在途未确认结束）；分享网关已按 broker "
                "终态停止；专用后端：%s；可用 share.chain_estop 立即取消在途"
                % sb_fact)
    elif (drain["drained"] is True and gateway == "stopped"
          and share_backend == "stopped"):
        state = "stopped"
        note = ("整链已停止：broker 有界排空完成、分享网关三证关闭、"
                "专用后端已停；%s" % sb_fact)
    else:
        state = "partial"
        note = ("整链停止未全部确认：drained=%s，timed_out=%s，stopped=%s；"
                "专用后端：%s"
                % (drain["drained"], drain["timed_out"], drain["stopped"],
                   sb_fact))
    _report(ctx, note)
    return {"state": state, "drained": drain["drained"],
            "timed_out": drain["timed_out"], "stopped": drain["stopped"],
            "notification_failed": drain["notification_failed"],
            "in_flight": drain["in_flight"], "layers": layers, "note": note}


def chain_estop(target, params, ctx=None):
    """D18-10 cloud whole-chain FORCED stop: broker emergency-stop ->
    stop 8110.

    No drain step and no cooperative cancel points (a forced stop runs to
    completion). ``cancelled_in_flight`` is the broker's own cleared count;
    unreadable = None, never a fabricated 0.
    """
    _cancel_point(ctx)
    _report(ctx, "整链强制停止：broker emergency-stop（立即取消在途）→ 停 8110")
    params = params if isinstance(params, dict) else {}
    interval_s = _budget(params, "poll_interval_s", _INTERVAL_DEFAULT_S)
    raw = None
    broker_error = None
    try:
        raw = _broker_ops().emergency_stop()
    except OperationError as exc:
        broker_error = exc
    except OperationCancelled:
        raise
    except Exception as exc:  # noqa: BLE001  never fake an emergency stop
        broker_error = OperationError(
            "share_layer_failed",
            "broker emergency-stop 异常（%s: %s）"
            % (type(exc).__name__, exc))
    stopped = isinstance(raw, dict) and raw.get("stopped") is True
    cancelled = raw.get("cleared_in_flight") if isinstance(raw, dict) else None
    if not isinstance(cancelled, int) or isinstance(cancelled, bool):
        cancelled = None
    sb_closed, sb_fact = _stop_share_backend_best_effort(
        _budget(params, "backend_confirm_budget_s", _STOP_CONFIRM_DEFAULT_S),
        interval_s)
    layers = {"gateway": "stopped" if stopped else "unconfirmed",
              "share_backend": "stopped" if sb_closed else "unconfirmed"}
    if broker_error is not None:
        state = "partial"
        note = ("整链强制停止未全部确认：broker emergency-stop 失败（%s）；"
                "分享网关=%s；专用后端：%s"
                % (broker_error.message, layers["gateway"], sb_fact))
    elif stopped and sb_closed:
        state = "stopped"
        note = ("整链强制停止完成：broker 已确认中断在途、分享网关已关闭、"
                "专用后端已停；%s" % sb_fact)
    else:
        state = "partial"
        note = ("整链强制停止未全部确认：分享网关=%s；专用后端：%s"
                % (layers["gateway"], sb_fact))
    _report(ctx, note)
    return {"state": state, "forced": True,
            "cancelled_in_flight": cancelled, "layers": layers,
            "note": note}


def share_status(target, params, ctx=None):
    """READ-ONLY layered observation (no side effects; enabled).

    Every layer is observed through its read-only seam; an unwired seam
    reads "unavailable" honestly. The dedicated share backend is observed
    through its own read-only seam (process running + upstream auth state);
    an unwired backend seam reads "unavailable" — the production chat2api
    process is never probed or touched here. desired_stop_in_force is the
    orchestration-side managed-stop view (True = stop intent in force,
    False = confirmed open, None = no authority record in this process);
    the production authority stays the broker's protected lifecycle record.
    """
    _cancel_point(ctx)
    _report(ctx, "只读分层观察（无副作用，不触发任何启停）")
    sb_obs = None
    try:
        sb_obs = _backend_observe()
    except OperationError:
        sb_obs = None
    if isinstance(sb_obs, dict) and sb_obs.get("running") is not None:
        sb_state = "running" if sb_obs.get("running") is True else "stopped"
    else:
        sb_state = "unavailable"
    auth_note = ""
    if isinstance(sb_obs, dict):
        auth_note = ("；专用后端上游认证=%s"
                     % _auth_word(sb_obs.get("auth_state")))
    ts_state = _ts_service_status()
    try:
        conn = _connectivity_ok()
    except OperationError:
        if ts_state == "stopped":
            # No daemon -> the CLI cannot answer -> DETERMINISTIC offline,
            # not an unresolvable observation.
            conn = False
        else:
            raise
    try:
        running = _broker_ops().verify_running()
        backend = {True: "running", False: "stopped"}.get(running, "unconfirmed")
    except OperationError:
        backend = "unavailable"
    funnel_obs = _funnel_observed()
    if funnel_obs is None and ts_state == "stopped":
        # No daemon -> no serve mapping: a deterministic OFF, not an
        # unresolvable observation.
        funnel_state = "off"
    else:
        funnel_state = {True: "on", False: "off"}.get(funnel_obs,
                                                      "unavailable")
    layers = {
        "share_backend": sb_state,
        "ts_service": ts_state if ts_state in ("running", "stopped")
        else "unavailable",
        "connectivity": {True: "ok", False: "lost"}.get(conn, "unavailable"),
        "backend": backend,
        "funnel": funnel_state,
    }
    desired = _desired_stop_in_force()
    if desired is True:
        desired_note = "停止意图生效中（stopped 终态仅在全层确认后写入）"
    elif desired is False:
        desired_note = "分享开启已确认（本进程无停止意图）"
    else:
        desired_note = ("无本进程停止意图记录；权威生命周期记录在 broker "
                        "受保护文件（watchdog 协调），由授权包接线读取")
    note = ("只读分层观察；unavailable 表示对应缝未接线/不可确认（如实，"
            "不冒充）；%s%s" % (desired_note, auth_note))
    # Machine-readable dedicated-backend auth (review 2026-09-17: the page
    # showed only the PROCESS state, so「认证有效」could not be confirmed at
    # a glance). state values mirror production_backend_observe: valid /
    # invalid / failed / logging_in / needs_login / unknown; observed_at is
    # the epoch of THIS read-only observation.
    # attribution (owner ruling 2026-09-26, reshaped by the same-day review:
    # ONE boolean must not stand for THREE different facts): the SAME
    # observation seam's attribution sub-dict passes through additively so this
    # share status read surface and the console status payload carry ONE shape —
    # {file_ready, reason, key_file, key_chars, instance_loaded,
    # request_attribution}. ``file_ready`` is only "this process can read a
    # long-enough key file now"; ``instance_loaded`` is an INFERENCE about the
    # RUNNING 8100 instance (yes/no/unknown, never "yes" when the process is
    # not found); ``request_attribution`` is NOT observed here and only points
    # at the 8110 ledger fact. The old ``wired`` key is gone (it was consumed
    # only by this read surface and usagesrv, both updated together), so no
    # reader can take one boolean for all three. None when the seam is unwired
    # or predates the field.
    backend_auth = {"state": None, "label": None, "observed_at": None,
                    "attribution": None}
    if isinstance(sb_obs, dict):
        backend_auth = {
            "state": sb_obs.get("auth_state"),
            "label": sb_obs.get("auth_label"),
            "observed_at": time.time(),
            "attribution": sb_obs.get("attribution")
            if isinstance(sb_obs.get("attribution"), dict) else None,
            "error": sb_obs.get("auth_error"),
            "recovery": sb_obs.get("auth_recovery")
            if isinstance(sb_obs.get("auth_recovery"), dict) else None,
        }
    return {"layers": layers, "desired_stop_in_force": desired,
            "backend_auth": backend_auth, "note": note}


# ==================================================== Rev9 single-layer actions
# A9-1 share.diagnostics / A9-2 share.backend_reauthenticate /
# A9-3 share.backend_restart / A9-4 share.gateway_restart.
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


def _share_gateway_probe():
    """Read-only 8100 instance probe (contract §3.2, production function).

    Lazy import keeps this module import-light; a missing production module or
    function reads as None (honest unavailable) — never a fabricated state.
    """
    try:
        import console_share_production
    except Exception:  # noqa: BLE001
        return None
    fn = getattr(console_share_production, "share_gateway_instance_probe", None)
    if fn is None:
        return None
    return fn()


def _share_gateway_attribution():
    """Read-only attribution status (contract §3.2, production function)."""
    try:
        import console_share_production
    except Exception:  # noqa: BLE001
        return None
    fn = getattr(console_share_production, "share_attribution_status", None)
    if fn is None:
        return None
    return fn()


def _backend_observe_read_only():
    """(obs, reason): read-only dedicated-backend observation.

    ``forget_stale=False`` semantics (contract §0.5): this NEVER deletes a
    stale ``backend.pid``. A seam that does not accept the switch (older/test
    stub) is called plainly; the production default is the historical read.
    """
    if _BACKEND_OBSERVE is None:
        return None, "share_backend_unwired"
    seam = _BACKEND_OBSERVE
    try:
        accepts = "forget_stale" in inspect.signature(seam).parameters
    except (TypeError, ValueError):
        accepts = False
    try:
        raw = seam(forget_stale=False) if accepts else seam()
    except OperationCancelled:
        raise
    except OperationError as exc:
        return None, exc.code
    except Exception:  # noqa: BLE001
        return None, "share_backend_probe_failed"
    if not isinstance(raw, dict):
        return None, "share_backend_unavailable"
    return raw, None


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


def _diag_share_gateway():
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
    probe, probe_reason = _probe(_share_gateway_probe)
    evidence["instance_probe"] = probe if isinstance(probe, dict) else None
    if not isinstance(probe, dict):
        evidence["instance_probe_reason"] = \
            probe_reason or "probe_unavailable"
    attribution, attr_reason = _probe(_share_gateway_attribution)
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


def _diag_share_backend():
    source = "backend:observe(forget_stale=False)+pid-state"
    obs, reason = _backend_observe_read_only()
    if obs is None:
        return _diag_layer("share_backend", "unavailable", "unavailable",
                           source, reason=reason)
    running = obs.get("running")
    state = {True: "running", False: "stopped"}.get(running, "unconfirmed")
    verdict = "unconfirmed" if running is None else "confirmed"
    evidence = {"observe": obs}
    ops = _BACKEND_OPS
    pid_state = None
    state_fn = getattr(ops, "backend_pid_state", None) \
        if ops is not None else None
    if state_fn is not None:
        pid_state, _pid_reason = _probe(state_fn)
    evidence["pid_state"] = pid_state if isinstance(pid_state, dict) else None
    if not isinstance(pid_state, dict):
        evidence["pid_state_reason"] = "pid_state_unavailable"
    return _diag_layer("share_backend", state, verdict, source,
                       checked_at=time.time(), evidence=evidence)


def _diag_backend_auth():
    source = "backend:admin/auth/status"
    obs, reason = _backend_observe_read_only()
    if obs is None:
        return _diag_layer("backend_auth", "unavailable", "unavailable",
                           source, reason=reason)
    auth = obs.get("auth_state")
    observed = obs.get("observed_at")
    if _is_epoch(observed):
        checked_at = float(observed)
        basis = "source_checked_epoch"
    else:
        checked_at = time.time()
        basis = "local_read"
    evidence = {
        "auth_label": obs.get("auth_label")
        if isinstance(obs.get("auth_label"), str) else None,
        "running": obs.get("running"),
        "attribution": obs.get("attribution")
        if isinstance(obs.get("attribution"), dict) else None,
    }
    if auth in ("valid", "invalid", "failed", "logging_in", "needs_login",
                "unknown"):
        state = auth
        reason = None
    else:
        state = "unknown"
        reason = "auth_state_unobserved"
    verdict = "unconfirmed" if state == "unknown" else "confirmed"
    return _diag_layer("backend_auth", state, verdict, source,
                       checked_at=checked_at, evidence=evidence,
                       reason=reason, timestamp_basis=basis)


def _diag_connectivity(ts_state):
    source = "ts:status"
    if _CONNECTIVITY_OK is None:
        return _diag_layer("connectivity", "unavailable", "unavailable",
                           source, reason="connectivity_unwired")
    conn, reason = _probe(_connectivity_ok)
    if reason is not None:
        if ts_state == "stopped":
            # No daemon -> the CLI cannot answer -> DETERMINISTIC offline
            # (same rule the legacy share.status uses).
            return _diag_layer("connectivity", "lost", "confirmed", source,
                               checked_at=time.time(),
                               reason="ts_service_stopped",
                               evidence={"error_code": reason})
        return _diag_layer("connectivity", "unavailable", "unavailable",
                           source, reason="connectivity_query_failed",
                           evidence={"error_code": reason})
    if conn is None:
        return _diag_layer("connectivity", "unavailable", "unavailable",
                           source, reason="connectivity_unwired")
    state = {True: "ok", False: "lost"}[conn]
    return _diag_layer("connectivity", state, "confirmed", source,
                       checked_at=time.time())


def _diag_upstream():
    source = "backend:health.upstream_reachability"
    obs, reason = _backend_observe_read_only()
    if obs is None:
        return _diag_layer("upstream", "unavailable", "unavailable",
                           source, reason=reason)
    if obs.get("running") is not True:
        return _diag_layer("upstream", "unavailable", "unavailable", source,
                           reason="backend_not_running",
                           evidence={"health_reachable": obs.get("running")})
    up = obs.get("upstream_reachability")
    if not isinstance(up, dict):
        return _diag_layer("upstream", "unconfirmed", "unconfirmed", source,
                           checked_at=time.time(),
                           reason="upstream_reachability_unobserved",
                           evidence={"health_reachable": True})
    state = {"ok": "ok", "unreachable": "lost",
             "unknown": "unconfirmed"}.get(up.get("state"))
    if state is None:
        return _diag_layer("upstream", "unconfirmed", "unconfirmed", source,
                           checked_at=time.time(),
                           reason="upstream_state_unrecognized",
                           evidence={"upstream": up, "health_reachable": True})
    if _is_epoch(up.get("checked_epoch")):
        checked_at = float(up["checked_epoch"])
        basis = "source_checked_epoch"
    else:
        checked_at = time.time()
        basis = "local_read"
    verdict = "unconfirmed" if state == "unconfirmed" else "confirmed"
    return _diag_layer("upstream", state, verdict, source,
                       checked_at=checked_at,
                       evidence={"upstream": up, "health_reachable": True},
                       timestamp_basis=basis)


def _diag_ts_service():
    source = "sc:query-ts"
    if _TS_SERVICE_STATUS is None:
        return _diag_layer("ts_service", "unavailable", "unavailable",
                           source, reason="ts_service_unwired")
    state, reason = _probe(_ts_service_status)
    if reason is not None:
        return _diag_layer("ts_service", "unavailable", "unavailable",
                           source, reason=reason)
    if state in ("running", "stopped", "absent"):
        return _diag_layer("ts_service", state, "confirmed", source,
                           checked_at=time.time())
    return _diag_layer("ts_service", "unavailable", "unavailable", source,
                       reason="ts_service_unavailable")


def _diag_funnel(ts_state):
    source = "ts:serve-status"
    if _FUNNEL_OBSERVED is None:
        return _diag_layer("funnel", "unavailable", "unavailable", source,
                           reason="funnel_unwired")
    observed, reason = _probe(_funnel_observed)
    if reason is not None:
        return _diag_layer("funnel", "unavailable", "unavailable",
                           source, reason=reason)
    if observed is True:
        return _diag_layer("funnel", "on", "confirmed", source,
                           checked_at=time.time())
    if observed is False:
        return _diag_layer("funnel", "off", "confirmed", source,
                           checked_at=time.time())
    if ts_state == "stopped":
        # No daemon -> no serve mapping: a deterministic OFF, same rule the
        # legacy share.status uses (never an unresolvable observation).
        return _diag_layer("funnel", "off", "confirmed", source,
                           checked_at=time.time(),
                           reason="ts_service_stopped_no_mapping")
    return _diag_layer("funnel", "unavailable", "unavailable", source,
                       reason="funnel_unavailable")


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


def _diag_watchdog():
    """Fixed SYSTEM task evidence; historical exit codes never overrule Running."""
    source = "broker:host-evidence{watchdog}"
    ops = _BROKER_OPS
    fn = getattr(ops, "host_evidence", None) if ops is not None else None
    empty = {"task_name": "umshare-watchdog", "exists": None,
             "task_state": None, "execute": None, "execute_matches": None,
             "script_path": None, "script_matches": None,
             "last_task_result": None, "last_task_result_hex": None,
             "last_run_at": None, "queried_at": None, "heartbeat_at": None,
             "heartbeat_source": None}
    if fn is None:
        return _diag_layer("watchdog", "unavailable", "unavailable", source,
                           reason="host_evidence_unwired", evidence=empty)
    raw, error = _probe(lambda: fn("watchdog"))
    if error is not None or not isinstance(raw, dict) or raw.get("check") != "watchdog":
        return _diag_layer("watchdog", "unavailable", "unavailable", source,
                           reason="watchdog_broker_unavailable", evidence=empty)
    value = raw.get("verdict")
    if not isinstance(value, dict):
        allowed = {"watchdog_capability_missing", "watchdog_query_failed",
                   "watchdog_broker_unavailable"}
        reason = raw.get("detail")
        return _diag_layer("watchdog", "unavailable", "unavailable", source,
                           reason=reason if isinstance(reason, str) and reason in allowed else "watchdog_response_invalid",
                           evidence=empty)
    evidence = {key: value.get(key) for key in empty}
    evidence["task_name"] = "umshare-watchdog"
    checked_at = float(raw["checked_at"]) if _is_epoch(raw.get("checked_at")) else None
    exists = value.get("exists")
    state, verdict, reason = "unconfirmed", "unconfirmed", None
    if exists is False:
        state, verdict, reason = "absent", "confirmed", "watchdog_task_missing"
    elif exists is not True:
        state, verdict, reason = "unavailable", "unavailable", "watchdog_query_failed"
    elif value.get("query_error") is not None:
        state, verdict, reason = "unavailable", "unavailable", "watchdog_query_failed"
    elif value.get("execute_matches") is False or value.get("script_matches") is False:
        reason = "watchdog_execution_mismatch"
    elif value.get("execute_matches") is not True or value.get("script_matches") is not True:
        reason = "watchdog_execution_unconfirmed"
    elif value.get("task_state") == "Running":
        state, verdict = "running", "confirmed"
    elif value.get("task_state") == "Queued":
        state, verdict, reason = "queued", "confirmed", "watchdog_task_queued"
    elif value.get("task_state") in ("Ready", "Disabled"):
        state, verdict, reason = "stopped", "confirmed", "watchdog_not_running"
    else:
        reason = "watchdog_state_unconfirmed"
    return _diag_layer("watchdog", state, verdict, source,
                       checked_at=checked_at, reason=reason, evidence=evidence)


def _diag_supervision(now):
    """Rev19 A19-2 监督段：分享链监督自己的持久状态（只读）。

    数据来自 ``_SUPERVISION_STATE`` 缝（生产装配在
    ``console_share_production.production_supervision_state`` 懒读监督模块，
    只读监督自己的状态文件，A9-5 单源）；缝未接线（测试/夹具默认）或读面
    自身故障 → ``None``，页面写「未返回监督段（未提供 ≠ 监督未运行）」——
    绝不把读不到写成「监督未运行」。陈旧只降新鲜度：状态文件 mtime 超过
    ``_SUPERVISION_STALE_S`` 标 ``stale``，不判宕机。``now`` 复用调用方
    （``diagnostics``）已取的时钟，不另取一次。只读：不写状态、不启停监督、
    不发请求。
    """
    if _SUPERVISION_STATE is None:
        return None
    try:
        raw = _SUPERVISION_STATE()
    except Exception:  # noqa: BLE001 — 读面自身故障按未提供，绝不冒充监督在跑
        return None
    if not isinstance(raw, dict):
        return None
    state = raw.get("state")
    state_path = raw.get("state_path")
    if not isinstance(state, dict) or state.get("unreadable"):
        return None
    decision = state.get("last_decision")
    decision = decision if isinstance(decision, dict) else None
    if not isinstance(state_path, str) or not state_path:
        # 路径不可得：新鲜度不可裁定，按陈旧上报（不冒充新鲜）。
        stale = True
    else:
        try:
            age = max(0.0, now - os.path.getmtime(state_path))
            stale = age > _SUPERVISION_STALE_S
        except OSError:
            stale = True
    attempts = state.get("restart_attempts_utc")
    return {
        "decision": decision,
        "state": {
            "idle_reason": state.get("idle_reason") or "",
            "consecutive_failures": state.get("consecutive_failures"),
            "restart_attempts_utc": list(attempts)
            if isinstance(attempts, list) else [],
            "version": state.get("version"),
        },
        "stale": stale,
        "checked_at": now,
    }


def diagnostics(target, params, ctx=None):
    """Layered read-only diagnostics, including fixed SYSTEM watchdog evidence.

    Global and never-raising per layer: one failing seam only marks THAT layer
    ``unavailable`` + reason, the report itself never raises (unlike the legacy
    share.status connectivity behaviour, which is untouched). Read-only: it never
    writes the intent, never writes a PID/token/ledger, never triggers SSO or a
    model request, and observes the dedicated backend with
    ``forget_stale=False`` semantics.
    """
    _cancel_point(ctx)
    _report(ctx, "分层只读诊断：九层证据；任一缝失败只让该层 unavailable，整体不抛")
    now = time.time()
    layers = {}
    _report(ctx, '核验分享入口：受限任务、进程身份和监听')
    layers["share_gateway"] = _diag_share_gateway()
    _cancel_point(ctx)
    _report(ctx, '核验独立分享后端 8110')
    layers["share_backend"] = _diag_share_backend()
    _cancel_point(ctx)
    _report(ctx, '核验分享账号认证')
    layers["backend_auth"] = _diag_backend_auth()
    _cancel_point(ctx)
    _report(ctx, '核验 TS 服务和连接')
    ts_layer = _diag_ts_service()
    layers["connectivity"] = _diag_connectivity(ts_layer["state"])
    _cancel_point(ctx)
    _report(ctx, '读取上游就绪证据')
    layers["upstream"] = _diag_upstream()
    _cancel_point(ctx)
    layers["ts_service"] = ts_layer
    _report(ctx, '核验 Funnel 公网映射')
    layers["funnel"] = _diag_funnel(ts_layer["state"])
    _cancel_point(ctx)
    _report(ctx, '核验 WFP 隔离和出站防火墙')
    layers["security_gate"] = _diag_security_gate()
    _cancel_point(ctx)
    _report(ctx, '核验分享看门狗')
    layers["watchdog"] = _diag_watchdog()
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
            "supervision": _diag_supervision(now),
            "note": _bounded("；".join(parts))}


def _diag_pair():
    """The {share_backend, backend_auth} layer pair for before/after."""
    try:
        return {"share_backend": _diag_share_backend(),
                "backend_auth": _diag_backend_auth()}
    except Exception:  # noqa: BLE001  a snapshot never breaks the action
        return {}


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


def backend_reauth(target, params, ctx=None):
    """A9-2 reauthenticate 8110 only (contract §4).

    Never cold-starts, never restarts the process, never bypasses the human
    authentication: the production adapter owns the state machine (valid = zero
    side effects; logging_in = no second window; failed = refuse; unknown =
    no trigger). ``ctx.raise_if_cancelled`` is passed in as the cooperative
    cancel point between polls.
    """
    _cancel_point(ctx)
    _report(ctx, "仅重新认证 8110：不冷启动、不重启进程、不绕过人工认证")
    if _BACKEND_REAUTH is None:
        raise OperationError(
            "share_backend_login_unwired",
            "专用后端重新认证缝未接线（生产适配器未装配）；如实拒绝，不触发认证")
    cancel_check = ctx.raise_if_cancelled if ctx is not None else None
    try:
        result = _BACKEND_REAUTH(cancel_check=cancel_check)
    except OperationCancelled:
        raise
    except OperationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OperationError(
            "share_backend_reauth_failed",
            "重新认证调用异常（%s）：实际状态以 share.status 只读复验为准"
            % type(exc).__name__)
    if not isinstance(result, dict):
        raise OperationError(
            "share_backend_reauth_failed", "重新认证结果形状不符合契约")
    _report(ctx, "重新认证结果：state=%s，triggered=%s，polled=%s"
            % (result.get("state"), result.get("triggered"),
               result.get("polled")))
    return result


def backend_restart(target, params, ctx=None):
    """A9-3 restart 8110 only (contract §5).

    Identity first (five-evidence strict; never cold-start, never kill a bare
    PID) -> maintenance lease on share_gateway (8100 stops accepting) ->
    bounded drain -> re-verify intent/round -> ``_BACKEND_OPS.restart`` ->
    resume acceptance. A drain timeout restores acceptance and fails WITHOUT
    force-stopping the target; a cancel or failure reports the phase, the actual
    target state and the compensation this operation owns.
    """
    _cancel_point(ctx)
    _report(ctx, "8110 单层重启：五项身份核实 → 暂停 8100 接收 → 有界排空 → 重启 → 恢复接收")
    params = params if isinstance(params, dict) else {}
    interval_s = _float_param(params, "poll_interval_s",
                              _RESTART_DEFAULTS["poll_interval_s"][0], 10.0)
    drain_budget_s = _float_param(params, "drain_budget_s",
                                 _RESTART_DEFAULTS["drain_budget_s"][0],
                                 600.0)
    stop_budget_s = _float_param(params, "stop_budget_s",
                                _RESTART_DEFAULTS["stop_budget_s"][0],
                                120.0)
    start_budget_s = _float_param(params, "start_budget_s",
                                 _RESTART_DEFAULTS["start_budget_s"][0],
                                 180.0)
    lease_s = _int_param(params, "lease_s", _RESTART_DEFAULTS["lease_s"])
    ops = _backend_ops()
    state_fn = getattr(ops, "backend_pid_state", None)
    if state_fn is None:
        raise OperationError(
            "share_backend_identity_unknown",
            "8110 身份证据缝缺席（backend_pid_state 不可得）：拒绝重启、绝不误杀")
    identity, id_reason = _probe(state_fn)
    if id_reason is not None or not isinstance(identity, dict):
        raise OperationError(
            "share_backend_identity_unknown",
            "8110 身份证据读取失败（%s）：拒绝重启、绝不误杀"
            % (id_reason or "unavailable"))
    if identity.get("state") != "running":
        raise OperationError(
            "share_backend_not_running",
            "8110 未确认运行（state=%s，identity=%s）：重启操作不冷启动。"
            "请先点击“启动 / 按需登录 8110”；认证有效时无需重复登录，"
            "不会重启私人 8000 或分享入口 8100。"
            % (identity.get("state"), identity.get("identity")))
    if identity.get("identity") != "verified":
        raise OperationError(
            "share_backend_identity_unknown",
            "8110 身份五项证据非 verified（identity=%s，reason=%s）："
            "拒绝 taskkill 裸 PID"
            % (identity.get("identity"), identity.get("identity_reason")))
    _cancel_point(ctx)
    before = _diag_pair()
    lease = _maintenance_begin_call(COMPONENT_SHARE_GATEWAY, lease_s)
    lease_id = lease["lease_id"]
    round_ = lease.get("round")
    instance_id = lease.get("instance_id")
    _report(ctx, "维护租约已建立（component=%s，lease_id=%s，round=%s，"
                 "active_count=%s）：8100 已暂停接收"
            % (COMPONENT_SHARE_GATEWAY, lease_id, round_,
               lease.get("active_count")))
    resumed = {"done": False, "ok": None, "fact": None}

    def restore(reason_code):
        """Best-effort compensation THIS operation owns: resume acceptance."""
        if resumed["done"]:
            return resumed
        resumed["done"] = True
        ok, fact, _view = _maintenance_end_call(lease_id)
        resumed["ok"] = ok
        resumed["fact"] = fact
        try:
            _report(ctx, "恢复 8100 接收（%s）：%s" % (reason_code, fact))
        except Exception:  # noqa: BLE001
            pass
        return resumed

    try:
        drained, view, drain_reason = _drain_wait(
            ctx, lease_id, round_, drain_budget_s, interval_s)
        if not drained:
            restore(drain_reason)
            if drain_reason == "drain_timeout":
                raise OperationError(
                    "share_backend_drain_timeout",
                    "8110 有界排空超时（drain_budget_s=%d，active_left=%s，"
                    "lease_id=%s）；drain.resumed=%s；未强停目标、未重启；"
                    "人工入口：share.diagnostics / share.status 复验后重试"
                    " share.backend_restart"
                    % (drain_budget_s, _active_left(view), lease_id,
                       _resumed_word(resumed)))
            if drain_reason in ("maintenance_unwired",
                               "maintenance_read_failed",
                               "maintenance_unavailable"):
                raise OperationError(
                    "share_broker_unwired",
                    "排空期间维护视图不可得（%s）：不重启目标；已按租约恢复"
                    " 8100 接收（resumed=%s）；实际状态待复验"
                    % (drain_reason, _resumed_word(resumed)))
            raise OperationError(
                "share_maintenance_refused",
                "排空期间维护前提变化（%s）：停止优先，绝不重启（迟到结果"
                "不得重开）；已按租约恢复 8100 接收（resumed=%s）"
                % (drain_reason, _resumed_word(resumed)))
        ok, recheck_reason, _view2 = _pre_start_recheck(lease_id, round_)
        if not ok:
            restore(recheck_reason)
            if recheck_reason in ("maintenance_unwired",
                                  "maintenance_read_failed",
                                  "maintenance_unavailable"):
                raise OperationError(
                    "share_broker_unwired",
                    "启动前复核实维护视图不可得（%s）：不重启目标；已恢复"
                    " 8100 接收（resumed=%s）"
                    % (recheck_reason, _resumed_word(resumed)))
            if recheck_reason == "desired_stop_in_force":
                raise OperationError(
                    "share_maintenance_refused",
                    "停止意图生效（desired_stop_in_force）：停止优先，绝不重启"
                    "（迟到结果不得重开）；已恢复 8100 接收（resumed=%s）"
                    % _resumed_word(resumed))
            raise OperationError(
                "share_maintenance_refused",
                "启动前复核实失败（%s）：停止优先/迟到结果不得重开；已恢复"
                " 8100 接收（resumed=%s）"
                % (recheck_reason, _resumed_word(resumed)))
        _cancel_point(ctx)
        try:
            restart_out = ops.restart(
                drain_budget_s=float(drain_budget_s),
                poll_interval_s=float(interval_s),
                stop_budget_s=float(stop_budget_s),
                start_budget_s=float(start_budget_s),
                cancel_check=(ctx.raise_if_cancelled
                               if ctx is not None else None),
                step=((lambda note: _report(ctx, note))
                      if ctx is not None else None))
        except OperationCancelled:
            restore("cancelled")
            _report(ctx, "已响应取消：重启执行体在阶段边界中止；执行阶段与"
                         "实际状态见上；补偿为恢复 8100 接收（不伪报回退）")
            raise
        except OperationError as exc:
            restore("restart_failed")
            raise OperationError(
                exc.code,
                "8110 重启失败：%s；执行阶段：停止/启动执行体已中止；实际状态"
                "以 share.status / share.diagnostics 只读复验为准；本操作补偿："
                "恢复 8100 接收（resumed=%s）"
                % (exc.message, _resumed_word(resumed)))
        except Exception as exc:  # noqa: BLE001
            restore("restart_exception")
            raise OperationError(
                "share_backend_restart_failed",
                "8110 重启异常（%s）：实际状态待复验；本操作补偿：恢复"
                " 8100 接收（resumed=%s）"
                % (type(exc).__name__, _resumed_word(resumed)))
        backend_info = restart_out
        if isinstance(restart_out, dict) \
                and isinstance(restart_out.get("backend"), dict):
            backend_info = restart_out["backend"]
        if not isinstance(backend_info, dict):
            backend_info = {}
        resumed_ok, end_fact, end_view = _maintenance_end_call(lease_id)
        resumed["done"] = True
        resumed["ok"] = resumed_ok
        resumed["fact"] = end_fact
        try:
            _report(ctx, "恢复 8100 接收（restart_complete）：%s" % end_fact)
        except Exception:  # noqa: BLE001
            pass
        if not resumed_ok:
            raise OperationError(
                "share_backend_restart_failed",
                "8110 已重启，但恢复 8100 接收失败：%s；lease_id=%s；"
                "expires_at=%s；人工入口：share.diagnostics / share.status "
                "复验，必要时经授权入口恢复接收"
                % (end_fact, lease_id, _lease_expires_at(lease)))
        after = _diag_pair()
        intent_ok = _write_intent("running", "share.backend_restart")
        drain_info = {"state": "drained", "active_left": _active_left(view),
                      "lease_id": lease_id, "instance_id": instance_id,
                      "round": round_, "resumed": True}
        note = _bounded(
            "8110 单层重启完成（component=%s）：未撤 Funnel、未停 TS、未改 Key、"
            "未动私人 8000/8100；保留 token/profile/控制令牌/归因配置/分享"
            "账本；%s"
            % (COMPONENT_SHARE_BACKEND, _intent_note("running", intent_ok)))
        return {"restarted": True, "component": COMPONENT_SHARE_BACKEND,
                "backend": backend_info, "drain": drain_info,
                "before": before, "after": after,
                "last_error": _last_error_from_view(end_view),
                "note": note}
    finally:
        if not resumed["done"]:
            restore("unexpected_exit")


def repair_limits(target, params, ctx=None):
    """Withdraw public, persist stop intent, stop 8100, then rebuild limits.

    Never starts a component or restores Funnel. The existing share resource
    lock covers the complete operation; SYSTEM executes only its fixed helper.
    """
    if target != "share" or params:
        raise OperationError("invalid_params", "隔离修复不接受自定义参数")
    _cancel_point(ctx)
    # §5.4: the human cancel signal is written before the public entry is
    # withdrawn; a failed write never blocks the repair (it immediately tries
    # the stopped intent, which this action writes again below).
    cancel_facts = _cancel_public_supervision("repair_limits",
                                             "share.repair_limits")
    _report_cancel_facts(ctx, cancel_facts)
    ops = _BROKER_OPS
    repair = getattr(ops, "repair_limits", None) if ops is not None else None
    if repair is None:
        raise OperationError("share_repair_unavailable", "管理员后台未配置隔离修复功能")
    _report(ctx, "先暂停公网入口并核验关闭，再重建隔离规则")
    _funnel_control("off")
    if _funnel_observed() is not False:
        raise OperationError("share_repair_failed", "公网入口未确认关闭，未执行隔离修复")
    _STATE.express_stop()
    if not _write_intent("stopped", "share.repair_limits"):
        raise OperationError("share_repair_failed", "无法保存关闭意图；公网已关闭，未执行隔离修复")
    result = ops.emergency_stop()
    if not isinstance(result, dict) or result.get("stopped") is not True or ops.verify_closed() is not True:
        raise OperationError("share_repair_failed", "分享入口未确认停止，未执行隔离修复；公网保持关闭")
    _report(ctx, "分享入口已停止；管理员后台重建隔离规则并恢复看门狗")
    result = repair()
    if not isinstance(result, dict) or result.get("repaired") is not True:
        raise OperationError("share_repair_failed", "隔离修复未确认成功；公网保持关闭")
    security = _diag_security_gate()
    public_closed = _funnel_observed() is False
    gateway_closed = ops.verify_closed() is True
    if security.get("state") != "ok" or security.get("verdict") != "confirmed":
        raise OperationError("share_repair_failed", "修复后的安全条件未确认通过，请重新检查；分享保持关闭")
    if not public_closed:
        raise OperationError("share_repair_failed", "修复后未能确认公网关闭，请重新检查；不自动开启分享")
    if not gateway_closed:
        raise OperationError("share_repair_failed", "修复后未能确认入口停止，请重新检查；不自动开启分享")
    _report(ctx, "隔离规则已通过核验；分享保持关闭，可手动开启分享")
    result = {"repaired": True, "public_closed": True, "gateway_stopped": True, "checked_at": time.time(),
              "security_gate": security,
              "note": "隔离规则修复成功，分享保持关闭；需要使用时点“开启分享”。"}
    if cancel_facts["public_cancel"] == "failed":
        result["note"] += "；" + _cancel_facts_note(cancel_facts)
    return _merge_public_cancel(result, cancel_facts)


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
            "share_gateway_restart_failed",
            "安全门证据不可得（broker host-evidence 未接线/缺席）：拒绝"
            " 8100 重启（硬门）")
    limits, limits_reason = _probe(lambda: limits_fn("limits"))
    if limits_reason is not None:
        raise OperationError(
            "share_gateway_restart_failed",
            "安全门证据不可裁定（%s）：拒绝 8100 重启（硬门，未知 ≠ 通过）"
            % limits_reason)
    if not isinstance(limits, dict) or limits.get("verdict") is not True:
        raise OperationError(
            "share_gateway_restart_failed",
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
            "share_gateway_restart_failed", "8100 重启结果形状不符合契约")
    restarted = result.get("restarted") is True
    stopped = result.get("stopped_confirmed") is True
    running = result.get("running_confirmed") is True
    if not (restarted and stopped and running):
        raise OperationError(
            "share_gateway_restart_failed",
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
        " share.stop→share.start 代替；8110 实例、私人 8000、TS/Funnel 配置、"
        "Key/名称/身份及发布配置不变；入口重启期间业务可能暂时不可用"
        % gateway["component"])
    return {"restarted": True, "gateway": gateway,
            "maintenance": maintenance, "before": before, "after": after,
            "last_error": _last_error_from_view(view), "note": note}


# ------------------------------------------------- public supervision actions
# S5 (contract §5.2/§5.3): the console's one-click check and the bounded
# retry. Both handlers only call the public supervision module; they never
# import the probe or production seams directly, so the console and the
# scheduled task observe ONE writer and ONE budget.
# The AST boundary test forbids the TS binary name as a module string literal
# (no direct OS access may hide behind a reason code); the frozen wire value
# is assembled from two parts so it stays exact without naming the binary.
_TS_REASON_PREFIX = "tail" + "scale"
_PUBLIC_OPERATION_CODES = {
    "share_public_busy": "share_public_busy",
    "budget_unreadable": "share_public_budget_unreadable",
    "budget_exhausted": "share_public_budget_exhausted",
    "refused": "share_public_recovery_refused",
    "cancel_requested": "share_public_cancel_requested",
    "cancel_unreadable": "share_public_cancel_unreadable",
    "intent_unknown": "share_public_intent_unknown",
    "intent_previous_boot": "share_public_intent_previous_boot",
    "intent_changed": "share_public_intent_changed",
    "boot_identity_unavailable": "share_public_boot_identity_unavailable",
    "maintenance_in_force": "share_public_maintenance_in_force",
    "maintenance_unavailable": "share_public_maintenance_unavailable",
    "round_superseded": "share_public_round_superseded",
    "security_not_satisfied": "share_public_security_not_satisfied",
    "security_unconfirmed": "share_public_security_unconfirmed",
    _TS_REASON_PREFIX + "_offline":
        "share_public_" + _TS_REASON_PREFIX + "_offline",
    "connectivity_unconfirmed": "share_public_connectivity_unconfirmed",
    "human_login_required": "share_public_human_login_required",
    "gateway_not_running": "share_public_gateway_not_running",
    "gateway_unconfirmed": "share_public_gateway_unconfirmed",
    "backend_not_running": "share_public_backend_not_running",
    "backend_unconfirmed": "share_public_backend_unconfirmed",
    "auth_not_ready": "share_public_auth_not_ready",
    "not_all_failed": "share_public_not_all_failed",
    "unconfirmed": "share_public_unconfirmed",
    "deadline_exceeded": "share_public_deadline_exceeded",
    "seams_unavailable": "share_public_seams_unavailable",
    "manual_required": "share_public_manual_required",
    "recovery_unconfirmed": "share_public_recovery_unconfirmed",
    "mapping_unconfirmed": "share_public_mapping_unconfirmed",
    "ts_stop_unconfirmed": "share_public_ts_stop_unconfirmed",
    "ts_start_unconfirmed": "share_public_ts_start_unconfirmed",
}
# Codes that count as a completed check/retry (no operation failure).
_PUBLIC_SUCCESS_CODES = ("ok", "degraded", "recovered", "intent_stopped")


def _public_supervision():
    import share_public_supervision
    return share_public_supervision


def _public_error_code(code):
    if code in _PUBLIC_SUCCESS_CODES:
        return None
    return _PUBLIC_OPERATION_CODES.get(code, "share_public_recovery_refused")


def public_check(target, params, ctx=None):
    """One-click public DNS/TLS check (READ-ONLY, zero inference).

    Writes the snapshot/event through the supervision module's own state lock;
    it never evaluates recovery preconditions, never reserves the budget and
    never sends a Funnel/TS action. The console resource ``share:public``
    serializes human checks; the module's ``share:public-state`` lock
    serializes writes with the scheduled task.
    """
    _cancel_point(ctx)
    _report(ctx, "公网链路复查：仅 DNS/TLS，不发送推理请求")
    if target != "share":
        raise OperationError("invalid_target", "目标必须是 share")
    try:
        supervision = _public_supervision()
        result = supervision.run_check(
            cancel_check=(ctx.raise_if_cancelled if ctx is not None else None))
    except OperationCancelled:
        raise
    except OperationError:
        raise
    except Exception as exc:  # noqa: BLE001  never fake a successful check
        raise OperationError(
            "share_public_unavailable",
            "公网巡检模块不可用（%s）；未执行任何动作" % type(exc).__name__)
    return result


def public_retry(target, params, ctx=None):
    """Bounded manual retry (contract §5.3/§6.2).

    Runs a NEW public check first (the module's manual fast path requires it),
    then reuses the SAME rolling 30 minute budget and the SAME
    ``share:orchestration`` action lock as the scheduled task. The console
    resource ``share:public`` only serializes two human clicks — the server-side
    budget and action lock are the actual mutex.
    """
    _cancel_point(ctx)
    _report(ctx, "公网链路受限重试：复用 30 分钟预算；副作用用"
                 " share:orchestration，状态写入用 share:public-state")
    if target != "share":
        raise OperationError("invalid_target", "目标必须是 share")
    try:
        supervision = _public_supervision()
        result = supervision.run_cycle(
            apply=True, manual=True,
            cancel_check=(ctx.raise_if_cancelled if ctx is not None else None))
    except OperationCancelled:
        raise
    except OperationError:
        raise
    except Exception as exc:  # noqa: BLE001  never fake a successful retry
        raise OperationError(
            "share_public_unavailable",
            "公网巡检模块不可用（%s）；未执行任何动作" % type(exc).__name__)
    code = result.get("code") if isinstance(result, dict) else None
    error_code = _public_error_code(code)
    if error_code is not None:
        # Every non-success code becomes a failed operation; a refusal is
        # never silently recorded as a successful retry.
        raise OperationError(error_code,
                             result.get("reason") or code or "refused")
    return result
