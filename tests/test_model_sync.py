# -*- coding: utf-8 -*-
import contextlib
import errno
import os
import struct
import subprocess
import sys
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_sync import config_adapters as model_config_adapters
from model_sync import sync as model_sync
from zcode import file_lock as zcode_file_lock
from zcode import hot_sync as zcode_hot_sync
from model_sync.sync_core import Binding, CatalogModel, SyncChange, SyncPlan, empty_state

# The suite must never read or write the operator's real rule store.
# model_sync resolves ZCODE_PROVIDER_CONFIG at call time, so a test that forgets to
# patch it reads ~/.zcode/v2/provider_config.json (and an apply whose scope covers
# ZCode would rewrite it — the 2026-09-26 incident class). This module-level patch
# points every unpatched call site at a temporary file; tests that need their own
# store still patch it themselves (the innermost patch wins).
_provider_config_patch = None
_provider_config_dir = None


def setUpModule():
    global _provider_config_patch, _provider_config_dir
    _provider_config_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    _provider_config_patch = mock.patch.object(
        model_sync, "ZCODE_PROVIDER_CONFIG",
        Path(_provider_config_dir.name) / "provider_config.json")
    _provider_config_patch.start()


def tearDownModule():
    if _provider_config_patch is not None:
        _provider_config_patch.stop()
    if _provider_config_dir is not None:
        _provider_config_dir.cleanup()


def aligned_rule_store(catalogs, bindings=None):
    """A rule store already aligned with these catalogs（对齐是 no-op）。

    2026-10-01 R20-2：规则库不可用（缺席／不可读／无法解析）时整个 ZCode
    写集合会延期——所以「写主配置」类用例的夹具必须给出一个**正常**的规则库
    （真实生产布局两份文件都在），否则用例测到的不是它想测的写入路径，而是
    延期分支。这里按目录构造每个受管 ZCode provider 的规则块：白名单/排序
    取目录发布面、模型规则逐条带 contextWindow（与渲染器 own 的属性一致），
    因此渲染结果与原文档相等，本轮不产生规则库变化。
    """
    bindings = tuple(bindings if bindings is not None
                     else model_sync.ACTIVE_BINDINGS)
    order: list[str] = []
    provider_rules: list[dict] = []
    model_rules: list[dict] = []
    for binding in bindings:
        catalog = (catalogs or {}).get(binding.source)
        if binding.consumer != "ZCode" or not catalog:
            continue
        published = [m for m in catalog if m.publish]
        order.append(binding.provider)
        provider_rules.append({
            "providerId": binding.provider,
            "providerName": binding.provider_name,
            "enabled": True,
            "config": {
                "group": "standard-personal",
                "personalModelIds": [m.model_id for m in published],
                "modelOrder": [m.model_id for m in published],
            },
        })
        for model in published:
            properties = {}
            if model.context is not None:
                properties["contextWindow"] = int(model.context)
            model_rules.append({
                "modelId": model.model_id,
                "config": {"properties": properties},
                "providerId": binding.provider,
            })
    return {"schemaVersion": 1, "config": {
        "providerOrder": order,
        "providerConfigRules": {"providerRules": provider_rules},
        "modelConfigRules": {"providerModelRules": model_rules},
    }}


class TestCapabilityProbe(unittest.TestCase):
    def test_deferred_managed_addition_is_not_a_new_source_model(self):
        binding = Binding(
            "um", "ZCode", "local:openwebui",
            "UM", "（UM）")
        model = CatalogModel("um", "already-known")
        state = empty_state()
        state["initializedBindings"] = [binding.key]
        state["managed"] = {binding.key: {"already-known": {"profile": {}}}}
        plan = SyncPlan(changes=[SyncChange(
            binding, "add", "already-known", "deferred", model=model)])

        candidates = model_sync._new_model_candidates(plan, state)

        self.assertEqual(candidates["um"], set())

    def test_m365_effort_objects_are_normalized(self):
        self.assertEqual(
            model_sync._effort_names([
                {"effort": "none"}, {"effort": "minimal"},
                {"effort": "xhigh"}, {"effort": "max"},
            ]),
            ("minimal", "xhigh"),
        )

    def test_qoder_global_can_preserve_max_effort(self):
        self.assertEqual(
            model_sync._effort_names(["low", "max"], allow_max=True),
            ("low", "max"),
        )

    def test_retired_source_addition_is_not_a_probe_candidate(self):
        # B0: a retired source (Qoder Global) is not in PROBE_LEVELS, so its
        # addition can never become a billed probe candidate.
        binding = Binding(
            "qoder_global", "ZCode", "local:qoder-global",
            "Qoder（国际）", "（qoder_Global）")
        model = CatalogModel("qoder_global", "new-global-model")
        state = empty_state()
        state["initializedBindings"] = [binding.key]
        plan = SyncPlan(changes=[SyncChange(
            binding, "add", model.model_id, "new global model", model=model)])

        candidates = model_sync._new_model_candidates(plan, state)

        self.assertEqual(candidates, {"um": set()})

    def test_transient_http_failure_stays_pending(self):
        catalogs = {"um": [CatalogModel("um", "new-model")], "qoder_cn": []}
        state = empty_state()

        with mock.patch.object(model_sync, "_get_json", return_value={"remaining": 999}), \
                mock.patch.object(model_sync, "_post_json", return_value={
                    "status": 503, "body": {"error": "temporary"}}):
            model_sync.probe_new_models(
                catalogs, state, {"um": {"new-model"}, "qoder_cn": set()})

        self.assertEqual(state["probes"]["um|new-model"]["status"], "pending")
        self.assertEqual(state["probes"]["um|new-model"]["attempts"], 1)

    def test_success_without_reasoning_evidence_is_ambiguous(self):
        catalogs = {"um": [CatalogModel("um", "new-model")], "qoder_cn": []}
        state = empty_state()

        with mock.patch.object(model_sync, "_get_json", return_value={"remaining": 999}), \
                mock.patch.object(model_sync, "_post_json", return_value={
                    "status": 200, "body": {"choices": [{"message": {"content": "323"}}]}}):
            model_sync.probe_new_models(
                catalogs, state, {"um": {"new-model"}, "qoder_cn": set()})

        self.assertEqual(state["probes"]["um|new-model"]["status"], "ambiguous")

    def test_retired_source_is_never_probed(self):
        # B0: a retired source catalog is not part of the default probe loop
        # (and its old token file is never read).
        catalogs = {"um": [], "qoder_cn": [CatalogModel("qoder_cn", "new-model")]}
        state = empty_state()

        with mock.patch.object(model_sync, "_get_json") as get, \
                mock.patch.object(model_sync, "_post_json") as post:
            notices = model_sync.probe_new_models(
                catalogs, state, {"um": set(), "qoder_cn": {"new-model"}})

        get.assert_not_called()
        post.assert_not_called()
        self.assertEqual(notices, [])
        self.assertNotIn("qoder_cn|new-model", state["probes"])


