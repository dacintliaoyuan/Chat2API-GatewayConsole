# -*- coding: utf-8 -*-
"""UM (chat2api) re-login orchestration adapter for the unified console.

Semantics reuse the legacy console ``Invoke-Chat2ApiReauthentication``:
chat2api's ``POST /admin/auth/login`` is the ONLY force re-login entry and
the backend login thread has no cancel endpoint. This adapter therefore:

* by default verifies current credentials through the restricted verify
  endpoint; valid credentials skip the login POST, while unknown verification
  refuses to open a login browser;
* an explicit ``force=true`` skips that verification and starts ONE login
  round directly; ``visible=true`` asks that round to show a local window and
  is valid only together with ``force`` (ADR 0005);
* never calls refresh and never sends any billing request (the login POST
  remains the established force re-entry for credentials needing login);
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
* ``force`` / ``visible`` (both optional bool, default false): an explicit
  forced re-login skips the on-demand verification; ``visible`` may only be
  requested together with ``force`` and asks chat2api for a visible window.

Error codes: status_unavailable / start_failed / login_unreachable /
login_busy / login_unavailable / login_verification_unknown /
login_timeout_not_settled / login_manual_takeover_required / login_failed /
stop_failed, plus restricted verification errors from console_um_adapter.
``login_busy`` means another login browser owns the profile, and
``login_unavailable`` means the visible window cannot be opened (no display or
no usable browser). A cancelled login round maps to OperationCancelled.

Ticket #15: a terminal manual-takeover round and an exhausted recovery fail
immediately with the reason instead of waiting for the console budget; a bare
``failed`` that chat2api may still retry keeps waiting. An operator cancel of
THIS round propagates as OperationCancelled (the task layer's cancelled state)
after restoring the service. Cancellation propagates as OperationCancelled and is
mapped by the task layer. The token is never included in messages or results.

Decision recorded here: a definitive-looking intermediate state (e.g.
``failed``) is NOT fast-failed; chat2api may retry internally, so the
console keeps polling within its budget and the budget-exhaustion recheck
reports the actually observed final state honestly.
"""

import json
import time
import urllib.error
import urllib.request

from login_contract import (
    MANUAL_TAKEOVER_REQUIRED_PREFIX,
    terminal_manual_reason,
)
from console import adapters as console_adapters
from console import um_adapter as console_um_adapter
from console.operations import OperationCancelled, OperationError

# The login POST is a state-changing force entry; 10s is ample for a loopback
# call. The status poll keeps the historical 5s per request, even though the
# shared adapter's own default is 10s (ticket #42 item 17).
_LOGIN_TIMEOUT_S = 10.0
_STATUS_POLL_TIMEOUT_S = 5.0

_START_WAIT_DEFAULT_S = 30.0
_LOGIN_BUDGET_DEFAULT_S = 190.0
_INTERVAL_DEFAULT_S = 1.0
_BUDGET_MAX_S = 3600.0

# Login-round outcomes shared with the gateway's explicit login entry
# (ADR 0005). The console maps them onto distinct operation results instead
# of collapsing a busy profile or an unavailable display into a generic
# unreachable failure. ``login_already_running`` is the current gateway's
# profile-lock reason; the others are the published outcome names.
_LOGIN_CANCELLED_CODES = frozenset(("cancelled", "login_cancelled"))
_LOGIN_BUSY_CODES = frozenset(("login_busy", "login_already_running"))
_LOGIN_UNAVAILABLE_CODES = frozenset((
    "login_unavailable", "visible_login_unavailable"))

# Ticket #15/#42: the chat2api terminal manual-takeover code and reasons are
# shared with the service through ONE contract module; the three reasons are also
# accepted bare, so a console talking to an older chat2api instance still names
# the manual step instead of waiting for the full budget.
_MANUAL_TAKEOVER_PREFIX = MANUAL_TAKEOVER_REQUIRED_PREFIX

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


def _bool_param(params, key):
    """Read one optional bool parameter; a present non-bool is a request error.

    The manager's schema already rejects non-bools, but the handler is also
    called directly by tests and by older callers, so the same contract is
    enforced here instead of silently treating a typo as False.
    """
    raw = params.get(key) if isinstance(params, dict) else None
    if raw is None:
        return False
    if not isinstance(raw, bool):
        raise OperationError("invalid_params", "%s must be a bool" % key)
    return raw


def _login_code(value):
    """Normalize one status/error field to a known login-round outcome code."""
    if not isinstance(value, str):
        return None
    code = value.strip().split(":", 1)[0].strip()
    if code in _LOGIN_CANCELLED_CODES:
        return "cancelled"
    if code in _LOGIN_BUSY_CODES:
        return "login_busy"
    if code in _LOGIN_UNAVAILABLE_CODES:
        return "login_unavailable"
    return None


