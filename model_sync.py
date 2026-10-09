# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from gateway_credentials import chat2api_local_key
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from model_config_adapters import ConfigPublishError, ZCODE_MANAGED_PROVIDER_DEFAULTS, apply_config_transaction, render_zcode_sync, replace_with_retry, zcode_managed_provider_template, zcode_running
from model_reconcile import KNOWN_MODELS, VARIANT_SUFFIXES
from model_sync_core import CatalogModel, DEFAULT_BINDINGS, RULE_SURFACE_KEY, empty_state, migrate_state, plan_catalog_mirror, validate_catalog
from zcode_file_lock import zcode_file_lock
from zcode_hot_sync import zcode_hot_sync_admission
HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / 'data'
STATE_FILE = DATA_DIR / 'model-sync-state.json'
RESULT_FILE = DATA_DIR / 'model-sync-last-result.json'
PREVIEW_FILE = DATA_DIR / 'model-sync-last-preview.json'
HISTORY_FILE = DATA_DIR / 'model-sync-history.jsonl'
BACKUP_DIR = DATA_DIR / 'model-sync-backups'
LOCK_FILE = DATA_DIR / 'model-sync.lock'
ZCODE_JSON = Path.home() / '.zcode' / 'v2' / 'config.json'

def _code_stamp() -> str:
    """Fingerprint of the running model_sync.py — history entries record which
    code produced them (a stale-copy invocation regressed the catalog once)."""
    import hashlib
    return hashlib.md5(Path(__file__).read_bytes()).hexdigest()[:12]
CODE_STAMP = _code_stamp()
ZCODE_PROVIDER_CONFIG = Path.home() / '.zcode' / 'v2' / 'provider_config.json'
LEGACY_CONFIG_MIGRATION_VERIFIED = False

def _legacy_config_write_allowed(store_problem: str | None) -> bool:
    """旧版 config.json 写面是否成立（Rev11 迁移门）。

    只有规则库缺席（客户端启动时一次性导入 config.json 的旧版本迁移形态）
    **且**迁移兼容性已取证时才成立；其余不可用形态（不可读/无法解析）与
    权威文件在场时都不成立。证据未补齐前恒为 False。
    """
    return store_problem == '文件缺席' and LEGACY_CONFIG_MIGRATION_VERIFIED
UM_MODELS_URL = 'http://127.0.0.1:8000/v1/models'
UM_CHAT_URL = 'http://127.0.0.1:8000/v1/chat/completions'
PROBE_LEVELS = {'um': ('low', 'medium', 'high', 'xhigh', 'max')}
MAX_PROBE_REQUESTS = 50
MAX_PROBE_ATTEMPTS = 3
KNOWN_SOURCES = ('um',)
KNOWN_CONSUMERS = ('ZCode',)
PAUSED_SOURCES = frozenset({*()})
ACTIVE_SOURCES = tuple((source for source in KNOWN_SOURCES if source not in PAUSED_SOURCES))
ACTIVE_BINDINGS = tuple((binding for binding in DEFAULT_BINDINGS if binding.source in ACTIVE_SOURCES))
PROBE_INPUT_STEPS = (4000, 16000, 64000, 256000)
CONTEXT_PROBE_MAX_TOKENS = 64
CONTEXT_PROBE_TRANSIENT_STATUSES = frozenset({401, 403, 408, 409, 425, 429})
CONTEXT_PROBE_ENDPOINTS = {'um': (UM_CHAT_URL, chat2api_local_key)}
PROBE_SCHEME_REVISION = 1
PROBE_HISTORY_MAX = 20

class SyncLock:

    def __enter__(self):
        import msvcrt
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.handle = open(LOCK_FILE, 'a+', encoding='utf-8')
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write('0')
            self.handle.flush()
        self.handle.seek(0)
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.handle.close()
            raise RuntimeError('另一个模型同步正在运行') from exc
        return self

    def __exit__(self, *_):
        import msvcrt
        self.handle.seek(0)
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.handle.close()

def _get_json(url: str, token: str | None=None, timeout: int=10) -> Any:
    request = urllib.request.Request(url)
    if token:
        request.add_header('Authorization', 'Bearer ' + token)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)

def _post_json(url: str, body: dict[str, Any], timeout: int=90, token: str | None=None) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'), headers={'Content-Type': 'application/json'}, method='POST')
    if token:
        request.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {'status': response.status, 'body': json.load(response)}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')[:300]
        return {'status': exc.code, 'body': {'error': detail}}

def _context_of(raw: dict[str, Any]) -> int | None:
    value = raw.get('max_model_len') or raw.get('context_length') or raw.get('contextWindow') or raw.get('context_window')
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None

def _known_efforts(model_id: str) -> tuple[tuple[str, ...], str | None]:
    known = KNOWN_MODELS.get(model_id) or {}
    return (tuple(known.get('efforts') or ()), known.get('default'))

def _effort_names(raw_efforts: Any, *, allow_max: bool=False) -> tuple[str, ...]:
    names: list[str] = []
    for item in raw_efforts or ():
        value = item.get('effort') if isinstance(item, dict) else item
        value = str(value or '').strip().lower()
        excluded = ('none', 'off') if allow_max else ('none', 'off', 'max')
        if value and value not in excluded and (value not in names):
            names.append(value)
    return tuple(names)

def collect_catalogs(sources=None):
    if sources is not None and (not isinstance(sources, (list, tuple)) or any((s != 'um' for s in sources))):
        raise ValueError('仅支持 chat2api 模型来源 um')
    catalogs, warnings = ({}, [])
    if sources == []:
        return (catalogs, warnings)
    try:
        payload = _get_json(UM_MODELS_URL, token=chat2api_local_key())
        models = []
        for raw in payload.get('data') or []:
            mid = str(raw.get('id') or '').strip()
            if not mid or _is_virtual_model(mid):
                continue
            efforts, default = _known_efforts(mid)
            models.append(CatalogModel('um', mid, _context_of(raw), efforts, default, efforts_source='known' if efforts else None))
        catalogs['um'] = models
    except Exception as exc:
        warnings.append('chat2api目录读取失败：' + type(exc).__name__)
    return (catalogs, warnings)

def _registered_provider_rule(binding: Any, options: dict[str, str] | None) -> dict[str, Any]:
    base_url = api_key = None
    if isinstance(options, dict):
        base_url = str(options.get('baseURL') or '').strip() or None
        api_key = str(options.get('apiKey') or '').strip() or None
    if not base_url or not api_key:
        raise ValueError(f'ZCode 受管 provider {binding.provider} 在规则库缺席，且缺少登记所需的连接信息（baseURL/apiKey）；本轮拒绝静默跳过')
    return {'providerId': binding.provider, 'providerName': binding.provider_name, 'enabled': True, 'config': {'group': 'standard-personal', 'access': {'type': 'api-key', 'apiKey': api_key}, 'api': {'type': 'openai-chat-completions', 'baseUrl': base_url}, 'personalModelIds': [], 'modelOrder': []}}

def render_provider_config_sync(raw: str, catalogs: dict[str, list[CatalogModel]], bindings=DEFAULT_BINDINGS, managed: dict[str, set[str]] | None=None, connections: dict[str, dict[str, str]] | None=None) -> str:
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
    cfg = doc.get('config')
    if not isinstance(cfg, dict):
        if cfg is None:
            cfg = {}
            doc['config'] = cfg
        else:
            raise ValueError('ZCode 规则库 config 结构异常，拒绝登记')
    rules_container = cfg.get('providerConfigRules')
    if not isinstance(rules_container, dict):
        if rules_container is None:
            rules_container = {}
            cfg['providerConfigRules'] = rules_container
        else:
            raise ValueError('ZCode 规则库 providerConfigRules 结构异常，拒绝登记')
    provider_rules = rules_container.get('providerRules')
    if not isinstance(provider_rules, list):
        if provider_rules is None:
            provider_rules = []
            rules_container['providerRules'] = provider_rules
        else:
            raise ValueError('ZCode 规则库 providerRules 结构异常，拒绝登记')
    rules_cfg = cfg.setdefault('modelConfigRules', {})
    model_rules = rules_cfg.get('providerModelRules')
    if not isinstance(model_rules, list):
        model_rules = []
        rules_cfg['providerModelRules'] = model_rules
    managed = managed or {}
    rebuilt_rules = list(model_rules)
    for binding in bindings:
        if binding.consumer != 'ZCode':
            continue
        catalog = catalogs.get(binding.source)
        if not catalog:
            continue
        rule = next((r for r in provider_rules if r.get('providerId') == binding.provider), None)
        if rule is None:
            if connections is None:
                continue
            rule = _registered_provider_rule(binding, (connections or {}).get(binding.provider))
            provider_rules.append(rule)
            order = cfg.get('providerOrder')
            if not isinstance(order, list):
                if order is None:
                    order = [r.get('providerId') for r in provider_rules if isinstance(r, dict) and r.get('providerId')]
                    cfg['providerOrder'] = order
                else:
                    raise ValueError('ZCode 规则库 providerOrder 结构异常，拒绝登记')
            if binding.provider not in order:
                order.append(binding.provider)
        elif connections is not None:
            order = cfg.get('providerOrder')
            if isinstance(order, list) and binding.provider not in order:
                order.append(binding.provider)
        rule_config = rule.setdefault('config', {})
        current_ids = list(rule_config.get('personalModelIds') or [])
        catalog_ids = [m.model_id for m in catalog if m.publish]
        sync_owned = managed.get(binding.key) or set()
        target = [mid for mid in current_ids if mid in catalog_ids or mid not in sync_owned]
        target += [mid for mid in catalog_ids if mid not in target]
        rule_config['personalModelIds'] = target
        if 'modelOrder' in rule_config:
            rule_config['modelOrder'] = list(target)
        context = {m.model_id: m.context for m in catalog if m.publish}
        own = [r for r in rebuilt_rules if r.get('providerId') == binding.provider]
        existing = {r.get('modelId'): r for r in own}

        def rebuilt_entry(model_id: str) -> dict[str, Any]:
            """One aligned rule entry: stored properties, catalog context wins.

            The entry's other ``config`` keys are preserved: this pass aligns the
            properties it owns, it does not normalize the rest of an entry away (a
            client's own flags such as ``enabled`` must survive a rebuild).
            """
            stored_config = (existing.get(model_id) or {}).get('config')
            if not isinstance(stored_config, dict):
                stored_config = {}
            old_props = stored_config.get('properties')
            old_props = dict(old_props) if isinstance(old_props, dict) else {}
            if model_id in context:
                catalog_context = context[model_id]
                if catalog_context is None:
                    old_props.pop('contextWindow', None)
                else:
                    old_props['contextWindow'] = int(catalog_context)
            new_config = dict(stored_config)
            new_config['properties'] = old_props
            return {'modelId': model_id, 'config': new_config, 'providerId': binding.provider}
        provider_rules_new: list[dict[str, Any]] = []
        written: set[str] = set()
        for old_entry in own:
            model_id = old_entry.get('modelId')
            if model_id in target:
                if model_id in written:
                    continue
                if model_id in context:
                    provider_rules_new.append(rebuilt_entry(model_id))
                else:
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
        rebuilt_rules = _replace_provider_rules(rebuilt_rules, binding.provider, provider_rules_new)
    rules_cfg['providerModelRules'] = rebuilt_rules
    return json.dumps(doc, ensure_ascii=False, indent=2) + '\n'

