# -*- coding: utf-8 -*-
"""`scripts/check.sh --quick` 的测试清单只能引用存在的测试模块。

退役任务会删除测试模块；如果门禁脚本的 ``--quick`` 清单没跟着更新，
``bash scripts/check.sh --quick`` 会在导入阶段报 ``ModuleNotFoundError``，
整波交付因此卡在门禁上（#34 删除 ``tests/test_share_chain_window.py`` 后即如此）。

本模块只读 ``scripts/check.sh`` 与 ``tests/`` 目录的文件名，不导入被引用的
测试模块（避免触发它们的模块级副作用），不跑其他测试、不写文件。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK_SCRIPT = ROOT / "scripts" / "check.sh"
TESTS_DIR = ROOT / "tests"

_MODULE_REF = re.compile(r"\btests\.([A-Za-z_][A-Za-z0-9_]*)")


def _quick_modules(text):
    """``--quick`` 分支里列出的 ``tests.<模块>`` 名（按出现顺序去重）。"""
    if "--quick)" not in text:
        raise AssertionError("scripts/check.sh 缺少 --quick 分支")
    block = text.split("--quick)", 1)[1].split(";;", 1)[0]
    seen = []
    for name in _MODULE_REF.findall(block):
        if name not in seen:
            seen.append(name)
    return seen


class QuickGateManifestTests(unittest.TestCase):
    def test_quick_gate_names_only_existing_test_modules(self):
        modules = _quick_modules(CHECK_SCRIPT.read_text(encoding="utf-8"))
        self.assertTrue(modules, "scripts/check.sh --quick 没有列出任何测试模块")
        missing = sorted(name for name in modules
                         if not (TESTS_DIR / (name + ".py")).is_file())
        self.assertEqual([], missing,
                         "scripts/check.sh --quick 引用了不存在的测试模块: "
                         + ", ".join(missing))


if __name__ == "__main__":
    unittest.main()
