# -*- coding: utf-8 -*-
"""Read-only UM (chat2api) authentication-state adapter for the console.

Scope: unified console authentication adapters. ``um_auth_status`` only
reads chat2api's in-memory state, so its ``verified`` is always False.
``um_verify_account`` performs the separate restricted credential check;
neither path refreshes credentials or opens the login browser. An
unreachable service is reported as unknown, never as invalid.

Endpoint facts (verified against chat2api source):
* ``GET /admin/auth/status`` is a pure memory read (``proxy.auth_status()``);
  200 JSON ``{"state": <str>, "label": <str>, "error": <str|None>}``.
* Auth: header ``X-Admin-Control`` must hmac-match the control token file
  content; client IP must be loopback; an ``Origin`` header is rejected;
  Host must be 127.0.0.1/localhost/::1. Token mismatch -> 403.

Restricted verify endpoint (shipped in chat2api): ``GET /admin/auth/verify``
reuses the same control auth
(loopback + ``X-Admin-Control``, ``Origin`` refused) and performs ONE
classification probe of the upstream ``/api/models`` directory via
``proxy.bearer()``; it never calls refresh and never mutates chat2api's
auth state. 200 JSON is ``{"verify": "valid|rejected|needs_login|unknown",
"reason": <str|null>, "upstream_status": <int|null>, "checked_epoch": <int>,
"note": <str>}``. ``um_verify_account`` is the console-side consumer;
the login orchestration reuses the same verification request before it
decides whether a browser login is needed.

Ticket #15: ``um_auth_status`` also passes the ONE login window
(``login_window_deadline_epoch`` / ``login_window_s``) through, so the
console can show how long the current explicit round may still take. A
malformed/missing window stays unknown (None), never 0.

Handler contract matches the task layer: ``handler(target, params, ctx)``.
Pure stdlib plus the shared console error type; import has no side effects.
"""

import json
import math
import os
import time
import urllib.error
import urllib.request

from console.operations import OperationError

HERE = os.path.dirname(os.path.abspath(__file__))

# The console side only READS this token file (creation on first use is the
# chat2api side's job, so this adapter stays side-effect free). Env override
# wins; tests either set the env before import or patch this constant.
CONTROL_TOKEN_FILE = (
    os.environ.get("CONSOLE_CONTROL_TOKEN_FILE")
    or os.path.join(HERE, os.pardir, "data", "admin-control-token"))

# States chat2api itself reports; anything else still passes through with
# state_known=False instead of being turned into an error.
_KNOWN_STATES = ("valid", "needs_login", "logging_in", "failed")

_TIMEOUT_S = 10.0

_STATE_NOTE = ("内存态（未主动核验）：仅读取 chat2api 自身认证状态，"
               "未向上游发起核验请求")

# Restricted verify contract (see module docstring): the four verify values
# the endpoint must report; anything else is a response-schema error.
_VERIFY_STATES = ("valid", "rejected", "needs_login", "unknown")

# The endpoint probes the upstream itself (its own budget is 15s), so the
# client keeps a margin above that.
_VERIFY_TIMEOUT_S = 20.0

_VERIFY_NOTE = ("受限核验：经 chat2api 受限端点单次探测上游目录，"
                "未调用 refresh、未改认证状态；rejected 表示凭据/访问被拒，"
                "不武断等同过期")


def _read_control_token(path=None):
    """Read the shared admin-control token file; never include its value or
    any file content in error messages. Three distinct facts, never merged
    (absent != unreadable != empty):

    * FileNotFoundError -> ``control_token_absent``: the file does not exist.
    * Other OSError / UnicodeDecodeError -> ``control_token_unreadable``:
      the file exists but its state cannot be determined; the auth state is
      unknown, which must not masquerade as absence.
    * Readable but blank after strip -> ``control_token_empty``.

    All three reject the query; none is an empty-success path. ``path`` lets a
    caller with its own deployment-specific token file (the cloud console uses
    ``USAGE_CONTROL_TOKEN_FILE``) read the SAME file without a second parser;
    the module constant stays the default for the local console and tests.
    """
    token_file = CONTROL_TOKEN_FILE if path is None else path
    try:
        with open(token_file, encoding="utf-8") as f:
            value = f.read().strip()
    except FileNotFoundError:
        raise OperationError(
            "control_token_absent",
            "管理控制令牌文件缺席，无法查询 UM 认证状态")
    except (OSError, UnicodeDecodeError) as exc:
        raise OperationError(
            "control_token_unreadable",
            "管理控制令牌文件不可读（%s）；状态未知，UM 认证状态查询拒绝执行"
            % type(exc).__name__)
    if not value:
        raise OperationError(
            "control_token_empty",
            "管理控制令牌文件为空，无法查询 UM 认证状态")
    return value


