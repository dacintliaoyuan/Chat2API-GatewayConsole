# -*- coding: utf-8 -*-
"""Pure model-catalog mirroring rules.

The module knows nothing about HTTP, processes, or user files.  Callers provide
normalized source catalogs, normalized consumer snapshots, and persisted local
state; the result is an immutable change plan plus the next state document.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    efforts_source: str | None = None

@dataclass(frozen=True)
class Binding:
    source: str
    consumer: str
    provider: str
    provider_name: str
    suffix: str

    @property
    def key(self) -> str:
        return f'{self.source}|{self.consumer}|{self.provider}'

@dataclass(frozen=True)
class SyncChange:
    binding: Binding
    kind: str
    model_id: str | None
    description: str
    model: CatalogModel | None = None
    previous_profile: dict[str, Any] | None = None

@dataclass
class SyncPlan:
    changes: list[SyncChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    next_state: dict[str, Any] = field(default_factory=dict)
RULE_SURFACE_KEY = 'ruleModelIds'

def rule_surface_ids(consumer: dict[str, Any]) -> list[str]:
    """Ordered, duplicate-free rule-store ids of one consumer snapshot."""
    raw = consumer.get(RULE_SURFACE_KEY)
    if not isinstance(raw, (list, tuple)):
        return []
    ids: list[str] = []
    for value in raw:
        model_id = str(value or '').strip()
        if model_id and model_id not in ids:
            ids.append(model_id)
    return ids
DEFAULT_BINDINGS: tuple[Binding, ...] = (Binding('um', 'ZCode', 'local:openwebui', 'UM', '（UM）'),)

def empty_state() -> dict[str, Any]:
    return {'version': STATE_VERSION, 'initializedBindings': [], 'managed': {}, 'catalogCounts': {}, 'pending': [], 'probes': {}, 'retirementWatch': {}, 'contextProbes': {}, 'modelSettings': {}}
STATE_VERSION = 4

def migrate_state(state: dict[str, Any]) -> dict[str, Any]:
    """Versioned, idempotent upgrade of a loaded state document (B8-2).

    v2 → v3: the pinned-model mechanism is REMOVED. The old ``pinned`` list
    is dropped wholesale: a pin never exempted retirement (it only kept a
    stale rule-store whitelist entry while the two-run retirement evidence
    accumulated), so dropping it cannot strand a published model — existing
    published models stay managed by the normal mirror, and manual
    non-managed entries were never governed by pins. The migration runs on
    the in-memory document load_state hands out; the next transactional
    state write persists the upgraded document, and a failed write leaves
    the original file untouched (失败不丢唯一状态). Re-running on an
    already-migrated (or fresh) state is a no-op.
    """
    if not isinstance(state, dict):
        return state
    version = int(state.get('version') or 1)
    if version < 2:
        state['probes'] = {}
    legacy_overrides = state.pop('contextOverrides', None)
    settings = state.setdefault('modelSettings', {})
    if isinstance(legacy_overrides, dict):
        for key, entry in legacy_overrides.items():
            if not isinstance(key, str) or '|' not in key:
                continue
            if not isinstance(entry, dict):
                continue
            tokens = entry.get('context')
            if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
                continue
            settings.setdefault(key, {'draft': {'alias': None, 'contextSource': 'manual', 'contextTokens': tokens, 'defaultEffort': None}, 'draftVersion': 1, 'draftSavedAt': entry.get('set_at'), 'applied': None, 'appliedVersion': None, 'appliedAt': None, 'appliedEvidence': None})
    if version < STATE_VERSION:
        state.pop('pinned', None)
        state['version'] = STATE_VERSION
    return state
MIN_RETIREMENT_RECHECK_SECONDS = 1800

def _parse_state_time(value: Any, fallback: datetime) -> datetime:
    """Parse a persisted ISO timestamp; corrupt/naive values fall back."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed

def validate_catalog(source: str, models: Iterable[CatalogModel], previous_count: int | None=None, max_drop_ratio: float=0.5) -> tuple[bool, str | None]:
    materialized = list(models)
    if not materialized:
        return (False, f'{source} 模型目录为空')
    ids = [model.model_id for model in materialized]
    if any((not model_id.strip() for model_id in ids)):
        return (False, f'{source} 模型目录含空 ID')
    if len(set(ids)) != len(ids):
        return (False, f'{source} 模型目录含重复 ID')
    if previous_count and previous_count > 0:
        drop_ratio = (previous_count - len(ids)) / previous_count
        if drop_ratio > max_drop_ratio:
            return (False, f'{source} 模型数从 {previous_count} 降至 {len(ids)}，超过 {int(max_drop_ratio * 100)}% 安全阈值')
    return (True, None)

