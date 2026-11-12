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
    # P1 (2026-09-24): WHERE the efforts came from — "upstream" (the
    # source's own /v1/models metadata), "known" (the local KNOWN_MODELS
    # table, e.g. the UM directory), "probe"
    # (accepted by a billed capability probe), or None when nothing
    # provided them. The console must never claim every tier is
    # upstream-declared while some come from the local table.
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
        return f"{self.source}|{self.consumer}|{self.provider}"


@dataclass(frozen=True)
class SyncChange:
    binding: Binding
    kind: str
    model_id: str | None
    description: str
    model: CatalogModel | None = None
    previous_profile: dict[str, Any] | None = None
    # R6/D4 (2026-10-10): True when this update must PRESERVE the client's
    # current contextWindow: the client value cannot be proven to be the value
    # this sync last published (three-way comparison, see
    # context_publish_action), so the renderers must not overwrite it — the
    # update still publishes the other fields (alias/efforts).
    preserve_context: bool = False


@dataclass
class SyncPlan:
    changes: list[SyncChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    next_state: dict[str, Any] = field(default_factory=dict)
    # R6/D4 (2026-10-10): binding key -> model ids whose client context must
    # NOT be rewritten this round (conflict or unreadable client snapshot).
    # Read-only evidence for the preview/read face lives in context_conflicts.
    context_holds: dict[str, set[str]] = field(default_factory=dict)
    context_conflicts: list[dict[str, Any]] = field(default_factory=list)


# Consumer-surface key for ZCode's provider rule store: the caller passes the
# provider's ordered model ids from ``provider_config.json``
# (personalModelIds / modelOrder / provider model rules) under this key.  The core
# never reads files — a snapshot without the key simply has no rule-store surface.
RULE_SURFACE_KEY = "ruleModelIds"


def rule_surface_ids(consumer: dict[str, Any]) -> list[str]:
    """Ordered, duplicate-free rule-store ids of one consumer snapshot."""
    raw = consumer.get(RULE_SURFACE_KEY)
    if not isinstance(raw, (list, tuple)):
        return []
    ids: list[str] = []
    for value in raw:
        model_id = str(value or "").strip()
        if model_id and model_id not in ids:
            ids.append(model_id)
    return ids


# B0 (2026-11-01): the default assembly is UM-only. Qoder CN, Qoder Global,
# CLIProxy and M365 were retired together with their gateways, endpoints and
# credential files, so no binding for them is planned, probed or written any more.
# Their historical state keys (``managed`` / ``initializedBindings``) stay readable
# and untouched: a default run never deletes them and never re-activates them.
DEFAULT_BINDINGS: tuple[Binding, ...] = (
    Binding("um", "DSH", "openwebui", "UM", "（UM）"),
    Binding("um", "ZCode", "local:openwebui", "UM", "（UM）"),
)


def empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "initializedBindings": [],
        # ``managed[binding.key][model_id]``: {"profile": <the client profile
        # as last planned>, "retired": <bool tombstone>, "orphan": <bool>,
        # "published": {"context": <int|None>, "at": <UTC ISO>} — R6/D4
        # (2026-10-10) the value this sync last ACTUALLY published for that
        # client, written in the SAME state transaction as the client files and
        # read back from the rendered candidates (写后读回).  An ABSENT record
        # means history is unknown and is never guessed overwritable; a recorded
        # None means the field's absence was published.}
        "managed": {},
        "catalogCounts": {},
        "pending": [],
        "probes": {},
        # Retirement evidence for MANAGED models missing from a valid catalog:
        # they are only retired after two independent valid-catalog runs still
        # miss them (see MIN_RETIREMENT_RECHECK_SECONDS /
        # plan_catalog_mirror).  Orphans are retired on the first valid
        # catalog and never enter this watch.
        "retirementWatch": {},
        # Declared-context probe queue (see model_sync.seed_context_probes /
        # probe_context_queue): measured long-input evidence per model.
        "contextProbes": {},
        # Manual context overrides (Plan-Revision 7 step 6; contract recorded
        # in model_sync's module docstring): {"source|model_id": {"context":
        # <positive int>, "set_at": <UTC ISO>, "source": "manual"}}.  Applied
        # at the single run_sync application point (apply_context_overrides)
        # so mirror and probe seeding share the same effective value; written
        # only via model.context_override_set/clear under the existing lock
        # and state transaction.
        # Per-model display/context/effort settings (Rev8 contract 5/6,
        # 2026-10-02): key "source|model_id" -> {"draft": {...}|None,
        # "draftVersion": int, "draftSavedAt": iso|None, "applied": {...}|None,
        # "appliedVersion": int|None, "appliedAt": iso|None,
        # "appliedEvidence": str|None}.  Field shape (both sides):
        # {"alias": str|None, "contextSource": "declared"|"manual"|
        #  "measured"|None, "contextTokens": int|None, "defaultEffort":
        #  str|None}.  Saving writes ONLY the draft; the applied side moves
        # at an explicit apply that goes through run_sync's single client
        # transaction.  Scheduled/normal sync consumes APPLIED values only
        # and never publishes a draft; while a draft is pending (no applied
        # value) the mirror HOLDS the model's profile updates so the
        # client's current value is never silently reverted to the catalog
        # declaration (held keys, see plan_catalog_mirror).
        "modelSettings": {},
    }


