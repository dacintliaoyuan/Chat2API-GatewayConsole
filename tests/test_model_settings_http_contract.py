"""Real console HTTP/registry/worker/client-transaction integration, offline."""
import json
import os
import tempfile
import time
from unittest import mock

try:  # `python -m unittest tests.<module>` from ai-gateway/
    from tests.test_console_api import ConsoleApiTestBase as _ApiBase
    from tests.test_console_model_actions import temp_sync_env, _aligned_store
except ImportError:  # `discover -s ai-gateway/tests` (top-level module names)
    from test_console_api import ConsoleApiTestBase as _ApiBase
    from test_console_model_actions import temp_sync_env, _aligned_store
import model_sync as sync
from model_sync_core import CatalogModel


class SettingsHttpContractTests(_ApiBase):
    def test_log_request_filter_reaches_reader_without_truncating_identity(self):
        cookie = self.exchange_session()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "runtime.log")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("2026-10-02 12:00:00 INFO request summary request=abc final=ok\n"
                             "2026-10-02 12:00:01 INFO request summary request=abc2 final=ok\n")
            with mock.patch.dict(os.environ, {"CHAT2API_SERVICE_LOG": path}):
                for request_id, expected in (("abc", None), ("a" * 65, "invalid_request_id")):
                    status, raw = self.req("GET", "/gw/usagesrv/api/console/log-entries?"
                        "source=chat2api&category=service&request_id=" + request_id,
                        headers={"Cookie": cookie})
                    self.assertEqual(status, 200)
                    payload = json.loads(raw)
                    if expected:
                        self.assertEqual(payload["error"], expected)
                    else:
                        self.assertEqual([row["event"]["request_id"] for row in payload["rows"]], ["abc"])
                        self.assertEqual(payload["request_scope"], "read_window")

    def submit(self, cookie, action, params):
        status, raw = self.req("POST", "/gw/usagesrv/api/console/operations",
            body={"action": action, "target": "model", "params": params},
            headers={"Cookie": cookie, "X-Requested-With": "XMLHttpRequest"})
        if status != 201:
            self.fail("operation submit refused: %s" % raw)
        submitted = json.loads(raw)["operation"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, raw = self.req("GET", "/gw/usagesrv/api/console/operations/" + submitted["id"],
                                   headers={"Cookie": cookie})
            self.assertEqual(status, 200)
            op = json.loads(raw)["operation"]
            if op["state"] not in ("queued", "running"):
                return op
            time.sleep(.01)
        self.fail("private model operation did not complete")

    def test_restoration_save_preview_apply_and_readback_use_real_entry(self):
        # Rev11 单文件发布：设置草稿经真实 console 链保存/预览/应用/读回。
        # 已安装客户端的个人 provider 规则面只携带 properties（contextWindow）；
        # 逐模型显示名与默认档位在该 store schema 里没有字段（alias/effort 的
        # 客户端面只在旧版 config.json 迁移路径，见 handoff Rev11 待裁定），
        # 所以本用例以「可发布的 context」验证同一保存/预览/应用/读回路径。
        cookie = self.exchange_session()
        catalogs = {"um": [CatalogModel("um", "fixture-model", context=131072,
            efforts=("low", "high"), default_effort="low", efforts_source="upstream")]}
        with temp_sync_env() as paths, \
             mock.patch.object(sync, "collect_catalogs", return_value=(catalogs, [])), \
             mock.patch.object(sync, "zcode_running", return_value=False), \
             mock.patch.object(sync, "_post_json", side_effect=AssertionError("paid request")):
            paths["ZCODE_PROVIDER_CONFIG"].write_text(json.dumps(_aligned_store(catalogs)), encoding="utf-8")
            config_before = paths["ZCODE_JSON"].read_bytes()
            saved = self.submit(cookie, "model.settings_save", {
                "source": "um", "model_id": "fixture-model", "alias": None,
                "contextSource": "manual", "contextTokens": 262144,
                "defaultEffort": None, "expected_draft_version": 0})
            self.assertEqual(saved["state"], "succeeded", saved)
            selection = {"um|fixture-model": saved["result"]["draft_version"]}
            preview = self.submit(cookie, "model.settings_preview", {"selection": selection})
            self.assertEqual(preview["state"], "succeeded", preview)
            applied = self.submit(cookie, "model.settings_apply", dict(selection=selection,
                preview_token=preview["result"]["preview_token"]))
            self.assertEqual(applied["state"], "succeeded", applied)
            self.assertEqual(applied["result"]["applied"], ["um|fixture-model"])
            # 发布只落在权威文件 provider_config.json；旧 config.json 不写。
            store = json.loads(paths["ZCODE_PROVIDER_CONFIG"].read_text("utf-8"))
            rule = next(rule for rule in store["config"]["modelConfigRules"]
                        ["providerModelRules"] if rule["modelId"] == "fixture-model")
            self.assertEqual(rule["config"]["properties"], {"contextWindow": 262144})
            self.assertEqual(paths["ZCODE_JSON"].read_bytes(), config_before)
            # Change only the context; variants, alias and effort stay equal.
            saved = self.submit(cookie, "model.settings_save", {
                "source": "um", "model_id": "fixture-model",
                "contextSource": "manual", "contextTokens": 200000,
                "expected_draft_version": 1})
            self.assertEqual(saved["state"], "succeeded", saved)
            selection = {"um|fixture-model": 2}
            preview = self.submit(cookie, "model.settings_preview", {"selection": selection})
            self.assertEqual(preview["state"], "succeeded", preview)
            applied = self.submit(cookie, "model.settings_apply", dict(selection=selection,
                preview_token=preview["result"]["preview_token"]))
            self.assertEqual(applied["state"], "succeeded", applied)
            store = json.loads(paths["ZCODE_PROVIDER_CONFIG"].read_text("utf-8"))
            rule = next(rule for rule in store["config"]["modelConfigRules"]
                        ["providerModelRules"] if rule["modelId"] == "fixture-model")
            self.assertEqual(rule["config"]["properties"], {"contextWindow": 200000})
            readback = self.submit(cookie, "model.context_overrides", {})
            self.assertEqual(readback["state"], "succeeded", readback)
            row = next(row for row in readback["result"]["overrides"] if row["model_id"] == "fixture-model")
            self.assertEqual(row["client_context"], 200000)
            zcode = next(row for row in row["client_readbacks"] if row["consumer"] == "ZCode")
            self.assertTrue(zcode["matchesApplied"]["context"])

    def test_combined_preview_and_apply_publish_drafts_with_catalog(self):
        # §11.3 组合窗口端到端（真实 console 链）：一次预览把已保存草稿与目录
        # 差异绑在同一 preview_token；确认后同一受控发布轮同时消费草稿与目录
        # 变化（一次事务、一次写入），结果返回实际应用/延期/冲突/读回。
        cookie = self.exchange_session()
        catalogs = {"um": [CatalogModel("um", "fixture-model", context=131072,
            efforts=("low", "high"), default_effort="low",
            efforts_source="upstream")]}
        with temp_sync_env() as paths, \
             mock.patch.object(sync, "collect_catalogs", return_value=(catalogs, [])), \
             mock.patch.object(sync, "zcode_running", return_value=False), \
             mock.patch.object(sync, "_post_json", side_effect=AssertionError("paid request")):
            store = _aligned_store(catalogs)
            # 目录差异：规则库 um-a 上下文 65536（目录声明 131072）。
            store["config"]["modelConfigRules"]["providerModelRules"][0][
                "config"]["properties"]["contextWindow"] = 65536
            paths["ZCODE_PROVIDER_CONFIG"].write_text(
                json.dumps(store), encoding="utf-8")
            # 草稿：显式采用控制台上下文 + 只改别名。
            saved = self.submit(cookie, "model.settings_save", {
                "source": "um", "model_id": "fixture-model",
                "alias": "renamed", "contextSource": "manual",
                "contextTokens": 131072, "expected_draft_version": 0})
            self.assertEqual(saved["state"], "succeeded", saved)
            selection = {"um|fixture-model": saved["result"]["draft_version"]}
            store_before = paths["ZCODE_PROVIDER_CONFIG"].read_bytes()
            preview = self.submit(cookie, "model.sync_preview", {
                "scope_mode": "all", "selection": selection})
            self.assertEqual(preview["state"], "succeeded", preview)
            # 打开/预览窗口零写入：规则库逐字节不变。
            self.assertEqual(paths["ZCODE_PROVIDER_CONFIG"].read_bytes(),
                             store_before)
            combined = preview["result"]["preview"]
            self.assertEqual([change["key"] for change in combined["changes"]],
                             ["um|fixture-model"])
            change = combined["changes"][0]
            self.assertEqual(change["draft"]["alias"], "renamed")
            self.assertEqual(change["draft"]["contextTokens"], 131072)
            self.assertIsNone(change["before"])
            self.assertEqual(len(preview["result"]["preview_token"]), 64)
            # 确认同步一次：同一受控发布轮同时消费草稿与目录差异。
            applied = self.submit(cookie, "model.sync_apply", {
                "scope_mode": "all", "selection": selection,
                "preview_token": preview["result"]["preview_token"]})
            self.assertEqual(applied["state"], "succeeded", applied)
            result = applied["result"]
            self.assertEqual(result["settingsApplied"], ["um|fixture-model"])
            self.assertEqual(result["settingsDeferred"], [])
            self.assertIn("更新 fixture-model", "\n".join(result["applied"]))
            after = json.loads(paths["ZCODE_PROVIDER_CONFIG"].read_text("utf-8"))
            rule = next(rule for rule in after["config"]["modelConfigRules"]
                        ["providerModelRules"] if rule["modelId"] == "fixture-model")
            self.assertEqual(rule["config"]["properties"]["contextWindow"],
                             131072)
            # 读回：状态面已应用 + 客户端实际值分开；文件写后读回已校验。
            readback = result["settingsReadback"]
            self.assertIsNotNone(readback)
            self.assertTrue(readback["filesVerified"])
            row = readback["keys"][0]
            self.assertEqual(row["settings_applied"]["alias"], "renamed")
            self.assertEqual(row["settings_applied"]["contextTokens"], 131072)
            self.assertEqual(row["settings_draft"], None)

    def test_stale_combined_token_is_rejected_without_writes(self):
        # §11.3.3：范围/草稿/目录/客户端输入变化使旧 token 失效——重新展示
        # 差异后再确认；过期 token 零写入（绝不降级成普通同步）。
        cookie = self.exchange_session()
        catalogs = {"um": [CatalogModel("um", "fixture-model", context=131072)]}
        with temp_sync_env() as paths, \
             mock.patch.object(sync, "collect_catalogs", return_value=(catalogs, [])), \
             mock.patch.object(sync, "zcode_running", return_value=False):
            paths["ZCODE_PROVIDER_CONFIG"].write_text(
                json.dumps(_aligned_store(catalogs)), encoding="utf-8")
            saved = self.submit(cookie, "model.settings_save", {
                "source": "um", "model_id": "fixture-model",
                "alias": "renamed", "expected_draft_version": 0})
            selection = {"um|fixture-model": saved["result"]["draft_version"]}
            preview = self.submit(cookie, "model.sync_preview", {
                "scope_mode": "all", "selection": selection})
            token = preview["result"]["preview_token"]
            # 目录在预览后变化（上下文 131072→65536）：旧 token 失效，零写入。
            edited = json.loads(
                paths["ZCODE_PROVIDER_CONFIG"].read_text("utf-8"))
            edited["config"]["modelConfigRules"]["providerModelRules"][0][
                "config"]["properties"]["contextWindow"] = 65536
            paths["ZCODE_PROVIDER_CONFIG"].write_text(
                json.dumps(edited), encoding="utf-8")
            state_before = paths["STATE_FILE"].read_bytes()
            store_before = paths["ZCODE_PROVIDER_CONFIG"].read_bytes()
            applied = self.submit(cookie, "model.sync_apply", {
                "scope_mode": "all", "selection": selection,
                "preview_token": token})
            self.assertNotEqual(applied["state"], "succeeded")
            self.assertEqual(applied["error"]["code"], "version_conflict")
            self.assertEqual(paths["STATE_FILE"].read_bytes(), state_before)
            self.assertEqual(paths["ZCODE_PROVIDER_CONFIG"].read_bytes(),
                             store_before)

    def test_combined_apply_requires_the_selection_and_token_pair(self):
        # §11.3.4：旧公开动作保留明确兼容语义——普通同步（无 selection）
        # 不接受 token，组合发布（有 selection）必须带回 token；空选/非法
        # 选/范围外草稿一律 invalid_params，绝不静默消费全部草稿。
        cookie = self.exchange_session()
        catalogs = {"um": [CatalogModel("um", "fixture-model", context=131072)]}
        with temp_sync_env() as paths, \
             mock.patch.object(sync, "collect_catalogs", return_value=(catalogs, [])), \
             mock.patch.object(sync, "zcode_running", return_value=False):
            paths["ZCODE_PROVIDER_CONFIG"].write_text(
                json.dumps(_aligned_store(catalogs)), encoding="utf-8")
            # 先经一次成功保存把状态文件建出来：非法参数绝不写状态。
            seeded = self.submit(cookie, "model.settings_save", {
                "source": "um", "model_id": "fixture-model",
                "alias": "seed", "expected_draft_version": 0})
            self.assertEqual(seeded["state"], "succeeded", seeded)
            state_before = paths["STATE_FILE"].read_bytes()
            cases = [
                {"scope_mode": "all", "preview_token": "invalid"},
                {"scope_mode": "all", "selection": {}},
                {"scope_mode": "all", "selection": {"um|fixture-model": 0}},
                {"scope_mode": "all", "selection": {"um|fixture-model": True}},
                {"scope_mode": "all", "selection": {"bad": 1}},
                {"scope_mode": "selected", "sources": json.dumps(["um"]),
                 "models": {"um": ["other-model"]},
                 "selection": {"um|fixture-model": 1}},
            ]
            for params in cases:
                with self.subTest(params=params):
                    refused = self.submit(cookie, "model.sync_apply", params)
                    self.assertEqual(refused["state"], "failed", refused)
                    self.assertEqual(refused["error"]["code"],
                                     "invalid_params", refused)
                    self.assertEqual(paths["STATE_FILE"].read_bytes(),
                                     state_before)

    def test_deferred_combined_round_keeps_the_draft_and_old_applied(self):
        # §11.3.3：延期保留待同步草稿与旧 applied，不提前清空或标已应用。
        cookie = self.exchange_session()
        catalogs = {"um": [CatalogModel("um", "fixture-model", context=131072)]}
        with temp_sync_env() as paths, \
             mock.patch.object(sync, "collect_catalogs", return_value=(catalogs, [])), \
             mock.patch.object(sync, "zcode_running", return_value=False):
            paths["ZCODE_PROVIDER_CONFIG"].write_text(
                json.dumps(_aligned_store(catalogs)), encoding="utf-8")
            saved = self.submit(cookie, "model.settings_save", {
                "source": "um", "model_id": "fixture-model",
                "alias": "renamed", "expected_draft_version": 0})
            selection = {"um|fixture-model": saved["result"]["draft_version"]}
        with temp_sync_env() as paths, \
             mock.patch.object(sync, "collect_catalogs", return_value=(catalogs, [])), \
             mock.patch.object(sync, "zcode_running", return_value=False):
            paths["ZCODE_PROVIDER_CONFIG"].write_text(
                json.dumps(_aligned_store(catalogs)), encoding="utf-8")
            state_doc = sync.empty_state()
            state_doc["modelSettings"] = {"um|fixture-model": {
                "draft": {"alias": "renamed", "contextSource": None,
                          "contextTokens": None, "defaultEffort": None},
                "draftVersion": 1, "draftSavedAt": "t",
                "applied": None, "appliedVersion": None,
                "appliedAt": None, "appliedEvidence": None}}
            paths["STATE_FILE"].write_text(
                json.dumps(state_doc, ensure_ascii=False), encoding="utf-8")
            preview = self.submit(cookie, "model.sync_preview", {
                "scope_mode": "all", "selection": selection})
            self.assertEqual(preview["state"], "succeeded", preview)
            state_before = paths["STATE_FILE"].read_bytes()
            store_before = paths["ZCODE_PROVIDER_CONFIG"].read_bytes()
            with mock.patch.object(sync, "zcode_running", return_value=True), \
                 mock.patch.object(sync, "zcode_hot_sync_admission",
                                   return_value=(False, "测试：未准入")):
                applied = self.submit(cookie, "model.sync_apply", {
                    "scope_mode": "all", "selection": selection,
                    "preview_token": preview["result"]["preview_token"]})
            self.assertEqual(applied["state"], "succeeded", applied)
            result = applied["result"]
            self.assertEqual(result["applied"], [])
            self.assertEqual(result["settingsApplied"], [])
            self.assertEqual(result["settingsDeferred"], ["um|fixture-model"])
            self.assertEqual(paths["ZCODE_PROVIDER_CONFIG"].read_bytes(),
                             store_before)
            state = json.loads(paths["STATE_FILE"].read_text("utf-8"))
            record = state["modelSettings"]["um|fixture-model"]
            self.assertEqual(record["draft"]["alias"], "renamed")
            self.assertIsNone(record["applied"])

    def test_catalog_source_scope_reads_only_the_selected_source(self):
        # R7：显式 sources 只读该来源；未知/已暂停来源在边界拒绝，绝不静默
        # 退回四源。
        cookie = self.exchange_session()
        catalogs = {"um": [CatalogModel("um", "fixture-model", context=131072)]}
        with temp_sync_env(), mock.patch.object(
                sync, "collect_catalogs",
                return_value=(catalogs, [])) as collect:
            scoped = self.submit(cookie, "model.catalog", {
                "sources": json.dumps(["um"])})
            self.assertEqual(scoped["state"], "succeeded", scoped)
            self.assertEqual(collect.call_args, mock.call(sources=["um"]))
            self.assertEqual(scoped["result"]["sources"], ["um"])
            self.assertEqual([row["id"] for row in
                              scoped["result"]["catalogs"]["um"]],
                             ["fixture-model"])
        with temp_sync_env(), mock.patch.object(
                sync, "collect_catalogs",
                return_value=(catalogs, [])) as collect:
            refused = self.submit(cookie, "model.catalog", {
                "sources": json.dumps(["m365"])})
            self.assertNotEqual(refused["state"], "succeeded")
            collect.assert_not_called()

    def test_context_conflict_holds_the_client_value_until_decided(self):
        # R6/D4 端到端（真实 console 链）：客户端手工值与控制台目标冲突、
        # 无基准记录 → 不覆盖、只读读面如实给出冲突与「无基准」；显式保存
        # 采用控制台值后（contextDecided）才发布并登记基准。
        cookie = self.exchange_session()
        catalogs = {"um": [CatalogModel("um", "fixture-model", context=131072)]}
        with temp_sync_env() as paths, \
             mock.patch.object(sync, "collect_catalogs", return_value=(catalogs, [])), \
             mock.patch.object(sync, "zcode_running", return_value=False), \
             mock.patch.object(sync, "_post_json", side_effect=AssertionError("paid request")):
            store = _aligned_store(catalogs)
            store["config"]["modelConfigRules"]["providerModelRules"][0][
                "config"]["properties"]["contextWindow"] = 65536
            paths["ZCODE_PROVIDER_CONFIG"].write_text(
                json.dumps(store), encoding="utf-8")
            store_before = paths["ZCODE_PROVIDER_CONFIG"].read_bytes()
            preview = self.submit(cookie, "model.sync_preview", {"scope_mode": "all"})
            self.assertEqual(preview["state"], "succeeded", preview)
            plan = preview["result"]["preview"]
            self.assertTrue(any("待裁定" in line for line in plan["plan"]), plan)
            self.assertEqual(len(plan["contextConflicts"]), 1)
            self.assertEqual(plan["contextConflicts"][0]["client"], 65536)
            self.assertEqual(plan["contextConflicts"][0]["target"], 131072)
            self.assertFalse(plan["contextConflicts"][0]["baseline_recorded"])
            applied = self.submit(cookie, "model.sync_apply", {"scope_mode": "all"})
            self.assertEqual(applied["state"], "succeeded", applied)
            # 冲突的模型绝不发布：ZCode 侧没有任何「更新」写入，规则库
            # 逐字节不变（DSH 侧无关的发布照常）。
            self.assertFalse(any("更新 fixture-model" in line
                                for line in applied["result"]["applied"]))
            self.assertEqual(paths["ZCODE_PROVIDER_CONFIG"].read_bytes(), store_before)
            readback = self.submit(cookie, "model.context_overrides", {})
            row = next(row for row in readback["result"]["overrides"] if row["model_id"] == "fixture-model")
            # 发布前：ZCode 客户端值仍是手工值，冲突未裁定、基准未知
            # （DSH 侧无关的发布不改 ZCode 规则库）。
            zcode = next(item for item in row["client_readbacks"]
                         if item["consumer"] == "ZCode")
            self.assertEqual(zcode["contextTokens"], 65536)
            self.assertFalse(zcode["publishedKnown"])
            self.assertIsNone(zcode["publishedContext"])
            self.assertTrue(row["context_conflict"])
            self.assertEqual(row["context_action"], "conflict")
            # 用户显式保存采用控制台值（contextDecided）→ 预览绑定草稿版本后
            # 应用：发布控制台目标并登记基准 131072。
            saved = self.submit(cookie, "model.settings_save", {
                "source": "um", "model_id": "fixture-model",
                "contextSource": "manual", "contextTokens": 131072,
                "expected_draft_version": 0})
            self.assertEqual(saved["state"], "succeeded", saved)
            selection = {"um|fixture-model": saved["result"]["draft_version"]}
            settings_preview = self.submit(cookie, "model.settings_preview",
                                           {"selection": selection})
            self.assertEqual(settings_preview["state"], "succeeded",
                              settings_preview)
            settings_apply = self.submit(cookie, "model.settings_apply", dict(
                selection=selection,
                preview_token=settings_preview["result"]["preview_token"]))
            self.assertEqual(settings_apply["state"], "succeeded", settings_apply)
            after = json.loads(paths["ZCODE_PROVIDER_CONFIG"].read_text("utf-8"))
            rule = next(rule for rule in after["config"]["modelConfigRules"]
                        ["providerModelRules"] if rule["modelId"] == "fixture-model")
            self.assertEqual(rule["config"]["properties"]["contextWindow"], 131072)
            readback = self.submit(cookie, "model.context_overrides", {})
            row = next(row for row in readback["result"]["overrides"] if row["model_id"] == "fixture-model")
            # 发布后：ZCode 客户端持有控制台目标，冲突解除，基准已登记。
            zcode = next(item for item in row["client_readbacks"]
                         if item["consumer"] == "ZCode")
            self.assertEqual(zcode["contextTokens"], 131072)
            self.assertTrue(zcode["publishedKnown"])
            self.assertEqual(zcode["publishedContext"], 131072)
            self.assertFalse(row["context_conflict"])

    def test_partial_field_save_keeps_untouched_values(self):
        # R5 端到端：前端只发送实际变动字段——未改字段不被空值清除；
        # 显式 null 才是恢复目录默认。
        cookie = self.exchange_session()
        catalogs = {"um": [CatalogModel("um", "fixture-model", context=131072)]}
        with temp_sync_env() as paths, \
             mock.patch.object(sync, "collect_catalogs", return_value=(catalogs, [])), \
             mock.patch.object(sync, "zcode_running", return_value=False):
            paths["ZCODE_PROVIDER_CONFIG"].write_text(
                json.dumps(_aligned_store(catalogs)), encoding="utf-8")
            first = self.submit(cookie, "model.settings_save", {
                "source": "um", "model_id": "fixture-model",
                "contextSource": "manual", "contextTokens": 262144,
                "expected_draft_version": 0})
            self.assertEqual(first["state"], "succeeded", first)
            # 只改 alias：上下文与默认档位原样保留（部分字段合并）。
            second = self.submit(cookie, "model.settings_save", {
                "source": "um", "model_id": "fixture-model",
                "alias": "renamed",
                "expected_draft_version": first["result"]["draft_version"]})
            self.assertEqual(second["state"], "succeeded", second)
            draft = second["result"]["draft"]
            self.assertEqual(draft["alias"], "renamed")
            self.assertEqual(draft["contextSource"], "manual")
            self.assertEqual(draft["contextTokens"], 262144)
            # 显式 null 恢复目录默认。
            third = self.submit(cookie, "model.settings_save", {
                "source": "um", "model_id": "fixture-model",
                "contextSource": None, "contextTokens": None,
                "expected_draft_version": second["result"]["draft_version"]})
            self.assertEqual(third["state"], "succeeded", third)
            self.assertIsNone(third["result"]["draft"]["contextSource"])
            self.assertIsNone(third["result"]["draft"]["contextTokens"])
            self.assertEqual(third["result"]["draft"]["alias"], "renamed")

    def test_stale_draft_version_is_a_conflict_without_writes(self):
        # R5 版本冲突：读面草稿版本过期 → version_conflict，状态零写入。
        cookie = self.exchange_session()
        catalogs = {"um": [CatalogModel("um", "fixture-model", context=131072)]}
        with temp_sync_env() as paths, \
             mock.patch.object(sync, "collect_catalogs", return_value=(catalogs, [])), \
             mock.patch.object(sync, "zcode_running", return_value=False):
            paths["ZCODE_PROVIDER_CONFIG"].write_text(
                json.dumps(_aligned_store(catalogs)), encoding="utf-8")
            first = self.submit(cookie, "model.settings_save", {
                "source": "um", "model_id": "fixture-model",
                "alias": "first", "expected_draft_version": 0})
            self.assertEqual(first["state"], "succeeded", first)
            state_before = paths["STATE_FILE"].read_bytes()
            stale = self.submit(cookie, "model.settings_save", {
                "source": "um", "model_id": "fixture-model",
                "alias": "stale", "expected_draft_version": 0})
            self.assertNotEqual(stale["state"], "succeeded")
            self.assertEqual(stale["error"]["code"], "version_conflict")
            self.assertEqual(paths["STATE_FILE"].read_bytes(), state_before)
