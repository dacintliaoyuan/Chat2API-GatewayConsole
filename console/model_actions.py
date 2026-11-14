# -*- coding: utf-8 -*-
"""Console adapters for the retained model-sync surfaces.

Ticket #32 retires the model sync/settings/task actions and the retired
consumer chain. The module now serves only the retained read/write surfaces:

* ``model.catalog`` reads the current sources' model directory through
  ``model_sync.collect_catalogs()`` — pure GETs, no probe, no billing, no
  client-config write, no SyncLock.
* ``model.context_probe_preview`` is a pure read of the persisted probe queue;
  ``model.context_probe_run`` performs the explicitly approved billed long-input
  requests inside the registered caps (≤20 requests, ≤64 tokens).
* ``model.context_overrides`` / ``set`` / ``clear`` read and write ONLY the
  model-sync state file under the existing SyncLock + transaction.

Page load never triggers synchronization, probing or client writes — only a
called handler has side effects.
"""

import json
import time
from datetime import datetime, timezone

from model_sync import sync as model_sync
from console.operations import (
    OperationContext,
    OperationError,
)

_ERROR_LIMIT = 200


def _single_line(text, limit=_ERROR_LIMIT):
    return " ".join(str(text).split())[:limit]


def _operation_failure(code, prefix, exc):
    """Map a model_sync native exception to one single-line Chinese
    OperationError carrying the reason (exception type + message)."""
    detail = _single_line("%s: %s" % (type(exc).__name__, exc))
    return OperationError(code, "%s：%s" % (prefix, detail))


