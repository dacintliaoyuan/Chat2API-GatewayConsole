# -*- coding: utf-8 -*-
"""Retired model-settings publication (Ticket #32).

The settings draft/apply chain belonged to the retired client synchronization.
These tests assert its actions and handlers stay gone; the retained context
overrides remain the only state-only model settings surface.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from console import adapters as console_adapters  # noqa: E402
from console import model_actions as console_model_actions  # noqa: E402
from model_sync import sync as model_sync  # noqa: E402

RETIRED_ACTIONS = (
    "model.settings_save", "model.settings_preview", "model.settings_apply",
)
RETIRED_HANDLERS = (
    "model_settings_save", "model_settings_preview", "model_settings_apply",
)
RETIRED_HELPERS = (
    "save_model_setting_draft", "promote_model_setting_drafts",
    "pending_model_setting_versions", "held_model_setting_keys",
    "_model_settings_of_state", "_validate_setting_values",
    "_settings_preview_binding", "_project_setting_drafts",
)


class RetiredSettingsSurfaceTests(unittest.TestCase):
    def test_settings_actions_are_not_registered(self):
        specs = {spec.name for spec in console_adapters.build_registry()}
        for action in RETIRED_ACTIONS:
            self.assertNotIn(action, specs)

    def test_settings_handlers_are_gone(self):
        for name in RETIRED_HANDLERS:
            self.assertFalse(hasattr(console_model_actions, name), name)

    def test_settings_draft_helpers_are_gone(self):
        for name in RETIRED_HELPERS:
            self.assertFalse(hasattr(model_sync, name), name)

    def test_context_overrides_remain_the_settings_surface(self):
        specs = {spec.name: spec for spec in console_adapters.build_registry()}
        for kept in ("model.context_overrides", "model.context_override_set",
                     "model.context_override_clear"):
            self.assertIn(kept, specs)
            self.assertTrue(specs[kept].enabled, kept)


if __name__ == "__main__":
    unittest.main()
