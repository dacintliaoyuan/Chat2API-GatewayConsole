#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""um-share 工具集写面的静态审计（只读；不写文件、不联网、不起进程）。

背景：维护者 2026-09-28 复审执行轮 9 指出「标为只读的检查有条件写入」——
验收采集必须真正无写副作用（不能经 ``console_share_production`` 的条件删除 stale
``backend.pid``），窗口前必须先有有权限的权威读数。本工具把整个工具集的**写面**盘成
一张机器可读的表：每个写动作在哪一行、什么触发条件、档位（能否关掉）。

只读边界（本工具的全部承诺）：

* 只读打开被审脚本 + 只做 AST 静态解析；**不导入、不执行**被审脚本（不跑它们的
  模块级代码、不触发它们的委托调用）；
* **不写任何文件、不创建目录、不联网、不起任何外部进程**；输出只到 stdout；
* 退出码：``0`` = 只发现 (a)(b) 档；``2`` = 发现 (c) 档，或存在未登记 /
  无法判定的写动作（含声明过期）。**读不到 / 判不出 ≠ 通过。**

三档口径（与 ``output/ai-gw-round10-20260928/write-surface-audit.md`` 同一份）：

* ``a`` 无副作用：只读打开、只读 GET、只读查询命令、纯函数；不改变任何持久状态。
* ``b`` 需显式开关且默认关闭：不给显式开关时该动作不可达（``--out`` 写这份文件、
  ``--apply`` 应用变更、``--authorized`` 授权写清单、``--run`` 真实发请求、
  ``--revert`` / ``--rehearse-rollback`` / ``--allow-new-store``）。
* ``c`` 默认路径就会发生、必须作为窗口动作管理：工具被正常使用（不给任何**写授权**
  开关）就会发生，或该写动作没有可关闭的开关。

判定分两级，**写动作以声明表为准，机械层给出现场行号与保守档位**：

1. **机械发现层**：AST 扫出全部写原语（``open`` 写模式、``os.*`` 变更、``shutil.*``、
   ``Path.write_*`` / ``mkdir`` / ``unlink``、``tempfile`` 创建、``.write()`` /
   ``json.dump()``、HTTP 写方法、变更型外部命令、变更型 PowerShell 脚本）与委托调用
   （``DELEGATED`` 里登记的产品符号）。机械层同时给出**启发式档位**：按静态调用图从
   ``main`` / 模块级出发，收集路径上的开关名（含函数体内提到的开关名）；
   委托写动作只有在**每条**路径上都出现**写授权开关**（``AUTH_SWITCHES``）时才是 (b)，
   只有模式 / 路径开关（``--check`` / ``--out``）时仍是 (c)——该写不是那个开关的语义，
   而是被调函数的副作用；直写动作在每条路径上都有写开关（含 ``--out``）时是 (b)。
2. **声明表**（``DECLARED``）：已人工审计的脚本（``AUDITED_FILES``）里的**写动作**逐条登记
   档位与触发条件，键 =（文件 / 函数 / 动作 / 同名序号），**不含行号**——行号由机械层现场给出，
   登记表不因行漂移而失效。声明与启发式不一致时必须在声明里给 ``note`` 说明原因。
   两类别例外，其档位由机械层**完全判定**、不进声明表（产品代码改了机械层跟着改，不会留下过期
   声明）：产品侧只读的委托符号；以及 ``forget_stale`` 链（按调用点的 ``forget_stale`` 实参 +
   路径开关判定）。

未登记的写动作 → ``?``（未登记）；声明了但机械层找不到 → ``?``（声明过期）；
两者都与 (c) 一样让退出码非零。本工具是**审计件**：不修改任何被审脚本、不执行修复。
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from collections import deque

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TIER_READ_ONLY = "a"
TIER_GATED = "b"
TIER_DEFAULT = "c"
TIER_UNDECIDED = "?"

#: 已人工审计的脚本集合：只有这些文件里的写动作必须逐条登记（``DECLARED``）；
#: 未登记 → ``?``。--target 传别的文件（夹具 / 新脚本）时按机械层档位报，
#: 但 (c) 一样非零。
AUDITED_FILES = frozenset((
    "tools/um-share/actions/generate_attribution_key.py",
    "tools/um-share/actions/ledger_row_recheck.py",
    "tools/um-share/actions/metering_acceptance.py",
    "tools/um-share/actions/metering_request_probe.py",
    "tools/um-share/readonly/post_restart_acceptance.py",
    "tools/um-share/readonly/request_budget_estimate.py",
    "tools/um-share/readonly/share_chain_snapshot.py",
    "tools/um-share/actions/test_key_ops.py",
    "tools/um-share/readonly/watchdog_forensics.py",
    "tools/handoff_anchor_audit.py",
))

#: 被审脚本（仓库相对路径）；``--target`` 可覆盖，用于夹具测试。
TARGETS = tuple(sorted(AUDITED_FILES))

#: 写授权开关：开关的语义就是「执行这个写动作」。委托写动作只有在**每条**路径上
#: 都出现这里的开关时才是 (b)；``out`` 是「写这份文件」的开关，故直写动作也算 (b)。
AUTH_SWITCHES = frozenset((
    "apply", "authorized", "run", "revert", "rehearse_rollback",
    "allow_new_store",
))
#: 写开关：直写动作（写动作就是被调函数的语义）的开关集合。
WRITE_SWITCHES = AUTH_SWITCHES | frozenset(("out",))
#: 模式开关：只选读取 / 采集模式或只指定路径，本身不是写授权。
MODE_SWITCHES = frozenset((
    "check", "compare", "json", "list", "dry_run", "from_fixture",
    "print_rules", "quiet", "before", "after", "target", "stamp",
    "backup", "keepset", "state", "config", "backup_root", "repo",
    "project", "ref", "max_excerpt", "route", "prompt", "max_tokens",
    "timeout", "expect", "credential_file", "max_cost_usd",
    "authorized_requests", "post_entry_state", "ledger", "plan",
))
ALL_SWITCH_NAMES = WRITE_SWITCHES | MODE_SWITCHES

