# -*- coding: utf-8 -*-
"""UM model speedtest for the unified AI-gateway console.

Scope: one single-shot streaming probe (``POST /v1/chat/completions`` with
``stream=true``, body ``{"model", "stream", "messages":[{"role":"user",
"content":"Hi"}]}``) against the UM source (chat2api :8000), reporting TTFB
(first non-empty ``delta.content``) and total stream time.

RETRY PROHIBITION (hard contract): any failure is reported honestly and the
module NEVER retries. This is not an optimization choice: a 401 from chat2api
triggers the server-side refresh state machine (chat2api.py:484
``ReauthenticationRequired`` and the shared visible reauthentication session at
chat2api.py:614), which can launch a REAL browser login. That side effect
belongs to the upstream service; this module sends exactly one request per
invocation and reports the failure.

SSE parsing (UM contract): chat2api.py:33 (POST /v1/chat/completions,
auth-free chat route): skip blank lines and the leading role-only delta block
(``delta.role``); ``delta.reasoning_content`` increments never count as first
char (upstream reasoning is discarded); first non-empty ``delta.content`` wins.
In-band error stream shape: ``data: {"error": ...}`` followed by
``data: [DONE]``.

Generic SSE framing: ``data: {...}`` lines, ``data: [DONE]`` terminator,
``: comment`` lines skipped, blank lines skipped, non-JSON data chunks skipped
(a malformed chunk never fails the whole probe).

Timing uses ``time.monotonic()`` anchored at the moment the request body has
actually been written to the socket (post-connect). Cancel cooperation is
line-granular: ``ctx.raise_if_cancelled()`` between reads, the connection is
closed immediately on cancellation (bounded release) and
``OperationCancelled`` propagates to the task layer, which maps it to the
cancelled state. A budget timeout honestly keeps any already-measured first
char (``ttfb_s``) — 成败均如实.

Batch (B4): ``speedtest_models`` runs an EXPLICIT, deduplicated UM model set
serially through ``speedtest_model`` — one short request per selected model, at
most ``MAX_BATCH_MODELS`` models, NO retry, and NO request for any unselected
model. The whole batch is ONE console operation on the SAME global
``model:speedtest`` resource (``console_adapters._speedtest_resource``), so a UI
loop cannot bypass the mutex; cancellation is checked between models and inside
each stream (the existing bounded release). ``speedtest_action`` is the single
registry entry for both shapes: params carrying ``models`` take the batch path,
while the legacy single-model params keep their exact result shape.

Pure stdlib; importing this module has no side effects.
Handler contract matches the task layer: ``handler(target, params, ctx)``.
"""

import http.client
import json
import socket
import time
import urllib.parse

from console.operations import OperationCancelled, OperationError

DEFAULT_TOTAL_BUDGET_S = 30.0

# Injected total budget (seconds) for the whole streaming probe; tests patch
# this module attribute. A timeout honestly keeps any measured TTFB.
_TOTAL_BUDGET_S = DEFAULT_TOTAL_BUDGET_S

# Explicit source table (UM only; retired sources are not runtime-discovered).
# ``auth`` names the credential store consulted for the inbound Bearer header:
#   None — chat endpoint is auth-free (chat2api /v1 route)
_DEFAULT_MAX_TOKENS = 64
_MAX_TOKENS_CAP = 512  # hard ceiling; the package quota decides less

SOURCES = {
    "um": {"auth": None},
}

BILLING_NOTE = "测速为真实计费请求；用量/费用以 UM 账本为准"

# Snapshot service key (gateway-runtime descriptors, single port authority:
# lib/gateway-runtime.ps1 — chat2api :8000).
_SOURCE_SERVICE_KEYS = {
    "um": "chat2api",
}

_CHAT_PATH = "/v1/chat/completions"
_PREVIEW_CHARS = 60

# B4 bounded batch: an explicit model set is deduplicated and capped BEFORE any
# request. 20 matches the console's existing batch budget convention
# (model.context_probe_run <= 20 requests) and keeps one batch far away from a
# "128-way real speedtest" shape. The JSON-array string ceiling is the transport
# bound for the same cap: 20 * (128 + 3) + 2 = 2622 chars, rounded up.
MAX_BATCH_MODELS = 20
BATCH_MODELS_MAX_CHARS = 2800

