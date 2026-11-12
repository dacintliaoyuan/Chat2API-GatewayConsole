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
  费用是**读取时派生**（``price_fn``），不是账本里的既成事实：账本只记录
  事实（谁、什么时候、哪条协议、上游是否给了用量、写入时的价目表身份）。

费用口径与 ``usagesrv`` 保持一致：按记录**自己的时刻**取价（价格版本切换
前后互不影响），缓存命中视为 input 的子集（价目引擎自己减），按北京时间日
分组。中断流（``status != "ok"``）只计已知下界：它的 token 数是上游已报告的
或本地已看到的量，不外推、不补零。

人民币折算同样逐条进行：每条记录用**它自己所属版本**的汇率换算后累加，绝不
先把 USD 小计聚合再乘一个现行汇率（跨版本的历史金额因此不被重算）。汇率
缺失/不可用、无价或用量未知时总额记未知/下界并标 partial，绝不折成 0；
用量未知只让总额成为下界，不把已知/估算小计标成未知；只有整节点没有任何
可换算记录时才返回 ``None``。
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


def _valid_rate(rate):
    """汇率可用于折算：有限正数；其余（缺失/布尔/NaN/inf/非正）一律不可用。"""
    return isinstance(rate, (int, float)) and not isinstance(rate, bool) \
        and math.isfinite(rate) and rate > 0


class _CnySum:
    """一个人民币小计，逐条按该记录自己的汇率折算后累加。

    与 ``usagesrv._Money`` 同一语义（本模块不 import usagesrv，只依赖标准库）：
    跨版本的分组各自按自己的汇率换算，历史金额不按现价重算。``partial`` 表示本节点有记录未能折算（无价/用量未知/无汇率），
    此时 ``result()`` 是下界；只有**没有任何可换算记录**时才返回 ``None``
    （未知，绝不折 0）。空节点是真实 0.0。
    """

    __slots__ = ("total", "converted", "partial")

    def __init__(self):
        self.total = 0.0
        self.converted = 0
        self.partial = False

    def add(self, usd, rate):
        self.total += usd * rate
        self.converted += 1

    def result(self):
        if not self.converted and self.partial:
            return None
        return round(self.total, 4)


class _CnyTotals:
    """一个分组节点的总额 / 已知 / 估算人民币累加器（与 USD 侧同分列）。"""

    __slots__ = ("total", "known", "estimated")

    def __init__(self):
        self.total = _CnySum()
        self.known = _CnySum()
        self.estimated = _CnySum()

    def add(self, usd, rate, source):
        self.total.add(usd, rate)
        (self.known if source == "upstream" else self.estimated).add(usd, rate)

    def mark_unconvertible(self, source):
        self.total.partial = True
        (self.known if source == "upstream" else self.estimated).partial = True


