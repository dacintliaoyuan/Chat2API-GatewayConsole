# -*- coding: utf-8 -*-
"""账本「移除 10 行」的按内容身份重核对（只读，默认 dry-run）。

背景：`output/ai-gw-round6-20260927/ledger-10-rows-plan.md` 的 10 行清单是在
2026-09-27 10:57:46 的账本快照上按 **行号 + 行内 sha256** 记录的。账本此后
持续被运行中的 8000 追加（当前已 3100 万字节以上，快照是 2828 万），因此
**行号不是稳定标识**：任何一次删除都会让其后所有行号前移，而且计划里的行号只对
那一份快照成立。本工具一律**按内容身份定位**，行号只作为输出，不作为判据。

内容身份（可从计划 JSON 复算，不需要计划的行号）：

    content_sha256(record) = sha256(canonical_json(record))
    canonical_json = json.dumps(record, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False).encode("utf-8")

即**逐字段规范化**：键排序 + 紧凑分隔符 + 固定 UTF-8，因此与字段顺序、行内空白、
行尾 CRLF 都无关；值本身不做任何转换——值变了就是真的不一致。计划里的
**行内 sha256**（原始行字节，含行尾 ``\\r``、不含 ``\\n``）作为第二判据交叉核对：
内容哈希命中但行内 sha256 不符 = ``content_mismatch``（同一份记录、不同字节）。

本工具做四件事，**全部只读**：

1. 按内容哈希在当前账本里定位这 10 行，逐行输出
   当前行号 / 是否找到 / 是否唯一 / 与计划记录是否一致；
2. 核对备份是当前账本的**字节前缀**（逐字节比较长度与内容），输出
   备份行数 / 当前行数 / 差值；不是前缀即退出码 2；
3. 重算移除计划：当前行号、移除后总行数、以及「移除前后逐行比对」
   （除这 10 行外其余行必须逐字节相同）的判据，并用备份做锚定复核；
4. ``--authorized`` 时把上述结果写成可执行的移除清单（JSON），清单里带
   读取时点的 CAS 锚点（字节/mtime_ns/sha256）与移除后预期内容哈希。

**本工具绝不修改账本、绝不删除任何行、绝不覆盖备份。** 唯一可能的写动作是
``--authorized --out <path>`` 指定的那份清单（先写临时文件再受控替换），并且拒绝
写到账本、备份、计划文件上，也拒绝写到 ``ai-gateway/data/`` 目录下的任何路径。
默认（无 ``--authorized``）连这份清单也不写，且**不执行任何移除**。

退出码：

* 0 = 10 行全部唯一命中且与计划一致、备份是当前账本的前缀、移除计划重算判据 PASS；
* 2 = 任一判据不成立（未找到 / 多重命中 / 内容不符 / 备份非前缀 / 账本读不稳定 /
  计划不可读或形状不符 / 行内 sha256 在账本里出现解析失败），或要求写清单时重核未通过；
* 3 = 要求写清单但未给 ``--authorized``（默认拒绝写），或 ``--out`` 落在禁写路径上。

用法：

    python tools/um-share/actions/ledger_row_recheck.py
    python tools/um-share/actions/ledger_row_recheck.py --json
    # 写清单须显式授权；本轮不执行：
    python tools/um-share/actions/ledger_row_recheck.py --authorized --out <path>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

_UM_SHARE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _UM_SHARE_DIR not in sys.path:
    sys.path.insert(0, _UM_SHARE_DIR)
from lib.paths import legacy_repo_parent  # noqa: E402

# 旧布局的默认账本根是 ``parents[3]``（仓库根的上一级）；移动一层后
# 由 helper 返回同一目录，默认路径逐字不变。
REPO_ROOT = Path(legacy_repo_parent(__file__))
LEDGER_DEFAULT = REPO_ROOT / "ai-gateway" / "data" / "usage-chat2api.jsonl"
PLAN_DEFAULT = (REPO_ROOT / "output" / "ai-gw-round6-20260927"
                / "ledger-10-rows.json")
BACKUP_DEFAULT = (REPO_ROOT / "output" / "ai-gw-round6-20260927"
                  / "usage-chat2api.jsonl.bak-20260927-105746")
DATA_DIR = REPO_ROOT / "ai-gateway" / "data"

EXPECTED_TARGETS = 10
READ_ATTEMPTS = 5
COMPARE_CHUNK = 1 << 20

STATE_UNIQUE = "unique"
STATE_MISSING = "missing"
STATE_AMBIGUOUS = "ambiguous"
STATE_CONTENT_MISMATCH = "content_mismatch"

EXIT_OK = 0
EXIT_FAIL = 2
EXIT_REFUSED = 3


# --------------------------------------------------------------- 纯函数（可测）
def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_record_bytes(record: dict) -> bytes:
    """逐字段规范化：键排序 + 紧凑分隔符 + UTF-8（值本身不转换）。"""
    return json.dumps(record, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def content_sha256(record: dict) -> str:
    """内容身份哈希：同一份记录不论字段顺序/空白/行尾，哈希相同。"""
    return sha256_hex(canonical_record_bytes(record))


def split_lines(raw: bytes) -> list[bytes]:
    """按 ``\\n`` 切分；文件末尾换行会留下一个尾空元素（与计划脚本同口径）。"""
    return raw.split(b"\n")


def count_nonempty(lines) -> int:
    return sum(1 for payload in lines if payload.strip())


def line_end_offsets(lines) -> list[int]:
    """每一行（1 起）**末字节**在原文件中的偏移；第 n 行的值 = 前 n 个元素加 n-1 个
    ``\\n`` 的长度。用于判断某一行是否被备份的字节长度覆盖。"""
    ends: list[int] = []
    offset = 0
    for payload in lines:
        offset += len(payload)
        ends.append(offset)
        offset += 1
    return ends


def validate_plan(plan) -> list[str]:
    """计划 JSON 的形状检查；返回问题列表（空 = 可用）。"""
    problems: list[str] = []
    if not isinstance(plan, dict):
        return ["计划 JSON 的顶层不是对象"]
    rows = plan.get("rows")
    if not isinstance(rows, list) or not rows:
        return ["计划 JSON 里没有 rows 列表（或为空）"]
    for index, row in enumerate(rows, 1):
        where = f"rows[{index}]"
        if not isinstance(row, dict):
            problems.append(f"{where} 不是对象")
            continue
        if not isinstance(row.get("record"), dict):
            problems.append(f"{where}.record 不是对象")
        if not isinstance(row.get("line"), int):
            problems.append(f"{where}.line 不是整数")
        digest = row.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            problems.append(f"{where}.sha256 不是 64 位十六进制串")
    return problems


def build_content_index(lines) -> tuple[dict[str, list[int]], list[dict]]:
    """行号（1 起）→ 内容哈希 的反查表；解析失败的行如实记入 errors。"""
    index: dict[str, list[int]] = {}
    errors: list[dict] = []
    for lineno, payload in enumerate(lines, 1):
        if not payload.strip():
            continue
        try:
            record = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            errors.append({"line": lineno, "error": f"{type(exc).__name__}: {exc}"})
            continue
        index.setdefault(content_sha256(record), []).append(lineno)
    return index, errors


def locate_rows(plan_rows, index, lines) -> list[dict]:
    """按内容哈希定位计划里的每一行；四态：unique / missing / ambiguous / content_mismatch。"""
    located: list[dict] = []
    for order, plan_row in enumerate(plan_rows, 1):
        record = plan_row["record"]
        plan_line = plan_row["line"]
        plan_raw_sha = plan_row["sha256"]
        digest = content_sha256(record)
        hits = list(index.get(digest, []))
        raw_hits = [n for n in hits if sha256_hex(lines[n - 1]) == plan_raw_sha]
        record_hits = [n for n in hits
                       if json.loads(lines[n - 1].decode("utf-8")) == record]
        found = bool(hits)
        unique = len(hits) == 1
        if not found:
            state = STATE_MISSING
        elif not unique:
            state = STATE_AMBIGUOUS
        elif not raw_hits:
            state = STATE_CONTENT_MISMATCH
        else:
            state = STATE_UNIQUE
        located.append({
            "order": order,
            "plan_line": plan_line,
            "content_sha256": digest,
            "plan_raw_sha256": plan_raw_sha,
            "current_lines": hits,
            "current_line": hits[0] if unique else None,
            "found": found,
            "unique": unique,
            "record_matches_plan": bool(hits) and len(record_hits) == len(hits),
            "raw_sha256_matches_plan": bool(raw_hits),
            "raw_sha256_at_hits": [sha256_hex(lines[n - 1]) for n in hits],
            "line_number_matches_plan": hits == [plan_line],
            "plan_occurrences": plan_row.get("occurrences"),
            "state": state,
        })
    return located


def compare_prefix(prefix: bytes, current: bytes,
                    chunk_size: int = COMPARE_CHUNK) -> dict:
    """备份必须是当前账本的字节前缀：先比长度，再逐字节（分块，块内定位首处不同）。"""
    result = {
        "prefix_bytes": len(prefix),
        "current_bytes": len(current),
        "length_ok": len(prefix) <= len(current),
        "is_prefix": False,
        "first_diff_offset": None,
        "reason": None,
    }
    if not result["length_ok"]:
        result["reason"] = (f"备份 {len(prefix)} 字节 > 当前账本 {len(current)} 字节，"
                            f"不可能是前缀")
        return result
    offset = 0
    while offset < len(prefix):
        step = min(chunk_size, len(prefix) - offset)
        if prefix[offset:offset + step] != current[offset:offset + step]:
            for i in range(offset, offset + step):
                if prefix[i] != current[i]:
                    result["first_diff_offset"] = i
                    break
            result["reason"] = f"在偏移 {result['first_diff_offset']} 处首次不同"
            return result
        offset += step
    result["is_prefix"] = True
    return result


def check_backup(backup_raw: bytes, ledger_raw: bytes) -> dict:
    """前缀核对 + 行数/差值。备份为空也算前缀（前缀判据本身不因空而失效）。"""
    verdict = compare_prefix(backup_raw, ledger_raw)
    backup_lines = split_lines(backup_raw)
    current_lines = split_lines(ledger_raw)
    verdict["backup_sha256"] = sha256_hex(backup_raw)
    verdict["current_sha256"] = sha256_hex(ledger_raw)
    verdict["backup_bytes"] = len(backup_raw)
    verdict["current_bytes"] = len(ledger_raw)
    verdict["backup_lines_total"] = len(backup_lines)
    verdict["backup_lines_nonempty"] = count_nonempty(backup_lines)
    verdict["current_lines_total"] = len(current_lines)
    verdict["current_lines_nonempty"] = count_nonempty(current_lines)
    verdict["delta_lines_total"] = (verdict["current_lines_total"]
                                    - verdict["backup_lines_total"])
    verdict["delta_lines_nonempty"] = (verdict["current_lines_nonempty"]
                                       - verdict["backup_lines_nonempty"])
    verdict["backup_ends_with_newline"] = backup_raw.endswith(b"\n")
    verdict["current_ends_with_newline"] = ledger_raw.endswith(b"\n")
    verdict["ok"] = bool(verdict["is_prefix"])
    return verdict


def plan_removal(lines, drop_lines) -> dict:
    """按行号集合算「移除后内容」；只丢这些行，其余原样按序保留（不重排、不重写）。"""
    drop = sorted(set(drop_lines))
    drop_set = set(drop)
    keep = [payload for i, payload in enumerate(lines, 1) if i not in drop_set]
    return {
        "drop_lines": drop,
        "kept_lines_total": len(keep),
        "after_bytes": b"\n".join(keep),
    }


def verify_linewise_removal(before_lines, after_lines, dropped_lines) -> dict:
    """「移除前后逐行比对」判据：除 ``dropped_lines`` 外其余行必须逐字节相同。

    独立走一遍比对，而不是复用生成 after 的同一个列表推导：先算出**应当保留**的
    行号序列，再逐位比对 after 的每一行；行数差、多余行、缺失行、任一保留行不同
    都记为不通过。
    """
    dropped = sorted(set(dropped_lines))
    drop_set = set(dropped)
    expected_kept = [i for i in range(1, len(before_lines) + 1)
                     if i not in drop_set]
    result = {
        "dropped_lines": dropped,
        "before_lines_total": len(before_lines),
        "after_lines_total": len(after_lines),
        "expected_after_lines_total": len(expected_kept),
        "count_ok": len(after_lines) == len(expected_kept),
        "mismatches": [],
        "extra_lines": 0,
        "missing_lines": 0,
        "ok": False,
    }
    for k in range(min(len(after_lines), len(expected_kept))):
        expected = before_lines[expected_kept[k] - 1]
        if after_lines[k] != expected:
            result["mismatches"].append({
                "after_line": k + 1,
                "expected_before_line": expected_kept[k],
                "after_sha256": sha256_hex(after_lines[k]),
                "expected_sha256": sha256_hex(expected),
            })
    if len(after_lines) > len(expected_kept):
        result["extra_lines"] = len(after_lines) - len(expected_kept)
    else:
        result["missing_lines"] = len(expected_kept) - len(after_lines)
    result["ok"] = bool(result["count_ok"] and not result["mismatches"]
                       and result["extra_lines"] == 0
                       and result["missing_lines"] == 0)
    return result


def backup_anchored_removal(backup_raw: bytes, ledger_raw: bytes,
                            drop_lines) -> dict:
    """用备份（回滚材料）锚定移除集：移除后当前账本须以「移除后的备份」为前缀。

    这是非重言式的交叉核对：``drop_lines`` 来自当前账本的内容哈希定位，
    这里改从备份侧独立算一遍移除，要求两侧一致。判据用**字节偏移**而不是行数
    编号：某一行的**末字节偏移**必须落在备份的字节长度之内才算被备份覆盖，因此
    备份末尾换行留下的尾空元素不会被误算成一行。备份不是前缀时判为不可判定，
    绝不默认通过。
    """
    result = {
        "drop_lines_in_backup": [],
        "drop_lines_outside_backup": [],
        "expected_after_backup_sha256": None,
        "expected_after_current_sha256": None,
        "is_prefix_after_removal": False,
        "ok": False,
        "reason": None,
    }
    backup_lines = split_lines(backup_raw)
    current_lines = split_lines(ledger_raw)
    prefix = compare_prefix(backup_raw, ledger_raw)
    if not prefix["is_prefix"]:
        result["reason"] = "备份不是当前账本的前缀，无法锚定"
        return result
    ends = line_end_offsets(current_lines)
    inside = [n for n in sorted(set(drop_lines)) if ends[n - 1] <= len(backup_raw)]
    outside = [n for n in sorted(set(drop_lines))
               if ends[n - 1] > len(backup_raw)]
    result["drop_lines_in_backup"] = inside
    result["drop_lines_outside_backup"] = outside
    after_backup = plan_removal(backup_lines, inside)["after_bytes"]
    after_current = plan_removal(current_lines, drop_lines)["after_bytes"]
    result["expected_after_backup_sha256"] = sha256_hex(after_backup)
    result["expected_after_current_sha256"] = sha256_hex(after_current)
    result["is_prefix_after_removal"] = after_current[:len(after_backup)] == after_backup
    result["ok"] = bool(result["is_prefix_after_removal"])
    if not result["ok"]:
        result["reason"] = "移除后的当前账本不以「移除后的备份」为前缀"
    return result


def recheck(ledger_raw: bytes, plan, backup_raw: bytes,
            expect: int = EXPECTED_TARGETS) -> dict:
    """整份重核：定位 → 备份前缀 → 移除计划重算。纯函数，不做 IO、不写任何东西。"""
    plan_problems = validate_plan(plan)
    lines = split_lines(ledger_raw)
    index, parse_errors = build_content_index(lines)
    plan_rows = list(plan.get("rows") or []) if isinstance(plan, dict) else []
    located = (locate_rows(plan_rows, index, lines)
               if plan_rows and not plan_problems else [])

    states = Counter(row["state"] for row in located)
    targets_ok = (not plan_problems and len(located) == expect
                  and states.get(STATE_UNIQUE, 0) == expect)
    drop_lines = [row["current_line"] for row in located
                  if row["state"] == STATE_UNIQUE]

    backup = check_backup(backup_raw, ledger_raw)

    removal = None
    anchored = None
    if targets_ok:
        planned = plan_removal(lines, drop_lines)
        criterion = verify_linewise_removal(
            lines, split_lines(planned["after_bytes"]), drop_lines)
        removal = {
            "current_lines": drop_lines,
            "current_lines_total": len(lines),
            "current_lines_nonempty": count_nonempty(lines),
            "after_lines_total": len(split_lines(planned["after_bytes"])),
            "after_lines_nonempty": count_nonempty(split_lines(planned["after_bytes"])),
            "after_bytes": len(planned["after_bytes"]),
            "after_sha256": sha256_hex(planned["after_bytes"]),
            "dropped_count": len(drop_lines),
            "criterion": criterion,
            "criterion_note": ("判据由 plan_removal 生成的内容自校验，"
                               "不是对真实移除的观测；真实移除另有 CAS 守卫"),
        }
        if backup["ok"]:
            anchored = backup_anchored_removal(backup_raw, ledger_raw, drop_lines)

    line_number_warnings = [
        row for row in located if row["found"] and not row["line_number_matches_plan"]
    ]

    gate_failures: list[str] = []
    if plan_problems:
        gate_failures.append("计划形状不符：" + "；".join(plan_problems))
    if len(located) != expect:
        gate_failures.append(f"计划里的行数是 {len(located)}，期望 {expect}")
    for row in located:
        if row["state"] == STATE_MISSING:
            gate_failures.append(f"计划行 {row['plan_line']} 未找到")
        elif row["state"] == STATE_AMBIGUOUS:
            gate_failures.append(
                f"计划行 {row['plan_line']} 多重命中 {row['current_lines']}")
        elif row["state"] == STATE_CONTENT_MISMATCH:
            gate_failures.append(
                f"计划行 {row['plan_line']} 内容不符（当前行 "
                f"{row['current_line']} 行内 sha256 "
                f"{row['raw_sha256_at_hits'][0]}）")
    if parse_errors:
        gate_failures.append(
            "账本有解析失败的行：" + str([e["line"] for e in parse_errors]))
    if not backup["ok"]:
        gate_failures.append("备份不是当前账本的前缀：" + str(backup["reason"]))
    if removal is not None and not removal["criterion"]["ok"]:
        gate_failures.append("移除前后逐行比对判据不通过")
    if anchored is not None and not anchored["ok"]:
        gate_failures.append("移除后的当前账本不以「移除后的备份」为前缀")

    return {
        "plan_problems": plan_problems,
        "expect": expect,
        "states": {state: states.get(state, 0) for state in
                   (STATE_UNIQUE, STATE_MISSING, STATE_AMBIGUOUS,
                    STATE_CONTENT_MISMATCH)},
        "located": located,
        "targets_ok": targets_ok,
        "parse_errors": parse_errors,
        "backup": backup,
        "removal": removal,
        "anchored": anchored,
        "line_number_warnings": line_number_warnings,
        "gate_failures": gate_failures,
        "ok": not gate_failures,
    }


def build_manifest(result: dict, ledger_path: Path, plan_path: Path,
                    backup_path: Path, ledger_anchor: dict,
                    generated_at: str) -> dict:
    """可执行的移除清单：身份 + CAS 锚点 + 移除后预期内容。本清单不含任何行内容。"""
    removal = result.get("removal") or {}
    located = result.get("located") or []
    return {
        "tool": "ledger_row_recheck",
        "schema_version": 1,
        "generated_at": generated_at,
        "authorized": True,
        "ledger": str(ledger_path),
        "ledger_anchor": ledger_anchor,
        "plan": str(plan_path),
        "backup": str(backup_path),
        "backup_sha256": result["backup"]["backup_sha256"],
        "backup_is_prefix": result["backup"]["is_prefix"],
        "identity": "content_sha256(canonical_json(record))",
        "raw_identity": "sha256(raw line bytes, incl. trailing CR, excl. LF)",
        "drop_lines": removal.get("current_lines", []),
        "drop_content_sha256": [row["content_sha256"] for row in located
                               if row["state"] == STATE_UNIQUE],
        "drop_raw_sha256": [row["plan_raw_sha256"] for row in located
                            if row["state"] == STATE_UNIQUE],
        "plan_lines": [row["plan_line"] for row in located],
        "dropped_count": removal.get("dropped_count"),
        "before_lines_total": removal.get("current_lines_total"),
        "before_lines_nonempty": removal.get("current_lines_nonempty"),
        "after_lines_total": removal.get("after_lines_total"),
        "after_lines_nonempty": removal.get("after_lines_nonempty"),
        "after_bytes": removal.get("after_bytes"),
        "after_sha256": removal.get("after_sha256"),
        "criterion": removal.get("criterion"),
        "backup_anchored": result.get("anchored"),
        "read_only_note": ("本清单由只读重核产出；生成时未修改账本、未删除任何行、"
                           "未覆盖备份。执行移除须另行授权。"),
    }


# ------------------------------------------------------------------- IO 包装
def read_stable(path: Path, attempts: int = READ_ATTEMPTS):
    """读取 + 前后 stat 核对；账本正被追加时重试，返回 (bytes, stat, 尝试次数, 是否稳定)。"""
    raw = b""
    info = path.stat()
    for attempt in range(1, attempts + 1):
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
        info = after
        if (after.st_size == before.st_size
                and after.st_mtime_ns == before.st_mtime_ns
                and len(raw) == after.st_size):
            return raw, after, attempt, True
    return raw, info, attempts, False


def write_manifest_atomic(path: Path, payload: dict) -> None:
    """先写同目录临时文件再受控替换；只写这一份清单。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_name(path.name + ".recheck.tmp")
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    if temporary.read_text(encoding="utf-8") != text:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("清单临时文件读回不一致，中止")
    os.replace(temporary, path)


