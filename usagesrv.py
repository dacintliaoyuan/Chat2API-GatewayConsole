#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
usagesrv — AI 网关费用统计聚合服务（127.0.0.1:8001）。

B0（2026-11-01）：默认装配只保留 UM 主链（chat2api 8000）与其必要的
记账/日志/健康配套。退役来源不再被探测、启动或读入默认汇总；历史账本
文件保留可追溯，但不进入 UM 首页。

读取两条本地记账来源，按统一口径聚合后通过 /api/summary 提供给
控制台前端（根路径直接托管 web/dist/index.html 构建产物；dist 未
就绪时根路径返回诚实 500，绝不回退旧静态页——2026-09-23 维护者裁定）：

  1. data/usage-chat2api.jsonl    UM 主链 chat2api（按价目表估算）
  2. data/dsh-ledger-20260902.json（DSH_MERGE_DAY 账本日快照）
                                 DSH 2026-09-02 一天的真实记账冻结快照
                                 （USD 计价，页面按 exchangeRate 换 CNY）

价格引擎移植自 dsh-cost-meter v1.7.7 的计费语义（模型匹配/峰谷判定/选档），
但价目表已与该插件解耦（2026-09-11 用户决定）：两级读取链——
  ① data/prices.json（自持价目表，用户查官网后手工维护；文件内 _说明 键
     记录维护方法，checkedAt 供页面过期提醒，超 PRICE_STALE_DAYS 天黄条）
  ② data/price.override.json（补缺，可手工编辑，最高优先级）
不再读取插件账本或插件包内任何文件；插件升级与本服务计价无关。
路径可用环境变量 USAGE_PRICE_FILE / DSH_LEDGER_SNAPSHOT_FILE 覆盖。

E19-1-04B（2026-11-07）：纯计价规则在 shared/pricing_rules.py（随两端
包携带）；本模块保留价目表读取、60 秒缓存与同名兼容包装，统计读面通过
console_stats.StatsEnvironment 显式注入本模块的路径、计价与加载器。

价目表按生效时刻版本化（2026-09-25 C1）：prices.json 可带一个可选的顶层
"versions" 数组，每段自带生效时刻与当时的模型价、汇率、峰谷规则：
  {"versions": [
     {"versionId": "2026-08-01", "effectiveAt": "2026-08-01T00:00:00Z",
      "checkedAt": "2026-09-11", "exchangeRate": 7.2,
      "peak": {"enabled": true, "effectiveAt": "...", "windows": [...]},
      "models": {...}, "providers": {...}},
     {"versionId": "2026-10-01", "effectiveAt": "2026-10-01T00:00:00Z", ...}]}
判定规则：记录时刻 → 生效时刻不大于该时刻的最近一段；每段的价格**自持**，
不继承顶层当前表（否则改当前表会重算历史）。记录时刻缺失/不可解析，或早于
首段生效时刻时，该记录的价格是「未知」——绝不默默按最新表计价。段内可缺省的
字段用同一兜底：缺 exchangeRate 用 7.2，缺 peak 视为该段无峰谷（按基础档），
缺 models/providers 即该段无价。旧形状（无 "versions" 键）映射为一段恒生效的
默认段，任何时刻（含时刻缺失）都取它——与版本化前逐字段一致，历史金额不变。

全站展示 CNY（汇率取当时生效版本的 exchangeRate）；峰谷口径与 cost-meter
一致：高峰按该版本 peak 子档计价（官方 DeepSeek 为谷价 2 倍；缺子档按基础价
不加倍），北京时间周六/周日全天谷价，逐条按请求时刻判定，时刻无效按基础价。
模型名匹配（精确→归一化→包含→前缀→家族相似）同源移植。历史金额不重算：
每条记录只按它当时生效版本的价目与汇率折算，改价只影响改价之后的请求。

