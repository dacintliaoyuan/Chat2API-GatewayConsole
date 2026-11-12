#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Catalog mirroring entry point for the currently enabled gateway sources.

The command is non-interactive unless ``--confirm`` is used.  It never opens a
login window.  Scheduled callers start the required local source services first
and use the structured last-result file to decide whether DSH needs a restart or
the user needs a notification.

Manual context overrides (Plan-Revision 7 step 6 — designed names, recorded
here as the module contract):

* ``state["contextOverrides"]`` maps ``"source|model_id"`` to
  ``{"context": <positive int>, "set_at": <UTC ISO stamp>, "source": "manual"}``.
* Written ONLY by the console actions ``model.context_override_set`` /
  ``model.context_override_clear`` under the existing SyncLock +
  ``apply_config_transaction``; read via ``model.context_overrides`` /
  ``context_override_surface``.
* ``apply_context_overrides`` applies the effective value at ONE point inside
  ``run_sync`` right after the catalogs are collected (``dataclasses.replace``
  on the CatalogModel), so the catalog mirror — and sync preview and apply,
  which share the identical path — all see the SAME effective value and the
  SAME scope.  The declared-context probe seeding deliberately reads the
  PRE-override catalog snapshot: a manual override must never masquerade as
  a catalog declaration nor reset verified probe evidence (a set→clear cycle
  re-tests nothing; ``declared_context`` is what the catalog says, not what
  the maintainer chose).
* Saving an override does NOT apply it to any real client: the client write
  still goes through the existing sync apply (ZCode config and rule store in
  the same transaction; a rule-library-unavailable round defers the whole
  ZCode write set, R20-2 semantics).  DSH gets no new write surface here —
  effective values reach it only through the existing mirror machinery.
