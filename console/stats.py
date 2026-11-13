#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""console_stats — bounded cross-source usage statistics for the console fee page.

Revision 5 L4 backend. Given a date range (Asia/Shanghai half-open day
interval) plus optional source/model filters, returns one unified aggregation
dict so the console overview and fee views share a single computation instead
of recomputing from the per-day/per-model summary shapes.

Sourcing rule (Revision 5): statistics are always computed from the raw
ledgers. The per-(day, model) granularity required for cross filtering does
not exist in usagesrv's service-level output (models and days are aggregated
along separate axes), so this module re-buckets raw records itself. All
record-field extraction, time parsing, day assignment and price conversion is
taken from the host's injected StatsEnvironment (usagesrv in the local
application) — no second parsing implementation lives here:

  - time -> epoch ms:        environment.parse_ts (usagesrv._parse_ts,
                             usagesrv.py:266-279)
  - epoch ms -> Beijing day: environment.beijing_day (usagesrv._beijing_day,
                             usagesrv.py:524-527; fixed UTC+8, same constant
                             as usagesrv — Asia/Shanghai has no DST since 1991)
  - model matching + pricing + peak tiers: environment.estimate_record
                             (usagesrv.py:483-495) via resolve_price/tier_for
  - price table / checkedAt: environment.price_table_loader
                             (usagesrv.load_price_table, usagesrv.py:379-434)
  - ledger paths + env redirect: environment.chat2api_log / dsh_snapshot
                             (usagesrv module constants,
                             usagesrv.py:54-66), the same constants that
                             build_summary consumes (usagesrv.py:659-710)

Per-field extraction mirrors (line refs at each site):
  - estimate records (chat2api): _bucket loop, usagesrv.py:539-544 —
    cached_input_tokens with cache_tokens fallback, reasoning_tokens; cost
    from usagesrv.estimate_record; group key `matched or model`
    (usagesrv.py:559).
  - DSH snapshot: read_dsh_ledger, usagesrv.py:602-652 — only the frozen
    DSH_MERGE_DAY (usagesrv.py:113, "2026-09-02") participates; cost is real
    ledger money in config.pricingCurrency (USD) converted with exchangeRate,
    never re-estimated. Deliberate deviation: usagesrv reads day-level totals
    directly (usagesrv.py:622-629) while this module derives per-day totals
    from the day's byProviderModel rows (usagesrv.py:631-641), because the
    snapshot's native granularity is exactly per-(day, provider:model); in
    the production snapshot the day-level fields are the sum of those rows.

Rounding: money = round(raw_usd_sum * exchangeRate, 4), identical to
usagesrv._round_service (usagesrv.py:593-599) and build_summary
(usagesrv.py:687, 696-698). To keep the console invariant
totals == sum(day_totals) == sum(source_totals) == sum(models), each parent
level emits the rounded sum of its children's emitted values instead of
rounding its own raw sum independently; per-model raw sums equal usagesrv's
and parent values agree with the sum of their children up to float dust
(< 1e-9 CNY from adding already-rounded children).

"Missing is not zero": a day with no records does not appear in `days` and is
never counted as zero cost; coverage.missing_days lists it. An absent ledger
file is reported via coverage.sources[...].ledger_present=False and
contributes nothing (no fabricated zeros). Malformed lines / reader errors /
bad records mark the source with coverage error info while keeping whatever
valid content could be parsed — partial data is flagged, never invented.

Undated records (unparseable `time`) cannot be proven inside a date-bounded
query: they are excluded from days/totals and counted per source in
coverage.sources[...].undated_records (usagesrv keeps them in service totals
without a day — documented deviation for bounded queries).

Filters: sources/models None means "all". Unknown requested sources produce
an empty result plus a coverage annotation, not an error. Model filtering
matches the raw ledger `model` field exactly (before price-name matching);
aggregation then groups by the usagesrv key (`matched or model`). For the dsh
snapshot the filter applies to the composite "provider:model" key.

A12-1 addition (Plan-Revision 12): `usage_tokens` / `usage_rates` below are
the bounded, read-only query layer behind the two new console endpoints
(`/api/console/usage-tokens`, `/api/console/usage-rates`). Their contract,
source vocabulary, per-source field mapping and honesty rules are documented in
the "A12-1 bounded token / rate query layer" section at the end of this file;
`bounded_stats` keeps its Revision 5 fee fields. Additive complete-token
metrics/ranking use strict integer evidence and separate estimates/lower
bounds; the legacy input/output sums are not their denominator.
"""

import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
from collections import namedtuple
from dataclasses import dataclass
from datetime import date as _date_type, datetime, timedelta, timezone
from typing import Callable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


class StatsNotConfigured(RuntimeError):
    """A statistics read ran without an injected StatsEnvironment.

    The local host builds one environment from its own path constants and
    price loader at startup. There is deliberately no implicit fallback to a
    host module: a missing injection fails loudly instead of reading another
    application's ledgers or price table.
    """


@dataclass(frozen=True)
class StatsEnvironment:
    """Explicit dependencies of the statistics read surface.

    ``chat2api_log`` / ``dsh_snapshot`` are
    the ledger paths owned by the host; ``dsh_merge_day`` is its frozen DSH
    day. ``parse_ts`` / ``beijing_day`` / ``day_start_ms`` / ``display_path``
    are the host's pure time and display rules, ``estimate_record`` keeps the
    historical four-tuple contract (cost_usd, priced, matched_model,
    peak_used), and ``price_table_loader`` returns the host's normalized price
    table. This module owns no host constants of its own.
    """
    chat2api_log: str
    dsh_snapshot: str
    dsh_merge_day: str
    magpie_price_snapshot: str
    magpie_price_status: str
    parse_ts: Callable[[object], Optional[float]]
    beijing_day: Callable[[Optional[float]], Optional[str]]
    day_start_ms: Callable[[object], Optional[float]]
    display_path: Callable[[object], str]
    estimate_record: Callable[..., tuple]
    price_table_loader: Callable[[], dict]


_STATS_ENVIRONMENT = None


def configure(environment):
    """Install the process-wide statistics environment.

    This is an explicit composition-root injection point, not business state:
    the local host calls it once at startup. Reads that receive an explicit
    ``environment=`` argument do not depend on it.
    """
    global _STATS_ENVIRONMENT
    if environment is None:
        raise TypeError("environment must be a StatsEnvironment")
    _STATS_ENVIRONMENT = environment


def _environment():
    environment = _STATS_ENVIRONMENT
    if environment is None:
        raise StatsNotConfigured(
            "console_stats is not configured: call console_stats.configure("
            "StatsEnvironment(...)) in the host composition root")
    return environment


TIMEZONE_NAME = "Asia/Shanghai"  # 口径=usagesrv._beijing_day 固定 UTC+8
MAX_RANGE_DAYS = 92
# 每来源解析口径标签，与 usagesrv 各服务的 kind 一致（usagesrv.py:685/643）。
_SOURCE_KINDS = {
    "chat2api": "estimate",
    "dsh": "dsh-ledger",
}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 与 usagesrv._read_jsonl 的默认 limit 一致（usagesrv.py:500）。
_LEDGER_RECORD_LIMIT = 200000


def default_ledgers(environment=None):
    """Ledger handle assembly for the default (UM) read chain.

    B0 (2026-11-01): the default assembly reads ONLY the UM main chain
    (chat2api) and the frozen DSH snapshot. Paths come from the injected
    ``StatsEnvironment``; the local host builds it from the usagesrv module
    constants resolved at its import time (CHAT2API_USAGE_LOG /
    DSH_LEDGER_SNAPSHOT_FILE), so this module never maintains a second
    path-resolution implementation.
    """
    environment = _environment() if environment is None else environment
    return {
        "chat2api": environment.chat2api_log,
        "dsh": environment.dsh_snapshot,
    }


def _parse_date(value):
    """Strict 'YYYY-MM-DD' -> datetime.date (date/datetime passthrough);
    anything else raises ValueError."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, _date_type):
        return value
    if isinstance(value, str) and _DATE_RE.match(value.strip()):
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            pass
    raise ValueError(
        "invalid date %r: expected YYYY-MM-DD (Asia/Shanghai calendar day)"
        % (value,))


def _parse_range(date_from, date_to):
    """Validate the half-open day interval and enforce the bounded-query cap.

    Returns (from_date, to_date, range_days). from > to or a span above
    MAX_RANGE_DAYS raises ValueError (the HTTP layer maps that to 400).
    """
    lo = _parse_date(date_from)
    hi = _parse_date(date_to)
    if lo > hi:
        raise ValueError(
            "invalid date range: date_from %s > date_to %s"
            % (lo.isoformat(), hi.isoformat()))
    range_days = (hi - lo).days + 1
    if range_days > MAX_RANGE_DAYS:
        raise ValueError(
            "date range too large: %d days exceeds the %d-day query limit"
            % (range_days, MAX_RANGE_DAYS))
    return lo, hi, range_days


def _iter_dates(lo, hi):
    cur = lo
    while cur <= hi:
        yield cur
        cur += timedelta(days=1)


def _read_ledger_jsonl(path, environment=None):
    """Read one JSONL ledger file, mirroring usagesrv._read_jsonl
    (usagesrv.py:500-521) field-for-field: strip lines, skip blanks, parse
    each line as JSON, keep dicts only, attach rec["_ms"] via
    environment.parse_ts, sort ascending by _ms, cap at the same default limit.

    Single deliberate difference: usagesrv silently drops malformed lines,
    while this reader counts them so coverage can flag ledger incompleteness
    honestly (console coverage contract; no records are invented either way).

    Returns (records, diagnostics)."""
    environment = _environment() if environment is None else environment
    records = []
    bad_lines = 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    bad_lines += 1
                    continue
                if isinstance(rec, dict):
                    rec["_ms"] = environment.parse_ts(rec.get("time"))  # 口径=usagesrv.py:514
                    records.append(rec)
    except FileNotFoundError:
        return records, {"missing": True, "bad_lines": bad_lines}
    except OSError as exc:
        return records, {"read_error": str(exc), "bad_lines": bad_lines}
    records.sort(key=lambda r: r.get("_ms") or 0)  # 口径=usagesrv.py:520
    return records[-_LEDGER_RECORD_LIMIT:], {"bad_lines": bad_lines}


def _read_dsh_snapshot(path, environment=None):
    """Read the DSH day snapshot JSON, mirroring usagesrv._load_json
    (usagesrv.py:371-376). Single difference: missing vs corrupt is
    distinguished so coverage can report honestly (absent is not an error).

    Returns (snapshot_dict_or_None, diagnostics)."""
    environment = _environment() if environment is None else environment
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, {"missing": True}
    except OSError as exc:
        return None, {"read_error": environment.display_path(str(exc))}
    except Exception as exc:  # json.JSONDecodeError and friends
        return None, {"corrupt": "%s: %s" % (type(exc).__name__, exc)}
    if not isinstance(data, dict):
        return None, {"corrupt": "snapshot root is not a JSON object"}
    return data, {}


def _resolve_reader(source, handle, environment=None):
    """Normalize one ledgers entry into (reader, paths).

    reader() -> (content, diagnostics). JSONL kinds: content is the merged
    ascending record list; dsh: content is the snapshot dict. Path forms:
    str/PathLike (single file) or a non-empty list/tuple of files merged
    into one stream. Callables are trusted to return the kind's raw shape
    (record list for JSONL kinds, snapshot dict for dsh); their exceptions
    become a source error, never a whole-query failure."""
    environment = _environment() if environment is None else environment
    if callable(handle):
        def reader():
            return handle(), None
        return reader, None
    if isinstance(handle, (str, os.PathLike)):
        paths = [os.fspath(handle)]
    elif isinstance(handle, (list, tuple)) and handle:
        paths = [os.fspath(p) for p in handle]
    else:
        raise TypeError("unsupported ledger handle: %r" % (handle,))

    if source == "dsh":
        def reader():
            snapshot, diag = _read_dsh_snapshot(paths[0], environment)
            return snapshot, diag
        return reader, paths

    def reader():
        merged = []
        diagnostics = {"bad_lines": 0, "missing_files": 0}
        for p in paths:
            recs, diag = _read_ledger_jsonl(p, environment)
            if diag.get("missing"):
                diagnostics["missing_files"] += 1
            diagnostics["bad_lines"] += diag.get("bad_lines", 0)
            if diag.get("read_error"):
                diagnostics["read_error"] = diag["read_error"]
            merged.extend(recs)
        merged.sort(key=lambda r: r.get("_ms") or 0)
        return merged[-_LEDGER_RECORD_LIMIT:], diagnostics

    return reader, paths


def _new_token_evidence():
    return {"records": 0, "complete_records": 0, "partial_records": 0,
            "estimated_records": 0, "unknown_records": 0,
            "complete_input": 0, "complete_output": 0,
            "partial_input": 0, "partial_output": 0,
            "estimated_input": 0, "estimated_output": 0,
            "partial_input_known": 0, "partial_output_known": 0,
            "estimated_input_known": 0, "estimated_output_known": 0}


def _trusted_token_count(value):
    """Narrow evidence validation; legacy fee conversion keeps its own contract."""
    return value if isinstance(value, int) and not isinstance(value, bool) \
        and value >= 0 else None


def _accumulate_token_evidence(acc, rec, *, snapshot=False):
    evidence = acc["token_evidence"]
    evidence["records"] += 1
    input_tok = _trusted_token_count(rec.get("input" if snapshot else "input_tokens"))
    output_tok = _trusted_token_count(rec.get("output" if snapshot else "output_tokens"))
    source = rec.get("usage_source")
    if source == "none" or (input_tok is None and output_tok is None):
        evidence["unknown_records"] += 1
        return
    complete = input_tok is not None and output_tok is not None
    estimated = source == "estimated" or (source is None and rec.get("estimated") is True)
    if estimated:
        prefix = "estimated"
    elif complete and source != "partial" and not _non_ok_status(rec.get("status")):
        evidence["complete_records"] += 1
        evidence["complete_input"] += input_tok
        evidence["complete_output"] += output_tok
        return
    else:
        prefix = "partial"
    evidence[prefix + "_records"] += 1
    for side, value in (("input", input_tok), ("output", output_tok)):
        if value is not None:
            evidence[prefix + "_" + side] += value
            evidence[prefix + "_" + side + "_known"] += 1


def _emit_token_evidence(evidence, kind):
    fields = {"token_coverage": {
        key: evidence[key] for key in ("records", "complete_records",
                                      "partial_records", "estimated_records",
                                      "unknown_records")}}
    coverage = fields["token_coverage"]
    coverage["complete"] = coverage["records"] > 0 \
        and coverage["complete_records"] == coverage["records"]
    coverage["unit"] = "model_day_aggregate" if kind == "dsh-ledger" else "request"
    fields["complete_input_tokens"] = evidence["complete_input"] \
        if coverage["complete_records"] else None
    fields["complete_output_tokens"] = evidence["complete_output"] \
        if coverage["complete_records"] else None
    fields["token_total"] = evidence["complete_input"] + evidence["complete_output"] \
        if coverage["complete_records"] else None
    for prefix in ("partial", "estimated"):
        for side in ("input", "output"):
            fields[prefix + "_" + side + "_tokens"] = evidence[prefix + "_" + side] \
                if evidence[prefix + "_" + side + "_known"] else None
        fields[prefix + "_token_total"] = (
            evidence[prefix + "_input"] + evidence[prefix + "_output"]
            if evidence[prefix + "_input_known"] or evidence[prefix + "_output_known"]
            else None)
    return fields


def _new_entry():
    return {"requests": 0, "input": 0, "output": 0, "cache": 0, "reasoning": 0,
            "cost_usd": 0.0, "priced": 0,
            "token_evidence": _new_token_evidence()}


def _empty_agg():
    return {"days": {}, "undated": 0, "unpriced": set(), "error": None}


