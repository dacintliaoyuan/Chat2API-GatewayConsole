# -*- coding: utf-8 -*-
import unittest

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_sync_core import (
    STATE_VERSION,
    Binding,
    CatalogModel,
    MIN_RETIREMENT_RECHECK_SECONDS,
    client_context_of,
    context_publish_action,
    empty_state,
    plan_catalog_mirror,
    validate_catalog,
)


UM_ZCODE = Binding("um", "ZCode", "local:openwebui", "UM", "（UM）")
CN_ZCODE = Binding("qoder_cn", "ZCode", "local:lingma-proxy",
                   "Qoder（国内）", "（qoder_CN）")
M365_DSH = Binding("m365", "DSH", "m365", "M365", "（M365）")


def consumer(binding, models, name=None):
    return {
        binding.key: {
            "providerName": name,
            "models": models,
        }
    }


def _utc_ago(seconds):
    return (datetime.now(timezone.utc) -
            timedelta(seconds=seconds)).isoformat()


class TestCatalogValidation(unittest.TestCase):
    def test_empty_state_uses_current_schema(self):
        # Rev8 B8-2: v3 — the pin mechanism (and its "pinned" key) is gone.
        self.assertEqual(empty_state()["version"], STATE_VERSION)
        self.assertNotIn("pinned", empty_state())

    def test_empty_state_carries_additive_queues(self):
        state = empty_state()
        self.assertEqual(state["retirementWatch"], {})
        self.assertEqual(state["contextProbes"], {})

    def test_plan_backfills_new_keys_into_legacy_state(self):
        # A v2 state file written before the retirementWatch/contextProbes
        # keys existed must be accepted (deepcopy + setdefault backfill).
        legacy = {"version": 2, "initializedBindings": [], "managed": {},
                  "catalogCounts": {}, "pending": [],
                  "probes": {}}
        plan = plan_catalog_mirror({}, {}, legacy, [])
        self.assertEqual(plan.next_state["retirementWatch"], {})
        self.assertEqual(plan.next_state["contextProbes"], {})

    def test_empty_catalog_is_not_authoritative(self):
        ok, warning = validate_catalog("um", [])
        self.assertFalse(ok)
        self.assertIn("为空", warning)

    def test_drop_over_half_is_rejected(self):
        models = [CatalogModel("um", "only")]
        ok, warning = validate_catalog("um", models, previous_count=3)
        self.assertFalse(ok)
        self.assertIn("超过 50%", warning)

    def test_exact_half_drop_is_allowed(self):
        models = [CatalogModel("um", "a"), CatalogModel("um", "b")]
        ok, _ = validate_catalog("um", models, previous_count=4)
        self.assertTrue(ok)


