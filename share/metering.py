"""分享用量只读查询（C2/C3 线）。

纪律（本模块的全部对外承诺）：

* **只读**：只打开一个显式给定的账本文件读；绝不写、绝不创建、绝不截断。
* **有界**：最多读 ``limit`` 条记录，且 ``coverage.truncated`` 如实说明是否
  还有更旧的记录没读（有界不等于静默丢数据）。
* **脱敏**：绝不返回、记录或打印分享 Key 明文。账本里本来就没有明文
  （只写 key_id），本模块也不做名称解析——``key_id`` 是分享键的稳定身份，
  名称由调用方（usagesrv）自己按管理库 join，不经过本模块。
* **绝不读私人账本**：``usage-chat2api.jsonl`` 是私人 chat2api 账本，
  传给本模块会直接拒绝（``ValueError``），而不是读它。
* **已知/估算/未知分列**：未知不当 0；中断流只计已知下界。
  账本只记录事实（谁、什么时候、哪条协议、上游是否给了用量）；费用/价格
  派生已随 #30 退役，本模块只汇总原始用量（请求数、Token、缓存报告）。

中断流（``status != "ok"``）只计已知下界：它的 token 数是上游已报告的
或本地已看到的量，不外推、不补零。
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone

# 与 usagesrv._read_jsonl 同一上界（只读、有界）。
DEFAULT_LIMIT = 200000
# 私人 chat2api 账本的文件名：绝不读（usagesrv.CHAT2API_LOG 的默认值）。
PRIVATE_LEDGER_BASENAMES = ("usage-chat2api.jsonl",)
# 用量覆盖度的固定取值（写盘端 record_usage 的 usage_source）。
USAGE_SOURCES = ("upstream", "partial", "estimated", "none")
# 缓存报告状态（Q11）：reported = 上游给了可信 cached_tokens（真实 0 也是证据），
# unreported = 上游没有报告；旧记录没有该字段，按 unknown 分列、不推测。
CACHE_REPORT_STATUSES = ("reported", "unreported")
# 只有 "ok" 是一次完整成功；其余（取消/失败/断流/重试）都是已知下界。
COMPLETE_STATUS = "ok"
BEIJING = timezone(timedelta(hours=8))

# 用量/请求页的时间窗（相对本次读取时刻）。"today" 是北京时间自然日
# （今天 00:00 起），不是滚动 24 小时；"all" 不裁剪历史。有界窗口无法
# 证明无时刻记录在窗内，因此把它们单列排除，绝不猜一个时刻。
SHARE_USAGE_WINDOWS = {
    "1h": 3600,
    "today": "calendar-day",
    "24h": 86400,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "all": None,
}
# 逐请求页的筛选值。unattributed 选择账本里没有可归因 key_id 的记录。
UNATTRIBUTED_FILTER = "unattributed"
SHARE_REQUEST_RESULTS = ("success", "failed", "cancelled", "unknown")
# 逐请求页的硬上界：一次最多返回 200 行；更旧的记录由 cursor 继续。
SHARE_REQUEST_PAGE_DEFAULT = 50
SHARE_REQUEST_PAGE_MAX = 200


def _parse_ts(value):
    """ISO 时间 → epoch ms；缺失或不可解析返回 None（不猜时刻）。"""
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() * 1000
    except (TypeError, ValueError, OverflowError):
        return None


def _beijing_day(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=BEIJING).strftime("%Y-%m-%d")


def _count(rec, *names):
    """第一个存在的非负计数；缺失返回 None（与真实 0 区分）。"""
    for name in names:
        value = rec.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, float) and math.isfinite(value) and value >= 0:
            return int(value)
    return None


def _source_of(rec):
    """一条记录的用量覆盖度；无法判定时返回 "none"（未知，不当 0）。

    * 写盘端写的 ``usage_source`` 优先；
    * 否则用旧字段 ``estimated``：``False`` 表示上游给了 usage 块，
      ``True`` 表示本地估算（旧记录因此仍可分列，不会被当成未知）；
    * 两者都没有 → 未知；
    * 声称上游给了用量却没有任何 token 字段 → 未知（缺用量不是真实 0）。
    """
    explicit = rec.get("usage_source")
    if isinstance(explicit, str) and explicit in USAGE_SOURCES:
        source = explicit
    elif isinstance(rec.get("estimated"), bool):
        source = "estimated" if rec["estimated"] else "upstream"
    else:
        return "none"
    if source == "none":
        return source
    if _count(rec, "input_tokens", "output_tokens", "total_tokens") is None:
        return "none"
    return source


def _cache_report_status(rec):
    """一条记录的缓存报告状态（Q11）。

    * 写盘端写的 ``cache_report_status`` 是唯一权威：reported/unreported；
    * 旧记录没有该字段 → "unknown"（不推测历史缓存量，也不当成明确 0）；
    * 未知值同样按 unknown 处理，绝不降级成 reported。
    """
    status = rec.get("cache_report_status")
    if isinstance(status, str) and status in CACHE_REPORT_STATUSES:
        return status
    return "unknown"


def _request_result(rec):
    """一条请求的真实结果（逐请求页的筛选口径）。

    只有显式 ``status == "ok"`` 是一次成功；``client_disconnected`` /
    ``cancelled`` 是客户端取消；其余非空状态（上游失败、超时、断流等）
    都按失败呈现。旧记录没有 status 时保持 ``unknown``——不因为账本里
    缺字段就猜成成功。
    """
    status = rec.get("status")
    if not isinstance(status, str) or not status:
        return "unknown"
    if status == COMPLETE_STATUS:
        return "success"
    if status in ("client_disconnected", "cancelled", "canceled"):
        return "cancelled"
    return "failed"


def _filter_value(name, value, allowed=None, max_length=200):
    """Validate one optional query filter; ``None`` means no filter."""
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ValueError(f"invalid {name} filter")
    if allowed is not None and value not in allowed:
        raise ValueError(f"invalid {name} filter")
    return value


def _window_cutoff(window, now_ms):
    if window is None:
        return None
    if window not in SHARE_USAGE_WINDOWS:
        raise ValueError("invalid window filter")
    seconds = SHARE_USAGE_WINDOWS[window]
    if seconds is None:
        return None
    if seconds == "calendar-day":
        local = datetime.fromtimestamp(now_ms / 1000, tz=BEIJING)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.timestamp() * 1000
    return now_ms - seconds * 1000


def _record_matches(rec, *, at_ms, cutoff, key_id, model, result):
    """One record against the frozen filter set (unknown time excluded)."""
    if cutoff is not None:
        if at_ms is None or at_ms < cutoff:
            return False
    if key_id is not None:
        rec_key = rec.get("key_id")
        if key_id == UNATTRIBUTED_FILTER:
            if isinstance(rec_key, str) and rec_key:
                return False
        elif rec_key != key_id:
            return False
    if model is not None and str(rec.get("model") or "unknown") != model:
        return False
    if result is not None and _request_result(rec) != result:
        return False
    return True


def _filters(window=None, key_id=None, model=None, result=None):
    return {"window": window or "all", "key": key_id,
            "model": model, "result": result}


def _empty_summary():
    return {"requests": 0, "unknownRequests": 0,
            "inputTokens": 0, "outputTokens": 0,
            "coverage": {"records": 0,
                         "unknownTimeRequests": 0,
                         "cacheReportedRequests": 0,
                         "cacheUnreportedRequests": 0,
                         "cacheUnknownRequests": 0},
            "tokenUsage": {
                "completeRequests": 0, "lowerBoundRequests": 0,
                "estimatedRequests": 0, "unknownRequests": 0,
                "inputTokens": 0, "outputTokens": 0,
                "lowerBoundInputTokens": 0, "lowerBoundOutputTokens": 0,
                "estimatedInputTokens": 0, "estimatedOutputTokens": 0,
                "lowerBoundInputKnown": 0, "lowerBoundOutputKnown": 0,
                "estimatedInputKnown": 0, "estimatedOutputKnown": 0,
                "cachedInputTokens": 0,
                "cacheReportedRequests": 0,
                "cacheUnreportedRequests": 0,
                "cacheUnknownRequests": 0}}


def _empty_key(key_id):
    return {**_empty_summary(), "key_id": key_id, "models": {}, "days": {},
            "undated": {"day": None, **_empty_summary()}}


def _trusted_count(value):
    """Evidence counts must be non-negative JSON integers, including real zero."""
    return value if isinstance(value, int) and not isinstance(value, bool) \
        and value >= 0 else None


def _accumulate_token_usage(usage, rec, source):
    # Cache reporting is independent of the input/output coverage: a request
    # whose usage is unknown can still tell us the cache was unreported.
    cache_status = _cache_report_status(rec)
    if cache_status == "reported":
        cached = _trusted_count(
            rec.get("cached_input_tokens", rec.get("cache_tokens")))
        if cached is None:
            usage["cacheUnknownRequests"] += 1
        else:
            usage["cacheReportedRequests"] += 1
            usage["cachedInputTokens"] += cached
    elif cache_status == "unreported":
        usage["cacheUnreportedRequests"] += 1
    else:
        usage["cacheUnknownRequests"] += 1
    input_tok = _trusted_count(rec.get("input_tokens"))
    output_tok = _trusted_count(rec.get("output_tokens"))
    if source == "none" or (input_tok is None and output_tok is None):
        usage["unknownRequests"] += 1
        return
    complete = input_tok is not None and output_tok is not None
    if source == "upstream" and complete \
            and str(rec.get("status") or COMPLETE_STATUS) == COMPLETE_STATUS:
        usage["completeRequests"] += 1
        usage["inputTokens"] += input_tok
        usage["outputTokens"] += output_tok
        return
    prefix = "estimated" if source == "estimated" else "lowerBound"
    usage[prefix + "Requests"] += 1
    for side, value in (("Input", input_tok), ("Output", output_tok)):
        if value is not None:
            usage[prefix + side + "Tokens"] += value
            usage[prefix + side + "Known"] += 1


def _add_summary(entry, rec, source, at_ms, *, include_unknown_request=False):
    # Preserve legacy requests/Token sums; coverage and tokenUsage carry the
    # stricter evidence semantics, including rows formerly skipped by groups.
    if source != "none" or include_unknown_request:
        entry["requests"] += 1
    coverage = entry["coverage"]
    coverage["records"] += 1
    coverage["unknownTimeRequests"] += int(at_ms is None)
    cache_status = _cache_report_status(rec)
    coverage["cacheReportedRequests"] += int(cache_status == "reported")
    coverage["cacheUnreportedRequests"] += int(cache_status == "unreported")
    coverage["cacheUnknownRequests"] += int(cache_status == "unknown")
    _accumulate_token_usage(entry["tokenUsage"], rec, source)
    if source == "none":
        entry["unknownRequests"] += 1
        return
    for field, names in (("inputTokens", ("input_tokens",)),
                         ("outputTokens", ("output_tokens",))):
        value = _count(rec, *names)
        if value is not None:
            entry[field] += value


def _refuse_private_ledger(path):
    name = os.path.basename(os.path.normpath(str(path)))
    if name in PRIVATE_LEDGER_BASENAMES:
        raise ValueError(
            "refusing to read the private chat2api ledger; pass the share "
            "backend ledger explicitly")


def _read_records_numbered(path, limit):
    """读账本尾部至多 limit 条有效记录；返回 ([(line_no, rec)], coverage)。

    ``line_no`` 是账本文件内的 1 起始行号（空行/坏行也占行号），只作为
    append-only 账本的稳定游标，不暴露绝对路径。记录按文件顺序返回。
    """
    _refuse_private_ledger(path)
    records = []
    total, unparsed, line_no = 0, 0, 0
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line_no += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    unparsed += 1
                    continue
                if not isinstance(rec, dict):
                    unparsed += 1
                    continue
                total += 1
                records.append((line_no, rec))
    except FileNotFoundError:
        return [], {"ledgerPresent": False, "ledgerReadable": False,
                    "recordsTotal": 0, "unparsedLines": 0, "truncated": False}
    except (OSError, UnicodeError):
        return [], {"ledgerPresent": True, "ledgerReadable": False,
                    "recordsTotal": 0, "unparsedLines": 0, "truncated": False}
    coverage = {"ledgerPresent": True, "ledgerReadable": True,
                "recordsTotal": total, "unparsedLines": unparsed,
                "truncated": total > limit}
    return records[-limit:] if limit else [], coverage


def _read_records(path, limit):
    """读账本尾部至多 limit 条有效记录；返回 (records, coverage)。"""
    numbered, coverage = _read_records_numbered(path, limit)
    return [rec for _line_no, rec in numbered], coverage


def read_share_usage(ledger_path, *, limit=DEFAULT_LIMIT,
                     window=None, key_id=None, model=None, result=None,
                     now=None):
    """只读汇总一个分享账本；返回按 Key 的请求数与原始用量。

    筛选（逐请求页与总览共用同一口径）：``window`` 取 ``SHARE_USAGE_WINDOWS``
    的键（默认 all），``key_id`` 为稳定 Key 身份或 ``unattributed``，``model``
    为精确模型名，``result`` 取 ``SHARE_REQUEST_RESULTS``。有界窗口无法证明
    无时刻记录在窗内，因此这类记录**单列排除**（``coverage.excludedUnknownTimeRequests``），
    绝不猜一个时刻。``filters`` 原样回显，供页面确认本次读数口径。
    ``now`` 仅供测试注入（epoch 秒）；缺省取本次读取时刻。

    返回 ``{requests, keys, unattributed, checkedAt, filters, results,
    coverage}``：``keys`` 只含归因成功的 Key（按请求数降序、key_id 升序），
    ``unattributed`` 是同一形状的未归属桶（``key_id`` 为 ``None``）。计数覆盖
    筛选后的记录，``results`` 按真实结果分列 success/failed/cancelled/unknown
    （``unknown`` 是未记录/无法判定的结果，绝不并入 ``success``）。费用/价格
    派生已随 #30 退役：本模块只汇总原始用量（请求数、Token、缓存报告），
    不输出任何 USD/CNY 金额、价格版本或未定价列表。删除或轮换 Key 后历史
    仍可审阅：本模块不查管理库，账本里出现过的 key_id 一律返回。

    Group ``requests`` and Token sums keep their earlier semantics.
    ``models`` and ``days`` retain unknown-usage rows, counted by
    ``coverage.records`` and ``unknownRequests`` rather than fabricating a
    request. ``undated`` holds records whose date is unknowable, or is null.

    ``tokenUsage.totalTokens`` is the complete reported input+output subset;
    estimated and interrupted/partial usage is separate, with absent sides
    null and real zero retained.

    绝不返回或记录分享 Key 明文，也绝不读私人账本（见
    ``_refuse_private_ledger``）；``coverage.ledger`` 只给文件名，不给绝对路径。
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    window = _filter_value("window", window or "all",
                           SHARE_USAGE_WINDOWS)
    key_filter = _filter_value("key", key_id, max_length=128)
    model_filter = _filter_value("model", model, max_length=200)
    result_filter = _filter_value("result", result, SHARE_REQUEST_RESULTS)
    now_ms = (time.time() if now is None else float(now)) * 1000
    cutoff = _window_cutoff(window, now_ms)
    records, read_coverage = _read_records(ledger_path, limit)
    scanned_records = len(records)
    filtered = []
    excluded_unknown_time = 0
    for rec in records:
        at_ms = _parse_ts(rec.get("time"))
        if not _record_matches(rec, at_ms=at_ms, cutoff=cutoff,
                               key_id=key_filter, model=model_filter,
                               result=result_filter):
            if cutoff is not None and at_ms is None:
                excluded_unknown_time += 1
            continue
        filtered.append(rec)
    records = filtered
    buckets = {}
    counters = {
        "records": len(records),
        "recordsScanned": scanned_records,
        "attributed": 0,
        "unattributed": 0,
        "incompleteRequests": 0,
        "unknownUsageRequests": 0,
        "unknownTimeRequests": 0,
        "attributionReasons": {},
        "cacheReportedRequests": 0,
        "cacheUnreportedRequests": 0,
        "cacheUnknownRequests": 0,
        "excludedUnknownTimeRequests": excluded_unknown_time,
        "results": {"success": 0, "failed": 0, "cancelled": 0,
                    "unknown": 0},
    }
    for rec in records:
        key_id = rec.get("key_id")
        if not isinstance(key_id, str) or not key_id:
            key_id = None
            counters["unattributed"] += 1
        else:
            counters["attributed"] += 1
        bucket = buckets.setdefault(key_id, _empty_key(key_id))
        if key_id is None:
            reason = rec.get("attribution")
            if not (isinstance(reason, str) and reason):
                # 写盘端明确记了「没有归因标记」；本字段之前写的旧记录没有
                # 可回填的标记，单列为 legacy。
                reason = "absent" if "attribution" in rec else "legacy"
            counters["attributionReasons"][reason] =                 counters["attributionReasons"].get(reason, 0) + 1
        if str(rec.get("status") or COMPLETE_STATUS) != COMPLETE_STATUS:
            counters["incompleteRequests"] += 1
        counters["results"][_request_result(rec)] += 1
        model = str(rec.get("model") or "unknown")
        at_ms = _parse_ts(rec.get("time"))
        if at_ms is None:
            counters["unknownTimeRequests"] += 1
        source = _source_of(rec)
        cache_status = _cache_report_status(rec)
        if cache_status == "reported":
            counters["cacheReportedRequests"] += 1
        elif cache_status == "unreported":
            counters["cacheUnreportedRequests"] += 1
        else:
            counters["cacheUnknownRequests"] += 1
        if source == "none":
            counters["unknownUsageRequests"] += 1
        model_entry = bucket["models"].setdefault(
            model, {"model": model, **_empty_summary()})
        day = _beijing_day(at_ms)
        if day:
            day_entry = bucket["days"].setdefault(
                day, {"day": day, **_empty_summary()})
        else:
            day_entry = bucket["undated"]
        _add_summary(bucket, rec, source, at_ms, include_unknown_request=True)
        for entry in (model_entry, day_entry):
            _add_summary(entry, rec, source, at_ms)
    keys = sorted(buckets.values(), key=lambda item: (-item["requests"],
                                                      item["key_id"] or ""))
    unattributed = buckets.get(None)
    return {
        "requests": len(records),
        "keys": [_finish_key(item) for item in keys
                 if item["key_id"] is not None],
        "unattributed": (_finish_key(unattributed)
                         if unattributed is not None
                         else _finish_key(_empty_key(None))),
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "filters": _filters(window, key_filter, model_filter, result_filter),
        "results": _finish_results(counters["results"]),
        "coverage": _finish_coverage(counters, read_coverage, limit,
                                     ledger_path, len(filtered)),
    }


