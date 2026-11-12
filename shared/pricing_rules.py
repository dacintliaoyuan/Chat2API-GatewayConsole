# -*- coding: utf-8 -*-
"""Pure pricing rules shared by the local stats and the cloud summary paths.

This module is bundled with both gateway applications. It owns no host, state,
file, network, cache or lock: the price table is always passed in explicitly by
the caller (``load_price_table`` and its 60s cache stay in the host).

The rules are the dsh-cost-meter port used by usagesrv: model matching, the
Beijing weekend/peak selection, price-table version selection and per-record cost
estimation. Behaviour is preserved field for field; the ledger semantics are
documented on the host module.

Cross-end consumers call the public names (``rate_of``, ``tiers_of``, ...). The
underscore-prefixed spellings of those same ten functions stay bound to the very
same function objects for the existing ``usagesrv.py`` and
``local/backend/stats_env.py`` call sites until E19-1-02 / E19-1-04 rename them;
they are aliases, not second implementations, so the two spellings can never
diverge.
"""

import re
from datetime import datetime, timezone, timedelta

# dsh-cost-meter pricing.js 的两个历史分界（峰谷时代前按 legacyBase 计、
# 北京时间周末全天谷价），保持与其账本口径一致。
LEGACY_BASE_BOUNDARY = "2026-08-16T16:00:00Z"
WEEKEND_OFFPEAK_AT = "2026-08-22T16:00:00Z"

# 汇率兜底：价目表（或某个版本段）没写 exchangeRate / 写了非正数时一律用
# 该值——与版本化前 load_price_table 内的硬编码兜底逐字段一致。
DEFAULT_EXCHANGE_RATE = 7.2


# ------------------------------------------------------------------ 时间规则

def parse_ts(value):
    """ISO 时间 → epoch ms；失败返回 None。"""
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
    except Exception:
        return None


