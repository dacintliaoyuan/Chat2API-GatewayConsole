# -*- coding: utf-8 -*-
"""Read-only UM (chat2api) authentication-state adapter for the console.

Scope: P2 unified console read adapters. This module only reads the
in-memory authentication state that chat2api itself maintains. It never
triggers the upstream re-login verification path (planning decision: the
"raw-fresh" active verification has side effects and is intentionally not
used here), so ``verified`` is always False and an unreachable service is
reported as "unknown", never as "invalid".

Endpoint facts (verified against chat2api source):
* ``GET /admin/auth/status`` is a pure memory read (``proxy.auth_status()``);
  200 JSON ``{"state": <str>, "label": <str>, "error": <str|None>}``.
* Auth: header ``X-Admin-Control`` must hmac-match the control token file
  content; client IP must be loopback; an ``Origin`` header is rejected;
  Host must be 127.0.0.1/localhost/::1. Token mismatch -> 403.

Restricted verify endpoint (contract agreed with chat2api; NOT yet shipped
in production): ``GET /admin/auth/verify`` reuses the same control auth
(loopback + ``X-Admin-Control``, ``Origin`` refused) and performs ONE
classification probe of the upstream ``/api/models`` directory via
``proxy.bearer()``; it never calls refresh and never mutates chat2api's
auth state. 200 JSON is ``{"verify": "valid|rejected|needs_login|unknown",
"reason": <str|null>, "upstream_status": <int|null>, "checked_epoch": <int>,
"note": <str>}``. ``um_verify_account`` is the console-side consumer and
lands BEFORE the endpoint exists in production: the registry action stays
disabled, so this path cannot execute against a real service until the
endpoint ships and the action is explicitly enabled.

Handler contract matches the task layer: ``handler(target, params, ctx)``.
Pure stdlib plus the shared console error type; import has no side effects.
"""

import json
import os
import time
import urllib.error
import urllib.request

from console_operations import OperationError

HERE = os.path.dirname(os.path.abspath(__file__))

# The console side only READS this token file (creation on first use is the
# chat2api side's job, so this adapter stays side-effect free). Env override
# wins; tests either set the env before import or patch this constant.
CONTROL_TOKEN_FILE = (
    os.environ.get("CONSOLE_CONTROL_TOKEN_FILE")
    or os.path.join(HERE, "data", "admin-control-token"))

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


def _read_control_token():
    """Read the shared admin-control token file; never include its value or
    any file content in error messages. Three distinct facts, never merged
    (absent != unreadable != empty):

    * FileNotFoundError -> ``control_token_absent``: the file does not exist.
    * Other OSError / UnicodeDecodeError -> ``control_token_unreadable``:
      the file exists but its state cannot be determined; the auth state is
      unknown, which must not masquerade as absence.
    * Readable but blank after strip -> ``control_token_empty``.

    All three reject the query; none is an empty-success path.
    """
    try:
        with open(CONTROL_TOKEN_FILE, encoding="utf-8") as f:
            value = f.read().strip()
    except FileNotFoundError:
        raise OperationError(
            "control_token_absent",
            "管理控制令牌文件缺席，无法查询上游认证状态")
    except (OSError, UnicodeDecodeError) as exc:
        raise OperationError(
            "control_token_unreadable",
            "管理控制令牌文件不可读（%s）；状态未知，上游认证状态查询拒绝执行"
            % type(exc).__name__)
    if not value:
        raise OperationError(
            "control_token_empty",
            "管理控制令牌文件为空，无法查询上游认证状态")
    return value


def resolve_chat2api_base_url():
    """Resolve the chat2api base URL from the runtime snapshot.

    The runtime snapshot (console_adapters.service_status) is the single
    source of truth for the service port; hardcoding 8000 is forbidden.
    """
    import console_adapters  # noqa: WPS433  (lazy: keep import side-effect free)

    try:
        snapshot = console_adapters.service_status()
    except Exception:
        raise OperationError(
            "status_unavailable",
            "无法从 runtime 快照解析 chat2api 服务地址")
    services = snapshot.get("services") if isinstance(snapshot, dict) else None
    if not isinstance(services, list):
        raise OperationError(
            "status_unavailable",
            "无法从 runtime 快照解析 chat2api 服务地址")
    for entry in services:
        if not isinstance(entry, dict) or entry.get("key") != "chat2api":
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


def um_auth_status(target, params, ctx=None):
    """Read chat2api's in-memory authentication state (read-only).

    Never sends an Origin header (urllib default), never touches the
    upstream, and never creates or rewrites the token file. The result
    never contains the token itself.
    """
    if ctx is not None:
        ctx.raise_if_cancelled()

    base_url = resolve_chat2api_base_url()
    token = _read_control_token()

    request = urllib.request.Request(
        base_url.rstrip("/") + "/admin/auth/status",
        headers={"X-Admin-Control": token})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as resp:
            status_code = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            exc.close()  # best-effort: release the wrapped response socket
        except Exception:
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

    state = payload["state"]
    return {
        "state": state,
        "label": _coerce_text(payload.get("label")),
        "error": _coerce_text(payload.get("error")),
        "source": "memory-state",
        "verified": False,
        "state_known": state in _KNOWN_STATES,
        "note": _STATE_NOTE,
        "checked_epoch": int(time.time()),
    }


def um_verify_account(target, params, ctx=None):
    """One-shot restricted credential verification via chat2api.

    Consumes the future ``GET /admin/auth/verify`` restricted endpoint (see
    the module docstring for the contract). The adapter never sends an
    Origin header, never calls refresh and never mutates auth state.
    ``rejected`` means the upstream refused the credential/access and must
    not be read as "expired". The result never contains the control token
    itself.
    """
    if ctx is not None:
        ctx.raise_if_cancelled()

    token = _read_control_token()
    base_url = resolve_chat2api_base_url()

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
        except Exception:
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
