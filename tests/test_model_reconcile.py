# -*- coding: utf-8 -*-
"""DSH/ZCode reconciliation is retired (Ticket #32).

The pure reconciliation rules served the retired consumer chain. The retained
``KNOWN_MODELS`` table now lives in ``model_sync.sync_core``; the reconcile
module itself is gone.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class RetiredReconcileTests(unittest.TestCase):
    def test_reconcile_module_is_gone(self):
        self.assertIsNone(importlib.util.find_spec("model_sync.reconcile"))

    def test_known_models_table_is_retained(self):
        from model_sync.sync_core import KNOWN_MODELS
        self.assertIn("DeepSeek-V4-Flash-0731", KNOWN_MODELS)
        for entry in KNOWN_MODELS.values():
            self.assertTrue(entry.get("efforts"))
            self.assertIn(entry.get("default"), entry["efforts"])


if __name__ == "__main__":
    unittest.main()