# Speed is a derived fact, never a fabricated one. The two scopes follow the
# console_stats D12-3/D12-4 convention ("整次平均" includes the first-char wait).
SPEED_NOTE = (
    "速度仅在分母可靠时计算：整次平均=输出 Token÷整次耗时（含首字等待）；"
    "生成阶段=输出 Token÷首字后到结束窗口（分子为整次输出，含首字分片）。"
    "请求未成功、上游未报告输出 Token 或耗时不可用时速度缺失，不推算、不补 0。")


def _resolve_base_url(service_key):
    """Resolve one service's base URL from the runtime snapshot.

    The snapshot is the single port authority; a snapshot failure is the
    action's failure, not a fallback to a guessed port.
    """
    from console import adapters as console_adapters  # noqa: WPS433  (lazy: keep import side-effect free)

    try:
        snapshot = console_adapters.service_status()
    except Exception:
        raise OperationError(
            "status_unavailable",
            "无法从 runtime 快照解析 %s 服务地址" % service_key)
    services = snapshot.get("services") if isinstance(snapshot, dict) else None
    if not isinstance(services, list):
        raise OperationError(
            "status_unavailable",
            "无法从 runtime 快照解析 %s 服务地址" % service_key)
    for entry in services:
        if not isinstance(entry, dict) or entry.get("key") != service_key:
            continue
        try:
            port = int(entry.get("port"))
        except (TypeError, ValueError):
            continue
        return "http://127.0.0.1:%d" % port
    raise OperationError(
        "status_unavailable",
        "无法从 runtime 快照解析 %s 服务地址" % service_key)


def _base_for_source(source):
    """Base URL for the UM source from the runtime snapshot."""
    return _resolve_base_url(_SOURCE_SERVICE_KEYS[source])