def _managed_model_key(binding: Binding, model_id: str) -> str:
    return f'{binding.key}|{model_id}'

def _expected_name(model: CatalogModel, binding: Binding) -> str:
    base = model.display_name or model.model_id
    return base if base.endswith(binding.suffix) else base + binding.suffix

def _profile_needs_update(profile: dict[str, Any], model: CatalogModel, binding: Binding) -> bool:
    name = str(profile.get('name') or '')
    if name and name != _expected_name(model, binding):
        return True
    if model.context is not None:
        context = profile.get('contextWindow')
        if context is None:
            context = (profile.get('limit') or {}).get('context')
        if context != model.context:
            return True
    if model.efforts:
        reasoning = profile.get('reasoning')
        configured = profile.get('reasoningEfforts')
        if binding.consumer != 'ZCode' or isinstance(configured, dict) or isinstance(reasoning, dict):
            if configured is None:
                configured = (reasoning or {}).get('variants')
            if isinstance(configured, dict):
                configured = [key for key in configured if key not in ('off', 'none')]
            if tuple(configured or ()) != model.efforts:
                return True
            if binding.consumer == 'ZCode' and isinstance(reasoning, dict):
                current_default = reasoning.get('defaultVariant')
                if current_default != (model.default_effort or model.efforts[0]):
                    return True
    return False