def _scope_str_list(value, name):
    """Console params for sources/consumers: canonical shape is a list[str].

    The shared param-schema vocabulary (str/int/bool/object) has no list
    type, so the HTTP submit path encodes the array as a JSON string; the
    handler accepts both shapes and rejects everything else (fail-closed,
    subset validation happens in model_sync._normalize_scope).
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise OperationError(
                "invalid_params", "%s 必须为字符串数组" % name) from None
    if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value):
        raise OperationError(
            "invalid_params", "%s 必须为字符串数组" % name)
    return value


def _serialize_catalogs(catalogs):
    """Serialize collected CatalogModel lists for the console (additive fields).

    ``context_state``/``efforts_source``/``output_token_cap`` are the existing
    Rev8 read contract: declared vs unknown context, where the effort tiers came
    from, and the compatibility output cap (never presented as the UM
    declaration).  The same serializer serves the ordinary aggregate read and the
    strict-fresh UM refresh, so the two paths cannot drift.
    """
    serialized = {}
    for source, models in (catalogs or {}).items():
        serialized[source] = [
            {
                "id": model.model_id,
                "display_name": model.display_name,
                "context": model.context,
                # Rev8 B8-4 加性字段（2026-10-02）：证据状态与只读输出上限。
                # context_state：declared（目录给了声明）/unknown（缺席即
                # 未知，绝不虚构 0 或精确容量）；efforts_source 沿用既有
                # 来源标注（目录声明 vs 本地 KNOWN_MODELS 回退）。
                "context_state": ("declared" if model.context is not None
                                  else "unknown"),
                # 只读输出上限：与既有客户端输出上限的
                # 既有 setdefault(16384) 兼容默认同源——16,384 标兼容默认，
                # 不新增手动编辑接口，也绝不静默缩短输出或截断输入。
                "output_token_cap": {"value": 16384,
                                     "source": "compat_default"},
                "efforts": list(model.efforts),
                "default_effort": model.default_effort,
                "publish": bool(model.publish),
                # P1 (2026-09-24): where the efforts came from — the
                # console must not label every tier "upstream-declared"
                # while the UM directory actually read the local
                # KNOWN_MODELS table.
                "efforts_source": model.efforts_source,
            }
            for model in (models or [])
        ]
    return serialized


def model_catalog(target, params, ctx=None):
    """读各来源实时模型目录（只读 collect_catalogs，无探测/无计费/无启停）。

    直接复用 ``model_sync.collect_catalogs()``：默认启用的来源只剩 UM，
    逐一 ``GET /v1/models``（或等价目录端点），单源失败只进 warnings
    不拖垮其余源。不持 SyncLock（该函数不写任何共享状态）。
    CatalogModel 的 ``efforts`` 是 tuple，序列化为 list。

    R7（2026-10-10）显式来源范围：``sources``（列表或 JSON 字符串，与
    scope/probe 选择同一约定）只读所选来源——单来源只读该来源，未选来源
    绝不请求；未知或退役来源、非法形态明确拒绝（invalid_params），绝不
    静默退回全量。不带 ``sources`` 保持「全部模型」聚合入口（UM）。
    响应的 ``sources`` 如实列出本轮实际读取的来源集合。

    B2（2026-11-01）严格刷新：``fresh=True`` 只接受 ``sources=["um"]``，
    沿 8000 /v1/models → UM /api/models 发 ``X-Chat2Api-Models:
    raw-fresh``，响应必须带网关的 raw-fresh 标记才算成功；结果显式返回
    ``success``/``fresh``/``status``/``collected_at``/``source``，失败时
    可展示上次成功数据但标 ``stale=True``（绝不用旧缓存 HTTP 200 宣告
    刷新成功），认证页/错误页/合法空目录/不可达分开。刷新不发推理、
    不探测、不写客户端/Magpie 配置、不触发旧同步 apply。不带 ``fresh``
    的旧调用保持原行为（不发严格取新头）。
    """
    if ctx is not None:
        ctx.raise_if_cancelled()
    params = params or {}
    unknown = set(params) - {"sources", "fresh"}
    if unknown:
        raise OperationError(
            "invalid_params", "未知参数：%s" % ",".join(sorted(unknown)))
    fresh = params.get("fresh", False)
    if not isinstance(fresh, bool):
        raise OperationError("invalid_params", "fresh 必须为布尔值")
    sources = None
    if params.get("sources") is not None:
        sources = _scope_str_list(params["sources"], "sources")
        if not sources:
            # 空选不回退为全量：全部模型请省略 sources（聚合入口）。
            raise OperationError(
                "invalid_params", "sources 不能为空列表；全部模型请省略 sources")
        if len(set(sources)) != len(sources):
            raise OperationError("invalid_params", "sources 不能重复")
        unknown_sources = [source for source in sources
                           if source not in model_sync.ACTIVE_SOURCES]
        if unknown_sources:
            # 未知/退役来源在边界就拒绝（与 collect_catalogs 同一启用
            # 来源表，绝不把拒绝交给下游或被 mock 吞掉）。
            raise OperationError(
                "invalid_params",
                "sources 必须为当前启用来源 %s 的子集（退役来源已不再同步）：%s"
                % (", ".join(model_sync.ACTIVE_SOURCES),
                   ",".join(unknown_sources)))
    if fresh:
        # B2（2026-11-01）：控制台「刷新UM模型列表」只选 UM，绝不触其他
        # 来源。严格取新分支走 chat2api 的 raw-fresh 头；返回显式的
        # 成功/失败、采集时间、来源与新鲜度，失败时保留的上次成功数据
        # 明确标为陈旧，绝不用旧缓存 HTTP 200 宣告刷新成功。本分支只读：
        # 不发模型推理、不探测、不写客户端/Magpie 配置、不触发旧同步 apply。
        if sources != ["um"]:
            raise OperationError(
                "invalid_params",
                "严格刷新只支持 sources=['um']（控制台刷新只选 UM，"
                "不触其他来源）")
        try:
            catalogs, warnings, report = model_sync.collect_catalogs_report(
                sources=["um"], fresh=True)
        except ValueError as exc:
            raise OperationError("invalid_params", _single_line(exc)) from exc
        except Exception as exc:
            raise _operation_failure(
                "catalog_failed", "模型目录刷新失败", exc) from exc
        report = report or {}
        refresh = {key: value for key, value in report.items()
                   if key != "models"}
        return {
            "catalogs": _serialize_catalogs(catalogs),
            "warnings": list(warnings or []),
            "sources": ["um"],
            "checked_epoch": report.get("checked_epoch", int(time.time())),
            # 显式刷新结果：success/fresh/status/collected_at/source 分开，
            # stale_fallback 表示本轮失败后展示的是上次成功数据（非本次成功）。
            "refresh": refresh,
            "fresh": bool(report.get("fresh")),
            "stale": bool(report.get("stale_fallback")),
            "collected_at": report.get("collected_at"),
            "source": "um",
            "success": bool(report.get("success")),
        }
    try:
        catalogs, warnings = model_sync.collect_catalogs(sources=sources)
    except ValueError as exc:
        raise OperationError("invalid_params", _single_line(exc)) from exc
    except Exception as exc:
        raise _operation_failure("catalog_failed", "模型目录查询失败", exc) from exc
    serialized = _serialize_catalogs(catalogs)
    return {
        "catalogs": serialized,
        "warnings": list(warnings or []),
        # R7：本轮实际读取的来源集合（显式 sources 的子集，或全部聚合入口
        # 的启用来源全集）——消费方不必猜本次读了哪些来源。
        "sources": list(sources) if sources is not None
        else list(model_sync.ACTIVE_SOURCES),
        "checked_epoch": int(time.time()),
    }


# ---------------------------------------------------------------------------
# Declared-context probe queue (L-E.4): preview is pure read; run performs
# REAL billed long-input requests.  The action is registered ENABLED (2026-09-22
# maintainer approval of the budget and request list) and stays an explicit,
# centrally-approved action: selection/retest are wired, every call is a real
# billing decision the maintainer makes on purpose.  The preview additionally
# READS each endpoint source's credential file once via its token provider
# (read-only, no side effects) so a missing credential is previewed as
# non-executable instead of runnable.
# ---------------------------------------------------------------------------

_COST_NOTE = "未定价/无法核算：本地网关无计价依据，不编造金额"
# B3（2026-11-01）：有界逐模型进度与结果读面。整批预算最多 20 请求，
# 进度最多 20 条；结果行与恢复证据各有硬上限，页面重连读原任务，
# 不因一次任务过大让操作结果无界增长。
_PROBE_PROGRESS_MAX = 20
_PROBE_RESULT_MAX_ROWS = 100
_PROBE_RECOVERY_MAX_RECORDS = 100



def _int_or_none(value):
    """One integer guard for the preview's numeric record fields.

    A bool or non-int (str/float/None) is NOT trusted and reads as None
    (unknown): a stale garbage value must never be passed through raw and must
    never crash the read-only preview.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def model_context_probe_preview(target, params, ctx=None):
    """上下文探测队列只读预览：队列、计划与估算；零请求、零写入。

    读面契约（Plan-Revision 7 第 2 步）：后端规范字段为 ``records``、输出
    上限统一名为 ``output_token_cap``。前端当前读 ``queue`` 并把类型定义为
    ``output_tokens_cap``，属接口错配——只登记给后续前端轮处理；后端不为
    迁就展示改名，也不造假字段或别名。

    每条记录携带：单位（``input_unit``="chars"／``output_unit``="tokens"，
    步进是字符数，不做字符→token 换算）；证据来源与时间（``declared_at``
    声明时间、``last_run`` 最后尝试时间、``last_success`` 最后成功时间）；
    ``last_error``；证据修订 ``evidence_revision``；过期原因 ``stale_reason``。
    ``observed_floor``/``observed_ceiling`` 只按历史字符步进解释，不是 token
    容量；上游 usage 报告的输入 token 另列 ``token_evidence``——它是某次
    成功请求的上游输入 Token 报告（下界证据），不是容量；0 是合法值，判断
    未知须用 is None（缺席即 Token 容量未知），禁止字符换算。无声明上下文
    的模型照常入队（Plan-Revision 7 第 4 步）：``declared_context`` 为空即
    声明未知（``declared_context_known``=False），记录自带说明——探测仍可
    测量实际可用长度，绝不把未知解释成零容量。存量记录归一（additive）：
    缺 ``declared_context_known`` 按 declared_context is not None 派生显示；
    ``scheme_revision`` 缺字段=引入修订号前的祖传记录，按修订 1 解释；
    ``evidence_revision`` 缺失保持 None（None=引入修订号前的祖传证据）；
    ``history_count`` 缺省 0。字段加性演进：旧状态缺新字段一律读作 None，
    不报错；记录缺席表示未知，不表示已实测或零容量。``attempts``/
    ``observed_ceiling``/``declared_context`` 与 ``observed_floor`` 同用
    整数守卫（isinstance(bool)/int）：存量垃圾值读作 None（未知），不原样
    透出、不让脏数据炸掉只读预览。

    显式选择（与 run 动作同一套校验，Plan-Revision 7 第 3 步）：``sources``
    （来源子集，列表或 JSON 字符串）与 ``models``（{source: [model_id]}）
    把估算限定到被选记录——未选中记录 ``estimated_requests``/
    ``input_chars_estimate`` 为 0、不进 totals（idle_records 口径）；不带
    选择参数保持全队列语义（无参数时与旧行为语义一致；输出为加性字段，
    源身份/凭据/归一按 runner 同口径）。``retest=True`` 时选中范围内
    verified/failed 记录按「先归档旧代次、再按全梯重测」计：估算=全梯
    步进数（与 run 的真实请求数一致——限 run 单次预算 max_requests
    用尽之前），``estimate_note`` 如实说明。
    选择校验复用 model_sync.normalize_probe_selection /
    resolve_probe_selection：无法确认的选择（未知来源、已暂停来源、空集合、
    非法类型、models 引用不在队列的模型）invalid_params 明确拒绝，绝不
    静默退回全队列。

    凭据只读检查：预览会对已登记探测端点的来源调用其 token provider
    （当前只有 um，且无需 token，不检查）。凭据不可读（token 缺失）时该
    来源记录 ``executable``=False、估算为 0 并如实说明——与 runner「凭据
    不可读零请求跳过」一致；凭据可读时 executable=True。本预览不发任何
    模型请求、不写任何状态。

    来源身份与 runner 同口径：状态记录缺 ``source``/``model_id`` 字段
    （手改状态）时按 key 的 source|model_id 前后缀兜底，不再显示 None 或
    因未知来源零请求。估算与 runner 实际行为一致：无探测端点的来源在
    runner 零请求跳过，其记录估算为 0，并以
    ``probe_endpoint``/``paused``/``estimate_note`` 如实标注。
    ``totals`` 统一计数定义（三分口径，三者之和=有效记录条数；非 dict 的
    垃圾记录两侧都跳过）：
    ``runnable_records``=本轮会消耗请求的记录（pending/stale，或 retest 下
    的 verified/failed；且有端点且凭据可读）；``blocked_records``=本轮有
    资格但不可执行（active 但无端点或凭据不可读）；``idle_records``=本轮
    不消耗请求（verified/failed 且未 retest、未知状态、或未选中，或 pending
    但既有证据已覆盖全梯——runner 对它是零请求并直接置 verified）。队列在
    每次目录同步后播种（model_sync.seed_context_probes）；本动作只读现有
    state（无 SyncLock、不播种、不发送任何模型请求）；输出上限为常量；
    绝不返回伪造费用金额。
    """
    if ctx is not None:
        ctx.raise_if_cancelled()
    params = params or {}
    # 显式选择与 run 共用同一校验（sources/models/retest 的形状、retest
    # 布尔、非法类型）；request=None 表示未给选择参数（全队列语义）。
    selection_request, retest = _probe_run_selection(params)
    state = model_sync.load_state()
    records = state.get("contextProbes")
    if not isinstance(records, dict):
        records = {}
    # 选择解析复用 runner 同一函数：{source: None} 展开为该来源全部在队
    # 模型；models 引用不在队列的模型等无法确认的选择 → invalid_params，
    # 绝不静默退回全队列。只读预览：解析只作用于本次读取的 records 快照，
    # 不取锁、不写状态。
    try:
        selection = model_sync.resolve_probe_selection(
            selection_request, records)
    except ValueError as exc:
        raise OperationError("invalid_params", _single_line(exc)) from exc
    steps = list(model_sync.PROBE_INPUT_STEPS)
    entries = []
    # totals 三分口径（三者之和 = records 条数）：runnable=本轮会消耗
    # 请求的记录（active 且有端点且凭据可读）；blocked=本轮有资格但不可
    # 执行（active 但无端点或凭据不可读）；idle=本轮不消耗请求（verified/
    # failed 且未 retest、未知状态、未选中，或 pending 但 floor 已覆盖全梯
    # ——runner 对它是零请求并直接置 verified）。
    totals = {"estimated_requests": 0, "estimated_input_chars": 0,
              "runnable_records": 0, "blocked_records": 0,
              "idle_records": 0}
    # 凭据只读检查按来源缓存：判据收在 model_sync.endpoint_credential 单
    # 一实现（与 runner 同一函数，绝不漂移），token provider 是只读文件
    # 读，预览期间每来源最多调用一次（零副作用、零网络、零写入）。
    credential_readable: dict[str, bool] = {}
    for key, record in list(records.items()):
        if not isinstance(record, dict):
            continue
        status = record.get("status")
        # 历史字符步进证据（chars，不是 token 容量）：floor/ceiling 只按
        # 当时的字符步进表解释，估算也按字符数计划，不换算 token。垃圾值
        # 防御：非整数 floor 不构成已知步进证据，按无 floor 处理，绝不
        # 让存量脏数据炸掉只读预览。attempts/observed_ceiling/
        # declared_context 同口径：非法旧值读作 None（未知），不原样透出。
        floor = _int_or_none(record.get("observed_floor"))
        ceiling = _int_or_none(record.get("observed_ceiling"))
        attempts = _int_or_none(record.get("attempts"))
        declared = _int_or_none(record.get("declared_context"))
        # 来源身份与 runner 同口径（model_sync.probe_context_queue）：状态
        # 记录缺 source/model_id 字段（手改状态）时按 key 的 source|model_
        # id 前后缀兜底，预览与 runner 的身份口径一致。
        prefix, _, suffix = str(key).partition("|")
        source = str(record.get("source") or prefix)
        model_id = str(record.get("model_id") or suffix)
        endpoint = model_sync.CONTEXT_PROBE_ENDPOINTS.get(source)
        probe_endpoint = endpoint is not None
        paused = source in model_sync.PAUSED_SOURCES
        if endpoint is not None and source not in credential_readable:
            # 与 runner 同一函数（F9 口径）：provider 存在且解析不出
            # token → 凭据不可读，零请求跳过。
            credential_readable[source] = model_sync.endpoint_credential(
                endpoint)[2]
        executable = (probe_endpoint and
                      credential_readable.get(source, False))
        selected = (selection is None or
                    (source in selection and model_id in selection[source]))
        # runner 资格（F9 口径）：pending/stale 直接入列；verified/failed
        # 仅显式 retest（先归档旧代次再按全梯重测）。stale 记录的旧证据
        # 属于归档代次，估算同样按全梯。
        eligible = (status in ("pending", "stale") or
                    (status in ("verified", "failed") and retest))
        # pending 记录的 floor 是当前代次证据（续跑跳过已测步进）；stale
        # 记录的旧证据属于归档代次，触碰时先归档再从全表重测，估算按全表。
        estimate_floor = floor if status == "pending" else None
        ladder = [step for step in steps
                  if estimate_floor is None or step > estimate_floor]
        will_request = selected and eligible and executable and bool(ladder)
        estimated_requests = len(ladder) if will_request else 0
        input_chars_estimate = sum(ladder) if will_request else 0
        if not selected:
            estimate_note = "本轮未选中（不在请求集合），实际零请求"
            totals["idle_records"] += 1
        elif not eligible:
            # verified/failed 且未 retest（或未知状态）：本轮零请求。
            estimate_note = None
            totals["idle_records"] += 1
        elif not probe_endpoint:
            estimate_note = ("该来源已暂停，实际零请求" if paused else
                             "该来源未登记探测端点，实际零请求")
            totals["blocked_records"] += 1
        elif not executable:
            estimate_note = "该来源凭据不可读（token 缺失），实际零请求"
            totals["blocked_records"] += 1
        elif not ladder:
            # pending 且 floor 已覆盖全梯：runner 对该记录零请求并直接置
            # verified（model_sync 的「既有实测证据已覆盖全部步进」分支），
            # 故本轮不消耗请求——与 runnable 的定义（本轮会消耗请求）一致。
            estimate_note = "既有实测证据已覆盖全部步进，无需新请求"
            totals["idle_records"] += 1
        else:
            # selected + eligible + executable：本轮按估算消耗请求。
            estimate_note = ("retest：先归档旧代次再按全梯重测"
                             if status in ("verified", "failed") else None)
            totals["runnable_records"] += 1
        totals["estimated_requests"] += estimated_requests
        totals["estimated_input_chars"] += input_chars_estimate
        history = record.get("history")
        # F4 存量记录归一：缺 declared_context_known 按归一后的
        # declared_context 派生；scheme_revision 缺失按修订 1 解释（祖传
        # 记录）；evidence_revision 缺失保持 None（祖传证据）；history_
        # count 缺省 0。
        known = (record["declared_context_known"]
                 if "declared_context_known" in record
                 else declared is not None)
        scheme_revision = record.get("scheme_revision")
        if scheme_revision is None:
            scheme_revision = 1
        entries.append({
            "source": source,
            "model_id": model_id,
            "declared_context": declared,
            "declared_context_known": known,
            "declared_note": record.get("declared_note"),
            "declared_at": record.get("declared_at"),
            "status": status,
            "observed_floor": floor,
            "observed_ceiling": ceiling,
            "attempts": attempts,
            "last_run": record.get("last_run"),
            "last_success": record.get("last_success"),
            "last_error": record.get("last_error"),
            "evidence_revision": record.get("evidence_revision"),
            "stale_reason": record.get("stale_reason"),
            "token_evidence": record.get("token_evidence"),
            "scheme_revision": scheme_revision,
            "history_count": len(history) if isinstance(history, list) else 0,
            "history": [{
                **{field: _int_or_none(item.get(field)) for field in (
                    "evidence_revision", "observed_floor", "observed_ceiling",
                    "token_evidence", "attempts")},
                **{field: (_single_line(item[field]) if isinstance(item.get(field), str) else None)
                   for field in ("status", "last_run", "last_success", "last_error",
                                 "stale_reason", "reason", "archived_at")},
                "character_unit": "chars", "token_unit": "tokens",
            } for item in (history[-model_sync.PROBE_HISTORY_MAX:]
                           if isinstance(history, list) else []) if isinstance(item, dict)],
            "probe_endpoint": probe_endpoint,
            "paused": paused,
            "executable": executable,
            "estimate_note": estimate_note,
            "input_unit": "chars",
            "output_unit": "tokens",
            "estimated_requests": estimated_requests,
            "input_chars_estimate": input_chars_estimate,
            "output_token_cap": model_sync.CONTEXT_PROBE_MAX_TOKENS,
        })
    return {
        "records": entries,
        "steps": steps,
        "totals": totals,
        "unit_note": (
            "步进与 input_chars_estimate 为字符数（input_unit=chars），"
            "输出上限为 token（output_unit=tokens）；observed_floor/"
            "observed_ceiling 是历史字符步进证据，不是 token 容量；"
            "token_evidence 为某次成功请求的上游输入 Token 报告（下界"
            "证据），不是容量；0 是合法值，判断未知须用 is None（缺席即 "
            "Token 容量未知）；不做字符→token 换算；scheme_revision 缺"
            "字段=引入修订号前的祖传记录，按修订 1 解释；evidence_revision "
            "None=引入修订号前的祖传证据；declared_context 为空即声明"
            "未知（declared_context_known=False），探测仍可测量实际可用"
            "长度；记录缺席表示未知，不表示已实测或零容量"),
        "budget_note": (
            "估算按固定步进表 %s（输入为字符数，不做字符→token 换算）；"
            "无探测端点或凭据不可读的来源（含已暂停）runner 零请求跳过，"
            "其记录估算为 0，totals 可核对总额；totals 三分口径 runnable+"
            "blocked+idle=records 条数：runnable=本轮会消耗请求（pending/"
            "stale 或 retest 下的 verified/failed，且有端点且凭据可读），"
            "blocked=本轮有资格但无端点或凭据不可读，idle=本轮不消耗请求"
            "（verified/failed 未 retest、未选中，或 pending 但既有证据已覆盖"
            "全梯——runner 零请求直接 verified）；全局请求预算口径："
            "run 动作单次 max_requests ≤ 20、单请求输出 max_tokens ≤ %d；"
            "未定价来源不做金额估算" % (
                steps, model_sync.CONTEXT_PROBE_MAX_TOKENS)),
        "cost_note": _COST_NOTE,
        # B3：保存失败时保留的已计费证据（只读；成功保存后清空）。
        "recovery": model_sync.read_probe_recovery(),
        "note": ("队列在每次目录同步后播种；本预览不发请求、不写状态"
                 "（凭据文件只读读一次）"),
    }