class TestCatalogMirror(unittest.TestCase):
    def test_first_run_adopts_and_updates_existing_model(self):
        catalogs = {"um": [CatalogModel("um", "A", context=128000)]}
        consumers = consumer(
            UM_ZCODE,
            {"A": {"name": "A", "limit": {"context": 64000}}},
            name="Open WebUI")
        plan = plan_catalog_mirror(catalogs, consumers, bindings=[UM_ZCODE])
        self.assertEqual(
            [change.kind for change in plan.changes],
            ["provider_name", "update"])
        self.assertIn("A", plan.next_state["managed"][UM_ZCODE.key])

    def test_new_model_is_published_and_managed(self):
        catalogs = {"um": [CatalogModel("um", "New", context=64000)]}
        consumers = consumer(UM_ZCODE, {}, name="UM")
        plan = plan_catalog_mirror(catalogs, consumers, bindings=[UM_ZCODE])
        self.assertEqual([change.kind for change in plan.changes], ["add"])
        self.assertIn("New", plan.next_state["managed"][UM_ZCODE.key])

    def test_single_absence_only_writes_watch_entry(self):
        # 2026-09-22 contract: a managed model missing from ONE valid catalog
        # snapshot enters the retirement watch (pending review) — no remove.
        state = empty_state()
        state["initializedBindings"] = [UM_ZCODE.key]
        state["managed"][UM_ZCODE.key] = {"Old": {"profile": {}}}
        catalogs = {"um": [CatalogModel("um", "Other")]}
        consumers = consumer(UM_ZCODE, {"Old": {"name": "Old（UM）"}}, name="UM")
        plan = plan_catalog_mirror(catalogs, consumers, state, [UM_ZCODE])
        self.assertFalse(any(change.kind == "remove" for change in plan.changes))
        watch = plan.next_state["retirementWatch"][UM_ZCODE.key + "|Old"]
        self.assertEqual(watch["evidenceRuns"], 1)
        self.assertIn("firstMissing", watch)
        self.assertIn("lastMissing", watch)
        # The managed entry stays untouched during the review period.
        self.assertIn("Old", plan.next_state["managed"][UM_ZCODE.key])

    def _catalogs(self, models=None):
        return {"um": models or [CatalogModel("um", "alive")]}

    def _consumers(self, models=None, rule_ids=None):
        consumer = {"providerName": "UM",
                    "models": models if models is not None
                    else {"alive": {"name": "alive（UM）"}}}
        if rule_ids is not None:
            consumer["ruleModelIds"] = rule_ids
        return {UM_ZCODE.key: consumer}

    def test_orphan_extra_in_a_managed_provider_is_retired(self):
        """契约变更（2026-09-26，维护者决定「下线直接移除」）。

        旧用例名 ``test_manual_extra_is_never_removed``：受管 provider 里不在
        受管登记表的条目被当手动附加项永久保留 —— 结果是 UM 目录早已下线的
        model-a 一直留在 provider_config.json 的白名单/排序/模型规则里。
        新决定：该来源本轮取得有效目录时，受管 provider 的整个模型面都以目录
        为准，孤儿在同一轮计划里退役；只有目录失败/为空/无效才一律不删。
        """
        state = empty_state()
        state["initializedBindings"] = [UM_ZCODE.key]
        catalogs = {"um": [CatalogModel("um", "SourceModel")]}
        consumers = consumer(UM_ZCODE, {"Manual": {"name": "Manual"}}, name="UM")
        plan = plan_catalog_mirror(catalogs, consumers, state, [UM_ZCODE])
        removals = [change for change in plan.changes
                    if change.kind == "remove"]
        self.assertEqual([change.model_id for change in removals], ["Manual"])
        self.assertIn("来源目录已不含该模型，直接退役", removals[0].description)
        self.assertIn("残留：主配置", removals[0].description)
        self.assertEqual(removals[0].previous_profile, {"name": "Manual"})
        # 孤儿退役不留观察项：同一轮就是最终决定。
        self.assertEqual(plan.next_state["retirementWatch"], {})
        # 受管条目（受管登记表里有 profile）的两次独立目录证据规则不变。
        self.assertIn("SourceModel", plan.next_state["managed"][UM_ZCODE.key])

    def test_orphan_only_in_the_rule_store_is_retired(self):
        # 规则库里的孤儿（主配置没有该条目）：直接退役，残留位置标注规则库。
        state = empty_state()
        state["initializedBindings"] = [UM_ZCODE.key]
        catalogs = {"um": [CatalogModel("um", "alive", context=128000)]}
        consumers = self._consumers({"alive": {"name": "alive"}},
                                   rule_ids=["alive", "model-a"])
        plan = plan_catalog_mirror(catalogs, consumers, state, [UM_ZCODE])
        removals = [change for change in plan.changes
                    if change.kind == "remove"]
        self.assertEqual([change.model_id for change in removals], ["model-a"])
        self.assertIn("残留：供应商规则库", removals[0].description)
        # 规则库条目没有主配置 profile：previous_profile 为 None，渲染层
        # 只清规则库，不伪造主配置条目。
        self.assertIsNone(removals[0].previous_profile)

    def test_orphan_retirement_is_idempotent_and_applies_immediately(self):
        # 第一次计划：孤儿退役并留墓碑（确认但未应用）；用 next_state 再计划
        # 一次仍是同一条退役变化，不会退化成新的观察项。
        state = empty_state()
        state["initializedBindings"] = [UM_ZCODE.key]
        catalogs = self._catalogs()
        consumers = self._consumers({"alive": {"name": "alive（UM）"},
                                    "model-a": {"name": "model-a"}})
        first = plan_catalog_mirror(catalogs, consumers, state, [UM_ZCODE])
        self.assertEqual([change.kind for change in first.changes], ["remove"])
        self.assertTrue(
            first.next_state["managed"][UM_ZCODE.key]["model-a"]["retired"])
        second = plan_catalog_mirror(
            catalogs, consumers, first.next_state, [UM_ZCODE])
        self.assertEqual([change.kind for change in second.changes], ["remove"])
        self.assertEqual(second.changes[0].model_id, "model-a")
        self.assertEqual(second.next_state["retirementWatch"], {})

    def test_absent_source_never_retires_an_orphan(self):
        # 该来源本轮没抓到目录（抓取失败/未选）：不产生任何退役。
        state = empty_state()
        state["initializedBindings"] = [UM_ZCODE.key]
        consumers = self._consumers({"model-a": {"name": "model-a"}})
        plan = plan_catalog_mirror({"m365": [CatalogModel("m365", "x")]},
                                  consumers, state, [UM_ZCODE])
        self.assertEqual(plan.changes, [])
        self.assertEqual(plan.next_state["managed"].get(UM_ZCODE.key, {}), {})
        self.assertEqual(plan.next_state["retirementWatch"], {})

    def test_empty_catalog_never_retires_an_orphan(self):
        # 目录为空 = 无效目录：不删、只报。
        state = empty_state()
        state["initializedBindings"] = [UM_ZCODE.key]
        consumers = self._consumers({"model-a": {"name": "model-a"}})
        plan = plan_catalog_mirror({"um": []}, consumers, state, [UM_ZCODE])
        self.assertEqual(plan.changes, [])
        self.assertTrue(plan.warnings)
        self.assertIn("为空", plan.warnings[0])
        self.assertEqual(plan.next_state["retirementWatch"], {})

    def test_duplicate_catalog_ids_never_retire_an_orphan(self):
        # 目录含重复 ID = 无效目录：不删、只报。
        state = empty_state()
        state["initializedBindings"] = [UM_ZCODE.key]
        consumers = self._consumers({"model-a": {"name": "model-a"}})
        plan = plan_catalog_mirror(
            {"um": [CatalogModel("um", "dup"), CatalogModel("um", "dup")]},
            consumers, state, [UM_ZCODE])
        self.assertEqual(plan.changes, [])
        self.assertIn("重复", plan.warnings[0])

    def test_drop_threshold_blocks_orphan_retirement(self):
        # 骤降阈值先拦：上一轮 16 项、本轮 1 项 → 目录无效，孤儿不删。
        state = empty_state()
        state["initializedBindings"] = [UM_ZCODE.key]
        state["catalogCounts"] = {"um": 16}
        consumers = self._consumers({"model-a": {"name": "model-a"}})
        plan = plan_catalog_mirror(
            {"um": [CatalogModel("um", "only")]}, consumers, state, [UM_ZCODE])
        self.assertEqual(plan.changes, [])
        self.assertIn("超过 50%", plan.warnings[0])
        self.assertEqual(plan.next_state["managed"].get(UM_ZCODE.key, {}), {})

    def test_unrelated_provider_entries_are_untouched(self):
        # 其他 provider / 未参与本轮计划的绑定：一条都不动。
        state = empty_state()
        state["initializedBindings"] = [UM_ZCODE.key, CN_ZCODE.key]
        state["managed"][CN_ZCODE.key] = {"manual-1": {"profile": {}}}
        consumers = {
            UM_ZCODE.key: {"providerName": "UM",
                            "models": {"alive": {"name": "alive（UM）"}}},
            CN_ZCODE.key: {"providerName": "手工 provider",
                            "models": {"manual-1": {"name": "manual-1"}}},
        }
        plan = plan_catalog_mirror(
            {"um": [CatalogModel("um", "alive")],
             "qoder_cn": [CatalogModel("qoder_cn", "cn-real")]},
            consumers, state, [UM_ZCODE])
        self.assertEqual(plan.changes, [])
        self.assertEqual(plan.next_state["managed"][CN_ZCODE.key],
                         {"manual-1": {"profile": {}}})

    def test_out_of_scope_orphan_is_untouched(self):
        # 写入面收窄时（models 过滤），未选中的孤儿不进本轮写入面。
        state = empty_state()
        state["initializedBindings"] = [UM_ZCODE.key]
        consumers = self._consumers({"model-a": {"name": "model-a"}})
        plan = plan_catalog_mirror(
            {"um": [CatalogModel("um", "alive")]}, consumers, state,
            [UM_ZCODE], model_filter={"um": {"alive"}})
        self.assertFalse(any(change.kind == "remove" for change in plan.changes))
        self.assertNotIn("model-a", plan.next_state["managed"][UM_ZCODE.key])

    def test_stale_managed_record_without_any_surface_is_dropped(self):
        # 受管记录在有效目录里缺失、且客户端两个面都不再持有它：记录本身
        # 按退役清理（无客户端变化、不产生观察项）。
        state = empty_state()
        state["initializedBindings"] = [UM_ZCODE.key]
        state["managed"][UM_ZCODE.key] = {"ghost": {"profile": {}}}
        plan = plan_catalog_mirror(
            {"um": [CatalogModel("um", "other")]},
            self._consumers({"other": {"name": "other（UM）"}}), state,
            [UM_ZCODE])
        self.assertEqual(plan.changes, [])
        self.assertNotIn("ghost", plan.next_state["managed"][UM_ZCODE.key])
        self.assertEqual(plan.next_state["retirementWatch"], {})

    def test_managed_model_not_removed_on_first_absence(self):
        """Rev8 B8-2：pinned 豁免已随机制移除——首次目录缺失一律进入退役观察。"""
        state = empty_state()
        state["initializedBindings"] = [UM_ZCODE.key]
        state["managed"][UM_ZCODE.key] = {"Old": {"profile": {}}}
        catalogs = {"um": [CatalogModel("um", "Other")]}
        consumers = consumer(UM_ZCODE, {"Old": {"name": "Old（UM）"}}, name="UM")
        plan = plan_catalog_mirror(catalogs, consumers, state, [UM_ZCODE])
        self.assertFalse(any(change.kind == "remove" for change in plan.changes))
        self.assertIn(UM_ZCODE.key + "|Old", plan.next_state["retirementWatch"])
        self.assertNotIn("pinned", plan.next_state)


