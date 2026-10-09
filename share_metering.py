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
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone

# 与 usagesrv._read_jsonl 同一上界（只读、有界）。
DEFAULT_LIMIT = 200000
# 私人 chat2api 账本的文件名：绝不读（usagesrv.CHAT2API_LOG 的默认值）。
PRIVATE_LEDGER_BASENAMES = ("usage-chat2api.jsonl",)
# 用量覆盖度的固定取值（写盘端 record_usage 的 usage_source）。
USAGE_SOURCES = ("upstream", "partial", "estimated", "none")
# 只有 "ok" 是一次完整成功；其余（取消/失败/断流/重试）都是已知下界。
COMPLETE_STATUS = "ok"
BEIJING = timezone(timedelta(hours=8))


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


def _empty_summary():
    return {"requests": 0, "costUsd": 0.0, "knownCostUsd": 0.0,
            "estimatedCostUsd": 0.0, "unknownRequests": 0,
            "unpricedRequests": 0, "inputTokens": 0, "outputTokens": 0,
            "coverage": {"records": 0, "pricedRequests": 0,
                         "unknownTimeRequests": 0,
                         "pricingUnavailableRequests": 0,
                         "costCompleteRecords": 0},
            "priceVersions": set(),
            "tokenUsage": {
                "completeRequests": 0, "lowerBoundRequests": 0,
                "estimatedRequests": 0, "unknownRequests": 0,
                "inputTokens": 0, "outputTokens": 0,
                "lowerBoundInputTokens": 0, "lowerBoundOutputTokens": 0,
                "estimatedInputTokens": 0, "estimatedOutputTokens": 0,
                "lowerBoundInputKnown": 0, "lowerBoundOutputKnown": 0,
                "estimatedInputKnown": 0, "estimatedOutputKnown": 0}}


def _empty_key(key_id):
    return {**_empty_summary(), "key_id": key_id, "models": {}, "days": {},
            "undated": {"day": None, **_empty_summary()}}


def _trusted_count(value):
    """Evidence counts must be non-negative JSON integers, including real zero."""
    return value if isinstance(value, int) and not isinstance(value, bool) \
        and value >= 0 else None


def _accumulate_token_usage(usage, rec, source):
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


def _add_summary(entry, rec, source, at_ms, cost_usd, priced, version_id,
                 priced_by_fn, *, include_unknown_request=False):
    # Preserve legacy requests/Token sums; coverage and tokenUsage carry the
    # stricter evidence semantics, including rows formerly skipped by groups.
    if source != "none" or include_unknown_request:
        entry["requests"] += 1
    coverage = entry["coverage"]
    coverage["records"] += 1
    coverage["unknownTimeRequests"] += int(at_ms is None)
    _accumulate_token_usage(entry["tokenUsage"], rec, source)
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
                and _trusted_count(rec.get("output_tokens")) is not None:
            coverage["costCompleteRecords"] += 1


def _refuse_private_ledger(path):
    name = os.path.basename(os.path.normpath(str(path)))
    if name in PRIVATE_LEDGER_BASENAMES:
        raise ValueError(
            "refusing to read the private chat2api ledger; pass the share "
            "backend ledger explicitly")


def _read_records(path, limit):
    """读账本尾部至多 limit 条有效记录；返回 (records, coverage)。"""
    _refuse_private_ledger(path)
    records = []
    total, unparsed = 0, 0
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
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
                records.append(rec)
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