def _probe_run_selection(params):
    """Validate the explicit probe selection params (sources/models/retest).

    Shared by the run handler AND the read-only preview — the preview must
    estimate exactly the request set the run would bill, so both reuse this
    validator.  Mirrors the sync-apply scope conventions: ``sources`` arrives
    as a list or a JSON-encoded string (the shared param-schema vocabulary
    has no list type), ``models`` as {source: [model_id, ...]}.  Any
    selection that cannot be confirmed raises OperationError("invalid_params")
    — neither handler is ever silently widened to the whole queue.  Returns
    the validated (request, retest) pair or (None, False)-shaped defaults
    when no selection param is present (legacy full-queue semantics).
    """
    sources = None
    models = None
    if params.get("sources") is not None:
        sources = _scope_str_list(params["sources"], "sources")
    if params.get("models") is not None:
        models = params["models"]
        if not isinstance(models, dict) or not models:
            raise OperationError(
                "invalid_params", "models 必须为非空的 {source: [model_id, "
                "...]} 对象")
        for source, ids in models.items():
            if not isinstance(source, str) or not source:
                raise OperationError(
                    "invalid_params", "models 键必须为非空来源名")
            if not isinstance(ids, list) or not ids or any(
                    not isinstance(mid, str) or not mid for mid in ids):
                raise OperationError(
                    "invalid_params",
                    "models[%s] 必须为非空 model_id 列表" % source)
    retest = params.get("retest", False)
    if not isinstance(retest, bool):
        raise OperationError("invalid_params", "retest 必须为布尔值")
    try:
        request = model_sync.normalize_probe_selection(sources, models)
    except ValueError as exc:
        raise OperationError("invalid_params", _single_line(exc)) from exc
    return request, retest