#: 写模式（``open`` / ``io.open``）：出现任一字符即写。
WRITE_MODE_CHARS = ("w", "a", "x", "+")

#: 点号写原语：``os.*`` / ``shutil.*`` / ``tempfile.*`` / ``json.dump``。
#: 只登记**真会改状态**的成员；``os.stat`` / ``os.path.*`` / ``shutil.which`` /
#: ``tempfile.gettempdir`` 等只读成员不在表内。
DOTTED_WRITES = {
    "os.replace": "os.replace",
    "os.remove": "os.remove",
    "os.unlink": "os.unlink",
    "os.rmdir": "os.rmdir",
    "os.mkdir": "os.mkdir",
    "os.makedirs": "os.makedirs",
    "os.chmod": "os.chmod",
    "os.rename": "os.rename",
    "os.truncate": "os.truncate",
    "shutil.rmtree": "shutil.rmtree",
    "shutil.move": "shutil.move",
    "shutil.copy": "shutil.copy",
    "shutil.copy2": "shutil.copy2",
    "shutil.copyfile": "shutil.copyfile",
    "shutil.copytree": "shutil.copytree",
    "tempfile.mkdtemp": "tempfile.mkdtemp",
    "tempfile.mkstemp": "tempfile.mkstemp",
    "tempfile.NamedTemporaryFile": "tempfile.NamedTemporaryFile",
    "tempfile.TemporaryDirectory": "tempfile.TemporaryDirectory",
    "json.dump": "json.dump",
}

#: 任意接收者的写方法（按方法名判定）。刻意**不含** ``replace``：字符串的
#: ``str.replace`` 是纯函数（handoff_anchor_audit.py:116 就是它），只认 ``os.replace``。
ATTR_WRITES = {
    "write": "write",
    "writelines": "writelines",
    "write_text": "write_text",
    "write_bytes": "write_bytes",
    "unlink": "unlink",
    "mkdir": "mkdir",
    "touch": "touch",
    "rmdir": "rmdir",
}

#: 变更型外部命令：``(可执行名, 变更参数前缀元组)``；不在表内或参数不匹配即只读。
MUTATING_COMMANDS = {
    "icacls": ("/grant", "/deny", "/remove", "/inheritance", "/setowner",
               "/reset", "/restore", "/save", "/substitute"),
    "taskkill": (),
    "schtasks": ("/create", "/change", "/delete", "/run", "/end"),
    "reg": ("add", "delete", "import", "copy", "restore", "save"),
    "sc": ("create", "delete", "config", "start", "stop", "failure"),
    "net": ("start", "stop", "user", "share", "localgroup"),
    "powershell": (),
    "pwsh": (),
    "git": ("push", "reset", "clean", "checkout", "commit", "rm", "mv",
            "restore", "revert", "stash"),
    "wmic": ("delete", "call", "set"),
}

#: 变更型 PowerShell 动词：只查真正写状态的那些；``Get-*`` / ``ConvertTo-*`` 只读。
PS_MUTATING_VERBS = (
    "Set-Content", "Add-Content", "Out-File", "New-Item", "Remove-Item",
    "Set-Acl", "Set-Item", "Set-ItemProperty", "New-ItemProperty",
    "Remove-ItemProperty", "Start-Process", "Stop-Process", "taskkill",
    "Register-ScheduledTask", "Unregister-ScheduledTask",
    "Start-ScheduledTask", "Stop-ScheduledTask", "Enable-ScheduledTask",
    "Disable-ScheduledTask", "Restart-Computer", "Stop-Service",
    "Start-Service", "Restart-Service", "Set-ScheduledTask",
)
#: 认作「开关容器」的变量名：``args.out`` / ``intent["apply"]`` / ``plan["run"]``。
#: 只按这些名字收集开关，避免把普通局部变量 ``run``（进程查询函数）误当开关。
SWITCH_RECEIVERS = frozenset((
    "args", "options", "opts", "flags", "ns", "namespace", "intent", "plan",
    "gate", "decision",
))
#: 跑外部命令的封装名（其字符串参数按 PowerShell 脚本文本检查）。
RUNNER_NAMES = frozenset((
    "_run_command", "_run_capture", "_run_powershell", "_run", "run",
    "subprocess.run", "check_output", "call", "Popen",
))
#: 模块级 PowerShell 脚本常量名后缀（其字符串值按脚本检查）。
SCRIPT_CONST_SUFFIX = "_SCRIPT"

#: 委托符号：工具把只读 / 写动作委托给产品模块；值 = 产品侧写面说明。
#: 键 = 符号名（``ast.Name`` / ``ast.Attribute.attr``）。
FORGET_STALE_KINDS = (
    "delegated:production_backend_observe",
    "delegated:backend_observe_facts",
)