def _replace_provider_rules(rules: list[Any], provider: str, replacement: list[dict[str, Any]]) -> list[Any]:
    """Replace one provider's rule entries in place of its current block.

    The block keeps its original position (so an unrelated provider's entries and
    the document order stay put); a provider with no entries yet is appended.
    """
    result: list[Any] = []
    replaced = False
    for rule in rules:
        if isinstance(rule, dict) and rule.get('providerId') == provider:
            if not replaced:
                result.extend(replacement)
                replaced = True
            continue
        result.append(rule)
    if not replaced:
        result.extend(replacement)
    return result

def _registration_connection(provider: str, provider_options: dict[str, Any] | None) -> dict[str, str] | None:
    manual = (provider_options or {}).get(provider)
    manual = manual if isinstance(manual, dict) else {}
    merged = {key: str(manual.get(key) or '').strip() for key in ('baseURL', 'apiKey')}
    if not all(merged.values()) and provider in ZCODE_MANAGED_PROVIDER_DEFAULTS:
        template_options = zcode_managed_provider_template(provider).get('options') or {}
        for key in ('baseURL', 'apiKey'):
            if not merged[key]:
                merged[key] = str(template_options.get(key) or '').strip()
    if not all(merged.values()):
        return None
    return merged

def _provider_config_candidate(catalogs: dict[str, list[CatalogModel]], plan, state: dict[str, Any], model_filter: dict[str, set[str]] | None=None, bindings=ACTIVE_BINDINGS, provider_options: dict[str, Any] | None=None) -> tuple[str | None, dict[Path, str], list[str]]:
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
        return (None, {}, [])
    candidates, registered = _provider_config_candidate_from_raw(raw, catalogs, plan, state, model_filter, bindings, provider_options)
    return (raw, candidates, registered)

def _provider_config_candidate_from_raw(raw: str, catalogs: dict[str, list[CatalogModel]], plan, state: dict[str, Any], model_filter: dict[str, set[str]] | None=None, bindings=ACTIVE_BINDINGS, provider_options: dict[str, Any] | None=None) -> tuple[dict[Path, str], list[str]]:
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
        return ({}, [])
    counts = plan.next_state.get('catalogCounts') or {}
    valid: dict[str, list[CatalogModel]] = {}
    for source, models in catalogs.items():
        ok, _ = validate_catalog(source, models, counts.get(source))
        if not ok:
            continue
        selected = (model_filter or {}).get(source)
        if selected is not None:
            filtered = [m for m in models if m.model_id in selected]
            if filtered:
                valid[source] = filtered
        else:
            valid[source] = models
    managed = _managed_ids(plan, model_filter)
    connections: dict[str, dict[str, str]] = {}
    if store_doc is not None:
        store_cfg = store_doc.get('config') or {}
        present_rules = {rule.get('providerId') for rule in (store_cfg.get('providerConfigRules') or {}).get('providerRules') or [] if isinstance(rule, dict)}
        for binding in bindings:
            if binding.consumer != 'ZCode':
                continue
            catalog = valid.get(binding.source)
            if not catalog or binding.provider in present_rules:
                continue
            options = _registration_connection(binding.provider, provider_options)
            if options is not None:
                connections[binding.provider] = options
    rendered = render_provider_config_sync(raw, valid, bindings, managed, connections)
    if json.loads(rendered) == json.loads(raw):
        return ({}, [])
    return ({ZCODE_PROVIDER_CONFIG: rendered}, sorted(connections))

def _managed_ids(plan, model_filter) -> dict[str, set[str]]:
    """本轮受管 id：计划后登记表 − 观察中 − 写范围外，按绑定 key。

    与 ``_provider_config_candidate_from_raw`` 的 sync-owned 集合同一口径
    （规则库渲染与锁内指纹都用它）。
    """
    watch_ids: dict[str, set[str]] = {}
    for watch_key in plan.next_state.get('retirementWatch') or {}:
        parts = str(watch_key).split('|')
        if len(parts) == 4:
            watch_ids.setdefault('|'.join(parts[:3]), set()).add(parts[3])
    managed: dict[str, set[str]] = {}
    for key, entries in (plan.next_state.get('managed') or {}).items():
        if isinstance(entries, dict):
            ids = set(entries.keys())
            if model_filter:
                source = key.split('|', 1)[0]
                if source in model_filter:
                    ids &= model_filter[source]
            ids -= watch_ids.get(key, set())
            managed.setdefault(key, set()).update(ids)
    return managed