# Rev8 契约 1（2026-10-02）：固定（pin）机制整体移除。v2 状态的 "pinned" 列表
# 由 migrate_state 升级时丢弃；v3 起状态不再有 pinned 字段，也不再有写入它的
# 通道（update_pin / model.pin_add / model.pin_remove / CLI --pin 全部删除）。
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
    version = int(state.get("version") or 1)
    if version < 2:
        # v1 → v2 (historical): probe records were rebuilt from scratch.
        state["probes"] = {}
    # v3 -> v4 (Rev8 contract 6, 2026-10-02) — and any stray legacy key on
    # a newer document (hand-edited state must not bypass the migration):
    # the manual context-override store migrates into per-model setting
    # DRAFTS.  A legacy override carries the maintainer's confirmed intent,
    # but whether it was ever published to a client is NOT provable from the
    # state alone (no publication evidence existed pre-Rev8), so every
    # migrated entry lands on the DRAFT side: scheduled syncs consume
    # APPLIED values only and never auto-publish an unconfirmed draft; the
    # client's current value keeps being held (plan_catalog_mirror
    # suppresses update planning for held ids), so an unapplied draft can
    # never silently revert the client to the catalog declaration.
    # draftVersion starts at 1 and the stored set_at becomes draftSavedAt —
    # an explicit apply then publishes it like any other confirmed change.
    # Malformed legacy values are dropped, never fabricated.
    legacy_overrides = state.pop("contextOverrides", None)
    settings = state.setdefault("modelSettings", {})
    if isinstance(legacy_overrides, dict):
        for key, entry in legacy_overrides.items():
            if not isinstance(key, str) or "|" not in key:
                continue
            if not isinstance(entry, dict):
                continue
            tokens = entry.get("context")
            if isinstance(tokens, bool) or not isinstance(tokens, int) \
                    or tokens <= 0:
                continue
            settings.setdefault(key, {
                "draft": {"alias": None,
                          "contextSource": "manual",
                          "contextTokens": tokens,
                          "defaultEffort": None},
                "draftVersion": 1,
                "draftSavedAt": entry.get("set_at"),
                "applied": None,
                "appliedVersion": None,
                "appliedAt": None,
                "appliedEvidence": None,
                # The legacy override WAS the maintainer's explicit context
                # decision: an explicit apply publishes it and releases the
                # R6/D4 conflict hold like any other decided draft.
                "contextDecided": True,
            })
    if version < STATE_VERSION:
        state.pop("pinned", None)
        state["version"] = STATE_VERSION
    return state


