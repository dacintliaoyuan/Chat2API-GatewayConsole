# -*- coding: utf-8 -*-
"""Retained model_sync surfaces (Ticket #32).

The DSH/ZCode consumer chain and the model sync/settings/task actions were
retired.  This module covers the retained catalog/state/probe helpers and asserts
the retired consumers/actions/scripts are gone.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_sync import sync as model_sync  # noqa: E402
from model_sync.sync_core import CatalogModel, empty_state  # noqa: E402


class CatalogTests(unittest.TestCase):
    def test_retired_sources_are_refused_before_any_read(self):
        with mock.patch.object(model_sync, "_get_json") as get:
            for source in ("m365", "qoder_cn", "qoder_global", "cliproxy"):
                with self.subTest(source=source):
                    with self.assertRaisesRegex(ValueError, "退役来源"):
                        model_sync.collect_catalogs([source])
        get.assert_not_called()

    def test_um_catalog_shapes_models_and_warnings(self):
        payload = {"data": [
            {"id": "DeepSeek-V4-Flash-0731", "max_model_len": 128000},
            {"id": "model-Fast", "max_model_len": 64000},
        ]}
        with mock.patch.object(model_sync, "_get_json", return_value=payload):
            catalogs, warnings = model_sync.collect_catalogs()
        self.assertEqual(warnings, [])
        self.assertEqual([model.model_id for model in catalogs["um"]],
                         ["DeepSeek-V4-Flash-0731"])
        model = catalogs["um"][0]
        self.assertEqual(model.context, 128000)
        self.assertEqual(model.efforts, ("low", "medium", "high", "max"))
        self.assertEqual(model.efforts_source, "known")

    def test_single_source_failure_lands_in_warnings(self):
        with mock.patch.object(model_sync, "_get_json",
                               side_effect=OSError("offline")):
            catalogs, warnings = model_sync.collect_catalogs()
        self.assertEqual(catalogs, {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("UM目录不可用", warnings[0])

    def test_unknown_sources_are_refused(self):
        with self.assertRaises(ValueError):
            model_sync.collect_catalogs(["bogus"])


class StateTests(unittest.TestCase):
    def test_state_load_is_isolated_and_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            state_file = data / "model-sync-state.json"
            state_file.write_text(json.dumps({
                "version": 2, "pinned": ["old"], "probes": {},
                "contextOverrides": {"um|a": {
                    "context": 200000, "set_at": "t",
                    "source": "manual"}}}, ensure_ascii=False),
                encoding="utf-8")
            with mock.patch.object(model_sync, "DATA_DIR", data), \
                 mock.patch.object(model_sync, "STATE_FILE", state_file):
                state = model_sync.load_state()
        self.assertEqual(state["version"], 4)
        self.assertNotIn("pinned", state)
        self.assertEqual(state["contextOverrides"]["um|a"]["context"], 200000)

    def test_absent_state_is_a_legal_empty_document(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            with mock.patch.object(model_sync, "DATA_DIR", data), \
                 mock.patch.object(model_sync, "STATE_FILE",
                                   data / "model-sync-state.json"):
                self.assertEqual(model_sync.load_state(), empty_state())


class ProbeQueueTests(unittest.TestCase):
    def test_pending_probe_is_verified_by_the_real_ladder(self):
        records = {"um|m": {
            "source": "um", "model_id": "m", "declared_context": 128000,
            "status": "pending", "observed_floor": None, "attempts": 0,
            "last_run": None, "last_error": None}}
        calls = []

        def post(url, body, timeout=90, token=None):
            content = body["messages"][0]["content"]
            marker = content.rsplit("[ctxprobe ", 1)[1].split("]", 1)[0]
            calls.append(len(content))
            return {"status": 200, "body": {"choices": [{"message": {
                "content": "[ctxprobe %s] FINAL anchor — quote this line "
                           "verbatim" % marker}}]}}

        outcome = model_sync.probe_context_queue(
            {"um": ("http://127.0.0.1:1/v1/chat/completions", None)},
            {"max_requests": 20, "max_tokens": 64}, records,
            lambda: True, post_json=post)
        self.assertEqual(outcome["used"], len(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(records["um|m"]["status"], "verified")
        self.assertEqual(records["um|m"]["observed_floor"],
                         max(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(calls, sorted(calls))


class RetiredConsumerAndSyncSurfaceTests(unittest.TestCase):
    RETIRED = (
        "collect_consumers", "KNOWN_CONSUMERS", "run_sync",
        "render_provider_config_sync", "render_dsh_sync",
        "render_zcode_sync", "probe_new_models", "main",
    )
    RETIRED_MODULES = ("model_sync.config_adapters", "model_sync.reconcile")

    def test_consumer_collection_and_sync_helpers_are_gone(self):
        for name in self.RETIRED:
            self.assertFalse(hasattr(model_sync, name), name)

    def test_retired_modules_are_gone(self):
        import importlib.util
        for name in self.RETIRED_MODULES:
            self.assertIsNone(importlib.util.find_spec(name), name)


if __name__ == "__main__":
    unittest.main()
