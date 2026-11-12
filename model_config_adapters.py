# -*- coding: utf-8 -*-
"""DSH YAML 和 ZCode JSON 的配置适配器。

每个 adapter 保留各自的格式差异，不为追求统一而发明通用配置 AST。
读取、应用、备份、重新解析验证都在这里完成。
"""
from __future__ import annotations

import errno
import io
import json
import os
import re
import shutil
import tempfile
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from model_reconcile import KNOWN_MODELS, UM_SUFFIX, RepairItem
from model_sync_core import CatalogModel, SyncChange

try:
    import yaml
except ImportError:
    yaml = None


# ---------------------------------------------------------------------------
# DSH YAML adapter
# ---------------------------------------------------------------------------

def _mark_offset(text: str, mark: Any) -> int:
    """把 PyYAML 的 (行, 列) 标记换算成文本字符偏移。"""
    if mark.line == 0:
        return mark.column
    lines = text.splitlines(keepends=True)
    return sum(len(line) for line in lines[:mark.line]) + mark.column


def _mapping_child(node: Any, key: str) -> Any | None:
    """返回 mapping 节点中某个键的 value 节点；缺失或无映射时返回 None。"""
    if not isinstance(node, yaml.MappingNode):
        return None
    for child_key, child_value in node.value:
        if child_key.value == key:
            return child_key, child_value
    return None


def dsh_provider_span(text: str, prov: str) -> tuple[int, int]:
    """指定 provider 块在 yaml 文本中的 [start, end) 范围。

    起止取自解析后节点的行列标记，而不是按缩进扫描：缩进扫描会把
    后续无关章节中恰好 4 空格缩进的行（例如 ``llm-deepseek`` flow
    块里的 ``models:``）误判为 provider 结尾。
    """
    if yaml is None:
        raise RuntimeError("PyYAML is required to locate DSH providers")
    doc = yaml.compose(text)
    pi_ai = _mapping_child(doc, "llm-pi-ai")
    found = None
    if pi_ai is not None:
        providers = _mapping_child(pi_ai[1], "providers")
        if providers is not None:
            found = _mapping_child(providers[1], prov)
    if found is None:
        raise ValueError(f"DSH 配置缺少受管 provider：{prov}")
    key_node, value_node = found
    return (_mark_offset(text, key_node.start_mark),
            _mark_offset(text, value_node.end_mark))


def dsh_model_span(block: str, mid: str) -> tuple[int, int]:
    marker = "        - id: %s\n" % mid
    i = block.index(marker)
    j = block.find("\n        - id:", i + 10)
    return i, (j if j != -1 else len(block))


def dsh_apply_ctx(text: str, prov: str, mid: str, ctx: int) -> str:
    start, end = dsh_provider_span(text, prov)
    block = text[start:end]
    i, j = dsh_model_span(block, mid)
    seg = re.sub(r"contextWindow: \d+", "contextWindow: %d" % ctx,
                 block[i:j], count=1)
    return text[:start] + block[:i] + seg + block[j:] + text[end:]


def dsh_apply_remove(text: str, prov: str, mid: str) -> str:
    start, end = dsh_provider_span(text, prov)
    block = text[start:end]
    i, j = dsh_model_span(block, mid)
    if j < len(block):
        j += 1  # 吃掉下一个条目前的换行，避免残留空行
    return text[:start] + block[:i] + block[j:] + text[end:]


def _expected_model_name(model: CatalogModel, suffix: str) -> str:
    base = model.display_name or model.model_id
    return base if base.endswith(suffix) else base + suffix