def _split_base(base_url):
    """(host, port) from a resolved base URL; malformed shape fails closed."""
    try:
        parts = urllib.parse.urlsplit(base_url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        host = port = None
    if not host or not port:
        raise OperationError("status_unavailable", "服务地址形状异常")
    return host, port


def _validate_params(params):
    """``{"source": "um", "model": <1..128 chars>}`` or reject.

    The task layer validates too (defense in depth); this module never
    trusts its caller's shape.
    """
    if not isinstance(params, dict):
        raise OperationError("invalid_params", "params must be an object")
    source = params.get("source")
    if source not in SOURCES:
        raise OperationError(
            "invalid_params",
            "source must be one of %s" % sorted(SOURCES))
    model = params.get("model")
    if not isinstance(model, str) or not 1 <= len(model) <= 128:
        raise OperationError(
            "invalid_params", "model must be a string of 1-128 chars")
    return source, model


def _single_line(text, limit=200):
    return " ".join(str(text).split())[:limit]


def _budget_s():
    """Total streaming budget in seconds (injected via ``_TOTAL_BUDGET_S``).

    Read at call time so tests can patch the module attribute; a broken
    override degrades to the documented default instead of an unbounded or
    zero-budget probe.
    """
    try:
        value = float(_TOTAL_BUDGET_S)
    except (TypeError, ValueError):
        return DEFAULT_TOTAL_BUDGET_S
    return value if value > 0 else DEFAULT_TOTAL_BUDGET_S


def _elapsed_since(t0):
    """Monotonic delta since the send anchor; 0.0 when the request was
    never actually written (failure during connect — no streaming window
    ever opened, so an honest zero, not a fabricated duration)."""
    return (time.monotonic() - t0) if t0 is not None else 0.0


def _output_tokens(usage):
    """Trusted output-token count from the in-stream usage block.

    Canonical OpenAI ``completion_tokens`` wins; ``output_tokens`` is the
    documented alias (chat2api.py:2723-2751 normalizes both spellings). A
    missing/invalid value is ``None`` — never a fabricated 0; a reported 0 is a
    real measured 0.
    """
    if not isinstance(usage, dict):
        return None
    for key in ("completion_tokens", "output_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _speed_facts(status, usage, ttfb_s, total_s):
    """Honest speed facts; a missing denominator/numerator stays ``None``.

    Two scopes, two denominators:
    * whole average — output tokens / ``total_s`` (includes first-char wait);
    * generation — output tokens / (``total_s`` - ``ttfb_s``), the measured
      window from the first content to the stream end. The numerator is the
      upstream's whole output count (it includes the first content chunk), so this
      is a labeled generation-window convention, not per-token timing.
    Speed is computed ONLY for a successful probe with a reported output-token
    count and a positive denominator; a failed/timeout probe keeps its honest
    status and gets no speed.
    """
    output_tokens = _output_tokens(usage)
    whole = None
    generation = None
    generation_window = None
    if status == "ok" and output_tokens is not None \
            and isinstance(total_s, (int, float)) \
            and not isinstance(total_s, bool) and total_s > 0:
        whole = round(output_tokens / total_s, 3)
        if isinstance(ttfb_s, (int, float)) \
                and not isinstance(ttfb_s, bool) and total_s > ttfb_s:
            # Unrounded window for the division; the stored window keeps the
            # same measured value (a tiny window is still a real denominator).
            generation_window = total_s - ttfb_s
            generation = round(output_tokens / generation_window, 3)
    if status != "ok":
        speed_status = "not_ok"
    elif output_tokens is None:
        speed_status = "missing_usage"
    elif whole is None:
        speed_status = "missing_duration"
    else:
        speed_status = "measured"
    return {
        "output_tokens": output_tokens,
        "output_tokens_status": ("reported" if output_tokens is not None
                                 else "missing"),
        "speed_status": speed_status,
        "whole_avg_tokens_per_s": whole,
        "generation_tokens_per_s": generation,
        "generation_window_s": generation_window,
        "speed_note": SPEED_NOTE,
    }


def _result(source, model, status, ttfb_s, total_s, error,
            preview=None, usage=None):
    """Honest result dict; measured facts are never overwritten."""
    result = {
        "source": source,
        "model": model,
        "status": status,
        "ttfb_s": ttfb_s,
        "total_s": total_s,
        "first_content_preview": preview,
        "error": error,
        "usage": usage,
        "billing_note": BILLING_NOTE,
    }
    result.update(_speed_facts(status, usage, ttfb_s, total_s))
    return result


def _max_tokens_param(params):
    """The billing-bound output cap (default 64, hard ceiling 512)."""
    max_tokens = params.get("max_tokens", _DEFAULT_MAX_TOKENS)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= _MAX_TOKENS_CAP:
        raise OperationError(
            "invalid_params",
            "max_tokens must be an int in 1..%d" % _MAX_TOKENS_CAP)
    return max_tokens


def _resolve_target(source):
    """Headers + (host, port) for the UM source; no credential gate needed.

    The UM chat route is auth-free, so there is no credential to read and no
    local refusal path. The batch calls this ONCE per batch so every model in
    one batch targets the same resolved service; a single-model call resolves
    once per call.
    """
    headers = {"Content-Type": "application/json"}
    base_url = _base_for_source(source)
    host, port = _split_base(base_url)
    return headers, host, port


def speedtest_model(target, params, ctx=None, _resolved=None):
    """Run ONE single-shot streaming probe against one model source.

    Returns the honest result dict: ``status`` is ``ok`` / ``failed`` /
    ``timeout``; ``ttfb_s`` is the monotonic delta from the written request
    body to the first non-empty ``delta.content`` (``None`` when never
    reached); ``total_s`` covers the whole streaming window;
    ``first_content_preview`` is the first 60 chars or ``None``; ``usage``
    passes the in-stream usage block through when the source sent one.
    Cancel raises
    ``OperationCancelled`` (mapped by the task layer); a measured first char
    is never discarded by cancellation or timeout. NO RETRY on any failure
    (see module docstring: um 401 may trigger a server-side refresh with a
    real browser — side effect belongs to the upstream; single-shot only).

    ``_resolved`` is the batch seam: a pre-resolved (headers, host, port)
    tuple so one batch performs ONE credential/snapshot resolution and every
    selected model still runs this exact single-model probe.
    """
    if ctx is not None:
        ctx.raise_if_cancelled()
    source, model = _validate_params(params)

    # Credential gate BEFORE anything network-shaped (see _resolve_target).
    if _resolved is None:
        headers, host, port = _resolve_target(source)
    else:
        headers, host, port = _resolved

    # Billing bound: a speedtest is a REAL billed request, so the output
    # length is hard-capped (the console action always sends the default;
    # a caller-provided cap may only shrink it). The cap is part of the
    # authorization-package contract, not a tunable.
    max_tokens = _max_tokens_param(params)
    body = json.dumps({
        "model": model,
        "stream": True,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": "Hi"}],
    }, ensure_ascii=False).encode("utf-8")

    budget = _budget_s()
    conn = http.client.HTTPConnection(host, port, timeout=budget)
    ttfb_s = None     # measured fact; survives timeout/failure honestly
    preview = None    # first _PREVIEW_CHARS of the first content
    usage = None      # last in-stream usage block (passthrough)
    t0 = None         # send anchor; None until the body is actually written
    try:
        # http.client connects lazily inside request(); the monotonic anchor
        # is taken right AFTER the request body has been written to the
        # socket (post-connect), per the timing contract.
        conn.request("POST", _CHAT_PATH, body=body, headers=headers)
        t0 = time.monotonic()
        response = conn.getresponse()
        if response.status != 200:
            # Honest HTTP-status failure; the body is not parsed. NO RETRY:
            # for um, a 401 triggers chat2api's server-side refresh (which
            # can launch a real browser login) — the side effect belongs to
            # the upstream, this module stops after exactly one request.
            note = ""
            if source == "um" and response.status == 401:
                note = ("；按契约不重试：UM 的 401 可能触发 chat2api 服务端"
                        " refresh（可能拉起真实浏览器登录），副作用归属上游")
            return _result(
                source, model, "failed", ttfb_s=None,
                total_s=time.monotonic() - t0,
                error={"code": "http_%d" % response.status,
                       "message": _single_line(
                           "上游 %s:%d 返回 HTTP %d%s"
                           % (host, port, response.status, note))})

        sock = conn.sock
        done = False
        while not done:
            # Cancel is checked between line reads; the finally below closes
            # the connection immediately when OperationCancelled propagates.
            if ctx is not None:
                ctx.raise_if_cancelled()
            elapsed = time.monotonic() - t0
            if elapsed >= budget:
                return _result(
                    source, model, "timeout", ttfb_s=ttfb_s,
                    total_s=time.monotonic() - t0, error=None,
                    preview=preview, usage=usage)
            if sock is not None:
                sock.settimeout(max(budget - elapsed, 0.05))
            raw = response.readline()
            if not raw:
                # EOF before [DONE]/finish_reason: honest connection-level
                # failure; measured TTFB/preview survive.
                return _result(
                    source, model, "failed", ttfb_s=ttfb_s,
                    total_s=time.monotonic() - t0,
                    error={"code": "connection_closed",
                           "message": _single_line(
                               "服务端在流结束前关闭连接（未收到 [DONE]）：%s:%d"
                               % (host, port))},
                    preview=preview, usage=usage)
            line = raw.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":"):
                continue  # blank / SSE comment (": connected", ": keepalive")
            if not line.startswith("data:"):
                continue  # tolerate non-SSE noise lines
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue  # non-JSON chunk: skip, never fail the probe
            if not isinstance(chunk, dict):
                continue
            error_block = chunk.get("error")
            if error_block is not None:
                # In-band stream error (UM shape: error + [DONE]).
                message = error_block.get("message") if isinstance(
                    error_block, dict) else None
                return _result(
                    source, model, "failed", ttfb_s=ttfb_s,
                    total_s=time.monotonic() - t0,
                    error={"code": "in_stream_error",
                           "message": _single_line(
                               message or "流中返回未说明错误（带内 error）")},
                    preview=preview, usage=usage)
            usage_block = chunk.get("usage")
            if isinstance(usage_block, dict):
                usage = usage_block
            choices = chunk.get("choices")
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    # UM first-char rule: role blocks carry no content
                    # (skipped); reasoning_content increments never count;
                    # the first non-empty content wins.
                    content = delta.get("content")
                    if (ttfb_s is None and isinstance(content, str)
                            and content):
                        ttfb_s = time.monotonic() - t0
                        preview = content[:_PREVIEW_CHARS]
                if choice.get("finish_reason"):
                    done = True
        return _result(
            source, model, "ok", ttfb_s=ttfb_s,
            total_s=time.monotonic() - t0, error=None,
            preview=preview, usage=usage)
    except ConnectionRefusedError:
        return _result(
            source, model, "failed", ttfb_s=None,
            total_s=_elapsed_since(t0),
            error={"code": "unreachable",
                   "message": _single_line(
                       "连接被拒绝：%s:%d" % (host, port))})
    except (TimeoutError, socket.timeout):
        # Connect / read-status / mid-stream read timeout; already-measured
        # TTFB survives honestly (成败均如实).
        return _result(
            source, model, "timeout", ttfb_s=ttfb_s,
            total_s=_elapsed_since(t0), error=None,
            preview=preview, usage=usage)
    except (http.client.RemoteDisconnected, ConnectionError) as exc:
        # Server accepted the request (or cut mid-stream) without any HTTP
        # status — the upstream silent-disconnect shape.
        return _result(
            source, model, "failed", ttfb_s=ttfb_s,
            total_s=_elapsed_since(t0),
            error={"code": "unreachable",
                   "message": _single_line(
                       "服务端接受请求后断开连接（无 HTTP 状态码）：%s:%d，%s"
                       % (host, port, type(exc).__name__))},
            preview=preview, usage=usage)
    except http.client.HTTPException as exc:
        return _result(
            source, model, "failed", ttfb_s=ttfb_s,
            total_s=_elapsed_since(t0),
            error={"code": "connection_error",
                   "message": _single_line(
                       "HTTP 协议层失败：%s %s:%d"
                       % (type(exc).__name__, host, port))},
            preview=preview, usage=usage)
    except OSError as exc:
        return _result(
            source, model, "failed", ttfb_s=ttfb_s,
            total_s=_elapsed_since(t0),
            error={"code": "connection_error",
                   "message": _single_line(
                       "连接失败：%s %s:%d"
                       % (type(exc).__name__, host, port))},
            preview=preview, usage=usage)
    finally:
        # Bounded release on every path: success, failure, cancel, timeout.
        try:
            conn.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# B4 bounded batch (explicit UM model set)
# ---------------------------------------------------------------------------

def _batch_model_set(params):
    """(requested_count, deduplicated ordered model ids) from ``models``.

    Accepts a JSON array string (the operation/HTTP schema vocabulary has no
    list type; same convention as model sync sources/consumers) or a plain list
    for direct in-process calls. Empty, malformed, non-string or oversized
    selections are refused BEFORE any request: an unselected model is never
    probed, and an empty set never widens to a default.
    """
    raw = params.get("models")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise OperationError(
                "invalid_params", "models 不能为空数组（未选模型不测速）")
        try:
            raw = json.loads(text)
        except ValueError:
            raise OperationError(
                "invalid_params", "models 必须是 JSON 数组字符串")
    if not isinstance(raw, list):
        raise OperationError("invalid_params", "models 必须是模型 ID 数组")
    if not raw:
        raise OperationError(
            "invalid_params", "models 不能为空数组（未选模型不测速）")
    requested_count = len(raw)
    ordered = []
    seen = set()
    for item in raw:
        if not isinstance(item, str):
            raise OperationError("invalid_params", "models 的每一项必须是字符串")
        model = item.strip()
        if not 1 <= len(model) <= 128:
            raise OperationError(
                "invalid_params", "模型 ID 必须为 1-128 个字符")
        if model in seen:
            continue  # 去重：同一模型只保留首次出现
        seen.add(model)
        ordered.append(model)
    if len(ordered) > MAX_BATCH_MODELS:
        raise OperationError(
            "invalid_params",
            "批量测速最多 %d 个模型（去重后 %d 个）"
            % (MAX_BATCH_MODELS, len(ordered)))
    return requested_count, ordered


def _failure_result(source, model, code, message):
    """Per-model infrastructure failure: no request, no fabricated speed."""
    return _result(source, model, "failed", ttfb_s=None, total_s=0.0,
                   error={"code": code, "message": _single_line(message)})


def speedtest_models(target, params, ctx=None):
    """Run ONE short streaming probe per selected UM model, serially.

    ``params`` = ``{"source": "um", "models": <JSON array string|list>,
    "max_tokens"?: int}``. The model set is explicit and deduplicated before
    any request; at most ``MAX_BATCH_MODELS`` models run, one short request
    each (``speedtest_model``), and the batch never retries a failure. Every
    selected model gets a real measured result (``ttfb_s`` / ``total_s`` /
    usage / speed facts or missing status); a failed model keeps ``failed`` and
    is never filled as success. The whole batch is ONE operation, so the task
    layer's global ``model:speedtest`` lock still serializes it with every
    other speedtest; cancellation is checked between models and inside each
    stream, and the existing connection close path releases the request.
    """
    if ctx is not None:
        ctx.raise_if_cancelled()
    if not isinstance(params, dict):
        raise OperationError("invalid_params", "params must be an object")
    if params.get("model") is not None:
        raise OperationError(
            "invalid_params",
            "model 与 models 只能二选一（批量入口只读取 models）")
    source = params.get("source")
    if source != "um":
        raise OperationError(
            "invalid_params",
            "批量测速只接受显式 UM 来源（source=um）；其他来源保留单模型调用")
    requested_count, models = _batch_model_set(params)
    max_tokens = _max_tokens_param(params)
    # ONE credential/snapshot resolution for the whole batch: every selected
    # model targets the same resolved UM service, and a resolution failure
    # refuses the batch before any request.
    resolved = _resolve_target(source)

    results = []
    for index, model in enumerate(models):
        if ctx is not None:
            ctx.raise_if_cancelled()
        single = {"source": source, "model": model,
                  "max_tokens": max_tokens}
        try:
            entry = speedtest_model(target, single, ctx, _resolved=resolved)
        except OperationCancelled:
            raise
        except OperationError as exc:
            # A per-model infrastructure failure is recorded honestly and the
            # batch moves on; it is never retried and never widened to ok.
            entry = _failure_result(source, model, exc.code, exc.message)
        results.append(entry)
        if ctx is not None:
            ctx.report_step("批量测速 %d/%d：%s → %s"
                            % (index + 1, len(models), model,
                               entry["status"]))

    counts = {"ok": 0, "failed": 0, "timeout": 0}
    for entry in results:
        counts[entry["status"]] += 1
    if counts["ok"] == len(results):
        status = "ok"
    elif counts["ok"] == 0:
        status = "failed"
    else:
        status = "partial"
    summary = (
        "批量测速结束：%d 个模型（输入 %d 个，去重 %d 个），成功 %d，"
        "失败 %d，超时 %d；每模型至多 1 次短请求，未自动重试"
        % (len(results), requested_count,
           requested_count - len(results), counts["ok"], counts["failed"],
           counts["timeout"]))
    return {
        "source": source,
        "batch": True,
        "status": status,
        "requested_count": requested_count,
        "model_count": len(results),
        "deduplicated_count": requested_count - len(results),
        "max_models": MAX_BATCH_MODELS,
        "max_tokens": max_tokens,
        # One probe attempt per selected model; no request is sent for an
        # unselected model, and a per-model pre-send failure is still one
        # attempt (honestly failed, never retried).
        "attempt_count": len(results),
        "status_counts": counts,
        "results": results,
        "summary": _single_line(summary),
        "billing_note": BILLING_NOTE,
        "speed_note": SPEED_NOTE,
    }


def speedtest_action(target, params, ctx=None):
    """The single ``model.speedtest`` registry entry for both shapes.

    Params carrying ``models`` take the B4 batch path (explicit UM model set,
    deduplicated, <=``MAX_BATCH_MODELS``, serial, one short request per model);
    the legacy single-model params keep their exact contract and result shape.
    The action name and the global ``model:speedtest`` resource are unchanged,
    so a UI loop cannot bypass the mutex by inventing a second action.
    """
    if isinstance(params, dict) and "models" in params:
        return speedtest_models(target, params, ctx)
    return speedtest_model(target, params, ctx)