def _collect_jsonl_records(records, models_filter, lo_iso, hi_iso,
                           environment=None):
    """Bucket raw JSONL records into per-(day, model) accumulators.

    Field extraction and cost semantics are usagesrv build_summary/_bucket
    同源 (line refs inline); the finer (day x model) granularity is the point
    of this module, not a second parsing implementation.
    """
    days = {}
    undated = 0
    unpriced = set()
    failed = 0
    last_error = None
    environment = _environment() if environment is None else environment
    for rec in records:
        token_seen = False
        token_group = None
        try:
            ms = rec.get("_ms")
            if ms is None:
                ms = environment.parse_ts(rec.get("time"))
            day = environment.beijing_day(ms)
            if day is None:
                # 时刻无效的记录无法证明落在查询区间内：不计入，coverage 如实计数。
                undated += 1
                continue
            if day < lo_iso or day > hi_iso:
                continue
            raw_model = str(rec.get("model") or "unknown")  # 口径=usagesrv.py:539/663
            if models_filter is not None and raw_model not in models_filter:
                continue
            token_group = (day, raw_model)
            # 口径=usagesrv _bucket（usagesrv.py:540-544）：缓存命中视为
            # input 的子集，不重复累计（estimate_record 内部做 input-cache）。
            in_tok = int(rec.get("input_tokens") or 0)
            out_tok = int(rec.get("output_tokens") or 0)
            cache_tok = int(rec.get("cached_input_tokens")
                            or rec.get("cache_tokens") or 0)
            reason_tok = int(rec.get("reasoning_tokens") or 0)
            cost_usd, priced, matched, _peak = environment.estimate_record(
                raw_model, ms, in_tok, out_tok, cache_tok, reason_tok)
            key = matched or raw_model  # 口径=usagesrv.py:559
            acc = days.setdefault(day, {}).setdefault(key, _new_entry())
            _accumulate_token_evidence(acc, rec)
            token_seen = True
            acc["requests"] += 1
            acc["input"] += in_tok
            acc["output"] += out_tok
            acc["cache"] += cache_tok
            acc["reasoning"] += reason_tok
            acc["cost_usd"] += cost_usd
            if priced:
                acc["priced"] += 1
            else:
                # priced False（价表无此模型）在 usagesrv 中落入
                # unpricedModels（usagesrv.py:552-555）。
                unpriced.add(key)
        except Exception as exc:  # 单条坏记录不拖垮整个源，coverage 如实标注
            if token_group is not None and not token_seen:
                day, raw_model = token_group
                acc = days.setdefault(day, {}).setdefault(raw_model, _new_entry())
                _accumulate_token_evidence(acc, rec)
            failed += 1
            last_error = "%s: %s" % (type(exc).__name__, exc)
    error = None
    if failed:
        error = {"type": "record_error", "failed_records": failed,
                 "detail": last_error}
    return {"days": days, "undated": undated, "unpriced": unpriced,
            "error": error}


def _collect_dsh_records(snapshot, models_filter, lo_iso, hi_iso,
                         environment=None):
    """口径=usagesrv read_dsh_ledger（usagesrv.py:602-652）同源：只统计冻结的
    DSH_MERGE_DAY（usagesrv.py:113）；cost 为真实记账（config.pricingCurrency，
    USD），按 exchangeRate 折 CNY，不做估算。偏差：逐日汇总取自该日
    byProviderModel 行加和（usagesrv.py:631-641 的字段口径）而非日级字段
    （usagesrv.py:622-629），因为快照原生粒度即 (day, provider:model)，生产
    快照两者一致；模型筛选作用于 "provider:model" 复合键。"""
    days = {}
    if not isinstance(snapshot, dict):
        return {"days": days, "undated": 0, "unpriced": set(), "error": None}
    environment = _environment() if environment is None else environment
    days_root = snapshot.get("days")
    if days_root is not None and not isinstance(days_root, dict):
        # 形状不对不是空账本：记入 coverage errors，不抛异常（同 A12-1 覆盖口径）。
        return {"days": days, "undated": 0, "unpriced": set(),
                "error": {"type": "corrupt_snapshot",
                          "detail": "snapshot days is not a JSON object: %s"
                                    % type(days_root).__name__}}
    merge_day = environment.dsh_merge_day
    day_e = (days_root or {}).get(merge_day)
    if not isinstance(day_e, dict) or not (lo_iso <= merge_day <= hi_iso):
        return {"days": days, "undated": 0, "unpriced": set(), "error": None}
    by_provider_model = day_e.get("byProviderModel")
    if by_provider_model is not None and not isinstance(by_provider_model, dict):
        return {"days": days, "undated": 0, "unpriced": set(),
                "error": {"type": "corrupt_snapshot",
                          "detail": "byProviderModel is not a JSON object: %s"
                                    % type(by_provider_model).__name__}}
    failed = 0
    last_error = None
    for pm, m in (by_provider_model or {}).items():
        try:
            if not isinstance(m, dict):
                continue
            if models_filter is not None and str(pm) not in models_filter:
                continue
            acc = days.setdefault(merge_day, {}).setdefault(
                str(pm), _new_entry())
            _accumulate_token_evidence(acc, m, snapshot=True)
            # 字段口径=usagesrv.py:636-641（calls/cost/input/output/cacheRead/reasoning）。
            acc["requests"] += int(m.get("calls") or 0)
            acc["cost_usd"] += float(m.get("cost") or 0)
            acc["input"] += int(m.get("input") or 0)
            acc["output"] += int(m.get("output") or 0)
            acc["cache"] += int(m.get("cacheRead") or 0)
            acc["reasoning"] += int(m.get("reasoning") or 0)
        except Exception as exc:
            failed += 1
            last_error = "%s: %s" % (type(exc).__name__, exc)
    error = None
    if failed:
        error = {"type": "record_error", "failed_rows": failed,
                 "detail": last_error}
    return {"days": days, "undated": 0, "unpriced": set(), "error": error}


def _emit_models(kind, entries, rate):
    """Emit per-model rows sorted by money desc then model name（排序对齐
    usagesrv models 列表 usagesrv.py:587/691，附加名称决胜保证确定性）。
    Money rounding mirrors usagesrv（_round_service usagesrv.py:593-599
    / build_summary usagesrv.py:696）; parent levels emit rounded sums of
    their children to keep totals == sum(day_totals) exact up to float dust
    (see module docstring)."""
    rows = []
    for key, acc in entries.items():
        # New token metrics use their own evidence accumulator. The existing
        # money/input/output fields retain the legacy fee API's conversion.
        tokens = _emit_token_evidence(
            acc.get("token_evidence", _new_token_evidence()), kind)
        if kind == "dsh-ledger":
            cost = round(acc["cost_usd"] * rate, 4)  # 真实记账折算，非估算
        elif acc["priced"] > 0:
            cost = round(acc["cost_usd"] * rate, 4)
        else:
            cost = None  # 未定价：不产生费用数字，unpriced_models 另行标注
        rows.append(((-(cost or 0.0), key), {
            "model": key,
            "requests": acc["requests"],
            "input_tokens": acc["input"],
            "output_tokens": acc["output"],
            **tokens,
            "cache_tokens": acc["cache"],
            "reasoning_tokens": acc["reasoning"],
            "estimated_cost_cny": cost,
        }))
    rows.sort(key=lambda pair: pair[0])
    return [row for _, row in rows]


def _emit_source_totals(models_out):
    """Per-day per-source totals as the rounded sum of the emitted models
    (chain consistency)."""
    return {
        "requests": sum(m["requests"] for m in models_out),
        "input_tokens": sum(m["input_tokens"] for m in models_out),
        "output_tokens": sum(m["output_tokens"] for m in models_out),
        "cache_tokens": sum(m["cache_tokens"] for m in models_out),
        "reasoning_tokens": sum(m["reasoning_tokens"] for m in models_out),
        "estimated_cost_cny": round(sum(m["estimated_cost_cny"] or 0.0
                                        for m in models_out), 4),
    }


def _new_totals():
    return {"requests": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_tokens": 0, "reasoning_tokens": 0,
            "estimated_cost_cny": 0.0}


def _add_totals(totals, other):
    """Accumulate a source/day totals dict into a parent totals dict.
    None money is skipped, never treated as a fabricated 0."""
    for key in ("requests", "input_tokens", "output_tokens", "cache_tokens",
                "reasoning_tokens", "estimated_cost_cny"):
        value = other.get(key)
        totals[key] += 0 if value is None else value


def _normalize_totals(totals):
    totals["estimated_cost_cny"] = round(totals["estimated_cost_cny"], 4)
    return totals


def _load_source(source, handle, models_filter, lo_iso, hi_iso,
                 environment=None):
    """Resolve one ledger handle, read it, and bucket its records.
    Returns (agg, coverage) — coverage marks presence, missing files and
    read errors honestly; a missing file never fabricates zero-filled days."""
    environment = _environment() if environment is None else environment
    kind = _SOURCE_KINDS[source]
    coverage = {"ledger_present": False, "days_with_records": 0,
                "kind": kind, "ledger_files": []}
    try:
        reader, paths = _resolve_reader(source, handle, environment)
    except (TypeError, ValueError) as exc:
        coverage["errors"] = [{"type": "invalid_handle", "detail": str(exc)}]
        return _empty_agg(), coverage
    if paths is not None:
        coverage["ledger_files"] = [environment.display_path(p) for p in paths]
    try:
        content, diag = reader()
    except Exception as exc:
        coverage["ledger_present"] = paths is None  # callable handle existed
        coverage["errors"] = [{"type": "reader_error",
                               "detail": "%s: %s" % (type(exc).__name__, exc)}]
        return _empty_agg(), coverage
    diag = diag or {}
    if source == "dsh":
        if diag.get("missing"):
            agg = _empty_agg()  # 快照缺席：present=False，不冒充零
        else:
            coverage["ledger_present"] = True
            if diag.get("read_error"):
                coverage["errors"] = [{"type": "read_error",
                                       "detail": diag["read_error"]}]
            if diag.get("corrupt"):
                coverage.setdefault("errors", []).append(
                    {"type": "corrupt_snapshot", "detail": diag["corrupt"]})
            agg = _collect_dsh_records(
                content if isinstance(content, dict) else {},
                models_filter, lo_iso, hi_iso, environment)
    else:
        records = content
        if not isinstance(records, list):
            coverage["ledger_present"] = paths is None or bool(paths)
            coverage["errors"] = [{"type": "invalid_reader_output",
                                   "detail": "expected a record list"}]
            return _empty_agg(), coverage
        missing = diag.get("missing_files", 0)
        # callable 句柄没有 paths（paths=None）：句柄在场即 present；
        # 路径句柄按成功读取的文件占比判定。
        coverage["ledger_present"] = paths is None or missing < len(paths)
        if missing and coverage["ledger_present"]:
            coverage["errors"] = [{"type": "missing_files", "count": missing}]
        read_error = diag.get("read_error")
        if read_error:
            coverage.setdefault("errors", []).append(
                {"type": "read_error", "detail": read_error})
        bad = diag.get("bad_lines", 0)
        if bad:
            coverage.setdefault("errors", []).append(
                {"type": "malformed_lines", "bad_lines": bad})
        agg = _collect_jsonl_records(records, models_filter, lo_iso, hi_iso,
                                    environment)
    if agg["error"]:
        coverage.setdefault("errors", []).append(agg["error"])
    if agg["undated"]:
        coverage["undated_records"] = agg["undated"]
    coverage["days_with_records"] = len(agg["days"])
    return agg, coverage


def bounded_stats(ledgers, date_from, date_to, sources=None, models=None,
                  environment=None):
    """Bounded statistics query over raw usage ledgers.

    ledgers: mapping source name -> ledger handle. Handles are either a path
    (str/PathLike), a non-empty list of paths (merged), or a zero-argument
    callable returning the raw ledger content (record list for JSONL kinds,
    snapshot dict for dsh). Use default_ledgers() for the production
    assembly.

    date_from/date_to: 'YYYY-MM-DD' (or date/datetime). The range is the
    Asia/Shanghai half-open interval [from 00:00, to+1d 00:00); from > to or
    a span above MAX_RANGE_DAYS raises ValueError (callers map that to 400).

    sources/models: None selects everything; unknown requested sources yield
    an empty result annotated in coverage, not an error.

    Returns the unified dict documented in the module docstring: days[],
    totals, coverage, meta. Money (estimated_cost_cny) stays in its own
    field; missing days are reported in coverage.missing_days and never
    treated as zero cost.

    environment: the injected StatsEnvironment. When None, the process-wide
    environment installed by ``configure`` is used; an unconfigured process
    raises StatsNotConfigured instead of reading a host module's paths.
    """
    if not isinstance(ledgers, dict):
        raise TypeError("ledgers must be a dict of source -> ledger handle")
    environment = _environment() if environment is None else environment
    lo, hi, range_days = _parse_range(date_from, date_to)
    models_filter = None if models is None else {str(m) for m in models}
    selected = sorted(set(sources) if sources is not None else set(ledgers))
    table = environment.price_table_loader()
    rate = table["exchangeRate"]
    lo_iso, hi_iso = lo.isoformat(), hi.isoformat()

    per_source = {}
    coverage_sources = {}
    for source in selected:
        handle = ledgers.get(source)
        kind = _SOURCE_KINDS.get(source)
        if handle is None or kind is None:
            coverage_sources[source] = {
                "ledger_present": False, "days_with_records": 0,
                "kind": kind, "ledger_files": [],
                "errors": [{"type": "unknown_source"
                            if handle is None else "unsupported_source",
                            "detail": "requested source has no usable ledger"}],
            }
            per_source[source] = _empty_agg()
            continue
        agg, cov = _load_source(source, handle, models_filter, lo_iso, hi_iso,
                               environment)
        per_source[source] = agg
        coverage_sources[source] = cov

    day_keys = sorted({day for agg in per_source.values() for day in agg["days"]})
    totals = _new_totals()
    days_out = []
    for day in day_keys:
        blocks = []
        day_total = _new_totals()
        for source in sorted(per_source):
            agg = per_source[source]
            entries = agg["days"].get(day)
            if not entries:
                continue
            kind = _SOURCE_KINDS[source]
            models_out = _emit_models(kind, entries, rate)
            src_totals = _emit_source_totals(models_out)
            blocks.append({"source": source, "kind": kind,
                           "models": models_out,
                           "source_totals": src_totals})
            _add_totals(day_total, src_totals)
        # Complete-evidence totals alone enter the ranking. Missing, estimated
        # and interrupted usage stays in model rows and day coverage.
        day_token_rows = []
        token_coverage = {key: 0 for key in (
            "records", "complete_records", "partial_records",
            "estimated_records", "unknown_records")}
        for block in blocks:
            for row in block["models"]:
                for key in token_coverage:
                    token_coverage[key] += row["token_coverage"][key]
                if row["token_total"] is None:
                    continue
                day_token_rows.append({
                    "source": block["source"],
                    "model": row["model"],
                    "input_tokens": row["complete_input_tokens"],
                    "output_tokens": row["complete_output_tokens"],
                    "token_total": row["token_total"],
                    "partial_token_total": row["partial_token_total"],
                    "estimated_token_total": row["estimated_token_total"],
                    "token_coverage": row["token_coverage"],
                })
        day_token_rows.sort(
            key=lambda row: (-row["token_total"], row["source"],
                             row["model"]))
        days_out.append({"date": day, "sources": blocks,
                         "model_token_ranking": day_token_rows,
                         "token_coverage": {**token_coverage,
                             "complete": token_coverage["records"] > 0
                             and token_coverage["complete_records"]
                             == token_coverage["records"]},
                         "day_total": _normalize_totals(day_total)})
        _add_totals(totals, day_total)

    in_range = {d.isoformat() for d in _iter_dates(lo, hi)}
    missing_days = sorted(in_range - set(day_keys))

    unpriced = set()
    for agg in per_source.values():
        unpriced |= agg["unpriced"]

    checked_at = table["meta"].get("checkedAt")
    return {
        "days": days_out,
        "totals": _normalize_totals(totals),
        "coverage": {
            "sources": coverage_sources,
            "missing_days": missing_days,
            "unpriced_models": sorted(unpriced),
        },
        "meta": {
            "timezone": TIMEZONE_NAME,
            "range_days": range_days,
            "price_checked_at": checked_at if isinstance(checked_at, str) else None,
        },
    }