def _login_round_code(status):
    """The login-round outcome carried by a status payload, or None.

    The gateway may publish the outcome as the round state, the error code, the
    recovery pause reason, or the primary failure masked by a cleanup failure.
    """
    recovery = status.get("recovery") or {}
    for value in (status.get("state"), status.get("error"),
                   status.get("primary"), recovery.get("pause_reason")):
        code = _login_code(value)
        if code is not None:
            return code
    return None


def _login_round_error(code, detail=None):
    """Map a known login-round outcome onto its console operation error."""
    if code == "cancelled":
        return OperationCancelled()
    if code == "login_busy":
        message = "登录未开始：同一 profile 已有登录浏览器占用（login_busy）"
    else:
        message = ("登录未开始：无法打开登录窗口"
                   "（login_unavailable / visible_login_unavailable）")
    if isinstance(detail, str) and detail.strip():
        message += "：%s" % " ".join(detail.split())[:300]
    return OperationError(code, message)


def _response_code(payload):
    """Find a login-round outcome code in one HTTP error body.

    The gateway's error envelope may carry the code at the top level or under
    ``error`` / ``error.detail``; all shapes are read without trusting the
    body for anything else.
    """
    if not isinstance(payload, dict):
        return None
    candidates = [payload.get("code"), payload.get("error"),
                  payload.get("reason"), payload.get("state"),
                  payload.get("message")]
    error = payload.get("error")
    if isinstance(error, dict):
        candidates.extend([error.get("code"), error.get("type"),
                            error.get("reason"), error.get("state"),
                            error.get("message")])
        detail = error.get("detail")
        if isinstance(detail, dict):
            candidates.extend([detail.get("code"), detail.get("reason"),
                                detail.get("state")])
        elif isinstance(detail, str):
            candidates.append(detail)
    for value in candidates:
        code = _login_code(value)
        if code is not None:
            return code
    return None


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
    except Exception:  # noqa: BLE001 — 轮询读失败按“尚未观察到”处理
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
    except Exception as exc:  # noqa: BLE001 — 状态未知即 fail-closed
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


def _post_force_login(base_url, token, visible=False):
    """POST /admin/auth/login with the X-Admin-Control header and JSON body.

    No Origin header is sent (urllib default). The body carries only the
    per-round presentation choice: ``{"visible": <bool>}``. 202 is the only
    accepted answer; the response body is drained (best effort) but not trusted -
    the follow-up status polling is the authority for login progress. A non-202
    answer is read for the gateway's own login-round outcome so a busy profile
    or an unavailable display is reported as such instead of a generic
    unreachable failure.
    """
    body = json.dumps({"visible": bool(visible)}).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/admin/auth/login",
        data=body,
        headers={"X-Admin-Control": token,
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=_LOGIN_TIMEOUT_S) as resp:
            status_code = resp.status
            resp.read()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 — 读取错误体失败时退回状态码
            raw = b""
        try:
            exc.close()
        except Exception:  # noqa: BLE001 — 释放 HTTPError 是尽力而为
            pass
        payload = None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            payload = None
        code = _response_code(payload)
        if code is not None:
            raise _login_round_error(code)
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


def _read_auth_status(base_url, token):
    """One GET /admin/auth/status read; state None means 'unknown'.

    Ticket #41: the read delegates to ``console_um_adapter.read_auth_status``
    so the console uses the SAME classification (``control_denied`` /
    ``status_unreachable`` / ``status_query_failed`` / ``status_schema``)
    instead of collapsing every failure into ``state=None, error=None``. A
    read failure is carried in ``read_error``; poll callers still keep
    waiting within their budget for a transient failure, but a configuration
    or schema error ends the wait honestly. Ticket #15 keeps only the
    whitelisted terminal fields (state/error/recovery state and pause
    reason) so a manual takeover or an operator cancel can end the wait
    immediately. Ticket #42 keeps ``primary`` (the outcome a cleanup failure
    masked) and bounds each poll request to the historical 5s.
    """
    try:
        status = console_um_adapter.read_auth_status(
            base_url, token, timeout=_STATUS_POLL_TIMEOUT_S)
    except OperationError as exc:
        return {"state": None, "error": None, "recovery": None,
                "primary": None,
                "read_error": {"code": exc.code, "message": exc.message}}
    return {
        "state": status["state"],
        "error": status["error"],
        "recovery": status["recovery"],
        "primary": status.get("primary"),
        "read_error": None,
    }


def _login_cancelled(status):
    """True when chat2api reports THIS login round as cancelled.

    An operator cancel is not a credential verdict: the console task layer
    already has the ``cancelled`` state for it, so the orchestration raises
    OperationCancelled (after restoring the service) instead of a failed login.
    """
    recovery = status.get("recovery") or {}
    return (status.get("state") == "cancelled"
            or status.get("error") == "cancelled"
            or recovery.get("pause_reason") == "cancelled"
            or status.get("primary") == "cancelled")


