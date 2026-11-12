# -*- coding: utf-8 -*-
"""④ 测试 Key 请求的**费用上限预估**（只读；不写盘、不发请求、不读任何账本）。

背景：``output/ai-gw-round6-20260927/metering-action-gate.md`` §④ 的第 3 件事实
需要一条真实分享请求，而 §⑥.3 / §4.3 要求维护者先写明四个声明值：**请求数 / 模型 /
费用上限 / 完成后入口状态**。本工具把「费用上限」算成一个可核对的数字，让维护者只需
批准一个值。

硬约束（本工具的全部承诺）：

1. **默认只读**：不读任何账本（分享账本与私人账本都不读）、不写任何文件、不发任何
   请求（HTTP/模型都没有）、不重启任何服务、不碰 ``ai-gateway/data/``。唯一的输入是
   价目表与产品自己的价目引擎；唯一的输出是 stdout。
2. **缺价即拒绝（退出码 2），绝不用 0 冒充**：模型不在价目表里、被标为 ``unpriced``、
   或价目表版本段无法证明当前价时，一律拒绝并给原因码；此时费用字段是 ``null``，
   **不是 0**。
3. **单价口径来自产品自己的解析链**：走 ``usagesrv.resolve_price()``（override →
   models 峰谷档 → providers flat 档），与账本计价同一条链；可达档位用产品自己的
   ``usagesrv.tier_for()`` 在窗口内逐小时采样收集——**不重写峰谷规则**。
4. **上限是上界不是点估**：逐请求上限 = 各可达档位**逐桶取最大单价**后相乘；打印值
   再向上取整到 ``USD_QUANTUM``，保证打印值 ≥ 精确上界。
5. **上限自检**：用 ``usagesrv.estimate_record_with_version()`` 在
   ``[now, now+horizon]`` 上逐小时采样，任一点超出上限即拒绝（绝不低报）。
6. **声明块只声明 ④**：四行声明只覆盖分享入口 8100 的 N 条白名单 POST；与 8096
   受控重启窗口**互不打包**（维护者 2026-09-27 要求各自独立执行）。
7. **完成后入口状态默认关闭**（维护者 2026-09-28 裁定）：声明值 4/4 默认
   「测试后关闭公网分享入口（8100 停止、Funnel 关闭）」，可用 ``--post-entry-state``
   显式覆盖；输出必须保留「该阈值是执行停止线，不是上游账单保证。」这句。
   该值是**终态**；关闭**次序**（先关 Funnel、8100 留到驱动运行后判据核对完成、再停 8100）
   见输出里的「[关闭次序]」行与 ``SHARING.md``——次序颠倒会让驱动判 FAIL。

**默认 token 口径的出处（必须写明，见 ``--help`` 与 request-budget.md）**：价目表只有
**单价**（USD/百万 token），**没有** token 数上限字段，所以默认 token 数不可能来自价目表；
默认值取自**将要发那条请求的驱动工具** ``metering_request_probe.py`` 的
``DEFAULT_PROMPT`` 字符数（1 字符 ≈ 1 token 的保守上界）与 ``DEFAULT_MAX_TOKENS``。
价目表贡献的是**单价档口径**：默认取可达档位里的最大单价（DeepSeek 峰谷档取 peak 档，
输入桶取 cacheMiss）——见 ``ceiling_rates``。

退出码：0 = 已算出上限（声明块已打印）；2 = 拒绝（缺模型/无价、参数不合法、
上限自检不过、价目引擎或驱动工具不可用）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from decimal import Decimal, ROUND_CEILING

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_UM_SHARE_DIR = os.path.dirname(TOOLS_DIR)
ACTIONS_DIR = os.path.join(_UM_SHARE_DIR, "actions")
if _UM_SHARE_DIR not in sys.path:
    sys.path.insert(0, _UM_SHARE_DIR)
from lib.paths import bootstrap  # noqa: E402

AI_GATEWAY_DIR = bootstrap(__file__)["repo"]
for _path in (AI_GATEWAY_DIR, TOOLS_DIR, ACTIONS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

EXIT_OK = 0
EXIT_REFUSED = 2

# 上限量化：向上取整到 1e-12 USD（1 皮美元）。打印值因此 ≥ 精确上界。
USD_QUANTUM = Decimal("0.000000000001")
# 浮点噪声容差（相对）：只吸收双精度舍入（约 1e-16 相对），远小于价目表里任何两档
# 单价的差（最小档差 0.007 USD/百万 token），所以它不会把「真的超上限」放过去。
FLOAT_TOLERANCE = Decimal("1e-9")
# 建议批准值的取整粒度：维护者批一个整数美分比批 2.288e-5 更好核对。
DEFAULT_APPROVAL_QUANTUM = Decimal("0.01")
# 上限覆盖窗口：14 天逐小时采样必然覆盖全部可达档位（峰谷只依赖 UTC 小时、是否
# 周末、与两个固定边界的大小关系）。窗口之外发出的请求不在本上限覆盖范围。
HORIZON_DAYS = 14
SAMPLE_STEP_MS = 3_600_000
DAY_MS = 86_400_000

# 完成后入口状态：默认「测试后关闭」（维护者 2026-09-28 裁定：测试后关闭入口；
# 动作门 §4.3 的第四项声明值）。允许用 --post-entry-state 显式覆盖，但默认必须是关闭。
POST_ENTRY_STATE_DEFAULT = "测试后关闭公网分享入口（8100 停止、Funnel 关闭）"
# 驱动工具 --post-entry-state 的等价取值：closed = 运行后 Funnel 必须为关。驱动工具自身的
# 默认仍是 unchanged，本工具不改它；用本工具的默认值时须显式传 --post-entry-state closed。
POST_ENTRY_STATE_PROBE_FLAG = "closed"
# 阈值口径（维护者 2026-09-28 要求输出保留这句）：上限是执行停止线，不是上游账单保证。
THRESHOLD_IS_STOP_LINE_NOTE = "该阈值是执行停止线，不是上游账单保证。"
# 关闭次序（代码推导，未实跑）：声明值 4/4 是**终态**，但驱动 metering_request_probe.py 的运行后门
# 判据要求「Funnel 关 + 8100 仍在听」，先停 8100 会让它 FAIL（:239-262 无论 declared 都要求运行后
# 两门仍答 404/401；连接被拒时 metering_acceptance.py:85-86 记 status=None）。故次序必须写明。
POST_ENTRY_STATE_ORDER_NOTE = (
    "关闭次序：①请求在 Funnel 开启时发出（否则那条请求不是公网请求）→ ②先关 Funnel → "
    "③8100 保持监听，让驱动运行后门判据核对完成（/ 与 /v1/models 仍答 404/401、funnel_on=False）"
    "→ ④再停 8100。终态 = 8100 停止 + Funnel 关闭；次序颠倒（先停 8100）会让驱动判 FAIL。")

# 拒绝原因码（四态不混同：缺价、显式无价、版本不可证、价目表不可读）。
REASON_NOT_IN_TABLE = "not_in_table"
REASON_EXPLICITLY_UNPRICED = "explicitly_unpriced"
REASON_VERSION_UNRESOLVED = "version_unresolved"
REASON_PRICE_TABLE_UNAVAILABLE = "price_table_unavailable"
REASON_ENGINE_UNAVAILABLE = "price_engine_unavailable"
REASON_PROBE_BASIS_UNAVAILABLE = "probe_basis_unavailable"
REASON_CEILING_VIOLATED = "ceiling_self_check_failed"
REASON_BAD_ARGUMENTS = "bad_arguments"


# --------------------------------------------------------------- 纯数值工具
def _dec(value):
    """float/str/int → Decimal（走 ``str()``，拿到价目表里写的那个十进制字面量）。"""
    return Decimal(str(value))


def ceil_to(value, quantum):
    """向上取整到 quantum（1e-12 USD）。打印值因此 ≥ 精确上界。"""
    return (value / quantum).to_integral_value(rounding=ROUND_CEILING) * quantum


def _usd_text(value):
    """USD 定长十进制文本（去掉多余的 0，不用科学计数）。"""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _canon(model_id):
    """归一化模型 id；引擎不可用时返回 None（只影响诊断精度，不改变拒绝）。"""
    try:
        import usagesrv
        return usagesrv.canon_model_id(model_id)
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------- 默认 token 口径
def default_token_basis():
    """默认 token 口径 ← ``metering_request_probe.py`` 的声明值（缺工具即拒绝）。

    返回 ``{prompt_tokens, completion_tokens, prompt_source, completion_source,
    source}``：这两个数就是驱动工具即将发那条请求的字符数与 max_tokens，因此上限
    描述的是**同一条请求**，不是另编一个假设。
    """
    try:
        import metering_request_probe as probe
    except Exception as exc:  # noqa: BLE001 — 缺口径即拒绝，不猜默认值
        return {"error": "probe_basis_unavailable:%s" % type(exc).__name__}
    return {
        "prompt_tokens": len(probe.DEFAULT_PROMPT),
        "completion_tokens": int(probe.DEFAULT_MAX_TOKENS),
        "prompt_source": "metering_request_probe.DEFAULT_PROMPT 字符数"
                         "（1 字符 ≈ 1 token 的保守上界）",
        "completion_source": "metering_request_probe.DEFAULT_MAX_TOKENS"
                             "（输出预算全算）",
        "source": "metering_request_probe",
        "note": "价目表只有单价、没有 token 数上限字段；token 口径来自驱动工具。",
    }


# ----------------------------------------------------------------- 价格解析
def _diagnose_missing(model, table):
    """缺价原因码：区分「表不可读」「版本不可证」「显式无价」「不在表里」。"""
    if not (table or {}).get("meta", {}).get("pricesFound"):
        return REASON_PRICE_TABLE_UNAVAILABLE
    if (table or {}).get("versioned") and not (table or {}).get("versions"):
        return REASON_VERSION_UNRESOLVED
    canon = _canon(model)
    override = (table or {}).get("override") or {}
    for key, raw in override.items():
        if key == model or (canon and canon == _canon(key)):
            if isinstance(raw, dict) and raw.get("unpriced") is True:
                return REASON_EXPLICITLY_UNPRICED
    for models, _mode, _source in (table or {}).get("tiers") or []:
        for key, raw in models.items():
            if key == model or (canon and canon == _canon(key)):
                if isinstance(raw, dict) and raw.get("unpriced") is True:
                    return REASON_EXPLICITLY_UNPRICED
    if (table or {}).get("versioned"):
        try:
            import usagesrv
            if usagesrv.version_at(table, int(time.time() * 1000)) is None:
                return REASON_VERSION_UNRESOLVED
        except Exception:  # noqa: BLE001
            return REASON_VERSION_UNRESOLVED
    return REASON_NOT_IN_TABLE


def resolve(model, now_ms):
    """解析单价：返回 ``{entry, matched, mode, source, version, peak_cfg, table,
    rate, file, override_file, reason_code}``；``entry=None`` 时 ``reason_code``
    说明为什么（不是 0）。"""
    out = {"entry": None, "matched": None, "mode": None, "source": None,
           "version": None, "peak_cfg": None, "table": None, "rate": None,
           "file": None, "override_file": None, "reason_code": None}
    try:
        import usagesrv
    except Exception:  # noqa: BLE001 — 引擎不可用即拒绝
        out["reason_code"] = REASON_ENGINE_UNAVAILABLE
        return out
    out["file"], out["override_file"] = usagesrv.PRICE_FILE, usagesrv.OVERRIDE_FILE
    table = usagesrv.load_price_table()
    out["table"] = table
    version = usagesrv.version_at(table, now_ms)
    out["peak_cfg"] = (version or table).get("peak")
    out["version"] = (version or {}).get("versionId")
    out["rate"] = (version or {}).get("exchangeRate") or table.get("exchangeRate")
    entry, mode, matched, source = usagesrv.resolve_price(model, now_ms)
    if entry is None:
        out["reason_code"] = _diagnose_missing(model, table)
        return out
    out.update({"entry": entry, "matched": matched, "mode": mode,
                "source": source})
    return out


# ------------------------------------------------------------------- 上限层
def _tier_rates(tier):
    """一档单价 → 四个桶的单价（USD/百万 token）。"""
    return {
        "cacheMiss": _dec(tier.get("cacheMiss") or 0),
        "cacheHit": _dec(tier.get("cacheHit") or 0),
        "output": _dec(tier.get("output") or 0),
        "reasoning": _dec(tier.get("reasoning") or 0),
    }


def reachable_tiers(entry, peak_cfg, now_ms, horizon_days=HORIZON_DAYS,
                    step_ms=SAMPLE_STEP_MS):
    """可达档位：用产品自己的 ``tier_for`` 在窗口内采样收集（不重写峰谷规则）。

    档位只依赖「UTC 小时、是否周末、与两个固定边界的大小关系」，14 天逐小时采样
    必然覆盖全部可达档；再补采 ``tier_for(entry, None, ...)`` 的基础档，保证
    「时刻无效」时落到的档也在集合里。返回 ``(tiers, samples)``。
    """
    import usagesrv
    tiers, samples = [], 0
    at, end = now_ms, now_ms + horizon_days * DAY_MS
    while at <= end:
        tiers.append(usagesrv.tier_for(entry, at, peak_cfg))
        samples += 1
        at += step_ms
    tiers.append(usagesrv.tier_for(entry, None, peak_cfg))
    return tiers, samples


def ceiling_rates(tiers):
    """可达档位逐桶最大单价（输入桶取 cacheMiss/cacheHit 的较大者）。

    缓存命中是 input 的子集；声明里不带缓存 token 时全部按 cacheMiss 计，已覆盖
    cacheHit 更大的极端表。reasoning 桶独立，取可达档最大。
    """
    if not tiers:
        return None
    return {
        "input": max(max(_tier_rates(t)["cacheMiss"], _tier_rates(t)["cacheHit"])
                     for t in tiers),
        "output": max(_tier_rates(t)["output"] for t in tiers),
        "reasoning": max(_tier_rates(t)["reasoning"] for t in tiers),
    }


def _distinct_tier_rates(tiers):
    """去重后的档位单价（保序）：338 个采样点通常只落到 2~3 个不同档。"""
    seen, out = set(), []
    for tier in tiers:
        rates = _tier_rates(tier)
        key = tuple(sorted((k, str(v)) for k, v in rates.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(rates)
    return out


def floor_rates(tiers):
    """可达档位逐桶最小单价：只用于打印「参考下界」，不是声明值。"""
    return {
        "input": min(min(_tier_rates(t)["cacheMiss"], _tier_rates(t)["cacheHit"])
                     for t in tiers),
        "output": min(_tier_rates(t)["output"] for t in tiers),
        "reasoning": min(_tier_rates(t)["reasoning"] for t in tiers),
    }


def _request_usd(tokens, rates, quantum=USD_QUANTUM):
    """一条请求的上限：各桶单价 × 声明 token 数，向上取整到 quantum。"""
    exact = (_dec(tokens["prompt"]) * rates["input"]
             + _dec(tokens["completion"]) * rates["output"]
             + _dec(tokens["reasoning"]) * rates["reasoning"]) / Decimal(1_000_000)
    return ceil_to(exact, quantum)


def ceiling_self_check(model, tokens, now_ms, horizon_days, total,
                      step_ms=SAMPLE_STEP_MS):
    """逐小时采样产品引擎的计价，核对「打印值 ≥ 每个采样点」（只读、纯计算）。

    采样是**证据**不是证明：解析上界由可达档位取最大给出；采样负责抓住「窗口内
    价格版本切换后单价变高」这种解析上界覆盖不到的情形。任一点超出容差即拒绝。
    """
    import usagesrv
    at, end = now_ms, now_ms + horizon_days * DAY_MS
    max_cost, unpriced, samples = Decimal(0), 0, 0
    while at <= end:
        cost, priced, _matched, _peak, _version = \
            usagesrv.estimate_record_with_version(
                model, at, tokens["prompt"], tokens["completion"], 0,
                tokens["reasoning"])
        samples += 1
        if not priced:
            unpriced += 1
        else:
            max_cost = max(max_cost, _dec(cost))
        at += step_ms
    tolerance = total * FLOAT_TOLERANCE
    violations = []
    if max_cost > total + tolerance:
        violations.append({
            "max_sampled_usd": _usd_text(ceil_to(max_cost, USD_QUANTUM)),
            "ceiling_usd": _usd_text(total)})
    return {"samples": samples, "step_ms": step_ms, "horizon_days": horizon_days,
            "max_sampled_usd": _usd_text(ceil_to(max_cost, USD_QUANTUM)),
            "tolerance_usd": _usd_text(ceil_to(tolerance, USD_QUANTUM)),
            "tolerance": "相对 1e-9（只吸收双精度噪声）",
            "unpriced_samples": unpriced, "violations": violations}


def estimate(model, requests, prompt_tokens, completion_tokens, reasoning_tokens,
              now_ms, horizon_days=HORIZON_DAYS,
              approval_quantum=DEFAULT_APPROVAL_QUANTUM):
    """纯函数：声明值 + 价目表 → 上限结果（字典进、字典出）。

    返回键：``ok``、``exit_code``、``refusals``、``reason_code``、``price``、
    ``tier_rates``、``tier_count``、``rates``、``tokens``、``requests``、
    ``per_request_usd``、``total_usd``、``approval_usd``、``min_reference_usd``、
    ``check``、``warnings``。
    """
    refusals, warnings = [], []
    out = {"ok": False, "exit_code": EXIT_REFUSED, "refusals": refusals,
           "reason_code": None, "price": None, "tier_rates": [], "tier_count": 0,
           "rates": None, "tokens": {"prompt": prompt_tokens,
                                    "completion": completion_tokens,
                                    "reasoning": reasoning_tokens},
           "requests": requests, "per_request_usd": None, "total_usd": None,
           "total_cny": None, "approval_usd": None, "min_reference_usd": None,
           "check": None, "warnings": warnings}
    if isinstance(requests, bool) or not isinstance(requests, int) or requests < 1:
        refusals.append("--requests 必须是 ≥1 的整数（0 条请求没有上限可批）")
    for label, value in (("--prompt-tokens", prompt_tokens),
                        ("--completion-tokens", completion_tokens),
                        ("--reasoning-tokens", reasoning_tokens)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            refusals.append("%s 必须是 ≥0 的整数" % label)
    if isinstance(horizon_days, bool) or not isinstance(horizon_days, int) \
            or horizon_days < 1:
        refusals.append("--horizon-days 必须是 ≥1 的整数")
    if not isinstance(model, str) or not model.strip():
        refusals.append("--model 必须是非空模型 id")
    if refusals:
        out["reason_code"] = REASON_BAD_ARGUMENTS
        return out

    resolved = resolve(model, now_ms)
    out["price"] = {
        "matched": resolved["matched"], "mode": resolved["mode"],
        "source": resolved["source"], "version": resolved["version"],
        "reason_code": resolved["reason_code"],
        "exchange_rate": resolved["rate"], "file": resolved["file"],
        "override_file": resolved["override_file"],
    }
    if resolved["entry"] is None:
        # 缺价一律拒绝：费用字段保持 None（绝不是 0），并给原因码。
        out["reason_code"] = resolved["reason_code"]
        refusals.append("价目表无法给 %r 定价（原因码 %s）：绝不用 0 冒充上限"
                        % (model, resolved["reason_code"]))
        return out

    tiers, samples = reachable_tiers(resolved["entry"], resolved["peak_cfg"],
                                    now_ms, horizon_days)
    rates = ceiling_rates(tiers)
    out["tier_rates"] = [{k: _usd_text(v) for k, v in rates.items()}
                         for rates in _distinct_tier_rates(tiers)]
    out["tier_count"] = len(out["tier_rates"])
    out["rates"] = {k: _usd_text(v) for k, v in rates.items()}
    out["tier_samples"] = samples
    if rates["reasoning"] > 0 and reasoning_tokens == 0:
        warnings.append(
            "价目表有非零 reasoning 单价（USD %s/百万 token），而声明 reasoning "
            "token = 0：上限不覆盖上游单独上报的 reasoning token"
            % _usd_text(rates["reasoning"]))
    if (resolved["table"] or {}).get("meta", {}).get("checkedStale") is True:
        warnings.append("价目表 checkedAt 已过期（页面会出黄条），单价可能不是最新")

    per_request = _request_usd(out["tokens"], rates)
    total = per_request * requests
    rate = _dec(resolved["rate"] or 0)
    out["per_request_usd"] = _usd_text(per_request)
    out["total_usd"] = _usd_text(total)
    out["total_cny"] = _usd_text(ceil_to(total * rate, USD_QUANTUM))
    out["approval_usd"] = _usd_text(ceil_to(total, approval_quantum))
    out["min_reference_usd"] = _usd_text(ceil_to(
        _request_usd(out["tokens"], floor_rates(tiers)) * requests, USD_QUANTUM))
    out["check"] = ceiling_self_check(model, out["tokens"], now_ms,
                                     horizon_days, total)
    if out["check"]["violations"]:
        out["reason_code"] = REASON_CEILING_VIOLATED
        refusals.append("上限自检不过：采样点最大 USD %s 超出上限 USD %s（容差 USD %s）"
                        % (out["check"]["max_sampled_usd"], out["total_usd"],
                           out["check"]["tolerance_usd"]))
        return out
    out["ok"] = True
    out["exit_code"] = EXIT_OK
    return out


# ------------------------------------------------------------------- 声明层
def declaration_lines(model, requests, total_usd, approval_usd, post_entry_state):
    """四行授权声明块：请求数 / 模型 / 费用上限 / 完成后入口状态。"""
    ceiling = ("USD %s（建议批准 USD %s）" % (total_usd, approval_usd)
               if total_usd is not None else "不可判定（缺价：不拿 0 顶替）")
    return [
        "[声明 1/4] 请求数：%d 条（驱动工具 --authorized-requests %d）"
        % (requests, requests),
        "[声明 2/4] 模型：%s" % model,
        "[声明 3/4] 费用上限：%s" % ceiling,
        "[声明 4/4] 完成后入口状态：%s" % post_entry_state,
    ]


def declaration_note():
    """声明块之后的说明行：④ 与 8096 窗口互不打包（维护者要求各自独立）。"""
    return ("[说明] 本声明只覆盖分享入口 8100 的请求数/模型/费用上限/入口状态；"
            "与 8096 受控重启窗口**互不打包**——8096 窗口（控制台宿主重启）"
            "按动作门 §③.1 另行单独授权、单独执行，两者不合并、不同一轮。")


def threshold_note_line():
    """阈值口径说明行：上限是执行停止线、不是上游账单保证（维护者 2026-09-28）。"""
    return "[阈值口径] %s" % THRESHOLD_IS_STOP_LINE_NOTE


def order_note_line():
    """关闭次序说明行：终态 vs 次序（先关 Funnel、8100 留到驱动核对完成）。"""
    return "[关闭次序] %s" % POST_ENTRY_STATE_ORDER_NOTE


# ------------------------------------------------------------------- 打印层
def _print_ok(result, post_entry_state, approval_quantum):
    price, rates, check = result["price"], result["rates"], result["check"]
    basis = result.get("token_basis_detail") or {}
    print("[上限预估] 模型：%s → 价目表命中 %s（档位口径 %s，来源 %s，版本 %s）"
          % (result["model"], price["matched"] or "无", price["mode"],
             price["source"], price["version"]))
    print("[上限预估] 单价口径：输入 USD %s/百万 token、输出 USD %s/百万 token、"
          "reasoning USD %s/百万 token（可达档位 %d 个逐桶取最大；输入桶取 "
          "cacheMiss/cacheHit 较大者）"
          % (rates["input"], rates["output"], rates["reasoning"],
             result["tier_count"]))
    print("[上限预估] token 口径：prompt %d、completion %d、reasoning %d；来源 %s"
          % (result["tokens"]["prompt"], result["tokens"]["completion"],
             result["tokens"]["reasoning"], result["token_basis"]))
    print("[上限预估] token 口径明细：prompt ← %s；completion ← %s"
          % (basis.get("prompt_source"), basis.get("completion_source")))
    print("[上限预估] 逐请求上限 USD %s；合计 %d 条上限 USD %s（≈ CNY %s @ %s）"
          % (result["per_request_usd"], result["requests"], result["total_usd"],
             result["total_cny"], price["exchange_rate"]))
    print("[上限预估] 建议批准值 USD %s（合计向上取整到 %s USD；参考下界 "
          "USD %s，仅参考、非声明值）"
          % (result["approval_usd"], _usd_text(approval_quantum),
             result["min_reference_usd"]))
    print("[上限预估] 上限自检：%d 天逐小时采样 %d 点，最大 USD %s，容差 %s；"
          "未定价采样 %d 点；越界 %d 点"
          % (check["horizon_days"], check["samples"], check["max_sampled_usd"],
             check["tolerance"], check["unpriced_samples"],
             len(check["violations"])))
    for line in result["warnings"]:
        print("[上限预估] 告警：%s" % line)
    for line in declaration_lines(result["model"], result["requests"],
                                  result["total_usd"], result["approval_usd"],
                                  post_entry_state):
        print(line)
    print(threshold_note_line())
    print(order_note_line())
    print(declaration_note())
    print("[边界] 只读：不读任何账本、不写盘、不发请求、不重启任何服务；价目表 %s"
          "（override %s）" % (price["file"], price["override_file"]))


def _print_refused(result, post_entry_state):
    print("[上限预估] 拒绝：上限不可判定——**绝不用 0 冒充**")
    print("[上限预估] 原因码：%s" % (result["reason_code"] or "unknown"))
    for item in result["refusals"]:
        print("[上限预估] 拒绝理由：%s" % item)
    print("[上限预估] 模型：%s；请求数：%s" % (result["model"], result["requests"]))
    print("[上限预估] 费用上限：不可判定（没有数字，不拿 0 顶替）")
    if result.get("price"):
        print("[上限预估] 价目表：%s（override %s；缺价原因码 %s）"
              % (result["price"]["file"], result["price"]["override_file"],
                 result["price"]["reason_code"]))
    for line in result["warnings"]:
        print("[上限预估] 告警：%s" % line)
    for line in declaration_lines(result["model"], result["requests"], None,
                                  None, post_entry_state):
        print(line)
    print(threshold_note_line())
    print(order_note_line())
    print(declaration_note())


# -------------------------------------------------------------------- 入口
def build_parser():
    parser = argparse.ArgumentParser(
        description="④ 测试 Key 请求的费用上限预估（只读；缺价即退出码 2）",
        epilog="默认 token 口径来自 metering_request_probe 的 DEFAULT_PROMPT 字符数与 "
               "DEFAULT_MAX_TOKENS（价目表只有单价、没有 token 上限）；用 --show-basis 打印。")
    parser.add_argument("--model", required=True, help="模型 id（声明值 2/4）")
    parser.add_argument("--requests", required=True, type=int,
                        help="请求数 N（声明值 1/4；必须 ≥1）")
    parser.add_argument("--prompt-tokens", type=int, default=None,
                        help="prompt token 数（默认见 --show-basis）")
    parser.add_argument("--completion-tokens", type=int, default=None,
                        help="completion token 数（默认见 --show-basis）")
    parser.add_argument("--reasoning-tokens", type=int, default=0,
                        help="reasoning token 数（默认 0；价目表有该桶单价时告警）")
    parser.add_argument("--post-entry-state", default=POST_ENTRY_STATE_DEFAULT,
                        help="完成后入口状态（声明值 4/4；默认 %r）"
                             % POST_ENTRY_STATE_DEFAULT)
    parser.add_argument("--horizon-days", type=int, default=HORIZON_DAYS,
                        help="上限覆盖窗口天数（默认 %d）" % HORIZON_DAYS)
    parser.add_argument("--json", action="store_true",
                        help="打印机器可读 JSON（只打 JSON）")
    parser.add_argument("--show-basis", action="store_true",
                        help="打印默认 token 口径的出处后退出（只读）")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    basis = default_token_basis()
    if args.show_basis:
        print(json.dumps(basis, ensure_ascii=False, indent=2))
        return EXIT_OK if "error" not in basis else EXIT_REFUSED
    if "error" in basis:
        result = _refusal_result(args.model, args.requests, basis,
                                 REASON_PROBE_BASIS_UNAVAILABLE,
                                 ["默认 token 口径不可得（%s）：不猜默认值"
                                  % basis["error"]])
        return _finish(result, args)
    prompt = basis["prompt_tokens"] if args.prompt_tokens is None \
        else args.prompt_tokens
    completion = basis["completion_tokens"] if args.completion_tokens is None \
        else args.completion_tokens
    result = {"model": args.model, "requests": args.requests,
              "token_basis": basis["source"], "token_basis_detail": basis}
    result.update(estimate(args.model, args.requests, prompt, completion,
                           args.reasoning_tokens, int(time.time() * 1000),
                           args.horizon_days))
    return _finish(result, args)


def _refusal_result(model, requests, basis, reason_code, reasons):
    """参数不合法等早期拒绝：字段与 ``estimate`` 的拒绝结果同形。"""
    out = estimate(model, 1, 0, 0, 0, int(time.time() * 1000)) if False else None
    del out  # 保留形状说明：早期拒绝不走引擎，直接给同形字段
    return {"ok": False, "exit_code": EXIT_REFUSED, "refusals": list(reasons),
            "reason_code": reason_code, "price": None, "tier_rates": [],
            "tier_count": 0, "rates": None,
            "tokens": {"prompt": None, "completion": None, "reasoning": None},
            "requests": requests, "per_request_usd": None, "total_usd": None,
            "total_cny": None, "approval_usd": None, "min_reference_usd": None,
            "check": None, "warnings": [], "model": model,
            "token_basis": basis.get("source", "unavailable"),
            "token_basis_detail": basis}


def build_json(result, post_entry_state, horizon_days, approval_quantum):
    price = result.get("price")
    return {
        "tool": "request_budget_estimate",
        "version": 1,
        "read_only": True,
        "exit_code": result["exit_code"],
        "model": result["model"],
        "requests": result["requests"],
        "price": None if price is None else {
            "matched": price["matched"], "mode": price["mode"],
            "source": price["source"], "version": price["version"],
            "reason_code": price["reason_code"], "file": price["file"],
            "override_file": price["override_file"],
            "exchange_rate": None if price["exchange_rate"] is None
                              else _usd_text(price["exchange_rate"]),
        },
        "ceiling_rates_usd_per_mtok": result["rates"],
        "ceiling_rates_unit": "USD/百万 token",
        "token_basis": result.get("token_basis"),
        "token_basis_detail": result.get("token_basis_detail"),
        "tokens": result["tokens"],
        "per_request_usd": result["per_request_usd"],
        "total_usd": result["total_usd"],
        "total_cny": result.get("total_cny"),
        "approval_suggested_usd": result["approval_usd"],
        "approval_quantum_usd": _usd_text(approval_quantum),
        "min_reference_usd": result.get("min_reference_usd"),
        "usd_quantum": _usd_text(USD_QUANTUM),
        "horizon_days": horizon_days,
        "tier_count": result.get("tier_count"),
        "tier_rates": result.get("tier_rates"),
        "ceiling_check": result.get("check"),
        "declaration": {
            "lines": declaration_lines(result["model"], result["requests"],
                                      result["total_usd"], result["approval_usd"],
                                      post_entry_state),
            "note": declaration_note(),
            "requests": result["requests"],
            "model": result["model"],
            "cost_ceiling_usd": result["total_usd"],
            "approval_suggested_usd": result["approval_usd"],
            "post_entry_state": post_entry_state,
            "post_entry_state_probe_flag": POST_ENTRY_STATE_PROBE_FLAG,
            "threshold_note": THRESHOLD_IS_STOP_LINE_NOTE,
            "post_entry_state_order_note": POST_ENTRY_STATE_ORDER_NOTE,
            "packaged_with_8096_window": False,
        },
        "refusals": result["refusals"],
        "warnings": result["warnings"],
    }


def _finish(result, args):
    if args.json:
        print(json.dumps(build_json(result, args.post_entry_state,
                                    args.horizon_days,
                                    DEFAULT_APPROVAL_QUANTUM),
                          ensure_ascii=False, indent=2))
    elif result["ok"]:
        _print_ok(result, args.post_entry_state, DEFAULT_APPROVAL_QUANTUM)
    else:
        _print_refused(result, args.post_entry_state)
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
