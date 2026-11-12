#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Narrow read-only Magpie effective-price synchronizer.

Scope (B1, 2026-11-01):

* read Magpie's model catalogue (``GET /v1/models``) and, when an explicit
  CLI path is configured, its effective-price detail (``magpie model price``);
* convert Magpie's per-token USD catalogue values to per-million USD values and
  keep the source unit explicit;
* map UM model ids to Magpie model ids ONLY through the configured exact map;
* price a record's cache component only from an explicit Q11
  ``cache_report_status``: missing/unknown stays unknown (unpriced), only
  ``reported`` with a zero value is a measured zero, and ``unreported`` is a
  marked cache-miss upper bound;
* append one immutable price-snapshot version per successful sync, atomically
  replacing the snapshot file; a failed sync never touches the last successful
  snapshot and writes a separate status file;
* never write ``data/prices.json`` or ``data/price.override.json``; those stay
  the share-execution price inputs and are outside this module's write surface.

The snapshot is a local UM reference price source. ``usagesrv`` reads it with
``compute_reference`` to show CNY reference cost for UM's own ledger. The share
metering path keeps using ``estimate_record_with_version_and_rate`` and the
existing price table; this module never changes that path.

No credentials are read, logged or stored. The HTTP reader refuses non-loopback
targets and non-http(s) schemes, sends no Authorization header, follows no
redirects and caps the response body size. The optional CLI reader uses an argument
vector (never a shell) and a bounded timeout/output.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import (HTTPRedirectHandler, ProxyHandler, Request,
                            build_opener)

HERE = os.path.dirname(os.path.abspath(__file__))

SNAPSHOT_FORMAT = "um-magpie-price-snapshot-v1"
STATUS_FORMAT = "um-magpie-price-status-v1"
SNAPSHOT_SCHEMA_VERSION = 1

DEFAULT_BASE_URL = "http://127.0.0.1:3425"
DEFAULT_SNAPSHOT_FILE = os.path.join(HERE, "data", "magpie-prices.snapshot.json")
DEFAULT_STATUS_FILE = os.path.join(HERE, "data", "magpie-prices.status.json")
MODELS_PATH = "/v1/models"
ROOT_PATH = "/"

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_CLI_OUTPUT_BYTES = 128 * 1024
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
HTTP_TIMEOUT = 5.0
CLI_TIMEOUT = 5.0

CURRENCY = "USD"
UNIT = "per_million_tokens"
SOURCE_UNIT = "per_token"
REFERENCE_LABEL = "参考"

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
# Q11: the writer's cache_report_status is the only authority. Missing or
# unrecognised values are historical/unknown, never a measured cache zero.
CACHE_REPORT_STATUSES = ("reported", "unreported")
_PRICE_KEYS = {
    "input": ("prompt",),
    "output": ("completion",),
    "cacheRead": ("input_cache_read",),
    "cacheWrite": ("input_cache_write",),
}


class MagpiePriceError(RuntimeError):
    """A predictable Magpie read/parse/save refusal with a stable code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)


class SnapshotError(MagpiePriceError):
    """The local price snapshot is missing, corrupt or unsupported."""


class SyncConfigError(MagpiePriceError):
    """The explicit synchronizer configuration is invalid."""


@dataclass
class SyncConfig:
    """Explicit Magpie entry, UM model map and local snapshot target."""

    base_url: str = DEFAULT_BASE_URL
    model_map: dict = field(default_factory=dict)
    snapshot_path: str = DEFAULT_SNAPSHOT_FILE
    status_path: str | None = None
    cli_path: str | None = None
    exchange_rate: float | None = None
    fx_version: str | None = None
    timeout: float = HTTP_TIMEOUT
    effective_at: str | None = None

    def __post_init__(self):
        self.base_url = _normalize_base_url(self.base_url)
        self.snapshot_path = str(self.snapshot_path)
        if not self.status_path:
            self.status_path = status_path_for(self.snapshot_path)
        else:
            self.status_path = str(self.status_path)
        self.model_map = _normalize_model_map(self.model_map)
        raw_rate = self.exchange_rate
        self.exchange_rate = _positive_number(raw_rate)
        if raw_rate is not None and self.exchange_rate is None:
            raise SyncConfigError("magpie_fx_invalid",
                                  "Magpie exchange rate must be positive")
        if self.exchange_rate is None:
            self.fx_version = None
        elif self.fx_version is not None:
            self.fx_version = str(self.fx_version).strip() or None
        try:
            self.timeout = float(self.timeout)
        except (TypeError, ValueError) as exc:
            raise SyncConfigError("magpie_timeout_invalid",
                                  "Magpie timeout must be a number") from exc
        if not (0 < self.timeout <= 60):
            raise SyncConfigError("magpie_timeout_invalid",
                                  "Magpie timeout must be within 0..60 seconds")
        if self.effective_at is not None and _parse_ts(self.effective_at) is None:
            raise SyncConfigError("magpie_effective_at_invalid",
                                  "Magpie effectiveAt is not a valid time")


def _normalize_model_map(raw):
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SyncConfigError("magpie_model_map_invalid",
                              "UM model map must be an object")
    result, seen = {}, set()
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise SyncConfigError("magpie_model_map_invalid",
                                  "UM model map keys and values must be strings")
        um_model = key.strip()
        magpie_model = value.strip()
        if not um_model or not magpie_model:
            raise SyncConfigError("magpie_model_map_invalid",
                                  "UM model map keys and values must be non-empty")
        if um_model in seen:
            raise SyncConfigError("magpie_model_map_duplicate",
                                  "UM model map has a duplicate model id")
        if len(um_model) > 200 or len(magpie_model) > 200:
            raise SyncConfigError("magpie_model_map_invalid",
                                  "UM model map entry is too long")
        if not _safe_model_id(um_model) or not _safe_model_id(magpie_model):
            raise SyncConfigError("magpie_model_map_invalid",
                                  "UM model map entry contains unsupported characters")
        seen.add(um_model)
        result[um_model] = magpie_model
    return result


def _safe_model_id(value):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}", str(value)))


def _normalize_base_url(value):
    if not isinstance(value, str) or not value.strip():
        raise SyncConfigError("magpie_url_invalid", "Magpie URL is empty")
    text = value.strip().rstrip("/")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise SyncConfigError("magpie_url_invalid",
                              "Magpie URL must be http or https")
    if parsed.username or parsed.password:
        raise SyncConfigError("magpie_url_invalid",
                              "Magpie URL must not carry credentials")
    if parsed.query or parsed.fragment:
        raise SyncConfigError("magpie_url_invalid",
                              "Magpie URL must not carry query or fragment")
    if parsed.path not in ("", "/"):
        raise SyncConfigError("magpie_url_invalid",
                              "Magpie URL must not carry a path")
    host = (parsed.hostname or "").lower()
    if host not in _LOOPBACK_HOSTS:
        raise SyncConfigError("magpie_url_not_loopback",
                              "Magpie URL must target loopback")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SyncConfigError("magpie_url_invalid",
                              "Magpie URL port is invalid") from exc
    if port is None:
        raise SyncConfigError("magpie_url_invalid",
                              "Magpie URL must name an explicit port")
    return text


def _join(base_url, path):
    return base_url.rstrip("/") + path


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_local_opener = build_opener(ProxyHandler({}), _NoRedirect())


def _fetch_json(url, timeout):
    """GET a JSON document with a hard body cap and no proxy/redirects."""
    req = Request(url, headers={"Accept": "application/json"})
    try:
        with _local_opener.open(req, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise MagpiePriceError(
                    "magpie_http_status",
                    "Magpie answered HTTP %s" % status)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise MagpiePriceError("magpie_http_status",
                               "Magpie answered HTTP %s" % exc.code) from exc
    except MagpiePriceError:
        raise
    except Exception as exc:
        raise MagpiePriceError("magpie_unreachable",
                               "Magpie read failed (%s)" % type(exc).__name__) from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise MagpiePriceError("magpie_response_too_large",
                               "Magpie response exceeds the body cap")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MagpiePriceError("magpie_response_invalid",
                               "Magpie response is not valid JSON") from exc


def _run_cli(cli_path, args, timeout):
    if not cli_path:
        raise MagpiePriceError("magpie_cli_not_configured",
                               "Magpie CLI is not configured")
    if not isinstance(args, (list, tuple)) or not all(
            isinstance(item, str) for item in args):
        raise MagpiePriceError("magpie_cli_args_invalid",
                               "Magpie CLI arguments must be strings")
    try:
        completed = subprocess.run(
            [str(cli_path)] + list(args),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise MagpiePriceError("magpie_cli_missing",
                               "Magpie CLI was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise MagpiePriceError("magpie_cli_timeout",
                               "Magpie CLI timed out") from exc
    except OSError as exc:
        raise MagpiePriceError("magpie_cli_unreadable",
                               "Magpie CLI could not be started") from exc
    if completed.returncode != 0:
        raise MagpiePriceError(
            "magpie_cli_failed",
            "Magpie CLI exited with %s" % completed.returncode)
    text = completed.stdout or ""
    if len(text.encode("utf-8", "replace")) > MAX_CLI_OUTPUT_BYTES:
        raise MagpiePriceError("magpie_cli_output_too_large",
                               "Magpie CLI output exceeds the cap")
    return text


def _as_number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            return None
    if not math.isfinite(number):
        return None
    return number


def _positive_number(value):
    number = _as_number(value)
    if number is None or number <= 0:
        return None
    return number


def _non_negative_number(value):
    number = _as_number(value)
    if number is None or number < 0:
        return None
    return number


def _token_count(value):
    number = _as_number(value)
    if number is None or number < 0:
        return None
    return int(number)


def _parse_ts(value):
    """ISO-8601 or epoch milliseconds -> epoch milliseconds; else None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp() * 1000.0