class TestPinManagement(unittest.TestCase):
    """Rev8 契约 1（2026-10-02）：固定（pin）机制整体移除后的诚实边界。"""

    def test_pin_write_path_is_gone(self):
        self.assertFalse(hasattr(model_sync, "update_pin"))

    def test_cli_pin_arguments_are_gone(self):
        # 模块绝对路径：discover 从仓库根跑，相对 "model_sync/sync.py" 会解析到
        # 仓库根（文件打不开），测不到 argparse 的拒绝语义。
        proc = subprocess.run(
            [sys.executable, model_sync.__file__, "--pin",
             "um|DSH|openwebui|model-a"], capture_output=True, timeout=60)
        self.assertEqual(2, proc.returncode)
        self.assertIn("unrecognized arguments",
                      proc.stderr.decode("utf-8", "replace"))

    def test_load_state_migrates_v2_pinned_to_v3(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_file = root / "state.json"
            legacy = json.dumps({
                "version": 2, "initializedBindings": [],
                "managed": {"um|DSH|openwebui": {"m": {"profile": {}}}},
                "pinned": ["um|DSH|openwebui|model-a"],
                "catalogCounts": {"um": 5}, "pending": [],
                "probes": {}, "retirementWatch": {},
                "contextProbes": {}, "contextOverrides": {},
            }, ensure_ascii=False)
            state_file.write_text(legacy, encoding="utf-8")
            with mock.patch.object(model_sync, "STATE_FILE", state_file):
                migrated = model_sync.load_state()
            self.assertEqual(4, migrated["version"])
            self.assertNotIn("pinned", migrated)
            self.assertEqual(
                {"um|DSH|openwebui": {"m": {"profile": {}}}},
                migrated["managed"])
            self.assertEqual({"um": 5}, migrated["catalogCounts"])
            # 幂等：重复迁移不再变化；磁盘文件保持 v2 原样（读路径不落盘）。
            self.assertEqual(migrated, model_sync.migrate_state(migrated))
            self.assertEqual(legacy, state_file.read_text(encoding="utf-8"))


class TestRetiredCatalogRequest(unittest.TestCase):
    def test_retired_catalog_request_is_never_sent(self):
        # B0: the retired 4141/8095/8097/8320 model lists belong to retired
        # sources; the default collect must neither request them nor read any
        # old token file.
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "tok"
            token_file.write_text("g-token", encoding="utf-8")
            with mock.patch.object(
                    model_sync, "_get_json", return_value={"data": []}) as get:
                catalogs, _ = model_sync.collect_catalogs()
            urls = [c.args[0] for c in get.call_args_list if c.args]
            self.assertEqual(urls, [model_sync.UM_MODELS_URL])
            self.assertEqual(sorted(catalogs), ["um"])


class TestRetiredCatalogHelpers(unittest.TestCase):
    """B0 (2026-11-01): the retired catalog shaping helpers are gone.

    Qoder CN / Qoder Global catalog shaping is no longer part of the default
    sync assembly; a caller cannot reach it through the module either.
    """

    def test_retired_catalog_helpers_are_not_exported(self):
        for name in ("qoder_cn_catalog_entries",
                     "qoder_global_catalog_entries",
                     "QODER_GLOBAL_TIER_MODEL_IDS"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(model_sync, name))


class TestProviderConfigSync(unittest.TestCase):
    BINDING = Binding("src", "ZCode", "local:a", "A", "")

    def _store(self):
        return {
            "schemaVersion": 1,
            "config": {
                "providerOrder": ["local:a", "local:b"],
                "providerConfigRules": {"providerRules": [
                    {"providerId": "local:a", "providerName": "A", "enabled": True,
                     "config": {"group": "standard-personal",
                                "personalModelIds": ["old1", "old2"],
                                "modelOrder": ["old1", "old2"]}},
                    {"providerId": "local:b", "providerName": "B",
                     "config": {"personalModelIds": ["x"], "modelOrder": ["x"]}},
                ]},
                "modelConfigRules": {"providerModelRules": [
                    {"modelId": "old1", "config": {"properties": {"contextWindow": 99}},
                     "providerId": "local:a"},
                    {"modelId": "old2", "config": {"properties": {}},
                     "providerId": "local:a"},
                    {"modelId": "x", "config": {"properties": {"contextWindow": 1}},
                     "providerId": "local:b"},
                ]},
            },
        }

    def _catalog(self):
        return [
            CatalogModel("src", "New1", context=200000),
            CatalogModel("src", "Hidden", context=100000, publish=False),
        ]

    def test_aligns_whitelist_model_rules_and_keeps_others(self):
        raw = json.dumps(self._store(), ensure_ascii=False)
        out = json.loads(model_sync.render_provider_config_sync(
            raw, {"src": self._catalog()}, [self.BINDING],
            managed={"src|ZCode|local:a": {"old1", "old2"}}))
        rule = out["config"]["providerConfigRules"]["providerRules"][0]
        self.assertEqual(rule["config"]["personalModelIds"], ["New1"])
        self.assertEqual(rule["config"]["modelOrder"], ["New1"])
        own = [r for r in out["config"]["modelConfigRules"]["providerModelRules"]
               if r["providerId"] == "local:a"]
        self.assertEqual([r["modelId"] for r in own], ["New1"])
        self.assertEqual(own[0]["config"]["properties"]["contextWindow"], 200000)
        other = out["config"]["providerConfigRules"]["providerRules"][1]
        self.assertEqual(other["config"]["personalModelIds"], ["x"])
        other_rules = [r for r in out["config"]["modelConfigRules"]["providerModelRules"]
                       if r["providerId"] == "local:b"]
        self.assertEqual(len(other_rules), 1)
        self.assertEqual(other_rules[0]["config"]["properties"]["contextWindow"], 1)

    def test_publishable_only_and_no_pin_kept(self):
        """Rev8 B8-2：pinned keep-set 已移除——目录缺失的受管 id 不再被白名单
        保留（按正常受管规则离开），手工非受管条目原样保留。"""
        raw = json.dumps(self._store(), ensure_ascii=False)
        out = json.loads(model_sync.render_provider_config_sync(
            raw, {"src": self._catalog()}, [self.BINDING],
            managed={"src|ZCode|local:a": {"old1", "old2"}}))
        rule = out["config"]["providerConfigRules"]["providerRules"][0]
        # old1/old2 都是受管且本轮目录缺失：白名单只留目录内 New1。
        self.assertEqual(rule["config"]["personalModelIds"], ["New1"])

    def test_manual_extras_follow_the_plan_managed_set(self):
        """契约变更（2026-09-26，维护者决定「下线直接移除」）。

        旧用例名 ``test_manual_extras_are_untouched``：受管 provider 里不在受管
        登记表的条目被永久保留 —— 结果是 UM 目录早已下线的 model-a 一直留在
        provider_config.json 的白名单/排序/模型规则里，维护者报「过期模型不删除」。
        新契约：该来源本轮取得有效目录（非空、无重复、通过骤降阈值）时，计划把
        缺失条目放进受管集合，渲染层就把它们从 personalModelIds / modelOrder /
        模型规则里清掉；计划没碰的条目（其他 provider、未选中范围、观察期）不动。
        这里 old1 在受管集合里（计划确认退役，目录里也已不含它），Custom 不在
        受管集合里（计划本轮没碰它）。

        规则条目与白名单同一条离开规则（2026-09-26 审查阻塞修复）：只有计划确实
        退役的 id 才离开规则表。``old2`` 是规则库独有、计划没碰过的条目（它不在
        受管集合里），因此它的条目原样留在原位 —— 旧实现按过滤后的 target 重建
        整块规则，会把它一并删掉，这正是范围同步删掉范围外条目的同一类缺陷。
        """
        store = self._store()
        rule = store["config"]["providerConfigRules"]["providerRules"][0]
        rule["config"]["personalModelIds"] = ["Custom", "old1"]
        rule["config"]["modelOrder"] = ["Custom", "old1"]
        store["config"]["modelConfigRules"]["providerModelRules"].append(
            {"modelId": "Custom", "config": {"properties": {}},
             "providerId": "local:a"})
        untouched_entry = next(
            r for r in store["config"]["modelConfigRules"]["providerModelRules"]
            if r["modelId"] == "old2")
        raw = json.dumps(store, ensure_ascii=False)
        catalog = [CatalogModel("src", "New1", context=200000)]
        out = json.loads(model_sync.render_provider_config_sync(
            raw, {"src": catalog}, [self.BINDING],
            managed={"src|ZCode|local:a": {"old1"}}))
        rule = out["config"]["providerConfigRules"]["providerRules"][0]
        self.assertEqual(rule["config"]["personalModelIds"],
                         ["Custom", "New1"])
        self.assertEqual(rule["config"]["modelOrder"], ["Custom", "New1"])
        own = [r for r in out["config"]["modelConfigRules"]
               ["providerModelRules"] if r["providerId"] == "local:a"]
        # 受管并本轮退役的 old1 离开；计划没碰的 old2 原样留在原位（整条条目
        # 逐字段不变）；白名单里的 Custom 照常重建；目录新增的 New1 追加在末尾。
        self.assertEqual([r["modelId"] for r in own],
                         ["old2", "Custom", "New1"])
        self.assertEqual(own[0], untouched_entry)
        # 其他 provider 的条目与规则一条不动。
        other = out["config"]["providerConfigRules"]["providerRules"][1]
        self.assertEqual(other["config"]["personalModelIds"], ["x"])
        other_rules = [r for r in out["config"]["modelConfigRules"]
                       ["providerModelRules"] if r["providerId"] == "local:b"]
        self.assertEqual([r["modelId"] for r in other_rules], ["x"])

    def test_later_binding_does_not_revert_an_earlier_rule_cleanup(self):
        # 多 provider 时，后一个 provider 的规则重建不能把前一个 provider
        # 的模型规则清理还原（旧实现每个 provider 都从原文档重建整张列表）。
        binding_c = Binding("src", "ZCode", "local:c", "C", "")
        store = self._store()
        store["config"]["providerConfigRules"]["providerRules"].append(
            {"providerId": "local:c", "providerName": "C", "enabled": True,
             "config": {"personalModelIds": ["c-old"],
                         "modelOrder": ["c-old"]}})
        store["config"]["modelConfigRules"]["providerModelRules"].append(
            {"modelId": "c-old", "config": {"properties": {}},
             "providerId": "local:c"})
        raw = json.dumps(store, ensure_ascii=False)
        catalog = [CatalogModel("src", "New1", context=200000)]
        out = json.loads(model_sync.render_provider_config_sync(
            raw, {"src": catalog}, [self.BINDING, binding_c],
            managed={"src|ZCode|local:a": {"old1", "old2"},
                       binding_c.key: {"c-old"}}))
        rules = {r["providerId"]: r["config"] for r in
                 out["config"]["providerConfigRules"]["providerRules"]}
        self.assertEqual(rules["local:a"]["personalModelIds"], ["New1"])
        self.assertEqual(rules["local:c"]["personalModelIds"], ["New1"])
        own = [r["modelId"] for r in
               out["config"]["modelConfigRules"]["providerModelRules"]]
        self.assertNotIn("old1", own)
        self.assertNotIn("c-old", own)
        self.assertEqual(sorted(own),
                         ["New1", "New1", "x"])

    def test_aligned_document_is_stable(self):
        raw = json.dumps(self._store(), ensure_ascii=False)
        once = model_sync.render_provider_config_sync(
            raw, {"src": self._catalog()}, [self.BINDING])
        twice = model_sync.render_provider_config_sync(
            once, {"src": self._catalog()}, [self.BINDING])
        self.assertEqual(json.loads(once), json.loads(twice))

    def test_missing_catalog_leaves_store_untouched(self):
        raw = json.dumps(self._store(), ensure_ascii=False)
        out = model_sync.render_provider_config_sync(raw, {}, [self.BINDING])
        self.assertEqual(json.loads(out), json.loads(raw))

    def test_rule_absent_leaves_store_untouched(self):
        store = self._store()
        store["config"]["providerConfigRules"]["providerRules"] = []
        raw = json.dumps(store, ensure_ascii=False)
        out = model_sync.render_provider_config_sync(
            raw, {"src": self._catalog()}, [self.BINDING])
        self.assertEqual(json.loads(out), json.loads(raw))


class TestScopedRuleStoreIsolation(unittest.TestCase):
    """范围同步只触碰本次写入范围内的条目（2026-09-26 审查阻塞 2）。

    审查形状：规则表里有 A（受管且在目录）与 B（只在规则表、未被选中），以
    ``model_filter={source: [A]}`` 跑规则库渲染 —— B 的规则条目必须仍在、A 照常
    重建、条目顺序稳定。

    ``render_provider_config_sync`` 没有 model_filter 参数：它拿到的就是
    ``_provider_config_candidate`` 在同一个 filter 下算出来的输入（目录与受管集合
    都已按该 filter 过滤，受管集合还减去了观察期条目）。所以下面既按这个形状
    直接调渲染层，也走真实 ``run_sync`` 的 scope 路径核对端到端结果。
    """

    UM = Binding("um", "ZCode", "local:openwebui", "UM", "（UM）")
    DSH_UM = Binding("um", "DSH", "openwebui", "UM", "（UM）")
    RENDER = Binding("src", "ZCode", "local:a", "A", "")

    # ---- 规则库渲染层（审查给的形状） ----

    @staticmethod
    def _render_store(whitelist, rule_ids, order=None, provider="local:a"):
        """规则库：白名单/排序取 whitelist，模型规则条目取 rule_ids 顺序。"""
        return {
            "schemaVersion": 1,
            "config": {
                "providerConfigRules": {"providerRules": [
                    {"providerId": provider, "providerName": "A",
                     "enabled": True,
                     "config": {"personalModelIds": list(whitelist),
                                "modelOrder": list(order or whitelist)}},
                    # 本 pass 不拥有的 provider：一条都不许动。
                    {"providerId": "local:b", "providerName": "B",
                     "config": {"personalModelIds": ["x"],
                                "modelOrder": ["x"]}},
                ]},
                "modelConfigRules": {"providerModelRules": [
                    {"modelId": mid, "config": {"properties": dict(props)},
                     "providerId": provider} for mid, props in rule_ids] + [
                    {"modelId": "x",
                     "config": {"properties": {"contextWindow": 1}},
                     "providerId": "local:b"},
                ]},
            },
        }

    @staticmethod
    def _own(doc, provider="local:a"):
        return [r for r in doc["config"]["modelConfigRules"]
                ["providerModelRules"] if r["providerId"] == provider]

    @staticmethod
    def _whitelist(doc):
        rule = doc["config"]["providerConfigRules"]["providerRules"][0]
        return (rule["config"]["personalModelIds"], rule["config"]["modelOrder"])

    def _render(self, raw, *, managed, catalog_ids=("A",), context=200000):
        # 渲染层夹具（R6/D4 2026-10-10）：这里显式声明「无上下文保留」——
        # 夹具测的是范围/顺序/条目保留机制，规则库当前值即上次发布值；
        # 无保留信息时渲染器对已有客户端值按 fail-closed 原样保留。
        return json.loads(model_sync.render_provider_config_sync(
            raw,
            {"src": [CatalogModel("src", mid, context=context)
                     for mid in catalog_ids]},
            [self.RENDER],
            managed={"src|ZCode|local:a": set(managed)},
            context_holds={"src|ZCode|local:a": set()}))

    def test_scoped_render_keeps_a_rule_only_entry_outside_the_selection(self):
        # A 受管且在目录（被选中）→ 照常重建；B 只在规则表、未被选中 →
        # 条目原样留在原位，绝不因为「不在 target 里」被删掉。
        store = self._render_store(["A"], [("B", {"contextWindow": 222}),
                                        ("A", {"contextWindow": 111})])
        raw = json.dumps(store, ensure_ascii=False)
        out = self._render(raw, managed={"A"})
        self.assertEqual(self._whitelist(out), (["A"], ["A"]))
        own = self._own(out)
        self.assertEqual([r["modelId"] for r in own], ["B", "A"])
        self.assertEqual(own[0], store["config"]["modelConfigRules"]
                         ["providerModelRules"][0])
        self.assertEqual(own[1]["config"]["properties"],
                         {"contextWindow": 200000})
        # 本 pass 不拥有的 provider 的条目与规则一条不动。
        other = out["config"]["modelConfigRules"]["providerModelRules"][-1]
        self.assertEqual(other["providerId"], "local:b")
        self.assertEqual(other["config"]["properties"], {"contextWindow": 1})

    def test_rebuild_leaves_an_unpublished_entry_byte_identical(self):
        # 只被白名单保留、本轮目录没有发布的条目（未选中/固定/手动附加）：
        # 整条逐字段原样不动——不新增 properties、不改键序、不动其他 config 键
        # （2026-09-26 复核：范围外原样不动必须严格成立）。
        store = self._render_store(["A", "B"],
                                  [("A", {"contextWindow": 111}),
                                   ("B", {"contextWindow": 222})])
        entries = store["config"]["modelConfigRules"]["providerModelRules"]
        entries[1]["config"] = {"enabled": True}
        before = json.loads(json.dumps(entries[1]))
        out = self._render(json.dumps(store, ensure_ascii=False),
                           managed=set(), catalog_ids=("A",))
        self.assertEqual(self._whitelist(out), (["A", "B"], ["A", "B"]))
        rebuilt = self._own(out)
        self.assertEqual([r["modelId"] for r in rebuilt], ["A", "B"])
        self.assertEqual(rebuilt[1], before)
        self.assertEqual(rebuilt[1]["config"], {"enabled": True})
        self.assertEqual(rebuilt[0]["config"]["properties"],
                         {"contextWindow": 200000})

    def test_scoped_render_appends_new_ids_after_the_kept_entries(self):
        # 顺序稳定：范围外的 B 留在原位（不因为重建被挪到末尾），目录新增的
        # New 追加在末尾。
        store = self._render_store(["A"], [("B", {"contextWindow": 222}),
                                        ("A", {"contextWindow": 111})])
        out = self._render(json.dumps(store, ensure_ascii=False),
                           managed={"A"}, catalog_ids=("A", "New"))
        self.assertEqual(self._whitelist(out), (["A", "New"], ["A", "New"]))
        self.assertEqual([r["modelId"] for r in self._own(out)],
                         ["B", "A", "New"])

    def test_rebuild_keeps_the_entrys_other_config_keys(self):
        # 重建只对齐 properties：条目 config 里的其他键（客户端自己的开关，
        # 如 enabled）必须保留，键序也保持原样（2026-09-26 复核 F1）。
        store = self._render_store(["A"], [("A", {"contextWindow": 111})])
        entry = store["config"]["modelConfigRules"]["providerModelRules"][0]
        entry["config"]["enabled"] = True
        out = self._render(json.dumps(store, ensure_ascii=False), managed={"A"})
        rebuilt = self._own(out)[0]
        self.assertEqual(rebuilt["config"]["enabled"], True)
        self.assertEqual(rebuilt["config"]["properties"],
                         {"contextWindow": 200000})
        self.assertEqual(list(rebuilt["config"].keys()), ["properties", "enabled"])

    def test_scoped_render_keeps_an_unselected_whitelist_id_in_place(self):
        # B 同时在白名单与规则表里但未被选中：白名单与条目都留在原位。
        store = self._render_store(["B", "A"], [("B", {"contextWindow": 222}),
                                              ("A", {"contextWindow": 111})])
        out = self._render(json.dumps(store, ensure_ascii=False),
                           managed={"A"})
        self.assertEqual(self._whitelist(out), (["B", "A"], ["B", "A"]))
        self.assertEqual([r["modelId"] for r in self._own(out)], ["B", "A"])
        self.assertEqual(self._own(out)[0]["config"]["properties"],
                         {"contextWindow": 222})

    def test_scoped_render_still_retires_a_confirmed_id(self):
        # C 被本轮选中、目录已不含它：计划确认退役 → 白名单与条目一起离开；
        # 未被选中的 B 仍原样保留。
        store = self._render_store(
            ["A", "C"], [("B", {"contextWindow": 222}),
                         ("A", {"contextWindow": 111}),
                         ("C", {"contextWindow": 333})])
        out = self._render(json.dumps(store, ensure_ascii=False),
                           managed={"A", "C"})
        self.assertEqual(self._whitelist(out), (["A"], ["A"]))
        own = self._own(out)
        self.assertEqual([r["modelId"] for r in own], ["B", "A"])
        self.assertNotIn("C", [r["modelId"] for r in own])

    def test_scoped_render_leaves_a_watched_id_alone(self):
        # 观察期：受管集合已减去仍待第二次证据的 id（调用方如此传入），
        # 该 id 的白名单与条目都不动。
        store = self._render_store(["A", "watched"],
                                  [("watched", {"contextWindow": 111}),
                                   ("A", {"contextWindow": 111})])
        out = self._render(json.dumps(store, ensure_ascii=False),
                           managed={"A"})
        self.assertEqual(self._whitelist(out),
                         (["A", "watched"], ["A", "watched"]))
        self.assertEqual([r["modelId"] for r in self._own(out)],
                         ["watched", "A"])
        self.assertEqual(self._own(out)[0]["config"]["properties"],
                         {"contextWindow": 111})

    def test_scoped_render_is_idempotent_on_replay(self):
        # 幂等重放：同一份输入再渲染一次（以及拿输出再渲染）逐字节不变，
        # 范围外的条目不会被反复搬动或反复清理。
        store = self._render_store(["A"], [("B", {"contextWindow": 222}),
                                        ("A", {"contextWindow": 111})])
        raw = json.dumps(store, ensure_ascii=False)
        once = model_sync.render_provider_config_sync(
            raw, {"src": [CatalogModel("src", "A", context=200000)]},
            [self.RENDER], managed={"src|ZCode|local:a": {"A"}})
        twice = model_sync.render_provider_config_sync(
            once, {"src": [CatalogModel("src", "A", context=200000)]},
            [self.RENDER], managed={"src|ZCode|local:a": {"A"}})
        self.assertEqual(once, twice)

    def test_empty_catalog_leaves_store_untouched(self):
        # 目录为空绝不删：渲染层自己的边界也要拦（调用方不会传空目录，
        # validate_catalog 先拒）。
        store = self._render_store(["A"], [("B", {"contextWindow": 222}),
                                        ("A", {"contextWindow": 111})])
        raw = json.dumps(store, ensure_ascii=False)
        out = self._render(raw, managed={"A", "B"}, catalog_ids=())
        self.assertEqual(out, store)

    # ---- 真实 run_sync 的 scope 路径 ----

    def _run(self, root, *, catalog, store, managed, scope=None,
             zcode_models=None, dsh_models=None, watch=None,
             seed=True, zcode_running=False):
        """隔离双存储 fixture：主配置 + 规则库 + 受管状态 + 目录快照。

        ``seed=False`` 时不再重写两份客户端配置与状态（重放那一轮读的就是
        上一轮写下的内容），与真实路径一致。
        """
        store_path = root / "provider_config.json"
        zcode = root / "config.json"
        state_file = root / "state.json"
        dsh = root / "settings.yaml"
        if seed:
            store_path.write_text(json.dumps(store, ensure_ascii=False),
                                 encoding="utf-8")
            zcode.write_text(json.dumps({"provider": {"local:openwebui": {
                "name": "UM", "models": {
                    mid: {"name": mid + "（UM）"}
                    for mid in (zcode_models or [])}}}},
                ensure_ascii=False), encoding="utf-8")
            lines = ["llm-pi-ai:", "  providers:", "    openwebui:",
                     "      displayName: UM", "      models:"]
            for mid in dsh_models or []:
                lines += ["        - id: %s" % mid,
                          "          name: %s（UM）" % mid,
                          "          contextWindow: 200000",
                          "          maxTokens: 16384"]
            dsh.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # Rev11 单文件发布：消费快照与发布使用同一权威文件（规则库）。
        # 重放那一轮读的是上一轮写进 provider_config.json 的白名单/规则，
        # config.json 只补规则库不携带的 profile 细节（name）——与真实
        # collect_consumers 的合并语义同源。
        payload = zcode.read_text(encoding="utf-8")
        legacy_models = json.loads(payload)[
            "provider"]["local:openwebui"]["models"]
        store_doc = json.loads(store_path.read_text(encoding="utf-8"))
        store_ids: list[str] = []
        store_profiles: dict[str, dict] = {}
        for rule in store_doc["config"]["providerConfigRules"]["providerRules"]:
            if rule["providerId"] != "local:openwebui":
                continue
            store_ids = list(rule["config"]["personalModelIds"])
        for rule in store_doc["config"]["modelConfigRules"]["providerModelRules"]:
            if rule["providerId"] != "local:openwebui":
                continue
            profile = dict(rule.get("config", {}).get("properties") or {})
            store_profiles[str(rule["modelId"])] = profile
        if seed:
            state_doc = empty_state()
            state_doc["initializedBindings"] = [self.UM.key]
            # R6/D4 三方比较夹具（2026-10-10）：规则库当前值就是本同步上次
            # 实际发布的基准——上下文对齐照常发布（本类测 scope 隔离与
            # 幂等重放，不是冲突保护；冲突用例见 TestContextPublishProtection）。
            state_doc["managed"] = {self.UM.key: {
                mid: {"profile": {},
                      "published": (
                          {"context": store_profiles[mid]["contextWindow"],
                           "at": "published-time"}
                          if isinstance(store_profiles.get(mid), dict)
                          and isinstance(store_profiles[mid].get("contextWindow"),
                                         int)
                          and not isinstance(
                              store_profiles[mid]["contextWindow"], bool)
                          else {"context": None,
                                "at": "published-time"})}
                for mid in managed}}
            if watch:
                state_doc["retirementWatch"] = watch
            state_file.write_text(json.dumps(state_doc, ensure_ascii=False),
                                  encoding="utf-8")
        consumers = {
            self.UM.key: {"providerName": "UM", "models": {
                mid: {**dict(legacy_models.get(mid) or
                            {"name": mid + "（UM）"}),
                      **store_profiles.get(mid, {})}
                for mid in store_ids}},
            self.DSH_UM.key: {"providerName": "UM", "models": {
                mid: {"id": mid, "name": mid + "（UM）"}
                for mid in (dsh_models or [])}},
        }
        with mock.patch.object(model_sync, "collect_catalogs",
                               return_value=(catalog, [])), \
            mock.patch.object(model_sync, "collect_consumers",
                              return_value=(consumers, dsh.read_text("utf-8"),
                                             payload)), \
            mock.patch.object(model_sync, "DSH_YAML", dsh), \
            mock.patch.object(model_sync, "zcode_running",
                              return_value=zcode_running), \
            mock.patch.object(model_sync, "zcode_hot_sync_admission",
                              return_value=(False, "测试：未准入")), \
            mock.patch.object(model_sync, "ZCODE_JSON", zcode), \
            mock.patch.object(model_sync, "ZCODE_PROVIDER_CONFIG", store_path), \
            mock.patch.object(model_sync, "STATE_FILE", state_file), \
            mock.patch.object(model_sync, "RESULT_FILE",
                              root / "result.json"), \
            mock.patch.object(model_sync, "PREVIEW_FILE",
                              root / "preview.json"), \
            mock.patch.object(model_sync, "HISTORY_FILE",
                              root / "history.jsonl"), \
            mock.patch.object(model_sync, "BACKUP_DIR", root / "backups"):
            result = model_sync.run_sync(
                apply=True, run_probes=False, confirm=False, scope=scope)
        return result, store_path, state_file

    def test_scoped_run_keeps_rule_entries_outside_the_selection(self):
        # 真实 scope 路径：只选中 um-a。um-b 只在规则表、未被选中 → 条目仍在、
        # 不进主配置、不进受管状态；um-a 照常重建并写进主配置。
        store = self._render_store(["um-a"], [("um-b", {"contextWindow": 222}),
                                            ("um-a", {"contextWindow": 111})],
                                  provider="local:openwebui")
        catalog = {"um": [CatalogModel("um", "um-a", context=200000)]}
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result, store_path, state_file = self._run(
                root, catalog=catalog, store=store, managed=["um-a"],
                scope={"models": {"um": ["um-a"]}})
            self.assertTrue(any("发布 um-a" in line
                               for line in result["applied"]))
            after = json.loads(store_path.read_text("utf-8"))
            self.assertEqual(self._whitelist(after), (["um-a"], ["um-a"]))
            own = self._own(after, "local:openwebui")
            self.assertEqual([r["modelId"] for r in own], ["um-b", "um-a"])
            self.assertEqual(own[0]["config"]["properties"],
                             {"contextWindow": 222})
            self.assertEqual(own[1]["config"]["properties"],
                             {"contextWindow": 200000})
            # Rev11 单文件发布：模型面只写 provider_config.json，config.json
            # 不是第二个实时权威——它保持夹具种下的空模型面不变。
            main_after = json.loads((root / "config.json").read_text("utf-8"))
            self.assertEqual(
                sorted(main_after["provider"]["local:openwebui"]["models"]),
                [])
            state = json.loads(state_file.read_text("utf-8"))
            self.assertEqual(sorted(state["managed"][self.UM.key]), ["um-a"])

    def test_scoped_run_is_idempotent_on_replay(self):
        # 幂等重放：同一 scope 再跑一轮，ZCode 侧无变化、规则库逐字节不变。
        store = self._render_store(["um-a"], [("um-b", {"contextWindow": 222}),
                                            ("um-a", {"contextWindow": 111})],
                                  provider="local:openwebui")
        catalog = {"um": [CatalogModel("um", "um-a", context=200000)]}
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first, store_path, _ = self._run(
                root, catalog=catalog, store=store, managed=["um-a"],
                scope={"models": {"um": ["um-a"]}})
            after_first = store_path.read_text("utf-8")
            second, _, _ = self._run(
                root, catalog=catalog, store=store, managed=["um-a"],
                scope={"models": {"um": ["um-a"]}}, seed=False)
            self.assertEqual(
                [d for d in second["applied"] if d.startswith("ZCode")], [])
            self.assertFalse(second["zcodeChanged"])
            self.assertEqual(store_path.read_text("utf-8"), after_first)
            self.assertTrue(first["zcodeChanged"])

    def test_scoped_run_still_retires_a_selected_managed_id(self):
        # 退役仍生效：um-gone 被本轮选中、目录已不含它、观察期已给出第二次
        # 独立证据 → 白名单与规则条目一起离开；未被选中的 um-b 仍原样保留。
        store = self._render_store(
            ["um-a", "um-gone"],
            [("um-b", {"contextWindow": 222}),
             ("um-gone", {"contextWindow": 111}),
             ("um-a", {"contextWindow": 111})],
            provider="local:openwebui")
        stamp = (datetime.now(timezone.utc) -
                 timedelta(hours=2)).isoformat()
        watch = {self.UM.key + "|um-gone": {
            "firstMissing": stamp, "lastMissing": stamp,
            "evidenceRuns": 1}}
        catalog = {"um": [CatalogModel("um", "um-a", context=200000)]}
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result, store_path, state_file = self._run(
                root, catalog=catalog, store=store,
                managed=["um-a", "um-gone"], watch=watch,
                zcode_models=["um-gone"],
                scope={"models": {"um": ["um-a", "um-gone"]}})
            applied = "\n".join(result["applied"])
            self.assertIn("退役 um-gone", applied)
            after = json.loads(store_path.read_text("utf-8"))
            self.assertEqual(self._whitelist(after), (["um-a"], ["um-a"]))
            self.assertEqual([r["modelId"]
                              for r in self._own(after, "local:openwebui")],
                             ["um-b", "um-a"])
            # Rev11 单文件发布：config.json 保持夹具原样（不被写，也不是
            # 第二个实时权威）；退役只落在规则库的白名单/规则里。
            main_after = json.loads((root / "config.json").read_text("utf-8"))
            self.assertIn("um-gone",
                          main_after["provider"]["local:openwebui"]["models"])
            state = json.loads(state_file.read_text("utf-8"))
            self.assertNotIn("um-gone", state["managed"][self.UM.key])


class TestProviderConfigCandidateInRunSync(unittest.TestCase):
    BINDING = Binding("um", "ZCode", "local:openwebui",
                      "UM", "（UM）")

    def _store_raw(self):
        return json.dumps({
            "schemaVersion": 1,
            "config": {
                "providerConfigRules": {"providerRules": [
                    {"providerId": "local:openwebui", "providerName": "UM",
                     "config": {"personalModelIds": ["old1"],
                                "modelOrder": ["old1"]}},
                ]},
                "modelConfigRules": {"providerModelRules": [
                    {"modelId": "old1", "config": {"properties": {}},
                     "providerId": "local:openwebui"},
                ]},
            },
        }, ensure_ascii=False)

    def _run(self, tmp_root, zcode_running, state_doc=None, scope=None):
        zcode_json = tmp_root / "config.json"
        pcfg = tmp_root / "provider_config.json"
        state_file = tmp_root / "state.json"
        result_file = tmp_root / "result.json"
        backup_dir = tmp_root / "backups"
        # old1 is present in the client config: retirement evidence only
        # accumulates for models the client still serves (there is a real
        # remove to plan once the second independent evidence confirms it).
        zcode_payload = json.dumps(
            {"provider": {"local:openwebui": {
                "name": "UM", "source": "custom",
                "options": {}, "models": {"old1": {"name": "old1"}}}}},
            ensure_ascii=False)
        zcode_json.write_text(zcode_payload, encoding="utf-8")
        pcfg.write_text(self._store_raw(), encoding="utf-8")
        if state_doc is None:
            # old1 was published by an earlier sync run, so its FIRST absence
            # only arms the retirement watch (pending review, no change).
            state_doc = {
                "version": 2,
                "initializedBindings": [self.BINDING.key],
                "managed": {self.BINDING.key: {"old1": {"profile": {}}}},
            }
        state_file.write_text(json.dumps(state_doc, ensure_ascii=False),
                              encoding="utf-8")
        catalog = [CatalogModel("um", "New1", context=200000)]
        consumers = {self.BINDING.key: {
            "providerName": "UM",
            "models": {"old1": {"name": "old1"}}}}
        with mock.patch.object(
                model_sync, "collect_catalogs",
                return_value=({"um": catalog}, [])), \
            mock.patch.object(
                model_sync, "collect_consumers",
                return_value=(consumers, "", zcode_payload)), \
            mock.patch.object(
                model_sync, "zcode_running", return_value=zcode_running), \
            mock.patch.object(model_sync, "zcode_hot_sync_admission",
                              return_value=(False, "测试：未准入")), \
            mock.patch.object(model_sync, "ZCODE_JSON", zcode_json), \
            mock.patch.object(model_sync, "ZCODE_PROVIDER_CONFIG", pcfg), \
            mock.patch.object(model_sync, "STATE_FILE", state_file), \
            mock.patch.object(model_sync, "RESULT_FILE", result_file), \
            mock.patch.object(model_sync, "HISTORY_FILE",
                              tmp_root / "history.jsonl"), \
            mock.patch.object(model_sync, "BACKUP_DIR", backup_dir):
            result = model_sync.run_sync(
                apply=True, run_probes=False, confirm=False, scope=scope)
        return result, pcfg, zcode_json

    def _watch_entry(self):
        from datetime import datetime, timedelta, timezone
        stamp = (datetime.now(timezone.utc) -
                 timedelta(hours=2)).isoformat()
        return {"firstMissing": stamp, "lastMissing": stamp,
                "evidenceRuns": 1}

    def test_first_absence_keeps_whitelist_entry_pending_review(self):
        # One valid-catalog miss = pending review: no remove change, so the
        # client config AND its whitelist entry both keep old1 (plus the new
        # catalog model is appended).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, pcfg, _ = self._run(root, zcode_running=False)
            after = json.loads(pcfg.read_text(encoding="utf-8"))
            rule = after["config"]["providerConfigRules"]["providerRules"][0]
            self.assertEqual(rule["config"]["personalModelIds"],
                             ["old1", "New1"])
            own = [r for r in after["config"]["modelConfigRules"]
                   ["providerModelRules"] if r["providerId"] == "local:openwebui"]
            self.assertEqual([r["modelId"] for r in own], ["old1", "New1"])
            self.assertEqual(result["status"], "changed")
            state = json.loads((root / "state.json").read_text("utf-8"))
            self.assertIn(self.BINDING.key + "|old1",
                          state["retirementWatch"])
            self.assertEqual(
                state["retirementWatch"][self.BINDING.key + "|old1"]
                ["evidenceRuns"], 1)

    def test_confirmed_retirement_cleans_whitelist_entry(self):
        # §6 contract: the post-plan managed set drives the whitelist, so a
        # managed model the second evidence just retired leaves
        # personalModelIds and its model rule. (Rev8: no pinned keep-set —
        # the id leaves via the managed registry alone.)
        state_doc = {
            "version": 2,
            "initializedBindings": [self.BINDING.key],
            "managed": {self.BINDING.key: {"old1": {"profile": {}}}},
            "retirementWatch": {
                self.BINDING.key + "|old1": self._watch_entry()},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, pcfg, zcode_json = self._run(root, zcode_running=False,
                                                 state_doc=state_doc)
            after = json.loads(pcfg.read_text(encoding="utf-8"))
            rule = after["config"]["providerConfigRules"]["providerRules"][0]
            self.assertEqual(rule["config"]["personalModelIds"], ["New1"])
            own = [r for r in after["config"]["modelConfigRules"]
                   ["providerModelRules"] if r["providerId"] == "local:openwebui"]
            self.assertEqual([r["modelId"] for r in own], ["New1"])
            # Rev11 单文件发布：退役与发布只落在规则库；config.json 保持
            # 夹具原样（不是第二个实时权威）——old1 仍在，New1 不写入。
            client_models = json.loads(
                zcode_json.read_text("utf-8"))["provider"][
                "local:openwebui"]["models"]
            self.assertIn("old1", client_models)
            self.assertNotIn("New1", client_models)
            self.assertEqual(result["status"], "changed")
            self.assertTrue(any("退役 old1" in line
                                for line in result["applied"]))
            state = json.loads((root / "state.json").read_text("utf-8"))
            self.assertNotIn("pinned", state)
            self.assertNotIn("old1", state["managed"][self.BINDING.key])
            self.assertNotIn(self.BINDING.key + "|old1",
                             state["retirementWatch"])

    def test_rule_store_aligned_when_zcode_closed(self):
        # Confirmed retirement variant without a pin: the rule store still
        # aligns (the sync-owned id leaves the whitelist with the client).
        state_doc = {
            "version": 2,
            "initializedBindings": [self.BINDING.key],
            "managed": {self.BINDING.key: {"old1": {"profile": {}}}},
            "retirementWatch": {
                self.BINDING.key + "|old1": self._watch_entry()},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, pcfg, _ = self._run(root, zcode_running=False,
                                        state_doc=state_doc)
            after = json.loads(pcfg.read_text(encoding="utf-8"))
            rule = after["config"]["providerConfigRules"]["providerRules"][0]
            self.assertEqual(rule["config"]["personalModelIds"], ["New1"])
            self.assertEqual(result["status"], "changed")

    def test_rule_store_deferred_while_zcode_running(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, pcfg, _ = self._run(root, zcode_running=True)
            self.assertEqual(json.loads(pcfg.read_text(encoding="utf-8")),
                             json.loads(self._store_raw()))
            self.assertEqual(result["status"], "pending")
            self.assertTrue(any("provider_config.json" in line
                                for line in result["pending"]))


class TestManagedProviderOrphanRetirement(unittest.TestCase):
    """维护者决定「下线直接移除」：受管 provider 的孤儿条目退役（run 级）。

    契约变更（2026-09-26）：旧契约把「不在受管登记表里」的条目当手动附加项
    永久保留（旧用例名 TestProviderConfigSync.test_manual_extras_are_untouched），
    结果是 UM 目录早已下线的 model-a 一直留在 provider_config.json 的白名单/
    排序/模型规则里。新契约：受管来源本轮取得有效目录（非空、无重复、通过骤降
    阈值）时，主配置与规则库里的缺失条目在同一轮计划里退役，覆盖主配置、
    personalModelIds、modelOrder、模型规则与受管状态；目录抓取失败/为空/异常
    一律不删、只报；其他 provider 与手动 provider 的条目不动；ZCode 运行时只
    登记待应用，绝不热写。
    """

    UM = Binding("um", "ZCode", "local:openwebui", "UM", "（UM）")
    CN = Binding("qoder_cn", "ZCode", "local:lingma-proxy",
                 "Qoder（国内）", "（qoder_CN）")
    OTHER = "local:custom-user"

    def _run(self, root, catalogs, *, um_models=None, cn_models=None,
             dsh_extra=None, whitelist=None, model_rules=None,
             managed=None, catalog_counts=None, apply=True,
             zcode_running=False, seed=True):
        """隔离双存储 fixture：主配置 + 规则库 + 受管状态 + 目录快照。

        主配置的 provider 模型面与 ``collect_consumers`` 返回的快照同形
        （真实路径由同一份 config.json 构造），规则库由 provider_config.json
        提供，受管状态由 state.json 提供。
        """
        zcode = root / "config.json"
        store = root / "provider_config.json"
        state_file = root / "state.json"
        # DSH 配置文件也指向临时目录：本用例绝不写真实 ~/.dsh/settings.yaml。
        dsh = root / "settings.yaml"
        dsh_lines = ["llm-pi-ai:", "  providers:", "    openwebui:",
                     "      displayName: UM", "      models:"]
        for mid in list(um_models or []) + list(dsh_extra or []):
            dsh_lines += ["        - id: %s" % mid,
                          "          name: %s（UM）" % mid,
                          "          contextWindow: 128000",
                          "          maxTokens: 16384"]
        dsh_raw = "\n".join(dsh_lines) + "\n"
        providers = {
            # DSH 绑定的 provider 是 openwebui，ZCode 侧是 local:*：两者
            # 都要有显示名，否则计划会凭空产生 provider_name 变化。
            "openwebui": ("UM", "（UM）", um_models or []),
            "local:openwebui": ("UM", "（UM）", um_models or []),
            "local:lingma-proxy": ("Qoder（国内）", "（qoder_CN）",
                                    cn_models or []),
        }
        whitelist = whitelist if whitelist is not None else {}
        model_rules = model_rules if model_rules is not None else whitelist
        if seed:
            dsh.write_text(dsh_raw, encoding="utf-8")
            zcode.write_text(json.dumps({"provider": {
                provider: {"name": name, "models": {
                    mid: {"name": mid + suffix} for mid in ids}}
                for provider, (name, suffix, ids) in providers.items()}},
                ensure_ascii=False), encoding="utf-8")
            provider_rules = [{
                "providerId": provider, "providerName": provider,
                "config": {"personalModelIds": list(ids),
                            "modelOrder": list(ids)},
            } for provider, ids in whitelist.items()]
            rule_entries = [{"modelId": mid, "config": {"properties": {}},
                              "providerId": provider}
                            for provider, ids in model_rules.items()
                            for mid in ids]
            store.write_text(json.dumps({"schemaVersion": 1, "config": {
                "providerConfigRules": {"providerRules": provider_rules},
                "modelConfigRules": {"providerModelRules": rule_entries}}},
                ensure_ascii=False), encoding="utf-8")
            state_doc = empty_state()
            state_doc["initializedBindings"] = [self.UM.key]
            state_doc["managed"] = {self.UM.key: {
                mid: {"profile": {}} for mid in (managed or [])}}
            state_doc["catalogCounts"] = catalog_counts or {}
            state_file.write_text(json.dumps(state_doc, ensure_ascii=False),
                                  encoding="utf-8")
        # 真实 collect_consumers 为每个启用绑定返回一条快照。
        consumers = {}
        for binding in model_sync.ACTIVE_BINDINGS:
            name, suffix, ids = providers.get(
                binding.provider, (None, None, []))
            if binding.consumer == "DSH":
                ids = list(ids) + list(dsh_extra or [])
            consumers[binding.key] = {
                "providerName": name,
                "models": {mid: {"id": mid, "name": mid + suffix}
                            for mid in ids}}
        payload = zcode.read_text(encoding="utf-8")
        with mock.patch.object(model_sync, "collect_catalogs",
                               return_value=(catalogs, [])), \
            mock.patch.object(model_sync, "collect_consumers",
                              return_value=(consumers, dsh_raw, payload)), \
            mock.patch.object(model_sync, "DSH_YAML", dsh), \
            mock.patch.object(model_sync, "zcode_running",
                              return_value=zcode_running), \
            mock.patch.object(model_sync, "zcode_hot_sync_admission",
                              return_value=(False, "测试：未准入")), \
            mock.patch.object(model_sync, "ZCODE_JSON", zcode), \
            mock.patch.object(model_sync, "ZCODE_PROVIDER_CONFIG", store), \
            mock.patch.object(model_sync, "STATE_FILE", state_file), \
            mock.patch.object(model_sync, "RESULT_FILE",
                              root / "result.json"), \
            mock.patch.object(model_sync, "PREVIEW_FILE",
                              root / "preview.json"), \
            mock.patch.object(model_sync, "HISTORY_FILE",
                              root / "history.jsonl"), \
            mock.patch.object(model_sync, "BACKUP_DIR", root / "backups"):
            result = model_sync.run_sync(
                apply=apply, run_probes=False, confirm=False)
        return result, zcode, store, state_file

    @staticmethod
    def _surfaces(store):
        """规则库的客户端面：白名单、排序、模型规则（provider -> ids）。"""
        cfg = json.loads(store.read_text(encoding="utf-8"))["config"]
        rules = {r["providerId"]: r.get("config") or {}
                 for r in cfg["providerConfigRules"]["providerRules"]}
        model_rules: dict[str, list[str]] = {}
        for rule in cfg["modelConfigRules"]["providerModelRules"]:
            model_rules.setdefault(rule["providerId"], []).append(
                rule["modelId"])
        return rules, model_rules

    def test_rule_store_orphan_is_planned_without_a_main_config_change(self):
        # 主配置没有 model-a（local:openwebui.models 为空），规则库里却还有：
        # 计划必须产出可预览、可事务应用的退役变化，而不是「无配置变化」。
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result, zcode, store, state_file = self._run(
                root,
                {"um": [CatalogModel("um", "um-a")]},
                um_models=[],
                whitelist={"local:openwebui": ["model-a", "um-a"]},
                model_rules={"local:openwebui": ["model-a", "um-a"]},
                managed=["um-a"])
            self.assertIn("ZCode 退役 model-a", "\n".join(result["applied"]))
            self.assertTrue(any("规则库" in line for line in result["applied"]))
            self.assertTrue(result["zcodeChanged"])
            rules, model_rules = self._surfaces(store)
            self.assertEqual(rules["local:openwebui"]["personalModelIds"],
                             ["um-a"])
            self.assertEqual(rules["local:openwebui"]["modelOrder"], ["um-a"])
            self.assertEqual(model_rules.get("local:openwebui"), ["um-a"])
            # 主配置本来就没有 model-a：不凭空写入，也不残留。
            main_after = json.loads(zcode.read_text(encoding="utf-8"))
            self.assertNotIn("model-a",
                             main_after["provider"]["local:openwebui"]["models"])
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertNotIn("model-a", state["managed"][self.UM.key])

    def test_retired_provider_entries_are_left_untouched(self):
        # B0: the retired Qoder CN provider (local:lingma-proxy) has no active
        # binding in the default assembly, so its rule-store entries are not
        # managed, retired or re-published — they stay exactly as they were.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result, zcode, store, state_file = self._run(
                root,
                {"um": [CatalogModel("um", "um-a")],
                 "qoder_cn": [CatalogModel("qoder_cn", "cn-real")]},
                um_models=["um-a"],
                cn_models=["cn-a"],
                whitelist={"local:lingma-proxy": ["cn-a", "cn-b", "cn-real"],
                            "local:openwebui": ["um-a"]},
                model_rules={"local:lingma-proxy": ["cn-a", "cn-b", "cn-real"],
                               "local:openwebui": ["um-a"]},
                managed=["um-a"])
            applied = "\n".join(result["applied"])
            self.assertNotIn("cn-a", applied)
            self.assertNotIn("cn-b", applied)
            rules, model_rules = self._surfaces(store)
            self.assertEqual(rules["local:lingma-proxy"]["personalModelIds"],
                             ["cn-a", "cn-b", "cn-real"])
            self.assertEqual(rules["local:lingma-proxy"]["modelOrder"],
                             ["cn-a", "cn-b", "cn-real"])
            self.assertEqual(model_rules.get("local:lingma-proxy"),
                             ["cn-a", "cn-b", "cn-real"])
            self.assertEqual(model_rules.get("local:openwebui"), ["um-a"])
            after = json.loads(zcode.read_text(encoding="utf-8"))
            self.assertEqual(
                sorted(after["provider"]["local:lingma-proxy"]["models"]),
                ["cn-a"])
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertNotIn("cn-a", state["managed"][self.UM.key])
            self.assertNotIn("cn-b", state["managed"][self.UM.key])

    def test_preview_shows_the_orphan_without_writing_anything(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result, _, store, _ = self._run(
                root,
                {"um": [CatalogModel("um", "um-a")]},
                um_models=["um-a"],
                whitelist={"local:openwebui": ["model-a", "um-a"]},
                model_rules={"local:openwebui": ["model-a", "um-a"]},
                managed=["um-a"], apply=False)
            self.assertTrue(any("退役 model-a" in line
                                for line in result["plan"]))
            rules, model_rules = self._surfaces(store)
            self.assertEqual(rules["local:openwebui"]["personalModelIds"],
                             ["model-a", "um-a"])
            self.assertEqual(model_rules.get("local:openwebui"),
                             ["model-a", "um-a"])

    def test_orphan_retirement_is_deferred_while_zcode_runs(self):
        # 同步器不得在 ZCode 运行时热写：两份客户端配置都不动，退役只能是
        # 待应用，并与「配置待应用」分开展示。
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result, _, store, _ = self._run(
                root,
                {"um": [CatalogModel("um", "um-a")]},
                um_models=["um-a"],
                whitelist={"local:openwebui": ["model-a", "um-a"]},
                model_rules={"local:openwebui": ["model-a", "um-a"]},
                managed=["um-a"], zcode_running=True)
            self.assertEqual(result["status"], "pending")
            # DSH 侧的正常发布不属于本用例范围，只看 ZCode 侧。
            self.assertEqual(
                [d for d in result["applied"]
                 if d.startswith("ZCode")], [])
            self.assertTrue(any("退役 model-a" in line
                                for line in result["pending"]))
            states = {item["model_id"]: item["state"]
                      for item in result["pendingRetirement"]}
            self.assertEqual(states.get("model-a"), "pendingApply")
            rules, model_rules = self._surfaces(store)
            self.assertEqual(rules["local:openwebui"]["personalModelIds"],
                             ["model-a", "um-a"])
            self.assertEqual(model_rules.get("local:openwebui"),
                             ["model-a", "um-a"])

    def test_orphan_retirement_is_idempotent_on_replay(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = dict(
                um_models=["um-a"],
                whitelist={"local:openwebui": ["model-a", "um-a"]},
                model_rules={"local:openwebui": ["model-a", "um-a"]},
                managed=["um-a"])
            catalogs = {"um": [CatalogModel("um", "um-a")]}
            first, _, store, _ = self._run(root, catalogs, **fixture)
            self.assertTrue(any("退役 model-a" in line
                                for line in first["applied"]))
            after_first = store.read_text(encoding="utf-8")
            second, _, store, _ = self._run(root, catalogs, seed=False,
                                           **fixture)
            self.assertEqual(
                [d for d in second["applied"]
                 if d.startswith("ZCode")], [])
            self.assertEqual(second["pending"], [])
            self.assertEqual(second["status"], "normal")
            self.assertEqual(store.read_text(encoding="utf-8"), after_first)

    def test_drop_threshold_blocks_orphan_retirement(self):
        # 骤降阈值先拦：上一轮 14 项、本轮 1 项 → 目录无效，孤儿不删。
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result, _, store, _ = self._run(
                root, {"um": [CatalogModel("um", "only")]},
                um_models=["um-a"],
                whitelist={"local:openwebui": ["model-a", "um-a"]},
                model_rules={"local:openwebui": ["model-a", "um-a"]},
                managed=["um-a"], catalog_counts={"um": 14})
            self.assertEqual(result["applied"], [])
            self.assertTrue(any("超过 50%" in line
                                for line in result["warnings"]))
            rules, model_rules = self._surfaces(store)
            self.assertEqual(rules["local:openwebui"]["personalModelIds"],
                             ["model-a", "um-a"])
            self.assertEqual(model_rules.get("local:openwebui"),
                             ["model-a", "um-a"])

    def test_empty_catalog_never_retires_orphans(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result, _, store, _ = self._run(
                root, {"um": []}, um_models=["um-a"],
                whitelist={"local:openwebui": ["model-a", "um-a"]},
                model_rules={"local:openwebui": ["model-a", "um-a"]},
                managed=["um-a"])
            self.assertEqual(result["applied"], [])
            self.assertTrue(any("为空" in line for line in result["warnings"]))
            rules, _ = self._surfaces(store)
            self.assertEqual(rules["local:openwebui"]["personalModelIds"],
                             ["model-a", "um-a"])

    def test_fetch_failure_never_retires_orphans(self):
        # 抓取失败（该来源本轮缺席）：不删、只报。
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result, _, store, _ = self._run(
                root, {}, um_models=["um-a"],
                whitelist={"local:openwebui": ["model-a", "um-a"]},
                model_rules={"local:openwebui": ["model-a", "um-a"]},
                managed=["um-a"])
            self.assertEqual(result["applied"], [])
            rules, model_rules = self._surfaces(store)
            self.assertEqual(rules["local:openwebui"]["personalModelIds"],
                             ["model-a", "um-a"])
            self.assertEqual(model_rules.get("local:openwebui"),
                             ["model-a", "um-a"])

    def test_other_provider_entries_are_untouched(self):
        # 其他 provider / 手动 provider 的条目不动（本轮没参与计划）。
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, _, store, _ = self._run(
                root, {"um": [CatalogModel("um", "um-a")]},
                um_models=["um-a"],
                whitelist={"local:openwebui": ["model-a", "um-a"],
                             self.OTHER: ["manual-1", "manual-2"]},
                model_rules={"local:openwebui": ["model-a", "um-a"],
                               self.OTHER: ["manual-1", "manual-2"]},
                managed=["um-a"])
            rules, model_rules = self._surfaces(store)
            self.assertEqual(rules[self.OTHER]["personalModelIds"],
                             ["manual-1", "manual-2"])
            self.assertEqual(rules[self.OTHER]["modelOrder"],
                             ["manual-1", "manual-2"])
            self.assertEqual(model_rules.get(self.OTHER),
                             ["manual-1", "manual-2"])
            self.assertEqual(rules["local:openwebui"]["personalModelIds"],
                             ["um-a"])

    def test_dsh_orphan_in_the_main_config_is_retired(self):
        # 主配置里的 DSH 孤儿（手工加入受管 provider 的模型）按同一决定
        # 退役：只有该来源本轮目录有效时才删。
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result, _, store, _ = self._run(
                root, {"um": [CatalogModel("um", "um-a")]},
                um_models=["um-a"], dsh_extra=["legacy"],
                whitelist={"local:openwebui": ["um-a"]},
                model_rules={"local:openwebui": ["um-a"]},
                managed=["um-a"])
            self.assertTrue(any("DSH 退役 legacy" in line
                                for line in result["applied"]))
            dsh_after = (root / "settings.yaml").read_text("utf-8")
            self.assertNotIn("legacy", dsh_after)
            self.assertIn("um-a", dsh_after)
            # ZCode 侧不受影响：规则库与主配置都还在。
            rules, _ = self._surfaces(store)
            self.assertEqual(rules["local:openwebui"]["personalModelIds"],
                             ["um-a"])

    def test_unreadable_rule_store_is_only_reported(self):
        # 规则库存在但无法解析：本轮不清理规则库、只报（绝不猜、绝不删）。
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = {
                "root": root,
                "catalogs": {"um": [CatalogModel("um", "um-a")]},
                "um_models": ["um-a"],
                "whitelist": {"local:openwebui": ["model-a", "um-a"]},
                "model_rules": {"local:openwebui": ["model-a", "um-a"]},
                "managed": ["um-a"],
            }
            self._run(**fixture)
            broken = (root / "provider_config.json").read_text("utf-8")
            (root / "provider_config.json").write_text(
                "{not json", encoding="utf-8")
            result, _, store, _ = self._run(**fixture, seed=False)
            self.assertEqual(
                (root / "provider_config.json").read_text("utf-8"),
                "{not json")
            self.assertTrue(any("无法解析" in line
                                for line in result["warnings"]))
            self.assertTrue(broken)

    def test_watching_absence_is_reported_as_pending_retirement(self):
        # 受管条目第一次缺失只登记观察（两次独立目录证据规则不变）：不能当
        # 成功，也不能把「目录缺失待退役」混进「配置待应用」。
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            # gone 只在规则库与受管登记表里（主配置已无它）：受管条目的
            # 第一次缺失只登记观察，规则库条目本轮保持不动。
            result, _, store, _ = self._run(
                root, {"um": [CatalogModel("um", "um-a")]},
                um_models=["um-a"],
                whitelist={"local:openwebui": ["gone", "um-a"]},
                model_rules={"local:openwebui": ["gone", "um-a"]},
                managed=["gone", "um-a"])
            self.assertEqual(
                [d for d in result["applied"]
                 if d.startswith("ZCode")], [])
            self.assertEqual(result["status"], "warning")
            self.assertEqual(result["pending"], [])
            self.assertTrue(any("待第二次独立目录证据确认退役" in line
                                for line in result["warnings"]))
            states = {item["model_id"]: item["state"]
                       for item in result["pendingRetirement"]}
            self.assertEqual(states.get("gone"), "watching")
            rules, _ = self._surfaces(store)
            self.assertIn("gone", rules["local:openwebui"]["personalModelIds"])



class TestCollectCatalogsScope(unittest.TestCase):
    def test_subset_only_requests_selected_sources(self):
        with mock.patch.object(model_sync, "_get_json",
                               return_value={"data": []}) as get:
            catalogs, warnings = model_sync.collect_catalogs(sources=["um"])
        urls = [call.args[0] for call in get.call_args_list]
        self.assertEqual(urls, [model_sync.UM_MODELS_URL])
        self.assertEqual(set(catalogs), {"um"})
        self.assertEqual(warnings, [])

    def test_default_collects_only_active_sources(self):
        with mock.patch.object(model_sync, "_get_json",
                               return_value={"data": []}) as get:
            catalogs, _ = model_sync.collect_catalogs()
        self.assertEqual(len(get.call_args_list), 1)
        self.assertEqual(set(catalogs), {"um"})
        self.assertEqual([call.args[0] for call in get.call_args_list],
                         [model_sync.UM_MODELS_URL])

    def test_retired_sources_cannot_be_selected(self):
        for source in ("m365", "qoder_cn", "qoder_global", "cliproxy"):
            with self.subTest(source=source):
                with mock.patch.object(model_sync, "_get_json") as get:
                    with self.assertRaisesRegex(ValueError, "退役来源"):
                        model_sync.collect_catalogs(sources=[source])
                    with self.assertRaisesRegex(ValueError, "已暂停"):
                        model_sync._normalize_scope({"sources": [source]})
                    with self.assertRaisesRegex(ValueError, "已暂停"):
                        model_sync._normalize_scope(
                            {"models": {source: ["model-x"]}})
                get.assert_not_called()

    def test_unknown_source_raises_value_error(self):
        with mock.patch.object(model_sync, "_get_json") as get:
            with self.assertRaises(ValueError):
                model_sync.collect_catalogs(sources=["bogus", "um"])
        get.assert_not_called()


class TestCatalogEffortProvenance(unittest.TestCase):
    """P1 (2026-09-24): every catalog row must carry WHERE its tiers came
    from. The UM directory always reads the local KNOWN_MODELS table (its
    /v1/models payload carries no reasoning_efforts). B0 (2026-11-01):
    retired sources are not collected, so only UM rows are checked."""

    def test_um_rows_are_known_and_unlisted_rows_have_no_source(self):
        known_id = next(iter(model_sync.KNOWN_MODELS))
        known_efforts = tuple(
            model_sync.KNOWN_MODELS[known_id].get("efforts") or ())
        self.assertTrue(known_efforts)  # 已知表命中才有来源可标

        def fake_get(url, *args, **kwargs):
            if url == model_sync.UM_MODELS_URL:
                return {"data": [{"id": known_id},
                                 {"id": "um-unlisted-model"}]}
            return {"data": []}

        with mock.patch.object(model_sync, "_get_json", side_effect=fake_get):
            catalogs, _ = model_sync.collect_catalogs(sources=["um"])

        um_rows = {m.model_id: m for m in catalogs["um"]}
        self.assertEqual(known_efforts, um_rows[known_id].efforts)
        self.assertEqual("known", um_rows[known_id].efforts_source)
        # 未命中已知表 → 无档位，也没有来源可标（绝不伪造）。
        self.assertEqual((), um_rows["um-unlisted-model"].efforts)
        self.assertIsNone(um_rows["um-unlisted-model"].efforts_source)


class TestRetiredSourceSync(unittest.TestCase):
    """B0 (2026-11-01): retired sources are not part of the default sync.

    M365 is archived and Qoder CN / Qoder Global / CLIProxy are retired. Their
    historical bindings, managed state and ledgers stay readable for
    traceability, but a default or explicit sync run never fetches, plans,
    probes or writes them, never reads their old token files and never
    auto-reactivates them.
    """

    def test_retired_bindings_are_not_active(self):
        self.assertEqual(model_sync.ACTIVE_SOURCES, ("um",))
        sources = {binding.source for binding in model_sync.ACTIVE_BINDINGS}
        self.assertEqual(sources, {"um"})

    def test_retired_sources_are_rejected_by_scope(self):
        for source in ("m365", "qoder_cn", "qoder_global", "cliproxy"):
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, "已暂停"):
                    model_sync._normalize_scope({"sources": [source]})
                with self.assertRaisesRegex(ValueError, "已暂停"):
                    model_sync._normalize_scope(
                        {"models": {source: ["model-x"]}})

    def test_retired_probe_selection_is_rejected(self):
        for source in ("m365", "qoder_cn", "qoder_global", "cliproxy"):
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, "已暂停"):
                    model_sync.normalize_probe_selection([source], None)
                with self.assertRaisesRegex(ValueError, "已暂停"):
                    model_sync.normalize_probe_selection(
                        None, {source: ["model-x"]})

    def test_retired_managed_state_is_preserved(self):
        # Historical managed/initialized bindings stay readable and are not
        # deleted or auto-reactivated by a default run.
        state = empty_state()
        state["initializedBindings"] = ["m365|ZCode|local:m365"]
        state["managed"] = {
            "m365|ZCode|local:m365": {
                "legacy": {"profile": {"name": "legacy"}}}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_file = root / "state.json"
            state_file.write_text(json.dumps(state, ensure_ascii=False),
                                  encoding="utf-8")
            zcode = root / "config.json"
            zcode.write_text(json.dumps({"provider": {}}, ensure_ascii=False),
                              encoding="utf-8")
            store = root / "provider_config.json"
            store.write_text(json.dumps({"schemaVersion": 1, "config": {}}),
                              encoding="utf-8")
            with mock.patch.object(model_sync, "STATE_FILE", state_file), \
                    mock.patch.object(model_sync, "ZCODE_JSON", zcode), \
                    mock.patch.object(model_sync, "ZCODE_PROVIDER_CONFIG", store), \
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
                            json.dumps({"provider": {}},
                                        ensure_ascii=False))), \
                    mock.patch.object(model_sync, "zcode_running",
                                      return_value=False):
                result = model_sync.run_sync(
                    apply=True, run_probes=False, confirm=False)
            after = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(after["managed"], state["managed"])
            self.assertEqual(after["initializedBindings"],
                             state["initializedBindings"])
            self.assertNotIn("m365", {binding.source
                                      for binding in model_sync.ACTIVE_BINDINGS})
            self.assertNotIn("local:m365", json.loads(
                zcode.read_text(encoding="utf-8"))["provider"])
            store_doc = json.loads(store.read_text(encoding="utf-8"))
            providers = {
                rule.get("providerId") for rule in
                store_doc["config"]["providerConfigRules"]["providerRules"]}
            self.assertNotIn("local:m365", providers)
            self.assertNotIn("local:openwebui", providers)


class TestManagedMissingProviderSync(unittest.TestCase):
    """A12-4 retired (B0): a managed-but-missing retired provider is NOT
    invented. ``local:cliproxy`` has no active binding in the UM-only default
    assembly, so the sync neither registers it nor reads its controlled key.
    """

    def test_retired_provider_is_not_registered(self):
        binding = Binding("cliproxy", "ZCode", "local:cliproxy",
                          "Gemini（CLIProxy）", "（Gemini）")
        self.assertNotIn(binding, model_sync.ACTIVE_BINDINGS)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "provider_config.json"
            store.write_text(json.dumps({"schemaVersion": 1, "config": {}}),
                              encoding="utf-8")
            with mock.patch.object(model_sync, "ZCODE_PROVIDER_CONFIG", store):
                with self.assertRaisesRegex(ValueError, "已暂停"):
                    model_sync.run_sync(
                        apply=False, run_probes=False, confirm=False,
                        scope={"sources": ["cliproxy"]})
            after = json.loads(store.read_text(encoding="utf-8"))
            self.assertEqual(after, {"schemaVersion": 1, "config": {}})


class TestRunSyncScope(unittest.TestCase):
    UM_ZCODE = Binding("um", "ZCode", "local:openwebui", "UM", "（UM）")

    def _run(self, tmp_root, catalogs, consumers, scope=None,
             zcode_running=False, state_doc=None):
        root = Path(tmp_root)
        state_file = root / "state.json"
        if state_doc is not None:
            state_file.write_text(json.dumps(state_doc, ensure_ascii=False),
                                  encoding="utf-8")
        # R20-2：规则库正常且与本目录对齐——「写主配置」路径的前提；缺席
        # 规则库会让整个 ZCode 写集合延期，不是这些用例要测的路径。
        (root / "provider_config.json").write_text(
            json.dumps(aligned_rule_store(catalogs), ensure_ascii=False),
            encoding="utf-8")
        zcode_payload = json.dumps({
            "provider": {
                "local:openwebui": {"name": "UM", "models": {}},
                "local:lingma-proxy": {"name": "Qoder（国内）", "models": {}},
            }
        }, ensure_ascii=False)
        with mock.patch.object(
                model_sync, "collect_catalogs",
                return_value=(catalogs, [])) as collect, \
            mock.patch.object(
                model_sync, "collect_consumers",
                return_value=(consumers, "llm-pi-ai: {}", zcode_payload)), \
            mock.patch.object(
                model_sync, "zcode_running", return_value=zcode_running), \
            mock.patch.object(model_sync, "zcode_hot_sync_admission",
                              return_value=(False, "测试：未准入")), \
            mock.patch.object(model_sync, "ZCODE_PROVIDER_CONFIG",
                              root / "provider_config.json"), \
            mock.patch.object(model_sync, "ZCODE_JSON",
                              root / "config.json"), \
            mock.patch.object(model_sync, "DSH_YAML",
                              root / "settings.yaml"), \
            mock.patch.object(model_sync, "STATE_FILE", state_file), \
            mock.patch.object(model_sync, "RESULT_FILE",
                              root / "result.json"), \
            mock.patch.object(model_sync, "HISTORY_FILE",
                              root / "history.jsonl"), \
            mock.patch.object(model_sync, "BACKUP_DIR", root / "backups"):
            result = model_sync.run_sync(
                apply=True, run_probes=False, confirm=False, scope=scope)
        state_after = json.loads(state_file.read_text("utf-8"))
        return result, state_after, collect

    def test_fixture_isolates_the_real_client_configs(self):
        # 守卫：apply 路径会把计划写进两份客户端配置。若夹具漏了某个路径，
        # 测试就会读写操作者真实的 ~/.zcode/v2/config.json 或
        # ~/.dsh/settings.yaml（执行轮 2 记录过同类缺陷）。
        real = {
            "ZCODE_JSON": Path.home() / ".zcode" / "v2" / "config.json",
            "DSH_YAML": Path.home() / ".dsh" / "settings.yaml",
            "ZCODE_PROVIDER_CONFIG":
                Path.home() / ".zcode" / "v2" / "provider_config.json",
        }
        seen = {}
        inner = model_sync.run_sync

        def spy(*args, **kwargs):
            for name in real:
                seen[name] = getattr(model_sync, name)
            return inner(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(model_sync, "run_sync", spy):
                self._run(root, {}, {})
        for name, path in real.items():
            self.assertNotEqual(path, seen[name], name)
            self.assertEqual(seen[name].parent, root, name)

    def test_sources_subset_never_touches_unselected_source(self):
        # Mirrors the real collect_catalogs subset: only the selected source
        # is collected (unselected sources never enter catalogs or counts).
        catalogs = {"um": [CatalogModel("um", "um-a", context=4096)]}
        consumers = {
            self.UM_ZCODE.key: {"providerName": "UM", "models": {}},
        }
        watch_key = "um|DSH|openwebui|legacy"
        state_doc = {
            "version": 2,
            "retirementWatch": {watch_key: {
                "firstMissing": "2026-09-21T06:00:00+00:00",
                "lastMissing": "2026-09-21T06:00:00+00:00",
                "evidenceRuns": 1}},
        }
        with tempfile.TemporaryDirectory() as directory:
            result, state, collect = self._run(
                Path(directory), catalogs, consumers,
                scope={"sources": ["um"]}, state_doc=state_doc)
        # The subset is passed down to the catalog collector (unselected
        # sources are never requested in the real path).
        self.assertEqual(collect.call_args[1]["sources"], ["um"])
        self.assertEqual(result["appliedDetails"],
                         [{"consumer": "ZCode", "kind": "add",
                           "model_id": "um-a",
                           "description": result["applied"][0]}])
        self.assertTrue(all("um-a" in d for d in result["applied"]))
        # The retirement watch of an unrelated model is frozen verbatim (no
        # evidence counting) because it is outside the selected source.
        self.assertEqual(state["retirementWatch"][watch_key],
                         state_doc["retirementWatch"][watch_key])

    def test_retired_source_cannot_be_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "已暂停"):
                self._run(Path(directory), {}, {},
                          scope={"sources": ["qoder_cn"]})

    def test_consumers_subset_leaves_unselected_client_untouched(self):
        catalogs = {"um": [CatalogModel("um", "um-a", context=4096)]}
        consumers = {
            "um|DSH|openwebui": {"providerName": "UM", "models": {}},
            self.UM_ZCODE.key: {"providerName": "UM", "models": {}},
        }
        dsh_raw = ("llm-pi-ai:\n  providers:\n    openwebui:\n"
                   "      displayName: UM\n      models:\n")
        zcode_before = json.dumps({"provider": {
            "local:openwebui": {"name": "UM", "models": {}}}},
            ensure_ascii=False)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dsh_yaml = root / "settings.yaml"
            zcode_json = root / "config.json"
            dsh_yaml.write_text(dsh_raw, encoding="utf-8")
            zcode_json.write_text(zcode_before, encoding="utf-8")
            with mock.patch.object(
                    model_sync, "collect_catalogs",
                    return_value=(catalogs, [])), \
                mock.patch.object(
                    model_sync, "collect_consumers",
                    return_value=(consumers, dsh_raw, zcode_before)), \
                mock.patch.object(model_sync, "DSH_YAML", dsh_yaml), \
                mock.patch.object(model_sync, "ZCODE_JSON", zcode_json), \
                mock.patch.object(model_sync, "zcode_running",
                                  return_value=False), \
                mock.patch.object(model_sync, "STATE_FILE",
                                  root / "state.json"), \
                mock.patch.object(model_sync, "RESULT_FILE",
                                  root / "result.json"), \
                mock.patch.object(model_sync, "HISTORY_FILE",
                                  root / "history.jsonl"), \
                mock.patch.object(model_sync, "BACKUP_DIR",
                                  root / "backups"):
                result = model_sync.run_sync(
                    apply=True, run_probes=False, confirm=False,
                    scope={"consumers": ["DSH"]})
            state = json.loads((root / "state.json").read_text("utf-8"))
            # The selected client config received the model; the unselected
            # one was not planned into any change (asserted inside the temp
            # context so the files still exist).
            self.assertEqual(
                [d["consumer"] for d in result["appliedDetails"]], ["DSH"])
            self.assertIn("um-a", dsh_yaml.read_text(encoding="utf-8"))
            self.assertEqual(zcode_json.read_text(encoding="utf-8"),
                             zcode_before)
            self.assertNotIn(self.UM_ZCODE.key, state["managed"])

    def test_models_subset_limits_add_and_provider_name(self):
        catalogs = {"um": [
            CatalogModel("um", "um-a", context=4096),
            CatalogModel("um", "um-b", context=4096),
        ]}
        consumers = {
            self.UM_ZCODE.key: {"providerName": "Wrong Name", "models": {}},
        }
        with tempfile.TemporaryDirectory() as directory:
            result, state, _ = self._run(
                Path(directory), catalogs, consumers,
                scope={"models": {"um": ["um-a"]}})
        kinds = [d["kind"] for d in result["appliedDetails"]]
        # Provider-name changes are suppressed for sources with a models
        # filter; only the selected model joins the write surface.
        self.assertEqual(kinds, ["add"])
        self.assertEqual(result["appliedDetails"][0]["model_id"], "um-a")
        self.assertNotIn("um-b", state["managed"]
                         ["um|ZCode|local:openwebui"])

    def test_models_ids_outside_scope_produce_no_changes(self):
        catalogs = {"um": [CatalogModel("um", "um-a", context=4096)]}
        consumers = {
            "um|DSH|openwebui": {"providerName": "UM", "models": {}},
            self.UM_ZCODE.key: {"providerName": "UM", "models": {}}}
        with tempfile.TemporaryDirectory() as directory:
            result, _, _ = self._run(
                Path(directory), catalogs, consumers,
                scope={"models": {"um": ["other-model"]}})
        self.assertEqual(result["applied"], [])
        self.assertEqual(result["status"], "normal")


class TestRunSyncContextProbeSeeding(unittest.TestCase):
    UM_ZCODE = Binding("um", "ZCode", "local:openwebui",
                       "UM", "（qoder_CN）")

    def _consumers(self):
        return {self.UM_ZCODE.key: {"providerName": "UM",
                                    "models": {}}}

    def _catalogs(self, context_a=1000, context_b=2000):
        return {"um": [
            CatalogModel("um", "cn-a", context=context_a),
            CatalogModel("um", "cn-b", context=context_b),
        ]}

    def _run(self, tmp_root, catalogs, scope=None, state_doc=None):
        root = Path(tmp_root)
        state_file = root / "state.json"
        if state_doc is not None:
            state_file.write_text(json.dumps(state_doc, ensure_ascii=False),
                                  encoding="utf-8")
        with mock.patch.object(
                model_sync, "collect_catalogs",
                return_value=(catalogs, [])), \
            mock.patch.object(
                model_sync, "collect_consumers",
                return_value=(self._consumers(), "llm-pi-ai: {}",
                              json.dumps({"provider": {
                                  "local:openwebui": {
                                      "name": "UM", "models": {}}}},
                                  ensure_ascii=False))), \
            mock.patch.object(model_sync, "zcode_running", return_value=False), \
            mock.patch.object(model_sync, "ZCODE_PROVIDER_CONFIG",
                              root / "provider_config.json"), \
            mock.patch.object(model_sync, "ZCODE_JSON",
                              root / "config.json"), \
            mock.patch.object(model_sync, "DSH_YAML",
                              root / "settings.yaml"), \
            mock.patch.object(model_sync, "STATE_FILE", state_file), \
            mock.patch.object(model_sync, "RESULT_FILE",
                              root / "result.json"), \
            mock.patch.object(model_sync, "HISTORY_FILE",
                              root / "history.jsonl"), \
            mock.patch.object(model_sync, "BACKUP_DIR", root / "backups"):
            result = model_sync.run_sync(
                apply=True, run_probes=False, confirm=False, scope=scope)
        return result, json.loads(state_file.read_text("utf-8"))

    def test_valid_catalog_seeds_queue_and_keeps_verified_record(self):
        state_doc = {"version": 2, "contextProbes": {
            "um|cn-a": {
                "source": "um", "model_id": "cn-a",
                "declared_context": 1000, "declared_at": "2026-09-21T06:00:00+00:00",
                "status": "verified", "observed_floor": 256000,
                "attempts": 4, "last_run": None, "last_error": None},
        }}
        with tempfile.TemporaryDirectory() as directory:
            result, state = self._run(
                Path(directory), self._catalogs(), state_doc=state_doc)
        queue = state["contextProbes"]
        self.assertEqual(queue["um|cn-a"]["status"], "verified")
        self.assertEqual(queue["um|cn-b"]["status"], "pending")
        self.assertEqual(queue["um|cn-b"]["declared_context"], 2000)
        self.assertIn("attempts", queue["um|cn-b"])

    def test_declared_change_flips_verified_record_to_stale(self):
        state_doc = {"version": 2, "contextProbes": {
            "um|cn-a": {
                "source": "um", "model_id": "cn-a",
                "declared_context": 1000,
                "declared_at": "2026-09-21T06:00:00+00:00",
                "status": "verified", "observed_floor": 256000,
                "attempts": 4, "last_run": None, "last_error": None},
        }}
        with tempfile.TemporaryDirectory() as directory:
            _, state = self._run(
                Path(directory), self._catalogs(context_a=5000),
                state_doc=state_doc)
        record = state["contextProbes"]["um|cn-a"]
        self.assertEqual(record["status"], "stale")
        self.assertEqual(record["declared_context"], 5000)

    def test_models_scope_limits_seeding(self):
        with tempfile.TemporaryDirectory() as directory:
            _, state = self._run(
                Path(directory), self._catalogs(),
                scope={"sources": ["um"], "models": {"um": ["cn-a"]}})
        self.assertEqual(set(state["contextProbes"]), {"um|cn-a"})


class TestContextProbeSeedingSources(unittest.TestCase):
    """Plan-Revision 7 step 4 source coverage at the seeding layer: Gemini
    rides the registered cliproxy gateway, models WITHOUT a declared context
    join as declaration-unknown (selectable, never zero capacity), and
    paused m365 never enters the active billing queue (historical records
    stay untouched)."""

    def test_retired_source_catalog_is_not_seeded(self):
        # B0: a retired source catalog (CLIProxy) never enters the active
        # probe queue; its historical records stay untouched.
        catalogs = {"cliproxy": [
            CatalogModel("cliproxy", "gemini-a", context=128000),
            CatalogModel("cliproxy", "gemini-b"),
        ]}
        state = empty_state()
        state["contextProbes"] = {"cliproxy|old": {"status": "verified"}}
        model_sync.seed_context_probes(catalogs, state)
        self.assertEqual(set(state["contextProbes"]), {"cliproxy|old"})
        self.assertEqual(state["contextProbes"]["cliproxy|old"]["status"],
                         "verified")

    def test_paused_m365_catalog_is_not_seeded(self):
        # M365 stays paused: no endpoint, never seeded into the active
        # billing queue.
        catalogs = {"m365": [CatalogModel("m365", "gpt-x", context=128000)]}
        state = empty_state()
        model_sync.seed_context_probes(catalogs, state)
        self.assertEqual(state["contextProbes"], {})

    def test_existing_m365_record_is_left_untouched(self):
        # Historical m365 records stay in state untouched by the pause —
        # endpoint failures never delete models.
        record = {"source": "m365", "model_id": "gpt-old",
                  "declared_context": 32000, "declared_at": "t",
                  "status": "verified", "observed_floor": 256000,
                  "attempts": 4, "last_run": "t", "last_error": None}
        state = empty_state()
        state["contextProbes"] = {"m365|gpt-old": dict(record)}
        catalogs = {"m365": [CatalogModel("m365", "gpt-x", context=128000)]}
        model_sync.seed_context_probes(catalogs, state)
        self.assertEqual(state["contextProbes"]["m365|gpt-old"], record)
        self.assertNotIn("m365|gpt-x", state["contextProbes"])


class TestContextOverrideSync(unittest.TestCase):
    """Plan-Revision 7 step 6: manual context overrides — validation, the
    single run_sync application point (mirror + probe seeding share the same
    effective value), the read surface, and history preservation on
    restore-default.  Every state write lands in an isolated temp directory."""

    def _state_doc(self, overrides=None):
        state = empty_state()
        state["contextProbes"] = {
            "um|model-a": {
                "source": "um", "model_id": "model-a",
                "declared_context": 128000,
                "declared_at": "2026-09-30T06:00:00+00:00",
                "status": "verified", "observed_floor": 256000,
                "attempts": 4, "last_run": "t", "last_error": None,
                "history": [{"observed_floor": 64000, "reason": "retest"}],
            },
        }
        # Rev8：设置存档键为 modelSettings（草稿/已应用分离）；旧
        # contextOverrides 已由 migrate_state 迁走，读面与测试统一走新形状。
        state["modelSettings"] = overrides or {}
        return state

    @staticmethod
    def _applied(tokens, at="2026-09-30T08:00:00+00:00"):
        """已应用记录（contextSource=manual）；alias/effort 未设置。"""
        return {"draft": None, "draftVersion": 0, "draftSavedAt": None,
                "applied": {"alias": None, "contextSource": "manual",
                            "contextTokens": tokens, "defaultEffort": None},
                "appliedVersion": 1, "appliedAt": at,
                "appliedEvidence": "applied"}

    @staticmethod
    def _draft(tokens, version=1, at="2026-09-30T08:00:00+00:00"):
        """待应用草稿记录（held：无已应用值，镜像不消费）。"""
        return {"draft": {"alias": None, "contextSource": "manual",
                          "contextTokens": tokens, "defaultEffort": None},
                "draftVersion": version, "draftSavedAt": at,
                "applied": None, "appliedVersion": None, "appliedAt": None,
                "appliedEvidence": None}

    def test_applied_settings_replace_only_overridden_models(self):
        # Rev8 契约 6：唯一应用点只消费「已应用」值——已应用 manual 覆盖生效，
        # 待应用草稿绝不生效（held，见 run_sync）；同值覆盖零通知零改写。
        catalogs = {"um": [CatalogModel("um", "model-a", context=128000),
                           CatalogModel("um", "model-b", context=64000)]}
        state = self._state_doc({"um|model-a": self._applied(200000)})
        applied, notices = model_sync.apply_context_overrides(
            catalogs, state)
        self.assertEqual(applied["um"][0].context, 200000)
        self.assertEqual(applied["um"][1].context, 64000)
        self.assertTrue(
            any("um|model-a → 200000" in line for line in notices))
        # A DRAFT is not an effective value: no replacement, no notice.
        state = self._state_doc({"um|model-a": self._draft(200000)})
        applied, notices = model_sync.apply_context_overrides(
            catalogs, state)
        self.assertEqual(applied["um"][0].context, 128000)
        self.assertEqual(notices, [])

    def test_malformed_applied_settings_are_reported_not_applied(self):
        # Hand-edited/corrupt state must never apply a malformed value
        # (string/bool/zero/negative tokens) — the declared value stays
        # effective and no notice fabricates an applied effect.
        for tokens in ("big", True, 0, -5):
            with self.subTest(contextTokens=tokens):
                catalogs = {"um": [CatalogModel("um", "model-a",
                                                context=128000)]}
                record = self._applied(0)
                record["applied"]["contextTokens"] = tokens
                state = self._state_doc({"um|model-a": record})
                applied, notices = model_sync.apply_context_overrides(
                    catalogs, state)
                self.assertEqual(applied["um"][0].context, 128000)
                self.assertEqual(notices, [])

    def test_set_validates_context_and_identity(self):
        catalogs = {"um": [CatalogModel("um", "model-a", context=128000)]}
        for context in (0, -5, "big", True, 1.5, None):
            with self.subTest(context=context):
                with self.assertRaises(ValueError):
                    model_sync.set_context_override(
                        catalogs, "um", "model-a", context)
        with self.assertRaisesRegex(ValueError, "模型不在当前目录"):
            model_sync.set_context_override(catalogs, "um", "ghost", 200000)
        with self.assertRaises(ValueError):
            model_sync.set_context_override(catalogs, "mars", "m", 200000)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(model_sync, "STATE_FILE",
                                   root / "state.json"),                 mock.patch.object(model_sync, "BACKUP_DIR",
                                  root / "backups"):
                entry = model_sync.set_context_override(
                    catalogs, "um", "model-a", 200000)
            self.assertEqual(entry["context"], 200000)
            self.assertEqual(entry["source"], "manual")
            self.assertEqual(entry["draft_version"], 1)
            datetime.fromisoformat(entry["set_at"])  # ISO stamp
            state = json.loads((root / "state.json").read_text("utf-8"))
            # Rev8 契约 6：保存只写 modelSettings 草稿，绝不写旧
            # contextOverrides 键（迁移后该键不再存在），也绝不触碰客户端。
            self.assertNotIn("contextOverrides", state)
            record = state["modelSettings"]["um|model-a"]
            self.assertEqual(record["draft"],
                             {"alias": None, "contextSource": "manual",
                              "contextTokens": 200000,
                              "defaultEffort": None})
            self.assertEqual(record["draftVersion"], 1)
            self.assertIsNone(record["applied"])
            self.assertIsNone(record["appliedVersion"])

    def test_set_enforces_expected_set_at_version_rule(self):
        catalogs = {"um": [CatalogModel("um", "model-a", context=128000)]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(model_sync, "STATE_FILE",
                                   root / "state.json"),                 mock.patch.object(model_sync, "BACKUP_DIR",
                                  root / "backups"):
                first = model_sync.set_context_override(
                    catalogs, "um", "model-a", 200000)
                # Rev8 版本令牌 = 读面 draftSavedAt：bare set 对既有草稿冲突。
                with self.assertRaises(
                        model_sync.ContextOverrideConflict):
                    model_sync.set_context_override(
                        catalogs, "um", "model-a", 64000)
                with self.assertRaises(
                        model_sync.ContextOverrideConflict):
                    model_sync.set_context_override(
                        catalogs, "um", "model-a", 64000,
                        "2026-01-01T00:00:00+00:00")
                # Matching version → update allowed.
                second = model_sync.set_context_override(
                    catalogs, "um", "model-a", 64000, first["set_at"])
                self.assertEqual(second["context"], 64000)
                self.assertEqual(second["draft_version"], 2)
                # A restore draft is ALSO a pending draft: bare set over it
                # conflicts (the caller must confirm the version it read).
                model_sync.clear_context_override("um", "model-a",
                                                  second["set_at"])
                with self.assertRaises(
                        model_sync.ContextOverrideConflict):
                    model_sync.set_context_override(
                        catalogs, "um", "model-a", 64000,
                        second["set_at"])

    def test_clear_writes_restore_draft_and_keeps_history(self):
        """Rev8 契约 6：恢复也形成可预览草稿——绝不静默删除存储覆盖。

        已应用 manual 覆盖存在时：bare clear 冲突（须确认读到的版本令牌）；
        令牌匹配 → 恢复草稿写入（contextSource=None，draftVersion 递增）；
        探测历史证据原样保留。恢复草稿已在待应用时再次 clear 是幂等零写入。
        """
        state = self._state_doc({"um|model-a": self._applied(200000)})
        probe_before = json.loads(json.dumps(
            state["contextProbes"]["um|model-a"]))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(model_sync, "STATE_FILE",
                                   root / "state.json"), \
                mock.patch.object(model_sync, "BACKUP_DIR",
                                  root / "backups"):
                (root / "state.json").write_text(
                    json.dumps(state, ensure_ascii=False), encoding="utf-8")
                # The version rule is explicit for clear too: a bare clear on
                # an EXISTING applied override is a conflict (the caller must
                # confirm the version it read), never a blind delete.
                with self.assertRaises(
                        model_sync.ContextOverrideConflict):
                    model_sync.clear_context_override("um", "model-a")
                outcome = model_sync.clear_context_override(
                    "um", "model-a", "2026-09-30T08:00:00+00:00")
                self.assertTrue(outcome["existed"])
                self.assertTrue(outcome["changed"])
                after = json.loads(
                    (root / "state.json").read_text("utf-8"))
                record = after["modelSettings"]["um|model-a"]
                # 恢复草稿可预览：contextSource=None；已应用值在显式应用前
                # 保持原样（不标已生效，也不撤回客户端当前值——镜像 held）。
                self.assertIsNone(record["draft"]["contextSource"])
                self.assertIsNone(record["draft"]["contextTokens"])
                self.assertEqual(record["applied"]["contextTokens"], 200000)
                self.assertEqual(record["draftVersion"], 1)
                # 恢复默认绝不删除历史证据：探测记录与其 history 原样保留。
                self.assertEqual(after["contextProbes"]["um|model-a"],
                                 probe_before)
                # Clearing with the pending restore draft is an idempotent
                # no-op (changed=False, zero write).
                again = model_sync.clear_context_override(
                    "um", "model-a", record["draftSavedAt"])
                self.assertTrue(again["existed"])
                self.assertFalse(again["changed"])

    def test_context_override_surface_fields(self):
        """读面：override_* 来自已应用设置；草稿以加性字段如实呈现（held），
        绝不冒充 effective。"""
        catalogs = {"um": [CatalogModel("um", "model-a", context=128000),
                           CatalogModel("um", "model-b", context=None)]}
        state = self._state_doc({
            "um|model-a": self._applied(200000),
            "um|ghost": self._applied(64000,
                                      at="2026-09-30T09:00:00+00:00"),
            "um|model-b": self._draft(64000, version=2,
                                      at="2026-09-30T09:30:00+00:00"),
        })
        state["modelSettings"]["um|model-b"]["draft"]["contextSource"] = None
        state["modelSettings"]["um|model-b"]["draft"]["contextTokens"] = None
        surface = {row["source"] + "|" + row["model_id"]: row
                   for row in model_sync.context_override_surface(
                       state, catalogs)}
        declared = surface["um|model-a"]
        self.assertEqual(declared["declared_context"], 128000)
        self.assertEqual(declared["override_context"], 200000)
        self.assertEqual(declared["effective_context"], 200000)
        self.assertEqual(declared["effective_source"], "override")
        self.assertEqual(declared["declared_at"],
                         "2026-09-30T06:00:00+00:00")
        self.assertEqual(declared["override_at"],
                         "2026-09-30T08:00:00+00:00")
        self.assertFalse(declared["settings_held"])
        self.assertEqual(declared["settings_applied_version"], 1)
        # Draft-only record: held=True, applied fields absent, effective
        # stays the declaration (a draft never masquerades as effective).
        draft_row = surface["um|model-b"]
        self.assertIsNone(draft_row["override_context"])
        self.assertIsNone(draft_row["effective_context"])
        self.assertEqual(draft_row["effective_source"], "unknown")
        self.assertTrue(draft_row["settings_held"])
        self.assertEqual(draft_row["settings_draft_version"], 2)
        self.assertIsNone(draft_row["settings_applied"])
        # Inert applied override: the model left the catalog — shown
        # honestly, never reported as effective.
        inert = surface["um|ghost"]
        self.assertIsNone(inert["declared_context"])
        self.assertEqual(inert["override_context"], 64000)
        self.assertIsNone(inert["effective_context"])
        self.assertEqual(inert["effective_source"], "unknown")
        self.assertFalse(inert["settings_held"])

    def _run_override_sync(self, tmp_root, catalogs, state_doc, apply,
                           state_override=None):
        """One run_sync round with the qoder_cn ZCode binding wired offline."""
        root = Path(tmp_root)
        cn_binding = Binding("qoder_cn", "ZCode", "local:lingma-proxy",
                             "Qoder（国内）", "（qoder_CN）")
        state_file = root / "state.json"
        state_file.write_text(json.dumps(state_doc, ensure_ascii=False),
                              encoding="utf-8")
        seen = []
        real_plan = model_sync.plan_catalog_mirror

        def plan_spy(*args, **kwargs):
            seen.append(args[0])
            return real_plan(*args, **kwargs)

        with mock.patch.object(
                model_sync, "plan_catalog_mirror",
                side_effect=plan_spy), \
            mock.patch.object(
                model_sync, "collect_catalogs",
                return_value=(catalogs, [])), \
            mock.patch.object(
                model_sync, "collect_consumers",
                return_value=(
                    {cn_binding.key: {"providerName": "Qoder（国内）",
                                      "models": {}}},
                    "llm-pi-ai: {}",
                    json.dumps({"provider": {
                        "local:lingma-proxy": {
                            "name": "Qoder（国内）", "models": {}}}},
                        ensure_ascii=False))), \
            mock.patch.object(model_sync, "zcode_running",
                              return_value=False), \
            mock.patch.object(model_sync, "ZCODE_PROVIDER_CONFIG",
                              root / "provider_config.json"), \
            mock.patch.object(model_sync, "ZCODE_JSON",
                              root / "config.json"), \
            mock.patch.object(model_sync, "DSH_YAML",
                              root / "settings.yaml"), \
            mock.patch.object(model_sync, "STATE_FILE", state_file), \
            mock.patch.object(model_sync, "RESULT_FILE",
                              root / "result.json"), \
            mock.patch.object(model_sync, "HISTORY_FILE",
                              root / "history.jsonl"), \
            mock.patch.object(model_sync, "PREVIEW_FILE",
                              root / "preview.json"), \
            mock.patch.object(model_sync, "BACKUP_DIR", root / "backups"):
            result = model_sync.run_sync(
                apply=apply, run_probes=False, confirm=False,
                state_override=state_override)
        return seen, result, json.loads(state_file.read_text("utf-8"))

    def test_run_sync_holds_pending_draft_and_applies_only_promoted(self):
        """Rev8 契约 6 的 run_sync 双断言：

        1. 待应用草稿 held——镜像两次 plan 都看到目录声明（不规划更新、
           零通知），探测播种读声明，草稿在同步轮后原样保留（定时同步只
           消费已应用值、不回撤客户端当前值）；
        2. promote 之后经 state_override 单事务发布——镜像看到 95000，
           overrideNotices 携带已应用通知，状态文件记录 applied 元数据。
        """
        catalogs = {"qoder_cn": [
            CatalogModel("qoder_cn", "cn-a", context=1000),
            CatalogModel("qoder_cn", "cn-b", context=2000),
        ]}
        state_doc = empty_state()
        state_doc["contextProbes"] = {
            "qoder_cn|cn-a": {
                "source": "qoder_cn", "model_id": "cn-a",
                "declared_context": 1000,
                "declared_at": "2026-09-30T06:00:00+00:00",
                "status": "verified", "observed_floor": 256000,
                "attempts": 4, "last_run": "t", "last_error": None,
                "history": []}}
        state_doc["modelSettings"] = {
            "qoder_cn|cn-a": self._draft(95000)}
        with tempfile.TemporaryDirectory() as directory:
            seen, result, after = self._run_override_sync(
                directory, catalogs, state_doc, apply=True)
        self.assertTrue(seen)
        for passed in seen:
            rows = {m.model_id: m for m in passed["qoder_cn"]}
            # held：草稿不是有效值，镜像看到目录声明。
            self.assertEqual(rows["cn-a"].context, 1000)
            self.assertEqual(rows["cn-b"].context, 2000)
        self.assertEqual(after["contextProbes"]["qoder_cn|cn-a"]
                         ["declared_context"], 1000)
        self.assertEqual(result["overrideNotices"], [])
        record = after["modelSettings"]["qoder_cn|cn-a"]
        self.assertEqual(record["draft"]["contextTokens"], 95000)
        self.assertIsNone(record["applied"])
        # Publish path: promote the draft IN MEMORY, publish through the
        # SAME single transaction via state_override.
        promoted, skipped = model_sync.promote_model_setting_drafts(
            state_doc, {"qoder_cn|cn-a": 1})
        self.assertEqual(promoted, ["qoder_cn|cn-a"])
        self.assertEqual(skipped, [])
        with tempfile.TemporaryDirectory() as directory:
            seen2, result2, after2 = self._run_override_sync(
                directory, catalogs, state_doc, apply=True,
                state_override=state_doc)
        self.assertTrue(seen2)
        for passed in seen2:
            rows = {m.model_id: m for m in passed["qoder_cn"]}
            self.assertEqual(rows["cn-a"].context, 95000)
        notice = "context 已应用：qoder_cn|cn-a → 95000（目录声明 1000）"
        self.assertIn(notice, result2["overrideNotices"])
        self.assertNotIn(notice, result2["warnings"])
        applied_record = after2["modelSettings"]["qoder_cn|cn-a"]
        self.assertEqual(applied_record["applied"]["contextTokens"], 95000)
        self.assertIsNone(applied_record["draft"])
        self.assertEqual(applied_record["appliedVersion"], 1)
        self.assertEqual(applied_record["appliedEvidence"], "applied")
        # Probe side unchanged: declared_context stays the raw declaration.
        self.assertEqual(after2["contextProbes"]["qoder_cn|cn-a"]
                         ["declared_context"], 1000)

    def test_sync_preview_uses_the_same_effective_value(self):
        # 同步预览与 apply 使用同一有效值：preview 走同一条 run_sync 路径，
        # 镜像计划收到同一替换后的目录，且不写任何客户端配置。
        catalogs = {"qoder_cn": [CatalogModel("qoder_cn", "cn-a",
                                              context=1000)]}
        state_doc = self._state_doc({"qoder_cn|cn-a": self._applied(95000)})
        with tempfile.TemporaryDirectory() as directory:
            seen, result, _ = self._run_override_sync(
                directory, catalogs, state_doc, apply=False)
        self.assertTrue(seen)
        for passed in seen:
            rows = {m.model_id: m for m in passed["qoder_cn"]}
            self.assertEqual(rows["cn-a"].context, 95000)
        notice = "context 已应用：qoder_cn|cn-a → 95000（目录声明 1000）"
        self.assertIn(notice, result["overrideNotices"])
        self.assertNotIn(notice, result["warnings"])

    def test_override_draft_then_promote_does_not_retest_verified_record(self):
        # 草稿保存→提升→应用周期绝不重置已 verified 的探测记录：人工设置
        # 只在镜像侧生效，探测播种永远读覆盖前的目录声明——保存/提升/应用
        # 都不会翻 stale、不归档、不重测（status 与 evidence_revision 不变、
        # history 为空），零计费请求。
        catalogs = {"qoder_cn": [CatalogModel("qoder_cn", "cn-a",
                                              context=1000)]}
        state_doc = empty_state()
        state_doc["contextProbes"] = {
            "qoder_cn|cn-a": {
                "source": "qoder_cn", "model_id": "cn-a",
                "declared_context": 1000,
                "declared_at": "2026-09-30T06:00:00+00:00",
                "status": "verified", "observed_floor": 256000,
                "attempts": 4, "last_run": "t", "last_error": None,
                "evidence_revision": 2,
                "scheme_revision": model_sync.PROBE_SCHEME_REVISION,
                "history": []}}
        state_doc["modelSettings"] = {
            "qoder_cn|cn-a": self._draft(95000)}
        with tempfile.TemporaryDirectory() as directory:
            _, result, after = self._run_override_sync(
                directory, catalogs, state_doc, apply=True)
        record = after["contextProbes"]["qoder_cn|cn-a"]
        self.assertEqual(record["status"], "verified")
        self.assertEqual(record["evidence_revision"], 2)
        self.assertEqual(record["history"], [])
        self.assertEqual(record["declared_context"], 1000)
        self.assertEqual(record["observed_floor"], 256000)
        self.assertEqual(record["attempts"], 4)
        # held：草稿未被应用，镜像零通知零改写。
        self.assertEqual(result["overrideNotices"], [])
        # Publish: promote + state_override → mirror consumes the applied
        # value; the probe record STILL keeps the raw declaration.
        model_sync.promote_model_setting_drafts(
            state_doc, {"qoder_cn|cn-a": 1})
        with tempfile.TemporaryDirectory() as directory:
            _, result2, after2 = self._run_override_sync(
                directory, catalogs, state_doc, apply=True,
                state_override=state_doc)
        record2 = after2["contextProbes"]["qoder_cn|cn-a"]
        self.assertEqual(record2["status"], "verified")
        self.assertEqual(record2["evidence_revision"], 2)
        self.assertEqual(record2["history"], [])
        self.assertEqual(record2["declared_context"], 1000)
        self.assertTrue(result2["overrideNotices"])

    def test_set_rejects_context_above_upper_bound(self):
        # 覆盖加上界（CONTEXT_OVERRIDE_MAX=10_000_000）：10**15 被拒，
        # 状态零写入（校验先于 load_state）。
        catalogs = {"um": [CatalogModel("um", "model-a", context=128000)]}
        self.assertEqual(model_sync.CONTEXT_OVERRIDE_MAX, 10_000_000)
        self.assertFalse(model_sync._valid_override_context(10 ** 15))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_file = root / "state.json"
            state_file.write_text("{}", encoding="utf-8")
            with mock.patch.object(model_sync, "STATE_FILE", state_file), \
                    mock.patch.object(model_sync, "BACKUP_DIR",
                                      root / "backups"):
                with self.assertRaises(ValueError):
                    model_sync.set_context_override(
                        catalogs, "um", "model-a", 10 ** 15)
            self.assertEqual(state_file.read_text("utf-8"), "{}")

    def test_clear_absent_override_writes_nothing(self):
        # 无覆盖 clear（existed=False）跳过状态写入：状态文件字节不变，
        # 返回 {"existed": False, "changed": False}。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_file = root / "state.json"
            original = json.dumps(self._state_doc(), ensure_ascii=False)
            state_file.write_text(original, encoding="utf-8")
            with mock.patch.object(model_sync, "STATE_FILE", state_file), \
                    mock.patch.object(model_sync, "BACKUP_DIR",
                                      root / "backups"):
                outcome = model_sync.clear_context_override("um", "model-a")
            self.assertEqual(outcome,
                             {"existed": False, "changed": False})
            self.assertEqual(state_file.read_text("utf-8"), original)


    def test_set_rejects_context_above_upper_bound(self):
        # 覆盖加上界（CONTEXT_OVERRIDE_MAX=10_000_000）：10**15 被拒，
        # 状态零写入（校验先于 load_state）。
        catalogs = {"um": [CatalogModel("um", "model-a", context=128000)]}
        self.assertEqual(model_sync.CONTEXT_OVERRIDE_MAX, 10_000_000)
        self.assertFalse(model_sync._valid_override_context(10 ** 15))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_file = root / "state.json"
            state_file.write_text("{}", encoding="utf-8")
            with mock.patch.object(model_sync, "STATE_FILE", state_file), \
                    mock.patch.object(model_sync, "BACKUP_DIR",
                                      root / "backups"):
                with self.assertRaises(ValueError):
                    model_sync.set_context_override(
                        catalogs, "um", "model-a", 10 ** 15)
            self.assertEqual(state_file.read_text("utf-8"), "{}")

    def test_clear_absent_override_writes_nothing(self):
        # 无覆盖 clear（existed=False）跳过状态写入：状态文件字节不变，
        # 返回 {"existed": False, "changed": False}。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_file = root / "state.json"
            original = json.dumps(self._state_doc(), ensure_ascii=False)
            state_file.write_text(original, encoding="utf-8")
            with mock.patch.object(model_sync, "STATE_FILE", state_file), \
                    mock.patch.object(model_sync, "BACKUP_DIR",
                                      root / "backups"):
                outcome = model_sync.clear_context_override("um", "model-a")
            self.assertEqual(outcome, {"existed": False, "changed": False})
            self.assertEqual(state_file.read_text("utf-8"), original)


class TestRetirementTombstoneApply(unittest.TestCase):
    """Tombstone lifecycle end to end (L-E.5: state progress must track the
    actual config application): the plan tombstones a confirmed-retired
    managed entry; run_sync deletes the key only when the removal is really
    applied, and a pending ZCode change keeps the tombstone so the config
    entry never strands as an untouchable manual extra."""

    KEY = "um|ZCode|local:openwebui"

    @staticmethod
    def _seed(tmp: Path):
        zcode_json = tmp / "config.json"
        zcode_json.write_text(json.dumps(
            {"provider": {"local:openwebui": {
                "name": "UM", "source": "custom",
                "options": {}, "models": {"gone": {"name": "gone"}}}}},
            ensure_ascii=False), encoding="utf-8")
        watch_stamp = (datetime.now(timezone.utc) - timedelta(hours=2))
        state = {
            "version": 2,
            "initializedBindings": [TestRetirementTombstoneApply.KEY],
            "managed": {TestRetirementTombstoneApply.KEY: {
                "gone": {"profile": {"name": "gone"}}}},
            "retirementWatch": {
                TestRetirementTombstoneApply.KEY + "|gone": {
                    "firstMissing": watch_stamp.isoformat(),
                    "lastMissing": (datetime.now(timezone.utc)
                                    - timedelta(hours=1)).isoformat(),
                    "evidenceRuns": 1}},
            "catalogCounts": {}, "pending": [], "probes": {},
            "contextProbes": {},
        }
        (tmp / "state.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8")
        return zcode_json

    def _run(self, tmp: Path, running: bool):
        zcode_json = self._seed(tmp)
        consumers = {self.KEY: {"providerName": "UM",
                                "models": {"gone": {"name": "gone"}}}}
        payload = zcode_json.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as io_root:
            io_dir = Path(io_root)
            # R20-2：规则库正常且与目录对齐——退役写入（pop gone）走的是
            # 主配置路径；缺席规则库会让整个 ZCode 写集合延期。
            (io_dir / "provider_config.json").write_text(
                json.dumps(aligned_rule_store(
                    {"um": [CatalogModel("um", "alive",
                                                   context=96000)]}),
                    ensure_ascii=False), encoding="utf-8")
            with mock.patch.object(model_sync, "DATA_DIR", io_dir), \
                    mock.patch.object(model_sync, "STATE_FILE",
                                      tmp / "state.json"), \
                    mock.patch.object(model_sync, "RESULT_FILE",
                                      io_dir / "result.json"), \
                    mock.patch.object(model_sync, "PREVIEW_FILE",
                                      io_dir / "preview.json"), \
                    mock.patch.object(model_sync, "HISTORY_FILE",
                                      io_dir / "history.jsonl"), \
                    mock.patch.object(model_sync, "BACKUP_DIR",
                                      io_dir / "backups"), \
                    mock.patch.object(model_sync, "ZCODE_JSON",
                                      zcode_json), \
                    mock.patch.object(model_sync, "ZCODE_PROVIDER_CONFIG",
                                      io_dir / "provider_config.json"), \
                    mock.patch.object(model_sync, "zcode_running",
                                      return_value=running), \
                    mock.patch.object(model_sync, "zcode_hot_sync_admission",
                                      return_value=(False, "测试：未准入")), \
                    mock.patch.object(
                        model_sync, "collect_catalogs",
                        return_value=({"um": [
                            CatalogModel("um", "alive",
                                         context=96000)]}, [])), \
                    mock.patch.object(
                        model_sync, "collect_consumers",
                        return_value=(consumers, "", payload)):
                result = model_sync.run_sync(
                    apply=True, run_probes=False, confirm=False)
        return result, zcode_json, json.loads(
            (tmp / "state.json").read_text(encoding="utf-8"))

    def test_applied_removal_deletes_the_tombstone(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            result, zcode_json, state = self._run(tmp, running=False)
            self.assertEqual(result["status"], "changed")
            self.assertNotIn("gone", state["managed"][self.KEY])
            # Rev11 单文件发布：config.json 不是第二个实时权威——退役只落在
            # 规则库/状态；旧文件保持夹具原样。
            self.assertIn("gone", json.loads(
                zcode_json.read_text(encoding="utf-8"))["provider"]
                ["local:openwebui"]["models"])

    def test_pending_removal_keeps_the_tombstone(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            result, zcode_json, state = self._run(tmp, running=True)
            self.assertEqual(result["status"], "pending")
            self.assertTrue(state["managed"][self.KEY]["gone"].get("retired"))
            self.assertIn("gone", json.loads(
                zcode_json.read_text(encoding="utf-8"))["provider"]
                ["local:openwebui"]["models"])


class TestSingleFilePublishAndRecovery(unittest.TestCase):
    """Rev11 单文件发布、迁移门与发布/状态失败对账（run_sync 端到端）。

    规则库（provider_config.json）是 ZCode 侧唯一实时权威：本轮模型变化只
    经它发布，config.json 只在规则库缺席的旧版本迁移路径（迁移门开启）才写。
    发布成功而状态落盘失败时按版本校验回退，并如实区分「文件已写」「已加载」
    「会话已采用」。每个用例只在自己的临时目录内构造双存储，绝不读写真实的
    ~/.zcode / ~/.dsh 配置。R27-HOT：运行中用例把安装版兼容准入按钉子给定
    （已准入/未准入），只测本类的发布/延期语义；真实准入核对见
    tests/test_zcode/hot_sync.py。
    """

    UM = Binding("um", "ZCode", "local:openwebui", "UM", "（UM）")

    def _layout(self, root, *, store_doc, config_doc, state_doc, dsh_text=None,
                 store_text=None):
        store = root / "provider_config.json"
        config = root / "config.json"
        state_file = root / "state.json"
        if store_text is not None:
            store.write_text(store_text, encoding="utf-8")
        elif store_doc is not None:
            store.write_text(json.dumps(store_doc, ensure_ascii=False),
                             encoding="utf-8")
        config.write_text(json.dumps(config_doc, ensure_ascii=False),
                          encoding="utf-8")
        state_file.write_text(json.dumps(state_doc, ensure_ascii=False),
                              encoding="utf-8")
        if dsh_text is not None:
            (root / "settings.yaml").write_text(dsh_text, encoding="utf-8")
        return store, config, state_file

    def _run(self, root, *, store_doc, config_doc, state_doc, catalogs,
             migration_verified=False, replace_failure=None, dsh_text=None,
             apply=True, before_publish=None, running=False,
             hot_admission=(False, "测试：未准入"), lock_wrapper=None,
             store_text=None, real_hot_admission=False):
        store = root / "provider_config.json"
        config = root / "config.json"
        state_file = root / "state.json"
        store, config, state_file = self._layout(
            root, store_doc=store_doc, config_doc=config_doc,
            state_doc=state_doc, dsh_text=dsh_text, store_text=store_text)
        config_before = config.read_bytes()
        store_before = store.read_bytes() if store.exists() else None
        patches = [
            mock.patch.object(model_sync, "ZCODE_PROVIDER_CONFIG", store),
            mock.patch.object(model_sync, "ZCODE_JSON", config),
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
                              return_value=running),
        ]
        if running and not real_hot_admission:
            # R27-HOT：运行中会真的调用安装版兼容准入；本类用例只测
            # 延期/发布语义，准入结论一律按钉子给定（真实准入见
            # tests/test_zcode/hot_sync.py 的独立核对）。
            patches.append(mock.patch.object(
                model_sync, "zcode_hot_sync_admission",
                return_value=hot_admission))
        if lock_wrapper is not None:
            patches.append(mock.patch.object(
                model_sync, "zcode_file_lock", lock_wrapper))
        if replace_failure is not None:
            patches.append(mock.patch.object(
                model_config_adapters, "replace_with_retry",
                side_effect=replace_failure))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            if migration_verified:
                stack.enter_context(mock.patch.object(
                    model_sync, "LEGACY_CONFIG_MIGRATION_VERIFIED", True))
            result = model_sync.run_sync(
                apply=apply, run_probes=False, confirm=False,
                before_publish=before_publish)
        return result, store, config, state_file, config_before, store_before

    @staticmethod
    def _store_doc(context=111, whitelist=("um-a",)):
        return {"schemaVersion": 1, "config": {
            "providerOrder": ["local:openwebui"],
            "providerConfigRules": {"providerRules": [{
                "providerId": "local:openwebui",
                "providerName": "UM",
                "enabled": True,
                "config": {"group": "standard-personal",
                           "personalModelIds": list(whitelist),
                           "modelOrder": list(whitelist)},
            }]},
            "modelConfigRules": {"providerModelRules": [
                {"modelId": mid, "providerId": "local:openwebui",
                 "config": {"properties": {"contextWindow": context}}}
                for mid in whitelist]},
        }}

    @staticmethod
    def _config_doc(context=111, whitelist=("um-a",)):
        return {"provider": {"local:openwebui": {
            "name": "UM", "models": {
                mid: {"name": mid + "（UM）",
                      "limit": {"context": context}} for mid in whitelist}}}}

    @staticmethod
    def _state_doc():
        state = empty_state()
        state["initializedBindings"] = [TestSingleFilePublishAndRecovery.UM.key]
        # R6/D4 三方比较夹具（2026-10-10）：客户端当前值（规则库 111 /
        # 主配置 111）就是本同步上次实际发布的基准——对齐夹具下上下文
        # 更新照常发布。本类用例测的是单文件发布/延期/回退语义，不是
        # 冲突保护（冲突/待裁定用例见 TestContextPublishProtection）。
        state["managed"] = {TestSingleFilePublishAndRecovery.UM.key: {
            "um-a": {"profile": {}, "published": {
                "context": 111, "at": "published-time"}}}}
        return state

    def _catalogs(self, context=200000):
        return {"um": [CatalogModel("um", "um-a", context=context)]}

    def test_apply_publishes_only_the_provider_config(self):
        # Rev11 单文件发布：上下文对齐（111→200000）只写权威文件
        # provider_config.json；config.json 保持字节不变（不是第二个实时权威）。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            result, store, config, state_file, config_before, _ = self._run(
                root, store_doc=self._store_doc(context=111),
                config_doc=self._config_doc(context=111),
                state_doc=self._state_doc(), catalogs=self._catalogs())
            self.assertEqual(result["status"], "changed")
            self.assertIn("更新 um-a", "\n".join(result["applied"]))
            after = json.loads(store.read_text("utf-8"))
            rule = next(
                rule for rule in after["config"]["modelConfigRules"]
                ["providerModelRules"] if rule["providerId"] == "local:openwebui")
            self.assertEqual(rule["config"]["properties"],
                             {"contextWindow": 200000})
            self.assertEqual(config.read_bytes(), config_before)
            # 生效边界：文件已写 ≠ 客户端已加载 ≠ 会话已采用。
            boundary = result["applicationBoundary"]
            self.assertEqual(boundary["filesWritten"], ["provider_config.json"])
            self.assertTrue(boundary["stateWritten"])
            self.assertEqual(boundary["clientLoaded"], "unverified")
            self.assertEqual(boundary["sessionAdopted"], "unverified")

    def test_running_publication_uses_real_protocol_admission(self):
        from zcode import hot_sync as zcode_hot_sync
        from zcode import hot_sync_protocol as zcode_hot_sync_protocol
        from tests.test_zcode_hot_sync import _asar_bytes, _layout_entries, _scan
        from tests.zcode_protocol_fixture import profile

        original = _layout_entries()
        trusted = profile(original["out/host/index.js"],
                          original["out/host/chunk-FNQWK6LT.js"])
        for compatible in (True, False):
            with self.subTest(compatible=compatible), tempfile.TemporaryDirectory(
                    ignore_cleanup_errors=True) as raw:
                root = Path(raw)
                installation = root / "installation"
                (installation / "resources").mkdir(parents=True)
                entries = _layout_entries(version="99.0.0")
                entries["out/host/index.js"] += b";const unrelatedFeature=1;"
                if not compatible:
                    entries["out/host/index.js"] = entries["out/host/index.js"].replace(
                        b"start(){return 1}", b"start(){return 0}")
                (installation / "resources" / "app.asar").write_bytes(
                    _asar_bytes(entries))
                with mock.patch.object(zcode_hot_sync, "zcode_process_scan",
                                       return_value=_scan(installation)), \
                        mock.patch.object(zcode_hot_sync_protocol,
                                          "PROTOCOL_PROFILE", trusted), \
                        mock.patch("subprocess.run", side_effect=AssertionError(
                            "Admission/publication must not restart the client")):
                    result, store, config, _, config_before, store_before = self._run(
                        root, store_doc=self._store_doc(context=111),
                        config_doc=self._config_doc(context=111),
                        state_doc=self._state_doc(), catalogs=self._catalogs(),
                        running=True, real_hot_admission=True)
                self.assertEqual(config.read_bytes(), config_before)
                if compatible:
                    self.assertEqual(result["status"], "changed")
                    self.assertNotEqual(store.read_bytes(), store_before)
                    self.assertEqual(result["applicationBoundary"]["filesWritten"],
                                     ["provider_config.json"])
                else:
                    self.assertEqual(result["status"], "pending")
                    self.assertEqual(store.read_bytes(), store_before)
                    self.assertEqual(result["applied"], [])
                    self.assertEqual(result["applicationBoundary"]["filesWritten"], [])

    def test_running_publication_uses_the_current_protocol_admission(self):
        # 3.15.1 当前布局（锁/持久化在独立 chunk、配置格式/监听在宿主）：
        # 真实 run_sync 入口 + 真实准入（不钉死结论）——等价改名后的安装
        # 仍按客户端锁协议发布 provider_config.json；关键实现变化保持
        # pending 且客户端字节不变。准入/发布都不得重启客户端。
        from zcode import hot_sync as zcode_hot_sync
        from zcode import hot_sync_protocol as zcode_hot_sync_protocol
        from tests import zcode_current_protocol_fixture as current
        from tests.test_zcode_hot_sync import _asar_bytes, _scan

        bundles = current.sources()
        trusted = current.profile(bundles)
        changed = dict(bundles)
        changed[current.LOCK_CHUNK] = changed[current.LOCK_CHUNK].replace(
            b'return!0}i(Jo,"isProcessAlive")',
            b'return!1}i(Jo,"isProcessAlive")')
        for label, candidate, compatible in (
                ("renamed-compatible", current.renamed(), True),
                ("changed-lock", changed, False)):
            with self.subTest(case=label), tempfile.TemporaryDirectory(
                    ignore_cleanup_errors=True) as raw:
                root = Path(raw)
                installation = root / "installation"
                (installation / "resources").mkdir(parents=True)
                entries = dict(candidate)
                entries["package.json"] = (
                    '{"name":"@zcode/desktop","version":"3.15.1"}').encode()
                (installation / "resources" / "app.asar").write_bytes(
                    _asar_bytes(entries))
                with mock.patch.object(zcode_hot_sync, "zcode_process_scan",
                                       return_value=_scan(installation)), \
                        mock.patch.object(
                            zcode_hot_sync_protocol,
                            "CURRENT_PROTOCOL_PROFILE", trusted), \
                        mock.patch("subprocess.run", side_effect=AssertionError(
                            "Admission/publication must not restart the client")):
                    result, store, config, _, config_before, store_before = self._run(
                        root, store_doc=self._store_doc(context=111),
                        config_doc=self._config_doc(context=111),
                        state_doc=self._state_doc(), catalogs=self._catalogs(),
                        running=True, real_hot_admission=True)
                self.assertEqual(config.read_bytes(), config_before)
                if compatible:
                    self.assertEqual(result["status"], "changed")
                    self.assertNotEqual(store.read_bytes(), store_before)
                    self.assertEqual(
                        result["applicationBoundary"]["filesWritten"],
                        ["provider_config.json"])
                else:
                    self.assertEqual(result["status"], "pending")
                    self.assertEqual(store.read_bytes(), store_before)
                    self.assertEqual(result["applied"], [])
                    self.assertEqual(
                        result["applicationBoundary"]["filesWritten"], [])

    def test_preview_boundary_reports_nothing_written(self):
        # 预览轮不写任何客户端文件：applicationBoundary 如实报空；
        # 加载/会话采用同样 unverified，不伪报已生效。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            result, store, config, state_file, config_before, _ = self._run(
                root, store_doc=self._store_doc(context=111),
                config_doc=self._config_doc(context=111),
                state_doc=self._state_doc(), catalogs=self._catalogs(),
                apply=False)
            self.assertEqual(result["status"], "preview")
            self.assertEqual(json.loads(store.read_text("utf-8")),
                             self._store_doc(context=111))
            self.assertEqual(config.read_bytes(), config_before)
            boundary = result["applicationBoundary"]
            self.assertEqual(boundary["filesWritten"], [])
            self.assertFalse(boundary["stateWritten"])
            self.assertEqual(boundary["clientLoaded"], "unverified")
            self.assertEqual(boundary["sessionAdopted"], "unverified")

    def test_absent_store_defers_without_the_migration_gate(self):
        # 规则库缺席 + 迁移兼容性未取证（迁移门关闭）：整个 ZCode 写集合
        # 延期——不写 config.json（「缺席就写」不是迁移条件），也不从零合成
        # 规则库；状态照常落盘并如实记录原因。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            result, store, config, state_file, config_before, _ = self._run(
                root, store_doc=None, config_doc=self._config_doc(),
                state_doc=self._state_doc(), catalogs=self._catalogs())
            self.assertEqual(result["status"], "pending")
            self.assertFalse(store.exists())
            self.assertEqual(config.read_bytes(), config_before)
            self.assertTrue(any("迁移" in line for line in result["pending"]))
            state = json.loads(state_file.read_text("utf-8"))
            self.assertEqual(state["lastStatus"], "pending")

    def test_migration_gate_writes_the_legacy_config_json(self):
        # 规则库缺席 + 迁移兼容性已取证（测试内翻转迁移门）：写完整
        # config.json（客户端启动时一次性导入）；绝不从零合成规则库。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            result, store, config, state_file, config_before, _ = self._run(
                root, store_doc=None, config_doc=self._config_doc(),
                state_doc=self._state_doc(), catalogs=self._catalogs(),
                migration_verified=True)
            self.assertEqual(result["status"], "changed")
            self.assertFalse(store.exists())
            self.assertNotEqual(config.read_bytes(), config_before)
            after = json.loads(config.read_text("utf-8"))
            self.assertIn("um-a",
                          after["provider"]["local:openwebui"]["models"])
            self.assertTrue(result["zcodeChanged"])

    def test_cancel_before_publish_writes_nothing(self):
        # 取消路径：发布前（before_publish）取消 → 事务不执行、零写入；
        # 旧 config.json 与规则库/状态都保持原样，绝不留下半份配置。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            store_doc = self._store_doc(context=111)

            def cancel():
                raise RuntimeError("cancelled")

            with self.assertRaises(RuntimeError):
                self._run(root, store_doc=store_doc,
                           config_doc=self._config_doc(context=111),
                           state_doc=self._state_doc(),
                           catalogs=self._catalogs(),
                           before_publish=cancel)
            store = root / "provider_config.json"
            state_file = root / "state.json"
            self.assertEqual(json.loads(store.read_text("utf-8")), store_doc)
            self.assertEqual(
                json.loads(state_file.read_text("utf-8")),
                self._state_doc())

    def test_state_write_failure_rolls_back_and_reports_recovery(self):
        # 发布成功而状态落盘失败：客户端文件按版本校验回退，失败结果区分
        # 已发布/已回退/保留现状，并给出恢复/对账路径；状态文件保持旧字节。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            store = root / "provider_config.json"
            state_file = root / "state.json"
            store_doc = self._store_doc(context=111)
            store_before = json.dumps(store_doc, ensure_ascii=False).encode("utf-8")
            real_replace = model_config_adapters.replace_with_retry

            def fail_state(source, target, **kwargs):
                if Path(target) == state_file:
                    raise OSError(errno.EACCES, "state busy")
                return real_replace(source, target, **kwargs)

            with self.assertRaises(
                    model_config_adapters.ConfigPublishError) as caught:
                self._run(root, store_doc=store_doc,
                           config_doc=self._config_doc(context=111),
                           state_doc=self._state_doc(),
                           catalogs=self._catalogs(),
                           replace_failure=fail_state)
            self.assertEqual(store.read_bytes(), store_before)
            self.assertEqual(caught.exception.failed_path, state_file)
            self.assertEqual(caught.exception.published, [store])
            self.assertEqual(caught.exception.rolled_back, [store])
            self.assertEqual(caught.exception.left_in_place, [])
            recovery = model_sync._publish_recovery(caught.exception)
            self.assertFalse(recovery["stateWritten"])
            self.assertFalse(recovery["reconcileRequired"])
            self.assertEqual(recovery["rolledBack"], ["provider_config.json"])
            self.assertIn("重跑", recovery["recovery"])

    def test_effort_without_client_evidence_is_not_replanned(self):
        # 客户端（规则库）不携带档位证据：把已应用档位当差异会让计划每轮
        # 产生一条永远无法落盘的「更新」。第二轮必须幂等（status=normal）。
        catalogs = {"um": [CatalogModel("um", "um-a", context=131072,
                                        efforts=("low", "high"),
                                        default_effort="low",
                                        efforts_source="upstream")]}
        state = empty_state()
        state["modelSettings"] = {"um|um-a": {
            "draft": {"alias": None, "contextSource": None,
                      "contextTokens": None, "defaultEffort": "high"},
            "draftVersion": 1, "draftSavedAt": "t0",
            "applied": None, "appliedVersion": None, "appliedAt": None,
            "appliedEvidence": None}}
        state, promoted = model_sync._project_setting_drafts(
            state, catalogs, {"um|um-a": 1})
        self.assertEqual(promoted, ["um|um-a"])
        dsh_text = ("llm-pi-ai:\n  providers:\n    openwebui:\n"
                    "      displayName: UM\n      models:\n"
                    "        - id: um-a\n          name: um-a（UM）\n"
                    "          contextWindow: 131072\n")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            result, store, config, state_file, _, _ = self._run(
                root, store_doc=self._store_doc(context=131072),
                config_doc=self._config_doc(context=131072),
                state_doc=state, catalogs=catalogs, dsh_text=dsh_text)
            applied = "\n".join(result["applied"])
            # 档位只经客户端确实带证据的通道（DSH YAML）发布；ZCode 侧
            # 没有可落盘的档位差异，不报「更新」。
            self.assertIn("DSH 更新 um-a", applied)
            self.assertNotIn("ZCode 更新 um-a", applied)
            applied_record = json.loads(
                state_file.read_text("utf-8"))["modelSettings"]["um|um-a"]
            self.assertEqual(applied_record["applied"]["defaultEffort"], "high")
            # 第二轮：客户端仍无档位证据，计划必须幂等（不再报更新）。
            result2, _, _, _, _, _ = self._run(
                root, store_doc=self._store_doc(context=131072),
                config_doc=self._config_doc(context=131072),
                state_doc=json.loads(state_file.read_text("utf-8")),
                catalogs=catalogs,
                dsh_text=(root / "settings.yaml").read_text("utf-8"))
            self.assertEqual(result2["status"], "normal")
            self.assertEqual(result2["applied"], [])

    def test_concurrent_edit_during_state_failure_is_not_overwritten(self):
        # 状态落盘失败前并发写者改写了规则库：回退必须校验当前版本，
        # 保留现状待核对（绝不盖回旧快照），并如实要求重跑对账。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            store = root / "provider_config.json"
            state_file = root / "state.json"
            store_doc = self._store_doc(context=111)
            real_replace = model_config_adapters.replace_with_retry

            def fail_state(source, target, **kwargs):
                if Path(target) == state_file:
                    # 并发写者：规则库替换后、状态落盘失败前改写它。
                    store.write_text("concurrent-edit", encoding="utf-8")
                    raise OSError(errno.EACCES, "state busy")
                return real_replace(source, target, **kwargs)

            with self.assertRaises(
                    model_config_adapters.ConfigPublishError) as caught:
                self._run(root, store_doc=store_doc,
                           config_doc=self._config_doc(context=111),
                           state_doc=self._state_doc(),
                           catalogs=self._catalogs(),
                           replace_failure=fail_state)
            self.assertEqual(store.read_text("utf-8"), "concurrent-edit")
            self.assertEqual(caught.exception.published, [store])
            self.assertEqual(caught.exception.rolled_back, [])
            self.assertEqual(caught.exception.left_in_place, [store])
            recovery = model_sync._publish_recovery(caught.exception)
            self.assertTrue(recovery["reconcileRequired"])
            self.assertEqual(recovery["leftInPlace"], ["provider_config.json"])
            self.assertIn("人工核对", recovery["recovery"])

    # ---- R27-HOT：运行中同步（安装版兼容准入） ----

    def test_running_client_with_admitted_build_publishes_the_rule_store(self):
        # 运行中且安装版已取证（准入放行）：本轮 ZCode 写入真的发生——
        # 经客户端锁协议发布 provider_config.json（单文件权威），写完整
        # 受管候选；config.json 仍不是第二个实时权威（字节不变）；
        # 生效边界如实区分「已写 / 未验证加载 / 未验证采用」。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            result, store, config, state_file, config_before, _ = self._run(
                root, store_doc=self._store_doc(context=111),
                config_doc=self._config_doc(context=111),
                state_doc=self._state_doc(), catalogs=self._catalogs(),
                running=True, hot_admission=(True, "已取证版本 3.14.4"))
            self.assertEqual(result["status"], "changed")
            self.assertEqual(result["pending"], [])
            after = json.loads(store.read_text("utf-8"))["config"]
            rule = next(
                rule for rule in after["providerConfigRules"]["providerRules"]
                if rule["providerId"] == "local:openwebui")
            self.assertEqual(rule["config"]["personalModelIds"], ["um-a"])
            self.assertEqual(rule["config"]["modelOrder"], ["um-a"])
            self.assertEqual(after["providerOrder"], ["local:openwebui"])
            model_rules = {
                item["modelId"]: item for item in
                after["modelConfigRules"]["providerModelRules"]
                if item["providerId"] == "local:openwebui"}
            self.assertEqual(
                model_rules["um-a"]["config"]["properties"],
                {"contextWindow": 200000})
            # 单文件发布：旧 config.json 不是第二个实时权威，字节不变。
            self.assertEqual(config.read_bytes(), config_before)
            # 生效边界：文件已写 ≠ 客户端已加载 ≠ 会话已采用。
            boundary = result["applicationBoundary"]
            self.assertIn("provider_config.json", boundary["filesWritten"])
            self.assertTrue(boundary["stateWritten"])
            self.assertEqual(boundary["clientLoaded"], "unverified")
            self.assertEqual(boundary["sessionAdopted"], "unverified")
            # 锁协议正常释放：写入窗口结束后锁目录不残留。
            self.assertFalse(Path(str(store) + ".lock").exists())

    def test_running_client_without_admission_defers_the_write(self):
        # 运行中但安装版未准入（fail closed）：与既有延期语义一致——
        # 整体延期、零写入，原因里带准入说明。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            result, store, config, state_file, config_before, store_before = \
                self._run(
                    root, store_doc=self._store_doc(context=111),
                    config_doc=self._config_doc(context=111),
                    state_doc=self._state_doc(), catalogs=self._catalogs(),
                    running=True, hot_admission=(False, "包版本 3.14.3 不一致"))
            self.assertEqual(result["status"], "pending")
            self.assertEqual(result["applied"], [])
            self.assertTrue(any("包版本 3.14.3 不一致" in line
                               for line in result["pending"]))
            self.assertEqual(store.read_bytes(), store_before)
            self.assertEqual(config.read_bytes(), config_before)
            state = json.loads(state_file.read_text("utf-8"))
            self.assertEqual(state["lastStatus"], "pending")
            self.assertEqual(state["managed"][self.UM.key],
                             self._state_doc()["managed"][self.UM.key])
            self.assertTrue(state["pending"])

    def test_running_client_with_corrupted_store_defers_the_write(self):
        # 运行中且已准入，但规则库损坏（无法解析）：R20-2 仍整体延期——
        # 损坏文本逐字节保留（绝不「修复」），config.json 不写。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            result, store, config, state_file, config_before, _ = self._run(
                root, store_doc=None, store_text="not json",
                config_doc=self._config_doc(context=111),
                state_doc=self._state_doc(), catalogs=self._catalogs(),
                running=True, hot_admission=(True, "已取证版本 3.14.4"))
            self.assertEqual(result["status"], "pending")
            self.assertEqual(result["applied"], [])
            self.assertEqual(store.read_text("utf-8"), "not json")
            self.assertEqual(config.read_bytes(), config_before)
            self.assertTrue(any("规则库" in line for line in result["pending"]))

    def test_running_client_with_concurrent_managed_edit_defers_the_write(self):
        # 运行中已准入：锁内重读发现受管范围被并发修改（版本/指纹冲突）
        # → 本轮 ZCode 写集合整体延期，绝不覆盖并发编辑。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            store = root / "provider_config.json"
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

            result, store, config, state_file, config_before, _ = self._run(
                root, store_doc=self._store_doc(context=111),
                config_doc=self._config_doc(context=111),
                state_doc=self._state_doc(), catalogs=self._catalogs(),
                running=True, hot_admission=(True, "已取证版本 3.14.4"),
                lock_wrapper=concurrent_edit)
            self.assertEqual(result["status"], "pending")
            self.assertTrue(any("版本/指纹冲突" in line
                               for line in result["pending"]))
            self.assertFalse(result["zcodeChanged"])
            # 并发编辑原样保留：规则库逐字节未被覆盖，旧配置也没写。
            self.assertEqual(store.read_text("utf-8"), concurrent["text"])
            self.assertEqual(config.read_bytes(), config_before)

    def test_running_client_lock_timeout_writes_nothing(self):
        # 运行中已准入但客户端锁在写入窗口内不可获得：抛
        # ZCodeFileLockTimeout，零写入（规则库/旧配置/状态逐字节不变，
        # 结果文件不产生）——绝不把「没拿到锁」当「没有并发写者」。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            store = root / "provider_config.json"
            store.write_text(json.dumps(self._store_doc(context=111),
                                      ensure_ascii=False), encoding="utf-8")
            lock_path = Path(str(store) + ".lock")
            lock_path.mkdir()
            (lock_path / "owner-test.json").write_text(json.dumps(
                {"pid": os.getpid(), "token": "test",
                 "createdAt": int(time.time() * 1000)}), encoding="utf-8")
            store_before = store.read_bytes()
            state_before = json.dumps(self._state_doc(),
                                     ensure_ascii=False)

            @contextlib.contextmanager
            def short_lock(path, **kwargs):
                # 真实锁协议、缩短等待：活 owner 不可回收，达到 maxWait
                # 即抛超时（与生产默认 8s 同一条路径，只是不等 8 秒）。
                with zcode_file_lock.zcode_file_lock(
                        path, retry_delays_ms=(), max_wait_ms=1):
                    yield

            with self.assertRaises(zcode_file_lock.ZCodeFileLockTimeout):
                self._run(
                    root, store_doc=self._store_doc(context=111),
                    config_doc=self._config_doc(context=111),
                    state_doc=self._state_doc(), catalogs=self._catalogs(),
                    running=True, hot_admission=(True, "已取证版本 3.14.4"),
                    lock_wrapper=short_lock)
            self.assertEqual(store.read_bytes(), store_before)
            self.assertEqual(
                (root / "config.json").read_text("utf-8"),
                json.dumps(self._config_doc(context=111), ensure_ascii=False))
            self.assertEqual(
                (root / "state.json").read_text("utf-8"), state_before)
            self.assertFalse((root / "result.json").exists())
            # 超时后锁目录原样保留（属于未退出的 owner）。
            self.assertTrue(lock_path.is_dir())

    def test_running_client_cancel_before_publish_writes_nothing(self):
        # 运行中已准入但发布前取消（before_publish）：事务不执行、零写入，
        # 规则库/旧配置/状态保持原样，锁不残留。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            store_doc = self._store_doc(context=111)

            def cancel():
                raise RuntimeError("cancelled")

            with self.assertRaises(RuntimeError):
                self._run(
                    root, store_doc=store_doc,
                    config_doc=self._config_doc(context=111),
                    state_doc=self._state_doc(), catalogs=self._catalogs(),
                    running=True, hot_admission=(True, "已取证版本 3.14.4"),
                    before_publish=cancel)
            store = root / "provider_config.json"
            self.assertEqual(json.loads(store.read_text("utf-8")), store_doc)
            self.assertEqual(
                json.loads((root / "state.json").read_text("utf-8")),
                self._state_doc())
            self.assertFalse(Path(str(store) + ".lock").exists())

    def test_running_client_with_incomplete_evidence_defers_the_write(self):
        # R28-ADMISSION：运行中走真实准入，但进程枚举证据不完整（存在身份/
        # 镜像不可确认的进程）→ 准入明确拒绝 → 整体 pending、客户端候选
        # 零发布；绝不以部分结果放行运行中写入。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            incomplete = zcode_hot_sync.ZCodeProcessScan(
                paths=((4242, str(root / "ZCode.exe")),),
                exited_pids=(), unreadable_pids=(5151,),
                snapshot_error=None, enumeration_error=None)
            with mock.patch.object(zcode_hot_sync, "zcode_process_scan",
                                   return_value=incomplete):
                result, store, config, state_file, config_before, store_before = \
                    self._run(
                        root, store_doc=self._store_doc(context=111),
                        config_doc=self._config_doc(context=111),
                        state_doc=self._state_doc(), catalogs=self._catalogs(),
                        running=True, real_hot_admission=True)
            self.assertEqual(result["status"], "pending")
            self.assertEqual(result["applied"], [])
            self.assertFalse(result["zcodeChanged"])
            self.assertTrue(any("5151" in line for line in result["pending"]))
            # 客户端候选零发布：规则库与旧配置逐字节不变，生效边界没有任何
            # 客户端文件被写。
            self.assertEqual(store.read_bytes(), store_before)
            self.assertEqual(config.read_bytes(), config_before)
            self.assertEqual(result["applicationBoundary"]["filesWritten"], [])
            state = json.loads(state_file.read_text("utf-8"))
            self.assertEqual(state["lastStatus"], "pending")
            # 同步侧结果记录（result.json）不是客户端发布物：它如实记录本轮
            # pending，绝不宣称任何客户端文件已发布。（run_sync 在 apply 路径
            # 总会写这份记录，见 model_sync/sync.py:3777 与 _write_result。）
            recorded = json.loads((root / "result.json").read_text("utf-8"))
            self.assertEqual(recorded["status"], "pending")
            self.assertEqual(recorded["applicationBoundary"]["filesWritten"], [])

    def test_running_client_with_corrupt_asar_defers_the_write(self):
        # R28-ADMISSION：运行中走真实准入，但安装的 asar 头部损坏（深层
        # 嵌套 JSON 让 json.loads 抛 RecursionError，RuntimeError 子类）→
        # 准入明确拒绝 → 整体 pending、客户端候选零发布；损坏头部绝不穿透
        # 整轮同步（run_sync 的准入调用点不包 try）。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            asar = root / "resources" / "app.asar"
            asar.parent.mkdir(parents=True, exist_ok=True)
            payload = b"[" * 100000 + b"]" * 100000
            asar.write_bytes(
                struct.pack("<IIII", 4, 8 + len(payload), 8 + len(payload),
                            len(payload)) + payload)
            complete = zcode_hot_sync.ZCodeProcessScan(
                paths=((4242, str(root / "ZCode.exe")),),
                exited_pids=(), unreadable_pids=(),
                snapshot_error=None, enumeration_error=None)
            with mock.patch.object(zcode_hot_sync, "zcode_process_scan",
                                   return_value=complete):
                result, store, config, state_file, config_before, store_before = \
                    self._run(
                        root, store_doc=self._store_doc(context=111),
                        config_doc=self._config_doc(context=111),
                        state_doc=self._state_doc(), catalogs=self._catalogs(),
                        running=True, real_hot_admission=True)
            self.assertEqual(result["status"], "pending")
            self.assertEqual(result["applied"], [])
            self.assertFalse(result["zcodeChanged"])
            self.assertTrue(any("头部" in line for line in result["pending"]))
            # 客户端候选零发布：规则库与旧配置逐字节不变，生效边界没有任何
            # 客户端文件被写。
            self.assertEqual(store.read_bytes(), store_before)
            self.assertEqual(config.read_bytes(), config_before)
            self.assertEqual(result["applicationBoundary"]["filesWritten"], [])
            state = json.loads(state_file.read_text("utf-8"))
            self.assertEqual(state["lastStatus"], "pending")
            recorded = json.loads((root / "result.json").read_text("utf-8"))
            self.assertEqual(recorded["status"], "pending")
            self.assertEqual(recorded["applicationBoundary"]["filesWritten"], [])