class TestRetirementEvidence(unittest.TestCase):
    """Two independent valid-catalog runs must still miss the model."""

    KEY = UM_ZCODE.key + "|Old"

    def _state(self, watch=None):
        state = empty_state()
        state["initializedBindings"] = [UM_ZCODE.key]
        state["managed"][UM_ZCODE.key] = {"Old": {"profile": {}}}
        if watch is not None:
            state["retirementWatch"] = watch
        return state

    def _catalogs(self):
        return {"um": [CatalogModel("um", "Other")]}

    def _consumers(self):
        return consumer(UM_ZCODE, {"Old": {"name": "Old（UM）"}}, name="UM")

    def test_second_independent_evidence_confirms_removal(self):
        watch = {self.KEY: {"firstMissing": _utc_ago(7200),
                            "lastMissing": _utc_ago(60), "evidenceRuns": 1}}
        plan = plan_catalog_mirror(self._catalogs(), self._consumers(),
                                   self._state(watch), [UM_ZCODE])
        removals = [c for c in plan.changes if c.kind == "remove"]
        self.assertEqual(len(removals), 1)
        self.assertIn("第二次独立目录证据确认下线", removals[0].description)
        self.assertEqual(removals[0].previous_profile, {"name": "Old（UM）"})
        # Confirmed retirement tombstones the managed entry (the key survives
        # until run_sync actually applies the removal — a pending ZCode change
        # must not strand the config entry) and clears the watch entry.
        self.assertTrue(
            plan.next_state["managed"][UM_ZCODE.key]["Old"].get("retired"))
        self.assertNotIn(self.KEY, plan.next_state["retirementWatch"])

    def test_same_window_repeats_do_not_accumulate_evidence(self):
        watch = {self.KEY: {"firstMissing": _utc_ago(60),
                            "lastMissing": _utc_ago(30), "evidenceRuns": 1}}
        plan = plan_catalog_mirror(self._catalogs(), self._consumers(),
                                   self._state(watch), [UM_ZCODE])
        self.assertFalse(any(c.kind == "remove" for c in plan.changes))
        entry = plan.next_state["retirementWatch"][self.KEY]
        self.assertEqual(entry["evidenceRuns"], 1)
        # Only lastMissing was refreshed inside the same window.
        self.assertGreater(entry["lastMissing"], watch[self.KEY]["lastMissing"])

    def test_recheck_interval_is_the_evidence_gap(self):
        self.assertEqual(MIN_RETIREMENT_RECHECK_SECONDS, 1800)

    def test_second_evidence_confirms_retirement(self):
        """Rev8 B8-2：第二次独立证据确认退役，不再有 pin 引用清理描述。"""
        state = self._state({self.KEY: {"firstMissing": _utc_ago(7200),
                                        "lastMissing": _utc_ago(60),
                                        "evidenceRuns": 1}})
        plan = plan_catalog_mirror(self._catalogs(), self._consumers(),
                                   state, [UM_ZCODE])
        removals = [c for c in plan.changes if c.kind == "remove"]
        self.assertEqual(len(removals), 1)
        self.assertNotIn("固定引用", removals[0].description)
        self.assertNotIn("pinned", plan.next_state)
        # Tombstone survives at plan time; run_sync deletes it on apply.
        self.assertTrue(
            plan.next_state["managed"][UM_ZCODE.key]["Old"].get("retired"))

    def test_reappearance_clears_watch_without_changes(self):
        watch = {self.KEY: {"firstMissing": _utc_ago(7200),
                            "lastMissing": _utc_ago(60), "evidenceRuns": 1}}
        state = self._state(watch)
        catalogs = {"um": [CatalogModel("um", "Old", context=128000)]}
        consumers = consumer(UM_ZCODE, {}, name="UM")
        plan = plan_catalog_mirror(catalogs, consumers, state, [UM_ZCODE])
        self.assertFalse(any(c.kind == "remove" for c in plan.changes))
        self.assertNotIn(self.KEY, plan.next_state["retirementWatch"])
        # The comeback still plans the add against the retained profile.
        self.assertTrue(any(c.kind == "add" and c.model_id == "Old"
                            for c in plan.changes))

    def test_invalid_catalog_counts_no_evidence_and_touches_nothing(self):
        watch = {self.KEY: {"firstMissing": _utc_ago(7200),
                            "lastMissing": _utc_ago(60), "evidenceRuns": 1}}
        state = self._state(watch)
        plan = plan_catalog_mirror({"um": []}, self._consumers(), state,
                                   [UM_ZCODE])
        self.assertEqual(plan.changes, [])
        self.assertTrue(plan.warnings)
        self.assertEqual(plan.next_state["retirementWatch"][self.KEY],
                         watch[self.KEY])

    def test_absent_catalog_counts_no_evidence(self):
        # Source not collected at all (scope or outage): the watch entry must
        # stay exactly as it was (no evidence run, no refresh).
        watch = {self.KEY: {"firstMissing": _utc_ago(7200),
                            "lastMissing": _utc_ago(60), "evidenceRuns": 1}}
        state = self._state(watch)
        plan = plan_catalog_mirror({"m365": [CatalogModel("m365", "X")]},
                                   self._consumers(), state, [UM_ZCODE])
        self.assertEqual(plan.changes, [])
        self.assertEqual(plan.next_state["retirementWatch"][self.KEY],
                         watch[self.KEY])

    def test_reappearance_after_confirmation_produces_add(self):
        # Post-confirmation state: the managed key is gone.  The model returns
        # to the catalog — the mirror must plan a fresh add again.
        state = empty_state()
        state["initializedBindings"] = [UM_ZCODE.key]
        catalogs = {"um": [CatalogModel("um", "Old", context=128000)]}
        consumers = consumer(UM_ZCODE, {}, name="UM")
        plan = plan_catalog_mirror(catalogs, consumers, state, [UM_ZCODE])
        self.assertTrue(any(c.kind == "add" and c.model_id == "Old"
                            for c in plan.changes))