* Restoring the default deletes the override entry and NEVER deletes
  measured history (the contextProbes record's ``history`` list stays).
* ``set_at`` version-conflict rule (explicit, tested): ``expected_set_at``
  names the override version the caller based its decision on (the read
  surface's ``override_at``; absent/None = "I expect NO existing override").
  The write applies only when the stored state matches that expectation
  exactly; any mismatch is rejected as ``ContextOverrideConflict`` instead
  of silently clobbering a concurrent change.

Context publication protection (R6/D4, 2026-10-10 — recorded here as the
module contract):

* Every managed registry entry (``managed[binding][model_id]``) MAY carry
  ``published``: ``{"context": <positive int|None>, "at": <UTC ISO>}`` — the
  value this sync last ACTUALLY published for that client.  It is written in
  the SAME state transaction as the client files, read back from the rendered
  candidates (写后读回) after the deferral decisions: a deferred client keeps
  its old entry, a remove deletes it.  An absent record means history is
  unknown and is NEVER conflated with a recorded ``None`` (we published the
  field's absence).
* ``model_sync_core.context_publish_action`` is the single three-way decision
  (console target / client current value / publish baseline) shared by the
  planner, the renderers and the read surface: a client value that differs from
  the target and is not provably ours is a CONFLICT — never overwritten, never
  deleted, surfaced for the user to arbitrate.  A user who explicitly decided
  the context (a saved draft carrying a context field, ``contextDecided``) lifts
  the hold; adopting the client value is an ordinary settings draft applied
  through preview, so no special write path exists.
* Conflicting models enter ``contextConflicts`` on preview/apply results and on
  the ``model.context_overrides`` read face; they are never disguised as
  applied, pending or a plain warning.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import http.client
import io
import ipaddress
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, replace

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_PACKAGE_ROOT = str(Path(__file__).resolve().parents[1])
if _PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, _PACKAGE_ROOT)

from model_sync.config_adapters import (
    ConfigPublishError,
    ZCODE_MANAGED_PROVIDER_DEFAULTS,
    apply_config_transaction,
    render_dsh_sync,
    render_zcode_sync,
    replace_with_retry,
    zcode_managed_provider_template,
    zcode_running,
)
from model_sync.reconcile import KNOWN_MODELS, VARIANT_SUFFIXES
from model_sync.sync_core import (
    CatalogModel,
    CONTEXT_CONFLICT,
    CONTEXT_UNREADABLE,
    DEFAULT_BINDINGS,
    RULE_SURFACE_KEY,
    client_context_of,
    context_publish_action,
    empty_state,
    migrate_state,
    plan_catalog_mirror,
    published_context,
    published_record,
    validate_catalog,
)
# Rev11 协作锁：与 ZCode 客户端同一文件锁协议（<file>.lock 目录 + owner json）。
from zcode.file_lock import zcode_file_lock
# Live publishing requires compatible lock, store and watcher protocols.
# Incomplete evidence fails closed; the displayed version is informational.
from zcode.hot_sync import zcode_hot_sync_admission


HERE = Path(__file__).resolve().parents[1]
DATA_DIR = HERE / "data"
STATE_FILE = DATA_DIR / "model-sync-state.json"
RESULT_FILE = DATA_DIR / "model-sync-last-result.json"
PREVIEW_FILE = DATA_DIR / "model-sync-last-preview.json"
HISTORY_FILE = DATA_DIR / "model-sync-history.jsonl"
BACKUP_DIR = DATA_DIR / "model-sync-backups"
LOCK_FILE = DATA_DIR / "model-sync.lock"
DSH_YAML = Path.home() / ".dsh" / "settings.yaml"
ZCODE_JSON = Path.home() / ".zcode" / "v2" / "config.json"


def _code_stamp() -> str:
    """Fingerprint of the running model_sync/sync.py — history entries record which
    code produced them (a stale-copy invocation regressed the catalog once)."""
    import hashlib

    return hashlib.md5(Path(__file__).read_bytes()).hexdigest()[:12]


CODE_STAMP = _code_stamp()
# ZCode's own provider rule store: custom providers resolve their model list
# from personalModelIds whitelists plus per-model rule entries here, and the
# app only rewrites the file on settings-UI edits.  config.json alone is not
# authoritative for the picker, so renames must align both files.
ZCODE_PROVIDER_CONFIG = Path.home() / ".zcode" / "v2" / "provider_config.json"

# Rev11 单文件发布：ZCode 侧权威文件是 provider_config.json（消费快照与发布
# 使用同一权威文件）。config.json 只是旧版文件，客户端仅在它缺席时做一次性导入；
# 写它必须同时满足「规则库缺席（旧版本迁移形态）」与「兼容性已在本机安装版本
# 上取证」两个条件。当前运行装配/一次性导入路径尚未取证（见 handoff Rev11：
# 「运行装配是否启用未验证」），因此保持 False：绝不因为「规则库缺席」本身
# 就把 config.json 当成替代权威写下去，也绝不在权威文件已存在时写第二个实时权威。
LEGACY_CONFIG_MIGRATION_VERIFIED = False


def _legacy_config_write_allowed(store_problem: str | None) -> bool:
    """旧版 config.json 写面是否成立（Rev11 迁移门）。

    只有规则库缺席（客户端启动时一次性导入 config.json 的旧版本迁移形态）
    **且**迁移兼容性已取证时才成立；其余不可用形态（不可读/无法解析）与
    权威文件在场时都不成立。证据未补齐前恒为 False。
    """
    return store_problem == "文件缺席" and LEGACY_CONFIG_MIGRATION_VERIFIED

UM_MODELS_URL = "http://127.0.0.1:8000/v1/models"
# Fixed connection parts for the strict-fresh UM catalog GET.  They are derived
# once from the configured endpoint constant and are never accepted from request
# input; the console never exposes a URL/host/port parameter.
_UM_MODELS_PARTS = urllib.parse.urlsplit(UM_MODELS_URL)
UM_MODELS_HOST = _UM_MODELS_PARTS.hostname or "127.0.0.1"
UM_MODELS_PORT = int(_UM_MODELS_PARTS.port or 80)
UM_MODELS_PATH = _UM_MODELS_PARTS.path or "/v1/models"

UM_CHAT_URL = "http://127.0.0.1:8000/v1/chat/completions"


def configure_runtime(data_dir=None, um_models_url=None, um_chat_url=None):
    """Redirect this module's runtime state and UM endpoints explicitly.

    E19-1-08: the local composition root owns its own state directory and its
    own UM runtime, so the historical ``<ai-gateway>/data`` paths and the
    production 8000 endpoints must be replaceable at startup.  ``None`` keeps
    the import-time defaults unchanged.  Every derived constant is refreshed
    together (state files, backup dir, lock file, host/port/path and the
    catalog allowlist) so no stale alias can reach the production endpoint.

    The call performs no file I/O and creates no directory; a full start is the
    caller's decision.  Invalid explicit values raise ``ValueError`` instead of
    silently falling back to the production defaults.
    """
    global DATA_DIR, STATE_FILE, RESULT_FILE, PREVIEW_FILE, HISTORY_FILE
    global BACKUP_DIR, LOCK_FILE
    global UM_MODELS_URL, UM_CHAT_URL, UM_MODELS_HOST, UM_MODELS_PORT
    global UM_MODELS_PATH, _CATALOG_ENDPOINTS
    global _LAST_FRESH_UM_CATALOG

    if data_dir is not None:
        if not isinstance(data_dir, (str, os.PathLike)) \
                or not str(data_dir).strip():
            raise ValueError("data_dir must be a non-empty path")
        DATA_DIR = Path(data_dir)
        STATE_FILE = DATA_DIR / "model-sync-state.json"
        RESULT_FILE = DATA_DIR / "model-sync-last-result.json"
        PREVIEW_FILE = DATA_DIR / "model-sync-last-preview.json"
        HISTORY_FILE = DATA_DIR / "model-sync-history.jsonl"
        BACKUP_DIR = DATA_DIR / "model-sync-backups"
        LOCK_FILE = DATA_DIR / "model-sync.lock"

    if um_models_url is not None:
        parts = _runtime_url_parts(um_models_url, "um_models_url")
        UM_MODELS_URL = um_models_url
        UM_MODELS_HOST = parts.hostname
        UM_MODELS_PORT = int(parts.port or 80)
        UM_MODELS_PATH = parts.path or "/v1/models"
        _CATALOG_ENDPOINTS = frozenset({UM_MODELS_URL})
        # A cached catalog from the previous endpoint must not survive: it would
        # present another instance's models as this runtime's fresh evidence.
        with _LAST_FRESH_UM_LOCK:
            _LAST_FRESH_UM_CATALOG = None

    if um_chat_url is not None:
        _runtime_url_parts(um_chat_url, "um_chat_url")
        UM_CHAT_URL = um_chat_url
        CONTEXT_PROBE_ENDPOINTS["um"] = (UM_CHAT_URL, None)


def _runtime_url_parts(value, field):
    """Validate an explicit UM endpoint and return its fixed connection parts."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a non-empty http(s) URL" % field)
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise ValueError("%s must be an http(s) URL" % field)
    if not parts.hostname:
        raise ValueError("%s must name a host" % field)
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("%s must not carry credentials, query or fragment"
                         % field)
    try:
        port = int(parts.port or 80)
    except ValueError as exc:
        raise ValueError("%s has an invalid port" % field) from exc
    if not 1 <= port <= 65535:
        raise ValueError("%s has an invalid port" % field)
    return parts

# B2 (2026-11-01): strict-fresh UM catalog refresh through the local
# chat2api gateway.  The header is chat2api's explicit raw-fresh opt-in:
# with it, an upstream failure answers 502 instead of replaying the stale
# model cache; on success chat2api adds the response marker below.  The marker
# is the freshness evidence — a 200 WITHOUT it is not a fresh answer and must
# never be reported as a successful refresh.  Both names are the gateway's wire
# contract (chat2api.py X_MODELS_HEADER / X_MODELS_MARKER); this module only
# reuses them, it does not define a second gateway contract.
UM_MODELS_FRESH_HEADER = "X-Chat2Api-Models"
UM_MODELS_FRESH_VALUE = "raw-fresh"
UM_MODELS_FRESH_MARKER = "X-Chat2Api-Raw-Models"
UM_MODELS_FRESH_MARKER_VALUE = "1"
UM_MODELS_SOURCE = "um"
# In-memory last successful strict-fresh UM catalog.  B2 allows a failed
# refresh to retain the last successful data, but the result is marked stale and
# success=False — it is NEVER presented as a fresh HTTP 200.  This is a read
# cache only: no state file, no client config, no Magpie config is written.
_LAST_FRESH_UM_CATALOG: dict[str, Any] | None = None
_LAST_FRESH_UM_LOCK = threading.Lock()

# B0（2026-11-01）：退役来源的端点、URL 与 Key 读取器全部删除。默认同步
# 装配只剩 UM；退役来源只保留名字身份，用于在边界处明确拒绝（绝不探测、
# 绝不读取它们的旧 token 文件，也绝不自动重新启用）。

PROBE_LEVELS = {
    "um": ("low", "medium", "high", "xhigh", "max"),
}
MAX_PROBE_REQUESTS = 50
MAX_PROBE_ATTEMPTS = 3

# Sources and consumers known to the sync chain; scope subsets are validated
# against these names (unknown names are rejected, never silently ignored).
KNOWN_SOURCES = ("um", "m365", "qoder_cn", "qoder_global", "cliproxy")
KNOWN_CONSUMERS = ("DSH", "ZCode")
# B0 (2026-11-01): the default sync assembly is UM-only. Every retired source
# (M365 archived, Qoder CN / Qoder Global / CLIProxy retired) is PAUSED: the
# historical binding/state definitions stay readable, but a default or explicit
# sync run never fetches, plans, probes or writes them, never reads their old
# token files and never auto-reactivates them.
PAUSED_SOURCES = frozenset({"m365", "qoder_cn", "qoder_global", "cliproxy"})
ACTIVE_SOURCES = ("um",)
ACTIVE_BINDINGS = tuple(binding for binding in DEFAULT_BINDINGS
                        if binding.source in ACTIVE_SOURCES)

# Declared-context probe queue (L-E.4): bounded increasing INPUT lengths in
# raw characters.  The ladder is deliberately conservative — every step is a
# real billed request, the whole run is gated behind the maintainer-approved
# model.context_probe_run action (registered disabled), and steps are
# characters, NOT tokens: no char→token conversion is claimed anywhere.
PROBE_INPUT_STEPS = (4000, 16000, 64000, 256000)
CONTEXT_PROBE_MAX_TOKENS = 64
# Statuses that carry no capacity conclusion (auth/quota/server trouble or
# transport failure): the record stops for this round and keeps its status.
CONTEXT_PROBE_TRANSIENT_STATUSES = frozenset({401, 403, 408, 409, 425, 429})
# Chat endpoints per source for the context probe queue: source -> (url,
# token provider).  B0 (2026-11-01): only the UM main chain is assembled.
# Retired sources have no endpoint entry and never enter the active billing
# queue; their records are skipped with the honest "来源已暂停" note.  A
# provider that resolves to no token skips its records with zero requests — an
# unauthenticated attempt is never fabricated.
CONTEXT_PROBE_ENDPOINTS = {
    "um": (UM_CHAT_URL, None),
}
# Stable identity of the probe scheme (Plan-Revision 7 step 3): the input
# ladder (PROBE_INPUT_STEPS), the anchor prompt wording + per-request marker
# mechanism (_context_probe_input / _tail_anchor) and the output cap
# (CONTEXT_PROBE_MAX_TOKENS) together form one probe scheme.  Bump this
# integer whenever any of them changes: seeded records carry the revision
# they were queued under and a mismatch marks their evidence outdated
# (stale_reason="probe_scheme_changed") so an old floor can never be reused
# as current-generation evidence.  The field is introduced AT revision 1 and
# the scheme has not changed since the queue was created, so a record
# missing the field is read as revision 1 (grandfathered), NOT as a
# mismatch; once the constant itself moves, missing-field records flip too.
PROBE_SCHEME_REVISION = 1
# 归档历史上限：单条探测记录的 ``history`` 最多保留 20 代，最旧的先挤出
# （history 只服务人工回看，不无限增长）。
PROBE_HISTORY_MAX = 20


class SyncLock:
    def __enter__(self):
        import msvcrt
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.handle = open(LOCK_FILE, "a+", encoding="utf-8")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write("0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.handle.close()
            raise RuntimeError("另一个模型同步正在运行") from exc
        return self

    def __exit__(self, *_):
        import msvcrt
        self.handle.seek(0)
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.handle.close()


# B0 (2026-11-01): only the UM main-chain catalog endpoint is assembled.
# Retired source endpoints stay as named constants for historical readers but
# are never part of the default catalog boundary.
_CATALOG_ENDPOINTS = frozenset({UM_MODELS_URL})


def _validate_catalog_endpoint(url: str) -> str:
    """Boundary for every catalog GET: fixed endpoints, http/https only.

    No caller-supplied URL reaches this function — every call passes a module
    constant.  The explicit allowlist plus the address-class checks keep a
    mistyped or monkeypatched constant from reaching metadata, private or
    reserved hosts.  The product's own loopback gateway endpoints are the only
    loopback entries and are named literally in the allowlist, never accepted
    from request input.  The strict-fresh adapter uses ``http.client`` and never
    follows redirects; the historical ``_get_json`` path keeps urllib's existing
    behavior unchanged.
    """
    if url not in _CATALOG_ENDPOINTS:
        raise ValueError("目录端点不在固定允许列表内")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("目录端点必须为 http/https")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("目录端点不接受凭据、查询或片段")
    host = parsed.hostname or ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("目录端点主机必须为 IP 字面量") from None
    if (address.is_link_local or address.is_multicast or
            address.is_reserved or address.is_unspecified):
        raise ValueError("目录端点不得为链路本地/保留/未指定地址")
    if address.is_private and not address.is_loopback:
        raise ValueError("目录端点不得为私有地址")
    if address.is_loopback and url not in _CATALOG_ENDPOINTS:
        raise ValueError("目录端点环回地址只允许固定本机网关")
    return url


def _get_um_models_response(
        timeout: int = 10) -> tuple[int, dict[str, str], Any, bytes]:
    """GET the FIXED local UM catalog endpoint with the raw-fresh opt-in.

    Host/port/path are fixed module constants (never request input); the
    endpoint boundary is validated by ``_validate_catalog_endpoint`` before the
    connection is opened.  HTTP error statuses are returned as facts (not raised)
    so the caller can distinguish an auth page, an error page and an
    unreachable endpoint.  A non-JSON body returns payload=None with the raw
    bytes preserved; transport failures still raise to the caller.
    """
    _validate_catalog_endpoint(UM_MODELS_URL)
    if not isinstance(UM_MODELS_PORT, int) or not 1 <= UM_MODELS_PORT <= 65535:
        raise ValueError("UM目录端口非法")
    connection = http.client.HTTPConnection(
        UM_MODELS_HOST, UM_MODELS_PORT, timeout=timeout)
    try:
        connection.request(
            "GET", UM_MODELS_PATH,
            headers={UM_MODELS_FRESH_HEADER: UM_MODELS_FRESH_VALUE})
        response = connection.getresponse()
        status = int(response.status)
        response_headers = {
            str(key).lower(): str(value)
            for key, value in response.getheaders()}
        raw = response.read()
    finally:
        connection.close()
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        payload = None
    return status, response_headers, payload, raw


def _get_json(url: str, token: str | None = None, timeout: int = 10) -> Any:
    request = urllib.request.Request(_validate_catalog_endpoint(url))
    if token:
        request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _post_json(url: str, body: dict[str, Any], timeout: int = 90,
               token: str | None = None) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {"status": response.status, "body": json.load(response)}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        return {"status": exc.code, "body": {"error": detail}}


# Login/auth page wording that marks a non-catalog HTML/JSON answer as an
# authentication problem rather than a legal empty catalog or a generic error page.
_AUTH_PAGE_MARKERS = (
    "login", "sign in", "signin", "password", "unauthorized", "forbidden",
    "auth required", "authentication required", "登录", "认证",
)


def _looks_like_html(raw: bytes) -> bool:
    head = raw.lstrip()[:512].lower()
    return head.startswith(b"<!doctype html") or b"<html" in head


def _looks_like_auth_page(raw: bytes) -> bool:
    text = raw.decode("utf-8", "replace").lower()[:4096]
    return any(marker in text for marker in _AUTH_PAGE_MARKERS)


def _payload_error_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("error") or payload.get("detail") or "").lower()


def _um_catalog_models(payload: Any) -> list[CatalogModel]:
    """Shape the UM /api/models payload exactly as collect_catalogs always has."""
    models: list[CatalogModel] = []
    for raw in payload.get("data") or []:
        model_id = str(raw.get("id") or "").strip()
        if not model_id or _is_virtual_model(model_id):
            continue
        efforts, default = _known_efforts(model_id)
        models.append(CatalogModel(
            "um", model_id, _context_of(raw), efforts, default,
            efforts_source="known" if efforts else None))
    return models


def _store_last_fresh_um_catalog(report: dict[str, Any]) -> None:
    global _LAST_FRESH_UM_CATALOG
    with _LAST_FRESH_UM_LOCK:
        _LAST_FRESH_UM_CATALOG = {
            "collected_at": report.get("collected_at"),
            "checked_epoch": report.get("checked_epoch"),
            "models": list(report.get("models") or []),
        }


def _with_last_success(report: dict[str, Any]) -> dict[str, Any]:
    """Attach the last successful fresh catalog as explicitly stale data.

    A failed refresh may retain the last successful data (B2), but the report
    stays success=False/fresh=False and names the failure; the data is never
    presented as a fresh HTTP 200 result.
    """
    with _LAST_FRESH_UM_LOCK:
        last = _LAST_FRESH_UM_CATALOG
    if last is None:
        report["models"] = []
        report["model_count"] = 0
        return report
    report["stale_fallback"] = True
    report["last_success"] = {
        "collected_at": last.get("collected_at"),
        "checked_epoch": last.get("checked_epoch"),
        "model_count": len(last.get("models") or []),
    }
    report["models"] = list(last.get("models") or [])
    report["model_count"] = len(report["models"])
    return report


def reset_fresh_um_catalog_cache() -> None:
    """Drop the in-memory last-success cache (tests / explicit reset)."""
    global _LAST_FRESH_UM_CATALOG
    with _LAST_FRESH_UM_LOCK:
        _LAST_FRESH_UM_CATALOG = None


def collect_fresh_um_catalog(timeout: int = 10) -> dict[str, Any]:
    """Strict-fresh UM catalog refresh (B2) — one report, never raises.

    The request goes through the local chat2api gateway (8000 /v1/models) to
    UM /api/models with ``X-Chat2Api-Models: raw-fresh``.  The gateway's
    response marker is required: a 200 without it is a stale-cache answer and
    is reported as ``not_fresh`` (success=False), never as a refresh success.
    Failure categories are distinct: transport/5xx -> unavailable, 401/403 or an
    HTML/JSON auth page -> auth_required, other non-catalog pages ->
    error_page, malformed shape -> invalid_shape, and a legal empty ``data``
    list -> empty (success=True, fresh=True).  The last successful models are
    retained in memory for an honest stale fallback; nothing is written to
    disk, no client/Magpie config and no sync apply is touched.
    """
    checked_epoch = int(time.time())
    report: dict[str, Any] = {
        "source": UM_MODELS_SOURCE,
        "requested": True,
        "success": False,
        "fresh": False,
        "status": "unavailable",
        "collected_at": None,
        "checked_epoch": checked_epoch,
        "http_status": None,
        "marker": False,
        "model_count": 0,
        "models": [],
        "error": None,
        "stale_fallback": False,
        "last_success": None,
    }
    try:
        status, response_headers, payload, raw = _get_um_models_response(
            timeout=timeout)
    except Exception as exc:
        report["error"] = "UM目录不可用：%s" % type(exc).__name__
        return _with_last_success(report)
    report["http_status"] = status
    marker = (response_headers.get(UM_MODELS_FRESH_MARKER.lower()) ==
              UM_MODELS_FRESH_MARKER_VALUE)
    report["marker"] = marker
    if status in (401, 403):
        report["status"] = "auth_required"
        report["error"] = "UM目录认证失败（HTTP %d）" % status
        return _with_last_success(report)
    if status >= 400:
        report["status"] = "unavailable"
        report["error"] = "UM目录不可用（HTTP %d）" % status
        return _with_last_success(report)
    if not marker:
        report["status"] = "not_fresh"
        report["error"] = ("UM目录响应缺少 raw-fresh 标记（可能来自旧缓存），"
                           "不宣告刷新成功")
        return _with_last_success(report)
    if payload is None:
        if _looks_like_html(raw):
            if _looks_like_auth_page(raw):
                report["status"] = "auth_required"
                report["error"] = "UM目录返回认证页（非目录数据）"
            else:
                report["status"] = "error_page"
                report["error"] = "UM目录返回错误页（非目录数据）"
        else:
            report["status"] = "invalid_shape"
            report["error"] = "UM目录响应不是合法 JSON"
        return _with_last_success(report)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        error_text = _payload_error_text(payload)
        if any(marker_text in error_text for marker_text in _AUTH_PAGE_MARKERS):
            report["status"] = "auth_required"
            report["error"] = "UM目录返回认证失败（非目录数据）"
        elif error_text:
            report["status"] = "error_page"
            report["error"] = "UM目录返回错误（非目录数据）"
        else:
            report["status"] = "invalid_shape"
            report["error"] = "UM目录响应结构非法（缺少 data 列表）"
        return _with_last_success(report)
    error_text = _payload_error_text(payload)
    if error_text:
        if any(marker_text in error_text for marker_text in _AUTH_PAGE_MARKERS):
            report["status"] = "auth_required"
            report["error"] = "UM目录返回认证失败（非目录数据）"
        else:
            report["status"] = "error_page"
            report["error"] = "UM目录返回错误（非目录数据）"
        return _with_last_success(report)
    try:
        models = _um_catalog_models(payload)
    except Exception as exc:
        report["status"] = "invalid_shape"
        report["error"] = "UM目录响应结构非法：%s" % type(exc).__name__
        return _with_last_success(report)
    report["collected_at"] = datetime.now(timezone.utc).isoformat()
    report["models"] = models
    report["model_count"] = len(models)
    if models:
        report["status"] = "fresh"
    else:
        # A legal empty catalog is a successful fresh answer, not a failure.
        report["status"] = "empty"
    report["success"] = True
    report["fresh"] = True
    _store_last_fresh_um_catalog(report)
    return report


def _context_of(raw: dict[str, Any]) -> int | None:
    value = (raw.get("max_model_len") or raw.get("context_length") or
             raw.get("contextWindow") or raw.get("context_window"))
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _known_efforts(model_id: str) -> tuple[tuple[str, ...], str | None]:
    known = KNOWN_MODELS.get(model_id) or {}
    return tuple(known.get("efforts") or ()), known.get("default")


def _effort_names(raw_efforts: Any, *, allow_max: bool = False) -> tuple[str, ...]:
    names: list[str] = []
    for item in raw_efforts or ():
        value = item.get("effort") if isinstance(item, dict) else item
        value = str(value or "").strip().lower()
        excluded = ("none", "off") if allow_max else ("none", "off", "max")
        if value and value not in excluded and value not in names:
            names.append(value)
    return tuple(names)


def collect_catalogs(
        sources: Any = None,
        fresh: bool = False,
        report: list[dict[str, Any]] | None = None
        ) -> tuple[dict[str, list[CatalogModel]], list[str]]:
    """Collect source catalogs; ``None`` = all configured sources.

    A subset list restricts the fetch to the selected sources: unselected
    sources are never requested, never enter the catalogs and therefore never
    count retirement evidence.  Unknown source names raise ValueError.

    ``fresh=True`` (B2, 2026-11-01) is the explicit strict-fresh branch:
    only the UM source is allowed and the request carries the gateway's
    raw-fresh opt-in header.  When a ``report`` list is supplied the full
    refresh report (success/freshness/status/collection time) is appended, so
    the console can distinguish a stale-cache 200 from a real refresh.  Old
    callers keep ``fresh=False`` and the historical behavior unchanged.
    """
    if fresh:
        if (not isinstance(sources, (list, tuple)) or
                list(sources) != [UM_MODELS_SOURCE]):
            raise ValueError(
                "严格取新仅支持 sources=['um']（控制台刷新只选 UM，不触其他来源）")
        result = collect_fresh_um_catalog()
        if report is not None:
            report.append(result)
        if result.get("success"):
            return {"um": list(result.get("models") or [])}, []
        error = result.get("error") or "UM目录刷新失败"
        return {}, [error]
    if sources is None:
        selected: tuple[str, ...] = ACTIVE_SOURCES
    else:
        if (not isinstance(sources, (list, tuple)) or
                any(source not in ACTIVE_SOURCES for source in sources)):
            raise ValueError(
                "sources 必须为当前启用来源 %s 的子集；"
                "退役来源（m365 / qoder_cn / qoder_global / cliproxy）不再同步"
                % (", ".join(ACTIVE_SOURCES),))
        selected = tuple(dict.fromkeys(sources))
    catalogs: dict[str, list[CatalogModel]] = {}
    warnings: list[str] = []

    if "um" in selected:
        try:
            payload = _get_json(UM_MODELS_URL)
            catalogs["um"] = _um_catalog_models(payload)
        except Exception as exc:
            warnings.append(f"UM目录不可用：{type(exc).__name__}")

    # B0: retired sources are never fetched here. A caller cannot reach this
    # point with one selected (the check above rejects it), and no token file
    # for them is read.
    return catalogs, warnings


def collect_catalogs_report(
        sources: Any = None, fresh: bool = False
        ) -> tuple[dict[str, list[CatalogModel]], list[str],
                   dict[str, Any] | None]:
    """Collect catalogs plus the strict-fresh report when requested.

    ``fresh=False`` delegates to the historical ``collect_catalogs`` path with
    the exact same call shape (old callers unchanged) and returns a None
    report.  ``fresh=True`` delegates to the explicit strict-fresh branch and
    returns its report (success/fresh/status/collection time/source), so the
    console never has to infer freshness from an HTTP 200.
    """
    if not fresh:
        catalogs, warnings = collect_catalogs(sources=sources)
        return catalogs, warnings, None
    reports: list[dict[str, Any]] = []
    catalogs, warnings = collect_catalogs(
        sources=sources, fresh=True, report=reports)
    report = reports[0] if reports else None
    if (report is not None and not report.get("success") and
            report.get("models")):
        # A failed refresh may retain the last successful data, but only the
        # report's stale_fallback flag marks it as stale; the tuple-returning
        # collect_catalogs keeps returning {} on failure.
        catalogs = {"um": list(report["models"])}
    return catalogs, warnings, report


# The upstream's real input ceiling is ~983,616 prompt units (chars, observed
# via "Range of input length should be [1, 983616]" on a 502) — NOT the 200K
# tokens the SDK catalog advertises.  JSON-escaped CJK history costs ~6.6-7.7
# units per ZCode-estimated token, so a declared context must stay well under
# the unit ceiling to be reachable; the exact ratio is payload-dependent and
# NOT re-derived here.  The declared context now mirrors the catalog source
# verbatim — the former fixed 125K synthetic cap is gone (2026-09-22) — and
# real long-input measurements are recorded independently per model in the
# contextProbes state queue (see seed_context_probes / probe_context_queue
# and the model.context_probe_preview console action).


def _registered_provider_rule(
        binding: Any, options: dict[str, str] | None) -> dict[str, Any]:
    """Build the full providerRules block for a managed provider the rule
    store does not know yet.

    Shape follows the real registered entry (handoff 2026-09-30 21:00 受控补齐的字段
    enabled + group + api + baseUrl，加上同一次补齐写入的 access{type, apiKey}；
    字段级形状由 tests/test_model_sync_provider_registration.py 钉住，真实 UI
    验收仍未做——本机从未把该登记块当作已验收的界面事实):
    enabled + group + access{type, apiKey} + api{type, baseUrl} plus the model
    whitelist/order that the alignment pass below fills.  The connection comes
    from the provider's own config.json options or the managed creation
    template — it is never invented, and a missing one is an explicit error:
    registering a provider the client cannot connect to would recreate the
    "shows up in the UI but is unusable" defect this registration fixes.
    """
    base_url = api_key = None
    if isinstance(options, dict):
        base_url = str(options.get("baseURL") or "").strip() or None
        api_key = str(options.get("apiKey") or "").strip() or None
    if not base_url or not api_key:
        raise ValueError(
            f"ZCode 受管 provider {binding.provider} 在规则库缺席，且缺少登记"
            "所需的连接信息（baseURL/apiKey）；本轮拒绝静默跳过")
    return {
        "providerId": binding.provider,
        "providerName": binding.provider_name,
        "enabled": True,
        "config": {
            "group": "standard-personal",
            "access": {"type": "api-key", "apiKey": api_key},
            "api": {"type": "openai-chat-completions", "baseUrl": base_url},
            "personalModelIds": [],
            "modelOrder": [],
        },
    }


def render_provider_config_sync(
        raw: str, catalogs: dict[str, list[CatalogModel]],
        bindings=ACTIVE_BINDINGS,
        managed: dict[str, set[str]] | None = None,
        connections: dict[str, dict[str, str]] | None = None,
        context_holds: dict[str, set[str]] | None = None) -> str:
    """Align ZCode's provider rule store (provider_config.json) with catalogs.

    The picker resolves custom-provider models from each rule's
    personalModelIds whitelist plus per-model rule entries in this file, and
    the app only rewrites it on settings-UI edits — so a catalog rename that
    touches config.json alone leaves a stale whitelist behind and the provider
    resolves to zero usable models.  The whitelist keeps its existing order: an id
    leaves it when this plan retired it (``managed`` is the plan's post-state
    managed registry for the provider, so it also covers ids the sync never
    published) or when it left the catalog;
    ids the plan did not touch — other providers, an out-of-scope run, or a model
    still under retirement evidence — stay, and new catalog ids are appended.
    (Rev8: the pinned keep-set is removed with the pin mechanism — a catalog-
    dropped managed id now always leaves the whitelist per the normal rules.)
    The per-model rule entries obey the same leave rule: an id that left the
    whitelist with this plan loses its entry too, a selected id is rebuilt in
    place, and an entry the plan did not touch stays exactly where it is — a
    scoped run never trims the entries it did not select (范围外不动).  Other
    providers and sections are untouched; callers skip the write when the parsed
    document is equal.

    R6/D4 context protection (2026-10-10): ``context_holds`` maps a binding
    key to the model ids whose ``properties.contextWindow`` must be PRESERVED —
    their client value cannot be proven to be what the sync last published, so
    it is never overwritten and never deleted.  The product path passes the
    planner's holds (plan.context_holds) so the rule store can never revert a
    client's manual context value; a caller that passes no holds at all
    (context_holds=None) gets the fail-closed default: an id whose stored entry
    already carries a contextWindow keeps it (no proof of ownership exists),
    while an id with no stored value is published normally.

    Registration (2026-09-30 Gemini gap): a managed provider whose rule block
    is missing is no longer silently skipped.  When the caller passes
    ``connections`` (provider -> {"baseURL", "apiKey"} — the provider's own
    config.json options or the managed creation template), this renderer
    registers the provider inside the SAME document the model rules align in:
    providerOrder gains the id at the END (existing entries never reorder,
    no duplicates), a full providerRules block is appended (enabled true,
    group standard-personal, access/api per the real registered shape) and
    the whitelist/order/model rules align to the catalog like any other
    provider.  A rule-missing provider WITHOUT usable connection info is an
    explicit error — never a silent skip that claims success while the client
    UI cannot see the provider.  Callers that pass no ``connections`` (the
    keepset maintenance tool) keep the conservative skip: they deliberately
    never create blocks, and the order list is left untouched too.
    """
    doc = json.loads(raw)
    cfg = doc.get("config")
    if not isinstance(cfg, dict):
        if cfg is None:
            cfg = {}
            doc["config"] = cfg
        else:
            raise ValueError("ZCode 规则库 config 结构异常，拒绝登记")
    rules_container = cfg.get("providerConfigRules")
    if not isinstance(rules_container, dict):
        if rules_container is None:
            rules_container = {}
            cfg["providerConfigRules"] = rules_container
        else:
            raise ValueError("ZCode 规则库 providerConfigRules 结构异常，拒绝登记")
    provider_rules = rules_container.get("providerRules")
    if not isinstance(provider_rules, list):
        if provider_rules is None:
            provider_rules = []
            rules_container["providerRules"] = provider_rules
        else:
            raise ValueError("ZCode 规则库 providerRules 结构异常，拒绝登记")
    # The order list participates only when registration is enabled (the
    # product path) AND a binding actually needs it; the lazy handling below
    # never invents an order for a store this run has no work for.
    rules_cfg = cfg.setdefault("modelConfigRules", {})
    model_rules = rules_cfg.get("providerModelRules")
    if not isinstance(model_rules, list):
        model_rules = []
        rules_cfg["providerModelRules"] = model_rules
    managed = managed or {}
    # The provider model rules are rebuilt ONCE for every managed provider: the
    # old per-binding pass rebuilt them from the parsed document, so a later
    # binding's pass silently reverted an earlier binding's rule additions and
    # retirements (the reported model-a rule survived that way).
    rebuilt_rules = list(model_rules)
    for binding in bindings:
        if binding.consumer != "ZCode":
            continue
        catalog = catalogs.get(binding.source)
        if not catalog:
            # Invalid, unavailable or empty catalog: this provider's rules stay
            # untouched (目录失败/为空绝不误删，也绝不据以登记).  Callers never
            # pass an empty catalog (validate_catalog rejects one), so this
            # only guards the renderer's own boundary.
            continue
        rule = next(
            (r for r in provider_rules if r.get("providerId") == binding.provider),
            None)
        if rule is None:
            if connections is None:
                # Legacy caller (the keepset tool): blocks are never created
                # here, and the caller refuses to write half a store.
                continue
            rule = _registered_provider_rule(
                binding, (connections or {}).get(binding.provider))
            provider_rules.append(rule)
            # A registration must land in providerOrder too (append-only: the
            # id joins at the END, existing entries never reorder and never
            # duplicate).  Without any order list on disk, derive it once from
            # the store's own rule order — the only layout the document itself
            # carries — instead of inventing positions for other providers.
            # A non-list order value is a malformed store: refuse, no guess.
            order = cfg.get("providerOrder")
            if not isinstance(order, list):
                if order is None:
                    order = [
                        r.get("providerId") for r in provider_rules
                        if isinstance(r, dict) and r.get("providerId")]
                    cfg["providerOrder"] = order
                else:
                    raise ValueError("ZCode 规则库 providerOrder 结构异常，拒绝登记")
            if binding.provider not in order:
                order.append(binding.provider)
        elif connections is not None:
            # Already registered: backfill a MISSING order id at the end when
            # the store carries an order list at all; an order-less store keeps
            # its client-default ordering (nothing is invented for it).
            order = cfg.get("providerOrder")
            if isinstance(order, list) and binding.provider not in order:
                order.append(binding.provider)
        rule_config = rule.setdefault("config", {})
        current_ids = list(rule_config.get("personalModelIds") or [])
        catalog_ids = [m.model_id for m in catalog if m.publish]
        sync_owned = (managed.get(binding.key) or set())
        target = [mid for mid in current_ids
                  if mid in catalog_ids or mid not in sync_owned]
        target += [mid for mid in catalog_ids if mid not in target]
        rule_config["personalModelIds"] = target
        if "modelOrder" in rule_config:
            rule_config["modelOrder"] = list(target)
        context = {m.model_id: m.context for m in catalog if m.publish}
        own = [r for r in rebuilt_rules
               if r.get("providerId") == binding.provider]
        existing = {r.get("modelId"): r for r in own}
        held_context_ids = (context_holds or {}).get(binding.key, set())

        def rebuilt_entry(model_id: str) -> dict[str, Any]:
            """One aligned rule entry: stored properties, catalog context wins.

            The entry's other ``config`` keys are preserved: this pass aligns the
            properties it owns, it does not normalize the rest of an entry away (a
            client's own flags such as ``enabled`` must survive a rebuild).
            R6/D4: a held id (its client context cannot be proven ours) keeps
            its stored ``contextWindow`` exactly as it stands — the renderer never
            overwrites it and never deletes it.
            """
            stored_config = (existing.get(model_id) or {}).get("config")
            if not isinstance(stored_config, dict):
                stored_config = {}
            old_props = stored_config.get("properties")
            old_props = dict(old_props) if isinstance(old_props, dict) else {}
            if model_id in context and model_id not in held_context_ids:
                # Mirrored model: context window follows the catalog; a None
                # catalog context removes the override.  R6/D4：调用方没有给出
                # 三方比较结论（context_holds is None）而客户端当前已有值时，
                # 绝不猜测可覆盖——原样保留；客户端没有值时照常发布。
                catalog_context = context[model_id]
                keep_client_context = (context_holds is None and
                                      "contextWindow" in old_props)
                if not keep_client_context:
                    if catalog_context is None:
                        old_props.pop("contextWindow", None)
                    else:
                        old_props["contextWindow"] = int(catalog_context)
            new_config = dict(stored_config)
            new_config["properties"] = old_props
            return {
                "modelId": model_id,
                "config": new_config,
                "providerId": binding.provider,
            }

        # The rule entries follow the whitelist's leave rule: only an id this plan
        # retired — it is in the plan's post-state managed registry for this
        # provider and no longer in the whitelist — loses its entry.  An id
        # outside the write scope (not selected by a scoped run, a rule-store-only
        # entry the plan never touched, a model still under retirement evidence)
        # keeps its entry exactly where it stands: a scoped run must never trim
        # the store it did not select (范围外不动).  The entries are spliced in
        # place, so the surviving order is stable, and catalog ids without an
        # entry are appended in whitelist order.
        provider_rules_new: list[dict[str, Any]] = []
        written: set[str] = set()
        for old_entry in own:
            model_id = old_entry.get("modelId")
            if model_id in target:
                if model_id in written:
                    # A repeated entry of an aligned id collapses into the one
                    # rebuilt entry (the whitelist holds the id once).
                    continue
                if model_id in context:
                    # This run publishes the model: align its properties (the
                    # catalog owns the context window) and keep its other keys.
                    provider_rules_new.append(rebuilt_entry(model_id))
                else:
                    # Kept in the whitelist but not published by this run — a
                    # manual extra, or an id outside a scoped selection: the
                    # entry stays exactly as it stands (范围外原样不动), not
                    # even gaining an empty properties.
                    provider_rules_new.append(old_entry)
                written.add(model_id)
            elif isinstance(model_id, str) and model_id in sync_owned:
                continue
            else:
                provider_rules_new.append(old_entry)
        for model_id in target:
            if model_id not in written and model_id in context:
                provider_rules_new.append(rebuilt_entry(model_id))
                written.add(model_id)
        # Rule entries of sync-retired ids leave with the whitelist entry; other
        # providers' entries keep their place (see _replace_provider_rules).
        rebuilt_rules = _replace_provider_rules(
            rebuilt_rules, binding.provider, provider_rules_new)
    rules_cfg["providerModelRules"] = rebuilt_rules
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


def _replace_provider_rules(
        rules: list[Any], provider: str,
        replacement: list[dict[str, Any]]) -> list[Any]:
    """Replace one provider's rule entries in place of its current block.

    The block keeps its original position (so an unrelated provider's entries and
    the document order stay put); a provider with no entries yet is appended.
    """
    result: list[Any] = []
    replaced = False
    for rule in rules:
        if isinstance(rule, dict) and rule.get("providerId") == provider:
            if not replaced:
                result.extend(replacement)
                replaced = True
            continue
        result.append(rule)
    if not replaced:
        result.extend(replacement)
    return result


def _registration_connection(
        provider: str,
        provider_options: dict[str, Any] | None) -> dict[str, str] | None:
    """Connection options for registering a rule-store-missing managed provider.

    The provider's OWN config.json options win byte-for-byte (a manual
    baseURL/apiKey is never overwritten); a registered managed creation
    template fills what is missing.  Returns None when neither side yields a
    complete baseURL/apiKey pair; the renderer then refuses the
    registration with an explicit error instead of writing a half block.
    """
    manual = (provider_options or {}).get(provider)
    manual = manual if isinstance(manual, dict) else {}
    merged = {key: str(manual.get(key) or "").strip()
              for key in ("baseURL", "apiKey")}
    if not all(merged.values()) and provider in ZCODE_MANAGED_PROVIDER_DEFAULTS:
        template_options = (
            zcode_managed_provider_template(provider).get("options") or {})
        for key in ("baseURL", "apiKey"):
            if not merged[key]:
                merged[key] = str(template_options.get(key) or "").strip()
    if not all(merged.values()):
        return None
    return merged


def _provider_config_candidate(
        catalogs: dict[str, list[CatalogModel]], plan, state: dict[str, Any],
        model_filter: dict[str, set[str]] | None = None,
        bindings=ACTIVE_BINDINGS,
        provider_options: dict[str, Any] | None = None,
    ) -> tuple[str | None, dict[Path, str], list[str]]:
    """Read the rule store ONCE and return ``(raw, candidates, registered)``.

    ``raw`` is the plan-base text this candidate was rendered against (None
    when the store is absent): the caller needs it to fingerprint the managed
    scope and to re-render inside the client lock against a fresh read
    (Rev11 协作锁/受管范围合并).  The candidate itself is computed by
    :func:`_provider_config_candidate_from_raw`; see that function for the
    alignment/registration semantics.
    """
    try:
        raw = _read_text(ZCODE_PROVIDER_CONFIG)
    except OSError:
        return None, {}, []
    candidates, registered = _provider_config_candidate_from_raw(
        raw, catalogs, plan, state, model_filter, bindings, provider_options)
    return raw, candidates, registered


def _provider_config_candidate_from_raw(
        raw: str,
        catalogs: dict[str, list[CatalogModel]], plan, state: dict[str, Any],
        model_filter: dict[str, set[str]] | None = None,
        bindings=ACTIVE_BINDINGS,
        provider_options: dict[str, Any] | None = None,
    ) -> tuple[dict[Path, str], list[str]]:
    """Rule-store alignment candidate for the planned ZCode model changes.

    Sync-owned sets come from the plan's POST-state managed registry (so a
    retirement the plan just confirmed — including an orphan the sync never
    published — leaves the whitelist) minus (a) the models currently under an
    active retirement watch: a watched model keeps its client entries until the
    second independent evidence confirms the retirement — and (b) ids outside the
    current write scope: a scoped run never widens or trims the whitelist or
    rule-store entries it did not select.  (Rev8: the pinned keep-set is gone
    with the pin mechanism.)

    Registration (2026-09-30 Gemini gap): a managed provider whose rule block
    is missing from the store is registered by the SAME candidate (the plan
    itself may have no model change at all — the provider and its models can
    already sit in config.json while the store knows nothing, the exact
    20:44→21:00 state).  Connections resolve lazily ONLY for providers whose
    rule block is actually missing, so no credential is read when nothing
    registers.  Returns ``(candidates, registered_providers)``; the
    registrations list is meaningful only when the candidate is non-empty
    (a replay whose rendered document is already equal applies nothing).

    ``raw`` is the caller's snapshot of the store.  Rev11: the write path
    re-reads the file INSIDE the client lock and calls this function again
    with the fresh text, so a concurrent client edit is merged (managed scope)
    or deferred (version/fingerprint conflict) instead of being overwritten.
    """
    try:
        store_doc = json.loads(raw)
    except ValueError:
        store_doc = None
    if not plan.changes and store_doc is None:
        # Steady state over an unparsable store: no rule-store work is possible
        # or planned; the surface reader has already reported the problem and
        # the run stays report-only (绝不猜、绝不删).
        return {}, []
    counts = plan.next_state.get("catalogCounts") or {}
    valid: dict[str, list[CatalogModel]] = {}
    for source, models in catalogs.items():
        ok, _ = validate_catalog(source, models, counts.get(source))
        if not ok:
            continue
        selected = (model_filter or {}).get(source)
        if selected is not None:
            filtered = [m for m in models if m.model_id in selected]
            # An empty selection must never wipe the whole rule store.
            if filtered:
                valid[source] = filtered
        else:
            valid[source] = models
    managed = _managed_ids(plan, model_filter)
    # Registration detection (2026-09-30 Gemini gap): a ZCode binding with a
    # valid catalog whose rule block is missing must be REGISTERED, not
    # silently skipped.  Connections resolve only for those providers — a
    # credential is never read when nothing registers.
    connections: dict[str, dict[str, str]] = {}
    if store_doc is not None:
        store_cfg = store_doc.get("config") or {}
        present_rules = {
            rule.get("providerId")
            for rule in ((store_cfg.get("providerConfigRules") or {})
                         .get("providerRules") or [])
            if isinstance(rule, dict)}
        for binding in bindings:
            if binding.consumer != "ZCode":
                continue
            catalog = valid.get(binding.source)
            if not catalog or binding.provider in present_rules:
                continue
            options = _registration_connection(
                binding.provider, provider_options)
            if options is not None:
                connections[binding.provider] = options
    rendered = render_provider_config_sync(
        raw, valid, bindings, managed, connections,
        context_holds=getattr(plan, "context_holds", None))
    if json.loads(rendered) == json.loads(raw):
        return {}, []
    return {ZCODE_PROVIDER_CONFIG: rendered}, sorted(connections)


def _managed_ids(plan, model_filter) -> dict[str, set[str]]:
    """本轮受管 id：计划后登记表 − 观察中 − 写范围外，按绑定 key。

    与 ``_provider_config_candidate_from_raw`` 的 sync-owned 集合同一口径
    （规则库渲染与锁内指纹都用它）。
    """
    watch_ids: dict[str, set[str]] = {}
    for watch_key in plan.next_state.get("retirementWatch") or {}:
        parts = str(watch_key).split("|")
        if len(parts) == 4:
            watch_ids.setdefault("|".join(parts[:3]), set()).add(parts[3])
    managed: dict[str, set[str]] = {}
    for key, entries in (plan.next_state.get("managed") or {}).items():
        if isinstance(entries, dict):
            ids = set(entries.keys())
            if model_filter:
                source = key.split("|", 1)[0]
                if source in model_filter:
                    ids &= model_filter[source]
            ids -= watch_ids.get(key, set())
            managed.setdefault(key, set()).update(ids)
    return managed


def _managed_scope_fingerprint(
        raw: str | None, plan, model_filter, bindings) -> str | None:
    """本轮受管 id 规则面的指纹（Rev11 锁内冲突检测用）。

    只取渲染器真正会改写的范围：每个受管 provider 的受管 id 在白名单里的
    在场、这些 id 的逐模型规则条目、规则块与 providerOrder 的在场，以及
    ``sync_owned`` 里这些 id 的当前白名单顺序。其他供应商、手工条目（含
    受管 provider 里不受管的 id）与顶层字段（``defaultModelSelection`` 等）
    都不参与指纹，因此并发手工编辑不会被误判成冲突、也不被覆盖；文档不可
    解析时返回 None，调用方按冲突处理（绝不覆盖）。
    """
    if raw is None:
        return None
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    cfg = doc.get("config") or {}
    if not isinstance(cfg, dict):
        return None
    managed = _managed_ids(plan, model_filter)
    order = cfg.get("providerOrder")
    provider_rules = ((cfg.get("providerConfigRules") or {})
                      .get("providerRules") or [])
    model_rules = ((cfg.get("modelConfigRules") or {})
                   .get("providerModelRules") or [])
    scope: list[dict[str, Any]] = []
    for binding in bindings:
        if binding.consumer != "ZCode":
            continue
        ids = managed.get(binding.key)
        if not ids:
            # 本轮不改写这个 provider：并发编辑不参与指纹（渲染器原样保留）。
            continue
        rule = next(
            (rule for rule in provider_rules
             if isinstance(rule, dict)
             and rule.get("providerId") == binding.provider),
            None)
        rule_config = rule.get("config") if isinstance(rule, dict) else None
        rule_config = rule_config if isinstance(rule_config, dict) else {}
        current_ids = [str(model_id)
                       for model_id in rule_config.get("personalModelIds") or []]
        scope.append({
            "providerId": binding.provider,
            "rulePresent": rule is not None,
            "managedWhitelist": [model_id for model_id in current_ids
                                 if model_id in ids],
            "managedModelRules": [
                rule for rule in model_rules
                if isinstance(rule, dict)
                and rule.get("providerId") == binding.provider
                and str(rule.get("modelId")) in ids],
            "inOrder": (binding.provider in order
                        if isinstance(order, list) else None),
        })
    if not scope:
        return None
    return hashlib.sha256(json.dumps(
        scope, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _locked_store_merge(
        catalogs: dict[str, list[CatalogModel]], plan, state: dict[str, Any],
        model_filter: dict[str, set[str]] | None, bindings,
        provider_options: dict[str, Any] | None,
        planned_raw: str | None,
    ) -> tuple[str | None, dict[Path, str], list[str]]:
    """锁内重读规则库并只合并本轮受管范围（Rev11）。

    调用方已持有客户端兼容锁；本函数：

    * 重读 ``provider_config.json``，受管范围指纹与计划基线（``planned_raw``）
      一致时用新文本重新渲染候选——其他供应商、手工条目、顺序与
      ``defaultModelSelection`` 由渲染器原样保留，只有本轮受管的 provider
      与其模型 id 面被对齐；
    * 指纹变化、文件不可读或无法解析时返回明确的延期原因，绝不覆盖并发
      编辑（``(原因, {}, [])``，调用方把本轮 ZCode 写集合整体退回 pending）。

    返回 ``(conflict, candidates, registered)``；``conflict`` 为 None 表示已按
    锁内新文本完成合并。
    """
    try:
        fresh_raw = _read_text(ZCODE_PROVIDER_CONFIG)
    except OSError as exc:
        return ("ZCode 供应商规则库在写入窗口内不可读（%s）；本轮不覆盖"
                % type(exc).__name__), {}, []
    except UnicodeError:
        return ("ZCode 供应商规则库在写入窗口内不可读（UnicodeDecodeError）；"
                "本轮不覆盖"), {}, []
    if (_managed_scope_fingerprint(fresh_raw, plan, model_filter, bindings) !=
            _managed_scope_fingerprint(planned_raw, plan, model_filter,
                                       bindings)):
        return ("ZCode 供应商规则库的受管范围在计划后已被并发修改"
                "（版本/指纹冲突）；本轮不覆盖，待下一轮重跑"), {}, []
    candidates, registered = _provider_config_candidate_from_raw(
        fresh_raw, catalogs, plan, state, model_filter, bindings,
        provider_options)
    return None, candidates, registered


def _zcode_rule_surface() -> tuple[dict[str, list[str]], str | None]:
    """ZCode 规则库模型面（provider -> 有序 model id 列表），只读。

    读取 ``provider_config.json`` 的 personalModelIds / modelOrder 与各 provider
    的模型规则条目：picker 解析自定义 provider 的模型就靠这三处，因此它们与
    主配置同属该 provider 的写入面。文件缺席时返回空面且不报警（尚未配置该
    规则库，属正常状态）；文件存在但读不出来或无法解析时返回 (空面, 说明)，
    本轮不清理规则库、只报，绝不猜。本函数绝不写入、也绝不“修复”该文档。
    """
    try:
        raw = _read_text(ZCODE_PROVIDER_CONFIG)
    except OSError as exc:
        if ZCODE_PROVIDER_CONFIG.exists():
            return {}, (f"ZCode 供应商规则库不可读（{type(exc).__name__}），"
                        "本轮不清理规则库")
        return {}, None
    except UnicodeError:
        # 在场但不是 UTF-8：与不可读同判（同一缺陷类），绝不猜、绝不删。
        return {}, "ZCode 供应商规则库不可读（UnicodeDecodeError），本轮不清理规则库"
    try:
        doc = json.loads(raw)
    except ValueError:
        return {}, "ZCode 供应商规则库无法解析，本轮不清理规则库"
    if not isinstance(doc, dict):
        # Valid JSON that is not an object: no rule store to read, and the
        # renderer would raise on ``doc.get``.  Report it instead of crashing.
        return {}, "ZCode 供应商规则库顶层不是对象，本轮不清理规则库"
    cfg = doc.get("config") or {}
    surface: dict[str, list[str]] = {}

    def add(provider: str, model_id: Any) -> None:
        value = str(model_id or "").strip()
        if not provider or not value:
            return
        ids = surface.setdefault(provider, [])
        if value not in ids:
            ids.append(value)

    for rule in ((cfg.get("providerConfigRules") or {}).get("providerRules")
                 or []):
        if not isinstance(rule, dict):
            continue
        provider = str(rule.get("providerId") or "")
        config = rule.get("config") or {}
        if not isinstance(config, dict):
            continue
        for key in ("personalModelIds", "modelOrder"):
            for model_id in config.get(key) or []:
                add(provider, model_id)
    for rule in ((cfg.get("modelConfigRules") or {}).get("providerModelRules")
                 or []):
        if isinstance(rule, dict):
            add(str(rule.get("providerId") or ""), rule.get("modelId"))
    return surface, None


def _zcode_store_snapshot() -> tuple[
        dict[str, dict[str, Any]] | None, str | None]:
    """新版权威文件 provider_config.json 的 ZCode 消费面（只读）。

    返回 ``(surface, problem)``：

    * ``surface``：``provider -> {"providerName": str|None,
      "models": {model_id: profile}}``。模型 id 取规则块的 personalModelIds
      （并回退 modelOrder）与逐模型规则条目；profile 取规则条目
      ``config.properties``（contextWindow 等），逐模型规则与规则块同在一个
      条目时按 id 合并。
    * ``problem``：文件在场但不可读／无法解析／顶层非对象时的原因短语；
      文件缺席时为 None——缺席是正常状态（客户端此时一次性导入旧配置），
      调用方走旧导入路径，绝不折叠成空面。

    本函数绝不写入、也绝不“修复”该文档；损坏时如实报告，绝不猜、绝不删。
    """
    try:
        raw = _read_text(ZCODE_PROVIDER_CONFIG)
    except OSError as exc:
        if ZCODE_PROVIDER_CONFIG.exists():
            return None, "不可读（%s）" % type(exc).__name__
        return None, None
    except UnicodeError:
        return None, "不可读（UnicodeDecodeError）"
    try:
        doc = json.loads(raw)
    except ValueError:
        return None, "无法解析"
    if not isinstance(doc, dict):
        return None, "无法解析（顶层不是对象）"
    cfg = doc.get("config") or {}
    if not isinstance(cfg, dict):
        return None, "无法解析（config 结构异常）"
    surface: dict[str, dict[str, Any]] = {}
    for rule in ((cfg.get("providerConfigRules") or {}).get("providerRules")
                 or []):
        if not isinstance(rule, dict):
            continue
        provider = str(rule.get("providerId") or "").strip()
        if not provider:
            continue
        rule_config = rule.get("config") or {}
        rule_config = rule_config if isinstance(rule_config, dict) else {}
        ids: list[str] = []
        for key in ("personalModelIds", "modelOrder"):
            for model_id in rule_config.get(key) or []:
                value = str(model_id or "").strip()
                if value and value not in ids:
                    ids.append(value)
        entry = surface.setdefault(provider, {
            "providerName": str(rule.get("providerName") or "").strip() or None,
            "models": {}})
        for model_id in ids:
            entry["models"].setdefault(model_id, {})
    for rule in ((cfg.get("modelConfigRules") or {}).get("providerModelRules")
                 or []):
        if not isinstance(rule, dict):
            continue
        provider = str(rule.get("providerId") or "").strip()
        model_id = str(rule.get("modelId") or "").strip()
        if not provider or not model_id:
            continue
        rule_config = rule.get("config") or {}
        properties = (rule_config.get("properties")
                      if isinstance(rule_config, dict) else None)
        profile: dict[str, Any] = {}
        if isinstance(properties, dict):
            for key, value in properties.items():
                profile[str(key)] = value
        entry = surface.setdefault(provider, {"providerName": None,
                                            "models": {}})
        stored = entry["models"].get(model_id)
        if isinstance(stored, dict):
            merged = dict(stored)
            merged.update(profile)
            entry["models"][model_id] = merged
        else:
            entry["models"][model_id] = profile
    return surface, None


def _rule_store_problem() -> str | None:
    """规则库能否承载本轮 ZCode 写入；不能时返回原因短语，能则 None。

    规则库文档绝不从零合成：文件缺席或无法解析时，本轮**没有**规则库写面，
    单独写主配置 config.json 就是写半份配置（模型已发布、picker 看不到），
    正是要修的缺陷形态。三种不可用形态分开报，不折叠成同一种：
    「文件缺席」「不可读（错误类型）」「无法解析」。
    """
    try:
        raw = _read_text(ZCODE_PROVIDER_CONFIG)
    except OSError as exc:
        if ZCODE_PROVIDER_CONFIG.exists():
            return "不可读（%s）" % type(exc).__name__
        return "文件缺席"
    except UnicodeError:
        # A file that is present but not UTF-8 is just as unusable as an
        # unreadable one; it must not escape as a crash of the whole round.
        return "不可读（UnicodeDecodeError）"
    try:
        doc = json.loads(raw)
    except ValueError:
        return "无法解析"
    if not isinstance(doc, dict):
        # Valid JSON that is not an object (null/array/string) carries no rule
        # store at all: the renderer would raise on ``doc.get`` and abort the
        # whole round, including the unrelated DSH side.
        return "无法解析（顶层不是对象）"
    return None


def _attach_rule_surface(
        consumers: dict[str, dict[str, Any]], bindings, surface: dict[str, list[str]],
        ) -> None:
    """把规则库模型面并入 ZCode 消费方快照（供核心计划使用）。

    受管 provider 的写入面是它完整的模型面：主配置的 models 映射加上供应商
    规则库。只挂在本次参与计划的 ZCode 绑定上——未选中的客户端、DSH 与其他
    provider 都不会因此多出写入面。
    """
    if not surface:
        return
    for binding in bindings:
        if binding.consumer != "ZCode":
            continue
        entry = consumers.get(binding.key)
        ids = surface.get(binding.provider)
        if isinstance(entry, dict) and ids:
            entry[RULE_SURFACE_KEY] = list(ids)


def _pending_retirement(plan, bindings) -> list[dict[str, Any]]:
    """本次计划里「目录缺失待退役」的条目（观察项，尚未改配置）。

    只列本次参与计划的绑定：未选中的来源其观察状态保持冻结，不属于本轮
    写入面，也不在这里冒充已处理。受管条目的两次独立目录证据规则仍然有效：
    第一次缺失只登记观察，第二次（≥30 分钟）才产生退役变化。
    """
    pending: list[dict[str, Any]] = []
    by_key = {binding.key: binding for binding in bindings}
    watch = plan.next_state.get("retirementWatch") or {}
    for watch_key in sorted(watch):
        parts = str(watch_key).split("|")
        if len(parts) != 4:
            continue
        binding = by_key.get("|".join(parts[:3]))
        if binding is None:
            continue
        entry = watch[watch_key] if isinstance(watch[watch_key], dict) else {}
        pending.append({
            "consumer": binding.consumer,
            "provider": binding.provider,
            "model_id": parts[3],
            # watching：只登记观察、本轮不改配置；pendingApply：退役已确认、
            # 客户端写入被推迟（见 run_sync 的 deferred_zcode）。
            "state": "watching",
            "evidenceRuns": int(entry.get("evidenceRuns") or 1),
            "firstMissing": entry.get("firstMissing"),
            "description": (
                f"{binding.consumer} 目录已不含 {parts[3]}，"
                "待第二次独立目录证据确认退役（本轮未改配置）"),
        })
    return pending


def _read_text(path: Path) -> str:
    with io.open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def _readback_problems(
        candidates: dict[Path, str]) -> list[tuple[Path, str]]:
    """写后读回：重新读取事务写入的每个文件并与候选内容比对。

    事务成功返回不等于磁盘内容可证明；这里整批读回核对，返回
    ``(文件, 问题短语)`` 列表。只报告文件名与差异类型，绝不回显文件内容——
    ZCode 候选可能含本机网关密钥。
    """
    problems: list[tuple[Path, str]] = []
    for path, content in candidates.items():
        try:
            with io.open(path, encoding="utf-8", newline="") as handle:
                actual = handle.read()
        except OSError as exc:
            problems.append(
                (path, f"{path.name} 写后读回失败（{type(exc).__name__}）"))
            continue
        if actual != content:
            problems.append((path, f"{path.name} 写后读回内容与候选不一致"))
    return problems


def _verify_written_candidates(candidates: dict[Path, str]) -> list[str]:
    """写后读回（兼容名）：返回问题短语列表，供既有调用方与测试沿用。"""
    return [problem for _, problem in _readback_problems(candidates)]


# ---------------------------------------------------------------------------
# R6/D4 发布基准（2026-10-10）：记录「上次实际发布值」的写后读回
# ---------------------------------------------------------------------------

def _dsh_published_context(
        raw: str, provider: str, model_id: str) -> tuple[bool, int | None]:
    """DSH settings.yaml 候选里该模型条目的 contextWindow；``(找到, 值)``。

    目录无声明值时渲染器写兼容默认 128000——基线读回的是真正写入的
    128000，绝不是假定的 None。条目缺席时返回 ``(False, None)``：不登记一个
    没有发生的发布。
    """
    import yaml
    doc = yaml.safe_load(raw) or {}
    providers = ((doc.get("llm-pi-ai") or {}).get("providers") or {})
    provider_doc = providers.get(provider)
    if not isinstance(provider_doc, dict):
        return False, None
    for item in provider_doc.get("models") or []:
        if not isinstance(item, dict) or item.get("id") != model_id:
            continue
        context = item.get("contextWindow")
        if isinstance(context, bool) or not isinstance(context, int) or \
                context <= 0:
            return True, None
        return True, context
    return False, None


def _zcode_store_published_context(
        raw: str, provider: str, model_id: str) -> tuple[bool, int | None]:
    """ZCode 规则库候选里该模型规则条目的 properties.contextWindow。"""
    doc = json.loads(raw)
    rules = (((doc.get("config") or {}).get("modelConfigRules") or {})
             .get("providerModelRules") or [])
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("providerId") != provider \
                or rule.get("modelId") != model_id:
            continue
        config = rule.get("config")
        properties = (config.get("properties")
                      if isinstance(config, dict) else None)
        context = (properties.get("contextWindow")
                   if isinstance(properties, dict) else None)
        if isinstance(context, bool) or not isinstance(context, int) or \
                context <= 0:
            return True, None
        return True, context
    return False, None


def _zcode_config_published_context(
        raw: str, provider: str, model_id: str) -> tuple[bool, int | None]:
    """旧版 config.json 候选里该模型 limit.context 的写后读回。"""
    doc = json.loads(raw)
    entry = (((doc.get("provider") or {}).get(provider) or {})
             .get("models") or {}).get(model_id)
    if not isinstance(entry, dict):
        return False, None
    limits = entry.get("limit")
    context = limits.get("context") if isinstance(limits, dict) else None
    if isinstance(context, bool) or not isinstance(context, int) or \
            context <= 0:
        return True, None
    return True, context


def _published_contexts(
        candidates: dict[Path, str],
        changes: list[Any]) -> dict[str, dict[str, int | None]]:
    """本轮实际写入的候选里，每个 (binding, model) 真正发布的上下文值。

    R6/D4：基准记录「我们到底发布了什么」，所以按真正写入的候选文本读回
    （写后读回），绝不用假定的目标值代替——DSH/旧版 ZCode 在目录无声明
    值时写入兼容默认 128000，读回的基线就是 128000。候选缺席（本轮没有
    写这个模型）的项被跳过：绝不登记一个没有发生的发布。返回
    ``{binding.key: {model_id: context|None}}``。
    """
    published: dict[str, dict[str, int | None]] = {}
    for change in changes:
        if change.model_id is None or change.kind not in ("add", "update"):
            continue
        found, context = False, None
        if change.binding.consumer == "DSH":
            raw = candidates.get(DSH_YAML)
            if raw is not None:
                found, context = _dsh_published_context(
                    raw, change.binding.provider, change.model_id)
        else:
            raw = candidates.get(ZCODE_PROVIDER_CONFIG)
            if raw is not None:
                found, context = _zcode_store_published_context(
                    raw, change.binding.provider, change.model_id)
            else:
                raw = candidates.get(ZCODE_JSON)
                if raw is not None:
                    found, context = _zcode_config_published_context(
                        raw, change.binding.provider, change.model_id)
        if found:
            published.setdefault(change.binding.key, {})[
                change.model_id] = context
    return published


def _application_boundary(published: list[Path], *,
                          applied: bool) -> dict[str, Any]:
    """生效边界（Rev11）：文件已写 ≠ 客户端已加载 ≠ 会话已采用。

    ``filesWritten`` 只列本批替换成功的客户端文件（本地同步状态文件单列
    ``stateWritten``）；加载与会话采用没有受控证据（无 RPC/日志取证），一律
    按 unverified 如实报告，绝不宣称热更新成功。预览轮如实为空。
    """
    if not applied:
        return {
            "filesWritten": [],
            "stateWritten": False,
            "clientLoaded": "unverified",
            "sessionAdopted": "unverified",
            "note": ("本轮为预览：未写任何客户端文件；加载与会话采用同样"
                     "无受控证据，按 unverified 报告"),
        }
    return {
        "filesWritten": [path.name for path in published
                         if path != STATE_FILE],
        "stateWritten": STATE_FILE in published,
        "clientLoaded": "unverified",
        "sessionAdopted": "unverified",
        "note": ("文件已完整替换并读回一致（已写）；客户端是否已加载、当前"
                 "会话与下一条消息是否采用均无受控证据，按 unverified 报告，"
                 "不宣称热更新成功"),
    }


# R5 读面归一化（2026-10-10）：草稿/已应用的规范四键。
_SETTING_VALUE_FIELDS = ("alias", "contextSource", "contextTokens",
                        "defaultEffort")


def _normalized_setting_values(values: Any) -> dict[str, Any] | None:
    """One settings record's values normalized to the canonical four keys.

    ``None`` (no record at all) stays None.  A dict always yields all four
    keys with missing entries as None — the frontend's applied fallback never
    sees an ``undefined`` field.  A malformed value (non-dict) reads as None
    rather than leaking garbage.  A context combination that is not legal
    (declared/None with tokens) has its tokens dropped, never passed through.
    """
    if not isinstance(values, dict):
        return None
    normalized = {field: values.get(field)
                  for field in _SETTING_VALUE_FIELDS}
    if normalized["contextSource"] in (None, "declared"):
        normalized["contextTokens"] = None
    return normalized


def _setting_version(value: Any) -> int:
    """草稿版本令牌归一：非负整数才透出，无记录/垃圾值读作 0。

    0 是合法读面值（「该模型没有草稿记录」），保存时按 expected=0 校验，
    绝不因缺版本省略并发检查。
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _setting_version_or_none(value: Any) -> int | None:
    """已应用版本（读面显示用）：非负整数才透出，垃圾值/缺席读作未知。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _context_conflict_line(conflict: dict[str, Any]) -> str:
    """R6/D4 冲突的单行中文说明（读面/预览/CLI 共用同一措辞）。"""
    client = conflict.get("client")
    target = conflict.get("target")
    baseline = conflict.get("baseline")
    baseline_note = ("无基准记录" if not conflict.get("baseline_recorded")
                     else "基准 %s" % (baseline if baseline is not None
                                       else "缺席"))
    resolution = ("用户已显式决定控制台值" if conflict.get("decided")
                  else "待用户裁定：不覆盖")
    if conflict.get("action") == "unreadable":
        return ("客户端上下文读取不可用（%s|%s）：本轮不猜测、不覆盖（%s）"
                % (conflict.get("source"), conflict.get("model_id"),
                   resolution))
    return ("客户端上下文冲突（%s %s|%s）：客户端 %s / 控制台目标 %s / %s；"
            "%s" % (conflict.get("consumer"), conflict.get("source"),
                    conflict.get("model_id"),
                    client if client is not None else "缺席",
                    target if target is not None else "无声明",
                    baseline_note, resolution))


def _publish_recovery(exc: ConfigPublishError) -> dict[str, Any]:
    """发布失败后的结构化对账面与恢复路径（Rev11）。

    客户端文件与本地同步状态不是真正的跨文件事务：如实区分已发布、已按
    版本校验回退、保留现状（并发写者已改写或回退失败）与状态是否落盘；
    只要还有保留现状的文件就按「待核对」报告，绝不伪报成功。
    """
    published = [path.name for path in exc.published]
    rolled_back = [path.name for path in exc.rolled_back]
    left_in_place = [path.name for path in exc.left_in_place]
    state_written = STATE_FILE in exc.published
    reconcile_required = bool(left_in_place)
    if reconcile_required:
        recovery = ("保留现状的文件可能已与本地同步状态不一致：请人工核对后重跑 "
                    "python model_sync/sync.py --apply，按当前客户端文件与目录重新规划")
    elif state_written:
        recovery = ("发布已完整落盘（失败发生在收尾阶段）：重跑 python "
                    "model_sync/sync.py --apply 为幂等对账")
    else:
        recovery = ("本轮已回到替换前状态（或失败发生在任何替换前）：重跑 python "
                    "model_sync/sync.py --apply 会按当前客户端文件与目录重新规划同一变更")
    return {
        "published": published,
        "rolledBack": rolled_back,
        "leftInPlace": left_in_place,
        "stateWritten": state_written,
        "reconcileRequired": reconcile_required,
        "clientLoaded": "unverified",
        "sessionAdopted": "unverified",
        "recovery": recovery,
        "note": ("客户端文件与本地同步状态不是跨文件事务：逐文件替换 + "
                 "版本校验回退；保留现状的文件需人工核对"),
    }


def collect_consumers(
        selected_consumers: Any = None,
    ) -> tuple[dict[str, dict[str, Any]], str | None, str | None]:
    """Read the consumer snapshots for the clients participating in this run.

    ``selected_consumers`` (a consumer-name collection, or None for the
    unscoped default run) decides which client files are opened at all: a
    ZCode-only selection never opens the DSH settings file, so the retired
    ``~/.dsh/settings.yaml`` (no longer on disk) cannot abort a ZCode-only
    sync (2026-09-30 gap).  No consumer file is ever created here.

    The DSH settings are read only when DSH participates.  A MISSING or
    unreadable settings file is reported as ``dsh_raw=None`` with NO DSH
    consumer entries — never folded into an empty snapshot, which the mirror
    would misread as "the client holds zero models" and answer with mass
    retirements.  run_sync turns that into an explicit error when DSH was
    explicitly selected, and into a skip-with-warning in the unscoped default
    run.  A file that exists but does not parse (or parses to a non-mapping
    document, or lacks the managed provider) still fails the run exactly as
    before — a broken file is evidence, never a guessed empty state.

    Returns ``(consumers, dsh_raw, zcode_raw)``; ``dsh_raw``/``zcode_raw``
    are None for the client files this run did not read.

    Rev11（新版权威文件）：ZCode 侧模型面改由 ``provider_config.json``
    给出——picker 就靠它的规则块（personalModelIds / modelOrder / 逐模型
    规则）解析自定义 provider 的模型面。文件在场且可解析时，provider 显示名
    与模型 id 面以它为准；规则库不携带的 profile 细节（name / reasoning 等）
    按 id 从旧 config.json 条目补齐（客户端在旧文件缺席时才一次性导入它，
    这里只补规则库没有的字段，绝不据此制造 add/退役决定）。规则库缺席或
    不可用时退回旧 config.json 快照（客户端自身的旧导入路径），损坏时如实
    报告、绝不折叠成空面。
    """
    consumers: dict[str, dict[str, Any]] = {}
    dsh_selected = selected_consumers is None or "DSH" in selected_consumers
    zcode_selected = (
        selected_consumers is None or "ZCode" in selected_consumers)
    dsh_raw: str | None = None
    dsh_doc: Any = None
    if dsh_selected:
        import yaml
        try:
            dsh_raw = _read_text(DSH_YAML)
        except OSError:
            # Missing or unreadable file: report absence via dsh_raw=None and
            # let the caller decide (explicit selection → explicit error; the
            # default run → skip with a warning).  Never an empty snapshot.
            dsh_raw = None
            dsh_doc = None
        else:
            # A present file must parse; a broken one raises (honest failure,
            # same as before this change).
            dsh_doc = yaml.safe_load(dsh_raw)
    zcode_raw: str | None = None
    zcode: dict[str, Any] = {}
    store_surface: dict[str, dict[str, Any]] | None = None
    if zcode_selected:
        zcode_raw = _read_text(ZCODE_JSON)
        zcode = json.loads(zcode_raw)
        # Rev11：新版权威文件在场且可解析时，ZCode 消费面的 provider
        # 显示名与模型 id 面以它为准；缺席/损坏时退回旧 config.json 快照，
        # 并保留既有诚实失败语义（旧文件缺席/损坏照旧抛出）。
        store_surface, _store_problem = _zcode_store_snapshot()
    for binding in ACTIVE_BINDINGS:
        if binding.consumer == "DSH":
            if dsh_raw is None:
                continue
            provider = dsh_doc["llm-pi-ai"]["providers"][binding.provider]
            models = {item["id"]: item for item in provider.get("models") or []}
            name = provider.get("displayName")
        else:
            if not zcode_selected:
                continue
            provider = (zcode.get("provider") or {}).get(binding.provider) or {}
            models = provider.get("models") or {}
            name = provider.get("name")
            if (store_surface is not None and
                    binding.provider in store_surface):
                store_provider = store_surface[binding.provider]
                merged: dict[str, Any] = {}
                for model_id, store_profile in store_provider["models"].items():
                    # Rev11 单文件发布：新版权威文件（provider_config.json）
                    # 不携带逐模型显示名与档位；同步无法经规则库发布它们，
                    # 因此不把旧 config.json 的 name/reasoning 当成待发布差异
                    # （否则计划每轮产生一条永远无法落盘的「更新」）。上下文
                    # 之外的 profile 细节仍按 id 从旧条目补齐（只补字段，不
                    # 决定在场/缺席）；旧文件迁移路径（规则库缺席）照旧补齐。
                    legacy = models.get(model_id)
                    entry = dict(legacy) if isinstance(legacy, dict) else {}
                    entry.pop("name", None)
                    entry.pop("reasoning", None)
                    entry.pop("reasoningEfforts", None)
                    if isinstance(store_profile, dict):
                        entry.update(store_profile)
                    merged[model_id] = entry
                for model_id, legacy in models.items():
                    # 旧条目在场而规则库缺席：保留在场，避免把它误判成
                    # add 并用空 profile 重写（字段回退）；规则库写入面仍
                    # 由渲染器按目录对齐。
                    merged.setdefault(model_id, legacy)
                models = merged
                if store_provider.get("providerName"):
                    name = store_provider["providerName"]
        consumers[binding.key] = {"providerName": name, "models": models}
    return consumers, dsh_raw, zcode_raw


def _new_model_candidates(plan, state: dict[str, Any]) -> dict[str, set[str]]:
    initialized_sources = {
        key.split("|", 1)[0]
        for key in state.get("initializedBindings") or []
    }
    known_source_models = {
        source: {
            model_id
            for binding_key, entries in (state.get("managed") or {}).items()
            if binding_key.startswith(source + "|")
            for model_id in entries
        }
        for source in PROBE_LEVELS
    }
    candidates: dict[str, set[str]] = {source: set() for source in PROBE_LEVELS}
    for change in plan.changes:
        if (change.kind == "add" and change.model is not None and
                change.binding.source in candidates and
                change.binding.source in initialized_sources and
                change.model.model_id not in known_source_models.get(
                    change.binding.source, set())):
            candidates[change.binding.source].add(change.model.model_id)
    return candidates


def load_state() -> dict[str, Any]:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        # Rev8 B8-2: versioned, idempotent upgrade (v2 pinned list dropped,
        # version raised to 3); runs on the in-memory doc, the next
        # transactional write persists it, a failed write keeps the original.
        return migrate_state(state)
    except (OSError, ValueError):
        return empty_state()


def _is_virtual_model(model_id: str) -> bool:
    return model_id.endswith(VARIANT_SUFFIXES)


def _response_has_reasoning(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    usage = payload.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    if int(details.get("reasoning_tokens") or 0) > 0:
        return True
    for choice in payload.get("choices") or []:
        message = choice.get("message") or {}
        if message.get("reasoning_content") or message.get("reasoning"):
            return True
    return False


def probe_new_models(
        catalogs: dict[str, list[CatalogModel]], state: dict[str, Any],
        candidate_ids: dict[str, set[str]]) -> list[str]:
    """Probe newly discovered UM models within the agreed budget.

    B0 (2026-11-01): only the UM main chain is probed. Retired sources have
    no candidate entry here and their old token files are never read.
    """
    notices: list[str] = []
    used = 0
    probes = state.setdefault("probes", {})

    for source in ("um",):
        endpoint = UM_CHAT_URL
        for index, model in enumerate(list(catalogs.get(source) or [])):
            if (model.model_id not in candidate_ids.get(source, set()) or
                    _is_virtual_model(model.model_id)):
                continue
            probe_key = f"{source}|{model.model_id}"
            record = probes.setdefault(probe_key, {"attempts": 0, "status": "pending"})
            if record.get("status") in ("verified", "unsupported", "ambiguous"):
                continue
            if _record_int(record, "attempts", 0) >= MAX_PROBE_ATTEMPTS:
                notices.append(f"{model.model_id}能力探测已达重试上限")
                continue
            levels = PROBE_LEVELS[source]
            if used + len(levels) + 1 > MAX_PROBE_REQUESTS:
                notices.append("能力探测达到本轮50请求上限，其余模型延期")
                return notices
            record["attempts"] = _record_int(record, "attempts", 0) + 1
            accepted: list[str] = []
            transient = False
            successful_without_evidence = False
            try:
                for effort in (*levels, "__invalid_effort__"):
                    result = _post_json(endpoint, {
                        "model": model.model_id,
                        "messages": [{
                            "role": "user",
                            "content": "Return only the final integer: 17*19",
                        }],
                        "reasoning_effort": effort,
                        "max_tokens": 64,
                        "stream": False,
                    }, token=None)
                    used += 1
                    if effort == "__invalid_effort__":
                        if result["status"] < 400:
                            accepted = []
                            record["status"] = "ambiguous"
                        break
                    status = int(result["status"])
                    if status in (401, 403, 408, 409, 425, 429) or status >= 500:
                        transient = True
                    elif status < 400:
                        if _response_has_reasoning(result["body"]):
                            accepted.append(effort)
                        else:
                            successful_without_evidence = True
                if record.get("status") != "ambiguous":
                    if transient:
                        record["status"] = "pending"
                    elif accepted:
                        record["status"] = "verified"
                        record["efforts"] = accepted
                        record["default"] = "medium" if "medium" in accepted else accepted[0]
                        catalogs[source][index] = replace(
                            model, efforts=tuple(accepted),
                            default_effort=record["default"],
                            efforts_source="probe")
                    elif successful_without_evidence:
                        record["status"] = "ambiguous"
                    else:
                        record["status"] = "unsupported"
                notices.append(
                    f"{model.model_id}能力探测：{record['status']}")
            except Exception as exc:
                record["status"] = "pending"
                record["lastError"] = type(exc).__name__
                notices.append(f"{model.model_id}能力探测失败，稍后重试")
    return notices


def _archive_probe_generation(record: dict[str, Any], reason: str) -> bool:
    """Move the record's current-generation evidence into its ``history`` list.

    Returns True when there was evidence to archive.  The entry snapshots the
    record's CURRENT values (including its ``evidence_revision`` — the
    generation the evidence actually belonged to) BEFORE any reset, so the
    caller bumps the revision only AFTER calling this.  Old state records
    missing ``history`` or any evidence field stay readable: missing fields
    archive as None and are never interpreted as a measurement.  The history
    list is capped at PROBE_HISTORY_MAX entries — after each append the
    oldest generations are dropped first (人工回看用途，不无限增长).
    """
    has_evidence = any(record.get(field) is not None for field in (
        "observed_floor", "observed_ceiling", "last_run", "last_success",
        "last_error", "token_evidence")) or bool(record.get("attempts"))
    if not has_evidence:
        return False
    history = record.get("history")
    if not isinstance(history, list):
        history = []
    history.append({
        "status": record.get("status"),
        "evidence_revision": record.get("evidence_revision"),
        "observed_floor": record.get("observed_floor"),
        "observed_ceiling": record.get("observed_ceiling"),
        "attempts": record.get("attempts"),
        "last_run": record.get("last_run"),
        "last_success": record.get("last_success"),
        "last_error": record.get("last_error"),
        "token_evidence": record.get("token_evidence"),
        "stale_reason": record.get("stale_reason"),
        "reason": reason,
        "archived_at": datetime.now(timezone.utc).isoformat(),
    })
    del history[:-PROBE_HISTORY_MAX]
    record["history"] = history
    for field in ("observed_floor", "observed_ceiling", "last_run",
                  "last_success", "last_error", "token_evidence",
                  "stale_reason"):
        record[field] = None
    record["attempts"] = 0
    return True


# 说明随无声明上下文的记录一起播种（Plan-Revision 7 第 4 步）：目录没给
# context 时记录显式标注声明未知——探测仍可测量实际可用长度，绝不把未知
# 解释成零容量，也绝不从字符数换算声明值。
DECLARED_CONTEXT_UNKNOWN_NOTE = "声明未知，探测仍可测量实际可用长度"


def seed_context_probes(
        catalogs: dict[str, list[CatalogModel]], state: dict[str, Any],
        model_filter: dict[str, set[str]] | None = None,
        allow_partial_catalog: bool = False) -> None:
    """Seed the declared-context probe queue from valid catalogs (L-E.4).

    Runs inside run_sync AFTER the catalogs are collected: every model of a
    valid catalog joins (``pending`` when new) — including models WITHOUT a
    declared context (Plan-Revision 7 step 4): those records carry
    ``declared_context`` None, ``declared_context_known`` False and the
    honest note 声明未知，探测仍可测量实际可用长度 (declared_note), so they
    stay explicitly selectable for a real measurement.  ``catalogs`` must be
    the PRE-override snapshot (run_sync passes the raw collector output):
    ``declared_context`` records what the catalog claims, never the manual
    override's value, so an override set→clear cycle cannot flip or re-bill
    a verified record.

    ANY declared-context change (value → different value, appearing
    None → value, vanishing value → None) or a probe-scheme mismatch
    (record's ``scheme_revision`` differs from PROBE_SCHEME_REVISION) flips
    the existing record to ``stale`` — the old evidence belongs to a retired
    generation (目录声明或探测方案改变时旧证据标过期，Plan-Revision 7 第 3
    步)，which never retires the model itself.  Records whose declaration
    AND scheme revision are unchanged are left untouched, so a ``verified``
    record is reused instead of re-billed.  Only valid catalogs seed (the
    same validate_catalog gate the mirror plan applies).  PAUSED sources
    (m365) are never seeded into the active billing queue; their historical
    records just sit in state and are skipped honestly by the runner.  Under
    a write-scope model filter only the selected models join the queue.
    ``allow_partial_catalog=True`` (B3, 2026-11-01) lets the explicit
    probe-run entry seed only the selected models: the list is a validated
    SUBSET of the live catalog, so the historical drop-ratio guard (which
    compares against the previous whole-catalog count) must not reject it.
    Non-empty/unique/ID validation still applies.

    Evidence contract (Plan-Revision 7 steps 2-3): a new record starts at
    ``evidence_revision`` 1 with no measurement (token capacity unknown).
    A flip (declared change or scheme mismatch) ARCHIVES the current
    generation into the record's ``history`` list — never deleted, never
    re-labelled as current — then resets the evidence fields, bumps
    ``evidence_revision`` and stamps ``stale_reason``
    ("declared_context_changed" / "probe_scheme_changed" /
    "probe_scheme_missing"), so the stale record starts its next
    measurement from independent evidence instead of reusing an old floor.
    A record already stale with no fresh evidence only refreshes
    ``stale_reason`` (no second bump — no evidence was produced); a
    grandfathered stale record whose ``evidence_revision`` is missing (or
    garbage) lands its first revision on touch instead of staying at None
    forever.  Old state records missing these fields stay readable: seeding
    only adds fields and never interprets an absent field as a measurement
    or a zero capacity.
    """
    queue = state.setdefault("contextProbes", {})
    if not isinstance(queue, dict):
        queue = {}
        state["contextProbes"] = queue
    counts = state.get("catalogCounts") or {}
    now = datetime.now(timezone.utc).isoformat()
    for source, models in catalogs.items():
        valid, _ = validate_catalog(
            source, models,
            None if allow_partial_catalog else counts.get(source))
        if not valid:
            continue
        if source in PAUSED_SOURCES:
            # M365 保持暂停：不进入活动计费队列，历史记录原样保留。
            continue
        selected = (model_filter or {}).get(source)
        for model in models:
            # 无声明上下文的模型照常播种（declared_context=None、
            # declared_context_known=False）：可被明确选择并实测可用长度，
            # 绝不把未知解释成零容量。
            if selected is not None and model.model_id not in selected:
                continue
            key = f"{source}|{model.model_id}"
            record = queue.get(key)
            if record is None:
                queue[key] = {
                    "source": source,
                    "model_id": model.model_id,
                    "declared_context": model.context,
                    "declared_context_known": model.context is not None,
                    "declared_note": (None if model.context is not None
                                      else DECLARED_CONTEXT_UNKNOWN_NOTE),
                    "declared_at": now,
                    "status": "pending",
                    "observed_floor": None,
                    "attempts": 0,
                    "last_run": None,
                    "last_error": None,
                    "last_success": None,
                    "token_evidence": None,
                    "evidence_revision": 1,
                    "stale_reason": None,
                    "scheme_revision": PROBE_SCHEME_REVISION,
                    "history": [],
                }
                continue
            old_declared = record.get("declared_context")
            # 目录声明或探测方案改变时旧证据标过期（Plan-Revision 7 第 3
            # 步）：任意声明变化——含 None↔值——都翻 stale。声明是探测方案
            # 的输入，声明变了旧证据就是旧代次的；「出现/消失不推翻」的旧
            # 不对称豁免已废（它让声明抖动拖着过期证据继续跑）。
            declared_changed = old_declared != model.context
            record_scheme = record.get("scheme_revision")
            # 引入跟踪之前的记录按修订 1 解释：方案自队列创建以来未变，
            # 缺字段不等于方案不一致；一旦常量前进，缺字段记录同样过期。
            scheme_mismatch = (1 if record_scheme is None else
                               record_scheme) != PROBE_SCHEME_REVISION
            if not declared_changed and not scheme_mismatch:
                continue
            reason = ("declared_context_changed" if declared_changed
                      else "probe_scheme_changed"
                      if record_scheme is not None
                      else "probe_scheme_missing")
            if declared_changed:
                record["declared_context"] = model.context
                record["declared_at"] = now
            record["declared_context_known"] = model.context is not None
            record["declared_note"] = (None if model.context is not None
                                       else DECLARED_CONTEXT_UNKNOWN_NOTE)
            archived = _archive_probe_generation(record, reason=reason)
            # 代次边界：归档了旧证据、首次进入过期态、或祖传记录（修订号
            # 缺失或垃圾值）触碰——修订号 +1，垃圾值按缺省处理绝不冒充
            # 数字；已是过期态且无新证据时只刷新过期原因，不重复推进
            # 修订号。
            if archived or record.get("status") != "stale" or \
                    _record_int(record, "evidence_revision", 0) < 1:
                record["evidence_revision"] = \
                    _record_int(record, "evidence_revision", 0) + 1
            record["status"] = "stale"
            record["stale_reason"] = reason
            record["scheme_revision"] = PROBE_SCHEME_REVISION


def _context_probe_input(length: int, marker: str) -> str:
    """Numbered anchor lines sized to ~length chars plus a unique final anchor.

    Every line embeds the per-request marker, so the tail check is meaningful:
    the reply must quote the FINAL anchor (marker + "FINAL"), which only
    exists at the very end of the input — repeated filler cannot pass.
    """
    template = "[ctxprobe %s] anchor %06d of the long-input retention check"
    lines: list[str] = []
    used = 0
    index = 0
    while used < length:
        index += 1
        line = template % (marker, index)
        lines.append(line)
        used += len(line) + 1
    lines.append("[ctxprobe %s] FINAL anchor — quote this line verbatim"
                 % marker)
    return "\n".join(lines)


def _tail_anchor(body: Any, marker: str) -> bool:
    """True when any choice's reply quotes the request's final anchor."""
    if not isinstance(body, dict):
        return False
    token = "[ctxprobe %s] FINAL" % marker
    for choice in body.get("choices") or []:
        message = (choice or {}).get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and token in content:
            return True
    return False


def _probe_error_text(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("error") or body.get("message") or "")
    return str(body or "")


def _trusted_prompt_tokens(body: Any) -> int | None:
    """Trusted upstream-reported INPUT token count from a chat response body.

    Same trust rule as the gateway's usage boundary (chat2api._reported_tokens
    / _usage_side_tokens): a non-bool, non-negative int; ``prompt_tokens`` is
    canonical and ``input_tokens`` is the alias of the same side (canonical
    wins when both spellings are legal; sides are never summed).  Anything
    else — bool/negative/str/float/non-finite/absent — is NOT trusted and
    yields None.  Characters are never converted to tokens here: a response
    without a trustworthy usage report leaves the record's token evidence at
    None (Token 容量未知), never a char-derived estimate.
    """
    if not isinstance(body, dict):
        return None
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None

    def reported(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    tokens = reported(usage.get("prompt_tokens"))
    if tokens is None:
        tokens = reported(usage.get("input_tokens"))
    return tokens


# Substrings (lower-cased) that mark an explicit upstream context/input-length
# refusal in a 4xx error body; such a refusal is a capacity conclusion (an
# upper bound), unlike auth/quota/5xx trouble.
_CONTEXT_LIMIT_MARKERS = (
    "context length", "maximum context", "max context", "input length",
    "too long", "length limit", "token limit", "上下文",
)


def _explicit_context_limit(status: int, body: Any) -> bool:
    if not 400 <= status < 500:
        return False
    text = _probe_error_text(body).lower()
    return any(marker in text for marker in _CONTEXT_LIMIT_MARKERS)


def normalize_probe_selection(
        sources: Any, models: Any) -> dict[str, set[str] | None] | None:
    """Validate an explicit probe selection (Plan-Revision 7 step 3).

    ``sources``: non-empty duplicate-free list of non-empty strings, each a
    known source (KNOWN_SOURCES).  Paused sources (PAUSED_SOURCES — m365)
    are explicitly REJECTED the same way sync_apply's scope rejects them:
    they have no probe endpoint and never enter the billing queue, so a
    selection naming one is refused, never silently widened or silently
    emptied.  ``models``: non-empty object mapping a known source to a
    non-empty duplicate-free list of non-empty model ids; when ``sources``
    is also given every ``models`` key must be inside it.  The console path
    may hand ``sources`` over as a JSON-encoded string (the shared
    param-schema vocabulary has no list type) — the handler decodes that
    before calling this.  Empty collections, unknown sources, wrong types
    and dangling shapes raise ValueError (the console layer maps that to
    invalid_params): a selection that cannot be confirmed is REJECTED,
    never silently widened to the full queue.  When neither param is given
    the result is None (legacy full-queue semantics).
    """
    if sources is None and models is None:
        return None
    request: dict[str, set[str] | None] = {}
    if sources is not None:
        if (not isinstance(sources, (list, tuple)) or not sources or
                len(set(sources)) != len(sources) or
                any(not isinstance(item, str) or not item for item in sources)):
            raise ValueError("sources 必须为非空字符串的非空无重复列表")
        unknown = sorted(item for item in sources if item not in KNOWN_SOURCES)
        if unknown:
            raise ValueError("sources 含未知来源：%s" % ", ".join(unknown))
        paused = sorted(item for item in sources if item in PAUSED_SOURCES)
        if paused:
            raise ValueError("sources 含已暂停来源（不进入计费队列）：%s"
                             % ", ".join(paused))
        for item in sources:
            request[item] = None  # 该来源的全部在队记录
    if models is not None:
        if not isinstance(models, dict) or not models:
            raise ValueError("models 必须为非空的 {source: [model_id, ...]} 对象")
        for source, ids in models.items():
            if (not isinstance(source, str) or not source or
                    source not in KNOWN_SOURCES):
                raise ValueError("models 含未知来源：%r" % (source,))
            if source in PAUSED_SOURCES:
                raise ValueError("models 含已暂停来源（不进入计费队列）：%s"
                                 % source)
            if sources is not None and source not in sources:
                raise ValueError("models 的来源 %s 不在所选 sources 内" % source)
            if (not isinstance(ids, (list, tuple)) or not ids or
                    any(not isinstance(mid, str) or not mid for mid in ids) or
                    len(set(ids)) != len(ids)):
                raise ValueError(
                    "models[%s] 必须为非空字符串的非空无重复列表" % source)
            request[source] = set(ids)
    return request


def resolve_probe_selection(
        request: dict[str, set[str] | None] | None,
        records: dict[str, Any]) -> dict[str, set[str]] | None:
    """Resolve a validated selection request against the actual queue.

    ``{source: None}`` expands to every model id queued for that source
    (possibly an empty set — a legal filter result, zero requests);
    ``{source: {ids}}`` must name models that ARE in the queue — anything
    unresolvable raises ValueError so the caller can reject the call
    instead of silently probing the whole queue.
    """
    if request is None:
        return None
    if not isinstance(records, dict):
        records = {}
    present: dict[str, set[str]] = {}
    for key, record in records.items():
        if not isinstance(record, dict):
            continue
        source, _, model_id = str(key).partition("|")
        source = str(record.get("source") or source)
        model_id = str(record.get("model_id") or model_id)
        present.setdefault(source, set()).add(model_id)
    resolved: dict[str, set[str]] = {}
    missing: list[str] = []
    for source, ids in request.items():
        queued = present.get(source, set())
        if ids is None:
            resolved[source] = set(queued)
        else:
            missing.extend("%s|%s" % (source, model_id)
                           for model_id in sorted(ids - queued))
            resolved[source] = set(ids)
    if missing:
        raise ValueError("以下模型不在探测队列：%s" % ", ".join(sorted(missing)))
    return resolved


class ProbeCatalogUnavailable(ValueError):
    """The live catalog could not be read; the selection is unconfirmed.

    S2 high #6 (2026-11-01): a catalog READ FAILURE is NOT retirement.
    Callers must surface this as an unavailable/unconfirmed catalog, never as
    "the model was retired" — and never as a silently widened selection.
    """


def resolve_probe_selection_catalogs(
        explicit_models: dict[str, set[str]] | None,
        records: dict[str, Any],
        catalog_loader: Any = None,
        validate_queued: bool = False) -> dict[str, list[CatalogModel]]:
    """Resolve explicitly selected models against the live read-only catalog.

    B3 (2026-11-01): the probe-run entry is independent of the old client
    synchronization.  A selected model that is NOT queued is validated against
    the live READ-ONLY catalog and returned for seeding.

    S2 high #6 (2026-11-01): ``validate_queued=True`` also validates models
    that ARE already in the probe queue against the SAME live catalog.  A queued
    model absent from a successfully read catalog is retired/invalid exactly like
    an unqueued one — the caller rejects the selection instead of billing a
    request to a model the catalog no longer serves.  With the default
    ``validate_queued=False`` the historical queued-model behavior is kept for
    legacy direct-handler calls; the console operation layer passes True.

    A catalog that cannot be read raises :class:`ProbeCatalogUnavailable`
    (failure ≠ retirement): it is never reported as a retirement and never
    silently treated as a successful empty catalog.  A model absent from the live
    catalog raises ValueError — never silently probed and never silently dropped.
    Nothing is written here: the caller seeds only the returned models under the
    existing lock/transaction.
    """
    if not explicit_models:
        return {}
    present: dict[str, set[str]] = {}
    for key, record in (records or {}).items():
        if not isinstance(record, dict):
            continue
        source, _, model_id = str(key).partition("|")
        source = str(record.get("source") or source)
        model_id = str(record.get("model_id") or model_id)
        present.setdefault(source, set()).add(model_id)
    missing: dict[str, set[str]] = {}
    for source, ids in explicit_models.items():
        gap = set(ids) - present.get(source, set())
        if gap:
            missing[source] = gap
    if not missing and not validate_queued:
        return {}
    # Validate queued models too when asked, but only read the catalog for the
    # sources actually being checked (queued models need no seeding).
    checked = explicit_models if validate_queued else missing
    loader = catalog_loader or collect_catalogs
    try:
        catalogs, _warnings = loader(sources=sorted(checked))
    except ValueError:
        raise
    except Exception as exc:
        raise ProbeCatalogUnavailable(
            "目录不可用，无法确认所选模型：%s" % type(exc).__name__) from exc
    selected: dict[str, list[CatalogModel]] = {}
    for source, ids in checked.items():
        models = catalogs.get(source)
        if models is None:
            raise ProbeCatalogUnavailable(
                "来源 %s 目录不可用，无法确认所选模型（读取失败，不是退役）"
                % source)
        by_id = {model.model_id: model for model in models}
        absent = sorted(model_id for model_id in ids
                        if model_id not in by_id)
        if absent:
            raise ValueError(
                "以下模型不在当前 %s 目录（退役或非法）：%s"
                % (source, ", ".join(absent)))
        seed_ids = missing.get(source)
        if seed_ids:
            selected[source] = [by_id[model_id]
                                for model_id in sorted(seed_ids)]
    return selected


def endpoint_credential(endpoint: Any) -> tuple[str, Any, bool]:
    """(url, token, readable) for one probe endpoint — ONE implementation.

    The runner's skip decision and the preview's ``executable`` fact must never
    drift: the preview's estimate IS the run's billing set, so both call THIS
    function.  ``readable`` is False only when a configured provider resolves
    no token (unreadable credential); a missing provider means no credential is
    needed (readable).  The provider is called at most once per call.
    """
    url, token_source = endpoint
    token = token_source() if callable(token_source) else token_source
    return url, token, token_source is None or bool(token)


def probe_context_queue(
        endpoints: dict[str, Any], budget: dict[str, Any],
        records: dict[str, dict[str, Any]], should_continue: Any,
        post_json: Any = None, steps: Any = None, selection: Any = None,
        retest: bool = False) -> dict[str, Any]:
    """Work the declared-context probe queue (L-E.4) — REAL billed requests.

    ``endpoints`` maps source -> (chat url, token provider or None); records
    of sources without an endpoint are skipped with an honest note (paused
    sources — m365 — say 来源已暂停 explicitly; unwired sources say
    未登记探测端点), and a provider that resolves to no token skips its
    records with ZERO requests — credentials unavailable is reported, an
    unauthenticated request is never sent and no success is fabricated.
    The endpoint/credential availability check happens BEFORE any generation
    boundary: a skipped record keeps its status and evidence untouched
    (stale stays stale instead of being flipped to pending and stuck there
    forever; a retest of a verified/failed record archives nothing when the
    source cannot run) — zero requests, zero state change, honest row.
    ``budget`` caps the whole run ({"max_requests", "max_tokens"});
    ``should_continue`` is consulted BEFORE every request (cancel hook).
    ``post_json``/``steps`` are injectable for offline tests (defaults:
    module _post_json and PROBE_INPUT_STEPS).

    ``selection`` restricts the request set (Plan-Revision 7 step 3): None
    means the whole queue (legacy semantics); a mapping
    {source: collection of model ids} processes ONLY those records —
    unselected records are untouched this round (no row, no state change, no
    budget).  A selection that is not None/dict-of-collections raises
    ValueError: an unconfirmable selection is rejected, never widened.

    Generation rules (step 3): a STALE record's old evidence belongs to a
    retired generation — at first touch it is archived into ``history``
    (when any is present) with ``evidence_revision`` bumped, the scheme
    revision stamped, and the record re-queued as ``pending`` so the old
    floor can never skip the new generation's ladder.  A grandfathered
    record whose ``evidence_revision`` is missing (or garbage) lands its
    first revision on touch instead of staying at None forever.  Record
    fields are read defensively: a garbage value (str/bool) for an integer
    field reads as its default instead of crashing the pass or silently
    adopting the string (``int("3")`` would).  ``verified``/``failed``
    records are only re-measured when ``retest`` is true: their current
    evidence moves to ``history`` and the generation restarts from
    independent evidence (observed_floor/observed_ceiling None, attempts 0,
    status pending, evidence_revision +1).  When a record concludes
    (verified, or failed via an explicit limit refusal) its ``stale_reason``
    is cleared — the reason lives on in the archived history entry.

    Per record: walk the input ladder ascending, skipping steps at or below
    the recorded observed_floor (known-good CURRENT-generation lengths are
    not re-billed).  A success whose reply quotes the final anchor raises
    observed_floor and continues; completing the ladder verifies the record.
    An explicit upstream context-limit refusal records a failure upper bound
    (observed_ceiling, status failed).  401/403/429/5xx/timeouts are transient
    (no capacity conclusion — the record stops for this round and keeps its
    status); a success WITHOUT the tail anchor is inconclusive and also stops
    the record.  attempts/last_run/observed_floor update after every request;
    an anchor-verified success also stamps ``last_success`` and raises
    ``token_evidence`` to the largest trusted upstream-reported input token
    count seen so far (None stays None without one — token capacity unknown,
    never derived from characters).  observed_floor/observed_ceiling are
    historical CHARACTER-ladder evidence, never token capacities.
    """
    if selection is not None and not isinstance(selection, dict):
        raise ValueError("selection 必须为 {source: model_id 集合} 或 None")
    post = post_json or _post_json
    ladder = tuple(steps) if steps is not None else PROBE_INPUT_STEPS
    max_requests = int((budget or {}).get("max_requests") or 0)
    requested_tokens = int((budget or {}).get("max_tokens") or 0)
    max_tokens = min(requested_tokens or CONTEXT_PROBE_MAX_TOKENS,
                     CONTEXT_PROBE_MAX_TOKENS)
    used = 0
    stop_reason: str | None = None
    results: list[dict[str, Any]] = []
    processed: set[str] = set()
    for key, record in list(records.items()):
        if not isinstance(record, dict):
            continue
        source, _, model_id = str(key).partition("|")
        source = str(record.get("source") or source)
        model_id = str(record.get("model_id") or model_id)
        if selection is not None:
            allowed = selection.get(source)
            # 未选中：本轮不动、不消耗预算、不产生结果行。
            if not isinstance(allowed, (set, frozenset, list, tuple)) or \
                    model_id not in allowed:
                continue
        status = record.get("status")
        if not (status in ("pending", "stale") or
                (status in ("verified", "failed") and retest)):
            continue
        # 端点与凭据的可用性检查先于任何状态归一（F9 修订）：无端点或凭据
        # 不可读的来源零请求、零状态变更——stale 保持 stale（不再被翻成
        # pending 后永远出不了队），verified/failed+retest 不清空当前代次
        # 证据；仍产生结果行并如实说明。
        endpoint = endpoints.get(source)
        if endpoint is None:
            # 端点失败不删除模型：未接线与已暂停分开说明，均为零请求。
            results.append({
                "key": key, "source": source, "model_id": model_id,
                "status": status, "requests": 0, "executed": False,
                "note": ("该来源已暂停，跳过（零请求）"
                         if source in PAUSED_SOURCES else
                         "该来源未登记探测端点，跳过（零请求）")})
            processed.add(key)
            continue
        url, token, readable = endpoint_credential(endpoint)
        if not readable:
            # 凭据不可读 ≠ 认证被拒 ≠ 模型不存在：零请求跳过，如实说明，
            # 绝不发送无凭据请求，也绝不伪造成功（与预览同一判据）。
            results.append({
                "key": key, "source": source, "model_id": model_id,
                "status": status, "requests": 0, "executed": False,
                "note": "该来源凭据不可读（token 缺失），跳过（零请求）"})
            processed.add(key)
            continue
        previous_status = status
        if status == "stale":
            # 代次边界：过期记录的旧证据（若有——旧状态可能仍带）归档后
            # 从独立证据开始；旧 floor 绝不用于跳过新代次步进。祖传记录
            # （修订号缺失或垃圾值）触碰时也落第一个修订号，不再永不落地。
            archived = _archive_probe_generation(
                record, reason=str(record.get("stale_reason") or "stale"))
            if archived or _record_int(record, "evidence_revision", 0) < 1:
                record["evidence_revision"] = \
                    _record_int(record, "evidence_revision", 0) + 1
            record["scheme_revision"] = PROBE_SCHEME_REVISION
            record["status"] = "pending"
            status = "pending"
        elif status in ("verified", "failed"):
            # 显式重测（前面资格检查保证此时 retest=True）：旧代次证据移入
            # history 保留，新代次从独立证据开始。pending 记录不走这里，
            # 直接从当前代次续跑。
            _archive_probe_generation(record, reason="retest")
            record["evidence_revision"] = \
                _record_int(record, "evidence_revision", 0) + 1
            record["scheme_revision"] = PROBE_SCHEME_REVISION
            record["stale_reason"] = None
            record["status"] = "pending"
            status = "pending"
        row = {"key": key, "source": source, "model_id": model_id,
               "status": status, "requests": 0, "executed": False,
               "note": ""}
        if status != previous_status:
            row["previous_status"] = previous_status
        if previous_status in ("verified", "failed") and retest:
            row["retest"] = True
        results.append(row)
        processed.add(key)
        floor = record.get("observed_floor")
        # 垃圾值防御：非整数 floor（含 bool）不构成已知步进证据，按无 floor
        # 处理（宁可多跑一步，绝不拿垃圾值当跳过依据或让它炸掉整轮）。
        if isinstance(floor, bool) or not isinstance(floor, int):
            floor = None
        pending_steps = [step for step in ladder
                         if floor is None or step > floor]
        if not pending_steps:
            # Existing measured evidence already covers the whole ladder —
            # CURRENT-generation resume only: stale records never reach this
            # with their old floor (it was archived at first touch).
            record["status"] = "verified"
            record["stale_reason"] = None
            row["status"] = "verified"
            row["note"] = "既有实测证据已覆盖全部步进，无需新请求"
            continue
        completed = True
        for step in pending_steps:
            if used >= max_requests:
                stop_reason = "budget"
                completed = False
                break
            if should_continue is not None and not should_continue():
                stop_reason = "cancelled"
                completed = False
                break
            used += 1
            row["requests"] += 1
            row["executed"] = True
            record["attempts"] = _record_int(record, "attempts", 0) + 1
            record["last_run"] = datetime.now(timezone.utc).isoformat()
            marker = "run%03d" % used
            body = {
                "model": model_id,
                "messages": [{
                    "role": "user",
                    "content": (
                        "The user message below contains numbered anchor "
                        "lines. Reply with ONLY the text of the FINAL anchor "
                        "line, verbatim, nothing else.\n\n"
                        + _context_probe_input(step, marker)),
                }],
                "max_tokens": max_tokens,
                "stream": False,
            }
            try:
                result = post(url, body, token=token)
            except Exception as exc:  # transport/timeout: no capacity verdict
                record["last_error"] = type(exc).__name__
                row["note"] = "transient:%s，不构成容量结论" % type(exc).__name__
                completed = False
                break
            http_status = int(result.get("status") or 0)
            body = result.get("body")
            if http_status < 400:
                if _tail_anchor(body, marker):
                    record["observed_floor"] = step
                    # 历史字符步进证据：observed_floor 是字符数，不是 token
                    # 容量。Token 证据另列：只有锚点校验成功的请求才采信其
                    # 上游 usage 报告（真实归一化边界），无可信报告不写入，
                    # 绝不做字符→token 换算。
                    record["last_success"] = \
                        datetime.now(timezone.utc).isoformat()
                    tokens = _trusted_prompt_tokens(body)
                    # 垃圾值防御：存量 token_evidence 非 int（含 bool）时
                    # 比较前按 None 处理——垃圾不冒充数字，也不炸掉本轮；
                    # 可信报告到来即替换。
                    stored_tokens = record.get("token_evidence")
                    if tokens is not None and (
                            isinstance(stored_tokens, bool) or
                            not isinstance(stored_tokens, int) or
                            tokens > stored_tokens):
                        record["token_evidence"] = tokens
                    record.pop("last_error", None)
                    row["note"] = "floor=%d" % step
                    continue
                record["last_error"] = "InconclusiveResponse"
                row["note"] = "回复未引用末段锚点，停止该记录本轮"
                completed = False
                break
            if _explicit_context_limit(http_status, body):
                record["status"] = "failed"
                record["observed_ceiling"] = step
                record["last_error"] = "ContextLimitRefused"
                # 代次在新证据下结论：过期原因转入归档历史，当前字段干净。
                record["stale_reason"] = None
                row["status"] = "failed"
                row["note"] = "上游明确上下文超限，失败上界=%d" % step
                completed = False
                break
            record["last_error"] = "HTTPError"
            row["note"] = "transient:HTTP%d，不构成容量结论" % http_status
            completed = False
            break
        if completed:
            record["status"] = "verified"
            record["stale_reason"] = None
            row["status"] = "verified"
            row["note"] = row["note"] or "步进表全部成功，实测通过"
        if stop_reason is not None:
            break
    # B3 (2026-11-01): when the cumulative batch budget (or a cancel)
    # stops the run, every remaining eligible selected record is reported as
    # explicitly NOT executed.  A partially executed record keeps its real
    # request count; the remaining records get requests=0 and an honest note.
    # The budget is never reset per model and no new batch is appended.
    if stop_reason is not None:
        stop_note = ("预算耗尽，本轮未执行" if stop_reason == "budget"
                     else "已取消，本轮未执行")
        for row in results:
            if not row.get("requests") and not row.get("note"):
                row["note"] = stop_note
        for key, record in list(records.items()):
            if key in processed or not isinstance(record, dict):
                continue
            source, _, model_id = str(key).partition("|")
            source = str(record.get("source") or source)
            model_id = str(record.get("model_id") or model_id)
            if selection is not None:
                allowed = selection.get(source)
                if not isinstance(allowed, (set, frozenset, list, tuple)) or \
                        model_id not in allowed:
                    continue
            status = record.get("status")
            if not (status in ("pending", "stale") or
                    (status in ("verified", "failed") and retest)):
                continue
            results.append({
                "key": key, "source": source, "model_id": model_id,
                "status": status, "requests": 0, "executed": False,
                "note": stop_note})
    return {"used": used, "stopped": stop_reason, "results": results,
            "max_requests": max_requests, "max_tokens": max_tokens}


def probe_recovery_path() -> Path:
    """Recovery evidence file next to the state file (same data directory).

    Derived from the current ``STATE_FILE`` so tests that redirect the data
    directory never touch the operator's real state; no request input selects it.
    """
    return STATE_FILE.with_name("model-sync-probe-recovery.json")


def read_probe_recovery() -> dict[str, Any] | None:
    """Read the bounded probe recovery evidence, if any (read-only)."""
    path = probe_recovery_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def merge_probe_recovery(state: dict[str, Any]) -> bool:
    """Merge preserved billed evidence into the in-memory state (B3).

    A recovery file exists only after a failed state transaction.  Its records
    are ACTUAL post-run evidence; they are merged into the queue by their
    ``last_run`` stamp (a newer run wins, an older/absent record never
    overwrites newer evidence).  The caller's successful transaction persists
    the merged state and then clears the recovery file; a repeated failure
    keeps the evidence (including the previous recovery generation).
    """
    recovery = read_probe_recovery()
    if not recovery:
        return False
    records = recovery.get("records")
    if not isinstance(records, dict):
        return False
    queue = state.get("contextProbes")
    if not isinstance(queue, dict):
        queue = {}
        state["contextProbes"] = queue
    merged = False
    previous = recovery.get("previous_recovery")
    previous_records = (previous.get("records")
                        if isinstance(previous, dict) else None)
    for source_records in (records, previous_records):
        if not isinstance(source_records, dict):
            continue
        for key, record in source_records.items():
            if not isinstance(record, dict):
                continue
            current = queue.get(key)
            if not isinstance(current, dict):
                queue[key] = record
                merged = True
                continue
            if str(record.get("last_run") or "") > str(
                    current.get("last_run") or ""):
                queue[key] = record
                merged = True
    return merged


def write_probe_recovery(payload: dict[str, Any]) -> Path:
    """Atomically preserve billed probe evidence after a state save failure.

    B3 (2026-11-01): the state transaction failed after real requests were
    sent; the evidence must not be lost or forged.  This writes the actual
    post-run records/results to a bounded recovery file next to the state
    file (never a client/Magpie config), stamped ``persistence="failed"`` so
    it can never be mistaken for a successful state save.
    """
    path = probe_recovery_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Preserve the previous failed generation under one bounded key before
    # this write replaces the file; never nest a third generation.
    previous = read_probe_recovery()
    if previous:
        payload = dict(payload)
        payload["previous_recovery"] = {
            key: value for key, value in previous.items()
            if key != "previous_recovery"}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="", delete=False,
            dir=path.parent, prefix=path.name + ".", suffix=".tmp") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def clear_probe_recovery() -> None:
    """Drop an old recovery file after a successful state save (best effort)."""
    try:
        probe_recovery_path().unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # A leftover recovery file is honest old evidence; never mask the
        # successful state transaction with a cleanup failure.
        pass


# ---------------------------------------------------------------------------
# Manual context overrides (Plan-Revision 7 step 6; contract recorded in the
# module docstring).  State owner: the model-sync state file's
# "contextOverrides" mapping; writes go only through set_context_override /
# clear_context_override under the console actions' SyncLock.
# ---------------------------------------------------------------------------


class ContextOverrideConflict(ValueError):
    """The stored override no longer matches the caller's expected version.

    Raised by set/clear when ``expected_set_at`` does not match the stored
    ``set_at`` (including "override appeared since the read").  The console
    layer maps it to a version_conflict error — a concurrent change is never
    silently clobbered.
    """


# Manual override bounds: the lower bound is "> 0" in
# _valid_override_context; this inclusive upper bound keeps a hand-typed
# value from dwarfing every real context window (F11 修订：下界也加上界).
CONTEXT_OVERRIDE_MAX = 10_000_000


def _record_int(record: dict[str, Any], field: str, default: int = 0) -> int:
    """Read a record's integer field defensively (state may be hand-edited).

    A bool or a non-int value (str/float/None) is NOT trusted and reads as
    ``default``: a garbage value never crashes a seeding/queue pass and
    never leaks into arithmetic the way ``int("3")`` or ``int(record.get(x)
    or 0)`` would (the latter silently adopts any string int and crashes on
    anything else).
    """
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _override_applies(entry: Any, model_context: int | None) -> bool:
    """Shared decision: does one stored override change this model's value?

    True only when the entry is a well-formed dict carrying a valid
    positive-int context AND that context differs from the model's catalog
    declaration — a None declaration is a legal target (200000 != None is a
    real change).  ``apply_context_overrides`` (the write path) and
    ``context_override_surface`` (the read face) both call this, so the two
    can never disagree about whether an override is in effect.
    """
    return (isinstance(entry, dict) and
            _valid_override_context(entry.get("context")) and
            entry.get("context") != model_context)


def _valid_override_context(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, int)
            and 0 < value <= CONTEXT_OVERRIDE_MAX)


def _setting_model(catalogs, key):
    source, _, model_id = key.partition("|")
    model = next((m for m in catalogs.get(source, [])
                  if isinstance(m, CatalogModel) and m.model_id == model_id), None)
    if model is None:
        raise ValueError("模型不在当前目录：%s" % key)
    return model


def _setting_efforts(model, state):
    """Reuse verified capability evidence without issuing a probe."""
    if model.efforts:
        return tuple(model.efforts), model.efforts_source or "upstream"
    record = (state.get("probes") or {}).get(f"{model.source}|{model.model_id}")
    if isinstance(record, dict) and record.get("status") == "verified":
        levels = _effort_names(record.get("efforts"), allow_max=True)
        if levels:
            return levels, "probe"
    return (), None


def _validate_setting_values(model, state, values):
    """Validate effective settings and bind measured values to token evidence."""
    evidence = {}
    alias = values.get("alias")
    if alias is not None and (not isinstance(alias, str) or
                              not alias.strip() or len(alias) > 128):
        raise ValueError("alias 必须为 1-128 字符字符串或 None")
    effort = values.get("defaultEffort")
    if effort is not None:
        levels, origin = _setting_efforts(model, state)
        if not isinstance(effort, str) or effort not in levels:
            raise ValueError("defaultEffort 缺少支持证据或不在支持档位中")
        evidence["effort"] = {"levels": list(levels), "source": origin}
    kind, tokens = values.get("contextSource"), values.get("contextTokens")
    if kind not in (None, "declared", "manual", "measured"):
        raise ValueError("contextSource 必须为 declared/manual/measured 或 None")
    if kind in (None, "declared"):
        if tokens is not None:
            raise ValueError("声明上下文不接受 contextTokens")
    elif not _valid_override_context(tokens):
        raise ValueError("contextTokens 必须为 1-%d 的正整数" % CONTEXT_OVERRIDE_MAX)
    if kind == "measured":
        key = f"{model.source}|{model.model_id}"
        record = (state.get("contextProbes") or {}).get(key)
        if not isinstance(record, dict):
            raise ValueError("实测上下文没有可信 Token 证据；字符数不能作 Token")
        bound = record.get("token_evidence")
        revision = record.get("evidence_revision")
        if (record.get("source") != model.source or
                record.get("model_id") != model.model_id or
                record.get("status") not in ("verified", "failed", "pending") or
                not _valid_override_context(bound) or
                isinstance(revision, bool) or not isinstance(revision, int) or revision < 1 or
                record.get("scheme_revision") != PROBE_SCHEME_REVISION or
                record.get("declared_context") != model.context or tokens > bound):
            raise ValueError("实测上下文证据缺席、过期或超出已验证 Token 下界")
        evidence["context"] = {
            "key": key, "revision": revision, "tokenLowerBound": bound,
            "declaredContext": model.context,
            "schemeRevision": record["scheme_revision"],
        }
    return evidence


def save_model_setting_draft(
        catalogs: dict[str, list[CatalogModel]], source: str, model_id: str,
        fields: dict[str, Any] | None = None,
        expected_draft_version: int | None = None) -> dict[str, Any]:
    """Validate and write one model-settings DRAFT (caller holds SyncLock).

    Rev8 契约 5/6（2026-10-02）：保存只写草稿，绝不触碰客户端配置，也绝不
    改动已应用值。可保存字段（部分更新，只合并给出的键）：
      * ``alias``：非空 str ≤128 或 None（None = 恢复目录显示名）；只影响
        显示名，真实模型身份不变；
      * ``contextSource``："declared"|"manual"|"measured"|None（None = 恢复
        目录声明）；
      * ``contextTokens``：manual/measured 时必填且为 1..CONTEXT_OVERRIDE_MAX
        的正整数；declared 时必须为 None；
      * ``defaultEffort``：必须在该模型当前目录 efforts 内，或 None（恢复上
        游默认）——档位校验针对当前目录 efforts，证据不足（模型不在目录）
        直接拒绝。
    ``source|model_id`` 必须存在于当前目录快照（只读采集）。版本规则：
    ``expected_draft_version`` 与存储 draftVersion 不一致 →
    ContextOverrideConflict（绝不静默覆盖并发修改）。返回写入后的记录
    （含 draft/draftVersion/draftSavedAt/applied 元数据）；调用方以既有事务
    持久化（本函数内部已落盘：STATE_FILE 单文件事务）。
    """
    if not isinstance(source, str) or not source or \
            not isinstance(model_id, str) or not model_id:
        raise ValueError("source 与 model_id 必须为非空字符串")
    fields = dict(fields or {})
    allowed = {"alias", "contextSource", "contextTokens", "defaultEffort"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError("未知设置字段：%s" % ",".join(sorted(unknown)))
    draft_fields: dict[str, Any] = {}
    alias = fields.get("alias")
    if "alias" in fields:
        if alias is not None and (not isinstance(alias, str) or
                                  not alias.strip() or len(alias) > 128):
            raise ValueError("alias 必须为 1-128 字符字符串或 None")
        draft_fields["alias"] = alias.strip() if alias else None
    source_kind = fields.get("contextSource")
    if "contextSource" in fields and source_kind not in (
            None, "declared", "manual", "measured"):
        raise ValueError("contextSource 必须为 declared/manual/measured 或 None")
    tokens = fields.get("contextTokens")
    if "contextTokens" in fields and tokens is not None:
        if not _valid_override_context(tokens):
            raise ValueError(
                "contextTokens 必须为 1-%d 的正整数" % CONTEXT_OVERRIDE_MAX)
    if "contextSource" in fields:
        if source_kind in (None, "declared"):
            if tokens is not None:
                raise ValueError(
                    "contextSource=declared/None 时不接受 contextTokens")
            draft_fields["contextSource"] = source_kind
            draft_fields["contextTokens"] = None
        else:
            if tokens is None:
                raise ValueError(
                    "contextSource=%s 必须给出 contextTokens" % source_kind)
            draft_fields["contextSource"] = source_kind
            draft_fields["contextTokens"] = int(tokens)
    elif "contextTokens" in fields:
        draft_fields["contextTokens"] = tokens
    effort = fields.get("defaultEffort")
    if "defaultEffort" in fields:
        if effort is not None and (
                not isinstance(effort, str) or
                not effort.strip() or len(effort) > 32):
            raise ValueError("defaultEffort 必须为 1-32 字符字符串或 None")
        draft_fields["defaultEffort"] = effort
    if not draft_fields:
        raise ValueError("未提供任何可保存的设置字段")
    source_models = catalogs.get(source) if isinstance(catalogs, dict) else None
    model = next((m for m in (source_models or [])
                  if isinstance(m, CatalogModel) and m.model_id == model_id),
                 None)
    if model is None:
        raise ValueError("模型不在当前目录：%s|%s" % (source, model_id))
    state = load_state()
    settings = _model_settings_of_state(state)
    key = f"{source}|{model_id}"
    record = settings.get(key)
    if not isinstance(record, dict):
        record = {"draft": None, "draftVersion": 0, "draftSavedAt": None,
                  "applied": None, "appliedVersion": None, "appliedAt": None,
                  "appliedEvidence": None}
    if expected_draft_version is not None and \
            record.get("draftVersion") != expected_draft_version:
        raise ContextOverrideConflict(
            "草稿版本冲突（%s）：读面 draftVersion=%s，当前存储=%s；请基于"
            "最新读面重试" % (key, expected_draft_version,
                             record.get("draftVersion")))
    # An alias-only edit starts from the last published values when no draft
    # exists. Explicit nulls restore defaults; absent fields preserve settings.
    merged = dict(record.get("draft") or record.get("applied") or {})
    merged.update(draft_fields)
    # 规范形状：草稿恒为全四键（alias/contextSource/contextTokens/
    # defaultEffort），未给出的键补 None——读面与消费方无需猜缺省。
    for field in ("alias", "contextSource", "contextTokens",
                  "defaultEffort"):
        merged.setdefault(field, None)
    record["draftEvidence"] = _validate_setting_values(model, state, merged)
    record["draft"] = merged
    # R6/D4 显式上下文决定（2026-10-10）：本次保存是否显式设置了上下文
    # 字段（contextSource/contextTokens，含 null=恢复声明）——这是用户对
    # 该模型上下文的明确选择，用于解除「客户端冲突不覆盖」的保留；只改
    # alias/档位的保存不算上下文决定，冲突仍待用户裁定。
    record["contextDecided"] = ("contextSource" in fields or
                               "contextTokens" in fields)
    record["draftVersion"] = int(record.get("draftVersion") or 0) + 1
    record["draftSavedAt"] = datetime.now(timezone.utc).isoformat()
    settings[key] = record
    state["modelSettings"] = settings
    apply_config_transaction(
        {STATE_FILE: json.dumps(state, ensure_ascii=False, indent=2) + "\n"},
        BACKUP_DIR, keep_backups=30)
    return dict(record)


def set_context_override(
        catalogs: dict[str, list[CatalogModel]], source: str, model_id: str,
        context: int, expected_set_at: str | None = None) -> dict[str, Any]:
    """Legacy-name wrapper: save one manual context DRAFT (Rev8 契约 6).

    历史入口保留名称与参数（console 动作与测试沿用），语义演进为「保存上下
    文草稿」：写入 modelSettings 的 contextSource=manual 草稿（draftVersion
    递增），定时同步不消费草稿——真实发布只能走 model_settings_apply 的
    显式应用流。返回草稿记录（context/set_at/source 键仍返回以兼容旧读方，
    另加 draftVersion）。caller holds SyncLock（与旧语义一致）。
    """
    # 旧契约校验强度保持：context 非正整数（bool/None/浮点/字符串）先于
    # 一切状态访问被拒，绝不静默转换。
    if not _valid_override_context(context):
        raise ValueError(
            "context 必须为 1-%d 的正整数" % CONTEXT_OVERRIDE_MAX)
    # Rev8 版本规则（保持旧契约强度）：存在任一待应用草稿（含恢复草稿）时，
    # bare set 冲突（调用方必须确认它读到的版本令牌）；expected_set_at 与
    # 读面 draftSavedAt 比对，匹配才允许更新（draftVersion 内层再核一次）。
    peek = load_state()
    record = _model_settings_of_state(peek).get(f"{source}|{model_id}")
    draft = record.get("draft") if isinstance(record, dict) else None
    if isinstance(draft, dict):
        stored_at = record.get("draftSavedAt")
        if expected_set_at is None or expected_set_at != stored_at:
            raise ContextOverrideConflict(
                "覆盖版本冲突（%s）：读面 override_at=%s，当前存储=%s；请基于"
                "最新读面重试" % (f"{source}|{model_id}", expected_set_at,
                                 stored_at))
    record = save_model_setting_draft(
        catalogs, source, model_id,
        {"contextSource": "manual", "contextTokens": int(context)},
        expected_draft_version=(record or {}).get("draftVersion"))
    draft = dict(record.get("draft") or {})
    return {"context": draft.get("contextTokens"),
            "set_at": record.get("draftSavedAt"),
            "source": "manual",
            "draft_version": record.get("draftVersion")}


def clear_context_override(
        source: str, model_id: str,
        expected_set_at: str | None = None) -> dict[str, Any]:
    """Write a RESTORE draft for the context field (Rev8 契约 6).

    恢复也形成可预览变更：本函数不再直接删除存储覆盖，而是为该模型的
    context 字段写入恢复草稿（draft.contextSource=None → 显式应用后回到目录
    声明）。当既无草稿也无已应用覆盖时是纯零写入 no-op（返回 existed=False/
    changed=False，状态文件不重写）；测量历史（contextProbes）不受影响。
    版本规则：``expected_set_at`` 与读面暴露的版本令牌比对——有草稿比
    draftSavedAt，无草稿有已应用覆盖比 applied 的 set_at，不一致抛
    ContextOverrideConflict（绝不静默覆盖并发修改）。
    """
    if not isinstance(source, str) or not source or \
            not isinstance(model_id, str) or not model_id:
        raise ValueError("source 与 model_id 必须为非空字符串")
    state = load_state()
    settings = _model_settings_of_state(state)
    key = f"{source}|{model_id}"
    record = settings.get(key)
    if not isinstance(record, dict):
        record = {"draft": None, "draftVersion": 0, "draftSavedAt": None,
                  "applied": None, "appliedVersion": None, "appliedAt": None,
                  "appliedEvidence": None}
    draft = record.get("draft") if isinstance(record.get("draft"), dict) \
        else None
    applied = record.get("applied") if isinstance(record.get("applied"), dict) \
        else None
    draft_pending = bool(draft and draft.get("contextSource") is not None)
    applied_manual = bool(applied and
                          applied.get("contextSource") in ("manual",
                                                           "measured"))
    restore_pending = bool(
        draft is not None and draft.get("contextSource") is None and
        draft.get("contextTokens") is None and applied_manual)
    if not draft_pending and not applied_manual:
        return {"existed": False, "changed": False}
    if restore_pending and not draft_pending:
        # 恢复草稿已在待应用：再次 clear 是幂等 no-op（同版本零写入），
        # 返回 existed=True/changed=False 与当前草稿版本令牌。
        return {"existed": True, "changed": False,
                "draftVersion": record.get("draftVersion"),
                "note": "恢复草稿已在待应用（幂等，零写入）；发布仍走 "
                        "model.settings_apply"}
    # Rev8 版本规则（保持旧契约强度）：bare clear over an existing draft or
    # applied override conflicts — the caller must confirm the version token
    # it read (draftSavedAt / applied setAt), never a blind delete.
    if expected_set_at is None:
        raise ContextOverrideConflict(
            "恢复版本冲突（%s）：存在待恢复覆盖但未提供版本令牌；请基于最新"
            "读面重试" % key)
    # Rev8 版本令牌在记录级：待应用草稿比 draftSavedAt，已应用覆盖比
    # appliedAt（draft 字典本身不带时间戳）。
    stored_at = (record.get("draftSavedAt") if draft_pending
                 else record.get("appliedAt"))
    if stored_at != expected_set_at:
        raise ContextOverrideConflict(
            "恢复版本冲突（%s）：读面版本令牌=%s，当前存储=%s；请基于"
            "最新读面重试" % (key, expected_set_at, stored_at))
    merged = dict(draft or {})
    merged["contextSource"] = None
    merged["contextTokens"] = None
    record["draft"] = merged
    # 恢复草稿同样是显式的上下文决定（恢复到目录声明）：解除冲突保留，
    # 应用后按目录声明发布。
    record["contextDecided"] = True
    record["draftVersion"] = int(record.get("draftVersion") or 0) + 1
    record["draftSavedAt"] = datetime.now(timezone.utc).isoformat()
    settings[key] = record
    state["modelSettings"] = settings
    apply_config_transaction(
        {STATE_FILE: json.dumps(state, ensure_ascii=False, indent=2) + "\n"},
        BACKUP_DIR, keep_backups=30)
    return {"existed": True, "changed": True,
            "draftVersion": record["draftVersion"],
            "note": "恢复草稿已保存：应用后回到目录声明（保存≠已应用）"}


def _context_overrides_of_state(state: dict[str, Any]) -> dict[str, Any]:
    overrides = state.get("contextOverrides")
    if not isinstance(overrides, dict):
        overrides = {}
    return overrides


def _model_settings_of_state(state: dict[str, Any]) -> dict[str, Any]:
    """Per-model settings records (Rev8 契约 6); missing/corrupt → empty."""
    settings = state.get("modelSettings")
    if not isinstance(settings, dict):
        settings = {}
    return settings


def pending_model_setting_versions(state: dict[str, Any]) -> dict[str, int]:
    """Read face for the apply binding: key -> draftVersion of every record
    with a pending draft, including edits of already published settings.
    The console passes these back
    as ``selection`` to promote + apply; a stale version is a conflict."""
    pending: dict[str, int] = {}
    for key, record in _model_settings_of_state(state).items():
        if not isinstance(key, str) or not isinstance(record, dict):
            continue
        if isinstance(record.get("draft"), dict) and \
                isinstance(record.get("draftVersion"), int) and \
                not isinstance(record.get("draftVersion"), bool) and \
                record["draftVersion"] > 0:
            pending[key] = record["draftVersion"]
    return pending


def promote_model_setting_drafts(
        state: dict[str, Any], selection: dict[str, int] | None = None
) -> tuple[list[str], list[dict[str, Any]]]:
    """Promote selected drafts to the applied side IN MEMORY (Rev8 契约 6).

    Used on a candidate copy under SyncLock; run_sync publishes the
    promotion in the SAME single transaction as the client writes.
    ``selection`` maps "source|model_id" -> expected draftVersion (the
    preview binding): a stale version raises ContextOverrideConflict; None
    selects every pending draft at its current version.  On success each
    record moves draft→applied (appliedVersion=draftVersion,
    appliedEvidence="applied"), the draft clears, and appliedAt is stamped.
    Nothing is persisted here — a run_sync failure leaves the caller's
    in-memory doc discarded and the on-disk state untouched (草稿保留).

    Returns ``(promoted_keys, skipped)`` — skipped carries the key and a
    single-line reason for records with no pending draft.
    """
    promoted: list[str] = []
    skipped: list[dict[str, str]] = []
    settings = _model_settings_of_state(state)
    keys = sorted(selection) if selection is not None else sorted(
        key for key, record in settings.items()
        if isinstance(record, dict) and isinstance(record.get("draft"), dict))
    for key in keys:
        record = settings.get(key)
        if not isinstance(record, dict) or not isinstance(record.get("draft"),
                                                          dict):
            skipped.append({"key": key, "reason": "无待应用草稿"})
            continue
        expected = (selection or {}).get(key)
        if expected is not None and \
                record.get("draftVersion") != expected:
            raise ContextOverrideConflict(
                "草稿版本冲突（%s）：预览绑定 draftVersion=%s，当前存储=%s"
                % (key, expected, record.get("draftVersion")))
        record["applied"] = dict(record["draft"])
        record["appliedVersion"] = record.get("draftVersion")
        record["appliedAt"] = datetime.now(timezone.utc).isoformat()
        record["appliedEvidence"] = "applied"
        record["appliedSettingEvidence"] = copy.deepcopy(record.get("draftEvidence"))
        record["draft"] = None
        record["draftEvidence"] = None
        promoted.append(key)
        settings[key] = record
    state["modelSettings"] = settings
    return promoted, skipped


def held_model_setting_keys(state: dict[str, Any]) -> set[str]:
    """Keys with a pending settings draft and NO applied value.

    While a draft is pending the mirror HOLDS the model's profile updates
    (定时同步只消费已应用值；草稿未被应用前客户端当前值不被回撤).  A record
    with an applied value is NOT held — the applied side is the published
    state the mirror consumes.
    """
    held: set[str] = set()
    for key, record in _model_settings_of_state(state).items():
        if not isinstance(key, str) or not isinstance(record, dict):
            continue
        if isinstance(record.get("draft"), dict) and \
                record.get("applied") is None:
            held.add(key)
    return held


def apply_context_overrides(
        catalogs: dict[str, list[CatalogModel]],
        state: dict[str, Any]) -> tuple[dict[str, list[CatalogModel]],
                                        list[str]]:
    """Apply manual context overrides at the single run_sync application point.

    Every catalog model with a stored override that PASSES the shared
    predicate ``_override_applies`` gets its context replaced via
    ``dataclasses.replace`` — the SAME effective value and scope then reach
    the catalog mirror (sync preview and apply take the identical path).
    The predicate is exactly what the read face uses, so the two surfaces
    can never disagree: a well-formed override differing from the
    declaration applies even when the declaration is None (200000 != None
    is a real change).  A malformed stored entry is reported, never
    applied; overrides whose model is absent from the current catalogs stay
    inert and visible on the read surface.  Returns (catalogs, notices) —
    notices report each actually-applied override so preview and apply make
    the effect visible (run_sync surfaces them as ``overrideNotices``,
    separate from ``warnings``).
    """
    settings = _model_settings_of_state(state)
    applied_records = {
        key: record for key, record in settings.items()
        if isinstance(record, dict) and isinstance(record.get("applied"), dict)}
    if not applied_records:
        return catalogs, []
    notices: list[str] = []
    effective: dict[str, list[CatalogModel]] = {}
    for source, models in catalogs.items():
        rows: list[CatalogModel] = []
        for model in models:
            levels, origin = _setting_efforts(model, state)
            if levels and not model.efforts:
                model = replace(model, efforts=levels, efforts_source=origin)
            record = applied_records.get(f"{source}|{model.model_id}")
            if record is None:
                rows.append(model)
                continue
            values = record["applied"]
            if values.get("alias"):
                notices.append(
                    "别名已应用：%s|%s 显示名 → %s"
                    % (source, model.model_id, values["alias"]))
                model = replace(model, display_name=values["alias"])
            tokens = values.get("contextTokens")
            if values.get("contextSource") in ("manual", "measured") and \
                    _valid_override_context(tokens) and \
                    tokens != model.context:
                notices.append(
                    "context 已应用：%s|%s → %d（目录声明 %s）"
                    % (source, model.model_id, tokens, model.context))
                model = replace(model, context=tokens)
            effort = values.get("defaultEffort")
            if effort and model.efforts and effort in model.efforts and \
                    effort != model.default_effort:
                notices.append(
                    "默认档已应用：%s|%s → %s" % (source, model.model_id, effort))
                model = replace(model, default_effort=effort)
            rows.append(model)
        effective[source] = rows
    return effective, notices


def context_override_surface(
        state: dict[str, Any],
        catalogs: dict[str, list[CatalogModel]],
        consumers: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Read-only surface: declaration, override, effective value and source,
    PLUS the client readback (Rev8 契约 6：客户端实际读回值不与「已生效」
    混称).  ``consumers`` is the read-only consumer snapshot (the product of
    collect_consumers); when given, every row carries additive fields:
    ``client_context`` (the contextWindow/limit.context the client config
    ACTUALLY holds for the source's managed binding — a read value beyond
    the write-success evidence), ``client_readback_state``
    (ok/absent/unavailable) and ``client_matches_applied`` (readback ==
    applied tokens; None when there is no applied value or the readback is
    unavailable).  An unavailable readback is never disguised as applied,
    and never fabricated as 0.

    Entries cover the union of current-catalog models and stored overrides,
    sorted by key.  ``override_state`` classifies each row: "none" (no
    stored override), "applied" (well-formed override differing from the
    declaration — the ONLY state where the effective value is the
    override), "equal" (well-formed but identical to the declaration — the
    value is not rewritten, effective_source stays "declared"/"unknown"),
    "malformed" (non-dict entry or an illegal stored value — never applied)
    and "model_missing" (the model left the current catalog — inert,
    effective follows the declaration).  INERT is therefore limited to
    exactly two causes: the model left the catalog, or the stored value is
    malformed.  ``override_applied`` and ``effective_source`` come from the
    SAME shared predicate the apply path uses (``_override_applies``), so
    the read face can never disagree with what a sync actually writes — a
    None declaration with a legal override reads applied (effective_context
    = the override value), never inert/unknown.  ``declared_at`` comes from
    the model's probe record when present; ``override_at`` is the stored
    override's ``set_at``.
    """
    # Rev8 契约 6：读面的 override_* 字段来自「已应用」设置（manual/
    # measured）；草稿不是有效值——它以 settings_draft/settings_held 等
    # 加性字段如实呈现，绝不冒充 effective。映射保持 surface 既有判定
    # 逻辑（context/set_at/source 同形）。
    settings = _model_settings_of_state(state)
    held_keys = held_model_setting_keys(state)
    overrides: dict[str, dict[str, Any]] = {}
    for key, record in settings.items():
        if not isinstance(key, str) or not isinstance(record, dict):
            continue
        applied = record.get("applied")
        if isinstance(applied, dict) and applied.get("contextSource") in (
                "manual", "measured"):
            overrides[key] = {"context": applied.get("contextTokens"),
                              "set_at": record.get("appliedAt"),
                              "source": applied.get("contextSource")}
    probes = state.get("contextProbes")
    if not isinstance(probes, dict):
        probes = {}
    rows: dict[str, dict[str, Any]] = {}
    for source, models in (catalogs or {}).items():
        if not isinstance(models, list):
            continue
        for model in models:
            key = f"{source}|{model.model_id}"
            probe = probes.get(key)
            probe = probe if isinstance(probe, dict) else {}
            rows[key] = {
                "source": source,
                "model_id": model.model_id,
                "declared_context": model.context,
                "override_context": None,
                "effective_context": model.context,
                "effective_source": ("declared" if model.context is not None
                                     else "unknown"),
                "declared_at": probe.get("declared_at"),
                "override_at": None,
                "override_applied": False,
                "override_state": "none",
            }
    for key, entry in overrides.items():
        source, _, model_id = str(key).partition("|")
        row = rows.get(key)
        if row is None:
            # 模型不在当前目录：inert 的唯一合法原因之一（另一是值损坏）。
            probe = probes.get(key)
            probe = probe if isinstance(probe, dict) else {}
            row = {"source": source, "model_id": model_id,
                   "declared_context": None, "override_context": None,
                   "effective_context": None, "effective_source": "unknown",
                   "declared_at": probe.get("declared_at"),
                   "override_at": None,
                   "override_applied": False,
                   "override_state": "model_missing"}
            rows[key] = row
            if isinstance(entry, dict):
                row["override_context"] = entry.get("context")
                row["override_at"] = entry.get("set_at")
            continue
        if not isinstance(entry, dict):
            # 条目损坏：读不出 context/set_at，如实标 malformed，绝不应用。
            row["override_state"] = "malformed"
            continue
        row["override_context"] = entry.get("context")
        row["override_at"] = entry.get("set_at")
        if _override_applies(entry, row["declared_context"]):
            row["override_applied"] = True
            row["override_state"] = "applied"
            row["effective_context"] = entry["context"]
            row["effective_source"] = "override"
        elif _valid_override_context(entry.get("context")):
            # 合法但等于声明：值未改写，来源不夸大（declared/unknown 原样）。
            row["override_state"] = "equal"
        else:
            row["override_state"] = "malformed"
    # Rev8 加性字段（B8-3/B8-4）：草稿/已应用元数据与 held 状态逐行可见；
    # 缺记录时字段为 None/false（不虚构值）。settings_held=True 表示该模型
    # 有待应用草稿且无已应用值——镜像按 held 抑制其更新规划。
    for key, record in settings.items():
        if not isinstance(key, str) or "|" not in key:
            continue
        row = rows.get(key)
        if row is None:
            source, _, model_id = key.partition("|")
            probe = probes.get(key)
            probe = probe if isinstance(probe, dict) else {}
            row = {"source": source, "model_id": model_id,
                   "declared_context": None, "override_context": None,
                   "effective_context": None, "effective_source": "unknown",
                   "declared_at": probe.get("declared_at"),
                   "override_at": None, "override_applied": False,
                   "override_state": "model_missing"}
            rows[key] = row
        if not isinstance(record, dict):
            continue
        draft = record.get("draft") if isinstance(record.get("draft"), dict) \
            else None
        applied = record.get("applied") if isinstance(record.get("applied"),
                                                      dict) else None
        # R5 读面归一化（2026-10-10）：草稿/已应用恒为全四键（缺席补 None），
        # 无记录时字段为 None——前端据此回退已应用值；草稿版本恒为非负整数
        # （无记录/垃圾值读作 0），保存的并发检查绝不因缺版本被省略。
        row["settings_draft"] = _normalized_setting_values(draft)
        row["settings_applied"] = _normalized_setting_values(applied)
        row["settings_draft_version"] = _setting_version(
            record.get("draftVersion"))
        row["settings_applied_version"] = _setting_version_or_none(
            record.get("appliedVersion"))
        row["settings_applied_at"] = record.get("appliedAt")
        row["settings_applied_evidence"] = record.get("appliedEvidence")
        row["settings_held"] = key in held_keys
    # Rev8 B8-4 客户端读回（加性）：client_context 是从客户端配置实际读出
    # 的 contextWindow/limit.context——写入成功证据之外的实读值；与已应用
    # 值的比较单列（client_matches_applied），绝不把「已写入」混称「客户端
    # 已生效」。读回不可用（快照缺席）时如实标 unavailable，绝不虚构 0。
    # R6/D4（2026-10-10）加性：published_context/published_known 是本同步
    # 上次实际发布的基准（managed 条目的 published 记录）；context_action 是
    # 三方比较的判定（publish/equal/conflict/unreadable），context_conflict
    # 表示客户端值无法证明是我们发布的、差异待用户裁定（绝不覆盖、绝不删除）。
    for key, row in rows.items():
        source, _, model_id = str(key).partition("|")
        client_context = None
        readback_state = "unavailable" if consumers is None else "absent"
        readback_consumer = None
        if consumers is not None:
            for binding in ACTIVE_BINDINGS:
                if binding.source != source:
                    continue
                consumer = consumers.get(binding.key)
                if not isinstance(consumer, dict):
                    continue
                profiles = consumer.get("models")
                profile = profiles.get(model_id) if isinstance(profiles, dict) else None
                if not isinstance(profile, dict):
                    continue
                context = client_context_of(profile)
                if context is not None:
                    client_context = context
                    row["client_readback_consumer"] = binding.consumer
                    readback_state = "ok"
                    break
        row["client_context"] = client_context
        row.setdefault("client_readback_consumer", None)
        row.setdefault("settings_draft", None)
        row.setdefault("settings_applied", None)
        row.setdefault("settings_applied_version", None)
        row.setdefault("settings_applied_at", None)
        row.setdefault("settings_applied_evidence", None)
        # R5：无记录的行草稿版本归一为 0（「没有草稿记录」的合法读面值），
        # 绝不透出 None 让前端省略并发检查。
        row.setdefault("settings_draft_version", 0)
        row.setdefault("settings_held", False)
        row["client_readback_state"] = readback_state
        applied = row.get("settings_applied")
        applied_tokens = (applied.get("contextTokens")
                          if isinstance(applied, dict) else None)
        row["client_matches_applied"] = (
            (client_context == applied_tokens)
            if (consumers is not None and applied_tokens is not None and
                    client_context is not None) else None)
        readbacks = []
        for binding in ACTIVE_BINDINGS:
            if binding.source != source:
                continue
            consumer = (consumers or {}).get(binding.key)
            profiles = consumer.get("models") if isinstance(consumer, dict) else None
            profile = profiles.get(model_id) if isinstance(profiles, dict) else None
            profile = profile if isinstance(profile, dict) else None
            limits = profile.get("limit") if profile else None
            limits = limits if isinstance(limits, dict) else {}
            reasoning = profile.get("reasoning") if profile else None
            reasoning = reasoning if isinstance(reasoning, dict) else {}
            name = profile.get("name") if profile else None
            alias = (name[:-len(binding.suffix)] if isinstance(name, str) and
                     name.endswith(binding.suffix) else name)
            context = profile.get("contextWindow", limits.get("context")) if profile else None
            output = profile.get("maxTokens", limits.get("output")) if profile else None
            context = context if _valid_override_context(context) else None
            output = output if _valid_override_context(output) else None
            effort = reasoning.get("defaultVariant")
            effort = effort if isinstance(effort, str) else None
            desired = applied if isinstance(applied, dict) else {}
            published = published_record(state, binding, model_id)
            readbacks.append({"consumer": binding.consumer,
                "state": ("unavailable" if consumers is None else "absent" if profile is None else "ok"),
                "contextTokens": context,
                "alias": alias if isinstance(alias, str) else None,
                "defaultEffort": effort,
                "outputTokenCap": {"value": output, "source": "client_config" if output is not None else None},
                # R6/D4 加性：该客户端绑定上次实际发布的上下文基准（None =
                # 无记录；publishedKnown=False）。
                "publishedContext": published_context(published),
                "publishedKnown": isinstance(published, dict),
                "matchesPublished": (context == published_context(published)
                                     if context is not None and
                                     isinstance(published, dict) else None),
                "matchesApplied": {
                    "context": (context == desired["contextTokens"] if context is not None and
                                desired.get("contextTokens") is not None else None),
                    "alias": (alias == desired["alias"] if isinstance(alias, str) and
                              desired.get("alias") is not None else None),
                    "defaultEffort": (effort == desired["defaultEffort"] if effort is not None and
                                      desired.get("defaultEffort") is not None else None)}})
        row["client_readbacks"] = readbacks
        preferred = next((item for item in readbacks if item["consumer"] == "ZCode"),
                         readbacks[0] if readbacks else {})
        row["client_alias"] = preferred.get("alias")
        row["client_default_effort"] = preferred.get("defaultEffort")
        row["client_output_token_cap"] = preferred.get("outputTokenCap", {"value": None, "source": None})
        # R6/D4 行级三方比较（与渲染器/规划器同一实现，绝不各判一套）：
        # target=有效同步值、client=客户端实读值、baseline=上次发布基准。
        # 行级取值沿用读面的既有偏好（ZCode 优先，其次首个有读回的绑定），
        # 与 client_alias/client_default_effort 同一口径；逐消费者真值在
        # client_readbacks 里，绝不把行级值冒充全部客户端。
        preferred_binding = next(
            (binding for binding in ACTIVE_BINDINGS
             if binding.source == source and
             binding.consumer == preferred.get("consumer")),
            next((binding for binding in ACTIVE_BINDINGS
                  if binding.source == source), None))
        preferred_published = (
            published_record(state, preferred_binding, model_id)
            if preferred_binding is not None else None)
        row["published_context"] = published_context(preferred_published)
        row["published_known"] = isinstance(preferred_published, dict)
        row["context_action"] = context_publish_action(
            row.get("effective_context"), preferred.get("contextTokens"),
            preferred_published, client_readable=consumers is not None)
        row["context_conflict"] = row["context_action"] in (
            CONTEXT_CONFLICT, CONTEXT_UNREADABLE)
    return [rows[key] for key in sorted(rows)]