def _completed_tasklist(returncode, stdout="", stderr=""):
    """真实 CompletedProcess（含退出码）——替身不得只给 stdout。"""
    return subprocess.CompletedProcess(
        ["tasklist"], returncode, stdout=stdout, stderr=stderr)


def _hot_scan(*, paths=(), complete=True, unreadable=()):
    """结构化枚举替身：complete=False 或存在不可确认进程 → 证据不完整。"""
    if complete and not unreadable:
        return zcode_hot_sync.ZCodeProcessScan(
            tuple(paths), (), (), None, None)
    return zcode_hot_sync.ZCodeProcessScan(
        tuple(paths), (), tuple(unreadable),
        None if unreadable else 5, None)


class ZCodeRunningGateFailClosedTests(unittest.TestCase):
    """R28/R29 同族：运行门必须 fail closed——只有确证「没在运行」才返回 False。

    维护者的契约是「任一步失败拒绝」：tasklist 抛异常、返回非零退出码、返回
    空结果或不可识别结果，都不能被当成「没在运行」而跳过运行门与准入；确证
    「无匹配进程」用结构化进程枚举事实兜底（不靠排除特定错误文案）。
    镜像名匹配用大小写不敏感比较（与本模块其它处同口径）。
    """

    def _observe(self, *, run_result=None, raise_run=False, scan=None):
        """在替身下同时读三态与布尔门（同一个查询只打一次钉子）。"""
        with contextlib.ExitStack() as stack:
            if raise_run:
                def boom(*args, **kwargs):
                    raise FileNotFoundError("tasklist")
                stack.enter_context(mock.patch.object(subprocess, "run", boom))
            else:
                stack.enter_context(mock.patch.object(
                    subprocess, "run", return_value=run_result))
            stack.enter_context(mock.patch.object(
                zcode_hot_sync, "zcode_process_scan",
                return_value=_hot_scan(**(scan or {"paths": ()}))))
            return (model_config_adapters.zcode_running_state(),
                    model_config_adapters.zcode_running())

    def test_exception_is_unknown_and_treated_as_running(self):
        self.assertEqual(self._observe(raise_run=True), ("unknown", True),
                         "tasklist 异常必须按运行中处理，绝不跳过运行门与准入")

    def test_nonzero_return_code_is_unknown(self):
        # subprocess.run 不会因非零退出抛异常：失败码不是结果，必须 unknown。
        for code in (1, 5):
            with self.subTest(returncode=code):
                self.assertEqual(
                    self._observe(run_result=_completed_tasklist(
                        code, stdout="", stderr="query failed")),
                    ("unknown", True))

    def test_success_with_lowercase_image_line_is_running(self):
        self.assertEqual(
            self._observe(run_result=_completed_tasklist(
                0, stdout="\nzcode.exe  1234 Console  1  100 K\n")),
            ("running", True))

    def test_success_without_recognizable_line_is_confirmed_by_the_scan(self):
        # 本地化的「没有匹配任务」提示：不能靠排除特定文案判定，
        # 必须用结构化枚举确证「无匹配进程」才允许 not_running。
        message = "信息: 没有运行的任务匹配指定标准。\n"
        self.assertEqual(
            self._observe(run_result=_completed_tasklist(0, stdout=message),
                          scan={"paths": (), "complete": True}),
            ("not_running", False))
        self.assertEqual(
            self._observe(run_result=_completed_tasklist(0, stdout=message),
                          scan={"paths": ((101, "D:\\apps\\zcode\\ZCode.exe"),),
                                "complete": True}),
            ("running", True))

    def test_empty_or_unknown_result_with_incomplete_scan_is_unknown(self):
        # 成功但没有可识别结果 + 枚举无法确证：既非运行中也非未运行 → unknown。
        for stdout in ("", "totally unexpected output\n"):
            with self.subTest(stdout=stdout):
                self.assertEqual(
                    self._observe(run_result=_completed_tasklist(0, stdout=stdout),
                                  scan={"paths": (), "complete": False}),
                    ("unknown", True))
                self.assertEqual(
                    self._observe(run_result=_completed_tasklist(0, stdout=stdout),
                                  scan={"paths": ((101, "x"),),
                                        "unreadable": (202,)}),
                    ("unknown", True))


