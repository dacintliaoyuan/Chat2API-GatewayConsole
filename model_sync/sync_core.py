# -*- coding: utf-8 -*-
"""Pure model-catalog and state primitives.

Ticket #32 retires the client consumer chain and its mirror planning.
The retained module knows nothing about HTTP, processes, or user files: callers
provide normalized source catalogs and persisted local state; the result is the
catalog validation verdict and the next state document.

The state file is owned by ``model_sync.sync``: it carries the catalog counts,
the declared-context probe queue and the manual context overrides.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class CatalogModel:
    source: str
    model_id: str
    context: int | None = None
    efforts: tuple[str, ...] = ()
    default_effort: str | None = None
    publish: bool = True
    display_name: str | None = None
    # P1 (2026-09-24): WHERE the efforts came from — "upstream" (the
    # source's own /v1/models metadata), "known" (the local KNOWN_MODELS
    # table, e.g. the UM directory), "probe" (accepted by a billed capability
    # probe), or None when nothing provided them. The console must never claim
    # every tier is upstream-declared while some come from the local table.
    efforts_source: str | None = None


# Reasoning-effort capabilities verified manually on 2026-09-02.
KNOWN_MODELS: dict[str, dict[str, Any]] = {
    "DeepSeek-V4-Flash-0731": {
        "efforts": ["low", "medium", "high", "max"], "default": "high"},
    "GLM-5.2-NVFP4": {
        "efforts": ["low", "medium", "high", "xhigh", "max"], "default": "medium"},
    "Qwen3.8-27B": {
        "efforts": ["low", "medium", "xhigh"], "default": "xhigh"},
    "gpt-oss-120b": {
        "efforts": ["low", "medium", "high"], "default": "medium"},
}

VARIANT_SUFFIXES = ("-Fast", "-Low", "-Medium", "-High")


def empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        # Catalog counts seed the declared-context probe queue; the queue is
        # measured long-input evidence per model.
        "catalogCounts": {},
        # Declared-context probe queue (see model_sync.seed_context_probes /
        # probe_context_queue): measured long-input evidence per model.
        "contextProbes": {},
        # Manual context overrides (Plan-Revision 7 step 6; contract recorded
        # in model_sync's module docstring): {"source|model_id": {"context":
        # <positive int>, "set_at": <UTC ISO>, "source": "manual"}}.  Written
        # only via model.context_override_set/clear under the existing lock and
        # state transaction.
        "contextOverrides": {},
    }


# Rev8 契约 1（2026-10-02）：固定（pin）机制整体移除。v2 状态的 "pinned" 列表
# 由 migrate_state 升级时丢弃；v3 起状态不再有 pinned 字段，也不再有写入它的
# 通道（update_pin / model.pin_add / model.pin_remove / CLI --pin 全部删除）。
STATE_VERSION = 4


def migrate_state(state: dict[str, Any]) -> dict[str, Any]:
    """Versioned, idempotent upgrade of a loaded state document (B8-2).

    v2 → v3: the pinned-model mechanism is REMOVED. The old ``pinned`` list is
    dropped wholesale: a pin never exempted retirement (it only kept a stale
    rule-store whitelist entry while retirement evidence accumulated), so dropping
    it cannot strand a published model.  The migration runs on the in-memory
    document load_state hands out; the next transactional state write persists
    the upgraded document, and a failed write leaves the original file untouched
    (失败不丢唯一状态).  Re-running on an already-migrated (or fresh) state is a
    no-op.
    """
    if not isinstance(state, dict):
        return state
    version = int(state.get("version") or 1)
    if version < 2:
        # v1 → v2 (historical): probe records were rebuilt from scratch.
        state["probes"] = {}
    if version < STATE_VERSION:
        state.pop("pinned", None)
        state["version"] = STATE_VERSION
    return state


def validate_catalog(
        source: str,
        models: Iterable[CatalogModel],
        previous_count: int | None = None,
        max_drop_ratio: float = 0.5) -> tuple[bool, str | None]:
    materialized = list(models)
    if not materialized:
        return False, f"{source} 模型目录为空"
    ids = [model.model_id for model in materialized]
    if any(not model_id.strip() for model_id in ids):
        return False, f"{source} 模型目录含空 ID"
    if len(set(ids)) != len(ids):
        return False, f"{source} 模型目录含重复 ID"
    if previous_count and previous_count > 0:
        drop_ratio = (previous_count - len(ids)) / previous_count
        if drop_ratio > max_drop_ratio:
            return False, (
                f"{source} 模型数从 {previous_count} 降至 {len(ids)}，"
                f"超过 {int(max_drop_ratio * 100)}% 安全阈值")
    return True, None
