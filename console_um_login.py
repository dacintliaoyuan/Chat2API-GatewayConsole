# -*- coding: utf-8 -*-
"""UM (chat2api) re-login orchestration adapter for the unified console.

Semantics reuse the legacy console ``Invoke-Chat2ApiReauthentication``:
chat2api's ``POST /admin/auth/login`` is the ONLY force re-login entry and
the backend login thread has no cancel endpoint. This adapter therefore:

* never calls refresh and never sends any billing request (the login POST
  is the established force re-entry);
* treats the console-side wait budgets as bounds on WAITING, not as proof
  about the backend thread: on budget exhaustion it rechecks the status
  once and reports honestly that a console timeout does not prove the
  backend thread has ended;
* restores the original service state in every path that changed it.

GLOBAL LOCK ORDER (E-P2-3, single direction, no cycles):
the task-layer worker holds ``account:um`` BEFORE calling this handler;
start/stop inside this orchestration go through the console runtime
wrapper with ``caller_holds_lock=False``, so the wrapper CHILD process
self-holds ``service:chat2api`` (a different lock file). Acquisition is
strictly parent -> child (account -> service) on every path; no path may
hold ``service:chat2api`` first and then acquire ``account:um``. While the
start wrapper child is running it owns ``service:chat2api``; this module
deliberately does NOT issue a concurrent stop before the started state is
confirmed from the snapshot (a racing stop child would block on that lock).

Handler contract matches the task layer: ``handler(target, params, ctx)``.
Cancellation is cooperative: the backend thread cannot be cancelled, so
after observing the cancel event this module stops polling and still
restores the service state as usual.

Injected params (bounded, optional; defaults match the console contract):
* ``start_wait_s`` (default 30): snapshot-wait budget after issuing start.
* ``login_budget_s`` (default 190): console wait budget for the login.
* ``poll_interval_s`` (default 1.0): step for both wait loops (tests use
  small values). Non-numeric or negative values fall back to the default;
  values are capped at 3600s so a bad injection cannot wait unbounded.

Error codes: status_unavailable / start_failed / login_unreachable /
login_timeout_not_settled / stop_failed (cancellation propagates as
OperationCancelled and is mapped by the task layer). The control token is
never included in messages or results.

Decision recorded here: a definitive-looking intermediate state (e.g.
``failed``) is NOT fast-failed; chat2api may retry internally, so the
console keeps polling within its budget and the budget-exhaustion recheck
reports the actually observed final state honestly.
"""

import json
import time
import urllib.error
import urllib.request

import console_adapters
import console_um_adapter
from console_operations import OperationCancelled, OperationError

# The login POST is a state-changing force entry; 10s is ample for a loopback
# call. The status poll timeout is contract-fixed at 5s per request.
_LOGIN_TIMEOUT_S = 10.0
_STATUS_TIMEOUT_S = 5.0

_START_WAIT_DEFAULT_S = 30.0
_LOGIN_BUDGET_DEFAULT_S = 190.0
_INTERVAL_DEFAULT_S = 1.0
_BUDGET_MAX_S = 3600.0

# Restore outcome kinds returned by _restore_service_state:
# kept     - the service was already running; nothing was started or stopped.
# restored - we started the service, then stopped it and verified Stopped.
# failed   - we started the service but could not confirm it is Stopped.
_RESTORE_KEPT = "kept"
_RESTORE_RESTORED = "restored"
_RESTORE_FAILED = "failed"


def _report(ctx, note):
    if ctx is not None:
        ctx.report_step(note)


def _budget_value(params, key, default):
    raw = params.get(key) if isinstance(params, dict) else None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value < 0.0:
        return default
    return min(value, _BUDGET_MAX_S)


def _chat2api_state(snapshot):
    """Best-effort chat2api state from one service_status snapshot; None when
    the snapshot shape is unusable or the entry is missing/ambiguous."""
    services = snapshot.get("services") if isinstance(snapshot, dict) else None
    if not isinstance(services, list):
        return None
    for entry in services:
        if isinstance(entry, dict) and entry.get("key") == "chat2api":
            state = entry.get("state")
            return state if isinstance(state, str) else None
    return None


def _snapshot_chat2api_state():
    """Lenient read for polling loops: a failed snapshot is 'not observed yet'
    and is bounded by the caller's budget, never turned into a fake state."""
    try:
        return _chat2api_state(console_adapters.service_status())
    except Exception:
        return None


def _initial_was_running():
    """Strict phase-1 read: True iff chat2api is Running in one snapshot.

    Any snapshot failure, bad shape or missing entry refuses the whole
    operation (we must not start/stop a service whose state is unknown).
    """
    try:
        snapshot = console_adapters.service_status()
    except OperationError as exc:
        raise OperationError(
            "status_unavailable",
            "无法读取服务快照（%s），无法判定 chat2api 初始状态" % exc.code)
    except Exception as exc:
        raise OperationError(
            "status_unavailable",
            "无法读取服务快照（%s），无法判定 chat2api 初始状态"
            % type(exc).__name__)
    state = _chat2api_state(snapshot)
    if state is None:
        raise OperationError(
            "status_unavailable",
            "服务快照中没有可用的 chat2api 状态，无法判定初始状态")
    return state == "Running"