class ZCodeRunningGateEntryTests(unittest.TestCase):
    """R29-RUN-GATE 入口级负例：查询失败时 run_sync 必须调用准入、整体延期、零客户端写入。

    真实 ``run_sync`` 调用真实运行门（只替换 tasklist 子进程、结构化枚举与
    准入结论）；目录、状态与客户端文件全在临时目录内，不发请求。
    """

    def _drive(self, root, *, run_result=None, raise_run=False,
                scan=None):
        fixture = TestSingleFilePublishAndRecovery()
        store, config, state_file = fixture._layout(
            root, store_doc=fixture._store_doc(),
            config_doc=fixture._config_doc(),
            state_doc=fixture._state_doc())
        store_before = store.read_bytes()
        config_before = config.read_bytes()
        with contextlib.ExitStack() as stack:
            for name, path in (
                    ("ZCODE_PROVIDER_CONFIG", store),
                    ("ZCODE_JSON", config),
                    ("DSH_YAML", root / "settings.yaml"),
                    ("STATE_FILE", state_file),
                    ("RESULT_FILE", root / "result.json"),
                    ("PREVIEW_FILE", root / "preview.json"),
                    ("HISTORY_FILE", root / "history.jsonl"),
                    ("BACKUP_DIR", root / "backups")):
                stack.enter_context(mock.patch.object(model_sync, name, path))
            stack.enter_context(mock.patch.object(
                model_sync, "collect_catalogs",
                return_value=(fixture._catalogs(), [])))
            if raise_run:
                def boom(*args, **kwargs):
                    raise FileNotFoundError("tasklist")
                stack.enter_context(mock.patch.object(subprocess, "run", boom))
            else:
                stack.enter_context(mock.patch.object(
                    subprocess, "run", return_value=run_result))
            stack.enter_context(mock.patch.object(
                zcode_hot_sync, "zcode_process_scan",
                return_value=_hot_scan(**(scan or {"paths": ()}))))
            admission = stack.enter_context(mock.patch.object(
                model_sync, "zcode_hot_sync_admission",
                return_value=(False, "测试：未准入")))
            result = model_sync.run_sync(
                apply=True, run_probes=False, confirm=False)
        return (result, admission.call_count, store_before, store.read_bytes(),
                config_before, config.read_bytes())

    def _assert_deferred_without_writes(self, **kwargs):
        with tempfile.TemporaryDirectory() as raw:
            (result, calls, store_before, store_after, config_before,
             config_after) = self._drive(Path(raw), **kwargs)
        self.assertGreaterEqual(calls, 1, "准入必须被实际调用，不得绕过")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["applied"], [])
        self.assertEqual(store_after, store_before, "客户端规则库不得被写入")
        self.assertEqual(config_after, config_before, "客户端主配置不得被写入")
        self.assertEqual(result["applicationBoundary"]["filesWritten"], [])

    def test_nonzero_return_defers_and_never_writes(self):
        for code in (1, 5):
            with self.subTest(returncode=code):
                self._assert_deferred_without_writes(run_result=(
                    subprocess.CompletedProcess(
                        ["tasklist"], code, stdout="", stderr="query failed")))

    def test_empty_result_defers_and_never_writes(self):
        self._assert_deferred_without_writes(run_result=(
            subprocess.CompletedProcess(["tasklist"], 0, stdout="", stderr="")),
            scan={"paths": (), "complete": False})

    def test_unrecognizable_result_defers_and_never_writes(self):
        self._assert_deferred_without_writes(run_result=(
            subprocess.CompletedProcess(
                ["tasklist"], 0, stdout="totally unexpected output\n",
                stderr="")),
            scan={"paths": (), "complete": False})

    def test_query_exception_defers_and_never_writes(self):
        self._assert_deferred_without_writes(raise_run=True)

    def test_confirmed_not_running_keeps_the_unwatched_path(self):
        # 保留：确证「没有匹配进程」时走既有未运行路径（不调用准入、正常发布）。
        with tempfile.TemporaryDirectory() as raw:
            (result, calls, store_before, store_after, config_before,
             config_after) = self._drive(
                Path(raw),
                run_result=subprocess.CompletedProcess(
                    ["tasklist"], 0,
                    stdout="信息: 没有运行的任务匹配指定标准。\n", stderr=""),
                scan={"paths": (), "complete": True})
        self.assertEqual(calls, 0, "确证未运行不得调用准入")
        self.assertEqual(result["status"], "changed")
        self.assertNotEqual(store_after, store_before,
                            "确证未运行应走既有未运行发布路径")


