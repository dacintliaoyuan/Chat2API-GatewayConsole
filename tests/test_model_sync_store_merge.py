# -*- coding: utf-8 -*-
"""Rev11：受管范围合并与消费快照权威文件（provider_config.json）。

产品路径：``model_sync.collect_consumers`` / ``model_sync.run_sync`` /
``model_sync._locked_store_merge``。所有用例只在自己的临时目录内构造双存储
（``provider_config.json`` + 旧 ``config.json``），绝不读取或写入真实的
~/.zcode / ~/.dsh 配置。

覆盖：消费快照改读 provider_config.json（规则库不携带的 profile 细节按 id 从
旧条目补齐）、锁内重读只合并本轮受管范围、并发受管编辑明确延期不覆盖、并发
手工编辑/顺序/defaultModelSelection 由渲染器原样保留、写入在锁内、锁超时以明确
错误码返回不静默继续。
"""
import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_sync import sync as model_sync
from zcode import file_lock as zcode_file_lock
from model_sync.sync_core import Binding, CatalogModel, empty_state

UM = Binding("um", "ZCode", "local:openwebui", "UM", "（UM）")
MANUAL = "local:manual"


def _store(provider="local:openwebui", *, provider_name="UM",
           whitelist=("um-a",), contexts=None, order=None,
           default_selection=None, extra_provider=None):
    """按客户端布局构造规则库文档（schemaVersion 1）。"""
    contexts = contexts or {}
    rules = [{
        "providerId": provider,
        "providerName": provider_name,
        "enabled": True,
        "config": {
            "group": "standard-personal",
            "personalModelIds": list(whitelist),
            "modelOrder": list(whitelist),
        },
    }]
    model_rules = [{
        "modelId": model_id,
        "providerId": provider,
        "config": {"properties": {"contextWindow": contexts[model_id]}},
    } for model_id in whitelist if model_id in contexts]
    if extra_provider is not None:
        rules.append(extra_provider[0])
        model_rules.extend(extra_provider[1])
    doc = {
        "schemaVersion": 1,
        "config": {
            "providerOrder": list(order) if order is not None
            else [provider],
            "providerConfigRules": {"providerRules": rules},
            "modelConfigRules": {"providerModelRules": model_rules},
        },
    }
    if default_selection is not None:
        doc["config"]["defaultModelSelection"] = default_selection
    return doc