DSH（DeepSeek Harness, 3080）流量经 chat2api 网关时已由 data/usage-chat2api.jsonl
逐条记账；DSH_MERGE_DAY 快照仅为用户 2026-09-04 决定保留的一个历史日
（2026-09-02），并入顶部累计参考费用；其他日期不进入任何 DSH 口径。
"""

import importlib
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

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from share.management import (BrokerClientAdapter, BrokerError, ShareManager,
                              status_code_for)  # noqa: E402
from share.store import (Disabled, StoreError, SecretUnavailable,  # noqa: E402
                         SecretUndecryptable, VersionMismatch, read_key_labels)
from console import public_url as console_public_url  # noqa: E402
from console import stats as console_stats  # noqa: E402
from shared import pricing_rules as _pricing_rules  # noqa: E402

CHAT2API_LOG = os.environ.get("CHAT2API_USAGE_LOG") or \
    os.path.join(HERE, "data", "usage-chat2api.jsonl")
OVERRIDE_FILE = os.path.join(HERE, "data", "price.override.json")
# 自持价目表与 DSH 历史日快照：输入路径可用环境变量覆盖（参照
# CHAT2API_USAGE_LOG 先例），测试与迁移时重定向；默认路径读取失败按
# 缺价/空账本降级，不崩溃。
PRICE_FILE = os.environ.get("USAGE_PRICE_FILE") or \
    os.path.join(HERE, "data", "prices.json")
# UM reference prices followed from Magpie (B1, 2026-11-01). This is a
# read-only snapshot; it never feeds resolve_price or share metering.
MAGPIE_PRICE_SNAPSHOT_FILE = os.environ.get("MAGPIE_PRICE_SNAPSHOT") or \
    os.path.join(HERE, "data", "magpie-prices.snapshot.json")
MAGPIE_PRICE_STATUS_FILE = os.environ.get("MAGPIE_PRICE_STATUS") or (
    MAGPIE_PRICE_SNAPSHOT_FILE[:-len(".snapshot.json")] + ".status.json"
    if MAGPIE_PRICE_SNAPSHOT_FILE.endswith(".snapshot.json")
    else os.path.splitext(MAGPIE_PRICE_SNAPSHOT_FILE)[0] + ".status.json")
DSH_SNAPSHOT_FILE = os.environ.get("DSH_LEDGER_SNAPSHOT_FILE") or \
    os.path.join(HERE, "data", "dsh-ledger-20260902.json")
# 根路径 / 托管的唯一页面：控制台前端构建产物。dist 未构建/缺失时根路径
# 返回诚实 JSON 500（见 do_GET），绝不回退旧 web/index.html 静态页。
INDEX_FILE = os.path.join(HERE, "web", "dist", "index.html")

# Console build-output root. Every file under dist is this repository's own
# build artifact, so the static fallthrough serves the whole tree instead of
# an allowlist; the guard is traversal-proofing (resolved target must stay
# inside the dist root, no directory listing, missing files 404).
CONSOLE_DIST_DIR = os.path.join(HERE, "web", "dist")
# Build identity written by every carried console build (web/vite.config.ts).
# Root serves the tree only when its role matches this process's role: the local
# 8001 must never host the cloud page, and the cloud package must not host the
# local one. Tests redirect this seam independently of CONSOLE_DIST_DIR.
CONSOLE_BUILD_IDENTITY_FILE = os.path.join(
    CONSOLE_DIST_DIR, "candidate-build.json")
CONSOLE_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".glb": "model/gltf-binary",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}

HOST = "127.0.0.1"

# ------------------------------------------------------- explicit deployment role
# 显式云端角色（2026-10-30）：只有显式设置 UM_SHARE_ROLE=cloud（或
# UM_SHARE_DEPLOYMENT=cloud）时，本服务才按「只装配 UM 分享」的云端角色
# 运行；未设置时保持 Windows 本地控制台的全部既有行为（8001、UM 主链与
# model_sync 管理面）。B0（2026-11-01）：退役来源的卡片与探测已从默认
# 装配移除。非法值/互相冲突一律 fail closed，绝不静默退回本地角色。
ROLE_ENV = "UM_SHARE_ROLE"
DEPLOYMENT_ENV = "UM_SHARE_DEPLOYMENT"
ROLE_LOCAL = "local"
ROLE_CLOUD = "cloud"
PORT_ENV = "USAGESRV_PORT"
LOCAL_DEFAULT_PORT = 8001
CLOUD_DEFAULT_PORT = 18096


class DeploymentRoleError(RuntimeError):
    """显式部署角色/端口配置非法（fail closed，不启动服务）。"""


def _resolve_deployment_role(environ=None):
    """Resolve the explicit deployment role: "local" | "cloud".

    Unset -> "local" (the Windows console default, unchanged). Both
    variables may be set only when they agree; an unknown value or a
    conflict raises DeploymentRoleError instead of silently running as the
    local role.
    """
    env = os.environ if environ is None else environ
    seen = []
    for name in (ROLE_ENV, DEPLOYMENT_ENV):
        raw = env.get(name)
        if raw is None:
            continue
        text = str(raw).lower()
        if text not in (ROLE_LOCAL, ROLE_CLOUD):
            raise DeploymentRoleError(
                "%s 只接受 local/cloud（实际=%r）" % (name, raw))
        seen.append(text)
    if not seen:
        return ROLE_LOCAL
    if len(set(seen)) > 1:
        raise DeploymentRoleError(
            "%s 与 %s 互相冲突（%r）" % (ROLE_ENV, DEPLOYMENT_ENV, seen))
    return seen[0]


def _resolve_port(role, environ=None):
    """Resolve the listen port with STRICT validation.

    Unset -> 8001 for the local role, 18096 for the cloud role (the SSH
    tunnel's local forwarding port, so it never collides with a local 8001).
    A set value must be ASCII digits only and within 1..65535; anything
    else (empty, sign, whitespace, hex, float, out of range) raises
    DeploymentRoleError — never a silently coerced port.
    """
    env = os.environ if environ is None else environ
    raw = env.get(PORT_ENV)
    default = CLOUD_DEFAULT_PORT if role == ROLE_CLOUD \
        else LOCAL_DEFAULT_PORT
    if raw is None:
        return default
    text = str(raw)
    if not text or not text.isascii() or not text.isdigit():
        raise DeploymentRoleError(
            "%s 必须是 1..65535 的十进制整数（实际=%r）" % (PORT_ENV, raw))
    port = int(text)
    if not 1 <= port <= 65535:
        raise DeploymentRoleError(
            "%s 必须是 1..65535 的十进制整数（实际=%r）" % (PORT_ENV, raw))
    return port


DEPLOYMENT_ROLE = _resolve_deployment_role()
CLOUD_ROLE = DEPLOYMENT_ROLE == ROLE_CLOUD
PORT = _resolve_port(DEPLOYMENT_ROLE)


def _console_role():
    """The role this process must find in the hosted console build identity."""
    return ROLE_CLOUD if CLOUD_ROLE else ROLE_LOCAL


def _console_build_identity_error():
    """Return an honest error when the hosted tree is not this process's role.

    ``None`` means ``CONSOLE_BUILD_IDENTITY_FILE`` names this process's role
    AND is a production build with a source identity. Missing, unreadable,
    malformed, candidate-line, or other-role identities all return a reason: the
    root page must answer 500 rather than host a console the backend does not
    match. The file is read on every root request so a rebuild is seen without
    restarting the service.
    """
    expected = _console_role()
    label = "web/dist/candidate-build.json"
    try:
        with open(CONSOLE_BUILD_IDENTITY_FILE, "rb") as f:
            raw = f.read()
    except OSError:
        return ("%s 缺失：无法确认托管页面属于 %s 角色"
                % (label, expected))
    try:
        identity = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ("%s 不是有效 JSON：无法确认托管页面属于 %s 角色"
                % (label, expected))
    if not isinstance(identity, dict):
        return ("%s 不是构建身份对象：无法确认托管页面属于 %s 角色"
                % (label, expected))
    role = identity.get("role")
    if role not in (ROLE_LOCAL, ROLE_CLOUD):
        return ("%s 的 role 无效（实际=%r）：无法确认托管页面属于 %s 角色"
                % (label, role, expected))
    if role != expected:
        return ("托管页面是 %s 角色的构建，本进程是 %s 角色："
                "拒绝把 %s 控制台页面托管到 %s 端口"
                % (role, expected, role, PORT))
    line = identity.get("line")
    if line != "production":
        return ("%s 的 line=%r：候选/非生产构建不能托管到 %s 端口"
                % (label, line, PORT))
    source_hash = identity.get("sourceHash")
    if not isinstance(source_hash, str) \
            or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None:
        return ("%s 缺少有效的 sourceHash（实际=%r）："
                "无法确认托管页面来自生产构建"
                % (label, source_hash))
    return None

# DSH 统计固定日期（2026-09-03 用户决定并入顶部累计；2026-09-04 用户决定
# DSH 全部口径只统计这一天，其余日期不纳入。换日在此修改并重启 8001）。
DSH_MERGE_DAY = "2026-09-02"

# dsh-cost-meter pricing.js 的两个历史分界（峰谷时代前按 legacyBase 计、
# 北京时间周末全天谷价），保持与其账本口径一致。纯规则常量在
# shared.pricing_rules；这里保留模块级别名，既有读取端不受影响。
LEGACY_BASE_BOUNDARY = _pricing_rules.LEGACY_BASE_BOUNDARY
WEEKEND_OFFPEAK_AT = _pricing_rules.WEEKEND_OFFPEAK_AT

# 价目表过期提醒：prices.json 的 checkedAt 距今超过该天数时页面黄条提示
# 查官网核对价格（用户手工维护价目表的时效提醒，替代原插件版本漂移黄条）。
PRICE_STALE_DAYS = 30

# 汇率兜底：价目表（或某个版本段）没写 exchangeRate / 写了非正数时一律用
# 该值——与版本化前 load_price_table 内的硬编码兜底逐字段一致。
DEFAULT_EXCHANGE_RATE = _pricing_rules.DEFAULT_EXCHANGE_RATE

# 账本记录里可能出现的用量字段（C1 2026-09-25）：整条记录一个都没有时，
# 该请求的用量是「未知」而不是真实 0。
USAGE_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens",
                      "cache_tokens", "reasoning_tokens", "total_tokens")

_price_lock = threading.Lock()
_price_cache = {"at": 0.0, "data": None}

# ------------------------------------------------------------------ 价格引擎

def canon_model_id(model_id):
    """归一化：小写、去括号附注、只留字母数字（'GLM-5.2 (go)' → 'glm52'）。"""
    return _pricing_rules.canon_model_id(model_id)


def _strip_id_decor(model_id):
    """去日期快照（-20260101 / -2026-01-01）与版本后缀（-v2 / -v1.5）。"""
    return _pricing_rules.strip_id_decor(model_id)


def _tokens_of(model_id):
    return _pricing_rules.tokens_of(model_id)


# match_model_id 的记忆化（2026-09-26 /api/console/stats 性能修复）：
# 该函数只依赖 (model_id, candidates) 两个入参——不读价目表、不读时钟——
# 是纯函数；但每次调用都要对全部候选做 canon/strip/token 三轮正则归一化。
# 账本按记录逐条计价时同一模型被重复匹配上万次（实测 3 天窗口 22671 条
# 记录只有 4 个不同模型名 → 单次请求 22.6 万次以上正则归一化，占
# /api/console/stats 总耗时的 97%）。按 (model_id, 候选序列) 记忆化后，
# 候选集合变化自然 miss；缓存有界，超出容量整表清空（宁可少命中，不无界增长）。
# E19-1-04B：统计计价改走 shared.pricing_rules，其 resolve_price 使用未缓存
# 匹配（纯规则不持全局状态）；本缓存保留为 usagesrv.match_model_id 的宿主
# 兼容入口，直接调用该函数的读取端仍享受记忆化。
_MATCH_CACHE_MAX = 4096
_match_cache = {}
_match_cache_lock = threading.Lock()


def match_model_id(model_id, candidates):
    """dsh-cost-meter matchModelId 的 Python 移植：
    精确 → 归一化等价 → 宽泛包含（最长候选）→ 去后缀精确 → 前缀 → 家族相似。
    数字分叉守卫（glm-5 vs glm-5.3）与之同口径。

    带记忆化（见 _match_cache 注释）：只对 list/tuple 候选启用，键为
    (model_id, 候选序列)——命中与未命中返回同一个值，调用方不依赖返回对象
    的身份，只读其内容。其他候选类型（字符串、集合、生成器……）的 ``in``
    与迭代语义不同，一律走未缓存路径，保证与记忆化前逐字段一致。"""
    if not model_id or not candidates:
        return None
    if not isinstance(candidates, (list, tuple)):
        return _match_model_id_uncached(model_id, candidates)
    try:
        key = (model_id, tuple(candidates))
        hash(key)
    except TypeError:
        # 候选不可哈希（非字符串）时退化为未缓存路径，不因缓存失败而拒绝。
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
    return _pricing_rules.match_model_id_uncached(model_id, candidates)


def complete_tier(raw):
    """补齐一档价格：cacheMiss←input，cacheHit←cachedInput/cacheRead，output←0。"""
    return _pricing_rules.complete_tier(raw)


def normalize_price(raw):
    """对应插件 normalizePrice：complete_tier 补齐基础三桶后保留峰谷子档
    （offPeak/peak/legacyBase），供 tier_for 按时刻选档；其余展示字段不移植。
    此前解析链直接用 complete_tier 会剥掉子档，导致 DeepSeek 峰谷与
    legacyBase 档位失真（2026-09-05 随 D1 对齐一并修复）。"""
    return _pricing_rules.normalize_price(raw)


def _parse_ts(value):
    """ISO 时间 → epoch ms；失败返回 None。"""
    return _pricing_rules.parse_ts(value)


def _beijing_weekday(day_index):
    """北京时间日序 → 星期（0=周日…6=周六；1970-01-01 为周四）。"""
    return _pricing_rules.beijing_weekday(day_index)


def weekend_zone_at(at_ms):
    """北京时间周六/周日全谷价区间；非周末、生效前或时刻无效返回 None
    （插件对非有限时刻同样返回 null）。"""
    return _pricing_rules.weekend_zone_at(at_ms)


def is_peak_hour(at_ms, effective_at_ms, windows):
    return _pricing_rules.is_peak_hour(at_ms, effective_at_ms, windows)


def tier_for(entry, at_ms, peak_cfg):
    """选档：生效后峰时段 → peak（缺子档按基础价，与插件 tierFor 一致）；
    生效后谷时段 → offPeak；生效前、禁用或时刻无效 → 基础价；峰谷时代前 → legacyBase。"""
    return _pricing_rules.tier_for(entry, at_ms, peak_cfg)


def cost_of(tokens, entry, at_ms, peak_cfg):
    """一次调用的 USD 成本。tokens 各桶相互独立（缓存部分须先从 input 里扣除）。"""
    return _pricing_rules.cost_of(tokens, entry, at_ms, peak_cfg)


# ------------------------------------------------------------- 价格源与查找

def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _tiers_of(models_table, providers_table):
    """价表扁平成 [(models_dict, billingMode, source)]，按精确度打分择优。"""
    return _pricing_rules.tiers_of(models_table, providers_table)


def _rate_of(raw_rate):
    """汇率：非数值/非正数一律退回 DEFAULT_EXCHANGE_RATE（既有兜底口径）。"""
    return _pricing_rules.rate_of(raw_rate)


def _peak_of(raw_peak):
    """峰谷规则：缺 enabled/windows 即无峰谷（按基础档），effectiveAt 解析失败为 None。"""
    return _pricing_rules.peak_of(raw_peak)


def _checked_age_days(checked_at, now):
    """checkedAt（人工核对日期）距今天数；缺失/不可解析返回 None（不猜）。"""
    return _pricing_rules.checked_age_days(checked_at, now)


def _version_id(raw, effective_at_ms):
    """版本标识：显式 versionId 优先，缺省用生效时刻字符串（便于按版本归组）。"""
    return _pricing_rules.version_id(raw, effective_at_ms)


def _price_version(raw, effective_at_ms, now):
    """一个显式版本段 → 内部查找结构（价格自持，不继承顶层当前表）。"""
    return _pricing_rules.price_version(raw, effective_at_ms, now)


def _current_version(versions, versioned, now_ms):
    """当前生效段：显式版本段按现时刻选；无可用段返回 None。"""
    return _pricing_rules.current_version(versions, versioned, now_ms)


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
        if _price_cache["data"] is not None and now - _price_cache["at"] < 60:
            return _price_cache["data"]

    prices = _load_json(PRICE_FILE) or {}
    top_rate = _rate_of(prices.get("exchangeRate"))
    top_peak = _peak_of(prices.get("peak"))
    top_tiers = _tiers_of(prices.get("models"), prices.get("providers"))
    checked_at = prices.get("checkedAt")
    checked_age_days = _checked_age_days(checked_at, now)

    raw_versions = prices.get("versions")
    versioned = isinstance(raw_versions, list)
    versions, invalid_versions = [], 0
    if versioned:
        for raw in raw_versions:
            if not isinstance(raw, dict):
                invalid_versions += 1
                continue
            effective_at_ms = _parse_ts(raw.get("effectiveAt") or "")
            if effective_at_ms is None:
                # 没有可用生效时刻的段不能被任何记录时刻命中：忽略并计数，
                # 绝不把它当成「一直生效」（否则等于用现价算历史）。
                invalid_versions += 1
                continue
            versions.append(_price_version(raw, effective_at_ms, now))
        versions.sort(key=lambda v: v["effectiveAtMs"])
    else:
        # 旧形状：单一默认段，恒生效（任何时刻，含时刻缺失）——兼容红线。
        versions = [{
            "versionId": None,
            "effectiveAt": None,
            "effectiveAtMs": None,
            "exchangeRate": top_rate,
            "peak": top_peak,
            "tiers": top_tiers,
            "checkedAt": checked_at if isinstance(checked_at, str) else None,
            "checkedAgeDays": checked_age_days,
        }]

    # 顶层「现行」视图：版本化时取当前生效段（现时刻早于首段时取最早一段），
    # 无可用段时退回顶层值仅供展示与既有单值口径读取；价格解析一律走版本段。
    current = _current_version(versions, versioned, now * 1000)
    if current is None:
        rate, peak_cfg, tiers = top_rate, top_peak, top_tiers
    else:
        rate, peak_cfg, tiers = (current["exchangeRate"], current["peak"],
                                current["tiers"])

    override = _load_json(OVERRIDE_FILE) or {}
    override_models = {k: v for k, v in override.items() if not k.startswith("_")}

    data = {
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
                "checkedStale": (current["checkedAgeDays"] > PRICE_STALE_DAYS)
                                if current["checkedAgeDays"] is not None else None,
            },
        },
        "meta": {
            "pricesFound": bool(tiers),
            "checkedAt": checked_at if isinstance(checked_at, str) else None,
            "checkedAgeDays": checked_age_days,
            "checkedStale": (checked_age_days > PRICE_STALE_DAYS)
                            if checked_age_days is not None else None,
            "overrideFound": os.path.exists(OVERRIDE_FILE),
        },
    }
    _price_cache["at"] = now
    _price_cache["data"] = data
    return data


def version_at(table, at_ms):
    """记录时刻 → 当时生效的版本段；无法证明时返回 None（不猜最新版本）。

    旧形状（无显式版本段）只有一段恒生效的默认段：任何时刻（含时刻缺失/
    不可解析）都取它——与版本化前逐字段一致。显式版本段下，时刻缺失、不可
    解析或早于首段生效时刻时返回 None，调用方据此按「未知」处理（不返回 0
    冒充，也不默默按最新价计）。纯规则在 shared.pricing_rules。
    """
    return _pricing_rules.version_at(table, at_ms)


def version_rate(table, at_ms):
    """记录时刻 → 当时生效版本的汇率；版本未知返回 None（不猜现价）。"""
    return _pricing_rules.version_rate(table, at_ms)


def current_version(table=None):
    """当前生效版本段（展示与既有单值口径）；无可用段时返回 None。"""
    table = table if table is not None else load_price_table()
    return _current_version(table.get("versions") or [], table.get("versioned"),
                            time.time() * 1000)


def resolve_price(model_id, at_ms=None):
    """返回 (entry, billingMode, matched_model, source)；未定价/版本未知返回 (None, ...)。

    at_ms = 记录时刻：按当时生效的版本段取价（含该段的峰谷与 override 优先级）。
    旧形状价表只有一段恒生效的默认段，时刻（含缺失/不可解析）不影响结果——
    与版本化前逐字段一致。补缺层 price.override.json 没有生效时刻，仍是最高
    优先级的手工修补层（要按时刻分段请写进版本段）。纯规则在 shared.pricing_rules。
    逐条计价走本模块的记忆化 ``match_model_id``（2026-09-26 性能修复），
    不在 shared 里持全局缓存。
    """
    return _pricing_rules.resolve_price(
        model_id, at_ms, load_price_table(), matcher=match_model_id)


def estimate_record(model_id, at_ms, input_tok, output_tok, cache_tok, reasoning_tok):
    """单条记录估算：返回 (cost_usd, priced, matched_model, peak_used)。

    既有 4 元组契约（console_stats 依赖）：按记录时刻所属版本计价，版本标识
    不进返回值；需要版本标识用 estimate_record_with_version。
    """
    return _pricing_rules.estimate_record(
        model_id, at_ms, input_tok, output_tok, cache_tok, reasoning_tok,
        load_price_table(), matcher=match_model_id)


def estimate_record_with_version(model_id, at_ms, input_tok, output_tok,
                                 cache_tok, reasoning_tok):
    """单条记录估算（版本化契约）：返回 (cost_usd, priced, matched, peak, version_id)。

    既有五元组契约（console_stats 等依赖）：version_id 为记录时刻生效版本段的
    标识，旧形状价表/版本未知时为 None。priced=False 表示价格或版本未知
    （cost 为 0 是「未知」的占位，不是真实 0）。分享计量的人民币折算入口
    见 ``estimate_record_with_version_and_rate``（同一金额，额外带汇率）。
    """
    return _pricing_rules.estimate_record_detail(
        model_id, at_ms, input_tok, output_tok, cache_tok, reasoning_tok,
        load_price_table(), matcher=match_model_id)[:5]


def _estimate_record_detail(model_id, at_ms, input_tok, output_tok, cache_tok, reasoning_tok):
    """内部估算：返回
    (cost_usd, priced, matched, peak, version_id, exchange_rate, cache_unknown)。

    exchange_rate 为该记录所属版本的汇率，None 表示版本未知（不折算、不猜
    现价）。版本化价表下记录时刻缺失/不可解析/早于首段时返回
    (0.0, False, None, False, None, None, False)。
    ``cache_tok is None`` 表示缓存量未报告（Q11）：此时不把未知缓存折成 0，
    而是按全部输入都是 cache-miss 计算上界，并由 cache_unknown=True 告诉
    调用方金额不是精确值。旧记录调用方始终传数值，历史金额不变。
    纯规则在 shared.pricing_rules。
    """
    return _pricing_rules.estimate_record_detail(
        model_id, at_ms, input_tok, output_tok, cache_tok, reasoning_tok,
        load_price_table(), matcher=match_model_id)


def estimate_record_with_version_and_rate(model_id, at_ms, input_tok, output_tok,
                                         cache_tok, reasoning_tok):
    """单条记录估算（含汇率契约）：返回
    (cost_usd, priced, matched, peak, version_id, exchange_rate)。

    分享计量（share_metering.price_fn）的人民币折算入口：USD 金额、版本标识与
    ``estimate_record_with_version`` 同源（同一个 shared ``estimate_record_detail``），
    额外返回该记录所属版本的汇率。``exchange_rate`` 为 None 表示版本未知
    （不折算、不猜现价）；旧形状价表只有恒生效的默认段，因此任何时刻
    （含时刻缺失）都返回默认段汇率。既有五元组契约
    ``estimate_record_with_version`` 保持不变，其消费者不受影响。
    """
    return _pricing_rules.estimate_record_detail(
        model_id, at_ms, input_tok, output_tok, cache_tok, reasoning_tok,
        load_price_table(), matcher=match_model_id)[:6]


MICROS_PER_YUAN = 1_000_000


def budget_cost_micros(model, at_ms, input_tok, output_tok, cache_tok,
                        reasoning_tok):
    """S2 预算人民币微元：返回 ``(cost_micros, facts)``。

    ``cost_micros`` 为 None 表示模型未定价或价目版本未知——不返回 0 冒充
    免费。facts 带实际价目版本/汇率/命中模型，供预算事件审计。
    """
    (cost_usd, priced, matched, peak, version_id, rate) = \
        estimate_record_with_version_and_rate(
            model, at_ms, input_tok, output_tok, cache_tok, reasoning_tok)
    facts = {
        "priced": bool(priced),
        "matched_model": matched,
        "peak": bool(peak),
        "price_version": version_id,
        "exchange_rate": rate,
    }
    if not priced or rate is None:
        return None, facts
    return int(round(float(cost_usd) * float(rate) * MICROS_PER_YUAN)), facts


def _bucket_price_estimate(rec, model, in_tok, out_tok, cache_tok, reason_tok):
    """_bucket 计价回调（chat2api）：按记录时刻所属版本估算。

    返回 (cost_usd, priced, matched, peak, version_id, rate, cache_unknown)；
    rate 为该记录所属版本的汇率，None = 版本未知（不折算、不猜现价）；
    cache_unknown=True 表示缓存量未报告，金额是 cache-miss 上界。
    """
    return _estimate_record_detail(model, rec.get("_ms"), in_tok, out_tok,
                                  cache_tok, reason_tok)


# ------------------------------------------------------------------- 读取账本

def _read_jsonl(path, limit=200000):
    """读取 JSONL（容忍尾行残缺），新→旧返回时间升序列表。"""
    records = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    rec["_ms"] = _parse_ts(rec.get("time"))
                    records.append(rec)
    except FileNotFoundError:
        return []
    except Exception:
        return records
    records.sort(key=lambda r: r.get("_ms") or 0)
    return records[-limit:]


def _beijing_day(ms):
    return _pricing_rules.beijing_day(ms)


def _day_start_ms(day):
    """北京时间某日 00:00 → epoch ms；缺失/不可解析返回 None（不猜）。"""
    return _pricing_rules.day_start_ms(day)


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
            return value == value  # NaN 不是可用数值
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            continue
        if number == number:
            return True
    return False


CACHE_REPORT_STATUSES = ("reported", "unreported")


def _cache_report_status(rec):
    """缓存报告状态（Q11）：reported/unreported，旧记录为 unknown。

    旧记录没有该字段，不能把兼容用的 cached_input_tokens=0 读成真实 0；
    unknown 只用于旧数据，绝不推测历史缓存量。计价仍沿用旧值，保持历史
    金额不变；新记录 unreported 才把缓存量视为未知。
    """
    status = rec.get("cache_report_status")
    if isinstance(status, str) and status in CACHE_REPORT_STATUSES:
        return status
    return "unknown"


class _Money:
    """USD 小计 + 按版本汇率分组（同一版本的小计只乘一次汇率）。

    旧形状价表只有一段恒生效的默认段，分组恒为单组，换算与版本化前逐位一致：
    服务级沿用既有的「先 round(usd, 6) 再乘汇率」（_bucket 的 costUsd 就是
    该四舍五入值，_round_service 消费的正是它），模型/日级用未取整小计。
    版本化价表下各版本小计各自按自己的汇率换算后相加——历史金额不重算。
    partial=True 表示有记录的金额未纳入本合计（无价格/用量未知/版本未知）；
    此时 cny() 在没有任何可折算记录时返回 None（未知，不折 0）。
    """

    __slots__ = ("usd", "by_rate", "partial", "round_usd")

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
            return sum(round(usd, 6) * rate for rate, usd in self.by_rate.items())
        return sum(usd * rate for rate, usd in self.by_rate.items())


def _bucket(records, kind, cost_fn):
    """按 (服务, 模型, 日) 聚合。

    cost_fn(rec, model, i, o, c, r) → (cost_usd, priced, matched, peak,
    version_id, rate[, cache_unknown])；rate 为该记录所属版本的汇率，None =
    版本未知（不折算、不猜现价）。未知一律显式分列、不折零：用量未知的请求不进
    token 合计（unknownUsageRequests），无价格的进 unpricedRequests，版本
    未知的进 unknownVersionRequests；cache 未报告的请求不进 cache token 合计，
    单独计数（cacheUnreportedRequests），其金额是 cache-miss 上界并让
    costCnyPartial=true（Q11）。costCnyPartial=true 表示 costCny 只含
    可折算的记录（下界，不是总额），只有整节点的金额全部无法折算时
    costCny=null（未知，绝不折 0）。既有字段的取值不变：旧形状下服务级
    costCny 与版本化前逐位一致，只有全无价格的模型行从 0 变成 null。
    """
    models, days = {}, {}
    total = {"requests": 0, "cost_usd": 0.0, "input": 0, "output": 0,
             "cache": 0, "reasoning": 0, "priced": 0, "peak_requests": 0,
             "unknown_usage": 0, "unpriced": 0, "unknown_version": 0,
             "cache_reported": 0, "cache_unreported": 0,
             "cache_unknown": 0, "cache_unknown_priced": 0}
    money = _Money(round_usd=True)  # 服务级：沿用既有先 round(usd, 6) 再乘汇率
    model_money = {}
    day_money = {}
    unpriced_models = set()
    versioned = bool(load_price_table().get("versioned"))
    for rec in records:
        ms = rec.get("_ms")
        model = str(rec.get("model") or "unknown")
        in_tok = int(rec.get("input_tokens") or 0)
        out_tok = int(rec.get("output_tokens") or 0)
        cache_tok = int(rec.get("cached_input_tokens") or rec.get("cache_tokens") or 0)
        reason_tok = int(rec.get("reasoning_tokens") or 0)
        cache_status = _cache_report_status(rec)
        usage_known = _usage_known(rec)
        priced_cache_tok = None if cache_status == "unreported" else cache_tok
        result = cost_fn(rec, model, in_tok, out_tok, priced_cache_tok, reason_tok)
        if isinstance(result, (tuple, list)) and len(result) >= 7:
            cost_usd, priced, matched, peak, version_id, rate = result[:6]
            cache_unknown = bool(result[6])
        else:
            cost_usd, priced, matched, peak, version_id, rate = result
            cache_unknown = False

        total["requests"] += 1
        if cache_status == "reported":
            total["cache_reported"] += 1
        elif cache_status == "unreported":
            total["cache_unreported"] += 1
        else:
            total["cache_unknown"] += 1
        if cache_unknown:
            total["cache_unknown_priced"] += 1
        if usage_known:
            total["input"] += in_tok
            total["output"] += out_tok
            if cache_status != "unreported":
                total["cache"] += cache_tok
            total["reasoning"] += reason_tok
        else:
            total["unknown_usage"] += 1
        total["cost_usd"] += cost_usd
        if priced:
            total["priced"] += 1
        else:
            unpriced_models.add(model)
            if priced is False:
                total["unpriced"] += 1
                if versioned and version_id is None:
                    total["unknown_version"] += 1
        if not usage_known or priced is False or cache_unknown:
            money.partial = True
        money.add(cost_usd, rate)
        if peak:
            total["peak_requests"] += 1

        key = matched or model
        m = models.setdefault(key, {"model": key, "requests": 0, "cost_usd": 0.0,
                                   "input": 0, "output": 0, "cache": 0, "reasoning": 0,
                                   "priced": 0, "peak_requests": 0,
                                   "unknown_usage": 0, "unpriced": 0,
                                   "unknown_version": 0,
                                   "cache_reported": 0, "cache_unreported": 0,
                                   "cache_unknown": 0,
                                   "cache_unknown_priced": 0})
        m_money = model_money.setdefault(key, _Money())
        m["requests"] += 1
        m["cost_usd"] += cost_usd
        if cache_status == "reported":
            m["cache_reported"] += 1
        elif cache_status == "unreported":
            m["cache_unreported"] += 1
        else:
            m["cache_unknown"] += 1
        if cache_unknown:
            m["cache_unknown_priced"] += 1
        if usage_known:
            m["input"] += in_tok
            m["output"] += out_tok
            if cache_status != "unreported":
                m["cache"] += cache_tok
            m["reasoning"] += reason_tok
        else:
            m["unknown_usage"] += 1
        if priced:
            m["priced"] += 1
        elif priced is False:
            m["unpriced"] += 1
            if versioned and version_id is None:
                m["unknown_version"] += 1
        if not usage_known or priced is False or cache_unknown:
            m_money.partial = True
        m_money.add(cost_usd, rate)
        if peak:
            m["peak_requests"] += 1

        day = _beijing_day(ms)
        if day:
            d = days.setdefault(day, {"day": day, "requests": 0, "cost_usd": 0.0})
            d_money = day_money.setdefault(day, _Money())
            d["requests"] += 1
            d["cost_usd"] += cost_usd
            if not usage_known or priced is False or cache_unknown:
                d_money.partial = True
            d_money.add(cost_usd, rate)
    for key, m in models.items():
        m["_money"] = model_money[key]
    for day, d in days.items():
        d["_money"] = day_money[day]
    return {
        "kind": kind,
        "requests": total["requests"],
        "costUsd": round(total["cost_usd"], 6),
        "tokens": {"input": total["input"], "output": total["output"],
                   "cache": total["cache"], "reasoning": total["reasoning"]},
        "cacheReportedRequests": total["cache_reported"],
        "cacheUnreportedRequests": total["cache_unreported"],
        "cacheUnknownRequests": total["cache_unknown"],
        "cacheUnknownPricedRequests": total["cache_unknown_priced"],
        "pricedRequests": total["priced"],
        "peakRequests": total["peak_requests"],
        "unknownUsageRequests": total["unknown_usage"],
        "unpricedRequests": total["unpriced"],
        "unknownVersionRequests": total["unknown_version"],
        "models": sorted(models.values(), key=lambda m: -m["cost_usd"]),
        "days": [days[k] for k in sorted(days)],
        "unpricedModels": sorted(unpriced_models),
        "_money": money,
    }


def _um_reference_unavailable(failure, state="unsynced", records=None):
    """Honest UM reference-cost shape when no local ledger can be read.

    ``records=None`` means the caller did not read the ledger at all (cloud
    role); the counts stay unknown instead of becoming zero.
    """
    count = records if isinstance(records, int) and records >= 0 else None
    return {
        "label": "参考",
        "cny": None,
        "cnyPartial": True,
        "costUsd": None,
        "price": {
            "state": state,
            "source": None,
            "snapshotAt": None,
            "version": None,
            "failure": failure,
        },
        "fx": {"rate": None, "version": None},
        "snapshotSha256": None,
        "coverage": {
            "records": count, "pricedRequests": None,
            "unpricedRequests": count, "estimatedRequests": None,
            "incompleteRequests": count,
            "unknownEffectiveRequests": None, "fxMissingRequests": None,
            "preEffectiveRequests": None,
            "costUsd": None, "cny": None, "cnyPartial": True,
        },
        "models": [],
    }


def _um_reference_summary(records):
    """Price UM's own ledger with the local Magpie snapshot.

    This is a separate read-only reference path: it never calls resolve_price,
    never writes the shared price table and never changes share metering. A
    missing module, bad snapshot or bad status stays explicit; it never becomes
    zero. The caller already read ``CHAT2API_LOG`` once and passes those records
    here, so no second ledger read is issued.
    """
    try:
        from console import magpie_prices as console_magpie_prices
    except Exception as exc:  # noqa: BLE001 - the service must still start
        return _um_reference_unavailable(
            "价格同步模块不可用（%s）" % type(exc).__name__,
            records=len(records))
    try:
        return console_magpie_prices.compute_reference(
            records, MAGPIE_PRICE_SNAPSHOT_FILE, MAGPIE_PRICE_STATUS_FILE)
    except Exception as exc:  # noqa: BLE001 - report, never fold to zero
        return _um_reference_unavailable(
            "参考费用计算失败（%s）" % type(exc).__name__,
            records=len(records))


def _round_service(svc, exchange_rate):
    """USD 小计 → CNY：按记录所属版本汇率逐段换算后合计（C1 2026-09-25）。

    节点带 _money（_bucket / read_dsh_ledger 写入）时按版本分组换算：同一
    版本的小计只乘一次汇率，历史金额不重算。旧形状价表恒为单组，与既有换算
    逐位一致（服务级沿用既有先 round(usd, 6) 再乘汇率的口径，模型/日级用
    未取整小计）。partial 的节点 costCnyPartial=true，costCny 只含可折算
    部分；只有整节点金额全部无法折算时 costCny=null（未知，绝不折 0、绝不按
    现价）。没有 _money 的节点沿用单一汇率换算（与版本化前同一公式）。
    """
    def money(node):
        raw = node.pop("_money", None)
        # 各节点只用其中一种键名：service 级 costUsd、models/days cost_usd。
        usd = node.pop("cost_usd", None)
        camel = node.pop("costUsd", None)
        if usd is None:
            usd = camel
        if raw is None:
            node["costCny"] = round((usd or 0.0) * exchange_rate, 4)
            return
        raw_cny = raw.cny()
        node["costCny"] = None if raw_cny is None else round(raw_cny, 4)
        if raw.partial:
            node["costCnyPartial"] = True

    money(svc)
    for m in svc.get("models", []):
        money(m)
    for d in svc.get("days", []):
        money(d)
    return svc


def read_dsh_ledger():
    """读 DSH 账本日快照（data/dsh-ledger-20260902.json）的 days 按日汇总。

    快照由 2026-09-11 解耦迁移时从账本存档冻结生成（现行插件账本已丢失该日，
    存档仍保有；生成方法见快照文件内 _说明）。与原插件记账口径一致：
    cost 以 config.pricingCurrency（USD）计价，按日、按 provider:model 汇总，
    cacheRead 计入缓存 token。只统计 DSH_MERGE_DAY 一天。文件缺失或损坏时
    返回空服务对象，页面显示"暂无记账数据"。

    折算（C1 2026-09-25）：金额是真实记账的 USD，按**该日生效版本段**的汇率
    折 CNY（逐日各自换算后相加），历史金额不重算；版本化价表下该日无版本可归属
    时该日 costCny=null（未知，不折 0、不按现价）。"""
    snapshot = _load_json(DSH_SNAPSHOT_FILE) or {}
    days = snapshot.get("days") or {}
    pricing_currency = (snapshot.get("config") or {}).get("pricingCurrency") or "USD"
    models, day_totals = {}, {}
    total = {"requests": 0, "cost": 0.0, "input": 0, "output": 0,
             "cache": 0, "reasoning": 0}
    money = _Money()
    model_money = {}
    day_money = {}
    day_rate = None
    for day, e in days.items():
        if not isinstance(e, dict):
            continue
        # 只统计 DSH_MERGE_DAY 这一天，其余日期（含之后）不纳入任何 DSH 口径。
        if day != DSH_MERGE_DAY:
            continue
        calls = int(e.get("calls") or 0)
        cost = float(e.get("cost") or 0)
        rate = day_rate = version_rate(load_price_table(), _day_start_ms(day))
        total["requests"] += calls
        total["cost"] += cost
        total["input"] += int(e.get("input") or 0)
        total["output"] += int(e.get("output") or 0)
        total["cache"] += int(e.get("cacheRead") or 0)
        total["reasoning"] += int(e.get("reasoning") or 0)
        d_money = day_money.setdefault(day, _Money())
        if rate is None:
            d_money.partial = True
        d_money.add(cost, rate)
        day_totals[day] = {"day": day, "requests": calls, "cost_usd": cost}
        for pm, m in (e.get("byProviderModel") or {}).items():
            if not isinstance(m, dict):
                continue
            mm = models.setdefault(pm, {"model": pm, "requests": 0, "cost_usd": 0.0,
                                       "input": 0, "output": 0, "cache": 0, "reasoning": 0})
            m_money = model_money.setdefault(pm, _Money())
            if rate is None:
                m_money.partial = True
            m_money.add(float(m.get("cost") or 0), rate)
            mm["requests"] += int(m.get("calls") or 0)
            mm["cost_usd"] += float(m.get("cost") or 0)
            mm["input"] += int(m.get("input") or 0)
            mm["output"] += int(m.get("output") or 0)
            mm["cache"] += int(m.get("cacheRead") or 0)
            mm["reasoning"] += int(m.get("reasoning") or 0)
    if day_rate is None and total["requests"]:
        # 有纳入日但没有可归属的版本：金额未折算（未知），不按现价、不折 0。
        money.partial = True
    if day_rate is not None:
        money.add(total["cost"], day_rate)
    for key, m in models.items():
        m["_money"] = model_money[key]
    for day, d in day_totals.items():
        d["_money"] = day_money[day]
    return {
        "kind": "dsh-ledger",
        "pricingCurrency": pricing_currency,
        "requests": total["requests"],
        "costUsd": total["cost"],
        "tokens": {"input": total["input"], "output": total["output"],
                   "cache": total["cache"], "reasoning": total["reasoning"]},
        "models": sorted(models.values(), key=lambda m: -m["cost_usd"]),
        "days": [day_totals[k] for k in sorted(day_totals)],
        "unpricedModels": [],
        "_money": money,
    }


_SUMMARY_CACHE = {"value": None, "at": 0.0}
_SUMMARY_REFRESH_SECONDS = 300.0
_SUMMARY_LOCK = threading.Lock()


def summary_cached() -> dict:
    """R9-R39（维护者裁定 2026-09-21）：费用聚合打开即看，不等约 30 秒聚合。

    GET 直接返回后台常驻缓存（带 _generated_epoch 截至时点），守护线程定时
    重聚合；失败保留上一次真实值（stale 由前端既有口径标注）。冷启动兜底：
    后台未出结果前首个请求同步聚合一次并写入缓存。
    """
    with _SUMMARY_LOCK:
        value = _SUMMARY_CACHE["value"]
        if value is not None:
            stamped = dict(value)
            stamped["_generated_epoch"] = _SUMMARY_CACHE["at"]
            stamped["_cached_age_seconds"] = round(time.time() - _SUMMARY_CACHE["at"], 1)
            return stamped
    try:
        built = build_summary()
    except Exception as exc:
        print("[usagesrv] summary build failed: %s" % type(exc).__name__, flush=True)
        return {"error": "summary unavailable"}
    with _SUMMARY_LOCK:
        _SUMMARY_CACHE["value"] = built
        _SUMMARY_CACHE["at"] = time.time()
    result = dict(built)
    result["_generated_epoch"] = _SUMMARY_CACHE["at"]
    result["_cached_age_seconds"] = 0.0
    return result


def _summary_refresh_worker() -> None:
    """Boot-time warm-up then periodic refresh; failure keeps the cache."""
    while True:
        try:
            built = build_summary()
            with _SUMMARY_LOCK:
                _SUMMARY_CACHE["value"] = built
                _SUMMARY_CACHE["at"] = time.time()
        except Exception as exc:
            print("[usagesrv] summary refresh failed: %s" % type(exc).__name__, flush=True)
        time.sleep(_SUMMARY_REFRESH_SECONDS)


def _start_summary_refresh_thread() -> None:
    threading.Thread(target=_summary_refresh_worker,
                     name="summary-refresh", daemon=True).start()


def _summary_cache_reset_for_tests() -> None:
    with _SUMMARY_LOCK:
        _SUMMARY_CACHE["value"] = None
        _SUMMARY_CACHE["at"] = 0.0


def _cloud_peak_and_versions(table):
    """The price-shape fields shared with the local summary (no ledger read)."""
    peak_cfg = table["peak"]
    peak_windows_beijing = []
    for w in peak_cfg["windows"]:
        peak_windows_beijing.append(
            {"beijing": f"{(w['start'] + 8) % 24:02d}:00-{(w['end'] + 8) % 24:02d}:00",
             "rate": 2.0})
    return peak_cfg, peak_windows_beijing


def _build_cloud_summary():
    """Cloud role summary: ONLY the UM share capability is assembled.

    It NEVER reads the private chat2api / DSH ledgers and NEVER
    probes any local source port: those resources do not exist on this deployment. The
    share usage has its OWN read surface (GET /api/console/share-usage), so this
    payload honestly reports an EMPTY services object plus the explicit
    assembly facts instead of fabricating zero rows for absent chains. Only the
    price table (data/prices.json, the same file the share metering prices
    against) is read for its metadata/versions/peak shape.
    """
    table = load_price_table()
    rate = table["exchangeRate"]
    peak_cfg, peak_windows_beijing = _cloud_peak_and_versions(table)
    return {
        "generatedAt": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "exchangeRate": rate,
        "currency": "CNY",
        "role": ROLE_CLOUD,
        "assembly": {
            "role": ROLE_CLOUD,
            "services": ["share"],
            "excluded": ["qoder", "chat2api", "m365", "dsh"],
            "note": "云端角色只装配 UM 分享：不读取本地私人账本、不探测 Qoder/"
                    "CLIProxy/model_sync；分享用量见 GET /api/console/share-usage",
        },
        "sources": {"priceMeta": table["meta"]},
        # The cloud role never reads the local UM ledger, so it cannot show a
        # local CNY reference cost. The shape is explicit, not zero.
        "umReference": _um_reference_unavailable(
            "云端角色不读取本地 UM 账本", "unsynced", records=None),
        "peak": {
            "enabled": peak_cfg["enabled"],
            "effectiveAt": time.strftime("%Y-%m-%d", time.gmtime(peak_cfg["effectiveAtMs"] / 1000)) if peak_cfg["effectiveAtMs"] else None,
            "windows": peak_windows_beijing,
            "weekendOffpeak": True,
            "note": "高峰按价表 peak 子档计价；北京时间周六/周日全天谷价（与分享计价同一价目表）",
        },
        "priceVersions": {
            "versioned": table["versioned"],
            "invalidCount": table["versionMeta"]["invalidCount"],
            "current": table["versionMeta"]["current"],
            "versions": [
                {"versionId": v["versionId"],
                 "effectiveAt": v["effectiveAt"],
                 "exchangeRate": v["exchangeRate"],
                 "checkedAt": v["checkedAt"],
                 "checkedStale": (v["checkedAgeDays"] > PRICE_STALE_DAYS)
                                 if v["checkedAgeDays"] is not None else None}
                for v in table["versions"]
            ],
            "note": "逐条按记录时刻取当时生效版本段的价目与汇率",
        },
        # 空 services 是诚实的「本部署未聚合私人账本」：前端显示缺少服务
        # 聚合的错误，而不是把「没读取」折成 0 请求 / 0 费用。
        "services": {},
        "dsh": {"url": None,
                "note": "云端角色未装配 DSH 快照读取"},
        "dshMergedIntoTotal": {
            "day": None, "costCny": 0.0,
            "note": "云端角色不并入任何本地 DSH 账本",
        },
    }


def build_summary():
    if CLOUD_ROLE:
        return _build_cloud_summary()
    rate = load_price_table()["exchangeRate"]

    # UM 主链 chat2api：按价目表估算（逐条按记录时刻所属版本计价与折算）。
    c2a_records = _read_jsonl(CHAT2API_LOG)
    c2a = _bucket(c2a_records, "estimate", _bucket_price_estimate)
    _round_service(c2a, rate)

    # DSH：cost-meter 账本按日汇总的真实记账（USD→CNY，非估算）。
    dsh = read_dsh_ledger()
    _round_service(dsh, rate)

    # DSH 并入顶部累计：只并入 DSH_MERGE_DAY 这一天，不做每日自动并入。
    dsh_merge_cny, dsh_merge_day = 0.0, None
    for d in dsh["days"]:
        if d["day"] == DSH_MERGE_DAY:
            dsh_merge_cny, dsh_merge_day = d["costCny"], d["day"]
            break

    table = load_price_table()
    peak_cfg = table["peak"]
    peak_windows_beijing = []
    for w in peak_cfg["windows"]:
        peak_windows_beijing.append(
            {"beijing": f"{(w['start'] + 8) % 24:02d}:00-{(w['end'] + 8) % 24:02d}:00",
             "rate": 2.0})

    return {
        "generatedAt": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "exchangeRate": rate,
        "currency": "CNY",
        "sources": {
            "chat2apiLog": _display_path(CHAT2API_LOG),
            "dshLedger": _display_path(DSH_SNAPSHOT_FILE),
            "priceMeta": table["meta"],
            "magpiePriceSnapshot": _display_path(MAGPIE_PRICE_SNAPSHOT_FILE),
        },
        "peak": {
            "enabled": peak_cfg["enabled"],
            "effectiveAt": time.strftime("%Y-%m-%d", time.gmtime(peak_cfg["effectiveAtMs"] / 1000)) if peak_cfg["effectiveAtMs"] else None,
            "windows": peak_windows_beijing,
            "weekendOffpeak": True,
            "note": "高峰按价表 peak 子档计价（官方 DeepSeek 为谷价 2 倍；缺子档按基础价不加倍）；北京时间周六/周日全天谷价（与 dsh-cost-meter 口径一致）",
        },
        # 价目表版本段（C1 2026-09-25）：versioned=false 表示旧形状（无 versions
        # 键，单一恒生效默认段）；versions 列出每段生效时刻与该段核对时间，供页面
        # 区分「当前生效版本」「本段核对时间」「本段是否过期」；invalidCount 是
        # 无可用生效时刻被忽略的段数（既不生效也不参与计价）。
        "priceVersions": {
            "versioned": table["versioned"],
            "invalidCount": table["versionMeta"]["invalidCount"],
            "current": table["versionMeta"]["current"],
            "versions": [
                {"versionId": v["versionId"],
                 "effectiveAt": v["effectiveAt"],
                 "exchangeRate": v["exchangeRate"],
                 "checkedAt": v["checkedAt"],
                 "checkedStale": (v["checkedAgeDays"] > PRICE_STALE_DAYS)
                                 if v["checkedAgeDays"] is not None else None}
                for v in table["versions"]
            ],
            "note": "逐条按记录时刻取当时生效版本段的价目与汇率；缺时刻/早于首段按未知（不计 0）；改价只影响之后的请求",
        },
        # B0：UM 主链 + 冻结的 DSH 快照；默认汇总绝不读取未登记来源。
        "services": {"chat2api": c2a, "dsh": dsh},
        # UM reference cost uses UM's own ledger and the local Magpie snapshot,
        # not data/prices.json. It is additive and never feeds share pricing.
        "umReference": _um_reference_summary(c2a_records),
        "dsh": {"url": "http://127.0.0.1:3080",
                "note": "费用为 2026-09-02 账本日快照的真实记账（USD→CNY）；快照生成方法见 data/dsh-ledger-20260902.json 内 _说明"},
        "dshMergedIntoTotal": {
            "day": dsh_merge_day,
            "costCny": dsh_merge_cny,
            "note": "仅此日 DSH 账本费用并入顶部累计参考费用",
        },
    }


# ------------------------------------------------------ 分享用量（只读挂载）
# share/metering.py 由另一名实现者并行编写：这里只做受保护的延迟导入——
# 模块不存在时 usagesrv 行为与现在完全一致（绝不因它起不来），路由如实报
# 不可用；绝不把「读不到」折成 0 请求 / 0 费用。挂载点是两个只读口：
# GET /api/share/usage（share 管理面，鉴权走既有 _share_auth：本机会话或管理
# 令牌）与 GET /api/console/share-usage（控制台面 R9-R35，本机同源页面免凭据
# 直读，边界由路由层 Host/Origin 门承担），两者共用同一构造点，见
# Handler._share_usage_response。
SHARE_USAGE_LIMIT = 200000
SHARE_BACKEND_DIR_ENV = "CONSOLE_SHARE_BACKEND_DIR"
SHARE_USAGE_LEDGER_NAME = "usage-share-backend.jsonl"


def share_usage_ledger_path():
    """分享专用后端 8110 的用量账本路径。

    解析口径与 console_share_production._backend_dir / console_log_reader 一致：
    CONSOLE_SHARE_BACKEND_DIR（须绝对路径）覆盖，否则 %LOCALAPPDATA% 下的
    SelfUseAI/ai-gateway/share-backend；解析不出返回 None（不猜路径）。"""
    override = os.environ.get(SHARE_BACKEND_DIR_ENV)
    if override:
        if not os.path.isabs(override):
            return None
        return os.path.join(override, SHARE_USAGE_LEDGER_NAME)
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    return os.path.join(local, "SelfUseAI", "ai-gateway", "share-backend",
                        SHARE_USAGE_LEDGER_NAME)


def share_usage_reader():
    """受保护的延迟导入 share_metering.read_share_usage。

    返回 (reader, error)：模块不存在/导入失败/接口缺失 → (None, 错误码)，
    导入失败只打印类型名，不抛出——服务照常运行。"""
    try:
        share_metering = __import__("importlib").import_module("share.metering")
    except Exception as exc:  # noqa: BLE001 — 缺模块不是错误：路由如实报不可用
        print(f"[usagesrv] share_metering unavailable: {type(exc).__name__}", flush=True)
        return None, "share_metering_unavailable"
    reader = getattr(share_metering, "read_share_usage", None)
    if not callable(reader):
        return None, "share_metering_unavailable"
    return reader, None


def share_usage_requests_reader():
    """受保护的延迟导入 share_metering.read_share_requests。

    逐请求页与汇总共用同一账本与计价链；模块/接口缺席时返回
    (None, 错误码)，路由如实报不可用，绝不折成空页。
    """
    try:
        share_metering = __import__("importlib").import_module("share.metering")
    except Exception as exc:  # noqa: BLE001 — 缺模块不是错误：路由如实报不可用
        print(f"[usagesrv] share_metering unavailable: {type(exc).__name__}",
              flush=True)
        return None, "share_metering_unavailable"
    reader = getattr(share_metering, "read_share_requests", None)
    if not callable(reader):
        return None, "share_metering_unavailable"
    return reader, None


# ------------------------------------------------------------------ HTTP 服务

# 本机管理会话：um_share 等本机进程无法读取 data/ 下的引导码与会话标识
# （仓库根 DENY），页面必须经控制台 [7] 携带一次性引导码换取 HttpOnly 会话。
# 引导码短时有效且成功兑换即原子消费（重放被拒）；会话时效自成功兑换起算；
# 码过期/已消费后由控制台持管理令牌重新申请，无需重启服务。
SESSION_COOKIE = "usage_session"
SESSION_TTL_SECONDS = 12 * 3600
SESSION_CODE_TTL_SECONDS = 600  # 引导码只在生成后 10 分钟内可兑换
SESSION_CODE_FILE = os.environ.get("USAGE_SESSION_CODE_FILE") or \
    os.path.join(HERE, "data", "usage-session-code")
CONTROL_TOKEN_FILE = os.environ.get("USAGE_CONTROL_TOKEN_FILE") or \
    os.path.join(HERE, "data", "admin-control-token")

_session_lock = threading.Lock()
_session = {
    "code": None, "code_expires": 0.0,
    "session": None, "session_expires": 0.0,
}


def _generate_session_code_locked(now):
    _session["code"] = secrets.token_urlsafe(24)
    _session["code_expires"] = now + SESSION_CODE_TTL_SECONDS
    try:
        from share.secret_protection import write_private_secret_path
        write_private_secret_path(
            SESSION_CODE_FILE, _session["code"].encode("utf-8"))
    except (OSError, ValueError, ImportError):
        pass  # 写失败只影响控制台引导，接口仍拒绝无会话访问


def _ensure_session_code_locked(now):
    if _session["code"] is None or now > _session["code_expires"]:
        _generate_session_code_locked(now)


def session_bootstrap_code():
    with _session_lock:
        _ensure_session_code_locked(time.time())
        return _session["code"]


def reissue_session_code(control_token):
    """控制台重新申请引导码：须持管理令牌；旧码（含未过期）立即作废。"""
    if not control_token_ok(control_token):
        return None
    with _session_lock:
        _generate_session_code_locked(time.time())
        return _session["code"]


def session_exchange(code):
    """一次性引导码换取会话标识：成功即原子消费（重放被拒），
    会话时效自本次兑换起算；码过期/已消费/不符返回 None。"""
    with _session_lock:
        now = time.time()
        current = _session["code"]
        if not code or not current or now > _session["code_expires"]:
            return None
        if not secrets.compare_digest(str(code), current):
            return None
        _session["code"] = None
        _session["code_expires"] = 0.0
        _session["session"] = secrets.token_urlsafe(32)
        _session["session_expires"] = now + SESSION_TTL_SECONDS
        return _session["session"]


def session_valid(cookie_header):
    with _session_lock:
        expected = _session["session"]
        expires = _session["session_expires"]
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
    """程序化访问通道：与 chat2api 共用本机管理令牌（data/admin-control-token）。

    POSIX 读取同样执行 0600/当前 uid 门禁：权限过宽或属主不符一律拒绝，
    不把管理令牌读进进程。Windows 保持既有读取路径（ACL 边界另管）。
    """
    try:
        from share.secret_protection import read_private_secret_path
        expected = read_private_secret_path(
            CONTROL_TOKEN_FILE, max_bytes=1024,
            label="control token").decode("utf-8").strip()
    except (OSError, ValueError, ImportError):
        return False
    return bool(expected) and bool(supplied) and secrets.compare_digest(supplied, expected)


def summary_access_allowed(cookie_header, control_token):
    return session_valid(cookie_header) or control_token_ok(control_token)


# ------------------------------------------------------------------ console
# 统一控制台 P1 基础层（薄装配）：任务管理器与适配器在首次 /api/console 访问时
# 惰性创建；生产注册表只开放只读状态，未授权动作一律显式不可用（console_adapters）。
CONSOLE_STATE_FILE = os.environ.get("CONSOLE_STATE_FILE") or \
    os.path.join(HERE, "data", "console-state.sqlite3")
CONSOLE_LOCK_DIR = os.environ.get("CONSOLE_LOCK_DIR") or \
    os.path.join(HERE, "data", "console-locks")
CONSOLE_STATUS_TTL = 2.0
_console_manager_lock = threading.Lock()
_console_manager = None
_console_status_lock = threading.Lock()
_console_status_cache = {"at": 0.0, "value": None}


def get_console_manager():
    """Lazily build the console operation manager (never at import time).

    Cloud role: ONLY the UM share registry is assembled (no
    model_sync/zcode card or handler), and the share seams are wired for the
    Linux deployment (TS/Funnel stay unwired).

    Local role (B0, 2026-11-01): the UM registry only — no non-UM action
    module is imported.
    """
    global _console_manager
    with _console_manager_lock:
        if _console_manager is None:
            console_adapters = importlib.import_module("console.adapters")
            console_operations = importlib.import_module("console.operations")
            console_share_production = importlib.import_module(
                "console.share_production")
            # E19-1-06/04B: the share broker construction loader is wired
            # explicitly BEFORE the seams are assembled; console_share_production
            # never imports this host module. The cloud root passes a broker
            # object directly instead (production_seams(broker=...)).
            console_share_production.configure_broker_loader(
                _load_share_management_deployment)
            console_adapters.wire_share_production_seams(
                keys_manager=get_share_manager(), cloud=CLOUD_ROLE)
            _console_manager = console_operations.OperationManager(
                db_path=CONSOLE_STATE_FILE,
                registry=console_adapters.build_registry(cloud=CLOUD_ROLE),
                lock_dir=CONSOLE_LOCK_DIR)
        return _console_manager


def console_services_snapshot():
    """Read-only services snapshot with a small TTL cache (wrapper-backed).

    Cloud role reads the Linux pid-file snapshot of the UM share entry only;
    the PowerShell wrapper is never invoked there.
    """
    now = time.time()
    with _console_status_lock:
        cached = _console_status_cache["value"]
        if cached is not None and now - _console_status_cache["at"] < CONSOLE_STATUS_TTL:
            return cached, True, round(now - _console_status_cache["at"], 1)
    console_adapters = importlib.import_module("console.adapters")
    payload = console_adapters.service_status(cloud=CLOUD_ROLE)
    with _console_status_lock:
        _console_status_cache["at"] = now
        _console_status_cache["value"] = payload
    return payload, False, 0.0


# ------------------------------------------------- R11-B2 (2026-09-23)
# UM 服务端健康探测：有界只读、短 TTL、绝不写操作账本、绝不把失败折成
# 0（未知 ≠ 0）。B0（2026-11-01）：默认装配只探测 UM 主链（8000）；
# 退役来源（8095/8097/8320）不再探测，缺来源时不发请求。TCP 拒绝=未监听，
# 任意 HTTP 应答=存活，TCP 可达但 HTTP 探测失败=listening+诚实注记。
_CONSOLE_HEALTH_TTL_S = 30
_console_health_lock = threading.Lock()
_console_health_cache = {"at": 0.0, "payload": None}

_CONSOLE_HEALTH_PROBES = (
    # chat2api is TCP-ONLY: its /api/models branch shares the proxy path
    # with /v1/models, which calls refresh()→browser_login() when the local
    # bearer is empty or upstream answers 401 (chat2api.py:2427→1246→1210).
    # A 30s automatic probe must never be able to pop a login window, so the
    # honest evidence here is the runtime PORT fact alone (the handoff's
    # "可用运行时端口证据").
    {"id": "chat2api", "port": 8000, "path": None},
)


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
        sock = socket.create_connection(("127.0.0.1", spec["port"]),
                                        timeout=1.5)
    except socket.timeout:
        # A connect TIMEOUT is not a refusal: the port may be filtered or the
        # backlog saturated. Report it as unknown instead of claiming
        # not_listening (TimeoutError ⊂ OSError, so it must be caught first).
        return {"id": spec["id"], "port": spec["port"],
                "state": "unknown", "note": "TCP 连接超时（探测未定，非未监听）",
                "ms": _ms()}
    except OSError:
        return {"id": spec["id"], "port": spec["port"],
                "state": "not_listening", "ms": _ms()}
    sock.close()
    if spec.get("path") is None:
        # TCP-only evidence (no HTTP GET at all): the service answered the
        # port; this probe deliberately issues NO request so it can never
        # trigger a login/refresh side effect.
        return {"id": spec["id"], "port": spec["port"],
                "state": "listening", "http_status": None,
                "note": "TCP 端口可达（未做 HTTP 探测）", "ms": _ms()}
    try:
        conn = http.client.HTTPConnection("127.0.0.1", spec["port"],
                                          timeout=2.0)
        conn.request("GET", spec["path"],
                     headers={"Accept": "application/json, */*"})
        resp = conn.getresponse()
        status = resp.status
        resp.read(256)
        conn.close()
        return {"id": spec["id"], "port": spec["port"],
                "state": "listening", "http_status": status, "ms": _ms()}
    except Exception:
        return {"id": spec["id"], "port": spec["port"],
                "state": "listening", "http_status": None,
                "note": "tcp accepted but http probe failed", "ms": _ms()}


def _console_health_payload(force):
    if CLOUD_ROLE:
        # 云端角色：只报 UM 分享入口的 pid/身份事实；绝不 TCP 探测
        # 非 UM 端口。
        from console import adapters as console_adapters
        snapshot = console_adapters.cloud_service_status()
        services = list(snapshot.get("services") or [])
        services.append({"id": "usagesrv", "port": PORT,
                         "state": "listening",
                         "note": "console host itself"})
        return {"checked_at": int(time.time()), "cached": False,
                "services": services}, False
    now = time.time()
    with _console_health_lock:
        cached = _console_health_cache["payload"]
        if (not force and cached is not None
                and now - _console_health_cache["at"] < _CONSOLE_HEALTH_TTL_S):
            out = dict(cached)
            out["cached"] = True
            return out, True
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        services = list(pool.map(_probe_service, _CONSOLE_HEALTH_PROBES))
    services.append({"id": "usagesrv", "port": PORT, "state": "listening",
                     "note": "console host itself"})
    # checked_at is stamped AFTER the probes (the probe wall-clock must not
    # shrink the effective TTL), and `cached` is ALWAYS present so the client
    # can tell a fresh probe from a field that is simply missing.
    payload = {"checked_at": int(time.time()), "cached": False,
               "services": services}
    with _console_health_lock:
        _console_health_cache["at"] = now
        _console_health_cache["payload"] = payload
    return dict(payload), False


def _console_error_status(exc):
    """Map task-layer rejections to HTTP status codes."""
    code = getattr(exc, "code", "")
    if code == "invalid_action":
        return code, 404
    if code == "source_retired":
        # B0: a retired-source action is refused explicitly (Gone), never a
        # generic 500 and never a simulated success.
        return code, 410
    if code == "action_disabled":
        return code, 403
    if code in ("invalid_params", "invalid_target", "invalid_idempotency_key",
                "invalid_resource"):
        return code, 400
    if code == "idempotency_key_conflict":
        return code, 409
    if code == "operation_not_found":
        return code, 404
    if code == "operation_already_finished":
        return code, 409
    return "console_error", 500


# 拒绝说明（2026-09-26）：error 机器码是契约本体，detail 只是加性的人类可读
# 补充。网页原本只拿到 `invalid_params`，看不出是哪个参数、期望什么形状（维护者
# 报告的「应用同步 model · 失败／被拒绝：invalid_params」）。这里只对已知的
# 参数形状类错误（400 族）附说明，且文案必须通过形状检查：不含路径分隔符、
# 盘符或长 token（可能是密钥/内部标识），并统一压成单行、截断到 200 字符。
# 检查不通过时宁可不给说明，也不改写原文——绝不把路径、密钥或内部结构带给
# 浏览器。校验规则本身不变（不放宽、不忽略未知参数）。
_CONSOLE_ERROR_DETAIL_CODES = frozenset((
    "invalid_params", "invalid_target", "invalid_idempotency_key",
    "invalid_resource"))
_CONSOLE_ERROR_DETAIL_MAX = 200
_CONSOLE_ERROR_DETAIL_UNSAFE = re.compile(
    r"[\\/]|[A-Za-z]:|[A-Za-z0-9_.-]{24,}")


def _console_error_detail(exc):
    """受控的拒绝说明；不适用或不安全时返回 None（宁缺勿猜）。"""
    if getattr(exc, "code", "") not in _CONSOLE_ERROR_DETAIL_CODES:
        return None
    message = getattr(exc, "message", None)
    if not isinstance(message, str):
        return None
    text = " ".join(message.split())
    if not text or _CONSOLE_ERROR_DETAIL_UNSAFE.search(text):
        return None
    return text[:_CONSOLE_ERROR_DETAIL_MAX]


# /api/share/* command layer is created lazily so merely importing usagesrv does
# not create/open the SQLite store. Test suites set SHARE_STORE_FILE /
# SHARE_MANAGEMENT_CONFIG before loading this module; production uses the
# default data/ paths.
_share_manager_lock = threading.Lock()
_share_manager = None

# W3 管理端装配配置：显式部署事实（永不推断）。文件只含路径与开关，
# 控制密钥本体仍在受 ACL 保护的 broker 目录中，绝不进入本配置。
SHARE_MANAGEMENT_CONFIG_FILE = os.environ.get("SHARE_MANAGEMENT_CONFIG") or \
    os.path.join(HERE, "data", "share-management.json")


def _load_cloud_share_management_deployment(payload):
    """Explicit cloud (Linux) management transport config (schema 2).

    The Windows schema 1 (named pipe) is NOT accepted here and a Unix path is
    NEVER stuffed into ``pipe_name``: the transport is explicit. A present but
    corrupt/unusable config still raises BrokerError (never a silent Noop).
    """
    schema = payload.get("schema_version")
    if isinstance(schema, bool) or schema != 2:
        raise BrokerError(
            "cloud share management config requires schema_version 2 "
            "(explicit unix_socket transport); got %r" % (schema,))
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise BrokerError("share management config 'enabled' must be an "
                          "explicit boolean (never inferred)")
    if enabled is False:
        return None
    transport = payload.get("transport")
    if transport != "unix_socket":
        raise BrokerError(
            "cloud share management config transport must be 'unix_socket' "
            "(got %r)" % (transport,))
    from share.broker_client import (BrokerClient,
                                    UnixSocketBrokerTransport,
                                    validate_unix_socket_path)
    socket_path = payload.get("socket_path")
    if not isinstance(socket_path, str) or not socket_path:
        raise BrokerError("cloud share management config socket_path missing")
    try:
        socket_path = validate_unix_socket_path(socket_path)
    except ValueError as exc:
        raise BrokerError(f"cloud socket path unusable: {exc}")
    key_file = payload.get("control_key_file")
    if not isinstance(key_file, str) or not key_file:
        raise BrokerError("share management config control_key_file missing")
    from share.broker_server import read_control_key
    try:
        secret = read_control_key(key_file)
    except (OSError, ValueError) as exc:
        raise BrokerError(f"share management control key unusable: {exc}")
    client = BrokerClient(lambda: UnixSocketBrokerTransport(socket_path),
                          secret)
    return BrokerClientAdapter(client)


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
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError) as exc:
        raise BrokerError(f"share management config unreadable: {exc}")
    if not isinstance(payload, dict):
        raise BrokerError("share management config must be an object")
    if CLOUD_ROLE:
        return _load_cloud_share_management_deployment(payload)
    schema = payload.get("schema_version")
    if isinstance(schema, bool) or schema != 1:
        raise BrokerError(
            f"unsupported share management config schema {schema!r}")
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise BrokerError("share management config 'enabled' must be an "
                          "explicit boolean (never inferred)")
    if enabled is False:
        return None
    pipe_prefix = "\\\\.\\pipe\\"
    pipe_name = payload.get("pipe_name")
    if (not isinstance(pipe_name, str)
            or not pipe_name.startswith(pipe_prefix)
            or len(pipe_name) <= len(pipe_prefix)):
        raise BrokerError("share management config pipe_name must be a "
                          "named-pipe path (\\\\.\\pipe\\<name>)")
    key_file = payload.get("control_key_file")
    if not isinstance(key_file, str) or not key_file:
        raise BrokerError("share management config control_key_file missing")
    from share.broker_client import BrokerClient, NamedPipeBrokerTransport
    from share.broker_server import read_control_key
    try:
        secret = read_control_key(key_file)
    except (OSError, ValueError) as exc:
        raise BrokerError(f"share management control key unusable: {exc}")
    client = BrokerClient(lambda: NamedPipeBrokerTransport(pipe_name), secret)
    return BrokerClientAdapter(client)


def get_share_manager():
    global _share_manager
    with _share_manager_lock:
        if _share_manager is None:
            broker = _load_share_management_deployment()
            _share_manager = ShareManager() if broker is None \
                else ShareManager(broker=broker)
        return _share_manager


def _display_path(path):
    """页面只显示文件名；绝对路径不跨进程泄露（本机管理边界，ADR 网络边界）。"""
    name = os.path.basename(str(path or ""))
    return name or "(未配置)"


# console_stats 的进程级显式注入环境（E19-1-04B）：路径取本模块启动时
# 解析出的常量，计价走 shared.pricing_rules，价目表加载走本模块的 60 秒
# 缓存链。main() 在启动时 configure 一次，让 um_today/um_trend 等不显式
# 传 environment 的函数也走同一注入；console_stats._FILE_CACHE 是 reader
# 端内缓存，不属于这个环境，也不上提 shared。
STATS_ENV = console_stats.StatsEnvironment(
    chat2api_log=CHAT2API_LOG,
    dsh_snapshot=DSH_SNAPSHOT_FILE,
    dsh_merge_day=DSH_MERGE_DAY,
    magpie_price_snapshot=MAGPIE_PRICE_SNAPSHOT_FILE,
    magpie_price_status=MAGPIE_PRICE_STATUS_FILE,
    parse_ts=_parse_ts,
    beijing_day=_beijing_day,
    day_start_ms=_day_start_ms,
    display_path=_display_path,
    estimate_record=estimate_record,
    price_table_loader=load_price_table,
)


def _stats_environment():
    """The statistics environment installed for this process.

    main() installs STATS_ENV with console_stats.configure; a test composition
    root may install its own fixture environment instead. Resolving at call time
    lets the explicit call points below pass the environment that is actually
    installed, without freezing a second copy or reading another app's ledgers.
    """
    return console_stats._environment()


def _hostname_of(host_header):
    try:
        parts = urlparse(f"//{host_header}")
        hostname = (parts.hostname or "").lower()
        if hostname:
            parts.port  # 端口损坏的 Host 一律拒绝（访问属性即校验）
        return hostname
    except ValueError:
        return ""


def host_allowed(host_header):
    """DNS rebinding 防护：本服务的 Host 只能是回环名。"""
    return _hostname_of(host_header) in {"127.0.0.1", "localhost", "::1"}


def origin_allowed(origin):
    """带 Origin 的请求只能来自本机页面，且端口必须与本服务一致。

    缺省端口（隐含 80）与非法端口（urlparse 抛 ValueError）都不属于本服务，
    一律拒绝；本服务是 http://127.0.0.1:8001，浏览器总会带端口。
    """
    if not origin:
        return True
    try:
        parts = urlparse(origin)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "http"
            and parts.hostname in {"127.0.0.1", "localhost"}
            and port == PORT)


# Remote console access is opt-in and device-scoped. The HTTP listener stays
# on loopback; only the configured HTTPS Serve proxy may authenticate a peer.
from console.tailnet import TailnetPolicy, has_proxy_metadata, load_policy

TAILNET_POLICY = TailnetPolicy()

# Private noVNC surface.  The console itself is credential-free behind the
# loopback Host/Origin gate or the Tailscale Serve policy, but the display
# WebSocket and its grant are not: an SSH -L tunnel also arrives as loopback,
# so only a management credential (Tailscale identity, admin control token or the
# console session) can bind a display grant.  x11vnc still challenges the RFB
# stream with its root-only password; the display proxy completes that
# challenge through the display service's narrow helper socket.  Imported at module
# load so a missing optional display package cannot stop the console; /display/
# then answers an honest 503.
try:
    from console.display_proxy import (
        DisplayGrantError,
        DisplayGrants,
        DisplayProxy,
        load_config as load_display_config,
        request_origin as display_request_origin,
        request_subject as display_request_subject,
    )
    DISPLAY_GRANTS = DisplayGrants()
    DISPLAY_PROXY = DisplayProxy(
        load_display_config(), session_valid=session_valid,
        control_token_ok=control_token_ok, grants=DISPLAY_GRANTS)
except ImportError:
    DisplayGrantError = None
    DISPLAY_GRANTS = None
    DISPLAY_PROXY = None
    display_request_subject = None


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


# 调用日志账本的字段形状（F4b）：各链账本写入的字段名不同，行构造器按形状
# 取值；形状里没有的字段一律 null（未采集/未知，绝不折 0）。键=读取表里的形状名。
_LOG_FEED_SHAPES = {
    # chat2api 账本：字段名同 usagesrv._bucket（time/input_tokens/
    # output_tokens/duration_ms）。
    "usage_ledger": {
        "time": ("time",), "model": ("model",), "status": ("status",),
        "input": ("input_tokens",), "output": ("output_tokens",),
        "cached_input": ("cached_input_tokens",),
        "reasoning": ("reasoning_tokens",), "duration": ("duration_ms",),
    },
}


def _console_log_readers():
    """调用日志账本读取表（F4b）：请求的 source id → (账本路径, 字段形状)。

    B0（2026-11-01）：默认装配只读 UM 主链（chat2api）账本。未登记的 id
    进 sources_unavailable，如实声明未覆盖，绝不静默跳过、也不发任何请求。
    """
    return {
        "chat2api": (CHAT2API_LOG, "usage_ledger"),
    }


def _console_log_row(source, record, shape="usage_ledger"):
    """一条账本记录 → /api/console/logs 行（F2：数值缺失一律 null，绝不折零）。

    账本没写该字段、写了 null、写了空串或写了非数值时，一律以 null 披露
    「未采集/未知」，而不是折成 0——0 会被前端读成真实测得值（例如缺输出
    Token 但耗时有效的请求会显示 0 Token/s）。真实 0 是已知值：写了 0 就是 0。
    字段名按 shape（各链账本的写入形状，见 _LOG_FEED_SHAPES）取值；形状里
    没有的字段同样一律 null。
    """
    fields = _LOG_FEED_SHAPES.get(shape) or _LOG_FEED_SHAPES["usage_ledger"]

    def raw(key):
        for name in fields[key]:
            if name in record:
                return record[name]
        return None

    cache_status = record.get("cache_report_status")
    return {
        "source": source,
        "time": str(raw("time") or ""),
        "model": str(raw("model") or ""),
        "status": str(raw("status") or ""),
        "input_tokens": _log_int_or_none(raw("input")),
        "output_tokens": _log_int_or_none(raw("output")),
        "cached_input_tokens": _log_int_or_none(raw("cached_input")),
        "cache_report_status": (cache_status
                                if cache_status in CACHE_REPORT_STATUSES
                                else None),
        "reasoning_tokens": _log_int_or_none(raw("reasoning")),
        "duration_ms": _log_int_or_none(raw("duration")),
    }


# 「事件集不可信」的 coverage errors[].type（read_error / corrupt_ledger）由
# console_stats 单点定义（console_stats.UNTRUSTED_EVENT_ERRORS 与判定函数
# console_stats._events_trusted）；本模块不另写一份字面量。malformed_lines
# （部分坏行、其余仍可读）不算：坏行已在 coverage 披露，其余事件仍可计数。


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
    from console import stats as console_stats
    coverage = payload.get("coverage") or {}
    hours = coverage.get("hours")
    checked_epoch = payload.get("checked_epoch")
    if not isinstance(hours, int) or not isinstance(checked_epoch, int):
        return {}  # 契约不完整：不给计数，前端按未知处理（不造 0）
    now_ms = checked_epoch * 1000
    window_start = console_stats._window_hour_starts(hours, now_ms)[0]
    window_end = window_start + hours * 3_600_000
    per_source, source_coverage = console_stats._scan_sources(
        console_stats.default_sources(environment=_stats_environment()))
    counts = {}
    for source, entry in per_source.items():
        entry_coverage = source_coverage.get(source) or {}
        if not entry_coverage.get("ledger_present"):
            counts[source] = None  # 未读取：未知，不是 0
            continue
        if not console_stats._events_trusted(entry_coverage):
            # 事件集不可信：读取失败时读取层退回的是上次缓存的事件，
            # 损坏快照的事件集可能为空或不完整——都不是本次读数，判为未知
            counts[source] = None
            continue
        counts[source] = sum(
            1 for ev in entry["events"]
            if ev.ms is not None and window_start <= ev.ms < window_end)
    return counts


# B5 UM maintenance read surface: the ORIGINAL console operation records
# (console_operations.OperationManager), filtered to the UM actions. It never
# creates a second operation ledger and never mixes request rows into it.
UM_MAINTENANCE_ACTIONS = frozenset({
    "account.um_status", "account.um_verify", "account.login"})
UM_MAINTENANCE_SERVICE_ACTIONS = frozenset({
    "service.start", "service.stop", "service.restart"})
UM_MAINTENANCE_TARGET = "chat2api"
UM_MAINTENANCE_MAX_READ = 200


def _is_um_maintenance_operation(op):
    if not isinstance(op, dict):
        return False
    action = op.get("action")
    if action in UM_MAINTENANCE_ACTIONS:
        return True
    return (action in UM_MAINTENANCE_SERVICE_ACTIONS
            and op.get("target") == UM_MAINTENANCE_TARGET)


def _um_maintenance_state(state):
    if state in ("queued", "running", "succeeded", "failed", "cancelled",
                  "interrupted"):
        return state
    return "interrupted"


def _um_maintenance_result(op, state):
    """Bounded human summary; never dumps arbitrary result JSON."""
    error = op.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "error")[:64]
        message = str(error.get("message") or "")[:160]
        return ("%s: %s" % (code, message)).strip(": ")
    result = op.get("result")
    if isinstance(result, dict):
        bits = []
        for key in ("status", "state", "verify", "label", "key", "action",
                     "reason", "detail", "note"):
            value = result.get(key)
            if value is None or isinstance(value, (dict, list)):
                continue
            bits.append("%s=%s" % (key, str(value)[:80]))
        if bits:
            return " · ".join(bits)[:240]
    if state == "succeeded":
        return "已完成（未记录结果）"
    if state == "failed":
        return "失败（未记录错误）"
    if state == "queued":
        return "排队中"
    if state == "running":
        return "执行中"
    if state == "cancelled":
        return "已取消"
    if state == "interrupted":
        return "已中断"
    return "状态未知"


def _um_maintenance_row(op):
    created = op.get("created_at")
    finished = op.get("finished_at")
    at = int(created * 1000) if isinstance(created, (int, float)) \
        and not isinstance(created, bool) else None
    finished_at = int(finished * 1000) \
        if isinstance(finished, (int, float)) \
        and not isinstance(finished, bool) else None
    state = _um_maintenance_state(op.get("state"))
    steps = []
    for step in op.get("steps") or []:
        if not isinstance(step, dict):
            continue
        stamp = step.get("at")
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
            continue
        steps.append({"at": int(stamp * 1000),
                       "note": str(step.get("note") or "")[:240]})
    return {
        "key": str(op.get("id") or ""),
        "action": str(op.get("action") or ""),
        "state": state,
        "at": at,
        "finishedAt": finished_at,
        "steps": steps,
        "result": _um_maintenance_result(op, state),
    }


def um_maintenance(manager=None, limit=50):
    """UM maintenance actions read from the ORIGINAL operation records.

    Only the UM actions are selected: account.um_status / account.um_verify /
    account.login, plus service.start/stop/restart whose target is chat2api.
    A non-UM operation (share.*, another service target) is
    not mixed into this table. The read is bounded to the newest
    UM_MAINTENANCE_MAX_READ original rows; older rows are not claimed to be
    absent. A manager/database failure is ``unavailable``, never an empty list.
    """
    try:
        limit = max(1, min(int(limit), UM_MAINTENANCE_MAX_READ))
    except (TypeError, ValueError):
        limit = 50
    if manager is None:
        try:
            manager = get_console_manager()
        except Exception as exc:  # noqa: BLE001 - report, never empty
            return {
                "rows": [],
                "scope": "UM 维护操作（原操作记录，最多 %d 条）"
                          % UM_MAINTENANCE_MAX_READ,
                "earlier": None,
                "coverage": {
                    "status": "unavailable",
                    "error": "%s: %s" % (type(exc).__name__,
                                         str(exc)[:160]),
                    "operations_read": 0,
                    "rows_returned": 0,
                    "bounded": True,
                    "note": "操作记录不可用不是合法空列表",
                },
            }
    try:
        operations = manager.list_operations(UM_MAINTENANCE_MAX_READ)
    except Exception as exc:  # noqa: BLE001 - report, never empty
        return {
            "rows": [],
            "scope": "UM 维护操作（原操作记录，最多 %d 条）"
                      % UM_MAINTENANCE_MAX_READ,
            "earlier": None,
            "coverage": {
                "status": "unavailable",
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:160]),
                "operations_read": 0,
                "rows_returned": 0,
                "bounded": True,
                "note": "操作记录读取失败不是合法空列表",
            },
        }
    operations_read = len(operations or [])
    operations = [op for op in operations or []
                  if _is_um_maintenance_operation(op)]
    rows = [_um_maintenance_row(op) for op in operations]
    rows.sort(key=lambda row: (
        -(row["at"] if row["at"] is not None else 0), row["key"]))
    rows = rows[:limit]
    return {
        "rows": rows,
        "scope": ("原操作记录中的 UM 维护项（account.um_status / "
                    "account.um_verify / account.login / service.* on "
                    "chat2api）；只读最近 %d 条，不与请求混表"
                    % UM_MAINTENANCE_MAX_READ),
        "earlier": None,
        "coverage": {
            "status": "ok",
            "error": None,
            "operations_read": operations_read,
            "rows_returned": len(rows),
            "bounded": True,
            "note": ("只读最近 %d 条原操作记录；更早的维护项未读取，"
                      "不是空列表" % UM_MAINTENANCE_MAX_READ),
        },
    }


# 控制台前端生产/开发共用路径别名：/gw/usagesrv/* 剥前缀后走既有路由分发，
# 与 5173 dev 代理 rewrite（web/vite.config.ts：strip 前缀、保留其余）
# 语义一致。前缀必须是完整路径段——/gw/usagesrvfoo 不属于别名，按未知路径
# 404；host_allowed/origin_allowed 门在剥前缀前生效，对别名同等约束。
USAGE_ALIAS_PREFIX = "/gw/usagesrv"

# Bounded discard for a rejected request body: the reply is already prepared, and
# closing a connection that still holds unread received data makes Windows answer
# the client's next read with RST — a real client would see a connection abort
# instead of the documented 413/400 (measured as a rare ConnectionAbortedError in
# the console API boundary test). Read and discard at most this many bytes, and
# never wait on a silent client: the socket timeout bounds that wait.
_DISCARD_MAX_BYTES = 1 << 20
_DISCARD_TIMEOUT_S = 2.0


# ---------------------------------------------------- S2 cloud-ops read face
def _load_cloud_ops_module(name):
    """Load ``um-cloud-deploy/<name>.py`` independent of sys.path."""
    import importlib.util
    module = sys.modules.get(name)
    if module is not None:
        return module
    path = os.path.join(HERE, "um-cloud-deploy", name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def cloud_ops_status():
    """Read-only security status for the cloud console (D18-11).

    The budget and automatic-backup sections are gone with their features.
    The independent security floor stays: a missing configuration is
    ``configured: false`` and an unreadable state is ``available: false`` with
    a reason — never a fabricated "no revocations" success.
    """
    out = {
        "available": False,
        # The time this read face was served, not the last time the security
        # state was checked. The console labels it 读取于; it is never evidence
        # that a check ran recently.
        "read_at": int(time.time()),
        "security": {"configured": False, "available": False},
        "reasons": [],
        # These facts are explicitly NOT closed by this read face. They are
        # listed so the page cannot present a read-only status as proof that
        # the remote export or the runtime generation ACK was exercised here.
        "unresolved": {
            "remote_export_confirmed": "not_checked_here",
            "runtime_generation_ack": "not_checked_here",
            "note": "本读面只透出安全状态文件现有字段；远端导出确认与"
                    "运行态 generation ACK 未由本读面验证",
        },
    }
    security_path = os.environ.get("UM_CLOUD_SECURITY_STATE")
    if security_path:
        out["security"]["configured"] = True
        try:
            module = _load_cloud_ops_module("backup_restore")
            state = module.read_security_state(security_path)
            out["security"] = {
                "configured": True, "available": True,
                "generation_floor": state["generation_floor"],
                "records": len(state["records"]),
                "pending_remote": state["pending_remote"],
                "exported_at": state.get("exported_at"),
            }
        except Exception as exc:  # noqa: BLE001 - fail-closed
            out["security"] = {"configured": True, "available": False,
                               "reason": f"{type(exc).__name__}: {exc}"}
            out["reasons"].append("security_unavailable")
    out["available"] = bool(out["security"]["configured"])
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json_attachment(self, code, obj, filename):
        """JSON download with a fixed attachment name (diagnostics export).

        The name is built from a fixed prefix and timestamp digits only, so
        no user value can reach the header.
        """
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % filename)
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
        lengths = self.headers.get_all("Content-Length", [])
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
            return "/" + self.path[len(USAGE_ALIAS_PREFIX):]
        if parsed.path.startswith(USAGE_ALIAS_PREFIX + "/"):
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
        name = path.strip("/")
        if not name:
            return False
        root = os.path.realpath(CONSOLE_DIST_DIR)
        try:
            target = os.path.realpath(os.path.join(root, name))
            inside = os.path.commonpath([root, target]) == root
        except ValueError:  # target resolved onto a different drive
            inside = False
        if not inside or not os.path.isfile(target):
            self._send_json(404, {"error": "unknown path"})
            return True
        ctype = CONSOLE_STATIC_TYPES.get(
            os.path.splitext(target)[1].lower(), "application/octet-stream")
        with open(target, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        return True

    # ----------------------------------------------------- share management API

    def _management_auth(self, write):
        """Management session/token gate for local management APIs.

        Reads may use the HttpOnly usage session or the local control token.
        Writes are allowed with the control token (CLI/control plane). A cookie
        session may also write only when the caller supplies the same-origin
        JavaScript CSRF marker (X-Requested-With). The route layer has already
        rejected foreign Origins and non-loopback Hosts. Shared by the
        /api/share/* and /api/console/* surfaces.
        """
        if control_token_ok(self.headers.get("X-Admin-Control")):
            return True, "control"
        if not session_valid(self.headers.get("Cookie")):
            return False, "no_credentials"
        if write and self.headers.get("X-Requested-With") != "XMLHttpRequest":
            return False, "csrf_required"
        return True, "session"

    def _share_auth(self, write):
        return self._management_auth(write)

    SINGLE_VALUE_HEADERS = (
        "Content-Type", "Content-Length", "Authorization", "Cookie",
        "X-Admin-Control", "X-Requested-With", "Transfer-Encoding",
        "Content-Encoding")

    def _duplicate_header_error(self):
        """Return the offending duplicate sensitive header name, or None.

        Only REPORTS: the route layer sends the single 400 response (R7-a:
        exactly one response per request — the previous in-place send plus
        the route's own 400 produced two responses on one connection).
        """
        for name in self.SINGLE_VALUE_HEADERS:
            if len(self.headers.get_all(name, [])) > 1:
                return f"duplicate header: {name}"
        return None

    def _read_share_json_body(self):
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding"):
            return None, 400, "unsupported_body_encoding"
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1:
            return None, 400, "invalid_content_length"
        try:
            length = int(lengths[0]) if lengths else 0
        except ValueError:
            return None, 400, "invalid_content_length"
        if not 0 <= length <= 65536:
            return None, 413, "request_too_large"
        content_types = self.headers.get_all("Content-Type", [])
        if length > 0 or content_types:
            if len(content_types) > 1 or not content_types:
                return None, 400, "content_type_required"
            ctype = content_types[0].split(";")[0].strip().lower()
            if ctype != "application/json":
                return None, 400, "content_type_required"
        raw = self.rfile.read(length)
        if len(raw) != length:
            return None, 400, "incomplete_body"
        try:
            obj = json.loads(raw) if raw else {}
        except (ValueError, UnicodeError):
            return None, 400, "invalid_json"
        if obj is None:
            obj = {}
        if not isinstance(obj, dict):
            return None, 400, "invalid_json"
        return obj, None, None

    def _reject_unknown_fields(self, body, allowed):
        unknown = set(body) - set(allowed)
        if unknown:
            self._send_json(400, {"error": f"unknown field(s): "
                                            f"{sorted(unknown)}"})
            return True
        return False

    def _share_error(self, exc):
        self._send_json(status_code_for(exc), {"error": str(exc)})

    def _share_usage_query_filters(self, names):
        """Parse the whitelisted usage filters from the query string.

        Returns ``(filters, error)``. Duplicate, unknown or non-text values
        are rejected as a whole (400) instead of silently choosing one; value
        ranges are validated by the metering reader (same bound as the writer).
        """
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        unknown = set(query) - set(names)
        if unknown:
            return None, "unknown_query_field"
        filters = {}
        for name in names:
            values = query.get(name)
            if values is None:
                continue
            if len(values) != 1:
                return None, "duplicate_query_field"
            filters[name] = values[0]
        return filters, None

    def _share_usage_response(self):
        """分享用量只读查询的唯一构造点：返回 (status, payload)。

        两个只读挂载点共用这一份数据与口径，绝不建第二套账本或第二套聚合：

        * ``GET /api/share/usage`` —— share 管理面（``_share_auth``：会话或
          管理令牌），公网管理面鉴权不变；
        * ``GET /api/console/share-usage`` —— 控制台面（R9-R35 本机同源页面
          免凭据直读，本地边界由路由层的 Host/Origin 门承担）。

        可选筛选 ``window``/``key``/``model``/``result`` 由同一 reader 应用，
        默认不筛（完整历史），``filters`` 原样回显。share_metering 缺席、
        账本路径解析不出、读取失败一律如实返回错误码，绝不以 0 请求 /
        0 费用冒充。price_fn 是版本化计价器（固定签名
        (model_id, at_ms, input_tok, output_tok, cache_tok, reasoning_tok) →
        (cost_usd, priced, matched, peak, version_id, exchange_rate)），分享
        参考费用与个人费用走同一条版本链；汇率让人民币金额按每条记录自己的
        版本折算，而不是先聚合 USD 再乘现行汇率。顶层载荷形状不新增
        currency/exchangeRate（人民币字段落在各分组节点与 coverage）。"""
        reader, error = share_usage_reader()
        if reader is None:
            return 503, {"error": error}
        ledger = share_usage_ledger_path()
        if not ledger:
            return 503, {"error": "share_ledger_path_unresolved"}
        filters, filter_error = self._share_usage_query_filters(
            ("window", "key", "model", "result"))
        if filter_error is not None:
            return 400, {"error": "invalid_share_usage_query",
                         "reason": filter_error}
        if "key" in filters:
            filters["key_id"] = filters.pop("key")
        try:
            payload = reader(ledger, estimate_record_with_version_and_rate,
                             limit=SHARE_USAGE_LIMIT, **filters)
        except ValueError:
            # The reader rejects an invalid window/key/model/result value; this
            # is caller input, not a ledger failure.
            return 400, {"error": "invalid_share_usage_query"}
        except Exception as exc:  # noqa: BLE001 — 读不到是错误，不是空数据
            print(f"[usagesrv] share usage error: {type(exc).__name__}: {exc}", flush=True)
            return 502, {"error": "share_usage_unavailable"}
        if not isinstance(payload, dict):
            return 502, {"error": "share_usage_invalid"}
        return 200, self._share_usage_project_key_names(payload)

    def _share_usage_requests_response(self):
        """逐请求分页读面：与汇总同一账本、同一筛选、同一计价链。

        返回 ``{rows, nextCursor, truncated, filters, coverage, checkedAt}``；
        ``rows`` 逐条保留未归属/未知/未报告/未定价，缺失值保持 null。
        分页按账本追加顺序（新→旧）用 cursor 行号前进；读取窗口本身被
        截断时 coverage.truncated/olderRecordsUnread 如实说明更旧记录未读。
        """
        reader, error = share_usage_requests_reader()
        if reader is None:
            return 503, {"error": error}
        ledger = share_usage_ledger_path()
        if not ledger:
            return 503, {"error": "share_ledger_path_unresolved"}
        filters, filter_error = self._share_usage_query_filters(
            ("window", "key", "model", "result", "cursor", "limit"))
        if filter_error is not None:
            return 400, {"error": "invalid_share_usage_query",
                         "reason": filter_error}
        if "limit" in filters:
            raw_limit = filters["limit"]
            if not raw_limit.isdigit() or not 1 <= int(raw_limit) <= 200:
                return 400, {"error": "invalid_share_usage_query",
                             "reason": "limit"}
            filters["limit"] = int(raw_limit)
        if "key" in filters:
            filters["key_id"] = filters.pop("key")
        try:
            payload = reader(ledger, estimate_record_with_version_and_rate,
                             **filters)
        except ValueError:
            return 400, {"error": "invalid_share_usage_query"}
        except Exception as exc:  # noqa: BLE001 — 读不到是错误，不是空页
            print(f"[usagesrv] share usage requests error: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            return 502, {"error": "share_usage_unavailable"}
        if not isinstance(payload, dict):
            return 502, {"error": "share_usage_invalid"}
        return 200, self._share_usage_project_request_key_names(payload)

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
        return self._share_usage_attach_key_labels(
            payload, "keys", "key_id")

    def _share_usage_project_request_key_names(self, payload):
        """逐请求页同一白名单投影；行身份字段是 camelCase ``keyId``。"""
        return self._share_usage_attach_key_labels(
            payload, "rows", "keyId")

    def _share_usage_attach_key_labels(self, payload, rows_key, id_key):
        rows = payload.get(rows_key)
        if not isinstance(rows, list):
            return payload
        wanted = set()
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            key_id = entry.get(id_key)
            if isinstance(key_id, str) and key_id:
                wanted.add(key_id)
        labels = None
        if wanted:
            try:
                path = os.environ.get("SHARE_STORE_FILE") or os.path.join(
                    HERE, "data", "share-store.db")
                labels = read_key_labels(path, wanted)
            except (StoreError, OSError, ValueError):
                labels = None
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            row = None
            key_id = entry.get(id_key)
            if isinstance(labels, dict) and isinstance(key_id, str):
                row = labels.get(key_id)
            if row is None:
                entry["keyName"] = None
                entry["deleted"] = None
                continue
            name = row.get("name")
            entry["keyName"] = name if isinstance(name, str) else None
            # read_key_labels 已把「存量 enabled=0」与新撤销一起判为已撤销，
            # 这里直接用它，不再只看 deleted_at。
            deleted = row.get("deleted")
            if deleted is None:
                deleted = row.get("deleted_at") is not None
            entry["deleted"] = bool(deleted)
        coverage = payload.get("coverage")
        if isinstance(coverage, dict):
            coverage["keyNames"] = (None if labels is None else len(labels))
        return payload

    def _share_api(self, parsed, method):
        dup = self._duplicate_header_error()
        if dup is not None:
            # Sensitive duplicated headers: the request body was never read,
            # so the connection cannot be reused after this single response
            # (R7-a: exactly one response, then close).
            self.close_connection = True
            self._send_json(400, {"error": dup})
            return
        ok, how = self._share_auth(method in ("POST", "PATCH", "DELETE"))
        if not ok:
            self.close_connection = True  # a write body would be unconsumed
            if how == "csrf_required":
                self._send_json(403, {"error": "session CSRF marker required"})
            else:
                self._send_json(401, {"error": "share management credentials required"})
            return
        path = parsed.path
        segments = [s for s in path.split("/") if s]
        # segments are api/share/...
        if len(segments) < 3 or segments[:2] != ["api", "share"]:
            self._send_json(404, {"error": "unknown path"})
            return
        rest = segments[2:]
        if method == "GET" and rest in (["usage"], ["usage", "requests"]):
            # 只读分享用量：不依赖 ShareManager 装配（broker 不可用不影响该查询），
            # 鉴权与其余 /api/share/* 相同（读：会话或管理令牌）。汇总与逐请求页
            # 共用同一账本、筛选与计价链；构造点与 GET /api/console/share-usage
            # 及 /api/console/share-usage/requests 共用。
            if rest == ["usage", "requests"]:
                status, payload = self._share_usage_requests_response()
            else:
                status, payload = self._share_usage_response()
            self._send_json(status, payload)
            return
        try:
            # 配置装配失败（BrokerError）必须走同一结构化 503 路径，
            # 不能落在 try 外变成连接中断。
            manager = get_share_manager()
            if method == "GET" and rest == ["status"]:
                self._send_json(200, manager.status())
                return
            if method == "GET" and rest == ["keys"]:
                self._send_json(200, manager.list_keys())
                return
            if method == "GET" and len(rest) == 2 and rest[0] == "keys":
                self._send_json(200, manager.get_key(rest[1]))
                return
            if method == "GET" and len(rest) == 3 and rest[0] == "keys" \
                    and rest[2] == "secret":
                # Same narrow readback contract as the console facade: the
                # management surface requires the management token/session, the
                # plaintext rides only this immediate no-store response and is
                # never an operation record, list row or log line.
                expected_raw = (parse_qs(urlparse(self.path).query)
                                .get("expected_row_version") or [""])[0]
                status, payload = self._share_secret_response(
                    manager, rest[1], expected_raw)
                self._send_json(status, payload)
                return
            if method == "GET" and len(rest) == 2 and rest[0] == "operations":
                self._send_json(200, manager.get_operation(rest[1]))
                return

            body, body_code, body_error = self._read_share_json_body()
            if body_code is not None:
                # The request body may not have been consumed (oversize,
                # bad Content-Length): the connection cannot be reused, and
                # the unread bytes must be discarded before the reply goes
                # out (see _discard_unread_body).
                self.close_connection = True
                self._discard_unread_body()
                self._send_json(body_code, {"error": body_error})
                return

            def _bad(message):
                self._send_json(400, {"error": message})
                return True

            if method == "POST" and rest == ["limits", "normalize"]:
                # D18-4/C1: the explicit operator normalization action. It is
                # never run silently at startup; the CLI `limits --normalize`
                # reaches the SAME manager action through this documented
                # route instead of falling through to the unknown-path 404.
                if self._reject_unknown_fields(body, ()):
                    return
                self._send_json(200, manager.normalize_limits())
                return

            if method == "POST" and rest == ["keys"]:
                if self._reject_unknown_fields(
                        body, ("name", "idempotency_key", "max_concurrency")):
                    return
                name = body.get("name")
                idem = body.get("idempotency_key")
                if not isinstance(name, str) or not 1 <= len(name) <= 64:
                    _bad("invalid name")
                    return
                if idem is not None and (not isinstance(idem, str)
                                         or not 1 <= len(idem) <= 128):
                    _bad("invalid idempotency_key")
                    return
                max_concurrency = body.get("max_concurrency")
                if max_concurrency is not None:
                    if isinstance(max_concurrency, bool) \
                            or not isinstance(max_concurrency, int) \
                            or not 1 <= max_concurrency <= 48:
                        _bad("max_concurrency must be an integer in 1..48")
                        return
                self._send_json(201, manager.create_key(
                    name, max_concurrency=max_concurrency,
                    idempotency_key=idem))
                return
            if method == "POST" and len(rest) == 3 and rest[0] == "keys" \
                    and rest[2] == "test":
                # Explicit version-bound key test through the public Funnel.
                # The expected row version is REQUIRED: a stale page cannot
                # test a secret it never rendered, and a key changed during
                # the probe answers key_changed instead of a fake success.
                if self._reject_unknown_fields(
                        body, ("expected_row_version",)):
                    return
                expected = body.get("expected_row_version")
                if not rest[1] or len(rest[1]) > 128 \
                        or isinstance(expected, bool) \
                        or not isinstance(expected, int) or expected < 1:
                    _bad("invalid key_id or expected_row_version")
                    return
                from console.share_probe import test_key
                self._send_json(200, test_key(
                    manager, rest[1], expected,
                    discover_url=lambda: self._share_funnel_url(refresh=True)))
                return
            if method == "POST" and len(rest) == 3 and rest[0] == "keys"                     and rest[2] == "revoke-and-terminate":
                # R8-A7 revoke-and-terminate: disable + one-shot interrupt of
                # THIS key's in-flight share requests (other keys and the
                # local machine are never touched).
                if self._reject_unknown_fields(
                        body, ("idempotency_key", "expected_row_version")):
                    return
                if len(rest[1]) > 128:
                    _bad("invalid key_id")
                    return
                idem = body.get("idempotency_key")
                if idem is not None and (not isinstance(idem, str)
                                         or not 1 <= len(idem) <= 128):
                    _bad("invalid idempotency_key")
                    return
                expected = body.get("expected_row_version")
                if isinstance(expected, bool) or not isinstance(expected, int)                         or expected < 1:
                    _bad("expected_row_version must be a positive integer")
                    return
                self._send_json(200, manager.revoke_and_terminate_key(
                    rest[1], expected_row_version=expected,
                    idempotency_key=idem))
                return
            if method == "POST" and len(rest) == 3 and rest[0] == "keys" and rest[2] == "rotate":
                if self._reject_unknown_fields(
                        body, ("idempotency_key", "expected_row_version")):
                    return
                if len(rest[1]) > 128:
                    _bad("invalid key_id")
                    return
                idem = body.get("idempotency_key")
                if idem is not None and (not isinstance(idem, str)
                                         or not 1 <= len(idem) <= 128):
                    _bad("invalid idempotency_key")
                    return
                expected = body.get("expected_row_version")
                if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
                    _bad("expected_row_version required")
                    return
                self._send_json(200, manager.rotate_key(
                    rest[1], idempotency_key=idem,
                    expected_row_version=expected))
                return
            if method == "PATCH" and len(rest) == 2 and rest[0] == "keys":
                # D18-3: `enabled` is NOT writable — a disabled or revoked
                # identity can never be re-enabled; the row keeps it as a
                # read-only fact. Only rename and the per-key concurrency
                # limit (1..48) are accepted.
                if "enabled" in body:
                    _bad("enabled_not_writable")
                    return
                if self._reject_unknown_fields(
                        body, ("name", "max_concurrency",
                               "expected_row_version")):
                    return
                if len(rest[1]) > 128:
                    _bad("invalid key_id")
                    return
                name = body.get("name")
                max_concurrency = body.get("max_concurrency")
                expected = body.get("expected_row_version")
                if name is not None and (not isinstance(name, str)
                                         or not 1 <= len(name) <= 64):
                    _bad("invalid name")
                    return
                if max_concurrency is not None:
                    if isinstance(max_concurrency, bool) \
                            or not isinstance(max_concurrency, int) \
                            or not 1 <= max_concurrency <= 48:
                        _bad("max_concurrency must be an integer in 1..48")
                        return
                if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
                    _bad("expected_row_version required")
                    return
                self._send_json(200, manager.update_key(
                    rest[1], name=name,
                    max_concurrency=max_concurrency,
                    expected_row_version=expected))
                return
            if method == "PATCH" and rest == ["settings"]:
                # Global share limits (D18-4): the ONLY writable body fields
                # are share_max_concurrency (+ the optional optimistic-gate
                # expected_share_max). The GLOBAL bound is 1..64 (all keys and
                # every entry point together); the per-key routes stay 1..48 —
                # the two are separate contracts.
                if self._reject_unknown_fields(
                        body, ("share_max_concurrency", "expected_share_max")):
                    return
                share_max = body.get("share_max_concurrency")
                if isinstance(share_max, bool) or not isinstance(share_max, int) \
                        or not 1 <= share_max <= 64:
                    _bad("share_max_concurrency must be an integer in 1..64")
                    return
                expected_max = body.get("expected_share_max")
                if expected_max is not None and (
                        isinstance(expected_max, bool)
                        or not isinstance(expected_max, int)):
                    _bad("expected_share_max must be an integer")
                    return
                self._send_json(200, manager.set_share_limits(
                    share_max, expected_share_max=expected_max))
                return
            if method == "DELETE" and len(rest) == 2 and rest[0] == "keys":
                if self._reject_unknown_fields(body, ("expected_row_version",)):
                    return
                if len(rest[1]) > 128:
                    _bad("invalid key_id")
                    return
                expected = body.get("expected_row_version")
                if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
                    _bad("expected_row_version required")
                    return
                self._send_json(200, manager.delete_key(
                    rest[1], expected_row_version=expected))
                return
            if method == "POST" and rest in (["start"], ["stop"], ["emergency-stop"]):
                if self._reject_unknown_fields(body, ("idempotency_key",)):
                    return
                idem = body.get("idempotency_key")
                if idem is not None and (not isinstance(idem, str)
                                         or not 1 <= len(idem) <= 128):
                    _bad("invalid idempotency_key")
                    return
                if rest == ["start"]:
                    self._send_json(202, manager.start(idempotency_key=idem))
                elif rest == ["stop"]:
                    self._send_json(202, manager.graceful_stop(idempotency_key=idem))
                else:
                    self._send_json(202, manager.emergency_stop(idempotency_key=idem))
                return
        except (StoreError, BrokerError) as exc:
            self.close_connection = True
            self._share_error(exc)
            return
        except Exception as exc:  # noqa: BLE001
            print(f"[usagesrv] share api error: {type(exc).__name__}: {exc}", flush=True)
            self.close_connection = True
            self._send_json(500, {"error": "share management error"})
            return
        self._send_json(404, {"error": "unknown path"})

    # ------------------------------ console share-management facade (A36-2)

    @staticmethod
    def _share_secret_response(manager, key_id, expected_raw):
        """One key's plaintext readback, shared by both management surfaces.

        The plaintext rides ONLY this immediate no-store response. Outcomes are
        distinct: legacy / undecryptable answer an honest inline unavailable
        state (an expected condition until manual rotation, not an error toast);
        deleted answers 410; unknown id 404; a stale expected_row_version answers
        the version-conflict state instead of plaintext.
        """
        if not key_id or len(key_id) > 128:
            return 400, {"error": "invalid key_id"}
        expected = None
        if expected_raw:
            try:
                expected = int(expected_raw)
            except ValueError:
                return 400, {"error": "invalid expected_row_version"}
            if expected < 1:
                return 400, {"error": "invalid expected_row_version"}
        try:
            payload = manager.reveal_key_secret(
                key_id, expected_row_version=expected)
        except Disabled:
            return 410, {"error": "key_deleted"}
        except VersionMismatch:
            return 200, {"key_id": key_id, "secret_unavailable": True,
                         "reason": "version_conflict"}
        except SecretUnavailable:
            return 200, {"key_id": key_id, "secret_unavailable": True,
                         "reason": "legacy"}
        except SecretUndecryptable:
            return 200, {"key_id": key_id, "secret_unavailable": True,
                         "reason": "undecryptable"}
        return 200, payload

    @staticmethod
    def _console_share_public_row(row):
        """Map a ShareManager key row onto the console facade contract.

        Display-safe fields only: the masked identifier is the closest
        secret-adjacent value any console response carries — plaintext and
        hashes never map into this shape (A36-2)."""
        return {
            "id": row.get("key_id"),
            "name": row.get("name"),
            "masked": row.get("masked_key") or row.get("mk"),
            "plaintext_viewable": bool(row.get("plaintext_viewable")),
            "enabled": row.get("enabled"),
            "max_concurrency": row.get("max_concurrency"),
            "row_version": row.get("row_version"),
            "created_at": row.get("created_at"),
        }

    # Public funnel URL cache: the node's Tailscale DNS name only changes on
    # tailnet re-login, so one successful probe is reused; the probe only
    # happens while the funnel is observed on and the cache is empty.
    _share_url_cache = {"url": None}

    @classmethod
    def _tailscale_funnel_url(cls, *, refresh=False):
        """The historical Tailscale Funnel URL (D18-1 fallback only)."""
        if not refresh and cls._share_url_cache["url"]:
            return cls._share_url_cache["url"]
        try:
            import subprocess
            proc = subprocess.run(["tailscale", "status", "--json"],
                                  capture_output=True, timeout=8)
            if proc.returncode != 0:
                return None
            node = json.loads(proc.stdout.decode("utf-8", "replace"))
            if refresh and node.get("BackendState") != "Running":
                return None
            name = str((node.get("Self") or {}).get("DNSName") or "").strip(".")
            if name:
                cls._share_url_cache["url"] = "https://%s" % name
                return cls._share_url_cache["url"]
        except Exception:
            pass
        return None if refresh else cls._share_url_cache["url"]

    @classmethod
    def _share_public_url_detail(cls, *, refresh=False):
        """``(url, source, reason)``: configured origin first, TS second.

        An invalid ``CONSOLE_PUBLIC_ORIGIN`` is an explicit error and never
        falls back to the Tailscale URL. With the key absent the historical
        Funnel discovery is unchanged.
        """
        resolved = console_public_url.resolve_configured()
        if resolved.source is not None \
                or resolved.reason == console_public_url.INVALID:
            return resolved
        return console_public_url.resolve(
            cls._tailscale_funnel_url(refresh=refresh))

    @classmethod
    def _share_funnel_url(cls, *, refresh=False):
        return cls._share_public_url_detail(refresh=refresh).url

    @staticmethod
    def _console_share_capacity(payload):
        """Configured admission cap vs observed in-flight / queue capacity.

        ``configured`` is the user-configured global share limit from this
        payload's ``share_max_concurrency`` (the note states that value, or
        "未上报" when the payload carries none) — an ADMISSION cap, never a
        measured upstream capacity. ``in_flight`` comes from the restricted
        broker's read-only maintenance view (the running 8100 instance's
        ``active_count``); an unwired/unreadable broker keeps it null with a
        reason. The dispatcher's queue depth and the upstream model capacity
        have no read seam on this surface, so both stay null with an explicit
        reason — never 0.
        """
        configured = payload.get("share_max_concurrency")
        if isinstance(configured, bool) or not isinstance(configured, int) \
                or configured < 1:
            configured = None
        in_flight = None
        checked_at = None
        reason = "maintenance_unwired"
        try:
            from console import share_actions as console_share_actions
            seam = getattr(console_share_actions, "_MAINTENANCE_STATUS", None)
            if seam is not None:
                view = seam()
                if isinstance(view, dict):
                    checked = view.get("checked_at")
                    if isinstance(checked, (int, float)) \
                            and not isinstance(checked, bool) and checked > 0:
                        checked_at = int(checked)
                    instance = view.get("instance")
                    if isinstance(instance, dict):
                        active = instance.get("active_count")
                        if isinstance(active, int) \
                                and not isinstance(active, bool) and active >= 0:
                            in_flight = active
                            reason = None
                        else:
                            reason = "instance_active_count_unavailable"
                    else:
                        reason = "instance_unavailable"
                else:
                    reason = "maintenance_unavailable"
        except Exception:  # noqa: BLE001 — unreadable stays unknown, never 0
            reason = "maintenance_unavailable"
        configured_note = "未上报" if configured is None else str(configured)
        return {
            "configured": configured,
            "in_flight": in_flight,
            "in_flight_reason": reason,
            "queued": None,
            "queued_reason": "not_exposed",
            "upstream_capacity": None,
            "upstream_capacity_measured": False,
            "upstream_capacity_reason": "not_measured",
            "checked_at": checked_at,
            "note": "configured 是用户配置的准入上限（当前部署 %s），"
                    "不是上游实测承载量；in_flight 来自受限后台的实例状态，"
                    "排队数与上游容量本读面未暴露/未实测" % configured_note,
        }

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
        snap = out.get("broker")
        if isinstance(snap, dict):
            mapped = {}
            state = snap.get("state")
            # BrokerClientAdapter nests the FULL broker status result (which
            # carries version/pid and the sanitized BrokerDiag ring) under
            # "broker"; without unwrapping it the console UI only ever sees
            # the bare "unconfirmed" word with no stage evidence.
            inner = snap.get("broker")
            inner = inner if isinstance(inner, dict) else {}
            if state == "ok":
                mapped["running"] = True
            elif state == "unavailable":
                mapped["running"] = False
                code = snap.get("error_code")
                if isinstance(code, str) and code:
                    mapped["error_code"] = code
            version = inner.get("version") if inner else snap.get("version")
            pid = inner.get("pid") if inner else snap.get("pid")
            if isinstance(version, str):
                mapped["version"] = version
            if isinstance(pid, int) and not isinstance(pid, bool):
                mapped["pid"] = pid
            diag = inner.get("diag")
            if isinstance(diag, list) and diag:
                # BrokerDiag entries are pre-sanitized (stage/ok/error_code/
                # exception_type/detail capped, no secrets) — pass the tail
                # through verbatim for the console's publish-failure view.
                mapped["diag"] = list(diag[-5:])
            out["broker"] = mapped
        return out

    @staticmethod
    def _console_share_public_module():
        """The public supervision module, or None when it is not deployed.

        Import failure is "module unavailable" (an honest 200 available:false),
        never an exception that turns into a 500.
        """
        try:
            share_public_supervision = __import__("importlib").import_module("share.public_supervision")
        except Exception:  # noqa: BLE001
            return None
        return share_public_supervision

    def _console_share_public_payload(self):
        """GET /public: snapshot + bounded event tail + budget + cancel."""
        supervision = self._console_share_public_module()
        if supervision is None:
            return {"available": False,
                    "reason": "public_supervision_unavailable",
                    "snapshot": None, "events": None,
                    "events_truncated": None, "events_unreadable": None,
                    "budget": None, "cancel": None, "unreadable": True}
        try:
            return supervision.read_status()
        except Exception:  # noqa: BLE001  unreadable is not "no snapshot"
            return {"available": True, "version": None, "checked_at": None,
                    "snapshot": None, "events": None,
                    "events_truncated": None, "events_unreadable": None,
                    "budget": None, "cancel": None,
                    "unreadable": True, "reason": "state_unreadable",
                    "state_file": getattr(supervision, "STATE_FILE_NAME", None)}

    def _console_share_public_events_payload(self):
        """GET /public-events: bounded, newest-first event page."""
        query = parse_qs(urlparse(self.path).query)
        raw_limit = (query.get("limit") or ["50"])[0]
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return 400, {"error": "invalid_events_query"}
        if not 1 <= limit <= 200:
            return 400, {"error": "invalid_events_query"}
        before = None
        raw_before = (query.get("before") or [""])[0]
        if raw_before:
            try:
                before = float(raw_before)
            except (TypeError, ValueError):
                return 400, {"error": "invalid_events_query"}
            if not before > 0 or before in (float("inf"), float("-inf")) \
                    or before != before:
                return 400, {"error": "invalid_events_query"}
        supervision = self._console_share_public_module()
        if supervision is None:
            return 200, {"available": False, "events": None,
                          "next_before": None, "truncated": None,
                          "reason": "public_supervision_unavailable"}
        try:
            status = supervision.read_status(
                event_limit=supervision.EVENT_MAX_RETURN)
        except Exception:  # noqa: BLE001  never render "no events" for a read failure
            return 200, {"available": True, "events": None,
                          "next_before": None, "truncated": None,
                          "reason": "state_unreadable"}
        events = status.get("events") if isinstance(status, dict) else None
        if not isinstance(events, list):
            return 200, {"available": True, "events": None,
                          "next_before": None, "truncated": None,
                          "reason": "events_unreadable"}
        rows = []
        for item in events:
            if not isinstance(item, dict):
                continue
            try:
                at = datetime.fromisoformat(str(item.get("at_utc"))).timestamp()
            except (TypeError, ValueError, OverflowError):
                continue
            if before is not None and at >= before:
                continue
            rows.append((at, item))
        rows.sort(key=lambda pair: pair[0], reverse=True)
        page = rows[:limit]
        next_before = page[-1][0] if len(rows) > limit and page else None
        return 200, {
            "available": True,
            "events": [{"at_utc": item.get("at_utc"),
                         "status": item.get("status"),
                         "code": item.get("code"),
                         "action": item.get("action")}
                        for _at, item in page],
            "next_before": next_before,
            "truncated": bool(status.get("events_truncated")),
        }

    def _console_share_public_export_payload(self):
        """GET /public-export: redacted diagnostics attachment."""
        query = parse_qs(urlparse(self.path).query)
        fmt = (query.get("format") or ["json"])[0]
        if fmt != "json":
            return 400, {"error": "invalid_export_format"}, None
        supervision = self._console_share_public_module()
        if supervision is None:
            return 503, {"error": "share_public_export_unavailable"}, None
        try:
            payload = supervision.export_diagnostics()
        except Exception:  # noqa: BLE001  export unavailable, never a fake file
            return 503, {"error": "share_public_export_unavailable"}, None
        generated = payload.get("generated_at") \
            if isinstance(payload, dict) else None
        try:
            stamp = datetime.fromisoformat(str(generated)).strftime(
                "%Y%m%d-%H%M%S")
        except (TypeError, ValueError, OverflowError):
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return 200, payload, "share-public-diagnostics-%s.json" % stamp

    def _console_display_session_payload(self):
        """GET /display-session: same-origin launcher path, never credentials.

        The browser receives only a relative path.  The display grant is a
        separate management action; this read answers ``available=false`` when
        the request has no Tailscale Serve identity, admin control token,
        console session or existing display session.  A bare loopback request is
        not an administrator: an SSH -L tunnel also looks like loopback.
        """
        if DISPLAY_PROXY is None:
            return {
                "available": False, "url": None,
                "reason": "display_proxy_unavailable",
                "expires_at": None, "mode": "novnc",
                "operator": self._console_operator_default(),
            }
        config = DISPLAY_PROXY.config
        if not config.enabled:
            return {
                "available": False, "url": None,
                "reason": config.error or "display_disabled",
                "expires_at": None, "mode": "novnc",
                "operator": self._console_operator_default(),
            }
        if not self._console_display_authorized():
            return {
                "available": False, "url": "/display/",
                "reason": "display_authorization_required",
                "expires_at": None, "mode": "novnc",
                "operator": self._console_operator_default(),
            }
        status, grant = self._console_display_grant_payload()
        if status != 200 or not isinstance(grant, dict) \
                or not isinstance(grant.get("url"), str):
            return {
                "available": False, "url": None,
                "reason": (grant.get("error")
                           if isinstance(grant, dict) else None)
                          or "display_grant_unavailable",
                "expires_at": None, "mode": "novnc",
                "operator": self._console_operator_default(),
            }
        return {
            "available": True, "url": grant["url"], "reason": "",
            "expires_at": grant.get("expires_at"),
            "mode": "novnc",
            "operator": self._console_operator_state(),
        }

    def _console_display_origin(self):
        """The console display request origin, or None (fail closed)."""
        if DISPLAY_PROXY is None:
            return None
        try:
            client_ip = self.client_address[0]
        except (AttributeError, IndexError, TypeError):
            client_ip = ""
        try:
            return display_request_origin(
                self.headers, client_ip, TAILNET_POLICY, PORT,
                DISPLAY_PROXY.session_valid, DISPLAY_PROXY.control_token_ok,
                require_authorization=False)
        except Exception:  # noqa: BLE001  an auth read must fail closed
            return None

    def _console_display_authorized(self):
        if DISPLAY_PROXY is None:
            return False
        origin = self._console_display_origin()
        if origin is None:
            return False
        try:
            return DISPLAY_PROXY._display_authorized(self.headers, origin)
        except Exception:  # noqa: BLE001  an auth read must fail closed
            return False

    @staticmethod
    def _console_operator_default():
        return {"owned_by_me": False, "owner_present": False,
                "generation": 0, "stale": False}

    def _console_operator_state(self):
        """The single-page operator state for the display-session read.

        Only a live display session can report ``owned_by_me``; an admin
        request without a display session still sees whether another page
        holds the operator. An unavailable proxy or denied origin reads the
        honest empty state, never a fabricated owner.
        """
        default = self._console_operator_default()
        if DISPLAY_PROXY is None or not DISPLAY_PROXY.config.enabled:
            return default
        origin = self._console_display_origin()
        if origin is None:
            return default
        try:
            state = DISPLAY_PROXY.operator_state(
                self.headers.get("Cookie"), origin)
        except Exception:  # noqa: BLE001  an auth read must fail closed
            return default
        return state if isinstance(state, dict) else default

    @staticmethod
    def _console_display_availability():
        if DISPLAY_PROXY is None:
            return {"available": False, "reason": "display_proxy_unavailable"}
        config = DISPLAY_PROXY.config
        if not config.enabled:
            return {"available": False,
                    "reason": config.error or "display_disabled"}
        return {"available": True, "reason": None}

    @staticmethod
    def _console_display_state_of(auth, display):
        """Map one read-only auth observation to the display-state contract.

        ``valid`` only comes from THIS read's ``state == "valid"``; an old
        login action, a black screen or an rfb connect never implies it.
        ``waiting_mfa`` needs an explicit MFA/manual reason. ``not_started``
        and ``display_disconnected`` stay separate facts.
        """
        state = auth.get("state")
        error = auth.get("error")
        reason = error
        if isinstance(reason, str) and reason.startswith("manual_takeover:"):
            reason = reason.split(":", 1)[1]
        if state == "valid":
            return "valid", "登录有效", None
        if state == "cancelled":
            return "cancelled", "本轮登录已取消", None
        if state == "timed_out":
            return "timed_out", "登录窗口已超时", None
        if state in ("invalid", "failed"):
            if reason == "timeout":
                return "timed_out", "登录窗口已超时", error
            return "failed", "登录失败", error
        if state in ("logging_in", "needs_login"):
            if reason in ("umpass_mfa_required", "duo_prompt_waiting"):
                return "waiting_mfa", "等待 MFA（本人操作）", error
            return "logging_in", "正在登录", error
        if state is None:
            if not display["available"]:
                return ("display_disconnected", "显示连接中断",
                        display["reason"])
            return "not_started", "未开始登录", None
        return "unknown", "状态未知", error

    def _console_display_state_payload(self):
        """GET /display-state: read-only auth + operator + display facts.

        The read never triggers a login, never starts/stops the dedicated
        instance and never extends the login window: the deadline comes from
        the instance's OWN one-time window and is passed through verbatim.
        """
        from console import share_actions as console_share_actions
        failed = None
        try:
            auth = console_share_actions.backend_auth_read()
        except Exception as exc:  # noqa: BLE001 - unreadable is unknown
            failed = "%s: %s" % (type(exc).__name__, exc)
            auth = {"state": None, "label": None, "error": None,
                    "recovery": None, "observed_at": None,
                    "login_window_deadline_epoch": None}
        display = self._console_display_availability()
        state, label, detail = self._console_display_state_of(auth, display)
        if failed is not None:
            state, label, detail = "unknown", "状态未知", failed
        return {
            "available": True,
            "state": state,
            "label": label,
            "detail": detail,
            "observed_at": auth.get("observed_at"),
            "login_window_deadline_epoch":
                auth.get("login_window_deadline_epoch"),
            "operator": self._console_operator_state(),
            "display": display,
        }

    def _console_backend_login_cancel_payload(self):
        """POST /backend-login-cancel: end THIS login round only (D18-8)."""
        from console import share_actions as console_share_actions
        from console.operations import OperationError
        try:
            result = console_share_actions.backend_login_cancel("", {})
        except OperationError as exc:
            return 503, {"cancelled": False, "state": None,
                         "detail": str(exc), "error": exc.code}
        if not isinstance(result, dict):
            return 503, {"cancelled": False, "state": None,
                         "detail": "登录取消响应不符合契约"}
        cancelled = bool(result.get("cancelled"))
        state = result.get("state")
        if state not in ("cancelled", "no_login_in_progress"):
            state = "cancelled" if cancelled else "no_login_in_progress"
        detail = result.get("detail")
        return 200, {"cancelled": cancelled, "state": state,
                     "detail": detail if isinstance(detail, str) else None}

    def _console_display_operator_payload(self, body):
        """POST /display-operator: acquire or explicitly take over (D18-9)."""
        if DISPLAY_PROXY is None:
            return 503, {"error": "display_proxy_unavailable"}
        if not DISPLAY_PROXY.config.enabled:
            return 503, {"error": DISPLAY_PROXY.config.error
                         or "display_disabled"}
        action = body.get("action")
        if action not in ("acquire", "takeover"):
            return 400, {"error": "display_operator_action_invalid"}
        origin = self._console_display_origin()
        if origin is None:
            return 403, {"error": "display_authorization_required"}
        try:
            result = DISPLAY_PROXY.acquire_operator(
                self.headers.get("Cookie"), origin, action=action)
        except Exception:  # noqa: BLE001  never fake an acquired owner
            return 503, {"error": "display_operator_unavailable"}
        if not isinstance(result, dict):
            return 503, {"error": "display_operator_unavailable"}
        if result.get("acquired"):
            return 200, {"acquired": True,
                         "generation": result.get("generation"),
                         "took_over": bool(result.get("took_over"))}
        error = result.get("error")
        if error == "display_operator_exists":
            return 409, {"error": "display_operator_exists",
                         "generation": result.get("generation"),
                         "owner_label": result.get("owner_label")}
        if error == "display_session_required":
            return 409, {"error": "display_session_required"}
        if error == "display_operator_action_invalid":
            return 400, {"error": "display_operator_action_invalid"}
        return 409, {"error": error or "display_operator_unavailable"}

    def _console_display_grant_payload(self):
        """POST /display-grant: bind a one-time grant to a management subject.

        The grant token is returned once in this no-store response.  It is bound
        to the exact origin and to the identity that proved itself here:
        Tailscale Serve's owner/device plus a console session, the admin control
        token, or the console session.  A bare loopback request is refused, so
        an arbitrary SSH -L client cannot turn itself into a display admin.
        """
        if DISPLAY_PROXY is None:
            return 503, {"error": "display_proxy_unavailable"}
        config = DISPLAY_PROXY.config
        if not config.enabled:
            return 503, {"error": config.error or "display_disabled"}
        try:
            client_ip = self.client_address[0]
        except (AttributeError, IndexError, TypeError):
            client_ip = ""
        try:
            binding = display_request_subject(
                self.headers, client_ip, TAILNET_POLICY, PORT,
                DISPLAY_PROXY.session_valid, DISPLAY_PROXY.control_token_ok,
                display_session_valid=DISPLAY_PROXY._display_session_valid)
        except Exception:  # noqa: BLE001  an auth read must fail closed
            binding = None
        if binding is None:
            return 403, {"error": "display_authorization_required"}
        origin, subject = binding
        try:
            token, expires_at = DISPLAY_PROXY.grants.issue(subject, origin)
        except DisplayGrantError as exc:
            return 503, {"error": str(exc)}
        return 200, {
            "grant": token,
            "expires_at": int(expires_at),
            "origin": origin,
            "url": "/display/grant#grant=" + token,
        }

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
        if method in ("POST", "PATCH", "DELETE"):
            if not control_token_ok(self.headers.get("X-Admin-Control")) \
                    and self.headers.get("X-Requested-With") != "XMLHttpRequest":
                self.close_connection = True  # a write body would be unconsumed
                self._send_json(403, {
                    "error": "same-origin marker or control token required"})
                return
        if method == "GET" and rest == ["display-session"]:
            self._send_json(200, self._console_display_session_payload())
            return
        if method == "GET" and rest == ["display-state"]:
            # D18-7/D18-8: one read-only snapshot of the instance's CURRENT
            # auth observation plus the display relay and operator facts. It
            # never triggers a login and never extends the login window.
            self._send_json(200, self._console_display_state_payload())
            return
        if method == "POST" and rest == ["backend-login-cancel"]:
            status, payload = self._console_backend_login_cancel_payload()
            self._send_json(status, payload)
            return
        if method == "POST" and rest == ["display-operator"]:
            body, body_code, body_error = self._read_share_json_body()
            if body_code is not None:
                self.close_connection = True
                self._discard_unread_body()
                self._send_json(body_code, {"error": body_error})
                return
            if self._reject_unknown_fields(body, ("action",)):
                return
            status, payload = self._console_display_operator_payload(body)
            self._send_json(status, payload)
            return
        if method == "POST" and rest == ["display-grant"]:
            status, payload = self._console_display_grant_payload()
            self._send_json(status, payload)
            return
        try:
            # 配置装配失败（BrokerError）与 _share_api 同走结构化 503，
            # 不能落在 try 外变成连接中断。
            if rest == ["control-status"]:
                if method != "GET":
                    self._send_json(405, {"error": "method not allowed"})
                    return
                # Fixed loopback health read: no store construction/recovery,
                # account probe, Funnel query or operation side effects.
                from console.share_production import production_broker_ops
                from console.operations import OperationError
                try:
                    production_broker_ops().ensure_control_ready()
                    payload = {"responsive": True}
                except OperationError as exc:
                    code = exc.code if exc.code in (
                        "share_broker_unavailable", "share_broker_unwired") else "share_control_unconfirmed"
                    payload = {"responsive": False, "error_code": code}
                except (BrokerError, OSError, ValueError):
                    payload = {"responsive": False, "error_code": "share_control_unconfirmed"}
                self._send_json(200, payload)
                return
            # S5（契约 §5.1）：公网巡检只读/导出三个入口。它们必须在
            # ``get_share_manager()`` 之前处理——读面只读巡检模块自己的
            # 状态文件，不构造分享 store、不探测账号、不触发 Funnel 查询。
            if method == "GET" and rest == ["public"]:
                self._send_json(
                    200, self._console_share_public_payload())
                return
            if method == "GET" and rest == ["public-events"]:
                status, payload = self._console_share_public_events_payload()
                self._send_json(status, payload)
                return
            if method == "GET" and rest == ["public-export"]:
                status, payload, filename = \
                    self._console_share_public_export_payload()
                if status != 200:
                    self._send_json(status, payload)
                    return
                self._send_json_attachment(status, payload, filename)
                return
            manager = get_share_manager()
            if method == "GET" and rest == ["status"]:
                payload = self._console_share_status_payload(
                    manager.status())
                # The console pill derives "已开放" from broker.running AND
                # funnel_on; the ShareManager store view never carries the
                # funnel layer, so the facade observes it through the SAME
                # read-only seam the share.status action uses. The seams are
                # wired lazily by get_console_manager() on operations access
                # only — a bare status GET must ensure the wiring itself
                # (idempotent setattr; unwired -> key absent -> the UI keeps
                # its honest 未确认 fallback). The CLOUD role passes its own
                # flag: this lazy path must never install the local
                # Tailscale/Funnel seams into a cloud process (the cloud
                # assembly deliberately leaves them unwired).
                try:
                    from console import adapters as console_adapters
                    from console import share_actions as console_share_actions
                    if console_share_actions._FUNNEL_OBSERVED is None:
                        console_adapters.wire_share_production_seams(
                            keys_manager=manager, cloud=CLOUD_ROLE)
                    funnel = console_share_actions._funnel_observed()
                    if funnel is not None:
                        payload["funnel_on"] = bool(funnel)
                    # D18-1: the configured public origin is the real
                    # deployment address; the Tailscale Funnel URL stays the
                    # fallback only when it is observed on. An invalid
                    # CONSOLE_PUBLIC_ORIGIN is an explicit error and never
                    # silently falls back to a different public host.
                    configured = console_public_url.resolve_configured()
                    if configured.source is not None \
                            or configured.reason == console_public_url.INVALID:
                        resolved = configured
                    elif funnel is True:
                        resolved = console_public_url.resolve(
                            self._tailscale_funnel_url())
                    else:
                        resolved = console_public_url.ResolvedPublicUrl(
                            None, None, console_public_url.UNCONFIGURED)
                    payload["share_url_source"] = resolved.source
                    payload["share_url_reason"] = resolved.reason
                    if resolved.url:
                        payload["share_url"] = resolved.url
                    # Gateway-instance evidence: broker.running is the ALWAYS-ON
                    # management daemon and must never read as share-open (or
                    # close-residue) evidence — observe the actual gateway
                    # instance through the same read-only seam the share.status
                    # action uses. Account validity rides the dedicated-backend
                    # observation seam; last_check_at stamps THIS read.
                    ops = console_share_actions._BROKER_OPS
                    if ops is not None:
                        # verify_running is THREE-STATE (True/False/None):
                        # None = unconfirmed (pipe busy/unreachable) — never
                        # collapse it to False, that would fabricate a
                        # confirmed-stopped fact; omit the key instead so the
                        # UI keeps its honest 未确认 fallback.
                        running = ops.verify_running()
                        if running is True or running is False:
                            payload["gateway_running"] = running
                    observe = console_share_actions._BACKEND_OBSERVE
                    if observe is not None:
                        ob = observe()
                        if isinstance(ob, dict) and \
                                ob.get("auth_state") is not None:
                            payload["account"] = {
                                "valid": ob.get("auth_state") == "valid",
                                "running": ob.get("running"),
                            }
                        # 归因启用状态（2026-09-26 owner 裁定，同日复核重塑：
                        # 一个布尔不得冒充三件事）：同一观察缝的 attribution
                        # 子字典原样透出——加性字段，既有字段的值与语义不变。
                        # file_ready 只表示本进程此刻能读到长度合格的文件；
                        # instance_loaded 是对运行中 8100 实例的推断
                        # （yes/no/unknown，查不到进程即 unknown）；request_attribution
                        # 本读面不观察，只指向 8110 账本 attribution="verified"
                        # 的运行期事实。旧的 wired 键已删除（只被本读面与
                        # console_share_actions 消费，两处已同步更新），
                        # 不留下一个含义模糊的布尔；前端渲染是另一步。
                        if isinstance(ob, dict) and \
                                isinstance(ob.get("attribution"), dict):
                            payload["attribution"] = dict(ob["attribution"])
                except Exception:
                    pass
                payload["last_check_at"] = int(time.time())
                # r14 总览契约：配置上限 / 在途 / 排队 / 上游容量分列。
                # 配置上限取 payload 的 share_max_concurrency（note 随实际值生成）；
                # 排队与上游容量没有读缝，保持 null。
                payload["capacity"] = self._console_share_capacity(payload)
                self._send_json(200, payload)
                return
            if method == "GET" and rest == ["keys"]:
                self._send_json(200, {"keys": [
                    self._console_share_public_row(r)
                    for r in manager.list_keys()["keys"]]})
                return
            if method == "GET" and len(rest) == 3 and rest[0] == "keys" \
                    and rest[2] == "secret":
                # Plan-Revision 11 B1 narrow readback: ONE key's plaintext,
                # loopback console only (this facade already sits behind the
                # console Host/Origin gate), no-store (every console JSON
                # response is no-store), never an operation record, never a
                # list-wide bulk return. Outcomes are distinct: legacy /
                # undecryptable answer an honest inline unavailable state
                # (an expected condition until manual rotation, not an
                # error toast); deleted answers 410; unknown id 404.
                key_id = rest[1]
                # Optional optimistic gate: ?expected_row_version=N lets a caller
                # that rendered a specific version refuse a plaintext belonging
                # to a newer row (R11-B1's 版本冲突 state, distinct from the
                # other three failures).
                expected_raw = (parse_qs(urlparse(self.path).query)
                                .get("expected_row_version") or [""])[0]
                status, payload = self._share_secret_response(
                    manager, key_id, expected_raw)
                self._send_json(status, payload)
                return

            body, body_code, body_error = self._read_share_json_body()
            if body_code is not None:
                # The request body may not have been consumed (oversize,
                # bad Content-Length): the connection cannot be reused, and
                # the unread bytes must be discarded before the reply goes
                # out (see _discard_unread_body).
                self.close_connection = True
                self._discard_unread_body()
                self._send_json(body_code, {"error": body_error})
                return

            def _bad(message):
                self._send_json(400, {"error": message})
                return True

            if method == "POST" and len(rest) == 3 and rest[0] == "keys" \
                    and rest[2] in ("test", "test-private"):
                if self._reject_unknown_fields(body, ("expected_row_version",)):
                    return
                expected = body.get("expected_row_version")
                if not rest[1] or len(rest[1]) > 128 or isinstance(expected, bool) \
                        or not isinstance(expected, int) or expected < 1:
                    _bad("invalid key_id or expected_row_version")
                    return
                from console.share_probe import test_key
                self._send_json(200, test_key(
                    manager, rest[1], expected, private=rest[2] == "test-private",
                    discover_url=lambda: self._share_funnel_url(refresh=True)))
                return

            if method == "POST" and rest == ["keys"]:
                if self._reject_unknown_fields(
                        body, ("name", "idempotency_key", "max_concurrency")):
                    return
                name = body.get("name")
                idem = body.get("idempotency_key")
                if not isinstance(name, str) or not 1 <= len(name) <= 64:
                    _bad("invalid name")
                    return
                if idem is not None and (not isinstance(idem, str)
                                         or not 1 <= len(idem) <= 128):
                    _bad("invalid idempotency_key")
                    return
                max_concurrency = body.get("max_concurrency")
                if max_concurrency is not None:
                    if isinstance(max_concurrency, bool) \
                            or not isinstance(max_concurrency, int) \
                            or not 1 <= max_concurrency <= 48:
                        _bad("max_concurrency must be an integer in 1..48")
                        return
                result = manager.create_key(
                    name, max_concurrency=max_concurrency,
                    idempotency_key=idem)
                key_id = result.get("key_id")
                self._send_json(201, {
                    "key": self._console_share_public_row(
                        manager.get_key(key_id)) if key_id else None,
                    "plaintext": result.get("secret")})
                return
            if method == "PATCH" and rest == ["settings"]:
                # Global share limit (D18-4: 1..64): the same value the
                # status read face reports as the configured admission cap.
                # The per-key routes below keep their separate 1..48
                # contract; the two bounds are never shared. Lowering is
                # fail-closed inside ShareManager.set_share_limits, raising
                # publishes best-effort.
                if self._reject_unknown_fields(
                        body, ("share_max_concurrency", "expected_share_max")):
                    return
                share_max = body.get("share_max_concurrency")
                if isinstance(share_max, bool) or not isinstance(share_max, int) \
                        or not 1 <= share_max <= 64:
                    _bad("share_max_concurrency must be an integer in 1..64")
                    return
                expected_max = body.get("expected_share_max")
                if expected_max is not None and (
                        isinstance(expected_max, bool)
                        or not isinstance(expected_max, int)):
                    _bad("expected_share_max must be an integer")
                    return
                self._send_json(200, manager.set_share_limits(
                    share_max, expected_share_max=expected_max))
                return
            if method == "POST" and len(rest) == 3 and rest[0] == "keys" \
                    and rest[2] == "test":
                # Explicit version-bound key test through the public Funnel.
                # The expected row version is REQUIRED: a stale page cannot
                # test a secret it never rendered, and a key changed during
                # the probe answers key_changed instead of a fake success.
                if self._reject_unknown_fields(
                        body, ("expected_row_version",)):
                    return
                expected = body.get("expected_row_version")
                if not rest[1] or len(rest[1]) > 128 \
                        or isinstance(expected, bool) \
                        or not isinstance(expected, int) or expected < 1:
                    _bad("invalid key_id or expected_row_version")
                    return
                from console.share_probe import test_key
                self._send_json(200, test_key(
                    manager, rest[1], expected,
                    discover_url=lambda: self._share_funnel_url(refresh=True)))
                return
            if method == "POST" and len(rest) == 3 and rest[0] == "keys" \
                    and rest[2] == "revoke-and-terminate":
                if self._reject_unknown_fields(
                        body, ("idempotency_key", "expected_row_version")):
                    return
                if len(rest[1]) > 128:
                    _bad("invalid key_id")
                    return
                idem = body.get("idempotency_key")
                if idem is not None and (not isinstance(idem, str)
                                         or not 1 <= len(idem) <= 128):
                    _bad("invalid idempotency_key")
                    return
                expected = body.get("expected_row_version")
                if isinstance(expected, bool) or not isinstance(expected, int) \
                        or expected < 1:
                    _bad("expected_row_version must be a positive integer")
                    return
                result = manager.revoke_and_terminate_key(
                    rest[1], expected_row_version=expected,
                    idempotency_key=idem)
                # C1/D18-3: the revoke answer distinguishes what the
                # management side released (8110 PENDING/GRANTED) from what it
                # aborted (8110 COMMITTED) and what 8100 closed upstream.
                # A missing count stays None (never a fabricated 0). The note
                # must not claim the in-flight state was confirmed when the
                # instance ACK carried no terminated count.
                terminated = result.get("terminated")
                confirmed = (isinstance(terminated, int)
                             and not isinstance(terminated, bool))
                self._send_json(200, {
                    "key": self._console_share_public_row(result),
                    "revoked": True,
                    "terminated": terminated,
                    "released": result.get("released"),
                    "aborted": result.get("aborted"),
                    "upstream_closed": result.get("upstream_closed"),
                    "note": ("已撤销并确认终止在途请求；已提交上游的消耗不可撤回"
                             if confirmed else
                             "已撤销；在途确认未完成；已提交上游的消耗不可撤回"),
                })
                return
            if method == "POST" and len(rest) == 3 and rest[0] == "keys" \
                    and rest[2] == "rotate":
                if self._reject_unknown_fields(
                        body, ("idempotency_key", "expected_row_version")):
                    return
                if len(rest[1]) > 128:
                    _bad("invalid key_id")
                    return
                idem = body.get("idempotency_key")
                if idem is not None and (not isinstance(idem, str)
                                         or not 1 <= len(idem) <= 128):
                    _bad("invalid idempotency_key")
                    return
                expected = body.get("expected_row_version")
                if isinstance(expected, bool) or not isinstance(expected, int) \
                        or expected < 1:
                    _bad("expected_row_version required")
                    return
                result = manager.rotate_key(
                    rest[1], idempotency_key=idem,
                    expected_row_version=expected)
                self._send_json(200, {
                    "key": self._console_share_public_row(
                        manager.get_key(rest[1])),
                    "plaintext": result.get("secret")})
                return
            if method == "PATCH" and len(rest) == 2 and rest[0] == "keys":
                # D18-3: `enabled` is NOT writable here either — the row
                # keeps it as a read-only fact, and only rename and the
                # per-key concurrency limit (1..48) are accepted.
                if "enabled" in body:
                    _bad("enabled_not_writable")
                    return
                if self._reject_unknown_fields(
                        body, ("name", "max_concurrency",
                               "expected_row_version")):
                    return
                if len(rest[1]) > 128:
                    _bad("invalid key_id")
                    return
                name = body.get("name")
                max_concurrency = body.get("max_concurrency")
                expected = body.get("expected_row_version")
                if name is not None and (not isinstance(name, str)
                                         or not 1 <= len(name) <= 64):
                    _bad("invalid name")
                    return
                if max_concurrency is not None:
                    if isinstance(max_concurrency, bool) \
                            or not isinstance(max_concurrency, int) \
                            or not 1 <= max_concurrency <= 48:
                        _bad("max_concurrency must be an integer in 1..48")
                        return
                if isinstance(expected, bool) or not isinstance(expected, int) \
                        or expected < 1:
                    _bad("expected_row_version required")
                    return
                self._send_json(200, {"key": self._console_share_public_row(
                    manager.update_key(
                        rest[1], name=name,
                        max_concurrency=max_concurrency,
                        expected_row_version=expected))})
                return
            if method == "DELETE" and len(rest) == 2 and rest[0] == "keys":
                if self._reject_unknown_fields(body, ("expected_row_version",)):
                    return
                if len(rest[1]) > 128:
                    _bad("invalid key_id")
                    return
                expected = body.get("expected_row_version")
                if isinstance(expected, bool) or not isinstance(expected, int) \
                        or expected < 1:
                    _bad("expected_row_version required")
                    return
                result = manager.delete_key(
                    rest[1], expected_row_version=expected)
                self._send_json(200, {"ok": True, "deleted": rest[1],
                                      "deleted_at": result.get("deleted_at")})
                return
        except (StoreError, BrokerError) as exc:
            self.close_connection = True
            self._send_json(status_code_for(exc), {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            # Details to the server console only; the response stays
            # generic (same discipline as _share_api).
            print(f"[usagesrv] console share api error: {type(exc).__name__}: {exc}",
                  flush=True)
            self.close_connection = True
            self._send_json(500, {"error": "share management error"})
            return
        self._send_json(404, {"error": "unknown path"})

    # ----------------------------------------------------- console API (P1)

    def _console_api(self, parsed, method):
        dup = self._duplicate_header_error()
        if dup is not None:
            self.close_connection = True
            self._send_json(400, {"error": dup})
            return
        # R9-R35（维护者裁定 2026-09-21）：控制台即开即用——console 面的
        # GET/POST 全部免凭据（引导码/管理会话 UI 已按维护者指令删除）。
        # 本地边界由路由层承担：do_GET/do_POST 已拒伪造 Host（DNS rebinding）
        # 与跨源 Origin（跨源 JSON POST 触发 preflight 被拦、跨源 form POST
        # 带 evil Origin 被拒），file:// 的 Origin=null 也不匹配本服务端口。
        # 程序化通道（X-Admin-Control）与 share 公网管理面（_share_auth）
        # 的鉴权不变。副作用动作=坐在维护者机器前的本机同源页面即可提交，
        # 与「维护者本人操作」语义一致。
        ok, how = True, "local"
        rest = [s for s in parsed.path.split("/") if s][2:]  # api/console/...
        if CLOUD_ROLE:
            # 云端角色只装配 UM 分享：非 UM 的读面（model_sync / zcode
            # 账本与探测）明确返回未装配，绝不探测或读取这些路径；
            # 管理面仍是 loopback + Host/Origin 门。
            blocked = rest[0] if rest[:1] and rest[0] in (
                "usage-tokens", "usage-rates", "gateway-connection",
                "logs", "log-sources", "log-entries",
                "log-overview", "stats", "um") else None
            if blocked is not None:
                self._send_json(503, {
                    "error": "capability_not_assembled",
                    "capability": blocked,
                    "role": ROLE_CLOUD,
                    "detail": "云端角色只装配 UM 分享：该读面未装配，"
                              "不探测/不读取非 UM 资源"})
                return
        if rest[:1] == ["cloud-ops"]:
            # S2 read face: budget, backup freshness and the independent
            # security floor.  Read-only; no budget/backup/restore action.
            if method != "GET" or rest != ["cloud-ops"]:
                self.close_connection = True
                self._discard_unread_body()
                self._send_json(404, {"error": "unknown path"})
                return
            self._send_json(200, cloud_ops_status())
            return
        if rest[:1] == ["share-management"]:
            # A36-2: the share-management facade rides the console trust
            # surface but talks to ShareManager — routed ahead of the
            # console-manager assembly so a console subsystem failure never
            # masks the share lane.
            self._console_share_api(rest[1:], method)
            return
        if rest[:1] == ["share-usage"]:
            # R3 审查阻塞 1：分享 Key 费用的控制台面窄只读口。与
            # GET /api/share/usage 同一份数据、同一口径（同一个
            # _share_usage_response），但走 R9-R35 的控制台信任面——本机
            # 同源页面免凭据直读，本地边界由路由层承担（do_GET 已拒伪造
            # Host 与跨源 Origin）。管理令牌绝不进网页：这里不要求也不读它，
            # share 管理面的既有鉴权不变。排在 console-manager 装配之前：
            # 账本读取不依赖运行态子系统（照 share-management /
            # usage-tokens 的先例）。
            if method != "GET" or rest not in (
                    ["share-usage"], ["share-usage", "requests"]):
                # 只读：写方法与未列入的子路径都不落到 reader，也不触发管理器
                # 装配。未读请求体先排空再应答（R7-a 单响应 +
                # _discard_unread_body：关闭仍有未读数据的连接会让真实
                # 客户端看到连接重置，而不是下面这个码）。写方法按未知路径
                # 拒绝，与 /api/share/usage 及控制台面其余只读路由
                # （usage-tokens / usage-rates / prices…）同一既有行为，
                # 不新增一套 405 语义。
                self.close_connection = True
                self._discard_unread_body()
                self._send_json(404, {"error": "unknown path"})
                return
            if rest == ["share-usage", "requests"]:
                status, payload = self._share_usage_requests_response()
            else:
                status, payload = self._share_usage_response()
            self._send_json(status, payload)
            return
        if rest[:1] == ["um"]:
            # B5 UM read surface: today/trend/requests/logs/request-logs
            # read UM's own chat2api ledger/logs; maintenance reads the
            # ORIGINAL console operation records. None of these touch the
            # share ledger, share pricing or the cloud share role.
            if method != "GET" or len(rest) != 2:
                self.close_connection = True
                self._discard_unread_body()
                self._send_json(404, {"error": "unknown path"})
                return
            query = parse_qs(parsed.query)

            def _q(key, cap=120):
                return (query.get(key) or [""])[0][:cap] or None

            def _qint(key, default):
                try:
                    return int((query.get(key) or [str(default)])[0])
                except (TypeError, ValueError):
                    raise ValueError("%s 必须是整数" % key)

            sub = rest[1]
            try:
                if sub == "today":
                    from console import stats as console_stats
                    payload = console_stats.um_today()
                elif sub == "trend":
                    from console import stats as console_stats
                    payload = console_stats.um_trend(
                        range=_q("range") or "7d")
                elif sub == "requests":
                    from console import stats as console_stats
                    payload = console_stats.um_requests(
                        window=_q("window") or "today",
                        model=_q("model"),
                        result=_q("result") or "all",
                        search=_q("search") or "",
                        cursor=_q("cursor", 512),
                        limit=_qint("limit", 50))
                elif sub == "logs":
                    from console import log_reader as console_log_reader
                    levels = [v.strip() for v in
                              (_q("levels") or "").split(",")
                              if v.strip()]
                    payload = console_log_reader.read_um_logs(
                        window=_q("window") or "today",
                        levels=levels,
                        source=_q("source") or "service",
                        cursor=_q("cursor", 512),
                        limit=_qint("limit", 80),
                        ledger_paths={"chat2api": CHAT2API_LOG})
                    if payload.get("status") == "error":
                        self._send_json(400, {
                            "error": "invalid_um_query",
                            "detail": payload.get("error")})
                        return
                elif sub == "request-logs":
                    from console import log_reader as console_log_reader
                    payload = console_log_reader.read_um_request_logs(
                        _q("request_id") or "",
                        limit=_qint("limit", 100),
                        ledger_paths={"chat2api": CHAT2API_LOG})
                elif sub == "maintenance":
                    payload = um_maintenance(limit=_qint("limit", 50))
                else:
                    self._send_json(404, {"error": "unknown path"})
                    return
            except ValueError as exc:
                self._send_json(400, {"error": "invalid_um_query",
                                      "detail": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                print("[usagesrv] UM read surface failed: %s"
                      % type(exc).__name__, flush=True)
                self._send_json(502, {"error": "um_query_failed"})
                return
            self._send_json(200, payload)
            return
        if method == "GET" and rest == ["usage-tokens"]:
            # A12-1 近 N 日（默认 14）完整 Token（输入+输出）按来源+模型
            # 分列。有界只读查询层全部在 console_stats 内收口（增量缓存、
            # 字节/行上限、缺分量单列不折零）；与 share-management 同理
            # 排在 console-manager 装配之前，账本读取不依赖运行态子系统。
            query = parse_qs(parsed.query)
            try:
                days = int((query.get("days") or ["14"])[0])
            except (TypeError, ValueError):
                self._send_json(400, {"error": "invalid_usage_query",
                                      "detail": "days 必须是整数"})
                return
            try:
                from console import stats as console_stats
                payload = console_stats.usage_tokens(
                    console_stats.default_sources(
                        environment=_stats_environment()), days=days)
            except ValueError as exc:
                self._send_json(400, {"error": "invalid_usage_query",
                                      "detail": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                print("[usagesrv] usage tokens query failed: %s"
                      % type(exc).__name__, flush=True)
                self._send_json(502, {"error": "usage_query_failed"})
                return
            self._send_json(200, payload)
            return
        if method == "GET" and rest == ["usage-rates"]:
            # A12-1 近 N 小时（默认 24）整次平均输出速率（输出 Token ÷
            # 整次耗时，含首字等待）；分母只含耗时为正且输出齐全的记录，
            # 跳过原因单列。同一查询层，同一 no-store 边界。
            query = parse_qs(parsed.query)
            try:
                hours = int((query.get("hours") or ["24"])[0])
            except (TypeError, ValueError):
                self._send_json(400, {"error": "invalid_usage_query",
                                      "detail": "hours 必须是整数"})
                return
            try:
                from console import stats as console_stats
                payload = console_stats.usage_rates(
                    console_stats.default_sources(
                        environment=_stats_environment()), hours=hours)
            except ValueError as exc:
                self._send_json(400, {"error": "invalid_usage_query",
                                      "detail": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                print("[usagesrv] usage rates query failed: %s"
                      % type(exc).__name__, flush=True)
                self._send_json(502, {"error": "usage_query_failed"})
                return
            # F5b：速率分桶只含「可计算速率」的记录，卡片上的「近 24 小时
            # 已记录调用」必须按账本记录数披露，故补一个按来源的已记录请求
            # 计数（含无法计算速率的记录；未读取=None）。计数失败只让该键
            # 缺席（前端按未知处理），不把整个已读到的速率载荷变成 502。
            payload = dict(payload)
            try:
                payload["recorded_requests"] = \
                    _usage_rates_recorded_requests(payload)
            except Exception as exc:  # noqa: BLE001
                print("[usagesrv] recorded request count failed: %s"
                      % type(exc).__name__, flush=True)
            self._send_json(200, payload)
            return
        if rest[:1] == ["gateway-connection"]:
            # Rev15（2026-10-06 维护者确认）：选中网关的连接信息窄只读口。
            # 与 share-usage 同一条纪律：直读 + _send_json，排在
            # get_console_manager() 装配之前（console 子系统故障不掩盖本读面），
            # 且绝不走操作框架——明文调用 Key 不能进
            # data/console-state.sqlite3 的 operations.result_json
            # （console/operations.py:266/:645-649）。只读：写方法与子路径
            # 按未知路径拒绝（与 /api/console/share-usage 同一既有行为，
            # 不新增 405 语义）；未读请求体先排空再应答。
            if method != "GET" or rest != ["gateway-connection"]:
                self.close_connection = True
                self._discard_unread_body()
                self._send_json(404, {"error": "unknown path"})
                return
            from console import gateway_connection as console_gateway_connection  # noqa: WPS433 惰性装配
            query = parse_qs(parsed.query)
            service = (query.get("service") or [""])[0].strip()
            if service not in console_gateway_connection.SERVICES:
                self._send_json(400, {
                    "error": "invalid_service",
                    "detail": "service 必须是 " + "、".join(
                        sorted(console_gateway_connection.SERVICES))})
                return
            try:
                snapshot, _cached, _age = console_services_snapshot()
            except Exception as exc:  # noqa: BLE001 — 快照失败按 unknown 如实上报
                print("[usagesrv] gateway connection snapshot failed: %s"
                      % type(exc).__name__, flush=True)
                snapshot = None
            try:
                payload = console_gateway_connection.connection_info(
                    service, snapshot=snapshot)
            except Exception as exc:  # noqa: BLE001 — 读面自身故障按 unknown 上报，不 500
                print("[usagesrv] gateway connection read failed: %s"
                      % type(exc).__name__, flush=True)
                self._send_json(503, {
                    "error": "connection_info_unavailable",
                    "detail": "连接信息读取失败：%s；按未知处理，"
                              "不返回猜测值" % type(exc).__name__})
                return
            self._send_json(200, payload)
            return
        try:
            manager = get_console_manager()
        except Exception as exc:
            print("[usagesrv] console manager unavailable: %s"
                  % type(exc).__name__, flush=True)
            self._send_json(503, {"error": "console subsystem unavailable"})
            return
        if method == "GET" and rest == ["services"]:
            try:
                payload, cached, cache_age = console_services_snapshot()
            except Exception:
                self._send_json(502, {"error": "runtime status unavailable"})
                return
            payload = dict(payload)
            meta = dict(payload.get("meta") or {})
            meta["cached"] = bool(cached)
            if cached:
                meta["cache_age_seconds"] = cache_age
            payload["meta"] = meta
            self._send_json(200, payload)
            return
        if method == "GET" and rest == ["prices"]:
            # R9-R29 费用页价目表：prices.json 原样透出（剥离 _说明 内部
            # 注释键），stale 判定与页面提醒共用 PRICE_STALE_DAYS 阈值；
            # 只读，读写口径与计价器完全一致（同一文件、同一过期判定）。
            try:
                with open(PRICE_FILE, encoding="utf-8") as f:
                    table = json.load(f)
            except OSError:
                self._send_json(502, {"error": "price_table_unavailable"})
                return
            except ValueError:
                self._send_json(502, {"error": "price_table_corrupt"})
                return
            if not isinstance(table, dict):
                self._send_json(502, {"error": "price_table_corrupt"})
                return
            table.pop("_说明", None)
            checked_stale = False
            checked_at = str(table.get("checkedAt") or "")
            try:
                import datetime as _dt
                age = (_dt.datetime.now() - _dt.datetime.strptime(
                    checked_at, "%Y-%m-%d")).days
                checked_stale = age > PRICE_STALE_DAYS
            except ValueError:
                checked_stale = True
            self._send_json(200, {"ok": True, **table,
                                  "checkedStale": checked_stale})
            return
        if method == "GET" and rest == ["health"]:
            # R11-6/B2: server-side loopback probes of the five services —
            # the production page probes THIS origin (native on 8001, dev
            # via the /gw/usagesrv prefix) instead of dev-only /gw/* paths.
            # Short TTL bounds repeated page polls; refresh=1 re-probes.
            query = parse_qs(parsed.query)
            force = (query.get("refresh") or [""])[0] in ("1", "true")
            payload, cached = _console_health_payload(force)
            self._send_json(200, payload)
            return
        if method == "GET" and rest == ["logs"]:
            # R9-R30 调用日志：UM 主链账本 JSONL 的尾部，只读尾块 seek
            # 读取（账本文件可达数 MB，绝不整文件读入）；覆盖范围诚实声明
            # 在响应里，缺席账本=该链无日志而非错误。B0（2026-11-01）：
            # 默认只读 chat2api；未登记来源 id 不在读取表内时不再静默跳过
            # ——该 id 进 sources_unavailable，前端据此区分「没有记录」与
            # 「该来源不在账本覆盖内」。数值字段（input/output/
            # cached_input/reasoning_tokens、duration_ms）缺失或不可解析
            # 一律 null（未知，F2 修复点），绝不折零——0 会被前端读成
            # 真实测得值。
            query = parse_qs(parsed.query)
            try:
                limit = max(1, min(200, int((query.get("limit") or ["80"])[0] or 80)))
            except ValueError:
                limit = 80
            requested = [s.strip() for s in
                         (query.get("sources") or ["chat2api"])[0].split(",")
                         if s.strip()]
            readers = _console_log_readers()
            # 未覆盖的 id（不在读取表内）：如实声明，绝不静默跳过
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
                    with open(path, "rb") as f:
                        f.seek(0, os.SEEK_END)
                        size = f.tell()
                        f.seek(max(0, size - 262144))
                        blob = f.read().decode("utf-8", errors="replace")
                    lines = blob.splitlines()
                    if size > 262144 and len(lines) > 1:
                        lines = lines[1:]  # 首行可能是截断残行，丢弃
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
            logs.sort(key=lambda e: e.get("time") or "", reverse=True)
            self._send_json(200, {"ok": True, "logs": logs[:limit],
                                  "sources": requested,
                                  "sources_unavailable": unavailable})
            return
        if method == "GET" and rest == ["actions"]:
            self._send_json(200, {"actions": manager.action_info()})
            return
        if method == "GET" and rest == ["stats"]:
            # Bounded statistics query (Revision 5 L4): same ledger
            # parsing as build_summary (console_stats reuses the usagesrv
            # functions directly). Invalid ranges answer 400; source read
            # errors stay inside coverage instead of failing the query.
            query = parse_qs(parsed.query)
            date_from = (query.get("from") or [""])[0]
            date_to = (query.get("to") or [""])[0]
            def _csv_list(key):
                raw = (query.get(key) or [""])[0]
                values = [v.strip() for v in raw.split(",") if v.strip()]
                return values or None
            sources = _csv_list("sources")
            models = _csv_list("models")
            try:
                from console import stats as console_stats
                payload = console_stats.bounded_stats(
                    console_stats.default_ledgers(
                        environment=_stats_environment()),
                    date_from, date_to,
                    sources=sources, models=models,
                    environment=_stats_environment())
            except ValueError as exc:
                self._send_json(400, {"error": "invalid_stats_query",
                                      "detail": str(exc)})
                return
            except Exception as exc:
                print("[usagesrv] stats query failed: %s"
                      % type(exc).__name__, flush=True)
                self._send_json(502, {"error": "stats_query_failed"})
                return
            self._send_json(200, payload)
            return
        if method == "GET" and rest == ["log-sources"]:
            # R9-R38 各网关运行日志详情页：来源注册表（服务端唯一权威，
            # 客户端只传 id，绝不传路径）。账本路径按既有环境变量先例注入；
            # 绝对路径不出本机——响应只带文件名，浏览器拿不到目录结构。
            from console import log_reader as console_log_reader
            sources = console_log_reader.build_registry(
                {"chat2api": CHAT2API_LOG}).get("sources") or []
            slim = []
            for src in sources:
                item = dict(src)
                cats = []
                for cat in item.get("categories") or []:
                    cat = dict(cat)
                    full = cat.pop("path", None)
                    cat["name"] = os.path.basename(full) if full else None
                    cats.append(cat)
                item["categories"] = cats
                slim.append(item)
            self._send_json(200, {"ok": True, "sources": slim})
            return
        if method == "GET" and rest == ["log-entries"]:
            # R9-R38：有限尾读/更早分页/筛选/游标全部在 console_log_reader
            # 内收口（预算、脱敏、三态分码）；此处只做查询串截断与装配。
            # cursor 是服务端铸的 opaque 串（~150 字符），不属搜索词预算，
            # 只截宽裕上限防滥用；形状仍由 reader 验证。
            from console import log_reader as console_log_reader
            query = parse_qs(parsed.query)
            def _s(key, cap=120):
                return (query.get(key) or [""])[0][:cap] or None
            rotation = None
            raw_rotation = _s("rotation")
            if raw_rotation:
                try:
                    rotation = int(raw_rotation)
                except ValueError:
                    rotation = -1  # reader answers invalid_rotation
            try:
                result = console_log_reader.read_entries(
                    _s("source") or "", _s("category") or "",
                    cursor=_s("cursor", 512), q=_s("q"), level=_s("level"),
                    since=_s("since"), until=_s("until"),
                    rotation=rotation,
                    ledger_paths={"chat2api": CHAT2API_LOG},
                    request_id=(query.get("request_id") or [None])[0])
            except Exception:
                self._send_json(502, {"error": "log_reader_failed"})
                return
            self._send_json(200, {"ok": True, **result})
            return
        if method == "GET" and rest == ["log-overview"]:
            # P3/R3：日志总览的窄只读口——一次聚合全部注册目标（不再只读
            # 每个来源的首分类），每目标独立游标（重复的 cursor 参数各自
            # 携带 src/cat，由 reader 解码归位）。汇总的覆盖/截断/未读/
            # 失败由 console_log_reader.read_overview 的 coverage 收口；
            # 逐来源详情契约仍是上面的 log-entries，不改语义。
            from console import log_reader as console_log_reader
            query = parse_qs(parsed.query)
            def _s(key, cap=120):
                return (query.get(key) or [""])[0][:cap] or None
            try:
                result = console_log_reader.read_overview(
                    cursors=[c[:512] for c in (query.get("cursor") or [])],
                    q=_s("q"), level=_s("level"),
                    since=_s("since"), until=_s("until"),
                    ledger_paths={"chat2api": CHAT2API_LOG})
            except Exception:
                self._send_json(502, {"error": "log_reader_failed"})
                return
            self._send_json(200, {"ok": True, **result})
            return
        if method == "GET" and rest == ["operations"]:
            limit = 50
            try:
                limit = int(parse_qs(parsed.query).get("limit", ["50"])[0])
            except ValueError:
                pass
            self._send_json(200, {"operations": manager.list_operations(limit)})
            return
        if method == "GET" and len(rest) == 2 and rest[0] == "operations":
            op = manager.get_operation(rest[1])
            if op is None:
                self._send_json(404, {"error": "unknown operation"})
                return
            self._send_json(200, {"operation": op})
            return
        body, body_code, body_error = self._read_share_json_body()
        if body_code is not None:
            # Discard the unread body before the reply: see
            # _discard_unread_body.
            self.close_connection = True
            self._discard_unread_body()
            self._send_json(body_code, {"error": body_error})
            return
        if method == "POST" and rest == ["operations"]:
            if self._reject_unknown_fields(
                    body, ("action", "target", "params", "idempotency_key")):
                return
            action = body.get("action")
            target = body.get("target") or ""
            params = body.get("params")
            idem = body.get("idempotency_key")
            if not isinstance(action, str) or not 1 <= len(action) <= 64:
                self._send_json(400, {"error": "invalid action"})
                return
            if not isinstance(target, str) or len(target) > 64:
                self._send_json(400, {"error": "invalid target"})
                return
            if idem is not None and (not isinstance(idem, str)
                                     or not 1 <= len(idem) <= 128):
                self._send_json(400, {"error": "invalid idempotency_key"})
                return
            try:
                op = manager.submit(action, target=target, params=params,
                                    idempotency_key=idem)
            except Exception as exc:
                code, status = _console_error_status(exc)
                payload = {"error": code}
                reason = getattr(exc, "reason", None)
                if reason:
                    payload["reason"] = str(reason)
                detail = _console_error_detail(exc)
                if detail:
                    payload["detail"] = detail
                self._send_json(status, payload)
                return
            self._send_json(200 if op.get("deduplicated") else 201,
                            {"operation": op})
            return
        if (method == "POST" and len(rest) == 3 and rest[0] == "operations"
                and rest[2] == "cancel"):
            if not rest[1] or len(rest[1]) > 64:
                self._send_json(400, {"error": "invalid operation id"})
                return
            try:
                op = manager.cancel(rest[1])
            except Exception as exc:
                code, status = _console_error_status(exc)
                self._send_json(status, {"error": code})
                return
            self._send_json(200, {"requested": True, "operation": op})
            return
        self._send_json(404, {"error": "unknown path"})

    def _request_origin_allowed(self):
        if has_proxy_metadata(self.headers):
            if not TAILNET_POLICY.allows(self.headers, self.client_address[0]):
                return False
            if self.command in ("POST", "PATCH", "DELETE"):
                return self.headers.get("X-Requested-With") == "XMLHttpRequest" \
                    or control_token_ok(self.headers.get("X-Admin-Control"))
            return True
        return host_allowed(self.headers.get("Host", "")) \
            and origin_allowed(self.headers.get("Origin"))

    def do_GET(self):
        # Local Host/Origin gates, or the explicit authenticated Serve boundary.
        if not self._request_origin_allowed():
            self.close_connection = True
            self._send_json(403, {"error": "local-only service"})
            return
        self.path = self._rewrite_usage_alias()
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/display" or path.startswith("/display/"):
            if DISPLAY_PROXY is None:
                self._send_json(503, {"error": "display_proxy_unavailable"})
            else:
                DISPLAY_PROXY.handle_get(
                    self, tailnet_policy=TAILNET_POLICY, console_port=PORT)
            return
        if path in ("/", "/index.html"):
            identity_error = _console_build_identity_error()
            if identity_error is not None:
                self._send_json(500, {"error": identity_error})
                return
            try:
                with open(INDEX_FILE, "rb") as f:
                    body = f.read()
            except OSError:
                # dist 缺失（文件/目录不存在或不可读）返回诚实 500，
                # 绝不回退旧静态页（2026-09-23 维护者裁定）。
                self._send_json(500, {
                    "error": "web/dist/index.html 缺失"
                             "（控制台前端构建产物未就绪）"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # A rebuild replaces the entry chunk; never let a browser reuse the
            # previous role's page from cache.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/health":
            self._send_json(200, {"status": "ok", "service": "usagesrv", "port": PORT})
        elif path == "/api/summary":
            # R9-R34（维护者裁定 2026-09-21）：本机日常查看不需要引导码兑换。
            # 只读直开的边界与 _console_api GET 相同：服务仅绑定 127.0.0.1，
            # 路由层 Host/Origin 门拒 DNS rebinding 与跨源页面，跨源响应因
            # 无 CORS 头不可读；账本文件本机进程本可直接读，此处不新增攻击
            # 面。写边界（POST /api/session、share、console operations）不变。
            qs = parse_qs(parsed.query)
            try:
                self._send_json(200, summary_cached())
            except Exception as e:  # 页面不能白屏；细节只进服务端控制台，不外带
                print(f"[usagesrv] summary error: {type(e).__name__}: {e}", flush=True)
                self._send_json(500, {"error": "summary unavailable"})
        elif path.startswith("/api/console/"):
            self._console_api(parsed, "GET")
        elif path.startswith("/api/share/"):
            self._share_api(parsed, "GET")
        else:
            self._serve_console_static(path)

    def do_POST(self):
        if not self._request_origin_allowed():
            self.close_connection = True
            self._discard_unread_body()
            self._send_json(403, {"error": "local-only service"})
            return
        self.path = self._rewrite_usage_alias()
        parsed = urlparse(self.path)
        if parsed.path == "/api/session":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            sid = session_exchange((body or {}).get("code"))
            if not sid:
                self._send_json(403, {"error": "invalid bootstrap code"})
                return
            payload = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Set-Cookie",
                "{0}={1}; HttpOnly; SameSite=Strict; Path=/; Max-Age={2}".format(
                    SESSION_COOKIE, sid, SESSION_TTL_SECONDS))
            self.end_headers()
            self.wfile.write(payload)
        elif parsed.path == "/api/session/reissue":
            # 控制台重新申请引导码：管理令牌鉴权（与 /api/summary 程序化
            # 通道同一令牌）。um_share 既读不到令牌文件也被 WFP 挡在门外。
            if not control_token_ok(self.headers.get("X-Admin-Control")):
                self._send_json(403, {"error": "control token required"})
                return
            code = reissue_session_code(self.headers.get("X-Admin-Control"))
            payload = json.dumps(
                {"ok": True, "code": code}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
        elif parsed.path.startswith("/api/console/"):
            self._console_api(parsed, "POST")
        elif parsed.path.startswith("/api/share/"):
            self._share_api(parsed, "POST")
        elif parsed.path == "/display" or parsed.path.startswith("/display/"):
            if DISPLAY_PROXY is None:
                self._send_json(503, {"error": "display_proxy_unavailable"})
            elif not DISPLAY_PROXY.handle_post(
                    self, tailnet_policy=TAILNET_POLICY, console_port=PORT):
                self._send_json(404, {"error": "unknown path"})
        else:
            self._send_json(404, {"error": "unknown path"})

    def do_PATCH(self):
        if not self._request_origin_allowed():
            self.close_connection = True
            self._send_json(403, {"error": "local-only service"})
            return
        self.path = self._rewrite_usage_alias()
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/console/"):
            # A36-2: PATCH reaches the facade only via the console prefix;
            # everything else keeps the /api/share surface unchanged.
            self._console_api(parsed, "PATCH")
        else:
            self._share_api(parsed, "PATCH")

    def do_DELETE(self):
        if not self._request_origin_allowed():
            self.close_connection = True
            self._send_json(403, {"error": "local-only service"})
            return
        self.path = self._rewrite_usage_alias()
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/console/"):
            self._console_api(parsed, "DELETE")
        else:
            self._share_api(parsed, "DELETE")

    def log_message(self, fmt, *args):
        pass  # 静默访问日志


def main():
    global TAILNET_POLICY
    # E19-1-04B: install the statistics environment once at startup. This is
    # the explicit process-level injection point; console_stats._FILE_CACHE is
    # the reader-side incremental cache and stays inside console_stats.
    console_stats.configure(STATS_ENV)
    TAILNET_POLICY = load_policy(os.environ.get("USAGE_TAILNET_CONFIG_FILE") or
                                 os.path.join(HERE, "data", "console-tailnet-access.json"))
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    session_bootstrap_code()  # 启动即生成引导码文件，供控制台 [7] 读取
    _start_summary_refresh_thread()  # R9-R39: 后台预聚合，首屏即时命中
    print("-" * 60)
    print(f"usagesrv listening on http://{HOST}:{PORT} (role={DEPLOYMENT_ROLE})")
    identity_error = _console_build_identity_error()
    if identity_error is None:
        print(f"  控制台页面 : web/dist（{_console_role()} 角色已核对）")
    else:
        print(f"  控制台页面 : 拒绝托管；{identity_error}")
    if TAILNET_POLICY.enabled:
        print(f"  私网管理   : {TAILNET_POLICY.origin}（仅已授权设备）")
    elif TAILNET_POLICY.error:
        print(f"  私网管理   : 已拒绝；{TAILNET_POLICY.error}")
    if CLOUD_ROLE:
        # 云端角色只装配 UM 分享：不打印、不读取本地私人账本路径。
        print("  云端角色   : 只装配 UM 分享（非 UM 模块未装配）")
        print("  管理面     : 仅 loopback；Host/Origin/CSRF 门保留")
    else:
        print(f"  chat2api   : {CHAT2API_LOG}")
        print(f"  DSH 快照   : {DSH_SNAPSHOT_FILE}")
        print(f"  价格主源   : {PRICE_FILE}")
        print("  退役账本   : 仅保留历史追溯（默认不读取/不探测）")
    print("  Ctrl+C 退出")
    print("-" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if DISPLAY_PROXY is not None:
            DISPLAY_PROXY.close_all()
        print("\nBye.")


if __name__ == "__main__":
    main()