def _terminal_login_failure(status):
    """Map a TERMINAL chat2api login failure to one console error, or None.

    Only a terminal fact ends the console wait early: a manual-takeover round
    (which chat2api has already ended and whose browser is closed) and a
    recovery cycle that is exhausted. A bare ``failed`` while the recovery
    cycle is still running is NOT terminal: chat2api may retry it internally,
    so the console keeps polling within its own budget. Cancellation is handled
    separately by ``_login_cancelled`` and keeps the cancelled state.
    """
    state = status.get("state")
    error = status.get("error")
    primary = status.get("primary")
    recovery = status.get("recovery") or {}
    recovery_state = recovery.get("state")
    pause_reason = recovery.get("pause_reason")

    manual_reason = terminal_manual_reason(error, pause_reason, primary)
    manual = (None if manual_reason is None
              else _MANUAL_TAKEOVER_PREFIX + manual_reason)
    if manual is not None:
        message = "登录需要人工接管（%s）；本轮已结束，浏览器已关闭" % manual
        if (isinstance(error, str)
                and terminal_manual_reason(error) is None):
            message += "；清理失败：%s" % error
        return ("login_manual_takeover_required", message)

    if recovery_state == "exhausted":
        reason = pause_reason or error or "failed"
        message = "登录失败（%s）；自动恢复已结束" % reason
        if isinstance(primary, str) and primary != reason:
            message += "；原始原因：%s" % primary
        return ("login_failed", message)
    return None


def _terminal_status_read_failure(status):
    """Map a status-read failure that waiting cannot fix to an error.

    A rejected control header (``control_denied``) and a malformed response
    (``status_schema``) are configuration/data errors: polling again cannot
    produce a different result. Transient read failures
    (``status_unreachable`` / ``status_query_failed``) stay within the poll
    budget and are reported by the final timeout message.
    """
    read_error = status.get("read_error")
    if not isinstance(read_error, dict):
        return None
    code = read_error.get("code")
    if code not in ("control_denied", "status_schema"):
        return None
    return OperationError(
        code,
        "无法读取认证状态，登录等待已结束：%s"
        % (read_error.get("message") or code))


def _classify_auth_status(status):
    """Return the observed state, or raise the terminal outcome.

    Shared by the polling loop and the final recheck so both treat a valid,
    cancelled, manual-takeover, exhausted-recovery or unreadable status the
    same way (ticket #42 review).
    """
    if status["state"] == "valid":
        return status["state"]
    read_failure = _terminal_status_read_failure(status)
    if read_failure is not None:
        raise read_failure
    if _login_cancelled(status):
        raise OperationCancelled()
    round_code = _login_round_code(status)
    if round_code is not None:
        # A busy profile or an unavailable visible window is a definitive
        # outcome for THIS round, not a transient status read: waiting for the
        # full budget cannot produce a different answer.
        raise _login_round_error(round_code)
    failure = _terminal_login_failure(status)
    if failure is not None:
        raise OperationError(*failure)
    return status["state"]


def _wait_login_valid(base_url, token, ctx, budget_s, interval_s):
    """Poll auth status until valid or the console budget is exhausted.

    Returns (state, waited_s, at_recheck, read_error). state == "valid"
    means the login settled; anything else is the budget-exhaustion outcome
    where state is the single recheck result (None when even the recheck was
    unreachable). A terminal manual-takeover or exhausted-recovery failure
    raises the mapped OperationError immediately; an operator cancel raises
    OperationCancelled. A configuration/schema status-read failure also ends
    the wait immediately, while a transient read failure keeps polling and is
    carried in ``read_error`` for the timeout message. Both restore the
    service first instead of waiting for the budget.
    Cooperative cancel: the event is checked before every poll and before
    the recheck, so a cancelled run stops polling and restores as usual.
    """
    started = time.monotonic()
    deadline = started + budget_s
    while True:
        if ctx is not None:
            ctx.raise_if_cancelled()
        status = _read_auth_status(base_url, token)
        state = _classify_auth_status(status)
        if state == "valid":
            return (state, time.monotonic() - started, False, None)
        if time.monotonic() >= deadline:
            break
        time.sleep(interval_s)
    # Budget exhausted: exactly one recheck (phase 5) before reporting.
    if ctx is not None:
        ctx.raise_if_cancelled()
    status = _read_auth_status(base_url, token)
    state = _classify_auth_status(status)
    if state == "valid":
        return (state, time.monotonic() - started, True, None)
    return (state, time.monotonic() - started, True,
            status.get("read_error"))


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