# =====================================================================
# A12-1 bounded token / rate query layer (console fee page)
# =====================================================================
#
# 端点契约（A12-1 与前端 A12-3 共用，不得各自改名）：
#
#   GET /api/console/usage-tokens?days=14     只读、有界、no-store
#     days[]    {date, source, model, input_tokens, output_tokens, complete,
#                complete_requests, partial_requests,
#                partial_input_tokens, partial_output_tokens}
#               input_tokens/output_tokens 只来自「输入与输出都齐全」的请求求和；
#               无此类请求时 null。complete ⇔ partial_requests == 0。
#               complete_requests = 参与上面两个求和的请求数；
#               partial_requests = 缺任一分量的请求数（另列，不混入完整总量）。
#               partial_input_tokens/partial_output_tokens = 仅来自部分请求的该分量
#               合计（部分请求里完全无该分量时为 null），用于「仅输出可见」展示。
#     missing[] {date, source, model, reason, input_tokens, output_tokens}
#               reason ∈ _MISSING_REASONS；date=null 表示时间戳无效
#               （reason="invalid_time"，无日可归）。input_tokens/output_tokens =
#               该桶「部分请求」中可展示的分量合计（完全不可见时 null），供
#               「仅输出可见」直接显示；invalid_time 行没有可归的桶，分量一律 null。
#     coverage  {days, complete_rows, partial_rows, scanned, truncated, clamped,
#                dropped_rows, dropped_missing_rows,
#                sources{<source>: {ledger, ledger_present,
#                enabled, events, note?, errors?, truncated?, ...}}}
#               days[] 与 missing[] 各自按 MAX_ROWS_PER_ENDPOINT 截断，丢弃条数
#               分别记在 dropped_rows / dropped_missing_rows；任一截断都置
#               coverage.truncated=True。
#               「事件集不可信」的来源（errors[].type ∈ UNTRUSTED_EVENT_ERRORS：
#               本次读取失败/结构损坏）整源排除，days[]/missing[] 不出现其行；
#               错误照旧在 coverage errors 如实披露（不静默、不折 0）。
#     timezone="Asia/Shanghai"、checked_epoch
#
#   GET /api/console/usage-rates?hours=24      只读、有界、no-store
#     hours[]   {hour, source, model, output_tokens, duration_ms, requests, rate}
#     skipped   {missing_output_tokens, missing_duration, invalid_duration,
#                invalid_time}（= _SKIPPED_REASONS；前三项对窗口内记录相互
#                独立计数，见「计数语义」）
#     coverage  {hours, rows, truncated, clamped, dropped_rows, sources{...}}
#               「事件集不可信」的来源（errors[].type ∈ UNTRUSTED_EVENT_ERRORS：
#               本次读取失败/结构损坏）整源排除，hours[] 不出现其行、也不计入
#               任何 skipped 原因；错误照旧在 coverage errors 如实披露。
#     timezone="Asia/Shanghai"、checked_epoch
#
# 口径（D12-3/D12-4，与 CONTEXT.md 的「Token 消耗量」「模型整次平均输出速率」
# 一致，不另造）：
#   - 完整 Token = 输入 + 输出，且只由「输入与输出都齐全」的请求构成：同桶里一条
#     缺分量的请求不得把其它完整请求排除出完整总量（F1 修复点），也不得被丢弃或
#     补零——它进 missing 另列，可展示的分量走 partial_*（days[] 行）与
#     missing[] 行的 input_tokens/output_tokens。分量为「参与求和的记录之和」，
#     完全无该分量时取 null；真实 0 是已知值（记录里写了 0 就是 0），不是缺失。
#     complete=true ⇔ 桶内没有缺分量的请求（= partial_requests == 0）。
#   - 速率 = 输出 Token ÷ 整次耗时（含首字等待），标「整次平均」，不等于纯
#     生成速度；分母只含耗时为正且输出 Token 齐全的记录，被跳过的记录按原因
#     计数单列（skipped），rate 为 null 表示该行无法计算，绝不填 0。
#   - 身份键 = source + model（同名跨来源不混并）；时间按北京时间自然日/小时
#     分桶（固定 UTC+8，口径=usagesrv._beijing_day，usagesrv.py:551-554）。
#
# 来源词表（端点契约 um|chat2api|dsh）：
#   * chat2api      <- data/usage-chat2api.jsonl（记录自带 service="chat2api"）
#   * dsh           <- data/dsh-ledger-20260902.json（冻结日快照）
#   * um            本轮无独立账本：UM 流量由 chat2api 账本记账，来源键即 chat2api。
#                   词表保留 um，但本轮不产出该来源的行；不造别名、不合并同名。
#
# 时间口径：时间解析复用 usagesrv._parse_ts（usagesrv.py:293-306；带 Z 或带
# 偏移的 ISO 串按各自偏移解析，无偏移串按 UTC，这是 usagesrv 既有口径）；归日
# 复用 usagesrv._beijing_day（固定 UTC+8）。无法解析的时间戳不丢弃，进 missing
# 并以 reason="invalid_time" 另列（date=null，因为不存在可归的北京自然日）。
#
# 有界：单次读取字节上限 MAX_READ_BYTES（每次读取超出即只读尾部，首读与后续
# 大批追加一视同仁，标 bytes_truncated）、单文件保留事件上限 _LEDGER_RECORD_LIMIT
# （同 usagesrv._read_jsonl 默认 200000）、每端点返回行数上限
# MAX_ROWS_PER_ENDPOINT（days[] 与 missing[] 各自适用）。超界截断并在 coverage
# 里如实标注 truncated / dropped_rows / dropped_missing_rows / clamped。
#
# 增量缓存：进程内按 (path, size, mtime, dev/ino) 缓存，只解析新增尾部；首读、
# 文件缩短、文件身份变化或同长度原地重写时整体重读。尾部半行不消费，留给下一次
# 读取（写入方可能正在追加）。解析失败/字段缺失不抛异常，记入 skipped/missing。
#
# 计数语义：skipped 的原因计数按原因独立——一条记录可同时缺输出 Token 又缺
# 耗时（或耗时非正），故这些计数之和可能大于被跳过的记录条数（不合并成
# 「一条只算一次」的伪总数）；唯一互斥的是耗时的两种状态（missing / invalid，
# 同一条记录的耗时只有一个状态）。invalid_time 单独计数：无时间戳的记录无法
# 证明落在窗口内，故不进其余三项计数。同理 tokens 端点的 coverage.partial_rows
# 只数 complete=false 的行，missing 里还额外含 invalid_time 条目（它们没有可归
# 的日期，不是行）。
#
# 覆盖说明（coverage.sources）：逐来源给出 ledger/ledger_present/enabled/events
# 与 note；账本文件缺席时 note 明确说明「本来源不出行，也不以 0 冒充无用量」，
# 与「该窗口确实无用量」区分。快照根、days 或
# byProviderModel 不是 JSON 对象时同样不抛异常：记入 coverage errors
# （corrupt_ledger），可解析的部分照常保留。
#
# 「事件集不可信」的来源（coverage errors[].type ∈ UNTRUSTED_EVENT_ERRORS）：
# 本次读取失败（read_error，读取层退回上次缓存的事件）或结构损坏
# （corrupt_ledger，事件集可能为空或不完整）时，旧缓存不是本次读数，两个端点
# 一律整源排除（days[]/hours[]/missing[]/skipped 不出现该来源的行与计数），
# 判定单点见 _events_trusted；错误仍由 coverage errors 如实披露（不静默、
# 不折 0）。malformed_lines（部分坏行、其余可读）不算，照常计数。

BEIJING_TZ = timezone(timedelta(hours=8))  # 口径=usagesrv._beijing_day（固定 UTC+8）
MAX_TOKEN_RANGE_DAYS = 31       # 近 N 日 Token 查询的窗口上限
MAX_RATE_RANGE_HOURS = 168      # 近 N 小时速率查询的窗口上限（7 天）
MAX_ROWS_PER_ENDPOINT = 5000     # 每端点返回行数上限
MAX_READ_BYTES = 32 * 1024 * 1024  # 单次读取字节上限（超出只读尾部）

# 每来源字段映射：字段名按各账本既有写入方（usagesrv._bucket / build_summary /
# read_dsh_ledger）取值，不另造；元组为可接受的别名（按序取第一个可用值）。
_SOURCE_SPECS = {
    "chat2api": {
        "kind": "jsonl",
        "time": ("time",), "input": ("input_tokens",),
        "output": ("output_tokens",), "duration": ("duration_ms",),
        # Q11 cache report status + the cache/thinking side channels the UM
        # read surface needs. The fields stay additive: the A12-1 endpoints keep
        # using only input/output/duration and are unaffected by these extras.
        "cache": ("cached_input_tokens", "cache_tokens"),
        "cache_report_status": ("cache_report_status",),
        "reasoning": ("reasoning_tokens",),
        "request_id": ("request_id",),
        "usage_source": ("usage_source",),
        "estimated": ("estimated",),
        "unknown_fields": [],
        "time_source": "账本 time 字段（ISO 8601，带偏移或 Z）",
    },
    "dsh": {
        "kind": "snapshot",
        "time": ("date",), "input": ("input",), "output": ("output",),
        "duration": (),
        "unknown_fields": ["duration_ms"],
        "time_source": "快照日（日级聚合，事件时间取该日北京 00:00）",
    },
}
# missing[].reason 的契约枚举（端点契约的封闭取值）：前三项是分量缺失原因；
# invalid_time 是「时间戳无效、无北京自然日可归」的记录（date=null）。
_MISSING_REASONS = ("output_only", "no_input", "no_tokens", "invalid_time")
# usage-rates 的 skipped 计数键（同一套原因，键名按该端点契约；顺序即渲染顺序）。
_SKIPPED_REASONS = ("missing_output_tokens", "missing_duration",
                    "invalid_duration", "invalid_time")

# coverage errors[].type 词表（单一来源）：生产端（本模块写 coverage）与消费端
# （usagesrv 判定事件集是否可信）都取这里的常量，不各写一份字面量。
ERROR_TYPE_READ = "read_error"                  # 本次读取失败（stat/open）
ERROR_TYPE_CORRUPT_LEDGER = "corrupt_ledger"    # 快照/账本结构损坏
ERROR_TYPE_MALFORMED_LINES = "malformed_lines"   # 部分坏行、其余仍可读
# 「事件集不可信」的错误类型：这些情形下本次读取没有拿到可信的事件集，即便
# ledger_present 仍为 true 也不能当成本次读数——read_error 时读取层退回的是上次
# 缓存的事件，旧缓存不是本次读数；corrupt_ledger 时事件集可能为空或不完整。
# malformed_lines 不算：坏行已在 coverage 披露，其余事件仍可计数。
UNTRUSTED_EVENT_ERRORS = frozenset({ERROR_TYPE_READ,
                                    ERROR_TYPE_CORRUPT_LEDGER})


def _events_trusted(coverage):
    """一个来源的 coverage 说明本次事件集是否可信（单点判定，两个端点与
    usagesrv 共用）。

    errors 里出现 UNTRUSTED_EVENT_ERRORS 任一类型即不可信：该来源必须整源排除
    （days[]/hours[]/missing[]/skipped 都不出现该来源的行与计数），错误本身仍由
    coverage errors 如实披露（不静默）。无 errors 或只有 malformed_lines 时可信。
    """
    for err in coverage.get("errors") or []:
        if isinstance(err, dict) and err.get("type") in UNTRUSTED_EVENT_ERRORS:
            return False
    return True


# 规范化事件：ms=None 表示时间戳无效（进 missing，不丢弃）。
# The trailing fields are additive (B5 UM read surface): existing A12-1 readers
# keep using the first seven fields; defaults keep every other producer working.
# A2-8: request_started_at is the request START in epoch ms (None when the row
# has no usable start evidence). It never replaces ms, which stays the completion
# time used for price-version selection.
_Event = namedtuple(
    "_Event",
    "ms model input_tokens output_tokens duration_ms duration_state status"
    " cache_tokens reasoning_tokens cache_report_status request_id"
    " usage_source estimated request_started_at",
    defaults=(None, None, None, None, None, False, None))


def default_sources(environment=None):
    """A12-1 账本句柄装配：默认只装配 UM 主链（chat2api + 冻结 DSH 快照）。

    B0（2026-11-01）：默认装配只有这两个来源；调用方不能靠本装配
    拿到未登记来源的账本，未登记来源按 unknown_source 如实披露。
    """
    environment = _environment() if environment is None else environment
    return {
        "chat2api": environment.chat2api_log,
        "dsh": environment.dsh_snapshot,
    }


# --------------------------------------------------------- 增量读取与缓存

_FILE_CACHE = {}  # path -> state dict（见 _refresh_jsonl_cache）
_FILE_CACHE_LOCK = threading.Lock()


def _reset_cache_for_tests():
    """清空进程内增量读缓存（测试隔离；账本轮转/整体重读时也走这里）。"""
    with _FILE_CACHE_LOCK:
        _FILE_CACHE.clear()


def _cache_state(path):
    """只读快照：一个路径当前的缓存状态（测试与诊断用，不含可变引用）。"""
    with _FILE_CACHE_LOCK:
        state = _FILE_CACHE.get(path)
        if state is None:
            return None
        snapshot = dict(state)
        snapshot["events"] = list(state["events"])
        return snapshot