def _explicit_probe_models(selection_request):
    """Return the explicitly named {source: {model_id}} selection, or None.

    ``sources``-only selections are not an explicit model set (they expand to
    the legacy queue); ``models`` selections are the B3 frontend contract —
    an explicit UM source plus an explicit ID set.
    """
    if not isinstance(selection_request, dict):
        return None
    explicit = {
        source: set(ids)
        for source, ids in selection_request.items()
        if isinstance(ids, (set, frozenset, list, tuple))}
    return explicit or None


def _selection_payload(selection):
    if selection is None:
        return None
    return {source: sorted(ids) for source, ids in sorted(selection.items())}


def _selected_record_keys(selection, records,
                          limit=_PROBE_RECOVERY_MAX_RECORDS):
    keys = []
    for key, record in (records or {}).items():
        if not isinstance(record, dict):
            continue
        source, _, model_id = str(key).partition("|")
        source = str(record.get("source") or source)
        model_id = str(record.get("model_id") or model_id)
        if selection is not None:
            allowed = selection.get(source)
            if not isinstance(allowed, (set, frozenset, list, tuple)) or \
                    model_id not in allowed:
                continue
        keys.append(str(key))
        if len(keys) >= limit:
            break
    return keys


def _report_probe_progress(ctx, outcome):
    """Bounded per-model progress + final line (operation-layer steps).

    Only records that actually sent at least one request produce a progress
    line, capped at _PROBE_PROGRESS_MAX (the whole-batch request budget is
    ≤20).  Progress-report failures never fail a billed run.
    """
    if ctx is None or not hasattr(ctx, "report_step"):
        return
    rows = [row for row in (outcome.get("results") or [])
            if isinstance(row, dict)]
    executed = [row for row in rows if row.get("requests")]
    for row in executed[:_PROBE_PROGRESS_MAX]:
        note = "上下文探测 %s：%s，请求 %d" % (
            row.get("key"), row.get("status"),
            int(row.get("requests") or 0))
        try:
            ctx.report_step(_single_line(note))
        except Exception:
            pass
    stopped = outcome.get("stopped")
    try:
        ctx.report_step(_single_line(
            "上下文探测结束：发送 %d 请求%s" % (
                int(outcome.get("used") or 0),
                "（%s 停止）" % stopped if stopped else "")))
    except Exception:
        pass