DELEGATED = {
    "production_backend_observe": {
        "read_only": False,
        "surface": "console/share_production.py:1496 → verify_running()；forget_stale=True（默认）时"
                   "经 _forget() os.remove 陈旧 backend.pid（:1343, :1381-1384）；"
                   "forget_stale=False 时全分支无写。",
        "citation": "ai-gateway/console/share_production.py:1495-1538",
    },
    "backend_observe_facts": {
        "read_only": False,
        "surface": "share_chain_snapshot.py:907 的加性开关原样转发给 "
                   "production_backend_observe(forget_stale=...)：默认 True = 陈旧 "
                   "backend.pid 仍会被删。",
        "citation": "tools/um-share/readonly/share_chain_snapshot.py:907-921",
    },
    "production_backend_pid_state": {
        "read_only": True,
        "surface": "console/share_production.py:1550 → backend_pid_state()；全分支无写、"
                   "无删（陈旧 pid 原样留在盘上）。",
        "citation": "ai-gateway/console/share_production.py:1348-1397, :1549-1558",
    },
    "share_attribution_status": {
        "read_only": True,
        "surface": "console/share_production.py:1111；只读事实（stat + 只读打开 + "
                   "只读 CIM 查询），无写。",
        "citation": "ai-gateway/console/share_production.py:1111-1185",
    },
    "share_attribution_key_path": {
        "read_only": True,
        "surface": "console/share_production.py:864；只读路径解析（环境变量 + 默认根），无写。",
        "citation": "ai-gateway/console/share_production.py:864-880",
    },
    "_share_run_root": {
        "read_only": True,
        "surface": "console/share_production.py:851；只读路径解析，无写。",
        "citation": "ai-gateway/console/share_production.py:851-861",
    },
    "estimate_record_with_version": {
        "read_only": True,
        "surface": "usagesrv.py:751 → load_price_table()（:549）只读价目文件；无写。",
        "citation": "ai-gateway/usagesrv.py:549-645, :751-766",
    },
    "get_share_manager": {
        "read_only": False,
        "surface": "usagesrv.py:1804 → ShareManager()（share/management.py:251）→ "
                   "_recover_interrupted_operations() 会写 share-store.db 的 ops 行；"
                   "真实 broker 装配还会在线发布。",
        "citation": "ai-gateway/usagesrv.py:1804-1811; "
                    "ai-gateway/share/management.py:251-274, :310-420",
    },
    "ShareManager": {
        "read_only": False,
        "surface": "share/management.py:251 __init__ → _recover_interrupted_operations()"
                   "（写 ops 行 / 可能驱动受限关停）；构造即写库。",
        "citation": "ai-gateway/share/management.py:251-274, :310-420",
    },
    "create_key": {
        "read_only": False,
        "surface": "share/management.py:672 → share/store.py:562 写 keys 行 + configgen "
                   "代次 + publish intent（同一事务）。",
        "citation": "ai-gateway/share/management.py:672; ai-gateway/share/store.py:562",
    },
    "delete_key": {
        "read_only": False,
        "surface": "share/management.py:797 → share/store.py:859 软删除 tombstone + "
                   "publish intent；撤销方向 fail-closed。",
        "citation": "ai-gateway/share/management.py:797-803; "
                    "ai-gateway/share/store.py:859-877",
    },
    "render_provider_config_sync": {
        "read_only": True,
        "surface": "model_sync/sync.py:468；纯函数（解析 + 返回新文本），无写。",
        "citation": "ai-gateway/model_sync/sync.py:468-610",
    },
    "CHAT2API_LOG": {
        "read_only": True,
        "surface": "usagesrv.py:75；模块级路径常量，读取不改状态。",
        "citation": "ai-gateway/usagesrv.py:75-76",
    },
    "DEFAULT_STORE_FILE": {
        "read_only": True,
        "surface": "share/management.py:28；模块级路径常量，读取不改状态。",
        "citation": "ai-gateway/share/management.py:28",
    },
    "CODE_STAMP": {
        "read_only": True,
        "surface": "model_sync/sync.py:65；模块级常量（自身文件指纹），读取不改状态。",
        "citation": "ai-gateway/model_sync/sync.py:57-65",
    },
    "DEFAULT_BINDINGS": {
        "read_only": True,
        "surface": "model_sync/sync_core.py:84；模块级绑定表，读取不改状态。",
        "citation": "ai-gateway/model_sync/sync_core.py:84",
    },
    "SHARE_ATTRIBUTION_KEY_NAME": {
        "read_only": True,
        "surface": "console/share_production.py:846；模块级常量，读取不改状态。",
        "citation": "ai-gateway/console/share_production.py:846",
    },
    "StoreError": {
        "read_only": True,
        "surface": "share/store.py 异常类型，捕获不写状态。",
        "citation": "ai-gateway/share/store.py",
    },
}

