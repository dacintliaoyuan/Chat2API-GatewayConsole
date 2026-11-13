# -*- coding: utf-8 -*-
"""Local statistics environment (E19-1-08).

The local composition root builds ``console_stats.StatsEnvironment`` from its own
explicit configuration; this module owns no reader-side price-table loader (the
cost/price feature set was retired with #30). It never imports ``usagesrv`` and
never reads a path the configuration did not name.

A missing or unreadable ledger yields an empty read instead of inventing usage:
records are reported honestly by the shared bounded reader.
"""
from __future__ import annotations

import os

from console.stats import StatsEnvironment


def build_stats_environment(config):
    """Build the statistics read environment from the explicit config."""
    return StatsEnvironment(
        chat2api_log=config.ledger_file,
        # E19-1-08: the local application has no DSH ledger snapshot; the
        # default UM read chain selects only chat2api, so this is never read.
        dsh_snapshot=None,
        dsh_merge_day="",
        parse_ts=_parse_ts,
        beijing_day=_beijing_day,
        day_start_ms=_day_start_ms,
        display_path=lambda path: os.path.basename(str(path or "")),
    )


def _parse_ts(value):
    """ISO 时间 → epoch ms；失败返回 None。"""
    from datetime import datetime, timezone
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


def _beijing_day(ms):
    from datetime import datetime, timedelta, timezone
    if ms is None:
        return None
    return datetime.fromtimestamp(
        ms / 1000, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def _day_start_ms(day):
    """北京时间某日 00:00 → epoch ms；缺失/不可解析返回 None（不猜）。"""
    from datetime import datetime, timedelta, timezone
    try:
        dt = datetime.strptime(str(day), "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    return dt.replace(
        tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000
