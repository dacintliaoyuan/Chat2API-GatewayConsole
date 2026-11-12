# -*- coding: utf-8 -*-
"""ZCode 供应商规则库「保留集」核对 / 受控应用 / 回退（动作门 ④ 与 ⑤ 的工具化）。

只读输入（本工具不改它们）：

* ``output/ai-gw-round6-20260927/zcode-keepset-and-backup.md`` —— 保留集口径
  （§2）、备份步骤（§5）、回退步骤（§6）与前置门（§7）；
* ``output/ai-gw-round6-20260927/zcode-keepset.json`` —— 机器推导的保留集
  （每个 ZCode binding 的 ``keep`` / ``retired`` / ``managed``）；
* ``ai-gateway/model_sync.py`` —— 规则库的**唯一写入规则**：``render_provider_config_sync``
  （:562-771）负责对齐 ``personalModelIds`` / ``modelOrder`` / ``providerModelRules``，
  产品调用方是 ``_provider_config_candidate``（:822-923）。

本工具**不重新实现**这套规则：``--check`` 与 ``--apply`` 都用产品函数
``render_provider_config_sync`` 求投影，所以「保留集应用后的文件」与真实同步写出的
文件出自同一段代码。本工具只做三件事：读现场、算投影、按显式开关写。

窗口顺序（完整判据见同目录 README.md）：

    # 先关闭 ZCode（3 个 ZCode.exe 会在设置界面重写两处存储）
    python ai-gateway/tools/zcode-config/keepset_apply.py --check
    python ai-gateway/tools/zcode-config/keepset_apply.py --apply
    # 重开 ZCode，核对 provider 与模型列表
    # 不符时回退：
    python ai-gateway/tools/zcode-config/keepset_apply.py --revert --backup output/<stamp>

边界（本工具的全部承诺）：

* 默认动作是 ``--check``：只读打开规则库、保留集与状态文件，**不写盘**；
* ``--apply`` / ``--revert`` 是显式写开关，且都**先备份、再写、写后读回核对**；
* 检测到 ``ZCode.exe`` 在运行就**拒绝**写（退出码 2）——窗口要求先关 ZCode；
* **绝不**改主配置 ``~/.zcode/v2/config.json``、**绝不**改 ``ai-gateway/data/``；
  本工具的唯一写入面是 ``--config`` 指向的规则库与 ``--backup-root`` 下的备份目录；
* **绝不**启动/停止 ZCode、**绝不**发模型请求、**绝不**生成密钥；
* **绝不**打印规则库内容：只打印路径、sha256、字节数、模型 id 与结构差异；结构差异里
  遇到疑似凭据字段（apiKey/token/secret/...）的值一律打码。

退出码：

* ``--check``：0 = 投影与当前规则库在 JSON 层逐键相等；2 = 有差异，或输入不可判定
  （规则库/保留集/状态文件读不到、保留集与状态文件或投影代码不一致）。读不到 ≠ 通过。
* ``--apply``：0 = 已写并读回核对（或本就无差异、未写盘）；2 = 拒绝，未写盘。
* ``--revert``：0 = 已从备份恢复且读回哈希一致；2 = 拒绝，未写盘。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 与产品代码同仓：把仓库根加进 sys.path，才能调用真实的规则库渲染函数
# （不复制规则、不另写一份实现）。迁移后应用根就是仓库根，没有上一级。
AI_GATEWAY_DIR = Path(__file__).resolve().parents[2]
if str(AI_GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(AI_GATEWAY_DIR))

REPO_ROOT = AI_GATEWAY_DIR
DEFAULT_KEEPSET = (REPO_ROOT / "output" / "ai-gw-round6-20260927"
                  / "zcode-keepset.json")
DEFAULT_CONFIG = Path.home() / ".zcode" / "v2" / "provider_config.json"
DEFAULT_STATE = AI_GATEWAY_DIR / "data" / "model-sync-state.json"
# 备份必须落在 .gitignore 覆盖的 output/ 下（.gitignore:75）：规则库里现有明文
# apiKey，备份副本落进可提交路径就等于把密钥写进 Git（材料 §5.1）。
DEFAULT_BACKUP_ROOT = REPO_ROOT / "output"

BEIJING = timezone(timedelta(hours=8))
BACKUP_NAME = "provider_config.json.bak"
DIGEST_NAME = "provider_config.json.sha256"

# 四态（动作门 ④ 的逐模型判据）。
STATUS_SAME = "SAME"
STATUS_ADDED = "ADDED"
STATUS_REMOVED = "REMOVED"
STATUS_OUT_OF_SCOPE = "OUT_OF_SCOPE"

# 结构差异里遇到这些字段名一律打码，绝不回显值。
SENSITIVE_HINTS = ("apikey", "api_key", "token", "secret", "password",
                   "authorization", "credential")
REDACTED = "<redacted>"

MAX_DIFF_LINES = 200
MAX_VALUE_CHARS = 80


# --------------------------------------------------------------------- IO 层
def read_text(path) -> str:
    with io.open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def read_bytes(path) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    return sha256_hex(read_bytes(path))


def read_json_file(path):
    """读并解析 JSON；读不到或解析失败抛异常（由调用方如实报原因码）。"""
    return json.loads(read_text(path))


def model_sync_module():
    """延迟导入产品模块 model_sync（导入本身无副作用，只读自身文件算代码指纹）。"""
    import model_sync
    return model_sync


def product_bindings():
    """产品里登记的 ZCode 绑定（``model_sync_core.DEFAULT_BINDINGS``）。"""
    return tuple(binding for binding in model_sync_module().DEFAULT_BINDINGS
                 if binding.consumer == "ZCode")


def zcode_running() -> bool:
    """只读检查 ``ZCode.exe`` 是否在运行；口径与产品同一处实现。

    委托 ``model_config_adapters.zcode_running``（:455-464，``tasklist`` 只读查询），
    绝不启动/停止任何进程。测试用注入的 ``running`` 替身，不真查本机进程。
    """
    import model_config_adapters
    return bool(model_config_adapters.zcode_running())


def write_bytes(path, data: bytes) -> None:
    """受控替换：先写同目录临时文件再 ``os.replace``，中断不留半份文件。"""
    path = Path(path)
    handle = tempfile.NamedTemporaryFile(
        "wb", delete=False, dir=str(path.parent), prefix=path.name + ".",
        suffix=".tmp")
    try:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)


# ----------------------------------------------------------------- 纯解析层
def rule_store_surface(doc):
    """纯函数：规则库文档 → ``provider -> 模型面``。

    与 ``model_sync._zcode_rule_surface``（:700-752）同一口径：一个 provider 的
    模型面 = ``personalModelIds`` ∪ ``modelOrder`` ∪ 该 provider 的
    ``providerModelRules`` 条目，按首次出现顺序去重。读不出来就是空面（由调用方
    在读取/解析失败时报原因码，绝不把读不到当空面）。
    """
    cfg = doc.get("config") if isinstance(doc, dict) else None
    cfg = cfg if isinstance(cfg, dict) else {}
    surface: dict[str, dict[str, list[str]]] = {}

    def slot(provider: str) -> dict[str, list[str]]:
        return surface.setdefault(provider, {"personalModelIds": [],
                                          "modelOrder": [], "rules": []})

    def add(provider, model_id, key):
        value = str(model_id or "").strip()
        if not provider or not value:
            return
        ids = slot(str(provider))[key]
        if value not in ids:
            ids.append(value)

    for rule in ((cfg.get("providerConfigRules") or {}).get("providerRules")
                 or []):
        if not isinstance(rule, dict):
            continue
        provider = str(rule.get("providerId") or "")
        rule_config = rule.get("config") or {}
        if not isinstance(rule_config, dict):
            continue
        for key in ("personalModelIds", "modelOrder"):
            for model_id in rule_config.get(key) or []:
                add(provider, model_id, key)
    for rule in ((cfg.get("modelConfigRules") or {}).get("providerModelRules")
                 or []):
        if isinstance(rule, dict):
            add(rule.get("providerId"), rule.get("modelId"), "rules")
    return surface


def surface_ids(entry) -> list[str]:
    """纯函数：一个 provider 的模型面 → 有序去重 id 列表。"""
    ids: list[str] = []
    for key in ("personalModelIds", "modelOrder", "rules"):
        for model_id in (entry or {}).get(key) or []:
            if model_id not in ids:
                ids.append(model_id)
    return ids


def stored_contexts(doc):
    """纯函数：规则库里已有的 ``contextWindow``（provider, model）→ 整数。

    本工具不携带目录，因此投影沿用规则库里已存的上下文窗口值，绝不猜一个新值；
    目录里没有的 id（新发布条目）不带 ``contextWindow``，由真实同步运行按目录补齐。
    """
    contexts: dict[tuple[str, str], int] = {}
    cfg = doc.get("config") if isinstance(doc, dict) else None
    cfg = cfg if isinstance(cfg, dict) else {}
    for rule in ((cfg.get("modelConfigRules") or {}).get("providerModelRules")
                 or []):
        if not isinstance(rule, dict):
            continue
        rule_config = rule.get("config")
        props = (rule_config.get("properties")
                 if isinstance(rule_config, dict) else None)
        props = props if isinstance(props, dict) else {}
        value = props.get("contextWindow")
        if isinstance(value, int) and not isinstance(value, bool):
            contexts[(str(rule.get("providerId") or ""),
                       str(rule.get("modelId") or ""))] = value
    return contexts


def keepset_entries(keepset):
    """纯函数：保留集文档 → 有序条目列表（每个 ZCode binding 一条）。"""
    entries = keepset.get("zcode") if isinstance(keepset, dict) else None
    entries = entries if isinstance(entries, dict) else {}
    out = []
    for key in sorted(entries):
        entry = entries[key] if isinstance(entries[key], dict) else {}
        out.append({
            "key": str(key),
            "keep": list(entry.get("keep") or []),
            "retired": list(entry.get("retired") or []),
            "managed": list(entry.get("managed") or []),
        })
    return out


def validate_keepset(keepset):
    """纯函数：保留集结构自检；返回原因码，合法返回 ``None``。

    ``managed`` 必须等于 ``keep`` ∪ ``retired``（材料 §2 的唯一口径），两者不得
    相交；binding 键必须是 ``source|ZCode|provider``。
    """
    if not isinstance(keepset, dict):
        return "keepset_not_object"
    entries = keepset.get("zcode")
    if not isinstance(entries, dict) or not entries:
        return "keepset_missing_zcode"
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            return "keepset_entry_invalid:%s" % key
        for field in ("keep", "retired", "managed"):
            value = entry.get(field)
            if not isinstance(value, list) or not all(
                    isinstance(item, str) for item in value):
                return "keepset_field_invalid:%s:%s" % (key, field)
        keep, retired, managed = (set(entry["keep"]), set(entry["retired"]),
                                 set(entry["managed"]))
        if keep & retired:
            return "keepset_keep_retired_overlap:%s" % key
        if keep | retired != managed:
            return "keepset_managed_mismatch:%s" % key
        parts = str(key).split("|")
        if len(parts) != 3 or parts[1] != "ZCode":
            return "keepset_binding_invalid:%s" % key
    return None


def bindings_for(entries, all_bindings):
    """纯函数：保留集条目 → ``(产品绑定对象列表, 未知键列表)``。

    绑定对象只从产品代码取（``provider_name`` / ``suffix`` 一并带上）；保留集里
    出现产品不认识的 binding 键时记未知，绝不猜一个绑定。
    """
    by_key = {binding.key: binding for binding in all_bindings}
    bindings, unknown = [], []
    for entry in entries:
        binding = by_key.get(entry["key"])
        if binding is None:
            unknown.append(entry["key"])
        else:
            bindings.append(binding)
    return bindings, unknown


def project_text(raw, entries, bindings):
    """纯函数：当前规则库文本 + 保留集 → 应用后的文本（写入规则来自产品）。

    直接调用 ``model_sync.render_provider_config_sync``：白名单保留规则、每模型规则
    的离开规则与「范围外不动」都由产品代码决定，本工具不复制一份。
    """
    from model_sync_core import CatalogModel
    module = model_sync_module()
    doc = json.loads(raw)
    contexts = stored_contexts(doc)
    catalogs: dict[str, list] = {}
    managed: dict[str, set[str]] = {}
    for entry, binding in zip(entries, bindings):
        catalogs[binding.source] = [
            CatalogModel(binding.source, model_id,
                         contexts.get((binding.provider, model_id)))
            for model_id in entry["keep"]
        ]
        managed[binding.key] = set(entry["managed"])
    # Rev8: the pinned keep-set was removed from the renderer's signature, so
    # this call names ``managed`` by keyword — the old positional ``{}`` would
    # land on ``managed`` and hand the registry to ``connections``.
    return module.render_provider_config_sync(raw, catalogs, bindings,
                                            managed=managed)


def classify(before_doc, after_doc, entries, bindings):
    """纯函数：投影前后两份文档 + 保留集 → 逐 provider/逐模型四态。

    四态直接由**投影前后**的面算出（不另立一套规则）：

    * ``SAME``：keep 里的 id，投影前后都在；
    * ``ADDED``：keep 里的 id，投影前不在（``blocked=True`` 表示投影后仍不在——
      该 provider 在规则库里没有规则块，产品渲染函数只对齐已存在的块，:518-522）；
    * ``REMOVED``：投影前在、投影后不在，且属于受管登记（``retired``）；
    * ``OUT_OF_SCOPE``：投影前在、投影后仍在、且不属于任何受管登记（手工条目，
      逐字节不动）；投影前在、投影后不在却不在受管登记里的异常也记在这里，并标
      ``dropped=True``。
    """
    before_surface = rule_store_surface(before_doc)
    after_surface = rule_store_surface(after_doc)
    providers = []
    for entry, binding in zip(entries, bindings):
        providers.append((binding.provider, binding.key, entry))
    for provider in sorted(before_surface):
        if provider not in {item[0] for item in providers}:
            providers.append((provider, None, None))

    blocks = []
    for provider, binding_key, entry in providers:
        keep = list(entry["keep"]) if entry else []
        managed = set(entry["managed"]) if entry else set()
        before_ids = surface_ids(before_surface.get(provider))
        after_ids = surface_ids(after_surface.get(provider))
        models = []
        for model_id in keep:
            if model_id in before_ids:
                models.append({"model_id": model_id, "status": STATUS_SAME,
                               "blocked": False, "dropped": False})
            else:
                models.append({"model_id": model_id, "status": STATUS_ADDED,
                               "blocked": model_id not in after_ids,
                               "dropped": False})
        for model_id in before_ids:
            if model_id in keep:
                continue
            if model_id not in after_ids and model_id in managed:
                models.append({"model_id": model_id, "status": STATUS_REMOVED,
                               "blocked": False, "dropped": False})
            else:
                models.append({"model_id": model_id,
                               "status": STATUS_OUT_OF_SCOPE, "blocked": False,
                               "dropped": model_id not in after_ids})
        blocks.append({
            "provider": provider,
            "binding": binding_key,
            "keep": keep,
            "retired": list(entry["retired"]) if entry else [],
            "block": provider in before_surface,
            "empty_keep": entry is not None and not keep,
            "models": models,
            "before": {key: list((before_surface.get(provider) or {}).get(key)
                                 or [])
                       for key in ("personalModelIds", "modelOrder", "rules")},
            "after": {key: list((after_surface.get(provider) or {}).get(key)
                               or [])
                      for key in ("personalModelIds", "modelOrder", "rules")},
        })
    counts = {status: 0 for status in (STATUS_SAME, STATUS_ADDED,
                                     STATUS_REMOVED, STATUS_OUT_OF_SCOPE)}
    blocked = []
    for block in blocks:
        for model in block["models"]:
            counts[model["status"]] += 1
        ids = [model["model_id"] for model in block["models"]
               if model["blocked"]]
        if ids:
            blocked.append({"provider": block["provider"], "model_ids": ids})
    return {"blocks": blocks, "counts": counts, "blocked": blocked}


def _identity(item):
    if not isinstance(item, dict):
        return None
    parts = [(key, item[key]) for key in ("providerId", "modelId")
             if isinstance(item.get(key), str) and item[key]]
    if not parts:
        return None
    return ",".join("%s=%s" % part for part in parts)


def _redact(key, value):
    if isinstance(key, str) and any(hint in key.lower()
                                  for hint in SENSITIVE_HINTS):
        return REDACTED
    return value


def json_diff(before, after, path="", out=None, limit=MAX_DIFF_LINES):
    """纯函数：两份 JSON → 叶子级差异列表 ``[{path, op, before, after}]``。

    字典按键递归；元素都是带 ``providerId`` / ``modelId`` 的字典时按该身份配对
    （规则库的 provider 规则块与每模型规则条目就是这个形状），其余列表按值集合
    比较；疑似凭据字段的值打码。
    """
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(before, dict) and isinstance(after, dict):
        for key in before:
            if key not in after:
                out.append({"path": "%s/%s" % (path, key), "op": "remove",
                            "before": _redact(key, before[key]), "after": None})
        for key in after:
            if key not in before:
                out.append({"path": "%s/%s" % (path, key), "op": "add",
                            "before": None, "after": _redact(key, after[key])})
            else:
                json_diff(before[key], after[key], "%s/%s" % (path, key),
                          out, limit)
        return out
    if isinstance(before, list) and isinstance(after, list):
        keys_before = [_identity(item) for item in before]
        keys_after = [_identity(item) for item in after]
        paired = (len(keys_before) == len(before)
                  and len(keys_after) == len(after)
                  and all(item is not None for item in keys_before)
                  and all(item is not None for item in keys_after))
        if paired:
            map_before = {item: value
                          for item, value in zip(keys_before, before)}
            map_after = {item: value for item, value in zip(keys_after, after)}
            for identity in keys_before:
                tag = "%s/[%s]" % (path, identity)
                if identity not in map_after:
                    out.append({"path": tag, "op": "remove",
                                "before": map_before[identity], "after": None})
                else:
                    json_diff(map_before[identity], map_after[identity], tag,
                               out, limit)
            for identity in keys_after:
                if identity not in map_before:
                    out.append({"path": "%s/[%s]" % (path, identity),
                                "op": "add", "before": None,
                                "after": map_after[identity]})
        elif before != after:
            plain_before = [item for item in before
                            if not isinstance(item, (dict, list))]
            plain_after = [item for item in after
                           if not isinstance(item, (dict, list))]
            if len(plain_before) == len(before) \
                    and len(plain_after) == len(after):
                removed = [item for item in plain_before
                           if item not in plain_after]
                added = [item for item in plain_after
                         if item not in plain_before]
                for item in removed:
                    out.append({"path": "%s/[]" % path, "op": "remove",
                                "before": _redact(path, item), "after": None})
                for item in added:
                    out.append({"path": "%s/[]" % path, "op": "add",
                                "before": None, "after": _redact(path, item)})
                if not removed and not added:
                    out.append({"path": "%s/[]" % path, "op": "change",
                                "before": "（顺序变化）", "after": "（顺序变化）"})
            else:
                out.append({"path": path, "op": "change", "before": "（列表）",
                            "after": "（列表）"})
        return out
    if before != after:
        key = str(path).rsplit("/", 1)[-1]
        out.append({"path": path, "op": "change",
                    "before": _redact(key, before),
                    "after": _redact(key, after)})
    return out


def text_layer_note(raw, canonical):
    """纯函数：当前字节 vs 产品规范形 → 文本层差异（与模型面无关）。"""
    note = {"byte_equal": raw == canonical, "bom": raw.startswith("\ufeff"),
            "crlf": "\r\n" in raw, "trailing_newline": raw.endswith("\n")}
    if note["byte_equal"]:
        note["detail"] = "字节与产品规范形一致"
        return note
    parts = []
    if note["bom"]:
        parts.append("当前文件带 BOM（产品规范形不带）")
    if note["crlf"]:
        parts.append("当前文件用 CRLF（产品规范形用 LF）")
    if not note["trailing_newline"]:
        parts.append("当前文件缺尾换行（产品规范形补一个）")
    note["detail"] = "；".join(parts) or "存在非模型面的文本差异"
    return note


def compare_managed(entries, state):
    """纯函数：保留集的 ``managed`` vs 状态文件 ``managed``（防用陈旧保留集）。"""
    managed = state.get("managed") if isinstance(state, dict) else None
    managed = managed if isinstance(managed, dict) else {}
    bindings = []
    for entry in entries:
        key = entry["key"]
        current = managed.get(key)
        current_ids = set(current) if isinstance(current, dict) else None
        keepset_ids = set(entry["managed"])
        if current_ids is None:
            bindings.append({"key": key, "state_managed": None,
                             "only_keepset": sorted(keepset_ids),
                             "only_state": []})
            continue
        bindings.append({
            "key": key,
            "state_managed": len(current_ids),
            "only_keepset": sorted(keepset_ids - current_ids),
            "only_state": sorted(current_ids - keepset_ids),
        })
    return {"bindings": bindings,
            "stale": any(item["state_managed"] is None
                         or item["only_keepset"] or item["only_state"]
                         for item in bindings)}


def _code_stamp_or_none():
    try:
        return model_sync_module().CODE_STAMP
    except Exception:  # noqa: BLE001 — 读不到是证据，不是崩溃
        return None


def _canonical_text(doc) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


# ----------------------------------------------------------------- 动作层
def check_keepset(config_path, keepset_path, state_path, running=None):
    """只读核对：返回结果字典（含 ``exit_code``），绝不写盘。"""
    running = running or zcode_running
    result = {"action": "check", "exit_code": 2, "reason": None,
              "undecidable": True, "config_path": str(config_path),
              "keepset_path": str(keepset_path), "state_path": str(state_path),
              "wrote": False, "state": {}}
    if running():
        # 只读核对也报出来：ZCode 运行中时读到的规则库可能正被它改写。
        result["zcode_running"] = True
    try:
        keepset = read_json_file(keepset_path)
    except (OSError, ValueError) as exc:
        result["reason"] = "keepset_unreadable:%s" % type(exc).__name__
        return result
    problem = validate_keepset(keepset)
    if problem:
        result["reason"] = problem
        return result
    entries = keepset_entries(keepset)
    try:
        raw = read_text(config_path)
        raw_bytes = read_bytes(config_path)
    except OSError as exc:
        result["reason"] = "config_unreadable:%s" % type(exc).__name__
        return result
    try:
        doc = json.loads(raw)
    except ValueError:
        result["reason"] = "config_not_json"
        return result
    try:
        all_bindings = product_bindings()
    except Exception as exc:  # noqa: BLE001 — 读不到是证据，不是崩溃
        result["reason"] = "product_bindings_failed:%s" % type(exc).__name__
        return result
    bindings, unknown = bindings_for(entries, all_bindings)
    if unknown:
        result["reason"] = "keepset_binding_unknown:%s" % ",".join(unknown)
        return result
    sources = [binding.source for binding in bindings]
    if len(set(sources)) != len(sources):
        result["reason"] = "keepset_source_duplicate"
        return result
    result["config_sha256"] = sha256_hex(raw_bytes)
    result["config_bytes"] = len(raw_bytes)
    result["keepset_derived_at"] = keepset.get("derived_at")
    result["keepset_code_stamp"] = keepset.get("code_stamp")
    try:
        projected = project_text(raw, entries, bindings)
    except Exception as exc:  # noqa: BLE001 — 投影失败如实报，不绕过
        result["reason"] = "projection_failed:%s" % type(exc).__name__
        return result
    after_doc = json.loads(projected)
    result["projected_sha256"] = sha256_hex(projected.encode("utf-8"))
    result["projected_bytes"] = len(projected.encode("utf-8"))
    result["json_equal"] = after_doc == doc
    result["diff"] = json_diff(doc, after_doc)
    result["diff_truncated"] = len(result["diff"]) >= MAX_DIFF_LINES
    result["classified"] = classify(doc, after_doc, entries, bindings)
    result["text_layer"] = text_layer_note(raw, _canonical_text(doc))
    result["code_stamp"] = _code_stamp_or_none()
    try:
        state = read_json_file(state_path)
    except (OSError, ValueError) as exc:
        result["state"] = {"error": type(exc).__name__}
        result["reason"] = "state_unreadable:%s" % type(exc).__name__
        return result
    result["state"] = compare_managed(entries, state)
    if result["keepset_code_stamp"] != result["code_stamp"]:
        result["reason"] = "code_stamp_mismatch:%s!=%s" % (
            result["keepset_code_stamp"], result["code_stamp"])
        return result
    if result["state"]["stale"]:
        result["reason"] = "keepset_stale"
        return result
    result["undecidable"] = False
    if result["classified"]["blocked"]:
        # A keep id whose provider block is missing cannot be projected: the
        # tool never creates blocks (render_provider_config_sync's legacy
        # caller), so this is a decidable refusal, never a silent success.
        result["reason"] = "rule_block_missing:%s" % ",".join(
            item["provider"] for item in result["classified"]["blocked"])
        return result
    result["exit_code"] = 0 if result["json_equal"] else 2
    if not result["json_equal"]:
        result["reason"] = "json_differs"
    return result


def apply_keepset(config_path, keepset_path, state_path, backup_root,
                  running=None, stamp=None):
    """受控应用：门全过才写，先按字节备份、写后读回核对。绝不打印配置内容。"""
    running = running or zcode_running
    result = {"action": "apply", "exit_code": 2, "reason": None,
              "config_path": str(config_path), "wrote": False,
              "backup": None, "stamp": None}
    if running():
        result["reason"] = "zcode_running"
        return result
    try:
        keepset = read_json_file(keepset_path)
    except (OSError, ValueError) as exc:
        result["reason"] = "keepset_unreadable:%s" % type(exc).__name__
        return result
    problem = validate_keepset(keepset)
    if problem:
        result["reason"] = problem
        return result
    entries = keepset_entries(keepset)
    try:
        raw = read_text(config_path)
        raw_bytes = read_bytes(config_path)
    except OSError as exc:
        result["reason"] = "config_unreadable:%s" % type(exc).__name__
        return result
    try:
        doc = json.loads(raw)
    except ValueError:
        result["reason"] = "config_not_json"
        return result
    try:
        all_bindings = product_bindings()
    except Exception as exc:  # noqa: BLE001 — 读不到是证据，不是崩溃
        result["reason"] = "product_bindings_failed:%s" % type(exc).__name__
        return result
    bindings, unknown = bindings_for(entries, all_bindings)
    if unknown:
        result["reason"] = "keepset_binding_unknown:%s" % ",".join(unknown)
        return result
    sources = [binding.source for binding in bindings]
    if len(set(sources)) != len(sources):
        result["reason"] = "keepset_source_duplicate"
        return result
    code_stamp = _code_stamp_or_none()
    if keepset.get("code_stamp") != code_stamp:
        result["reason"] = "code_stamp_mismatch:%s!=%s" % (
            keepset.get("code_stamp"), code_stamp)
        return result
    try:
        state = read_json_file(state_path)
    except (OSError, ValueError) as exc:
        result["reason"] = "state_unreadable:%s" % type(exc).__name__
        return result
    if compare_managed(entries, state)["stale"]:
        result["reason"] = "keepset_stale"
        return result
    try:
        projected = project_text(raw, entries, bindings)
    except Exception as exc:  # noqa: BLE001 — 投影失败如实报，不绕过
        result["reason"] = "projection_failed:%s" % type(exc).__name__
        return result
    after_doc = json.loads(projected)
    classified = classify(doc, after_doc, entries, bindings)
    result["classified"] = classified
    if classified["blocked"]:
        # 缺规则块的 provider：本工具不新建块（:518-522），绝不写半份。
        # 该 provider 的 keep 无法补齐，即使其余部分没有差异也不是成功。
        result["reason"] = "rule_block_missing:%s" % ",".join(
            item["provider"] for item in classified["blocked"])
        return result
    if after_doc == doc:
        result["exit_code"] = 0
        result["reason"] = "no_difference"
        return result
    result["source_sha256"] = sha256_hex(raw_bytes)
    result["source_bytes"] = len(raw_bytes)
    try:
        backup = write_backup(backup_root, raw_bytes, stamp=stamp)
    except OSError as exc:
        result["reason"] = "backup_failed:%s" % type(exc).__name__
        return result
    result["backup"] = backup
    result["stamp"] = backup["stamp"]
    try:
        write_bytes(config_path, projected.encode("utf-8"))
    except OSError as exc:
        result["reason"] = "write_failed:%s" % type(exc).__name__
        return result
    result["wrote"] = True
    try:
        written = sha256_file(config_path)
    except OSError as exc:
        result["reason"] = "readback_failed:%s" % type(exc).__name__
        return result
    result["projected_sha256"] = sha256_hex(projected.encode("utf-8"))
    result["written_sha256"] = written
    if written != result["projected_sha256"]:
        result["reason"] = "readback_mismatch:%s!=%s" % (
            written, result["projected_sha256"])
        return result
    result["exit_code"] = 0
    result["reason"] = "applied"
    return result


def write_backup(backup_root, raw_bytes, stamp=None):
    """写备份：``<backup_root>/<stamp>/provider_config.json.bak`` + 哈希记录。

    目录已存在就拒绝（不覆盖既有备份）；备份写后读回，sha256 与源字节一致才写
    哈希记录（``provider_config.json.sha256``，只写十六进制摘要，不含配置内容）。
    """
    stamp = stamp or datetime.now(BEIJING).strftime("%Y%m%d-%H%M%S")
    directory = Path(backup_root) / stamp
    directory.mkdir(parents=True, exist_ok=False)
    backup_path = directory / BACKUP_NAME
    digest_path = directory / DIGEST_NAME
    backup_path.write_bytes(raw_bytes)
    digest = sha256_hex(raw_bytes)
    readback = sha256_file(backup_path)
    if readback != digest:
        raise OSError("backup_readback_mismatch:%s!=%s" % (readback, digest))
    digest_path.write_text(digest + "\n", encoding="ascii", newline="\n")
    recorded = digest_path.read_text(encoding="ascii").strip()
    if recorded != digest:
        raise OSError("backup_digest_readback_mismatch")
    return {"stamp": stamp, "directory": str(directory),
            "backup_path": str(backup_path), "digest_path": str(digest_path),
            "digest": digest, "bytes": len(raw_bytes)}


def resolve_backup(path):
    """``--backup`` 可给 ``.bak`` 文件或它所在目录；返回 ``(备份, 哈希记录)``。"""
    target = Path(path)
    if target.is_dir():
        target = target / BACKUP_NAME
    return target, target.with_name(DIGEST_NAME)


def revert_backup(config_path, backup_path, running=None):
    """回退：备份哈希记录核对通过才写，写后读回哈希核对。"""
    running = running or zcode_running
    result = {"action": "revert", "exit_code": 2, "reason": None,
              "config_path": str(config_path),
              "backup": str(backup_path), "wrote": False}
    if running():
        result["reason"] = "zcode_running"
        return result
    backup_file, digest_file = resolve_backup(backup_path)
    result["backup"] = str(backup_file)
    if not backup_file.exists():
        result["reason"] = "backup_missing"
        return result
    if not digest_file.exists():
        result["reason"] = "backup_digest_missing"
        return result
    try:
        raw_bytes = read_bytes(backup_file)
        recorded = digest_file.read_text(encoding="ascii").strip()
    except OSError as exc:
        result["reason"] = "backup_unreadable:%s" % type(exc).__name__
        return result
    digest = sha256_hex(raw_bytes)
    result["backup_sha256"] = digest
    result["recorded_sha256"] = recorded
    if digest != recorded:
        result["reason"] = "backup_digest_mismatch"
        return result
    try:
        json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeError, ValueError):
        result["reason"] = "backup_not_json"
        return result
    try:
        result["previous_sha256"] = sha256_file(config_path)
    except OSError:
        result["previous_sha256"] = None
    try:
        write_bytes(config_path, raw_bytes)
    except OSError as exc:
        result["reason"] = "write_failed:%s" % type(exc).__name__
        return result
    result["wrote"] = True
    try:
        written = sha256_file(config_path)
    except OSError as exc:
        result["reason"] = "readback_failed:%s" % type(exc).__name__
        return result
    result["restored_sha256"] = written
    if written != digest:
        result["reason"] = "readback_mismatch:%s!=%s" % (written, digest)
        return result
    result["exit_code"] = 0
    result["reason"] = "reverted"
    return result


# ------------------------------------------------------------- 人读输出层
def _short(value) -> str:
    if value == REDACTED:
        return REDACTED
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "…"


def _status_line(model) -> str:
    tags = [model["status"]]
    if model["blocked"]:
        tags.append("BLOCKED")
    if model["dropped"]:
        tags.append("DROPPED")
    return "   [%s] %s" % ("·".join(tags), model["model_id"])


def _surface_line(block) -> str:
    parts = []
    for key in ("personalModelIds", "modelOrder", "rules"):
        before, after = block["before"][key], block["after"][key]
        if before != after:
            parts.append("%s %d → %d" % (key, len(before), len(after)))
    return "；".join(parts) if parts else "三处面均无变化"


def _state_lines(result) -> list[str]:
    state = result.get("state") or {}
    lines = []
    if "error" in state:
        lines.append("[核对] 状态文件 %s 读不到（%s）→ 不可判定"
                     % (result["state_path"], state["error"]))
        return lines
    bindings = state.get("bindings") or []
    stale = [item for item in bindings
             if item["state_managed"] is None or item["only_keepset"]
             or item["only_state"]]
    if not stale:
        lines.append("[核对] 状态文件 %s 的受管登记与保留集一致（%d 个 binding）"
                     % (result["state_path"], len(bindings)))
        return lines
    lines.append("[核对] 状态文件 %s 与保留集不一致 → 保留集已陈旧（%d 个 binding）"
                 % (result["state_path"], len(stale)))
    for item in stale:
        lines.append("   %s：state 受管=%s 只在保留集=%s 只在状态=%s"
                     % (item["key"], item["state_managed"],
                        item["only_keepset"], item["only_state"]))
    return lines


def format_check(result) -> str:
    """把 ``--check`` 结果排成逐 provider/逐模型的人读文本（不含配置内容）。"""
    lines = ["[核对] 规则库 %s" % result["config_path"]]
    if "config_sha256" in result:
        lines.append("[核对] 当前 sha256=%s bytes=%d"
                     % (result["config_sha256"], result["config_bytes"]))
    lines.append("[核对] 保留集 %s（derived_at=%s code_stamp=%s）"
                 % (result["keepset_path"], result.get("keepset_derived_at"),
                    result.get("keepset_code_stamp")))
    lines.append("[核对] 投影代码 code_stamp=%s" % result.get("code_stamp"))
    if result.get("zcode_running"):
        lines.append("[核对] ZCode.exe 正在运行：读到的规则库可能正被设置界面改写")
    lines.extend(_state_lines(result))
    if result["undecidable"]:
        lines.append("[核对] 原因码：%s（退出码 %d，未写盘）"
                     % (result["reason"], result["exit_code"]))
        return "\n".join(lines)
    classified = result["classified"]
    counts = classified["counts"]
    lines.append("[核对] 统计：SAME=%d ADDED=%d REMOVED=%d OUT_OF_SCOPE=%d"
                 % (counts[STATUS_SAME], counts[STATUS_ADDED],
                    counts[STATUS_REMOVED], counts[STATUS_OUT_OF_SCOPE]))
    for block in classified["blocks"]:
        lines.append("-- provider %s（binding=%s，规则块=%s）"
                     % (block["provider"], block["binding"] or "未登记",
                        "有" if block["block"] else "缺"))
        lines.append("   %s" % _surface_line(block))
        if block["empty_keep"]:
            lines.append("   keep 为空：产品渲染函数对空目录不误删"
                         "（model_sync.py:512-517），该 provider 本轮不会被对齐")
        for model in block["models"]:
            lines.append(_status_line(model))
    if classified["blocked"]:
        lines.append("[核对] 阻塞：以下 provider 在规则库里没有规则块，产品渲染函数只对齐"
                     "已存在的块（model_sync.py:518-522），本工具不会新建块：")
        for item in classified["blocked"]:
            lines.append("   %s：%d 条 keep 无法补齐（%s）"
                         % (item["provider"], len(item["model_ids"]),
                            ",".join(item["model_ids"])))
    lines.append("[核对] 文本层：%s" % result["text_layer"]["detail"])
    lines.append("[核对] JSON 差异 %d 条%s："
                 % (len(result["diff"]),
                    "（已截断）" if result["diff_truncated"] else ""))
    if not result["diff"]:
        lines.append("   （无）")
    for item in result["diff"]:
        lines.append("   %-6s %s  %s -> %s"
                     % (item["op"], item["path"], _short(item["before"]),
                        _short(item["after"])))
    if result["json_equal"] and not classified["blocked"]:
        lines.append("[核对] 结论：投影与当前规则库在 JSON 层逐键相等（退出码 0）")
    else:
        lines.append("[核对] 结论：与保留集不一致（退出码 %d）" % result["exit_code"])
    return "\n".join(lines)


def format_action(result) -> str:
    """把 ``--apply`` / ``--revert`` 结果排成人读文本（只给路径与哈希）。"""
    label = "应用" if result["action"] == "apply" else "回退"
    lines = ["[%s] 规则库 %s" % (label, result["config_path"])]
    if result["action"] == "apply":
        backup = result.get("backup")
        if backup:
            lines.append("[应用] 备份 %s（sha256=%s bytes=%d，哈希记录 %s）"
                         % (backup["backup_path"], backup["digest"],
                            backup["bytes"], backup["digest_path"]))
        if result.get("source_sha256"):
            lines.append("[应用] 源 sha256=%s bytes=%d"
                         % (result["source_sha256"], result["source_bytes"]))
        if result.get("projected_sha256"):
            lines.append("[应用] 投影 sha256=%s" % result["projected_sha256"])
    else:
        if result.get("backup"):
            lines.append("[回退] 备份 %s（记录 sha256=%s 实测=%s）"
                         % (result["backup"], result.get("recorded_sha256"),
                            result.get("backup_sha256")))
        if result.get("previous_sha256"):
            lines.append("[回退] 覆盖前目标 sha256=%s" % result["previous_sha256"])
        if result.get("restored_sha256"):
            lines.append("[回退] 恢复后读回 sha256=%s" % result["restored_sha256"])
    if result.get("written_sha256"):
        lines.append("[%s] 写后读回 sha256=%s" % (label, result["written_sha256"]))
    lines.append("[%s] 原因码：%s（退出码 %d%s）"
                 % (label, result["reason"], result["exit_code"],
                    "" if result["wrote"] else "，未写盘"))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ZCode 供应商规则库保留集核对 / 受控应用 / 回退（动作门 ④/⑤）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true",
                       help="只读核对当前规则库与保留集（默认动作）")
    group.add_argument("--apply", action="store_true",
                       help="显式写开关：先备份再写，写后读回核对")
    group.add_argument("--revert", action="store_true",
                       help="显式写开关：从 --backup 恢复，写后读回核对")
    parser.add_argument("--backup", metavar="PATH",
                        help="--revert 的备份文件或它所在目录")
    parser.add_argument("--config", metavar="PATH", default=str(DEFAULT_CONFIG),
                        help="规则库路径（默认 ~/.zcode/v2/provider_config.json）")
    parser.add_argument("--keepset", metavar="PATH", default=str(DEFAULT_KEEPSET),
                        help="保留集 JSON 路径")
    parser.add_argument("--state", metavar="PATH", default=str(DEFAULT_STATE),
                        help="model-sync 状态文件（只读比对受管登记）")
    parser.add_argument("--backup-root", metavar="PATH",
                        default=str(DEFAULT_BACKUP_ROOT),
                        help="备份根目录（备份落在 <root>/<stamp>/）")
    parser.add_argument("--stamp", metavar="NAME",
                        help="备份目录名（默认北京时间 yyyymmdd-HHMMSS）")
    return parser


def main(argv=None, running=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.backup and not args.revert:
        parser.error("--backup 只与 --revert 一起使用")
    if args.revert and not args.backup:
        parser.error("--revert 需要 --backup <path>")
    if args.revert:
        result = revert_backup(args.config, args.backup, running=running)
    elif args.apply:
        result = apply_keepset(args.config, args.keepset, args.state,
                              args.backup_root, running=running,
                              stamp=args.stamp)
    else:
        result = check_keepset(args.config, args.keepset, args.state,
                             running=running)
    print(format_action(result) if args.apply or args.revert
          else format_check(result))
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