#: 声明表：已人工审计的写动作。键 = (文件, 函数, 动作, 同名序号)；序号按行号升序。
#: ``line`` 只作审计留档（机械层现场给出行号，不用于匹配）；``note`` 是
#: 与启发式档位不一致时必须给出的原因。``citation`` 用于委托条目（产品侧行号）。
DECLARED = {
    # ------------------------------------------------------ (b) 显式开关且默认关闭
    ("tools/um-share/actions/generate_attribution_key.py",
     "write_key_file", "open", 1):
        {"tier": "b", "line": 799,
         "trigger": "--apply 或 --rehearse-rollback（写密钥文件；--apply 只允许 "
                    r"C:\ProgramData\UMShareRun 之下）"},
    ("tools/um-share/actions/generate_attribution_key.py",
     "write_key_file", "write", 1):
        {"tier": "b", "line": 800,
         "trigger": "同上（写 64 字节 hex，无 BOM、单行、不追加结尾换行）"},
    ("tools/um-share/actions/generate_attribution_key.py",
     "apply_acl", "icacls", 1):
        {"tier": "b", "line": 806,
         "trigger": "--apply 或 --rehearse-rollback（icacls /inheritance:r + 四个 "
                    "/grant:r 改 ACL）"},
    ("tools/um-share/actions/generate_attribution_key.py",
     "rehearse_rollback", "tempfile.mkdtemp", 1):
        {"tier": "b", "line": 965, "trigger": "--rehearse-rollback（临时目录里演练回退）"},
    ("tools/um-share/actions/generate_attribution_key.py",
     "rehearse_rollback", "os.remove", 1):
        {"tier": "b", "line": 1014, "trigger": "--rehearse-rollback（删临时目录里的假密钥）"},
    ("tools/um-share/actions/generate_attribution_key.py",
     "rehearse_rollback", "shutil.rmtree", 1):
        {"tier": "b", "line": 1025, "trigger": "--rehearse-rollback（删整个临时目录）"},
    ("tools/um-share/actions/ledger_row_recheck.py",
     "write_manifest_atomic", "open", 1):
        {"tier": "b", "line": 498,
         "trigger": "--authorized --out <path>（写可执行移除清单；默认不写）"},
    ("tools/um-share/actions/ledger_row_recheck.py",
     "write_manifest_atomic", "write", 1):
        {"tier": "b", "line": 499, "trigger": "同上（清单正文写入临时文件）"},
    ("tools/um-share/actions/ledger_row_recheck.py",
     "write_manifest_atomic", "unlink", 1):
        {"tier": "b", "line": 503, "trigger": "同上（临时文件读回不一致时删临时文件）"},
    ("tools/um-share/actions/ledger_row_recheck.py",
     "write_manifest_atomic", "os.replace", 1):
        {"tier": "b", "line": 505, "trigger": "同上（受控替换到 --out）"},
    ("tools/um-share/actions/metering_request_probe.py",
     "send_request", "urllib.request.Request", 1):
        {"tier": "b", "line": 368,
         "trigger": "--run 且 --authorized-requests/--max-cost-usd/--model/"
                    "--credential-file 全给且两门通过、费用可证 ≤ 上限（唯一带凭据的"
                    "白名单 POST，真实模型请求）"},
    ("tools/um-share/readonly/share_chain_snapshot.py",
     "write_snapshot", "os.makedirs", 1):
        {"tier": "b", "line": 299, "trigger": "--out <path>（快照目录不存在时创建）"},
    ("tools/um-share/readonly/share_chain_snapshot.py",
     "write_snapshot", "open", 1):
        {"tier": "b", "line": 301, "trigger": "--out <path>（先写临时文件）"},
    ("tools/um-share/readonly/share_chain_snapshot.py",
     "write_snapshot", "json.dump", 1):
        {"tier": "b", "line": 302, "trigger": "--out <path>（快照正文写入临时文件）"},
    ("tools/um-share/readonly/share_chain_snapshot.py",
     "write_snapshot", "write", 1):
        {"tier": "b", "line": 303, "trigger": "--out <path>（补结尾换行）"},
    ("tools/um-share/readonly/share_chain_snapshot.py",
     "write_snapshot", "os.replace", 1):
        {"tier": "b", "line": 304, "trigger": "--out <path>（受控替换）"},
    ("tools/um-share/actions/test_key_ops.py",
     "write_secret_exclusive", "open", 1):
        {"tier": "b", "line": 370,
         "trigger": "--apply --create --secret-out <path>（明文独占写入该文件）"},
    ("tools/um-share/actions/test_key_ops.py",
     "write_secret_exclusive", "write", 1):
        {"tier": "b", "line": 371, "trigger": "同上（写入明文）"},
    ("tools/um-share/actions/test_key_ops.py",
     "write_secret_exclusive", "os.chmod", 1):
        {"tier": "b", "line": 373,
         "trigger": "同上（尽力设 0600；Windows 上只影响只读位）"},
    ("tools/um-share/actions/test_key_ops.py",
     "build_manager", "delegated:ShareManager", 1):
        {"tier": "b", "line": 364,
         "trigger": "--apply（构造即写库：_recover_interrupted_operations 写 ops 行）",
         "citation": "share/management.py:251-274, :310-420",
         "note": "启发式把 build_manager 归 (c)：--apply 门在 main 的 "
                    "if not intent[\"apply\"]: return EXIT_OK 提前返回里，静态扫描"
                    "不建模这种提前返回；人工审计按 --apply 判 (b)。"},
    ("tools/um-share/actions/test_key_ops.py",
     "build_manager", "delegated:get_share_manager", 1):
        {"tier": "b", "line": 363,
         "trigger": "--apply --broker console（控制台自己的装配：真实 broker 会在线发布）",
         "citation": "usagesrv.py:1804-1811",
         "note": "启发式把 build_manager 归 (c)：--apply 门在 main 的提前返回里；"
                    "人工审计按 --apply 判 (b)。"},
    ("tools/um-share/actions/test_key_ops.py",
     "apply_create", "delegated:create_key", 1):
        {"tier": "b", "line": 385,
         "trigger": "--apply --create（写权威存储 keys 行 + 代次 + publish intent）",
         "citation": "share/management.py:672; share/store.py:562",
         "note": "启发式把 apply_create 归 (c)：--apply 门在 main 的提前返回里；"
                    "人工审计按 --apply 判 (b)。"},
    ("tools/um-share/actions/test_key_ops.py",
     "apply_revoke", "delegated:delete_key", 1):
        {"tier": "b", "line": 404,
         "trigger": "--apply --revoke（软删除 tombstone + publish intent；发布未确认时 "
                    "fail-closed）",
         "citation": "share/management.py:797-803; share/store.py:859-877",
         "note": "启发式把 apply_revoke 归 (c)：--apply 门在 main 的提前返回里；"
                    "人工审计按 --apply 判 (b)。"},
}


def _dotted(node):
    """``ast`` 表达式 → 点号名（``os.replace``）；拼不出返回 None。"""
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return "%s.%s" % (base, node.attr) if base else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _string_items(node):
    """列表 / 元组字面量里的字符串常量。"""
    return [element.value for element in node.elts
            if isinstance(element, ast.Constant)
            and isinstance(element.value, str)]


def _const_text(node):
    """调用参数里的字符串常量（含 ``%`` 拼接与嵌套列表里的字面）。"""
    return [child.value for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)]


def _open_mode(node):
    """``open`` 调用的模式：第二个位置参数或 ``mode=``；缺省 = 只读 ``r``。"""
    mode = None
    if len(node.args) >= 2:
        second = node.args[1]
        if isinstance(second, ast.Constant) and isinstance(second.value, str):
            mode = second.value
    for keyword in node.keywords or ():
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant) \
                and isinstance(keyword.value.value, str):
            mode = keyword.value.value
    return mode or "r"


def _is_write_mode(mode):
    return any(char in mode for char in WRITE_MODE_CHARS)


def _ps_mutating(text):
    for verb in PS_MUTATING_VERBS:
        if verb.lower() in text.lower():
            return verb
    return None


