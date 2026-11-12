# -*- coding: utf-8 -*-
"""有界真实分享请求驱动（动作门 §④ 的执行工具；默认 dry-run）。

背景：``output/ai-gw-round6-20260927/metering-action-gate.md`` §④ 的第 3 件
事实（8110 账本出现 ``attribution=verified``）**需要一条真实分享请求**，而 §⑥.3
要求维护者先写明「请求数 / 模型 / 费用上限 / 完成后入口状态」。本工具把那条请求
做成一条可核对的命令：**先声明上限，再发请求，最后按三件事实判定**。

硬约束（本工具的全部承诺）：

1. **默认 dry-run**：不加 ``--run`` 只打印计划，**不发任何请求**、不读任何账本、
   不解析任何路径（纯打印）。
2. **缺任一项即拒绝启动（退出码 2）**：真正发请求必须同时给
   ``--run --authorized-requests N --max-cost-usd X --model <id>``，
   外加 ``--credential-file <path>``（凭据只从文件读）。
3. **必须先打印四件事再发请求**：请求数、模型、费用上限、完成后入口状态
   （``--post-entry-state``，默认 ``unchanged``）在任何网络动作之前打印。
4. **只打分享入口 8100 的白名单路由**（``share_entry.py:21-22`` 的 ``ALLOWED``：
   ``POST /v1/chat/completions``、``POST /v1/messages``），并且先核对两种门：
   白名单**外**的 ``GET /`` → 404 ``route_not_shared``，白名单**内**的
   ``GET /v1/models``（未带凭据）→ 401 ``invalid_api_key``
   （``share_entry.py:922-931``）。门不符 → 拒发。
5. **凭据只从 ``--credential-file`` 读**：绝不打印内容（只打印路径与 strip 后
   长度）、绝不放进 argv。
6. **请求数不得超过 ``--authorized-requests``**：循环上界就是它，超出即停；
   任何 429/401/403 立即停止并如实报告；其余非 2xx 与传输错误也停（不重试、
   不补发）。
7. **费用先证明 ≤ 上限**：用运行中产品的价目引擎
   （``usagesrv.estimate_record_with_version``）按当前时刻估一条请求的费用；
   无价（``priced=False``）或超上限 → 拒发（绝不拿未知当 0）。
8. **逐条记录**：状态码、时长、是否出现归属（读 8110 账本新增行，只给
   ``time/model/key_id/attribution`` 字段）；结束后自动跑
   ``metering_acceptance`` 的三事实验收并打印结论。

只读/委托边界（与 ``metering_acceptance`` 同一口径；2026-09-28 执行轮 10 的 L1
收紧后同步）：本工具对 8096/8100 只发不带凭据的 ``GET http://127.0.0.1``；唯一带凭据
的请求是发给 8100 的那 N 条白名单 POST（维护者授权的有界请求）；账本只读打开；
结束后自动跑的三事实验收（:533-537）走 ``metering_acceptance.collect_facts()``，它经
``metering_acceptance.share_ledger_path``（:546）→
``console_share_production.production_backend_observe(forget_stale=False)`` 解析 8110
账本路径——**陈旧 ``backend.pid`` 的 ``os.remove`` 已被关掉**（本条此前写「带两条已核对
的委托副作用」，现已不再成立，如实更正）。剩下的委托只读观察只有只读
``GET /admin/auth/status``（只读 GET、非模型请求、不打印令牌内容）。

退出码：0 = dry-run 计划已打印，或真实运行完成且三事实验收通过；2 = 拒绝
（缺参数、门不符、凭据不可读、费用超上限/无价、账本路径不可解析）或运行后验收
未通过。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_UM_SHARE_DIR = os.path.dirname(TOOLS_DIR)
if _UM_SHARE_DIR not in sys.path:
    sys.path.insert(0, _UM_SHARE_DIR)
from lib.paths import bootstrap  # noqa: E402

AI_GATEWAY_DIR = bootstrap(__file__)["repo"]
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import metering_acceptance  # noqa: E402 — 同目录工具，复用三事实验收与只读 GET

EXIT_OK = 0
EXIT_REFUSED = 2

GATEWAY_PORT = 8100           # 分享入口（share_entry.py）
CONSOLE_PORT = 8096           # 控制台网页宿主（usagesrv.py）
CHAT_PATH = "/v1/chat/completions"
MESSAGES_PATH = "/v1/messages"
# 白名单内的两条 POST 路由（share_entry.py:21-22 的 ALLOWED 子集）。
ROUTES = {"chat": ("POST", CHAT_PATH), "messages": ("POST", MESSAGES_PATH)}
ENTRY_STATE_CHOICES = ("unchanged", "open", "closed")
STOP_STATUSES = (429, 401, 403)
MIN_CREDENTIAL_CHARS = 8       # 只用来抓空文件/纯空白；长度照打印
MAX_RESPONSE_BYTES = 65536      # 响应只读这么多字节（有界）
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_MAX_TOKENS = 16
DEFAULT_PROMPT = "ping"
TAIL_ROWS = 5
ATTRIBUTION_VERIFIED = metering_acceptance.ATTRIBUTION_VERIFIED
LEDGER_ROW_FIELDS = metering_acceptance.LEDGER_ROW_FIELDS

# 运行前必须成立的两道门（未带凭据的只读 GET）：
ENTRY_GATES = (
    ("root", "/", 404, "route_not_shared"),
    ("whitelist", "/v1/models", 401, "invalid_api_key"),
)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


# ------------------------------------------------------------------ 声明层
def start_gate(args):
    """纯函数：CLI 参数 → ``{run, exit_code, missing, refusals}``。

    ``--run`` 缺 ``--authorized-requests``/``--max-cost-usd``/``--model``/
    ``--credential-file`` 任一 → 拒绝（退出码 2）。未给 ``--run`` 时给出的值也
    照校验：不合法的值不在计划里假装合法。
    """
    missing, refusals = [], []
    count = getattr(args, "authorized_requests", None)
    cap = getattr(args, "max_cost_usd", None)
    model = getattr(args, "model", None)
    credential_file = getattr(args, "credential_file", None)
    if count is not None and (isinstance(count, bool)
                              or not isinstance(count, int) or count < 1):
        refusals.append("--authorized-requests 必须是 ≥1 的整数（请求数上限）")
    if cap is not None and (not _is_number(cap) or cap <= 0):
        refusals.append("--max-cost-usd 必须是 >0 的有限数值（费用上限）")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        refusals.append("--model 必须是非空模型 id")
    if not getattr(args, "run", False):
        return {"run": False, "exit_code": EXIT_OK if not refusals
                 else EXIT_REFUSED, "missing": [], "refusals": refusals}
    if count is None:
        missing.append("--authorized-requests")
    if cap is None:
        missing.append("--max-cost-usd")
    if model is None:
        missing.append("--model")
    if credential_file is None:
        missing.append("--credential-file")
    return {"run": True,
            "exit_code": EXIT_REFUSED if (missing or refusals) else EXIT_OK,
            "missing": missing, "refusals": refusals}


# ------------------------------------------------------------------ 计划层
def request_body(args, _route):
    """两条白名单路由共用的最小请求体（字段都在各自的 whitelist 内）。

    ``model`` 保持上游真实 id（分享不发明虚拟别名）；``stream=False`` 让响应
    一次给全，便于有界读取与逐条计时。
    """
    return {"model": getattr(args, "model", None) or "<未声明>",
            "messages": [{"role": "user",
                          "content": getattr(args, "prompt", None)
                                     or DEFAULT_PROMPT}],
            "max_tokens": getattr(args, "max_tokens", DEFAULT_MAX_TOKENS),
            "stream": False}


def build_plan(args, ledger_path=None, ledger_error=None, cost=None,
               credential_chars=None):
    """纯函数：CLI 参数 → 计划（四件事 + 路由 + 停止规则 + 费用估算）。"""
    route = getattr(args, "route", "chat")
    body = request_body(args, route)
    return {
        "run": bool(getattr(args, "run", False)),
        "request_count": getattr(args, "authorized_requests", None),
        "model": getattr(args, "model", None),
        "max_cost_usd": getattr(args, "max_cost_usd", None),
        "post_entry_state": getattr(args, "post_entry_state", "unchanged"),
        "method": ROUTES[route][0],
        "path": ROUTES[route][1],
        "port": GATEWAY_PORT,
        "body": body,
        "prompt_chars": len(body["messages"][0]["content"]),
        "max_tokens": body["max_tokens"],
        "credential_file": getattr(args, "credential_file", None),
        "credential_chars": credential_chars,
        "timeout_s": getattr(args, "timeout", DEFAULT_TIMEOUT_S),
        "ledger_path": ledger_path,
        "ledger_error": ledger_error,
        "cost": cost,
        "entry_gates": ENTRY_GATES,
        "stop_rules": [
            "最多发 --authorized-requests 条，绝不超出",
            "任一 429/401/403 立即停止并如实报告",
            "其余非 2xx 与传输错误也停止（不重试、不补发）",
            "结束前不改任何服务状态；--post-entry-state 只声明核对口径",
        ],
    }


def estimate_cost(model, prompt_chars, max_tokens):
    """用运行中产品的价目引擎估一条请求的费用（只读；不打印任何秘密）。"""
    out = {"priced": False, "cost_usd": None, "matched": None,
           "peak": None, "version_id": None, "prompt_tokens": None,
           "error": None}
    if not isinstance(model, str) or not model.strip():
        out["error"] = "model_missing"
        return out
    # prompt token 的保守上界：1 字符 ≈ 1 token（对 ASCII 与 CJK 都是上界，
    # 真实 token 数不会超过字符数）；输出 token 用 max_tokens 全算。
    prompt_tokens = int(prompt_chars or 0)
    out["prompt_tokens"] = prompt_tokens
    try:
        import usagesrv
        now_ms = int(time.time() * 1000)
        cost, priced, matched, peak, version_id = \
            usagesrv.estimate_record_with_version(
                model, now_ms, prompt_tokens, int(max_tokens or 0), 0, 0)
    except Exception as exc:  # noqa: BLE001 — 估不出价是证据，不是崩溃
        out["error"] = "price_estimate_failed:%s" % type(exc).__name__
        return out
    out.update({"priced": bool(priced), "cost_usd": float(cost),
                "matched": matched, "peak": bool(peak),
                "version_id": version_id})
    return out


# ---------------------------------------------------------------- 只读观测层
def entry_gate_facts(get):
    """运行前/后的两门 + funnel：``{gates, funnel_on, error}``（只读 GET）。"""
    out = {"gates": {}, "funnel_on": None, "error": None}
    for name, path, _status, _code in ENTRY_GATES:
        response = get(GATEWAY_PORT, path)
        code = None
        payload = response.get("json") if isinstance(response, dict) else None
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict) \
                and isinstance(payload["error"].get("code"), str):
            code = payload["error"]["code"]
        out["gates"][name] = {"status": response.get("status")
                              if isinstance(response, dict) else None,
                              "code": code,
                              "error": response.get("error")
                              if isinstance(response, dict) else "probe_failed"}
    status = get(CONSOLE_PORT, "/api/console/share-management/status")
    payload = status.get("json") if isinstance(status, dict) else None
    if isinstance(payload, dict) and isinstance(payload.get("funnel_on"), bool):
        out["funnel_on"] = payload["funnel_on"]
    elif isinstance(status, dict) and status.get("error"):
        out["error"] = "console_status_unreadable:%s" % status.get("error")
    return out


def gate_check(before, after, declared):
    """纯函数：入口状态核对（两门 + funnel；读不到 = 不通过）。"""
    items = []
    for name, _path, status, code in ENTRY_GATES:
        for label, facts in (("运行前", before), ("运行后", after)):
            gate = (facts.get("gates") or {}).get(name) or {}
            ok = gate.get("status") == status and gate.get("code") == code
            items.append({"id": "gate_%s_%s" % (name, label),
                          "ok": bool(ok),
                          "detail": "GET %s → %r/%r（期望 %d %s）"
                                    % (_path, gate.get("status"),
                                       gate.get("code"), status, code)})
    before_funnel = before.get("funnel_on")
    after_funnel = after.get("funnel_on")
    if declared == "unchanged":
        ok = before_funnel is not None and before_funnel == after_funnel
        detail = "funnel_on 运行前=%r 运行后=%r（声明 unchanged，必须一致）" \
            % (before_funnel, after_funnel)
    elif declared == "open":
        ok = after_funnel is True
        detail = "funnel_on 运行后=%r（声明 open，必须为 True）" % (after_funnel,)
    else:
        ok = after_funnel is False
        detail = "funnel_on 运行后=%r（声明 closed，必须为 False）" % (after_funnel,)
    items.append({"id": "funnel_after", "ok": bool(ok), "detail": detail})
    if before.get("error") or after.get("error"):
        items.append({"id": "entry_read", "ok": False,
                      "detail": "入口读面错误：运行前=%r 运行后=%r"
                                % (before.get("error"), after.get("error"))})
    return {"ok": all(item["ok"] for item in items), "items": items}


def new_rows_between(before, after):
    """纯函数：两次账本快照 → 新增行的字段（有界；读不到不当 0）。"""
    out = {"rows": None, "new": [], "note": None, "error": None}
    for facts in (before, after):
        if not isinstance(facts, dict) or facts.get("error"):
            out["error"] = (facts or {}).get("error") or "ledger_snapshot_failed"
            return out
    added = after.get("rows", 0) - before.get("rows", 0)
    if added < 0:
        out["error"] = "ledger_shrank"
        return out
    out["rows"] = added
    tail = after.get("tail") or []
    if added:
        out["new"] = tail[-added:]
    if added > len(tail):
        out["note"] = "新增 %d 行，末 %d 行之外的字段不在本记录里" \
            % (added, len(tail))
    return out


def scan_share_ledger(path, tail_count=TAIL_ROWS):
    """只读扫描 8110 账本：``{rows, verified, tail, error}``（只留约定字段）。"""
    out = {"path": path, "rows": 0, "verified": 0, "tail": [], "error": None}
    if not path:
        out["error"] = "ledger_path_unresolved"
        return out
    rows = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                out["rows"] += 1
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("attribution") == ATTRIBUTION_VERIFIED:
                    out["verified"] += 1
                rows.append({field: record.get(field)
                             for field in LEDGER_ROW_FIELDS})
    except OSError as exc:
        out["error"] = "ledger_unreadable:%s" % type(exc).__name__
        return out
    out["tail"] = rows[-tail_count:]
    return out


def read_credential(path):
    """凭据只从文件读：``(secret, error)``；只打印长度，绝不打印内容。"""
    if not path:
        return None, "credential_file_missing"
    try:
        secret = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError, ValueError) as exc:
        return None, "credential_unreadable:%s" % type(exc).__name__
    if len(secret) < MIN_CREDENTIAL_CHARS:
        return None, "credential_too_short"
    return secret, None


# ------------------------------------------------------------------ 请求层
def summarize_response(status, raw):
    """纯函数：响应体（有界）→ 只含状态/用量/错误码的摘要，绝不回显正文。"""
    out = {"bytes": len(raw or b""), "parsed": False, "model": None,
           "usage": None, "choices": None, "error_code": None,
           "error_type": None}
    try:
        payload = json.loads((raw or b"").decode("utf-8"))
    except (UnicodeError, ValueError):
        return out
    out["parsed"] = True
    if not isinstance(payload, dict):
        return out
    if isinstance(payload.get("model"), str):
        out["model"] = payload["model"]
    usage = payload.get("usage")
    if isinstance(usage, dict):
        out["usage"] = {key: usage.get(key) for key in
                        ("prompt_tokens", "completion_tokens", "total_tokens")}
    choices = payload.get("choices")
    if isinstance(choices, list):
        out["choices"] = len(choices)
    error = payload.get("error")
    if isinstance(error, dict):
        if isinstance(error.get("code"), str):
            out["error_code"] = error["code"]
        if isinstance(error.get("type"), str):
            out["error_type"] = error["type"]
    return out


def send_request(plan, credential, timeout=None):
    """发一条白名单 POST（凭据只进请求头，绝不打印、绝不进 argv）。"""
    url = "http://127.0.0.1:%d%s" % (plan["port"], plan["path"])
    payload = json.dumps(plan["body"], ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json",
                  "Authorization": "Bearer " + credential,
                  "Content-Length": str(len(payload))})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(
                request, timeout=timeout or plan["timeout_s"]) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES)
            status = resp.status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(MAX_RESPONSE_BYTES)
        except Exception:  # noqa: BLE001 — 读 body 失败也是证据
            raw = b""
        status = exc.code
    except Exception as exc:  # noqa: BLE001 — 传输失败如实记错误
        return {"status": None, "duration_s": round(time.monotonic() - started, 3),
                "error": type(exc).__name__, "summary": None}
    return {"status": status,
            "duration_s": round(time.monotonic() - started, 3),
            "error": None, "summary": summarize_response(status, raw)}


# ------------------------------------------------------------------ 编排层
def format_plan(plan):
    """四件事 + 路由 + 停止规则；在**任何**网络动作之前打印。"""
    cost = plan.get("cost") or {}
    if cost.get("priced"):
        cost_text = ("priced=True cost=%.6f USD（≤ 上限 %s）version=%s peak=%s "
                     "matched=%s（prompt_tokens 上界=%s）"
                     % (cost["cost_usd"], plan["max_cost_usd"],
                        cost.get("version_id"), cost.get("peak"),
                        cost.get("matched"), cost.get("prompt_tokens")))
    elif cost:
        cost_text = "priced=False（原因=%s）—— 拒发，不拿未知当 0" \
            % (cost.get("error") or "价格未知")
    else:
        cost_text = ("在门/凭据之后、发请求之前用运行中产品的价目引擎估算"
                     "（真实运行必须先证明 ≤ 上限）")
    lines = ["== 有界真实请求计划（动作门 §④） ==",
             "[计划] 请求数（--authorized-requests）：%s"
             % (plan["request_count"] if plan["request_count"] is not None
                else "未声明（真实运行必须给）"),
             "[计划] 模型（--model）：%s"
             % (plan["model"] if plan["model"] else "未声明（真实运行必须给）"),
             "[计划] 费用上限（--max-cost-usd）：%s"
             % (plan["max_cost_usd"] if plan["max_cost_usd"] is not None
                else "未声明（真实运行必须给）"),
             "[计划] 完成后入口状态（--post-entry-state）：%s"
             % plan["post_entry_state"],
             "[计划] 路由：%s http://127.0.0.1:%d%s（白名单内）"
             % (plan["method"], plan["port"], plan["path"]),
             "[计划] 请求体：%s；prompt %d 字符；max_tokens=%s；stream=False"
             % (json.dumps(plan["body"]["messages"][0], ensure_ascii=False),
                plan["prompt_chars"], plan["max_tokens"]),
             "[计划] 凭据：只从 --credential-file 读（%s；%s 字符）——"
             "绝不打印、绝不进 argv"
             % (plan["credential_file"] or "未声明",
                plan["credential_chars"] if plan["credential_chars"] is not None
                else "未读"),
             "[计划] 费用估算：%s" % cost_text,
             "[计划] 运行前门（未带凭据的只读 GET）：%s"
             % "；".join("GET %s → %d %s" % (path, status, code)
                         for _name, path, status, code in ENTRY_GATES),
             "[计划] 停止规则：%s" % "；".join(plan["stop_rules"]),
             "[计划] 账本（8110，只读）：%s%s"
             % (plan["ledger_path"] or "运行前解析",
                "" if not plan["ledger_error"]
                else "（错误=%s）" % plan["ledger_error"]),
             "[计划] 结束后：自动跑 metering_acceptance 三事实验收并打印结论"]
    return "\n".join(lines)


def format_request_record(index, record):
    ledger = record.get("ledger") or {}
    new_rows = ledger.get("new")
    if isinstance(new_rows, list) and new_rows:
        attribution = "出现（新增 %d 行：%s%s）" % (
            ledger.get("rows"), json.dumps(new_rows, ensure_ascii=False),
            "" if not ledger.get("note") else "；%s" % ledger["note"])
    elif ledger.get("error"):
        attribution = "读不到（%s）" % ledger["error"]
    else:
        attribution = "未观察到（新增 0 行）"
    summary = record.get("summary") or {}
    return ("#%d status=%s duration_s=%s attribution=%s usage=%s model=%s "
            "error=%s"
            % (index, record.get("status"), record.get("duration_s"),
               attribution, json.dumps(summary.get("usage"),
                                      ensure_ascii=False),
               summary.get("model"),
               summary.get("error_code") or record.get("error")))


def run_probe(plan, credential, deps):
    """按声明的上限逐条发请求；任一门不成立即停（不重试、不补发）。"""
    records = []
    stop = {"stopped": False, "reason": None}
    for index in range(1, int(plan["request_count"]) + 1):
        before = deps["ledger_snapshot"](plan["ledger_path"])
        response = deps["send"](plan, credential)
        after = deps["ledger_snapshot"](plan["ledger_path"])
        record = dict(response)
        record["ledger"] = new_rows_between(before, after)
        records.append(record)
        status = record.get("status")
        if status in STOP_STATUSES:
            stop = {"stopped": True,
                     "reason": "状态码 %s 是停止条件（%s）"
                               % (status, ",".join(str(s)
                                                  for s in STOP_STATUSES))}
        elif status is None or not 200 <= int(status) < 300:
            stop = {"stopped": True,
                    "reason": "非 2xx（status=%s）：停，不重试不补发" % status}
        if stop["stopped"]:
            break
    return {"records": records, "stop": stop}


def run_acceptance(deps):
    """结束后自动跑三事实验收（metering_acceptance，只读）。"""
    try:
        facts, result, text = deps["acceptance"]()
    except Exception as exc:  # noqa: BLE001 — 采集失败如实报非零
        return {"ok": False, "text": "三事实验收采集失败：%s: %s"
                % (type(exc).__name__, exc), "facts": None, "result": None}
    return {"ok": bool(result.get("ok")), "text": text, "facts": facts,
            "result": result}


# ------------------------------------------------------------------- CLI 层
def build_parser():
    parser = argparse.ArgumentParser(
        description="有界真实分享请求驱动（默认 dry-run；--run 才发请求）")
    parser.add_argument("--run", action="store_true",
                        help="真正发请求（须同时给 --authorized-requests/"
                             "--max-cost-usd/--model/--credential-file）")
    parser.add_argument("--authorized-requests", type=int, default=None,
                        help="维护者授权的请求数上限（本工具绝不超出）")
    parser.add_argument("--max-cost-usd", type=float, default=None,
                        help="维护者授权的费用上限（USD；估算超上限即拒发）")
    parser.add_argument("--model", default=None,
                        help="上游真实模型 id（分享不发明虚拟别名）")
    parser.add_argument("--credential-file", default=None,
                        help="专用测试 Key 的明文文件（只读；绝不打印内容）")
    parser.add_argument("--route", choices=tuple(sorted(ROUTES)), default="chat",
                        help="白名单路由：chat=POST /v1/chat/completions；"
                             "messages=POST /v1/messages")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT,
                        help="请求里的用户内容（默认 %r，保持最小）"
                             % DEFAULT_PROMPT)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                        help="max_tokens（默认 %d；上限请求的输出预算）"
                             % DEFAULT_MAX_TOKENS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                        help="单条请求的超时秒数（默认 %s）"
                             % DEFAULT_TIMEOUT_S)
    parser.add_argument("--post-entry-state", choices=ENTRY_STATE_CHOICES,
                        default="unchanged",
                        help="完成后入口状态：unchanged（默认）/open/closed")
    return parser


def collect_acceptance():
    """结束后自动跑三事实验收（metering_acceptance，只读）。"""
    facts = metering_acceptance.collect_facts()
    result = metering_acceptance.evaluate_acceptance(facts)
    return facts, result, metering_acceptance.format_acceptance(facts, result)


def default_deps():
    """真实依赖（网络 + 账本 + 验收）；dry-run 一个都不会用到。"""
    return {
        "get": metering_acceptance.http_get_json,
        "send": send_request,
        "ledger_snapshot": scan_share_ledger,
        "resolve_ledger_path": metering_acceptance.share_ledger_path,
        "read_credential": read_credential,
        "estimate_cost": estimate_cost,
        "acceptance": collect_acceptance,
    }


def main(argv=None, deps=None):
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_REFUSED
    deps = deps or default_deps()

    gate = start_gate(args)
    if gate["exit_code"] != EXIT_OK:
        print("[拒绝] 不发任何请求：", file=sys.stderr)
        for name in gate["missing"]:
            print("  - --run 必须同时给 %s" % name, file=sys.stderr)
        for reason in gate["refusals"]:
            print("  - %s" % reason, file=sys.stderr)
        return EXIT_REFUSED

    # 计划先打印（纯打印，不碰网络、不读账本）；dry-run 到此为止。
    plan = build_plan(args)
    print(format_plan(plan))
    if not plan["run"]:
        print("（dry-run：本工具没有发任何请求、没有读任何账本；"
              "加 --run 并给全四件事才发）")
        return EXIT_OK

    # 真实运行：先门、再凭据、再费用上限，任一不成立即拒发。
    before = entry_gate_facts(deps["get"])
    print("[运行前] 入口两门 + funnel：%s"
          % json.dumps(before, ensure_ascii=False))
    for name, _path, status, code in ENTRY_GATES:
        gate_facts = (before.get("gates") or {}).get(name) or {}
        if gate_facts.get("status") != status or gate_facts.get("code") != code:
            print("[拒绝] 入口门不符（期望 %d %s）：%s —— 不发请求"
                  % (status, code, json.dumps(gate_facts, ensure_ascii=False)),
                  file=sys.stderr)
            return EXIT_REFUSED

    ledger_path, ledger_error = deps["resolve_ledger_path"]()
    if ledger_error:
        print("[拒绝] 8110 账本路径不可解析（%s）：无法观察归属，不发请求"
              % ledger_error, file=sys.stderr)
        return EXIT_REFUSED
    plan["ledger_path"], plan["ledger_error"] = ledger_path, ledger_error

    credential, credential_error = deps["read_credential"](
        plan["credential_file"])
    if credential_error:
        print("[拒绝] 凭据不可用（%s）：只从 --credential-file 读，不发请求"
              % credential_error, file=sys.stderr)
        return EXIT_REFUSED
    plan["credential_chars"] = len(credential)

    cost = deps["estimate_cost"](plan["model"], plan["prompt_chars"],
                                plan["max_tokens"])
    plan["cost"] = cost
    if not cost.get("priced"):
        print("[拒绝] 费用无法证明 ≤ 上限（%s）：不拿未知当 0，不发请求"
              % (cost.get("error") or "价格未知"), file=sys.stderr)
        return EXIT_REFUSED
    if cost["cost_usd"] > plan["max_cost_usd"]:
        print("[拒绝] 估算 %.6f USD 超过上限 %.6f：不发请求"
              % (cost["cost_usd"], plan["max_cost_usd"]), file=sys.stderr)
        return EXIT_REFUSED
    print("[运行] 费用估算 %.6f USD ≤ 上限 %.6f；请求数 %d；凭据 %d 字符"
          % (cost["cost_usd"], plan["max_cost_usd"], plan["request_count"],
             plan["credential_chars"]))

    outcome = run_probe(plan, credential, deps)
    print("== 逐条记录 ==")
    for index, record in enumerate(outcome["records"], 1):
        print(format_request_record(index, record))
    if outcome["stop"]["stopped"]:
        print("[停止] %s" % outcome["stop"]["reason"])

    after = entry_gate_facts(deps["get"])
    check = gate_check(before, after, plan["post_entry_state"])
    print("== 完成后入口状态核对（声明 %s） ==" % plan["post_entry_state"])
    for item in check["items"]:
        print("  [%s] %s —— %s" % ("PASS" if item["ok"] else "FAIL",
                                     item["id"], item["detail"]))

    acceptance = run_acceptance(deps)
    print(acceptance["text"])

    statuses = [record.get("status") for record in outcome["records"]]
    all_ok = (bool(statuses)
              and all(isinstance(s, int) and 200 <= s < 300 for s in statuses))
    ok = all_ok and check["ok"] and acceptance["ok"] \
        and not outcome["stop"]["stopped"]
    print("== 结论：%s ==" % ("通过（退出码 0）" if ok else "未通过（退出码 2）"))
    return EXIT_OK if ok else EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
