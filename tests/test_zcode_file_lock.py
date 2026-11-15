# -*- coding: utf-8 -*-
"""ZCode 文件锁已退役（Ticket #31）。

Rev11 的 ``zcode.file_lock`` 实现了 ZCode 客户端兼容的锁协议。ZCode
热同步包退役后，该模块与整个 ``zcode`` 包都不应再存在；这里断言退役面
保持不存在，而不是继续执行已删除的锁实现。
"""
import importlib
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class RetiredZCodeFileLockTests(unittest.TestCase):
    def test_zcode_package_is_gone(self):
        self.assertIsNone(importlib.util.find_spec("zcode"))
        self.assertFalse(any((ROOT / "zcode").rglob("*.py")))

    def test_zcode_file_lock_module_is_gone(self):
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("zcode.file_lock")
        self.assertFalse((ROOT / "zcode" / "file_lock.py").exists())


if __name__ == "__main__":
    unittest.main()