# Recheck interval for catalog-absence retirement evidence of MANAGED entries
# (models an earlier sync published): the two runs that both miss a managed model
# must be at least this far apart, so tight retries cannot fabricate a second
# evidence run inside one outage window.  Repeats inside the window only refresh
# lastMissing and never accumulate evidence.  Orphans (see plan_catalog_mirror) are
# not covered by this rule: they were never published by the sync, so the first
# valid catalog that misses them retires them directly.
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


def _managed_model_key(binding: Binding, model_id: str) -> str:
    return f"{binding.key}|{model_id}"


# ---------------------------------------------------------------------------
# Context publication protection (R6/D4, 2026-10-10)
#
# The console target value for a model's contextWindow is the effective catalog
# context (declaration or an explicitly APPLIED manual/measured override).  The
# client holds its own current value.  This sync also persists the value it last
# ACTUALLY published per (binding, model) — the publish baseline.  The three
# values are compared before anything is written:
#
#   * client value == target              -> nothing to write ("equal");
#   * client value absent                 -> publish (no manual value to protect);
#   * client value == baseline            -> provably ours, may be updated;
#   * anything else (baseline unknown, or
#     client value differs from it)        -> "conflict": NEVER overwrite and
#                                           NEVER delete; the user decides
#                                           which side wins.
#
# A missing baseline record is NOT "we published absence" — historical state
# (or a failed read) must never be guessed overwritable.  A recorded baseline of
# None means this sync published the field's absence.
# ---------------------------------------------------------------------------

CONTEXT_PUBLISH = "publish"
CONTEXT_EQUAL = "equal"
CONTEXT_CONFLICT = "conflict"
CONTEXT_UNREADABLE = "unreadable"


def client_context_of(profile: dict[str, Any] | None) -> int | None:
    """The client's current context value from one client profile snapshot.

    Reads ``contextWindow`` first, then ``limit.context`` (the two client
    shapes the renderers know).  A bool, a string, a non-positive value or a
    missing key is NOT evidence: it reads as None (unknown) — never as 0 and
    never coerced.
    """
    if not isinstance(profile, dict):
        return None
    context = profile.get("contextWindow")
    if context is None:
        limits = profile.get("limit")
        context = limits.get("context") if isinstance(limits, dict) else None
    if isinstance(context, bool) or not isinstance(context, int) or context <= 0:
        return None
    return context


def context_publish_action(
        target: int | None, client: int | None,
        published: dict[str, Any] | None,
        *, client_readable: bool = True) -> str:
    """Three-way decision for one model's contextWindow.

    ``target`` is the console target value (effective catalog context after the
    applied overrides), ``client`` the client's current value and ``published``
    the persisted publish-baseline RECORD — exactly the managed entry's
    ``published`` value: None means NO record exists (history unknown), a dict
    means a record exists and its ``context`` is what this sync last actually
    published (None inside the dict = we published the field's absence).  The two
    are never conflated.  ``client_readable`` False means the client snapshot
    could not be read for this binding.

    Returns one of:
      * "unreadable": the client snapshot is unavailable — never guess;
      * "equal": client == target — nothing to write;
      * "publish": the client holds no value (nothing to protect), or its
        value is provably the value this sync last published — the target may
        be written (or the field removed when the target is None and the
        baseline proves the client value was ours);
      * "conflict": the client value differs from the target and cannot be
        proven ours — never overwrite, never delete; the preview must show
        both values for the user to arbitrate.
    """
    if not client_readable:
        return CONTEXT_UNREADABLE
    if client == target:
        return CONTEXT_EQUAL
    if client is None:
        return CONTEXT_PUBLISH
    if isinstance(published, dict) and client == published_context(published):
        return CONTEXT_PUBLISH
    return CONTEXT_CONFLICT


def published_context(published: dict[str, Any] | None) -> int | None:
    """The context inside one publish-baseline record; malformed → None.

    A recorded None means "this sync published the field's absence" — a
    malformed stored value is never trusted as a baseline value.
    """
    if not isinstance(published, dict):
        return None
    context = published.get("context")
    if isinstance(context, bool) or not isinstance(context, int) or context <= 0:
        return None
    return context