class StoreSnapshotTests(unittest.TestCase):
    """消费快照：ZCode 侧读取 provider_config.json。"""

    def _layout(self, root, *, store_doc, config_doc=None,
                config_present=True):
        store = root / "provider_config.json"
        config = root / "config.json"
        if store_doc is not None:
            store.write_text(json.dumps(store_doc, ensure_ascii=False),
                             encoding="utf-8")
        if config_present:
            config.write_text(json.dumps(config_doc or {}, ensure_ascii=False),
                              encoding="utf-8")
        return store, config

    def test_store_surface_wins_over_the_legacy_file(self):
        # Rev11 单文件发布：规则库在场且可解析时 provider 显示名与模型 id 面
        # 以它为准；规则库不携带的逐模型显示名/档位（新版权威文件没有该
        # 字段，同步无法经它发布）不从旧条目补齐——否则计划每轮产生一条
        # 永远无法落盘的「更新」。旧条目独有而规则库缺席的 id 保留在场
        # （不制造字段回退的 add）。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            store_doc = _store(
                provider_name="UM（新）",
                whitelist=("um-a", "um-b"),
                contexts={"um-a": 111, "um-b": 222})
            config_doc = {"provider": {"local:openwebui": {
                "name": "UM",
                "models": {
                    "um-a": {"name": "um-a（UM）",
                             "limit": {"context": 111},
                             "reasoning": {"variants": ["low"],
                                            "defaultVariant": "low"}},
                    "legacy-only": {"name": "legacy"},
                }}}}
            store, config = self._layout(root, store_doc=store_doc,
                                         config_doc=config_doc)
            with mock.patch.object(model_sync, "ZCODE_JSON", config), \
                    mock.patch.object(model_sync, "ZCODE_PROVIDER_CONFIG",
                                       store), \
                    mock.patch.object(model_sync, "DSH_YAML",
                                       root / "settings.yaml"):
                consumers, _, zcode_raw = model_sync.collect_consumers(
                    ["ZCode"])
            self.assertIsInstance(zcode_raw, str)
            snapshot = consumers[UM.key]
            self.assertEqual(snapshot["providerName"], "UM（新）")
            self.assertEqual(sorted(snapshot["models"]),
                             ["legacy-only", "um-a", "um-b"])
            # 规则库的 profile（contextWindow）为准；旧条目的 name/reasoning
            # 不进入发布快照。
            self.assertNotIn("name", snapshot["models"]["um-a"])
            self.assertNotIn("reasoning", snapshot["models"]["um-a"])
            self.assertEqual(snapshot["models"]["um-a"]["contextWindow"], 111)
            self.assertEqual(snapshot["models"]["um-b"]["contextWindow"], 222)
            # 旧条目独有而规则库缺席的 id 保留在场（保留旧 profile）。
            self.assertEqual(snapshot["models"]["legacy-only"]["name"],
                             "legacy")

    def test_missing_store_falls_back_to_the_legacy_file(self):
        # 规则库缺席＝客户端一次性导入旧配置的常态：快照退回旧文件，
        # 绝不折叠成空面。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            config_doc = {"provider": {"local:openwebui": {
                "name": "UM", "models": {"um-a": {"name": "um-a（UM）"}}}}}
            store, config = self._layout(root, store_doc=None,
                                         config_doc=config_doc)
            self.assertFalse(store.exists())
            with mock.patch.object(model_sync, "ZCODE_JSON", config), \
                    mock.patch.object(model_sync, "ZCODE_PROVIDER_CONFIG",
                                       store), \
                    mock.patch.object(model_sync, "DSH_YAML",
                                       root / "settings.yaml"):
                consumers, _, _ = model_sync.collect_consumers(["ZCode"])
            self.assertEqual(consumers[UM.key]["providerName"], "UM")
            self.assertEqual(sorted(consumers[UM.key]["models"]), ["um-a"])

    def test_corrupt_store_reports_and_falls_back(self):
        # 规则库在场但无法解析：如实报告（problem），快照退回旧文件；
        # 旧文件也缺席/损坏时才是诚实失败。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            store = root / "provider_config.json"
            store.write_text("{not json", encoding="utf-8")
            config = root / "config.json"
            config.write_text(json.dumps({"provider": {"local:openwebui": {
                "name": "UM", "models": {"um-a": {"name": "um-a（UM）"}}}}}),
                encoding="utf-8")
            with mock.patch.object(model_sync, "ZCODE_JSON", config), \
                    mock.patch.object(model_sync, "ZCODE_PROVIDER_CONFIG",
                                       store), \
                    mock.patch.object(model_sync, "DSH_YAML",
                                       root / "settings.yaml"):
                consumers, _, _ = model_sync.collect_consumers(["ZCode"])
                self.assertEqual(consumers[UM.key]["providerName"], "UM")
                surface, problem = model_sync._zcode_store_snapshot()
                self.assertIsNone(surface)
                self.assertIn("无法解析", problem)
                config.unlink()
                with self.assertRaises(OSError):
                    model_sync.collect_consumers(["ZCode"])

    def test_missing_both_files_is_an_honest_failure(self):
        # 规则库与旧文件都缺席：如实失败，绝不折叠成空面。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            with mock.patch.object(model_sync, "ZCODE_JSON",
                                   root / "config.json"), \
                    mock.patch.object(model_sync, "ZCODE_PROVIDER_CONFIG",
                                       root / "provider_config.json"), \
                    mock.patch.object(model_sync, "DSH_YAML",
                                       root / "settings.yaml"):
                with self.assertRaises(OSError):
                    model_sync.collect_consumers(["ZCode"])