def _iso(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _iso_from_epoch(epoch):
    return _iso(float(epoch) * 1000.0)


def _parse_threshold(text, suffix):
    number = _as_number(text)
    if number is None or number <= 0:
        return None
    factor = {"": 1, "k": 1000, "m": 1000000}.get(str(suffix or "").lower())
    if factor is None:
        return None
    return int(number * factor)


def parse_price_detail(text):
    """Parse ``magpie model price`` human output into effective components.

    The CLI is the only installed surface that exposes the effective price source
    (manual vs. list), cache write price and tier rules. The parser is deliberately
    narrow: an unparsed line leaves the corresponding field ``None`` and marks the
    detail incomplete; it never invents zero.
    """
    if not isinstance(text, str) or not text.strip():
        return {
            "base": None, "tiers": None, "tiersReported": False,
            "source": None, "manualPrice": None, "detailComplete": False,
            "detailError": "empty_output",
        }
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    base_line = next((line for line in lines
                      if "per 1M in/out" in line and "over " not in line
                      and "cache read" not in line), None)
    cache_line = next((line for line in lines
                       if "cache read" in line and "per 1M in/out" not in line),
                      None)
    tier_lines = [line for line in lines
                  if "input tokens" in line and "over " in line]
    source_line = next((line for line in lines
                        if "what " in line and
                        ("lists" in line or "you said" in line)), None)

    base = None
    error = None
    if base_line:
        match = re.search(
            r"\$([0-9]+(?:\.[0-9]+)?)/\$([0-9]+(?:\.[0-9]+)?) "
            r"per 1M in/out", base_line)
        if match:
            base = {"input": _parse_detail_number(match.group(1)),
                    "output": _parse_detail_number(match.group(2)),
                    "cacheRead": None, "cacheWrite": None}
    if base is None:
        error = "base_not_parsed"

    if base is not None and cache_line:
        match = re.search(
            r"cache read \$([0-9]+(?:\.[0-9]+)?)"
            r"(?:,\s*cache write \$([0-9]+(?:\.[0-9]+)?))?",
            cache_line)
        if match:
            base["cacheRead"] = _parse_detail_number(match.group(1))
            if match.group(2) is not None:
                base["cacheWrite"] = _parse_detail_number(match.group(2))
        else:
            error = error or "cache_not_parsed"

    tiers = None
    tiers_reported = False
    if base is not None and source_line is not None:
        tiers = []
        tiers_reported = True
        for line in tier_lines:
            match = re.search(
                r"over ([0-9]+(?:\.[0-9]+)?)\s*([KkMm]?) input tokens"
                r".*?\$([0-9]+(?:\.[0-9]+)?)/\$([0-9]+(?:\.[0-9]+)?) "
                r"in/out(?:,\s*cache \$([0-9]+(?:\.[0-9]+)?)/"
                r"\$([0-9]+(?:\.[0-9]+)?))?", line)
            if not match:
                tiers_reported = False
                error = error or "tier_not_parsed"
                continue
            threshold = _parse_threshold(match.group(1), match.group(2))
            if threshold is None:
                tiers_reported = False
                error = error or "tier_threshold_not_parsed"
                continue
            tier = {
                "overInputTokens": threshold,
                "input": _parse_detail_number(match.group(3)),
                "output": _parse_detail_number(match.group(4)),
                "cacheRead": (_parse_detail_number(match.group(5))
                               if match.group(5) is not None else None),
                "cacheWrite": (_parse_detail_number(match.group(6))
                                if match.group(6) is not None else None),
            }
            if None in (tier["input"], tier["output"]):
                tiers_reported = False
                error = error or "tier_price_not_parsed"
                continue
            tiers.append(tier)
        if not tier_lines:
            tiers = []
    elif base is not None and tier_lines:
        # A tier line without a parsed source cannot be trusted as complete.
        tiers_reported = False
        error = error or "source_not_parsed"

    source = source_line.lstrip("·- ").strip() if source_line else None
    manual = None
    if source is not None:
        lowered = source.lower()
        if "you said" in lowered:
            manual = True
        elif "lists" in lowered:
            manual = False

    complete = (base is not None and source is not None and tiers_reported)
    if base is not None and source is None:
        error = error or "source_not_parsed"
    return {
        "base": base,
        "tiers": tiers,
        "tiersReported": tiers_reported,
        "source": source,
        "manualPrice": manual,
        "detailComplete": complete,
        "detailError": error,
    }


def _parse_detail_number(value):
    number = _as_number(value)
    if number is None or number < 0:
        return None
    return number


def _catalog_index(payload):
    if not isinstance(payload, dict):
        raise MagpiePriceError("magpie_catalog_invalid",
                               "Magpie catalogue is not an object")
    data = payload.get("data")
    if not isinstance(data, list):
        raise MagpiePriceError("magpie_catalog_invalid",
                               "Magpie catalogue has no data list")
    index = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        index[model_id.strip()] = item
    if not index:
        raise MagpiePriceError("magpie_catalog_empty",
                               "Magpie catalogue has no model ids")
    return index


def _catalog_prices(item):
    pricing = item.get("pricing") if isinstance(item, dict) else None
    if not isinstance(pricing, dict):
        return {key: None for key in _PRICE_KEYS}
    return {
        "input": _non_negative_number(pricing.get("prompt")),
        "output": _non_negative_number(pricing.get("completion")),
        "cacheRead": _non_negative_number(pricing.get("input_cache_read")),
        "cacheWrite": _non_negative_number(pricing.get("input_cache_write")),
    }


def _per_million(value):
    return None if value is None else round(value * 1000000.0, 12)


def _detail_prices(detail):
    base = detail.get("base") if isinstance(detail, dict) else None
    if not isinstance(base, dict):
        return None
    return {
        "input": _non_negative_number(base.get("input")),
        "output": _non_negative_number(base.get("output")),
        "cacheRead": _non_negative_number(base.get("cacheRead")),
        "cacheWrite": _non_negative_number(base.get("cacheWrite")),
    }


def _detail_tiers(detail):
    if not isinstance(detail, dict) or detail.get("tiersReported") is not True:
        return None
    tiers = detail.get("tiers")
    if not isinstance(tiers, list):
        return None
    result = []
    for tier in tiers:
        if not isinstance(tier, dict):
            return None
        threshold = _token_count(tier.get("overInputTokens"))
        input_price = _non_negative_number(tier.get("input"))
        output_price = _non_negative_number(tier.get("output"))
        if threshold is None or input_price is None or output_price is None:
            return None
        result.append({
            "overInputTokens": threshold,
            "input": input_price,
            "output": output_price,
            "cacheRead": _non_negative_number(tier.get("cacheRead")),
            "cacheWrite": _non_negative_number(tier.get("cacheWrite")),
        })
    return sorted(result, key=lambda item: item["overInputTokens"])


def _model_entry(um_model, magpie_model, item, detail, observed_at):
    catalog = _catalog_prices(item)
    detail_prices = _detail_prices(detail)
    if detail_prices is not None:
        prices = detail_prices
        price_source = "magpie-cli-effective"
    else:
        prices = {
            "input": _per_million(catalog["input"]),
            "output": _per_million(catalog["output"]),
            "cacheRead": _per_million(catalog["cacheRead"]),
            "cacheWrite": _per_million(catalog["cacheWrite"]),
        }
        price_source = "magpie-catalog"

    catalog_mismatch = False
    if detail_prices is not None:
        for key in _PRICE_KEYS:
            catalog_value = _per_million(catalog.get(key))
            detail_value = detail_prices.get(key)
            if catalog_value is not None and detail_value is not None and \
                    abs(catalog_value - detail_value) > 1e-9:
                catalog_mismatch = True

    tiers = _detail_tiers(detail)
    tiers_reported = tiers is not None
    manual = detail.get("manualPrice") if isinstance(detail, dict) else None
    correction = "magpie-catalog"
    if manual is True:
        correction = "magpie-manual"
    elif detail is not None and detail.get("base") is not None:
        correction = "magpie-effective"
    display_name = item.get("display_name") if isinstance(item, dict) else None
    aliases = [um_model]
    if isinstance(display_name, str) and display_name.strip() and \
            display_name.strip() not in aliases:
        aliases.append(display_name.strip())
    complete = (prices["input"] is not None and
                prices["output"] is not None and
                prices["cacheRead"] is not None and
                prices["cacheWrite"] is not None)
    return {
        "umModel": um_model,
        "magpieModel": magpie_model,
        "displayName": display_name if isinstance(display_name, str) else None,
        "aliases": aliases,
        "currency": CURRENCY,
        "unit": UNIT,
        "sourceUnit": SOURCE_UNIT if price_source == "magpie-catalog" else UNIT,
        "input": prices["input"],
        "output": prices["output"],
        "cacheRead": prices["cacheRead"],
        "cacheWrite": prices["cacheWrite"],
        "tiers": tiers,
        "tiersReported": tiers_reported,
        "manualPrice": manual,
        "correctionSource": correction,
        "priceSource": price_source,
        "source": (detail.get("source") if isinstance(detail, dict)
                   and detail.get("source") else "magpie:/v1/models"),
        "sourceObservedAt": observed_at,
        "complete": complete,
        "catalogMismatch": catalog_mismatch,
        "detailError": (detail.get("detailError") if isinstance(detail, dict)
                        else None),
    }


def _apply_aliases(models, model_map):
    """Record every configured UM id that resolves to the same Magpie model."""
    by_magpie = {}
    for um_model, magpie_model in model_map.items():
        by_magpie.setdefault(magpie_model, []).append(um_model)
    for um_model, entry in models.items():
        aliases = sorted(by_magpie.get(entry.get("magpieModel"), [um_model]))
        display = entry.get("displayName")
        if isinstance(display, str) and display.strip() and \
                display.strip() not in aliases:
            aliases.append(display.strip())
        entry["aliases"] = aliases


def _missing_model_entry(um_model, magpie_model, observed_at, reason):
    return {
        "umModel": um_model,
        "magpieModel": magpie_model,
        "displayName": None,
        "aliases": [um_model],
        "currency": CURRENCY,
        "unit": UNIT,
        "sourceUnit": None,
        "input": None,
        "output": None,
        "cacheRead": None,
        "cacheWrite": None,
        "tiers": None,
        "tiersReported": False,
        "manualPrice": None,
        "correctionSource": "magpie-unavailable",
        "priceSource": "magpie-catalog",
        "source": "magpie:/v1/models",
        "sourceObservedAt": observed_at,
        "complete": False,
        "unpricedReason": reason,
    }


def _version_id(now, models):
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    return "magpie-%s-%s" % (stamp, _content_hash(models)[:8])


def _atomic_write_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2,
                      sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, str(target))
        _fsync_dir(target.parent)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _fsync_dir(directory):
    if os.name != "posix":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def status_path_for(snapshot_path):
    path = Path(snapshot_path)
    name = path.name
    if name.endswith(".snapshot.json"):
        return str(path.with_name(name[:-len(".snapshot.json")] +
                                ".status.json"))
    return str(path.with_name(path.stem + ".status.json"))


