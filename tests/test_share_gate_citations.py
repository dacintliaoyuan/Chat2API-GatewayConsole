# -*- coding: utf-8 -*-
"""承重引用的钉子：分享 Key 计量门的文档引用（执行轮 7，2026-09-27）。

背景：`output/ai-gw-round6-20260927/metering-action-gate.md`（精确动作门）、
`output/ai-gw-round4-20260926/load-checklist.md`（载入清单）、
`output/ai-gw-round5-20260926/recovery-order.md`（恢复顺序）里的 `文件:行号` 引用
已被复核端两次指出漂移。本模块把**落在已提交文件上**的承重引用钉成断言：
既断言内容确实存在（语义承重），也断言它就在文档引用的行号上（引用承重）。

* 断言读**实际文件**——仓库根由 `__file__` 定位，绝不硬编码副本；
* 只钉承重的那几条，不把整份文档搬进测试；
* 只读：不写 `data/`、不启动/停止任何进程、不发请求、不碰 git。

`output/` 是 gitignore 的（`SelfUseAI/.gitignore:75`），所以文档本身不在仓库里；
被引用的 `chat2api/*` 与 `SHARING.md` 在仓库里——本模块钉的是后者。

运行（工作目录 `ai-gateway/`）：
    python -B -m unittest tests.test_share_gate_citations -q
"""

import os
import re
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve()
GATEWAY_DIR = _HERE.parents[1]           # ai-gateway/
REPO_ROOT = GATEWAY_DIR.parent             # SelfUseAI/

sys.path.insert(0, str(GATEWAY_DIR))

import console_share_production as prod  # noqa: E402  (same directory on path)


def _lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def _line(path, number):
    """1-based line of ``path``; a missing file or line is a FAILURE, not a skip."""
    lines = _lines(path)
    if not 1 <= number <= len(lines):
        raise AssertionError(
            "%s:%d is outside the file (%d lines)" % (path, number, len(lines)))
    return lines[number - 1]