def _command_list_kind(node):
    """列表 / 元组字面量 → ``(动作, 是否变更)``；非命令字面量返回 None。"""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    items = _string_items(node)
    if not items:
        return None
    head = os.path.basename(items[0]).lower()
    if head not in MUTATING_COMMANDS:
        return None
    prefixes = MUTATING_COMMANDS[head]
    if head in ("powershell", "pwsh"):
        verb = _ps_mutating(" ".join(items))
        if verb:
            return ("powershell:" + verb, True)
        return ("powershell", False)
    if not prefixes:
        return (head, True)
    for item in items[1:]:
        lowered = item.lower()
        for prefix in prefixes:
            if lowered.startswith(prefix):
                return (head, True)
    return (head, False)


def _write_call_kind(node):
    """一个调用节点 → 写动作名；只读调用返回 None。"""
    dotted = _dotted(node.func)
    if dotted is None:
        return None
    if dotted in DOTTED_WRITES:
        return DOTTED_WRITES[dotted]
    if dotted in ("open", "io.open"):
        return "open" if _is_write_mode(_open_mode(node)) else None
    leaf = dotted.rsplit(".", 1)[-1]
    if leaf in ATTR_WRITES and dotted != "json.dump":
        return ATTR_WRITES[leaf]
    if dotted in ("urllib.request.Request", "Request"):
        method = None
        for keyword in node.keywords or ():
            if keyword.arg == "method" and isinstance(keyword.value,
                                                      ast.Constant):
                method = keyword.value.value
        if isinstance(method, str) and method.upper() in ("POST", "PUT",
                                                         "PATCH", "DELETE"):
            return "urllib.request.Request"
    return None


def _runner_call(node):
    dotted = _dotted(node.func)
    if dotted is None:
        return False
    return dotted in RUNNER_NAMES or dotted.rsplit(".", 1)[-1] in RUNNER_NAMES


def _read_call_kind(node):
    """一个调用节点 → 只读动作名；写 / 判不出返回 None。"""
    dotted = _dotted(node.func)
    if dotted is None:
        return None
    if dotted in ("open", "io.open"):
        return "open_read" if not _is_write_mode(_open_mode(node)) else None
    if dotted in ("urllib.request.urlopen", "urlopen"):
        return "http_read"
    if dotted in ("urllib.request.Request", "Request"):
        for keyword in node.keywords or ():
            if keyword.arg == "method" and isinstance(keyword.value,
                                                      ast.Constant) \
                    and isinstance(keyword.value.value, str):
                if keyword.value.value.upper() not in ("GET", "HEAD"):
                    return None
        return "http_read"
    if _runner_call(node):
        return "command"
    return None


class FunctionFacts:
    """一个函数的静态事实：定义行、写 / 读动作、调用点、开关名。"""

    def __init__(self, node):
        self.node = node
        self.name = node.name
        self.lineno = node.lineno
        self.writes = []           # [(lineno, kind)]
        self.reads = []             # [(lineno, kind)]
        self.calls = []             # [(lineno, callee, 调用点开关)]
        self.gates = set()          # 函数体内提到的开关名
        self.value_refs = set()     # 函数体内作为值引用的本文件函数名


def _switch_names_in(node):
    """开关名：只认开关容器上的属性（``args.out``）与容器下标（``intent["apply"]``）。

    开关容器名 = ``SWITCH_RECEIVERS``；另认 ``getattr(args, "run", ...)`` 这种
    取开关的写法。普通局部变量 / 函数参数不参与——例如把进程查询函数叫 ``run``
    不会被误当成 ``--run`` 开关。
    """
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) \
                and isinstance(child.value, ast.Name) \
                and child.value.id in SWITCH_RECEIVERS:
            names.add(child.attr)
        elif isinstance(child, ast.Subscript) \
                and isinstance(child.value, ast.Name) \
                and child.value.id in SWITCH_RECEIVERS \
                and isinstance(child.slice, ast.Constant) \
                and isinstance(child.slice.value, str):
            names.add(child.slice.value)
        elif isinstance(child, ast.Call) \
                and isinstance(child.func, ast.Name) \
                and child.func.id == "getattr" and len(child.args) >= 2 \
                and isinstance(child.args[0], ast.Name) \
                and child.args[0].id in SWITCH_RECEIVERS \
                and isinstance(child.args[1], ast.Constant) \
                and isinstance(child.args[1].value, str):
            names.add(child.args[1].value)
    return names & ALL_SWITCH_NAMES