class TestAuthorityMarkedRetirement(unittest.TestCase):
    def test_unpublishable_model_retired(self):
        """Rev8 B8-2：来源标记不可发布的模型立即确认退役，无 pin 引用描述。"""
        state = empty_state()
        state["initializedBindings"] = [M365_DSH.key]
        state["managed"][M365_DSH.key] = {"gpt-image-2": {"profile": {}}}
        catalogs = {"m365": [CatalogModel("m365", "gpt-image-2",
                                          publish=False)]}
        consumers = consumer(
            M365_DSH, {"gpt-image-2": {"id": "gpt-image-2"}}, name="M365")
        plan = plan_catalog_mirror(catalogs, consumers, state, [M365_DSH])
        removals = [c for c in plan.changes if c.kind == "remove"]
        self.assertEqual(len(removals), 1)
        self.assertIn("来源标记为不可发布", removals[0].description)
        self.assertNotIn("固定引用", removals[0].description)
        self.assertNotIn("pinned", plan.next_state)
        # The managed entry keeps a tombstone until run_sync applies the
        # removal to the consumer config.
        self.assertTrue(
            plan.next_state["managed"][M365_DSH.key]["gpt-image-2"]
            .get("retired"))

    def test_non_chat_m365_model_is_retired_on_first_adoption(self):
        catalogs = {"m365": [CatalogModel(
            "m365", "gpt-image-2", publish=False)]}
        consumers = consumer(
            M365_DSH,
            {"gpt-image-2": {"id": "gpt-image-2"}}, name="M365")
        plan = plan_catalog_mirror(catalogs, consumers, bindings=[M365_DSH])
        self.assertTrue(any(change.kind == "remove" for change in plan.changes))