class ManagedScopeMergeTests(unittest.TestCase):
    """锁内重读、只合并受管范围与写入窗口。"""

    def _layout(self, root, *, store_doc, config_doc, state_doc):
        store = root / "provider_config.json"
        store.write_text(json.dumps(store_doc, ensure_ascii=False),
                         encoding="utf-8")
        config = root / "config.json"
        config.write_text(json.dumps(config_doc, ensure_ascii=False),
                          encoding="utf-8")
        state_file = root / "state.json"
        state_file.write_text(json.dumps(state_doc, ensure_ascii=False),
                              encoding="utf-8")
        return store, config, state_file

    def _run(self, root, catalogs, *, scope=None, lock_wrapper=None,
             transaction=None):
        store = root / "provider_config.json"
        config = root / "config.json"
        state_file = root / "state.json"
        patches = [
            mock.patch.object(model_sync, "ZCODE_JSON", config),
            mock.patch.object(model_sync, "ZCODE_PROVIDER_CONFIG", store),
            mock.patch.object(model_sync, "DSH_YAML",
                               root / "settings.yaml"),
            mock.patch.object(model_sync, "STATE_FILE", state_file),
            mock.patch.object(model_sync, "RESULT_FILE", root / "result.json"),
            mock.patch.object(model_sync, "PREVIEW_FILE",
                               root / "preview.json"),
            mock.patch.object(model_sync, "HISTORY_FILE",
                               root / "history.jsonl"),
            mock.patch.object(model_sync, "BACKUP_DIR", root / "backups"),
            mock.patch.object(model_sync, "collect_catalogs",
                               return_value=(catalogs, [])),
            mock.patch.object(model_sync, "zcode_running",
                               return_value=False),
        ]
        if lock_wrapper is not None:
            patches.append(mock.patch.object(
                model_sync, "zcode_file_lock", lock_wrapper))
        if transaction is not None:
            patches.append(mock.patch.object(
                model_sync, "apply_config_transaction", transaction))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            return model_sync.run_sync(
                apply=True, run_probes=False, confirm=False, scope=scope)

    def _aligned_layout(self, root, *, catalog_context=200000,
                        store_context=111, concurrent_edit=None):
        """对齐前提：目录 um-a=200000，规则库 um-a 上下文待对齐。"""
        catalogs = {"um": [CatalogModel("um", "um-a",
                                        context=catalog_context)]}
        store_doc = _store(whitelist=("um-a",),
                            contexts={"um-a": store_context})
        config_doc = {"provider": {"local:openwebui": {
            "name": "UM",
            "models": {"um-a": {"name": "um-a（UM）",
                                 "limit": {"context": catalog_context}}}}}}
        state_doc = empty_state()
        state_doc["initializedBindings"] = [UM.key]
        # R6/D4 三方比较夹具（2026-10-10）：规则库当前值 111 就是本同步
        # 上次实际发布的基准——上下文对齐照常发布（本类测锁内重读/合并与写入
        # 窗口，不是冲突保护；冲突用例见 test_model_sync 的
        # TestContextPublishProtection）。
        state_doc["managed"] = {UM.key: {"um-a": {
            "profile": {},
            "published": {"context": store_context,
                          "at": "published-time"}}}}
        state_doc["catalogCounts"] = {"um": 1}
        store, config, state_file = self._layout(
            root, store_doc=store_doc, config_doc=config_doc,
            state_doc=state_doc)
        return catalogs, store, config

    def test_concurrent_managed_edit_defers_without_overwriting(self):
        # 计划基线之后、锁内重读之前并发改写了受管 id 的规则条目：
        # 版本/指纹冲突 → 本轮 ZCode 写集合整体延期，绝不覆盖。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            catalogs, store, config = self._aligned_layout(root)
            config_before = config.read_text("utf-8")
            concurrent = {}

            @contextlib.contextmanager
            def concurrent_edit(path, **kwargs):
                edited = json.loads(store.read_text("utf-8"))
                edited["config"]["modelConfigRules"]["providerModelRules"][0][
                    "config"]["properties"]["contextWindow"] = 222
                concurrent["text"] = json.dumps(edited, ensure_ascii=False)
                store.write_text(concurrent["text"], encoding="utf-8")
                with zcode_file_lock.zcode_file_lock(path, **kwargs):
                    yield

            result = self._run(root, catalogs, lock_wrapper=concurrent_edit)
            self.assertEqual(result["status"], "pending")
            self.assertTrue(any("版本/指纹冲突" in line
                                for line in result["pending"]))
            self.assertFalse(result["zcodeChanged"])
            # 并发编辑原样保留：规则库逐字节未被覆盖，旧文件也没写。
            self.assertEqual(store.read_text("utf-8"), concurrent["text"])
            self.assertEqual(json.loads(store.read_text("utf-8"))["config"]
                             ["modelConfigRules"]["providerModelRules"][0]
                             ["config"]["properties"]["contextWindow"], 222)
            self.assertEqual(config.read_text("utf-8"), config_before)

    def test_concurrent_manual_edits_are_preserved_by_the_merge(self):
        # 并发手工编辑（其他供应商、受管 provider 里的手工 id、顺序与
        # defaultModelSelection）不参与受管指纹 → 锁内合并照常进行且全部保留。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            catalogs, store, config = self._aligned_layout(root)

            @contextlib.contextmanager
            def concurrent_edit(path, **kwargs):
                edited = json.loads(store.read_text("utf-8"))
                cfg = edited["config"]
                # 受管 provider 里的手工 id（不在 sync_owned，也不在目录）
                cfg["providerConfigRules"]["providerRules"][0]["config"][
                    "personalModelIds"].append("manual-note")
                cfg["providerConfigRules"]["providerRules"][0]["config"][
                    "modelOrder"].append("manual-note")
                # 其他供应商 + 手工条目
                cfg["providerConfigRules"]["providerRules"].append({
                    "providerId": MANUAL, "providerName": "手工",
                    "enabled": True,
                    "config": {"group": "standard-personal",
                                "personalModelIds": ["m1", "m2"],
                                "modelOrder": ["m1", "m2"]}})
                cfg["modelConfigRules"]["providerModelRules"].extend([
                    {"modelId": "m1", "providerId": MANUAL,
                     "config": {"properties": {"contextWindow": 1}}},
                    {"modelId": "m2", "providerId": MANUAL,
                     "config": {"properties": {"contextWindow": 2}}},
                ])
                cfg["providerOrder"] = [MANUAL, "local:openwebui"]
                cfg["defaultModelSelection"] = {"providerId": MANUAL,
                                                "modelId": "m1"}
                store.write_text(json.dumps(edited, ensure_ascii=False),
                                 encoding="utf-8")
                with zcode_file_lock.zcode_file_lock(path, **kwargs):
                    yield

            result = self._run(root, catalogs, lock_wrapper=concurrent_edit)
            self.assertEqual(result["status"], "changed")
            self.assertTrue(any("更新 um-a" in line
                                for line in result["applied"]))
            after = json.loads(store.read_text("utf-8"))["config"]
            rule = next(r for r in after["providerConfigRules"]["providerRules"]
                        if r["providerId"] == "local:openwebui")
            self.assertEqual(rule["config"]["personalModelIds"],
                             ["um-a", "manual-note"])
            manual = next(r for r in after["providerConfigRules"]["providerRules"]
                          if r["providerId"] == MANUAL)
            self.assertEqual(manual["config"]["personalModelIds"], ["m1", "m2"])
            self.assertEqual(after["providerOrder"], [MANUAL, "local:openwebui"])
            self.assertEqual(after["defaultModelSelection"],
                             {"providerId": MANUAL, "modelId": "m1"})
            # 受管 id 的上下文按目录对齐（111 → 200000）。
            model_rules = {r["modelId"]: r for r in
                           after["modelConfigRules"]["providerModelRules"]
                           if r["providerId"] == "local:openwebui"}
            self.assertEqual(
                model_rules["um-a"]["config"]["properties"]["contextWindow"],
                200000)
            # 手工 id 不发布：它保留在白名单里，但没有逐模型规则条目。
            self.assertNotIn("manual-note", model_rules)

    def test_store_only_conflict_is_recorded_as_pending(self):
        # 本轮只有规则库对齐（providerOrder 缺受管 provider、模型面无变化）：
        # 锁内并发改写受管 id 时同样延期，且「为什么没写」必须进 pending。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            catalogs, store, config = self._aligned_layout(
                root, store_context=200000)
            edited = json.loads(store.read_text("utf-8"))
            edited["config"]["providerOrder"] = []
            store.write_text(json.dumps(edited, ensure_ascii=False),
                             encoding="utf-8")

            @contextlib.contextmanager
            def concurrent_edit(path, **kwargs):
                edited = json.loads(store.read_text("utf-8"))
                edited["config"]["modelConfigRules"]["providerModelRules"][0][
                    "config"]["properties"]["contextWindow"] = 222
                store.write_text(json.dumps(edited, ensure_ascii=False),
                                 encoding="utf-8")
                with zcode_file_lock.zcode_file_lock(path, **kwargs):
                    yield

            result = self._run(root, catalogs, lock_wrapper=concurrent_edit)
            self.assertEqual(result["status"], "pending")
            self.assertTrue(any("版本/指纹冲突" in line
                                for line in result["pending"]))
            self.assertFalse(result["zcodeChanged"])

    def test_write_runs_inside_the_client_lock(self):
        # 事务替换发生在客户端兼容锁内：写入窗口被锁覆盖。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            catalogs, store, config = self._aligned_layout(root)
            observed = {}
            real_transaction = model_sync.apply_config_transaction

            def recorder(candidates, backup_root, keep_backups=30):
                observed["locked"] = os.path.isdir(str(store) + ".lock")
                return real_transaction(candidates, backup_root,
                                        keep_backups=keep_backups)

            result = self._run(root, catalogs, transaction=recorder)
            self.assertEqual(result["status"], "changed")
            self.assertTrue(observed["locked"])
            self.assertFalse(Path(str(store) + ".lock").exists())

    def test_lock_timeout_is_not_silently_continued(self):
        # 锁等待超时：以明确错误码抛出，绝不静默继续，也绝不写任何文件。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            catalogs, store, config = self._aligned_layout(root)
            store_before = store.read_text("utf-8")
            config_before = config.read_text("utf-8")

            @contextlib.contextmanager
            def timed_out(path, **kwargs):
                raise zcode_file_lock.ZCodeFileLockTimeout(
                    path, str(path) + ".lock", 250)
                yield  # pragma: no cover

            with self.assertRaises(zcode_file_lock.ZCodeFileLockTimeout) as caught:
                self._run(root, catalogs, lock_wrapper=timed_out)
            self.assertEqual(caught.exception.code,
                             zcode_file_lock.ZCODE_FILE_LOCK_TIMEOUT)
            self.assertEqual(store.read_text("utf-8"), store_before)
            self.assertEqual(config.read_text("utf-8"), config_before)


