# -*- coding: utf-8 -*-
"""DSH/ZCode config adapters are retired (Ticket #32).

The configuration adapters and the reconciliation rules belonged to the retired
DSH/ZCode consumer chain. These tests assert the modules are gone; the retained
catalog and context surfaces live in ``model_sync.sync``.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class RetiredConfigAdapterTests(unittest.TestCase):
    RETIRED_MODULES = (
        "model_sync.config_adapters", "model_sync.reconcile",
    )

    def test_retired_modules_are_gone(self):
        for name in self.RETIRED_MODULES:
            self.assertIsNone(importlib.util.find_spec(name), name)

    def test_sync_no_longer_imports_client_adapters(self):
        from model_sync import sync as model_sync
        source = Path(model_sync.__file__).read_text(encoding="utf-8")
        for name in ("config_adapters", "render_dsh_sync",
                     "render_zcode_sync"):
            self.assertNotIn(name, source)

    def test_known_models_live_in_the_retained_core(self):
        from model_sync.sync_core import KNOWN_MODELS
        self.assertIn("DeepSeek-V4-Flash-0731", KNOWN_MODELS)


if __name__ == "__main__":
    unittest.main()