class TestModelScopeFilter(unittest.TestCase):
    def test_filter_limits_changes_and_provider_name(self):
        state = empty_state()
        state["initializedBindings"] = [UM_ZCODE.key]
        state["managed"][UM_ZCODE.key] = {
            "A": {"profile": {"name": "A（UM）"}},
            "B": {"profile": {"name": "B（UM）"}},
        }
        catalogs = {"um": [
            CatalogModel("um", "A", context=999999),   # update candidate
            CatalogModel("um", "B", context=888888),   # unselected update
            CatalogModel("um", "C", context=777777),   # unselected add
        ]}
        consumers = consumer(UM_ZCODE, {
            "A": {"name": "A（UM）"}, "B": {"name": "B（UM）"}},
            name="Wrong Name")  # would trigger provider_name unfiltered
        plan = plan_catalog_mirror(catalogs, consumers, state, [UM_ZCODE],
                                   model_filter={"um": {"A"}})
        kinds = [c.kind for c in plan.changes]
        self.assertEqual(kinds, ["update"])
        self.assertEqual(plan.changes[0].model_id, "A")

    def test_filter_blocks_retirement_confirmation(self):
        key = UM_ZCODE.key + "|Old"
        state = empty_state()
        state["initializedBindings"] = [UM_ZCODE.key]
        state["managed"][UM_ZCODE.key] = {"Old": {"profile": {}}}
        state["retirementWatch"] = {
            key: {"firstMissing": _utc_ago(7200), "lastMissing": _utc_ago(60),
                  "evidenceRuns": 1}}
        catalogs = {"um": [CatalogModel("um", "Other")]}
        consumers = consumer(UM_ZCODE, {"Old": {"name": "Old（UM）"}}, name="UM")
        plan = plan_catalog_mirror(catalogs, consumers, state, [UM_ZCODE],
                                   model_filter={"um": {"Other"}})
        self.assertFalse(any(c.kind == "remove" for c in plan.changes))
        # Out-of-scope models: frozen watch state, untouched entries.
        self.assertEqual(plan.next_state["retirementWatch"][key],
                         state["retirementWatch"][key])
        self.assertIn("Old", plan.next_state["managed"][UM_ZCODE.key])

    def test_unfiltered_source_still_plans_provider_name(self):
        catalogs = {"um": [CatalogModel("um", "A")]}
        consumers = consumer(UM_ZCODE, {}, name="Old UM")
        plan = plan_catalog_mirror(catalogs, consumers,
                                   model_filter={"m365": {"X"}})
        self.assertTrue(any(c.kind == "provider_name" for c in plan.changes))