def _read_json_file(path, max_bytes=MAX_SNAPSHOT_BYTES):
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        raise SnapshotError("snapshot_missing", "价格快照不存在")
    except OSError as exc:
        raise SnapshotError("snapshot_unreadable",
                            "价格快照不可读（%s）" % type(exc).__name__)
    if len(raw) > max_bytes:
        raise SnapshotError("snapshot_too_large", "价格快照超过大小上限")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SnapshotError("snapshot_corrupt", "价格快照损坏（不是有效 JSON）") from exc


def _validate_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        raise SnapshotError("snapshot_corrupt", "价格快照不是对象")
    if snapshot.get("format") != SNAPSHOT_FORMAT:
        raise SnapshotError("snapshot_unsupported", "价格快照格式不受支持")
    versions = snapshot.get("versions")
    if not isinstance(versions, list) or not versions:
        raise SnapshotError("snapshot_corrupt", "价格快照没有版本")
    result = []
    for raw in versions:
        if not isinstance(raw, dict):
            raise SnapshotError("snapshot_corrupt", "价格快照版本不是对象")
        version = raw.get("version")
        effective_ms = _parse_ts(raw.get("effectiveAt"))
        observed_ms = _parse_ts(raw.get("sourceObservedAt"))
        models = raw.get("models")
        if not isinstance(version, str) or not version.strip():
            raise SnapshotError("snapshot_corrupt", "价格快照版本缺少标识")
        if effective_ms is None:
            raise SnapshotError("snapshot_corrupt", "价格快照版本缺少生效时间")
        if observed_ms is None:
            raise SnapshotError("snapshot_corrupt", "价格快照版本缺少来源时间")
        if not isinstance(models, dict):
            raise SnapshotError("snapshot_corrupt", "价格快照版本缺少模型价格")
        rate = _positive_number(raw.get("exchangeRate"))
        fx_version = raw.get("fxVersion")
        if fx_version is not None and not isinstance(fx_version, str):
            raise SnapshotError("snapshot_corrupt", "价格快照汇率版本非法")
        normalized_models = {}
        for um_model, entry in models.items():
            if not isinstance(um_model, str) or not isinstance(entry, dict):
                raise SnapshotError("snapshot_corrupt", "价格快照模型条目非法")
            normalized = dict(entry)
            for key in ("input", "output", "cacheRead", "cacheWrite"):
                value = _non_negative_number(entry.get(key))
                if entry.get(key) is not None and value is None:
                    raise SnapshotError("snapshot_corrupt",
                                        "价格快照模型价格非法")
                normalized[key] = value
            if entry.get("unit") != UNIT or entry.get("currency") != CURRENCY:
                raise SnapshotError("snapshot_corrupt", "价格快照单位或币种非法")
            tiers = entry.get("tiers")
            if tiers is not None:
                if not isinstance(tiers, list):
                    raise SnapshotError("snapshot_corrupt", "价格快照阶梯非法")
                normalized_tiers = []
                for tier in tiers:
                    if not isinstance(tier, dict):
                        raise SnapshotError("snapshot_corrupt",
                                            "价格快照阶梯条目非法")
                    threshold = _token_count(tier.get("overInputTokens"))
                    input_price = _non_negative_number(tier.get("input"))
                    output_price = _non_negative_number(tier.get("output"))
                    if threshold is None or input_price is None or \
                            output_price is None:
                        raise SnapshotError("snapshot_corrupt",
                                            "价格快照阶梯价格非法")
                    normalized_tiers.append({
                        "overInputTokens": threshold,
                        "input": input_price,
                        "output": output_price,
                        "cacheRead": _non_negative_number(
                            tier.get("cacheRead")),
                        "cacheWrite": _non_negative_number(
                            tier.get("cacheWrite")),
                    })
                normalized["tiers"] = sorted(
                    normalized_tiers,
                    key=lambda item: item["overInputTokens"])
            normalized_models[um_model] = normalized
        result.append({
            "version": version.strip(),
            "effectiveAt": raw.get("effectiveAt"),
            "effectiveAtMs": effective_ms,
            "sourceObservedAt": raw.get("sourceObservedAt"),
            "sourceObservedAtMs": observed_ms,
            "exchangeRate": rate,
            "fxVersion": fx_version if isinstance(fx_version, str) else None,
            "fxAvailable": rate is not None,
            "correctionSource": raw.get("correctionSource"),
            "source": raw.get("source"),
            "models": normalized_models,
            "unpricedModels": [str(item) for item in
                               (raw.get("unpricedModels") or [])
                               if isinstance(item, str)],
        })
    result.sort(key=lambda item: item["effectiveAtMs"])
    return {
        "format": SNAPSHOT_FORMAT,
        "schemaVersion": snapshot.get("schemaVersion", SNAPSHOT_SCHEMA_VERSION),
        "generatedAt": snapshot.get("generatedAt"),
        "source": snapshot.get("source") if isinstance(
            snapshot.get("source"), dict) else {},
        "currentVersion": snapshot.get("currentVersion"),
        "versions": result,
    }


