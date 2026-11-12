# -*- coding: utf-8 -*-
"""分享 Key 计量「三事实验收」只读打印（动作门 §④）。

三件事实缺一不可，**不得**用任一单字段宣布接线成功：

1. **文件就绪** —— ``file_ready`` / ``file_reason`` / ``key_chars``
   （密钥文件此刻可读且长度合格；只给长度，绝不给内容）；
2. **8100 实例已载入** —— ``instance_loaded``（``value`` / ``basis`` /
   ``reason``；这是「8100 启动时间 ≥ 密钥 mtime」的**推断**，查不到进程即
   ``unknown``，绝不默认 ``yes``）；
3. **请求归因被观察到** —— ``request_attribution_observed`` 与 8110 账本尾部
   的 ``time/model/key_id/attribution``（运行期事实 = 某行 ``attribution="verified"``）。

两个旁证：控制台 ``GET /api/console/share-usage`` 的 ``requests`` / ``keys[]`` /
``unattributed``，以及私人总账 ``key_id`` 非空行数（门内不变式 3：**必须为 0**）。

只读边界（2026-09-28 复审执行轮 9 后收紧：采集路径**真正零写副作用**）：

* 本工具自身：只发不带凭据的 ``GET http://127.0.0.1:8096``，本机文件只读打开；
  **本工具自己不写任何文件**；
* 委托的只读观察一律走**只读开关**，**不删任何文件、不写任何文件**：
  ``production_backend_observe(forget_stale=False)``（取 8110 账本路径与
  ``running``/``auth_state``）把 ``verify_running()`` 里唯一那次 ``_forget()``
  （``os.remove`` 陈旧 ``backend.pid``）关掉——此前它会在 PID 不匹配／进程已不在时
  删掉那份陈旧 pid 文件，现在陈旧文件照实报出并原样留在盘上；
  ``production_backend_pid_state()`` 全分支无写、无删，如实报
  ``pid_file_present`` / ``pid`` / ``pid_matches`` / ``port_listening`` /
  ``state``（陈旧即 ``pid_stale``：进程已不在或身份不匹配，两者不猜）；
* ``_backend_auth_state()`` 会读 ``control-token`` 并带 ``X-Admin-Control`` 头发
  只读 ``GET /admin/auth/status``（非模型请求、不打印令牌）；
* **不重启任何服务、不生成密钥、不发模型请求、不写 ``ai-gateway/data/``、
  不打印任何密钥内容**。

写面清单（2026-09-28，逐项核对本工具的全部 IO 入口）：HTTP 只有不带凭据的
只读 GET；文件只有 ``open(path, "r")`` 只读打开（``read_ledger_tail`` /
``scan_private_ledger`` / ``count_verified_rows``）；委托观察只走上面两个只读入口。
**没有 ``os.remove``／``os.unlink``／写模式 ``open``／``os.makedirs``／
子进程动作**——采集路径上的条件写已消掉。

退出码：0 = 三件事实都成立**且**私人账本未被写入；2 = 任一不成立或不可判定
（「未接线」是合法状态，但门必须报非零——绝不把未接线报成成功）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

_UM_SHARE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _UM_SHARE_DIR not in sys.path:
    sys.path.insert(0, _UM_SHARE_DIR)
from lib.paths import bootstrap  # noqa: E402

AI_GATEWAY_DIR = bootstrap(__file__)["repo"]

CONSOLE_PORT = 8096
KEY_MIN_CHARS = 32
TAIL_ROWS = 3
LEDGER_ROW_FIELDS = ("time", "model", "key_id", "attribution")
ATTRIBUTION_VERIFIED = "verified"
SEVEN_ATTRIBUTION_KEYS = (
    "file_ready", "file_reason", "instance_loaded", "key_chars", "key_file",
    "request_attribution_detail", "request_attribution_observed")
PROBE_TIMEOUT_S = 20.0

_MISSING = object()


# --------------------------------------------------------------------- IO 层
def http_get_json(port, path, timeout=PROBE_TIMEOUT_S):
    """不带凭据的只读 GET；返回 ``{"status", "json", "error"}``（只走 127.0.0.1）。"""
    url = "http://127.0.0.1:%d%s" % (int(port), path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 — 读 body 失败也是证据
            raw = b""
        status = exc.code
    except Exception as exc:  # noqa: BLE001 — 连接失败如实记错误
        return {"status": None, "json": None, "error": type(exc).__name__}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        payload = None
    return {"status": status, "json": payload, "error": None}


def read_ledger_tail(path, count=TAIL_ROWS):
    """只读账本尾部：返回 ``(rows, error)``；每行只留约定字段，绝不给内容以外的东西。"""
    if not path:
        return None, "ledger_path_unresolved"
    rows = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                rows.append({field: record.get(field)
                             for field in LEDGER_ROW_FIELDS})
    except OSError as exc:
        return None, "ledger_unreadable:%s" % type(exc).__name__
    return rows[-count:], None


def scan_private_ledger(path):
    """只读扫描私人总账：``{rows, key_id_nonempty, attribution_verified, error}``。"""
    result = {"path": path, "rows": 0, "unparsed": 0, "key_id_nonempty": 0,
              "attribution_verified": 0, "error": None}
    if not path:
        result["error"] = "ledger_path_unresolved"
        return result
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                result["rows"] += 1
                try:
                    record = json.loads(line)
                except ValueError:
                    result["unparsed"] += 1
                    continue
                if not isinstance(record, dict):
                    result["unparsed"] += 1
                    continue
                key_id = record.get("key_id")
                if isinstance(key_id, str) and key_id.strip():
                    result["key_id_nonempty"] += 1
                if record.get("attribution") == ATTRIBUTION_VERIFIED:
                    result["attribution_verified"] += 1
    except OSError as exc:
        result["error"] = "ledger_unreadable:%s" % type(exc).__name__
    return result


def count_verified_rows(path):
    """只读数 8110 账本里 ``attribution == "verified"`` 的行数；读不到返回 ``(None, error)``。"""
    if not path:
        return None, "ledger_path_unresolved"
    verified = 0
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) \
                        and record.get("attribution") == ATTRIBUTION_VERIFIED:
                    verified += 1
    except OSError as exc:
        return None, "ledger_unreadable:%s" % type(exc).__name__
    return verified, None


_PID_STATE_KEYS = ("pid_file", "pid_file_present", "pid", "pid_matches",
                   "port_listening", "state")


def backend_pid_facts():
    """8110 专用后端 pid 文件的只读报告：**不删任何文件、不写任何文件**。

    走产品的 ``production_backend_pid_state()``（全分支无写、无删）：陈旧
    ``backend.pid`` 照实报成 ``pid_stale``，文件原样留在盘上。读不到即
    ``state="unavailable"`` 并带原因码，绝不折成「不存在」。
    """
    out = {key: None for key in _PID_STATE_KEYS}
    out["error"] = None
    try:
        from console import share_production as console_share_production
        facts = console_share_production.production_backend_pid_state()
    except Exception as exc:  # noqa: BLE001 — 读不到是证据，不是崩溃
        out["state"] = "unavailable"
        out["error"] = "backend_pid_state_failed:%s" % type(exc).__name__
        return out
    if not isinstance(facts, dict):
        out["state"] = "unavailable"
        out["error"] = "backend_pid_state_invalid"
        return out
    for key in _PID_STATE_KEYS:
        out[key] = facts.get(key)
    return out


def share_ledger_path():
    """8110 账本路径：从 8110 的只读观察里取；取不到不猜路径。

    ``forget_stale=False``：这条只读路径不再可能删除陈旧 ``backend.pid``。
    """
    try:
        from console import share_production as console_share_production
        observed = console_share_production.production_backend_observe(
            forget_stale=False)
    except Exception as exc:  # noqa: BLE001 — 读不到是证据，不是崩溃
        return None, "backend_observe_failed:%s" % type(exc).__name__
    if isinstance(observed, dict):
        attribution = observed.get("attribution")
        if isinstance(attribution, dict):
            detail = attribution.get("request_attribution_detail")
            if isinstance(detail, dict) and isinstance(detail.get("ledger_file"),
                                                         str):
                return detail["ledger_file"], None
    return None, "share_ledger_path_unresolved"


def private_ledger_path():
    """私人总账路径：只认 ``usagesrv.CHAT2API_LOG``；导入失败不猜路径。"""
    try:
        import usagesrv
    except Exception as exc:  # noqa: BLE001 — 读不到是证据，不是崩溃
        return None, "usagesrv_import_failed:%s" % type(exc).__name__
    path = getattr(usagesrv, "CHAT2API_LOG", None)
    if not isinstance(path, str) or not path:
        return None, "private_ledger_path_unresolved"
    return path, None


def in_process_attribution():
    """同身份的只读直调（动作门 §4.1 的替代观察）：事实 1/2 的来源之一。"""
    out = {"source": "in_process_share_attribution_status",
           "file_ready": None, "file_reason": None, "key_chars": None,
           "key_file": None, "instance_loaded_value": None,
           "instance_loaded_basis": None, "instance_loaded_reason": None,
           "request_attribution_observed": None, "error": None}
    try:
        from console import share_production as console_share_production
        facts = console_share_production.share_attribution_status()
    except Exception as exc:  # noqa: BLE001 — 读不到是证据，不是崩溃
        out["error"] = "share_attribution_status_failed:%s" % type(exc).__name__
        return out
    if not isinstance(facts, dict):
        out["error"] = "share_attribution_status_invalid"
        return out
    for key in ("file_ready", "file_reason", "key_chars", "key_file"):
        out[key] = facts.get(key)
    loaded = facts.get("instance_loaded")
    if isinstance(loaded, dict):
        out["instance_loaded_value"] = loaded.get("value")
        out["instance_loaded_basis"] = loaded.get("basis")
        out["instance_loaded_reason"] = loaded.get("reason")
    observed = facts.get("request_attribution_observed")
    if isinstance(observed, bool):
        out["request_attribution_observed"] = observed
    return out


def read_surface_attribution(response):
    """纯函数：8096 读面 → 三态子字典（只在 7 键形状下可用）。"""
    out = {"http_status": None, "keys": None, "file_ready": None,
           "instance_loaded_value": None, "instance_loaded_basis": None,
           "instance_loaded_reason": None, "request_attribution_observed": None,
           "error": None}
    if not isinstance(response, dict):
        out["error"] = "status_probe_failed"
        return out
    out["http_status"] = response.get("status")
    payload = response.get("json")
    if not isinstance(payload, dict):
        out["error"] = "status_payload_unavailable"
        return out
    attribution = payload.get("attribution")
    if not isinstance(attribution, dict):
        out["error"] = "attribution_missing"
        return out
    keys = sorted(str(key) for key in attribution.keys())
    out["keys"] = keys
    if keys != sorted(SEVEN_ATTRIBUTION_KEYS):
        # 旧 4 键形状：读面没有三态子字段，事实 1/2 只能走同身份直调。
        out["error"] = "legacy_read_surface_shape"
        return out
    if isinstance(attribution.get("file_ready"), bool):
        out["file_ready"] = attribution["file_ready"]
    loaded = attribution.get("instance_loaded")
    if isinstance(loaded, dict):
        out["instance_loaded_value"] = loaded.get("value")
        out["instance_loaded_basis"] = loaded.get("basis")
        out["instance_loaded_reason"] = loaded.get("reason")
    observed = attribution.get("request_attribution_observed")
    if isinstance(observed, bool):
        out["request_attribution_observed"] = observed
    return out


def share_usage_facts(response):
    """纯函数：``/api/console/share-usage`` 响应 → ``{requests, keys, unattributed}``。"""
    out = {"http_status": None, "requests": None, "keys": None,
           "keys_rows": None, "unattributed": None,
           "attributionReasons": None, "error": None}
    if not isinstance(response, dict):
        out["error"] = "share_usage_probe_failed"
        return out
    out["http_status"] = response.get("status")
    payload = response.get("json")
    if not isinstance(payload, dict):
        out["error"] = "share_usage_payload_unavailable"
        return out
    if isinstance(payload.get("requests"), int):
        out["requests"] = payload["requests"]
    keys = payload.get("keys")
    if isinstance(keys, list):
        out["keys"] = keys
        # 只投出身份与计数，绝不带出任何 Key 明文（账本里本来也没有）。
        out["keys_rows"] = [
            {"key_id": row.get("key_id"), "requests": row.get("requests")}
            for row in keys if isinstance(row, dict)
        ]
    unattributed = payload.get("unattributed")
    if isinstance(unattributed, dict):
        out["unattributed"] = {
            "key_id": unattributed.get("key_id"),
            "requests": unattributed.get("requests"),
        }
    # attributionReasons 在 coverage 段（share_metering._finish_coverage），不在
    # unattributed 段；两处都查，读不到就是 None（不折成空表）。
    coverage = payload.get("coverage")
    reasons = None
    if isinstance(coverage, dict) and isinstance(
            coverage.get("attributionReasons"), dict):
        reasons = coverage["attributionReasons"]
    elif isinstance(unattributed, dict) and isinstance(
            unattributed.get("attributionReasons"), dict):
        reasons = unattributed["attributionReasons"]
    out["attributionReasons"] = reasons
    return out


def collect_facts(get=None):
    """采集三件事实与两个旁证（只读）。不重启、不生成密钥、不发模型请求。"""
    get = get or http_get_json

    status_response = get(CONSOLE_PORT, "/api/console/share-management/status")
    surface = read_surface_attribution(status_response)
    if isinstance(surface.get("keys"), list) and surface.get("error") is None:
        attribution = {
            "source": "console_read_surface",
            "file_ready": surface.get("file_ready"),
            "file_reason": None,
            "key_chars": None,
            "key_file": None,
            "instance_loaded_value": surface.get("instance_loaded_value"),
            "instance_loaded_basis": surface.get("instance_loaded_basis"),
            "instance_loaded_reason": surface.get("instance_loaded_reason"),
            "request_attribution_observed":
                surface.get("request_attribution_observed"),
            "error": None,
        }
        # 读面 7 键形状下 key_chars/key_file 在同层，补上（只给长度，不给内容）。
        payload = status_response.get("json")
        if isinstance(payload, dict) and isinstance(payload.get("attribution"),
                                                    dict):
            raw_attribution = payload["attribution"]
            attribution["file_reason"] = raw_attribution.get("file_reason")
            attribution["key_chars"] = raw_attribution.get("key_chars")
            attribution["key_file"] = raw_attribution.get("key_file")
    else:
        attribution = in_process_attribution()

    ledger_file, ledger_error = share_ledger_path()
    tail, tail_error = read_ledger_tail(ledger_file)
    verified_rows, verified_error = count_verified_rows(ledger_file)
    private_path, private_error = private_ledger_path()
    private_ledger = scan_private_ledger(private_path)
    if private_error and private_ledger.get("error") is None:
        private_ledger["error"] = private_error

    return {
        "console_port": CONSOLE_PORT,
        "read_surface_http_status": surface.get("http_status"),
        "read_surface_keys": surface.get("keys"),
        "read_surface_error": surface.get("error"),
        "attribution": attribution,
        "backend_pid": backend_pid_facts(),
        "share_ledger_file": ledger_file,
        "share_ledger_tail": tail,
        "share_ledger_tail_error": tail_error,
        "share_ledger_verified_rows": verified_rows,
        "share_ledger_verified_error": verified_error,
        "share_usage": share_usage_facts(
            get(CONSOLE_PORT, "/api/console/share-usage")),
        "private_ledger": private_ledger,
    }


# ------------------------------------------------------------- 纯判定层（验收）
def _item(item_id, title, verdict, detail):
    return {"id": item_id, "title": title, "verdict": verdict,
            "detail": detail}


def _fact_file_ready(facts):
    attribution = facts.get("attribution")
    if not isinstance(attribution, dict):
        return _item("fact1_file_ready", "事实 1 · 文件就绪（file_ready）",
                     "undecidable", "归因事实未取到")
    if attribution.get("error"):
        return _item("fact1_file_ready", "事实 1 · 文件就绪（file_ready）",
                     "undecidable",
                     "读取失败：%s" % attribution.get("error"))
    ready = attribution.get("file_ready")
    reason = attribution.get("file_reason")
    chars = attribution.get("key_chars")
    detail = "file_ready=%r file_reason=%r key_chars=%r" % (ready, reason, chars)
    if ready is True and reason == "ok" \
            and isinstance(chars, int) and chars >= KEY_MIN_CHARS:
        return _item("fact1_file_ready", "事实 1 · 文件就绪（file_ready）",
                     "pass", detail)
    return _item("fact1_file_ready", "事实 1 · 文件就绪（file_ready）",
                 "fail", detail)


def _fact_instance_loaded(facts):
    attribution = facts.get("attribution")
    if not isinstance(attribution, dict):
        return _item("fact2_instance_loaded",
                     "事实 2 · 8100 实例已载入（instance_loaded）",
                     "undecidable", "归因事实未取到")
    if attribution.get("error"):
        return _item("fact2_instance_loaded",
                     "事实 2 · 8100 实例已载入（instance_loaded）",
                     "undecidable", "读取失败：%s" % attribution.get("error"))
    value = attribution.get("instance_loaded_value")
    basis = attribution.get("instance_loaded_basis")
    reason = attribution.get("instance_loaded_reason")
    detail = "value=%r basis=%r reason=%r" % (value, basis, reason)
    if value == "yes" and basis == "inference":
        return _item("fact2_instance_loaded",
                     "事实 2 · 8100 实例已载入（instance_loaded）",
                     "pass", detail)
    if value in ("no", "unknown"):
        return _item("fact2_instance_loaded",
                     "事实 2 · 8100 实例已载入（instance_loaded）",
                     "fail", detail)
    return _item("fact2_instance_loaded",
                 "事实 2 · 8100 实例已载入（instance_loaded）",
                 "undecidable", detail)


def _fact_request_attribution(facts):
    title = "事实 3 · 请求归因被观察到（8110 账本 attribution=verified）"
    verified = facts.get("share_ledger_verified_rows")
    verified_error = facts.get("share_ledger_verified_error")
    usage = facts.get("share_usage")
    keys = None
    usage_error = None
    if isinstance(usage, dict):
        keys = usage.get("keys")
        usage_error = usage.get("error")
    observed = None
    attribution = facts.get("attribution")
    if isinstance(attribution, dict):
        observed = attribution.get("request_attribution_observed")
    detail = ("request_attribution_observed=%r（本读面不观察该事实）；"
              "8110 账本 verified 行=%r（错误=%r）；share-usage keys=%r"
              "（错误=%r）" % (observed, verified, verified_error,
                              keys if keys is not None else None, usage_error))
    if verified_error or usage_error:
        return _item("fact3_request_attribution", title, "undecidable", detail)
    if not isinstance(verified, int):
        return _item("fact3_request_attribution", title, "undecidable", detail)
    if verified < 1:
        return _item("fact3_request_attribution", title, "fail", detail)
    if not isinstance(keys, list) or not keys:
        return _item("fact3_request_attribution", title, "fail", detail)
    return _item("fact3_request_attribution", title, "pass", detail)


def _private_ledger_untouched(facts):
    title = "旁证 · 私人总账未被分享链写入（key_id 非空 = 0）"
    private = facts.get("private_ledger")
    if not isinstance(private, dict):
        return _item("private_ledger_untouched", title, "undecidable",
                     "私人账本未取到")
    if private.get("error"):
        return _item("private_ledger_untouched", title, "undecidable",
                     "读取失败：%s" % private.get("error"))
    keyed = private.get("key_id_nonempty")
    verified = private.get("attribution_verified")
    detail = "key_id 非空=%r attribution=verified=%r（行数=%r）" \
        % (keyed, verified, private.get("rows"))
    if keyed != 0:
        return _item("private_ledger_untouched", title, "fail", detail)
    if isinstance(verified, int) and verified != 0:
        return _item("private_ledger_untouched", title, "fail", detail)
    return _item("private_ledger_untouched", title, "pass", detail)


def evaluate_acceptance(facts):
    """纯函数：事实字典 → 三件事实判定（字典进、字典出）。

    返回 ``{"items": [...], "ok": bool, "exit_code": 0|2}``：三件事实都 ``pass``
    且私人账本未变才 0；任一 ``fail``/``undecidable`` 都是 2。
    """
    items = [
        _fact_file_ready(facts),
        _fact_instance_loaded(facts),
        _fact_request_attribution(facts),
        _private_ledger_untouched(facts),
    ]
    ok = all(item["verdict"] == "pass" for item in items)
    return {"items": items, "ok": ok, "exit_code": 0 if ok else 2}


def format_acceptance(facts, result):
    """打印三件事实 + 两个旁证；只给长度与字段，绝不给密钥内容。"""
    lines = ["== 三事实验收（动作门 §④，只读） =="]
    for item in result["items"]:
        lines.append("[%s] %s —— %s"
                      % (item["verdict"].upper(), item["title"], item["detail"]))
    attribution = facts.get("attribution")
    if isinstance(attribution, dict):
        lines.append("事实 1/2 来源：%s（file_ready=%r instance_loaded=%r/%r）"
                      % (attribution.get("source"),
                         attribution.get("file_ready"),
                         attribution.get("instance_loaded_value"),
                         attribution.get("instance_loaded_reason")))

    lines.append("== 8110 专用后端 pid 文件（只读；不删任何文件、不写任何文件） ==")
    pid_facts = facts.get("backend_pid")
    if isinstance(pid_facts, dict):
        lines.append("  state=%s pid_file=%s pid_file_present=%s pid=%s "
                     "pid_matches=%s port_listening=%s error=%s"
                     % (pid_facts.get("state"), pid_facts.get("pid_file"),
                        pid_facts.get("pid_file_present"), pid_facts.get("pid"),
                        pid_facts.get("pid_matches"),
                        pid_facts.get("port_listening"),
                        pid_facts.get("error")))
        if pid_facts.get("state") == "pid_stale":
            lines.append("  （陈旧 backend.pid：照实报出并原样保留，未被删除）")
    else:
        lines.append("  读不到")

    lines.append("== 8110 账本末 %d 行（time/model/key_id/attribution） ==" % TAIL_ROWS)
    tail = facts.get("share_ledger_tail")
    if isinstance(tail, list):
        for row in tail:
            lines.append("  %s" % json.dumps(row, ensure_ascii=False))
    else:
        lines.append("  读不到：%s" % facts.get("share_ledger_tail_error"))

    lines.append("== 控制台 /api/console/share-usage ==")
    usage = facts.get("share_usage")
    if isinstance(usage, dict):
        lines.append("  http=%s requests=%s keys=%s unattributed=%s "
                     "attributionReasons=%s error=%s"
                      % (usage.get("http_status"), usage.get("requests"),
                         json.dumps(usage.get("keys_rows"),
                                    ensure_ascii=False)
                         if usage.get("keys_rows") is not None
                         else None,
                         json.dumps(usage.get("unattributed"),
                                    ensure_ascii=False),
                         json.dumps(usage.get("attributionReasons"),
                                    ensure_ascii=False),
                         usage.get("error")))
    else:
        lines.append("  读不到")

    lines.append("== 私人总账 ==")
    private = facts.get("private_ledger")
    if isinstance(private, dict):
        lines.append("  path=%s rows=%s key_id 非空=%s attribution=verified=%s "
                     "error=%s"
                     % (private.get("path"), private.get("rows"),
                        private.get("key_id_nonempty"),
                        private.get("attribution_verified"),
                        private.get("error")))
    else:
        lines.append("  读不到")

    lines.append("== 8096 读面 ==")
    lines.append("  http=%s keys=%s error=%s"
                 % (facts.get("read_surface_http_status"),
                    facts.get("read_surface_keys"),
                    facts.get("read_surface_error")))
    lines.append("=" * 60)
    if result["ok"]:
        lines.append("结论：三件事实都成立且私人账本未变（退出码 0）")
    else:
        not_pass = [item["id"] for item in result["items"]
                    if item["verdict"] != "pass"]
        lines.append("结论：存在未成立项 %s（退出码 2；未接线是合法状态，"
                     "门必须报非零）" % not_pass)
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="分享 Key 计量三事实验收只读打印（动作门 §④）")
    parser.parse_args(argv)
    try:
        facts = collect_facts()
    except Exception as exc:  # noqa: BLE001 — 采集失败如实报非零
        print("[验收] 采集失败：%s: %s" % (type(exc).__name__, exc),
              file=sys.stderr)
        return 2
    result = evaluate_acceptance(facts)
    print(format_acceptance(facts, result))
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