def read_share_usage(ledger_path, price_fn=None, *, limit=DEFAULT_LIMIT):
    """只读汇总一个分享账本；返回按 Key 的请求数与已知/估算/未知费用。

    ``price_fn(model_id, at_ms, input_tok, output_tok, cache_tok,
    reasoning_tok) -> (cost_usd, priced, matched, peak, version_id)`` 是**唯一**
    计价入口，由调用方（usagesrv）提供；``price_fn=None`` 时全部费用字段为
    ``None``，只给计数。``priced`` 为假表示该模型无价（缺价格保留未知，不当
    0），``matched`` 是命中的模型名（按它归组，与 usagesrv 一致），
    ``version_id`` 是价格版本标识（``coverage.priceVersions`` 汇总）。

    返回 ``{requests, keys, unattributed, checkedAt, coverage}``：``keys`` 只含
    归因成功的 Key（按请求数降序、key_id 升序），``unattributed`` 是同一形状
    的未归属桶（``key_id`` 为 ``None``）。计数覆盖全部记录，费用只覆盖能派生
    出费用的记录，差额由 ``unknownRequests``（用量未知）与 ``unpricedRequests``
    （无价）说明。删除或轮换 Key 后历史仍可审阅：本模块不查管理库，账本里出现
    过的 key_id 一律返回。

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
    records, read_coverage = _read_records(ledger_path, limit)
    priced_by_fn = price_fn is not None
    buckets = {}
    counters = {
        "records": len(records),
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
        model = str(rec.get("model") or "unknown")
        at_ms = _parse_ts(rec.get("time"))
        if at_ms is None:
            counters["unknownTimeRequests"] += 1
        source = _source_of(rec)
        if source == "none":
            counters["unknownUsageRequests"] += 1
            cost_usd, priced, matched, peak, version_id = \
                (0.0, False, None, False, None)
        else:
            cost_usd, priced, matched, peak, version_id = _price(
                price_fn, rec, model, at_ms)
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
                     priced_by_fn, include_unknown_request=True)
        for entry in (model_entry, day_entry):
            _add_summary(entry, rec, source, at_ms, cost_usd, priced, version_id,
                         priced_by_fn)
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
        "coverage": _finish_coverage(counters, read_coverage, limit, ledger_path),
    }


def _price(price_fn, rec, model, at_ms):
    """调用唯一的计价入口；未提供价格函数时全部费用未知。

    计价函数声明的 ``priced`` 是它的判断；返回的金额不可用时按无价处理
    （进 ``unpricedRequests``，不当 0），不静默丢弃也不猜一个金额。
    """
    if price_fn is None:
        return 0.0, False, None, False, None
    cost_usd, priced, matched, peak, version_id = price_fn(
        model, at_ms, _count(rec, "input_tokens") or 0,
        _count(rec, "output_tokens") or 0,
        _count(rec, "cached_input_tokens", "cache_tokens") or 0,
        _count(rec, "reasoning_tokens") or 0)
    if not isinstance(cost_usd, (int, float)) or isinstance(cost_usd, bool) \
            or not math.isfinite(cost_usd) or cost_usd < 0:
        cost_usd = 0.0
        priced = False
    return float(cost_usd), bool(priced), matched, bool(peak), version_id


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
    return result


def _finish_summary(entry, priced_by_fn):
    result = {field: entry[field] for field in (
        "requests", "inputTokens", "outputTokens", "unknownRequests",
        "unpricedRequests")}
    for field in ("costUsd", "knownCostUsd", "estimatedCostUsd"):
        result[field] = round(entry[field], 6) if priced_by_fn else None
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


def _finish_coverage(counters, read_coverage, limit, ledger_path):
    result = dict(read_coverage)
    result.update({
        "ledger": os.path.basename(os.path.normpath(str(ledger_path))),
        "limit": limit,
        "records": counters["records"],
        "attributed": counters["attributed"],
        "unattributed": counters["unattributed"],
        "incompleteRequests": counters["incompleteRequests"],
        "unknownUsageRequests": counters["unknownUsageRequests"],
        "unknownTimeRequests": counters["unknownTimeRequests"],
        "unpricedRequests": counters["unpricedRequests"],
        "peakRequests": counters["peakRequests"],
        "unpricedModels": sorted(counters["unpricedModels"]),
        "priceVersions": sorted(counters["priceVersions"]),
        "priceFn": counters["priceFn"],
        "attributionReasons": {
            key: counters["attributionReasons"][key]
            for key in sorted(counters["attributionReasons"])},
    })
    return result
