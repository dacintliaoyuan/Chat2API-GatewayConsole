# -*- coding: utf-8 -*-
"""窗口一（只载入 8096）的只读前置核对器：一条命令汇总前置判据，逐项打 PASS/FAIL/UNDECIDABLE。

维护者 2026-09-28 的窗口一要求：进入 8096 受控重启窗口之前，先确认**在途操作为零、
回退副本与当前运行版本匹配**，并记录分享链各层的读数（8000/8096/8100/8110 的监听 PID、
8096 读面键集、Funnel、入口白名单外 404 与白名单内无凭据 401、8110 账本末行 attribution、
watchdog 任务、WFP 限制）。

十项判据（每项独立读来源；第 5/6 项读同一份 8096 状态响应的不同字段，已在读数里注明同源）：

1. ``operations_in_flight`` —— ``GET http://127.0.0.1:8096/api/console/operations?limit=50``
   的 ``queued/running`` 必须为 0（动作门 §1.1）。读不到 = UNDECIDABLE。
2. ``rollback_copies`` —— 三份 ``output/ai-gw-round6-20260927/rollback-*.py`` 的字节数与
   sha256 必须与动作门 §1.4 表逐项相符（§1.4 判据原文：「两列 sha256 与上表逐一相符」）。
   文件不存在 = FAIL（确定事实）；读不到（权限等）= UNDECIDABLE（读不到 ≠ 不存在）。
3. ``load_targets_head`` —— 三份「将载入版本」必须同时满足：
   (a) 工作区 = HEAD（逐字节比较 git blob，`git show HEAD:<repo 相对路径>`）；
   (b) sha256/字节数与动作门 §1.4 表「将载入版本（HEAD）」列相符。
   工作区 ≠ HEAD 记 ``workspace_differs_from_head``；表不符记 ``gate_table_stale``
   （表已过期：先更新 §1.4 表或由维护者确认按当前 HEAD 载入，再重跑本工具）——两者都是
   确定事实，不是「读不到」；git 跑不起来/HEAD blob 读不到 = UNDECIDABLE。
4. ``listeners`` —— 8000/8096/8100/8110 的 LISTENING PID（每口必须恰一个）。
   无人监听 = FAIL（掉线）；多个 = FAIL（PID 不唯一）；netstat 读不到 = UNDECIDABLE。
5. ``read_surface`` —— 8096 读面 ``attribution`` 键集：4 键旧形状（新代码未载入）与
   7 键新形状（新代码已载入）都是**已知形状**（如实记录是哪一种）；既不是 4 键也不是
   7 键 = FAIL（运行版本不是预期的两版之一）；读不到 = UNDECIDABLE。
6. ``funnel`` —— 同一份读面响应的 ``funnel_on`` 字段（同源、不同字段；读不到 = UNDECIDABLE）。
7. ``share_entry_gates`` —— 8100 白名单**外**的 ``GET /``、``GET /health`` 须 404
   ``route_not_shared``；白名单**内**无凭据的 ``GET /v1/models`` 须 401 ``invalid_api_key``。
   命中期望 = PASS；可读但门不符 = FAIL；任一条探测失败 = UNDECIDABLE。
8. ``share_ledger_tail`` —— 8110 分享账本行数与末行 ``time/model/key_id/attribution``
   （账本路径由 ``share_chain_snapshot.backend_observe_facts(forget_stale=False)`` 解析，
   **不猜路径**）；读不到 = UNDECIDABLE；可读但 0 行 = FAIL（记录不到末行）。
9. ``watchdog_task`` —— ``umshare-watchdog`` 计划任务状态：读提升身份只读探针
   ``authoritative_readings.ps1``（默认只打印 stdout，**不给 -Out**，不写任何文件）的
   ``window_signals.watchdog_task``，再用本目录 ``share_chain_snapshot.parse_watchdog_query``
   + ``watchdog_task_signal`` 从原始信封**重算**；探针结论为 ``ok`` 而信封缺失
   （重算输入读不到）记 UNDECIDABLE（``probe_rederivation_envelope_missing``），
   重算与探针结论不一致记 UNDECIDABLE（``probe_rederivation_mismatch``）。
   探针跑不起来/条目形状不符 = UNDECIDABLE。
10. ``wfp_limits`` —— 同一探针的 ``window_signals.wfp_limits``（WFP strong + 防火墙规则）；
   本项只有这一层探针读数，**没有**交叉校验（第 9 项才有信封重算）。

第 9/10 项在非提升身份下读不到：**一律 UNDECIDABLE，绝不折成「未注册／不在位」**
（动作门 §1.6）。只有提升身份下权威读到「未注册」（``absent``）或「WFP strong 未通过／
出站阻断规则不在位」（``absent``）才是 FAIL。

退出码（对维护者两处措辞的收敛口径）：**0 = 十项全部 PASS**；**2 = 任一 FAIL 或
UNDECIDABLE**——``UNDECIDABLE`` 是允许出现的判定值（工具照常给出完整读数并逐项列出），
但它不是通过：**读不到 ≠ 通过**，全 PASS 才进窗口；出现 UNDECIDABLE 先补证据（提升身份
重跑本工具），出现 FAIL 才按对应层回退或恢复。默认 dry-run **不读任何现场**，退出码 0
（dry-run 不是判定）。

只读边界：HTTP 只发**不带凭据**的 ``GET http://127.0.0.1:<本机端口>``；进程只跑
``netstat -ano -p TCP``；账本只读打开；委托观察只走
``share_chain_snapshot.backend_observe_facts(forget_stale=False)``（**不删**陈旧
``backend.pid``）与提升身份只读探针（默认只打印 stdout，不给 ``-Out``）。本工具默认
**自己不写任何文件、不重启任何服务、不生成密钥、不创建或撤销分享 Key、不发模型请求、不改
``ai-gateway/data/``、不改前端 ``web/``、不碰私人 8000**（只读它的监听 PID）。绝不打印
任何密钥内容或分享 Key 明文：账本只打印 ``time/model/key_id/attribution`` 字段，读面只给
路径、原因码与 ``strip()`` 后的长度。

唯一写路径（``--refresh-gate-table``，显式开关，默认关闭）：第 3 项判据报
``gate_table_stale`` 时，可从工作区实测值重写**两份且仅两份**文件——动作门 §1.4 表
（``output/ai-gw-round6-20260927/metering-action-gate.md`` 的三行「将载入版本（HEAD）」）
与本工具的 ``LOAD_TARGET_TABLE`` 常量。写前逐条字面量替换（每条必须恰好命中一次，
格式变化即拒绝写、绝不半匹配）；写后用临时文件 + ``os.replace`` 受控替换，再读回
sha256 并打印前后哈希。``--check`` 本身仍只读；该开关只在这一条提示里出现，
不在 dry-run 计划里提示。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import locale
import os
import re
import subprocess
import sys
import time

# 同目录的 round7/round9 只读快照工具：复用它的公开纯解析/观察函数与期望值，
# 避免两处口径漂移（与 post_restart_acceptance.py:91-97 同一做法）。
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
AI_GATEWAY_DIR = os.path.dirname(os.path.dirname(TOOLS_DIR))
PROJECT_ROOT = os.path.dirname(AI_GATEWAY_DIR)
for _path in (TOOLS_DIR, AI_GATEWAY_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import share_chain_snapshot as snapshot_tool  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
UNDECIDABLE = "UNDECIDABLE"

CONSOLE_PORT = snapshot_tool.CONSOLE_PORT      # 8096 控制台网页宿主
GATEWAY_PORT = snapshot_tool.GATEWAY_PORT       # 8100 分享入口
BACKEND_PORT = snapshot_tool.BACKEND_PORT       # 8110 分享专用后端
PRIVATE_PORT = 8000                             # 私人 8000（只记录 PID，不动它）

# 窗口一记录这四个口的监听 PID（动作门 §1.2 表）。
LISTEN_PORTS = (PRIVATE_PORT, CONSOLE_PORT, GATEWAY_PORT, BACKEND_PORT)

OPERATIONS_PATH = "/api/console/operations?limit=50"
STATUS_PATH = "/api/console/share-management/status"

PROBE_TIMEOUT_S = snapshot_tool.PROBE_TIMEOUT_S
RUNNER_TIMEOUT_S = snapshot_tool.RUNNER_TIMEOUT_S
# 提升身份探针内部要 dot-source 看门狗脚本并跑 wfp_helper -Action strong，
# 给足超时；超时即「读不到」→ UNDECIDABLE。
READINGS_TIMEOUT_S = 180.0

# 提升身份只读探针（同目录；本工具只读它的 stdout，不给 -Out）。
READINGS_TOOL = snapshot_tool.AUTHORITATIVE_READINGS_TOOL
READINGS_TOOL_PATH = os.path.join(TOOLS_DIR, READINGS_TOOL + ".ps1")
WATCHDOG_SIGNAL_NAMES = ("watchdog_task", "wfp_limits")

GATE_TABLE_REL = "output/ai-gw-round6-20260927/metering-action-gate.md"
GATE_TABLE_PATH = os.path.normpath(os.path.join(PROJECT_ROOT, GATE_TABLE_REL))
GATE_SOURCE = GATE_TABLE_REL + " §1.4"
# 本工具源码路径：``--refresh-gate-table`` 的第二个写入对象（LOAD_TARGET_TABLE 常量）。
TOOL_PATH = os.path.abspath(__file__)

# 动作门 §1.4 表逐字：回退副本（本项判据）。
ROLLBACK_COPY_TABLE = (
    ("rollback-console_share_production.py",
     "output/ai-gw-round6-20260927/rollback-console_share_production.py",
     79626,
     "5f5c0eda21f7e9b130288ff111fe4ea329867802edfc593121e70707f03d4f68"),
    ("rollback-usagesrv.py",
     "output/ai-gw-round6-20260927/rollback-usagesrv.py",
     161103,
     "bcc47fd198d5210c70e6ddd5de2b07f8c12ca54aa90e26e7a8e6bc192fdf15d7"),
    ("rollback-console_share_actions.py",
     "output/ai-gw-round6-20260927/rollback-console_share_actions.py",
     57520,
     "06424f8603bd9db0971b5155544c0be7203dbf5ad88fd4398ee7690d4596961f"),
)

# 动作门 §1.4 表逐字：将载入版本（HEAD）列（本项判据的第二条；表随 HEAD 前进会过期）。
LOAD_TARGET_TABLE = (
    # 2026-09-28 更新（执行轮 11 前置核对器报出漂移）：执行轮 10 的只读开关
    # `forget_stale` 与 `_pid_alive` 修复、执行轮 11 的行号变动把这份「将载入版本」
    # 从 94523/266dc3d0… 变成 99133/512710e7…。本常量与动作门 §1.4 表**必须一起改**：
    # 它就是这个前置门用来发现「表已过期」的那把尺子。
    ("console_share_production.py",
     "ai-gateway/console_share_production.py",
     99133,
     "2e5a6892fdc1bca4deecc5bb87f0e08b136e21d3c56ea723fa8779c57c918d24"),
    ("usagesrv.py",
     "ai-gateway/usagesrv.py",
     161584,
     "1345a4f08c1b8009e47cf8ad7af5cd129b0fde9f7de16d6e15c74893bf9af8c9"),
    ("console_share_actions.py",
     "ai-gateway/console_share_actions.py",
     58050,
     "54a681d5d40cc268aa00ed5582ec0261ba1722347c43ee8c90d3b9b5a6930029"),
)

VERDICT_RANK = {PASS: 0, UNDECIDABLE: 1, FAIL: 2}


def _worse(left, right):
    """取两个判定里更坏的一个（FAIL > UNDECIDABLE > PASS）。"""
    return left if VERDICT_RANK[left] >= VERDICT_RANK[right] else right


# --------------------------------------------------------------------- IO 层
def _run_capture(args, timeout=RUNNER_TIMEOUT_S):
    """只读外部命令：``(returncode, stdout_bytes, error)``；启动失败返回 ``(None, None, reason)``。"""
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, None, "spawn_failed:%s" % type(exc).__name__
    return proc.returncode, proc.stdout, None


def decode_json_output(raw):
    """纯函数：外部命令 stdout 字节 → ``(payload, encoding, error)``。

    候选编码顺序 UTF-8 → 本机代码页（``mbcs``）→ ``locale``；**每种编码都要求解码与
    JSON 解析同时成功**，全部失败才记 ``unparsable``（与
    ``share_chain_snapshot.load_authoritative_file`` 同口径；PowerShell 5.1 的 stdout
    被 cmd 重定向时写的是控制台代码页，本机实测中文为 GBK）。
    """
    if not isinstance(raw, (bytes, bytearray)):
        return None, None, "not_bytes"
    candidates = ["utf-8"]
    if os.name == "nt":
        candidates.append("mbcs")
    candidates.append(locale.getpreferredencoding(False))
    tried = []
    for encoding in candidates:
        try:
            text = raw.decode(encoding)
        except (UnicodeError, LookupError):
            continue
        tried.append(encoding)
        try:
            return json.loads(text), encoding, None
        except ValueError:
            continue
    return None, None, "unparsable:%s" % ",".join(tried)


def read_file_facts(path):
    """只读一份文件的字节事实：``{path, exists, size, sha256, error, error_kind}``。

    区分「不存在」（``FileNotFoundError`` → ``exists=False``）与「读不到」
    （``PermissionError`` 等 ``OSError`` → ``error_kind='unreadable'``）：读不到 ≠ 不存在。
    """
    out = {"path": path, "exists": False, "size": None, "sha256": None,
           "error": None, "error_kind": None}
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        out["error"] = "FileNotFoundError"
        out["error_kind"] = "missing"
        return out
    except OSError as exc:
        out["error"] = type(exc).__name__
        out["error_kind"] = "unreadable"
        return out
    out["exists"] = True
    out["size"] = len(data)
    out["sha256"] = hashlib.sha256(data).hexdigest()
    return out


def git_repo_root():
    """只读解析仓库根：``(root, error)``；解析不出返回 ``(None, reason)``。"""
    rc, stdout, error = _run_capture(
        ["git", "-C", PROJECT_ROOT, "rev-parse", "--show-toplevel"])
    if error:
        return None, "git_" + error
    if rc != 0:
        return None, "git_rev_parse_failed:rc=%s" % rc
    root = stdout.decode("utf-8", errors="replace").strip()
    if not root:
        return None, "git_toplevel_empty"
    return os.path.normpath(root), None


def git_head_blob_facts(repo_root, path):
    """只读取 HEAD 里同名文件的字节事实（``git show HEAD:<repo 相对路径>``）。"""
    out = {"sha256": None, "size": None, "error": None, "repo_rel": None}
    repo_rel = os.path.relpath(os.path.abspath(path), repo_root)
    repo_rel = repo_rel.replace(os.sep, "/")
    out["repo_rel"] = repo_rel
    rc, stdout, error = _run_capture(
        ["git", "-C", repo_root, "show", "HEAD:" + repo_rel])
    if error:
        out["error"] = "git_" + error
        return out
    if rc != 0:
        out["error"] = "head_blob_unavailable:rc=%s" % rc
        return out
    out["size"] = len(stdout)
    out["sha256"] = hashlib.sha256(stdout).hexdigest()
    return out


def git_diff_clean(repo_root, path):
    """只读 ``git diff --name-status HEAD -- <path>``：``{clean, error, output}``。

    与 blob 逐字节比较互补（覆盖文件模式等 blob 之外的差异）：空输出 = 无差异。
    """
    out = {"clean": None, "error": None, "output": None}
    repo_rel = os.path.relpath(os.path.abspath(path), repo_root)
    repo_rel = repo_rel.replace(os.sep, "/")
    rc, stdout, error = _run_capture(
        ["git", "-C", repo_root, "diff", "--name-status", "HEAD", "--",
         repo_rel])
    if error:
        out["error"] = "git_" + error
        return out
    if rc != 0:
        out["error"] = "git_diff_failed:rc=%s" % rc
        return out
    text = stdout.decode("utf-8", errors="replace")
    out["output"] = text
    out["clean"] = text.strip() == ""
    return out


def collect_operations(get=None):
    """只读 ``GET /api/console/operations`` → ``snapshot_tool.operations_summary``。"""
    get = get or snapshot_tool.http_get_json
    return snapshot_tool.operations_summary(get(CONSOLE_PORT, OPERATIONS_PATH))


def collect_rollback_facts():
    """只读三份回退副本的字节事实（含 §1.4 表期望值）。"""
    rows = []
    for label, rel, want_bytes, want_sha in ROLLBACK_COPY_TABLE:
        path = os.path.normpath(os.path.join(PROJECT_ROOT, rel))
        facts = read_file_facts(path)
        facts["label"] = label
        facts["expected_size"] = want_bytes
        facts["expected_sha256"] = want_sha
        rows.append(facts)
    return rows


def collect_load_target_facts():
    """只读三份「将载入版本」的工作区事实、HEAD blob 事实与 git diff 干净度。"""
    repo_root, repo_error = git_repo_root()
    rows = []
    for label, rel, want_bytes, want_sha in LOAD_TARGET_TABLE:
        path = os.path.normpath(os.path.join(PROJECT_ROOT, rel))
        row = {"label": label, "path": path,
                "table": {"bytes": want_bytes, "sha256": want_sha},
                "workspace": read_file_facts(path),
                "repo_root": repo_root, "repo_error": repo_error,
                "head": {"sha256": None, "size": None, "error": None},
                "diff": {"clean": None, "error": None}}
        if repo_error is None:
            row["head"] = git_head_blob_facts(repo_root, path)
            row["diff"] = git_diff_clean(repo_root, path)
        else:
            row["head"]["error"] = repo_error
            row["diff"]["error"] = repo_error
        rows.append(row)
    return rows


# ------------------------------------- 门表刷新（显式开关；本工具唯一写路径）
def _sha256_file(path):
    """只读一份文件的 sha256：``(sha256, error)``；读不到即报原因码。"""
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest(), None
    except OSError as exc:
        return None, "%s:%s" % (type(exc).__name__, exc)


def _measured_rows(collect=None):
    """三份将载入版本的工作区实测值 → ``(rows, error)``。

    任一份缺失／读不到／事实不全即拒绝（``error`` 非空），绝不半份写入。
    """
    rows = []
    read_facts = collect or read_file_facts
    for label, rel, _want_bytes, _want_sha in LOAD_TARGET_TABLE:
        path = os.path.normpath(os.path.join(PROJECT_ROOT, rel))
        facts = read_facts(path)
        if not isinstance(facts, dict):
            return None, "workspace_facts_missing:%s" % label
        if facts.get("error_kind") == "missing":
            return None, "workspace_missing:%s" % label
        if facts.get("error_kind") == "unreadable" or not facts.get("exists"):
            return None, "workspace_unreadable:%s:%s" % (label,
                                                         facts.get("error"))
        if not isinstance(facts.get("size"), int) \
                or isinstance(facts.get("size"), bool) \
                or not isinstance(facts.get("sha256"), str) \
                or len(facts.get("sha256")) != 64:
            return None, "workspace_facts_incomplete:%s" % label
        rows.append((label, rel, facts["size"], facts["sha256"]))
    return rows, None


def _rewrite_tool_source(text, rows):
    """纯函数：源码文本里逐条替换三份将载入版本的 (bytes, sha256) 字面量。

    每条必须**恰好命中一次**（格式变化即拒绝写入，绝不半匹配），返回
    ``(text, error)``；注释与其余内容一律不动。
    """
    out = text
    for label, rel, size, sha in rows:
        pattern = re.compile(
            r'(\("%s",\s*\n\s*"%s",\s*\n\s*)\d+,(\s*\n\s*")[0-9a-f]{64}(")'
            % (re.escape(label), re.escape(rel)))
        out, count = pattern.subn(
            lambda m: "%s%d,%s%s%s" % (m.group(1), size, m.group(2), sha,
                                        m.group(3)), out)
        if count != 1:
            return None, "tool_row_not_unique:%s:%s" % (label, count)
    return out, None


def _rewrite_gate_source(text, rows):
    """纯函数：§1.4 表里逐条替换三份将载入版本行的 (bytes, sha256) 单元格。

    每条必须**恰好命中一次**（格式变化即拒绝写入，绝不半匹配），返回
    ``(text, error)``。
    """
    out = text
    for label, rel, size, sha in rows:
        pattern = re.compile(
            r"(\| 将载入版本（HEAD） \| `%s` \| )\d+( \| `)[0-9a-f]{64}(` \|)"
            % re.escape(rel))
        out, count = pattern.subn(
            lambda m: "%s%d%s%s%s" % (m.group(1), size, m.group(2), sha,
                                      m.group(3)), out)
        if count != 1:
            return None, "gate_row_not_unique:%s:%s" % (label, count)
    return out, None


def _write_file_atomic(path, data):
    """受控替换写：先写同目录临时文件，再 ``os.replace`` 覆盖目标。"""
    tmp_path = path + ".refresh-tmp"
    with open(tmp_path, "wb") as handle:
        handle.write(data)
    os.replace(tmp_path, path)


def refresh_gate_table(gate_path=None, tool_path=None, collect=None):
    """从工作区实测值重写 §1.4 表与 ``LOAD_TARGET_TABLE``（显式开关的唯一写路径）。

    默认写真实动作门 md（``GATE_TABLE_PATH``）与本工具源码（``TOOL_PATH``）；
    夹具传临时路径（绝不写真实文件）。流程：读工作区实测值 → 读两份文件 →
    逐行字面量替换（每条必须恰好命中一次，否则拒绝写）→ 受控替换写 →
    写后读回 sha256 并核对。返回报告字典（``error`` 非空即未完成）。
    """
    report = {"gate_path": gate_path or GATE_TABLE_PATH,
              "tool_path": tool_path or TOOL_PATH,
              "rows": [], "error": None, "detail": None,
              "gate": {"before_sha256": None, "after_sha256": None,
                       "expected_sha256": None, "readback_ok": None},
              "tool": {"before_sha256": None, "after_sha256": None,
                       "expected_sha256": None, "readback_ok": None}}
    rows, error = _measured_rows(collect)
    if error:
        report["error"] = error
        report["detail"] = "工作区实测值读不到／不完整：不写任何文件"
        return report
    report["rows"] = [{"label": label, "path": rel, "size": size,
                       "sha256": sha} for label, rel, size, sha in rows]
    gate_before, error = _sha256_file(report["gate_path"])
    if error:
        report["error"] = "gate_table_unreadable:%s" % error
        report["detail"] = "动作门表读不到：不写任何文件"
        return report
    tool_before, error = _sha256_file(report["tool_path"])
    if error:
        report["error"] = "tool_source_unreadable:%s" % error
        report["detail"] = "本工具源码读不到：不写任何文件"
        return report
    report["gate"]["before_sha256"] = gate_before
    report["tool"]["before_sha256"] = tool_before
    try:
        with open(report["gate_path"], "rb") as handle:
            gate_text = handle.read().decode("utf-8")
        with open(report["tool_path"], "rb") as handle:
            tool_text = handle.read().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        report["error"] = "refresh_read_failed:%s" % type(exc).__name__
        report["detail"] = "两份文件读不到／解不开：不写任何文件"
        return report
    new_gate_text, error = _rewrite_gate_source(gate_text, rows)
    if error:
        report["error"] = error
        report["detail"] = "§1.4 表行未逐条命中：拒绝写入（绝不半匹配）"
        return report
    new_tool_text, error = _rewrite_tool_source(tool_text, rows)
    if error:
        report["error"] = error
        report["detail"] = "LOAD_TARGET_TABLE 行未逐条命中：拒绝写入（绝不半匹配）"
        return report
    gate_bytes = new_gate_text.encode("utf-8")
    tool_bytes = new_tool_text.encode("utf-8")
    try:
        _write_file_atomic(report["gate_path"], gate_bytes)
        _write_file_atomic(report["tool_path"], tool_bytes)
    except OSError as exc:
        report["error"] = "refresh_write_failed:%s" % type(exc).__name__
        report["detail"] = "写盘失败：%s" % exc
        return report
    report["gate"]["expected_sha256"] = hashlib.sha256(gate_bytes).hexdigest()
    report["tool"]["expected_sha256"] = hashlib.sha256(tool_bytes).hexdigest()
    gate_after, gate_error = _sha256_file(report["gate_path"])
    tool_after, tool_error = _sha256_file(report["tool_path"])
    report["gate"]["after_sha256"] = gate_after
    report["tool"]["after_sha256"] = tool_after
    if gate_error or tool_error:
        report["error"] = "readback_failed"
        report["detail"] = "写后读回失败：%s / %s" % (gate_error, tool_error)
        return report
    report["gate"]["readback_ok"] = \
        gate_after == report["gate"]["expected_sha256"]
    report["tool"]["readback_ok"] = \
        tool_after == report["tool"]["expected_sha256"]
    if not (report["gate"]["readback_ok"] and report["tool"]["readback_ok"]):
        report["error"] = "readback_mismatch"
        report["detail"] = "写后读回与写入内容不一致"
        return report
    return report


def collect_listeners(run=None):
    """只读四个口的 LISTENING PID（复用快照工具的同一解析口径）。"""
    return {str(port): snapshot_tool.listening_pids(port, run=run)
            for port in LISTEN_PORTS}


def collect_read_surface(get=None):
    """只读 8096 状态响应 → ``snapshot_tool.attribution_read_surface``。"""
    get = get or snapshot_tool.http_get_json
    return snapshot_tool.attribution_read_surface(get(CONSOLE_PORT, STATUS_PATH))


def collect_share_entry_probes(get=None):
    """只读三条入口探测（白名单外 404 / 白名单内无凭据 401）。"""
    get = get or snapshot_tool.http_get_json
    return {name: snapshot_tool.share_entry_probe_result(get(GATEWAY_PORT, path))
            for name, path, _status, _code in snapshot_tool.SHARE_ENTRY_EXPECTED}


def collect_share_ledger_tail():
    """只读 8110 分享账本末行（路径由产品解析器给出，不猜路径）。"""
    observe = snapshot_tool.backend_observe_facts(forget_stale=False)
    ledger = snapshot_tool.scan_share_ledger(observe.get("ledger_file"))
    ledger["observe_error"] = observe.get("error")
    ledger["ledger_file"] = observe.get("ledger_file")
    return ledger


def collect_authoritative_signals(probe_run=None):
    """只读跑提升身份探针，取 ``watchdog_task`` / ``wfp_limits`` 两个信号。

    探针自身只读：默认只打印 stdout；本工具**不给 -Out**，因此探针不写任何文件。
    """
    out = {"available": False, "reason": None, "elevated": None,
           "identity": None, "captured_at": None, "captured_at_iso": None,
           "encoding": None, "stdout_bytes": None, "rc": None,
           "signals": {}}
    if not os.path.exists(READINGS_TOOL_PATH):
        out["reason"] = "probe_missing"
        return out
    command = ["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", READINGS_TOOL_PATH]
    rc, stdout, error = (probe_run or _run_capture)(command,
                                                   READINGS_TIMEOUT_S)
    if error:
        out["reason"] = "probe_" + error
        return out
    out["rc"] = rc
    out["stdout_bytes"] = len(stdout) if stdout is not None else None
    payload, encoding, decode_error = decode_json_output(stdout)
    out["encoding"] = encoding
    if decode_error:
        out["reason"] = "probe_" + decode_error
        return out
    out["elevated"] = payload.get("elevated")
    out["identity"] = payload.get("identity")
    out["captured_at"] = payload.get("captured_at")
    out["captured_at_iso"] = payload.get("captured_at_iso")
    signals = payload.get("window_signals")
    if isinstance(signals, dict):
        for name in WATCHDOG_SIGNAL_NAMES:
            entry = signals.get(name)
            if isinstance(entry, dict):
                out["signals"][name] = entry
    if not out["signals"]:
        out["reason"] = "probe_signals_missing"
        return out
    if rc != 0:
        # 探针给出非零退出码但仍打印了可解析 JSON：读数照收，原因码如实记。
        out["reason"] = "probe_rc:%s" % rc
    out["available"] = True
    return out


def collect_facts(get=None, run=None, probe_run=None):
    """采集十项判据的全部只读事实（不重启、不写盘、不发模型请求）。"""
    return {
        "operations": collect_operations(get=get),
        "rollback": collect_rollback_facts(),
        "load_targets": collect_load_target_facts(),
        "listeners": collect_listeners(run=run),
        "read_surface": collect_read_surface(get=get),
        "share_entry_probes": collect_share_entry_probes(get=get),
        "share_ledger": collect_share_ledger_tail(),
        "authoritative": collect_authoritative_signals(probe_run=probe_run),
    }


# ------------------------------------------------------------------ 纯判定层
def _item(item_id, title, verdict, reason, detail, readings):
    return {"id": item_id, "title": title, "verdict": verdict,
            "reason": reason, "detail": detail, "readings": readings}


def judge_operations(summary):
    """判据 1：在途操作 ``queued/running`` 必须为 0。"""
    readings = dict(summary) if isinstance(summary, dict) else {}
    if not isinstance(summary, dict) or summary.get("error"):
        error = (summary or {}).get("error") if isinstance(summary, dict) \
            else "operations_facts_missing"
        return _item(
            "operations_in_flight", "在途操作为零", UNDECIDABLE,
            "operations_probe_failed:%s" % error,
            "控制台 operations 端点读不到（%s）——读不到 ≠ 无在途操作；"
            "先补证据再进窗口" % error, readings)
    live = summary.get("live")
    total = summary.get("total")
    if not isinstance(live, int) or isinstance(live, bool):
        return _item(
            "operations_in_flight", "在途操作为零", UNDECIDABLE,
            "operations_live_unavailable",
            "operations 响应可读但 queued/running 计数读不到——读不到 ≠ 为 0",
            readings)
    if live == 0:
        return _item(
            "operations_in_flight", "在途操作为零", PASS, "ok",
            "在途操作 live=0（total=%s）——可进窗口" % total, readings)
    return _item(
        "operations_in_flight", "在途操作为零", FAIL,
        "in_flight:%s" % live,
        "存在 %s 条 queued/running（total=%s）——停在窗口前，等它结束再重跑本工具"
        % (live, total), readings)


def judge_rollback_copies(rows):
    """判据 2：三份回退副本的字节数与 sha256 与 §1.4 表逐项相符。"""
    if not isinstance(rows, list) or not rows:
        return _item(
            "rollback_copies", "回退副本与 §1.4 表相符", UNDECIDABLE,
            "rollback_facts_missing", "回退副本事实缺失——读不到 ≠ 相符", {})
    verdict = PASS
    reasons = []
    readings = []
    for row in rows:
        entry = {"label": row.get("label"), "path": row.get("path"),
                 "exists": row.get("exists"),
                 "size": row.get("size"), "sha256": row.get("sha256"),
                 "error": row.get("error"),
                 "error_kind": row.get("error_kind"),
                 "expected_size": row.get("expected_size"),
                 "expected_sha256": row.get("expected_sha256"),
                 "size_match": None, "sha256_match": None}
        if row.get("error_kind") == "unreadable":
            verdict = _worse(verdict, UNDECIDABLE)
            reasons.append("rollback_copy_unreadable:%s:%s"
                           % (row.get("label"), row.get("error")))
        elif row.get("error_kind") == "missing":
            verdict = _worse(verdict, FAIL)
            reasons.append("rollback_copy_missing:%s" % row.get("label"))
        elif not row.get("exists"):
            verdict = _worse(verdict, FAIL)
            reasons.append("rollback_copy_missing:%s" % row.get("label"))
        else:
            entry["size_match"] = row.get("size") == row.get("expected_size")
            entry["sha256_match"] = \
                row.get("sha256") == row.get("expected_sha256")
            if not entry["size_match"]:
                verdict = _worse(verdict, FAIL)
                reasons.append("rollback_copy_size_mismatch:%s:%s!=%s"
                               % (row.get("label"), row.get("size"),
                                  row.get("expected_size")))
            if not entry["sha256_match"]:
                verdict = _worse(verdict, FAIL)
                reasons.append("rollback_copy_sha256_mismatch:%s"
                               % row.get("label"))
        readings.append(entry)
    if verdict == PASS:
        return _item(
            "rollback_copies", "回退副本与 §1.4 表相符", PASS, "ok",
            "三份回退副本的字节数与 sha256 与动作门 §1.4 表逐项相符（%s）"
            % GATE_SOURCE, {"rows": readings, "gate_source": GATE_SOURCE})
    return _item(
        "rollback_copies", "回退副本与 §1.4 表相符", verdict,
        ";".join(reasons),
        "回退副本与 §1.4 表不符：%s——先查清差异来源（不要用副本回退未核实的版本）"
        % "；".join(reasons),
        {"rows": readings, "gate_source": GATE_SOURCE})


def judge_load_targets_head(rows):
    """判据 3：三份「将载入版本」工作区 = HEAD，且与 §1.4 表相符。"""
    if not isinstance(rows, list) or not rows:
        return _item(
            "load_targets_head", "将载入版本工作区=HEAD", UNDECIDABLE,
            "load_target_facts_missing", "将载入版本事实缺失——读不到 ≠ 一致", {})
    verdict = PASS
    reasons = []
    readings = []
    drift = []
    for row in rows:
        workspace = row.get("workspace") or {}
        head = row.get("head") or {}
        diff = row.get("diff") or {}
        table = row.get("table") or {}
        entry = {"label": row.get("label"), "path": row.get("path"),
                 "workspace": {"exists": workspace.get("exists"),
                               "size": workspace.get("size"),
                               "sha256": workspace.get("sha256"),
                               "error": workspace.get("error"),
                               "error_kind": workspace.get("error_kind")},
                 "head": dict(head),
                 "diff": dict(diff),
                 "table": dict(table),
                 "repo_error": row.get("repo_error"),
                 "workspace_equals_head": None, "table_match": None}
        if workspace.get("error_kind") == "unreadable" or head.get("error") \
                or diff.get("error") or row.get("repo_error"):
            verdict = _worse(verdict, UNDECIDABLE)
            reasons.append("load_target_unreadable:%s" % row.get("label"))
        elif workspace.get("error_kind") == "missing":
            verdict = _worse(verdict, FAIL)
            reasons.append("load_target_missing:%s" % row.get("label"))
        else:
            entry["workspace_equals_head"] = \
                workspace.get("sha256") == head.get("sha256")
            entry["table_match"] = (
                workspace.get("size") == table.get("bytes")
                and workspace.get("sha256") == table.get("sha256"))
            if not entry["workspace_equals_head"]:
                verdict = _worse(verdict, FAIL)
                reasons.append("workspace_differs_from_head:%s" % row.get("label"))
            if not entry["table_match"]:
                verdict = _worse(verdict, FAIL)
                reasons.append("gate_table_stale:%s" % row.get("label"))
                drift.append({
                    "label": row.get("label"), "path": row.get("path"),
                    "workspace_size": workspace.get("size"),
                    "workspace_sha256": workspace.get("sha256"),
                    "table_bytes": table.get("bytes"),
                    "table_sha256": table.get("sha256"),
                    "gate_source": GATE_SOURCE})
            if diff.get("clean") is not True:
                verdict = _worse(verdict, FAIL)
                reasons.append("git_diff_not_clean:%s" % row.get("label"))
        readings.append(entry)
    readings_block = {"rows": readings, "gate_source": GATE_SOURCE,
                      "drift": drift}
    if verdict == PASS:
        return _item(
            "load_targets_head", "将载入版本工作区=HEAD", PASS, "ok",
            "三份「将载入版本」工作区与 HEAD 逐字节一致，且与 §1.4 表相符"
            "（%s）" % GATE_SOURCE, readings_block)
    return _item(
        "load_targets_head", "将载入版本工作区=HEAD", verdict,
        ";".join(reasons),
        "将载入版本核对未通过：%s——工作区≠HEAD 时先查清在途改动；"
        "§1.4 表过期（gate_table_stale）时先更新表或由维护者确认按当前 HEAD "
        "载入，再重跑本工具" % "；".join(reasons), readings_block)


def judge_listeners(listeners):
    """判据 4：8000/8096/8100/8110 的 LISTENING PID（每口恰一个）。"""
    if not isinstance(listeners, dict) or not listeners:
        return _item(
            "listeners", "四口监听 PID", UNDECIDABLE, "listener_facts_missing",
            "监听事实缺失——读不到 ≠ 没在听", {})
    verdict = PASS
    reasons = []
    readings = {}
    for port in LISTEN_PORTS:
        entry = listeners.get(str(port))
        pids = entry.get("pids") if isinstance(entry, dict) else None
        readings[str(port)] = {"pids": pids,
                              "reason": (entry or {}).get("reason")
                              if isinstance(entry, dict) else None}
        if pids is None:
            verdict = _worse(verdict, UNDECIDABLE)
            reasons.append("listener_query_failed:%s" % port)
        elif len(pids) == 0:
            verdict = _worse(verdict, FAIL)
            reasons.append("not_listening:%s" % port)
        elif len(pids) > 1:
            verdict = _worse(verdict, FAIL)
            reasons.append("multiple_listeners:%s" % port)
    if verdict == PASS:
        return _item(
            "listeners", "四口监听 PID", PASS, "ok",
            "8000/8096/8100/8110 各恰一个 LISTENING PID：%s"
            % "、".join("%s=%s" % (port, readings[str(port)]["pids"][0])
                        for port in LISTEN_PORTS), readings)
    return _item(
        "listeners", "四口监听 PID", verdict, ";".join(reasons),
        "监听核对未通过：%s——掉线或多义 PID 都停在窗口前" % "；".join(reasons),
        readings)


def judge_read_surface(surface):
    """判据 5：8096 读面键集（4 键旧形状 / 7 键新形状）。"""
    readings = dict(surface) if isinstance(surface, dict) else {}
    if not isinstance(surface, dict) or surface.get("error"):
        error = (surface or {}).get("error") if isinstance(surface, dict) \
            else "read_surface_facts_missing"
        return _item(
            "read_surface", "8096 读面键集", UNDECIDABLE,
            "read_surface_probe_failed:%s" % error,
            "8096 状态读面读不到（%s）——读不到 ≠ 旧形状" % error, readings)
    shape = surface.get("shape")
    keys = surface.get("keys")
    if shape == "seven_key":
        return _item(
            "read_surface", "8096 读面键集", PASS, "seven_key",
            "读面已是 7 键新形状（新代码已载入）：%s" % keys, readings)
    if shape == "four_key":
        return _item(
            "read_surface", "8096 读面键集", PASS, "four_key",
            "读面仍是 4 键旧形状（新代码未载入，本窗口的载入对象）：%s" % keys,
            readings)
    return _item(
        "read_surface", "8096 读面键集", FAIL,
        "unknown_attribution_shape",
        "读面可读但键集既不是 4 键旧形状也不是 7 键新形状（运行版本不是预期"
        "两版之一）：%s" % keys, readings)


def judge_funnel(surface):
    """判据 6：同一读面的 ``funnel_on`` 字段（同源、不同字段）。"""
    readings = dict(surface) if isinstance(surface, dict) else {}
    if not isinstance(surface, dict) or surface.get("error"):
        error = (surface or {}).get("error") if isinstance(surface, dict) \
            else "read_surface_facts_missing"
        return _item(
            "funnel", "Funnel 状态", UNDECIDABLE,
            "read_surface_probe_failed:%s" % error,
            "读面读不到（%s）——Funnel 状态读不到 ≠ 关闭" % error, readings)
    funnel_on = surface.get("funnel_on")
    if isinstance(funnel_on, bool):
        return _item(
            "funnel", "Funnel 状态", PASS, "ok",
            "funnel_on=%s（与第 5 项同源：同一份 8096 状态响应的不同字段）"
            % funnel_on, readings)
    return _item(
        "funnel", "Funnel 状态", UNDECIDABLE, "funnel_field_unavailable",
        "读面可读但没有 funnel_on 字段——Funnel 状态读不到 ≠ 关闭", readings)


def judge_share_entry_gates(probes):
    """判据 7：白名单外 404 ``route_not_shared`` / 白名单内无凭据 401 ``invalid_api_key``。"""
    signal = snapshot_tool.share_entry_gates_signal(probes)
    readings = dict(signal)
    status = signal.get("status")
    if status == "ok":
        return _item(
            "share_entry_gates", "分享入口 404/401 两种门", PASS, "ok",
            signal.get("detail"), readings)
    if status == "absent":
        return _item(
            "share_entry_gates", "分享入口 404/401 两种门", FAIL,
            signal.get("reason"),
            "两种门未按期望出现：%s——先查清入口状态再进窗口"
            % signal.get("detail"), readings)
    return _item(
        "share_entry_gates", "分享入口 404/401 两种门", UNDECIDABLE,
        signal.get("reason") or "probe_missing",
        "入口探测读不到：%s——读不到 ≠ 门不在" % signal.get("detail"),
        readings)


def judge_share_ledger_tail(ledger):
    """判据 8：8110 分享账本行数与末行 ``attribution``。"""
    readings = dict(ledger) if isinstance(ledger, dict) else {}
    if not isinstance(ledger, dict):
        return _item(
            "share_ledger_tail", "8110 账本末行", UNDECIDABLE,
            "ledger_facts_missing", "账本事实缺失——读不到 ≠ 没有行", {})
    if ledger.get("error"):
        return _item(
            "share_ledger_tail", "8110 账本末行", UNDECIDABLE,
            "ledger_unreadable:%s" % ledger.get("error"),
            "账本读不到（%s）——读不到 ≠ 没有行" % ledger.get("error"),
            readings)
    rows = ledger.get("rows")
    if not isinstance(rows, int):
        return _item(
            "share_ledger_tail", "8110 账本末行", UNDECIDABLE,
            "rows_unavailable", "账本行数读不到", readings)
    if rows == 0:
        return _item(
            "share_ledger_tail", "8110 账本末行", FAIL, "ledger_empty",
            "账本可读但没有行（末行不存在）——基线是 1680 行，先查清账本路径与"
            "轮换再进窗口", readings)
    return _item(
        "share_ledger_tail", "8110 账本末行", PASS, "ok",
        "账本 %s 行；末行 time=%r model=%r key_id=%r attribution=%r；"
        "已归属行 %s 条"
        % (rows, (ledger.get("last") or {}).get("time"),
           (ledger.get("last") or {}).get("model"),
           (ledger.get("last") or {}).get("key_id"),
           (ledger.get("last") or {}).get("attribution"),
           len(ledger.get("attributed_rows") or [])), readings)


def judge_watchdog(authoritative):
    """判据 9：``umshare-watchdog`` 任务状态（提升身份只读）。

    探针结论为 ``ok`` 时必须能用本目录自己的解析 + 判定从**原始信封**重算：
    信封缺失 → UNDECIDABLE（``probe_rederivation_envelope_missing``），
    重算与探针结论不一致 → UNDECIDABLE（``probe_rederivation_mismatch``）——
    两条都按「读不到 ≠ 通过」处理，绝不静默跳过重算后报 PASS。
    """
    if not isinstance(authoritative, dict):
        return _item(
            "watchdog_task", "看门狗任务状态", UNDECIDABLE,
            "probe_unavailable", "提升身份只读探针事实缺失——读不到 ≠ 未注册", {})
    readings = dict(authoritative)
    if not authoritative.get("available"):
        reason = authoritative.get("reason") or "probe_unavailable"
        return _item(
            "watchdog_task", "看门狗任务状态", UNDECIDABLE,
            "probe_unavailable:%s" % reason,
            "提升身份只读探针没给出可用读数（%s）——读不到 ≠ 未注册；"
            "先补证据（提升身份重跑本工具）" % reason, readings)
    signal = (authoritative.get("signals") or {}).get("watchdog_task")
    if not isinstance(signal, dict):
        return _item(
            "watchdog_task", "看门狗任务状态", UNDECIDABLE,
            "watchdog_signal_missing",
            "探针输出里没有 watchdog_task 信号——读不到 ≠ 未注册", readings)
    readings["signal"] = dict(signal)
    status = signal.get("status")
    if status == "ok":
        state = signal.get("state")
        state_text = signal.get("state_text")
        # 同一口径重算：用本目录自己的解析 + 判定从探针的原始信封重算。
        # 重算的**输入**也必须可读：信封缺失/不是对象 → UNDECIDABLE
        # （读不到 ≠ 通过），绝不静默跳过重算后拿探针自报的 ok 当 PASS。
        query = signal.get("query")
        if not isinstance(query, dict):
            readings["rederived"] = False
            return _item(
                "watchdog_task", "看门狗任务状态", UNDECIDABLE,
                "probe_rederivation_envelope_missing",
                "探针结论 status=ok 但没有可用的原始信封（query）：重算无法执行"
                "——读不到 ≠ 通过；先补证据（重跑提升身份探针）", readings)
        rederived = snapshot_tool.watchdog_task_signal(
            snapshot_tool.parse_watchdog_query(json.dumps(query)))
        readings["rederived"] = {
            "status": rederived.get("status"),
            "reason": rederived.get("reason"),
            "state": rederived.get("state")}
        if rederived.get("status") != status:
            return _item(
                "watchdog_task", "看门狗任务状态", UNDECIDABLE,
                "probe_rederivation_mismatch",
                "探针结论（%s／%s）与本工具重算（%s／%s）不一致：按 "
                "UNDECIDABLE 记，绝不折成通过"
                % (status, signal.get("reason"), rederived.get("status"),
                   rederived.get("reason")), readings)
        if state == 4:
            return _item(
                "watchdog_task", "看门狗任务状态", PASS, "ok",
                "任务已注册且 State=Running（%s）" % state_text, readings)
        return _item(
            "watchdog_task", "看门狗任务状态", FAIL,
            "watchdog_state:%s" % state_text,
            "任务已注册但 State=%s（不是 Running）——先补证据／修任务再进窗口"
            % state_text, readings)
    if status == "absent":
        return _item(
            "watchdog_task", "看门狗任务状态", FAIL,
            "watchdog_not_registered_elevated",
            "提升身份下的权威读数：任务未注册（%s）——先注册 watchdog 再进窗口"
            % signal.get("detail"), readings)
    return _item(
        "watchdog_task", "看门狗任务状态", UNDECIDABLE,
        signal.get("reason") or "watchdog_undecidable",
        "读不到（%s）——读不到 ≠ 未注册；先补证据（提升身份重跑本工具）"
        % signal.get("detail"), readings)


def judge_wfp_limits(authoritative):
    """判据 10：WFP 限制（提升身份只读；读不到一律 UNDECIDABLE）。"""
    if not isinstance(authoritative, dict):
        return _item(
            "wfp_limits", "WFP 限制", UNDECIDABLE, "probe_unavailable",
            "提升身份只读探针事实缺失——读不到 ≠ limits 不在位", {})
    readings = dict(authoritative)
    if not authoritative.get("available"):
        reason = authoritative.get("reason") or "probe_unavailable"
        return _item(
            "wfp_limits", "WFP 限制", UNDECIDABLE,
            "probe_unavailable:%s" % reason,
            "提升身份只读探针没给出可用读数（%s）——读不到 ≠ limits 不在位；"
            "先补证据（提升身份重跑本工具）" % reason, readings)
    signal = (authoritative.get("signals") or {}).get("wfp_limits")
    if not isinstance(signal, dict):
        return _item(
            "wfp_limits", "WFP 限制", UNDECIDABLE, "wfp_signal_missing",
            "探针输出里没有 wfp_limits 信号——读不到 ≠ limits 不在位", readings)
    readings["signal"] = dict(signal)
    status = signal.get("status")
    if status == "ok":
        return _item(
            "wfp_limits", "WFP 限制", PASS, "ok",
            "WFP strong=OK 且出站阻断规则在位（wfp_ok=%s firewall_ok=%s）"
            % (signal.get("wfp_ok"), signal.get("firewall_ok")), readings)
    if status == "absent":
        return _item(
            "wfp_limits", "WFP 限制", FAIL, "wfp_limits_not_in_place",
            "提升身份下的权威读数：limits 不在位（wfp_ok=%s firewall_ok=%s，"
            "%s）——先按 um-share-deploy 恢复限制再进窗口"
            % (signal.get("wfp_ok"), signal.get("firewall_ok"),
               signal.get("reason")), readings)
    return _item(
        "wfp_limits", "WFP 限制", UNDECIDABLE,
        signal.get("reason") or "wfp_undecidable",
        "读不到（%s）——读不到 ≠ limits 不在位；先补证据（提升身份重跑本工具）"
        % signal.get("detail"), readings)


def evaluate(facts):
    """纯函数：十项事实 → 逐项判定 + 汇总 + 退出码（0 = 十项全 PASS）。"""
    facts = facts if isinstance(facts, dict) else {}
    items = [
        judge_operations(facts.get("operations")),
        judge_rollback_copies(facts.get("rollback")),
        judge_load_targets_head(facts.get("load_targets")),
        judge_listeners(facts.get("listeners")),
        judge_read_surface(facts.get("read_surface")),
        judge_funnel(facts.get("read_surface")),
        judge_share_entry_gates(facts.get("share_entry_probes")),
        judge_share_ledger_tail(facts.get("share_ledger")),
        judge_watchdog(facts.get("authoritative")),
        judge_wfp_limits(facts.get("authoritative")),
    ]
    counts = {PASS: 0, FAIL: 0, UNDECIDABLE: 0}
    for item in items:
        counts[item["verdict"]] += 1
    warnings = []
    for item in items:
        for entry in (item.get("readings") or {}).get("drift") or []:
            warnings.append(entry)
    ok = counts[FAIL] == 0 and counts[UNDECIDABLE] == 0
    return {"items": items, "counts": counts, "warnings": warnings,
            "exit_code": 0 if ok else 2}


# ------------------------------------------------------------------ 打印层
def format_plan():
    """dry-run：只打印将查什么与判据；**不读任何现场**（退出码 0）。"""
    lines = ["== 窗口一（只载入 8096）前置核对 · dry-run"
             "（只打印将查什么；不读任何现场） ==",
             "--check 才真查；以下全部只读。十项判据与来源："]
    lines.append("  1) operations_in_flight —— 不带凭据 GET "
                 "http://127.0.0.1:8096/api/console/operations?limit=50 的 "
                 "queued/running 必须 = 0（动作门 §1.1）；读不到 = UNDECIDABLE。")
    lines.append("  2) rollback_copies —— 三份回退副本的字节数与 sha256 与 "
                 "动作门 §1.4 表逐项相符：")
    for label, rel, want_bytes, want_sha in ROLLBACK_COPY_TABLE:
        lines.append("       %s（%s）：%s 字节、sha256=%s"
                     % (label, rel, want_bytes, want_sha))
    lines.append("     文件不存在 = FAIL；读不到（权限等）= UNDECIDABLE"
                 "（读不到 ≠ 不存在）。")
    lines.append("  3) load_targets_head —— 三份「将载入版本」必须工作区 = HEAD"
                 "（逐字节比较 git blob）且与 §1.4 表「将载入版本（HEAD）」列相符：")
    for label, rel, want_bytes, want_sha in LOAD_TARGET_TABLE:
        lines.append("       %s（%s）：%s 字节、sha256=%s"
                     % (label, rel, want_bytes, want_sha))
    lines.append("     工作区 ≠ HEAD 记 workspace_differs_from_head；表不符记 "
                 "gate_table_stale（表已过期：先更新 §1.4 表或由维护者确认按当前 "
                 "HEAD 载入，再重跑本工具）；git 读不到 = UNDECIDABLE。")
    lines.append("  4) listeners —— 8000/8096/8100/8110 的 LISTENING PID"
                 "（每口恰一个）；无人监听 = FAIL、多个 = FAIL、netstat 读不到 = "
                 "UNDECIDABLE。")
    lines.append("  5) read_surface —— 8096 状态读面 attribution 键集：4 键旧形状"
                 "（%s）与 7 键新形状（%s）都是已知形状（记录哪一种）；"
                 "既不是 4 键也不是 7 键 = FAIL；读不到 = UNDECIDABLE。"
                 % (sorted(snapshot_tool.LEGACY_ATTRIBUTION_KEYS),
                    sorted(snapshot_tool.SEVEN_ATTRIBUTION_KEYS)))
    lines.append("  6) funnel —— 同一份 8096 状态响应的 funnel_on 字段（同源、"
                 "不同字段）；读不到 = UNDECIDABLE。")
    lines.append("  7) share_entry_gates —— 不带凭据 GET 8100 的 / 与 /health 须 "
                 "404 route_not_shared；GET /v1/models 须 401 invalid_api_key；"
                 "门不符 = FAIL、探测读不到 = UNDECIDABLE。")
    lines.append("  8) share_ledger_tail —— 8110 账本行数与末行 time/model/key_id/"
                 "attribution（路径由 backend_observe_facts(forget_stale=False) 解析，"
                 "不猜路径）；读不到 = UNDECIDABLE、可读但 0 行 = FAIL。")
    lines.append("  9) watchdog_task —— umshare-watchdog 任务状态：提升身份只读探针 "
                 "authoritative_readings.ps1（默认只打印 stdout，不给 -Out）的 "
                 "window_signals.watchdog_task，并用本目录 share_chain_snapshot 的 "
                 "parse_watchdog_query + watchdog_task_signal 从原始信封重算；探针结论为 "
                 "ok 但信封缺失 = UNDECIDABLE（重算输入读不到）；重算不一致 = "
                 "UNDECIDABLE；非提升读不到 = UNDECIDABLE（绝不记成「未注册」）。")
    lines.append(" 10) wfp_limits —— 同一探针的 window_signals.wfp_limits"
                 "（WFP strong + 防火墙规则；只有这一层探针读数，无交叉校验）；"
                 "非提升读不到 = UNDECIDABLE（绝不记成「不在位」）。")
    lines.append("=" * 60)
    lines.append("总判定：任一项 FAIL 或 UNDECIDABLE 即退出码 2；只有十项全部 PASS "
                 "才退出码 0（读不到 ≠ 通过）。")
    lines.append("本 dry-run 不读任何现场，退出码 0（dry-run 不是判定）。")
    return "\n".join(lines)


def format_report(result, identity=None, elevated=None, captured_at_iso=None):
    """打印十项判定、汇总、警告与总判定；只给字段与原因码，绝不打印密钥内容。"""
    lines = ["== 窗口一（只载入 8096）前置核对 · --check（只读） =="]
    lines.append("采集时刻：%s；身份：%s（elevated=%s）"
                 % (captured_at_iso, identity, elevated))
    for index, item in enumerate(result["items"], start=1):
        lines.append("[%s] %s) %s —— %s"
                     % (item["verdict"], index, item["title"],
                        item["detail"]))
    lines.append("-" * 60)
    counts = result["counts"]
    lines.append("汇总：PASS %s / FAIL %s / UNDECIDABLE %s"
                 % (counts[PASS], counts[FAIL], counts[UNDECIDABLE]))
    undecidable = [item["id"] for item in result["items"]
                   if item["verdict"] == UNDECIDABLE]
    if undecidable:
        lines.append("UNDECIDABLE 项：%s——先补证据（提升身份重跑本工具）；"
                     "UNDECIDABLE 不是通过" % ", ".join(undecidable))
    if result["warnings"]:
        lines.append("§1.4 表漂移明细 %s 项（本工具第 3 项判据之一；"
                     "进窗口前必须先处理）：" % len(result["warnings"]))
        for drift in result["warnings"]:
            lines.append("  - %s（%s）：工作区=HEAD sha256=%s（%s 字节）；"
                         "§1.4 表记 %s（%s 字节）→ 表已过期；"
                         "处理见执行卡窗口一步 0"
                         % (drift.get("label"), drift.get("path"),
                            drift.get("workspace_sha256"),
                            drift.get("workspace_size"),
                            drift.get("table_sha256"),
                            drift.get("table_bytes")))
        # 只在 --check 报 gate_table_stale 时提示可用：默认关闭，绝不自动写。
        lines.append("  - 可用 `--refresh-gate-table` 从工作区实测值重写 §1.4 表与本"
                     "工具的 LOAD_TARGET_TABLE（显式开关，默认关闭；写后读回并"
                     "打印前后哈希），随后重跑 --check 复核。")
    lines.append("=" * 60)
    if result["exit_code"] == 0:
        lines.append("结论：十项全部 PASS（退出码 0）——可进窗口一")
    else:
        lines.append("结论：未完成核对——存在 FAIL/UNDECIDABLE（退出码 2；"
                     "缺证据不宣布通过）")
    return "\n".join(lines)


def format_refresh_report(report):
    """打印门表刷新的前后哈希与写后读回结论（只给路径/哈希，绝不打印文件内容）。"""
    lines = ["== §1.4 门表刷新 · --refresh-gate-table（本工具唯一写路径） =="]
    lines.append("将载入版本（工作区实测值）：")
    for row in report.get("rows") or []:
        lines.append("  %s（%s）：%s 字节 sha256=%s"
                     % (row.get("label"), row.get("path"),
                        row.get("size"), row.get("sha256")))
    gate = report.get("gate") or {}
    tool = report.get("tool") or {}
    lines.append("动作门 §1.4 表（%s）：" % (report.get("gate_path"),))
    lines.append("  前 sha256=%s" % (gate.get("before_sha256"),))
    lines.append("  后 sha256=%s（期望 %s；读回 %s）"
                 % (gate.get("after_sha256"), gate.get("expected_sha256"),
                    "一致" if gate.get("readback_ok") else "不一致/未核对"))
    lines.append("本工具 LOAD_TARGET_TABLE（%s）：" % (report.get("tool_path"),))
    lines.append("  前 sha256=%s" % (tool.get("before_sha256"),))
    lines.append("  后 sha256=%s（期望 %s；读回 %s）"
                 % (tool.get("after_sha256"), tool.get("expected_sha256"),
                    "一致" if tool.get("readback_ok") else "不一致/未核对"))
    lines.append("=" * 60)
    if report.get("error"):
        lines.append("结论：未完成刷新——%s（退出码 2；未写或写后核对失败）"
                     % (report.get("error"),))
        if report.get("detail"):
            lines.append("  detail：%s" % (report.get("detail"),))
    else:
        lines.append("结论：已从工作区实测值重写两份文件并读回一致（退出码 0）；"
                     "请重跑 --check 复核第 3 项")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="窗口一（只载入 8096）前置核对（默认 dry-run；--check 才真查且只读）")
    parser.add_argument("--check", action="store_true",
                        help="真实只读核对（默认 dry-run 只打印将查什么）")
    parser.add_argument("--refresh-gate-table", action="store_true",
                        help="唯一写路径（显式开关，默认关闭）：从工作区实测值重写"
                             "动作门 §1.4 表与本工具的 LOAD_TARGET_TABLE，"
                             "写后读回并打印前后哈希；只在 --check 报 "
                             "gate_table_stale 时使用")
    args = parser.parse_args(argv)

    if args.refresh_gate_table:
        # 显式开关才写；默认（含 --check）绝不写任何文件。
        report = refresh_gate_table()
        print(format_refresh_report(report))
        return 0 if report.get("error") is None else 2

    if not args.check:
        print(format_plan())
        return 0

    captured_at_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    identity = None
    elevated = None
    try:
        facts = collect_facts()
    except Exception as exc:  # noqa: BLE001 — 采集失败如实报非零
        print("[前置核对] 采集失败：%s: %s" % (type(exc).__name__, exc),
              file=sys.stderr)
        print("== 窗口一（只载入 8096）前置核对 · --check（只读） ==")
        print("结论：未完成核对——采集失败：%s: %s（退出码 2）"
              % (type(exc).__name__, exc))
        return 2
    authoritative = facts.get("authoritative") or {}
    identity = authoritative.get("identity")
    elevated = authoritative.get("elevated")
    result = evaluate(facts)
    print(format_report(result, identity=identity, elevated=elevated,
                        captured_at_iso=captured_at_iso))
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