def _dsh_model_entry(change: SyncChange) -> dict[str, Any]:
    model = change.model
    if model is None or change.model_id is None:
        raise ValueError("DSH model change is missing model metadata")
    entry = deepcopy(change.previous_profile or {})
    entry["id"] = change.model_id
    entry["name"] = _expected_model_name(model, change.binding.suffix)
    if not change.preserve_context:
        # R6/D4：客户端上下文无法证明是我们发布的时不改写（保留现状，
        # 包括「客户端本来没有该字段」的现状）。
        if model.context is not None:
            entry["contextWindow"] = model.context
        entry.setdefault("contextWindow", 128000)
    entry.setdefault("maxTokens", 16384)
    if model.efforts:
        entry["reasoningEfforts"] = {
            "off": None,
            **{effort: effort for effort in model.efforts},
        }
    return entry


def dsh_apply_add(text: str, prov: str, entry: dict[str, Any]) -> str:
    """Append one model mapping without rewriting unrelated YAML."""
    if yaml is None:
        raise RuntimeError("PyYAML is required to render DSH settings")
    start, end = dsh_provider_span(text, prov)
    block = text[start:end].rstrip("\n")
    rendered = yaml.safe_dump(
        [entry], allow_unicode=True, sort_keys=False,
        default_flow_style=False).rstrip("\n")
    rendered = "\n".join("        " + line for line in rendered.splitlines())
    return text[:start] + block + "\n" + rendered + "\n" + text[end:].lstrip("\n")


def dsh_apply_provider_name(text: str, prov: str, display_name: str) -> str:
    start, end = dsh_provider_span(text, prov)
    block = text[start:end]
    replacement = "      displayName: " + json.dumps(display_name, ensure_ascii=False)
    updated, count = re.subn(
        r"(?m)^      displayName:.*$", replacement, block, count=1)
    if count == 0:
        first_line_end = block.find("\n")
        updated = (block[:first_line_end + 1] + replacement + "\n" +
                   block[first_line_end + 1:])
    return text[:start] + updated + text[end:]


def render_dsh_sync(raw: str, changes: list[SyncChange]) -> str:
    """Render and validate all DSH synchronization changes in memory."""
    nl = "\r\n" if "\r\n" in raw else "\n"
    text = raw.replace(nl, "\n") if nl != "\n" else raw
    for change in changes:
        provider = change.binding.provider
        if change.kind == "provider_name":
            text = dsh_apply_provider_name(
                text, provider, change.binding.provider_name)
        elif change.kind == "remove":
            text = dsh_apply_remove(text, provider, change.model_id)
        elif change.kind in ("add", "update"):
            if change.kind == "update":
                text = dsh_apply_remove(text, provider, change.model_id)
            text = dsh_apply_add(text, provider, _dsh_model_entry(change))
    output = text.replace("\n", nl) if nl != "\n" else text
    if yaml is None:
        raise RuntimeError("PyYAML is required to validate DSH settings")
    yaml.safe_load(output)
    return output