def _int_or_none(value):
    """整数化：bool 不是计数；非整数数值与不可解析字符串一律 None（不折零）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _first_value(record, fields):
    """字段别名链上第一个非 null 值；全缺返回 None。"""
    for name in fields:
        if name in record and record.get(name) is not None:
            return record.get(name)
    return None


def _non_ok_status(status):
    """status 是否可证明不是成功：数值非 2xx，或非空且非 ok 的字符串。

    只做披露用（既不参与估算，也不静默剔除）；不可证明的一律不算失败
    （None、空串、非数值/非字符串类型）。chat2api 记 status="ok"，其余
    写法在这里统一归一。
    """
    if status is None or isinstance(status, bool):
        return False
    if isinstance(status, (int, float)):
        return not (200 <= status < 300)
    if isinstance(status, str):
        text = status.strip().lower()
        if not text or text in ("ok", "success"):
            return False
        try:
            return not (200 <= int(text) < 300)
        except ValueError:
            return True
    return False


def _duration_state(record, fields):
    """(duration_ms, state)：state ∈ ok|missing|invalid。

    missing = 字段缺席或为 null；invalid = 非数值或非正值（D12-4 分母只含正耗时）。
    """
    if not fields:
        return None, "missing"
    raw = _first_value(record, fields)
    if raw is None:
        return None, "missing"
    value = _int_or_none(raw)
    if value is None or value <= 0:
        return None, "invalid"
    return value, "ok"


def _first_int(record, fields):
    """字段别名链上第一个可解释为整数的值；缺失/非数值一律 None（不折零）。"""
    for name in fields:
        if name in record:
            value = _int_or_none(record.get(name))
            if value is not None:
                return value
    return None


def _event_cache_report_status(record, spec):
    """Q11 cache report status on one ledger row; None = historical unknown.

    The write side (chat2api.record_usage) writes "reported"/"unreported" only
    for new rows. An absent value is NOT "unreported" and NOT a measured zero:
    the old compatibility ``cached_input_tokens=0`` must not be read as a real
    cache hit (usagesrv._cache_report_status keeps the same vocabulary).
    """
    raw = _first_value(record, spec.get("cache_report_status") or ())
    return raw if raw in ("reported", "unreported") else None


def _event_request_start(record):
    """A2-8 请求 START → epoch ms；缺失或不可解析一律 None。

    chat2api 写端写入 ISO-8601 字符串（不可用时为 null），价格契约同时接受
    epoch ms 数值。坏值按缺失处理：既不能凭它产生新的定价资格，也不能用
    ``time - duration_ms`` 倒推一个开始时刻。
    """
    value = record.get("request_started_at")
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value else None
    return _environment().parse_ts(value)


def _event_from_record(source, spec, record):
    """一条账本记录 → 规范化事件（字段口径见 _SOURCE_SPECS；不抛异常）。"""
    ms = _environment().parse_ts(_first_value(record, spec["time"]))
    duration_ms, duration_state = _duration_state(record, spec["duration"])
    usage_source = record.get("usage_source")
    if usage_source not in ("upstream", "partial", "estimated", "none"):
        usage_source = None
    request_id = record.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        request_id = None
    return _Event(
        ms=ms,
        model=str(record.get("model") or "unknown"),  # 口径=usagesrv.py:566/747
        input_tokens=_first_int(record, spec["input"]),
        output_tokens=_first_int(record, spec["output"]),
        duration_ms=duration_ms,
        duration_state=duration_state,
        status=record.get("status"),
        cache_tokens=_first_int(record, spec.get("cache") or ()),
        reasoning_tokens=_first_int(record, spec.get("reasoning") or ()),
        cache_report_status=_event_cache_report_status(record, spec),
        request_id=request_id,
        usage_source=usage_source,
        estimated=record.get("estimated") is True,
        request_started_at=_event_request_start(record),
    )


def _refresh_jsonl_cache(path, source, spec):
    """增量解析一个 JSONL 账本，返回 (state, diagnostics)。

    只解析自上次 offset 起的新增字节；每次读取都以 MAX_READ_BYTES 为字节上限
    ——首读、缩短、身份变化、同长度原地重写时的整体重读，以及后续大批追加，
    超限时一律只读尾部并标 bytes_truncated（丢弃起始处的残行）。尾部半行不消费，
    留给下一次。读取在 _FILE_CACHE_LOCK 内完成：并发查询不会重复解析或交叉写缓存。
    """
    with _FILE_CACHE_LOCK:
        state = _FILE_CACHE.get(path)
        try:
            info = os.stat(path)
        except FileNotFoundError:
            _FILE_CACHE.pop(path, None)
            return None, {"missing": True}
        except OSError as exc:
            return state, {"read_error": str(exc)}
        size, mtime_ns = info.st_size, info.st_mtime_ns
        dev = getattr(info, "st_dev", 0)
        ino = getattr(info, "st_ino", 0)
        if state is not None and state["size"] == size \
                and state["mtime_ns"] == mtime_ns:
            return state, {}
        if state is None or size < state["offset"] or state["dev"] != dev \
                or state["ino"] != ino or (size == state["offset"]
                                           and state["mtime_ns"] != mtime_ns):
            state = {"offset": 0, "size": size, "mtime_ns": mtime_ns,
                     "dev": dev, "ino": ino, "events": [], "bad_lines": 0,
                     "dropped_events": 0, "bytes_truncated": False}
        read_start = state["offset"]
        bytes_truncated = state["bytes_truncated"]
        head_skipped = 0
        # 单次读取字节上限：本次待读字节超过 MAX_READ_BYTES（首读之外，后续大批
        # 追加同样适用）时只读尾部 MAX_READ_BYTES，起始点落在行中，故丢弃首个
        # 残行；本来源标 bytes_truncated（coverage 语义），绝不静默全量读入。
        clamped_start = size - read_start > MAX_READ_BYTES
        if clamped_start:
            read_start = size - MAX_READ_BYTES
            bytes_truncated = True
        try:
            with open(path, "rb") as f:
                f.seek(read_start)
                blob = f.read(max(0, size - read_start))
        except OSError as exc:
            return state, {"read_error": str(exc)}
        if clamped_start:
            cut = blob.find(b"\n")  # 只读尾部时丢弃首个残行
            head_skipped = cut + 1 if cut >= 0 else len(blob)
        data = blob[head_skipped:]
        end = data.rfind(b"\n")
        if end < 0:
            # 尾部半行不消费：写入方可能正在追加，下次再读
            consumed, new_offset = b"", state["offset"]
        else:
            consumed = data[:end + 1]
            new_offset = read_start + head_skipped + len(consumed)
        events = state["events"]
        bad_lines = state["bad_lines"]
        for raw in consumed.split(b"\n"):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line.decode("utf-8"))
            except Exception:
                bad_lines += 1
                continue
            if not isinstance(record, dict):
                bad_lines += 1
                continue
            try:
                events.append(_event_from_record(source, spec, record))
            except Exception:  # 单条坏记录不拖垮整个源
                bad_lines += 1
        dropped = state["dropped_events"]
        if len(events) > _LEDGER_RECORD_LIMIT:
            dropped += len(events) - _LEDGER_RECORD_LIMIT
            del events[:len(events) - _LEDGER_RECORD_LIMIT]
        new_state = {"offset": new_offset, "size": size, "mtime_ns": mtime_ns,
                     "dev": dev, "ino": ino, "events": events,
                     "bad_lines": bad_lines, "dropped_events": dropped,
                     "bytes_truncated": bytes_truncated}
        _FILE_CACHE[path] = new_state
        return new_state, {}


def _snapshot_events(snapshot):
    """DSH 冻结日快照 → 规范化事件 + 坏行计数 + 形状损坏说明。

    口径=usagesrv.read_dsh_ledger（usagesrv.py:629-679）：只统计
    DSH_MERGE_DAY 这一天；每 byProviderModel 行一条事件，模型键为
    "provider:model" 复合键（与 console_stats._collect_dsh_records 同口径）。
    快照是日级聚合、没有逐次耗时，故 duration_state=missing（速率端点按
    skipped.missing_duration 计数，不臆造耗时）。

    Returns (events, bad_rows, corrupt)：根、days 或 byProviderModel 不是 JSON
    对象时 corrupt 为说明字符串（本来源仍可读，只是形状不对）——调用方把它记入
    coverage errors[].type="corrupt_ledger"，不抛异常、不把整次查询变成 502；
    可解析的部分照常保留。形状正确时 corrupt 为 None。
    """
    if not isinstance(snapshot, dict):
        return [], 0, "snapshot root is not a JSON object: %s" \
            % type(snapshot).__name__
    day = _environment().dsh_merge_day
    days_root = snapshot.get("days")
    if days_root is None:
        days_root = {}
    if not isinstance(days_root, dict):
        return [], 0, "snapshot days is not a JSON object: %s" \
            % type(days_root).__name__
    day_entry = days_root.get(day)
    if not isinstance(day_entry, dict):
        return [], 0, None
    by_provider_model = day_entry.get("byProviderModel")
    if by_provider_model is None:
        by_provider_model = {}
    if not isinstance(by_provider_model, dict):
        return [], 0, "byProviderModel is not a JSON object: %s" \
            % type(by_provider_model).__name__
    day_start_ms = _environment().parse_ts(day + "T00:00:00+08:00")
    events, bad_rows = [], 0
    for provider_model, row in by_provider_model.items():
        if not isinstance(row, dict):
            bad_rows += 1
            continue
        try:
            events.append(_Event(
                ms=day_start_ms, model=str(provider_model),
                input_tokens=_int_or_none(row.get("input")),
                output_tokens=_int_or_none(row.get("output")),
                duration_ms=None, duration_state="missing", status=None,
                cache_tokens=_int_or_none(row.get("cacheRead")),
                reasoning_tokens=_int_or_none(row.get("reasoning"))))
        except Exception:
            bad_rows += 1
    return events, bad_rows, None


def _refresh_snapshot_cache(path):
    """JSON 快照（DSH 账本）按 (path, size, mtime) 缓存整份解析结果。

    诊断随解析结果一并缓存：损坏说明（corrupt）在缓存命中时仍然返回，不因第二次
    查询而消失（覆盖披露必须稳定，不把坏快照在复读时当成正常空账本）。
    """
    with _FILE_CACHE_LOCK:
        state = _FILE_CACHE.get(path)
        try:
            info = os.stat(path)
        except FileNotFoundError:
            _FILE_CACHE.pop(path, None)
            return None, {"missing": True}
        except OSError as exc:
            return state, {"read_error": str(exc)}
        if state is not None and state["size"] == info.st_size \
                and state["mtime_ns"] == info.st_mtime_ns:
            return state, dict(state.get("diag") or {})
        snapshot, diag = _read_dsh_snapshot(path)
        if diag.get("missing"):
            _FILE_CACHE.pop(path, None)
            return None, {"missing": True}
        events, bad_rows, corrupt = ([], 0, None)
        if not diag and isinstance(snapshot, dict):
            events, bad_rows, corrupt = _snapshot_events(snapshot)
        if corrupt:
            diag = dict(diag or {})
            diag["corrupt"] = corrupt
        new_state = {"offset": info.st_size, "size": info.st_size,
                     "mtime_ns": info.st_mtime_ns,
                     "dev": getattr(info, "st_dev", 0),
                     "ino": getattr(info, "st_ino", 0), "events": events,
                     "bad_lines": bad_rows, "dropped_events": 0,
                     "bytes_truncated": False, "diag": dict(diag or {})}
        _FILE_CACHE[path] = new_state
        return new_state, diag


def _load_source_events(source, handle, spec):
    """一个来源 → (events, state, diagnostics)；只读、有界、不抛异常。

    非路径句柄（未支持形状）返回空事件 + 覆盖说明，绝不猜测内容。
    """
    if not isinstance(handle, (str, os.PathLike)):
        return [], None, {"unsupported_handle": True}
    path = os.fspath(handle)
    if spec["kind"] == "snapshot":
        state, diag = _refresh_snapshot_cache(path)
    else:
        state, diag = _refresh_jsonl_cache(path, source, spec)
    if state is None:
        return [], None, diag or {}
    return state["events"], state, diag or {}


def _source_coverage(source, spec, path, events, state, diag):
    """一个来源的覆盖披露：账本在场、是否启用、坏行/截断/未知字段与计数。"""
    present = state is not None
    coverage = {
        "ledger": _environment().display_path(path),
        "ledger_present": present,
        "enabled": True,
        "events": len(events),
        "time_source": spec["time_source"],
        "unknown_fields": list(spec["unknown_fields"]),
    }
    if not present:
        coverage["note"] = "账本文件缺席：本来源不出行，不冒充零用量"
        return coverage
    if diag.get("read_error"):
        coverage["errors"] = [{"type": ERROR_TYPE_READ,
                              "detail": diag["read_error"]}]
    if diag.get("corrupt"):
        coverage.setdefault("errors", []).append(
            {"type": ERROR_TYPE_CORRUPT_LEDGER, "detail": diag["corrupt"]})
    if state.get("bad_lines"):
        coverage.setdefault("errors", []).append(
            {"type": ERROR_TYPE_MALFORMED_LINES,
             "bad_lines": state["bad_lines"]})
    if state.get("dropped_events"):
        coverage["dropped_events"] = state["dropped_events"]
        coverage["truncated"] = True
    if state.get("bytes_truncated"):
        coverage["bytes_truncated"] = True
        coverage["truncated"] = True
    coverage["undated_records"] = sum(1 for ev in events if ev.ms is None)
    coverage["non_ok_records"] = sum(
        1 for ev in events if _non_ok_status(ev.status))
    return coverage


def _unsupported_coverage(handle):
    return {
        "ledger": _environment().display_path(handle)
        if isinstance(handle, (str, os.PathLike)) else None,
        "ledger_present": False,
        "enabled": False,
        "events": 0,
        "time_source": None,
        "unknown_fields": [],
        "note": "本轮无独立账本：本来源不出行（不造别名、不合并同名）",
    }


# ------------------------------------------------------------- 聚合

def _now_ms(now=None):
    """查询基准时刻 → epoch ms：None=当前；datetime=该时刻（无时区按
    usagesrv._parse_ts 同口径视为 UTC）；数值=epoch 秒。"""
    if now is None:
        return int(time.time() * 1000)
    if isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return int(now.timestamp() * 1000)
    if isinstance(now, (int, float)):
        return int(float(now) * 1000)
    raise TypeError("now must be None, a datetime or epoch seconds")


def _clamp_int(value, default, low, high):
    """整数参数解析 + 收口到 [low, high]；返回 (值, 是否被收口)。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if number < low:
        return low, number != low
    if number > high:
        return high, True
    return number, False


def _window_day_keys(days, now_ms):
    """窗口内北京自然日（升序），末项=基准时刻所在日。"""
    today = datetime.fromtimestamp(now_ms / 1000, tz=BEIJING_TZ).date()
    return [(today - timedelta(days=offset)).isoformat()
            for offset in range(days - 1, -1, -1)]


