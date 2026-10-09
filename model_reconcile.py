# -*- coding: utf-8 -*-
"""纯模型核对规则 module：输入已读取的快照，输出不可变 repair plan。

本 module 不打印、不读取环境、不访问网络、不写文件、不查询进程。
所有 I/O 由 adapter 在 composition root 中完成后传入。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
KNOWN_MODELS: dict[str, dict[str, Any]] = {'DeepSeek-V4-Flash-0731': {'efforts': ['low', 'medium', 'high', 'max'], 'default': 'high'}, 'GLM-5.2-NVFP4': {'efforts': ['low', 'medium', 'high', 'xhigh', 'max'], 'default': 'medium'}, 'Qwen3.8-27B': {'efforts': ['low', 'medium', 'xhigh'], 'default': 'xhigh'}, 'gpt-oss-120b': {'efforts': ['low', 'medium', 'high'], 'default': 'medium'}}
VARIANT_SUFFIXES = ('-Fast', '-Low', '-Medium', '-High')
UM_SUFFIX = '（UM）'

@dataclass(frozen=True)
class RepairItem:
    """一条不可变修复计划项。"""
    target: str
    kind: str
    mid: str
    desc: str
    ctx: int | None = None
    prov: str | None = None
    efforts: list[str] | None = None
    safe_auto: bool = True

@dataclass(frozen=True)
class ProblemItem:
    """一条不可变问题/信息项。"""
    text: str
    severity: str = 'problem'

@dataclass(frozen=True)
class ReconcileSnapshot:
    """核对所需的全部已读快照。全部可选——缺失的部分跳过。"""
    upstream: dict[str, int | None] = field(default_factory=dict)
    gw_models: list[str] = field(default_factory=list)
    zcode_config: dict[str, Any] | None = None
    effort_model_bases: set[str] = field(default_factory=set)
    gw_up: bool = False

@dataclass
class ReconcileResult:
    """核对结果：问题列表 + 修复计划。"""
    problems: list[ProblemItem] = field(default_factory=list)
    fixes: list[RepairItem] = field(default_factory=list)

def is_variant_of(mid: str, bases) -> bool:
    """mid 是否是某个基础模型的变体（-Fast/-Low/-Medium/-High）。"""
    for suf in VARIANT_SUFFIXES:
        if mid.endswith(suf) and mid[:-len(suf)] in bases:
            return True
    return False

def reconcile(snapshot: ReconcileSnapshot) -> ReconcileResult:
    """主核对入口：纯规则，无副作用。"""
    result = ReconcileResult()
    upstream = snapshot.upstream
    gw_models = set(snapshot.gw_models)
    _reconcile_zcode(snapshot, result)
    _reconcile_variants(upstream, gw_models, result)
    return result

def _reconcile_zcode(snapshot: ReconcileSnapshot, result: ReconcileResult) -> None:
    if snapshot.zcode_config is None:
        return
    upstream = snapshot.upstream
    try:
        ms = snapshot.zcode_config['provider']['local:openwebui']['models']
    except (KeyError, TypeError):
        result.problems.append(ProblemItem('ZCode config.json 结构异常'))
        return
    zc_ids = list(ms)
    for mid, m in ms.items():
        if upstream and mid not in upstream and (not is_variant_of(mid, upstream)):
            result.problems.append(ProblemItem('ZCode 中 %s 不在当前上游快照（仅警告，不删除）' % mid))
        elif upstream and mid in upstream:
            ctx = m.get('limit', {}).get('context')
            if ctx != upstream[mid]:
                result.problems.append(ProblemItem('ZCode 中 %s 上下文窗口与上游不符' % mid))
                result.fixes.append(RepairItem(target='ZCode', kind='ctx', mid=mid, ctx=upstream[mid], desc='ZCode %s 上下文窗口 → %s' % (mid, upstream[mid])))
            name = m.get('name') or ''
            if UM_SUFFIX not in name:
                result.fixes.append(RepairItem(target='ZCode', kind='name', mid=mid, desc='ZCode %s 显示名补%s' % (mid, UM_SUFFIX)))
    if upstream:
        eff_bases = snapshot.effort_model_bases
        for mid in sorted(upstream):
            if mid not in zc_ids:
                result.fixes.append(RepairItem(target='ZCode', kind='add', mid=mid, ctx=upstream[mid], efforts=KNOWN_MODELS.get(mid, {}).get('efforts'), desc='ZCode 新增 %s（ctx=%s，能力表%s）' % (mid, upstream[mid], '命中' if mid in KNOWN_MODELS else '未命中')))
            if mid in eff_bases and mid + '-Fast' not in zc_ids:
                result.fixes.append(RepairItem(target='ZCode', kind='add', mid=mid + '-Fast', ctx=upstream[mid], efforts=None, desc='ZCode 新增 %s-Fast（effort-model 注册变体）' % mid))

def _reconcile_variants(upstream, gw_models, result: ReconcileResult) -> None:
    """变体模型：基础模型下线则变体必然不可用。"""
    if not upstream:
        return
    bases = set(upstream)
    for mid in gw_models:
        for suf in VARIANT_SUFFIXES:
            if mid.endswith(suf) and mid[:-len(suf)] not in bases:
                result.problems.append(ProblemItem('变体 %s 的基础模型已下线' % mid))
