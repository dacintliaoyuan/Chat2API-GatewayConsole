# -*- coding: utf-8 -*-
"""SYSTEM watchdog（``umshare-watchdog``）的离线判定件：什么条件下停/重启分享链。

把 ``ai-gateway/um-share-deploy/watchdog-umshare.ps1``（516 行）的判定**逐条压成
纯函数**：输入一份「信号快照」字典（进程列表、任务状态、日志尾部、PID/父 PID、端口、
limits/broker 读数），输出「看门狗会怎么判」的结论与命中的规则。**不连生产、不重启、
不写文件**：本工具唯一的写动作是调用方自己把 ``--json`` 结果重定向到文件（本工具自身
只往 stdout 打印）。

判定来源（逐条给行号；行号按**本文件写入时**的
``ai-gateway/um-share-deploy/watchdog-umshare.ps1``；该脚本此后未变——
复核者用 ``git diff --stat 06317d46..a86b3d78 -- …/watchdog-umshare.ps1``（无输出）核对过，
但 HEAD 会继续前进，故引用前先自己核行号）：

* ``Get-LimitsState``（:154-171）+ ``Get-FirewallState``（:138-152）→ limits 半场；
  主循环先算 limits（:407-411），**只有 WFP 半场通过才继续查防火墙**（:410）。
* ``Get-ShareState``（:174-200）→ 分享链健康：**8100 端口上的监听者 PID**，再用
  ``Win32_Process`` 的 ``ExecutablePath``（必须**全路径等于** ``$ExpectedExe``，:111、:187）
  与 ``GetOwner().User``（必须等于 ``um_share``，:191）核实身份；**不读命令行**（对比
  ``lib/gateway-runtime.ps1:303-310`` 的 ``Test-GatewayProcessOwnership`` 才读
  ``CommandLine``）。
* 主循环判定顺序（:404-515）：``unconfirmed`` → **ANOMALY**（:417-424，撤公网 + 注销
  ``umshare-live`` + ``exit 1``）；``verified`` → 记 seen（:432-441）；limits 丢失 →
  **LIMIT-LOSS**（:444-460，停进程 + 注销任务 + ``exit 1``）；seen 后缺席 → 宽限
  （:462-481，``$MissLimit = 3``）；持续缺席 → **有界恢复**（:485-496 → :367-393，
  ``Start-ScheduledTask umshare-live``）→ 恢复确认则继续轮询（**重启**），否则撤公网 +
  注销任务 + ``exit 1``（:497-507）。

**本工具明确建模的「非输入」**（看门狗根本不读的信号，用于回答「8096 重启会不会触发」）：

* ``processes[*].parent_pid``（``ParentProcessId``）：看门狗源码里**没有**这个字符串
  （``grep -c ParentProcessId um-share-deploy/watchdog-umshare.ps1`` = 0）；读它的是快照工具
  ``tools/um-share/readonly/share_chain_snapshot.py``（CIM 查询的 ``Select-Object … ParentProcessId``
  与解析层的 ``row.get("ParentProcessId")`` 两处）。**该文件当前正被另一条线改写**（执行轮 9 的
  快照扩展线），工作树行号会漂移，故本文件只引「命令＋文件」，不引会漂移的工作树行号。
* 8096／8110 的端口、进程、健康：看门狗源码里没有任何 ``8096``／``8110`` 字面量
  （``grep -nE '8096|8110|8320' um-share-deploy/watchdog-umshare.ps1`` → 无命中；
  该文件只认 ``$LivePort = 8100``，:38；:298 的 ``--https=443 off`` 是撤公网动作的固定参数，
  不是判定输入）；8096 的重启路径见
  ``lib/gateway-runtime.ps1:95-109``（``usage`` 描述符 = 8096）与 ``:524-531``
  （``Port`` 停止 = 只杀该端口的占用者 + 该描述符命令行匹配者）。
* 控制台的 ``backend.pid`` 文件：看门狗不读；读它的是
  ``console/share_production.py:1328-1346``／``:1400-1412``。

**建模口径的两条声明**（哪些是源码事实、哪些是本工具的建模选择）：

* ``anomaly.listener-query-error``：源码 :176 用 ``-ErrorAction SilentlyContinue``，
  非终止错误被抑制后 ``$conn`` 为空 → :177 返回 ``absent``；只有 ``:180`` 的 CIM 查询用
  ``-ErrorAction Stop``。文件头注释（:17-19）与 :197-199 的 catch 表达的是「查询失败 →
  ``unconfirmed``」的意图。本工具按**注释意图**建模（``listener.query_ok=False`` → ``unconfirmed``
  → ANOMALY），**这条是建模选择，不是逐行复现 :176-177 的代码路径**，且**未在运行时诱发过
  ``Get-NetTCPConnection`` 的故障**（本轮只实测了工具模型）。
* ``limits.wfp-strong-not-ok`` 的 token 检查按 PowerShell ``-notlike`` 的**大小写不敏感**
  语义建模（源码 :169）；``limits.wfp-detection-failure`` 覆盖 ``strong`` 输出为空的情形
  （源码 :167 先判 ``$output.Count -eq 0``）。

输出与退出码：

* 判定 ``no-action`` → 0；``stop`` → 3；``restart`` → 4；``undecidable`` → 2。
  「读不到」永远不是 0（不得把不可读当健康）。
* 窗口三态 ``no``／``yes``／``depends`` 取优先级 ``stop > restart > undecidable > no-action``。

夹具 schema（v1；``--from-fixture`` 读的字典；键都可缺，缺即不可判定）：

```
{
  "schema_version": 1,
  "scenario": "<自由文本>",
  "watchdog": {"poll_seconds": 10, "miss_limit": 3, "recovery_limit": 3,
                "live_port": 8100, "expected_exe": "<全路径>",
                "require_owner": "um_share"},
  "snapshots": [{"label": "before", "signals": {...}},
                 {"label": "after", "signals": {...}}]
}
```

单份 ``signals``（也可直接给 ``{"scenario": ..., "signals": {...}}``）：

```
{
  "read_by_caller": {"wfp_strong": true, "umshare_live_task": false, ...},
  "limits": {"backend_port": {"readable": true, "value": 8110, "error": null},
              "wfp_strong": {"query_ok": true, "rc": 0, "text": "… STRONG=OK …"},
              "firewall": {"query_ok": true, "found": true, "enabled": true,
                            "action": "Block", "program": "<全路径>",
                            "direction": "Outbound", "scope_ok": true,
                            "scope_reason": "scope-ok", "error": null}},
  "share": {"listener": {"query_ok": true, "pids": [<8100 监听 PID>],
                          "error": null},
             "identity": {"readable": true, "exe": "<全路径>",
                            "owner": "um_share", "creation_epoch": <float>,
                            "error": null}},
  "seen": {"state": "seen"|"unseen", "misses": <int>},
  "recovery": {"boot_id": "<非空即本 boot 可判>", "attempts": <int>},
  "tasks": {"umshare-live": {"readable": true, "registered": true,
                              "state": "Running"}},
  "processes": {"<pid>": {"name": …, "parent_pid": <int>,
                            "start_epoch": <float>}},   # parent_pid 只供人读，非输入
  "ports": {"8096": {"pids": [<int>]}, "8100": {...}, "8110": {...}},
  "health_8096": {"status": 200, "json": {...}, "error": null},
  "log_tail": ["<看门狗日志尾部行>"]
}
```

运行（工作目录不限；本工具不读网络、不查进程、不写任何文件）：

    python tools/um-share/readonly/watchdog_forensics.py --print-rules
    python tools/um-share/readonly/watchdog_forensics.py --from-fixture <fixture.json>
    python tools/um-share/readonly/watchdog_forensics.py --from-fixture <fixture.json> --json
"""