def _success_note(waited, at_recheck, fact, login_skipped):
    parts = (["当前账号有效，无需重新登录（state=valid）"] if login_skipped
             else ["登录完成（state=valid）"])
    if waited is not None and not login_skipped:
        parts.append("等待 %.1fs" % waited)
    if at_recheck:
        parts.append("预算耗尽后的复查确认")
    parts.append(fact)
    return "；".join(parts)


def _timeout_message(final_state, waited, fact, read_error=None):
    observed = final_state if final_state else "不可达"
    if isinstance(read_error, dict) and read_error.get("code"):
        observed += "（读取失败：%s）" % read_error["code"]
    return ("登录在控制台等待上限内未确认完成（等待 %.1fs，复查状态=%s）；"
            "控制台超时不保证后端线程已结束，实际状态可能仍在变化；%s"
            % (waited, observed, fact))


def relogin_um(target, params, ctx=None):
    """Verify UM credentials and re-login only when needed, restoring state.

    ``force=true`` skips the on-demand verification and sends the single login
    POST directly (ADR 0005); ``visible=true`` is accepted only with
    ``force`` and asks that explicit round for a visible window. See the module
    docstring for the lock order, cancel semantics and the honest-timeout
    contract. Returns only on full success (credentials verified valid or login
    settled to valid, AND the service state is back to its pre-operation shape
    or the service was left untouched because it was already running).
    """
    if ctx is not None:
        ctx.raise_if_cancelled()
    force = _bool_param(params, "force")
    visible = _bool_param(params, "visible")
    if visible and not force:
        raise OperationError(
            "invalid_params",
            "visible=true 只能与 force=true 一起使用；可见窗口只用于显式强制重新登录")
    if force:
        _report(ctx, "开始 UM 登录编排：显式强制重新登录（%s）"
                % ("可见窗口" if visible else "headless"))
    else:
        _report(ctx, "开始 UM 登录编排：先核验当前认证，仅在需要时重新登录")

    # Token errors (absent/unreadable/empty) pass through unchanged; the
    # token value itself never enters messages, steps or results.
    token = console_um_adapter.read_control_token()
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

    login_skipped = False
    state, waited, at_recheck, read_error = None, 0.0, False, None
    try:
        base_url = console_um_adapter.resolve_chat2api_base_url()
        if ctx is not None:
            ctx.raise_if_cancelled()
        if force:
            # ADR 0005: the explicit forced re-login skips the on-demand
            # verification, even when the current credential is still valid.
            _report(ctx, "显式强制重新登录：跳过按需核验，直接发送登录请求（%s）"
                    % ("可见窗口" if visible else "headless"))
            _post_force_login(base_url, token, visible=visible)
            _report(ctx, "登录请求已被 chat2api 接受（202），开始等待登录完成")
            # Phase 4 + 5: bounded cooperative polling with a final recheck.
            state, waited, at_recheck, read_error = _wait_login_valid(
                base_url, token, ctx, login_budget_s, interval_s)
        else:
            _report(ctx, "通过受限核验端点确认当前认证，尚未发送登录请求")
            try:
                verification = console_um_adapter._verify_account(base_url, token)
            except OperationError as exc:
                raise OperationError(
                    exc.code, "无法确认当前认证，暂不打开登录：" + exc.message)
            if ctx is not None:
                ctx.raise_if_cancelled()
            verify_state = verification["verify"]
            if verify_state == "valid":
                login_skipped = True
                state, waited, at_recheck = "valid", 0.0, False
                read_error = None
                _report(ctx, "当前账号有效，无需重新登录，未发送登录请求")
            elif verify_state in ("needs_login", "rejected"):
                # Phase 3: the single force re-login entry, only after a
                # definitive credential result. A rejected credential/access
                # is not necessarily expired, but the user may reauthenticate.
                _post_force_login(base_url, token)
                _report(ctx, "登录请求已被 chat2api 接受（202），开始等待登录完成")
                # Phase 4 + 5: bounded cooperative polling with a final recheck.
                state, waited, at_recheck, read_error = _wait_login_valid(
                    base_url, token, ctx, login_budget_s, interval_s)
            else:
                raise OperationError(
                    "login_verification_unknown",
                    "无法确认当前认证，暂不打开登录；受限核验结果为 unknown")
    except BaseException as exc:  # noqa: BLE001 — 恢复服务后原样重抛，不吞异常
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
            _timeout_message(state, waited, fact, read_error))
    if kind == _RESTORE_FAILED:
        # Authentication is valid but restore could not be confirmed:
        # report the residual instead of a fake full success.
        outcome = ("当前账号有效，无需重新登录" if login_skipped
                   else "登录已完成（state=valid）")
        raise OperationError("stop_failed", outcome + "，但" + fact)
    return {
        "was_running": was_running,
        "restored": kind == _RESTORE_RESTORED,
        "final_state": "valid",
        "login_skipped": login_skipped,
        "login_waited_s": waited,
        "note": _success_note(waited, at_recheck, fact, login_skipped),
    }