def _probe_result(outcome, selection, retest):
    """Bounded result read face for the operation layer / reconnecting page."""
    used = int(outcome.get("used") or 0)
    stopped = outcome.get("stopped")
    rows = [row for row in (outcome.get("results") or [])
            if isinstance(row, dict)]
    executed = [row for row in rows if row.get("requests")]
    not_executed = [row for row in rows if not row.get("requests")]
    summary = "上下文探测结束：发送 %d 请求" % used
    if stopped:
        summary += "（%s 停止）" % stopped
    return {
        "requests_used": used,
        "stopped": stopped,
        "results": rows[:_PROBE_RESULT_MAX_ROWS],
        "result_total": len(rows),
        "results_truncated": len(rows) > _PROBE_RESULT_MAX_ROWS,
        "executed_models": [row.get("key") for row in
                           executed[:_PROBE_RESULT_MAX_ROWS]],
        "executed_total": len(executed),
        "not_executed": [{
            "key": row.get("key"), "status": row.get("status"),
            "note": row.get("note")} for row in
            not_executed[:_PROBE_RESULT_MAX_ROWS]],
        "not_executed_total": len(not_executed),
        "budget": {
            "max_requests": int(outcome.get("max_requests") or 0),
            "max_tokens": int(outcome.get("max_tokens") or 0),
            "requests_used": used,
            "remaining_requests": max(
                0, int(outcome.get("max_requests") or 0) - used),
        },
        "retest": retest,
        "selection": _selection_payload(selection),
        "summary": _single_line(summary),
        "cost_note": _COST_NOTE,
    }