from __future__ import annotations

import argparse
import json
import sys

SCHEMA_VERSION = 1

VERDICT_NO_ACTION = "no-action"
VERDICT_STOP = "stop"
VERDICT_RESTART = "restart"
VERDICT_UNDECIDABLE = "undecidable"

# Exit codes (documented in the module docstring and --print-rules).
EXIT_BY_VERDICT = {
    VERDICT_NO_ACTION: 0,
    VERDICT_UNDECIDABLE: 2,
    VERDICT_STOP: 3,
    VERDICT_RESTART: 4,
}

# Window-level three-state answer to "does restarting 8096 stop the share chain".
WINDOW_NO = "no"
WINDOW_YES = "yes"
WINDOW_DEPENDS = "depends"

# Watchdog defaults, read from watchdog-umshare.ps1:32-52 and the loop (:330, :402).
DEFAULT_WATCHDOG = {
    "poll_seconds": 10,          # :44 $PollSeconds
    "miss_limit": 3,             # :402 $MissLimit
    "recovery_limit": 3,         # :330 $RecoveryLimit
    "live_port": 8100,           # :38 $LivePort
    "expected_exe": r"C:\ProgramData\UMShareRun\um-share-python.exe",  # :56
    "require_owner": "um_share",  # :37 $RequireOwner
    "recovery_wait_seconds": 40,  # :382
}

WD = "ai-gateway/um-share-deploy/watchdog-umshare.ps1"
DEPLOY = "ai-gateway/um-share-deploy/deploy-elevated.ps1"
RUNTIME = "ai-gateway/lib/gateway-runtime.ps1"
PROD = "ai-gateway/console/share_production.py"
GUARD = "ai-gateway/um-share-deploy/share_guard.py"
SNAP = "tools/um-share/readonly/share_chain_snapshot.py"
# The deployment README is a DIFFERENT file from the deploy script:
# concatenating DEPLOY (…deploy-elevated.ps1) with "/README.md"
# produced an unresolvable path (2026-09-28 review finding 7).
DEPLOY_README = "ai-gateway/um-share-deploy/README.md"