def load_snapshot(path):
    return _validate_snapshot(_read_json_file(path))


def load_status(path):
    try:
        raw = _read_json_file(path, max_bytes=1024 * 1024)
    except SnapshotError as exc:
        if exc.code == "snapshot_missing":
            return None
        return {"state": "failed", "failure": "价格同步状态损坏",
                "lastAttemptAt": None, "lastSuccessAt": None,
                "snapshotVersion": None, "snapshotAt": None}
    if not isinstance(raw, dict) or raw.get("format") != STATUS_FORMAT:
        return {"state": "failed", "failure": "价格同步状态格式不受支持",
                "lastAttemptAt": None, "lastSuccessAt": None,
                "snapshotVersion": None, "snapshotAt": None}
    state = raw.get("state")
    if state not in ("synced", "failed", "unsynced"):
        state = "failed"
    return {
        "format": STATUS_FORMAT,
        "state": state,
        "lastAttemptAt": raw.get("lastAttemptAt"),
        "lastSuccessAt": raw.get("lastSuccessAt"),
        "snapshotVersion": raw.get("snapshotVersion"),
        "snapshotAt": raw.get("snapshotAt"),
        "failure": raw.get("failure") if isinstance(
            raw.get("failure"), str) else None,
        "modelCount": raw.get("modelCount"),
        "unpricedModels": raw.get("unpricedModels") or [],
    }