def _preserve_probe_evidence(records, selection, outcome, exc):
    """Preserve billed evidence when the state transaction fails (B3).

    Writes the ACTUAL post-run records/results to a bounded recovery file
    next to the state file, stamped persistence="failed".  The main state file
    keeps its previous successful evidence; this recovery file is never
    presented as a successful state save.  Returns a one-line note for the
    error message (never masks the original failure if recovery also fails).
    """
    try:
        path = model_sync.write_probe_recovery({
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "persistence": "failed",
            "error": _single_line("%s: %s" % (type(exc).__name__, exc)),
            "selection": _selection_payload(selection),
            "requests_used": int(outcome.get("used") or 0),
            "stopped": outcome.get("stopped"),
            "results": [row for row in (outcome.get("results") or [])
                        if isinstance(row, dict)][:_PROBE_RESULT_MAX_ROWS],
            "records": {
                key: records[key]
                for key in _selected_record_keys(selection, records)},
            "note": ("状态事务保存失败；本文件保留本轮已计费证据，"
                     "未写入主状态"),
        })
        return "；已计费证据保留到恢复文件 %s" % path.name
    except Exception as recovery_exc:
        return "；已计费证据恢复文件写入失败：%s" % type(
            recovery_exc).__name__


def model_context_probe_run(target, params, ctx=None):
    """执行上下文探测队列（真实长输入计费请求——注册已启用）。

    handler 与 runner 复用同一队列语义：显式预算校验（max_requests ≤ 20、
    max_tokens ≤ 64），SyncLock 内读改写 state 的 contextProbes（经既有
    备份+解析校验事务），端点与取消钩子注入
    model_sync.probe_context_queue（取消钩子在每个请求前检查；取消/预算
    用尽时已发生的实测证据仍如实持久化）。动作注册状态为 enabled（2026-09-22
    维护者批准预算与请求清单后启用）：选择（sources/models）与重测
    （retest）均已接线，但它仍是集中批准后的显式动作——每次调用都是维护者
    有意发起的真实计费决策，本 handler 不解析伪造成功。

    选择与重测（Plan-Revision 7 第 3 步）：sources（来源子集，列表或 JSON
    字符串）与 models（{source: [model_id]}）把请求集合限定到被选记录，
    未选中记录本轮零请求零改动；选择无法确认（未知来源、已暂停来源、空
    集合、非法类型、models 引用不在队列的模型）一律 invalid_params 明确
    拒绝，绝不静默退回全队列；不带选择参数的既有调用保持全队列语义。
    retest=True 时 verified/failed 记录先归档旧代次证据到 history 再从
    独立证据重测；stale 记录的旧证据同样在触碰时归档。并发/重复提交复用
    既有 SyncLock 与状态事务，不新增第二套锁。

    B3（2026-11-01）独立批量上下文任务：操作层入口必须提交明确的 UM
    来源与模型 ID 集合（空选/非法选择拒绝，不回退全队列）；显式选中但
    尚未在队列的新模型先在只读目录里确认（退役/非法模型明确拒绝），
    然后只补齐所选记录并测试——不调用已退役的客户端同步。S2 high #6
    （2026-11-01）：操作层对已入队模型也做同一只读目录校验，已入队但
    已从当前目录退役的模型同样拒绝（invalid_params，零请求）；目录读取
    失败单独返回 catalog_failed（失败不是退役，不冒充退役、也不静默放行）。
    整批累计最多 20 请求、每请求输出最多 64 Token（不按模型各自重置）；
    预算耗尽的剩余模型在结果中明确标为本轮未执行。结果返回有界逐模型进度
    与结果读面，保存失败时本轮已计费证据写入恢复文件，绝不丢失或伪造。
    """
    if ctx is not None:
        ctx.raise_if_cancelled()
    params = params or {}
    max_requests = params.get("max_requests", 20)
    max_tokens = params.get("max_tokens", model_sync.CONTEXT_PROBE_MAX_TOKENS)
    for name, value, cap in (
            ("max_requests", max_requests, 20),
            ("max_tokens", max_tokens, model_sync.CONTEXT_PROBE_MAX_TOKENS)):
        if isinstance(value, bool) or not isinstance(value, int) or \
                not 1 <= value <= cap:
            raise OperationError(
                "invalid_params", "%s 必须为 1-%d 的整数" % (name, cap))
    selection_request, retest = _probe_run_selection(params)
    explicit_models = _explicit_probe_models(selection_request)
    # B3（2026-11-01）：操作层是用户显式点击的 run 入口，必须提交明确的
    # UM 来源与模型 ID 集合。sources-only 或完全省略选择都不再回退全队列
    # （旧的直接 handler 调用保留全队列兼容语义，但生产页面永远走操作层）。
    if isinstance(ctx, OperationContext) and explicit_models is None:
        raise OperationError(
            "invalid_params",
            "必须提交明确的 UM 来源与模型 ID 集合；空选或遗漏参数不回退全队列")
    if isinstance(ctx, OperationContext) and set(explicit_models) != {"um"}:
        raise OperationError(
            "invalid_params", "操作层上下文探测只接受明确的 UM 来源")
    budget = {"max_requests": max_requests, "max_tokens": max_tokens}
    cancel_event = getattr(ctx, "cancel_event", None) if ctx is not None \
        else None
    if cancel_event is not None:
        should_continue = lambda: not cancel_event.is_set()  # noqa: E731
    else:
        should_continue = lambda: True  # noqa: E731
    try:
        with model_sync.SyncLock():
            state = model_sync.load_state()
            # B3：上一次状态事务保存失败时保留的已计费证据，在本次
            # 成功事务里按 last_run 合并（旧证据不覆盖新证据），然后
            # 成功保存后清理恢复文件。
            model_sync.merge_probe_recovery(state)
            records = state.get("contextProbes")
            if not isinstance(records, dict):
                records = {}
                state["contextProbes"] = records
            # B3（2026-11-01）：选择/播种与客户端同步解耦。显式选中
            # 的 UM 模型若尚未在队列，先在只读目录里确认（退役/非法模型
            # 明确拒绝），然后只补齐所选记录；不调用已退役的客户端同步。
            # 目录刷新（model.catalog）仍保持只读，不在此播种。
            # S2 high #6（2026-11-01）：操作层 run 对队列内模型也做同一
            # 只读目录校验——已入队但已从当前目录退役的模型同样拒绝
            # （invalid_params，零请求），绝不照常发真实计费请求。目录读取
            # 失败抛 ProbeCatalogUnavailable，单独映射为 catalog_failed：
            # 读取失败不是退役，不冒充退役、也不静默放行。
            if explicit_models is not None:
                try:
                    seed_catalogs = model_sync.resolve_probe_selection_catalogs(
                        explicit_models, records,
                        validate_queued=isinstance(ctx, OperationContext))
                except model_sync.ProbeCatalogUnavailable as exc:
                    # 操作层（生产入口）单独返回 catalog_failed；旧直接
                    # handler 保留 invalid_params 兼容语义，但错误文案仍明确
                    # “读取失败，不是退役”。
                    code = ("catalog_failed"
                            if isinstance(ctx, OperationContext)
                            else "invalid_params")
                    raise OperationError(code, _single_line(exc)) from exc
                except ValueError as exc:
                    raise OperationError(
                        "invalid_params", _single_line(exc)) from exc
                if seed_catalogs:
                    model_sync.seed_context_probes(
                        seed_catalogs, state,
                        model_filter={
                            source: {model.model_id for model in models}
                            for source, models in seed_catalogs.items()},
                        allow_partial_catalog=True)
            # 只有「选择无法确认」是参数问题：ValueError 映射收窄到
            # resolve_probe_selection，持久化阶段抛出的 ValueError 不再被
            # 误标成 invalid_params（此时请求已计费）。
            try:
                selection = model_sync.resolve_probe_selection(
                    selection_request, records)
            except ValueError as exc:
                raise OperationError(
                    "invalid_params", _single_line(exc)) from exc
            # B3：对用户显式点「上下文探测」的已测模型按既有重测归档
            # 规则处理（不静默跳过）；sources-only 选择保持旧的全队列
            # 语义（未显式指定已测模型）。
            if explicit_models is not None and not retest:
                retest = any(
                    isinstance(records.get("%s|%s" % (source, model_id)), dict)
                    and records["%s|%s" % (source, model_id)].get("status")
                    in ("verified", "failed")
                    for source, ids in explicit_models.items()
                    for model_id in ids)
            outcome = model_sync.probe_context_queue(
                model_sync.CONTEXT_PROBE_ENDPOINTS, budget, records,
                should_continue, selection=selection, retest=retest)
            try:
                model_sync.apply_config_transaction(
                    {model_sync.STATE_FILE:
                     json.dumps(state, ensure_ascii=False, indent=2) + "\n"},
                    model_sync.BACKUP_DIR, keep_backups=30)
            except Exception as exc:
                # B3：状态事务保存失败不丢失/伪造证据——本轮真实计费结果
                # 原样写入恢复文件，主状态保持上一份成功证据；错误明确
                # 说明保存失败与恢复文件位置。
                recovery = _preserve_probe_evidence(
                    records, selection, outcome, exc)
                raise _operation_failure(
                    "probe_failed", "上下文探测失败" + recovery, exc) from exc
            model_sync.clear_probe_recovery()
    except OperationError:
        raise
    except Exception as exc:
        raise _operation_failure("probe_failed", "上下文探测失败", exc) from exc
    _report_probe_progress(ctx, outcome)
    result = _probe_result(outcome, selection, retest)
    result["operation_id"] = getattr(ctx, "op_id", None)
    return result


