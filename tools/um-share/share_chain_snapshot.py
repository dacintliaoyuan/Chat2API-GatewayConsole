# -*- coding: utf-8 -*-
"""分享链现场只读快照 + 8096 重启前后核对（动作门 ③.1b 的工具化）。

背景材料：``output/ai-gw-round6-20260927/metering-action-gate.md`` 的精确动作门
（① 载入前检查 → ② 生成密钥 → ③ 受控重启 → ④ 三事实验收 → ⑤ 回退）。
其中 §③.1b 要求 8096 重启**前后各做一次**只读核对，并把结果逐项比对。本工具
把那次核对压成三步（授权窗口内只需跑这三条命令）：

    python ai-gateway/tools/um-share/share_chain_snapshot.py --out <轮目录>/snapshot-before.json
    # …执行 §③.1 的 8096 重启（本工具不重启任何东西）…
    python ai-gateway/tools/um-share/share_chain_snapshot.py --out <轮目录>/snapshot-after.json
    python ai-gateway/tools/um-share/share_chain_snapshot.py --compare <轮目录>/snapshot-before.json <轮目录>/snapshot-after.json

提升身份的权威读数（执行轮 10，2026-09-28；维护者要求「窗口前必须有有权限的权威读数」）：

非提升身份下 ``window_signals.watchdog_task`` 只能记 ``undecidable``（SYSTEM 任务
看不见），WFP/防火墙 limits 与 8100 的 ``raw.txt`` 更是完全读不到；而 ``--compare``
要求两侧都有可读状态。所以窗口前后各跑一次**提升身份只读探针**
``tools/um-share/authoritative_readings.ps1``（它复用 ``um-share-deploy`` 里看门狗自己的
只读 ``Get-LimitsState`` / ``Get-FirewallState``，不另写一套语义），再用
``--authoritative`` 把那份权威读数**并进快照**：

    # 提升身份（窗口前 / 窗口后各一次；探针默认只打印，-Out 才写文件）
    pwsh -NoProfile -ExecutionPolicy Bypass -File `
      ai-gateway/tools/um-share/authoritative_readings.ps1 `
      -Out <轮目录>/authoritative-before.json
    python ai-gateway/tools/um-share/share_chain_snapshot.py `
      --out <轮目录>/snapshot-before.json `
      --authoritative <轮目录>/authoritative-before.json

合并是**加性**的：不提供 ``--authoritative`` 时快照与判定与现在逐字相同；提供时
``window_signals`` 里同名信号被权威读数替换，并在 ``authoritative_readings`` 里记录
**来源**（工具名、路径、sha256、编码、提升身份与身份名）与**采集时刻**（探针的
``captured_at`` / ``captured_at_iso`` 与本工具的 ``merged_at``）。``watchdog_task`` 还会用
本工具自己的 ``parse_watchdog_query`` + ``watchdog_task_signal`` 从探针带出的原始信封
**重算**；信封缺失（``query`` 不是对象 / 缺字段）或解析不出时按 ``undecidable`` 记并给
原因码（``authoritative_envelope_missing`` / ``authoritative_envelope_unparsable``），重算与
探针结论不一致时同样按 ``undecidable`` 记（绝不把不一致折成通过）——**绝不**只凭自报字段给
PASS。
形状不符的条目会被**拒绝**并如实记进 ``rejected_signals``，绝不半份接受。
探针文件读不到／解不开时 ``--out`` 退出码 2 且**不写快照**（绝不写一份「看起来有权威
读数」的快照）；读入编码依次试 UTF-8（带/不带 BOM）、UTF-16、系统 ANSI 代码页，并把
实际用的编码记进来源，便于复核。

``--compare`` 另增 3 项（只在任一侧含这三类权威信号时纳入）：WFP/防火墙 limits、
8100 ``raw.txt`` 的路径与 mtime、8110 ``backend.pid`` 的字节内容。两侧都没跑探针时
不纳入，输出里打印一行说明；**窗口前后都要跑探针并各自合并**，否则缺失侧记
``undecidable``（读不到 ≠ 通过）。判定文案按维护者要求：``undecidable`` 打印
「先补取证据，不得报通过」，``fail`` 才打印「按 §⑤ 回退」。

只读边界（本工具的全部承诺；2026-09-28 复审执行轮 9/10 后收紧；**不是全目录零写**）：

* 本工具自身：HTTP 只发**不带凭据**的 ``GET http://127.0.0.1:<本机端口>``；
  进程查询只跑 ``netstat -ano -p TCP`` 与 ``Get-CimInstance Win32_Process``；
  账本文件只读打开，绝不写、不创建、不截断；**绝不写 ``ai-gateway/data/``**；
  **本工具自己唯一的写动作是 ``--out`` 指定的那份快照 JSON**（先临时文件再替换）；
  新证据面同样是只读：broker 只读管理动词 ``host-evidence{faults}`` 与
  ``maintenance-status``（只读回包，不改任何状态），``share_backend_supervision``
  只读打开运行意图 / 监督状态两个文件（不写、不创建）；
* **两个验收工具**（``metering_acceptance.py`` / ``post_restart_acceptance.py``）
  的委托只读观察一律走只读开关，**不删任何文件、不写任何文件**（都不是本工具
  自己的写动作，也都不写 ``ai-gateway/data/``、不杀活进程、不重启、不发模型请求）：
  1. ``console_share_production.production_backend_observe(forget_stale=False)``
     （console_share_production.py:1495-1546）→
     ``_ProductionBackendOps.verify_running()``（:1328-1346）的
     ``forget_stale=False`` 关掉那条 ``_forget()``（:1408-1412）
     ``os.remove(paths["pid"])``——此前它会删除**陈旧**的
     ``<backend dir>\\backend.pid``（该路径当前存在、5 字节），现在陈旧文件
     照实报出并原样留在盘上；
  2. ``console_share_production.production_backend_pid_state()``（:1549-1557）
     读同一份 pid 文件与身份证据，全分支无写、无删（陈旧即 ``pid_stale``）；
  3. 同一观察的 ``_backend_auth_state()``（:1560-1583）会读 ``control-token``
     并以 ``X-Admin-Control`` 头发只读 ``GET /admin/auth/status``（只读 GET、
     非模型请求、不打印令牌内容）；
* **仍带条件写的路径（本工具自己的 ``--out`` 档，未消掉）**：``collect_snapshot``
  （def :1037）里的 ``backend_observe_facts()``（委托点 :1062，无参）仍用默认 ``forget_stale=True``
  ⇒ 拍快照时那份**陈旧** ``backend.pid`` 仍可能被 ``os.remove``。它在 ``--out``
  档上、不在两个验收工具的采集路径上；要把它也改成零写会改 ``--out`` 档行为，
  需单独决策（执行轮 10 的 L1 授权只收紧两个验收工具）；
* **绝不重启任何服务、绝不生成密钥、绝不发模型请求、绝不改 ``web/``**；
* **绝不打印密钥内容**：只打印路径、原因码与 ``strip()`` 后的长度（``key_chars``）；
* 判定逻辑（``compare_snapshots`` 及其逐项函数）是**纯函数**：字典进、字典出，
  与 IO 分离，夹具测试直接喂字典验同一判定。

窗口专用扩展（执行轮 9；**只增不删**，round7 的 10 项判定逐字保留）：

* 快照新增 ``window_signals``：八类信号逐项标 ``ok / absent / undecidable``——
  ① 8096 读面键集（4 键旧 / 7 键新）；② 8100 的镜像名与启动时间；③ 8110 的
  PID 与其**父 PID**（并标出父 PID 是否等于 8096 的 PID）；④ ``umshare-watchdog``
  计划任务的存在与状态（**非提升身份读不到即 ``undecidable``，绝不记成「未注册」**）；
  ⑤ 分享入口的 404/401 两种门；⑥ 8110 账本行数与最后一行 attribution；⑦ Funnel 状态；
  ⑧ 分享入口故障证据（broker ``host-evidence{faults}`` 的只读回包：末条故障与按
  phase 计数；证据不可得即 ``undecidable``，**绝不折成「无故障」**，也不填 0）。
  ``absent`` 只用于「读成功且信号确定不存在」，读不到一律 ``undecidable``。
* 快照另有**两个顶层证据节**（契约 §8.3 的另外两个新信号；``WINDOW_SIGNAL_IDS``
  按冻结保持 8 项，这两个只作顶层节与摘要打印）：
  ``maintenance_state``——broker ``maintenance-status`` 的只读视图（租约 / 受信实例 /
  最近维护结果；视图不可得即 ``undecidable``，**绝不伪造**记录状态）；
  ``supervision_state``——``share_backend_supervision`` 的运行意图与监督状态文件
  （文件确定缺失记 ``absent``、存在但读不出记 ``undecidable``，**绝不折成空预算**）。
* ``--compare`` 新增 6 项判定：读面键集迁移、8100 镜像/启动时间、8110 父 PID 关系、
  watchdog 任务、账本末行归因、分享入口故障证据。**纳入规则**：任一侧快照含
  ``window_signals`` 时纳入这 6 项；两侧都是 round7 schema（无 ``window_signals``）时
  不纳入（旧快照没有 watchdog 来源，强行判定会把「本快照没查过」误报成「未注册」），
  此时输出里会打印一行说明。混用时缺失的一侧按 ``undecidable`` 记（读不到 ≠ 通过）。

退出码：

* ``--out``：0 = 快照已写出（**即使现场本身是「未接线」也照写**）；2 = 采集/写出失败。
* ``--compare``：0 = 全部 PASS；2 = 任一项 FAIL 或 UNDECIDABLE（读不到 ≠ 通过）。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# 与 ai-gateway 产品代码同仓：把 ai-gateway 目录加进 sys.path，才能直接读
# 运行中代码的只读事实（share_attribution_status / production_backend_observe）。
AI_GATEWAY_DIR = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if AI_GATEWAY_DIR not in sys.path:
    sys.path.insert(0, AI_GATEWAY_DIR)

BEIJING = timezone(timedelta(hours=8))
# v2 = 在 v1 之上**只增** window_signals / watchdog_task_query；v1 判定语义未改。
SCHEMA_VERSION = 2

# 分享链上的五个监听端口（动作门 §1.2 实测表）。
CHAIN_PORTS = (8000, 8096, 8100, 8110, 8320)
CONSOLE_PORT = 8096          # 控制台网页宿主（usagesrv.py）
GATEWAY_PORT = 8100          # 分享入口（share_entry.py）
BACKEND_PORT = 8110          # 分享专用后端（chat2api.py）

PROBE_TIMEOUT_S = 10.0
RUNNER_TIMEOUT_S = 30.0

# 8096 归因读面的两种形状（动作门 §1.3）：旧 4 键 = 08fdc96f 之前的代码；
# 新 7 键 = 三态子字典。键集本身就是「新代码是否已载入」的可用指纹。
LEGACY_ATTRIBUTION_KEYS = ("key_chars", "key_file", "reason", "wired")
SEVEN_ATTRIBUTION_KEYS = (
    "file_ready", "file_reason", "instance_loaded", "key_chars", "key_file",
    "request_attribution_detail", "request_attribution_observed")
KEY_MIN_CHARS = 32

# 分享入口白名单探测（动作门 §③.1b 行 7）：未带凭据时，白名单**外**的路径
# 404 ``route_not_shared``，白名单**内**的路径 401 ``invalid_api_key``。
# 期望值只写在这里，``compare_snapshots`` 与采集都读同一份。
SHARE_ENTRY_EXPECTED = (
    ("root", "/", 404, "route_not_shared"),
    ("health", "/health", 404, "route_not_shared"),
    ("whitelist_models", "/v1/models", 401, "invalid_api_key"),
)

# 分享账本尾部只留这些字段；绝不打印整行（行里可能带别的内容）。
LEDGER_ROW_FIELDS = ("time", "model", "key_id", "attribution")
ATTRIBUTION_VERIFIED = "verified"

# 分享链看护计划任务（um-share-deploy/deploy-elevated.ps1 注册的 SYSTEM 任务）。
# 非提升身份读不到它——动作门 §1.6：**读不到 ≠ 未注册**，只记 undecidable。
WATCHDOG_TASK_NAME = "umshare-watchdog"
# MSFT_ScheduledTask.State：0 Unknown / 1 Disabled / 2 Queued / 3 Ready / 4 Running。
WATCHDOG_TASK_STATES = {0: "Unknown", 1: "Disabled", 2: "Queued",
                        3: "Ready", 4: "Running"}
# 8100 镜像名期望集（与 console_share_production.SHARE_GATEWAY_IMAGE_NAMES :944 同口径）。
GATEWAY_IMAGE_NAMES = ("um-share-python.exe",)

# 窗口信号名（快照 window_signals 的键；--compare 的新增判定按名取）。
# 契约 §8.3 冻结：加入 share_entry_faults 后恰为 8 项（7 → 8）。
# maintenance_state / supervision_state 是快照的顶层证据节（契约 §8.3 的三个新信号
# 之二），**不进**这份冻结表——冻结表保持 8 项，两者随摘要打印与顶层键可读。
WINDOW_SIGNAL_IDS = ("read_surface_keys", "gateway_8100_image_start",
                     "backend_8110_pid_parent", "watchdog_task",
                     "share_entry_gates", "share_ledger_tail", "funnel",
                     "share_entry_faults")

# 快照顶层证据节的键名（契约 §8.3 的另外两个新信号；与 window_signals 并列可读）。
MAINTENANCE_STATE_KEY = "maintenance_state"
SUPERVISION_STATE_KEY = "supervision_state"

# 权威读数（提升身份只读探针 authoritative_readings.ps1）新增的三类信号：非提升身份
# 读不到，本工具自身**不采集**它们；只从 ``--authoritative`` 的 JSON 合并进来。
# （``WINDOW_SIGNAL_IDS`` 不含这三类，故不提供 ``--authoritative`` 时行为逐字不变。）
AUTHORITATIVE_SIGNAL_IDS = ("wfp_limits", "raw_txt", "backend_pid_file")
AUTHORITATIVE_READINGS_TOOL = "authoritative_readings"
AUTHORITATIVE_READINGS_SCHEMA_VERSION = 1
# 三态只认这三种；其它值一律按形状不符**拒绝合并**（不折成已知态）。
SIGNAL_STATUSES = ("ok", "absent", "undecidable")
# **需要原始信封重算**的信号（当前只有 ``watchdog_task``）：合并时 ``query`` 不是
# 对象（缺失 / 缺字段）或解析不出（``query_ok`` 非 True）→ 一律 ``undecidable``
# 并给原因码（``authoritative_envelope_missing`` / ``authoritative_envelope_unparsable``），
# **绝不**只凭自报字段给 PASS。与 ``window_preflight.judge_watchdog`` 同一口径。
AUTHORITATIVE_ENVELOPE_REQUIRED = ("watchdog_task",)
# 每类信号必须带的键（探针与合并的同一口径）：缺键 = 形状不符 → 拒绝，
# 绝不半份接受。``watchdog_task`` 的 ``query``（原始信封，用于重算）**不在**这份
# 必填键里——它缺失时按 ``undecidable`` 记（见上面那条），不是拒绝整条；
# 额外键允许。
AUTHORITATIVE_SIGNAL_REQUIRED_KEYS = {
    "watchdog_task": ("signal", "status", "task_name", "state", "state_text",
                      "elevated", "identity", "reason", "detail"),
    "wfp_limits": ("signal", "status", "app_exe", "backend_port",
                   "backend_port_reason", "wfp_ok", "wfp_reason",
                   "firewall_ok", "firewall_reason", "reason", "detail"),
    "raw_txt": ("signal", "status", "path", "exists", "size", "mtime",
                "mtime_utc_iso", "reason", "detail"),
    "backend_pid_file": ("signal", "status", "path", "path_source", "content",
                         "bytes_hex", "size", "pid", "mtime", "reason",
                         "detail"),
}
# 八类窗口信号的**公共**必填键（与本工具自己的构建函数同口径：每类都带
# ``signal`` / ``status`` / ``reason`` / ``detail``，另有各自的专属键）。权威读数
# 专属的三类另有更细的必填键（上面那本）。合并时按名字取，取不到就用这一份：
# 允许集内的名字**绝不因为查不到必填键而崩**——形状不符照契约进
# ``rejected_signals``，绝不半份接受。
COMMON_SIGNAL_REQUIRED_KEYS = ("signal", "status", "reason", "detail")
# 非 PASS 项的处置文案（维护者 2026-09-28 对齐）：
# UNDECIDABLE = 先补取证据，**不得**报通过；FAIL 才是按 §⑤ 回退。
GUIDANCE_UNDECIDABLE = "先补取证据，不得报通过"
GUIDANCE_FAIL = "按 §⑤ 回退"

# ``collect_snapshot`` 的 ``not_covered`` 里被权威读数**取代**的两条（合并时按这两条
# 原文前缀替换；常量只写一次，采集与合并读同一份）。
NOT_COVERED_LIMITS = "8100 limits（WFP + 防火墙）需提权核对，本快照不含"
NOT_COVERED_WATCHDOG = ("umshare-watchdog 计划任务：非提升身份读不到 → "
                       "window_signals.watchdog_task "
                       "记 undecidable（读不到 ≠ 未注册）；提升身份下的读数才是权威")

_MISSING = object()


# --------------------------------------------------------------------- IO 层
def _run_command(args, timeout=RUNNER_TIMEOUT_S):
    """只读外部命令；失败（含非零退出码）返回 None，绝不猜输出。"""
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def http_get_json(port, path, timeout=PROBE_TIMEOUT_S):
    """不带凭据的只读 GET；返回 ``{"status", "json", "error"}``。

    只连 ``127.0.0.1``；4xx/5xx 也是证据（``urllib`` 抛 HTTPError，body 照读），
    读不到/非 JSON 一律记 ``error``，**绝不把读不到折成成功**。
    """
    url = "http://127.0.0.1:%d%s" % (int(port), path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 — 读 body 失败也是证据，不是崩溃
            raw = b""
        status = exc.code
    except Exception as exc:  # noqa: BLE001 — 连接失败：如实记错误，不重试
        return {"status": None, "json": None, "error": type(exc).__name__}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        payload = None
    return {"status": status, "json": payload, "error": None}


def listening_pids(port, run=None):
    """只读取 ``port`` 上的 LISTENING PID：``{"pids", "reason"}``。

    ``pids=None`` 表示**查询失败**（读不到 ≠ 没在听）；空列表表示查询成功且无人监听。
    """
    text = (run or _run_command)(["netstat", "-ano", "-p", "TCP"])
    if text is None:
        return {"pids": None, "reason": "query_failed"}
    pids, reason = parse_listener_pids(text, port)
    return {"pids": pids, "reason": reason}


def query_processes(pids):
    """只读 CIM 查询：``{pid: 行}``；查询失败返回 ``None``（不折成空表）。"""
    wanted = sorted({int(pid) for pid in pids})
    if not wanted:
        return {}
    filter_text = " or ".join("ProcessId=%d" % pid for pid in wanted)
    script = (
        "$rows = Get-CimInstance Win32_Process -Filter '%s' | "
        "Select-Object ProcessId,Name,ParentProcessId,"
        "@{n='StartTicks';e={[string]$_.CreationDate.ToUniversalTime().Ticks}}; "
        "ConvertTo-Json -InputObject @($rows) -Compress" % filter_text)
    text = _run_command(["pwsh", "-NoProfile", "-Command", script])
    if text is None:
        return None
    return parse_cim_processes(text)


def query_watchdog_task(run=None):
    """只读查询 ``umshare-watchdog`` 计划任务：一次性 PowerShell 返回 JSON 信封。

    同一命令里带出**当前身份是否提升**（``elevated``）与身份名：这是区分
    「权威的未注册」与「读不到」的唯一依据——非提升身份查不到 SYSTEM 任务
    （动作门 §1.6：``schtasks /query`` 拒绝访问、``Get-ScheduledTask`` 也看不见），
    所以非提升下的「找不到」**不是**「未注册」。命令自身不抛错（try/catch 内联），
    返回 ``None`` 仅当 PowerShell 跑不起来。
    """
    script = (
        "$elevated = [Security.Principal.WindowsPrincipal]::new("
        "[Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole("
        "[Security.Principal.WindowsBuiltInRole]::Administrator); "
        "$found = $false; $state = $null; $stateText = $null; $err = $null; "
        "try { $t = Get-ScheduledTask -TaskName '%s' -ErrorAction Stop; "
        "$found = $true; $state = [int]$t.State; $stateText = [string]$t.State } "
        "catch { $err = $_.Exception.GetType().Name + '|' + "
        "$_.FullyQualifiedErrorId }; "
        "ConvertTo-Json -InputObject @{elevated=$elevated; name="
        "[Security.Principal.WindowsIdentity]::GetCurrent().Name; found=$found; "
        "state=$state; stateText=$stateText; error=$err} -Compress"
        % WATCHDOG_TASK_NAME)
    text = (run or _run_command)(["pwsh", "-NoProfile", "-Command",
                                  script])
    return parse_watchdog_query(text)


def read_json_file(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write_snapshot(path, snapshot):
    """先写临时文件再受控替换：中断不会留下半份快照。"""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


# ----------------------------------------------------------------- 纯解析层
def parse_listener_pids(text, port):
    """纯解析 ``netstat -ano -p TCP``：``(pids, reason)``。

    与 ``console_share_production._listening_pids``（ai-gateway/console_share_production.py
    :954-981）同一口径：``(sorted pids, None)`` 解析成功（空表 = 无人监听），
    ``(None, "query_failed")`` 有行解析不出 PID（**证据不可用，绝不当作空表**）。
    """
    pids = set()
    suffix = ":%d" % int(port)
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] != "TCP" or parts[3] != "LISTENING":
            continue
        if not parts[1].endswith(suffix):
            continue
        try:
            pids.add(int(parts[4]))
        except ValueError:
            return (None, "query_failed")
    return (sorted(pids), None)


_DOTNET_EPOCH_SECONDS = 62135596800.0


def parse_cim_processes(text):
    """纯解析 CIM JSON（``ConvertTo-Json``）：``{pid: 行}``。

    ``None`` = 输出读不出来/结构不可用；``{}`` = 查询成功且没有任何行。
    启动时间用 .NET ticks（1970 起）换算成 epoch 秒；缺失/不可解析记
    ``started_at_reason="start_time_unavailable"``，**绝不猜一个时间**。
    """
    if text is None:
        return None
    text = text.strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    rows = payload if isinstance(payload, list) else [payload]
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            return None
        pid = row.get("ProcessId")
        if not isinstance(pid, int) or isinstance(pid, bool):
            return None
        started_at, started_reason = None, "start_time_unavailable"
        ticks = row.get("StartTicks")
        if ticks is not None:
            try:
                started_at = (int(str(ticks)) / 10000000.0
                              - _DOTNET_EPOCH_SECONDS)
                started_reason = "ok"
            except (TypeError, ValueError):
                started_at, started_reason = None, "start_time_unavailable"
        name = row.get("Name")
        parent = row.get("ParentProcessId")
        out[pid] = {
            "name": name if isinstance(name, str) and name else None,
            "parent_pid": parent if isinstance(parent, int)
            and not isinstance(parent, bool) else None,
            "started_at": started_at,
            "started_at_reason": started_reason,
        }
    return out


def parse_watchdog_query(text):
    """纯解析 ``query_watchdog_task`` 的 JSON 信封。

    返回 ``{"query_ok", "elevated", "identity", "found", "state",
    "state_text", "error", "error_kind"}``。``query_ok=False`` 表示**查询本身
    没跑成**（命令失败/输出不可解析），此时 ``found`` 保持 ``None``——
    「读不到」绝不被折成「未注册」。``error_kind`` 只把
    ``FullyQualifiedErrorId`` 里的 ``NotFound`` 识别为权威的「找不到」；
    其余错误（含拒绝访问）一律 ``query_error``。
    """
    out = {"query_ok": False, "elevated": None, "identity": None,
           "found": None, "state": None, "state_text": None,
           "error": None, "error_kind": None}
    if not isinstance(text, str):
        return out
    text = text.strip()
    if not text:
        return out
    try:
        payload = json.loads(text)
    except ValueError:
        return out
    if not isinstance(payload, dict):
        return out
    elevated = payload.get("elevated")
    if not isinstance(elevated, bool):
        return out
    out["elevated"] = elevated
    identity = payload.get("name")
    out["identity"] = identity if isinstance(identity, str) and identity else None
    found = payload.get("found")
    if not isinstance(found, bool):
        return out
    out["found"] = found
    state = payload.get("state")
    if isinstance(state, int) and not isinstance(state, bool):
        out["state"] = state
    state_text = payload.get("stateText")
    if isinstance(state_text, str) and state_text:
        out["state_text"] = state_text
    error = payload.get("error")
    if isinstance(error, str) and error:
        out["error"] = error
        out["error_kind"] = "not_found" if "NotFound" in error else "query_error"
    out["query_ok"] = True
    return out


def share_entry_probe_result(response):
    """纯函数：把一次入口探测的响应归成 ``{status, code, error}``。"""
    out = {"status": None, "code": None, "error": None}
    if not isinstance(response, dict):
        out["error"] = "probe_failed"
        return out
    out["status"] = response.get("status")
    payload = response.get("json")
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            out["code"] = error["code"]
    if out["status"] is None:
        out["error"] = response.get("error") or "probe_failed"
    return out


def attribution_shape(keys):
    """纯函数：归因读面键集 → ``"seven_key"`` / ``"four_key"`` / ``"unknown"``。"""
    if not isinstance(keys, list) or not keys:
        return "unknown"
    normalized = sorted(str(key) for key in keys)
    if normalized == sorted(SEVEN_ATTRIBUTION_KEYS):
        return "seven_key"
    if normalized == sorted(LEGACY_ATTRIBUTION_KEYS):
        return "four_key"
    return "unknown"


def attribution_read_surface(response):
    """纯函数：8096 状态响应 → 归因读面事实（键集、形状、funnel、三态子字段）。"""
    out = {"http_status": None, "error": None, "keys": None, "shape": "unknown",
           "funnel_on": None, "file_ready": None,
           "instance_loaded_value": None}
    if not isinstance(response, dict):
        out["error"] = "status_probe_failed"
        return out
    out["http_status"] = response.get("status")
    payload = response.get("json")
    if not isinstance(payload, dict):
        out["error"] = "status_payload_unavailable"
        return out
    funnel = payload.get("funnel_on")
    if isinstance(funnel, bool):
        out["funnel_on"] = funnel
    attribution = payload.get("attribution")
    if isinstance(attribution, dict):
        keys = sorted(str(key) for key in attribution.keys())
        out["keys"] = keys
        out["shape"] = attribution_shape(keys)
        if out["shape"] == "seven_key":
            if isinstance(attribution.get("file_ready"), bool):
                out["file_ready"] = attribution["file_ready"]
            loaded = attribution.get("instance_loaded")
            if isinstance(loaded, dict) and isinstance(loaded.get("value"),
                                                       str):
                out["instance_loaded_value"] = loaded["value"]
    return out


def chain_parent_relation(listeners, processes):
    """纯函数：8096↔8110 的父子关系（动作门 §③.1b 行 3 的证据面）。"""
    out = {"parent_port": CONSOLE_PORT, "child_port": BACKEND_PORT,
           "parent_pid": None, "child_pid": None, "child_parent_pid": None,
           "relation": "unknown", "reason": None}
    if processes is None:
        out["reason"] = "process_query_failed"
        return out
    parent = _pids_of(listeners, CONSOLE_PORT)
    child = _pids_of(listeners, BACKEND_PORT)
    if len(parent) != 1 or len(child) != 1:
        out["reason"] = "ambiguous_or_missing_listener"
        return out
    out["parent_pid"], out["child_pid"] = parent[0], child[0]
    row = processes.get(child[0])
    if row is None:
        out["reason"] = "child_process_row_missing"
        return out
    out["child_parent_pid"] = row.get("parent_pid")
    if row.get("parent_pid") is None:
        out["reason"] = "parent_pid_unavailable"
        return out
    if row["parent_pid"] == out["parent_pid"]:
        out["relation"] = "child_parent_is_parent_pid"
        out["reason"] = "ok"
    else:
        out["relation"] = "mismatch"
        out["reason"] = "child_parent_pid_differs"
    return out


def gateway_process_facts(listeners, processes):
    """纯函数：8100 的镜像名与启动时间（动作门 §③.1b 行 4 的证据面）。"""
    out = {"port": GATEWAY_PORT, "pid": None, "image": None,
           "image_reason": None, "started_at": None,
           "started_at_reason": None, "reason": None}
    if processes is None:
        out["reason"] = "process_query_failed"
        return out
    pids = _pids_of(listeners, GATEWAY_PORT)
    if not pids:
        out["reason"] = "not_listening"
        return out
    if len(pids) != 1:
        out["reason"] = "ambiguous_listener"
        return out
    out["pid"] = pids[0]
    row = processes.get(pids[0])
    if row is None:
        out["reason"] = "process_row_missing"
        return out
    out["image"] = row.get("name")
    out["image_reason"] = "ok" if out["image"] else "image_name_unavailable"
    out["started_at"] = row.get("started_at")
    out["started_at_reason"] = row.get("started_at_reason")
    out["reason"] = "ok"
    return out


def read_surface_keys_signal(read_surface):
    """窗口信号①：8096 读面键集（``ok`` 可读 / ``absent`` 无归因对象 / 读不到不可判）。"""
    out = {"signal": "read_surface_keys", "status": "undecidable",
           "keys": None, "shape": None, "reason": None, "detail": ""}
    if not isinstance(read_surface, dict):
        out["reason"] = "read_surface_missing"
        out["detail"] = "读面事实缺失"
        return out
    keys = read_surface.get("keys")
    if isinstance(keys, list) and keys:
        out["status"] = "ok"
        out["keys"] = list(keys)
        out["shape"] = read_surface.get("shape") or attribution_shape(keys)
        out["reason"] = "ok"
        out["detail"] = "键集 %s（%s）" % (sorted(keys), out["shape"])
        return out
    if read_surface.get("http_status") is not None \
            and read_surface.get("error") is None:
        out["status"] = "absent"
        out["reason"] = "attribution_surface_absent"
        out["detail"] = "读面可读但没有 attribution 键集"
        return out
    out["reason"] = read_surface.get("error") or "status_probe_failed"
    out["detail"] = "读面读不到（http=%r）：读不到 ≠ 没有键集" \
        % (read_surface.get("http_status"),)
    return out


def gateway_image_start_signal(gateway_facts):
    """窗口信号②：8100 的镜像名与启动时间。"""
    out = {"signal": "gateway_8100_image_start", "status": "undecidable",
           "pid": None, "image": None, "image_expected": None,
           "started_at": None, "reason": None, "detail": ""}
    if not isinstance(gateway_facts, dict):
        out["reason"] = "gateway_facts_missing"
        out["detail"] = "8100 事实缺失"
        return out
    reason = gateway_facts.get("reason")
    if reason == "not_listening":
        out["status"] = "absent"
        out["reason"] = "not_listening"
        out["detail"] = "8100 无人监听（掉线）"
        return out
    if reason != "ok":
        out["reason"] = reason or "process_facts_unavailable"
        out["detail"] = "8100 进程事实读不到（%s）" % (reason,)
        return out
    image = gateway_facts.get("image")
    started_at = gateway_facts.get("started_at")
    out["pid"] = gateway_facts.get("pid")
    out["image"] = image
    out["image_expected"] = image in GATEWAY_IMAGE_NAMES
    out["started_at"] = started_at
    if not image or started_at is None:
        out["reason"] = "image_or_start_time_unavailable"
        out["detail"] = "镜像名或启动时间读不到（image=%r started_at=%r）" \
            % (image, started_at)
        return out
    out["status"] = "ok"
    out["reason"] = "ok"
    out["detail"] = "pid=%s image=%s started_at=%r" \
        % (out["pid"], image, started_at)
    return out


def backend_pid_parent_signal(listeners, processes):
    """窗口信号③：8110 的 PID 与其父 PID，并标出父 PID 是否等于 8096 的 PID。"""
    out = {"signal": "backend_8110_pid_parent", "status": "undecidable",
           "backend_pid": None, "parent_pid": None, "console_pid": None,
           "parent_is_console_pid": None, "reason": None, "detail": ""}
    if processes is None:
        out["reason"] = "process_query_failed"
        out["detail"] = "进程查询失败：父 PID 读不到"
        return out
    console = _pids_of(listeners, CONSOLE_PORT)
    backend = _pids_of(listeners, BACKEND_PORT)
    if not backend:
        out["status"] = "absent"
        out["reason"] = "backend_not_listening"
        out["detail"] = "8110 无人监听（掉线）"
        return out
    if len(backend) != 1:
        out["reason"] = "ambiguous_backend_listener"
        out["detail"] = "8110 监听 PID 不唯一：%s" % backend
        return out
    out["backend_pid"] = backend[0]
    row = processes.get(backend[0])
    if row is None:
        out["reason"] = "process_row_missing"
        out["detail"] = "8110 PID %s 的进程行读不到" % backend[0]
        return out
    parent = row.get("parent_pid")
    if parent is None:
        out["reason"] = "parent_pid_unavailable"
        out["detail"] = "8110 父 PID 读不到"
        return out
    out["parent_pid"] = parent
    if not console:
        out["reason"] = "console_pid_unavailable"
        out["detail"] = "8096 监听 PID 读不到：无法比较父 PID"
        return out
    if len(console) != 1:
        out["reason"] = "ambiguous_console_listener"
        out["detail"] = "8096 监听 PID 不唯一：%s" % console
        return out
    out["console_pid"] = console[0]
    out["parent_is_console_pid"] = (parent == console[0])
    out["status"] = "ok"
    out["reason"] = "ok"
    out["detail"] = "8110 PID=%s 父 PID=%s；父 PID 等于 8096 PID：%s" \
        % (backend[0], parent, out["parent_is_console_pid"])
    return out


def watchdog_task_signal(query):
    """窗口信号④：``umshare-watchdog`` 任务的存在与状态。

    **读不到即 ``undecidable``，绝不记成「未注册」**：只有提升身份下
    ``Get-ScheduledTask`` 的权威「找不到」（``error_kind=not_found``）才记
    ``absent``；非提升身份查不到记 ``undecidable``（``reason`` 写明
    ``not_visible_to_current_identity``）。
    """
    out = {"signal": "watchdog_task", "status": "undecidable",
           "task_name": WATCHDOG_TASK_NAME, "state": None,
           "state_text": None, "elevated": None, "identity": None,
           "reason": None, "detail": ""}
    if not isinstance(query, dict) or query.get("query_ok") is not True:
        out["reason"] = "query_failed"
        out["detail"] = "计划任务查询没跑成（命令失败或输出不可解析）——读不到 ≠ 未注册"
        return out
    out["elevated"] = query.get("elevated")
    out["identity"] = query.get("identity")
    if query.get("found") is True:
        state = query.get("state")
        if not isinstance(state, int) or isinstance(state, bool):
            out["reason"] = "state_unavailable"
            out["detail"] = "任务对象可见但 State 读不到——读不到 ≠ 未注册"
            return out
        out["status"] = "ok"
        out["state"] = state
        out["state_text"] = (query.get("state_text")
                             or WATCHDOG_TASK_STATES.get(state))
        out["reason"] = "ok"
        out["detail"] = "任务已注册：State=%s（%s）" \
            % (out["state_text"], state)
        return out
    if query.get("elevated") is True and query.get("error_kind") == "not_found":
        out["status"] = "absent"
        out["reason"] = "not_registered_elevated"
        out["detail"] = "提升身份下的权威读数：任务未注册（absent）"
        return out
    if query.get("elevated") is False:
        out["reason"] = "not_visible_to_current_identity"
        out["detail"] = ("非提升身份（%s）查不到 SYSTEM 任务：按『读不到』记 "
                         "undecidable，绝不记成『未注册』（动作门 §1.6）"
                         % (query.get("identity"),))
        return out
    out["reason"] = "query_error"
    out["detail"] = "任务查询返回错误而非权威的『未注册』：%s" \
        % (query.get("error"),)
    return out


def _envelope_required_undecidable(name, reason, detail):
    """纯函数：``AUTHORITATIVE_ENVELOPE_REQUIRED`` 信号缺可解析信封时的规范条目。

    形状与 ``watchdog_task_signal`` 的产物一致（``status=undecidable``），只是
    ``reason`` / ``detail`` 换成信封缺失的原因码；``elevated`` / ``identity`` 一律
    ``None``——**不采信自报字段**（连身份冲突核对也不拿它当证据）。下游
    （``--compare`` 与八层验收）按同一形状消费，**绝不**只凭自报字段给 PASS。
    """
    return {"signal": name, "status": "undecidable",
            "task_name": WATCHDOG_TASK_NAME, "state": None,
            "state_text": None, "elevated": None, "identity": None,
            "reason": reason, "detail": detail}


def share_entry_gates_signal(probes):
    """窗口信号⑤：分享入口的 404/401 两种门（``ok`` 命中期望 / ``absent`` 门不在）。"""
    out = {"signal": "share_entry_gates", "status": "undecidable",
           "entries": {}, "reason": None, "detail": ""}
    if not isinstance(probes, dict):
        out["reason"] = "probe_missing"
        out["detail"] = "入口探测事实缺失"
        return out
    entries = {}
    for name, path, want_status, want_code in SHARE_ENTRY_EXPECTED:
        probe = probes.get(name)
        if not isinstance(probe, dict):
            out["reason"] = "probe_missing"
            out["detail"] = "入口探测结果缺失：%s" % name
            return out
        status = probe.get("status")
        entries[name] = {"path": path, "status": status,
                         "code": probe.get("code"),
                         "error": probe.get("error"),
                         "expected": {"status": want_status, "code": want_code},
                         "matches": (status, probe.get("code"))
                         == (want_status, want_code)}
        if status is None:
            out["status"] = "undecidable"
            out["entries"] = entries
            out["reason"] = "probe_failed:%s" % name
            out["detail"] = "入口探测失败：%s error=%r（读不到 ≠ 门不在）" \
                % (name, probe.get("error"))
            return out
    out["entries"] = entries
    missing = [name for name, path, _s, _c in SHARE_ENTRY_EXPECTED
               if not entries[name]["matches"]]
    if missing:
        detail_bits = []
        for name, path, want_status, want_code in SHARE_ENTRY_EXPECTED:
            if name in missing:
                detail_bits.append("%s %s 实测 %r %r 期望 %d %s"
                                   % (name, path, entries[name]["status"],
                                      entries[name]["code"], want_status,
                                      want_code))
        out["status"] = "absent"
        out["reason"] = "gate_missing:%s" % ",".join(missing)
        out["detail"] = "两种门未按期望出现：%s" % "；".join(detail_bits)
        return out
    out["status"] = "ok"
    out["reason"] = "ok"
    out["detail"] = "；".join(
        "%s %s → %s %s" % (name, entries[name]["path"],
                           entries[name]["status"], entries[name]["code"])
        for name, _path, _status, _code in SHARE_ENTRY_EXPECTED)
    return out


def share_ledger_tail_signal(share_ledger):
    """窗口信号⑥：8110 账本行数与最后一行 attribution。"""
    out = {"signal": "share_ledger_tail", "status": "undecidable",
           "rows": None, "last_index": None, "last_attribution": None,
           "last_key_id": None, "reason": None, "detail": ""}
    if not isinstance(share_ledger, dict):
        out["reason"] = "ledger_facts_missing"
        out["detail"] = "账本事实缺失"
        return out
    if share_ledger.get("error"):
        out["reason"] = share_ledger["error"]
        out["detail"] = "账本读不到（%s）——读不到 ≠ 没有行" \
            % share_ledger["error"]
        return out
    rows = share_ledger.get("rows")
    if not isinstance(rows, int):
        out["reason"] = "rows_unavailable"
        out["detail"] = "账本行数读不到"
        return out
    out["rows"] = rows
    if rows == 0:
        out["status"] = "absent"
        out["reason"] = "no_rows"
        out["detail"] = "账本可读但没有行（末行不存在）"
        return out
    last = share_ledger.get("last")
    if not isinstance(last, dict):
        out["reason"] = "last_row_unavailable"
        out["detail"] = "账本有 %d 行但末行读不到" % rows
        return out
    out["status"] = "ok"
    out["last_index"] = last.get("index")
    out["last_attribution"] = last.get("attribution")
    out["last_key_id"] = last.get("key_id")
    out["reason"] = "ok"
    out["detail"] = "rows=%d 末行 #%s attribution=%r key_id=%r" \
        % (rows, last.get("index"), last.get("attribution"),
           last.get("key_id"))
    return out


def funnel_signal(read_surface):
    """窗口信号⑦：Funnel 状态。"""
    out = {"signal": "funnel", "status": "undecidable", "funnel_on": None,
           "reason": None, "detail": ""}
    if not isinstance(read_surface, dict):
        out["reason"] = "read_surface_missing"
        out["detail"] = "读面事实缺失"
        return out
    if read_surface.get("http_status") is None \
            or read_surface.get("error"):
        out["reason"] = read_surface.get("error") or "status_probe_failed"
        out["detail"] = "读面读不到（http=%r）——读不到 ≠ funnel 关闭" \
            % (read_surface.get("http_status"),)
        return out
    funnel_on = read_surface.get("funnel_on")
    if isinstance(funnel_on, bool):
        out["status"] = "ok"
        out["funnel_on"] = funnel_on
        out["reason"] = "ok"
        out["detail"] = "funnel_on=%r" % funnel_on
        return out
    out["status"] = "absent"
    out["reason"] = "funnel_field_absent"
    out["detail"] = "读面可读但没有 funnel_on 字段"
    return out


def share_entry_faults_signal(facts):
    """窗口信号⑧：分享入口故障证据（broker ``host-evidence{faults}`` 只读回包）。

    证据面 = ``console_share_production.production_broker_evidence("faults")``
    的冻结回包 ``{check, verdict, detail, checked_at}``（契约 §2.3/§8.6）。
    三态纪律：

    * ``ok``：回包可读且 ``verdict`` 是故障尾读结果（``last_fault`` 非空）；
    * ``absent``：回包可读且确定无故障记录（``last_fault=null``，counts 全 0）；
    * ``undecidable``：回包不可得（未接线 / 不可达 / 旧 broker 拒绝）或
      ``verdict=null``（故障文件不存在 / 读不到）——**读不到 ≠ 无故障**；
      ``counts_by_phase`` 保持 ``None``，**绝不填 0、绝不伪造**。
    """
    out = {"signal": "share_entry_faults", "status": "undecidable",
           "last_fault": None, "counts_by_phase": None, "file": None,
           "verdict": None, "checked_at": None, "check": "faults",
           "reason": None, "detail": ""}
    reply = facts.get("reply") if isinstance(facts, dict) else None
    if not isinstance(reply, dict):
        out["reason"] = ((facts.get("error") if isinstance(facts, dict)
                          else None) or "broker_evidence_unavailable")
        out["detail"] = ("broker host-evidence{faults} 不可得（未接线 / 不可达 / "
                         "旧 broker 拒绝）：读不到 ≠ 无故障，绝不填 0（§8.3）")
        return out
    check = reply.get("check")
    if check is not None and check != "faults":
        out["reason"] = "evidence_check_mismatch:%r" % (check,)
        out["detail"] = "回包的 check 不是 faults：形状不符，拒绝采信"
        return out
    out["checked_at"] = reply.get("checked_at")
    out["verdict"] = reply.get("verdict")
    verdict = out["verdict"]
    if verdict is None:
        detail = reply.get("detail")
        out["reason"] = (detail if isinstance(detail, str) and detail
                         else "fault_log_absent")
        out["detail"] = ("broker 如实回报证据不可裁定（verdict=null，detail=%r）："
                         "故障文件不存在 / 读不到 → 读不到 ≠ 无故障"
                         % (detail,))
        return out
    if not isinstance(verdict, dict):
        out["reason"] = "evidence_verdict_shape_invalid"
        out["detail"] = "verdict 不是故障尾读对象：形状不符，拒绝采信"
        return out
    counts = verdict.get("counts_by_phase")
    if not isinstance(counts, dict):
        out["reason"] = "counts_by_phase_unavailable"
        out["detail"] = "故障尾读结果缺 counts_by_phase：读不到 ≠ 无故障，绝不填 0"
        return out
    last_fault = verdict.get("last_fault")
    if last_fault is not None and not isinstance(last_fault, dict):
        out["reason"] = "last_fault_shape_invalid"
        out["detail"] = "last_fault 不是记录对象：形状不符，拒绝半份采信"
        return out
    file_facts = verdict.get("file")
    out["file"] = file_facts if isinstance(file_facts, dict) else None
    out["counts_by_phase"] = dict(counts)
    if last_fault is None:
        out["status"] = "absent"
        out["reason"] = "no_faults_recorded"
        out["detail"] = ("故障文件可读且确定无故障记录（按 broker 读面口径："
                         "counts 全 0、last_fault=null）；counts=%s" % (counts,))
        return out
    out["status"] = "ok"
    out["last_fault"] = dict(last_fault)
    out["reason"] = "ok"
    out["detail"] = ("末条故障 phase=%r code=%r at=%r；counts=%s"
                     % (last_fault.get("phase"), last_fault.get("code"),
                        last_fault.get("at"), counts))
    return out


def maintenance_state_signal(facts):
    """顶层证据节：维护状态（broker ``maintenance-status`` 只读视图）。

    证据面 = ``console_share_production.production_maintenance_status()`` 的冻结
    回包 ``{checked_at, lease, lease_expired, instance, last_outcome}``（契约 §2.3）。
    视图不可得（未接线 / 不可达 / 旧 broker）→ ``undecidable``，
    ``maintenance_state`` 与租约字段保持 ``None``——**不伪造**记录状态：broker 的
    idle / leased / restarting 记录状态不在这份只读视图里，只有租约在册与否可判：

    * 租约在册且未过期 → ``maintenance_state="leased"``；
    * 租约在册但 ``lease_expired`` → ``maintenance_state="lease_expired"``；
    * 无租约 → ``maintenance_state=None``（``state_basis`` 写明视图不暴露记录
      状态，不猜成 idle——``restarting`` 记录可以没有租约）。
    ``last_outcome`` 原样透出（§8.5：最近一次维护结果含失败，恢复不擦除失败）。
    """
    out = {"signal": "maintenance_state", "status": "undecidable",
           "maintenance_state": None, "state_basis": None,
           "checked_at": None, "lease": None, "lease_expired": None,
           "instance": None, "last_outcome": None,
           "reason": None, "detail": ""}
    view = facts.get("view") if isinstance(facts, dict) else None
    if not isinstance(view, dict):
        out["reason"] = ((facts.get("error") if isinstance(facts, dict)
                          else None) or "maintenance_view_unavailable")
        out["detail"] = ("broker maintenance-status 不可得（未接线 / 不可达 / "
                         "旧 broker）：读不到 ≠ 无维护，不伪造记录状态（§8.5）")
        return out
    out["checked_at"] = view.get("checked_at")
    lease = view.get("lease")
    out["lease"] = dict(lease) if isinstance(lease, dict) else None
    expired = view.get("lease_expired")
    out["lease_expired"] = expired if isinstance(expired, bool) else None
    instance = view.get("instance")
    out["instance"] = dict(instance) if isinstance(instance, dict) else None
    outcome = view.get("last_outcome")
    out["last_outcome"] = dict(outcome) if isinstance(outcome, dict) else None
    out["status"] = "ok"
    out["reason"] = "ok"
    if out["lease"] is None:
        out["state_basis"] = "lease_absent_state_not_exposed"
        out["detail"] = ("维护视图可读但没有租约在册（maintenance-status 不回传记录"
                         "状态）：maintenance_state 不猜；instance=%r last_outcome=%r"
                         % (out["instance"], out["last_outcome"]))
        return out
    if out["lease_expired"] is True:
        out["maintenance_state"] = "lease_expired"
        out["state_basis"] = "lease_expired_flag"
        out["detail"] = ("维护租约在册但已过期（lease_id=%r component=%r）："
                         "按 lease_expired 如实记，不猜成 idle"
                         % (out["lease"].get("lease_id"),
                            out["lease"].get("component")))
        return out
    out["maintenance_state"] = "leased"
    out["state_basis"] = "lease_in_force"
    out["detail"] = ("维护租约在册：component=%s lease_id=%s expires_at=%s；"
                     "last_outcome=%r"
                     % (out["lease"].get("component"),
                        out["lease"].get("lease_id"),
                        out["lease"].get("expires_at"), out["last_outcome"]))
    return out


def supervision_state_signal(facts):
    """顶层证据节：监督状态（``share_backend_supervision`` 两个只读文件）。

    证据面 = ``read_desired_state()``（运行意图）与 ``read_watch_state()``
    （监督持久状态）的只读结果（契约 §7/A9-5）。三态纪律：

    * ``ok``：监督状态文件可读（携带意图、失败计数与最近决策；意图缺失 /
      畸形时 ``desired_state=None`` 并如实标注）；
    * ``absent``：监督状态文件确定不存在（``reason="missing"``）——监督从未
      写入状态（确定事实，不是「读不到」）；
    * ``undecidable``：文件存在但读不出（``unreadable`` / ``malformed`` /
      ``version_mismatch``）或模块不可用——**读不到 ≠ 未运行**，
      绝不折成空预算（A9-5），计数保持 ``None``、绝不填 0。
    """
    out = {"signal": "supervision_state", "status": "undecidable",
           "desired_state": None, "desired_at_utc": None,
           "desired_source": None, "desired_boot_id": None,
           "watch_readable": None, "watch_reason": None,
           "consecutive_failures": None, "restart_attempts_utc": None,
           "idle_reason": None, "last_decision": None,
           "state_dir": None, "reason": None, "detail": ""}
    if not isinstance(facts, dict):
        out["reason"] = "supervision_facts_missing"
        out["detail"] = "监督状态事实缺失（本次采集未接线）"
        return out
    out["state_dir"] = facts.get("state_dir")
    desired = facts.get("desired")
    if isinstance(desired, dict):
        state = desired.get("state")
        out["desired_state"] = state if state in ("running", "stopped") else None
        out["desired_at_utc"] = desired.get("at_utc")
        out["desired_source"] = desired.get("source")
        out["desired_boot_id"] = desired.get("boot_id")
    if facts.get("watch_error"):
        out["reason"] = str(facts["watch_error"])
        out["detail"] = ("监督状态读取失败（%s）：读不到 ≠ 未运行"
                         % facts["watch_error"])
        return out
    watch = facts.get("watch")
    if not isinstance(watch, dict):
        out["reason"] = "watch_state_unavailable"
        out["detail"] = "监督状态不可得：读不到 ≠ 未运行"
        return out
    if watch.get("unreadable") is True:
        reason = watch.get("reason")
        out["watch_readable"] = False
        out["watch_reason"] = reason
        if reason == "missing":
            out["status"] = "absent"
            out["reason"] = "watch_state_missing"
            out["detail"] = ("监督状态文件确定不存在（reason=missing）；运行意图=%r："
                             "监督从未写入状态，不折成空预算"
                             % (out["desired_state"],))
            return out
        out["reason"] = "watch_state_unreadable:%s" % (reason,)
        out["detail"] = ("监督状态文件存在但读不出（reason=%s）：读不到 ≠ 未运行，"
                         "绝不折成空预算（A9-5）" % (reason,))
        return out
    out["watch_readable"] = True
    out["consecutive_failures"] = watch.get("consecutive_failures")
    attempts = watch.get("restart_attempts_utc")
    out["restart_attempts_utc"] = list(attempts) \
        if isinstance(attempts, list) else None
    out["idle_reason"] = watch.get("idle_reason")
    out["last_decision"] = watch.get("last_decision")
    out["status"] = "ok"
    out["reason"] = "ok"
    out["detail"] = ("运行意图=%r；consecutive_failures=%s attempts_this_hour=%s "
                     "idle_reason=%r last_decision=%r"
                     % (out["desired_state"], out["consecutive_failures"],
                        len(out["restart_attempts_utc"])
                        if isinstance(out["restart_attempts_utc"], list) else None,
                        out["idle_reason"], out["last_decision"]))
    return out


def build_window_signals(listeners, processes, read_surface, gateway_facts,
                        share_entry_probe, share_ledger, watchdog_query,
                        share_entry_faults=None):
    """把八类窗口信号压成一份字典（纯函数；采集层只负责喂事实）。"""
    signals = {
        "read_surface_keys": read_surface_keys_signal(read_surface),
        "gateway_8100_image_start": gateway_image_start_signal(gateway_facts),
        "backend_8110_pid_parent": backend_pid_parent_signal(listeners,
                                                             processes),
        "watchdog_task": watchdog_task_signal(watchdog_query),
        "share_entry_gates": share_entry_gates_signal(share_entry_probe),
        "share_ledger_tail": share_ledger_tail_signal(share_ledger),
        "funnel": funnel_signal(read_surface),
        "share_entry_faults": share_entry_faults_signal(share_entry_faults),
    }
    return signals


def operations_summary(response):
    """纯函数：``/api/console/operations`` 响应 → ``{total, live}``。"""
    out = {"http_status": None, "total": None, "live": None, "error": None}
    if not isinstance(response, dict):
        out["error"] = "operations_probe_failed"
        return out
    out["http_status"] = response.get("status")
    payload = response.get("json")
    operations = payload.get("operations") if isinstance(payload, dict) else None
    if not isinstance(operations, list):
        out["error"] = "operations_payload_unavailable"
        return out
    out["total"] = len(operations)
    out["live"] = sum(1 for op in operations if isinstance(op, dict)
                      and op.get("state") in ("queued", "running"))
    return out


def scan_share_ledger(path, tail_fields=LEDGER_ROW_FIELDS):
    """只读扫描分享账本：行数、末行、已归属行、attribution 直方图。"""
    result = {"path": path, "rows": 0, "unparsed": 0, "last": None,
              "attributed_rows": [], "attribution_counts": {}, "error": None}
    if not path:
        result["error"] = "ledger_path_unresolved"
        return result
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                index = result["rows"]
                result["rows"] += 1
                try:
                    record = json.loads(line)
                except ValueError:
                    result["unparsed"] += 1
                    continue
                if not isinstance(record, dict):
                    result["unparsed"] += 1
                    continue
                row = {"index": index}
                for field in tail_fields:
                    row[field] = record.get(field)
                result["last"] = row
                attribution = record.get("attribution")
                if not (isinstance(attribution, str) and attribution):
                    # 与 share_metering.py :194-200 同一口径：写盘端明确写了
                    # 「没有归因标记」记 absent，旧记录没有该字段记 legacy。
                    attribution = "absent" if "attribution" in record else "legacy"
                result["attribution_counts"][attribution] = \
                    result["attribution_counts"].get(attribution, 0) + 1
                key_id = record.get("key_id")
                if isinstance(key_id, str) and key_id.strip():
                    result["attributed_rows"].append(dict(row))
    except OSError as exc:
        result["error"] = "ledger_unreadable:%s" % type(exc).__name__
    return result


def scan_private_ledger(path):
    """只读扫描私人总账：行数与 ``key_id`` 非空行数（门内不变式 3 必须为 0）。"""
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


def _pids_of(listeners, port):
    if not isinstance(listeners, dict):
        return []
    entry = listeners.get(str(port))
    if not isinstance(entry, dict):
        return []
    pids = entry.get("pids")
    return pids if isinstance(pids, list) else []


# ----------------------------------------------------------------- 采集层
def gateway_attribution_facts():
    """同身份的只读直调（动作门 §4.1 的替代观察）：三态事实 1/2。"""
    out = {"source": "in_process_share_attribution_status",
           "file_ready": None, "file_reason": None, "key_chars": None,
           "key_file": None, "instance_loaded_value": None,
           "instance_loaded_basis": None, "instance_loaded_reason": None,
           "request_attribution_observed": None, "error": None}
    try:
        import console_share_production
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


def backend_observe_facts(forget_stale=False):
    """只读调 8110 的 /health 与 /admin/auth/status，并带出账本路径。

    ``forget_stale`` 是**加性**只读开关，本包装器默认 ``False``（只读）——
    它是快照/验收专用的观测入口，观测不该顺手删文件；产品侧
    ``production_backend_observe`` 的默认仍是 ``True``（既有行为逐字不变），
    需要清理的调用方显式传 ``True``。
    原样转发给 ``production_backend_observe``：``forget_stale=False`` 时这条
    只读路径**不删**陈旧 ``backend.pid``。八层验收的采集层与快照工具的
    ``collect_snapshot`` 都走只读那一档（2026-09-28 起）；默认值 ``True``
    仍是既有行为，供需要清理的调用方显式使用。
    """
    out = {"running": None, "auth_state": None, "auth_label": None,
           "ledger_file": None, "error": None}
    try:
        import console_share_production
        observed = console_share_production.production_backend_observe(
            forget_stale=forget_stale)
    except Exception as exc:  # noqa: BLE001 — 读不到是证据，不是崩溃
        out["error"] = "backend_observe_failed:%s" % type(exc).__name__
        return out
    if not isinstance(observed, dict):
        out["error"] = "backend_observe_invalid"
        return out
    out["running"] = observed.get("running")
    out["auth_state"] = observed.get("auth_state")
    out["auth_label"] = observed.get("auth_label")
    attribution = observed.get("attribution")
    if isinstance(attribution, dict):
        detail = attribution.get("request_attribution_detail")
        if isinstance(detail, dict) and isinstance(detail.get("ledger_file"),
                                                     str):
            out["ledger_file"] = detail["ledger_file"]
    if out["ledger_file"] is None:
        out["error"] = "ledger_path_unresolved"
    return out


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


def broker_faults_facts():
    """只读 broker ``host-evidence{faults}`` 证据（契约 §8.3 读者二）。

    委托 ``console_share_production.production_broker_evidence("faults")``：
    证据在 SYSTEM 权限的 broker 内执行，只读尾读故障文件，回包原样返回。
    未接线 / 不可达 / 旧 broker 拒绝一律记 ``error``（诚实不可得，
    **绝不折成「无故障」**）。本函数不写任何文件、不重启任何东西。
    """
    out = {"source": "console_share_production.production_broker_evidence",
           "reply": None, "error": None}
    try:
        import console_share_production
        reply = console_share_production.production_broker_evidence("faults")
    except Exception as exc:  # noqa: BLE001 — 读不到是证据，不是崩溃
        out["error"] = "broker_evidence_failed:%s" % type(exc).__name__
        return out
    if isinstance(reply, dict):
        out["reply"] = reply
    else:
        out["error"] = "broker_evidence_unavailable"
    return out


def maintenance_state_facts():
    """只读 broker ``maintenance-status`` 维护视图（契约 §2.3/§8.5）。

    委托 ``console_share_production.production_maintenance_status()``：只读管理
    动词，未接线 / 不可达 / 旧 broker 一律记 ``error``（诚实不可得）。
    """
    out = {"source": "console_share_production.production_maintenance_status",
           "view": None, "error": None}
    try:
        import console_share_production
        view = console_share_production.production_maintenance_status()
    except Exception as exc:  # noqa: BLE001 — 读不到是证据，不是崩溃
        out["error"] = "maintenance_read_failed:%s" % type(exc).__name__
        return out
    if isinstance(view, dict):
        out["view"] = view
    else:
        out["error"] = "maintenance_view_unavailable"
    return out


def supervision_state_facts(state_dir=None):
    """只读 ``share_backend_supervision`` 的运行意图与监督状态文件。

    ``state_dir`` 仅供隔离测试重定向（默认取模块自己的 ``DEFAULT_WATCH_DIR``）。
    两个文件都只读打开，绝不写、不创建；导入失败或读取抛错一律记 ``error``
    （诚实不可得），绝不折成空状态。
    """
    out = {"source": "share_backend_supervision", "state_dir": None,
           "desired": None, "desired_error": None,
           "watch": None, "watch_error": None}
    try:
        import share_backend_supervision
    except Exception as exc:  # noqa: BLE001 — 读不到是证据，不是崩溃
        out["watch_error"] = "import_failed:%s" % type(exc).__name__
        return out
    base = state_dir or share_backend_supervision.DEFAULT_WATCH_DIR
    out["state_dir"] = base
    desired_path = os.path.join(base, share_backend_supervision.DESIRED_FILE_NAME)
    state_path = os.path.join(base, share_backend_supervision.STATE_FILE_NAME)
    try:
        out["desired"] = share_backend_supervision.read_desired_state(
            desired_path)
    except Exception as exc:  # noqa: BLE001 — 读不到是证据，不是崩溃
        out["desired_error"] = "read_failed:%s" % type(exc).__name__
    try:
        out["watch"] = share_backend_supervision.read_watch_state(state_path)
    except Exception as exc:  # noqa: BLE001 — 读不到是证据，不是崩溃
        out["watch_error"] = "read_failed:%s" % type(exc).__name__
    return out


def collect_snapshot(get=None, cim=None, run=None, watchdog=None,
                     broker_faults=None, maintenance=None, supervision=None):
    """采集一份只读现场快照（字典）。不重启、不生成密钥、不发模型请求。

    新增三个只读证据面（契约 §8.3）：broker ``host-evidence{faults}``、
    broker ``maintenance-status`` 与 ``share_backend_supervision`` 的两个状态
    文件；都只读，都可注入（隔离测试），不可得时如实记 undecidable/absent。
    """
    get = get or http_get_json
    cim = cim or query_processes
    run = run or _run_command
    watchdog = watchdog or query_watchdog_task
    broker_faults = broker_faults or broker_faults_facts
    maintenance = maintenance or maintenance_state_facts
    supervision = supervision or supervision_state_facts

    listeners = {str(port): listening_pids(port, run=run)
                 for port in CHAIN_PORTS}
    health = get(CONSOLE_PORT, "/health")
    share_status = get(CONSOLE_PORT, "/api/console/share-management/status")
    operations = operations_summary(
        get(CONSOLE_PORT, "/api/console/operations?limit=50"))
    share_entry_probe = {
        name: share_entry_probe_result(get(GATEWAY_PORT, path))
        for name, path, _status, _code in SHARE_ENTRY_EXPECTED
    }
    pids_to_query = []
    for port in (CONSOLE_PORT, GATEWAY_PORT, BACKEND_PORT):
        pids_to_query.extend(_pids_of(listeners, port))
    processes = cim(pids_to_query)
    chain_parent = chain_parent_relation(listeners, processes)
    gateway_8100 = gateway_process_facts(listeners, processes)
    read_surface = attribution_read_surface(share_status)
    gateway_attribution = gateway_attribution_facts()
    # forget_stale=False：快照是只读观测，不得顺手删陈旧 backend.pid
    # （执行轮 10 写面审计报出的两条 (c) 档之一，2026-09-28 收紧）。
    backend_observe = backend_observe_facts(forget_stale=False)
    share_ledger = scan_share_ledger(backend_observe.get("ledger_file"))
    private_path, private_error = private_ledger_path()
    private_ledger = scan_private_ledger(private_path)
    if private_error and private_ledger.get("error") is None:
        private_ledger["error"] = private_error
    watchdog_query = watchdog(run=run)
    # 三个新证据面（契约 §8.3）：全部只读、全部可注入；读不到如实记
    # undecidable / absent（§8.3 加性兼容：旧部署不折成「无故障」）。
    faults_facts = broker_faults()
    maintenance_facts = maintenance()
    supervision_facts = supervision()
    window_signals = build_window_signals(
        listeners, processes, read_surface, gateway_8100, share_entry_probe,
        share_ledger, watchdog_query, faults_facts)

    return {
        "tool": "share_chain_snapshot",
        "schema_version": SCHEMA_VERSION,
        "captured_at": time.time(),
        "captured_at_iso": datetime.now(BEIJING).isoformat(timespec="seconds"),
        "listeners": listeners,
        "health_8096": health,
        "chain_parent": chain_parent,
        "gateway_8100": gateway_8100,
        "share_status": read_surface,
        "gateway_attribution": gateway_attribution,
        "backend_observe": backend_observe,
        "share_entry_probe": share_entry_probe,
        "operations": operations,
        "share_ledger": share_ledger,
        "private_ledger": private_ledger,
        "watchdog_task_query": watchdog_query,
        "window_signals": window_signals,
        # 顶层证据节（契约 §8.3 的三个新信号之二；不进冻结的 WINDOW_SIGNAL_IDS）。
        MAINTENANCE_STATE_KEY: maintenance_state_signal(maintenance_facts),
        SUPERVISION_STATE_KEY: supervision_state_signal(supervision_facts),
        "not_covered": [
            "8100 启动任务日志（启动行「请求归因：已启用」）需提权读取，本快照不含",
            NOT_COVERED_LIMITS,
            NOT_COVERED_WATCHDOG,
            "密钥内容与任何分享 Key 明文：本快照只含路径、原因码与 strip() 后长度",
        ],
    }


# ------------------------------------------------- 权威读数（提升身份探针）合并层
def load_authoritative_file(path):
    """只读读取提升身份探针的 JSON：``{"path", "sha256", "encoding", "readings", "error"}``。

    读不到／解不开一律记 ``error``（调用方按非零退出处理，**不写快照**——绝不
    写一份「看起来有权威读数」的快照）。编码按探针的交付方式判定：``-Out`` 写
    UTF-8（无 BOM）；为防手工重定向，依次试 UTF-8（带/不带 BOM）、UTF-16
    （PowerShell 5.1 的 ``>``/``Out-File`` 写 UTF-16LE）与系统 ANSI 代码页
    （``cmd`` 的 ``>`` 写控制台代码页，本机实测为 GBK）；**每种编码都要求
    ``decode`` 与 ``json.loads`` 同时成功**，否则换下一种；全部失败才是
    ``authoritative_unparsable``，绝不猜。实际用的编码记在 ``encoding`` 里，
    便于复核（ASCII 字段不受影响，只有人读的 detail 文案依赖它）。
    """
    out = {"path": path, "sha256": None, "encoding": None, "readings": None,
           "error": None}
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        out["error"] = "authoritative_unreadable:%s" % type(exc).__name__
        return out
    out["sha256"] = hashlib.sha256(raw).hexdigest()
    decoded = False
    for encoding in ("utf-8-sig", "utf-16", "mbcs"):
        try:
            text = raw.decode(encoding)
        except (UnicodeError, LookupError, ValueError):
            continue
        decoded = True
        try:
            readings = json.loads(text)
        except ValueError:
            continue
        out["readings"] = readings
        out["encoding"] = encoding
        return out
    out["error"] = ("authoritative_unparsable:not_json" if decoded
                    else "authoritative_unparsable:undecodable")
    return out


def authoritative_section_present(snapshot):
    """纯函数：快照是否带权威读数信号（任一侧带即纳入新增判定）。"""
    if not isinstance(snapshot, dict):
        return False
    signals = snapshot.get("window_signals")
    if not isinstance(signals, dict):
        return False
    return any(isinstance(signals.get(name), dict)
               for name in AUTHORITATIVE_SIGNAL_IDS)


def merge_authoritative_readings(snapshot, readings, source=None):
    """纯函数：把提升身份探针的权威读数并进快照，返回 ``(merged, report)``。

    ``readings`` 结构不可用（不是字典／没有 ``window_signals`` 字典）时抛
    ``ValueError``；调用方按非零退出处理，绝不写半份「权威」快照。逐条规则：

    * 信号名必须在 ``WINDOW_SIGNAL_IDS + AUTHORITATIVE_SIGNAL_IDS`` 内，否则拒绝；
    * 条目必须是字典、``status`` 必须是 ok/absent/undecidable、且带齐该信号名的必填键
      （权威读数三类按 ``AUTHORITATIVE_SIGNAL_REQUIRED_KEYS``，其余八类窗口信号按
      ``COMMON_SIGNAL_REQUIRED_KEYS``），否则拒绝（形状不符 = 拒绝，绝不半份接受）；
    * ``watchdog_task``（``AUTHORITATIVE_ENVELOPE_REQUIRED``）必须用本工具自己的
      ``parse_watchdog_query`` + ``watchdog_task_signal`` 从 ``query`` 原始信封
      **重算**（同一口径，不另写一套规则）：``query`` 不是对象（缺失 / 缺字段）→
      ``undecidable``（``authoritative_envelope_missing``）、信封解析不出（``query_ok``
      非 True）→ ``undecidable``（``authoritative_envelope_unparsable``），**绝不**只凭
      自报字段给 PASS；重算与探针结论不一致时同样按 ``undecidable`` 记，绝不折成通过；
    * 只替换同名信号；快照自身的其它字段与未合并信号不动（不提供
      ``--authoritative`` 时行为逐字不变）。
    """
    if not isinstance(readings, dict) or \
            not isinstance(readings.get("window_signals"), dict):
        raise ValueError("authoritative_readings_invalid:window_signals 缺失")
    tool = readings.get("tool")
    if tool is not None and tool != AUTHORITATIVE_READINGS_TOOL:
        raise ValueError("authoritative_readings_tool_mismatch:%r" % (tool,))
    merged = copy.deepcopy(snapshot)
    signals = merged.get("window_signals")
    if not isinstance(signals, dict):
        signals = {}
        merged["window_signals"] = signals
    source = source if isinstance(source, dict) else {}
    captured_at = readings.get("captured_at")
    captured_at_iso = readings.get("captured_at_iso")
    merged_signals, rejected, warnings = [], [], []
    schema_version = readings.get("schema_version")
    if schema_version != AUTHORITATIVE_READINGS_SCHEMA_VERSION:
        # 探针版本与合并口径不一致：记告警（仍按同一口径读，绝不静默当新版本）。
        warnings.append({"signal": None,
                          "reason": "probe_schema_version_mismatch:%r"
                                    % (schema_version,)})
    if readings.get("read_only") is not True:
        warnings.append({"signal": None,
                          "reason": "probe_not_marked_read_only:%r"
                                    % (readings.get("read_only"),)})
    watchdog_query = None
    for name in sorted(readings["window_signals"]):
        entry = readings["window_signals"][name]
        if name not in WINDOW_SIGNAL_IDS + AUTHORITATIVE_SIGNAL_IDS:
            rejected.append({"signal": name, "reason": "unknown_signal"})
            continue
        if not isinstance(entry, dict):
            rejected.append({"signal": name, "reason": "not_a_dict"})
            continue
        status = entry.get("status")
        if status not in SIGNAL_STATUSES:
            rejected.append({"signal": name,
                             "reason": "bad_status:%r" % (status,)})
            continue
        if entry.get("signal") != name:
            # 同一口径：条目自报的信号名必须与它所在的键一致，否则拒绝
            # （防止把某类信号的内容并到另一类名下）。
            rejected.append({"signal": name,
                             "reason": "signal_mismatch:%r"
                                       % (entry.get("signal"),)})
            continue
        required = AUTHORITATIVE_SIGNAL_REQUIRED_KEYS.get(
            name, COMMON_SIGNAL_REQUIRED_KEYS)
        missing = [key for key in required if key not in entry]
        if missing:
            rejected.append({"signal": name,
                             "reason": "missing_keys:%s" % ",".join(missing)})
            continue
        previous = signals.get(name)
        previous_status = (previous.get("status")
                           if isinstance(previous, dict) else None)
        value = copy.deepcopy(entry)
        rederived = False
        if name in AUTHORITATIVE_ENVELOPE_REQUIRED:
            # 需要原始信封重算的信号：没有可解析的信封就没有可核对的权威读数，
            # 一律按 undecidable 记并给原因码——**绝不**只凭自报字段给 PASS
            # （自报的 ok/absent 都不当结论；这里是合并的**唯一判定点**）。
            rederived = True
            envelope = entry.get("query")
            if not isinstance(envelope, dict):
                value = _envelope_required_undecidable(
                    name, "authoritative_envelope_missing",
                    "探针条目没有带原始信封（query）：重算无法执行，只凭自报"
                    "字段不得当结论（读不到 ≠ 通过；先补取证据）")
                warnings.append({"signal": name,
                                 "reason": "probe_envelope_missing"})
            else:
                query = parse_watchdog_query(json.dumps(envelope))
                watchdog_query = query
                if query.get("query_ok") is not True:
                    value = _envelope_required_undecidable(
                        name, "authoritative_envelope_unparsable",
                        "探针的原始信封（query）存在但不可解析（缺字段）："
                        "重算无法执行，只凭自报字段不得当结论（读不到 ≠ 通过；"
                        "先补取证据）")
                    warnings.append({"signal": name,
                                     "reason": "probe_envelope_unparsable"})
                else:
                    # 同一口径：用本工具自己的解析 + 判定规则从原始信封重算。
                    canonical = watchdog_task_signal(query)
                    rederived_status = canonical.get("status")
                    rederived_reason = canonical.get("reason")
                    if rederived_status != status:
                        warnings.append({
                            "signal": name,
                            "reason": "probe_rederivation_mismatch",
                            "probe_status": status,
                            "probe_reason": entry.get("reason"),
                            "rederived_status": rederived_status,
                            "rederived_reason": rederived_reason})
                        canonical["status"] = "undecidable"
                        canonical["reason"] = "probe_rederivation_mismatch"
                        canonical["detail"] = (
                            "探针结论（%s／%s）与本工具重算（%s／%s）不一致：按 "
                            "undecidable 记，绝不折成通过"
                            % (status, entry.get("reason"), rederived_status,
                               rederived_reason))
                    value = canonical
        signals[name] = value
        merged_signals.append({
            "signal": name,
            "status": value.get("status"),
            "previous_status": previous_status,
            "source_captured_at": captured_at,
            "source_captured_at_iso": captured_at_iso,
            "rederived": rederived})
    not_covered = merged.get("not_covered")
    if isinstance(not_covered, list):
        kept = [entry for entry in not_covered
                if not str(entry).startswith(NOT_COVERED_LIMITS)
                and not str(entry).startswith(NOT_COVERED_WATCHDOG)]
        kept.append(
            "权威读数已合并（提升身份只读探针 authoritative_readings.ps1；来源与"
            "采集时刻见 authoritative_readings）：watchdog 任务 / WFP limits / "
            "raw.txt / backend.pid 以该节为准")
        merged["not_covered"] = kept
    report = {
        "source": {
            "tool": tool,
            "schema_version": readings.get("schema_version"),
            "path": source.get("path"),
            "sha256": source.get("sha256"),
            "encoding": source.get("encoding"),
            "read_only": readings.get("read_only"),
            "elevated": readings.get("elevated"),
            "identity": readings.get("identity"),
            "captured_at": captured_at,
            "captured_at_iso": captured_at_iso,
            "watchdog_query": watchdog_query,
        },
        "merged_at": time.time(),
        "merged_at_iso": datetime.now(BEIJING).isoformat(timespec="seconds"),
        "merged_signals": merged_signals,
        "rejected_signals": rejected,
        "warnings": warnings,
        "notes": [
            "合并只替换同名信号；未合并信号与快照其它字段不动（不提供 "
            "--authoritative 时行为与现在逐字相同）",
            "watchdog_task 由本工具从探针的原始 query 信封重算（同一口径）；"
            "信封缺失 / 不可解析即 undecidable（authoritative_envelope_missing / "
            "authoritative_envelope_unparsable），绝不只凭自报字段给 PASS",
            "权威读数由提升身份手动采集：来源路径与 sha256 记录在上面的 source",
        ],
    }
    merged["authoritative_readings"] = report
    return merged, report


# ------------------------------------------------------------- 纯判定层（比对）
def _dig(data, *path):
    """按路径取值；任一段缺失返回 ``_MISSING``（缺失 ≠ None）。"""
    node = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return _MISSING
        node = node[key]
    return node


def _item(item_id, title, gate_ref, verdict, detail):
    return {"id": item_id, "title": title, "gate_ref": gate_ref,
            "verdict": verdict, "detail": detail}


def _listener_state(entry):
    """纯函数：一份监听记录 → ``(state, pids)``。

    ``"ok"`` = 查询成功且有监听者；``"absent"`` = 查询成功且无人监听
    （**掉线**，不是「读不到」）；``"unknown"`` = 字段缺失或不可用
    （读不到 ≠ 没在听）。
    """
    if not isinstance(entry, dict):
        return "unknown", []
    pids = entry.get("pids")
    if not isinstance(pids, list):
        return "unknown", []
    if not pids:
        return "absent", []
    clean = [pid for pid in pids if isinstance(pid, int)
             and not isinstance(pid, bool)]
    if len(clean) != len(pids):
        return "unknown", []
    return "ok", clean


def _item_8096_health(before, after):
    title = "8096 健康（200 ok/usagesrv/8096）且 PID 已变"
    status = _dig(after, "health_8096", "status")
    body = _dig(after, "health_8096", "json")
    before_state, before_pids = _listener_state(
        _dig(before, "listeners", str(CONSOLE_PORT)))
    after_state, after_pids = _listener_state(
        _dig(after, "listeners", str(CONSOLE_PORT)))
    if status is _MISSING or not isinstance(body, dict):
        return _item("8096_health", title, "③.1b 行1", "undecidable",
                     "8096 /health 未取到（status=%r）"
                     % (None if status is _MISSING else status))
    if status != 200 or body.get("status") != "ok" \
            or body.get("service") != "usagesrv" or body.get("port") != CONSOLE_PORT:
        return _item("8096_health", title, "③.1b 行1", "fail",
                     "8096 /health 不符：status=%r body=%s"
                     % (status, json.dumps(body, ensure_ascii=False)[:200]))
    if before_state == "unknown" or after_state == "unknown":
        return _item("8096_health", title, "③.1b 行1", "undecidable",
                     "PID 证据不可用（before=%s after=%s）"
                     % (before_state, after_state))
    if after_state == "absent":
        return _item("8096_health", title, "③.1b 行1", "fail",
                     "8096 无人监听")
    if set(before_pids) & set(after_pids):
        return _item("8096_health", title, "③.1b 行1", "fail",
                     "PID 未变：%s（新代码未载入）" % after_pids)
    return _item("8096_health", title, "③.1b 行1", "pass",
                 "health 200；PID %s → %s" % (before_pids, after_pids))


def _item_same_listener(before, after, port, item_id, title, gate_ref):
    before_state, before_pids = _listener_state(
        _dig(before, "listeners", str(port)))
    after_state, after_pids = _listener_state(
        _dig(after, "listeners", str(port)))
    if after_state == "unknown":
        return _item(item_id, title, gate_ref, "undecidable",
                     "%d 监听 PID 读不到" % port)
    if after_state == "absent":
        return _item(item_id, title, gate_ref, "fail",
                     "%d 已掉线（无人监听）" % port)
    if before_state != "ok":
        return _item(item_id, title, gate_ref, "undecidable",
                     "重启前 %d 监听 PID 不可用（%s）" % (port, before_state))
    if set(before_pids) != set(after_pids):
        return _item(item_id, title, gate_ref, "fail",
                     "%d PID 变了：%s → %s" % (port, before_pids, after_pids))
    return _item(item_id, title, gate_ref, "pass",
                 "%d 仍在监听，PID %s 不变" % (port, after_pids))


def _item_8100_attribution(before, after):
    title = "8100 归因载入状态未回退"
    gate_ref = "③.1b 行3（以密钥文件就绪 + 8100 启动时间推断代替启动行）"
    before_ready = _dig(before, "gateway_attribution", "file_ready")
    after_ready = _dig(after, "gateway_attribution", "file_ready")
    after_value = _dig(after, "gateway_attribution", "instance_loaded_value")
    if before_ready is _MISSING or after_ready is _MISSING \
            or after_value is _MISSING:
        return _item("8100_attribution", title, gate_ref, "undecidable",
                     "密钥文件就绪度/实例载入事实缺失")
    if after_ready is True:
        if after_value == "yes":
            return _item("8100_attribution", title, gate_ref, "pass",
                         "密钥文件就绪，8100 实例推断已载入（inference）")
        if after_value == "no":
            return _item("8100_attribution", title, gate_ref, "fail",
                         "密钥文件已就绪但运行中 8100 实例未载入（key 在 8100 启动之后才出现）")
        return _item("8100_attribution", title, gate_ref, "undecidable",
                     "实例载入状态不可判定：instance_loaded=%r" % (after_value,))
    if after_ready is False:
        if before_ready is True:
            return _item("8100_attribution", title, gate_ref, "fail",
                         "密钥文件由就绪变为不就绪")
        if after_value in ("unknown", None):
            return _item("8100_attribution", title, gate_ref, "pass",
                         "密钥未生成：两端一致报未启用（合法状态，不是故障）")
        return _item("8100_attribution", title, gate_ref, "undecidable",
                     "密钥未就绪但 instance_loaded=%r" % (after_value,))
    return _item("8100_attribution", title, gate_ref, "undecidable",
                 "密钥文件就绪度不可判定：file_ready=%r" % (after_ready,))


def _item_8110_auth(before, after):
    title = "8110 存活且 auth_state 与重启前一致"
    after_observe = _dig(after, "backend_observe")
    before_observe = _dig(before, "backend_observe")
    if not isinstance(after_observe, dict):
        return _item("8110_auth", title, "③.1b 行5", "undecidable",
                     "重启后 8110 观察缺失")
    if after_observe.get("running") is not True:
        return _item("8110_auth", title, "③.1b 行5", "fail",
                     "8110 running=%r（认证无法确认）" % (after_observe.get("running"),))
    after_state = after_observe.get("auth_state")
    if after_state is None:
        return _item("8110_auth", title, "③.1b 行5", "undecidable",
                     "重启后 auth_state 读不到")
    if not isinstance(before_observe, dict) or before_observe.get("auth_state") is None:
        return _item("8110_auth", title, "③.1b 行5", "undecidable",
                     "重启前 auth_state 读不到")
    if before_observe["auth_state"] != after_state:
        return _item("8110_auth", title, "③.1b 行5", "fail",
                     "auth_state 变了：%r → %r"
                     % (before_observe["auth_state"], after_state))
    return _item("8110_auth", title, "③.1b 行5", "pass",
                 "running=True，auth_state=%s 不变" % after_state)


def _item_funnel(before, after):
    title = "Funnel 状态与重启前一致"
    before_on = _dig(before, "share_status", "funnel_on")
    after_on = _dig(after, "share_status", "funnel_on")
    if before_on is _MISSING or after_on is _MISSING \
            or before_on is None or after_on is None:
        return _item("funnel", title, "③.1b 行6", "undecidable",
                     "funnel_on 读不到（before=%r after=%r）"
                     % (None if before_on is _MISSING else before_on,
                        None if after_on is _MISSING else after_on))
    if before_on is True and after_on is not True:
        return _item("funnel", title, "③.1b 行6", "fail",
                     "Funnel 由开变关（停止点）")
    if before_on != after_on:
        return _item("funnel", title, "③.1b 行6", "fail",
                     "funnel_on 变了：%r → %r" % (before_on, after_on))
    return _item("funnel", title, "③.1b 行6", "pass",
                 "funnel_on=%r 不变" % (after_on,))


def _item_share_entry(before, after):
    title = "分享入口白名单探测与重启前一致"
    details = []
    for name, path, want_status, want_code in SHARE_ENTRY_EXPECTED:
        before_probe = _dig(before, "share_entry_probe", name)
        after_probe = _dig(after, "share_entry_probe", name)
        if not isinstance(before_probe, dict) or not isinstance(after_probe, dict):
            return _item("share_gateway", title, "③.1b 行7", "undecidable",
                         "探测结果缺失：%s" % name)
        if after_probe.get("status") is None:
            return _item("share_gateway", title, "③.1b 行7", "undecidable",
                         "重启后探测失败：%s error=%r"
                         % (name, after_probe.get("error")))
        if before_probe.get("status") is None:
            return _item("share_gateway", title, "③.1b 行7", "undecidable",
                         "重启前探测失败：%s error=%r"
                         % (name, before_probe.get("error")))
        if (after_probe.get("status"), after_probe.get("code")) \
                != (want_status, want_code):
            return _item("share_gateway", title, "③.1b 行7", "fail",
                         "%s %s 期望 %d %s，实测 %r %r"
                         % (name, path, want_status, want_code,
                            after_probe.get("status"), after_probe.get("code")))
        if (before_probe.get("status"), before_probe.get("code")) \
                != (want_status, want_code):
            return _item("share_gateway", title, "③.1b 行7", "fail",
                         "重启前 %s 就不符期望：%r %r"
                         % (name, before_probe.get("status"),
                            before_probe.get("code")))
        details.append("%s %s %s→%s" % (name, path, after_probe.get("status"),
                                        after_probe.get("code")))
    return _item("share_gateway", title, "③.1b 行7", "pass",
                 "；".join(details))


def _item_share_ledger(before, after):
    title = "8110 账本只增不减、已归属行不变"
    before_rows = _dig(before, "share_ledger", "rows")
    after_rows = _dig(after, "share_ledger", "rows")
    if not isinstance(before_rows, int) or not isinstance(after_rows, int):
        return _item("share_ledger", title, "③.1b 行8", "undecidable",
                     "账本行数读不到（before=%r after=%r）"
                     % (before_rows, after_rows))
    if after_rows < before_rows:
        return _item("share_ledger", title, "③.1b 行8", "fail",
                     "行数减少：%d → %d" % (before_rows, after_rows))
    before_attributed = _dig(before, "share_ledger", "attributed_rows")
    after_attributed = _dig(after, "share_ledger", "attributed_rows")
    if not isinstance(before_attributed, list) \
            or not isinstance(after_attributed, list):
        return _item("share_ledger", title, "③.1b 行8", "undecidable",
                     "已归属行证据缺失")
    after_by_index = {}
    for row in after_attributed:
        if isinstance(row, dict) and isinstance(row.get("index"), int):
            after_by_index[row["index"]] = row
    for row in before_attributed:
        if not isinstance(row, dict) or not isinstance(row.get("index"), int):
            return _item("share_ledger", title, "③.1b 行8", "undecidable",
                         "已归属行缺少 index")
        current = after_by_index.get(row["index"])
        if current is None:
            return _item("share_ledger", title, "③.1b 行8", "fail",
                         "已归属行 #%d 丢失" % row["index"])
        if (current.get("key_id"), current.get("attribution")) \
                != (row.get("key_id"), row.get("attribution")):
            return _item("share_ledger", title, "③.1b 行8", "fail",
                         "已归属行 #%d 被改写" % row["index"])
    return _item("share_ledger", title, "③.1b 行8", "pass",
                 "行数 %d → %d；已归属行 %d 条全部保留"
                 % (before_rows, after_rows, len(before_attributed)))


def _item_private_ledger(before, after):
    title = "私人总账未被分享链写入（key_id 非空 = 0）"
    gate_ref = "门内不变式 3（附录 C）"
    before_keyed = _dig(before, "private_ledger", "key_id_nonempty")
    after_keyed = _dig(after, "private_ledger", "key_id_nonempty")
    after_verified = _dig(after, "private_ledger", "attribution_verified")
    if not isinstance(before_keyed, int) or not isinstance(after_keyed, int):
        return _item("private_ledger", title, gate_ref, "undecidable",
                     "私人账本 key_id 非空行数读不到（before=%r after=%r）"
                     % (before_keyed, after_keyed))
    if before_keyed != 0:
        return _item("private_ledger", title, gate_ref, "fail",
                     "重启前私人账本已有 %d 行 key_id 非空" % before_keyed)
    if after_keyed != 0:
        return _item("private_ledger", title, gate_ref, "fail",
                     "私人账本出现 %d 行 key_id 非空（分享链可能绕过 8110）"
                     % after_keyed)
    if isinstance(after_verified, int) and after_verified != 0:
        return _item("private_ledger", title, gate_ref, "fail",
                     "私人账本出现 %d 行 attribution=verified" % after_verified)
    return _item("private_ledger", title, gate_ref, "pass",
                 "key_id 非空 = 0（attribution=verified = %r）"
                 % (after_verified,))


def _item_read_surface_shape(before, after):
    title = "8096 归因读面已载入新形状（7 键）"
    gate_ref = "③.3（键集 = 7 键）"
    keys = _dig(after, "share_status", "keys")
    if not isinstance(keys, list) or not keys:
        return _item("read_surface_shape", title, gate_ref, "undecidable",
                     "重启后归因键集读不到")
    shape = attribution_shape(keys)
    if shape == "seven_key":
        return _item("read_surface_shape", title, gate_ref, "pass",
                     "键集 = %s" % sorted(keys))
    if shape == "four_key":
        return _item("read_surface_shape", title, gate_ref, "fail",
                     "仍是 4 键旧形状：新代码未载入")
    return _item("read_surface_shape", title, gate_ref, "undecidable",
                 "键集不认识：%s" % sorted(keys))


def _window_signal(snapshot, name):
    """取快照的窗口信号：``window_signals`` 缺失时返回 ``_MISSING``。"""
    signals = _dig(snapshot, "window_signals")
    if not isinstance(signals, dict):
        return _MISSING
    entry = signals.get(name)
    return entry if isinstance(entry, dict) else _MISSING


def window_section_present(snapshot):
    """纯函数：快照是否带窗口信号节（任一侧带即纳入新增判定）。"""
    return isinstance(snapshot, dict) \
        and isinstance(snapshot.get("window_signals"), dict)


def _item_key_set_transition(before, after):
    title = "8096 读面键集迁移：窗口前形状 → 窗口后 7 键"
    gate_ref = "③.3（键集 = 7 键）"
    before_sig = _window_signal(before, "read_surface_keys")
    after_sig = _window_signal(after, "read_surface_keys")
    if before_sig is _MISSING or after_sig is _MISSING:
        return _item("key_set_transition", title, gate_ref, "undecidable",
                     "该侧快照不含 window_signals.read_surface_keys"
                     "（旧快照请用本工具重取）")
    after_shape = after_sig.get("shape")
    before_shape = before_sig.get("shape")
    if not isinstance(after_shape, str) or after_shape == "unknown":
        return _item("key_set_transition", title, gate_ref, "undecidable",
                     "重启后键集形状不可判定：keys=%r"
                     % (after_sig.get("keys"),))
    if after_shape != "seven_key":
        return _item("key_set_transition", title, gate_ref, "fail",
                     "重启后仍是 %s（%s）：新代码未载入"
                     % (after_shape, after_sig.get("keys")))
    if before_shape == "four_key":
        return _item("key_set_transition", title, gate_ref, "pass",
                     "键集 4 键 → 7 键：新代码已载入")
    if before_shape == "seven_key":
        return _item("key_set_transition", title, gate_ref, "pass",
                     "窗口前已是 7 键（本窗口对该读面本无必要）；重启后仍 7 键")
    return _item("key_set_transition", title, gate_ref, "pass",
                 "键集 %r → 7 键（窗口前形状不在已知两种，按实际记录）"
                 % (before_shape,))


def _item_gateway_image_start(before, after):
    title = "8100 镜像名与启动时间不变（未随 8096 重启）"
    gate_ref = "③.1b 行2/行3 补强（8100 不随 8096 重启）"
    before_sig = _window_signal(before, "gateway_8100_image_start")
    after_sig = _window_signal(after, "gateway_8100_image_start")
    if before_sig is _MISSING or after_sig is _MISSING:
        return _item("gateway_image_start", title, gate_ref, "undecidable",
                     "该侧快照不含 window_signals.gateway_8100_image_start"
                     "（旧快照请用本工具重取）")
    if after_sig.get("status") == "absent":
        return _item("gateway_image_start", title, gate_ref, "fail",
                     "8100 不在监听（掉线）")
    if after_sig.get("status") != "ok":
        return _item("gateway_image_start", title, gate_ref, "undecidable",
                     "重启后 8100 镜像/启动时间读不到：reason=%s"
                     % (after_sig.get("reason"),))
    if before_sig.get("status") != "ok":
        return _item("gateway_image_start", title, gate_ref, "undecidable",
                     "重启前 8100 镜像/启动时间读不到：reason=%s"
                     % (before_sig.get("reason"),))
    if after_sig.get("image") not in GATEWAY_IMAGE_NAMES:
        return _item("gateway_image_start", title, gate_ref, "fail",
                     "8100 镜像名 %r 不在期望集 %s"
                     % (after_sig.get("image"), list(GATEWAY_IMAGE_NAMES)))
    if after_sig.get("pid") != before_sig.get("pid"):
        return _item("gateway_image_start", title, gate_ref, "fail",
                     "8100 PID 变了：%s → %s"
                     % (before_sig.get("pid"), after_sig.get("pid")))
    if after_sig.get("started_at") != before_sig.get("started_at"):
        return _item("gateway_image_start", title, gate_ref, "fail",
                     "8100 启动时间变了：%r → %r（8100 被重启过）"
                     % (before_sig.get("started_at"),
                        after_sig.get("started_at")))
    return _item("gateway_image_start", title, gate_ref, "pass",
                 "image=%s pid=%s started_at=%r 不变"
                 % (after_sig.get("image"), after_sig.get("pid"),
                    after_sig.get("started_at")))


def _item_backend_parent_relation(before, after):
    title = "8110 PID 与父 PID：父 PID = 窗口前 8096 PID（孤儿签名）"
    gate_ref = "③.1b 行4 补强（附录 B：ParentProcessId 指向已死进程）"
    before_sig = _window_signal(before, "backend_8110_pid_parent")
    after_sig = _window_signal(after, "backend_8110_pid_parent")
    if before_sig is _MISSING or after_sig is _MISSING:
        return _item("backend_parent_relation", title, gate_ref, "undecidable",
                     "该侧快照不含 window_signals.backend_8110_pid_parent"
                     "（旧快照请用本工具重取）")
    if before_sig.get("status") != "ok":
        return _item("backend_parent_relation", title, gate_ref, "undecidable",
                     "窗口前 8110 PID/父 PID 读不到：reason=%s"
                     % (before_sig.get("reason"),))
    if after_sig.get("status") == "absent":
        return _item("backend_parent_relation", title, gate_ref, "fail",
                     "8110 不在监听（掉线）")
    if after_sig.get("status") != "ok":
        return _item("backend_parent_relation", title, gate_ref, "undecidable",
                     "重启后 8110 PID/父 PID 读不到：reason=%s"
                     % (after_sig.get("reason"),))
    if after_sig.get("backend_pid") != before_sig.get("backend_pid"):
        return _item("backend_parent_relation", title, gate_ref, "fail",
                     "8110 PID 变了：%s → %s"
                     % (before_sig.get("backend_pid"),
                        after_sig.get("backend_pid")))
    before_console = before_sig.get("console_pid")
    after_console = after_sig.get("console_pid")
    if before_console is None or after_console is None:
        return _item("backend_parent_relation", title, gate_ref, "undecidable",
                     "8096 PID 读不到：父 PID 关系无从比较")
    if before_console == after_console:
        return _item("backend_parent_relation", title, gate_ref, "undecidable",
                     "8096 PID 未变（%s）：窗口未发生，孤儿签名无从观察"
                     % (after_console,))
    if after_sig.get("parent_pid") == before_console:
        # 判定用的是 before_console（旧 8096 PID）；detail 必须印同一个值——
        # 印 after_console（新 8096 PID）会把这行读成「新 PID 是旧 PID」。
        return _item("backend_parent_relation", title, gate_ref, "pass",
                     "8110 PID=%s 不变；父 PID=%s = 旧 8096 PID（已死），"
                     "符合 detached 孤儿预期"
                     % (after_sig.get("backend_pid"), before_console))
    if after_sig.get("parent_pid") == after_console:
        return _item("backend_parent_relation", title, gate_ref, "fail",
                     "8110 的父 PID=%s = 新 8096 PID：8110 被新 8096 拉起"
                     "（随 8096 一并重启）" % (after_console,))
    return _item("backend_parent_relation", title, gate_ref, "undecidable",
                 "8110 父 PID=%s 既不是旧 8096 PID（%s）也不是新 8096 PID（%s）"
                 % (after_sig.get("parent_pid"), before_console, after_console))


def _item_watchdog_task(before, after):
    title = "watchdog 任务状态与窗口前一致（读不到即 UNDECIDABLE，绝不记成「未注册」）"
    gate_ref = "③.1b 补强（维护者点名的历史事件：重启 8096 后分享链被 watchdog 停止）"
    before_sig = _window_signal(before, "watchdog_task")
    after_sig = _window_signal(after, "watchdog_task")
    if before_sig is _MISSING or after_sig is _MISSING:
        return _item("watchdog_task", title, gate_ref, "undecidable",
                     "该侧快照不含 window_signals.watchdog_task"
                     "（旧快照请用本工具重取）")
    after_status = after_sig.get("status")
    if after_status == "undecidable":
        return _item("watchdog_task", title, gate_ref, "undecidable",
                     "重启后 watchdog 任务读不到：reason=%s（读不到 ≠ 未注册）"
                     % (after_sig.get("reason"),))
    if before_sig.get("status") == "undecidable":
        return _item("watchdog_task", title, gate_ref, "undecidable",
                     "窗口前 watchdog 任务读不到：reason=%s（读不到 ≠ 未注册）"
                     % (before_sig.get("reason"),))
    before_status = before_sig.get("status")
    if before_status == "ok" and after_status == "ok":
        if before_sig.get("state") == after_sig.get("state"):
            return _item("watchdog_task", title, gate_ref, "pass",
                         "State=%s（%s）不变"
                         % (after_sig.get("state_text"), after_sig.get("state")))
        return _item("watchdog_task", title, gate_ref, "fail",
                     "watchdog State 变了：%s → %s（历史事件：重启后分享链被停）"
                     % (before_sig.get("state_text"), after_sig.get("state_text")))
    if before_status == "absent" and after_status == "absent":
        return _item("watchdog_task", title, gate_ref, "pass",
                     "两侧权威读数都是『未注册』（提升身份）：窗口未改变该事实")
    if before_status == "absent" and after_status == "ok":
        return _item("watchdog_task", title, gate_ref, "fail",
                     "watchdog 由未注册变为 %s（窗口期间被注册）"
                     % (after_sig.get("state_text"),))
    return _item("watchdog_task", title, gate_ref, "fail",
                 "watchdog 由 %s 变为未注册（分享链失去看护）"
                 % (before_sig.get("state_text"),))


def _item_ledger_last_attribution(before, after):
    title = "8110 账本末行 attribution 未回退"
    gate_ref = "③.1b 行8 补强（末行归因；④.3 负向判据）"
    before_sig = _window_signal(before, "share_ledger_tail")
    after_sig = _window_signal(after, "share_ledger_tail")
    if before_sig is _MISSING or after_sig is _MISSING:
        return _item("ledger_last_attribution", title, gate_ref, "undecidable",
                     "该侧快照不含 window_signals.share_ledger_tail"
                     "（旧快照请用本工具重取）")
    after_status = after_sig.get("status")
    before_status = before_sig.get("status")
    if after_status == "undecidable":
        return _item("ledger_last_attribution", title, gate_ref, "undecidable",
                     "重启后账本末行读不到：reason=%s" % (after_sig.get("reason"),))
    if after_status == "absent":
        if before_status == "absent":
            return _item("ledger_last_attribution", title, gate_ref, "pass",
                         "两侧账本都无行（末行不存在）")
        return _item("ledger_last_attribution", title, gate_ref, "fail",
                     "账本末行消失：%r → 0 行" % (before_sig.get("rows"),))
    if before_status != "ok":
        return _item("ledger_last_attribution", title, gate_ref, "undecidable",
                     "窗口前账本末行读不到：reason=%s"
                     % (before_sig.get("reason"),))
    before_index = before_sig.get("last_index")
    after_index = after_sig.get("last_index")
    if not isinstance(after_index, int) or not isinstance(before_index, int):
        return _item("ledger_last_attribution", title, gate_ref, "undecidable",
                     "末行 index 读不到（before=%r after=%r）"
                     % (before_index, after_index))
    if after_index < before_index:
        return _item("ledger_last_attribution", title, gate_ref, "fail",
                     "末行 index 回退：%d → %d" % (before_index, after_index))
    if after_index == before_index:
        before_pair = (before_sig.get("last_key_id"),
                       before_sig.get("last_attribution"))
        after_pair = (after_sig.get("last_key_id"),
                      after_sig.get("last_attribution"))
        if before_pair != after_pair:
            return _item("ledger_last_attribution", title, gate_ref, "fail",
                         "末行 #%d 被改写：%r/%r → %r/%r"
                         % (after_index, before_pair[0], before_pair[1],
                            after_pair[0], after_pair[1]))
        return _item("ledger_last_attribution", title, gate_ref, "pass",
                     "末行 #%d 不变：key_id=%r attribution=%r"
                     % (after_index, after_pair[0], after_pair[1]))
    if after_sig.get("last_attribution") == "bad_signature":
        return _item("ledger_last_attribution", title, gate_ref, "fail",
                     "窗口期间出现新行 attribution=bad_signature"
                     "（两端不是同一文件；SHARING.md:141-144）")
    return _item("ledger_last_attribution", title, gate_ref, "pass",
                 "窗口期间新增 %d 行；末行 #%d attribution=%r key_id=%r"
                 % (after_index - before_index, after_index,
                    after_sig.get("last_attribution"),
                    after_sig.get("last_key_id")))


def _item_share_entry_faults(before, after):
    title = "分享入口故障证据未回退（broker host-evidence{faults} 只读尾读）"
    gate_ref = "§8.3/§8.6（故障留证；未接线不折成无故障）"
    before_sig = _window_signal(before, "share_entry_faults")
    after_sig = _window_signal(after, "share_entry_faults")
    if before_sig is _MISSING or after_sig is _MISSING:
        return _item("share_entry_faults", title, gate_ref, "undecidable",
                     "该侧快照不含 window_signals.share_entry_faults"
                     "（旧快照请用本工具重取；读不到 ≠ 无故障）")
    for label, sig in (("窗口前", before_sig), ("重启后", after_sig)):
        if sig.get("status") == "undecidable":
            return _item("share_entry_faults", title, gate_ref, "undecidable",
                         "%s 故障证据不可得：reason=%s（读不到 ≠ 无故障；"
                         "先补取证据，不得报通过）"
                         % (label, sig.get("reason")))
    before_status = before_sig.get("status")
    after_status = after_sig.get("status")
    if before_status == "absent" and after_status == "absent":
        return _item("share_entry_faults", title, gate_ref, "pass",
                     "两侧故障证据都确定无故障记录（可读、counts 全 0、"
                     "last_fault=null）")
    if before_status == "absent" or after_status == "absent":
        return _item("share_entry_faults", title, gate_ref, "undecidable",
                     "一侧确定无故障记录、另一侧有故障记录（%s → %s）："
                     "可能是 256KiB 轮转（.1）或文件被替换，需人工核对"
                     % (before_status, after_status))
    before_last = before_sig.get("last_fault")
    after_last = after_sig.get("last_fault")
    before_at = before_last.get("at") if isinstance(before_last, dict) else None
    after_at = after_last.get("at") if isinstance(after_last, dict) else None
    if not isinstance(before_at, (int, float)) \
            or not isinstance(after_at, (int, float)):
        return _item("share_entry_faults", title, gate_ref, "undecidable",
                     "末条故障记录时间读不到（before=%r after=%r）"
                     % (before_at, after_at))
    if after_at < before_at:
        return _item("share_entry_faults", title, gate_ref, "fail",
                     "末条故障记录时间回退：%r → %r（故障文件被重写/替换）"
                     % (before_at, after_at))
    return _item("share_entry_faults", title, gate_ref, "pass",
                 "末条故障 %s/%s at=%r → %s/%s at=%r 未回退；"
                 "counts 差异只作参考（读面是末 50 行有界窗口）"
                 % (before_last.get("phase"), before_last.get("code"),
                    before_at, after_last.get("phase"),
                    after_last.get("code"), after_at))


def _authoritative_side_missing(signal_id, title, gate_ref):
    return _item(signal_id, title, gate_ref, "undecidable",
                 "该侧快照不含 window_signals.%s（未跑提升身份只读探针 "
                 "authoritative_readings.ps1，或未用 --authoritative 合并）："
                 "读不到 ≠ 通过" % signal_id)


def _item_wfp_limits(before, after):
    title = "分享限制（WFP strong + 出站阻断规则）与窗口前一致"
    gate_ref = "③.1b 补强（limits 丢失 = 看门狗撤公网；提升身份权威读数）"
    before_sig = _window_signal(before, "wfp_limits")
    after_sig = _window_signal(after, "wfp_limits")
    if before_sig is _MISSING or after_sig is _MISSING:
        return _authoritative_side_missing("wfp_limits", title, gate_ref)
    for label, sig in (("窗口前", before_sig), ("重启后", after_sig)):
        if sig.get("status") == "undecidable":
            return _item("wfp_limits", title, gate_ref, "undecidable",
                         "%s limits 读不到：reason=%s（读不到 ≠ limits 在位）"
                         % (label, sig.get("reason")))
        if sig.get("status") == "absent":
            return _item("wfp_limits", title, gate_ref, "fail",
                         "%s limits 不在位：reason=%s（看门狗会撤公网）"
                         % (label, sig.get("reason")))
    if before_sig.get("app_exe") != after_sig.get("app_exe"):
        return _item("wfp_limits", title, gate_ref, "fail",
                     "WFP 镜像路径变了：%r → %r"
                     % (before_sig.get("app_exe"), after_sig.get("app_exe")))
    if before_sig.get("backend_port") != after_sig.get("backend_port"):
        return _item("wfp_limits", title, gate_ref, "fail",
                     "许可端口变了：%r → %r（backend-port.txt 被改写）"
                     % (before_sig.get("backend_port"),
                        after_sig.get("backend_port")))
    for label, sig in (("窗口前", before_sig), ("重启后", after_sig)):
        if sig.get("wfp_ok") is not True or sig.get("firewall_ok") is not True:
            return _item("wfp_limits", title, gate_ref, "fail",
                         "%s 权威读数自称 ok 但 wfp_ok=%r firewall_ok=%r"
                         % (label, sig.get("wfp_ok"), sig.get("firewall_ok")))
    return _item("wfp_limits", title, gate_ref, "pass",
                 "app_exe=%s port=%s；WFP strong=OK 且出站阻断规则在位，两侧一致"
                 % (after_sig.get("app_exe"), after_sig.get("backend_port")))


def _item_raw_txt(before, after):
    title = "8100 启动日志 raw.txt 未被重写（提升身份权威读数：路径与 mtime 不变）"
    gate_ref = "③.1b 行3 补强（8100 未随 8096 重启）"
    before_sig = _window_signal(before, "raw_txt")
    after_sig = _window_signal(after, "raw_txt")
    if before_sig is _MISSING or after_sig is _MISSING:
        return _authoritative_side_missing("raw_txt", title, gate_ref)
    for label, sig in (("窗口前", before_sig), ("重启后", after_sig)):
        if sig.get("status") == "undecidable":
            return _item("raw_txt", title, gate_ref, "undecidable",
                         "%s raw.txt 读不到：reason=%s（读不到 ≠ 没有 raw.txt）"
                         % (label, sig.get("reason")))
    if before_sig.get("status") == "absent" and after_sig.get("status") == "absent":
        return _item("raw_txt", title, gate_ref, "pass",
                     "两侧都没有 raw.txt（该事实未改变）")
    if after_sig.get("status") == "absent":
        return _item("raw_txt", title, gate_ref, "fail",
                     "raw.txt 在窗口期间消失：%r → 无" % (before_sig.get("path"),))
    if before_sig.get("status") == "absent":
        return _item("raw_txt", title, gate_ref, "fail",
                     "raw.txt 在窗口期间出现：8100 任务被重跑过")
    if before_sig.get("path") != after_sig.get("path"):
        return _item("raw_txt", title, gate_ref, "fail",
                     "raw.txt 路径变了：%r → %r"
                     % (before_sig.get("path"), after_sig.get("path")))
    if before_sig.get("mtime") is None or after_sig.get("mtime") is None:
        return _item("raw_txt", title, gate_ref, "undecidable",
                     "raw.txt mtime 读不到（before=%r after=%r）"
                     % (before_sig.get("mtime"), after_sig.get("mtime")))
    if before_sig.get("mtime") != after_sig.get("mtime"):
        return _item("raw_txt", title, gate_ref, "fail",
                     "raw.txt 被重写：mtime %r → %r（8100 任务在窗口期间被重跑）"
                     % (before_sig.get("mtime"), after_sig.get("mtime")))
    return _item("raw_txt", title, gate_ref, "pass",
                 "path=%s mtime=%r 不变（size %r → %r）"
                 % (after_sig.get("path"), after_sig.get("mtime"),
                    before_sig.get("size"), after_sig.get("size")))


def _item_backend_pid_file(before, after):
    title = "8110 backend.pid 字节内容不变（提升身份权威读数）"
    gate_ref = "③.1b 行4 补强（8110 未被重新拉起）"
    before_sig = _window_signal(before, "backend_pid_file")
    after_sig = _window_signal(after, "backend_pid_file")
    if before_sig is _MISSING or after_sig is _MISSING:
        return _authoritative_side_missing("backend_pid_file", title, gate_ref)
    for label, sig in (("窗口前", before_sig), ("重启后", after_sig)):
        if sig.get("status") == "undecidable":
            return _item("backend_pid_file", title, gate_ref, "undecidable",
                         "%s backend.pid 读不到：reason=%s（读不到 ≠ 不存在）"
                         % (label, sig.get("reason")))
    if before_sig.get("status") == "absent" and after_sig.get("status") == "absent":
        return _item("backend_pid_file", title, gate_ref, "pass",
                     "两侧都没有 backend.pid（该事实未改变）")
    if after_sig.get("status") == "absent":
        return _item("backend_pid_file", title, gate_ref, "fail",
                     "backend.pid 在窗口期间消失：%r → 无"
                     % (before_sig.get("path"),))
    if before_sig.get("status") == "absent":
        return _item("backend_pid_file", title, gate_ref, "fail",
                     "backend.pid 在窗口期间出现：8110 被重新拉起过")
    before_bytes = before_sig.get("bytes_hex")
    after_bytes = after_sig.get("bytes_hex")
    if not isinstance(before_bytes, str) or not isinstance(after_bytes, str):
        before_bytes = before_sig.get("content")
        after_bytes = after_sig.get("content")
    if before_bytes != after_bytes:
        return _item("backend_pid_file", title, gate_ref, "fail",
                     "backend.pid 字节内容被改写：%r → %r（8110 被重新拉起）"
                     % (before_sig.get("content"), after_sig.get("content")))
    return _item("backend_pid_file", title, gate_ref, "pass",
                 "path=%s 字节=%r（%r）不变"
                 % (after_sig.get("path"), after_bytes,
                    after_sig.get("content")))


def _window_items(before, after):
    """新增的 6 项窗口判定（+ 有权威读数时的 3 项）。

    **纳入规则**：任一侧快照含 ``window_signals`` 时纳入前 6 项；两侧都是 round7
    schema（无该节）时不纳入——旧快照没有任何 watchdog 来源，强行判定会把
    「本快照没查过」误报成「未注册」（见 ``format_comparison`` 的说明行）。
    混用（一侧旧、一侧新）时纳入，缺失侧按 ``undecidable`` 记。

    权威读数 3 项（WFP limits / raw.txt / backend.pid）只在**任一侧带这三类信号**
    时纳入（即窗口前后都跑了提升身份探针）；两侧都没跑时它们不参与判定，输出里会
    打印一行说明（未跑 ≠ 通过，也 ≠ 失败）。
    """
    if not (window_section_present(before) or window_section_present(after)):
        return []
    items = [
        _item_key_set_transition(before, after),
        _item_gateway_image_start(before, after),
        _item_backend_parent_relation(before, after),
        _item_watchdog_task(before, after),
        _item_ledger_last_attribution(before, after),
        _item_share_entry_faults(before, after),
    ]
    if authoritative_section_present(before) or authoritative_section_present(after):
        items = items + [
            _item_wfp_limits(before, after),
            _item_raw_txt(before, after),
            _item_backend_pid_file(before, after),
        ]
    return items


def compare_snapshots(before, after):
    """纯函数：两份快照 → 逐项判定（字典进、字典出）。

    ``before``/``after`` 是 ``--out`` 写出的快照字典。返回
    ``{"items": [...], "ok": bool, "exit_code": 0|2,
    "window_signals_compared": bool}``：全 ``pass`` 才 0，
    任一 ``fail``/``undecidable`` 都是 2（读不到 ≠ 通过）。

    round7 的 10 项语义逐字保留（只增不删）；窗口 6 项只在任一侧含
    ``window_signals`` 时纳入（``window_signals_compared``）；权威读数 3 项只在
    任一侧含 WFP limits / raw.txt / backend.pid 时纳入
    （``authoritative_compared``）。
    """
    items = [
        _item_8096_health(before, after),
        _item_same_listener(before, after, GATEWAY_PORT, "8100_listening",
                             "8100 不随 8096 重启（仍在监听、PID 不变）",
                             "③.1b 行2"),
        _item_8100_attribution(before, after),
        _item_same_listener(before, after, BACKEND_PORT, "8110_listening",
                             "8110 不随 8096 重启（仍在监听、PID 不变）",
                             "③.1b 行4"),
        _item_8110_auth(before, after),
        _item_funnel(before, after),
        _item_share_entry(before, after),
        _item_share_ledger(before, after),
        _item_private_ledger(before, after),
        _item_read_surface_shape(before, after),
    ]
    window_items = _window_items(before, after)
    items = items + window_items
    ok = all(item["verdict"] == "pass" for item in items)
    return {"items": items, "ok": ok, "exit_code": 0 if ok else 2,
            "window_signals_compared": bool(window_items),
            "authoritative_compared": (authoritative_section_present(before)
                                      or authoritative_section_present(after))}


def format_comparison(result):
    """把判定结果排成逐项 PASS/FAIL/UNDECIDABLE 的可读文本。

    措辞按维护者 2026-09-28 的要求对齐：``UNDECIDABLE`` 项打印
    「先补取证据，不得报通过」（读不到 ≠ 通过，也不因此回退）；``FAIL`` 项才
    打印「按 §⑤ 回退」。
    """
    lines = []
    for item in result["items"]:
        line = "[%s] %s（%s）—— %s" % (item["verdict"].upper(),
                                       item["title"], item["gate_ref"],
                                       item["detail"])
        if item["verdict"] == "fail":
            line += "；处置：%s" % GUIDANCE_FAIL
        elif item["verdict"] == "undecidable":
            line += "；处置：%s" % GUIDANCE_UNDECIDABLE
        lines.append(line)
    lines.append("=" * 60)
    if not result.get("window_signals_compared", False):
        lines.append("注：两侧快照都不含 window_signals（round7 schema）："
                     "watchdog 等 6 项窗口判定未纳入；窗口前后请用本工具重取快照。")
    elif not result.get("authoritative_compared", False):
        lines.append("注：两侧快照都不含权威读数信号（wfp_limits / raw_txt / "
                     "backend.pid）：这 3 项未纳入（未跑提升身份探针 ≠ 通过）；"
                     "窗口前后各跑一次 authoritative_readings.ps1 并用 "
                     "--authoritative 合并后才纳入。")
    if result["ok"]:
        lines.append("结论：全部 PASS（退出码 0）")
    else:
        failed = [item["id"] for item in result["items"]
                  if item["verdict"] == "fail"]
        undecided = [item["id"] for item in result["items"]
                     if item["verdict"] == "undecidable"]
        lines.append("结论：存在非 PASS 项 %s（退出码 2）"
                     % (failed + undecided))
        if failed:
            lines.append("处置（FAIL %s）：%s" % (failed, GUIDANCE_FAIL))
        if undecided:
            lines.append("处置（UNDECIDABLE %s）：%s（读不到 ≠ 通过，不得据此报通过）"
                         % (undecided, GUIDANCE_UNDECIDABLE))
    return "\n".join(lines)


def format_snapshot_summary(snapshot):
    """快照的人读摘要；只打印路径/原因码/长度，绝不打印密钥内容。"""
    lines = ["[快照] %s（schema v%s）"
             % (snapshot.get("captured_at_iso"), snapshot.get("schema_version"))]
    listeners = snapshot.get("listeners", {})
    for port in CHAIN_PORTS:
        entry = listeners.get(str(port), {})
        lines.append("  监听 %d: pids=%s reason=%s"
                     % (port, entry.get("pids"), entry.get("reason")))
    health = snapshot.get("health_8096", {})
    lines.append("  8096 /health: status=%s body=%s"
                 % (health.get("status"),
                    json.dumps(health.get("json"), ensure_ascii=False)
                    if isinstance(health.get("json"), dict) else health.get("error")))
    chain = snapshot.get("chain_parent", {})
    lines.append("  8096↔8110: %s（parent=%s child=%s child_parent=%s）"
                 % (chain.get("relation"), chain.get("parent_pid"),
                    chain.get("child_pid"), chain.get("child_parent_pid")))
    gateway = snapshot.get("gateway_8100", {})
    lines.append("  8100: pid=%s image=%s started_at=%s"
                 % (gateway.get("pid"), gateway.get("image"),
                    gateway.get("started_at")))
    status = snapshot.get("share_status", {})
    lines.append("  分享状态: http=%s funnel_on=%s 键集=%s 形状=%s"
                 % (status.get("http_status"), status.get("funnel_on"),
                    status.get("keys"), status.get("shape")))
    attribution = snapshot.get("gateway_attribution", {})
    lines.append("  归因(同身份直调): file_ready=%s file_reason=%s key_chars=%s "
                 "instance_loaded=%s/%s"
                 % (attribution.get("file_ready"),
                    attribution.get("file_reason"), attribution.get("key_chars"),
                    attribution.get("instance_loaded_value"),
                    attribution.get("instance_loaded_reason")))
    backend = snapshot.get("backend_observe", {})
    lines.append("  8110: running=%s auth_state=%s"
                 % (backend.get("running"), backend.get("auth_state")))
    probes = snapshot.get("share_entry_probe", {})
    for name, path, want_status, want_code in SHARE_ENTRY_EXPECTED:
        probe = probes.get(name, {})
        lines.append("  入口探测 %s %s: status=%s code=%s（期望 %d %s）"
                     % (name, path, probe.get("status"), probe.get("code"),
                        want_status, want_code))
    operations = snapshot.get("operations", {})
    lines.append("  在途操作: total=%s live(queued/running)=%s"
                 % (operations.get("total"), operations.get("live")))
    ledger = snapshot.get("share_ledger", {})
    lines.append("  分享账本: rows=%s last=%s counts=%s error=%s"
                 % (ledger.get("rows"),
                    json.dumps(ledger.get("last"), ensure_ascii=False)
                    if ledger.get("last") else None,
                    json.dumps(ledger.get("attribution_counts"),
                               ensure_ascii=False),
                    ledger.get("error")))
    private = snapshot.get("private_ledger", {})
    lines.append("  私人账本: rows=%s key_id_nonempty=%s verified=%s error=%s"
                 % (private.get("rows"), private.get("key_id_nonempty"),
                    private.get("attribution_verified"), private.get("error")))
    signals = snapshot.get("window_signals", {})
    for name in WINDOW_SIGNAL_IDS:
        entry = signals.get(name, {}) if isinstance(signals, dict) else {}
        lines.append("  窗口信号 %s: [%s] %s"
                     % (name, str(entry.get("status", "missing")).upper(),
                        entry.get("detail") or entry.get("reason")))
    for name in (MAINTENANCE_STATE_KEY, SUPERVISION_STATE_KEY):
        entry = snapshot.get(name, {})
        entry = entry if isinstance(entry, dict) else {}
        lines.append("  证据节 %s: [%s] %s"
                     % (name, str(entry.get("status", "missing")).upper(),
                        entry.get("detail") or entry.get("reason")))
    authoritative = snapshot.get("authoritative_readings")
    if isinstance(authoritative, dict):
        source = authoritative.get("source", {})
        lines.append("  权威读数: tool=%s elevated=%s identity=%s 采集=%s "
                     "sha256=%s"
                     % (source.get("tool"), source.get("elevated"),
                        source.get("identity"), source.get("captured_at_iso"),
                        str(source.get("sha256"))[:12]))
        for name in AUTHORITATIVE_SIGNAL_IDS:
            entry = signals.get(name, {}) if isinstance(signals, dict) else {}
            lines.append("  权威信号 %s: [%s] %s"
                         % (name, str(entry.get("status", "missing")).upper(),
                            entry.get("detail") or entry.get("reason")))
        merged = authoritative.get("merged_signals", [])
        lines.append("  权威读数合并: %s"
                     % "；".join("%s %s←%s"
                                 % (row.get("signal"), row.get("status"),
                                    row.get("previous_status"))
                                 for row in merged if isinstance(row, dict)))
        rejected = authoritative.get("rejected_signals", [])
        if rejected:
            lines.append("  权威读数被拒绝: %s"
                         % "；".join("%s（%s）"
                                     % (row.get("signal"), row.get("reason"))
                                     for row in rejected
                                     if isinstance(row, dict)))
        for warning in authoritative.get("warnings", []):
            if isinstance(warning, dict):
                lines.append("  权威读数告警: %s（%s）探针=%s 重算=%s"
                             % (warning.get("signal"), warning.get("reason"),
                                warning.get("probe_status"),
                                warning.get("rederived_status")))
    watchdog = snapshot.get("watchdog_task_query", {})
    lines.append("  watchdog 查询: elevated=%s identity=%s found=%s error=%s"
                 % (watchdog.get("elevated"), watchdog.get("identity"),
                    watchdog.get("found"), watchdog.get("error")))
    lines.append("  未覆盖: %s" % "；".join(snapshot.get("not_covered", [])))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="分享链只读现场快照 + 8096 重启前后核对（动作门 ③.1b）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--out", metavar="PATH",
                       help="采集只读现场并写成 JSON 快照（唯一写动作）")
    group.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"),
                       help="逐项比对两份快照；全 PASS=0，任一项非 PASS=2")
    parser.add_argument(
        "--authoritative", metavar="PATH",
        help="提升身份只读探针（tools/um-share/authoritative_readings.ps1）产出的 JSON："
             "把权威读数并进快照并记录来源与采集时刻（加性；不提供时行为与现在"
             "逐字相同；只与 --out 同用）")
    args = parser.parse_args(argv)

    if args.authoritative and not args.out:
        print("[比对] --authoritative 只用于 --out（把权威读数并进快照）；"
              "--compare 读的是已合并的快照本身", file=sys.stderr)
        return 2

    if args.out:
        try:
            snapshot = collect_snapshot()
            if args.authoritative:
                loaded = load_authoritative_file(args.authoritative)
                if loaded["error"]:
                    print("[快照] 权威读数不可用：%s（%s）"
                          % (loaded["error"], args.authoritative),
                          file=sys.stderr)
                    return 2
                snapshot, report = merge_authoritative_readings(
                    snapshot, loaded["readings"],
                    source={"path": loaded["path"], "sha256": loaded["sha256"],
                            "encoding": loaded.get("encoding")})
                for entry in report["rejected_signals"]:
                    print("[快照] 权威读数被拒绝：%s（%s）"
                          % (entry["signal"], entry["reason"]), file=sys.stderr)
                for warning in report["warnings"]:
                    print("[快照] 权威读数告警：%s（%s：探针=%s 重算=%s）"
                          % (warning.get("signal"), warning.get("reason"),
                             warning.get("probe_status"),
                             warning.get("rederived_status")),
                          file=sys.stderr)
            write_snapshot(args.out, snapshot)
        except Exception as exc:  # noqa: BLE001 — 采集/写出失败如实报非零
            print("[快照] 失败：%s: %s" % (type(exc).__name__, exc),
                  file=sys.stderr)
            return 2
        print(format_snapshot_summary(snapshot))
        print("[快照] 已写出：%s" % args.out)
        return 0

    try:
        before = read_json_file(args.compare[0])
        after = read_json_file(args.compare[1])
    except (OSError, ValueError) as exc:
        print("[比对] 读取快照失败：%s: %s" % (type(exc).__name__, exc),
              file=sys.stderr)
        return 2
    result = compare_snapshots(before, after)
    print(format_comparison(result))
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
