# -*- coding: utf-8 -*-
"""Retained model_sync core primitives (Ticket #32).

The DSH/ZCode mirror planning was retired with the consumer chain.  This module
covers only the catalog/state primitives the retained surfaces still use, and
asserts the planning surface is gone.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_sync.sync_core import (  # noqa: E402
    KNOWN_MODELS,
    STATE_VERSION,
    VARIANT_SUFFIXES,
    CatalogModel,
    empty_state,
    migrate_state,
    validate_catalog,
)


class CatalogStateTests(unittest.TestCase):
    def test_empty_state_uses_current_schema(self):
        state = empty_state()
        self.assertEqual(state["version"], STATE_VERSION)
        self.assertNotIn("pinned", state)
        self.assertIsInstance(state["contextProbes"], dict)
        self.assertIsInstance(state["contextOverrides"], dict)

    def test_migrate_drops_only_the_retired_pin_list(self):
        legacy = {
            "version": 2,
            "initializedBindings": ["um|DSH|openwebui"],
            "managed": {"um|DSH|openwebui": {"model-a": {"profile": {}}}},
            "pinned": ["um|DSH|openwebui|model-a"],
            "catalogCounts": {"um": 7},
            "pending": ["ZCode 发布 model-b"],
            "probes": {"um|model-a": {"status": "ok"}},
            "retirementWatch": {},
            "contextProbes": {},
            "contextOverrides": {"um|model-a": {
                "context": 200000, "set_at": "t", "source": "manual"}},
        }
        migrated = migrate_state(dict(legacy))
        self.assertEqual(migrated["version"], STATE_VERSION)
        self.assertNotIn("pinned", migrated)
        for key, value in legacy.items():
            if key in ("version", "pinned"):
                continue
            self.assertEqual(migrated[key], value, key)

    def test_migrate_is_idempotent_and_handles_v1(self):
        v1 = {"version": 1, "pending": [], "pinned": ["old"]}
        migrated = migrate_state(dict(v1))
        self.assertEqual(migrated["version"], STATE_VERSION)
        self.assertNotIn("pinned", migrated)
        self.assertEqual(migrated["probes"], {})
        self.assertEqual(migrated, migrate_state(dict(migrated)))


class CatalogModelTests(unittest.TestCase):
    def test_model_carries_declaration_and_effort_evidence(self):
        model = CatalogModel(
            "um", "model-a", context=128000,
            efforts=("low", "high"), default_effort="high",
            efforts_source="upstream", display_name="A")
        self.assertEqual(model.model_id, "model-a")
        self.assertEqual(model.context, 128000)
        self.assertEqual(model.efforts, ("low", "high"))
        self.assertEqual(model.efforts_source, "upstream")

    def test_known_efforts_are_read_from_the_local_table(self):
        self.assertIn("DeepSeek-V4-Flash-0731", KNOWN_MODELS)
        self.assertEqual(
            KNOWN_MODELS["DeepSeek-V4-Flash-0731"]["default"], "high")

    def test_variant_suffixes_mark_virtual_models(self):
        self.assertFalse("model-a".endswith(VARIANT_SUFFIXES))
        self.assertTrue("model-a-Fast".endswith(VARIANT_SUFFIXES))


class ValidateCatalogTests(unittest.TestCase):
    def test_empty_catalog_is_not_authoritative(self):
        ok, warning = validate_catalog("um", [])
        self.assertFalse(ok)
        self.assertIn("为空", warning)

    def test_empty_and_duplicate_ids_are_rejected(self):
        for models in (
                [CatalogModel("um", "")],
                [CatalogModel("um", "a"), CatalogModel("um", "a")]):
            with self.subTest(models=models):
                ok, warning = validate_catalog("um", models)
                self.assertFalse(ok)
                self.assertTrue(warning)

    def test_drop_over_half_is_rejected(self):
        models = [CatalogModel("um", "only")]
        ok, warning = validate_catalog("um", models, previous_count=3)
        self.assertFalse(ok)
        self.assertIn("超过 50%", warning)

    def test_exact_half_drop_is_allowed(self):
        models = [CatalogModel("um", "a"), CatalogModel("um", "b")]
        ok, _ = validate_catalog("um", models, previous_count=4)
        self.assertTrue(ok)


class RetiredPlanningSurfaceTests(unittest.TestCase):
    RETIRED = (
        "Binding", "SyncChange", "SyncPlan", "DEFAULT_BINDINGS",
        "RULE_SURFACE_KEY", "rule_surface_ids", "plan_catalog_mirror",
        "client_context_of", "context_publish_action", "published_context",
        "published_record", "MIN_RETIREMENT_RECHECK_SECONDS",
    )

    def test_retired_planning_symbols_are_gone(self):
        import model_sync.sync_core as core
        for name in self.RETIRED:
            self.assertFalse(hasattr(core, name), name)


if __name__ == "__main__":
    unittest.main()
