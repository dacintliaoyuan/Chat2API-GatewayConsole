# -*- coding: utf-8 -*-
"""Local statistics environment (E19-1-08).

The local composition root builds ``console_stats.StatsEnvironment`` from its own
explicit configuration; this module owns the reader-side price-table loader and
its bounded cache. It never imports ``usagesrv`` and never reads a path the
configuration did not name.

The loader normalizes the existing on-disk price document into the versioned
shape ``shared.pricing_rules`` expects: a document with a top-level ``versions``
list is parsed segment by segment, so each record uses the segment in effect at
its time; a legacy document (no ``versions``) becomes ONE always-effective
default segment. An explicit override document is the highest-priority manual patch
layer. A missing or unreadable document yields an empty table instead of
inventing prices: every record is then reported as unpriced, never as zero cost.
"""
from __future__ import annotations

import json
import os
import threading
import time

from console_stats import StatsEnvironment
from shared import pricing_rules

PRICE_CACHE_TTL_S = 60.0
PRICE_STALE_DAYS = 30
_PRICE_CACHE = {}
_PRICE_CACHE_LOCK = threading.Lock()
# standards-tests-01: the model matcher is memoized in THIS endpoint, the same
# way the usagesrv host does it. ``shared.pricing_rules`` is a pure rule module
# with no cache, so without this the local read chain re-ran the expensive match
# for every ledger row (400 uncached matches for a 400-row fixture) while the
# regression test only pinned the host adapter.
_MATCH_CACHE = {}
_MATCH_CACHE_LOCK = threading.Lock()
MATCH_CACHE_MAX = 8192


def match_model_id(model_id, candidates):
    """Memoized model match for the local read chain.

    The cache key is ``(model_id, tuple(candidates))``; only list/tuple
    candidates are cached (other iterables have different ``in``/iteration
    semantics), and the cache is bounded by ``MATCH_CACHE_MAX``.
    """
    if not model_id or not candidates:
        return None
    if not isinstance(candidates, (list, tuple)):
        return pricing_rules.match_model_id_uncached(model_id, candidates)
    try:
        key = (model_id, tuple(candidates))
        hash(key)
    except TypeError:
        return pricing_rules.match_model_id_uncached(model_id, candidates)
    with _MATCH_CACHE_LOCK:
        if key in _MATCH_CACHE:
            return _MATCH_CACHE[key]
    result = pricing_rules.match_model_id_uncached(model_id, candidates)
    with _MATCH_CACHE_LOCK:
        if len(_MATCH_CACHE) >= MATCH_CACHE_MAX:
            _MATCH_CACHE.clear()
        _MATCH_CACHE[key] = result
    return result


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def normalize_price_table(payload, override=None, now=None,
                           override_found=None):
    """Normalize the on-disk price document into the shared table shape.

    The parsing itself is the ONE pure rule both ends use
    (``shared.pricing_rules.normalize_price_table``), so the local stats reader
    and the cloud summary read the same document the same way. The file READ and
    the cache stay in this endpoint: ``shared`` performs no I/O.
    """
    return pricing_rules.normalize_price_table(
        payload,
        override=override,
        now=time.time() if now is None else now,
        override_found=override_found)


def load_price_table(path, override_path=None):
    """Read the explicit price file once per minute; never a second path."""
    target = os.path.abspath(str(path))
    override_target = (os.path.abspath(str(override_path))
                       if override_path else None)
    cache_key = (target, override_target)
    now = time.time()
    with _PRICE_CACHE_LOCK:
        cached = _PRICE_CACHE.get(cache_key)
        if cached is not None and now - cached["at"] < PRICE_CACHE_TTL_S:
            return cached["data"]
    data = normalize_price_table(
        _read_json(target),
        override=(_read_json(override_target)
                  if override_target is not None else None),
        now=now,
        override_found=(override_target is not None
                        and os.path.exists(override_target)))
    with _PRICE_CACHE_LOCK:
        _PRICE_CACHE[cache_key] = {"at": now, "data": data}
    return data


def build_stats_environment(config):
    """Build the statistics read environment from the explicit config."""
    price_file = config.price_file
    override_file = config.price_override_file

    def estimate_record(model_id, at_ms, input_tok, output_tok, cache_tok,
                         reasoning_tok):
        return pricing_rules.estimate_record(
            model_id, at_ms, input_tok, output_tok, cache_tok, reasoning_tok,
            load_price_table(price_file, override_file),
            matcher=match_model_id)

    return StatsEnvironment(
        chat2api_log=config.ledger_file,
        # E19-1-08: the local application has no DSH ledger snapshot; the
        # default UM read chain selects only chat2api, so this is never read.
        dsh_snapshot=None,
        qoder_log="",
        m365_log="",
        dsh_merge_day="",
        # Magpie price paths are owned by the UM fee read surface, which this
        # composition root does not assemble; the summary route never reads them.
        magpie_price_snapshot=os.path.join(
            config.state_root, "magpie-prices.snapshot.json"),
        magpie_price_status=os.path.join(
            config.state_root, "magpie-prices.status.json"),
        parse_ts=pricing_rules.parse_ts,
        beijing_day=pricing_rules.beijing_day,
        day_start_ms=pricing_rules.day_start_ms,
        display_path=lambda path: os.path.basename(str(path or "")),
        estimate_record=estimate_record,
        price_table_loader=lambda: load_price_table(price_file, override_file),
    )
