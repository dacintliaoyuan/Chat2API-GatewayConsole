# -*- coding: utf-8 -*-
"""Cloud-role summary payload.

E19-1-11.

The cloud role reports ONLY the UM share capability: it never reads the local
Qoder/chat2api/M365/DSH ledgers and never probes a local port. ``services`` is
an explicit empty object (not fabricated zero rows) because the share usage
has its own read surface.

The price table is INJECTED by the caller (the host owns
``load_price_table`` and its cache); this module uses the shared pure pricing
rules for the version/peak shape and reads no file, clock or global state.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# The PUBLIC pricing surface only (E19-1-01): ``pricing_rules`` also carries
# legacy underscored aliases for consumers that have not migrated, but a new
# cross-package consumer must not reach into them.
from shared.pricing_rules import DEFAULT_EXCHANGE_RATE, version_at

ROLE_CLOUD = "cloud"
BEIJING = timezone(timedelta(hours=8))
PRICE_STALE_DAYS = 30


def _beijing_windows(windows):
    out = []
    for window in windows or ():
        if not isinstance(window, dict):
            continue
        start = window.get("start")
        end = window.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        out.append({
            "beijing": "%02d:00-%02d:00" % ((start + 8) % 24, (end + 8) % 24),
            "rate": 2.0,
        })
    return out


def build_cloud_summary(table, *, generated_at=None):
    """Build the cloud summary from an explicitly injected price table."""
    if not isinstance(table, dict):
        raise ValueError("price table must be a mapping")
    rate = table.get("exchangeRate")
    if (not isinstance(rate, (int, float)) or isinstance(rate, bool)
            or rate <= 0):
        rate = DEFAULT_EXCHANGE_RATE
    peak = table.get("peak")
    if not isinstance(peak, dict):
        peak = {}
    versions = table.get("versions")
    if not isinstance(versions, list):
        versions = []
    versioned = table.get("versioned") is True
    now = generated_at or datetime.now(BEIJING)
    now_ms = now.timestamp() * 1000
    current = version_at(table, now_ms) if versions else None
    meta = table.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    version_meta = table.get("versionMeta")
    if not isinstance(version_meta, dict):
        version_meta = {}
    effective_at_ms = peak.get("effectiveAtMs")
    return {
        "generatedAt": now.isoformat(timespec="seconds"),
        "exchangeRate": rate,
        "currency": "CNY",
        "role": ROLE_CLOUD,
        "assembly": {
            "role": ROLE_CLOUD,
            "services": ["share"],
            "excluded": ["qoder", "chat2api", "m365", "dsh"],
            "note": "云端角色只装配 UM 分享：不读取本地私人账本、不探测 Qoder/"
                    "CLIProxy/model_sync；分享用量见分享用量读面",
        },
        "sources": {"priceMeta": meta},
        # The cloud role never reads the local UM ledger: the shape is
        # explicit, never a fabricated zero.
        "umReference": {
            "available": False,
            "records": None,
            "note": "云端角色不读取本地 UM 账本",
        },
        "peak": {
            "enabled": peak.get("enabled") is True,
            "effectiveAt": (
                datetime.fromtimestamp(
                    effective_at_ms / 1000, tz=BEIJING).strftime("%Y-%m-%d")
                if isinstance(effective_at_ms, (int, float))
                and not isinstance(effective_at_ms, bool) else None),
            "windows": _beijing_windows(peak.get("windows")),
            "weekendOffpeak": True,
            "note": "高峰按价表 peak 子档计价；北京时间周六/周日全天谷价"
                    "（与分享计价同一价目表）",
        },
        "priceVersions": {
            "versioned": versioned,
            "invalidCount": version_meta.get("invalidCount"),
            "current": None if current is None else {
                "versionId": current.get("versionId"),
                "effectiveAt": current.get("effectiveAt"),
                "exchangeRate": current.get("exchangeRate"),
                "checkedAt": current.get("checkedAt"),
                "checkedStale": (
                    current.get("checkedAgeDays") > PRICE_STALE_DAYS
                    if isinstance(current.get("checkedAgeDays"), (int, float))
                    else None),
            },
            "versions": [{
                "versionId": version.get("versionId"),
                "effectiveAt": version.get("effectiveAt"),
                "exchangeRate": version.get("exchangeRate"),
                "checkedAt": version.get("checkedAt"),
                "checkedStale": (
                    version.get("checkedAgeDays") > PRICE_STALE_DAYS
                    if isinstance(version.get("checkedAgeDays"), (int, float))
                    else None),
            } for version in versions if isinstance(version, dict)],
            "note": "逐条按记录时刻取当时生效版本段的价目与汇率",
        },
        # Honest "this deployment aggregates no private ledger": the UI
        # shows the missing-service aggregate, not fake zero requests/cost.
        "services": {},
    }