def _start_and_confirm(ctx, budget_s, interval_s):
    """Start chat2api via the wrapper and wait for the snapshot to say Running.

    The wrapper child self-holds ``service:chat2api`` (caller_holds_lock is
    False on purpose: the parent only holds account:um). The poll reads the
    snapshot BEFORE checking cancellation so a cancel racing with a finished
    start still observes Running and the finally-restore can stop it. When
    the budget passes without ever observing Running, start is reported as
    failed and no stop is issued: the service was never confirmed started
    (and the starting child still owns the service lock, so a racing stop
    would be wrong anyway). No login request is sent on this path.
    """
    _report(ctx, "chat2api 未在运行，经 wrapper 启动（服务锁由 wrapper 子进程自持）")
    try:
        console_adapters._invoke_wrapper(
            "start", key="chat2api", caller_holds_lock=False)
    except OperationError as exc:
        raise OperationError(
            "start_failed",
            "启动 chat2api 失败（%s），未发送登录请求，也未执行停止" % exc.code)
    deadline = time.monotonic() + budget_s
    while True:
        state = _snapshot_chat2api_state()
        if state == "Running":
            return
        if ctx is not None:
            ctx.raise_if_cancelled()
        if time.monotonic() >= deadline:
            raise OperationError(
                "start_failed",
                "启动已请求但 %.0fs 内快照未观察到 Running；未发送登录请求，"
                "也未执行停止（服务未确认启动成功）" % budget_s)
        time.sleep(interval_s)


def _post_force_login(base_url, token):
    """POST /admin/auth/login with only the X-Admin-Control header.

    No Origin header is sent (urllib default). 202 is the only accepted
    answer; the response body is drained (best effort) but not trusted -
    the follow-up status polling is the authority for login progress.
    """
    request = urllib.request.Request(
        base_url.rstrip("/") + "/admin/auth/login",
        headers={"X-Admin-Control": token}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=_LOGIN_TIMEOUT_S) as resp:
            status_code = resp.status
            resp.read()
    except urllib.error.HTTPError as exc:
        try:
            exc.close()
        except Exception:
            pass
        raise OperationError(
            "login_unreachable",
            "登录请求未完成：chat2api 返回 HTTP %d（期望 202）" % exc.code)
    except (urllib.error.URLError, OSError, TimeoutError):
        raise OperationError(
            "login_unreachable", "登录请求未完成：chat2api 服务不可达")
    if status_code != 202:
        raise OperationError(
            "login_unreachable",
            "登录请求未完成：chat2api 返回 HTTP %d（期望 202）" % status_code)


def _read_auth_state(base_url, token):
    """One GET /admin/auth/status read; None means 'unknown this round'.

    Poll callers treat None as 'keep waiting within the budget'; the final
    recheck reports the last actually observed fact instead of guessing.
    """
    request = urllib.request.Request(
        base_url.rstrip("/") + "/admin/auth/status",
        headers={"X-Admin-Control": token})
    try:
        with urllib.request.urlopen(request, timeout=_STATUS_TIMEOUT_S) as resp:
            status_code = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            exc.close()
        except Exception:
            pass
        return None
    except (urllib.error.URLError, OSError, TimeoutError):
        return None
    if status_code != 200:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if isinstance(payload, dict) and isinstance(payload.get("state"), str):
        return payload["state"]
    return None


def _wait_login_valid(base_url, token, ctx, budget_s, interval_s):
    """Poll auth status until valid or the console budget is exhausted.

    Returns (state, waited_s, at_recheck). state == "valid" means the login
    settled; anything else is the budget-exhaustion outcome where state is
    the single recheck result (None when even the recheck was unreachable).
    Cooperative cancel: the event is checked before every poll and before
    the recheck, so a cancelled run stops polling and restores as usual.
    """
    started = time.monotonic()
    deadline = started + budget_s
    while True:
        if ctx is not None:
            ctx.raise_if_cancelled()
        state = _read_auth_state(base_url, token)
        if state == "valid":
            return state, time.monotonic() - started, False
        if time.monotonic() >= deadline:
            break
        time.sleep(interval_s)
    # Budget exhausted: exactly one recheck (phase 5) before reporting.
    if ctx is not None:
        ctx.raise_if_cancelled()
    state = _read_auth_state(base_url, token)
    return state, time.monotonic() - started, True


