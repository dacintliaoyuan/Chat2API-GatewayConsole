#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Model catalog, declared-context probe queue and manual context overrides.

Ticket #32 retires the client consumer chain and the model sync/settings/task
actions. This module now owns only the retained model-sync surfaces:

* Catalog reads (``collect_catalogs`` / ``collect_fresh_um_catalog``): plain GETs
  against the fixed UM endpoint, with single-source failures reported as
  warnings and a strict-fresh branch that requires the gateway's raw-fresh
  marker.
* Declared-context probe queue (``seed_context_probes`` /
  ``probe_context_queue`` / recovery helpers): measured long-input evidence per
  model, seeded after a catalog read and run only through the explicitly
  approved console action (≤20 requests, ≤64 tokens per run).
* Manual context overrides (``contextOverrides``): written ONLY by the console
  actions ``model.context_override_set`` / ``model.context_override_clear`` under
  ``SyncLock`` + ``apply_config_transaction``; read via
  ``model.context_overrides`` / ``context_override_surface``.  The stored
  ``set_at`` is the version token (``expected_set_at``); a mismatch raises
  ``ContextOverrideConflict`` instead of silently clobbering a concurrent change.
  With the client synchronization retired the override is state-only: saving it
  never writes a client configuration, and restoring it never deletes measured
  probe history.
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import http.client
import ipaddress

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PACKAGE_ROOT = str(Path(__file__).resolve().parents[1])
if _PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, _PACKAGE_ROOT)

from model_sync.sync_core import (
    CatalogModel,
    KNOWN_MODELS,
    VARIANT_SUFFIXES,
    empty_state,
    migrate_state,
    validate_catalog,
)

HERE = Path(__file__).resolve().parents[1]
DATA_DIR = HERE / "data"
STATE_FILE = DATA_DIR / "model-sync-state.json"
BACKUP_DIR = DATA_DIR / "model-sync-backups"
LOCK_FILE = DATA_DIR / "model-sync.lock"

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
    global DATA_DIR, STATE_FILE, BACKUP_DIR, LOCK_FILE
    global UM_MODELS_URL, UM_CHAT_URL, UM_MODELS_HOST, UM_MODELS_PORT
    global UM_MODELS_PATH, _CATALOG_ENDPOINTS
    global _LAST_FRESH_UM_CATALOG

    if data_dir is not None:
        if not isinstance(data_dir, (str, os.PathLike)) \
                or not str(data_dir).strip():
            raise ValueError("data_dir must be a non-empty path")
        DATA_DIR = Path(data_dir)
        STATE_FILE = DATA_DIR / "model-sync-state.json"
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
# chat2api gateway.  The header is chat2api's explicit raw-fresh opt-in: with
# it, an upstream failure answers 502 instead of replaying the stale model cache;
# on success chat2api adds the response marker below.  The marker is the freshness
# evidence — a 200 WITHOUT it is not a fresh answer and must never be reported as
# a successful refresh.
UM_MODELS_FRESH_HEADER = "X-Chat2Api-Models"
UM_MODELS_FRESH_VALUE = "raw-fresh"
UM_MODELS_FRESH_MARKER = "X-Chat2Api-Raw-Models"
UM_MODELS_FRESH_MARKER_VALUE = "1"
UM_MODELS_SOURCE = "um"
# In-memory last successful strict-fresh UM catalog.  A failed refresh may retain
# the last successful data, but the result is marked stale and success=False — it
# is NEVER presented as a fresh HTTP 200.  This is a read cache only: no state
# file, no client config, no Magpie config is written.
_LAST_FRESH_UM_CATALOG: dict[str, Any] | None = None
_LAST_FRESH_UM_LOCK = threading.Lock()