# ---------------------------------------------------------------- 规则表
# id, 条件（人读）, 看门狗的动作, 源码出处。
RULES = (
    {
        "id": "limits.backend-port-unavailable",
        "when": "backend-port.txt 缺失／不可读／值非法（1-65535 之外）",
        "action": VERDICT_STOP,
        "kind": "limit-loss",
        "source": WD + ":66-83, :156-163, :444-460; " + DEPLOY + ":921-923",
        "note": "没有回退端口：文件不可用即检测失败，绝不用私人 8000 兜底",
    },
    {
        "id": "limits.wfp-detection-failure",
        "when": "wfp_helper strong 无输出／rc≠0（源码 :167 先判 `$output.Count -eq 0`）",
        "action": VERDICT_STOP,
        "kind": "limit-loss",
        "source": WD + ":164-169, :444-460",
        "note": "检测失败与限额丢失同等处理（失败关闭）",
    },
    {
        "id": "limits.wfp-strong-not-ok",
        "when": "strong 输出不含 token STRONG=OK（**大小写不敏感**，同 PowerShell `-notlike`；filter action/layer/appid 任一不符）",
        "action": VERDICT_STOP,
        "kind": "limit-loss",
        "source": WD + ":169, :444-460; " + DEPLOY + ":284-293",
    },
    {
        "id": "limits.firewall-query-error",
        "when": "防火墙规则查询抛错（含 WMI/COM 瞬时失败，如「调用已取消」）",
        "action": VERDICT_STOP,
        "kind": "limit-loss",
        "source": WD + ":149-151, :410, :444-460",
        "note": "2026-09-24 14:20 现场即此路径（firewall-error 调用已取消）",
    },
    {
        "id": "limits.firewall-rule-missing",
        "when": "um-share-outbound-block 规则不存在／未启用／Action≠Block",
        "action": VERDICT_STOP,
        "kind": "limit-loss",
        "source": WD + ":141-143, :444-460",
    },
    {
        "id": "limits.firewall-program-or-scope-narrowed",
        "when": "规则 Direction≠Outbound、Program≠当前映像，或协议/端口/远端地址被收窄",
        "action": VERDICT_STOP,
        "kind": "limit-loss",
        "source": WD + ":119-136（Direction 检查在 :125）, :144-147, :444-460",
    },
    {
        "id": "anomaly.listener-query-error",
        "when": "8100 监听查询本身失败（netstat 读不出；建模按文件头注释 :17-19 与 catch :197-199 的意图）",
        "action": VERDICT_STOP,
        "kind": "anomaly",
        "source": WD + ":17-19（注释）, :197-199（catch）, :417-424；"
                  "**非 :176-177 的代码路径**（:176 用 -ErrorAction SilentlyContinue，"
                  "非终止错误被抑制后 :177 返回 absent）",
        "note": "先撤公网映射，再注销 umshare-live，不杀分享进程；"
                "这条是建模选择，未在运行时诱发过 Get-NetTCPConnection 故障",
    },
    {
        "id": "anomaly.procquery-error",
        "when": "8100 监听者进程查询失败（WMI/COM，如「内存不足」）",
        "action": VERDICT_STOP,
        "kind": "anomaly",
        "source": WD + ":184-186, :417-424",
        "note": "2026-09-24 15:52 现场即此路径",
    },
    {
        "id": "anomaly.identity-mismatch",
        "when": "8100 监听者映像路径≠$ExpectedExe，或 owner≠um_share（含读不到而取空）",
        "action": VERDICT_STOP,
        "kind": "anomaly",
        "source": WD + ":111, :187-195, :417-424",
    },
    {
        "id": "anomaly.close-public-and-cleanup",
        "when": "ANOMALY 命中后的固定动作序列",
        "action": VERDICT_STOP,
        "kind": "anomaly",
        "source": WD + ":418-423, :283-315, :317-320",
        "note": "Close-Public（funnel off + 复核）→ Cleanup-Task（注销 umshare-live）→ exit 1",
    },
    {
        "id": "share-absent.grace",
        "when": "曾 seen 之后连续缺席，次数 < $MissLimit(3)",
        "action": VERDICT_NO_ACTION,
        "kind": "grace",
        "source": WD + ":462-481",
        "note": "有界延迟：probe/real 重启不得判成死亡（:402 注释）",
    },
    {
        "id": "managed-stop.in-force",
        "when": "broker 受保护记录 lifecycle.state=stopped 且 at > 看门狗末次 seen 的创建时间",
        "action": VERDICT_NO_ACTION,
        "kind": "expected-stop",
        "source": WD + ":230-253, :474-478",
        "note": "有意的管理停止不是分享死亡：任务保留、缺席清零",
    },
    {
        "id": "recovery.budget-exhausted",
        "when": "缺席达 3 次且（boot id 读不到 或 本 boot 已用满 3 次）",
        "action": VERDICT_STOP,
        "kind": "recovery-failed",
        "source": WD + ":330, :339-356, :485-501, :502-507",
        "note": "boot id 读不到也走此路（失败关闭），不当作「可恢复」",
    },
    {
        "id": "recovery.task-unreadable",
        "when": "缺席达 3 次、预算未满，但 umshare-live 任务状态读不到（无法判断能否启动）",
        "action": VERDICT_UNDECIDABLE,
        "kind": "read",
        "source": WD + ":367-380（`Start-ScheduledTask` 本身不读任务状态）, :497-507；"
                  "本工具建模选择：读不到即不判 stop",
        "note": "源码在 :377 直接 try Start-ScheduledTask，不先查任务状态；"
                "本工具选择「读不到 → 不可判定」，不冒充 stop",
    },
    {
        "id": "recovery.task-not-registered",
        "when": "缺席达 3 次、预算未满，且 umshare-live 任务确认未注册（Start-ScheduledTask 会失败）",
        "action": VERDICT_STOP,
        "kind": "recovery-failed",
        "source": WD + ":367-380, :497-507",
        "note": "RECOVERY-START-FAILED → 撤公网 + 注销任务 + exit 1",
    },
    {
        "id": "recovery.unconfirmed",
        "when": "缺席达 3 次、任务已启动，但 40 秒内未见已核实的监听者",
        "action": VERDICT_STOP,
        "kind": "recovery-failed",
        "source": WD + ":382-393, :497-507",
    },
    {
        "id": "recovery.confirmed",
        "when": "缺席达 3 次、任务启动成功且 40 秒内重新核实到监听者",
        "action": VERDICT_RESTART,
        "kind": "recovery",
        "source": WD + ":377, :382-390, :491-496",
        "note": "这是「重启分享链」的唯一路径，重启的是同一个已注册任务",
    },
    {
        "id": "limit-loss.stop-reverify",
        "when": "LIMIT-LOSS 停进程前的身份复核失败（映像／owner／创建时间不符）",
        "action": VERDICT_STOP,
        "kind": "limit-loss",
        "source": WD + ":255-281, :449-455",
        "note": "停止未被确认 → 以撤公网映射兜底",
    },
    {
        "id": "limit-loss.stop-confirmed",
        "when": "LIMIT-LOSS 且映像＋owner＋创建时间三项复核通过 → 停止确认",
        "action": VERDICT_STOP,
        "kind": "limit-loss",
        "source": WD + ":260-277, :444-460",
        "note": "回收的 PID 属于别的进程时绝不杀（:266-270）",
    },
    {
        "id": "limit-loss.share-absent-nothing-to-kill",
        "when": "LIMIT-LOSS 且查询成功确认 8100 无人监听",
        "action": VERDICT_STOP,
        "kind": "limit-loss",
        "source": WD + ":446-447, :457-459",
        "note": "没有可杀对象也要注销 umshare-live 并退出（分享链同样被停）",
    },
    {
        "id": "share.verified-seen-record",
        "when": "8100 监听者身份已核实（映像全路径＋owner）",
        "action": VERDICT_NO_ACTION,
        "kind": "ok",
        "source": WD + ":426-441, :462-467",
        "note": "每轮写 seen 记录（pid + creation_epoch），只服务 managed-stop 判定",
    },
    {
        "id": "startup.grace",
        "when": "从未 seen 且当前无人监听",
        "action": VERDICT_NO_ACTION,
        "kind": "startup",
        "source": WD + ":513-514",
    },
    {
        "id": "identity.parent-pid-not-read",
        "when": "快照里 8110 的父 PID 指向已死进程（孤儿）",
        "action": VERDICT_NO_ACTION,
        "kind": "non-input",
        "source": WD + ":174-200（只看 8100 监听者 + 映像 + owner；无 ParentProcessId）; "
                  + SNAP + "（`grep -n ParentProcessId` → CIM Select-Object 与 row.get 两处；"
                  "该文件正被另一条线改写，工作树行号会漂移，故不引）",
        "note": "看门狗没有「父进程消失即孤儿」判定：这是本仓最容易被误信的假机制",
    },
    {
        "id": "identity.pid-change-not-a-trigger",
        "when": "同一端口上的监听者 PID 变了（映像与 owner 仍匹配）",
        "action": VERDICT_NO_ACTION,
        "kind": "non-input",
        "source": WD + ":174-200, :426-442",
        "note": "看门狗不做跨轮 PID 比较；seen 记录只服务于 managed-stop 判定（:237-253）",
    },
    {
        "id": "window.8096-restart-not-an-input",
        "when": "8096 重启（8096 PID 变化、8096 健康 200、8096 读面键集变化）",
        "action": VERDICT_NO_ACTION,
        "kind": "non-input",
        "source": RUNTIME + ":95-109, :522-531; " + WD + ":32-52",
        "note": "看门狗源码无 8096／8110 字面量；8096 重启只动 8096 端口占用者与 usagesrv.py",
    },
    {
        "id": "window.transient-query-failure",
        "when": "窗口内看门狗自身的 WMI/COM 查询瞬时失败（历史两次：firewall 调用已取消、procquery 内存不足）",
        "action": VERDICT_STOP,
        "kind": "transient",
        "source": WD + ":138-152, :174-200, :417-424, :444-460; "
                  + DEPLOY_README + ":53-57",
        "note": "这是维护者所指历史事件的机制形态；8096 重启本身不是输入，但它与查询压力同窗出现",
    },
    {
        "id": "window.share-chain-touched",
        "when": "窗口步骤另动了分享链（8100/8110 重启、撤 limits、注销任务、重装 watchdog）",
        "action": VERDICT_UNDECIDABLE,
        "kind": "window",
        "source": DEPLOY + ":774-785, :962-971; " + WD + ":444-460, :485-507",
        "note": "与 8096 重启打包执行即落此态；维护者要求各自独立执行",
    },
    {
        "id": "read.decisive-signal-unreadable",
        "when": "判定所依赖的信号在本身份下读不到（如 RunRoot、任务状态、WFP 强校验）",
        "action": VERDICT_UNDECIDABLE,
        "kind": "read",
        "source": "output/ai-gw-round6-20260927/metering-action-gate.md:128-130（§1.6 不可判定项："
                  "不得把非提升下的不可读当「未注册」）; "
                  "output/ai-gw-round9-20260927/post-restart-acceptance.md:18/:34/:95"
                  "（读不到时记 UNDECIDABLE）",
        "note": "读不到永远不折成「健康」或「未注册」",
    },
)