def _window_hour_starts(hours, now_ms):
    """窗口内整点（epoch ms 升序），末项=基准时刻所在整点。

    +08:00 为整小时偏移，故 UTC 整点边界即北京时间整点边界。
    """
    current = (now_ms // 3_600_000) * 3_600_000
    return [current - offset * 3_600_000 for offset in range(hours - 1, -1, -1)]


def _hour_label(ms):
    return datetime.fromtimestamp(ms / 1000, tz=BEIJING_TZ).strftime(
        "%Y-%m-%dT%H:00:00+08:00")


def _missing_reason(acc):
    """缺分量的原因：_MISSING_REASONS 里的三选一（互斥，见模块说明）。

    invalid_time 不由本函数产生（无时间戳的记录没有可归的日期，单独成条）。
    只对含部分请求的桶调用（complete 桶无原因可言）。

    output_only = 输入分量不完整（可能完全不可见，如国际日志）；no_input =
    输出分量不完整；no_tokens = 两个分量都不完整。
    """
    input_complete = acc["partial_input_known"] == acc["partial_requests"]
    output_complete = acc["partial_output_known"] == acc["partial_requests"]
    if not input_complete and not output_complete:
        return "no_tokens"
    if not input_complete:
        return "output_only"
    return "no_input"


def _empty_bucket():
    """桶累加器：完整请求与部分请求分开求和（F1 修复点）。

    complete_* 只累计「输入与输出都齐全」的请求；partial_* 只累计缺任一分量
    的请求，并按分量分别记下可展示合计与可见条数（partial_input_known /
    partial_output_known = 部分请求中该分量可见的条数）。这样一条缺分量的请求
    不会把同桶其它完整请求排除出完整总量，部分请求也不会被丢弃或补零。
    """
    return {"requests": 0, "complete_requests": 0, "partial_requests": 0,
            "complete_input": 0, "complete_output": 0,
            "partial_input": 0, "partial_output": 0,
            "partial_input_known": 0, "partial_output_known": 0}


def _accumulate_event(acc, ev):
    """一条规范化事件计入桶（完整/部分拆分口径见 A12-1 节端点契约）。"""
    acc["requests"] += 1
    has_input = ev.input_tokens is not None
    has_output = ev.output_tokens is not None
    if has_input and has_output:
        acc["complete_requests"] += 1
        acc["complete_input"] += ev.input_tokens
        acc["complete_output"] += ev.output_tokens
        return
    acc["partial_requests"] += 1
    if has_input:
        acc["partial_input"] += ev.input_tokens
        acc["partial_input_known"] += 1
    if has_output:
        acc["partial_output"] += ev.output_tokens
        acc["partial_output_known"] += 1


def _token_row(day, source, model, acc):
    """桶 → days[] 行（键与语义见 A12-1 节端点契约，不得改名或改义）。

    input_tokens/output_tokens 只来自「两分量都齐全」的请求，无此类请求时 null；
    partial_* 只来自缺分量的请求（绝不混入完整总量、绝不补零），完全无该分量
    时为 null（真实 0 是已知值，不是缺失）。complete ⇔ partial_requests == 0。
    """
    return {"date": day, "source": source, "model": model,
             "input_tokens": acc["complete_input"]
             if acc["complete_requests"] else None,
             "output_tokens": acc["complete_output"]
             if acc["complete_requests"] else None,
             "complete": acc["partial_requests"] == 0,
             "complete_requests": acc["complete_requests"],
             "partial_requests": acc["partial_requests"],
             "partial_input_tokens": acc["partial_input"]
             if acc["partial_input_known"] else None,
             "partial_output_tokens": acc["partial_output"]
             if acc["partial_output_known"] else None}


def _missing_row(day, source, model, reason, acc):
    """桶 → missing[] 行：带该桶「部分请求」可展示的分量数字，便于
    「仅输出可见」直接显示（acc=None 表示无分量可归，如 invalid_time 行）。

    这里 input_tokens/output_tokens 是该桶缺分量请求中该分量的合计（该分量在
    部分请求里完全不可见时为 null）——与 days[] 行不同名同义，故只出现在本
    列表；完整总量一律看 days[] 行的 input_tokens/output_tokens。
    """
    if acc is None:
        input_tokens = output_tokens = None
    else:
        input_tokens = acc["partial_input"] \
            if acc["partial_input_known"] else None
        output_tokens = acc["partial_output"] \
            if acc["partial_output_known"] else None
    return {"date": day, "source": source, "model": model, "reason": reason,
            "input_tokens": input_tokens, "output_tokens": output_tokens}


def _scan_sources(ledgers):
    """读取全部来源 → (per_source, coverage_sources)。

    per_source[source] = {"events": [...], "spec": ..., "events_trusted": bool}
    未知/无账本来源只给覆盖说明，不产出任何行。events_trusted=False 表示本次
    读取失败或结构损坏（读取层可能退回上次缓存的事件）：两个端点都必须整源排除
    该来源的行与计数（见 _events_trusted），不得把旧缓存当本次读数。
    """
    per_source, coverage_sources = {}, {}
    for source in sorted(ledgers):
        handle = ledgers[source]
        spec = _SOURCE_SPECS.get(source)
        if spec is None or spec.get("kind") is None:
            coverage_sources[source] = _unsupported_coverage(handle)
            per_source[source] = {"events": [], "spec": None,
                                 "events_trusted": True}
            continue
        path = handle if isinstance(handle, (str, os.PathLike)) else None
        events, state, diag = _load_source_events(source, handle, spec)
        coverage_sources[source] = _source_coverage(
            source, spec, path, events, state, diag)
        per_source[source] = {
            "events": events, "spec": spec,
            "events_trusted": _events_trusted(coverage_sources[source])}
    return per_source, coverage_sources


def usage_tokens(ledgers=None, days=14, now=None):
    """近 N 个北京自然日（默认 14，上限 31）的按来源+模型 Token 分量。

    返回端点契约载荷（见 A12-1 节端点契约）。days[] 的 input_tokens/
    output_tokens 只统计「输入与输出都齐全」的请求（F1 修复点：一条缺分量的
    请求不得把同桶其它完整请求排除）；缺任一分量的请求另列进 missing，并带上
    该桶部分请求可展示的分量（partial_* / missing[] 的 input_tokens/
    output_tokens），绝不补零、绝不混入完整总量。identity 键 = source + model，
    同名跨来源不混并。无效时间戳的记录进 missing（date=null，
    reason="invalid_time"），不丢弃。days[] 与 missing[] 各自按
    MAX_ROWS_PER_ENDPOINT 截断，丢弃条数在 coverage 里如实标注。
    本次读取失败（read_error）/结构损坏（corrupt_ledger）的来源整源排除：
    days[]/missing[] 不出现其行（退回的旧缓存不是本次读数，不折 0），错误由
    coverage errors 如实披露；malformed_lines 不影响（其余事件仍可计数）。
    """
    if ledgers is None:
        ledgers = default_sources()
    window_days, clamped = _clamp_int(days, 14, 1, MAX_TOKEN_RANGE_DAYS)
    now_ms = _now_ms(now)
    day_keys = _window_day_keys(window_days, now_ms)
    window = set(day_keys)

    buckets, undated, scanned = {}, {}, 0
    per_source, coverage_sources = _scan_sources(ledgers)
    for source, entry in per_source.items():
        if not entry["events_trusted"]:
            # 本次读取失败/结构损坏：事件集不可信（可能只是上次缓存的旧事件），
            # 整源排除——不出 days[]/missing[] 行、不计入 scanned；错误已在
            # coverage.sources[source].errors 如实披露，不静默、不折 0。
            continue
        for ev in entry["events"]:
            if ev.ms is None:
                # 无法归桶的记录不丢弃：进 missing（coverage 另计条数）
                undated.setdefault((source, ev.model), True)
                continue
            day = _environment().beijing_day(ev.ms)
            if day not in window:
                continue
            scanned += 1
            _accumulate_event(
                buckets.setdefault((day, source, ev.model), _empty_bucket()), ev)

    rows, missing = [], []
    for (day, source, model) in sorted(buckets):
        acc = buckets[(day, source, model)]
        rows.append(_token_row(day, source, model, acc))
        if acc["partial_requests"]:
            missing.append(_missing_row(day, source, model,
                                      _missing_reason(acc), acc))
    for (source, model) in sorted(undated):
        # 时间戳无效：无法证明落在窗口内，分量一律 null（不臆测、不归并）
        missing.append(_missing_row(None, source, model, "invalid_time", None))

    rows.sort(key=lambda r: (r["date"], r["source"], r["model"]))
    missing.sort(key=lambda m: (m["date"] or "", m["source"], m["model"],
                               m["reason"]))
    # 两个列表各自按同一行数上限截断（保留排序尾部=最新日期，丢弃头部），
    # 丢弃条数分别标注：dropped_rows / dropped_missing_rows。
    dropped_rows = 0
    truncated = clamped
    if len(rows) > MAX_ROWS_PER_ENDPOINT:
        dropped_rows = len(rows) - MAX_ROWS_PER_ENDPOINT
        rows = rows[-MAX_ROWS_PER_ENDPOINT:]
        truncated = True
    dropped_missing_rows = 0
    if len(missing) > MAX_ROWS_PER_ENDPOINT:
        dropped_missing_rows = len(missing) - MAX_ROWS_PER_ENDPOINT
        missing = missing[-MAX_ROWS_PER_ENDPOINT:]
        truncated = True
    complete_rows = sum(1 for r in rows if r["complete"])
    return {
        "days": rows,
        "missing": missing,
        "coverage": {
            "days": window_days,
            "complete_rows": complete_rows,
            "partial_rows": len(rows) - complete_rows,
            "scanned": scanned,
            "truncated": truncated,
            "clamped": clamped,
            "dropped_rows": dropped_rows,
            "dropped_missing_rows": dropped_missing_rows,
            "sources": coverage_sources,
        },
        "timezone": TIMEZONE_NAME,
        "checked_epoch": now_ms // 1000,
    }


def usage_rates(ledgers=None, hours=24, now=None):
    """近 N 个北京时间整点（默认 24，上限 168）的整次平均输出速率。

    返回端点契约载荷（见本节顶部）。rate = 输出 Token ÷ 整次耗时（含首字
    等待），分母只含耗时为正且输出 Token 齐全的记录；缺输出 Token、缺耗时、
    耗时非正值的记录按原因计数单列，rate 为 null 表示无法计算，绝不填 0。
    本次读取失败（read_error）/结构损坏（corrupt_ledger）的来源整源排除：
    hours[] 不出现其行、也不计入任何 skipped 原因（退回的旧缓存不是本次
    读数，不折 0），错误由 coverage errors 如实披露；malformed_lines 不影响。
    """
    if ledgers is None:
        ledgers = default_sources()
    window_hours, clamped = _clamp_int(hours, 24, 1, MAX_RATE_RANGE_HOURS)
    now_ms = _now_ms(now)
    window_start = _window_hour_starts(window_hours, now_ms)[0]
    window_end = window_start + window_hours * 3_600_000  # 含当前整点内已发生的记录

    buckets = {}
    skipped = {reason: 0 for reason in _SKIPPED_REASONS}
    per_source, coverage_sources = _scan_sources(ledgers)
    for source, entry in per_source.items():
        if not entry["events_trusted"]:
            # 本次读取失败/结构损坏：事件集不可信（可能只是上次缓存的旧事件），
            # 整源排除——不出 hours[] 行、不计数任何 skipped 原因（旧缓存不是
            # 本次读数）；错误已在 coverage.sources[source].errors 如实披露。
            continue
        for ev in entry["events"]:
            if ev.ms is None:
                # 无时间戳的记录无法归桶：不丢弃，计数单列（本端点无 missing 列表）
                skipped["invalid_time"] += 1
                continue
            if ev.ms < window_start or ev.ms >= window_end:
                continue  # 窗口外记录不参与，也不计为跳过
            # 原因计数相互独立（见本节「计数语义」）：同一条记录可同时缺输出
            # Token 又缺/坏耗时，两处都要计；互斥的只有耗时的两种状态。
            if ev.output_tokens is None:
                skipped["missing_output_tokens"] += 1
            if ev.duration_state == "missing":
                skipped["missing_duration"] += 1
            elif ev.duration_state == "invalid":
                skipped["invalid_duration"] += 1
            if ev.output_tokens is None or ev.duration_state != "ok":
                continue  # 缺输出或耗时不可用：不进分母，也不出行
            hour = (ev.ms // 3_600_000) * 3_600_000
            acc = buckets.setdefault((hour, source, ev.model), {
                "output_tokens": 0, "duration_ms": 0, "requests": 0})
            acc["output_tokens"] += ev.output_tokens
            acc["duration_ms"] += ev.duration_ms
            acc["requests"] += 1

    rows = []
    for (hour, source, model) in sorted(buckets):
        acc = buckets[(hour, source, model)]
        if acc["duration_ms"] > 0:
            rate = round(acc["output_tokens"] / (acc["duration_ms"] / 1000.0), 6)
        else:
            rate = None  # 无有效耗时：无法计算，绝不填 0
        rows.append({"hour": _hour_label(hour), "source": source,
                     "model": model,
                     "output_tokens": acc["output_tokens"],
                     "duration_ms": acc["duration_ms"],
                     "requests": acc["requests"], "rate": rate})

    dropped_rows = 0
    truncated = clamped
    if len(rows) > MAX_ROWS_PER_ENDPOINT:
        dropped_rows = len(rows) - MAX_ROWS_PER_ENDPOINT
        rows = rows[-MAX_ROWS_PER_ENDPOINT:]
        truncated = True
    return {
        "hours": rows,
        "skipped": skipped,
        "coverage": {
            "hours": window_hours,
            "rows": len(rows),
            "truncated": truncated,
            "clamped": clamped,
            "dropped_rows": dropped_rows,
            "sources": coverage_sources,
        },
        "timezone": TIMEZONE_NAME,
        "checked_epoch": now_ms // 1000,
    }


# =====================================================================
# B5 UM read surface (UM's own chat2api ledger)
# =====================================================================
#
# 契约（B5；前端 r3 的 UmToday/UmCost/UmTrend/UmRequestPage 由 B6 映射）：
#
#   um_today(ledger, now) ->
#     {today, cost, coverage, timezone, checked_epoch}
#   um_trend(ledger, range, now) ->
#     {trend, coverage, timezone, checked_epoch}
#   um_requests(ledger, window, model, result, search, cursor, limit, now) ->
#     {page, models, coverage, timezone, checked_epoch}
#
# 口径（全部只读 UM 自己的 chat2api 账本；不混入 dsh，
# 也不读云端分享账本；不新增第二套记账服务）：
#   - 今日/趋势按北京时间自然日 [00:00, 次日 00:00)；近 24 小时是滚动窗口。
#   - 请求数 = 逻辑请求数：chat2api 的物理重试若单独写了一条
#     status="upstream_transient_retried" / "degenerate_repetition_retried"
#     记录，它不是一次新请求，不重复计数；它只作为该逻辑请求的 retries 证据
#     （有可靠 request_id 时）与 token/费用合计的一部分（它记录的是被丢弃
#     尝试的真实消耗，不包含在最终记录的累计值里）。
#   - Token 合计 = 输入 + 输出；缓存读取已含在输入、思考 Token 已含在输出，
#     绝不再次相加。0 是已知值；缺失/未报告/估算/未知分别用 Reading 的
#     kind 表达，不折成 0。
#   - 费用 = 参考：复用 B1 的 console_magpie_prices.compute_reference
#     （Magpie 有效价快照 + 汇率版本），不是 UM 实际扣款；未同步/未定价/
#     不完整覆盖一律如实披露，绝不当作 0。
#   - 速度 = 输出 Token ÷ 整次耗时（含首字等待），只由逻辑请求中有真实输出
#     Token 与正耗时的记录聚合；无法确认就 missing，绝不推算首字。
#   - 日志沿 console_log_reader 的有界尾读；只有 32 位小写十六进制
#     request_id（chat2api 已验证归因格式）才关联，12 位内部短 ID 不关联。
#   - 缺数据/损坏/拒绝/读取失败与合法空列表由 coverage.status 区分。

UM_LEDGER_SOURCE = "chat2api"
UM_WINDOWS = ("today", "24h", "7d", "30d")
UM_TREND_RANGES = ("7d", "14d", "30d")
UM_RESULTS = ("success", "failed", "truncated", "client_disconnected",
               "cancelled", "unknown")
UM_RETRY_STATUSES = frozenset({
    "upstream_transient_retried", "degenerate_repetition_retried"})
UM_TRUNCATED_STATUSES = frozenset({
    "upstream_missing_finish_reason", "upstream_eof_without_done",
    "aggregate_upstream_missing_terminal"})
UM_CANCELLED_STATUSES = frozenset({"cancelled", "canceled"})
UM_CLIENT_DISCONNECTED_STATUSES = frozenset({"client_disconnected"})
UM_MAX_PAGE = 100
UM_MAX_SEARCH_CHARS = 120
UM_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
UM_READING_KINDS = ("exact", "atLeast", "estimate", "unreported",
                    "unknown", "missing")

# 账本 status 的展示说明（不把任何未知值猜成成功/失败；只截断展示）。
_UM_STATUS_NOTES = {
    "upstream_unavailable": "上游不可用",
    "reauthentication_required": "上游认证失效",
    "share_dispatch_timeout": "分享调度超时",
    "share_dispatch_revoked": "分享调度已撤销",
    "share_dispatch_state_unavailable": "分享调度状态不可用",
    "request_deadline_exceeded": "请求超过截止时间",
    "concurrency_limit": "并发上限",
    "unexpected_upstream_response": "上游响应不符合预期",
    "upstream_stream_error": "上游流失败",
    "upstream_read_error": "上游读取失败",
    "upstream_prelude_overflow": "上游预读超出上限",
    "upstream_first_byte_timeout": "上游首字节超时",
    "upstream_explicit_error": "上游明确报错",
    "upstream_bad_frame": "上游返回无效帧",
    "upstream_missing_terminal": "上游流缺少完整终止",
    "upstream_error": "上游错误",
    "degenerate_repetition": "上游重复输出",
}


def _um_reading(kind, value=None, note=None):
    """One honest reading: exact / atLeast / estimate / unreported /
    unknown / missing. A real zero is only ever ``exact`` with value 0."""
    reading = {"kind": kind}
    if value is not None:
        reading["value"] = value
    if note:
        reading["note"] = note
    return reading


def _um_exact(value):
    return _um_reading("exact", value)


def _um_value(reading):
    """Numeric part of a reading, or None when the reading has no number."""
    if not isinstance(reading, dict):
        return None
    if reading.get("kind") not in ("exact", "atLeast", "estimate"):
        return None
    value = reading.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _um_sum_readings(readings):
    """Sum readings without turning missing/unknown into zero.

    Any unknown/missing/atLeast contribution makes the sum a lower bound;
    an estimate-only sum stays an estimate; all exact stays exact. An empty
    list is the legal zero (no records) -> exact 0.
    """
    readings = [r for r in readings if isinstance(r, dict)]
    if not readings:
        return _um_exact(0)
    notes = []
    for reading in readings:
        note = reading.get("note")
        if isinstance(note, str) and note and note not in notes:
            notes.append(note)
    note_text = "；".join(notes)[:240]
    values = [r for r in readings
              if r.get("kind") in ("exact", "atLeast", "estimate")]
    if not values:
        if any(r.get("kind") == "unknown" for r in readings):
            return _um_unknown(note_text or "部分记录没有可用数值")
        if any(r.get("kind") == "unreported" for r in readings):
            return _um_reading("unreported",
                               note=note_text or "没有报告该数值")
        return _um_reading("missing",
                           note=note_text or "没有该数值")
    # Keep the numeric type: token counts are ints, money readings are
    # floats. int() would truncate a CNY reading such as 0.0422 to 0.
    total = sum(r.get("value") or 0 for r in values)
    if any(r.get("kind") in ("atLeast", "unknown", "missing",
                             "unreported") for r in readings):
        return _um_reading("atLeast", total,
                           note_text or "部分记录缺少数值，合计为下界")
    if any(r.get("kind") == "estimate" for r in readings):
        return _um_reading("estimate", total,
                           note_text or "含估算值")
    return _um_exact(total)


def _um_unknown(note):
    return _um_reading("unknown", note=note)


def _um_lower_bound(reading, note):
    """A damaged/truncated ledger makes every exact sum a lower bound."""
    if not isinstance(reading, dict):
        return reading
    if reading.get("kind") in ("exact", "estimate"):
        return _um_reading("atLeast", reading.get("value", 0), note)
    return reading


def _um_sample_reading(reading):
    """A damaged/truncated ledger makes an exact sample average an estimate."""
    if isinstance(reading, dict) and reading.get("kind") == "exact":
        return _um_reading(
            "estimate", reading.get("value", 0),
            "账本不完整：按已读样本的整次平均")
    return reading


def _um_mark_partial(today, cost):
    note = "账本有损坏行/截断，合计为下界"
    for key in ("total", "input", "output"):
        today["tokens"][key] = _um_lower_bound(
            today["tokens"][key], note)
    today["cacheRead"]["tokens"] = _um_lower_bound(
        today["cacheRead"]["tokens"], note)
    today["reasoning"] = _um_lower_bound(today["reasoning"], note)
    for key in ("recorded", "failed"):
        today["requests"][key] = _um_lower_bound(
            today["requests"][key], note)
    today["speed"]["value"] = _um_sample_reading(today["speed"]["value"])
    cost["cny"] = _um_lower_bound(cost["cny"], note)
    cost["cnyPartial"] = True


def _um_mark_trend_partial(trend):
    note = "账本有损坏行/截断，合计为下界"
    buckets = list(trend["all"]["daily"]) + [trend["all"]["total"]]
    for row in trend["models"]:
        buckets.extend(row["daily"])
        buckets.append(row["total"])
        row["failed"] = _um_lower_bound(row["failed"], note)
    for bucket in buckets:
        bucket["tokens"] = _um_lower_bound(bucket["tokens"], note)
        bucket["costCny"] = _um_lower_bound(bucket["costCny"], note)
        bucket["requests"] = _um_lower_bound(bucket["requests"], note)
        bucket["speed"] = _um_sample_reading(bucket["speed"])


def _um_is_retry(event):
    status = event.status
    return isinstance(status, str) and status.strip().lower() \
        in UM_RETRY_STATUSES


def _um_http_status(status):
    """Ledger status -> HTTP code only when the text proves one; else None."""
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    if isinstance(status, str):
        match = re.match(r"^HTTP\s*(\d{3})$", status.strip(),
                         re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _um_status_note(status):
    text = status.strip()
    note = _UM_STATUS_NOTES.get(text.lower())
    if note:
        return note
    if text.lower().startswith("aggregate_"):
        return "上游流失败（%s）" % text[:64]
    return "账本状态：%s" % text[:64]


def _um_result(event):
    """Ledger row -> (RequestResult, resultNote, httpStatus)."""
    status = event.status
    if not isinstance(status, str) or not status.strip():
        return "unknown", "账本没有记录完成状态", None
    text = status.strip()
    lower = text.lower()
    if lower in ("ok", "success"):
        return "success", "HTTP 200，正常完成", 200
    if lower in UM_RETRY_STATUSES:
        # Physical retry evidence, never a separate logical request.
        return "unknown", "物理重试记录，不是独立请求", None
    if lower in UM_TRUNCATED_STATUSES:
        return "truncated", "HTTP 200，但回复在完成前截断，按失败计", 200
    if lower in UM_CANCELLED_STATUSES:
        return "cancelled", "收到明确的取消请求后停止", None
    if lower in UM_CLIENT_DISCONNECTED_STATUSES:
        return "client_disconnected", \
            "客户端在回复完成前断开连接；不推断为主动取消", None
    http_status = _um_http_status(text)
    if http_status is not None and 200 <= http_status < 300:
        return "success", "HTTP %d，正常完成" % http_status, http_status
    return "failed", _um_status_note(text), http_status


def _um_request_id(value):
    """Only the producer's verified 32-lowercase-hex id is reliable."""
    if isinstance(value, str) and UM_REQUEST_ID_RE.match(value):
        return value
    return None


def _um_event_token_reading(event, side):
    value = event.input_tokens if side == "input" else event.output_tokens
    if event.usage_source == "none":
        return _um_unknown("上游没有报告 %s Token（写端记为 0 表示未知）" % side)
    if value is None:
        return _um_unknown("旧记录缺少 %s Token 字段" % side)
    if event.estimated or event.usage_source == "estimated":
        return _um_reading("estimate", value, "本地估算，不是上游报告值")
    if event.usage_source == "partial":
        return _um_reading("atLeast", value, "上游只报告了一侧，另一侧为本地估算")
    return _um_exact(value)


def _um_event_total_reading(event):
    return _um_sum_readings([
        _um_event_token_reading(event, "input"),
        _um_event_token_reading(event, "output")])


def _um_event_cache_reading(event):
    if event.cache_report_status == "reported":
        if event.cache_tokens is None:
            return _um_unknown("缓存报告状态为 reported 但缓存字段缺失")
        return _um_exact(event.cache_tokens)
    if event.cache_report_status == "unreported":
        return _um_reading("unreported", note="上游没有报告缓存")
    return _um_unknown("旧记录没有缓存报告状态（Q11 unknown）")


def _um_event_reasoning_reading(event):
    if event.reasoning_tokens is None:
        return _um_unknown("旧记录缺少思考 Token 字段；已计入输出，不重复相加")
    if event.reasoning_tokens > 0:
        return _um_reading("exact", event.reasoning_tokens,
                            "已计入输出，不重复相加")
    # The writer folds a missing reasoning report to 0, so a zero is not
    # provably a measured zero; it stays unreported (never a fabricated 0).
    return _um_reading(
        "unreported",
        note="上游没有报告思考 Token（写端把缺失折为 0；已计入输出）")


def _um_cache_report(events):
    if not events:
        return "unknown", "今天没有请求，缓存报告状态不适用"
    reported = sum(1 for ev in events
                   if ev.cache_report_status == "reported")
    unreported = sum(1 for ev in events
                     if ev.cache_report_status == "unreported")
    unknown = len(events) - reported - unreported
    if reported and not unreported and not unknown:
        return "reported", "已含在输入中；每条回复都报告了缓存"
    if reported:
        return "partial", ("已含在输入中；部分回复未报告缓存"
                           "（reported=%d, unreported=%d, unknown=%d）"
                           % (reported, unreported, unknown))
    if unreported and not unknown:
        return "unreported", "已含在输入中；上游没有报告缓存"
    if unreported and unknown:
        return "partial", ("已含在输入中；部分回复未报告缓存，部分旧记录"
                           "状态未知（unreported=%d, unknown=%d）"
                           % (unreported, unknown))
    return "unknown", "已含在输入中；旧记录没有缓存报告状态（Q11 unknown）"


def _um_cache_tokens_reading(events):
    if not events:
        return _um_exact(0)
    reported = [ev for ev in events if ev.cache_report_status == "reported"]
    value = sum(ev.cache_tokens or 0 for ev in reported)
    if len(reported) == len(events):
        return _um_exact(value)
    if reported:
        return _um_reading("atLeast", value,
                           "部分记录未报告缓存，合计为下界")
    if any(ev.cache_report_status == "unreported" for ev in events):
        return _um_reading("unreported", note="上游没有报告缓存")
    return _um_unknown("旧记录没有缓存报告状态（Q11 unknown）")


def _um_requests_reading(logical):
    counts = {key: 0 for key in UM_RESULTS}
    for ev in logical:
        result, _note, _status = _um_result(ev)
        counts[result] = counts.get(result, 0) + 1
    failed = counts["failed"] + counts["truncated"]
    if counts["unknown"]:
        failed_reading = _um_reading(
            "atLeast", failed,
            "部分记录没有完成状态，失败数至少为 %d" % failed)
    else:
        failed_reading = _um_exact(failed)
    return _um_exact(len(logical)), failed_reading, counts


def _um_speed_payload(events, logical, empty_note="该时段没有请求"):
    samples = [ev for ev in logical
               if ev.output_tokens is not None and ev.duration_state == "ok"]
    missing_output = sum(1 for ev in logical if ev.output_tokens is None)
    missing_duration = sum(1 for ev in logical
                            if ev.duration_state == "missing")
    invalid_duration = sum(1 for ev in logical
                            if ev.duration_state == "invalid")
    if not logical:
        value = _um_reading("missing", note=empty_note)
    elif not samples:
        value = _um_reading("missing", note="没有可计时的请求")
    else:
        output = sum(ev.output_tokens for ev in samples)
        duration = sum(ev.duration_ms for ev in samples)
        rate = round(output / (duration / 1000.0), 6) if duration > 0 else None
        estimated = sum(1 for ev in samples
                        if ev.estimated or ev.usage_source == "estimated")
        if rate is None:
            value = _um_reading("missing", note="耗时合计非正，无法计算")
        elif len(samples) == len(logical) and not estimated:
            value = _um_exact(rate)
        else:
            value = _um_reading(
                "estimate", rate,
                "按 %d 个可计时请求的整次平均；%d 个请求缺输出/耗时，"
                "%d 个输出为估算"
                % (len(samples), len(logical) - len(samples), estimated))
    return {
        "value": value,
        "basis": "whole_request",
        "samples": len(samples),
        "coverage": {
            "samples": len(samples),
            "missing_output_tokens": missing_output,
            "missing_duration": missing_duration,
            "invalid_duration": invalid_duration,
            "requests": len(logical),
            "estimated_samples": sum(
                1 for ev in samples
                if ev.estimated or ev.usage_source == "estimated"),
            "note": "整次平均含首字等待；不是首字耗时或纯生成速度",
        },
    }


def _um_event_record(event):
    """Normalized event -> the ledger dict B1's pricing contract reads.

    ``cache_report_status`` is passed as "unreported" for historical rows that
    lack Q11 status, so the compatibility zero is never read as a measured
    cache hit. Thinking tokens are already part of output and are not added.
    ``request_started_at`` carries A2-8 start evidence so this path applies the
    same first-effective eligibility as ``usagesrv``'s raw-ledger summary.
    """
    return {
        "_ms": event.ms,
        "model": event.model,
        "input_tokens": event.input_tokens,
        "output_tokens": event.output_tokens,
        "cached_input_tokens": event.cache_tokens,
        "cache_report_status": (event.cache_report_status
                                or "unreported"),
        "reasoning_tokens": event.reasoning_tokens,
        "duration_ms": event.duration_ms,
        "status": event.status,
        "request_id": event.request_id,
        "usage_source": event.usage_source,
        "estimated": event.estimated,
        "request_started_at": event.request_started_at,
    }


def _um_price_paths(snapshot_path=None, status_path=None):
    if snapshot_path is None or status_path is None:
        environment = _environment()
        if snapshot_path is None:
            snapshot_path = environment.magpie_price_snapshot
        if status_path is None:
            status_path = environment.magpie_price_status
    return snapshot_path, status_path


def _um_price_one(record, snapshot_path, status_path):
    """Price one ledger record with B1's public compute_reference.

    The B1 module is the single UM pricing implementation; this read surface
    only maps its honest coverage into a per-row Reading. A missing module,
    snapshot or status stays explicit (never zero).
    """
    try:
        from console import magpie_prices as console_magpie_prices
    except Exception as exc:  # noqa: BLE001 - the console must still start
        return {"priced": False,
                "reason": "价格同步模块不可用（%s）" % type(exc).__name__,
                "estimated": False, "costUsd": None, "cny": None,
                "priceVersion": None, "fxRate": None, "fxVersion": None,
                "cnyPartial": True}
    if not snapshot_path:
        return {"priced": False, "reason": "价格快照路径未装配",
                "estimated": False, "costUsd": None, "cny": None,
                "priceVersion": None, "fxRate": None, "fxVersion": None,
                "cnyPartial": True}
    try:
        result = console_magpie_prices.compute_reference(
            [record], snapshot_path, status_path)
    except Exception as exc:  # noqa: BLE001 - report, never fold to zero
        return {"priced": False,
                "reason": "参考费用计算失败（%s）" % type(exc).__name__,
                "estimated": False, "costUsd": None, "cny": None,
                "priceVersion": None, "fxRate": None, "fxVersion": None,
                "cnyPartial": True}
    coverage = result.get("coverage") or {}
    priced = coverage.get("pricedRequests") == 1
    model_rows = result.get("models") or []
    model_row = model_rows[0] if model_rows and isinstance(
        model_rows[0], dict) else {}
    reason = None
    if not priced:
        if coverage.get("preEffectiveRequests"):
            reason = "请求开始于首次价格生效前，保持未定价"
        elif coverage.get("unknownEffectiveRequests"):
            reason = "请求时刻不在价格快照有效范围"
        elif coverage.get("fxMissingRequests"):
            reason = "汇率未就绪"
        elif coverage.get("unpricedRequests"):
            reason = "未定价或用量不完整"
        else:
            reason = "未定价"
    return {
        "priced": priced,
        "reason": reason,
        "estimated": bool(model_row.get("estimatedRequests")),
        "costUsd": result.get("costUsd") if priced else None,
        "cny": result.get("cny") if priced else None,
        "priceVersion": model_row.get("priceVersion"),
        "fxRate": model_row.get("fxRate"),
        "fxVersion": model_row.get("fxVersion"),
        "cnyPartial": bool(result.get("cnyPartial")),
    }


def _um_price_meta(price):
    state = price.get("state") if isinstance(price, dict) else None
    if state not in ("synced", "unsynced", "failed"):
        state = "failed"
    raw_snapshot_at = price.get("snapshotAt") \
        if isinstance(price, dict) else None
    if isinstance(raw_snapshot_at, (int, float)) \
            and not isinstance(raw_snapshot_at, bool):
        snapshot_at = int(raw_snapshot_at)
    else:
        parsed = _environment().parse_ts(raw_snapshot_at)
        snapshot_at = int(parsed) if parsed is not None else None
    return {
        "state": state,
        "source": price.get("source") if isinstance(price, dict) else None,
        "snapshotAt": snapshot_at,
        "version": price.get("version") if isinstance(price, dict) else None,
        "failure": price.get("failure") if isinstance(price, dict) else None,
    }


def _um_cost_reading(cost):
    """B1 reference result -> one Reading; never turns unsynced into zero.

    ``cost["cny"]`` is a number on B1's result and already a Reading on
    this module's cost payload; both shapes are accepted so the trend can
    reuse the same helper.
    """
    cny = cost.get("cny")
    if isinstance(cny, dict):
        return cny
    if cny is None:
        failure = (cost.get("price") or {}).get("failure")
        return _um_reading("missing", note=failure or
                           "没有可定价的请求；不是零费用")
    if cost.get("cnyPartial"):
        return _um_reading("atLeast", cny,
                           "参考下界：部分请求未定价/用量未知/缓存未报告")
    if (cost.get("coverage") or {}).get("estimatedRequests"):
        return _um_reading("estimate", cny, "按价格快照估算")
    return _um_exact(cny)


def _um_cost_model_reading(cost, model):
    rows = [row for row in cost.get("models") or []
            if isinstance(row, dict) and row.get("model") == model]
    if not rows:
        return _um_exact(0)  # no records for this model -> legal zero
    row = rows[0]
    cny = row.get("cny")
    if cny is None:
        return _um_reading("missing", note="未定价，未计入金额")
    if row.get("cnyPartial"):
        return _um_reading("atLeast", cny,
                           "部分请求未定价/用量未知，金额为下界")
    if row.get("estimatedRequests"):
        return _um_reading("estimate", cny, "按价格快照估算")
    return _um_exact(cny)


def _um_cost_unavailable(failure, records=None, events=None):
    """Honest cost shape when no B1 reference can be read.

    ``records=None`` means the ledger itself could not be read: the counts stay
    unknown, never 0. A legal empty day (records == 0) is exact 0.
    """
    events = events or []
    count = records if isinstance(records, int) and records >= 0 else None
    empty = count == 0
    return {
        "label": "参考",
        "cny": _um_exact(0) if empty else _um_reading(
            "missing", note=failure),
        "cnyPartial": not empty,
        "costUsd": 0.0 if empty else None,
        "price": {
            "state": "failed" if not empty else "unsynced",
            "source": None, "snapshotAt": None, "version": None,
            "failure": failure,
        },
        "fx": {"rate": None, "version": None},
        "unpricedRequests": 0 if empty else None,
        "estimatedRequests": 0 if empty else None,
        "coverage": {
            "records": count,
            "pricedRequests": 0 if empty else None,
            "unpricedRequests": 0 if empty else count,
            "estimatedRequests": 0 if empty else None,
            "incompleteRequests": 0 if empty else count,
            "unknownEffectiveRequests": None,
            "preEffectiveRequests": None,
            "fxMissingRequests": None,
            "costUsd": 0.0 if empty else None,
            "cny": 0.0 if empty else None,
            "cnyPartial": not empty,
            "usageUnknownRequests": sum(
                1 for ev in events if ev.usage_source == "none") or None,
            "cacheUnknownRequests": sum(
                1 for ev in events if ev.cache_report_status is None)
            or None,
            "retryRecords": sum(1 for ev in events if _um_is_retry(ev)),
            "logicalRecords": sum(
                1 for ev in events if not _um_is_retry(ev)),
            "label": "参考",
        },
        "models": [],
    }


def _um_cost_payload(events, snapshot_path, status_path):
    """UM's own ledger -> B1 reference cost + per-model coverage."""
    records = [_um_event_record(ev) for ev in events]
    usage_unknown = sum(1 for ev in events
                        if ev.usage_source == "none")
    cache_unknown = sum(1 for ev in events
                        if ev.cache_report_status is None)
    cache_unreported = sum(1 for ev in events
                            if ev.cache_report_status == "unreported")
    retry_records = sum(1 for ev in events if _um_is_retry(ev))
    logical_records = len(events) - retry_records
    try:
        from console import magpie_prices as console_magpie_prices
    except Exception as exc:  # noqa: BLE001 - the console must still start
        return _um_cost_unavailable(
            "价格同步模块不可用（%s）" % type(exc).__name__, len(records),
            events)
    if not snapshot_path:
        return _um_cost_unavailable("价格快照路径未装配", len(records),
                                     events)
    try:
        reference = console_magpie_prices.compute_reference(
            records, snapshot_path, status_path)
    except Exception as exc:  # noqa: BLE001 - report, never fold to zero
        return _um_cost_unavailable(
            "参考费用计算失败（%s）" % type(exc).__name__, len(records),
            events)
    coverage = dict(reference.get("coverage") or {})
    coverage["usageUnknownRequests"] = usage_unknown
    coverage["cacheUnknownRequests"] = cache_unknown
    coverage["cacheUnreportedRequests"] = cache_unreported
    coverage["retryRecords"] = retry_records
    coverage["logicalRecords"] = logical_records
    coverage["label"] = "参考"
    price = reference.get("price") or {}
    cny = reference.get("cny")
    if not events:
        # Legal empty day: no requests to price -> exact 0, regardless of
        # whether a snapshot exists (the zero is about usage, not price).
        cny_reading = _um_exact(0)
        cny_partial = False
    elif cny is None:
        cny_reading = _um_reading(
            "missing", note=price.get("failure") or
            "没有可定价的请求；不是零费用")
        cny_partial = True
    elif reference.get("cnyPartial") or usage_unknown or cache_unknown \
            or cache_unreported:
        cny_reading = _um_reading(
            "atLeast", cny,
            "参考下界：部分请求未定价/用量未知/缓存未报告")
        cny_partial = True
    elif coverage.get("estimatedRequests"):
        cny_reading = _um_reading("estimate", cny,
                                   "按价格快照估算")
        cny_partial = False
    else:
        cny_reading = _um_exact(cny)
        cny_partial = False
    return {
        "label": "参考",
        "cny": cny_reading,
        "cnyPartial": cny_partial,
        "costUsd": reference.get("costUsd"),
        "price": _um_price_meta(price),
        "fx": {
            "rate": (reference.get("fx") or {}).get("rate"),
            "version": (reference.get("fx") or {}).get("version"),
        },
        "unpricedRequests": coverage.get("unpricedRequests"),
        "estimatedRequests": coverage.get("estimatedRequests"),
        "coverage": coverage,
        "models": reference.get("models") or [],
    }


def _um_read(ledger=None):
    """Read UM's own chat2api ledger through the shared bounded reader."""
    path = _environment().chat2api_log if ledger is None else ledger
    if not isinstance(path, (str, os.PathLike)):
        raise TypeError("UM ledger must be a path")
    spec = _SOURCE_SPECS[UM_LEDGER_SOURCE]
    events, state, diag = _load_source_events(
        UM_LEDGER_SOURCE, path, spec)
    coverage = _source_coverage(
        UM_LEDGER_SOURCE, spec, path, events, state, diag)
    return {
        "events": events,
        "state": state,
        "diag": diag or {},
        "coverage": coverage,
        "trusted": state is not None and _events_trusted(coverage),
    }


def _um_ledger_status(read):
    if read["state"] is None:
        if read["diag"].get("read_error"):
            return "unavailable", "账本读取失败：%s" % read["diag"][
                "read_error"]
        return "missing", "账本文件缺席：本来源不出行，不以 0 冒充无用量"
    if not read["trusted"]:
        return "unavailable", \
            "账本读取失败或结构损坏：旧缓存不是本次读数"
    if read["coverage"].get("errors"):
        return "partial", "账本有坏行/坏记录，可解析部分照常保留"
    if read["coverage"].get("truncated"):
        return "partial", "账本超出有界读取上限，保留部分合计为下界"
    return "ok", None


def _um_window_bounds(window, now_ms):
    if window not in UM_WINDOWS:
        raise ValueError(
            "invalid window %r: expected one of %s"
            % (window, ", ".join(UM_WINDOWS)))
    if window == "today":
        day = _window_day_keys(1, now_ms)[0]
        start = int(_environment().day_start_ms(day))
        return {"start": start, "end": start + 86_400_000,
                "label": "今天"}
    if window == "24h":
        return {"start": int(now_ms - 86_400_000), "end": int(now_ms) + 1,
                "label": "近 24 小时"}
    days = 7 if window == "7d" else 30
    start = int(_environment().day_start_ms(
        _window_day_keys(days, now_ms)[0]))
    end = int(_environment().day_start_ms(
        _window_day_keys(1, now_ms)[0])) + 86_400_000
    return {"start": start, "end": end, "label": "近 %d 天" % days}


def _um_iso(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=BEIJING_TZ).isoformat()


def _um_window_events(read, start, end):
    events = [ev for ev in read["events"]
              if ev.ms is not None and start <= ev.ms < end]
    undated = sum(1 for ev in read["events"] if ev.ms is None)
    return events, undated


def _um_retry_map(events):
    retries = {}
    for ev in events:
        if not _um_is_retry(ev):
            continue
        request_id = _um_request_id(ev.request_id)
        if request_id is not None:
            retries.setdefault(request_id, []).append(ev)
    return retries


def _um_retry_note(event):
    status = event.status if isinstance(event.status, str) else "unknown"
    return "物理重试记录：%s；本次记录是估算下界" % status[:64]


def _um_row_digest(event):
    payload = {
        "time": event.ms,
        "model": event.model,
        "status": event.status,
        "input": event.input_tokens,
        "output": event.output_tokens,
        "cache": event.cache_tokens,
        "cache_report_status": event.cache_report_status,
        "reasoning": event.reasoning_tokens,
        "duration": event.duration_ms,
        "request_id": event.request_id,
        "usage_source": event.usage_source,
        "estimated": event.estimated,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True)
        .encode("utf-8")).hexdigest()[:16]


def _um_occurrence_keys(events):
    counts, keys = {}, {}
    for ev in events:
        digest = _um_row_digest(ev)
        occurrence = counts.get(digest, 0)
        counts[digest] = occurrence + 1
        keys[id(ev)] = "chat2api:%s:%d" % (digest, occurrence)
    return keys


def _um_price_reading(price):
    if price is None:
        return _um_reading("missing", note="价格同步不可用，未计价")
    if not price.get("priced"):
        return _um_reading("missing",
                            note=price.get("reason") or "未定价")
    if price.get("cny") is None:
        return _um_reading("missing", note="汇率未就绪，金额无法折算")
    if price.get("cnyPartial"):
        return _um_reading("atLeast", price["cny"],
                            "参考下界：未定价/用量未知/缓存未报告")
    if price.get("estimated"):
        return _um_reading("estimate", price["cny"], "按价格快照估算")
    return _um_exact(price["cny"])


def _um_request_row(event, key, retries, price, retry_prices=None):
    result, result_note, http_status = _um_result(event)
    events = [event] + list(retries or [])
    input_reading = _um_sum_readings(
        [_um_event_token_reading(ev, "input") for ev in events])
    output_reading = _um_sum_readings(
        [_um_event_token_reading(ev, "output") for ev in events])
    cache_reading = _um_sum_readings(
        [_um_event_cache_reading(ev) for ev in events])
    reasoning_reading = _um_sum_readings(
        [_um_event_reasoning_reading(ev) for ev in events])
    cost_reading = _um_sum_readings(
        [_um_price_reading(price)]
        + [_um_price_reading(item) for item in retry_prices or []])
    if len(events) > 1 and cost_reading.get("kind") in ("exact", "estimate"):
        cost_reading = _um_reading(
            cost_reading["kind"], cost_reading.get("value", 0),
            "参考：含物理重试记录的估算")
    if event.duration_state == "ok":
        duration_reading = _um_exact(event.duration_ms)
    elif event.duration_state == "invalid":
        duration_reading = _um_unknown("耗时字段非正或不可解析")
    else:
        duration_reading = _um_unknown("账本没有记录耗时")
    if (event.output_tokens is not None
            and event.duration_state == "ok"):
        rate = round(event.output_tokens / (event.duration_ms / 1000.0), 6)
        speed_reading = (_um_reading("estimate", rate,
                                     "输出为本地估算")
                          if event.estimated else _um_exact(rate))
    else:
        speed_reading = _um_reading(
            "missing", note="缺输出 Token 或正耗时，无法计算")
    request_id = _um_request_id(event.request_id)
    retry_rows = None if request_id is None else [
        {"at": int(ev.ms) if ev.ms is not None else None,
         "note": _um_retry_note(ev)} for ev in retries or []]
    error = None
    if result in ("failed", "truncated", "client_disconnected",
                  "cancelled"):
        error = {"code": (event.status or "unknown")[:64],
                 "message": result_note[:160]}
    return {
        "key": key,
        "requestId": request_id,
        "at": int(event.ms) if event.ms is not None else None,
        "model": event.model,
        "result": result,
        "resultNote": result_note,
        "httpStatus": http_status,
        "input": input_reading,
        "output": output_reading,
        "cacheRead": cache_reading,
        "reasoning": reasoning_reading,
        "costCny": cost_reading,
        "durationMs": duration_reading,
        "firstTokenMs": _um_reading(
            "missing", note="账本没有记录首字耗时，不推算"),
        "speed": speed_reading,
        "speedBasis": "whole_request",
        "retries": retry_rows,
        "error": error,
        "status": event.status if isinstance(event.status, str) else None,
    }


def _um_empty_today(read, status, note, now_ms):
    """No events in the window: exact zero only when the ledger is complete."""
    day = _window_day_keys(1, now_ms)[0]
    start = int(_environment().day_start_ms(day))
    boundaries = {
        "timezone": TIMEZONE_NAME,
        "start": _um_iso(start),
        "end": _um_iso(start + 86_400_000),
        "half_open": True,
    }
    if status == "ok":
        reading = _um_exact(0)
        today = {
            "day": day,
            "tokens": {"total": reading, "input": reading,
                        "output": reading},
            "cacheRead": {"tokens": reading, "report": "unknown",
                           "detail": "今天没有请求，缓存报告状态不适用"},
            "reasoning": reading,
            "requests": {"recorded": reading, "failed": reading},
            "speed": {"value": _um_reading("missing",
                                            note="今天没有请求"),
                       "basis": "whole_request", "samples": 0},
            "boundaries": boundaries,
        }
        cost = _um_cost_payload([], *_um_price_paths())
    elif status == "partial":
        reading = _um_reading("atLeast", 0, "账本不完整，合计为下界")
        today = {
            "day": day,
            "tokens": {"total": reading, "input": reading,
                        "output": reading},
            "cacheRead": {"tokens": reading, "report": "unknown",
                           "detail": "账本不完整，缓存报告状态未知"},
            "reasoning": reading,
            "requests": {"recorded": reading, "failed": reading},
            "speed": {"value": _um_reading("missing",
                                            note="账本不完整，无法计算"),
                       "basis": "whole_request", "samples": 0},
            "boundaries": boundaries,
        }
        cost = _um_cost_payload([], *_um_price_paths())
        cost["cny"] = _um_reading("atLeast", 0,
                                    "账本不完整，金额为下界")
        cost["cnyPartial"] = True
    else:
        missing = _um_reading("missing", note=note)
        today = {
            "day": day,
            "tokens": {"total": missing, "input": missing,
                        "output": missing},
            "cacheRead": {"tokens": missing, "report": "unknown",
                           "detail": note},
            "reasoning": missing,
            "requests": {"recorded": missing, "failed": missing},
            "speed": {"value": missing, "basis": "whole_request",
                       "samples": None},
            "boundaries": boundaries,
        }
        cost = _um_cost_unavailable(note, records=None)
    return today, cost


def um_today(ledger=None, now=None, snapshot_path=None, status_path=None):
    """Today's five readings + reference cost from UM's own ledger.

    The window is the Beijing natural day [00:00, next 00:00). Request counts
    are logical requests (a physical retry row is not counted twice); token and
    cost totals include the retry rows' recorded consumption, because those are
    real consumption not included in the final row's cumulative values. Cache
    reads are part of input and thinking tokens part of output; neither is added
    again. A missing ledger or a failed/corrupt read is missing, never zero.
    """
    read = _um_read(ledger)
    now_ms = _now_ms(now)
    snapshot_path, status_path = _um_price_paths(
        snapshot_path, status_path)
    status, note = _um_ledger_status(read)
    if status in ("missing", "unavailable", "partial"):
        if status == "partial":
            # A partial ledger still has usable events: fall through and mark
            # every sum as a lower bound.
            pass
        else:
            today, cost = _um_empty_today(read, status, note, now_ms)
            return {
                "today": today,
                "cost": cost,
                "coverage": {
                    "status": status,
                    "note": note,
                    "ledger_present": bool(
                        read["coverage"].get("ledger_present")),
                    "events": len(read["events"]),
                    "errors": read["coverage"].get("errors") or [],
                    "truncated": bool(
                        read["coverage"].get("truncated")),
                },
                "timezone": TIMEZONE_NAME,
                "checked_epoch": now_ms // 1000,
            }
    day = _window_day_keys(1, now_ms)[0]
    start = int(_environment().day_start_ms(day))
    events, undated = _um_window_events(read, start, start + 86_400_000)
    if not events and status == "partial":
        today, cost = _um_empty_today(read, status, note, now_ms)
        return {
            "today": today,
            "cost": cost,
            "coverage": {
                "status": status,
                "note": note,
                "ledger_present": bool(
                    read["coverage"].get("ledger_present")),
                "events": len(read["events"]),
                "errors": read["coverage"].get("errors") or [],
                "truncated": bool(read["coverage"].get("truncated")),
            },
            "timezone": TIMEZONE_NAME,
            "checked_epoch": now_ms // 1000,
        }
    logical = [ev for ev in events if not _um_is_retry(ev)]
    retries = [ev for ev in events if _um_is_retry(ev)]
    input_reading = _um_sum_readings(
        [_um_event_token_reading(ev, "input") for ev in events])
    output_reading = _um_sum_readings(
        [_um_event_token_reading(ev, "output") for ev in events])
    total_reading = _um_sum_readings([input_reading, output_reading])
    cache_reading = _um_cache_tokens_reading(events)
    cache_report, cache_detail = _um_cache_report(events)
    reasoning_reading = _um_sum_readings(
        [_um_event_reasoning_reading(ev) for ev in events])
    recorded, failed, result_counts = _um_requests_reading(logical)
    speed = _um_speed_payload(events, logical, "今天没有请求")
    cost = _um_cost_payload(events, snapshot_path, status_path)
    if status == "partial" and cost["cny"].get("kind") == "exact":
        cost["cny"] = _um_reading(
            "atLeast", cost["cny"].get("value", 0),
            "账本不完整，金额为下界")
        cost["cnyPartial"] = True
    boundaries = {
        "timezone": TIMEZONE_NAME,
        "start": _um_iso(start),
        "end": _um_iso(start + 86_400_000),
        "half_open": True,
    }
    today = {
        "day": day,
        "tokens": {"total": total_reading, "input": input_reading,
                    "output": output_reading},
        "cacheRead": {"tokens": cache_reading,
                       "report": cache_report,
                       "detail": cache_detail},
        "reasoning": reasoning_reading,
        "requests": {"recorded": recorded, "failed": failed},
        "speed": speed,
        "boundaries": boundaries,
        "results": result_counts,
    }
    if status == "partial":
        _um_mark_partial(today, cost)
    return {
        "today": today,
        "cost": cost,
        "coverage": {
            "status": status,
            "note": note,
            "ledger_present": bool(read["coverage"].get("ledger_present")),
            "events": len(read["events"]),
            "scanned": len(events),
            "undated_records": undated,
            "logical_records": len(logical),
            "retry_records": len(retries),
            "truncated": bool(read["coverage"].get("truncated")),
            "errors": read["coverage"].get("errors") or [],
            "boundaries": boundaries,
        },
        "timezone": TIMEZONE_NAME,
        "checked_epoch": now_ms // 1000,
    }


def _um_trend_bucket(events, logical, cost_reading):
    return {
        "tokens": _um_sum_readings(
            [_um_event_total_reading(ev) for ev in events]),
        "costCny": cost_reading,
        "requests": _um_exact(len(logical)),
        "speed": _um_speed_payload(events, logical)["value"],
    }


def _um_merge_buckets(buckets):
    return {
        "tokens": _um_sum_readings([b["tokens"] for b in buckets]),
        "costCny": _um_sum_readings([b["costCny"] for b in buckets]),
        "requests": _um_sum_readings([b["requests"] for b in buckets]),
        "speed": _um_reading("missing", note="按请求加权的整次平均"),
    }


def _um_trend_speed(events, logical):
    return _um_speed_payload(events, logical)["value"]


def _um_empty_trend(days, range, status, note, start, end, now_ms,
                    ledger_present=False, errors=None):
    missing = _um_reading("missing", note=note)
    bucket = {"tokens": missing, "costCny": missing,
              "requests": missing, "speed": missing}
    daily = [{key: value for key, value in bucket.items()}
              for _day in days]
    total = {key: value for key, value in bucket.items()}
    trend = {
        "range": range,
        "days": days,
        "all": {"daily": daily, "total": total},
        "models": [],
        "speedBasis": "whole_request",
        "boundaries": {
            "timezone": TIMEZONE_NAME,
            "start": _um_iso(start),
            "end": _um_iso(end),
            "half_open": True,
        },
    }
    return {
        "trend": trend,
        "coverage": {
            "status": status,
            "note": note,
            "ledger_present": ledger_present,
            "events": 0,
            "scanned": 0,
            "undated_records": 0,
            "logical_records": 0,
            "retry_records": 0,
            "truncated": False,
            "errors": errors or [],
            "days": days,
        },
        "timezone": TIMEZONE_NAME,
        "checked_epoch": now_ms // 1000,
    }


def um_trend(ledger=None, range="7d", now=None, snapshot_path=None,
              status_path=None):
    """Near-N Beijing natural-day trend + same-window model ranking.

    Token/cost sums include every ledger row (including the physical retry
    rows' recorded consumption); the request series counts logical requests
    only, so a physical retry is never counted twice. Speed is the
    request-weighted whole-request average over the logical requests with real
    output-token and positive-duration evidence; a bucket without evidence is
    missing, never 0.
    """
    if range not in UM_TREND_RANGES:
        raise ValueError(
            "invalid trend range %r: expected one of %s"
            % (range, ", ".join(UM_TREND_RANGES)))
    read = _um_read(ledger)
    now_ms = _now_ms(now)
    snapshot_path, status_path = _um_price_paths(
        snapshot_path, status_path)
    status, note = _um_ledger_status(read)
    days = _window_day_keys(
        {"7d": 7, "14d": 14, "30d": 30}[range], now_ms)
    start = int(_environment().day_start_ms(days[0]))
    end = int(_environment().day_start_ms(days[-1])) + 86_400_000
    if status in ("missing", "unavailable"):
        return _um_empty_trend(
            days, range, status, note, start, end, now_ms,
            ledger_present=bool(
                read["coverage"].get("ledger_present")),
            errors=read["coverage"].get("errors") or [])
    events, undated = _um_window_events(read, start, end)
    logical = [ev for ev in events if not _um_is_retry(ev)]
    retries = [ev for ev in events if _um_is_retry(ev)]
    by_day = {day: [] for day in days}
    for ev in events:
        day = _environment().beijing_day(ev.ms)
        if day in by_day:
            by_day[day].append(ev)
    model_names = sorted({ev.model for ev in events})
    day_costs, day_buckets = {}, {}
    for day in days:
        day_events = by_day.get(day) or []
        if not day_events:
            day_costs[day] = _um_cost_payload(
                [], snapshot_path, status_path)
        else:
            day_costs[day] = _um_cost_payload(
                day_events, snapshot_path, status_path)
        day_logical = [ev for ev in day_events if not _um_is_retry(ev)]
        day_buckets[day] = _um_trend_bucket(
            day_events, day_logical, _um_cost_reading(day_costs[day]))
    model_rows = []
    for model in model_names:
        daily = []
        model_events = []
        model_logical = []
        failed_total = 0
        for day in days:
            day_events = [ev for ev in by_day.get(day) or []
                          if ev.model == model]
            day_logical = [ev for ev in day_events
                           if not _um_is_retry(ev)]
            model_events.extend(day_events)
            model_logical.extend(day_logical)
            for ev in day_logical:
                if _um_result(ev)[0] in ("failed", "truncated"):
                    failed_total += 1
            daily.append(_um_trend_bucket(
                day_events, day_logical,
                _um_cost_model_reading(day_costs[day], model)))
        total = _um_merge_buckets(daily)
        total["speed"] = _um_trend_speed(model_events, model_logical)
        model_rows.append({
            "model": model,
            "daily": daily,
            "total": total,
            "failed": _um_exact(failed_total),
        })
    model_rows.sort(key=lambda row: (
        -(_um_value(row["total"]["tokens"]) or 0), row["model"]))
    all_daily = [day_buckets[day] for day in days]
    all_total = _um_merge_buckets(all_daily)
    all_total["speed"] = _um_trend_speed(events, logical)
    trend = {
        "range": range,
        "days": days,
        "all": {"daily": all_daily, "total": all_total},
        "models": model_rows,
        "speedBasis": "whole_request",
        "boundaries": {
            "timezone": TIMEZONE_NAME,
            "start": _um_iso(start),
            "end": _um_iso(end),
            "half_open": True,
        },
    }
    if status == "partial":
        _um_mark_trend_partial(trend)
    return {
        "trend": trend,
        "coverage": {
            "status": status,
            "note": note,
            "ledger_present": bool(read["coverage"].get("ledger_present")),
            "events": len(read["events"]),
            "scanned": len(events),
            "undated_records": undated,
            "logical_records": len(logical),
            "retry_records": len(retries),
            "truncated": bool(read["coverage"].get("truncated")),
            "errors": read["coverage"].get("errors") or [],
            "days": days,
        },
        "timezone": TIMEZONE_NAME,
        "checked_epoch": now_ms // 1000,
    }


def _um_decode_cursor(cursor):
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("invalid cursor")
    try:
        payload = json.loads(
            base64.b64decode(cursor.encode("ascii"), validate=True)
            .decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - malformed cursor is a 400
        raise ValueError("invalid cursor") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise ValueError("invalid cursor")
    at = payload.get("at")
    key = payload.get("key")
    if isinstance(at, bool) or not isinstance(at, int):
        raise ValueError("invalid cursor")
    if not isinstance(key, str) or not key:
        raise ValueError("invalid cursor")
    return at, key


def _um_encode_cursor(at, key):
    payload = json.dumps({"v": 1, "at": at, "key": key},
                          ensure_ascii=True, sort_keys=True)
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


def um_requests(ledger=None, window="today", model=None, result="all",
                  search="", cursor=None, limit=50, now=None,
                  snapshot_path=None, status_path=None):
    """Bounded UM request list from UM's own ledger.

    Filtering is by Beijing time window, exact model, real result and a
    bounded search over request_id/model/status/result note only (no request
    body or secret is ever read or returned). Pagination is newest first;
    a malformed/stale cursor is a 400, not an empty list. Physical retry
    rows are not request rows: with a reliable 32-hex request_id they become
    the logical row's retries; otherwise they are counted in coverage and
    never inflate the request count.
    """
    if window not in UM_WINDOWS:
        raise ValueError(
            "invalid window %r: expected one of %s"
            % (window, ", ".join(UM_WINDOWS)))
    if result != "all" and result not in UM_RESULTS:
        raise ValueError(
            "invalid result %r: expected all or one of %s"
            % (result, ", ".join(UM_RESULTS)))
    if search is None:
        search = ""
    if not isinstance(search, str):
        raise ValueError("invalid search")
    search = search[:UM_MAX_SEARCH_CHARS]
    limit, clamped = _clamp_int(limit, 50, 1, UM_MAX_PAGE)
    cursor_at = cursor_key = None
    if cursor is not None:
        cursor_at, cursor_key = _um_decode_cursor(cursor)
    read = _um_read(ledger)
    now_ms = _now_ms(now)
    snapshot_path, status_path = _um_price_paths(
        snapshot_path, status_path)
    bounds = _um_window_bounds(window, now_ms)
    status, note = _um_ledger_status(read)
    if status in ("missing", "unavailable"):
        return {
            "page": {
                "rows": [],
                "scope": "%s的 UM 账本（chat2api）" % bounds["label"],
                "earlier": None,
                "coverage": {
                    "status": status, "note": note,
                    "ledger_present": bool(
                        read["coverage"].get("ledger_present")),
                    "events": len(read["events"]),
                    "errors": read["coverage"].get("errors") or [],
                    "truncated": bool(
                        read["coverage"].get("truncated")),
                },
            },
            "models": [],
            "coverage": {
                "status": status, "note": note,
                "ledger_present": bool(
                    read["coverage"].get("ledger_present")),
                "events": len(read["events"]),
                "errors": read["coverage"].get("errors") or [],
                "truncated": bool(read["coverage"].get("truncated")),
            },
            "timezone": TIMEZONE_NAME,
            "checked_epoch": now_ms // 1000,
        }
    events, undated = _um_window_events(
        read, bounds["start"], bounds["end"])
    retries = [ev for ev in events if _um_is_retry(ev)]
    logical = [ev for ev in events if not _um_is_retry(ev)]
    retry_map = _um_retry_map(read["events"])
    keys = _um_occurrence_keys(logical)
    price_records = []
    price_index = {}
    for ev in logical:
        record = _um_event_record(ev)
        price_records.append(record)
        price_index[id(ev)] = record
    for ev in retries:
        if id(ev) not in price_index:
            record = _um_event_record(ev)
            price_records.append(record)
            price_index[id(ev)] = record
    priced = []
    for record in price_records:
        priced.append(_um_price_one(
            record, snapshot_path, status_path))
    price_by_record = {id(record): price for record, price
                        in zip(price_records, priced)}
    logical_ids = {rid for ev in logical
                   for rid in (_um_request_id(ev.request_id),)
                   if rid is not None}
    # A retry row is unattached only when it has no reliable id or no
    # logical row in this window carries that id. A logical row without an id
    # is not an unattached retry; its retries simply stay unrecorded (null).
    unattached = sum(
        1 for ev in retries
        for rid in (_um_request_id(ev.request_id),)
        if rid is None or rid not in logical_ids)
    rows = []
    for ev in logical:
        row_result, row_note, _http = _um_result(ev)
        if model is not None and ev.model != model:
            continue
        if result != "all" and row_result != result:
            continue
        request_id = _um_request_id(ev.request_id)
        attached = list(retry_map.get(request_id, [])) \
            if request_id is not None else []
        if search:
            haystack = " ".join((
                ev.model, ev.status if isinstance(ev.status, str) else "",
                request_id or "", row_note))
            if search.lower() not in haystack.lower():
                continue
        price = price_by_record.get(id(price_index.get(id(ev))))
        retry_prices = [
            price_by_record.get(id(price_index.get(id(retry))))
            for retry in attached]
        row = _um_request_row(ev, keys[id(ev)], attached, price,
                               retry_prices)
        rows.append(row)
    rows.sort(key=lambda row: (-int(row["at"]), row["key"]))
    if cursor is not None:
        # Rows are sorted ascending by (-at, key); the cursor's next page is
        # the rows strictly AFTER it in that order (older time first).
        rows = [row for row in rows
                 if (-int(row["at"]), row["key"]) >
                 (-cursor_at, cursor_key)]
    earlier = None
    if len(rows) > limit:
        last = rows[limit - 1]
        earlier = _um_encode_cursor(int(last["at"]), last["key"])
        rows = rows[:limit]
    return {
        "page": {
            "rows": rows,
            "scope": ("北京时间%s（%s – %s）的 UM 账本（chat2api），"
                       "时间倒序；物理重试不单独成行"
                       % (bounds["label"], _um_iso(bounds["start"]),
                          _um_iso(bounds["end"]))),
            "earlier": earlier,
            "coverage": {
                "status": status,
                "note": note,
                "ledger_present": bool(
                    read["coverage"].get("ledger_present")),
                "events": len(read["events"]),
                "scanned": len(events),
                "undated_records": undated,
                "logical_records": len(logical),
                "retry_records": len(retries),
                "unattached_retries": unattached,
                "truncated": bool(
                    read["coverage"].get("truncated")),
                "errors": read["coverage"].get("errors") or [],
                "bounded": True,
                "clamped": clamped,
            },
        },
        "models": sorted({ev.model for ev in events}),
        "coverage": {
            "status": status,
            "note": note,
            "ledger_present": bool(
                read["coverage"].get("ledger_present")),
            "events": len(read["events"]),
            "scanned": len(events),
            "undated_records": undated,
            "logical_records": len(logical),
            "retry_records": len(retries),
            "unattached_retries": unattached,
            "truncated": bool(read["coverage"].get("truncated")),
            "errors": read["coverage"].get("errors") or [],
            "bounded": True,
            "clamped": clamped,
        },
        "timezone": TIMEZONE_NAME,
        "checked_epoch": now_ms // 1000,
    }
