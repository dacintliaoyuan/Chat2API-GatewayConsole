# -*- coding: utf-8 -*-
import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from share_management import BrokerClientAdapter, BrokerError, ShareManager, status_code_for
from share_store import Disabled, StoreError, SecretUnavailable, SecretUndecryptable, VersionMismatch, read_key_labels
CHAT2API_LOG = os.environ.get('CHAT2API_USAGE_LOG') or os.path.join(HERE, 'data', 'usage-chat2api.jsonl')
OVERRIDE_FILE = os.path.join(HERE, 'data', 'price.override.json')
PRICE_FILE = os.environ.get('USAGE_PRICE_FILE') or os.path.join(HERE, 'data', 'prices.json')
INDEX_FILE = os.path.join(HERE, 'web', 'dist', 'index.html')
CONSOLE_DIST_DIR = os.path.join(HERE, 'web', 'dist')
CONSOLE_STATIC_TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8', '.webmanifest': 'application/manifest+json; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon', '.woff2': 'font/woff2', '.woff': 'font/woff', '.glb': 'model/gltf-binary', '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.txt': 'text/plain; charset=utf-8', '.md': 'text/markdown; charset=utf-8'}
HOST = '127.0.0.1'
PORT = 8096

def _read_local_json(url, timeout=0.6, headers=None):
    """Best-effort local status read; never blocks the usage page startup."""
    try:
        req = Request(url, headers=headers or {})
        with urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except Exception:
        return None
LEGACY_BASE_BOUNDARY = '2026-08-16T16:00:00Z'
WEEKEND_OFFPEAK_AT = '2026-08-22T16:00:00Z'
PRICE_STALE_DAYS = 30
DEFAULT_EXCHANGE_RATE = 7.2
USAGE_TOKEN_FIELDS = ('input_tokens', 'output_tokens', 'cached_input_tokens', 'cache_tokens', 'reasoning_tokens', 'total_tokens')
_price_lock = threading.Lock()
_price_cache = {'at': 0.0, 'data': None}

def canon_model_id(model_id):
    """归一化：小写、去括号附注、只留字母数字（'GLM-5.2 (go)' → 'glm52'）。"""
    out = str(model_id or '').lower()
    for close, open_ in ((')', '('), ('）', '（')):
        while open_ in out and close in out[out.index(open_):]:
            i = out.index(open_)
            j = out.find(close, i)
            if j < 0:
                break
            out = out[:i] + ' ' + out[j + 1:]
    return ''.join((ch for ch in out if ch.isascii() and ch.isalnum()))

def _strip_id_decor(model_id):
    """去日期快照（-20260101 / -2026-01-01）与版本后缀（-v2 / -v1.5）。"""
    import re
    out = str(model_id).lower()
    out = re.sub('[-@]\\d{4}-?\\d{2}-?\\d{2}$', '', out)
    out = re.sub('[-@]v\\d+(\\.\\d+)*$', '', out)
    return out

def _tokens_of(model_id):
    import re
    return [t for t in re.split('[-_./:]+', _strip_id_decor(model_id)) if t]
_MATCH_CACHE_MAX = 4096
_match_cache = {}
_match_cache_lock = threading.Lock()

def match_model_id(model_id, candidates):
    if not model_id or not candidates:
        return None
    if not isinstance(candidates, (list, tuple)):
        return _match_model_id_uncached(model_id, candidates)
    try:
        key = (model_id, tuple(candidates))
        hash(key)
    except TypeError:
        return _match_model_id_uncached(model_id, candidates)
    with _match_cache_lock:
        if key in _match_cache:
            return _match_cache[key]
    hit = _match_model_id_uncached(model_id, candidates)
    with _match_cache_lock:
        if len(_match_cache) >= _MATCH_CACHE_MAX:
            _match_cache.clear()
        _match_cache[key] = hit
    return hit

def _match_model_id_uncached(model_id, candidates):
    """match_model_id 的未缓存实现（纯函数；带记忆化的入口见上）。"""
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
    import re
    contain_hit, contain_len = (None, 0)
    for c in candidates:
        cc = canon_model_id(c)
        if len(cc) < 4 or cc == canon:
            continue
        idx = canon.find(cc)
        if idx >= 0 and len(cc) > contain_len:
            if re.fullmatch('\\d{1,2}', canon[idx + len(cc):]):
                continue
            contain_hit, contain_len = (c, len(cc))
    if contain_hit is not None:
        return contain_hit
    stripped = _strip_id_decor(model_id)
    for c in candidates:
        if _strip_id_decor(c) == stripped:
            return c
    prefix_hit = None
    for c in candidates:
        cs = _strip_id_decor(c)
        if not cs or cs == stripped:
            continue
        rest = stripped[len(cs):] if stripped.startswith(cs) else ''
        if rest and rest[0] in '-_./:':
            tail = re.sub('^[-_./:]+', '', rest)
            if re.fullmatch('\\d{1,2}', tail):
                continue
            if prefix_hit is None or len(_strip_id_decor(prefix_hit)) < len(cs):
                prefix_hit = c
    if prefix_hit is not None:
        return prefix_hit
    mt = _tokens_of(model_id)
    if len(mt) < 2:
        return None
    best, best_len = (None, 0)
    for c in candidates:
        ct = _tokens_of(c)
        n = 0
        while n < len(mt) and n < len(ct) and (mt[n] == ct[n]):
            n += 1
        if n < len(mt) and n < len(ct) and mt[n].isdigit() and ct[n].isdigit():
            continue
        if n >= 2 and n == len(ct) and (n < len(mt)) and all((re.fullmatch('\\d{1,2}', t) for t in mt[n:])):
            continue
        if n >= 2 and n < len(mt) and (n < len(ct)) and (re.fullmatch('\\d{1,2}', mt[n]) and ct[n][:1].isalpha() or (re.fullmatch('\\d{1,2}', ct[n]) and mt[n][:1].isalpha())):
            continue
        if n >= 2 and (n > best_len or (n == best_len and best is not None and (len(c) < len(best)))):
            best, best_len = (c, n)
    return best

def complete_tier(raw):
    """补齐一档价格：cacheMiss←input，cacheHit←cachedInput/cacheRead，output←0。"""
    if not isinstance(raw, dict):
        return None

    def num(key):
        v = raw.get(key)
        return v if isinstance(v, (int, float)) and v == v and (v >= 0) else None
    cache_miss = num('cacheMiss') if num('cacheMiss') is not None else num('input')
    cache_hit = num('cacheHit')
    if cache_hit is None:
        cache_hit = num('cachedInput')
    if cache_hit is None:
        cache_hit = num('cacheRead')
    if cache_hit is None:
        cache_hit = cache_miss
    return {'cacheHit': cache_hit or 0, 'cacheMiss': cache_miss or 0, 'output': num('output') or 0, 'reasoning': num('reasoning'), 'unpriced': raw.get('unpriced') is True}

def normalize_price(raw):
    """对应插件 normalizePrice：complete_tier 补齐基础三桶后保留峰谷子档
    （offPeak/peak/legacyBase），供 tier_for 按时刻选档；其余展示字段不移植。
    此前解析链直接用 complete_tier 会剥掉子档，导致 DeepSeek 峰谷与
    legacyBase 档位失真（2026-09-05 随 D1 对齐一并修复）。"""
    entry = complete_tier(raw)
    if entry is None:
        return None
    if isinstance(raw, dict):
        for key in ('offPeak', 'peak', 'legacyBase'):
            sub = complete_tier(raw.get(key))
            if sub is not None:
                entry[key] = sub
    return entry

def _parse_ts(value):
    """ISO 时间 → epoch ms；失败返回 None。"""
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() * 1000
    except Exception:
        return None

def _beijing_weekday(day_index):
    """北京时间日序 → 星期（0=周日…6=周六；1970-01-01 为周四）。"""
    return (day_index + 4) % 7

