# -*- coding: utf-8 -*-
"""B0 (2026-11-01): the retired CLIProxy provider registration is gone.

The CLIProxy source is retired: its binding is not part of the default sync
assembly, an explicit scope naming it is refused, and the sync never reads its
controlled gateway key nor creates a provider for it. The historical rule-store
entries stay on disk untouched (the default assembly does not manage them).
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_sync import config_adapters as model_config_adapters  # noqa: E402
from model_sync import sync as model_sync  # noqa: E402
from model_sync.sync_core import Binding  # noqa: E402


CLIPROXY_BINDING = Binding("cliproxy", "ZCode", "local:cliproxy",
                           "Gemini（CLIProxy）", "（Gemini）")


class RetiredProviderRegistrationTests(unittest.TestCase):
    def test_retired_binding_is_not_active(self):
        self.assertNotIn(CLIPROXY_BINDING, model_sync.ACTIVE_BINDINGS)
        self.assertNotIn("cliproxy", model_sync.ACTIVE_SOURCES)

    def test_explicit_scope_is_refused(self):
        with mock.patch.object(model_sync, "collect_catalogs") as collect:
            with self.assertRaisesRegex(ValueError, "已暂停"):
                model_sync.run_sync(
                    apply=False, run_probes=False, confirm=False,
                    scope={"sources": ["cliproxy"]})
            collect.assert_not_called()

    def test_no_provider_is_registered_and_key_is_not_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "provider_config.json"
            store.write_text(
                '{"schemaVersion": 1, "config": {}}', encoding="utf-8")
            state = root / "state.json"
            state.write_text('{"version": 4}', encoding="utf-8")
            with mock.patch.object(model_sync, "ZCODE_PROVIDER_CONFIG", store), \
                    mock.patch.object(model_sync, "STATE_FILE", state), \
                    mock.patch.object(model_sync, "RESULT_FILE",
                                      root / "result.json"), \
                    mock.patch.object(model_sync, "PREVIEW_FILE",
                                      root / "preview.json"), \
                    mock.patch.object(model_sync, "HISTORY_FILE",
                                      root / "history.jsonl"), \
                    mock.patch.object(model_sync, "BACKUP_DIR",
                                      root / "backups"), \
                    mock.patch.object(model_sync, "collect_catalogs",
                                      return_value=({"um": []}, [])), \
                    mock.patch.object(
                        model_sync, "collect_consumers",
                        return_value=(
                            {}, "llm-pi-ai:\n  providers: {}\n",
                            '{"provider": {}}')), \
                    mock.patch.object(model_sync, "zcode_running",
                                      return_value=False):
                model_sync.run_sync(
                    apply=True, run_probes=False, confirm=False)
            store_doc = json.loads(store.read_text(encoding="utf-8"))
            providers = {
                rule.get("providerId") for rule in
                store_doc["config"]["providerConfigRules"]["providerRules"]}
            self.assertNotIn("local:cliproxy", providers)
            state_doc = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(state_doc["version"], 4)
            self.assertNotIn("cliproxy", {
                key.split("|", 1)[0]
                for key in state_doc.get("initializedBindings") or []})
            self.assertNotIn("cliproxy", {
                key.split("|", 1)[0]
                for key in state_doc.get("managed") or {}})
            # 退役来源的受控 Key 读取器与模板都已删除：没有任何路径会读它。
            self.assertFalse(
                hasattr(model_config_adapters, "CLIPROXY_API_KEY_FILE"))
            self.assertIsNone(
                model_config_adapters.zcode_managed_provider_template(
                    "local:cliproxy"))


if __name__ == "__main__":
    unittest.main()