def _request_row(rec):
    """One ledger record as a request-page row (unknown stays null, never 0)."""
    at_ms = _parse_ts(rec.get("time"))
    model = rec.get("model")
    model = model if isinstance(model, str) and model else None
    source = _source_of(rec)
    cache_status = _cache_report_status(rec)
    request_id = rec.get("request_id")
    key_id = rec.get("key_id")
    attribution = rec.get("attribution")
    status = rec.get("status")
    if source == "none":
        # Unknown usage has no trusted token evidence: legacy numeric fields
        # are NOT re-used and nothing is fabricated as 0.
        input_tokens = output_tokens = cache_tokens = reasoning_tokens = None
    else:
        input_tokens = _count(rec, "input_tokens")
        output_tokens = _count(rec, "output_tokens")
        cache_tokens = (_count(rec, "cached_input_tokens", "cache_tokens")
                         if cache_status == "reported" else None)
        reasoning_tokens = _count(rec, "reasoning_tokens")
    return {
        "requestId": request_id if isinstance(request_id, str) and request_id
        else None,
        "time": rec.get("time") if isinstance(rec.get("time"), str) else None,
        "day": _beijing_day(at_ms),
        "keyId": key_id if isinstance(key_id, str) and key_id else None,
        "model": model,
        "status": status if isinstance(status, str) and status else None,
        "result": _request_result(rec),
        "attribution": (attribution
                        if isinstance(attribution, str) and attribution
                        else None),
        "usageSource": source,
        "cacheReportStatus": cache_status,
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        # The ledger's own measured duration; an old row without it stays null.
        "durationMs": _count(rec, "duration_ms"),
        # A reported cache count may be a real zero; unreported/unknown is null.
        "cacheTokens": cache_tokens,
        "reasoningTokens": reasoning_tokens,
    }