def resolve_chat2api_base_url(cloud=False, snapshot=None):
    """Resolve the chat2api base URL from the runtime snapshot.

    The runtime snapshot (console_adapters.service_status) is the single
    source of truth for the service port; hardcoding 8000 is forbidden.
    ``cloud=True`` selects the Linux share entry (``share-gateway``), which is
    the only UM process the cloud role runs. A caller that already read the
    snapshot may pass it in ``snapshot`` so one read feeds both facts.
    """
    from console import adapters as console_adapters  # noqa: WPS433  (lazy: keep import side-effect free)

    if snapshot is None:
        try:
            # Keep the local call signature unchanged: existing callers and
            # tests patch the zero-argument snapshot read.
            if cloud:
                snapshot = console_adapters.service_status(cloud=True)
            else:
                snapshot = console_adapters.service_status()
        except Exception:  # noqa: BLE001 — 状态快照不可用即 fail-closed
            raise OperationError(
                "status_unavailable",
                "无法从 runtime 快照解析 chat2api 服务地址")
    services = snapshot.get("services") if isinstance(snapshot, dict) else None
    if not isinstance(services, list):
        raise OperationError(
            "status_unavailable",
            "无法从 runtime 快照解析 chat2api 服务地址")
    wanted = "share-gateway" if cloud else "chat2api"
    for entry in services:
        if not isinstance(entry, dict) or entry.get("key") != wanted:
            continue
        try:
            port = int(entry.get("port"))
        except (TypeError, ValueError):
            continue
        return "http://127.0.0.1:%d" % port
    raise OperationError(
        "status_unavailable",
        "无法从 runtime 快照解析 chat2api 服务地址")


def _coerce_text(value):
    """Normalize an optional response field to str|None."""
    return value if isinstance(value, str) else None