class ModuleFacts:
    """一个被审脚本的静态事实（AST 进、字典出；不导入、不执行）。"""

    def __init__(self, relpath, source):
        self.relpath = relpath
        self.tree = ast.parse(source)
        self.functions = {}
        self.parent = {}
        self.module_writes = []     # [(lineno, kind, 函数, 开关)]
        self.module_reads = []      # [(lineno, kind, 函数)]
        self.module_calls = []      # [(lineno, callee, 开关)]
        self.delegated = []         # [(lineno, 符号, forget_stale, 函数, 开关)]
        self.script_consts = []     # [(lineno, 常量名, 文本)]（*_SCRIPT 常量）
        self.module_consts = {}      # 模块级字符串常量：名字 → 文本
        self.write_request_names = set()   # 装了写请求的变量名（urlopen 据此不报只读）
        self._build()

    # ------------------------------------------------------------ 构建
    def _build(self):
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parent[child] = node
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name] = FunctionFacts(node)
        for node in self.tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if not isinstance(target, ast.Name):
                        continue
                    if isinstance(node.value, ast.Constant) \
                            and isinstance(node.value.value, str):
                        self.module_consts[target.id] = node.value.value
                        if target.id.endswith(SCRIPT_CONST_SUFFIX):
                            self.script_consts.append((node.lineno, target.id,
                                                      node.value.value))
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Assign) \
                    and isinstance(node.value, ast.Call) \
                    and _write_call_kind(node.value) \
                    and isinstance(node.targets[0], ast.Name):
                self.write_request_names.add(node.targets[0].id)

        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(node, (ast.List, ast.Tuple)):
                kind = _command_list_kind(node)
                if kind and kind[1]:
                    self._record_write(node, kind[0])
                elif kind and not isinstance(self.parent.get(node), ast.Call):
                    # 命令字面量直接当参数传给跑命令的封装时，由那个调用节点统一报，
                    # 避免同一行报两条。
                    self._record_read(node, "command")
            elif isinstance(node, ast.Call):
                kind = _write_call_kind(node)
                if kind is None and _runner_call(node):
                    for text in _const_text(node):
                        verb = _ps_mutating(text)
                        if verb:
                            kind = "powershell:%s" % verb
                            break
                if kind:
                    self._record_write(node, kind)
                else:
                    read = _read_call_kind(node)
                    if read == "command":
                        text = self._runner_text(node)
                        if text is None or _ps_mutating(text):
                            read = None
                    if read == "http_read":
                        for arg in node.args:
                            if isinstance(arg, ast.Name) \
                                    and arg.id in self.write_request_names:
                                read = None   # urlopen(写请求变量)：不是只读
                    if read:
                        self._record_read(node, read)
                self._record_call(node)
                self._record_delegated(node)
            elif isinstance(node, ast.Attribute) and node.attr in DELEGATED:
                parent = self.parent.get(node)
                if not (isinstance(parent, ast.Call) and parent.func is node):
                    func = self._enclosing_function(node)
                    self._record_delegated_attr(node, func)

        for name, facts in self.functions.items():
            facts.gates = _switch_names_in(facts.node)
            for child in ast.walk(facts.node):
                if isinstance(child, ast.Name) and child.id in self.functions \
                        and child.id != name:
                    facts.value_refs.add(child.id)
                elif isinstance(child, ast.Attribute) \
                        and child.attr in self.functions and child.attr != name:
                    facts.value_refs.add(child.attr)

    def _enclosing_function(self, node):
        current = self.parent.get(node)
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return current.name
            current = self.parent.get(current)
        return None

    def _gates_of(self, node):
        gates = set()
        current = self.parent.get(node)
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef,
                                    ast.Module)):
                break
            if isinstance(current, ast.If):
                gates |= _switch_names_in(current.test)
            current = self.parent.get(current)
        return gates

    def _runner_text(self, node):
        """跑外部命令调用的命令文本；参数里有解析不了的名字即返回 None。"""
        parts = []
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                parts.append(arg.value)
            elif isinstance(arg, ast.Name) and arg.id in self.module_consts:
                parts.append(self.module_consts[arg.id])
            elif isinstance(arg, (ast.List, ast.Tuple)):
                parts.extend(_string_items(arg))
            else:
                return None
        return " ".join(parts) or None

    def _record_write(self, node, kind):
        func = self._enclosing_function(node)
        gates = self._gates_of(node)
        entry = (node.lineno, kind, func, frozenset(gates))
        if func is None or func not in self.functions:
            self.module_writes.append(entry)
        else:
            self.functions[func].writes.append(entry)

    def _record_read(self, node, kind):
        func = self._enclosing_function(node)
        entry = (node.lineno, kind, func)
        if func is None or func not in self.functions:
            self.module_reads.append(entry)
        else:
            self.functions[func].reads.append(entry)

    def _record_call(self, node):
        callee = _dotted(node.func)
        if callee is None:
            return
        func = self._enclosing_function(node)
        entry = (node.lineno, callee, frozenset(self._gates_of(node)))
        if func is None or func not in self.functions:
            self.module_calls.append(entry)
        else:
            self.functions[func].calls.append(entry)

    def _record_delegated(self, node):
        symbol = _dotted(node.func)
        if symbol is None:
            return
        leaf = symbol.rsplit(".", 1)[-1]
        if leaf not in DELEGATED:
            return
        func = self._enclosing_function(node)
        forget_stale = None
        for keyword in node.keywords or ():
            if keyword.arg != "forget_stale":
                continue
            value = keyword.value
            if isinstance(value, ast.Constant) and isinstance(value.value, bool):
                forget_stale = value.value
            elif isinstance(value, ast.Name):
                # 转发型调用（``forget_stale=forget_stale``）：按**所在函数**
                # 该参数的默认值解析（2026-09-28 修：包装器默认改成只读后，
                # 不解析就会把「默认路径不写」误报成 (c)）。
                forget_stale = self._enclosing_param_default(func, value.id)
        self.delegated.append((node.lineno, leaf, forget_stale, func,
                               frozenset(self._gates_of(node))))

    def _enclosing_param_default(self, func, name):
        """把 ``x=<参数名>`` 解析成该参数的 bool 默认值；解析不出返回 None。

        只认 bool 字面量默认值：无默认、非字面量、非该函数参数一律 None，
        交给分类层保守按 (c)。位置参数（含 posonly）与 keyword-only 都覆盖。
        """
        facts = self.functions.get(func)
        if facts is None:
            return None
        args = facts.node.args
        positional = list(getattr(args, "posonlyargs", []) or []) + list(args.args)
        defaults = list(args.defaults or [])
        offset = len(positional) - len(defaults)
        for index, arg in enumerate(positional):
            if arg.arg != name:
                continue
            if index < offset:
                return None
            default = defaults[index - offset]
            if isinstance(default, ast.Constant) and isinstance(default.value, bool):
                return default.value
            return None
        for arg, default in zip(args.kwonlyargs or [], args.kw_defaults or []):
            if arg.arg == name:
                if isinstance(default, ast.Constant) and isinstance(default.value, bool):
                    return default.value
                return None
        return None

    def _record_delegated_attr(self, node, func):
        kind = "delegated_attr:%s" % node.attr
        if func is None or func not in self.functions:
            self.module_writes.append((node.lineno, kind, func, frozenset()))
        else:
            self.functions[func].writes.append(
                (node.lineno, kind, func, frozenset()))

    # ----------------------------------------------------------- 可达性
    def reachable_gate_sets(self):
        """函数 → 从 main / 模块级到它的**最小开关集列表**（判不出返回 None）。"""
        edges = {}
        for name, facts in self.functions.items():
            edges.setdefault(name, [])
            for _lineno, callee, gates in facts.calls:
                edges[name].append((callee.rsplit(".", 1)[-1], gates))
            for ref in facts.value_refs:
                edges[name].append((ref, frozenset()))
        entries = [("main", frozenset())]
        for _lineno, callee, gates in self.module_calls:
            entries.append((callee.rsplit(".", 1)[-1], gates))
        paths = {}
        queue = deque(entries)
        while queue:
            name, gates = queue.popleft()
            if name not in self.functions:
                continue
            gates = gates | self.functions[name].gates
            existing = paths.setdefault(name, [])
            if any(gates >= item for item in existing):
                continue
            existing[:] = [item for item in existing if not item >= gates]
            existing.append(gates)
            for callee, extra in edges.get(name, ()):
                queue.append((callee, gates | extra))
        return paths