def read_share_requests(ledger_path, *, window=None,
                         key_id=None, model=None, result=None, cursor=None,
                         limit=SHARE_REQUEST_PAGE_DEFAULT, now=None):
    """Read a bounded, newest-first page of individual share requests.

    The page is the per-request companion to ``read_share_usage``: same ledger,
    same filters. ``cursor`` is the opaque line number of the last row already
    returned; the next page only contains OLDER records, so an append-only ledger
    never shifts a page under the reader. ``limit`` is 1..200.
    ``coverage.truncated`` keeps the read-window fact (older records were not
    scanned); ``coverage.olderRecordsUnread`` repeats it for the page so a client
    does not mistake the end of the window for the end of the ledger.
    ``nextCursor`` is null when no older matched row exists in the read window.

    Unknown/absent values stay null: a row with no time has ``time``/``day`` null,
    a row with unknown usage has null token fields (never 0), and an unreported
    cache count is null (never a measured zero). ``filters`` echoes the exact
    filter set. Cost/price fields were retired with #30.
    """
    if isinstance(limit, bool) or not isinstance(limit, int)             or not 1 <= limit <= SHARE_REQUEST_PAGE_MAX:
        raise ValueError(
            f"limit must be an integer in 1..{SHARE_REQUEST_PAGE_MAX}")
    window = _filter_value("window", window or "all", SHARE_USAGE_WINDOWS)
    key_id = _filter_value("key", key_id, max_length=128)
    model = _filter_value("model", model, max_length=200)
    result = _filter_value("result", result, SHARE_REQUEST_RESULTS)
    if cursor is not None:
        if not isinstance(cursor, str) or not cursor.isdigit():
            raise ValueError("invalid cursor")
        cursor_line = int(cursor)
        if cursor_line < 0:
            raise ValueError("invalid cursor")
    else:
        cursor_line = None
    now_ms = (time.time() if now is None else float(now)) * 1000
    cutoff = _window_cutoff(window, now_ms)
    numbered, read_coverage = _read_records_numbered(ledger_path, DEFAULT_LIMIT)
    matched = []
    excluded_unknown_time = 0
    for line_no, rec in reversed(numbered):
        at_ms = _parse_ts(rec.get("time"))
        if not _record_matches(rec, at_ms=at_ms, cutoff=cutoff,
                               key_id=key_id, model=model, result=result):
            if cutoff is not None and at_ms is None:
                excluded_unknown_time += 1
            continue
        if cursor_line is not None and line_no >= cursor_line:
            continue
        matched.append((line_no, rec))
    page = matched[:limit]
    has_more = len(matched) > limit
    next_cursor = str(page[-1][0]) if has_more and page else None
    coverage = {
        **_read_coverage_fields(read_coverage),
        "ledger": os.path.basename(os.path.normpath(str(ledger_path))),
        "readLimit": DEFAULT_LIMIT,
        "limit": limit,
        "recordsScanned": len(numbered),
        "recordsMatched": len(matched),
        "recordsReturned": len(page),
        "olderRecordsUnread": read_coverage["truncated"],
        "excludedUnknownTimeRequests": excluded_unknown_time,
        "filters": _filters(window, key_id, model, result),
    }
    for key, value in _page_counters(page).items():
        coverage[key] = value
    return {
        "rows": [_request_row(rec) for _line_no, rec in page],
        "nextCursor": next_cursor,
        "truncated": has_more or read_coverage["truncated"],
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "filters": _filters(window, key_id, model, result),
        "coverage": coverage,
    }