def plan_catalog_mirror(catalogs: dict[str, list[CatalogModel]], consumers: dict[str, dict[str, Any]], state: dict[str, Any] | None=None, bindings: Iterable[Binding]=DEFAULT_BINDINGS, model_filter: dict[str, set[str]] | None=None, held: set[str] | None=None) -> SyncPlan:
    """Plan safe catalog mirroring for every valid source binding.

    ``model_filter`` optionally restricts the write surface per source: with
    a filter active for a source, add/update/remove changes are limited to
    the selected model ids, provider-name changes are suppressed for that
    source, and retirement evidence is only accumulated or confirmed for the
    selected models.  Catalogs themselves stay unfiltered so the drop-ratio
    guard keeps seeing the full directory.  ``held`` ("source|model_id" keys
    carrying a pending settings draft with no applied value) suppresses UPDATE
    planning for those ids only: the client's current profile is never
    silently reverted toward the catalog while its draft waits for an explicit
    apply (Rev8 契约 6).  add/remove/retirement are NOT held — retirement
    evidence stays authoritative.

    A consumer snapshot may carry the provider rule-store surface
    (``RULE_SURFACE_KEY``): the ids the consumer's rule store holds for that
    provider.  They are part of the provider's write surface, so an id that only
    the rule store holds and that a valid catalog no longer lists is retired too —
    otherwise the rule store alone would keep an offline model alive forever.
    """
    next_state = deepcopy(state or empty_state())
    for key, value in empty_state().items():
        next_state.setdefault(key, deepcopy(value))
    next_state.pop('pinned', None)
    held_keys = set(held or ())
    initialized = set(next_state['initializedBindings'])
    managed: dict[str, dict[str, Any]] = next_state['managed']
    watch: dict[str, dict[str, Any]] = next_state['retirementWatch']
    if not isinstance(watch, dict):
        watch = {}
        next_state['retirementWatch'] = watch
    valid_catalogs: dict[str, dict[str, CatalogModel]] = {}
    plan = SyncPlan(next_state=next_state)
    now = datetime.now(timezone.utc)
    for source, models in catalogs.items():
        previous_count = next_state['catalogCounts'].get(source)
        valid, warning = validate_catalog(source, models, previous_count)
        if not valid:
            plan.warnings.append(warning or f'{source} 模型目录无效')
            continue
        valid_catalogs[source] = {model.model_id: model for model in models}
        next_state['catalogCounts'][source] = len(models)
    for binding in bindings:
        catalog = valid_catalogs.get(binding.source)
        if catalog is None:
            continue
        consumer = consumers.get(binding.key)
        if consumer is None:
            plan.warnings.append(f'缺少消费方配置：{binding.key}')
            continue
        current_models: dict[str, dict[str, Any]] = consumer.get('models') or {}
        rule_ids = rule_surface_ids(consumer)
        binding_managed = managed.setdefault(binding.key, {})
        selected = (model_filter or {}).get(binding.source)
        if binding.key not in initialized:
            for model_id in set(current_models).intersection(catalog):
                if selected is not None and model_id not in selected:
                    continue
                binding_managed.setdefault(model_id, {'profile': deepcopy(current_models[model_id])})
            initialized.add(binding.key)
        watch_prefix = binding.key + '|'
        for watch_key in [k for k in watch if k.startswith(watch_prefix)]:
            if watch_key[len(watch_prefix):] in catalog:
                watch.pop(watch_key, None)
        current_provider_name = consumer.get('providerName')
        if selected is None and current_provider_name != binding.provider_name:
            plan.changes.append(SyncChange(binding=binding, kind='provider_name', model_id=None, description=f'{binding.consumer} {binding.provider} 显示名 → {binding.provider_name}'))
        for model_id, model in catalog.items():
            if selected is not None and model_id not in selected:
                continue
            state_entry = binding_managed.get(model_id)
            current = current_models.get(model_id)
            if not model.publish:
                if state_entry is not None and current is not None:
                    state_entry['profile'] = deepcopy(current)
                    state_entry['retired'] = True
                    plan.changes.append(SyncChange(binding, 'remove', model_id, f'{binding.consumer} 退役 {model_id}（来源标记为不可发布）', model=model, previous_profile=deepcopy(current)))
                continue
            if state_entry is None:
                state_entry = {'profile': deepcopy(current) if current else {}}
                binding_managed[model_id] = state_entry
            elif state_entry.get('retired') is True:
                state_entry.pop('retired', None)
                watch.pop(_managed_model_key(binding, model_id), None)
            if current is None:
                plan.changes.append(SyncChange(binding, 'add', model_id, f'{binding.consumer} 发布 {model_id}', model=model, previous_profile=deepcopy(state_entry.get('profile') or {})))
            else:
                state_entry['profile'] = deepcopy(current)
                if f'{binding.source}|{model_id}' in held_keys:
                    continue
                if _profile_needs_update(current, model, binding):
                    plan.changes.append(SyncChange(binding, 'update', model_id, f'{binding.consumer} 更新 {model_id}', model=model, previous_profile=deepcopy(current)))
        surface_ids = list(current_models)
        for model_id in rule_ids:
            if model_id not in current_models:
                surface_ids.append(model_id)
        for model_id in list(binding_managed):
            if model_id not in current_models and model_id not in rule_ids:
                surface_ids.append(model_id)
        for model_id in surface_ids:
            if model_id in catalog:
                continue
            if selected is not None and model_id not in selected:
                continue
            state_entry = binding_managed.get(model_id)
            current = current_models.get(model_id)
            watch_key = _managed_model_key(binding, model_id)
            if current is None and model_id not in rule_ids:
                binding_managed.pop(model_id, None)
                watch.pop(watch_key, None)
                continue
            confirmed = state_entry is not None and state_entry.get('retired') is True
            if state_entry is None or confirmed:
                if current is not None and model_id in rule_ids:
                    where = '主配置与供应商规则库'
                elif current is not None:
                    where = '主配置'
                else:
                    where = '供应商规则库'
                if confirmed:
                    description = f'{binding.consumer} 退役 {model_id}（已确认退役，客户端配置待应用）'
                else:
                    description = f'{binding.consumer} 退役 {model_id}（来源目录已不含该模型，直接退役；残留：{where}）'
                plan.changes.append(SyncChange(binding, 'remove', model_id, description, previous_profile=deepcopy(current) if current is not None else None))
                binding_managed[model_id] = {'profile': deepcopy(current) if current is not None else {}, 'retired': True, 'orphan': not confirmed}
                watch.pop(watch_key, None)
                continue
            entry = watch.get(watch_key)
            if entry is None:
                watch[watch_key] = {'firstMissing': now.isoformat(), 'lastMissing': now.isoformat(), 'evidenceRuns': 1}
                continue
            first = _parse_state_time(entry.get('firstMissing'), now)
            if (now - first).total_seconds() < MIN_RETIREMENT_RECHECK_SECONDS:
                entry['lastMissing'] = now.isoformat()
                continue
            entry['evidenceRuns'] = int(entry.get('evidenceRuns') or 1) + 1
            entry['lastMissing'] = now.isoformat()
            if int(entry['evidenceRuns']) < 2:
                continue
            if current is not None:
                binding_managed[model_id]['profile'] = deepcopy(current)
            plan.changes.append(SyncChange(binding, 'remove', model_id, f'{binding.consumer} 退役 {model_id}（第二次独立目录证据确认下线）', previous_profile=deepcopy(current) if current is not None else None))
            binding_managed[model_id]['retired'] = True
            watch.pop(watch_key, None)
    next_state['initializedBindings'] = sorted(initialized)
    return plan