def published_record(
        state: dict[str, Any] | None, binding: Binding,
        model_id: str) -> dict[str, Any] | None:
    """Read one managed entry's publish-baseline record; None when absent.

    The baseline lives in the managed registry entry (``published``) written in
    the SAME transaction as the client files after a successful publish.  A
    missing entry/record means history is unknown; a malformed record is
    returned as-is and the decision helper never trusts its value.
    """
    managed = (state or {}).get("managed")
    entries = managed.get(binding.key) if isinstance(managed, dict) else None
    entry = entries.get(model_id) if isinstance(entries, dict) else None
    published = entry.get("published") if isinstance(entry, dict) else None
    return published if isinstance(published, dict) else None


def _expected_name(model: CatalogModel, binding: Binding) -> str:
    base = model.display_name or model.model_id
    return base if base.endswith(binding.suffix) else base + binding.suffix


def _profile_needs_update(
        profile: dict[str, Any], model: CatalogModel, binding: Binding,
        *, preserve_context: bool = False) -> bool:
    name = str(profile.get("name") or "")
    # Rev11 单文件发布：ZCode 侧的实时权威文件（provider_config.json）不携带
    # 逐模型显示名——已安装客户端的模型规则 schema 没有 name 字段，客户端的
    # 重命名 API 只改模型 id。因此「客户端没有名字」不是待发布的改名证据：
    # 同步无法经规则库发布显示名，把它当差异会让计划每轮产生一条永远无法
    # 落盘的「更新」。客户端确实带名字的通道（DSH YAML、旧版 config.json
    # 迁移路径）仍照常比对。
    if name and name != _expected_name(model, binding):
        return True
    if model.context is not None and not preserve_context:
        # R6/D4：上下文差异只有在三方比较判定「可以发布」时才触发更新；
        # preserve_context=True（客户端值无法证明是我们发布的）时不规划上下文
        # 更新——渲染器原样保留客户端值，冲突单独登记。
        context = client_context_of(profile)
        if context != model.context:
            return True
    if model.efforts:
        # Rev11 单文件发布：只有客户端 profile 确实带有 reasoning 证据时才比对
        # 档位。ZCode 侧的实时权威文件（provider_config.json）不携带逐模型
        # 档位/默认档位（已安装客户端的模型规则 schema 的 optionSpecs 不在
        # 同步的发布面内），同步无法经它发布档位；把「客户端没有档位」当差异
        # 会让计划每轮产生一条永远无法落盘的「更新」。客户端确实带档位的
        # 通道（DSH YAML、旧版 config.json 迁移路径）仍照常比对。
        reasoning = profile.get("reasoning")
        configured = profile.get("reasoningEfforts")
        # DSH 的模型条目携带 reasoningEfforts（同步照常发布档位）；ZCode 的
        # 实时权威文件（provider_config.json）不携带逐模型档位/默认档位——见上。
        if (binding.consumer != "ZCode" or isinstance(configured, dict) or
                isinstance(reasoning, dict)):
            if configured is None:
                configured = (reasoning or {}).get("variants")
            if isinstance(configured, dict):
                configured = [key for key in configured
                              if key not in ("off", "none")]
            if tuple(configured or ()) != model.efforts:
                return True
            if binding.consumer == "ZCode" and isinstance(reasoning, dict):
                current_default = reasoning.get("defaultVariant")
                if current_default != (model.default_effort or model.efforts[0]):
                    return True
    return False