def _read_coverage_fields(read_coverage):
    return {key: read_coverage[key] for key in (
        "ledgerPresent", "ledgerReadable", "recordsTotal", "unparsedLines",
        "truncated")}


def _page_counters(page):
    """Counters for one page: unknown/unreported never fold to 0."""
    counters = {
        "attributedRequests": 0,
        "unattributedRequests": 0,
        "unknownUsageRequests": 0,
        "cacheReportedRequests": 0,
        "cacheUnreportedRequests": 0,
        "cacheUnknownRequests": 0,
        "attributionReasons": {},
    }
    for _line_no, rec in page:
        key_id = rec.get("key_id")
        if isinstance(key_id, str) and key_id:
            counters["attributedRequests"] += 1
        else:
            counters["unattributedRequests"] += 1
            reason = rec.get("attribution")
            if not (isinstance(reason, str) and reason):
                reason = "absent" if "attribution" in rec else "legacy"
            counters["attributionReasons"][reason] =                 counters["attributionReasons"].get(reason, 0) + 1
        source = _source_of(rec)
        cache_status = _cache_report_status(rec)
        if cache_status == "reported":
            counters["cacheReportedRequests"] += 1
        elif cache_status == "unreported":
            counters["cacheUnreportedRequests"] += 1
        else:
            counters["cacheUnknownRequests"] += 1
        if source == "none":
            counters["unknownUsageRequests"] += 1
    counters["attributionReasons"] = {
        key: counters["attributionReasons"][key]
        for key in sorted(counters["attributionReasons"])}
    return counters