# Sources known to the retained chain; scope subsets are validated against
# these names (unknown names are rejected, never silently ignored).  Retired
# sources keep their name identity only so a selection naming one is refused as
# "已暂停", never silently widened or silently emptied.
KNOWN_SOURCES = ("um", "m365", "qoder_cn", "qoder_global", "cliproxy")
# B0 (2026-11-01): the default assembly is UM-only. Every retired source is
# PAUSED: the historical records stay readable, but no probe ever fetches them.
PAUSED_SOURCES = frozenset({"m365", "qoder_cn", "qoder_global", "cliproxy"})
ACTIVE_SOURCES = ("um",)

# Declared-context probe queue (L-E.4): bounded increasing INPUT lengths in
# raw characters.  The ladder is deliberately conservative — every step is a real
# billed request, the whole run is gated behind the maintainer-approved
# model.context_probe_run action, and steps are characters, NOT tokens: no
# char→token conversion is claimed anywhere.
PROBE_INPUT_STEPS = (4000, 16000, 64000, 256000)
CONTEXT_PROBE_MAX_TOKENS = 64
# Chat endpoints per source for the context probe queue: source -> (url,
# token provider).  B0 (2026-11-01): only the UM main chain is assembled.
CONTEXT_PROBE_ENDPOINTS = {
    "um": (UM_CHAT_URL, None),
}
# Stable identity of the probe scheme (Plan-Revision 7 step 3): the input
# ladder, the anchor prompt wording + per-request marker mechanism and the
# output cap together form one probe scheme.  Bump this integer whenever any of
# them changes: seeded records carry the revision they were queued under and a
# mismatch marks their evidence outdated (stale_reason="probe_scheme_changed").
PROBE_SCHEME_REVISION = 1
# 归档历史上限：单条探测记录的 ``history`` 最多保留 20 代，最旧的先挤出。
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

    Called after a catalog read: every model of a valid catalog joins
    (``pending`` when new) — including models WITHOUT a declared context
    (Plan-Revision 7 step 4): those records carry ``declared_context`` None,
    ``declared_context_known`` False and the honest note
    声明未知，探测仍可测量实际可用长度 (declared_note), so they stay
    explicitly selectable for a real measurement.  ``catalogs`` must be the
    PRE-override snapshot (the raw collector output): ``declared_context``
    records what the catalog claims, never the manual override's value, so an
    override set→clear cycle cannot flip or re-bill a verified record.

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
# _valid_override_context; this inclusive upper bound keeps a hand-typed value
# from dwarfing every real context window (F11 修订：下界也加上界).
CONTEXT_OVERRIDE_MAX = 10_000_000


def _record_int(record: dict[str, Any], field: str, default: int = 0) -> int:
    """Read a record's integer field defensively (state may be hand-edited).

    A bool or a non-int value (str/float/None) is NOT trusted and reads as
    ``default``: a garbage value never crashes a seeding/queue pass and never
    leaks into arithmetic the way ``int("3")`` or ``int(record.get(x) or 0)``
    would (the latter silently adopts any string int and crashes on anything
    else).
    """
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _valid_override_context(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, int)
            and 0 < value <= CONTEXT_OVERRIDE_MAX)


def _override_applies(entry: Any, model_context: int | None) -> bool:
    """Shared decision: does one stored override change this model's value?

    True only when the entry is a well-formed dict carrying a valid positive-int
    context AND that context differs from the model's catalog declaration — a None
    declaration is a legal target (200000 != None is a real change).  The write
    path and the read face both call this, so the two can never disagree about
    whether an override is in effect.
    """
    return (isinstance(entry, dict) and
            _valid_override_context(entry.get("context")) and
            entry.get("context") != model_context)


def _context_overrides_of_state(state: dict[str, Any]) -> dict[str, Any]:
    overrides = state.get("contextOverrides")
    if not isinstance(overrides, dict):
        overrides = {}
    return overrides


