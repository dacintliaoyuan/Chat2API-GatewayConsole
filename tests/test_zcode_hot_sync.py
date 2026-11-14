# -*- coding: utf-8 -*-
"""ZCode 热同步已退役（Ticket #31）。

``zcode.hot_sync``/``zcode.hot_sync_protocol`` 以及它们专用的协议夹具随
ZCode 热同步包一起退役。这里断言模块与夹具都不再存在；保留功能（模型目录、
context probe、本机控制台）由其他回归覆盖。
"""
import importlib
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RETIRED_MODULES = (
    "zcode",
    "zcode.hot_sync",
    "zcode.hot_sync_protocol",
)

RETIRED_FIXTURES = (
    ROOT / "tests" / "zcode_protocol_fixture.py",
    ROOT / "tests" / "zcode_current_protocol_fixture.py",
)


class RetiredZCodeHotSyncTests(unittest.TestCase):
    def test_zcode_package_and_modules_are_gone(self):
        for name in RETIRED_MODULES:
            with self.subTest(module=name):
                try:
                    spec = importlib.util.find_spec(name)
                except ModuleNotFoundError:
                    spec = None
                self.assertIsNone(spec)
                with self.assertRaises(ModuleNotFoundError):
                    importlib.import_module(name)
        self.assertFalse(any((ROOT / "zcode").rglob("*.py")))

    def test_zcode_protocol_fixtures_are_gone(self):
        for path in RETIRED_FIXTURES:
            with self.subTest(path=path.name):
                self.assertFalse(path.exists())
                self.assertNotIn(
                    path.stem, sys.modules,
                    "%s was imported after retirement" % path.stem)


if __name__ == "__main__":
    unittest.main()