def _save_history(result: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now().astimezone() - timedelta(days=90)
    kept: list[str] = []
    if HISTORY_FILE.exists():
        for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
            try:
                stamp = datetime.fromisoformat(json.loads(line)["timestamp"])
                if stamp >= cutoff:
                    kept.append(line)
            except (ValueError, KeyError, TypeError):
                continue
    kept.append(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    HISTORY_FILE.write_text("\n".join(kept) + "\n", encoding="utf-8")


def _write_result(result: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    temporary = RESULT_FILE.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    # 结果文件同样是「完整替换」：Windows 占用类错误有界重试，耗尽即明确失败。
    replace_with_retry(temporary, RESULT_FILE)
    _save_history(result)


def _write_preview(result: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    temporary = PREVIEW_FILE.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    replace_with_retry(temporary, PREVIEW_FILE)


def _normalize_scope(scope: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate the optional sync scope (the backend write-surface restriction).

    ``sources``/``consumers`` must be non-empty duplicate-free subsets of the
    known names; ``models`` maps source -> selected model ids (non-empty
    strings, at most 64 per source, 256 overall).  Unknown names or shapes
    raise ValueError; the console layer maps that to
    OperationError("invalid_params").  A scope without any restriction is
    normalized to None (plain full sync).

    """
    if scope is None:
        return None
    if not isinstance(scope, dict):
        raise ValueError("scope 必须为对象")
    sources = scope.get("sources")
    if sources is not None:
        if (not isinstance(sources, (list, tuple)) or not sources or
                len(set(sources)) != len(sources) or
                any(source not in ACTIVE_SOURCES for source in sources)):
            raise ValueError("sources 必须为启用来源的非空无重复子集（M365 已暂停）")
        sources = list(sources)
    consumers = scope.get("consumers")
    if consumers is not None:
        if (not isinstance(consumers, (list, tuple)) or not consumers or
                len(set(consumers)) != len(consumers) or
                any(item not in KNOWN_CONSUMERS for item in consumers)):
            raise ValueError("consumers 必须为 DSH/ZCode 的非空无重复子集")
        consumers = list(consumers)
    raw_models = scope.get("models") or {}
    if not isinstance(raw_models, dict):
        raise ValueError("models 必须为 {source: [model_id, ...]} 对象")
    models: dict[str, list[str]] = {}
    total = 0
    for source, ids in raw_models.items():
        if source not in ACTIVE_SOURCES:
            raise ValueError(f"models 含未知或已暂停来源：{source}")
        if sources is not None and source not in sources:
            raise ValueError(f"models 的来源 {source} 不在所选 sources 内")
        if not isinstance(ids, (list, tuple)):
            raise ValueError(f"models[{source}] 必须为 model_id 列表")
        if any(not isinstance(mid, str) or not mid.strip() for mid in ids):
            raise ValueError(f"models[{source}] 含空 model_id")
        if len(ids) > 64:
            raise ValueError(f"models[{source}] 超过每源 64 个上限")
        total += len(ids)
        models[source] = list(ids)
    if total > 256:
        raise ValueError("models 总条目超过 256 上限")
    if not sources and not consumers and not models:
        return None
    return {"sources": sources, "consumers": consumers, "models": models}


def _project_setting_drafts(state, catalogs, selection):
    """Project a version-bound selection without touching the loaded document."""
    projected = copy.deepcopy(state)
    settings = _model_settings_of_state(projected)
    for key, version in selection.items():
        record = settings.get(key)
        if (not isinstance(record, dict) or not isinstance(record.get("draft"), dict)
                or record.get("draftVersion") != version):
            raise ContextOverrideConflict("草稿版本冲突（%s）；请重新预览" % key)
        evidence = _validate_setting_values(_setting_model(catalogs, key), state,
                                            record["draft"])
        saved = record.get("draftEvidence") or {}
        if record["draft"].get("contextSource") == "measured" and \
                saved.get("context") != evidence.get("context"):
            raise ContextOverrideConflict("实测上下文证据已变化（%s）；请重新保存草稿" % key)
        if saved.get("effort") and saved["effort"] != evidence.get("effort"):
            raise ContextOverrideConflict("思考档位证据已变化（%s）；请重新保存草稿" % key)
        record["draftEvidence"] = evidence
    promoted, _ = promote_model_setting_drafts(projected, selection)
    return projected, promoted


def _settings_preview_binding(original, catalogs, consumers, dsh_raw, zcode_raw,
                              bindings, selection, scope):
    """Hash exact publication inputs; no credentials or client contents leave here."""
    rule_hash = None
    if any(binding.consumer == "ZCode" for binding in bindings):
        try:
            rule_hash = hashlib.sha256(ZCODE_PROVIDER_CONFIG.read_bytes()).hexdigest()
        except OSError as exc:
            rule_hash = "unavailable:" + type(exc).__name__
    changes = [{"key": key, "source": key.split("|", 1)[0],
                "model_id": key.split("|", 1)[1],
                "before": copy.deepcopy(original["modelSettings"][key].get("applied")),
                # 草稿按规范四键透出（内部标记如 contextDecided 不进读面/
                # 预览形状）。
                "draft": _normalized_setting_values(
                    original["modelSettings"][key]["draft"])}
               for key in sorted(selection)]
    inputs = {"scope": scope, "selection": selection,
              "applied_settings": original.get("modelSettings", {}),
              "records": {key: original["modelSettings"][key] for key in selection},
              "catalogs": {source: [asdict(m) for m in models]
                           for source, models in catalogs.items()},
              "consumers": consumers, "dsh": dsh_raw, "zcode": zcode_raw,
              "rule_hash": rule_hash,
              "evidence": {key: (original.get("contextProbes") or {}).get(key)
                           for key in selection},
              # 目标集合（本轮参与的绑定）也参与绑定（§11.3.5）：客户端或
              # provider 集合变化必须让旧 token 失效，绝不只绑草稿版本。
              "targets": [{"key": binding.key, "source": binding.source,
                           "consumer": binding.consumer,
                           "provider": binding.provider,
                           "provider_name": binding.provider_name}
                          for binding in bindings]}
    token = hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"selection": dict(selection), "preview_token": token, "changes": changes}


def run_sync(apply: bool, run_probes: bool, confirm: bool,
             scope: dict[str, Any] | None = None,
             state_override: dict[str, Any] | None = None,
             settings_selection: dict[str, int] | None = None,
             preview_token: str | None = None,
             bind_preview: bool = False,
             before_publish=None) -> dict[str, Any]:
    """Run one sync round; ``scope`` optionally restricts the write surface.

    ``bind_preview`` binds ordinary previews as well as selected drafts to the
    current state, participating client files and applied settings. A supplied
    token is checked before any publication. Legacy unbound calls remain valid.

    scope keys: ``sources`` (subset of ``um`` — retired sources are
    rejected and never fetched, and plan no changes), ``consumers``
    (subset of DSH/ZCode; unselected clients are never planned) and ``models``
    ({source: [model_id, ...]} — per-source add/update/remove limited to the
    selected ids, provider-name changes suppressed for filtered sources,
    retirement evidence only accumulated/confirmed inside the selection).
    The applied-path result carries additive appliedDetails/pendingDetails
    (structured mirrors of applied/pending changes) and pendingRetirement
    (catalog-missing retirement observations, separate from the config-pending
    ``pending`` list).  Manual context override effects have their own
    additive ``overrideNotices`` list (present on preview and apply alike,
    separate from ``warnings``); the probe seeding reads the pre-override
    catalog snapshot, so an override never leaks into ``declared_context``
    nor resets verified probe evidence.

    Consumer collection follows the selected consumers (2026-09-30 gap): only
    the client files of participating bindings are opened, so a ZCode-only run
    never reads the retired DSH settings file.  When those settings are absent
    (the retired steady state), the unscoped default run skips the DSH side
    silently; when the file exists but is unreadable it skips with a warning;
    an explicitly selected DSH consumer is in both cases an explicit error —
    never an empty snapshot.  A managed provider missing from the rule store
    is registered transactionally in the same write batch (providerOrder,
    providerRules, model whitelist/order, per-model rules); the registration
    is reported as an applied provider_registration item instead of a silent
    skip.

    ``settings_selection`` projects exact draft versions on a candidate
    copy of ``state_override`` after validating their supporting evidence.
    Preview binds this candidate, the catalog and client snapshots to a
    token; application rejects a changed token before any publication.
    Settings and client files publish in one transaction. If one client
    defers, the whole settings group retains its old applied state and new
    drafts. The normal/scheduled path consumes APPLIED settings only.
    """
    started = datetime.now().astimezone()
    scope = _normalize_scope(scope)
    sources = scope.get("sources") if scope else None
    selected_consumers = scope.get("consumers") if scope else None
    model_filter = ({source: set(ids) for source, ids in scope["models"].items()}
                    if scope and scope["models"] else None)
    state = load_state() if state_override is None else state_override
    original_state = copy.deepcopy(state)
    settings_promoted: list[str] = []
    settings_deferred: list[str] = []
    settings_preview = None
    catalogs, source_warnings = collect_catalogs(sources=sources)
    # R6/D4：用户显式决定过上下文的模型（settings_selection 里带
    # contextDecided 的草稿）——该决定即裁定，解除「客户端冲突不覆盖」的
    # 保留；未显式决定的模型仍按三方比较保留并进预览待裁定。
    decided_context: set[str] = set()
    if settings_selection:
        selection_records = _model_settings_of_state(state)
        for key in settings_selection:
            record = selection_records.get(key)
            if isinstance(record, dict) and \
                    record.get("contextDecided") is True:
                decided_context.add(key)
    if settings_selection is not None:
        if not settings_selection:
            raise ValueError("设置应用选择不能为空")
        seed_context_probes(catalogs, state, model_filter)
        state, settings_promoted = _project_setting_drafts(state, catalogs,
                                                          settings_selection)
    # 人工 context 覆盖在唯一应用点生效（Plan-Revision 7 第 6 步）：镜像
    # （及共享同一路径的预览与 apply）使用同一有效值与同一范围。探测播种
    # 刻意使用覆盖前的原始目录快照——覆盖绝不写进探测记录的
    # declared_context，也绝不重置已 verified 的记录（set→clear 周期零重
    # 测、零归档、零计费）。覆盖生效通知单独成通道（overrideNotices），
    # 不再混进 source_warnings。
    raw_catalogs = catalogs
    # Rev8 契约 6：唯一应用点只消费已应用值（alias/context/effort）；草稿
    # 绝不在此生效——held 草稿抑制镜像更新规划，客户端当前值不被回撤。
    catalogs, override_notices = apply_context_overrides(catalogs, state)
    held_keys = held_model_setting_keys(state)
    # 草稿清理（2026-10-11）：模型已不在本轮有效目录里的草稿永远发布不出去（发布
    # 路径要求模型在目录内），却会把整轮同步预览顶失败（invalid_params：模型不在
    # 当前目录）。这里在「来源本轮目录有效」的前提下如实列出这些草稿，并在应用时
    # 清理记录；来源本轮未采集或目录无效时一律不动——那是「不知道」，不是「模型
    # 已消失」，绝不用一次目录读取失败去清用户的草稿。
    stale_draft_keys: list[str] = []
    settings_doc = _model_settings_of_state(state)
    catalog_counts = state.get("catalogCounts") or {}
    for key in sorted(settings_doc):
        record = settings_doc[key]
        source, _, model_id = key.partition("|")
        # 范围（§11.3「范围外草稿保留」/ run_sync(scope.models) 只改选中模型）：
        # 范围外来源与范围外模型的草稿一律保留，绝不进入清理计划——前端按范围
        # 筛选，后端必须用同一范围，否则用户只同步 A 也会被删掉 B 的草稿。
        if sources is not None and source not in sources:
            continue
        selected_models = (model_filter or {}).get(source)
        if selected_models is not None and model_id not in selected_models:
            continue
        source_models = catalogs.get(source)
        if source_models is None:
            continue
        valid, _ = validate_catalog(source, source_models,
                                    catalog_counts.get(source))
        if not valid:
            continue
        if (not isinstance(record, dict)
                or not isinstance(record.get("draft"), dict)
                or record.get("applied") is not None):
            continue
        if any(model.model_id == model_id for model in source_models):
            continue
        stale_draft_keys.append(key)
    bindings = [
        binding for binding in ACTIVE_BINDINGS
        if (sources is None or binding.source in sources) and
           (selected_consumers is None or
            binding.consumer in selected_consumers)]
    # Collect per the selected consumers: only the client files of bindings
    # that actually participate are opened.  A ZCode-only run never touches
    # the DSH settings file (2026-09-30 gap).  When the DSH settings turn out
    # to be absent/unreadable: an explicitly selected DSH consumer is an
    # honest error; the unscoped default run skips the DSH side with a
    # warning.  Both paths refuse to fold the absence into an empty snapshot
    # (the mirror would misread it as "the client holds zero models" and plan
    # mass retirements).
    consumers, dsh_raw, zcode_raw = collect_consumers(
        selected_consumers=sorted({binding.consumer for binding in bindings}))
    if dsh_raw is None and any(
            binding.consumer == "DSH" for binding in bindings):
        if selected_consumers is not None and "DSH" in selected_consumers:
            raise ValueError(
                "所选 DSH 消费方的 settings.yaml 缺席或不可读（%s）；"
                "不折叠成空配置" % DSH_YAML)
        if DSH_YAML.exists():
            # 文件在场但读不出来：真实的本轮异常，照旧告警并跳过。
            source_warnings.append(
                "DSH settings.yaml 存在但不可读，本轮跳过 DSH 侧同步")
        # else: settings.yaml 已退役（2026-09-30 起 DSH 侧下线，文件不再
        # 部署），缺席是常态而非本轮异常——静默跳过 DSH 侧，不再把每轮
        # 默认运行钉成 warning/退出码 2（计划任务会因此每次运行都弹通知）。
        # 文件若重新出现会照常被读取，无需登记。
        bindings = [binding for binding in bindings
                    if binding.consumer != "DSH"]
    # The write surface of a managed provider is its whole model surface: the
    # client's model map PLUS its rule store.  Merge the rule-store ids into the
    # consumer snapshots so an id that only the rule store still holds (the
    # reported model-a / cn-b residue) is planned as a retirement too instead of
    # being preserved forever as a manual extra.
    rule_surface, rule_surface_problem = _zcode_rule_surface()
    _attach_rule_surface(consumers, bindings, rule_surface)
    # A live client may have removed a managed provider since the previous
    # sync.  Do not turn a missing provider into an empty one and then fail
    # the entire default run at render time.  Explicitly selected sources
    # still fail with a precise, actionable error.
    #
    # 受管但缺失（ZCODE_MANAGED_PROVIDER_DEFAULTS 有创建模板）的 provider
    # 不在这里跳过：它照常进入预览与事务写入路径，由 render_zcode_sync 用
    # 模板创建，已存在的 provider 不动。
    # 无模板的缺失 provider 才按上面的语义跳过或报错。
    zcode_providers = (
        json.loads(zcode_raw).get("provider") or {}
    ) if zcode_raw is not None else {}
    missing = [
        binding for binding in bindings
        if binding.consumer == "ZCode"
        and binding.source in catalogs
        and binding.provider not in zcode_providers
        and binding.provider not in ZCODE_MANAGED_PROVIDER_DEFAULTS
    ]
    if missing:
        labels = ", ".join(binding.provider for binding in missing)
        if sources is not None or any(
                binding.source in (model_filter or {}) for binding in missing):
            raise ValueError(f"ZCode 配置缺少所选来源 provider：{labels}")
        source_warnings.extend(
            f"ZCode 配置缺少 {binding.provider}，本轮跳过该来源的 ZCode 同步"
            for binding in missing)
        bindings = [binding for binding in bindings if binding not in missing]
    if settings_selection is not None or bind_preview:
        settings_preview = _settings_preview_binding(
            original_state, raw_catalogs, consumers, dsh_raw, zcode_raw,
            bindings, settings_selection or {}, scope)
        if apply and preview_token != settings_preview["preview_token"]:
            raise ContextOverrideConflict("预览已过期：草稿、目录证据或客户端配置已变化；请重新预览")
        if not bindings:
            raise ValueError("没有可发布设置的客户端绑定")
    initial_plan = plan_catalog_mirror(
        catalogs, consumers, state, bindings=bindings,
        model_filter=model_filter, held=held_keys,
        context_decided=decided_context)
    candidate_ids = _new_model_candidates(initial_plan, state)
    probe_records = state.setdefault("probes", {})
    for source, model_ids in candidate_ids.items():
        if sources is not None and source not in sources:
            continue
        for model_id in model_ids:
            if (model_filter is not None and source in model_filter and
                    model_id not in model_filter[source]):
                continue
            probe_records.setdefault(
                f"{source}|{model_id}", {
                    "attempts": 0, "status": "pending", "newModel": True})
    for key, record in list(probe_records.items()):
        try:
            source, model_id = key.split("|", 1)
        except ValueError:
            continue
        if (source in candidate_ids and record.get("status") == "pending" and
                record.get("newModel") is True and
                (sources is None or source in sources) and
                (model_filter is None or source not in model_filter or
                 model_id in model_filter[source])):
            candidate_ids[source].add(model_id)
    if run_probes:
        source_warnings.extend(probe_new_models(
            catalogs, state, candidate_ids))
    # 探测播种读覆盖前的原始声明：declared_context 记录目录的说法，人工
    # 覆盖只在镜像侧生效。
    seed_context_probes(raw_catalogs, state, model_filter)
    # The authoritative plan is derived ONCE from the original state (plus the
    # seeded probe records): re-planning from a mutated next_state would lose
    # retirement confirmations whose watch entry the first pass already
    # consumed.  Both passes see the same inputs, so retirement evidence and
    # confirmations are deterministic.
    plan = plan_catalog_mirror(
        catalogs, consumers, state, bindings=bindings,
        model_filter=model_filter, held=held_keys,
        context_decided=decided_context)
    # 「目录缺失待退役」与「配置待应用」分开呈现：受管模型第一次缺失只登记
    # 观察（第二次独立目录证据才产生退役变化），它必须作为警告/观察项出现，
    # 不能被「无配置变化」当成功。
    pending_retirement = _pending_retirement(plan, bindings)
    warnings = source_warnings + plan.warnings + [
        item["description"] for item in pending_retirement]
    # R6/D4 客户端上下文冲突（2026-10-10）：冲突不覆盖且进预览待裁定——
    # 单独成通道（contextConflicts），绝不混进 warnings（不是本轮异常），
    # 也绝不冒充「待应用配置」。
    context_conflicts = [
        dict(conflict, description=_context_conflict_line(conflict))
        for conflict in plan.context_conflicts]
    if settings_preview is not None:
        # R6/D4 设置预览逐项带上下文裁定证据：冲突不覆盖时列出两边值与
        # 本次采用的边（用户显式决定过上下文=console，否则 pending）。
        conflict_by_key = {
            "%s|%s" % (item["source"], item["model_id"]): item
            for item in context_conflicts}
        for change in settings_preview["changes"]:
            conflict = conflict_by_key.get(change["key"])
            if conflict is None:
                continue
            change["context_conflict"] = {
                "client": conflict["client"],
                "target": conflict["target"],
                "baseline": conflict["baseline"],
                "baseline_recorded": conflict["baseline_recorded"],
                "action": conflict["action"],
                "resolution": ("console" if conflict["decided"]
                               else "pending"),
            }
    if rule_surface_problem:
        warnings.append(rule_surface_problem)
    store_problem = _rule_store_problem()
    if (any(change.binding.consumer == "ZCode" for change in plan.changes)
            and store_problem):
        # R20-2：规则库不可用（缺席／不可读／无法解析）时本轮没有规则库写面，
        # 单独写主配置就是半份配置。整个 ZCode 写集合延期（见 apply 路径），
        # 这里先如实报出原因；绝不从零合成规则库文档（既有边界不变）。
        warnings.append(
            "ZCode 供应商规则库%s（%s）：本轮不写主配置 config.json，"
            "ZCode 侧写集合整体延期——写半份配置会让新供应商/模型在 "
            "picker 不可见" % (store_problem, ZCODE_PROVIDER_CONFIG))

    # Registration inputs: the provider's own config.json options (parsed
    # once above) drive the connection a rule-store-missing managed
    # provider is registered with; the managed creation template fills
    # what the provider entry itself lacks.
    provider_options = {
        provider_id: (provider.get("options")
                      if isinstance(provider, dict) else None)
        for provider_id, provider in zcode_providers.items()}
    # 预览必须反映应用路径的登记检测（同一只读计算）：20:44→21:00 形态
    # （provider 已在 config.json、规则库缺席、无模型变化）在计划里没有任何
    # 变化，应用时却会登记供应商——预览绝不能读成「无配置变化」。连接不可
    # 解析时预览不崩溃，同一条错误降级为预览警告（应用路径在 ZCode 未运行
    # 时仍按显式契约硬失败）。
    preview_candidate: dict[Path, str] = {}
    preview_registered: list[str] = []
    preview_lines: list[str] = []
    if not apply:
        if store_problem:
            # R20-2：规则库不可用时应用路径会把整个 ZCode 写集合延期——
            # 预览必须与之一致，不能读成「应用时会写」。
            if any(change.binding.consumer == "ZCode"
                    for change in plan.changes):
                preview_lines.append(
                    "（应用时）ZCode 侧写集合整体延期：供应商规则库%s"
                    % store_problem)
        else:
            try:
                _, preview_candidate, preview_registered = \
                    _provider_config_candidate(
                        catalogs, plan, state, model_filter, bindings,
                        provider_options)
            except ValueError as exc:
                preview_candidate, preview_registered = {}, []
                warnings.append(
                    "ZCode 供应商规则库登记候选解析失败（预览口径）：%s" % exc)
            for provider in preview_registered:
                preview_lines.append(
                    "（应用时）ZCode 规则库登记供应商 %s"
                    "（providerOrder/providerRules/模型白名单与规则）" % provider)
            if preview_candidate and not preview_registered:
                preview_lines.append(
                    "（应用时）ZCode 供应商规则库将对齐（provider_config.json）")

    print("模型目录同步计划：")
    for change in plan.changes:
        print("  - " + change.description)
    for notice in override_notices:
        # 覆盖生效值塑造了本轮计划：CLI 读者也看得见（与 warnings 分开）。
        print("  - " + notice)
    for conflict in context_conflicts:
        # R6/D4 冲突不覆盖：CLI 读者看得到「待裁定」与两边值（与 warnings
        # 分开，绝不冒充已发布或已延期）。
        print("  [待裁定] " + conflict["description"])
    for line in preview_lines:
        print("  - " + line)
    for warning in warnings:
        print("  [警告] " + warning)
    if not plan.changes and not preview_lines and not context_conflicts:
        print("  无配置变化。")

    should_apply = apply
    if confirm and plan.changes:
        should_apply = input("应用上述变化？[y/N]: ").strip().lower() == "y"

    next_state: dict[str, Any] | None = None
    applied_changes = []
    pending_changes = []
    applied_details = []
    pending_details = []
    deferred_zcode: list[Any] = []
    deferred_clients: list[Any] = []
    dsh_changed = False
    zcode_changed = False
    # 本批实际替换成功、已落盘的文件（发布失败时为失败前的部分集合）。
    published_paths: list[Path] = []
    if should_apply:
        dsh_changes = [
            change for change in plan.changes if change.binding.consumer == "DSH"]
        zcode_changes = [
            change for change in plan.changes if change.binding.consumer == "ZCode"]
        # Rule-store alignment is planned independently of the main config: a
        # rule-store orphan (the reported model-a / cn-b residue) produces a
        # retirement change even though config.json itself has nothing to change,
        # so the candidate must be computed before the running-client gate and
        # must not be dropped when config.json is unchanged.
        zcode_live = zcode_running()
        # R27-HOT 运行中同步：运行中不再一律延期——先做安装版兼容准入
        # （证据表见 zcode_hot_sync）。已准入时本轮 ZCode 写入真的发生：
        # 经客户端锁协议发布 provider_config.json（单文件权威）；未准入
        # （未取证/升级/降级/文件变化/读不到）时整体延期，fail closed。
        if zcode_live:
            hot_admitted, hot_detail = zcode_hot_sync_admission()
        else:
            hot_admitted, hot_detail = False, ""
        defer_zcode_write = zcode_live and not hot_admitted
        # Rev11（R27-HOT 扩展）：计划基线的规则库文本；真正写入时（含
        # ZCode 运行中但已准入的轮次）会在锁内重读并只合并本轮受管范围
        # （见下方 store_lock）。
        plan_store_raw: str | None = None
        if store_problem:
            # R20-2: the rule store cannot take this round's write at all
            # (absent, unreadable or unparsable) and its document is never
            # synthesized from scratch.  No candidate is computed here — a
            # malformed store would raise inside the renderer and abort the
            # whole round, including the unrelated DSH side.
            provider_candidate, registered_providers = {}, []
        elif defer_zcode_write:
            # When this round's ZCode write is deferred (ZCode running and the
            # installed build not admitted), a candidate whose connection
            # resolution fails (rule block missing AND the connection
            # unresolvable, an unreadable gateway key, a malformed store)
            # must degrade into a warning instead of aborting the whole round -
            # the DSH side and the state write are unrelated to it.  When the
            # write will really happen (ZCode not running, or running with an
            # admitted installed build) the raise keeps the explicit contract:
            # a half-registerable provider still refuses the round.
            try:
                plan_store_raw, provider_candidate, registered_providers = \
                    _provider_config_candidate(
                        catalogs, plan, state, model_filter, bindings,
                        provider_options)
            except ValueError as exc:
                plan_store_raw, provider_candidate, registered_providers = \
                    None, {}, []
                warnings.append(
                    "ZCode 供应商规则库登记候选解析失败（本轮 ZCode 运行中，"
                    "写入已推迟）：%s" % exc)
        else:
            plan_store_raw, provider_candidate, registered_providers = \
                _provider_config_candidate(
                    catalogs, plan, state, model_filter, bindings,
                    provider_options)
        defer_reasons: list[str] = []
        settings_zcode = bool(settings_promoted and any(
            binding.consumer == "ZCode" for binding in bindings))
        if (zcode_changes or provider_candidate or settings_zcode) and \
                defer_zcode_write:
            # ZCode running without admission (unverified or changed install):
            # the write would really happen, so it must not be hot-written.
            # Both the model changes and the rule-store alignment/registration
            # are registered as pending instead.  Running with an admitted
            # installed build publishes provider_config.json under the client
            # lock protocol (single-file authority) below.
            defer_reasons.append(
                "ZCode 运行中（或运行状态无法确认，按运行中处理）：客户端配置不热写"
                "（%s）" % hot_detail)
        if (zcode_changes or provider_candidate or settings_zcode) and store_problem:
            # R20-2: the rule store cannot take this round's write (absent,
            # unreadable or unparsable) and its document is never synthesized
            # from scratch.  Writing config.json alone publishes models the
            # picker cannot see - the exact "written but invisible" defect - so
            # the WHOLE ZCode write set is deferred here: nothing under the
            # client is written, every change stays pending with its reason, and
            # no binding is reported as synced.  Other consumers (DSH) keep
            # their existing boundary; a later run with a usable store applies
            # the same complete transaction.
            #
            # Rev11 迁移门（唯一例外）：规则库缺席时，客户端会在启动时一次性
            # 导入 config.json——但只有在迁移兼容性已取证（本机安装版本仍走该
            # 导入路径）时才允许写它；「缺席」本身绝不构成写面，绝不把
            # config.json 当第二个实时权威。
            if not _legacy_config_write_allowed(store_problem):
                reason = ("供应商规则库%s（%s）不可用，不写半份配置"
                          % (store_problem, ZCODE_PROVIDER_CONFIG))
                if store_problem == "文件缺席":
                    reason += ("；旧版 config.json 迁移兼容性未取证"
                               "（Rev11 迁移门），不以「缺席就写」替代迁移条件")
                defer_reasons.append(reason)
        # Rev11 协作锁（与客户端 atomicFileLock.ts 同一协议）：真正会写
        # provider_config.json 时（含 ZCode 运行中但安装版已准入的轮次），整个
        # 「锁内重读 → 只合并受管范围 → 原子替换」窗口都在锁内完成；锁只覆盖
        # 同步写入窗口，不长期持有。锁内重读发现受管范围指纹变化（并发编辑/
        # 版本冲突）时，本轮 ZCode 写集合整体退回 pending——绝不覆盖并发编辑。
        store_lock = (
            zcode_file_lock(ZCODE_PROVIDER_CONFIG)
            if provider_candidate and not defer_reasons and not store_problem
            else contextlib.nullcontext())
        with store_lock:
            # 锁内冲突：即使本轮只有规则库对齐（无模型变更）也要让延期
            # 分支运行，否则「为什么没写」会丢失。
            store_conflict: str | None = None
            if provider_candidate and not defer_reasons and not store_problem:
                conflict, merged_candidate, merged_registered = \
                    _locked_store_merge(
                        catalogs, plan, state, model_filter, bindings,
                        provider_options, plan_store_raw)
                if conflict is None:
                    provider_candidate = merged_candidate
                    registered_providers = merged_registered
                else:
                    store_conflict = conflict
                    defer_reasons.append(conflict)
                    provider_candidate, registered_providers = {}, []
            if defer_reasons and (zcode_changes or provider_candidate
                                   or settings_zcode or store_conflict):
                # Both causes can hold at once (ZCode running AND the store
                # unusable): the reason line names every cause that actually
                # applies, so the persisted pending record never loses one of them.
                deferred_zcode = list(zcode_changes)
                deferred_clients = list(zcode_changes)
                if settings_promoted:
                    # A model has one published settings record. Do not publish
                    # one consumer while another consumer defers that same record.
                    settings_deferred = list(settings_promoted)
                    deferred_clients.extend(dsh_changes)
                    pending_changes.extend(change.description for change in dsh_changes)
                    pending_details.extend({"consumer": change.binding.consumer,
                        "kind": change.kind, "model_id": change.model_id,
                        "description": change.description} for change in dsh_changes)
                    dsh_changes = []
                pending_changes.extend(change.description for change in zcode_changes)
                pending_details.extend({
                    "consumer": change.binding.consumer,
                    "kind": change.kind,
                    "model_id": change.model_id,
                    "description": change.description,
                } for change in zcode_changes)
                if provider_candidate:
                    pending_changes.append(
                        "ZCode 供应商规则库（provider_config.json）随模型变更待对齐")
                reason_line = (
                    "ZCode 侧写集合整体延期（%s）：本轮不写 config.json / "
                    "provider_config.json，待条件就绪后按完整事务重跑"
                    % "；".join(defer_reasons))
                pending_changes.append(reason_line)
                # 延期原因也要进结构化镜像：只读 pendingDetails 的消费方同样
                # 能看到「为什么这一轮没写」。
                pending_details.append({
                    "consumer": "ZCode",
                    "kind": "provider_write_deferred",
                    "model_id": None,
                    "description": reason_line,
                })
                zcode_changes = []
                provider_candidate = {}
                registered_providers = []
            candidates: dict[Path, str] = {}
            if dsh_changes:
                candidates[DSH_YAML] = render_dsh_sync(dsh_raw, dsh_changes)
                dsh_changed = True
            if zcode_changes and _legacy_config_write_allowed(store_problem):
                # Rev11 单文件发布：新版权威文件在场时，ZCode 模型变更只经
                # provider_config.json 的规则面发布，config.json 不是第二个实时
                # 权威，本轮不写。只有规则库缺席的旧版本迁移路径（迁移门开启）
                # 才写它——客户端启动时一次性导入。
                rendered = render_zcode_sync(zcode_raw, zcode_changes)
                # A rule-store-only retirement leaves config.json byte-identical
                # (nothing to pop): never rewrite a client file without a change.
                if json.loads(rendered) != json.loads(zcode_raw):
                    candidates[ZCODE_JSON] = rendered
            if provider_candidate:
                candidates.update(provider_candidate)
            zcode_changed = ZCODE_JSON in candidates or \
                ZCODE_PROVIDER_CONFIG in candidates
            next_state = plan.next_state
            if settings_deferred:
                for key in settings_deferred:
                    next_state["modelSettings"][key] = copy.deepcopy(
                        original_state["modelSettings"][key])
            if stale_draft_keys:
                # 预览已如实列出（plan 里「清理草稿」行）：应用时连同记录一起清理。
                # 只清「模型已不在本轮有效目录」的纯草稿；来源未采集/目录无效的
                # 草稿一律保留（见上面的计算前提）。结果回报在 applied 重建之后。
                for key in stale_draft_keys:
                    next_state["modelSettings"].pop(key, None)
            if deferred_clients:
                # 延期轮没有把任何东西写进客户端，就不该把该绑定标成「早先一次
                # 同步已发布」（受管登记表的自身语义）：add/update 的受管条目回退
                # 到本轮之前的状态，下一次可用时重跑同一计划。已确认退役的墓碑
                # （remove）保持计划后状态——它的语义是「待应用」而不是「已同步」。
                managed_after = next_state.setdefault("managed", {})
                managed_before = state.get("managed") or {}
                for change in deferred_clients:
                    if change.kind not in ("add", "update") \
                            or change.model_id is None:
                        continue
                    entries = managed_after.get(change.binding.key)
                    if not isinstance(entries, dict):
                        continue
                    previous = managed_before.get(change.binding.key)
                    if isinstance(previous, dict) and change.model_id in previous:
                        entries[change.model_id] = previous[change.model_id]
                    else:
                        entries.pop(change.model_id, None)
                    if not entries and change.binding.key not in managed_before:
                        # 本轮之前该绑定没有任何受管条目：不要留下一个空壳
                        # 登记项，让「已发布」的读面保持干净。
                        managed_after.pop(change.binding.key, None)
            # R6/D4 发布基准（2026-10-10）：真正写入的 add/update 在同一个
            # 状态事务里登记「我们到底发布了什么」（按候选写后读回）；延期
            # 的绑定已整体回退到本轮之前的条目，基线不会前进。remove 的
            # 受管条目在同一事务里被删除，无需登记。
            for key, published in _published_contexts(
                    candidates, dsh_changes + zcode_changes).items():
                entries = next_state.setdefault("managed", {}).get(key)
                if not isinstance(entries, dict):
                    continue
                for model_id, context in published.items():
                    entry = entries.get(model_id)
                    if isinstance(entry, dict):
                        entry["published"] = {
                            "context": context,
                            "at": started.isoformat(),
                        }
            # Applied removals drop their tombstones in the same transaction: the
            # state file lands together with the consumer configs, so a "retired"
            # managed entry whose removal was actually applied must not survive —
            # otherwise the change leaves the books and the config entry could
            # never be cleaned again. Pending (ZCode running) tombstones stay.
            for change in dsh_changes + zcode_changes:
                if change.kind != "remove" or change.model_id is None:
                    continue
                entries = next_state.get("managed", {}).get(change.binding.key)
                if isinstance(entries, dict):
                    entry = entries.get(change.model_id)
                    if isinstance(entry, dict) and entry.get("retired") is True:
                        entries.pop(change.model_id, None)
            next_state["pending"] = pending_changes
            next_state["lastRun"] = started.isoformat()
            next_state["lastStatus"] = (
                "pending" if pending_changes else
                "changed" if dsh_changes or zcode_changes or provider_candidate else
                "warning" if warnings else "normal")
            candidates[STATE_FILE] = json.dumps(
                next_state, ensure_ascii=False, indent=2) + "\n"
            if before_publish is not None:
                before_publish()
            published_paths = apply_config_transaction(
                candidates, BACKUP_DIR, keep_backups=30)
            # 写后读回：事务的逐文件原子替换成功不代表最终内容可证明（例如
            # 客户端进程可能在替换后重写了文件）。读回范围就是本批的 ZCode 侧
            # 文件（config.json / provider_config.json / 状态文件），DSH 配置的
            # 渲染与写入语义不在本次范围。任何不一致按失败上报；消息只含
            # 文件名，不含配置内容或密钥。这里不做回退——读回不一致说明文件
            # 已被并发写者改写，版本校验必然不通过，绝不盖回旧快照；恢复/
            # 对账路径是重跑同步，按当前客户端文件与目录重新规划。
            readback = _readback_problems({
                path: content for path, content in candidates.items()
                if path != DSH_YAML})
            if readback:
                raise ConfigPublishError(
                    "写入后读回校验失败：" + "；".join(
                        problem for _, problem in readback) +
                    "；文件已替换但内容不可证明（可能被并发写者改写）；"
                    "不做回退（版本校验不通过，绝不盖回旧快照）；"
                    "恢复/对账路径：重跑 python model_sync/sync.py --apply，"
                    "按当前客户端文件与目录重新规划",
                    failed_path=readback[0][0], published=published_paths,
                    left_in_place=[path for path, _ in readback])
        applied_changes = [
            change.description for change in dsh_changes + zcode_changes]
        applied_details = [{
            "consumer": change.binding.consumer,
            "kind": change.kind,
            "model_id": change.model_id,
            "description": change.description,
        } for change in dsh_changes + zcode_changes]
        # A rule-store registration (2026-09-30 Gemini gap) is a real applied
        # write even when the plan itself had no model change: report it as an
        # applied item instead of claiming "no changes" while the client files
        # moved.  The description names the surfaces; it never echoes the
        # connection credential.
        for provider in registered_providers:
            line = ("ZCode 规则库登记供应商 %s"
                    "（providerOrder/providerRules/模型白名单与规则）" % provider)
            applied_changes.append(line)
            applied_details.append({
                "consumer": "ZCode",
                "kind": "provider_registration",
                "model_id": None,
                "description": line,
            })
            print("  - " + line)
        # 草稿清理（2026-10-11）：模型已不在目录的草稿在应用时被清理，必须在
        # applied 里如实回报（预览的 plan 已列出同一行）；绝不把这次清理读成
        # 「无变化」。
        for key in stale_draft_keys:
            line = "清理草稿 %s（模型已不在目录，草稿无法发布）" % key
            applied_changes.append(line)
            applied_details.append({
                "consumer": "model",
                "kind": "stale_draft_cleanup",
                "model_id": key.partition("|")[2],
                "description": line,
            })
            print("  - " + line)
        if provider_candidate and not applied_changes:
            # A rule-store-only write (order backfill or whitelist alignment)
            # with no model change is still a real client-file write: report
            # it instead of letting the result read as "无配置变化".
            line = "ZCode 供应商规则库已对齐（provider_config.json）"
            applied_changes.append(line)
            applied_details.append({
                "consumer": "ZCode",
                "kind": "provider_rule_store",
                "model_id": None,
                "description": line,
            })
            print("  - " + line)
    # 已确认退役、但客户端配置还没写入（ZCode 运行中）的条目：与观察项一起
    # 放进 pendingRetirement，绝不能只出现在 applied 里当成功。
    for change in deferred_zcode:
        if change.kind != "remove" or change.model_id is None:
            continue
        pending_retirement.append({
            "consumer": change.binding.consumer,
            "provider": change.binding.provider,
            "model_id": change.model_id,
            "state": "pendingApply",
            "evidenceRuns": 2,
            "firstMissing": None,
            "description": "（待应用）" + change.description,
        })

    status = "preview"
    if should_apply:
        if pending_changes:
            status = "pending"
        elif applied_changes:
            status = "changed"
        elif warnings:
            status = "warning"
        else:
            status = "normal"
    settings_readback = None
    if settings_selection is not None:
        # §11.3.5 读回：状态面（已应用/草稿）与客户端实际值分开给出。
        # 应用轮重新只读采集一次本轮参与客户端的配置（写后读回），读取失败
        # 如实标 unavailable，绝不把「已写入」冒充「客户端已生效」；预览轮
        # 用本轮已采集的快照（零写入），不重新读。
        readback_consumers = consumers
        if should_apply:
            try:
                readback_consumers, _, _ = collect_consumers(
                    selected_consumers=sorted(
                        {binding.consumer for binding in bindings}))
            except Exception:
                readback_consumers = None
        committed_state = next_state if should_apply and \
            next_state is not None else state
        selected_keys = set(settings_selection)
        rows = [
            row for row in context_override_surface(
                committed_state, catalogs, consumers=readback_consumers)
            if f"{row['source']}|{row['model_id']}" in selected_keys]
        settings_readback = {
            "keys": rows,
            "clientReadbackAvailable": readback_consumers is not None,
            "filesVerified": (not readback) if should_apply else None,
            "note": ("读回：状态面（已应用/草稿）与客户端实际值分列；"
                     "客户端读回失败时如实标 unavailable，绝不把「已写入」"
                     "冒充「客户端已生效」；客户端实际值也可经 "
                     "model.context_overrides 读面复核"),
        }
    result = {
        "timestamp": started.isoformat(),
        "code_stamp": CODE_STAMP,
        "status": status,
        "applied": applied_changes,
        "pending": pending_changes,
        # Additive structured mirrors of applied/pending changes (the plain
        # string lists stay for compatibility).
        "appliedDetails": applied_details,
        "pendingDetails": pending_details,
        # Additive: 目录缺失待退役 / 已确认退役待应用的条目，与「配置待应用」
        # （pending）分开呈现。state=watching 是只登记观察、尚未产生变化的受管
        # 模型；state=pendingApply 是退役已确认但客户端写入被推迟的条目。
        "pendingRetirement": pending_retirement,
        # Additive (R6/D4 2026-10-10): 客户端上下文冲突（客户端值无法证明是
        # 本同步发布的）——冲突不覆盖且进预览待裁定；单独成通道，绝不混进
        # warnings，也绝不冒充「待应用配置」。预览与应用携带同一份证据。
        "contextConflicts": context_conflicts,
        # Additive (F8 修订): 每条实际生效的人工 context 覆盖通知——预览与
        # apply 都携带自己的通道，绝不混进 warnings（覆盖生效不是本轮异常）。
        "overrideNotices": override_notices,
        "warnings": warnings,
        "dshChanged": dsh_changed,
        "zcodeChanged": zcode_changed,
        # 生效边界（Rev11）：对外区分「文件已写」「客户端已加载」「会话已采用」。
        # 文件替换并读回一致只是「已写」；客户端是否加载、当前/下一条消息是否
        # 采用目前都没有受控证据（无 RPC/日志取证），只能按 unverified 如实
        # 报告，绝不宣称热更新成功。
        "applicationBoundary": _application_boundary(
            published_paths, applied=should_apply),
    }
    if settings_preview is not None:
        result.update(settings_preview)
        result["settingsApplied"] = ([] if settings_deferred or not should_apply
                                     else settings_promoted)
        result["settingsDeferred"] = settings_deferred
        # §11.3.5：组合结果返回实际应用/延期/读回——读回与预览同一实现
        # （context_override_surface），绝不另判一套。
        result["settingsReadback"] = settings_readback
    if apply:
        _write_result(result)
    else:
        # Preview-only additive field: the apply-path applied/pending lists
        # are both empty here by construction, so the recomputed plan is the
        # only real signal a JSON consumer gets (the CLI prints it; the old
        # payload dropped it, making a dirty plan read as "no changes").
        result["plan"] = [change.description for change in plan.changes]
        # 应用路径会做的规则库登记/对齐也进入预览计划，预览与应用不再分叉。
        result["plan"].extend(preview_lines)
        # 草稿清理（2026-10-11）：模型已不在目录的草稿在预览里如实列出（应用时
        # 清理记录），绝不静默丢掉用户写的草稿，也绝不拿它顶掉整轮预览。
        result["plan"].extend(
            "清理草稿 %s（模型已不在目录，草稿无法发布）" % key
            for key in stale_draft_keys)
        # R6/D4：冲突不覆盖也进预览计划（带「待裁定」标记），预览绝不
        # 把它读成「无变化」或「将会覆盖」。
        result["plan"].extend(
            "（待裁定，本轮不覆盖）" + conflict["description"]
            for conflict in context_conflicts)
        _write_preview(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="AI网关模型目录同步")
    parser.add_argument("--apply", action="store_true", help="应用计划")
    parser.add_argument("--confirm", action="store_true", help="应用前确认")
    parser.add_argument("--probe", action="store_true",
                        help="显式能力探测轮（真实计费请求，独立于同步应用；"
                             "不带 --probe 的同步轮不发任何模型请求)")
    parser.add_argument("--scheduled", action="store_true", help="06:00计划任务模式")
    parser.add_argument(
        "--sources", default=None,
        help="限定本轮同步的来源（逗号分隔，当前启用来源子集）；"
             "人工停用的来源由调用方排除，空列表表示本轮无来源可同步")
    args = parser.parse_args()

    if args.scheduled:
        now = datetime.now().astimezone()
        if now.hour != 6 or now.minute > 10:
            print("已错过06:00执行窗口，本次跳过。")
            return 0
    scope: dict[str, Any] | None = None
    if args.sources is not None:
        # R1/D1（2026-10-11）：定时/手动全源路径也要遵守「显式停用取消自动上游
        # 查询」。调用方（invoke-model-sync.ps1）按 desired-state 标记剔除人工停用
        # 的来源后把子集传进来；空列表是「本轮无来源」，绝不能当成「全部」。
        requested = [part.strip() for part in args.sources.split(",")
                     if part.strip()]
        if not requested:
            print("本轮无可同步来源（人工停用已剔除），不读取任何上游。")
            return 0
        scope = {"sources": requested}
    try:
        with SyncLock():
            result = run_sync(
                apply=args.apply, run_probes=args.probe, confirm=args.confirm,
                scope=scope)
    except ConfigPublishError as exc:
        # 发布失败：区分已发布/已回退/保留现状，并给出恢复/对账路径。
        # 失败结果文件本身也尽力落盘；写不出去时 stderr 消息仍完整。
        result = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "code_stamp": CODE_STAMP,
            "status": "failed",
            "applied": [],
            "pending": [],
            "pendingRetirement": [],
            "warnings": [f"{type(exc).__name__}: {exc}"],
            "dshChanged": False,
            "zcodeChanged": False,
            "publish": _publish_recovery(exc),
        }
        print("模型同步失败：" + result["warnings"][0], file=sys.stderr)
        if args.apply:
            _write_result(result)
        else:
            _write_preview(result)
        return 1
    except Exception as exc:
        result = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "code_stamp": CODE_STAMP,
            "status": "failed",
            "applied": [],
            "pending": [],
            "pendingRetirement": [],
            "warnings": [f"{type(exc).__name__}: {exc}"],
            "dshChanged": False,
            "zcodeChanged": False,
        }
        print("模型同步失败：" + result["warnings"][0], file=sys.stderr)
        if args.apply:
            _write_result(result)
        else:
            _write_preview(result)
        return 1
    return 2 if result["status"] in ("pending", "warning") else 0


if __name__ == "__main__":
    raise SystemExit(main())
