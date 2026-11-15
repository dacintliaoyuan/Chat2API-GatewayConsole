# -*- coding: utf-8 -*-
"""Ticket #42：人工接管契约只有一个来源。

`login_contract.py` 定义前缀和原因集合；生产模块若重新手写字面量，就会在新增
原因时与共享契约漂移。这里做源码扫描：前缀字面量只允许出现在
`login_contract.py`，`usagesrv.py` 的原因分类只允许使用共享集合。
"""

import unittest
from pathlib import Path

import login_contract

ROOT = Path(__file__).resolve().parents[1]
PREFIX_LITERALS = ('"manual_takeover:', "'manual_takeover:")
PREFIX_PATHS = (
    ROOT / "chat2api" / "chat2api.py",
    ROOT / "console" / "um_login.py",
    ROOT / "usagesrv.py",
)


class ManualTakeoverPrefixSourceTests(unittest.TestCase):
    def test_production_modules_do_not_hardcode_the_prefix(self):
        for path in PREFIX_PATHS:
            text = path.read_text(encoding="utf-8")
            for literal in PREFIX_LITERALS:
                self.assertNotIn(
                    literal, text,
                    "%s 手写了人工接管前缀；请从 login_contract.py 导入"
                    % path.relative_to(ROOT).as_posix())


class ManualReasonSetTests(unittest.TestCase):
    def test_terminal_and_soft_reasons_are_disjoint(self):
        self.assertFalse(
            login_contract.LOGIN_TERMINAL_MANUAL_REASONS
            & login_contract.LOGIN_SOFT_MANUAL_REASONS)
        self.assertEqual(
            login_contract.LOGIN_SOFT_MANUAL_REASONS,
            frozenset(("duo_prompt_waiting",)))

    def test_display_classification_uses_the_shared_reason_set(self):
        text = (ROOT / "usagesrv.py").read_text(encoding="utf-8")
        self.assertIn("LOGIN_MANUAL_REASONS", text)
        for reason in login_contract.LOGIN_TERMINAL_MANUAL_REASONS:
            self.assertNotIn(
                '"%s"' % reason, text,
                "usagesrv.py 重新手写了原因 %s；请使用 LOGIN_MANUAL_REASONS"
                % reason)