def _file_sha256(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def load_price_reference(snapshot_path, status_path=None):
    """Return the current snapshot state without pricing any ledger record."""
    status_path = status_path or status_path_for(snapshot_path)
    status = load_status(status_path)
    try:
        snapshot = load_snapshot(snapshot_path)
    except SnapshotError as exc:
        if exc.code == "snapshot_missing":
            if status and status.get("state") == "failed":
                return {
                    "state": "failed", "failure": status.get("failure"),
                    "snapshotAt": None, "version": None,
                    "source": None, "exchangeRate": None,
                    "fxVersion": None, "snapshotSha256": None,
                    "versions": [], "models": {},
                }
            return {
                "state": "unsynced", "failure": "还没有成功的价格快照",
                "snapshotAt": None, "version": None,
                "source": None, "exchangeRate": None,
                "fxVersion": None, "snapshotSha256": None,
                "versions": [], "models": {},
            }
        failure = (status.get("failure") if status and
                   status.get("state") == "failed" and status.get("failure")
                   else str(exc))
        return {
            "state": "failed", "failure": failure,
            "snapshotAt": None, "version": None,
            "source": None, "exchangeRate": None,
            "fxVersion": None, "snapshotSha256": None,
            "versions": [], "models": {},
        }
    current = snapshot["versions"][-1]
    failed = bool(status and status.get("state") == "failed")
    return {
        "state": "failed" if failed else "synced",
        "failure": status.get("failure") if failed else None,
        "snapshotAt": (current["sourceObservedAtMs"]
                       if current.get("sourceObservedAtMs") is not None
                       else current["effectiveAtMs"]),
        "version": current["version"],
        "source": "Magpie（本机）",
        "exchangeRate": current.get("exchangeRate"),
        "fxVersion": current.get("fxVersion"),
        "snapshotSha256": _file_sha256(snapshot_path),
        "versions": [version["version"] for version in snapshot["versions"]],
        "models": current["models"],
        "currentVersion": current,
    }


def _select_version(snapshot, at_ms):
    if at_ms is None or at_ms != at_ms:
        return None
    versions = snapshot["versions"]
    keys = [version["effectiveAtMs"] for version in versions]
    index = bisect.bisect_right(keys, at_ms) - 1
    return versions[index] if index >= 0 else None


def _select_tier(model, input_tokens):
    tiers = model.get("tiers")
    if not isinstance(tiers, list):
        return None
    hit = None
    for tier in tiers:
        threshold = tier.get("overInputTokens")
        if isinstance(threshold, int) and input_tokens > threshold:
            if hit is None or threshold > hit["overInputTokens"]:
                hit = tier
    return hit


def _record_time(record):
    if isinstance(record, dict):
        value = record.get("_ms")
        if isinstance(value, (int, float)) and value == value:
            return float(value)
        return _parse_ts(record.get("time"))
    return None


def _record_start(record):
    """The request START used only by the A2-8 first-effective boundary.

    ``None`` means the historical row carries no start evidence: it keeps the
    previous completion-time pricing unchanged and is never inferred from
    ``time - duration_ms``. A present but unparsable value is also ``None``,
    so a damaged field cannot silently become a new eligibility claim.
    """
    if isinstance(record, dict):
        value = record.get("request_started_at")
        if isinstance(value, (int, float)) and value == value:
            return float(value)
        return _parse_ts(value)
    return None


def _record_number(record, names):
    for name in names:
        if name in record:
            value = _token_count(record.get(name))
            if value is not None:
                return value
    return None


def _record_cache(record):
    """Q11 cache report status and a trustworthy cached token count.

    ``cache_report_status`` is the only authority (the chat2api writer always
    writes it for new rows): a missing or unrecognised value is ``unknown`` for
    historical rows, never ``reported``. The compatibility
    ``cached_input_tokens``/``cache_tokens`` value is read only for an explicit
    ``reported`` status, so neither an unknown row nor an ``unreported`` row can
    turn the compatibility zero into a measured cache hit.
    """
    status = record.get("cache_report_status")
    if status == "unreported":
        return None, "unreported"
    if status != "reported":
        return None, "unknown"
    value = _record_number(record, ("cached_input_tokens", "cache_tokens"))
    if value is None:
        return None, "unknown"
    return value, "reported"


def _record_cache_write(record):
    value = _record_number(record, (
        "cached_write_tokens", "cache_write_tokens",
        "cache_creation_input_tokens"))
    if value is None:
        return None
    return value


def _price_record(record, version):
    model_name = str(record.get("model") or "unknown")
    model = version["models"].get(model_name)
    cache_tokens, cache_report = _record_cache(record)
    if model is None:
        return {"priced": False, "reason": "model_unmapped",
                "estimated": False, "costUsd": 0.0,
                "cacheReport": cache_report}
    input_tokens = _record_number(record, ("input_tokens",))
    output_tokens = _record_number(record, ("output_tokens",))
    cache_write_tokens = _record_cache_write(record)
    if input_tokens is None or output_tokens is None:
        return {"priced": False, "reason": "usage_unknown",
                "estimated": False, "costUsd": 0.0,
                "cacheReport": cache_report}

    tier = _select_tier(model, input_tokens)
    input_price = tier["input"] if tier else model.get("input")
    output_price = tier["output"] if tier else model.get("output")
    cache_read_price = (tier["cacheRead"] if tier else model.get("cacheRead"))
    cache_write_price = (tier["cacheWrite"] if tier
                         else model.get("cacheWrite"))
    if input_price is None or output_price is None:
        return {"priced": False, "reason": "price_missing",
                "estimated": False, "costUsd": 0.0,
                "cacheReport": cache_report}

    if cache_report == "unknown" and (cache_read_price is None or
                                     cache_read_price > 0):
        # Q11: a missing/unrecognised cache status is not a measured zero.
        # The cache component (usage and/or price) is unknown, so the record
        # stays unpriced instead of charging every input token as cache-miss.
        return {"priced": False, "reason": "cache_usage_unknown",
                "estimated": False, "costUsd": 0.0,
                "cacheReport": cache_report}
    if cache_tokens is not None and cache_read_price is None and cache_tokens > 0:
        return {"priced": False, "reason": "cache_price_missing",
                "estimated": False, "costUsd": 0.0,
                "cacheReport": cache_report}
    if cache_write_tokens is not None and cache_write_price is None and \
            cache_write_tokens > 0:
        return {"priced": False, "reason": "cache_write_price_missing",
                "estimated": False, "costUsd": 0.0,
                "cacheReport": cache_report}
    if cache_write_tokens is None and cache_write_price is not None and \
            cache_write_price > 0:
        return {"priced": False, "reason": "cache_write_usage_unknown",
                "estimated": False, "costUsd": 0.0,
                "cacheReport": cache_report}

    # An unreported cache is not a measured zero, but the record can still be
    # priced as a cache-miss upper bound and is marked estimated/unreported.
    cache = cache_tokens or 0
    noncached = max(0, input_tokens - cache)
    cost_usd = (
        noncached * input_price
        + cache * (cache_read_price or 0)
        + output_tokens * output_price
        + (cache_write_tokens or 0) * (cache_write_price or 0)
    ) / 1000000.0
    estimated = (model.get("tiers") is None or
                 model.get("complete") is not True or
                 cache_report != "reported")
    return {"priced": True, "reason": None, "estimated": estimated,
            "costUsd": cost_usd, "cacheReport": cache_report}


def _empty_coverage():
    return {
        "records": 0, "pricedRequests": 0, "unpricedRequests": 0,
        "estimatedRequests": 0, "incompleteRequests": 0,
        "unknownEffectiveRequests": 0, "fxMissingRequests": 0,
        "preEffectiveRequests": 0,
        "cacheReportedRequests": 0, "cacheUnreportedRequests": 0,
        "cacheUnknownRequests": 0,
        "costUsd": 0.0, "cny": None, "cnyPartial": False,
    }


def _base_result(state, failure, reference):
    coverage = _empty_coverage()
    coverage["cny"] = None
    coverage["cnyPartial"] = True
    return {
        "label": REFERENCE_LABEL,
        "cny": None,
        "cnyPartial": True,
        "costUsd": 0.0,
        "price": {
            "state": state,
            "source": reference.get("source"),
            "snapshotAt": reference.get("snapshotAt"),
            "version": reference.get("version"),
            "failure": failure,
        },
        "fx": {
            "rate": reference.get("exchangeRate"),
            "version": reference.get("fxVersion"),
        },
        "snapshotSha256": reference.get("snapshotSha256"),
        "coverage": coverage,
        "models": [],
    }


def compute_reference(records, snapshot_path, status_path=None, now=None):
    """Price UM ledger records with the local Magpie snapshot.

    ``records`` are the UM (chat2api) ledger dicts with ``_ms`` already set.
    Only an exact UM model id in the snapshot is priced; no fuzzy matching.
    A record outside every snapshot version's effective range is not repriced
    with the newest version. Thinking tokens are already part of output and are
    never added again.
    """
    records = list(records or [])
    status_path = status_path or status_path_for(snapshot_path)
    status = load_status(status_path)
    try:
        snapshot = load_snapshot(snapshot_path)
    except SnapshotError as exc:
        state = "failed"
        failure = str(exc)
        if exc.code == "snapshot_missing" and not (
                status and status.get("state") == "failed"):
            state = "unsynced"
            failure = "还没有成功的价格快照"
        elif status and status.get("state") == "failed" and status.get("failure"):
            failure = status.get("failure")
        result = _base_result(state, failure, {})
        result["coverage"]["records"] = len(records)
        result["coverage"]["unpricedRequests"] = len(records)
        result["coverage"]["incompleteRequests"] = len(records)
        result["coverage"]["cnyPartial"] = bool(records)
        return result
    current = snapshot["versions"][-1]
    failed = bool(status and status.get("state") == "failed")
    reference = {
        "source": "Magpie（本机）",
        "snapshotAt": (current["sourceObservedAtMs"]
                       if current.get("sourceObservedAtMs") is not None
                       else current["effectiveAtMs"]),
        "version": current["version"],
        "exchangeRate": current.get("exchangeRate"),
        "fxVersion": current.get("fxVersion"),
        "snapshotSha256": _file_sha256(snapshot_path),
    }
    if not records:
        result = _base_result("synced", None, reference)
        result["cny"] = 0.0 if reference["exchangeRate"] is not None else None
        result["cnyPartial"] = reference["exchangeRate"] is None
        result["price"]["state"] = "failed" if failed else "synced"
        result["price"]["failure"] = status.get("failure") if failed else None
        return result

    coverage = _empty_coverage()
    models = {}
    used_versions = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        coverage["records"] += 1
        model_name = str(record.get("model") or "unknown")
        at_ms = _record_time(record)
        version = _select_version(snapshot, at_ms)
        model_row = models.setdefault(model_name, {
            "model": model_name,
            "magpieModel": None,
            "requests": 0,
            "pricedRequests": 0,
            "unpricedRequests": 0,
            "estimatedRequests": 0,
            "incompleteRequests": 0,
            "unknownEffectiveRequests": 0,
            "fxMissingRequests": 0,
            "preEffectiveRequests": 0,
            "cacheReportedRequests": 0,
            "cacheUnreportedRequests": 0,
            "cacheUnknownRequests": 0,
            "costUsd": 0.0,
            "cny": None,
            "cnyPartial": False,
            "priceVersion": None,
            "fxRate": None,
            "fxVersion": None,
            "input": None,
            "output": None,
            "cacheRead": None,
            "cacheWrite": None,
        })
        model_row["requests"] += 1
        # Q11 cache evidence is counted independently of whether the record
        # can be priced: a missing status is unknown, never a measured zero.
        cache_report = _record_cache(record)[1]
        if cache_report == "reported":
            coverage["cacheReportedRequests"] += 1
            model_row["cacheReportedRequests"] += 1
        elif cache_report == "unreported":
            coverage["cacheUnreportedRequests"] += 1
            model_row["cacheUnreportedRequests"] += 1
        else:
            coverage["cacheUnknownRequests"] += 1
            model_row["cacheUnknownRequests"] += 1
        start_ms = _record_start(record)
        if (start_ms is not None
                and start_ms < snapshot["versions"][0]["effectiveAtMs"]):
            # A2-8: a request that STARTED before the FIRST effective
            # snapshot stays unpriced even when it completed after that
            # snapshot. A historical row without start evidence keeps its
            # previous completion-time pricing unchanged.
            coverage["preEffectiveRequests"] += 1
            coverage["unknownEffectiveRequests"] += 1
            coverage["unpricedRequests"] += 1
            coverage["incompleteRequests"] += 1
            model_row["preEffectiveRequests"] += 1
            model_row["unknownEffectiveRequests"] += 1
            model_row["unpricedRequests"] += 1
            model_row["incompleteRequests"] += 1
            continue
        if version is None:
            coverage["unknownEffectiveRequests"] += 1
            coverage["unpricedRequests"] += 1
            coverage["incompleteRequests"] += 1
            model_row["unknownEffectiveRequests"] += 1
            model_row["unpricedRequests"] += 1
            model_row["incompleteRequests"] += 1
            continue
        model = version["models"].get(model_name)
        if model is not None:
            model_row["magpieModel"] = model.get("magpieModel")
            model_row["input"] = model.get("input")
            model_row["output"] = model.get("output")
            model_row["cacheRead"] = model.get("cacheRead")
            model_row["cacheWrite"] = model.get("cacheWrite")
        model_row["priceVersion"] = version["version"]
        model_row["fxRate"] = version.get("exchangeRate")
        model_row["fxVersion"] = version.get("fxVersion")
        priced = _price_record(record, version)
        if not priced["priced"]:
            coverage["unpricedRequests"] += 1
            coverage["incompleteRequests"] += 1
            model_row["unpricedRequests"] += 1
            model_row["incompleteRequests"] += 1
            continue
        coverage["pricedRequests"] += 1
        model_row["pricedRequests"] += 1
        used_versions[version["version"]] = version
        coverage["costUsd"] += priced["costUsd"]
        model_row["costUsd"] += priced["costUsd"]
        if priced["estimated"]:
            coverage["estimatedRequests"] += 1
            model_row["estimatedRequests"] += 1
        rate = version.get("exchangeRate")
        if rate is None:
            coverage["fxMissingRequests"] += 1
            coverage["incompleteRequests"] += 1
            coverage["cnyPartial"] = True
            model_row["fxMissingRequests"] += 1
            model_row["incompleteRequests"] += 1
            model_row["cnyPartial"] = True
            continue
        coverage["cny"] = (coverage["cny"] or 0.0) + priced["costUsd"] * rate
        model_row["cny"] = (model_row["cny"] or 0.0) + priced["costUsd"] * rate

    if len(used_versions) == 1:
        used = next(iter(used_versions.values()))
        reference["version"] = used["version"]
        reference["exchangeRate"] = used.get("exchangeRate")
        reference["fxVersion"] = used.get("fxVersion")
        reference["snapshotAt"] = (used["sourceObservedAtMs"]
                                   if used.get("sourceObservedAtMs") is not None
                                   else used["effectiveAtMs"])
    coverage["costUsd"] = round(coverage["costUsd"], 6)
    for row in models.values():
        row["costUsd"] = round(row["costUsd"], 6)
        if row["cny"] is not None:
            row["cny"] = round(row["cny"], 4)
        row["cnyPartial"] = bool(
            row["cnyPartial"] or row["incompleteRequests"] or
            row["unpricedRequests"] or row["unknownEffectiveRequests"] or
            row["fxMissingRequests"] or row["estimatedRequests"])
    if coverage["cny"] is not None:
        coverage["cny"] = round(coverage["cny"], 4)
    coverage["cnyPartial"] = bool(
        coverage["cnyPartial"] or coverage["incompleteRequests"] or
        coverage["unpricedRequests"] or coverage["unknownEffectiveRequests"] or
        coverage["estimatedRequests"])
    result = _base_result(
        "failed" if failed else "synced",
        status.get("failure") if failed else None, reference)
    result["cny"] = coverage["cny"] if coverage["pricedRequests"] else None
    result["cnyPartial"] = bool(
        coverage["cnyPartial"] or coverage["pricedRequests"] == 0)
    result["costUsd"] = coverage["costUsd"]
    result["coverage"] = coverage
    result["models"] = sorted(models.values(),
                             key=lambda item: (-item["costUsd"], item["model"]))
    result["price"]["state"] = "failed" if failed else "synced"
    result["price"]["failure"] = status.get("failure") if failed else None
    return result


def _write_status(path, payload):
    try:
        _atomic_write_json(path, payload)
    except OSError:
        return False
    return True


def _load_fx(config):
    rate = config.exchange_rate
    version = config.fx_version
    if rate is not None:
        return rate, version
    # A2-7 isolation: the UM reference price reads ONLY the Magpie FX file.
    # ``data/prices.json`` and ``USAGE_PRICE_FILE`` belong to the share price
    # path; borrowing their rate would silently couple the two ledgers and would
    # fabricate an FX version the Magpie snapshot never observed.
    path = os.environ.get("MAGPIE_PRICE_FX_FILE")
    if not path:
        return None, None
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None, None
    if not isinstance(raw, dict):
        return None, None
    rate = _positive_number(raw.get("exchangeRate"))
    version = raw.get("checkedAt")
    versions = raw.get("versions")
    if isinstance(versions, list) and versions:
        current = versions[-1] if isinstance(versions[-1], dict) else {}
        rate = _positive_number(current.get("exchangeRate")) or rate
        version = current.get("versionId") or version
    return rate, str(version).strip() if isinstance(version, str) else None


def _detail_for_model(config, magpie_model, cli_runner=None):
    if not config.cli_path:
        return None, "cli_not_configured"
    try:
        text = (cli_runner([config.cli_path, "model", "price", magpie_model],
                           config.timeout) if cli_runner
                else _run_cli(config.cli_path,
                              ["model", "price", magpie_model],
                              config.timeout))
        return parse_price_detail(text), None
    except MagpiePriceError as exc:
        return None, exc.code
    except Exception as exc:  # optional detail must not fail the snapshot
        return None, "cli_detail_failed_%s" % type(exc).__name__


def sync(config, fetch=None, cli_runner=None, now=None):
    """Fetch, map, append and atomically save one price snapshot version."""
    if not isinstance(config, SyncConfig):
        raise SyncConfigError("magpie_config_invalid",
                              "SyncConfig is required")
    if not config.model_map:
        raise SyncConfigError("magpie_model_map_empty",
                              "UM model map is empty")
    fetch = fetch or _fetch_json
    epoch = time.time() if now is None else float(now)
    observed_at = _iso_from_epoch(epoch)
    effective_at = config.effective_at or observed_at
    effective_ms = _parse_ts(effective_at)
    if effective_ms is None:
        raise SyncConfigError("magpie_effective_at_invalid",
                              "Magpie effectiveAt is not a valid time")
    rate, fx_version = _load_fx(config)

    try:
        catalog = _catalog_index(fetch(_join(config.base_url, MODELS_PATH),
                                      config.timeout))
        try:
            root = fetch(_join(config.base_url, ROOT_PATH), config.timeout)
        except Exception:  # root is optional; the catalogue is the contract
            root = {}
        magpie_version = (root.get("version")
                           if isinstance(root, dict) else None)
        details = {}
        detail_errors = {}
        for um_model, magpie_model in config.model_map.items():
            detail, error = _detail_for_model(config, magpie_model, cli_runner)
            if detail is not None:
                details[um_model] = detail
            elif error:
                detail_errors[um_model] = error
        models = {}
        unpriced = []
        for um_model, magpie_model in sorted(config.model_map.items()):
            item = catalog.get(magpie_model)
            detail = details.get(um_model)
            if item is None:
                entry = _missing_model_entry(
                    um_model, magpie_model, observed_at, "magpie_model_missing")
                unpriced.append(um_model)
            else:
                entry = _model_entry(um_model, magpie_model, item, detail,
                                     observed_at)
                if entry["input"] is None or entry["output"] is None:
                    unpriced.append(um_model)
            models[um_model] = entry
        _apply_aliases(models, config.model_map)
        corrections = {entry.get("correctionSource") for entry in models.values()}
        correction_source = ("magpie-manual" if "magpie-manual" in corrections
                             else "magpie-effective-price")
        content = {
            "models": models,
            "exchangeRate": rate,
            "fxVersion": fx_version,
            "effectiveAt": effective_at,
        }
        version = {
            "version": _version_id(epoch, content),
            "effectiveAt": effective_at,
            "effectiveAtMs": int(effective_ms),
            "sourceObservedAt": observed_at,
            "sourceObservedAtMs": int(epoch * 1000),
            "exchangeRate": rate,
            "fxVersion": fx_version,
            "fxAvailable": rate is not None,
            "correctionSource": correction_source,
            "source": {
                "kind": "magpie",
                "baseUrl": config.base_url,
                "version": magpie_version if isinstance(
                    magpie_version, str) else None,
                "catalogPath": MODELS_PATH,
                "observedAt": observed_at,
            },
            "models": models,
            "unpricedModels": sorted(unpriced),
            "detailErrors": detail_errors,
        }
        snapshot = {
            "format": SNAPSHOT_FORMAT,
            "schemaVersion": SNAPSHOT_SCHEMA_VERSION,
            "generatedAt": observed_at,
            "source": version["source"],
            "currentVersion": version["version"],
            "versions": [],
        }
        try:
            previous = load_snapshot(config.snapshot_path)
            snapshot["versions"] = previous["versions"]
        except SnapshotError as exc:
            if exc.code != "snapshot_missing":
                # A corrupt snapshot is not silently discarded: keep it and
                # report failure so an operator can inspect the damage.
                raise SnapshotError(
                    "snapshot_corrupt",
                    "已有价格快照损坏，拒绝覆盖（%s）" % exc) from exc
        snapshot["versions"].append(version)
        snapshot["versions"].sort(key=lambda item: item["effectiveAtMs"])
        snapshot["currentVersion"] = snapshot["versions"][-1]["version"]
        _atomic_write_json(config.snapshot_path, snapshot)
    except Exception as exc:
        failure = str(exc) if isinstance(exc, MagpiePriceError) else \
            "价格同步失败（%s）" % type(exc).__name__
        prior_status = load_status(config.status_path)
        previous = None
        try:
            previous = load_snapshot(config.snapshot_path)
        except SnapshotError:
            previous = None
        current = previous["versions"][-1] if previous else None
        last_success = (prior_status.get("lastSuccessAt")
                        if prior_status else None)
        if not last_success and current:
            last_success = _iso(current["sourceObservedAtMs"])
        snapshot_version = (current["version"] if current else
                            prior_status.get("snapshotVersion")
                            if prior_status else None)
        snapshot_at = (_iso(current["sourceObservedAtMs"]) if current else
                       prior_status.get("snapshotAt") if prior_status else None)
        _write_status(
            config.status_path,
            _status_payload(
                "failed", epoch,
                snapshot_version,
                snapshot_at,
                failure,
                len(current["models"]) if current else None,
                current.get("unpricedModels") if current else None,
                last_success_at=last_success))
        return {
            "ok": False,
            "state": "failed",
            "failure": failure,
            "snapshotPath": config.snapshot_path,
            "statusPath": config.status_path,
            "version": current["version"] if current else None,
            "effectiveAt": current["effectiveAt"] if current else None,
            "sourceObservedAt": current["sourceObservedAt"] if current else None,
            "exchangeRate": current["exchangeRate"] if current else None,
            "fxVersion": current["fxVersion"] if current else None,
            "modelCount": len(current["models"]) if current else 0,
            "unpricedModels": current.get("unpricedModels", []) if current else [],
            "versions": len(previous["versions"]) if previous else 0,
            "snapshotSha256": _file_sha256(config.snapshot_path),
        }

    _write_status(
        config.status_path,
        _status_payload(
            "synced", epoch, version["version"],
            _iso(version["sourceObservedAtMs"]), None,
            len(models), sorted(unpriced)))
    return {
        "ok": True,
        "state": "synced",
        "failure": None,
        "snapshotPath": config.snapshot_path,
        "statusPath": config.status_path,
        "version": version["version"],
        "effectiveAt": version["effectiveAt"],
        "sourceObservedAt": version["sourceObservedAt"],
        "exchangeRate": rate,
        "fxVersion": fx_version,
        "modelCount": len(models),
        "unpricedModels": sorted(unpriced),
        "versions": len(snapshot["versions"]),
        "snapshotSha256": _file_sha256(config.snapshot_path),
    }


def backup_manifest(snapshot_path, status_path=None):
    """Return the snapshot's version closure for a backup owner to include."""
    status_path = status_path or status_path_for(snapshot_path)
    snapshot = load_snapshot(snapshot_path)
    entries = []
    for version in snapshot["versions"]:
        entries.append({
            "version": version["version"],
            "effectiveAt": version["effectiveAt"],
            "sourceObservedAt": version["sourceObservedAt"],
            "sha256": _content_hash(version["models"]),
            "modelCount": len(version["models"]),
        })
    files = [{
        "role": "snapshot",
        "path": str(snapshot_path),
        "sha256": _file_sha256(snapshot_path),
    }]
    if os.path.exists(status_path):
        files.append({
            "role": "status",
            "path": str(status_path),
            "sha256": _file_sha256(status_path),
        })
    return {
        "format": "um-magpie-price-backup-closure-v1",
        "current": entries[-1] if entries else None,
        "versions": entries,
        "files": files,
    }


def _content_hash(value):
    text = json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _status_payload(state, epoch, version=None, snapshot_at=None,
                    failure=None, model_count=None, unpriced=None,
                    last_success_at=None):
    return {
        "format": STATUS_FORMAT,
        "state": state,
        "lastAttemptAt": _iso_from_epoch(epoch),
        "lastSuccessAt": (_iso_from_epoch(epoch) if state == "synced"
                           else last_success_at),
        "snapshotVersion": version,
        "snapshotAt": snapshot_at,
        "failure": failure,
        "modelCount": model_count,
        "unpricedModels": unpriced or [],
    }


def config_from_env():
    raw_map = os.environ.get("MAGPIE_PRICE_MODEL_MAP") or "{}"
    try:
        model_map = json.loads(raw_map)
    except ValueError as exc:
        raise SyncConfigError("magpie_model_map_invalid",
                              "MAGPIE_PRICE_MODEL_MAP is not JSON") from exc
    snapshot = os.environ.get("MAGPIE_PRICE_SNAPSHOT") or DEFAULT_SNAPSHOT_FILE
    status = os.environ.get("MAGPIE_PRICE_STATUS") or status_path_for(snapshot)
    rate = os.environ.get("MAGPIE_PRICE_FX_RATE")
    return SyncConfig(
        base_url=os.environ.get("MAGPIE_PRICE_URL") or DEFAULT_BASE_URL,
        model_map=model_map,
        snapshot_path=snapshot,
        status_path=status,
        cli_path=os.environ.get("MAGPIE_PRICE_CLI") or None,
        exchange_rate=rate,
        fx_version=os.environ.get("MAGPIE_PRICE_FX_VERSION") or None,
        effective_at=os.environ.get("MAGPIE_PRICE_EFFECTIVE_AT") or None,
    )


def _config_from_args(args):
    """Merge the environment configuration with explicit CLI overrides.

    The environment is the deployment path (``console.env`` + the systemd
    ``EnvironmentFile``); CLI flags are operator overrides and win.  A missing
    ``--status`` still derives the status path from the effective snapshot so
    ``--snapshot`` never writes status next to the default snapshot.
    """
    base = config_from_env()
    overrides = {}
    if args.base_url is not None:
        overrides["base_url"] = args.base_url
    if args.model_map is not None:
        try:
            overrides["model_map"] = json.loads(args.model_map)
        except ValueError as exc:
            raise SyncConfigError("magpie_model_map_invalid",
                                  "--model-map is not JSON") from exc
    if args.snapshot is not None:
        overrides["snapshot_path"] = args.snapshot
        if args.status is None and not os.environ.get("MAGPIE_PRICE_STATUS"):
            overrides["status_path"] = None
    if args.status is not None:
        overrides["status_path"] = args.status
    if args.cli is not None:
        overrides["cli_path"] = args.cli
    if args.fx_rate is not None:
        overrides["exchange_rate"] = args.fx_rate
    if args.fx_version is not None:
        overrides["fx_version"] = args.fx_version
    if args.effective_at is not None:
        overrides["effective_at"] = args.effective_at
    if not overrides:
        return base
    return replace(base, **overrides)


def _main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description="Read Magpie effective prices into the local UM snapshot")
    parser.add_argument("--sync", action="store_true",
                        help="fetch and save one snapshot version")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--model-map", default=None,
                        help="JSON object: UM model id -> Magpie model id")
    parser.add_argument("--snapshot", default=None)
    parser.add_argument("--status", default=None)
    parser.add_argument("--cli", default=None)
    parser.add_argument("--fx-rate", default=None)
    parser.add_argument("--fx-version", default=None)
    parser.add_argument("--effective-at", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.sync:
        parser.print_help()
        return 0
    try:
        config = _config_from_args(args)
        result = sync(config)
    except MagpiePriceError as exc:
        result = {"ok": False, "state": "failed", "failure": str(exc),
                  "code": exc.code}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(result.get("failure") or "价格快照已同步：%s" % result.get("version"))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_main())