class TestContextPublishDecision(unittest.TestCase):
    """R6/D4 三方比较（2026-10-10）：发布基准 / 客户端当前值 / 控制台目标值。

    决策是纯函数，镜像规划、渲染器与只读读面共用同一实现，三方不得各判一套。
    """

    def test_equal_client_value_needs_no_write(self):
        self.assertEqual(context_publish_action(131072, 131072, None), "equal")

    def test_client_absent_publishes_the_target(self):
        # 客户端没有值：没有可保护的人工值（添加字段不是覆盖人工值）。
        self.assertEqual(context_publish_action(131072, None, None), "publish")
        self.assertEqual(context_publish_action(None, None, None), "equal")

    def test_provably_ours_value_may_be_updated(self):
        self.assertEqual(
            context_publish_action(262144, 131072, {"context": 131072}),
            "publish")

    def test_manual_client_value_is_never_guessed_overwritable(self):
        # 客户端 65536 与目录 131072 不同、基准未知（历史状态）→ 冲突：
        # 绝不猜测客户端值可覆盖。
        self.assertEqual(context_publish_action(131072, 65536, None), "conflict")
        # 基准已记录但客户端值已被手工改动（不再等于基准）→ 同样冲突。
        self.assertEqual(
            context_publish_action(262144, 65536, {"context": 131072}),
            "conflict")
        # 基准记录的是「我们发布过缺席」，客户端现在的值不是我们写的 → 冲突。
        self.assertEqual(
            context_publish_action(262144, 65536, {"context": None}),
            "conflict")

    def test_no_declared_target_never_deletes_an_existing_client_value(self):
        # 目录无声明值（None）而客户端有值：绝不删除已有字段。
        self.assertEqual(context_publish_action(None, 65536, None), "conflict")
        self.assertEqual(
            context_publish_action(None, 65536, {"context": 65536}), "publish")

    def test_unreadable_client_snapshot_is_never_guessed(self):
        self.assertEqual(
            context_publish_action(131072, None, None, client_readable=False),
            "unreadable")

    def test_client_context_reads_context_window_or_limit_context(self):
        self.assertEqual(client_context_of({"contextWindow": 65536}), 65536)
        self.assertEqual(client_context_of({"limit": {"context": 65536}}), 65536)
        self.assertEqual(
            client_context_of({"contextWindow": None,
                               "limit": {"context": 65536}}), 65536)
        # 垃圾值不是证据：布尔/字符串/非正数一律读作未知（None）。
        for profile in (None, {}, {"contextWindow": True},
                        {"contextWindow": "65536"}, {"contextWindow": 0},
                        {"limit": {"context": -1}}, {"limit": 65536}):
            self.assertIsNone(client_context_of(profile))

    def test_ordinary_mirror_holds_a_conflicting_client_context(self):
        catalogs = {"um": [CatalogModel("um", "A", context=131072)]}
        consumers = consumer(
            UM_ZCODE, {"A": {"name": "A（UM）", "contextWindow": 65536}},
            name="UM")
        plan = plan_catalog_mirror(catalogs, consumers, bindings=[UM_ZCODE])
        # 客户端值与我们发布过的值无法证明一致：不规划上下文更新，冲突如实登记。
        self.assertEqual([change.kind for change in plan.changes], [])
        self.assertEqual(plan.context_holds, {UM_ZCODE.key: {"A"}})
        conflict = plan.context_conflicts[0]
        self.assertEqual(conflict["source"], "um")
        self.assertEqual(conflict["model_id"], "A")
        self.assertEqual(conflict["consumer"], "ZCode")
        self.assertEqual(conflict["target"], 131072)
        self.assertEqual(conflict["client"], 65536)
        self.assertIsNone(conflict["baseline"])
        self.assertFalse(conflict["baseline_recorded"])

    def test_published_baseline_lets_the_mirror_update(self):
        catalogs = {"um": [CatalogModel("um", "A", context=131072)]}
        consumers = consumer(
            UM_ZCODE, {"A": {"name": "A（UM）", "contextWindow": 65536}},
            name="UM")
        state = empty_state()
        state["initializedBindings"] = [UM_ZCODE.key]
        state["managed"][UM_ZCODE.key] = {
            "A": {"profile": {"contextWindow": 65536},
                  "published": {"context": 65536, "at": "published-time"}}}
        plan = plan_catalog_mirror(catalogs, consumers, state, [UM_ZCODE])
        self.assertEqual([change.kind for change in plan.changes], ["update"])
        self.assertFalse(plan.context_holds)
        self.assertEqual(plan.context_conflicts, [])

    def test_alias_update_preserves_a_conflicting_context(self):
        # 名称差异照常更新，但冲突的上下文绝不被覆盖：更新项携带
        # preserve_context，渲染器据此原样保留客户端值。
        catalogs = {"um": [CatalogModel("um", "A", context=131072)]}
        consumers = consumer(
            UM_ZCODE, {"A": {"name": "Old A（UM）", "contextWindow": 65536}},
            name="UM")
        plan = plan_catalog_mirror(catalogs, consumers, bindings=[UM_ZCODE])
        updates = [change for change in plan.changes if change.kind == "update"]
        self.assertEqual(len(updates), 1)
        self.assertTrue(updates[0].preserve_context)
        self.assertEqual(plan.context_holds, {UM_ZCODE.key: {"A"}})

    def test_explicitly_decided_context_releases_the_hold(self):
        # 用户显式保存了上下文（draftContextSet）后：该模型的上下文冲突不再
        # 抑制发布；未被显式决定的模型仍按冲突保留。
        catalogs = {"um": [CatalogModel("um", "A", context=131072),
                          CatalogModel("um", "B", context=131072)]}
        consumers = consumer(
            UM_ZCODE, {"A": {"name": "A（UM）", "contextWindow": 65536},
                       "B": {"name": "B（UM）", "contextWindow": 65536}},
            name="UM")
        plan = plan_catalog_mirror(catalogs, consumers, bindings=[UM_ZCODE],
                                  context_decided={"um|A"})
        updates = [change for change in plan.changes if change.kind == "update"]
        self.assertEqual([change.model_id for change in updates], ["A"])
        self.assertFalse(updates[0].preserve_context)
        self.assertEqual(plan.context_holds, {UM_ZCODE.key: {"B"}})
        self.assertEqual(
            [item["model_id"] for item in plan.context_conflicts], ["A", "B"])
        self.assertTrue(plan.context_conflicts[0]["decided"])
        self.assertFalse(plan.context_conflicts[1]["decided"])

    def test_unreadable_baseline_record_is_not_a_guess(self):
        # 基准记录损坏（非字典/垃圾值）时不得当作「可覆盖」。
        catalogs = {"um": [CatalogModel("um", "A", context=131072)]}
        consumers = consumer(
            UM_ZCODE, {"A": {"name": "A（UM）", "contextWindow": 65536}},
            name="UM")
        for published in ("junk", {"context": "junk"}, {"context": -5},
                          {"context": True}):
            state = empty_state()
            state["initializedBindings"] = [UM_ZCODE.key]
            state["managed"][UM_ZCODE.key] = {
                "A": {"profile": {}, "published": published}}
            plan = plan_catalog_mirror(catalogs, consumers, state, [UM_ZCODE])
            self.assertEqual(plan.context_holds, {UM_ZCODE.key: {"A"}})
            self.assertIsNone(plan.context_conflicts[0]["baseline"])


if __name__ == "__main__":
    unittest.main()