def _restore_service_state(ctx, started_by_us):
    """Restore the pre-operation service state; never claims a stop it
    cannot verify. Returns (kind, chinese_fact) for messages and notes."""
    if not started_by_us:
        return _RESTORE_KEPT, "chat2api 原本运行，未执行启停，服务保持运行"
    try:
        console_adapters._invoke_wrapper(
            "stop", key="chat2api", caller_holds_lock=False)
    except OperationError as exc:
        return (_RESTORE_FAILED,
                "服务停止未确认（停止调用失败 %s），可能残留运行状态" % exc.code)
    state = _snapshot_chat2api_state()
    if state is None:
        return (_RESTORE_FAILED,
                "停止后快照不可用，未能确认 Stopped，可能残留运行状态")
    if state != "Stopped":
        return (_RESTORE_FAILED,
                "服务停止未确认，可能残留运行状态（复验状态=%s）" % state)
    return _RESTORE_RESTORED, "服务已停止并复验 Stopped"


def _reraise_after_restore(exc, kind, fact):
    """Re-raise the original failure with the restore fact attached.

    OperationError keeps its code and gains the restore fact in the message
    (single line). OperationCancelled is re-raised bare when the restore
    succeeded (the fact is preserved as an operation step); when the
    restore itself failed during cancellation, stop_failed is raised
    instead so the residual fact is not silently lost under "cancelled".
    """
    if isinstance(exc, OperationCancelled):
        if kind == _RESTORE_FAILED:
            raise OperationError(
                "stop_failed", "已响应取消，但" + fact)
        raise exc
    if isinstance(exc, OperationError):
        combined = " ".join((exc.message + "；" + fact).split())
        raise OperationError(exc.code, combined)
    raise exc


def _success_note(waited, at_recheck, fact):
    parts = ["登录完成（state=valid）"]
    if waited is not None:
        parts.append("等待 %.1fs" % waited)
    if at_recheck:
        parts.append("预算耗尽后的复查确认")
    parts.append(fact)
    return "；".join(parts)


def _timeout_message(final_state, waited, fact):
    observed = final_state if final_state else "不可达"
    return ("登录在控制台等待上限内未确认完成（等待 %.1fs，复查状态=%s）；"
            "控制台超时不保证后端线程已结束，实际状态可能仍在变化；%s"
            % (waited, observed, fact))


def relogin_um(target, params, ctx=None):
    """Orchestrate one UM force re-login with full service-state restore.

    See the module docstring for the lock order, cancel semantics and the
    honest-timeout contract. Returns only on full success (login settled to
    valid AND the service state is back to its pre-operation shape or the
    service was left untouched because it was already running).
    """
    if ctx is not None:
        ctx.raise_if_cancelled()
    _report(ctx, "开始上游重新登录编排（chat2api 登录接口为唯一 force 重入口）")

    # Token errors (absent/unreadable/empty) pass through unchanged; the
    # token value itself never enters messages, steps or results.
    token = console_um_adapter._read_control_token()
    _report(ctx, "已读取管理控制令牌")

    start_wait_s = _budget_value(params, "start_wait_s", _START_WAIT_DEFAULT_S)
    login_budget_s = _budget_value(params, "login_budget_s", _LOGIN_BUDGET_DEFAULT_S)
    interval_s = _budget_value(params, "poll_interval_s", _INTERVAL_DEFAULT_S)

    # Phase 1: judge the pre-operation service state from one snapshot.
    was_running = _initial_was_running()
    _report(ctx, "chat2api 初始状态：%s" % ("Running" if was_running else "未运行"))

    started_by_us = False
    if not was_running:
        # Phase 2: start and confirm Running from snapshots.
        _start_and_confirm(ctx, start_wait_s, interval_s)
        started_by_us = True
        _report(ctx, "chat2api 已启动（快照确认 Running）")

    try:
        base_url = console_um_adapter.resolve_chat2api_base_url()
        # Phase 3: the single force re-login entry.
        _post_force_login(base_url, token)
        _report(ctx, "登录请求已被 chat2api 接受（202），开始等待登录完成")
        # Phase 4 + 5: bounded cooperative polling with a final recheck.
        state, waited, at_recheck = _wait_login_valid(
            base_url, token, ctx, login_budget_s, interval_s)
    except BaseException as exc:
        kind, fact = _restore_service_state(ctx, started_by_us)
        _report(ctx, fact)
        _reraise_after_restore(exc, kind, fact)

    # Phase 6: restore. Every path that reaches here owns the decision.
    kind, fact = _restore_service_state(ctx, started_by_us)
    _report(ctx, fact)

    if state != "valid":
        # Console budget exhausted; the recheck state is reported as-is.
        raise OperationError(
            "login_timeout_not_settled",
            _timeout_message(state, waited, fact))
    if kind == _RESTORE_FAILED:
        # Login completed but the restore could not be confirmed: report the
        # residual instead of a fake full success.
        raise OperationError(
            "stop_failed",
            "登录已完成（state=valid），但" + fact)
    return {
        "was_running": was_running,
        "restored": kind == _RESTORE_RESTORED,
        "final_state": "valid",
        "login_waited_s": waited,
        "note": _success_note(waited, at_recheck, fact),
    }