def dsh_read(path: Path) -> dict[str, Any] | None:
    """Read and parse DSH settings.yaml, returning None on failure."""
    if yaml is None:
        return None
    try:
        with io.open(path, encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except Exception:
        return None


# Windows 上 os.replace 会因目标文件正被其他进程打开而失败：客户端轮询读取
# 或安全软件扫描的瞬间，MoveFileEx 报 ERROR_ACCESS_DENIED / SHARING_VIOLATION /
# LOCK_VIOLATION，Python 侧表现为 PermissionError/OSError（errno EACCES/EPERM/EBUSY，
# 实测有读者时 30 次写入 4 次耗尽）。这里按固定退避序列有限重试，耗尽后原样抛出；
# 绝不无限重试，也绝不吞掉其他错误（ENOENT/EXDEV 等立即失败）。
RENAME_RETRY_DELAYS_S: tuple[float, ...] = (0.025, 0.05, 0.1, 0.2, 0.4, 0.8)
_RENAME_RETRYABLE_WINERRORS = frozenset({5, 32, 33, 170})


def _rename_error_retryable(exc: OSError) -> bool:
    """该 rename 错误是否属于「目标暂时被占用」的可重试类。"""
    if getattr(exc, "winerror", None) in _RENAME_RETRYABLE_WINERRORS:
        return True
    return getattr(exc, "errno", None) in (errno.EPERM, errno.EACCES,
                                          errno.EBUSY)


def replace_with_retry(source: Path, target: Path, *,
                      delays: tuple[float, ...] = RENAME_RETRY_DELAYS_S) -> None:
    """``os.replace`` 的有界重试：仅对 Windows 占用类错误退避重试。

    共尝试 ``len(delays) + 1`` 次（首次 + 每个退避一次），最后一次失败原样
    抛出——耗尽即明确失败，调用方按发布失败处理，绝不静默继续。
    """
    for attempt in range(len(delays) + 1):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if not _rename_error_retryable(exc) or attempt == len(delays):
                raise
            time.sleep(delays[attempt])


def _atomic_replace_with_backup(
        path: Path, content: str, backup_path: Path) -> None:
    """Write validated text atomically while preserving the original."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", newline="", delete=False,
                dir=path.parent, prefix=path.name + ".", suffix=".tmp") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        shutil.copy2(path, backup_path)
        replace_with_retry(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def dsh_apply_fixes(path: Path, fixes: list[RepairItem]) -> bool:
    """Apply DSH fixes atomically after validating the complete result."""
    with io.open(path, encoding="utf-8", newline="") as handle:
        raw = handle.read()
    nl = "\r\n" if "\r\n" in raw else "\n"
    text = raw.replace(nl, "\n") if nl != "\n" else raw
    for f in fixes:
        prov = f.prov or "openwebui"
        if f.kind == "ctx":
            text = dsh_apply_ctx(text, prov, f.mid, f.ctx)
        elif f.kind == "remove":
            text = dsh_apply_remove(text, prov, f.mid)
    output = text.replace("\n", nl) if nl != "\n" else text
    if yaml is None:
        raise RuntimeError("PyYAML is required to validate DSH settings")
    yaml.safe_load(output)
    _atomic_replace_with_backup(path, output, path.with_suffix(".yaml.bak"))
    return True


# ---------------------------------------------------------------------------
# ZCode JSON adapter
# ---------------------------------------------------------------------------

# 受管 provider 的创建模板：B0（2026-11-01）起没有登记项——UM
# （local:openwebui）的 provider 由客户端既有配置提供，退役来源
# （local:qoder-global / local:cliproxy）的模板与受控 Key 读取器已删除。
# 未登记的 provider 由调用方明确拒绝，绝不写占位凭据。
ZCODE_MANAGED_PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {}


def zcode_managed_provider_template(provider: str) -> dict[str, Any] | None:
    """受管 provider 的创建模板（深拷贝）；未登记返回 None。

    模板可能含密钥，调用方不得回显；密钥值不进模块级状态，错误信息只报
    provider 名，不回显密钥。
    """
    template = ZCODE_MANAGED_PROVIDER_DEFAULTS.get(provider)
    if template is None:
        return None
    return deepcopy(template)


def zcode_models(
        c: dict[str, Any], provider: str = "local:openwebui") -> dict[str, Any]:
    return c["provider"][provider]["models"]


def zcode_new_entry(mid: str, ctx: int, priority: int,
                    efforts: list[str] | None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "limit": {"context": ctx, "output": 128000},
        "modalities": {"input": ["text"], "output": ["text"]},
        "zcode": {"modified": True, "priority": priority},
        "name": mid + UM_SUFFIX,
    }
    if efforts:
        entry["reasoning"] = {
            "enabled": True, "variants": efforts,
            "defaultVariant": KNOWN_MODELS[mid]["default"]}
    return entry


def _zcode_model_entry(change: SyncChange, priority: int) -> dict[str, Any]:
    model = change.model
    if model is None or change.model_id is None:
        raise ValueError("ZCode model change is missing model metadata")
    entry = deepcopy(change.previous_profile or {})
    entry["name"] = _expected_model_name(model, change.binding.suffix)
    limits = entry.setdefault("limit", {})
    if not change.preserve_context:
        # R6/D4：客户端上下文无法证明是我们发布的时不改写（保留现状，
        # 包括「客户端本来没有该字段」的现状）。
        if model.context is not None:
            limits["context"] = model.context
        limits.setdefault("context", 128000)
    limits.setdefault("output", 16384)
    entry.setdefault("modalities", {"input": ["text"], "output": ["text"]})
    entry.setdefault("zcode", {"modified": True, "priority": priority})
    if model.efforts:
        entry["reasoning"] = {
            "enabled": True,
            "variants": list(model.efforts),
            "defaultVariant": model.default_effort or model.efforts[0],
        }
    return entry


def render_zcode_sync(raw: str, changes: list[SyncChange]) -> str:
    """Render and validate all ZCode synchronization changes in memory."""
    config = json.loads(raw)
    priorities: dict[str, int] = {}
    for change in changes:
        providers = config.setdefault("provider", {})
        if change.binding.provider not in providers:
            # 受管但缺失的 provider 在这里创建（如本机 ZCode 尚无
            # local:openwebui）：模板经 zcode_managed_provider_template 解析，
            # 已存在的 provider 及其 options/手工模型一律不动。
            entry = zcode_managed_provider_template(change.binding.provider)
            if entry is None:
                raise KeyError(
                    f"missing ZCode provider: {change.binding.provider}")
            providers[change.binding.provider] = entry
        provider = providers[change.binding.provider]
        models = provider["models"]
        if change.kind == "provider_name":
            provider["name"] = change.binding.provider_name
            continue
        if change.kind == "remove":
            models.pop(change.model_id, None)
            continue
        if change.kind not in ("add", "update"):
            continue
        if change.binding.provider not in priorities:
            priorities[change.binding.provider] = min([
                item.get("zcode", {}).get("priority", 50)
                for item in models.values()
            ] or [50]) - 1
        priority = max(1, priorities[change.binding.provider])
        models[change.model_id] = _zcode_model_entry(change, priority)
        priorities[change.binding.provider] -= 1
    output = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    json.loads(output)
    return output


class ConfigPublishError(RuntimeError, OSError):
    """一次发布失败的结构化结果（客户端文件与本地状态不是跨文件事务）。

    逐文件替换 + 版本校验回退不是真正的跨文件事务：失败后必须区分
    「已发布」「已回退」「保留现状待核对」三类文件，绝不把部分发布
    说成整体成功，也绝不把并发写者的新内容盖回旧快照。

    同时是 :class:`RuntimeError` 与 :class:`OSError`：既有调用方按任一
    家族捕获（发布失败既是运行失败也是文件系统失败），行为兼容。

    字段：

    * ``failed_path``：替换失败的文件；None 表示失败发生在任何替换之前。
    * ``published``：替换成功、已落盘的文件（按替换顺序）。
    * ``rolled_back``：已发布但经版本校验确认仍是本次写入、并已恢复旧内容的文件。
    * ``left_in_place``：未回退的文件——并发写者已改写（版本校验不通过）或
      回退本身失败；其当前内容保持原样，待人工核对/重跑对账。
    * ``cause``：原始失败（OSError 等），供调用方按需上报。
    """

    def __init__(self, message: str, *, failed_path: Path | None = None,
                 published: Any = (), rolled_back: Any = (),
                 left_in_place: Any = (), cause: BaseException | None = None):
        super().__init__(message)
        self.failed_path = failed_path
        self.published = list(published)
        self.rolled_back = list(rolled_back)
        self.left_in_place = list(left_in_place)
        self.cause = cause


def _rollback_replaced(
        replaced: list[tuple[Path, bytes]],
        originals: dict[Path, bytes | None],
        ) -> tuple[list[Path], list[Path]]:
    """按版本校验回退已替换文件；返回 ``(已回退, 保留现状)``。

    回退前重读当前字节：只有仍等于本次写入内容的文件才恢复旧快照——
    并发写者改写过（版本校验不通过）或读不出来的文件保持现状，绝不覆盖。
    回退写入同样走有界重试；回退本身失败的文件同样保留现状、如实报告。
    """
    rolled_back: list[Path] = []
    left_in_place: list[Path] = []
    for path, written in reversed(replaced):
        try:
            current = path.read_bytes()
        except OSError:
            left_in_place.append(path)
            continue
        if current != written:
            # 并发写者已改写：旧快照绝不盖回去。
            left_in_place.append(path)
            continue
        original = originals.get(path)
        rollback_path = path.with_name(path.name + ".rollback.tmp")
        try:
            if original is None:
                path.unlink()
            else:
                rollback_path.write_bytes(original)
                replace_with_retry(rollback_path, path)
        except OSError:
            left_in_place.append(path)
            continue
        finally:
            if rollback_path.exists():
                rollback_path.unlink(missing_ok=True)
        rolled_back.append(path)
    return rolled_back, left_in_place


def _publish_failure_message(
        failed_path: Path | None, cause: BaseException,
        replaced: list[tuple[Path, bytes]], rolled_back: list[Path],
        left_in_place: list[Path]) -> str:
    """发布失败消息：只报文件名与三类结果，绝不回显文件内容。"""
    def names(paths: list[Path]) -> str:
        return "、".join(sorted({path.name for path in paths})) or "无"

    failed = failed_path.name if failed_path is not None else "（替换前）"
    return (
        "发布失败（%s：%s）：已替换 %s；已按版本校验回退 %s；保留现状待核对 %s；"
        "客户端文件与本地同步状态不是跨文件事务，需按上述恢复/对账路径处理"
        % (failed, type(cause).__name__,
           names([path for path, _ in replaced]),
           names(rolled_back), names(left_in_place)))


def apply_config_transaction(
        candidates: dict[Path, str], backup_root: Path,
        keep_backups: int = 30, timestamp: str | None = None) -> list[Path]:
    """逐个完整替换一批已验证文件；失败时按版本校验回退，绝不盖并发编辑。

    每个文件都是一次完整替换（同目录 temp + fsync + 原子 rename，Windows
    占用类错误有界重试）。客户端文件与本地同步状态**不是**真正的跨文件
    事务：这里只保证逐文件原子替换 + 失败补偿。补偿只回退仍等于本次写入
    内容的文件；已被并发写者改写或回退失败的文件保留现状，并以
    :class:`ConfigPublishError` 明确失败（已发布/已回退/保留现状三分类），
    绝不静默成功、绝不无限重试。
    """
    if not candidates:
        return []
    stamp = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_dir = backup_root / stamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    originals: dict[Path, bytes | None] = {}
    staged: dict[Path, Path] = {}
    replaced: list[tuple[Path, bytes]] = []
    current: Path | None = None
    try:
        for path, content in candidates.items():
            current = path
            path.parent.mkdir(parents=True, exist_ok=True)
            originals[path] = path.read_bytes() if path.exists() else None
            if originals[path] is not None:
                backup_path = backup_dir / (path.name + ".bak")
                backup_path.write_bytes(originals[path])
            with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", newline="", delete=False,
                    dir=path.parent, prefix=path.name + ".", suffix=".tmp") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                staged[path] = Path(handle.name)
        for path, temporary_path in staged.items():
            current = path
            replace_with_retry(temporary_path, path)
            replaced.append((path, candidates[path].encode("utf-8")))
        directories = sorted(
            [item for item in backup_root.iterdir() if item.is_dir()],
            key=lambda item: item.name, reverse=True)
        for old in directories[keep_backups:]:
            shutil.rmtree(old)
        return [path for path, _ in replaced]
    except Exception as exc:
        rolled_back, left_in_place = _rollback_replaced(replaced, originals)
        raise ConfigPublishError(
            _publish_failure_message(current, exc, replaced, rolled_back,
                                     left_in_place),
            failed_path=current, published=[path for path, _ in replaced],
            rolled_back=rolled_back, left_in_place=left_in_place,
            cause=exc) from exc
    finally:
        for temporary_path in staged.values():
            temporary_path.unlink(missing_ok=True)


def zcode_read(path: Path) -> dict[str, Any] | None:
    """Read and parse ZCode config.json, returning None on failure."""
    try:
        with io.open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def zcode_apply_fixes(path: Path, fixes: list[RepairItem]) -> bool:
    """Apply ZCode fixes atomically after validating the complete result."""
    with io.open(path, encoding="utf-8") as handle:
        c = json.load(handle)
    ms = zcode_models(c)
    prio = min([m.get("zcode", {}).get("priority", 50)
                for m in ms.values()] or [50]) - 1
    for f in fixes:
        if f.kind == "ctx":
            ms[f.mid]["limit"]["context"] = f.ctx
        elif f.kind == "name":
            name = ms[f.mid].get("name") or f.mid
            ms[f.mid]["name"] = name.rstrip() + UM_SUFFIX
        elif f.kind == "add":
            prio = max(1, prio)
            ms[f.mid] = zcode_new_entry(
                f.mid, f.ctx, prio,
                KNOWN_MODELS.get(f.mid, {}).get("efforts"))
            prio -= 1
        elif f.kind == "remove":
            ms.pop(f.mid, None)
    output = json.dumps(c, ensure_ascii=False, indent=2) + "\n"
    json.loads(output)
    _atomic_replace_with_backup(path, output, path.with_suffix(".json.bak"))
    return True


def zcode_running_state() -> str:
    """ZCode 运行状态：``running`` / ``not_running`` / ``unknown``（只读）。

    R28/R29 同族契约：**查询失败绝不等于「没在运行」**，只有确证才下结论。

    * tasklist 抛异常，或返回**非零退出码**（失败码不是结果）→ ``unknown``；
    * tasklist 成功且输出里出现 ZCode.exe 行（大小写不敏感）→ ``running``；
    * tasklist 成功但没有可识别的进程行（例如本地化的「没有匹配任务」提示）时，
      用**结构化进程枚举**确证：枚举证据完整且没有匹配进程 → ``not_running``；
      枚举不完整（快照/枚举失败、存在身份不可确认的进程）→ ``unknown``。

    只按结果文案排除特定错误串不足以确证，因此这里用枚举事实兜底；
    匹配用大小写不敏感比较（与本模块其它处同口径）。
    """
    import subprocess

    import zcode_hot_sync

    try:
        completed = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ZCode.exe", "/NH"],
            capture_output=True, text=True, timeout=10)
    except Exception:
        # 抛异常＝查询失败：不是「没在运行」，按运行中处理（fail closed）。
        return "unknown"
    if completed.returncode != 0:
        # 非零退出码＝查询失败：失败码不是结果，不能推出「未运行」。
        return "unknown"
    if "zcode.exe" in (completed.stdout or "").lower():
        return "running"
    # 成功但没有可识别的进程行（例如本地化的「没有匹配任务」提示）：用
    # 结构化进程枚举确证「无匹配进程」；枚举不完整一律 unknown。
    scan = zcode_hot_sync.zcode_process_scan()
    if scan.complete:
        return "running" if scan.paths else "not_running"
    return "unknown"


def zcode_running() -> bool:
    """检查 ZCode.exe 是否正在运行（fail closed：无法确证就按运行中处理）。

    只有确证「没在运行」才返回 False；``unknown``（tasklist 查询失败/结果
    不可识别且枚举无法确证）返回 True——运行门据此走延期而不是跳过准入。
    """
    return zcode_running_state() != "not_running"