def _empty_summary():
    return {"requests": 0, "costUsd": 0.0, "knownCostUsd": 0.0,
            "estimatedCostUsd": 0.0, "unknownRequests": 0,
            "unpricedRequests": 0, "inputTokens": 0, "outputTokens": 0,
            "coverage": {"records": 0, "pricedRequests": 0,
                         "unknownTimeRequests": 0,
                         "pricingUnavailableRequests": 0,
                         "cacheReportedRequests": 0,
                         "cacheUnreportedRequests": 0,
                         "cacheUnknownRequests": 0,
                         "cacheUnknownPricedRequests": 0,
                         "costCompleteRecords": 0},
            "priceVersions": set(),
            "cny": _CnyTotals(),
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


def _accumulate_cny(entry, source, cost_usd, priced, priced_by_fn, rate):
    """把一条记录并入节点的人民币累加器（按它自己的汇率）。

    只有 ``priced`` 且汇率是可用的有限正数才折算；其余（无价/无汇率）把对应的
    累加器标为 partial，绝不加 0 冒充实测金额。

    用量未知（``source == "none"``）的记录没有已知/估算归属：它只让该节点的
    总额成为下界，不能把已知或估算小计标成 partial——那会把真实 0.0 的估算
    小计变成 null，页面因此显示并不存在的“估算未知”。
    """
    if not priced_by_fn:
        return
    cny = entry["cny"]
    if source == "none":
        cny.total.partial = True
        return
    if priced and _valid_rate(rate):
        cny.add(cost_usd, rate, source)
    else:
        cny.mark_unconvertible(source)


def _add_summary(entry, rec, source, at_ms, cost_usd, priced, version_id,
                 priced_by_fn, *, rate=None, include_unknown_request=False,
                 cache_unknown=False):
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
    if cache_unknown:
        coverage["cacheUnknownPricedRequests"] += 1
    _accumulate_token_usage(entry["tokenUsage"], rec, source)
    # CNY is derived from the same record and its own version's exchange rate;
    # unknown usage is folded in as unconvertible (never a fabricated 0).
    _accumulate_cny(entry, source, cost_usd, priced, priced_by_fn, rate)
    if cache_unknown:
        # The price is an upper bound (all input charged at cache-miss); it is
        # not the record's exact cost, so the CNY subtotals stay partial.
        entry["cny"].total.partial = True
        entry["cny"].known.partial = True
        entry["cny"].estimated.partial = True
    if source == "none":
        entry["unknownRequests"] += 1
        return
    if not priced_by_fn:
        coverage["pricingUnavailableRequests"] += 1
    elif not priced:
        entry["unpricedRequests"] += 1
    if version_id is not None:
        entry["priceVersions"].add(str(version_id))
    for field, names in (("inputTokens", ("input_tokens",)),
                         ("outputTokens", ("output_tokens",))):
        value = _count(rec, *names)
        if value is not None:
            entry[field] += value
    if priced:
        coverage["pricedRequests"] += 1
        entry["costUsd"] += cost_usd
        field = "knownCostUsd" if source == "upstream" else "estimatedCostUsd"
        entry[field] += cost_usd
        if at_ms is not None and source in ("upstream", "estimated") \
                and str(rec.get("status") or COMPLETE_STATUS) == COMPLETE_STATUS \
                and _trusted_count(rec.get("input_tokens")) is not None \
                and _trusted_count(rec.get("output_tokens")) is not None \
                and not cache_unknown:
            coverage["costCompleteRecords"] += 1


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


def read_share_usage(ledger_path, price_fn=None, *, limit=DEFAULT_LIMIT,
                     window=None, key_id=None, model=None, result=None,
                     now=None):
    """只读汇总一个分享账本；返回按 Key 的请求数与已知/估算/未知费用。

    ``price_fn(model_id, at_ms, input_tok, output_tok, cache_tok,
    reasoning_tok) -> (cost_usd, priced, matched, peak, version_id[, rate])``
    是**唯一**计价入口，由调用方（usagesrv）提供；五元组与六元组都接受，六元组
    的第六项是该记录所属版本的汇率。``price_fn=None`` 时全部费用字段为 ``None``
    且**不输出任何人民币字段**，只给计数。``priced`` 为假表示该模型无价（缺价格
    保留未知，不当 0），``matched`` 是命中的模型名（按它归组，与 usagesrv 一致），
    ``version_id`` 是价格版本标识（``coverage.priceVersions`` 汇总）。

    筛选（逐请求页与总览共用同一口径）：``window`` 取 ``SHARE_USAGE_WINDOWS``
    的键（默认 all），``key_id`` 为稳定 Key 身份或 ``unattributed``，``model``
    为精确模型名，``result`` 取 ``SHARE_REQUEST_RESULTS``。有界窗口无法证明
    无时刻记录在窗内，因此这类记录**单列排除**（``coverage.excludedUnknownTimeRequests``），
    绝不猜一个时刻。``filters`` 原样回显，供页面确认本次读数口径。
    ``now`` 仅供测试注入（epoch 秒）；缺省取本次读取时刻。

    人民币字段（``costCny``/``knownCostCny``/``estimatedCostCny`` 与
    ``costCnyPartial``/``knownCostCnyPartial``/``estimatedCostCnyPartial``）只在
    提供 ``price_fn`` 时出现：每条记录按它自己的汇率折算（六元组返回的 ``rate``；
    五元组没有汇率，金额因此未知并标 partial）。``known`` 对应
    ``source == "upstream"``，其余可计价来源进 ``estimated``。三个 partial 标记
    各自只描述自己的小计：用量未知（``source == "none"``）只让总额成为下界，
    已知/估算仍是真实 0.0；无价或缺汇率才把对应小计标成 partial。
    没有任何可换算记录时金额为 ``None``（未知，绝不折 0）；有可换算记录但仍
    有不可换算部分时金额是下界且对应的 partial 为 true；空节点是真实 0.0。
    ``coverage.rateFn`` 表示本次读取确实观察到六元组计价器（至少一条记录返回
    了汇率字段；空账本没有可观察的返回，因此为 false）。

    返回 ``{requests, keys, unattributed, checkedAt, filters, results,
    coverage}``：``keys`` 只含归因成功的 Key（按请求数降序、key_id 升序），
    ``unattributed`` 是同一形状的未归属桶（``key_id`` 为 ``None``）。计数覆盖
    筛选后的记录，``results`` 按真实结果分列 success/failed/cancelled/unknown
    （``unknown`` 是未记录/无法判定的结果，绝不并入 ``success``），
    费用只覆盖能派生出费用的记录，差额由 ``unknownRequests``（用量未知）与
    ``unpricedRequests``（无价）说明。删除或轮换 Key 后历史仍可审阅：本模块不查
    管理库，账本里出现过的 key_id 一律返回。

    Group ``requests``/``costUsd`` and legacy Token sums keep their earlier
    semantics. ``models`` and ``days`` now retain unknown-usage rows, counted
    by ``coverage.records`` and ``unknownRequests`` rather than fabricating a
    priced request. ``undated`` holds records whose date is unknowable, or is
    null. Local coverage and priceVersions belong to that exact group.

    ``tokenUsage.totalTokens`` is the complete reported input+output subset;
    estimated and interrupted/partial usage is separate, with absent sides
    null and real zero retained. ``coverage.costComplete`` means every record
    in the group has both cost components, a valid date and a usable price;
    estimates still remain estimates. Otherwise numeric cost fields are only
    the covered components, never the group's complete cost.

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
    priced_by_fn = price_fn is not None
    buckets = {}
    counters = {
        "records": len(records),
        "recordsScanned": scanned_records,
        "attributed": 0,
        "unattributed": 0,
        "incompleteRequests": 0,
        "unknownUsageRequests": 0,
        "unknownTimeRequests": 0,
        "unpricedRequests": 0,
        "peakRequests": 0,
        "unpricedModels": set(),
        "priceVersions": set(),
        "attributionReasons": {},
        "priceFn": priced_by_fn,
        "costCnyPartial": False,
        "rateFn": False,
        "cacheReportedRequests": 0,
        "cacheUnreportedRequests": 0,
        "cacheUnknownRequests": 0,
        "cacheUnknownPricedRequests": 0,
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
            counters["attributionReasons"][reason] = \
                counters["attributionReasons"].get(reason, 0) + 1
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
        rate_capable = False
        cache_unknown = False
        if source == "none":
            counters["unknownUsageRequests"] += 1
            cost_usd, priced, matched, peak, version_id, rate = \
                (0.0, False, None, False, None, None)
        else:
            (cost_usd, priced, matched, peak, version_id, rate,
             rate_capable, cache_unknown) = _price(price_fn, rec, model, at_ms)
        if cache_unknown:
            counters["cacheUnknownPricedRequests"] += 1
        if rate_capable:
            # Evidence that this pricer returns exchange rates at all; a
            # five-tuple (or a ledger with no priced record) cannot prove it.
            counters["rateFn"] = True
        if priced_by_fn and not (priced and _valid_rate(rate)):
            # This record's CNY amount is unknown/lower-bound; the top-level
            # coverage says so once instead of leaving it to per-key inference.
            counters["costCnyPartial"] = True
        if priced_by_fn and source != "none":
            if not priced:
                counters["unpricedRequests"] += 1
                counters["unpricedModels"].add(matched or model)
            if version_id is not None:
                counters["priceVersions"].add(str(version_id))
        if peak:
            counters["peakRequests"] += 1
        group = str(matched) if priced_by_fn and matched else model
        model_entry = bucket["models"].setdefault(
            group, {"model": group, **_empty_summary()})
        day = _beijing_day(at_ms)
        if day:
            day_entry = bucket["days"].setdefault(
                day, {"day": day, **_empty_summary()})
        else:
            day_entry = bucket["undated"]
        _add_summary(bucket, rec, source, at_ms, cost_usd, priced, version_id,
                     priced_by_fn, rate=rate, include_unknown_request=True,
                     cache_unknown=cache_unknown)
        for entry in (model_entry, day_entry):
            _add_summary(entry, rec, source, at_ms, cost_usd, priced, version_id,
                         priced_by_fn, rate=rate, cache_unknown=cache_unknown)
    keys = sorted(buckets.values(), key=lambda item: (-item["requests"],
                                                      item["key_id"] or ""))
    unattributed = buckets.get(None)
    return {
        "requests": len(records),
        "keys": [_finish_key(item, priced_by_fn) for item in keys
                 if item["key_id"] is not None],
        "unattributed": (_finish_key(unattributed, priced_by_fn)
                         if unattributed is not None
                         else _finish_key(_empty_key(None), priced_by_fn)),
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "filters": _filters(window, key_filter, model_filter, result_filter),
        "results": _finish_results(counters["results"]),
        "coverage": _finish_coverage(counters, read_coverage, limit,
                                     ledger_path, len(filtered)),
    }


def _request_row(rec, price_fn):
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
    cost_usd, priced, _matched, _peak, version_id, rate, _rate_capable, \
        cache_unknown = _price(price_fn, rec, model or "unknown", at_ms)
    if price_fn is None:
        # No pricing chain was supplied: amounts are not evaluated (unknown),
        # which is different from a model that was priced and found free.
        cost_usd, priced, version_id, rate = None, None, None, None
    if source == "none":
        # Unknown usage has no trusted token evidence and no cost to derive:
        # legacy numeric fields are NOT re-used and nothing is fabricated as 0.
        cost_usd, priced, version_id, rate = None, None, None, None
        input_tokens = output_tokens = cache_tokens = reasoning_tokens = None
    else:
        input_tokens = _count(rec, "input_tokens")
        output_tokens = _count(rec, "output_tokens")
        cache_tokens = (_count(rec, "cached_input_tokens", "cache_tokens")
                         if cache_status == "reported" else None)
        reasoning_tokens = _count(rec, "reasoning_tokens")
    row = {
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
        "priced": priced,
        "priceVersion": version_id,
        "costUsd": None,
        "costCny": None,
        "costCnyPartial": False,
    }
    if priced is None:
        # Unknown usage: the amount is unknown, not a lower bound of a known cost.
        row["costCnyPartial"] = True
        return row
    if priced and rate is not None:
        row["costUsd"] = round(float(cost_usd), 6)
        row["costCny"] = round(float(cost_usd) * rate, 4)
        row["costCnyPartial"] = cache_unknown
    elif priced:
        # A price exists but the record's version carried no usable rate: the
        # USD amount is known, the CNY amount is not.
        row["costUsd"] = round(float(cost_usd), 6)
        row["costCnyPartial"] = True
    else:
        # Unpriced model: no amount at all, never 0.
        row["costCnyPartial"] = True
    return row


def read_share_requests(ledger_path, price_fn=None, *, window=None,
                         key_id=None, model=None, result=None, cursor=None,
                         limit=SHARE_REQUEST_PAGE_DEFAULT, now=None):
    """Read a bounded, newest-first page of individual share requests.

    The page is the per-request companion to ``read_share_usage``: same ledger,
    same filters, same per-record pricing chain. ``cursor`` is the opaque line
    number of the last row already returned; the next page only contains OLDER
    records, so an append-only ledger never shifts a page under the reader.
    ``limit`` is 1..200. ``coverage.truncated`` keeps the read-window fact
    (older records were not scanned); ``coverage.olderRecordsUnread`` repeats it
    for the page so a client does not mistake the end of the window for the end
    of the ledger. ``nextCursor`` is null when no older matched row exists in
    the read window.

    Unknown/absent values stay null: a row with no time has ``time``/``day`` null,
    a row with unknown usage has null token/cost fields (never 0), an unreported
    cache count is null (never a measured zero), and an unpriced model has null
    amounts with ``priced`` false. ``filters`` echoes the exact filter set.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) \
            or not 1 <= limit <= SHARE_REQUEST_PAGE_MAX:
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
    for key, value in _page_counters(page, price_fn).items():
        coverage[key] = value
    return {
        "rows": [_request_row(rec, price_fn) for _line_no, rec in page],
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


def _page_counters(page, price_fn):
    """Counters for one page: unknown/unreported/unpriced never fold to 0."""
    counters = {
        "attributedRequests": 0,
        "unattributedRequests": 0,
        "unknownUsageRequests": 0,
        "cacheReportedRequests": 0,
        "cacheUnreportedRequests": 0,
        "cacheUnknownRequests": 0,
        "pricedRequests": 0,
        "unpricedRequests": 0,
        "costCnyPartial": False,
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
            counters["attributionReasons"][reason] = \
                counters["attributionReasons"].get(reason, 0) + 1
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
            counters["costCnyPartial"] = True
            continue
        if price_fn is None:
            counters["costCnyPartial"] = True
            continue
        row = _request_row(rec, price_fn)
        if row["priced"] is True:
            counters["pricedRequests"] += 1
            if row["costCny"] is None:
                counters["costCnyPartial"] = True
        else:
            counters["unpricedRequests"] += 1
            counters["costCnyPartial"] = True
    counters["attributionReasons"] = {
        key: counters["attributionReasons"][key]
        for key in sorted(counters["attributionReasons"])}
    return counters


def _price(price_fn, rec, model, at_ms):
    """调用唯一的计价入口；未提供价格函数时全部费用未知。

    计价函数声明的 ``priced`` 是它的判断；返回的金额不可用时按无价处理
    （进 ``unpricedRequests``，不当 0），不静默丢弃也不猜一个金额。

    同时接受五元组、六元组与七元组：六元组的第六项是该记录所属版本的汇率，五元组
    没有汇率（人民币金额因此未知）。七元组在六元组之上多一个 ``cache_unknown``
    布尔（缓存未报告，金额是缓存未命中上界，不是精确值）。返回
    ``(cost_usd, priced, matched, peak, version_id, rate, rate_capable,
    cache_unknown)``；``rate_capable`` 记录本次调用是否真的返回了六/七元组
    （``rate`` 本身仍要通过有限正数校验，否则为 ``None``）。
    """
    if price_fn is None:
        return 0.0, False, None, False, None, None, False, False
    cache_tok = _count(rec, "cached_input_tokens", "cache_tokens")
    cache_unknown = False
    if _cache_report_status(rec) == "unreported":
        # Do not turn an unreported cache count into a measured zero. Passing
        # None lets the pricer charge the whole input at cache-miss (an upper
        # bound) and mark the amount as not exact.
        cache_tok = None
        cache_unknown = True
    result = price_fn(
        model, at_ms, _count(rec, "input_tokens") or 0,
        _count(rec, "output_tokens") or 0,
        cache_tok,
        _count(rec, "reasoning_tokens") or 0)
    rate = None
    rate_capable = False
    if isinstance(result, (tuple, list)) and len(result) in (6, 7):
        cost_usd, priced, matched, peak, version_id, rate = result[:6]
        rate_capable = True
        if len(result) == 7:
            cache_unknown = cache_unknown or bool(result[6])
    else:
        cost_usd, priced, matched, peak, version_id = result
    if not isinstance(cost_usd, (int, float)) or isinstance(cost_usd, bool) \
            or not math.isfinite(cost_usd) or cost_usd < 0:
        cost_usd = 0.0
        priced = False
    return (float(cost_usd), bool(priced), matched, bool(peak), version_id,
            rate if _valid_rate(rate) else None, rate_capable, cache_unknown)


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


def _finish_summary(entry, priced_by_fn):
    result = {field: entry[field] for field in (
        "requests", "inputTokens", "outputTokens", "unknownRequests",
        "unpricedRequests")}
    for field in ("costUsd", "knownCostUsd", "estimatedCostUsd"):
        result[field] = round(entry[field], 6) if priced_by_fn else None
    if priced_by_fn:
        cny = entry["cny"]
        result["costCny"] = cny.total.result()
        result["knownCostCny"] = cny.known.result()
        result["estimatedCostCny"] = cny.estimated.result()
        result["costCnyPartial"] = cny.total.partial
        result["knownCostCnyPartial"] = cny.known.partial
        result["estimatedCostCnyPartial"] = cny.estimated.partial
    coverage = dict(entry["coverage"])
    complete_records = coverage.pop("costCompleteRecords")
    coverage["costComplete"] = (coverage["records"] > 0
                                 and complete_records == coverage["records"])
    result["coverage"] = coverage
    result["tokenUsage"] = _finish_token_usage(
        entry["tokenUsage"], coverage["records"])
    for source, target in (("completeRequests", "completeUsageRequests"),
                           ("lowerBoundRequests", "partialUsageRequests"),
                           ("estimatedRequests", "estimatedUsageRequests"),
                           ("unknownRequests", "unknownUsageRequests")):
        coverage[target] = result["tokenUsage"][source]
    result["priceVersions"] = sorted(entry["priceVersions"])
    return result


def _finish_key(bucket, priced_by_fn):
    models = sorted(bucket["models"].values(),
                     key=lambda item: (-item["costUsd"], item["model"]))
    days = [bucket["days"][day] for day in sorted(bucket["days"])]
    return {
        "key_id": bucket["key_id"],
        **_finish_summary(bucket, priced_by_fn),
        "models": [{"model": entry["model"],
                    **_finish_summary(entry, priced_by_fn)} for entry in models],
        "days": [{"day": entry["day"],
                  **_finish_summary(entry, priced_by_fn)} for entry in days],
        "undated": ({"day": None,
                     **_finish_summary(bucket["undated"], priced_by_fn)}
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
        "unpricedRequests": counters["unpricedRequests"],
        "peakRequests": counters["peakRequests"],
        "unpricedModels": sorted(counters["unpricedModels"]),
        "priceVersions": sorted(counters["priceVersions"]),
        "priceFn": counters["priceFn"],
        "attributionReasons": {
            key: counters["attributionReasons"][key]
            for key in sorted(counters["attributionReasons"])},
        "cacheReportedRequests": counters["cacheReportedRequests"],
        "cacheUnreportedRequests": counters["cacheUnreportedRequests"],
        "cacheUnknownRequests": counters["cacheUnknownRequests"],
        "cacheUnknownPricedRequests": counters["cacheUnknownPricedRequests"],
    })
    if counters["priceFn"]:
        # CNY coverage belongs to the price chain only; a pricer-less read keeps
        # exactly the old shape (no new keys at all).
        result["costCnyPartial"] = counters["costCnyPartial"]
        result["rateFn"] = counters["rateFn"]
    return result