_RULES_BY_ID = {rule["id"]: rule for rule in RULES}


def _rule(rule_id, detail):
    rule = _RULES_BY_ID[rule_id]
    return {"id": rule_id, "action": rule["action"], "kind": rule["kind"],
            "source": rule["source"], "detail": detail}


def _hit(rule_id, detail):
    return _rule(rule_id, detail)


def _int_or_none(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


# ------------------------------------------------------------- limits 半场
def _limits_half(signals, cfg):
    """``Get-LimitsState``（:154-171）+ ``Get-FirewallState``（:138-152）。"""
    limits = signals.get("limits")
    if not isinstance(limits, dict):
        return None, "limits-signal-missing", []
    hits = []
    port = limits.get("backend_port")
    if not isinstance(port, dict):
        return None, "backend-port-signal-missing", hits
    value = port.get("value")
    ok = _int_or_none(value)
    if port.get("readable") is not True or ok is None or not 1 <= ok <= 65535:
        reason = port.get("error") or ("bad value: %r" % (value,))
        hits.append(_hit("limits.backend-port-unavailable",
                        "backend-port-unavailable(%s)" % reason))
        return False, "detection-failure(" + str(reason) + ")", hits
    wfp = limits.get("wfp_strong")
    if not isinstance(wfp, dict):
        return None, "wfp-signal-missing", hits
    text = wfp.get("text")
    # (:167) `$output.Count -eq 0` is checked BEFORE the rc / token checks,
    # so an empty reading is a detection failure with its own reason string.
    if not isinstance(text, str) or not text.strip():
        hits.append(_hit("limits.wfp-detection-failure",
                        "detection-failure(no-output, rc=%r)"
                        % (wfp.get("rc"),)))
        return False, "detection-failure(no-output, rc=%r)" % (wfp.get("rc"),), hits
    if wfp.get("query_ok") is not True:
        hits.append(_hit("limits.wfp-detection-failure",
                        "detection-failure(no-output/query-failed, rc=%r)"
                        % (wfp.get("rc"),)))
        return False, "detection-failure(wfp query failed)", hits
    if _int_or_none(wfp.get("rc")) not in (0,):
        hits.append(_hit("limits.wfp-detection-failure",
                        "detection-failure(rc=%r)" % (wfp.get("rc"),)))
        return False, "detection-failure(rc=%r)" % (wfp.get("rc"),), hits
    # (:169) PowerShell `-notlike '*STRONG=OK*'` is CASE-INSENSITIVE; the
    # helper's real output carries the token as `…; STRONG=OK` (deploy log
    # 2026-09-25 12:15:50), so the comparison must be case-insensitive too.
    if "STRONG=OK" not in text.upper():
        hits.append(_hit("limits.wfp-strong-not-ok",
                        "strong-not-ok: %r" % (text,)))
        return False, "strong-not-ok: %r" % (text,), hits
    # The firewall half is queried ONLY when the WFP half passed (:410).
    fw = limits.get("firewall")
    if not isinstance(fw, dict):
        return None, "firewall-signal-missing", hits
    if fw.get("query_ok") is not True:
        reason = fw.get("error") or "query-error"
        hits.append(_hit("limits.firewall-query-error",
                        "firewall:" + str(reason)))
        return False, "firewall:" + str(reason), hits
    if fw.get("found") is not True:
        hits.append(_hit("limits.firewall-rule-missing", "rule-missing"))
        return False, "firewall:rule-missing", hits
    if fw.get("enabled") is not True:
        hits.append(_hit("limits.firewall-rule-missing", "rule-disabled"))
        return False, "firewall:rule-disabled", hits
    # PowerShell `-ne` 大小写不敏感：`block`/`BLOCK` 与 `Block` 等价；
    # 缺失或非字符串动作仍是失败（`$null -ne 'Block'` 为真）。
    action = fw.get("action")
    if not (isinstance(action, str) and action.lower() == "block"):
        hits.append(_hit("limits.firewall-rule-missing", "action=%r" % (action,)))
        return False, "firewall:action=%r" % (action,), hits
    # (:125) Direction is part of Test-FirewallScopeOk: a rule narrowed to
    # another direction fails the limits check exactly like a narrowed scope.
    # PowerShell `-ne` 大小写不敏感：`outbound`/`OUTBOUND` 与 `Outbound` 等价
    # （复核者 2026-09-28 复现：原区分大小写的实现会把小写值误报成「会」）；
    # 缺失或非字符串方向仍是失败（`$null -ne 'Outbound'` 为真）。
    direction = fw.get("direction")
    if not (isinstance(direction, str) and direction.lower() == "outbound"):
        hits.append(_hit("limits.firewall-program-or-scope-narrowed",
                        "direction=%r" % (direction,)))
        return False, "firewall:direction=%r" % (direction,), hits
    if fw.get("program") != cfg["expected_exe"]:
        hits.append(_hit("limits.firewall-program-or-scope-narrowed",
                        "program=%r" % (fw.get("program"),)))
        return False, "firewall:program=%r" % (fw.get("program"),), hits
    if fw.get("scope_ok") is not True:
        hits.append(_hit("limits.firewall-program-or-scope-narrowed",
                        "scope: %r" % (fw.get("scope_reason"),)))
        return False, "firewall:scope(%r)" % (fw.get("scope_reason"),), hits
    return True, "ok", hits


# ------------------------------------------------------------- share 半场
def _share_state(signals, cfg):
    """``Get-ShareState``（:174-200）→ ``(state, pid, creation, hits, why)``。

    ``state`` ∈ ``verified`` / ``absent`` / ``unconfirmed``；``absent`` 只在**查询成功
    且无人监听**时给出（:177）；查询失败或身份核实不了都是 ``unconfirmed``。
    """
    share = signals.get("share")
    if not isinstance(share, dict):
        return None, None, None, [], "share-signal-missing"
    listener = share.get("listener")
    if not isinstance(listener, dict):
        return None, None, None, [], "listener-signal-missing"
    if listener.get("query_ok") is not True:
        hits = [_hit("anomaly.listener-query-error",
                     "listener-query-error: %r" % (listener.get("error"),))]
        return "unconfirmed", None, None, hits, "listener-query-error"
    pids = listener.get("pids")
    if not isinstance(pids, list):
        return None, None, None, [], "listener-pids-missing"
    if not pids:
        return "absent", None, None, [], "no-listener"
    pid = _int_or_none(pids[0])
    if pid is None:
        return None, None, None, [], "listener-pid-unusable"
    identity = share.get("identity")
    if not isinstance(identity, dict):
        return None, None, None, [], "identity-signal-missing"
    if identity.get("readable") is not True:
        hits = [_hit("anomaly.procquery-error",
                     "procquery: %r" % (identity.get("error"),))]
        return "unconfirmed", pid, None, hits, "procquery-error"
    exe = identity.get("exe")
    owner = identity.get("owner")
    if exe != cfg["expected_exe"]:
        hits = [_hit("anomaly.identity-mismatch",
                     "identity-mismatch exe=%r owner=%r" % (exe, owner))]
        return "unconfirmed", pid, None, hits, "image-mismatch"
    if owner != cfg["require_owner"]:
        hits = [_hit("anomaly.identity-mismatch",
                     "identity-mismatch exe=%r owner=%r" % (exe, owner))]
        return "unconfirmed", pid, None, hits, "owner-mismatch"
    return "verified", pid, identity.get("creation_epoch"), [], "ok"


def _stop_verdict(share_state, share_pid, share_creation, signals, cfg, hits):
    """``Stop-ShareProcess``（:255-281）复核 + LIMIT-LOSS 序列（:444-460）。"""
    if share_state == "absent":
        hits.append(_hit("limit-loss.share-absent-nothing-to-kill",
                         "share confirmed absent on the port; nothing to kill"))
        return
    identity = (signals.get("share") or {}).get("identity") or {}
    exe = identity.get("exe")
    owner = identity.get("owner")
    creation = identity.get("creation_epoch")
    if exe != cfg["expected_exe"]:
        hits.append(_hit("limit-loss.stop-reverify",
                         "stop-unconfirmed image-mismatch %r -> public-close fallback"
                         % (exe,)))
        return
    if owner != cfg["require_owner"]:
        hits.append(_hit("limit-loss.stop-reverify",
                         "stop-unconfirmed owner-mismatch %r -> public-close fallback"
                         % (owner,)))
        return
    if not isinstance(creation, (int, float)) or not isinstance(
            share_creation, (int, float)) or abs(creation - share_creation) > 2:
        hits.append(_hit("limit-loss.stop-reverify",
                         "stop-unconfirmed creation-mismatch -> public-close fallback"))
        return
    hits.append(_hit("limit-loss.stop-confirmed", "stopped pid=%s" % (share_pid,)))


# ------------------------------------------------------------ 单份快照判定
def judge_snapshot(signals, watchdog=None):
    """纯函数：一份信号快照 → 看门狗会怎么判（字典进、字典出）。"""
    cfg = dict(DEFAULT_WATCHDOG)
    if isinstance(watchdog, dict):
        for key, value in watchdog.items():
            if key in cfg:
                cfg[key] = value
    if not isinstance(signals, dict):
        return {"verdict": VERDICT_UNDECIDABLE, "kind": "read",
                "rules": [_hit("read.decisive-signal-unreadable",
                               "snapshot is not a dict")],
                "reason": "snapshot is not a dict"}

    limits_ok, limit_reason, hits = _limits_half(signals, cfg)
    state, pid, creation, share_hits, share_why = _share_state(signals, cfg)
    hits.extend(share_hits)

    # Missing / unreadable signal groups: the verdict must stay undecidable,
    # never "no-action" (a blank snapshot is not a healthy share).
    if limits_ok is None:
        hits.append(_hit("read.decisive-signal-unreadable",
                         "limits half unreadable: %s" % limit_reason))
        return {"verdict": VERDICT_UNDECIDABLE, "kind": "read", "rules": hits,
                "reason": limit_reason, "share_state": state}
    if state is None:
        hits.append(_hit("read.decisive-signal-unreadable",
                         "share half unreadable: %s" % share_why))
        return {"verdict": VERDICT_UNDECIDABLE, "kind": "read", "rules": hits,
                "reason": share_why, "limits_ok": limits_ok}

    # (:417-424) ANOMALY is evaluated BEFORE the limit-loss branch (:444).
    if state == "unconfirmed":
        hits.append(_hit("anomaly.close-public-and-cleanup", share_why))
        return {"verdict": VERDICT_STOP, "kind": "anomaly", "rules": hits,
                "reason": "ANOMALY: " + share_why,
                "share_state": state, "limits_ok": limits_ok}

    if not limits_ok:
        # (:426-441) the seen record is written BEFORE the limit-loss branch
        # (:444) in the same round, so it is reported here too.
        if state == "verified":
            hits.append(_hit("share.verified-seen-record",
                             "seen record written pid=%s" % (pid,)))
        _stop_verdict(share_state=state, share_pid=pid, share_creation=creation,
                      signals=signals, cfg=cfg, hits=hits)
        return {"verdict": VERDICT_STOP, "kind": "limit-loss", "rules": hits,
                "reason": "LIMIT-LOSS: " + limit_reason,
                "share_state": state, "limits_ok": limits_ok}

    seen = signals.get("seen")
    seen_state = (seen or {}).get("state") if isinstance(seen, dict) else None
    if state == "verified":
        hits.append(_hit("share.verified-seen-record",
                         "seen record written pid=%s" % (pid,)))
        return {"verdict": VERDICT_NO_ACTION, "kind": "ok", "rules": hits,
                "reason": "check-ok limits=ok share=%s" % (pid,),
                "share_state": state, "limits_ok": limits_ok, "share_pid": pid}

    if seen_state != "seen":
        hits.append(_hit("startup.grace", "share=none seen=false"))
        return {"verdict": VERDICT_NO_ACTION, "kind": "startup", "rules": hits,
                "reason": "startup grace (never seen this share)",
                "share_state": state, "limits_ok": limits_ok}

    # (:474) managed-stop is checked BEFORE the miss counter increments (:479).
    misses = _int_or_none((seen or {}).get("misses")) or 0
    if _managed_stop_in_force(signals):
        hits.append(_hit("managed-stop.in-force",
                         "share absent and a confirmed management stop is in force"))
        return {"verdict": VERDICT_NO_ACTION, "kind": "expected-stop",
                "rules": hits, "reason": "expected-stop -> task kept",
                "share_state": state, "limits_ok": limits_ok, "misses": 0}
    misses += 1
    if misses < cfg["miss_limit"]:
        hits.append(_hit("share-absent.grace",
                         "share-absent poll %d/%d" % (misses, cfg["miss_limit"])))
        return {"verdict": VERDICT_NO_ACTION, "kind": "grace", "rules": hits,
                "reason": "sustained-absence grace open",
                "share_state": state, "limits_ok": limits_ok, "misses": misses}

    # Sustained absence -> bounded recovery (:485-496).
    recovery = signals.get("recovery")
    boot = (recovery or {}).get("boot_id") if isinstance(recovery, dict) else None
    attempts = (recovery or {}).get("attempts") if isinstance(recovery, dict) else None
    attempts = _int_or_none(attempts) or 0
    if not boot or attempts >= cfg["recovery_limit"]:
        hits.append(_hit("recovery.budget-exhausted",
                         "attempts=%d/%d boot=%r" % (attempts,
                                                      cfg["recovery_limit"], boot)))
        return {"verdict": VERDICT_STOP, "kind": "recovery-failed",
                "rules": hits, "reason": "RECOVERY-BUDGET-EXHAUSTED -> close public",
                "share_state": state, "limits_ok": limits_ok, "misses": misses}

    task = (signals.get("tasks") or {}).get("umshare-live")
    if not isinstance(task, dict) or task.get("readable") is not True:
        # (:377) the source does NOT read the task state first — it calls
        # Start-ScheduledTask inside a try and only then names the failure.
        # Reading the task state is THIS tool's modelling choice, so an
        # unreadable state must not be reported as a "stop" rule.
        hits.append(_hit("recovery.task-unreadable",
                         "umshare-live task state unreadable -> recovery outcome cannot be judged"))
        return {"verdict": VERDICT_UNDECIDABLE, "kind": "read", "rules": hits,
                "reason": "recovery needs the umshare-live task state; unreadable",
                "share_state": state, "limits_ok": limits_ok, "misses": misses}
    if task.get("registered") is not True:
        hits.append(_hit("recovery.task-not-registered",
                         "umshare-live not registered -> RECOVERY-START-FAILED"))
        return {"verdict": VERDICT_STOP, "kind": "recovery-failed",
                "rules": hits, "reason": "RECOVERY-START-FAILED -> close public",
                "share_state": state, "limits_ok": limits_ok, "misses": misses}
    confirmed = signals.get("recovery_result")
    if not isinstance(confirmed, dict) or confirmed.get("listener_verified") is not True:
        hits.append(_hit("recovery.unconfirmed",
                         "no verified listener within %ss"
                         % cfg["recovery_wait_seconds"]))
        return {"verdict": VERDICT_STOP, "kind": "recovery-failed", "rules": hits,
                "reason": "RECOVERY-UNCONFIRMED -> close public",
                "share_state": state, "limits_ok": limits_ok, "misses": misses}
    hits.append(_hit("recovery.confirmed",
                     "RECOVERY-OK pid=%s" % (confirmed.get("pid"),)))
    return {"verdict": VERDICT_RESTART, "kind": "recovery", "rules": hits,
            "reason": "RECOVERY-OK -> restart of the SAME registered task",
            "share_state": state, "limits_ok": limits_ok, "misses": 0,
            "recovery_pid": confirmed.get("pid")}


def _managed_stop_in_force(signals):
    """``Test-ManagedStopInForce``（:237-253）的纯函数等价物。"""
    broker = signals.get("broker_status")
    if not isinstance(broker, dict) or broker.get("readable") is not True:
        return False
    if broker.get("lifecycle_state") != "stopped":
        return False
    at = broker.get("at")
    seen_epoch = broker.get("seen_creation_epoch")
    if not isinstance(at, (int, float)) or at <= 0:
        return False
    if not isinstance(seen_epoch, (int, float)):
        return False
    return at > seen_epoch


# ------------------------------------------------------------- 窗口级判定
def judge_window(before, after, watchdog=None):
    """纯函数：8096 重启窗口的前后两份快照 → 三态结论（会／不会／取决于 X）。"""
    cfg = dict(DEFAULT_WATCHDOG)
    if isinstance(watchdog, dict):
        for key, value in watchdog.items():
            if key in cfg:
                cfg[key] = value
    before_result = judge_snapshot(before, cfg)
    after_result = judge_snapshot(after, cfg)

    conditions = []
    rules = []

    # (1) any proven trigger in the window -> yes.
    for label, result in (("before", before_result), ("after", after_result)):
        if result["verdict"] in (VERDICT_STOP, VERDICT_RESTART):
            conditions.append("%s snapshot triggers %s (%s)"
                              % (label, result["verdict"], result["reason"]))
            rules.extend(result["rules"])

    # (2) share-chain touches declared by the window (or read from the pair).
    touches = _share_chain_touches(before, after, cfg)
    if touches:
        rules.append(_hit("window.share-chain-touched", "；".join(touches)))
        conditions.extend(touches)

    # (3) unreadable decisive signals -> depends, never "no".
    unreadable = _unreadable_signals(before) + _unreadable_signals(after)
    if unreadable:
        rules.append(_hit("read.decisive-signal-unreadable",
                         "；".join(unreadable)))
        conditions.extend(unreadable)

    # (4) the explicit non-inputs, always reported.
    rules.append(_hit("identity.parent-pid-not-read", _parent_pid_note(before, after)))
    rules.append(_hit("identity.pid-change-not-a-trigger",
                      _pid_change_note(before, after)))
    rules.append(_hit("window.8096-restart-not-an-input",
                      _port_8096_note(before, after)))

    verdicts = (before_result["verdict"], after_result["verdict"])
    if VERDICT_STOP in verdicts:
        state = WINDOW_YES
        code = EXIT_BY_VERDICT[VERDICT_STOP]
    elif VERDICT_RESTART in verdicts:
        state = WINDOW_YES
        code = EXIT_BY_VERDICT[VERDICT_RESTART]
    elif unreadable or touches or VERDICT_UNDECIDABLE in verdicts:
        state = WINDOW_DEPENDS
        code = EXIT_BY_VERDICT[VERDICT_UNDECIDABLE]
    else:
        state = WINDOW_NO
        code = EXIT_BY_VERDICT[VERDICT_NO_ACTION]

    return {
        "schema_version": SCHEMA_VERSION,
        "state": state,
        "exit_code": code,
        "conditions": conditions,
        "rules": rules,
        "before": before_result,
        "after": after_result,
        "non_inputs": [
            {"signal": "processes[*].parent_pid",
             "read_by": "tools/um-share/readonly/share_chain_snapshot.py（CIM Select-Object 与 row.get 两处；"
                        "该文件正被另一条线改写，不引工作树行号）",
             "read_by_watchdog": False,
             "source": WD + ":174-200"},
            {"signal": "ports['8096'] / ports['8110']",
             "read_by": "tools/um-share/readonly/share_chain_snapshot.py（CHAIN_PORTS 与监听读取两处）",
             "read_by_watchdog": False,
             "source": WD + ":38（$LivePort = 8100）, :176"},
            {"signal": "console backend.pid",
             "read_by": PROD + ":1328-1412",
             "read_by_watchdog": False,
             "source": WD + ":174-200"},
            {"signal": "8096 /health",
             "read_by": RUNTIME + ":95-109, :253-289",
             "read_by_watchdog": False,
             "source": WD + ":32-52"},
        ],
    }


def _share_chain_touches(before, after, cfg):
    """窗口是否另动了分享链（→ 取决于 X）。"""
    out = []
    for port in (8100, 8110):
        b = _port_pids(before, port)
        a = _port_pids(after, port)
        if b and a and set(b) != set(a):
            out.append("%d 监听 PID 变化：%s → %s" % (port, b, a))
    for label, signals in (("before", before), ("after", after)):
        signals = signals if isinstance(signals, dict) else {}
        limits = signals.get("limits") or {}
        port = limits.get("backend_port")
        if isinstance(port, dict) and (
                port.get("readable") is False
                or (port.get("readable") is True
                    and _int_or_none(port.get("value")) is None)):
            out.append("%s 快照 limits 读不到 → 撤 limits 的可能性无法排除" % label)
        tasks = signals.get("tasks") or {}
        if any((t or {}).get("registered") is False
               for t in tasks.values() if isinstance(t, dict)):
            out.append("%s 快照显示 umshare-* 任务被注销" % label)
        if signals.get("watchdog_rearmed") is True:
            out.append("%s 快照显示 watchdog 被重新武装（re-arm）" % label)
    return out


def _port_pids(signals, port, key=None):
    """读 ``ports``／``listeners`` 两个键名（快照与夹具两种形状）。"""
    signals = signals if isinstance(signals, dict) else {}
    for name in ([key] if key else []) + ["ports", "listeners"]:
        entry = signals.get(name)
        if not isinstance(entry, dict):
            continue
        row = entry.get(str(port))
        if not isinstance(row, dict):
            continue
        pids = row.get("pids")
        if not isinstance(pids, list):
            continue
        return [pid for pid in pids
                if isinstance(pid, int) and not isinstance(pid, bool)]
    return []


def _unreadable_signals(signals):
    """本身份读不到的判定输入（读不到 → 不可判定，绝不折成健康）。"""
    out = []
    if not isinstance(signals, dict):
        return ["snapshot 不是字典"]
    read = signals.get("read_by_caller") or {}
    for name in ("wfp_strong", "backend_port", "firewall", "share_identity",
                  "umshare_live_task", "umshare_watchdog_task",
                  "watchdog_decision_log", "broker_status"):
        if read.get(name) is False:
            out.append("%s 在本身份下读不到" % name)
    limits = signals.get("limits") or {}
    for name in ("wfp_strong", "backend_port", "firewall"):
        row = limits.get(name)
        if isinstance(row, dict) and row.get("readable_by_caller") is False:
            label = "limits." + name
            if label not in out:
                out.append(label + " 在本身份下读不到")
    share = signals.get("share") or {}
    identity = share.get("identity")
    if isinstance(identity, dict) and identity.get("readable") is False:
        out.append("share.identity 在本身份下读不到")
    return out


def _process_row(processes, pid):
    """进程行查表：夹具用 int 键，JSON 往返后用 str 键，两种都认。"""
    if not isinstance(processes, dict):
        return None
    for key in (pid, str(pid)):
        row = processes.get(key)
        if isinstance(row, dict):
            return row
    return None


def _parent_pid_note(before, after):
    """8110 的父 PID 是否指向已死进程——看门狗**不读**这条。"""
    after = after if isinstance(after, dict) else {}
    processes = after.get("processes")
    if not isinstance(processes, dict):
        return "快照未含进程列表：无法说明 8110 的父 PID 归属（看门狗本就不读该信号）"
    pids_8110 = _port_pids(after, 8110, "ports")
    pids_8096 = _port_pids(after, 8096, "ports")
    if not pids_8110:
        return "快照未含 8110 监听 PID：父 PID 归属无法给出（看门狗本就不读该信号）"
    row = _process_row(processes, pids_8110[0])
    if row is None:
        return "快照未含 8110 进程行：父 PID 归属无法给出"
    parent = row.get("parent_pid")
    if parent is None:
        return "快照未给出 8110 的父 PID（读不到 ≠ 无父）"
    if _process_row(processes, parent) is None:
        return ("8110(pid=%s) 父 PID=%s 已不在进程表中（孤儿）；看门狗不读 ParentProcessId，"
                "不因此判定异常" % (pids_8110[0], parent))
    if pids_8096 and parent in pids_8096:
        return ("8110(pid=%s) 父 PID=%s = 8096 监听 PID（父子关系成立）；看门狗不读该信号"
                % (pids_8110[0], parent))
    return ("8110(pid=%s) 父 PID=%s 仍在进程表中；看门狗不读该信号"
            % (pids_8110[0], parent))


def _pid_change_note(before, after):
    parts = []
    for port in (8096, 8100, 8110):
        b = _port_pids(before, port, "ports")
        a = _port_pids(after, port, "ports")
        if b and a and set(b) != set(a):
            parts.append("%d PID %s → %s（看门狗不读 %d 的 PID；仅 8100 的监听者身份参与判定）"
                          % (port, b, a, port))
    if not parts:
        return "窗口内 8096/8100/8110 的 PID 未见变化（或快照缺读数）"
    return "；".join(parts)


def _port_8096_note(before, after):
    b = _port_pids(before, 8096, "ports")
    a = _port_pids(after, 8096, "ports")
    health = (after or {}).get("health_8096")
    return ("8096 PID %s → %s；8096 健康读数 %r；看门狗源码无 8096 字面量，"
            "该信号不进入其判定（%s:38 只认 8100）" % (b, a, health, WD))


# ------------------------------------------------------------------- CLI
def judge_fixture(fixture):
    """纯函数：夹具字典 → 完整报告（含窗口三态结论）。"""
    if not isinstance(fixture, dict):
        return {"schema_version": SCHEMA_VERSION, "error": "fixture-not-an-object"}
    watchdog = fixture.get("watchdog")
    out = {"schema_version": SCHEMA_VERSION,
           "scenario": fixture.get("scenario"),
           "watchdog": dict(DEFAULT_WATCHDOG)}
    if isinstance(watchdog, dict):
        for key, value in watchdog.items():
            if key in out["watchdog"]:
                out["watchdog"][key] = value
    snapshots = fixture.get("snapshots")
    if isinstance(snapshots, list) and snapshots:
        judged = []
        for index, entry in enumerate(snapshots):
            entry = entry if isinstance(entry, dict) else {}
            signals = entry.get("signals") if isinstance(entry, dict) else None
            judged.append({"label": entry.get("label") or ("snapshot-%d" % index),
                           "result": judge_snapshot(signals, watchdog)})
        out["snapshots"] = judged
        if len(judged) >= 2:
            out["window"] = judge_window(snapshots[0].get("signals"),
                                        snapshots[-1].get("signals"), watchdog)
        else:
            out["window"] = judge_window(snapshots[0].get("signals"),
                                        snapshots[0].get("signals"), watchdog)
        out["verdict"] = out["window"]["state"]
        out["exit_code"] = out["window"]["exit_code"]
        return out
    signals = fixture.get("signals")
    if isinstance(signals, dict):
        result = judge_snapshot(signals, watchdog)
        out["snapshots"] = [{"label": fixture.get("label") or "single",
                             "result": result}]
        out["window"] = None
        out["verdict"] = result["verdict"]
        out["exit_code"] = EXIT_BY_VERDICT[result["verdict"]]
        return out
    out["error"] = "fixture has neither 'signals' nor 'snapshots'"
    out["verdict"] = VERDICT_UNDECIDABLE
    out["exit_code"] = EXIT_BY_VERDICT[VERDICT_UNDECIDABLE]
    return out


def format_rules():
    lines = ["看门狗判定规则表（来源：%s）" % WD, "=" * 72]
    for rule in RULES:
        lines.append("[%s] %s" % (rule["action"], rule["id"]))
        lines.append("    条件：%s" % rule["when"])
        lines.append("    来源：%s" % rule["source"])
        if rule.get("note"):
            lines.append("    注：%s" % rule["note"])
    lines.append("=" * 72)
    lines.append("退出码：no-action=0、undecidable=2、stop=3、restart=4")
    return "\n".join(lines)


def format_report(report):
    lines = []
    scenario = report.get("scenario")
    if scenario:
        lines.append("场景：%s" % scenario)
    if report.get("error"):
        lines.append("夹具错误：%s" % report["error"])
    for entry in report.get("snapshots") or []:
        result = entry["result"]
        lines.append("[%s] 判定=%s kind=%s —— %s"
                      % (entry["label"], result["verdict"], result["kind"],
                         result.get("reason")))
        for rule in result.get("rules") or []:
            lines.append("    命中 %s（%s）—— %s"
                          % (rule["id"], rule["action"], rule["detail"]))
    window = report.get("window")
    if window:
        lines.append("-" * 72)
        lines.append("窗口三态结论：%s（会／不会／取决于 X）"
                      % {"no": "不会", "yes": "会",
                         "depends": "取决于 X"}.get(window["state"],
                                                  window["state"]))
        for condition in window["conditions"]:
            lines.append("    条件：%s" % condition)
        for rule in window["rules"]:
            lines.append("    规则 %s —— %s" % (rule["id"], rule["detail"]))
        lines.append("    非输入（看门狗不读）：")
        for item in window["non_inputs"]:
            lines.append("      %s ← 读它的是 %s（看门狗不读：%s）"
                          % (item["signal"], item["read_by"],
                             item["read_by_watchdog"]))
    lines.append("=" * 72)
    lines.append("退出码：%s" % report.get("exit_code"))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="umshare-watchdog 离线判定件：什么条件下停/重启分享链（只读）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--from-fixture", metavar="PATH",
                       help="读一份本地夹具 JSON 并打印判定（唯一读入；不连网络）")
    group.add_argument("--print-rules", action="store_true",
                       help="只打印规则表与退出码约定（只读）")
    parser.add_argument("--json", action="store_true",
                        help="以 JSON 打印判定结果")
    args = parser.parse_args(argv)

    if args.print_rules:
        print(format_rules())
        return 0
    if args.from_fixture:
        try:
            with open(args.from_fixture, encoding="utf-8") as handle:
                fixture = json.load(handle)
        except (OSError, ValueError) as exc:
            print("[夹具] 读取失败：%s: %s" % (type(exc).__name__, exc),
                  file=sys.stderr)
            return 2
        report = judge_fixture(fixture)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_report(report))
        return int(report.get("exit_code", EXIT_BY_VERDICT[VERDICT_UNDECIDABLE]))

    # Default: read-only rule listing (no fixture, no IO).
    print(format_rules())
    print()
    print("默认只读：未给 --from-fixture 时本工具不读任何夹具、不连网络、不查进程。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