class TestContextPublishProtection(unittest.TestCase):
    """R6/D4 三方比较端到端（2026-10-10）：发布基准 / 客户端当前值 / 目标值。

    客户端上下文无法证明是「本同步上次实际发布的基准」时绝不覆盖，冲突
    单独成通道进预览待裁定；真正发布后在同一状态事务里登记基准。夹具全是
    临时目录内的合成双存储，绝不读写真实的 ~/.zcode / ~/.dsh 配置。
    """

    UM = Binding("um", "ZCode", "local:openwebui", "UM", "（UM）")
    KEY = "um|um-a"

    def _layout(self, root, *, store_doc, config_doc, state_doc):
        store = root / "provider_config.json"
        config = root / "config.json"
        state_file = root / "state.json"
        store.write_text(json.dumps(store_doc, ensure_ascii=False),
                          encoding="utf-8")
        config.write_text(json.dumps(config_doc, ensure_ascii=False),
                           encoding="utf-8")
        state_file.write_text(json.dumps(state_doc, ensure_ascii=False),
                              encoding="utf-8")
        return store, config, state_file

    @staticmethod
    def _store_doc(context=131072):
        return {"schemaVersion": 1, "config": {
            "providerOrder": ["local:openwebui"],
            "providerConfigRules": {"providerRules": [{
                "providerId": "local:openwebui",
                "providerName": "UM",
                "enabled": True,
                "config": {"group": "standard-personal",
                           "personalModelIds": ["um-a"],
                           "modelOrder": ["um-a"]},
            }]},
            "modelConfigRules": {"providerModelRules": [{
                "modelId": "um-a",
                "providerId": "local:openwebui",
                "config": {"properties": {"contextWindow": context}},
            }]},
        }}

    @staticmethod
    def _config_doc(context=131072):
        return {"provider": {"local:openwebui": {
            "name": "UM", "models": {
                "um-a": {"name": "um-a（UM）",
                          "limit": {"context": context}}}}}}

    @staticmethod
    def _state_doc(managed=None, settings=None):
        state = empty_state()
        state["initializedBindings"] = [TestContextPublishProtection.UM.key]
        state["managed"] = {TestContextPublishProtection.UM.key:
                            (managed if managed is not None else
                             {"um-a": {"profile": {}}})}
        state["modelSettings"] = dict(settings or {})
        return state

    def _run(self, root, *, store_doc, config_doc, state_doc, catalogs,
             apply=True, selection=None, token=None, running=False,
             before_publish=None):
        store = root / "provider_config.json"
        config = root / "config.json"
        state_file = root / "state.json"
        store, config, state_file = self._layout(
            root, store_doc=store_doc, config_doc=config_doc,
            state_doc=state_doc)
        config_before = config.read_bytes()
        store_before = store.read_bytes()
        with mock.patch.object(model_sync, "ZCODE_PROVIDER_CONFIG", store), \
                mock.patch.object(model_sync, "ZCODE_JSON", config), \
                mock.patch.object(model_sync, "DSH_YAML",
                                  root / "settings.yaml"), \
                mock.patch.object(model_sync, "STATE_FILE", state_file), \
                mock.patch.object(model_sync, "RESULT_FILE",
                                  root / "result.json"), \
                mock.patch.object(model_sync, "PREVIEW_FILE",
                                  root / "preview.json"), \
                mock.patch.object(model_sync, "HISTORY_FILE",
                                  root / "history.jsonl"), \
                mock.patch.object(model_sync, "BACKUP_DIR", root / "backups"), \
                mock.patch.object(model_sync, "collect_catalogs",
                                  return_value=(catalogs, [])), \
                mock.patch.object(model_sync, "zcode_running",
                                  return_value=running), \
                mock.patch.object(model_sync, "zcode_hot_sync_admission",
                                  return_value=(False, "测试：未准入")):
            if selection is not None and token is None and apply:
                # 真实两段流：先预览拿 preview_token，再应用；预览零写入。
                preview = model_sync.run_sync(
                    apply=False, run_probes=False, confirm=False,
                    settings_selection=selection)
                token = preview["preview_token"]
            result = model_sync.run_sync(
                apply=apply, run_probes=False, confirm=False,
                settings_selection=selection, preview_token=token,
                before_publish=before_publish)
        return result, store, state_file, config, config_before, store_before

    def _catalogs(self, context=200000):
        return {"um": [CatalogModel("um", "um-a", context=context)]}

    def _store_context(self, store):
        doc = json.loads(store.read_text(encoding="utf-8"))
        rule = next(rule for rule in doc["config"]["modelConfigRules"]
                    ["providerModelRules"]
                    if rule["modelId"] == "um-a")
        return rule["config"]["properties"].get("contextWindow")

    def test_manual_client_context_is_never_overwritten(self):
        # 客户端 131072（对齐夹具）与目录 200000 不同、无基准记录 → 冲突：
        # 不规划更新、不覆盖客户端值、不删字段；冲突进独立通道待裁定。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            result, store, state_file, config, config_before, store_before = \
                self._run(
                    root, store_doc=self._store_doc(context=131072),
                    config_doc=self._config_doc(context=131072),
                    state_doc=self._state_doc(), catalogs=self._catalogs())
            self.assertEqual(result["applied"], [])
            self.assertEqual(store.read_bytes(), store_before)
            self.assertEqual(config.read_bytes(), config_before)
            conflicts = result["contextConflicts"]
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0]["model_id"], "um-a")
            self.assertEqual(conflicts[0]["client"], 131072)
            self.assertEqual(conflicts[0]["target"], 200000)
            self.assertIsNone(conflicts[0]["baseline"])
            self.assertFalse(conflicts[0]["baseline_recorded"])
            self.assertFalse(conflicts[0]["decided"])
            self.assertEqual(self._store_context(store), 131072)

    def test_publish_baseline_is_recorded_and_reused(self):
        # 上次实际发布基准 131072 == 客户端当前值 → 可以发布；发布后基准在同一
        # 状态事务里登记为真正写入的 200000；重放轮读回客户端值相等 → 无变化。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            state_doc = self._state_doc(managed={"um-a": {
                "profile": {}, "published": {
                    "context": 131072, "at": "published-time"}}})
            result, store, state_file, _, _, _ = self._run(
                root, store_doc=self._store_doc(context=131072),
                config_doc=self._config_doc(context=131072),
                state_doc=state_doc, catalogs=self._catalogs())
            self.assertTrue(any("更新 um-a" in line for line in result["applied"]))
            self.assertEqual(result["contextConflicts"], [])
            self.assertEqual(self._store_context(store), 200000)
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(
                state["managed"][self.UM.key]["um-a"]["published"]["context"],
                200000)
            # 重放轮：客户端现在持有我们上次发布的 200000、目录仍是 200000 →
            # 相等，无更新、无冲突；基准保持 200000。
            replay, store2, state_file2, _, _, _ = self._run(
                root, store_doc=json.loads(store.read_text("utf-8")),
                config_doc=json.loads(store.read_text(encoding="utf-8")),
                state_doc=state, catalogs=self._catalogs())
            self.assertEqual(replay["applied"], [])
            self.assertEqual(replay["contextConflicts"], [])
            state2 = json.loads(state_file2.read_text(encoding="utf-8"))
            self.assertEqual(
                state2["managed"][self.UM.key]["um-a"]["published"]["context"],
                200000)

    def test_deferred_round_never_advances_the_baseline(self):
        # 延期轮零客户端写入：基准绝不前进（与受管条目一起回退到本轮之前）。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            state_doc = self._state_doc(managed={"um-a": {
                "profile": {}, "published": {
                    "context": 131072, "at": "published-time"}}})
            result, store, state_file, _, _, store_before = self._run(
                root, store_doc=self._store_doc(context=131072),
                config_doc=self._config_doc(context=131072),
                state_doc=state_doc, catalogs=self._catalogs(),
                running=True)
            self.assertEqual(result["status"], "pending")
            self.assertEqual(store.read_bytes(), store_before)
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(
                state["managed"][self.UM.key]["um-a"]["published"]["context"],
                131072)

    def test_settings_apply_with_explicit_context_decision_publishes_and_records(self):
        # 用户显式保存了上下文（contextDecided）并显式应用：该模型的上下文
        # 冲突不再保留——发布控制台目标 65536 并登记基准 65536。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            state_doc = self._state_doc(settings={self.KEY: {
                "profile": {},
                "draft": {"alias": None, "contextSource": "manual",
                          "contextTokens": 65536, "defaultEffort": None},
                "draftVersion": 1, "draftSavedAt": "draft-time",
                "contextDecided": True}})
            result, store, state_file, _, _, _ = self._run(
                root, store_doc=self._store_doc(context=131072),
                config_doc=self._config_doc(context=131072),
                state_doc=state_doc, catalogs=self._catalogs(),
                selection={self.KEY: 1})
            self.assertTrue(any("更新 um-a" in line for line in result["applied"]))
            # 用户显式决定过控制台值：冲突条目如实记录为已裁定（resolution
            # =console），发布按控制台目标进行，绝不是「无冲突」。
            conflicts = result["contextConflicts"]
            self.assertEqual(len(conflicts), 1)
            self.assertTrue(conflicts[0]["decided"])
            self.assertEqual(conflicts[0]["resolution"], "console")
            self.assertEqual(self._store_context(store), 65536)
            state = json.loads(state_file.read_text(encoding="utf-8"))
            record = state["modelSettings"][self.KEY]
            self.assertEqual(record["applied"]["contextTokens"], 65536)
            self.assertEqual(
                state["managed"][self.UM.key]["um-a"]["published"]["context"],
                65536)

    def test_alias_only_draft_keeps_a_context_conflict_pending(self):
        # 只改别名/档位的草稿不是上下文决定：客户端值（999999）与已应用
        # 262144 冲突时不覆盖，冲突待裁定；草稿仍照常应用（alias）。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            state_doc = self._state_doc(
                managed={"um-a": {"profile": {}, "published": {
                    "context": 262144, "at": "published-time"}}},
                settings={self.KEY: {
                    "profile": {},
                    "draft": {"alias": "renamed", "contextSource": "manual",
                              "contextTokens": 262144, "defaultEffort": None},
                    "draftVersion": 1, "draftSavedAt": "draft-time",
                    "applied": {"alias": None, "contextSource": "manual",
                                "contextTokens": 262144, "defaultEffort": None},
                    "appliedVersion": 1, "appliedAt": "applied-time",
                    "contextDecided": False}})
            result, store, state_file, _, _, store_before = self._run(
                root, store_doc=self._store_doc(context=999999),
                config_doc=self._config_doc(context=999999),
                state_doc=state_doc, catalogs=self._catalogs(),
                selection={self.KEY: 1})
            # 规则库不携带显示名：别名不构成客户端写入；冲突的上下文保留。
            self.assertEqual(result["applied"], [])
            self.assertEqual(self._store_context(store), 999999)
            conflicts = result["contextConflicts"]
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0]["client"], 999999)
            self.assertEqual(conflicts[0]["target"], 262144)
            self.assertEqual(conflicts[0]["baseline"], 262144)
            self.assertTrue(conflicts[0]["baseline_recorded"])
            self.assertFalse(conflicts[0]["decided"])
            self.assertEqual(conflicts[0]["action"], "conflict")
            state = json.loads(state_file.read_text(encoding="utf-8"))
            record = state["modelSettings"][self.KEY]
            self.assertEqual(record["applied"]["alias"], "renamed")
            self.assertEqual(record["applied"]["contextTokens"], 262144)

    def test_adopting_the_client_value_publishes_nothing_and_keeps_it(self):
        # 显式采用客户端值（D4 的另一边）：草稿=客户端当前值 → 目标与客户端
        # 相等，无需写客户端、无冲突；客户端值保持原样。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            state_doc = self._state_doc(settings={self.KEY: {
                "profile": {},
                "draft": {"alias": None, "contextSource": "manual",
                          "contextTokens": 65536, "defaultEffort": None},
                "draftVersion": 1, "draftSavedAt": "draft-time",
                "contextDecided": True}})
            result, store, state_file, _, _, store_before = self._run(
                root, store_doc=self._store_doc(context=65536),
                config_doc=self._config_doc(context=65536),
                state_doc=state_doc, catalogs=self._catalogs(),
                selection={self.KEY: 1})
            self.assertEqual(result["contextConflicts"], [])
            self.assertEqual(store.read_bytes(), store_before)
            state = json.loads(state_file.read_text(encoding="utf-8"))
            record = state["modelSettings"][self.KEY]
            self.assertEqual(record["applied"]["contextTokens"], 65536)

    def test_cancel_before_publish_writes_nothing(self):
        # 取消路径：冲突轮发布前取消 → 零客户端写入、零状态提交；冲突仍可读。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)

            def cancel():
                raise RuntimeError("cancelled")

            with self.assertRaises(RuntimeError):
                self._run(root, store_doc=self._store_doc(context=131072),
                           config_doc=self._config_doc(context=131072),
                           state_doc=self._state_doc(),
                           catalogs=self._catalogs(),
                           before_publish=cancel)
            self.assertEqual(
                json.loads((root / "provider_config.json").read_text("utf-8")),
                self._store_doc(context=131072))
            self.assertEqual(
                json.loads((root / "state.json").read_text("utf-8")),
                self._state_doc())

    def test_corrupt_publish_baseline_is_not_a_guess(self):
        # 基准记录损坏（非字典/垃圾值）不当作「可覆盖」，也不让只读读面崩溃。
        for published in ("junk", {"context": "junk"}, {"context": -5},
                          {"context": True}):
            with self.subTest(published=published):
                with tempfile.TemporaryDirectory(
                        ignore_cleanup_errors=True) as raw:
                    root = Path(raw)
                    result, store, state_file, _, _, store_before = self._run(
                        root, store_doc=self._store_doc(context=131072),
                        config_doc=self._config_doc(context=131072),
                        state_doc=self._state_doc(managed={"um-a": {
                            "profile": {}, "published": published}}),
                        catalogs=self._catalogs())
                    self.assertEqual(result["applied"], [])
                    self.assertEqual(store.read_bytes(), store_before)
                    self.assertEqual(
                        result["contextConflicts"][0]["baseline"], None)

    def test_preview_lists_the_conflict_without_writing(self):
        # 预览与应用同一口径：冲突进预览计划（待裁定），预览零写入。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            result, store, _, config, config_before, store_before = self._run(
                root, store_doc=self._store_doc(context=131072),
                config_doc=self._config_doc(context=131072),
                state_doc=self._state_doc(), catalogs=self._catalogs(),
                apply=False)
            self.assertEqual(result["status"], "preview")
            self.assertEqual(store.read_bytes(), store_before)
            self.assertTrue(any("待裁定" in line for line in result["plan"]))
            self.assertTrue(any("客户端上下文冲突" in line
                               for line in result["plan"]))
            self.assertEqual(len(result["contextConflicts"]), 1)

    def test_read_face_reports_conflict_and_publish_baseline(self):
        # 只读读面：冲突与上次发布基准如实呈现，绝不把「已写入」混称
        # 「客户端已生效」，也绝不把「无基准」猜成可覆盖。
        catalogs = self._catalogs()
        clients = {
            binding.key: {"models": {"um-a": {
                "name": "um-a" + binding.suffix,
                "contextWindow": 131072}}}
            for binding in model_sync.ACTIVE_BINDINGS
            if binding.source == "um"}
        state = self._state_doc()
        row = model_sync.context_override_surface(state, catalogs, clients)[0]
        self.assertEqual(row["client_context"], 131072)
        self.assertEqual(row["context_action"], "conflict")
        self.assertTrue(row["context_conflict"])
        self.assertIsNone(row["published_context"])
        self.assertFalse(row["published_known"])
        zcode = next(item for item in row["client_readbacks"]
                     if item["consumer"] == "ZCode")
        self.assertIsNone(zcode["publishedContext"])
        self.assertFalse(zcode["publishedKnown"])
        # 基准记录在场且等于客户端值：读面判定可发布，冲突清除。
        managed = {self.UM.key: {"um-a": {
            "profile": {}, "published": {
                "context": 131072, "at": "published-time"}}}}
        row = model_sync.context_override_surface(
            self._state_doc(managed=managed["um" + "|ZCode|local:openwebui"]),
            catalogs, clients)[0]
        self.assertEqual(row["published_context"], 131072)
        self.assertTrue(row["published_known"])
        self.assertEqual(row["context_action"], "publish")
        self.assertFalse(row["context_conflict"])