def _decide_tier(item, path_sets):
    """启发式档位：委托写只有在**每条**路径上都有写授权开关时才是 (b)。

    ``path_sets`` = 从 main / 模块级到该函数的**最小开关集列表**（去掉了被包含的
    超集）；``None`` = 静态调用图到不了该函数（经注册表 / 动态分派间接调用）。
    """
    kind = item["kind"]
    if item.get("read_only_symbol"):
        return TIER_READ_ONLY, "该委托符号在产品侧只读（无写）"
    if kind in FORGET_STALE_KINDS:
        symbol = kind.split(":", 1)[1]
        base = ("调用 %s：forget_stale=True（默认）时产品侧经 _forget() 删陈旧 "
                "backend.pid；forget_stale=False 时全分支无写、无删。" % symbol)
        if item["forget_stale"] is False:
            return TIER_READ_ONLY, base + "本调用点显式传 forget_stale=False → 只读。"
        if path_sets is None:
            return TIER_DEFAULT, base + "该调用点静态调用图到不了（经注册表间接）：保守按 (c)。"
        union = set().union(*path_sets) if path_sets else set()
        if any(not (item & AUTH_SWITCHES) for item in path_sets):
            return TIER_DEFAULT, base + "该调用点所在路径没有写授权开关（%s）→ 采集的默认路径就会写。" \
                % (",".join(sorted(union)) or "无")
        return TIER_GATED, base + "该调用点每条路径上都有写授权开关 %s。" \
            % ",".join(sorted(union & AUTH_SWITCHES))
    if path_sets is None:
        return TIER_DEFAULT, "静态调用图到不了该函数（经注册表 / 动态分派间接）：保守按 (c)"
    union = set().union(*path_sets) if path_sets else set()
    shown = ",".join(sorted(union)) or "（无）"
    if item["delegated"]:
        if any(not (item & AUTH_SWITCHES) for item in path_sets):
            return TIER_DEFAULT, ("路径上没有写授权开关（只有 %s）：该写不是它们的语义"
                                 % shown)
        return TIER_GATED, None
    if any(not (item & WRITE_SWITCHES) for item in path_sets):
        return TIER_DEFAULT, ("路径上的开关 %s 不是写开关" % shown)
    return TIER_GATED, None


def scan_target(root, relpath):
    """一个脚本 → ``{"writes": [...], "reads": [...]}``（机械层；不导入、不执行）。"""
    with open(os.path.join(root, relpath), encoding="utf-8") as handle:
        source = handle.read()
    facts = ModuleFacts(relpath, source)
    paths = facts.reachable_gate_sets()
    writes = []

    def add(lineno, kind, func, gates, delegated, forget_stale):
        ordinal = 1 + sum(1 for item in writes
                          if item["function"] == func and item["kind"] == kind)
        symbol = kind.split(":", 1)[1] if ":" in kind else ""
        entry = DELEGATED.get(symbol) or {}
        writes.append({
            "file": relpath, "line": lineno, "function": func, "kind": kind,
            "ordinal": ordinal, "gates": sorted(gates), "delegated": delegated,
            "forget_stale": forget_stale,
            "read_only_symbol": bool(entry.get("read_only")),
        })

    for lineno, kind, func, gates in sorted(facts.module_writes):
        add(lineno, kind, func, gates, kind.startswith("delegated"), None)
    for name, entry in facts.functions.items():
        for lineno, kind, func, gates in sorted(entry.writes):
            add(lineno, kind, func, gates, kind.startswith("delegated"), None)
    for lineno, symbol, forget_stale, func, gates in sorted(facts.delegated):
        add(lineno, "delegated:%s" % symbol, func, gates, True, forget_stale)
    for lineno, name, text in sorted(facts.script_consts):
        verb = _ps_mutating(text)
        if verb:
            writes.append({
                "file": relpath, "line": lineno, "function": None,
                "kind": "powershell_mutation", "ordinal": 1, "gates": [],
                "delegated": False, "forget_stale": None,
                "read_only_symbol": False,
                "note": "模块级常量 %s 含变更型 PowerShell 动词 %s" % (name, verb),
            })

    writes.sort(key=lambda item: (item["line"], item["kind"]))
    for item in writes:
        func = item["function"]
        if func is None:
            path_sets = [frozenset()]      # 模块级（import 即执行）：没有开关
        elif func in paths:
            path_sets = paths[func]
        else:
            path_sets = None
        item["path_sets"] = ([sorted(item_set) for item_set in path_sets]
                              if path_sets is not None else None)
        item["reachable"] = (sorted(set().union(*path_sets))
                              if path_sets is not None else None)
        tier, note = _decide_tier(item, path_sets)
        item["heuristic_tier"] = tier
        item["heuristic_note"] = note or ""
        declared = DECLARED.get((item["file"], item["function"],
                                item["kind"], item["ordinal"]))
        mechanical = item["read_only_symbol"] \
            or item["kind"] in FORGET_STALE_KINDS
        audited = item["file"] in AUDITED_FILES
        if declared:
            item["declared"] = True
            item["tier"] = declared["tier"]
            item["trigger"] = declared["trigger"]
            item["declared_line"] = declared["line"]
            item["declaration_note"] = declared.get("note") or ""
            item["citation"] = declared.get("citation") or ""
        elif mechanical:
            # 该档位由机械层完全判定（产品侧只读符号 / forget_stale 实参 + 路径
            # 开关），不依赖声明表：产品代码改了这里会跟着改，不会留下过期声明。
            item["declared"] = False
            item["tier"] = item["heuristic_tier"]
            item["trigger"] = item["heuristic_note"]
            item["declared_line"] = None
            item["declaration_note"] = ""
            item["citation"] = (DELEGATED.get(item["kind"].split(":", 1)[1])
                                or {}).get("citation", "")
        elif not audited:
            # 非已审计脚本（例如夹具片段）：声明表不覆盖它，按启发式档位报，
            # 但**不**把「未登记」当通过——(c) 仍然非零。
            item["declared"] = False
            item["tier"] = item["heuristic_tier"]
            item["trigger"] = ("非已审计脚本，按机械层判定：" +
                                item["heuristic_note"])
            item["declared_line"] = None
            item["declaration_note"] = ""
            item["citation"] = ""
        else:
            item["declared"] = False
            item["tier"] = TIER_UNDECIDED
            item["trigger"] = "未登记：机械层发现该写动作，声明表没有对应条目"
            item["declared_line"] = None
            item["declaration_note"] = ""
            item["citation"] = ""

    reads = []
    for lineno, kind, func in sorted(facts.module_reads):
        reads.append({"file": relpath, "line": lineno, "kind": kind,
                      "function": func})
    for name, entry in facts.functions.items():
        for lineno, kind, func in sorted(entry.reads):
            reads.append({"file": relpath, "line": lineno, "kind": kind,
                          "function": func})
    reads.sort(key=lambda item: (item["line"], item["kind"]))
    return {"writes": writes, "reads": reads}