def set_context_override(
        catalogs: dict[str, list[CatalogModel]], source: str, model_id: str,
        context: int, expected_set_at: str | None = None) -> dict[str, Any]:
    """Write one manual context override (caller holds SyncLock).

    ``context`` must be a positive integer bounded by CONTEXT_OVERRIDE_MAX and
    ``source|model_id`` must exist in the current catalog snapshot.  The stored
    ``set_at`` is the version token: ``expected_set_at`` names the version the
    caller based its decision on (the read surface's ``override_at``; absent/None
    = "I expect NO existing override").  Any mismatch raises
    ContextOverrideConflict instead of silently clobbering a concurrent change.
    The state file is persisted in the SAME transaction as the write.
    """
    if not isinstance(source, str) or not source or \
            not isinstance(model_id, str) or not model_id:
        raise ValueError("source 与 model_id 必须为非空字符串")
    if not _valid_override_context(context):
        raise ValueError(
            "context 必须为 1-%d 的正整数" % CONTEXT_OVERRIDE_MAX)
    source_models = catalogs.get(source) if isinstance(catalogs, dict) else None
    model = next((m for m in (source_models or [])
                  if isinstance(m, CatalogModel) and m.model_id == model_id),
                 None)
    if model is None:
        raise ValueError("模型不在当前目录：%s|%s" % (source, model_id))
    state = load_state()
    overrides = _context_overrides_of_state(state)
    key = f"{source}|{model_id}"
    existing = overrides.get(key)
    stored_at = existing.get("set_at") if isinstance(existing, dict) else None
    if stored_at != expected_set_at:
        raise ContextOverrideConflict(
            "覆盖版本冲突（%s）：读面 override_at=%s，当前存储=%s；请基于"
            "最新读面重试" % (key, expected_set_at, stored_at))
    entry = {
        "context": int(context),
        "set_at": datetime.now(timezone.utc).isoformat(),
        "source": "manual",
    }
    overrides[key] = entry
    state["contextOverrides"] = overrides
    apply_config_transaction(
        {STATE_FILE: json.dumps(state, ensure_ascii=False, indent=2) + "\n"},
        BACKUP_DIR, keep_backups=30)
    return dict(entry)


def clear_context_override(
        source: str, model_id: str,
        expected_set_at: str | None = None) -> dict[str, Any]:
    """Delete one manual context override (caller holds SyncLock).

    探测记录与 history 原样保留；expected_set_at 版本规则同 set。清除不存在的
    覆盖是幂等成功（existed=False/changed=False），状态文件不重写。
    """
    if not isinstance(source, str) or not source or \
            not isinstance(model_id, str) or not model_id:
        raise ValueError("source 与 model_id 必须为非空字符串")
    state = load_state()
    overrides = _context_overrides_of_state(state)
    key = f"{source}|{model_id}"
    existing = overrides.get(key)
    stored_at = existing.get("set_at") if isinstance(existing, dict) else None
    if existing is None:
        return {"existed": False, "changed": False}
    if stored_at != expected_set_at:
        raise ContextOverrideConflict(
            "恢复版本冲突（%s）：读面版本令牌=%s，当前存储=%s；请基于最新"
            "读面重试" % (key, expected_set_at, stored_at))
    del overrides[key]
    state["contextOverrides"] = overrides
    apply_config_transaction(
        {STATE_FILE: json.dumps(state, ensure_ascii=False, indent=2) + "\n"},
        BACKUP_DIR, keep_backups=30)
    return {"existed": True, "changed": True,
            "note": "覆盖已清除；目录声明值即为当前有效值"}


