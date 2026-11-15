# -*- coding: utf-8 -*-
"""The retired CLIProxy provider registration is gone (Ticket #32).

The DSH/ZCode consumer chain (including its provider rule store and the
CLIProxy registration) is retired with the model sync actions. These tests assert
the sync assembly and the config adapters stay gone.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_sync import sync as model_sync  # noqa: E402


class RetiredProviderRegistrationTests(unittest.TestCase):
    def test_retired_sources_are_not_active(self):
        self.assertEqual(model_sync.ACTIVE_SOURCES, ("um",))
        for source in ("m365", "qoder_cn", "qoder_global", "cliproxy"):
            self.assertIn(source, model_sync.PAUSED_SOURCES)

    def test_retired_scope_is_refused_before_any_read(self):
        from unittest import mock
        with mock.patch.object(model_sync, "collect_catalogs") as collect:
            with self.assertRaisesRegex(ValueError, "已暂停"):
                model_sync._normalize_scope({"sources": ["cliproxy"]})
            collect.assert_not_called()

    def test_provider_registration_surface_is_gone(self):
        self.assertIsNone(importlib.util.find_spec("model_sync.config_adapters"))
        for name in ("run_sync", "render_provider_config_sync",
                     "_registered_provider_rule"):
            self.assertFalse(hasattr(model_sync, name), name)


if __name__ == "__main__":
    unittest.main()