def stale_declarations(findings, targets):
    """已审计脚本里声明了但本次机械层找不到（声明过期）：返回未命中的键列表。

    只对**本次扫到的**已审计脚本判过期：``--target`` 传夹具时不把别的脚本的
    声明误报成过期。
    """
    seen = {(item["file"], item["function"], item["kind"], item["ordinal"])
            for item in findings}
    scanned = set(targets) & AUDITED_FILES
    return [key for key in DECLARED
            if key[0] in scanned and key not in seen]


def _summary(findings, stale):
    summary = {TIER_READ_ONLY: 0, TIER_GATED: 0, TIER_DEFAULT: 0,
               TIER_UNDECIDED: 0, "stale_declarations": len(stale),
               "default_path_writes": []}
    for item in findings:
        summary[item["tier"]] = summary.get(item["tier"], 0) + 1
        if item["tier"] == TIER_DEFAULT:
            summary["default_path_writes"].append(
                "%s:%d %s（%s）" % (item["file"], item["line"], item["kind"],
                                    item["trigger"]))
    return summary


def _row(item):
    trigger = item["trigger"]
    if item["declaration_note"]:
        trigger += "；声明注：%s" % item["declaration_note"]
    if item["declared"] and item["heuristic_tier"] != item["tier"]:
        trigger += "；启发式档位=%s（%s）" % (item["heuristic_tier"],
                                          item["heuristic_note"] or "无")
    return (item["file"], item["line"], item["kind"], trigger, item["tier"])


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="um-share 工具集写面静态审计（默认只读；不写文件、不联网）")
    parser.add_argument("--root", default=REPO_ROOT,
                        help="仓库根（默认按脚本位置推断）")
    parser.add_argument("--target", action="append", metavar="PATH",
                        help="被审脚本（仓库相对路径；可重复；默认 10 个已审计脚本）")
    parser.add_argument("--json", action="store_true",
                        help="以 JSON 打印机器可读的表")
    args = parser.parse_args(argv)

    targets = tuple(args.target) if args.target else TARGETS
    findings, reads = [], []
    for relpath in targets:
        scanned = scan_target(args.root, relpath)
        findings.extend(scanned["writes"])
        reads.extend(scanned["reads"])
    stale = stale_declarations(findings, targets)
    summary = _summary(findings, stale)
    exit_code = 0 if summary[TIER_DEFAULT] == 0 and summary[TIER_UNDECIDED] == 0 \
        and not stale else 2

    if args.json:
        print(json.dumps({
            "tool": "audit_tool_write_surface",
            "read_only": True,
            "targets": list(targets),
            "findings": findings,
            "read_only_actions": reads,
            "summary": summary,
            "exit_code": exit_code,
        }, ensure_ascii=False, indent=2))
        return exit_code

    print("== um-share 工具集写面静态审计（只读；不写文件、不联网） ==")
    print("被审脚本 %d 个；机械层发现写动作 %d 条；只读动作 %d 条"
          % (len(targets), len(findings), len(reads)))
    print("")
    print("脚本 | 行号 | 动作 | 触发条件 | 档位")
    for item in findings:
        print(" | ".join(str(part) for part in _row(item)))
    if stale:
        print("")
        print("声明过期（机械层找不到，声明表可能已不适用）：")
        for key in stale:
            print("  %s | %s | %s | 序号 %d" % key)
    print("")
    print("只读动作（档位 a；不改变持久状态）：")
    for item in reads:
        print("  %s:%d %s（%s）" % (item["file"], item["line"], item["kind"],
                                     item["function"] or "模块级"))
    print("")
    print("汇总：(a) %d / (b) %d / (c) %d / (?) %d；声明过期 %d"
          % (summary[TIER_READ_ONLY], summary[TIER_GATED],
             summary[TIER_DEFAULT], summary[TIER_UNDECIDED],
             summary["stale_declarations"]))
    for line in summary["default_path_writes"]:
        print("  [C] %s" % line)
    print("退出码 %d（0 = 只发现 (a)(b)；2 = 发现 (c)/未登记/声明过期）"
          % exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
