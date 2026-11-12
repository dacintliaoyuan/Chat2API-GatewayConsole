"""Settings publication regressions using real adapters and private client files."""
import copy
import json
import unittest
from unittest import mock

try:  # `python -m unittest tests.<module>` from ai-gateway/
    from tests.test_console_model_actions import (
        temp_sync_env, _aligned_store, _Ctx)
except ImportError:  # `discover -s ai-gateway/tests` (top-level module names)
    from test_console_model_actions import temp_sync_env, _aligned_store, _Ctx
import console_model_actions as actions
import model_sync as sync
from model_sync_core import CatalogModel
from console_operations import OperationError
from console_operations import OperationCancelled, OperationManager
import console_adapters


class SettingsPublicationTests(unittest.TestCase):
    key = "um|model-a"
    catalogs = {"um": [CatalogModel("um", "model-a", context=128000)]}

    def test_local_settings_read_never_collects_upstream_and_preserves_drafts(self):
        state = self.state()
        with temp_sync_env(state_text=json.dumps(state)) as paths, \
             mock.patch.object(sync, "collect_catalogs", side_effect=AssertionError("upstream")), \
             mock.patch.object(sync, "collect_consumers", return_value=({}, None, None)):
            before = paths["STATE_FILE"].read_bytes()
            rows = actions.model_context_overrides("", {
                "local_only": True, "models": {"um": ["model-a", "new-model"]}}, _Ctx())["overrides"]
            self.assertEqual(paths["STATE_FILE"].read_bytes(), before)
        by_id = {row["model_id"]: row for row in rows}
        self.assertEqual(by_id["model-a"]["settings_draft"]["contextTokens"], 64000)
        self.assertIsNone(by_id["new-model"]["declared_context"])
        self.assertEqual(by_id["new-model"]["settings_draft_version"], 0)

    def test_local_settings_identity_input_cannot_forge_declarations(self):
        with self.assertRaises(OperationError):
            actions.model_context_overrides("", {"local_only": True,
                "models": {"um": [{"id": "model-a", "context": 999999}]}}, _Ctx())

    def test_plain_sync_preview_binds_inputs_and_rejects_changed_client(self):
        state = sync.empty_state()
        with temp_sync_env(state_text=json.dumps(state)) as paths, \
             mock.patch.object(sync, "collect_catalogs", return_value=(self.catalogs, [])), \
             mock.patch.object(sync, "zcode_running", return_value=False):
            scope = {"scope_mode": "selected", "sources": ["um"], "consumers": ["ZCode"]}
            paths["ZCODE_PROVIDER_CONFIG"].write_text(json.dumps(_aligned_store(self.catalogs)), encoding="utf-8")
            result = actions.model_sync_preview("", scope, _Ctx())
            self.assertRegex(result["preview_token"], r"^[0-9a-f]{64}$")
            before = paths["STATE_FILE"].read_bytes()
            client = json.loads(paths["ZCODE_JSON"].read_text(encoding="utf-8"))
            client["extra"] = "changed after preview"
            paths["ZCODE_JSON"].write_text(json.dumps(client), encoding="utf-8")
            with self.assertRaises(OperationError) as caught:
                actions.model_sync_apply("", dict(scope, preview_token=result["preview_token"]), _Ctx())
            self.assertEqual(caught.exception.code, "version_conflict")
            self.assertEqual(paths["STATE_FILE"].read_bytes(), before)

    def test_plain_sync_can_publish_its_unchanged_preview(self):
        with temp_sync_env(state_text=json.dumps(sync.empty_state())) as paths, \
             mock.patch.object(sync, "collect_catalogs", return_value=(self.catalogs, [])), \
             mock.patch.object(sync, "zcode_running", return_value=False):
            scope = {"scope_mode": "selected", "sources": ["um"], "consumers": ["ZCode"]}
            paths["ZCODE_PROVIDER_CONFIG"].write_text(json.dumps(_aligned_store(self.catalogs)), encoding="utf-8")
            preview = actions.model_sync_preview("", scope, _Ctx())
            result = actions.model_sync_apply("", dict(scope, preview_token=preview["preview_token"]), _Ctx())
            self.assertNotEqual(result.get("status"), "failed")

    def state(self):
        state = sync.empty_state()
        state["modelSettings"] = {self.key: {
            "draft": {"alias": "new", "contextSource": "manual",
                      "contextTokens": 64000, "defaultEffort": None},
            "draftVersion": 2, "draftSavedAt": "draft-time",
            "applied": {"alias": "old", "contextSource": "manual",
                        "contextTokens": 32000, "defaultEffort": None},
            "appliedVersion": 1, "appliedAt": "old-time",
            "appliedEvidence": "applied"}}
        return state

    def test_explicit_empty_selection_does_not_promote_everything(self):
        state = self.state()
        before = copy.deepcopy(state)
        self.assertEqual(sync.promote_model_setting_drafts(state, {})[0], [])
        self.assertEqual(state, before)

    def test_partial_alias_update_preserves_applied_settings(self):
        state = self.state()
        state["modelSettings"][self.key]["draft"] = None
        with temp_sync_env(state_text=json.dumps(state)):
            saved = sync.save_model_setting_draft(
                self.catalogs, "um", "model-a", {"alias": "renamed"}, 2)
        self.assertEqual(saved["draft"]["contextTokens"], 32000)
        self.assertEqual(saved["draft"]["contextSource"], "manual")
        self.assertEqual(saved["applied"]["alias"], "old")

    def test_unknown_effort_is_rejected_when_catalog_has_no_evidence(self):
        with temp_sync_env():
            with self.assertRaises(ValueError):
                sync.save_model_setting_draft(
                    self.catalogs, "um", "model-a", {"defaultEffort": "max"})

    def test_measured_value_requires_token_evidence_not_characters(self):
        state = self.state()
        state["contextProbes"] = {self.key: {
            "source": "um", "model_id": "model-a", "status": "verified",
            "declared_context": 128000, "scheme_revision": sync.PROBE_SCHEME_REVISION,
            "evidence_revision": 3, "observed_floor": 1000000,
            "token_evidence": 16000}}
        with temp_sync_env(state_text=json.dumps(state)):
            with self.assertRaises(ValueError):
                sync.save_model_setting_draft(self.catalogs, "um", "model-a",
                    {"contextSource": "measured", "contextTokens": 1000000})
            saved = sync.save_model_setting_draft(self.catalogs, "um", "model-a",
                    {"contextSource": "measured", "contextTokens": 16000})
        self.assertEqual(saved["draftEvidence"]["context"]["revision"], 3)

    def test_existing_applied_record_with_new_draft_is_pending(self):
        self.assertEqual(sync.pending_model_setting_versions(self.state()),
                         {self.key: 2})

    def test_actual_readback_is_separate_for_each_client_and_missing_values(self):
        state = self.state()
        state["modelSettings"][self.key]["applied"]["defaultEffort"] = "high"
        clients = {binding.key: {"models": {"model-a": (
            {"name": "old" + binding.suffix, "limit": {"context": 32000, "output": 0},
             "reasoning": {"defaultVariant": "low"}} if binding.consumer == "ZCode" else
            {"name": "old" + binding.suffix, "contextWindow": 16000, "maxTokens": 64})}}
            for binding in sync.ACTIVE_BINDINGS if binding.source == "um"}
        row = sync.context_override_surface(state, self.catalogs, clients)[0]
        by_client = {item["consumer"]: item for item in row["client_readbacks"]}
        self.assertFalse(by_client["ZCode"]["matchesApplied"]["defaultEffort"])
        self.assertFalse(by_client["DSH"]["matchesApplied"]["context"])
        self.assertEqual(row["client_alias"], "old")
        self.assertEqual(row["client_default_effort"], "low")
        self.assertIsNone(row["client_output_token_cap"]["value"])
        for snapshot, expected in (({}, "absent"), (None, "unavailable")):
            row = sync.context_override_surface(state, self.catalogs, snapshot)[0]
            self.assertEqual(row["client_readback_state"], expected)
            self.assertTrue(all(item["state"] == expected for item in row["client_readbacks"]))
            self.assertIsNone(row["client_context"])
            self.assertIsNone(row["client_matches_applied"])

    def test_probe_history_is_bounded_and_does_not_expose_extra_fields(self):
        state = sync.empty_state()
        state["contextProbes"] = {self.key: {"source": "um", "model_id": "model-a",
            "declared_context": 128000, "status": "verified",
            "history": [{"evidence_revision": index, "observed_floor": 1000,
                "token_evidence": None, "reason": "retest", "private_token": "secret"}
                for index in range(sync.PROBE_HISTORY_MAX + 3)]}}
        with temp_sync_env(state_text=json.dumps(state)), mock.patch.object(
                sync, "endpoint_credential", return_value=("", None, False)):
            row = actions.model_context_probe_preview("", {}, _Ctx())["records"][0]
        self.assertEqual(len(row["history"]), sync.PROBE_HISTORY_MAX)
        self.assertEqual(row["history"][0]["evidence_revision"], 3)
        self.assertIsNone(row["history"][0]["token_evidence"])
        self.assertNotIn("private_token", row["history"][0])

    def test_changed_measurement_generation_invalidates_saved_draft(self):
        state = sync.empty_state()
        record = {"source": "um", "model_id": "model-a", "status": "verified",
            "declared_context": 128000, "scheme_revision": sync.PROBE_SCHEME_REVISION,
            "evidence_revision": 3, "token_evidence": 16000}
        state["contextProbes"] = {self.key: record}
        with temp_sync_env(state_text=json.dumps(state)), mock.patch.object(
                sync, "collect_catalogs", return_value=(self.catalogs, [])):
            saved = sync.save_model_setting_draft(self.catalogs, "um", "model-a",
                {"contextSource": "measured", "contextTokens": 16000})
            changed = sync.load_state()
            changed["contextProbes"][self.key]["evidence_revision"] = 4
            sync.STATE_FILE.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaises(OperationError) as caught:
                actions.model_settings_preview("", {"selection": {self.key: saved["draftVersion"]}}, _Ctx())
            self.assertEqual(caught.exception.code, "version_conflict")

    def test_public_schema_accepts_explicit_restoration_nulls(self):
        spec = next(s for s in console_adapters.build_registry()
                    if s.name == "model.settings_save")
        values = {"alias": None, "contextSource": None,
                  "contextTokens": None, "defaultEffort": None}
        self.assertEqual(OperationManager._validate_params(None, spec, values), values)

    def test_cancel_during_catalog_read_prevents_draft_write(self):
        calls = 0
        def cancelled():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise OperationCancelled()
        ctx = mock.Mock(raise_if_cancelled=cancelled)
        with temp_sync_env() as paths, mock.patch.object(
                sync, "collect_catalogs", return_value=(self.catalogs, [])):
            with self.assertRaises(OperationCancelled):
                actions.model_settings_save("", {"source": "um", "model_id": "model-a",
                    "alias": "cancelled"}, ctx)
            self.assertFalse(paths["STATE_FILE"].exists())

    def test_cancel_before_publication_is_not_misreported_as_failure(self):
        state = self.state()
        with temp_sync_env(state_text=json.dumps(state)) as paths, \
             mock.patch.object(sync, "collect_catalogs", return_value=(self.catalogs, [])), \
             mock.patch.object(sync, "zcode_running", return_value=False):
            paths["ZCODE_PROVIDER_CONFIG"].write_text(
                json.dumps(_aligned_store(self.catalogs)), encoding="utf-8")
            selection = {self.key: 2}
            preview = actions.model_settings_preview("", {"selection": selection}, _Ctx())
            ctx = mock.Mock()
            ctx.raise_if_cancelled.side_effect = [None, OperationCancelled()]
            with self.assertRaises(OperationCancelled):
                actions.model_settings_apply("", dict(selection=selection,
                    preview_token=preview["preview_token"]), ctx)
            self.assertEqual(json.loads(paths["STATE_FILE"].read_text("utf-8")), state)

    def _round(self, paths, state, *, running=False, mutate=None):
        paths["ZCODE_PROVIDER_CONFIG"].write_text(
            json.dumps(_aligned_store(self.catalogs)), encoding="utf-8")
        with mock.patch.object(sync, "collect_catalogs", return_value=(self.catalogs, [])), \
             mock.patch.object(sync, "zcode_running", return_value=running), \
             mock.patch.object(sync, "zcode_hot_sync_admission",
                                return_value=(False, "测试：未准入")), \
             mock.patch.object(sync, "_get_json", side_effect=AssertionError("network")):
            params = {"selection": {self.key: 2}}
            before = {name: paths[name].read_bytes() for name in (
                "STATE_FILE", "DSH_YAML", "ZCODE_JSON", "ZCODE_PROVIDER_CONFIG")}
            preview = actions.model_settings_preview("", params, _Ctx())
            self.assertEqual(preview["changes"][0]["draft"]["contextTokens"], 64000)
            self.assertEqual(before, {name: paths[name].read_bytes() for name in before})
            if mutate:
                mutate(paths)
            result = actions.model_settings_apply("", dict(params,
                preview_token=preview["preview_token"]), _Ctx())
            return result, before

    def test_preview_and_apply_share_draft_and_one_transaction(self):
        state = self.state()
        with temp_sync_env(state_text=json.dumps(state)) as paths:
            real = sync.apply_config_transaction
            with mock.patch.object(sync, "apply_config_transaction", wraps=real) as tx:
                result, _ = self._round(paths, state)
            after = json.loads(paths["STATE_FILE"].read_text("utf-8"))
            self.assertEqual(tx.call_count, 1)
            self.assertEqual(result["applied"], [self.key])
            self.assertIsNone(after["modelSettings"][self.key]["draft"])
            self.assertIn("64000", paths["DSH_YAML"].read_text("utf-8"))

    def test_changed_client_snapshot_invalidates_preview_without_writes(self):
        state = self.state()
        def changed(paths):
            raw = json.loads(paths["ZCODE_JSON"].read_text("utf-8"))
            raw["externalEdit"] = True
            paths["ZCODE_JSON"].write_text(json.dumps(raw), encoding="utf-8")
        with temp_sync_env(state_text=json.dumps(state)) as paths:
            with self.assertRaises(OperationError) as caught:
                self._round(paths, state, mutate=changed)
            self.assertEqual(caught.exception.code, "version_conflict")
            self.assertEqual(json.loads(paths["STATE_FILE"].read_text("utf-8")), state)

    def test_deferral_preserves_old_applied_and_new_draft_for_all_clients(self):
        state = self.state()
        with temp_sync_env(state_text=json.dumps(state)) as paths:
            real = sync.apply_config_transaction
            with mock.patch.object(sync, "apply_config_transaction", wraps=real) as tx:
                result, before = self._round(paths, state, running=True)
            after = json.loads(paths["STATE_FILE"].read_text("utf-8"))
            self.assertEqual(tx.call_count, 1)
            self.assertEqual(result["applied"], [])
            self.assertEqual(result["deferred"], [self.key])
            self.assertEqual(after["modelSettings"], state["modelSettings"])
            for name in ("DSH_YAML", "ZCODE_JSON", "ZCODE_PROVIDER_CONFIG"):
                self.assertEqual(paths[name].read_bytes(), before[name])


if __name__ == "__main__":
    unittest.main()
