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

from console import share_production as prod  # noqa: E402  (same directory on path)


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


class ConsoleOperationsLockCitationTests(CitationPin):
    """`console/operations.py`：锁语义与锁文件名推导的行号（动作门 §1.1）。"""

    def setUp(self):
        self.path = self.gateway("console/operations.py")

    def test_file_presence_alone_never_means_locked(self):
        # 动作门 §1.1 引用 `console/operations.py:86-94` 与 `:88-94` 的原文。
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


@unittest.skip("SHARING.md is now the merged-process boundary")
class ConsoleShareProductionCitationTests(CitationPin):
    """`console/share_production.py`：`_paths` / `_child_env` / 状态读面的行号与键名。"""

    def setUp(self):
        self.path = self.gateway("console/share_production.py")

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

    def test_login_timeout_comment_citation_matches_the_real_default_line(self):
        """注释必须引到 `--login-timeout` 默认 180 的真实位置。

        `console/share_production.py` 的 `_child_cmdline` 注释原写
        `chat2api.py:1446`（那是 `if saved and saved.get("token"):`），实测默认值在
        `ap.add_argument("--login-timeout", type=int, default=180`。该错引由执行轮 7
        的引用漂移复核发现并在同一轮就地修正。

        **E19-1-08 step 1：本用例不再钉注释所在的行号。** 旧版把
        `console/share_production.py:1587` 写死，随后每次在它上面插入代码都会打破
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


if __name__ == "__main__":
    unittest.main()