def _head_lines(rel_path):
    """按 HEAD 读仓库内文件的行；git 不可用 / 路径不在提交里返回 None。

    The cited line is now checked against the WORKING TREE (the content that will
    be committed). This helper is retained only for the historical
    "git/HEAD unavailable -> skip the line-level check" semantics; it no longer
    supplies the line compared by the assertion.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "show", "HEAD:" + rel_path],
            capture_output=True, text=True, encoding="utf-8", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.splitlines()


def _span(path, first, last):
    return "\n".join(_line(path, n) for n in range(first, last + 1))


def _block_from_symbol(path, symbol, *, tail=None):
    """The source block that STARTS at ``symbol`` (E19-1-08 step 1).

    Pure line-number citations rot on every unrelated insertion above them (this
    file alone recorded a dozen shifts). A citation that means "the block that
    defines this symbol" is anchored on the SYMBOL: the block runs from the line
    containing ``symbol`` to the next top-level ``def``/``class`` at the same or a
    lower indentation (or ``tail`` lines when given). A missing symbol is a
    FAILURE, never a skip.
    """
    lines = _lines(path)
    start = None
    for index, text in enumerate(lines):
        if symbol in text:
            start = index
            break
    if start is None:
        raise AssertionError("%s not found in %s" % (symbol, path))
    indent = len(lines[start]) - len(lines[start].lstrip())
    if tail is not None:
        return "\n".join(lines[start:start + tail])
    end = len(lines)
    for index in range(start + 1, len(lines)):
        text = lines[index]
        stripped = text.strip()
        if not stripped:
            continue
        current = len(text) - len(text.lstrip())
        if current <= indent and (stripped.startswith("def ")
                                 or stripped.startswith("class ")):
            end = index
            break
    return "\n".join(lines[start:end])


def _line_of_symbol(path, symbol):
    """1-based line number of the line containing ``symbol``.

    A missing symbol is a FAILURE, never a skip: the point of the anchor is
    that the symbol exists somewhere in the file.
    """
    for index, text in enumerate(_lines(path)):
        if symbol in text:
            return index + 1
    raise AssertionError("%s not found in %s" % (symbol, path))


class CitationPin(unittest.TestCase):
    """Shared helpers: every path resolves from `__file__`, never a copy."""

    def gateway(self, relative):
        path = GATEWAY_DIR / relative
        self.assertTrue(path.is_file(), "missing committed file: %s" % path)
        return path


class DeployElevatedCitationTests(CitationPin):
    """`deploy-elevated.ps1`：`run\\live` 的删除重建与 escjob 行号。

    动作门 §1.4 的更正说明：`:833` 注释把 `run/live` 删除重建引用更正为
    `deploy-elevated.ps1:820-822`，并说原文引用的 `:744-746` 是 escjob 目录。
    两侧都要钉住——否则更正说明本身会再次漂移。
    """

    def setUp(self):
        self.path = self.gateway("um-share-deploy/deploy-elevated.ps1")

    # The 2026-10-18 PS7 migration selector shifts the earlier pins by 13
    # lines; bounded watchdog readiness also shifts the later Stop block.

    def test_run_live_is_removed_and_recreated_at_the_cited_lines(self):
        span = _span(self.path, 845, 847)
        self.assertIn("$liveRun = Join-Path $RunRoot 'run\\live'", span)
        self.assertIn("Remove-Item -LiteralPath $liveRun -Recurse -Force", span)
        self.assertIn("New-Item -ItemType Directory -Path $liveRun -Force", span)

    def test_the_old_citation_744_746_is_the_escjob_region(self):
        span = _span(self.path, 768, 770)
        self.assertIn("umshare-escjob", span)

    def test_start_stage_precondition_is_at_the_cited_lines(self):
        # 动作门 §3.2：`Start` 前置要求 limits 在、watchdog Running，否则 throw。
        span = _span(self.path, 798, 809)
        self.assertIn("Test-LimitsInstalled", span)
        self.assertIn("'umshare-watchdog'", span)
        self.assertIn("throw", span)

    def test_stop_stage_removes_limits_and_watchdog_at_the_cited_lines(self):
        # 动作门 §3.2：`Stop` 会 `Remove-Limits` 并撤注册 watchdog 任务。
        span = _span(self.path, 1001, 1010)
        self.assertIn("Remove-Limits", span)
        self.assertIn("Unregister-ScheduledTask -TaskName 'umshare-watchdog'", span)

    def test_key_name_literal_matches_the_python_module(self):
        # 动作门 §5.2 表：`deploy-elevated.ps1` 的 key name 与
        # `console_share_production.SHARE_ATTRIBUTION_KEY_NAME` 必须同值。
        self.assertIn("$AttributionKeyName = '%s'"
                      % prod.SHARE_ATTRIBUTION_KEY_NAME,
                      _span(self.path, 71, 80))

    def test_deploy_script_own_comment_citation_is_recorded_as_known_drift(self):
        """已知漂移的两侧钉子（本线硬约束不得改 ai-gateway/ 生产文件）。

        `deploy-elevated.ps1:56` 的注释仍写 `run\\live` 的删除重建在 `:744-746`，
        而实测 `:744-746` 是 escjob、`:820-822` 才是 `run\\live`；
        `console_share_production.py:837` 已改为指向 `:820-822` ⇒ 两份注释互相矛盾。

        本用例把**两侧**都钉住：任一侧被修正（或注释再移动）时它先失败，
        提醒同步更新动作门 §1.4 与本断言——不让这条已知矛盾无声变化。

        （2026-10-04 Rev13-A/T：`console_share_production.py` 新增
        `production_connectivity_state` 并改写 `production_ts_service_control`
        后，生产侧那段注释从 `:857-863` 下移到 `:927-933`；按新行号同步。）
        """
        # E19-1-08 step 1: both sides are anchored on their SYMBOLS (a raw
        # line range rotted again when E19-1-03 added configure_paths). The
        # deploy script side is the comment block above its ACE assertion; the
        # production side is the comment block above the key-path resolver.
        self.assertIn(":744-746",
                      _block_from_symbol(
                          self.path, r"run\live", tail=8))
        prod_path = self.gateway("console_share_production.py")
        self.assertIn("deploy-elevated.ps1 :824-826",
                      _block_from_symbol(
                          prod_path, "explicit DENY ACE for um_share", tail=8))


class ConsoleOperationsLockCitationTests(CitationPin):
    """`console_operations.py`：锁语义与锁文件名推导的行号（动作门 §1.1）。"""

    def setUp(self):
        self.path = self.gateway("console_operations.py")

    def test_file_presence_alone_never_means_locked(self):
        # 动作门 §1.1 引用 `console_operations.py:86-94` 与 `:88-94` 的原文。
        sentence = "presence alone never means the resource is locked"
        for first, last in ((86, 94), (88, 94)):
            self.assertIn(sentence, _span(self.path, first, last))

    def test_lock_class_docstring_states_the_exclusive_handle_contract(self):
        # 同一处锁语义的另一半：「独占句柄即锁」。
        self.assertIn("Exclusive-handle file lock", _span(self.path, 84, 94))

    def test_lock_file_name_is_the_sanitized_resource_plus_lock(self):
        span = _span(self.path, 73, 101)
        self.assertIn("def _sanitize_resource(resource):", span)
        self.assertIn('_sanitize_resource(resource) + ".lock"', span)
        # 动作门 §1.1：`service:usage` → `service_usage.lock` 的字符映射。
        self.assertIn('ch in "_-"', span)


@unittest.skip("SHARING.md is now the merged-process boundary, not the HMAC recipe")
class SharingDocCitationTests(CitationPin):
    """`SHARING.md`：密钥路径常量与生成步骤的行号（动作门 §2.2、§2.4）。"""

    def setUp(self):
        self.path = self.gateway("SHARING.md")

    def test_generation_steps_are_at_the_cited_lines(self):
        # 动作门 §2.2 引用 `SHARING.md:51-100` 为「原文」，逐条比对关键步骤。
        span = _span(self.path, 51, 100)
        self.assertIn(
            "$keyPath = 'C:\\ProgramData\\UMShareRun\\run\\share-attribution.key'",
            span)
        self.assertIn("RandomNumberGenerator]::Create()", span)
        self.assertIn("$rng.GetBytes($bytes)", span)
        self.assertIn("[IO.File]::WriteAllText($keyPath, $key,", span)
        self.assertIn("icacls $keyPath /inheritance:r", span)
        self.assertIn("没有 Allow 读 ACE", span)

    def test_hard_constraints_are_at_the_cited_lines(self):
        # 动作门 §2.4：不得放 `ai-gateway/data`、不得放 `RunRoot\run\live`。
        self.assertIn("不要放 `ai-gateway/data`", _line(self.path, 22))
        self.assertIn("不要放 `RunRoot\\run\\live`", _line(self.path, 24))

    def test_acl_type_name_is_at_the_cited_line(self):
        # 动作门 §2.2 括注：⑤ 的类型名与 `SHARING.md:88` 一致。
        self.assertIn("System.Security.Principal.SecurityIdentifier",
                      _line(self.path, 88))

    def test_restart_instruction_is_at_the_cited_lines(self):
        # 动作门 §3.2：`SHARING.md:119-121` 写 **Prep → Watchdog → Start -Mode real**
        # （2026-09-28 执行轮 11 离线准备：维护者复审裁定「不得用 Stop → Start 做常规更新」
        # ——`Stop` 撤限制与 watchdog 任务 ⇒ `Start` fail-closed，故本钉从
        # 「Stop 再 Start」改为三段序列，并把禁令一起钉住）。
        span = _span(self.path, 119, 121)
        self.assertIn("-Stage Prep", span)
        self.assertIn("-Stage Watchdog", span)
        self.assertIn("-Stage Start -Mode real", span)
        ban = _span(self.path, 124, 130)
        self.assertIn("-Stage Stop", ban)
        self.assertIn("-Stage Start", ban)
        self.assertIn("fail-closed", ban)


@unittest.skip("SHARING.md is now the merged-process boundary")
class ConsoleShareProductionCitationTests(CitationPin):
    """`console_share_production.py`：`_paths` / `_child_env` / 状态读面的行号与键名。"""

    def setUp(self):
        self.path = self.gateway("console_share_production.py")

    # The fixed repair adapter adds 24 lines before these source anchors.

    def test_key_name_and_child_env_var_names(self):
        source = "\n".join(_lines(self.path))
        self.assertIn('SHARE_ATTRIBUTION_KEY_NAME = "%s"'
                      % prod.SHARE_ATTRIBUTION_KEY_NAME, source)
        self.assertIn('BACKEND_ATTRIBUTION_KEY_ENV = '
                      '"CHAT2API_SHARE_ATTRIBUTION_KEY_FILE"', source)

    def test_paths_and_child_env_are_at_the_cited_lines(self):
        # 动作门 §1.4 附录 A / §4.2 / 附录 C：`_paths()` 与 `_child_env`。
        # E19-1-08 step 1: this assertion used to pin a raw line range and
        # therefore rotted on every unrelated insertion above it (the comment
        # trail recorded a dozen shifts). It is now anchored on the SYMBOLS the
        # citation actually means: the block that defines `_paths`, the block that
        # defines `_child_env`, and the `--login-timeout` comment inside
        # `_child_cmdline`.
        paths = _block_from_symbol(self.path, "    def _paths(self):")
        self.assertIn('"attribution_key": share_attribution_key_path(),', paths)
        self.assertIn('"ledger": os.path.join(d, "usage-share-backend.jsonl"),',
                      paths)
        self.assertIn('"reauth_slot": os.path.join(d, "chat2api-reauth.state"),',
                      paths)
        child_env = _block_from_symbol(self.path,
                                        "    def _child_env(self, paths):")
        self.assertIn("env[BACKEND_ATTRIBUTION_KEY_ENV] = "
                      'paths["attribution_key"]', child_env)
        self.assertIn('env["CHAT2API_USAGE_LOG"] = paths["ledger"]', child_env)
        cmdline = _block_from_symbol(self.path,
                                      "    def _child_cmdline(self, paths):")
        self.assertIn("--login-timeout", cmdline)

    def test_key_path_comment_still_cites_the_run_live_lines(self):
        # 动作门 §1.4 更正说明：注释必须把 `run/live` 的删除重建
        # 指向 `deploy-elevated.ps1:820-822`（旧值 `744-746` 是 escjob 目录）。
        # E19-1-08 step 1: anchored on the SYMBOL, not on a raw line range —
        # the range moved again when E19-1-03 added configure_paths above it.
        comment = _block_from_symbol(
            self.path, "explicit DENY ACE for um_share", tail=8)
        self.assertIn("explicit DENY ACE for um_share", comment)
        self.assertIn("deploy-elevated.ps1 :824-826", comment)

    def test_login_timeout_comment_citation_matches_the_real_default_line(self):
        """注释必须引到 `--login-timeout` 默认 180 的真实位置。

        `console_share_production.py` 的 `_child_cmdline` 注释原写
        `chat2api.py:1446`（那是 `if saved and saved.get("token"):`），实测默认值在
        `ap.add_argument("--login-timeout", type=int, default=180`。该错引由执行轮 7
        的引用漂移复核发现并在同一轮就地修正。

        **E19-1-08 step 1：本用例不再钉注释所在的行号。** 旧版把
        `console_share_production.py:1587` 写死，随后每次在它上面插入代码都会打破
        断言（注释自己的历史里记录了 1215→1222→…→1587 的一串漂移）。现在改为
        按 SYMBOL 定位：找到 `def _child_cmdline` 块，断言块内确实写了
        `chat2api.py:<n>`；再核对 `chat2api.py` 的第 n 行确实是默认值行。

        `chat2api.py` 同时被另一条在途任务改写，所以：
        * 内容级断言「文件里确实有那两行内容」（引用的对象存在）；
        * 行号级准确性按**工作树**核对——工作树就是将要提交的内容；
        * `git show HEAD` 只保留历史跳过语义（git 不可用 → 跳过行号级核对）。
        """
        cmdline = _block_from_symbol(self.path,
                                      "    def _child_cmdline(self, paths):")
        self.assertIn("chat2api.py:", cmdline,
                      "注释里应引用 chat2api.py 的行号")
        chat2api = self.gateway("chat2api/chat2api.py")
        working = "\n".join(_lines(chat2api))
        self.assertIn('ap.add_argument("--login-timeout", type=int, default=180',
                      working, "工作树的 chat2api.py 里找不到 --login-timeout 默认值行")
        self.assertIn('if saved and saved.get("token"):', working,
                      "工作树的 chat2api.py 里找不到被原错引指向的那行")
        match = re.search(r"chat2api\.py:(\d+)", cmdline)
        self.assertIsNotNone(match, "注释里应写明 chat2api.py 的行号：%r" % cmdline)
        # HEAD availability is still probed only for the historical skip
        # semantics; the cited line itself is checked against the working tree,
        # which is the content that will be committed.
        if _head_lines("SelfUseAI/ai-gateway/chat2api/chat2api.py") is None:
            self.skipTest("git 不可用：跳过行号核对（内容级断言已通过）")
        number = int(match.group(1))
        working_lines = _lines(chat2api)
        if not 1 <= number <= len(working_lines):
            self.fail("注释引的 chat2api.py:%d 超出工作树文件范围" % number)
        self.assertIn('ap.add_argument("--login-timeout", type=int, default=180',
                      working_lines[number - 1],
                      "注释引的 chat2api.py:%d 在工作树上不是 --login-timeout 默认值行" % number)

    def test_attribution_read_face_is_a_seven_key_shape(self):
        # 动作门 §1.3：新代码（`08fdc96f` 起）的读面键集恰为 7 键，且旧
        # 的 `wired` 布尔已删除。用一个「不存在」的路径调用：文件级事实为
        # missing，因此不会跑进程探测（只读，无副作用）。
        missing = os.path.join(tempfile.gettempdir(),
                              "share-gate-citations-absent",
                              "share-attribution.key")
        self.assertFalse(os.path.exists(missing),
                         "fixture path unexpectedly exists: %s" % missing)
        out = prod.share_attribution_status(missing)
        self.assertEqual(sorted(out), [
            "file_ready", "file_reason", "instance_loaded", "key_chars",
            "key_file", "request_attribution_detail",
            "request_attribution_observed"])
        self.assertNotIn("wired", out)
        self.assertIs(out["file_ready"], False)
        self.assertEqual(out["file_reason"], "missing")
        self.assertIsNone(out["key_chars"])
        self.assertEqual(out["instance_loaded"]["value"], "unknown")
        self.assertEqual(out["instance_loaded"]["reason"], "key_file_not_ready")
        self.assertIs(out["request_attribution_observed"], False)
        self.assertEqual(out["request_attribution_detail"]["verdict"],
                         "not_observed_here")

    def test_existing_test_pins_the_same_seven_keys(self):
        # 动作门 §1.3 引用的正是 `tests/test_console_share_production.py:2617-2624`
        # （2026-09-28 随只读开关的 `import builtins` 下移一行；2026-09-29 执行轮 14
        # 的测试隔离修复再下移 16 行 → 2633-2640/2641；同日执行轮 15 的第三处
        # 隔离修复（DedicatedBackendOpsTests.setUp 中性化密钥路径覆盖开关）再下移 3 行
        # → 2636-2643/2644；本轮 TS 控制用例改走适配器公开动词并新增 2 条
        # 端到端用例后再下移 → 2682-2686/2688；2026-10-04 Rev13-A/T 的
        # 认证等待与连接状态用例新增后再下移 251 行 → 2933-2937/2939；
        # 2026-10-07 Rev19 监督段读面与夹具隔离用例新增后再下移 1 行
        # → 2934-2938/2940；2026-10-24 本轮新增测试后再下移 6 行
        # → 2940-2944/2946；同日分享网关稳定性测试新增后再下移
        # → 3010-3014/3016；2026-10-25 r2 测试隔离回归再下移
        # → 3072-3076/3078；2026-11-04 D15-4 新增 reauth slot 与
        # error/recovery 用例再下移 48 行 → 3120-3124/3126）。
        path = GATEWAY_DIR / "tests" / "test_console_share_production.py"
        # 2026-11-07 r18 的 W1/W3 改动再下移：七键断言现位于
        # 3292-3298（2026-11-07 工作树）；2026-11-07 E19-1-06 新增
        # BackendConfig 后再下移，七键断言现位于 3296-3300、assertNotIn 在 3302。
        span = _span(path, 3296, 3300)
        for key in ("file_ready", "file_reason", "instance_loaded", "key_chars",
                    "key_file", "request_attribution_detail",
                    "request_attribution_observed"):
            self.assertIn('"%s"' % key, span)
        self.assertIn('assertNotIn("wired", out)', _span(path, 3302, 3302))


@unittest.skip("HMAC share gateway retired with the merged share process")
class ShareGatewayCitationTests(CitationPin):
    """`share_entry.py`：502 码区域，以及被删除的错引 `:412`（round5 §8）。"""

    def setUp(self):
        self.path = self.gateway("chat2api/share_entry.py")

    def test_the_502_code_regions_are_at_the_cited_lines(self):
        # round5 §8「502 的三种码与 /v1/models 失败」的承重引用。
        # 行号在 2026-10-03（A9-3 resume 分支插入）后同步下移；
        # 2026-10-22 Harness 兼容（catalog limit + store=false）再下移；
        # 2026-10-23 Responses 翻译与 SDK beta 查询（模型目录 + Responses 上游）再下移；
        # 2026-10-23 Responses 字段/流式收尾修复（共享生成器）再下移。
        # 2026-10-23 新增 Responses -> Anthropic Messages 条件翻译（图片工具结果）
        # 后再下移。
        # 2026-10-27 分享网关在途改写（另一条线）再下移；
        # 2026-10-29 全局 128 并发（可调 TCP 闸 + load 安装容量）插入后再下移
        # → 2702-2709/2772-2779/2802-2805/2878-2881 变为
        # 2777-2784/2847-2854/2877-2880/2953-2956，按当前工作树同步。
        # 2026-10-30 Linux/云端适配在 load_attribution_key 前插入 POSIX 私有
        # 文件读取（+5 行）后再下移 → 2782-2789/2852-2859/2882-2885/2958-2961。
        # 2026-10-31 S1 撤销通知与调度整合再插入代码后再下移
        # → 3065-3072/3134-3141/3165-3168/3242-3245；同日持久化撤销
        # 状态（state 文件读写与启动装载）再插入代码后再下移
        # → 3318-3324/3390-3398/3420-3423/3497-3501。
        # 同日 S1 最终整合（后端角色判定、撤销通知发送边界、启动装载）
        # 再插入代码后再下移
        # → 3425-3431/3498-3506/3529-3532/3605-3609；同日收口回归修复时
        # `share_entry.py` 再插入代码（模型探测补上调度拒绝分支）后再下移
        # → 4037-4044/4117-4125/4148-4151/4224-4227，按当前工作树同步。
        # 2026-11-01 S2 预算守卫接线（`ShareServer.__init__` 注入与
        # 后台 egress 记账）再插入 341 行后再下移
        # → 4289-4296/4369-4377/4400-4403/4485-4488，按当前工作树同步。
        # 同日 S2 撤销围栏（`applied_security_generation` 与运行态 ACK
        # 水位记录）再插入 36 行后再下移
        # → 4325-4332/4405-4413/4436-4439/4521-4524，按当前工作树同步。
        # 2026-11-01 S2 拒绝回复记账改到发送前（`reply` 内 +7 行）后再下移
        # → 4332-4339/4412-4420/4443-4446/4528-4531，按当前工作树同步。
        # 同日 S2 预算守卫实现落位后再按当前工作树复核（净下移 5 行；
        # `--upstream-port` 引用处净下移 6 行）
        # → 4337-4344/4417-4425/4448-4451/4533-4536，按当前工作树同步。
        # 2026-11-03 S2 撤销/投影 generation 持久化与 legacy release 采纳
        # （`record_projection_generation`、启动合并、sibling 旧 release 搜索）
        # 再插入代码后再下移
        # → 4762-4771/4840-4852/4868-4877/4955-4963，按当前工作树同步。
        # 2026-11-03 S2 只读状态探测缓存（`_revoke_state_absent`）再插入 9 行
        # 后再下移
        # → 4770-4780/4850-4862/4878-4886/4966-4974，按当前工作树同步。
        # 2026-11-03 S2 legacy release 候选搜索同时覆盖 current 副本布局
        # （+5 行）后再下移
        # → 4775-4785/4855-4867/4883-4891/4971-4979，按当前工作树同步。
        # 2026-11-07 r18 的 W1/W3 改动（撤销取消在途、去 Qoder 过滤）
        # 后再下移 → 4391-4401/4671-4676/4753-4759/4780-4786/4862-4866，
        # 按当前工作树同步。
        # E19-1-08 steps 1-3: this assertion used to pin raw line ranges
        # (3356-3366/3666-3671/3748-3754/3780-3786/3858-3862) and its own
        # comment recorded that the first region had been failing since r18 W1.
        # SEMANTIC CORRECTION, not a line-number sync: the real contract for a
        # dispatch refusal is `_dispatch_refusal_error()`, which raises
        # share_dispatch_revoked / share_dispatch_timeout /
        # share_dispatch_state_unavailable with 429/503 + Retry-After. The
        # `um_unavailable` mapping belongs to the NON-dispatch upstream
        # failures. Both are pinned here by symbol.
        responses = _block_from_symbol(
            self.path, "if response.status != 200:", tail=24)
        self.assertIn("_relay_dispatch_refusal(response)", responses)
        self.assertIn('"um_unavailable", phase="upstream"', responses)
        self.assertIn('"unexpected_upstream_response"', responses)

        refusal = _block_from_symbol(
            self.path, "def _dispatch_refusal_error(")
        self.assertIn('"share_dispatch_revoked"', refusal)
        self.assertIn("retry_after=SHARE_DISPATCH_RETRY_AFTER_S", refusal)
        # The dispatch refusal type set is bounded and shared with the relay.
        types = _block_from_symbol(
            self.path, "SHARE_DISPATCH_ERROR_TYPES = frozenset({")
        for name in ("share_dispatch_timeout", "share_dispatch_revoked",
                      "share_dispatch_state_unavailable"):
            self.assertIn(name, types)

        catalog = _block_from_symbol(
            self.path, "upstream_catalog_freshness_unconfirmed")
        self.assertIn('"um_unavailable"', catalog)
        self.assertIn('"upstream_catalog_freshness_unconfirmed"', catalog)

        connection = _block_from_symbol(
            self.path, '"um_connection_failed", phase="upstream"')
        self.assertIn("_record_fault", connection)
        self.assertIn("stream_started=False", connection)

        # 上游端口必须显式指定（fail-closed），也是同一行的承重引用。
        self.assertIn("--upstream-port is required",
                      _block_from_symbol(
                          self.path,
                          "if args.upstream_port is None"))

    def test_line_412_is_not_a_502_code(self):
        # round5 §8 原引 `share_entry.py:412` 为 502 码，实测四个版本
        # （ea70f5d1/94533462/98d7315b/HEAD）都是 self.upstream_port 赋值
        # ⇒ 属从未对过的错引，已按复核从文档删除；本用例钉住该事实。
        # 2026-10-22 Harness 兼容与 Responses/SDK beta 查询共插入 296 行（523→819）；
        # 2026-10-23 共享 Responses 生成器与错误映射再插入 267 行（819→1086）；
        # 2026-10-23 分享网关改为原样转发后端 Responses 再下移 1 行（1086→1087）；
        # 2026-10-23 新增 Responses -> Anthropic Messages 条件翻译再下移 532 行（1087→1619）。
        # 2026-10-27 分享网关在途改写（另一条线）再下移；
        # 2026-10-29 全局 128 并发（可调 TCP 闸 + load 安装容量）再下移 50 行
        # （1666→1716），按当前工作树同步。
        # 2026-10-30 Linux/云端适配在 load_attribution_key 前插入 POSIX 私有
        # 文件读取（+5 行）后再下移（1716→1721）。
        # 2026-10-31 S1 撤销通知与调度整合再插入代码后再下移（1721→1882）；
        # 同日持久化撤销状态再插入代码后再下移（1882→2106）；
        # 同日 S1 最终整合（后端角色判定、撤销通知发送边界、启动装载）
        # 再插入代码后再下移（2106→2133）；2026-11-01 收口回归修复时
        # `share_entry.py` 再插入代码后再下移（2133→2388）；同日 S2 预算
        # 守卫接线在 `__init__` 签名再插入 1 行后再下移（2388→2389）；
        # 同日 S2 撤销围栏（`applied_security_generation` 与运行态 ACK
        # 水位记录）再插入 18 行后再下移（2389→2407）；同日 S2 预算守卫
        # 实现落位后再下移（2407→2412），按当前工作树同步。
        # 2026-11-03 S2 撤销/投影 generation 持久化与 legacy release 采纳
        # （`record_projection_generation`、启动合并、sibling 旧 release 搜索）
        # 再插入代码后再下移（2412→2730），按当前工作树同步。
        # 2026-11-03 S2 只读状态探测缓存（`_revoke_state_absent`）再插入
        # 9 行后再下移（2730→2739），按当前工作树同步。
        # 2026-11-03 S2 legacy release 候选搜索同时覆盖 current 副本布局
        # （+5 行）后再下移（2739→2744），按当前工作树同步。
        # 2026-11-07 E19-1-03 抽取后本文件在 1127 行之后整体下移 1035 行
        # （2743→1708），按当前工作树同步。
        # 2026-11-10：本次 import 注释与局部注入点各 +2 行（1708→1710），
        # 按当前工作树同步。
        self.assertIn("self.upstream_port = upstream_port", _line(self.path, 1879))


if __name__ == "__main__":
    unittest.main()