def _coerce_number(value):
    """Normalize an optional response number to float|None.

    Booleans are not numbers here (``True`` must never become 1.0), and a
    malformed/missing/non-finite window field stays unknown instead of being
    folded to 0. Ticket #42: JSON accepts bare ``NaN``/``Infinity`` tokens but
    neither is a known deadline; keeping one would make the console's own
    ``json.dumps`` emit invalid JSON.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


_RECOVERY_STATES = ("idle", "waiting", "running", "exhausted", "stopped")


def _recovery_summary(value):
    """Whitelist the additive recovery summary from chat2api (A7).

    Only the progress fields the console may display are copied; the reason a
    manual takeover stopped the cycle stays in the ``error`` field and is never
    passed through as page content. A missing or malformed summary is ``None``
    rather than a fabricated zero.
    """
    if not isinstance(value, dict):
        return None
    state = value.get("state")
    if not isinstance(state, str) or state not in _RECOVERY_STATES:
        return None
    summary = {"state": state}
    for key in ("round", "session_starts", "max_sessions"):
        number = value.get(key)
        if isinstance(number, bool) or not isinstance(number, int):
            return None
        summary[key] = number
    next_attempt = value.get("next_attempt_epoch")
    if (isinstance(next_attempt, bool)
            or not isinstance(next_attempt, (int, float))
            or not math.isfinite(float(next_attempt))):
        summary["next_attempt_epoch"] = None
    else:
        summary["next_attempt_epoch"] = float(next_attempt)
    pause_reason = value.get("pause_reason")
    summary["pause_reason"] = _coerce_text(pause_reason)
    # Ticket #42 item 16: a cleanup failure can mask the session outcome. The
    # console read face keeps that primary reason alongside the cleanup error.
    summary["primary"] = _coerce_text(value.get("primary"))
    return summary


def um_auth_status(target, params, ctx=None):
    """Read chat2api's in-memory authentication state (read-only).

    Never sends an Origin header (urllib default), never touches the
    upstream, and never creates or rewrites the token file. The result
    never contains the token itself. Ticket #15 also passes the ONE login
    window deadline through, so the console can show it.
    """
    if ctx is not None:
        ctx.raise_if_cancelled()

    base_url = resolve_chat2api_base_url()
    token = _read_control_token()
    status = _read_auth_status(base_url, token)
    return {
        **status,
        "source": "memory-state",
        "verified": False,
        "state_known": status["state"] in _KNOWN_STATES,
        "note": _STATE_NOTE,
    }


def _read_auth_status(base_url, token, timeout=_TIMEOUT_S):
    """One GET /admin/auth/status read with an already-read token.

    Shared by the read action, the login orchestration and the console
    display-state read so all three use the same HTTP boundary and the same
    whitelist. Ticket #15 passes the login window through; malformed window
    fields are unknown (None), never 0. Ticket #42 lets the login orchestration
    keep its historical 5s poll timeout while the other callers keep the
    adapter default, and passes the primary cleanup outcome through.
    """
    request = urllib.request.Request(
        base_url.rstrip("/") + "/admin/auth/status",
        headers={"X-Admin-Control": token})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            status_code = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            exc.close()  # best-effort: release the wrapped response socket
        except Exception:  # noqa: BLE001 — 释放 HTTPError 是尽力而为
            pass
        if exc.code in (401, 403):
            raise OperationError(
                "control_denied",
                "管理控制头被拒绝（控制令牌不匹配）")
        raise OperationError(
            "status_query_failed",
            "查询失败：chat2api 返回 HTTP %d，凭据状态未知（未知 ≠ 失效）"
            % exc.code)
    except (urllib.error.URLError, OSError, TimeoutError):
        # Connection refused, DNS failure and socket timeouts all land here.
        raise OperationError(
            "status_unreachable",
            "查询失败：chat2api 服务不可达，凭据状态未知（未知 ≠ 失效）")

    if status_code != 200:
        raise OperationError(
            "status_query_failed",
            "查询失败：chat2api 返回 HTTP %d，凭据状态未知（未知 ≠ 失效）"
            % status_code)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise OperationError(
            "status_schema", "auth status 响应形状与预期不符")
    if not isinstance(payload, dict) or not isinstance(
            payload.get("state"), str):
        raise OperationError(
            "status_schema", "auth status 响应形状与预期不符")

    return {
        "state": payload["state"],
        "label": _coerce_text(payload.get("label")),
        "error": _coerce_text(payload.get("error")),
        # A7 additive recovery summary: explicitly whitelisted fields only.
        # The full recovery dict is never passed through, so a future private
        # field cannot leak through this read face.
        "recovery": _recovery_summary(payload.get("recovery")),
        # Ticket #15 additive login window: the ONE deadline fixed by the
        # explicit login round. A status read never extends it.
        "login_window_deadline_epoch": _coerce_number(
            payload.get("login_window_deadline_epoch")),
        "login_window_s": _coerce_number(payload.get("login_window_s")),
        # Ticket #42 item 16: the console read face keeps the primary session
        # outcome a terminal cleanup failure masked.
        "primary": _coerce_text(payload.get("primary")),
        "checked_epoch": int(time.time()),
    }


def um_verify_account(target, params, ctx=None):
    """One-shot restricted credential verification via chat2api.

    Consumes the ``GET /admin/auth/verify`` restricted endpoint (see the
    module docstring for the contract). The adapter never sends an
    Origin header, never calls refresh and never mutates auth state.
    ``rejected`` means the upstream refused the credential/access and must
    not be read as "expired". The result never contains the control token
    itself.
    """
    if ctx is not None:
        ctx.raise_if_cancelled()

    token = _read_control_token()
    base_url = resolve_chat2api_base_url()
    return _verify_account(base_url, token)


def _verify_account(base_url, token):
    """Verify one resolved instance with an already-read control token.

    Shared by the read action and guarded login orchestration so both use
    the same restricted HTTP boundary, time budget and response checks.
    """
    request = urllib.request.Request(
        base_url.rstrip("/") + "/admin/auth/verify",
        headers={"X-Admin-Control": token})
    try:
        with urllib.request.urlopen(request, timeout=_VERIFY_TIMEOUT_S) as resp:
            status_code = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            exc.close()  # best-effort: release the wrapped response socket
        except Exception:  # noqa: BLE001 — 释放 HTTPError 是尽力而为
            pass
        if exc.code in (401, 403):
            raise OperationError(
                "control_denied",
                "管理控制头被拒绝（控制令牌不匹配）")
        raise OperationError(
            "verify_query_failed",
            "查询失败：chat2api 返回 HTTP %d（受限核验端点），"
            "核验状态未知（未知 ≠ 失效）" % exc.code)
    except (urllib.error.URLError, OSError, TimeoutError):
        # Connection refused, DNS failure and socket timeouts all land here.
        raise OperationError(
            "status_unreachable",
            "查询失败：chat2api 服务不可达，核验状态未知（未知 ≠ 失效）")

    if status_code != 200:
        raise OperationError(
            "verify_query_failed",
            "查询失败：chat2api 返回 HTTP %d（受限核验端点），"
            "核验状态未知（未知 ≠ 失效）" % status_code)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise OperationError("verify_schema", "受限核验响应形状与预期不符")
    if (not isinstance(payload, dict)
            or payload.get("verify") not in _VERIFY_STATES):
        raise OperationError("verify_schema", "受限核验响应形状与预期不符")

    checked_epoch = payload.get("checked_epoch")
    note = _VERIFY_NOTE
    if isinstance(checked_epoch, bool) or not isinstance(checked_epoch, int):
        # The endpoint contract promises an epoch int; when it is missing
        # (or unusable) fall back to the local clock and say so in the note.
        checked_epoch = int(time.time())
        note = note + "；端点未带时间"

    upstream_status = payload.get("upstream_status")
    if isinstance(upstream_status, bool) or not isinstance(upstream_status, int):
        upstream_status = None

    return {
        "verify": payload["verify"],
        "reason": _coerce_text(payload.get("reason")),
        "upstream_status": upstream_status,
        "checked_epoch": checked_epoch,
        "note": note,
    }