def beijing_day(ms):
    """epoch ms → 北京时间日期字符串；时刻缺失返回 None。"""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def day_start_ms(day):
    """北京时间某日 00:00 → epoch ms；缺失/不可解析返回 None（不猜）。"""
    try:
        dt = datetime.strptime(str(day), "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000


def beijing_weekday(day_index):
    """北京时间日序 → 星期（0=周日…6=周六；1970-01-01 为周四）。"""
    return (day_index + 4) % 7


def weekend_zone_at(at_ms):
    """北京时间周六/周日全谷价区间；非周末、生效前或时刻无效返回 None
    （插件对非有限时刻同样返回 null）。"""
    if not isinstance(at_ms, (int, float)) or at_ms != at_ms:
        return None
    eff = parse_ts(WEEKEND_OFFPEAK_AT)
    if at_ms < eff:
        return None
    day = int((at_ms + 8 * 3600_000) // 86_400_000)
    weekday = beijing_weekday(day)
    if weekday not in (6, 0):
        return None
    sat_day = day if weekday == 6 else day - 1
    start = max(sat_day * 86_400_000 - 8 * 3600_000, eff)
    end = (sat_day + 2) * 86_400_000 - 8 * 3600_000
    return (start, end)


def is_peak_hour(at_ms, effective_at_ms, windows):
    if at_ms is None:
        return False
    if not windows:
        return False
    if weekend_zone_at(at_ms) is not None:
        return False
    if effective_at_ms is not None and at_ms < effective_at_ms:
        return False
    hour = datetime.fromtimestamp(at_ms / 1000, tz=timezone.utc).hour
    for w in windows:
        try:
            start, end = int(w["start"]), int(w["end"])
        except Exception:
            continue
        if start < end:
            if start <= hour < end:
                return True
        elif hour >= start or hour < end:
            return True
    return False


# ------------------------------------------------------------------ 模型匹配

def canon_model_id(model_id):
    """归一化：小写、去括号附注、只留字母数字（'GLM-5.2 (go)' → 'glm52'）。"""
    out = str(model_id or "").lower()
    for close, open_ in ((")", "("), ("）", "（")):
        while open_ in out and close in out[out.index(open_):]:
            i = out.index(open_)
            j = out.find(close, i)
            if j < 0:
                break
            out = out[:i] + " " + out[j + 1:]
    return "".join(ch for ch in out if ch.isascii() and ch.isalnum())


def strip_id_decor(model_id):
    """去日期快照（-20260101 / -2026-01-01）与版本后缀（-v2 / -v1.5）。"""
    out = str(model_id).lower()
    out = re.sub(r"[-@]\d{4}-?\d{2}-?\d{2}$", "", out)
    out = re.sub(r"[-@]v\d+(\.\d+)*$", "", out)
    return out


def tokens_of(model_id):
    return [t for t in re.split(r"[-_./:]+", strip_id_decor(model_id)) if t]


def match_model_id_uncached(model_id, candidates):
    """dsh-cost-meter matchModelId 的 Python 移植（纯函数，无记忆化）：
    精确 → 归一化等价 → 宽泛包含（最长候选）→ 去后缀精确 → 前缀 → 家族相似。
    数字分叉守卫（glm-5 vs glm-5.3）与之同口径。

    带记忆化的入口留在宿主（usagesrv.match_model_id），缓存有界；本函数
    不持全局状态，任何调用方都可安全地按请求重复调用。"""
    if not model_id or not candidates:
        return None
    if model_id in candidates:
        return model_id
    canon = canon_model_id(model_id)
    if not canon:
        return None
    for c in candidates:
        if canon_model_id(c) == canon:
            return c
    contain_hit, contain_len = None, 0
    for c in candidates:
        cc = canon_model_id(c)
        if len(cc) < 4 or cc == canon:
            continue
        idx = canon.find(cc)
        if idx >= 0 and len(cc) > contain_len:
            if re.fullmatch(r"\d{1,2}", canon[idx + len(cc):]):
                continue
            contain_hit, contain_len = c, len(cc)
    if contain_hit is not None:
        return contain_hit
    stripped = strip_id_decor(model_id)
    for c in candidates:
        if strip_id_decor(c) == stripped:
            return c
    prefix_hit = None
    for c in candidates:
        cs = strip_id_decor(c)
        if not cs or cs == stripped:
            continue
        rest = stripped[len(cs):] if stripped.startswith(cs) else ""
        if rest and rest[0] in "-_./:":
            tail = re.sub(r"^[-_./:]+", "", rest)
            if re.fullmatch(r"\d{1,2}", tail):
                continue
            if prefix_hit is None or len(strip_id_decor(prefix_hit)) < len(cs):
                prefix_hit = c
    if prefix_hit is not None:
        return prefix_hit
    mt = tokens_of(model_id)
    if len(mt) < 2:
        return None
    best, best_len = None, 0
    for c in candidates:
        ct = tokens_of(c)
        n = 0
        while n < len(mt) and n < len(ct) and mt[n] == ct[n]:
            n += 1
        if n < len(mt) and n < len(ct) and mt[n].isdigit() and ct[n].isdigit():
            continue
        if n >= 2 and n == len(ct) and n < len(mt) and all(
                re.fullmatch(r"\d{1,2}", t) for t in mt[n:]):
            continue
        if n >= 2 and n < len(mt) and n < len(ct) and (
                (re.fullmatch(r"\d{1,2}", mt[n]) and ct[n][:1].isalpha())
                or (re.fullmatch(r"\d{1,2}", ct[n]) and mt[n][:1].isalpha())):
            continue
        if n >= 2 and (n > best_len or (n == best_len and best is not None and len(c) < len(best))):
            best, best_len = c, n
    return best


# -------------------------------------------------------------- 档位与成本

def complete_tier(raw):
    """补齐一档价格：cacheMiss←input，cacheHit←cachedInput/cacheRead，output←0。"""
    if not isinstance(raw, dict):
        return None

    def num(key):
        v = raw.get(key)
        return v if isinstance(v, (int, float)) and v == v and v >= 0 else None

    cache_miss = num("cacheMiss") if num("cacheMiss") is not None else num("input")
    cache_hit = num("cacheHit")
    if cache_hit is None:
        cache_hit = num("cachedInput")
    if cache_hit is None:
        cache_hit = num("cacheRead")
    if cache_hit is None:
        cache_hit = cache_miss
    return {
        "cacheHit": cache_hit or 0,
        "cacheMiss": cache_miss or 0,
        "output": num("output") or 0,
        "reasoning": num("reasoning"),
        "unpriced": raw.get("unpriced") is True,
    }


def normalize_price(raw):
    """对应插件 normalizePrice：complete_tier 补齐基础三桶后保留峰谷子档
    （offPeak/peak/legacyBase），供 tier_for 按时刻选档；其余展示字段不移植。
    此前解析链直接用 complete_tier 会剥掉子档，导致 DeepSeek 峰谷与
    legacyBase 档位失真（2026-09-05 随 D1 对齐一并修复）。"""
    entry = complete_tier(raw)
    if entry is None:
        return None
    if isinstance(raw, dict):
        for key in ("offPeak", "peak", "legacyBase"):
            sub = complete_tier(raw.get(key))
            if sub is not None:
                entry[key] = sub
    return entry


def tier_for(entry, at_ms, peak_cfg):
    """选档：生效后峰时段 → peak（缺子档按基础价，与插件 tierFor 一致）；
    生效后谷时段 → offPeak；生效前、禁用或时刻无效 → 基础价；峰谷时代前 → legacyBase。"""
    base = entry or {"cacheHit": 0, "cacheMiss": 0, "output": 0}
    legacy = base.get("legacyBase")

    def as_tier(price):
        return {
            "cacheHit": price.get("cacheHit") or 0,
            "cacheMiss": price.get("cacheMiss") or 0,
            "output": price.get("output") or 0,
            "reasoning": price.get("reasoning"),
        }

    if at_ms is not None and at_ms < parse_ts(LEGACY_BASE_BOUNDARY):
        return as_tier(legacy if legacy else base)
    if not peak_cfg.get("enabled"):
        return as_tier(base)
    effective = peak_cfg.get("effectiveAtMs")
    if is_peak_hour(at_ms, effective, peak_cfg["windows"]):
        peak = base.get("peak")
        # 缺子档按基础价原值，与插件 tierFor 一致（2026-09-05 用户拍板对齐，
        # 撤销移植期「按官方峰谷比例 ×2」的近似）。
        return as_tier(peak) if peak else as_tier(base)
    if at_ms is not None and effective is not None and at_ms >= effective:
        off = base.get("offPeak")
        if off:
            return as_tier(off)
    return as_tier(base)


def cost_of(tokens, entry, at_ms, peak_cfg):
    """一次调用的 USD 成本。tokens 各桶相互独立（缓存部分须先从 input 里扣除）。"""
    tier = tier_for(entry, at_ms, peak_cfg)
    cost = (tokens.get("input", 0) * tier["cacheMiss"]
            + tokens.get("output", 0) * tier["output"]
            + tokens.get("cache", 0) * tier["cacheHit"]
            + tokens.get("reasoning", 0) * (tier["reasoning"] or 0)) / 1_000_000
    return cost if cost == cost and 0 < cost < float("inf") else 0.0


# ------------------------------------------------------------- 价目表结构

def tiers_of(models_table, providers_table):
    """价表扁平成 [(models_dict, billingMode, source)]，按精确度打分择优。"""
    tiers = []
    if isinstance(models_table, dict) and models_table:
        tiers.append((models_table, "deepseek-peak", "prices"))
    for prov, table in (providers_table or {}).items():
        if isinstance(table, dict) and table.get("models"):
            tiers.append((table["models"], "flat", "prices"))
    return tiers


def rate_of(raw_rate):
    """汇率：非数值/非正数一律退回 DEFAULT_EXCHANGE_RATE（既有兜底口径）。"""
    if not isinstance(raw_rate, (int, float)) or raw_rate <= 0:
        return DEFAULT_EXCHANGE_RATE
    return float(raw_rate)


def peak_of(raw_peak):
    """峰谷规则：缺 enabled/windows 即无峰谷（按基础档），effectiveAt 解析失败为 None。"""
    peak_raw = raw_peak if isinstance(raw_peak, dict) else {}
    return {
        "enabled": peak_raw.get("enabled") is True,
        "effectiveAtMs": parse_ts(peak_raw.get("effectiveAt") or ""),
        "windows": peak_raw.get("windows") or [],
    }


def checked_age_days(checked_at, now):
    """checkedAt（人工核对日期）距今天数；缺失/不可解析返回 None（不猜）。"""
    if isinstance(checked_at, str) and checked_at.strip():
        checked_ms = parse_ts(checked_at.strip())
        if checked_ms is not None:
            return max(0, int((now * 1000 - checked_ms) // 86_400_000))
    return None


def version_id(raw, effective_at_ms):
    """版本标识：显式 versionId 优先，缺省用生效时刻字符串（便于按版本归组）。

    可用版本段必然有可解析的生效时刻，因此这里的返回值不会是 None；旧形状
    的默认段没有显式版本，标识为 None。
    """
    version_id = raw.get("versionId")
    if isinstance(version_id, str) and version_id.strip():
        return version_id.strip()
    effective_at = raw.get("effectiveAt")
    if isinstance(effective_at, str) and effective_at.strip():
        return effective_at.strip()
    return None


def price_version(raw, effective_at_ms, now):
    """一个显式版本段 → 内部查找结构（价格自持，不继承顶层当前表）。"""
    checked_at = raw.get("checkedAt")
    return {
        "versionId": version_id(raw, effective_at_ms),
        "effectiveAt": raw.get("effectiveAt"),
        "effectiveAtMs": effective_at_ms,
        "exchangeRate": rate_of(raw.get("exchangeRate")),
        "peak": peak_of(raw.get("peak")),
        "tiers": tiers_of(raw.get("models"), raw.get("providers")),
        "checkedAt": checked_at if isinstance(checked_at, str) else None,
        "checkedAgeDays": checked_age_days(checked_at, now),
    }


def current_version(versions, versioned, now_ms):
    """当前生效段：显式版本段按现时刻选；无可用段返回 None。"""
    if not versions:
        return None
    if not versioned:
        return versions[0]  # 旧形状：唯一默认段恒生效
    hit = version_at({"versions": versions, "versioned": True}, now_ms)
    return hit if hit is not None else versions[0]


def version_at(table, at_ms):
    """记录时刻 → 当时生效的版本段；无法证明时返回 None（不猜最新版本）。

    旧形状（无显式版本段）只有一段恒生效的默认段：任何时刻（含时刻缺失/
    不可解析）都取它——与版本化前逐字段一致。显式版本段下，时刻缺失、不可
    解析或早于首段生效时刻时返回 None，调用方据此按「未知」处理（不返回 0
    冒充，也不默默按最新价计）。
    """
    versions = table.get("versions") or []
    if not versions:
        return None
    if not table.get("versioned"):
        return versions[0]
    if not isinstance(at_ms, (int, float)) or at_ms != at_ms:
        return None
    hit = None
    for version in versions:
        effective = version.get("effectiveAtMs")
        if effective is None or at_ms < effective:
            continue
        if hit is None or effective > hit["effectiveAtMs"]:
            hit = version
    return hit


def version_rate(table, at_ms):
    """记录时刻 → 当时生效版本的汇率；版本未知返回 None（不猜现价）。"""
    version = version_at(table, at_ms)
    return version["exchangeRate"] if version else None


# ---------------------------------------------------- 旧私有名（同一对象别名）
# usagesrv.py 消费全部十个、local/backend/stats_env.py 消费其中六个；两处调用
# 点分别在 E19-1-02 / E19-1-04 改名。这里把旧拼写绑到**同一个函数对象**，
# 不是复制实现：改名前后行为逐字节一致，别名不会与公开名分叉。
_beijing_weekday = beijing_weekday
_checked_age_days = checked_age_days
_current_version = current_version
_peak_of = peak_of
_price_version = price_version
_rate_of = rate_of
_strip_id_decor = strip_id_decor
_tiers_of = tiers_of
_tokens_of = tokens_of
_version_id = version_id


# ------------------------------------------------------------- 价格解析与估算

PRICE_STALE_DAYS = 30


def normalize_price_table(payload, override=None, *, now,
                          override_found=None):
    """Normalize a raw on-disk price document into the versioned table shape.

    This is the ONE pure normalization both ends use, so ``local``'s stats reader
    and ``cloud``'s summary read the SAME document the same way. It performs no
    I/O: the caller reads the document and the override file and passes them in.

    A top-level ``versions`` list is parsed segment by segment, so a record is
    priced with the segment in effect at its time (the current version is NOT
    silently used for history); a document without ``versions`` becomes ONE
    always-effective default segment (the legacy shape). ``override`` is the
    explicit highest-priority manual patch layer; a missing/unreadable one is
    empty, never invented.
    """
    payload = payload if isinstance(payload, dict) else {}
    now = time.time() if now is None else now
    top_rate = rate_of(payload.get("exchangeRate"))
    top_peak = peak_of(payload.get("peak"))
    top_tiers = tiers_of(
        payload.get("models"), payload.get("providers"))
    checked_at = payload.get("checkedAt")
    checked_age = checked_age_days(checked_at, now)

    raw_versions = payload.get("versions")
    versioned = isinstance(raw_versions, list)
    versions, invalid_versions = [], 0
    if versioned:
        for raw in raw_versions:
            if not isinstance(raw, dict):
                invalid_versions += 1
                continue
            effective_at_ms = parse_ts(raw.get("effectiveAt") or "")
            if effective_at_ms is None:
                # A segment with no usable effective time can never be hit by
                # a record time: ignore and count it, never treat it as
                # effective forever (that would price history with today).
                invalid_versions += 1
                continue
            versions.append(price_version(raw, effective_at_ms, now))
        versions.sort(key=lambda version: version["effectiveAtMs"])
    else:
        # Legacy shape: ONE default segment effective for every instant.
        versions = [{
            "versionId": None,
            "effectiveAt": None,
            "effectiveAtMs": None,
            "exchangeRate": top_rate,
            "peak": top_peak,
            "tiers": top_tiers,
            "checkedAt": checked_at if isinstance(checked_at, str) else None,
            "checkedAgeDays": checked_age,
        }]

    # Top-level "current" view: the segment in effect now (the earliest one
    # when now precedes the first segment); no usable segment falls back to the
    # top-level values for display only — resolution always uses the segments.
    current = current_version(versions, versioned, now * 1000)
    if current is None:
        rate, peak_cfg, tiers = top_rate, top_peak, top_tiers
    else:
        rate, peak_cfg, tiers = (current["exchangeRate"], current["peak"],
                                 current["tiers"])

    override_payload = override if isinstance(override, dict) else {}
    override_models = {key: value for key, value in override_payload.items()
                       if not key.startswith("_")}
    if override_found is None:
        override_found = bool(override_models)
    return {
        "exchangeRate": rate,
        "peak": peak_cfg,
        "tiers": tiers,
        "override": override_models,
        "versions": versions,
        "versioned": versioned,
        "versionMeta": {
            "versioned": versioned,
            "count": len(versions),
            "invalidCount": invalid_versions,
            "current": None if current is None else {
                "versionId": current["versionId"],
                "effectiveAt": current["effectiveAt"],
                "exchangeRate": current["exchangeRate"],
                "checkedAt": current["checkedAt"],
                "checkedStale": (
                    current["checkedAgeDays"] > PRICE_STALE_DAYS)
                if current["checkedAgeDays"] is not None else None,
            },
        },
        "meta": {
            "pricesFound": bool(top_tiers),
            "checkedAt": checked_at if isinstance(checked_at, str) else None,
            "checkedAgeDays": checked_age,
            "checkedStale": (checked_age > PRICE_STALE_DAYS)
            if checked_age is not None else None,
            "overrideFound": bool(override_found),
        },
    }


def resolve_price(model_id, at_ms, table, *, matcher=None):
    """返回 (entry, billingMode, matched_model, source)；未定价/版本未知返回 (None, ...)。

    at_ms = 记录时刻：按当时生效的版本段取价（含该段的峰谷与 override 优先级）。
    旧形状价表只有一段恒生效的默认段，时刻（含缺失/不可解析）不影响结果——
    与版本化前逐字段一致。补缺层 price.override.json 没有生效时刻，仍是最高
    优先级的手工修补层（要按时刻分段请写进版本段）。

    ``table`` 由调用方显式传入（宿主 load_price_table）；本函数不读文件、不建缓存。
    ``matcher`` 是宿主显式的模型匹配入口（usagesrv.match_model_id 的记忆化版本）：
    缺省用纯函数 ``match_model_id_uncached``，本模块自身仍不持全局缓存。
    """
    match = matcher if matcher is not None else match_model_id_uncached
    version = version_at(table, at_ms)
    if version is None:
        return None, "flat", None, None
    override = table["override"]
    if override:
        if model_id in override:
            hit = override[model_id]
        else:
            canon = canon_model_id(model_id)
            hit = next((v for k, v in override.items()
                        if canon_model_id(k) == canon), None)
        if hit is not None:
            if isinstance(hit, dict) and isinstance(hit.get("target"), str):
                return resolve_price(hit["target"], at_ms, table,
                                     matcher=matcher)
            entry = normalize_price(hit)
            if entry and not entry["unpriced"]:
                return entry, "flat", model_id, "override"
    best = None  # (score, entry, billingMode, matched, source)，分数最高的标价命中
    unpriced_score = None  # 显式 unpriced 命中的最高分
    for models, billing_mode, source in version["tiers"]:
        keys = list(models.keys())
        if model_id in keys:
            hit, exact = model_id, True
        else:
            hit = match(model_id, keys)
            exact = False
        if hit is None:
            continue
        entry = normalize_price(models[hit])
        if entry is None:
            continue
        score = (1000 if exact else 0) + len(canon_model_id(hit))
        if entry["unpriced"]:
            if unpriced_score is None or score > unpriced_score:
                unpriced_score = score
            continue
        if best is None or score > best[0]:
            best = (score, entry, billing_mode, hit, source)
    # 「无价不标价」一票否决：显式 unpriced 命中与标价命中同样具体（同分，如
    # 同一模型 id 分列无价/有价两个 provider 档）或更具体时，视为该模型已被
    # 价表标记无价，不落到其他档的标价（2026-09-05 用户拍板）。
    if best is None or (unpriced_score is not None and unpriced_score >= best[0]):
        return None, "flat", None, None
    return best[1], best[2], best[3], best[4]


def estimate_record(model_id, at_ms, input_tok, output_tok, cache_tok,
                    reasoning_tok, table, *, matcher=None):
    """单条记录估算：返回 (cost_usd, priced, matched_model, peak_used)。

    既有 4 元组契约（console_stats 依赖）：按记录时刻所属版本计价，版本标识
    不进返回值；需要版本标识用 estimate_record_detail。
    """
    cost, priced, matched, peak, _version_id, _rate, _cache_unknown = \
        estimate_record_detail(
            model_id, at_ms, input_tok, output_tok, cache_tok, reasoning_tok,
            table, matcher=matcher)
    return cost, priced, matched, peak


def estimate_record_detail(model_id, at_ms, input_tok, output_tok, cache_tok,
                          reasoning_tok, table, *, matcher=None):
    """内部估算：返回
    (cost_usd, priced, matched, peak, version_id, exchange_rate, cache_unknown)。

    exchange_rate 为该记录所属版本的汇率，None 表示版本未知（不折算、不猜
    现价）。版本化价表下记录时刻缺失/不可解析/早于首段时返回
    (0.0, False, None, False, None, None, False)。
    ``cache_tok is None`` 表示缓存量未报告（Q11）：此时不把未知缓存折成 0，
    而是按全部输入都是 cache-miss 计算上界，并由 cache_unknown=True 告诉
    调用方金额不是精确值。旧记录调用方始终传数值，历史金额不变。
    """
    cache_unknown = cache_tok is None
    version = version_at(table, at_ms)
    if version is None:
        return 0.0, False, None, False, None, None, cache_unknown
    entry, _billing_mode, matched, _source = resolve_price(
        model_id, at_ms, table, matcher=matcher)
    if entry is None:
        return (0.0, False, None, False, version["versionId"], None,
                cache_unknown)
    cache = max(0, cache_tok or 0)
    noncached = max(0, (input_tok or 0) - cache)  # 缓存命中视为 input 的子集
    peak_cfg = version["peak"]
    in_peak = is_peak_hour(at_ms, peak_cfg["effectiveAtMs"], peak_cfg["windows"])
    tokens = {"input": noncached, "output": output_tok or 0,
              "cache": cache, "reasoning": reasoning_tok or 0}
    cost = cost_of(tokens, entry, at_ms, peak_cfg)
    return (cost, True, matched, in_peak, version["versionId"],
            version["exchangeRate"], cache_unknown)