def _finish_token_usage(usage, records):
    result = {field: usage[field] for field in (
        "completeRequests", "lowerBoundRequests", "estimatedRequests",
        "unknownRequests")}
    for field in ("inputTokens", "outputTokens"):
        result[field] = usage[field] if usage["completeRequests"] else None
    result["totalTokens"] = (usage["inputTokens"] + usage["outputTokens"]
                             if usage["completeRequests"] else None)
    for prefix in ("lowerBound", "estimated"):
        for side in ("Input", "Output"):
            field = prefix + side + "Tokens"
            result[field] = usage[field] if usage[prefix + side + "Known"] else None
        result[prefix + "Tokens"] = (
            usage[prefix + "InputTokens"] + usage[prefix + "OutputTokens"]
            if usage[prefix + "InputKnown"] or usage[prefix + "OutputKnown"]
            else None)
    result["complete"] = records > 0 and usage["completeRequests"] == records
    # Q11: cache evidence is separate from the input/output coverage. The
    # cached sum only covers REPORTED rows; unreported and legacy rows are
    # counted instead of being folded into a measured zero.
    result["cachedInputTokens"] = (usage["cachedInputTokens"]
                                   if usage["cacheReportedRequests"] else None)
    result["cacheReportedRequests"] = usage["cacheReportedRequests"]
    result["cacheUnreportedRequests"] = usage["cacheUnreportedRequests"]
    result["cacheUnknownRequests"] = usage["cacheUnknownRequests"]
    return result