def weekend_zone_at(at_ms):
    """北京时间周六/周日全谷价区间；非周末、生效前或时刻无效返回 None
    （插件对非有限时刻同样返回 null）。"""
    if not isinstance(at_ms, (int, float)) or at_ms != at_ms:
        return None
    eff = _parse_ts(WEEKEND_OFFPEAK_AT)
    if at_ms < eff:
        return None
    day = int((at_ms + 8 * 3600000) // 86400000)
    weekday = _beijing_weekday(day)
    if weekday not in (6, 0):
        return None
    sat_day = day if weekday == 6 else day - 1
    start = max(sat_day * 86400000 - 8 * 3600000, eff)
    end = (sat_day + 2) * 86400000 - 8 * 3600000
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
            start, end = (int(w['start']), int(w['end']))
        except Exception:
            continue
        if start < end:
            if start <= hour < end:
                return True
        elif hour >= start or hour < end:
            return True
    return False

def tier_for(entry, at_ms, peak_cfg):
    """选档：生效后峰时段 → peak（缺子档按基础价，与插件 tierFor 一致）；
    生效后谷时段 → offPeak；生效前、禁用或时刻无效 → 基础价；峰谷时代前 → legacyBase。"""
    base = entry or {'cacheHit': 0, 'cacheMiss': 0, 'output': 0}
    legacy = base.get('legacyBase')

    def as_tier(price):
        return {'cacheHit': price.get('cacheHit') or 0, 'cacheMiss': price.get('cacheMiss') or 0, 'output': price.get('output') or 0, 'reasoning': price.get('reasoning')}
    if at_ms is not None and at_ms < _parse_ts(LEGACY_BASE_BOUNDARY):
        return as_tier(legacy if legacy else base)
    if not peak_cfg.get('enabled'):
        return as_tier(base)
    effective = peak_cfg.get('effectiveAtMs')
    if is_peak_hour(at_ms, effective, peak_cfg['windows']):
        peak = base.get('peak')
        return as_tier(peak) if peak else as_tier(base)
    if at_ms is not None and effective is not None and (at_ms >= effective):
        off = base.get('offPeak')
        if off:
            return as_tier(off)
    return as_tier(base)

def cost_of(tokens, entry, at_ms, peak_cfg):
    """一次调用的 USD 成本。tokens 各桶相互独立（缓存部分须先从 input 里扣除）。"""
    tier = tier_for(entry, at_ms, peak_cfg)
    cost = (tokens.get('input', 0) * tier['cacheMiss'] + tokens.get('output', 0) * tier['output'] + tokens.get('cache', 0) * tier['cacheHit'] + tokens.get('reasoning', 0) * (tier['reasoning'] or 0)) / 1000000
    return cost if cost == cost and 0 < cost < float('inf') else 0.0

def _load_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None

def _tiers_of(models_table, providers_table):
    """价表扁平成 [(models_dict, billingMode, source)]，按精确度打分择优。"""
    tiers = []
    if isinstance(models_table, dict) and models_table:
        tiers.append((models_table, 'deepseek-peak', 'prices'))
    for prov, table in (providers_table or {}).items():
        if isinstance(table, dict) and table.get('models'):
            tiers.append((table['models'], 'flat', 'prices'))
    return tiers

def _rate_of(raw_rate):
    """汇率：非数值/非正数一律退回 DEFAULT_EXCHANGE_RATE（既有兜底口径）。"""
    if not isinstance(raw_rate, (int, float)) or raw_rate <= 0:
        return DEFAULT_EXCHANGE_RATE
    return float(raw_rate)

def _peak_of(raw_peak):
    """峰谷规则：缺 enabled/windows 即无峰谷（按基础档），effectiveAt 解析失败为 None。"""
    peak_raw = raw_peak if isinstance(raw_peak, dict) else {}
    return {'enabled': peak_raw.get('enabled') is True, 'effectiveAtMs': _parse_ts(peak_raw.get('effectiveAt') or ''), 'windows': peak_raw.get('windows') or []}

def _checked_age_days(checked_at, now):
    """checkedAt（人工核对日期）距今天数；缺失/不可解析返回 None（不猜）。"""
    if isinstance(checked_at, str) and checked_at.strip():
        checked_ms = _parse_ts(checked_at.strip())
        if checked_ms is not None:
            return max(0, int((now * 1000 - checked_ms) // 86400000))
    return None

def _version_id(raw, effective_at_ms):
    """版本标识：显式 versionId 优先，缺省用生效时刻字符串（便于按版本归组）。

    可用版本段必然有可解析的生效时刻，因此这里的返回值不会是 None；旧形状
    的默认段没有显式版本，标识为 None。
    """
    version_id = raw.get('versionId')
    if isinstance(version_id, str) and version_id.strip():
        return version_id.strip()
    effective_at = raw.get('effectiveAt')
    if isinstance(effective_at, str) and effective_at.strip():
        return effective_at.strip()
    return None

def _price_version(raw, effective_at_ms, now):
    """一个显式版本段 → 内部查找结构（价格自持，不继承顶层当前表）。"""
    checked_at = raw.get('checkedAt')
    return {'versionId': _version_id(raw, effective_at_ms), 'effectiveAt': raw.get('effectiveAt'), 'effectiveAtMs': effective_at_ms, 'exchangeRate': _rate_of(raw.get('exchangeRate')), 'peak': _peak_of(raw.get('peak')), 'tiers': _tiers_of(raw.get('models'), raw.get('providers')), 'checkedAt': checked_at if isinstance(checked_at, str) else None, 'checkedAgeDays': _checked_age_days(checked_at, now)}

def _current_version(versions, versioned, now_ms):
    """当前生效段：显式版本段按现时刻选；无可用段返回 None。"""
    if not versions:
        return None
    if not versioned:
        return versions[0]
    hit = version_at({'versions': versions, 'versioned': True}, now_ms)
    return hit if hit is not None else versions[0]

def load_price_table():
    """自持价目链聚合为统一查找结构；mtime 60 秒内直接用缓存。

    主源 data/prices.json（用户手工维护，checkedAt 供过期提醒），
    补缺层 data/price.override.json 最高优先级（resolve_price 内处理）。

    版本段（C1 2026-09-25）：文件带可选顶层 "versions" 时按段解析，价格只
    取记录时刻生效的那一段；无 "versions" 键的旧形状映射为一段恒生效的默认段，
    顶层 exchangeRate/peak/tiers 与版本化前逐字段一致。table["versions"] /
    ["versioned"] / ["versionMeta"] 是新增的加性字段，既有读取端不受影响。
    """
    now = time.time()
    with _price_lock:
        if _price_cache['data'] is not None and now - _price_cache['at'] < 60:
            return _price_cache['data']
    prices = _load_json(PRICE_FILE) or {}
    top_rate = _rate_of(prices.get('exchangeRate'))
    top_peak = _peak_of(prices.get('peak'))
    top_tiers = _tiers_of(prices.get('models'), prices.get('providers'))
    checked_at = prices.get('checkedAt')
    checked_age_days = _checked_age_days(checked_at, now)
    raw_versions = prices.get('versions')
    versioned = isinstance(raw_versions, list)
    versions, invalid_versions = ([], 0)
    if versioned:
        for raw in raw_versions:
            if not isinstance(raw, dict):
                invalid_versions += 1
                continue
            effective_at_ms = _parse_ts(raw.get('effectiveAt') or '')
            if effective_at_ms is None:
                invalid_versions += 1
                continue
            versions.append(_price_version(raw, effective_at_ms, now))
        versions.sort(key=lambda v: v['effectiveAtMs'])
    else:
        versions = [{'versionId': None, 'effectiveAt': None, 'effectiveAtMs': None, 'exchangeRate': top_rate, 'peak': top_peak, 'tiers': top_tiers, 'checkedAt': checked_at if isinstance(checked_at, str) else None, 'checkedAgeDays': checked_age_days}]
    current = _current_version(versions, versioned, now * 1000)
    if current is None:
        rate, peak_cfg, tiers = (top_rate, top_peak, top_tiers)
    else:
        rate, peak_cfg, tiers = (current['exchangeRate'], current['peak'], current['tiers'])
    override = _load_json(OVERRIDE_FILE) or {}
    override_models = {k: v for k, v in override.items() if not k.startswith('_')}
    data = {'exchangeRate': rate, 'peak': peak_cfg, 'tiers': tiers, 'override': override_models, 'versions': versions, 'versioned': versioned, 'versionMeta': {'versioned': versioned, 'count': len(versions), 'invalidCount': invalid_versions, 'current': None if current is None else {'versionId': current['versionId'], 'effectiveAt': current['effectiveAt'], 'exchangeRate': current['exchangeRate'], 'checkedAt': current['checkedAt'], 'checkedStale': current['checkedAgeDays'] > PRICE_STALE_DAYS if current['checkedAgeDays'] is not None else None}}, 'meta': {'pricesFound': bool(tiers), 'checkedAt': checked_at if isinstance(checked_at, str) else None, 'checkedAgeDays': checked_age_days, 'checkedStale': checked_age_days > PRICE_STALE_DAYS if checked_age_days is not None else None, 'overrideFound': os.path.exists(OVERRIDE_FILE)}}
    _price_cache['at'] = now
    _price_cache['data'] = data
    return data

def version_at(table, at_ms):
    """记录时刻 → 当时生效的版本段；无法证明时返回 None（不猜最新版本）。

    旧形状（无显式版本段）只有一段恒生效的默认段：任何时刻（含时刻缺失/
    不可解析）都取它——与版本化前逐字段一致。显式版本段下，时刻缺失、不可
    解析或早于首段生效时刻时返回 None，调用方据此按「未知」处理（不返回 0
    冒充，也不默默按最新价计）。
    """
    versions = table.get('versions') or []
    if not versions:
        return None
    if not table.get('versioned'):
        return versions[0]
    if not isinstance(at_ms, (int, float)) or at_ms != at_ms:
        return None
    hit = None
    for version in versions:
        effective = version.get('effectiveAtMs')
        if effective is None or at_ms < effective:
            continue
        if hit is None or effective > hit['effectiveAtMs']:
            hit = version
    return hit

def version_rate(table, at_ms):
    """记录时刻 → 当时生效版本的汇率；版本未知返回 None（不猜现价）。"""
    version = version_at(table, at_ms)
    return version['exchangeRate'] if version else None

def current_version(table=None):
    """当前生效版本段（展示与既有单值口径）；无可用段时返回 None。"""
    table = table if table is not None else load_price_table()
    return _current_version(table.get('versions') or [], table.get('versioned'), time.time() * 1000)

def resolve_price(model_id, at_ms=None):
    """返回 (entry, billingMode, matched_model, source)；未定价/版本未知返回 (None, ...)。

    at_ms = 记录时刻：按当时生效的版本段取价（含该段的峰谷与 override 优先级）。
    旧形状价表只有一段恒生效的默认段，时刻（含缺失/不可解析）不影响结果——
    与版本化前逐字段一致。补缺层 price.override.json 没有生效时刻，仍是最高
    优先级的手工修补层（要按时刻分段请写进版本段）。
    """
    table = load_price_table()
    version = version_at(table, at_ms)
    if version is None:
        return (None, 'flat', None, None)
    override = table['override']
    if override:
        if model_id in override:
            hit = override[model_id]
        else:
            canon = canon_model_id(model_id)
            hit = next((v for k, v in override.items() if canon_model_id(k) == canon), None)
        if hit is not None:
            if isinstance(hit, dict) and isinstance(hit.get('target'), str):
                return resolve_price(hit['target'], at_ms)
            entry = normalize_price(hit)
            if entry and (not entry['unpriced']):
                return (entry, 'flat', model_id, 'override')
    best = None
    unpriced_score = None
    for models, billing_mode, source in version['tiers']:
        keys = list(models.keys())
        if model_id in keys:
            hit, exact = (model_id, True)
        else:
            hit = match_model_id(model_id, keys)
            exact = False
        if hit is None:
            continue
        entry = normalize_price(models[hit])
        if entry is None:
            continue
        score = (1000 if exact else 0) + len(canon_model_id(hit))
        if entry['unpriced']:
            if unpriced_score is None or score > unpriced_score:
                unpriced_score = score
            continue
        if best is None or score > best[0]:
            best = (score, entry, billing_mode, hit, source)
    if best is None or (unpriced_score is not None and unpriced_score >= best[0]):
        return (None, 'flat', None, None)
    return (best[1], best[2], best[3], best[4])

def estimate_record(model_id, at_ms, input_tok, output_tok, cache_tok, reasoning_tok):
    """单条记录估算：返回 (cost_usd, priced, matched_model, peak_used)。

    既有 4 元组契约（console_stats 依赖）：按记录时刻所属版本计价，版本标识
    不进返回值；需要版本标识用 estimate_record_with_version。
    """
    cost, priced, matched, peak, _version_id, _rate = _estimate_record_detail(model_id, at_ms, input_tok, output_tok, cache_tok, reasoning_tok)
    return (cost, priced, matched, peak)

def estimate_record_with_version(model_id, at_ms, input_tok, output_tok, cache_tok, reasoning_tok):
    """单条记录估算（版本化契约）：返回 (cost_usd, priced, matched, peak, version_id)。

    分享计量（share_metering.price_fn）的固定签名；version_id 为记录时刻生效
    版本段的标识，旧形状价表/版本未知时为 None。priced=False 表示价格或版本
    未知（cost 为 0 是「未知」的占位，不是真实 0）。
    """
    cost, priced, matched, peak, version_id, _rate = _estimate_record_detail(model_id, at_ms, input_tok, output_tok, cache_tok, reasoning_tok)
    return (cost, priced, matched, peak, version_id)

def _estimate_record_detail(model_id, at_ms, input_tok, output_tok, cache_tok, reasoning_tok):
    """内部估算：返回 (cost_usd, priced, matched, peak, version_id, exchange_rate)。

    exchange_rate 为该记录所属版本的汇率，None 表示版本未知（不折算、不猜
    现价）。版本化价表下记录时刻缺失/不可解析/早于首段时返回
    (0.0, False, None, False, None, None)。
    """
    table = load_price_table()
    version = version_at(table, at_ms)
    if version is None:
        return (0.0, False, None, False, None, None)
    entry, _billing_mode, matched, _source = resolve_price(model_id, at_ms)
    if entry is None:
        return (0.0, False, None, False, version['versionId'], None)
    cache = max(0, cache_tok or 0)
    noncached = max(0, (input_tok or 0) - cache)
    peak_cfg = version['peak']
    in_peak = is_peak_hour(at_ms, peak_cfg['effectiveAtMs'], peak_cfg['windows'])
    tokens = {'input': noncached, 'output': output_tok or 0, 'cache': cache, 'reasoning': reasoning_tok or 0}
    cost = cost_of(tokens, entry, at_ms, peak_cfg)
    return (cost, True, matched, in_peak, version['versionId'], version['exchangeRate'])

def _bucket_price_estimate(rec, model, in_tok, out_tok, cache_tok, reason_tok):
    """_bucket 计价回调（chat2api）：按记录时刻所属版本估算。

    返回 (cost_usd, priced, matched, peak, version_id, rate)；rate 为该记录
    所属版本的汇率，None = 版本未知（不折算、不猜现价）。
    """
    return _estimate_record_detail(model, rec.get('_ms'), in_tok, out_tok, cache_tok, reason_tok)

def _read_jsonl(path, limit=200000):
    """读取 JSONL（容忍尾行残缺），新→旧返回时间升序列表。"""
    records = []
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    rec['_ms'] = _parse_ts(rec.get('time'))
                    records.append(rec)
    except FileNotFoundError:
        return []
    except Exception:
        return records
    records.sort(key=lambda r: r.get('_ms') or 0)
    return records[-limit:]

def _beijing_day(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone(timedelta(hours=8))).strftime('%Y-%m-%d')

def _day_start_ms(day):
    """北京时间某日 00:00 → epoch ms；缺失/不可解析返回 None（不猜）。"""
    try:
        dt = datetime.strptime(str(day), '%Y-%m-%d')
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000

def _usage_known(rec):
    """记录是否带可用用量：任一 token 字段存在且可解析为有限数值即视为已知。

    整条记录一个 token 字段都没有时，用量是「未知」而不是真实 0。取值口径
    （缺字段折 0 参与**取值**）不变，但未知请求不进 token 合计，改由
    unknownUsageRequests 如实计数——0 会被读成真实测得值（C1 2026-09-25）。
    """
    for name in USAGE_TOKEN_FIELDS:
        if name not in rec:
            continue
        value = rec.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return value == value
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            continue
        if number == number:
            return True
    return False

class _Money:
    """USD 小计 + 按版本汇率分组（同一版本的小计只乘一次汇率）。

    旧形状价表只有一段恒生效的默认段，分组恒为单组，换算与版本化前逐位一致：
    服务级沿用既有的「先 round(usd, 6) 再乘汇率」（_bucket 的 costUsd 就是
    该四舍五入值，_round_service 消费的正是它），模型/日级用未取整小计。
    版本化价表下各版本小计各自按自己的汇率换算后相加——历史金额不重算。
    partial=True 表示有记录的金额未纳入本合计（无价格/用量未知/版本未知）；
    此时 cny() 在没有任何可折算记录时返回 None（未知，不折 0）。
    """
    __slots__ = ('usd', 'by_rate', 'partial', 'round_usd')

    def __init__(self, round_usd=False):
        self.usd = 0.0
        self.by_rate = {}
        self.partial = False
        self.round_usd = round_usd

    def add(self, usd, rate):
        self.usd += usd
        if rate is not None:
            self.by_rate[rate] = self.by_rate.get(rate, 0.0) + usd

    def cny(self):
        if not self.by_rate:
            return None if self.partial else 0.0
        if self.round_usd:
            return sum((round(usd, 6) * rate for rate, usd in self.by_rate.items()))
        return sum((usd * rate for rate, usd in self.by_rate.items()))

def _bucket(records, kind, cost_fn):
    """按 (服务, 模型, 日) 聚合。

    cost_fn(rec, model, i, o, c, r) → (cost_usd, priced, matched, peak,
    version_id, rate)；rate 为该记录所属版本的汇率，None = 版本未知
    （不折算、不猜现价）。未知一律显式分列、不折零：用量未知的请求不进
    token 合计（unknownUsageRequests），无价格的进 unpricedRequests，版本
    未知的进 unknownVersionRequests；costCnyPartial=true 表示 costCny 只含
    可折算的记录（下界，不是总额），只有整节点的金额全部无法折算时
    costCny=null（未知，绝不折 0）。既有字段的取值不变：旧形状下服务级
    costCny 与版本化前逐位一致，只有全无价格的模型行从 0 变成 null。
    """
    models, days = ({}, {})
    total = {'requests': 0, 'cost_usd': 0.0, 'input': 0, 'output': 0, 'cache': 0, 'reasoning': 0, 'priced': 0, 'peak_requests': 0, 'unknown_usage': 0, 'unpriced': 0, 'unknown_version': 0}
    money = _Money(round_usd=True)
    model_money = {}
    day_money = {}
    unpriced_models = set()
    versioned = bool(load_price_table().get('versioned'))
    for rec in records:
        ms = rec.get('_ms')
        model = str(rec.get('model') or 'unknown')
        in_tok = int(rec.get('input_tokens') or 0)
        out_tok = int(rec.get('output_tokens') or 0)
        cache_tok = int(rec.get('cached_input_tokens') or rec.get('cache_tokens') or 0)
        reason_tok = int(rec.get('reasoning_tokens') or 0)
        usage_known = _usage_known(rec)
        cost_usd, priced, matched, peak, version_id, rate = cost_fn(rec, model, in_tok, out_tok, cache_tok, reason_tok)
        total['requests'] += 1
        if usage_known:
            total['input'] += in_tok
            total['output'] += out_tok
            total['cache'] += cache_tok
            total['reasoning'] += reason_tok
        else:
            total['unknown_usage'] += 1
        total['cost_usd'] += cost_usd
        if priced:
            total['priced'] += 1
        else:
            unpriced_models.add(model)
            if priced is False:
                total['unpriced'] += 1
                if versioned and version_id is None:
                    total['unknown_version'] += 1
        if not usage_known or priced is False:
            money.partial = True
        money.add(cost_usd, rate)
        if peak:
            total['peak_requests'] += 1
        key = matched or model
        m = models.setdefault(key, {'model': key, 'requests': 0, 'cost_usd': 0.0, 'input': 0, 'output': 0, 'cache': 0, 'reasoning': 0, 'priced': 0, 'peak_requests': 0, 'unknown_usage': 0, 'unpriced': 0, 'unknown_version': 0})
        m_money = model_money.setdefault(key, _Money())
        m['requests'] += 1
        m['cost_usd'] += cost_usd
        if usage_known:
            m['input'] += in_tok
            m['output'] += out_tok
            m['cache'] += cache_tok
            m['reasoning'] += reason_tok
        else:
            m['unknown_usage'] += 1
        if priced:
            m['priced'] += 1
        elif priced is False:
            m['unpriced'] += 1
            if versioned and version_id is None:
                m['unknown_version'] += 1
        if not usage_known or priced is False:
            m_money.partial = True
        m_money.add(cost_usd, rate)
        if peak:
            m['peak_requests'] += 1
        day = _beijing_day(ms)
        if day:
            d = days.setdefault(day, {'day': day, 'requests': 0, 'cost_usd': 0.0})
            d_money = day_money.setdefault(day, _Money())
            d['requests'] += 1
            d['cost_usd'] += cost_usd
            if not usage_known or priced is False:
                d_money.partial = True
            d_money.add(cost_usd, rate)
    for key, m in models.items():
        m['_money'] = model_money[key]
    for day, d in days.items():
        d['_money'] = day_money[day]
    return {'kind': kind, 'requests': total['requests'], 'costUsd': round(total['cost_usd'], 6), 'tokens': {'input': total['input'], 'output': total['output'], 'cache': total['cache'], 'reasoning': total['reasoning']}, 'pricedRequests': total['priced'], 'peakRequests': total['peak_requests'], 'unknownUsageRequests': total['unknown_usage'], 'unpricedRequests': total['unpriced'], 'unknownVersionRequests': total['unknown_version'], 'models': sorted(models.values(), key=lambda m: -m['cost_usd']), 'days': [days[k] for k in sorted(days)], 'unpricedModels': sorted(unpriced_models), '_money': money}

def _round_service(svc, exchange_rate):

    def money(node):
        raw = node.pop('_money', None)
        usd = node.pop('cost_usd', None)
        camel = node.pop('costUsd', None)
        if usd is None:
            usd = camel
        if raw is None:
            node['costCny'] = round((usd or 0.0) * exchange_rate, 4)
            return
        raw_cny = raw.cny()
        node['costCny'] = None if raw_cny is None else round(raw_cny, 4)
        if raw.partial:
            node['costCnyPartial'] = True
    money(svc)
    for m in svc.get('models', []):
        money(m)
    for d in svc.get('days', []):
        money(d)
    return svc
_SUMMARY_CACHE = {'value': None, 'at': 0.0}
_SUMMARY_REFRESH_SECONDS = 300.0
_SUMMARY_LOCK = threading.Lock()

def summary_cached() -> dict:
    """R9-R39（维护者裁定 2026-09-21）：费用聚合打开即看，不等约 30 秒聚合。

    GET 直接返回后台常驻缓存（带 _generated_epoch 截至时点），守护线程定时
    重聚合；失败保留上一次真实值（stale 由前端既有口径标注）。冷启动兜底：
    后台未出结果前首个请求同步聚合一次并写入缓存。
    """
    with _SUMMARY_LOCK:
        value = _SUMMARY_CACHE['value']
        if value is not None:
            stamped = dict(value)
            stamped['_generated_epoch'] = _SUMMARY_CACHE['at']
            stamped['_cached_age_seconds'] = round(time.time() - _SUMMARY_CACHE['at'], 1)
            return stamped
    try:
        built = build_summary()
    except Exception as exc:
        print('[usagesrv] summary build failed: %s' % type(exc).__name__, flush=True)
        return {'error': 'summary unavailable'}
    with _SUMMARY_LOCK:
        _SUMMARY_CACHE['value'] = built
        _SUMMARY_CACHE['at'] = time.time()
    result = dict(built)
    result['_generated_epoch'] = _SUMMARY_CACHE['at']
    result['_cached_age_seconds'] = 0.0
    return result

def _summary_refresh_worker() -> None:
    """Boot-time warm-up then periodic refresh; failure keeps the cache."""
    while True:
        try:
            built = build_summary()
            with _SUMMARY_LOCK:
                _SUMMARY_CACHE['value'] = built
                _SUMMARY_CACHE['at'] = time.time()
        except Exception as exc:
            print('[usagesrv] summary refresh failed: %s' % type(exc).__name__, flush=True)
        time.sleep(_SUMMARY_REFRESH_SECONDS)

def _start_summary_refresh_thread() -> None:
    threading.Thread(target=_summary_refresh_worker, name='summary-refresh', daemon=True).start()

def _summary_cache_reset_for_tests() -> None:
    with _SUMMARY_LOCK:
        _SUMMARY_CACHE['value'] = None
        _SUMMARY_CACHE['at'] = 0.0

def build_summary():
    table = load_price_table()
    rate = table['exchangeRate']
    data = _bucket(_read_jsonl(CHAT2API_LOG), 'estimate', _bucket_price_estimate)
    _round_service(data, rate)
    peak = table['peak']
    return {'generatedAt': datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds'), 'exchangeRate': rate, 'currency': 'CNY', 'sources': {'chat2apiLog': _display_path(CHAT2API_LOG), 'priceMeta': table['meta']}, 'peak': {'enabled': peak['enabled'], 'effectiveAt': peak.get('effectiveAt'), 'windows': [{'beijing': f"{(w['start'] + 8) % 24:02d}:00-{(w['end'] + 8) % 24:02d}:00", 'rate': 2.0} for w in peak['windows']], 'weekendOffpeak': True, 'note': '费用按记录时刻对应的参考价目表估算'}, 'priceVersions': {'versioned': table['versioned'], 'invalidCount': table['versionMeta']['invalidCount'], 'current': table['versionMeta']['current'], 'versions': [{'versionId': v['versionId'], 'effectiveAt': v['effectiveAt'], 'exchangeRate': v['exchangeRate'], 'checkedAt': v['checkedAt'], 'checkedStale': v['checkedAgeDays'] > PRICE_STALE_DAYS if v['checkedAgeDays'] is not None else None} for v in table['versions']]}, 'services': {'chat2api': data}}
SHARE_USAGE_LIMIT = 200000
SHARE_BACKEND_DIR_ENV = 'CONSOLE_SHARE_BACKEND_DIR'
SHARE_USAGE_LEDGER_NAME = 'usage-share-backend.jsonl'

def share_usage_ledger_path():
    """分享专用后端 8110 的用量账本路径。

    解析口径与 console_share_production._backend_dir / console_log_reader 一致：
    CONSOLE_SHARE_BACKEND_DIR（须绝对路径）覆盖，否则 %LOCALAPPDATA% 下的
    AIGatewayPublic/share-backend；解析不出返回 None（不猜路径）。"""
    override = os.environ.get(SHARE_BACKEND_DIR_ENV)
    if override:
        if not os.path.isabs(override):
            return None
        return os.path.join(override, SHARE_USAGE_LEDGER_NAME)
    local = os.environ.get('LOCALAPPDATA')
    if not local:
        return None
    return os.path.join(local, 'AIGatewayPublic', 'share-backend', SHARE_USAGE_LEDGER_NAME)

def share_usage_reader():
    """受保护的延迟导入 share_metering.read_share_usage。

    返回 (reader, error)：模块不存在/导入失败/接口缺失 → (None, 错误码)，
    导入失败只打印类型名，不抛出——服务照常运行。"""
    try:
        import share_metering
    except Exception as exc:
        print(f'[usagesrv] share_metering unavailable: {type(exc).__name__}', flush=True)
        return (None, 'share_metering_unavailable')
    reader = getattr(share_metering, 'read_share_usage', None)
    if not callable(reader):
        return (None, 'share_metering_unavailable')
    return (reader, None)
SESSION_COOKIE = 'usage_session'
SESSION_TTL_SECONDS = 12 * 3600
SESSION_CODE_TTL_SECONDS = 600
SESSION_CODE_FILE = os.environ.get('USAGE_SESSION_CODE_FILE') or os.path.join(HERE, 'data', 'usage-session-code')
CONTROL_TOKEN_FILE = os.environ.get('USAGE_CONTROL_TOKEN_FILE') or os.path.join(HERE, 'data', 'admin-control-token')
_session_lock = threading.Lock()
_session = {'code': None, 'code_expires': 0.0, 'session': None, 'session_expires': 0.0}

def _generate_session_code_locked(now):
    _session['code'] = secrets.token_urlsafe(24)
    _session['code_expires'] = now + SESSION_CODE_TTL_SECONDS
    try:
        os.makedirs(os.path.dirname(os.path.abspath(SESSION_CODE_FILE)), exist_ok=True)
        with open(SESSION_CODE_FILE, 'w', encoding='utf-8') as f:
            f.write(_session['code'])
    except OSError:
        pass

def _ensure_session_code_locked(now):
    if _session['code'] is None or now > _session['code_expires']:
        _generate_session_code_locked(now)

def session_bootstrap_code():
    with _session_lock:
        _ensure_session_code_locked(time.time())
        return _session['code']

def reissue_session_code(control_token):
    """控制台重新申请引导码：须持管理令牌；旧码（含未过期）立即作废。"""
    if not control_token_ok(control_token):
        return None
    with _session_lock:
        _generate_session_code_locked(time.time())
        return _session['code']

def session_exchange(code):
    """一次性引导码换取会话标识：成功即原子消费（重放被拒），
    会话时效自本次兑换起算；码过期/已消费/不符返回 None。"""
    with _session_lock:
        now = time.time()
        current = _session['code']
        if not code or not current or now > _session['code_expires']:
            return None
        if not secrets.compare_digest(str(code), current):
            return None
        _session['code'] = None
        _session['code_expires'] = 0.0
        _session['session'] = secrets.token_urlsafe(32)
        _session['session_expires'] = now + SESSION_TTL_SECONDS
        return _session['session']

def session_valid(cookie_header):
    with _session_lock:
        expected = _session['session']
        expires = _session['session_expires']
        if not expected or time.time() > expires:
            return False
    if not cookie_header:
        return False
    try:
        jar = SimpleCookie()
        jar.load(cookie_header)
        morsel = jar.get(SESSION_COOKIE)
        return bool(morsel) and secrets.compare_digest(morsel.value, expected)
    except Exception:
        return False

def control_token_ok(supplied):
    """程序化访问通道：与 chat2api 共用本机管理令牌（data/admin-control-token）。"""
    try:
        with open(CONTROL_TOKEN_FILE, encoding='utf-8') as f:
            expected = f.read().strip()
    except OSError:
        return False
    return bool(expected) and bool(supplied) and secrets.compare_digest(supplied, expected)

def summary_access_allowed(cookie_header, control_token):
    return session_valid(cookie_header) or control_token_ok(control_token)
CONSOLE_STATE_FILE = os.environ.get('CONSOLE_STATE_FILE') or os.path.join(HERE, 'data', 'console-state.sqlite3')
CONSOLE_LOCK_DIR = os.environ.get('CONSOLE_LOCK_DIR') or os.path.join(HERE, 'data', 'console-locks')
CONSOLE_STATUS_TTL = 2.0
_console_manager_lock = threading.Lock()
_console_manager = None
_console_status_lock = threading.Lock()
_console_status_cache = {'at': 0.0, 'value': None}

def get_console_manager():
    """Lazily build the console operation manager (never at import time)."""
    global _console_manager
    with _console_manager_lock:
        if _console_manager is None:
            import console_adapters
            import console_operations
            console_adapters.wire_share_production_seams(keys_manager=get_share_manager())
            _console_manager = console_operations.OperationManager(db_path=CONSOLE_STATE_FILE, registry=console_adapters.build_registry(), lock_dir=CONSOLE_LOCK_DIR)
        return _console_manager

def console_services_snapshot():
    """Read-only services snapshot with a small TTL cache (wrapper-backed)."""
    now = time.time()
    with _console_status_lock:
        cached = _console_status_cache['value']
        if cached is not None and now - _console_status_cache['at'] < CONSOLE_STATUS_TTL:
            return (cached, True, round(now - _console_status_cache['at'], 1))
    import console_adapters
    payload = console_adapters.service_status()
    with _console_status_lock:
        _console_status_cache['at'] = now
        _console_status_cache['value'] = payload
    return (payload, False, 0.0)
_CONSOLE_HEALTH_TTL_S = 30
_console_health_lock = threading.Lock()
_console_health_cache = {'at': 0.0, 'payload': None}
_CONSOLE_HEALTH_PROBES = ({'id': 'chat2api', 'port': 8000, 'path': None},)

def _quota_snapshot_payload(force=False):
    return {'available': False, 'reason': '上游未提供可信账户余额接口'}

def _probe_service(spec):
    """One bounded loopback probe: TCP connect, then a bounded HTTP GET.

    Connection refused = not_listening; ANY HTTP answer = listening (the
    service itself answered); TCP accepted but HTTP failed = listening
    with an honest note. Probes are read-only against the service roots
    and never trigger login/refresh flows."""
    import http.client
    import socket
    started = time.perf_counter()

    def _ms():
        return int((time.perf_counter() - started) * 1000)
    try:
        sock = socket.create_connection(('127.0.0.1', spec['port']), timeout=1.5)
    except socket.timeout:
        return {'id': spec['id'], 'port': spec['port'], 'state': 'unknown', 'note': 'TCP 连接超时（探测未定，非未监听）', 'ms': _ms()}
    except OSError:
        return {'id': spec['id'], 'port': spec['port'], 'state': 'not_listening', 'ms': _ms()}
    sock.close()
    if spec.get('path') is None:
        return {'id': spec['id'], 'port': spec['port'], 'state': 'listening', 'http_status': None, 'note': 'TCP 端口可达（未做 HTTP 探测）', 'ms': _ms()}
    try:
        conn = http.client.HTTPConnection('127.0.0.1', spec['port'], timeout=2.0)
        conn.request('GET', spec['path'], headers={'Accept': 'application/json, */*'})
        resp = conn.getresponse()
        status = resp.status
        resp.read(256)
        conn.close()
        return {'id': spec['id'], 'port': spec['port'], 'state': 'listening', 'http_status': status, 'ms': _ms()}
    except Exception:
        return {'id': spec['id'], 'port': spec['port'], 'state': 'listening', 'http_status': None, 'note': 'tcp accepted but http probe failed', 'ms': _ms()}

def _console_health_payload(force):
    now = time.time()
    with _console_health_lock:
        cached = _console_health_cache['payload']
        if not force and cached is not None and (now - _console_health_cache['at'] < _CONSOLE_HEALTH_TTL_S):
            out = dict(cached)
            out['cached'] = True
            return (out, True)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        services = list(pool.map(_probe_service, _CONSOLE_HEALTH_PROBES))
    services.append({'id': 'usagesrv', 'port': 8096, 'state': 'listening', 'note': 'console host itself'})
    payload = {'checked_at': int(time.time()), 'cached': False, 'services': services}
    with _console_health_lock:
        _console_health_cache['at'] = now
        _console_health_cache['payload'] = payload
    return (dict(payload), False)

def _console_error_status(exc):
    """Map task-layer rejections to HTTP status codes."""
    code = getattr(exc, 'code', '')
    if code == 'invalid_action':
        return (code, 404)
    if code == 'action_disabled':
        return (code, 403)
    if code in ('invalid_params', 'invalid_target', 'invalid_idempotency_key', 'invalid_resource'):
        return (code, 400)
    if code == 'idempotency_key_conflict':
        return (code, 409)
    if code == 'operation_not_found':
        return (code, 404)
    if code == 'operation_already_finished':
        return (code, 409)
    return ('console_error', 500)
_CONSOLE_ERROR_DETAIL_CODES = frozenset(('invalid_params', 'invalid_target', 'invalid_idempotency_key', 'invalid_resource'))
_CONSOLE_ERROR_DETAIL_MAX = 200
_CONSOLE_ERROR_DETAIL_UNSAFE = re.compile('[\\\\/]|[A-Za-z]:|[A-Za-z0-9_.-]{24,}')

def _console_error_detail(exc):
    """受控的拒绝说明；不适用或不安全时返回 None（宁缺勿猜）。"""
    if getattr(exc, 'code', '') not in _CONSOLE_ERROR_DETAIL_CODES:
        return None
    message = getattr(exc, 'message', None)
    if not isinstance(message, str):
        return None
    text = ' '.join(message.split())
    if not text or _CONSOLE_ERROR_DETAIL_UNSAFE.search(text):
        return None
    return text[:_CONSOLE_ERROR_DETAIL_MAX]
_share_manager_lock = threading.Lock()
_share_manager = None
SHARE_MANAGEMENT_CONFIG_FILE = os.environ.get('SHARE_MANAGEMENT_CONFIG') or os.path.join(HERE, 'data', 'share-management.json')

def _load_share_management_deployment():
    """Consume the explicit management deployment config and build the real
    broker client for the /api/share/* command layer.

    Fail-closed contract (§210.5/§214.2):
    * absent config file or explicit ``enabled: false`` -> None (NoopBroker;
      status honestly reports 未安装/未授权 and revoking mutations refuse);
    * a PRESENT but corrupt/unusable config (bad JSON, wrong schema,
      missing/garbage enabled flag, malformed pipe name, unreadable or
      oversized control key) -> BrokerError. The share API must never
      quietly fall back to NoopBroker while a config claims to be enabled;
    * the BrokerClient receives a transport FACTORY (a fresh
      NamedPipeBrokerTransport per exchange), never a transport instance —
      the client calls factory().open() for every single exchange.
    """
    path = SHARE_MANAGEMENT_CONFIG_FILE
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            payload = json.load(f)
    except (OSError, ValueError) as exc:
        raise BrokerError(f'share management config unreadable: {exc}')
    if not isinstance(payload, dict):
        raise BrokerError('share management config must be an object')
    schema = payload.get('schema_version')
    if isinstance(schema, bool) or schema != 1:
        raise BrokerError(f'unsupported share management config schema {schema!r}')
    enabled = payload.get('enabled')
    if not isinstance(enabled, bool):
        raise BrokerError("share management config 'enabled' must be an explicit boolean (never inferred)")
    if enabled is False:
        return None
    pipe_prefix = '\\\\.\\pipe\\'
    pipe_name = payload.get('pipe_name')
    if not isinstance(pipe_name, str) or not pipe_name.startswith(pipe_prefix) or len(pipe_name) <= len(pipe_prefix):
        raise BrokerError('share management config pipe_name must be a named-pipe path (\\\\.\\pipe\\<name>)')
    key_file = payload.get('control_key_file')
    if not isinstance(key_file, str) or not key_file:
        raise BrokerError('share management config control_key_file missing')
    from share_broker_client import BrokerClient, NamedPipeBrokerTransport
    from share_broker_server import read_control_key
    try:
        secret = read_control_key(key_file)
    except (OSError, ValueError) as exc:
        raise BrokerError(f'share management control key unusable: {exc}')
    client = BrokerClient(lambda: NamedPipeBrokerTransport(pipe_name), secret)
    return BrokerClientAdapter(client)

def get_share_manager():
    global _share_manager
    with _share_manager_lock:
        if _share_manager is None:
            broker = _load_share_management_deployment()
            _share_manager = ShareManager() if broker is None else ShareManager(broker=broker)
        return _share_manager

def _display_path(path):
    """页面只显示文件名；绝对路径不跨进程泄露（本机管理边界，ADR 网络边界）。"""
    name = os.path.basename(str(path or ''))
    return name or '(未配置)'

def _hostname_of(host_header):
    try:
        parts = urlparse(f'//{host_header}')
        hostname = (parts.hostname or '').lower()
        if hostname:
            parts.port
        return hostname
    except ValueError:
        return ''

def host_allowed(host_header):
    """DNS rebinding 防护：本服务的 Host 只能是回环名。"""
    return _hostname_of(host_header) in {'127.0.0.1', 'localhost', '::1'}

def origin_allowed(origin):
    """带 Origin 的请求只能来自本机页面，且端口必须与本服务一致。

    缺省端口（隐含 80）与非法端口（urlparse 抛 ValueError）都不属于本服务，
    一律拒绝；本服务是 http://127.0.0.1:8096，浏览器总会带端口。
    """
    if not origin:
        return True
    try:
        parts = urlparse(origin)
        port = parts.port
    except ValueError:
        return False
    return parts.scheme == 'http' and parts.hostname in {'127.0.0.1', 'localhost'} and (port == PORT)

def _log_int_or_none(value):
    """调用日志数值字段 → int；缺失/不可解析一律 None（未知），绝不折零。

    /api/console/logs 专用（F2）：账本没写该字段、写了 null、写了空串或写了
    非数值时，一律以 null 披露「未采集/未知」，让前端按未知渲染，而不是折成
    0（0 会被读成真实测得值，如「缺输出但耗时有效」的请求会显示 0 Token/s）。
    bool 不是计数；非整数浮点数与不可解析字符串同样一律 None（不四舍五入）。
    真实 0 是已知值：字段里写了 0 就返回 0。
    """
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
_LOG_FEED_SHAPES = {'usage_ledger': {'time': ('time',), 'model': ('model',), 'status': ('status',), 'input': ('input_tokens',), 'output': ('output_tokens',), 'cached_input': ('cached_input_tokens',), 'reasoning': ('reasoning_tokens',), 'duration': ('duration_ms',), 'credits': ('credits',)}}

def _console_log_readers():
    return {'chat2api': (CHAT2API_LOG, 'usage_ledger')}

def _console_log_row(source, record, shape='usage_ledger'):
    """一条账本记录 → /api/console/logs 行（F2：数值缺失一律 null，绝不折零）。

    账本没写该字段、写了 null、写了空串或写了非数值时，一律以 null 披露
    「未采集/未知」，而不是折成 0——0 会被前端读成真实测得值（例如缺输出
    Token 但耗时有效的请求会显示 0 Token/s）。真实 0 是已知值：写了 0 就是 0。
    字段名按 shape（各链账本的写入形状，见 _LOG_FEED_SHAPES）取值；形状里
    没有的字段（如国际日志的 input_tokens/credits）同样一律 null。
    credits 保持既有 null 语义（非 int/float 一律 null，本函数不改）。
    """
    fields = _LOG_FEED_SHAPES.get(shape) or _LOG_FEED_SHAPES['usage_ledger']

    def raw(key):
        for name in fields[key]:
            if name in record:
                return record[name]
        return None
    credits = raw('credits')
    return {'source': source, 'time': str(raw('time') or ''), 'model': str(raw('model') or ''), 'status': str(raw('status') or ''), 'input_tokens': _log_int_or_none(raw('input')), 'output_tokens': _log_int_or_none(raw('output')), 'cached_input_tokens': _log_int_or_none(raw('cached_input')), 'reasoning_tokens': _log_int_or_none(raw('reasoning')), 'duration_ms': _log_int_or_none(raw('duration')), 'credits': credits if isinstance(credits, (int, float)) else None}

def _usage_rates_recorded_requests(payload):
    """按来源的「已记录请求数」（F5b）：/api/console/usage-rates 的补充计数。

    口径=账本记录数，不是速率分桶数：窗口内该来源被账本记录的每个请求都计入
    ——含无法计算速率的记录（缺输出 Token / 缺耗时 / 无效耗时），但**不含**
    时间戳无效（无法证明落在窗口内）与窗口外的记录。因此它 ≥ 该来源速率分桶的
    请求数之和，「有记录但均无法计算速率」时是真实的大于 0。

    窗口取响应自身的 coverage.hours 与 checked_epoch（与 hours[] 分桶同一窗口，
    不另算一次当前时刻）；事件读取复用 console_stats 的规范化读取层（同一
    _SOURCE_SPECS 字段映射、同一增量缓存与读取上限），不另造第二套账本解析：
    缓存命中时不再解析文件，只多一次 O(事件数) 的计数遍历。

    未知（None，绝不折 0）的三种情形：账本缺席/未读取（ledger_present=false）；
    本次读取失败（coverage errors[].type="read_error"——读取层会退回上次缓存的事件，
    旧缓存不是本次读数）；快照/账本结构损坏（type="corrupt_ledger"——事件集可能
    为空或不完整）。两者的判定取 console_stats 的单点定义（_events_trusted /
    UNTRUSTED_EVENT_ERRORS），本模块不另写一份字面量。账本已读取且事件集可信、
    但窗口内无记录为真实的 0（零记录 ≠ 无记录）；malformed_lines（部分坏行、
    其余仍可读）照常计数。
    """
    import console_stats
    coverage = payload.get('coverage') or {}
    hours = coverage.get('hours')
    checked_epoch = payload.get('checked_epoch')
    if not isinstance(hours, int) or not isinstance(checked_epoch, int):
        return {}
    now_ms = checked_epoch * 1000
    window_start = console_stats._window_hour_starts(hours, now_ms)[0]
    window_end = window_start + hours * 3600000
    per_source, source_coverage = console_stats._scan_sources(console_stats.default_sources())
    counts = {}
    for source, entry in per_source.items():
        entry_coverage = source_coverage.get(source) or {}
        if not entry_coverage.get('ledger_present'):
            counts[source] = None
            continue
        if not console_stats._events_trusted(entry_coverage):
            counts[source] = None
            continue
        counts[source] = sum((1 for ev in entry['events'] if ev.ms is not None and window_start <= ev.ms < window_end))
    return counts
USAGE_ALIAS_PREFIX = '/gw/usagesrv'
_DISCARD_MAX_BYTES = 1 << 20
_DISCARD_TIMEOUT_S = 2.0

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _discard_unread_body(self):
        """Consume a rejected request body before the reply goes out.

        The body was not read (oversize, or an unusable Content-Length), so the
        socket still holds unread received data; closing such a connection makes
        Windows answer the client's next read with RST, and the client sees a
        connection abort instead of this 413/400. Read and discard a bounded
        amount, with a socket timeout so a silent client cannot hold the handler.
        """
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1:
            return
        try:
            remaining = int(lengths[0])
        except (TypeError, ValueError):
            return
        if remaining <= 0:
            return
        remaining = min(remaining, _DISCARD_MAX_BYTES)
        try:
            self.connection.settimeout(_DISCARD_TIMEOUT_S)
        except OSError:
            return
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.connection.settimeout(None)
            except OSError:
                pass

    def _rewrite_usage_alias(self):
        """Rewrite the /gw/usagesrv prefix alias onto the native path.

        Mirrors the 5173 dev-proxy rewrite (web/vite.config.ts): strip
        the prefix, keep the rest of the path AND the query string intact
        ("/gw/usagesrv/api/summary?x=1" -> "/api/summary?x=1"; the bare
        prefix maps to "/"). Non-alias paths pass through unchanged, and
        "/gw/usagesrvfoo" is NOT an alias — the prefix must be a full path
        segment. Each do_* method applies this before dispatch, so every
        route (including the protected session/share surfaces) is reachable
        through the alias with identical semantics and gates.
        """
        parsed = urlparse(self.path)
        if parsed.path == USAGE_ALIAS_PREFIX:
            return '/' + self.path[len(USAGE_ALIAS_PREFIX):]
        if parsed.path.startswith(USAGE_ALIAS_PREFIX + '/'):
            return self.path[len(USAGE_ALIAS_PREFIX):]
        return self.path

    def _serve_console_static(self, path):
        """Serve a build-output file under web/dist.

        The dist tree is this repository's own artifact, so any regular file
        inside it is servable; the guard is traversal-proofing — the resolved
        target must stay inside the dist root. Missing files and directory
        paths (no listing) get the same honest "unknown path" 404 as the rest
        of the non-API surface. Returns True once a static response (200 or
        404) has been written; False means "/" ownership stays with the root
        branch.
        """
        name = path.strip('/')
        if not name:
            return False
        root = os.path.realpath(CONSOLE_DIST_DIR)
        try:
            target = os.path.realpath(os.path.join(root, name))
            inside = os.path.commonpath([root, target]) == root
        except ValueError:
            inside = False
        if not inside or not os.path.isfile(target):
            self._send_json(404, {'error': 'unknown path'})
            return True
        ctype = CONSOLE_STATIC_TYPES.get(os.path.splitext(target)[1].lower(), 'application/octet-stream')
        with open(target, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)
        return True

    def _management_auth(self, write):
        """Management session/token gate for local management APIs.

        Reads may use the HttpOnly usage session or the local control token.
        Writes are allowed with the control token (CLI/control plane). A cookie
        session may also write only when the caller supplies the same-origin
        JavaScript CSRF marker (X-Requested-With). The route layer has already
        rejected foreign Origins and non-loopback Hosts. Shared by the
        /api/share/* and /api/console/* surfaces.
        """
        if control_token_ok(self.headers.get('X-Admin-Control')):
            return (True, 'control')
        if not session_valid(self.headers.get('Cookie')):
            return (False, 'no_credentials')
        if write and self.headers.get('X-Requested-With') != 'XMLHttpRequest':
            return (False, 'csrf_required')
        return (True, 'session')

    def _share_auth(self, write):
        return self._management_auth(write)
    SINGLE_VALUE_HEADERS = ('Content-Type', 'Content-Length', 'Authorization', 'Cookie', 'X-Admin-Control', 'X-Requested-With', 'Transfer-Encoding', 'Content-Encoding')

    def _duplicate_header_error(self):
        """Return the offending duplicate sensitive header name, or None.

        Only REPORTS: the route layer sends the single 400 response (R7-a:
        exactly one response per request — the previous in-place send plus
        the route's own 400 produced two responses on one connection).
        """
        for name in self.SINGLE_VALUE_HEADERS:
            if len(self.headers.get_all(name, [])) > 1:
                return f'duplicate header: {name}'
        return None

    def _read_share_json_body(self):
        if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Encoding'):
            return (None, 400, 'unsupported_body_encoding')
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) > 1:
            return (None, 400, 'invalid_content_length')
        try:
            length = int(lengths[0]) if lengths else 0
        except ValueError:
            return (None, 400, 'invalid_content_length')
        if not 0 <= length <= 65536:
            return (None, 413, 'request_too_large')
        content_types = self.headers.get_all('Content-Type', [])
        if length > 0 or content_types:
            if len(content_types) > 1 or not content_types:
                return (None, 400, 'content_type_required')
            ctype = content_types[0].split(';')[0].strip().lower()
            if ctype != 'application/json':
                return (None, 400, 'content_type_required')
        raw = self.rfile.read(length)
        if len(raw) != length:
            return (None, 400, 'incomplete_body')
        try:
            obj = json.loads(raw) if raw else {}
        except (ValueError, UnicodeError):
            return (None, 400, 'invalid_json')
        if obj is None:
            obj = {}
        if not isinstance(obj, dict):
            return (None, 400, 'invalid_json')
        return (obj, None, None)

    def _reject_unknown_fields(self, body, allowed):
        unknown = set(body) - set(allowed)
        if unknown:
            self._send_json(400, {'error': f'unknown field(s): {sorted(unknown)}'})
            return True
        return False

    def _share_error(self, exc):
        self._send_json(status_code_for(exc), {'error': str(exc)})

    def _share_usage_response(self):
        """分享用量只读查询的唯一构造点：返回 (status, payload)。

        两个只读挂载点共用这一份数据与口径，绝不建第二套账本或第二套聚合：

        * ``GET /api/share/usage`` —— share 管理面（``_share_auth``：会话或
          管理令牌），公网管理面鉴权不变；
        * ``GET /api/console/share-usage`` —— 控制台面（R9-R35 本机同源页面
          免凭据直读，本地边界由路由层的 Host/Origin 门承担）。

        share_metering 缺席、账本路径解析不出、读取失败一律如实返回错误码，
        绝不以 0 请求 / 0 费用冒充。price_fn 是版本化计价器（固定签名
        (model_id, at_ms, input_tok, output_tok, cache_tok, reasoning_tok) →
        (cost_usd, priced, matched, peak, version_id)），因此分享参考费用与
        个人费用同一条版本链。"""
        reader, error = share_usage_reader()
        if reader is None:
            return (503, {'error': error})
        ledger = share_usage_ledger_path()
        if not ledger:
            return (503, {'error': 'share_ledger_path_unresolved'})
        try:
            payload = reader(ledger, estimate_record_with_version, limit=SHARE_USAGE_LIMIT)
        except Exception as exc:
            print(f'[usagesrv] share usage error: {type(exc).__name__}: {exc}', flush=True)
            return (502, {'error': 'share_usage_unavailable'})
        if not isinstance(payload, dict):
            return (502, {'error': 'share_usage_invalid'})
        return (200, self._share_usage_project_key_names(payload))

    def _share_usage_project_key_names(self, payload):
        """Rev8 B8-6 加性投影：每个 Key 的**白名单**名称与删除状态。

        账本侧（``share_metering``）按设计不依赖管理库，所以名称在这里、
        读面的唯一构造点只用 ``read_key_labels`` 的只读 SQLite 连接：
        包括删除行，但只 SELECT key_id/name/deleted_at，不创建数据库、不迁移，
        更不装配有恢复副作用的 ShareManager/broker。轮换仍使用稳定 key_id。
        密钥明文、密文与哈希绝不进入这个形状。
        管理库读不到**不是**费用读取失败的理由：此时每个名称/删除状态如实
        为 ``None``（未知，绝不当空名或 False），``coverage.keyNames`` 记
        下这个状态。没有 Key 条目的载荷根本不读管理库。本投影只读，绝不写
        分享或个人账本。
        """
        keys = payload.get('keys')
        if not isinstance(keys, list):
            return payload
        wanted = set()
        for entry in keys:
            if not isinstance(entry, dict):
                continue
            key_id = entry.get('key_id')
            if isinstance(key_id, str) and key_id:
                wanted.add(key_id)
        rows = None
        if wanted:
            try:
                path = os.environ.get('SHARE_STORE_FILE') or os.path.join(HERE, 'data', 'share-store.db')
                rows = read_key_labels(path, wanted)
            except (StoreError, OSError, ValueError):
                rows = None
        for entry in keys:
            if not isinstance(entry, dict):
                continue
            row = None
            key_id = entry.get('key_id')
            if isinstance(rows, dict) and isinstance(key_id, str):
                row = rows.get(key_id)
            if row is None:
                entry['keyName'] = None
                entry['deleted'] = None
                continue
            name = row.get('name')
            entry['keyName'] = name if isinstance(name, str) else None
            entry['deleted'] = row.get('deleted_at') is not None
        coverage = payload.get('coverage')
        if isinstance(coverage, dict):
            coverage['keyNames'] = None if rows is None else len(rows)
        return payload

    def _share_api(self, parsed, method):
        dup = self._duplicate_header_error()
        if dup is not None:
            self.close_connection = True
            self._send_json(400, {'error': dup})
            return
        ok, how = self._share_auth(method in ('POST', 'PATCH', 'DELETE'))
        if not ok:
            self.close_connection = True
            if how == 'csrf_required':
                self._send_json(403, {'error': 'session CSRF marker required'})
            else:
                self._send_json(401, {'error': 'share management credentials required'})
            return
        path = parsed.path
        segments = [s for s in path.split('/') if s]
        if len(segments) < 3 or segments[:2] != ['api', 'share']:
            self._send_json(404, {'error': 'unknown path'})
            return
        rest = segments[2:]
        if method == 'GET' and rest == ['usage']:
            status, payload = self._share_usage_response()
            self._send_json(status, payload)
            return
        try:
            manager = get_share_manager()
            if method == 'GET' and rest == ['status']:
                self._send_json(200, manager.status())
                return
            if method == 'GET' and rest == ['keys']:
                self._send_json(200, manager.list_keys())
                return
            if method == 'GET' and len(rest) == 2 and (rest[0] == 'keys'):
                self._send_json(200, manager.get_key(rest[1]))
                return
            if method == 'GET' and len(rest) == 2 and (rest[0] == 'operations'):
                self._send_json(200, manager.get_operation(rest[1]))
                return
            body, body_code, body_error = self._read_share_json_body()
            if body_code is not None:
                self.close_connection = True
                self._discard_unread_body()
                self._send_json(body_code, {'error': body_error})
                return

            def _bad(message):
                self._send_json(400, {'error': message})
                return True
            if method == 'POST' and rest == ['keys']:
                if self._reject_unknown_fields(body, ('name', 'idempotency_key', 'max_concurrency')):
                    return
                name = body.get('name')
                idem = body.get('idempotency_key')
                if not isinstance(name, str) or not 1 <= len(name) <= 64:
                    _bad('invalid name')
                    return
                if idem is not None and (not isinstance(idem, str) or not 1 <= len(idem) <= 128):
                    _bad('invalid idempotency_key')
                    return
                max_concurrency = body.get('max_concurrency')
                if max_concurrency is not None:
                    if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or (not 1 <= max_concurrency <= 64):
                        _bad('max_concurrency must be an integer in 1..64')
                        return
                self._send_json(201, manager.create_key(name, max_concurrency=max_concurrency, idempotency_key=idem))
                return
            if method == 'POST' and len(rest) == 3 and (rest[0] == 'keys') and (rest[2] == 'revoke-and-terminate'):
                if self._reject_unknown_fields(body, ('idempotency_key', 'expected_row_version')):
                    return
                if len(rest[1]) > 128:
                    _bad('invalid key_id')
                    return
                idem = body.get('idempotency_key')
                if idem is not None and (not isinstance(idem, str) or not 1 <= len(idem) <= 128):
                    _bad('invalid idempotency_key')
                    return
                expected = body.get('expected_row_version')
                if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
                    _bad('expected_row_version must be a positive integer')
                    return
                self._send_json(200, manager.revoke_and_terminate_key(rest[1], expected_row_version=expected, idempotency_key=idem))
                return
            if method == 'POST' and len(rest) == 3 and (rest[0] == 'keys') and (rest[2] == 'rotate'):
                if self._reject_unknown_fields(body, ('idempotency_key', 'expected_row_version')):
                    return
                if len(rest[1]) > 128:
                    _bad('invalid key_id')
                    return
                idem = body.get('idempotency_key')
                if idem is not None and (not isinstance(idem, str) or not 1 <= len(idem) <= 128):
                    _bad('invalid idempotency_key')
                    return
                expected = body.get('expected_row_version')
                if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
                    _bad('expected_row_version required')
                    return
                self._send_json(200, manager.rotate_key(rest[1], idempotency_key=idem, expected_row_version=expected))
                return
            if method == 'PATCH' and len(rest) == 2 and (rest[0] == 'keys'):
                if self._reject_unknown_fields(body, ('name', 'enabled', 'max_concurrency', 'expected_row_version')):
                    return
                if len(rest[1]) > 128:
                    _bad('invalid key_id')
                    return
                name = body.get('name')
                enabled = body.get('enabled')
                max_concurrency = body.get('max_concurrency')
                expected = body.get('expected_row_version')
                if name is not None and (not isinstance(name, str) or not 1 <= len(name) <= 64):
                    _bad('invalid name')
                    return
                if enabled is not None and (not isinstance(enabled, bool)):
                    _bad('enabled must be bool')
                    return
                if max_concurrency is not None:
                    if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or (not 1 <= max_concurrency <= 64):
                        _bad('max_concurrency must be an integer in 1..64')
                        return
                if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
                    _bad('expected_row_version required')
                    return
                self._send_json(200, manager.update_key(rest[1], name=name, enabled=enabled, max_concurrency=max_concurrency, expected_row_version=expected))
                return
            if method == 'PATCH' and rest == ['settings']:
                if self._reject_unknown_fields(body, ('share_max_concurrency', 'expected_share_max')):
                    return
                share_max = body.get('share_max_concurrency')
                if isinstance(share_max, bool) or not isinstance(share_max, int) or (not 1 <= share_max <= 64):
                    _bad('share_max_concurrency must be an integer in 1..64')
                    return
                expected_max = body.get('expected_share_max')
                if expected_max is not None and (isinstance(expected_max, bool) or not isinstance(expected_max, int)):
                    _bad('expected_share_max must be an integer')
                    return
                self._send_json(200, manager.set_share_limits(share_max, expected_share_max=expected_max))
                return
            if method == 'DELETE' and len(rest) == 2 and (rest[0] == 'keys'):
                if self._reject_unknown_fields(body, ('expected_row_version',)):
                    return
                if len(rest[1]) > 128:
                    _bad('invalid key_id')
                    return
                expected = body.get('expected_row_version')
                if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
                    _bad('expected_row_version required')
                    return
                self._send_json(200, manager.delete_key(rest[1], expected_row_version=expected))
                return
            if method == 'POST' and rest in (['start'], ['stop'], ['emergency-stop']):
                if self._reject_unknown_fields(body, ('idempotency_key',)):
                    return
                idem = body.get('idempotency_key')
                if idem is not None and (not isinstance(idem, str) or not 1 <= len(idem) <= 128):
                    _bad('invalid idempotency_key')
                    return
                if rest == ['start']:
                    self._send_json(202, manager.start(idempotency_key=idem))
                elif rest == ['stop']:
                    self._send_json(202, manager.graceful_stop(idempotency_key=idem))
                else:
                    self._send_json(202, manager.emergency_stop(idempotency_key=idem))
                return
        except (StoreError, BrokerError) as exc:
            self.close_connection = True
            self._share_error(exc)
            return
        except Exception as exc:
            print(f'[usagesrv] share api error: {type(exc).__name__}: {exc}', flush=True)
            self.close_connection = True
            self._send_json(500, {'error': 'share management error'})
            return
        self._send_json(404, {'error': 'unknown path'})

    @staticmethod
    def _console_share_public_row(row):
        """Map a ShareManager key row onto the console facade contract.

        Display-safe fields only: the masked identifier is the closest
        secret-adjacent value any console response carries — plaintext and
        hashes never map into this shape (A36-2)."""
        return {'id': row.get('key_id'), 'name': row.get('name'), 'masked': row.get('masked_key') or row.get('mk'), 'plaintext_viewable': bool(row.get('plaintext_viewable')), 'enabled': row.get('enabled'), 'max_concurrency': row.get('max_concurrency'), 'row_version': row.get('row_version'), 'created_at': row.get('created_at')}
    _share_url_cache = {'url': None}

    @classmethod
    def _share_funnel_url(cls):
        if cls._share_url_cache['url']:
            return cls._share_url_cache['url']
        try:
            import subprocess
            proc = subprocess.run(['tailscale', 'status', '--json'], capture_output=True, timeout=8)
            node = json.loads(proc.stdout.decode('utf-8', 'replace'))
            name = str((node.get('Self') or {}).get('DNSName') or '').strip('.')
            if name:
                cls._share_url_cache['url'] = 'https://%s' % name
        except Exception:
            pass
        return cls._share_url_cache['url']

    @staticmethod
    def _console_share_status_payload(status):
        """Reshape ShareManager.status() for the console contract.

        Top-level evidence (generations, in_sync, key counts, blockers)
        passes through untouched. The broker snapshot is a {state} shape
        from the adapter ("ok"/"unavailable"), not a {running} boolean —
        map only what the snapshot itself proves, so the UI never reads a
        present-but-unmapped broker object as "not running"; fields the
        snapshot does not carry (version/pid) stay absent for the honest
        不可取得 fallback."""
        out = dict(status)
        snap = out.get('broker')
        if isinstance(snap, dict):
            mapped = {}
            state = snap.get('state')
            inner = snap.get('broker')
            inner = inner if isinstance(inner, dict) else {}
            if state == 'ok':
                mapped['running'] = True
            elif state == 'unavailable':
                mapped['running'] = False
                code = snap.get('error_code')
                if isinstance(code, str) and code:
                    mapped['error_code'] = code
            version = inner.get('version') if inner else snap.get('version')
            pid = inner.get('pid') if inner else snap.get('pid')
            if isinstance(version, str):
                mapped['version'] = version
            if isinstance(pid, int) and (not isinstance(pid, bool)):
                mapped['pid'] = pid
            diag = inner.get('diag')
            if isinstance(diag, list) and diag:
                mapped['diag'] = list(diag[-5:])
            out['broker'] = mapped
        return out

    def _console_share_api(self, rest, method):
        """A36-2 narrow facade for /api/console/share-management/*.

        Fixed whitelist over the existing ShareManager — no arbitrary
        URL/action/parameter proxying. Validation mirrors _share_api
        branch-for-branch (same field constraints, same optimistic-
        concurrency gates, same status_code_for mapping), with two
        deliberate deltas:

        * Trust follows the R9-R35 console surface: reads are credential-
          free behind the loopback-Host/same-origin route gate; writes must
          carry the same-origin CSRF marker (X-Requested-With) or the
          X-Admin-Control token. There is no session cookie in the console
          anymore, so _share_auth's session path cannot apply here.
        * Key rows are re-mapped through _console_share_public_row; create/
          rotate answer {key, plaintext} with the plaintext riding this
          immediate no-store response ONLY — never a console_operations
          record, never logs, never replayed (A36-2 plaintext-once; loss is
          unrecoverable by design, rotate/revoke are the recovery paths).

        Lifecycle (start/stop/emergency-stop) is deliberately absent: those
        actions stay on the console-operations chain (A36-1)."""
        if method in ('POST', 'PATCH', 'DELETE'):
            if not control_token_ok(self.headers.get('X-Admin-Control')) and self.headers.get('X-Requested-With') != 'XMLHttpRequest':
                self.close_connection = True
                self._send_json(403, {'error': 'same-origin marker or control token required'})
                return
        try:
            manager = get_share_manager()
            if method == 'GET' and rest == ['status']:
                payload = self._console_share_status_payload(manager.status())
                try:
                    import console_adapters
                    import console_share_actions
                    if console_share_actions._FUNNEL_OBSERVED is None:
                        console_adapters.wire_share_production_seams(keys_manager=manager)
                    funnel = console_share_actions._funnel_observed()
                    if funnel is not None:
                        payload['funnel_on'] = bool(funnel)
                        if funnel is True:
                            url = self._share_funnel_url()
                            if url:
                                payload['share_url'] = url
                    ops = console_share_actions._BROKER_OPS
                    if ops is not None:
                        running = ops.verify_running()
                        if running is True or running is False:
                            payload['gateway_running'] = running
                    observe = console_share_actions._BACKEND_OBSERVE
                    if observe is not None:
                        ob = observe()
                        if isinstance(ob, dict) and ob.get('auth_state') is not None:
                            payload['account'] = {'valid': ob.get('auth_state') == 'valid', 'running': ob.get('running')}
                        if isinstance(ob, dict) and isinstance(ob.get('attribution'), dict):
                            payload['attribution'] = dict(ob['attribution'])
                except Exception:
                    pass
                payload['last_check_at'] = int(time.time())
                self._send_json(200, payload)
                return
            if method == 'GET' and rest == ['keys']:
                self._send_json(200, {'keys': [self._console_share_public_row(r) for r in manager.list_keys()['keys']]})
                return
            if method == 'GET' and len(rest) == 3 and (rest[0] == 'keys') and (rest[2] == 'secret'):
                key_id = rest[1]
                if not key_id or len(key_id) > 128:
                    self._send_json(400, {'error': 'invalid key_id'})
                    return
                expected_raw = (parse_qs(urlparse(self.path).query).get('expected_row_version') or [''])[0]
                expected = None
                if expected_raw:
                    try:
                        expected = int(expected_raw)
                    except ValueError:
                        self._send_json(400, {'error': 'invalid expected_row_version'})
                        return
                    if expected < 1:
                        self._send_json(400, {'error': 'invalid expected_row_version'})
                        return
                try:
                    payload = manager.reveal_key_secret(key_id, expected_row_version=expected)
                except Disabled:
                    self._send_json(410, {'error': 'key_deleted'})
                    return
                except VersionMismatch:
                    self._send_json(200, {'key_id': key_id, 'secret_unavailable': True, 'reason': 'version_conflict'})
                    return
                except SecretUnavailable:
                    self._send_json(200, {'key_id': key_id, 'secret_unavailable': True, 'reason': 'legacy'})
                    return
                except SecretUndecryptable:
                    self._send_json(200, {'key_id': key_id, 'secret_unavailable': True, 'reason': 'undecryptable'})
                    return
                self._send_json(200, payload)
                return
            body, body_code, body_error = self._read_share_json_body()
            if body_code is not None:
                self.close_connection = True
                self._discard_unread_body()
                self._send_json(body_code, {'error': body_error})
                return

            def _bad(message):
                self._send_json(400, {'error': message})
                return True
            if method == 'POST' and rest == ['keys']:
                if self._reject_unknown_fields(body, ('name', 'idempotency_key', 'max_concurrency')):
                    return
                name = body.get('name')
                idem = body.get('idempotency_key')
                if not isinstance(name, str) or not 1 <= len(name) <= 64:
                    _bad('invalid name')
                    return
                if idem is not None and (not isinstance(idem, str) or not 1 <= len(idem) <= 128):
                    _bad('invalid idempotency_key')
                    return
                max_concurrency = body.get('max_concurrency')
                if max_concurrency is not None:
                    if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or (not 1 <= max_concurrency <= 64):
                        _bad('max_concurrency must be an integer in 1..64')
                        return
                result = manager.create_key(name, max_concurrency=max_concurrency, idempotency_key=idem)
                key_id = result.get('key_id')
                self._send_json(201, {'key': self._console_share_public_row(manager.get_key(key_id)) if key_id else None, 'plaintext': result.get('secret')})
                return
            if method == 'POST' and len(rest) == 3 and (rest[0] == 'keys') and (rest[2] == 'revoke-and-terminate'):
                if self._reject_unknown_fields(body, ('idempotency_key', 'expected_row_version')):
                    return
                if len(rest[1]) > 128:
                    _bad('invalid key_id')
                    return
                idem = body.get('idempotency_key')
                if idem is not None and (not isinstance(idem, str) or not 1 <= len(idem) <= 128):
                    _bad('invalid idempotency_key')
                    return
                expected = body.get('expected_row_version')
                if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
                    _bad('expected_row_version must be a positive integer')
                    return
                result = manager.revoke_and_terminate_key(rest[1], expected_row_version=expected, idempotency_key=idem)
                self._send_json(200, {'key': self._console_share_public_row(result), 'terminated': result.get('terminated')})
                return
            if method == 'POST' and len(rest) == 3 and (rest[0] == 'keys') and (rest[2] == 'rotate'):
                if self._reject_unknown_fields(body, ('idempotency_key', 'expected_row_version')):
                    return
                if len(rest[1]) > 128:
                    _bad('invalid key_id')
                    return
                idem = body.get('idempotency_key')
                if idem is not None and (not isinstance(idem, str) or not 1 <= len(idem) <= 128):
                    _bad('invalid idempotency_key')
                    return
                expected = body.get('expected_row_version')
                if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
                    _bad('expected_row_version required')
                    return
                result = manager.rotate_key(rest[1], idempotency_key=idem, expected_row_version=expected)
                self._send_json(200, {'key': self._console_share_public_row(manager.get_key(rest[1])), 'plaintext': result.get('secret')})
                return
            if method == 'PATCH' and len(rest) == 2 and (rest[0] == 'keys'):
                if self._reject_unknown_fields(body, ('name', 'enabled', 'max_concurrency', 'expected_row_version')):
                    return
                if len(rest[1]) > 128:
                    _bad('invalid key_id')
                    return
                name = body.get('name')
                enabled = body.get('enabled')
                max_concurrency = body.get('max_concurrency')
                expected = body.get('expected_row_version')
                if name is not None and (not isinstance(name, str) or not 1 <= len(name) <= 64):
                    _bad('invalid name')
                    return
                if enabled is not None and (not isinstance(enabled, bool)):
                    _bad('enabled must be bool')
                    return
                if max_concurrency is not None:
                    if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or (not 1 <= max_concurrency <= 64):
                        _bad('max_concurrency must be an integer in 1..64')
                        return
                if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
                    _bad('expected_row_version required')
                    return
                self._send_json(200, {'key': self._console_share_public_row(manager.update_key(rest[1], name=name, enabled=enabled, max_concurrency=max_concurrency, expected_row_version=expected))})
                return
            if method == 'DELETE' and len(rest) == 2 and (rest[0] == 'keys'):
                if self._reject_unknown_fields(body, ('expected_row_version',)):
                    return
                if len(rest[1]) > 128:
                    _bad('invalid key_id')
                    return
                expected = body.get('expected_row_version')
                if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
                    _bad('expected_row_version required')
                    return
                result = manager.delete_key(rest[1], expected_row_version=expected)
                self._send_json(200, {'ok': True, 'deleted': rest[1], 'deleted_at': result.get('deleted_at')})
                return
        except (StoreError, BrokerError) as exc:
            self.close_connection = True
            self._send_json(status_code_for(exc), {'error': str(exc)})
            return
        except Exception as exc:
            print(f'[usagesrv] console share api error: {type(exc).__name__}: {exc}', flush=True)
            self.close_connection = True
            self._send_json(500, {'error': 'share management error'})
            return
        self._send_json(404, {'error': 'unknown path'})

    def _console_api(self, parsed, method):
        dup = self._duplicate_header_error()
        if dup is not None:
            self.close_connection = True
            self._send_json(400, {'error': dup})
            return
        ok, how = (True, 'local')
        rest = [s for s in parsed.path.split('/') if s][2:]
        if rest[:1] == ['share-management']:
            self._console_share_api(rest[1:], method)
            return
        if rest[:1] == ['share-usage']:
            if method != 'GET' or rest != ['share-usage']:
                self.close_connection = True
                self._discard_unread_body()
                self._send_json(404, {'error': 'unknown path'})
                return
            status, payload = self._share_usage_response()
            self._send_json(status, payload)
            return
        if method == 'GET' and rest == ['usage-tokens']:
            query = parse_qs(parsed.query)
            try:
                days = int((query.get('days') or ['14'])[0])
            except (TypeError, ValueError):
                self._send_json(400, {'error': 'invalid_usage_query', 'detail': 'days 必须是整数'})
                return
            try:
                import console_stats
                payload = console_stats.usage_tokens(console_stats.default_sources(), days=days)
            except ValueError as exc:
                self._send_json(400, {'error': 'invalid_usage_query', 'detail': str(exc)})
                return
            except Exception as exc:
                print('[usagesrv] usage tokens query failed: %s' % type(exc).__name__, flush=True)
                self._send_json(502, {'error': 'usage_query_failed'})
                return
            self._send_json(200, payload)
            return
        if method == 'GET' and rest == ['usage-rates']:
            query = parse_qs(parsed.query)
            try:
                hours = int((query.get('hours') or ['24'])[0])
            except (TypeError, ValueError):
                self._send_json(400, {'error': 'invalid_usage_query', 'detail': 'hours 必须是整数'})
                return
            try:
                import console_stats
                payload = console_stats.usage_rates(console_stats.default_sources(), hours=hours)
            except ValueError as exc:
                self._send_json(400, {'error': 'invalid_usage_query', 'detail': str(exc)})
                return
            except Exception as exc:
                print('[usagesrv] usage rates query failed: %s' % type(exc).__name__, flush=True)
                self._send_json(502, {'error': 'usage_query_failed'})
                return
            payload = dict(payload)
            try:
                payload['recorded_requests'] = _usage_rates_recorded_requests(payload)
            except Exception as exc:
                print('[usagesrv] recorded request count failed: %s' % type(exc).__name__, flush=True)
            self._send_json(200, payload)
            return
        if rest[:1] == ['gateway-connection']:
            if method != 'GET' or rest != ['gateway-connection']:
                self.close_connection = True
                self._discard_unread_body()
                self._send_json(404, {'error': 'unknown path'})
                return
            import console_gateway_connection
            query = parse_qs(parsed.query)
            service = (query.get('service') or [''])[0].strip()
            if service not in console_gateway_connection.SERVICES:
                self._send_json(400, {'error': 'invalid_service', 'detail': 'service 必须是 ' + '、'.join(sorted(console_gateway_connection.SERVICES))})
                return
            try:
                snapshot, _cached, _age = console_services_snapshot()
            except Exception as exc:
                print('[usagesrv] gateway connection snapshot failed: %s' % type(exc).__name__, flush=True)
                snapshot = None
            try:
                payload = console_gateway_connection.connection_info(service, snapshot=snapshot)
            except Exception as exc:
                print('[usagesrv] gateway connection read failed: %s' % type(exc).__name__, flush=True)
                self._send_json(503, {'error': 'connection_info_unavailable', 'detail': '连接信息读取失败：%s；按未知处理，不返回猜测值' % type(exc).__name__})
                return
            self._send_json(200, payload)
            return
        try:
            manager = get_console_manager()
        except Exception as exc:
            print('[usagesrv] console manager unavailable: %s' % type(exc).__name__, flush=True)
            self._send_json(503, {'error': 'console subsystem unavailable'})
            return
        if method == 'GET' and rest == ['services']:
            try:
                payload, cached, cache_age = console_services_snapshot()
            except Exception:
                self._send_json(502, {'error': 'runtime status unavailable'})
                return
            payload = dict(payload)
            meta = dict(payload.get('meta') or {})
            meta['cached'] = bool(cached)
            if cached:
                meta['cache_age_seconds'] = cache_age
            payload['meta'] = meta
            self._send_json(200, payload)
            return
        if method == 'GET' and rest == ['prices']:
            try:
                with open(PRICE_FILE, encoding='utf-8') as f:
                    table = json.load(f)
            except OSError:
                self._send_json(502, {'error': 'price_table_unavailable'})
                return
            except ValueError:
                self._send_json(502, {'error': 'price_table_corrupt'})
                return
            if not isinstance(table, dict):
                self._send_json(502, {'error': 'price_table_corrupt'})
                return
            table.pop('_说明', None)
            checked_stale = False
            checked_at = str(table.get('checkedAt') or '')
            try:
                import datetime as _dt
                age = (_dt.datetime.now() - _dt.datetime.strptime(checked_at, '%Y-%m-%d')).days
                checked_stale = age > PRICE_STALE_DAYS
            except ValueError:
                checked_stale = True
            self._send_json(200, {'ok': True, **table, 'checkedStale': checked_stale})
            return
        if method == 'GET' and rest == ['quota-snapshot']:
            query = parse_qs(parsed.query)
            force = (query.get('refresh') or [''])[0] in ('1', 'true')
            self._send_json(200, _quota_snapshot_payload(force))
            return
        if method == 'GET' and rest == ['health']:
            query = parse_qs(parsed.query)
            force = (query.get('refresh') or [''])[0] in ('1', 'true')
            payload, cached = _console_health_payload(force)
            self._send_json(200, payload)
            return
        if method == 'GET' and rest == ['logs']:
            query = parse_qs(parsed.query)
            try:
                limit = max(1, min(200, int((query.get('limit') or ['80'])[0] or 80)))
            except ValueError:
                limit = 80
            requested = [s.strip() for s in (query.get('sources') or ['chat2api'])[0].split(',') if s.strip()]
            readers = _console_log_readers()
            unavailable = [src for src in requested if src not in readers]
            per = max(1, limit // max(1, len(requested)))
            logs = []
            for src in requested:
                entry = readers.get(src)
                if not entry:
                    continue
                path, shape = entry
                tail_lines = None
                try:
                    with open(path, 'rb') as f:
                        f.seek(0, os.SEEK_END)
                        size = f.tell()
                        f.seek(max(0, size - 262144))
                        blob = f.read().decode('utf-8', errors='replace')
                    lines = blob.splitlines()
                    if size > 262144 and len(lines) > 1:
                        lines = lines[1:]
                    tail_lines = lines[-per:]
                except OSError:
                    tail_lines = []
                for line in tail_lines:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    logs_row = _console_log_row(src, row, shape)
                    logs.append(logs_row)
            logs.sort(key=lambda e: e.get('time') or '', reverse=True)
            self._send_json(200, {'ok': True, 'logs': logs[:limit], 'sources': requested, 'sources_unavailable': unavailable})
            return
        if method == 'GET' and rest == ['actions']:
            self._send_json(200, {'actions': manager.action_info()})
            return
        if method == 'GET' and rest == ['stats']:
            query = parse_qs(parsed.query)
            date_from = (query.get('from') or [''])[0]
            date_to = (query.get('to') or [''])[0]

            def _csv_list(key):
                raw = (query.get(key) or [''])[0]
                values = [v.strip() for v in raw.split(',') if v.strip()]
                return values or None
            sources = _csv_list('sources')
            models = _csv_list('models')
            try:
                import console_stats
                payload = console_stats.bounded_stats(console_stats.default_ledgers(), date_from, date_to, sources=sources, models=models)
            except ValueError as exc:
                self._send_json(400, {'error': 'invalid_stats_query', 'detail': str(exc)})
                return
            except Exception as exc:
                print('[usagesrv] stats query failed: %s' % type(exc).__name__, flush=True)
                self._send_json(502, {'error': 'stats_query_failed'})
                return
            self._send_json(200, payload)
            return
        if method == 'GET' and rest == ['log-sources']:
            import console_log_reader
            sources = console_log_reader.build_registry({'chat2api': CHAT2API_LOG}).get('sources') or []
            slim = []
            for src in sources:
                item = dict(src)
                cats = []
                for cat in item.get('categories') or []:
                    cat = dict(cat)
                    full = cat.pop('path', None)
                    cat['name'] = os.path.basename(full) if full else None
                    cats.append(cat)
                item['categories'] = cats
                slim.append(item)
            self._send_json(200, {'ok': True, 'sources': slim})
            return
        if method == 'GET' and rest == ['log-entries']:
            import console_log_reader
            query = parse_qs(parsed.query)

            def _s(key, cap=120):
                return (query.get(key) or [''])[0][:cap] or None
            rotation = None
            raw_rotation = _s('rotation')
            if raw_rotation:
                try:
                    rotation = int(raw_rotation)
                except ValueError:
                    rotation = -1
            try:
                result = console_log_reader.read_entries(_s('source') or '', _s('category') or '', cursor=_s('cursor', 512), q=_s('q'), level=_s('level'), since=_s('since'), until=_s('until'), rotation=rotation, ledger_paths={'chat2api': CHAT2API_LOG}, request_id=(query.get('request_id') or [None])[0])
            except Exception:
                self._send_json(502, {'error': 'log_reader_failed'})
                return
            self._send_json(200, {'ok': True, **result})
            return
        if method == 'GET' and rest == ['model-pending']:
            import console_model_actions
            try:
                payload = console_model_actions.model_sync_pending('', {}, None)
            except Exception as exc:
                print('[usagesrv] model-pending error: %s' % type(exc).__name__, flush=True)
                self._send_json(502, {'error': 'model_state_unavailable'})
                return
            self._send_json(200, payload)
            return
        if method == 'GET' and rest == ['operations']:
            limit = 50
            try:
                limit = int(parse_qs(parsed.query).get('limit', ['50'])[0])
            except ValueError:
                pass
            self._send_json(200, {'operations': manager.list_operations(limit)})
            return
        if method == 'GET' and len(rest) == 2 and (rest[0] == 'operations'):
            op = manager.get_operation(rest[1])
            if op is None:
                self._send_json(404, {'error': 'unknown operation'})
                return
            self._send_json(200, {'operation': op})
            return
        body, body_code, body_error = self._read_share_json_body()
        if body_code is not None:
            self.close_connection = True
            self._discard_unread_body()
            self._send_json(body_code, {'error': body_error})
            return
        if method == 'POST' and rest == ['operations']:
            if self._reject_unknown_fields(body, ('action', 'target', 'params', 'idempotency_key')):
                return
            action = body.get('action')
            target = body.get('target') or ''
            params = body.get('params')
            idem = body.get('idempotency_key')
            if not isinstance(action, str) or not 1 <= len(action) <= 64:
                self._send_json(400, {'error': 'invalid action'})
                return
            if not isinstance(target, str) or len(target) > 64:
                self._send_json(400, {'error': 'invalid target'})
                return
            if idem is not None and (not isinstance(idem, str) or not 1 <= len(idem) <= 128):
                self._send_json(400, {'error': 'invalid idempotency_key'})
                return
            try:
                op = manager.submit(action, target=target, params=params, idempotency_key=idem)
            except Exception as exc:
                code, status = _console_error_status(exc)
                payload = {'error': code}
                reason = getattr(exc, 'reason', None)
                if reason:
                    payload['reason'] = str(reason)
                detail = _console_error_detail(exc)
                if detail:
                    payload['detail'] = detail
                self._send_json(status, payload)
                return
            self._send_json(200 if op.get('deduplicated') else 201, {'operation': op})
            return
        if method == 'POST' and len(rest) == 3 and (rest[0] == 'operations') and (rest[2] == 'cancel'):
            if not rest[1] or len(rest[1]) > 64:
                self._send_json(400, {'error': 'invalid operation id'})
                return
            try:
                op = manager.cancel(rest[1])
            except Exception as exc:
                code, status = _console_error_status(exc)
                self._send_json(status, {'error': code})
                return
            self._send_json(200, {'requested': True, 'operation': op})
            return
        self._send_json(404, {'error': 'unknown path'})

    def do_GET(self):
        if not host_allowed(self.headers.get('Host', '')) or not origin_allowed(self.headers.get('Origin')):
            self._send_json(403, {'error': 'local-only service'})
            return
        self.path = self._rewrite_usage_alias()
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ('/', '/index.html'):
            try:
                with open(INDEX_FILE, 'rb') as f:
                    body = f.read()
            except OSError:
                self._send_json(500, {'error': 'web/dist/index.html 缺失（控制台前端构建产物未就绪）'})
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == '/health':
            self._send_json(200, {'status': 'ok', 'service': 'usagesrv', 'port': PORT})
        elif path == '/api/summary':
            qs = parse_qs(parsed.query)
            try:
                self._send_json(200, summary_cached())
            except Exception as e:
                print(f'[usagesrv] summary error: {type(e).__name__}: {e}', flush=True)
                self._send_json(500, {'error': 'summary unavailable'})
        elif path.startswith('/api/console/'):
            self._console_api(parsed, 'GET')
        elif path.startswith('/api/share/'):
            self._share_api(parsed, 'GET')
        else:
            self._serve_console_static(path)

    def do_POST(self):
        if not host_allowed(self.headers.get('Host', '')) or not origin_allowed(self.headers.get('Origin')):
            self._send_json(403, {'error': 'local-only service'})
            return
        self.path = self._rewrite_usage_alias()
        parsed = urlparse(self.path)
        if parsed.path == '/api/session':
            length = int(self.headers.get('Content-Length') or 0)
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            sid = session_exchange((body or {}).get('code'))
            if not sid:
                self._send_json(403, {'error': 'invalid bootstrap code'})
                return
            payload = json.dumps({'ok': True}, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Set-Cookie', '{0}={1}; HttpOnly; SameSite=Strict; Path=/; Max-Age={2}'.format(SESSION_COOKIE, sid, SESSION_TTL_SECONDS))
            self.end_headers()
            self.wfile.write(payload)
        elif parsed.path == '/api/session/reissue':
            if not control_token_ok(self.headers.get('X-Admin-Control')):
                self._send_json(403, {'error': 'control token required'})
                return
            code = reissue_session_code(self.headers.get('X-Admin-Control'))
            payload = json.dumps({'ok': True, 'code': code}, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(payload)
        elif parsed.path.startswith('/api/console/'):
            self._console_api(parsed, 'POST')
        elif parsed.path.startswith('/api/share/'):
            self._share_api(parsed, 'POST')
        else:
            self._send_json(404, {'error': 'unknown path'})

    def do_PATCH(self):
        if not host_allowed(self.headers.get('Host', '')) or not origin_allowed(self.headers.get('Origin')):
            self._send_json(403, {'error': 'local-only service'})
            return
        self.path = self._rewrite_usage_alias()
        parsed = urlparse(self.path)
        if parsed.path.startswith('/api/console/'):
            self._console_api(parsed, 'PATCH')
        else:
            self._share_api(parsed, 'PATCH')

    def do_DELETE(self):
        if not host_allowed(self.headers.get('Host', '')) or not origin_allowed(self.headers.get('Origin')):
            self._send_json(403, {'error': 'local-only service'})
            return
        self.path = self._rewrite_usage_alias()
        parsed = urlparse(self.path)
        if parsed.path.startswith('/api/console/'):
            self._console_api(parsed, 'DELETE')
        else:
            self._share_api(parsed, 'DELETE')

    def log_message(self, fmt, *args):
        pass

def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    session_bootstrap_code()
    _start_summary_refresh_thread()
    print('-' * 60)
    print(f'usagesrv listening on http://{HOST}:{PORT}')
    print(f'  chat2api   : {CHAT2API_LOG}')
    print(f'  价格主源   : {PRICE_FILE}')
    print('  Ctrl+C 退出')
    print('-' * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nBye.')
if __name__ == '__main__':
    main()