def plan_catalog_mirror(
        catalogs: dict[str, list[CatalogModel]],
        consumers: dict[str, dict[str, Any]],
        state: dict[str, Any] | None = None,
        bindings: Iterable[Binding] = DEFAULT_BINDINGS,
        model_filter: dict[str, set[str]] | None = None,
        held: set[str] | None = None,
        context_decided: set[str] | None = None) -> SyncPlan:
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

    R6/D4 (2026-10-10) context protection: every current model's context is
    decided by the three-way comparison (``context_publish_action``) between
    the console target (the effective catalog context), the client's current
    value and this sync's persisted publish baseline.  A conflicting client
    value is never overwritten or deleted: the update is suppressed (or planned
    with ``preserve_context`` when another field really changed) and the
    conflict is reported in ``context_conflicts``.  ``context_decided``
    ("source|model_id") marks models whose context the user explicitly
    decided (a saved draft carried a context field): their hold is lifted —
    the explicit decision is the arbitration.

    A consumer snapshot may carry the provider rule-store surface
    (``RULE_SURFACE_KEY``): the ids the consumer's rule store holds for that
    provider.  They are part of the provider's write surface, so an id that only
    the rule store holds and that a valid catalog no longer lists is retired too —
    otherwise the rule store alone would keep an offline model alive forever.
    """
    next_state = deepcopy(state or empty_state())
    for key, value in empty_state().items():
        next_state.setdefault(key, deepcopy(value))
    # Rev8: the pin mechanism is gone — a legacy v2 document that reaches the
    # planner (not via load_state) still has its pinned list dropped here, so
    # no planned next-state ever carries one back to disk.
    next_state.pop("pinned", None)
    held_keys = set(held or ())
    decided_keys = set(context_decided or ())

    initialized = set(next_state["initializedBindings"])
    managed: dict[str, dict[str, Any]] = next_state["managed"]
    watch: dict[str, dict[str, Any]] = next_state["retirementWatch"]
    if not isinstance(watch, dict):
        watch = {}
        next_state["retirementWatch"] = watch
    valid_catalogs: dict[str, dict[str, CatalogModel]] = {}
    plan = SyncPlan(next_state=next_state)
    now = datetime.now(timezone.utc)

    for source, models in catalogs.items():
        previous_count = next_state["catalogCounts"].get(source)
        valid, warning = validate_catalog(source, models, previous_count)
        if not valid:
            plan.warnings.append(warning or f"{source} 模型目录无效")
            continue
        valid_catalogs[source] = {model.model_id: model for model in models}
        next_state["catalogCounts"][source] = len(models)

    for binding in bindings:
        catalog = valid_catalogs.get(binding.source)
        if catalog is None:
            continue
        consumer = consumers.get(binding.key)
        if consumer is None:
            plan.warnings.append(f"缺少消费方配置：{binding.key}")
            continue
        current_models: dict[str, dict[str, Any]] = consumer.get("models") or {}
        rule_ids = rule_surface_ids(consumer)
        binding_managed = managed.setdefault(binding.key, {})
        selected = (model_filter or {}).get(binding.source)

        if binding.key not in initialized:
            # Adopt only entries that are proven to belong to this source.  This
            # includes non-publishable entries such as gpt-image-2 so the first
            # mirror can retire an invalid legacy placement.  Under a write-
            # scope model filter only the selected ids are adopted.
            for model_id in set(current_models).intersection(catalog):
                if selected is not None and model_id not in selected:
                    continue
                binding_managed.setdefault(
                    model_id, {"profile": deepcopy(current_models[model_id])})
            initialized.add(binding.key)

        # A model back in a valid catalog clears its retirement watch.  This
        # hygiene runs regardless of the write scope: it only removes stale
        # watch state and never produces a change.
        watch_prefix = binding.key + "|"
        for watch_key in [k for k in watch if k.startswith(watch_prefix)]:
            if watch_key[len(watch_prefix):] in catalog:
                watch.pop(watch_key, None)

        current_provider_name = consumer.get("providerName")
        if (selected is None and
                current_provider_name != binding.provider_name):
            plan.changes.append(SyncChange(
                binding=binding,
                kind="provider_name",
                model_id=None,
                description=(f"{binding.consumer} {binding.provider} 显示名 → "
                             f"{binding.provider_name}"),
            ))

        for model_id, model in catalog.items():
            if selected is not None and model_id not in selected:
                continue
            state_entry = binding_managed.get(model_id)
            current = current_models.get(model_id)

            if not model.publish:
                if state_entry is not None and current is not None:
                    # The source itself declares the model unpublishable: an
                    # immediate, confirmed retirement. The managed key keeps a
                    # tombstone until the removal is actually applied
                    # (run_sync deletes applied tombstones) — deleting at plan
                    # time would let a pending ZCode change strand the config
                    # entry as an untouchable manual extra.
                    state_entry["profile"] = deepcopy(current)
                    state_entry["retired"] = True
                    plan.changes.append(SyncChange(
                        binding, "remove", model_id,
                        f"{binding.consumer} 退役 {model_id}"
                        "（来源标记为不可发布）",
                        model=model,
                        previous_profile=deepcopy(current)))
                continue

            if state_entry is None:
                state_entry = {"profile": deepcopy(current) if current else {}}
                binding_managed[model_id] = state_entry
            elif state_entry.get("retired") is True:
                # Confirmed-retired model back in the catalog: clear the
                # tombstone and let the normal add/update path re-publish it.
                state_entry.pop("retired", None)
                watch.pop(_managed_model_key(binding, model_id), None)

            if current is None:
                plan.changes.append(SyncChange(
                    binding, "add", model_id,
                    f"{binding.consumer} 发布 {model_id}",
                    model=model,
                    previous_profile=deepcopy(state_entry.get("profile") or {})))
            else:
                state_entry["profile"] = deepcopy(current)
                # R6/D4 三方比较：客户端当前值 / 控制台目标值 / 上次实际发布
                # 基准。冲突（或客户端快照不可读）时本模型上下文绝不覆盖，
                # 更新项携带 preserve_context；显式决定过上下文的模型（用户
                # 保存的草稿带上下文字段）解除保留——该决定即裁定。
                decision_key = f"{binding.source}|{model_id}"
                published = published_record(next_state, binding, model_id)
                client_value = client_context_of(current)
                action = context_publish_action(
                    model.context, client_value, published)
                if action in (CONTEXT_CONFLICT, CONTEXT_UNREADABLE):
                    plan.context_conflicts.append({
                        "source": binding.source,
                        "model_id": model_id,
                        "consumer": binding.consumer,
                        "target": model.context,
                        "client": client_value,
                        "baseline": published_context(published),
                        "baseline_recorded": isinstance(published, dict),
                        "decided": decision_key in decided_keys,
                        # "console"：用户显式决定过控制台值（本次发布即裁定）；
                        # "pending"：冲突待用户裁定（本轮不覆盖）。
                        "resolution": ("console" if decision_key in decided_keys
                                       else "pending"),
                        "action": action,
                    })
                    if decision_key not in decided_keys:
                        plan.context_holds.setdefault(
                            binding.key, set()).add(model_id)
                if f"{binding.source}|{model_id}" in held_keys:
                    # Rev8 contract 6: a pending settings draft HOLDS this
                    # model's display/context/effort profile — no update is
                    # planned (定时同步只消费已应用值，草稿未被应用前客户端
                    # 当前值不被回撤；model_settings_apply 是唯一发布通道).
                    # The hold never covers add/remove/retirement.
                    continue
                held_context = model_id in plan.context_holds.get(
                    binding.key, ())
                if _profile_needs_update(current, model, binding,
                                         preserve_context=held_context):
                    plan.changes.append(SyncChange(
                        binding, "update", model_id,
                        f"{binding.consumer} 更新 {model_id}",
                        model=model,
                        previous_profile=deepcopy(current),
                        preserve_context=held_context))

        # Catalog-absence retirement.  The provider's write surface is the whole
        # model surface of that managed provider — the client's own model map
        # PLUS the provider rule store — so a stale id is retired no matter
        # which of the two holds it.  Two evidence classes:
        #
        # * Managed entries (an earlier sync published them, so the state carries
        #   a profile): the two-run independent-evidence rule stays.  A single
        #   omission can be an upstream flake, so the first valid-catalog run
        #   that misses the model only arms the retirement watch (shown as
        #   "目录缺失待退役", no change) and the removal fires on the second
        #   independent run at least MIN_RETIREMENT_RECHECK_SECONDS later.
        #   (Rev8: the old pinned exemption is gone entirely — a pin never
        #   exempted retirement anyway, it only kept a stale whitelist entry
        #   meanwhile.)
        # * Orphans (on a client surface but never recorded as managed, e.g.
        #   residue of an older era, a hand edit, or a rule-store entry): the
        #   maintainer's decision is 下线直接移除 — a valid catalog is
        #   authoritative for the managed provider, so the first run that misses
        #   them retires them and no watch is armed.
        #
        # Both classes only run for a binding whose source produced a VALID
        # catalog this round (non-empty, duplicate-free, inside the drop
        # threshold); a failed, empty or otherwise invalid catalog plans no
        # retirement at all and only reports the warning.
        surface_ids = list(current_models)
        for model_id in rule_ids:
            if model_id not in current_models:
                surface_ids.append(model_id)
        # Managed records whose id is on no client surface any more still enter
        # the absence check so stale bookkeeping is dropped (see below).
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
                # No client surface holds the id any more: the managed record
                # is stale bookkeeping and is dropped without any change.
                binding_managed.pop(model_id, None)
                watch.pop(watch_key, None)
                continue
            confirmed = (state_entry is not None and
                         state_entry.get("retired") is True)
            if state_entry is None or confirmed:
                # Orphan residue, or a retirement already confirmed whose
                # application the client was running for: retire directly.
                if current is not None and model_id in rule_ids:
                    where = "主配置与供应商规则库"
                elif current is not None:
                    where = "主配置"
                else:
                    where = "供应商规则库"
                if confirmed:
                    description = (
                        f"{binding.consumer} 退役 {model_id}"
                        "（已确认退役，客户端配置待应用）")
                else:
                    description = (
                        f"{binding.consumer} 退役 {model_id}"
                        f"（来源目录已不含该模型，直接退役；残留：{where}）")
                plan.changes.append(SyncChange(
                    binding, "remove", model_id, description,
                    previous_profile=(deepcopy(current)
                                      if current is not None else None)))
                binding_managed[model_id] = {
                    "profile": deepcopy(current) if current is not None else {},
                    "retired": True,
                    "orphan": not confirmed,
                }
                watch.pop(watch_key, None)
                continue
            entry = watch.get(watch_key)
            if entry is None:
                watch[watch_key] = {
                    "firstMissing": now.isoformat(),
                    "lastMissing": now.isoformat(),
                    "evidenceRuns": 1,
                }
                continue
            first = _parse_state_time(entry.get("firstMissing"), now)
            if (now - first).total_seconds() < MIN_RETIREMENT_RECHECK_SECONDS:
                # Same outage window: refresh only, never accumulate evidence.
                entry["lastMissing"] = now.isoformat()
                continue
            entry["evidenceRuns"] = int(entry.get("evidenceRuns") or 1) + 1
            entry["lastMissing"] = now.isoformat()
            if int(entry["evidenceRuns"]) < 2:
                continue
            if current is not None:
                binding_managed[model_id]["profile"] = deepcopy(current)
            plan.changes.append(SyncChange(
                binding, "remove", model_id,
                f"{binding.consumer} 退役 {model_id}"
                "（第二次独立目录证据确认下线）",
                previous_profile=(deepcopy(current)
                                  if current is not None else None)))
            # Tombstone, not delete: the managed key survives until run_sync
            # actually applies the removal (a pending ZCode change must not
            # strand the config entry as an untouchable manual extra).
            binding_managed[model_id]["retired"] = True
            watch.pop(watch_key, None)

    next_state["initializedBindings"] = sorted(initialized)
    return plan
