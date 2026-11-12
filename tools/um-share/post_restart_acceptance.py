# -*- coding: utf-8 -*-
"""8096 重启后八层验收：拒绝在缺层时宣布成功（动作门 §③.1b / §③.3 / §④）。

维护者点名的硬要求：**不能只验「8096 已重启」**。本工具把窗口后的核对拆成
八层，逐层打印 PASS / FAIL / UNDECIDABLE，并给总判定：

① 8096 健康 200 且 PID 已变（新 PID ≠ 重启前 PID）；**重启前 8096 无监听／
   监听证据读不到时记 ``undecidable``**——没有旧 PID 可证「已变」，缺证据不得报
   PASS；health 非 200 记 FAIL（状态读到了就不是「不可判定」），200 但体不是
   JSON 对象记 ``undecidable``；
② 8096 读面已是 7 键新形状（``file_ready`` / ``file_reason`` / ``key_chars`` /
   ``instance_loaded`` / ``request_attribution_observed`` /
   ``request_attribution_detail`` / ``key_file``）；仍为 4 键即 FAIL；
③ 8100 仍在监听，且启动行（``<RunRoot>\\run\\live\\raw.txt``）如实报
   「请求归因：已启用」或「未启用（共享密钥不可读，后端记为未归属）」——两者都
   合法，但必须打印实际值；启动行早于当前 8100 进程启动时间（陈旧行）时记
   ``undecidable``，不得把不是当前实例的启动行当它的实际值（2026-09-28 执行轮 11
   修的真机缺陷：采集层此前把内层表交给 ``_listener_state``，8100 的 PID 与进程
   启动时间**永远读不到**，这条陈旧行核对曾是死代码）。
   **本拓扑下启动行结构性为空**（2026-09-28 执行轮 14 定稿的等价证据口径）：
   8100 由 ``share_guard.py`` 以 ``CreateProcessW(..., False, ...)`` 启动（第 5 参
   bInheritHandles=False，``share_guard.py:488``），且该文件 ``print(`` 计数为 0；
   「请求归因：」那行由 ``share_entry.py:1292`` 打印，而
   ``deploy-elevated.ps1:536`` 把 guard 的 stdout 重定向到 ``run\\live\\raw.txt``
   ⇒ **该行永远采不到**。这是拓扑事实，**不是窗口失败**，绝不记成 FAIL：
   启动行读不到时本层改用 ``share_attribution_status()`` 的 ``file_ready`` /
   ``instance_loaded`` 等价且权威的证据判定（实际值写等价标签）：
   密钥就绪且 ``instance_loaded.value='yes'`` → PASS；密钥未就绪
   （``file_ready=False``）且 ``instance_loaded.reason='key_file_not_ready'`` →
   PASS（密钥未生成时的预期状态，不是故障）；密钥就绪但实例载入 ``no``/``unknown``
   或事实读不到 → ``undecidable``；
④ 8110 仍在监听、``auth_state`` 与重启前一致；
⑤ 分享入口白名单外 404 ``route_not_shared``、白名单内无凭据 401
   ``invalid_api_key``；
⑥ Funnel 状态（``funnel_on``）与重启前一致；
⑦ 8110 账本行数只增不减、已归属行不变、账本路径不变（动作门 §③.1b 行 8
   的判据全文）；
⑧ watchdog 任务状态（``umshare-watchdog``；读不到即 ``undecidable``，绝不记成
   「未注册」）；给了 ``--authoritative`` 时改由**窗口后权威读数**判定（见下）。

总判定：任一层 FAIL 或 UNDECIDABLE 即退出码 2 并打印「未完成核对」——脚本拒绝
在缺层时宣布成功；只有八层全部 PASS 才退出码 0。①④⑥⑦ 需要重启前读数：由
``--before <snapshot.json>`` 给出（``share_chain_snapshot.py --out`` 写出的那份）；
不给或读不到时这四层如实记 ``undecidable``，不得用「看起来没问题」补齐。

窗口后权威读数接缝（``--authoritative <json>``，加性；2026-09-28 复审执行轮 11）：

* **为什么需要**：``--check`` 自己跑在普通权限下时，``Get-ScheduledTask`` /
  ``schtasks`` 读不到 SYSTEM 的 watchdog 任务（拒绝访问），第 ⑧ 层只能记
  ``undecidable``——即使前后两份快照都已合并提升身份的读数。本开关把**窗口后**
  那份提升身份只读探针（``tools/um-share/authoritative_readings.ps1 -Out``）的 JSON
  直接交给八层验收，用其中的读数判定第 ⑧ 层（以及 ③ 的 raw.txt mtime、④ 的
  ``backend.pid`` 身份旁证）。
* **读数必须先过四道校验**（**按 ①②③④ 的顺序求值**；任一条不满足 → 相关层记
  ``undecidable`` 并给原因码，**不得报 PASS**，文案仍是「先补取证据」）：① 结构可用
  （``tool`` / ``schema_version`` / ``window_signals`` 与快照同 schema，逐条信号按
  ``share_chain_snapshot.merge_authoritative_readings`` 的同一口径校验；需要原始信封
  重算的 ``watchdog_task`` 缺可解析信封 → ``undecidable``
  （``authoritative_envelope_missing`` / ``authoritative_envelope_unparsable``），
  绝不只凭自报字段给 PASS）；② 新鲜度（``captured_at`` 不早于当前 8100 进程启动时间、
  不早于窗口前快照的 ``captured_at``）；③ 身份（读数须声明采集身份且为提升身份；
  非提升身份采的读数不得当权威）；④ 时间顺序（窗口后读数不得早于窗口前读数，颠倒即拒绝）。
* **给了 ``--authoritative`` 就以它为准**：它不可用时第 ⑧ 层记 ``undecidable``，
  **绝不退回自身身份那次读数**（那正是本开关要避免的假 PASS 来源）。
* **不提供 ``--authoritative`` 时，接缝不参与任何判定**（``authoritative=None``，三层
  只走原路径）：第 ⑧ 层仍是自身身份读数（读不到即 ``undecidable``），八层判定与
  报告与**同一份代码**不提供该开关时逐字相同（``AuthoritativeSeamAdditivityTests``
  钉住）。与**加接缝前的基线**相比有一处不同、且与本接缝无关：第 ③ 层 detail 因
  **同轮（执行轮 11）的真机缺陷修复**而不同（``collect_facts`` 此前把采集层内层表
  交给 ``_listener_state`` → 8100 的 PID 与进程启动时间永远读不到、「陈旧行」核对
  曾是死代码；现在 detail 会打印 pid/started_at，缺进程启动时间记 ``undecidable``）
  ——见模块开头第 ③ 层说明与 ``collect_gateway_startup`` 的 docstring。另一处与
  本接缝无关的差异：第 ③ 层因**执行轮 14 的判定口径修正**新增了等价证据路径
  （启动行读不到时改用 ``share_attribution_status()`` 的 ``file_ready`` /
  ``instance_loaded`` 事实，见模块开头第 ③ 层说明）。

只读边界（本工具的全部承诺；2026-09-28 复审执行轮 9 后收紧为**真正零写副作用**）：

* 本工具自身：HTTP 只发**不带凭据**的 ``GET http://127.0.0.1:<本机端口>``；
  进程查询只跑 ``netstat -ano -p TCP`` 与 ``Get-CimInstance Win32_Process``；
  watchdog 查询只跑 ``Get-ScheduledTask``（回退 ``schtasks /query``）；raw.txt 与
  账本只读打开；**本工具自己不写任何文件**；**不重启任何服务、不生成密钥、不创建
  分享 Key、不发模型请求、不改任何配置、不碰私人 8000**。
* 委托的只读观察一律走**只读开关**，**不删任何文件、不写任何文件**：
  1. ``console_share_production.production_backend_observe(forget_stale=False)``
     （经 ``share_chain_snapshot.backend_observe_facts(forget_stale=False)``）——
     ``forget_stale=False`` 关掉 ``_ProductionBackendOps.verify_running()`` 里唯一那次
     ``_forget()``（``os.remove`` 陈旧 ``backend.pid``）。此前这条采集会在 PID
     不匹配／进程已不在时删掉那份陈旧 pid 文件；现在陈旧文件照实报出并原样留在盘上
     （第 ④ 层因此拿 ``backend_pid.state`` 说明「为何 running 不是 True」）；
  2. ``console_share_production.production_backend_pid_state()`` 读同一份 pid 文件与
     进程身份证据，全分支无写、无删（陈旧即 ``pid_stale``：进程已不在或身份
     不匹配，两者不猜）；
  3. 同一观察的 ``_backend_auth_state()`` 会读 ``control-token`` 并以
     ``X-Admin-Control`` 头发只读 ``GET /admin/auth/status``（只读 GET、非模型请求、
     不打印令牌内容）；
  4. ``console_share_production.share_attribution_status()``（经快照工具的
     ``gateway_attribution_facts()``）—— 第 ③ 层的等价权威证据：读密钥文件
     （``os.stat`` + 只读打开，只取 ``strip()`` 后的长度）并只读探测 8100
     进程身份（CIM 查询）；不写、不删、不重启，绝不打印密钥内容。

写面清单（2026-09-28，逐项核对本工具的全部 IO 入口）：HTTP 只有不带凭据的只读
GET；进程查询只有 ``netstat`` / ``Get-CimInstance``；watchdog 只有 ``Get-ScheduledTask``
/ ``schtasks``；raw.txt 与账本只有 ``open(path, "rb"/"r")`` 只读打开；
``--authoritative`` 只多一个 ``open(path, "rb")`` 只读打开（经快照工具的
``load_authoritative_file``）。
**没有 ``os.remove``／``os.unlink``／写模式 ``open``／``os.makedirs``／
子进程动作**——采集路径上的条件写已消掉。
* **绝不打印密钥内容**：raw.txt 的启动行与账本行里没有密钥明文；本工具只打印
  路径、原因码、字段与 ``strip()`` 后的长度。
* 判定逻辑（``evaluate`` 及逐层函数）是**纯函数**：字典进、字典出，与 IO 分离；
  夹具测试直接喂字典验同一判定。

八层的判据来源 = ``output/ai-gw-round6-20260927/metering-action-gate.md`` 的
§③.1b 八项核对表 + §③.3 健康后验 + §④ 三事实验收；解析/观察口径复用
``share_chain_snapshot.py`` 的公开纯函数（``listening_pids`` / ``query_processes`` /
``attribution_read_surface`` / ``share_entry_probe_result`` /
``backend_observe_facts(forget_stale=False)`` / ``scan_share_ledger`` /
``gateway_attribution_facts`` / ``attribution_shape`` /
``SHARE_ENTRY_EXPECTED``），不复制字面量。

退出码：0 = 八层全 PASS（``--check``）或已打印 dry-run 计划；2 = 任一 FAIL /
UNDECIDABLE / 采集失败（读不到 ≠ 通过）。
"""