class TestCombinedSyncRound(unittest.TestCase):
    """§11.3 组合窗口（2026-10-10）：草稿与目录差异在同一受控发布轮。

    同一 ``run_sync`` 的 settings_selection/preview_token 链：预览只读、
    应用一次事务；token 绑定完整输入与目标集合；定时/普通同步只消费已应用
    设置；同步始终不发模型请求。夹具全在临时目录内。
    """

    UM = Binding("um", "ZCode", "local:openwebui", "UM", "（UM）")
    KEY = "um|um-a"

    def _catalogs(self, context=131072):
        return {"um": [CatalogModel("um", "um-a", context=context)]}

    def _state_doc(self, *, draft=None, applied=None, decided=False):
        state = empty_state()
        state["initializedBindings"] = [self.UM.key]
        state["managed"] = {self.UM.key: {"um-a": {"profile": {}}}}
        record = {"draft": draft, "draftVersion": 1 if draft else 0,
                  "draftSavedAt": "draft-time" if draft else None,
                  "applied": applied,
                  "appliedVersion": 1 if applied else None,
                  "appliedAt": "applied-time" if applied else None,
                  "appliedEvidence": "applied" if applied else None,
                  # 记录级显式上下文决定标记（save_model_setting_draft 写入）。
                  "contextDecided": True if decided else None}
        state["modelSettings"] = {self.KEY: record}
        return state

    @staticmethod
    def _draft(alias="renamed", context=None, tokens=None):
        # 草稿恒为规范四键；contextDecided 是记录级标记（显式上下文决定），
        # 由 save_model_setting_draft 写入、读面不单列。
        return {"alias": alias, "contextSource": context,
                "contextTokens": tokens, "defaultEffort": None}

    def _run(self, root, *, state_doc, catalogs, scope=None, selection=None,
             token=None, apply=False, running=False):
        store = root / "provider_config.json"
        store.write_text(json.dumps(self._store_doc(), ensure_ascii=False),
                          encoding="utf-8")
        (root / "config.json").write_text(json.dumps({
            "provider": {"local:openwebui": {
                "name": "UM", "models": {}}}}, ensure_ascii=False),
            encoding="utf-8")
        # DSH 侧夹具：让 DSH 绑定参与，验证 token 绑定目标集合。
        (root / "settings.yaml").write_text(
            "llm-pi-ai:\n  providers:\n    openwebui:\n"
            "      displayName: UM\n      models:\n", encoding="utf-8")
        state_file = root / "state.json"
        state_file.write_text(json.dumps(state_doc, ensure_ascii=False),
                              encoding="utf-8")
        with mock.patch.object(model_sync, "ZCODE_PROVIDER_CONFIG", store), \
                mock.patch.object(model_sync, "ZCODE_JSON",
                                  root / "config.json"), \
                mock.patch.object(model_sync, "DSH_YAML",
                                  root / "settings.yaml"), \
                mock.patch.object(model_sync, "STATE_FILE", state_file), \
                mock.patch.object(model_sync, "RESULT_FILE",
                                  root / "result.json"), \
                mock.patch.object(model_sync, "PREVIEW_FILE",
                                  root / "preview.json"), \
                mock.patch.object(model_sync, "HISTORY_FILE",
                                  root / "history.jsonl"), \
                mock.patch.object(model_sync, "BACKUP_DIR", root / "backups"), \
                mock.patch.object(model_sync, "collect_catalogs",
                                  return_value=(catalogs, [])), \
                mock.patch.object(model_sync, "zcode_running",
                                  return_value=running), \
                mock.patch.object(model_sync, "zcode_hot_sync_admission",
                                  return_value=(False, "测试：未准入")), \
                mock.patch.object(model_sync, "_post_json",
                                  side_effect=AssertionError("paid request")):
            result = model_sync.run_sync(
                apply=apply, run_probes=False, confirm=False, scope=scope,
                settings_selection=selection, preview_token=token)
        return result, store, state_file

    @staticmethod
    def _store_doc(context=65536):
        return {"schemaVersion": 1, "config": {
            "providerOrder": ["local:openwebui"],
            "providerConfigRules": {"providerRules": [{
                "providerId": "local:openwebui",
                "providerName": "UM",
                "enabled": True,
                "config": {"group": "standard-personal",
                           "personalModelIds": ["um-a"],
                           "modelOrder": ["um-a"]},
            }]},
            "modelConfigRules": {"providerModelRules": [{
                "modelId": "um-a",
                "providerId": "local:openwebui",
                "config": {"properties": {"contextWindow": context}},
            }]},
        }}

    def test_combined_token_binds_the_target_set(self):
        # §11.3.5：token 绑定完整输入与目标集合——只改目标客户端
        # （consumers）也必须换 token，绝不只绑草稿版本；输入相同则 token 相同。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            state_doc = self._state_doc(draft=self._draft())
            selection = {self.KEY: 1}
            first, _, _ = self._run(
                root, state_doc=state_doc, catalogs=self._catalogs(),
                scope={"consumers": ["ZCode"]}, selection=selection)
            second, _, _ = self._run(
                root, state_doc=state_doc, catalogs=self._catalogs(),
                scope={"consumers": ["ZCode", "DSH"]}, selection=selection)
            again, _, _ = self._run(
                root, state_doc=state_doc, catalogs=self._catalogs(),
                scope={"consumers": ["ZCode"]}, selection=selection)
            self.assertEqual(first["preview_token"], again["preview_token"])
            self.assertNotEqual(first["preview_token"],
                                second["preview_token"])

    def test_combined_round_returns_readback_and_one_transaction(self):
        # §11.3.5：确认后同一受控发布轮同时消费草稿与目录差异，结果返回
        # 实际应用/延期/冲突/读回；草稿与目录变化只写一次事务。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            state_doc = self._state_doc(
                draft=self._draft(context="manual", tokens=131072),
                decided=True)
            preview, _, _ = self._run(
                root, state_doc=state_doc, catalogs=self._catalogs(),
                scope={"consumers": ["ZCode"]}, selection={self.KEY: 1})
            self.assertEqual([], preview["settingsApplied"])
            self.assertEqual([], preview["settingsDeferred"])
            # 预览逐项带草稿差异（规范四键，不含内部标记）。
            change = preview["changes"][0]
            self.assertEqual("renamed", change["draft"]["alias"])
            self.assertNotIn("contextDecided", change["draft"])
            with mock.patch.object(
                    model_sync, "apply_config_transaction",
                    wraps=model_sync.apply_config_transaction) as tx:
                applied, store, state_file = self._run(
                    root, state_doc=state_doc, catalogs=self._catalogs(),
                    scope={"consumers": ["ZCode"]}, selection={self.KEY: 1},
                    token=preview["preview_token"], apply=True)
            self.assertEqual(1, tx.call_count)
            self.assertEqual([self.KEY], applied["settingsApplied"])
            self.assertEqual([], applied["settingsDeferred"])
            # 用户显式决定过控制台值：冲突如实记录为已裁定（resolution=
            # console），绝不冒充「无冲突」。
            conflicts = applied["contextConflicts"]
            self.assertEqual(1, len(conflicts))
            self.assertTrue(conflicts[0]["decided"])
            self.assertEqual("console", conflicts[0]["resolution"])
            readback = applied["settingsReadback"]
            self.assertIsNotNone(readback)
            self.assertTrue(readback["filesVerified"])
            self.assertEqual(self.KEY, "%s|%s" % (
                readback["keys"][0]["source"],
                readback["keys"][0]["model_id"]))
            self.assertEqual("renamed",
                             readback["keys"][0]["settings_applied"]["alias"])
            # 规则库（客户端）上下文已按目录目标 131072 发布。
            doc = json.loads(store.read_text(encoding="utf-8"))
            rule = next(rule for rule in doc["config"]["modelConfigRules"]
                        ["providerModelRules"] if rule["modelId"] == "um-a")
            self.assertEqual(131072,
                             rule["config"]["properties"]["contextWindow"])
            # 草稿已转已应用、草稿清空。
            state = json.loads(state_file.read_text(encoding="utf-8"))
            record = state["modelSettings"][self.KEY]
            self.assertIsNone(record["draft"])
            self.assertEqual("renamed", record["applied"]["alias"])

    def test_scheduled_round_never_publishes_a_pending_draft(self):
        # §11.3.5：定时/普通同步只消费已应用设置——待同步草稿绝不发布；
        # 目录差异照常镜像（应用值 65536 == 客户端当前值，无上下文改写）。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            state_doc = self._state_doc(
                draft=self._draft(alias="renamed"),
                applied={"alias": None, "contextSource": "manual",
                          "contextTokens": 65536, "defaultEffort": None})
            result, store, state_file = self._run(
                root, state_doc=state_doc, catalogs=self._catalogs(),
                apply=True)
            self.assertNotIn("settingsApplied", result)
            self.assertNotIn("settingsDeferred", result)
            state = json.loads(state_file.read_text("utf-8"))
            record = state["modelSettings"][self.KEY]
            # 草稿原样保留、已应用值不变：待同步草稿没有被发布。
            self.assertEqual("renamed", record["draft"]["alias"])
            self.assertIsNone(record["applied"]["alias"])
            self.assertEqual(65536, record["applied"]["contextTokens"])

    def test_combined_deferral_keeps_draft_and_old_applied(self):
        # §11.3.3：延期保留待同步草稿与旧 applied，不提前清空或标已应用。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            state_doc = self._state_doc(draft=self._draft())
            preview, _, _ = self._run(
                root, state_doc=state_doc, catalogs=self._catalogs(),
                scope={"consumers": ["ZCode"]}, selection={self.KEY: 1})
            applied, store, state_file = self._run(
                root, state_doc=state_doc, catalogs=self._catalogs(),
                scope={"consumers": ["ZCode"]}, selection={self.KEY: 1},
                token=preview["preview_token"], apply=True, running=True)
            self.assertEqual([], applied["applied"])
            self.assertEqual([self.KEY], applied["settingsDeferred"])
            state = json.loads(state_file.read_text("utf-8"))
            record = state["modelSettings"][self.KEY]
            self.assertEqual("renamed", record["draft"]["alias"])
            self.assertIsNone(record["applied"])
            doc = json.loads(store.read_text(encoding="utf-8"))
            rule = next(rule for rule in doc["config"]["modelConfigRules"]
                        ["providerModelRules"] if rule["modelId"] == "um-a")
            self.assertEqual(65536,
                             rule["config"]["properties"]["contextWindow"])


    def test_stale_draft_is_reported_in_preview_and_cleaned_on_apply(self):
        # 草稿的模型已不在目录（来源本轮目录有效、里面没有它）：预览如实列出
        # 「清理草稿」并保持状态不变（预览不写）；应用时清理记录。绝不静默丢掉
        # 草稿，也绝不让一条草稿把整轮同步预览顶失败。
        stale = {"draft": {"contextSource": "manual", "contextTokens": 1},
                 "draftVersion": 10, "draftSavedAt": "2026-10-02T06:26:51+00:00",
                 "applied": None, "appliedVersion": None}

        def state_with_stale():
            state = self._state_doc()
            state["modelSettings"]["um|model-a"] = dict(stale)
            return state

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            result, _, state_file = self._run(
                root, state_doc=state_with_stale(), catalogs=self._catalogs(),
                apply=False)
            self.assertIn("清理草稿 um|model-a", "\n".join(result["plan"]))
            state = json.loads(state_file.read_text("utf-8"))
            self.assertIn("um|model-a", state["modelSettings"])
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            result, _, state_file = self._run(
                root, state_doc=state_with_stale(), catalogs=self._catalogs(),
                apply=True)
            state = json.loads(state_file.read_text("utf-8"))
            self.assertNotIn("um|model-a", state["modelSettings"])
            self.assertTrue(
                any("清理草稿 um|model-a" in line for line in result["applied"]),
                result["applied"])

    def test_out_of_scope_stale_draft_is_preserved(self):
        # §19.2：只同步 um-a 时，同来源但范围外的 um|model-a 草稿必须原样保留
        # （§11.3「范围外草稿保留」/ run_sync(scope.models) 只改选中模型）。
        state_doc = self._state_doc()
        state_doc["modelSettings"]["um|model-a"] = {
            "draft": {"contextSource": "manual", "contextTokens": 1},
            "draftVersion": 10, "draftSavedAt": "t", "applied": None}
        catalogs = {"um": [CatalogModel("um", "um-a", context=200000),
                           CatalogModel("um", "model-b", context=200000)]}
        scope = {"sources": ["um"], "consumers": ["ZCode"],
                 "models": {"um": ["um-a"]}}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            result, _, state_file = self._run(
                root, state_doc=state_doc, catalogs=catalogs, scope=scope,
                apply=False)
            self.assertNotIn("清理草稿",
                             "\n".join(result.get("plan", [])))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            result, _, state_file = self._run(
                root, state_doc=state_doc, catalogs=catalogs, scope=scope,
                apply=True)
            state = json.loads(state_file.read_text("utf-8"))
            self.assertIn("um|model-a", state["modelSettings"])
            self.assertNotIn("清理草稿",
                             "\n".join(result["applied"]))

    def test_in_scope_stale_draft_is_cleaned_when_explicitly_selected(self):
        # 显式把 model-a 纳入范围时：预览只读列出、应用清理（与范围契约一致）。
        state_doc = self._state_doc()
        state_doc["modelSettings"]["um|model-a"] = {
            "draft": {"contextSource": "manual", "contextTokens": 1},
            "draftVersion": 10, "draftSavedAt": "t", "applied": None}
        catalogs = {"um": [CatalogModel("um", "um-a", context=200000),
                            CatalogModel("um", "model-b", context=200000)]}
        scope = {"sources": ["um"], "consumers": ["ZCode"],
                 "models": {"um": ["um-a", "model-a"]}}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            result, _, state_file = self._run(
                root, state_doc=state_doc, catalogs=catalogs, scope=scope,
                apply=False)
            self.assertIn("清理草稿 um|model-a",
                          "\n".join(result["plan"]))
            self.assertIn("um|model-a",
                          json.loads(state_file.read_text("utf-8"))["modelSettings"])
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            result, _, state_file = self._run(
                root, state_doc=state_doc, catalogs=catalogs, scope=scope,
                apply=True)
            self.assertNotIn("um|model-a",
                             json.loads(state_file.read_text("utf-8"))["modelSettings"])
            self.assertTrue(any("清理草稿 um|model-a" in line
                               for line in result["applied"]), result["applied"])

    def test_draft_of_an_uncollected_source_is_never_touched(self):
        # 来源本轮未采集（人工停用 / 读取失败）：目录里没有该来源 = 不知道，
        # 绝不用一次目录读取失败去清用户的草稿。
        state_doc = self._state_doc()
        state_doc["modelSettings"]["um|model-a"] = {
            "draft": {"contextSource": "manual", "contextTokens": 1},
            "draftVersion": 3, "draftSavedAt": "t", "applied": None}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            result, _, state_file = self._run(
                root, state_doc=state_doc,
                catalogs={"qoder_cn": [CatalogModel("qoder_cn", "cn-a",
                                                     context=1000)]},
                apply=True)
            joined = "\n".join(result.get("plan", []) + result["applied"])
            self.assertNotIn("清理草稿", joined)
            state = json.loads(state_file.read_text("utf-8"))
            self.assertIn("um|model-a", state["modelSettings"])