def context_override_surface(
        state: dict[str, Any],
        catalogs: dict[str, list[CatalogModel]],
) -> list[dict[str, Any]]:
    """Read-only surface: declaration, override, effective value and source.

    Entries cover the union of current-catalog models and stored overrides,
    sorted by key.  ``override_state`` classifies each row: "none" (no stored
    override), "applied" (well-formed override differing from the declaration
    — the ONLY state where the effective value is the override), "equal"
    (well-formed but identical to the declaration — the value is not rewritten,
    effective_source stays "declared"/"unknown"), "malformed" (non-dict entry or
    an illegal stored value — never applied) and "model_missing" (the model left
    the current catalog — inert, effective follows the declaration).  INERT is
    therefore limited to exactly two causes: the model left the catalog, or the
    stored value is malformed.  ``override_applied`` and ``effective_source``
    come from the SAME shared predicate the write path uses
    (``_override_applies``), so the read face can never disagree with what the
    write actually stores — a None declaration with a legal override reads
    applied (effective_context = the override value), never inert/unknown.
    ``declared_at`` comes from the model's probe record when present;
    ``override_at`` is the stored override's ``set_at``.
    """
    overrides = _context_overrides_of_state(state)
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
    return [rows[key] for key in sorted(rows)]


def _normalize_scope(scope: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate the optional source/model scope.

    ``sources`` must be a non-empty duplicate-free subset of the known names;
    ``models`` maps source -> selected model ids (non-empty strings, at most 64
    per source, 256 overall).  Unknown names or shapes raise ValueError; the
    console layer maps that to OperationError("invalid_params").  A scope without
    any restriction is normalized to None.
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
    if not sources and not models:
        return None
    return {"sources": sources, "models": models}


# ---------------------------------------------------------------------------
# State-file transactions.  Client files are gone; the retained state file is
# still replaced atomically (same-directory temp + fsync + bounded rename
# retry) with a version-checked rollback on failure.
# ---------------------------------------------------------------------------
RENAME_RETRY_DELAYS_S: tuple[float, ...] = (0.025, 0.05, 0.1, 0.2, 0.4, 0.8)
_RENAME_RETRYABLE_WINERRORS = frozenset({5, 32, 33, 170})


def _rename_error_retryable(exc: OSError) -> bool:
    """该 rename 错误是否属于「目标暂时被占用」的可重试类。"""
    if getattr(exc, "winerror", None) in _RENAME_RETRYABLE_WINERRORS:
        return True
    return getattr(exc, "errno", None) in (errno.EPERM, errno.EACCES,
                                          errno.EBUSY)


def replace_with_retry(source: Path, target: Path, *,
                      delays: tuple[float, ...] = RENAME_RETRY_DELAYS_S) -> None:
    """``os.replace`` 的有界重试：仅对 Windows 占用类错误退避重试。

    共尝试 ``len(delays) + 1`` 次（首次 + 每个退避一次），最后一次失败原样
    抛出——耗尽即明确失败，调用方按发布失败处理，绝不静默继续。
    """
    for attempt in range(len(delays) + 1):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if not _rename_error_retryable(exc) or attempt == len(delays):
                raise
            time.sleep(delays[attempt])


class ConfigPublishError(RuntimeError, OSError):
    """一次发布失败的结构化结果（本地状态文件不是跨文件事务）。

    逐文件替换 + 版本校验回退不是真正的跨文件事务：失败后必须区分
    「已发布」「已回退」「保留现状待核对」三类文件，绝不把部分发布
    说成整体成功，也绝不把并发写者的新内容盖回旧快照。
    """

    def __init__(self, message: str, *, failed_path: Path | None = None,
                 published: Any = (), rolled_back: Any = (),
                 left_in_place: Any = (), cause: BaseException | None = None):
        super().__init__(message)
        self.failed_path = failed_path
        self.published = list(published)
        self.rolled_back = list(rolled_back)
        self.left_in_place = list(left_in_place)
        self.cause = cause


def _rollback_replaced(
        replaced: list[tuple[Path, bytes]],
        originals: dict[Path, bytes | None],
        ) -> tuple[list[Path], list[Path]]:
    """按版本校验回退已替换文件；返回 ``(已回退, 保留现状)``。"""
    rolled_back: list[Path] = []
    left_in_place: list[Path] = []
    for path, written in reversed(replaced):
        try:
            current = path.read_bytes()
        except OSError:
            left_in_place.append(path)
            continue
        if current != written:
            # 并发写者已改写：旧快照绝不盖回去。
            left_in_place.append(path)
            continue
        original = originals.get(path)
        rollback_path = path.with_name(path.name + ".rollback.tmp")
        try:
            if original is None:
                path.unlink()
            else:
                rollback_path.write_bytes(original)
                replace_with_retry(rollback_path, path)
        except OSError:
            left_in_place.append(path)
            continue
        finally:
            if rollback_path.exists():
                rollback_path.unlink(missing_ok=True)
        rolled_back.append(path)
    return rolled_back, left_in_place


def _publish_failure_message(
        failed_path: Path | None, cause: BaseException,
        replaced: list[tuple[Path, bytes]], rolled_back: list[Path],
        left_in_place: list[Path]) -> str:
    """发布失败消息：只报文件名与三类结果，绝不回显文件内容。"""
    def names(paths: list[Path]) -> str:
        return "、".join(sorted({path.name for path in paths})) or "无"

    failed = failed_path.name if failed_path is not None else "（替换前）"
    return (
        "发布失败（%s：%s）：已替换 %s；已按版本校验回退 %s；保留现状待核对 %s；"
        "客户端文件与本地同步状态不是跨文件事务，需按上述恢复/对账路径处理"
        % (failed, type(cause).__name__,
           names([path for path, _ in replaced]),
           names(rolled_back), names(left_in_place)))


def apply_config_transaction(
        candidates: dict[Path, str], backup_root: Path,
        keep_backups: int = 30, timestamp: str | None = None) -> list[Path]:
    """逐个完整替换一批已验证文件；失败时按版本校验回退，绝不盖并发编辑。

    每个文件都是一次完整替换（同目录 temp + fsync + 原子 rename，Windows
    占用类错误有界重试）。本地状态文件**不是**真正的跨文件事务：这里只保证
    逐文件原子替换 + 失败补偿。补偿只回退仍等于本次写入内容的文件；已被并发
    写者改写或回退失败的文件保留现状，并以 :class:`ConfigPublishError`
    明确失败（已发布/已回退/保留现状三分类），绝不静默成功、绝不无限重试。
    """
    if not candidates:
        return []
    stamp = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_dir = backup_root / stamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    originals: dict[Path, bytes | None] = {}
    staged: dict[Path, Path] = {}
    replaced: list[tuple[Path, bytes]] = []
    current: Path | None = None
    try:
        for path, content in candidates.items():
            current = path
            path.parent.mkdir(parents=True, exist_ok=True)
            originals[path] = path.read_bytes() if path.exists() else None
            if originals[path] is not None:
                backup_path = backup_dir / (path.name + ".bak")
                backup_path.write_bytes(originals[path])
            with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", newline="", delete=False,
                    dir=path.parent, prefix=path.name + ".", suffix=".tmp") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                staged[path] = Path(handle.name)
        for path, temporary_path in staged.items():
            current = path
            replace_with_retry(temporary_path, path)
            replaced.append((path, candidates[path].encode("utf-8")))
        directories = sorted(
            [item for item in backup_root.iterdir() if item.is_dir()],
            key=lambda item: item.name, reverse=True)
        for old in directories[keep_backups:]:
            shutil.rmtree(old)
        return [path for path, _ in replaced]
    except Exception as exc:
        rolled_back, left_in_place = _rollback_replaced(replaced, originals)
        raise ConfigPublishError(
            _publish_failure_message(current, exc, replaced, rolled_back,
                                     left_in_place),
            failed_path=current, published=[path for path, _ in replaced],
            rolled_back=rolled_back, left_in_place=left_in_place,
            cause=exc) from exc
    finally:
        for temporary_path in staged.values():
            temporary_path.unlink(missing_ok=True)
