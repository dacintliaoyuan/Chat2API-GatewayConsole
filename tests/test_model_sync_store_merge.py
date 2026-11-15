# -*- coding: utf-8 -*-
"""The ZCode provider rule-store merge is retired (Ticket #32).

Rev11 merged managed scopes against the ZCode ``provider_config.json`` store.
That consumer chain is gone: the store merge, the consumer snapshot and the
ZCode file lock are no longer part of the retained surfaces.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_sync import sync as model_sync  # noqa: E402


class RetiredStoreMergeTests(unittest.TestCase):
    RETIRED_SYNC_SYMBOLS = (
        "_locked_store_merge", "collect_consumers",
        "render_provider_config_sync", "_zcode_rule_surface",
        "_zcode_store_snapshot",
    )

    def test_store_merge_helpers_are_gone(self):
        for name in self.RETIRED_SYNC_SYMBOLS:
            self.assertFalse(hasattr(model_sync, name), name)

    def test_sync_no_longer_imports_the_zcode_client(self):
        source = Path(model_sync.__file__).read_text(encoding="utf-8")
        self.assertNotIn("from zcode", source)
        self.assertNotIn("import zcode", source)

    def test_zcode_package_is_gone(self):
        self.assertIsNone(importlib.util.find_spec("zcode"))
        self.assertFalse(
            any((Path(__file__).resolve().parents[1] / "zcode").rglob("*.py")))

    def test_context_overrides_remain_state_only(self):
        from model_sync.sync_core import empty_state
        state = empty_state()
        self.assertEqual(state["contextOverrides"], {})
        self.assertNotIn("modelSettings", state)


if __name__ == "__main__":
    unittest.main()