def _managed_scope_fingerprint(raw: str | None, plan, model_filter, bindings) -> str | None:
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
    cfg = doc.get('config') or {}
    if not isinstance(cfg, dict):
        return None
    managed = _managed_ids(plan, model_filter)
    order = cfg.get('providerOrder')
    provider_rules = (cfg.get('providerConfigRules') or {}).get('providerRules') or []
    model_rules = (cfg.get('modelConfigRules') or {}).get('providerModelRules') or []
    scope: list[dict[str, Any]] = []
    for binding in bindings:
        if binding.consumer != 'ZCode':
            continue
        ids = managed.get(binding.key)
        if not ids:
            continue
        rule = next((rule for rule in provider_rules if isinstance(rule, dict) and rule.get('providerId') == binding.provider), None)
        rule_config = rule.get('config') if isinstance(rule, dict) else None
        rule_config = rule_config if isinstance(rule_config, dict) else {}
        current_ids = [str(model_id) for model_id in rule_config.get('personalModelIds') or []]
        scope.append({'providerId': binding.provider, 'rulePresent': rule is not None, 'managedWhitelist': [model_id for model_id in current_ids if model_id in ids], 'managedModelRules': [rule for rule in model_rules if isinstance(rule, dict) and rule.get('providerId') == binding.provider and (str(rule.get('modelId')) in ids)], 'inOrder': binding.provider in order if isinstance(order, list) else None})
    if not scope:
        return None
    return hashlib.sha256(json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()

def _locked_store_merge(catalogs: dict[str, list[CatalogModel]], plan, state: dict[str, Any], model_filter: dict[str, set[str]] | None, bindings, provider_options: dict[str, Any] | None, planned_raw: str | None) -> tuple[str | None, dict[Path, str], list[str]]:
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
        return ('ZCode 供应商规则库在写入窗口内不可读（%s）；本轮不覆盖' % type(exc).__name__, {}, [])
    except UnicodeError:
        return ('ZCode 供应商规则库在写入窗口内不可读（UnicodeDecodeError）；本轮不覆盖', {}, [])
    if _managed_scope_fingerprint(fresh_raw, plan, model_filter, bindings) != _managed_scope_fingerprint(planned_raw, plan, model_filter, bindings):
        return ('ZCode 供应商规则库的受管范围在计划后已被并发修改（版本/指纹冲突）；本轮不覆盖，待下一轮重跑', {}, [])
    candidates, registered = _provider_config_candidate_from_raw(fresh_raw, catalogs, plan, state, model_filter, bindings, provider_options)
    return (None, candidates, registered)

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
            return ({}, f'ZCode 供应商规则库不可读（{type(exc).__name__}），本轮不清理规则库')
        return ({}, None)
    except UnicodeError:
        return ({}, 'ZCode 供应商规则库不可读（UnicodeDecodeError），本轮不清理规则库')
    try:
        doc = json.loads(raw)
    except ValueError:
        return ({}, 'ZCode 供应商规则库无法解析，本轮不清理规则库')
    if not isinstance(doc, dict):
        return ({}, 'ZCode 供应商规则库顶层不是对象，本轮不清理规则库')
    cfg = doc.get('config') or {}
    surface: dict[str, list[str]] = {}

    def add(provider: str, model_id: Any) -> None:
        value = str(model_id or '').strip()
        if not provider or not value:
            return
        ids = surface.setdefault(provider, [])
        if value not in ids:
            ids.append(value)
    for rule in (cfg.get('providerConfigRules') or {}).get('providerRules') or []:
        if not isinstance(rule, dict):
            continue
        provider = str(rule.get('providerId') or '')
        config = rule.get('config') or {}
        if not isinstance(config, dict):
            continue
        for key in ('personalModelIds', 'modelOrder'):
            for model_id in config.get(key) or []:
                add(provider, model_id)
    for rule in (cfg.get('modelConfigRules') or {}).get('providerModelRules') or []:
        if isinstance(rule, dict):
            add(str(rule.get('providerId') or ''), rule.get('modelId'))
    return (surface, None)

def _zcode_store_snapshot() -> tuple[dict[str, dict[str, Any]] | None, str | None]:
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
            return (None, '不可读（%s）' % type(exc).__name__)
        return (None, None)
    except UnicodeError:
        return (None, '不可读（UnicodeDecodeError）')
    try:
        doc = json.loads(raw)
    except ValueError:
        return (None, '无法解析')
    if not isinstance(doc, dict):
        return (None, '无法解析（顶层不是对象）')
    cfg = doc.get('config') or {}
    if not isinstance(cfg, dict):
        return (None, '无法解析（config 结构异常）')
    surface: dict[str, dict[str, Any]] = {}
    for rule in (cfg.get('providerConfigRules') or {}).get('providerRules') or []:
        if not isinstance(rule, dict):
            continue
        provider = str(rule.get('providerId') or '').strip()
        if not provider:
            continue
        rule_config = rule.get('config') or {}
        rule_config = rule_config if isinstance(rule_config, dict) else {}
        ids: list[str] = []
        for key in ('personalModelIds', 'modelOrder'):
            for model_id in rule_config.get(key) or []:
                value = str(model_id or '').strip()
                if value and value not in ids:
                    ids.append(value)
        entry = surface.setdefault(provider, {'providerName': str(rule.get('providerName') or '').strip() or None, 'models': {}})
        for model_id in ids:
            entry['models'].setdefault(model_id, {})
    for rule in (cfg.get('modelConfigRules') or {}).get('providerModelRules') or []:
        if not isinstance(rule, dict):
            continue
        provider = str(rule.get('providerId') or '').strip()
        model_id = str(rule.get('modelId') or '').strip()
        if not provider or not model_id:
            continue
        rule_config = rule.get('config') or {}
        properties = rule_config.get('properties') if isinstance(rule_config, dict) else None
        profile: dict[str, Any] = {}
        if isinstance(properties, dict):
            for key, value in properties.items():
                profile[str(key)] = value
        entry = surface.setdefault(provider, {'providerName': None, 'models': {}})
        stored = entry['models'].get(model_id)
        if isinstance(stored, dict):
            merged = dict(stored)
            merged.update(profile)
            entry['models'][model_id] = merged
        else:
            entry['models'][model_id] = profile
    return (surface, None)

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
            return '不可读（%s）' % type(exc).__name__
        return '文件缺席'
    except UnicodeError:
        return '不可读（UnicodeDecodeError）'
    try:
        doc = json.loads(raw)
    except ValueError:
        return '无法解析'
    if not isinstance(doc, dict):
        return '无法解析（顶层不是对象）'
    return None

def _attach_rule_surface(consumers: dict[str, dict[str, Any]], bindings, surface: dict[str, list[str]]) -> None:
    if not surface:
        return
    for binding in bindings:
        if binding.consumer != 'ZCode':
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
    watch = plan.next_state.get('retirementWatch') or {}
    for watch_key in sorted(watch):
        parts = str(watch_key).split('|')
        if len(parts) != 4:
            continue
        binding = by_key.get('|'.join(parts[:3]))
        if binding is None:
            continue
        entry = watch[watch_key] if isinstance(watch[watch_key], dict) else {}
        pending.append({'consumer': binding.consumer, 'provider': binding.provider, 'model_id': parts[3], 'state': 'watching', 'evidenceRuns': int(entry.get('evidenceRuns') or 1), 'firstMissing': entry.get('firstMissing'), 'description': f'{binding.consumer} 目录已不含 {parts[3]}，待第二次独立目录证据确认退役（本轮未改配置）'})
    return pending

def _read_text(path: Path) -> str:
    with io.open(path, encoding='utf-8', newline='') as handle:
        return handle.read()

def _readback_problems(candidates: dict[Path, str]) -> list[tuple[Path, str]]:
    """写后读回：重新读取事务写入的每个文件并与候选内容比对。

    事务成功返回不等于磁盘内容可证明；这里整批读回核对，返回
    ``(文件, 问题短语)`` 列表。只报告文件名与差异类型，绝不回显文件内容——
    ZCode 候选可能含本机网关密钥。
    """
    problems: list[tuple[Path, str]] = []
    for path, content in candidates.items():
        try:
            with io.open(path, encoding='utf-8', newline='') as handle:
                actual = handle.read()
        except OSError as exc:
            problems.append((path, f'{path.name} 写后读回失败（{type(exc).__name__}）'))
            continue
        if actual != content:
            problems.append((path, f'{path.name} 写后读回内容与候选不一致'))
    return problems

def _verify_written_candidates(candidates: dict[Path, str]) -> list[str]:
    """写后读回（兼容名）：返回问题短语列表，供既有调用方与测试沿用。"""
    return [problem for _, problem in _readback_problems(candidates)]

def _application_boundary(published: list[Path], *, applied: bool) -> dict[str, Any]:
    """生效边界（Rev11）：文件已写 ≠ 客户端已加载 ≠ 会话已采用。

    ``filesWritten`` 只列本批替换成功的客户端文件（本地同步状态文件单列
    ``stateWritten``）；加载与会话采用没有受控证据（无 RPC/日志取证），一律
    按 unverified 如实报告，绝不宣称热更新成功。预览轮如实为空。
    """
    if not applied:
        return {'filesWritten': [], 'stateWritten': False, 'clientLoaded': 'unverified', 'sessionAdopted': 'unverified', 'note': '本轮为预览：未写任何客户端文件；加载与会话采用同样无受控证据，按 unverified 报告'}
    return {'filesWritten': [path.name for path in published if path != STATE_FILE], 'stateWritten': STATE_FILE in published, 'clientLoaded': 'unverified', 'sessionAdopted': 'unverified', 'note': '文件已完整替换并读回一致（已写）；客户端是否已加载、当前会话与下一条消息是否采用均无受控证据，按 unverified 报告，不宣称热更新成功'}

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
        recovery = '保留现状的文件可能已与本地同步状态不一致：请人工核对后重跑 python model_sync.py --apply，按当前客户端文件与目录重新规划'
    elif state_written:
        recovery = '发布已完整落盘（失败发生在收尾阶段）：重跑 python model_sync.py --apply 为幂等对账'
    else:
        recovery = '本轮已回到替换前状态（或失败发生在任何替换前）：重跑 python model_sync.py --apply 会按当前客户端文件与目录重新规划同一变更'
    return {'published': published, 'rolledBack': rolled_back, 'leftInPlace': left_in_place, 'stateWritten': state_written, 'reconcileRequired': reconcile_required, 'clientLoaded': 'unverified', 'sessionAdopted': 'unverified', 'recovery': recovery, 'note': '客户端文件与本地同步状态不是跨文件事务：逐文件替换 + 版本校验回退；保留现状的文件需人工核对'}

def collect_consumers(selected_consumers: Any=None) -> tuple[dict[str, dict[str, Any]], str | None]:
    consumers: dict[str, dict[str, Any]] = {}
    zcode_selected = selected_consumers is None or 'ZCode' in selected_consumers
    zcode_raw: str | None = None
    zcode: dict[str, Any] = {}
    store_surface: dict[str, dict[str, Any]] | None = None
    if zcode_selected:
        zcode_raw = _read_text(ZCODE_JSON)
        zcode = json.loads(zcode_raw)
        store_surface, _store_problem = _zcode_store_snapshot()
    for binding in ACTIVE_BINDINGS:
        if not zcode_selected:
            continue
        provider = (zcode.get('provider') or {}).get(binding.provider) or {}
        models = provider.get('models') or {}
        name = provider.get('name')
        if store_surface is not None and binding.provider in store_surface:
            store_provider = store_surface[binding.provider]
            merged: dict[str, Any] = {}
            for model_id, store_profile in store_provider['models'].items():
                legacy = models.get(model_id)
                entry = dict(legacy) if isinstance(legacy, dict) else {}
                entry.pop('name', None)
                entry.pop('reasoning', None)
                entry.pop('reasoningEfforts', None)
                if isinstance(store_profile, dict):
                    entry.update(store_profile)
                merged[model_id] = entry
            for model_id, legacy in models.items():
                merged.setdefault(model_id, legacy)
            models = merged
            if store_provider.get('providerName'):
                name = store_provider['providerName']
        consumers[binding.key] = {'providerName': name, 'models': models}
    return (consumers, zcode_raw)

def _new_model_candidates(plan, state: dict[str, Any]) -> dict[str, set[str]]:
    initialized_sources = {key.split('|', 1)[0] for key in state.get('initializedBindings') or []}
    known_source_models = {source: {model_id for binding_key, entries in (state.get('managed') or {}).items() if binding_key.startswith(source + '|') for model_id in entries} for source in PROBE_LEVELS}
    candidates: dict[str, set[str]] = {source: set() for source in PROBE_LEVELS}
    for change in plan.changes:
        if change.kind == 'add' and change.model is not None and (change.binding.source in candidates) and (change.binding.source in initialized_sources) and (change.model.model_id not in known_source_models.get(change.binding.source, set())):
            candidates[change.binding.source].add(change.model.model_id)
    return candidates

def load_state() -> dict[str, Any]:
    try:
        state = json.loads(STATE_FILE.read_text(encoding='utf-8'))
        return migrate_state(state)
    except (OSError, ValueError):
        return empty_state()

def _is_virtual_model(model_id: str) -> bool:
    return model_id.endswith(VARIANT_SUFFIXES)

def _response_has_reasoning(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    usage = payload.get('usage') or {}
    details = usage.get('completion_tokens_details') or {}
    if int(details.get('reasoning_tokens') or 0) > 0:
        return True
    for choice in payload.get('choices') or []:
        message = choice.get('message') or {}
        if message.get('reasoning_content') or message.get('reasoning'):
            return True
    return False

def probe_new_models(catalogs: dict[str, list[CatalogModel]], state: dict[str, Any], candidate_ids: dict[str, set[str]]) -> list[str]:
    notices: list[str] = []
    used = 0
    probes = state.setdefault('probes', {})
    for source in ('um',):
        endpoint = UM_CHAT_URL
        for index, model in enumerate(list(catalogs.get(source) or [])):
            if model.model_id not in candidate_ids.get(source, set()) or _is_virtual_model(model.model_id):
                continue
            probe_key = f'{source}|{model.model_id}'
            record = probes.setdefault(probe_key, {'attempts': 0, 'status': 'pending'})
            if record.get('status') in ('verified', 'unsupported', 'ambiguous'):
                continue
            if _record_int(record, 'attempts', 0) >= MAX_PROBE_ATTEMPTS:
                notices.append(f'{model.model_id}能力探测已达重试上限')
                continue
            levels = PROBE_LEVELS[source]
            if used + len(levels) + 1 > MAX_PROBE_REQUESTS:
                notices.append('能力探测达到本轮50请求上限，其余模型延期')
                return notices
            record['attempts'] = _record_int(record, 'attempts', 0) + 1
            accepted: list[str] = []
            transient = False
            successful_without_evidence = False
            try:
                for effort in (*levels, '__invalid_effort__'):
                    result = _post_json(endpoint, {'model': model.model_id, 'messages': [{'role': 'user', 'content': 'Return only the final integer: 17*19'}], 'reasoning_effort': effort, 'max_tokens': 64, 'stream': False}, token=chat2api_local_key())
                    used += 1
                    if effort == '__invalid_effort__':
                        if result['status'] < 400:
                            accepted = []
                            record['status'] = 'ambiguous'
                        break
                    status = int(result['status'])
                    if status in (401, 403, 408, 409, 425, 429) or status >= 500:
                        transient = True
                    elif status < 400:
                        if _response_has_reasoning(result['body']):
                            accepted.append(effort)
                        else:
                            successful_without_evidence = True
                if record.get('status') != 'ambiguous':
                    if transient:
                        record['status'] = 'pending'
                    elif accepted:
                        record['status'] = 'verified'
                        record['efforts'] = accepted
                        record['default'] = 'medium' if 'medium' in accepted else accepted[0]
                        catalogs[source][index] = replace(model, efforts=tuple(accepted), default_effort=record['default'], efforts_source='probe')
                    elif successful_without_evidence:
                        record['status'] = 'ambiguous'
                    else:
                        record['status'] = 'unsupported'
                notices.append(f"{model.model_id}能力探测：{record['status']}")
            except Exception as exc:
                record['status'] = 'pending'
                record['lastError'] = type(exc).__name__
                notices.append(f'{model.model_id}能力探测失败，稍后重试')
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
    has_evidence = any((record.get(field) is not None for field in ('observed_floor', 'observed_ceiling', 'last_run', 'last_success', 'last_error', 'token_evidence'))) or bool(record.get('attempts'))
    if not has_evidence:
        return False
    history = record.get('history')
    if not isinstance(history, list):
        history = []
    history.append({'status': record.get('status'), 'evidence_revision': record.get('evidence_revision'), 'observed_floor': record.get('observed_floor'), 'observed_ceiling': record.get('observed_ceiling'), 'attempts': record.get('attempts'), 'last_run': record.get('last_run'), 'last_success': record.get('last_success'), 'last_error': record.get('last_error'), 'token_evidence': record.get('token_evidence'), 'stale_reason': record.get('stale_reason'), 'reason': reason, 'archived_at': datetime.now(timezone.utc).isoformat()})
    del history[:-PROBE_HISTORY_MAX]
    record['history'] = history
    for field in ('observed_floor', 'observed_ceiling', 'last_run', 'last_success', 'last_error', 'token_evidence', 'stale_reason'):
        record[field] = None
    record['attempts'] = 0
    return True
DECLARED_CONTEXT_UNKNOWN_NOTE = '声明未知，探测仍可测量实际可用长度'

def seed_context_probes(catalogs: dict[str, list[CatalogModel]], state: dict[str, Any], model_filter: dict[str, set[str]] | None=None) -> None:
    queue = state.setdefault('contextProbes', {})
    if not isinstance(queue, dict):
        queue = {}
        state['contextProbes'] = queue
    counts = state.get('catalogCounts') or {}
    now = datetime.now(timezone.utc).isoformat()
    for source, models in catalogs.items():
        valid, _ = validate_catalog(source, models, counts.get(source))
        if not valid:
            continue
        if source in PAUSED_SOURCES:
            continue
        selected = (model_filter or {}).get(source)
        for model in models:
            if selected is not None and model.model_id not in selected:
                continue
            key = f'{source}|{model.model_id}'
            record = queue.get(key)
            if record is None:
                queue[key] = {'source': source, 'model_id': model.model_id, 'declared_context': model.context, 'declared_context_known': model.context is not None, 'declared_note': None if model.context is not None else DECLARED_CONTEXT_UNKNOWN_NOTE, 'declared_at': now, 'status': 'pending', 'observed_floor': None, 'attempts': 0, 'last_run': None, 'last_error': None, 'last_success': None, 'token_evidence': None, 'evidence_revision': 1, 'stale_reason': None, 'scheme_revision': PROBE_SCHEME_REVISION, 'history': []}
                continue
            old_declared = record.get('declared_context')
            declared_changed = old_declared != model.context
            record_scheme = record.get('scheme_revision')
            scheme_mismatch = (1 if record_scheme is None else record_scheme) != PROBE_SCHEME_REVISION
            if not declared_changed and (not scheme_mismatch):
                continue
            reason = 'declared_context_changed' if declared_changed else 'probe_scheme_changed' if record_scheme is not None else 'probe_scheme_missing'
            if declared_changed:
                record['declared_context'] = model.context
                record['declared_at'] = now
            record['declared_context_known'] = model.context is not None
            record['declared_note'] = None if model.context is not None else DECLARED_CONTEXT_UNKNOWN_NOTE
            archived = _archive_probe_generation(record, reason=reason)
            if archived or record.get('status') != 'stale' or _record_int(record, 'evidence_revision', 0) < 1:
                record['evidence_revision'] = _record_int(record, 'evidence_revision', 0) + 1
            record['status'] = 'stale'
            record['stale_reason'] = reason
            record['scheme_revision'] = PROBE_SCHEME_REVISION

def _context_probe_input(length: int, marker: str) -> str:
    """Numbered anchor lines sized to ~length chars plus a unique final anchor.

    Every line embeds the per-request marker, so the tail check is meaningful:
    the reply must quote the FINAL anchor (marker + "FINAL"), which only
    exists at the very end of the input — repeated filler cannot pass.
    """
    template = '[ctxprobe %s] anchor %06d of the long-input retention check'
    lines: list[str] = []
    used = 0
    index = 0
    while used < length:
        index += 1
        line = template % (marker, index)
        lines.append(line)
        used += len(line) + 1
    lines.append('[ctxprobe %s] FINAL anchor — quote this line verbatim' % marker)
    return '\n'.join(lines)

def _tail_anchor(body: Any, marker: str) -> bool:
    """True when any choice's reply quotes the request's final anchor."""
    if not isinstance(body, dict):
        return False
    token = '[ctxprobe %s] FINAL' % marker
    for choice in body.get('choices') or []:
        message = (choice or {}).get('message') or {}
        content = message.get('content')
        if isinstance(content, str) and token in content:
            return True
    return False

def _probe_error_text(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get('error') or body.get('message') or '')
    return str(body or '')

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
    usage = body.get('usage')
    if not isinstance(usage, dict):
        return None

    def reported(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value
    tokens = reported(usage.get('prompt_tokens'))
    if tokens is None:
        tokens = reported(usage.get('input_tokens'))
    return tokens
_CONTEXT_LIMIT_MARKERS = ('context length', 'maximum context', 'max context', 'input length', 'too long', 'length limit', 'token limit', '上下文')

def _explicit_context_limit(status: int, body: Any) -> bool:
    if not 400 <= status < 500:
        return False
    text = _probe_error_text(body).lower()
    return any((marker in text for marker in _CONTEXT_LIMIT_MARKERS))

def normalize_probe_selection(sources: Any, models: Any) -> dict[str, set[str] | None] | None:
    if sources is None and models is None:
        return None
    request: dict[str, set[str] | None] = {}
    if sources is not None:
        if not isinstance(sources, (list, tuple)) or not sources or len(set(sources)) != len(sources) or any((not isinstance(item, str) or not item for item in sources)):
            raise ValueError('sources 必须为非空字符串的非空无重复列表')
        unknown = sorted((item for item in sources if item not in KNOWN_SOURCES))
        if unknown:
            raise ValueError('sources 含未知来源：%s' % ', '.join(unknown))
        paused = sorted((item for item in sources if item in PAUSED_SOURCES))
        if paused:
            raise ValueError('sources 含已暂停来源（不进入计费队列）：%s' % ', '.join(paused))
        for item in sources:
            request[item] = None
    if models is not None:
        if not isinstance(models, dict) or not models:
            raise ValueError('models 必须为非空的 {source: [model_id, ...]} 对象')
        for source, ids in models.items():
            if not isinstance(source, str) or not source or source not in KNOWN_SOURCES:
                raise ValueError('models 含未知来源：%r' % (source,))
            if source in PAUSED_SOURCES:
                raise ValueError('models 含已暂停来源（不进入计费队列）：%s' % source)
            if sources is not None and source not in sources:
                raise ValueError('models 的来源 %s 不在所选 sources 内' % source)
            if not isinstance(ids, (list, tuple)) or not ids or any((not isinstance(mid, str) or not mid for mid in ids)) or (len(set(ids)) != len(ids)):
                raise ValueError('models[%s] 必须为非空字符串的非空无重复列表' % source)
            request[source] = set(ids)
    return request

def resolve_probe_selection(request: dict[str, set[str] | None] | None, records: dict[str, Any]) -> dict[str, set[str]] | None:
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
        source, _, model_id = str(key).partition('|')
        source = str(record.get('source') or source)
        model_id = str(record.get('model_id') or model_id)
        present.setdefault(source, set()).add(model_id)
    resolved: dict[str, set[str]] = {}
    missing: list[str] = []
    for source, ids in request.items():
        queued = present.get(source, set())
        if ids is None:
            resolved[source] = set(queued)
        else:
            missing.extend(('%s|%s' % (source, model_id) for model_id in sorted(ids - queued)))
            resolved[source] = set(ids)
    if missing:
        raise ValueError('以下模型不在探测队列：%s' % ', '.join(sorted(missing)))
    return resolved

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
    return (url, token, token_source is None or token_source is chat2api_local_key or bool(token))

def probe_context_queue(endpoints: dict[str, Any], budget: dict[str, Any], records: dict[str, dict[str, Any]], should_continue: Any, post_json: Any=None, steps: Any=None, selection: Any=None, retest: bool=False) -> dict[str, Any]:
    if selection is not None and (not isinstance(selection, dict)):
        raise ValueError('selection 必须为 {source: model_id 集合} 或 None')
    post = post_json or _post_json
    ladder = tuple(steps) if steps is not None else PROBE_INPUT_STEPS
    max_requests = int((budget or {}).get('max_requests') or 0)
    requested_tokens = int((budget or {}).get('max_tokens') or 0)
    max_tokens = min(requested_tokens or CONTEXT_PROBE_MAX_TOKENS, CONTEXT_PROBE_MAX_TOKENS)
    used = 0
    stop_reason: str | None = None
    results: list[dict[str, Any]] = []
    for key, record in list(records.items()):
        if not isinstance(record, dict):
            continue
        source, _, model_id = str(key).partition('|')
        source = str(record.get('source') or source)
        model_id = str(record.get('model_id') or model_id)
        if selection is not None:
            allowed = selection.get(source)
            if not isinstance(allowed, (set, frozenset, list, tuple)) or model_id not in allowed:
                continue
        status = record.get('status')
        if not (status in ('pending', 'stale') or (status in ('verified', 'failed') and retest)):
            continue
        endpoint = endpoints.get(source)
        if endpoint is None:
            results.append({'key': key, 'source': source, 'model_id': model_id, 'status': status, 'requests': 0, 'note': '该来源已暂停，跳过（零请求）' if source in PAUSED_SOURCES else '该来源未登记探测端点，跳过（零请求）'})
            continue
        url, token, readable = endpoint_credential(endpoint)
        if not readable:
            results.append({'key': key, 'source': source, 'model_id': model_id, 'status': status, 'requests': 0, 'note': '该来源凭据不可读（token 缺失），跳过（零请求）'})
            continue
        previous_status = status
        if status == 'stale':
            archived = _archive_probe_generation(record, reason=str(record.get('stale_reason') or 'stale'))
            if archived or _record_int(record, 'evidence_revision', 0) < 1:
                record['evidence_revision'] = _record_int(record, 'evidence_revision', 0) + 1
            record['scheme_revision'] = PROBE_SCHEME_REVISION
            record['status'] = 'pending'
            status = 'pending'
        elif status in ('verified', 'failed'):
            _archive_probe_generation(record, reason='retest')
            record['evidence_revision'] = _record_int(record, 'evidence_revision', 0) + 1
            record['scheme_revision'] = PROBE_SCHEME_REVISION
            record['stale_reason'] = None
            record['status'] = 'pending'
            status = 'pending'
        row = {'key': key, 'source': source, 'model_id': model_id, 'status': status, 'requests': 0, 'note': ''}
        if status != previous_status:
            row['previous_status'] = previous_status
        if previous_status in ('verified', 'failed') and retest:
            row['retest'] = True
        results.append(row)
        floor = record.get('observed_floor')
        if isinstance(floor, bool) or not isinstance(floor, int):
            floor = None
        pending_steps = [step for step in ladder if floor is None or step > floor]
        if not pending_steps:
            record['status'] = 'verified'
            record['stale_reason'] = None
            row['status'] = 'verified'
            row['note'] = '既有实测证据已覆盖全部步进，无需新请求'
            continue
        completed = True
        for step in pending_steps:
            if used >= max_requests:
                stop_reason = 'budget'
                completed = False
                break
            if should_continue is not None and (not should_continue()):
                stop_reason = 'cancelled'
                completed = False
                break
            used += 1
            row['requests'] += 1
            record['attempts'] = _record_int(record, 'attempts', 0) + 1
            record['last_run'] = datetime.now(timezone.utc).isoformat()
            marker = 'run%03d' % used
            body = {'model': model_id, 'messages': [{'role': 'user', 'content': 'The user message below contains numbered anchor lines. Reply with ONLY the text of the FINAL anchor line, verbatim, nothing else.\n\n' + _context_probe_input(step, marker)}], 'max_tokens': max_tokens, 'stream': False}
            try:
                result = post(url, body, token=token)
            except Exception as exc:
                record['last_error'] = type(exc).__name__
                row['note'] = 'transient:%s，不构成容量结论' % type(exc).__name__
                completed = False
                break
            http_status = int(result.get('status') or 0)
            body = result.get('body')
            if http_status < 400:
                if _tail_anchor(body, marker):
                    record['observed_floor'] = step
                    record['last_success'] = datetime.now(timezone.utc).isoformat()
                    tokens = _trusted_prompt_tokens(body)
                    stored_tokens = record.get('token_evidence')
                    if tokens is not None and (isinstance(stored_tokens, bool) or not isinstance(stored_tokens, int) or tokens > stored_tokens):
                        record['token_evidence'] = tokens
                    record.pop('last_error', None)
                    row['note'] = 'floor=%d' % step
                    continue
                record['last_error'] = 'InconclusiveResponse'
                row['note'] = '回复未引用末段锚点，停止该记录本轮'
                completed = False
                break
            if _explicit_context_limit(http_status, body):
                record['status'] = 'failed'
                record['observed_ceiling'] = step
                record['last_error'] = 'ContextLimitRefused'
                record['stale_reason'] = None
                row['status'] = 'failed'
                row['note'] = '上游明确上下文超限，失败上界=%d' % step
                completed = False
                break
            record['last_error'] = 'HTTPError'
            row['note'] = 'transient:HTTP%d，不构成容量结论' % http_status
            completed = False
            break
        if completed:
            record['status'] = 'verified'
            record['stale_reason'] = None
            row['status'] = 'verified'
            row['note'] = row['note'] or '步进表全部成功，实测通过'
        if stop_reason is not None:
            break
    return {'used': used, 'stopped': stop_reason, 'results': results}

class ContextOverrideConflict(ValueError):
    """The stored override no longer matches the caller's expected version.

    Raised by set/clear when ``expected_set_at`` does not match the stored
    ``set_at`` (including "override appeared since the read").  The console
    layer maps it to a version_conflict error — a concurrent change is never
    silently clobbered.
    """
CONTEXT_OVERRIDE_MAX = 10000000

def _record_int(record: dict[str, Any], field: str, default: int=0) -> int:
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
    return isinstance(entry, dict) and _valid_override_context(entry.get('context')) and (entry.get('context') != model_context)

def _valid_override_context(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and (0 < value <= CONTEXT_OVERRIDE_MAX)

def _setting_model(catalogs, key):
    source, _, model_id = key.partition('|')
    model = next((m for m in catalogs.get(source, []) if isinstance(m, CatalogModel) and m.model_id == model_id), None)
    if model is None:
        raise ValueError('模型不在当前目录：%s' % key)
    return model

def _setting_efforts(model, state):
    """Reuse verified capability evidence without issuing a probe."""
    if model.efforts:
        return (tuple(model.efforts), model.efforts_source or 'upstream')
    record = (state.get('probes') or {}).get(f'{model.source}|{model.model_id}')
    if isinstance(record, dict) and record.get('status') == 'verified':
        levels = _effort_names(record.get('efforts'), allow_max=True)
        if levels:
            return (levels, 'probe')
    return ((), None)

def _validate_setting_values(model, state, values):
    """Validate effective settings and bind measured values to token evidence."""
    evidence = {}
    alias = values.get('alias')
    if alias is not None and (not isinstance(alias, str) or not alias.strip() or len(alias) > 128):
        raise ValueError('alias 必须为 1-128 字符字符串或 None')
    effort = values.get('defaultEffort')
    if effort is not None:
        levels, origin = _setting_efforts(model, state)
        if not isinstance(effort, str) or effort not in levels:
            raise ValueError('defaultEffort 缺少支持证据或不在支持档位中')
        evidence['effort'] = {'levels': list(levels), 'source': origin}
    kind, tokens = (values.get('contextSource'), values.get('contextTokens'))
    if kind not in (None, 'declared', 'manual', 'measured'):
        raise ValueError('contextSource 必须为 declared/manual/measured 或 None')
    if kind in (None, 'declared'):
        if tokens is not None:
            raise ValueError('声明上下文不接受 contextTokens')
    elif not _valid_override_context(tokens):
        raise ValueError('contextTokens 必须为 1-%d 的正整数' % CONTEXT_OVERRIDE_MAX)
    if kind == 'measured':
        key = f'{model.source}|{model.model_id}'
        record = (state.get('contextProbes') or {}).get(key)
        if not isinstance(record, dict):
            raise ValueError('实测上下文没有可信 Token 证据；字符数不能作 Token')
        bound = record.get('token_evidence')
        revision = record.get('evidence_revision')
        if record.get('source') != model.source or record.get('model_id') != model.model_id or record.get('status') not in ('verified', 'failed', 'pending') or (not _valid_override_context(bound)) or isinstance(revision, bool) or (not isinstance(revision, int)) or (revision < 1) or (record.get('scheme_revision') != PROBE_SCHEME_REVISION) or (record.get('declared_context') != model.context) or (tokens > bound):
            raise ValueError('实测上下文证据缺席、过期或超出已验证 Token 下界')
        evidence['context'] = {'key': key, 'revision': revision, 'tokenLowerBound': bound, 'declaredContext': model.context, 'schemeRevision': record['scheme_revision']}
    return evidence

def save_model_setting_draft(catalogs: dict[str, list[CatalogModel]], source: str, model_id: str, fields: dict[str, Any] | None=None, expected_draft_version: int | None=None) -> dict[str, Any]:
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
    if not isinstance(source, str) or not source or (not isinstance(model_id, str)) or (not model_id):
        raise ValueError('source 与 model_id 必须为非空字符串')
    fields = dict(fields or {})
    allowed = {'alias', 'contextSource', 'contextTokens', 'defaultEffort'}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError('未知设置字段：%s' % ','.join(sorted(unknown)))
    draft_fields: dict[str, Any] = {}
    alias = fields.get('alias')
    if 'alias' in fields:
        if alias is not None and (not isinstance(alias, str) or not alias.strip() or len(alias) > 128):
            raise ValueError('alias 必须为 1-128 字符字符串或 None')
        draft_fields['alias'] = alias.strip() if alias else None
    source_kind = fields.get('contextSource')
    if 'contextSource' in fields and source_kind not in (None, 'declared', 'manual', 'measured'):
        raise ValueError('contextSource 必须为 declared/manual/measured 或 None')
    tokens = fields.get('contextTokens')
    if 'contextTokens' in fields and tokens is not None:
        if not _valid_override_context(tokens):
            raise ValueError('contextTokens 必须为 1-%d 的正整数' % CONTEXT_OVERRIDE_MAX)
    if 'contextSource' in fields:
        if source_kind in (None, 'declared'):
            if tokens is not None:
                raise ValueError('contextSource=declared/None 时不接受 contextTokens')
            draft_fields['contextSource'] = source_kind
            draft_fields['contextTokens'] = None
        else:
            if tokens is None:
                raise ValueError('contextSource=%s 必须给出 contextTokens' % source_kind)
            draft_fields['contextSource'] = source_kind
            draft_fields['contextTokens'] = int(tokens)
    elif 'contextTokens' in fields:
        draft_fields['contextTokens'] = tokens
    effort = fields.get('defaultEffort')
    if 'defaultEffort' in fields:
        if effort is not None and (not isinstance(effort, str) or not effort.strip() or len(effort) > 32):
            raise ValueError('defaultEffort 必须为 1-32 字符字符串或 None')
        draft_fields['defaultEffort'] = effort
    if not draft_fields:
        raise ValueError('未提供任何可保存的设置字段')
    source_models = catalogs.get(source) if isinstance(catalogs, dict) else None
    model = next((m for m in source_models or [] if isinstance(m, CatalogModel) and m.model_id == model_id), None)
    if model is None:
        raise ValueError('模型不在当前目录：%s|%s' % (source, model_id))
    state = load_state()
    settings = _model_settings_of_state(state)
    key = f'{source}|{model_id}'
    record = settings.get(key)
    if not isinstance(record, dict):
        record = {'draft': None, 'draftVersion': 0, 'draftSavedAt': None, 'applied': None, 'appliedVersion': None, 'appliedAt': None, 'appliedEvidence': None}
    if expected_draft_version is not None and record.get('draftVersion') != expected_draft_version:
        raise ContextOverrideConflict('草稿版本冲突（%s）：读面 draftVersion=%s，当前存储=%s；请基于最新读面重试' % (key, expected_draft_version, record.get('draftVersion')))
    merged = dict(record.get('draft') or record.get('applied') or {})
    merged.update(draft_fields)
    for field in ('alias', 'contextSource', 'contextTokens', 'defaultEffort'):
        merged.setdefault(field, None)
    record['draftEvidence'] = _validate_setting_values(model, state, merged)
    record['draft'] = merged
    record['draftVersion'] = int(record.get('draftVersion') or 0) + 1
    record['draftSavedAt'] = datetime.now(timezone.utc).isoformat()
    settings[key] = record
    state['modelSettings'] = settings
    apply_config_transaction({STATE_FILE: json.dumps(state, ensure_ascii=False, indent=2) + '\n'}, BACKUP_DIR, keep_backups=30)
    return dict(record)

def set_context_override(catalogs: dict[str, list[CatalogModel]], source: str, model_id: str, context: int, expected_set_at: str | None=None) -> dict[str, Any]:
    """Legacy-name wrapper: save one manual context DRAFT (Rev8 契约 6).

    历史入口保留名称与参数（console 动作与测试沿用），语义演进为「保存上下
    文草稿」：写入 modelSettings 的 contextSource=manual 草稿（draftVersion
    递增），定时同步不消费草稿——真实发布只能走 model_settings_apply 的
    显式应用流。返回草稿记录（context/set_at/source 键仍返回以兼容旧读方，
    另加 draftVersion）。caller holds SyncLock（与旧语义一致）。
    """
    if not _valid_override_context(context):
        raise ValueError('context 必须为 1-%d 的正整数' % CONTEXT_OVERRIDE_MAX)
    peek = load_state()
    record = _model_settings_of_state(peek).get(f'{source}|{model_id}')
    draft = record.get('draft') if isinstance(record, dict) else None
    if isinstance(draft, dict):
        stored_at = record.get('draftSavedAt')
        if expected_set_at is None or expected_set_at != stored_at:
            raise ContextOverrideConflict('覆盖版本冲突（%s）：读面 override_at=%s，当前存储=%s；请基于最新读面重试' % (f'{source}|{model_id}', expected_set_at, stored_at))
    record = save_model_setting_draft(catalogs, source, model_id, {'contextSource': 'manual', 'contextTokens': int(context)}, expected_draft_version=(record or {}).get('draftVersion'))
    draft = dict(record.get('draft') or {})
    return {'context': draft.get('contextTokens'), 'set_at': record.get('draftSavedAt'), 'source': 'manual', 'draft_version': record.get('draftVersion')}

def clear_context_override(source: str, model_id: str, expected_set_at: str | None=None) -> dict[str, Any]:
    """Write a RESTORE draft for the context field (Rev8 契约 6).

    恢复也形成可预览变更：本函数不再直接删除存储覆盖，而是为该模型的
    context 字段写入恢复草稿（draft.contextSource=None → 显式应用后回到目录
    声明）。当既无草稿也无已应用覆盖时是纯零写入 no-op（返回 existed=False/
    changed=False，状态文件不重写）；测量历史（contextProbes）不受影响。
    版本规则：``expected_set_at`` 与读面暴露的版本令牌比对——有草稿比
    draftSavedAt，无草稿有已应用覆盖比 applied 的 set_at，不一致抛
    ContextOverrideConflict（绝不静默覆盖并发修改）。
    """
    if not isinstance(source, str) or not source or (not isinstance(model_id, str)) or (not model_id):
        raise ValueError('source 与 model_id 必须为非空字符串')
    state = load_state()
    settings = _model_settings_of_state(state)
    key = f'{source}|{model_id}'
    record = settings.get(key)
    if not isinstance(record, dict):
        record = {'draft': None, 'draftVersion': 0, 'draftSavedAt': None, 'applied': None, 'appliedVersion': None, 'appliedAt': None, 'appliedEvidence': None}
    draft = record.get('draft') if isinstance(record.get('draft'), dict) else None
    applied = record.get('applied') if isinstance(record.get('applied'), dict) else None
    draft_pending = bool(draft and draft.get('contextSource') is not None)
    applied_manual = bool(applied and applied.get('contextSource') in ('manual', 'measured'))
    restore_pending = bool(draft is not None and draft.get('contextSource') is None and (draft.get('contextTokens') is None) and applied_manual)
    if not draft_pending and (not applied_manual):
        return {'existed': False, 'changed': False}
    if restore_pending and (not draft_pending):
        return {'existed': True, 'changed': False, 'draftVersion': record.get('draftVersion'), 'note': '恢复草稿已在待应用（幂等，零写入）；发布仍走 model.settings_apply'}
    if expected_set_at is None:
        raise ContextOverrideConflict('恢复版本冲突（%s）：存在待恢复覆盖但未提供版本令牌；请基于最新读面重试' % key)
    stored_at = record.get('draftSavedAt') if draft_pending else record.get('appliedAt')
    if stored_at != expected_set_at:
        raise ContextOverrideConflict('恢复版本冲突（%s）：读面版本令牌=%s，当前存储=%s；请基于最新读面重试' % (key, expected_set_at, stored_at))
    merged = dict(draft or {})
    merged['contextSource'] = None
    merged['contextTokens'] = None
    record['draft'] = merged
    record['draftVersion'] = int(record.get('draftVersion') or 0) + 1
    record['draftSavedAt'] = datetime.now(timezone.utc).isoformat()
    settings[key] = record
    state['modelSettings'] = settings
    apply_config_transaction({STATE_FILE: json.dumps(state, ensure_ascii=False, indent=2) + '\n'}, BACKUP_DIR, keep_backups=30)
    return {'existed': True, 'changed': True, 'draftVersion': record['draftVersion'], 'note': '恢复草稿已保存：应用后回到目录声明（保存≠已应用）'}

def _context_overrides_of_state(state: dict[str, Any]) -> dict[str, Any]:
    overrides = state.get('contextOverrides')
    if not isinstance(overrides, dict):
        overrides = {}
    return overrides

def _model_settings_of_state(state: dict[str, Any]) -> dict[str, Any]:
    """Per-model settings records (Rev8 契约 6); missing/corrupt → empty."""
    settings = state.get('modelSettings')
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
        if isinstance(record.get('draft'), dict) and isinstance(record.get('draftVersion'), int) and (not isinstance(record.get('draftVersion'), bool)) and (record['draftVersion'] > 0):
            pending[key] = record['draftVersion']
    return pending

def promote_model_setting_drafts(state: dict[str, Any], selection: dict[str, int] | None=None) -> tuple[list[str], list[dict[str, Any]]]:
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
    keys = sorted(selection) if selection is not None else sorted((key for key, record in settings.items() if isinstance(record, dict) and isinstance(record.get('draft'), dict)))
    for key in keys:
        record = settings.get(key)
        if not isinstance(record, dict) or not isinstance(record.get('draft'), dict):
            skipped.append({'key': key, 'reason': '无待应用草稿'})
            continue
        expected = (selection or {}).get(key)
        if expected is not None and record.get('draftVersion') != expected:
            raise ContextOverrideConflict('草稿版本冲突（%s）：预览绑定 draftVersion=%s，当前存储=%s' % (key, expected, record.get('draftVersion')))
        record['applied'] = dict(record['draft'])
        record['appliedVersion'] = record.get('draftVersion')
        record['appliedAt'] = datetime.now(timezone.utc).isoformat()
        record['appliedEvidence'] = 'applied'
        record['appliedSettingEvidence'] = copy.deepcopy(record.get('draftEvidence'))
        record['draft'] = None
        record['draftEvidence'] = None
        promoted.append(key)
        settings[key] = record
    state['modelSettings'] = settings
    return (promoted, skipped)

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
        if isinstance(record.get('draft'), dict) and record.get('applied') is None:
            held.add(key)
    return held

def apply_context_overrides(catalogs: dict[str, list[CatalogModel]], state: dict[str, Any]) -> tuple[dict[str, list[CatalogModel]], list[str]]:
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
    applied_records = {key: record for key, record in settings.items() if isinstance(record, dict) and isinstance(record.get('applied'), dict)}
    if not applied_records:
        return (catalogs, [])
    notices: list[str] = []
    effective: dict[str, list[CatalogModel]] = {}
    for source, models in catalogs.items():
        rows: list[CatalogModel] = []
        for model in models:
            levels, origin = _setting_efforts(model, state)
            if levels and (not model.efforts):
                model = replace(model, efforts=levels, efforts_source=origin)
            record = applied_records.get(f'{source}|{model.model_id}')
            if record is None:
                rows.append(model)
                continue
            values = record['applied']
            if values.get('alias'):
                notices.append('别名已应用：%s|%s 显示名 → %s' % (source, model.model_id, values['alias']))
                model = replace(model, display_name=values['alias'])
            tokens = values.get('contextTokens')
            if values.get('contextSource') in ('manual', 'measured') and _valid_override_context(tokens) and (tokens != model.context):
                notices.append('context 已应用：%s|%s → %d（目录声明 %s）' % (source, model.model_id, tokens, model.context))
                model = replace(model, context=tokens)
            effort = values.get('defaultEffort')
            if effort and model.efforts and (effort in model.efforts) and (effort != model.default_effort):
                notices.append('默认档已应用：%s|%s → %s' % (source, model.model_id, effort))
                model = replace(model, default_effort=effort)
            rows.append(model)
        effective[source] = rows
    return (effective, notices)

def context_override_surface(state: dict[str, Any], catalogs: dict[str, list[CatalogModel]], consumers: dict[str, dict[str, Any]] | None=None) -> list[dict[str, Any]]:
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
    settings = _model_settings_of_state(state)
    held_keys = held_model_setting_keys(state)
    overrides: dict[str, dict[str, Any]] = {}
    for key, record in settings.items():
        if not isinstance(key, str) or not isinstance(record, dict):
            continue
        applied = record.get('applied')
        if isinstance(applied, dict) and applied.get('contextSource') in ('manual', 'measured'):
            overrides[key] = {'context': applied.get('contextTokens'), 'set_at': record.get('appliedAt'), 'source': applied.get('contextSource')}
    probes = state.get('contextProbes')
    if not isinstance(probes, dict):
        probes = {}
    rows: dict[str, dict[str, Any]] = {}
    for source, models in (catalogs or {}).items():
        if not isinstance(models, list):
            continue
        for model in models:
            key = f'{source}|{model.model_id}'
            probe = probes.get(key)
            probe = probe if isinstance(probe, dict) else {}
            rows[key] = {'source': source, 'model_id': model.model_id, 'declared_context': model.context, 'override_context': None, 'effective_context': model.context, 'effective_source': 'declared' if model.context is not None else 'unknown', 'declared_at': probe.get('declared_at'), 'override_at': None, 'override_applied': False, 'override_state': 'none'}
    for key, entry in overrides.items():
        source, _, model_id = str(key).partition('|')
        row = rows.get(key)
        if row is None:
            probe = probes.get(key)
            probe = probe if isinstance(probe, dict) else {}
            row = {'source': source, 'model_id': model_id, 'declared_context': None, 'override_context': None, 'effective_context': None, 'effective_source': 'unknown', 'declared_at': probe.get('declared_at'), 'override_at': None, 'override_applied': False, 'override_state': 'model_missing'}
            rows[key] = row
            if isinstance(entry, dict):
                row['override_context'] = entry.get('context')
                row['override_at'] = entry.get('set_at')
            continue
        if not isinstance(entry, dict):
            row['override_state'] = 'malformed'
            continue
        row['override_context'] = entry.get('context')
        row['override_at'] = entry.get('set_at')
        if _override_applies(entry, row['declared_context']):
            row['override_applied'] = True
            row['override_state'] = 'applied'
            row['effective_context'] = entry['context']
            row['effective_source'] = 'override'
        elif _valid_override_context(entry.get('context')):
            row['override_state'] = 'equal'
        else:
            row['override_state'] = 'malformed'
    for key, record in settings.items():
        if not isinstance(key, str) or '|' not in key:
            continue
        row = rows.get(key)
        if row is None:
            source, _, model_id = key.partition('|')
            probe = probes.get(key)
            probe = probe if isinstance(probe, dict) else {}
            row = {'source': source, 'model_id': model_id, 'declared_context': None, 'override_context': None, 'effective_context': None, 'effective_source': 'unknown', 'declared_at': probe.get('declared_at'), 'override_at': None, 'override_applied': False, 'override_state': 'model_missing'}
            rows[key] = row
        if not isinstance(record, dict):
            continue
        draft = record.get('draft') if isinstance(record.get('draft'), dict) else None
        applied = record.get('applied') if isinstance(record.get('applied'), dict) else None
        row['settings_draft'] = dict(draft) if draft else None
        row['settings_applied'] = dict(applied) if applied else None
        row['settings_draft_version'] = record.get('draftVersion')
        row['settings_applied_version'] = record.get('appliedVersion')
        row['settings_applied_at'] = record.get('appliedAt')
        row['settings_applied_evidence'] = record.get('appliedEvidence')
        row['settings_held'] = key in held_keys
    for key, row in rows.items():
        source, _, model_id = str(key).partition('|')
        client_context = None
        readback_state = 'unavailable' if consumers is None else 'absent'
        readback_consumer = None
        if consumers is not None:
            for binding in ACTIVE_BINDINGS:
                if binding.source != source:
                    continue
                consumer = consumers.get(binding.key)
                if not isinstance(consumer, dict):
                    continue
                profiles = consumer.get('models')
                profile = profiles.get(model_id) if isinstance(profiles, dict) else None
                if not isinstance(profile, dict):
                    continue
                context = profile.get('contextWindow')
                if context is None:
                    limits = profile.get('limit')
                    context = limits.get('context') if isinstance(limits, dict) else None
                if _valid_override_context(context):
                    client_context = context
                    row['client_readback_consumer'] = binding.consumer
                    readback_state = 'ok'
                    break
        row['client_context'] = client_context
        row.setdefault('client_readback_consumer', None)
        for field in ('settings_draft', 'settings_applied', 'settings_draft_version', 'settings_applied_version', 'settings_applied_at', 'settings_applied_evidence'):
            row.setdefault(field, None)
        row.setdefault('settings_held', False)
        row['client_readback_state'] = readback_state
        applied = row.get('settings_applied')
        applied_tokens = applied.get('contextTokens') if isinstance(applied, dict) else None
        row['client_matches_applied'] = client_context == applied_tokens if consumers is not None and applied_tokens is not None and (client_context is not None) else None
        readbacks = []
        for binding in ACTIVE_BINDINGS:
            if binding.source != source:
                continue
            consumer = (consumers or {}).get(binding.key)
            profiles = consumer.get('models') if isinstance(consumer, dict) else None
            profile = profiles.get(model_id) if isinstance(profiles, dict) else None
            profile = profile if isinstance(profile, dict) else None
            limits = profile.get('limit') if profile else None
            limits = limits if isinstance(limits, dict) else {}
            reasoning = profile.get('reasoning') if profile else None
            reasoning = reasoning if isinstance(reasoning, dict) else {}
            name = profile.get('name') if profile else None
            alias = name[:-len(binding.suffix)] if isinstance(name, str) and name.endswith(binding.suffix) else name
            context = profile.get('contextWindow', limits.get('context')) if profile else None
            output = profile.get('maxTokens', limits.get('output')) if profile else None
            context = context if _valid_override_context(context) else None
            output = output if _valid_override_context(output) else None
            effort = reasoning.get('defaultVariant')
            effort = effort if isinstance(effort, str) else None
            desired = applied if isinstance(applied, dict) else {}
            readbacks.append({'consumer': binding.consumer, 'state': 'unavailable' if consumers is None else 'absent' if profile is None else 'ok', 'contextTokens': context, 'alias': alias if isinstance(alias, str) else None, 'defaultEffort': effort, 'outputTokenCap': {'value': output, 'source': 'client_config' if output is not None else None}, 'matchesApplied': {'context': context == desired['contextTokens'] if context is not None and desired.get('contextTokens') is not None else None, 'alias': alias == desired['alias'] if isinstance(alias, str) and desired.get('alias') is not None else None, 'defaultEffort': effort == desired['defaultEffort'] if effort is not None and desired.get('defaultEffort') is not None else None}})
        row['client_readbacks'] = readbacks
        preferred = next((item for item in readbacks if item['consumer'] == 'ZCode'), readbacks[0] if readbacks else {})
        row['client_alias'] = preferred.get('alias')
        row['client_default_effort'] = preferred.get('defaultEffort')
        row['client_output_token_cap'] = preferred.get('outputTokenCap', {'value': None, 'source': None})
    return [rows[key] for key in sorted(rows)]

def _save_history(result: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now().astimezone() - timedelta(days=90)
    kept: list[str] = []
    if HISTORY_FILE.exists():
        for line in HISTORY_FILE.read_text(encoding='utf-8').splitlines():
            try:
                stamp = datetime.fromisoformat(json.loads(line)['timestamp'])
                if stamp >= cutoff:
                    kept.append(line)
            except (ValueError, KeyError, TypeError):
                continue
    kept.append(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
    HISTORY_FILE.write_text('\n'.join(kept) + '\n', encoding='utf-8')

def _write_result(result: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    content = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    temporary = RESULT_FILE.with_suffix('.tmp')
    temporary.write_text(content, encoding='utf-8')
    replace_with_retry(temporary, RESULT_FILE)
    _save_history(result)

def _write_preview(result: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    content = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    temporary = PREVIEW_FILE.with_suffix('.tmp')
    temporary.write_text(content, encoding='utf-8')
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
        raise ValueError('scope 必须为对象')
    sources = scope.get('sources')
    if sources is not None:
        if not isinstance(sources, (list, tuple)) or not sources or len(set(sources)) != len(sources) or any((source not in ACTIVE_SOURCES for source in sources)):
            raise ValueError('sources 必须为启用来源的非空无重复子集')
        sources = list(sources)
    consumers = scope.get('consumers')
    if consumers is not None:
        if not isinstance(consumers, (list, tuple)) or not consumers or len(set(consumers)) != len(consumers) or any((item not in KNOWN_CONSUMERS for item in consumers)):
            raise ValueError('consumers 必须为 ZCode 的非空无重复子集')
        consumers = list(consumers)
    raw_models = scope.get('models') or {}
    if not isinstance(raw_models, dict):
        raise ValueError('models 必须为 {source: [model_id, ...]} 对象')
    models: dict[str, list[str]] = {}
    total = 0
    for source, ids in raw_models.items():
        if source not in ACTIVE_SOURCES:
            raise ValueError(f'models 含未知或已暂停来源：{source}')
        if sources is not None and source not in sources:
            raise ValueError(f'models 的来源 {source} 不在所选 sources 内')
        if not isinstance(ids, (list, tuple)):
            raise ValueError(f'models[{source}] 必须为 model_id 列表')
        if any((not isinstance(mid, str) or not mid.strip() for mid in ids)):
            raise ValueError(f'models[{source}] 含空 model_id')
        if len(ids) > 64:
            raise ValueError(f'models[{source}] 超过每源 64 个上限')
        total += len(ids)
        models[source] = list(ids)
    if total > 256:
        raise ValueError('models 总条目超过 256 上限')
    if not sources and (not consumers) and (not models):
        return None
    return {'sources': sources, 'consumers': consumers, 'models': models}

def _project_setting_drafts(state, catalogs, selection):
    """Project a version-bound selection without touching the loaded document."""
    projected = copy.deepcopy(state)
    settings = _model_settings_of_state(projected)
    for key, version in selection.items():
        record = settings.get(key)
        if not isinstance(record, dict) or not isinstance(record.get('draft'), dict) or record.get('draftVersion') != version:
            raise ContextOverrideConflict('草稿版本冲突（%s）；请重新预览' % key)
        evidence = _validate_setting_values(_setting_model(catalogs, key), state, record['draft'])
        saved = record.get('draftEvidence') or {}
        if record['draft'].get('contextSource') == 'measured' and saved.get('context') != evidence.get('context'):
            raise ContextOverrideConflict('实测上下文证据已变化（%s）；请重新保存草稿' % key)
        if saved.get('effort') and saved['effort'] != evidence.get('effort'):
            raise ContextOverrideConflict('思考档位证据已变化（%s）；请重新保存草稿' % key)
        record['draftEvidence'] = evidence
    promoted, _ = promote_model_setting_drafts(projected, selection)
    return (projected, promoted)

def _settings_preview_binding(original, catalogs, consumers, zcode_raw, bindings, selection, scope):
    """Hash exact publication inputs; no credentials or client contents leave here."""
    rule_hash = None
    if any((binding.consumer == 'ZCode' for binding in bindings)):
        try:
            rule_hash = hashlib.sha256(ZCODE_PROVIDER_CONFIG.read_bytes()).hexdigest()
        except OSError as exc:
            rule_hash = 'unavailable:' + type(exc).__name__
    changes = [{'key': key, 'source': key.split('|', 1)[0], 'model_id': key.split('|', 1)[1], 'before': copy.deepcopy(original['modelSettings'][key].get('applied')), 'draft': copy.deepcopy(original['modelSettings'][key]['draft'])} for key in sorted(selection)]
    inputs = {'scope': scope, 'selection': selection, 'records': {key: original['modelSettings'][key] for key in selection}, 'catalogs': {source: [asdict(m) for m in models] for source, models in catalogs.items()}, 'consumers': consumers, 'zcode': zcode_raw, 'rule_hash': rule_hash, 'evidence': {key: (original.get('contextProbes') or {}).get(key) for key in selection}}
    token = hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    return {'selection': dict(selection), 'preview_token': token, 'changes': changes}

def run_sync(apply: bool, run_probes: bool, confirm: bool, scope: dict[str, Any] | None=None, state_override: dict[str, Any] | None=None, settings_selection: dict[str, int] | None=None, preview_token: str | None=None, before_publish=None) -> dict[str, Any]:
    started = datetime.now().astimezone()
    scope = _normalize_scope(scope)
    sources = scope.get('sources') if scope else None
    selected_consumers = scope.get('consumers') if scope else None
    model_filter = {source: set(ids) for source, ids in scope['models'].items()} if scope and scope['models'] else None
    state = load_state() if state_override is None else state_override
    original_state = copy.deepcopy(state)
    settings_promoted: list[str] = []
    settings_deferred: list[str] = []
    settings_preview = None
    catalogs, source_warnings = collect_catalogs(sources=sources)
    if settings_selection is not None:
        if not settings_selection:
            raise ValueError('设置应用选择不能为空')
        seed_context_probes(catalogs, state, model_filter)
        state, settings_promoted = _project_setting_drafts(state, catalogs, settings_selection)
    raw_catalogs = catalogs
    catalogs, override_notices = apply_context_overrides(catalogs, state)
    held_keys = held_model_setting_keys(state)
    bindings = [binding for binding in ACTIVE_BINDINGS if (sources is None or binding.source in sources) and (selected_consumers is None or binding.consumer in selected_consumers)]
    consumers, zcode_raw = collect_consumers(selected_consumers=sorted({binding.consumer for binding in bindings}))
    rule_surface, rule_surface_problem = _zcode_rule_surface()
    _attach_rule_surface(consumers, bindings, rule_surface)
    zcode_providers = json.loads(zcode_raw).get('provider') or {} if zcode_raw is not None else {}
    missing = [binding for binding in bindings if binding.consumer == 'ZCode' and binding.source in catalogs and (binding.provider not in zcode_providers) and (binding.provider not in ZCODE_MANAGED_PROVIDER_DEFAULTS)]
    if missing:
        labels = ', '.join((binding.provider for binding in missing))
        if sources is not None or any((binding.source in (model_filter or {}) for binding in missing)):
            raise ValueError(f'ZCode 配置缺少所选来源 provider：{labels}')
        source_warnings.extend((f'ZCode 配置缺少 {binding.provider}，本轮跳过该来源的 ZCode 同步' for binding in missing))
        bindings = [binding for binding in bindings if binding not in missing]
    if settings_selection is not None:
        settings_preview = _settings_preview_binding(original_state, raw_catalogs, consumers, zcode_raw, bindings, settings_selection, scope)
        if apply and preview_token != settings_preview['preview_token']:
            raise ContextOverrideConflict('预览已过期：草稿、目录证据或客户端配置已变化；请重新预览')
        if not bindings:
            raise ValueError('没有可发布设置的客户端绑定')
    initial_plan = plan_catalog_mirror(catalogs, consumers, state, bindings=bindings, model_filter=model_filter, held=held_keys)
    candidate_ids = _new_model_candidates(initial_plan, state)
    probe_records = state.setdefault('probes', {})
    for source, model_ids in candidate_ids.items():
        if sources is not None and source not in sources:
            continue
        for model_id in model_ids:
            if model_filter is not None and source in model_filter and (model_id not in model_filter[source]):
                continue
            probe_records.setdefault(f'{source}|{model_id}', {'attempts': 0, 'status': 'pending', 'newModel': True})
    for key, record in list(probe_records.items()):
        try:
            source, model_id = key.split('|', 1)
        except ValueError:
            continue
        if source in candidate_ids and record.get('status') == 'pending' and (record.get('newModel') is True) and (sources is None or source in sources) and (model_filter is None or source not in model_filter or model_id in model_filter[source]):
            candidate_ids[source].add(model_id)
    if run_probes:
        source_warnings.extend(probe_new_models(catalogs, state, candidate_ids))
    seed_context_probes(raw_catalogs, state, model_filter)
    plan = plan_catalog_mirror(catalogs, consumers, state, bindings=bindings, model_filter=model_filter, held=held_keys)
    pending_retirement = _pending_retirement(plan, bindings)
    warnings = source_warnings + plan.warnings + [item['description'] for item in pending_retirement]
    if rule_surface_problem:
        warnings.append(rule_surface_problem)
    store_problem = _rule_store_problem()
    if any((change.binding.consumer == 'ZCode' for change in plan.changes)) and store_problem:
        warnings.append('ZCode 供应商规则库%s（%s）：本轮不写主配置 config.json，ZCode 侧写集合整体延期——写半份配置会让新供应商/模型在 picker 不可见' % (store_problem, ZCODE_PROVIDER_CONFIG))
    provider_options = {provider_id: provider.get('options') if isinstance(provider, dict) else None for provider_id, provider in zcode_providers.items()}
    preview_candidate: dict[Path, str] = {}
    preview_registered: list[str] = []
    preview_lines: list[str] = []
    if not apply:
        if store_problem:
            if any((change.binding.consumer == 'ZCode' for change in plan.changes)):
                preview_lines.append('（应用时）ZCode 侧写集合整体延期：供应商规则库%s' % store_problem)
        else:
            try:
                _, preview_candidate, preview_registered = _provider_config_candidate(catalogs, plan, state, model_filter, bindings, provider_options)
            except ValueError as exc:
                preview_candidate, preview_registered = ({}, [])
                warnings.append('ZCode 供应商规则库登记候选解析失败（预览口径）：%s' % exc)
            for provider in preview_registered:
                preview_lines.append('（应用时）ZCode 规则库登记供应商 %s（providerOrder/providerRules/模型白名单与规则）' % provider)
            if preview_candidate and (not preview_registered):
                preview_lines.append('（应用时）ZCode 供应商规则库将对齐（provider_config.json）')
    print('模型目录同步计划：')
    for change in plan.changes:
        print('  - ' + change.description)
    for notice in override_notices:
        print('  - ' + notice)
    for line in preview_lines:
        print('  - ' + line)
    for warning in warnings:
        print('  [警告] ' + warning)
    if not plan.changes and (not preview_lines):
        print('  无配置变化。')
    should_apply = apply
    if confirm and plan.changes:
        should_apply = input('应用上述变化？[y/N]: ').strip().lower() == 'y'
    applied_changes = []
    pending_changes = []
    applied_details = []
    pending_details = []
    deferred_zcode: list[Any] = []
    deferred_clients: list[Any] = []
    zcode_changed = False
    published_paths: list[Path] = []
    if should_apply:
        zcode_changes = [change for change in plan.changes if change.binding.consumer == 'ZCode']
        zcode_live = zcode_running()
        if zcode_live:
            hot_admitted, hot_detail = zcode_hot_sync_admission()
        else:
            hot_admitted, hot_detail = (False, '')
        defer_zcode_write = zcode_live and (not hot_admitted)
        plan_store_raw: str | None = None
        if store_problem:
            provider_candidate, registered_providers = ({}, [])
        elif defer_zcode_write:
            try:
                plan_store_raw, provider_candidate, registered_providers = _provider_config_candidate(catalogs, plan, state, model_filter, bindings, provider_options)
            except ValueError as exc:
                plan_store_raw, provider_candidate, registered_providers = (None, {}, [])
                warnings.append('ZCode 供应商规则库登记候选解析失败（本轮 ZCode 运行中，写入已推迟）：%s' % exc)
        else:
            plan_store_raw, provider_candidate, registered_providers = _provider_config_candidate(catalogs, plan, state, model_filter, bindings, provider_options)
        defer_reasons: list[str] = []
        settings_zcode = bool(settings_promoted and any((binding.consumer == 'ZCode' for binding in bindings)))
        if (zcode_changes or provider_candidate or settings_zcode) and defer_zcode_write:
            defer_reasons.append('ZCode 运行中（或运行状态无法确认，按运行中处理）：客户端配置不热写（%s）' % hot_detail)
        if (zcode_changes or provider_candidate or settings_zcode) and store_problem:
            if not _legacy_config_write_allowed(store_problem):
                reason = '供应商规则库%s（%s）不可用，不写半份配置' % (store_problem, ZCODE_PROVIDER_CONFIG)
                if store_problem == '文件缺席':
                    reason += '；旧版 config.json 迁移兼容性未取证（Rev11 迁移门），不以「缺席就写」替代迁移条件'
                defer_reasons.append(reason)
        store_lock = zcode_file_lock(ZCODE_PROVIDER_CONFIG) if provider_candidate and (not defer_reasons) and (not store_problem) else contextlib.nullcontext()
        with store_lock:
            store_conflict: str | None = None
            if provider_candidate and (not defer_reasons) and (not store_problem):
                conflict, merged_candidate, merged_registered = _locked_store_merge(catalogs, plan, state, model_filter, bindings, provider_options, plan_store_raw)
                if conflict is None:
                    provider_candidate = merged_candidate
                    registered_providers = merged_registered
                else:
                    store_conflict = conflict
                    defer_reasons.append(conflict)
                    provider_candidate, registered_providers = ({}, [])
            if defer_reasons and (zcode_changes or provider_candidate or settings_zcode or store_conflict):
                deferred_zcode = list(zcode_changes)
                deferred_clients = list(zcode_changes)
                if settings_promoted:
                    settings_deferred = list(settings_promoted)
                pending_changes.extend((change.description for change in zcode_changes))
                pending_details.extend(({'consumer': change.binding.consumer, 'kind': change.kind, 'model_id': change.model_id, 'description': change.description} for change in zcode_changes))
                if provider_candidate:
                    pending_changes.append('ZCode 供应商规则库（provider_config.json）随模型变更待对齐')
                reason_line = 'ZCode 侧写集合整体延期（%s）：本轮不写 config.json / provider_config.json，待条件就绪后按完整事务重跑' % '；'.join(defer_reasons)
                pending_changes.append(reason_line)
                pending_details.append({'consumer': 'ZCode', 'kind': 'provider_write_deferred', 'model_id': None, 'description': reason_line})
                zcode_changes = []
                provider_candidate = {}
                registered_providers = []
            candidates: dict[Path, str] = {}
            if zcode_changes and _legacy_config_write_allowed(store_problem):
                rendered = render_zcode_sync(zcode_raw, zcode_changes)
                if json.loads(rendered) != json.loads(zcode_raw):
                    candidates[ZCODE_JSON] = rendered
            if provider_candidate:
                candidates.update(provider_candidate)
            zcode_changed = ZCODE_JSON in candidates or ZCODE_PROVIDER_CONFIG in candidates
            next_state = plan.next_state
            if settings_deferred:
                for key in settings_deferred:
                    next_state['modelSettings'][key] = copy.deepcopy(original_state['modelSettings'][key])
            if deferred_clients:
                managed_after = next_state.setdefault('managed', {})
                managed_before = state.get('managed') or {}
                for change in deferred_clients:
                    if change.kind not in ('add', 'update') or change.model_id is None:
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
                        managed_after.pop(change.binding.key, None)
            for change in zcode_changes:
                if change.kind != 'remove' or change.model_id is None:
                    continue
                entries = next_state.get('managed', {}).get(change.binding.key)
                if isinstance(entries, dict):
                    entry = entries.get(change.model_id)
                    if isinstance(entry, dict) and entry.get('retired') is True:
                        entries.pop(change.model_id, None)
            next_state['pending'] = pending_changes
            next_state['lastRun'] = started.isoformat()
            next_state['lastStatus'] = 'pending' if pending_changes else 'changed' if zcode_changes or provider_candidate else 'warning' if warnings else 'normal'
            candidates[STATE_FILE] = json.dumps(next_state, ensure_ascii=False, indent=2) + '\n'
            if before_publish is not None:
                before_publish()
            published_paths = apply_config_transaction(candidates, BACKUP_DIR, keep_backups=30)
            readback = _readback_problems(candidates)
            if readback:
                raise ConfigPublishError('写入后读回校验失败：' + '；'.join((problem for _, problem in readback)) + '；文件已替换但内容不可证明（可能被并发写者改写）；不做回退（版本校验不通过，绝不盖回旧快照）；恢复/对账路径：重跑 python model_sync.py --apply，按当前客户端文件与目录重新规划', failed_path=readback[0][0], published=published_paths, left_in_place=[path for path, _ in readback])
        applied_changes = [change.description for change in zcode_changes]
        applied_details = [{'consumer': change.binding.consumer, 'kind': change.kind, 'model_id': change.model_id, 'description': change.description} for change in zcode_changes]
        for provider in registered_providers:
            line = 'ZCode 规则库登记供应商 %s（providerOrder/providerRules/模型白名单与规则）' % provider
            applied_changes.append(line)
            applied_details.append({'consumer': 'ZCode', 'kind': 'provider_registration', 'model_id': None, 'description': line})
            print('  - ' + line)
        if provider_candidate and (not applied_changes):
            line = 'ZCode 供应商规则库已对齐（provider_config.json）'
            applied_changes.append(line)
            applied_details.append({'consumer': 'ZCode', 'kind': 'provider_rule_store', 'model_id': None, 'description': line})
            print('  - ' + line)
    for change in deferred_zcode:
        if change.kind != 'remove' or change.model_id is None:
            continue
        pending_retirement.append({'consumer': change.binding.consumer, 'provider': change.binding.provider, 'model_id': change.model_id, 'state': 'pendingApply', 'evidenceRuns': 2, 'firstMissing': None, 'description': '（待应用）' + change.description})
    status = 'preview'
    if should_apply:
        if pending_changes:
            status = 'pending'
        elif applied_changes:
            status = 'changed'
        elif warnings:
            status = 'warning'
        else:
            status = 'normal'
    result = {'timestamp': started.isoformat(), 'code_stamp': CODE_STAMP, 'status': status, 'applied': applied_changes, 'pending': pending_changes, 'appliedDetails': applied_details, 'pendingDetails': pending_details, 'pendingRetirement': pending_retirement, 'overrideNotices': override_notices, 'warnings': warnings, 'zcodeChanged': zcode_changed, 'applicationBoundary': _application_boundary(published_paths, applied=should_apply)}
    if settings_preview is not None:
        result.update(settings_preview)
        result['settingsApplied'] = [] if settings_deferred or not should_apply else settings_promoted
        result['settingsDeferred'] = settings_deferred
    if apply:
        _write_result(result)
    else:
        result['plan'] = [change.description for change in plan.changes]
        result['plan'].extend(preview_lines)
        _write_preview(result)
    return result

def main() -> int:
    parser = argparse.ArgumentParser(description='AI网关模型目录同步')
    parser.add_argument('--apply', action='store_true', help='应用计划')
    parser.add_argument('--confirm', action='store_true', help='应用前确认')
    parser.add_argument('--probe', action='store_true', help='显式能力探测轮（真实计费请求，独立于同步应用；不带 --probe 的同步轮不发任何模型请求)')
    parser.add_argument('--scheduled', action='store_true', help='06:00计划任务模式')
    args = parser.parse_args()
    if args.scheduled:
        now = datetime.now().astimezone()
        if now.hour != 6 or now.minute > 10:
            print('已错过06:00执行窗口，本次跳过。')
            return 0
    try:
        with SyncLock():
            result = run_sync(apply=args.apply, run_probes=args.probe, confirm=args.confirm)
    except ConfigPublishError as exc:
        result = {'timestamp': datetime.now().astimezone().isoformat(), 'code_stamp': CODE_STAMP, 'status': 'failed', 'applied': [], 'pending': [], 'pendingRetirement': [], 'warnings': [f'{type(exc).__name__}: {exc}'], 'zcodeChanged': False, 'publish': _publish_recovery(exc)}
        print('模型同步失败：' + result['warnings'][0], file=sys.stderr)
        if args.apply:
            _write_result(result)
        else:
            _write_preview(result)
        return 1
    except Exception as exc:
        result = {'timestamp': datetime.now().astimezone().isoformat(), 'code_stamp': CODE_STAMP, 'status': 'failed', 'applied': [], 'pending': [], 'pendingRetirement': [], 'warnings': [f'{type(exc).__name__}: {exc}'], 'zcodeChanged': False}
        print('模型同步失败：' + result['warnings'][0], file=sys.stderr)
        if args.apply:
            _write_result(result)
        else:
            _write_preview(result)
        return 1
    return 2 if result['status'] in ('pending', 'warning') else 0
if __name__ == '__main__':
    raise SystemExit(main())
