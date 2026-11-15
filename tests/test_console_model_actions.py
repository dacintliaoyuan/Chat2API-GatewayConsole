# -*- coding: utf-8 -*-
"""Isolated tests for console_model_actions (Revision 5 L3/L1).

All model_sync state files are redirected into a temporary directory by
mock.patching the module constants (model_sync's injectable surface is exactly
those module constants). No real client config, gateway data directory, catalog
HTTP endpoint or Windows scheduled task is ever touched.

Ticket #32 retires the model sync/settings/task actions and the DSH/ZCode
consumer chain. These tests assert the retired surfaces stay gone while the kept
catalog, context probe and context override actions keep working.
"""

import contextlib
import json
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from console import adapters as console_adapters  # noqa: E402
from console import model_actions as console_model_actions  # noqa: E402
from model_sync import sync as model_sync  # noqa: E402
from model_sync import sync_core as model_sync_core  # noqa: E402
from model_sync.sync_core import CatalogModel  # noqa: E402
from console.operations import OperationCancelled, OperationError  # noqa: E402

PIN_KEY = "um|DSH|openwebui|model-a"


def _stub_catalogs(source="um", model_id="model-a", context=32768):
    return {source: [CatalogModel(source, model_id, context=context)]}


@contextlib.contextmanager
def temp_sync_env(state_text=None):
    """Point every model_sync path constant at an isolated temp layout."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        data = root / "data"
        data.mkdir(parents=True, exist_ok=True)
        paths = {
            "DATA_DIR": data,
            "STATE_FILE": data / "model-sync-state.json",
            "BACKUP_DIR": data / "model-sync-backups",
            "LOCK_FILE": data / "model-sync.lock",
        }
        if state_text is not None:
            paths["STATE_FILE"].write_text(state_text, encoding="utf-8")
        with contextlib.ExitStack() as stack:
            for name, value in paths.items():
                stack.enter_context(mock.patch.object(model_sync, name, value))
            yield paths


# Real write targets of the retained chain; snapshots assert byte+mtime identity.
_SNAPSHOT_TARGETS = ("STATE_FILE",)


def _snapshot(paths, names=_SNAPSHOT_TARGETS):
    snap = {}
    for name in names:
        path = paths[name]
        snap[name] = (path.read_bytes(), path.stat().st_mtime_ns)             if path.exists() else None
    return snap


def _assert_unchanged(test, paths, snap):
    for name, before in snap.items():
        path = paths[name]
        if before is None:
            test.assertFalse(path.exists(), "%s unexpectedly created" % name)
        else:
            test.assertEqual(
                (path.read_bytes(), path.stat().st_mtime_ns), before,
                "%s was modified" % name)


class _Ctx:
    """Minimal OperationContext stand-in matching the task-layer contract."""

    def __init__(self, cancelled=False):
        self._event = threading.Event()
        if cancelled:
            self._event.set()
        # The run handler derives its probe cancel hook from this attribute
        # (mirrors OperationContext's public cancel_event).
        self.cancel_event = self._event

    def raise_if_cancelled(self):
        if self._event.is_set():
            raise OperationCancelled()

    def report_step(self, note):
        pass


class ModelCatalogTests(unittest.TestCase):
    """model.catalog is a pure read over collect_catalogs (stubbed here)."""

    def test_serializes_known_catalogs_and_passes_warnings(self):
        catalogs = {
            "um": [CatalogModel("um", "model-a", context=32768)],
            "m365": [CatalogModel(
                "m365", "gpt-x", context=128000, efforts=("high", "low"),
                default_effort="high", publish=False,
                display_name="GPT X")],
        }
        warnings = ["Qoder国内版目录不可用：OSError"]
        with temp_sync_env() as paths:
            before = _snapshot(paths)
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(catalogs, warnings)) as collect, \
                    mock.patch.object(model_sync, "SyncLock") as sync_lock:
                result = console_model_actions.model_catalog("", {}, _Ctx())
            _assert_unchanged(self, paths, before)
        # R7：不带 sources 时显式传 None = 全部模型聚合入口（与旧语义一致）。
        collect.assert_called_once_with(sources=None)
        sync_lock.assert_not_called()  # pure read: no SyncLock, ever
        self.assertEqual(list(model_sync.ACTIVE_SOURCES), result["sources"])
        self.assertEqual(
            [{"id": "model-a", "display_name": None, "context": 32768,
              "context_state": "declared",
              "output_token_cap": {"value": 16384,
                                   "source": "compat_default"},
              "efforts": [], "default_effort": None, "publish": True,
              "efforts_source": None}],
            result["catalogs"]["um"])
        self.assertEqual(
            [{"id": "gpt-x", "display_name": "GPT X", "context": 128000,
              "context_state": "declared",
              "output_token_cap": {"value": 16384,
                                   "source": "compat_default"},
              "efforts": ["high", "low"], "default_effort": "high",
              "publish": False, "efforts_source": None}],
            result["catalogs"]["m365"])
        self.assertEqual(["Qoder国内版目录不可用：OSError"], result["warnings"])
        self.assertIsInstance(result["checked_epoch"], int)

    def test_explicit_sources_scope_reads_only_that_source(self):
        # R7：显式 sources 只读该来源——单来源目录读取不请求其他来源。
        catalogs = {"um": [CatalogModel("um", "model-a", context=32768)]}
        with temp_sync_env():
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(catalogs, [])) as collect:
                result = console_model_actions.model_catalog(
                    "", {"sources": json.dumps(["um"])}, _Ctx())
        collect.assert_called_once_with(sources=["um"])
        self.assertEqual(["um"], result["sources"])
        self.assertEqual(["model-a"],
                         [row["id"] for row in result["catalogs"]["um"]])

    def test_unknown_or_paused_sources_are_rejected_before_reading(self):
        # 未知/已暂停来源、重复、空列表与非法形态一律 invalid_params，
        # 绝不静默退回四源，也绝不先发一次读取再拒绝。
        for bad in (["m365"], ["nope"], ["um", "um"], [], "not-json",
                    [1], {"um": 1}):
            with self.subTest(sources=bad), temp_sync_env():
                with mock.patch.object(model_sync, "collect_catalogs",
                                       return_value=({}, [])) as collect:
                    with self.assertRaises(OperationError) as caught:
                        console_model_actions.model_catalog(
                            "", {"sources": bad}, _Ctx())
                self.assertEqual("invalid_params", caught.exception.code)
                collect.assert_not_called()

    def test_unknown_params_are_rejected(self):
        with temp_sync_env():
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=({}, [])) as collect:
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_catalog(
                        "", {"source": "um"}, _Ctx())
        self.assertEqual("invalid_params", caught.exception.code)
        collect.assert_not_called()

    def test_efforts_source_rides_the_catalog_payload(self):
        """P1 (2026-09-24): the console must be able to tell WHERE a
        model's tiers came from — the UM directory and the qoder_cn
        fallback read the local KNOWN_MODELS table, so a payload that
        labels everything "upstream-declared" is a false claim."""
        catalogs = {
            "um": [CatalogModel("um", "known-row", efforts=("low",),
                                 efforts_source="known")],
            "qoder_global": [CatalogModel(
                "qoder_global", "upstream-row", efforts=("high",),
                efforts_source="upstream")],
            "cliproxy": [CatalogModel("cliproxy", "probe-row",
                                      efforts=("medium",),
                                      efforts_source="probe")],
        }
        with temp_sync_env():
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(catalogs, [])):
                result = console_model_actions.model_catalog("", {}, _Ctx())
        self.assertEqual(
            "known", result["catalogs"]["um"][0]["efforts_source"])
        self.assertEqual(
            "upstream", result["catalogs"]["qoder_global"][0]["efforts_source"])
        self.assertEqual(
            "probe", result["catalogs"]["cliproxy"][0]["efforts_source"])

    def test_collect_failure_maps_to_catalog_failed(self):
        with temp_sync_env():
            with mock.patch.object(model_sync, "collect_catalogs",
                                   side_effect=RuntimeError("boom\nline2")):
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_catalog("", {}, _Ctx())
        self.assertEqual("catalog_failed", caught.exception.code)
        self.assertTrue(caught.exception.message.startswith("模型目录查询失败"))
        self.assertNotIn("\n", caught.exception.message)
        self.assertIn("RuntimeError", caught.exception.message)

    def test_cancelled_ctx_refuses_before_collect(self):
        with mock.patch.object(model_sync, "collect_catalogs") as collect:
            with self.assertRaises(OperationCancelled):
                console_model_actions.model_catalog(
                    "", {}, _Ctx(cancelled=True))
        collect.assert_not_called()


class PinRemovedContractTests(unittest.TestCase):
    """Rev8 契约 1（2026-10-02）：固定（pin）机制整体移除后的诚实边界。

    旧 ``PinActionTests``（pin 增删往返/排序/暂停来源拒绝/非法键/锁忙）随机制
    一并删除。这里固定移除后的契约：注册表不再提供 model.pin_add /
    model.pin_remove，模块不再有可写 pin 的处理器或 update_pin；旧 v2 状态
    文件经 load_state 幂等升级到 v3（pinned 丢弃、其余字段保留、失败不丢
    唯一状态）；CLI --pin/--unpin 参数随 argparse 一起消失。
    """

    def test_registry_no_longer_registers_pin_actions(self):
        from console import adapters as console_adapters
        names = {spec.name for spec in console_adapters.build_registry()}
        self.assertNotIn("model.pin_add", names)
        self.assertNotIn("model.pin_remove", names)

    def test_no_pin_write_path_remains(self):
        # 诚实拒绝而非旁路：模块上不再存在任何能写 pin 的入口。
        self.assertFalse(hasattr(console_model_actions, "model_pin_add"))
        self.assertFalse(hasattr(console_model_actions, "model_pin_remove"))
        self.assertFalse(hasattr(console_model_actions, "_pin_update"))
        self.assertFalse(hasattr(model_sync, "update_pin"))
        with temp_sync_env() as paths:
            state = json.dumps(
                dict(model_sync.empty_state(), version=2, pinned=[PIN_KEY]),
                ensure_ascii=False)
            paths["STATE_FILE"].write_text(state, encoding="utf-8")
            migrated = model_sync.load_state()
            self.assertEqual(model_sync_core.STATE_VERSION,
                             migrated["version"])
            self.assertNotIn("pinned", migrated)
            # 幂等：再次迁移不再变化。
            self.assertEqual(migrated, model_sync.migrate_state(migrated))

    def test_migration_keeps_everything_except_pinned(self):
        # 失败不丢唯一状态：v2 文档迁移只丢 pinned，managed/probes/
        # contextOverrides 等原样保留；迁移只发生在内存副本上，磁盘文件
        # 保持原样直到下一次事务写。
        legacy = {
            "version": 2,
            "initializedBindings": ["um|DSH|openwebui"],
            "managed": {"um|DSH|openwebui": {"model-a": {"profile": {}}}},
            "pinned": [PIN_KEY],
            "catalogCounts": {"um": 7},
            "pending": ["ZCode 发布 model-b"],
            "probes": {"um|model-a": {"status": "ok"}},
            "retirementWatch": {},
            "contextProbes": {},
            "contextOverrides": {"um|model-a": {
                "context": 200000, "set_at": "t", "source": "manual"}},
        }
        with temp_sync_env(state_text=json.dumps(legacy, ensure_ascii=False)) as paths:
            migrated = model_sync.load_state()
            self.assertEqual(model_sync_core.STATE_VERSION,
                             migrated["version"])
            self.assertNotIn("pinned", migrated)
            for key, value in legacy.items():
                if key in ("pinned", "version"):
                    continue
                self.assertEqual(value, migrated[key], key)
            # 迁移只发生在内存副本上：磁盘文件保持 v2 原样（直到下一次事务写）。
            self.assertEqual(
                json.dumps(legacy, ensure_ascii=False),
                paths["STATE_FILE"].read_text(encoding="utf-8"))

    def test_migration_is_idempotent_and_handles_v1(self):
        v1 = {"version": 1, "pending": [], "pinned": [PIN_KEY]}
        migrated = model_sync.migrate_state(dict(v1))
        self.assertEqual(model_sync_core.STATE_VERSION, migrated["version"])
        self.assertNotIn("pinned", migrated)
        self.assertEqual({}, migrated["probes"])
        self.assertEqual(migrated, model_sync.migrate_state(dict(migrated)))

    def test_cli_pin_arguments_are_gone(self):
        # The sync CLI itself is retired with the DSH/ZCode consumer chain:
        # no main()/argparse entry remains to accept --pin or --unpin.
        self.assertFalse(hasattr(model_sync, "main"))
        source = Path(model_sync.__file__).read_text(encoding="utf-8")
        self.assertNotIn("--pin", source)
        self.assertNotIn("--unpin", source)


class ContextProbeActionTests(unittest.TestCase):
    """model.context_probe_preview (pure read) / run (state write; enabled
    since the 2026-09-22 maintainer budget approval — the handler is
    exercised here offline with injected transports)."""

    def _probe_state(self):
        state = model_sync.empty_state()
        state["contextProbes"] = {
            "um|fresh": {"source": "um", "model_id": "fresh",
                         "declared_context": 128000, "declared_at": "t",
                         "status": "pending", "observed_floor": None,
                         "attempts": 0, "last_run": None, "last_error": None},
            "um|stale": {"source": "um", "model_id": "stale",
                         "declared_context": 64000, "declared_at": "t",
                         "status": "stale", "observed_floor": 16000,
                         "attempts": 2, "last_run": None, "last_error": None},
            "um|done": {"source": "um", "model_id": "done",
                        "declared_context": 64000, "declared_at": "t",
                        "status": "verified", "observed_floor": 256000,
                        "attempts": 4, "last_run": None, "last_error": None},
        }
        return json.dumps(state, ensure_ascii=False)

    def test_preview_reports_queue_estimates_without_side_effects(self):
        with temp_sync_env(state_text=self._probe_state()) as paths:
            before = _snapshot(paths)
            result = console_model_actions.model_context_probe_preview(
                "", {}, _Ctx())
            _assert_unchanged(self, paths, before)
        entries = {e["model_id"]: e for e in result["records"]}
        self.assertEqual(entries["fresh"]["estimated_requests"], 4)
        self.assertEqual(entries["fresh"]["input_chars_estimate"],
                         sum(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(entries["fresh"]["output_token_cap"],
                         model_sync.CONTEXT_PROBE_MAX_TOKENS)
        # Step-3 estimate rule (mirrors the runner): a stale record's old
        # floor belongs to an archived generation — it is re-planned over the
        # FULL ladder, not resumed from the old floor.
        self.assertEqual(entries["stale"]["estimated_requests"],
                         len(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(entries["stale"]["input_chars_estimate"],
                         sum(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(entries["stale"]["status"], "stale")
        self.assertEqual(entries["done"]["estimated_requests"], 0)
        self.assertEqual(result["steps"], list(model_sync.PROBE_INPUT_STEPS))
        self.assertIn("未定价", result["cost_note"])
        self.assertIn("max_requests", result["budget_note"])
        self.assertIn("不发请求", result["note"])

    def test_preview_zero_estimate_for_endpoint_less_sources(self):
        # F2: 永远零请求的记录（m365 已暂停且无探测端点）不计为全额请求：
        # estimated_requests/input_chars_estimate 为 0，probe_endpoint/paused
        # 如实标注，estimate_note 说明实际零请求；totals 让读面自己核总额
        # （runner 对无端点来源零请求跳过，估算与 runner 行为一致）。
        state = model_sync.empty_state()
        state["contextProbes"] = {
            "m365|paused-a": {"source": "m365", "model_id": "paused-a",
                              "declared_context": 32000, "declared_at": "t",
                              "status": "pending", "observed_floor": None,
                              "attempts": 0, "last_run": None,
                              "last_error": None},
            "m365|paused-b": {"source": "m365", "model_id": "paused-b",
                              "declared_context": 32000, "declared_at": "t",
                              "status": "stale", "observed_floor": 16000,
                              "attempts": 2, "last_run": None,
                              "last_error": None},
            "um|live": {"source": "um", "model_id": "live",
                        "declared_context": 128000, "declared_at": "t",
                        "status": "pending", "observed_floor": None,
                        "attempts": 0, "last_run": None, "last_error": None},
        }
        with temp_sync_env(state_text=json.dumps(
                state, ensure_ascii=False)) as paths:
            result = console_model_actions.model_context_probe_preview(
                "", {}, _Ctx())
        entries = {e["model_id"]: e for e in result["records"]}
        for model_id in ("paused-a", "paused-b"):
            entry = entries[model_id]
            self.assertEqual(entry["estimated_requests"], 0)
            self.assertEqual(entry["input_chars_estimate"], 0)
            self.assertFalse(entry["probe_endpoint"])
            self.assertTrue(entry["paused"])
            self.assertEqual(entry["estimate_note"], "该来源已暂停，实际零请求")
        live = entries["live"]
        self.assertEqual(live["estimated_requests"],
                         len(model_sync.PROBE_INPUT_STEPS))
        self.assertTrue(live["probe_endpoint"])
        self.assertFalse(live["paused"])
        self.assertIsNone(live["estimate_note"])
        self.assertTrue(live["executable"])
        for model_id in ("paused-a", "paused-b"):
            self.assertFalse(entries[model_id]["executable"])
        totals = result["totals"]
        self.assertEqual(totals["estimated_requests"],
                         len(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(totals["estimated_input_chars"],
                         sum(model_sync.PROBE_INPUT_STEPS))
        # 条 5 新口径：无端点+active → blocked；本组合无 verified/failed
        # 记录，idle 为 0；三分之和 = records 条数（旧值 runnable=1/
        # blocked=2 在新定义下不变，改写为补充三分口径断言）。
        self.assertEqual(totals["runnable_records"], 1)
        self.assertEqual(totals["blocked_records"], 2)
        self.assertEqual(totals["idle_records"], 0)
        self.assertEqual(totals["runnable_records"] +
                         totals["blocked_records"] +
                         totals["idle_records"], 3)
        self.assertIn("零请求跳过", result["budget_note"])

    def test_preview_without_state_is_legal_empty(self):
        with temp_sync_env() as paths:
            before = _snapshot(paths)
            result = console_model_actions.model_context_probe_preview(
                "", {}, _Ctx())
            _assert_unchanged(self, paths, before)
        self.assertEqual(result["records"], [])

    def test_preview_surfaces_evidence_contract_fields(self):
        # Plan-Revision 7 step 2: the read face passes the evidence contract
        # through verbatim — units, declaration/attempt/success times, last
        # error, evidence revision and stale reason — without inventing or
        # transforming values.
        state = model_sync.empty_state()
        state["contextProbes"] = {
            "um|rich": {"source": "um", "model_id": "rich",
                        "declared_context": 128000,
                        "declared_context_known": True,
                        "declared_note": None,
                        "declared_at": "2026-09-30T08:00:00+00:00",
                        "status": "stale", "observed_floor": 16000,
                        "observed_ceiling": 256000, "attempts": 5,
                        "last_run": "2026-09-30T09:00:00+00:00",
                        "last_success": "2026-09-30T08:30:00+00:00",
                        "last_error": "HTTPError",
                        "evidence_revision": 3,
                        "stale_reason": "declared_context_changed",
                        "token_evidence": 61234},
        }
        with temp_sync_env(state_text=json.dumps(
                state, ensure_ascii=False)) as paths:
            before = _snapshot(paths)
            result = console_model_actions.model_context_probe_preview(
                "", {}, _Ctx())
            _assert_unchanged(self, paths, before)
        self.assertEqual(len(result["records"]), 1)
        entry = result["records"][0]
        self.assertEqual(entry["declared_at"], "2026-09-30T08:00:00+00:00")
        self.assertEqual(entry["last_run"], "2026-09-30T09:00:00+00:00")
        self.assertEqual(entry["last_success"], "2026-09-30T08:30:00+00:00")
        self.assertEqual(entry["last_error"], "HTTPError")
        self.assertEqual(entry["evidence_revision"], 3)
        self.assertEqual(entry["stale_reason"], "declared_context_changed")
        self.assertEqual(entry["token_evidence"], 61234)
        # Step-4 read face: the declaration-unknown flag and its note pass
        # through verbatim (True/None here for a declared model).
        self.assertIs(entry["declared_context_known"], True)
        self.assertIsNone(entry["declared_note"])
        self.assertEqual(entry["observed_floor"], 16000)
        self.assertEqual(entry["observed_ceiling"], 256000)
        # Units are part of the contract: the ladder evidence is characters,
        # the output cap is tokens.
        self.assertEqual(entry["input_unit"], "chars")
        self.assertEqual(entry["output_unit"], "tokens")

    def test_preview_old_state_missing_new_fields_reads_as_none(self):
        # Additive evolution: an old-state record (original fields only) and
        # a record without declared_context both read fine — missing fields
        # surface as None, nothing is interpreted as measured or zero
        # capacity, and a missing declaration shows as unknown.
        state = model_sync.empty_state()
        state["contextProbes"] = {
            "um|old": {"source": "um", "model_id": "old",
                       "declared_context": 64000, "declared_at": "t",
                       "status": "verified", "observed_floor": 256000,
                       "attempts": 4, "last_run": "t", "last_error": None},
            "um|nodecl": {"source": "um", "model_id": "nodecl",
                          "status": "pending", "attempts": 0},
        }
        with temp_sync_env(state_text=json.dumps(
                state, ensure_ascii=False)) as paths:
            result = console_model_actions.model_context_probe_preview(
                "", {}, _Ctx())
        entries = {e["model_id"]: e for e in result["records"]}
        self.assertEqual(set(entries), {"old", "nodecl"})
        # Original fields pass through verbatim; the NEW contract fields are
        # absent in old state and must read as None (additive evolution).
        self.assertEqual(entries["old"]["declared_at"], "t")
        self.assertEqual(entries["old"]["last_run"], "t")
        for field in ("last_success", "last_error", "evidence_revision",
                      "stale_reason", "token_evidence"):
            self.assertIsNone(entries["old"][field])
        # F4 存量记录归一：缺 declared_context_known 按 declared_context
        # 派生显示（64000 → True）；scheme_revision 缺失归一为 1（缺字段=
        # 引入修订号前的祖传记录，按修订 1 解释）；evidence_revision 缺失
        # 保持 None（None=引入修订号前的祖传证据）。
        self.assertIs(entries["old"]["declared_context_known"], True)
        self.assertEqual(entries["old"]["scheme_revision"], 1)
        self.assertIsNone(entries["old"]["evidence_revision"])
        # F7: history_count 缺省 0（缺 history 字段的祖传记录）。
        self.assertEqual(entries["old"]["history_count"], 0)
        self.assertEqual(entries["nodecl"]["history_count"], 0)
        self.assertEqual(entries["nodecl"]["scheme_revision"], 1)
        # Old floors are historical CHARACTER-ladder evidence (chars, not
        # token capacity); the unit fields state that on every record.
        self.assertEqual(entries["old"]["observed_floor"], 256000)
        self.assertEqual(entries["old"]["input_unit"], "chars")
        self.assertEqual(entries["old"]["output_unit"], "tokens")
        # A model without a declared context still shows (declared unknown;
        # a later seeding round will fill it in) — not dropped, not zero.
        self.assertIsNone(entries["nodecl"]["declared_context"])
        self.assertIsNone(entries["nodecl"]["token_evidence"])
        self.assertIs(entries["nodecl"]["declared_context_known"], False)
        self.assertEqual(entries["nodecl"]["status"], "pending")

    def test_preview_keeps_records_canonical_without_queue_alias(self):
        # Interface-mismatch registration (B7-1): ``records`` stays the
        # backend canonical field and ``output_token_cap`` the unified name;
        # the frontend reading ``queue``/``output_tokens_cap`` is left to the
        # later frontend round — the backend neither renames nor fakes alias
        # fields to accommodate the current page.
        with temp_sync_env(state_text=self._probe_state()) as paths:
            result = console_model_actions.model_context_probe_preview(
                "", {}, _Ctx())
        self.assertIn("records", result)
        self.assertNotIn("queue", result)
        self.assertEqual(result["steps"], list(model_sync.PROBE_INPUT_STEPS))
        for entry in result["records"]:
            self.assertIn("output_token_cap", entry)
            self.assertNotIn("output_tokens_cap", entry)
        # The unit/contract note states the token-evidence boundary (F5:
        # 下界证据、0 是合法值、判断未知须用 is None), the no-char→token
        # conversion rule and the declaration-unknown rule explicitly
        # (step 4: unknown declaration is still measurable).  F4 的两条
        # 祖传归一规则也写在 note 里。
        self.assertIn("Token 容量未知", result["unit_note"])
        self.assertIn("下界证据", result["unit_note"])
        self.assertIn("0 是合法值", result["unit_note"])
        self.assertIn("判断未知须用 is None", result["unit_note"])
        self.assertIn("不做字符→token 换算", result["unit_note"])
        self.assertIn("缺字段=引入修订号前的祖传记录，按修订 1 解释",
                      result["unit_note"])
        self.assertIn("None=引入修订号前的祖传证据", result["unit_note"])
        self.assertIn("记录缺席", result["unit_note"])
        self.assertIn("声明未知", result["unit_note"])
        self.assertIn("探测仍可测量实际可用长度", result["unit_note"])

    # ------------------------------------------------------------------
    # 预览收尾（维护者处置表 2026-10-01，5 条 low 的必须/应当修）：显式
    # 选择、凭据只读检查、身份兜底、垃圾值归一与 totals 三分口径。预览
    # 与 run 复用同一套选择校验与 runner 口径，估算=run 的真实请求集合。
    # ------------------------------------------------------------------

    def test_preview_selection_limits_estimates_to_selected_records(self):
        # 条 1：sources/models 显式选择把估算限定到被选记录——未选中记录
        # estimated_requests/input_chars_estimate 为 0、不进 totals。
        with temp_sync_env(state_text=self._probe_state()) as paths:
            before = _snapshot(paths)
            result = console_model_actions.model_context_probe_preview(
                "", {"models": {"um": ["fresh"]}}, _Ctx())
            _assert_unchanged(self, paths, before)
        entries = {e["model_id"]: e for e in result["records"]}
        self.assertEqual(entries["fresh"]["estimated_requests"],
                         len(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(entries["fresh"]["input_chars_estimate"],
                         sum(model_sync.PROBE_INPUT_STEPS))
        self.assertIsNone(entries["fresh"]["estimate_note"])
        for model_id in ("stale", "done"):
            entry = entries[model_id]
            self.assertEqual(entry["estimated_requests"], 0)
            self.assertEqual(entry["input_chars_estimate"], 0)
            self.assertEqual(entry["estimate_note"],
                             "本轮未选中（不在请求集合），实际零请求")
        totals = result["totals"]
        self.assertEqual(totals["estimated_requests"],
                         len(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(totals["estimated_input_chars"],
                         sum(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(totals["runnable_records"], 1)
        self.assertEqual(totals["blocked_records"], 0)
        self.assertEqual(totals["idle_records"], 2)
        self.assertEqual(totals["runnable_records"] +
                         totals["blocked_records"] +
                         totals["idle_records"], 3)

    def test_preview_retest_estimate_matches_real_run_requests(self):
        # 条 1：retest=True 时选中范围内 verified/failed 记录按「先归档旧
        # 代次、再按全梯重测」计（估算=全梯步进数）；估算与 run 的真实
        # 请求数一致——离线 fake post 闭环核对 run 实际发送的请求数。
        def fake_post(url, body, timeout=90, token=None):
            content = body["messages"][0]["content"]
            marker = re.search(r"\[ctxprobe (run\d+)\] FINAL anchor",
                               content).group(1)
            return {"status": 200, "body": {"choices": [{"message": {
                "content": "[ctxprobe %s] FINAL anchor — quote this line "
                           "verbatim" % marker}}]}}

        state = model_sync.empty_state()
        state["contextProbes"] = {
            "um|done": {"source": "um", "model_id": "done",
                        "declared_context": 64000, "declared_at": "t",
                        "status": "verified", "observed_floor": 256000,
                        "attempts": 4, "last_run": "t0",
                        "last_error": None, "evidence_revision": 2},
        }
        state_text = json.dumps(state, ensure_ascii=False)
        endpoint = ("http://127.0.0.1:1/v1/chat/completions", None)
        with temp_sync_env(state_text=state_text) as paths:
            with mock.patch.object(
                    model_sync, "CONTEXT_PROBE_ENDPOINTS", {"um": endpoint}):
                before = _snapshot(paths)
                preview = console_model_actions.model_context_probe_preview(
                    "", {"retest": True}, _Ctx())
                _assert_unchanged(self, paths, before)
            entry = preview["records"][0]
            self.assertEqual(entry["estimated_requests"],
                             len(model_sync.PROBE_INPUT_STEPS))
            self.assertEqual(entry["input_chars_estimate"],
                             sum(model_sync.PROBE_INPUT_STEPS))
            self.assertEqual(entry["estimate_note"],
                             "retest：先归档旧代次再按全梯重测")
            self.assertTrue(entry["executable"])
            self.assertEqual(preview["totals"]["runnable_records"], 1)
            self.assertEqual(preview["totals"]["idle_records"], 0)
            with mock.patch.object(
                    model_sync, "CONTEXT_PROBE_ENDPOINTS", {"um": endpoint}), \
                mock.patch.object(model_sync, "_post_json", fake_post):
                run = console_model_actions.model_context_probe_run(
                    "", {"retest": True}, _Ctx())
        self.assertEqual(run["requests_used"],
                         len(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(entry["estimated_requests"],
                         run["requests_used"])

    def test_preview_rejects_unconfirmable_selections(self):
        # 条 1：预览与 run 同一校验口径（normalize_probe_selection /
        # resolve_probe_selection）——无法确认的选择 invalid_params 明确
        # 拒绝，绝不静默退回全队列；预览保持零写入。
        cases = [
            {"sources": ["mars"]},           # 未知来源
            {"sources": ["m365"]},           # 已暂停来源
            {"sources": []},                 # 空集合
            {"sources": "not-json"},         # 不可解码字符串
            {"models": {"um": ["ghost"]}},   # 模型不在队列
            {"models": {"m365": ["m"]}},     # 已暂停来源（models 形态）
            {"retest": "yes"},               # 非布尔 retest
        ]
        for params in cases:
            with self.subTest(params=params):
                with temp_sync_env(state_text=self._probe_state()) as paths:
                    before = _snapshot(paths)
                    with self.assertRaises(OperationError) as caught:
                        console_model_actions.model_context_probe_preview(
                            "", params, _Ctx())
                    _assert_unchanged(self, paths, before)
                self.assertEqual("invalid_params", caught.exception.code)

    def test_preview_no_params_keeps_legacy_full_queue_behavior(self):
        # 条 1：无参数 = 现状（全队列 pending/stale）——显式缺省、空 params
        # 与 retest=False 的结果逐字段一致；加性参数不改变既有读面行为。
        with temp_sync_env(state_text=self._probe_state()) as paths:
            results = [
                console_model_actions.model_context_probe_preview(
                    "", None, _Ctx()),
                console_model_actions.model_context_probe_preview(
                    "", {}, _Ctx()),
                console_model_actions.model_context_probe_preview(
                    "", {"retest": False}, _Ctx()),
            ]
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])
        totals = results[0]["totals"]
        self.assertEqual(totals["runnable_records"], 2)   # fresh/stale
        self.assertEqual(totals["blocked_records"], 0)
        self.assertEqual(totals["idle_records"], 1)       # done 未 retest
        self.assertEqual(totals["runnable_records"] +
                         totals["blocked_records"] +
                         totals["idle_records"], 3)

    def test_preview_retired_source_is_paused_with_zero_requests(self):
        # B0：退役来源（CLIProxy）没有探测端点，预览如实标为暂停、估算 0，
        # 不发任何请求，也不读它的旧凭据文件。
        state = model_sync.empty_state()
        state["contextProbes"] = {
            "cliproxy|gem": {"source": "cliproxy", "model_id": "gem",
                             "declared_context": 128000, "declared_at": "t",
                             "status": "pending", "observed_floor": None,
                             "attempts": 0, "last_run": None,
                             "last_error": None},
        }
        state_text = json.dumps(state, ensure_ascii=False)
        with temp_sync_env(state_text=state_text) as paths:
            result = console_model_actions.model_context_probe_preview(
                "", {}, _Ctx())
            entry = result["records"][0]
            self.assertFalse(entry["executable"])
            self.assertEqual(entry["estimated_requests"], 0)
            self.assertEqual(entry["input_chars_estimate"], 0)
            self.assertEqual(entry["estimate_note"],
                             "该来源已暂停，实际零请求")
            self.assertFalse(entry["probe_endpoint"])
            self.assertTrue(entry["paused"])
            totals = result["totals"]
            self.assertEqual(totals["runnable_records"], 0)
            self.assertEqual(totals["blocked_records"], 1)
            self.assertEqual(totals["idle_records"], 0)
            self.assertEqual(totals["estimated_requests"], 0)

    def test_preview_source_fallback_matches_runner(self):
        # 条 3：状态记录缺 source/model_id 字段（手改状态）时，预览按 key
        # 的 source|model_id 前后缀兜底，与 runner 同口径——不再显示 None
        # 或因未知来源零请求；估算与 runner 实际请求数一致。
        def fake_post(url, body, timeout=90, token=None):
            content = body["messages"][0]["content"]
            marker = re.search(r"\[ctxprobe (run\d+)\] FINAL anchor",
                               content).group(1)
            return {"status": 200, "body": {"choices": [{"message": {
                "content": "[ctxprobe %s] FINAL anchor — quote this line "
                           "verbatim" % marker}}]}}

        state = model_sync.empty_state()
        state["contextProbes"] = {
            "um|fallback": {"status": "pending", "observed_floor": None,
                            "attempts": 0, "last_run": None,
                            "last_error": None},
        }
        with temp_sync_env(state_text=json.dumps(
                state, ensure_ascii=False)) as paths:
            preview = console_model_actions.model_context_probe_preview(
                "", {}, _Ctx())
            endpoint = ("http://127.0.0.1:1/v1/chat/completions", None)
            with mock.patch.object(
                    model_sync, "CONTEXT_PROBE_ENDPOINTS",
                    {"um": endpoint}), \
                mock.patch.object(model_sync, "_post_json", fake_post):
                run = console_model_actions.model_context_probe_run(
                    "", {}, _Ctx())
        entry = preview["records"][0]
        row = run["results"][0]
        self.assertEqual(entry["source"], "um")
        self.assertEqual(entry["model_id"], "fallback")
        self.assertEqual((entry["source"], entry["model_id"]),
                         (row["source"], row["model_id"]))
        self.assertEqual(entry["estimated_requests"],
                         len(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(entry["estimated_requests"],
                         run["requests_used"])

    def test_preview_normalizes_garbage_legacy_values(self):
        # 条 4：attempts/observed_ceiling/declared_context 与 observed_floor
        # 同用整数守卫——存量垃圾值读作 None（未知），不原样透出。
        state = model_sync.empty_state()
        state["contextProbes"] = {
            "um|dirty": {"status": "pending", "observed_floor": None,
                         "attempts": "garbage", "observed_ceiling": "x",
                         "declared_context": "y", "declared_at": "t",
                         "last_run": None, "last_error": None},
        }
        with temp_sync_env(state_text=json.dumps(
                state, ensure_ascii=False)) as paths:
            before = _snapshot(paths)
            result = console_model_actions.model_context_probe_preview(
                "", {}, _Ctx())
            _assert_unchanged(self, paths, before)
        entry = result["records"][0]
        self.assertIsNone(entry["attempts"])
        self.assertIsNone(entry["observed_ceiling"])
        self.assertIsNone(entry["declared_context"])
        # 声明归一为未知：declared_context_known 派生 False。
        self.assertIs(entry["declared_context_known"], False)
        # 垃圾值不掩盖可跑性：pending+端点照常按全梯估算。
        self.assertEqual(entry["estimated_requests"],
                         len(model_sync.PROBE_INPUT_STEPS))

    def test_preview_totals_three_way_partition(self):
        # 条 5：totals 统一计数定义——runnable=本轮会消耗请求的记录（active
        # 且有端点且凭据可读）；blocked=本轮有资格但不可执行（active 但无
        # 端点或凭据不可读）；idle=本轮不消耗请求（verified/failed 且未
        # retest，或未选中）。三者之和 = records 条数。
        state = model_sync.empty_state()
        state["contextProbes"] = {
            "m365|blocked": {"source": "m365", "model_id": "blocked",
                             "status": "pending", "observed_floor": None,
                             "attempts": 0, "last_run": None,
                             "last_error": None},
            "um|done": {"source": "um", "model_id": "done",
                        "status": "verified", "observed_floor": 256000,
                        "attempts": 4, "last_run": None,
                        "last_error": None},
            "um|live": {"source": "um", "model_id": "live",
                        "status": "pending", "observed_floor": None,
                        "attempts": 0, "last_run": None,
                        "last_error": None},
        }
        with temp_sync_env(state_text=json.dumps(
                state, ensure_ascii=False)) as paths:
            result = console_model_actions.model_context_probe_preview(
                "", {}, _Ctx())
        totals = result["totals"]
        self.assertEqual(totals["blocked_records"], 1)   # active 无端点
        self.assertEqual(totals["idle_records"], 1)      # verified 未 retest
        self.assertEqual(totals["runnable_records"], 1)  # active 有端点
        self.assertEqual(totals["runnable_records"] +
                         totals["blocked_records"] +
                         totals["idle_records"],
                         len(result["records"]))
        self.assertEqual(totals["estimated_requests"],
                         len(model_sync.PROBE_INPUT_STEPS))

    def test_preview_counts_ladder_exhausted_pending_as_idle(self):
        # 条 5 补：pending 记录的 floor 已覆盖全梯时，run 对它零请求并直接
        # 置 verified（model_sync 的「既有实测证据已覆盖全部步进，无需新请求」
        # 分支），因此 preview 必须把它计为 idle 并如实说明，而不是
        # runnable（「本轮会消耗请求」）。
        def must_not_post(*args, **kwargs):
            raise AssertionError("runner must send zero requests")

        state = model_sync.empty_state()
        state["contextProbes"] = {
            "um|full": {"source": "um", "model_id": "full",
                        "status": "pending",
                        "observed_floor": max(model_sync.PROBE_INPUT_STEPS),
                        "attempts": 4, "last_run": None, "last_error": None},
        }
        state_text = json.dumps(state, ensure_ascii=False)
        endpoint = ("http://127.0.0.1:1/v1/chat/completions", None)
        with temp_sync_env(state_text=state_text) as paths:
            with mock.patch.object(
                    model_sync, "CONTEXT_PROBE_ENDPOINTS", {"um": endpoint}):
                preview = console_model_actions.model_context_probe_preview(
                    "", {}, _Ctx())
            totals = preview["totals"]
            self.assertEqual(totals["runnable_records"], 0)
            self.assertEqual(totals["idle_records"], 1)
            self.assertEqual(totals["blocked_records"], 0)
            self.assertEqual(totals["estimated_requests"], 0)
            entry = [r for r in preview["records"]
                      if r["model_id"] == "full"][0]
            self.assertIn("无需新请求", entry["estimate_note"])
            with mock.patch.object(
                    model_sync, "CONTEXT_PROBE_ENDPOINTS", {"um": endpoint}), \
                mock.patch.object(model_sync, "_post_json", must_not_post):
                run = console_model_actions.model_context_probe_run(
                    "", {"max_requests": 20, "max_tokens": 64}, _Ctx())
            self.assertEqual(run["requests_used"], 0)
            after = json.loads(paths["STATE_FILE"].read_text("utf-8"))
        self.assertEqual(after["contextProbes"]["um|full"]["status"],
                         "verified")

    def test_preview_registry_schema_declares_selection_params(self):
        # 预览的注册 schema 加性声明显式选择参数（console 层拒绝 schema
        # 外参数）；预览保持只读启用态（bump_generation 不翻代次）。
        spec = {s.name: s for s in console_adapters.build_registry()}[
            "model.context_probe_preview"]
        self.assertEqual(spec.param_schema, {
            "sources": {"type": "str", "max_len": 512},
            "models": {"type": "object"},
            "retest": {"type": "bool"},
        })
        self.assertTrue(spec.enabled)
        self.assertFalse(spec.bump_generation_on_success)

    def test_run_validates_explicit_budget_params(self):
        for params in ({"max_requests": 21}, {"max_requests": 0},
                       {"max_requests": True}, {"max_tokens": 65},
                       {"max_tokens": "8"}):
            with self.subTest(params=params):
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_context_probe_run(
                        "", params, _Ctx())
                self.assertEqual("invalid_params", caught.exception.code)

    def test_run_persists_updated_records_under_sync_lock(self):
        outcome = {"used": 3, "stopped": None,
                   "results": [{"key": "um|fresh", "requests": 3}]}
        with temp_sync_env(state_text=self._probe_state()) as paths:
            with mock.patch.object(
                    model_sync, "probe_context_queue",
                    return_value=outcome) as runner, \
                    mock.patch.object(model_sync, "SyncLock",
                                      wraps=model_sync.SyncLock) as lock:
                result = console_model_actions.model_context_probe_run(
                    "", {"max_requests": 5, "max_tokens": 32}, _Ctx())
            self.assertTrue(lock.called)
            runner.assert_called_once()
            endpoints, budget, records, should_continue = runner.call_args[0]
            self.assertEqual(budget, {"max_requests": 5, "max_tokens": 32})
            self.assertEqual(set(records),
                             {"um|fresh", "um|stale", "um|done"})
            self.assertTrue(callable(should_continue))
            state = json.loads(paths["STATE_FILE"].read_text("utf-8"))
            self.assertIn("contextProbes", state)
            self.assertEqual(result["requests_used"], 3)
            self.assertIsNone(result["stopped"])
            self.assertIn("未定价", result["cost_note"])

    def test_run_reports_budget_exhaustion_and_persists(self):
        outcome = {"used": 2, "stopped": "budget", "results": []}
        with temp_sync_env(state_text=self._probe_state()) as paths:
            with mock.patch.object(model_sync, "probe_context_queue",
                                   return_value=outcome):
                result = console_model_actions.model_context_probe_run(
                    "", {}, _Ctx())
        self.assertEqual(result["requests_used"], 2)
        self.assertEqual(result["stopped"], "budget")
        self.assertIn("budget", result["summary"])

    def test_run_passes_selection_and_retest_to_runner(self):
        outcome = {"used": 0, "stopped": None, "results": []}
        with temp_sync_env(state_text=self._probe_state()) as paths:
            with mock.patch.object(model_sync, "probe_context_queue",
                                   return_value=outcome) as runner:
                result = console_model_actions.model_context_probe_run(
                    "", {"models": {"um": ["fresh"]}, "retest": True}, _Ctx())
        endpoints, budget, records, should_continue = runner.call_args[0]
        self.assertEqual(set(records),
                         {"um|fresh", "um|stale", "um|done"})
        self.assertEqual(runner.call_args[1],
                         {"selection": {"um": {"fresh"}}, "retest": True})
        self.assertEqual(result["selection"], {"um": ["fresh"]})
        self.assertTrue(result["retest"])

    def test_run_sources_selection_expands_to_queued_models(self):
        outcome = {"used": 0, "stopped": None, "results": []}
        with temp_sync_env(state_text=self._probe_state()) as paths:
            with mock.patch.object(model_sync, "probe_context_queue",
                                   return_value=outcome) as runner:
                console_model_actions.model_context_probe_run(
                    "", {"sources": ["um"]}, _Ctx())
        # {source: None} expands to every model id queued for that source.
        self.assertEqual(runner.call_args[1]["selection"],
                         {"um": {"fresh", "stale", "done"}})
        self.assertIs(runner.call_args[1]["retest"], False)

    def test_run_accepts_sources_as_json_string(self):
        # The HTTP submit path encodes the array as a JSON string (the shared
        # param-schema vocabulary has no list type) — same contract as the
        # sync_apply scope params.
        outcome = {"used": 0, "stopped": None, "results": []}
        with temp_sync_env(state_text=self._probe_state()) as paths:
            with mock.patch.object(model_sync, "probe_context_queue",
                                   return_value=outcome) as runner:
                console_model_actions.model_context_probe_run(
                    "", {"sources": '["um"]'}, _Ctx())
        self.assertEqual(runner.call_args[1]["selection"],
                         {"um": {"fresh", "stale", "done"}})

    def test_run_rejects_unconfirmable_selections(self):
        # Plan-Revision 7 step 3: a selection that cannot be confirmed is
        # REJECTED (invalid_params) — the run never silently widens to the
        # full queue, no runner call happens, no state byte changes.
        cases = [
            {"sources": ["mars"]},                    # unknown source
            {"sources": ["m365"]},                    # paused source (F10)
            {"sources": []},                          # empty collection
            {"sources": "not-json"},                  # undecodable string
            {"sources": [42]},                        # non-string entries
            {"models": {}},                           # empty object
            {"models": {"um": []}},                   # empty id list
            {"models": {"um": ["fresh", "fresh"]}},   # duplicate ids
            {"models": {"mars": ["m"]}},              # unknown source
            {"models": {"m365": ["m"]}},              # paused source (F10)
            {"models": {"um": ["ghost"]}},            # model not in queue
            {"models": {"um": 3}},                    # non-list ids
            {"sources": ["qoder_global"],
             "models": {"um": ["fresh"]}},            # outside sources
            {"retest": "yes"},                        # non-bool retest
        ]
        for params in cases:
            with self.subTest(params=params):
                with temp_sync_env(state_text=self._probe_state()) as paths:
                    before = _snapshot(paths)
                    with mock.patch.object(
                            model_sync, "probe_context_queue") as runner:
                        with self.assertRaises(OperationError) as caught:
                            console_model_actions.model_context_probe_run(
                                "", params, _Ctx())
                        runner.assert_not_called()
                    _assert_unchanged(self, paths, before)
                self.assertEqual("invalid_params", caught.exception.code)

    def test_run_end_to_end_selection_limits_requests(self):
        # REAL handler → runner → state transaction closed loop with an
        # injected transport (zero network): only the selected record is
        # billed; the unselected record is untouched in the persisted state.
        def fake_post(url, body, timeout=90, token=None):
            content = body["messages"][0]["content"]
            marker = re.search(r"\[ctxprobe (run\d+)\] FINAL anchor",
                               content).group(1)
            return {"status": 200, "body": {"choices": [{"message": {
                "content": "[ctxprobe %s] FINAL anchor — quote this line "
                           "verbatim" % marker}}]}}

        state = model_sync.empty_state()
        state["contextProbes"] = {
            "um|sel": {"source": "um", "model_id": "sel",
                       "declared_context": 128000, "declared_at": "t",
                       "status": "pending", "observed_floor": None,
                       "attempts": 0, "last_run": None, "last_error": None},
            "um|skip": {"source": "um", "model_id": "skip",
                        "declared_context": 128000, "declared_at": "t",
                        "status": "pending", "observed_floor": None,
                        "attempts": 0, "last_run": None, "last_error": None},
        }
        with temp_sync_env(state_text=json.dumps(
                state, ensure_ascii=False)) as paths:
            with mock.patch.object(
                    model_sync, "CONTEXT_PROBE_ENDPOINTS",
                    {"um": ("http://127.0.0.1:1/v1/chat/completions",
                            None)}), \
                mock.patch.object(model_sync, "_post_json", fake_post):
                result = console_model_actions.model_context_probe_run(
                    "", {"models": {"um": ["sel"]}}, _Ctx())
            state_after = json.loads(
                paths["STATE_FILE"].read_text("utf-8"))
        self.assertEqual(result["requests_used"],
                         len(model_sync.PROBE_INPUT_STEPS))
        self.assertEqual(result["selection"], {"um": ["sel"]})
        self.assertEqual([row["key"] for row in result["results"]], ["um|sel"])
        sel = state_after["contextProbes"]["um|sel"]
        self.assertEqual(sel["status"], "verified")
        self.assertEqual(sel["attempts"], len(model_sync.PROBE_INPUT_STEPS))
        skip = state_after["contextProbes"]["um|skip"]
        self.assertEqual(skip["status"], "pending")
        self.assertEqual(skip["attempts"], 0)
        self.assertIsNone(skip["observed_floor"])

    def test_run_end_to_end_retest_archives_history(self):
        # REAL handler → runner closed loop: retest=True moves the verified
        # record's current generation into history and re-measures the full
        # ladder; the transaction persists both the archive and the new
        # generation.
        def fake_post(url, body, timeout=90, token=None):
            content = body["messages"][0]["content"]
            marker = re.search(r"\[ctxprobe (run\d+)\] FINAL anchor",
                               content).group(1)
            return {"status": 200, "body": {"choices": [{"message": {
                "content": "[ctxprobe %s] FINAL anchor — quote this line "
                           "verbatim" % marker}}],
                "usage": {"prompt_tokens": 61234}}}

        state = model_sync.empty_state()
        state["contextProbes"] = {
            "um|done": {"source": "um", "model_id": "done",
                        "declared_context": 64000, "declared_at": "t",
                        "status": "verified", "observed_floor": 256000,
                        "attempts": 4, "last_run": "t0",
                        "last_error": None, "last_success": "t1",
                        "token_evidence": 61234, "evidence_revision": 2,
                        "stale_reason": None,
                        "scheme_revision": model_sync.PROBE_SCHEME_REVISION,
                        "history": []},
        }
        with temp_sync_env(state_text=json.dumps(
                state, ensure_ascii=False)) as paths:
            with mock.patch.object(
                    model_sync, "CONTEXT_PROBE_ENDPOINTS",
                    {"um": ("http://127.0.0.1:1/v1/chat/completions",
                            None)}), \
                mock.patch.object(model_sync, "_post_json", fake_post):
                result = console_model_actions.model_context_probe_run(
                    "", {"retest": True}, _Ctx())
            state_after = json.loads(
                paths["STATE_FILE"].read_text("utf-8"))
        self.assertTrue(result["retest"])
        self.assertEqual(result["requests_used"],
                         len(model_sync.PROBE_INPUT_STEPS))
        record = state_after["contextProbes"]["um|done"]
        self.assertEqual(record["status"], "verified")
        self.assertEqual(record["observed_floor"], 256000)
        self.assertEqual(record["evidence_revision"], 3)
        self.assertEqual(record["attempts"], len(model_sync.PROBE_INPUT_STEPS))
        history = record["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "verified")
        self.assertEqual(history[0]["evidence_revision"], 2)
        self.assertEqual(history[0]["observed_floor"], 256000)
        self.assertEqual(history[0]["reason"], "retest")

    def test_registry_schema_declares_selection_params(self):
        # The registered param schema must accept the explicit selection
        # params (the console layer rejects params outside the schema), with
        # the budget caps unchanged.
        spec = {s.name: s for s in console_adapters.build_registry()}[
            "model.context_probe_run"]
        self.assertEqual(spec.param_schema, {
            "max_requests": {"type": "int", "min": 1, "max": 20},
            "max_tokens": {"type": "int", "min": 1, "max": 64},
            "sources": {"type": "str", "max_len": 512},
            "models": {"type": "object"},
            "retest": {"type": "bool"},
        })

    def test_persistence_value_error_is_probe_failed_not_invalid_params(self):
        # A ValueError from the persistence transaction is a product failure
        # (the billed requests already happened), never an argument problem:
        # the invalid_params mapping is narrowed to resolve_probe_selection.
        with temp_sync_env() as paths:
            with mock.patch.object(
                    model_sync, "CONTEXT_PROBE_ENDPOINTS", {}), \
                mock.patch.object(
                    model_sync, "apply_config_transaction",
                    side_effect=ValueError("state write")):
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_context_probe_run(
                        "", {"max_requests": 1, "max_tokens": 64}, _Ctx())
        self.assertEqual("probe_failed", caught.exception.code)
        self.assertFalse(paths["STATE_FILE"].exists())


class ContextOverrideActionTests(unittest.TestCase):
    """Plan-Revision 7 step 6: the manual context override actions write
    ONLY the model-sync state file (isolated temp layout); identity is
    validated against the CURRENT catalog (collected read-only via the mocked
    collector), version conflicts surface as version_conflict, and the read
    surface reports declared/override/effective honestly."""

    def _catalogs(self):
        return {"um": [CatalogModel("um", "model-a", context=128000),
                       CatalogModel("um", "model-b", context=None)]}

    def test_set_writes_override_and_reports_effective_value(self):
        with temp_sync_env() as paths:
            before = _snapshot(paths)
            with mock.patch.object(
                    model_sync, "collect_catalogs",
                    return_value=(self._catalogs(), [])) as collect:
                result = console_model_actions.model_context_override_set(
                    "", {"source": "um", "model_id": "model-a",
                         "context": 200000}, _Ctx())
            after = json.loads(paths["STATE_FILE"].read_text("utf-8"))
            collect.assert_called_once()
            record = after["contextOverrides"]["um|model-a"]
            self.assertEqual(record["context"], 200000)
            self.assertEqual(record["source"], "manual")
            self.assertIsInstance(record["set_at"], str)  # ISO stamp
            self.assertEqual(result["source"], "um")
            self.assertEqual(result["context"], 200000)
            self.assertEqual(result["set_at"], record["set_at"])
            self.assertIn("覆盖保存≠客户端已应用", result["note"])
            # The state file is the only write target.
            self.assertTrue(paths["STATE_FILE"].exists())
            self.assertEqual(after["contextOverrides"]["um|model-a"]["context"],
                             200000)

    def test_set_rejects_invalid_context_before_catalog_fetch(self):
        for context in (0, -5, "big", True, 1.5, None):
            with self.subTest(context=context):
                with temp_sync_env() as paths:
                    before = _snapshot(paths)
                    with mock.patch.object(
                            model_sync, "collect_catalogs") as collect:
                        with self.assertRaises(OperationError) as caught:
                            console_model_actions.model_context_override_set(
                                "", {"source": "um", "model_id": "model-a",
                                     "context": context}, _Ctx())
                        collect.assert_not_called()
                    _assert_unchanged(self, paths, before)
                self.assertEqual("invalid_params", caught.exception.code)

    def test_set_distinguishes_catalog_unavailable_from_model_absent(self):
        # Catalog collect FAILED (source absent from the result) → the
        # identity cannot be confirmed: honest refusal, NOT "model missing".
        with temp_sync_env():
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=({}, ["UM目录不可用"])) as \
                    collect:
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_context_override_set(
                        "", {"source": "um", "model_id": "model-a",
                             "context": 200000}, _Ctx())
                collect.assert_called_once()
        self.assertEqual("invalid_params", caught.exception.code)
        self.assertIn("当前目录不可用", caught.exception.message)
        # Catalog present but the model missing → precise identity error.
        with temp_sync_env():
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(self._catalogs(), [])):
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_context_override_set(
                        "", {"source": "um", "model_id": "ghost",
                             "context": 200000}, _Ctx())
        self.assertEqual("invalid_params", caught.exception.code)
        self.assertIn("模型不在当前目录", caught.exception.message)

    def test_set_rejects_unknown_and_paused_source(self):
        def raise_value_error(sources):
            raise ValueError("sources 必须为当前启用来源的子集（退役来源已不再同步）"
                             if sources == ["m365"] else
                             "sources 含未知来源：mars")

        with temp_sync_env():
            for source in ("mars", "m365"):
                with self.subTest(source=source):
                    with mock.patch.object(
                            model_sync, "collect_catalogs",
                            side_effect=raise_value_error):
                        with self.assertRaises(OperationError) as caught:
                            console_model_actions.model_context_override_set(
                                "", {"source": source, "model_id": "m",
                                     "context": 200000}, _Ctx())
                    self.assertEqual("invalid_params", caught.exception.code)

    def test_set_rejects_context_above_upper_bound(self):
        # F11: 覆盖加上界（model_sync.CONTEXT_OVERRIDE_MAX=10_000_000，
        # schema 同步声明 max）：10**15 被拒为 invalid_params，状态零写入。
        with temp_sync_env() as paths:
            before = _snapshot(paths)
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(self._catalogs(), [])):
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_context_override_set(
                        "", {"source": "um", "model_id": "model-a",
                             "context": 10 ** 15}, _Ctx())
            _assert_unchanged(self, paths, before)
        self.assertEqual("invalid_params", caught.exception.code)

    def test_set_version_conflict_maps_to_version_conflict_code(self):
        state = model_sync.empty_state()
        state["contextOverrides"] = {"um|model-a": {
            "context": 200000, "set_at": "2026-09-30T08:00:00+00:00",
            "source": "manual"}}
        state_text = json.dumps(state, ensure_ascii=False)
        with temp_sync_env(state_text=state_text) as paths:
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(self._catalogs(), [])):
                # Bare set on an existing override (caller expected none).
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_context_override_set(
                        "", {"source": "um", "model_id": "model-a",
                             "context": 64000}, _Ctx())
                self.assertEqual("version_conflict", caught.exception.code)
                # Stale expectation → conflict, state untouched.
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_context_override_set(
                        "", {"source": "um", "model_id": "model-a",
                             "context": 64000,
                             "expected_set_at": "2020-01-01T00:00:00+00:00"},
                        _Ctx())
                self.assertEqual("version_conflict", caught.exception.code)
                # 冲突是纯拒绝：磁盘文件保持夹具原样字节。
                self.assertEqual(
                    paths["STATE_FILE"].read_text("utf-8"), state_text)
                # Matching version → update allowed.
                result = console_model_actions.model_context_override_set(
                    "", {"source": "um", "model_id": "model-a",
                         "context": 64000,
                         "expected_set_at": "2026-09-30T08:00:00+00:00"},
                    _Ctx())
                self.assertEqual(result["context"], 64000)
                self.assertNotEqual(result["set_at"],
                                    "2026-09-30T08:00:00+00:00")

    def test_clear_idempotent_and_keeps_probe_history(self):
        state = model_sync.empty_state()
        state["contextOverrides"] = {"um|model-a": {
            "context": 200000, "set_at": "2026-09-30T08:00:00+00:00",
            "source": "manual"}}
        state["contextProbes"] = {"um|model-a": {
            "source": "um", "model_id": "model-a",
            "declared_context": 128000, "declared_at": "t",
            "status": "verified", "observed_floor": 256000,
            "attempts": 4, "last_run": "t", "last_error": None,
            "history": [{"observed_floor": 64000, "reason": "retest"}]}}
        state_text = json.dumps(state, ensure_ascii=False)
        with temp_sync_env(state_text=state_text) as paths:
            # Bare clear on an existing override → version conflict.
            with self.assertRaises(OperationError) as caught:
                console_model_actions.model_context_override_clear(
                    "", {"source": "um", "model_id": "model-a"}, _Ctx())
            self.assertEqual("version_conflict", caught.exception.code)
            result = console_model_actions.model_context_override_clear(
                "", {"source": "um", "model_id": "model-a",
                     "expected_set_at": "2026-09-30T08:00:00+00:00"}, _Ctx())
            self.assertTrue(result["existed"])
            self.assertTrue(result["changed"])
            self.assertIn("已清除", result["note"])
            after = json.loads(paths["STATE_FILE"].read_text("utf-8"))
            self.assertNotIn("um|model-a", after["contextOverrides"])
            # 探测历史证据原样保留：恢复默认不删除实测记录。
            self.assertEqual(after["contextProbes"]["um|model-a"]["history"],
                             [{"observed_floor": 64000, "reason": "retest"}])
            self.assertEqual(after["contextProbes"]["um|model-a"]
                             ["observed_floor"], 256000)
            again = console_model_actions.model_context_override_clear(
                "", {"source": "um", "model_id": "model-a"}, _Ctx())
            self.assertTrue(again["cleared"])
            self.assertFalse(again["existed"])
            # 幂等成功的措辞必须与 existed=False 一致：不能声称「已恢复默认」。
            self.assertNotIn("已恢复默认", again["note"])
            self.assertIn("不存在", again["note"])

    def test_read_surface_reports_malformed_and_missing_entries(self):
        state = model_sync.empty_state()
        state["contextOverrides"] = {
            # 垃圾记录（非字典）不炸只读读面：如实标 malformed，绝不应用。
            "um|model-a": "junk",
            # 模型不在当前目录：inert 的唯一合法原因之一（另一是值损坏）。
            "um|ghost": {"context": 64000,
                         "set_at": "2026-09-30T08:00:00+00:00",
                         "source": "manual"},
        }
        catalogs = {"um": [CatalogModel("um", "model-a", context=128000),
                           CatalogModel("um", "model-b", context=None)]}
        rows = {row["model_id"]: row
                for row in model_sync.context_override_surface(
                    state, catalogs)}
        self.assertEqual(rows["model-a"]["override_state"], "malformed")
        self.assertEqual(rows["model-a"]["effective_source"], "declared")
        self.assertEqual(rows["model-a"]["effective_context"], 128000)
        self.assertEqual(rows["model-b"]["override_state"], "none")
        self.assertEqual(rows["model-b"]["effective_source"], "unknown")
        inert = rows["ghost"]
        self.assertEqual(inert["override_context"], 64000)
        self.assertIsNone(inert["effective_context"])
        self.assertEqual(inert["effective_source"], "unknown")
        self.assertFalse(inert["override_applied"])
        self.assertEqual(inert["override_state"], "model_missing")

    def test_read_surface_reports_declared_override_and_inert(self):
        state = model_sync.empty_state()
        state["contextOverrides"] = {
            "um|model-a": {
                "context": 200000,
                "set_at": "2026-09-30T08:00:00+00:00",
                "source": "manual"},
            "um|ghost": {
                "context": 64000,
                "set_at": "2026-09-30T09:00:00+00:00",
                "source": "manual"},
        }
        with temp_sync_env(state_text=json.dumps(
                state, ensure_ascii=False)) as paths:
            before = _snapshot(paths)
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(self._catalogs(), [])):
                result = console_model_actions.model_context_overrides(
                    "", {}, _Ctx())
            _assert_unchanged(self, paths, before)
        rows = {row["source"] + "|" + row["model_id"]: row
                for row in result["overrides"]}
        self.assertEqual(rows["um|model-a"]["effective_source"], "override")
        self.assertEqual(rows["um|model-a"]["effective_context"], 200000)
        self.assertEqual(rows["um|model-a"]["override_at"],
                         "2026-09-30T08:00:00+00:00")
        self.assertTrue(rows["um|model-a"]["override_applied"])
        self.assertEqual(rows["um|model-a"]["override_state"], "applied")
        self.assertIsNone(rows["um|model-b"]["declared_context"])
        self.assertEqual(rows["um|model-b"]["effective_source"], "unknown")
        self.assertFalse(rows["um|model-b"]["override_applied"])
        self.assertEqual(rows["um|model-b"]["override_state"], "none")
        inert = rows["um|ghost"]
        self.assertEqual(inert["override_context"], 64000)
        self.assertIsNone(inert["effective_context"])
        self.assertEqual(inert["effective_source"], "unknown")
        self.assertFalse(inert["override_applied"])
        self.assertEqual(inert["override_state"], "model_missing")
        self.assertIn("覆盖保存≠客户端已应用", result["note"])

    def test_read_surface_override_on_none_declaration_is_applied(self):
        # F1: 目录声明 None + 合法人工覆盖 → effective_source=override、
        # effective_context=覆盖值、override_state=applied。
        state = model_sync.empty_state()
        state["contextOverrides"] = {"um|model-b": {
            "context": 200000, "set_at": "2026-09-30T10:00:00+00:00",
            "source": "manual"}}
        with temp_sync_env(state_text=json.dumps(
                state, ensure_ascii=False)) as paths:
            before = _snapshot(paths)
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(self._catalogs(), [])):
                result = console_model_actions.model_context_overrides(
                    "", {}, _Ctx())
            _assert_unchanged(self, paths, before)
        rows = {row["source"] + "|" + row["model_id"]: row
                for row in result["overrides"]}
        row = rows["um|model-b"]
        self.assertEqual(row["declared_context"], None)
        self.assertEqual(row["effective_source"], "override")
        self.assertEqual(row["effective_context"], 200000)
        self.assertTrue(row["override_applied"])
        self.assertEqual(row["override_state"], "applied")

    def test_read_surface_equal_override_stays_declared(self):
        # F1: 合法覆盖但等于目录声明——值未改写，来源不夸大：
        # effective_source 保持 declared、effective_context 为声明值、
        # override_state=equal、override_applied=False。
        state = model_sync.empty_state()
        state["contextOverrides"] = {"um|model-a": {
            "context": 128000, "set_at": "2026-09-30T10:00:00+00:00",
            "source": "manual"}}
        with temp_sync_env(state_text=json.dumps(
                state, ensure_ascii=False)):
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(self._catalogs(), [])):
                result = console_model_actions.model_context_overrides(
                    "", {}, _Ctx())
        rows = {row["source"] + "|" + row["model_id"]: row
                for row in result["overrides"]}
        row = rows["um|model-a"]
        self.assertEqual(row["effective_source"], "declared")
        self.assertEqual(row["effective_context"], 128000)
        self.assertFalse(row["override_applied"])
        self.assertEqual(row["override_state"], "equal")

    def test_registry_schema_declares_override_params(self):
        specs = {s.name: s for s in console_adapters.build_registry()}
        self.assertEqual(
            specs["model.context_override_set"].param_schema, {
                "source": {"type": "str", "max_len": 128},
                "model_id": {"type": "str", "max_len": 128},
                # F11: context 覆盖下界 1、上界 CONTEXT_OVERRIDE_MAX。
                "context": {"type": "int", "min": 1, "max": 10_000_000},
                "expected_set_at": {"type": "str", "max_len": 64},
            })
        self.assertEqual(
            specs["model.context_override_clear"].param_schema, {
                "source": {"type": "str", "max_len": 128},
                "model_id": {"type": "str", "max_len": 128},
                "expected_set_at": {"type": "str", "max_len": 64},
            })
        self.assertEqual(specs["model.context_overrides"].param_schema, {
            "local_only": {"type": "bool"}, "models": {"type": "object"}})
        self.assertTrue(specs["model.context_overrides"].enabled)
        self.assertTrue(specs["model.context_override_set"].enabled)
        self.assertTrue(specs["model.context_override_clear"].enabled)


class CancelTests(unittest.TestCase):
    def test_cancelled_ctx_raises_before_any_side_effect(self):
        ctx = _Ctx(cancelled=True)
        cases = [
            (console_model_actions.model_catalog, {}),
            (console_model_actions.model_context_probe_preview, {}),
            (console_model_actions.model_context_probe_run, {}),
            (console_model_actions.model_context_overrides, {}),
            (console_model_actions.model_context_override_set, {}),
            (console_model_actions.model_context_override_clear, {}),
        ]
        with mock.patch.object(model_sync, "collect_catalogs") as collect:
            for handler, params in cases:
                with self.assertRaises(OperationCancelled):
                    handler("", params, ctx)
            collect.assert_not_called()


class RetiredModelSyncSurfaceTests(unittest.TestCase):
    """Ticket #32: DSH/ZCode consumers and the model sync/settings/task
    actions are gone; only catalog, context probe and overrides remain."""

    RETIRED_ACTIONS = (
        "model.sync_preview", "model.sync_pending",
        "model.sync_history", "model.sync_apply",
        "model.settings_save", "model.settings_preview",
        "model.settings_apply",
        "model.task_status", "model.task_install",
        "model.task_disable",
    )
    RETIRED_HANDLERS = (
        "model_sync_preview", "model_sync_pending",
        "model_sync_history", "model_sync_apply",
        "model_settings_save", "model_settings_preview",
        "model_settings_apply",
        "model_task_status", "model_task_install",
        "model_task_disable",
    )
    RETIRED_SYNC_HELPERS = (
        "run_sync", "collect_consumers", "KNOWN_CONSUMERS",
        "render_provider_config_sync", "render_dsh_sync",
        "render_zcode_sync",
    )
    RETIRED_MODULES = ("model_sync.config_adapters", "model_sync.reconcile")
    RETIRED_SCRIPTS = (
        "invoke-model-sync.ps1",
        "manage-model-sync-task.ps1",
        "show-model-sync-notification.ps1",
    )

    def test_retired_action_handlers_are_gone(self):
        for name in self.RETIRED_HANDLERS:
            self.assertFalse(hasattr(console_model_actions, name), name)

    def test_retired_actions_are_not_registered(self):
        specs = {s.name for s in console_adapters.build_registry()}
        for action in self.RETIRED_ACTIONS:
            self.assertNotIn(action, specs)

    def test_consumer_collection_and_sync_helpers_are_gone(self):
        for name in self.RETIRED_SYNC_HELPERS:
            self.assertFalse(hasattr(model_sync, name), name)

    def test_retired_modules_are_gone(self):
        import importlib.util
        for name in self.RETIRED_MODULES:
            self.assertIsNone(importlib.util.find_spec(name), name)

    def test_retired_sync_scripts_are_gone(self):
        root = Path(__file__).resolve().parents[1]
        for name in self.RETIRED_SCRIPTS:
            self.assertFalse((root / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