# ---------------------------------------------------------------------------
# Manual context overrides (Plan-Revision 7 step 6; contract recorded in
# model_sync's module docstring).  Set/clear write ONLY the model-sync state
# file under the existing SyncLock + transaction; the read face reports the
# declared/override/effective value honestly.  With the client synchronization
# retired there is no client write behind these actions: 覆盖保存≠客户端已应用.
# ---------------------------------------------------------------------------

_OVERRIDE_APPLY_NOTE = ("覆盖已保存到 model-sync 状态；客户端同步已退役，"
                        "覆盖保存≠客户端已应用")


def _override_identity_params(params):
    """Validate shared identity params; returns (source, model_id, expected)."""
    source = params.get("source")
    model_id = params.get("model_id")
    expected = params.get("expected_set_at")
    if not isinstance(source, str) or not source.strip():
        raise OperationError("invalid_params", "source 必须为非空字符串")
    if not isinstance(model_id, str) or not model_id.strip():
        raise OperationError("invalid_params", "model_id 必须为非空字符串")
    if expected is not None and (not isinstance(expected, str) or
                                 not expected.strip()):
        raise OperationError(
            "invalid_params", "expected_set_at 必须为读面返回的 override_at "
            "字符串或缺省（表示预期无既有覆盖）")
    return source.strip(), model_id.strip(), expected