class TestScheduledSourceScope(unittest.TestCase):
    """R1/D1（2026-10-11）：定时/手动全源同步的来源集合由调用方（脚本按人工
    停用意图剔除后）给出；空集合是「本轮无来源」，绝不当成「全部来源」，也不
    读取任何上游。本类用例只替身 run_sync 与私有临时锁/结果文件，绝不写真实
    客户端配置、绝不发真实请求。
    """

    def _main(self, argv, *, run_sync=None):
        captured = {}

        def fake(**kwargs):
            captured.update(kwargs)
            return {"status": "preview", "applied": [], "pending": []}

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            with mock.patch.object(sys, "argv", ["model_sync/sync.py"] + argv), \
                    mock.patch.object(model_sync, "DATA_DIR", root), \
                    mock.patch.object(model_sync, "LOCK_FILE",
                                      root / "sync.lock"), \
                    mock.patch.object(model_sync, "RESULT_FILE",
                                      root / "result.json"), \
                    mock.patch.object(model_sync, "PREVIEW_FILE",
                                      root / "preview.json"), \
                    mock.patch.object(model_sync, "run_sync",
                                      side_effect=run_sync or fake):
                code = model_sync.main()
        return code, captured

    def test_sources_argument_restricts_the_run_to_the_given_subset(self):
        code, captured = self._main(["--sources", "um,qoder_cn"])
        self.assertEqual(0, code)
        self.assertEqual({"sources": ["um", "qoder_cn"]},
                         captured.get("scope"))

    def test_empty_sources_skips_without_any_upstream_read(self):
        calls = []
        code, _ = self._main(["--sources", ""],
                             run_sync=lambda **kwargs: calls.append(kwargs))
        self.assertEqual(0, code)
        self.assertEqual([], calls)

    def test_absent_sources_argument_keeps_the_full_default(self):
        code, captured = self._main(["--apply"])
        self.assertEqual(0, code)
        self.assertIsNone(captured.get("scope"))
        self.assertTrue(captured.get("apply"))


if __name__ == "__main__":
    unittest.main()