def guard_out_path(out: Path, ledger: Path, plan: Path, backup: Path) -> str | None:
    """禁写路径：账本、备份、计划本身，以及 ``ai-gateway/data/`` 下任何路径。"""
    resolved = out.resolve()
    for label, target in (("账本", ledger), ("备份", backup), ("计划", plan)):
        if resolved == target.resolve():
            return f"--out 指向{label}路径，拒绝写入"
    data_dir = DATA_DIR.resolve()
    if resolved == data_dir or data_dir in resolved.parents:
        return "--out 落在 ai-gateway/data/ 下，拒绝写入"
    return None


# --------------------------------------------------------------------- 打印
def format_report(result: dict, ledger: Path, plan_path: Path, backup_path: Path,
                   ledger_anchor: dict, plan_meta: dict) -> str:
    out: list[str] = []
    add = out.append
    add("账本重核对（只读，默认 dry-run）  工具 ledger_row_recheck")
    add(f"账本 {ledger}")
    add(f"  字节 {ledger_anchor['bytes']}  sha256 {ledger_anchor['sha256']}")
    add(f"  mtime_ns {ledger_anchor['mtime_ns']}  "
        f"读取 {ledger_anchor['read_attempts']} 次"
        f"（{'稳定' if ledger_anchor['read_stable'] else '不稳定'}）")
    add(f"  行数 {ledger_anchor['lines_total']}"
        f"（非空 {ledger_anchor['lines_nonempty']}；"
        f"末元素为末尾换行后的空串）")
    add(f"计划 {plan_path}")
    add(f"  记录于 {plan_meta.get('measured_at')}  "
        f"计划里的行数 {plan_meta.get('row_count')}  期望 {result['expect']}")
    if result["plan_problems"]:
        add("  [FAIL] 计划形状：" + "；".join(result["plan_problems"]))

    add("")
    add("按内容哈希定位（逐字段规范化 sha256；行号只作输出，不作判据）")
    add("  #  计划行   当前行        找到 唯一 记录一致 行内sha256一致 状态")
    for row in result["located"]:
        if not row["found"]:
            current = "-"
        elif row["unique"]:
            current = str(row["current_line"])
        else:
            current = str(row["current_lines"])
        add(f"  {row['order']:>2}  {row['plan_line']:>7}  {current:<12}  "
            f"{'是' if row['found'] else '否'}   "
            f"{'是' if row['unique'] else '否'}   "
            f"{'是' if row['record_matches_plan'] else '否'}       "
            f"{'是' if row['raw_sha256_matches_plan'] else '否'}            "
            f"{row['state']}")
    states = result["states"]
    add(f"  汇总：unique {states[STATE_UNIQUE]} / missing {states[STATE_MISSING]} / "
        f"ambiguous {states[STATE_AMBIGUOUS]} / "
        f"content_mismatch {states[STATE_CONTENT_MISMATCH]}")
    if result["parse_errors"]:
        add("  [FAIL] 账本解析失败的行：" + str([e["line"] for e in result["parse_errors"]]))
    for row in result["line_number_warnings"]:
        add(f"  [WARN] 计划行 {row['plan_line']} 的当前行号 "
            f"{row['current_lines']} 与计划记录不一致（仅提示，不作判据）")

    backup = result["backup"]
    add("")
    add("备份前缀核对")
    add(f"备份 {backup_path}")
    add(f"  字节 {backup['backup_bytes']} <= 账本 {backup['current_bytes']}"
        f"  sha256 {backup['backup_sha256']}")
    add(f"  逐字节前缀：{'是' if backup['is_prefix'] else '否'}"
        f"（首处不同：{backup['first_diff_offset']}；{backup['reason'] or '无'}）")
    add(f"  行数：备份非空 {backup['backup_lines_nonempty']}"
        f" / 当前非空 {backup['current_lines_nonempty']}"
        f"  差值 +{backup['delta_lines_nonempty']}"
        f"（含末空元素：备份 {backup['backup_lines_total']}"
        f" / 当前 {backup['current_lines_total']}"
        f"  差值 +{backup['delta_lines_total']}）")

    removal = result["removal"]
    add("")
    add("移除计划重算（本工具不执行移除）")
    if removal is None:
        add("  [FAIL] 10 行未能全部唯一命中，无法重算移除计划")
    else:
        add(f"  当前行号（按内容哈希定位，升序）：{removal['current_lines']}")
        add(f"  移除后行数：非空 {removal['after_lines_nonempty']}"
            f"（当前 {removal['current_lines_nonempty']} - "
            f"{removal['dropped_count']}）；"
            f"含末空元素 {removal['after_lines_total']}")
        add(f"  移除后字节 {removal['after_bytes']}  "
            f"sha256 {removal['after_sha256']}")
        criterion = removal["criterion"]
        add(f"  移除前后逐行比对判据："
            f"{'PASS' if criterion['ok'] else 'FAIL'}"
            f"（除这 {removal['dropped_count']} 行外其余 "
            f"{criterion['expected_after_lines_total']} 行逐字节相同；"
            f"多余 {criterion['extra_lines']} / 缺失 {criterion['missing_lines']}"
            f" / 不同 {len(criterion['mismatches'])}）")
        add(f"  注：{removal['criterion_note']}")
    anchored = result["anchored"]
    if anchored is None:
        add("  备份锚定复核：不可判定（备份非前缀或 10 行未全部唯一命中）")
    else:
        add(f"  备份锚定复核：{'PASS' if anchored['ok'] else 'FAIL'}"
            f"（移除后的当前账本以「移除后的备份」为前缀："
            f"{'是' if anchored['is_prefix_after_removal'] else '否'}；"
            f"备份内移除行 {anchored['drop_lines_in_backup']}"
            f"，备份外 {anchored['drop_lines_outside_backup']}）")
        if anchored["reason"]:
            add(f"    [FAIL] {anchored['reason']}")

    add("")
    if result["ok"]:
        add("结论：10 行按内容身份唯一命中且与计划一致；备份是当前账本的字节前缀；"
            "移除计划重算判据 PASS。退出码 0。")
        add("本工具未修改账本、未删除任何行、未覆盖备份；执行移除须另行授权。")
    else:
        add("结论：判据不成立，退出码 2。逐项：")
        for failure in result["gate_failures"]:
            add(f"  [FAIL] {failure}")
        add("本工具未修改账本、未删除任何行、未覆盖备份。")
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="账本「移除 10 行」的按内容身份重核对（只读，默认 dry-run）")
    parser.add_argument("--ledger", default=str(LEDGER_DEFAULT),
                        help="账本路径（默认生产账本，只读）")
    parser.add_argument("--plan", default=str(PLAN_DEFAULT),
                        help="计划 JSON（10 行的内容身份来源）")
    parser.add_argument("--backup", default=str(BACKUP_DEFAULT),
                        help="备份路径（必须为账本的字节前缀）")
    parser.add_argument("--expect", type=int, default=EXPECTED_TARGETS,
                        help=f"期望的计划行数（默认 {EXPECTED_TARGETS}）")
    parser.add_argument("--json", action="store_true", help="打印机器可读 JSON")
    parser.add_argument("--authorized", action="store_true",
                        help="显式声明已获授权写移除清单（默认不写任何文件）")
    parser.add_argument("--out", default=None,
                        help="移除清单写出路径（须同时给 --authorized）")
    args = parser.parse_args(argv)

    ledger_path = Path(args.ledger)
    plan_path = Path(args.plan)
    backup_path = Path(args.backup)

    if args.out and not args.authorized:
        print("拒绝写入：写出可执行的移除清单须显式授权。确认后加 --authorized。",
              file=sys.stderr)
        return EXIT_REFUSED
    if args.out:
        refusal = guard_out_path(Path(args.out), ledger_path, plan_path, backup_path)
        if refusal:
            print(f"拒绝写入：{refusal}。", file=sys.stderr)
            return EXIT_REFUSED

    if not ledger_path.is_file():
        print(f"账本不存在：{ledger_path}", file=sys.stderr)
        return EXIT_FAIL
    if not backup_path.is_file():
        print(f"备份不存在：{backup_path}", file=sys.stderr)
        return EXIT_FAIL
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"计划不可读：{plan_path}（{type(exc).__name__}: {exc}）", file=sys.stderr)
        return EXIT_FAIL

    ledger_raw, info, attempts, stable = read_stable(ledger_path)
    ledger_anchor = {
        "path": str(ledger_path),
        "bytes": len(ledger_raw),
        "sha256": sha256_hex(ledger_raw),
        "mtime_ns": info.st_mtime_ns,
        "read_attempts": attempts,
        "read_stable": stable,
        "lines_total": len(split_lines(ledger_raw)),
        "lines_nonempty": count_nonempty(split_lines(ledger_raw)),
    }
    backup_raw = backup_path.read_bytes()
    plan_meta = {
        "measured_at": plan.get("measured_at") if isinstance(plan, dict) else None,
        "row_count": len(plan.get("rows") or []) if isinstance(plan, dict) else 0,
    }

    result = recheck(ledger_raw, plan, backup_raw, expect=args.expect)
    if not stable:
        result["gate_failures"].append(
            f"账本在读取期间持续被追加（{attempts} 次尝试均不稳定）")
        result["ok"] = False
    result["ledger_anchor"] = ledger_anchor
    result["plan_meta"] = plan_meta

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_report(result, ledger_path, plan_path, backup_path,
                           ledger_anchor, plan_meta))

    if args.out:
        if not result["ok"]:
            print("拒绝写入：重核未通过，不产出「可执行的移除清单」。"
                  "未做任何修改。", file=sys.stderr)
            return EXIT_FAIL
        manifest = build_manifest(result, ledger_path, plan_path, backup_path,
                                 ledger_anchor,
                                 time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        try:
            write_manifest_atomic(Path(args.out), manifest)
        except OSError as exc:
            print(f"清单写出失败：{args.out}（{type(exc).__name__}: {exc}）",
                  file=sys.stderr)
            return EXIT_FAIL
        print(f"\n已写出移除清单（未执行任何移除）：{args.out}")

    return EXIT_OK if result["ok"] else EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