def model_context_overrides(target, params, ctx=None):
    """人工 context 覆盖读面（只读）：声明、覆盖、有效值与来源。

    目录经既有只读 /v1/models 采集（与 set 动作的身份校验同一口径）；
    ``context_override_surface`` 覆盖当前目录模型与存量覆盖的并集，inert
    覆盖（模型已不在目录）如实显示为 effective_source="unknown"，绝不
    冒充生效。零写入。
    """
    if ctx is not None:
        ctx.raise_if_cancelled()
    params = params or {}
    if set(params) - {"local_only", "models"}:
        raise OperationError("invalid_params", "设置读面含未知参数")
    local_only = params.get("local_only", False)
    if not isinstance(local_only, bool):
        raise OperationError("invalid_params", "local_only 必须为布尔值")
    if "models" in params and not local_only:
        raise OperationError("invalid_params", "models 只用于本地设置读取")
    try:
        if local_only:
            # Identities only: the browser cannot supply declaration/evidence.
            models = params.get("models", {})
            if not isinstance(models, dict):
                raise ValueError("models 必须为来源到模型 id 列表的对象")
            scope = model_sync._normalize_scope({"models": models})
            catalogs = {
                source: [model_sync.CatalogModel(source, mid) for mid in ids]
                for source, ids in (scope.get("models", {}) if scope else {}).items()
            }
        else:
            catalogs, _ = model_sync.collect_catalogs()
    except ValueError as exc:
        raise OperationError("invalid_params", _single_line(exc)) from exc
    except Exception as exc:
        raise _operation_failure(
            "catalog_failed", "目录采集失败", exc) from exc
    state = model_sync.load_state()
    return {
        "overrides": model_sync.context_override_surface(state, catalogs),
        "note": ("只读读面：declared=目录声明、override=已保存覆盖、"
                 "effective=有效值；覆盖保存≠客户端已应用"),
    }


def model_context_override_set(target, params, ctx=None):
    """设置/更新一个来源|模型的人工 context 覆盖（仅写 model-sync 状态）。

    校验：context 正整数；模型身份必须存在于当前目录（只读采集确认；
    来源目录不可读与模型不存在分开如实拒绝）；expected_set_at 版本冲突
    规则——不匹配即 version_conflict，绝不静默覆盖并发修改。
    """
    if ctx is not None:
        ctx.raise_if_cancelled()
    params = params or {}
    source, model_id, expected = _override_identity_params(params)
    context = params.get("context")
    if isinstance(context, bool) or not isinstance(context, int) or \
            context < 1:
        raise OperationError("invalid_params", "context 必须为正整数")
    try:
        catalogs, _warnings = model_sync.collect_catalogs(sources=[source])
    except ValueError as exc:
        raise OperationError("invalid_params", _single_line(exc)) from exc
    except Exception as exc:
        raise _operation_failure(
            "catalog_failed", "目录采集失败", exc) from exc
    if source not in catalogs:
        # 已知启用来源采集失败（目录缺席）≠ 模型不存在：如实拒绝，不冒认。
        raise OperationError(
            "invalid_params",
            "来源 %s 当前目录不可用，无法确认模型身份" % source)
    try:
        with model_sync.SyncLock():
            entry = model_sync.set_context_override(
                catalogs, source, model_id, context, expected)
    except model_sync.ContextOverrideConflict as exc:
        raise OperationError("version_conflict", _single_line(exc)) from exc
    except ValueError as exc:
        raise OperationError("invalid_params", _single_line(exc)) from exc
    except Exception as exc:
        raise _operation_failure("override_failed", "覆盖保存失败", exc) from exc
    return {
        "source": source,
        "model_id": model_id,
        "context": entry["context"],
        "set_at": entry["set_at"],
        "note": _OVERRIDE_APPLY_NOTE,
    }


def model_context_override_clear(target, params, ctx=None):
    """恢复默认＝删除该来源|模型的人工 context 覆盖（不删除历史证据）。

    探测记录与 history 保留原样；expected_set_at 版本规则同 set。清除不
    存在的覆盖是幂等成功（existed=False），如实返回。
    """
    if ctx is not None:
        ctx.raise_if_cancelled()
    params = params or {}
    source, model_id, expected = _override_identity_params(params)
    try:
        with model_sync.SyncLock():
            outcome = model_sync.clear_context_override(
                source, model_id, expected)
    except model_sync.ContextOverrideConflict as exc:
        raise OperationError("version_conflict", _single_line(exc)) from exc
    except ValueError as exc:
        raise OperationError("invalid_params", _single_line(exc)) from exc
    except Exception as exc:
        raise _operation_failure("override_failed", "覆盖清除失败", exc) from exc
    return {
        "source": source,
        "model_id": model_id,
        "cleared": True,
        "existed": outcome["existed"],
        "changed": outcome.get("changed", outcome["existed"]),
        "note": (outcome.get("note") if outcome.get("changed")
                 else "该覆盖不存在（幂等成功，零写入）；目录声明值即为当前"
                      "有效值"),
    }