class ProfileEvidenceScopeTests(unittest.TestCase):
    """Rev11：档位/显示名的比对只在客户端确实带证据时生效。

    已安装客户端的实时权威文件（provider_config.json）不携带逐模型显示名与
    默认档位；同步无法经它发布这些字段，把「客户端没有档位」当差异会让计划
    每轮产生一条永远无法落盘的「更新」。客户端确实带证据的通道（DSH YAML、
    旧版 config.json 迁移路径）仍照常比对。
    """

    def test_absent_client_reasoning_does_not_plan_an_update(self):
        catalogs = {"um": [CatalogModel("um", "um-a", context=200000,
                                        efforts=("low", "high"),
                                        default_effort="low")]}
        consumers = {UM.key: {"providerName": "UM",
                              "models": {"um-a": {"contextWindow": 200000}}}}
        state = empty_state()
        state["initializedBindings"] = [UM.key]
        state["managed"] = {UM.key: {"um-a": {"profile": {
            "contextWindow": 200000}}}}
        plan = model_sync.plan_catalog_mirror(catalogs, consumers, state,
                                             [UM])
        # 客户端没有档位证据：不产生无法发布的「更新」，计划幂等。
        self.assertEqual(plan.changes, [])

    def test_legacy_display_name_is_not_a_pending_publish(self):
        # 旧 config.json 的显示名与新版权威文件不同：同步无法经规则库发布显示名
        # → 不产生永远无法落盘的「更新」，计划幂等（旧文件也不被写）。
        catalogs = {"um": [CatalogModel("um", "um-a", context=200000)]}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            store = root / "provider_config.json"
            config = root / "config.json"
            store.write_text(json.dumps(_store(whitelist=("um-a",),
                                              contexts={"um-a": 200000}),
                                       ensure_ascii=False), encoding="utf-8")
            config.write_text(json.dumps({"provider": {"local:openwebui": {
                "name": "UM", "models": {
                    "um-a": {"name": "旧别名（UM）",
                             "limit": {"context": 200000}}}}}},
                ensure_ascii=False), encoding="utf-8")
            with mock.patch.object(model_sync, "ZCODE_JSON", config), \
                    mock.patch.object(model_sync, "ZCODE_PROVIDER_CONFIG",
                                       store), \
                    mock.patch.object(model_sync, "DSH_YAML",
                                       root / "settings.yaml"):
                consumers, _, _ = model_sync.collect_consumers(["ZCode"])
        snapshot = consumers[UM.key]
        self.assertNotIn("name", snapshot["models"]["um-a"])
        state = empty_state()
        state["initializedBindings"] = [UM.key]
        state["managed"] = {UM.key: {"um-a": {"profile": {
            "contextWindow": 200000}}}}
        plan = model_sync.plan_catalog_mirror(catalogs, consumers, state,
                                              [UM])
        self.assertEqual(plan.changes, [])

    def test_client_reasoning_evidence_still_plans_the_update(self):
        catalogs = {"um": [CatalogModel("um", "um-a", context=200000,
                                        efforts=("low", "high"),
                                        default_effort="low")]}
        consumers = {UM.key: {"providerName": "UM", "models": {
            "um-a": {"contextWindow": 200000,
                     "reasoning": {"variants": ["low"],
                                   "defaultVariant": "low"}}}}}
        state = empty_state()
        state["initializedBindings"] = [UM.key]
        state["managed"] = {UM.key: {"um-a": {"profile": {
            "contextWindow": 200000,
            "reasoning": {"variants": ["low"],
                           "defaultVariant": "low"}}}}}
        plan = model_sync.plan_catalog_mirror(catalogs, consumers, state,
                                             [UM])
        # 客户端确实带档位证据且与目录不一致：照常规划更新。
        kinds = [change.kind for change in plan.changes]
        self.assertEqual(kinds, ["update"])
        self.assertEqual(plan.changes[0].model_id, "um-a")


if __name__ == "__main__":
    unittest.main()
