# -*- coding: utf-8 -*-
"""ZCode 配置工具已退役（Ticket #31）。

``tools/zcode-config/keepset_apply.py`` 是 ZCode 供应商规则库的保留集
核对/应用工具；``model_sync/config_adapters.py`` 是它依赖的 ZCode 配置
适配器。两者都随 ZCode 热同步退役。这里断言工具、适配器和 import
边都不再存在。
"""
import importlib
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TOOL_DIR = ROOT / "tools" / "zcode-config"
TOOL = TOOL_DIR / "keepset_apply.py"
CONFIG_ADAPTERS = ROOT / "model_sync" / "config_adapters.py"


class RetiredZCodeConfigToolsTests(unittest.TestCase):
    def test_config_tool_is_gone(self):
        self.assertFalse(TOOL_DIR.exists())
        self.assertFalse(TOOL.exists())

    def test_zcode_config_adapter_is_gone(self):
        self.assertFalse(CONFIG_ADAPTERS.exists())
        self.assertIsNone(importlib.util.find_spec("model_sync.config_adapters"))
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("model_sync.config_adapters")

    def test_model_sync_no_longer_imports_zcode(self):
        offenders = []
        for path in sorted((ROOT / "model_sync").glob("*.py")):
            source = path.read_text(encoding="utf-8")
            if "from zcode" in source or "import zcode" in source:
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_zcode_package_is_gone(self):
        self.assertIsNone(importlib.util.find_spec("zcode"))
        self.assertFalse((ROOT / "zcode").exists())


if __name__ == "__main__":
    unittest.main()