from __future__ import annotations

import argparse
import json
import locale
import os
import subprocess
import sys

# 同目录的 round7 只读快照工具：复用它的公开纯解析/观察函数，避免两处口径漂移。
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
AI_GATEWAY_DIR = os.path.dirname(os.path.dirname(TOOLS_DIR))
for _path in (TOOLS_DIR, AI_GATEWAY_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import share_chain_snapshot as snapshot_tool  # noqa: E402

CONSOLE_PORT = 8096
GATEWAY_PORT = 8100
BACKEND_PORT = 8110
PROBE_PORTS = (CONSOLE_PORT, GATEWAY_PORT, BACKEND_PORT)

WATCHDOG_TASK_NAME = "umshare-watchdog"

# 8100 启动行（share_entry.py:1290-1295 的 banner）写进 <RunRoot>\run\live\raw.txt
# （deploy-elevated.ps1:523-546 的 New-UmCaptureScript + :820 的 $liveRun）。
# 标记与两种合法实际值逐字取自 share_entry.py:1293-1294 与 SHARING.md:123-124。
STARTUP_MARKER = "请求归因："
ATTRIBUTION_ENABLED_LABEL = "已启用"
ATTRIBUTION_DISABLED_LABEL = "未启用"
STARTUP_LINE_CAP = 300
# raw.txt 由 live 任务在 Start 时截断重写（deploy-elevated.ps1:523-546），所以它的 mtime
# 不应早于当前 8100 进程的启动时间；容差用于吸收启动与写盘之间的正常先后抖动。
STARTUP_STALE_TOLERANCE_S = 5.0

# 启动行读不到时的等价且权威证据（2026-09-28 执行轮 14 定稿）：本拓扑下 raw.txt
# 结构性为空（代码行依据见下面 STRUCTURAL_EMPTY_NOTE），本层改用
# ``share_attribution_status()`` 的 ``file_ready`` / ``instance_loaded`` 事实判定，
# 实际值用下面两个标签（与启动行的「已启用」/「未启用（...）」回答同一问题）。
ATTRIBUTION_ENABLED_INFERRED = "已启用（以密钥就绪 + 实例载入推断）"
ATTRIBUTION_DISABLED_KEY_NOT_READY = "未启用（密钥未就绪）"

# 「raw.txt 在本拓扑下结构性为空」的代码行依据（写进第 ③ 层 detail，不得记成 FAIL）：
# 8100 由 share_guard.py 以 CreateProcessW 启动，第 5 参 bInheritHandles=False
# （share_guard.py:488），且该文件 print( 计数为 0；「请求归因：」那行由
# share_entry.py:1292 打印，而 deploy-elevated.ps1:536 把 guard 的 stdout 重定向到
# <RunRoot>\run\live\raw.txt ⇒ 该行永远采不到（拓扑事实，不是窗口失败）。
STRUCTURAL_EMPTY_NOTE = (
    "raw.txt 在本拓扑下结构性为空：8100 由 share_guard.py 以 CreateProcessW"
    "(..., False, ...) 启动（第 5 参 bInheritHandles=False，share_guard.py:488）"
    "且该文件 print( 计数为 0；「%s」那行由 share_entry.py:1292 打印、"
    "deploy-elevated.ps1:536 把 guard 的 stdout 重定向到 <RunRoot>\\run\\live\\"
    "raw.txt ⇒ 该行永远采不到（拓扑事实，不是窗口失败）" % STARTUP_MARKER)

# 窗口后权威读数接缝（``--authoritative``）用到的常量：
# * 探针 JSON 的工具名 / schema 版本逐字取自 ``share_chain_snapshot``（同一口径，
#   不复制字面量）；
# * ``WATCHDOG_RUNNING_STATE`` 是 ``MSFT_ScheduledTask.State`` 的 Running
#   （``share_chain_snapshot.WATCHDOG_TASK_STATES`` 里 4 = "Running"）；标签文案
#   一律走那份映射，本文件不另抄一份。
AUTHORITATIVE_TOOL = snapshot_tool.AUTHORITATIVE_READINGS_TOOL
AUTHORITATIVE_SCHEMA_VERSION = snapshot_tool.AUTHORITATIVE_READINGS_SCHEMA_VERSION
WATCHDOG_RUNNING_STATE = 4

RUNNER_TIMEOUT_S = 30.0


# --------------------------------------------------------------------- IO 层
def _decode_output(raw):
    """解码外部命令输出（Windows 先本机代码页再 UTF-8）；只用于原因码，不用于密钥。"""
    if raw is None:
        return None
    candidates = []
    if os.name == "nt":
        candidates.append("mbcs")
    candidates.append(locale.getpreferredencoding(False))
    candidates.append("utf-8")
    for encoding in candidates:
        try:
            return raw.decode(encoding)
        except (UnicodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _run_capture(args, timeout=RUNNER_TIMEOUT_S):
    """只读外部命令：``(returncode, stdout, stderr)``；启动失败返回 ``(None, None, reason)``。"""
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, None, "spawn_failed:%s" % type(exc).__name__
    return (proc.returncode, _decode_output(proc.stdout),
            _decode_output(proc.stderr))


_WATCHDOG_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "try { $t = Get-ScheduledTask -TaskName '%s' -ErrorAction Stop;"
    " ConvertTo-Json -InputObject @{name=$t.TaskName; state=[string]$t.State}"
    " -Compress }"
    " catch { [Console]::Error.Write('query_failed: ' +"
    " $_.Exception.GetType().Name); exit 3 }"
) % WATCHDOG_TASK_NAME


# ----------------------------------------------------------------- 纯解析层
def _listener_state(container, port):
    """纯函数：监听记录 → ``(state, pids)``（与 share_chain_snapshot._listener_state 同口径）。

    ``"ok"`` = 查询成功且有监听者；``"absent"`` = 查询成功且无人监听（掉线，不是
    「读不到」）；``"unknown"`` = 字段缺失或不可用（读不到 ≠ 没在听）。
    """
    entry = None
    if isinstance(container, dict):
        listeners = container.get("listeners")
        if isinstance(listeners, dict):
            entry = listeners.get(str(port))
    if not isinstance(entry, dict):
        return "unknown", []
    pids = entry.get("pids")
    if not isinstance(pids, list):
        return "unknown", []
    if not pids:
        return "absent", []
    clean = [pid for pid in pids
             if isinstance(pid, int) and not isinstance(pid, bool)]
    if len(clean) != len(pids):
        return "unknown", []
    return "ok", clean


def _listener_pids(container, port):
    """监听表里 ``port`` 的 PID 列表；读不到返回 ``None``（不折成空表）。"""
    state, pids = _listener_state(container, port)
    return pids if state == "ok" else None


def decode_startup_text(raw):
    """纯函数：raw.txt 字节 → ``(text, encoding)``。

    cmd 重定向写的是控制台代码页（本机实测中文为 GBK），所以候选顺序是
    UTF-8 → 本机代码页；**优先取含启动标记的那个解码**，都不含标记时取第一个
    能解码的结果（标记找不到由 ``parse_startup_attribution`` 如实报）。
    """
    if not isinstance(raw, (bytes, bytearray)):
        return None, "not_bytes"
    candidates = ["utf-8"]
    if os.name == "nt":
        candidates.append("mbcs")
    candidates.append(locale.getpreferredencoding(False))
    decoded = []
    for encoding in candidates:
        try:
            decoded.append((raw.decode(encoding), encoding))
        except (UnicodeError, LookupError):
            continue
    for text, encoding in decoded:
        if STARTUP_MARKER in text:
            return text, encoding
    if decoded:
        return decoded[0]
    return raw.decode("utf-8", errors="replace"), "utf-8:replace"


def parse_startup_attribution(text):
    """纯函数：raw.txt 文本 → 最后一条启动行的实际值。

    返回 ``{"line", "attribution", "reason"}``；``attribution`` ∈
    ``"enabled"`` / ``"disabled"`` / ``None``（都合法，**必须打印实际值**）。
    找不到标记行即 ``reason="marker_not_found"``，绝不猜一个值。
    """
    out = {"line": None, "attribution": None, "reason": "marker_not_found"}
    if not isinstance(text, str):
        out["reason"] = "text_unavailable"
        return out
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if STARTUP_MARKER not in line:
            continue
        tail = line.split(STARTUP_MARKER, 1)[1]
        out["line"] = line[:STARTUP_LINE_CAP]
        if tail.startswith(ATTRIBUTION_ENABLED_LABEL):
            out["attribution"] = "enabled"
        elif tail.startswith(ATTRIBUTION_DISABLED_LABEL):
            out["attribution"] = "disabled"
        else:
            out["attribution"] = None
        out["reason"] = "ok"
    return out


def parse_watchdog_state(text, returncode=None):
    """纯函数：``Get-ScheduledTask`` 的 JSON → ``state``；解析不了返回 ``None``。"""
    if returncode is not None and returncode != 0:
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    state = payload.get("state")
    return state if isinstance(state, str) and state else None


# schtasks 的中英文状态标签都归一到部署脚本用的英文枚举值。
_SCHTASKS_STATE_LABELS = {
    "running": "Running", "ready": "Ready", "disabled": "Disabled",
    "queued": "Queued", "unknown": "Unknown",
    "正在运行": "Running", "就绪": "Ready", "已禁用": "Disabled",
    "已排队": "Queued", "未知": "Unknown",
}


def parse_schtasks_state(text):
    """纯函数：``schtasks /query /FO LIST /V`` → 状态行归一（中英标签都认）。"""
    if not isinstance(text, str):
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for prefix in ("状态:", "Status:", "狀態:"):
            if stripped.startswith(prefix):
                value = stripped[len(prefix):].strip()
                return _SCHTASKS_STATE_LABELS.get(value.lower()) or value
    return None


# ----------------------------------------------------------------- 采集层
def gateway_startup_path():
    """8100 启动行路径：``<RunRoot>/run/live/raw.txt``（``(path, error)``）。

    路径解析器 = 产品的 ``console_share_production._share_run_root()``（默认
    ``DEFAULT_SHARE_RUN_ROOT``，console_share_production.py:843-861；部署端
    ``-RunRoot`` 同一默认，deploy-elevated.ps1:820 的 ``$liveRun`` 就是
    ``<RunRoot>\\run\\live``）；本工具不复制它的默认字面量。解析失败不猜路径。
    """
    try:
        import console_share_production
        root = console_share_production._share_run_root()
    except Exception as exc:  # noqa: BLE001 — 解析不了就如实报，不猜路径
        return None, "run_root_unresolved:%s" % type(exc).__name__
    if not isinstance(root, str) or not root:
        return None, "run_root_unresolved"
    return os.path.join(root, "run", "live", "raw.txt"), None


def collect_gateway_startup(container, processes):
    """只读采集 8100 启动行事实：路径、mtime、最后一条标记行与实际值。

    ``container`` 是**快照/事实形状**的容器（``{"listeners": {port: {pids}}}``），
    与 ``_listener_state`` 同一口径。2026-09-28 执行轮 11 修的真机缺陷：此前
    调用方交的是采集层的**内层扁平表**（``{port: {pids}}``），``_listener_state``
    在里面找不到 ``listeners`` 键 → 8100 的 PID 与进程启动时间**永远读不到**
    （真机实测：第 ③ 层打印 ``监听 pid=[37020]`` 而 ``8100_started_at=None``），
    「启动行早于当前 8100 进程启动时间（陈旧行）」这条核对成了死代码。
    """
    out = {"pid": None, "started_at": None, "started_at_reason": None,
           "raw_path": None, "raw_mtime": None, "line": None,
           "attribution": None, "reason": None, "error": None}
    pids = _listener_pids(container, GATEWAY_PORT)
    if isinstance(pids, list) and len(pids) == 1:
        out["pid"] = pids[0]
        row = processes.get(pids[0]) if isinstance(processes, dict) else None
        if isinstance(row, dict):
            out["started_at"] = row.get("started_at")
            out["started_at_reason"] = row.get("started_at_reason")

    raw_path, path_error = gateway_startup_path()
    out["raw_path"] = raw_path
    if raw_path is None:
        out["reason"] = "raw_path_unresolved"
        out["error"] = path_error
        return out
    if os.name != "nt":
        out["reason"] = "not_windows"
        return out
    try:
        with open(raw_path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        out["reason"] = "raw_unreadable"
        out["error"] = type(exc).__name__
        return out
    try:
        out["raw_mtime"] = os.stat(raw_path).st_mtime
    except OSError as exc:
        out["raw_mtime"] = None
        out["error"] = type(exc).__name__
    text, _encoding = decode_startup_text(raw)
    parsed = parse_startup_attribution(text)
    out["line"] = parsed.get("line")
    out["attribution"] = parsed.get("attribution")
    out["reason"] = parsed.get("reason")
    return out


def backend_pid_facts():
    """8110 专用后端 pid 文件的只读报告：**不删任何文件、不写任何文件**。

    走产品的 ``console_share_production.production_backend_pid_state()``（全分支
    无写、无删）：陈旧 ``backend.pid`` 照实报成 ``pid_stale``（进程已不在或身份
    不匹配，``_pid_matches`` 无法区分两者，本报告不猜），文件原样留在盘上。
    状态词表与产品逐字一致：``pid_file_absent`` / ``pid_file_unreadable`` /
    ``pid_stale`` / ``port_not_listening`` / ``running``（console_share_production.py
    :1348-1388）。读不到即 ``state="unavailable"`` 并带原因码，绝不折成
    「不存在」。
    """
    out = {"pid_file": None, "pid_file_present": None, "pid": None,
           "pid_matches": None, "port_listening": None, "state": None,
           "error": None}
    try:
        import console_share_production
        facts = console_share_production.production_backend_pid_state()
    except Exception as exc:  # noqa: BLE001 — 读不到是证据，不是崩溃
        out["state"] = "unavailable"
        out["error"] = "backend_pid_state_failed:%s" % type(exc).__name__
        return out
    if not isinstance(facts, dict):
        out["state"] = "unavailable"
        out["error"] = "backend_pid_state_invalid"
        return out
    for key in ("pid_file", "pid_file_present", "pid", "pid_matches",
                "port_listening", "state"):
        out[key] = facts.get(key)
    return out


def read_watchdog_state(run=None):
    """只读 watchdog 任务状态：``{"state", "query", "error"}``。

    主查询 = ``Get-ScheduledTask``（与部署脚本同一读取口径，deploy-elevated.ps1:782）；
    失败时回退 ``schtasks /query``（本机实测非提升会「拒绝访问」——**读不到即
    ``state=None``，绝不记成「未注册」**）。
    """
    run = run or _run_capture
    rc, out, err = run(["pwsh", "-NoProfile", "-NonInteractive",
                        "-Command", _WATCHDOG_SCRIPT])
    if rc == 0:
        state = parse_watchdog_state(out, rc)
        if state:
            return {"state": state, "query": "Get-ScheduledTask", "error": None}
        return {"state": None, "query": "Get-ScheduledTask",
                "error": "state_unparsable"}
    first_error = (err or "").strip() or "rc=%r" % (rc,)
    rc2, out2, err2 = run(["schtasks", "/query", "/TN", WATCHDOG_TASK_NAME,
                          "/FO", "LIST", "/V"])
    if rc2 == 0:
        state = parse_schtasks_state(out2)
        if state:
            return {"state": state, "query": "schtasks", "error": None}
        return {"state": None, "query": "schtasks", "error": "state_unparsable"}
    second_error = (err2 or "").strip() or "rc=%r" % (rc2,)
    return {"state": None, "query": "Get-ScheduledTask+schtasks",
            "error": "get_scheduled_task:%s | schtasks:%s"
                     % (first_error[:160], second_error[:160])}


def collect_facts(get=None):
    """采集八层所需的只读事实（不重启、不生成密钥、不发模型请求、不写盘）。"""
    get = get or snapshot_tool.http_get_json
    listeners = {str(port): snapshot_tool.listening_pids(port)
                 for port in PROBE_PORTS}
    health = get(CONSOLE_PORT, "/health")
    share_status = snapshot_tool.attribution_read_surface(
        get(CONSOLE_PORT, "/api/console/share-management/status"))
    entry_probe = {
        name: snapshot_tool.share_entry_probe_result(get(GATEWAY_PORT, path))
        for name, path, _status, _code in snapshot_tool.SHARE_ENTRY_EXPECTED
    }
    # forget_stale=False：只读那一档，陈旧 backend.pid 照实报出、不被删除。
    backend = snapshot_tool.backend_observe_facts(forget_stale=False)
    backend_pid = backend_pid_facts()
    ledger = snapshot_tool.scan_share_ledger(backend.get("ledger_file"))
    # 第 ③ 层的等价权威证据：同身份的只读直调 ``share_attribution_status()``
    # （经快照工具的同一包装器，字段与快照的 gateway_attribution 逐字同形）。
    # 只读：读密钥文件与探测 8100 进程身份，不写、不删、不重启。
    gateway_attribution = snapshot_tool.gateway_attribution_facts()
    # _listener_state 的口径是快照/事实形状的容器（{"listeners": {port: {pids}}}）：
    # 采集层的内层表（{port: {pids}}）按同一口径包一层再取 PID，否则它找不到
    # "listeners" 键 → 8100 的 PID 与进程启动时间永远读不到（执行轮 11 修的真机
    # 缺陷：第 ③ 层打印监听 pid=[37020] 而 gateway_startup.started_at=None）。
    facts_container = {"listeners": listeners}
    gateway_pids = _listener_pids(facts_container, GATEWAY_PORT)
    processes = snapshot_tool.query_processes(gateway_pids or [])
    startup = collect_gateway_startup(facts_container, processes)
    watchdog = read_watchdog_state()
    return {
        "listeners": listeners,
        "health_8096": health,
        "share_status": share_status,
        "share_entry_probe": entry_probe,
        "backend_observe": backend,
        "backend_pid": backend_pid,
        "gateway_attribution": gateway_attribution,
        "share_ledger": ledger,
        "gateway_startup": startup,
        "watchdog": watchdog,
    }


def read_before_snapshot(path):
    """只读重启前快照（``share_chain_snapshot.py --out`` 那份）：``(snapshot, error)``。"""
    try:
        snapshot = snapshot_tool.read_json_file(path)
    except (OSError, ValueError) as exc:
        return None, "%s:%s" % (type(exc).__name__, exc)
    if not isinstance(snapshot, dict):
        return None, "snapshot_not_an_object"
    return snapshot, None


# --------------------------------------- 窗口后权威读数接缝（加性；只读一个 JSON）
def load_authoritative_readings(path):
    """只读加载窗口后权威读数（提升身份探针的 JSON）。

    复用 ``share_chain_snapshot.load_authoritative_file``：同一份编码探测
    （UTF-8 带/不带 BOM、UTF-16、系统 ANSI）与 sha256 口径，读不到／解不开
    一律记 ``error``，绝不猜。
    """
    return snapshot_tool.load_authoritative_file(path)


def _number(value):
    """纯函数：真数值（排除 bool；``True`` 不是时刻）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _snapshot_captured_at(snapshot):
    """纯函数：快照自身的采集时刻（读不到返回 ``None``）。"""
    if not isinstance(snapshot, dict):
        return None
    value = snapshot.get("captured_at")
    return value if _number(value) else None


def _before_reading_captured_at(before):
    """纯函数：窗口前**权威读数**的采集时刻（前快照合并过探针时才有）。"""
    report = before.get("authoritative_readings") \
        if isinstance(before, dict) else None
    source = report.get("source") if isinstance(report, dict) else None
    value = source.get("captured_at") if isinstance(source, dict) else None
    return value if _number(value) else None


def _rejected_reasons(report):
    """纯函数：合并报告里被拒绝的信号 → ``{signal: reason}``。"""
    out = {}
    rows = report.get("rejected_signals") if isinstance(report, dict) else None
    if not isinstance(rows, list):
        return out
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("signal"), str):
            out[row["signal"]] = row.get("reason")
    return out


def authoritative_evidence(loaded, after, before):
    """纯函数：校验窗口后权威读数 → 判定用的证据字典。

    维护者 2026-09-28 复审要求（执行轮 11）：八层验收不能只用**自身运行身份**
    读 watchdog 任务状态，否则普通权限下即使前后快照已合并提升身份的读数，第 ⑧ 层
    仍停在 ``undecidable``。本函数把那份读数收进来，但**先过四道校验**——任一条
    不满足即整体拒绝（``usable=False`` + ``reason`` 原因码），相关层照实记
    ``undecidable``，绝不报 PASS：

    ① **结构**：``tool`` / ``schema_version`` / ``window_signals`` 与快照同 schema，
       逐条信号按 ``share_chain_snapshot.merge_authoritative_readings`` 的**同一
       口径**校验（缺键 / 坏状态 / 名字不符 / 未知信号一律拒绝，绝不半份接受）；
       需要原始信封重算的信号（``watchdog_task``）缺可解析信封时按 ``undecidable``
       记（``authoritative_envelope_missing`` / ``authoritative_envelope_unparsable``）
       ——**绝不**只凭自报字段给 PASS；
    ② **新鲜度**：``captured_at`` 不早于当前 8100 进程启动时间，也不早于窗口前
       快照的 ``captured_at``；任一参考时刻读不到 → 记 ``undecidable``（读不到 ≠
       通过）；
    ③ **身份**：读数须自报采集身份（``elevated`` + ``identity``）且为提升身份；
       非提升身份采的读数不得当权威；顶层身份与探针原始信封里的
       ``elevated``/``name`` 不一致时同样拒绝（手改过的 JSON 不得当权威）；
    ④ **时间顺序**：窗口后读数不得早于窗口前读数（前快照合并过探针时按它的
       ``source.captured_at`` 比），颠倒即拒绝；前读数时刻读不到 → 顺序无从核对，
       记 ``undecidable``。

    四道校验**按 ①②③④ 的顺序求值**：任一条不满足即返回该条的原因码，因此
    既过期又非提升身份的读数先得 ``authoritative_stale:*``（不是
    ``authoritative_not_elevated``）——顺序由 ``AuthoritativeSeamFreshnessTests``
    的夹具钉住。

    ``usable`` 只表示读数过了这四道校验；单条信号（如缺信封的
    ``watchdog_task``）仍可能记 ``undecidable``——层判定消费信号状态，**不看**
    ``usable`` 单独给 PASS。

    ``watchdog`` 是快照工具自己从探针原始信封重算的规范信号（同一口径，不另写
    一套规则）；``raw_mtime`` / ``backend_pid_file`` 只在该信号可用时给出，供
    ③/④ 做旁证。
    """
    out = {
        "requested": True, "usable": False, "reason": None, "detail": "",
        "signals": {}, "watchdog": None, "raw_txt": None,
        "backend_pid_file": None, "raw_mtime": None,
        "captured_at": None, "captured_at_iso": None,
        "elevated": None, "identity": None,
        "warnings": [], "rejected_signals": [],
        "source": {"path": None, "sha256": None, "encoding": None},
        "references": {"gateway_started_at": None,
                       "before_snapshot_captured_at": None,
                       "before_reading_captured_at": None},
    }
    if not isinstance(loaded, dict):
        out["reason"] = "authoritative_not_loaded"
        out["detail"] = "加载结果形状不对（不是字典）"
        return out
    out["source"] = {"path": loaded.get("path"),
                     "sha256": loaded.get("sha256"),
                     "encoding": loaded.get("encoding")}
    if loaded.get("error"):
        out["reason"] = str(loaded["error"])
        out["detail"] = "窗口后权威读数读不到／解不开（读不到 ≠ 通过）"
        return out
    readings = loaded.get("readings")
    if not isinstance(readings, dict):
        out["reason"] = "authoritative_not_an_object"
        out["detail"] = "权威读数不是 JSON 对象"
        return out
    tool = readings.get("tool")
    if tool != AUTHORITATIVE_TOOL:
        out["reason"] = "authoritative_tool_mismatch:%r" % (tool,)
        out["detail"] = ("该文件不是提升身份只读探针的 JSON（tool=%r ≠ %r）："
                         "合并过的快照请用 --before 口径，不在这里当权威读数"
                         % (tool, AUTHORITATIVE_TOOL))
        return out
    schema_version = readings.get("schema_version")
    if schema_version != AUTHORITATIVE_SCHEMA_VERSION:
        out["reason"] = ("authoritative_schema_version_mismatch:%r"
                         % (schema_version,))
        out["detail"] = "探针 schema 版本与合并口径不一致：不猜语义，先补取证据"
        return out
    if readings.get("read_only") is not True:
        out["warnings"].append("probe_not_marked_read_only:%r"
                               % (readings.get("read_only"),))
    try:
        merged, report = snapshot_tool.merge_authoritative_readings(
            {}, readings, source=out["source"])
    except ValueError as exc:
        out["reason"] = "authoritative_shape:%s" % (exc,)
        out["detail"] = "window_signals 形状不符（按合并口径拒绝，绝不半份接受）"
        return out
    signals = merged.get("window_signals")
    out["signals"] = signals if isinstance(signals, dict) else {}
    out["rejected_signals"] = report.get("rejected_signals") or []
    for warning in report.get("warnings") or []:
        if isinstance(warning, dict) and warning.get("reason"):
            out["warnings"].append(warning["reason"])
    rejected = _rejected_reasons(report)

    # ② 新鲜度：采集时刻不早于 8100 进程启动时间、不早于窗口前快照。
    # 求值顺序与判据编号一致：① 结构 → ② 新鲜度 → ③ 身份 → ④ 时间顺序；
    # 既过期又非提升身份的读数因此先得「过期」原因码（夹具钉住这条顺序）。
    captured_at = readings.get("captured_at")
    if not _number(captured_at):
        out["reason"] = ("authoritative_captured_at_unreadable:%r"
                         % (captured_at,))
        out["detail"] = "读数没有可用的采集时刻（captured_at）：新鲜度无从核对"
        return out
    out["captured_at"] = captured_at
    captured_at_iso = readings.get("captured_at_iso")
    out["captured_at_iso"] = captured_at_iso \
        if isinstance(captured_at_iso, str) and captured_at_iso else None
    startup = after.get("gateway_startup") if isinstance(after, dict) else None
    started_at = startup.get("started_at") if isinstance(startup, dict) else None
    out["references"]["gateway_started_at"] = \
        started_at if _number(started_at) else None
    if not _number(started_at):
        out["reason"] = ("authoritative_freshness_unverifiable:"
                         "gateway_started_at_unavailable")
        out["detail"] = "8100 进程启动时间读不到：无法证明读数采集于窗口后"
        return out
    if captured_at < started_at:
        out["reason"] = "authoritative_stale:before_8100_start"
        out["detail"] = ("读数采集时刻 %r 早于 8100 进程启动时间 %r："
                         "这是窗口前的旧读数，不得当窗口后读数"
                         % (captured_at, started_at))
        return out
    before_snapshot_at = _snapshot_captured_at(before)
    out["references"]["before_snapshot_captured_at"] = before_snapshot_at
    if before_snapshot_at is None:
        out["reason"] = ("authoritative_freshness_unverifiable:"
                         "before_snapshot_captured_at_unavailable")
        out["detail"] = "窗口前快照的采集时刻读不到：无法证明读数不早于窗口前快照"
        return out
    if captured_at < before_snapshot_at:
        out["reason"] = "authoritative_stale:before_snapshot"
        out["detail"] = ("读数采集时刻 %r 早于窗口前快照采集时刻 %r："
                         "这不是窗口后的读数" % (captured_at, before_snapshot_at))
        return out

    # ③ 身份：读数须自报采集身份，且必须是提升身份。
    elevated = readings.get("elevated")
    identity = readings.get("identity")
    out["elevated"] = elevated if isinstance(elevated, bool) else None
    out["identity"] = identity \
        if isinstance(identity, str) and identity else None
    if elevated is False:
        out["reason"] = "authoritative_not_elevated"
        out["detail"] = ("读数自报非提升身份（identity=%r）：非提升身份采的读数"
                         "不得当权威（读不到 ≠ 通过）" % (identity,))
        return out
    if elevated is not True or out["identity"] is None:
        out["reason"] = ("authoritative_identity_undeclared:elevated=%r/identity=%r"
                         % (elevated, identity))
        out["detail"] = "读数没有声明采集身份或身份名：无从核对，先补取证据"
        return out

    # ⑧ 的核心信号：探针原始信封里重算出来的规范 watchdog 信号。
    watchdog = out["signals"].get("watchdog_task")
    if isinstance(watchdog, dict):
        if watchdog.get("elevated") is False:
            out["reason"] = "authoritative_identity_conflict:watchdog_envelope"
            out["detail"] = ("顶层自报提升身份，但探针原始信封里的 elevated=False："
                             "两份读数互相矛盾，不得当权威")
            return out
        envelope_identity = watchdog.get("identity")
        if isinstance(envelope_identity, str) and envelope_identity \
                and envelope_identity != out["identity"]:
            out["reason"] = ("authoritative_identity_conflict:%r/%r"
                             % (out["identity"], envelope_identity))
            out["detail"] = ("顶层身份与探针原始信封里的身份不一致：两份读数"
                             "互相矛盾，不得当权威")
            return out
    if "watchdog_task" in rejected:
        out["reason"] = ("authoritative_signal_rejected:watchdog_task:%s"
                         % (rejected["watchdog_task"],))
        out["detail"] = "watchdog_task 形状不符（缺键 / 坏状态）：拒绝该读数"
        return out
    if not isinstance(watchdog, dict):
        out["reason"] = "authoritative_signal_missing:watchdog_task"
        out["detail"] = "读数里没有 watchdog_task 信号：无从判定第 ⑧ 层"
        return out
    out["watchdog"] = watchdog

    # ④ 时间顺序：窗口后读数不得早于窗口前读数。
    before_reading_at = _before_reading_captured_at(before)
    out["references"]["before_reading_captured_at"] = before_reading_at
    if before_reading_at is None:
        out["reason"] = ("authoritative_freshness_unverifiable:"
                         "before_reading_captured_at_unavailable")
        out["detail"] = ("窗口前快照没有合并过权威读数：无从核对前后时间顺序"
                         "（窗口前那份读数须单独新采集，见 --before 口径）")
        return out
    if captured_at < before_reading_at:
        out["reason"] = "authoritative_time_reversed:before_reading"
        out["detail"] = ("窗口后读数采集时刻 %r 早于窗口前读数采集时刻 %r："
                         "前后时间颠倒，拒绝该读数"
                         % (captured_at, before_reading_at))
        return out

    # ③ 的 raw.txt mtime 与 ④ 的 backend.pid 旁证：可用就用，不可用只记原因码。
    raw_txt = out["signals"].get("raw_txt")
    if "raw_txt" in rejected:
        out["warnings"].append("raw_txt_rejected:%s" % (rejected["raw_txt"],))
    elif isinstance(raw_txt, dict) and raw_txt.get("status") == "ok" \
            and _number(raw_txt.get("mtime")):
        out["raw_txt"] = raw_txt
        out["raw_mtime"] = raw_txt["mtime"]
    pid_file = out["signals"].get("backend_pid_file")
    if "backend_pid_file" in rejected:
        out["warnings"].append("backend_pid_file_rejected:%s"
                               % (rejected["backend_pid_file"],))
    elif isinstance(pid_file, dict) and pid_file.get("status") == "ok":
        out["backend_pid_file"] = pid_file
    out["usable"] = True
    out["detail"] = "四道校验全部通过（结构 / 新鲜度 / 身份 / 时间顺序）"
    return out


# ------------------------------------------------------------- 纯判定层（八层）
def _item(index, title, verdict, detail):
    return {"index": index, "title": title, "verdict": verdict,
            "detail": detail}


def _before_missing(before_error):
    if before_error:
        return "重启前快照读不到：%s" % before_error
    return "未给 --before 重启前快照"


def _layer_8096_health(before, after, before_error):
    index = "①"
    title = "8096 健康 200 且 PID 已变"
    health = after.get("health_8096")
    status = health.get("status") if isinstance(health, dict) else None
    body = health.get("json") if isinstance(health, dict) else None
    if status is None:
        return _item(index, title, "undecidable",
                     "8096 /health 未取到（error=%r）"
                     % (health.get("error")
                        if isinstance(health, dict) else None))
    if status != 200:
        # 状态读到了但非 200：这是「health 非 200」的 FAIL，不是不可判定。
        return _item(index, title, "fail",
                     "8096 /health 非 200：status=%r" % (status,))
    if not isinstance(body, dict):
        return _item(index, title, "undecidable",
                     "8096 /health 状态读到了但体不是 JSON 对象（body=%r）"
                     % (body,))
    if body.get("status") != "ok" or body.get("service") != "usagesrv" \
            or body.get("port") != CONSOLE_PORT:
        return _item(index, title, "fail",
                     "8096 /health 字段不符：status=%r body=%s"
                     % (status, json.dumps(body, ensure_ascii=False)[:200]))
    if before is None:
        return _item(index, title, "undecidable",
                     _before_missing(before_error) + "：无法证明 PID 已变")
    before_state, before_pids = _listener_state(before, CONSOLE_PORT)
    after_state, after_pids = _listener_state(after, CONSOLE_PORT)
    if after_state == "unknown":
        return _item(index, title, "undecidable", "重启后 8096 监听 PID 读不到")
    if after_state == "absent":
        return _item(index, title, "fail",
                     "health 200 但 8096 无人监听（读数自相矛盾）")
    if before_state != "ok":
        # 重启前 8096 无人监听／读不到（例如掉线期间或停→启之间取「前」快照）：
        # 没有旧 PID 可证「已变」，缺证据不得报 PASS。
        return _item(index, title, "undecidable",
                     "重启前 8096 监听证据不可用（before=%s）：无法证明 PID 已变"
                     % before_state)
    if set(before_pids) & set(after_pids):
        return _item(index, title, "fail",
                     "PID 未变：%s（新代码未载入）" % after_pids)
    return _item(index, title, "pass",
                 "health 200；PID %s → %s" % (before_pids, after_pids))


def _layer_read_surface(after):
    index = "②"
    title = "8096 读面已是 7 键新形状"
    share_status = after.get("share_status")
    keys = share_status.get("keys") if isinstance(share_status, dict) else None
    if not isinstance(keys, list) or not keys:
        return _item(index, title, "undecidable",
                     "重启后归因键集读不到（error=%r）"
                     % (share_status.get("error")
                        if isinstance(share_status, dict) else None))
    shape = snapshot_tool.attribution_shape(keys)
    if shape == "seven_key":
        return _item(index, title, "pass", "键集 = %s" % sorted(keys))
    if shape == "four_key":
        return _item(index, title, "fail",
                     "仍是 4 键旧形状（%s）：新代码未载入" % sorted(keys))
    return _item(index, title, "undecidable",
                 "键集不认识：%s" % sorted(keys))


def _authoritative_raw_mtime(authoritative, raw_path):
    """纯函数：权威读数里的 raw.txt mtime（路径须与本次采集的 raw_path 一致）。

    探针**不读** raw.txt 内容，只记路径、size 与 mtime；所以它只能替第 ③ 层
    补上「启动行是否早于当前 8100 进程启动时间」这条核对，**不能**补启动行的
    实际值（那要能读 raw.txt 内容，本开关不改变这一点）。路径不一致时不猜。
    """
    if not isinstance(authoritative, dict) or not authoritative.get("usable"):
        return None
    if not isinstance(raw_path, str) or not raw_path:
        return None
    raw_txt = authoritative.get("raw_txt")
    if not isinstance(raw_txt, dict) or raw_txt.get("path") != raw_path:
        return None
    mtime = raw_txt.get("mtime")
    return mtime if _number(mtime) else None


def attribution_fallback_evidence(after):
    """纯函数：启动行读不到时的等价权威证据 → ``(actual, reason, detail)``。

    消费 ``after["gateway_attribution"]``（``share_attribution_status()`` 的只读
    直调事实，快照工具 ``gateway_attribution_facts`` 的同一形状）：

    * 密钥就绪（``file_ready=True``）且 ``instance_loaded.value='yes'`` →
      ``actual="enabled"``（实际值「已启用（以密钥就绪 + 实例载入推断）」）；
    * 密钥未就绪（``file_ready=False``）且 ``instance_loaded.value`` 为空/``unknown``
      且 ``instance_loaded.reason='key_file_not_ready'`` → ``actual="disabled"``
      （实际值「未启用（密钥未就绪）」；这是密钥未生成时的预期状态，不是故障）；
    * 密钥未就绪但 ``instance_loaded.value`` 带着非空值（``yes``/``no`` 等）→
      ``actual=None``（防御加固：该组合生产不可达——文件未就绪时没有 mtime 可比，
      探测不会被调用——事实自相矛盾时不得按「未启用」放行，先补证据）；
    * 密钥就绪但实例载入为 ``no``/``unknown`` → ``actual=None``（先补证据）；
    * 读不到任何一项（缺 ``gateway_attribution``／带 ``error``／字段不全）→
      ``actual=None``（先补证据）。

    ``reason`` 是判定依据的原因码（写进 detail）；``detail`` 是事实一行。
    """
    facts = after.get("gateway_attribution") if isinstance(after, dict) else None
    if not isinstance(facts, dict):
        return None, "attribution_facts_missing", "gateway_attribution 事实未取到"
    if facts.get("error"):
        return (None, "attribution_facts_unreadable:%s" % (facts.get("error"),),
                "gateway_attribution 事实读不到（error=%r）" % (facts.get("error"),))
    file_ready = facts.get("file_ready")
    loaded = facts.get("instance_loaded_value")
    loaded_reason = facts.get("instance_loaded_reason")
    detail = ("file_ready=%r file_reason=%r instance_loaded.value=%r "
              "instance_loaded.reason=%r key_file=%r"
              % (file_ready, facts.get("file_reason"), loaded,
                 loaded_reason, facts.get("key_file")))
    if file_ready is True and loaded == "yes":
        return "enabled", "ok", detail
    if file_ready is False:
        # 防御加固（执行轮 14）：file_ready=False 时 instance_loaded.value 只可能
        # 是空/unknown（没有 mtime 可比 → 探测不会被调用）；带着非空值说明事实
        # 自相矛盾（该组合生产不可达），不得按「未启用」放行，记 undecidable。
        if loaded not in (None, "unknown"):
            return (None,
                    "facts_inconsistent:file_not_ready_but_instance_loaded_%s"
                    % (loaded,), detail)
        if loaded_reason == "key_file_not_ready":
            return "disabled", "ok", detail
    if file_ready is True and loaded in ("no", "unknown"):
        return None, "instance_loaded_%s" % (loaded,), detail
    return None, "facts_incomplete", detail


def _layer_gateway(after, authoritative=None):
    index = "③"
    title = "8100 仍在监听且启动行如实报归因状态"
    state, pids = _listener_state(after, GATEWAY_PORT)
    if state == "unknown":
        return _item(index, title, "undecidable", "8100 监听 PID 读不到")
    if state == "absent":
        return _item(index, title, "fail", "8100 已掉线（无人监听）")
    startup = after.get("gateway_startup")
    startup = startup if isinstance(startup, dict) else {}
    attribution = startup.get("attribution")
    actual = {None: None, "enabled": "已启用", "disabled": "未启用"}.get(
        attribution, attribution)
    raw_mtime = startup.get("raw_mtime")
    mtime_source = "本工具采集"
    if not _number(raw_mtime):
        # 普通权限下 raw.txt 可能读不到（SYSTEM 所有）：提升身份探针的 mtime
        # 只能补这条核对，补不了启动行的实际值（探针不读 raw.txt 内容）。
        fallback = _authoritative_raw_mtime(authoritative, startup.get("raw_path"))
        if fallback is not None:
            raw_mtime, mtime_source = fallback, "窗口后权威读数"
    started_at = startup.get("started_at")
    stale = (isinstance(raw_mtime, (int, float))
             and not isinstance(raw_mtime, bool)
             and isinstance(started_at, (int, float))
             and not isinstance(started_at, bool)
             and raw_mtime < started_at - STARTUP_STALE_TOLERANCE_S)
    detail = ("监听 pid=%s；启动行实际值 attribution=%r（%r）；"
              "line=%r raw_path=%r raw_mtime=%r 8100_started_at=%r "
              "stale=%r reason=%r error=%r"
              % (pids, attribution, actual, startup.get("line"),
                 startup.get("raw_path"), raw_mtime, started_at, stale,
                 startup.get("reason"), startup.get("error")))
    if authoritative is not None:
        # 给了 --authoritative 才加这一段：不提供时第 ③ 层的文案不含接缝
        # 这一段（第 ③ 层自身因同轮缺陷修复已改，见模块开头第 ③ 层说明）。
        detail += ("；raw_mtime_source=%r 权威读数 usable=%r reason=%r"
                   % (mtime_source, authoritative.get("usable"),
                      authoritative.get("reason")))
    if stale:
        # raw.txt 早于当前 8100 进程的启动时间：这条行不可能是当前实例的启动行
        # （陈旧行），不得当它的实际值用；缺证据不报 PASS。
        return _item(index, title, "undecidable",
                     detail + "：启动行早于当前 8100 进程启动时间（陈旧行）")
    if attribution in ("enabled", "disabled"):
        if not _number(started_at):
            # 8100 进程启动时间读不到：无法核对这条启动行是否属于当前实例，
            # 缺证据不报 PASS（复核者 2026-09-28 发现的窄类假 PASS）。
            return _item(index, title, "undecidable",
                         detail + "：8100 进程启动时间读不到，无法核对启动行是否属于当前实例")
        return _item(index, title, "pass", detail)
    # 启动行读不到（本拓扑下结构性为空 / 读不到 / 值不认识）：改用等价且权威的
    # 证据——share_attribution_status() 的 file_ready / instance_loaded 事实。
    evidence_actual, evidence_reason, evidence_detail = \
        attribution_fallback_evidence(after)
    fallback_detail = ("%s；%s；等价权威证据（share_attribution_status 事实）：%s"
                      % (detail, STRUCTURAL_EMPTY_NOTE, evidence_detail))
    if evidence_actual == "enabled":
        return _item(index, title, "pass",
                     "%s；实际值=%s（reason=%s）"
                     % (fallback_detail, ATTRIBUTION_ENABLED_INFERRED,
                        evidence_reason))
    if evidence_actual == "disabled":
        return _item(index, title, "pass",
                     "%s；实际值=%s（reason=%s）"
                     % (fallback_detail, ATTRIBUTION_DISABLED_KEY_NOT_READY,
                        evidence_reason))
    return _item(index, title, "undecidable",
                 "%s；实际值读不到（reason=%s）"
                 % (fallback_detail, evidence_reason))


def _pid_state_detail(after):
    """纯函数：``backend_pid`` 事实 → 一行详情（只读；不删任何文件、不写任何文件）。"""
    facts = after.get("backend_pid")
    if not isinstance(facts, dict):
        return "backend.pid 事实未取到"
    return ("backend.pid state=%r pid_file_present=%r pid=%r "
            "pid_matches=%r port_listening=%r error=%r"
            % (facts.get("state"), facts.get("pid_file_present"),
               facts.get("pid"), facts.get("pid_matches"),
               facts.get("port_listening"), facts.get("error")))


def _authoritative_pid_detail(authoritative):
    """纯函数：权威读数里 ``backend.pid`` 的旁证一行（**不参与**第 ④ 层判定）。

    第 ④ 层的判定仍由 ``backend_observe`` 的 ``running`` 与 ``auth_state``
    决定；探针不给 ``auth_state``，所以它只把提升身份读到的 ``backend.pid``
    身份（路径 / 内容 / PID / 路径来源）照实打印出来，供复核者对照。
    """
    if not isinstance(authoritative, dict) or not authoritative.get("requested"):
        return "（未给 --authoritative）"
    if not authoritative.get("usable"):
        return ("不可用（reason=%s）；读不到 ≠ 通过"
                % (authoritative.get("reason"),))
    pid_file = authoritative.get("backend_pid_file")
    if not isinstance(pid_file, dict):
        return "backend.pid 信号不可用（读数里没有它，或形状被拒）"
    return ("backend.pid 身份旁证：path=%r content=%r pid=%r "
            "path_source=%r reason=%r"
            % (pid_file.get("path"), pid_file.get("content"),
               pid_file.get("pid"), pid_file.get("path_source"),
               pid_file.get("reason")))


def _layer_backend(before, after, before_error, authoritative=None):
    index = "④"
    title = "8110 仍在监听、auth_state 与重启前一致"
    # 只在给了 --authoritative 时带这一段：不提供时第 ④ 层文案不含接缝这一段。
    suffix = ""
    if authoritative is not None:
        suffix = "；窗口后权威读数 %s" % _authoritative_pid_detail(
            authoritative)
    state, pids = _listener_state(after, BACKEND_PORT)
    if state == "unknown":
        return _item(index, title, "undecidable", "8110 监听 PID 读不到" + suffix)
    if state == "absent":
        return _item(index, title, "fail", "8110 已掉线（无人监听）" + suffix)
    backend = after.get("backend_observe")
    if not isinstance(backend, dict):
        return _item(index, title, "undecidable", "8110 观察未取到" + suffix)
    if backend.get("running") is not True:
        # running 不是 True 时把 pid 文件的只读事实一起打印：陈旧 pid 文件
        # 照实报成 pid_mismatch / pid_gone，且它没有被删除（只读开关）。
        return _item(index, title, "fail",
                     "8110 running=%r（存活无法确认）；%s%s"
                     % (backend.get("running"), _pid_state_detail(after),
                        suffix))
    after_state = backend.get("auth_state")
    if after_state is None:
        return _item(index, title, "undecidable",
                     "重启后 auth_state 读不到" + suffix)
    if before is None:
        return _item(index, title, "undecidable",
                     _before_missing(before_error) + "：无法比对 auth_state"
                     + suffix)
    before_observe = before.get("backend_observe")
    before_state = before_observe.get("auth_state") \
        if isinstance(before_observe, dict) else None
    if before_state is None:
        return _item(index, title, "undecidable",
                     "重启前 auth_state 读不到" + suffix)
    detail = "监听 pid=%s；auth_state %r → %r；%s%s" \
        % (pids, before_state, after_state, _pid_state_detail(after), suffix)
    if before_state != after_state:
        return _item(index, title, "fail", detail)
    return _item(index, title, "pass", detail)


def _layer_share_entry(after):
    index = "⑤"
    title = "分享入口白名单外 404 route_not_shared、白名单内 401 invalid_api_key"
    probes = after.get("share_entry_probe")
    if not isinstance(probes, dict):
        return _item(index, title, "undecidable", "入口探测未取到")
    details = []
    for name, path, want_status, want_code in snapshot_tool.SHARE_ENTRY_EXPECTED:
        probe = probes.get(name)
        if not isinstance(probe, dict):
            return _item(index, title, "undecidable", "探测结果缺失：%s" % name)
        if probe.get("status") is None:
            return _item(index, title, "undecidable",
                         "探测失败：%s %s error=%r"
                         % (name, path, probe.get("error")))
        if (probe.get("status"), probe.get("code")) != (want_status, want_code):
            return _item(index, title, "fail",
                         "%s %s 期望 %d %s，实测 %r %r"
                         % (name, path, want_status, want_code,
                            probe.get("status"), probe.get("code")))
        details.append("%s %s %s→%s"
                       % (name, path, probe.get("status"), probe.get("code")))
    return _item(index, title, "pass", "；".join(details))


def _layer_funnel(before, after, before_error):
    index = "⑥"
    title = "Funnel 状态与重启前一致"
    share_status = after.get("share_status")
    after_on = share_status.get("funnel_on") \
        if isinstance(share_status, dict) else None
    if not isinstance(after_on, bool):
        return _item(index, title, "undecidable", "重启后 funnel_on 读不到")
    if before is None:
        return _item(index, title, "undecidable",
                     _before_missing(before_error) + "：无法比对 funnel_on")
    before_status = before.get("share_status")
    before_on = before_status.get("funnel_on") \
        if isinstance(before_status, dict) else None
    if not isinstance(before_on, bool):
        return _item(index, title, "undecidable", "重启前 funnel_on 读不到")
    detail = "funnel_on %r → %r" % (before_on, after_on)
    if before_on != after_on:
        return _item(index, title, "fail", detail + "（由开变关即停止点）")
    return _item(index, title, "pass", detail)


def _layer_share_ledger(before, after, before_error):
    index = "⑦"
    title = "8110 账本行数只增不减、已归属行不变、账本路径不变"
    ledger = after.get("share_ledger")
    after_rows = ledger.get("rows") if isinstance(ledger, dict) else None
    if not isinstance(after_rows, int) or isinstance(after_rows, bool):
        return _item(index, title, "undecidable",
                     "重启后账本行数读不到（error=%r）"
                     % (ledger.get("error") if isinstance(ledger, dict)
                        else None))
    if before is None:
        return _item(index, title, "undecidable",
                     _before_missing(before_error) + "：无法比对账本")
    before_ledger = before.get("share_ledger")
    if not isinstance(before_ledger, dict):
        return _item(index, title, "undecidable", "重启前账本事实读不到")
    before_rows = before_ledger.get("rows")
    if not isinstance(before_rows, int) or isinstance(before_rows, bool):
        return _item(index, title, "undecidable", "重启前账本行数读不到")
    before_path = before_ledger.get("path")
    after_path = ledger.get("path") if isinstance(ledger, dict) else None
    if not isinstance(before_path, str) or not isinstance(after_path, str):
        return _item(index, title, "undecidable",
                     "账本路径读不到（before=%r after=%r）：无法确认是同一份账本"
                     % (before_path, after_path))
    before_attributed = before_ledger.get("attributed_rows")
    after_attributed = ledger.get("attributed_rows") \
        if isinstance(ledger, dict) else None
    if not isinstance(before_attributed, list) \
            or not isinstance(after_attributed, list):
        return _item(index, title, "undecidable", "已归属行证据缺失")
    detail = ("账本路径 %r 不变；行数 %d → %d；已归属行 %d 条"
              % (after_path, before_rows, after_rows,
                 len(before_attributed)))
    if before_path != after_path:
        return _item(index, title, "fail",
                     "账本路径变了：%r → %r（可能换了新文件）"
                     % (before_path, after_path))
    if after_rows < before_rows:
        return _item(index, title, "fail", detail + "（行数减少）")
    after_by_index = {}
    for row in after_attributed:
        if isinstance(row, dict) and isinstance(row.get("index"), int):
            after_by_index[row["index"]] = row
    for row in before_attributed:
        if not isinstance(row, dict) or not isinstance(row.get("index"), int):
            return _item(index, title, "undecidable", "已归属行缺少 index")
        current = after_by_index.get(row["index"])
        if current is None:
            return _item(index, title, "fail",
                         "已归属行 #%d 丢失" % row["index"])
        if (current.get("key_id"), current.get("attribution")) \
                != (row.get("key_id"), row.get("attribution")):
            return _item(index, title, "fail",
                         "已归属行 #%d 被改写" % row["index"])
    return _item(index, title, "pass",
                 detail + "；重启前已归属行全部保留")


def _watchdog_state_text(state):
    """纯函数：State 数字 → 标签（走快照工具的同一份映射，不另抄字面量）。"""
    if isinstance(state, int) and not isinstance(state, bool):
        return snapshot_tool.WATCHDOG_TASK_STATES.get(state)
    return None


def _authoritative_watchdog_line(authoritative, signal):
    """纯函数：权威读数 watchdog 信号的来源与读数一行。"""
    source = authoritative.get("source") or {}
    state_text = signal.get("state_text") \
        or _watchdog_state_text(signal.get("state"))
    return ("窗口后权威读数（path=%r sha256=%r encoding=%r elevated=%r "
            "identity=%r captured_at_iso=%r）：status=%r state=%r "
            "state_text=%r reason=%r"
            % (source.get("path"), source.get("sha256"),
               source.get("encoding"), authoritative.get("elevated"),
               authoritative.get("identity"),
               authoritative.get("captured_at_iso"), signal.get("status"),
               signal.get("state"), state_text, signal.get("reason")))


def _layer_watchdog(after, authoritative=None):
    index = "⑧"
    title = "watchdog 任务状态（%s）" % WATCHDOG_TASK_NAME
    if isinstance(authoritative, dict) and authoritative.get("requested"):
        # 给了 --authoritative 就以窗口后权威读数为准（提升身份采的），**绝不**
        # 退回自身身份那次读数——那正是本开关要避免的假 PASS 来源。
        if not authoritative.get("usable"):
            return _item(index, title, "undecidable",
                         "窗口后权威读数不可用：reason=%s；%s；%s"
                         % (authoritative.get("reason"),
                            authoritative.get("detail"),
                            snapshot_tool.GUIDANCE_UNDECIDABLE))
        signal = authoritative.get("watchdog")
        if not isinstance(signal, dict):
            return _item(index, title, "undecidable",
                         "窗口后权威读数里没有 watchdog 信号：%s"
                         % snapshot_tool.GUIDANCE_UNDECIDABLE)
        detail = _authoritative_watchdog_line(authoritative, signal)
        status = signal.get("status")
        state = signal.get("state")
        if status == "ok" and state == WATCHDOG_RUNNING_STATE:
            return _item(index, title, "pass", detail + "：状态=Running")
        if status == "ok":
            return _item(index, title, "fail",
                         detail + "：状态=%r（应为 Running；分享开放时 watchdog "
                         "必须武装）" % (_watchdog_state_text(state),))
        if status == "absent":
            return _item(index, title, "fail",
                         detail + "：提升身份下的权威读数=未注册"
                         "（分享链失去看护）")
        return _item(index, title, "undecidable",
                     detail + "：权威读数读不到（读不到 ≠ 通过）；%s"
                     % snapshot_tool.GUIDANCE_UNDECIDABLE)
    watchdog = after.get("watchdog")
    if not isinstance(watchdog, dict):
        return _item(index, title, "undecidable", "watchdog 事实未取到")
    state = watchdog.get("state")
    if not isinstance(state, str) or not state:
        return _item(index, title, "undecidable",
                     "读不到（query=%r error=%r）"
                     % (watchdog.get("query"), watchdog.get("error")))
    if state == "Running":
        return _item(index, title, "pass",
                     "状态=Running（query=%s）" % watchdog.get("query"))
    return _item(index, title, "fail",
                 "状态=%r（应为 Running；分享开放时 watchdog 必须武装）" % state)


def evaluate(before, after, before_error=None, authoritative=None):
    """纯函数：重启前快照（可为 ``None``）+ 重启后事实 → 八层判定。

    返回 ``{"items": [...], "ok": bool, "exit_code": 0|2}``：八层全 ``pass`` 才 0；
    任一 ``fail``/``undecidable`` 都是 2（缺层绝不宣布成功）。

    ``authoritative`` 是 ``authoritative_evidence`` 的产物（``--authoritative``
    给了才有）：给了它，第 ⑧ 层以那份提升身份读数为准，不可用即 ``undecidable``；
    ③ 用它补 raw.txt mtime，④ 只把它当 ``backend.pid`` 的旁证。不给（``None``）时
    接缝不参与判定：第 ⑧ 层走自身身份读数（读不到即 ``undecidable``）。
    """
    if not isinstance(after, dict):
        # 采集层保证返回字典；这一句是防御性的：形状不对时八层全部如实记
        # undecidable（读不到 ≠ 通过），绝不在这里抛异常冒充成功。
        after = {}
    items = [
        _layer_8096_health(before, after, before_error),
        _layer_read_surface(after),
        _layer_gateway(after, authoritative),
        _layer_backend(before, after, before_error, authoritative),
        _layer_share_entry(after),
        _layer_funnel(before, after, before_error),
        _layer_share_ledger(before, after, before_error),
        _layer_watchdog(after, authoritative),
    ]
    ok = all(item["verdict"] == "pass" for item in items)
    return {"items": items, "ok": ok, "exit_code": 0 if ok else 2}


# ----------------------------------------------------------------- 打印层
def _authoritative_summary(authoritative_path, authoritative):
    """报告用的一行：路径、sha256、编码与四道校验的读数（只字段与原因码）。"""
    if not isinstance(authoritative, dict):
        return "%s（未校验）" % (authoritative_path,)
    source = authoritative.get("source") or {}
    references = authoritative.get("references") or {}
    parts = ["path=%s" % (authoritative_path,),
             "sha256=%s" % (source.get("sha256"),),
             "encoding=%r" % (source.get("encoding"),),
             "usable=%r" % (authoritative.get("usable"),),
             "reason=%s" % (authoritative.get("reason"),),
             "elevated=%r" % (authoritative.get("elevated"),),
             "identity=%r" % (authoritative.get("identity"),),
             "captured_at_iso=%r" % (authoritative.get("captured_at_iso"),),
             "8100_started_at=%r"
             % (references.get("gateway_started_at"),),
             "before_snapshot_captured_at=%r"
             % (references.get("before_snapshot_captured_at"),),
             "before_reading_captured_at=%r"
             % (references.get("before_reading_captured_at"),)]
    warnings = authoritative.get("warnings") or []
    if warnings:
        parts.append("warnings=%s" % ("，".join(str(w) for w in warnings),))
    return "；".join(parts)


def format_report(after, result, before_path=None, authoritative_path=None,
                  authoritative=None):
    """打印八层判定与总判定；只给字段与原因码，绝不打印密钥内容。"""
    lines = ["== 8096 重启后八层验收（只读；动作门 §③.1b + §③.3 + §④） =="]
    lines.append("重启前快照（--before）：%s"
                 % (before_path or "（未给）"))
    if authoritative_path:
        # 只在给了 --authoritative 时多打这一行：不提供时报告不含这一行
        # （第 ③ 层 detail 的同轮变化与本接缝无关，见模块开头第 ③ 层说明）。
        lines.append("窗口后权威读数（--authoritative）：%s"
                     % _authoritative_summary(authoritative_path,
                                              authoritative))
    for item in result["items"]:
        lines.append("[%s] %s —— %s"
                     % (item["verdict"].upper(), item["title"],
                        item["detail"]))
    watchdog = after.get("watchdog")
    if isinstance(watchdog, dict):
        lines.append("watchdog 实际读数：state=%r query=%r error=%r"
                     % (watchdog.get("state"), watchdog.get("query"),
                        watchdog.get("error")))
    if isinstance(authoritative, dict) and authoritative.get("requested") \
            and authoritative.get("usable"):
        signal = authoritative.get("watchdog")
        if isinstance(signal, dict):
            lines.append("窗口后权威读数 watchdog：state=%r state_text=%r "
                         "elevated=%r identity=%r"
                         % (signal.get("state"), signal.get("state_text"),
                            authoritative.get("elevated"),
                            authoritative.get("identity")))
    lines.append("=" * 60)
    if result["ok"]:
        lines.append("结论：八层全部 PASS（退出码 0）")
    else:
        not_pass = [item["index"] for item in result["items"]
                    if item["verdict"] != "pass"]
        lines.append("结论：未完成核对——非 PASS 层 %s（退出码 2；缺层不宣布成功）"
                     % not_pass)
    return "\n".join(lines)


def format_plan(before_path=None, authoritative_path=None):
    """dry-run 打印：将查的八层、读法与判据；不读任何端口/文件/任务。"""
    lines = ["== 8096 重启后八层验收 · dry-run（只打印将查什么；不读任何现场） =="]
    lines.append("--check 才真查；以下全部只读。八层与判据：")
    lines.append("  ① 8096 健康 200 且 PID 已变 —— 不带凭据 GET "
                 "http://127.0.0.1:8096/health 须 200 "
                 "{status:ok,service:usagesrv,port:8096}（非 200 = FAIL、200 但体"
                 "不是 JSON 对象 = UNDECIDABLE）；netstat -ano -p TCP 取 8096 "
                 "LISTENING PID，与重启前快照比对：PID 未变 = FAIL；重启前 "
                 "8096 无监听／读不到 = UNDECIDABLE（没有旧 PID 可证「已变」）。")
    lines.append("  ② 8096 读面 7 键新形状 —— GET "
                 "/api/console/share-management/status 的 attribution 键集须 = %s；"
                 "仍为 4 键旧形状（%s）= FAIL；读不到 = UNDECIDABLE。"
                 % (sorted(snapshot_tool.SEVEN_ATTRIBUTION_KEYS),
                    sorted(snapshot_tool.LEGACY_ATTRIBUTION_KEYS)))
    lines.append("  ③ 8100 仍在监听 + 启动行实际值 —— netstat 取 8100 "
                 "LISTENING PID；只读 <RunRoot>\\run\\live\\raw.txt 取最后一条"
                 "「%s」行：实际值「已启用」或「未启用」都合法（必须打印），"
                 "启动行早于当前 8100 进程启动时间（陈旧行）= UNDECIDABLE。"
                 "本拓扑下 raw.txt 结构性为空（share_guard.py:488 的 "
                 "CreateProcessW 第 5 参 bInheritHandles=False 且该文件无 "
                 "print(；deploy-elevated.ps1:536 把 guard 的 stdout 重定向到"
                 "raw.txt）——这是拓扑事实，不是窗口失败：启动行读不到时改用 "
                 "share_attribution_status() 的 file_ready / instance_loaded 事实"
                 "（密钥就绪 + 实例载入 yes → PASS「已启用（以密钥就绪 + 实例"
                 "载入推断）」；密钥未就绪 + reason=key_file_not_ready → PASS"
                 "「未启用（密钥未就绪）」；密钥就绪但实例载入 no/unknown 或"
                 "事实读不到 = UNDECIDABLE）。RunRoot 由产品解析器给出（默认 "
                 "C:\\ProgramData\\UMShareRun）。" % STARTUP_MARKER)
    lines.append("  ④ 8110 仍在监听、auth_state 与重启前一致 —— netstat 取 "
                 "8110 LISTENING PID；production_backend_observe("
                 "forget_stale=False) 的 running=true 且 auth_state 与重启前"
                 "快照一致；running 不是 true 时一并打印 "
                 "production_backend_pid_state() 的只读 pid 事实（陈旧 "
                 "backend.pid 照实报 pid_stale，且不被删除）；"
                 "读不到 = UNDECIDABLE。")
    lines.append("  ⑤ 分享入口 404/401 —— 不带凭据 GET 8100 的 / 与 /health "
                 "须 404 route_not_shared；GET /v1/models 须 401 "
                 "invalid_api_key；读不到 = UNDECIDABLE。")
    lines.append("  ⑥ Funnel 状态与重启前一致 —— GET "
                 "/api/console/share-management/status 的 funnel_on 与重启前快照"
                 "一致；由开变关 = FAIL；读不到 = UNDECIDABLE。")
    lines.append("  ⑦ 8110 账本行数只增不减、已归属行不变、账本路径不变 —— "
                 "账本路径取 production_backend_observe("
                 "forget_stale=False).attribution."
                 "request_attribution_detail.ledger_file；行数须 ≥ 重启前快照、"
                 "重启前已归属行（按 index/key_id/attribution）须逐条保留、"
                 "账本路径须与重启前相同（换新文件 = FAIL）；"
                 "读不到 = UNDECIDABLE。")
    lines.append("  ⑧ watchdog 任务状态 —— Get-ScheduledTask -TaskName "
                 "'%s'（回退 schtasks /query）；状态 Running = PASS、其他 = FAIL、"
                 "读不到（非提升常见「拒绝访问」）= UNDECIDABLE，绝不记成"
                 "「未注册」。" % WATCHDOG_TASK_NAME)
    lines.append("重启前快照（--before）：%s；--check 时读取：不给或读不到时 "
                 "①④⑥⑦ 如实记 UNDECIDABLE。" % (before_path or "（未给）"))
    if authoritative_path:
        # 只在给了 --authoritative 时多打这一段：不提供时计划文本不含接缝这一段。
        lines.append("窗口后权威读数（--authoritative）：%s；第 ⑧ 层（以及 ③ 的 "
                     "raw.txt mtime、④ 的 backend.pid 旁证）以该读数为准，读数须按 "
                     "①②③④ 的顺序过四道校验——① 结构可用（tool / schema_version / "
                     "window_signals 与快照同 schema；需要原始信封重算的 watchdog_task "
                     "缺可解析信封即 UNDECIDABLE，authoritative_envelope_missing / "
                     "authoritative_envelope_unparsable）、② 新鲜度（captured_at 不早于"
                     "当前 8100 进程启动时间、不早于窗口前快照的 captured_at）、"
                     "③ 身份（须声明提升身份；非提升身份采的读数不得当权威）、"
                     "④ 时间顺序（窗口后读数不得早于窗口前读数，颠倒即拒绝）；"
                     "任一条不满足 → 该层记 UNDECIDABLE 并给原因码（文案「先补取证据，"
                     "不得报通过」），**不得报 PASS**。"
                     % (authoritative_path,))
    lines.append("只读边界：HTTP 不带凭据且只连 127.0.0.1；进程只跑 netstat / "
                 "Get-CimInstance；watchdog 只跑 Get-ScheduledTask（回退 "
                 "schtasks）；raw.txt 与账本只读打开；**不删任何文件、不写任何"
                 "文件**（委托观察一律走 forget_stale=False 的只读档）；本工具"
                 "自己不写任何文件、不重启、不生成密钥、不发模型请求、不改配置。"
                 + ("；--authoritative 只多读一个 JSON 文件（%s），不写、不删。"
                    % (authoritative_path,) if authoritative_path else ""))
    lines.append("总判定：任一层 FAIL 或 UNDECIDABLE 即退出码 2 并打印"
                 "「未完成核对」；只有八层全部 PASS 才 0。")
    lines.append("本 dry-run 不读任何现场，退出码 0（dry-run 不是判定）。")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="8096 重启后八层验收（默认 dry-run；--check 才真查且只读）")
    parser.add_argument("--check", action="store_true",
                        help="真实只读核对（默认 dry-run 只打印将查什么）")
    parser.add_argument("--before", default=None, metavar="PATH",
                        help="重启前快照 JSON（share_chain_snapshot.py --out "
                             "写出的那份）；不给时 ①④⑥⑦ 记 UNDECIDABLE")
    parser.add_argument("--authoritative", default=None, metavar="PATH",
                        help="窗口后权威读数 JSON（提升身份只读探针 "
                             "tools/um-share/authoritative_readings.ps1 -Out "
                             "写出的那份）；给定时第 ⑧ 层（及 ③ 的 raw.txt "
                             "mtime、④ 的 backend.pid 旁证）以它为准，读数须先过"
                             "结构/新鲜度/身份/时间顺序四道校验，不可用即 "
                             "UNDECIDABLE；不给时接缝不参与判定")
    args = parser.parse_args(argv)

    if not args.check:
        print(format_plan(args.before, args.authoritative))
        return 0

    before, before_error = None, None
    if args.before:
        before, before_error = read_before_snapshot(args.before)
    loaded = None
    if args.authoritative:
        try:
            loaded = load_authoritative_readings(args.authoritative)
        except Exception as exc:  # noqa: BLE001 — 读不到是证据，不是崩溃
            loaded = {"path": args.authoritative, "sha256": None,
                      "encoding": None, "readings": None,
                      "error": "authoritative_load_failed:%s"
                               % type(exc).__name__}
    try:
        after = collect_facts()
    except Exception as exc:  # noqa: BLE001 — 采集失败如实报非零
        # 采集失败也是「未完成核对」：退出码 2，且 stdout 同样打印这四个字，
        # 不让这条路径成为唯一一个非零却不打印它的出口。
        print("[验收] 采集失败：%s: %s" % (type(exc).__name__, exc),
              file=sys.stderr)
        print("== 8096 重启后八层验收（只读；动作门 §③.1b + §③.3 + §④） ==")
        print("结论：未完成核对——采集失败：%s: %s（退出码 2）"
              % (type(exc).__name__, exc))
        return 2
    authoritative = None
    if args.authoritative:
        # 纯函数：字典进、字典出；读数的四道校验在这里，判定层只消费结论。
        authoritative = authoritative_evidence(loaded, after, before)
    result = evaluate(before, after, before_error, authoritative)
    print(format_report(after, result, args.before, args.authoritative,
                        authoritative))
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