def _finish_summary(entry):
    result = {field: entry[field] for field in (
        "requests", "inputTokens", "outputTokens", "unknownRequests")}
    coverage = dict(entry["coverage"])
    result["coverage"] = coverage
    result["tokenUsage"] = _finish_token_usage(
        entry["tokenUsage"], coverage["records"])
    for source, target in (("completeRequests", "completeUsageRequests"),
                           ("lowerBoundRequests", "partialUsageRequests"),
                           ("estimatedRequests", "estimatedUsageRequests"),
                           ("unknownRequests", "unknownUsageRequests")):
        coverage[target] = result["tokenUsage"][source]
    return result


def _finish_key(bucket):
    models = sorted(bucket["models"].values(),
                     key=lambda item: (-item["requests"], item["model"]))
    days = [bucket["days"][day] for day in sorted(bucket["days"])]
    return {
        "key_id": bucket["key_id"],
        **_finish_summary(bucket),
        "models": [{"model": entry["model"],
                    **_finish_summary(entry)} for entry in models],
        "days": [{"day": entry["day"],
                  **_finish_summary(entry)} for entry in days],
        "undated": ({"day": None,
                     **_finish_summary(bucket["undated"])}
                    if bucket["undated"]["coverage"]["records"] else None),
    }


def _finish_results(counts):
    """The four real outcomes plus their total and coverage (C8).

    ``unknown`` counts records whose outcome was never recorded; it is NEVER
    merged into ``success``. ``coverage.unknownResult`` repeats that count so a
    chart can say how much of the total it cannot classify.
    """
    return {
        "success": counts["success"],
        "failed": counts["failed"],
        "cancelled": counts["cancelled"],
        "unknown": counts["unknown"],
        "total": sum(counts.values()),
        "coverage": {"unknownResult": counts["unknown"]},
    }


def _finish_coverage(counters, read_coverage, limit, ledger_path,
                      matched_records=None):
    result = dict(read_coverage)
    result.update({
        "ledger": os.path.basename(os.path.normpath(str(ledger_path))),
        "limit": limit,
        "recordsScanned": counters.get("recordsScanned",
                                         counters["records"]),
        "recordsMatched": (counters["records"] if matched_records is None
                           else matched_records),
        "records": counters["records"],
        "attributed": counters["attributed"],
        "unattributed": counters["unattributed"],
        "incompleteRequests": counters["incompleteRequests"],
        "unknownUsageRequests": counters["unknownUsageRequests"],
        "unknownTimeRequests": counters["unknownTimeRequests"],
        "excludedUnknownTimeRequests":
            counters["excludedUnknownTimeRequests"],
        "attributionReasons": {
            key: counters["attributionReasons"][key]
            for key in sorted(counters["attributionReasons"])},
        "cacheReportedRequests": counters["cacheReportedRequests"],
        "cacheUnreportedRequests": counters["cacheUnreportedRequests"],
        "cacheUnknownRequests": counters["cacheUnknownRequests"],
    })
    return result
